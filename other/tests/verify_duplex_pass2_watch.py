# -*- coding: utf-8 -*-
"""验收脚本 · 手动双面「第 2 趟后台计时 + 插单确认」（2026-11-12）

用户现场：手动双面的**第 2 趟**是"一次提交进 spooler 就返回" —— worker 早就收工、
常驻窗也关了，纸还在打印机里一张张出。这时又启动一个打印任务，两批纸就在出纸槽里
叠成一摞（上一批第 2 趟的尾巴 + 新任务第 1 趟），翻纸/页序/出纸槽计数全串。

用户口径（原话）：「第二趟开始打印后，依然需要在后台默默计时，如果在预估的时间内，
启动了一个打印任务，此时应该在弹窗中显示"上一个打印任务的第二趟可能正在进行中，
已用时 / 预计"，然后下面有"确定上一个任务已完成"按钮，按下后，确定上一个任务的
第二趟打印完成，启动第二个任务的打印，同时开始计时。然后将打印时间计入打印速度学习之中。」

验的东西（每条都能红）：
  ① `pass2_watch.Pass2Watch` 纯逻辑：窗口内/窗口外用 snapshot 拦，确认/释放/到期清空；
  ② 常驻窗"确认态"（prev2）：文案是**上一批第 2 趟**的已用/预计、按钮叫
     「✔ 确定上一个任务已完成」、`_on_go` 只发 prev_pass2_confirmed（绝不发
     continue_requested —— 那会被 worker 当成"翻纸了"直接去打上一批第 2 趟）；
  ③ worker 闸门：确认 → 本批第 1 趟照打 + 这笔实测发样本；到期没事人确认 → 照常开打
     **但不写样本**（没人看见纸）；跳过/取消 → 一页纸都不打；
  ④ 主线程：确认写 Pass2Watch、第 2 趟实测写档案，且**不冻结新批次窗口的计时**；
  ⑤ 接线是静态的（源码断言）—— 信号不连、参数不传，功能就静默消失，跑不出红。

⚠ 本脚本用桩替掉真打印（print_plan_pass / _wait_pass1_output / count_pending_print_jobs），
  不碰真打印机；PDF 合并与数页用**真的**（reportlab 造的真 PDF）。

    python other/tests/verify_duplex_pass2_watch.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_pass2_watch_")
os.environ["APPDATA"] = _SANDBOX                  # 用户数据目录 → 沙箱（不碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui  # noqa: E402
import pass2_watch  # noqa: E402
import pdf_printer  # noqa: E402
from printer_config import PrintJob, PrinterProfile, format_duration  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ── 真 PDF 素材（3 页 ⇒ 第 1 趟 3 张纸，与用户现场一致）──
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


PDF3 = make_pdf("doc3.pdf", 3)
PDF5 = make_pdf("doc5.pdf", 5)

MANUAL = PrinterProfile(auto_duplex=False, capability_source="manual", manual_duplex=True,
                        first="odd", reverse_second=True, second_rotate_180=True,
                        verified_at="2026-10-12 22:14:49", verified_by="丙成员")


# ============================================================
# 桩：记录两趟的提交与信号（不碰真打印机）
# ============================================================
class Rec:
    def __init__(self):
        self.plan_pass = []       # [(which, ...)]
        self.announce = []        # [常驻窗第 1 趟播报文案]
        self.flip = []            # [翻纸提示]
        self.logs = []            # [日志行]
        self.finished = []        # [(idx, ok, msg)]
        self.stalled = []         # [(idx, msg, record)]
        self.all_finished = []    # [(成功数, 失败数)]
        self.samples = []         # [(第 1 趟张数, 秒)]
        self.pass2_watch = []     # [(文案, 上一批张数, 上一批预估, 上一批已用)]
        self.pass2_samples = []   # [(上一批张数, 秒)]
        self.finished_evt = 0
        self.raised = None


def patched(rec):
    orig = {n: getattr(gui, n) for n in
            ("print_pdf", "print_plan_pass", "wait_for_output", "get_converter",
             "count_pending_print_jobs")}
    orig_merge = pdf_printer.merge_pdfs
    orig_wait = gui.PrintWorker._wait_pass1_output

    def _merge(pdfs, out):
        return orig_merge(pdfs, out)

    def _print_plan_pass(plan, path, printer, which, second_rotate_180=False,
                         progress_callback=None, dpi=0, landscape_fill=True, **kw):
        rec.plan_pass.append((which, plan.pass_pages(which)))
        if progress_callback:
            n = len(list(plan.pass_pages(which)))
            for i in range(1, n + 1):
                progress_callback(i, n)
        return True, f"桩：第 {which} 趟"

    def _wait_pass1_output(self_w, pages, timeout=120.0):
        return {"drained": True, "queue_seconds": 0.05, "interrupted": False,
                "detail": ""}

    def _wait_for_output(printer, pages=0, **kw):
        return {"drained": True, "queue_seconds": 0.0, "margin_seconds": 2.0,
                "peak_jobs": 0, "detail": ""}

    gui.print_pdf = lambda **kw: (rec.plan_pass.append(("pdf", [])), (True, "桩"))[1]
    gui.print_plan_pass = _print_plan_pass
    gui.wait_for_output = _wait_for_output
    gui.PrintWorker._wait_pass1_output = _wait_pass1_output
    gui.get_converter = lambda: object()
    gui.count_pending_print_jobs = lambda printer="": 0
    pdf_printer.merge_pdfs = _merge

    def restore():
        for k, v in orig.items():
            setattr(gui, k, v)
        gui.PrintWorker._wait_pass1_output = orig_wait
        pdf_printer.merge_pdfs = orig_merge

    return restore


def make_job(path, duplex="on", copies=1, mode="long-edge", name="", page_count=0):
    return PrintJob(file_path=path, copies=copies, duplex=duplex, duplex_mode=mode,
                    page_range="", page_count=page_count, dpi=0,
                    display_name=name or os.path.basename(path))


def run_worker(jobs, profile, flip_answer=True, pass2_watch_inst=None, pass2_action=None):
    """跑一次 PrintWorker（同线程 → 信号直连）。

    pass2_action:
      · "confirm" → 闸门弹窗一出现就 `Pass2Watch.confirm()`（= 操作员点了确认）；
      · "skip"    → `worker.confirm_flip(False)`（= 确认态里点了「跳过本任务」）；
      · "cancel"  → `worker.cancel()`（= 别的通道取消批次）。
    """
    rec = Rec()
    w = gui.PrintWorker(jobs=jobs, printer_name="Fake Print", duplex_mode="long-edge",
                        keep_temp_pdf=False, render_dpi=200,
                        manual_duplex_profile=profile,
                        pass2_watch=pass2_watch_inst)
    w.log_message.connect(rec.logs.append)
    w.job_finished.connect(lambda i, ok, msg: rec.finished.append((i, ok, msg)))
    w.job_stalled.connect(lambda i, msg, rec2: rec.stalled.append((i, msg, rec2)))
    w.all_finished.connect(lambda a, b: rec.all_finished.append((a, b)))
    w.duplex_announce.connect(rec.announce.append)
    w.duplex_finished.connect(lambda _t: setattr(rec, "finished_evt", rec.finished_evt + 1))
    w.duplex_speed_sample.connect(lambda n, s: rec.samples.append((n, s)))
    w.duplex_pass2_watch.connect(
        lambda t, n, est, el: rec.pass2_watch.append((t, n, est, el)))
    w.duplex_pass2_sample.connect(lambda n, s: rec.pass2_samples.append((n, s)))
    if pass2_action == "confirm" and pass2_watch_inst is not None:
        w.duplex_pass2_watch.connect(lambda *a: pass2_watch_inst.confirm())
    elif pass2_action == "skip":
        w.duplex_pass2_watch.connect(lambda *a: w.confirm_flip(False))
    elif pass2_action == "cancel":
        w.duplex_pass2_watch.connect(lambda *a: w.cancel())
    if flip_answer is not None:
        w.flip_required.connect(lambda text, sec: (rec.flip.append(text),
                                                   w.confirm_flip(flip_answer)))
    restore = patched(rec)
    try:
        w.run()
    except BaseException as e:            # 异常逃出 run() 本身就是被测行为
        rec.raised = e
    finally:
        restore()
    return rec


# ============================================================
# ① Pass2Watch 纯逻辑（无 Qt、无打印）
# ============================================================
print("\n=== ① Pass2Watch 纯逻辑 ===")

_w0 = pass2_watch.Pass2Watch()
check("★ 没开始过：snapshot 返回 None（不拦任何批次）", _w0.snapshot() is None)

_w0.start(3, 40.0)
_snap = _w0.snapshot()
check("★ 第 2 趟一提交就有窗口：张数/预估/已用/剩余都在",
      _snap is not None and _snap["sheets"] == 3 and _snap["estimate"] == 40.0
      and _snap["elapsed"] >= 0.0 and _snap["remaining"] > 0.0, f"{_snap}")

_w0.start(0, 0.0)
check("★ 没有预估（0 秒）就不拦：snapshot → None（宁可放过，不能瞎等）",
      _w0.snapshot() is None)

_w0.start(3, 0.05)
_grace0 = pass2_watch.GRACE_SEC
pass2_watch.GRACE_SEC = 0.02
try:
    time.sleep(0.12)
    _expired = _w0.snapshot()
finally:
    pass2_watch.GRACE_SEC = _grace0
check("窗口过了「预估 + 宽限」⇒ snapshot 自己清空（不会把下一个任务永远卡住）",
      _expired is None)

_w0.start(2, 30.0)
_sec1 = _w0.confirm()
time.sleep(0.05)
_sec2 = _w0.confirm()
check("★ 确认只认第一次：重复点不会把已用时间越按越长",
      abs(_sec1 - _sec2) < 0.001 and _w0.is_confirmed() and _w0.confirmed_elapsed() == _sec1,
      f"{_sec1:.3f} vs {_sec2:.3f}")

_w0.release()
check("释放后窗口关闭（本批打完/确认过的分支都要走到这里）",
      _w0.snapshot() is None and not _w0.is_confirmed())

_w0.start(1, 10.0)
_w0.wait_confirmed(0.05)
check("wait_confirmed 超时返回 False（worker 侧用轮询不用它，但 API 不能骗人）",
      _w0.wait_confirmed(0.05) is False)


# ============================================================
# ② 常驻窗"确认态"（prev2）
# ============================================================
print("\n=== ② 常驻窗确认态（prev2）===")

_dlg = gui.ManualDuplexDialog()
_go, _skip, _conf = [], [], []
_dlg.continue_requested.connect(lambda: _go.append(1))
_dlg.skip_requested.connect(lambda: _skip.append(1))
_dlg.prev_pass2_confirmed.connect(lambda: _conf.append(1))

_dlg.announce_prev_pass2("上一批（3 张）的第 2 趟可能还在出纸…", sheets=3,
                         estimate_sec=40.0, elapsed_sec=7.0)
check("★ 进确认态：标题写「上一个打印任务的第 2 趟可能正在进行中」",
      _dlg._mode == "prev2"
      and "上一个打印任务的第 2 趟可能正在进行中" in _dlg._status.text(),
      _dlg._status.text())
check("★ 大号计时显示的是**上一批第 2 趟**的已用/预计（不是本批第 1 趟）",
      "上一批第 2 趟" in _dlg._pass1.text()
      and format_duration(7) in _dlg._pass1.text()
      and format_duration(40) in _dlg._pass1.text(), _dlg._pass1.text())
check("★ 按钮就叫「✔ 确定上一个任务已完成」且立刻可点",
      _dlg._btn_go.text() == "✔ 确定上一个任务已完成" and _dlg._btn_go.isEnabled())
check("确认态里「跳过本任务」也可点（不想等就明说）",
      _dlg._btn_skip.text() == "⏭ 跳过本任务" and _dlg._btn_skip.isEnabled())
check("倒计时提示「还有多久自动开始本批」，并说明到点就不再等",
      "自动开始本批" in _dlg._countdown.text() and "还有" in _dlg._countdown.text(),
      _dlg._countdown.text())

# 已用时间随时间走（不再从 0 起跑，接的是 worker 报的已用）
_dlg._prev_base = 7.0
_dlg._prev_t0.invalidate()
check("_prev2_elapsed 接得住「立窗那一刻已经用了多久」", abs(_dlg._prev2_elapsed() - 7.0) < 0.01,
      f"{_dlg._prev2_elapsed()}")
_dlg._pass1_tick()
check("刷新只改文本/配色，不会自己放行（放行只由 worker / 操作员决定）",
      _dlg._mode == "prev2")

check("到点也不由界面 emit 任何信号（放行是 worker 的到期分支）",
      not _go and not _conf)

_dlg._on_go()
check("★ 点「确定上一个任务已完成」只发 prev_pass2_confirmed，绝不发 continue_requested"
      "（否则 worker 会当成翻纸了、直接去打上一批第 2 趟）",
      _conf == [1] and not _go, f"conf={_conf} go={_go}")
check("确认后按钮禁用、标题改成「本批马上开始第 1 趟」",
      _dlg._mode == "prev2_confirmed" and not _dlg._btn_go.isEnabled()
      and "本批马上开始第 1 趟" in _dlg._status.text(), _dlg._status.text())

_dlg2 = gui.ManualDuplexDialog()
_skip2 = []
_dlg2.skip_requested.connect(lambda: _skip2.append(1))
_dlg2.announce_prev_pass2("说明", sheets=2, estimate_sec=10.0, elapsed_sec=1.0)
_dlg2._on_skip()
check("确认态里点「跳过本任务」= 明确跳过（worker 收到 False）",
      _skip2 == [1] and _dlg2._mode == "skipped")

_dlg3 = gui.ManualDuplexDialog()
_skip3 = []
_dlg3.skip_requested.connect(lambda: _skip3.append(1))
_dlg3.announce_prev_pass2("说明", sheets=2, estimate_sec=10.0, elapsed_sec=1.0)
_dlg3.show()
_app.processEvents()
_dlg3.close()
_app.processEvents()
check("★ 确认态关窗**不算跳过**：本批一页纸都还没打，worker 仍按到期自动放行",
      not _skip3 and _dlg3._mode == "prev2", f"skip={_skip3} mode={_dlg3._mode}")

_dlg4 = gui.ManualDuplexDialog()
_dlg4.finish("上一批结束")
check("批次收尾会排一个「稍候自动关窗」", _dlg4._close_timer.isActive())
_dlg4.announce_prev_pass2("说明", sheets=2, estimate_sec=10.0, elapsed_sec=1.0)
check("★ 新批次的确认态能把上一批排下的自动关窗取消掉（否则窗口刚立就被关）",
      not _dlg4._close_timer.isActive() and _dlg4._mode == "prev2")

_dlg4.announce("本批第 1 趟开打", sheets=2, estimate_sec=10.0)
check("确认态之后进正常第 1 趟：计时从 0 起跑、文案回到「第 1 趟」",
      _dlg4._mode == "pass1" and not _dlg4._prev_t0.isValid()
      and "第 1 趟" in _dlg4._pass1.text(), _dlg4._pass1.text())
for _d in (_dlg, _dlg2, _dlg3, _dlg4):
    _d._close_timer.stop()
    _d.close()

# 上一批的 duplex_finished 跨线程排队、晚一步才到：不能把新立起来的确认窗按 done 收掉
_dlg5 = gui.ManualDuplexDialog()
_dlg5.announce_prev_pass2("说明", sheets=2, estimate_sec=10.0, elapsed_sec=1.0)
_dlg5.finish("手动双面批次结束")
check("★ 上一批晚到的「批次结束」不会收掉确认窗（否则操作员没机会按确认）",
      _dlg5._mode == "prev2" and not _dlg5._close_timer.isActive()
      and _dlg5._btn_go.isEnabled(), f"mode={_dlg5._mode}")
_dlg5._close_timer.stop()
_dlg5.close()


# ============================================================
# ③ worker 闸门（真 run()，桩掉打印）
# ============================================================
print("\n=== ③ worker 闸门 ===")

# ③a 没有计时器（老路径/攒批）：绝不多弹一个窗
_rec = run_worker([make_job(PDF3)], MANUAL, pass2_watch_inst=None)
check("没挂 Pass2Watch 时闸门直接放过（老路径零回归）",
      not _rec.pass2_watch and len(_rec.plan_pass) == 2 and _rec.raised is None,
      f"raised={_rec.raised}")

# ③b 计时器在窗口内 + 操作员确认 → 本批第 1 趟照打，这笔实测记样本
_w = pass2_watch.Pass2Watch()
_w.start(3, 60.0)
_rec = run_worker([make_job(PDF3)], MANUAL, pass2_watch_inst=_w, pass2_action="confirm")
check("★ 确认后本批照常两趟打完", len(_rec.plan_pass) == 2 and _rec.raised is None,
      f"plan_pass={[p[0] for p in _rec.plan_pass]} raised={_rec.raised}")
check("★ 闸门弹过一次确认请求，带的是上一批的张数/预估/已用",
      len(_rec.pass2_watch) == 1 and _rec.pass2_watch[0][1] == 3
      and _rec.pass2_watch[0][2] == 60.0 and "上一批（3 张）" in _rec.pass2_watch[0][0],
      f"{_rec.pass2_watch}")
check("★ 确认那一刻把上一批第 2 趟的实测发了出来（主线程据此写速度学习）",
      len(_rec.pass2_samples) == 1 and _rec.pass2_samples[0][0] == 3,
      f"{_rec.pass2_samples}")
check("日志说清了「本批第 1 趟暂不送纸」→「记入速度样本」",
      any("本批第 1 趟暂不送纸" in m for m in _rec.logs)
      and any("记入速度样本" in m for m in _rec.logs), f"{_rec.logs[:6]}")
check("★ 确认后计时器被交还给本批自己：确认标记清掉、第 2 趟窗口从这次提交重新起算",
      not _w.is_confirmed() and (_w.snapshot() or {}).get("elapsed", 99) < 1.0,
      f"{_w.snapshot()}")
_w.release()

# ③c 窗口到期没人确认 → 照常开打，但**不写样本**（没人看见纸）
_grace = pass2_watch.GRACE_SEC
_pass2_watch_mod = gui.pass2_watch
_pass2_watch_mod.GRACE_SEC = 0.6
try:
    _w2 = pass2_watch.Pass2Watch()
    _w2.start(2, 0.5)
    _t0 = time.time()
    _rec = run_worker([make_job(PDF5)], MANUAL, pass2_watch_inst=_w2)
    _dt = time.time() - _t0
finally:
    _pass2_watch_mod.GRACE_SEC = _grace
check("★ 到期没人确认也照常开打（云端自动单不能被这一步卡死）",
      len(_rec.plan_pass) == 2 and _rec.raised is None, f"raised={_rec.raised}")
check("★ 到期分支**不写**速度样本（那一趟没人看见纸，数字不可信）",
      not _rec.pass2_samples and any("窗口到点" in m for m in _rec.logs),
      f"samples={_rec.pass2_samples}")
check("到期真的等够了宽限时间（不是立刻冲过去）", _dt >= 0.6, f"{_dt:.2f}s")

# ③d 确认态里点「跳过本任务」→ 本批一页纸都不打
_w3 = pass2_watch.Pass2Watch()
_w3.start(3, 60.0)
_rec = run_worker([make_job(PDF3)], MANUAL, pass2_watch_inst=_w3, pass2_action="skip")
check("★ 确认态点跳过：本批没有任何一趟送进打印机",
      not _rec.plan_pass and _rec.raised is None, f"plan_pass={_rec.plan_pass}")
check("跳过按「本批还没开始打印」收尾（不是半成品，不写补救台账）",
      _rec.finished == [(0, False, "已跳过：本批还没开始打印")] and not _rec.stalled,
      f"{_rec.finished} stalled={_rec.stalled}")
check("跳过时窗口也释放了", _w3.snapshot() is None)

# ③e 批次被取消 → 闸门立刻退出，不打一页
_w4 = pass2_watch.Pass2Watch()
_w4.start(3, 60.0)
_rec = run_worker([make_job(PDF3)], MANUAL, pass2_watch_inst=_w4, pass2_action="cancel")
check("取消时闸门立刻退出（不会等到宽限结束）",
      not _rec.plan_pass
      and any("批次已取消" in m for m in _rec.logs), f"{_rec.logs[:6]}")
check("取消时窗口释放", _w4.snapshot() is None)


# ============================================================
# ④ 主线程：确认写计时器、第 2 趟实测写档案（不动新窗计时）
# ============================================================
print("\n=== ④ 主线程收尾 ===")


class _LearnWin:
    """MainWindow 方法的假宿主（只给这些处理器用到的成员）。"""

    def __init__(self, cfg, path, dialog=None, watch=None):
        self._config, self._path, self._duplex_dialog = cfg, path, dialog
        self._pass2_watch = watch
        self.logs = []
        # 假宿主不是 MainWindow 子类：把共用的"写档案"方法按未绑定调用挂上来
        self._record_speed_sample = (
            lambda sheets, seconds, label="":
            gui.MainWindow._record_speed_sample(self, sheets, seconds, label))

    def _printer_profile(self):
        return self._config.get_profile(self._config.printer_name)

    def _save_config(self):
        self._config.save(self._path)

    def _log(self, msg):
        self.logs.append(msg)


_cfg = gui.PrinterConfig()
_cfg.printer_name = "Fake Print"
_cfg.save(os.path.join(_SANDBOX, "cfg.json"))

_cfg_path = os.path.join(_SANDBOX, "cfg2.json")
_cfg2 = gui.PrinterConfig()
_cfg2.printer_name = "Fake Print"
_cfg2.save(_cfg_path)

_w5 = pass2_watch.Pass2Watch()
_w5.start(3, 60.0)
_lw = _LearnWin(_cfg2, _cfg_path, dialog=None, watch=_w5)
gui.MainWindow._on_prev2_confirmed(_lw)
check("★ 操作员确认 = Pass2Watch.confirm()（worker 闸门据此放行）",
      _w5.is_confirmed() and any("已确认上一个任务的第 2 趟打完了" in m for m in _lw.logs),
      f"{_lw.logs}")

_lw2 = _LearnWin(_cfg2, _cfg_path, dialog=None, watch=_w5)
gui.MainWindow._on_duplex_pass2_sample(_lw2, 3, 37.5)
_reloaded = gui.PrinterConfig.load(_cfg_path).get_profile("Fake Print")
check("★ 第 2 趟实测写进同一份速度档案（[张数, 秒]）",
      [3, 37.5] in [list(map(float, s)) for s in _reloaded.speed_samples],
      f"{_reloaded.speed_samples}")
check("第 2 趟的样本在日志里标明「（第 2 趟）」",
      any("第 2 趟" in m for m in _lw2.logs) and any("速度样本" in m for m in _lw2.logs),
      f"{_lw2.logs}")

# 新批次窗口的第 1 趟计时绝不能被上一批的实测冻结
_dlg_new = gui.ManualDuplexDialog()
_dlg_new.announce("本批第 1 趟开打", sheets=3, estimate_sec=40.0)
_lw3 = _LearnWin(_cfg2, _cfg_path, dialog=_dlg_new, watch=None)
gui.MainWindow._on_duplex_pass2_sample(_lw3, 3, 41.0)
check("★ 第 2 趟实测**不冻结**新批次窗口的计时（否则操作员看到「已用 00:00」）",
      not _dlg_new._pass1_done, f"done={_dlg_new._pass1_done} {_dlg_new._pass1.text()}")
_dlg_new._close_timer.stop()
_dlg_new.close()


# ============================================================
# ⑤ 静态接线（信号不连/参数不传，功能就静默消失）
# ============================================================
print("\n=== ⑤ 静态接线 ===")

GUI_SRC = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()

check("PrintWorker 构造时把跨批次的计时器传下去",
      'pass2_watch=getattr(self, "_pass2_watch", None)' in GUI_SRC)
check("两个新信号的连接都在 _start_print_worker 里",
      "worker.duplex_pass2_watch.connect(self._on_duplex_pass2_watch)" in GUI_SRC
      and "worker.duplex_pass2_sample.connect(self._on_duplex_pass2_sample)" in GUI_SRC)
check("常驻窗的确认信号接到了主窗",
      "dlg.prev_pass2_confirmed.connect(self._on_prev2_confirmed)" in GUI_SRC)

_i_plan = GUI_SRC.index("self.duplex_plan.emit(plan.sheet_count")
_i_gate = GUI_SRC.index("self._wait_prev_pass2_gate()")
_i_announce = GUI_SRC.index("self.duplex_announce.emit(_announce_txt)", _i_gate)
check("★ 闸门的位置：批次播报之后、第 1 趟播报之前（早于任何送纸）",
      _i_plan < _i_gate < _i_announce, f"{_i_plan} < {_i_gate} < {_i_announce}")

_i_start = GUI_SRC.index("self._pass2_watch.start(")
_i_pass2 = GUI_SRC.index("print_plan_pass(plan, merged, self._printer_name, 2", _i_start)
check("★ 计时起点在第 2 趟提交之前（提交那一刻就开始算，纸还在出）",
      _i_start < _i_pass2, f"{_i_start} < {_i_pass2}")

_i_p2 = GUI_SRC.index("def _on_duplex_pass2_sample")
_i_p2_end = GUI_SRC.index("\n    def ", _i_p2 + 10)
_p2_block = GUI_SRC[_i_p2:_i_p2_end]
check("★ _on_duplex_pass2_sample 里没有冻结窗口计时的调用（这是新批次的窗口）",
      "freeze_pass1(" not in _p2_block)
check("第 2 趟实测走 _record_speed_sample（与第 1 趟同一条口径）",
      "_record_speed_sample(sheets, seconds" in _p2_block)

_i_ce = GUI_SRC.index("    def closeEvent(self, ev):")
_ce_block = GUI_SRC[_i_ce:_i_ce + 2000]
check("★ 关窗算跳过的态里**没有** prev2（等上一批出纸时关窗不等于跳过）",
      'if self._mode in ("pass1", "flip", "flip_gate"):' in _ce_block,
      _ce_block.split("super().closeEvent")[0][-120:])

print()
if ok_all:
    print("全部通过 ✅（手动双面第 2 趟后台计时 + 插单确认）")
else:
    print("有失败项 ❌")
sys.exit(0 if ok_all else 1)
