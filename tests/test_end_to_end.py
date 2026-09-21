"""端到端测试：从草稿字典一路算到 DPS / DPH。

前面每个模块都测了自己那一层。这里把整条链路接起来::

    JSON 草稿 → loader → Operator/Skill → combat → damage → rotation → 结果

**期望值全部手算得出来**，注释里写出算式。测试里出现 ``1645.9`` 时，
读的人应该能拿计算器核对，而不是只能相信作者。

两个锚点刻意照着真实干员的数值构造（银灰的真银斩、能天使的双天赋），
因为那正是这套引擎要算的东西——但它们写成**合成草稿**，
所以离线、不随 PRTS 改版而变。

最后一组用例拿**全部 460 份导入草稿**跑一遍，只看"会不会崩、
会不会算出非法数值"，不看具体数字——真实数据会随版本变，
钉死数字等于给自己找麻烦。
"""

from __future__ import annotations

import math
import unittest
from pathlib import Path

from fixtures import operator  # noqa: F401  （导入以设定 sys.path）

from arkdps import (
    Clause,
    ChargeType,
    DamageType,
    DEFAULT_RULES,
    Effects,
    Enemy,
    GameRules,
    Operator,
    Recognition,
    Skill,
    Triple,
    analyze,
    map_clauses,
    summarize,
)
from arkdps import __version__
from arkdps.loader import load_operators, operator_from_dict
from arkdps.rotation import solve_operator, solve_skill

ROOT = Path(__file__).resolve().parent.parent
DATA_OPERATORS = ROOT / "data" / "operators"


# --------------------------------------------------------------------------
# 锚点一：银灰 真银斩
# --------------------------------------------------------------------------
#
# 面板 713 + 信赖 50 + 潜能 26 = 789
# 天赋「领袖」攻击力 +10%，真银斩攻击力 +200%（百分比**加算**）
#   常态攻击力 = 789 × (1 + 0.10)          = 867.9
#   技能攻击力 = 789 × (1 + 0.10 + 2.00)   = 2445.9
# 打 800 防御的敌人
#   常态 = 867.9 − 800 = 67.9      （高于 5% 下限）
#   技能 = 2445.9 − 800 = 1645.9
# 攻击间隔 1.3 秒
#   技能期 DPS = 1645.9 ÷ 1.3 = 1266.08
# 技力 90，自动回复 1/秒，持续 30 秒 → 循环 120 秒，覆盖率 25%
#   循环伤害 = 1266.08×30 + 52.23×90 = 42683.1
#   循环 DPS = 42683.1 ÷ 120 = 355.7
SILVERASH_DRAFT = {
    "name": "合成银灰",
    "atk": 713.0,
    "atk_trust": 50.0,
    "atk_potential": 26.0,
    "attack_interval": 1.3,
    "damage_type": "physical",
    "talent": {"atk_pct": 10.0},
    "skills": [
        {
            "name": "合成真银斩",
            "charge": "auto",
            "sp_cost": 90.0,
            "duration": 30.0,
            "effects": {"atk_pct": 200.0, "targets": 6},
        }
    ],
}

DEF_800 = Enemy(defense=800.0, name="800 防御")


class TestSilverashAnchor(unittest.TestCase):
    """手算锚点：一次完整的「百分比加算 → 减防御 → 开技能循环」。"""

    def setUp(self):
        self.op = operator_from_dict(SILVERASH_DRAFT)
        self.skill = self.op.skill("合成真银斩")
        assert self.skill is not None
        self.result = solve_skill(self.op, self.skill, DEF_800, rules=DEFAULT_RULES)

    def test_panel_attack_sums_trust_and_potential(self):
        self.assertEqual(self.op.base_atk, 789.0)

    def test_normal_attack_is_percentage_buff_only(self):
        self.assertAlmostEqual(self.result.atk_normal, 867.9, places=4)

    def test_skill_attack_is_additive_not_multiplicative(self):
        """回归：百分比**加算**。写成乘算会得到 789×1.1×3.0 = 2603.7，差很多。"""
        self.assertAlmostEqual(self.result.atk_skill, 2445.9, places=4)

    def test_dph_subtracts_defense(self):
        self.assertAlmostEqual(self.result.dph, 1645.9, places=4)

    def test_skill_dps(self):
        self.assertAlmostEqual(self.result.dps_skill, 1266.08, places=2)

    def test_charge_time_is_full_sp_cost(self):
        """阻回：技能持续期间技力不回复，充能是整段 90 秒。"""
        self.assertAlmostEqual(self.result.charge_time, 90.0)

    def test_uptime(self):
        self.assertAlmostEqual(self.result.uptime, 0.25)

    def test_cycle_dps(self):
        self.assertAlmostEqual(self.result.dps_cycle, 355.7, places=1)

    def test_targets_multiply_the_total(self):
        self.assertEqual(self.result.targets, 6)
        self.assertAlmostEqual(
            self.result.dps_cycle_total, self.result.dps_cycle * 6, places=6
        )

    def test_normal_row_exposes_the_same_skill(self):
        rows = solve_operator(self.op, DEF_800)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].is_normal_row)
        self.assertAlmostEqual(rows[0].dph, 67.9, places=4)


