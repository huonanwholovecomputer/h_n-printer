# -*- coding: utf-8 -*-
"""L2-4：补救台账落盘失败（save 返回 False）不许被吞掉。

体检结论：duplex_remedy.add() 里 `save(recs)` 的返回值被丢弃 ⇒ 补救计划没落盘，
add() 照样返回一条"看起来成功"的记录，调用方把 id 写进 job.remedy_pending；
下次启动读台账找不到这条 ⇒ 「半成品补救」窗里没有它、纸却已经算"记过了"。

判据（实现改坏就红）：
  · add() 不看 save() 返回值 → ①-a/①-b 红
  · _keep_stack_stall() 不检查 add() 结果 → ③ 红
  · _on_stack_flush_done() 在没落盘时仍写 remedy_pending → ④-b 红

跑法：
    PYTHONPATH= PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \
        C:/Users/Administrator/AppData/Local/Programs/Python/Python312/python.exe \
        other/tests/verify_remedy_save_failure.py
"""
import os
import sys
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="hn_remedy_save_")
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
import duplex_stack  # noqa: E402
import duplex_remedy  # noqa: E402


def make_pdf(path, pages):
    from reportlab.pdfgen import canvas as _canvas
    c = _canvas.Canvas(path, pagesize=(595, 842))
    for i in range(pages):
        c.setFont("Helvetica", 20)
        c.drawString(72, 700, f"page {i + 1}")
        c.showPage()
    c.save()
    return path


def mk_entry(seq, title, pdf, n_pages, state, task_id=0):
    return duplex_stack.StackEntry(
        seq=seq, title=title, tab_key="1", order_id=0,
        order_number=f"HN{seq:04d}", page_count=n_pages,
        sheet_count=(n_pages + 1) // 2, state=state, rotate=True,
        created_at="2026-11-17 10:00:00", pdf=os.path.basename(pdf),
        file_plans=[{"label": title, "pages": list(range(1, n_pages + 1)),
                     "duplex": True, "second_rotate": True}],
        tasks=[{"task_id": task_id, "display_name": title, "file_path": "",
                "copies": 1, "duplex": "on", "page_range": "",
                "page_count": n_pages, "tab_key": "1"}],
    )


print("── ① 纯逻辑：save() 失败 ⇒ add() 不得假装成功 ──")
_real_save = duplex_remedy.save
try:
    duplex_remedy.save = lambda recs: False
    before = len(duplex_remedy.pending())
    rec = duplex_remedy.add(duplex_remedy.make_record(
        stage=duplex_remedy.STAGE_PASS1_DONE, reason="单元测试",
        printer="Fake Print", sheet_count=1, total_sides=2,
        items=[{"task_id": 1}], file_plans=[]))
    after = len(duplex_remedy.pending())
    check("①-a save() 返回 False 时 add() 返回空（不能给调用方一个假 id）",
          not rec or not rec.get("id"), rec)
    check("①-b 台账里没有多出这条（磁盘上确实没写）", after == before,
          f"{before} -> {after}")
    check("①-c add() 读不到它（get 返回 None）",
          duplex_remedy.get("") is None and duplex_remedy.get(
              (rec or {}).get("id") or "") is None)
finally:
    duplex_remedy.save = _real_save

print("── ② 正向对照：save() 正常时 add() 照常成功（防「一律返回空」的假修复）──")
rec_ok = duplex_remedy.add(duplex_remedy.make_record(
    stage=duplex_remedy.STAGE_PASS1_DONE, reason="正向对照",
    printer="Fake Print", sheet_count=1, total_sides=2, items=[{"task_id": 2}],
    file_plans=[]))
check("②-a 有 id", isinstance(rec_ok, dict) and bool(rec_ok.get("id")), rec_ok)
check("②-b 能从盘上读回", bool(duplex_remedy.get((rec_ok or {}).get("id") or "")))

print("── ③ 运行期：_keep_stack_stall 在 save 失败时返回 None ──")
win = gui.MainWindow(config_path=paths.config_path())
win._cloud_client = None
win._log = lambda *a, **k: None
win._config.printer_name = "Fake Print"
st = duplex_stack.DuplexStack(capacity=90, printer_name="Fake Print")
st.new_batch("savefail1")
bdir = duplex_stack.batch_dir(paths.duplex_stack_dir(), st.batch_id)
os.makedirs(bdir, exist_ok=True)
p1 = make_pdf(os.path.join(bdir, "entry_0001.pdf"), 2)
p2 = make_pdf(os.path.join(bdir, "entry_0002.pdf"), 2)
e1 = mk_entry(1, "A", p1, 2, duplex_stack.STATE_PRINTING, task_id=11)
e2 = mk_entry(2, "B", p2, 2, duplex_stack.STATE_PRINTING, task_id=12)
st.add(e1)
st.add(e2)
win._stack = st
before3 = len(duplex_remedy.pending())
try:
    duplex_remedy.save = lambda recs: False
    rec3 = win._keep_stack_stall([e1, e2], bdir, "攒批第 2 趟失败：boom")
finally:
    duplex_remedy.save = _real_save
check("③-a 返回 None（调用方据此退回「按失败收尾」，不得写 remedy_pending）",
      rec3 is None, rec3)
check("③-b 台账没有多出记录", len(duplex_remedy.pending()) == before3,
      f"{before3} -> {len(duplex_remedy.pending())}")

print("── ④ 运行期：没落盘 ⇒ 收尾不写 remedy_pending，任务按失败上报 ──")
job = gui.PrintJob(file_path="", display_name="A", copies=1)
job.task_id = 11
win._config.tabs["1"] = gui.TabSettings(jobs=[job])
win._config.tabs["1"].frozen = False
reported = []
win._report_job_result = lambda j, ok, msg, **k: reported.append((ok, msg))
warn = []
gui.QMessageBox.warning = staticmethod(lambda *a, **k: warn.append(
    tuple(str(x) for x in a if isinstance(x, str))))
win._stack = st
win._stack_flush_snapshot = [e1, e2]
win._stack_flush_worker = None
try:
    duplex_remedy.save = lambda recs: False
    win._on_stack_flush_done(False, pdf_printer.PARTIAL_MARKER + "整叠第 2 趟失败：boom", 0)
    crashed = None
except Exception as e:  # noqa: BLE001
    crashed = f"{type(e).__name__}: {e}"
finally:
    duplex_remedy.save = _real_save
check("④-a 收尾不抛异常", crashed is None, crashed)
check("④-b 没有给任务写一个盘上不存在的 remedy_pending",
      not str(getattr(job, "remedy_pending", "") or "").strip(),
      getattr(job, "remedy_pending", ""))
check("④-c 退回「按失败上报」（没证据就别让任务停在补救状态）",
      any(ok is False for ok, _m in reported), reported)

print("── ⑤ 静态兜底：add() 检查了 save() 的返回值 ──")
_src = open(os.path.join(LOCAL_TOOL, "duplex_remedy.py"), "r", encoding="utf-8").read()
_blk = _src.split("def add(", 1)[1].split("\ndef ", 1)[0]
check("⑤-a add() 里 save() 的结果被用来判定成败",
      ("if not save(" in _blk) or ("saved = save(" in _blk and "if not saved" in _blk),
      _blk[-400:])

print()
print(f"PASS {len(_ok)}  FAIL {len(_fail)}")
if _fail:
    print("存在失败 ❌")
    sys.exit(1)
print("全部通过 ✅")
