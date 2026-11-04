# -*- coding: utf-8 -*-
"""验收脚本 · 攒批窗口的「身份/形状自适应」（offscreen，不真打印）

用户场景（2026-11-01）：「优化攒批列表和打印列表的展示方式。需要考虑支持双面和不支持双面、
不支持双面但不开启攒批模式这三种情况下，打印单个任务、多个任务时攒批列表和打印列表的展示
方式，请逐个模拟。」

改造前的事实（截图 `截图展示/列表展示_改造前_*.png`，6 个场景全部 1180×520）：
不管哪种机型/模式、不管几个任务，窗口都长得一模一样 —— 标题都叫「🖐 攒批列表」、
区块都是「已攒 + 等待打印 + 打印列表」、按钮都是 5 个、顶部都是 4 行讲攒批的长文。
在自动双面的机器上，两块攒批表格恒空却占掉 80% 的高度，真正在打的那个任务挤在最下面一行。

本脚本钉住改造后的口径（6 个场景逐个断言，**这是"逐个模拟"的可执行版本**）：

  ① 自动双面（攒批不适用）+ 单任务/多任务
     ⇒ 标题只写「🖨 打印列表」；左右两栏隐藏；三个"动纸"按钮隐藏；打印列表可见；
       没有半句攒批宣传（灰字说明为空）；窗口最小宽度 < 900（不再 1180）。
  ② 手工双面 + 攒批开，还没攒到东西 + 单任务
     ⇒ 标题写「🖐 攒批列表 · 🖨 打印列表」；左右两栏仍然隐藏（空表格换成一行灰字）；
       灰字说明讲"怎么攒"；按钮隐藏。
  ③ 手工双面 + 攒批开 + 多任务（有纸叠 + 有排队 + 有队列）
     ⇒ 三块都在、5 个按钮都在、提示句讲左右栏、窗口加高到 1180×700，
       且「打印列表」初始占竖向分割约 40%（…… 之后可用鼠标拖，见 ⑦）。
  ④ 手工双面 + 攒批关 + 单任务/多任务
     ⇒ 标题只写「🖨 打印列表」；攒批两块隐藏；灰字说明**不出现**（队列视图里不塞攒批宣传）。
  ⑤ 兼容：老调用 `render(stack)` 不传新形参 ⇒ 有纸叠照常显示左右两栏、标题「🖐 攒批列表」；
     老验收脚本依赖的树内占位文案（「还没有攒进…」「没有在等的文件」）仍在树里。
  ⑥ 退化态：什么都没有 ⇒ 左右两栏隐藏但给一行灰字解释（不是一片空白）。

    python tests/verify_stack_window_shape.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_stack_shape_")
os.environ["APPDATA"] = _SANDBOX                    # 纸叠清单/配置 → 沙箱
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import duplex_stack as ds       # noqa: E402
import gui                      # noqa: E402
from printer_config import PrintJob  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def make_job(name, pages=4, duplex="on"):
    return PrintJob(file_path=os.path.join(_SANDBOX, name), copies=1, duplex=duplex,
                    page_range="", page_count=pages, orientation="portrait",
                    engine="pdf", display_name=name)


def stack_two():
    """两个已攒单（页序/张数走真实 duplex_stack 账本结构）。"""
    stack = ds.DuplexStack(capacity=90, printer_name="HP LaserJet 1020")
    stack.new_batch("batch_shape")
    stack.first, stack.rotate = "odd", True
    e1 = ds.StackEntry(
        seq=1, title="单 1 · 2 个文件", tab_key="1", order_id=3,
        order_number="A-1", page_count=7, sheet_count=5, original_sheet_count=5,
        state=ds.STATE_DONE, rotate=True, first="odd", pdf="e1.pdf",
        created_at="2026-11-01 10:00:00",
        file_plans=[{"label": "a.pdf", "pages": [1, 2, 3, 4], "duplex": True,
                     "file_pages": [1, 2, 3, 4]},
                    {"label": "b.xlsx", "pages": [5, 6, 7], "duplex": False,
                     "file_pages": [1, 2, 3]}],
        taken_plans=[1])
    e2 = ds.StackEntry(
        seq=2, title="单 2 · 1 个文件", tab_key="2", order_id=4,
        order_number="A-2", page_count=6, sheet_count=3, original_sheet_count=3,
        state=ds.STATE_DONE, rotate=True, first="odd", pdf="e2.pdf",
        created_at="2026-11-01 10:05:00",
        file_plans=[{"label": "c.docx", "pages": [1, 2, 3, 4, 5, 6], "duplex": True,
                     "file_pages": [1, 2, 3, 4, 5, 6]}])
    stack.entries = [e1, e2]
    return stack


def empty_stack():
    return ds.DuplexStack(capacity=90, printer_name="HP LaserJet 1020")


def waits():
    return [{"reason": "等这一叠打完第 2 趟才放行", "tab_key": "1",
             "jobs": [make_job("扫描件.pdf", pages=8, duplex="on")]},
            {"reason": "容量不足", "tab_key": "1",
             "jobs": [make_job("教辅.docx", pages=12, duplex="on")]}]


ROW_ACTIVE = {"state": "进行中", "source": "云端自动", "content": "教辅.docx",
              "sheets": "6", "eta": "00:41 · 21:33", "active": True}
ROW_WAIT1 = {"state": "等待中", "source": "本机点击", "content": "扫描件.pdf",
             "sheets": "4", "eta": "01:12 · 21:44", "active": False}
ROW_WAIT2 = {"state": "等待中（预约）", "source": "预约", "content": "预约单 #12",
             "sheets": "—", "eta": "预约 21:50", "active": False}


def vis(w) -> bool:
    return w is not None and not w.isHidden()


def build(stack, waits_list, local_rows, mode: bool, ok: bool):
    dlg = gui.StackQueueDialog()
    dlg.render(stack, busy=False, wait_requests=waits_list,
               stack_mode=mode, stack_applicable=ok)
    dlg.set_local_queue(local_rows)
    return dlg


def scene(name, stack, waits_list, local_rows, mode: bool, ok: bool):
    dlg = build(stack, waits_list, local_rows, mode, ok)
    print(f"\n=== {name} ===")
    print(f"    标题「{dlg.windowTitle()}」 / 最小 {dlg.minimumWidth()}x{dlg.minimumHeight()}")
    return dlg


# ============================================================
# ① 自动双面（攒批不适用）：单任务 / 多任务
# ============================================================
print("\n" + "=" * 60)
print("① 自动双面机（攒批不适用）")
print("=" * 60)

for label, rows in (("单任务", [ROW_ACTIVE]),
                    ("多任务", [ROW_ACTIVE, ROW_WAIT1, ROW_WAIT2])):
    dlg = scene(f"① 自动双面 · {label}", empty_stack(), [], rows, mode=False, ok=False)
    check(f"① [{label}] 标题只写「🖨 打印列表」", dlg.windowTitle() == "🖨 打印列表",
          dlg.windowTitle())
    check(f"① [{label}] 左栏「已攒」隐藏", not vis(dlg._left_group))
    check(f"① [{label}] 右栏「等待打印」隐藏", not vis(dlg._right_group))
    check(f"① [{label}] 打印列表可见", vis(dlg._local_group) and dlg.local_queue_visible())
    check(f"① [{label}] 三个动纸按钮隐藏",
          not vis(dlg._btn_take) and not vis(dlg._btn_start) and not vis(dlg._btn_abandon))
    check(f"① [{label}] 没有攒批宣传（灰字说明为空）", not dlg._empty_line.text(),
          dlg._empty_line.text())
    check(f"① [{label}] 汇总句讲队列", "打印列表：进行中" in dlg._summary.text(),
          dlg._summary.text())
    check(f"① [{label}] 提示句讲队列", "排队" in dlg._hint.text(), dlg._hint.text())
    check(f"① [{label}] 不再硬撑 1180 宽", dlg.minimumWidth() <= 900,
          f"{dlg.minimumWidth()}px")

# ============================================================
# ② 手工双面 + 攒批开，还没攒到东西：单任务
# ============================================================
print("\n" + "=" * 60)
print("② 手工双面 + 攒批开（还没攒到纸）")
print("=" * 60)

dlg = scene("② 攒批开 · 单任务", empty_stack(), [], [ROW_ACTIVE], mode=True, ok=True)
check("② 标题两个都写", dlg.windowTitle() == "🖐 攒批列表 · 🖨 打印列表", dlg.windowTitle())
check("② 左栏隐藏（空表格不摆）", not vis(dlg._left_group))
check("② 右栏隐藏", not vis(dlg._right_group))
check("② 用一行灰字交代「攒批已开、还没攒到」",
      "攒批模式已开启" in dlg._empty_line.text(), dlg._empty_line.text())
check("② 灰字在该场景可见", vis(dlg._empty_line))
check("② 打印列表仍可见", vis(dlg._local_group))
check("② 动纸按钮隐藏", not vis(dlg._btn_start))
check("② 汇总句保留攒批口径", "还没有攒进文件" in dlg._summary.text(), dlg._summary.text())

# ============================================================
# ③ 手工双面 + 攒批开 + 真的攒到东西（多任务，三块并存）
# ============================================================
print("\n" + "=" * 60)
print("③ 手工双面 + 攒批开（真有纸叠 + 排队 + 队列）")
print("=" * 60)

dlg = scene("③ 攒批开 · 多任务", stack_two(), waits(),
            [ROW_ACTIVE, ROW_WAIT1, ROW_WAIT2], mode=True, ok=True)
check("③ 标题两个都写", dlg.windowTitle() == "🖐 攒批列表 · 🖨 打印列表", dlg.windowTitle())
check("③ 左栏可见（2 单）", vis(dlg._left_group) and "2 单" in dlg._left_group.title(),
      dlg._left_group.title())
check("③ 右栏可见（2 个请求）", vis(dlg._right_group) and "2 个请求" in dlg._right_group.title(),
      dlg._right_group.title())
check("③ 打印列表可见", vis(dlg._local_group))
check("③ 5 个按钮全在",
      all(vis(b) for b in (dlg._btn_take, dlg._btn_start, dlg._btn_abandon,
                           dlg._btn_refresh, dlg._btn_close)))
check("③ 没有任何灰字说明", not dlg._empty_line.text())
check("③ 提示句讲左右栏", "左栏" in dlg._hint.text() and "右栏" in dlg._hint.text(),
      dlg._hint.text())
check("③ 同时在场时加高（520 → 700，屏幕矮时按可用高度封顶）",
      dlg.minimumWidth() == 1180 and 680 <= dlg.minimumHeight() <= 700,
      f"{dlg.minimumWidth()}x{dlg.minimumHeight()}")
check("③ 左栏仍按单分组（第一条是 #1 单）",
      dlg._tree_left.topLevelItemCount() == 2, str(dlg._tree_left.topLevelItemCount()))

# ============================================================
# ④ 手工双面 + 攒批关：单任务 / 多任务
# ============================================================
print("\n" + "=" * 60)
print("④ 手工双面机 + 攒批模式关")
print("=" * 60)

for label, rows in (("单任务", [ROW_ACTIVE]),
                    ("多任务", [ROW_ACTIVE, ROW_WAIT1, ROW_WAIT2])):
    dlg = scene(f"④ 攒批关 · {label}", empty_stack(), [], rows, mode=False, ok=True)
    check(f"④ [{label}] 标题只写「🖨 打印列表」", dlg.windowTitle() == "🖨 打印列表",
          dlg.windowTitle())
    check(f"④ [{label}] 攒批两块隐藏",
          not vis(dlg._left_group) and not vis(dlg._right_group))
    check(f"④ [{label}] 打印列表可见", vis(dlg._local_group))
    check(f"④ [{label}] 队列视图里不塞攒批宣传", not dlg._empty_line.text(),
          dlg._empty_line.text())
    check(f"④ [{label}] 动纸按钮隐藏", not vis(dlg._btn_start))

# ============================================================
# ⑤ 兼容老调用 & 老验收依赖的占位文案
# ============================================================
print("\n" + "=" * 60)
print("⑤ 兼容")
print("=" * 60)

dlg = gui.StackQueueDialog()
dlg.render(stack_two(), busy=False, wait_requests=[])
check("⑤ 老调用 render(stack) 仍显示左栏", vis(dlg._left_group))
check("⑤ 老调用标题仍是「🖐 攒批列表」", dlg.windowTitle() == "🖐 攒批列表",
      dlg.windowTitle())
check("⑤ 老调用显示动纸按钮", vis(dlg._btn_start))

dlg2 = gui.StackQueueDialog()
dlg2.render(empty_stack(), busy=False, wait_requests=[])
_left_txt = dlg2._tree_left.topLevelItem(0).text(0) if dlg2._tree_left.topLevelItemCount() else ""
_right_txt = (dlg2._tree_right.topLevelItem(0).text(0)
              if dlg2._tree_right.topLevelItemCount() else "")
check("⑤ 左栏占位文案仍在树里（老验收读它）", "还没有攒进" in _left_txt, _left_txt)
check("⑤ 右栏占位文案仍在树里", "没有在等的文件" in _right_txt, _right_txt)

# ============================================================
# ⑥ 退化态 + UI 硬规矩
# ============================================================
print("\n" + "=" * 60)
print("⑥ 退化态与文案规矩")
print("=" * 60)

dlg = build(empty_stack(), [], [], mode=False, ok=False)
check("⑥ 什么都没有时给一行解释（不是一片空白）",
      vis(dlg._empty_line) and bool(dlg._empty_line.text()), dlg._empty_line.text())
check("⑥ 自动双面机的解释不再教「怎么攒批」",
      "自动双面" in dlg._empty_line.text(), dlg._empty_line.text())

for label, dlg in (("自动双面", build(empty_stack(), [], [ROW_ACTIVE], False, False)),
                   ("攒批开空", build(empty_stack(), [], [ROW_ACTIVE], True, True)),
                   ("三块并存", build(stack_two(), waits(), [ROW_ACTIVE], True, True))):
    texts = [dlg.windowTitle(), dlg._summary.text(), dlg._hint.text(),
             dlg._empty_line.text(), dlg._hint.toolTip()]
    check(f"⑥ [{label}] 文案里没有 markdown 星号",
          all("**" not in t for t in texts))
    check(f"⑥ [{label}] 提示全文在 tooltip 里（不占窗口高度）",
          len(dlg._hint.toolTip()) > len(dlg._hint.text()),
          f"tooltip {len(dlg._hint.toolTip())} 字 > 行内 {len(dlg._hint.text())} 字")

# ============================================================
# ⑦ 攒批两块 ↔ 打印列表：高度可以用鼠标拖（2026-11-01 用户要求）
# ============================================================
from PySide6.QtCore import Qt  # noqa: E402

print("\n" + "=" * 60)
print("⑦ 「打印列表」的高度可以用鼠标拖")
print("=" * 60)

dlg = build(stack_two(), waits(), [ROW_ACTIVE, ROW_WAIT1, ROW_WAIT2], True, True)
dlg.show()
_app.processEvents()

check("⑦ 有竖向分隔条", hasattr(dlg, "_vsplit"))
check("⑦ 方向是竖的", dlg._vsplit.orientation() == Qt.Vertical)
check("⑦ 上面是攒批两块、下面是打印列表",
      dlg._vsplit.widget(0) is dlg._split and dlg._vsplit.widget(1) is dlg._local_group)
check("⑦ 打印列表已经归分隔条管（不在主布局里，才拖得动）",
      dlg._local_group.parent() is dlg._vsplit)
check("⑦ 抓手够宽，好点中", dlg._vsplit.handleWidth() >= 5,
      f"{dlg._vsplit.handleWidth()}px")
check("⑦ 拖到极端也不会消失（还能抓回来）", dlg._vsplit.childrenCollapsible() is False)

up0, low0 = dlg._vsplit.sizes()
check("⑦ 三块并存时上下都有高度", up0 > 0 and low0 > 0, f"上 {up0} / 下 {low0}")
_total0 = max(1, up0 + low0)
_ratio0 = low0 / _total0
check("⑦ 初始就给「打印列表」约 40%（不是塞在底部的一小条）",
      0.33 <= _ratio0 <= 0.47, f"下 {low0}/{_total0} = {_ratio0:.0%}")
check("⑦ 三块并存时窗口足够高（>=680）", dlg.minimumHeight() >= 680,
      f"{dlg.minimumHeight()}px")

dlg._vsplit.setSizes([200, 460])
_app.processEvents()
up1, low1 = dlg._vsplit.sizes()
check("⑦ 拖动真的改变了高度", (up1, low1) != (up0, low0), f"上 {up1} / 下 {low1}")
check("⑦ 能把打印列表拖大", low1 > low0, f"下 {low0} → {low1}")

# 2 秒心跳就是这么刷新的：不许把操作员拖出来的比例抢回去
dlg.render(stack_two(), busy=False, wait_requests=waits(),
           stack_mode=True, stack_applicable=True)
dlg.set_local_queue([ROW_ACTIVE, ROW_WAIT1, ROW_WAIT2])
_app.processEvents()
check("⑦ 刷新后高度不变（不抢回操作员拖出来的比例）",
      dlg._vsplit.sizes() == [up1, low1], str(dlg._vsplit.sizes()))

dlg._vsplit.setSizes([0, 1000])
_app.processEvents()
up2, _low2 = dlg._vsplit.sizes()
check("⑦ 往上拖到底：上面仍留高度", up2 > 0, f"上 {up2}px")

dlg._vsplit.setSizes([1000, 0])
_app.processEvents()
_up3, low3 = dlg._vsplit.sizes()
check("⑦ 往下拖到底：打印列表仍留高度", low3 > 0, f"下 {low3}px")

dlg2 = build(stack_two(), waits(), [], True, True)
dlg2.show()
_app.processEvents()
check("⑦ 打印列表空时不留一条没用的分隔条",
      dlg2._vsplit.sizes()[1] == 0, str(dlg2._vsplit.sizes()))


def finish(code: int):
    """收尾：先把 offscreen 控件交给 Qt 销毁，再退出。

    直接 `sys.exit()` 会让 6 个对话框活到解释器收尾阶段 —— 那时 Qt 库已经卸载，
    PySide6 会在 C 层 abort（进程退出码 127，`-X dev` 下能看到
    "Fatal Python error: Aborted ... QThreadStorage"），run_all 因此把全绿判成红。
    """
    app = QApplication.instance()
    if app is not None:
        for w in app.topLevelWidgets():
            w.deleteLater()
        app.processEvents()
    sys.exit(code)


print()
if ok_all:
    print("全部通过 ✅")
    finish(0)
print("有失败 ❌")
finish(1)
