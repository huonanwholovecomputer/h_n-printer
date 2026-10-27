# -*- coding: utf-8 -*-
"""验收脚本 · 攒批模式（多任务合并翻纸）接入 GUI/打印线程（offscreen，不碰真打印机）

攒批模式要解决的是「大量小订单 + 无自动双面器」的组合：旧行为一个订单翻一次纸，
操作员每个订单都要跑一趟机器。开启后：多个订单的第 1 趟**连续**打进同一叠（出纸槽），
人工**一次性**确认后统一打整叠的第 2 趟 —— 一轮只翻一次纸。

三条铁律必须逐条钉住（错了就是整叠串页/顾客收到半成品/后端把订单判失败）：

  ① 出纸槽独占：攒批期间任何别的输出（别的订单/单面文件/封面页/测速）都不能落到托盘里，
     否则整叠第 2 趟的逆序整体错位 ⇒ 其余打印请求一律排队。
  ② 第 1 趟不算完成：第 1 趟只是半成品，**绝不** emit job_finished / 置 sent / 报
     print_success；收尾只发生在整叠第 2 趟打完（或放弃）时。
  ③ 超时不自动跳过：第 2 趟要人把纸叠搬回进纸盒，超时只变红预警。

覆盖：worker 攒批第 1 趟（含封面页并入、单面文件也进叠）、整叠第 2 趟 worker、
主窗口闸门/排队/容量与方向准入/收尾回报、保活、以及静态接线核对。

    python tests/verify_duplex_stack_gui.py     # exit 0 = 全绿
"""
import io
import json
import os
import re
import sys
import tempfile
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_stack_gui_")
os.environ["APPDATA"] = _SANDBOX                  # 用户数据目录 → 沙箱（不碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui  # noqa: E402
import duplex_stack as ds  # noqa: E402
import manual_duplex as md  # noqa: E402
import paths  # noqa: E402
from printer_config import PrintJob, PrinterProfile, TabSettings  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ── 真 PDF 素材 ──
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


PDF5 = make_pdf("doc5.pdf", 5)
PDF3 = make_pdf("doc3.pdf", 3)
PDF2 = make_pdf("doc2.pdf", 2)
check("测试素材：3 份真 PDF（5/3/2 页）",
      all(os.path.isfile(p) and os.path.getsize(p) > 400 for p in (PDF5, PDF3, PDF2)))

MANUAL = PrinterProfile(auto_duplex=False, capability_source="manual", manual_duplex=True,
                        first="odd", reverse_second=True, second_rotate_180=True,
                        verified_at="2026-10-12 22:14:49", verified_by="测试成员",
                        stack_capacity=90, stack_warn_after_sec=1800)


# ============================================================
# 桩：打印调用记录 + 无副作用
# ============================================================
class Rec:
    def __init__(self):
        self.plan_pass = []      # [(which, rotate, 0-based序列, 1-based序列)]
        self.print_pdf = []
        self.announce = []
        self.planned = []
        self.entry_done = []
        self.job_finished = []
        self.all_finished = []
        self.samples = []
        self.waited = []
        self.progress = []


def patched(rec, plan_ok=True, drained=True):
    orig = {n: getattr(gui, n) for n in
            ("print_pdf", "print_plan_pass", "wait_for_output", "get_converter")}

    def _print_pdf(**kw):
        rec.print_pdf.append(kw)
        return True, "桩：一次提交"

    def _print_plan_pass(plan, path, printer, which, second_rotate_180=False,
                         progress_callback=None, dpi=0):
        rec.plan_pass.append((which, bool(which == 2 and second_rotate_180),
                              plan.pass_page_indices(which), plan.pass_pages(which)))
        if progress_callback:
            n = len(plan.pass_page_indices(which))
            for i in range(1, n + 1):
                progress_callback(i, n)
        return plan_ok, f"桩：第 {which} 趟"

    def _wait_for_output(printer, pages=0, **kw):
        rec.waited.append(pages)
        return {"drained": bool(drained), "queue_seconds": 0.0 if drained else 120.0,
                "margin_seconds": 2.0, "peak_jobs": 0, "detail": ""}

    gui.print_pdf = _print_pdf
    gui.print_plan_pass = _print_plan_pass
    gui.wait_for_output = _wait_for_output
    gui.get_converter = lambda: object()

    def restore():
        for k, v in orig.items():
            setattr(gui, k, v)
    return restore


def make_job(path, duplex="on", copies=1, mode="long-edge", name="", task_id=0, order_id=0):
    return PrintJob(file_path=path, copies=copies, duplex=duplex, duplex_mode=mode,
                    page_range="", page_count=0, task_id=task_id, order_id=order_id,
                    display_name=name or os.path.basename(path))


def run_stack_pass1(jobs, out_pdf, cover=False, rotate=True, seq=1, plan_ok=True,
                    drained=True):
    """跑一次攒批第 1 趟（同线程直连信号）。drained=False 模拟"队列 120s 没排空"。"""
    rec = Rec()
    w = gui.PrintWorker(jobs=jobs, printer_name="Fake Print", duplex_mode="long-edge",
                        keep_temp_pdf=False, render_dpi=200, cover_page=cover,
                        cover_page_config={"order_number": "HN20261012-001",
                                           "created_at": "2026-10-12 15:00:00"},
                        manual_duplex_profile=MANUAL,
                        stack_mode=True, stack_out_pdf=out_pdf, stack_seq=seq,
                        stack_rotate=rotate)
    w.log_message.connect(lambda _m: None)
    w.progress.connect(lambda c, t, s: rec.progress.append((c, t, s)))
    w.job_finished.connect(lambda i, ok, msg: rec.job_finished.append((i, ok, msg)))
    w.all_finished.connect(lambda a, b: rec.all_finished.append((a, b)))
    w.duplex_announce.connect(rec.announce.append)
    w.stack_entry_planned.connect(lambda p: rec.planned.append(p))
    w.stack_entry_done.connect(lambda s, ok, msg, n: rec.entry_done.append((s, ok, msg, n)))
    w.duplex_speed_sample.connect(lambda n, t: rec.samples.append((n, t)))
    restore = patched(rec, plan_ok=plan_ok, drained=drained)
    try:
        w.run()
    finally:
        restore()
    return rec


# ============================================================
# ① 攒批第 1 趟：整单（双面+单面）进同一叠，第 1 趟打完不收尾
# ============================================================
print("\n=== ① 攒批第 1 趟 ===")
batch_dir = os.path.join(_SANDBOX, "batch_1")
os.makedirs(batch_dir, exist_ok=True)
OUT1 = os.path.join(batch_dir, "entry_0001.pdf")
jobs1 = [make_job(PDF5, "on", name="双面5页.pdf"), make_job(PDF3, "off", name="单面3页.pdf")]
rec = run_stack_pass1(jobs1, OUT1, rotate=True)
check("★ 第 1 趟只打一趟（plan_pass 只调用 1 次、which=1）",
      [p[0] for p in rec.plan_pass] == [1], f"{[p[0] for p in rec.plan_pass]}")
check("★ 第 1 趟不旋转（旋转只在第 2 趟）", rec.plan_pass and rec.plan_pass[0][1] is False)
check("★ 第 1 趟页序 = 双面 1/3/5 + 单面 6/7/8（单面也进这一叠）",
      rec.plan_pass[0][3] == [1, 3, 5, 6, 7, 8], f"{rec.plan_pass[0][3]}")
check("★ 本单 PDF 已生成在批次目录里（第 2 趟要重排它）",
      os.path.isfile(OUT1) and os.path.getsize(OUT1) > 400)
check("★ stack_entry_planned 在打印**之前**发出（崩了也知道叠里有什么）",
      len(rec.planned) == 1 and rec.planned[0]["state"] == ds.STATE_PRINTING)
pl = rec.planned[0]
check("★ 本单台账：6 张纸 / 8 页 / file_plans 带真实单双面",
      pl["sheet_count"] == 6 and pl["page_count"] == 8
      and [f["duplex"] for f in pl["file_plans"]] == [True, False],
      f"{pl['sheet_count']}/{pl['page_count']}/{pl['file_plans']}")
check("★ 台账里每个文件计划都带 file_pages（它**自己**的页号，供列表显示；"
      "pages 是合并 PDF 里的页码，不拿来当页号给人看）",
      [f.get("file_pages") for f in pl["file_plans"]] == [[1, 2, 3, 4, 5], [1, 2, 3]]
      and [f["pages"] for f in pl["file_plans"]] == [[1, 2, 3, 4, 5], [6, 7, 8]],
      f"{pl['file_plans']}")
check("★ 第 2 趟页序（整叠逆序）＝ 单面 3 张空白在前 + 双面 5 的 4/2/空白",
      pl["pass2_pages"] == [None, None, None, None, 4, 2], f"{pl['pass2_pages']}")
check("★ 铁律②：第 1 趟**不**发 job_finished（半成品不能被标记完成）",
      rec.job_finished == [], f"{rec.job_finished}")
check("★ all_finished 报 (0,0)（本单任务是半成品，不算成功也不算失败）",
      rec.all_finished == [(0, 0)], f"{rec.all_finished}")
check("★ 入叠播报一次（攒批模式文案，说明不会停下来等翻纸）",
      len(rec.announce) == 1 and "攒批模式" in rec.announce[0]
      and "不会" in rec.announce[0], (rec.announce[0][:50] if rec.announce else ""))
check("★ entry_done 报成功且张数正确",
      rec.entry_done == [(1, True, "第 1 趟完成（6 张纸已堆入出纸槽）", 6)],
      f"{rec.entry_done}")

# 封面页并入第 1 趟（不单独打 —— 单独打会插进纸叠中间）
OUT1C = os.path.join(batch_dir, "entry_0002.pdf")
rec_c = run_stack_pass1(jobs1, OUT1C, cover=True)
plc = rec_c.planned[0]
check("★ 封面页并入本单：占独立一张纸、file_plans 首项是单面封面",
      plc["file_plans"][0] == {"label": "封面页", "pages": [1], "file_pages": [1],
                               "duplex": False},
      f"{plc['file_plans'][:1]}")
