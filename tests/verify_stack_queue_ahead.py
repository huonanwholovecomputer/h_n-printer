# -*- coding: utf-8 -*-
"""验收脚本 · 攒批模式「提前入队」（不等上一单真打完就送下一单）

2026-11-02 用户反馈（第 2 条）：

  「上个打印任务未完成时，发起一个双面打印任务，直接弹出了双面打印的控制弹窗，说明这个
   双面打印的任务完成了渲染并发送到了打印机的打印队列。这是好事儿……但是我记得现在的攒批
   模式下，当我同时开始打印多个订单的时候，每个打印订单都会等待至上个订单实际返回结束状态
   才开始打印，这导致打印效率较低，需要允许提前将任务发送到打印机队列。」

根因：`_print_stack_pass1()` 在 `print_plan_pass()` 之后**无条件** `wait_for_output(pages=n1)`，
一直阻塞到纸真的出完；worker 不结束 ⇒ 主线程 `_on_all_finished` 不跑 ⇒ 队列里排着的下一单
开不了。打印机在"等出纸信号"这一段完全空转。

本脚本断言的是"作业什么时候交给 spooler"，不是"代码里出现了哪个信号名"：

  ① 后面还排着别的单 ⇒ 第 1 趟打完**立刻**收工（entry_done 已发），出纸等待发生在一个
     守护线程里，且出纸完成后 `stack_entry_drained` 会回报（张数对得上、样本只记一次）；
  ② 后面没排单了（最后一单）⇒ 保持老行为：**先等出纸**再发 entry_done，次序不能反
     （老验收 `verify_duplex_stack_gui.py` 逐字断言那条文案，行为不能变）；
  ③ 出纸槽闸门（2026-11-02 事故那道）在「队列里剩的都是本叠自己的第 1 趟」时放行，
     混进外部作业时照旧拦截、在飞记录过期后不再豁免；
  ④ 静态接线：回调传下去、信号连上、entry_done 文案未改（老验收逐字断言）。

全程离屏、不出纸、不碰真实 %APPDATA%。

    python tests/verify_stack_queue_ahead.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_stack_ahead_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import converter as _converter  # noqa: E402

_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}

import gui  # noqa: E402
from printer_config import PrintJob, PrinterProfile  # noqa: E402

gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.Yes)
gui.QMessageBox.warning = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)
gui.QMessageBox.information = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


from reportlab.pdfgen import canvas  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402


def make_pdf(name, pages):
    path = os.path.join(_SANDBOX, name)
    c = canvas.Canvas(path, pagesize=A4)
    for i in range(pages):
        c.drawString(72, 760, f"{name} page {i + 1}")
        c.showPage()
    c.save()
    return path


PDF4 = make_pdf("ahead4.pdf", 4)
PDF6 = make_pdf("ahead6.pdf", 6)

MANUAL = PrinterProfile(auto_duplex=False, capability_source="manual", manual_duplex=True,
                        first="odd", reverse_second=True, second_rotate_180=True,
                        stack_capacity=90)

MSG_TEMPLATE = "第 1 趟完成（{n} 张纸已堆入出纸槽）"


# ============================================================
# 桩：打印调用记录 + 无副作用（wait_for_output 可阻塞）
# ============================================================
class Rec:
    def __init__(self):
        self.plan_pass = []
        self.entry_done = []
        self.drained = []
        self.samples = []
        self.waited = []          # [(pages, thread_name)]
        self.order = []           # 事件时序：("wait" / "wait_done" / "done")


def patched(rec, gate: threading.Event | None = None, drained: bool = True,
            drain_delay: float = 0.0):
    orig = {n: getattr(gui, n) for n in
            ("print_pdf", "print_plan_pass", "wait_for_output", "get_converter")}

    def _print_pdf(**kw):
        return True, "桩：一次提交"

    def _print_plan_pass(plan, path, printer, which, second_rotate_180=False,
                         progress_callback=None, dpi=0, landscape_fill=True, **kw):
        rec.plan_pass.append((which, plan.pass_page_indices(which)))
        return True, f"桩：第 {which} 趟"

    def _wait_for_output(printer, pages=0, **kw):
        rec.waited.append((pages, threading.current_thread().name))
        if gate is not None:
            # 模拟"纸还没出完"：等到测试放行
            gate.wait(10.0)
        if drain_delay:
            time.sleep(drain_delay)
        rec.order.append(("wait_done", pages))
        # margin_seconds 必须为 0：worker 会从实测耗时里扣掉这段"余量"，桩里 2s 余量会把
        # 本来 1s 级的耗时扣成负数 → 速度样本被丢，测不到"只记一次"。
        return {"drained": bool(drained), "queue_seconds": 0.0 if drained else 120.0,
                "margin_seconds": 0.0, "peak_jobs": 0, "detail": ""}

    gui.print_pdf = _print_pdf
    gui.print_plan_pass = _print_plan_pass
    gui.wait_for_output = _wait_for_output
    gui.get_converter = lambda: object()

    def restore():
        for k, v in orig.items():
            setattr(gui, k, v)
    return restore


def make_job(path, duplex="on", name=""):
    return PrintJob(file_path=path, copies=1, duplex=duplex, duplex_mode="long-edge",
                    page_range="", page_count=0,
                    display_name=name or os.path.basename(path))


def run_pass1(jobs, out_pdf, more_pending, seq=1, gate=None, drained=True):
    """跑一次攒批第 1 趟。more_pending 为真 = 后面还排着别的单。"""
    rec = Rec()
    w = gui.PrintWorker(jobs=jobs, printer_name="Fake Print", duplex_mode="long-edge",
                        keep_temp_pdf=False, render_dpi=200,
                        manual_duplex_profile=MANUAL,
                        stack_mode=True, stack_out_pdf=out_pdf, stack_seq=seq,
                        stack_rotate=True,
                        stack_more_pending=(lambda: True) if more_pending else None)
    w.log_message.connect(lambda _m: None)
    w.stack_entry_done.connect(
        lambda s, ok, msg, n: (rec.entry_done.append((s, ok, msg, n)), rec.order.append(("done", n))))
    w.stack_entry_drained.connect(lambda s, n, t: rec.drained.append((s, n, t)))
    w.duplex_speed_sample.connect(lambda n, t: rec.samples.append((n, t)))
    restore = patched(rec, gate=gate, drained=drained)
    t0 = time.perf_counter()
    try:
        w.run()
    finally:
        elapsed = time.perf_counter() - t0
        restore()
    return rec, w, elapsed


# ============================================================
# ① 后面还排着单 ⇒ 立刻交作业，出纸另开线程等
# ============================================================
print("\n=== ① 提前入队：不等出纸就把作业交出去 ===")
out1 = os.path.join(_SANDBOX, "entry_0001.pdf")
gate = threading.Event()          # 出纸闸门：测试自己放行
rec1, w1, elapsed1 = run_pass1([make_job(PDF4, "on", "a.pdf")], out1, more_pending=True,
                               seq=1, gate=gate)
check("★ 第 1 趟打完就收工（worker 没被出纸等待卡住）", elapsed1 < 3.0, f"{elapsed1:.2f}s")
check("★ entry_done 已经发出（文案与老验收逐字一致）",
      rec1.entry_done and rec1.entry_done[0][1] is True
      and rec1.entry_done[0][2] == MSG_TEMPLATE.format(n=2),
      f"{rec1.entry_done}")
check("★ 出纸等待发生在**守护线程**里，不是 worker 线程",
      len(rec1.waited) == 1 and rec1.waited[0][1].startswith("stack-drain-"),
      f"{rec1.waited}")
check("★ 出纸完成前没有 drained 回报（说明是先交作业、后等出纸）",
      rec1.drained == [], f"{rec1.drained}")
gate.set()                         # 放行"出纸"
_deadline = time.time() + 10.0
while time.time() < _deadline and not rec1.drained:
    _app.processEvents()
    time.sleep(0.05)
check("★ 出纸完成后回报 drained（张数对得上）",
      len(rec1.drained) == 1 and rec1.drained[0][0] == 1 and rec1.drained[0][1] == 2,
      f"{rec1.drained}")
check("★ 实测出纸速度只记一次样本（不重复计数）",
      len(rec1.samples) == 1 and rec1.samples[0][0] == 2, f"{rec1.samples}")

# ============================================================
# ② 最后一单（后面没人排）⇒ 保持老行为：先等出纸、再报完成
# ============================================================
print("\n=== ② 最后一单保持老行为（先等出纸）===")
out2 = os.path.join(_SANDBOX, "entry_0002.pdf")
rec2, w2, elapsed2 = run_pass1([make_job(PDF6, "on", "b.pdf")], out2, more_pending=False,
                               seq=2, gate=None, drained=True)
check("★ 没有排队单时按老样子阻塞等出纸", len(rec2.waited) == 1)
check("★ 时序：先 wait_done、再 done（老验收依赖这个次序）",
      [e[0] for e in rec2.order] == ["wait_done", "done"], f"{rec2.order}")
check("★ entry_done 文案仍是逐字那条（张数 3 张纸）",
      rec2.entry_done and rec2.entry_done[0][2] == MSG_TEMPLATE.format(n=3),
      f"{rec2.entry_done}")
check("★ 老路径也回报 drained（出纸已由 worker 自己等完）",
      rec2.drained and rec2.drained[0][1] == 3, f"{rec2.drained}")
check("★ 老路径样本只记一次", len(rec2.samples) == 1, f"{rec2.samples}")

# ============================================================
# ③ 出纸槽闸门：本叠自己的作业不算"别人家的纸"
# ============================================================
print("\n=== ③ 出纸槽闸门豁免 ===")
_win = gui.MainWindow.__new__(gui.MainWindow)
_win._config = type("C", (), {"printer_name": "Fake Print"})()
_win._stack_inflight = {}
_win._last_tray_risk_ts = 0.0

_orig_count = gui.count_pending_print_jobs
gui.count_pending_print_jobs = lambda _p: 0
try:
    check("★ 队列空 + 无在飞 ⇒ 可以开打", _win._stack_tray_busy_reason() == "",
          _win._stack_tray_busy_reason())
    _win._stack_inflight = {1: time.time()}
    gui.count_pending_print_jobs = lambda _p: 1
    check("★ 队列剩的正是本叠自己的第 1 趟 ⇒ 放行（下一单提前入队的前提）",
          _win._stack_tray_busy_reason() == "", _win._stack_tray_busy_reason())
    gui.count_pending_print_jobs = lambda _p: 2
    check("★ 混进了外部作业（队列 2 > 在飞 1）⇒ 照旧拦截",
          "打印队列里还有" in _win._stack_tray_busy_reason(),
          _win._stack_tray_busy_reason())
    _win._stack_inflight = {}
    check("★ 没有在飞记录 ⇒ 照旧拦截",
          "打印队列里还有" in _win._stack_tray_busy_reason(),
          _win._stack_tray_busy_reason())
    _win._stack_inflight = {1: time.time() - 1000.0}
    check("★ 在飞记录过期（>900s）不再豁免（防止卡纸后无限放行）",
          "打印队列里还有" in _win._stack_tray_busy_reason(),
          _win._stack_tray_busy_reason())
finally:
    gui.count_pending_print_jobs = _orig_count

# ============================================================
# ④ 静态接线
# ============================================================
print("\n=== ④ 静态接线 ===")
_gui_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
check("★ worker 构造时把「后面还排着单吗」回调传下去",
      "stack_more_pending=lambda: bool(getattr(self, \"_stack_entry_wait\", None))" in _gui_src)
check("★ 主线程连上 stack_entry_drained",
      "worker.stack_entry_drained.connect(self._on_stack_entry_drained)" in _gui_src)
check("★ 有出纸回报槽函数（移出在飞清单 + 接着排下一单）",
      "def _on_stack_entry_drained(" in _gui_src
      and "self._stack_inflight.pop(int(seq), None)" in _gui_src
      and "self._drain_stack_entry_queue()" in _gui_src)
_i_more = _gui_src.index("if self._stack_more_ahead():")
_i_wait = _gui_src.index("w = wait_for_output(self._printer_name, pages=n1)", _i_more)
check("★ 提前入队分支排在阻塞等待之前（否则永远走不到）", _i_more < _i_wait,
      f"more@{_i_more} wait@{_i_wait}")
check("★ 老文案没有被改写（verify_duplex_stack_gui 逐字断言）",
      'f"第 1 趟完成（{plan.sheet_count} 张纸已堆入出纸槽）", n1)' in _gui_src)
check("★ 出纸等待在守护线程里（daemon=True，退出程序不挂住）",
      'name=f"stack-drain-{seq}"' in _gui_src and "daemon=True" in _gui_src)

print()
if ok_all:
    print("✅ 全部通过")
    sys.exit(0)
print("❌ 有失败项")
sys.exit(1)
