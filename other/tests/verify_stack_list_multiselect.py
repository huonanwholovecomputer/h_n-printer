# -*- coding: utf-8 -*-
"""验收脚本 · 攒批列表「拖动多选」（offscreen，不碰真打印机）

用户要求（2026-11-12）：「这里需要允许拖动多选」—— 截图里操作员框住的是 7 行
「🔓 可整份取走」的单面文件，意图是**一次把整片选中的纸都登记成「我已取走」**，
而不是逐个点。所以要钉住两件事，缺一条这个功能就是半成品：

  ① 左栏（以及同一个窗里的右栏 / 打印列表）真的能用鼠标**拖框**选多行
     —— `ExtendedSelection` + `SelectRows` + `setDragEnabled(False)` 三件缺一不可
     （dragEnabled=True 时从行内按住拖动会被 Qt 判成"内部拖动"，拖框根本起不来）；
  ② 「✓ 我已取走 / ↩ 放回」对**整片选区**生效，而且不会把 N 个模态告警框叠在
     操作员脸上：批量走一条 `toggle_many_requested`，主窗口**一次判定、一次落盘、
     一条日志、最多一个告警框、一次重绘**（账本裁决仍逐个走，拒绝理由不吞）。

覆盖：静态接线 / 拖框（正向、反向、起手列）/ Ctrl·Shift 点选 / 选区→按钮文案与
可用性 / 单选走老信号 / 批量信号内容 / 主窗口批量处理（成功、部分拒绝、忙时拒绝）。

    python other/tests/verify_stack_list_multiselect.py     # exit 0 = 全绿
"""
import io
import os
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_stack_multi_")
os.environ["APPDATA"] = _SANDBOX                  # 用户数据目录 → 沙箱（不碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QAbstractItemView, QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui  # noqa: E402
import duplex_stack as ds  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ============================================================
# 0. 静态接线
# ============================================================
print("=== 0. 静态接线 ===")
SRC = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
_make_tree = re.search(r"    def _make_tree\(.*?\n        return t\n", SRC, re.S)
check("找到 _make_tree（攒批列表三棵树都从这里造）", bool(_make_tree))
_mt = _make_tree.group(0) if _make_tree else ""
# ⚠ 必须锚到"整行就是这次调用"，不能用 `"xxx" in 源码`：注释里也写着这串字符
# （`⚠ setDragEnabled(False) 不是可选项…`），子串判据会被注释喂饱 —— 变异自证时实测漏过。
check("★ _make_tree 调用 ExtendedSelection（多选入口）",
      re.search(r"(?m)^\s*t\.setSelectionMode\(QAbstractItemView\.ExtendedSelection\)\s*$", _mt)
      is not None)
check("★ _make_tree 真的调用 setDragEnabled(False)（否则拖框被内部拖动抢走）",
      re.search(r"(?m)^\s*t\.setDragEnabled\(False\)\s*$", _mt) is not None)
check("★ _make_tree 里不再有 SingleSelection 的**调用**（旧单选口径）",
      re.search(r"(?m)^\s*t\.setSelectionMode\(QAbstractItemView\.SingleSelection\)\s*$", _mt)
      is None)
check("★ 批量信号存在且有说明",
      re.search(r"toggle_many_requested = Signal\(object\)", SRC) is not None)
check("★ 主窗口接上批量信号（不是只发不接）",
      "dlg.toggle_many_requested.connect(self._on_stack_taken_toggle_many)" in SRC)
check("★ 批量处理由账本逐个裁决 + 只落盘一次",
      SRC.count("def _on_stack_taken_toggle_many") == 1
      and "duplex_stack.save_manifest(bdir, self._stack)" in SRC)
check("选区解析抽成 _take_targets（按钮文案与点击共用一份口径）",
      "def _take_targets(self):" in SRC and SRC.count("self._take_targets()") >= 2)


# ============================================================
# 1. 造一叠：5 份单面（第 2 趟全空 ⇒ 可整份取走）+ 1 份双面（第 2 趟还要用）
# ============================================================
print("\n=== 1. 列表与选区 ===")
dlg = gui.StackQueueDialog()
dlg.resize(1240, 620)

st = ds.DuplexStack(capacity=90, printer_name="Fake Print")
plans = []
for i in range(5):
    plans.append({"label": f"单面2页-{i + 1}.pdf", "pages": [1, 2], "duplex": False})
plans.append({"label": "双面3页.pdf", "pages": [4, 5, 6], "duplex": True})
st.entries = [ds.StackEntry(seq=1, title="HN-2026-MULTI", page_count=13, sheet_count=7,
                            rotate=True, pdf="e1.pdf", first="odd", state=ds.STATE_DONE,
                            file_plans=plans)]
