# -*- coding: utf-8 -*-
"""验收脚本 · Ctrl+Enter = 开始打印（2026-11-02 用户要求）

需求原话：「顺手加个 Ctrl+Enter 快捷键，用于"开始打印"。」

本脚本钉住四件事：
1. 静态接线：`Ctrl+Return` 与 `Ctrl+Enter`（主键盘 / 小键盘）两个序列都注册、都是
   ApplicationShortcut、都指向 `_on_start_print`（与右下角「▶ 开始打印」按钮**同一个落点**，
   不另写一份启动逻辑）；按钮 tooltip 与「帮助 → 快捷键」表都写明 Ctrl+Enter。
2. 不破坏既有键盘验收：`QShortcut(QKeySequence(_seq), self)` 的出现次数仍是 2
   （`tests/verify_tab_arrow_shortcut.py` 按这个字面量钉次数，新循环换了变量名）。
3. 真实按键：QTest.keyClick 走 Qt 的 QShortcut 派发路径 —— 焦点在表格里、以及焦点在
   「页码范围」输入框里（文本控件）时，Ctrl+Enter 都要启动打印，且**只启动一次**。
4. 语义护栏仍是 `_on_start_print` 自己的：已冻结标签页 ⇒ 只记日志、不启动；
   空标签页 ⇒ 只记日志、不启动。

全程离屏、不出纸、不碰真实 %APPDATA%（`_start_print_worker` 被桩掉，只数调用次数）。

    python tests/verify_start_print_shortcut.py     # exit 0 = 全绿
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

_SANDBOX = tempfile.mkdtemp(prefix="hn_start_shortcut_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QShortcut  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

import converter as _converter  # noqa: E402

_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}

import gui  # noqa: E402
import paths  # noqa: E402
from printer_config import PrintJob, TabSettings  # noqa: E402

gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.Yes)
gui.QMessageBox.warning = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)
gui.QMessageBox.information = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


SRC = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()

# ============================================================
print("\n=== ① 静态接线（源码口径） ===")
# ============================================================
check("注册了 Ctrl+Return 与 Ctrl+Enter 两个序列",
      'for _key in ("Ctrl+Return", "Ctrl+Enter"):' in SRC)
check("两个 QShortcut 都是 ApplicationShortcut 且指向 _on_start_print",
      re.search(r'for _key in \("Ctrl\+Return", "Ctrl\+Enter"\):\n'
                r'\s+_sc = QShortcut\(QKeySequence\(_key\), self\)\n'
                r'\s+_sc\.setContext\(Qt\.ApplicationShortcut\)\n'
                r'\s+_sc\.activated\.connect\(self\._on_start_print\)', SRC) is not None)
check("没有把落点写成别的处理函数（不另写启动逻辑）",
      "self._on_start_print)" in SRC
      and SRC.count("_sc.activated.connect(self._on_start_print)") == 1)
check("★ 既有键盘验收的口径未被打破：QShortcut(QKeySequence(_seq), self) 仍只出现 2 次",
      SRC.count("QShortcut(QKeySequence(_seq), self)") == 2,
      str(SRC.count("QShortcut(QKeySequence(_seq), self)")))
check("「▶ 开始打印」按钮 tooltip 写明 Ctrl+Enter",
      '开始打印当前标签页（快捷键 Ctrl+Enter）' in SRC)
check("帮助 → 快捷键表里有 Ctrl+Enter 一行",
      '"<tr><td><b>Ctrl+Enter</b></td>"' in SRC
      and "开始打印（等同右下角「▶ 开始打印」）" in SRC)

# ============================================================
print("\n=== ② 真实按键（offscreen MainWindow + QTest.keyClick） ===")
# ============================================================
win = gui.MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
win.activateWindow()
QTest.qWaitForWindowExposed(win)
app.processEvents()

seqs = {}
for sc in win.findChildren(QShortcut):
    seqs.setdefault(sc.key().toString(), []).append(sc)
check("窗口上能找到 Ctrl+Return / Ctrl+Enter 两个 QShortcut",
      "Ctrl+Return" in seqs and "Ctrl+Enter" in seqs, "实际得到 " + str(sorted(seqs)))
check("两者都是 ApplicationShortcut（焦点在子控件里也生效）",
      all(s.context() == Qt.ApplicationShortcut
          for s in seqs.get("Ctrl+Return", []) + seqs.get("Ctrl+Enter", [])))
check("既有快捷键未被覆盖（Ctrl+C / Ctrl+T / Ctrl+W 仍在）",
      all(k in seqs for k in ("Ctrl+C", "Ctrl+T", "Ctrl+W")), str(sorted(seqs)))

calls = []


def _fake_start(flat_jobs=None, *a, **k):
    calls.append(len(flat_jobs or []))
    return True


win._start_print_worker = _fake_start


def make_job(tag):
    return PrintJob(file_path=os.path.join(_SANDBOX, tag + ".pdf"), copies=1, duplex="off",
                    page_count=1, orientation="portrait", engine="word",
                    display_name=tag + ".pdf")


def seed(jobs, frozen=False):
    tab = TabSettings()
    tab.jobs = jobs
    tab.frozen = frozen
    win._config.tabs = {"1": tab}
    win._current_tab = "1"
    win._config.active_tab = "1"
    win._rebuild_table()
    win._refresh_tab_display()
    app.processEvents()


def press_ctrl_enter(widget=None):
    QTest.keyClick(widget or win, Qt.Key_Return, Qt.ControlModifier)
    app.processEvents()


seed([make_job("a"), make_job("b")])
calls.clear()
win._table.setFocus()
app.processEvents()
press_ctrl_enter()
check("焦点在文件表格里：Ctrl+Enter 启动打印，且只启动一次",
      calls == [2], f"calls={calls}")

# 文本输入控件里也要生效（Ctrl+Enter 不是文本框的编辑键，不像 ←/→ 要避让）
win._edit_page_range.setFocus()
app.processEvents()
calls.clear()
press_ctrl_enter(win._edit_page_range)
check("焦点在「页码范围」输入框里：Ctrl+Enter 仍然启动打印",
      calls == [2], f"calls={calls}")

# 小键盘 Enter（KeypadModifier + Key_Enter）
calls.clear()
QTest.keyClick(win, Qt.Key_Enter, Qt.ControlModifier | Qt.KeypadModifier)
app.processEvents()
check("小键盘 Enter（Ctrl+小键盘回车）也能启动打印",
      calls == [2], f"calls={calls}")

# ============================================================
print("\n=== ③ 语义护栏仍是 _on_start_print 自己的 ===")
# ============================================================
seed([make_job("f")], frozen=True)
calls.clear()
win._table.setFocus()
_app_logs = []
win._log = lambda m, *a, **k: _app_logs.append(str(m))
press_ctrl_enter()
check("★ 已冻结标签页：不启动打印，只记「已固定」日志",
      calls == [] and any("已固定" in m for m in _app_logs),
      f"calls={calls} logs={_app_logs[-1:] if _app_logs else []}")

seed([])
calls.clear()
_app_logs.clear()
win._table.setFocus()
press_ctrl_enter()
check("★ 空标签页：不启动打印，只记「没有文件」日志",
      calls == [] and any("没有文件" in m for m in _app_logs),
      f"calls={calls} logs={_app_logs[-1:] if _app_logs else []}")

check("Ctrl+Enter 未抢走 Ctrl+C（复制合计仍可用）",
      "self._shortcut_copy.activated.connect(self._on_shortcut_copy_total)" in SRC)

win.close()
print("\n" + ("✅ 全部通过" if ok_all else "❌ 存在失败项"))
sys.exit(0 if ok_all else 1)
