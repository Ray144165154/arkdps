"""arkdps —— 数据驱动的明日方舟 DPS / DPH 计算引擎。

**引擎里没有干员数值。** 它只知道"一个攻击力 X、攻击间隔 Y、技能参数 Z
的东西"，然后算出它能打多少伤害。干员数据是**输入**，从 JSON 读进来，
不改代码就能加干员。

用法概览::

    from arkdps import Enemy
    from arkdps.loader import load_operator
    from arkdps.rotation import solve_operator

    op = load_operator("data/operators/银灰.json")
    for row in solve_operator(op, Enemy(defense=800)):
        print(row.label, "DPH", row.dph, "循环 DPS", row.dps_cycle)

也可以绕过干员数据，直接问任意参数问题::

    from arkdps import Enemy, Operator, solve_operator   # solve_operator 见 rotation
    op = Operator(name="假设", atk=789, interval=1.3)

数据从哪来？两条路：

  * 自己写 JSON（格式见 ``docs/DATA-FORMAT.md``）
  * 用 :mod:`arkdps.importers.prts` 从 PRTS wiki 导入草稿，
    再把标了"需要复核"的地方补一下
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # 引擎数据模型
    "Operator",
    "Skill",
    "Enemy",
    "Effects",
    "DamageType",
    "ChargeType",
    "CombatState",
    # 游戏规则
    "GameRules",
    "DEFAULT_RULES",
    # 语义解析（导入器与高级用法）
    "analyze",
    "summarize",
    "map_clauses",
    "Recognition",
    "Clause",
    "Triple",
]

from .mapping import map_clauses
from .model import (
    ChargeType,
    CombatState,
    DamageType,
    Effects,
    Enemy,
    Operator,
    Skill,
)
from .recognizer import Clause, Recognition, Triple, analyze, summarize
from .rules import DEFAULT_RULES, GameRules
