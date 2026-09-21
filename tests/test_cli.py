"""命令行测试。

CLI 是用户唯一直接接触的界面，所以这里测的不只是"跑不跑得起来"，
还有三件容易坏又不容易发现的事：

  * **退出码**——CI 和脚本靠它判断成败，返回错了没人会注意
  * **中文排版**——中日韩字符占两格，按 ``len()`` 排会错位
  * **JSON 输出**——被别的程序读，字段名是接口

数据用合成草稿写在临时目录里，不碰 ``data/`` 也不联网。
数值都取得很整齐，期望值可以口算。
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from fixtures import temp_dir, write_draft
from arkdps import cli
from arkdps.cli import _disp_width, _pad, _render_table, build_parser, format_effects, main
from arkdps.model import DamageType, Effects


# --------------------------------------------------------------------------
# 合成数据
# --------------------------------------------------------------------------
#
# 面板 1000，天赋 +10%，技能 +100%（加算）→ 技能期攻击力 2100
# 攻击间隔 1.0s，SP 30 自动回复，持续 10s
#   常态 攻击力 1100，间隔 1.0，DPS 1100
#   技能 攻击力 2100，DPH 2100，DPS 2100
#   充能 30s，循环 40s，覆盖率 25%
#   循环 DPS = (2100×10 + 1100×30) ÷ 40 = 1350
DRAFT = {
    "name": "测试甲",
    "atk": 1000.0,
    "attack_interval": 1.0,
    "rarity": 6,
    "class": "近卫",
    "branch": "领主",
    "damage_type": "physical",
    "talent": {"atk_pct": 10.0},
    "skills": [
        {
            "name": "测试技能",
            "charge": "auto",
            "sp_cost": 30.0,
            "duration": 10.0,
            "effects": {"atk_pct": 100.0},
        }
    ],
}

#: 另一份草稿：DPH 更高但循环 DPS 更低，用来验证 compare 的排序依据确实生效。
#:
#:   常态攻击力 400，间隔 4.0s → 常态 DPS 100
#:   技能攻击力 400×15 = 6000，DPH 6000，技能期 DPS 6000÷4 = 1500
#:   充能 100s，持续 5s → 循环 105s，覆盖率 4.8%
#:   循环 DPS = (1500×5 + 100×100) ÷ 105 = 166.7
#:
#: 于是：DPH 6000 > 2100（乙赢），循环 DPS 166.7 < 1350（甲赢）——
#: 两种排序给出**相反**的顺序，排序依据写错了就会露馅。
SLOW_DRAFT = {
    "name": "测试乙",
    "atk": 400.0,
    "attack_interval": 4.0,
    "rarity": 5,
    "class": "狙击",
    "damage_type": "physical",
    "talent": {},
    "skills": [
        {
            "name": "慢技能",
            "charge": "auto",
            "sp_cost": 100.0,
            "duration": 5.0,
            "effects": {"atk_mult": 15.0},
        }
    ],
}

#: 数据有问题的草稿：没有面板攻击力，导入器留下的 _warnings 必须传到 note
BROKEN_DRAFT = {
    "name": "缺数据",
    "atk": None,
    "attack_interval": 1.0,
    "class": "术师",
    "_warnings": ["没有取到「精英2_满级_攻击」，需要手工填写"],
    "skills": [],
}


def _run(argv: list[str]) -> tuple[int, str, str]:
    """跑一次 CLI，返回 ``(退出码, stdout, stderr)``。"""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as exc:  # argparse 的 --help / --version 走这里
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


class _DataDirMixin(unittest.TestCase):
    """把合成草稿放进临时目录，供各用例使用。

    目录结构刻意做成**两层**::

        <临时根>/_coverage.json     ← coverage 子命令读这个
        <临时根>/operators/*.json   ← --data 指向这里

    因为 ``arkdps coverage`` 找的是 ``Path(data).parent/_coverage.json``。
    如果直接把临时目录本身当 ``--data``，报告就会写进**系统临时目录的根**——
    那是所有测试共用的地方，写了不删会污染别的用例
    （`coverage` 的"没有报告"用例会莫名其妙地读到上一个用例留下的报告）。
    """

    def setUp(self):
        self._ctx = temp_dir()
        self.root = self._ctx.__enter__()
        self.addCleanup(lambda: self._ctx.__exit__(None, None, None))

        self.data = self.root / "operators"
        self.data.mkdir()

        write_draft(self.data, "测试甲.json", DRAFT)
        write_draft(self.data, "测试乙.json", SLOW_DRAFT)
        write_draft(self.data, "缺数据.json", BROKEN_DRAFT)

    def argv(self, *rest: str) -> list[str]:
        return [*rest, "--data", str(self.data)]


# --------------------------------------------------------------------------
# 排版：中日韩宽度
# --------------------------------------------------------------------------

class TestDisplayWidth(unittest.TestCase):
    """中日韩字符在终端里占**两格**，``len()`` 只算一个。

    直接用 ljust 排版，中文列会和英文列错开——一个满是中文的表格
    错位到看不清。
    """

    def test_ascii_counts_one(self):
        self.assertEqual(_disp_width("abc"), 3)
        self.assertEqual(_disp_width("DPH"), 3)

    def test_cjk_counts_two(self):
        self.assertEqual(_disp_width("攻击力"), 6)
        self.assertEqual(_disp_width("银灰"), 4)

    def test_mixed(self):
        self.assertEqual(_disp_width("攻击力abc"), 9)
        # 「技能期」3 个汉字 = 6，空格 = 1，「DPS」= 3
        self.assertEqual(_disp_width("技能期 DPS"), 10)

    def test_empty(self):
        self.assertEqual(_disp_width(""), 0)

    def test_pad_uses_display_width_not_length(self):
        """回归：按 ``len()`` 补齐会让中文表格错位。"""
        self.assertEqual(_disp_width(_pad("银灰", 6)), 6)
        self.assertEqual(_disp_width(_pad("abc", 6)), 6)
        self.assertEqual(_pad("银灰", 6), "银灰  ")

    def test_pad_right(self):
        self.assertEqual(_pad("1.0", 6, right=True), "   1.0")

    def test_pad_never_truncates(self):
        self.assertEqual(_pad("很长的中文名字", 4), "很长的中文名字")


class TestRenderTable(unittest.TestCase):
    def test_all_rows_have_equal_display_width(self):
        text = _render_table(
            ["技能", "DPH"],
            [["真银斩", "1645.9"], ["（常态）", "67.9"]],
            right=[1],
        )
        widths = {_disp_width(line) for line in text.splitlines()}
        self.assertEqual(len(widths), 1, f"表格没对齐：{widths}")

    def test_columns_are_right_aligned_when_asked(self):
        text = _render_table(["a"], [["1"], ["100"]], right=[0])
        lines = text.splitlines()
        self.assertTrue(lines[2].endswith("  1"))
        self.assertTrue(lines[3].endswith("100"))

    def test_empty_rows_still_renders_header(self):
        text = _render_table(["技能"], [], right=[])
        self.assertEqual(len(text.splitlines()), 2)


class TestFormatEffects(unittest.TestCase):
    def test_percentage(self):
        self.assertEqual(format_effects(Effects(atk_pct=10.0)), "攻击力+10%")

    def test_negative_percentage(self):
        self.assertEqual(format_effects(Effects(atk_pct=-30.0)), "攻击力-30%")

    def test_multiplier_reads_as_times(self):
        self.assertEqual(format_effects(Effects(atk_mult=1.45)), "攻击力×1.45")

    def test_override_reads_as_assignment(self):
        """回归：覆盖语义不能写成 "+3"。

        ``段数+3`` 读起来像"再增加 3 段"，而实际是"一共 3 段"——
        按前者理解，算出来的伤害会差一倍。
        """
        self.assertEqual(format_effects(Effects(hits_override=3.0)), "段数=3")
        self.assertEqual(format_effects(Effects(targets=6)), "目标数=6")

    def test_seconds(self):
        self.assertEqual(format_effects(Effects(interval_flat=-0.22)), "间隔-0.22s")

    def test_neutral_fields_are_hidden(self):
        self.assertEqual(format_effects(Effects()), "（无）")
        self.assertEqual(
            format_effects(Effects(atk_pct=0.0, atk_mult=1.0, targets=1)),
            "（无）",
        )

    def test_damage_type(self):
        self.assertEqual(
            format_effects(Effects(damage_type=DamageType.ARTS)),
            "伤害类型=法术",
        )

    def test_attacks_disabled(self):
        self.assertEqual(format_effects(Effects(attacks=False)), "不进行普攻")

    def test_several_fields_join_with_comma(self):
        text = format_effects(Effects(atk_pct=10.0, aspd=12.0))
        self.assertEqual(text, "攻击力+10%，攻速+12")

    def test_targets_scope(self):
        text = format_effects(Effects(targets_scope="all_blocked"))
        self.assertIn("阻挡的所有敌人", text)


# --------------------------------------------------------------------------
# 解析器与退出码
# --------------------------------------------------------------------------

class TestParser(unittest.TestCase):
    def test_build_parser_does_not_crash(self):
        self.assertIsNotNone(build_parser())

    def test_help_does_not_crash(self):
        """回归：帮助文本里有中文，在非 UTF-8 输出流上打印会抛 UnicodeEncodeError。

        CI 的 Windows 任务就是这么挂的。
        """
        code, out, _err = _run(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("arkdps", out)

    def test_version(self):
        code, out, _err = _run(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("arkdps", out)

    def test_no_subcommand_prints_help_and_succeeds(self):
        """不带子命令不是错误——打印帮助并返回 0。"""
        code, out, _err = _run([])
        self.assertEqual(code, 0)
        self.assertIn("子命令", out)

    def test_unknown_subcommand_is_a_usage_error(self):
        code, _out, err = _run(["不存在的子命令"])
        self.assertEqual(code, 2)
        self.assertTrue(err)


# --------------------------------------------------------------------------
# 找数据
# --------------------------------------------------------------------------

class TestFindDraft(_DataDirMixin):
    def test_by_filename(self):
        self.assertIsNotNone(cli._find_draft("测试甲", self.data))

    def test_by_name_field_when_filename_differs(self):
        """回归：文件名经过清洗，和干员名不一定对得上。

        只按文件名找会"找不到明明存在的干员"——真实数据里
        ``GALLUS²``、``Raidian(卫戍协议)`` 都是这种。
        """
        path = write_draft(self.data, "odd_filename.json", {"name": "奇怪名字"})
        self.assertTrue(path.is_file())
        self.assertIsNone(cli._find_draft("奇怪名字", self.data / "不存在"))
        found = cli._find_draft("奇怪名字", self.data)
        self.assertEqual(found, path)

    def test_name_match_ignores_case_and_padding(self):
        write_draft(self.data, "x.json", {"name": "Mon3tr"})
        self.assertIsNotNone(cli._find_draft("  mon3tr  ", self.data))

    def test_missing_returns_none(self):
        self.assertIsNone(cli._find_draft("没有这个", self.data))

    def test_accepts_a_string_directory(self):
        """回归：``--data`` 从 argparse 出来是字符串，不是 Path。"""
        self.assertIsNotNone(cli._find_draft("测试甲", str(self.data)))

    def test_resolve_one_raises_with_a_useful_message(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            cli._resolve_one("没有这个", self.data)
        self.assertIn("arkdps list", str(ctx.exception))


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------

class TestListCommand(_DataDirMixin):
    def test_lists_operators(self):
        code, out, _err = _run(self.argv("list"))
        self.assertEqual(code, 0)
        self.assertIn("测试甲", out)
        self.assertIn("测试乙", out)

    def test_header_is_aligned(self):
        _code, out, _err = _run(self.argv("list"))
        table = [line for line in out.splitlines() if line.strip()][:4]
        widths = {_disp_width(line) for line in table}
        self.assertEqual(len(widths), 1, f"表格没对齐：{widths}")

    def test_warns_about_problematic_data(self):
        """数据有问题的干员要带 ⚠ —— 否则用户不知道结果不可信。"""
        _code, out, _err = _run(self.argv("list"))
        marked = [line for line in out.splitlines() if line.startswith("⚠")]
        self.assertTrue(marked, "缺数据的干员没有被标记出来")
        self.assertTrue(any("缺数据" in line for line in marked))

    def test_filter_by_class(self):
        _code, out, _err = _run(self.argv("list", "--class", "狙击"))
        self.assertIn("测试乙", out)
        self.assertNotIn("测试甲", out)

    def test_filter_by_rarity(self):
        _code, out, _err = _run(self.argv("list", "--rarity", "5"))
        self.assertIn("测试乙", out)
        self.assertNotIn("测试甲", out)

    def test_json_is_parseable(self):
        code, out, _err = _run(self.argv("list", "--json"))
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual({item["name"] for item in data}, {"测试甲", "测试乙", "缺数据"})
        self.assertEqual(
            next(i for i in data if i["name"] == "测试甲")["skills"], ["测试技能"]
        )

    def test_empty_directory_explains_what_to_do(self):
        with temp_dir() as empty:
            code, out, _err = _run(["list", "--data", str(empty)])
        self.assertEqual(code, 1)
        self.assertIn("arkdps import", out)


# --------------------------------------------------------------------------
# calc
# --------------------------------------------------------------------------

class TestCalcCommand(_DataDirMixin):
    def test_hand_checkable_numbers(self):
        code, out, _err = _run(self.argv("calc", "测试甲"))
        self.assertEqual(code, 0)
        # 技能期攻击力 2100，DPH 2100，循环 DPS 1350
        self.assertIn("2100.0", out)
        self.assertIn("1350.0", out)
        self.assertIn("25.0%", out)

    def test_shows_panel_and_target(self):
        _code, out, _err = _run(self.argv("calc", "测试甲", "--def", "800"))
        self.assertIn("面板攻击 1000", out)
        self.assertIn("防御 800", out)

    def test_defense_reduces_dph(self):
        _code, out, _err = _run(self.argv("calc", "测试甲", "--def", "100"))
        # 2100 − 100 = 2000
        self.assertIn("2000.0", out)

    def test_hp_produces_time_to_kill(self):
        _code, out, _err = _run(self.argv("calc", "测试甲", "--hp", "13500"))
        # 循环 DPS 1350 → 10 秒
        self.assertIn("10.0s", out)

    def test_five_percent_floor_is_visible(self):
        """防御高到超过攻击力时走 5% 下限——结果不该是 0。"""
        _code, out, _err = _run(self.argv("calc", "测试甲", "--def", "99999"))
        self.assertIn("105.0", out)  # 2100 × 5%

    def test_skill_filter(self):
        code, out, _err = _run(self.argv("calc", "测试甲", "--skill", "测试技能"))
        self.assertEqual(code, 0)
        self.assertIn("测试技能", out)

    def test_unknown_skill_lists_the_options(self):
        code, _out, err = _run(self.argv("calc", "测试甲", "--skill", "没有的技能"))
        self.assertEqual(code, 1)
        self.assertIn("测试技能", err)

    def test_unknown_operator_fails(self):
        code, _out, err = _run(self.argv("calc", "没有这个"))
        self.assertEqual(code, 1)
        self.assertIn("找不到干员", err)

    def test_json_is_parseable_and_hand_checkable(self):
        _code, out, _err = _run(self.argv("calc", "测试甲", "--json"))
        data = json.loads(out)
        self.assertEqual(data["operator"], "测试甲")
        skill_row = next(r for r in data["rows"] if r["skill"] == "测试技能")
        self.assertAlmostEqual(skill_row["dph"], 2100.0)
        self.assertAlmostEqual(skill_row["dps_cycle"], 1350.0)
        self.assertAlmostEqual(skill_row["uptime"], 0.25)

    def test_json_normal_row_is_marked(self):
        _code, out, _err = _run(self.argv("calc", "测试甲", "--json"))
        rows = json.loads(out)["rows"]
        normal = [r for r in rows if r["is_normal"]]
        self.assertEqual(len(normal), 1)
        self.assertAlmostEqual(normal[0]["dps_cycle"], 1100.0)

    def test_operator_without_skills_still_works(self):
        code, out, _err = _run(self.argv("calc", "缺数据"))
        self.assertEqual(code, 0)
        self.assertIn("（常态）", out)

    def test_notes_are_shown(self):
        """模型给出的假设必须让人看见，不能只藏在结果里。

        强制指定 3 个目标会触发"总 DPS 为上表数值 × 3"这条说明。
        """
        code, out, _err = _run(self.argv("calc", "测试甲", "--targets", "3"))
        self.assertEqual(code, 0)
        self.assertIn("说明", out)
        self.assertIn("同时攻击 3 个目标", out)

    def test_no_notes_means_no_notes_section(self):
        """没有假设就不该凭空长出一段"说明"。"""
        _code, out, _err = _run(self.argv("calc", "测试甲"))
        self.assertNotIn("说明", out)

    def test_data_warning_reaches_the_operator(self):
        _code, out, _err = _run(self.argv("calc", "缺数据"))
        self.assertIn("⚠", out)


# --------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------

class TestCompareCommand(_DataDirMixin):
    def test_sorted_by_cycle_dps(self):
        code, out, _err = _run(self.argv("compare", "测试乙", "测试甲"))
        self.assertEqual(code, 0)
        self.assertLess(out.index("测试甲"), out.index("测试乙"))

    def test_sorted_by_dph(self):
        """乙的 DPH 更高，按 DPH 排应当它在前。"""
        code, out, _err = _run(self.argv("compare", "测试乙", "测试甲", "--by", "dph"))
        self.assertEqual(code, 0)
        self.assertLess(out.index("测试乙"), out.index("测试甲"))

    def test_json_is_parseable(self):
        _code, out, _err = _run(self.argv("compare", "测试甲", "测试乙", "--json"))
        data = json.loads(out)
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["operator"], "测试甲")

    def test_unknown_operator_fails(self):
        code, _out, err = _run(self.argv("compare", "测试甲", "没有这个"))
        self.assertEqual(code, 1)
        self.assertIn("找不到干员", err)


# --------------------------------------------------------------------------
# show
# --------------------------------------------------------------------------

class TestShowCommand(_DataDirMixin):
    def test_shows_panel_and_talent(self):
        code, out, _err = _run(self.argv("show", "测试甲"))
        self.assertEqual(code, 0)
        self.assertIn("测试甲", out)
        self.assertIn("攻击力 1000", out)
        self.assertIn("攻击力+10%", out)

    def test_shows_skill_source_text(self):
        _code, out, _err = _run(self.argv("show", "测试甲"))
        self.assertIn("测试技能", out)

    def test_json_is_parseable(self):
        _code, out, _err = _run(self.argv("show", "测试甲", "--json"))
        data = json.loads(out)
        self.assertEqual(data["name"], "测试甲")
        self.assertEqual(data["base_atk"], 1000.0)
        self.assertEqual(len(data["skills"]), 1)
        self.assertEqual(data["skills"][0]["charge"], "auto")

    def test_warning_is_surfaced(self):
        _code, out, _err = _run(self.argv("show", "缺数据"))
        self.assertIn("需要注意", out)
        self.assertIn("精英2_满级_攻击", out)

    def test_unknown_operator_fails(self):
        code, _out, err = _run(self.argv("show", "没有这个"))
        self.assertEqual(code, 1)
        self.assertIn("找不到干员", err)


# --------------------------------------------------------------------------
# coverage
# --------------------------------------------------------------------------

class TestCoverageCommand(_DataDirMixin):
    def _write_report(self, payload: dict):
        (self.root / "_coverage.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def test_missing_report_explains_what_to_do(self):
        """回归：这个用例曾经因为**别的用例**往系统临时目录写了报告而失败。

        共享的全局位置被污染时，"没有报告"这种用例会读到别人的残留——
        所以每个用例的临时根必须独占。
        """
        code, _out, err = _run(self.argv("coverage"))
        self.assertEqual(code, 1)
        self.assertIn("arkdps import", err)

    def test_renders_summary(self):
        self._write_report({
            "operators": 3, "skills": 10,
            "confidence": {"exact": 6, "partial": 2, "low": 2},
            "skills_needing_review": 2,
            "top_unresolved_risky": [["某句话", 4]],
            "cache_hits": 3, "requests": 0, "failures": [],
        })
        code, out, _err = _run(self.argv("coverage"))
        self.assertEqual(code, 0)
        self.assertIn("60%", out)
        self.assertIn("20%", out)
        self.assertIn("某句话", out)

    def test_json_round_trips(self):
        payload = {"operators": 1, "skills": 0, "confidence": {}, "failures": []}
        self._write_report(payload)
        _code, out, _err = _run(self.argv("coverage", "--json"))
        self.assertEqual(json.loads(out)["operators"], 1)

    def test_zero_skills_does_not_divide_by_zero(self):
        self._write_report({"operators": 0, "skills": 0, "confidence": {}})
        code, out, _err = _run(self.argv("coverage"))
        self.assertEqual(code, 0)
        self.assertIn("技能总数", out)


class TestEncodingModuleSurface(unittest.TestCase):
    def test_configure_output_encoding_is_idempotent(self):
        cli._configure_output_encoding()
        cli._configure_output_encoding()
        self.assertTrue(callable(cli.main))

    def test_write_handles_empty_string(self):
        out = io.StringIO()
        with redirect_stdout(out):
            cli._write("")
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
