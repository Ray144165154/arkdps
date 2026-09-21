"""把「干员 + 技能」合成成一次攻击的实际参数。

顺序很重要::

    攻击力 = (精英2满级攻击 + 信赖 + 潜能)
             × (1 + 攻击力% / 100)          ← 天赋与技能的百分比**加算**
             × 攻击力倍率                    ← 「提高至X%」**乘算**
             × 特性倍率

    攻击间隔 = (基础间隔 + 间隔修正) ÷ (1 + 攻速/100) × 间隔倍率

    段数     = 覆盖值（若有）否则 基础段数 × 段数倍率 + 额外段数

百分比加算、倍率乘算是游戏机制，混了会让结果差很多：
「攻击力+100%」与「攻击力提高至110%」同时存在时，
正确结果是 ``1 + 100%`` 再 ``× 110% = 2.2 倍``，而不是加在一起。
"""

from __future__ import annotations

from .model import CombatState, DamageType, Effects, Operator, Skill
from .rules import DEFAULT_RULES, GameRules

__all__ = ["merge_effects", "build_state", "effective_atk", "effective_interval"]


def merge_effects(
    operator: Operator,
    skill: Skill | None = None,
) -> Effects:
    """合并特性、天赋与技能的修正。

    叠加规则由 :meth:`Effects.merged_with` 定义（百分比加算、倍率乘算）。
    """
    merged = operator.trait.merged_with(operator.talent)
    if skill is not None:
        merged = merged.merged_with(skill.effects)
    return merged


def effective_atk(base_atk: float, effects: Effects) -> float:
    """算最终攻击力。

    注意 ``atk_pct`` 与 ``atk_mult`` 的处理不同——前者加算进括号，
    后者乘在括号外。这是最容易写错的一步。
    """
    return base_atk * (1.0 + effects.atk_pct / 100.0) * effects.atk_mult


def effective_interval(
    base_interval: float,
    effects: Effects,
    rules: GameRules = DEFAULT_RULES,
) -> float:
    """算最终攻击间隔（秒）。"""
    interval = (base_interval + effects.interval_flat) * effects.interval_mult
    if effects.aspd:
        interval = interval / (1.0 + effects.aspd / 100.0)
    # 间隔不可能小于 1 帧
    floor = 1.0 / rules.fps if rules.fps > 0 else 0.0
    return rules.round_interval(max(interval, floor))


def build_state(
    operator: Operator,
    skill: Skill | None = None,
    *,
    rules: GameRules = DEFAULT_RULES,
    target_count: int | None = None,
) -> CombatState:
    """把干员与技能合成成一次攻击的参数。

    :param target_count: 强制指定同时攻击目标数。

        有些技能写的是「同时攻击**阻挡的所有敌人**」而不是一个数字——
        打几个取决于场上有几个敌人挡住了，引擎无从得知。
        这时必须由调用方给出；不给就按单体 1 计算，并在结果里标注。
    """
    effects = merge_effects(operator, skill)

    atk = effective_atk(operator.base_atk, effects)
    interval = effective_interval(operator.interval, effects, rules)

    if effects.hits_override is not None:
        hits = effects.hits_override
    else:
        hits = operator.hits * effects.hits_mult + effects.hits_add
    hits = max(hits, 1.0)

    if target_count is not None:
        targets = max(1, int(target_count))
    else:
        targets = max(1, effects.targets)

    damage_type: DamageType = effects.damage_type or operator.damage_type

    return CombatState(
        atk=atk,
        interval=interval,
        hits=hits,
        targets=targets,
        damage_type=damage_type,
        effects=effects,
    )
