# -*- coding: utf-8 -*-
"""验收脚本 · PDF 转换完成后右侧「编辑选中任务」面板必须刷新 + 启动日志记版本号

现场反馈（2026-11-01，用户截图）：
选中一个**正在转换**的 Word 文件，转换完成后文件列表那行已经更新（页数、方向、费用都对），
但右侧「⚙ 任务参数 / 编辑选中任务」面板还停在「未转换」的样子 —— 单/双面灰成单面、
「排除末尾空白页」不可点、页码范围没有总页数。

根因：`MainWindow._on_convert_finished` 里更新面板的那次调用被 `_single_page_norm`
（1 页文件被自动改成单面）这个**特例**门住了：
    if _single_page_norm and target_row in self._selected_rows():
        self._on_table_selection()
普通 Word 文件转换完成时 `_single_page_norm` 恒为 False ⇒ 面板永远等不到刷新。

本脚本验收两层：

  ① 静态：`_on_convert_finished` 改成无条件调 `_refresh_edit_panel_after_convert()`，
     旧的特例门已删除；新方法带「选区不含这一行就跳过」「冻结标签页跳过」「保留正在输入
     的页码范围文本」三道保护；调用点仍在「目标标签页 == 前台标签页」块内。
  ② 真 MainWindow（offscreen，不碰真实 %APPDATA%）：
     · 转换前页数未知 → 面板总页数 0；转换完成（3 页 PDF，末页空白）→ 面板总页数 3、
       「排除末尾空白页」文案/可用性跟着变（这就是用户截图里没更新的那一块）；
     · 选区不在这一行 → 面板一个字节都不动；
     · 光标停在页码范围输入框里 → 没提交的 "2" 不被冲掉；
     · 启动即把当前版本号写进当天日志文件。

    python tests/verify_edit_panel_refresh.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_panel_refresh_")
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
import daily_log  # noqa: E402
from printer_config import PrintJob, TabSettings  # noqa: E402

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


def section(title):
    print(f"\n=== {title} ===")


# ══════════════════════════════════════════════════════════════════
# ① 静态：面板刷新不再是「单页归一化」的特例
# ══════════════════════════════════════════════════════════════════
section("① 静态：转换完成 → 无条件刷新面板")

_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()


def _method_body(src: str, header: str) -> str:
    """按缩进切出一个方法体（从 header 所在行到第一个缩进 <= 它的非空行为止）。"""
    i = src.find(header)
    if i < 0:
        return ""
    line_start = src.rfind("\n", 0, i) + 1
    base = len(src[line_start:i]) - len(src[line_start:i].lstrip())
    out = []
    pos = src.find("\n", i) + 1
    while 0 <= pos < len(src):
        end = src.find("\n", pos)
        line = src[pos:] if end < 0 else src[pos:end]
        if line.strip() and (len(line) - len(line.lstrip())) <= base:
            break
        out.append(line)
        if end < 0:
            break
        pos = end + 1
    return "\n".join(out)


_CONV = _method_body(_src, "def _on_convert_finished(")
check("① _on_convert_finished 存在", bool(_CONV))
check("★ ① 转换完成后面板刷新改为无条件调用 `_refresh_edit_panel_after_convert(target_row)`",
      "self._refresh_edit_panel_after_convert(target_row)" in _CONV)
check("★ ① 旧的特例门（只在单页归一化时刷新）已删除",
      "_single_page_norm and target_row in self._selected_rows()" not in _src,
      "旧写法只在这一种分支刷新面板，普通 Word 文件转完永远不更新")
check("① 刷新调用仍在「目标标签页 == 前台标签页」块内（后台标签页不动）",
      0 <= _CONV.find("if not tab_key or tab_key == self._current_tab:") < _CONV.find(
          "self._refresh_edit_panel_after_convert(target_row)"))

_PANEL = _method_body(_src, "def _refresh_edit_panel_after_convert(")
check("★ ① 新方法 _refresh_edit_panel_after_convert(row) 存在", bool(_PANEL))
check("① 选区不含这一行 → 不刷新（面板显示的是别的行）",
      "row not in set(self._selected_rows())" in _PANEL)
check("① 冻结（打印后固定）标签页 → 不刷新",
      "_is_current_tab_frozen()" in _PANEL)
check("① 光标停在页码范围里 → 保留没提交的文本（get_ranges / set_ranges）",
      "get_ranges()" in _PANEL and "set_ranges(keep_range)" in _PANEL)
check("① 真正重画面板走既有只读入口 `_on_table_selection()`（不重复一套面板逻辑）",
      "self._on_table_selection()" in _PANEL)
check("① 面板刷新失败不得影响转换结果（异常被吞并记 warning）",
      "转换完成后面板刷新失败" in _PANEL)

section("② 静态：启动日志记版本号")

_LOG_LINE = 'f"HN 本地打印工具启动 · 版本 v{APP_VERSION}"'
check("★ ② 启动日志行含当前版本号（APP_VERSION）", _LOG_LINE in _src)
check("② 保留旧前缀「HN 本地打印工具启动」（verify_daily_log_split 的既有断言仍成立）",
      "HN 本地打印工具启动" in _src
      and "HN 本地打印工具启动 · 版本 v{APP_VERSION}" in _src)
_version_log_body = _method_body(_src, "_file_logger.info(f\"HN 本地打印工具启动")
check("② 启动日志行不含 markdown 星号", "*" not in _version_log_body.split("\n")[0])

# ══════════════════════════════════════════════════════════════════
# ③ 真 MainWindow：面板刷新行为
# ══════════════════════════════════════════════════════════════════
section("③ 真 MainWindow：转换完成 → 面板按最新页数重画")

from reportlab.pdfgen import canvas  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402


def make_pdf(name: str, pages: int, blank_last: bool = False) -> str:
    path = os.path.join(_SANDBOX, name)
    c = canvas.Canvas(path, pagesize=A4)
    for i in range(pages):
        if not (blank_last and i == pages - 1):
            c.drawString(72, 760, f"{name} page {i + 1}")
        c.showPage()
    c.save()
    return path


PDF3B = make_pdf("three_blank.pdf", 3, blank_last=True)
PDF5 = make_pdf("five.pdf", 5)
check("③ 测试素材：3 页（末页空白）与 5 页真 PDF",
      os.path.getsize(PDF3B) > 400 and os.path.getsize(PDF5) > 400)

win = gui.MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
_app.processEvents()

# 启动日志（MainWindow.__init__ 里写的）
_log_path = daily_log.latest_log_path(paths.logs_dir())
_log_body = ""
if _log_path and os.path.isfile(_log_path):
    with open(_log_path, encoding="utf-8", errors="replace") as _f:
        _log_body = _f.read()
check("★ ③ 日志文件里能查到本次启动的版本号",
      f"HN 本地打印工具启动 · 版本 v{gui.APP_VERSION}" in _log_body,
      f"{os.path.basename(_log_path or '(无日志文件)')}")


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


def select_row(row: int):
    win._table.clearSelection()
    win._table.selectionModel().select(
        win._table.model().index(row, 0),
        QItemSelectionModel.Select | QItemSelectionModel.Rows)
    _app.processEvents()


def panel_state():
    return {
        "total_pages": win._edit_page_range._total_pages,
        "range": win._edit_page_range.get_ranges(),
        "blank_text": win._edit_blank_tail.text(),
        "blank_enabled": win._edit_blank_tail.isEnabled(),
        "duplex_enabled": win._edit_duplex.isEnabled(),
    }


DOCX3 = os.path.join(_SANDBOX, "正在转换的作业.docx")
with open(DOCX3, "wb") as _f:
    _f.write(b"not a real docx\n")                 # 内容无关：回调不读文件

jobs = seed([PrintJob(file_path=DOCX3, copies=1, duplex="on", duplex_mode="long-edge",
                      page_count=0, orientation="", engine="word",
                      display_name="正在转换的作业.docx")])
select_row(0)
win._on_table_selection()
_app.processEvents()
_before = panel_state()
check("③ 转换前：页数未知 → 面板总页数 0、末页空白页不可点（= 截图里那种「未转换」样子）",
      _before["total_pages"] == 0 and _before["blank_enabled"] is False,
      f"{_before}")

_expected_tail = gui.MainWindow._detect_blank_tail(PDF3B)
win._on_convert_finished(0, DOCX3, PDF3B, 3, "portrait", "")
_app.processEvents()
_after = panel_state()
check("★ ③ 转换完成 → 面板总页数立刻变成 3（旧版这里还是 0，就是用户报的 BUG）",
      _after["total_pages"] == 3, f"total_pages={_after['total_pages']}")
check("★ ③ 「排除末尾空白页」跟着最新页数重算（文案 / 可用性不再停在 1 页口径）",
      (_after["blank_enabled"] is True and f"（{_expected_tail} 页）" in _after["blank_text"])
      if _expected_tail > 0 else (_after["blank_text"] == "排除末尾空白页"),
      f"tail={_expected_tail} text={_after['blank_text']!r} enabled={_after['blank_enabled']}")
check("③ 面板与表格同源：表格页数列也已更新（不是面板单独改）",
      "3" in win._table.item(0, gui.MainWindow.COL_PAGES).text(),
      f"{win._table.item(0, gui.MainWindow.COL_PAGES).text()!r}")

# ── 负例：选区不在这一行 → 面板一个字节都不动 ──
section("④ 负例：选区已换行 → 不拿别的行的事实重画面板")

DOCX_A = os.path.join(_SANDBOX, "转换中的A.docx")
DOCX_B = os.path.join(_SANDBOX, "选中的B.docx")
for _p in (DOCX_A, DOCX_B):
    with open(_p, "wb") as _f:
        _f.write(b"x\n")
jobs2 = seed([PrintJob(file_path=DOCX_A, copies=1, duplex="on", page_count=0, engine="word",
                       display_name="转换中的A.docx"),
              PrintJob(file_path=DOCX_B, copies=1, duplex="off", page_count=5, engine="word",
                       display_name="选中的B.docx")])
select_row(1)                      # 选中 B（5 页）
win._on_table_selection()
_app.processEvents()
_before2 = panel_state()
win._on_convert_finished(0, DOCX_A, PDF3B, 3, "portrait", "")   # A 转完了
_app.processEvents()
_after2 = panel_state()
check("★ ④ 选区在 B 行时，A 行转换完成不重画面板（B 的 5 页口径原样保留）",
      _before2["total_pages"] == 5 and _after2 == _before2,
      f"{_before2} -> {_after2}")

# ── 正例补：选中 A（转换中那一行）时确实刷新 ──
select_row(0)
win._on_table_selection()
_app.processEvents()
check("③ 选中转换中的那一行 → 面板显示 A 的页数（3）",
      panel_state()["total_pages"] == 3, f"{panel_state()}")

# ── 焦点保护：光标停在页码范围里，没提交的输入不能被冲掉 ──
section("⑤ 光标停在页码范围输入框 → 保留没提交的文本")

_node = win._edit_page_range._inputs[0]
_node.setText("2")
check("⑤ 前提：输入框里已有未提交的 '2'", win._edit_page_range.get_ranges() == "2",
      f"{win._edit_page_range.get_ranges()!r}")
_orig_focus = gui.QApplication.focusWidget
try:
    gui.QApplication.focusWidget = staticmethod(lambda: _node)
    win._refresh_edit_panel_after_convert(0)
finally:
    try:
        gui.QApplication.focusWidget = _orig_focus
    except Exception:
        pass
_app.processEvents()
check("★ ⑤ 面板刷新后，未提交的 '2' 仍在（用户输入优先于 job 里的空范围）",
      win._edit_page_range.get_ranges() == "2",
      f"{win._edit_page_range.get_ranges()!r}")

print("\n" + ("=" * 60))
print("结果：" + ("全部通过" if ok_all else "存在失败项"))
try:
    win.close()
except Exception:
    pass
sys.exit(0 if ok_all else 1)
