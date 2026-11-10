# -*- coding: utf-8 -*-
"""攒批（整叠）第 2 趟失败 / 第 1 趟失败的单 —— 必须留证据、不许被顺带报成功。

对应体检报告 `docs/健壮性体检_2026-11-10.md` 的：
  🔴1 攒批整叠第 2 趟失败：删证据 + 按失败上报，没有任何补打入口
  🔴2 第 1 趟失败的单照样进第 2 趟页序，并被整叠的成功顺带 report_success

本脚本的每一条断言都要求「实现被改坏就会红」：
  · `file_plans()` 里少了 STATE_FAILED 的跳过 → ③ 立刻红（失败单的页又出现在第 2 趟）
  · `_finish_stack_entries()` 里少了强制失败 → ④ 立刻红（失败单被报成功）
  · `_on_stack_flush_done()` 里无条件 discard_batch → ⑤ 红（批次目录被删了）
  · 失败路径仍然「按失败上报」→ ⑥ 红（云端会重推 ⇒ 已出的纸再出一遍）

跑法：
    PYTHONPATH= PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \
        C:/Users/Administrator/AppData/Local/Programs/Python/Python312/python.exe \
        other/tests/verify_stack_failure_ledger.py
"""
import os
import sys
import json
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="hn_stack_fail_")
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


def read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui  # noqa: E402
import paths  # noqa: E402
import duplex_stack  # noqa: E402
import duplex_remedy  # noqa: E402
import manual_duplex  # noqa: E402


def make_pdf(path, pages):
    from reportlab.pdfgen import canvas as _canvas
    c = _canvas.Canvas(path, pagesize=(595, 842))
    for i in range(pages):
        c.setFont("Helvetica", 24)
        c.drawString(72, 700, f"page {i + 1}")
        c.showPage()
    c.save()
    return path


def new_win():
    win = gui.MainWindow(config_path=paths.config_path())
    win._cloud_client = None
    win._cloud_connected = True
    win._log = lambda *a, **k: None
    return win


def mk_entry(seq, title, pdf, n_pages, state, tab_key="1", files=None, task_id=0):
    e = duplex_stack.StackEntry(
        seq=seq, title=title, tab_key=tab_key, order_id=0,
        order_number=f"HN{seq:04d}", page_count=n_pages,
        sheet_count=(n_pages + 1) // 2, state=state, rotate=True,
        created_at="2026-11-10 16:00:00", pdf=os.path.basename(pdf),
        file_plans=[{"label": title, "pages": list(range(1, n_pages + 1)),
                     "duplex": True, "second_rotate": True}],
        tasks=[{"task_id": task_id, "display_name": title,
                "file_path": files or "", "copies": 1, "duplex": "on",
                "page_range": "", "page_count": n_pages, "tab_key": tab_key}],
    )
    return e


print("── ① 静态：三处接线都在 ──")
src_stack = read(os.path.join(LOCAL_TOOL, "duplex_stack.py"))
_blk = src_stack.split("def file_plans", 1)[1].split("def ", 1)[0]
check("①-a file_plans() 跳过 STATE_FAILED 的单",
      "STATE_FAILED" in _blk and "continue" in _blk, _blk[:200])
check("①-b 跳过时仍按原始页数推进偏移（否则后面整叠错位）",
      "off +=" in _blk, _blk[:400])

src_gui = read(os.path.join(LOCAL_TOOL, "gui.py"))
_fin = src_gui.split("def _finish_stack_entries(", 1)[1].split("\n    def ", 1)[0]
check("①-c _finish_stack_entries() 对第 1 趟失败的单强制失败",
      'duplex_stack.STATE_FAILED' in _fin and "entry_ok = False" in _fin,
      _fin[:400])
_flush = src_gui.split("def _on_stack_flush_done(", 1)[1].split("\n    def ", 1)[0]
check("①-d 失败路径先落台账（_keep_stack_stall）",
      "_keep_stack_stall(" in _flush)
check("①-e 只有落了台账才不删批次目录（失败保留证据）",
      "_stalled_rec is None" in _flush and "discard_batch" in _flush)
