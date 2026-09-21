"""映射层：把语义三元组写进引擎字段。

识别器只回答"这句话在说什么属性、什么关系、多少数值"，不关心引擎长什么样。
本模块负责最后一跳::

    (atk,  increase,    200)   →   atk_pct += 200
    (atk,  set_to,      110)   →   atk_mult *= 1.10
    (atk,  proportional,145)   →   atk_mult *= 1.45
    (aspd, increase,     50)   →   aspd += 50
    (interval, decrease, 0.22) →   interval_flat -= 0.22
    (interval, set_to,   70)   →   interval_mult *= 0.70
    (hits, unspecified,   5)   →   hits_override = 5
    (targets, max_of,     6)   →   targets = 6
    (res,  ignore,       20)   →   res_ignore += 20
    (defense, ignore,    30)   →   def_ignore_pct += 30   （带 % 时）

两条重要的设计决定：

**① 认出来但映射不了，也要报出来。**
``(duration, unspecified, 10)`` 映射到的是技能级字段而不是效果字段；
``(targets, unspecified, None)``（"阻挡的所有敌人"）需要战场信息才能定数量。
这类子句既不算"识别失败"，也不算"已应用"——它们进 ``unresolved``，
在草稿里和未识别片段并列，因为它们**同样需要人看一眼**。

**② 数值的符号由关系决定，不由字面决定。**
``攻击间隔缩短(-0.22)`` 里的 -0.22 已经带了负号，而 ``攻击间隔缩短0.22``
没带。两种写法都要归到"减少 0.22 秒"。所以要按关系归一化符号，
不能直接相加。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .lexicon import DAMAGE_TYPE_WORDS
from .model import DamageType
from .recognizer import Clause, Recognition, Triple

__all__ = [
    "MappingResult",
    "map_clause",
    "map_clauses",
    "map_clauses_detailed",
    "merge_fields",
    "SKILL_LEVEL_FIELDS",
]

#: 这些键不属于 :class:`~arkdps.model.Effects`，而是技能级字段。
#: 调用方按这张表分流。
SKILL_LEVEL_FIELDS: frozenset[str] = frozenset({
    "duration", "infinite_duration", "attacks", "stance",
    "end_condition", "overrides_trait",
})


@dataclass(slots=True)
class MappingResult:
    """一段描述映射成引擎字段的结果。"""

    #: 引擎字段（可能同时含 Effects 字段与技能级字段，用 SKILL_LEVEL_FIELDS 分流）
    fields: dict[str, object] = field(default_factory=dict)
    #: **带条件**的效果字段。默认**不**参与计算，必须由人确认后再启用。
    conditional: dict[str, object] = field(default_factory=dict)
    #: 逐条审计：每句话变成了哪个字段、值是多少
    records: list[dict] = field(default_factory=list)
    #: 认出来了但没能落到任何字段上的子句
    unresolved: list[dict] = field(default_factory=list)
    #: 被判定为有条件而没应用的效果（原文 + 命中的条件标记）
    conditions: list[dict] = field(default_factory=list)

    @property
    def effects_fields(self) -> dict[str, object]:
        return {k: v for k, v in self.fields.items() if k not in SKILL_LEVEL_FIELDS}

    @property
    def skill_fields(self) -> dict[str, object]:
        return {k: v for k, v in self.fields.items() if k in SKILL_LEVEL_FIELDS}

    @property
    def has_unresolved_risk(self) -> bool:
        return any(item.get("affects_damage") is not False for item in self.unresolved)


# --------------------------------------------------------------------------
# 单个三元组 → 引擎字段
# --------------------------------------------------------------------------


def _scope_from_text(text: str) -> str | None:
    """识别「目标数为非数值」时的范围说法。"""
    for keyword, scope in (
        ("阻挡的所有敌人", "all_blocked"),
        ("阻挡的所有", "all_blocked"),
        ("阻挡的敌人", "all_blocked"),
        ("范围内的目标", "all_in_range"),
        ("范围内的所有", "all_in_range"),
        ("整个战场", "all_in_range"),
        ("所有敌人", "all_in_range"),
        ("全部敌人", "all_in_range"),
    ):
        if keyword in text:
            return scope
    return None


def _damage_type_from_text(text: str) -> DamageType | None:
    """从文本里取伤害类型。

    注意「造成法术伤害」与「伤害类型变为法术」都要认出来，
    而且**不能**把「造成法术伤害」误判成"技能描述里出现了法术二字"
    ——必须真的在说伤害类型。
    """
    if "伤害类型" not in text and "造成" not in text and "变为" not in text:
        return None
    for word, canonical in DAMAGE_TYPE_WORDS.items():
        if word in text:
            return DamageType(canonical)
    return None


def _ignore_amount(triple: Triple) -> tuple[float, bool]:
    """区分「无视 N 点」与「无视 N%」。返回 ``(数值, 是否为百分比)``。"""
    value = triple.value or 0.0
    return value, triple.unit in ("%", "％")


def map_clause(clause: Clause) -> MappingResult:
    """把单个子句映射成引擎字段。"""
    result = MappingResult()

    if clause.kind is not Recognition.RECOGNIZED or clause.triple is None:
        if clause.kind is Recognition.UNKNOWN:
            result.unresolved.append({
                "text": clause.text,
                "kind": "unknown",
                "affects_damage": clause.affects_damage,
                "reason": clause.reason,
            })
        return result

    triple = clause.triple
    attr, rel, value = triple.attribute, triple.relation, triple.value
    fields = result.fields

    # ---- 归属检查：作用于**敌人**的效果不能套到干员身上 ----
    #
    # 「诅咒娃娃周围**敌人的**攻击力和防御力-50%」说的是把敌人的攻击力
    # 打下去，和干员自己的输出无关。按"自己攻击力-50%"套用会让该干员的
    # DPS 直接砍半——而且不会有任何报错。
    #
    # 引擎目前只描述"干员自己"的修正，无法表达"敌人的属性被改变"，
    # 所以这里既不应用、也不丢弃，而是明确报告出来让人决定
    # （例如把敌人的防御力手动调低）。
    if clause.owner == "enemy":
        result.unresolved.append({
            "text": triple.text,
            "kind": "enemy_effect",
            "affects_damage": True,
            "reason": (
                f"原文说的是**敌人**的「{attr}」发生了变化，"
                "不是干员自己——不能套用到干员身上，需人工确认"
            ),
        })
        return result

    applied: str | None = None

    # ---- 攻击力 ----
    if attr == "atk":
        if rel == "stop":
            # 「停止攻击」「技能未开启时无法普通攻击」
            fields["attacks"] = False
            applied = "attacks"
        elif value is not None and rel == "increase":
            # 符号由**关系**决定，不由字面决定（见模块开头 ②）。
            # 不加 abs 会把「攻击力-30%」算成 +30%——减攻变加攻。
            fields["atk_pct"] = fields.get("atk_pct", 0.0) + abs(value)
            applied = "atk_pct"
        elif value is not None and rel == "decrease":
            fields["atk_pct"] = fields.get("atk_pct", 0.0) - abs(value)
            applied = "atk_pct"
        elif value is not None and rel in ("set_to", "proportional", "unspecified"):
            fields["atk_mult"] = fields.get("atk_mult", 1.0) * (value / 100.0)
            applied = "atk_mult"

    elif attr == "per_hit_mult":
        if value is not None:
            fields["atk_mult"] = fields.get("atk_mult", 1.0) * (value / 100.0)
            applied = "atk_mult"

    # ---- 攻击节奏 ----
    elif attr == "aspd":
        if value is not None:
            # 同样按关系归一化符号：「攻击速度-20」的负号已经在数值里了，
            # 再取一次负会变成加速。
            if rel == "decrease":
                delta = -abs(value)
            elif rel == "increase":
                delta = abs(value)
            else:
                delta = value
            fields["aspd"] = fields.get("aspd", 0.0) + delta
            applied = "aspd"

    elif attr == "interval":
        # 「缩短(*0.2)」里的星号表示这是**倍率**而不是秒数。
        # 当成秒数算会得到"间隔 −0.2 秒"——数值看起来还算合理，
        # 但实际应该是"间隔 × 0.2"，两者差了 5 倍。
        star_multiplier = re.search(r"\(\s*\*\s*([\d.]+)\s*\)", triple.text)
        if star_multiplier is not None:
            fields["interval_mult"] = (
                fields.get("interval_mult", 1.0) * float(star_multiplier.group(1))
            )
            applied = "interval_mult"
        elif value is not None and rel == "set_to":
            fields["interval_mult"] = fields.get("interval_mult", 1.0) * (value / 100.0)
            applied = "interval_mult"
        elif value is not None and rel == "decrease":
            # 按关系归一化符号：无论字面是否带负号，都归到"减少"
            fields["interval_flat"] = fields.get("interval_flat", 0.0) - abs(value)
            applied = "interval_flat"
        elif value is not None and rel == "increase":
            fields["interval_flat"] = fields.get("interval_flat", 0.0) + abs(value)
            applied = "interval_flat"

    # ---- 段数与目标 ----
    elif attr == "hits":
        if value is not None:
            fields["hits_override"] = value
            applied = "hits_override"

    elif attr == "targets":
        if value is not None:
            fields["targets"] = max(int(fields.get("targets", 1)), int(value))
            applied = "targets"
        else:
            scope = _scope_from_text(triple.text)
            if scope:
                fields["targets_scope"] = scope
                applied = "targets_scope"

    # ---- 范围说法被误当成属性时的补救 ----
    elif attr == "range":
        # 「攻击范围内的所有敌人」这类子句：属性被更长的「攻击范围」抢走了
        # （`find_attribute` 是最长词优先，4 字的「攻击范围」胜过 2 字的
        # 「敌人」），但它真正在说的是**目标数范围**。
        #
        # 真正的范围表述（「攻击范围扩大」）在识别层就被判成 IRRELEVANT 了，
        # 根本走不到这里，所以这个分支不会误伤它们。
        scope = _scope_from_text(triple.text)
        if scope:
            fields["targets_scope"] = scope
            applied = "targets_scope"

    # ---- 伤害类型 ----
    elif attr == "damage_type":
        damage_type = _damage_type_from_text(triple.text)
        if damage_type is not None:
            fields["damage_type"] = damage_type
            applied = "damage_type"

    # ---- 穿透 ----
    elif attr == "res":
        if rel == "ignore" and value is not None:
            amount, is_pct = _ignore_amount(triple)
            key = "res_ignore_pct" if is_pct else "res_ignore"
            fields[key] = fields.get(key, 0.0) + amount
            applied = key

    elif attr == "defense":
        if rel == "ignore" and value is not None:
            amount, is_pct = _ignore_amount(triple)
            key = "def_ignore_pct" if is_pct else "def_ignore"
            fields[key] = fields.get(key, 0.0) + amount
            applied = key

    # ---- 持续时间（技能级字段）----
    elif attr == "duration":
        if value == float("inf"):
            fields["infinite_duration"] = True
            applied = "infinite_duration"
        elif value is not None:
            fields["duration"] = value
            applied = "duration"

    # ---- 形态切换：标记出来，让引擎拒绝用普通循环模型算它 ----
    elif attr == "stance":
        fields["stance"] = True
        applied = "stance"

    # ---- 技能终止方式：影响循环，但要落到字段上，否则会被当成"没解析出来" ----
    elif attr == "termination":
        fields["end_condition"] = triple.text
        applied = "end_condition"

    # ---- 特性覆盖：例如「远程攻击不再降低攻击力」会把特性的倍率取消 ----
    elif attr == "trait":
        fields["overrides_trait"] = True
        applied = "overrides_trait"

    if applied is not None:
        result.records.append({
            "text": triple.text,
            "attribute": attr,
            "relation": rel,
            "value": value,
            "field": applied,
        })
    else:
        # 认出了属性，但没能落到字段上 —— 同样需要人看一眼
        result.unresolved.append({
            "text": triple.text,
            "kind": "unmapped",
            "affects_damage": clause.affects_damage,
            "reason": f"识别为「{attr} / {rel}」，但没能映射到引擎字段",
        })

    # ---- 带条件的效果：不参与默认计算 ----
    #
    # 「可以进行远程攻击，**但此时**攻击力降低至80%」——这个 80% 只在远程攻击时
    # 生效。如果当成常驻效果套用，算出来的 DPS 会**恒定偏低 20%**，
    # 而且从结果上看不出哪里错了。
    #
    # 所以把这类效果单独放到 conditional 桶里，并在 conditions 里留下原文，
    # 由人决定要不要按某种假设启用。
    if clause.is_conditional and result.fields:
        result.conditional = dict(result.fields)
        result.fields = {}
        result.conditions.append({
            "text": clause.text,
            "condition": clause.condition,
            "fields": result.conditional,
            "reason": f"含条件标记「{clause.condition}」，是否生效取决于战场情况",
        })

    return result


# --------------------------------------------------------------------------
# 整段
# --------------------------------------------------------------------------


def merge_fields(target: dict, source: dict) -> None:
    """按游戏机制把 ``source`` 里的字段并进 ``target``。

    单独抽出来是因为它不止用在一处：一段描述内部要合并，
    **多个天赋之间**也要合并（一个干员可能有两个天赋）。
    """
    for key, value in source.items():
        if key in ("atk_pct", "aspd", "interval_flat", "hits_add",
                   "def_ignore_pct", "def_ignore", "res_ignore_pct",
                   "res_ignore", "bonus_arts_pct", "bonus_true_pct", "dot_pct"):
            target[key] = target.get(key, 0.0) + value
        elif key in ("atk_mult", "interval_mult", "hits_mult"):
            target[key] = target.get(key, 1.0) * value
        elif key == "targets":
            target[key] = max(int(target.get(key, 1)), int(value))
        else:
            # 覆盖类：hits_override / damage_type / targets_scope /
            # attacks / duration / infinite_duration / stance
            target[key] = value


def map_clauses(clauses: list[Clause]) -> tuple[dict, list[dict], list[dict]]:
    """把一段描述的所有子句映射成引擎字段。

    返回 ``(fields, records, unresolved)``。

    数值累加规则：同一段描述里对同一字段的多次修改会合并
    （``atk_pct`` 相加、``atk_mult`` 连乘），因为技能描述里
    "攻击力+100%，攻击力提高至110%"确实是两件事同时发生。

    带条件的子句**不进 fields**，而是进 :attr:`MappingResult.conditional`。
    需要拿到它们请用 :func:`map_clauses_detailed`。
    """
    detailed = map_clauses_detailed(clauses)
    return detailed.fields, detailed.records, detailed.unresolved


def map_clauses_detailed(clauses: list[Clause]) -> MappingResult:
    """与 :func:`map_clauses` 相同，但把条件效果与条件说明一并返回。"""
    merged = MappingResult()

    for clause in clauses:
        one = map_clause(clause)

        merge_fields(merged.fields, one.fields)

        # 条件字段单独累计，绝不混进上面那套
        for key, value in one.conditional.items():
            merged.conditional[key] = value

        merged.records.extend(one.records)
        merged.unresolved.extend(one.unresolved)
        merged.conditions.extend(one.conditions)

    # 只保留真正改动过的字段，让草稿干净
    merged.fields = {
        key: value for key, value in merged.fields.items()
        if not _is_neutral(key, value)
    }
    merged.conditional = {
        key: value for key, value in merged.conditional.items()
        if not _is_neutral(key, value)
    }
    return merged


def _is_neutral(key: str, value: object) -> bool:
    """判断一个字段是否等于默认值（等于就不用写进草稿）。"""
    defaults: dict[str, object] = {
        "atk_pct": 0.0, "atk_mult": 1.0, "aspd": 0.0,
        "interval_mult": 1.0, "interval_flat": 0.0,
        "hits_mult": 1.0, "hits_add": 0.0, "targets": 1,
        "def_ignore_pct": 0.0, "def_ignore": 0.0,
        "res_ignore_pct": 0.0, "res_ignore": 0.0,
        "bonus_arts_pct": 0.0, "bonus_true_pct": 0.0, "dot_pct": 0.0,
        "attacks": True, "infinite_duration": False,
    }
    return key in defaults and value == defaults[key]