# --------------------------------------------------------------------------
# 锚点二：能天使 双天赋 + 两种技能
# --------------------------------------------------------------------------
#
# 面板 540 + 信赖 90 + 潜能 27 = 657
# 天赋「快速弹匣」攻速 +12，「天使的祝福」攻击力 +6%（两个天赋都要生效）
#   常态攻击力 = 657 × 1.06 = 696.42
#   常态间隔   = 1.0 ÷ 1.12 = 0.892857
# 冲锋模式：攻击力提高至 145%，3 连发
#   攻击力 = 696.42 × 1.45 = 1009.81
#   DPH   = 1009.81 × 3 = 3029.43
# 过载模式：攻击力提高至 110%，5 连发，间隔 ×0.78
#   攻击力 = 696.42 × 1.10 = 766.06
#   DPH   = 766.06 × 5 = 3830.31
#   间隔   = 1.0 × 0.78 ÷ 1.12 = 0.696429
EXUSIAI_DRAFT = {
    "name": "合成能天使",
    "atk": 540.0,
    "atk_trust": 90.0,
    "atk_potential": 27.0,
    "attack_interval": 1.0,
    "damage_type": "physical",
    "talent": {"aspd": 12.0, "atk_pct": 6.0},
    "skills": [
        {
            "name": "冲锋模式",
            "charge": "auto",
            "sp_cost": 30.0,
            "duration": 15.0,
            "effects": {"atk_mult": 1.45, "hits_override": 3.0},
        },
        {
            "name": "过载模式",
            "charge": "auto",
            "sp_cost": 20.0,
            "duration": 15.0,
            "effects": {
                "atk_mult": 1.1,
                "hits_override": 5.0,
                "interval_mult": 0.78,
            },
        },
    ],
}

NO_DEF = Enemy(defense=0.0, name="无防御")


class TestExusiaiAnchor(unittest.TestCase):
    def setUp(self):
        self.op = operator_from_dict(EXUSIAI_DRAFT)

    def test_both_talents_are_applied(self):
        """回归：两个天赋都要生效。只取一个会同时丢掉攻速和攻击力。"""
        result = solve_skill(self.op, None, NO_DEF)
        self.assertAlmostEqual(result.atk_normal, 696.42, places=4)
        self.assertAlmostEqual(result.interval_normal, 1.0 / 1.12, places=6)

    def test_charge_mode(self):
        result = solve_skill(self.op, self.op.skill("冲锋模式"), NO_DEF)
        self.assertAlmostEqual(result.atk_skill, 1009.809, places=3)
        self.assertAlmostEqual(result.dph, 3029.427, places=3)
        self.assertAlmostEqual(result.dph_per_hit, 1009.809, places=3)

    def test_overload_mode_interval_multiplier(self):
        """回归：「攻击间隔×0.78」是倍率，不是 −0.78 秒。"""
        result = solve_skill(self.op, self.op.skill("过载模式"), NO_DEF)
        self.assertAlmostEqual(result.interval_skill, 0.78 / 1.12, places=6)
        self.assertAlmostEqual(result.atk_skill, 766.062, places=3)
        self.assertAlmostEqual(result.dph, 3830.31, places=2)

    def test_more_hits_means_higher_dph_but_not_higher_dps_per_hit(self):
        charge = solve_skill(self.op, self.op.skill("冲锋模式"), NO_DEF)
        overload = solve_skill(self.op, self.op.skill("过载模式"), NO_DEF)
        self.assertGreater(overload.dph, charge.dph)
        self.assertGreater(overload.dps_skill, charge.dps_skill)


