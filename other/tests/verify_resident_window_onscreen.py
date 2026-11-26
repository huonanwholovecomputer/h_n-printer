# -*- coding: utf-8 -*-
"""验收脚本 · 常驻窗必须落在屏幕可用区域内（2026-11-24 现场）

现场：操作员截图报「攒批窗口怎么没有窗口边框了，我无法移动它」。
实测（Win32 枚举进程窗口）：窗口**有** WS_CAPTION，但 rect=(23,-31,775x317) ——
30px 标题栏整条在屏幕上方，屏幕上只剩客户区从 y=0 开始显示，所以看着像无边框；
而原生窗口只有标题栏能拖，于是彻底动不了。

根因：MainWindow._place_resident_window 按主窗口中心摆位时**不夹屏幕**，主窗口靠上
（或比屏幕还高）时 y 会算成负数。修法：摆完调 _fit_resident_window_on_screen，
并在两个常驻窗每次显示时再兜一次（被拖到够不着的地方 ⇒ 下次显示拉回来）。

判据（A 静态 + B 运行时 + C 变异自证）：

  A. gui.py 里必须有 _fit_resident_window_on_screen；_place_resident_window、
     _show_duplex_dialog、_show_stack_queue_dialog 三处都必须接上。
  B. 运行时（offscreen，屏幕 800x800）：
     ① y=-31（复现现场）⇒ 拉回屏幕内，标题栏够得着；
     ② 整窗在屏幕右侧外 ⇒ 拉回；
     ③ 够得着的位置（含贴边）⇒ 一个像素都不动（不许抢操作员摆的位置）；
     ④ 主窗口被拖到屏幕上方时首次摆位 ⇒ 摆完标题栏仍在屏幕内；
     ⑤ 主窗口收在托盘（不可见）⇒ 仍然不摆位（原语义，别把窗口抖到别的屏幕）。
  C. 变异自证：把 _fit_resident_window_on_screen 打桩成空操作后，④ 必须转红
     （否则这套判据是空转的）。

    python other/tests/verify_resident_window_onscreen.py     # exit 0 = 全绿
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

GUI = os.path.join(LOCAL_TOOL, "gui.py")
SRC = io.open(GUI, encoding="utf-8").read()

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return cond


def section(title):
    print("\n" + "=" * 78)
    print(title)


def _func_body(src, name, cls=None):
    """取一个方法/函数的源码体（cls=None 取模块级函数）。"""
    if cls:
        m = re.search(rf"^class {cls}\b", src, re.M)
        if not m:
            return ""
        src = src[m.start():]
    m = re.search(rf"\n(?:    )?def {name}\(.*?\n(?=\n(?:    )?def |\n\nclass |\Z)", src, re.S)
    return m.group(0) if m else ""


# ── A. 静态：助手函数在，三处调用点都接上 ────────────────────────────────
section("A. 静态：摆位必须夹屏幕")

check("A1 gui.py 里有 _fit_resident_window_on_screen（模块级）",
      bool(re.search(r"^def _fit_resident_window_on_screen\(", SRC, re.M)))
place = _func_body(SRC, "_place_resident_window", "MainWindow")
check("A2 _place_resident_window 摆完调了 _fit_resident_window_on_screen",
      "_fit_resident_window_on_screen(dlg)" in place,
      "否则主窗口靠上时标题栏会被摆到屏幕外" if "_fit_resident_window_on_screen(dlg)" not in place else "")
show_dup = _func_body(SRC, "_show_duplex_dialog", "MainWindow")
check("A3 _show_duplex_dialog 每次显示都兜一次", "_fit_resident_window_on_screen(dlg)" in show_dup)
show_stack = _func_body(SRC, "_show_stack_queue_dialog", "MainWindow")
check("A4 _show_stack_queue_dialog 每次显示都兜一次",
      "_fit_resident_window_on_screen(dlg)" in show_stack)
check("A5 摆位仍是「只摆一次」（_hn_placed 闸门还在）", "_hn_placed" in place)

# ── B. 运行时（offscreen）─────────────────────────────────────────────────
section("B. 运行时：真窗口摆位")

from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

_app = QApplication.instance() or QApplication([])
import gui  # noqa: E402

AV = _app.primaryScreen().availableGeometry()
print(f"  屏幕可用区域 = {AV.getRect()}")

# 助手函数不在 ⇒ 运行时判据无从谈起：干净地红在断言上（不要落成 AttributeError）
if not hasattr(gui, "_fit_resident_window_on_screen"):
    check("B0 gui 里能取到 _fit_resident_window_on_screen", False,
          "改前版本即如此：摆位不夹屏幕 ⇒ 标题栏被摆到屏幕外")
    print("\n常驻窗屏幕内验收: 有失败 ❌")
    sys.exit(1)

# ① 复现现场：y=-31 ⇒ 标题栏够不着 ⇒ 拉回来
stack = gui.StackQueueDialog()
stack.show()
_app.processEvents()
stack.move(23, -31)
_moved = gui._fit_resident_window_on_screen(stack)
fg = stack.frameGeometry()
check("B1 y=-31（现场坐标）被拉回屏幕内", _moved and fg.top() >= AV.top(),
      f"现在 ({fg.left()},{fg.top()})")
check("B2 拉回后标题栏够得着（顶部 24px 在屏幕内）",
      AV.top() <= fg.top() <= AV.bottom() - 24, f"top={fg.top()} avail={AV.getRect()}")

# ② 整窗在屏幕右侧外 ⇒ 拉回
stack.move(AV.right() + 300, 100)
_moved = gui._fit_resident_window_on_screen(stack)
fg = stack.frameGeometry()
check("B3 整窗在屏幕右侧外被拉回", _moved and fg.left() <= AV.right() - 24,
      f"left={fg.left()} availRight={AV.right()}")

# ③ 够得着就不动（贴边也算够得着：操作员故意停边上）
stack.move(100, 120)
_before = (stack.x(), stack.y())
_moved = gui._fit_resident_window_on_screen(stack)
check("B4 位置正常时一个像素都不挪", (not _moved) and (stack.x(), stack.y()) == _before,
      f"{_before} → ({stack.x()},{stack.y()})")

# ④ 主窗口被拖到屏幕上方 ⇒ 首次摆位后标题栏仍在屏幕内
main = QMainWindow()
main.setGeometry(120, -320, 900, 640)          # 中心 y≈0，比屏幕还高
main.show()
_app.processEvents()
stack2 = gui.StackQueueDialog()
stack2.show()
_app.processEvents()
gui.MainWindow._place_resident_window(main, stack2)
fg2 = stack2.frameGeometry()
check("B5 主窗口拖到屏幕上方时，摆位后标题栏仍在屏幕内",
      AV.top() <= fg2.top() <= AV.bottom() - 24,
      f"dlg top={fg2.top()}（未修时会是负数：现场 -31）")
check("B6 摆位后仍标记为已摆（不会每次显示都重摆）", getattr(stack2, "_hn_placed", False))

# ⑤ 主窗口收在托盘（不可见）⇒ 不摆位
main.hide()
_app.processEvents()
stack3 = gui.StackQueueDialog()
stack3.show()
_app.processEvents()
_before3 = (stack3.x(), stack3.y())
gui.MainWindow._place_resident_window(main, stack3)
check("B7 主窗口不可见时不摆位（没有可参照的中心）",
      (stack3.x(), stack3.y()) == _before3 and not getattr(stack3, "_hn_placed", False),
      f"{_before3} → ({stack3.x()},{stack3.y()})")

# ── C. 变异自证：把兜底函数打桩成空操作，B5 必须转红 ──────────────────────
section("C. 变异自证：判据必须能红")

_real_fit = gui._fit_resident_window_on_screen
gui._fit_resident_window_on_screen = lambda *a, **k: False
main.show()
_app.processEvents()
main.setGeometry(120, -320, 900, 640)
_app.processEvents()
stack4 = gui.StackQueueDialog()
stack4.show()
_app.processEvents()
gui.MainWindow._place_resident_window(main, stack4)
fg4 = stack4.frameGeometry()
gui._fit_resident_window_on_screen = _real_fit
check("C1 变异（兜底函数变空操作）→ 窗口真的被摆到屏幕外（判据能红）",
      fg4.top() < AV.top(), f"dlg top={fg4.top()}（负值即现场形态）")

section("结果")
print("常驻窗屏幕内验收: " + ("全部通过 ✅" if ok_all else "有失败 ❌"))
sys.exit(0 if ok_all else 1)