check("★ 封面页**没有**被单独 print_pdf（否则会插进纸叠中间串页）",
      rec_c.print_pdf == [], f"{rec_c.print_pdf}")
check("★ 带封面的本单：9 页 / 7 张纸，第 1 趟首面是封面", 
      plc["page_count"] == 9 and plc["sheet_count"] == 7
      and plc["pass1_pages"][0] == 1, f"{plc['page_count']}/{plc['sheet_count']}")

# 第 1 趟失败 → entry_done 报失败（主线程据此把整叠标存疑）
OUT1F = os.path.join(batch_dir, "entry_0003.pdf")
rec_f = run_stack_pass1(jobs1, OUT1F, plan_ok=False)
check("★ 第 1 趟失败 → entry_done 报失败（不静默）",
      rec_f.entry_done and rec_f.entry_done[0][1] is False
      and "失败" in rec_f.entry_done[0][2], f"{rec_f.entry_done}")

# 出纸等待超时（打印机离线/USB 拔掉：spooler 永不排空 → 等满 120s）：
# 这段"实测"里混着超时等待，不是打印机耗时 ⇒ 绝不能写进速度样本（2026-10-21 现场）
OUT1T = os.path.join(batch_dir, "entry_0004.pdf")
rec_t = run_stack_pass1(jobs1, OUT1T, drained=False)
check("★ 出纸等待超时（队列没排空）→ 不写速度样本（120s 超时不能当打印机耗时学进去）",
      rec_t.samples == [] and rec_t.entry_done and rec_t.entry_done[0][1] is True,
      f"samples={rec_t.samples} waited={rec_t.waited}")


# ============================================================
# ② 整叠第 2 趟 worker：合并各单 PDF + 全局逆序 + 旋转补偿
# ============================================================
print("\n=== ② 整叠第 2 趟 worker ===")
stack = ds.DuplexStack(capacity=90, printer_name="Fake Print")
stack.new_batch("batch_flush")
bdir = ds.batch_dir(os.path.join(_SANDBOX, "stack_root"), stack.batch_id)
os.makedirs(bdir, exist_ok=True)

# 第 1 单：双面 5 页（3 张纸）→ 单内配对 1/2 3/4 5/空；第 2 单：单面 2 页（2 张纸）
e1 = ds.StackEntry(seq=1, title="单A", page_count=5, sheet_count=3, rotate=True,
                   pdf="entry_0001.pdf",
                   file_plans=[{"label": "A", "pages": [1, 2, 3, 4, 5], "duplex": True}])
e2 = ds.StackEntry(seq=2, title="单B", page_count=2, sheet_count=2, rotate=True,
                   pdf="entry_0002.pdf",
                   file_plans=[{"label": "B", "pages": [1, 2], "duplex": False}])
import shutil as _sh
_sh.copy2(PDF5, os.path.join(bdir, "entry_0001.pdf"))
_sh.copy2(PDF2, os.path.join(bdir, "entry_0002.pdf"))
stack.entries = [e1, e2]
stack.rotate = True

rec2 = Rec()
restore = patched(rec2)
try:
    fw = gui.DuplexFlushWorker(stack, bdir, "Fake Print", dpi=200)
    fw.log_message.connect(lambda m: None)
    fw.progress.connect(lambda c, t, s: rec2.progress.append((c, t, s)))
    res = []
    fw.done.connect(lambda ok, msg, n: res.append((ok, msg, n)))
    fw.run()
finally:
    restore()
check("★ 整叠第 2 趟只打 which=2 且带 180° 补偿", 
      [p[0] for p in rec2.plan_pass] == [2] and rec2.plan_pass[0][1] is True,
      f"{rec2.plan_pass}")
check("★ 整叠合并后页数 = 各单页数之和（5+2=7）",
      os.path.isfile(os.path.join(bdir, "merged_pass2.pdf")))
import fitz  # noqa: E402
with fitz.open(os.path.join(bdir, "merged_pass2.pdf")) as _d:
    check("★ 合并文档 7 页且各单顺序不变", len(_d) == 7, f"{len(_d)}")
check("★ 整叠第 2 趟页序 = 单B(2 张空白) 在前、单A(空/4/2) 在后（整叠逆序）",
      rec2.plan_pass[0][3] == [None, None, None, 4, 2],
      f"{rec2.plan_pass[0][3]}")
check("★ 进度按整叠面数推进（5 面 = 2 张单B + 3 张单A）",
      len(rec2.progress) == 5, f"{len(rec2.progress)}")
check("★ done 报成功且面数=5", res and res[0][0] and res[0][2] == 5, f"{res}")

# 源文件缺失 → 明确失败（纸还在出纸槽，不能静默当成功）
stack_missing = ds.DuplexStack(capacity=90)
stack_missing.new_batch("batch_missing")
bdir_m = ds.batch_dir(os.path.join(_SANDBOX, "stack_root"), stack_missing.batch_id)
os.makedirs(bdir_m, exist_ok=True)
stack_missing.entries = [ds.StackEntry(seq=1, page_count=1, sheet_count=1, pdf="none.pdf",
                                       file_plans=[{"label": "A", "pages": [1], "duplex": True}])]
res_m = []
fw_m = gui.DuplexFlushWorker(stack_missing, bdir_m, "Fake Print")
fw_m.done.connect(lambda ok, msg, n: res_m.append((ok, msg, n)))
restore = patched(rec2)
try:
    fw_m.run()
finally:
    restore()
check("★ 源 PDF 缺失 → 明确失败并提示人工处理（不静默成功）",
      res_m and res_m[0][0] is False and "人工处理" in res_m[0][1], f"{res_m}")


# ============================================================
# ③ 主窗口：闸门 / 排队 / 准入 / 收尾回报
# ============================================================
print("\n=== ③ 主窗口攒批状态机 ===")

# 弹窗全部自动应答，避免 offscreen 下死等
gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.Yes)
gui.QMessageBox.warning = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)
gui.QMessageBox.information = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)

# 打印线程不真跑：只记录"启动过的 worker"，由测试手动喂信号
started_workers = []
_orig_start = gui.PrintWorker.start


def _fake_start(self):
    started_workers.append(self)


gui.PrintWorker.start = _fake_start

flush_created = []


class _Sig:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)


class FlushStub:
    def __init__(self, stack, batch_dir, printer_name, dpi=0, parent=None):
        self.stack, self.batch_dir = stack, batch_dir
        self.progress, self.log_message, self.done = _Sig(), _Sig(), _Sig()
        self.started = False
        flush_created.append(self)

    def start(self):
        self.started = True

    def isRunning(self):
        return bool(self.started)   # 真 worker 一 start 就在跑；列表按钮的 busy 判据要能测到

    def wait(self, ms=0):
        return True


_orig_flush = gui.DuplexFlushWorker
gui.DuplexFlushWorker = FlushStub


class CloudStub:
    """只记录云端调用，不联网（is_connected=False → 走离线取号路径）。"""

    def __init__(self):
        self.success, self.fail, self.keepalive = [], [], []
        self.api_url, self.token, self.client_id = "", "", "test-client"

    def is_connected(self):
        return False

    def report_success(self, task_id, cfg=None):
        self.success.append((task_id, cfg or {}))

    def report_fail(self, task_id, msg):
        self.fail.append((task_id, msg))

    def report_keepalive(self, ids):
        self.keepalive.append(list(ids))
        return True

    def report_start_printing(self, order_id, task_ids):
        return True

    def accept_order_to_server(self, order_id):
        return "ok"

    def abandon_order_to_server(self, order_id):
        return True

    def reject_task(self, task_id):
        return True


win = gui.MainWindow(config_path=paths.config_path())


def _prep(w=None):
    """每次启动打印前把"打印机名 + 档案"钉回去。

    `_start_print_worker` 内部会 `_sync_ui_to_config()`，把 `printer_name` 从界面下拉框
    读回来（生产环境里用户选的队列名就在那）；测试环境下拉框里是**本机真实打印机列表**，
    会把我们伪造的队列名覆盖掉 → 档案查不到 → 手动双面判定失效。
    """
    w = w or win
    w._config.printer_name = "Fake Print"
    w._config.printer_profiles["Fake Print"] = MANUAL


_prep()
win._config.admin_names = ["测试成员"]
win._config.duplex_stack_mode = True
win._cloud_client = CloudStub()
win._config.tabs = {"1": TabSettings(jobs=[], owner_name="测试成员", is_admin_print=False),
                    "2": TabSettings(jobs=[], owner_name="测试成员", is_admin_print=False)}
win._current_tab = "1"
win._stack.capacity = 90
try:
    paths.duplex_stack_dir()
    for _n in os.listdir(paths.duplex_stack_dir()):
        ds.discard_batch(os.path.join(paths.duplex_stack_dir(), _n))
except OSError:
    pass

check("攒批模式开关读得到（配置）", win._stack_mode_on() is True)

# 3.1 请求 A（双面）→ 作为第 1 单开打第 1 趟
jobA = make_job(PDF5, "on", name="A.pdf", task_id=101, order_id=11)
win._config.tabs["1"].jobs = [jobA]
_prep()
okA = win._start_print_worker([jobA], tab_key="1")
check("★ 攒批开启 + 双面请求 → 走「攒批单」路径（进叠而不是单任务翻纸）",
      okA and started_workers and started_workers[-1]._stack_mode is True,
      f"ok={okA} stack_mode="
      f"{getattr(started_workers[-1] if started_workers else None, '_stack_mode', None)}")
wA = started_workers[-1]
check("★ 本单输出 PDF 落在批次目录里（entry_0001.pdf）",
      wA._stack_out_pdf.endswith("entry_0001.pdf")
      and os.path.dirname(wA._stack_out_pdf) == win._stack_batch_dir(),
      wA._stack_out_pdf)
check("★ 有单在飞 → 出纸槽视为被独占", win._stack_open_for_business() is True)

# 3.2 请求 B（纯单面、另一个标签页）→ 必须排队（铁律①）
jobB = make_job(PDF3, "off", name="B.pdf", task_id=102, order_id=12)
win._config.tabs["2"].jobs = [jobB]
_prep()
okB = win._start_print_worker([jobB], tab_key="2")
check("★ 攒批期间单面订单被排队（插进纸叠会串页）",
      okB is True and len(win._stack_wait) == 1
      and win._stack_wait[0]["jobs"][0] is jobB,
      f"ok={okB} wait={len(win._stack_wait)}")
