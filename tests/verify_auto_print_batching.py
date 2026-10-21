# -*- coding: utf-8 -*-
"""批次 B4 验收：无障碍订单被拆成多批（现场 #535 的 11 个文件分 7 批 / 同一订单号报 4 次）

现场（docs/审查报告_2026-10/日志诊断_2026-10-14.md）
  · 订单 #535 的 11 个文件分 7 批打印、同一订单号被上报 4 次（3+2+6+1 = 12 > 11，确有重复）
  · 任务 #1062 上报"打印成功"两次；#1063 的 ready 事件一秒内被处理 10 次

三处根因
  B4-1 ready 分支缺幂等守卫：唯一守卫 `_processed_cloud_tasks` 只在**真正处理后**才写，
       "下载完成 → 防抖到期"之间存在数秒窗口，期间任何重复 task_updated(ready)
       （SocketIO 重推 / HTTP 兜底 / 状态队列重放）都会再次入队并**反复重置防抖**。
  B4-2 "整单到齐"判据是 3 秒空闲防抖，而文件是**逐个下载完成**的（现场跨度 34 秒），
       相邻间隔 > 3 秒即被"击穿"一次 ⇒ 每批先到的文件被当整单处理并 pop 清空。
  B4-3 忙时重试队列与新批次并发抢同一批 jobs：`jobs` 是"当前标签页所有未 sent 的文件"，
       旧写法无条件 append ⇒ 同一订单多条条目、task_ids 互相重叠 ⇒ 补打时重复打印；
       且补打"先 pop 再启动"，启动失败即**静默丢单**。

⚠ B4-2 的**根治**需要后端在 push/accept 响应里下发订单级 file_count（客户端无从得知
  该订单总共几个文件）。本脚本只验收客户端侧能做的部分：幂等、防抖窗口与 timer 复用、
  重试队列去重、补打失败回队。
"""
import os
import re
import shutil
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_b4_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

_app = QApplication.instance() or QApplication([])
QMessageBox.information = staticmethod(lambda *a, **kw: QMessageBox.Ok)
QMessageBox.warning = staticmethod(lambda *a, **kw: QMessageBox.Ok)
QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.Yes)

import gui    # noqa: E402
import paths  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


class FakeTask:
    def __init__(self, task_id, order_id, status="ready", auto_print=True):
        self.task_id = task_id
        self.order_id = order_id
        self.status = status
        self.auto_print = auto_print
        self.file_name = f"f{task_id}.pdf"
        self.schedule_mode = "now"


class CloudStub:
    def __init__(self):
        self.accepted = []
        self.success = []
        self.failed = []

    def accept_order_to_server(self, order_id):
        self.accepted.append(order_id)
        return "ok"

    def report_success(self, task_id, payload=None):
        self.success.append(task_id)

    def report_fail(self, task_id, reason=""):
        self.failed.append((task_id, reason))

    def abandon_order_to_server(self, *a, **kw):
        pass


def new_win(start_ok=True):
    w = gui.MainWindow(config_path=paths.config_path())
    w._cloud_client = CloudStub()
    w._auto_print_queue = {}
    w._auto_print_timers = {}
    w._enqueued_cloud_tasks = set()
    w._auto_print_retry = []
    w._config.tabs = {"1": gui.TabSettings()}
    w._current_tab = "1"
    w._config.active_tab = "1"
    w._manual_flip_required_for = lambda jobs: False
    w._mark_processed_task = lambda tid: w._processed_cloud_tasks.add(tid)

    def _add(tasks, switch_tab=True):
        tab = w._config.tabs.setdefault("1", gui.TabSettings())
        for t in tasks:
            if not any(getattr(j, "task_id", 0) == t.task_id for j in tab.jobs):
                tab.jobs.append(gui.PrintJob(file_path=os.path.join(_SANDBOX, t.file_name),
                                             task_id=t.task_id, order_id=t.order_id,
                                             page_count=1))
        return "1"

    w._add_cloud_tasks_to_new_tab = _add
    w._start_print_worker = lambda jobs, **kw: start_ok
    return w


