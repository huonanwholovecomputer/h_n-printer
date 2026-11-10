# -*- coding: utf-8 -*-
"""预约单「挂起等人工翻纸」的标签页必须允许人工开打（客户端侧验收）

现场（2026-11-10，HN20261110-0005 / 订单 id 1252）：预约单到点先 `_lock_scheduled_tab()`
把标签页冻住（🔒），紧接着 `_hold_for_manual_duplex()` 挂起并在日志里写
「本机已挂起（不回报失败），请人工点「开始打印」」—— 而 `_on_start_print()` 开头就是
「已固定 ⇒ 不能重复打印」，`_btn_start` 也是灰的。程序让人去做一件它自己会拒绝的事，
用户最后只能把文件复制到新标签页才打出来；原单永远停在 waiting，后端每 30 秒重发一次
兜底 `start_print`（后来才加了节流）。

本脚本钉住修复后的口径：
  · ⏸ 挂起态（`manual_duplex_pending` 且还有未打的任务）**可开打**，按钮必须是亮的；
  · 普通冻结页（打印完成后的 🔒）仍然不许重复打印，按钮仍是灰的；
  · 挂起但任务都已 `sent`（其实已经打完了）⇒ 不许再打；
  · 未冻结页零回归。

用法: python other/tests/verify_scheduled_manual_hold_start.py   （exit 0 = 全部通过）
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_manual_hold_start_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui      # noqa: E402
import paths    # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


class Sink:
    def __init__(self):
        self.lines = []
        self.started = 0

    def log(self, msg):
        self.lines.append(str(msg))

    def text(self):
        return "\n".join(self.lines)


def new_win():
    w = gui.MainWindow(config_path=paths.config_path())
    w._cloud_client = None
    w._cloud_connected = True
    sink = Sink()
    w._log = sink.log

    def _start(*a, **kw):
        sink.started += 1
        return True

    w._start_print_worker = _start
    return w, sink


def put_tab(win, key, frozen=False, manual_pending=False, sent=False, n_jobs=1):
    tab = gui.TabSettings()
    tab.jobs = [
        gui.PrintJob(file_path=os.path.join(_SANDBOX, f"{key}-{i}.pdf"),
                     order_id=900 + i, task_id=900 + i, page_count=2, sent=sent)
        for i in range(n_jobs)
    ]
    tab.frozen = frozen
    tab.manual_duplex_pending = manual_pending
    win._config.tabs[key] = tab
    return tab


def show(win, key):
    """把某个标签页设为当前页并刷新界面（按钮状态就位）。"""
    win._current_tab = key
    win._refresh_tab_display()


# ══════════════════════════════════════════════════════════════════════════
# ① 静态接线：两处都必须用同一个判据
# ══════════════════════════════════════════════════════════════════════════
print("\n=== ① 静态接线 ===")
_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
check("①-a 有 _tab_manual_hold_startable 判据",
      "def _tab_manual_hold_startable" in _src)
_osp = _src.split("def _on_start_print", 1)[1].split("\n    def ", 1)[0]
check("①-b _on_start_print 在冻结分支里放行挂起态",
      "_tab_manual_hold_startable" in _osp and "预约单挂起后的人工开打" in _osp,
      _osp[:200].replace("\n", " ⏎ "))
_rtd = _src.split("def _refresh_tab_display", 1)[1].split("\n    def ", 1)[0]
check("①-c _refresh_tab_display 的打印按钮按同一判据放行",
      "_hold_start" in _rtd and "_tab_manual_hold_startable(tab)" in _rtd)
check("①-d 判据只认「挂起 + 还有没打的任务」",
      'manual_duplex_pending' in _src.split("def _tab_manual_hold_startable", 1)[1]
      .split("\n    def ", 1)[0])

# ══════════════════════════════════════════════════════════════════════════
# ② 挂起态：按钮必须亮、点得动
# ══════════════════════════════════════════════════════════════════════════
print("\n=== ② 挂起态可开打（🔴19 主场景） ===")
w, sink = new_win()
put_tab(w, "1", frozen=True, manual_pending=True)
show(w, "1")
check("②-a 冻结 + 挂起 + 有未打任务 ⇒ 开始按钮是亮的",
      w._btn_start.isEnabled())
check("②-b 按钮 tooltip 说清「预约单挂起等人工翻纸」",
      "预约单挂起等人工翻纸" in (w._btn_start.toolTip() or ""),
      w._btn_start.toolTip())
w._on_start_print()
check("②-c 点了就真的开打（不是静默丢弃）", sink.started == 1, f"started={sink.started}")
check("②-d 日志讲清这是挂起后的人工开打",
      "预约单挂起后的人工开打" in sink.text(), sink.text())
check("②-e 不再出现「不能重复打印」的死路文案",
      "不能重复打印" not in sink.text(), sink.text())

# ══════════════════════════════════════════════════════════════════════════
# ③ 普通冻结页：仍然不许重复打印
# ══════════════════════════════════════════════════════════════════════════
print("\n=== ③ 普通 🔒 页零回归 ===")
w2, sink2 = new_win()
put_tab(w2, "1", frozen=True, manual_pending=False)
show(w2, "1")
check("③-a 冻结且没挂起 ⇒ 按钮仍是灰的", not w2._btn_start.isEnabled())
w2._on_start_print()
check("③-b 点了也不打（照旧拒绝）", sink2.started == 0, f"started={sink2.started}")
check("③-c 日志仍是「该标签页已固定，不能重复打印」",
      "不能重复打印" in sink2.text(), sink2.text())
check("③-d 默认 tooltip 没被清空",
      (w2._btn_start.toolTip() or "") != "" and "预约单挂起" not in (w2._btn_start.toolTip() or ""),
      w2._btn_start.toolTip())

# ══════════════════════════════════════════════════════════════════════════
# ④ 挂起但已打完：不许再打（防双倍出纸）
# ══════════════════════════════════════════════════════════════════════════
print("\n=== ④ 挂起但任务已 sent ===")
w3, sink3 = new_win()
put_tab(w3, "1", frozen=True, manual_pending=True, sent=True)
show(w3, "1")
check("④-a 全部 sent ⇒ 按钮灰的", not w3._btn_start.isEnabled())
w3._on_start_print()
check("④-b 全部 sent ⇒ 拒绝开打", sink3.started == 0, f"started={sink3.started}")
check("④-c 日志仍是固定页文案", "不能重复打印" in sink3.text(), sink3.text())

# ══════════════════════════════════════════════════════════════════════════
# ⑤ 未冻结页零回归
# ══════════════════════════════════════════════════════════════════════════
print("\n=== ⑤ 未冻结页零回归 ===")
w4, sink4 = new_win()
put_tab(w4, "1", frozen=False, manual_pending=True)
show(w4, "1")
check("⑤-a 未冻结 ⇒ 按钮亮", w4._btn_start.isEnabled())
w4._on_start_print()
check("⑤-b 未冻结 ⇒ 正常开打", sink4.started == 1, f"started={sink4.started}")
check("⑤-c 未冻结不看挂起文案（走正常路径）",
      "预约单挂起后的人工开打" not in sink4.text(), sink4.text())

# ══════════════════════════════════════════════════════════════════════════
# ⑥ 判据本身：None / 无属性 / 空 jobs
# ══════════════════════════════════════════════════════════════════════════
print("\n=== ⑥ 判据边界 ===")
check("⑥-a None ⇒ False", w._tab_manual_hold_startable(None) is False)


class _Bare:
    pass


_bare = _Bare()
check("⑥-b 没有 manual_duplex_pending 属性 ⇒ False",
      w._tab_manual_hold_startable(_bare) is False)
_empty = gui.TabSettings()
_empty.jobs = []
_empty.manual_duplex_pending = True
check("⑥-c 挂起但一个任务都没有 ⇒ False（没什么可打）",
      w._tab_manual_hold_startable(_empty) is False)

print("\n" + ("全部通过 ✅" if ok_all else "存在失败 ❌"))
sys.exit(0 if ok_all else 1)
