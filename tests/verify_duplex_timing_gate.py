"""tests/verify_duplex_timing_gate.py — 「上一个任务还在出纸时，下一个手动双面任务**不许**开始计时」

现场（用户报告）：常驻窗显示「⏱ 第 1 趟已用 02:07 / 预计 00:42（超出 85 秒）」，而这一单只有
6 张纸（本来约 42 秒）—— 上一单第 2 趟的纸还在出，新单的第 1 趟计时已经起跑了。计时起点早了，
后面全跟着错：预警颜色假红、翻纸闸门按假用时算、实测样本把别人的出纸时间当成自己的斜率
（2026-10-18 踩过：斜率被带成负数 ⇒ 预估塌到 10 秒以内 ⇒ 闸门自己失效）。

本脚本钉住修法（每条都对应 brief 的验收项）：
  A1 上一作业未结束时进「等待上一个任务」态：文案明确、**不计时**（时间源拉大也不涨）、
     「▶ 继续打第 2 趟」保持禁用；
  A2 打印队列真的排空（drained=True）之后才切「① 正在打印第 1 趟」并从 0 起跑，
     起跑时刻晚于/等于真正开始送纸的时刻；
  A3 等待时长既不进第 1 趟用时、也不进 duplex_speed_sample；
  A6 前面本来没有任务（队列空）时一秒都不多等；
  兜底：等不到（队列卡住 / 打印机离线）走既有 wait_for_output 的 120 秒上限，按「队列没排空」
     口径继续，日志写明原因 —— 绝不把流程吊死。

验收：python tests/verify_duplex_timing_gate.py（退出码即判定）
"""
import io
import os
import sys
import tempfile
import time
from contextlib import contextmanager

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_duplex_gate_")
os.environ["APPDATA"] = _SANDBOX                  # 用户数据目录 → 沙箱（不碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui  # noqa: E402
import paths  # noqa: E402
import pdf_printer  # noqa: E402
from printer_config import PrintJob, PrinterProfile  # noqa: E402

# offscreen 下没人能回答模态框 → 一律自动回「好/是」
gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.Yes)
gui.QMessageBox.warning = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)
gui.QMessageBox.information = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)

from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    return bool(cond)


def make_pdf(name: str, pages: int) -> str:
    path = os.path.join(_SANDBOX, name)
    c = canvas.Canvas(path, pagesize=A4)
    for i in range(pages):
        c.drawString(72, 760, f"{name} page {i + 1}")
        c.showPage()
    c.save()
    return path


PDF6 = make_pdf("gate6.pdf", 6)      # 6 页双面 → 3 张纸（现场"6 张纸"同量级：这里 3 张纸两趟）

MANUAL = PrinterProfile(auto_duplex=False, capability_source="manual", manual_duplex=True,
                        first="odd", reverse_second=True, second_rotate_180=True,
                        verified_at="2026-10-12 22:14:49", verified_by="丙成员")

print("=" * 78)
print("⓪ 静态接线与常量（等待态必须由 worker 的**新信号**驱动，不能接回 announce）")
print("=" * 78)

check("QUEUE_GATE_TIMEOUT_SEC == 120（与既有 wait_for_output 上限同一条口径）",
      float(gui.QUEUE_GATE_TIMEOUT_SEC) == 120.0, f"{gui.QUEUE_GATE_TIMEOUT_SEC}")
check("PrintWorker 有 duplex_waiting 信号", hasattr(gui.PrintWorker, "duplex_waiting"))
check("MainWindow 有 _on_duplex_waiting 处理器",
      callable(getattr(gui.MainWindow, "_on_duplex_waiting", None)))

with io.open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8") as fh:
    SRC = fh.read()


def _seg(start: str, end: str) -> str:
    i = SRC.find(start)
    if i < 0:
        return ""
    j = SRC.find(end, i)
    return SRC[i:] if j < 0 else SRC[i:j]


_BATCH = _seg("def _print_manual_batch(", "def _print_stack_pass1(")
check("★ _print_manual_batch 里：探测队列 → 发 duplex_waiting → 等排空 → 才发 duplex_announce",
      -1 < _BATCH.find("count_pending_print_jobs(self._printer_name)")
      < _BATCH.find("self.duplex_waiting.emit(_announce_txt)")
      < _BATCH.find("self.duplex_announce.emit(_announce_txt)"),
      f"pending={_BATCH.find('count_pending_print_jobs(self._printer_name)')} "
      f"waiting={_BATCH.find('self.duplex_waiting.emit(_announce_txt)')} "
      f"announce={_BATCH.find('self.duplex_announce.emit(_announce_txt)')}")
