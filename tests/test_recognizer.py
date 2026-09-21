"""识别器测试：中文描述 → 结构化语义。

这一层是"自动识别"和"穷举"的分水岭，所以测试要盯住的不是
"这句话认没认出来"，而是**认出来的东西对不对**。

下面每个 ``class`` 里带「回归」标记的用例，都对应一个真实踩过的坑：
它们曾经悄悄算出错误的 DPS，而没有任何报错。
"""

from __future__ import annotations

import math
import unittest

from fixtures import operator  # noqa: F401  （导入以设定 sys.path）
from arkdps import lexicon, recognizer
from arkdps.recognizer import Recognition, analyze, analyze_clause, split_clauses, summarize


def clause(text: str):
    return analyze_clause(text)


class TestSplitClauses(unittest.TestCase):
    def test_splits_on_comma(self):
        self.assertEqual(
            split_clauses("攻击力提升，攻击速度提升"),
            ["攻击力提升", "攻击速度提升"],
        )

    def test_does_not_split_inside_brackets(self):
        """回归：括号里的逗号是数值的一部分，切开数值就废了。"""
        self.assertEqual(
            split_clauses("攻击间隔缩短(-0.22)，攻击力提升至110%"),
            ["攻击间隔缩短(-0.22)", "攻击力提升至110%"],
        )

    def test_all_bracket_styles(self):
        for open_ch, close_ch in [("（", "）"), ("(", ")"), ("【", "】"), ("[", "]")]:
            with self.subTest(bracket=open_ch):
                text = f"攻击力提升{open_ch}+10，每秒{close_ch}，攻速提升"
                self.assertEqual(
                    split_clauses(text), [f"攻击力提升{open_ch}+10，每秒{close_ch}", "攻速提升"]
                )

    def test_nested_brackets(self):
        self.assertEqual(
            split_clauses("获得{（内，层）}效果，攻击力提升"),
            ["获得{（内，层）}效果", "攻击力提升"],
        )

    def test_various_separators(self):
        self.assertEqual(
            split_clauses("攻击力提升；攻速提升。间隔缩短\n目标数增加、段数增加"),
            ["攻击力提升", "攻速提升", "间隔缩短", "目标数增加", "段数增加"],
        )

    def test_drops_punctuation_only_pieces(self):
        self.assertEqual(split_clauses("攻击力提升，，；；"), ["攻击力提升"])

    def test_splits_at_conjunction(self):
        """回归：「且」是真实存在的子句连接词。

        「（持续10秒），且优先攻击未获得此效果的敌人」出自真实语料。
        不切开会把两个效果塞进一个子句。
        """
        self.assertEqual(
            split_clauses("攻击范围扩大且攻击力+50%"),
            ["攻击范围扩大", "攻击力+50%"],
        )

    def test_conjunction_after_comma_leaves_no_empty_piece(self):
        self.assertEqual(
            split_clauses("（持续10秒），且优先攻击未获得此效果的敌人"),
            ["（持续10秒）", "优先攻击未获得此效果的敌人"],
        )

    def test_unbalanced_close_bracket_does_not_go_negative(self):
        """多余的右括号不该让深度变成负数，否则后面的分隔符全部失效。"""
        self.assertEqual(
            split_clauses("攻击力），攻速提升"), ["攻击力）", "攻速提升"]
        )

    def test_empty_input(self):
        self.assertEqual(split_clauses(""), [])


class TestParseNumber(unittest.TestCase):
    def test_arabic(self):
        self.assertEqual(lexicon.parse_number("攻击力+200%"), 200.0)

    def test_decimal(self):
        self.assertEqual(lexicon.parse_number("攻击间隔缩短(-0.22)"), -0.22)

    def test_signed_plus(self):
        self.assertEqual(lexicon.parse_number("+50"), 50.0)

    def test_chinese_numeral(self):
        self.assertEqual(lexicon.parse_number("攻击变为二连击"), 2.0)

    def test_chinese_numeral_not_truncated(self):
        """回归：「十二」不能被「十」截断成 10。"""
        self.assertEqual(lexicon.parse_number("攻击变为十二连击"), 12.0)
        self.assertEqual(lexicon.parse_number("攻击变为二十连击"), 20.0)

    def test_chinese_numeral_bare(self):
        self.assertEqual(lexicon.parse_number("并连续攻击三次"), 3.0)

    def test_no_number(self):
        self.assertIsNone(lexicon.parse_number("攻击力翻倍"))
        self.assertIsNone(lexicon.parse_number(""))
        self.assertIsNone(lexicon.parse_number("持续时间无限"))

    def test_arabic_wins_over_chinese(self):
        self.assertEqual(lexicon.parse_number("每次攻击5次，共三次"), 5.0)