st.entries[0].original_sheet_count = 7
dlg.render(st, busy=False, wait_requests=[], printing_text="")
dlg.show()
_app.processEvents()

TL = dlg._tree_left
sec = {"one": [], "many": []}
dlg.toggle_requested.connect(lambda s, i, t: sec["one"].append((s, i, t)))
dlg.toggle_many_requested.connect(lambda ch: sec["many"].append(list(ch)))

check("★ 左栏 1 单 / 6 个文件行（5 单面 + 1 双面）",
      TL.topLevelItemCount() == 1 and TL.topLevelItem(0).childCount() == 6,
      f"{TL.topLevelItemCount()} 单 / {TL.topLevelItem(0).childCount()} 行")
check("三棵树都开了多选",
      all(t.selectionMode() == QAbstractItemView.ExtendedSelection
          for t in (dlg._tree_left, dlg._tree_right, dlg._tree_local)))


def child(i):
    return TL.topLevelItem(0).child(i)


def center(item, col=0):
    return TL.visualRect(TL.indexFromItem(item, col)).center()


def send(kind, pos, buttons=Qt.NoButton, button=Qt.NoButton):
    vp = TL.viewport()
    ev = QMouseEvent(kind, QPointF(pos), QPointF(vp.mapToGlobal(pos)), button, buttons,
                     Qt.NoModifier)
    _app.sendEvent(vp, ev)
    _app.processEvents()


def sel_indexes():
    picked = {id(x) for x in TL.selectedItems()}
    return [i for i in range(TL.topLevelItem(0).childCount()) if id(child(i)) in picked]