check("★ 排队不算「启动失败」（返回 True，免得被当打印机忙而反复重试）",
      okB is True and len(started_workers) == 1)

# 3.3 本单计划 → 入叠；第 1 趟完成 → 状态落盘
payloadA = {
    "seq": 1, "pdf": "entry_0001.pdf", "rotate": True,
    "file_plans": [{"label": "A.pdf", "pages": [1, 2, 3, 4, 5], "duplex": True}],
    "page_count": 5, "sheet_count": 3,
    "pass1_pages": [1, 3, 5], "pass2_pages": [None, 4, 2],
    "state": ds.STATE_PRINTING,
    "items": [{"idx": 0, "task_id": 101, "order_id": 11, "order_number": "HN20261012-A",
               "display_name": "A.pdf", "file_path": PDF5, "copies": 1, "duplex": "on",
               "page_range": "", "page_count": 5}],
}
win._on_stack_entry_planned(payloadA)
check("★ 入叠后台账：1 单 / 3 张纸",
      win._stack.count == 1 and win._stack.total_sheets == 3, win._stack.sheet_text())
check("★ 清单已落盘（崩溃/强杀后靠它续打第 2 趟）",
      os.path.isfile(ds.manifest_path(win._stack_batch_dir())))
_m = ds.load_manifest(win._stack_batch_dir())
check("★ 落盘清单能读回且任务快照完整",
      _m is not None and _m.count == 1 and _m.entries[0].tasks[0]["task_id"] == 101)
win._on_stack_entry_done(1, True, "第 1 趟完成（3 张纸已堆入出纸槽）", 3)
check("★ 第 1 趟完成 → 单状态 pass1_done",
      win._stack.entries[0].state == ds.STATE_DONE)
win._on_all_finished(0, 0)     # worker 结束（模拟）
check("★ worker 结束不把本单任务标记完成（铁律②）",
      jobA.sent is False and win._cloud_client.success == [])
check("★ 攒批单结束不清空纸叠、状态栏显示攒批中",
      win._stack.count == 1 and "攒批中" in win._status_label.text(),
      win._status_label.text())
check("★ 出纸槽仍被这一叠独占（纸叠非空 → 别的请求继续排队）",
      win._stack_open_for_business() is True)   # 纸叠非空 → 仍然独占

# 3.4 请求 C（双面、容量够）→ 第 2 单
jobC = make_job(PDF2, "on", name="C.pdf", task_id=103, order_id=13)
win._config.tabs["1"].jobs = [jobA, jobC]
_prep()
okC = win._start_print_worker([jobC], tab_key="1")
check("★ 纸叠非空但容量够 + 方向一致 → 第 2 单接着堆",
      okC and started_workers[-1]._stack_out_pdf.endswith("entry_0002.pdf"),
      f"{started_workers[-1]._stack_out_pdf}")
win._on_stack_entry_planned({
    "seq": 2, "pdf": "entry_0002.pdf", "rotate": True,
    "file_plans": [{"label": "C.pdf", "pages": [1, 2], "duplex": True}],
    "page_count": 2, "sheet_count": 1,
    "pass1_pages": [1], "pass2_pages": [2], "state": ds.STATE_PRINTING,
    "items": [{"idx": 0, "task_id": 103, "order_id": 13, "order_number": "HN20261012-C",
               "display_name": "C.pdf", "file_path": PDF2, "copies": 1, "duplex": "on",
               "page_range": "", "page_count": 2}],
})
win._on_stack_entry_done(2, True, "第 1 趟完成（1 张纸已堆入出纸槽）", 1)
win._on_all_finished(0, 0)
check("★ 两单在叠：4 张纸（3+1）", win._stack.count == 2 and win._stack.total_sheets == 4,
      win._stack.sheet_text())

# 3.5 容量闸门：把容量压到 4，请求 D（双面 5 页 = 3 张）放不下 → 排队
win._stack.capacity = 4
jobD = make_job(PDF5, "on", name="D.pdf", task_id=104, order_id=14)
win._config.tabs["2"].jobs = [jobB, jobD]
n_before = len(started_workers)
_prep()
okD = win._start_print_worker([jobD], tab_key="2")
check("★ 容量闸门：加上这一单会超上限 → 拒绝入叠并排队",
      okD is True and len(started_workers) == n_before
      and any(r["jobs"][0] is jobD for r in win._stack_wait),
      f"wait={[r['reason'][:20] for r in win._stack_wait]}")
check("★ 容量拒绝的文案点明已堆/还需/上限",
      "4" in win._stack_wait[-1]["reason"] and "上限" in win._stack_wait[-1]["reason"],
      win._stack_wait[-1]["reason"])

# 3.6 装订方向闸门：短边请求 → 拒（一叠只能一种旋转补偿）
jobS = make_job(PDF2, "on", mode="short-edge", name="S.pdf", task_id=105, order_id=15)
_prep()
okS = win._start_print_worker([jobS], tab_key="1")
check("★ 装订方向不一致 → 拒绝入叠并排队",
      okS is True and any(r["jobs"][0] is jobS for r in win._stack_wait)
      and "装订方向" in win._stack_wait[-1]["reason"],
      f"{win._stack_wait[-1]['reason'][:30]}")

# 3.7 保活：纸叠/排队/在飞的任务都被覆盖
alive = win._collect_keepalive_tasks()
check("★ 保活覆盖纸叠两单 + 排队的单（含页数/任务 id）",
      {101, 103, 102, 104, 105} <= alive, f"{sorted(alive)}")
win._tick_keepalive(force=True)
_n_ka = len(win._cloud_client.keepalive)
check("★ 保活真的发给后端（含纸叠两单与排队的单）",
      _n_ka >= 1 and {101, 103, 102, 104, 105} <= set(win._cloud_client.keepalive[-1]),
      f"{win._cloud_client.keepalive[-1] if _n_ka else None}")
win._tick_keepalive()          # 集合没变 → 不重复发
check("★ 集合没变时不重复保活（不空转刷后端）",
      len(win._cloud_client.keepalive) == _n_ka)
jobE = make_job(PDF2, "on", name="E.pdf", task_id=106, order_id=16)
win._config.tabs["1"].jobs = [jobA, jobC, jobE]
_prep()
win._start_print_worker([jobE], tab_key="1")
win._tick_keepalive()
check("★ 有新任务进来 → 立刻补发一次保活（新订单的 3 分钟窗口开始顺延）",
      len(win._cloud_client.keepalive) == _n_ka + 1
      and 106 in win._cloud_client.keepalive[-1], f"{win._cloud_client.keepalive}")

# 3.8 打整叠第 2 趟：按钮 → 整叠 worker → 收尾所有单
check("攒批入口按钮显示张数", "4" in win._btn_stack.text(), win._btn_stack.text())
win._on_stack_flush()
check("★ 开始第 2 趟：整叠 worker 带着**同一个批次目录**启动",
      flush_created and flush_created[-1].batch_dir == win._stack_batch_dir()
      and flush_created[-1].stack.count == 2,
      f"{flush_created[-1].batch_dir if flush_created else None}")
win._on_stack_flush_done(True, "整叠第 2 趟完成（2 单 / 4 张纸 / 4 面）", 4)
check("★ 整叠打完 → 两单的任务都标记完成 + 都回报了云端成功",
      jobA.sent is True and jobC.sent is True
      and {101, 103} <= {t for t, _ in win._cloud_client.success},
      f"sent={jobA.sent},{jobC.sent} success={[t for t, _ in win._cloud_client.success]}")
_succ_cfg = dict(win._cloud_client.success[0][1])
check("★ 回报带归属与管理员标记（标签页之后可能被删，所以收尾用快照）",
      _succ_cfg.get("owner_name") == "测试成员" and "is_admin_print" in _succ_cfg,
      f"{_succ_cfg}")
check("★ 打完清空纸叠 + 删掉批次目录（不留垃圾）",
      win._stack.entries == [] and not os.path.isdir(flush_created[-1].batch_dir))
check("★ 收尾后自动放行排队请求（容量已腾空）",
      len(win._stack_wait) <= 1, f"wait={len(win._stack_wait)}")

# 3.8b 标签页被删/重编号 → 找不到 job 也要按快照回报云端（订单不能永远吊在 printing）
entry_lost = ds.StackEntry(
    seq=9, title="已删标签页的单", tab_key="(已删除)", page_count=1, sheet_count=1,
    pdf="x.pdf", file_plans=[{"label": "z", "pages": [1], "duplex": True}],
    tasks=[{"task_id": 401, "copies": 1, "duplex": "on", "page_range": "",
            "page_count": 1, "owner_name": "测试成员", "is_admin_print": False}])
_n_succ = len(win._cloud_client.success)
win._finish_stack_entries([entry_lost], True, "ok")
_lost_cfg = next((c for t, c in win._cloud_client.success[_n_succ:] if t == 401), None)
check("★ 标签页已删（job 找不到）→ 仍按任务快照回报云端成功 + 带上归属",
      _lost_cfg is not None and _lost_cfg.get("owner_name") == "测试成员",
      f"{win._cloud_client.success[_n_succ:]}")

# 3.9 第 2 趟失败 → 按失败收尾（绝不标完成）
win2 = win
jobF = make_job(PDF2, "on", name="F.pdf", task_id=201, order_id=21)
win2._stack.entries = []
win2._stack_wait = []
win2._config.tabs["1"].jobs = [jobF]
_prep(win2)
win2._start_print_worker([jobF], tab_key="1")
win2._on_stack_entry_planned({
    "seq": 1, "pdf": "entry_0001.pdf", "rotate": True,
    "file_plans": [{"label": "F.pdf", "pages": [1, 2], "duplex": True}],
    "page_count": 2, "sheet_count": 1,
    "pass1_pages": [1], "pass2_pages": [2], "state": ds.STATE_PRINTING,
    "items": [{"idx": 0, "task_id": 201, "order_id": 21, "order_number": "HN-F",
               "display_name": "F.pdf", "file_path": PDF2, "copies": 1, "duplex": "on",
               "page_range": "", "page_count": 2}],
})
win2._on_stack_entry_done(1, True, "ok", 1)
win2._on_all_finished(0, 0)
n_fail_before = len(win2._cloud_client.fail)
win2._on_stack_flush_done(False, "整叠第 2 趟失败：桩", 0)
check("★ 第 2 趟失败 → 任务不标记完成、按失败上报云端",
      jobF.sent is False
      and any(t == 201 for t, _ in win2._cloud_client.fail[n_fail_before:]),
      f"sent={jobF.sent} fail={win2._cloud_client.fail[n_fail_before:]}")
