"""伤害公式测试。

期望值都取了整数，可以口算核对。
"""

from __future__ import annotations

import unittest

from fixtures import effects, enemy
from arkdps.damage import (
    effective_defense,
    effective_res,
    per_hit_damage,
    resolve_attack,
)
from arkdps.model import DamageType
from arkdps.rules import GameRules

RULES = GameRules()


class TestEffectiveDefense(unittest.TestCase):
    def test_percent_applies_before_flat(self):
        """先按比例削减，再减固定值。顺序反了结果会差很多。"""
        # 1000 × (1 − 50%) − 200 = 300
        self.assertAlmostEqual(
            effective_defense(1000.0, effects(def_ignore_pct=50.0, def_ignore=200.0)),
            300.0,
        )

    def test_order_matters(self):
        # 如果先减固定值再打折：(1000 − 200) × 0.5 = 400 ≠ 300
        result = effective_defense(
            1000.0, effects(def_ignore_pct=50.0, def_ignore=200.0)
        )
        self.assertNotAlmostEqual(result, 400.0)

    def test_never_goes_negative(self):
        self.assertAlmostEqual(
            effective_defense(100.0, effects(def_ignore=5000.0)), 0.0
        )

    def test_no_penetration_is_identity(self):
        self.assertAlmostEqual(effective_defense(800.0, effects()), 800.0)

    def test_full_percent_ignore(self):
        self.assertAlmostEqual(
            effective_defense(800.0, effects(def_ignore_pct=100.0)), 0.0
        )


class TestEffectiveRes(unittest.TestCase):
    def test_basic_reduction(self):
        self.assertAlmostEqual(effective_res(30.0, effects(res_ignore=10.0)), 20.0)

    def test_can_go_negative(self):
        """法抗能被削成负数——这是机制，不是错误。"""
        self.assertAlmostEqual(effective_res(10.0, effects(res_ignore=50.0)), -40.0)

    def test_percent_then_flat(self):
        # 60 × (1 − 50%) − 10 = 20
        self.assertAlmostEqual(
            effective_res(60.0, effects(res_ignore_pct=50.0, res_ignore=10.0)), 20.0
        )


class TestPhysicalDamage(unittest.TestCase):
    def test_subtraction(self):
        self.assertAlmostEqual(
            per_hit_damage(1000.0, DamageType.PHYSICAL, enemy(defense=400.0),
                           effects(), RULES),
            600.0,
        )

    def test_minimum_five_percent(self):
        """攻击力低于防御时，伤害不会归零，而是保底 5%。"""
        self.assertAlmostEqual(
            per_hit_damage(100.0, DamageType.PHYSICAL, enemy(defense=500.0),
                           effects(), RULES),
            5.0,
        )

    def test_exactly_at_the_floor(self):
        # 100 − 95 = 5，与保底值相同，两边都应该是 5
        self.assertAlmostEqual(
            per_hit_damage(100.0, DamageType.PHYSICAL, enemy(defense=95.0),
                           effects(), RULES),
            5.0,
        )

    def test_defense_penetration_applies(self):
        # 有效防御 = 1000×0.5 − 100 = 400 → 1000 − 400 = 600
        self.assertAlmostEqual(
            per_hit_damage(
                1000.0, DamageType.PHYSICAL, enemy(defense=1000.0),
                effects(def_ignore_pct=50.0, def_ignore=100.0), RULES,
            ),
            600.0,
        )

    def test_zero_defense(self):
        self.assertAlmostEqual(
            per_hit_damage(1000.0, DamageType.PHYSICAL, enemy(defense=0.0),
                           effects(), RULES),
            1000.0,
        )


class TestArtsDamage(unittest.TestCase):
    def test_basic_reduction(self):
        # 1000 × (1 − 30/100) = 700
        self.assertAlmostEqual(
            per_hit_damage(1000.0, DamageType.ARTS, enemy(res=30.0), effects(), RULES),
            700.0,
        )

    def test_minimum_five_percent(self):
        # 1000 × (1 − 99/100) = 10，低于保底 50 → 取 50
        self.assertAlmostEqual(
            per_hit_damage(1000.0, DamageType.ARTS, enemy(res=99.0), effects(), RULES),
            50.0,
        )

    def test_negative_res_amplifies_damage(self):
        """负法抗让法术伤害**高于**攻击力。"""
        # 有效法抗 = 0 − 50 = −50 → 1000 × (1 + 0.5) = 1500
        result = per_hit_damage(
            1000.0, DamageType.ARTS, enemy(res=0.0),
            effects(res_ignore=50.0), RULES,
        )
        self.assertAlmostEqual(result, 1500.0)
        self.assertGreater(result, 1000.0, "负法抗必须让伤害超过攻击力")

    def test_res_never_floor_clamped(self):
        """与防御不同，有效法抗**不做下限截断**。"""
        self.assertLess(effective_res(0.0, effects(res_ignore=100.0)), 0.0)