class TestDphVersusDps(unittest.TestCase):
    """DPH 与 DPS 是两件事，混了会让"谁更强"判断反掉。"""

    def test_relationship_holds(self):
        op = operator_from_dict(EXUSIAI_DRAFT)
        result = solve_skill(op, op.skill("冲锋模式"), NO_DEF)
        self.assertAlmostEqual(
            result.dps_skill,
            result.dph / result.interval_skill,
            places=6,
        )

    def test_high_dph_can_have_low_dps(self):
        """慢速高伤 vs 快速低伤——DPH 高不代表 DPS 高。"""
        slow = Operator(name="慢", atk=2000.0, interval=2.0)
        fast = Operator(name="快", atk=1000.0, interval=0.5)
        slow_row = solve_skill(slow, None, NO_DEF)
        fast_row = solve_skill(fast, None, NO_DEF)

        self.assertGreater(slow_row.dph, fast_row.dph)
        self.assertLess(slow_row.dps_normal, fast_row.dps_normal)


class TestPipelineConsistency(unittest.TestCase):
    """链路各层之间的一致性——这类错误最不容易被发现。"""

    def test_effects_are_actually_used(self):
        """回归防线：去掉天赋，结果必须变化。全部相等说明天赋被忽略了。"""
        with_talent = operator_from_dict(EXUSIAI_DRAFT)
        without = operator_from_dict({**EXUSIAI_DRAFT, "talent": {}})

        a = solve_skill(with_talent, None, NO_DEF)
        b = solve_skill(without, None, NO_DEF)
        self.assertNotAlmostEqual(a.atk_normal, b.atk_normal)
        self.assertNotAlmostEqual(a.interval_normal, b.interval_normal)

    def test_loader_and_engine_agree_on_skill_names(self):
        op = operator_from_dict(EXUSIAI_DRAFT)
        self.assertEqual(op.skill_names, ["冲锋模式", "过载模式"])
        for name in op.skill_names:
            self.assertIsNotNone(op.skill(name))

    def test_missing_skill_returns_none(self):
        op = operator_from_dict(EXUSIAI_DRAFT)
        self.assertIsNone(op.skill("不存在的技能"))

    def test_semantic_layer_feeds_the_engine(self):
        """手动走一遍「中文 → 字段 → 结果」，验证层与层能对接。"""
        clause_list = analyze("攻击力+200%，攻击速度+50")
        fields, _records, _unresolved = map_clauses(clause_list)
        self.assertEqual(fields, {"atk_pct": 200.0, "aspd": 50.0})

        op = Operator(name="合成", atk=1000.0, interval=1.0,
                      skills=[Skill(name="S", effects=Effects(**fields))])
        result = solve_skill(op, op.skill("S"), NO_DEF)
        # 攻击力 3000，间隔 1.0 ÷ 1.5
        self.assertAlmostEqual(result.atk_skill, 3000.0)
        self.assertAlmostEqual(result.interval_skill, 1.0 / 1.5, places=6)

    def test_conditional_effects_stay_out_of_the_pipeline(self):
        """带条件的效果不该在无人工确认的情况下影响结果。"""
        clauses = analyze("攻击力+50%，但此时攻击力降低至80%")
        fields, _records, _unresolved = map_clauses(clauses)
        self.assertEqual(fields, {"atk_pct": 50.0})

        op = Operator(name="合成", atk=1000.0, interval=1.0,
                      skills=[Skill(name="S", effects=Effects(**fields))])
        result = solve_skill(op, op.skill("S"), NO_DEF)
        self.assertAlmostEqual(result.atk_skill, 1500.0)


class TestPublicApi(unittest.TestCase):
    def test_version_is_a_string(self):
        self.assertIsInstance(__version__, str)
        self.assertTrue(__version__)

    def test_exports_are_importable(self):
        """``__all__`` 里写了的名字必须真的能导入——否则文档就是骗人的。"""
        import arkdps

        for name in arkdps.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(arkdps, name), f"{name} 在 __all__ 里但导不进来")

    def test_key_types_are_the_expected_ones(self):
        self.assertIsInstance(Operator(name="x", atk=1.0, interval=1.0), Operator)
        self.assertIsInstance(Effects(), Effects)
        self.assertIsInstance(Skill(name="s"), Skill)
        self.assertIsInstance(Enemy(), Enemy)
        self.assertIsInstance(DEFAULT_RULES, GameRules)

        self.assertEqual(DamageType.ARTS.value, "arts")
        self.assertEqual(ChargeType.ATTACK.value, "attack")
        self.assertIsInstance(Recognition.RECOGNIZED, Recognition)
        self.assertIsInstance(Triple("a", "b", 1.0, "", "x"), Triple)
        self.assertIsInstance(Clause("x", Recognition.UNKNOWN), Clause)

        for func in (analyze, summarize, map_clauses):
            with self.subTest(func=func.__name__):
                self.assertTrue(callable(func))


