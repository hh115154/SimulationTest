# SimulationTest — AUTOSAR IPC 可视化监控面板

本地原生 GUI 工具，用于监控和控制 **MyAutoSarAp**（SOC/AP）与 **MyAutoSarCP**（MCU/CP）之间的 SOME/IP 通信，并提供实时数据可视化。

## 技术栈

| 组件 | 版本 | 说明 |
|------|------|------|
| Python | 3.9+ | 主语言 |
| PyQt6 | 6.10+ | GUI 框架（arm64 原生）|
| pyqtgraph | 0.13+ | 实时绘图（GPU 加速）|
| numpy | 2.0+ | 数值计算 |
| psutil | 7.0+ | 进程资源监控 |

## 快速启动

```bash
# 安装依赖（只需一次）
pip3 install PyQt6 pyqtgraph numpy psutil

# 启动 GUI
cd /Users/hh/WorkBuddy/SimulationTest
python3 run_gui.py

# 或使用脚本
bash launch.sh
```

打开后点击 **[▶ 启动完整 Demo]** 自动启动 AP + CP 进程，即可看到实时信号流。

## 界面功能

| 区域 | 功能 |
|------|------|
| **左侧边栏** | Demo 一键启停 · AP/CP 进程控制 · 信号订阅开关 · 通信统计 |
| **📈 实时信号** | 6 路信号仪表盘 + 进度条 + SOME/IP 帧详情 + E2E 状态 |
| **📉 趋势图** | 车速/转速/燃油/转角 80 点滚动折线图（GPU 渲染）|
| **🔗 通信状态** | AP↔CP 通信架构图 · SOME/IP 协议参数表 |
| **📋 进程日志** | AP/CP 进程实时日志流（彩色高亮）|

## 目录结构

```
SimulationTest/
├── run_gui.py              # 启动入口
├── launch.sh               # 一键启动脚本
├── gui/
│   ├── main_window.py      # 主窗口（PyQt6）
│   ├── styles.py           # 暗色主题 QSS + 调色板
│   ├── data_interface.py   ★ 后端对接入口
│   └── process_manager.py  ★ 进程管理（可替换）
├── logs/                   # 运行日志
└── README.md
```

## 后端工程师对接指南

### 接入真实数据源

修改 `gui/data_interface.py` 中的 `DataSource._rx_loop()` 方法：

```python
def _rx_loop(self):
    """★ 替换此方法以接入真实硬件数据 ★"""
    while self._running:
        # 从真实硬件读取数据，构造 VehicleSignal 对象
        sig = VehicleSignal(
            vehicle_speed_kmh = your_can_bus.read_speed(),
            engine_rpm        = your_can_bus.read_rpm(),
            # ...
        )
        self._deliver(sig)  # 推送给 GUI
        time.sleep(0.01)
```

### 支持的数据接入方式

| 方式 | 对接方法 |
|------|---------|
| SOME/IP UDP（当前默认）| 已实现，无需改动 |
| CAN bus (python-can) | 替换 `_rx_loop()` |
| SPI (spidev) | 替换 `_rx_loop()` |
| 共享内存 | 替换 `_rx_loop()` |
| gRPC / REST | 替换整个 `DataSource` 类 |
| 硬件 HIL | 替换整个 `DataSource` 类 |

GUI 层只调用 `on_frame` 回调，完全不关心数据来源。

## SOME/IP 协议参数

| 参数 | 值 |
|------|-----|
| Service ID | `0x1001` |
| Event ID | `0x8001` |
| UDP 端口 | `30501` |
| Payload | 20 bytes（VehicleSignalPayload_t）|
| E2E 保护 | Profile 2 · CRC8 · 多项式 `0x1D` |
