# -*- coding: utf-8 -*-
"""批次 B1 验收：攒批「出纸槽独占」的两个旁路（🔴-3）

事故形状
  攒批（多任务合并翻纸）期间，出纸槽/进纸盒被**整叠**独占。铁律①：任何别的输出插进去
  都会让第 2 趟的全局逆序整体错位串页；铁律②：只打了第 1 趟的纸是半成品，绝不置
  job.sent / 绝不上报 print_success。原实现有三处旁路：

  ① `_stack_gate` 把"含手动双面文件 → 作为新单入叠"的判断放在"纸叠是否被独占"**之前**
     ⇒ 整叠第 2 趟已经在打（纸已搬回进纸盒）时，新单照样被收进 `_stack.entries`；
  ② `_start_print_worker` 的忙判断只看 `self._worker`，**不看** `_stack_flush_worker`
     （第 2 趟用的正是后者）⇒ 新单会与整叠第 2 趟并发塞进同一台打印机；
  ③ `_on_calibrate_speed`（帮助 → 打印速度校准）全函数**零** `_stack_*` 引用，测速要打
     11 张纸，插进纸叠同样串页。

  ①②叠加的后果：`_on_stack_flush_done` 用**当下**的 `_stack.entries` 收尾 ⇒ 快照里含
  这个只打了第 1 趟的新单 ⇒ `_finish_stack_entries(ok=True)` 把它当成品
  `job.sent = True` + `report_success` —— 白收钱 + 账目错。

验收方式：APPDATA 沙箱里构造真实 MainWindow，直接驱动闸门与三个旁路入口。
"""
import os
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

_SANDBOX = tempfile.mkdtemp(prefix="hn_stack_gate_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

_app = QApplication.instance() or QApplication([])

# offscreen 下任何弹窗都会永久阻塞 → 一律桩掉
QMessageBox.information = staticmethod(lambda *a, **kw: QMessageBox.Ok)
QMessageBox.warning = staticmethod(lambda *a, **kw: QMessageBox.Ok)
QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.Yes)

import duplex_stack as ds  # noqa: E402
import gui                 # noqa: E402
import paths               # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


class _Prof:
    """"不支持自动双面 + 需人工翻纸"的打印机档案。"""
    auto_duplex = False
    manual_duplex = True
    second_rotate_180 = True
    stack_capacity = 90
    stack_warn_after_sec = 1800
    first = ""


class _Sig:
    def connect(self, *a, **kw):
        pass


class _StubWorker:
    """flush worker 桩：只要求 isRunning()。"""
    def __init__(self, running=True):
        self._running = running

    def isRunning(self):
        return self._running


class _StubSpeedWorker:
    """SpeedCalibrationWorker 桩：真实现会真的打 11 张纸。"""
    def __init__(self, name):
        self.log = _Sig()
        self.done = _Sig()

    def start(self):
        pass

    def isRunning(self):
        return False


gui.SpeedCalibrationWorker = _StubSpeedWorker


def new_win(stack_mode=True):
    w = gui.MainWindow(config_path=paths.config_path())
    w._cloud_client = None
    w._printer_profile = lambda: _Prof()
    w._config.duplex_stack_mode = stack_mode
    w._stack = ds.DuplexStack(capacity=90, printer_name="Fake Print")
    w._stack.rotate = True
    w._stack.first = ""
    return w


def entry(seq, sheets):
    return ds.StackEntry(seq=seq, title=f"单{seq}", page_count=sheets * 2,
                         sheet_count=sheets, rotate=True,
                         pdf=f"entry_{seq:04d}.pdf",
                         file_plans=[{"label": str(seq), "pages": [1, 2], "duplex": True}])


def make_job(order_id, pages=4):
    return gui.PrintJob(file_path=os.path.join(_SANDBOX, f"{order_id}.pdf"),
                        duplex="on", duplex_mode="long-edge", page_count=pages,
                        task_id=order_id, order_id=order_id)


# ══════════════════════════════════════════════════════════════════════════
print("=== 1. 🔴-3① 整叠第 2 趟已开始 ⇒ 带手动双面文件的新单也必须排队（不得入叠）===")
win = new_win()
win._stack.entries = [entry(1, 3)]          # 整叠已有第 1 趟的纸
win._stack_flush_worker = _StubWorker(True)  # 第 2 趟正在打
win._stack_wait = []
job = make_job(101)

gate = win._stack_gate([job], "1")
check("闸门返回 parked（改前：走 entry 分支被收进纸叠）", gate == "parked", f"实为 {gate!r}")
check("请求进了排队队列", len(win._stack_wait) == 1, f"{len(win._stack_wait)}")
check("排队原因点明「第 2 趟」", "第 2 趟" in (win._stack_wait[0]["reason"] if win._stack_wait else ""),
      win._stack_wait[0]["reason"] if win._stack_wait else "")