class TestAttributeLexicon(unittest.TestCase):
    def test_bare_attack_is_not_a_damage_attribute(self):
        """回归：光秃秃的「攻击」在技能描述里多半是动词。

        「同时攻击至多6个目标」的修饰对象是「目标」而不是攻击力。
        把「攻击」当 atk 会抢走本该属于 targets 的匹配，目标数就丢了。
        """
        words = lexicon.ATTRIBUTES["atk"]
        self.assertNotIn("攻击", words)
        self.assertIn("攻击力", words)

    def test_attack_speed_not_stolen(self):
        """「攻击速度」不能被「攻击」抢走。"""
        self.assertEqual(lexicon.find_attribute("攻击速度+12"), "aspd")

    def test_longest_word_wins(self):
        self.assertEqual(lexicon.find_attribute("攻击间隔缩短"), "interval")
        self.assertEqual(lexicon.find_attribute("攻击力提升"), "atk")

    def test_unit_is_not_a_target_keyword(self):
        """回归：「优先攻击空中单位」里的「单位」是名词，不是目标数量。

        放进 targets 词表会让这条纯索敌规则被误判成"影响 DPS"。
        """
        self.assertNotIn("单位", lexicon.ATTRIBUTES["targets"])

    def test_targets_and_hits(self):
        self.assertEqual(lexicon.find_attribute("目标数增加"), "targets")
        self.assertEqual(lexicon.find_attribute("攻击变为连射"), "hits")


class TestRelationLexicon(unittest.TestCase):
    def test_set_to_beats_increase(self):
        """「提高至」必须优先于「提高」，否则倍率会被当成加算百分比。"""
        self.assertEqual(lexicon.find_relation("攻击力提升至110%"), "set_to")
        self.assertEqual(lexicon.find_relation("攻击力提升50%"), "increase")

    def test_proportional(self):
        self.assertEqual(lexicon.find_relation("相当于攻击力145%"), "proportional")

    def test_ignore(self):
        self.assertEqual(lexicon.find_relation("无视20点法术抗性"), "ignore")
        self.assertEqual(lexicon.find_relation("防御力穿透"), "ignore")

    def test_shorten_counts_as_decrease(self):
        """回归：「缩短」曾经不在 decrease 词表里。

        结果是「攻击间隔缩短0.22」（不带负号的写法）整条映射不出来，
        而带负号的「缩短(-0.22)」却能工作——两种写法表现不一致。
        """
        self.assertEqual(lexicon.find_relation("攻击间隔缩短0.22"), "decrease")

    def test_shorten_to_still_beats_shorten(self):
        """加了「缩短」之后，「缩短至」必须仍然优先命中 set_to。"""
        self.assertEqual(lexicon.find_relation("攻击间隔缩短至70%"), "set_to")

    def test_multiply(self):
        self.assertEqual(lexicon.find_relation("造成2倍伤害"), "multiply")

    def test_quantity_relations(self):
        self.assertEqual(lexicon.find_relation("至多6个目标"), "max_of")
        self.assertEqual(lexicon.find_relation("至少3次"), "min_of")


class TestCountableCorrection(unittest.TestCase):
    """回归：数量关系词只修饰可数属性。"""

    def test_max_of_targets(self):
        result = clause("同时攻击至多6个目标")
        self.assertIs(result.kind, Recognition.RECOGNIZED)
        self.assertEqual(result.triple.attribute, "targets")
        self.assertEqual(result.triple.value, 6.0)
        self.assertEqual(result.triple.unit, "个")

    def test_countable_finder_picks_targets_or_hits(self):
        self.assertEqual(
            recognizer._find_countable_attribute("同时攻击至多6个目标"), "targets"
        )
        self.assertEqual(
            recognizer._find_countable_attribute("攻击变为连射"), "hits"
        )

    def test_countable_finder_is_none_without_countable_words(self):
        """没有可数属性时不能凭空造一个出来。"""
        for text in ("攻击力提升50%", "攻击速度+12", "持续时间无限"):
            with self.subTest(text=text):
                self.assertIsNone(recognizer._find_countable_attribute(text))


