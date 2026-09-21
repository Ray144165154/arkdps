"""PRTS wiki 导入器。

把 PRTS 干员页转成 arkdps 的数据草稿。整个流程：

    ① 枚举干员      Category:{职业}干员
    ② 抓取页面      MediaWiki API（``action=parse``），带缓存与限速
    ③ 取模板字段    属性 / 技能 / 潜能提升 / CharinfoV2
    ④ 解析描述      :mod:`arkdps.recognizer` 把中文描述变成语义三元组
    ⑤ 映射成参数    :mod:`arkdps.mapping` 把三元组写进 effects
    ⑥ 产出草稿      带 ``_source`` / ``_parsed`` / ``_unparsed`` / ``_confidence``

**第 ⑥ 步是本模块存在的意义。** 草稿里明确写出：

  * 每个字段是从哪句话来的（``_source``）
  * 解析出来了什么（``_parsed``）
  * 有什么没解析出来、以及它是否可能影响 DPS（``_unparsed``）
  * 整体置信度（``_confidence``）

人只要扫一眼 ``_confidence`` 是 ``low`` 的技能，就知道哪里需要手工补。

网络纪律：带磁盘缓存（同一页面只抓一次）、请求间限速、可配置 User-Agent。
对 wiki 保持礼貌不是可选项。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..mapping import SKILL_LEVEL_FIELDS
from ..recognizer import Clause, Recognition, analyze, summarize
from .wikitext import clean_value, flatten, split_templates, template_fields

__all__ = ["PrtsClient", "PrtsImporter", "DRAFT_SCHEMA", "OPERATOR_CLASSES"]

DRAFT_SCHEMA = "arkdps/operator/1"
OPERATOR_CLASSES = ("近卫", "狙击", "术师", "医疗", "重装", "辅助", "特种", "先锋")

DEFAULT_API = "https://prts.wiki/api.php"
DEFAULT_UA = "arkdps/0.1 (Arknights DPS engine; data importer)"

#: 技能类型1 → 技力回复方式
_CHARGE_WORDS: dict[str, str] = {
    "自动回复": "auto",
    "攻击回复": "attack",
    "受击回复": "hit",
    "被动": "passive",
}


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


@dataclass
class PrtsClient:
    """带缓存与限速的 PRTS API 客户端。

    :param cache_dir: 页面 wikitext 的缓存目录。``None`` 表示不缓存。
    :param delay: 两次**实际网络请求**之间的最小间隔（秒）。
    :param user_agent: 请求标识。用默认值时请自行确认符合站点要求。
    """

    cache_dir: Path | None = None
    delay: float = 0.25
    user_agent: str = DEFAULT_UA
    api: str = DEFAULT_API
    timeout: int = 60
    retries: int = 3

    _last_request: float = field(default=0.0, repr=False)
    requests_made: int = field(default=0, repr=False)
    cache_hits: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        if self.cache_dir is not None:
            self.cache_dir = Path(self.cache_dir)
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- 内部 ------------------------------------------------------------
    def _cache_path(self, key: str) -> Path | None:
        if self.cache_dir is None:
            return None
        safe = re.sub(r"[^\w\u4e00-\u9fff.-]", "_", key)[:120]
        return self.cache_dir / f"{safe}.json"

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def _request(self, params: dict) -> dict | None:
        url = f"{self.api}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})

        for attempt in range(self.retries):
            self._throttle()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    self._last_request = time.monotonic()
                    self.requests_made += 1
                    return json.loads(response.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                time.sleep(2 + attempt * 3)
            except Exception:
                time.sleep(2 + attempt * 3)
        return None

    # -- 对外 ------------------------------------------------------------
    def page_wikitext(self, title: str) -> str | None:
        """取一个页面的 wikitext（带缓存）。"""
        cache = self._cache_path(f"page_{title}")
        if cache is not None and cache.exists():
            try:
                self.cache_hits += 1
                return json.loads(cache.read_text(encoding="utf-8"))["wikitext"]
            except Exception:
                pass

        data = self._request({
            "action": "parse", "page": title, "prop": "wikitext",
            "format": "json", "formatversion": "2",
        })
        if not data or "parse" not in data:
            return None

        wikitext = data["parse"]["wikitext"]
        if cache is not None:
            try:
                cache.write_text(
                    json.dumps({"title": title, "wikitext": wikitext}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError:
                pass
        return wikitext

    def operators_in_class(self, operator_class: str) -> list[str]:
        """列出某个职业下的所有干员。"""
        data = self._request({
            "action": "query", "list": "categorymembers",
            "cmtitle": f"Category:{operator_class}干员",
            "cmlimit": "500", "format": "json",
        })
        members = (data or {}).get("query", {}).get("categorymembers", [])
        return [
            m["title"] for m in members
            if not m["title"].startswith(("Category:", "模板:", "分类:"))
        ]

    def all_operators(self) -> list[str]:
        """列出全部干员（按职业汇总去重）。"""
        seen: dict[str, None] = {}
        for operator_class in OPERATOR_CLASSES:
            for name in self.operators_in_class(operator_class):
                seen.setdefault(name, None)
        return list(seen)


# --------------------------------------------------------------------------
# 数值解析
# --------------------------------------------------------------------------


def _to_float(value: str | None) -> float | None:
    """从字段值里取数值，例如 ``"90"`` / ``"1.3s"`` / ``"30"``。"""
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", clean_value(value))
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def _first_template(wikitext: str, name: str) -> dict[str, str]:
    """取页面上第一个指定名字的模板字段。"""
    for template_name, body in split_templates(wikitext):
        if template_name == name:
            return template_fields(body)
    return {}


def _all_templates(wikitext: str, names: set[str]) -> list[dict[str, str]]:
    """取页面上所有指定名字的模板字段。"""
    return [
        template_fields(body)
        for template_name, body in split_templates(wikitext)
        if template_name in names
    ]


# --------------------------------------------------------------------------
# 导入器
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Provenance:
    """一个技能的效果是从哪句话来的，以及解析得怎么样。"""

    clauses: list[Clause] = field(default_factory=list)

    def unparsed(self) -> list[dict]:
        return [
            {
                "text": c.text,
                "affects_damage": c.affects_damage,
                "reason": c.reason,
            }
            for c in self.clauses
            if c.kind is Recognition.UNKNOWN
        ]

    def confidence(self) -> str:
        return summarize(self.clauses).confidence

    def has_risk(self) -> bool:
        return any(
            c.kind is Recognition.UNKNOWN and c.affects_damage is not False
            for c in self.clauses
        )


class PrtsImporter:
    """把 PRTS 干员页转成数据草稿。"""

    def __init__(self, client: PrtsClient | None = None) -> None:
        self.client = client or PrtsClient()

    # -- 干员 ------------------------------------------------------------
    def import_operator(self, title: str) -> dict | None:
        """导入一个干员，返回草稿字典；页面不存在时返回 None。"""
        wikitext = self.client.page_wikitext(title)
        if not wikitext:
            return None

        info = _first_template(wikitext, "CharinfoV2")
        stats = _first_template(wikitext, "属性")
        potential = _first_template(wikitext, "潜能提升")

        # PRTS 的「稀有度」是 **0 起算** 的：0=1星 … 5=6星。
        # 直接抄下来会让所有干员都少一星。
        #
        # ⚠️ 不能写成 ``(_to_float(...) or -1) + 1``：稀有度 0 是合法的 1 星
        # （Castle-3、Lancet-2 这些机器人），而 ``0.0`` 是 falsy，
        # 会被 ``or`` 换成 -1，结果 1 星算成 0 星。必须判 ``is None``。
        rarity_raw = _to_float(info.get("稀有度"))

        # 面板攻击力按精英阶段从高到低取（低星干员没有精英 2）
        atk_value, atk_key = _pick_elite_stat(stats, "攻击")

        draft: dict = {
            "_schema": DRAFT_SCHEMA,
            "_source": {"wiki": "PRTS", "page": title},
            "name": info.get("干员名") or title,
            "class": info.get("职业", ""),
            "branch": info.get("分支", ""),
            "rarity": rarity_raw + 1 if rarity_raw is not None else None,
            "trait_text": flatten(info.get("特性", "")),
            "attack_interval": _to_float(stats.get("攻击速度")),
            "atk": atk_value,
            "atk_trust": _to_float(stats.get("信赖加成_攻击")) or 0.0,
            "atk_potential": _potential_atk(potential),
            "skills": [],
            "_warnings": [],
        }

        # 记下攻击力是从哪一档取的。低星干员取的是精英 1 或精英 0，
        # 不说清楚的话，看数据的人会以为这是精英 2 的面板。
        if atk_key and atk_key != "精英2_满级_攻击":
            draft["_atk_from"] = atk_key

        if draft["atk"] is None:
            draft["_warnings"].append(
                "没有取到面板攻击力（精英2/精英1/精英0 的满级攻击都没有），"
                "可能是异格干员或页面结构不同，需要手工填写"
            )
        if draft["attack_interval"] is None:
            draft["_warnings"].append("没有取到「攻击速度」，需要手工填写")

        # 特性文本里如果含有影响攻击力的说法，解析成 trait effects
        trait_text = draft["trait_text"]
        if trait_text:
            trait_clauses = analyze(trait_text)
            trait_mapped = _effects_from_clauses(trait_clauses)
            draft["trait"] = trait_mapped.effects_fields
            draft["_trait_parsed"] = trait_mapped.records

            # 带条件的效果不参与计算，但必须让人看见
            if trait_mapped.conditional:
                draft["_trait_conditional"] = trait_mapped.conditional
                draft["_trait_conditions"] = trait_mapped.conditions

            trait_review = _merge_unresolved(trait_clauses, trait_mapped.unresolved)
            if trait_review:
                draft["_trait_unparsed"] = trait_review
                draft["_trait_needs_review"] = any(
                    item.get("affects_damage") is not False for item in trait_review
                )
            draft["_trait_confidence"] = summarize(trait_clauses).confidence

        # 天赋（可能有多个，各取精英 2、不含模组那一档）
        talent_effects, talent_metas, talent_parsed, talent_unresolved = _parse_talents(wikitext)
        if talent_metas:
            draft["talent"] = talent_effects
            draft["_talent_meta"] = talent_metas
            draft["_talent_parsed"] = talent_parsed
            if talent_unresolved:
                draft["_talent_unparsed"] = talent_unresolved
                draft["_talent_needs_review"] = any(
                    item.get("affects_damage") is not False for item in talent_unresolved
                )
            # 天赋里的条件效果同样不进计算
            conditional: dict = {}
            for meta in talent_metas:
                conditional.update(meta.get("conditional") or {})
            if conditional:
                draft["_talent_conditional"] = conditional

        # 技能
        for fields in _all_templates(wikitext, {"技能", "技能2"}):
            skill = self._build_skill(fields)
            if skill:
                draft["skills"].append(skill)

        return draft

    def _build_skill(self, fields: dict[str, str]) -> dict | None:
        name = fields.get("技能名")
        if not name:
            return None

        description = flatten(fields.get("技能专精3描述", ""))
        clauses = analyze(description) if description else []
        provenance = _Provenance(clauses=clauses)
        mapped = _effects_from_clauses(clauses)

        # 技能级字段从映射结果里分流出来；剩下的才是 effects
        skill_fields = {
            key: value for key, value in mapped.fields.items()
            if key in SKILL_LEVEL_FIELDS
        }
        effects = {
            key: value for key, value in mapped.fields.items()
            if key not in SKILL_LEVEL_FIELDS
        }

        skill: dict = {
            "name": name.strip(),
            "charge": _CHARGE_WORDS.get(fields.get("技能类型1", "").strip(), "auto"),
            "trigger": fields.get("技能类型2", "").strip(),
            "sp_cost": _to_float(fields.get("技能专精3消耗")) or 0.0,
            "init_sp": _to_float(fields.get("技能专精3初始")) or 0.0,
            "duration": _to_float(fields.get("技能专精3持续")) or 0.0,
            "effects": effects,
            "_source": description,
            "_parsed": mapped.records,
            "_confidence": provenance.confidence(),
        }

        # 带条件的效果：单独存放，默认不参与计算
        if mapped.conditional:
            skill["_conditional"] = mapped.conditional
            skill["_conditions"] = mapped.conditions

        # 描述里写的持续时间可以补模板字段的空缺（例如「持续时间无限」）
        if skill_fields.get("infinite_duration"):
            skill["infinite_duration"] = True
        if skill_fields.get("stance"):
            skill["stance"] = True
        if skill_fields.get("attacks") is False:
            skill["attacks"] = False
        if not skill["duration"] and skill_fields.get("duration"):
            skill["duration"] = skill_fields["duration"]

        review = _merge_unresolved(clauses, mapped.unresolved)
        if review:
            skill["_unparsed"] = review
            skill["_needs_review"] = any(
                item.get("affects_damage") is not False for item in review
            )

        return skill

    # -- 批量 ------------------------------------------------------------
    def import_many(self, titles: list[str]) -> Iterator[dict]:
        """批量导入，逐个产出草稿（生成器，便于边抓边写文件）。"""
        for title in titles:
            draft = self.import_operator(title)
            if draft is not None:
                yield draft

    def import_class(self, operator_class: str) -> Iterator[dict]:
        yield from self.import_many(self.client.operators_in_class(operator_class))

    def import_all(self) -> Iterator[dict]:
        yield from self.import_many(self.client.all_operators())


# --------------------------------------------------------------------------
# 描述 → effects
# --------------------------------------------------------------------------


#: 面板数值在 PRTS 的 ``{{属性}}`` 模板里是**按精英阶段分档**存的::
#:
#:     精英0_1级_攻击 / 精英0_满级_攻击 / 精英1_满级_攻击 / 精英2_满级_攻击
#:
#: 1~3 星干员没有精英 2（1 星连精英 1 都没有），那些字段**根本不存在**。
#: 只读精英 2 会让全部 40 个低星干员的面板攻击力为空——他们的 DPS 直接
#: 算成 0，而报告里只有一句"需要手工填写"。
#:
#: 逐档回退取到的是**该干员能达到的最高精英阶段的满级攻击力**，
#: 正是"满级面板"该有的含义。
_ELITE_PHASES: tuple[str, ...] = ("精英2", "精英1", "精英0")


def _pick_elite_stat(fields: dict[str, str], suffix: str) -> tuple[float | None, str]:
    """按精英阶段从高到低取面板数值。返回 ``(数值, 命中的字段名)``。"""
    for phase in _ELITE_PHASES:
        key = f"{phase}_满级_{suffix}"
        value = _to_float(fields.get(key))
        if value is not None:
            return value, key
    return None, ""


def _potential_atk(fields: dict[str, str]) -> float:
    """从潜能提升模板里取攻击力加成。

    PRTS 的潜能表把每一潜的效果写成独立字段，形状不固定，
    所以这里做的是**保守的关键词搜索**：只挑明确写着「攻击力」的那一潜。
    """
    for key, value in fields.items():
        if "攻击" in key or "攻击" in value:
            text = flatten(value)
            if "攻击力" in text:
                # 「攻击力+25」这类写法
                match = re.search(r"攻击力\s*[+＋]\s*(\d+(?:\.\d+)?)", text)
                if match:
                    return float(match.group(1))
    return 0.0


def _parse_talents(wikitext: str) -> tuple[dict, list[dict], list[dict], list[dict]]:
    """解析**所有** ``{{天赋列表3}}``，每个模板代表一个天赋。

    ⚠️ 一个干员可能有**多个**天赋，对应页面上**多个** ``天赋列表3`` 模板。
    早先的实现处理完第一个就返回了，结果漏掉后面的天赋——
    能天使就是这样丢掉「天使的祝福（攻击力+6%）」的。
    这类错误不会报错，只会让 DPS 悄悄偏低。

    模板长这样::

        {{天赋列表3
        |天赋1=领袖|天赋1条件=精英1|天赋1效果=攻击力{{*|5%|+5%}}…
        |天赋2=领袖|天赋2条件=精英2|天赋2效果=攻击力{{*|10%|+10%}}…
        |天赋3=领袖|天赋3条件=精英2 X模组2级|天赋3效果=…
        }}

    每个模板内取条件**恰好等于「精英2」**的那一档；模组档位带「模组」字样，
    默认不采用（模组是可选强化，算进去会让"无模组"的对比失真）。
    找不到精确匹配时退回最后一档，并在元信息里标注。

    返回 ``(合并后的 effects, [每个天赋的元信息], records, unresolved)``。
    """
    from ..mapping import merge_fields

    merged: dict[str, object] = {}
    metas: list[dict] = []
    records: list[dict] = []
    unresolved: list[dict] = []

    for template_name, body in split_templates(wikitext):
        if template_name != "天赋列表3":
            continue

        fields = template_fields(body)
        entries: list[tuple[str, str, str]] = []
        for key in fields:
            match = re.fullmatch(r"天赋(\d+)效果", key)
            if not match:
                continue
            index = match.group(1)
            entries.append((
                fields.get(f"天赋{index}条件", "").strip(),
                fields.get(f"天赋{index}效果", ""),
                fields.get(f"天赋{index}", "").strip(),
            ))

        if not entries:
            continue

        chosen = next((e for e in entries if e[0] == "精英2"), None)
        fallback = False
        if chosen is None:
            chosen = entries[-1]
            fallback = True

        condition, effect_text, talent_name = chosen
        flattened = flatten(effect_text)
        clauses = analyze(flattened)
        mapped = _effects_from_clauses(clauses)

        merge_fields(merged, mapped.effects_fields)
        metas.append({
            "name": talent_name,
            "condition": condition,
            "text": flattened,
            **({"fallback": True} if fallback else {}),
        })
        records.extend(mapped.records)
        unresolved.extend(mapped.unresolved)

        # 天赋里也可能有带条件的效果（"攻击精英与领袖敌人时…"）
        if mapped.conditional:
            metas[-1]["conditional"] = mapped.conditional
            metas[-1]["conditions"] = mapped.conditions

    return merged, metas, records, unresolved


def _effects_from_clauses(clauses: list[Clause]):
    """把识别出来的子句映射成引擎字段。

    实际的字段映射在 :mod:`arkdps.mapping` 里；这里只负责组装草稿需要的形状。
    返回 :class:`~arkdps.mapping.MappingResult`。
    """
    from ..mapping import map_clauses_detailed

    return map_clauses_detailed(clauses)


def _merge_unresolved(clauses: list[Clause], unresolved: list[dict]) -> list[dict]:
    """把两类「需要人看一眼」的片段合并成一份清单。

    ① 识别器**认不出**的子句（``unknown``）
    ② 识别器认出了属性、但**映射层落不到字段上**的子句（``unmapped``）

    对使用者来说这两类的处理方式是一样的：都要人工确认。
    所以合成一份清单，按影响程度排序——可能影响 DPS 的排在前面。
    """
    items: list[dict] = [
        {
            "text": c.text,
            "kind": "unknown",
            "affects_damage": c.affects_damage,
            "reason": c.reason,
        }
        for c in clauses
        if c.kind is Recognition.UNKNOWN
    ]
    items.extend(unresolved)

    # 去重（同一句话可能同时被两条路径记到）
    seen: set[str] = set()
    unique: list[dict] = []
    for item in items:
        if item["text"] in seen:
            continue
        seen.add(item["text"])
        unique.append(item)

    # 可能影响 DPS 的排前面；None（影响未知）次之；确认无关的排最后
    order = {True: 0, None: 1, False: 2}
    unique.sort(key=lambda i: order.get(i.get("affects_damage"), 1))
    return unique