check("①-f 落了台账就不再说「已按失败上报」",
      "已留证据" in _flush or "没有按失败上报" in _flush)

print("── ② 纯逻辑：失败单被排除，但偏移不乱 ──")
st = duplex_stack.DuplexStack()
st.new_batch("testbatch")
bdir = duplex_stack.batch_dir(paths.duplex_stack_dir(), st.batch_id)
os.makedirs(bdir, exist_ok=True)
p1 = make_pdf(os.path.join(bdir, "entry_0001.pdf"), 2)
p2 = make_pdf(os.path.join(bdir, "entry_0002.pdf"), 2)
p3 = make_pdf(os.path.join(bdir, "entry_0003.pdf"), 2)
e1 = mk_entry(1, "A-OK", p1, 2, duplex_stack.STATE_PRINTING)
e2 = mk_entry(2, "B-FAILED", p2, 2, duplex_stack.STATE_FAILED)
e3 = mk_entry(3, "C-OK", p3, 2, duplex_stack.STATE_PRINTING)
for e in (e1, e2, e3):
    st.add(e)

fps = st.file_plans()
labels = [f.label for f in fps]
check("②-a 失败单不进第 2 趟页序", "B-FAILED" not in labels, labels)
check("②-b 前后的单都还在", labels == ["A-OK", "C-OK"], labels)
check("②-c 失败单之后的单页号仍然对（偏移按 2 页推进）",
      [f.pages for f in fps] == [[1, 2], [5, 6]],
      [f.pages for f in fps])
plan = st.build_plan()
check("②-d 整叠计划里也没有失败单的页",
      3 not in [p for p in plan.pass_pages(1) if p is not None]
      and 4 not in [p for p in plan.pass_pages(1) if p is not None],
      plan.pass_pages(1))
check("②-e 张数按剩下的两单算（共 2 张）", plan.sheet_count == 2, plan.sheet_count)

print("── ③ 运行期：_keep_stack_stall 落台账 ──")
win = new_win()
win._config.printer_name = "Fake Print"
win._stack = st
_before = len(duplex_remedy.pending())
rec = win._keep_stack_stall([e1, e2, e3], bdir, "攒批第 2 趟失败：boom")
check("③-a 返回了台账记录", isinstance(rec, dict) and bool(rec.get("id")), rec)
check("③-b 写进了台账（pending 多一条）",
      len(duplex_remedy.pending()) == _before + 1,
      f"{_before} -> {len(duplex_remedy.pending())}")
_rid = (rec or {}).get("id") or ""
_stored = duplex_remedy.get(_rid) if _rid else None
check("③-c 落盘记录也是 step=pass1_done（可只补第 2 趟）",
      bool(_stored) and _stored.get("stage") == duplex_remedy.STAGE_PASS1_DONE,
      (_stored or {}).get("stage"))
check("③-d 半成品 PDF 存在（不删证据）",
      bool(_stored) and duplex_remedy.pdf_available(_stored))
check("③-e 记录里有整叠页序（可重建，不另算一套配对）",
      bool(_stored) and len(_stored.get("file_plans") or []) == 2,
      len((_stored or {}).get("file_plans") or []))
check("③-f 记录里有每单任务快照（含 tab_key）",
      bool(_stored) and all("tab_key" in it for it in (_stored.get("items") or [])),
      (_stored or {}).get("items"))
check("③-g 张数/面数与整叠一致",
      bool(_stored) and _stored.get("sheet_count") == plan.sheet_count,
      (_stored or {}).get("sheet_count"))

print("── ④ 运行期：第 2 趟失败 → 不再按失败上报、批次目录保留 ──")
# 造一个能找回 job 的标签页：把 tab_key 设成 1，并把 job 放进配置
job = gui.PrintJob(file_path="", display_name="A-OK", copies=1)
job.task_id = 0
win._config.tabs["1"] = gui.TabSettings(jobs=[job])
win._config.tabs["1"].frozen = False
e1.tasks[0]["file_path"] = ""
win._report_job_result = lambda *a, **k: None
reported = []
win._report_job_result = lambda j, ok, msg, **k: reported.append((ok, msg))
warn = []


