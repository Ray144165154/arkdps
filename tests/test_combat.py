"""攻击力合成与攻击间隔测试。

这一层最容易写错的是**百分比加算、倍率乘算**的区别——
把「攻击力+100%」和「攻击力提高至110%」加在一起算，
结果会差一大截，而且不会报错。
"""

from __future__ import annotations

import unittest

from fixtures import effects, operator, skill
from arkdps.combat import build_state, effective_atk, effective_interval, merge_effects
from arkdps.model import DamageType, Effects
from arkdps.rules import GameRules

RULES = GameRules()


class TestEffectiveAtk(unittest.TestCase):
    def test_no_modifiers(self):
        self.assertAlmostEqual(effective_atk(1000.0, effects()), 1000.0)

    def test_percent_is_additive_inside_parentheses(self):
        # 1000 × (1 + 100/100) = 2000
        self.assertAlmostEqual(effective_atk(1000.0, effects(atk_pct=100.0)), 2000.0)

    def test_multiplier_is_outside_parentheses(self):
        # 1000 × 1 × 2.0 = 2000
        self.assertAlmostEqual(effective_atk(1000.0, effects(atk_mult=2.0)), 2000.0)

    def test_percent_and_multiplier_combined(self):
        """关键区分：两者作用位置不同，不能相加。

        「攻击力+100%」与「攻击力提高至110%」同时存在时：
            正确 → 1000 × (1 + 100%) × 1.1 = 2200
            错误 → 1000 × (1 + 100% + 10%) = 2100
        """
        result = effective_atk(1000.0, effects(atk_pct=100.0, atk_mult=1.1))
        self.assertAlmostEqual(result, 2200.0)
        self.assertNotAlmostEqual(result, 2100.0)

    def test_negative_percent(self):
        self.assertAlmostEqual(effective_atk(1000.0, effects(atk_pct=-50.0)), 500.0)


class TestEffectiveInterval(unittest.TestCase):
    def test_no_modifiers(self):
        self.assertAlmostEqual(effective_interval(1.0, effects()), 1.0)

    def test_flat_reduction(self):
        self.assertAlmostEqual(
            effective_interval(1.0, effects(interval_flat=-0.2)), 0.8
        )

    def test_aspd_divides(self):
        # 1.0 ÷ (1 + 25/100) = 0.8
        self.assertAlmostEqual(effective_interval(1.0, effects(aspd=25.0)), 0.8)

    def test_full_formula(self):
        """(基础 + 修正) ÷ (1 + 攻速/100) × 倍率

        用能天使过载模式的真实参数核对：
            基础 1.0，修正 −0.22，攻速 +12
            (1.0 − 0.22) ÷ 1.12 = 0.696428…
        """
        result = effective_interval(
            1.0, effects(interval_flat=-0.22, aspd=12.0)
        )
        self.assertAlmostEqual(result, 0.78 / 1.12, places=9)

    def test_multiplier_applies(self):
        # (1.0 + 0) × 0.7 = 0.7
        self.assertAlmostEqual(
            effective_interval(1.0, effects(interval_mult=0.7)), 0.7
        )

    def test_order_flat_then_mult_then_aspd(self):
        # (1.0 − 0.2) × 0.5 ÷ 1.25 = 0.32
        result = effective_interval(
            1.0, effects(interval_flat=-0.2, interval_mult=0.5, aspd=25.0)
        )
        self.assertAlmostEqual(result, 0.32)

    def test_never_below_one_frame(self):
        result = effective_interval(1.0, effects(interval_flat=-5.0))
        self.assertAlmostEqual(result, 1.0 / RULES.fps)


class TestEffectsMerge(unittest.TestCase):
    def test_percents_add(self):
        merged = effects(atk_pct=50.0).merged_with(effects(atk_pct=100.0))
        self.assertAlmostEqual(merged.atk_pct, 150.0)

    def test_multipliers_multiply(self):
        merged = effects(atk_mult=1.5).merged_with(effects(atk_mult=2.0))
        self.assertAlmostEqual(merged.atk_mult, 3.0)

    def test_targets_take_max(self):
        merged = effects(targets=1).merged_with(effects(targets=6))
        self.assertEqual(merged.targets, 6)
        merged_back = effects(targets=6).merged_with(effects(targets=1))
        self.assertEqual(merged_back.targets, 6)

    def test_damage_type_override(self):
        merged = effects().merged_with(effects(damage_type=DamageType.ARTS))
        self.assertEqual(merged.damage_type, DamageType.ARTS)
        # 反转方向不应把已有值抹掉
        kept = effects(damage_type=DamageType.ARTS).merged_with(effects())
        self.assertEqual(kept.damage_type, DamageType.ARTS)

    def test_hits_override_wins(self):
        merged = effects(hits_mult=2.0).merged_with(effects(hits_override=5.0))
        self.assertEqual(merged.hits_override, 5.0)

    def test_attacks_is_and(self):
        merged = effects(attacks=True).merged_with(effects(attacks=False))
        self.assertFalse(merged.attacks)
        self.assertFalse(effects(attacks=False).merged_with(effects()).attacks)

    def test_identity_merge_is_noop(self):
        original = effects(atk_pct=100.0, aspd=20.0)
        self.assertEqual(original.merged_with(effects()), original)

    def test_is_identity(self):
        self.assertTrue(effects().is_identity())
        self.assertFalse(effects(atk_pct=1.0).is_identity())


