"""技能循环求解测试。

四种技能形态必须分别建模，混在一起算会得到荒谬的结果：

  * 持续型 —— 正常的「充能 + 持续」循环
  * 瞬发型 —— 「N 次攻击中 1 次被强化」，当成"持续 0 秒的技能"会算出无穷大
  * 无限持续 —— 循环时长无意义
  * 形态切换 —— 没有"持续多久"，只有"当前在哪个形态"

所有期望值都可以口算。
"""

from __future__ import annotations

import math
import unittest

from fixtures import enemy, operator, skill
from arkdps.model import ChargeType, DamageType
from arkdps.rotation import solve_operator, solve_skill
from arkdps.rules import GameRules

RULES = GameRules()


class TestContinuousSkill(unittest.TestCase):
    """持续型技能：最常见的形态。"""

    def setUp(self):
        # 常态 1000 DPS；技能期攻击力翻倍 → 2000 DPS
        # 消耗 30 技力（自动回复 1/秒 → 充能 30 秒），持续 10 秒
        self.op = operator(atk=1000.0, interval=1.0)
        self.sk = skill("翻倍", atk_pct=100.0, sp_cost=30.0, duration=10.0)
        self.result = solve_skill(self.op, self.sk, enemy(defense=0.0), rules=RULES)

    def test_normal_dps(self):
        self.assertAlmostEqual(self.result.dps_normal, 1000.0)

    def test_skill_dps(self):
        self.assertAlmostEqual(self.result.dps_skill, 2000.0)

    def test_charge_time_is_full_sp_cost_over_rate(self):
        """阻回：技能持续期间技力**不回复**，所以充能是整段 30 秒。"""
        self.assertAlmostEqual(self.result.charge_time, 30.0)

    def test_cycle_time(self):
        self.assertAlmostEqual(self.result.cycle_time, 40.0)

    def test_uptime(self):
        self.assertAlmostEqual(self.result.uptime, 0.25)

    def test_cycle_damage(self):
        # 2000 × 10 + 1000 × 30 = 50000
        self.assertAlmostEqual(self.result.damage_cycle, 50000.0)

    def test_cycle_dps(self):
        # 50000 ÷ 40 = 1250
        self.assertAlmostEqual(self.result.dps_cycle, 1250.0)

    def test_total_dps_scales_with_targets(self):
        result = solve_skill(
            self.op, skill(atk_pct=100.0, sp_cost=30.0, duration=10.0, targets=3),
            enemy(defense=0.0), rules=RULES,
        )
        self.assertAlmostEqual(result.dps_cycle_total, 1250.0 * 3)


class TestInstantSkill(unittest.TestCase):
    """瞬发型：「下次攻击的攻击力提高至X%」这类。"""

    def setUp(self):
        # 常态一击 1000；技能把这一次攻击提到 2000
        # 消耗 4 技力 → 4 次攻击里 1 次被强化
        self.op = operator(atk=1000.0, interval=1.0)
        self.sk = skill("强力击", atk_mult=2.0, sp_cost=4.0, duration=0.0)
        self.result = solve_skill(self.op, self.sk, enemy(defense=0.0), rules=RULES)

    def test_not_treated_as_zero_duration_cycle(self):
        """若按「持续 0 秒」计算会得到无穷大 DPS——必须走瞬发分支。"""
        self.assertTrue(math.isfinite(self.result.dps_cycle))

    def test_cycle_damage_is_three_normal_plus_one_boosted(self):
        # 1000 × 3 + 2000 = 5000
        self.assertAlmostEqual(self.result.damage_cycle, 5000.0)

    def test_cycle_time_is_n_swings(self):
        # 4 次攻击 × 1 秒 = 4 秒
        self.assertAlmostEqual(self.result.cycle_time, 4.0)

    def test_cycle_dps(self):
        self.assertAlmostEqual(self.result.dps_cycle, 1250.0)

    def test_uptime_is_one_over_n(self):
        self.assertAlmostEqual(self.result.uptime, 0.25)

    def test_note_explains_the_model(self):
        self.assertTrue(any("瞬发" in n for n in self.result.notes))

    def test_is_instant_flag(self):
        self.assertTrue(self.sk.is_instant)


class TestInfiniteDuration(unittest.TestCase):
    def test_cycle_dps_equals_skill_dps(self):
        op = operator(atk=1000.0, interval=1.0)
        sk = skill("黄昏", atk_pct=200.0, sp_cost=5.0, infinite_duration=True)
        result = solve_skill(op, sk, enemy(defense=0.0), rules=RULES)
        self.assertAlmostEqual(result.dps_cycle, result.dps_skill)
        self.assertAlmostEqual(result.uptime, 1.0)
        self.assertEqual(result.cycle_time, math.inf)

    def test_note_present(self):
        op = operator()
        sk = skill(infinite_duration=True)
        result = solve_skill(op, sk, enemy(), rules=RULES)
        self.assertTrue(any("无限" in n for n in result.notes))

    def test_is_not_treated_as_instant(self):
        sk = skill(infinite_duration=True, sp_cost=5.0)
        self.assertFalse(sk.is_instant, "无限持续不能被当成瞬发")


