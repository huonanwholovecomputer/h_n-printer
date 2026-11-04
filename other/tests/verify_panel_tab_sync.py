# -*- coding: utf-8 -*-
"""验收脚本 · 右侧「任务参数」面板在标签页切换/复制时的取值正确性

2026-11-02 用户反馈（现场 31 页习题，封面单面 + 2-31 双面拆成两个标签页打）：
「右侧的任务参数面板中的『页码范围』貌似会在某些情况下发生跨页保留，即出现了在其他
已锁定标签页出现第二个标签页中 2-31 的页码范围，这可能和订单锁定、『➕ 添加到新标签页』
功能、标签页的新建等功能有关。」

根因（本脚本钉住的就是它）：`_on_table_selection()` 在「标签页已冻结」时**提前 return**，
只把控件灰掉、**不按选中行刷新内容** ⇒ 面板继续显示上一个标签页留下的页码范围/份数。

本脚本断言的是"面板显示的事实 = 选中任务的事实"，而不是"控件被灰掉了"：

  ① 切到已锁定标签页并选中行 ⇒ 面板按**这一行**刷新（不是上一个标签页的残留）；
  ② 切回未锁定标签页 ⇒ 面板又跟着刷新回它自己的值（两个方向都不能串）；
  ③ 锁定标签页里面板只读：点什么都不落盘（灰控件不回写）；
  ④ 「➕ 添加到新标签页」⇒ 新标签页是**独立对象**（改新页不动旧页），页参数（含
     「封面单独打印」）照抄；
  ⑤ 无选中 ⇒ 面板禁用（不会把残值写进任何任务）。

全程离屏、不出纸、不碰真实 %APPDATA%。

    python tests/verify_panel_tab_sync.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_tab_sync_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtCore import QItemSelectionModel  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import converter as _converter  # noqa: E402

_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}

import gui  # noqa: E402
import paths  # noqa: E402
from printer_config import PrintJob, TabSettings  # noqa: E402

# 模态框一律自动确认（离屏里没人能点）
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
        c.drawString(80, 700, f"{name} page {i + 1}")
        c.showPage()
    c.save()
    return path


PDF31 = make_pdf("heji.pdf", 31)

win = gui.MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
_app.processEvents()


def frozen_job(**kw):
    base = dict(file_path=PDF31, copies=1, duplex="on", duplex_mode="long-edge",
                page_range="", page_count=31, display_name="a.pdf")
    base.update(kw)
    return PrintJob(**base)


# 标签页 1：已锁定（= 已回报订单快照），页码范围空
# 标签页 2：未锁定，页码范围 2-31、份数 2
tab1 = TabSettings()
tab1.frozen = True
tab1.jobs = [frozen_job()]
tab2 = TabSettings()
tab2.jobs = [frozen_job(page_range="2-31", copies=2, duplex="off", display_name="b.pdf")]
win._config.tabs = {"1": tab1, "2": tab2}


def select_tab(key: str):
    win._current_tab = key
    win._config.active_tab = key
    win._rebuild_table()
    win._refresh_tab_display()
    _app.processEvents()
    if win._table.model().rowCount():
        win._table.clearSelection()
        win._table.selectionModel().select(
            win._table.model().index(0, 0),
            QItemSelectionModel.Select | QItemSelectionModel.Rows)
        win._on_table_selection()
        _app.processEvents()


# ============================================================
print("\n=== ① 已锁定标签页不显示别的标签页的参数 ===")
# ============================================================
select_tab("2")
check("①a 未锁定标签页显示它自己的页码范围（2-31）与份数（2）",
      win._edit_page_range.get_ranges() == "2-31" and win._edit_copies.value() == 2,
      f"{win._edit_page_range.get_ranges()!r} / {win._edit_copies.value()}")

select_tab("1")
check("★ ①b 切到已锁定标签页 ⇒ 页码范围回到**这一行**的空值（不是 2-31 的残留）",
      win._edit_page_range.get_ranges() == "",
      repr(win._edit_page_range.get_ranges()))
check("★ ①c 份数/单双面也跟着刷新（1 份、双面），不是上一个标签页的 2 份单面",
      win._edit_copies.value() == 1 and win._edit_duplex.currentIndex() == 0,
      f"{win._edit_copies.value()} / idx={win._edit_duplex.currentIndex()}")
check("①d 锁定标签页里面板整体只读（页码范围/份数/单双面都灰着）",
      win._edit_page_range.isEnabled() is False
      and win._edit_copies.isEnabled() is False
      and win._edit_duplex.isEnabled() is False)
check("①e 刷新面板不改任务（锁定的快照必须原样）",
      tab1.jobs[0].page_range == "" and tab1.jobs[0].copies == 1)

# ============================================================
print("\n=== ② 来回切换两个方向都不能串 ===")
# ============================================================
select_tab("2")
check("②a 切回未锁定标签页 ⇒ 又是 2-31 / 2 份",
      win._edit_page_range.get_ranges() == "2-31" and win._edit_copies.value() == 2,
      f"{win._edit_page_range.get_ranges()!r} / {win._edit_copies.value()}")
select_tab("1")
select_tab("2")
check("②b 连续切换 3 次后仍各归各位",
      win._edit_page_range.get_ranges() == "2-31" and win._edit_copies.value() == 2,
      f"{win._edit_page_range.get_ranges()!r} / {win._edit_copies.value()}")

# ============================================================
print("\n=== ③ 锁定标签页里点「封面单独打印」也不会落盘 ===")
# ============================================================
select_tab("1")
win._edit_simplex.setChecked(True)
win._on_simplex_toggled(True)
_app.processEvents()
check("★ ③a 锁定标签页：勾选框被回滚成未勾选、任务未被写入",
      win._edit_simplex.isChecked() is False
      and tab1.jobs[0].simplex_enabled is False,
      f"checked={win._edit_simplex.isChecked()} job={tab1.jobs[0].simplex_enabled}")

# ============================================================
print("\n=== ④ 复制到新标签页：独立对象 + 页参数照抄 ===")
# ============================================================
# 给锁定标签页的 job 一个「封面单独打印」配置（模拟原单就是这么打的）
tab1.jobs[0].simplex_enabled = True
tab1.jobs[0].simplex_pages = "1,16-23"
tab1.jobs[0].page_range = "1-31"
select_tab("1")
new_key = win._add_rows_to_new_tab([0])
_app.processEvents()
check("④a 「添加到新标签页」返回了新标签页且当前已切过去",
      bool(new_key) and win._current_tab == new_key,
      f"new_key={new_key!r} current={win._current_tab!r}")
new_jobs = win._get_current_jobs()
check("★ ④b 新标签页的 job 是**另一个对象**（改新页不会动到锁定的快照）",
      len(new_jobs) == 1 and new_jobs[0] is not tab1.jobs[0])
check("★ ④c 页参数照抄：页码范围 1-31、封面单独打印 1,16-23",
      new_jobs and new_jobs[0].page_range == "1-31"
      and new_jobs[0].simplex_enabled is True
      and new_jobs[0].simplex_pages == "1,16-23",
      f"{new_jobs[0].page_range!r} / {new_jobs[0].simplex_enabled} / "
      f"{new_jobs[0].simplex_pages!r}" if new_jobs else "no jobs")
select_tab(new_key)   # 复制后新标签页里还没选中行 → 先选中再读面板
check("④d 新标签页未锁定：选中该行后面板可编辑并显示照抄来的值",
      win._edit_page_range.isEnabled() is True
      and win._edit_simplex.isChecked() is True,
      f"enabled={win._edit_page_range.isEnabled()} checked={win._edit_simplex.isChecked()}")
# 在新标签页改页码范围 → 锁定标签页的快照不受影响
win._edit_page_range.set_ranges("2-31")
win._edit_page_range.rangesChanged.emit()
_app.processEvents()
check("★ ④e 在新标签页改页码范围 ⇒ 锁定的旧标签页仍是 1-31（互不干扰）",
      win._get_current_jobs()[0].page_range == "2-31"
      and tab1.jobs[0].page_range == "1-31",
      f"new={win._get_current_jobs()[0].page_range!r} old={tab1.jobs[0].page_range!r}")

# ============================================================
print("\n=== ⑤ 无选中 ⇒ 面板禁用、不写任何任务 ===")
# ============================================================
before = (win._get_current_jobs()[0].page_range,
          win._get_current_jobs()[0].simplex_pages)
win._table.clearSelection()
win._on_table_selection()
_app.processEvents()
after = (win._get_current_jobs()[0].page_range,
         win._get_current_jobs()[0].simplex_pages)
check("⑤a 清空选区 ⇒ 页面范围/封面页两个输入都禁用",
      win._edit_page_range.isEnabled() is False
      and win._edit_simplex_pages.isEnabled() is False)
check("⑤b 清空选区不改任务", before == after, f"{before} → {after}")
check("★ ⑤c 清空选区 ⇒ 两个页输入也**清空文本**（面板是纯视图，不能留着上一个持有者的字）",
      win._edit_page_range.get_ranges() == ""
      and win._edit_simplex_pages.get_ranges() == "",
      f"{win._edit_page_range.get_ranges()!r} / {win._edit_simplex_pages.get_ranges()!r}")

# ============================================================
print("\n=== ⑥ 切页后「一行都没选中」⇒ 面板清空（不是只灰掉） ===")
# ============================================================
# 真实路径：操作员在 tab2 选了行（面板显示它自己的范围），点「›」切走，
# 切过去那一刻新标签页**没有选中行**（_switch_tab 内部只有 _rebuild_table()
# + _sync_edit_enabled(False)）。旧写法只置灰、不清文本 ⇒ 面板继续挂着
# 上一个标签页的范围，和表格「页码范围」列（读 job.page_range 真值）自相矛盾。
win._current_tab = "2"
win._config.active_tab = "2"
win._rebuild_table()
win._table.clearSelection()
win._table.selectionModel().select(
    win._table.model().index(0, 0),
    QItemSelectionModel.Select | QItemSelectionModel.Rows)
win._on_table_selection()
_app.processEvents()
check("⑥a 前置：tab2 选行后面板显示它自己的 2-31",
      win._edit_page_range.get_ranges() == "2-31",
      repr(win._edit_page_range.get_ranges()))
# 切页（不选中任何行）
win._current_tab = "1"
win._config.active_tab = "1"
win._rebuild_table()
win._sync_edit_enabled(False)
_app.processEvents()
check("★ ⑥b 切页后未选中行 ⇒ 页码范围被清空（不是残留 2-31）",
      win._edit_page_range.get_ranges() == "",
      repr(win._edit_page_range.get_ranges()))
check("★ ⑥c 同时清掉封面单独打印的页输入并收起输入框",
      win._edit_simplex_pages.get_ranges() == ""
      and win._edit_simplex_pages.isVisible() is False,
      repr(win._edit_simplex_pages.get_ranges()))
check("★ ⑥d 面板此时是禁用态（无主）",
      win._edit_page_range.isEnabled() is False)
check("★ ⑥e 清空只是显示层：tab2 的 job.page_range 仍是 2-31",
      tab2.jobs[0].page_range == "2-31", repr(tab2.jobs[0].page_range))

win.close()
print("\n" + ("✅ 全部通过" if ok_all else "❌ 存在失败项"))
sys.exit(0 if ok_all else 1)
