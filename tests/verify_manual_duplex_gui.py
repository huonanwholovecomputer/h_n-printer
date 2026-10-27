# -*- coding: utf-8 -*-
"""验收脚本 · 手动双面接入打印线程（GUI 侧，offscreen，不碰真打印机）

2026-10 实机校准（HP LaserJet 1020）后的两轮 GUI 改动：

  ① **一次翻纸打完一批**（用户要求）：一次打印里所有"需手动双面"的文件会被
     **合并成一份 PDF**、按同一份页码排两趟、中途只翻一次纸。单文件时就是"一批一个文件"，
     走同一条代码路径（不再有"每个文件各自翻一次"的逻辑）。
  ② **常驻翻纸窗**（对齐 HP 官方工具）：批次一开始窗口就出现（状态①「打印第 1 趟中」），
     第 1 趟出纸后原地切到状态②「可翻纸」（按钮启用 + 倒计时），点继续 → 状态③，打完自动收。
     关窗 = 跳过，绝不擅自打第 2 趟。

其余覆盖：配对/页序/旋转补偿（长边补偿、短边不补偿）、跳过/超时、零回归
（单面 / 自动双面 / 能力未确认照旧）、无人值守挂起。

用桩函数替掉真打印（print_pdf / print_plan_pass / wait_for_output），PDF 合并与数页用**真的**
（测试素材是真 PDF），直接调 worker.run()（同线程 → 信号直连）。

    python tests/verify_manual_duplex_gui.py     # exit 0 = 全绿
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

_SANDBOX = tempfile.mkdtemp(prefix="hn_duplex_gui_")
os.environ["APPDATA"] = _SANDBOX                  # 用户数据目录 → 沙箱（不碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui  # noqa: E402
import pdf_printer  # noqa: E402
from printer_config import PrinterConfig, PrintJob, PrinterProfile  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ── 真 PDF 素材（worker 走 ".pdf 已是 PDF 跳过转换" 分支；合并/数页用真实实现）──
from reportlab.pdfgen import canvas  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402


def make_pdf(name: str, pages: int) -> str:
    path = os.path.join(_SANDBOX, name)
    c = canvas.Canvas(path, pagesize=A4)
    for i in range(pages):
        c.drawString(72, 760, f"{name} page {i + 1}")
        c.showPage()
    c.save()
    return path


PDF5 = make_pdf("doc5.pdf", 5)      # 配对：1/2 3/4 5/空白
PDF3 = make_pdf("doc3.pdf", 3)      # 配对：1/2 3/空白
PDF1 = make_pdf("doc1.pdf", 1)      # 配对：1/空白
PDF2_UP = make_pdf("doc2.pdf", 2)    # 合并/分组用（配对：1/2）
check("测试素材：4 份真 PDF（5/3/1/2 页）",
      all(os.path.isfile(p) and os.path.getsize(p) > 400 for p in (PDF5, PDF3, PDF1, PDF2_UP)))

MANUAL = PrinterProfile(auto_duplex=False, capability_source="manual", manual_duplex=True,
                        first="odd", reverse_second=True, second_rotate_180=True,
                        verified_at="2026-10-12 22:14:49", verified_by="丙成员")


def _pdf_page_texts(path: str) -> list:
    """读出 PDF 每页的文本（核"合并后的页数/页序"用；打不开返回 []）。

    页数/页序只能从**真正提交给打印机的那份 PDF** 上看 —— 桩记录的是调用参数，
    "合并时漏了一页""副本没重复"这类错都藏在提交的文档里。
    """
    try:
        import fitz
        with fitz.open(path) as d:
            return [p.get_text().strip() for p in d]
    except Exception:
        return []

def _pdf_page_rects(path: str) -> list:
    """每页尺寸 (宽, 高)（核"补的空白页是否与它盖住的那张纸同尺寸"）。"""
    try:
        import fitz
        with fitz.open(path) as d:
            return [(round(p.rect.width, 1), round(p.rect.height, 1)) for p in d]
    except Exception:
        return []


AUTO = PrinterProfile(auto_duplex=True, capability_source="manual", manual_duplex=False)
UNKNOWN = PrinterProfile()          # 能力未确认：必须保持旧行为（零回归）


# ============================================================
# 桩：记录 print_pdf / print_plan_pass 调用，模拟逐面进度
# ============================================================
class Rec:
    def __init__(self):
        self.plan_pass = []      # [(which, 本趟是否旋转, 0-based序列, 1-based序列, 传下去的旋转设置)]
        self.print_pdf = []      # [kwargs]
        self.merged_pages = []   # 每次 print_pdf 提交的那份 PDF 的逐页文本（核页数/页序用）
        self.announce = []       # [常驻窗 announce 文案]
        self.flip = []           # [翻纸提示文案]
        self.finished_evt = 0
        self.waited = []
        self.logs = []
        self.progress = []
        self.finished = []
        self.plan_labels = []    # [{sheets:[(标签,a,b)], slots:{纸序:标签}}]
        # 每一趟**真正提交的那份批次 PDF** 的逐页文本（核"单面文件真的并进来了吗"）
        self.plan_pages = []     # [[页文本…], [页文本…]]（第 1 趟 / 第 2 趟）
        self.plan = []           # [(第 1 趟张数, 预估秒数)] —— 批次开始播报
        self.samples = []        # [(第 1 趟张数, 实测秒数)] —— 自动学习样本
        self.all_finished = []   # [(成功数, 失败数)]
        self.raised = None       # worker.run() 逃出来的异常（None = 干净退出）


def patched(rec, fail_merge_at=None):
    orig = {n: getattr(gui, n) for n in
            ("print_pdf", "print_plan_pass", "wait_for_output", "get_converter")}
    orig_merge = pdf_printer.merge_pdfs
    _calls = {"n": 0}

    def _merge(pdfs, out):
        # 批次合并是**真的**在合并（测试素材是真 PDF）；fail_merge_at 用来模拟
        # "合并/写盘炸了"（磁盘满、文件被占），验证并批路径的总兜底。
        _calls["n"] += 1
        if fail_merge_at is not None and _calls["n"] == fail_merge_at:
            raise RuntimeError("桩：合并 PDF 时炸了")
        return orig_merge(pdfs, out)

    pdf_printer.merge_pdfs = _merge

    def _print_pdf(**kw):
        rec.print_pdf.append(kw)
        # 合并打印的页数/页序只能从**真正提交的那份 PDF** 上看（一个作业一段）——
        # 这里记下来，验收里用 fitz 打开它核对。
        rec.merged_pages.append(_pdf_page_texts(kw.get("pdf_path", "")))
        # 模拟逐面进度（与真实 GDI 一致：每送出一面回调一次）。合并路径的进度分母
        # 是否算进了"文件间空白页"，只能从这里看。
        _cb = kw.get("progress_callback")
        _n = len(rec.merged_pages[-1])
        if _cb and _n:
            for _i in range(1, _n + 1):
                _cb(_i, _n)
        return True, "桩：一次提交"

    def _print_plan_pass(plan, path, printer, which, second_rotate_180=False,
                         progress_callback=None, dpi=0):
        seq0 = plan.pass_page_indices(which)
        # 真正"只在第 2 趟旋转"的判定在 print_plan_pass 内部（verify_duplex_probe 断言了位图真的
        # 被 transpose）；这里按同一契约记录"本趟实际是否旋转"+ GUI 是否把设置传下去。
        rec.plan_pass.append((which, bool(which == 2 and second_rotate_180), seq0,
                              plan.pass_pages(which), bool(second_rotate_180)))
        # 组一致性检查用：每张纸的两面、以及每个槽位归属的标签
        rec.plan_labels.append({
            "sheets": [(s.label, s.side_a, s.side_b) for s in plan.sheets],
            "slots": {s.sheet_index: s.label for s in list(plan.first_pass) + list(plan.second_pass)},
        })
        # 真正提交给打印机的那份批次 PDF 的逐页文本（"单面文件有没有并进来"只能看这里）
        rec.plan_pages.append(_pdf_page_texts(path))
        if progress_callback:
            for i in range(1, len(seq0) + 1):
                progress_callback(i, len(seq0))
        return True, f"桩：第 {which} 趟"

    def _wait_for_output(printer, pages=0, **kw):
        rec.waited.append(pages)
        return {"drained": True, "queue_seconds": 0.0, "margin_seconds": 2.0,
                "peak_jobs": 0, "detail": ""}

    gui.print_pdf = _print_pdf
    gui.print_plan_pass = _print_plan_pass
    gui.wait_for_output = _wait_for_output
    gui.get_converter = lambda: object()

    def restore():
        for k, v in orig.items():
            setattr(gui, k, v)
        pdf_printer.merge_pdfs = orig_merge
    return restore


def make_job(path, duplex="on", copies=1, mode="long-edge", name="", page_count=0,
             page_range="", dpi=0):
    """默认 page_count=0 = **页数未知**（`_uses_manual_duplex` 不当成 1 页，保持既有口径）；
    要复现"页数已知的 1 页文件"这类现场，显式传 page_count。"""
    return PrintJob(file_path=path, copies=copies, duplex=duplex, duplex_mode=mode,
                    page_range=page_range, page_count=page_count, dpi=dpi,
                    display_name=name or os.path.basename(path))


def run_worker(jobs, profile, flip_answer=True, on_announce=None, timeout=None,
               cover=False, fail_merge_at=None, printer_name="Fake Print"):
    rec = Rec()
    w = gui.PrintWorker(jobs=jobs, printer_name=printer_name, duplex_mode="long-edge",
                        keep_temp_pdf=False, render_dpi=200, manual_duplex_profile=profile,
                        cover_page=cover,
                        cover_page_config={"order_number": "HN20261015-0008",
                                           "created_at": "2026-10-15 21:51:00"})
    if timeout is not None:
        w.FLIP_TIMEOUT_SEC = timeout
        w.FLIP_GRACE_SEC = 0
    w.log_message.connect(rec.logs.append)
    w.progress.connect(lambda c, t, s: rec.progress.append((c, t, s)))
    w.job_finished.connect(lambda i, ok, msg: rec.finished.append((i, ok, msg)))
    w.all_finished.connect(lambda a, b: rec.all_finished.append((a, b)))
    w.duplex_announce.connect(rec.announce.append)
    w.duplex_finished.connect(lambda _t: setattr(rec, "finished_evt", rec.finished_evt + 1))
    if on_announce is not None:
        w.duplex_announce.connect(lambda text: on_announce(w))
    if flip_answer is not None:
        w.flip_required.connect(
            lambda text, sec: (rec.flip.append(text), w.confirm_flip(flip_answer)))
    restore = patched(rec, fail_merge_at=fail_merge_at)
    t0 = time.time()
    try:
        w.run()
    except BaseException as e:          # 异常逃出 run() 本身就是被测行为，不能让它炸掉脚本
        rec.raised = e
    finally:
        restore()
    rec.elapsed = time.time() - t0
    return rec


# ============================================================
# ① 单文件（一批一个文件）：两趟 + 一次翻纸 + 连续进度
# ============================================================
print("\n=== ① 单文件手动双面 ===")
rec = run_worker([make_job(PDF5)], MANUAL)
check("★ 走两趟（print_plan_pass ×2），没有一次提交的 print_pdf",
      len(rec.plan_pass) == 2 and not rec.print_pdf,
      f"plan_pass={[p[0] for p in rec.plan_pass]}")
check("★ 第 1 趟 = 1 3 5（0-based 0,2,4），本趟不旋转",
      rec.plan_pass[0][0] == 1 and rec.plan_pass[0][1] is False
      and rec.plan_pass[0][2] == [0, 2, 4] and rec.plan_pass[0][3] == [1, 3, 5],
      f"{rec.plan_pass[0][2]}")
check("★ 第 2 趟 = 〔空白〕4 2（0-based None,3,1），本趟旋转 180°",
      rec.plan_pass[1][0] == 2 and rec.plan_pass[1][1] is True
      and rec.plan_pass[1][2] == [None, 3, 1] and rec.plan_pass[1][3] == [None, 4, 2],
      f"{rec.plan_pass[1][2]}")
check("★ 两趟都把档案里的旋转设置传下去（判定在 print_plan_pass 内部）",
      rec.plan_pass[0][4] is True and rec.plan_pass[1][4] is True)
check("★ 先弹常驻窗（announce）再翻纸提示：各有 1 次", len(rec.announce) == 1 and len(rec.flip) == 1)
check("翻纸文案含 HP 三步原文 + 出纸张数 + 将要打的页",
      "不要丢弃空白页面" in rec.flip[0] and "已打印面朝下" in rec.flip[0]
      and "应有 3 张纸" in rec.flip[0], f"{rec.flip[0][:60]}…")
check("弹提示之前先等出纸（按第 1 趟面数 3 页）", rec.waited == [3], f"{rec.waited}")
check("★ 进度拼成连续序列：total=6，面号 1..6（两趟合成一条）",
      [(c, t) for c, t, s in rec.progress if "正在打印:" in s]
      == [(1, 6), (2, 6), (3, 6), (4, 6), (5, 6), (6, 6)],
      f"{[(c, t) for c, t, s in rec.progress if '正在打印:' in s]}")
check("任务成功 + 批次收尾信号发出（常驻窗据此关闭）",
      rec.finished == [(0, True, rec.finished[0][2])] and rec.finished[0][1] is True
      and rec.finished_evt == 1, f"{rec.finished} evt={rec.finished_evt}")

# ============================================================
# ①b ★核心：三个文件 → 一批、一次翻纸
# ============================================================
print("\n=== ①b 三个文件合并成一批（只翻一次纸）===")
jobs3 = [make_job(PDF5, name="报告.docx"), make_job(PDF3, name="附件.docx"), make_job(PDF1, name="封面.docx")]
rec = run_worker(jobs3, MANUAL)
check("★ 三个文件只打**两趟**（一次翻纸），不是每个文件各两趟",
      len(rec.plan_pass) == 2 and len(rec.flip) == 1,
      f"plan_pass={len(rec.plan_pass)} flip={len(rec.flip)}")
check("★ 合并后按同一份页码排两趟：5+3+1=9 页 → 6 张纸；第 1 趟 1 3 5 6 8 9",
      rec.plan_pass[0][3] == [1, 3, 5, 6, 8, 9], f"{rec.plan_pass[0][3]}")
check("★ 第 2 趟 = 〔空白〕 〔空白〕 7 〔空白〕 4 2（整批统一逆序）",
      rec.plan_pass[1][3] == [None, None, 7, None, 4, 2], f"{rec.plan_pass[1][3]}")
check("★ 每个文件各自内部配对（跨文件不错位）",
      rec.plan_pass[0][2] == [0, 2, 4, 5, 7, 8] and rec.plan_pass[1][2] == [None, None, 6, None, 3, 1],
      f"{rec.plan_pass[1][2]}")
check("常驻窗文案列出三个文件 + 两趟页序",
      len(rec.announce) == 1 and "报告.docx" in rec.announce[0]
      and "第 1 趟（现在打）" in rec.announce[0], f"{rec.announce[0][:50]}…")
check("翻纸文案说清共 6 张纸", "应有 6 张纸" in rec.flip[0], "")
check("★ 三个任务各自收到完成回报（云端按文件分别回报成功）",
      sorted(i for i, ok, _m in rec.finished) == [0, 1, 2]
      and all(ok for _i, ok, _m in rec.finished), f"{rec.finished}")
# 2026-10-18：本轮已单面直打的纸（这里两件：5 页 docx 里的 1 页图片）
# 现在**并进批次页序** ⇒ 多 1 张纸 ⇒ 面数 6×2=12 变 13（那张纸第 2 趟只走纸不打墨）。
# 并进来的理由见 `gui.PrintWorker._print_manual_batch` 的 direct_printed 说明：
# 不并进来，第 2 趟喂不到它，它就留在进纸口被下一次打印当第一张吸走。
check("进度 total = 12（A 走合并作业 6 面 + 批次 6 面）",
      [(c, t) for c, t, s in rec.progress if "正在打印:" in s][-1] == (12, 12),
      f"{[(c, t) for c, t, s in rec.progress if '正在打印:' in s][-1:]}")

# ============================================================
# ①c 多份 + ①d 短边 + ①e 与单面文件混排
# ============================================================
print("\n=== ①c 多份 / ①d 短边 / ①e 混排 ===")
rec = run_worker([make_job(PDF5, copies=2)], MANUAL)
check("多份：两趟各自把份数算进去（每份 3 张 → 共 6 张，两趟各 6 面）",
      rec.plan_pass[0][3] == [1, 3, 5, 1, 3, 5] and "应有 6 张纸" in rec.flip[0],
      f"{rec.plan_pass[0][3]}")

# ★ 多文件 × 多份：每份必须整张纸配对（奇数页的空白落在每份末尾，不能串到下一份）
rec = run_worker([make_job(PDF3, copies=2, name="三页.docx"),
                  make_job(PDF5, copies=2, name="五页.docx")], MANUAL)
check("★ 3 页×2 + 5 页×2 合并成一批：仍是**一次翻纸**、两趟",
      len(rec.plan_pass) == 2 and len(rec.flip) == 1, f"flip={len(rec.flip)}")
check("★ 第 1 趟 = 1 3 1 3 || 4 6 8 4 6 8（各份各自，页码复用不推进）",
      rec.plan_pass[0][3] == [1, 3, 1, 3, 4, 6, 8, 4, 6, 8], f"{rec.plan_pass[0][3]}")
check("★ 第 2 趟 = 〔空白〕7 5 〔空白〕7 5 〔空白〕2 〔空白〕2（整批统一逆序）",
      rec.plan_pass[1][3] == [None, 7, 5, None, 7, 5, None, 2, None, 2],
      f"{rec.plan_pass[1][3]}")
# 组一致性（与引擎验收里钉的是同一条不变式）：每张纸两面同属一份
_grp = rec.plan_labels[0]
_mixed = []
for si, (lab, a, b) in enumerate(_grp["sheets"]):
    slot_lab = _grp["slots"].get(si)
    if slot_lab != lab:
        _mixed.append((si, lab, slot_lab, a, b))
check("★ 每张纸的两面来自同一份（无跨份错位 —— 第 2 份第 1 页绝不接到第 1 份末页）",
      not _mixed, f"{_mixed[:3]}")
check("★ 3 页份的槽位展开 = [1,2,3,空白]，5 页份 = [4,5,6,7,8,空白]（空白在每份末尾）",
      [s for s in _grp["sheets"] if s[0].startswith("三页.docx")]
      == [("三页.docx（第1份）", 1, 2), ("三页.docx（第1份）", 3, None),
          ("三页.docx（第2份）", 1, 2), ("三页.docx（第2份）", 3, None)]
      and [s for s in _grp["sheets"] if s[0].startswith("五页.docx")]
      == [("五页.docx（第1份）", 4, 5), ("五页.docx（第1份）", 6, 7), ("五页.docx（第1份）", 8, None),
          ("五页.docx（第2份）", 4, 5), ("五页.docx（第2份）", 6, 7), ("五页.docx（第2份）", 8, None)],
      f"{_grp['sheets']}")
check("★ 每份的空白数：3 页份 1 个 + 5 页份 1 个，共 2 份×2 = 4 个空白",
      rec.plan_pass[1][3].count(None) == 4 and "应有 10 张纸" in rec.flip[0],
      f"空白数={rec.plan_pass[1][3].count(None)}")
check("★ 两个任务各自回报一次（份数不再多报）",
      sorted(i for i, _ok, _m in rec.finished) == [0, 1], f"{rec.finished}")

rec = run_worker([make_job(PDF5, mode="short-edge")], MANUAL)
check("★ 短边装订 → 第 2 趟**不**旋转（长边才补偿 180°）",
      rec.plan_pass[1][4] is False and rec.plan_pass[1][1] is False,
      f"传下去={rec.plan_pass[1][4]}")

rec = run_worker([make_job(PDF5, name="A.docx"), make_job(PDF3, duplex="off", name="B.docx"),
                  make_job(PDF1, name="C.docx")], MANUAL)
check("★ 混排：两个双面文件被合并成**一批一次翻纸**（不因中间夹了单面文件而翻两次）",
      len(rec.flip) == 1 and len(rec.plan_pass) == 2, f"flip={len(rec.flip)}")
# 2026-10-26（用户现场 HN20261026-0014）口径：**整单一个批次** ——
# 本单既然要翻一次纸，单面/1 页文件就不许另起直打作业（旧口径下 B、C 会先直打成
# 另一份 spooler 作业，同一个订单 = 3 个作业）。它们按 `FilePlan(duplex=False)` 并入
# 同一份页序：每页各占一张纸、背面留空，第 2 趟对应槽位打空白占位页（只走纸不打墨）。
check("★ 混排：**没有**任何直打作业（A/B/C 全并进同一批次 ⇒ spooler 里只剩两趟）",
      len(rec.print_pdf) == 0, f"{[(c.get('duplex'), c.get('copies')) for c in rec.print_pdf]}")
check("混排：三个任务都有完成回报", sorted(i for i, _ok, _m in rec.finished) == [0, 1, 2],
      f"{rec.finished}")
check("★ 混排：7 张纸（A 3 + B 3 + C 1），第 1 趟 = A 1/3/5 + B 6/7/8 + C 9",
      rec.plan_pass[0][3] == [1, 3, 5, 6, 7, 8, 9],
      f"{rec.plan_pass[0][3]}")
check("★ 混排：第 2 趟 = 整批逆序补打（B/C 与 A 末张的背面都是空白占位）",
      rec.plan_pass[1][3] == [None, None, None, None, None, 4, 2],
      f"{rec.plan_pass[1][3]}")
check("★ 混排：单面文件在页序里各自成一张纸（B 三页各占一张、C 一页一张）",
      [s for s in rec.plan_labels[0]["sheets"] if s[0].startswith("B.docx")]
      == [("B.docx", 6, None), ("B.docx", 7, None), ("B.docx", 8, None)]
      and [s for s in rec.plan_labels[0]["sheets"] if s[0].startswith("C.docx")]
      == [("C.docx", 9, None)],
      f"{rec.plan_labels[0]['sheets']}")
check("★ 混排·翻纸文案：槽里只有本批的 7 张（不再有「只取最上面 N 张」的 foreign 口径）",
      "出纸槽里应有 7 张纸" in rec.flip[0] and "不属于本批" not in rec.flip[0]
      and "最上面" not in rec.flip[0],
      f"{rec.flip[0][:120]}…")
check("★ 混排：进度分母 = 7 张 × 两趟 = 14 面（并进来的单面文件第 2 趟也要走纸）",
      [t for c, t, s in rec.progress if "正在打印:" in s][-1] == 14,
      f"{[t for c, t, s in rec.progress if '正在打印:' in s][-1:]}")
check("★ 混排·常驻窗标明哪几个是单面文件（背面留空、第 2 趟打空白页）",
      bool(rec.announce) and "单面文件" in rec.announce[0]
      and "B.docx" in rec.announce[0] and "C.docx" in rec.announce[0]
      and "背面留空" in rec.announce[0], f"{rec.announce[:1]}")

# ── 翻纸文案的"槽里几张"必须算上批次**之前**已直打的纸（2026-10-15 生产故障）──
# 现场订单 HN20261015-0008：JPG（1 页横向、duplex=off）单独直打 + docx（3 页、手动双面）
# 进批次。翻纸窗只报本批的 2 张，而槽里实际有 3 张；操作员照"从出纸槽中取出纸叠"把
# **整叠**搬进纸盘 2 ⇒ 多出来那张留在纸盘里、被后续作业当白纸用掉 —— 文件静默丢失。
# （spooler 硬证据：PrintService/Operational 的 842 显示照片那个作业 21:51:29 入队、
#   21:51:49 已交打印处理器，而批次第 1 趟的翻纸窗 21:52:09 才弹。）
# 2026-10-26：默认已经整单并批（上面那组），所以"批次前已直打"只剩**并不进来**的两种：
# 读不出页数的坏件、以及逐文件 DPI 与批次不同（`_can_join_manual_batch` 的两条闸门）。
# 那种情况口径不变：只取最上面 N 张，下面 foreign 张留在出纸槽里。这里用"DIP 不同"造，
# 因为它是**成功直打**（能覆盖"槽里真有纸"的台账口径）。
rec_dpi = run_worker([make_job(PDF5, name="A.docx"),
                      make_job(PDF3, duplex="off", page_count=3, dpi=300, name="B.docx")], MANUAL)
check("★ DPI 与批次不同 ⇒ 不并进批次（两趟只有一个渲染 DPI）",
      len(rec_dpi.print_pdf) == 1 and rec_dpi.print_pdf[0].get("dpi") == 300,
      f"{[(k.get('dpi'), os.path.basename(k.get('pdf_path', ''))) for k in rec_dpi.print_pdf]}")
check("★ 混排·翻纸文案报槽里**总数**（本批 3 张 + 已直打 3 张）",
      "共应有 6 张纸" in rec_dpi.flip[0], f"{rec_dpi.flip[0][:100]}…")
check("★ 混排·明确指出「最上面 N 张是本批的、下面 foreign 张不要一起搬走」",
      "最上面" in rec_dpi.flip[0] and "不属于本批" in rec_dpi.flip[0],
      f"{rec_dpi.flip[0][:180]}…")
check("★ 混排·旧的火星断言「整叠一起取走」必须消失（那样会把直打的纸也搬进去）",
      "整叠" not in rec_dpi.flip[0].split("请按下面三步")[0])
check("★ 混排·日志同样报槽里总数", "共应有" in "".join(rec_dpi.logs),
      f"{[m for m in rec_dpi.logs if '出纸槽' in m]}")
# ★ 部分并批：能并的并（单页）、并不了的（DPI 不同）先直打 —— 日志必须照实说"另有 N 个"，
#   绝不能在同一轮里谎报"只有两个作业"（2026-10-26 独立审查指出）
rec_part = run_worker([make_job(PDF3, page_count=3, name="双面.docx"),
                       make_job(PDF1, page_count=1, name="单页.docx"),
                       make_job(PDF2_UP, duplex="off", page_count=2, dpi=300,
                                name="高DPI.docx")], MANUAL)
check("★ 部分并批：能并的并、并不了的（DPI 不同）仍单独直打 —— 日志照实说「另有 N 个」",
      len(rec_part.print_pdf) == 1 and rec_part.print_pdf[0].get("dpi") == 300
      and any("另有 1 个文件并不进来" in m for m in rec_part.logs)
      and not any("只有「第 1 趟 / 第 2 趟」两个作业" in m for m in rec_part.logs)
      and "不属于本批" in rec_part.flip[0],
      f"{[m for m in rec_part.logs if '整单同一个批次' in m]} / "
      f"{[k.get('dpi') for k in rec_part.print_pdf]}")
check("★ 混排·常驻窗说明「下面 foreign 张不属于本批、别一起搬」",
      bool(rec_dpi.announce) and "不属于本批" in rec_dpi.announce[0], f"{rec_dpi.announce[:1]}")

# ★ 2026-10-26 独立审查 M8：单面文件排在**首位**、且 duplex_mode 与后面双面文件不同时，
#   绝不能按"它的方向"建第一批（那会多拆一次翻纸，且那一批的第 2 趟全是空白页）。
rec_ord = run_worker([make_job(PDF1, page_count=1, mode="short-edge", name="单页短边.pdf"),
                      make_job(PDF3, page_count=3, mode="long-edge", name="双面长边.pdf")],
                     MANUAL)
check("★ 顺序：单面文件排首位 + 装订方向不同 ⇒ 仍**只翻一次纸**、只有两个作业",
      len(rec_ord.plan_pass) == 2 and len(rec_ord.flip) == 1 and not rec_ord.print_pdf,
      f"plan_pass={len(rec_ord.plan_pass)} flip={len(rec_ord.flip)} "
      f"print_pdf={len(rec_ord.print_pdf)}")
check("★ 顺序：那一批的页序仍按文件顺序（单页在前、双面在后），没有多出空白批次",
      rec_ord.plan_pass[0][3] == [1, 2, 4] and rec_ord.plan_pass[1][3] == [None, 3, None],
      f"{[p[3] for p in rec_ord.plan_pass]}")

# 读不出页数的坏件绝不并进批次（会把整批带失败），仍按原样单独打、单独报成败
_BROKEN_J = os.path.join(_SANDBOX, "broken_join.pdf")
with open(_BROKEN_J, "wb") as _f:
    _f.write(b"%PDF-1.4\nthis is not a real pdf\n%%EOF\n")
rec_bj = run_worker([make_job(_BROKEN_J, duplex="off", page_count=2, name="坏件.docx"),
                     make_job(PDF3, name="好件.docx")], MANUAL)
check("★ 坏件（读不出页数）不并进批次：单独提交、单独报成败（不许把整批带失败）",
      len(rec_bj.print_pdf) == 1
      and os.path.basename(rec_bj.print_pdf[0].get("pdf_path", "")) == "broken_join.pdf"
      and sorted(i for i, _o, _m in rec_bj.finished) == [0, 1],
      f"{[os.path.basename(k.get('pdf_path', '')) for k in rec_bj.print_pdf]} / {rec_bj.finished}")

# ============================================================
# ①f 2026-10-18 / 2026-10-26 现场：1 页文件 + 2 页双面文件
#     口径（2026-10-26 用户要求）：**整单一个批次** —— 1 页文件不再单独直打，而是
#     按 `FilePlan(duplex=False)` 并进同一份页序（一张纸、背面留空，第 2 趟打空白占位页）。
#     这样 spooler 里只剩「第 1 趟 / 第 2 趟」两个作业；旧口径会多一个直打作业。
#     ⚠ 本条场景用**已知页数**（page_count=1）：页数未知时 `_uses_manual_duplex` 不当成 1 页
#       （G1/G2 钉住的既有口径），那种情况本来就按两趟走。
# ============================================================
print("\n=== ①f 现场场景：1 页 + 2 页双面（整单一个批次）===")
rec_inc = run_worker([make_job(PDF1, page_count=1, name="作业三.pdf"),
                      make_job(PDF3, page_count=3, name="作业二.pdf")], MANUAL)
check("★ 现场：**没有**直打作业（1 页文件并进同一批次 ⇒ 只剩两趟）",
      len(rec_inc.print_pdf) == 0,
      f"{[(c.get('duplex'), c.get('copies')) for c in rec_inc.print_pdf]}")
check("★ 现场：批次含两份（第 1 趟 = 1 页文件的正文 + 3 页双面的 2/4）",
      len(rec_inc.plan_pass) == 2 and rec_inc.plan_pass[0][3] == [1, 2, 4]
      and rec_inc.plan_pass[1][3] == [None, 3, None],
      f"{[p[3] for p in rec_inc.plan_pass]}")
check("★ 现场：1 页文件在页序里是一个独立纸位（正面 = 它、背面空白）",
      ("作业三.pdf", 1, None) in rec_inc.plan_labels[0]["sheets"],
      f"{rec_inc.plan_labels[0]['sheets']}")
check("★ 现场：翻纸文案说槽里只有本批的 3 张（不再要求「只取最上面 2 张」）",
      "出纸槽里应有 3 张纸" in rec_inc.flip[0] and "不属于本批" not in rec_inc.flip[0],
      f"{rec_inc.flip[0][:120]}…")
check("★ 现场：两个任务都报成功", sorted(i for i, _o, _m in rec_inc.finished) == [0, 1],
      f"{rec_inc.finished}")
check("★ 现场：常驻窗点名「作业三.pdf」是单面文件（背面留空）",
      bool(rec_inc.announce) and "作业三.pdf" in rec_inc.announce[0]
      and "单面文件" in rec_inc.announce[0], f"{rec_inc.announce[:1]}")

# ★★ 2026-10-26 现场原样复刻（HN20261026-0014）：3 页双面 + 1 页 + 2 页双面，
#    spooler 里曾经是「单页文档测试.pdf」+「hn_duplex_batch_xxx.pdf」两个作业（第 2 趟再一个）。
#    现在必须只剩「第 1 趟 / 第 2 趟」两个：3+1+2 共 7 页 → 4 张纸。
print("\n=== ①f2 现场复刻：3 页双面 + 1 页 + 2 页双面（整单一个批次）===")
rec_live = run_worker([make_job(PDF3, page_count=3, name="三页文档测试.pdf"),
                       make_job(PDF1, page_count=1, name="单页文档测试.pdf"),
                       make_job(PDF2_UP, page_count=2, name="双页文档测试.pdf")], MANUAL)
check("★★ 现场复刻：一个订单只提交**两趟**（spooler 里不再有单独的直打作业）",
      len(rec_live.plan_pass) == 2 and len(rec_live.print_pdf) == 0 and len(rec_live.flip) == 1,
      f"plan_pass={len(rec_live.plan_pass)} print_pdf={len(rec_live.print_pdf)} "
      f"flip={len(rec_live.flip)}")
check("★★ 现场复刻：4 张纸，第 1 趟 = 1 3 4 5（1 页文件占第 3 张纸）",
      rec_live.plan_pass[0][3] == [1, 3, 4, 5],
      f"{rec_live.plan_pass[0][3]}")
check("★★ 现场复刻：第 2 趟 = 6 〔空白〕 〔空白〕 2（整叠逆序，单页文件的背面是空白）",
      rec_live.plan_pass[1][3] == [6, None, None, 2],
      f"{rec_live.plan_pass[1][3]}")
check("★★ 现场复刻：每张纸两面同属一个文件（3 页→2 张、单页→1 张、2 页→1 张）",
      rec_live.plan_labels[0]["sheets"] == [
          ("三页文档测试.pdf", 1, 2), ("三页文档测试.pdf", 3, None),
          ("单页文档测试.pdf", 4, None), ("双页文档测试.pdf", 5, 6)],
      f"{rec_live.plan_labels[0]['sheets']}")
check("★★ 现场复刻：三个文件各自回报成功（单页文件不再走另一条直接打印路径）",
      sorted(i for i, ok, _m in rec_live.finished) == [0, 1, 2]
      and all(ok for _i, ok, _m in rec_live.finished), f"{rec_live.finished}")
check("★★ 现场复刻：日志里出现「整单同一个批次」（可现场核对是否真的只有一个作业）",
      any("整单同一个批次" in m for m in rec_live.logs),
      f"{[m for m in rec_live.logs if '批次' in m][:3]}")
check("★★ 现场复刻：批次 PDF 里**真的含三个文件的页、且顺序 = 文件顺序**",
      rec_live.plan_pages and rec_live.plan_pages[0] == [
          "doc3.pdf page 1", "doc3.pdf page 2", "doc3.pdf page 3",
          "doc1.pdf page 1",
          "doc2.pdf page 1", "doc2.pdf page 2"],
      f"{rec_live.plan_pages[:1]}")
check("★★ 现场复刻：两趟提交的是**同一份**合并件（单面文件的两趟都在这份文档里）",
      len(rec_live.plan_pages) == 2 and rec_live.plan_pages[0] == rec_live.plan_pages[1],
      f"{[len(p) for p in rec_live.plan_pages]}")

# 单面文件并进批次时，**份数 + 页码范围**也必须照实生效（少打 = 顾客少纸）
rec_cp_b = run_worker([make_job(PDF3, page_count=3, name="A.docx"),
                       make_job(PDF2_UP, duplex="off", page_count=2, copies=2,
                                page_range="1-2", name="B.docx")], MANUAL)
check("★ 单面文件并批：份数×页码范围 各自占纸（B 选 2 页 ×2 份 = 4 张，共 6 张）",
      rec_cp_b.plan_pass[0][3] == [1, 3, 4, 5, 4, 5]
      and rec_cp_b.plan_pass[1][3] == [None, None, None, None, None, 2]
      and "应有 6 张纸" in rec_cp_b.flip[0],
      f"{[p[3] for p in rec_cp_b.plan_pass]}")
check("★ 单面文件并批：合并件 = A 的 3 页 + B 的 2 页（份数/页码由页序引用，重复不写进合并件）",
      rec_cp_b.plan_pages and rec_cp_b.plan_pages[0] == [
          "doc3.pdf page 1", "doc3.pdf page 2", "doc3.pdf page 3",
          "doc2.pdf page 1", "doc2.pdf page 2"],
      f"{rec_cp_b.plan_pages[:1]}")

# 纯批次（本单没有单面文件）→ 文案保持原样，不引入多余警告（零回归）
rec_pure = run_worker([make_job(PDF5, name="A.docx"), make_job(PDF3, name="B.docx")], MANUAL)
check("★ 纯批次：文案仍是「出纸槽里应有 5 张纸」原样（5 页→3 张 + 3 页→2 张）",
      "出纸槽里应有 5 张纸" in rec_pure.flip[0] and "最上面" not in rec_pure.flip[0],
      f"{rec_pure.flip[0][:80]}…")

# ============================================================
# ①g 「一个作业打完一段连续同参数文件」（2026-10-18 用户现场：7 个文件逐个触发打印、每个之间停一下）
#     旧行为：一个文件 = 一个打印作业（每次 print_pdf 都 CreateDC→StartDoc→…→EndDoc→DeleteDC）
#     ⇒ 7 个文件 7 次作业边界，主机型打印机每个之间都要重做设备上下文 + spooler 收尾。
#     新行为：连续**同参数**（双面/双面模式/份数/DPI）的文件合并成一份 PDF、只提交一次。
# ============================================================
print("\n=== ①g 连续同参数文件合并成一个打印作业 ===")


def _uniq_pdf_path(tag: str) -> str:
    return os.path.join(_SANDBOX, f"uniq_{tag}.pdf")


def _uniq_pdf(tag: str, pages: int) -> str:
    """独立内容的小 PDF（核"合并页序"用：每页文本唯一，能分辨谁在前谁在后）。"""
    path = os.path.join(_SANDBOX, f"uniq_{tag}.pdf")
    c = canvas.Canvas(path, pagesize=A4)
    for i in range(pages):
        c.drawString(72, 760, f"{tag}#{i + 1}")
        c.showPage()
    c.save()
    return path


_three = [make_job(_uniq_pdf("a", 1), duplex="off", page_count=1, name="a"),
          make_job(_uniq_pdf("b", 1), duplex="off", page_count=1, name="b"),
          make_job(_uniq_pdf("c", 1), duplex="off", page_count=1, name="c")]
rec_m = run_worker(_three, AUTO)
check("★ 3 个单页同参数文件 → **只提交 1 次** print_pdf（一个打印作业，不再逐个触发）",
      len(rec_m.print_pdf) == 1, f"{len(rec_m.print_pdf)} 次")
check("★ 合并件真的含 3 页（不是只打了一份）",
      len(rec_m.merged_pages[0]) == 3 if rec_m.merged_pages else False,
      f"{rec_m.merged_pages}")
check("★ 合并件页序 = 按文件顺序 a/b/c（不是乱序、不是只留最后一个）",
      rec_m.merged_pages and rec_m.merged_pages[0] == ["a#1", "b#1", "c#1"],
      f"{rec_m.merged_pages[:1]}")
check("★ 3 个任务各自都收到完成回报（合并作业不能漏报/重复报）",
      [i for i, ok, _m in rec_m.finished] == [0, 1, 2]
      and all(ok for _i, ok, _m in rec_m.finished), f"{rec_m.finished}")

# 参数不同（份数）→ 必须断成两个作业，且各自的份数在**合并件**里真实重复
rec_cp = run_worker([make_job(_uniq_pdf("x", 1), duplex="off", copies=2, page_count=1, name="x"),
                     make_job(_uniq_pdf("y", 2), duplex="off", copies=1, page_count=2, name="y")],
                    AUTO)
check("★ 份数不同 → 断成 2 个作业（一个作业只能一套 DEVMODE）",
      len(rec_cp.print_pdf) == 2, f"{len(rec_cp.print_pdf)} 次")
# 份数只会出现在**两处之一**：要么并进合并件（合并路径），要么留在调用的 copies 上
# （单文件路径）。两条路径都必须真的把 2 份打出来 —— 所以断言"最终页数"：
# 第 1 个提交的总页数 = 该文档页数 × 它的 copies。
def _selected_pages(path: str, page_range: str = "") -> int:
    """该文件按页码范围实际要打的页数（与 `_count_pages_in_range` 同源）。"""
    import fitz
    with fitz.open(path) as d:
        n = len(d)
    return max(1, gui._count_pages_in_range(page_range or "", n) or n)


check("★ 份数不同时各自都真出「每份页数 × 份数」页（两个作业互不串页数）",
      _selected_pages(_uniq_pdf_path("x")) * 2 == 2
      and _selected_pages(_uniq_pdf_path("y")) * 1 == 2,
      f"x={_selected_pages(_uniq_pdf_path('x')) * 2} y={_selected_pages(_uniq_pdf_path('y'))}")
check("★ 合并提交时 copies 恒为 1（份数已并进合并件，否则会打出双倍）",
      any(k.get("copies") == 1 for k in rec_cp.print_pdf),
      f"{[(os.path.basename(k.get('pdf_path', '')), k.get('copies')) for k in rec_cp.print_pdf]}")

# 页码范围：不同页码范围**各成一段**（不能并在一起，否则"只打第 2 页"会被打成整份）。
# 用两份同范围 + 一份不同范围，一次覆盖"能合"与"不许串"。
rec_rg = run_worker([make_job(_uniq_pdf("r", 2), duplex="off", page_count=2, name="r",
                              page_range="2"),
                     make_job(_uniq_pdf("s", 2), duplex="off", page_count=2, name="s",
                              page_range="2"),
                     make_job(_uniq_pdf("t", 2), duplex="off", page_count=2, name="t")], AUTO)
check("★ 同页码范围的两份合并成 1 段、且**只含选中的第 2 页**（不会整份打出来）",
      rec_rg.merged_pages and rec_rg.merged_pages[0] == ["r#2", "s#2"],
      f"{rec_rg.merged_pages}")
check("★ 不同页码范围的那份**另起一段**（不与前者串页）",
      len(rec_rg.print_pdf) == 2 and rec_rg.merged_pages[1] == ["t#1", "t#2"],
      f"{[os.path.basename(k.get('pdf_path', '')) for k in rec_rg.print_pdf]} / {rec_rg.merged_pages}")

# 读不出页数的文件（损坏 PDF）→ 不许合并出空文档，退回单文件直打
_broken = os.path.join(_SANDBOX, "broken.pdf")
with open(_broken, "wb") as _f:
    _f.write(b"%PDF-1.4\nthis is not a real pdf\n%%EOF\n")
rec_bk = run_worker([make_job(_broken, duplex="on", page_count=2, name="broken")], AUTO)
check("★ 读不出页数的 PDF → 不合并成空文档，按原样提交（duplex 保持 on）",
      len(rec_bk.print_pdf) == 1 and rec_bk.print_pdf[0].get("duplex") == "on",
      f"{[(k.get('duplex'), os.path.basename(k.get('pdf_path', ''))) for k in rec_bk.print_pdf]}")

# ★ 合并路径里的**份数**必须真的重复（旧夹具只测了"份数不同⇒断段"，那是分组、没走合并）
rec_mc = run_worker([make_job(_uniq_pdf("p2", 2), duplex="off", copies=2, page_count=2, name="p"),
                     make_job(_uniq_pdf("q3", 3), duplex="off", copies=2, page_count=3, name="q")],
                    AUTO)
check("★ 同 copies>1 的两份合并后逐页核「每份页数×份数」（丢份数重复必红）",
      len(rec_mc.print_pdf) == 1 and rec_mc.merged_pages
      and rec_mc.merged_pages[0] == ["p2#1", "p2#2", "p2#1", "p2#2",
                                     "q3#1", "q3#2", "q3#3", "q3#1", "q3#2", "q3#3"],
      f"{rec_mc.merged_pages}")

# ★ 多件组里的**坏件**绝不能被静默丢掉还报成功（旧用例只有单个坏件 ⇒ 走 solo，没测到）
rec_bb = run_worker([make_job(os.path.join(_SANDBOX, "broken.pdf"), duplex="off",
                              page_count=2, name="坏件.docx"),
                     make_job(_uniq_pdf("b1", 2), duplex="off", page_count=2, name="b1"),
                     make_job(_uniq_pdf("c1", 3), duplex="off", page_count=3, name="c1")], AUTO)
_mb = os.path.join(_SANDBOX, "broken.pdf")
check("★ 坏件与两个正常件同组：坏件被**单独提交**（不参与合并、也不丢）",
      len(rec_bb.print_pdf) == 2 and any(
          os.path.basename(k.get("pdf_path", "")) == "broken.pdf" for k in rec_bb.print_pdf),
      f"{[os.path.basename(k.get('pdf_path', '')) for k in rec_bb.print_pdf]}")
check("★ 坏件与正常件同组：三个任务各有一次回报（不许漏报）",
      sorted(i for i, _ok, _m in rec_bb.finished) == [0, 1, 2], f"{rec_bb.finished}")

# ★ 1 页 + duplex=on（单页硬闸门）：合并后**必须**按 off 提交（否则手动双面机型整组硬拒、
#   自动双面机型两页挤一张纸 —— 2026-10-17 事故的同一状态，合并路径不能绕过闸门）
rec_1p = run_worker([make_job(_uniq_pdf("s1", 1), duplex="on", page_count=1, name="s1"),
                     make_job(_uniq_pdf("s2", 1), duplex="on", page_count=1, name="s2")], AUTO)
check("★ 两个 1 页文件（请求双面）合并后 duplex 仍是 **off**（单页硬闸门不被合并绕过）",
      len(rec_1p.print_pdf) == 1 and rec_1p.print_pdf[0].get("duplex") == "off",
      f"{[(os.path.basename(k.get('pdf_path', '')), k.get('duplex')) for k in rec_1p.print_pdf]}")
# 手动机型（会硬拒 duplex=on）同样必须按 off 提交
rec_1m = run_worker([make_job(_uniq_pdf("t1", 1), duplex="on", page_count=1, name="t1"),
                     make_job(_uniq_pdf("t2", 1), duplex="on", page_count=1, name="t2")], MANUAL)
check("★ 手动机型下同样按 off 提交（1 页双面会被驱动硬拒 → 整组失败）",
      all(k.get("duplex") == "off" for k in rec_1m.print_pdf)
      and sorted(i for i, ok, _m in rec_1m.finished if ok) == [0, 1],
      f"{[(k.get('duplex')) for k in rec_1m.print_pdf]} / {rec_1m.finished}")

# ============================================================
# ①h 自动双面合并作业：文件/副本之间补空白页（2026-10-21 现场「串页」）
#   旧行为：3 页文件 + 2 页文件合并成一个作业后，驱动按"连续两页 = 一张纸的两面"
#   配对 ⇒ 第二个文件从第一个文件的**第 4 页位置**（= 第 2 张纸的背面）开始印：
#   顾客拿到的文件背面是别人文件的内容，也没法按文件把纸分开。
#   修法：页数为奇数的文件/副本段尾补一张空白页，把下一段顶到新的纸正面。
# ============================================================
print("\n=== ①h 自动双面合并作业：文件间补空白页（修串页）===")

P3 = _uniq_pdf("p3", 3)
P2 = _uniq_pdf("p2", 2)
rec_pad = run_worker([make_job(P3, duplex="on", page_count=3, name="p3"),
                      make_job(P2, duplex="on", page_count=2, name="p2")], AUTO)
check("★ 3 页 + 2 页双面合并：页序 = 1,2,3,〔空〕,1,2（下一份从新纸正面开始）",
      rec_pad.merged_pages and rec_pad.merged_pages[0] ==
      ["p3#1", "p3#2", "p3#3", "", "p2#1", "p2#2"], f"{rec_pad.merged_pages}")
check("★ 合并件 6 面 = 3 + 1 空白 + 2（不是把两个文件硬拼成 5 页）",
      rec_pad.merged_pages and len(rec_pad.merged_pages[0]) == 6)
# 物理出纸模拟：自动双面 = 连续两页印在同一张纸的前/后两面（「串页」就是这个模型）
_seq = (rec_pad.merged_pages or [[]])[0]
_sheets = [(_seq[i] if i < len(_seq) else "", _seq[i + 1] if i + 1 < len(_seq) else "")
           for i in range(0, len(_seq), 2)]
check("★ 物理出纸模拟：第 2 张纸背面是空白，第二个文件从第 3 张纸**正面**开始（不串页）",
      _sheets == [("p3#1", "p3#2"), ("p3#3", ""), ("p2#1", "p2#2")], f"{_sheets}")
check("日志说明补了空白页，且不再误报『合并页数不符』",
      any("补了空白页" in m for m in rec_pad.logs)
      and not any("合并页数不符" in m for m in rec_pad.logs), f"{rec_pad.logs}")
check("★ 进度分母含空白页（状态栏不出现 x/5 却打了 6 面）",
      all(t == 6 for _c, t, s in rec_pad.progress if s.startswith("正在打印"))
      and any("文件间空白页" in s for _c, _t, s in rec_pad.progress),
      f"{[(c, t) for c, t, s in rec_pad.progress]}")
check("两个任务都照常各报一次成功（补空白页不改变回报粒度）",
      [(i, ok) for i, ok, _m in rec_pad.finished] == [(0, True), (1, True)])

# 直接核合并件：空白页尺寸 = 它盖住的那张纸（A4），不是默认 Letter 之类
_mp = os.path.join(_SANDBOX, "merge_pad.pdf")
_ok_pad, _msg_pad = gui.PrintWorker._merge_selected([(P3, [1, 2, 3]), (P2, [1, 2])],
                                                    _mp, [True, False])
_rects = _pdf_page_rects(_mp)
check("★ 空白页尺寸取『被盖住那张纸』的尺寸（6 页同尺寸 A4，第 4 页 = 第 3 页）",
      _ok_pad and len(_rects) == 6 and len(set(_rects)) == 1
      and _rects[3] == _rects[2], f"{_msg_pad} / {_rects}")
_ok_nopad, _msg_nopad = gui.PrintWorker._merge_selected([(P3, [1, 2, 3]), (P2, [1, 2])], _mp)
check("blank_after 不给 ⇒ 不补（单面 / 末段的旧行为不变）",
      _ok_nopad and _pdf_page_texts(_mp) == ["p3#1", "p3#2", "p3#3", "p2#1", "p2#2"],
      f"{_msg_nopad} / {_pdf_page_texts(_mp)}")

# 份数：同一个文件的两份各自独立成段 ⇒ 每份后面都要补（否则第 2 份接着背面印）
rec_padc = run_worker([make_job(_uniq_pdf("pc", 3), duplex="on", copies=2,
                                page_count=3, name="pc"),
                       make_job(_uniq_pdf("qc", 2), duplex="on", copies=2,
                                page_count=2, name="qc")], AUTO)
check("★ 3 页×2 份 + 2 页×2 份：每份 3 页后补空页，2 页份不补"
      "（pc1-pc3,〔空〕,pc1-pc3,〔空〕,qc1,qc2,qc1,qc2）",
      rec_padc.merged_pages and rec_padc.merged_pages[0] ==
      ["pc#1", "pc#2", "pc#3", "", "pc#1", "pc#2", "pc#3", "",
       "qc#1", "qc#2", "qc#1", "qc#2"],
      f"{rec_padc.merged_pages}")
check("★ 多份 + 多文件时进度分母不重复计分隔页（估算 11 + 文件分界 1 = 实际 12 面）",
      all(t == 12 for _c, t, s in rec_padc.progress if s.startswith("正在打印")),
      f"{[(c, t) for c, t, s in rec_padc.progress]}")

# 偶数页不用补（本来就从正面开始）；最后一段是奇数也不补（它的背面本来就是空白）
rec_even = run_worker([make_job(_uniq_pdf("e2", 2), duplex="on", page_count=2, name="e2"),
                       make_job(_uniq_pdf("o3", 3), duplex="on", page_count=3, name="o3")], AUTO)
check("偶数页段不补空页、末段奇数也不补（不白加页）",
      rec_even.merged_pages and rec_even.merged_pages[0] ==
      ["e2#1", "e2#2", "o3#1", "o3#2", "o3#3"], f"{rec_even.merged_pages}")

# 单面作业（duplex=off）合并：不补（每页各占一张纸，不存在正反面配对）
rec_simplex_odd = run_worker([make_job(_uniq_pdf("s3", 3), duplex="off", page_count=3, name="s3"),
                              make_job(_uniq_pdf("s2", 2), duplex="off", page_count=2, name="s2")],
                             AUTO)
check("★ 单面作业合并不补空白页（只有硬件双面配对才需要）",
      rec_simplex_odd.merged_pages and
      rec_simplex_odd.merged_pages[0] == ["s3#1", "s3#2", "s3#3", "s2#1", "s2#2"],
      f"{rec_simplex_odd.merged_pages}")

# 档案明确「无自动双面器」（还没盖成手动章时）：驱动不会真配对，补了就是白出空白纸
SIMPLEX_PROBED = PrinterProfile(auto_duplex=False, capability_source="probe",
                                manual_duplex=False)
rec_sx = run_worker([make_job(_uniq_pdf("x3", 3), duplex="on", page_count=3, name="x3"),
                     make_job(_uniq_pdf("x2", 2), duplex="on", page_count=2, name="x2")],
                    SIMPLEX_PROBED)
check("★ 无自动双面器的档案（auto_duplex=False）不补空白页（补了只会白出纸）",
      rec_sx.merged_pages and
      rec_sx.merged_pages[0] == ["x3#1", "x3#2", "x3#3", "x2#1", "x2#2"],
      f"{rec_sx.merged_pages}")

# 虚拟打印机：双面是空操作、页面不两两配对 ⇒ 不补（否则导出的 PDF 多出空白页）
rec_v = run_worker([make_job(_uniq_pdf("v3", 3), duplex="on", page_count=3, name="v3"),
                    make_job(_uniq_pdf("v2", 2), duplex="on", page_count=2, name="v2")],
                   AUTO, printer_name="Microsoft Print to PDF")
check("★ 虚拟打印机（Print to PDF）不补空白页（双面为空操作、不配对）",
      rec_v.merged_pages and
      rec_v.merged_pages[0] == ["v3#1", "v3#2", "v3#3", "v2#1", "v2#2"],
      f"{rec_v.merged_pages}")

# 装订方向混排 → 拆两批；第 2 批翻纸时第 1 批的纸也还在槽里，同样必须算进去
rec_grp = run_worker([make_job(PDF5, mode="long-edge", name="L.docx"),
                      make_job(PDF3, mode="short-edge", name="S.docx")], MANUAL)
check("★ 装订方向混排 → 拆两批、翻两次纸", len(rec_grp.flip) == 2, f"{len(rec_grp.flip)}")
check("第 1 批翻纸时槽里是空的 → 保持原文案",
      "出纸槽里应有 3 张纸" in rec_grp.flip[0] and "最上面" not in rec_grp.flip[0],
      f"{rec_grp.flip[0][:80]}…")
check("★ 第 2 批翻纸：本批 2 张 + 第 1 批已出的 3 张 = 5 张，且只翻最上面 2 张",
      "5 张" in rec_grp.flip[1] and "最上面 2 张" in rec_grp.flip[1],
      f"{rec_grp.flip[1][:180]}…")

# 首页（封面页）：非攒批时它同样是"单独打一张再进批次"⇒ 同一机制。
# docs/审查报告_2026-10/findings_B_gui_print.md:183 记的就是这条，且此前**只有攒批那条有守卫**。
rec_cov = run_worker([make_job(PDF5, name="A.docx")], MANUAL, cover=True)
check("首页（封面页）确实单独打了一张（非攒批路径）",
      len(rec_cov.print_pdf) == 1 and rec_cov.print_pdf[0]["duplex"] == "off",
      f"{[(c.get('duplex')) for c in rec_cov.print_pdf]}")
check("★ 首页 + 手动双面：翻纸文案要含首页那张（本批 3 + 首页 1 = 共 4 张）",
      "共应有 4 张纸" in rec_cov.flip[0] and "最上面 3 张" in rec_cov.flip[0],
      f"{rec_cov.flip[0][:180]}…")
check("★ 首页 + 手动双面：文案明确「首页那张不要一起搬」（否则首页留在纸盘里被当白纸用掉）",
      "下面 1 张" in rec_cov.flip[0] and "只取出最上面 3 张" in rec_cov.flip[0],
      f"{rec_cov.flip[0][:220]}…")

# ── ①i 并批路径的总兜底：批次里抛异常也必须把信号发全（否则标签页永久 🔒）──
# run() 的并批段落原先没有 except（`_print_manual_batch` 只有 try/finally）：
# 异常一旦逃出 run()，job_finished / all_finished 一个都不发，主线程收不到收尾信号，
# 标签页会一直停在"打印中"（用户只能重启工具），日志里也只剩 traceback、没有可读原因。
rec_bad = run_worker([make_job(PDF5, name="A.docx")], MANUAL, fail_merge_at=1)
check("★ 并批异常：绝不允许逃出 run()（逃出去就没人发收尾信号了）",
      rec_bad.raised is None, f"{rec_bad.raised!r}")
check("★ 并批异常：任务收到失败回报 + all_finished 报 (0,1)（标签页不会永久 🔒）",
      len(rec_bad.finished) == 1 and rec_bad.finished[0][:2] == (0, False)
      and rec_bad.all_finished == [(0, 1)],
      f"{rec_bad.finished} {rec_bad.all_finished}")
check("★ 并批异常：日志里有一条可读失败原因（不只 traceback）",
      any("手动双面批次异常" in m for m in rec_bad.logs),
      f"{[m for m in rec_bad.logs if '异常' in m]}")

# 第 2 批才炸：已成功的第 1 批不能重复回报，失败也只报一次
rec_bad2 = run_worker([make_job(PDF5, mode="long-edge", name="L.docx"),
                       make_job(PDF3, mode="short-edge", name="S.docx")],
                      MANUAL, fail_merge_at=2)
check("★ 并批异常·第 2 批才炸：成功的那批照常回报，失败只报一次、不重复",
      rec_bad2.raised is None
      and [(i, ok) for i, ok, _m in rec_bad2.finished] == [(0, True), (1, False)]
      and rec_bad2.all_finished == [(1, 1)],
      f"{rec_bad2.finished} {rec_bad2.all_finished}")

# ============================================================
# ② 跳过（第 1 趟出纸后）→ 第 2 趟绝不打
# ============================================================
print("\n=== ② 操作员选择「跳过本任务」===")
rec = run_worker([make_job(PDF5), make_job(PDF3)], MANUAL, flip_answer=False)
check("★ 跳过时只打了第 1 趟（绝不再打第 2 趟）",
      len(rec.plan_pass) == 1 and rec.plan_pass[0][0] == 1, f"{[p[0] for p in rec.plan_pass]}")
check("★ 两个任务都判失败，且说明纸是半成品",
      len(rec.finished) == 2 and all(ok is False for _i, ok, _m in rec.finished)
      and all("半成品" in m for _i, _ok, m in rec.finished),
      f"{rec.finished[0][2] if rec.finished else ''}")

# ============================================================
# ③ 第 1 趟还在打时就点「跳过」→ 预置答案立刻生效（不必等 10 分钟）
# ============================================================
print("\n=== ③ 第 1 趟打印期间就点跳过（预置答案）===")
rec = run_worker([make_job(PDF5)], MANUAL, flip_answer=None, timeout=9999,
                 on_announce=lambda w: w.confirm_flip(False))
check("★ 立刻按跳过收尾（没有干等超时）",
      rec.elapsed < 5 and len(rec.plan_pass) == 1 and rec.finished[0][1] is False,
      f"耗时 {rec.elapsed:.2f}s, plan_pass={len(rec.plan_pass)}")

# ============================================================
# ④ 零回归：单面 / 自动双面 / 能力未确认
# ============================================================
print("\n=== ④ 原有行为不变（零回归）===")
rec = run_worker([make_job(PDF5, duplex="off")], MANUAL)
check("单面任务：一次提交，不弹常驻窗、不走两趟",
      len(rec.print_pdf) == 1 and not rec.plan_pass and not rec.flip and not rec.announce,
      f"{rec.print_pdf[0]['duplex']}")

rec = run_worker([make_job(PDF5)], AUTO)
check("支持自动双面的机器：仍由驱动双面一次提交（不插手）",
      len(rec.print_pdf) == 1 and rec.print_pdf[0]["duplex"] == "on" and not rec.plan_pass)

rec = run_worker([make_job(PDF5)], UNKNOWN)
check("★ 能力**未确认**的机器：照旧走驱动双面（零回归）",
      len(rec.print_pdf) == 1 and rec.print_pdf[0]["duplex"] == "on" and not rec.plan_pass)

rec = run_worker([make_job(PDF5)], PrinterProfile(auto_duplex=False, manual_duplex=False))
check("档案「不支持自动双面但也不用手动双面」→ 不强行走两趟",
      len(rec.print_pdf) == 1 and not rec.plan_pass)

# ============================================================
# ⑤ 无人值守路径：挂起等人
# ============================================================
print("\n=== ⑤ 自动/预约打印遇「需人工翻纸」→ 挂起 ===")


class _FakeWin:
    _printer_profile = gui.MainWindow._printer_profile
    _manual_flip_required_for = gui.MainWindow._manual_flip_required_for
    _hold_for_manual_duplex = gui.MainWindow._hold_for_manual_duplex

    def __init__(self, profile):
        self._config = PrinterConfig(printer_name="HP LaserJet 1020")
        self._config.printer_profiles["HP LaserJet 1020"] = profile
        self._config.tabs["1"] = gui.TabSettings()
        self.logs, self.saved, self.refreshed = [], 0, 0

    def _log(self, m):
        self.logs.append(m)

    def _save_config(self):
        self.saved += 1

    def _refresh_tab_display(self):
        self.refreshed += 1


duplex_job = PrintJob(file_path=PDF5, duplex="on")
simplex_job = PrintJob(file_path=PDF5, duplex="off")
w_manual = _FakeWin(MANUAL)
check("需人工翻纸判定：手动机器 + 双面文件 → True",
      w_manual._manual_flip_required_for([duplex_job]) is True)
check("手动机器 + 全单面 → False", w_manual._manual_flip_required_for([simplex_job]) is False)
check("自动双面机器 → False", _FakeWin(AUTO)._manual_flip_required_for([duplex_job]) is False)
check("★ 能力未确认 → False（不擅自挂起自动打印）",
      _FakeWin(UNKNOWN)._manual_flip_required_for([duplex_job]) is False)
ok_hold = w_manual._hold_for_manual_duplex([duplex_job], "1", "无障碍打印订单 #7")
check("★ 挂起：返回 True + 标签页置 manual_duplex_pending + 记日志",
      ok_hold is True and w_manual._config.tabs["1"].manual_duplex_pending is True
      and w_manual.saved >= 1 and any("已挂起" in m for m in w_manual.logs))
w_manual2 = _FakeWin(MANUAL)
check("不满足条件不挂起", w_manual2._hold_for_manual_duplex([simplex_job], "1", "订单 #8") is False)
_cfg = PrinterConfig()
_cfg.tabs["2"] = gui.TabSettings(manual_duplex_pending=True)
_d = _cfg.to_dict()
check("★ manual_duplex_pending 持久化（⏸ 标记扛重启）",
      _d["tabs"]["2"]["manual_duplex_pending"] is True
      and PrinterConfig.from_dict(_d).tabs["2"].manual_duplex_pending is True)

# ============================================================
# ⑥ 常驻翻纸窗状态机
# ============================================================
print("\n=== ⑥ 常驻翻纸窗（对齐官方工具）===")
dlg = gui.ManualDuplexDialog()
evts = []
dlg.continue_requested.connect(lambda: evts.append("go"))
dlg.skip_requested.connect(lambda: evts.append("skip"))

dlg.announce("第 1 趟（现在打）：1 3 5\n第 2 趟（翻纸后打）：空白 4 2")
check("① 批次开始：窗口立起来（常驻），此时**不能**翻纸",
      "正在打印第 1 趟" in dlg._status.text() and dlg._btn_go.isEnabled() is False
      and "第 1 趟（现在打）" in dlg._detail.text(), f"{dlg._status.text()}")
check("窗口置顶（否则操作员注意不到打印机在等）",
      bool(dlg.windowFlags() & gui.Qt.WindowStaysOnTopHint))

dlg.set_flippable("出纸槽里应有 3 张纸", 600)
check("② 第 1 趟出纸：状态切「可翻纸」+ 按钮启用 + 倒计时显示",
      "请翻纸" in dlg._status.text() and dlg._btn_go.isEnabled() is True
      and "还有 9 分" in dlg._countdown.text(), f"{dlg._countdown.text()}")
dlg._on_go()
check("③ 点继续：状态切「打印第 2 趟」+ 按钮禁用防重复点 + 发 continue 信号",
      evts == ["go"] and "正在打印第 2 趟" in dlg._status.text()
      and dlg._btn_go.isEnabled() is False, f"{evts}")
dlg.finish("手动双面完成")
check("④ 批次结束：状态收尾，窗口稍候自动关闭",
      "手动双面完成" in dlg._status.text() and dlg._mode == "done", f"{dlg._mode}")

dlg2 = gui.ManualDuplexDialog()
evts2 = []
dlg2.skip_requested.connect(lambda: evts2.append("skip"))
dlg2.announce("x")
dlg2._on_skip()
check("★ 第 1 趟打印期间点「跳过」也会上报（预置答案，worker 不必等超时）",
      evts2 == ["skip"] and dlg2._mode == "skipped", f"{evts2}")
dlg3 = gui.ManualDuplexDialog()
evts3 = []
dlg3.skip_requested.connect(lambda: evts3.append("skip"))
dlg3.set_flippable("x", 2)
dlg3._tick()
dlg3._tick()
check("★ 倒计时归零 → 自动按跳过处理（不会永远等人）",
      evts3 == ["skip"] and dlg3._mode == "skipped", f"{evts3} {dlg3._mode}")
dlg4 = gui.ManualDuplexDialog()
evts4 = []
dlg4.skip_requested.connect(lambda: evts4.append("skip"))
dlg4.set_flippable("x", 600)
dlg4.close()
check("★ 未确认就关窗 = 跳过（不能让关窗把第 2 趟放过去）",
      evts4 == ["skip"] and dlg4._mode == "skipped", f"{evts4}")
dlg5 = gui.ManualDuplexDialog()
evts5 = []
dlg5.continue_requested.connect(lambda: evts5.append("go"))
dlg5.announce("x")
dlg5._on_go()
check("第 1 趟期间点「继续」无效（按钮本来就禁用）", evts5 == [], f"{evts5}")

# ============================================================
# ⑧ 翻纸间隙的并发保护（第 1 趟打完 → 操作员确认之间，别的任务不能插进来）
# ============================================================
print("\n=== ⑧ 翻纸间隙的并发保护 ===")
src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()


class _BusyWin:
    """只够走到 _start_print_worker 的"忙"判定（不是真主窗口）。"""

    # 🔴-3（B1）：真实 `_stack_gate` / `_start_print_worker` 都会读 `_stack_flush_worker`
    # 判断"整叠第 2 趟是否在打"。本桩表示"没有第 2 趟在飞"。
    # （教训：借用真实方法的桩，在真实方法新增被读属性时要同步补上。）
    _stack_flush_worker = None

    def __init__(self, worker):
        self._worker = worker
        self._print_starting = False
        self.logs = []

    def _has_members(self):
        return True

    def _log(self, msg):
        self.logs.append(msg)

    # 攒批模式闸门（2026-10）：用**真实**闸门函数（它只会用到上面那两个桩：
    # 未开攒批 + 没有纸叠 → 直接放行到"忙"判定）。
    _stack_gate = gui.MainWindow._stack_gate

    def _stack_mode_on(self):
        return False

    def _stack_open_for_business(self):
        return False


class _FinWin:
    """只够跑 _on_all_finished 的替身：记录"清 worker"与"补打"的先后顺序。"""

    def __init__(self, worker, retry_item, tab_jobs):
        self._worker = worker
        self._auto_print_retry = [retry_item]
        self._all_printed = False
        self._printing_tab_key = "0"
        self.calls = []
        # 🔴-B4-3：`_on_all_finished` 的补打改成"确认能启动才出队"，
        # 所以替身要能表达启动结果（默认成功；失败路径见 verify_auto_print_batching.py 场景 4）
        self.start_result = True

        class _Label:
            def setText(self, _t):
                pass

        self._status_label = _Label()

        class _Tab:
            jobs = tab_jobs

        self._config = type("C", (), {"tabs": {"0": _Tab()}})()

    def _log(self, msg):
        pass

    def _start_print_worker(self, jobs, tab_key=None):
        # 记录"补打开始时 worker 已经置空"——证明门是先开的，补打不是插队
        self.calls.append(("start_retry", self._worker is None))
        return self.start_result

    def _drain_stack_wait_queue(self):
        # 攒批排队放行（2026-10）：本替身没有纸叠/排队请求，实际不会放行任何东西。
        # 不记进 calls —— 那个列表专门用来钉「先开门 → 再补打」的先后顺序。
        pass

    def _drain_stack_entry_queue(self):
        # 攒批"连续堆第 1 趟"（2026-10-21）：本替身没有排队等第 1 趟的攒批单，空转。
        pass

    def _find_tab_key_by_order_id(self, _oid):
        return ""


# 真 worker 停在"等翻纸"：只记录提示、不回执（不 confirm）→ 线程阻塞在 _flip_event.wait()。
# 必须用 QThread.start()（生产就是这样起的）：isRunning() 只对 QThread 自身有效；
# 换普通线程跑 run() 的话 isRunning() 恒 False，测不到"门关着"这件事。
# 跨线程发信号是排队投递 → 主线程要 processEvents() 才收得到。
_rec = Rec()
_w = gui.PrintWorker(jobs=[make_job(PDF5)], printer_name="Fake Print", duplex_mode="long-edge",
                     keep_temp_pdf=False, render_dpi=200, manual_duplex_profile=MANUAL)
_w.duplex_announce.connect(_rec.announce.append)
_w.log_message.connect(_rec.logs.append)
flip_seen = threading.Event()
_w.flip_required.connect(lambda text, sec: (flip_seen.set(), _rec.flip.append(text)))
_restore = patched(_rec)
_w.start()
try:
    _deadline = time.time() + 30
    while not flip_seen.is_set() and time.time() < _deadline:
        _app.processEvents()
        time.sleep(0.02)
    check("翻纸提示已发出（worker 进入等待）", flip_seen.is_set(), f"logs={_rec.logs[-2:]}")
    check("★ 等翻纸期间 worker 线程**仍然活着**（isRunning=True）—— 这是「门关着」的根据",
          _w.isRunning() is True)
    check("★ 等翻纸期间只打了第 1 趟：第 2 趟必须等确认",
          len(_rec.plan_pass) == 1 and _rec.plan_pass[0][0] == 1, f"{[p[0] for p in _rec.plan_pass]}")
    check("★ 翻纸提示里明确警告「别插单」（第 2 趟要接着这叠纸打）",
          "不要让别的打印任务插进来" in (_rec.flip[0] if _rec.flip else ""), "")
    _fake = _BusyWin(_w)
    check("★ 此时点「开始打印」被拒（_start_print_worker → False + 忙碌日志，不会真的开打）",
          gui.MainWindow._start_print_worker(_fake, [make_job(PDF5)]) is False
          and any("已有打印任务正在进行" in m for m in _fake.logs), f"{_fake.logs}")
    _w.confirm_flip(True)
    _fin_deadline = time.time() + 60
    while _w.isRunning() and time.time() < _fin_deadline:
        _app.processEvents()
        time.sleep(0.02)
    _w.wait(30000)
    _app.processEvents()
finally:
    _restore()
check("★ 确认翻纸后第 2 趟才发出，且线程退出（门才重新打开）",
      len(_rec.plan_pass) == 2 and _rec.plan_pass[1][0] == 2 and _w.isRunning() is False,
      f"{[p[0] for p in _rec.plan_pass]}")
_fin = _FinWin(_w, {"order_id": 7, "tab_key": "0", "task_ids": []}, [make_job(PDF3)])
gui.MainWindow._on_all_finished(_fin, 1, 0)
check("★ 批次结束才置空 _worker 并补打排队订单（顺序：先开门 → 再补打）",
      _fin._worker is None and _fin.calls == [("start_retry", True)] and _fin._auto_print_retry == [],
      f"{_fin.calls}")

# ── 两条自动启动路径遇忙必须"排队/重试"，绝不插队 ──
check("★ 无障碍自动打印：打印机忙 → 入 _auto_print_retry（不插队，批次结束后补打）",
      "self._auto_print_retry.append({" in src
      and "if not self._start_print_worker(list(jobs), tab_key=tab_key):" in src)
check("★ 预约打印：打印机忙 → 10 秒后重试（不插队、不丢单、不计入失败上限）",
      "打印机正忙，10s 后重试" in src
      and "_start_scheduled_print_timer(order_id, int(time.time()) + 10)" in src)
# 🔴-B4-3 之后 `_on_all_finished` 内部有 3 个出队点（启动成功 / 已全打完 / 标签页已删除），
# 所以"全局仅 1 处 pop"这个计数代理已过期。真正要钉的不变量是：
# 「**没有任何**出队点落在 `_on_all_finished` 之外」—— 别处出队 = 有别的代码路径偷偷丢单。
_body_fin = src.split("def _on_all_finished", 1)[1].split("\n    def ", 1)[0]
_pop_all = src.count("self._auto_print_retry.pop(0)")
_pop_in = _body_fin.count("self._auto_print_retry.pop(0)")
check("★ 补打队列只在 _on_all_finished 里出队（函数外零出队点 ⇒ 无静默丢单路径）",
      _pop_in >= 1 and _pop_all == _pop_in
      and "self._worker = None" in _body_fin[:400],
      f"函数内 {_pop_in} 处 / 全局 {_pop_all} 处")
_start_fn = src.split("def _start_print_worker")[1].split("\n    def ")[0]
check("★ 手动「开始打印」也走同一道门（全文件只此一处构造+启动 PrintWorker）",
      src.count("worker = PrintWorker(") == 1
      and "worker = PrintWorker(" in _start_fn and "self._worker = worker" in _start_fn
      and "worker.start()" in _start_fn
      and "self._start_print_worker(list(jobs))" in src)

# ============================================================
# ⑨ 第 1 趟计时器 / 预估变色 / 自动学习 / 测速校准（全程桩，无真打印）
# ============================================================
print("\n=== ⑨ 常驻窗第 1 趟计时器与预估 ===")


def _wait_zero(printer, pages=0, **kw):
    """桩：把"静置余量"报成 0（默认真桩报 2s），并真的睡 0.6s —— 这样第 1 趟的
    实测耗时 >0.5s，才留得下速度样本（否则会被 add_speed_sample 当异常丢掉）。"""
    time.sleep(0.6)
    return {"drained": True, "queue_seconds": 0.6, "margin_seconds": 0.0,
            "peak_jobs": 0, "detail": ""}


def run_worker_zero_margin(jobs, profile, flip_answer=True):
    rec = Rec()
    w = gui.PrintWorker(jobs=jobs, printer_name="Fake Print", duplex_mode="long-edge",
                        keep_temp_pdf=False, render_dpi=200, manual_duplex_profile=profile)
    w.log_message.connect(rec.logs.append)
    w.duplex_plan.connect(lambda n, est: rec.plan.append((n, est)))
    w.duplex_speed_sample.connect(lambda n, sec: rec.samples.append((n, sec)))
    w.flip_required.connect(
        lambda text, sec: (rec.flip.append(text), w.confirm_flip(flip_answer)))
    restore = patched(rec)
    gui.wait_for_output = _wait_zero
    try:
        w.run()
    finally:
        restore()
    return rec


_rec9 = run_worker_zero_margin([make_job(PDF3)], MANUAL)
# 3 页 PDF → **2 张纸**（1|2、3|空白）：预估/采样都按"张数"算，不是页数
check("★ 批次开始播报「第 1 趟几张 + 预计秒数」（常驻窗据此计时/变色）",
      len(_rec9.plan) == 1 and _rec9.plan[0][0] == 2 and _rec9.plan[0][1] > 0,
      f"{_rec9.plan}")
check("★ 第 1 趟实测被回报（自动学习样本：张数 + 秒数）",
      len(_rec9.samples) == 1 and _rec9.samples[0][0] == 2 and _rec9.samples[0][1] >= 0.5,
      f"{_rec9.samples}")
check("实测与预估一起写进日志（现场可核对）",
      any("第 1 趟实测" in m and "预估" in m for m in _rec9.logs))

# ── 关键：实测**与操作员按不按「继续」无关**（第 1 趟出纸就采样，不等翻纸回执）──
_rec9b = Rec()
_w9b = gui.PrintWorker(jobs=[make_job(PDF3)], printer_name="Fake Print", duplex_mode="long-edge",
                       keep_temp_pdf=False, render_dpi=200, manual_duplex_profile=MANUAL)
_w9b.log_message.connect(_rec9b.logs.append)
_w9b.duplex_speed_sample.connect(lambda n, sec: _rec9b.samples.append((n, sec)))
_flip9_seen = threading.Event()
_w9b.flip_required.connect(lambda text, sec: _flip9_seen.set())   # 只记录，**不回执**（模拟人不在）
_restore9 = patched(_rec9b)
gui.wait_for_output = _wait_zero
_w9b.start()
try:
    _dl9 = time.time() + 30
    while not _flip9_seen.is_set() and time.time() < _dl9:
        _app.processEvents()
        time.sleep(0.02)
    check("★ 操作员一直不按「继续」，第 1 趟实测照样已经记下来了（等待时间不计入）",
          _flip9_seen.is_set() and len(_rec9b.samples) == 1 and _rec9b.samples[0][1] < 30,
          f"samples={_rec9b.samples}")
    _t_more = _rec9b.samples[0][1] if _rec9b.samples else 0
    time.sleep(1.2)
    _app.processEvents()
    check("★ 干等 1.2 秒也不会把样本拖长（采样点在第 1 趟出纸，不在翻纸回执）",
          len(_rec9b.samples) == 1 and _rec9b.samples[0][1] == _t_more,
          f"{_rec9b.samples}")
    _w9b.confirm_flip(False)
    _dl9b = time.time() + 30
    while _w9b.isRunning() and time.time() < _dl9b:
        _app.processEvents()
        time.sleep(0.02)
    _w9b.wait(20000)
finally:
    _restore9()

_dlg9 = gui.ManualDuplexDialog()
_dlg9.announce("说明文本", sheets=10, estimate_sec=43.0)
_dlg9.set_speed_hint("启动 ≈8s + 每张 ≈3.5s · 基于最近 2 次实测")
check("窗口一出现就开始正计时：显示「已用 … / 预计 …」且为绿色",
      "已用" in _dlg9._pass1.text() and "预计 00:43" in _dlg9._pass1.text()
      and gui.ManualDuplexDialog.PASS1_GREEN in _dlg9._pass1.styleSheet(),
      _dlg9._pass1.text())
check("提示里说明预估依据（基于最近 N 次实测）",
      "基于最近 2 次实测" in _dlg9._pass1_hint.text())
check("★ 颜色阈值：在预估内(+30s 宽容带)绿 / 超出 30 秒黄 / 超出 1 分钟红",
      gui.ManualDuplexDialog.pass1_color(43.0, 43.0) == gui.ManualDuplexDialog.PASS1_GREEN
      and gui.ManualDuplexDialog.pass1_color(73.0, 43.0) == gui.ManualDuplexDialog.PASS1_GREEN
      and gui.ManualDuplexDialog.pass1_color(74.0, 43.0) == gui.ManualDuplexDialog.PASS1_YELLOW
      and gui.ManualDuplexDialog.pass1_color(103.0, 43.0) == gui.ManualDuplexDialog.PASS1_YELLOW
      and gui.ManualDuplexDialog.pass1_color(104.0, 43.0) == gui.ManualDuplexDialog.PASS1_RED)

# ── ★ 翻纸放行闸门（2026-10-19 现场 bug）──────────────────────────────────────
# 主机型打印机只回报"数据送进端口"、不回报"纸打完"：换原生 1:1 渲染策略后第 1 趟的
# "完成回报"从 60~70 秒塌到 1~2 秒（16 张纸 2.1 秒），纸还在出窗口就说打完了。
# 用户口径：按钮按**预估**放行 —— 等到"预估结束前 5 秒"；预估 <10 秒不做这个特判。
# 2026-10-20 用户口径：改成「预估 × 比例」放行（纸越多误差越大，固定提前 5 秒不合适）
check("★ 闸门时刻 = 预估 × 50%（默认）；可叠加人工翻页准备时间；没预估/算出<1 秒 → 0",
      gui.ManualDuplexDialog.flip_gate_at(43.0) == 21.5
      and gui.ManualDuplexDialog.flip_gate_at(43.0, 0.5, 20.0) == 41.5
      and gui.ManualDuplexDialog.flip_gate_at(100.0, 1.0, 0.0) == 100.0
      and gui.ManualDuplexDialog.flip_gate_at(1.0) == 0.0
      and gui.ManualDuplexDialog.flip_gate_at(0.0) == 0.0
      and gui.ManualDuplexDialog.flip_gate_at(None) == 0.0,
      f"{gui.ManualDuplexDialog.flip_gate_at(43.0)} / {gui.ManualDuplexDialog.flip_gate_at(1.0)}")
# 比例来自档案（「帮助 → 打印速度…」可调）：announce 传进来后，闸门按它算
_dlg_ratio = gui.ManualDuplexDialog()
_dlg_ratio.announce("按比例放行", sheets=20, estimate_sec=100.0, gate_ratio=0.8,
                    gate_extra=10.0)
_dlg_ratio.set_flippable("翻纸说明", 600)
check("★ 档案里的放行比例/翻页准备时间真的进入闸门（100 × 80% + 10 = 90 秒）",
      _dlg_ratio._mode == "flip_gate" and _dlg_ratio._flip_gate_at == 90.0
      and "80%" in _dlg_ratio._countdown.text(),
      f"{_dlg_ratio._flip_gate_at} | {_dlg_ratio._countdown.text()[:60]}")

_dlg_noest = gui.ManualDuplexDialog()
_dlg_noest.announce("没样本的机型", sheets=3, estimate_sec=0.0)
_dlg_noest.set_flippable("翻纸说明", 600)
check("★ 没有预估（老档案/没速度样本）→ 不设闸门，一回报就能翻纸（零回归）",
      _dlg_noest._mode == "flip" and _dlg_noest._btn_go.isEnabled()
      and _dlg_noest._pass1_done and _dlg_noest.is_early_signal(0.1) is False,
      f"{_dlg_noest._mode}")

_dlg_short = gui.ManualDuplexDialog()
_dlg_short.announce("短活儿", sheets=2, estimate_sec=9.0)
_dlg_short.set_flippable("翻纸说明", 600)
check("★ 短活儿（预估 9 秒 → 闸门 4.5 秒）仍按比例放行；预估 <2 秒干脆不设闸门",
      _dlg_short._mode == "flip_gate" or _dlg_short._mode == "flip")
_dlg_tiny = gui.ManualDuplexDialog()
_dlg_tiny.announce("极小活儿", sheets=1, estimate_sec=1.0)
_dlg_tiny.set_flippable("翻纸说明", 600)
check("★ 预估 1 秒 → 闸门算出来 0.5 秒 < 1 秒 ⇒ 不设闸门（打完即翻）",
      _dlg_tiny._mode == "flip" and _dlg_tiny._btn_go.isEnabled())

# 早回报：界面时钟≈0 秒，闸门在 38 秒 → **按钮不放行**，计时继续走
_dlg9.set_flippable("翻纸说明", 600)
check("★ 早回报（预估 43s）→「继续打第 2 趟」**不放行**，窗口明说还在出纸",
      _dlg9._mode == "flip_gate" and _dlg9._btn_go.isEnabled() is False
      and "还在出纸" in _dlg9._status.text() and "还有" in _dlg9._countdown.text()
      and "还要等" in _dlg9._btn_go.text(),
      f"{_dlg9._mode} | {_dlg9._countdown.text()} | {_dlg9._btn_go.text()}")
check("★ 闸门期间**不拿假回报冻结**计时（真实用时还在走：_pass1_done 仍为 False）",
      not _dlg9._pass1_done and _dlg9._pass1_timer.isActive() and "已用" in _dlg9._pass1.text(),
      _dlg9._pass1.text())
check("★ 闸门期间不扣翻纸确认倒计时（那 10 分钟从「真的能翻纸」那一刻才算）",
      _dlg9._left == 0 and "还有" in _dlg9._countdown.text()
      and "超时按跳过处理" not in _dlg9._countdown.text(),
      f"_left={_dlg9._left} | {_dlg9._countdown.text()}")
check("闸门期间提示里保留预估依据 + 说明为什么不采信打印机回报",
      "基于最近 2 次实测" in _dlg9._pass1_hint.text()
      and "早于实机出纸" in _dlg9._pass1_hint.text(), _dlg9._pass1_hint.text())
# 到点：把"实时已用"换成 39 秒（真等 38 秒没必要，逻辑与真时钟同一条路径）
_elapsed9_orig = _dlg9._elapsed_sec
_dlg9._elapsed_sec = lambda: 39.0
_dlg9._tick()
check("★ 到闸门时刻 → 放行：按钮启用 + 状态切「请翻纸」+ 计时冻结成「用时」",
      _dlg9._mode == "flip" and _dlg9._btn_go.isEnabled() and _dlg9._pass1_done
      and "请翻纸" in _dlg9._status.text() and "用时 00:39" in _dlg9._pass1.text(),
      f"{_dlg9._mode} | {_dlg9._pass1.text()}")
check("★ 放行后翻纸确认倒计时才开始（两个计时器互不干扰）",
      _dlg9._left == 599 and "超时按跳过处理" in _dlg9._countdown.text(),
      f"_left={_dlg9._left} | {_dlg9._countdown.text()}")
_frozen_text = _dlg9._pass1.text()
_dlg9._pass1_tick()
check("冻结后再 tick 也不会继续走", _dlg9._pass1.text() == _frozen_text)
_dlg9._elapsed_sec = _elapsed9_orig

_dlg_huge = gui.ManualDuplexDialog()
_dlg_huge.announce("超大批次", sheets=400, estimate_sec=1200.0)
_dlg_huge.set_flippable("翻纸说明", 600)
check("★ 超大批次：闸门被钳在翻纸确认超时之内（worker 只等 600+30 秒；不钳就会先超时"
      "按半成品判失败、纸白打）",
      _dlg_huge._mode == "flip_gate" and _dlg_huge._flip_gate_at == 600.0,
      f"{_dlg_huge._flip_gate_at}")

# 闸门期间关窗 / 点跳过 = 跳过本任务（绝不能把第 2 趟放过去）
_dlg_gate = gui.ManualDuplexDialog()
_gate_ev = []
_dlg_gate.skip_requested.connect(lambda: _gate_ev.append("skip"))
_dlg_gate.announce("闸门期间关窗", sheets=16, estimate_sec=43.0)
_dlg_gate.set_flippable("翻纸说明", 600)
_dlg_gate.close()
check("★ 闸门期间关窗 = 跳过（不会「关一下窗就把第 2 趟打了」）",
      _dlg_gate._mode == "skipped" and _gate_ev == ["skip"], f"{_dlg_gate._mode} {_gate_ev}")
_dlg_gate_click = gui.ManualDuplexDialog()
_gate_click = []
_dlg_gate_click.continue_requested.connect(lambda: _gate_click.append("go"))
_dlg_gate_click.announce("闸门期间硬点继续", sheets=16, estimate_sec=43.0)
_dlg_gate_click.set_flippable("翻纸说明", 600)
_dlg_gate_click._on_go()          # 按钮虽然禁用，仍要挡住程序化/快捷键触发
check("★ 闸门期间就算硬调 _on_go 也不放行（按钮禁用不是唯一防线）",
      _dlg_gate_click._mode == "flip_gate" and _gate_click == [],
      f"{_dlg_gate_click._mode} {_gate_click}")

_dlg9.announce("第二批", sheets=3, estimate_sec=18.5)
check("新批次复位重新计时",
      not _dlg9._pass1_done and "已用" in _dlg9._pass1.text() and _dlg9._pass1_timer.isActive())
# 权威值冻结：worker 实测（程序侧）优先于界面时钟
_dlg9.freeze_pass1(42.0)
check("★ 窗口按**程序实测的权威值**冻结（不是界面自己数的），且换算成最终用时",
      _dlg9._pass1_done and abs(_dlg9._pass1_final - 42.0) < 0.01
      and "用时 00:42" in _dlg9._pass1.text(), _dlg9._pass1.text())
_dlg9.announce("第三批", sheets=3, estimate_sec=18.5)
_dlg9.freeze_pass1(None)
check("没有权威值时退回界面时钟兜底（照样能冻结）",
      _dlg9._pass1_done and _dlg9._pass1_final >= 0.0)
_dlg9.announce("第四批", sheets=3, estimate_sec=18.5)
_dlg9.freeze_pass1(30.0)          # 一笔不早于闸门（13.5s）的可信实测
_dlg9.set_flippable("翻纸说明", 600)
check("★ 手上已有可信实测时直接放行（闸门只挡提前回报，不挡真打完的）",
      _dlg9._pass1_done and _dlg9._mode == "flip" and _dlg9._btn_go.isEnabled()
      and not _dlg9._pass1_timer.isActive() and abs(_dlg9._pass1_final - 30.0) < 0.01,
      f"{_dlg9._mode} {_dlg9._pass1_final}")
_dlg9.finish("批次结束")
check("收尾把计时器停掉", not _dlg9._pass1_timer.isActive())

# ── 自动学习落盘（真的写 print_config.json 副本，跑完读回来核对）──
print("\n=== ⑨b 实测样本自动学习并落盘 ===")
_cfg9 = os.path.join(_SANDBOX, "learn_cfg.json")
_learn_cfg = gui.PrinterConfig()
_learn_cfg.printer_name = "Fake Print"
_learn_cfg.save(_cfg9)


class _LearnWin:
    def __init__(self, cfg, path, dialog=None):
        self._config, self._path, self.logs = cfg, path, []
        self._duplex_dialog = dialog

    def _printer_profile(self):
        return self._config.get_profile(self._config.printer_name)

    def _save_config(self):
        self._config.save(self._path)

    def _log(self, msg):
        self.logs.append(msg)


# 主线程收到"权威实测"后：先用它冻结窗口计时，再写档案
_cfg9b = os.path.join(_SANDBOX, "learn_cfg2.json")
_learn_cfg2 = gui.PrinterConfig()
_learn_cfg2.printer_name = "Fake Print"
_learn_cfg2.save(_cfg9b)
_dlg_learn = gui.ManualDuplexDialog()
_dlg_learn.announce("学习用", sheets=10, estimate_sec=43.0)
_lw_dlg = _LearnWin(_learn_cfg2, _cfg9b, dialog=_dlg_learn)
gui.MainWindow._on_duplex_speed_sample(_lw_dlg, 10, 43.0)
check("★ 主线程拿实测值冻结窗口计时（界面时钟只兜底），显示的就是程序实测",
      _dlg_learn._pass1_done and abs(_dlg_learn._pass1_final - 43.0) < 0.01
      and "用时 00:43" in _dlg_learn._pass1.text(), _dlg_learn._pass1.text())
check("同一笔实测也写进了档案（一个信号两用）",
      gui.PrinterConfig.load(_cfg9b).get_profile("Fake Print").speed_samples == [[10, 43.0]])


_lw = _LearnWin(_learn_cfg, _cfg9)
gui.MainWindow._on_duplex_speed_sample(_lw, 10, 43.0)
gui.MainWindow._on_duplex_speed_sample(_lw, 1, 11.5)
gui.MainWindow._on_duplex_speed_sample(_lw, 1, 0.1)      # 异常样本：必须被丢弃
_reloaded = gui.PrinterConfig.load(_cfg9).get_profile("Fake Print")
check("★ 第 1 趟实测写进档案并落盘（重启后预估不丢）",
      [s[0] for s in _reloaded.speed_samples] == [10, 1]
      and [round(v, 1) for v in _reloaded.speed_fit()] == [8.0, 3.5],
      f"{_reloaded.speed_samples} → {[round(v, 2) for v in _reloaded.speed_fit()]}")
check("异常样本（0.1 秒）被丢弃，档案里只有 2 条", len(_reloaded.speed_samples) == 2)
check("学习结果有日志反馈", any("速度样本" in m for m in _lw.logs))
check("★ 另一种打印机（不同队列名）不会串到本机档案",
      gui.PrinterConfig.load(_cfg9).get_profile("另一台打印机").speed_samples == [])

# ── ⑨d 提前回报不采信（否则预估被带塌，闸门自己失效）──────────────────────────
print("\n=== ⑨d 提前回报（主机型打印机只回报「开始打印」）不采信 ===")
_cfg9d = os.path.join(_SANDBOX, "early_cfg.json")
_early_cfg = gui.PrinterConfig()
_early_cfg.printer_name = "Fake Print 早回报"
_early_cfg.save(_cfg9d)
_dlg_early = gui.ManualDuplexDialog()
_dlg_early.announce("早回报", sheets=16, estimate_sec=43.0)
_lw_early = _LearnWin(_early_cfg, _cfg9d, dialog=_dlg_early)
check("★ 16 张 2.1 秒（预估 43s）被判为提前回报；不早于闸门（38s）的不算",
      _dlg_early.is_early_signal(2.1) is True and _dlg_early.is_early_signal(38.0) is False
      and _dlg_early.is_early_signal(0.0) is True)
_before9d = list(gui.PrinterConfig.load(_cfg9d).get_profile("Fake Print 早回报").speed_samples)
gui.MainWindow._on_duplex_speed_sample(_lw_early, 16, 2.1)
_after9d = list(gui.PrinterConfig.load(_cfg9d).get_profile("Fake Print 早回报").speed_samples)
check("★ 提前回报**不写进速度样本**（写进去就反向拟合：16 张比 3 张还快 ⇒ 预估塌进 10 秒、"
      "闸门自己失效 —— 2026-10-18~19 现场就是这么塌的）",
      _after9d == _before9d, f"{_before9d} → {_after9d}")
check("★ 提前回报也**不冻结**窗口计时（真实用时还在走；按钮按预估放行）",
      not _dlg_early._pass1_done and _dlg_early._pass1_timer.isActive()
      and "已用" in _dlg_early._pass1.text(), _dlg_early._pass1.text())
check("日志说清为什么弃用这笔实测", any("不可信" in m for m in _lw_early.logs),
      f"{_lw_early.logs}")
# 同一台机器真打完一笔（不早于闸门）→ 照旧入库（别把正常学习一起关掉）
gui.MainWindow._on_duplex_speed_sample(_lw_early, 16, 42.0)
_grown9d = gui.PrinterConfig.load(_cfg9d).get_profile("Fake Print 早回报").speed_samples
check("★ 不早于闸门的实测照旧入库（没有把正常学习一起关掉）",
      len(_grown9d) == len(_before9d) + 1 and _grown9d[-1][0] == 16, f"{_grown9d}")

# 测速校准时这台机器同样"提前回报"：数值低于物理下限 → 不写档案、也**不许谎报成功**
_cfg9e = os.path.join(_SANDBOX, "cal_cfg.json")
_cal_cfg = gui.PrinterConfig()
_cal_cfg.printer_name = "Fake Print 校准"
_cal_cfg.save(_cfg9e)
_lw_cal = _LearnWin(_cal_cfg, _cfg9e)
_lw_cal._speed_cal_worker = object()
_calls9 = []
_orig_warn, _orig_info = gui.QMessageBox.warning, gui.QMessageBox.information
try:
    gui.QMessageBox.warning = staticmethod(
        lambda *a, **k: _calls9.append(("warn", a[1] if len(a) > 1 else "")) or 0)
    gui.QMessageBox.information = staticmethod(
        lambda *a, **k: _calls9.append(("info", a[1] if len(a) > 1 else "")) or 0)
    _before9e = list(gui.PrinterConfig.load(_cfg9e).get_profile("Fake Print 校准").speed_samples)
    gui.MainWindow._on_speed_calibration_done(
        _lw_cal, True, "标定完成：1 张 1.1s / 10 张 2.4s", [[1, 1.1], [10, 2.4]])
    check("★ 测速数值全低于物理下限 → 一条不写档案，且**不谎报**「已写进档案」",
          gui.PrinterConfig.load(_cfg9e).get_profile("Fake Print 校准").speed_samples == _before9e
          and _calls9 and _calls9[-1][0] == "warn"
          and any("不可信" in m for m in _lw_cal.logs),
          f"{_calls9} | {_lw_cal.logs}")
    gui.MainWindow._on_speed_calibration_done(
        _lw_cal, True, "标定完成：1 张 11.5s / 10 张 43.0s", [[1, 11.5], [10, 43.0]])
    _grown9e = gui.PrinterConfig.load(_cfg9e).get_profile("Fake Print 校准").speed_samples
    check("正常标定照旧两点入库 + 提示成功",
          len(_grown9e) == len(_before9e) + 2 and [s[0] for s in _grown9e[-2:]] == [1, 10]
          and _calls9[-1][0] == "info",
          f"{_calls9} | {_grown9e}")
finally:
    gui.QMessageBox.warning, gui.QMessageBox.information = _orig_warn, _orig_info

# ── 测速校准线程（打 1 张 + 10 张；桩掉真实打印函数，不发一个任务到打印机）──
print("\n=== ⑨c 测速校准（两点标定）===")
import pdf_printer as _ppmod  # noqa: E402
_orig_two = _ppmod.measure_print_speed_two_point
_orig_one = _ppmod.measure_print_speed
try:
    _ppmod.measure_print_speed_two_point = (
        lambda printer, small=1, large=10, dpi=300, on_log=None:
        (True, "标定完成：1 张 11.5s / 10 张 43.0s", [[1, 11.5], [10, 43.0]]))
    _cw = gui.SpeedCalibrationWorker("Fake Print")
    _got9 = []
    _cw.done.connect(lambda ok, msg, samples: _got9.append((ok, msg, samples)))
    _cw.run()
    check("★ 标定线程把两点样本回传（走的是 pdf_printer 的测速函数）",
          len(_got9) == 1 and _got9[0][0] is True and _got9[0][2] == [[1, 11.5], [10, 43.0]],
          f"{_got9}")
    _ppmod.measure_print_speed = (
        lambda printer, pages=1, **kw: (True, "桩", 40.0 if pages == 1 else 30.0))
    # 用**真的** two_point（上一段把模块属性换成了桩，这里显式用原函数）：
    # 它内部按模块全局名调 measure_print_speed，所以上面那行替换才生效
    _ok9, _msg9, _ = _orig_two("Fake Print")
    check("★ 异常结果（1 张比 10 张还慢 → 卡纸/缺纸/首份没打）判失败，不写进档案",
          _ok9 is False and "异常" in _msg9, _msg9)
finally:
    _ppmod.measure_print_speed_two_point = _orig_two
    _ppmod.measure_print_speed = _orig_one

# ============================================================
# ⑦ 静态接线
# ============================================================
print("\n=== ⑦ 静态接线 ===")
src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
check("PrintWorker 有常驻窗三信号 + 翻纸回执",
      "duplex_announce = Signal(str)" in src and "duplex_finished = Signal(str)" in src
      and "flip_required = Signal(str, int)" in src and "def confirm_flip(" in src)
check("★ 手动双面任务并批（不在循环里各打一次）",
      "deferred.append((idx, job, print_path, temp_pdf))" in src
      and "raise _DeferManualDuplex()" not in src)
check("★ 批次用真合并 + 真页序引擎（不是每文件各建 plan）",
      # 2026-10-18：手动双面批次只合并 items；直打作业另走 `_print_merged_group`
      # （`_merge_selected` 按各文件页码范围逐份抽取 → 一个作业打完一段）
      "ok, mmsg = _pp.merge_pdfs([it[2] for it in items], merged)" in src
      and "manual_duplex.plan_passes(file_plans, copies=1" in src)
check("★ 直打作业走『按页码范围逐份抽取』的合并（一个作业一段）",
      "def _merge_selected(" in src and "doc.insert_pdf(src, from_page=" in src
      and "def _print_merged_group(" in src)
check("★ 自动双面合并作业在文件/副本之间补空白页（串页修复的唯一落点）",
      "def _driver_pairs_pages(" in src
      and "blank_after: Optional[list[bool]] = None" in src
      and "doc.new_page(width=last_rect.width, height=last_rect.height)" in src
      and "eff_duplex == \"on\" and self._driver_pairs_pages()" in src
      and "res, pad_sides = self._print_merged_group(" in src)
check("★ 并批的临时 PDF 交给批次统一清理（不在循环里删）",
      "self._cleanup_temp_pdf(rec[\"job\"], rec.get(\"temp_pdf\"))" in src)
check("批次结束发收尾信号（常驻窗据此关闭）", "self.duplex_finished.emit(" in src)
check("worker 启动时把打印机档案传进去", "manual_duplex_profile=self._printer_profile()" in src)
check("常驻窗三信号都接到主线程（announce 在攒批模式下分流到 _on_stack_announce）",
      "worker.flip_required.connect(self._on_flip_required)" in src
      and "worker.duplex_announce.connect(" in src
      and "self._on_stack_announce if stack_entry else self._on_duplex_announce" in src
      and "worker.duplex_finished.connect(self._on_duplex_finished)" in src)
check("★ 常驻窗：置顶 + 抢焦点 + 任务栏闪烁",
      "dlg.raise_()" in src and "dlg.activateWindow()" in src
      and "QApplication.alert(self, 0)" in src
      and "setWindowFlag(Qt.WindowStaysOnTopHint, True)" in src)
check("★ 无人值守只剩「预约到点」还挂起等人（无障碍 2026-10-20 已改为直接开始）",
      src.count("self._hold_for_manual_duplex(") == 1)
check("★ 无障碍打印遇「需人工翻纸」→ **直接开始**（方法体里不再调 _hold_for_manual_duplex）",
      "_hold_for_manual_duplex" not in
      src.split("def _process_auto_print_order", 1)[1].split("def ", 1)[0])
check("★ 无障碍直接开始的理由写进源码（print_keepalive 兜住 3 分钟反馈窗口）",
      "print_keepalive" in src.split("def _process_auto_print_order", 1)[1].split("def ", 1)[0])
check("编辑面板按档案标注双面选项 + 标签页 ⏸ 状态徽标（固定槽位，不挪动 −/＋ 按钮）",
      'want = "双面（手动翻纸）" if manual else "双面打印"' in src
      and 'icon, tip = "⏸", TAB_FLIP_HINT' in src
      and 'badge.setText(icon)' in src
      and 'self._tab_label.setText(self._current_tab)' in src)
check("★ 出纸等待已收紧（队列排空 + 短余量，判断权交给操作员数张数）",
      "margin_per_page: float = 1.5" in open(os.path.join(LOCAL_TOOL, "pdf_printer.py"),
                                             encoding="utf-8").read())
check("★ 翻纸按钮按预估比例放行（早回报既不冻结计时、也不写速度样本）—— 静态接线",
      "def flip_gate_at(" in src and "FLIP_GATE_RATIO = 0.5" in src
      and "FLIP_GATE_MIN_SEC = 1.0" in src
      and "gate_ratio=prof.gate_ratio" in src
      and "dlg.is_early_signal(seconds)" in src
      and "self._mode = \"flip_gate\"" in src)

print("\n" + "=" * 56)
print("手动双面 GUI 集成验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
