# -*- coding: utf-8 -*-
"""验收脚本 · 文件列表「选中行 = 蓝色」（深/浅两套主题一致，浅色不许再用灰）

需求（2026-10 现场，两轮）：
  ① 深色模式下选中文件是**蓝色**，浅色模式却是灰底灰字 —— 与未选中几乎分不出来。
     而「移除选中」「➕ 添加到新标签页」这类批量操作正是对着**整片选区**动手的，
     看不清选中了哪几行就会点错。要求浅色也改成蓝色。
  ② 第一版用了主题强调蓝 #1e66f5 + 白字，现场反馈**太艳、字发飘看不清** ⇒
     改成**淡蓝底 + 深色字**（浅色 #cfe3ff / #4c4f69）；深色仍是深蓝 #264f78 + 白字。

本脚本两层判据：
  ① 静态：两套 QSS 的 `QTableWidget` 规则里选中底色必须是**蓝色系**（蓝通道占主导），
     文字色与底色对比度 ≥ 4.5:1（WCAG AA）；浅色还额外要求底色"淡"（亮度 ≥ 0.5）
     且文字**比底色深**（不许再拿白字压艳蓝），深色保持"深底浅字"；
  ② 运行时（offscreen 真渲染）：真实 MainWindow 里选中几行 → `win.grab()` 取选中行
     单元格内的像素，必须真的是蓝的，且与未选中行**不同**、未选中行不是蓝的
     （QSS 规则丢了/被换回灰色会立刻转红）。

    python tests/verify_table_selection_style.py     # exit 0 = 全绿
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

_SANDBOX = tempfile.mkdtemp(prefix="hn_selstyle_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtCore import Qt, QPoint, QItemSelectionModel  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

import gui as _gui  # noqa: E402
_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

from gui import MainWindow  # noqa: E402
from printer_config import PrintJob, TabSettings  # noqa: E402
from theme_manager import ThemeManager  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ─────────── 颜色工具 ───────────

def hex_rgb(s: str) -> tuple:
    s = s.strip().lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def _lin(c: int) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb: tuple) -> float:
    r, g, b = (_lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple, b: tuple) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def is_blue(rgb: tuple) -> bool:
    """蓝通道明显占主导 = 蓝色系（灰/黑/白都不满足）。"""
    r, g, b = rgb
    return b > r + 40 and b > g + 20


def qss_table_block(theme: str) -> str:
    qss = open(os.path.join(LOCAL_TOOL, f"styles_{theme}.qss"), encoding="utf-8").read()
    m = re.search(r"QTableWidget\s*\{(.*?)\}", qss, re.S)
    return m.group(1) if m else ""


def qss_value(body: str, prop: str) -> str:
    m = re.search(rf"{prop}\s*:\s*([^;]+);", body)
    return m.group(1).strip() if m else ""


# ═══════════════ ① 静态：两套主题的选中色 ═══════════════

print("=== ① 静态：QTableWidget 选中底色/文字色 ===")
OLD_GREY = (0xcc, 0xd0, 0xda)          # 浅色主题最初那版灰选中底
TOO_VIVID = (0x1e, 0x66, 0xf5)         # 浅色主题上一版"太艳"的强调蓝
sel = {}
for theme in ("dark", "light"):
    body = qss_table_block(theme)
    bg_hex = qss_value(body, "selection-background-color")
    fg_hex = qss_value(body, "selection-color")
    check(f"[{theme}] QTableWidget 规则里有 selection-background-color / selection-color",
          bool(bg_hex) and bool(fg_hex), f"bg={bg_hex!r} fg={fg_hex!r}")
    if not (bg_hex and fg_hex):
        continue
    bg, fg = hex_rgb(bg_hex), hex_rgb(fg_hex)
    sel[theme] = (bg, fg)
    check(f"[{theme}] 选中底色是蓝色系（{bg_hex} = {bg}）", is_blue(bg), f"rgb={bg}")
    c = contrast(bg, fg)
    check(f"[{theme}] 选中文字与底色对比度 ≥ 4.5:1（{fg_hex} on {bg_hex}）", c >= 4.5,
          f"contrast={c:.2f}:1")
    if theme == "light":
        # 「淡」= 底色够亮（亮度 ≥ 0.5）且文字比底色**深** —— 白字压艳蓝那版会两条全挂
        lb, lf = luminance(bg), luminance(fg)
        check(f"[light] 选中底色足够淡（亮度 {lb:.2f} ≥ 0.50）", lb >= 0.5, f"L={lb:.3f}")
        check(f"[light] 选中文字比底色深（深字压淡底，L字 {lf:.2f} < L底 {lb:.2f}）", lf < lb,
              f"L_fg={lf:.3f} L_bg={lb:.3f}")
    else:
        check(f"[{theme}] 深色保持「深底浅字」（L字 {luminance(fg):.2f} > L底 {luminance(bg):.2f}）",
              luminance(fg) > luminance(bg),
              f"L_fg={luminance(fg):.3f} L_bg={luminance(bg):.3f}")

if "light" in sel:
    check("浅色选中底色**不再是**旧的灰 #ccd0da", sel["light"][0] != OLD_GREY,
          f"rgb={sel['light'][0]}")
    check("浅色选中底色**不再是**太艳的 #1e66f5", sel["light"][0] != TOO_VIVID,
          f"rgb={sel['light'][0]}")
if {"dark", "light"} <= set(sel):
    check("深/浅两套主题的选中色是同一色系（都蓝）",
          is_blue(sel["dark"][0]) and is_blue(sel["light"][0]),
          f"dark={sel['dark'][0]} light={sel['light'][0]}")

# ═══════════════ ② 运行时：真渲染取像素 ═══════════════

print("=== ② 运行时：选中行像素必须是蓝的（offscreen 真渲染）===")

_pdf = os.path.join(_SANDBOX, "A.pdf")
try:
    import fitz
    _d = fitz.open()
    _d.new_page()
    _d.save(_pdf)
    _d.close()
except Exception:
    with open(_pdf, "wb") as f:
        f.write(b"%PDF-1.4\n")

_dark_qss = open(os.path.join(LOCAL_TOOL, "styles_dark.qss"), encoding="utf-8").read()
_light_qss = open(os.path.join(LOCAL_TOOL, "styles_light.qss"), encoding="utf-8").read()
_tm = ThemeManager(paths.theme_settings_path())
_tm.init(app, _dark_qss, _light_qss)


def sample_selected_and_free(win, mode):
    """返回（选中行单元格像素, 未选中行单元格像素）——取行内空白处，避开文字。"""
    tab = TabSettings()
    tab.jobs = [PrintJob(file_path=_pdf, page_count=i + 1, display_name=f"F{i}.pdf")
                for i in range(4)]
    win._config.tabs = {"1": tab}
    win._current_tab = "1"
    win._rebuild_table()
    app.processEvents()

    t = win._table
    t.clearSelection()
    sm = t.selectionModel()
    for r in (0, 1):
        sm.select(t.model().index(r, 0),
                  QItemSelectionModel.Select | QItemSelectionModel.Rows)
    app.processEvents()

    pm = win.grab()
    img = pm.toImage()
    dpr = pm.devicePixelRatio() or 1.0

    def px(row, col):
        rect = t.visualRect(t.model().index(row, col))
        p = t.viewport().mapTo(win, QPoint(rect.left() + 3, rect.center().y()))
        c = img.pixelColor(int(round(p.x() * dpr)), int(round(p.y() * dpr)))
        return (c.red(), c.green(), c.blue())

    return px(0, MainWindow.COL_ORIENT), px(3, MainWindow.COL_ORIENT)


for mode in ("dark", "light"):
    _tm.set_mode(mode)
    win = MainWindow(config_path=paths.config_path(), theme_manager=_tm)
    win.resize(1400, 900)
    win.show()
    app.processEvents()
    s_px, f_px = sample_selected_and_free(win, mode)
    check(f"[{mode}] 选中行像素是蓝色系", is_blue(s_px), f"选中={s_px}")
    # 淡蓝底与浅灰底只差 50 来个通道值（色相不同才是主要判据），故阈值取 40
    dist = sum(abs(a - b) for a, b in zip(s_px, f_px))
    check(f"[{mode}] 选中行与未选中行可辨（通道差合计 ≥ 40）", dist >= 40,
          f"选中={s_px} 未选中={f_px} 差={dist}")
    check(f"[{mode}] 未选中行不是蓝的（判据有区分度）", not is_blue(f_px), f"未选中={f_px}")

print()
print("=" * 60)
print("全部通过 ✅" if ok_all else "存在 FAIL ❌")
sys.exit(0 if ok_all else 1)
