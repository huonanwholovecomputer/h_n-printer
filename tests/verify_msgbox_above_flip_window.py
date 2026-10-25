# -*- coding: utf-8 -*-
"""验收 · 翻纸常驻窗开着时弹出的 QMessageBox 不得变成「白板」（2026-10-25 现场 bug）

现场：LaserJet 1020 上「⏭ 跳过本任务」之后，批次失败的「打印完成（有错误）」提示框只画出
深色标题栏 + **纯白内容区**（无文字、无 OK 按钮），操作员只能盲点关闭。

复现（本机真显示器，稳定三次；脚本 `_tmp_bug251025/probe_sequence_screen.py` 的同一时序）：
手动双面批次结束 → `ManualDuplexDialog.finish()` 让置顶翻纸窗 1.5 秒后自动关 → 同时
`_on_all_finished` 弹 QMessageBox。提示框是在**完全被置顶翻纸窗遮住**的位置创建的 ⇒
Win32 首次 paint 不发生；翻纸窗关掉、提示框露出来仍是空白（`repaint()` 也不恢复 ——
Qt backing store 里有内容，`grab()` 能拿到完整文字与按钮，屏幕就是不画）。
对照：提示框自己带 `WindowStaysOnTopHint`、或先收起翻纸窗再弹，都正常。

修法：`gui._MsgBoxAboveFlipWindow`（应用级事件过滤器）—— 翻纸窗可见期间，任何 QMessageBox
在 Show 时给平台窗口补置顶标志；翻纸窗不在场时不动任何东西。

本脚本（offscreen，无合成器）验的是**机制契约**：
  ① 首次创建翻纸窗时装上守卫（且只装一次）；
  ② 翻纸窗可见 → 新弹的 QMessageBox 被补置顶（不会被完全遮挡）；
  ③ 翻纸窗不可见 → 不补（零回归，普通提示框层级不变）；
  ④ 非 QMessageBox 的 Show 事件不受影响（不误伤文件选择框/自定义窗）；
  ⑤ 守卫内部异常不外溢（消息框必须照常弹得出来）。

用法: PYTHONPATH= PYTHONUTF8=1 python.exe tests/verify_msgbox_above_flip_window.py
      exit 0 = 全绿
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_msgbox_topmost_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QDialog  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


app = QApplication.instance() or QApplication([])
import paths  # noqa: E402
import gui    # noqa: E402


def topmost(widget) -> bool:
    wh = widget.windowHandle()
    return bool(wh is not None and (wh.flags() & Qt.WindowStaysOnTopHint))


win = gui.MainWindow(config_path=paths.config_path())
win.show()
app.processEvents()

# ============================================================
print("=== ① 翻纸窗首次创建时装上守卫（且只装一次）===")
check("初始没有翻纸窗/守卫",
      getattr(win, "_duplex_dialog", None) is None
      and getattr(win, "_msgbox_topmost_guard", None) is None, "")
dlg = win._ensure_duplex_dialog()
guard1 = getattr(win, "_msgbox_topmost_guard", None)
_guard_cls = getattr(gui, "_MsgBoxAboveFlipWindow", None)
check("★ 创建翻纸窗后装上了应用级守卫",
      _guard_cls is not None and isinstance(guard1, _guard_cls), f"{guard1!r}")
dlg2 = win._ensure_duplex_dialog()
check("再次获取同一个翻纸窗，守卫不重复安装",
      dlg2 is dlg and getattr(win, "_msgbox_topmost_guard", None) is guard1
      and guard1 is not None, "")

# ============================================================
print("\n=== ② 翻纸窗可见 → 提示框补置顶（现场白板的直接原因）===")
win._show_duplex_dialog(dlg)
app.processEvents()
check("翻纸窗此时可见", dlg.isVisible(), "")
box = QMessageBox(win)
box.setWindowTitle("打印完成（有错误）")
box.setText("全部 1 个任务处理完毕。\n成功: 0\n失败: 1")
box.setIcon(QMessageBox.Warning)
box.show()
app.processEvents()
check("★ 翻纸窗可见期间弹的 QMessageBox 带置顶标志", topmost(box),
      f"flags={hex(int(box.windowHandle().flags())) if box.windowHandle() else None}")
box.close()
app.processEvents()

# ============================================================
print("\n=== ③ 翻纸窗不可见 → 不补置顶（零回归）===")
dlg.hide()
app.processEvents()
box2 = QMessageBox(win)
box2.setWindowTitle("提示")
box2.setText("普通提示")
box2.show()
app.processEvents()
check("★ 没有翻纸窗时不改提示框层级", not topmost(box2), "")
box2.close()
app.processEvents()

# ============================================================
print("\n=== ④ 别的对话框不受影响（只认 QMessageBox）===")
win._show_duplex_dialog(dlg)
app.processEvents()
other = QDialog(win)
other.setWindowTitle("别的窗")
other.show()
app.processEvents()
check("普通 QDialog 不被补置顶（不误伤文件选择框/自定义窗）", not topmost(other), "")
other.close()
app.processEvents()

# ============================================================
print("\n=== ⑤ 守卫内部异常不外溢（消息框必须照常弹得出来）===")
_orig = win._config.tabs


class _Boom:
    def get(self, *a, **k):
        raise RuntimeError("boom")


win._config.tabs = _Boom()      # 让守卫读 _duplex_dialog 之外的任何东西都炸
box3 = QMessageBox(win)
box3.setWindowTitle("异常下仍要能弹")
box3.setText("x")
box3.show()
app.processEvents()
check("★ 守卫内部异常被吞掉、消息框照常显示", box3.isVisible(), "")
box3.close()
win._config.tabs = _orig
app.processEvents()

# ============================================================
print("\n=== ⑥ 静态接线 ===")
src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
check("守卫类存在且说明现场症状",
      "class _MsgBoxAboveFlipWindow(QObject)" in src and "纯白空窗口" in src)
check("★ 它在 _ensure_duplex_dialog 里安装（翻纸窗出现即生效）",
      "_app.installEventFilter(_guard)" in src
      and "self._msgbox_topmost_guard = _guard" in src)
check("改的是平台窗口标志（不重新 show、不抢焦点）",
      "wh.setFlag(Qt.WindowStaysOnTopHint, True)" in src)
check("完成提示框仍是 _on_all_finished 里的 QMessageBox.warning（调用点不变）",
      'self, "打印完成（有错误）"' in src)
_guard_body = (src.split("class _MsgBoxAboveFlipWindow", 1)[1]
               .split("def _ensure_duplex_dialog", 1)[0]
               if "class _MsgBoxAboveFlipWindow" in src else "")
check("只有翻纸窗可见时才动手（零回归判据）", "dlg.isVisible()" in _guard_body)

# 清理：把守卫摘掉，避免影响同进程后续（本脚本到此结束）
try:
    _g = getattr(win, "_msgbox_topmost_guard", None)
    if _g is not None:
        app.removeEventFilter(_g)
except Exception:
    pass

print("\n" + "=" * 56)
print("提示框置顶守卫验收：" + ("全部通过 ✅" if ok_all else "有失败 ❌"))
sys.exit(0 if ok_all else 1)