class TestTrueDamage(unittest.TestCase):
    def test_ignores_defense_and_res(self):
        self.assertAlmostEqual(
            per_hit_damage(1000.0, DamageType.TRUE,
                           enemy(defense=5000.0, res=99.0), effects(), RULES),
            1000.0,
        )


class TestDamageBreakdown(unittest.TestCase):
    def test_per_attack_includes_all_hits(self):
        """DPH 是"打一下"的总伤害，多段攻击要全算上。"""
        breakdown = resolve_attack(
            1000.0, DamageType.PHYSICAL, hits=3.0,
            enemy=enemy(defense=0.0), effects=effects(), rules=RULES,
        )
        self.assertAlmostEqual(breakdown.per_hit, 1000.0)
        self.assertAlmostEqual(breakdown.per_attack, 3000.0)

    def test_bonus_arts_damage_is_reduced_by_res(self):
        """附加法术伤害**同样受法抗影响**——这一点很容易漏。"""
        # 主体 1000 物理（0 防 → 1000）；附加 50% 攻击力法术，法抗 50 → 250
        breakdown = resolve_attack(
            1000.0, DamageType.PHYSICAL, hits=1.0, enemy=enemy(defense=0.0, res=50.0),
            effects=effects(bonus_arts_pct=50.0), rules=RULES,
        )
        self.assertAlmostEqual(breakdown.bonus_arts, 250.0)
        self.assertAlmostEqual(breakdown.per_attack, 1250.0)

    def test_bonus_true_damage_ignores_res(self):
        breakdown = resolve_attack(
            1000.0, DamageType.PHYSICAL, hits=1.0, enemy=enemy(defense=0.0, res=90.0),
            effects=effects(bonus_true_pct=50.0), rules=RULES,
        )
        self.assertAlmostEqual(breakdown.bonus_true, 500.0)

    def test_no_bonus_is_zero(self):
        breakdown = resolve_attack(
            1000.0, DamageType.PHYSICAL, hits=1.0, enemy=enemy(),
            effects=effects(), rules=RULES,
        )
        self.assertEqual(breakdown.bonus_arts, 0.0)
        self.assertEqual(breakdown.bonus_true, 0.0)

    def test_breakdown_exposes_intermediates(self):
        """拆解里要能看到中间量——计算器的价值一半在于"能核对"。"""
        breakdown = resolve_attack(
            1000.0, DamageType.PHYSICAL, hits=1.0, enemy=enemy(defense=300.0),
            effects=effects(), rules=RULES,
        )
        self.assertAlmostEqual(breakdown.effective_defense, 300.0)
        self.assertAlmostEqual(breakdown.atk, 1000.0)


class TestGameRules(unittest.TestCase):
    def test_custom_min_damage_ratio(self):
        rules = GameRules(min_damage_ratio=0.10)
        # 100 × 10% = 10 > (100 − 500) → 10
        self.assertAlmostEqual(
            per_hit_damage(100.0, DamageType.PHYSICAL, enemy(defense=500.0),
                           effects(), rules),
            10.0,
        )

    def test_frame_rounding_rounds_to_frames(self):
        rules = GameRules(frame_rounding=True)
        # 0.64 秒 × 30 帧 = 19.2 → 19 帧 → 19/30 ≈ 0.6333
        self.assertAlmostEqual(rules.round_interval(0.64), 19 / 30)

    def test_frame_rounding_off_is_identity(self):
        self.assertAlmostEqual(GameRules().round_interval(0.64), 0.64)

    def test_minimum_one_frame(self):
        rules = GameRules(frame_rounding=True)
        self.assertAlmostEqual(rules.round_interval(0.001), 1 / 30)

    def test_invalid_rules_rejected(self):
        with self.assertRaises(ValueError):
            GameRules(fps=0)
        with self.assertRaises(ValueError):
            GameRules(min_damage_ratio=2.0)


if __name__ == "__main__":
    unittest.main()