def _fake_warning(*a, **k):
    warn.append(tuple(str(x) for x in a if isinstance(x, str)))


gui.QMessageBox.warning = staticmethod(_fake_warning)
_pending_before = len(duplex_remedy.pending())
win._stack = st
win._stack_flush_snapshot = [e1, e2, e3]
win._stack_flush_worker = None
try:
    win._on_stack_flush_done(False, "第 2 趟中途失败", 0)
    crashed = None
except Exception as e:  # noqa: BLE001
    crashed = f"{type(e).__name__}: {e}"
check("④-a 收尾不抛异常", crashed is None, crashed)
check("④-b 台账多了一条（失败与成功走不同收尾）",
      len(duplex_remedy.pending()) == _pending_before + 1,
      f"{_pending_before} -> {len(duplex_remedy.pending())}")
check("④-c 没有任何单被按失败上报（云端重推会把已出的纸再打一遍）",
      reported == [], reported)
check("④-d 批次目录还在（清单与各单 PDF 都留着）", os.path.isdir(bdir), bdir)
_warn_txt = "\n".join(" ".join(str(x) for x in w) if isinstance(w, tuple) else str(w)
                     for w in warn)
check("④-e 弹窗说的是「可补救」而不是「已按失败上报」",
      "半成品补救" in _warn_txt and "已按失败上报" not in _warn_txt, _warn_txt[:200])

print("── ⑤ 运行期：第 1 趟失败的单不能被整叠的成功顺带报成功 ──")
win2 = new_win()
_job_ok = gui.PrintJob(file_path="", display_name="C-OK", copies=1)
_job_ok.task_id = 88
_job_bad = gui.PrintJob(file_path="", display_name="B-FAILED", copies=1)
_job_bad.task_id = 77
win2._config.tabs["1"] = gui.TabSettings(jobs=[_job_ok, _job_bad])
win2._config.tabs["1"].frozen = False
got = []
win2._report_job_result = lambda j, ok, msg, **k: got.append((ok, msg))
win2._cloud_client = None
e2s = mk_entry(2, "B-FAILED", p2, 2, duplex_stack.STATE_FAILED, task_id=77)
e3s = mk_entry(3, "C-OK", p3, 2, duplex_stack.STATE_PRINTING, task_id=88)
win2._stack = st
win2._finish_stack_entries([e2s, e3s], True, "整叠第 2 趟完成")
_bad = [g for g in got if "第 1 趟打印失败" in g[1]]
_okr = [g for g in got if g[0] is True]
check("⑤-a 第 1 趟失败的单被报成失败、正常的单仍报成功",
      len(got) == 2 and len(_bad) == 1 and _bad[0][0] is False and len(_okr) == 1,
      got)
check("⑤-b 说明里写清「第 1 趟打印失败」",
      bool(_bad) and "第 1 趟打印失败" in _bad[0][1], got)

print("── ⑥ 台账内容可读（补救窗要显示的东西都在）──")
_txt = duplex_remedy.describe(_stored) if _stored else ""
check("⑥-a 说明里有第 1 趟/第 2 趟页序", "第 1 趟" in _txt and "第 2 趟" in _txt, _txt[:120])
check("⑥-b 说明里有「补打第 2 趟」的处理办法", "补打第 2 趟" in _txt)
check("⑥-c 说明里没有 markdown 星号", "**" not in _txt)
_manifest = duplex_remedy.manifest_path()
try:
    with open(_manifest, "r", encoding="utf-8") as _f:
        _raw = json.load(_f)
    _json_ok = isinstance(_raw, dict) and bool(_raw.get("records"))
except Exception as _e:  # noqa: BLE001
    _json_ok = False
    print(f"    （清单读取失败：{type(_e).__name__}: {_e}）")
check("⑥-d 台账文件是合法 JSON（跨进程可读）", _json_ok, _manifest)

print()
print(f"PASS {len(_ok)}  FAIL {len(_fail)}")
if _fail:
    print("存在失败 ❌")
    sys.exit(1)
print("全部通过 ✅")