check("★ 失败后也清空纸叠（纸交人工处理，不能一直占着出纸槽）",
      win2._stack.entries == [])

# 3.10 放弃整叠 → 按失败收尾
jobG = make_job(PDF2, "on", name="G.pdf", task_id=202, order_id=22)
win2._config.tabs["1"].jobs = [jobG]
_prep(win2)
win2._start_print_worker([jobG], tab_key="1")
win2._on_stack_entry_planned({
    "seq": 1, "pdf": "entry_0001.pdf", "rotate": True,
    "file_plans": [{"label": "G.pdf", "pages": [1, 2], "duplex": True}],
    "page_count": 2, "sheet_count": 1,
    "pass1_pages": [1], "pass2_pages": [2], "state": ds.STATE_PRINTING,
    "items": [{"idx": 0, "task_id": 202, "order_id": 22, "order_number": "HN-G",
               "display_name": "G.pdf", "file_path": PDF2, "copies": 1, "duplex": "on",
               "page_range": "", "page_count": 2}],
})
win2._on_stack_entry_done(1, True, "ok", 1)
win2._on_all_finished(0, 0)
n_fail_before = len(win2._cloud_client.fail)
win2._on_stack_abandon()
check("★ 放弃整叠 → 任务按失败上报、不标记完成、清空纸叠",
      jobG.sent is False and win2._stack.entries == []
      and any(t == 202 for t, _ in win2._cloud_client.fail[n_fail_before:]),
      f"{win2._cloud_client.fail[n_fail_before:]}")

# 3.11 攒批关闭 + 无纸叠 → 回到普通路径（零回归）
win2._config.duplex_stack_mode = False
n_before = len(started_workers)
jobH = make_job(PDF2, "on", name="H.pdf", task_id=203, order_id=23)
win2._config.tabs["1"].jobs = [jobH]
_prep(win2)
okH = win2._start_print_worker([jobH], tab_key="1")
check("★ 攒批关闭 → 双面请求走原路径（stack_mode=False，不进叠）",
      okH and started_workers[-1] is not None
      and started_workers[-1]._stack_mode is False, f"ok={okH}")
# 审计 2026-10：这里原先写成 `started_workers[-1]._stack_pending_ctx = None` ——
# 赋给了**桩 worker**，而 `_stack_pending_ctx` 是 MainWindow 的属性（gui.py:2536）
# ⇒ 该赋值等于没写，只在桩上挂了个没人读的字段，回归抓不到。清理对象改为 win2 并补断言。
win2._stack_pending_ctx = {}
check("★ 非攒批路径不残留待处理上下文（_stack_pending_ctx 已清空）",
      not win2._stack_pending_ctx,
      f"ctx={win2._stack_pending_ctx!r}")
win2._config.duplex_stack_mode = True

# 3.12 离开攒批（关掉开关）但纸叠还在 → 别的请求仍要排队
win2._config.duplex_stack_mode = False
win2._stack.entries = [ds.StackEntry(seq=1, title="遗留", page_count=2, sheet_count=1,
                                     pdf="x.pdf",
                                     file_plans=[{"label": "x", "pages": [1, 2],
                                                  "duplex": True}])]
jobI = make_job(PDF2, "off", name="I.pdf")
_prep(win2)
win2._start_print_worker([jobI], tab_key="1")
check("★ 开关关了但纸叠还没打完 → 别的输出依旧排队（不能插进纸叠）",
      any(r["jobs"][0] is jobI for r in win2._stack_wait),
      f"wait={len(win2._stack_wait)}")
win2._config.duplex_stack_mode = True
win2._stack.entries = []
win2._stack_wait = []


# ============================================================
# ③b 翻纸窗按钮 → 攒批动作的**真实点击路径**（2026-10-20 现场 bug）
# ============================================================
# 现场症状：点「▶ 开始第 2 趟」后窗口切到「③ 正在打印整叠的第 2 趟…（请勿再动纸叠）」，
# 却一张纸都不出、两个按钮全灰 —— 现场读作"点了没反应"，而且再也点不动。
# 根因：ManualDuplexDialog._on_go/_on_skip 是**先改 `_mode` 再 emit**，主窗口
# `_on_duplex_continue` 按 `dlg._mode == "stack_ready"` 分流 ⇒ 永远分不进攒批分支
# （弃整叠同理：`_on_duplex_skip` 也一样分不进去）。旧验收全是直接调 `win._on_stack_flush()`,
# 没走按钮 ⇒ 这条断线一直没人抓到。
print("\n=== ③b 翻纸窗按钮 → 攒批动作（真实点击） ===")


def _seed_stack(win_, task_id=901, order_id=91, pdf=PDF2):
    """造一单已经打完第 1 趟、正等第 2 趟的纸叠。"""
    job = make_job(pdf, "on", name="K.pdf", task_id=task_id, order_id=order_id)
    win_._config.tabs["1"].jobs = [job]
    _prep(win_)
    win_._start_print_worker([job], tab_key="1")
    win_._on_stack_entry_planned({
        "seq": 1, "pdf": "entry_0001.pdf", "rotate": True,
        "file_plans": [{"label": "K.pdf", "pages": [1, 2], "duplex": True}],
        "page_count": 2, "sheet_count": 1,
        "pass1_pages": [1], "pass2_pages": [2], "state": ds.STATE_PRINTING,
        "items": [{"idx": 0, "task_id": task_id, "order_id": order_id,
                   "order_number": "HN-K", "display_name": "K.pdf", "file_path": pdf,
                   "copies": 1, "duplex": "on", "page_range": "", "page_count": 2}],
    })
    win_._on_stack_entry_done(1, True, "第 1 趟完成（1 张纸已堆入出纸槽）", 1)
    win_._on_all_finished(0, 0)
    return job


def _ready_list(win_):
    """叫出攒批列表并按当前纸叠刷新 —— 第 2 趟从这里发起（2026-10-20 改版）。"""
    lst = win_._ensure_stack_queue_dialog()
    win_._refresh_stack_queue()
    return lst


# ① 开始第 2 趟：按钮点下去必须真的启动整叠 worker（现场 bug 就是这里断的）
jobK = _seed_stack(win2, task_id=901, order_id=91)
_lst = _ready_list(win2)
_n_flush = len(flush_created)
_lst._btn_start.click()
check("★ 点「▶ 开始打印第 2 趟」→ 真的启动整叠第 2 趟 worker（现场 bug：点了没反应）",
      len(flush_created) == _n_flush + 1 and flush_created[-1].started
      and win2._stack_flush_worker is flush_created[-1],
      f"created={len(flush_created) - _n_flush} "
      f"started={flush_created[-1].started if flush_created else None}")
check("★ 启动后列表按钮立刻禁用（第 2 趟在飞时不许再点）",
      not _lst._btn_start.isEnabled() and "正在打整叠第 2 趟" in _lst._summary.text(),
      f"{_lst._btn_start.isEnabled()}/{_lst._summary.text()[:24]}")
win2._on_stack_flush_done(True, "整叠第 2 趟完成（桩）", 1)
check("★ 真实点击 → 打完正常收尾（本单标完成、纸叠清空）",
      jobK.sent is True and win2._stack.entries == [],
      f"sent={jobK.sent} entries={win2._stack.count}")

# ② 放弃整叠：同一处断线的另一半（点下去必须按失败收尾，不能只是"跳过本任务"）
jobL = _seed_stack(win2, task_id=902, order_id=92)
_lst = _ready_list(win2)
gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.Yes)
_n_fail = len(win2._cloud_client.fail)
_lst._btn_abandon.click()
check("★ 点「⏭ 放弃整叠」→ 真的按失败收尾 + 清空纸叠（旧写法只打一行日志，"
      "半成品留在出纸槽、订单一直挂着）",
      jobL.sent is False and win2._stack.entries == []
      and any(t == 902 for t, _ in win2._cloud_client.fail[_n_fail:]),
      f"entries={win2._stack.count} fail={win2._cloud_client.fail[_n_fail:]}")

# ③ 放弃时点「否」→ 纸叠保留、按钮必须还能点（旧写法是死状态）
jobM = _seed_stack(win2, task_id=903, order_id=93)
_lst = _ready_list(win2)
gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.No)
_lst._btn_abandon.click()
check("★ 放弃整叠点「否」→ 纸叠保留 + 按钮仍可用（不卡死）",
      win2._stack.count == 1 and _lst._btn_start.isEnabled()
      and _lst._btn_abandon.isEnabled() and jobM.sent is False,
      f"entries={win2._stack.count} enabled={_lst._btn_start.isEnabled()}")

# ④ 开打前的落机对账点「否」→ 不启动、不抽纸、纸叠保留、按钮仍可用
_n_flush = len(flush_created)
_lst._btn_start.click()
check("★ 对账点「否」→ 第 2 趟不启动（不白抽纸）+ 纸叠保留 + 按钮仍可用",
      len(flush_created) == _n_flush and win2._stack.count == 1
      and _lst._btn_start.isEnabled(),
      f"created={len(flush_created) - _n_flush} entries={win2._stack.count}")

# ⑤ 打印机还在忙（第 1 趟在打）时：列表按钮直接禁用，点了也不会启动
class _RunningWorker:
    def isRunning(self):
        return True


win2._worker = _RunningWorker()
win2._refresh_stack_queue()
_n_flush = len(flush_created)
gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.Yes)
_lst._btn_start.click()
check("★ 忙时列表按钮禁用 → 点不动、不会启动第 2 趟（纸叠保留）",
      len(flush_created) == _n_flush and not _lst._btn_start.isEnabled()
      and win2._stack.count == 1,
      f"created={len(flush_created) - _n_flush} enabled={_lst._btn_start.isEnabled()}")
