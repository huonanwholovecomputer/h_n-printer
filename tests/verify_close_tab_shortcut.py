# -*- coding: utf-8 -*-
"""验收脚本 · Ctrl+W 关闭当前标签页（GUI 集成，offscreen）

需求（2026-10-27）：「Ctrl+W 快捷键，用于快速删除当前标签页（如果当前标签页中的任务未完成，
需要弹出放弃打印或者有多少文件未打印的确认）」。用户拍板的两条口径：
  · 只剩一个标签页时 → 保留该页、清空其中的文件（"至少保留一个标签页"仍成立）；
  · 当前页的文件正在打印 → 硬拦（边打边删会立刻向后端报放弃，而纸还在出纸槽里）。

本脚本钉住四件事：
1. 静态接线：Ctrl+W 是 ApplicationShortcut、指向 _on_shortcut_close_tab、落点唯一
   （_close_current_tab）、帮助 → 快捷键表里有这一行、攒批纸叠的任务经 skip_jobs 不被放弃。
2. 真实按键：QTest.keyClick 打进窗口，走 Qt 的 QShortcut 派发路径（不是直接调方法）。
3. 语义：全部已打印完成 → 静默关闭并切到**右侧邻居**；有未打印 → 先确认（文案写明几个
   文件没打印），选「否」原样保留；只剩一个标签页 → 清空但不删页、key 不重编号；
   正在打印 → 硬拦且一个文件都不动（按 job 对象身份比对，别的页在打印不拦这一页）。
4. 联动：关页时放弃窗外未打印任务 → 已打过的跳过、攒批纸叠里的跳过；
   排队等纸叠放行的请求随之摘掉（否则纸叠打完还会把那单印出来）。

    python tests/verify_close_tab_shortcut.py     # exit 0 = 全绿
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

_SANDBOX = tempfile.mkdtemp(prefix="hn_ctrlw_")
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

import duplex_stack as _duplex_stack  # noqa: E402
import gui as _gui  # noqa: E402

CALLS = {"question": [], "warning": []}
ANSWER = {"value": _gui.QMessageBox.No}


def _fake_question(parent, title, text, *a, **k):
    CALLS["question"].append((title, text))
    return ANSWER["value"]


def _fake_warning(parent, title, text, *a, **k):
    CALLS["warning"].append((title, text))
    return _gui.QMessageBox.Ok


_gui.QMessageBox.question = staticmethod(_fake_question)
_gui.QMessageBox.warning = staticmethod(_fake_warning)
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

check("注册了 Ctrl+W（ApplicationShortcut → _on_shortcut_close_tab）",
      re.search(r"_shortcut_ctrl_w = QShortcut\(QKeySequence\('Ctrl\+W'\), self\)\n"
                r"\s+self\._shortcut_ctrl_w\.setContext\(Qt\.ApplicationShortcut\)\n"
                r"\s+self\._shortcut_ctrl_w\.activated\.connect\(self\._on_shortcut_close_tab\)",
                SRC.replace('"', "'")) is not None,
      "缺则键盘入口根本没接上")
check("落点唯一（_close_current_tab 只被 _on_shortcut_close_tab 调）",
      SRC.count("def _close_current_tab") == 1
      and SRC.count("self._close_current_tab()") == 1)
check("帮助 → 快捷键表列出 Ctrl+W",
      "<b>Ctrl+W</b></td><td>关闭当前标签页（未打印的先确认）" in SRC)
check("未打印先确认（标题与未打印个数文案）",
      '"放弃打印？"' in SRC and "个文件还没打印完" in SRC
      and "QMessageBox.Yes | QMessageBox.No, QMessageBox.No" in SRC)
check("正在打印硬拦（_tab_jobs_in_flight + 警告窗）",
      "def _tab_jobs_in_flight" in SRC and 'self, "正在打印",' in SRC)
check("只剩一个标签页只清空不删页",
      "只剩一个标签页，已清空其文件" in SRC and "tab.frozen = False" in SRC)
check("攒批纸叠里的任务不被放弃（skip 逐层透传）",
      "def _abandon_tab_jobs(self, tab_entry, skip=())" in SRC
      and 'skip_jobs=()' in SRC
      and "self._abandon_tab_jobs(tab_entry, skip=skip_jobs)" in SRC
      and "reason=\"Ctrl+W 关闭标签页\", skip_jobs=keep_jobs" in SRC)
check("排队等放行的请求随关页摘掉",
      "def _drop_parked_requests_for_tab" in SRC
      and "self._drop_parked_requests_for_tab(key, jobs)" in SRC)

print("\n② 真实按键（offscreen MainWindow）")
win = MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
win.activateWindow()
QTest.qWaitForWindowExposed(win)
app.processEvents()

shorts = win.findChildren(QShortcut)
seqs = {}
for sc in shorts:
    seqs.setdefault(sc.key().toString(), []).append(sc)
check("窗口上能找到 Ctrl+W 的 QShortcut", "Ctrl+W" in seqs, "实际得到 " + str(sorted(seqs)))
check("是 ApplicationShortcut（焦点在输入框时也生效）",
      bool(seqs.get("Ctrl+W"))
      and all(s.context() == Qt.ApplicationShortcut for s in seqs["Ctrl+W"]))
check("既有快捷键未被覆盖（Ctrl+C / Ctrl+D / Ctrl+T / Ctrl+V 仍在）",
      all(k in seqs for k in ("Ctrl+C", "Ctrl+D", "Ctrl+T", "Ctrl+V")), str(sorted(seqs)))


def job(name, sent=False, task_id=0, order_id=0, order_number=""):
    j = PrintJob(file_path=os.path.join(_SANDBOX, name), copies=1, duplex="off",
                 page_count=1, orientation="portrait", engine="word", display_name=name)
    j.sent = sent
    j.task_id = task_id
    j.order_id = order_id
    j.order_number = order_number
    return j


def mk_tab(jobs, frozen=False):
    t = TabSettings()
    t.jobs = list(jobs)
    t.frozen = frozen
    return t


def seed(tabs, current):
    win._config.tabs = tabs
    win._current_tab = current
    win._config.active_tab = current
    win._rebuild_table()
    win._refresh_tab_display()
    app.processEvents()


def reset():
    CALLS["question"].clear()
    CALLS["warning"].clear()
    ANSWER["value"] = _gui.QMessageBox.No


def press():
    QTest.keyClick(win, Qt.Key_W, Qt.ControlModifier)
    app.processEvents()


class _Recorder:
    """假云端客户端：只记 abandon 调用（不联网）。"""

    def __init__(self):
        self.abandoned = []
        self.removed_md5 = []

    def abandon_order_to_server(self, oid):
        self.abandoned.append(oid)

    def abandon_reserved_order(self, *a, **k):
        self.abandoned.append(a[0] if a else "?")

    def remove_cached_pdf(self, md5):
        self.removed_md5.append(md5)


print("\n③ 已打印完成 → 静默关闭，切到右侧邻居")
seed({"1": mk_tab([job("a.pdf", sent=True), job("b.pdf", sent=True)]),
      "2": mk_tab([job("c.pdf", sent=True)])}, "1")
reset()
press()
check("不弹确认（全部已完成）", not CALLS["question"], str(CALLS["question"]))
check("标签页 1 已删除、只剩一页并重编号",
      sorted(win._config.tabs) == ["1"], str(sorted(win._config.tabs)))
check("留在右侧邻居那一页（不是跳回别的页）",
      win._current_tab == "1"
      and [j.display_name for j in win._config.tabs["1"].jobs] == ["c.pdf"],
      "current=" + str(win._current_tab))
check("日志写明是 Ctrl+W 关的", "Ctrl+W 关闭标签页" in win._log_text.toPlainText())

print("\n④ 有未打印文件 → 先确认；选「否」原样保留")
seed({"1": mk_tab([job("d.pdf"), job("e.pdf", sent=True)]),
      "2": mk_tab([job("f.pdf", sent=True)])}, "1")
reset()
press()
check("弹了且只弹了 1 个确认", len(CALLS["question"]) == 1, str(len(CALLS["question"])))
_q = CALLS["question"][0] if CALLS["question"] else ("", "")
check("标题是「放弃打印？」", _q[0] == "放弃打印？", _q[0])
check("文案写明有几个文件没打印（1 / 共 2）",
      "1 个文件还没打印完" in _q[1] and "共 2 个文件" in _q[1], _q[1].replace("\n", " / "))
check("默认按钮是「否」（不误删）",
      "QMessageBox.Yes | QMessageBox.No, QMessageBox.No" in SRC)
check("选「否」→ 标签页与文件原样保留",
      sorted(win._config.tabs) == ["1", "2"] and len(win._config.tabs["1"].jobs) == 2
      and win._current_tab == "1",
      str(sorted(win._config.tabs)) + " current=" + str(win._current_tab))
check("日志写了取消", "已取消关闭标签页" in win._log_text.toPlainText())

print("\n⑤ 选「是」→ 删除并放弃未打印任务（已打过的跳过）")
rec = _Recorder()
win._cloud_client = rec
seed({"1": mk_tab([job("g1.pdf", sent=True)]),
      "2": mk_tab([job("g2.pdf", order_id=11), job("g3.pdf", sent=True)]),
      "3": mk_tab([job("g4.pdf", sent=True)])}, "2")
reset()
ANSWER["value"] = _gui.QMessageBox.Yes
press()
check("删掉了当前页，切到右侧邻居（原 3 号重编号为 2）",
      sorted(win._config.tabs) == ["1", "2"] and win._current_tab == "2"
      and [j.display_name for j in win._config.tabs["2"].jobs] == ["g4.pdf"],
      str(sorted(win._config.tabs)) + " current=" + str(win._current_tab))
check("未打印的云端订单已上报放弃（11）", rec.abandoned == [11], str(rec.abandoned))
check("已打印完成的没被放弃", 11 in rec.abandoned and len(rec.abandoned) == 1)
check("被删页里的文件已从配置消失",
      all("g2.pdf" != j.display_name
          for t in win._config.tabs.values() for j in t.jobs))

print("\n⑥ 正在打印 → 硬拦（一个文件都不动）")
seed({"1": mk_tab([job("h.pdf")]), "2": mk_tab([job("i.pdf", sent=True)])}, "1")
reset()


class _FakeWorker:
    def __init__(self, jobs):
        self._jobs = list(jobs)
        self.running = True

    def isRunning(self):
        return self.running


win._worker = _FakeWorker(win._config.tabs["1"].jobs)
press()
check("弹了「正在打印」警告", bool(CALLS["warning"]) and CALLS["warning"][0][0] == "正在打印",
      str(CALLS["warning"]))
check("连放弃确认都不弹（顺序在前）", not CALLS["question"])
check("标签页与文件一个都没动",
      sorted(win._config.tabs) == ["1", "2"] and len(win._config.tabs["1"].jobs) == 1
      and win._current_tab == "1")
check("警告文案点明账纸会对不上",
      "账与纸会对不上" in (CALLS["warning"][0][1] if CALLS["warning"] else ""))

print("\n⑦ 按 job 对象身份比对：别的页在打印不拦这一页")
seed({"1": mk_tab([job("j1.pdf", sent=True)]),
      "2": mk_tab([job("j2.pdf", sent=True)])}, "1")
reset()
ANSWER["value"] = _gui.QMessageBox.Yes
win._worker = _FakeWorker(win._config.tabs["2"].jobs)   # 打的是 2 号页
press()
check("别的页在打印 → 当前页照常关闭",
      not CALLS["warning"] and sorted(win._config.tabs) == ["1"]
      and [j.display_name for j in win._config.tabs["1"].jobs] == ["j2.pdf"],
      str(CALLS["warning"]))
win._worker = None

print("\n⑧ 只剩一个标签页 → 清空文件但不删页（key 不重编号）")
seed({"2": mk_tab([job("k.pdf"), job("l.pdf", sent=True)], frozen=True)}, "2")
reset()
ANSWER["value"] = _gui.QMessageBox.Yes
press()
check("弹了确认，且文案说的是「清空这个标签页的文件」",
      len(CALLS["question"]) == 1 and "清空这个标签页的文件" in CALLS["question"][0][1],
      CALLS["question"][0][1].replace("\n", " / ") if CALLS["question"] else "")
check("标签页保留（key 仍是 2，没有被重编号成 1）",
      sorted(win._config.tabs) == ["2"] and win._current_tab == "2",
      str(sorted(win._config.tabs)) + " current=" + str(win._current_tab))
check("文件已清空", len(win._config.tabs["2"].jobs) == 0)
check("冻结标记已复位（空页不该还是 🔒）", win._config.tabs["2"].frozen is False)
check("日志写明保留标签页", "已清空其文件（标签页 2 保留）" in win._log_text.toPlainText())

print("\n⑨ 只剩一个空标签页 → 无动作、不弹窗")
seed({"1": mk_tab([])}, "1")
reset()
press()
check("不弹任何窗", not CALLS["question"] and not CALLS["warning"])
check("标签页仍在", sorted(win._config.tabs) == ["1"])
check("日志写明无需关闭", "无需关闭" in win._log_text.toPlainText())

print("\n⑩ 攒批纸叠里的任务不被放弃（只打了第 1 趟 = 半成品）")
rec2 = _Recorder()
win._cloud_client = rec2
_j_stack = job("m.pdf", task_id=777)
seed({"1": mk_tab([_j_stack]), "2": mk_tab([job("n.pdf", sent=True)])}, "1")
win._stack.entries.append(_duplex_stack.StackEntry(
    seq=1, title="t", tab_key="1", tasks=[{"task_id": 777}], sheet_count=1))
reset()
ANSWER["value"] = _gui.QMessageBox.Yes
try:
    press()
    check("文案点出「攒批纸叠里」的保留口径",
          "攒批纸叠里" in (CALLS["question"][0][1] if CALLS["question"] else ""))
    check("攒批里的任务没被上报放弃",
          rec2.abandoned == [], "实际 " + str(rec2.abandoned))
    check("页面照常关掉", sorted(win._config.tabs) == ["1"], str(sorted(win._config.tabs)))
finally:
    win._stack.entries.clear()

print("\n⑪ 排队等纸叠放行的请求随关页摘掉（否则打完还会印出来）")
_parked = job("o.pdf", task_id=888)
seed({"1": mk_tab([_parked]), "2": mk_tab([job("p.pdf", sent=True)])}, "1")
win._stack_wait[:] = [{"jobs": [_parked], "tab_key": "1", "reason": "纸叠未完成"}]
reset()
ANSWER["value"] = _gui.QMessageBox.Yes
press()
check("排队请求已摘掉", win._stack_wait == [], str(win._stack_wait))
check("日志写明了摘掉的数量", "已摘掉 1 个排队等放行的文件" in win._log_text.toPlainText())

print("\n⑫ 回归：Ctrl+T 仍新建、Ctrl+W 不误伤配置落盘")
seed({"1": mk_tab([])}, "1")
QTest.keyClick(win, Qt.Key_T, Qt.ControlModifier)
app.processEvents()
check("Ctrl+T 仍新建标签页 2", sorted(win._config.tabs) == ["1", "2"] and win._current_tab == "2",
      str(sorted(win._config.tabs)))
QTest.keyClick(win, Qt.Key_W, Qt.ControlModifier)
app.processEvents()
check("Ctrl+W 关掉刚建的空页、回到 1 号",
      sorted(win._config.tabs) == ["1"] and win._current_tab == "1",
      str(sorted(win._config.tabs)) + " current=" + str(win._current_tab))
_raw = open(paths.config_path(), encoding="utf-8").read()
check("配置已落盘且只剩一页", '"tabs"' in _raw and '"2"' not in _raw.split('"tabs"')[1][:40],
      paths.config_path())

win._cloud_client = None
print("\n" + ("PASS 全部通过" if ok_all else "FAIL 有失败项"))
sys.exit(0 if ok_all else 1)
