"""数据载入：把 JSON 草稿变成引擎对象。

草稿里有两类内容：

  * **引擎字段** —— ``effects`` / ``atk`` / ``interval`` 这些，引擎直接用
  * **审计字段** —— 以下划线开头的 ``_source`` / ``_parsed`` / ``_unparsed`` /
    ``_conditional`` 等，是给人看的，引擎不读

载入时会**检查未解决的问题**：如果某个技能有"可能影响 DPS 但没解析出来"
的片段，载入仍然成功，但会在 :attr:`Operator.note` 里留一条记录，
并且 :meth:`Skill.needs_review` 为 True。调用方可以据此决定要不要提示用户。

这条设计的用意：**引擎不应该假装数据是完整的。**
"""

from __future__ import annotations

import json
from pathlib import Path

from .model import ChargeType, DamageType, Effects, Enemy, Operator, Skill

__all__ = [
    "effects_from_dict",
    "skill_from_dict",
    "operator_from_dict",
    "load_operator",
    "load_operators",
    "enemy_from_dict",
]

#: :class:`Effects` 认识的字段名。草稿里其余的键一律忽略（它们以下划线开头，
#: 或者是引擎不认识的扩展字段）。
_EFFECT_FIELDS = frozenset({
    "atk_pct", "atk_mult", "aspd", "interval_mult", "interval_flat",
    "hits_mult", "hits_add", "hits_override", "targets", "targets_scope",
    "damage_type", "def_ignore_pct", "def_ignore", "res_ignore_pct",
    "res_ignore", "bonus_arts_pct", "bonus_true_pct", "dot_pct", "attacks",
})


def effects_from_dict(data: dict | None) -> Effects:
    """从字典构造 :class:`Effects`，忽略不认识的键。"""
    if not data:
        return Effects()

    kwargs: dict[str, object] = {}
    for key, value in data.items():
        if key not in _EFFECT_FIELDS or value is None:
            continue
        if key == "damage_type":
            kwargs[key] = DamageType(value)
        elif key == "hits_override":
            kwargs[key] = float(value)
        elif key == "targets":
            kwargs[key] = int(value)
        elif key == "targets_scope":
            kwargs[key] = str(value)
        elif key == "attacks":
            kwargs[key] = bool(value)
        else:
            kwargs[key] = float(value)
    return Effects(**kwargs)  # type: ignore[arg-type]


def skill_from_dict(data: dict) -> Skill:
    """从字典构造 :class:`Skill`。"""
    duration = float(data.get("duration") or 0.0)
    infinite = bool(data.get("infinite_duration")) or duration == float("inf")

    # ⚠️ 不能写成 ``str(data.get("charge") or "auto")``。
    # ``class ChargeType(str, Enum)`` 在 Python 3.12 上 ``str()`` 出来的是
    # ``"ChargeType.HIT"`` 而不是 ``"hit"``，于是 ``ChargeType(...)`` 抛
    # ValueError 被下面的兜底吞掉——**受击回复被静默当成自动回复**。
    # 传枚举成员本来是最自然的写法，结果反而坏掉。
    charge_raw = data.get("charge")
    if isinstance(charge_raw, ChargeType):
        charge = charge_raw
    else:
        try:
            charge = ChargeType(str(charge_raw or "auto"))
        except ValueError:
            charge = ChargeType.AUTO

    return Skill(
        name=str(data.get("name") or "未命名技能"),
        charge=charge,
        sp_cost=float(data.get("sp_cost") or 0.0),
        init_sp=float(data.get("init_sp") or 0.0),
        duration=float("inf") if infinite else duration,
        infinite_duration=infinite,
        stance=bool(data.get("stance")),
        effects=effects_from_dict(data.get("effects")),
        source=str(data.get("_source") or ""),
        confidence=str(data.get("_confidence") or "exact"),
        unparsed=list(data.get("_unparsed") or []),
        note=str(data.get("note") or ""),
    )


def operator_from_dict(data: dict) -> Operator:
    """从字典构造 :class:`Operator`。

    载入时会顺手把"需要复核"的技能数记到 ``note`` 里——这样调用方
    只要看一眼就能知道这份数据可不可信。
    """
    damage_type_raw = data.get("damage_type")
    damage_type = DamageType(damage_type_raw) if damage_type_raw else DamageType.PHYSICAL

    skills = [skill_from_dict(item) for item in (data.get("skills") or [])]
    review_count = sum(1 for s in skills if s.needs_review)

    note_parts: list[str] = []

    # 导入器记下的警告必须**传下去**。
    # 「没有取到精英2_满级_攻击」这类草稿加载后 base_atk 是 0，DPS 会算成 0；
    # 允许这种情况存在（页面结构确实会变），但绝不允许它悄无声息——
    # 导入器专门记了 _warnings，在载入时丢掉就等于白记。
    for warning in (data.get("_warnings") or []):
        note_parts.append(str(warning))

    if review_count:
        note_parts.append(
            f"{review_count}/{len(skills)} 个技能含「可能影响 DPS 但未解析」的片段，"
            "结果可能偏离，用 --review 查看清单"
        )
    if data.get("_trait_needs_review"):
        note_parts.append("特性含未解析片段，结果可能偏离")
    if data.get("_trait_conditional"):
        note_parts.append(
            f"特性有 {len(data['_trait_conditional'])} 项条件效果**未计入**"
            "（随战场情况变化）"
        )

    return Operator(
        name=str(data.get("name") or "未命名干员"),
        atk=float(data.get("atk") or 0.0),
        interval=float(data.get("attack_interval") or 1.0),
        class_name=str(data.get("class") or ""),
        branch=str(data.get("branch") or ""),
        rarity=data.get("rarity"),
        atk_trust=float(data.get("atk_trust") or 0.0),
        atk_potential=float(data.get("atk_potential") or 0.0),
        trait=effects_from_dict(data.get("trait")),
        talent=effects_from_dict(data.get("talent")),
        damage_type=damage_type,
        hits=float(data.get("hits") or 1.0),
        targets=int(data.get("targets") or 1),
        skills=skills,
        trait_text=str(data.get("trait_text") or ""),
        source=str((data.get("_source") or {}).get("page") or ""),
        note="；".join(note_parts),
    )


def enemy_from_dict(data: dict) -> Enemy:
    """从字典构造 :class:`Enemy`。"""
    hp = data.get("hp")

    # ``defense`` 优先于别名 ``def``，但**不能**写成
    # ``data.get("defense") or data.get("def")``：防御力 0 是合法值
    # （无防御的敌人），而 0.0 是 falsy，会被别名顶掉。
    raw_defense = data.get("defense")
    if raw_defense is None:
        raw_defense = data.get("def")

    return Enemy(
        name=str(data.get("name") or "测试目标"),
        defense=float(raw_defense or 0.0),
        res=float(data.get("res") or 0.0),
        hp=float(hp) if hp is not None else float("inf"),
        is_elite=bool(data.get("is_elite")),
    )


# --------------------------------------------------------------------------
# 文件
# --------------------------------------------------------------------------


def load_operator(path: str | Path) -> Operator:
    """从一个 JSON 文件载入干员。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return operator_from_dict(data)


def load_operators(directory: str | Path) -> list[Operator]:
    """载入一个目录下所有干员，按名字排序。"""
    directory = Path(directory)
    operators: list[Operator] = []
    for path in sorted(directory.glob("*.json")):
        try:
            operators.append(load_operator(path))
        except Exception:
            continue
    operators.sort(key=lambda op: op.name)
    return operators
