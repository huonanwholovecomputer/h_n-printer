#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收：打印列表（打印队列）—— 忙时入队 / 上移 / 置顶 / 移出 / 落盘恢复（2026-11-23 用户需求）。

用户原话：「现在的队列总是会因为打印正在进行就无法加入队列。此外还需要完善打印队列的
功能，比如删除任务，提前任务等常见的功能。」

本脚本钉住的口径（改了任何一条都要先改这里，再改代码）：
  1. 打印机被占着时点「开始打印」**不再被拒绝**：这一批排进「🖨 打印列表」，前面打完自动接上；
  2. 攒批模式的「第 1 趟单」（`stack_entry` 非空）不走这条队列 —— 它有自己的 `_stack_entry_wait`；
  3. 同一标签页重复点 = 按当前文件清单**更新**排队内容，不产生第二个队列项；
  4. 排队行带 `queue_id`，才能被上移 / 置顶 / 移出；**正在打印那一行没有 id ⇒ 天然不可删/不可移**；
  5. 队列落盘到 `print_config.json`（`PrinterConfig.print_queue`），重启后恢复为
     「待确认」(`resume=True`)，必须操作员点「▶ 继续队列」才开打（开机不许自己出纸）；
  6. 队列项里的暂存副本必须活到开打那一刻（`_staged_reference_paths` 要算上它们）。