class TestIrrelevantConcepts(unittest.TestCase):
    """能认出「这与 DPS 无关」也是识别能力——否则报告会被噪音淹没。"""

    def test_attack_range(self):
        result = clause("攻击范围扩大")
        self.assertIs(result.kind, Recognition.IRRELEVANT)
        self.assertEqual(result.concept, "攻击范围")
        self.assertIs(result.affects_damage, False)

    def test_targeting_rule(self):
        result = clause("优先攻击空中单位")
        self.assertIs(result.kind, Recognition.IRRELEVANT)
        self.assertEqual(result.concept, "索敌规则")
        self.assertIs(result.affects_damage, False)

    def test_deployment_cost(self):
        result = clause("部署费用降低")
        self.assertIs(result.kind, Recognition.IRRELEVANT)
        self.assertIs(result.affects_damage, False)

    def test_blocking_trait_is_irrelevant(self):
        """回归：「能够阻挡两个敌人」说的是**能挡住几个**，不是"同时攻击几个"。

        「敌人」会让它被认成 targets，于是这个干员的总 DPS 直接翻倍。
        实测影响 72 个干员的特性——重装、先锋、近卫几乎全都中招。
        """
        for text in (
            "能够阻挡两个敌人",
            "能够阻挡三个敌人",
            "能够阻挡一个敌人",
            "起飞后能够阻挡2个飞行敌人",
        ):
            with self.subTest(text=text):
                result = clause(text)
                self.assertIs(
                    result.kind, Recognition.IRRELEVANT,
                    f"{text!r} 说的是挡几个，与输出无关",
                )
                self.assertIs(result.affects_damage, False)

    def test_bare_blocking_still_means_targets(self):
        """但光秃秃的「阻挡」不能被一起误伤。

        「同时攻击阻挡的所有敌人」里的「阻挡」确实在说打几个。
        """
        result = clause("同时攻击阻挡的所有敌人")
        self.assertIsNot(result.kind, Recognition.IRRELEVANT)

    def test_healing_is_irrelevant(self):
        """回归：「每攻击到一个敌人回复自身50生命」

        它同时含「敌人」和数字 50，不拦下来会被认成"同时攻击 50 个目标"，
        总 DPS 直接翻 50 倍。
        """
        result = clause("每攻击到一个敌人回复自身50生命")
        self.assertIs(result.kind, Recognition.IRRELEVANT)
        self.assertIs(result.affects_damage, False)

    def test_chain_jump_is_a_delivery_mode(self):
        """回归：「且会在3个敌人间跳跃，每次跳跃伤害降低15%」

        链式跳跃每跳伤害递减，用单一的目标数表达不了。
        对单体 DPS 而言它不改变"打中时的数值"，所以归到攻击模式。
        """
        result = clause("且会在3个敌人间跳跃，每次伤害降低15%")
        self.assertIs(result.kind, Recognition.IRRELEVANT)

    def test_charge_storage_is_not_a_target_count(self):
        """回归：「储存起来之后一齐发射（最多3个）」里的 3 是蓄力次数。"""
        result = clause("在找不到攻击目标时可以将攻击能量储存起来之后一齐发射（最多3个）")
        self.assertIs(result.kind, Recognition.IRRELEVANT)

    def test_damage_clause_with_jump_is_not_swallowed(self):
        """复合子句检查必须保护带攻击力倍率的跳跃攻击。

        「攻击造成攻击力240%的法术伤害并以短暂间隔跳跃至其他3名敌人造成攻击力120%的法术伤害」
        里虽然有「跳跃」，但更关键的是它有攻击力倍率——不能被判成无关。
        """
        result = clause("攻击造成攻击力240%的法术伤害并跳跃至其他3名敌人")
        self.assertIsNot(result.kind, Recognition.IRRELEVANT)
        self.assertIs(result.affects_damage, True)

    def test_control_effect(self):
        for text in ("晕眩", "停顿", "击退"):
            with self.subTest(text=text):
                self.assertIs(clause(text).kind, Recognition.IRRELEVANT)

    def test_irrelevant_does_not_swallow_compound_clause(self):
        """含无关概念但**同时**含伤害属性的复合子句不能被简单放过。

        「攻击范围扩大」本身无害，但若同一子句里还有「攻击力+50%」，
        整句判成无关就会把攻击力加成静默丢掉。
        """
        result = clause("攻击范围扩大且攻击力+50%")
        self.assertIsNot(
            result.affects_damage,
            False,
            "同一子句里的攻击力加成不能被「攻击范围」掩盖掉",
        )

    def test_compound_clause_is_split_at_the_conjunction(self):
        """更根本的解法：在连接词处切开，两个效果各归各的。"""
        clauses = analyze("攻击范围扩大且攻击力+50%")
        self.assertEqual(len(clauses), 2)

        self.assertIs(clauses[0].kind, Recognition.IRRELEVANT)
        self.assertIs(clauses[0].affects_damage, False)

        self.assertIs(clauses[1].kind, Recognition.RECOGNIZED)
        self.assertEqual(clauses[1].triple.attribute, "atk")
        self.assertEqual(clauses[1].triple.value, 50.0)

    def test_compound_warning_does_not_fire_when_attribute_is_critical(self):
        """「远程攻击不再降低攻击力」里的「远程攻击」是无关概念，

        但整句的属性是 trait（**确实影响伤害**），必须照常识别，
        不能被复合子句告警拦下来。
        """
        result = clause("远程攻击不再降低攻击力")
        self.assertIs(result.kind, Recognition.RECOGNIZED)
        self.assertEqual(result.triple.attribute, "trait")
        self.assertIs(result.affects_damage, True)

    def test_generic_words_do_not_trigger_the_compound_warning(self):
        """回归：复合子句的判据不能用「持续」「敌人」「目标」这类泛词。

        ``duration`` / ``targets`` 的词太泛，和确定无关的子句大量共现。
        实测这三类一共上百条，一旦被误伤就全变成"有风险"，
        IRRELEVANT 词表想解决的"报告被噪音淹没"就回来了。
        """
        cases = [
            "技能持续时间内逐渐获得12点部署费用",   # 「持续」不该触发
            "持续时间内回复总共18点部署费用",
            "优先攻击未处于损伤爆发期间的敌人",     # 「敌人」不该触发
            "自身嘲讽等级更容易受到敌人攻击",
            "立即寻找前方生命值最低的目标",         # 「目标」不该触发
        ]
        for text in cases:
            with self.subTest(text=text):
                result = clause(text)
                self.assertIs(
                    result.kind, Recognition.IRRELEVANT,
                    f"{text!r} 应当被判为与 DPS 无关",
                )
                self.assertIs(result.affects_damage, False)

    def test_attack_range_shortened(self):
        """回归：真实语料用的是「缩短」，词表里只收了「缩小」。"""
        result = clause("攻击范围缩短")
        self.assertIs(result.kind, Recognition.IRRELEVANT)
        self.assertIs(result.affects_damage, False)


