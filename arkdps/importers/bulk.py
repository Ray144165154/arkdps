"""批量导入：把所有干员导成数据草稿，并产出一份覆盖率报告。

单独放一层是因为有两个调用方：

  * ``arkdps import`` 命令行
  * ``tools/import_all.py``

两边都需要同一套"逐个抓、写草稿、统计覆盖率"的逻辑，写两份迟早会不一致。

第一次跑会抓几百个页面（几分钟），之后页面缓存在磁盘上，再跑就是秒级——
所以调词表时不用重复联网。
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .prts import OPERATOR_CLASSES, PrtsClient, PrtsImporter

__all__ = [
    "ImportReport",
    "safe_filename",
    "import_operators",
    "run_import",
    "DEFAULT_DATA_DIR",
    "DEFAULT_CACHE_DIR",
]

#: 包所在仓库的根目录（``<repo>/arkdps/importers/bulk.py`` 往上三层）
_PKG_ROOT = Path(__file__).resolve().parent.parent.parent


def _default_data_dir() -> Path:
    """默认草稿目录。

    **优先当前工作目录，其次才是包所在的仓库。** 因为
    ``pip install arkdps`` 之后包躺在 site-packages 里，那里没有 data
    目录；而用户通常是在自己的工作目录里跑命令，数据也在那里。
    源码 checkout 的情形两者一致，所以这个顺序不会让人意外。
    """
    cwd_data = Path.cwd() / "data" / "operators"
    if cwd_data.is_dir():
        return cwd_data
    return _PKG_ROOT / "data" / "operators"


def _default_cache_dir() -> Path:
    """默认缓存目录，规则同 :func:`_default_data_dir`。"""
    cwd_cache = Path.cwd() / ".cache" / "prts"
    if cwd_cache.parent.is_dir():
        return cwd_cache
    return _PKG_ROOT / ".cache" / "prts"


DEFAULT_DATA_DIR = _default_data_dir()
DEFAULT_CACHE_DIR = _default_cache_dir()

#: 进度回调：``(已完成数, 总数, 已写文件数, 已用秒数)``
ProgressFn = Callable[[int, int, int, float], None]


def safe_filename(title: str) -> str:
    """把干员名变成安全的文件名。

    干员名里有 ``Mon3tr``、``GALLUS²``、``Raidian(卫戍协议)`` 这类写法，
    直接当文件名在 Windows 上会出问题。
    """
    return "".join(c if c.isalnum() or c in "-_()（）" else "_" for c in title)


@dataclass(slots=True)
class ImportReport:
    """一次批量导入的结果统计。"""

    operators: int = 0
    failures: list[str] = field(default_factory=list)
    skills: int = 0
    skills_needing_review: int = 0
    confidence: Counter = field(default_factory=Counter)
    unresolved_risky: Counter = field(default_factory=Counter)
    cache_hits: int = 0
    requests: int = 0
    elapsed: float = 0.0

    @property
    def exact_ratio(self) -> float:
        return self.confidence.get("exact", 0) / self.skills if self.skills else 0.0

    @property
    def partial_ratio(self) -> float:
        return self.confidence.get("partial", 0) / self.skills if self.skills else 0.0

    @property
    def low_ratio(self) -> float:
        return self.confidence.get("low", 0) / self.skills if self.skills else 0.0

    def as_dict(self, top: int = 40) -> dict:
        """转成可写盘的报告。

        键名保持不变，``data/_coverage.json`` 的格式是稳定的——
        覆盖率报告会被别的工具读，改字段名等于破坏接口。

        ⚠️ 这里**只放描述数据的字段**，不放运行遥测。``elapsed``、
        ``cache_hits``、``requests`` 每次跑都不一样（缓存冷热、机器快慢），
        写进受版本控制的文件会让每次导入都产生无意义的 diff——
        而这个文件是提交进仓库的。

        运行遥测由调用方直接读 :class:`ImportReport` 的字段来展示，
        它属于"这次运行"而不属于"这份数据"。
        """
        return {
            "operators": self.operators,
            "failures": self.failures,
            "skills": self.skills,
            "skills_needing_review": self.skills_needing_review,
            "confidence": dict(self.confidence),
            "top_unresolved_risky": self.unresolved_risky.most_common(top),
        }


def import_operators(
    titles: list[str],
    importer: PrtsImporter,
    out_dir: Path,
    *,
    progress: ProgressFn | None = None,
    every: int = 25,
) -> ImportReport:
    """把一批干员导成草稿，逐个写进 ``out_dir``。

    :param progress: 每 ``every`` 个调一次，用来打进度。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    report = ImportReport()
    started = time.monotonic()

    for index, title in enumerate(titles, 1):
        draft = importer.import_operator(title)
        if draft is None:
            report.failures.append(title)
            continue

        (out_dir / f"{safe_filename(title)}.json").write_text(
            json.dumps(draft, ensure_ascii=False, indent=1),
            encoding="utf-8",
            # ⚠️ 必须显式指定 ``newline="\n"``。``write_text`` 默认会把 ``\n``
            # 按平台换成 ``os.linesep``，于是同一次导入在 Windows 上产出
            # CRLF、在 Linux 上产出 LF——**生成的数据不该因平台而异**。
            # 否则每次换平台重跑都会得到一份巨大的、毫无意义的 diff。
            newline="\n",
        )
        report.operators += 1

        for skill in draft.get("skills", []):
            report.skills += 1
            report.confidence[skill.get("_confidence", "?")] += 1
            if skill.get("_needs_review"):
                report.skills_needing_review += 1
            for item in skill.get("_unparsed", []):
                if item.get("affects_damage") is not False:
                    report.unresolved_risky[item["text"][:48]] += 1

        if progress is not None and (index % every == 0 or index == len(titles)):
            progress(index, len(titles), report.operators, time.monotonic() - started)

    report.elapsed = time.monotonic() - started
    report.cache_hits = getattr(importer.client, "cache_hits", 0)
    report.requests = getattr(importer.client, "requests_made", 0)
    return report


def run_import(
    *,
    class_name: str | None = None,
    limit: int = 0,
    out_dir: Path | str = DEFAULT_DATA_DIR,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    delay: float = 0.25,
    progress: ProgressFn | None = None,
    client: PrtsClient | None = None,
    titles: list[str] | None = None,
) -> ImportReport:
    """列干员 → 逐个导入 → 写草稿与覆盖率报告。

    :param titles: 直接给定要导入的干员名，跳过枚举（测试与
        "只重导这几个"的场合用）。
    :param client: 换一个客户端（测试会塞离线假客户端进来）。
    """
    out_dir = Path(out_dir)
    client = client or PrtsClient(cache_dir=Path(cache_dir), delay=delay)

    if titles is None:
        if class_name:
            titles = client.operators_in_class(class_name)
        else:
            titles = client.all_operators()
    if limit:
        titles = titles[:limit]

    report = import_operators(
        titles, PrtsImporter(client), out_dir, progress=progress
    )

    (out_dir.parent / "_coverage.json").write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=1),
        encoding="utf-8",
        newline="\n",   # 理由同 import_operators：生成物必须跨平台一致
    )
    return report


def iter_classes() -> Iterator[str]:
    """可用的职业列表。"""
    yield from OPERATOR_CLASSES