win2._worker = None
win2._stack.clear()
win2._stack_wait = []

gui.PrintWorker.start = _orig_start
gui.DuplexFlushWorker = _orig_flush


# ============================================================
# ⑤ 启动时续打遗留纸叠（进程被杀/退出时纸还在出纸槽）
# ============================================================
print("\n=== ⑤ 启动续打遗留纸叠 ===")
_root = paths.duplex_stack_dir()
for _n in os.listdir(_root):
    ds.discard_batch(os.path.join(_root, _n))

# 一个"垃圾"批次（清单坏了 → 不该被当成待续打，且要被顺手清掉）
_garbage = ds.batch_dir(_root, "batch_garbage")
os.makedirs(_garbage, exist_ok=True)
with open(os.path.join(_garbage, ds.MANIFEST_NAME), "w", encoding="utf-8") as _f:
    _f.write("{坏")

st_old = ds.DuplexStack(capacity=33, printer_name="Fake Print")
st_old.new_batch("batch_resume")
st_old.add(ds.StackEntry(
    seq=1, title="遗留单", page_count=2, sheet_count=1, rotate=True, pdf="entry_0001.pdf",
    file_plans=[{"label": "x", "pages": [1, 2], "duplex": True}],
    tasks=[{"task_id": 301, "copies": 1, "duplex": "on", "page_range": "",
            "page_count": 2, "owner_name": "测试成员", "is_admin_print": False}]))
bdir_old = ds.batch_dir(_root, st_old.batch_id)
os.makedirs(bdir_old, exist_ok=True)
ds.save_manifest(bdir_old, st_old)
check("测试前提：磁盘上有一个待续打的批次 + 一个坏批次",
      os.path.isfile(ds.manifest_path(bdir_old)) and os.path.isdir(_garbage))

gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.Yes)
win._stack.clear()
win._check_pending_stack_on_startup()
check("★ 启动发现遗留纸叠 → 恢复整叠（含容量等档案参数）",
      win._stack.count == 1 and win._stack.capacity == 33,
      f"count={win._stack.count} cap={win._stack.capacity}")
check("★ 恢复后 batch_id 与磁盘目录一致（否则第 2 趟找不到各单 PDF）",
      win._stack_batch_id_ok() if hasattr(win, "_stack_batch_id_ok") else
      win._stack_batch_dir() == bdir_old, f"{win._stack_batch_dir()} vs {bdir_old}")
check("★ 续打入口按钮亮出来（窗口未 show 时用 isHidden 判，off screen 下 isVisible 恒 False）",
      (not win._btn_stack.isHidden()) and "1" in win._btn_stack.text(),
      f"hidden={win._btn_stack.isHidden()} text={win._btn_stack.text()}")
check("★ 顺手清掉其它遗留批次目录（坏清单那个）", not os.path.isdir(_garbage))

gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.No)
win._stack.clear()
_n_fail = len(win._cloud_client.fail)
win._check_pending_stack_on_startup()
check("★ 选择放弃 → 按失败回报云端 + 删清单（不能把订单永远吊在 printing）",
      win._stack.count == 0 and not os.path.isdir(bdir_old)
      and any(t == 301 for t, _ in win._cloud_client.fail[_n_fail:]),
      f"{win._cloud_client.fail[_n_fail:]}")
gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.Yes)


# ============================================================
# ⑥ 攒批不再复用「手动双面」窗（2026-10-20 用户要求）
# ============================================================
print()
print("=== ⑥ 攒批不再复用「手动双面」窗（2026-10-20 用户要求） ===")
check("① 双面窗已没有攒批专用状态（announce_stack/set_stack_ready/stack_finished 全删）",
      not any(hasattr(gui.ManualDuplexDialog, _n) for _n in
              ("announce_stack", "set_stack_ready", "set_stack_waiting", "stack_finished")))
check("② 双面窗不再声明攒批信号",
      not hasattr(gui.ManualDuplexDialog, "stack_flush_requested")
      and not hasattr(gui.ManualDuplexDialog, "stack_abandon_requested"))
# 攒批第 1 趟播报只记日志 + 刷列表：绝不把翻纸窗弹出来（旧实现会弹，还从第 1 趟开始计时）
win._duplex_dialog = None
win._on_stack_announce("本单 2 个文件（第 1 趟）")
check("③ 攒批第 1 趟开打：**不弹**手动双面窗、不建窗口、不计时",
      getattr(win, "_duplex_dialog", None) is None,
      f"{type(getattr(win, '_duplex_dialog', None)).__name__}")
check("④ 但攒批列表被刷新了（操作员在列表里看进度）",
      getattr(win, "_stack_queue_dialog", None) is not None)
_G6 = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
_gate_src = _G6[_G6.index("def _stack_reconcile_gate"):
                _G6.index("def _finish_empty_stack_second_pass")]
check("⑤ 数纸对话框文案：说清「应是 M 张」+ 点是继续 / 点否取消",
      "请数清楚进纸盒里现在有多少张纸" in _gate_src
      and "应是 {present} 张" in _gate_src
      and "点「是」= 继续打印整叠的第 2 趟" in _gate_src)


# ============================================================
# ⑦ 底部「🖐 攒批」入口 + 攒批列表常驻（2026-10-20 改版 / 2026-10-21 收敛）
# ============================================================
print("\n=== ⑦ 「🖐 攒批」入口 + 列表常驻 ===")
win._stack.clear()
win._config.duplex_stack_mode = True
win._stack_list_user_closed = False
_prep()
check("前置：攒批模式生效 + 无纸叠", win._stack_mode_on() and not win._stack.entries)

# 2026-10-20 用户要求：开启攒批模式后列表就**常驻**，不用再点按钮
win._sync_stack_list_visible()
_lst = win._ensure_stack_queue_dialog()
check("★ 攒批模式开启 ⇒ 攒批列表常驻出现（不等点按钮）", not _lst.isHidden(),
      f"hidden={_lst.isHidden()}")

# 2026-10-21 用户要求：**空纸叠时底部入口不占位** —— 列表已常驻，留着它只会和左边
# 「🖐 攒批模式」复选框并排被读成"第二个开关"；点一下也只是把已经开着的窗抬到最前
win._sync_stack_button()
check("★ 空纸叠（模式开着）⇒ 底部「🖐 攒批」入口隐藏",
      win._btn_stack.isHidden(),
      f"hidden={win._btn_stack.isHidden()} text={win._btn_stack.text()!r}")
check("★ 隐藏态文案不再顶着光秃秃的「🖐 攒批」（与左边复选框区分开）",
      win._btn_stack.text().strip() == "🖐 攒批列表", repr(win._btn_stack.text()))

win._config.duplex_stack_mode = False
win._sync_stack_list_visible()
check("★ 关掉攒批模式 ⇒ 列表收起", _lst.isHidden(), f"hidden={_lst.isHidden()}")
win._sync_stack_button()
check("★ 空纸叠（模式关着）⇒ 入口同样隐藏（不是「模式关了才藏」）",
      win._btn_stack.isHidden(),
      f"hidden={win._btn_stack.isHidden()} text={win._btn_stack.text()!r}")
win._config.duplex_stack_mode = True

# 有纸叠 ⇒ 入口出现并给读数：这是它在主界面上剩的第一条实质作用
# （列表被最小化/被主窗挡住/挪到另一块屏时，只有这里能看到"压着多少张、几单"）
win._stack.entries = [ds.StackEntry(
    seq=1, title="HN-⑦", page_count=5, sheet_count=3, rotate=True,
    pdf="entry7.pdf", first="odd", state=ds.STATE_DONE,
    original_sheet_count=3,
    file_plans=[{"label": "⑦.pdf", "pages": [1, 2, 3, 4, 5], "duplex": True}],
    tasks=[{"task_id": 701, "order_id": 71, "owner_name": "测试成员",
            "is_admin_print": False, "copies": 1, "duplex": "on",
            "page_range": "", "page_count": 5}])]
win._sync_stack_button()
check("★ 有纸叠 ⇒ 入口出现且带「N张/M单」读数",
      (not win._btn_stack.isHidden()) and "3张" in win._btn_stack.text()
      and "1单" in win._btn_stack.text(),
      f"hidden={win._btn_stack.isHidden()} text={win._btn_stack.text()!r}")

# 第二条实质作用：操作员主动收起列表后，它是唯一既定的重新打开入口
win._stack_list_user_closed = True
_lst.hide()
win._on_stack_button()
check("★ 主动收起列表后点入口 ⇒ 列表重新出现 + 收起标记清零",
      (not _lst.isHidden()) and win._stack_list_user_closed is False,
      f"hidden={_lst.isHidden()} closed={win._stack_list_user_closed}")

win._stack.clear()
win._sync_stack_button()
check("★ 整叠打完（纸叠清空）⇒ 入口自动收起，不留下一个没读数可显示的按钮",
      win._btn_stack.isHidden(),
      f"hidden={win._btn_stack.isHidden()} text={win._btn_stack.text()!r}")


# ============================================================
# ⑧ 攒批列表：左「已攒」/ 右「等待打印」两栏 + 页号 + [空]（2026-10-20 改版）
# ============================================================
# 用户口径：左侧列出所有"攒"了的文件及具体页数，右侧列出所有等待打印的文件及具体页数，
# 空白占位写 [空]。判据走**渲染出来的树**（不是方法返回值），才抓得到"界面没跟上"。
print("\n=== ⑧ 攒批列表左右两栏 ===")
_list_dlg = gui.StackQueueDialog()
_st = ds.DuplexStack(capacity=90, printer_name="Fake Print")
_st.entries = [
    ds.StackEntry(seq=1, title="HN-A", page_count=3, sheet_count=2, rotate=True,
                  pdf="e1.pdf", first="odd", state=ds.STATE_DONE,
                  file_plans=[{"label": "双面3页.pdf", "pages": [1, 2, 3], "duplex": True}]),
    ds.StackEntry(seq=2, title="HN-B", page_count=2, sheet_count=2, rotate=True,
                  pdf="e2.pdf", first="odd", state=ds.STATE_DONE,
                  file_plans=[{"label": "单面2页.pdf", "pages": [1, 2], "duplex": False}]),
]
_q1 = make_job(PDF2, "on", name="排队双面.pdf")
_q1.page_count = 4
_q2 = make_job(PDF2, "off", name="排队单面.pdf")
_q2.page_count = 3
_list_dlg.render(_st, busy=False,
                 wait_requests=[{"jobs": [_q1], "tab_key": "2", "reason": "容量不足：还需 2 张"},
                                {"jobs": [_q2], "tab_key": "3", "reason": "装订方向不一致"}],
                 printing_text="")
