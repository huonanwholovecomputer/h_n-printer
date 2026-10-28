# -*- coding: utf-8 -*-
"""验收脚本 · 「页数」列显示**有效打印页数**（GUI 集成，offscreen）

现场（2026-10-27，订单 HN20261027-0012）：2 页的 .doc 把页码范围改成 1、费用已按 1 张算到
¥0.30，但表格「页数」列仍写着 2 ⇒ 操作员判成「页码范围没生效 / 卡住了」。
真相：该列原先直接显示 job.page_count（整份页数），与「实际打几页」不是一回事。

现在口径与结算页 v7.2 一致：**列里写有效打印页数**，整份页数不同时括号注明
（2 页只打第 1 页 → 「1 (共 2 页)」），悬停给出「文件共 2 页 · 页码范围 1（实际打印 1 页）」。
折算只走 printer_config._count_pages_in_range（与计费、单页硬闸门、后端同一份算法）。

    python tests/verify_pages_column_effective.py     # exit 0 = 全绿
"""
import io
import os
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_pagescol_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

import converter as _converter  # noqa: E402
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}

import gui as _gui  # noqa: E402
_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

from gui import MainWindow  # noqa: E402
from printer_config import PrintJob, TabSettings, calc_cost, _count_pages_in_range  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


SRC = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()

print("\n① 静态接线（口径只有一处）")
check("「页数」格由 _pages_cell_text / _set_pages_cell 统一给出",
      "def _pages_cell_text(self, job)" in SRC and "def _set_pages_cell(self, row: int, job)" in SRC
      and "QTableWidgetItem(self._pages_cell_text(job))" in SRC)
check("折算复用 printer_config._count_pages_in_range（不另写一份页码范围解析）",
      "eff = int(_count_pages_in_range(getattr(job, \"page_range\", \"\") or \"\", total))" in SRC
      and "_pages_effective" in SRC)
check("没有把整份页数直接写进「页数」格的漏网写入",
      "COL_PAGES).setText(str(page_count))" not in SRC)

win = MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
win.activateWindow()
app.processEvents()


def seed(jobs):
    tab = TabSettings()
    tab.jobs = jobs
    win._config.tabs = {"1": tab}
    win._current_tab = "1"
    win._config.active_tab = "1"
    win._rebuild_table()
    win._refresh_tab_display()
    app.processEvents()


def pages_cell(row=0):
    item = win._table.item(row, win.COL_PAGES)
    return (item.text(), item.toolTip()) if item else ("", "")


def cost_cell(row=0):
    item = win._table.item(row, win.COL_COST)
    return item.text() if item else ""


def mk(pages, rng="", duplex="on"):
    return PrintJob(file_path=os.path.join(_SANDBOX, "x.pdf"), copies=1, duplex=duplex,
                    page_count=pages, orientation="portrait", engine="word",
                    display_name="x.pdf", page_range=rng)


print("\n② 单元格文案（真表格）")
seed([mk(2, "")])
txt, tip = pages_cell()
check("整份 2 页、没选范围 → 「2」", txt == "2", txt)
check("  悬停说明写文件共 2 页", "文件共 2 页" in tip, tip)

seed([mk(2, "1", duplex="off")])
txt, tip = pages_cell()
check("现场原样（2 页只打第 1 页）→ 「1 (共 2 页)」", txt == "1 (共 2 页)", txt)
check("  悬停写明实际打印 1 页", "文件共 2 页" in tip and "页码范围 1" in tip
      and "实际打印 1 页" in tip, tip)
check("  与费用列同口径（1 张 × 单价）", "1张" in cost_cell(), cost_cell())

seed([mk(3, "2", duplex="off")])
txt, _ = pages_cell()
check("3 页只打第 2 页 → 「1 (共 3 页)」", txt == "1 (共 3 页)", txt)

seed([mk(2, "1,2")])
check("选满 1,2 → 「2」（不画蛇添足）", pages_cell()[0] == "2", pages_cell()[0])

seed([mk(4, "1-2")])
check("4 页选 1-2 → 「2 (共 4 页)」", pages_cell()[0] == "2 (共 4 页)", pages_cell()[0])

seed([mk(0, "")])
check("页数未知 → 「?」", pages_cell()[0] == "?")
check("  未知时悬停说明会补上", "页数未知" in pages_cell()[1], pages_cell()[1])

print("\n③ 改页码范围后**当场**重画（用户真正踩的路径）")
job = mk(2, "", duplex="on")
seed([job])
win._table.selectRow(0)
app.processEvents()
win._on_table_selection()
app.processEvents()
check("先选中该行、面板范围为空", pages_cell()[0] == "2")
# 走真实控件：改输入框文本 → RangeListWidget.rangesChanged → _auto_apply_edit
win._edit_page_range._inputs[0].setText("1")
app.processEvents()
check("面板里填 1 → 表格「页数」立刻变成 1 (共 2 页)",
      pages_cell()[0] == "1 (共 2 页)", pages_cell()[0])
check("  同时「页码范围」列写着 1",
      win._table.item(0, win.COL_RANGE).text() == "1",
      win._table.item(0, win.COL_RANGE).text())
check("  单页硬闸门同步：单/双面列变「单面」",
      win._table.item(0, win.COL_DUPLEX).text() == "单面",
      win._table.item(0, win.COL_DUPLEX).text())
check("  费用也按 1 页算（不会出现 页数1/费用2 的自相矛盾）",
      "1张" in cost_cell(), cost_cell())
check("  job 真的被改写（不是只改显示）", job.page_range == "1", repr(job.page_range))

# 与「后端折算」同源：界面文案必须等于 _count_pages_in_range 的结论
eff = int(_count_pages_in_range(job.page_range, job.page_count))
check("  文案里的有效页数 == _count_pages_in_range 结果", eff == 1, str(eff))
cost, _formula = calc_cost(job.page_count, job.copies, "off",
                           win._config.simplex_price, win._config.duplex_price, job.page_range)
check("  费用 == calc_cost（同源）", f"¥{cost:.2f}" in cost_cell(), cost_cell())

win._edit_page_range._inputs[0].setText("")
app.processEvents()
check("把范围清空 → 回到「2」", pages_cell()[0] == "2", pages_cell()[0])

print("\n" + ("PASS 全部通过" if ok_all else "FAIL 有失败项"))
sys.exit(0 if ok_all else 1)
