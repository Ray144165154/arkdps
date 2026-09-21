"""识别器：把中文技能描述解析成结构化语义。

流程分三步：

    ① 切句        按标点把描述切成子句，但**不切括号内部**
                  （``攻击间隔缩短(-0.22)`` 里的括号是数值的一部分）

    ② 识别        每个子句判定为三态之一：

                  ``RECOGNIZED``  找到「属性 + 关系 + 数值」→ 交给映射层
                  ``IRRELEVANT``  认出是已知的**非 DPS** 概念 → 不报警
                  ``UNKNOWN``     认不出 → 标出来并评估是否可能影响 DPS

    ③ 风险评估    对 ``UNKNOWN`` 的子句判断"它会不会影响打出来的数字"，
                  只有可能有影响的才值得让人去看

第 ② 步里的 ``IRRELEVANT`` 是关键。像「攻击范围扩大」这种子句在一次
全量解析里会出现很多次，如果一律按"未识别"处理，报告会被噪音淹没。
**能认出"这与 DPS 无关"本身就是一种识别能力。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .lexicon import (
    ATTRIBUTES,
    DAMAGE_CRITICAL,
    IRRELEVANT,
    PENETRATION_ATTRIBUTES,
    find_attribute,
    find_irrelevant,
    find_relation,
    parse_number,
)

__all__ = [
    "Recognition",
    "Triple",
    "Clause",
    "split_clauses",
    "analyze_clause",
    "analyze",
    "summarize",
]


class Recognition(str, Enum):
    """一个子句的识别结果。"""

    RECOGNIZED = "recognized"   # 找到语义三元组
    IRRELEVANT = "irrelevant"   # 已知与非伤害计算无关
    UNKNOWN = "unknown"         # 认不出


@dataclass(frozen=True, slots=True)
class Triple:
    """「属性 + 关系 + 数值」——与具体措辞无关的语义表达。"""

    attribute: str
    relation: str
    value: float | None
    unit: str
    text: str

    def __str__(self) -> str:
        value = "?" if self.value is None else f"{self.value:g}{self.unit}"
        return f"{self.attribute} {self.relation} {value}"


@dataclass(frozen=True, slots=True)
class Clause:
    """一个子句的完整分析结果。"""

    text: str
    kind: Recognition
    triple: Triple | None = None
    concept: str | None = None      # IRRELEVANT 时的概念名
    reason: str = ""
    #: 该子句是否**可能**影响算出来的 DPS。
    #: True = 会；False = 不会；None = 不确定
    affects_damage: bool | None = None
    #: 命中的条件标记（"但此时""未被阻挡"…）。
    #: 非 None 表示这个效果**依赖战场情况**，不能无条件套用。
    condition: str | None = None
    #: 这个属性属于谁：``"self"``（干员自己）或 ``"enemy"``（敌人）。
    #: ``"enemy"`` 的效果**不能**套到干员身上——见 :func:`_attribute_owner`。
    owner: str = "self"

    @property
    def is_conditional(self) -> bool:
        return self.condition is not None

    def __str__(self) -> str:
        if self.kind is Recognition.RECOGNIZED and self.triple is not None:
            suffix = f"  [条件: {self.condition}]" if self.condition else ""
            return f"[识别] {self.text}  →  {self.triple}{suffix}"
        if self.kind is Recognition.IRRELEVANT:
            return f"[无关] {self.text}  →  {self.concept}（{self.reason}）"
        risk = {True: "可能影响 DPS", False: "与 DPS 无关",
                None: "影响未知"}[self.affects_damage]
        return f"[未知] {self.text}  →  {risk}"


# --------------------------------------------------------------------------
# ① 切句
# --------------------------------------------------------------------------

_BRACKET_OPEN = "（(【[「"
_BRACKET_CLOSE = "）)】]」"
#: 「且」是真实存在的子句连接词，例如
#: 「（持续10秒），且优先攻击未获得此效果的敌人」。不切开会把两个效果
#: 塞进一个子句，导致「攻击范围扩大且攻击力+50%」里的加成被「攻击范围」吃掉。
#: 「并」没有加进来：它出现的场合（「并连续攻击三次」）本来就能正确解析，
#: 而它确实是「合并」「并排」等词的组成部分，切了有风险。
_SPLIT_CHARS = "，,；;。\n、且"


def split_clauses(text: str) -> list[str]:
    """按标点切分子句，**但不切括号内部**。

    例子::

        "攻击间隔缩短(-0.22)，攻击力提升至110%"
        → ["攻击间隔缩短(-0.22)", "攻击力提升至110%"]

    括号内的逗号不会被当作分隔符，否则数值会被切碎。
    """
    clauses: list[str] = []
    buf: list[str] = []
    depth = 0

    for ch in text:
        if ch in _BRACKET_OPEN:
            depth += 1
        elif ch in _BRACKET_CLOSE:
            depth = max(0, depth - 1)

        if depth == 0 and ch in _SPLIT_CHARS:
            piece = "".join(buf).strip()
            if piece:
                clauses.append(piece)
            buf = []
            continue
        buf.append(ch)

    piece = "".join(buf).strip()
    if piece:
        clauses.append(piece)

    # 丢掉纯标点/空白
    return [c for c in clauses if c and not re.fullmatch(r"[\s、，,；;。]+", c)]


# --------------------------------------------------------------------------
# ② 识别
# --------------------------------------------------------------------------

#: 出现这些词就说明该子句**可能**与输出有关——用于给未知子句做风险分级
_DAMAGE_HINTS = (
    "攻击力", "攻击", "伤害", "攻速", "攻击速度", "间隔", "连射", "连击",
    "目标", "防御", "法抗", "法术抗性", "倍率", "暴击", "脆弱", "增伤",
    "加成", "提升", "提高", "降低", "无视", "穿透", "真实", "法术",
    "物理", "每段", "段", "次", "枚", "发",
)

#: 明确表示"这个技能期间打不出普攻"的说法
_STOP_ATTACK_HINTS = (    "停止攻击", "无法普通攻击", "无法进行普通攻击", "不能普通攻击",
    "不再进行普通攻击", "无法攻击", "不进行攻击",
)

_UNIT_PATTERN = re.compile(r"(%|％|点|个|名|秒|s|S|倍|层|发|次|段|枚|格)")

#: 这些限定词等价于「有一个值」，只是不是数字。
#: 「持续时间无限」的「无限」、「同时攻击阻挡的所有敌人」的「所有」都属于此类。
_QUALIFIERS: tuple[str, ...] = (
    "无限", "所有", "全部", "整个战场", "阻挡的", "范围内", "全体", "全场",
)

#: 这些属性**没有数值也是有意义的**——它们表达的是"切换"而不是"增减"。
#: 例如「伤害类型变为法术」「技能结束后」「视为近距离攻击」「状态切换」。
_NUMBERLESS_ATTRIBUTES: frozenset[str] = frozenset({
    "damage_type", "termination", "trait", "stance",
})

#: 「敌人是属性的所有者」的线索词。中文里修饰语在属性词**前面**，
#: 所以只要看紧挨着属性词之前的几个字就够了——详见 :func:`_attribute_owner`。
_ENEMY_OWNERS: tuple[str, ...] = (
    "敌人的", "敌方的", "敌军的", "对手的",
    "敌人", "敌方", "敌军", "对手",
    "目标的", "目标",
)


def _attribute_word(text: str, attribute: str) -> str | None:
    """在该属性自己的词表里，找出文本中**最长**的那个说法。"""
    best: str | None = None
    for word in ATTRIBUTES[attribute]:
        if word in text and (best is None or len(word) > len(best)):
            best = word
    return best


def _attribute_owner(text: str, attribute: str) -> str:
    """判断这个属性属于**谁**——干员自己（``self``）还是敌人（``enemy``）。

    这一步不能省。真实数据里有::

        「诅咒娃娃周围**敌人的**攻击力和防御力-50%」

    它说的是把**敌人**的攻击力打下去，和干员自己的输出毫无关系。
    按"自己的攻击力-50%"套用，会让该干员的 DPS 直接砍半——
    而且不会有任何报错，置信度也照样是 partial。

    但也不能简单地"句子里出现敌人就算敌人的"：下面两句里的
    「敌人」全是**打向**的目标，属性仍然属于自己::

        「仅攻击到一个敌人时对**其**攻击力提升至160%」
        「攻击被晕眩**目标**时攻击力提高至250%」

    区分办法是**位置**：归属词必须紧挨着属性词之前（「敌人的攻击力」）。
    后者中间隔着「时」「对其」，所以不会被误判。
    """
    word = _attribute_word(text, attribute)
    if not word:
        return "self"
    prefix = text[: text.find(word)]
    for cue in _ENEMY_OWNERS:
        if prefix.endswith(cue):
            return "enemy"
    return "self"


def _detect_unit(text: str) -> str:
    match = _UNIT_PATTERN.search(text)
    return match.group(1) if match else ""


def _assess_unknown(text: str) -> bool | None:
    """给一个未识别的子句评估"会不会影响 DPS"。

    宁可返回 ``None``（不确定）也不猜。报告里 ``None`` 会明确写成
    "影响未知"，而不是假装没关系。
    """
    if any(hint in text for hint in _STOP_ATTACK_HINTS):
        # 「停止攻击」明确影响输出——技能期间没有普攻
        return True

    lowered = text
    has_damage_hint = any(hint in lowered for hint in _DAMAGE_HINTS)
    has_non_damage = any(
        word in lowered
        for concept, (keywords, _reason) in IRRELEVANT.items()
        for word in keywords
    )

    if has_damage_hint and not has_non_damage:
        return True
    if has_non_damage and not has_damage_hint:
        return False
    if not has_damage_hint:
        return False
    return None


#: 数量关系词（至多/至少）只可能修饰「可数的属性」。
#: 描述里「同时攻击至多6个目标」中，「同时攻击」是状语，真正的属性是「目标」。
_COUNTABLE_ATTRIBUTES: tuple[str, ...] = ("targets", "hits")


def _find_countable_attribute(text: str) -> str | None:
    for key in _COUNTABLE_ATTRIBUTES:
        for word in ATTRIBUTES[key]:
            if word in text:
                return key
    return None


#: 条件标记。带这些说法的效果**依赖战场情况**，不能无条件套用::
#:
#:     「可以进行远程攻击，**但此时**攻击力降低至80%」
#:         → 只有远程攻击时才降，近战不降
#:     「攻击**未被阻挡的**敌人时攻击力提升至110%」
#:         → 取决于敌人状态
#:
#: 静默地把这类效果当成常驻，会让计算结果**恒定偏离**——而且看不出哪里错了。
#: 所以识别到条件就把它单独放到 ``conditional`` 桶里，不参与默认计算，
#: 并在草稿里明确列出，由人决定。
#:
#: ⚠️ 标记必须**足够具体**。曾经用过单字「当」，结果「相**当**于攻击力145%」
#: 被误判为有条件，把该技能的伤害倍率整个隔离掉了。中文没有词边界，
#: 单字标记会和常用词内部撞车，所以这里一律用短语。
_CONDITION_MARKERS: tuple[str, ...] = (
    "但此时", "此时",
    "若", "如果",
    "未被阻挡", "阻挡的敌人", "阻挡时", "未阻挡",
    "精英", "领袖",
    "首次", "第二次", "第三次",
    "生命值低于", "生命值高于", "生命值不足",
    "未开启时", "开启时",
    "每有一个", "每存在",
    "时，", "时则",
)

#: 「假朋友」：**含有**条件标记、但整个词与条件无关的常用词。
#:
#: 「若」是必要的单字标记（「若目标处于X」），可「若干」是个表示"几个"的
#: 限定词，真实语料里出现过（「创造若干从空中直线向地面移动的弹道」）。
#: 命中标记时若发现它整个落在这些词里，就跳过。
_CONDITION_DECOYS: tuple[str, ...] = ("若干", "倘若", "宛若", "假若", "自若")


def _inside_decoy(text: str, start: int, length: int) -> bool:
    """``text[start:start+length]`` 是否整个落在某个假朋友词内部。"""
    end = start + length
    for decoy in _CONDITION_DECOYS:
        at = text.find(decoy)
        while at != -1:
            if at <= start and end <= at + len(decoy):
                return True
            at = text.find(decoy, at + 1)
    return False


def _detect_condition(text: str) -> str | None:
    """检测子句里是否含条件，返回命中的标记或 None。"""
    for marker in _CONDITION_MARKERS:
        at = text.find(marker)
        while at != -1:
            if not _inside_decoy(text, at, len(marker)):
                return marker
            at = text.find(marker, at + 1)
    return None


#: 复合子句判定的「强信号」属性。#:
#: 这些词一旦出现，几乎一定意味着子句里有真正的伤害数值，值得为
#: "数值归属不明"报一次未知。刻意**不包含** ``targets`` / ``duration`` /
#: ``termination`` / ``trait`` / ``stance``：它们的词太泛或者太"框架性"，
#: 会在确定无关的子句里大量出现（详见 :func:`_damage_critical_present`）。
_COMPOUND_SIGNAL_ATTRIBUTES: tuple[str, ...] = (
    "atk", "aspd", "interval", "per_hit_mult", "hits", "damage_type",
)


def _damage_critical_present(text: str) -> bool:
    """子句里是否出现了**强伤害信号**属性词。

    与 :func:`find_attribute` 不同，这里不做"最长词胜出"，只要出现就算。
    用途是识别**复合子句**：一个子句里既有无关概念又有伤害属性时，
    数值到底属于谁无法确定，静默处理会丢数据。

    ⚠️ 这里用的是 :data:`_COMPOUND_SIGNAL_ATTRIBUTES` 而**不是**整个
    ``DAMAGE_CRITICAL``。原因是 ``targets`` 与 ``duration`` 的词
    （「敌人」「目标」「持续」）在中文里太泛，会和确定无关的子句大量共现::

        「技能持续时间内逐渐获得12点部署费用」   ← 被「持续」误伤
        「优先攻击未处于损伤爆发期间的敌人」     ← 被「敌人」误伤
        「自身嘲讽等级更容易受到敌人攻击」       ← 被「敌人」误伤

    实测这三类一共上百条，全变成"有风险"就把 IRRELEVANT 词表的效果
    抵消了——"报告被噪音淹没"正是那张表要解决的问题。
    """
    for key in _COMPOUND_SIGNAL_ATTRIBUTES:
        for word in ATTRIBUTES[key]:
            if word in text:
                return True
    return False


def _is_relevant(attribute: str, relation: str | None) -> bool:
    """判断「属性 + 关系」是否可能影响 DPS。

    ``defense`` / ``res`` 必须分两种情况——只看属性词分不出来：

        「无视20点法术抗性」→ 说的是**敌人**的抗性 → 影响伤害
        「防御力+100%」     → 说的是**干员自己**的防御 → 与输出无关

    区分它们的唯一信号是关系词：只有「无视 / 穿透」才指向敌人。
    """
    if attribute in DAMAGE_CRITICAL:
        return True
    if attribute in PENETRATION_ATTRIBUTES:
        return relation == "ignore"
    return False


def analyze_clause(text: str) -> Clause:
    """分析单个子句。"""
    text = text.strip()
    if not text:
        return Clause(text="", kind=Recognition.UNKNOWN, affects_damage=False)

    # 条件检测在最前面做一次，后面每个返回点都带上它。
    # 带条件的效果不能无条件套用，必须让调用方看得见。
    condition = _detect_condition(text)

    # ---- 先看是不是已知的无关概念 ----
    # 这一步必须排在属性匹配之前：像「攻击范围扩大」里含有「攻击」二字，
    # 若先做属性匹配会被误认成 atk，然后因为找不到数值而变成"未知"。
    compound_warning: str | None = None
    concept = find_irrelevant(text)
    if concept is not None:
        _keywords, reason = IRRELEVANT[concept]
        if not _damage_critical_present(text):
            return Clause(
                text=text,
                kind=Recognition.IRRELEVANT,
                concept=concept,
                reason=reason,
                affects_damage=False,
                condition=condition,
            )
        # 复合子句：既有无关概念，又出现了伤害相关的属性词。
        # 不能直接判"无关"（会把「攻击范围扩大且攻击力+50%」里的加成丢掉），
        # 也不能直接判"识别成功"（数值属于哪个属性说不清）。
        # 继续往下走：若最终落到的属性本身就影响伤害（如「远程攻击不再
        # 降低攻击力」→ trait）就照常接受；否则按"未知"报出来让人确认。
        compound_warning = (
            f"子句同时含有「{concept}」与影响伤害的属性词，无法确定数值属于哪一个"
        )

    # ---- 「停止攻击」优先处理 ----
    # 它没有数值，但语义非常明确，而且**确实影响 DPS**（技能期间没有普攻）。
    if any(hint in text for hint in _STOP_ATTACK_HINTS):
        return Clause(
            text=text,
            kind=Recognition.RECOGNIZED,
            triple=Triple(attribute="atk", relation="stop", value=None, unit="", text=text),
            reason="技能期间不进行普通攻击",
            affects_damage=True,
            condition=condition,
        )

    # ---- 属性 + 关系 + 数值 ----
    attribute = find_attribute(text)
    relation = find_relation(text)
    value = parse_number(text)
    unit = _detect_unit(text)
    owner = _attribute_owner(text, attribute) if attribute is not None else "self"

    # 数量关系词纠正属性判定：「同时攻击至多6个目标」说的是目标数
    if relation in ("max_of", "min_of"):
        countable = _find_countable_attribute(text)
        if countable is not None:
            attribute = countable

    # 「持续时间无限」——把「无限」转成真正的无穷大，别丢信息
    if attribute == "duration" and "无限" in text:
        value = float("inf")
        unit = ""

    if attribute is not None:
        has_qualifier = any(q in text for q in _QUALIFIERS)
        numberless_ok = attribute in _NUMBERLESS_ATTRIBUTES

        if value is not None or has_qualifier or numberless_ok:
            triple = Triple(
                attribute=attribute,
                relation=relation or "unspecified",
                value=value,
                unit=unit,
                text=text,
            )
            if _is_relevant(attribute, relation):
                return Clause(
                    text=text,
                    kind=Recognition.RECOGNIZED,
                    triple=triple,
                    affects_damage=True,
                    condition=condition,
                    owner=owner,
                )
            if compound_warning is not None:
                # 数值落在了一个与伤害无关的属性上，但同一子句里还有别的
                # 伤害属性词——归属说不清，报"未知"而不是假装没关系。
                return Clause(
                    text=text,
                    kind=Recognition.UNKNOWN,
                    affects_damage=True,
                    reason=compound_warning,
                    condition=condition,
                    owner=owner,
                )
            return Clause(
                text=text,
                kind=Recognition.RECOGNIZED,
                triple=triple,
                reason=f"{attribute} 不参与伤害计算",
                affects_damage=False,
                condition=condition,
                owner=owner,
            )

    # ---- 认不出 ----
    # 再给一次机会：有属性词但没有数值（例如「攻击力翻倍」）
    if attribute is not None:
        return Clause(
            text=text,
            kind=Recognition.UNKNOWN,
            affects_damage=True if compound_warning else _assess_unknown(text),
            reason=compound_warning or "找到了属性词，但没能确定数值或关系",
            condition=condition,
        )

    return Clause(
        text=text,
        kind=Recognition.UNKNOWN,
        affects_damage=True if compound_warning else _assess_unknown(text),
        reason=compound_warning or "既没有可识别的属性词，也不是已知的无关概念",
        condition=condition,
    )


# --------------------------------------------------------------------------
# ③ 整段分析
# --------------------------------------------------------------------------


def analyze(text: str) -> list[Clause]:
    """把一整段技能描述解析成子句列表。"""
    return [analyze_clause(c) for c in split_clauses(text)]


@dataclass(frozen=True, slots=True)
class Summary:
    """一段描述的识别汇总——导入器的覆盖率报告就用它。"""

    total: int
    recognized: int
    irrelevant: int
    unknown: int
    unknown_risky: int      # 未知**且可能影响 DPS** 的子句数

    @property
    def confidence(self) -> str:
        """根据风险子句数给出置信度。"""
        if self.unknown_risky:
            return "low"
        if self.unknown:
            return "partial"
        return "exact"

    @property
    def coverage(self) -> float:
        """已识别（含判定为无关）的比例。"""
        if not self.total:
            return 1.0
        return (self.recognized + self.irrelevant) / self.total


def summarize(clauses: list[Clause]) -> Summary:
    recognized = sum(1 for c in clauses if c.kind is Recognition.RECOGNIZED)
    irrelevant = sum(1 for c in clauses if c.kind is Recognition.IRRELEVANT)
    unknown = sum(1 for c in clauses if c.kind is Recognition.UNKNOWN)
    risky = sum(
        1 for c in clauses
        if c.kind is Recognition.UNKNOWN and c.affects_damage is not False
    )
    return Summary(
        total=len(clauses),
        recognized=recognized,
        irrelevant=irrelevant,
        unknown=unknown,
        unknown_risky=risky,
    )
