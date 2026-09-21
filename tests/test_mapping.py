"""映射层测试：语义三元组 → 引擎字段。

这一层是"最后一跳"，也是**最容易悄悄算错**的一层：
识别对了但映射错了，结果就是数字看上去合理、实际差了 5 倍。

两条被测试重点保护的设计决定：

  * **符号由关系决定，不由字面决定**
    「攻击间隔缩短(-0.22)」和「攻击间隔缩短0.22」必须归到同一个结果。
  * **认出来但映射不了，也要报出来**
    认出 ``termination`` / ``trait`` 却不落字段，会被误报成"没解析出来"。
"""

from __future__ import annotations

import unittest

from fixtures import operator  # noqa: F401  （导入以设定 sys.path）
from arkdps.mapping import (
    SKILL_LEVEL_FIELDS,
    MappingResult,
    map_clause,
    map_clauses,
    map_clauses_detailed,
    merge_fields,
)
from arkdps.model import DamageType
from arkdps.recognizer import Recognition, analyze, analyze_clause


def fields_of(text: str) -> dict:
    """把一句话映射成引擎字段——绝大多数用例只需要这个。"""
    return map_clause(analyze_clause(text)).fields


class TestAttackMapping(unittest.TestCase):
    def test_increase_is_additive_percentage(self):
        self.assertEqual(fields_of("攻击力+200%"), {"atk_pct": 200.0})

    def test_decrease_subtracts(self):
        self.assertEqual(fields_of("攻击力-30%"), {"atk_pct": -30.0})

    def test_set_to_is_a_multiplier(self):
        """「提高至110%」是乘算倍率，不是加算 110%。"""
        self.assertEqual(fields_of("攻击力提高至110%"), {"atk_mult": 1.1})

    def test_proportional_is_a_multiplier(self):
        """「相当于攻击力145%」——回归：曾因「当」被误判为有条件而整条隔离。"""
        self.assertEqual(fields_of("相当于攻击力145%"), {"atk_mult": 1.45})

    def test_multiplier_accumulates(self):
        """两次「提高至」应当连乘，而不是取其中一个。"""
        result = map_clauses_detailed(analyze("攻击力提高至110%，攻击力提高至120%"))
        self.assertAlmostEqual(result.fields["atk_mult"], 1.1 * 1.2)

    def test_percent_additive_and_multiplier_are_separate(self):
        """百分比加算与倍率乘算是两件事，必须同时保留。

        正确结果是 ``(1 + 100%) × 110% = 2.2 倍``。
        """
        result = map_clauses_detailed(analyze("攻击力+100%，攻击力提高至110%"))
        self.assertEqual(result.fields["atk_pct"], 100.0)
        self.assertAlmostEqual(result.fields["atk_mult"], 1.1)

    def test_stop_attack_sets_attacks_false(self):
        self.assertEqual(fields_of("停止攻击"), {"attacks": False})

    def test_per_hit_multiplier(self):
        self.assertEqual(
            fields_of("每次攻击造成相当于攻击力200%的伤害"),
            {"atk_mult": 2.0},
        )


class TestAttackSpeedMapping(unittest.TestCase):
    def test_increase(self):
        self.assertEqual(fields_of("攻击速度+40"), {"aspd": 40.0})

    def test_decrease_flips_sign(self):
        """「攻速降低」要变成负的 aspd，否则会变成加速。"""
        self.assertEqual(fields_of("攻击速度降低20"), {"aspd": -20.0})

    def test_decrease_with_negative_literal_is_not_double_negated(self):
        """回归：「攻击速度-20」的负号已经在数值里，再取一次负会变成加速。"""
        self.assertEqual(fields_of("攻击速度-20"), {"aspd": -20.0})


