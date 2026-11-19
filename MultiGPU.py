"""MultiGPU 启动入口：双卡 Blender 渲染调度器。"""

from __future__ import annotations

import sys

from multigpu.ui import main

if __name__ == "__main__":
    sys.exit(main())
