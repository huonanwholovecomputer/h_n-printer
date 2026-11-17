# -*- coding: utf-8 -*-
"""L2-3：手动双面第 1 趟**中途失败**必须落「半成品补救」台账。

现状 bug（gui.py:5115 一带）：第 1 趟失败只把 _tray_sheets 加一笔、发 log，然后
return False —— 出纸槽里可能已经压着本批前几张纸，但没有任何持久记录：主线程按
「失败」上报，操作员下次开打时程序以为出纸槽是空的，整叠错位。

修法口径（物理事实决定，不看 UI 文案）：
  · pdf_printer.is_partial(msg1) 为真（PARTIAL_MARKER / MID_PRINT_MARKER，含义是
    StartPage 已执行 = 真出过纸）⇒ 这叠纸页序不可信、不能补打第 2 趟，只能整单
    重打/作废 ⇒ 落台账 stage=STAGE_PASS2_PARTIAL（补救窗对它的按钮就是这两个）。
  · 没有 marker（一张都没出）⇒ 纸叠没被动过，按普通失败上报重打即可，不许塞台账
    （多记一条会让操作员去翻一个空的出纸槽）。

判据（实现改坏就红）：
  · 删掉 _keep_stall( 调用 → ②-b/②-c 红
  · 不判 is_partial、一律记台账 → ③-b/③-c 红
  · 台账 stage 写 pass1_done（会给「补打第 2 趟」按钮）→ ②-d 红

跑法：
    PYTHONPATH= PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \
        C:/Users/Administrator/AppData/Local/Programs/Python/Python312/python.exe \
        other/tests/verify_manual_duplex_pass1_stall.py
"""
import os
import sys
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="hn_duplex_p1_stall_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

_ok = []
_fail = []


def check(name, cond, detail=""):
    if cond:
        _ok.append(name)
        print(f"  [PASS] {name}")
    else:
        _fail.append(name)
        print(f"  [FAIL] {name}" + (f"  ← {detail}" if detail else ""))


from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui  # noqa: E402
import paths  # noqa: E402
import pdf_printer  # noqa: E402
import duplex_remedy  # noqa: E402
from printer_config import PrintJob, PrinterProfile  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.Yes)
gui.QMessageBox.warning = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)
gui.QMessageBox.information = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)


def make_pdf(name, pages):
    path = os.path.join(_SANDBOX, name)
    c = canvas.Canvas(path, pagesize=A4)
    for i in range(pages):
        c.drawString(72, 760, f"{name} page {i + 1}")
        c.showPage()
    c.save()
    return path


PDF6 = make_pdf("p1fail6.pdf", 6)      # 6 页双面 → 3 张纸
MANUAL = PrinterProfile(auto_duplex=False, capability_source="manual", manual_duplex=True,
                        first="odd", reverse_second=True, second_rotate_180=True,
                        verified_at="2026-10-12 22:14:49", verified_by="丙成员")


def read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


SRC = read(os.path.join(LOCAL_TOOL, "gui.py"))
_BATCH = SRC.split("def _print_manual_batch(", 1)[1].split("\n    def ", 1)[0]
_FAIL1 = _BATCH.split("if not ok1:", 1)[1].split("# 第 1 趟的纸已经", 1)[0] \
    if "if not ok1:" in _BATCH else ""

print("── ① 静态：第 1 趟失败分支必须判 is_partial 并落台账 ──")
check("①-a 失败分支里调了 _keep_stall(", "_keep_stall(" in _FAIL1, _FAIL1[:160])
check("①-b 用 is_partial(msg1) 判「有没有真出过纸」", "is_partial(msg1)" in _FAIL1)
check("①-c 中途失败记 pass2_partial（这叠纸不能补打第 2 趟）",
      "STAGE_PASS2_PARTIAL" in _FAIL1)
check("①-d 仍然先按既有口径把出纸槽张数算进去（_tray_sheets）",
      "_tray_sheets" in _FAIL1)
check("①-e 超时不自动跳过的那道闸门还在（_ask_operator_to_flip 仍是阻塞判断）",
      "if not self._ask_operator_to_flip(" in _BATCH)

print("── ② 运行期：第 1 趟中途失败（带 PARTIAL_MARKER）→ 落台账、发 job_stalled ──")
_orig = {n: getattr(gui, n) for n in
         ("print_pdf", "print_plan_pass", "wait_for_output", "get_converter",
          "count_pending_print_jobs")}
_orig_wait_pass1 = gui.PrintWorker._wait_pass1_output

_pass1_msg = pdf_printer.PARTIAL_MARKER + "卡纸（第 1 张之后没出来）"


def _print_plan_pass(plan, path, printer, which, **kw):
    if which == 1:
        return False, _pass1_msg
    return True, f"桩：第 {which} 趟"


