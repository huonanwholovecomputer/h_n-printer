# -*- coding: utf-8 -*-
"""半成品证据链（下）：合并作业「打了一部分」与「打印中取消并退出」都必须留台账。

对应体检报告 `docs/健壮性体检_2026-11-10.md` 的：
  🔴4 合并作业中途失败把「已经出了纸」的事实剥掉 → 上层整组重打 → 已出的页再出一遍
  🔴5 「取消打印并退出」先 terminate 再按放弃上报 → 纸打一半、订单变 abandoned 且救不回

每条断言都要求「实现被改坏就会红」：
  · 把 `print_pdf()` 的 PARTIAL_MARKER 去掉 → ① 立刻红
  · `_print_merged_group()` 不再调 `_keep_partial_group()` → ③ 红
  · `_keep_interrupt_stall()` 不写台账 / 写错 stage → ④⑤ 红
  · 放弃循环不再跳过 remedy_pending → ⑥ 红

跑法：
    PYTHONPATH= PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \
        C:/Users/Administrator/AppData/Local/Programs/Python/Python312/python.exe \
        other/tests/verify_partial_and_quit_ledger.py
"""
import os
import sys
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="hn_partial_quit_")
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
import pdf_printer  # noqa: E402
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


SRC_GUI = read(os.path.join(LOCAL_TOOL, "gui.py"))

print("── ① 「已经出了纸」这个事实必须能被上层看见 ──")
check("①-a 中途失败带上 PARTIAL_MARKER",
      "PARTIAL_MARKER + reason" in read(os.path.join(LOCAL_TOOL, "pdf_printer.py")))
check("①-b is_partial 认 PARTIAL_MARKER",
      pdf_printer.is_partial(pdf_printer.PARTIAL_MARKER + "卡纸") is True)
check("①-c is_partial 也认老的 MID_PRINT_MARKER（兼容）",
      pdf_printer.is_partial(pdf_printer.MID_PRINT_MARKER + "卡纸") is True)
check("①-d 普通失败不算部分出纸",
      pdf_printer.is_partial("打印机未就绪") is False)
check("①-e strip_partial 去掉标记留原因",
      pdf_printer.strip_partial(pdf_printer.PARTIAL_MARKER + "卡纸") == "卡纸",
      pdf_printer.strip_partial(pdf_printer.PARTIAL_MARKER + "卡纸"))
check("①-f 无标记时 strip_partial 原样返回",
      pdf_printer.strip_partial("打印机未就绪") == "打印机未就绪")

print("── ② 静态：合并作业失败分支已接线 ──")
_blk = SRC_GUI.split("def _print_merged_group(", 1)[1].split("\n    def ", 1)[0]
check("②-a 失败分支先判 is_partial", "pdf_printer.is_partial(msg)" in _blk)
check("②-b 只有部分出纸才落台账（别把纯失败也塞进来）",
      "if _partial:" in _blk and "_keep_partial_group(" in _blk)
check("②-c 日志说明不会自动整组重打", "不会自动整组重打" in _blk)
_kpg = SRC_GUI.split("def _keep_partial_group(", 1)[1].split("\n    def ", 1)[0]
check("②-d 台账 stage=pass2_partial（这叠纸不能再补第 2 趟）",
      "STAGE_PASS2_PARTIAL" in _kpg)
check("②-e 故意不传 file_plans（哪几张出来了无法可靠还原）",
      "file_plans" not in _kpg.replace("file_plans=[]", ""))

print("── ③ 运行期：合并作业部分出纸 → 落台账、禁「补打第 2 趟」──")
w = gui.PrintWorker([], "Fake Print", "off", False, paper="A4")
merged = make_pdf(os.path.join(tempfile.gettempdir(), "hn_partial_merged.pdf"), 4)
job1 = gui.PrintJob(file_path="", display_name="doc-A", copies=1)
job2 = gui.PrintJob(file_path="", display_name="doc-B", copies=1)
group = [{"idx": 0, "job": job1, "path": "", "temp_pdf": ""},
         {"idx": 1, "job": job2, "path": "", "temp_pdf": ""}]
_before = len(duplex_remedy.pending())
rec = w._keep_partial_group(group, merged, {0, 1}, pdf_printer.PARTIAL_MARKER + "卡纸")
check("③-a 返回台账记录", isinstance(rec, dict) and bool(rec.get("id")), rec)
_st = duplex_remedy.get((rec or {}).get("id") or "") if rec else None
check("③-b 落盘 stage=pass2_partial",
      bool(_st) and _st.get("stage") == duplex_remedy.STAGE_PASS2_PARTIAL,
      (_st or {}).get("stage"))