class TestIntervalMapping(unittest.TestCase):
    """攻击间隔是重灾区——三种写法对应三个不同的字段。"""

    def test_flat_decrease_with_negative_literal(self):
        """回归：字面已经带负号，**不能**再取负一次变成加快。"""
        self.assertEqual(
            fields_of("攻击间隔缩短(-0.22)"), {"interval_flat": -0.22}
        )

    def test_flat_decrease_without_sign(self):
        """回归：字面没带负号，也必须归到"减少"。"""
        self.assertEqual(
            fields_of("攻击间隔缩短0.22"), {"interval_flat": -0.22}
        )

    def test_flat_increase(self):
        self.assertEqual(fields_of("攻击间隔增加0.3"), {"interval_flat": 0.3})

    def test_set_to_is_a_multiplier(self):
        """「缩短至70%」是倍率。"""
        self.assertEqual(fields_of("攻击间隔缩短至70%"), {"interval_mult": 0.7})

    def test_star_notation_is_a_multiplier_not_seconds(self):
        """回归：「攻击间隔大幅度缩短(*0.2)」里的星号表示倍率。

        曾经当成秒数算，得到"间隔 −0.2 秒"。数值看起来还挺合理，
        但正确答案是"间隔 × 0.2"——两者差了 **5 倍**，
        而且从结果上完全看不出哪里错了。
        """
        fields = fields_of("攻击间隔大幅度缩短(*0.2)")
        self.assertAlmostEqual(fields["interval_mult"], 0.2)
        self.assertNotIn(
            "interval_flat", fields,
            "带星号的写法必须落到 interval_mult，不能落到 interval_flat",
        )

    def test_star_notation_tolerates_spaces(self):
        for text in ("攻击间隔缩短(*0.3)", "攻击间隔缩短( * 0.3 )"):
            with self.subTest(text=text):
                self.assertAlmostEqual(
                    fields_of(text)["interval_mult"], 0.3
                )

    def test_plain_seconds_notation_still_flat(self):
        """没有星号时，数值就是秒数——不能把两种写法搞混。"""
        fields = fields_of("攻击间隔缩短(-0.22)")
        self.assertNotIn("interval_mult", fields)


class TestHitsAndTargets(unittest.TestCase):
    def test_hits_becomes_override(self):
        self.assertEqual(fields_of("攻击变为5连射"), {"hits_override": 5.0})

    def test_chinese_numeral_hits(self):
        self.assertEqual(fields_of("攻击变为二连击"), {"hits_override": 2.0})

    def test_numeric_targets(self):
        self.assertEqual(fields_of("同时攻击至多6个目标"), {"targets": 6})

    def test_targets_never_below_one(self):
        self.assertEqual(fields_of("同时攻击至多0个目标"), {"targets": 1})

    def test_non_numeric_targets_become_a_scope(self):
        """「阻挡的所有敌人」打几个取决于战场，引擎无从得知——记录范围。"""
        result = map_clause(analyze_clause("同时攻击阻挡的所有敌人"))
        self.assertEqual(result.fields["targets_scope"], "all_blocked")
        self.assertNotIn("targets", result.fields)

    def test_in_range_scope(self):
        result = map_clause(analyze_clause("攻击范围内的所有敌人"))
        self.assertIn(
            result.fields.get("targets_scope"), ("all_in_range", "all_blocked")
        )


class TestDamageTypeMapping(unittest.TestCase):
    def test_damage_type_change(self):
        self.assertEqual(
            fields_of("伤害类型变为法术"), {"damage_type": DamageType.ARTS}
        )
        self.assertEqual(
            fields_of("伤害类型变为真实"), {"damage_type": DamageType.TRUE}
        )

    def test_physical(self):
        self.assertEqual(
            fields_of("造成物理伤害"), {"damage_type": DamageType.PHYSICAL}
        )

    def test_mentioning_arts_without_changing_type_is_not_mapped(self):
        """「法术」二字单独出现不算改伤害类型——必须真的在说类型。

        否则「可以攻击法术抗性」之类会被静默改成法术伤害。
        """
        result = map_clause(analyze_clause("法术抗性降低"))
        self.assertNotIn("damage_type", result.fields)