def _run_worker():
    w = gui.PrintWorker(jobs=[PrintJob(file_path=PDF6, copies=1, duplex="on",
                                       duplex_mode="long-edge", page_count=6,
                                       display_name="p1fail.pdf")],
                        printer_name="Fake Print", duplex_mode="long-edge",
                        keep_temp_pdf=False, render_dpi=200,
                        manual_duplex_profile=MANUAL, cover_page=False)
    got = {"stalled": [], "finished": [], "logs": []}
    w.log_message.connect(got["logs"].append)
    w.job_stalled.connect(lambda idx, msg, rec: got["stalled"].append((idx, msg, rec)))
    w.job_finished.connect(lambda idx, ok, msg: got["finished"].append((idx, ok, msg)))
    w.run()
    return w, got


gui.print_pdf = lambda **kw: (True, "桩：单次提交")
gui.print_plan_pass = _print_plan_pass
gui.wait_for_output = lambda *a, **k: {"drained": True, "queue_seconds": 0.2,
                                       "margin_seconds": 0.0,
                                       "interrupted": False, "detail": ""}
gui.get_converter = lambda: object()
gui.count_pending_print_jobs = lambda printer="": 0
gui.PrintWorker._wait_pass1_output = lambda self, pages, timeout=120.0: {
    "drained": True, "queue_seconds": 0.2, "interrupted": False, "detail": ""}

try:
    _before = len(duplex_remedy.pending())
    w, got = _run_worker()
    check("②-a worker 收尾、不再把这一单按失败上报",
          len(got["finished"]) == 0, got["finished"])
    check("②-b 发了 job_stalled（主线程才能标 remedy_pending、不让云端重推）",
          len(got["stalled"]) == 1, got["stalled"])
    _rec = (got["stalled"] or [(0, "", {})])[0][2] or {}
    check("②-c 台账记录有 id（真落盘，不是内存字典）",
          bool(str(_rec.get("id") or "")), _rec.get("id"))
    _st = duplex_remedy.get(str(_rec.get("id") or "")) if _rec.get("id") else None
    check("②-d 落盘 stage=pass2_partial（补救窗只给整单重打/作废）",
          bool(_st) and _st.get("stage") == duplex_remedy.STAGE_PASS2_PARTIAL,
          (_st or {}).get("stage"))
    check("②-e 原因写明是「第 1 趟」失败（不是第 2 趟）",
          bool(_st) and "第 1 趟" in str(_st.get("reason") or ""),
          (_st or {}).get("reason"))
    check("②-f pending 增加 1 条", len(duplex_remedy.pending()) == _before + 1,
          f"{_before} -> {len(duplex_remedy.pending())}")
    check("②-g 台账 PDF 副本在（可整单重打）",
          bool(_st) and duplex_remedy.pdf_available(_st))
    check("②-h 给操作员的说明里有「整单重打 / 作废」",
          bool(_st) and "整单重打" in duplex_remedy.describe(_st))
    check("②-i job_stalled 的消息里标了「半成品补救」",
          "半成品补救" in ((got["stalled"] or [("", "", {})])[0][1] or ""),
          (got["stalled"] or [("", "", {})])[0][1])
finally:
    for k, v in _orig.items():
        setattr(gui, k, v)
    gui.PrintWorker._wait_pass1_output = _orig_wait_pass1

print("── ③ 反向：第 1 趟一张都没出（无 marker）→ 不许塞台账（照旧按失败上报）──")
_pass1_msg2 = "打印机未就绪（0x00000006）"


def _print_plan_pass2(plan, path, printer, which, **kw):
    if which == 1:
        return False, _pass1_msg2
    return True, f"桩：第 {which} 趟"


gui.print_pdf = lambda **kw: (True, "桩：单次提交")
gui.print_plan_pass = _print_plan_pass2
gui.wait_for_output = lambda *a, **k: {"drained": True, "queue_seconds": 0.2,
                                       "margin_seconds": 0.0,
                                       "interrupted": False, "detail": ""}
gui.get_converter = lambda: object()
gui.count_pending_print_jobs = lambda printer="": 0
gui.PrintWorker._wait_pass1_output = lambda self, pages, timeout=120.0: {
    "drained": True, "queue_seconds": 0.2, "interrupted": False, "detail": ""}
try:
    _before2 = len(duplex_remedy.pending())
    w2, got2 = _run_worker()
    check("③-a 没有台账记录（_stalled_record 为空）",
          getattr(w2, "_stalled_record", "missing") is None,
          getattr(w2, "_stalled_record", "missing"))
    check("③-b 没有发 job_stalled", got2["stalled"] == [], got2["stalled"])
    check("③-c 按普通失败上报（job_finished(False)）",
          len(got2["finished"]) == 1 and got2["finished"][0][1] is False,
          got2["finished"])
    check("③-d pending 没变", len(duplex_remedy.pending()) == _before2,
          f"{_before2} -> {len(duplex_remedy.pending())}")
    check("③-e 日志仍写明第 1 趟没打完", any("第 1 趟没打完" in s for s in got2["logs"]),
          got2["logs"][-3:])
finally:
    for k, v in _orig.items():
        setattr(gui, k, v)
    gui.PrintWorker._wait_pass1_output = _orig_wait_pass1

print()
print(f"PASS {len(_ok)}  FAIL {len(_fail)}")
if _fail:
    print("存在失败 ❌")
    sys.exit(1)
print("全部通过 ✅")