class TestConditionDetection(unittest.TestCase):
    """带条件的效果依赖战场情况，不能无条件套用。"""

    def test_proportional_contains_the_char_dang_but_is_not_conditional(self):
        """回归：单字「当」会撞上「相**当**于」。

        曾经用「当」当条件标记，结果「相当于攻击力145%」被误判成有条件，
        该技能的伤害倍率被整个隔离掉了——DPS 直接少一大截。
        """
        result = clause("相当于攻击力145%")
        self.assertIsNone(result.condition)
        self.assertFalse(result.is_conditional)

    def test_single_char_marker_dang_is_not_used(self):
        """回归：单字「当」会撞上「相**当**于」。

        曾经用「当」当条件标记，结果「相当于攻击力145%」被误判成有条件，
        该技能的伤害倍率被整个隔离掉了——DPS 直接少一大截。
        """
        self.assertNotIn("当", recognizer._CONDITION_MARKERS)

    def test_neutral_damage_phrases_are_never_conditional(self):
        """条件标记的判据是"不会误伤常用词"，不是"必须多于一个字"。

        这里用一批**必须无条件**的真实表述来守住这个性质：
        任何一条被误判成有条件，效果就会被隔离出默认计算。
        """
        neutral = [
            "相当于攻击力145%",
            "攻击力+200%",
            "攻击间隔缩短(-0.22)",
            "同时攻击至多6个目标",
            "持续时间无限",
            "攻击速度+12",
            "无视20点法术抗性",
            "攻击变为五连射",
            "第一天赋效果提升至1.5倍",
        ]
        for text in neutral:
            with self.subTest(text=text):
                self.assertIsNone(
                    clause(text).condition,
                    f"{text!r} 不该被判定为有条件",
                )

    def test_decoy_words_do_not_trigger_conditions(self):
        """「若干」含有标记「若」，但它是表示"几个"的限定词，不是条件。

        真实语料里出现过：「创造若干从空中直线向地面移动的弹道」。
        """
        self.assertIsNone(clause("创造若干从空中直线向地面移动的弹道").condition)
        self.assertIn("若干", recognizer._CONDITION_DECOYS)

    def test_real_ruo_condition_still_works(self):
        """假朋友处理不能把真正的「若」条件一起挡掉。"""
        result = clause("若目标处于停顿状态则攻击力提升至150%")
        self.assertEqual(result.condition, "若")

    def test_remote_attack_condition(self):
        result = clause("但此时攻击力降低至80%")
        self.assertEqual(result.condition, "但此时")
        self.assertTrue(result.is_conditional)

    def test_unblocked_enemy_condition(self):
        result = clause("攻击未被阻挡的敌人时攻击力提升至110%")
        self.assertIsNotNone(result.condition)

    def test_elite_condition(self):
        result = clause("精英2时攻击力+10%")
        self.assertEqual(result.condition, "精英")

    def test_condition_survives_on_irrelevant_clauses_too(self):
        result = clause("精英2时攻击范围扩大")
        self.assertIs(result.kind, Recognition.IRRELEVANT)
        self.assertEqual(result.condition, "精英")

    def test_plain_clause_has_no_condition(self):
        for text in ("攻击力+200%", "攻击速度+12", "攻击间隔缩短(-0.22)"):
            with self.subTest(text=text):
                self.assertIsNone(clause(text).condition)


