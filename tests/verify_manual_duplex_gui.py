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
check("测试素材：3 份真 PDF（5/3/1 页）",
      all(os.path.isfile(p) and os.path.getsize(p) > 400 for p in (PDF5, PDF3, PDF1)))

MANUAL = PrinterProfile(auto_duplex=False, capability_source="manual", manual_duplex=True,
                        first="odd", reverse_second=True, second_rotate_180=True,
                        verified_at="2026-10-12 22:14:49", verified_by="丙成员")
AUTO = PrinterProfile(auto_duplex=True, capability_source="manual", manual_duplex=False)
UNKNOWN = PrinterProfile()          # 能力未确认：必须保持旧行为（零回归）


# ============================================================
# 桩：记录 print_pdf / print_plan_pass 调用，模拟逐面进度
# ============================================================
class Rec:
    def __init__(self):
        self.plan_pass = []      # [(which, 本趟是否旋转, 0-based序列, 1-based序列, 传下去的旋转设置)]
        self.print_pdf = []      # [kwargs]
        self.announce = []       # [常驻窗 announce 文案]
        self.flip = []           # [翻纸提示文案]
        self.finished_evt = 0
        self.waited = []
        self.logs = []
        self.progress = []
        self.finished = []
        self.plan_labels = []    # [{sheets:[(标签,a,b)], slots:{纸序:标签}}]
        self.plan = []           # [(第 1 趟张数, 预估秒数)] —— 批次开始播报
        self.samples = []        # [(第 1 趟张数, 实测秒数)] —— 自动学习样本


def patched(rec):
    orig = {n: getattr(gui, n) for n in
            ("print_pdf", "print_plan_pass", "wait_for_output", "get_converter")}

    def _print_pdf(**kw):
        rec.print_pdf.append(kw)
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
    return restore


def make_job(path, duplex="on", copies=1, mode="long-edge", name=""):
    return PrintJob(file_path=path, copies=copies, duplex=duplex, duplex_mode=mode,
                    page_range="", page_count=0, display_name=name or os.path.basename(path))


