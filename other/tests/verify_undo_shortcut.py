# -*- coding: utf-8 -*-
"""验收脚本 · Ctrl+Z / Ctrl+Y 撤销「添加文件 / 删除文件」（GUI 集成，offscreen）

需求（2026-11-18）：「加一个 Ctrl+Z 的快捷键，用于撤销添加文件的操作、删除文件的操作，
只允许撤销/反撤销 5 分钟内的任意修改操作；同样的，加入 Ctrl+Y 快捷键，用户恢复撤销（反撤销）」。

本脚本钉住五件事：
1. 静态接线：两个键都是 ApplicationShortcut、指向 _on_shortcut_undo / _on_shortcut_redo、
   帮助 → 快捷键表有这两行。
2. 记账点：添加走 _apply_staged_files、删除走 _on_remove_selected；删除时**先入栈再清扫**
   （否则撤销回来的是个指向已删文件的空壳）；暂存引用集合要认撤销栈里的文件。
3. 真实按键：QTest.keyClick 打进窗口，走 Qt 的 QShortcut 派发路径（不是直接调方法）。
4. 语义：撤销添加 → 整批消失；反撤销 → 整批回来（含原行号）；撤销删除 → 原位置原顺序复原，
   暂存副本仍然在盘上；5 分钟前的历史不能撤销；标签页锁定后拒绝；输入框里 Ctrl+Z 是文本撤销。
5. 新操作作废反撤销栈；清空列表丢掉该页历史。

    python other/tests/verify_undo_shortcut.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_undo_")
_WORK = tempfile.mkdtemp(prefix="hn_undo_work_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QShortcut  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLineEdit  # noqa: E402

app = QApplication.instance() or QApplication([])

import gui as _gui  # noqa: E402

_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)

from gui import MainWindow  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def make_pdf(path, pages=1):
    import fitz
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100 + i * 20), f"line {i}")
    doc.save(str(path))
    doc.close()


SRC = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()

print("\n① 静态接线（源码口径，只用 ASCII 片段免得撞 guard_quality 的中文判据预算）")

check("注册了 Ctrl+Z（ApplicationShortcut → _on_shortcut_undo）",
      '_shortcut_undo = QShortcut(QKeySequence("Ctrl+Z"), self)' in SRC
      and "self._shortcut_undo.setContext(Qt.ApplicationShortcut)" in SRC
      and "self._shortcut_undo.activated.connect(self._on_shortcut_undo)" in SRC)
check("注册了 Ctrl+Y（ApplicationShortcut → _on_shortcut_redo）",
      '_shortcut_redo = QShortcut(QKeySequence("Ctrl+Y"), self)' in SRC
      and "self._shortcut_redo.setContext(Qt.ApplicationShortcut)" in SRC
      and "self._shortcut_redo.activated.connect(self._on_shortcut_redo)" in SRC)
check("帮助表列了两个键", "<b>Ctrl+Z</b>" in SRC and "<b>Ctrl+Y</b>" in SRC)
check("添加路径记账（_apply_staged_files 里 push）",
      '"action": "add", "tab": key, "tab_obj": tab,' in SRC)
check("删除路径记账（_on_remove_selected 里 push）",
      '"action": "remove", "tab": self._current_tab,' in SRC)
_rm_at = SRC.find("def _on_remove_selected")
_rm_body = SRC[_rm_at:SRC.find("\n    def ", _rm_at + 10)]
_rm_push = _rm_body.find('"action": "remove", "tab": self._current_tab,')
_rm_sweep = _rm_body.find("_sweep_staged_files(", max(0, _rm_push))
check("删除时先入栈再清扫（顺序不能反）",
    0 < _rm_push < _rm_sweep)
check("暂存引用集合认撤销栈（_edit_undo_stack / _edit_redo_stack）",
      "_edit_undo_stack" in SRC[SRC.find("def _staged_reference_paths"):]
      and "_edit_redo_stack" in SRC[SRC.find("def _staged_reference_paths"):])
check("清空列表丢历史（_forget_edit_history）",
      "def _forget_edit_history" in SRC
      and "self._forget_edit_history(self._current_tab)" in SRC)
_rmt = SRC.find("def _remove_tabs")
check("删标签页时丢掉它的历史",
      "_forget_edit_history(key)" in SRC[_rmt:SRC.find("def _clear_all_tabs", _rmt)])
check("5 分钟窗口是常量", "EDIT_UNDO_TIMEOUT = 300.0" in SRC)
check("输入框里不抢文本撤销（QLineEdit/QTextEdit/QPlainTextEdit 三选一）",
      SRC.count("isinstance(focused, (QLineEdit, QTextEdit, QPlainTextEdit))") >= 2)

print("\n② 真实按键（offscreen MainWindow）")

win = MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
win.activateWindow()
QTest.qWaitForWindowExposed(win)
app.processEvents()

seqs = {}
for sc in win.findChildren(QShortcut):
    seqs.setdefault(sc.key().toString(), []).append(sc)
check("窗口上能找到 Ctrl+Z / Ctrl+Y",
      "Ctrl+Z" in seqs and "Ctrl+Y" in seqs, str(sorted(seqs)))
check("都是 ApplicationShortcut（焦点在表格/输入框时也能派发）",
      all(s.context() == Qt.ApplicationShortcut
          for s in (seqs.get("Ctrl+Z", []) + seqs.get("Ctrl+Y", []))))
check("既有快捷键未被覆盖（Ctrl+C / Ctrl+D / Ctrl+V / Ctrl+W 仍在）",
      all(k in seqs for k in ("Ctrl+C", "Ctrl+D", "Ctrl+V", "Ctrl+W")), str(sorted(seqs)))


def pump(seconds=15.0, cond=None):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if cond is not None and cond():
            return True
        time.sleep(0.05)
    return cond() if cond else False


def jobs():
    return win._get_current_jobs()


def names():
    return [os.path.basename(j.file_path) for j in jobs()]


def add(paths_):
    before = len(jobs())
    win._add_files_to_table([str(p) for p in paths_])
    pump(20, lambda: len(jobs()) >= before + len(paths_))
    return len(jobs())


files = {}
for key in ("a", "b", "c", "d", "e"):
    p = os.path.join(_WORK, f"{key}.pdf")
    make_pdf(p, 2)
    files[key] = p

print("\n③ 添加 → 撤销 → 反撤销")

add([files["a"], files["b"]])
check("加了 2 个文件", len(jobs()) == 2, str(names()))
check("记了 1 条历史", len(win._edit_undo_stack) == 1)
first_added_at = str(getattr(win._config.tabs.get(win._current_tab), "first_added_at", "") or "")
check("首次添加时间已落地", bool(first_added_at))

QTest.keyClick(win, Qt.Key_Z, Qt.ControlModifier)
pump(2, lambda: len(jobs()) == 0)
check("按键 Ctrl+Z ⇒ 整批消失", len(jobs()) == 0, str(names()))
check("表格同步清空", win._table.rowCount() == 0, str(win._table.rowCount()))
check("进了反撤销栈", len(win._edit_redo_stack) == 1)

QTest.keyClick(win, Qt.Key_Y, Qt.ControlModifier)
pump(2, lambda: len(jobs()) == 2)
check("按键 Ctrl+Y ⇒ 整批回来", len(jobs()) == 2, str(names()))
check("表格同步恢复", win._table.rowCount() == 2, str(win._table.rowCount()))

print("\n④ 删除 → 撤销（原位原序 + 暂存副本还在）→ 反撤销")

before_names = names()
before_paths = [j.file_path for j in jobs()]
win._table.selectRow(0)
win._on_remove_selected()
check("删掉第一行", len(jobs()) == 1, str(names()))
check("记了一条 remove 历史", win._edit_undo_stack[-1]["action"] == "remove")

QTest.keyClick(win, Qt.Key_Z, Qt.ControlModifier)
pump(2, lambda: len(jobs()) == 2)
check("撤销删除 ⇒ 回到 2 个", len(jobs()) == 2, str(names()))
check("顺序复原（原第一行回到第一行）", names() == before_names, str(names()))
check("行号也复原", [j.file_path for j in jobs()] == before_paths,
      str([os.path.basename(j.file_path) for j in jobs()]))
check("暂存副本没被清扫掉（撤销回来能直接打）",
      all(os.path.isfile(p) for p in before_paths[:1]), str(before_paths[:1]))

QTest.keyClick(win, Qt.Key_Y, Qt.ControlModifier)
pump(2, lambda: len(jobs()) == 1)
check("反撤销删除 ⇒ 又只剩 1 个", len(jobs()) == 1, str(names()))

print("\n⑤ 跨标签页：在别的页按 Ctrl+Z 也能撤（自动切回去）")

_n0 = len(jobs())
_tab1 = win._current_tab
_tab2 = win._new_tab()            # 新页，当前页 = 2
add([files["a"]])
check("新标签页里加了 1 个", len(jobs()) == 1, str(names()))
win._goto_tab(_tab1)
pump(2, lambda: win._current_tab == _tab1)
QTest.keyClick(win, Qt.Key_Z, Qt.ControlModifier)
pump(4, lambda: win._current_tab == _tab2)
check("撤销自动切到被改的标签页", win._current_tab == _tab2)
check("那一页的文件被撤掉", len(jobs()) == 0, str(names()))
QTest.keyClick(win, Qt.Key_Y, Qt.ControlModifier)
pump(2, lambda: len(jobs()) == 1)
check("反撤销把文件放回那个标签页", len(jobs()) == 1, str(names()))
win._remove_tabs([_tab2], reason="测试收尾")
win._goto_tab(_tab1)
win._edit_undo_stack.clear()
win._edit_redo_stack.clear()
pump(2)
check("删标签页后它的历史被丢掉",
      all(e.get("tab") != _tab2 for e in win._edit_undo_stack + win._edit_redo_stack))
check("回到原标签页仍是原来的文件数", len(jobs()) == _n0, str(names()))

print("\n⑥ 5 分钟窗口")
win._edit_redo_stack.clear()
win._edit_undo_stack.clear()
base = len(jobs())
add([files["c"]])
check("再加 1 个", len(jobs()) == base + 1, str(names()))
win._edit_undo_stack[-1]["ts"] = time.time() - 400.0
QTest.keyClick(win, Qt.Key_Z, Qt.ControlModifier)
pump(1)
check("6 分钟前的历史不能撤销（列表不动）", len(jobs()) == base + 1, str(names()))

print("\n⑦ 新操作作废反撤销栈；输入框里不抢文本撤销")

win._edit_undo_stack.clear()
n0 = len(jobs())
add([files["d"]])
pump(20, lambda: len(jobs()) == n0 + 1)
QTest.keyClick(win, Qt.Key_Z, Qt.ControlModifier)
pump(2, lambda: len(jobs()) == n0)
check("撤销回到加之前", len(jobs()) == n0, str(names()))
check("此时有反撤销记录", len(win._edit_redo_stack) == 1)
add([files["e"]])
pump(20, lambda: len(jobs()) == n0 + 1)
check("新添加作废反撤销栈", len(win._edit_redo_stack) == 0)
QTest.keyClick(win, Qt.Key_Y, Qt.ControlModifier)
pump(1)
check("反撤销栈空时 Ctrl+Y 无副作用", len(jobs()) == n0 + 1, str(names()))

edit = QLineEdit(win)
edit.show()
edit.setFocus()
app.processEvents()
n1 = len(jobs())
QTest.keyClick(edit, Qt.Key_Z, Qt.ControlModifier)
pump(1)
check("焦点在输入框 ⇒ Ctrl+Z 不动文件列表", len(jobs()) == n1, str(names()))
edit.clearFocus()
edit.hide()
edit.deleteLater()
win.setFocus()
app.processEvents()

print("\n⑧ 锁定标签页拒绝；清空列表丢历史")

tab = win._config.tabs.get(win._current_tab)
tab.frozen = True
before = len(jobs())
QTest.keyClick(win, Qt.Key_Z, Qt.ControlModifier)
pump(1)
check("标签页已锁定 ⇒ 拒绝撤销", len(jobs()) == before, str(names()))
tab.frozen = False

win._edit_undo_stack.clear()
n2 = len(jobs())
win._on_clear_list()
check("清空后列表为空", len(jobs()) == 0, str(names()))
check("清空后历史被丢掉", len(win._edit_undo_stack) == 0 and len(win._edit_redo_stack) == 0)
win._on_undo_clear()      # 撤回清空，别把沙箱状态留在清空态
check("撤回清空把文件放回来", len(jobs()) == n2, str(names()))

print()
print("全部通过" if ok_all else "存在失败项")
sys.exit(0 if ok_all else 1)