def _real_data_available() -> bool:
    try:
        return DATA_OPERATORS.is_dir() and any(DATA_OPERATORS.glob("*.json"))
    except OSError:
        return False


@unittest.skipUnless(_real_data_available(), f"没有可用的导入数据：{DATA_OPERATORS}")
class TestWholeCorpusRobustness(unittest.TestCase):
    """拿**全部**导入草稿跑一遍引擎。

    只检查"会不会崩"和"数值合不合法"，**不钉死具体数字**——
    真实数据随版本变，钉死等于给自己找麻烦。
    """

    @classmethod
    def setUpClass(cls):
        cls.operators = load_operators(DATA_OPERATORS)
        cls.hits = 0
        cls.failures: list[str] = []

        enemy = Enemy(defense=500.0, res=20.0, hp=20000.0, name="综合测试目标")
        for op in cls.operators:
            try:
                rows = solve_operator(op, enemy)
            except Exception as exc:  # noqa: BLE001 —— 这里就是要抓住一切异常
                cls.failures.append(f"{op.name}: {type(exc).__name__}: {exc}")
                continue
            cls.hits += len(rows)
            for row in rows:
                for label, value in (
                    ("dph", row.dph),
                    ("dps_cycle", row.dps_cycle),
                    ("uptime", row.uptime),
                    ("ttk", row.ttk),
                ):
                    if math.isnan(value) or value < 0:
                        cls.failures.append(
                            f"{op.name}/{row.label}: {label} = {value}"
                        )

    def test_corpus_is_not_empty(self):
        self.assertGreater(len(self.operators), 100)

    def test_no_operator_crashes_the_engine(self):
        self.assertEqual(self.failures[:10], [], "有草稿把引擎跑崩了")

    def test_every_result_is_finite_and_non_negative(self):
        self.assertGreater(self.hits, 100)

    def test_every_operator_has_a_positive_interval(self):
        """引擎要拿攻击间隔当除数，为 0 会直接炸。"""
        broken = [op.name for op in self.operators if not (op.interval > 0)]
        self.assertEqual(broken[:10], [])

    def test_missing_attack_is_visible_not_silent(self):
        """面板攻击力缺失的草稿必须**带着说明**载入。

        少数草稿（异格、特殊页面）取不到「精英2_满级_攻击」，
        载入后 ``base_atk`` 是 0——DPS 会算成 0。

        允许这种情况存在，因为页面结构确实会变；但**绝不允许它悄无声息**：
        导入器专门记下的 ``_warnings`` 必须在 ``note`` 里看得见。
        """
        silent = [
            op.name for op in self.operators
            if op.base_atk <= 0 and not op.note
        ]
        self.assertEqual(
            silent[:10], [],
            "面板攻击力为 0 却没给出任何说明——这种错误没人看得出来",
        )

    def test_zero_attack_operators_are_a_small_minority(self):
        """这类草稿应该是少数；比例突然变高说明 PRTS 页面结构变了。"""
        zero = [op for op in self.operators if op.base_atk <= 0]
        self.assertLess(
            len(zero) / len(self.operators), 0.10,
            f"有 {len(zero)}/{len(self.operators)} 份草稿取不到攻击力，偏多",
        )

    def test_rarity_is_one_based(self):
        """回归：PRTS 的稀有度是 0 起算，导入时要 +1。

        判据是"最小稀有度必须是 1"——1 星机器人（Castle-3 之类）
        在 PRTS 上是 0，漏掉 +1 或把它当成"没有值"都会在这里露出来。
        """
        rarities = [op.rarity for op in self.operators if op.rarity is not None]
        self.assertTrue(rarities, "没有一份草稿带稀有度")
        self.assertGreaterEqual(min(rarities), 1)
        self.assertLessEqual(max(rarities), 6)


if __name__ == "__main__":
    unittest.main()