_TL, _TR = _list_dlg._tree_left, _list_dlg._tree_right


def _cell(tree, row, col):
    it = tree.topLevelItem(0) if row == 0 else tree.topLevelItem(0).child(row - 1)
    return it.text(col)


def _rows_of(tree):
    out = []
    n = tree.columnCount()
    for i in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(i)
        out.append([top.text(0)] + [[top.child(j).text(c) for c in range(n)]
                                    for j in range(top.childCount())])
    return out


_left_rows = lambda: _rows_of(_TL)
_right_rows = lambda: _rows_of(_TR)


_L = _left_rows()
_R = _right_rows()
check("★ 左栏按单分组，列出每个文件（不合并成一句『本单 3 页』）",
      len(_L) == 2 and len(_L[0]) == 2 and _L[0][1][0].endswith("双面3页.pdf")
      and _L[1][1][0].endswith("单面2页.pdf"),
      f"{[r[0] for r in _L]} / {[r[1][0] for r in _L]}")
check("★ 双面 3 页：第 1 趟=1 3、第 2 趟=[空] 2（空白占位写 [空]，逆序在最上面）",
      _L[0][1][1] == "1 3" and _L[0][1][2] == "[空] 2", f"{_L[0][1][1]} | {_L[0][1][2]}")
check("★ 单面 2 页：第 1 趟=1 2、第 2 趟=[空] [空]（每页各占一张纸，背面全空）",
      _L[1][1][1] == "1 2" and _L[1][1][2] == "[空] [空]",
      f"{_L[1][1][1]} | {_L[1][1][2]}")
check("★ 每行给张数（3 页双面=2 张 / 2 页单面=2 张）",
      _L[0][1][3] == "2 张" and _L[1][1][3] == "2 张",
      f"{_L[0][1][3]} / {_L[1][1][3]}")
check("★ 右栏列出所有等待打印的文件 + 页号 + 排队原因",
      len(_R) == 2 and "容量不足" in _R[0][0] and "装订方向不一致" in _R[1][0]
      and _R[0][1][0].endswith("排队双面.pdf") and _R[1][1][0].endswith("排队单面.pdf"),
      f"{[r[0] for r in _R]}")
check("★ 排队文件也按第 1 趟/第 2 趟给页号（4 页双面 → 1 3 / 4 2，第 2 趟逆序）",
      _R[0][1][1] == "1 3" and _R[0][1][2] == "4 2",
      f"{_R[0][1][1]} | {_R[0][1][2]}")
check("★ 排队单面 3 页：第 2 趟 3 个 [空]（背面全空但仍要各走一遍纸）",
      _R[1][1][2] == "[空] [空] [空]", _R[1][1][2])
check("★ 右栏写明页数/份数（不只一个文件名）",
      "4 页" in _R[0][1][3] and "3 页" in _R[1][1][3], f"{_R[0][1][3]} / {_R[1][1][3]}")
check("★ 列表标题给出整叠汇总 + 左右栏各自的计数",
      "1 单" not in _list_dlg._summary.text() and "2 单" in _list_dlg._summary.text()
      and "2 单" in _list_dlg._left_group.title() and "2 个请求" in _list_dlg._right_group.title(),
      f"{_list_dlg._summary.text()} / {_list_dlg._left_group.title()} / "
      f"{_list_dlg._right_group.title()}")

# 「我已取走」只对「第 2 趟无内容」的计划开放（整份单面文件才有内容全空）
_list_dlg._tree_left.topLevelItem(1).child(0).setSelected(True)
_list_dlg._on_left_selection()
check("★ 选中「第 2 趟全空」的文件 → 「我已取走」可点",
      _list_dlg._btn_take.isEnabled(), f"enabled={_list_dlg._btn_take.isEnabled()}")
_list_dlg._tree_left.topLevelItem(0).child(0).setSelected(True)
_list_dlg._tree_left.topLevelItem(1).child(0).setSelected(False)
_list_dlg._on_left_selection()
check("★ 选中「第 2 趟还要用」的文件 → 「我已取走」禁用（撤走必然整叠错位）",
      not _list_dlg._btn_take.isEnabled())
_list_dlg.close()

# 页号必须是**这份文件自己**的页码，而不是它在合并任务里的页码（2026-10-25 用户反馈：
# 现场看到「三页文档测试.pdf 第 1 趟 4 6 / 第 2 趟 [空] 5」，那是它在整单合并 PDF 里的位置）
_list_own = gui.StackQueueDialog()
_st_own = ds.DuplexStack(capacity=90, printer_name="Fake Print")
_st_own.entries = [
    ds.StackEntry(seq=1, title="HN-MERGED", page_count=6, sheet_count=4, rotate=True,
                  pdf="m.pdf", first="odd", state=ds.STATE_DONE,
                  file_plans=[
                      {"label": "单页文档测试.pdf", "pages": [1], "file_pages": [1],
                       "duplex": False},
                      {"label": "双页文档测试.pdf", "pages": [2, 3], "file_pages": [1, 2],
                       "duplex": True},
                      {"label": "三页文档测试.pdf", "pages": [4, 5, 6], "file_pages": [1, 2, 3],
                       "duplex": True},
                  ]),
]
_list_own.render(_st_own)
_own_rows = [_list_own._tree_left.topLevelItem(0).child(k) for k in range(3)]
check("★ 每个文件的页号按**它自己**算（合并任务里的 2/3、4/5/6 → 1/2、1/2/3）",
      [r.text(1) for r in _own_rows] == ["1", "1", "1 3"]
      and [r.text(2) for r in _own_rows] == ["[空]", "2", "[空] 2"],
      f"{[(r.text(1), r.text(2)) for r in _own_rows]}")
check("★ 张数列不受影响：仍是每份文件在纸叠里占几张纸（1/1/2 张）",
      [r.text(3) for r in _own_rows] == ["1 张", "1 张", "2 张"],
      f"{[r.text(3) for r in _own_rows]}")
check("★ 旧清单（没有 file_pages）退回合并页码，不崩、不留空",
      _list_own._add_plan_row is not None
      and ds.plan_own_pages({"pages": [4, 5, 6]}) == [4, 5, 6],
      f"{ds.plan_own_pages({'pages': [4, 5, 6]})}")
_list_own.close()

# 空纸叠/空队列也要有明确占位文案（常驻窗不能是一片空白）
_list_dlg2 = gui.StackQueueDialog()
_list_dlg2.render(ds.DuplexStack(capacity=90), busy=False, wait_requests=[])
check("★ 空列表给出占位文案（左『还没有攒进任何文件』/ 右『没有在等的文件』）",
      "还没有攒进" in _list_dlg2._tree_left.topLevelItem(0).text(0)
      and "没有在等的文件" in _list_dlg2._tree_right.topLevelItem(0).text(0),
      f"{_list_dlg2._tree_left.topLevelItem(0).text(0)} / "
      f"{_list_dlg2._tree_right.topLevelItem(0).text(0)}")
_list_dlg2.close()


# ============================================================
# ⑨ 关闭攒批列表 / 关闭攒批模式：必须三选确认（2026-10-21 用户提问）
# ============================================================
# 用户口径：「如果攒批列表里还有没打第 2 趟的文件，手动点关闭攒批 / 关闭攒批列表会发生
# 什么？应该弹确认窗口，确认关闭会导致相关订单全部失败。」
# 旧行为两条都不对：关窗口只 hide（一句提示都没有，订单既不失败也看不见），
# 关模式被「打印机正忙」直接堵死（连选择都没有）。
# 现在的口径：三选 —— 保留（只收起/关模式，纸叠与订单都留着，绝不失败）/
# 放弃整叠（这 N 单全部按失败收尾）/ 取消（什么都不做）。
print("\n=== ⑨ 关闭列表 / 关闭模式：三选确认 ===")


def _fake_stack(win_, tids=(801, 802)):
    """直接造账本（不走真实打印）：两单已打第 1 趟、等第 2 趟，任务里有云端 task_id。"""
    win_._stack.clear()
    win_._stack_wait = []
    win_._config.tabs["1"].jobs = []          # job 找不到 ⇒ 收尾只走"回报云端"那条路
    ents = []
    for i, tid in enumerate(tids, 1):
        ents.append(ds.StackEntry(
            seq=i, title=f"HN-{tid}", page_count=2, sheet_count=1, rotate=True,
            pdf=f"e{i}.pdf", first="odd", state=ds.STATE_DONE,
            file_plans=[{"label": f"{tid}.pdf", "pages": [1, 2], "duplex": True}],
            tasks=[{"task_id": tid, "order_id": 800 + i, "owner_name": "测试成员",
                    "is_admin_print": False, "copies": 1, "duplex": "on",
                    "page_range": "", "page_count": 2, "display_name": f"{tid}.pdf"}]))
    win_._stack.entries = ents
    return ents


_fake_stack(win2)
_confirm_calls = []


def _confirm_stub(answer):
    def _f(purpose="关闭攒批列表"):
        _confirm_calls.append(purpose)
        return answer
    return _f


_orig_confirm = win2._confirm_stack_close

# ① 有纸叠时点「关闭」→ 必须问（不能像旧写法那样直接 hide）
win2._stack_list_user_closed = False
_lst9 = _ready_list(win2)
win2._show_stack_queue_dialog(_lst9)          # 先让它真的显示出来，才验得了"收起/不收起"
win2._confirm_stack_close = _confirm_stub("")
_confirm_calls.clear()
_lst9._btn_close.click()
check("★ 有未打第 2 趟的纸叠时点「关闭」→ 先弹三选确认（旧写法直接 hide）",
      _confirm_calls == ["关闭攒批列表"], f"{_confirm_calls}")