class TestPenetrationMapping(unittest.TestCase):
    def test_res_ignore_flat(self):
        self.assertEqual(fields_of("无视20点法术抗性"), {"res_ignore": 20.0})

    def test_res_ignore_percent(self):
        self.assertEqual(fields_of("无视20%法术抗性"), {"res_ignore_pct": 20.0})

    def test_defense_ignore_flat(self):
        self.assertEqual(fields_of("无视50点防御力"), {"def_ignore": 50.0})

    def test_defense_ignore_percent(self):
        self.assertEqual(fields_of("无视50%防御力"), {"def_ignore_pct": 50.0})

    def test_penetration_accumulates(self):
        result = map_clauses_detailed(analyze("无视20点法术抗性，无视10点法术抗性"))
        self.assertEqual(result.fields["res_ignore"], 30.0)

    def test_own_defense_buff_is_not_mapped_as_penetration(self):
        """自己的防御力加成绝不能落进 def_ignore——那会让伤害虚高。"""
        result = map_clause(analyze_clause("防御力+100%"))
        self.assertNotIn("def_ignore", result.fields)
        self.assertNotIn("def_ignore_pct", result.fields)


class TestSkillLevelFields(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(fields_of("持续10秒"), {"duration": 10.0})

    def test_infinite_duration(self):
        self.assertEqual(fields_of("持续时间无限"), {"infinite_duration": True})

    def test_stance(self):
        self.assertEqual(
            fields_of("在下列状态和初始状态间切换"), {"stance": True}
        )

    def test_duration_is_a_skill_level_field(self):
        self.assertIn("duration", SKILL_LEVEL_FIELDS)
        self.assertIn("infinite_duration", SKILL_LEVEL_FIELDS)
        self.assertIn("stance", SKILL_LEVEL_FIELDS)

    def test_termination_lands_on_a_field(self):
        """回归：termination 认出来了却不落字段，会被误报成"没解析出来"。

        「打完后技能结束」影响循环时长，必须真的变成一个字段。
        """
        result = map_clause(analyze_clause("打完后技能结束"))
        self.assertEqual(result.fields["end_condition"], "打完后技能结束")
        self.assertEqual(result.unresolved, [], "不该同时报成未解析")
        self.assertEqual(len(result.records), 1)

    def test_trait_override_lands_on_a_field(self):
        """回归：trait 同理。"""
        result = map_clause(analyze_clause("远程攻击不再降低攻击力"))
        self.assertIs(result.fields["overrides_trait"], True)
        self.assertEqual(result.unresolved, [])

    def test_end_condition_and_overrides_trait_are_skill_level(self):
        self.assertIn("end_condition", SKILL_LEVEL_FIELDS)
        self.assertIn("overrides_trait", SKILL_LEVEL_FIELDS)


class TestUnresolvedReporting(unittest.TestCase):
    """认出来但映射不了，既不算失败也不算已应用——同样需要人看一眼。"""

    def test_unknown_clause_is_reported(self):
        result = map_clause(analyze_clause("攻击力翻倍"))
        self.assertEqual(len(result.unresolved), 1)
        self.assertEqual(result.unresolved[0]["kind"], "unknown")
        self.assertIs(result.unresolved[0]["affects_damage"], True)

    def test_recognized_but_unmappable_is_reported(self):
        """识别成功却没有对应字段的，要报成 unmapped 而不是静默丢掉。

        用「生命上限+10%」当例子：它确实被识别成了 ``hp``，
        但这个属性不属于引擎的 ``Effects``（引擎只管输出），
        所以映射不到任何字段——这种情况要看得见。
        """
        clause = analyze_clause("生命上限+10%")
        self.assertIs(clause.kind, Recognition.RECOGNIZED)
        result = map_clause(clause)
        self.assertEqual(len(result.unresolved), 1)
        self.assertEqual(result.unresolved[0]["kind"], "unmapped")
        self.assertIs(result.unresolved[0]["affects_damage"], False)

    def test_healing_clauses_are_known_irrelevant(self):
        """「回复自身50生命」这类不进报告——它们已被判定为与 DPS 无关。"""
        for text in ("每攻击到一个敌人回复自身50生命", "回复所有技力", "治疗友军"):
            with self.subTest(text=text):
                result = map_clause(analyze_clause(text))
                self.assertEqual(result.unresolved, [])
                self.assertEqual(result.records, [])

    def test_irrelevant_clause_is_not_reported(self):
        """判定为无关的子句不该出现在任何报告里——否则噪音又回来了。"""
        result = map_clause(analyze_clause("攻击范围扩大"))
        self.assertEqual(result.unresolved, [])
        self.assertEqual(result.records, [])

    def test_has_unresolved_risk(self):
        risky = MappingResult(unresolved=[{"affects_damage": True}])
        safe = MappingResult(unresolved=[{"affects_damage": False}])
        unsure = MappingResult(unresolved=[{"affects_damage": None}])
        self.assertTrue(risky.has_unresolved_risk)
        self.assertFalse(safe.has_unresolved_risk)
        self.assertTrue(unsure.has_unresolved_risk, "不确定要按有风险处理")
        self.assertFalse(MappingResult().has_unresolved_risk)


class TestConditionalIsolation(unittest.TestCase):
    """带条件的效果不能无条件套用。"""

    def test_conditional_effect_is_not_applied(self):
        """回归：「可以进行远程攻击，但此时攻击力降低至80%」

        这个 80% 只在远程攻击时生效。当成常驻会算出**恒定偏低 20%** 的
        DPS，而且从结果上完全看不出哪里错了。
        """
        result = map_clause(analyze_clause("但此时攻击力降低至80%"))
        self.assertEqual(result.fields, {}, "有条件的效果不进默认字段")
        self.assertAlmostEqual(result.conditional["atk_mult"], 0.8)
        self.assertEqual(len(result.conditions), 1)
        self.assertEqual(result.conditions[0]["condition"], "但此时")

    def test_unconditional_effect_is_applied(self):
        result = map_clause(analyze_clause("攻击力降低至80%"))
        self.assertAlmostEqual(result.fields["atk_mult"], 0.8)
        self.assertEqual(result.conditional, {})

    def test_mixed_description_keeps_them_apart(self):
        result = map_clauses_detailed(analyze("攻击力+50%，但此时攻击力降低至80%"))
        self.assertEqual(result.fields, {"atk_pct": 50.0})
        self.assertAlmostEqual(result.conditional["atk_mult"], 0.8)
        self.assertEqual(len(result.conditions), 1)


class TestNeutralFieldPruning(unittest.TestCase):
    def test_zero_bonus_is_dropped(self):
        """等于默认值的字段不该写进草稿，否则草稿全是噪音。"""
        result = map_clauses_detailed(analyze("攻击力+0%"))
        self.assertEqual(result.fields, {})

    def test_one_times_multiplier_is_dropped(self):
        result = map_clauses_detailed(analyze("攻击力提高至100%"))
        self.assertEqual(result.fields, {})

    def test_real_change_survives(self):
        result = map_clauses_detailed(analyze("攻击力+1%"))
        self.assertEqual(result.fields, {"atk_pct": 1.0})


class TestMergeFields(unittest.TestCase):
    """合并规则不止用在一处：一段描述内部要合并，**多个天赋之间**也要。"""

    def test_percentages_add(self):
        target = {"atk_pct": 10.0}
        merge_fields(target, {"atk_pct": 5.0, "aspd": 12.0})
        self.assertEqual(target["atk_pct"], 15.0)
        self.assertEqual(target["aspd"], 12.0)

    def test_multipliers_multiply(self):
        target = {"atk_mult": 1.1}
        merge_fields(target, {"atk_mult": 1.2})
        self.assertAlmostEqual(target["atk_mult"], 1.32)

    def test_multiplier_defaults_to_one_not_zero(self):
        """回归：倍率的单位元是 1.0。若按 0.0 起算，伤害会被清零。"""
        target: dict = {}
        merge_fields(target, {"atk_mult": 1.5})
        self.assertAlmostEqual(target["atk_mult"], 1.5)

    def test_targets_takes_the_max(self):
        target = {"targets": 3}
        merge_fields(target, {"targets": 2})
        self.assertEqual(target["targets"], 3)
        merge_fields(target, {"targets": 6})
        self.assertEqual(target["targets"], 6)

    def test_override_fields_replace(self):
        target = {"damage_type": DamageType.PHYSICAL, "hits_override": 2.0}
        merge_fields(target, {"damage_type": DamageType.ARTS, "hits_override": 5.0})
        self.assertEqual(target["damage_type"], DamageType.ARTS)
        self.assertEqual(target["hits_override"], 5.0)

    def test_attacks_false_is_not_lost(self):
        target: dict = {}
        merge_fields(target, {"attacks": False})
        self.assertIs(target["attacks"], False)


class TestEnemyOwnedEffects(unittest.TestCase):
    """作用于**敌人**的效果不能套到干员身上。"""

    def test_enemy_debuff_is_not_applied_to_the_operator(self):
        """回归：巫恋「诅咒娃娃周围**敌人的**攻击力和防御力-50%」

        原文说的是把敌人的攻击力打下去。按"自己攻击力-50%"套用，
        该干员的 DPS 会直接砍半——而且不会有任何报错，
        置信度也照样是 partial，人工复核列表里根本看不到。
        """
        result = map_clause(analyze_clause("诅咒娃娃周围敌人的攻击力和防御力-50%"))
        self.assertEqual(result.fields, {}, "敌人的减攻不能变成自己的减攻")
        self.assertNotIn("atk_pct", result.fields)

        self.assertEqual(len(result.unresolved), 1)
        self.assertEqual(result.unresolved[0]["kind"], "enemy_effect")
        self.assertIs(result.unresolved[0]["affects_damage"], True)

    def test_other_real_cases(self):
        for text in (
            "在23秒内使围绕区域内的敌人攻击力-15%",
            "5秒内使击中目标攻击力-40%",
        ):
            with self.subTest(text=text):
                result = map_clause(analyze_clause(text))
                self.assertEqual(result.fields, {})
                self.assertEqual(result.unresolved[0]["kind"], "enemy_effect")

    def test_self_buff_still_applies(self):
        """反面：打向敌人的攻击力倍率仍然要正常生效。"""
        result = map_clause(analyze_clause("仅攻击到一个敌人时对其攻击力提升至160%"))
        self.assertAlmostEqual(result.fields["atk_mult"], 1.6)
        self.assertEqual(result.unresolved, [])

    def test_damage_to_enemies_still_applies(self):
        result = map_clause(analyze_clause("对周围所有敌人造成相当于攻击力300%的物理伤害"))
        self.assertAlmostEqual(result.fields["atk_mult"], 3.0)
        self.assertEqual(result.unresolved, [])


class TestMappingResultViews(unittest.TestCase):
    def test_effects_and_skill_fields_are_split(self):
        result = MappingResult(fields={"atk_pct": 50.0, "duration": 10.0})
        self.assertEqual(result.effects_fields, {"atk_pct": 50.0})
        self.assertEqual(result.skill_fields, {"duration": 10.0})

    def test_records_are_an_audit_trail(self):
        """每条识别成功的效果都要留下"哪句话变成了哪个字段"的痕迹。"""
        result = map_clause(analyze_clause("攻击力+200%"))
        self.assertEqual(len(result.records), 1)
        record = result.records[0]
        self.assertEqual(record["attribute"], "atk")
        self.assertEqual(record["relation"], "increase")
        self.assertEqual(record["value"], 200.0)
        self.assertEqual(record["field"], "atk_pct")


class TestMapClausesShorthand(unittest.TestCase):
    def test_returns_triple(self):
        fields, records, unresolved = map_clauses(analyze("攻击力+50%"))
        self.assertEqual(fields, {"atk_pct": 50.0})
        self.assertEqual(len(records), 1)
        self.assertEqual(unresolved, [])

    def test_empty_input(self):
        fields, records, unresolved = map_clauses([])
        self.assertEqual(fields, {})
        self.assertEqual(records, [])
        self.assertEqual(unresolved, [])


if __name__ == "__main__":
    unittest.main()