def rubber_band(i_from, i_to, col_from=0, col_to=0):
    """按下 → 分 5 步移动（按住左键）→ 中途读选区 → 松开。"""
    TL.clearSelection()
    _app.processEvents()
    a, b = center(child(i_from), col_from), center(child(i_to), col_to)
    send(QEvent.MouseButtonPress, a, Qt.LeftButton, Qt.LeftButton)
    for k in range(1, 6):
        send(QEvent.MouseMove,
             QPoint(a.x() + (b.x() - a.x()) * k // 5, a.y() + (b.y() - a.y()) * k // 5),
             Qt.LeftButton)
    mid = sel_indexes()
    send(QEvent.MouseButtonRelease, b, Qt.NoButton, Qt.LeftButton)
    return mid


# ── 1a. 拖框：正向 / 反向 / 从中间列起手 ──
mid = rubber_band(0, 4)
check("★ 拖框过程（未松开）实时选中第 0..4 行", mid == [0, 1, 2, 3, 4], f"选中={mid}")
check("★ 松开后保持 5 行选中", sel_indexes() == [0, 1, 2, 3, 4], f"选中={sel_indexes()}")
rubber_band(4, 2)
check("★ 反向拖框（4 → 2）只选 2..4", sel_indexes() == [2, 3, 4], f"选中={sel_indexes()}")
rubber_band(0, 2, col_from=2, col_to=3)
check("★ 从「第 2 趟」列中间起手拖框同样有效（选 0..2）",
      sel_indexes() == [0, 1, 2], f"选中={sel_indexes()}")
check("★ 拖框不会顺手选中顶层「#1 …」汇总行（它不是一个可动纸的单位）",
      TL.selectedItems() and all(x.parent() is not None for x in TL.selectedItems()))

# ── 1b. Ctrl / Shift ──
def click(i, mods=Qt.NoModifier):
    QTest.mouseClick(TL.viewport(), Qt.LeftButton, mods, center(child(i)))


def release_mods():
    QTest.keyRelease(TL.viewport(), Qt.Key_Control)
    QTest.keyRelease(TL.viewport(), Qt.Key_Shift)
    _app.processEvents()


TL.clearSelection()
_app.processEvents()
click(0)
click(2, Qt.ControlModifier)
click(4, Qt.ControlModifier)
check("Ctrl+左键逐个追加（0,2,4）", sel_indexes() == [0, 2, 4], f"选中={sel_indexes()}")
click(2, Qt.ControlModifier)
check("Ctrl+再点同一行 = 取消该行（0,4）", sel_indexes() == [0, 4], f"选中={sel_indexes()}")
click(0)
click(3, Qt.ShiftModifier)
check("Shift+左键连选（0..3）", sel_indexes() == [0, 1, 2, 3], f"选中={sel_indexes()}")
release_mods()


# ============================================================
# 2. 选区 → 按钮文案 / 可用性
# ============================================================
print("\n=== 2. 「✓ 我已取走 / ↩ 放回」跟随整片选区 ===")


def select(idx_list):
    TL.clearSelection()
    for i in idx_list:
        child(i).setSelected(True)
    _app.processEvents()
    dlg._on_left_selection()


select([0, 1, 2, 3, 4])
check("★ 选中 5 份「第 2 趟全空」→ 按钮写明份数「✓ 我已取走（5 份）」",
      dlg._btn_take.text() == "✓ 我已取走（5 份）", dlg._btn_take.text())
check("★ 且可点（未被账本忙状态挡住）", dlg._btn_take.isEnabled())

select([5])
check("★ 只选「第 2 趟还要用」的那份 → 禁用（撤走必然整叠错位）",
      not dlg._btn_take.isEnabled(), f"enabled={dlg._btn_take.isEnabled()}")
check("★ 禁用时 tooltip 说清原因", "第 2 趟还要用" in dlg._btn_take.toolTip(),
      dlg._btn_take.toolTip())

select([0, 5, 2])
check("混合选区（2 份可走 + 1 份不可走）→ 只算那 2 份",
      dlg._btn_take.text() == "✓ 我已取走（2 份）" and dlg._btn_take.isEnabled(),
      dlg._btn_take.text())
check("混合选区 tooltip 指明另外 1 行这次不处理",
      "另外 1 行" in dlg._btn_take.toolTip(), dlg._btn_take.toolTip())

select([0])
check("单选一份 → 文案保持老样子「✓ 我已取走」（不带份数）",
      dlg._btn_take.text() == "✓ 我已取走", dlg._btn_take.text())

select([])
check("空选区 → 禁用 + 提示「可拖框多选」",
      not dlg._btn_take.isEnabled() and "拖框" in dlg._btn_take.toolTip(),
      dlg._btn_take.toolTip())

# 忙状态：份数照算，但一律不许动
dlg.render(st, busy=True, wait_requests=[], printing_text="正在打整叠第 2 趟")
select([0, 1, 2])
check("打印机忙 → 按钮禁用（但份数照常显示，操作员知道点下去要动几份）",
      not dlg._btn_take.isEnabled() and "（3 份）" in dlg._btn_take.text(),
      f"{dlg._btn_take.isEnabled()} / {dlg._btn_take.text()}")
dlg.render(st, busy=False, wait_requests=[], printing_text="")


# ============================================================
# 3. 点按钮：单选走老信号，多选走批量信号
# ============================================================
print("\n=== 3. 点击 → 信号 ===")
sec["one"].clear()
sec["many"].clear()
select([0])
dlg._btn_take.click()
_app.processEvents()
check("单选点下去仍发老信号 toggle_requested(1, 0, True)（老调用方零回归）",
      sec["one"] == [(1, 0, True)], f"one={sec['one']}")
check("单选不会同时发批量信号", sec["many"] == [], f"many={sec['many']}")

sec["one"].clear()
sec["many"].clear()
select([0, 1, 3, 5])          # 3 份可走 + 1 份不可走
dlg._btn_take.click()
_app.processEvents()
check("★ 多选点下去发一条批量信号，带 3 项（跳过第 2 趟还要用的那份）",
      sec["many"] == [[(1, 0, True), (1, 1, True), (1, 3, True)]], f"many={sec['many']}")
check("多选不再逐条发老信号（不会把 N 个告警框叠在操作员脸上）",
      sec["one"] == [], f"one={sec['one']}")

# 全选「已取走」的行 → 批量放回
for i in (0, 1):
    st.entries[0].toggle_taken(i, True)
dlg.render(st, busy=False, wait_requests=[], printing_text="")
TL = dlg._tree_left
sec["many"].clear()
select([0, 1])
check("★ 选中的都是已取走的 → 按钮变「↩ 放回（2 份）」",
      dlg._btn_take.text() == "↩ 放回（2 份）", dlg._btn_take.text())
dlg._btn_take.click()
_app.processEvents()
check("★ 放回走同一批信号，flag=False",
      sec["many"] == [[(1, 0, False), (1, 1, False)]], f"many={sec['many']}")


# ============================================================
# 4. 主窗口批量处理：一次落盘 / 一条日志 / 最多一个告警框
# ============================================================
print("\n=== 4. 主窗口 _on_stack_taken_toggle_many ===")
st2 = ds.DuplexStack(capacity=90, printer_name="Fake Print")
st2.entries = [ds.StackEntry(seq=1, title="HN-BATCH", page_count=6, sheet_count=6,
                             rotate=True, pdf="e2.pdf", first="odd", state=ds.STATE_DONE,
                             file_plans=[{"label": f"f{i}.pdf", "pages": [i * 2 + 1, i * 2 + 2],
                                          "duplex": False} for i in range(3)])]
st2.entries[0].original_sheet_count = 6
BDIR = os.path.join(_SANDBOX, "batch_9")
os.makedirs(BDIR, exist_ok=True)


class _Host:
    """只给批量处理用到的那些成员（不初始化半个 MainWindow）。"""

    def __init__(self, stack, busy=False, bdir=""):
        self._stack = stack
        self._busy_flag = busy
        self._bdir = bdir
        self.logs = []
        self.synced = 0
        self.refreshed = 0

    def _log(self, msg):
        self.logs.append(str(msg))

    def _stack_busy(self):
        return self._busy_flag

    def _stack_batch_dir(self):
        return self._bdir

    def _sync_stack_button(self):
        self.synced += 1

    def _refresh_stack_queue(self):
        self.refreshed += 1

    _stack_present_sheets = lambda self: gui.MainWindow._stack_present_sheets(self)
    _stack_original_sheets = lambda self: gui.MainWindow._stack_original_sheets(self)
    _stack_taken_sheets = lambda self: gui.MainWindow._stack_taken_sheets(self)


_saved, _warned = [], []
_orig_save, _orig_warn = gui.duplex_stack.save_manifest, gui.QMessageBox.warning
gui.duplex_stack.save_manifest = lambda d, stk: _saved.append((d, len(stk.entries)))
gui.QMessageBox.warning = staticmethod(lambda *a, **kw: _warned.append(a[2] if len(a) > 2 else ""))
try:
    host = _Host(st2, bdir=BDIR)
    gui.MainWindow._on_stack_taken_toggle_many(host, [(1, 0, True), (1, 1, True), (1, 9, True)])
    check("★ 两项成功 + 一项越界被账本拒绝 ⇒ taken_plans 只含 0、1",
          st2.entries[0].taken_plans == [0, 1], f"taken={st2.entries[0].taken_plans}")
    check("★ 清单只落盘一次（不是三次）", len(_saved) == 1, f"save={len(_saved)}")
    check("日志一条汇总：批量取走 2 份 + 被拒 1 份 + 第 2 趟要送的张数",
          any("批量取走 2 份文件" in m and "1 份被账本拒绝" in m and "第 2 趟要送" in m
              for m in host.logs), f"{host.logs}")
    check("★ 只弹一个告警框（写明哪一份没成），不是每个失败各弹一个",
          len(_warned) == 1 and "第 1 单的第 10 个文件" in _warned[0], f"{_warned}")
    check("刷新/按钮同步各只做一次", host.refreshed == 1 and host.synced == 1,
          f"refresh={host.refreshed} sync={host.synced}")

    # 忙：不落盘、不改账本、只提示
    _saved.clear(); _warned.clear()
    st3 = ds.DuplexStack(capacity=90, printer_name="Fake Print")
    st3.entries = [ds.StackEntry(seq=1, title="HN-BUSY", page_count=4, sheet_count=4,
                                 rotate=True, pdf="e3.pdf", first="odd", state=ds.STATE_DONE,
                                 file_plans=[{"label": "g0.pdf", "pages": [1, 2], "duplex": False},
                                             {"label": "g1.pdf", "pages": [3, 4], "duplex": False}])]
    st3.entries[0].original_sheet_count = 4
    busy_host = _Host(st3, busy=True, bdir=BDIR)
    gui.MainWindow._on_stack_taken_toggle_many(busy_host, [(1, 0, True), (1, 1, True)])
    check("打印机忙 ⇒ 一份都没改",
          st3.entries[0].taken_plans == [], f"taken={st3.entries[0].taken_plans}")
    check("打印机忙 ⇒ 不落盘", _saved == [], f"save={len(_saved)}")
    check("打印机忙 ⇒ 只弹一个说明原因的告警框",
          len(_warned) == 1 and "不能批量取走" in _warned[0], f"{_warned}")

    _warned.clear()
    _refreshed_before = host.refreshed
    gui.MainWindow._on_stack_taken_toggle_many(host, [])
    check("空变更清单 = 什么也不做（不发日志、不弹框、不重绘）",
          _warned == [] and host.refreshed == _refreshed_before,
          f"refresh={host.refreshed}（之前 {_refreshed_before}）")
finally:
    gui.duplex_stack.save_manifest = _orig_save
    gui.QMessageBox.warning = _orig_warn

dlg.close()


# ============================================================
# 5. 2 秒心跳刷新不许清掉选区（2026-11-12 用户反馈）
# ============================================================
print("\n=== 5. 心跳刷新不清选区 ===")
# 现场问题：这个窗由主窗口 2 秒心跳驱动（`_refresh_stack_queue` → `render()`），
# 以前每次 render 都无条件 `_tree_left.clear()` 重建 ⇒ 拖框选中的几行 3~5 秒内被刷掉，
# 「批量取走」根本点不完。修法：内容指纹没变就不重建；真变了才重建并按 (seq,index)
# 还原选区/滚动/折叠。
hb = gui.StackQueueDialog()
hb.resize(1240, 620)
hb.show()


def _hb_stack(title="HB"):
    s = ds.DuplexStack(capacity=90, printer_name="Fake Print")
    s.entries = [ds.StackEntry(seq=1, title=title, page_count=13, sheet_count=7,
                               rotate=True, pdf="h1.pdf", first="odd", state=ds.STATE_DONE,
                               file_plans=list(plans))]
    s.entries[0].original_sheet_count = 7
    return s


st_hb = _hb_stack()
hb.render(st_hb, busy=False, wait_requests=[], printing_text="")
_app.processEvents()
HTL = hb._tree_left


def hb_child(i):
    return HTL.topLevelItem(0).child(i)


# 模拟一次拖框：选中 3 行（0/1/3 都是单面 ⇒ 三行都可整份取走）
for i in (0, 1, 3):
    hb_child(i).setSelected(True)
_app.processEvents()
sel_before = sorted(hb._left_selected_keys())
check("拖框选中 3 行（按钮立刻显示份数）",
      len(sel_before) == 3 and "（3 份）" in hb._btn_take.text(),
      f"sel={sel_before} btn={hb._btn_take.text()!r}")

# 心跳：内容完全没变的 render 连调 5 次（≈10 秒，正是用户被刷掉的时间窗）
top_before = HTL.topLevelItem(0)
child_before = hb_child(0)
for _ in range(5):
    hb.render(st_hb, busy=False, wait_requests=[], printing_text="")
    _app.processEvents()
check("★ 内容没变的心跳刷新不动选区（用户反馈「被刷掉」已修）",
      sorted(hb._left_selected_keys()) == sel_before,
      f"期望 {sel_before} 实际 {sorted(hb._left_selected_keys())}")
check("★ 心跳刷新也不把「✓ 我已取走（3 份）」打回默认文案",
      "（3 份）" in hb._btn_take.text(), f"{hb._btn_take.text()!r}")
# 指纹命中必须**真的不重建**：重建会打断进行中的拖框、把滚动位置弹回顶部。
check("★ 内容没变 ⇒ 一行都没重建（同一批 QTreeWidgetItem 对象）",
      HTL.topLevelItem(0) is top_before and hb_child(0) is child_before)

# 内容真的变了（多了一单）⇒ 允许重建，但必须把原来选中的 (seq,index) 还原
st_hb2 = _hb_stack("HB-2")
st_hb2.entries.append(ds.StackEntry(
    seq=2, title="HN-HB-2", page_count=2, sheet_count=2, rotate=True, pdf="h2.pdf",
    first="odd", state=ds.STATE_DONE,
    file_plans=[{"label": "新单.pdf", "pages": [1, 2], "duplex": False}]))
st_hb2.entries[1].original_sheet_count = 2
hb.render(st_hb2, busy=False, wait_requests=[], printing_text="")
_app.processEvents()
check("★ 真有变化而重建时，原位选中的行仍然选中（不静默丢选区）",
      sorted(hb._left_selected_keys()) == sel_before,
      f"期望 {sel_before} 实际 {sorted(hb._left_selected_keys())}")
check("重建后新增的那一单也画出来了", HTL.topLevelItemCount() == 2,
      f"{HTL.topLevelItemCount()} 单")

# 选中行被账本改成「已取走」⇒ 内容变了要重建，但选区仍按 (seq,index) 还原
hb_child(1).setSelected(True)
_app.processEvents()
sel3 = sorted(hb._left_selected_keys())
st_hb3 = _hb_stack("HB-3")
st_hb3.entries[0].taken_plans = [0]          # 第 0 个文件已取走（显示删除线）
hb.render(st_hb3, busy=False, wait_requests=[], printing_text="")
_app.processEvents()
check("内容变了（某行变成已取走）重建后，选区依然按行还原",
      sorted(hb._left_selected_keys()) == sel3,
      f"期望 {sel3} 实际 {sorted(hb._left_selected_keys())}")
hb.close()


# ============================================================
# 汇总
# ============================================================
print("\n" + ("全部通过 ✅（攒批列表拖动多选 + 批量取走）" if ok_all
              else "有失败项 ❌（见上）"))
sys.exit(0 if ok_all else 1)
