# -*- coding: utf-8 -*-
"""验收脚本 · Ctrl+Shift+T 打开「📑 标签页管理」窗口（GUI 集成，offscreen）

需求（2026-10-30，用户明确说这个窗口已经存在）：标签页管理窗原先只有一条入口 ——
点标签页那个数字（`_tab_label.mousePressEvent`）。现在加一条全局快捷键 **Ctrl+Shift+T**，
与点击完全等价：**同一个落点** `_show_tab_manager`，不另写窗口。

本脚本钉住四件事：
1. 静态接线：Ctrl+Shift+T 是 ApplicationShortcut、指向 `_show_tab_manager`；窗口标题仍是
   「📑 标签页管理」；帮助 → 快捷键表列了它；**不再多出一个 `_seq` 循环**
   （`QShortcut(QKeySequence(_seq), self)` 仍恰好 2 处 —— existing
   tests/verify_tab_arrow_shortcut.py 按次数钉住这个写法）。
2. 真实按键：`QTest.keyClick` 走 Qt 的 QShortcut 派发路径（不是直接调方法），
   用 QTimer 在模态 `exec()` 的嵌套事件循环里抓窗口、断言标题与模态性后关掉。
3. 可重复：关掉后再按一次仍能打开、不抛异常、重入标记被清干净；窗口**开着**时再触发
   （连按 / 连点数字）不会叠出第二个模态窗（重入守卫）。
4. 零回归：Ctrl+T 仍新建标签页；Ctrl+Shift+T 本身**不**新建标签页、不冻结标签页。

    python tests/verify_tab_manager_shortcut.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_tabmgr_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QShortcut  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

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
TITLE = "📑 标签页管理"


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


SRC = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()

print("\n① 静态接线（源码口径）")

check("注册了 Ctrl+Shift+T 一个 QShortcut（独立注册，不并进 _seq 循环）",
      SRC.count('QShortcut(QKeySequence("Ctrl+Shift+T"), self)') == 1,
      "缺则该快捷键根本没接上")

check("它是 ApplicationShortcut（焦点在表格/日志区也生效）",
      'self._shortcut_tab_manager = QShortcut(QKeySequence("Ctrl+Shift+T"), self)\n'
      '        self._shortcut_tab_manager.setContext(Qt.ApplicationShortcut)' in SRC)

check("★ 绑到同一个方法：_show_tab_manager（与点标签数字完全等价，不另写窗口）",
      "self._shortcut_tab_manager.activated.connect(self._show_tab_manager)" in SRC
      and "self._tab_label.mousePressEvent = lambda e: self._show_tab_manager()" in SRC)

check("窗口标题仍是「📑 标签页管理」（用户说的那个已有窗口）",
      'dlg.setWindowTitle("📑 标签页管理")' in SRC)

check("重入守卫存在（模态窗开着时再触发不会叠第二个窗口）",
      "if _open.isVisible():" in SRC and "self._tab_manager_dialog = None" in SRC,
      "连着按两次快捷键会叠两层模态窗，按 Esc 只关最上面那层")

check("exec() 被 try/finally 包住（异常/提前退出也要清重入标记）",
      "try:\n            dlg.exec()\n        finally:" in SRC)

check("帮助 → 快捷键表列出 Ctrl+Shift+T",
      "<tr><td><b>Ctrl+Shift+T</b></td>" in SRC)

check("★ 零回归：_seq 循环写法仍恰好 2 处（Ctrl+T/N 与 ←/→，"
      "verify_tab_arrow_shortcut.py 按次数钉住）",
      SRC.count("QShortcut(QKeySequence(_seq), self)") == 2,
      "实际 " + str(SRC.count("QShortcut(QKeySequence(_seq), self)")))

check("Ctrl+T/N 与 Ctrl+W 注册仍在",
      'for _seq in ("Ctrl+T", "Ctrl+N"):' in SRC
      and 'QShortcut(QKeySequence("Ctrl+W"), self)' in SRC)

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
check("窗口上能找到 Ctrl+Shift+T 这个 QShortcut",
      "Ctrl+Shift+T" in seqs, "实际得到 " + str(sorted(seqs)))
check("它是 ApplicationShortcut，且就是 self._shortcut_tab_manager",
      all(s.context() == Qt.ApplicationShortcut for s in seqs.get("Ctrl+Shift+T", []))
      and getattr(win, "_shortcut_tab_manager", None) is not None
      and win._shortcut_tab_manager.key().toString() == "Ctrl+Shift+T")
check("既有快捷键没被挤掉（Ctrl+T / Ctrl+N / Ctrl+W / Left / Right 仍在）",
      all(k in seqs for k in ("Ctrl+T", "Ctrl+N", "Ctrl+W", "Left", "Right")),
      str(sorted(seqs)))


def make_job(tag):
    return PrintJob(file_path=os.path.join(_SANDBOX, tag + ".pdf"), copies=1, duplex="off",
                    page_count=1, orientation="portrait", engine="word",
                    display_name=tag + ".pdf")


def seed(n=2):
    tabs = {}
    for i in range(1, n + 1):
        t = TabSettings()
        t.jobs = [make_job("t%d" % i)]
        tabs[str(i)] = t
    win._config.tabs = tabs
    win._current_tab = "1"
    win._config.active_tab = "1"
    win._rebuild_table()
    win._refresh_tab_display()
    app.processEvents()


def find_tab_manager():
    """当前可见的标签页管理窗（可能有多个 —— 守卫失效时正是这里能抓到第二个）。"""
    out = []
    for top in QApplication.topLevelWidgets():
        if isinstance(top, QDialog):
            try:
                if top.isVisible() and top.windowTitle() == TITLE:
                    out.append(top)
            except RuntimeError:
                continue
    return out


def activate_main():
    """offscreen 平台下模态窗关掉后活动窗口会丢 —— ApplicationShortcut 只在应用有活动窗口
    时派发，所以每次按键前重新把主窗口激活（不这么做只有第一次按键有效）。"""
    win.raise_()
    win.activateWindow()
    win.setFocus()
    app.processEvents()
    QTest.qWaitForWindowExposed(win, 2000)


def press_ctrl_shift_t(on_seen=None, timeout_ms=4000):
    """真按键触发 Ctrl+Shift+T。

    `_show_tab_manager` 走模态 `dlg.exec()`（嵌套事件循环）⇒ QTest.keyClick 不会马上返回，
    所以用 QTimer 在嵌套循环里轮询窗口；on_seen(dlg) 里做断言/关闭。
    返回 {"opened": bool, "title": str, "modal": bool, "closed": bool, "error": str}
    """
    state = {"opened": False, "title": "", "modal": False, "closed": False,
             "error": "", "ticks": 0}
    deadline_ticks = max(1, timeout_ms // 40)

    def _probe():
        state["ticks"] += 1
        if state["opened"]:
            return
        found = find_tab_manager()
        if not found:
            if state["ticks"] < deadline_ticks:
                QTimer.singleShot(40, _probe)
            return
        dlg = found[0]
        state["opened"] = True
        state["title"] = dlg.windowTitle()
        state["modal"] = dlg.isModal()
        try:
            if on_seen is not None:
                on_seen(dlg)
            else:
                dlg.reject()
            state["closed"] = True
        except Exception as exc:            # 断言/关闭里的异常不能让嵌套循环卡死
            state["error"] = f"{type(exc).__name__}: {exc}"
            try:
                dlg.reject()
            except Exception:
                pass

    app.processEvents()
    activate_main()
    app.processEvents()
    QTimer.singleShot(40, _probe)
    try:
        QTest.keyClick(win, Qt.Key_T,
                       Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    except Exception as exc:
        state["error"] = state["error"] or f"{type(exc).__name__}: {exc}"
    app.processEvents()
    return state


seed(2)
r1 = press_ctrl_shift_t()
check("Ctrl+Shift+T 弹出窗口，标题是「📑 标签页管理」",
      r1["opened"] and r1["title"] == TITLE,
      f"opened={r1['opened']} title={r1['title']!r} err={r1['error']}")
check("窗口是模态（与点标签数字弹出来的是同一个窗口）", r1["modal"])
check("关掉之后重入标记清空（否则下一次会被守卫误判成“已经开着”）",
      getattr(win, "_tab_manager_dialog", None) is None)
check("Ctrl+Shift+T 本身不新建标签页（标签页数不变）",
      len(win._config.tabs) == 2, f"n={len(win._config.tabs)}")
check("Ctrl+Shift+T 不冻结标签页",
      not getattr(win._config.tabs.get("1"), "frozen", False))

print("\n③ 可重复打开 / 关闭（再按一次不报错）")
seed(2)
r2 = press_ctrl_shift_t()
check("第二次按 Ctrl+Shift+T 仍能打开、标题一致",
      r2["opened"] and r2["title"] == TITLE and not r2["error"],
      f"opened={r2['opened']} title={r2['title']!r} err={r2['error']}")
check("第一次会话残留的对话框不会再出现（不会越点越多）",
      len(find_tab_manager()) == 0, "剩余 " + str(len(find_tab_manager())))

r3 = press_ctrl_shift_t()
check("第三次依然正常（连续开关无异常、无泄漏）",
      r3["opened"] and r3["title"] == TITLE and getattr(win, "_tab_manager_dialog", None) is None,
      f"tabs={sorted(win._config.tabs)}")

print("\n④ 重入守卫：窗口开着时再触发不会叠出第二个模态窗")
reenter = {}


def _on_seen_reenter(dlg):
    reenter["before"] = len(find_tab_manager())
    reenter["same_obj"] = getattr(win, "_tab_manager_dialog", None) is dlg
    # 直接调一次（等价于连按第二次快捷键 / 连点标签数字）：守卫应抬窗后立即返回
    win._show_tab_manager()
    reenter["after"] = len(find_tab_manager())
    reenter["ref_after"] = getattr(win, "_tab_manager_dialog", None) is dlg
    dlg.reject()


r4 = press_ctrl_shift_t(on_seen=_on_seen_reenter)
check("窗口开着时再触发：可见的管理窗数量仍是 1（没有叠第二层）",
      reenter.get("before") == 1 and reenter.get("after") == 1,
      f"before={reenter.get('before')} after={reenter.get('after')} err={r4['error']}")
check("重入期间 self._tab_manager_dialog 始终指向同一个已开窗口",
      bool(reenter.get("same_obj")) and bool(reenter.get("ref_after")))
check("守卫返回后窗口仍可正常关闭、标记清空",
      r4["closed"] and getattr(win, "_tab_manager_dialog", None) is None)

print("\n⑤ 零回归：Ctrl+T 仍新建标签页")
seed(2)
activate_main()
try:
    QTest.keyClick(win, Qt.Key_T, Qt.KeyboardModifier.ControlModifier)
    app.processEvents()
except Exception as exc:
    check("Ctrl+T 按键本身不抛异常", False, f"{type(exc).__name__}: {exc}")
check("Ctrl+T 仍新建标签页（2 → 3，并切到新页）",
      len(win._config.tabs) == 3 and win._current_tab == "3",
      f"tabs={sorted(win._config.tabs)} current={win._current_tab}")

print("\n⑥ 关闭后的收尾（避免污染后续脚本的 QApplication）")
try:
    for d in find_tab_manager():
        d.reject()
    win.close()
    app.processEvents()
except Exception:
    pass

print("\n" + ("PASS 全部通过" if ok_all else "FAIL 有失败项"))
sys.exit(0 if ok_all else 1)