运行：python other/tests/verify_print_queue.py     退出码 0 = 全绿
"""
import os
import sys
import tempfile
import types

_SANDBOX = tempfile.mkdtemp(prefix="hn_print_queue_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_TOOL = os.path.normpath(os.path.join(HERE, "..", "..", "local_print_tool"))
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui  # noqa: E402
import printer_config  # noqa: E402
from printer_config import PrintJob, PrinterConfig  # noqa: E402

_app = QApplication.instance() or QApplication([])

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f"  — {detail}" if detail else ""))
        FAILS.append(name)


def job(path, pages=3, name=""):
    return PrintJob(file_path=path, page_count=pages, display_name=name or os.path.basename(path))


class FakeWorker:
    """够 `_print_busy_state` 用的假 worker。"""

    def __init__(self, running=True, jobs=None):
        self._running = bool(running)
        self._jobs = list(jobs or [])

    def isRunning(self):
        return self._running


def fake_tab(jobs=None):
    return types.SimpleNamespace(jobs=list(jobs or []), frozen=False, order_number="")


class Host:
    """只带队列相关属性的宿主：把 MainWindow 的方法绑上来跑，不建整个窗口。"""

    def __init__(self):
        self._config = PrinterConfig()
        self._config.tabs = {}
        self._print_queue = []
        self._print_queue_seq = 0
        self._print_queue_draining = False
        self._print_starting = False
        self._closing = False
        self._current_tab = "t1"
        self._worker = None
        self._stack_flush_worker = None
        self._remedy_worker = None
        self._flat_jobs = []
        self._cleared_jobs_backup = {}
        self._edit_undo_stack = []
        self._edit_redo_stack = []
        self.logs = []
        self.saves = 0
        self.sweeps = []
        self.started = []
        self.start_ok = True

    # ── 被绑定的 MainWindow 方法需要的胶水 ──
    def _log(self, msg):
        self.logs.append(str(msg))

    def _save_config(self, *a, **k):
        self.saves += 1

    def _refresh_tab_display(self):
        pass

    def _sync_local_queue(self):
        pass

    def _has_members(self):
        return True

    def _show_no_member_hint(self):
        pass

    def _stack_gate(self, *a, **k):
        return ""

    def _sweep_staged_files(self, reason="", *a, **k):
        self.sweeps.append(str(reason))

    def _prune_edit_history(self):
        pass

    def _cloud_kept_paths(self):
        return set()

    def _start_print_worker(self, flat_jobs, tab_key=None, stack_entry=None, interactive=True):
        self.started.append((list(flat_jobs), tab_key, bool(interactive)))
        return self.start_ok

    def log_text(self):
        return "\n".join(self.logs)


def bind(host, *names):
    for n in names:
        setattr(host, n, types.MethodType(getattr(gui.MainWindow, n), host))


QUEUE_METHODS = ("_print_busy_state", "_queue_index_of_tab", "_enqueue_print_batch",
                 "_save_print_queue", "_restore_print_queue", "_drain_print_queue",
                 "_on_queue_action", "_tab_queued_reason", "_staged_reference_paths")


def new_host():
    h = Host()
    bind(h, *QUEUE_METHODS)
    return h


print("=" * 78)
print("0) 代码在场（防止改动被回滚成「忙就拒绝」）")
src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
cfg_src = open(os.path.join(LOCAL_TOOL, "printer_config.py"), encoding="utf-8").read()
check("0a 旧的「已有打印任务正在进行，不能重复启动」已从 gui.py 消失",
      "已有打印任务正在进行，不能重复启动" not in src)
check("0b 忙分支改为入队",
      "return self._enqueue_print_batch(flat_jobs, tab_key, _busy)" in src)
check("0c 攒批第 1 趟仍然拒绝（不走这条队列）",
      "return False" in src[src.index("_busy = self._print_busy_state()"):][:900]
      and "_enqueue_print_batch(flat_jobs, tab_key, _busy)" in src)
check("0d 普通批次收尾后排空队列",
      src.count("QTimer.singleShot(600, lambda: self._drain_print_queue(") == 2)
check("0e 两条排空钩子各一处（普通收尾 + 整叠第 2 趟收尾）",
      src.count("_drain_print_queue(") >= 4)
check("0f 对话框信号已定义", "queue_action_requested = Signal(str, int)" in src)
check("0g 对话框信号已接到主窗口",
      "dlg.queue_action_requested.connect(self._on_queue_action)" in src)
_staged_src = src[src.index("def _staged_reference_paths("):]
_staged_src = _staged_src[:_staged_src.index("\n    def ", 10)]
check("0h 队列副本参与暂存保留", "_print_queue" in _staged_src)
check("0i 启动时恢复队列（在启动暂存清扫之前）",
      src.index("self._restore_print_queue()") < src.index("_sweep_staged_files("))
_rows_src = src[src.index("def _local_queue_rows("):]
_rows_src = _rows_src[:_rows_src.index("\n    def ", 10)]
check("0j 排队行排在「其它等待」之前",
      _rows_src.index("_print_queue")
      < _rows_src.index('for req in (getattr(self, "_stack_entry_wait"'))
check("0k printer_config 有 print_queue 字段与规范化函数",
      "print_queue: list[dict]" in cfg_src and "def _sanitize_print_queue(" in cfg_src)

print("\n" + "=" * 78)
print("1) 落盘格式：_sanitize_print_queue（脏配置不许把队列带崩）")
S = printer_config._sanitize_print_queue
check("1a 非 list → 空", S(None) == [] and S("x") == [] and S(3) == [])
check("1b 空批次整项丢弃（空批次卡队首永远打不出纸）",
      S([{"id": 1, "jobs": []}, {"id": 2, "jobs": [{"file_path": "a"}]}]) ==
      [{"id": 2, "tab": "", "created": 0.0, "resume": True, "jobs": [{"file_path": "a"}]}])
check("1c 只留白名单键（多余键不进配置）",
      "junk" not in S([{"id": 3, "junk": "x", "jobs": [{"file_path": "a"}]}])[0])
check("1d resume 缺省 True（从磁盘捡回来的一律要人工放行）",
      S([{"id": 4, "jobs": [{"file_path": "a"}]}])[0]["resume"] is True)
check("1e resume False 原样保留",
      S([{"id": 5, "resume": False, "jobs": [{"file_path": "a"}]}])[0]["resume"] is False)
check("1f 坏 id 兜底 0", S([{"id": "x", "jobs": [{"file_path": "a"}]}])[0]["id"] == 0)
check("1g jobs 里的非 dict 项被丢掉",
      S([{"id": 6, "jobs": [{"file_path": "a"}, "junk", None]}])[0]["jobs"] == [{"file_path": "a"}])

print("\n" + "=" * 78)
print("2) 配置往返（关窗/重启后还在）")
c = PrinterConfig()
check("2a 默认空队列", c.print_queue == [])
j1 = job("C:/tmp/q1.pdf", 5, "q1.pdf")
j2 = job("C:/tmp/q2.pdf", 2, "q2.pdf")
c.print_queue = [{"id": 1, "tab": "t1", "created": 123.5, "resume": False,
                  "jobs": [j1.to_dict(), j2.to_dict()]}]
c2 = PrinterConfig.from_dict(c.to_dict())
check("2b 往返保住 1 项 / 2 个任务", len(c2.print_queue) == 1
      and len(c2.print_queue[0]["jobs"]) == 2, c2.print_queue)
check("2c 往返保住 tab / created / resume",
      c2.print_queue[0]["tab"] == "t1" and abs(c2.print_queue[0]["created"] - 123.5) < 1e-6
      and c2.print_queue[0]["resume"] is False)
back = PrintJob.from_dict(c2.print_queue[0]["jobs"][0])
check("2d PrintJob 往返保住路径与页数",
      back.file_path == "C:/tmp/q1.pdf" and back.page_count == 5 and back.display_name == "q1.pdf")
old = {"simplex_price": 0.3}   # 老配置：没有 print_queue 键
check("2e 老配置缺键 → 空队列（零回归）", PrinterConfig.from_dict(old).print_queue == [])

print("\n" + "=" * 78)
print("3) 忙时点「开始打印」= 入队（不再拒绝）")
h = new_host()
bind(h, "_start_print_worker")
h._worker = FakeWorker(True)          # 打印机被占着
_h_jobs = [job("C:/tmp/a.pdf", 3), job("C:/tmp/b.pdf", 4)]
ok = h._start_print_worker(_h_jobs, tab_key="t1")
check("3a 返回 True（已受理）", ok is True)
check("3b 队列里出现 1 项", len(h._print_queue) == 1, h._print_queue)
check("3c 记在 t1 上", h._print_queue[0]["tab"] == "t1")
check("3d 这一项不是「待确认」", h._print_queue[0]["resume"] is False)
check("3e 带着 2 个任务", len(h._print_queue[0]["jobs"]) == 2)
check("3f 日志说明已加入队列", "已加入打印列表" in h.log_text(), h.log_text())
check("3g 已落盘（配置里也有）", len(h._config.print_queue) == 1)

h._start_print_worker([job("C:/tmp/c.pdf")], tab_key="t1")
check("3h 同一标签页重复点 → 更新而不是新增一项", len(h._print_queue) == 1)
check("3i 更新成新的文件清单", len(h._print_queue[0]["jobs"]) == 1)
check("3j 日志说明是「更新排队内容」", "更新排队内容" in h.log_text())

h._start_print_worker([job("C:/tmp/d.pdf")], tab_key="t2")
check("3k 另一个标签页 → 排到第 2 位", len(h._print_queue) == 2)
check("3l 位置查询：t1=1 / t2=2 / 未知=0",
      (h._queue_index_of_tab("t1"), h._queue_index_of_tab("t2"),
       h._queue_index_of_tab("t9"), h._queue_index_of_tab("")) == (1, 2, 0, 0))
check("3m 标签页徽标文案带位置",
      h._tab_queued_reason("t1").startswith("打印列表第 1 位")
      and "下一个就是它" in h._tab_queued_reason("t1")
      and "前面还有 1 个批次" in h._tab_queued_reason("t2"),
      (h._tab_queued_reason("t1"), h._tab_queued_reason("t2")))

h2 = new_host()
bind(h2, "_start_print_worker")
h2._worker = FakeWorker(True)
check("3n 攒批第 1 趟（stack_entry 非空）仍然拒绝、不进队列",
      h2._start_print_worker([job("C:/tmp/e.pdf")], tab_key="t1", stack_entry=(1, "x", True)) is False
      and h2._print_queue == [])
h2._remedy_worker = FakeWorker(True)
h2._worker = None
h2._start_print_worker([job("C:/tmp/f.pdf")], tab_key="t1")
check("3o 半成品补打在飞时也走队列", len(h2._print_queue) == 1
      and "半成品补打" in h2.log_text())

print("\n" + "=" * 78)
print("4) 队列项参与暂存保留（副本必须活到开打那一刻）")
h._config.tabs = {"t1": fake_tab([job("C:/tmp/tab1.pdf")])}
h._print_queue = [{"id": 9, "tab": "t1", "created": 0.0, "resume": False,
                   "jobs": [job("C:/tmp/queued.pdf")]}]
refs = h._staged_reference_paths()
_queued = os.path.normcase(os.path.abspath("C:/tmp/queued.pdf"))
_tabbed = os.path.normcase(os.path.abspath("C:/tmp/tab1.pdf"))
check("4a 队列里的副本被算作仍被引用", _queued in refs, refs)
check("4b 标签页里的任务也算", _tabbed in refs)

print("\n" + "=" * 78)
print("5) 排空队列：打印机空了才接上、待确认的必须点「继续队列」")


def drain_host():
    hh = new_host()
    hh._config.tabs = {"t1": fake_tab(), "t2": fake_tab()}
    return hh


d = drain_host()
check("5a 空队列 → False", d._drain_print_queue() is False and d.started == [])
d._print_queue = [{"id": 1, "tab": "t1", "created": 0.0, "resume": False, "jobs": [job("C:/tmp/1.pdf")]}]
d._worker = FakeWorker(True)
check("5b 打印机正忙 → 不启动、队列原样", d._drain_print_queue() is False
      and len(d._print_queue) == 1 and d.started == [])
d._worker = None
d._print_queue[0]["resume"] = True
check("5c 待确认项 + 没有 force → 不启动", d._drain_print_queue() is False
      and len(d._print_queue) == 1 and d.started == [])
check("5d 待确认项 + force → 开打并出队", d._drain_print_queue("测试", force=True) is True
      and d._print_queue == [] and len(d.started) == 1)
check("5e 开打时带上原标签页", d.started[0][1] == "t1" and d.started[0][2] is True)
check("5f 日志说明这一批接着打完",
      any("打印列表继续" in x for x in d.logs), d.logs)

d2 = drain_host()
d2._print_queue = [
    {"id": 2, "tab": "gone-tab", "created": 0.0, "resume": False, "jobs": [job("C:/tmp/x.pdf")]},
    {"id": 3, "tab": "t2", "created": 0.0, "resume": False, "jobs": [job("C:/tmp/y.pdf")]},
]
check("5g 所属标签页已删除的队首被丢弃、下一项照打", d2._drain_print_queue() is True
      and d2._print_queue == [] and len(d2.started) == 1 and d2.started[0][1] == "t2")
check("5h 丢弃有日志", any("丢弃 1 个所属标签页已删除的批次" in x for x in d2.logs), d2.logs)

d3 = drain_host()
d3._print_queue = [{"id": 4, "tab": "t1", "created": 0.0, "resume": False, "jobs": [job("C:/tmp/z.pdf")]}]
d3.start_ok = False
check("5i 没开起来 → 原样放回第 1 位（不丢活）", d3._drain_print_queue() is False
      and len(d3._print_queue) == 1 and d3._print_queue[0]["id"] == 4)
check("5j 放回有日志", any("已放回第 1 位" in x for x in d3.logs), d3.logs)
d3.start_ok = True
d3._print_queue_draining = True
check("5k 重入闸门（收尾里再进来一次不会重复开打）", d3._drain_print_queue() is False
      and len(d3._print_queue) == 1)

print("\n" + "=" * 78)
print("6) 行级操作：上移 / 置顶 / 移出 / 继续队列")
q = drain_host()
q._print_queue = [
    {"id": 11, "tab": "t1", "created": 0.0, "resume": False, "jobs": [job("C:/tmp/1.pdf")]},
    {"id": 12, "tab": "t2", "created": 0.0, "resume": False, "jobs": [job("C:/tmp/2.pdf")]},
    {"id": 13, "tab": "t3", "created": 0.0, "resume": False, "jobs": [job("C:/tmp/3.pdf")]},
]
q._on_queue_action("up", 13)
check("6a 上移一位", [it["id"] for it in q._print_queue] == [11, 13, 12], q._print_queue)
q._on_queue_action("top", 12)
check("6b 置顶", [it["id"] for it in q._print_queue] == [12, 11, 13], q._print_queue)
q._on_queue_action("up", 12)
check("6c 已在最前面时上移 = 不动", [it["id"] for it in q._print_queue] == [12, 11, 13])
q._on_queue_action("del", 11)
check("6d 移出队列", [it["id"] for it in q._print_queue] == [12, 13], q._print_queue)
check("6e 移出后顺手回收暂存副本", q.sweeps and q.sweeps[-1] == "队列项移出", q.sweeps)
check("6f 移出不等于删文件（日志写明文件还在标签页里）",
      any("文件仍在原标签页里" in x for x in q.logs), q.logs)
n_before = len(q._print_queue)
q._on_queue_action("del", 999)
check("6g 找不到的 id → 只记日志、不动队列",
      len(q._print_queue) == n_before and any("已经不在列表里了" in x for x in q.logs))
q._worker = None
q.start_ok = True
for it in q._print_queue:
    it["resume"] = True
q._on_queue_action("resume_all", 0)
check("6h 「继续队列」清掉全部待确认标记", all(not it["resume"] for it in q._print_queue)
      or q._print_queue == [])
check("6i 「继续队列」立刻开打队首", len(q.started) == 1 and q.started[0][1] == "t2",
      q.started)

print("\n" + "=" * 78)
print("7) 恢复：重启后一律是「待确认」，不许自己出纸")
r = new_host()
r._config.print_queue = [
    {"id": 0, "tab": "t1", "created": 1.0, "resume": False,
     "jobs": [job("C:/tmp/r1.pdf").to_dict()]},
    {"id": 5, "tab": "t2", "created": 2.0, "resume": False,
     "jobs": [job("C:/tmp/r2.pdf").to_dict(), job("C:/tmp/r3.pdf").to_dict()]},
    {"id": 6, "tab": "t3", "created": 3.0, "resume": True, "jobs": []},
]
n = r._restore_print_queue()
check("7a 恢复 2 项（空批次被丢弃）", n == 2 and len(r._print_queue) == 2, (n, r._print_queue))
check("7b 一律标 resume=True", all(it["resume"] is True for it in r._print_queue))
check("7c id 为 0/缺失的重新编号、且不撞号",
      [it["id"] for it in r._print_queue] == [1, 5] and r._print_queue_seq >= 5,
      [(it["id"], r._print_queue_seq) for it in r._print_queue])
check("7d 任务是真 PrintJob 对象",
      all(isinstance(j, PrintJob) for it in r._print_queue for j in it["jobs"]))
check("7e 恢复有日志（点「继续队列」才开始打）",
      any("已恢复上次退出时还没开打的" in x for x in r.logs), r.logs)
r._worker = FakeWorker(True)   # 还有别的东西在打
check("7f 恢复后不会自己开打（忙 + 待确认两道闸门）",
      r._drain_print_queue() is False and r.started == [])

print("\n" + "=" * 78)
print("8) 对话框：哪几行能删/能移、按钮会不会误点")
dlg = gui.StackQueueDialog(None)
emitted = []
dlg.queue_action_requested.connect(lambda a, q: emitted.append((a, q)))
rows = [
    {"state": "进行中", "source": "本机点击", "content": "正在打的.pdf",
     "sheets": "5 张", "eta": "剩 1 分", "active": True},
    {"state": "排队中（第 1 位）", "source": "本机点击", "content": "排队的.pdf",
     "sheets": "3 张", "eta": "--", "active": False, "queue_id": 7},
    {"state": "排队中（第 2 位）· 待确认", "source": "本机点击", "content": "待确认.pdf",
     "sheets": "2 张", "eta": "--", "active": False, "queue_id": 8, "resume": True},
]
dlg.set_local_queue(rows)
tree = dlg._tree_local
check("8a 三行都画出来", tree.topLevelItemCount() == 3)
check("8b 排队行带 queue_id",
      tree.topLevelItem(1).data(0, Qt.UserRole) == 7
      and tree.topLevelItem(2).data(0, Qt.UserRole) == 8)
check("8c 进行中那一行没有 queue_id（天然不可删）",
      int(tree.topLevelItem(0).data(0, Qt.UserRole) or 0) == 0)
check("8d 有「▶ 继续队列」按钮且当前可点", dlg._btn_q_resume.isEnabled() is True)
check("8e 没选中时上移/置顶/移出是灰的",
      not dlg._btn_q_up.isEnabled() and not dlg._btn_q_top.isEnabled()
      and not dlg._btn_q_del.isEnabled())
tree.setCurrentItem(tree.topLevelItem(1))
dlg._update_queue_buttons()
check("8f 选中排队行 → 三个按钮可点",
      dlg._btn_q_up.isEnabled() and dlg._btn_q_top.isEnabled() and dlg._btn_q_del.isEnabled())
dlg._btn_q_up.click()
check("8g 点「上移」发出 (up, 7)，不是自己去改队列", emitted[-1] == ("up", 7), emitted)
tree.setCurrentItem(tree.topLevelItem(0))
dlg._update_queue_buttons()
check("8h 选中正在打的那一行 → 三个按钮又变灰",
      not dlg._btn_q_up.isEnabled() and not dlg._btn_q_del.isEnabled())
n_emit = len(emitted)
dlg._btn_q_del.click()
check("8i 正在打的那一行点「移出」发不出任何请求", len(emitted) == n_emit)
dlg.set_local_queue([])
check("8j 队列清空后按钮全灰", not dlg._btn_q_resume.isEnabled()
      and not dlg._btn_q_up.isEnabled())
dlg.deleteLater()

print("\n" + "=" * 78)
if FAILS:
    print(f"✗ FAIL {len(FAILS)} 条：")
    for f in FAILS:
        print(f"   · {f}")
    sys.exit(1)
print("✓ 全部通过")
sys.exit(0)