print("\n=== 2. 回归保护：第 1 趟累积中仍允许继续入叠（多单攒批不能被修坏）===")
win2 = new_win()
win2._stack.entries = [entry(1, 3)]          # 第 1 趟的纸堆在出纸槽里（正常中间态）
win2._stack_flush_worker = None              # 第 2 趟未开始
win2._stack_wait = []
gate2 = win2._stack_gate([make_job(202)], "1")
check("闸门返回 entry（攒批仍可连续收多单）", gate2 == "entry", f"实为 {gate2!r}")
check("没有误排队", len(win2._stack_wait) == 0, f"{len(win2._stack_wait)}")

print("\n=== 3. 🔴-3② 第 2 趟在飞 ⇒ _start_print_worker 必须拒绝启动 ===")
win3 = new_win()
win3._stack_flush_worker = _StubWorker(True)
win3._worker = None
win3._stack_gate = lambda jobs, tab_key: "normal"   # 隔离闸门，只验忙判断本身
win3._has_members = lambda: True                    # 绕过成员校验（否则会弹窗）
reached = []


def _mark_then_stop(*a, **kw):
    reached.append(1)
    raise RuntimeError("STOP-AFTER-BUSY-CHECK")


win3._sync_ui_to_config = _mark_then_stop
try:
    started = win3._start_print_worker([make_job(303)], tab_key="1")
except RuntimeError:
    started = None
check("返回 False（被拒）", started is False, f"实为 {started!r}")
check("没有越过忙判断（改前：会一路走到 _sync_ui_to_config 并真的启动打印）",
      not reached, f"reached={len(reached)}")

print("\n=== 4. 🔴-3③ 纸叠未完成 ⇒ 打印速度校准必须被拒（测速要打 11 张纸）===")
win4 = new_win()
win4._stack.entries = [entry(1, 3)]
win4._stack_flush_worker = None
win4._speed_cal_worker = None
win4._log_lines = []
_orig_log = win4._log
win4._log = lambda m: (win4._log_lines.append(m), _orig_log(m))[1]
win4._on_calibrate_speed()
check("没有创建测速 worker（改前：会真的启动 11 张纸的测速）",
      win4._speed_cal_worker is None, f"{win4._speed_cal_worker}")
check("日志明确说明被拒原因",
      any("测速被拒" in m for m in win4._log_lines),
      "; ".join(win4._log_lines[-2:]))

print("\n=== 5. 🔴-3 第二道防线：第 2 趟收尾只用开始时的快照（混入的新单不得当成品）===")
win5 = new_win()
bdir5 = ds.batch_dir(paths.duplex_stack_dir(), "b_gate_5")
os.makedirs(bdir5, exist_ok=True)
win5._stack.new_batch("b_gate_5")
e1, e2 = entry(1, 3), entry(2, 2)
win5._stack_flush_snapshot = [e1]      # 第 2 趟开始那一刻冻结的整叠
win5._stack.entries = [e1, e2]         # 第 2 趟期间混进来的新单（只打了第 1 趟）
finished = []
win5._finish_stack_entries = lambda entries, ok, message: finished.append(list(entries))
win5._on_stack_flush_done(True, "整叠第 2 趟完成", 5)
seqs = [e.seq for e in finished[0]] if finished else []
check("收尾只用第 2 趟开始时的快照 [1]（改前：[1,2] → 把半成品当成品回报）",
      seqs == [1], f"实为 {seqs}")
check("快照消费后已清空（不残留到下一叠）", win5._stack_flush_snapshot == [],
      f"{win5._stack_flush_snapshot}")

print("\n=== 6. 空叠 / 未开攒批时不误拦（正常路径不受影响）===")
win6 = new_win(stack_mode=False)
win6._stack.entries = []
win6._stack_flush_worker = None
win6._stack_wait = []
check("空叠 + 未开攒批 → normal", win6._stack_gate([make_job(606)], "1") == "normal")
check("空叠 + 未开攒批 → 不排队", len(win6._stack_wait) == 0)
win7 = new_win(stack_mode=False)
win7._stack.entries = [entry(1, 3)]        # 有纸叠但攒批模式已关（纸还得处理完）
win7._stack_wait = []
check("纸叠非空 + 关闭攒批 → 单面请求仍排 parked",
      win7._stack_gate([gui.PrintJob(file_path="x.pdf", duplex="off", page_count=2)], "1")
      == "parked")

for w in (win, win2, win3, win4, win5, win6, win7):
    w._refresh_engine_availability = lambda *a, **kw: None
    try:
        w.close()
    except Exception:
        pass

print("\n" + ("全部通过" if ok_all else "存在失败项"))
shutil.rmtree(_SANDBOX, ignore_errors=True)
sys.exit(0 if ok_all else 1)