class TestStanceSkill(unittest.TestCase):
    """形态切换类：没有"持续多久"的概念。"""

    def setUp(self):
        self.op = operator(atk=1000.0, interval=1.0)
        self.sk = skill("形态", atk_pct=50.0, sp_cost=5.0, stance=True)
        self.result = solve_skill(self.op, self.sk, enemy(defense=0.0), rules=RULES)

    def test_not_treated_as_instant(self):
        """早先的实现把它当瞬发，算出"5 次攻击中 1 次被强化"——完全错的。"""
        self.assertFalse(self.sk.is_instant)
        self.assertFalse(
            any("瞬发" in n for n in self.result.notes),
            "形态切换技能不该出现瞬发相关的说明",
        )

    def test_assumes_stance_stays_on(self):
        self.assertAlmostEqual(self.result.dps_cycle, self.result.dps_skill)
        self.assertAlmostEqual(self.result.uptime, 1.0)

    def test_note_states_the_assumption(self):
        self.assertTrue(
            any("形态切换" in n for n in self.result.notes),
            "必须说明「按开启后一直保持」是个假设",
        )


class TestChargeTypes(unittest.TestCase):
    def test_auto_regen(self):
        op = operator(interval=1.0)
        sk = skill(charge=ChargeType.AUTO, sp_cost=20.0, duration=0.0)
        result = solve_skill(op, sk, enemy(), rules=RULES)
        self.assertAlmostEqual(result.sp_per_second, 1.0)
        self.assertAlmostEqual(result.charge_time, 20.0)

    def test_attack_regen_uses_normal_attack_speed(self):
        """攻击回复：充能速度取决于**常态**攻速，不是技能期。"""
        op = operator(interval=0.5)  # 2 次/秒
        sk = skill(charge=ChargeType.ATTACK, sp_cost=10.0, duration=5.0)
        result = solve_skill(op, sk, enemy(), rules=RULES)
        self.assertAlmostEqual(result.sp_per_second, 2.0)
        self.assertAlmostEqual(result.charge_time, 5.0)

    def test_hit_regen_uses_assumed_rate(self):
        op = operator()
        sk = skill(charge=ChargeType.HIT, sp_cost=20.0, duration=5.0)
        result = solve_skill(
            op, sk, enemy(), rules=RULES, hits_taken_per_second=2.0
        )
        self.assertAlmostEqual(result.charge_time, 10.0)

    def test_extra_regen_from_allies(self):
        op = operator()
        sk = skill(charge=ChargeType.AUTO, sp_cost=30.0, duration=10.0)
        result = solve_skill(op, sk, enemy(), rules=RULES, extra_sp_regen=0.5)
        self.assertAlmostEqual(result.sp_per_second, 1.5)
        self.assertAlmostEqual(result.charge_time, 20.0)

    def test_passive_needs_no_sp(self):
        op = operator()
        sk = skill(charge=ChargeType.PASSIVE, sp_cost=0.0, duration=10.0)
        result = solve_skill(op, sk, enemy(), rules=RULES)
        self.assertAlmostEqual(result.charge_time, 0.0)

    def test_initial_sp_reduces_first_charge(self):
        op = operator()
        sk = skill(sp_cost=30.0, init_sp=20.0, duration=10.0)
        first = solve_skill(op, sk, enemy(), rules=RULES, use_initial_sp=True)
        steady = solve_skill(op, sk, enemy(), rules=RULES, use_initial_sp=False)
        self.assertAlmostEqual(first.charge_time, 10.0)
        self.assertAlmostEqual(steady.charge_time, 30.0)

    def test_zero_regen_means_never_fires(self):
        op = operator()
        sk = skill(charge=ChargeType.HIT, sp_cost=10.0, duration=5.0)
        result = solve_skill(
            op, sk, enemy(), rules=RULES, hits_taken_per_second=0.0
        )
        self.assertEqual(result.charge_time, math.inf)
        self.assertTrue(any("永远开不出来" in n for n in result.notes))


