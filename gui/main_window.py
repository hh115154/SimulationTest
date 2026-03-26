"""
main_window.py — AUTOSAR IPC 可视化监控面板主窗口
════════════════════════════════════════════════════════════════════════
技术栈：PyQt6 + pyqtgraph
后端对接：修改 data_interface.py 和 process_manager.py 即可
════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations
import sys
import time
from datetime import datetime
from collections import deque
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, QObject, QSize
)
from PyQt6.QtGui import (
    QColor, QFont, QPalette, QIcon, QTextCursor
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QLabel, QPushButton, QSplitter,
    QTabWidget, QTextEdit, QCheckBox, QProgressBar, QFrame,
    QSizePolicy, QScrollArea, QStatusBar
)

from styles import DARK_QSS, C, PLOT_COLORS
from data_interface import DataSource, VehicleSignal
from process_manager import ProcessManager


# ═══════════════════════════════════════════════════════════════════════
# 信号桥（线程安全：子线程 → Qt 主线程）
# ═══════════════════════════════════════════════════════════════════════

class _Bridge(QObject):
    frame_received  = pyqtSignal(object)   # VehicleSignal
    log_received    = pyqtSignal(str, str, str)   # key, line, ts
    status_changed  = pyqtSignal(str, bool, object)  # key, running, pid


# ═══════════════════════════════════════════════════════════════════════
# 仪表卡片
# ═══════════════════════════════════════════════════════════════════════

class GaugeCard(QWidget):
    """单路信号仪表：标题 + 大数字 + 单位 + 进度条"""

    def __init__(self, title: str, unit: str, val_max: float,
                 color: str = C["blue"], decimals: int = 1):
        super().__init__()
        self._max      = val_max
        self._color    = color
        self._decimals = decimals

        self.setObjectName("GaugeCard")
        self.setStyleSheet(f"""
            #GaugeCard {{
                background-color: {C['surface2']};
                border: 1px solid {C['border']};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(12, 10, 12, 10)

        self._title_lbl = QLabel(title.upper())
        self._title_lbl.setProperty("role", "title")
        self._title_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self._val_lbl = QLabel("—")
        self._val_lbl.setFont(QFont("SF Mono", 24, QFont.Weight.Bold))
        self._val_lbl.setStyleSheet(f"color: {color}; background: transparent;")
        self._val_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self._unit_lbl = QLabel(unit)
        self._unit_lbl.setProperty("role", "unit")

        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(4)
        self._bar.setStyleSheet(f"""
            QProgressBar {{
                background-color: {C['surface']};
                border: none;
                border-radius: 2px;
            }}
            QProgressBar::chunk {{
                border-radius: 2px;
                background-color: {color};
            }}
        """)

        layout.addWidget(self._title_lbl)
        layout.addWidget(self._val_lbl)
        layout.addWidget(self._unit_lbl)
        layout.addWidget(self._bar)
        layout.addStretch()

    def update_value(self, v: float):
        fmt = f"{v:.{self._decimals}f}"
        if self._decimals == 0:
            fmt = f"{int(v)}"
        self._val_lbl.setText(fmt)
        pct = int(min(1.0, max(0.0, v / self._max)) * 1000)
        self._bar.setValue(pct)

    def flash(self, ok: bool):
        """E2E 错误时短暂变红"""
        color = self._color if ok else C["red"]
        self._val_lbl.setStyleSheet(f"color: {color}; background: transparent;")


# ═══════════════════════════════════════════════════════════════════════
# 统计卡片
# ═══════════════════════════════════════════════════════════════════════

