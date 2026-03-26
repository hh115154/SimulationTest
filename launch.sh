#!/usr/bin/env bash
# 一键启动 SimulationTest GUI 面板
cd "$(dirname "$0")"
echo "Starting AUTOSAR IPC Monitor GUI..."
python3 run_gui.py "$@"