# ══════════════════════════════════════════════════════════════════════════
print("=== 1. 🔴-B4-1 同一 task_id 的重复 ready 只入队一次，且不重建防抖 timer ===")
win = new_win()
t = FakeTask(1063, 535)
win._enqueue_auto_print_task(t)
timer1 = win._auto_print_timers.get(535)
win._enqueue_auto_print_task(t)          # SocketIO 重推
win._on_cloud_task_updated(FakeTask(1063, 535))   # HTTP 兜底重放
check("队列里只有 1 个文件（不重复入队）",
      len(win._auto_print_queue.get(535, [])) == 1,
      f"{len(win._auto_print_queue.get(535, []))}")
check("task_id 已登记进幂等集合", 1063 in win._enqueued_cloud_tasks)
check("防抖 timer 未被重建（同一只对象 —— 旧写法每次都 new QTimer(self)）",
      win._auto_print_timers.get(535) is timer1)

print("\n=== 2. 处理完成后，同一 task_id 的迟到 ready 被 _processed_cloud_tasks 拦 ===")
win._process_auto_print_order(535)
check("出队即解除「已入队」守卫", 1063 not in win._enqueued_cloud_tasks)
check("已进 _processed_cloud_tasks", win._is_processed_task(1063) is True)
win._enqueue_auto_print_task(FakeTask(1063, 535))
check("迟到 ready 不再入队", 535 not in win._auto_print_queue,
      f"{list(win._auto_print_queue)}")
check("accept_order 只被调用 1 次", len(win._cloud_client.accepted) == 1,
      f"{win._cloud_client.accepted}")

print("\n=== 3. 🔴-B4-3 打印机忙时，同一订单在重试队列里只保留一条（task_ids 取并集）===")
win3 = new_win(start_ok=False)
win3._enqueue_auto_print_task(FakeTask(201, 601))
win3._process_auto_print_order(601)
check("第一批已入重试队列", len(win3._auto_print_retry) == 1,
      f"{len(win3._auto_print_retry)}")
check("task_ids = [201]", win3._auto_print_retry[0]["task_ids"] == [201],
      f"{win3._auto_print_retry[0]['task_ids']}")
# 第二批（同订单的后续文件）到达 → 旧写法会 append 第二条
win3._enqueue_auto_print_task(FakeTask(202, 601))
win3._process_auto_print_order(601)
check("仍只有一条（不新增重复条目）", len(win3._auto_print_retry) == 1,
      f"{len(win3._auto_print_retry)}")
check("task_ids 已并集为 [201,202]（补打时不会重复提交同一任务）",
      win3._auto_print_retry[0]["task_ids"] == [201, 202],
      f"{win3._auto_print_retry[0]['task_ids']}")

print("\n=== 4. 🔴-B4-3 补打启动失败必须留在队列（旧写法先 pop 再启动 ⇒ 静默丢单）===")
win4 = new_win(start_ok=False)          # 补打时"又忙了"
win4._config.tabs["1"].jobs = [gui.PrintJob(file_path="x.pdf", task_id=301,
                                            order_id=701, page_count=1)]
win4._auto_print_retry = [{"order_id": 701, "tab_key": "1", "task_ids": [301]}]
win4._on_all_finished(1, 0)
check("条目仍留在重试队列（不静默丢单）", len(win4._auto_print_retry) == 1,
      f"{len(win4._auto_print_retry)}")

print("\n=== 5. 补打启动成功 → 条目出队；标签页已无未打印文件 → 条目出队 ===")
win4._start_print_worker = lambda jobs, **kw: True     # 打印机空了
win4._on_all_finished(1, 0)
check("启动成功 → 出队", win4._auto_print_retry == [], f"{win4._auto_print_retry}")
win4._config.tabs["1"].jobs = [gui.PrintJob(file_path="x.pdf", task_id=501,
                                            order_id=901, page_count=1, sent=True)]
win4._auto_print_retry = [{"order_id": 901, "tab_key": "1", "task_ids": [501]}]
win4._on_all_finished(1, 0)
check("无可打文件 → 出队（不无限重试）", win4._auto_print_retry == [],
      f"{win4._auto_print_retry}")