def run_worker(jobs, profile, flip_answer=True, on_announce=None, timeout=None):
    rec = Rec()
    w = gui.PrintWorker(jobs=jobs, printer_name="Fake Print", duplex_mode="long-edge",
                        keep_temp_pdf=False, render_dpi=200, manual_duplex_profile=profile)
    if timeout is not None:
        w.FLIP_TIMEOUT_SEC = timeout
        w.FLIP_GRACE_SEC = 0
    w.log_message.connect(rec.logs.append)
    w.progress.connect(lambda c, t, s: rec.progress.append((c, t, s)))
    w.job_finished.connect(lambda i, ok, msg: rec.finished.append((i, ok, msg)))
    w.duplex_announce.connect(rec.announce.append)
    w.duplex_finished.connect(lambda _t: setattr(rec, "finished_evt", rec.finished_evt + 1))
    if on_announce is not None:
        w.duplex_announce.connect(lambda text: on_announce(w))
    if flip_answer is not None:
        w.flip_required.connect(
            lambda text, sec: (rec.flip.append(text), w.confirm_flip(flip_answer)))
    restore = patched(rec)
    t0 = time.time()
    try:
        w.run()
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
check("进度 total = 12（6 张纸 ×2 趟）",
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
check("混排：单面文件照旧一次提交（走驱动/单面路径）",
      len(rec.print_pdf) == 1 and rec.print_pdf[0]["duplex"] == "off",
      f"{[c.get('duplex') for c in rec.print_pdf]}")
check("混排：三个任务都有完成回报", sorted(i for i, _ok, _m in rec.finished) == [0, 1, 2],
      f"{rec.finished}")
check("混排：双面批次的页序只含两个双面文件（5+1=6 页 → 4 张纸）",
      rec.plan_pass[0][3] == [1, 3, 5, 6] and "应有 4 张纸" in rec.flip[0],
      f"{rec.plan_pass[0][3]}")

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

    def __init__(self, worker):
        self._worker = worker
        self._print_starting = False
        self.logs = []

    def _has_members(self):
        return True

    def _log(self, msg):
        self.logs.append(msg)


class _FinWin:
    """只够跑 _on_all_finished 的替身：记录"清 worker"与"补打"的先后顺序。"""

    def __init__(self, worker, retry_item, tab_jobs):
        self._worker = worker
        self._auto_print_retry = [retry_item]
        self._all_printed = False
        self._printing_tab_key = "0"
        self.calls = []

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
check("★ 补打队列只在 _on_all_finished 里出队（全代码仅此一处 pop）",
      src.count("self._auto_print_retry.pop(0)") == 1
      and "self._worker = None" in src.split("def _on_all_finished")[1][:400])
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
_dlg9.set_flippable("翻纸说明", 600)
check("★ 第 1 趟出纸 → 冻结成「用时」（0 秒也是合法结果，不能拿 0.0 当「未完成」）",
      _dlg9._pass1_done and "用时" in _dlg9._pass1.text() and not _dlg9._pass1_timer.isActive(),
      _dlg9._pass1.text())
_frozen_text = _dlg9._pass1.text()
_dlg9._pass1_tick()
check("冻结后再 tick 也不会继续走", _dlg9._pass1.text() == _frozen_text)
check("翻纸确认倒计时仍在（两个计时器互不干扰）", "还有" in _dlg9._countdown.text(),
      _dlg9._countdown.text())
_dlg9.announce("第二批", sheets=3, estimate_sec=18.5)
check("新批次复位重新计时",
      not _dlg9._pass1_done and "已用" in _dlg9._pass1.text() and _dlg9._pass1_timer.isActive())
_dlg9.finish("批次结束")
check("收尾把计时器停掉", not _dlg9._pass1_timer.isActive())

# ── 自动学习落盘（真的写 print_config.json 副本，跑完读回来核对）──
print("\n=== ⑨b 实测样本自动学习并落盘 ===")
_cfg9 = os.path.join(_SANDBOX, "learn_cfg.json")
_learn_cfg = gui.PrinterConfig()
_learn_cfg.printer_name = "Fake Print"
_learn_cfg.save(_cfg9)


class _LearnWin:
    def __init__(self, cfg, path):
        self._config, self._path, self.logs = cfg, path, []

    def _printer_profile(self):
        return self._config.get_profile(self._config.printer_name)

    def _save_config(self):
        self._config.save(self._path)

    def _log(self, msg):
        self.logs.append(msg)


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
      and "raise _DeferManualDuplex()" in src)
check("★ 批次用真合并 + 真页序引擎（不是每文件各建 plan）",
      "merge_pdfs([it[2] for it in items], merged)" in src
      and "manual_duplex.plan_passes(file_plans, copies=1" in src)
check("★ 并批的临时 PDF 不在循环 finally 里删掉（批次还要用）",
      "if not defer:\n                    self._cleanup_temp_pdf(job, temp_pdf)" in src)
check("批次结束发收尾信号（常驻窗据此关闭）", "self.duplex_finished.emit(" in src)
check("worker 启动时把打印机档案传进去", "manual_duplex_profile=self._printer_profile()" in src)
check("常驻窗三信号都接到主线程",
      "worker.flip_required.connect(self._on_flip_required)" in src
      and "worker.duplex_announce.connect(self._on_duplex_announce)" in src
      and "worker.duplex_finished.connect(self._on_duplex_finished)" in src)
check("★ 常驻窗：置顶 + 抢焦点 + 任务栏闪烁",
      "dlg.raise_()" in src and "dlg.activateWindow()" in src
      and "QApplication.alert(self, 0)" in src
      and "setWindowFlag(Qt.WindowStaysOnTopHint, True)" in src)
check("★ 无人值守两处（无障碍 + 预约）都挂了手动双面拦截",
      src.count("self._hold_for_manual_duplex(") == 2)
check("编辑面板按档案标注双面选项 + 标签页 ⏸ 标记",
      'want = "双面（手动翻纸）" if manual else "双面打印"' in src
      and 'self._tab_label.setText(f"⏸{self._current_tab}")' in src)
check("★ 出纸等待已收紧（队列排空 + 短余量，判断权交给操作员数张数）",
      "margin_per_page: float = 1.5" in open(os.path.join(LOCAL_TOOL, "pdf_printer.py"),
                                             encoding="utf-8").read())

print("\n" + "=" * 56)
print("手动双面 GUI 集成验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
