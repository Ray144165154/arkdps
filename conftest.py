"""pytest 兼容层。

测试套件本身**只用 unittest**（零依赖），这个文件只是让习惯 pytest 的人
可以直接 ``pytest`` 而不必先读 README：

  * 把项目根目录与 ``tests/`` 放进 ``sys.path``，
    这样 ``import arkdps`` 与 ``import fixtures`` 在 pytest 下也能工作；
  * 不做任何其它事——测试用例不强依赖 pytest 的任何特性。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS = ROOT / "tests"

for _path in (ROOT, TESTS):
    _text = str(_path)
    if _text not in sys.path:
        sys.path.insert(0, _text)