class TestStopAttack(unittest.TestCase):
    def test_stop_attack_is_recognized_and_matters(self):
        """「停止攻击」没有数值，但语义明确且**确实影响 DPS**。"""
        result = clause("停止攻击")
        self.assertIs(result.kind, Recognition.RECOGNIZED)
        self.assertEqual(result.triple.attribute, "atk")
        self.assertEqual(result.triple.relation, "stop")
        self.assertIsNone(result.triple.value)
        self.assertIs(result.affects_damage, True)

    def test_variants(self):
        for text in ("无法普通攻击", "不再进行普通攻击", "不能普通攻击"):
            with self.subTest(text=text):
                self.assertIs(clause(text).affects_damage, True)


class TestPenetrationAttributes(unittest.TestCase):
    """回归：防御 / 法抗要分「敌人的」和「自己的」两种情况。

    只看属性词分不出来——「无视20点法术抗性」说的是敌人，
    「防御力+100%」说的是干员自己。区分它们的唯一信号是关系词。
    """

    def test_ignoring_enemy_res_affects_damage(self):
        result = clause("无视20点法术抗性")
        self.assertIs(result.kind, Recognition.RECOGNIZED)
        self.assertEqual(result.triple.attribute, "res")
        self.assertEqual(result.triple.relation, "ignore")
        self.assertEqual(result.triple.value, 20.0)
        self.assertIs(result.affects_damage, True)

    def test_ignoring_enemy_defense_affects_damage(self):
        result = clause("无视50%防御力")
        self.assertIs(result.affects_damage, True)

    def test_own_defense_buff_does_not_affect_damage(self):
        """自己的防御力加成与输出无关——不排除掉会污染"待确认"列表。"""
        for text in ("防御力提升至200%", "防御力+100%"):
            with self.subTest(text=text):
                self.assertIs(clause(text).affects_damage, False)

    def test_own_res_buff_does_not_affect_damage(self):
        self.assertIs(clause("法术抗性提升").affects_damage, False)

    def test_penetration_attributes_not_in_damage_critical(self):
        self.assertFalse(lexicon.PENETRATION_ATTRIBUTES & lexicon.DAMAGE_CRITICAL)

    def test_numberless_penetration_is_unknown_but_flagged(self):
        """「无视防御力」没给数值——是未知，但必须标成可能影响 DPS。"""
        result = clause("无视防御力")
        self.assertIs(result.kind, Recognition.UNKNOWN)
        self.assertIs(result.affects_damage, True)


