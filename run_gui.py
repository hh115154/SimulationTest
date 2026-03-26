#!/usr/bin/env python3
"""
run_gui.py — SimulationTest GUI 启动入口
用法：
    python3 run_gui.py          # 直接启动
    python3 run_gui.py --demo   # 启动后自动运行 Demo
"""
import sys
import os

# 确保 gui/ 目录在 import 路径中
_gui_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui")
if _gui_dir not in sys.path:
    sys.path.insert(0, _gui_dir)

# 自动检查并提示依赖
def _check_deps():
    missing = []
    for pkg in ("PyQt6", "pyqtgraph", "numpy", "psutil"):
        try:
            __import__(pkg.replace("-", "_").split(".")[0])
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"[ERROR] 缺少依赖：{', '.join(missing)}")
        print(f"        请运行：pip3 install {' '.join(missing)}")
        sys.exit(1)

_check_deps()

from gui.main_window import main

if __name__ == "__main__":
    main()
