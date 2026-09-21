"""PRTS 导入器测试：wikitext → 数据草稿。

**完全离线**。做法是塞一个假客户端进去，把合成 wikitext 当成页面返回:

    PrtsImporter(_FakeClient({"测试干员": sample_wikitext()}))

联网测试会因 PRTS 改版而莫名其妙地失败，而这里要验的是"字段取对了没有"，
和网站今天长什么样没关系。合成页面与真实页面的结构由 ``fixtures`` 保证一致。

盯住的都是**算错也不会报错**的地方：稀有度少一星、天赋丢一个、
描述里的倍率被漏掉——这些错误只会让 DPS 悄无声息地偏掉。
"""

from __future__ import annotations

import unittest

from fixtures import conditional_wikitext, multi_talent_wikitext, sample_wikitext

from arkdps.importers.prts import (
    DRAFT_SCHEMA,
    OPERATOR_CLASSES,
    PrtsImporter,
    _potential_atk,
)

# --------------------------------------------------------------------------
# 离线客户端
# --------------------------------------------------------------------------

class _FakeClient:
    """把预先准备好的 wikitext 当成页面返回，绝不联网。

    顺带记录被请求过的标题，这样"批量导入到底问了哪些页面"也能测。
    """

    def __init__(self, pages: dict[str, str]) -> None:
        self._pages = dict(pages)
        self.requested: list[str] = []

    def page_wikitext(self, title: str) -> str | None:
        self.requested.append(title)
        return self._pages.get(title)


def _importer(pages: dict[str, str]) -> PrtsImporter:
    return PrtsImporter(_FakeClient(pages))


def _draft(**kwargs) -> dict:
    """导入一份合成页面，返回草稿。"""
    page = sample_wikitext(**kwargs)
    draft = _importer({"测试干员": page}).import_operator("测试干员")
    assert draft is not None
    return draft


#: 一个结构完整的技能模板。字段名与 PRTS 上的一致。
SKILL_BLOCK = """{{技能
|技能名=测试斩
|技能类型1=自动回复
|技能类型2=手动触发
|技能专精3消耗=30
|技能专精3初始=10
|技能专精3持续=15
|技能专精3描述=攻击力+100%，攻击速度+40
}}"""


class TestBasicFields(unittest.TestCase):
    def test_info_fields(self):
        draft = _draft(name="测试干员")
        self.assertEqual(draft["name"], "测试干员")
        self.assertEqual(draft["class"], "近卫")
        self.assertEqual(draft["branch"], "领主")

    def test_stats_fields(self):
        draft = _draft(attack=700, interval="1.3s")
        self.assertEqual(draft["atk"], 700.0)
        self.assertEqual(draft["attack_interval"], 1.3)
        self.assertEqual(draft["atk_trust"], 50.0)

    def test_schema_stamp(self):
        """草稿带 schema 标记，方便以后迁移。"""
        self.assertEqual(_draft()["_schema"], DRAFT_SCHEMA)
        self.assertEqual(DRAFT_SCHEMA, "arkdps/operator/1")

    def test_source_is_recorded(self):
        draft = _draft()
        self.assertEqual(draft["_source"]["wiki"], "PRTS")
        self.assertEqual(draft["_source"]["page"], "测试干员")

    def test_operator_classes_are_the_eight(self):
        self.assertEqual(len(OPERATOR_CLASSES), 8)
        self.assertIn("近卫", OPERATOR_CLASSES)
        self.assertIn("狙击", OPERATOR_CLASSES)


class TestRarity(unittest.TestCase):
    """回归：PRTS 的「稀有度」是 **0 起算** 的。"""

    def test_zero_based_is_shifted(self):
        """稀有度 5 在 PRTS 上表示 6 星。直接抄下来会让所有干员少一星。"""
        self.assertEqual(_draft(rarity=5)["rarity"], 6)

    def test_lowest_rarity(self):
        self.assertEqual(_draft(rarity=0)["rarity"], 1)

    def test_mid_rarity(self):
        self.assertEqual(_draft(rarity=3)["rarity"], 4)


