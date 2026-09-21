"""全量导入：把 PRTS 上**所有**干员导成数据草稿。

这个脚本只是 ``arkdps import`` 的薄包装——真正的逻辑在
:mod:`arkdps.importers.bulk` 里，命令行与这里共用同一份实现。

保留独立脚本是因为 CI 与文档里习惯用 ``python tools/import_all.py``，
而且它不需要先把包装好。

用法::

    python tools/import_all.py                # 导入全部
    python tools/import_all.py --class 近卫    # 只导入某个职业
    python tools/import_all.py --limit 20     # 先试前 20 个
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from arkdps.importers.bulk import (  # noqa: E402
    DEFAULT_CACHE_DIR,
    DEFAULT_DATA_DIR,
    run_import,
)
from arkdps.importers.prts import OPERATOR_CLASSES  # noqa: E402


def _configure_output_encoding() -> None:
    """把输出切到 UTF-8。

    标准输出被重定向时 Windows 默认按 GBK 编码，而这里的进度和报告
    全是中文——不重配就是一堆乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_output_encoding()

    parser = argparse.ArgumentParser(description="从 PRTS 导入全部干员数据草稿")
    parser.add_argument("--class", dest="klass", choices=OPERATOR_CLASSES,
                        help="只导入指定职业")
    parser.add_argument("--limit", type=int, default=0, help="最多导入多少个（0 = 全部）")
    parser.add_argument("--out", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--cache", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--delay", type=float, default=0.25,
                        help="请求间隔秒数（对 wiki 保持礼貌）")
    args = parser.parse_args(argv)

    print("枚举干员…")

    def progress(done: int, total: int, written: int, elapsed: float) -> None:
        print(f"  {done}/{total}  已写 {written}  用时 {elapsed:.0f}s")

    report = run_import(
        class_name=args.klass,
        limit=args.limit,
        out_dir=args.out,
        cache_dir=args.cache,
        delay=args.delay,
        progress=progress,
    )

    print()
    print("=" * 62)
    print("  导入完成")
    print("=" * 62)
    print(f"  干员草稿      {report.operators}")
    print(f"  技能总数      {report.skills}")
    if report.skills:
        print(f"  完全解析      {report.confidence.get('exact', 0):>5}"
              f"  ({report.exact_ratio:.0%})")
        print(f"  有无关的未知  {report.confidence.get('partial', 0):>5}"
              f"  ({report.partial_ratio:.0%})")
        print(f"  需人工复核    {report.confidence.get('low', 0):>5}"
              f"  ({report.low_ratio:.0%})")
    print(f"  缓存命中      {report.cache_hits}   实际请求 {report.requests}")
    if report.failures:
        print(f"  失败          {len(report.failures)}: {report.failures[:10]}")
    print(f"  覆盖率报告已写入 {pathlib.Path(args.out).parent / '_coverage.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