check("★ 第 1 趟计时 _t_pass1 在等待闸门**之后**才取（等待天然不进用时/样本）",
      0 < _BATCH.find("_wait_previous_output(_pending)")
      < _BATCH.find("_t_pass1 = time.perf_counter()"),
      f"gate={_BATCH.find('_wait_previous_output(_pending)')} "
      f"t_pass1={_BATCH.find('_t_pass1 = time.perf_counter()')}")
_checkpoint = _BATCH.find("self.duplex_announce.emit(_announce_txt)")
check("duplex_announce 紧跟在等待之后（不是等待之前）",
      _checkpoint > 0 and _checkpoint < _BATCH.find("_t_pass1 = time.perf_counter()"))

_WAITING = _seg("def announce_waiting(", "def announce(self")
_ANNOUNCE = _seg("def announce(self", "def set_speed_hint(")
check("★ announce_waiting 里**没有** _t0.restart()（等待态绝不起跑计时）",
      "_t0.restart()" not in _WAITING and "_pass1_timer.start(" not in _WAITING)
check("★ announce 里才有 _t0.restart() + _pass1_timer.start(500)",
      "_t0.restart()" in _ANNOUNCE and "_pass1_timer.start(500)" in _ANNOUNCE)
check("等待态直接返回、绝不显示「已用」",
      'if self._mode == "waiting":' in _seg("def _pass1_tick(self)", "def freeze_pass1("))
check("攒批第 1 趟不发 duplex_waiting（_print_stack_pass1 不弹窗、不计时，口径不变）",
      "duplex_waiting.emit" not in _seg("def _print_stack_pass1(", "def _print_stack_second"))
check("MainWindow 接线：worker.duplex_waiting.connect(self._on_duplex_waiting)",
      "worker.duplex_waiting.connect(self._on_duplex_waiting)" in SRC)
check("全文只有一处 duplex_waiting.emit（不多发、不重发）",
      SRC.count("duplex_waiting.emit") == 1, f"{SRC.count('duplex_waiting.emit')}")

print()
print("=" * 78)
print("① 常驻窗「等待上一个任务」态：不计时、文案明确、按钮禁用")
print("=" * 78)

dlg = gui.ManualDuplexDialog(None)
dlg.announce_waiting("本单 3 张纸，第 1 趟：1 3 5", sheets=3, estimate_sec=42.0,
                     gate_ratio=0.5, gate_extra=0.0)

_status_txt = dlg._status.text()
check("mode == waiting", dlg._mode == "waiting", dlg._mode)
check("★ 状态文案明说在等上一个任务 + 原因（上一批还在出纸）",
      "请等待上一个任务完成" in _status_txt and "上一批的纸还在出" in _status_txt, _status_txt)
check("详情仍写着本批怎么打（等完就能看懂的上下文）",
      "3 张纸" in dlg._detail.text(), dlg._detail.text())
check("★ 等待态不显示「已用」", "已用" not in dlg._pass1.text(), repr(dlg._pass1.text()))
check("★ 等待态不显示「预计」", "预计" not in dlg._pass1.text(), repr(dlg._pass1.text()))
check("★ 等待态不给颜色（三色预警不该在没打的时候亮）", dlg._pass1.styleSheet() == "",
      repr(dlg._pass1.styleSheet()))
check("★ 等待态第 1 趟计时器没在跑", dlg._pass1_timer.isActive() is False)
check("★ 「▶ 继续打第 2 趟」保持禁用", dlg._btn_go.isEnabled() is False)
check("★ 按钮文案点明还在等上一个任务", "等上一个任务结束" in dlg._btn_go.text(),
      dlg._btn_go.text())
check("「⏭ 跳过本任务」仍可用（等不到也不能把操作员锁死）", dlg._btn_skip.isEnabled() is True)

# A1 核心：把时间源拉大（模拟"已经等了 2 分钟"），等待态文本必须**不增长**
for _v in (5.0, 130.0):
    dlg._elapsed_sec = (lambda v: (lambda: v))(_v)
    dlg._pass1_tick()
check("★ A1 时间源=130 秒，等待态文本仍为空（不计时、不变色）",
      dlg._pass1.text() == "" and dlg._pass1.styleSheet() == "",
      f"{dlg._pass1.text()!r} / {dlg._pass1.styleSheet()!r}")
check("★ A1 时间源拉大后按钮仍是禁用（等待态不会被误切到可翻纸）",
      dlg._btn_go.isEnabled() is False)
del dlg._elapsed_sec                     # 还原真时钟

