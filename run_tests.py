#!/usr/bin/env python3
"""测试入口。

    python run_tests.py              # 跑全部
    python run_tests.py -v           # 列出每个用例
    python run_tests.py test_rotation
    python run_tests.py -f           # 第一个失败就停

**为什么不用** ``python -m unittest discover`` **了**：

1. ``unittest`` 把结果写到 stderr，而 PowerShell 会把原生命令的 stderr
   当成致命错误——"测试全绿但退出码是 1"。这个入口把结果写到 stdout，
   退出码由 Python 自己决定。
2. ``$LASTEXITCODE`` 在管道后会失效，CI 里判断退出码并不可靠。
3. 顺带打印每个测试文件的用例数，CI 日志里能一眼看出是哪个模块变少了。

只依赖标准库。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS = ROOT / "tests"


def _prepare_path() -> None:
    """让 `import arkdps` 与 `import fixtures` 都能工作。"""
    for path in (ROOT, TESTS):
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)


def _configure_output_encoding() -> None:
    """把测试输出切到 UTF-8。

    标准输出被重定向时，Windows 默认按 GBK 编码。本项目的输出全是中文，
    不重配就会乱码（或者直接抛 ``UnicodeEncodeError``）。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _count(suite: unittest.TestSuite) -> int:
    return suite.countTestCases()


def _per_module_counts(suite: unittest.TestSuite) -> list[tuple[str, int]]:
    """把 suite 摊平成「模块名 → 用例数」。

    ``loader.discover`` 返回的是嵌套 suite，所以要递归下去。
    """
    counts: dict[str, int] = {}

    def walk(item: object) -> None:
        if isinstance(item, unittest.TestSuite):
            for child in item:
                walk(child)
            return
        module = type(item).__module__.rsplit(".", 1)[-1]
        counts[module] = counts.get(module, 0) + 1

    walk(suite)
    return sorted(counts.items())


#: 从 traceback 里抓出最后一个 ``File "路径", line 行号``
_LOCATION = re.compile(r'File "([^"]+)", line (\d+)')


def _locate(traceback_text: str) -> tuple[str, int]:
    matches = _LOCATION.findall(traceback_text)
    if not matches:
        return "run_tests.py", 1
    path, line = matches[-1]
    # GitHub 的注解要**正斜杠**路径，Windows 上 traceback 给的是反斜杠，
    # 直接发过去注解可能挂不到文件上。
    return path.replace("\\", "/"), int(line)


def _emit_github_annotations(result: unittest.TestResult) -> None:
    """在 GitHub Actions 里把失败转成**注解**。

    为什么要多这一步：GitHub 的原始 job 日志要认证才能下载，
    而注解可以通过公开的检查 API（``/commits/<sha>/check-runs``）读到，
    里面带着文件、行号和消息。失败时能直接看到"哪个文件的哪一行断言错了"，
    不用去翻日志。

    非 CI 环境下什么都不做。
    """
    if not os.environ.get("GITHUB_ACTIONS"):
        return

    for case, traceback_text in [*result.failures, *result.errors]:
        path, line = _locate(traceback_text)
        message = traceback_text.strip().splitlines()[-1] if traceback_text.strip() else ""
        title = f"{type(case).__name__}.{getattr(case, '_testMethodName', '?')}"
        message = message.replace("\n", " ")[:900]
        print(f"::error file={path},line={line},title={title}::{message}")

    for case, _reason in result.skipped:
        title = f"{type(case).__name__}.{getattr(case, '_testMethodName', '?')}"
        print(f"::warning title=skip {title}::用例被跳过")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="运行 arkdps 测试（无网络依赖）",
    )
    parser.add_argument(
        "pattern",
        nargs="?",
        default="test_*.py",
        help="测试文件名通配（默认 test_*.py，可用 test_rotation 只跑一个）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="列出每个用例")
    parser.add_argument("-f", "--failfast", action="store_true", help="首个失败即停")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出汇总")
    args = parser.parse_args(argv)

    _prepare_path()
    _configure_output_encoding()

    pattern = args.pattern
    if not pattern.endswith(".py"):
        pattern += ".py"

    loader = unittest.TestLoader()
    try:
        suite = loader.discover(str(TESTS), pattern=pattern, top_level_dir=str(TESTS))
    except ImportError as exc:  # 导入期就炸的模块
        print(f"导入测试模块失败：{exc}", file=sys.stdout)
        return 1

    total = _count(suite)
    if total == 0:
        print(f"没有匹配到任何测试：tests/{pattern}", file=sys.stdout)
        return 1

    # 必须在 run() **之前**统计：unittest 跑完会把 suite 里的用例置为 None
    # 来释放内存，之后再遍历就只能拿到一堆 None（模块名会显示成 builtins）。
    counts = _per_module_counts(suite)

    if not args.quiet:
        print("=" * 62)
        print(f"arkdps 测试套件 —— {total} 个用例")
        print("=" * 62)

    # failfast / verbosity 交给 runner，退出码也由 runner 决定
    runner = unittest.TextTestRunner(
        verbosity=2 if args.verbose else 1,
        failfast=args.failfast,
        stream=sys.stdout,
    )
    result = runner.run(suite)

    _emit_github_annotations(result)

    # 写 stdout 而不是 stderr：PowerShell 不会把它当成错误
    print()
    print("-" * 62)
    if not args.quiet:
        for module, count in counts:
            print(f"  {module:<24} {count:>4}")
        print("-" * 62)

    failed = len(result.failures) + len(result.errors)
    status = "失败" if failed or result.unexpectedSuccesses else "通过"
    print(
        f"共 {result.testsRun} 个用例，失败 {len(result.failures)}，"
        f"错误 {len(result.errors)}，跳过 {len(result.skipped)} —— {status}"
    )
    print("-" * 62)

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
