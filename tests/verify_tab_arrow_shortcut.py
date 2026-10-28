# -*- coding: utf-8 -*-
"""验收脚本 · ←/→ 切换标签页（GUI 集成，offscreen）

需求（2026-10）：「−/＋」之外再加两条键盘入口 —— ← 上一个标签页、→ 下一个标签页；
**最右侧不新建**（←/→ 是连按的键，误按一下就多一个空标签页）。

本脚本钉住四件事：
1. 静态接线：Left/Right 都是 ApplicationShortcut、都指向 _on_shortcut_switch_tab；
   _switch_tab 的「最后一页」分支必须被 create_at_end 守住（←/→ 不新建，「＋」照旧新建）；
   帮助 → 快捷键表与 −/＋ tooltip 都写明 ←/→。
2. 真实按键：QTest.keyClick 走 Qt 的 QShortcut 派发路径（不是直接调方法）。
3. 语义：→ 在最后一页停住（标签页数不变、current 不变）、← 在第一页停住、不环绕。
4. 焦点纪律：输入类控件（QLineEdit/QPlainTextEdit/QComboBox/QAbstractSpinBox）里 ←/→
   仍归控件（光标/改值）；只读文件表格里 ←/→ 切页。

    python tests/verify_tab_arrow_shortcut.py     # exit 0 = 全绿
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

_SANDBOX = tempfile.mkdtemp(prefix="hn_arrowtab_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QShortcut  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QLabel,  # noqa: E402
                               QLineEdit, QPlainTextEdit, QSpinBox, QTreeWidget)

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

check("注册了 Left 与 Right 两个序列",
      'for _seq, _delta in (("Left", -1), ("Right", 1)):' in SRC
      # 两处 _seq 循环：Ctrl+T/N 一处、←/→ 一处（共用同一个构造写法）
      and SRC.count("QShortcut(QKeySequence(_seq), self)") == 2,
      "缺则键盘入口根本没接上")

check("两个 QShortcut 都是 ApplicationShortcut 且指向 _on_shortcut_switch_tab",
      re.search(r"for _seq, _delta in \(\(.Left., -1\), \(.Right., 1\)\):\n"
                r"\s+_sc = QShortcut\(QKeySequence\(_seq\), self\)\n"
                r"\s+_sc\.setContext\(Qt\.ApplicationShortcut\)\n"
                r"\s+_sc\.activated\.connect\(lambda d=_delta: self\._on_shortcut_switch_tab\(d\)\)",
                SRC) is not None,
      "Widget 级上下文在焦点落到表格/日志区时会失效")

check("_switch_tab 有 create_at_end 开关（默认 True = 「＋」口径）",
      "def _switch_tab(self, delta: int, create_at_end: bool = True):" in SRC)

check("「最后一页」分支被 create_at_end 守住（否则 ←/→ 会在最右侧新建）",
      "if new_idx >= len(tab_keys):\n"
      "            # 在最后一页点 + → 新建标签；←/→ 传 create_at_end=False ⇒ 停住不动\n"
      "            if create_at_end:\n"
      "                self._new_tab()\n"
      "            return" in SRC)

check("快捷键落点唯一（_on_shortcut_switch_tab → _switch_tab(..., create_at_end=False)）",
      len(re.findall(r"self\._switch_tab\(delta, create_at_end=False\)", SRC)) == 1
      and "def _on_shortcut_switch_tab(self, delta: int):" in SRC)

check("豁免清单 = 文本类控件（QLineEdit/QTextEdit/QPlainTextEdit/QAbstractSpinBox）",
      "isinstance(focused, (QLineEdit, QTextEdit, QPlainTextEdit,\n"
      "                                    QAbstractSpinBox))" in SRC)
check("★ 下拉框不豁免（否则焦点停在打印机下拉时 → 会静默换打印机，"
      "且启动首个焦点控件就是它 ⇒ 快捷键默认全失效）",
      "QComboBox, QAbstractSpinBox))" not in SRC)

check("帮助 → 快捷键表列出 ←/→",
      "<tr><td><b>←</b> / <b>→</b></td>" in SRC)
check("−/＋ tooltip 提示 ←/→",
      'setToolTip("上一个标签页（←）")' in SRC
      and "下一个标签页（→，到最右侧不新建）" in SRC)

print("\n② 真实按键（offscreen MainWindow + QTest.keyClick）")
win = MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
win.activateWindow()
QTest.qWaitForWindowExposed(win)
app.processEvents()

seqs = {}
for sc in win.findChildren(QShortcut):
    seqs.setdefault(sc.key().toString(), []).append(sc)
check("窗口上能找到 Left / Right 两个 QShortcut",
      "Left" in seqs and "Right" in seqs, "实际得到 " + str(sorted(seqs)))
check("两者都是 ApplicationShortcut",
      all(s.context() == Qt.ApplicationShortcut
          for s in seqs.get("Left", []) + seqs.get("Right", [])))
check("既有快捷键未被覆盖（Ctrl+C / Ctrl+T / Ctrl+W 仍在）",
      all(k in seqs for k in ("Ctrl+C", "Ctrl+T", "Ctrl+W")), str(sorted(seqs)))


def make_job(tag):
    return PrintJob(file_path=os.path.join(_SANDBOX, tag + ".pdf"), copies=1, duplex="off",
                    page_count=1, orientation="portrait", engine="word",
                    display_name=tag + ".pdf")


def seed(n=3):
    tabs = {}
    for i in range(1, n + 1):
        t = TabSettings()
        t.jobs = [make_job("t%d" % i)]      # 每页都有文件 ⇒ 不会触发空页清理
        tabs[str(i)] = t
    win._config.tabs = tabs
    win._current_tab = "1"
    win._config.active_tab = "1"
    win._rebuild_table()
    win._refresh_tab_display()
    app.processEvents()


def press(key, widget=None):
    QTest.keyClick(widget or win, key)
    app.processEvents()


seed(3)
check("起点：3 个标签页、当前 1、标签数字显示 1",
      sorted(win._config.tabs) == ["1", "2", "3"] and win._current_tab == "1"
      and win._tab_label.text() == "1",
      "tabs=" + str(sorted(win._config.tabs)) + " current=" + str(win._current_tab))

press(Qt.Key_Right)
check("→ 切到标签页 2，标签页数不变",
      win._current_tab == "2" and len(win._config.tabs) == 3
      and win._tab_label.text() == "2",
      "current=" + str(win._current_tab) + " n=" + str(len(win._config.tabs)))

press(Qt.Key_Right)
check("→ 再切到标签页 3（最右侧）",
      win._current_tab == "3", "current=" + str(win._current_tab))

n_before = len(win._config.tabs)
press(Qt.Key_Right)
check("★ 最右侧按 → 停住：不新建、current 不变、标签页数不变",
      win._current_tab == "3" and len(win._config.tabs) == n_before,
      "current=" + str(win._current_tab) + " " + str(n_before) + " → "
      + str(len(win._config.tabs)))
check("最右侧按 → 之后标签数字仍是 3", win._tab_label.text() == "3")

press(Qt.Key_Right)
press(Qt.Key_Right)
check("连按 → 也只会停在最右侧", win._current_tab == "3"
      and len(win._config.tabs) == n_before,
      "current=" + str(win._current_tab) + " n=" + str(len(win._config.tabs)))

press(Qt.Key_Left)
check("← 回到标签页 2", win._current_tab == "2", "current=" + str(win._current_tab))
press(Qt.Key_Left)
check("← 回到第 1 页", win._current_tab == "1", "current=" + str(win._current_tab))
press(Qt.Key_Left)
check("★ 第 1 页按 ← 停住（不环绕到最后一页）", win._current_tab == "1"
      and len(win._config.tabs) == 3, "current=" + str(win._current_tab))

print("\n③「＋」按钮语义不回退")
seed(3)
win._current_tab = "3"          # 站到最后一页，「＋」才走新建分支
win._config.active_tab = "3"
win._refresh_tab_display()
app.processEvents()
win._switch_tab(1)
app.processEvents()
check("_switch_tab(1) 默认仍在最后一页新建（4 号）",
      sorted(win._config.tabs) == ["1", "2", "3", "4"] and win._current_tab == "4",
      "tabs=" + str(sorted(win._config.tabs)) + " current=" + str(win._current_tab))

print("\n④ 焦点纪律")
seed(2)
win._table.setFocus()
app.processEvents()
press(Qt.Key_Right, win._table)
check("焦点在只读文件表格时 → 依然切页（表格不在豁免清单里）",
      win._current_tab == "2", "current=" + str(win._current_tab))

seed(2)
win._edit_remark.setFocus()
app.processEvents()
press(Qt.Key_Right, win._edit_remark)
check("焦点在备注（QPlainTextEdit）时 → 归控件，绝不切页",
      win._current_tab == "1", "current=" + str(win._current_tab))

seed(2)
win._printer_combo.setFocus()
app.processEvents()
press(Qt.Key_Right, win._printer_combo)
check("焦点在打印机下拉（QComboBox，不豁免）时 → 切页而不是静默换打印机",
      win._current_tab == "2", "current=" + str(win._current_tab))

seed(2)
_dlg = QDialog(win)
_dlg.setObjectName("arrowProbeDialog")
_dlg_tree = QTreeWidget(_dlg)          # 攒批队列窗里的那种树
_dlg.show()
app.processEvents()
_dlg_tree.setFocus()
app.processEvents()
_check_focus = QApplication.focusWidget()
if _check_focus is not None and _check_focus.window() is _dlg:
    press(Qt.Key_Right, _dlg_tree)
    check("焦点在另一个顶层窗口（攒批队列那样的 QDialog + QTreeWidget）时绝不切主窗口标签页",
          win._current_tab == "1" and len(win._config.tabs) == 2,
          "current=" + str(win._current_tab))
else:
    check("焦点在另一个顶层窗口时绝不切主窗口标签页（offscreen 下拿不到焦点，按静态口径判）",
          "if focused is not None and focused.window() is not self:\n            return True" in SRC)
_dlg.close()
app.processEvents()

_orig = QApplication.focusWidget
try:
    for cls, expect, name in ((QLineEdit, True, "QLineEdit"),
                              (QSpinBox, True, "QSpinBox"),
                              (QComboBox, False, "QComboBox"),
                              (QPlainTextEdit, True, "QPlainTextEdit"),
                              (QLabel, False, "QLabel")):
        probe = cls(win)         # 挂在主窗口下 ⇒ window() is win，量的是类型清单本身
        QApplication.focusWidget = staticmethod(lambda w=probe: w)
        got = win._focus_takes_arrow_keys()
        check("_focus_takes_arrow_keys(): %s ⇒ %s" % (name, expect), got == expect,
              "得到 " + str(got))
finally:
    QApplication.focusWidget = _orig

check("即使豁免，_switch_tab(create_at_end=False) 本身绝不新建",
      (lambda: (seed(2), setattr(win, "_current_tab", "2"), win._switch_tab(1, create_at_end=False),
                win._current_tab == "2" and len(win._config.tabs) == 2)[-1])(),
      "两层保护：快捷键有豁免清单，_switch_tab 自己也不新建")

print("\n" + ("PASS 全部通过" if ok_all else "FAIL 有失败项"))
sys.exit(0 if ok_all else 1)