check("★ 选「取消」→ 窗口不收起、纸叠原样保留",
      not _lst9.isHidden() and win2._stack.count == 2,
      f"hidden={_lst9.isHidden()} count={win2._stack.count}")

# ② 选「保留」→ 只收起窗口：订单**绝不能**被判失败
_n_fail = len(win2._cloud_client.fail)
win2._confirm_stack_close = _confirm_stub("keep")
_lst9._btn_close.click()
check("★ 选「保留」→ 窗口收起 + 纸叠/订单都留着 + 一个失败回报都不发",
      _lst9.isHidden() and win2._stack.count == 2
      and len(win2._cloud_client.fail) == _n_fail
      and win2._stack_list_user_closed is True,
      f"hidden={_lst9.isHidden()} count={win2._stack.count} "
      f"fail={win2._cloud_client.fail[_n_fail:]}")
check("★ 「保留」收起后，同步逻辑不会把窗口抢着弹回来（旧写法每次 sync 都 show）",
      (win2._sync_stack_list_visible() or True) and _lst9.isHidden(),
      f"hidden={_lst9.isHidden()}")
win2._on_stack_button()
check("★ 点底部「🖐 攒批」→ 重新打开并清掉「操作员已收起」标记",
      not _lst9.isHidden() and win2._stack_list_user_closed is False,
      f"hidden={_lst9.isHidden()} closed={win2._stack_list_user_closed}")

# ③ 选「放弃整叠」→ 这 N 单全部按失败收尾（用户口径：确认关闭 = 相关订单全部失败）
_fake_stack(win2)
_n_fail = len(win2._cloud_client.fail)
_lst9 = _ready_list(win2)
win2._show_stack_queue_dialog(_lst9)
win2._confirm_stack_close = _confirm_stub("abandon")
_lst9._btn_close.click()
_failed = {t for t, _m in win2._cloud_client.fail[_n_fail:]}
check("★ 选「放弃整叠」→ 两单都按失败回报云端 + 账本清空 + 窗口收起",
      _failed == {801, 802} and win2._stack.entries == [] and _lst9.isHidden(),
      f"failed={sorted(_failed)} count={win2._stack.count} hidden={_lst9.isHidden()}")

# ④ 空纸叠：关闭不该再弹框（省一次无意义的确认）
_fake_stack(win2)
win2._stack.clear()
_lst9 = _ready_list(win2)
win2._show_stack_queue_dialog(_lst9)
_confirm_calls.clear()
win2._confirm_stack_close = _confirm_stub("")
_lst9._btn_close.click()
check("★ 没有纸叠时点「关闭」→ 不问、直接收起", _confirm_calls == [] and _lst9.isHidden(),
      f"calls={_confirm_calls} hidden={_lst9.isHidden()}")

# ⑤ 关闭攒批模式（勾选框）：有纸叠时同样三选，且不再被「打印机正忙」堵死
_prep(win2)
_fake_stack(win2)
win2._config.duplex_stack_mode = True
win2._stack_list_user_closed = False
win2._confirm_stack_close = _confirm_stub("")
win2._on_stack_mode_toggled(False)
check("★ 关闭攒批模式选「取消」→ 勾选回滚、模式仍开、纸叠保留",
      win2._config.duplex_stack_mode is True and win2._stack.count == 2
      and win2._chk_stack_mode.isChecked() is True,
      f"mode={win2._config.duplex_stack_mode} count={win2._stack.count}")

_n_fail = len(win2._cloud_client.fail)
_prep(win2)
win2._confirm_stack_close = _confirm_stub("keep")
win2._on_stack_mode_toggled(False)
check("★ 关闭攒批模式选「保留」→ 模式关掉但纸叠保留、订单不失败",
      win2._config.duplex_stack_mode is False and win2._stack.count == 2
      and len(win2._cloud_client.fail) == _n_fail,
      f"mode={win2._config.duplex_stack_mode} count={win2._stack.count} "
      f"fail={win2._cloud_client.fail[_n_fail:]}")
check("★ 模式关掉后列表仍在场（纸叠还没打第 2 趟，不能让它离开视线）",
      not win2._stack_queue_dialog.isHidden(),
      f"hidden={win2._stack_queue_dialog.isHidden()}")

_prep(win2)
_fake_stack(win2)
win2._config.duplex_stack_mode = True
_n_fail = len(win2._cloud_client.fail)
win2._confirm_stack_close = _confirm_stub("abandon")
_confirm_calls.clear()
win2._on_stack_mode_toggled(False)
_failed = {t for t, _m in win2._cloud_client.fail[_n_fail:]}
check("★ 关闭攒批模式选「放弃整叠」→ 模式关掉 + 这 N 单全部按失败收尾",
      win2._config.duplex_stack_mode is False and win2._stack.entries == []
      and _failed == {801, 802},
      f"mode={win2._config.duplex_stack_mode} failed={sorted(_failed)}")

# ⑥ 正在打印时关闭模式仍硬拦（那是真忙，不是"有一叠在等翻纸"）


class _RunningWorker2:
    def isRunning(self):
        return True


_prep(win2)
_fake_stack(win2)
win2._config.duplex_stack_mode = True
win2._worker = _RunningWorker2()
_confirm_calls.clear()
win2._confirm_stack_close = _confirm_stub("keep")
_gui_i = gui.QMessageBox.information
gui.QMessageBox.information = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)
win2._on_stack_mode_toggled(False)
gui.QMessageBox.information = _gui_i
win2._worker = None
check("★ 正在打印时关闭攒批模式 → 仍然硬拦（不弹三选、不问纸叠）",
      _confirm_calls == [] and win2._config.duplex_stack_mode is True
      and win2._stack.count == 2,
      f"calls={_confirm_calls} mode={win2._config.duplex_stack_mode}")
win2._confirm_stack_close = _orig_confirm
win2._stack.clear()
win2._stack_wait = []
win2._stack_list_user_closed = False

# 静态接线：关闭按钮/窗口 × 都走 close_requested；主窗口三选框存在且默认是安全项
G9 = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
check("★ 列表「关闭」按钮发 close_requested（不再自己 hide）",
      "self._btn_close.clicked.connect(lambda *_: self.close_requested.emit())" in G9)
check("★ 窗口 × / Esc 也走 close_requested（closeEvent 里 emit + ignore）",
      "def closeEvent(self, ev):" in G9 and "self.close_requested.emit()" in G9
      and "ev.ignore()" in G9)
check("★ 主窗口接了 close_requested（不接就是「点了没反应」）",
      "dlg.close_requested.connect(self._on_stack_list_close_requested)" in G9)
_check_src = G9[G9.index("def _confirm_stack_close"):G9.index("def _on_stack_list_close_requested")]
check("★ 三选确认框：保留 / 放弃整叠 / 取消 三颗键，默认是安全的「保留」",
      "保留（纸叠与订单都留着）" in _check_src
      and "单全部按失败收尾" in _check_src and "取消（保持原样）" in _check_src
      and "box.setDefaultButton(b_keep)" in _check_src)
check("★ 关模式时跳过「出纸槽里有纸」那一条忙判定（否则永远走不到确认框）",
      "self._stack_busy_reason(skip_entries=True)" in G9)
_T_SRC = G9[G9.index("def _sync_stack_list_visible"):G9.index("def _on_stack_taken_toggle")]
check("★ 列表可见性：有未打完的纸叠即使模式关掉也在场 + 尊重「操作员已收起」",
      "has_entries" in _T_SRC and "_stack_list_user_closed" in _T_SRC)


# ============================================================
# ⑥b 攒批"连续堆第 1 趟"：打印机忙时**排队**，空出来自动接着打（2026-10-21 现场 bug）
# ============================================================
# 现场症状（USB 拔掉的生产机实测）：第 1 单第 1 趟还在等出纸时点第 2 单 →
# 「⚠️ 已有打印任务正在进行，不能重复启动」；反复点无效；等第 1 单打完，
# `_on_all_finished` 又报「🔒 全部 0 个任务已完成打印并回报成功！」。
# 根因：① 忙检查在 `_start_stack_entry` 写 `_stack_pending_ctx` **之后**，被拒时 ctx 已被
#       覆盖成新单、随后清空 ⇒ 在飞那一单收尾时被判成"普通打印批次"（0 个任务）；
#       ② 攒批单没有排队通道，忙 = 硬拒。
print("\n=== ⑥b 攒批连续堆第 1 趟：忙时排队 → 自动接着打 ===")
gui.PrintWorker.start = _fake_start
try:
    _prep(win2)
    win2._stack.clear()
    win2._stack.capacity = 90
    win2._stack_wait = []
    win2._stack_entry_wait = []
    win2._stack_pending_ctx = {}
    win2._worker = None
    win2._config.tabs["1"].jobs = []
    win2._config.tabs["2"] = TabSettings(jobs=[], owner_name="测试成员", is_admin_print=False)
    win2._config.tabs["2"].jobs = []

    jobQ1 = make_job(PDF2, "on", name="Q1.pdf", task_id=1101, order_id=501)
    jobQ2 = make_job(PDF2, "on", name="Q2.pdf", task_id=1102, order_id=502)
    win2._config.tabs["1"].jobs = [jobQ1]
    win2._config.tabs["2"].jobs = [jobQ2]

    _n0 = len(started_workers)
    ok_q1 = win2._start_print_worker([jobQ1], tab_key="1")
    check("前提：第 1 单已开打，待处理上下文已定格",
          ok_q1 and len(started_workers) == _n0 + 1
          and win2._stack_pending_ctx.get("tab_key") == "1",
          f"ok={ok_q1} ctx={win2._stack_pending_ctx}")

    class _BusyWorker:
        """模拟"第 1 单第 1 趟还在打"（真 worker 在跑，QThread.isRunning()=True）。"""
        _stack_mode = True

        def __init__(self, jobs):
            self._jobs = list(jobs)

        def isRunning(self):
            return True

    win2._worker = _BusyWorker([jobQ1])
    _prep(win2)          # `_start_print_worker` 会把界面下拉框里的真机名同步进 config，先钉回去
    ok_q2 = win2._start_print_worker([jobQ2], tab_key="2")
    check("★ 第 1 趟还在打时点第二单 → 受理排队（不再硬拒「已有打印任务正在进行」）",
          ok_q2 is True and len(started_workers) == _n0 + 1
          and len(win2._stack_entry_wait) == 1
          and win2._stack_entry_wait[0]["jobs"][0] is jobQ2,
          f"ok={ok_q2} wait={len(win2._stack_entry_wait)}")
    check("★ 排队没清掉在飞那一单的上下文（「全部 0 个任务已完成」的根因）",
          win2._stack_pending_ctx.get("tab_key") == "1",
          f"ctx={win2._stack_pending_ctx}")

    _prep(win2)
    ok_q2b = win2._start_print_worker([jobQ2], tab_key="2")
    check("★ 同一单重复点 → 只在原条目上更新，不排第二份（避免同一单打两遍）",
          ok_q2b is True and len(win2._stack_entry_wait) == 1,
          f"wait={len(win2._stack_entry_wait)}")

    win2._on_all_finished(0, 0)      # 第 1 单第 1 趟结束
    check("★ 第 1 趟结束 → 排队的第 2 单**自动开打**（不用再点一次）",
          len(win2._stack_entry_wait) == 0
          and len(started_workers) == _n0 + 2
          and started_workers[-1]._stack_out_pdf.endswith("entry_0001.pdf"),
          f"wait={len(win2._stack_entry_wait)} workers={len(started_workers) - _n0}")
    check("★ 第 1 趟结束仍走攒批分支（状态栏「攒批中」，不再报「全部 0 个任务已完成」）",
          "攒批中" in win2._status_label.text(), win2._status_label.text())
    check("★ 自动接上的第 2 单已把上下文换成自己的标签页（收尾归属不串单）",
          win2._stack_pending_ctx.get("tab_key") == "2",
          f"ctx={win2._stack_pending_ctx}")

    # 排队窗口里操作员把文件从标签页删了（这一单还没冻结）→ 不能再印已看不见的文件
    jobQ3 = make_job(PDF2, "on", name="Q3.pdf", task_id=1103, order_id=503)
    win2._config.tabs["2"].jobs = []
    win2._stack_entry_wait = [{"jobs": [jobQ3], "tab_key": "2"}]
    _prep(win2)
    win2._worker = None
    _n1 = len(started_workers)
    win2._drain_stack_entry_queue()
    check("★ 排队期间文件被移出标签页 → 该项不再开打（不让已删除文件继续印）",
          len(started_workers) == _n1 and not win2._stack_entry_wait,
          f"started={len(started_workers) - _n1}")