print()
print("=" * 78)
print("② 队列排空 → announce：切「① 正在打印第 1 趟」，计时从 0 起跑")
print("=" * 78)

dlg.announce("本单 3 张纸，第 1 趟：1 3 5", sheets=3, estimate_sec=42.0,
             gate_ratio=0.5, gate_extra=0.0)
_t = dlg._pass1.text()
check("mode == pass1", dlg._mode == "pass1", dlg._mode)
check("状态文案切回「① 正在打印第 1 趟」", "正在打印第 1 趟" in dlg._status.text(),
      dlg._status.text())
check("★ A2 起跑后已用从 0 开始（已用 00:00）", "已用 00:00" in _t, _t)
check("★ A2 起跑后给出预计（预计 00:42）", "预计 00:42" in _t, _t)
check("★ A2 起跑后第 1 趟计时器在跑", dlg._pass1_timer.isActive() is True)
check("★ A2 起跑后已用秒数是真时钟（< 1 秒），不是把等待时长算进来",
      dlg._elapsed_sec() < 1.0, f"{dlg._elapsed_sec():.2f}s")
check("★ A2 起跑后按钮文案回到「等第 1 趟打完」", "等第 1 趟打完" in dlg._btn_go.text(),
      dlg._btn_go.text())
dlg.finish("done")
dlg.hide()


# ============================================================
# worker 侧：桩掉真打印 + 真出纸等待，只观测「什么时候起跑」
# ============================================================
class Rec:
    def __init__(self):
        self.events = []        # ["waiting" / "announce"] 的顺序
        self.plan = []          # [(张数, 预估秒)]
        self.plan_pass = []     # [1, 2]
        self.samples = []       # [(张数, 实测秒)]
        self.logs = []
        self.waited = []        # wait_for_output 的 pages 参数（0 = 等待闸门）
        self.flip = []
        self.elapsed = 0.0


def make_job(path=PDF6, pages=6):
    return PrintJob(file_path=path, copies=1, duplex="on", duplex_mode="long-edge",
                    page_count=pages, display_name=os.path.basename(path))


GATE_DEFAULT = {"drained": True, "queue_seconds": 0.0, "margin_seconds": 0.0,
                "peak_jobs": 0, "detail": ""}


@contextmanager
def stubbed(rec, pending=0, gate_wait=None, pass1_wait=None, gate_sleep=0.0):
    """桩：不发真纸，但**照旧**走 worker 的真实时序（队列探测 → 等待 → 起跑 → 两趟）。

    gate_wait / pass1_wait：pages=0（等待上一个任务）与 pages=n1（本批第 1 趟出纸）分开给，
    这样"等待多久"与"本批自己打多久"在验收里互不污染。
    """
    orig = {n: getattr(gui, n) for n in
            ("print_pdf", "print_plan_pass", "wait_for_output", "get_converter",
             "count_pending_print_jobs")}
    orig_merge = pdf_printer.merge_pdfs

    def _print_plan_pass(plan, path, printer, which, **kw):
        rec.plan_pass.append(which)
        return True, f"桩：第 {which} 趟"

    def _print_pdf(**kw):
        return True, "桩：单次提交"

    def _wait(printer, pages=0, **kw):
        rec.waited.append(pages)
        if pages <= 0:
            if gate_sleep:
                time.sleep(gate_sleep)      # 模拟"上一个任务还在出纸、真等了这么久"
            return dict(gate_wait or GATE_DEFAULT)
        return dict(pass1_wait or GATE_DEFAULT)

    gui.print_pdf = _print_pdf
    gui.print_plan_pass = _print_plan_pass
    gui.wait_for_output = _wait
    gui.get_converter = lambda: object()
    gui.count_pending_print_jobs = lambda printer="": pending
    pdf_printer.merge_pdfs = orig_merge
    try:
        yield
    finally:
        for k, v in orig.items():
            setattr(gui, k, v)
        pdf_printer.merge_pdfs = orig_merge


def make_worker(jobs=None):
    return gui.PrintWorker(jobs=jobs if jobs is not None else [make_job()],
                           printer_name="Fake Print", duplex_mode="long-edge",
                           keep_temp_pdf=False, render_dpi=200,
                           manual_duplex_profile=MANUAL, cover_page=False)


def wire(w, rec, extra=None):
    w.log_message.connect(rec.logs.append)
    w.duplex_plan.connect(lambda s, e: rec.plan.append((s, e)))
    w.duplex_speed_sample.connect(lambda n, sec: rec.samples.append((n, sec)))
    w.duplex_waiting.connect(lambda t: rec.events.append("waiting"))
    w.duplex_announce.connect(lambda t: rec.events.append("announce"))
    w.flip_required.connect(lambda t, sec: (rec.flip.append(t), w.confirm_flip(True)))
    if extra is not None:
        extra(w)


