# -*- coding: utf-8 -*-
"""验收脚本 · 「封面单独打印」（单面页与双面页互相穿插，空白占位页必须正确）

需求（2026-11-02 用户，31 页 PDF「人工智能程序设计习题」现场）：
一个文档的第一页封面要求单面打、剩下双面打。旧做法是拆成两个标签页（1 页 + 2-31 页）打两次；
新做法是在任务参数面板勾「封面单独打印」，填写需要**单面**的页，其余页双面。
封面页可能不止 1 页（前置内容也要单面），所以页数必须能手动填、能填多行；
单面段与双面段**允许互相穿插**（一个 PDF 里有多份任务时会这样），**不允许重叠**。

物理约束（为什么只能程序自己分两趟）：每张纸两趟各占**且只占一个槽位**，内容不足补
空白占位页；单面页占据的那张纸的背面必须打空白占位页（只走纸不打墨），否则整叠错位。
所以本脚本的核心断言不是"页列表看起来对"，而是：

  ① 纯页序：31 页例子拆出的纸序 / 两趟槽位 / 空白占位页位置逐项对；
  ② 不变式：把两趟按"整叠原样放回、face-down 出纸"还原回纸的正面/背面，必须与
     `(页,是否双面)` 语义完全一致（漏一个占位页这里就会串页）；
  ③ 计费：按**摊纸规则**收钱（单面页各计 1 张×单面价、双面段按张计双面价）；
  ④ PrintWorker 真的走"程序两趟"路线，并且把混排计划交给 print_plan_pass；
     全单面（勾了全部页）必须退回纯单面，不能白走一趟空白 + 白翻一次纸；
  ⑤ GUI：复选框在「页码范围」**上面**、勾上有默认值、多行框随勾选显隐、多选项卡灰掉、
     灰掉的控件不回写。

全程**不出纸**：print_pdf / print_plan_pass / wait_for_output 全部桩掉；
APPDATA 指到临时沙箱，绝不碰真实 `%APPDATA%\\HN打印工具`。

    python tests/verify_simplex_cover.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_simplex_cover_")
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
import manual_duplex  # noqa: E402
import paths  # noqa: E402
from printer_config import (PrintJob, PrinterProfile, TabSettings,  # noqa: E402
                            calc_cost, job_simplex_pages)

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


def make_pdf(name: str, pages: int) -> str:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    path = os.path.join(_SANDBOX, name)
    c = canvas.Canvas(path, pagesize=A4)
    for i in range(pages):
        c.drawString(72, 760, f"{name} page {i + 1}")
        c.showPage()
    c.save()
    return path


PDF31 = make_pdf("heji.pdf", 31)
check("测试素材：31 页真 PDF", os.path.isfile(PDF31) and os.path.getsize(PDF31) > 1000)

MANUAL = PrinterProfile(auto_duplex=False, capability_source="manual", manual_duplex=True,
                        first="odd", reverse_second=True, second_rotate_180=True)

# ============================================================
# ① 纯页序：31 页的最典型形态
#    1 单面 | 2-15 双面 | 16-23 单面 | 24-31 双面  ⇒ 1+7+8+4 = 20 张纸
# ============================================================
print("\n=== ① 纯页序：单面/双面穿插（31 页现场形态）===")

PAGES31 = list(range(1, 32))
SX = {1} | set(range(16, 24))

check("①a 段落切分：单面段与双面段逐段拆开、段内连续",
      manual_duplex.split_simplex_runs(PAGES31, SX)
      == [(list(range(1, 2)), False), (list(range(2, 16)), True),
          (list(range(16, 24)), False), (list(range(24, 32)), True)],
      str(manual_duplex.split_simplex_runs(PAGES31, SX)))

plans = manual_duplex.file_plans_for_pages("f", PAGES31, True, sorted(SX), None)
check("①b 计划条数 = 段数（单面段整段单面、双面段整段双面）",
      [p.pages for p in plans] == [list(range(1, 2)), list(range(2, 16)),
                                   list(range(16, 24)), list(range(24, 32))]
      and [p.duplex for p in plans] == [False, True, False, True],
      f"{[(p.pages, p.duplex) for p in plans]}")

check("①c 无单面页 ⇒ 单条老口径计划（一个页序分支都不动）",
      [p.pages for p in manual_duplex.file_plans_for_pages("f", PAGES31, True, [], None)]
      == [PAGES31])

plan = manual_duplex.plan_passes(plans, copies=1)
check("★ ①d 张数 = 20（不是 ceil(31/2)=16：9 个单面页各占一张纸）",
      plan.sheet_count == 20, f"sheet_count={plan.sheet_count}")
check("★ ①e 面数 = 40（20 张纸 × 两趟，第 2 趟含 9 个空白占位面）",
      plan.total_sides == 40, f"total_sides={plan.total_sides}")

EXPECT_P1 = [1, 2, 4, 6, 8, 10, 12, 14, 16, 17, 18, 19, 20, 21, 22, 23, 24, 26, 28, 30]
EXPECT_P2 = [31, 29, 27, 25] + [None] * 8 + [15, 13, 11, 9, 7, 5, 3, None]
check("★ ①f 第 1 趟页序", plan.pass_pages(1) == EXPECT_P1, str(plan.pass_pages(1)))
check("★ ①g 第 2 趟页序（逆序；单面页的背面是空白占位页 = None）",
      plan.pass_pages(2) == EXPECT_P2, str(plan.pass_pages(2)))
check("①h 空白占位页数量 = 单面页数量（每张单面纸都要一个空白背面）",
      plan.blank_slots_first + plan.blank_slots_second == len(SX),
      f"{plan.blank_slots_first}+{plan.blank_slots_second} 应为 {len(SX)}")

# 每条纸的槽位：单面页必须独占一张纸（另一面 None），双面段必须两两配一纸
bad = []
for sh in plan.sheets:
    if sh.side_a in SX or sh.side_b in SX:
        continue
    if (sh.side_a is None) != (sh.side_b is None):
        bad.append(sh)
check("①i 双面段每张纸两面都有内容；单面页所在纸另一面为空",
      not bad, str(bad[:3]))

# ============================================================
# ② 不变式：两趟按物理顺序还原回一张张纸，必须与原语义一致
# ============================================================
print("\n=== ② 物理还原不变式（整叠原样放回、face-down 出纸）===")


def reconstruct(plan):
    """模拟现场：第 1 趟按纸序打正面 → face-down 出纸（最上面是最后一张）→
    整叠原样放回 → 第 2 趟逆序打背面。返回 [(正面页, 背面页)]（按纸序）。"""
    p1 = plan.pass_pages(1)
    p2 = plan.pass_pages(2)
    assert len(p1) == len(p2) == plan.sheet_count, (len(p1), len(p2))
    # 第 2 趟逆序 ⇒ p2[i] 是第 (n-1-i) 张纸的背面（p2[0] 打的是最上面那张 = 最后一张纸）
    n = len(p1)
    return [(p1[i], p2[n - 1 - i]) for i in range(n)]


pairs = reconstruct(plan)
expect_pairs = [((1, None))] + [(a, b) for a, b in
                               zip(range(2, 16, 2), range(3, 16, 2))] + \
               [(p, None) for p in range(16, 24)] + \
               [(a, b) for a, b in zip(range(24, 32, 2), range(25, 32, 2))]
check("★ ②a 还原出的 20 张纸与预期 (正面,背面) 完全一致（漏一个占位页这里就串页）",
      pairs == expect_pairs,
      f"got={pairs[:3]}…{pairs[-3:]}")

flat = []
for a, b in pairs:
    flat.append(a)
    if b is not None:
        flat.append(b)
check("★ ②b 还原后按纸序读出的全部页 = 1..31 且不重不漏",
      flat == PAGES31, f"{flat}")

# 覆盖：单面页在首/中/尾、单面页相邻、全单面、全双面
for label, sx in [("单面在中间", {5, 6}), ("单面在尾部", {30, 31}),
                  ("单面在首部", {1, 2}), ("单面单页", {17})]:
    _plans = manual_duplex.file_plans_for_pages("f", PAGES31, True, sorted(sx), None)
    _plan = manual_duplex.plan_passes(_plans, copies=1)
    _pairs = reconstruct(_plan)
    _flat = []
    for a, b in _pairs:
        _flat.append(a)
        if b is not None:
            _flat.append(b)
    # 不变式（注意：双面段页数为奇数时末页的正/背面也会是 (末页, None)，那是老口径的
    # 正常收尾，不是缺失占位页 —— 所以判据只钉「单面页的背面必为空」+「张数对」+「页序全」）
    _sx_bad = [(a, b) for a, b in _pairs if a in sx and b is not None]
    check(f"②c 穿插形态「{label}」：页序完整、单面页背面必为空白、张数按摊纸规则",
          _flat == PAGES31
          and not _sx_bad
          and _plan.sheet_count == manual_duplex.count_sheets(PAGES31, sx),
          f"flat_ok={_flat == PAGES31} sx_bad={_sx_bad[:3]} "
          f"sheets={_plan.sheet_count}/{manual_duplex.count_sheets(PAGES31, sx)}")

_all = manual_duplex.plan_passes(
    manual_duplex.file_plans_for_pages("f", PAGES31, True, PAGES31, None), copies=1)
check("②d 全单面 ⇒ 31 张纸、第 2 趟整趟空白（路由层会拦下这种输入，纯逻辑仍须自洽）",
      _all.sheet_count == 31
      and all(p is None for p in _all.pass_pages(2))
      and _all.pass_pages(1) == PAGES31)

# 多份：每份都要重复同一套页序（空白占位页按份重复）
_two = manual_duplex.plan_passes(
    manual_duplex.file_plans_for_pages("f", PAGES31, True, sorted(SX), None), copies=2)
check("②e 打 2 份：张数与空白占位页数都翻倍",
      _two.sheet_count == 40
      and _two.blank_slots_first + _two.blank_slots_second == len(SX) * 2,
      f"{_two.sheet_count}/{_two.blank_slots_first + _two.blank_slots_second}")

# ============================================================
# ③ 计费：按摊纸规则收钱
# ============================================================
print("\n=== ③ 计费（单面价 0.3 / 双面价 0.4，与界面默认配置同值）===")


def cost(pages, sx, page_range="", copies=1, duplex="on", n=31):
    return calc_cost(n, copies, duplex, 0.3, 0.4, page_range, sx)


check("③a 纯双面 2-31（15 张）：15×0.40",
      cost(31, "", "2-31") == (6.0, "15张×0.40"), str(cost(31, "", "2-31")))
check("★ ③b 封面单面 1 + 其余双面：15×0.40 + 1×0.30 = ¥6.30（旧做法两个标签页也是这笔钱）",
      cost(31, "1") == (6.3, "15张×0.40+1张×0.30"), str(cost(31, "1")))
check("★ ③c 穿插 1,16-23 单面：11×0.40 + 9×0.30 = ¥7.10（= 20 张纸的钱）",
      cost(31, "1,16-23") == (7.1, "11张×0.40+9张×0.30"), str(cost(31, "1,16-23")))
check("③d 多份：(2张×0.40+1张×0.30)×2份（5 页，第 1 页单面）",
      cost(5, "1", copies=2, n=5) == (2.2, "(2张×0.40+1张×0.30)×2份"),
      str(cost(5, "1", copies=2, n=5)))
check("③e 纯单面请求不受影响（duplex=off 走老口径）",
      abs(cost(31, "1", duplex="off")[0] - 9.3) < 1e-9, str(cost(31, "1", duplex="off")))
check("③f 单面页落在页码范围外 ⇒ 静默忽略（不该把整单卡住）",
      cost(31, "1", "2-31") == (6.0, "15张×0.40"), str(cost(31, "1", "2-31")))

_j = PrintJob(file_path="x.pdf", duplex="on", page_count=31, simplex_enabled=True,
              simplex_pages="1,16-23")
check("③g 计费口径收敛点 `job_simplex_pages`：未勾选 ⇒ 空串（老调用点原样）",
      job_simplex_pages(PrintJob(file_path="x.pdf", duplex="on", page_count=31,
                                 simplex_enabled=False, simplex_pages="1,16-23")) == ""
      and job_simplex_pages(_j) == "1,16-23")

# ============================================================
# ④ PrintWorker：真的走"程序两趟"，并把混排计划交给渲染层
# ============================================================
print("\n=== ④ PrintWorker 路线与页序（不出纸）===")


class Rec:
    def __init__(self):
        self.plan_pass = []
        self.print_pdf = []
        self.flip = []
        self.logs = []
        self.announce = []
        self.raised = None


def patched(rec):
    orig = {n: getattr(gui, n) for n in
            ("print_pdf", "print_plan_pass", "wait_for_output",
             "count_pending_print_jobs")}

    def _print_plan_pass(plan, path, printer, which, second_rotate_180=False,
                         progress_callback=None, dpi=0, landscape_fill=True, **kw):
        seq0 = plan.pass_page_indices(which)
        rec.plan_pass.append((which, bool(which == 2 and second_rotate_180), seq0,
                              plan.pass_pages(which)))
        return True, f"桩：第 {which} 趟"

    def _print_pdf(**kw):
        rec.print_pdf.append(kw)
        return True, "桩：单面直打"

    def _wait_for_output(printer, pages=0, **kw):
        return {"drained": True, "queue_seconds": 0.0, "margin_seconds": 0.0,
                "peak_jobs": 0, "detail": ""}

    gui.print_pdf = _print_pdf
    gui.print_plan_pass = _print_plan_pass
    gui.wait_for_output = _wait_for_output
    gui.count_pending_print_jobs = lambda *a, **k: 0

    def restore():
        for k, v in orig.items():
            setattr(gui, k, v)
    return restore


def run_worker(jobs, profile):
    rec = Rec()
    w = gui.PrintWorker(jobs=jobs, printer_name="Fake Print", duplex_mode="long-edge",
                        keep_temp_pdf=False, render_dpi=200, manual_duplex_profile=profile)
    w.log_message.connect(rec.logs.append)
    w.duplex_announce.connect(rec.announce.append)
    w.flip_required.connect(lambda text, sec: (rec.flip.append(text), w.confirm_flip(True)))
    restore = patched(rec)
    try:
        w.run()
    except BaseException as e:
        rec.raised = e
    finally:
        restore()
    return rec


def mkjob(**kw):
    base = dict(file_path=PDF31, copies=1, duplex="on", duplex_mode="long-edge",
                page_range="1-31", page_count=31, display_name="heji.pdf")
    base.update(kw)
    return PrintJob(**base)


# ④a 手动双面机型 + 封面单面
r = run_worker([mkjob(simplex_enabled=True, simplex_pages="1,16-23")], MANUAL)
check("★ ④a 勾了单面页 ⇒ 走「程序自己分两趟」（即使这台机器的档案本可走驱动手动双面）",
      r.raised is None and len(r.plan_pass) == 2,
      f"raised={r.raised} plan_pass={[p[0] for p in r.plan_pass]}")
if len(r.plan_pass) == 2:
    check("★ ④b 第 1 趟交给渲染层的 0-based 槽位 = 混排页序（含单面页独占一张纸）",
          r.plan_pass[0][2] == [p - 1 for p in EXPECT_P1],
          str(r.plan_pass[0][2]))
    check("★ ④c 第 2 趟槽位里的空白占位页位置正确（None 只出现在单面页的背面）",
          r.plan_pass[1][2] == [None if p is None else p - 1 for p in EXPECT_P2],
          str(r.plan_pass[1][2]))
    check("④d 翻纸提示里两趟页序与空白占位页都写清楚了（操作员按它核纸）",
          any("1 2 4 6" in t for t in r.flip) and any("空白" in t for t in r.flip),
          str(r.flip)[:200])

# ④e 全单面（勾了全部页）⇒ 不能白走一趟空白
r2 = run_worker([mkjob(simplex_enabled=True, simplex_pages="1-31")], MANUAL)
check("★ ④e 全部页都标单面 ⇒ 退回纯单面（只打一趟、不翻纸）",
      r2.raised is None and len(r2.plan_pass) == 0 and len(r2.print_pdf) == 1
      and not r2.flip,
      f"plan_pass={len(r2.plan_pass)} print_pdf={len(r2.print_pdf)} flip={len(r2.flip)}")
if r2.print_pdf:
    check("④f 纯单面路线请求的就是单面（duplex=off）",
          (r2.print_pdf[0].get("duplex") or "") == "off", str(r2.print_pdf[0])[:160])

# ④g 没有勾选 ⇒ 老行为（手动档案机型仍走两趟、页序与改造前一致：
#     第 1 趟打每张纸的正面 = 奇数页，第 2 趟逆序打背面）
r3 = run_worker([mkjob()], MANUAL)
check("④g 未勾选「封面单独打印」⇒ 页序老口径（第 1 趟奇数页 1,3,…,31 两趟）",
      len(r3.plan_pass) == 2
      and r3.plan_pass[0][3] == list(range(1, 32, 2))
      and r3.plan_pass[1][3] == [None] + list(range(30, 1, -2)),
      str([p[3] for p in r3.plan_pass]))

# ④h 自动双面机型（没有手动双面档案）+ 混排 ⇒ 也要能打（不能 AttributeError）
r4 = run_worker([mkjob(simplex_enabled=True, simplex_pages="1")], None)
check("★ ④h 自动双面机型勾了封面单面 ⇒ 用页序模块默认参数走两趟（不崩）",
      r4.raised is None and len(r4.plan_pass) == 2,
      f"raised={r4.raised!r} plan_pass={len(r4.plan_pass)}")

# ④i 混排 + 单面页在页码范围之外 ⇒ 等同未勾选
r5 = run_worker([mkjob(page_range="2-31", simplex_enabled=True, simplex_pages="1")], MANUAL)
check("④i 单面页全在页码范围外 ⇒ 静默忽略（照老页序打，不报错）",
      r5.raised is None and len(r5.plan_pass) == 2,
      f"raised={r5.raised!r}")

# ============================================================
# ⑤ GUI：控件位置、默认值、显隐、灰控件不回写
# ============================================================
print("\n=== ⑤ 任务参数面板 ===")

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


def panel_index(widget, ref):
    """控件在参考控件所在那一层布局里的序号（三个控件必须同一层才可比）。"""
    lay = ref.parentWidget().layout() if ref.parentWidget() else None
    if lay is None:
        return None
    for i in range(lay.count()):
        item = lay.itemAt(i)
        if item and item.widget() is widget:
            return i
    return None


cfg = win._config
cfg.simplex_price = 0.3
cfg.duplex_price = 0.4

jobs = seed([PrintJob(file_path=PDF31, copies=1, duplex="on", duplex_mode="long-edge",
                      page_range="1-31", page_count=31, display_name="heji.pdf")])
select_row0()

i_sx = panel_index(win._edit_simplex, win._edit_simplex)
i_sx_pages = panel_index(win._edit_simplex_pages, win._edit_simplex)
i_pages = panel_index(win._label_range, win._edit_simplex)
i_pages_input = panel_index(win._edit_page_range, win._edit_simplex)
check("★ ⑤a 复选框在「页码范围」**上面**（用户明确要求的版式）",
      i_sx is not None and i_pages is not None and i_sx < i_pages,
      f"simplex={i_sx} label_range={i_pages}")
check("⑤b 多行页输入紧跟在复选框下方、仍在页码范围上方",
      i_sx_pages is not None and i_sx < i_sx_pages < i_pages,
      f"simplex={i_sx} 单面页输入={i_sx_pages} 页码范围标签={i_pages}"
      f"（页码范围输入={i_pages_input}）")
check("⑤c 未勾选时多行页输入隐藏（不占版面）",
      win._edit_simplex_pages.isVisible() is False)
check("⑤d 控件是 ThemedCheckBox（硬规矩：新增复选框不许裸 QCheckBox）",
      type(win._edit_simplex).__name__ == "ThemedCheckBox",
      type(win._edit_simplex).__name__)

win._edit_simplex.setChecked(True)
win._on_simplex_toggled(True)
_app.processEvents()
check("★ ⑤e 勾上后多行页输入出现，且自动填「1」（99% 的现场用法：只打封面）",
      win._edit_simplex_pages.isVisible() is True
      and win._edit_simplex_pages.get_ranges() == "1",
      f"visible={win._edit_simplex_pages.isVisible()} text={win._edit_simplex_pages.get_ranges()!r}")
check("★ ⑤f 勾选立刻写回任务（不等焦点离开）：simplex_enabled=True / simplex_pages='1'",
      jobs[0].simplex_enabled is True and jobs[0].simplex_pages == "1",
      f"{jobs[0].simplex_enabled}/{jobs[0].simplex_pages!r}")
check("⑤g 页码范围本身不被改写（单面页是独立的一份输入，不改写 page_range）",
      jobs[0].page_range == "1-31", jobs[0].page_range)

win._edit_simplex_pages.set_ranges("1,16-23")
win._edit_simplex_pages.rangesChanged.emit()
_app.processEvents()
check("⑤h 多行页输入写入 simplex_pages",
      jobs[0].simplex_pages == "1,16-23", jobs[0].simplex_pages)

check("⑤i 表格「页数」列标出「封面单面」（操作员一眼看见这一单有特殊页）",
      "封面单面" in win._table.item(0, gui.MainWindow.COL_PAGES).text(),
      win._table.item(0, gui.MainWindow.COL_PAGES).text())

row_cost = win._table.item(0, gui.MainWindow.COL_COST).text()
check("★ ⑤j 表格费用列 = ¥7.10（11张×0.40 + 9张×0.30，和计费口径同源）",
      "7.10" in row_cost, row_cost)

# 多选 ⇒ 整片不可编辑（与页码范围同口径）
jobs.append(PrintJob(file_path=make_pdf("b.pdf", 3), copies=1, duplex="on",
                     page_count=3, page_range="", display_name="b.pdf"))
win._rebuild_table()
_app.processEvents()
win._table.clearSelection()
win._table.selectionModel().select(
    win._table.model().index(0, 0),
    QItemSelectionModel.Select | QItemSelectionModel.Rows)
win._table.selectionModel().select(
    win._table.model().index(1, 0),
    QItemSelectionModel.Select | QItemSelectionModel.Rows)
win._on_table_selection()
_app.processEvents()
check("★ ⑤k 多选 ⇒ 复选框灰掉（与「页码范围」整体禁用同口径）",
      win._edit_simplex.isEnabled() is False)
check("⑤l 多选时也没把 simplex 写进第二行（灰控件不回写）",
      jobs[1].simplex_enabled is False and jobs[1].simplex_pages == "",
      f"{jobs[1].simplex_enabled}/{jobs[1].simplex_pages!r}")

# 取消勾选 ⇒ 只关开关，保留已填的页（再勾回来不用重填）
jobs.pop()
win._rebuild_table()
_app.processEvents()
select_row0()
win._edit_simplex.setChecked(False)
win._on_simplex_toggled(False)
_app.processEvents()
check("★ ⑤m 取消勾选：simplex_enabled=False，但已填的页保留在配置里",
      jobs[0].simplex_enabled is False and jobs[0].simplex_pages == "1,16-23",
      f"{jobs[0].simplex_enabled}/{jobs[0].simplex_pages!r}")
base_cost = calc_cost(31, 1, "on", 0.3, 0.4, "1-31", "")
now_cost = calc_cost(31, 1, "on", 0.3, 0.4, "1-31", job_simplex_pages(jobs[0]))
check("⑤n 取消勾选后多行页输入隐藏、任务按老页序打（计费回到纯双面）",
      win._edit_simplex_pages.isVisible() is False
      and job_simplex_pages(jobs[0]) == ""
      and now_cost == base_cost == (6.3, "15张×0.40+1张×0.30"),
      f"visible={win._edit_simplex_pages.isVisible()} "
      f"sx={job_simplex_pages(jobs[0])!r} cost={now_cost}")

# 图片文件：图片一律单面，不该出现「封面单独打印」
img = os.path.join(_SANDBOX, "pic.png")
from PySide6.QtGui import QImage  # noqa: E402
QImage(120, 80, QImage.Format_RGB32).save(img)
jobs2 = seed([PrintJob(file_path=img, copies=1, duplex="off", page_count=1,
                       display_name="pic.png")])
select_row0()
check("⑤o 图片/1 页文件 ⇒ 复选框灰掉（没有「封面」可言）",
      win._edit_simplex.isEnabled() is False)

# 已锁定标签页：勾不动
jobs3 = seed([PrintJob(file_path=PDF31, copies=1, duplex="on", page_count=31,
                       page_range="1-31", display_name="heji.pdf")])
win._config.tabs["1"].frozen = True
select_row0()
check("⑤p 已锁定标签页 ⇒ 复选框灰掉且不写回任务",
      win._edit_simplex.isEnabled() is False and jobs3[0].simplex_enabled is False,
      f"enabled={win._edit_simplex.isEnabled()} on={jobs3[0].simplex_enabled}")

win.close()
print("\n" + ("✅ 全部通过" if ok_all else "❌ 存在失败项"))
sys.exit(0 if ok_all else 1)
