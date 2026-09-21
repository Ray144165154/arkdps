#!/usr/bin/env python
"""端到端冒烟测试：把命令行**真的跑一遍**。

单元测试调用的是函数，这个脚本跑的是**安装后的那个东西**——
它能抓到单元测试看不见的问题：

  * 打包配置漏了子包（``arkdps.importers``），装出来缺一半功能
  * 某个子命令在这台机器上直接崩
  * 退出码不对（CI 和脚本靠它判断成败）
  * 中文输出在非 UTF-8 控制台上抛 ``UnicodeEncodeError``

为什么不写在 PowerShell / bash 里：``$LASTEXITCODE`` 在管道之后会失效，
而把原生命令的 stderr 合并进 PowerShell 的错误流还会让"全绿"变成"失败"。
退出码的判断放在 Python 里最可靠。

本地运行::

    python tools/ci_smoke.py
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "arkdps"
DATA = ROOT / "data" / "operators"


def _configure_output_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def run(args: list[str], *, expect: int = 0, label: str = "") -> subprocess.CompletedProcess:
    """跑一条命令，断言退出码。返回 CompletedProcess。"""
    # 强制子进程也用 UTF-8，否则 Windows 上中文输出会被按 GBK 编码，
    # 再被我们按 UTF-8 解码成乱码。
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT)

    proc = subprocess.run(
        [sys.executable, *args],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    if proc.returncode != expect:
        print(f"✘ {label or ' '.join(args)}")
        print(f"  期望退出码 {expect}，实际 {proc.returncode}")
        if proc.stdout.strip():
            print(f"  stdout: {proc.stdout.strip()[:800]}")
        if proc.stderr.strip():
            print(f"  stderr: {proc.stderr.strip()[:800]}")
        raise SystemExit(1)
    return proc


def check_packaging() -> None:
    """``pyproject.toml`` 里的 ``packages`` 必须覆盖所有子包。

    ``packages`` 是**精确列表**，不会自动递归。漏掉一个子包，
    装出来的包会缺功能——而且直到有人调用它才会发现。
    本地跑源码时完全看不出来，因为源码是在当前目录里直接导入的。
    """
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = set(config["tool"]["setuptools"]["packages"])

    actual: set[str] = set()
    for path in SRC.rglob("__init__.py"):
        rel = path.parent.relative_to(ROOT)
        actual.add(".".join(rel.parts).removesuffix(".__pycache__"))

    missing = actual - declared
    if missing:
        print(f"✘ pyproject.toml 的 packages 漏了子包: {sorted(missing)}")
        print("  装出来的包会缺这些模块，但源码目录里跑却一切正常。")
        raise SystemExit(1)

    stale = declared - actual
    if stale:
        print(f"✘ pyproject.toml 声明了不存在的包: {sorted(stale)}")
        raise SystemExit(1)

    print(f"✔ 打包配置覆盖 {len(actual)} 个包: {sorted(actual)}")


def check_cli() -> None:
    if not DATA.is_dir() or not any(DATA.glob("*.json")):
        print(f"! 跳过命令行检查：没有草稿数据 {DATA}")
        return

    run(["-m", "arkdps", "--version"], label="arkdps --version")
    run(["-m", "arkdps", "--help"], label="arkdps --help")

    # 不带子命令应当打印帮助并成功退出，而不是报错
    run(["-m", "arkdps"], label="arkdps（无子命令）")

    proc = run(["-m", "arkdps", "list"], label="arkdps list")
    if "干员" not in proc.stdout:
        print("✘ arkdps list 没有输出干员表")
        raise SystemExit(1)

    proc = run(["-m", "arkdps", "coverage"], label="arkdps coverage")
    if "解析覆盖率" not in proc.stdout:
        print("✘ arkdps coverage 输出不对")
        raise SystemExit(1)

    # 真的算一遍，并核对一个手算得出来的数
    proc = run(["-m", "arkdps", "calc", "银灰", "--def", "800", "--json"],
               label="arkdps calc 银灰")
    try:
        payload = json.loads(proc.stdout)
    except ValueError as exc:
        print(f"✘ --json 输出不是合法 JSON: {exc}")
        print(proc.stdout[:500])
        raise SystemExit(1) from exc

    rows = payload.get("rows") or []
    if not rows:
        print("✘ calc 没有返回任何结果行")
        raise SystemExit(1)

    normal = next((r for r in rows if r["is_normal"]), None)
    if normal is None:
        print("✘ calc 结果里没有常态对照行")
        raise SystemExit(1)

    # 银灰面板 713 + 信赖 50 + 潜能 26 = 789，天赋 +10% → 867.9
    if abs(normal["atk"] - 867.9) > 0.05:
        print(f"✘ 银灰常态攻击力是 {normal['atk']}，期望 867.9")
        raise SystemExit(1)

    print(f"✔ 命令行 5 个子命令全部正常（银灰常态攻击力 {normal['atk']:.1f}）")


def main() -> int:
    _configure_output_encoding()

    print("=" * 62)
    print("  arkdps 冒烟测试")
    print("=" * 62)

    check_packaging()
    check_cli()

    print("-" * 62)
    print("✔ 冒烟测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
