# -*- coding: utf-8 -*-
"""验收脚本 · 底部按钮栏的短竖分隔线（攒批开关 ↔ 开始打印）

背景（2026-10 用户反馈）：底部按钮栏里「🖐 攒批模式」与「▶ 开始打印」紧挨着，
两个控件被读成同一组 —— 主操作（开始打印）属于整页，不该看起来像是攒批开关的兄弟。
对策：两者之间插一道 **1px 宽、22px 高的短竖线** + 两侧留白，纯视觉分组。

设计约束（本脚本逐条把关）：
  1. **颜色由 QSS 给**（`QFrame#vSepBottomBar`，深/浅两根主题表各一条）。
     不用 `QFrame.VLine` 形状 —— 它按调色板画线，深色主题下几乎看不见；
     若哪天 QSS 那条规则丢了，分隔线会退化成窗口底色（肉眼看不见）⇒ 运行时像素判据拦住。
  2. **纯装饰**：无信号连接、无业务逻辑，不随攒批状态显隐（它分隔的是左右两组，不是某个开关）。
  3. **位置**：`_setup_button_bar` 里夹在「攒批开关/纸叠入口」与「开始打印」之间。

    python tests/verify_bottom_bar_separator.py     # exit 0 = 全绿
"""
import io
import os
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="hn_btnbar_sep_")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
# 离屏渲染需要真字体（本机 PySide6 不带字体目录）：挂上系统字体，避免文字画成空框影响布局宽度
for _f in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/segoeui.ttf"):
    if os.path.isfile(_f):
        QFontDatabase.addApplicationFont(_f)

import paths  # noqa: E402
from theme_manager import ThemeManager  # noqa: E402
import gui as _gui  # noqa: E402

_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

SEP_NAME = "vSepBottomBar"
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
_lines = _src.splitlines()

# ────────────────────────────────────────────────────────────
print("=== ① 静态：gui.py 里的分隔线控件 ===")

# _setup_button_bar 的源码块（只看这一段，避免别处同名误判）
_bar_start = _src.index("def _setup_button_bar")
_bar_end = _src.index("    # ---- 数据 → UI ----", _bar_start)
_bar = _src[_bar_start:_bar_end]

check(f"构造了 QFrame 且 objectName = {SEP_NAME!r}",
      f'self._btnbar_sep = QFrame()' in _bar and f'setObjectName("{SEP_NAME}")' in _bar)
check("尺寸固定为 1px × 22px（短竖线，不是贯穿整栏的边框）",
      "self._btnbar_sep.setFixedSize(1, 22)" in _bar)
check("用 QFrame.NoFrame + QSS 上色（不用 VLine 形状：调色板画线在深色主题下看不见）",
      "self._btnbar_sep.setFrameShape(QFrame.NoFrame)" in _bar
      and "setFrameShape(QFrame.VLine)" not in _bar)
check("声明 WA_StyledBackground（保证 QSS 的 background-color 真被绘制）",
      "setAttribute(Qt.WA_StyledBackground, True)" in _bar)

_conn = [ln.strip() for ln in _bar.splitlines()
         if "_btnbar_sep" in ln and (".connect(" in ln or "clicked" in ln)]
check("纯装饰：分隔线不接任何信号/不带业务", _conn == [], f"{_conn}")

# 布局顺序：攒批开关 → 纸叠入口 → 分隔线 → 开始打印
_i_chk = _bar.find("self._chk_stack_mode = ThemedCheckBox")
_i_stack = _bar.find("self._btn_stack = QPushButton")
_i_sep = _bar.find("self._btnbar_sep = QFrame()")
_i_start = _bar.find('self._btn_start = QPushButton("▶ 开始打印")')
check("布局顺序：攒批开关 → 纸叠入口 → 分隔线 → 开始打印",
      -1 not in (_i_chk, _i_stack, _i_sep, _i_start) and _i_chk < _i_stack < _i_sep < _i_start,
      f"chk={_i_chk} stack={_i_stack} sep={_i_sep} start={_i_start}")