class TestNumberlessAttributes(unittest.TestCase):
    """回归：有些属性**没有数值也是有意义的**，它们表达的是"切换"。"""

    def test_stance(self):
        """回归：stance 漏出 _NUMBERLESS_ATTRIBUTES 时，形态切换技能整条认不出来。"""
        result = clause("在下列状态和初始状态间切换")
        self.assertIs(result.kind, Recognition.RECOGNIZED)
        self.assertEqual(result.triple.attribute, "stance")
        self.assertIs(result.affects_damage, True)

    def test_termination(self):
        result = clause("打完后技能结束")
        self.assertIs(result.kind, Recognition.RECOGNIZED)
        self.assertEqual(result.triple.attribute, "termination")

    def test_trait_override(self):
        result = clause("远程攻击不再降低攻击力")
        self.assertIs(result.kind, Recognition.RECOGNIZED)
        self.assertEqual(result.triple.attribute, "trait")

    def test_damage_type(self):
        result = clause("伤害类型变为法术")
        self.assertIs(result.kind, Recognition.RECOGNIZED)
        self.assertEqual(result.triple.attribute, "damage_type")

    def test_set_membership_is_explicit(self):
        self.assertEqual(
            recognizer._NUMBERLESS_ATTRIBUTES,
            frozenset({"damage_type", "termination", "trait", "stance"}),
        )


class TestDurationInfinite(unittest.TestCase):
    def test_infinite_becomes_real_infinity(self):
        """「无限」要变成真正的无穷大，不能只是"没数值"。"""
        result = clause("持续时间无限")
        self.assertIs(result.kind, Recognition.RECOGNIZED)
        self.assertEqual(result.triple.attribute, "duration")
        self.assertEqual(result.triple.value, math.inf)

    def test_finite_duration(self):
        result = clause("持续10秒")
        self.assertEqual(result.triple.attribute, "duration")
        self.assertEqual(result.triple.value, 10.0)


class TestUnknownRiskGrading(unittest.TestCase):
    def test_attack_double_without_number_is_risky(self):
        """「攻击力翻倍」找得到属性词但没有数值——是未知，且要标成有风险。"""
        result = clause("攻击力翻倍")
        self.assertIs(result.kind, Recognition.UNKNOWN)
        self.assertIs(result.affects_damage, True)

    def test_plain_noise_is_not_risky(self):
        result = clause("并获得以下效果")
        self.assertIs(result.kind, Recognition.UNKNOWN)
        self.assertIs(result.affects_damage, False)

    def test_empty_clause(self):
        result = clause("")
        self.assertIs(result.kind, Recognition.UNKNOWN)
        self.assertIs(result.affects_damage, False)

    def test_unknown_never_claims_false_when_damage_hint_and_non_damage_coexist(self):
        """伤害词与无关词同时出现时返回 None（不确定），不猜。"""
        result = clause("攻击范围扩大并提升攻击力")
        self.assertIn(result.affects_damage, (True, None))


