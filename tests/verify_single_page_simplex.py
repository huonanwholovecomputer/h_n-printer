# -*- coding: utf-8 -*-
"""验收脚本 · 单页文件一律单面（1 页 ⇒ simplex，杜绝「空白页 + 正式页」）

现场事故（2026-10-17 17:48）：单页 docx「第二章作业.docx」的 job.duplex="on"
（print_config.json 里 page_count=1, duplex="on"）→ 日志「正在打印 (份数:1, 双面:on)」
→ 驱动弹 HP「双面打印说明」向导，打出一张空白页 + 正式页。

1 页纸物理上不可能双面（配对需要 2 页），请求双面只会让驱动多送一张空白纸走一趟。
因此**任何路径**都不得再对「有效页数 == 1」的任务请求双面。本脚本验收三层：

  ① 页数已知时归一化：`MainWindow._on_convert_finished` 写回 page_count 后把 1 页文件
     的 duplex 改成 "off"（表格双面列立刻显示「单面」、编辑面板同步灰掉；
     **页数未知(0) 不得当成 1 页**，2 页文件不得被误降单面）；
  ② 打印时硬闸门（最重要）：`PrintWorker._effective_duplex` / `_print_one_job` 按
     **真实 PDF** 数页 —— 覆盖云端订单 / 旧配置 / 手工改坏；`_uses_manual_duplex` 对
     1 页任务返回 False（不进手动双面两趟批次，免得白打一张空白占位纸）；
     进度/面数预估口径同步（1 页 ×2 份 不再按"双面+分隔页"估成 3 面）；
  ③ 攒批/批次闸门同口径：`MainWindow._uses_manual_duplex_job` 与 `_print_stack_pass1`。

已知残留（lead 2026-10-17 裁定不闭合）：`page_count` 仍为 0 **且原件本身就是 PDF**
（如云端 PDF 页数分析失败）时不在判定处解析 PDF、保持旧行为 —— 手动档案下仍会进两趟
批次：驱动全程单面（绝不请求双面），但会白走一张空白背面 + 白翻一次纸；不就地解析 PDF
是因为 `verify_manual_duplex_gui.py` ①b 把「1 页 PDF（page_count=0）参与手动双面合批」
钉成了既有断言，解析 PDF 会立刻把它测红（任务要求不许改那条断言）。

全程**不出纸**：print_pdf / print_plan_pass / wait_for_output 全部桩掉；
APPDATA 指到临时沙箱，绝不碰真实 `%APPDATA%\\HN打印工具`。

    python tests/verify_single_page_simplex.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_single_page_")
os.environ["APPDATA"] = _SANDBOX                  # 用户数据目录 → 沙箱（不碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtCore import QItemSelectionModel  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import converter as _converter  # noqa: E402
# 引擎可用性探测会真的 Dispatch Word/WPS COM（离屏里慢且会拉起 wps.exe 残留）→ 桩掉
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}

import gui  # noqa: E402
import paths  # noqa: E402
import pdf_printer  # noqa: E402
from printer_config import PrintJob, PrinterProfile, TabSettings  # noqa: E402

# offscreen 下没人能回答模态框 → 一律自动回「是/好」
gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.Yes)
gui.QMessageBox.warning = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)
gui.QMessageBox.information = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ── 真 PDF 素材（worker 走 ".pdf 已是 PDF 跳过转换"；数页/合并用真实实现）──
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


PDF1 = make_pdf("one.pdf", 1)
PDF2 = make_pdf("two.pdf", 2)
PDF3 = make_pdf("three.pdf", 3)
check("测试素材：1/2/3 页真 PDF",
      all(os.path.isfile(p) and os.path.getsize(p) > 400 for p in (PDF1, PDF2, PDF3)))

MANUAL = PrinterProfile(auto_duplex=False, capability_source="manual", manual_duplex=True,
                        first="odd", reverse_second=True, second_rotate_180=True)
UNKNOWN = PrinterProfile()        # 能力未确认（= 本次事故现场：printer_profiles 为空）


# ============================================================
# 桩：记录 print_pdf / print_plan_pass 调用，模拟逐面进度
# ============================================================
class Rec:
    def __init__(self):
        self.print_pdf = []      # [kwargs]
        self.plan_pass = []      # [(which, 本趟是否旋转, 0-based 序列, 1-based 序列)]
        self.flip = []           # [翻纸提示文案]
        self.announce = []       # [常驻窗文案]
        self.logs = []
        self.progress = []
        self.finished = []
        self.all_finished = []
        self.raised = None


def patched(rec):
    orig = {n: getattr(gui, n) for n in
            ("print_pdf", "print_plan_pass", "wait_for_output", "get_converter")}

    def _print_pdf(**kw):
        rec.print_pdf.append(kw)
        # 模拟逐面进度（让进度条口径可被断言）
        cb = kw.get("progress_callback")
        if cb:
            for i in range(1, max(1, int(kw.get("copies") or 1)) + 1):
                cb(i, i)
        return True, "桩：一次提交"

    def _print_plan_pass(plan, path, printer, which, second_rotate_180=False,
                         progress_callback=None, dpi=0):
        seq0 = plan.pass_page_indices(which)
        rec.plan_pass.append((which, bool(which == 2 and second_rotate_180), seq0,
                              plan.pass_pages(which)))
        if progress_callback:
            for i in range(1, len(seq0) + 1):
                progress_callback(i, len(seq0))
        return True, f"桩：第 {which} 趟"

    def _wait_for_output(printer, pages=0, **kw):
        return {"drained": True, "queue_seconds": 0.0, "margin_seconds": 0.0,
                "peak_jobs": 0, "detail": ""}

    gui.print_pdf = _print_pdf
    gui.print_plan_pass = _print_plan_pass
    gui.wait_for_output = _wait_for_output
    gui.get_converter = lambda: object()

    def restore():
        for k, v in orig.items():
            setattr(gui, k, v)
    return restore


def run_worker(jobs, profile, flip_answer=True):
    rec = Rec()
    w = gui.PrintWorker(jobs=jobs, printer_name="Fake Print", duplex_mode="long-edge",
                        keep_temp_pdf=False, render_dpi=200, manual_duplex_profile=profile)
    w.log_message.connect(rec.logs.append)
    w.progress.connect(lambda c, t, s: rec.progress.append((c, t, s)))
    w.job_finished.connect(lambda i, ok, msg: rec.finished.append((i, ok, msg)))
    w.all_finished.connect(lambda a, b: rec.all_finished.append((a, b)))
    w.duplex_announce.connect(rec.announce.append)
    if flip_answer is not None:
        w.flip_required.connect(
            lambda text, sec: (rec.flip.append(text), w.confirm_flip(flip_answer)))
    restore = patched(rec)
    try:
        w.run()
    except BaseException as e:          # 异常逃出 run() 本身也是被测行为
        rec.raised = e
    finally:
        restore()
    return rec


def job1page(path=PDF1, page_count=1, **kw):
    """1 页任务（现场形态：page_count=1 且 duplex="on"）。`name=` 是 display_name 的简写。"""
    job_kw = dict(kw)
    job_kw.pop("name", None)
    if "name" in kw and "display_name" not in job_kw:
        job_kw["display_name"] = kw["name"]
    return PrintJob(file_path=path, copies=job_kw.pop("copies", 1),
                    duplex=job_kw.pop("duplex", "on"), duplex_mode="long-edge",
                    page_range="", page_count=page_count,
                    display_name=job_kw.pop("display_name", os.path.basename(path)),
                    **job_kw)


# ============================================================
# ① 页数已知时归一化（_on_convert_finished）
# ============================================================
print("\n=== ① 转换完成写回 page_count → 1 页文件自动改单面 ===")

win = gui.MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
_app.processEvents()


def seed(jobs):
    tab = TabSettings()
    tab.jobs = jobs
    win._config.tabs = {"1": tab}
    win._current_tab = "1"
    win._config.active_tab = "1"
    win._rebuild_table()
    win._refresh_tab_display()
    _app.processEvents()
    return jobs


def select_row0():
    win._table.clearSelection()
    win._table.selectionModel().select(
        win._table.model().index(0, 0),
        QItemSelectionModel.Select | QItemSelectionModel.Rows)
    win._on_table_selection()
    _app.processEvents()


DOCX1 = os.path.join(_SANDBOX, "第二章作业.docx")
with open(DOCX1, "wb") as _f:
    _f.write(b"not a real docx: \xe4\xbd\x9c\xe4\xb8\x9a\n")   # 内容无关，回调不读它

jobs = seed([PrintJob(file_path=DOCX1, copies=1, duplex="on", duplex_mode="long-edge",
                      page_count=0, orientation="", engine="word",
                      display_name="第二章作业.docx")])
select_row0()
# 转换前：页数未知 → 双面控件可用、面板显示「双面」
check("① 转换前：page_count=0（未知）→ 面板双面控件可用（未知不当成单页）",
      win._edit_duplex.isEnabled() is True)

win._on_convert_finished(0, DOCX1, PDF1, 1, "portrait", "")
_app.processEvents()
check("★ ① 1 页 docx 转换完成 → job.duplex 变 'off'（写回配置）",
      jobs[0].duplex == "off" and jobs[0].page_count == 1, f"{jobs[0].duplex}/{jobs[0].page_count}")
check("★ ① 表格双面列立刻显示「单面」（整行重画，不是只改页数列）",
      win._table.item(0, gui.MainWindow.COL_DUPLEX).text() == "单面",
      f"{win._table.item(0, gui.MainWindow.COL_DUPLEX).text()!r}")
check("★ ① 编辑面板同步：双面控件灰掉且停在「单面」（口径与 _selection_traits 同源）",
      win._edit_duplex.isEnabled() is False and win._edit_duplex.currentIndex() == 1,
      f"enabled={win._edit_duplex.isEnabled()} idx={win._edit_duplex.currentIndex()}")
# 落盘核对（配置里 duplex 已是 off，重启后不会再按双面请求）
_cfg = gui.PrinterConfig.load(paths.config_path())
check("★ ① 归一化结果已落盘（print_config.json 里 duplex='off'）",
      _cfg.tabs["1"].jobs[0].duplex == "off", f"{_cfg.tabs['1'].jobs[0].duplex}")

# 负例：2 页文件不得被误降单面；页数未知(0) 也不得当成 1 页
DOCX2 = os.path.join(_SANDBOX, "第三章作业.docx")
with open(DOCX2, "wb") as _f:
    _f.write(b"x\n")
jobs2 = seed([PrintJob(file_path=DOCX2, copies=1, duplex="on", page_count=0, engine="word",
                       display_name="第三章作业.docx")])
win._on_convert_finished(0, DOCX2, PDF2, 2, "portrait", "")
check("★ ① 负例：2 页 docx 仍是双面（不得一刀切改单面）",
      jobs2[0].duplex == "on"
      and win._table.item(0, gui.MainWindow.COL_DUPLEX).text() != "单面",
      f"{jobs2[0].duplex}/{win._table.item(0, gui.MainWindow.COL_DUPLEX).text()}")

DOCX0 = os.path.join(_SANDBOX, "页数未知.docx")
with open(DOCX0, "wb") as _f:
    _f.write(b"x\n")
jobs0 = seed([PrintJob(file_path=DOCX0, copies=1, duplex="on", page_count=0, engine="word",
                       display_name="页数未知.docx")])
win._on_convert_finished(0, DOCX0, PDF1, 0, "portrait", "")
check("★ ① 负例：page_count=0（未知）**不得**当成 1 页 → duplex 仍 'on'",
      jobs0[0].duplex == "on", f"{jobs0[0].duplex}")

# 攒批/无人值守闸门同口径（MainWindow 侧）
win._config.printer_name = "Fake Manual"
win._config.printer_profiles["Fake Manual"] = MANUAL
check("★ ③ MainWindow._uses_manual_duplex_job：1 页 + 手动机型 → False（不给它配空白占位纸）",
      win._uses_manual_duplex_job(job1page()) is False)
check("★ ③ MainWindow._uses_manual_duplex_job：2 页 + 手动机型 → True（照旧两趟）",
      win._uses_manual_duplex_job(job1page(PDF2, page_count=2)) is True)
check("★ ③ MainWindow._uses_manual_duplex_job：页数未知(0) + 手动机型 → True（旧行为不变）",
      win._uses_manual_duplex_job(PrintJob(file_path=DOCX0, duplex="on", page_count=0)) is True)
check("★ ④ MainWindow._uses_manual_duplex_job：云端 1 页任务（task_id/order_id）同样被闸住",
      win._uses_manual_duplex_job(
          job1page(task_id=123, order_id=456, order_number="HN20261017-0001")) is False)


# ============================================================
# ② 打印时硬闸门（PrintWorker._effective_duplex / _print_one_job）
# ============================================================
print("\n=== ② 打印时硬闸门：1 页任务既不进两趟批次、也不请求双面 ===")

rec = run_worker([job1page()], MANUAL)
check("★ ② 手动机型 + 1 页 + duplex='on'：不走手动双面批次（没有两趟、没有翻纸窗）",
      not rec.plan_pass and not rec.flip and not rec.announce,
      f"plan_pass={[p[0] for p in rec.plan_pass]} flip={len(rec.flip)}")
check("★ ② 也**不把 duplex='on' 传给 print_pdf**（桩断言收到 'off'）",
      len(rec.print_pdf) == 1 and rec.print_pdf[0]["duplex"] == "off",
      f"{[c.get('duplex') for c in rec.print_pdf]}")
check("② 有可读日志说明本次按单面打",
      any("单面" in m and "1 页" in m for m in rec.logs),
      f"{[m for m in rec.logs if '单面' in m]}")
check("② 任务照常成功回报、批次收尾正常",
      rec.finished == [(0, True, rec.finished[0][2])] and rec.all_finished == [(1, 0)],
      f"{rec.finished} {rec.all_finished}")

rec = run_worker([job1page()], UNKNOWN)
check("★ ③ 能力未确认（本次事故现场：printer_profiles 为空）→ 同样只传 'off'",
      len(rec.print_pdf) == 1 and rec.print_pdf[0]["duplex"] == "off" and not rec.plan_pass,
      f"{[c.get('duplex') for c in rec.print_pdf]}")

# ④ 云端 1 页任务：job 由云端下发（duplex='on'，本机页数分析后 page_count=1）
cloud1 = job1page(task_id=123, order_id=456, order_number="HN20261017-0001",
                  display_name="云端单页作业.docx")
rec = run_worker([cloud1], MANUAL)
check("★ ④ 云端 1 页任务（duplex='on'）→ 既不走两趟，也只传 'off'",
      not rec.plan_pass and len(rec.print_pdf) == 1
      and rec.print_pdf[0]["duplex"] == "off",
      f"plan_pass={len(rec.plan_pass)} duplex={[c.get('duplex') for c in rec.print_pdf]}")

# 旧配置 / 手工改坏：docx 的 page_count 已过时（写 0），但缓存 PDF 就是 1 页 →
# 打印线程按**真实 PDF** 补判，仍按单面直打（绝不给它配空白占位纸）
stale = PrintJob(file_path=DOCX1, cached_pdf=PDF1, copies=1, duplex="on",
                 duplex_mode="long-edge", page_range="", page_count=0,
                 display_name="旧配置单页.docx")
rec = run_worker([stale], MANUAL)
check("★ ② 转换件 page_count 过时(=0) 但缓存 PDF 只 1 页 → 转换后补判，按单面直打",
      len(rec.print_pdf) == 1 and rec.print_pdf[0]["duplex"] == "off" and not rec.plan_pass,
      f"plan_pass={len(rec.plan_pass)} duplex={[c.get('duplex') for c in rec.print_pdf]}")

# 边界（刻意保留的既有行为）：原件本身就是 PDF、page_count 未知(0) 时，判据只用
# job.page_count（未知不当成 1 页）→ 仍按手动机型的合批走；但两趟**全程 SIMPLEX**，
# 驱动永远不会收到 duplex="on"（verify_manual_duplex_gui.py 把这条钉成了既有行为）。
rec = run_worker([PrintJob(file_path=PDF1, copies=1, duplex="on", duplex_mode="long-edge",
                           page_range="", page_count=0, display_name="单页.pdf")], MANUAL)
check("★ ② 边界：1 页 PDF + page_count 未知(0) → 驱动侧**一个 duplex='on' 都没有**",
      all(c.get("duplex") != "on" for c in rec.print_pdf),
      f"print_pdf={[c.get('duplex') for c in rec.print_pdf]} 两趟={[p[0] for p in rec.plan_pass]}")

# 页数未知且数不出来（文件不是 PDF / 不存在）→ 不能当成 1 页，保持请求原样
unknown_job = PrintJob(file_path=os.path.join(_SANDBOX, "不存在.docx"), copies=1,
                       duplex="on", page_count=0, display_name="未知.docx")
w_probe = gui.PrintWorker(jobs=[unknown_job], printer_name="Fake Print",
                          duplex_mode="long-edge", keep_temp_pdf=False,
                          render_dpi=200, manual_duplex_profile=UNKNOWN)
check("★ ② 页数确实未知（数不出来）→ 不当作 1 页，保持 'on'（不误降单面）",
      w_probe._effective_pages(unknown_job) == 0
      and w_probe._effective_duplex(unknown_job) == "on",
      f"{w_probe._effective_pages(unknown_job)}/{w_probe._effective_duplex(unknown_job)}")

# 进度/面数预估口径同步：1 页 ×2 份 + duplex='on' → 实际 2 面（旧口径会按"双面+分隔页"估 3）
rec = run_worker([job1page(copies=2)], UNKNOWN)
_total = rec.progress[0][1] if rec.progress else -1
check("★ ② 进度预估口径同步：1 页 ×2 份 按单面估 2 面（旧的 'on' 口径会估 3 面）",
      _total == 2 and rec.print_pdf[0]["duplex"] == "off" and rec.print_pdf[0]["copies"] == 2,
      f"total={_total} duplex={rec.print_pdf[0].get('duplex')} copies={rec.print_pdf[0].get('copies')}")


# ============================================================
# ⑤ 零回归：多页文件照旧请求双面 / 手动双面两趟流程照旧
# ============================================================
print("\n=== ⑤ 零回归 ===")

rec = run_worker([job1page(PDF2, page_count=2)], UNKNOWN)
check("⑤ 2 页 + duplex='on'（能力未确认）→ 仍传 'on'",
      len(rec.print_pdf) == 1 and rec.print_pdf[0]["duplex"] == "on",
      f"{[c.get('duplex') for c in rec.print_pdf]}")

rec = run_worker([job1page(PDF3, page_count=3)], UNKNOWN)
check("⑤ 3 页 + duplex='on'（能力未确认）→ 仍传 'on'",
      len(rec.print_pdf) == 1 and rec.print_pdf[0]["duplex"] == "on",
      f"{[c.get('duplex') for c in rec.print_pdf]}")

rec = run_worker([job1page(PDF3, page_count=3, name="三页.docx")], MANUAL)
check("★ ⑤ 3 页 + 手动机型 → 手动双面两趟照旧（第 1 趟 1 3、第 2 趟 空白 2）",
      [p[0] for p in rec.plan_pass] == [1, 2] and rec.plan_pass[0][3] == [1, 3]
      and rec.plan_pass[1][3] == [None, 2] and len(rec.flip) == 1,
      f"{[p[3] for p in rec.plan_pass]}")

rec = run_worker([job1page(PDF2, page_count=2)], MANUAL)
check("⑤ 2 页 + 手动机型 → 手动双面两趟照旧（1/2，无空白占位）",
      [p[0] for p in rec.plan_pass] == [1, 2] and rec.plan_pass[0][3] == [1]
      and rec.plan_pass[1][3] == [2],
      f"{[p[3] for p in rec.plan_pass]}")

# 混合批次：多页文件仍按两趟并批
rec = run_worker([job1page(PDF3, page_count=3, name="A.docx"),
                  job1page(PDF2, page_count=2, name="B.docx")], MANUAL)
check("⑤ 混合：两个多页文件合并成一批一次翻纸",
      len(rec.plan_pass) == 2 and len(rec.flip) == 1 and not rec.print_pdf,
      f"plan_pass={len(rec.plan_pass)} flip={len(rec.flip)} print_pdf={len(rec.print_pdf)}")

# 2026-10-26：**整单一个批次** —— 1 页文件不再单独直打（那会多一个 spooler 作业），
# 而是按 FilePlan(duplex=False) 并进同一份页序（一张纸、背面留空，第 2 趟打空白占位页）。
# 关键不变式仍在：它**没有**被排成双面配对（每页一张纸），驱动侧也永远收不到 duplex="on"。
rec_mix = run_worker([job1page(PDF3, page_count=3, name="A.docx"),
                      job1page(PDF2, page_count=2, name="B.docx"),
                      job1page(PDF1, page_count=1, name="C单页.docx")], MANUAL)
check("★ ⑤ 混排：整单一个批次（不再单独直打 1 页文件）",
      len(rec_mix.print_pdf) == 0 and all(c.get("duplex") != "on" for c in rec_mix.print_pdf)
      and len(rec_mix.plan_pass) == 2 and len(rec_mix.flip) == 1,
      f"print_pdf={[c.get('duplex') for c in rec_mix.print_pdf]} plan_pass={len(rec_mix.plan_pass)}")
check("★ ⑤ 混排：1 页文件在页序里只占**一张纸**（第 1 趟打它、第 2 趟空白占位）",
      rec_mix.plan_pass[0][3] == [1, 3, 4, 6]
      and rec_mix.plan_pass[1][3] == [None, 5, None, 2],
      f"{[p[3] for p in rec_mix.plan_pass]}")
check("⑤ 混排：三个任务都收到完成回报",
      sorted(i for i, ok, _m in rec_mix.finished) == [0, 1, 2]
      and all(ok for _i, ok, _m in rec_mix.finished), f"{rec_mix.finished}")


# ============================================================
# ⑥ 攒批第 1 趟：1 页任务不给它排双面配对（张数不变，只是不再谎称双面）
# ============================================================
print("\n=== ⑥ 攒批第 1 趟的单页口径 ===")
_batch_dir = os.path.join(_SANDBOX, "batch_1")
os.makedirs(_batch_dir, exist_ok=True)
OUT = os.path.join(_batch_dir, "entry_0001.pdf")
_rec6 = Rec()
_w6 = gui.PrintWorker(jobs=[job1page(PDF1, page_count=1, name="单页.docx")],
                      printer_name="Fake Print", duplex_mode="long-edge",
                      keep_temp_pdf=False, render_dpi=200, manual_duplex_profile=MANUAL,
                      stack_mode=True, stack_out_pdf=OUT, stack_seq=1, stack_rotate=True)
_w6.log_message.connect(lambda _m: None)
_plans6 = []
_w6.stack_entry_planned.connect(_plans6.append)
_restore6 = patched(_rec6)
try:
    _w6.run()
finally:
    _restore6()
_pl6 = _plans6[0] if _plans6 else {}
check("★ ⑥ 攒批单里的 1 页任务 file_plans 标 duplex=False（不再谎称双面任务）",
      _pl6.get("file_plans") == [{"label": "单页.docx", "pages": [1], "file_pages": [1],
                                  "duplex": False}],
      f"{_pl6.get('file_plans')}")
check("⑥ 但它仍占 1 张纸、第 2 趟给一个空白占位槽（张数不变，配对结构不破）",
      _pl6.get("sheet_count") == 1 and _pl6.get("pass1_pages") == [1]
      and _pl6.get("pass2_pages") == [None],
      f"{_pl6.get('sheet_count')}/{_pl6.get('pass1_pages')}/{_pl6.get('pass2_pages')}")
check("⑥ 攒批第 1 趟仍只打一趟（which=1），不出双面请求",
      [p[0] for p in _rec6.plan_pass] == [1] and all(c.get("duplex") != "on" for c in _rec6.print_pdf),
      f"{[p[0] for p in _rec6.plan_pass]}")

print("\n" + "=" * 56)
print("单页文件一律单面 验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