def run_worker(rec, extra=None, **kw):
    w = make_worker()
    wire(w, rec, extra=extra)
    with stubbed(rec, **kw):
        t0 = time.time()
        w.run()
        rec.elapsed = time.time() - t0


print()
print("=" * 78)
print("③ worker：上一个任务还在出纸 → 先「等待」态，排空后才 announce（A1/A2 真链路）")
print("=" * 78)

win = gui.MainWindow(config_path=paths.config_path())
win._config.printer_name = "Fake Print"
snaps = {"waiting": None, "announce": None}


def _snap_waiting(_text):
    _d = win._duplex_dialog
    snaps["waiting"] = {"mode": _d._mode, "pass1": _d._pass1.text(),
                        "timer": _d._pass1_timer.isActive(),
                        "go": _d._btn_go.isEnabled(), "status": _d._status.text(),
                        "elapsed": _d._elapsed_sec()}


def _snap_announce(_text):
    _d = win._duplex_dialog
    snaps["announce"] = {"mode": _d._mode, "pass1": _d._pass1.text(),
                         "timer": _d._pass1_timer.isActive(),
                         "elapsed": _d._elapsed_sec(),
                         "status": _d._status.text()}


def _wire_win(w):
    w.duplex_plan.connect(win._on_duplex_plan)
    w.duplex_waiting.connect(win._on_duplex_waiting)     # 生产接线同款
    w.duplex_announce.connect(win._on_duplex_announce)
    w.duplex_waiting.connect(_snap_waiting)              # 后连 ⇒ 在界面处理之后取快照
    w.duplex_announce.connect(_snap_announce)


rec = Rec()
run_worker(rec, extra=_wire_win, pending=2, gate_sleep=0.8)
check("★ 队列非空 ⇒ 先发 duplex_waiting、后发 duplex_announce（顺序不能反）",
      rec.events == ["waiting", "announce"], f"{rec.events}")
# 快照缺失（= 根本没进等待态，旧行为）时用空 dict 兜底：让每条断言各自**判红**，
# 而不是脚本第一行就 TypeError 崩掉（崩掉就只剩一条证据，排查要重跑）。
_W = snaps["waiting"] or {}
_A = snaps["announce"] or {}
check("★ A1 等待态下常驻窗 mode == waiting", _W.get("mode") == "waiting", f"{_W}")
check("★ A1 等待态下「已用」文本为空（不计时）", _W.get("pass1") == "",
      repr(_W.get("pass1")))
check("★ A1 等待态下第 1 趟计时器没在跑", _W.get("timer") is False)
check("★ A1 等待态下「▶ 继续打第 2 趟」禁用", _W.get("go") is False)
check("★ A1 等待态文案对操作员可见（点明在等上一个任务）",
      "请等待上一个任务完成" in (_W.get("status") or ""), _W.get("status"))
check("★ A2 排空后才切 pass1 且计时器起跑",
      _A.get("mode") == "pass1" and _A.get("timer") is True, f"{_A}")
check("★ A2 起跑后已用从 0 开始（「已用 00:00」）",
      "已用 00:00" in (_A.get("pass1") or ""), repr(_A.get("pass1")))
check("★ A2 起跑时刻的界面时钟 ≈0（等待那 0.8 秒没被算进去）",
      bool(_A) and float(_A.get("elapsed") or 0.0) < 0.3, f"{_A.get('elapsed')}")
check("★ 等待期日志明说在等谁 + 这段不算用时",
      any("打印队列里还有 2 个作业" in m and "不计入第 1 趟用时" in m for m in rec.logs),
      next((m for m in rec.logs if "打印队列里还有" in m), ""))
check("★ 排空后日志写明等了多久 + 计时从 0 起跑",
      any("上一个任务已结束" in m and "计时从 0 起跑" in m for m in rec.logs),
      next((m for m in rec.logs if "上一个任务已结束" in m), ""))
check("两趟照常打完（等待没有影响本批流程）", rec.plan_pass == [1, 2], f"{rec.plan_pass}")

print()
print("=" * 78)
print("④ A3 等待时长既不进第 1 趟用时、也不进 duplex_speed_sample")
print("=" * 78)