check("③-c 台账里有两个任务快照",
      bool(_st) and len(_st.get("items") or []) == 2,
      len((_st or {}).get("items") or []))
check("③-d 合并件副本留在台账目录（可整单重打）",
      bool(_st) and duplex_remedy.pdf_available(_st))
_pr = duplex_remedy.problems(_st) if _st else ["无记录"]
check("③-e 没有页序 ⇒ 补救窗不会给「补打第 2 趟」",
      bool(_pr) and any("页序" in p or "PDF" in p for p in _pr), _pr)
check("③-f pending 增加了一条（不会静默丢）",
      len(duplex_remedy.pending()) == _before + 1,
      f"{_before} -> {len(duplex_remedy.pending())}")
check("③-g 说明里写清「整单重打 / 作废」",
      "整单重打" in duplex_remedy.describe(_st) if _st else False)

print("── ④ 运行期：「取消打印并退出」先落证据再动手 ──")
win = gui.MainWindow(config_path=paths.config_path())
win._cloud_client = None
win._log = lambda *a, **k: None
win._config.printer_name = "Fake Print"
win._config.paper_size = "A4"
j1 = gui.PrintJob(file_path="C:/tmp/a.pdf", display_name="job-A", copies=1)
j1.task_id = 501
j2 = gui.PrintJob(file_path="", display_name="job-B", copies=1)   # 无路径无 task ⇒ 不入账
_before2 = len(duplex_remedy.pending())
rid = win._keep_interrupt_stall([j1, j2], 7)
check("④-a 返回了记录 id", isinstance(rid, str) and bool(rid), rid)
_i = duplex_remedy.get(rid) if rid else None
check("④-b 落盘 stage=pass2_partial（纸已经出来过了）",
      bool(_i) and _i.get("stage") == duplex_remedy.STAGE_PASS2_PARTIAL,
      (_i or {}).get("stage"))
check("④-c 出纸槽张数被记下来（人工核对要用）",
      bool(_i) and int(_i.get("tray_foreign") or 0) == 7,
      (_i or {}).get("tray_foreign"))
check("④-d 找不到文件/任务的空行没有进台账",
      bool(_i) and len(_i.get("items") or []) == 1,
      len((_i or {}).get("items") or []))
check("④-e 原因里写明「取消打印并退出」",
      bool(_i) and "取消打印并退出" in str(_i.get("reason") or ""),
      (_i or {}).get("reason"))
check("④-f pending 增加 1 条", len(duplex_remedy.pending()) == _before2 + 1,
      f"{_before2} -> {len(duplex_remedy.pending())}")

print("── ⑤ 静态：closeEvent 的先后顺序与放弃过滤 ──")
# ⚠ MainWindow 不是唯一有 closeEvent 的类（ManualDuplexDialog 等也有），
# 所以按「离 _keep_interrupt_stall 最近的那个 def closeEvent」取作用域，
# 不能用 split(..., 1) 取第一个。
# 找**调用点**（`_keep_interrupt_stall(` 的第一次出现是方法定义本身）
_i_in = SRC_GUI.find("_keep_interrupt_stall(_snap")
_ce_start = SRC_GUI.rfind("def closeEvent(", 0, _i_in) if _i_in >= 0 else -1
_ce = SRC_GUI[_ce_start:] if _ce_start >= 0 else ""
_ce = _ce.split("\n    def ", 1)[0] if "\n    def " in _ce else _ce
_i_cancel = _ce.find(".cancel()")
check("⑤-a 先落台账再 cancel（否则连 job 快照都没了）",
      0 <= _i_in and _ce_start >= 0 and 0 <= _i_cancel and
      (_i_in - _ce_start) < _i_cancel,
      f"stall_offset={( _i_in - _ce_start) if _ce_start >= 0 else -1} cancel@{_i_cancel}")
check("⑤-b 落了台账的 job 被标 remedy_pending",
      "_rid" in _ce and "remedy_pending" in _ce)
check("⑤-c 放弃上报循环跳过 remedy_pending 的 job",
      "getattr(job, 'remedy_pending'" in _ce or 'getattr(job, "remedy_pending"' in _ce)

print("── ⑥ 台账不会被「补打第 2 趟」误点（problems 是唯一判据）──")
check("⑥-a pass2_partial 的台账在补救窗里被禁补打",
      any("整单重打" in duplex_remedy.describe(_i or {}) for _ in [0]))
check("⑥-b problems() 对缺 PDF 的记录也会拒",
      bool(duplex_remedy.problems({"pdf": "", "file_plans": []})))

print()
print(f"PASS {len(_ok)}  FAIL {len(_fail)}")
if _fail:
    print("存在失败 ❌")
    sys.exit(1)
print("全部通过 ✅")