class TestTalentParsing(unittest.TestCase):
    def test_two_talent_templates_are_both_kept(self):
        """回归：处理完第一个 ``天赋列表3`` 就 return，会丢掉第二个天赋。

        像能天使这种双天赋干员，丢掉一个天赋的结果是 DPS 悄悄偏低——
        少的是「天使的祝福」那 6% 攻击力。
        """
        draft = _importer({"测试干员": multi_talent_wikitext()}).import_operator("测试干员")
        assert draft is not None

        names = [meta["name"] for meta in draft["_talent_meta"]]
        self.assertEqual(names, ["快速弹匣", "天使的祝福"])

        # 两个天赋的效果都并进了 talent
        self.assertEqual(draft["talent"]["aspd"], 12.0)
        self.assertEqual(draft["talent"]["atk_pct"], 6.0)

    def test_elite2_tier_is_chosen_over_elite1(self):
        """同一模板里有精英1/精英2两档时，要取精英2那一档。

        取错档会让天赋加成偏低（这里是 6 而不是 12）。
        """
        draft = _importer({"测试干员": multi_talent_wikitext()}).import_operator("测试干员")
        assert draft is not None
        quick_reload = draft["_talent_meta"][0]
        self.assertEqual(quick_reload["condition"], "精英2")
        self.assertNotIn("fallback", quick_reload)

    def test_fallback_when_elite2_is_absent(self):
        """没有精英2 那一档时退回最后一档，并**明确标注**是退而求其次。"""
        page = sample_wikitext(talent_blocks="""{{天赋列表3
|天赋1=独苗
|天赋1条件=精英1
|天赋1效果=攻击力{{*|10%|+10%}}
}}""")
        draft = _importer({"测试干员": page}).import_operator("测试干员")
        assert draft is not None

        meta = draft["_talent_meta"][0]
        self.assertEqual(meta["condition"], "精英1")
        self.assertIs(meta.get("fallback"), True)
        self.assertEqual(draft["talent"]["atk_pct"], 10.0)

    def test_talent_text_is_flattened(self):
        """``{{*|12|+12}}`` 这个 wiki 写法要取到**显示值** +12，不是 12。"""
        draft = _importer({"测试干员": multi_talent_wikitext()}).import_operator("测试干员")
        assert draft is not None
        self.assertEqual(draft["_talent_meta"][0]["text"], "攻击速度+12")

    def test_page_without_talents_has_no_talent_key(self):
        draft = _draft(talent_blocks="")
        self.assertNotIn("talent", draft)

    def test_talent_conditional_is_isolated(self):
        """天赋里的条件效果同样不进默认计算，但必须留下痕迹。"""
        page = sample_wikitext(talent_blocks="""{{天赋列表3
|天赋1=条件天赋
|天赋1条件=精英2
|天赋1效果=攻击精英与领袖敌人时攻击力提升至115%
}}""")
        draft = _importer({"测试干员": page}).import_operator("测试干员")
        assert draft is not None

        self.assertNotIn("atk_mult", draft["talent"])
        self.assertAlmostEqual(draft["_talent_conditional"]["atk_mult"], 1.15)


class TestTraitParsing(unittest.TestCase):
    def test_plain_trait_text(self):
        draft = _draft(trait="攻击造成法术伤害")
        self.assertIn("trait_text", draft)

    def test_conditional_trait_is_isolated(self):
        """回归：「可以进行远程攻击，但此时攻击力降低至80%」

        这个 80% 只在远程攻击时生效。当成常驻会算出恒定偏低 20% 的 DPS，
        而且从结果上看不出哪里错了。
        """
        draft = _importer({"测试干员": conditional_wikitext()}).import_operator("测试干员")
        assert draft is not None

        self.assertNotIn(
            "atk_mult", draft["trait"],
            "有条件的效果不能进默认特性字段",
        )
        self.assertAlmostEqual(draft["_trait_conditional"]["atk_mult"], 0.8)
        self.assertEqual(len(draft["_trait_conditions"]), 1)
        self.assertEqual(draft["_trait_conditions"][0]["condition"], "但此时")


