"""伤害公式。

三条公式，出处是游戏机制本身（当年从 PRTS 的技能页与实测数据反推核对）::

    物理伤害 = max(攻击力 - 有效防御力, 攻击力 × 最低伤害系数)
    法术伤害 = max(攻击力 × (1 - 有效法抗/100), 攻击力 × 最低伤害系数)
    真实伤害 = 攻击力

    有效防御力 = max(防御力 × (1 - 无视防御%) - 无视防御固定值, 0)
    有效法抗   = 法抗 × (1 - 无视法抗%) - 无视法抗固定值      ← 可以为负

**有效法抗可以为负**，这时 ``1 - 有效法抗/100 > 1``，法术伤害反而高于攻击力。
这是机制而非错误，必须保留。

本模块只做纯计算：输入攻击力、伤害类型、敌人、修正，输出数值。
不碰文件、不碰网络、不依赖时间——所以可以精确测试。
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import DamageType, Effects, Enemy
from .rules import GameRules

__all__ = [
    "effective_defense",
    "effective_res",
    "per_hit_damage",
    "DamageBreakdown",
    "resolve_attack",
]


def effective_defense(defense: float, effects: Effects) -> float:
    """有效防御力：先按比例削减，再减去固定值，最后**不低于 0**。

    注意顺序：百分比作用在原始防御上，固定值在之后扣。
    顺序反了会让高防目标的结果差很多。
    """
    reduced = defense * (1.0 - effects.def_ignore_pct / 100.0)
    return max(reduced - effects.def_ignore, 0.0)


def effective_res(res: float, effects: Effects) -> float:
    """有效法术抗性。

    与防御不同，这里**不做下限截断**——法抗可以被削成负数，
    而负法抗意味着法术伤害**高于**攻击力。这是游戏机制的一部分。
    """
    return res * (1.0 - effects.res_ignore_pct / 100.0) - effects.res_ignore


def per_hit_damage(
    atk: float,
    damage_type: DamageType,
    enemy: Enemy,
    effects: Effects,
    rules: GameRules,
) -> float:
    """单段伤害。

    :param atk: 已经算完所有攻击力加成之后的攻击力
    """
    floor = atk * rules.min_damage_ratio

    if damage_type is DamageType.PHYSICAL:
        defense = effective_defense(enemy.defense, effects)
        return max(atk - defense, floor)

    if damage_type is DamageType.ARTS:
        res = effective_res(enemy.res, effects)
        return max(atk * (1.0 - res / 100.0), floor)

    # 真实伤害：无视防御与法抗
    return atk


@dataclass(slots=True)
class DamageBreakdown:
    """一次攻击的伤害拆解。

    保留中间量而不是只给一个总数，是因为计算器的价值一半在于"能核对"。
    """

    damage_type: DamageType
    atk: float
    hits: float
    targets: int
    effective_defense: float
    effective_res: float
    per_hit: float
    bonus_arts: float
    bonus_true: float

    @property
    def per_attack(self) -> float:
        """**一次攻击**造成的总伤害（含所有段与附加伤害）。

        这就是社区说的 DPH——"打一下能打多少"。
        多段攻击算的是所有段之和；想知道单段多少看 :attr:`per_hit`。
        """
        return self.per_hit * self.hits + self.bonus_arts + self.bonus_true

    @property
    def per_attack_per_target(self) -> float:
        """对单个目标的一次攻击伤害。

        ``per_attack`` 在多目标时是**总计**，这个属性则是"每个目标挨了多少"。
        两者都有用，所以分开给出，由调用方决定展示哪个。
        """
        return self.per_attack


def resolve_attack(
    atk: float,
    damage_type: DamageType,
    hits: float,
    enemy: Enemy,
    effects: Effects,
    rules: GameRules,
) -> DamageBreakdown:
    """结算一次攻击，返回完整的伤害拆解。"""
    single = per_hit_damage(atk, damage_type, enemy, effects, rules)

    bonus_arts = atk * effects.bonus_arts_pct / 100.0 if effects.bonus_arts_pct else 0.0
    if bonus_arts:
        # 附加法术伤害**同样受法抗影响**，这一点很容易漏
        res = effective_res(enemy.res, effects)
        bonus_arts = max(bonus_arts * (1.0 - res / 100.0), 0.0)

    bonus_true = atk * effects.bonus_true_pct / 100.0 if effects.bonus_true_pct else 0.0

    return DamageBreakdown(
        damage_type=damage_type,
        atk=atk,
        hits=hits,
        targets=effects.targets,
        effective_defense=effective_defense(enemy.defense, effects),
        effective_res=effective_res(enemy.res, effects),
        per_hit=single,
        bonus_arts=bonus_arts,
        bonus_true=bonus_true,
    )
