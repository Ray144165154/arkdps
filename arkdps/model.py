"""引擎的数据模型。

刻意遵守的一条纪律：**本模块里不会出现任何干员的名字。**

引擎只知道"一个攻击力 X、攻击间隔 Y、技能参数 Z 的东西"，
然后算出它能打多少伤害。干员数值属于**数据**，从 JSON 读进来。

技能与天赋共用同一套 :class:`Effects` 字段——它们对伤害的影响方式
是一样的（都是"攻击力+X%""攻击间隔×0.7"），差别只在于作用范围：
天赋常驻，技能只在开启期间生效。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "DamageType",
    "ChargeType",
    "Effects",
    "Skill",
    "Operator",
    "Enemy",
    "CombatState",
]


class DamageType(str, Enum):
    """伤害类型。三者的公式完全不同。"""

    PHYSICAL = "physical"
    ARTS = "arts"
    TRUE = "true"

    @property
    def label(self) -> str:
        return {"physical": "物理", "arts": "法术", "true": "真实"}[self.value]


class ChargeType(str, Enum):
    """技力回复方式——决定技能多久能开一次。"""

    AUTO = "auto"        # 自动回复：每秒 +1
    ATTACK = "attack"    # 攻击回复：每次攻击 +1
    HIT = "hit"          # 受击回复：每次受击 +1
    PASSIVE = "passive"  # 被动：不需要技力

    @property
    def label(self) -> str:
        return {"auto": "自动回复", "attack": "攻击回复",
                "hit": "受击回复", "passive": "被动"}[self.value]


@dataclass(slots=True)
class Effects:
    """一次攻击的参数修正集合。

    字段分成三组，便于理解：

    **攻击力**
        ``atk_pct`` 是"攻击力+X%"（与其它百分比加成**加算**）；
        ``atk_mult`` 是"攻击力提高至X%"（**乘算**，会连乘）。

    **攻击节奏**
        ``aspd`` 是攻速数值（加算）；
        ``interval_mult`` 是间隔倍率（乘算）；
        ``interval_flat`` 是间隔的秒数修正（加算）。

    **伤害细节**
        段数、目标数、伤害类型、无视防御/法抗、附加伤害。
    """

    # -- 攻击力 --
    atk_pct: float = 0.0
    atk_mult: float = 1.0

    # -- 攻击节奏 --
    aspd: float = 0.0
    interval_mult: float = 1.0
    interval_flat: float = 0.0

    # -- 段数与目标 --
    hits_mult: float = 1.0
    hits_add: float = 0.0
    hits_override: float | None = None
    targets: int = 1
    #: 目标数为非数值时的范围（例如"阻挡的所有敌人"）。
    #: 具体数量取决于战场，引擎无法自行决定，必须由调用方给。
    targets_scope: str | None = None

    # -- 伤害类型 --
    damage_type: DamageType | None = None

    # -- 防御/法抗穿透 --
    def_ignore_pct: float = 0.0
    def_ignore: float = 0.0
    res_ignore_pct: float = 0.0
    res_ignore: float = 0.0

    # -- 附加伤害 --
    bonus_arts_pct: float = 0.0
    bonus_true_pct: float = 0.0
    dot_pct: float = 0.0

    # -- 是否还能普攻 --
    #: 部分技能（如"解放者"类）在未开启时根本不攻击，
    #: 也有技能会"停止攻击"。这会直接改变 DPS 的计算方式。
    attacks: bool = True

    def is_identity(self) -> bool:
        """是否完全没有修正。"""
        return self == Effects()

    def merged_with(self, other: Effects) -> Effects:
        """把另一组修正叠加进来。

        叠加规则按游戏机制：

          * 百分比类（``atk_pct`` / ``aspd`` / ``interval_flat``）**加算**
          * 倍率类（``atk_mult`` / ``interval_mult`` / ``hits_mult``）**乘算**
          * 覆盖类（``hits_override`` / ``damage_type``）**后者优先**
          * 目标数取**较大者**（实际中目标数只会增加）
          * ``attacks`` 取**与**（任一来源说不能攻击，就是不能）
        """
        return Effects(
            atk_pct=self.atk_pct + other.atk_pct,
            atk_mult=self.atk_mult * other.atk_mult,
            aspd=self.aspd + other.aspd,
            interval_mult=self.interval_mult * other.interval_mult,
            interval_flat=self.interval_flat + other.interval_flat,
            hits_mult=self.hits_mult * other.hits_mult,
            hits_add=self.hits_add + other.hits_add,
            hits_override=(
                other.hits_override
                if other.hits_override is not None
                else self.hits_override
            ),
            targets=max(self.targets, other.targets),
            targets_scope=other.targets_scope or self.targets_scope,
            damage_type=other.damage_type or self.damage_type,
            def_ignore_pct=self.def_ignore_pct + other.def_ignore_pct,
            def_ignore=self.def_ignore + other.def_ignore,
            res_ignore_pct=self.res_ignore_pct + other.res_ignore_pct,
            res_ignore=self.res_ignore + other.res_ignore,
            bonus_arts_pct=self.bonus_arts_pct + other.bonus_arts_pct,
            bonus_true_pct=self.bonus_true_pct + other.bonus_true_pct,
            dot_pct=self.dot_pct + other.dot_pct,
            attacks=self.attacks and other.attacks,
        )


@dataclass(slots=True)
class Skill:
    """一个技能。"""

    name: str
    charge: ChargeType = ChargeType.AUTO
    sp_cost: float = 0.0
    init_sp: float = 0.0
    duration: float = 0.0
    #: 持续时间无限（黄昏、电流翻涌之类）
    infinite_duration: bool = False
    #: 形态切换类技能（开启后进入另一个形态，可一直保持或随时切回）。
    #: 这类技能**不能**用"充能 + 持续"的循环模型去算——它没有"持续秒数"，
    #: 只有"当前处于哪个形态"。引擎会按"开启形态下一直保持"处理并明确标注。
    stance: bool = False
    effects: Effects = field(default_factory=Effects)

    # -- 来源与可信度（由导入器填写，引擎不用） --
    source: str = ""
    confidence: str = "exact"
    unparsed: list[dict] = field(default_factory=list)
    note: str = ""

    @property
    def is_instant(self) -> bool:
        """瞬发技能：「下次攻击的攻击力提高至X%」这类，没有持续时间。"""
        return self.duration <= 0 and not self.infinite_duration and not self.stance

    @property
    def needs_review(self) -> bool:
        """是否有「可能影响 DPS 但没解析出来」的片段。"""
        return any(
            item.get("affects_damage") is not False for item in self.unparsed
        )


@dataclass(slots=True)
class Operator:
    """一个干员。

    ``atk`` 是精英 2 满级的面板攻击力；信赖与潜能加成单独放着，
    因为它们是**加在面板上**的，不是百分比加成。
    """

    name: str
    atk: float
    interval: float

    class_name: str = ""
    branch: str = ""
    rarity: float | None = None

    atk_trust: float = 0.0
    atk_potential: float = 0.0

    #: 特性（常驻）
    trait: Effects = field(default_factory=Effects)
    #: 天赋（常驻，概率类按期望值填写）
    talent: Effects = field(default_factory=Effects)

    damage_type: DamageType = DamageType.PHYSICAL
    hits: float = 1.0
    targets: int = 1

    skills: list[Skill] = field(default_factory=list)

    trait_text: str = ""
    source: str = ""
    note: str = ""

    @property
    def base_atk(self) -> float:
        """面板攻击力（含信赖与潜能，不含任何百分比加成）。"""
        return self.atk + self.atk_trust + self.atk_potential

    def skill(self, name: str) -> Skill | None:
        """按名字找技能。"""
        for skill in self.skills:
            if skill.name == name:
                return skill
        return None

    @property
    def skill_names(self) -> list[str]:
        return [s.name for s in self.skills]


@dataclass(slots=True)
class Enemy:
    """受击目标。"""

    name: str = "测试目标"
    defense: float = 0.0
    res: float = 0.0
    hp: float = float("inf")
    #: 是否为精英/领袖（部分天赋对它生效）
    is_elite: bool = False


@dataclass(slots=True)
class CombatState:
    """「干员 + 技能」合成后的攻击参数——伤害公式的输入。"""

    atk: float
    interval: float
    hits: float
    targets: int
    damage_type: DamageType
    effects: Effects

    @property
    def attacks_per_second(self) -> float:
        return 1.0 / self.interval if self.interval > 0 else 0.0
