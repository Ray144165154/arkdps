"""技能循环求解：从"一次攻击的参数"到 DPS / DPH / 覆盖率。

这是把静态数值变成"实战强度"的一步。核心是**稳态循环**模型：

    循环 = 充能（攒技力） + 技能持续
    循环 DPS = (技能期 DPS × 持续 + 常态 DPS × 充能时间) ÷ 循环时长

三种技能形态要分别处理，混在一起算会得到荒谬的结果：

**① 持续型**（大多数技能）
    正常的"充能 → 开启 → 持续 → 再充能"循环。

**② 瞬发型**（``duration == 0``，如"下次攻击的攻击力提高至290%"）
    技能只强化**一次攻击**。所以循环是"N 次攻击中一次被强化"，
    其中 N = 技力消耗。把它按"持续 0 秒的技能"算会得出无穷大 DPS。

**③ 无限持续**（黄昏、电流翻涌之类）
    技能开了就不会关，循环时长记为无限，循环 DPS 取技能期 DPS。
    注意这类技能通常有代价（如持续掉血），但那影响生存不影响输出。

**阻回**：技能持续期间技力停止回复。所以充能时间是**整段**的
``技力消耗 ÷ 回复速度``，而不是"循环里平摊"。这一点写错了会让
短持续技能被严重高估。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .combat import build_state
from .damage import DamageBreakdown, resolve_attack
from .model import ChargeType, CombatState, DamageType, Enemy, Operator, Skill
from .rules import DEFAULT_RULES, GameRules

__all__ = ["RotationResult", "solve_skill", "solve_operator"]


@dataclass(slots=True)
class RotationResult:
    """一个「干员 × 技能 × 敌人」的完整结算结果。"""

    operator: str
    skill: str

    damage_type: DamageType
    targets: int

    # -- 常态 --
    atk_normal: float
    interval_normal: float
    attack_normal: DamageBreakdown
    dps_normal: float

    # -- 技能期 --
    atk_skill: float
    interval_skill: float
    attack_skill: DamageBreakdown
    dps_skill: float

    # -- 循环 --
    dps_cycle: float             # 循环平均 DPS（单目标）
    dps_cycle_total: float       # 循环平均 DPS（含目标数）
    uptime: float                # 技能覆盖率
    active_time: float           # 技能持续
    charge_time: float           # 充能时间
    cycle_time: float            # 一个完整循环的时长
    damage_cycle: float          # 一个循环造成的总伤害（单目标）

    # -- 其他 --
    ttk: float = math.inf        # 击杀耗时（秒）；未设生命值时为正无穷
    is_normal_row: bool = False  # 这是"不使用技能"的对照行
    sp_per_second: float = 0.0   # 实际技力回复速度
    notes: list[str] = field(default_factory=list)

    @property
    def dph(self) -> float:
        """**DPH**：一次攻击打出的伤害（含所有段与附加伤害）。

        多段攻击算的是所有段之和——因为"打一下"在实战里就是整个动作。
        想知道单段多少看 :attr:`dph_per_hit`。
        """
        breakdown = self.attack_normal if self.is_normal_row else self.attack_skill
        return breakdown.per_attack

    @property
    def dph_per_hit(self) -> float:
        """单段伤害。多段攻击时与 :attr:`dph` 差一个段数。"""
        breakdown = self.attack_normal if self.is_normal_row else self.attack_skill
        return breakdown.per_hit

    @property
    def atk(self) -> float:
        return self.atk_normal if self.is_normal_row else self.atk_skill

    @property
    def interval(self) -> float:
        return self.interval_normal if self.is_normal_row else self.interval_skill

    @property
    def label(self) -> str:
        return "（常态）" if self.is_normal_row else self.skill


def _sp_per_second(
    skill: Skill,
    state_normal: CombatState,
    *,
    rules: GameRules,
    extra_regen: float = 0.0,
    hits_taken_per_second: float = 1.0,
) -> tuple[float, str]:
    """技能期间的技力回复速度（点/秒），以及一句说明。"""
    if skill.charge is ChargeType.AUTO:
        rate = rules.auto_sp_per_second + extra_regen
        return rate, f"自动回复 {rate:g}/秒"
    if skill.charge is ChargeType.ATTACK:
        rate = state_normal.attacks_per_second + extra_regen
        return rate, f"攻击回复，常态 {state_normal.attacks_per_second:.2f} 次/秒"
    if skill.charge is ChargeType.HIT:
        rate = hits_taken_per_second + extra_regen
        return rate, f"受击回复，假设 {hits_taken_per_second:g} 次/秒"
    return 0.0, "被动，不需要技力"


def _dps(state: CombatState, breakdown: DamageBreakdown) -> float:
    """由一次攻击的伤害与间隔算出 DPS。

    ``breakdown.per_attack`` 已经包含了所有段与附加伤害，
    所以这里是"每次攻击的伤害 × 每秒攻击次数"。
    """
    if state.interval <= 0:
        return 0.0
    if not state.effects.attacks:
        return 0.0
    return breakdown.per_attack / state.interval


def solve_skill(
    operator: Operator,
    skill: Skill | None,
    enemy: Enemy,
    *,
    rules: GameRules = DEFAULT_RULES,
    target_count: int | None = None,
    extra_sp_regen: float = 0.0,
    hits_taken_per_second: float = 1.0,
    use_initial_sp: bool = False,
) -> RotationResult:
    """求解一个「干员 × 技能」的完整强度。

    :param skill: ``None`` 表示"不使用技能"，只算常态。
    :param extra_sp_regen: 队友/道具提供的额外技力回复（点/秒）。
    :param use_initial_sp: 按"首次开技能"计算（计入初始技力）。
    """
    notes: list[str] = []

    state_normal = build_state(operator, None, rules=rules, target_count=target_count)
    state_skill = (
        build_state(operator, skill, rules=rules, target_count=target_count)
        if skill is not None
        else state_normal
    )

    attack_normal = resolve_attack(
        state_normal.atk, state_normal.damage_type, state_normal.hits,
        enemy, state_normal.effects, rules,
    )
    attack_skill = (
        resolve_attack(
            state_skill.atk, state_skill.damage_type, state_skill.hits,
            enemy, state_skill.effects, rules,
        )
        if skill is not None
        else attack_normal
    )

    dps_normal = _dps(state_normal, attack_normal)
    dps_skill = _dps(state_skill, attack_skill)

    # 目标数为非数值时（"阻挡的所有敌人"），能说的只有"按单体算"
    if state_skill.effects.targets_scope and target_count is None:
        notes.append(
            "该技能的目标数是「"
            + {"all_blocked": "阻挡的所有敌人", "all_in_range": "范围内的敌人"}
            .get(state_skill.effects.targets_scope, "非固定数量")
            + "」，未指定 --targets，已按单体 1 计算"
        )

    if skill is None:
        return RotationResult(
            operator=operator.name, skill="", damage_type=state_normal.damage_type,
            targets=state_normal.targets,
            atk_normal=state_normal.atk, interval_normal=state_normal.interval,
            attack_normal=attack_normal, dps_normal=dps_normal,
            atk_skill=state_skill.atk, interval_skill=state_skill.interval,
            attack_skill=attack_skill, dps_skill=dps_skill,
            dps_cycle=dps_normal, dps_cycle_total=dps_normal * state_normal.targets,
            uptime=0.0, active_time=0.0, charge_time=0.0, cycle_time=0.0,
            damage_cycle=0.0, is_normal_row=True, notes=notes,
        )

    sp_rate, sp_note = _sp_per_second(
        skill, state_normal, rules=rules,
        extra_regen=extra_sp_regen, hits_taken_per_second=hits_taken_per_second,
    )

    # ---- 充能时间 ----
    if skill.charge is ChargeType.PASSIVE or skill.sp_cost <= 0:
        charge_time = 0.0
    elif sp_rate <= 0:
        charge_time = math.inf
        notes.append("技力回复速度为 0，这个技能永远开不出来")
    else:
        needed = max(0.0, skill.sp_cost - (skill.init_sp if use_initial_sp else 0.0))
        charge_time = needed / sp_rate

    # ---- 三种技能形态 ----
    if skill.stance:
        # 形态切换：没有"持续多久"的概念，只有"当前在哪个形态"。
        # 按"开启形态下一直保持"处理，并明确告诉使用者这是个假设。
        notes.append(
            "形态切换类技能：已按「开启后一直保持该形态」计算，"
            "实际强度取决于你什么时候切、切多久"
        )
        dps_cycle = dps_skill
        uptime = 1.0
        cycle_time = math.inf
        damage_cycle = math.inf
        active_time = math.inf

    elif skill.infinite_duration:
        notes.append("技能持续时间无限，循环 DPS 取技能期 DPS")
        dps_cycle = dps_skill
        uptime = 1.0
        cycle_time = math.inf
        damage_cycle = math.inf
        active_time = math.inf

    elif skill.is_instant:
        # 瞬发：N 次攻击里有一次被强化，N = 技力消耗
        swings = max(1.0, skill.sp_cost)
        per_attack_normal = attack_normal.per_attack
        per_attack_skill = attack_skill.per_attack
        total = per_attack_normal * (swings - 1) + per_attack_skill
        cycle_time = swings / state_normal.attacks_per_second
        dps_cycle = total / cycle_time if cycle_time > 0 else 0.0
        uptime = 1.0 / swings
        active_time = 0.0
        damage_cycle = total
        notes.append(
            f"瞬发技能：按「{swings:.0f} 次攻击中有 1 次被强化」计算"
        )

    else:
        active_time = skill.duration
        cycle_time = active_time + charge_time
        uptime = active_time / cycle_time if cycle_time > 0 else 0.0
        damage_cycle = dps_skill * active_time + dps_normal * charge_time
        dps_cycle = damage_cycle / cycle_time if cycle_time > 0 else 0.0

    # ---- 击杀耗时 ----
    ttk = math.inf
    if enemy.hp < math.inf and dps_cycle > 0:
        ttk = enemy.hp / (dps_cycle * state_skill.targets)

    if state_skill.targets > 1:
        notes.append(f"同时攻击 {state_skill.targets} 个目标，总 DPS 为上表数值 × {state_skill.targets}")

    return RotationResult(
        operator=operator.name, skill=skill.name,
        damage_type=state_skill.damage_type, targets=state_skill.targets,
        atk_normal=state_normal.atk, interval_normal=state_normal.interval,
        attack_normal=attack_normal, dps_normal=dps_normal,
        atk_skill=state_skill.atk, interval_skill=state_skill.interval,
        attack_skill=attack_skill, dps_skill=dps_skill,
        dps_cycle=dps_cycle, dps_cycle_total=dps_cycle * state_skill.targets,
        uptime=uptime, active_time=active_time, charge_time=charge_time,
        cycle_time=cycle_time, damage_cycle=damage_cycle,
        ttk=ttk, sp_per_second=sp_rate, notes=notes,
    )


def solve_operator(
    operator: Operator,
    enemy: Enemy,
    *,
    rules: GameRules = DEFAULT_RULES,
    **kwargs,
) -> list[RotationResult]:
    """求解一个干员的**所有**技能，外加一行常态对照。"""
    results = [solve_skill(operator, None, enemy, rules=rules, **kwargs)]
    for skill in operator.skills:
        results.append(solve_skill(operator, skill, enemy, rules=rules, **kwargs))
    return results
