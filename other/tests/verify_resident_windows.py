# -*- coding: utf-8 -*-
"""验收脚本 · 常驻提示窗不再绑定主窗口（2026-11-17 用户要求）

现场：操作员点一下「手动双面打印」翻纸窗想看一眼，藏在后面的主界面就被系统一起顶到最前。
根因不是 Qt 的置顶标志，而是 **Windows 的 owner 关系**：翻纸窗/攒批列表原来都是
QDialog(parent=MainWindow) —— 激活被拥有的窗口，系统会连带激活并抬升它的 owner。

判据（A 组静态 + B 组运行时 + C 组变异自证）：

  A. gui.py 里这两个窗的 __init__ 必须是 super().__init__(None) 且调
     _make_resident_tool_window(self)（= Qt.Tool）；创建点不得再传 self；
     退出/激活两条配套链路（_close_resident_windows / _raise_resident_windows）必须接上。
  B. 运行时（offscreen）：即使把主窗口当 parent 传进去，parent() 也是 None、
     windowHandle().transientParent() 也是 None、窗口带 Qt.Tool；
     退出时 _close_resident_windows 只 hide（不许触发「关窗 = 跳过本任务」）；
     _place_resident_window 只摆一次，之后尊重操作员拖到的位置。
  C. 变异自证：把 super().__init__(None) 改回 parent、或删掉 _make_resident_tool_window(self)，
     A 必须当场转红（否则这套判据是空转的）。

    python tests/verify_resident_windows.py     # exit 0 = 全绿
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


# ── A. 静态：两个窗必须是无父的工具窗，配套链路必须接上 ──────────────────────
def _class_init_body(src, cls_name):
    """取某个 QDialog 子类 __init__ 的源码行（到同缩进的下一处 def 或 class 为止）。"""
    m = re.search(rf"^class {cls_name}\(QDialog\):", src, re.M)
    if not m:
        return ""
    tail = src[m.end():]
    m2 = re.search(r"\n    def __init__\(self, parent=None\):", tail)
    if not m2:
        return ""
    body = tail[m2.end():]
    end = re.search(r"\n    (?:def |class )", body)
    return body[:end.start()] if end else body


def static_fails(src):
    """返回静态判据的失败清单；传变异后的源码即可复用做自证。"""
    fails = []
    for cls in ("ManualDuplexDialog", "StackQueueDialog"):
        body = _class_init_body(src, cls)
        if not body:
            fails.append(f"{cls}: 找不到 __init__(self, parent=None)")
            continue
        if "super().__init__(None)" not in body:
            if "super().__init__(parent)" in body:
                fails.append(f"{cls}: 还是 super().__init__(parent)"
                             f"（Windows owner 会把主窗口一起抬起来）")
            else:
                fails.append(f"{cls}: __init__ 里没看到 super().__init__(None)")
        if "_make_resident_tool_window(self)" not in body:
            fails.append(f"{cls}: 没调 _make_resident_tool_window(self)（Qt.Tool / 不进任务栏）")

    # 创建点不得再把主窗口当 parent 传进去
    for bad in ("ManualDuplexDialog(self)", "StackQueueDialog(self)"):
        if bad in src:
            fails.append(f"创建点还在传 parent：{bad}")

    # 工具窗标志本身
    m = re.search(r"def _make_resident_tool_window\(.*?\n(?=\n\ndef |\n\nclass )", src, re.S)
    if not m:
        fails.append("找不到 _make_resident_tool_window")
    elif "setWindowFlag(Qt.Tool, True)" not in m.group(0):
        fails.append("_make_resident_tool_window 里没有 setWindowFlag(Qt.Tool, True)")

    # 两条配套链路：主窗口激活 → raise；真退出 → 收窗
    m = re.search(r"def changeEvent\(self, event\):.*?(?=\n    def )", src, re.S)
    if not m or "_raise_resident_windows()" not in m.group(0):
        fails.append("changeEvent 没接 _raise_resident_windows()"
                     "（工具窗没有任务栏按钮，主界面一激活就再也点不回来）")
    m = re.search(r"def closeEvent\(self, event\):.*?_stop_external_watch\(\)", SRC_OF(src), re.S)
    if not m or "_close_resident_windows()" not in m.group(0):
        fails.append("MainWindow.closeEvent 没接 _close_resident_windows()"
                     "（脱离 owner 后不会随主窗口销毁）")
    return fails


def SRC_OF(src):
    """取 MainWindow 段（避免撞上其它类里同名的 closeEvent）。"""
    m = re.search(r"^class MainWindow\(QMainWindow\):", src, re.M)
    return src[m.start():] if m else src


def _close_only_hides(src):
    """_close_resident_windows 必须只 hide + deleteLater（不走 close）。"""
    m = re.search(r"def _close_resident_windows\(self\):.*?\n    def ", src, re.S)
    if not m:
        return ["找不到 _close_resident_windows"]
    body = re.sub(r'"""[\s\S]*?"""', "", m.group(0))   # 文档字符串里讲"为什么不 close()"不算
    fails = []
    if "close()" in body:
        fails.append("_close_resident_windows 里调了 close()"
                     "（ManualDuplexDialog.closeEvent 会把它解释成「跳过本任务」）")
    if "hide()" not in body:
        fails.append("_close_resident_windows 里没 hide()")
    return fails


section("A. 静态：两个常驻窗必须是无父工具窗")
fails = static_fails(SRC) + _close_only_hides(SRC)
for msg in fails:
    check(msg, False)
check("A 全部静态判据（无父构造 / Qt.Tool / 创建点 / 激活 raise / 退出收窗）",
      not fails, "；".join(fails) if fails else "")

# ── B. 运行时（offscreen）─────────────────────────────────────────────────
section("B. 运行时：真构造一遍，确认没有 owner 关系")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

_app = QApplication.instance() or QApplication([])
import gui  # noqa: E402

main = QMainWindow()
main.setGeometry(120, 80, 900, 640)
main.show()

dup = gui.ManualDuplexDialog(main)      # 故意把主窗口当 parent 传进去（应被丢弃）
dup.show()
check("ManualDuplexDialog: parent() is None（不再是主窗口的子对象）", dup.parent() is None,
      f"parent={dup.parent()!r}")
check("ManualDuplexDialog: 带 Qt.Tool（不进任务栏 / 不进 Alt+Tab）",
      bool(dup.windowFlags() & Qt.Tool))
try:
    tp = dup.windowHandle().transientParent() if dup.windowHandle() else None
except Exception as e:                                  # pragma: no cover
    tp = f"<取不到: {e}>"
check("ManualDuplexDialog: windowHandle().transientParent() is None", tp is None, f"{tp!r}")
check("ManualDuplexDialog: 主窗口 findChildren 里找不到它（QObject 树上也脱离了）",
      dup not in main.findChildren(type(dup)))

stack = gui.StackQueueDialog(main)
stack.show()
check("StackQueueDialog: parent() is None", stack.parent() is None, f"parent={stack.parent()!r}")
check("StackQueueDialog: 带 Qt.Tool", bool(stack.windowFlags() & Qt.Tool))

# 关窗 = 跳过本任务（翻纸窗）：退出收窗绝不能走 close()
_skips = []
dup.skip_requested.connect(lambda: _skips.append(1))
dup._mode = "flip"                      # 最容易误触发「跳过」的状态
check("前置：翻纸窗在 flip 态时可被 hide", dup.isVisible())


class _Stub:
    pass


stub = _Stub()
stub._duplex_dialog = dup
stub._stack_queue_dialog = stack
gui.MainWindow._close_resident_windows(stub)
check("退出收窗：两个常驻窗都被隐藏", not dup.isVisible() and not stack.isVisible())
check("退出收窗：没有触发「跳过本任务」（只 hide，不 close）", not _skips,
      f"skip 发了 {len(_skips)} 次")

# 摆位：第一次居中，之后尊重操作员拖到的位置
section("B2. 首次摆位只摆一次")
from PySide6.QtCore import QPoint  # noqa: E402

dup2 = gui.ManualDuplexDialog()
dup2.show()
gui.MainWindow._place_resident_window(main, dup2)
_c1 = dup2.frameGeometry().center()
_m1 = main.frameGeometry().center()
check("首次显示摆到主窗口正中",
      abs(_c1.x() - _m1.x()) <= 40 and abs(_c1.y() - _m1.y()) <= 40,
      f"dlg={_c1.x()},{_c1.y()} main={_m1.x()},{_m1.y()}")
dup2.move(QPoint(7, 9))
gui.MainWindow._place_resident_window(main, dup2)
check("第二次（操作员拖动过）不再强制挪位", (dup2.x(), dup2.y()) == (7, 9),
      f"现在是 {dup2.x()},{dup2.y()}")

# 主窗口激活 → 只 raise，不抢焦点
section("B3. 主窗口激活时把常驻窗抬起来")
_raised = []
dup3 = gui.ManualDuplexDialog()
dup3.show()
dup3.raise_ = lambda: _raised.append("dup")          # 打桩，只看有没有被调到
stub2 = _Stub()
stub2._duplex_dialog = dup3
stub2._stack_queue_dialog = None
gui.MainWindow._raise_resident_windows(stub2)
check("_raise_resident_windows 对可见常驻窗调了 raise_", _raised == ["dup"], f"{_raised}")
dup3.hide()
gui.MainWindow._raise_resident_windows(stub2)
check("不可见时不调 raise_（别把隐藏窗顶出来）", _raised == ["dup"], f"{_raised}")

# ── C. 变异自证 ───────────────────────────────────────────────────────────
section("C. 变异自证：判据必须能红")
mut1 = SRC.replace('super().__init__(None)\n        self.setWindowTitle("手动双面打印")',
                   'super().__init__(parent)\n        self.setWindowTitle("手动双面打印")', 1)
check("变异体 1 真的改到了源码（锚点存在）", mut1 != SRC)
f1 = static_fails(mut1)
check("变异 1（改回 super().__init__(parent)）→ 静态判据转红", bool(f1),
      "；".join(f1[:3]))

mut2 = SRC.replace("        _make_resident_tool_window(self)\n        self.setMinimumSize(1180, 520)",
                   "        self.setMinimumSize(1180, 520)", 1)
check("变异体 2 真的改到了源码（锚点存在）", mut2 != SRC)
f2 = static_fails(mut2)
check("变异 2（删掉攒批列表的 _make_resident_tool_window）→ 静态判据转红", bool(f2),
      "；".join(f2[:3]))

mut3 = SRC.replace('                dlg.hide()\n                dlg.deleteLater()',
                   '                dlg.close()\n                dlg.deleteLater()', 1)
check("变异体 3 真的改到了源码（锚点存在）", mut3 != SRC)
f3 = _close_only_hides(mut3)
check("变异 3（退出改走 close()）→ 转红", bool(f3), "；".join(f3))

section("结果")
print("常驻窗脱离主窗口验收: " + ("全部通过 ✅" if ok_all else "有失败 ❌"))
sys.exit(0 if ok_all else 1)