check("两侧各加了显式留白（layout.addSpacing）",
      len(re.findall(r"layout\.addSpacing\(\d+\)", _bar)) >= 2)

# ────────────────────────────────────────────────────────────
print("\n=== ② 静态：两根主题表都给了这条规则（颜色 + 去掉边框）===")

for theme in ("dark", "light"):
    _qss_path = os.path.join(LOCAL_TOOL, f"styles_{theme}.qss")
    _qss = open(_qss_path, encoding="utf-8").read()
    m = re.search(rf"QFrame#{SEP_NAME}\s*\{{(.*?)\}}", _qss, re.S)
    body = m.group(1) if m else ""
    check(f"styles_{theme}.qss 定义了 QFrame#{SEP_NAME} 的 background-color",
          m is not None and "background-color:" in body)
    check(f"styles_{theme}.qss 里该规则 border: none（不留 QSS 默认边框）",
          "border: none" in body)

# ────────────────────────────────────────────────────────────
print("\n=== ③ 运行时（离屏，深/浅各一遍）：尺寸、留白、像素可见 ===")

_dark = open(os.path.join(LOCAL_TOOL, "styles_dark.qss"), encoding="utf-8").read()
_light = open(os.path.join(LOCAL_TOOL, "styles_light.qss"), encoding="utf-8").read()
_tm = ThemeManager(paths.theme_settings_path())
_tm.init(app, _dark, _light)

for mode in ("dark", "light"):
    _tm.set_mode(mode)
    win = _gui.MainWindow(config_path=paths.config_path(), theme_manager=_tm)
    win.resize(1400, 900)
    win.show()
    app.processEvents()

    sep = win._btnbar_sep
    chk = win._chk_stack_mode
    btn = win._btn_start

    check(f"[{mode}] 分隔线已显示且为 1×22",
          sep.isVisible() and (sep.width(), sep.height()) == (1, 22),
          f"visible={sep.isVisible()} {sep.width()}x{sep.height()}")

    gap_l = sep.mapTo(win, QPoint(0, 0)).x() - chk.mapTo(win, QPoint(chk.width(), 0)).x()
    gap_r = btn.mapTo(win, QPoint(0, 0)).x() - sep.mapTo(win, QPoint(sep.width(), 0)).x()
    check(f"[{mode}] 与左右邻居都隔开（左右各 ≥ 8px）", gap_l >= 8 and gap_r >= 8,
          f"left={gap_l} right={gap_r}")

    sep_mid = sep.mapTo(win, QPoint(0, sep.height() // 2))
    btn_mid = btn.mapTo(win, QPoint(0, btn.height() // 2))
    check(f"[{mode}] 竖直方向与「开始打印」居中对齐（±3px）",
          abs(sep_mid.y() - btn_mid.y()) <= 3, f"sep_y={sep_mid.y()} btn_y={btn_mid.y()}")

    # 像素判据：竖线那一列必须和两侧留白（窗口底色）**可辨**。
    # QSS 规则一旦丢失，1px 的 QFrame 会退化成底色 → 此处转红。
    pm = win.grab()
    img = pm.toImage()
    dpr = pm.devicePixelRatio() or 1.0

    def _px(logical_x, logical_y):
        c = img.pixelColor(int(round(logical_x * dpr)), int(round(logical_y * dpr)))
        return (c.red(), c.green(), c.blue())

    line = _px(sep_mid.x(), sep_mid.y())
    bg_l = _px(sep_mid.x() - 6, sep_mid.y())
    bg_r = _px(sep_mid.x() + 6, sep_mid.y())
    d_l = sum(abs(a - b) for a, b in zip(line, bg_l))
    d_r = sum(abs(a - b) for a, b in zip(line, bg_r))
    check(f"[{mode}] 竖线像素与两侧底色可辨（真的看得见）", d_l >= 20 and d_r >= 20,
          f"line={line} bg_left={bg_l} bg_right={bg_r} diff={d_l}/{d_r}")

    win.close()
    app.processEvents()

print("\n" + ("全绿 ✅" if ok_all else "有失败 ❌"))
sys.exit(0 if ok_all else 1)