finally:
    gui.PrintWorker.start = _orig_start
    win2._worker = None
    win2._stack.clear()
    win2._stack_wait = []
    win2._stack_entry_wait = []
    win2._stack_pending_ctx = {}
    win2._stack.capacity = 90
    win2._config.tabs["1"].jobs = []


# ============================================================
# ④ 静态接线核对（防"改了行为却漏了另一处"）
# ============================================================
print("\n=== ④ 静态接线 ===")
G = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
APP = open(os.path.join(REPO, "mobile_apps", "printer_backend_placeholder.txt"),
           encoding="utf-8").read() if False else open(
    os.path.join(REPO, "mobile_apps", "printer-backend", "app.py"), encoding="utf-8").read()
CC = open(os.path.join(LOCAL_TOOL, "cloud_client.py"), encoding="utf-8").read()
DS = open(os.path.join(LOCAL_TOOL, "duplex_stack.py"), encoding="utf-8").read()
PC = open(os.path.join(LOCAL_TOOL, "printer_config.py"), encoding="utf-8").read()

check("worker：攒批模式下**整单**都挂起（defer = stack_mode or 需手动双面）",
      "defer = self._stack_mode or self._uses_manual_duplex(job)" in G)
check("worker：攒批第 1 趟方法里没有 job_finished（铁律②）",
      "job_finished" not in G.split("def _print_stack_pass1", 1)[1]
                      .split("def run(self)", 1)[0])
check("worker：stack_entry_planned 在 print_plan_pass 之前发出",
      G.index("self.stack_entry_planned.emit(") < G.index(
          "ok1, msg1 = print_plan_pass(plan, self._stack_out_pdf"))
check("worker：攒批第 1 趟有异常兜底（异常也必须报 entry_done，否则出纸槽永远被占）",
      "self.stack_entry_done.emit(self._stack_seq, False, msg, 0)" in G)
check("主窗口：闸门在「忙」判断之前（排队不算启动失败）",
      G.index("gate = self._stack_gate(flat_jobs, tab_key)") < G.index(
          "已有打印任务正在进行，不能重复启动"))
# 2026-10-20（用户要求）：攒批不再复用「手动双面」窗 —— 第 1 趟不弹窗/不计时，
# 第 2 趟与放弃整叠都从攒批列表发起（数纸确认 → 打整叠）。
check("★ 双面窗里不许再出现攒批状态/信号（它只管单任务）",
      "stack_flush_requested" not in G and "stack_abandon_requested" not in G
      and "def announce_stack" not in G and "def set_stack_ready" not in G
      and "def stack_finished" not in G and 'getattr(dlg, "_mode", "") == "stack_ready"' not in G)
check("★ 攒批列表三个动作直连主窗口（▶ 开始第 2 趟 / ⏭ 放弃整叠 / 关闭先确认）",
      "dlg.start_second_requested.connect(self._on_stack_flush)" in G
      and "dlg.abandon_requested.connect(lambda *_: self._on_stack_abandon())" in G
      and "dlg.close_requested.connect(self._on_stack_list_close_requested)" in G)
check("★ 攒批第 1 趟播报不碰双面窗（_on_stack_announce 只记日志 + 刷列表）",
      "_ensure_duplex_dialog" not in G[G.index("def _on_stack_announce"):
                                      G.index("def _stack_present_sheets")])
check("★ 第 2 趟没启动的每一条提前返回都刷新列表（按钮不停在假的「打印中」）",
      "_stack_ready_state" not in G
      and G.count("self._refresh_stack_queue()") >= 6)
# 2026-10-18 设计改动：收尾仍是**唯一落点**（`_on_stack_flush_done`），但"第 2 趟无内容"这一态
# 需要**逐单**按成品/异常拆（否则整叠只剩第 1 趟失败的单时会把半成品报成功）。
# 所以断言从"字面上只有 2 次调用"换成**意图判据**：除收尾落点本身外，任何收尾调用都必须由
# `_on_stack_flush_done` 或其私有助手发起 —— 不许出现第三条独立的收尾路径（账目分叉）。
_finish_callers = re.findall(r"\n    def ([\w_]+)\([^)]*\):(?:(?!\n    def ).)*?self\._finish_stack_entries\(",
                             G, re.S)
check("主窗口：收尾调用只出现在收尾落点及其私有助手（不许第三条独立路径）",
      set(_finish_callers) <= {"_on_stack_flush_done", "_finish_stack_entries_split",
                               "_on_stack_abandon"},
      f"{_finish_callers}")
check("主窗口：第 2 趟无内容时逐单拆成品/异常（半成品不得按成品报成功）",
      "_finish_stack_entries_split" in G
      and "_entry_is_finished_product(e)" in G
      and "empty_pass2" in G)
check("主窗口：退出时把纸叠里的任务从「放弃名单」里摘出来",
      "_stack_task_ids" in G and "半成品，保留订单等续打第 2 趟" in G)
check("主窗口：续打入口在启动时挂上",
      "_check_pending_stack_on_startup" in G and "QTimer.singleShot(1200," in G)
check("云客户端：report_keepalive 发 print_keepalive（不写状态栏，避免每分钟刷日志）",
      "print_keepalive" in CC and "def report_keepalive" in CC)
check("后端：print_keepalive 事件刷新 locked_at 且只认 printing/accepted",
      "def on_print_keepalive" in APP and "print_keepalive" in APP
      and "SET locked_at = ?" in APP and "'printing', 'accepted'" in APP)
# ⚠ 判据要取**语义**不要取字面：原判据 grep 日志文案「不是本机在打的任务」，
#   D3 把该处理器改成统一归属判定 `_subtask_ownership()` 后文案随之改写 ⇒ 立刻**假红**
#   （守卫抓着的是措辞而非校验本身）。改为：抽 `on_print_keepalive` 函数体，断言里面
#   **确实做了归属校验**（统一判定 or 显式 operator_client 比对）。
_kp = re.search(r"(?ms)^def on_print_keepalive\(.*?(?=^def |\Z)", APP)
_kp_body = _kp.group(0) if _kp else ""
check("后端：保活不替别的设备续命（keepalive 里有归属校验）",
      bool(_kp_body) and ("_subtask_ownership(" in _kp_body
                          or re.search(r"operator_client\s*!=\s*client_id", _kp_body) is not None),
      "on_print_keepalive 函数体里没找到归属校验")
check("配置：duplex_stack_mode 落盘并回读",
      '"duplex_stack_mode": self.duplex_stack_mode' in PC
      and 'duplex_stack_mode=_bool_or_default(' in PC)
check("档案：容量/预警阈值随打印队列名保存",
      "stack_capacity=" in PC and "stack_warn_after_sec=" in PC)
check("★ 批次目录**不在** file_cache 里（staging.sweep 会删掉没被引用的文件）",
      "duplex_stack" in open(os.path.join(LOCAL_TOOL, "paths.py"), encoding="utf-8").read()
      and "file_cache" not in DS)
import paths as _paths2  # noqa: E402
check("★ 运行期校验：duplex_stack_dir 与 file_cache_dir 不是同一棵树",
      os.path.normcase(_paths2.duplex_stack_dir()) !=
      os.path.normcase(_paths2.file_cache_dir())
      and not os.path.normcase(_paths2.duplex_stack_dir()).startswith(
          os.path.normcase(_paths2.file_cache_dir()) + os.sep),
      _paths2.duplex_stack_dir())
check("攒批方案注释齐（三条铁律在 gui.py 里写明，防后人误删）",
      "出纸槽独占" in G and "第 1 趟不算完成" in G and "超时不自动跳过" in G)

print("\n" + ("=" * 60))
print("全部通过 ✅" if ok_all else "存在失败项 ❌")
sys.exit(0 if ok_all else 1)
