# -*- coding: utf-8 -*-
"""验收脚本 · 空白页率拆单（2026-11-23 用户需求，offscreen，不碰真打印机）

用户口径（现场单 HN20261123-0010）：一单里混着「整份单面」和「需要双面」的文件时，
单面文件每页各占一张纸、背面永远空着（那一单 4 个文件 = 38 张纸，其中 27 张有一面
空白 = 71%）。空白页率达到阈值 ⇒ **拆成两次打**：先打只需单面的文件 → 操作员把这一叠
取走 → 再打需要双面的文件。人工翻纸机型默认 40%、自动双面机型默认 60%（都可改），
拆之前默认弹框问一句（默认按钮「拆成两次」，带「以后不再问」）。

本脚本验的是「数纸口径 + 判定 + 顺序」，不是翻纸窗本身（那是 verify_manual_duplex_gui.py）：

  1) 纯逻辑：`blank_split.summarize` 数与 `manual_duplex` 摊纸必须**同一份东西**
     （同一批 FileEntry，两种算法得出的空白面数相同）；
  2) 配置往返：三个字段落盘/读回 + 百分数容错（老配置存 40 也认）；
  3) worker 判定 `_blank_split_decision`：达标才拆、攒批不拆、读不出页数不拆、
     询问回执三种选择（拆 / 仍一次打完 / 记住不再问）；
  4) 端到端 `run()`：**先单面 → 等取走 → 再双面**的真实顺序；未达阈值/选「仍一次
     打完」时完全退回旧行为（整单一个批次）；
  5) 静态检查：信号/方法/菜单/对话框都在，且空白页率只在 blank_split.py 算一次。

    python tests/verify_blank_split.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_blank_split_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import blank_split  # noqa: E402
import gui  # noqa: E402
import manual_duplex  # noqa: E402
import pdf_printer  # noqa: E402
from printer_config import PrinterConfig, PrintJob, PrinterProfile  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ── 真 PDF 素材 ──
def make_pdf(name: str, pages: int) -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    path = os.path.join(_SANDBOX, name)
    c = canvas.Canvas(path, pagesize=A4)
    for i in range(pages):
        c.drawString(72, 720, f"page {i + 1}")
        c.showPage()
    c.save()
    return path


def _pdf_pages(path: str) -> int:
    try:
        import fitz
        with fitz.open(path) as doc:
            return doc.page_count
    except Exception:
        return -1


MANUAL = PrinterProfile(auto_duplex=False, capability_source="manual", manual_duplex=True,
                        first="odd", reverse_second=True, second_rotate_180=True)
AUTO = PrinterProfile(auto_duplex=True, capability_source="manual", manual_duplex=False)

PDF7 = make_pdf("turtle_7p.pdf", 7)
PDF9 = make_pdf("string_9p.pdf", 9)
PDF8 = make_pdf("list_8p.pdf", 8)
PDF25 = make_pdf("course_report_25p.pdf", 25)
PDF4 = make_pdf("short_duplex_4p.pdf", 4)


# ============================================================
# 1) 纯逻辑：数纸口径
# ============================================================
print("\n=== 1) blank_split 数纸口径（现场单 HN20261123-0010）===")

ENTRIES = [
    blank_split.FileEntry("37-拓交乐-Turtle与面向对象编程.docx", tuple(range(1, 8))),
    blank_split.FileEntry("37-拓交乐-组合数据类型-字符串.docx", tuple(range(1, 10))),
    blank_split.FileEntry("37-拓交乐-组合数据类型-列表.docx", tuple(range(1, 9))),
    blank_split.FileEntry("方块消消乐课程设计报告.docx", tuple(range(1, 26)), duplex=False),
]
stat = blank_split.summarize(ENTRIES)
check("现场单：38 张纸", stat.sheets == 38, str(stat))
check("现场单：27 张有一面空白", stat.blanks == 27, str(stat))
check("现场单：空白页率 71%", int(round(stat.percent)) == 71,
      f"{stat.percent:.2f}")
check("现场单：单面组 25 张 / 双面组 13 张",
      (stat.simplex_sheets, stat.duplex_sheets) == (25, 13), str(stat))
check("现场单：两边都有文件 ⇒ 可拆", stat.splittable is True)
check("现场单：describe 说人话",
      "38 张纸" in blank_split.describe(stat, 0.40)
      and "27 张" in blank_split.describe(stat, 0.40),
      blank_split.describe(stat, 0.40))

# 与 manual_duplex 自己摊出来的纸**逐张对账**：空白面数 = 两趟里的 None 槽位总数
_plans = []
for _e in ENTRIES:
    _plans.extend(manual_duplex.file_plans_for_pages(
        _e.label, list(_e.pages), _e.duplex, tuple(_e.simplex_pages), None))
_plan = manual_duplex.plan_passes(_plans, copies=1, first=MANUAL.first,
                                  reverse_second=MANUAL.reverse_second,
                                  second_rotate_180=MANUAL.second_rotate_180)
check("★ 口径与 manual_duplex 逐张一致（张数 / 空白面）",
      _plan.sheet_count == stat.sheets
      and (_plan.blank_slots_first + _plan.blank_slots_second) == stat.blanks,
      f"plan={_plan.sheet_count} 张 / {_plan.blank_slots_first + _plan.blank_slots_second} 空面"
      f" vs stat={stat.sheets} / {stat.blanks}")

# 阈值：40% / 60% 都该拆；比 71% 高就不拆；0 = 关掉
check("71% ≥ 40% ⇒ 拆", blank_split.should_split(stat, 0.40) is True)
check("71% ≥ 60% ⇒ 拆（自动双面机型也拆）", blank_split.should_split(stat, 0.60) is True)
check("71% < 72% ⇒ 不拆", blank_split.should_split(stat, 0.72) is False)
check("阈值 0 ⇒ 永不拆（等于关掉）", blank_split.should_split(stat, 0.0) is False)
check("阈值 40 = 40% ⇒ 边界也拆（达到即触发）",
      blank_split.should_split(stat, stat.ratio) is True)

# 只有一组时不拆（拆了也不会少打一张纸）
_simplex_only = blank_split.summarize([
    blank_split.FileEntry("a.docx", tuple(range(1, 6)), duplex=False),
    blank_split.FileEntry("b.docx", tuple(range(1, 4)), duplex=False)])
check("全单面 ⇒ 空白率 100% 但不拆",
      int(round(_simplex_only.percent)) == 100 and _simplex_only.splittable is False
      and blank_split.should_split(_simplex_only, 0.40) is False, str(_simplex_only))
_duplex_only = blank_split.summarize([
    blank_split.FileEntry("c.docx", tuple(range(1, 8))),
    blank_split.FileEntry("d.docx", tuple(range(1, 10)))])
check("全双面 ⇒ 可拆性 False（没有单面组）",
      _duplex_only.splittable is False
      and blank_split.should_split(_duplex_only, 0.10) is False, str(_duplex_only))

# 份数：张数/空白都跟着翻倍，比率不变
_multi = blank_split.summarize([
    blank_split.FileEntry("x.docx", tuple(range(1, 4)), copies=2),
    blank_split.FileEntry("y.docx", tuple(range(1, 6)), duplex=False, copies=2)])
check("份数 ×2 ⇒ 张数/空白都 ×2（比率不变）",
      (_multi.sheets, _multi.blanks) == (14, 12)
      and abs(_multi.ratio - 12.0 / 14.0) < 1e-9, str(_multi))

# 混排（整份双面、其中几页单面）：算进「双面组」，张数按段摊
_mixed = blank_split.summarize([
    blank_split.FileEntry("mix.docx", (1, 2, 3, 4, 5, 6, 7), simplex_pages=(1, 3))])
check("混排：7 页（1/3 单面）⇒ 5 张纸、3 张背面空",
      (_mixed.sheets, _mixed.blanks) == (5, 3), str(_mixed))
check("混排算「需要背面」的那一组", _mixed.duplex_labels == ("mix.docx",), str(_mixed))

check("单页双面文件不需要背面（与「1 页必单面」同一口径）",
      blank_split.FileEntry("one.docx", (1,)).needs_back_side is False)
check("threshold_for：人工 40% / 自动 60%",
      (blank_split.threshold_for(False), blank_split.threshold_for(True)) == (0.40, 0.60))
check("空输入不炸", blank_split.summarize([]).sheets == 0)


# ============================================================
# 2) 配置往返
# ============================================================
print("\n=== 2) printer_config：三个字段落盘/读回 ===")
cfg = PrinterConfig()
check("默认：先问 / 40% / 60%",
      (cfg.blank_split_ask, cfg.blank_split_threshold_manual,
       cfg.blank_split_threshold_auto) == (True, 0.40, 0.60), str(cfg.blank_split_ask))
cfg.blank_split_ask = False
cfg.blank_split_threshold_manual = 0.55
cfg.blank_split_threshold_auto = 0.75
_back = PrinterConfig.from_dict(cfg.to_dict())
check("往返：三个字段一致",
      (_back.blank_split_ask, _back.blank_split_threshold_manual,
       _back.blank_split_threshold_auto) == (False, 0.55, 0.75), str(_back.to_dict().get("blank_split_ask")))
check("按机型取阈值", (_back.blank_split_threshold(False), _back.blank_split_threshold(True))
      == (0.55, 0.75))
_cfg_old = PrinterConfig.from_dict({"blank_split_threshold_manual": 40,
                                    "blank_split_threshold_auto": 60})
check("老配置存百分数（40/60）⇒ 收敛成 0.40/0.60",
      (_cfg_old.blank_split_threshold(False), _cfg_old.blank_split_threshold(True))
      == (0.40, 0.60), str(_cfg_old.to_dict().get("blank_split_threshold_manual")))
_cfg_bad = PrinterConfig.from_dict({"blank_split_threshold_manual": "abc",
                                    "blank_split_threshold_auto": -3,
                                    "blank_split_ask": "yes"})
check("坏值：阈值回落默认、开关按类型兜底",
      _cfg_bad.blank_split_threshold(False) == 0.40
      and _cfg_bad.blank_split_threshold(True) == 0.60, str(_cfg_bad.to_dict()))


# ============================================================
# 3) worker 判定
# ============================================================
print("\n=== 3) PrintWorker._blank_split_decision ===")


def make_job(path, duplex="on", copies=1, page_count=0, page_range="", name=""):
    return PrintJob(file_path=path, copies=copies, duplex=duplex,
                    duplex_mode="long-edge", page_range=page_range,
                    page_count=page_count, display_name=name or os.path.basename(path))


def rec(idx, job, path, copies=1):
    return {"idx": idx, "job": job, "path": path, "temp_pdf": None, "copies": copies,
            "dpi": 200, "ori": "portrait", "duplex": "on", "sides": 1, "key": ()}


def make_worker(interactive=False, ask=True, threshold=0.40, profile=MANUAL, stack=False):
    return gui.PrintWorker(jobs=[], printer_name="Fake Print", duplex_mode="long-edge",
                           keep_temp_pdf=False, render_dpi=200,
                           manual_duplex_profile=profile, stack_mode=stack,
                           blank_split_interactive=interactive, blank_split_ask=ask,
                           blank_split_threshold=threshold)


_SINGLE = rec(0, make_job(PDF25, duplex="off", page_count=25), PDF25)
_DUP = [rec(1, make_job(PDF7, page_count=7), PDF7),
        rec(2, make_job(PDF9, page_count=9), PDF9),
        rec(3, make_job(PDF8, page_count=8), PDF8)]

w = make_worker(interactive=False)
_logs_headless = []
w.log_message.connect(_logs_headless.append)
_dec = w._blank_split_decision([_SINGLE], [(r["idx"], r["job"], r["path"], None) for r in _DUP])
check("★ 无界面交互（无人值守/自动化调用）不拆 —— 机器前没人取纸，拆了只是把一单劈成两段",
      _dec is None, str(_dec))
check("无界面交互仍照实把空白页率写进日志，并说明为什么没拆",
      any("空白页率检查" in s for s in _logs_headless)
      and any("不拆单" in s for s in _logs_headless), str(_logs_headless))

w_stack = make_worker(interactive=True, stack=True)
check("★ 攒批模式不拆（整叠第 2 趟是一次打，拆了会打乱纸叠）",
      w_stack._blank_split_decision([_SINGLE], []) is None)

_bad = rec(9, make_job(os.path.join(_SANDBOX, "missing.pdf"), page_count=0), "")
check("★ 有文件读不出页数 ⇒ 宁可不拆",
      make_worker(interactive=True)._blank_split_decision([_bad], []) is None)

_w_high = make_worker(interactive=True, threshold=0.95)
_asked_high = []
_w_high.blank_split_ask.connect(lambda p: _asked_high.append(p))
check("未达阈值 ⇒ None，且根本不弹框（这里阈值 95%）",
      _w_high._blank_split_decision(
          [_SINGLE], [(r["idx"], r["job"], r["path"], None) for r in _DUP]) is None
      and _asked_high == [], str(_asked_high))

# 询问回执：拆 / 仍一次打完 / 取消。Worker 的信号在主线程 emit ⇒ 直连槽当场执行，
# 所以这里同线程调用即可（真实运行是 QThread → 主线程，见第 4 段 processEvents 那组）。
def _decide_inline(worker, reply=None, remember=False):
    out = {}

    def _on_ask(payload):
        out["ask"] = payload
        if reply is not None:
            worker.confirm_blank_split(reply, remember=remember)

    worker.blank_split_ask.connect(_on_ask)
    out["dec"] = worker._blank_split_decision(
        [_SINGLE], [(r["idx"], r["job"], r["path"], None) for r in _DUP])
    return out


_out = _decide_inline(make_worker(interactive=True), reply="split", remember=True)
check("★ 询问后选「拆成两次」⇒ 拆",
      _out.get("dec") is not None and len(_out["dec"][0]) == 1,
      str(_out.get("dec") and _out["dec"][1]))
check("判定结果里的统计 = 38 张 / 27 空 / 25 张单面",
      _out.get("dec") is not None
      and (_out["dec"][2].sheets, _out["dec"][2].blanks, _out["dec"][2].simplex_sheets)
      == (38, 27, 25), str(_out.get("dec") and _out["dec"][2]))
check("询问 payload 带齐现场数字（38 张 / 71% / 1 单面 / 3 双面）",
      _out.get("ask") is not None
      and (_out["ask"]["sheets"], _out["ask"]["singles"], _out["ask"]["duplex"]) == (38, 1, 3)
      and int(round(_out["ask"]["percent"])) == 71, str(_out.get("ask")))

_out2 = _decide_inline(make_worker(interactive=True), reply="whole")
check("★ 询问后选「仍一次打完」⇒ 不拆（完全退回旧行为）",
      _out2.get("dec") is None, str(_out2.get("dec")))

# 只勾「以后不再问」不点拆 ⇒ 勾选本身不生效（记住的是"直接拆"，不是"这次也拆"）
_out3 = _decide_inline(make_worker(interactive=True), reply="whole", remember=True)
check("选「仍一次打完」+勾「不再问」⇒ 这一次仍打整单，且不让 worker 记住",
      _out3.get("dec") is None, str(_out3.get("dec")))

# 取消：弹框还没回执就点取消 ⇒ 立刻返回 None，不卡死
w_cancel = make_worker(interactive=True)


def _cancel_soon():
    time.sleep(0.4)
    w_cancel.cancel()


threading.Thread(target=_cancel_soon, daemon=True).start()
_t0 = time.time()
_cancel_dec = w_cancel._blank_split_decision(
    [_SINGLE], [(r["idx"], r["job"], r["path"], None) for r in _DUP])
check("★ 弹框期间点取消 ⇒ 判定立刻返回 None（不卡死）",
      _cancel_dec is None and (time.time() - _t0) < 5.0, f"{time.time() - _t0:.2f}s")

# 取纸回执
w_pick = make_worker(interactive=True)
w_pick._tray_sheets = 25
_clicks = []
w_pick.split_pickup.connect(lambda p: (_clicks.append(p), w_pick.confirm_split_pickup(True)))
_ok_pick = w_pick._wait_split_pickup(stat, 25, 1)
check("★ 取纸框回执「已取走」⇒ 放行且出纸槽台账扣掉这一叠",
      _ok_pick is True and w_pick._tray_sheets == 0 and _clicks
      and _clicks[0]["sheets"] == 25, f"tray={w_pick._tray_sheets} {_clicks}")

w_keep = make_worker(interactive=True)
w_keep._tray_sheets = 25
w_keep.split_pickup.connect(lambda p: w_keep.confirm_split_pickup(False))
check("「还没取走」⇒ 照打，但台账不扣（后面翻纸文案会多算）",
      w_keep._wait_split_pickup(stat, 25, 1) is True and w_keep._tray_sheets == 25)

w_headless = make_worker(interactive=False)
check("无界面交互 ⇒ 不等取纸（日志说明一句就继续）",
      w_headless._wait_split_pickup(stat, 25, 1) is True)


# ============================================================
# 4) 端到端：run() 的顺序
# ============================================================
print("\n=== 4) run()：先单面 → 取走 → 再双面 ===")


def run_order(threshold=0.40, reply="split", pickup=True, jobs=None, profile=MANUAL):
    """跑一遍 worker.run()（打印全部打桩），返回事件时间线。"""
    events = []
    rec_log = {"finished": [], "stalled": [], "all_finished": [], "logs": []}
    jobs = jobs if jobs is not None else [
        make_job(PDF25, duplex="off", page_count=25),
        make_job(PDF7, page_count=7), make_job(PDF9, page_count=9),
        make_job(PDF8, page_count=8),
    ]
    w = gui.PrintWorker(jobs=jobs, printer_name="Fake Print", duplex_mode="long-edge",
                        keep_temp_pdf=False, render_dpi=200,
                        manual_duplex_profile=profile,
                        blank_split_interactive=True, blank_split_ask=True,
                        blank_split_threshold=threshold)
    orig = {n: getattr(gui, n) for n in
            ("print_pdf", "print_plan_pass", "wait_for_output", "get_converter",
             "count_pending_print_jobs")}
    orig_manual = gui.PrintWorker._print_manual_batch
    orig_pass1 = gui.PrintWorker._wait_pass1_output

    def _print_pdf(**kw):
        events.append(("print_pdf", _pdf_pages(kw.get("pdf_path", ""))))
        return True, "桩：一次提交"

    def _print_plan_pass(*a, **kw):
        _which = kw.get("which") or (a[3] if len(a) > 3 else "")
        events.append(("plan_pass", _which))
        return True, f"桩：第 {_which} 趟"

    def _manual(self_, items, rotate=None, base_offset=0, total_sides=0):
        events.append(("manual", len(items)))
        return True, "桩：手动双面批次打完", 0

    gui.print_pdf = _print_pdf
    gui.print_plan_pass = _print_plan_pass
    gui.wait_for_output = lambda *a, **kw: True
    gui.get_converter = lambda *a, **kw: None
    gui.count_pending_print_jobs = lambda *a, **kw: 0
    gui.PrintWorker._print_manual_batch = _manual
    gui.PrintWorker._wait_pass1_output = lambda *a, **kw: True

    w.job_finished.connect(lambda i, ok, msg: rec_log["finished"].append((i, ok, msg)))
    w.job_stalled.connect(lambda i, msg, r: rec_log["stalled"].append((i, msg)))
    w.all_finished.connect(lambda a, b: rec_log["all_finished"].append((a, b)))
    w.log_message.connect(rec_log["logs"].append)
    w.blank_split_ask.connect(
        lambda p: (events.append(("ask", p["singles"], p["duplex"])),
                   w.confirm_blank_split(reply)))
    w.split_pickup.connect(
        lambda p: (events.append(("pickup", p["sheets"])), w.confirm_split_pickup(pickup)))
    try:
        w.run()
    finally:
        for n, f in orig.items():
            setattr(gui, n, f)
        gui.PrintWorker._print_manual_batch = orig_manual
        gui.PrintWorker._wait_pass1_output = orig_pass1
    return events, rec_log, w


_ev, _rl, _w = run_order()
check("★ 顺序：先询问 → 打单面（25 张一份作业）→ 等取走 → 再打 3 个双面文件",
      _ev == [("ask", 1, 3), ("print_pdf", 25), ("pickup", 25), ("manual", 3)], str(_ev))
check("4 个任务全部回报成功（单面 1 + 双面 3）",
      len(_rl["finished"]) == 4 and all(ok for _i, ok, _m in _rl["finished"]),
      str(_rl["finished"]))
check("没有半成品（拆单路径不该产生 job_stalled）", _rl["stalled"] == [], str(_rl["stalled"]))
check("日志讲清楚为什么拆（含 ⇄ 与取走确认）",
      any("⇄ 拆成两次打印" in s for s in _rl["logs"])
      and any("单面那一叠已取走" in s for s in _rl["logs"]),
      [s for s in _rl["logs"] if "⇄" in s or "取走" in s])

_ev_whole, _rl_whole, _ = run_order(reply="whole")
check("★ 选「仍一次打完」⇒ 不分段：4 个文件一个批次（旧行为）",
      _ev_whole == [("ask", 1, 3), ("manual", 4)], str(_ev_whole))
check("仍一次打完：4 个任务照样都成功",
      len(_rl_whole["finished"]) == 4 and all(ok for _i, ok, _m in _rl_whole["finished"]))

_ev_high, _, _ = run_order(threshold=0.95)
check("★ 未达阈值 ⇒ 根本不询问、整单一个批次",
      _ev_high == [("manual", 4)], str(_ev_high))

_ev_dup_only, _, _ = run_order(jobs=[make_job(PDF7, page_count=7),
                                     make_job(PDF9, page_count=9)])
check("全双面单 ⇒ 不询问、不拆（可拆性 False）",
      _ev_dup_only == [("manual", 2)], str(_ev_dup_only))


# ---- 真实线程模型：worker 在子线程里跑，弹框/回执走「主线程 processEvents」----
# 上面那组是主线程同线程调用（信号直连）；这里复现真实部署：worker 是 QThread，
# 信号排队送到主线程，模态框在主线程弹、操作员点击后才回执 —— 卡死/丢事件只在这种
# 模型下才会露出来。
def run_order_threaded(threshold=0.40, reply="split", pickup=True, timeout=20.0):
    events, done = [], {"fin": None}
    jobs = [make_job(PDF25, duplex="off", page_count=25),
            make_job(PDF7, page_count=7), make_job(PDF9, page_count=9),
            make_job(PDF8, page_count=8)]
    w = gui.PrintWorker(jobs=jobs, printer_name="Fake Print", duplex_mode="long-edge",
                        keep_temp_pdf=False, render_dpi=200,
                        manual_duplex_profile=MANUAL,
                        blank_split_interactive=True, blank_split_ask=True,
                        blank_split_threshold=threshold)
    orig = {n: getattr(gui, n) for n in
            ("print_pdf", "print_plan_pass", "wait_for_output", "get_converter",
             "count_pending_print_jobs")}
    orig_manual = gui.PrintWorker._print_manual_batch
    orig_pass1 = gui.PrintWorker._wait_pass1_output

    def _manual(self_, items, rotate=None, base_offset=0, total_sides=0):
        events.append(("manual", len(items)))
        return True, "桩：手动双面批次打完", 0

    gui.print_pdf = lambda **kw: (events.append(("print_pdf", _pdf_pages(kw.get("pdf_path", "")))),
                                  (True, "桩"))[1]
    gui.print_plan_pass = lambda *a, **kw: (True, "桩")
    gui.wait_for_output = lambda *a, **kw: True
    gui.get_converter = lambda *a, **kw: None
    gui.count_pending_print_jobs = lambda *a, **kw: 0
    gui.PrintWorker._print_manual_batch = _manual
    gui.PrintWorker._wait_pass1_output = lambda *a, **kw: True

    w.blank_split_ask.connect(
        lambda p: (events.append(("ask", p["singles"], p["duplex"])),
                   w.confirm_blank_split(reply)))
    w.split_pickup.connect(
        lambda p: (events.append(("pickup", p["sheets"])), w.confirm_split_pickup(pickup)))
    w.all_finished.connect(lambda a, b: done.__setitem__("fin", (a, b)))
    th = threading.Thread(target=w.run, daemon=True)
    th.start()
    deadline = time.time() + timeout
    while done["fin"] is None and time.time() < deadline:
        _app.processEvents()
        time.sleep(0.02)
    th.join(5.0)
    for n, f in orig.items():
        setattr(gui, n, f)
    gui.PrintWorker._print_manual_batch = orig_manual
    gui.PrintWorker._wait_pass1_output = orig_pass1
    return events, done


_ev_t, _done_t = run_order_threaded()
check("★ 真实线程模型（worker 子线程 + 主线程弹框）也按 询问→单面→取走→双面 的顺序走完",
      _ev_t == [("ask", 1, 3), ("print_pdf", 25), ("pickup", 25), ("manual", 3)],
      f"{_ev_t} / 收尾={_done_t['fin']}")
check("真实线程模型下 4 个任务全部成功收尾",
      _done_t["fin"] == (4, 0), str(_done_t["fin"]))

_ev_pick_later, _rl_later, _ = run_order(pickup=False)
check("「还没取走」⇒ 顺序照旧，只是台账不扣（日志提醒一句）",
      _ev_pick_later == [("ask", 1, 3), ("print_pdf", 25), ("pickup", 25), ("manual", 3)]
      and any("未取走" in s for s in _rl_later["logs"]), str(_ev_pick_later))

# 自动双面机型：阈值更高（60%），71% 仍触发；改成 95% 就不触发。
# 它不需要翻纸（走直打/合并路径），所以"再打双面组"就是一次合并打印：
# 7+9+8 = 24 页 + 文件分界补的 2 面空白 = 26 面。
_ev_auto, _, _ = run_order(profile=AUTO)
check("自动双面机型同样支持拆单（本轮直接打，不涉及翻纸窗）",
      _ev_auto == [("ask", 1, 3), ("print_pdf", 25), ("pickup", 25), ("print_pdf", 26)],
      str(_ev_auto))
_ev_auto_high, _, _ = run_order(profile=AUTO, threshold=0.95)
check("自动双面机型阈值调高后不拆（单面/双面各一个作业，不询问）",
      [e[0] for e in _ev_auto_high] == ["print_pdf", "print_pdf"], str(_ev_auto_high))


# ============================================================
# 4b) 对话框：构造参数 ↔ 现值（选项 → 空白页率拆单…）
# ============================================================
print("\n=== 4b) 对话框：构造参数 ↔ 现值 ===")


def _dlg(ask, th_manual, th_auto, auto=False):
    return gui.BlankSplitDialog(None, ask=ask, threshold_manual=th_manual,
                                threshold_auto=th_auto, auto_duplex=auto)


_d1 = _dlg(True, 0.40, 0.60)
check("★ 构造时传 ask=True ⇒ 复选框就是勾上的（否则一按保存就把「先问我」悄悄关掉）",
      _d1.values()[0] is True, str(_d1.values()))
check("构造参数原样回到现值（0.40 / 0.60 存成 40% / 60% 输入框）",
      _d1.values() == (True, 0.40, 0.60), str(_d1.values()))

_d2 = _dlg(False, 0.55, 0.70, auto=True)
check("ask=False 与自定义阈值同样原样往返",
      _d2.values() == (False, 0.55, 0.70), str(_d2.values()))

_d2._on_reset()
check("「恢复默认」回到 40% / 60% 且重新勾上「先问我」",
      _d2.values() == (True, 0.40, 0.60), str(_d2.values()))

_d3 = _dlg(True, 0.40, 0.60)
_d3._th_manual.setValue(100)
check("阈值输入框上限 100% 归一成 1.0", _d3.values()[1] == 1.0, str(_d3.values()))
_d4_auto = _dlg(True, 0.4, 0.6, auto=True)
_d4_man = _dlg(True, 0.4, 0.6, auto=False)
check("对话框说明随机型变（自动双面机型说「生效的是自动双面这一个阈值」）",
      "自动双面机型" in _d4_auto._note.text()
      and "手动翻纸" in _d4_man._note.text())

for _d in (_d1, _d2, _d3, _d4_auto, _d4_man):
    _d.deleteLater()

# ============================================================
# 5) 静态检查
# ============================================================
print("\n=== 5) 静态检查（接线齐全 / 只有一处口径）===")
with open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8") as f:
    gui_src = f.read()
with open(os.path.join(LOCAL_TOOL, "blank_split.py"), encoding="utf-8") as f:
    bs_src = f.read()
with open(os.path.join(LOCAL_TOOL, "printer_config.py"), encoding="utf-8") as f:
    cfg_src = f.read()

for _name, _needle in [
    ("worker 信号 blank_split_ask", "blank_split_ask = Signal(object)"),
    ("worker 信号 split_pickup", "split_pickup = Signal(object)"),
    ("回执 confirm_blank_split", "def confirm_blank_split("),
    ("回执 confirm_split_pickup", "def confirm_split_pickup("),
    ("判定 _blank_split_decision", "def _blank_split_decision("),
    ("等取纸 _wait_split_pickup", "def _wait_split_pickup("),
    ("分组打印 _print_grouped", "def _print_grouped("),
    ("主线程 _on_blank_split_ask", "def _on_blank_split_ask("),
    ("主线程 _on_split_pickup", "def _on_split_pickup("),
    ("设置项 _on_blank_split_options", "def _on_blank_split_options("),
    ("对话框 BlankSplitDialog", "class BlankSplitDialog(QDialog)"),
    ("菜单项已挂到选项菜单", "blank_split_action = QAction("),
    ("启动时传 interactive=True", "blank_split_interactive=True"),
    ("信号已接线", "worker.blank_split_ask.connect("),
    ("取纸信号已接线", "worker.split_pickup.connect("),
]:
    check(f"gui.py 有：{_name}", _needle in gui_src)

# 静态锚点只用 ASCII 标识符/代码行：拿中文文案当契约会让"改一句提示"变成假失败
# （verify_guard_quality 的 A1/A2 棘轮就是为此而设，别把计数推上去）。
check("拆单块排在整单并批之前（否则单面文件已被搬进 deferred）",
      gui_src.index("_blank_split_decision(pending, deferred)")
      < gui_src.index("moved = [rec for rec in pending if self._can_join_manual_batch(rec)]"))
check("配置三个字段落盘", "blank_split_ask" in cfg_src
      and "blank_split_threshold_manual" in cfg_src
      and "blank_split_threshold_auto" in cfg_src)
check("blank_split.py 不 import 打印栈（纯逻辑，可单测）",
      "pdf_printer" not in bs_src.split('"""')[2])
check("空白页率只在 blank_split.py 算（GUI 里没有第二套除法）",
      "blank_slots_first + plan.blank_slots_second" not in gui_src)
check("对话框阈值用百分数输入（1~100）", "setRange(1, 100)" in gui_src)


print()
print("=" * 60)
print("全部通过" if ok_all else "有失败项")
sys.exit(0 if ok_all else 1)
