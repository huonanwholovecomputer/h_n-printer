# -*- coding: utf-8 -*-
"""验收脚本 · 复选框勾选态必须是「对号」（而不是一块实心方块）

背景（2026-10 用户反馈）：QSS 只画 `QCheckBox::indicator` 的**底色**（勾选 = 蓝方块），
对号由 `gui.ThemedCheckBox.paintEvent` 额外绘制 "✓"。所以：
  · 用 `ThemedCheckBox` → 蓝底 + 白对号（与「管理员自行打印」一致）；
  · 用原生 `QCheckBox`   → 只剩一块实心蓝方块（攒批模式曾经如此）。

本脚本两层把关：
  1. 静态：gui.py 里**不允许**直接构造 QCheckBox（只允许 class ThemedCheckBox(QCheckBox)）；
  2. 运行时：真实 MainWindow 里两个主界面复选框
     （管理员自行打印 / 🖐 攒批模式）勾选后，指示器内部**必须出现白色对号像素**，
     且未勾选时是纯底色 —— 两种主题各测一遍。

    python tests/verify_checkbox_tick_style.py     # exit 0 = 全绿
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

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="hn_chk_")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtGui import QFontDatabase  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QStyle, QStyleOptionButton,
)

app = QApplication.instance() or QApplication([])
# 离屏渲染需要真字体（本机 PySide6 不带字体目录）：挂上系统字体，
# 否则 "✓" 可能画成空框/不画，本脚本的像素判据会假红。
for _f in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/segoeui.ttf"):
    if os.path.isfile(_f):
        QFontDatabase.addApplicationFont(_f)

import gui as _gui  # noqa: E402

_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

from gui import MainWindow, ThemedCheckBox  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


print("=== 1. 静态：不允许直接构造原生 QCheckBox ===")
_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
# 去掉 import 行与 class 声明行后，源码里不应再出现 "QCheckBox("
_hits = [ln.strip() for ln in _src.splitlines()
         if re.search(r"(?<!Themed)\bQCheckBox\(", ln)
         and not ln.strip().startswith(("class ThemedCheckBox", "#"))
         and "import" not in ln]
check("gui.py 里只有 class ThemedCheckBox(QCheckBox)，没有别的 QCheckBox 构造",
      _hits == [], f"{_hits}")
check("ThemedCheckBox 仍然自己绘制对号（paintEvent 里画 ✓）",
      'painter.drawText(rect, Qt.AlignCenter, "✓")' in _src)
check("攒批模式开关用的是 ThemedCheckBox",
      'self._chk_stack_mode = ThemedCheckBox("🖐 攒批模式"' in _src)
check("管理员自行打印开关也用的是 ThemedCheckBox",
      'self._admin_print_check = ThemedCheckBox("管理员自行打印"' in _src)

print("\n… 正在创建主窗口", flush=True)
win = MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
app.processEvents()

# 攒批模式的复选框现在**只对「不支持自动双面、需人工翻纸」的机型显示**（2026-10 用户要求）：
# 临时配置里没有任何打印机档案 ⇒ 本脚本所在机器上它是隐藏的，而隐藏控件的 indicator 矩形会
# 算到离屏坐标、像素探针扫到图像外（判据假红/假绿）。这里把档案**桩**成那样一台机器，让它在
# 真实条件下可见 —— 而且必须桩 `_printer_profile`（不是只写 config.printer_profiles）：
# 探针里 `setChecked(True)` 会真的触发切换 → `_apply_stack_mode` → `_save_config()` →
# `_sync_ui_to_config()` 从界面下拉框读回真实打印机名，配置里的伪档案随即失效、控件又被收起。
from printer_config import PrinterProfile  # noqa: E402

_FAKE_MANUAL = PrinterProfile(auto_duplex=False, capability_source="manual",
                              manual_duplex=True)
win._printer_profile = lambda: _FAKE_MANUAL
win._sync_stack_button()
app.processEvents()
check("前置：需人工翻纸的机型 → 攒批复选框可见（下面那套对号判据才成立）",
      not win._chk_stack_mode.isHidden(), f"hidden={win._chk_stack_mode.isHidden()}")

print("\n=== 2. 运行时：勾选后指示器里必须有白色对号像素 ===")


def indicator_rect(w):
    """复选框指示器矩形（QSS 16px + 1px 边框 → 18×18，含边框）。"""
    opt = QStyleOptionButton()
    w.initStyleOption(opt)
    return w.style().subElementRect(QStyle.SE_CheckBoxIndicator, opt, w)


def probe(w, margin=3):
    """返回指示器内部的像素统计：(近白核像素数, 颜色种类数, 总面积, 矩形)。

    近白核 = min(r,g,b) > 232：浅色主题的方框底色 #e6e9ef 的三通道最小值是 230，
    因此**不会**被误判成"对号"；纯白对号（#ffffff）与其中间笔画的抗锯齿核会计入。
    """
    r = indicator_rect(w)
    img = w.grab().toImage()
    white = 0
    colors = set()
    total = 0
    for y in range(r.top() + margin, r.bottom() + 1 - margin):
        for x in range(r.left() + margin, r.right() + 1 - margin):
            c = img.pixelColor(x, y)
            colors.add((c.red() // 32, c.green() // 32, c.blue() // 32))
            if min(c.red(), c.green(), c.blue()) > 232:
                white += 1
            total += 1
    return white, len(colors), total, r


def state_probe(w, enabled: bool, checked: bool, margin=3):
    was = w.isEnabled()
    w.setEnabled(enabled)
    w.setChecked(checked)
    app.processEvents()
    out = probe(w, margin)
    w.setEnabled(was)
    return out


for theme in ("light", "dark"):
    with open(os.path.join(LOCAL_TOOL, f"styles_{theme}.qss"), encoding="utf-8") as fh:
        app.setStyleSheet(fh.read())
    win.style().unpolish(win)
    win.style().polish(win)
    app.processEvents()

    sizes = {}
    for label, w in (("管理员自行打印", win._admin_print_check),
                     ("🖐 攒批模式", win._chk_stack_mode)):
        check(f"{theme}/{label}: 是 ThemedCheckBox（自己画对号）",
              isinstance(w, ThemedCheckBox) and type(w) is not QCheckBox,
              type(w).__name__)
        check(f"{theme}/{label}: 指示器可用（非空矩形）",
              not indicator_rect(w).isEmpty(), f"{indicator_rect(w)}")

        # ① 未勾选（可用）= 纯底色，无对号
        off_white, off_colors, _, r = state_probe(w, True, False)
        check(f"{theme}/{label}: 未勾选 = 纯底色（无对号像素）",
              off_white == 0 and off_colors == 1,
              f"white={off_white} colors={off_colors} area={r}")

        # ② 勾选（可用）= 底色 + **白色对号**（不是一块实心方块）
        on_white, on_colors, _, r = state_probe(w, True, True)
        check(f"{theme}/{label}: 勾选后有白色对号像素（不是一块实心方块）",
              on_white >= 3 and on_colors >= 2,
              f"white={on_white} colors={on_colors} area={r}")

        # ③ 勾选（禁用）= 灰对号仍要画出来（禁用不等于"没有对号"）
        dis_white, dis_colors, _, r = state_probe(w, False, True)
        check(f"{theme}/{label}: 禁用+勾选仍是「对号」（只是灰的）",
              dis_colors >= 2, f"white={dis_white} colors={dis_colors} area={r}")

        # ④ 未勾选（禁用）= 纯底色
        dis_off_white, dis_off_colors, _, r = state_probe(w, False, False)
        check(f"{theme}/{label}: 禁用+未勾选 = 纯底色",
              dis_off_colors == 1, f"colors={dis_off_colors} area={r}")

        sizes[label] = indicator_rect(w).size().toTuple()

    check(f"{theme}: 两处复选框口径一致（同样大小、同样 QSS）",
          len(set(sizes.values())) == 1 and min(sizes.values()) >= (16, 16),
          f"{sizes}")

    win._chk_stack_mode.setChecked(False)
    win._admin_print_check.setChecked(False)
    app.processEvents()

win.close()
app.processEvents()

print("\n" + "=" * 56)
print("复选框对号样式验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