class TestBuildState(unittest.TestCase):
    def test_three_layer_merge(self):
        """特性 + 天赋 + 技能三层修正都要生效。"""
        op = operator(
            atk=1000.0,
            trait=effects(atk_mult=0.8),
            talent=effects(atk_pct=10.0),
        )
        sk = skill(atk_pct=200.0)
        state = build_state(op, sk, rules=RULES)
        # 1000 × (1 + 210/100) × 0.8 = 2480
        self.assertAlmostEqual(state.atk, 2480.0)

    def test_base_atk_includes_trust_and_potential(self):
        op = operator(atk=700.0, atk_trust=50.0, atk_potential=26.0)
        self.assertAlmostEqual(op.base_atk, 776.0)
        state = build_state(op, None, rules=RULES)
        self.assertAlmostEqual(state.atk, 776.0)

    def test_no_skill_uses_operator_state(self):
        op = operator(atk=1000.0, interval=1.3)
        state = build_state(op, None, rules=RULES)
        self.assertAlmostEqual(state.interval, 1.3)

    def test_hits_override_replaces_base_hits(self):
        op = operator(hits=1.0)
        sk = skill(hits_override=5.0)
        self.assertAlmostEqual(build_state(op, sk, rules=RULES).hits, 5.0)

    def test_hits_multiplier_and_adder(self):
        op = operator(hits=2.0)
        sk = skill(hits_mult=2.0, hits_add=1.0)
        # 2 × 2 + 1 = 5
        self.assertAlmostEqual(build_state(op, sk, rules=RULES).hits, 5.0)

    def test_hits_never_below_one(self):
        op = operator(hits=1.0)
        sk = skill(hits_add=-5.0)
        self.assertGreaterEqual(build_state(op, sk, rules=RULES).hits, 1.0)

    def test_target_count_override(self):
        op = operator()
        sk = skill(targets=6)
        state = build_state(op, sk, rules=RULES, target_count=3)
        self.assertEqual(state.targets, 3)

    def test_targets_scope_without_override_defaults_to_one(self):
        op = operator()
        sk = skill(targets_scope="all_blocked")
        state = build_state(op, sk, rules=RULES)
        self.assertEqual(state.targets, 1)

    def test_damage_type_override(self):
        op = operator(damage_type=DamageType.PHYSICAL)
        sk = skill(damage_type=DamageType.ARTS)
        self.assertEqual(
            build_state(op, sk, rules=RULES).damage_type, DamageType.ARTS
        )

    def test_attacks_flag_propagates(self):
        op = operator()
        sk = skill(attacks=False)
        self.assertFalse(build_state(op, sk, rules=RULES).effects.attacks)

    def test_attacks_per_second(self):
        op = operator(interval=1.0)
        sk = skill(aspd=100.0)
        # 间隔 1.0 ÷ 2 = 0.5 → 2 次/秒
        self.assertAlmostEqual(build_state(op, sk, rules=RULES).attacks_per_second, 2.0)


class TestMergeEffectsHelper(unittest.TestCase):
    def test_merge_without_skill(self):
        op = operator(trait=effects(atk_mult=0.8), talent=effects(atk_pct=10.0))
        merged = merge_effects(op, None)
        self.assertAlmostEqual(merged.atk_pct, 10.0)
        self.assertAlmostEqual(merged.atk_mult, 0.8)

    def test_merge_leaves_operator_untouched(self):
        op = operator(talent=Effects(atk_pct=10.0))
        merge_effects(op, skill(atk_pct=50.0))
        self.assertAlmostEqual(op.talent.atk_pct, 10.0, msg="不能就地修改干员数据")


if __name__ == "__main__":
    unittest.main()