print("\n=== 6. 防抖窗口与根治路径（静态）===")
check("防抖常量已从 3s 提到 ≥8s（客户端缓解）",
      getattr(gui, "AUTO_PRINT_DEBOUNCE_MS", 0) >= 8000,
      f"{getattr(gui, 'AUTO_PRINT_DEBOUNCE_MS', None)}")
src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
check("排队/防抖路径不再有硬编码的 3000ms 会话",
      re.search(r"timer\.start\(3000\)", src) is None)
check("已无「队列内 any() 去重」的旧写法（改由 _enqueued_cloud_tasks 承担）",
      "if not any(t.task_id == task.task_id for t in self._auto_print_queue[oid]):" not in src)

for w in (win, win3, win4):
    w._refresh_engine_availability = lambda *a, **kw: None
    try:
        w.close()
    except Exception:
        pass

print("\n=== 7. 2026-10-20 用户要求：无障碍打印遇「需人工翻纸」→ 直接开始，不挂起等人点 ===")
# 旧行为（2026-10-18）：本机不支持自动双面 + 含双面文件 → 挂起、亮 ⏸、等操作员点「开始打印」。
# 新行为：无障碍 = 顾客要求无人值守 ⇒ 直接 accept + 打第 1 趟（第 2 趟仍弹翻纸窗等确认）。
# 为什么现在安全：作业进行中每 60 秒发 print_keepalive 刷新 locked_at，
# 后端 3 分钟反馈窗口不会把这种长作业误判失败。
from printer_config import PrinterProfile  # noqa: E402

win6 = new_win()
win6._printer_profile = lambda: PrinterProfile(
    auto_duplex=False, capability_source="manual", manual_duplex=True)
# 用**真实**判据（别用 new_win 里那个恒 False 的桩）
win6._manual_flip_required_for = gui.MainWindow._manual_flip_required_for.__get__(win6)
_holds = []
_orig_hold = gui.MainWindow._hold_for_manual_duplex.__get__(win6)


def _spy_hold(jobs, tab_key, reason):
    _holds.append((tab_key, reason))
    return _orig_hold(jobs, tab_key, reason)


win6._hold_for_manual_duplex = _spy_hold
_starts = []


def _add_duplex(tasks, switch_tab=True):
    tab = win6._config.tabs.setdefault("1", gui.TabSettings())
    for t in tasks:
        tab.jobs.append(gui.PrintJob(file_path=os.path.join(_SANDBOX, t.file_name),
                                     task_id=t.task_id, order_id=t.order_id,
                                     page_count=5, duplex="on"))
    return "1"


win6._add_cloud_tasks_to_new_tab = _add_duplex


def _start(jobs, **kw):
    _starts.append([j.task_id for j in jobs])
    return True


win6._start_print_worker = _start
win6._enqueue_auto_print_task(FakeTask(5011, 8001))
win6._process_auto_print_order(8001)
check("★ 无障碍 + 手动双面机 + 双面文件 → **不挂起**（_hold_for_manual_duplex 0 次）",
      _holds == [], f"{_holds}")
check("★ 无障碍 + 手动双面机 + 双面文件 → 直接 accept 并开始打印第 1 趟",
      win6._cloud_client.accepted == [8001] and _starts == [[5011]],
      f"accepted={win6._cloud_client.accepted} starts={_starts}")
check("★ 标签页没有被标记「待人工翻纸」（不该亮 ⏸ 等人点）",
      getattr(win6._config.tabs["1"], "manual_duplex_pending", False) is False,
      f"{getattr(win6._config.tabs['1'], 'manual_duplex_pending', None)}")
check("★ 日志如实说明「直接开始第 1 趟、第 2 趟仍等翻纸确认」",
      any("直接开始第 1 趟" in m for m in win6._log_text.toPlainText().splitlines()),
      win6._log_text.toPlainText()[-160:])

print("\n" + ("全部通过" if ok_all else "存在失败项"))
shutil.rmtree(_SANDBOX, ignore_errors=True)
sys.exit(0 if ok_all else 1)
