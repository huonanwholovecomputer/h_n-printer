# -*- coding: utf-8 -*-
"""托盘退出 × 模态子窗：2026-11-11 现场（用户报「标签页管理器开着时从托盘退出，
程序直接消失、标签页与订单原样留在配置里」）。

根因：Qt 默认 quitOnLastWindowClosed=True，主窗常驻托盘时是不可见的，
最后可见的那个 exec() 模态窗（标签页管理器 / 未完成确认框）一关，
Qt 直接结束事件循环 —— closeEvent 里「未完成确认 / 放弃上报 / 保存配置」一步都没走。

本脚本钉住：
    ① 常驻托盘成功后关掉 quitOnLastWindowClosed（托盘不可用时保持默认）
    ② _quit_app 先收模态子窗，再 close()；close() 没把收尾走完（_close_done 仍假）就再来一次
    ③ 操作员在确认框里点「不退了」⇒ 进程绝不退出（_force_quit 被复位、不调 QApplication.quit）
    ④ _close_modal_dialogs_for_quit 真的把 _tab_manager_dialog reject 掉
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
_SANDBOX = tempfile.mkdtemp(prefix="hn_quit_modal_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui  # noqa: E402
import paths  # noqa: E402

_fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"[PASS] {name}")
    else:
        _fails.append(name)
        print(f"[FAIL] {name}" + (f"  ←  {detail}" if detail else ""))


class FakeTray:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


def new_win():
    win = gui.MainWindow(config_path=paths.config_path())
    win._log = lambda *a, **k: None
    win._tray = FakeTray()
    return win


SRC = io.open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()


def _method(name, needle=""):
    """取某方法的方法体。gui.py 里有多个同名 closeEvent（别的类），必须按 needle 挑对那一个。"""
    idx = -1
    while True:
        idx = SRC.find(name, idx + 1)
        if idx < 0:
            return ""
        body = SRC[idx:]
        if not needle or needle in body[:40000]:
            return body.split("\n    def ", 1)[0]


_setup = _method("def _setup_tray(self)")
_quit = _method("def _quit_app(self)")
_close_modals = _method("def _close_modal_dialogs_for_quit(self)")
_ce = _method("def closeEvent(self, event)", "_ask_unfinished_tabs")
assert _setup and _quit and _close_modals and _ce, "源码切片失败"

print("== ① 常驻托盘期间关掉 quitOnLastWindowClosed ==")
check("①-a 托盘启动成功后才关（在 if ctrl.start() 分支里）",
      "QApplication.setQuitOnLastWindowClosed(False)" in _setup
      and _setup.index("if ctrl.start():") < _setup.index("QApplication.setQuitOnLastWindowClosed(False)"),
      "没有关掉 / 位置不在成功分支")
check("①-b 注释写明现场原因（对话框一关就结束进程、closeEvent 走不到）",
      "quitOnLastWindowClosed" in _setup and "对话框" in _setup and "closeEvent" in _setup)
check("①-c 托盘不可用的分支不执行这条（保持默认：关窗即退出）",
      _setup.count("setQuitOnLastWindowClosed") == 1)

print("== ② _quit_app：先收模态窗，再判断收尾是否真的走完 ==")
check("②-a 先收模态子窗、再 close()",
      "self._close_modal_dialogs_for_quit()" in _quit
      and _quit.index("self._close_modal_dialogs_for_quit()") < _quit.index("self.close()"),
      "顺序不对")
check("②-b close() 前后用 _close_done 门控（没走完就再来一次）",
      "self._close_done = False" in _quit and "not self._close_done" in _quit,
      "缺 _close_done 门控")
check("②-c closeEvent 走到最后才置 _close_done",
      "_close_done = True" in _ce and _ce.index("_close_done = True") > _ce.index("_ask_unfinished_tabs"),
      "标记位置不对")

print("== ③ 行为：模态窗挡住时重试一次、最后一次真的退 ==")
w = new_win()
_calls = []
w._close_modal_dialogs_for_quit = lambda: _calls.append("modals")


def _close_blocked():
    _calls.append("close")          # 模拟 closeEvent 被模态循环挡住，收尾没跑完


w.close = _close_blocked
_quits = []
_orig_qapp_quit = QApplication.quit
QApplication.quit = staticmethod(lambda: _quits.append(1))
try:
    w._quit_app()
finally:
    QApplication.quit = _orig_qapp_quit
check("③-a 收模态窗 → close → 发现没走完 → 再收一次 + 再 close",
      _calls == ["modals", "close", "modals", "close"], f"{_calls}")
check("③-b 仍然只退出一次进程", _quits == [1], f"{_quits}")
check("③-c 托盘图标被收起", w._tray.stopped == 1, f"stopped={w._tray.stopped}")

w2 = new_win()
_calls2 = []
w2._close_modal_dialogs_for_quit = lambda: _calls2.append("modals")


def _close_ok():
    _calls2.append("close")
    w2._close_done = True           # 收尾真的跑完了


w2.close = _close_ok
_quits2 = []
QApplication.quit = staticmethod(lambda: _quits2.append(1))
try:
    w2._quit_app()
finally:
    QApplication.quit = _orig_qapp_quit
check("③-d 收尾正常跑完 ⇒ 不再重复 close",
      _calls2 == ["modals", "close"], f"{_calls2}")
check("③-e 正常路径也只退一次", _quits2 == [1], f"{_quits2}")

print("== ④ 操作员点「不退了」：进程不许退 ==")
w3 = new_win()
w3._close_modal_dialogs_for_quit = lambda: None


def _close_cancelled():
    w3._force_quit = False          # closeEvent 里 event.ignore() 会把它复位
    w3._closing = False


w3.close = _close_cancelled
_quits3 = []
QApplication.quit = staticmethod(lambda: _quits3.append(1))
try:
    w3._quit_app()
finally:
    QApplication.quit = _orig_qapp_quit
check("④-a 不退出（没调 QApplication.quit）", _quits3 == [], f"{_quits3}")
check("④-b 托盘图标也不收（还要继续用）", w3._tray.stopped == 0, f"stopped={w3._tray.stopped}")

print("== ⑤ _close_modal_dialogs_for_quit 真能收掉标签页管理器 ==")


class _DummyDlg:
    def __init__(self):
        self.rejected = 0

    def isVisible(self):
        return True

    def reject(self):
        self.rejected += 1


w4 = new_win()
_d = _DummyDlg()
w4._tab_manager_dialog = _d
w4._close_modal_dialogs_for_quit()
check("⑤-a 可见的标签页管理器被 reject", _d.rejected == 1, f"rejected={_d.rejected}")
check("⑤-b 源码先收标签页管理器，再有 activeModalWidget 兜底",
      "_tab_manager_dialog" in _close_modals
      and "activeModalWidget" in _close_modals
      and _close_modals.index("_tab_manager_dialog") < _close_modals.index("activeModalWidget"))

print()
if _fails:
    print(f"❌ 存在失败（{len(_fails)} 条）：" + "、".join(_fails))
    sys.exit(1)
print("✅ 全部通过")
