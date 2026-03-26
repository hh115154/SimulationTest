"""
styles.py — 全局暗色主题 QSS + 调色板
后端工程师：只需关注 data_interface.py，不用改这里
"""

# ── 颜色常量（供 Python 代码直接引用） ─────────────────────────────────────
C = {
    "bg":        "#0d1117",   # 主背景
    "surface":   "#161b22",   # 卡片背景
    "surface2":  "#21262d",   # 次级面板
    "border":    "#30363d",   # 边框
    "text":      "#c9d1d9",   # 主文字
    "text2":     "#8b949e",   # 次文字
    "green":     "#3fb950",   # 成功/运行
    "blue":      "#58a6ff",   # 信息/主色
    "orange":    "#f78166",   # 警告
    "red":       "#da3633",   # 错误/停止
    "yellow":    "#e3b341",   # 注意
    "cyan":      "#76e3ea",   # 高亮
    "purple":    "#bc8cff",   # 附加
}

# ── pyqtgraph 曲线颜色 ──────────────────────────────────────────────────────
PLOT_COLORS = {
    "speed":   (88,  166, 255),   # blue
    "rpm":     (63,  185, 80),    # green
    "fuel":    (227, 179, 65),    # yellow
    "steer":   (188, 140, 255),   # purple
}

# ── 主 QSS ──────────────────────────────────────────────────────────────────
DARK_QSS = f"""
/* ── 全局 ── */
QWidget {{
    background-color: {C['bg']};
    color: {C['text']};
    font-family: "SF Mono", "Menlo", "Cascadia Code", "Consolas", monospace;
    font-size: 12px;
}}

/* ── 主窗口 ── */
QMainWindow {{
    background-color: {C['bg']};
}}

/* ── GroupBox ── */
QGroupBox {{
    background-color: {C['surface']};
    border: 1px solid {C['border']};
    border-radius: 8px;
    margin-top: 8px;
    padding: 8px 8px 8px 8px;
    font-size: 11px;
    font-weight: 600;
    color: {C['text2']};
    text-transform: uppercase;
    letter-spacing: 1px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    top: -1px;
    padding: 0 4px;
    background-color: {C['surface']};
}}

/* ── QPushButton ── */
QPushButton {{
    background-color: {C['surface2']};
    color: {C['text']};
    border: 1px solid {C['border']};
    border-radius: 6px;
    padding: 5px 14px;
    font-weight: 600;
    font-size: 12px;
    min-width: 64px;
}}
QPushButton:hover {{
    background-color: #2d333b;
    border-color: {C['blue']};
}}
QPushButton:pressed {{
    background-color: #1c2128;
}}
QPushButton:disabled {{
    color: {C['border']};
    border-color: {C['surface2']};
}}

/* 绿色按钮 */
QPushButton[btntype="green"] {{
    background-color: rgba(63, 185, 80, 0.15);
    color: {C['green']};
    border-color: {C['green']};
}}
QPushButton[btntype="green"]:hover {{
    background-color: rgba(63, 185, 80, 0.25);
}}

/* 红色按钮 */
QPushButton[btntype="red"] {{
    background-color: rgba(218, 54, 51, 0.15);
    color: {C['red']};
    border-color: {C['red']};
}}
QPushButton[btntype="red"]:hover {{
    background-color: rgba(218, 54, 51, 0.25);
}}

/* 蓝色按钮 */
QPushButton[btntype="blue"] {{
    background-color: rgba(88, 166, 255, 0.15);
    color: {C['blue']};
    border-color: {C['blue']};
}}
QPushButton[btntype="blue"]:hover {{
    background-color: rgba(88, 166, 255, 0.25);
}}

/* 大型主按钮 */
QPushButton[btntype="demo_start"] {{
    background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
        stop:0 rgba(35,134,54,0.3), stop:1 rgba(31,111,235,0.3));
    color: {C['green']};
    border: 1px solid {C['green']};
    border-radius: 8px;
    padding: 10px 20px;
    font-size: 14px;
    font-weight: 700;
    letter-spacing: 0.5px;
}}
QPushButton[btntype="demo_start"]:hover {{
    background: rgba(63, 185, 80, 0.25);
}}

QPushButton[btntype="demo_stop"] {{
    background-color: rgba(218, 54, 51, 0.15);
    color: {C['red']};
    border: 1px solid {C['red']};
    border-radius: 8px;
    padding: 8px 20px;
    font-size: 13px;
    font-weight: 700;
}}
QPushButton[btntype="demo_stop"]:hover {{
    background-color: rgba(218, 54, 51, 0.25);
}}

/* ── QLabel ── */
QLabel {{
    background: transparent;
    color: {C['text']};
}}
QLabel[role="title"] {{
    font-size: 11px;
    font-weight: 600;
    color: {C['text2']};
    text-transform: uppercase;
    letter-spacing: 1px;
}}
QLabel[role="value_big"] {{
    font-size: 26px;
    font-weight: 700;
    color: {C['blue']};
}}
QLabel[role="value_big_green"] {{
    font-size: 26px;
    font-weight: 700;
    color: {C['green']};
}}
QLabel[role="value_big_yellow"] {{
    font-size: 26px;
    font-weight: 700;
    color: {C['yellow']};
}}
QLabel[role="value_big_purple"] {{
    font-size: 26px;
    font-weight: 700;
    color: {C['purple']};
}}
QLabel[role="unit"] {{
    font-size: 11px;
    color: {C['text2']};
}}
QLabel[role="stat_value"] {{
    font-size: 22px;
    font-weight: 700;
    color: {C['green']};
}}
QLabel[role="stat_label"] {{
    font-size: 10px;
    color: {C['text2']};
}}
QLabel[role="badge_ok"] {{
    background-color: rgba(63, 185, 80, 0.15);
    color: {C['green']};
    border: 1px solid {C['green']};
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}}
QLabel[role="badge_err"] {{
    background-color: rgba(218, 54, 51, 0.15);
    color: {C['red']};
    border: 1px solid {C['red']};
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}}
QLabel[role="badge_run"] {{
    background-color: rgba(63, 185, 80, 0.12);
    color: {C['green']};
    border: 1px solid {C['green']};
    border-radius: 10px;
    padding: 2px 10px;
    font-size: 11px;
    font-weight: 600;
}}
QLabel[role="badge_stop"] {{
    background-color: rgba(218, 54, 51, 0.12);
    color: {C['red']};
    border: 1px solid {C['red']};
    border-radius: 10px;
    padding: 2px 10px;
    font-size: 11px;
    font-weight: 600;
}}
QLabel[role="header_title"] {{
    font-size: 16px;
    font-weight: 700;
    color: {C['blue']};
}}
QLabel[role="meta"] {{
    font-size: 11px;
    color: {C['text2']};
}}
QLabel[role="frame_val"] {{
    font-family: "SF Mono", "Menlo", monospace;
    font-size: 13px;
    font-weight: 600;
    color: {C['cyan']};
}}

/* ── QTextEdit（日志） ── */
QTextEdit {{
    background-color: #0d1117;
    color: {C['text']};
    border: 1px solid {C['border']};
    border-radius: 6px;
    font-family: "SF Mono", "Menlo", monospace;
    font-size: 11px;
    selection-background-color: #2d333b;
}}

/* ── QTabWidget ── */
QTabWidget::pane {{
    border: 1px solid {C['border']};
    border-radius: 0 6px 6px 6px;
    background-color: {C['surface']};
}}
QTabBar::tab {{
    background-color: {C['surface2']};
    color: {C['text2']};
    border: 1px solid {C['border']};
    padding: 5px 16px;
    margin-right: 2px;
    border-bottom: none;
    border-radius: 4px 4px 0 0;
    font-size: 12px;
}}
QTabBar::tab:selected {{
    background-color: {C['surface']};
    color: {C['text']};
    border-bottom-color: {C['surface']};
    border-top-color: {C['blue']};
    border-top-width: 2px;
}}
QTabBar::tab:hover:!selected {{
    background-color: #2d333b;
    color: {C['text']};
}}

/* ── QScrollBar ── */
QScrollBar:vertical {{
    background: {C['surface']};
    width: 6px;
    border-radius: 3px;
}}
QScrollBar::handle:vertical {{
    background: {C['border']};
    border-radius: 3px;
    min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

/* ── QCheckBox ── */
QCheckBox {{
    color: {C['text']};
    font-size: 12px;
    spacing: 6px;
}}
QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {C['border']};
    border-radius: 3px;
    background-color: {C['surface2']};
}}
QCheckBox::indicator:checked {{
    background-color: {C['blue']};
    border-color: {C['blue']};
    image: none;
}}
QCheckBox::indicator:hover {{
    border-color: {C['blue']};
}}

/* ── QProgressBar ── */
QProgressBar {{
    background-color: {C['surface2']};
    border: none;
    border-radius: 3px;
    height: 5px;
    text-align: center;
}}
QProgressBar::chunk {{
    border-radius: 3px;
    background-color: {C['blue']};
}}
QProgressBar[color="green"]::chunk {{ background-color: {C['green']}; }}
QProgressBar[color="yellow"]::chunk {{ background-color: {C['yellow']}; }}
QProgressBar[color="orange"]::chunk {{ background-color: {C['orange']}; }}
QProgressBar[color="red"]::chunk {{ background-color: {C['red']}; }}

/* ── QSplitter ── */
QSplitter::handle {{
    background-color: {C['border']};
}}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical   {{ height: 1px; }}

/* ── QFrame separator ── */
QFrame[frameShape="4"],  /* HLine */
QFrame[frameShape="5"]   /* VLine */
{{
    color: {C['border']};
}}
"""
