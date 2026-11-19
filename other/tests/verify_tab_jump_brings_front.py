# -*- coding: utf-8 -*-
"""验收脚本 · 「转到指定标签页」必须把主窗口叫到最前面

用户现场（2026-11-18）：
  「本地打印工具『需要转到指定标签页』的功能（例：关闭时某标签页未完成打印要跳转），
    当主 UI 最小化 / 在托盘 / 非前台时必须把主 UI 前置；现在只转标签页、界面不前置。」

为什么原来不置前：
  `_goto_tab()` 只切 `_current_tab` + 重建表格，一帧都不碰窗口状态；
  而退出流程里唯一会碰窗口的是 `_ensure_visible_for_close_dialog()`，它**只在
  `not self.isVisible()` 时**才恢复（2026-11-01 为了不闪白窗才这么写的）——
  主窗口只是被别的程序盖住时它什么都不做，于是「去到这个标签页」= 切了页、看不见。

判定口径：
  ① 程序自己发起的跳转默认置前（`_goto_tab(key)` 默认 `bring_to_front=True`），
     目标就是当前页时也要置前（调用方意图是"让人看见这一页"）；
  ② 助手批量操作（agent_ops 四处）显式 `bring_to_front=False`，后台切页不抢焦点；
  ③ `_bring_main_window_to_front()` 覆盖三种状态：最小化 / 收在托盘 / 只是被盖住；
  ④ 标签页管理器双击不再手抄一份切页逻辑，统一走 `_goto_tab`（否则置前逻辑会漏）。

    python other/tests/verify_tab_jump_brings_front.py     # exit 0 = 全绿
"""
import io
import os
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_bring_front_")
os.environ["APPDATA"] = _SANDBOX                 # 用户数据目录 → 沙箱（别碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

import gui as _gui  # noqa: E402
import converter as _converter  # noqa: E402

_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}
_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.critical = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.ok = True
        self.status_code = 200
        self.text = "{}"

    def json(self):
        return self._payload


class _HttpRecorder:
    def get(self, *a, **k):
        return _Resp({"success": True})

    def post(self, *a, **k):
        return _Resp({"success": True})


_gui.http_requests = _HttpRecorder()          # 必须早于 MainWindow

from gui import MainWindow  # noqa: E402
from printer_config import PrintJob  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def read(rel):
    with io.open(os.path.join(REPO, rel), encoding="utf-8", errors="replace") as f:
        return f.read()


# ─────────────────────── ① 静态：接线与默认值 ───────────────────────
print("=== ① 静态接线 ===")
gui_src = read("local_print_tool/gui.py")
ops_src = read("local_print_tool/agent_ops.py")
tray_src = read("local_print_tool/tray_icon.py")

check("`_goto_tab` 默认置前（bring_to_front: bool = True）",
      re.search(r"def _goto_tab\(self, key: str, bring_to_front: bool = True\)", gui_src)
      is not None)
check("`_bring_main_window_to_front()` 存在且先走 _restore_from_tray",
      re.search(r"def _bring_main_window_to_front\(self\):[\s\S]{0,900}?_restore_from_tray\(\)",
                gui_src) is not None)
check("置前兜底包含 Win32 SetForegroundWindow（Qt 抢不到前台时用）",
      "SetForegroundWindow" in gui_src)
check("`_goto_tab` 在切页前调用置前（目标=当前页也置前）",
      re.search(r"if bring_to_front:\s*\n\s*#[^\n]*\n\s*self\._bring_main_window_to_front\(\)",
                gui_src) is not None)
check("标签页管理器双击统一走 _goto_tab（不再手抄切页）",
      "_on_double_click" in gui_src
      and re.search(r"def _on_double_click\(row, col\):[\s\S]{0,700}?self\._goto_tab\(key\)",
                    gui_src) is not None
      and "self._cancel_undo_if_active()\n                    self._current_tab = key" not in gui_src)
check("托盘「显示应用」优先走 _bring_main_window_to_front",
      re.search(r"def show_window\(self\)[^\n]*:[\s\S]{0,900}?_bring_main_window_to_front",
                tray_src) is not None)

_ops_calls = re.findall(r"_goto_tab\([^)]*\)", ops_src)
check(f"助手四处切页全部 bring_to_front=False（找到 {len(_ops_calls)} 处）",
      len(_ops_calls) == 4 and all("bring_to_front=False" in c for c in _ops_calls),
      " / ".join(_ops_calls))

# ─────────────────────── ② 行为：真 MainWindow ───────────────────────
print("=== ② 行为：跳转时置前被调用 ===")
win = MainWindow(config_path=paths.config_path())
win.show()
app.processEvents()

calls = []
_real_bring = win._bring_main_window_to_front


def _spy():
    calls.append(win._current_tab)
    # 不真的抢前台（离屏环境没有前台窗口），只记录调用


win._bring_main_window_to_front = _spy

for _k in ("1", "2"):
    _t = win._make_new_tab()
    _t.jobs = [PrintJob(file_path=os.path.join(_SANDBOX, f"f{_k}.pdf"), page_count=1)]
    win._config.tabs[_k] = _t
win._current_tab = "1"
win._config.active_tab = "1"

calls.clear()
ok = win._goto_tab("2")
check("切到别的标签页 → 置前被调用一次", ok is True and len(calls) == 1, f"{calls}")

calls.clear()
ok = win._goto_tab("2")          # 目标就是当前页
check("目标=当前页也要置前（意图是让人看见这一页）", ok is True and len(calls) == 1, f"{calls}")

calls.clear()
ok = win._goto_tab("2", bring_to_front=False)
check("显式 bring_to_front=False 不置前（助手后台切页用）", ok is True and calls == [], f"{calls}")

calls.clear()
ok = win._goto_tab("99")
check("目标页不存在 → 返回 False 且不置前", ok is False and calls == [], f"{calls}")

win._bring_main_window_to_front = _real_bring

# ─────────────────────── ③ 三种窗口状态都要能叫回来 ───────────────────────
print("=== ③ 最小化 / 托盘 / 被盖住 ===")
win.hide()
app.processEvents()
check("夹具：窗口已收进托盘（不可见）", not win.isVisible())
win._bring_main_window_to_front()
app.processEvents()
check("收在托盘 → 置前后窗口可见", win.isVisible())

win.showMinimized()
app.processEvents()
win._bring_main_window_to_front()
app.processEvents()
check("最小化 → 置前后不再最小化",
      not win.isMinimized(), f"isMinimized={win.isMinimized()}")

# 可见但非前台：置前不抛异常、窗口仍可见（前台归属由 Win32 兜底，离屏测不出来）
win.show()
app.processEvents()
try:
    win._bring_main_window_to_front()
    _err = ""
except Exception as e:              # noqa: BLE001
    _err = f"{type(e).__name__}: {e}"
check("可见但非前台 → 置前不抛异常且窗口仍可见", _err == "" and win.isVisible(), _err)

print("=== 收尾 ===")
win._force_quit = True
try:
    win.close()
except Exception:
    pass
app.processEvents()

print("全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