class TestSkillParsing(unittest.TestCase):
    def setUp(self):
        self.draft = _draft(skills=SKILL_BLOCK)
        self.skill = self.draft["skills"][0]

    def test_skill_name_and_trigger(self):
        self.assertEqual(self.skill["name"], "测试斩")
        self.assertEqual(self.skill["trigger"], "手动触发")

    def test_charge_word_maps_to_enum_value(self):
        self.assertEqual(self.skill["charge"], "auto")

    def test_charge_words_all_map(self):
        expected = {
            "自动回复": "auto",
            "攻击回复": "attack",
            "受击回复": "hit",
            "被动": "passive",
        }
        for word, want in expected.items():
            with self.subTest(word=word):
                block = SKILL_BLOCK.replace("自动回复", word)
                draft = _draft(skills=block)
                self.assertEqual(draft["skills"][0]["charge"], want)

    def test_numeric_fields(self):
        self.assertEqual(self.skill["sp_cost"], 30.0)
        self.assertEqual(self.skill["init_sp"], 10.0)
        self.assertEqual(self.skill["duration"], 15.0)

    def test_effects_are_parsed_from_description(self):
        self.assertEqual(
            self.skill["effects"], {"atk_pct": 100.0, "aspd": 40.0}
        )

    def test_parsed_records_are_kept_for_audit(self):
        """每条效果都要留下"哪句话变成了哪个字段"的痕迹。"""
        fields = {r["field"] for r in self.skill["_parsed"]}
        self.assertEqual(fields, {"atk_pct", "aspd"})

    def test_confidence_is_exact(self):
        self.assertEqual(self.skill["_confidence"], "exact")

    def test_skill_without_name_is_dropped(self):
        block = SKILL_BLOCK.replace("|技能名=测试斩", "")
        self.assertEqual(_draft(skills=block)["skills"], [])

    def test_description_duration_fills_missing_template_field(self):
        """模板里没写持续时间，但描述里写了「持续时间无限」。"""
        block = SKILL_BLOCK.replace("|技能专精3持续=15", "")
        block = block.replace(
            "|技能专精3描述=攻击力+100%，攻击速度+40",
            "|技能专精3描述=攻击力+100%，持续时间无限",
        )
        skill = _draft(skills=block)["skills"][0]
        self.assertIs(skill.get("infinite_duration"), True)

    def test_stance_skill_is_flagged(self):
        block = SKILL_BLOCK.replace(
            "|技能专精3描述=攻击力+100%，攻击速度+40",
            "|技能专精3描述=在下列状态和初始状态间切换",
        )
        skill = _draft(skills=block)["skills"][0]
        self.assertIs(skill.get("stance"), True)

    def test_stop_attack_is_flagged(self):
        block = SKILL_BLOCK.replace(
            "|技能专精3描述=攻击力+100%，攻击速度+40",
            "|技能专精3描述=停止攻击",
        )
        skill = _draft(skills=block)["skills"][0]
        self.assertIs(skill.get("attacks"), False)

    def test_two_skill_templates(self):
        block = SKILL_BLOCK + SKILL_BLOCK.replace("测试斩", "第二个技能")
        skills = _draft(skills=block)["skills"]
        self.assertEqual([s["name"] for s in skills], ["测试斩", "第二个技能"])

    def test_unparsed_clauses_are_reported(self):
        block = SKILL_BLOCK.replace(
            "|技能专精3描述=攻击力+100%，攻击速度+40",
            "|技能专精3描述=攻击力翻倍",
        )
        skill = _draft(skills=block)["skills"][0]
        self.assertEqual(skill["_confidence"], "low")
        self.assertIs(skill["_needs_review"], True)
        self.assertIn("翻倍", skill["_unparsed"][0]["text"])


