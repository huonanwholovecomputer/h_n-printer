# -*- coding: utf-8 -*-
"""验收脚本 · Ctrl+T / Ctrl+N 新建标签页（GUI 集成，offscreen）

需求（2026-10）：「＋」按钮之外，再加两条键盘入口 —— Ctrl+T 与 Ctrl+N，
行为与「在最后一页点 ＋」完全一致（新建空标签页并切过去，原标签页原样不动）。

本脚本同时钉住三件事：
1. 静态接线：两个 QKeySequence 都是 ApplicationShortcut 且指向 _on_shortcut_new_tab；
   _switch_tab 的新建分支与标签页管理窗的「＋ 新建标签页」都收敛到同一个 _new_tab；
   帮助 → 快捷键表里有这一行（否则没人知道有这个键）。
2. 真实按键：QTest.keyClick 打进窗口，走 Qt 的 QShortcut 派发路径（不是直接调方法）。
3. 语义：新键 = 最大数字 key + 1、新标签页为空且被切到、**原标签页的文件/订单状态不变**、
   配置已落盘；顺带确认 Ctrl+C/D/V 没被覆盖。

    python tests/verify_new_tab_shortcut.py     # exit 0 = 全绿
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

_SANDBOX = tempfile.mkdtemp(prefix="hn_newtab_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QShortcut  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

import converter as _converter  # noqa: E402
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}

import gui as _gui  # noqa: E402
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


SRC = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()

print("\n① 静态接线（源码口径）")

check("注册了 Ctrl+T 与 Ctrl+N 两个序列",
      'for _seq in ("Ctrl+T", "Ctrl+N"):' in SRC
      # 2026-10-27：←/→ 切页也用同一个 _seq 构造写法（另一处循环），
      # 所以只能数「Ctrl+T/N 这一处」的循环头，不能数全局的构造次数。
      and SRC.count('for _seq in ("Ctrl+T", "Ctrl+N"):') == 1,
      "缺则键盘入口根本没接上")

check("两个 QShortcut 都是 ApplicationShortcut",
      re.search(r"for _seq in \(.Ctrl\+T., .Ctrl\+N.\):\n"
                r"\s+_sc = QShortcut\(QKeySequence\(_seq\), self\)\n"
                r"\s+_sc\.setContext\(Qt\.ApplicationShortcut\)\n"
                r"\s+_sc\.activated\.connect\(self\._on_shortcut_new_tab\)", SRC) is not None,
      "Widget 级上下文在焦点落到表格/日志区时会失效")

check("取号仍只有 _new_tab 与「添加到新标签页」两处（都是 _next_tab_key）",
      len(re.findall(r"self\._next_tab_key\(\)", SRC)) == 2,
      "两处各自算号会把已有标签页顶掉")

check("「＋」/标签页管理窗/快捷键三个入口都收敛到 _new_tab",
      "self._new_tab()\n            return" in SRC
      and "self._new_tab(),\n            _rebuild_dialog_table()," in SRC
      and "def _on_shortcut_new_tab(self):" in SRC
      and "self._new_tab()\n\n    def _on_shortcut_paste" in SRC,
      "有一条入口绕过 _new_tab 就会与其它入口行为不一致")

check("帮助 → 快捷键表列出 Ctrl+T / Ctrl+N",
      "<b>Ctrl+T</b> / <b>Ctrl+N</b></td><td>新建标签页" in SRC)
check("「＋」按钮 tooltip 提示快捷键",
      "新建标签页（Ctrl+T 或 Ctrl+N）" in SRC)

print("\n② 真实按键（offscreen MainWindow + QTest.keyClick）")
win = MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
win.activateWindow()
QTest.qWaitForWindowExposed(win)
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


JOB_A = PrintJob(file_path=os.path.join(_SANDBOX, "a.pdf"), copies=2, duplex="on",
                 page_count=3, orientation="portrait", engine="word", display_name="a.pdf")
JOB_B = PrintJob(file_path=os.path.join(_SANDBOX, "b.pdf"), copies=1, duplex="off",
                 page_count=1, orientation="portrait", engine="word", display_name="b.pdf")
seed([JOB_A, JOB_B])

shorts = win.findChildren(QShortcut)
seqs = {}
for sc in shorts:
    seqs.setdefault(sc.key().toString(), []).append(sc)
check("窗口上能找到 Ctrl+T / Ctrl+N 两个 QShortcut",
      "Ctrl+T" in seqs and "Ctrl+N" in seqs, "实际得到 " + str(sorted(seqs)))
check("两者都是 ApplicationShortcut",
      all(s.context() == Qt.ApplicationShortcut
          for s in seqs.get("Ctrl+T", []) + seqs.get("Ctrl+N", [])))
check("既有快捷键未被覆盖（Ctrl+C / Ctrl+D / Ctrl+V 仍在）",
      all(k in seqs for k in ("Ctrl+C", "Ctrl+D", "Ctrl+V")), str(sorted(seqs)))

jobs_before = [j.file_path for j in win._config.tabs["1"].jobs]
QTest.keyClick(win, Qt.Key_T, Qt.ControlModifier)
app.processEvents()
tabs = win._config.tabs
check("Ctrl+T 新建了标签页 2 并切了过去",
      sorted(tabs) == ["1", "2"] and win._current_tab == "2"
      and win._config.active_tab == "2",
      "tabs=" + str(sorted(tabs)) + " current=" + str(win._current_tab))
check("新标签页是空页（不带旧页的文件）", len(tabs.get("2").jobs) == 0)
check("原标签页 1 的文件原样保留",
      [j.file_path for j in tabs["1"].jobs] == jobs_before and len(jobs_before) == 2)
check("新页 tab_label 显示 2", win._tab_label.text() == "2")
check("日志写了新建记录", "新建标签页 2" in win._log_text.toPlainText())

QTest.keyClick(win, Qt.Key_N, Qt.ControlModifier)
app.processEvents()
check("Ctrl+N 也新建页（3 号，不覆盖 1/2）",
      sorted(win._config.tabs) == ["1", "2", "3"] and win._current_tab == "3",
      "tabs=" + str(sorted(win._config.tabs)) + " current=" + str(win._current_tab))

saved = paths.config_path()
raw = open(saved, encoding="utf-8").read() if os.path.exists(saved) else ""
check("配置已落盘且包含三个标签页",
      '"3"' in raw and '"2"' in raw, saved)

print("\n③「＋」按钮与新建语义（回归）")
win._switch_tab(1)
app.processEvents()
check("在最后一页点 ＋ 仍是新建下一号（4）",
      sorted(win._config.tabs) == ["1", "2", "3", "4"] and win._current_tab == "4",
      "tabs=" + str(sorted(win._config.tabs)))
win._switch_tab(-1)
app.processEvents()
# 往回切不新建；顺手把右侧的空标签页收掉（既有 F3 行为），4 号空页应被清理而 1~3 保留
check("往回切不新建，只清掉右侧空标签页（既有行为）",
      win._current_tab == "3" and sorted(win._config.tabs) == ["1", "2", "3"],
      "current=" + str(win._current_tab) + " tabs=" + str(sorted(win._config.tabs)))

win._current_tab = "1"
win._config.active_tab = "1"
win._rebuild_table()
app.processEvents()
win._edit_remark.setFocus()
app.processEvents()
n_before = len(win._config.tabs)
QTest.keyClick(win._edit_remark, Qt.Key_T, Qt.ControlModifier)
app.processEvents()
check("备注输入框有焦点时 Ctrl+T 依然新建（ApplicationShortcut）",
      len(win._config.tabs) == n_before + 1,
      str(n_before) + " → " + str(len(win._config.tabs)))

print("\n" + ("PASS 全部通过" if ok_all else "FAIL 有失败项"))
sys.exit(0 if ok_all else 1)