class TestAttributeOwner(unittest.TestCase):
    """属性属于谁——干员自己还是敌人。

    这是**最容易造成静默错误**的一处：把"减敌人的攻"当成"减自己的攻"，
    算出来的 DPS 会直接砍半，而且不会有任何报错。
    """

    def test_enemy_possessive(self):
        """真实原文：巫恋的召唤物削弱的是**敌人**的攻击力。"""
        result = clause("诅咒娃娃周围敌人的攻击力和防御力-50%")
        self.assertEqual(result.owner, "enemy")

    def test_enemy_adjacent(self):
        result = clause("在23秒内使围绕区域内的敌人攻击力-15%")
        self.assertEqual(result.owner, "enemy")

    def test_target_adjacent(self):
        result = clause("5秒内使击中目标攻击力-40%")
        self.assertEqual(result.owner, "enemy")

    def test_targeting_phrase_is_not_enemy_owned(self):
        """回归的反面：这两句里的「敌人」是**打向**的目标，属性仍是自己的。

        区分办法是位置——归属词必须紧挨着属性词之前。
        只看"句子里有没有敌人"会把这两句误伤，导致真实的攻击力加成丢失。

            「仅攻击到一个敌人时对**其**攻击力提升至160%」   ← 史尔特尔
            「攻击被晕眩**目标**时攻击力提高至250%」        ← 灰烬
        """
        for text in (
            "仅攻击到一个敌人时对其攻击力提升至160%",
            "攻击被晕眩目标时攻击力提高至250%",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    clause(text).owner, "self",
                    f"{text!r} 说的是打某个敌人时自己的攻击力，不该判成敌人的",
                )

    def test_damage_dealt_to_enemies_is_self_owned(self):
        """绝大多数「对敌人造成相当于攻击力X%」都是在说自己的能力倍率。"""
        for text in (
            "对周围所有敌人造成相当于攻击力300%的物理伤害",
            "对前方6名敌人造成攻击力380%的物理伤害",
            "对目标造成相当于攻击力400%的物理伤害",
        ):
            with self.subTest(text=text):
                self.assertEqual(clause(text).owner, "self")

    def test_penetration_is_self_owned(self):
        """「无视20点法术抗性」虽然提到了抗性，但属性归自己（是自己在无视）。"""
        self.assertEqual(clause("无视20点法术抗性").owner, "self")

    def test_plain_buff_is_self_owned(self):
        self.assertEqual(clause("攻击力+200%").owner, "self")
        self.assertEqual(clause("自身的攻击力+50%").owner, "self")


class TestSummarize(unittest.TestCase):
    def test_empty(self):
        summary = summarize([])
        self.assertEqual(summary.total, 0)
        self.assertEqual(summary.coverage, 1.0)
        self.assertEqual(summary.confidence, "exact")

    def test_all_recognized_is_exact(self):
        summary = summarize(analyze("攻击力+200%，攻击速度+12"))
        self.assertEqual(summary.recognized, 2)
        self.assertEqual(summary.confidence, "exact")
        self.assertEqual(summary.coverage, 1.0)

    def test_unknown_without_risk_is_partial(self):
        summary = summarize(analyze("并获得以下效果"))
        self.assertEqual(summary.unknown, 1)
        self.assertEqual(summary.unknown_risky, 0)
        self.assertEqual(summary.confidence, "partial")

    def test_unknown_with_risk_is_low(self):
        summary = summarize(analyze("攻击力翻倍"))
        self.assertEqual(summary.unknown_risky, 1)
        self.assertEqual(summary.confidence, "low")

    def test_irrelevant_counts_as_covered(self):
        """判定为无关也是识别成功，不该拉低覆盖率。"""
        summary = summarize(analyze("攻击范围扩大"))
        self.assertEqual(summary.irrelevant, 1)
        self.assertEqual(summary.coverage, 1.0)
        self.assertEqual(summary.confidence, "exact")


class TestAnalyzeWholeDescription(unittest.TestCase):
    def test_realistic_skill_description(self):
        text = (
            "攻击力+100%，攻击速度+40，"
            "攻击范围扩大，同时攻击至多3个目标"
        )
        clauses = analyze(text)
        self.assertEqual(len(clauses), 4)

        by_attr = {}
        for item in clauses:
            if item.triple is not None:
                by_attr[item.triple.attribute] = item.triple.value

        self.assertEqual(by_attr["atk"], 100.0)
        self.assertEqual(by_attr["aspd"], 40.0)
        self.assertEqual(by_attr["targets"], 3.0)
        self.assertIs(clauses[2].kind, Recognition.IRRELEVANT)

        summary = summarize(clauses)
        self.assertEqual(summary.coverage, 1.0)

    def test_conditional_effect_is_separated(self):
        """「但此时」的降攻不能无条件套用，否则 DPS 恒定偏低 20%。"""
        clauses = analyze("可以进行远程攻击，但此时攻击力降低至80%")
        conditional = [c for c in clauses if c.is_conditional]
        self.assertEqual(len(conditional), 1)
        self.assertEqual(conditional[0].triple.attribute, "atk")


if __name__ == "__main__":
    unittest.main()