class TestDphDefinition(unittest.TestCase):
    """DPH = 一次攻击打出的总伤害（含所有段）。"""

    def test_multi_hit_dph_sums_all_hits(self):
        op = operator(atk=1000.0, interval=1.0)
        sk = skill(hits_override=3.0, atk_mult=1.45)
        result = solve_skill(op, sk, enemy(defense=0.0), rules=RULES)
        # 技能期攻击力 1000 × 1.45 = 1450；三段的 DPH = 4350
        self.assertAlmostEqual(result.dph, 4350.0)
        self.assertAlmostEqual(result.dph_per_hit, 1450.0)

    def test_single_hit_dph_equals_per_hit(self):
        op = operator(atk=1000.0, interval=1.0)
        sk = skill(atk_pct=50.0)
        result = solve_skill(op, sk, enemy(defense=0.0), rules=RULES)
        self.assertAlmostEqual(result.dph, result.dph_per_hit)
        self.assertAlmostEqual(result.dph, 1500.0)

    def test_normal_row_dph(self):
        op = operator(atk=1000.0, interval=1.0)
        result = solve_skill(op, None, enemy(defense=300.0), rules=RULES)
        self.assertAlmostEqual(result.dph, 700.0)
        self.assertTrue(result.is_normal_row)


class TestNoAttackSkills(unittest.TestCase):
    """「停止攻击」「技能未开启时无法普通攻击」这类要让 DPS 归零。"""

    def test_dps_is_zero_when_attacks_disabled(self):
        op = operator(atk=1000.0, interval=1.0)
        sk = skill(attacks=False, duration=10.0, sp_cost=10.0)
        result = solve_skill(op, sk, enemy(defense=0.0), rules=RULES)
        self.assertAlmostEqual(result.dps_skill, 0.0)

    def test_normal_attacks_still_count(self):
        op = operator(atk=1000.0, interval=1.0)
        sk = skill(attacks=False, duration=10.0, sp_cost=10.0)
        result = solve_skill(op, sk, enemy(defense=0.0), rules=RULES)
        self.assertAlmostEqual(result.dps_normal, 1000.0)


class TestTimeToKill(unittest.TestCase):
    def test_ttk_computed_from_cycle_dps(self):
        op = operator(atk=1000.0, interval=1.0)
        sk = skill(atk_pct=100.0, sp_cost=30.0, duration=10.0)
        # 循环 DPS 1250，生命 10000 → 8 秒
        result = solve_skill(op, sk, enemy(hp=10000.0), rules=RULES)
        self.assertAlmostEqual(result.ttk, 8.0)

    def test_infinite_hp_gives_infinite_ttk(self):
        op = operator()
        result = solve_skill(op, skill(), enemy(), rules=RULES)
        self.assertEqual(result.ttk, math.inf)


class TestTargetScopeWarning(unittest.TestCase):
    def test_warns_when_target_count_unknown(self):
        op = operator()
        sk = skill(targets_scope="all_blocked")
        result = solve_skill(op, sk, enemy(), rules=RULES)
        self.assertTrue(any("目标数" in n for n in result.notes))

    def test_no_warning_when_targets_given(self):
        op = operator()
        sk = skill(targets_scope="all_blocked")
        result = solve_skill(op, sk, enemy(), rules=RULES, target_count=3)
        self.assertFalse(any("未指定 --targets" in n for n in result.notes))


class TestSolveOperator(unittest.TestCase):
    def test_includes_normal_row_plus_all_skills(self):
        op = operator(skills=[
            skill("技能A", sp_cost=10.0),
            skill("技能B", sp_cost=20.0),
        ])
        results = solve_operator(op, enemy(), rules=RULES)
        self.assertEqual(len(results), 3, "一行常态 + 两个技能")
        self.assertTrue(results[0].is_normal_row)
        self.assertEqual({r.skill for r in results[1:]}, {"技能A", "技能B"})

    def test_operator_without_skills(self):
        op = operator(skills=[])
        results = solve_operator(op, enemy(), rules=RULES)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].is_normal_row)


class TestDamageTypePropagation(unittest.TestCase):
    def test_arts_operator(self):
        op = operator(atk=1000.0, damage_type=DamageType.ARTS)
        result = solve_skill(op, skill(), enemy(res=50.0), rules=RULES)
        self.assertEqual(result.damage_type, DamageType.ARTS)
        # 1000 × (1 − 50/100) = 500
        self.assertAlmostEqual(result.dph, 500.0)

    def test_skill_overrides_type(self):
        op = operator(atk=1000.0, damage_type=DamageType.PHYSICAL)
        sk = skill(damage_type=DamageType.TRUE)
        result = solve_skill(op, sk, enemy(defense=9999.0), rules=RULES)
        self.assertEqual(result.damage_type, DamageType.TRUE)
        self.assertAlmostEqual(result.dph, 1000.0)


if __name__ == "__main__":
    unittest.main()