class StatCard(QWidget):
    def __init__(self, label: str, color: str = C["green"]):
        super().__init__()
        self.setObjectName("StatCard")
        self.setStyleSheet(f"""
            #StatCard {{
                background-color: {C['surface']};
                border: 1px solid {C['border']};
                border-radius: 6px;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._val = QLabel("0")
        self._val.setFont(QFont("SF Mono", 20, QFont.Weight.Bold))
        self._val.setStyleSheet(f"color: {color}; background: transparent;")
        self._val.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._lbl = QLabel(label)
        self._lbl.setStyleSheet(f"color: {C['text2']}; font-size: 10px; background: transparent;")
        self._lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(self._val)
        layout.addWidget(self._lbl)

    def set_value(self, v):
        self._val.setText(str(v))


# ═══════════════════════════════════════════════════════════════════════
# 进程控制面板
# ═══════════════════════════════════════════════════════════════════════

class ProcessPanel(QGroupBox):
    def __init__(self, key: str, title: str, color: str, pm: ProcessManager, bridge: _Bridge):
        super().__init__(title)
        self._key   = key
        self._color = color
        self._pm    = pm

        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # 状态行
        row1 = QHBoxLayout()
        self._badge = QLabel("● STOPPED")
        self._badge.setProperty("role", "badge_stop")
        self._pid_lbl = QLabel("PID: —")
        self._pid_lbl.setProperty("role", "meta")
        row1.addWidget(self._badge)
        row1.addStretch()
        row1.addWidget(self._pid_lbl)
        layout.addLayout(row1)

        # 资源行
        self._res_lbl = QLabel("CPU: —   MEM: —")
        self._res_lbl.setProperty("role", "meta")
        layout.addWidget(self._res_lbl)

        # 按钮行
        row2 = QHBoxLayout()
        self._btn_start   = self._mk_btn("▶ Start",   "green")
        self._btn_stop    = self._mk_btn("■ Stop",    "red")
        self._btn_restart = self._mk_btn("↺ Restart", "blue")
        row2.addWidget(self._btn_start)
        row2.addWidget(self._btn_stop)
        row2.addWidget(self._btn_restart)
        layout.addLayout(row2)

        self._btn_start.clicked.connect(lambda: pm.start(key))
        self._btn_stop.clicked.connect(lambda: pm.stop(key))
        self._btn_restart.clicked.connect(lambda: pm.restart(key))

        bridge.status_changed.connect(self._on_status)

        # 检查二进制是否存在
        exists = Path_exists(key, pm)
        if not exists:
            self._btn_start.setEnabled(False)
            self._btn_restart.setEnabled(False)
            warn = QLabel("⚠ Binary not built. Run --build first.")
            warn.setStyleSheet(f"color: {C['yellow']}; font-size: 11px;")
            layout.addWidget(warn)

    def _mk_btn(self, text: str, btype: str) -> QPushButton:
        b = QPushButton(text)
        b.setProperty("btntype", btype)
        return b

    def _on_status(self, key: str, running: bool, pid):
        if key != self._key:
            return
        if running:
            self._badge.setText("● RUNNING")
            self._badge.setProperty("role", "badge_run")
            self._pid_lbl.setText(f"PID: {pid}")
        else:
            self._badge.setText("● STOPPED")
            self._badge.setProperty("role", "badge_stop")
            self._pid_lbl.setText("PID: —")
            self._res_lbl.setText("CPU: —   MEM: —")
        # force style refresh
        self._badge.style().unpolish(self._badge)
        self._badge.style().polish(self._badge)

    def update_resource(self, res: dict):
        cpu = res.get("cpu", 0)
        mem = res.get("mem_mb", 0)
        self._res_lbl.setText(f"CPU: {cpu}%   MEM: {mem:.1f} MB")


def Path_exists(key: str, pm: ProcessManager) -> bool:
    return pm.ap_bin_exists if key == "ap" else pm.cp_bin_exists


# ═══════════════════════════════════════════════════════════════════════
# 实时滚动图（pyqtgraph）
# ═══════════════════════════════════════════════════════════════════════

class RollingPlot(pg.PlotWidget):
    """60 点滚动折线图"""

    def __init__(self, title: str, unit: str, color_rgb: tuple,
                 y_range: tuple = (0, 100), n_points: int = 80):
        super().__init__()
        self._n  = n_points
        self._xs = list(range(n_points))
        self._ys = deque([0.0] * n_points, maxlen=n_points)

        self.setBackground(C["surface2"])
        self.setTitle(f"<span style='color:{C['text2']};font-size:10px'>{title}</span>")
        self.setLabel("left",  f"<span style='color:{C['text2']}'>{unit}</span>")
        self.getAxis("bottom").hide()
        self.getAxis("left").setStyle(tickFont=QFont("SF Mono", 8), tickLength=-4)
        self.getAxis("left").setPen(pg.mkPen(C["border"]))
        self.getAxis("left").setTextPen(pg.mkPen(C["text2"]))
        self.showGrid(x=False, y=True, alpha=0.3)
        self.setYRange(*y_range)
        self.setMouseEnabled(x=False, y=False)
        self.setMenuEnabled(False)

        pen = pg.mkPen(color=color_rgb, width=1.5)
        brush = pg.mkBrush(color=(*color_rgb, 30))
        self._curve = self.plot(
            self._xs,
            list(self._ys),
            pen=pen,
            fillLevel=y_range[0],
            brush=brush,
        )

        # 最新值 label
        self._latest = pg.TextItem(text="—", color=color_rgb, anchor=(1, 0))
        self._latest.setFont(QFont("SF Mono", 11, QFont.Weight.Bold))
        self.addItem(self._latest)
        self._latest.setPos(n_points - 1, y_range[1])

    def push(self, value: float):
        self._ys.append(value)
        self._curve.setData(self._xs, list(self._ys))
        self._latest.setText(f"{value:.1f}")


# ═══════════════════════════════════════════════════════════════════════
# 通信矩阵图示
# ═══════════════════════════════════════════════════════════════════════

class CommDiagram(QWidget):
    def __init__(self):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(0)

        # CP 节点
        self._cp_node = self._make_node(
            "MyAutoSarCP", "MCU · AUTOSAR CP R25-11",
            "SOME/IP Provider", C["orange"]
        )
        # 箭头区
        arrow = self._make_arrow()
        # AP 节点
        self._ap_node = self._make_node(
            "MyAutoSarAp", "SOC · AUTOSAR AP R25-11",
            "SOME/IP Consumer", C["blue"]
        )

        layout.addStretch()
        layout.addWidget(self._cp_node)
        layout.addSpacing(16)
        layout.addWidget(arrow)
        layout.addSpacing(16)
        layout.addWidget(self._ap_node)
        layout.addStretch()

        self._cp_frame: Optional[QFrame] = self._cp_node.findChild(QFrame, "nodeFrame")
        self._ap_frame: Optional[QFrame] = self._ap_node.findChild(QFrame, "nodeFrame")

    def _make_node(self, name: str, role1: str, role2: str, color: str) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        frame = QFrame()
        frame.setObjectName("nodeFrame")
        frame.setStyleSheet(f"""
            QFrame#nodeFrame {{
                background-color: {C['surface']};
                border: 2px solid {C['border']};
                border-radius: 10px;
                padding: 2px;
                min-width: 160px;
            }}
        """)
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(16, 12, 16, 12)
        fl.setSpacing(3)

        lbl_name = QLabel(name)
        lbl_name.setFont(QFont("SF Mono", 13, QFont.Weight.Bold))
        lbl_name.setStyleSheet(f"color: {color}; background: transparent;")
        lbl_name.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lbl_r1 = QLabel(role1)
        lbl_r1.setStyleSheet(f"color: {C['text2']}; font-size: 10px; background: transparent;")
        lbl_r1.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lbl_r2 = QLabel(role2)
        lbl_r2.setStyleSheet(f"color: {C['text2']}; font-size: 10px; background: transparent;")
        lbl_r2.setAlignment(Qt.AlignmentFlag.AlignCenter)

        fl.addWidget(lbl_name)
        fl.addWidget(lbl_r1)
        fl.addWidget(lbl_r2)

        lay.addWidget(frame)
        return w

    def _make_arrow(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(180)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._flow_lbl = QLabel("━━━━━━━━━━━━▶")
        self._flow_lbl.setFont(QFont("SF Mono", 14))
        self._flow_lbl.setStyleSheet(f"color: {C['green']}; background: transparent;")
        self._flow_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lbl1 = QLabel("SOME/IP  0x1001")
        lbl1.setStyleSheet(f"color: {C['cyan']}; font-size: 11px; font-weight:700; background:transparent;")
        lbl1.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lbl2 = QLabel("UDP 127.0.0.1:30501")
        lbl2.setStyleSheet(f"color: {C['text2']}; font-size: 10px; background:transparent;")
        lbl2.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lbl3 = QLabel("Event 0x8001 · 10 ms")
        lbl3.setStyleSheet(f"color: {C['text2']}; font-size: 10px; background:transparent;")
        lbl3.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._age_lbl = QLabel("最后帧: —")
        self._age_lbl.setStyleSheet(f"color: {C['cyan']}; font-size: 10px; background:transparent;")
        self._age_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lay.addWidget(self._flow_lbl)
        lay.addWidget(lbl1)
        lay.addWidget(lbl2)
        lay.addWidget(lbl3)
        lay.addWidget(self._age_lbl)
        return w

    def set_active(self, active: bool):
        """有数据流动时激活箭头动画"""
        color = C["green"] if active else C["border"]
        self._flow_lbl.setStyleSheet(f"color: {color}; background: transparent;")

    def set_age(self, age_s: Optional[float]):
        if age_s is None:
            self._age_lbl.setText("最后帧: —")
            self._age_lbl.setStyleSheet(f"color: {C['text2']}; font-size: 10px; background:transparent;")
        else:
            self._age_lbl.setText(f"最后帧: {age_s:.1f}s 前")
            color = C["danger"] if age_s > 2 else C["cyan"]
            self._age_lbl.setStyleSheet(f"color: {color}; font-size: 10px; background:transparent;")

    def set_node_status(self, key: str, running: bool):
        # 找到对应 frame 设置边框颜色
        pass  # 通过 ProcessPanel badge 已经足够


# ═══════════════════════════════════════════════════════════════════════
# SOME/IP 帧详情面板
# ═══════════════════════════════════════════════════════════════════════

class FrameDetailPanel(QGroupBox):
    def __init__(self):
        super().__init__("📦  最新 SOME/IP 帧")
        layout = QGridLayout(self)
        layout.setSpacing(6)

        fields = [
            ("Service ID",   "fi_svc"),
            ("Event ID",     "fi_evt"),
            ("Session ID",   "fi_sid"),
            ("Length",       "fi_len"),
            ("E2E CRC",      "fi_crc"),
            ("E2E Counter",  "fi_cnt"),
            ("Msg Type",     "fi_mtype"),
            ("时间戳",        "fi_ts"),
        ]
        self._vals: dict[str, QLabel] = {}
        for i, (label, key) in enumerate(fields):
            row, col = divmod(i, 2)
            cell = QWidget()
            cell.setStyleSheet(f"""
                background-color: {C['surface']};
                border: 1px solid {C['border']};
                border-radius: 6px;
            """)
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(8, 6, 8, 6)
            cl.setSpacing(2)
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color: {C['text2']}; font-size: 10px; background:transparent;")
            val = QLabel("—")
            val.setProperty("role", "frame_val")
            val.setStyleSheet(f"color: {C['cyan']}; font-size: 13px; font-weight:700; background:transparent;")
            cl.addWidget(lbl)
            cl.addWidget(val)
            layout.addWidget(cell, row, col)
            self._vals[key] = val

        # E2E 状态
        e2e_cell = QWidget()
        e2e_cell.setStyleSheet(f"background-color: {C['surface']}; border: 1px solid {C['border']}; border-radius: 6px;")
        e2e_l = QHBoxLayout(e2e_cell)
        e2e_l.setContentsMargins(8, 6, 8, 6)
        self._e2e_badge = QLabel("● E2E OK")
        self._e2e_badge.setProperty("role", "badge_ok")
        e2e_l.addWidget(QLabel("E2E 状态"))
        e2e_l.addStretch()
        e2e_l.addWidget(self._e2e_badge)
        layout.addWidget(e2e_cell, 4, 0, 1, 2)

    def update_frame(self, sig: VehicleSignal):
        def h(n, w=4): return f"0x{n:0{w}X}"
        mtypes = {0:"REQUEST", 1:"REQUEST_NR", 2:"NOTIFICATION", 0x80:"RESPONSE", 0x81:"ERROR"}
        self._vals["fi_svc"].setText(h(sig.service_id))
        self._vals["fi_evt"].setText(h(sig.method_id))
        self._vals["fi_sid"].setText(str(sig.session_id))
        self._vals["fi_len"].setText(f"{sig.length} B")
        self._vals["fi_crc"].setText(h(sig.e2e_crc, 2))
        self._vals["fi_cnt"].setText(str(sig.e2e_counter))
        self._vals["fi_mtype"].setText(mtypes.get(sig.msg_type, h(sig.msg_type, 2)))
        self._vals["fi_ts"].setText(datetime.fromtimestamp(sig.recv_time).strftime("%H:%M:%S.%f")[:-3])

        if sig.e2e_ok:
            self._e2e_badge.setText("● E2E OK")
            self._e2e_badge.setStyleSheet(f"""
                color: {C['green']}; background-color: rgba(63,185,80,0.15);
                border: 1px solid {C['green']}; border-radius: 10px; padding: 2px 8px; font-size:11px; font-weight:700;
            """)
        else:
            self._e2e_badge.setText("✗ E2E FAIL")
            self._e2e_badge.setStyleSheet(f"""
                color: {C['red']}; background-color: rgba(218,54,51,0.15);
                border: 1px solid {C['red']}; border-radius: 10px; padding: 2px 8px; font-size:11px; font-weight:700;
            """)


# ═══════════════════════════════════════════════════════════════════════
# 主窗口
# ═══════════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AUTOSAR IPC 可视化监控面板  v1.0")
        self.resize(1480, 900)
        self.setMinimumSize(1200, 750)

        # ── 核心对象 ──
        self._bridge = _Bridge()
        self._pm     = ProcessManager()
        self._ds     = DataSource()
        self._last_frame_time: Optional[float] = None

        self._ds.on_frame = lambda sig: self._bridge.frame_received.emit(sig)
        self._ds.on_error = lambda msg: self._statusbar_msg(f"[DataSource] {msg}", "warn")
        self._pm.on_log    = lambda k, l, ts: self._bridge.log_received.emit(k, l, ts)
        self._pm.on_status = lambda k, r, pid: self._bridge.status_changed.emit(k, r, pid)

        # ── 信号连接 ──
        self._bridge.frame_received.connect(self._on_frame)
        self._bridge.log_received.connect(self._on_log)
        self._bridge.status_changed.connect(self._on_status_changed)

        self._build_ui()
        self._start_timers()

        # 启动 UDP 监听
        ok = self._ds.start()
        if ok:
            self._statusbar_msg(f"UDP 监听 127.0.0.1:30501 已启动", "ok")
        else:
            self._statusbar_msg("UDP 监听启动失败（端口可能被占用）", "warn")

    # ── UI 构建 ────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 顶部 Header
        root.addWidget(self._build_header())

        # 主分割区
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)

        # 左侧边栏
        sidebar = self._build_sidebar()
        sidebar.setFixedWidth(280)
        splitter.addWidget(sidebar)

        # 右侧主区
        splitter.addWidget(self._build_main_area())
        splitter.setStretchFactor(1, 1)

        root.addWidget(splitter, 1)

        # 状态栏
        self._statusbar = QStatusBar()
        self._statusbar.setStyleSheet(f"""
            QStatusBar {{
                background-color: {C['surface']};
                border-top: 1px solid {C['border']};
                color: {C['text2']};
                font-size: 11px;
            }}
        """)
        self.setStatusBar(self._statusbar)
        self._statusbar_msg("就绪", "ok")

    def _build_header(self) -> QWidget:
        h = QWidget()
        h.setFixedHeight(52)
        h.setStyleSheet(f"background-color: {C['surface']}; border-bottom: 1px solid {C['border']};")
        lay = QHBoxLayout(h)
        lay.setContentsMargins(16, 0, 16, 0)

        icon = QLabel("⚙")
        icon.setFont(QFont("Arial", 20))
        icon.setStyleSheet(f"color: {C['blue']}; background:transparent;")

        title = QLabel("AUTOSAR IPC 可视化监控面板")
        title.setFont(QFont("SF Mono", 15, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C['blue']}; background:transparent;")

        sub = QLabel("MyAutoSarAp (AP/SOC)  ←→  SOME/IP UDP  ←→  MyAutoSarCP (CP/MCU)")
        sub.setStyleSheet(f"color: {C['text2']}; font-size: 11px; background:transparent;")

        self._clock_lbl = QLabel("--:--:--")
        self._clock_lbl.setFont(QFont("SF Mono", 13))
        self._clock_lbl.setStyleSheet(f"color: {C['text2']}; background:transparent;")

        self._ds_badge = QLabel("● UDP 监听")
        self._ds_badge.setStyleSheet(f"color: {C['green']}; font-size: 11px; font-weight:700; background:transparent;")

        lay.addWidget(icon)
        lay.addSpacing(8)
        lay.addWidget(title)
        lay.addSpacing(16)
        lay.addWidget(sub)
        lay.addStretch()
        lay.addWidget(self._ds_badge)
        lay.addSpacing(16)
        lay.addWidget(self._clock_lbl)
        return h

    def _build_sidebar(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background-color: {C['surface']}; border-right: 1px solid {C['border']};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)

        # ── Demo 控制 ──
        demo_box = QGroupBox("🚀  Demo 控制")
        db_lay = QVBoxLayout(demo_box)
        db_lay.setSpacing(6)

        self._demo_start_btn = QPushButton("▶  启动完整 Demo  (AP + CP)")
        self._demo_start_btn.setProperty("btntype", "demo_start")
        self._demo_start_btn.setMinimumHeight(40)
        self._demo_start_btn.clicked.connect(self._start_demo)

        self._demo_stop_btn = QPushButton("⬛  停止 Demo")
        self._demo_stop_btn.setProperty("btntype", "demo_stop")
        self._demo_stop_btn.setMinimumHeight(34)
        self._demo_stop_btn.clicked.connect(self._stop_demo)

        clear_btn = QPushButton("↺  清空统计")
        clear_btn.setProperty("btntype", "blue")
        clear_btn.clicked.connect(self._clear_stats)

        db_lay.addWidget(self._demo_start_btn)
        db_lay.addWidget(self._demo_stop_btn)
        db_lay.addWidget(clear_btn)
        lay.addWidget(demo_box)

        # ── AP 进程 ──
        self._ap_panel = ProcessPanel("ap", "🔷  MyAutoSarAp (SOC)", C["blue"], self._pm, self._bridge)
        lay.addWidget(self._ap_panel)

        # ── CP 进程 ──
        self._cp_panel = ProcessPanel("cp", "🔶  MyAutoSarCP (MCU)", C["orange"], self._pm, self._bridge)
        lay.addWidget(self._cp_panel)

        # ── 信号订阅 ──
        sub_box = QGroupBox("📡  信号订阅")
        sb_lay = QVBoxLayout(sub_box)
        sb_lay.setSpacing(4)
        sub_defs = [
            ("vehicle_speed",  "车速 (speed)"),
            ("engine_rpm",     "转速 (rpm)"),
            ("brake_pedal",    "制动 (brake)"),
            ("steering_angle", "转角 (steering)"),
            ("door_status",    "车门 (door)"),
            ("fuel_level",     "燃油 (fuel)"),
        ]
        self._sub_checks: dict[str, QCheckBox] = {}
        for key, label in sub_defs:
            cb = QCheckBox(label)
            cb.setChecked(True)
            cb.stateChanged.connect(lambda state, k=key: self._ds.subscribe(k, bool(state)))
            sb_lay.addWidget(cb)
            self._sub_checks[key] = cb
        lay.addWidget(sub_box)

        # ── 统计 ──
        stat_box = QGroupBox("📊  通信统计")
        sg = QGridLayout(stat_box)
        sg.setSpacing(6)
        self._stat_frames = StatCard("总帧数",  C["green"])
        self._stat_fps    = StatCard("帧率/s",  C["blue"])
        self._stat_e2e    = StatCard("E2E错误", C["red"])
        self._stat_gaps   = StatCard("序列跳变", C["orange"])
        sg.addWidget(self._stat_frames, 0, 0)
        sg.addWidget(self._stat_fps,    0, 1)
        sg.addWidget(self._stat_e2e,    1, 0)
        sg.addWidget(self._stat_gaps,   1, 1)
        lay.addWidget(stat_box)

        lay.addStretch()
        return w

    def _build_main_area(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)

        # ── Tab 区 ──
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self._tabs.addTab(self._build_tab_signals(), "  📈 实时信号  ")
        self._tabs.addTab(self._build_tab_charts(),  "  📉 趋势图    ")
        self._tabs.addTab(self._build_tab_comm(),    "  🔗 通信状态  ")
        self._tabs.addTab(self._build_tab_logs(),    "  📋 进程日志  ")

        lay.addWidget(self._tabs, 1)
        return w

    # ── Tab: 实时信号 ─────────────────────────────────────────────────

    def _build_tab_signals(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(12)

        # Gauge 行
        gauge_row = QHBoxLayout()
        gauge_row.setSpacing(10)

        self._g_speed = GaugeCard("车速",     "km/h",  200,  C["blue"],   1)
        self._g_rpm   = GaugeCard("转速",     "RPM",   8000, C["green"],  0)
        self._g_fuel  = GaugeCard("燃油",     "%",     100,  C["yellow"], 1)
        self._g_steer = GaugeCard("方向盘转角", "deg",  540,  C["purple"], 1)
        self._g_brake = GaugeCard("制动踏板",  "0/1",   1,    C["orange"], 0)
        self._g_door  = GaugeCard("车门状态",  "mask",  15,   C["cyan"],   0)

        for g in (self._g_speed, self._g_rpm, self._g_fuel,
                  self._g_steer, self._g_brake, self._g_door):
            gauge_row.addWidget(g)
        lay.addLayout(gauge_row)

        # 帧详情
        lay.addWidget(self._build_frame_detail_row())
        lay.addStretch()
        return w

    def _build_frame_detail_row(self) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setSpacing(12)
        row.setContentsMargins(0, 0, 0, 0)

        self._frame_panel = FrameDetailPanel()
        row.addWidget(self._frame_panel, 1)

        # 右侧：E2E 总览 + fps 大字
        info = QGroupBox("📡  连接总览")
        il = QVBoxLayout(info)
        il.setSpacing(10)

        self._big_fps = QLabel("0.0")
        self._big_fps.setFont(QFont("SF Mono", 40, QFont.Weight.Bold))
        self._big_fps.setStyleSheet(f"color: {C['blue']}; background:transparent;")
        self._big_fps.setAlignment(Qt.AlignmentFlag.AlignCenter)
        fps_lbl = QLabel("帧率  fps")
        fps_lbl.setStyleSheet(f"color: {C['text2']}; font-size: 11px; background:transparent;")
        fps_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._e2e_big = QLabel("● E2E OK")
        self._e2e_big.setStyleSheet(f"""
            color: {C['green']}; background-color: rgba(63,185,80,0.15);
            border: 2px solid {C['green']}; border-radius: 12px;
            padding: 6px 16px; font-size: 14px; font-weight: 700;
        """)
        self._e2e_big.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._age_lbl_big = QLabel("等待数据...")
        self._age_lbl_big.setStyleSheet(f"color: {C['text2']}; font-size: 11px; background:transparent;")
        self._age_lbl_big.setAlignment(Qt.AlignmentFlag.AlignCenter)

        il.addStretch()
        il.addWidget(self._big_fps)
        il.addWidget(fps_lbl)
        il.addSpacing(8)
        il.addWidget(self._e2e_big)
        il.addSpacing(4)
        il.addWidget(self._age_lbl_big)
        il.addStretch()

        row.addWidget(info)
        return w

    # ── Tab: 趋势图 ───────────────────────────────────────────────────

    def _build_tab_charts(self) -> QWidget:
        w = QWidget()
        lay = QGridLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(12)

        self._plot_speed = RollingPlot("车速",     "km/h", PLOT_COLORS["speed"],  (0, 200))
        self._plot_rpm   = RollingPlot("发动机转速", "RPM",  PLOT_COLORS["rpm"],    (0, 8000))
        self._plot_fuel  = RollingPlot("燃油液位",  "%",    PLOT_COLORS["fuel"],   (0, 100))
        self._plot_steer = RollingPlot("方向盘转角", "deg",  PLOT_COLORS["steer"], (-540, 540))

        lay.addWidget(self._plot_speed, 0, 0)
        lay.addWidget(self._plot_rpm,   0, 1)
        lay.addWidget(self._plot_fuel,  1, 0)
        lay.addWidget(self._plot_steer, 1, 1)
        lay.setRowStretch(0, 1)
        lay.setRowStretch(1, 1)
        return w

    # ── Tab: 通信状态 ─────────────────────────────────────────────────

    def _build_tab_comm(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(16)

        self._comm_diagram = CommDiagram()
        lay.addWidget(self._comm_diagram)

        # 协议参数表
        proto_box = QGroupBox("🛡️  SOME/IP 协议参数")
        pl = QGridLayout(proto_box)
        pl.setSpacing(8)
        params = [
            ("Service ID",       "0x1001  (VehicleSignalService)"),
            ("Event ID",         "0x8001"),
            ("传输协议",          "UDP · 127.0.0.1:30501"),
            ("帧周期",            "10 ms  (100 fps 理论值)"),
            ("Payload",          "20 bytes (VehicleSignalPayload_t)"),
            ("E2E 保护",          "Profile 2 · CRC8 · 多项式 0x1D"),
            ("Session ID",       "单调递增  0x0001 ~ 0xFFFF"),
            ("数据字节序",         "Big-Endian"),
        ]
        for i, (k, v) in enumerate(params):
            row, col = divmod(i, 2)
            cell = QWidget()
            cell.setStyleSheet(f"background:{C['surface']}; border:1px solid {C['border']}; border-radius:6px;")
            cl = QHBoxLayout(cell)
            cl.setContentsMargins(10, 6, 10, 6)
            lk = QLabel(k + ":")
            lk.setStyleSheet(f"color:{C['text2']};font-size:11px;background:transparent;min-width:100px;")
            lv = QLabel(v)
            lv.setStyleSheet(f"color:{C['cyan']};font-size:12px;font-weight:700;background:transparent;")
            cl.addWidget(lk)
            cl.addWidget(lv)
            cl.addStretch()
            pl.addWidget(cell, row, col)
        lay.addWidget(proto_box)
        lay.addStretch()
        return w

    # ── Tab: 日志 ─────────────────────────────────────────────────────

    def _build_tab_logs(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        self._log_tabs = QTabWidget()
        self._log_tabs.setDocumentMode(True)

        self._ap_log = QTextEdit()
        self._ap_log.setReadOnly(True)
        self._ap_log.setPlaceholderText("AP (SOC) 进程日志将在此处显示...")

        self._cp_log = QTextEdit()
        self._cp_log.setReadOnly(True)
        self._cp_log.setPlaceholderText("CP (MCU) 进程日志将在此处显示...")

        self._log_tabs.addTab(self._ap_log, "  🔷 AP (SOC)  ")
        self._log_tabs.addTab(self._cp_log, "  🔶 CP (MCU)  ")
        lay.addWidget(self._log_tabs, 1)

        # 清空按钮
        cl_btn = QPushButton("清空日志")
        cl_btn.setProperty("btntype", "blue")
        cl_btn.setFixedWidth(100)
        cl_btn.clicked.connect(self._clear_logs)
        lay.addWidget(cl_btn, 0, Qt.AlignmentFlag.AlignRight)
        return w

    # ── 事件处理 ──────────────────────────────────────────────────────

    def _on_frame(self, sig: VehicleSignal):
        self._last_frame_time = sig.recv_time

        # 仪表盘
        self._g_speed.update_value(sig.vehicle_speed_kmh)
        self._g_rpm.update_value(sig.engine_rpm)
        self._g_fuel.update_value(sig.fuel_level_pct)
        self._g_steer.update_value(abs(sig.steering_angle_deg))
        self._g_brake.update_value(sig.brake_pedal)
        self._g_door.update_value(sig.door_status)
        for g in (self._g_speed, self._g_rpm, self._g_fuel,
                  self._g_steer, self._g_brake, self._g_door):
            g.flash(sig.e2e_ok)

        # 趋势图
        self._plot_speed.push(sig.vehicle_speed_kmh)
        self._plot_rpm.push(sig.engine_rpm)
        self._plot_fuel.push(sig.fuel_level_pct)
        self._plot_steer.push(sig.steering_angle_deg)

        # 帧详情
        self._frame_panel.update_frame(sig)

        # E2E 大字
        if sig.e2e_ok:
            self._e2e_big.setText("● E2E OK")
            self._e2e_big.setStyleSheet(f"""
                color:{C['green']}; background-color:rgba(63,185,80,0.15);
                border:2px solid {C['green']}; border-radius:12px;
                padding:6px 16px; font-size:14px; font-weight:700;
            """)
        else:
            self._e2e_big.setText("✗ E2E FAIL")
            self._e2e_big.setStyleSheet(f"""
                color:{C['red']}; background-color:rgba(218,54,51,0.15);
                border:2px solid {C['red']}; border-radius:12px;
                padding:6px 16px; font-size:14px; font-weight:700;
            """)

        # 通信图流动激活
        self._comm_diagram.set_active(True)

    def _on_log(self, key: str, line: str, ts: str):
        log_widget = self._ap_log if key == "ap" else self._cp_log
        color = C["blue"] if key == "ap" else C["orange"]
        html = (f'<span style="color:{C["text2"]}">{ts} </span>'
                f'<span style="color:{color};font-weight:700">[{key.upper()}]</span> '
                f'<span style="color:{C["text"]}">{line}</span>')
        log_widget.append(html)
        # 自动滚动到底部
        cursor = log_widget.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        log_widget.setTextCursor(cursor)

    def _on_status_changed(self, key: str, running: bool, pid):
        action = "started" if running else "stopped"
        pid_str = f"(PID={pid})" if pid else ""
        self._statusbar_msg(f"{key.upper()} {action} {pid_str}", "ok" if running else "warn")

    # ── Demo 控制 ────────────────────────────────────────────────────

    def _start_demo(self):
        self._pm.stop_all()
        time.sleep(0.3)
        self._ds.reset_stats()
        r_ap = self._pm.start("ap")
        time.sleep(0.3)
        r_cp = self._pm.start("cp")
        self._statusbar_msg(f"Demo 启动: {r_ap} | {r_cp}", "ok")

    def _stop_demo(self):
        self._pm.stop_all()
        self._statusbar_msg("Demo 已停止", "warn")

    def _clear_stats(self):
        self._ds.reset_stats()
        self._stat_frames.set_value(0)
        self._stat_fps.set_value(0)
        self._stat_e2e.set_value(0)
        self._stat_gaps.set_value(0)

    def _clear_logs(self):
        self._ap_log.clear()
        self._cp_log.clear()

    # ── 定时器 ───────────────────────────────────────────────────────

    def _start_timers(self):
        # 时钟
        t_clock = QTimer(self)
        t_clock.timeout.connect(self._tick_clock)
        t_clock.start(1000)

        # 统计 + 资源（1s）
        t_stats = QTimer(self)
        t_stats.timeout.connect(self._tick_stats)
        t_stats.start(1000)

        # 通信图动画（500ms 渐暗）
        self._comm_active_ts = 0.0
        t_comm = QTimer(self)
        t_comm.timeout.connect(self._tick_comm)
        t_comm.start(500)

    def _tick_clock(self):
        self._clock_lbl.setText(datetime.now().strftime("%H:%M:%S"))

    def _tick_stats(self):
        s = self._ds.stats
        self._stat_frames.set_value(s.total_frames)
        self._stat_fps.set_value(s.fps)
        self._stat_e2e.set_value(s.e2e_errors)
        self._stat_gaps.set_value(s.session_gaps)
        self._big_fps.setText(str(s.fps))

        # 资源
        self._ap_panel.update_resource(self._pm.get_resource("ap"))
        self._cp_panel.update_resource(self._pm.get_resource("cp"))

    def _tick_comm(self):
        if self._last_frame_time:
            age = time.time() - self._last_frame_time
            self._comm_diagram.set_age(age)
            self._comm_diagram.set_active(age < 1.0)
            txt = f"最后接收: {age:.1f}s 前 · 总帧 {self._ds.stats.total_frames}"
            self._age_lbl_big.setText(txt)
        else:
            self._comm_diagram.set_age(None)
            self._comm_diagram.set_active(False)

    # ── 状态栏 ──────────────────────────────────────────────────────

    def _statusbar_msg(self, msg: str, level: str = "ok"):
        colors = {"ok": C["green"], "warn": C["yellow"], "err": C["red"]}
        c = colors.get(level, C["text2"])
        self._statusbar.setStyleSheet(f"""
            QStatusBar {{
                background-color: {C['surface']};
                border-top: 1px solid {C['border']};
                color: {c};
                font-size: 11px;
            }}
        """)
        ts = datetime.now().strftime("%H:%M:%S")
        self._statusbar.showMessage(f"[{ts}]  {msg}")

    # ── 关闭 ────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self._pm.stop_all()
        self._ds.stop()
        event.accept()


# ═══════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════

def main():
    pg.setConfigOptions(
        antialias=True,
        useOpenGL=False,   # macOS 稳定性
        enableExperimental=False,
    )
    pg.setConfigOption("background", C["surface2"])
    pg.setConfigOption("foreground", C["text2"])

    app = QApplication(sys.argv)
    app.setApplicationName("AUTOSAR IPC Monitor")
    app.setStyleSheet(DARK_QSS)

    # 全局字体
    font = QFont("SF Mono", 12)
    app.setFont(font)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
