# -*- coding: utf-8 -*-
"""验收脚本 · 多选（Ctrl/Shift/拖框）+「➕ 添加到新标签页」（GUI 集成，offscreen）

需求（2026-10「订单打印出错的强化策略」）：
1. 标签页已**锁定**（🔒 打印完成）时，文件右键菜单里有「➕ 添加到新标签页」：
   点击后把选中文件**复制**到新标签页并**直接跳转过去**等待处理；原标签页与已上报订单
   原样保留（冻结标签页是账目快照，只能读不能改）。
2. 标签页**未锁定**时菜单项照常显示，但**灰色不可用**。
3. 支持鼠标**拖框**（rubber band）框选多个文件；
4. 支持 **Ctrl / Shift + 左键**多选；多选后的批量操作（复制到新标签页 / 移除）对整片选区生效。

本脚本用真实 MainWindow 打真鼠标事件驱动 Qt 的选择状态机，再直接构造右键菜单
（`_build_table_context_menu` 不弹出、可断言）验收。

    python tests/verify_multi_select_gui.py     # exit 0 = 全绿
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

_SANDBOX = tempfile.mkdtemp(prefix="hn_multisel_")
os.environ["APPDATA"] = _SANDBOX                 # 用户数据目录 → 沙箱（别碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtCore import Qt, QPoint, QPointF, QEvent, QItemSelectionModel  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QAbstractItemView  # noqa: E402

app = QApplication.instance() or QApplication([])

import gui as _gui  # noqa: E402

# 引擎可用性探测（GUI 启动时 QTimer 会调）会真的 Dispatch Word/WPS COM：离屏 GUI 测试里既慢又有
# 副作用（实测会拉起 wps.exe 残留进程），而且本机 WPS 的 RPC 端点异常时探测本身就会报
# RPC_S_SERVER_UNAVAILABLE。GUI 测试不关心真实可用性 → 统一桩成“三个引擎都可用”；
# §10 再单独换成别的返回值来验灰度逻辑。
import converter as _converter  # noqa: E402
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}

# offscreen 下没人能回答模态框 → 一律自动回「是/好」
_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

from gui import MainWindow  # noqa: E402
from printer_config import PrintJob, TabSettings  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ═══════════════ 准备：临时“文档”（真 PDF，供页数补数用）═══════════════

import fitz  # noqa: E402  （PyMuPDF，gui 里读 PDF 页数用的就是它）


def make_pdf(name: str, pages: int = 1) -> str:
    p = os.path.join(_SANDBOX, name)
    doc = fitz.open()
    for _ in range(max(1, pages)):
        doc.new_page()
    doc.save(p)
    doc.close()
    return p


print("… 正在创建主窗口", flush=True)
win = MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
app.processEvents()

PA = make_pdf("A.pdf", 3)
PB = make_pdf("B.pdf", 2)
PC = make_pdf("C.pdf", 1)
PD = make_pdf("D.pdf", 4)
VIEWPORT = win._table.viewport()


def seed(jobs, frozen=False):
    """把当前标签页重建成只有这些 jobs（可选锁定），返回 key。"""
    tab = TabSettings()
    tab.jobs = jobs
    tab.frozen = frozen
    win._config.tabs = {"1": tab}
    win._current_tab = "1"
    win._config.active_tab = "1"
    win._rebuild_table()
    win._refresh_tab_display()
    app.processEvents()
    return "1"


def simple_jobs():
    return [PrintJob(file_path=p, copies=1, duplex="on", page_count=i + 1,
                     orientation="portrait", engine="word",
                     display_name=os.path.basename(p))
            for i, p in enumerate((PA, PB, PC, PD))]


def sel_rows():
    return sorted({i.row() for i in win._table.selectedIndexes()})


def select_rows(rows):
    """程序化选中若干整行（走 selectionModel，等价 Ctrl 点选，不受修饰键状态影响）。"""
    win._table.clearSelection()
    sm = win._table.selectionModel()
    for r in rows:
        sm.select(win._table.model().index(r, 0),
                  QItemSelectionModel.Select | QItemSelectionModel.Rows)
    app.processEvents()


def cell_center(row, col=0):
    return win._table.visualRect(win._table.model().index(row, col)).center()


def send(kind, pos, buttons=Qt.NoButton, button=Qt.NoButton):
    vp = VIEWPORT
    ev = QMouseEvent(kind, QPointF(pos), QPointF(vp.mapToGlobal(pos)), button, buttons,
                     Qt.NoModifier)
    app.sendEvent(vp, ev)
    app.processEvents()


def rubber_band(row_from, row_to, col_from=0, col_to=0):
    """按下 → 分步移动（按住左键）→ 松开，模拟真实拖框。返回拖动途中的选中行。"""
    win._table.clearSelection()
    app.processEvents()
    a, b = cell_center(row_from, col_from), cell_center(row_to, col_to)
    send(QEvent.MouseButtonPress, a, Qt.LeftButton, Qt.LeftButton)
    for i in range(1, 6):
        send(QEvent.MouseMove, QPoint(a.x() + (b.x() - a.x()) * i // 5,
                                      a.y() + (b.y() - a.y()) * i // 5), Qt.LeftButton)
    mid = sel_rows()
    send(QEvent.MouseButtonRelease, b, Qt.NoButton, Qt.LeftButton)
    return mid


def click(row, mods=Qt.NoModifier):
    QTest.mouseClick(VIEWPORT, Qt.LeftButton, mods, cell_center(row))


def release_mods():
    """QTest 只传修饰键给鼠标事件、不会发对应按键抬起 ⇒ 手动复位，
    否则 `QApplication.keyboardModifiers()` 会一直"按着"（真实用户会松手）。"""
    QTest.keyRelease(VIEWPORT, Qt.Key_Control)
    QTest.keyRelease(VIEWPORT, Qt.Key_Shift)
    app.processEvents()


def ctx(pos):
    return win._build_table_context_menu(pos)


def act(menu, keyword):
    for a in menu.actions():
        if keyword in a.text():
            return a
    return None


# ═══════════════ 0. 静态源码口径 ═══════════════

print("=== 0. 静态接线 ===")
src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
check("主表格选择模式 = ExtendedSelection（多选入口）",
      bool(re.search(r"self\._table\.setSelectionMode\(QAbstractItemView\.ExtendedSelection\)", src)))
check("主表格已显式关闭内部拖动（否则拖框会被拖动抢掉）",
      bool(re.search(r"self\._table\.setDragEnabled\(False\)", src)))
check("右键落点收敛走 _select_only_row（不受按住的 Ctrl/Shift 影响）",
      "self._select_only_row(clicked_row)" in src)
check("存在「添加到新标签页」菜单项与复制实现",
      "添加到新标签页" in src and "def _add_rows_to_new_tab" in src)

# ═══════════════ 1. 表格配置 ═══════════════

print("=== 1. 表格配置 ===")
check("selectionMode == ExtendedSelection",
      win._table.selectionMode() == QAbstractItemView.ExtendedSelection)
check("selectionBehavior == SelectRows",
      win._table.selectionBehavior() == QAbstractItemView.SelectRows)
check("dragEnabled == False（拖框不被内部拖动抢走）", win._table.dragEnabled() is False)

# ═══════════════ 2. 拖框（rubber band）多选 ═══════════════

print("=== 2. 鼠标拖框多选 ===")
seed(simple_jobs())
mid = rubber_band(0, 3)
check("拖框过程（未松开）即已实时选中 0..3", mid == [0, 1, 2, 3], f"选中={mid}")
check("松开后保持选中 0..3", sel_rows() == [0, 1, 2, 3], f"选中={sel_rows()}")
rubber_band(3, 1)
check("反向拖框（3 → 1）选中 1..3", sel_rows() == [1, 2, 3], f"选中={sel_rows()}")
rubber_band(1, 2, col_from=2, col_to=3)
check("从行中间起手拖框同样有效（选中 1..2）", sel_rows() == [1, 2], f"选中={sel_rows()}")

# ═══════════════ 3. Ctrl / Shift + 左键多选 ═══════════════

print("=== 3. Ctrl / Shift + 左键多选 ===")
seed(simple_jobs())
click(0)
check("普通单击 = 单选第 0 行", sel_rows() == [0], f"选中={sel_rows()}")
click(2, Qt.ControlModifier)
click(3, Qt.ControlModifier)
check("Ctrl+左键逐个追加（0,2,3）", sel_rows() == [0, 2, 3], f"选中={sel_rows()}")
click(2, Qt.ControlModifier)
check("Ctrl+左键再点同一行 = 取消该行（0,3）", sel_rows() == [0, 3], f"选中={sel_rows()}")
release_mods()
check("松开 Ctrl 后修饰键状态复位（后续手势不受影响）",
      QApplication.keyboardModifiers() == Qt.NoModifier,
      str(QApplication.keyboardModifiers()))
click(0)
click(2, Qt.ShiftModifier)
check("Shift+左键 = 连续区间（0..2）", sel_rows() == [0, 1, 2], f"选中={sel_rows()}")
click(3, Qt.ShiftModifier)
check("Shift+左键继续扩展（0..3）", sel_rows() == [0, 1, 2, 3], f"选中={sel_rows()}")
release_mods()

# ═══════════════ 4. 右键菜单：显示与灰色口径 ═══════════════

print("=== 4. 右键菜单（未锁定 = 显示但灰色 / 已锁定 = 可用）===")
seed(simple_jobs(), frozen=False)
select_rows([0, 2])
m = ctx(cell_center(0))
a_move = act(m, "添加到新标签页")
a_remove = act(m, "移除选中")
check("未锁定标签页：菜单里**有**「添加到新标签页」", a_move is not None)
check("未锁定标签页：「添加到新标签页」为灰色不可用",
      a_move is not None and not a_move.isEnabled())
check("未锁定标签页：「移除选中」可用", a_remove is not None and a_remove.isEnabled())
check("多选时菜单项带文件数", a_remove is not None and "2 个文件" in a_remove.text(),
      a_remove.text() if a_remove else "")
check("右键落在已选中行 → 整片选区保留（0,2）", sel_rows() == [0, 2], f"选中={sel_rows()}")
m = ctx(cell_center(1))
check("右键落在未选中行 → 选区收敛为该行（1）", sel_rows() == [1], f"选中={sel_rows()}")

# 4b. 用户多选后**没松开 Shift** 就右键（Qt 的 selectRow 此时会把旧选区并进来）
print("=== 4b. 右键时仍按着 Shift（修饰键未松开）===")
seed(simple_jobs())
click(0, Qt.ShiftModifier)
click(2, Qt.ShiftModifier)
check("前置：Shift 状态确实处于「按住」", QApplication.keyboardModifiers() == Qt.ShiftModifier,
      str(QApplication.keyboardModifiers()))
select_rows([0, 2])
m = ctx(cell_center(1))
check("按着 Shift 右键未选中行 → 仍然只收敛到该行（1）", sel_rows() == [1],
      f"选中={sel_rows()}")
release_mods()

seed(simple_jobs(), frozen=True)
select_rows([1, 3])
m = ctx(cell_center(1))
a_move = act(m, "添加到新标签页")
a_remove = act(m, "移除选中")
check("锁定标签页：「添加到新标签页」可用", a_move is not None and a_move.isEnabled())
check("锁定标签页：「移除选中」禁用（订单已锁定）",
      a_remove is not None and not a_remove.isEnabled())
check("锁定标签页：右键已选中行保住多选（1,3）", sel_rows() == [1, 3], f"选中={sel_rows()}")
check("锁定标签页：菜单里仍有「打开文件位置」", act(m, "打开文件位置") is not None)

# ═══════════════ 5. 点击「添加到新标签页」：复制 + 跳转 ═══════════════

print("=== 5. 「添加到新标签页」行为 ===")
jobs = simple_jobs()
jobs[0].copies = 3
jobs[0].duplex = "off"
jobs[0].duplex_mode = "short-edge"
jobs[0].page_range = "1-2"
jobs[0].dpi = 300
jobs[0].image_orientation = "landscape"
jobs[0].display_name = "云端座位表.pdf"
jobs[0].order_number = "HN20261020-0007"      # 旧单号（绝不能带过去）
jobs[0].task_id = 41
jobs[0].order_id = 7
jobs[0].sent = True
jobs[0].remark = "老订单备注"
jobs[1].copies = 2
src_key = seed(jobs, frozen=True)
win._config.tabs[src_key].owner_name = "张三"
before_tab_count = len(win._config.tabs)

select_rows([0, 1])
act(ctx(cell_center(0)), "添加到新标签页").trigger()
app.processEvents()

new_key = win._current_tab
check("触发了新标签页并**直接跳转过去**", new_key != src_key, f"{src_key} → {new_key}")
check("新标签页是新增的（标签总数 +1）", len(win._config.tabs) == before_tab_count + 1)
new_tab = win._config.tabs.get(new_key)
check("新标签页取到 2 个文件（只复制选中的行）",
      new_tab is not None and len(new_tab.jobs) == 2,
      f"{len(new_tab.jobs) if new_tab else 'None'}")
check("新标签页未锁定（可直接编辑/打印）", new_tab is not None and not new_tab.frozen)
if new_tab and len(new_tab.jobs) == 2:
    j0, j1 = new_tab.jobs
    check("参数照抄：份数/双面/翻转/范围/DPI/图片方向/页数",
          (j0.copies, j0.duplex, j0.duplex_mode, j0.page_range, j0.dpi,
           j0.image_orientation, j0.page_count)
          == (3, "off", "short-edge", "1-2", 300, "landscape", 1),
          f"{(j0.copies, j0.duplex, j0.duplex_mode, j0.page_range, j0.dpi, j0.image_orientation, j0.page_count)}")
    check("显示名沿用（云端原始文件名）", j0.display_name == "云端座位表.pdf", j0.display_name)
    check("订单身份清零：单号/云端 ID/完成标记/备注",
          (j0.order_number, j0.task_id, j0.order_id, j0.sent, j0.remark)
          == ("", 0, 0, False, ""),
          f"{(j0.order_number, j0.task_id, j0.order_id, j0.sent, j0.remark)!r}")
    check("第二个文件也复制过来了", j1.file_path == PB and j1.copies == 2,
          f"{j1.file_path} x{j1.copies}")
    check("文件不重复拷贝：新标签页与旧标签页共用同一份文件路径",
          j0.file_path == PA and os.path.isfile(j0.file_path))
    check("新标签页在表格里显示 2 行", win._table.rowCount() == 2, str(win._table.rowCount()))

old_tab = win._config.tabs.get(src_key)
check("原标签页原样保留：仍是 4 个文件 + 已锁定", old_tab is not None
      and len(old_tab.jobs) == 4 and old_tab.frozen)
check("原标签页的订单号/云端 ID/完成标记没被动过",
      old_tab.jobs[0].order_number == "HN20261020-0007"
      and old_tab.jobs[0].task_id == 41 and old_tab.jobs[0].sent)
check("原标签页归属人不受影响", old_tab.owner_name == "张三", old_tab.owner_name)
check("两个标签页各自独立（不是同一个 job 对象）",
      old_tab.jobs[0] is not new_tab.jobs[0])
new_tab.jobs[0].copies = 9
check("改新标签页的参数不会污染原订单", old_tab.jobs[0].copies == 3,
      str(old_tab.jobs[0].copies))
select_rows([0])
check("未锁定的新标签页里该项为灰色（不能再复制）",
      not act(ctx(cell_center(0)), "添加到新标签页").isEnabled())

# 页数未知的 PDF（云端 Word 的常见情况：后端返回 0）→ 复制时补出真实页数
jobs = simple_jobs()
jobs[0].page_count = 0
jobs[0].orientation = ""
seed(jobs, frozen=True)
new_key = win._add_rows_to_new_tab([0])
app.processEvents()
check("页数未知的 PDF：复制时补出真实页数（3 页）",
      new_key is not None and win._config.tabs[new_key].jobs[0].page_count == 3,
      str(win._config.tabs[new_key].jobs[0].page_count if new_key else None))

# ═══════════════ 6. 文件丢失的容错 ═══════════════

print("=== 6. 文件丢失容错 ===")
jobs = simple_jobs()
jobs[2].file_path = os.path.join(_SANDBOX, "不存在-副本.pdf")   # 副本没了
jobs[2].source_path = os.path.join(_SANDBOX, "不存在-原件.pdf")  # 原件也没了
seed(jobs, frozen=True)
new_key = win._add_rows_to_new_tab([1, 2, 3])
app.processEvents()
check("部分文件丢失：只复制还存在的文件（2 个）",
      new_key is not None and len(win._config.tabs[new_key].jobs) == 2,
      f"{len(win._config.tabs[new_key].jobs) if new_key else 'None'}")
seed(simple_jobs(), frozen=True)
win._config.tabs["1"].jobs[0].file_path = os.path.join(_SANDBOX, "没了.pdf")
win._config.tabs["1"].jobs[0].source_path = ""
before = len(win._config.tabs)
ret = win._add_rows_to_new_tab([0])
check("全部文件丢失：不创建空标签页", ret is None and len(win._config.tabs) == before,
      f"ret={ret} tabs={len(win._config.tabs)}")

seed(simple_jobs(), frozen=True)
win._config.tabs["1"].jobs[0].file_path = os.path.join(_SANDBOX, "暂存副本丢了.pdf")
win._config.tabs["1"].jobs[0].source_path = PA
new_key = win._add_rows_to_new_tab([0])
app.processEvents()
check("副本丢失但原件在 → 重建副本后再复制",
      new_key is not None and os.path.isfile(win._config.tabs[new_key].jobs[0].file_path)
      and win._config.tabs[new_key].jobs[0].file_path != PA)

# ═══════════════ 7. 多选下的参数编辑（整片选区一起应用）═══════════════

print("=== 7. 多选 + 参数编辑面板 ===")
jobs = simple_jobs()
for i, j in enumerate(jobs):          # 统一 5 页 + 各自不同份数（便于验证「仅选中不改写」）
    j.page_count = 5
    j.copies = i + 1
seed(jobs)
select_rows([0, 1, 2])
check("面板标题显示选中文件数", win._edit_group.title() == "编辑选中任务（3 个文件）",
      win._edit_group.title())
# 面板同步（把选区第一行的值填进控件）绝不能顺着多选把第一行的值"铺"给其他行
copies_before = [win._config.tabs["1"].jobs[i].copies for i in range(4)]
check("仅选中不改写任何行的参数", copies_before == [1, 2, 3, 4], str(copies_before))
win._edit_copies._increase()          # 模拟真实点「+」（setValue 不发信号）
app.processEvents()
copies = [win._config.tabs["1"].jobs[i].copies for i in range(4)]
check("份数修改应用到**全部选中行**（0,1,2 变 2；未选的 3 不变）", copies == [2, 2, 2, 4],
      str(copies))
# 现场报的 bug：数据改了、费用列也按 ×2 重算了，但「份数」那一格还停在 1
cells = [win._table.item(i, _gui.MainWindow.COL_COPIES).text() for i in range(4)]
check("份数列**立刻同步**（表格 0,1,2 显示 2，未选的 3 仍显示 1）",
      cells == ["2", "2", "2", "4"], str(cells))
check("选区在多选批量改参数后保持不丢（否则连点第二下 + 就没了）",
      sel_rows() == [0, 1, 2], f"选中={sel_rows()}")
win._edit_duplex.setCurrentIndex(1)   # 单面（选区都是多页文件 → 双面可用）
app.processEvents()
duplex = [win._config.tabs["1"].jobs[i].duplex for i in range(4)]
check("双面修改同样应用到全部选中行", duplex == ["off", "off", "off", "on"], str(duplex))
dual_cells = [win._table.item(i, _gui.MainWindow.COL_DUPLEX).text() for i in range(4)]
check("单/双面列同步为「单面」", dual_cells == ["单面", "单面", "单面", "双面(长边)"],
      str(dual_cells))
check("合计随批量修改同步刷新（不是旧值）", "¥" in win._total_label.text(),
      win._total_label.text())
select_rows([1])
app.processEvents()
check("单选时标题回到「编辑选中任务」", win._edit_group.title() == "编辑选中任务",
      win._edit_group.title())
check("单选时页码范围恢复可用（多选才禁用）", win._edit_page_range.isEnabled())
win._edit_page_range._inputs[0].setText("1-2")   # 单选下真的在范围框里打字
app.processEvents()
check("单选时页码范围照常生效", win._config.tabs["1"].jobs[1].page_range == "1-2",
      win._config.tabs["1"].jobs[1].page_range)
check("页码范围列同步为「1-2」",
      win._table.item(1, _gui.MainWindow.COL_RANGE).text() == "1-2",
      win._table.item(1, _gui.MainWindow.COL_RANGE).text())

# ═══════════════ 8. 多选「移除选中」（批量操作）═══════════════

print("=== 8. 多选「移除选中」（批量操作）===")
seed(simple_jobs())
select_rows([0, 2])
act(ctx(cell_center(0)), "移除选中").trigger()
app.processEvents()
left = [j.display_name for j in win._config.tabs["1"].jobs]
check("移除选中对整片选区生效（0、2 被移除，剩 B、D）", left == ["B.pdf", "D.pdf"], str(left))
check("表格同步为 2 行", win._table.rowCount() == 2, str(win._table.rowCount()))

# ═══════════════ 9. 面板可用性按「整片选区」判定 ═══════════════
# 现场报的两个错位：① 多选时「页码范围」还亮着；② 选区里有单页文件时「单/双面」「双面模式」还亮着。
print("=== 9. 多选 / 含单页文件时的控件可用性 ===")
PNG = os.path.join(_SANDBOX, "图片测试.png")
with open(PNG, "wb") as f:
    f.write(b"\x89PNG\r\n\x1a\n")

jobs = simple_jobs()
for j in jobs:
    j.page_count = 3            # 除特别说明外都当多页文件（simple_jobs 默认是 1..4 页）
jobs[3].page_count = 1          # 单页文件（双面物理上即单面）
jobs[3].display_name = "单页文档测试.pdf"
seed(jobs)

select_rows([0])
check("单选多页 PDF：页码范围可用", win._edit_page_range.isEnabled())
check("单选多页 PDF：双面/双面模式可用",
      win._edit_duplex.isEnabled() and win._edit_duplex_mode.isEnabled())

select_rows([0, 1])
check("多选（都是多页文件）：页码范围**禁用**", not win._edit_page_range.isEnabled())
check("多选：页码范围标签也灰着", not win._label_range.isEnabled())
check("多选（都是多页文件）：双面仍可用",
      win._edit_duplex.isEnabled() and win._edit_duplex_mode.isEnabled())

select_rows([0, 3])
check("选区含单页文件：单/双面**禁用**", not win._edit_duplex.isEnabled())
check("选区含单页文件：双面模式**禁用**", not win._edit_duplex_mode.isEnabled())
check("选区含单页文件：两个标签也灰着",
      not win._label_duplex.isEnabled() and not win._label_duplex_mode.isEnabled())
check("选区含单页文件（且多选）：页码范围仍禁用", not win._edit_page_range.isEnabled())

select_rows([3])
check("单选那个单页文件：双面禁用、页码范围可用",
      (not win._edit_duplex.isEnabled()) and win._edit_page_range.isEnabled())

jobs = simple_jobs()
for j in jobs:
    j.page_count = 3
jobs[2].file_path = PNG         # 图片：单页、无页码/双面概念（页数未知也一样禁用）
jobs[2].display_name = "图片测试.png"
jobs[2].page_count = 0
seed(jobs)
select_rows([2])
check("选中图片：双面/双面模式禁用",
      (not win._edit_duplex.isEnabled()) and (not win._edit_duplex_mode.isEnabled()))
check("选中图片：页码范围禁用", not win._edit_page_range.isEnabled())

# 「范围只选 1 页」也算单页（_job_is_single_page 走 _count_pages_in_range），双面同样要禁用
jobs = simple_jobs()
for j in jobs:
    j.page_count = 5
jobs[1].page_range = "3"        # 5 页的文件只打第 3 页 → 实际 1 页
seed(jobs)
select_rows([1])
check("单选「范围只选 1 页」的文件：双面禁用",
      (not win._edit_duplex.isEnabled()) and (not win._edit_duplex_mode.isEnabled()))
select_rows([0, 1])
check("多选且其中有「范围只选 1 页」的文件：双面禁用（其余行双面不受影响）",
      not win._edit_duplex.isEnabled())

# 灰着的控件**绝不能**被写回：面板此时被迫显示「单面」，照写会把多页文件静默改成单面
jobs = simple_jobs()
for j in jobs:
    j.page_count = 3
jobs[0].duplex = "on"
jobs[3].page_count = 1
seed(jobs)
select_rows([0, 3])
check("前置：面板被迫显示「单面」且双面控件灰着",
      win._edit_duplex.currentIndex() == 1 and not win._edit_duplex.isEnabled(),
      f"index={win._edit_duplex.currentIndex()} enabled={win._edit_duplex.isEnabled()}")
win._edit_copies._increase()    # 改份数（会触发 _auto_apply_edit）
app.processEvents()
check("改份数不会顺手把多页文件的双面改成单面（原值保留）",
      win._config.tabs["1"].jobs[0].duplex == "on",
      win._config.tabs["1"].jobs[0].duplex)
check("份数改动本身照常生效", win._config.tabs["1"].jobs[0].copies == 2,
      str(win._config.tabs["1"].jobs[0].copies))

# 页码范围的写回也要被挡住（控件灰着时即便有信号也不写）
jobs = simple_jobs()
jobs[0].page_range = "1-2"
jobs[1].page_range = ""
seed(jobs)
select_rows([0, 1])
win._edit_page_range._inputs[0].setText("1")   # 绕过禁用直接发信号（模拟任何异常触发）
app.processEvents()
kept = [win._config.tabs["1"].jobs[i].page_range for i in range(4)]
check("多选时页码范围的写回被挡住（各文件范围原样保留）", kept == ["1-2", "", "", ""], str(kept))

# ═══════════════ 10. 转换引擎在多选下的显示与写回 ═══════════════
# 现场提问：选区里同时有图片 / Word 文档 / WPS 文档时，引擎下拉显示什么？会不会影响所有文件？
print("=== 10. 转换引擎：多选下的显示与写回 ===")
PDOCX_W = os.path.join(_SANDBOX, "word编辑.docx")
PDOCX_S = os.path.join(_SANDBOX, "wps编辑.docx")
for _p in (PDOCX_W, PDOCX_S):
    with open(_p, "wb") as f:
        f.write(b"PK\x03\x04")          # 只按扩展名分类，内容不解析

jobs = [PrintJob(file_path=PNG, page_count=1, engine="libreoffice", display_name="图片测试.png"),
        PrintJob(file_path=PDOCX_S, page_count=3, engine="wps", display_name="wps编辑.docx"),
        PrintJob(file_path=PDOCX_W, page_count=2, engine="word", display_name="word编辑.docx"),
        PrintJob(file_path=PA, page_count=3, engine="libreoffice", display_name="A.pdf")]
seed(jobs)

select_rows([1])
check("单选 WPS 文档：引擎下拉可用且显示 WPS",
      win._edit_engine.isEnabled() and win._edit_engine.currentText() == "WPS",
      f"enabled={win._edit_engine.isEnabled()} text={win._edit_engine.currentText()}")

select_rows([0])
check("单选图片：引擎下拉是只读标签「图片渲染」且灰着",
      (not win._edit_engine.isEnabled()) and win._edit_engine.currentText() == "图片渲染",
      f"enabled={win._edit_engine.isEnabled()} text={win._edit_engine.currentText()}")

# ① 选区第一行（最低行号）是图片，选区里有 WPS 文档 → 面板应显示「那个 Word 文件的引擎」
select_rows([0, 1])
check("多选[图片, WPS文档]：引擎下拉显示 WPS（取自选区里的 Word 文件，而不是图片的只读标签）",
      win._edit_engine.currentText() == "WPS",
      f"text={win._edit_engine.currentText()}")
check("多选[图片, WPS文档]：引擎下拉可用（改动只作用于 .docx）", win._edit_engine.isEnabled())
win._edit_copies._increase()            # 改份数会触发 _auto_apply_edit
app.processEvents()
check("改份数**不会**把 WPS 文档的引擎静默改成 Word（原值保留）",
      win._config.tabs["1"].jobs[1].engine == "wps",
      win._config.tabs["1"].jobs[1].engine)
check("非 Word 文件（图片）的 engine 字段不受影响",
      win._config.tabs["1"].jobs[0].engine == "libreoffice",
      win._config.tabs["1"].jobs[0].engine)

# ② 多选 [WPS文档, Word文档, PDF]：显示第一个 Word 文件的引擎；改引擎只改 .docx
jobs = [PrintJob(file_path=PNG, page_count=1, engine="libreoffice", display_name="图片测试.png"),
        PrintJob(file_path=PDOCX_S, page_count=3, engine="wps", display_name="wps编辑.docx"),
        PrintJob(file_path=PDOCX_W, page_count=2, engine="word", display_name="word编辑.docx"),
        PrintJob(file_path=PA, page_count=3, engine="libreoffice", display_name="A.pdf")]
seed(jobs)
select_rows([3, 2])
check("多选[PDF, Word文档]：下拉显示第一个 Word 文件的引擎（Word）",
      win._edit_engine.currentText() == "Word", win._edit_engine.currentText())
check("多选[PDF, Word文档]：引擎下拉可用", win._edit_engine.isEnabled())
win._edit_engine.setCurrentIndex(1)      # 选 WPS
app.processEvents()
eng = [win._config.tabs["1"].jobs[i].engine for i in range(4)]
check("改引擎 → 选中的 .docx 变 WPS（未选的 WPS 文档保持原值），图片/PDF 的 engine 不动",
      eng == ["libreoffice", "wps", "wps", "libreoffice"], str(eng))

# ③ 选区里没有任何 Word 文件 → 只读标签、灰着，改别的参数也不许写 engine
jobs = [PrintJob(file_path=PNG, page_count=1, engine="libreoffice", display_name="图片测试.png"),
        PrintJob(file_path=PA, page_count=3, engine="libreoffice", display_name="A.pdf")]
seed(jobs)
select_rows([0, 1])
check("多选[图片, PDF]：引擎下拉灰着且是只读标签",
      (not win._edit_engine.isEnabled()) and win._edit_engine.currentText() == "图片渲染",
      f"enabled={win._edit_engine.isEnabled()} text={win._edit_engine.currentText()}")
win._edit_copies._increase()
app.processEvents()
check("选区没有 Word 文件时，改别的参数不会写 engine",
      [win._config.tabs["1"].jobs[i].engine for i in range(2)] == ["libreoffice", "libreoffice"],
      str([win._config.tabs["1"].jobs[i].engine for i in range(2)]))

# ④ 选中的 Word 文件引擎不一致 → 多出一项「混合（各自保留）」，且它不写回
jobs = [PrintJob(file_path=PDOCX_S, page_count=3, engine="wps", display_name="wps编辑.docx"),
        PrintJob(file_path=PDOCX_W, page_count=2, engine="word", display_name="word编辑.docx"),
        PrintJob(file_path=PA, page_count=3, engine="libreoffice", display_name="A.pdf")]
seed(jobs)
select_rows([0, 1])
check("多选两个引擎不同的 Word 文件：下拉多了「混合（各自保留）」且被选中",
      win._edit_engine.count() == 4 and win._edit_engine.currentText() == "混合（各自保留）",
      f"count={win._edit_engine.count()} text={win._edit_engine.currentText()}")
check("「混合」项本身可选（不是灰的）",
      win._edit_engine.model().item(3).isEnabled())
win._edit_copies._increase()             # 改别的参数不该动引擎
app.processEvents()
eng = [win._config.tabs["1"].jobs[i].engine for i in range(3)]
check("停在「混合」时改份数 → 两个 Word 文件各自保留原引擎",
      eng == ["wps", "word", "libreoffice"], str(eng))
check("「混合」项带着说明性的 tooltip",
      "各自保留" in win._edit_engine.toolTip(), win._edit_engine.toolTip())
win._edit_engine.setCurrentIndex(1)      # 显式选 WPS → 统一改掉
app.processEvents()
eng = [win._config.tabs["1"].jobs[i].engine for i in range(3)]
check("显式选一个引擎 → 两个 Word 文件统一改成它",
      eng == ["wps", "wps", "libreoffice"], str(eng))

select_rows([1])
check("单选（引擎一致）：下拉回到 3 项、没有「混合」项",
      win._edit_engine.count() == 3 and win._edit_engine.currentText() == "WPS",
      f"count={win._edit_engine.count()} text={win._edit_engine.currentText()}")

# ⑤ 引擎可用性灰度：只读标签不该被 Word 的可用性连带灰掉（且重建下拉后仍保持灰度）
import converter as _conv  # noqa: E402
_orig_avail = _conv.get_available_engines
_conv.get_available_engines = lambda: {"word": False, "wps": False, "libreoffice": True}
try:
    jobs = [PrintJob(file_path=PA, page_count=3, engine="libreoffice", display_name="A.pdf"),
            PrintJob(file_path=PDOCX_W, page_count=2, engine="word", display_name="word编辑.docx")]
    seed(jobs)
    select_rows([0])                     # 选中的是非 Word → 下拉是只读标签
    win._refresh_engine_availability()   # 显式调一次（与启动时的调用同路径）
    check("Word 没装时，只读标签（PDF「直接打印」）**不被灰掉**",
          win._edit_engine.model().item(0).isEnabled(),
          f"enabled={win._edit_engine.model().item(0).isEnabled()}")
    select_rows([1])                     # 选中 Word → 三选一，重建后应保留灰度
    app.processEvents()
    states = [win._edit_engine.model().item(i).isEnabled() for i in range(3)]
    check("选 Word 文件后：Word/WPS 两项灰、LibreOffice 亮（重建下拉也没丢灰度）",
          states == [False, False, True], str(states))
    check("不可用引擎带 tooltip", "未安装" in (win._edit_engine.model().item(0).toolTip() or ""),
          win._edit_engine.model().item(0).toolTip())
finally:
    _conv.get_available_engines = _orig_avail
    win._refresh_engine_availability()   # 还原真实灰度状态，别影响后续

print()
print("=" * 60)
print("全部通过 ✅" if ok_all else "存在 FAIL ❌")
sys.exit(0 if ok_all else 1)
