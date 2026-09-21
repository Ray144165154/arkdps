"""让 ``python -m arkdps`` 等价于 ``arkdps``。

有了这个文件，不想装命令的人也能直接用::

    python -m arkdps calc 银灰 --def 800
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