rec4 = Rec()
run_worker(rec4, pending=3, gate_sleep=1.2)
check("★ A3 真的等过了（wall ≥ 1.2 秒，不是桩没生效）", rec4.elapsed >= 1.2,
      f"{rec4.elapsed:.2f}s")
check("★ A3 速度样本只有 1 条（第 1 趟那一条）", len(rec4.samples) == 1, f"{rec4.samples}")
_sample_sec = rec4.samples[0][1] if rec4.samples else -1.0
check("★ A3 速度样本秒数**不含等待**（< 0.5 秒，而 wall 有 1.2 秒以上）",
      0 <= _sample_sec < 0.5, f"sample={_sample_sec:.2f}s wall={rec4.elapsed:.2f}s")
check("★ A3 样本张数 = 第 1 趟槽位数 3（不是被等待污染的别的数）",
      rec4.samples and rec4.samples[0][0] == 3, f"{rec4.samples}")
_gate_calls = [p for p in rec4.waited if p <= 0]
_pass1_calls = [p for p in rec4.waited if p > 0]
check("★ A3 等待闸门单独调 wait_for_output(pages=0)，没混进本批张数",
      _gate_calls == [0] and _pass1_calls == [3], f"waited={rec4.waited}")

print()
print("=" * 78)
print("⑤ A6 前面本来没有任务（队列空）⇒ 一秒都不多等")
print("=" * 78)

rec5 = Rec()
run_worker(rec5, pending=0)
check("★ A6 队列空 ⇒ 根本不发 duplex_waiting", rec5.events == ["announce"], f"{rec5.events}")
check("★ A6 队列空 ⇒ 不调等待闸门（wait_for_output 只被本批出纸调一次，pages=3）",
      rec5.waited == [3], f"{rec5.waited}")
check("★ A6 队列空 ⇒ 整个第 1 趟流程 < 1 秒（没有可感知延迟）",
      rec5.elapsed < 1.0, f"{rec5.elapsed:.2f}s")
check("队列空时日志不提「上一个任务」",
      not any("上一个任务" in m for m in rec5.logs),
      next((m for m in rec5.logs if "上一个任务" in m), ""))

# 再钉一层：_wait_previous_output(0) 连 wait_for_output 都不许碰（一秒不多等的实现证据）
_boom = {"n": 0}


def _no_wait(*a, **k):
    _boom["n"] += 1
    raise AssertionError("pending<=0 时不该调 wait_for_output")


_orig_wait_for_output = gui.wait_for_output
gui.wait_for_output = _no_wait
try:
    _w = make_worker(jobs=[])
    _out = _w._wait_previous_output(0)
finally:
    gui.wait_for_output = _orig_wait_for_output
check("★ A6 _wait_previous_output(0) 立刻返回 {'waited':0,'drained':True}，一次也没等",
      _out.get("waited") == 0.0 and _out.get("drained") is True and _boom["n"] == 0,
      f"{_out} calls={_boom['n']}")

print()
print("=" * 78)
print("⑥ 兜底：等不到（队列卡住/打印机离线）不吊死，走既有「队列没排空」口径")
print("=" * 78)

rec6 = Rec()
run_worker(rec6, pending=5, gate_wait={"drained": False, "queue_seconds": 120.0,
                                       "margin_seconds": 2.0, "peak_jobs": 5,
                                       "detail": "桩：队列一直没排空"})
check("★ 超时后流程照常走完两趟（不吊死、不判失败）", rec6.plan_pass == [1, 2],
      f"{rec6.plan_pass}")
check("★ 超时日志写明「等上一个任务超时…队列没排空…按既有口径继续」",
      any("等待上一个任务超时" in m and "队列没排空" in m and "按既有" in m
          for m in rec6.logs),
      next((m for m in rec6.logs if "等待上一个任务超时" in m), ""))
check("★ 超时时也说清等待不计入用时",
      any("等待上一个任务超时" in m and "不计入第 1 趟用时" in m for m in rec6.logs))
check("超时后仍会发 announce（常驻窗从等待态切到 pass1，不会停在等待）",
      rec6.events == ["waiting", "announce"], f"{rec6.events}")
check("超时不产生速度样本污染（第 1 趟本身 drained=True ⇒ 只有正常那一条）",
      len(rec6.samples) == 1, f"{rec6.samples}")
check("等待上限用的就是 QUEUE_GATE_TIMEOUT_SEC（120 秒），不是自己另编的数",
      gui.QUEUE_GATE_TIMEOUT_SEC == 120.0)

print()
print("=" * 78)
print(f"结果：{'全部通过' if ok_all else '有失败项（见上）'}")
print("=" * 78)
sys.exit(0 if ok_all else 1)