class TestElitePhaseFallback(unittest.TestCase):
    """回归：面板攻击力必须**按精英阶段逐档回退**。

    PRTS 的 ``{{属性}}`` 模板把面板值按精英阶段分开存::

        精英0_1级_攻击 / 精英0_满级_攻击 / 精英1_满级_攻击 / 精英2_满级_攻击

    1~3 星干员没有精英 2（1 星连精英 1 都没有），那些字段**根本不存在**。
    只读精英 2 会让全部 40 个低星干员的面板攻击力为空——
    他们的 DPS 直接算成 0。
    """

    def test_elite2_is_preferred(self):
        draft = _draft(attack=713)
        self.assertEqual(draft["atk"], 713.0)
        self.assertNotIn(
            "_atk_from", draft,
            "取到精英2 时不该记来源——精英2 是常态",
        )

    def test_falls_back_to_elite1(self):
        draft = _draft(attack=325, attack_key="精英1_满级_攻击")
        self.assertEqual(draft["atk"], 325.0)
        self.assertEqual(draft["_atk_from"], "精英1_满级_攻击")

    def test_falls_back_to_elite0(self):
        draft = _draft(attack=353, attack_key="精英0_满级_攻击")
        self.assertEqual(draft["atk"], 353.0)
        self.assertEqual(draft["_atk_from"], "精英0_满级_攻击")

    def test_all_three_phases_present_prefers_the_highest(self):
        page = sample_wikitext(attack=700).replace(
            "|精英2_满级_攻击=700",
            "|精英0_满级_攻击=444\n|精英1_满级_攻击=577\n|精英2_满级_攻击=713",
        )
        draft = _importer({"测试干员": page}).import_operator("测试干员")
        assert draft is not None
        self.assertEqual(draft["atk"], 713.0)

    def test_no_warning_when_a_lower_phase_was_found(self):
        """回退成功就不该报警——但要留下"从哪一档取的"的痕迹。"""
        draft = _draft(attack=325, attack_key="精英1_满级_攻击")
        self.assertEqual(draft["_warnings"], [])

    def test_warning_mentions_all_phases(self):
        page = """{{CharinfoV2
|干员名=缺数据
|稀有度=3
|职业=近卫
}}
{{属性
|攻击速度=1.0s
}}"""
        draft = _importer({"缺数据": page}).import_operator("缺数据")
        assert draft is not None
        self.assertIsNone(draft["atk"])
        joined = " ".join(draft["_warnings"])
        for phase in ("精英2", "精英1", "精英0"):
            self.assertIn(phase, joined)

    def test_pick_elite_stat_helper(self):
        from arkdps.importers.prts import _pick_elite_stat

        self.assertEqual(
            _pick_elite_stat({"精英2_满级_攻击": "713"}, "攻击"),
            (713.0, "精英2_满级_攻击"),
        )
        self.assertEqual(
            _pick_elite_stat({"精英1_满级_攻击": "577"}, "攻击"),
            (577.0, "精英1_满级_攻击"),
        )
        self.assertEqual(_pick_elite_stat({}, "攻击"), (None, ""))


class TestPotentialAtk(unittest.TestCase):
    def test_attack_potential_is_extracted(self):
        self.assertEqual(_potential_atk({"潜能5": "攻击力+25"}), 25.0)

    def test_attack_speed_is_not_attack(self):
        """回归：「攻击速度+7」里有「攻击」二字，但不是攻击力。"""
        self.assertEqual(_potential_atk({"潜能2": "攻击速度+7"}), 0.0)

    def test_no_attack_at_all(self):
        self.assertEqual(_potential_atk({"潜能3": "部署费用-1"}), 0.0)

    def test_empty(self):
        self.assertEqual(_potential_atk({}), 0.0)

    def test_full_width_plus(self):
        self.assertEqual(_potential_atk({"潜能5": "攻击力＋30"}), 30.0)


class TestWarnings(unittest.TestCase):
    def test_missing_stats_are_warned_about(self):
        """页面结构不同导致取不到数值时，必须**说出来**，不能给个空值了事。"""
        page = """{{CharinfoV2
|干员名=缺数据
|稀有度=3
|职业=近卫
}}
{{属性
|精英2_满级_生命上限=1000
}}"""
        draft = _importer({"缺数据": page}).import_operator("缺数据")
        assert draft is not None

        self.assertIsNone(draft["atk"])
        self.assertIsNone(draft["attack_interval"])
        joined = " ".join(draft["_warnings"])
        # 三个精英阶段都试过了才放弃 —— 提示里要说清楚
        self.assertIn("面板攻击力", joined)
        self.assertIn("攻击速度", joined)

    def test_no_warnings_for_a_complete_page(self):
        self.assertEqual(_draft()["_warnings"], [])


class TestMissingPages(unittest.TestCase):
    def test_unknown_title_returns_none(self):
        self.assertIsNone(_importer({}).import_operator("不存在"))

    def test_empty_wikitext_returns_none(self):
        self.assertIsNone(_importer({"空页": ""}).import_operator("空页"))

    def test_import_many_skips_missing_pages(self):
        pages = {"甲": sample_wikitext(name="甲"), "乙": sample_wikitext(name="乙")}
        importer = _importer(pages)
        drafts = list(importer.import_many(["甲", "不存在", "乙"]))
        self.assertEqual([d["name"] for d in drafts], ["甲", "乙"])
        self.assertEqual(importer.client.requested, ["甲", "不存在", "乙"])


class TestOfflineGuarantee(unittest.TestCase):
    def test_no_network_is_touched(self):
        """这些用例一旦联网就会变慢、变脆。断言客户端是假的。"""
        importer = _importer({"测试干员": sample_wikitext()})
        self.assertIsInstance(importer.client, _FakeClient)
        importer.import_operator("测试干员")
        self.assertEqual(importer.client.requested, ["测试干员"])


if __name__ == "__main__":
    unittest.main()
