# -*- coding: utf-8 -*-
"""验收：页数还没统计完时不许开始打印（2026-10-30 现场 bug HN20261030-0021）

现场：Word/TXT 刚拖进列表（后台 ConvertWorker 还在转 PDF、数页数）就点了「开始打印」
⇒ 旧代码照常冻结标签页、禁用按钮，然后用 page_count=0 算出 0 元价格，
把一份**页数不全**的订单上报给云端 —— 价格卡住、云端订单也不完整。

本脚本钉住五件事：
  ① 有在跑的统计线程时点开始打印 ⇒ 不冻结标签页、不取号/不上报、不启打印 Worker、
     日志与状态栏有提示、按钮**仍然可用**（machine 要无人值守，绝不能永久禁用）；
  ② 统计线程结束后同一批能正常启动（守卫不是"永久拦"）；
  ③ 页数 0 但**没有**在跑的统计线程（文件损坏/转换失败）⇒ 允许开打，但日志/状态栏
     明确提示会按 0 页计费上报，且**不弹模态框**；
  ④ 静态接线：守卫必须在 `tab.frozen = True` 与 `files_data` 构造**之前**；
  ⑤ 变异可判别：脚本对"守卫挪到冻结之后""删掉按钮恢复""删掉页数未知提示"都会转红。

跑法：PYTHONPATH= PYTHONUTF8=1 <python> tests/verify_page_count_guard.py
"""
import io
import os
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(_HERE)
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_pageguard_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

app = QApplication.instance() or QApplication([])

import printer_config as _pc  # noqa: E402
import gui as _gui  # noqa: E402
import paths  # noqa: E402
from printer_config import PrintJob, TabSettings  # noqa: E402

SRC = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()

# 转换引擎探测在本机可能很慢/弹窗，钉成可用（转换线程本身是假的，不会真跑）
try:
    import converter as _converter
    _converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}
except Exception:
    pass

# 所有模态框都打桩：本脚本要验证"页数未知不弹模态"
MODALS = {"question": [], "warning": [], "information": [], "critical": []}
_gui.QMessageBox.question = staticmethod(
    lambda *a, **k: MODALS["question"].append(a) or _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(
    lambda *a, **k: MODALS["warning"].append(a) or _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(
    lambda *a, **k: MODALS["information"].append(a) or _gui.QMessageBox.Ok)
_gui.QMessageBox.critical = staticmethod(
    lambda *a, **k: MODALS["critical"].append(a) or _gui.QMessageBox.Ok)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if cond:
        print(f"[PASS] {name}" + (f"  -- {detail}" if detail else ""))
    else:
        print(f"[FAIL] {name}" + (f"  -- {detail}" if detail else ""))
        ok_all = False
    return bool(cond)


def body_of(name):
    """抓某个 MainWindow/PrintWorker 方法正文（到下一个同级 def 为止）。"""
    m = re.search(r"\n    def %s\(.*?(?=\n    def |\nclass )" % re.escape(name), SRC, re.S)
    return m.group(0) if m else ""


class FakeConvertWorker:
    """只提供页数守卫读的两样东西：isRunning() 与 _file_path。"""

    def __init__(self, file_path, running=True, finished=False):
        self._file_path = file_path
        self._row = 0
        self._engine = "word"
        self._running = running
        self.finished = finished

    def isRunning(self):  # noqa: N802  (Qt 命名)
        return self._running


# ── 建窗口 ────────────────────────────────────────────────────────────────
win = _gui.MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
win.activateWindow()
QTest.qWaitForWindowExposed(win, 3000)
app.processEvents()
# 订单必须有归属成员：没有任何成员时 `_start_print_worker` 第一步就弹「暂无成员」模态框
# （那是 QMessageBox 实例 + exec()，打桩 staticmethod 挡不住，会把脚本挂死）。
# 本脚本只验页数守卫，成员表钉成常量即可。
win._member_names = lambda: ["测试成员"]
win._stack_state_timer.stop()
for _t in ("_scheduled_check_timer", "_keepalive_timer", "_convert_check_timer"):
    _t_obj = getattr(win, _t, None)
    if _t_obj is not None:
        try:
            _t_obj.stop()
        except Exception:
            pass

DOCX = os.path.join(_SANDBOX, "统计中的文档.docx")
PDF = os.path.join(_SANDBOX, "已统计好的文档.pdf")
for _p in (DOCX, PDF):
    with open(_p, "wb") as _f:
        _f.write(b"%PDF-1.4\n%dummy\n")


def seed(jobs, key="1"):
    """把给定任务塞进一枚新标签页并切到它（不真打印，只走 _start_print_worker 的前半段）。"""
    tab = TabSettings()
    tab.jobs = list(jobs)
    win._config.tabs = {key: tab}
    win._current_tab = key
    win._config.active_tab = key
    win._rebuild_table()
    win._refresh_tab_display()
    win._btn_start.setEnabled(True)
    return tab


def make_job(path, pages, name, duplex="off", task_id=0, order_id=0):
    return PrintJob(file_path=path, copies=1, duplex=duplex, page_count=pages,
                    orientation="portrait", engine="word", display_name=name,
                    task_id=task_id, order_id=order_id)


# 打印 Worker / 取号上报都换成记录器，脚本里一个字节都不往外发
UPLOAD_CALLS = []
CREATED_WORKERS = []
_ORIG_UPLOAD = win._acquire_order_number_and_upload
_ORIG_START = _gui.PrintWorker.start


def _spy_upload(*a, **k):
    UPLOAD_CALLS.append(a)
    return _ORIG_UPLOAD(*a, **k)


def _stub_start(self):
    CREATED_WORKERS.append(self)


win._acquire_order_number_and_upload = _spy_upload
_gui.PrintWorker.start = _stub_start
win._cloud_client = None      # 离线：取号走本地号，不碰网络
win._offline_sync = None      # 不写本地 SQLite（沙箱里也没必要）


def reset_recorders():
    UPLOAD_CALLS.clear()
    CREATED_WORKERS.clear()
    MODALS["question"].clear()
    MODALS["warning"].clear()
    MODALS["information"].clear()
    MODALS["critical"].clear()
    for _t in ("_scheduled_check_timer", "_keepalive_timer"):
        _o = getattr(win, _t, None)
        if _o is not None:
            try:
                _o.stop()
            except Exception:
                pass


LOGS = []
_orig_log = win._log


def _log_spy(msg):
    LOGS.append(str(msg))
    return _orig_log(msg)


win._log = _log_spy

# ══════════════════════════════════════════════════════════════════════════
print("\n=== ① 有在跑的统计线程 ⇒ 不开始打印 ===")
jobs = [make_job(PDF, 3, "已统计好的文档.pdf"),
        make_job(DOCX, 0, "统计中的文档.docx")]
tab1 = seed(jobs)
win._convert_workers = [FakeConvertWorker(DOCX, running=True)]
reset_recorders()
LOGS.clear()

ok = win._start_print_worker(win._get_current_jobs())
after_freeze_btn = win._btn_start.isEnabled()

check("返回 False（没开始）", ok is False, f"ret={ok}")
check("标签页**没有**被冻结", tab1.frozen is False, f"frozen={tab1.frozen}")
check("没有取号/上报", UPLOAD_CALLS == [], f"{len(UPLOAD_CALLS)} 次")
check("没有启动打印 Worker", CREATED_WORKERS == [], f"{len(CREATED_WORKERS)} 个")
check("日志写明「正在统计页数」",
      any("正在统计页数" in _m for _m in LOGS), " / ".join(LOGS[-2:])[:160])
check("日志写明「统计完再点开始打印」",
      any("统计完再点开始打印" in _m for _m in LOGS))
check("日志点出是哪个文件",
      any("统计中的文档.docx" in _m for _m in LOGS), " / ".join(LOGS[-2:])[:160])
check("状态栏也有提示", "统计页数" in (win._status_bar.currentMessage() or ""),
      win._status_bar.currentMessage())
check("「开始打印」按钮仍然可用（不能留永久禁用）", after_freeze_btn is True)
check("_print_starting 没留脏值", getattr(win, "_print_starting", None) is False,
      repr(getattr(win, "_print_starting", None)))
check("不弹模态框（只靠日志 + 状态栏）",
      sum(len(v) for v in MODALS.values()) == 0,
      {k: len(v) for k, v in MODALS.items()})

# ② 统计线程结束（页数写回来）后，同一批能正常启动
print("\n=== ② 统计完成后能正常启动（守卫不是永久拦） ===")
win._convert_workers = []                       # 线程结束
jobs[1].page_count = 5                          # 页数已写回
reset_recorders()
LOGS.clear()

ok2 = win._start_print_worker(win._get_current_jobs())
check("返回 True（已启动）", ok2 is True, f"ret={ok2}")
check("标签页被冻结", tab1.frozen is True, f"frozen={tab1.frozen}")
check("取了号 / 上报了一次", len(UPLOAD_CALLS) == 1, f"{len(UPLOAD_CALLS)} 次")
check("启动了一个打印 Worker", len(CREATED_WORKERS) == 1, f"{len(CREATED_WORKERS)} 个")
check("Worker 拿到两个文件",
      len(getattr(CREATED_WORKERS[0], "_jobs", []) or []) == 2,
      f"{len(getattr(CREATED_WORKERS[0], '_jobs', []) or [])} 个")
check("页数未知的提示没出现（页数已经齐全）",
      not any("页数未知" in _m for _m in LOGS), " / ".join(LOGS[-2:])[:160])

# ③ 页数 0 但**没有**在跑的统计线程 ⇒ 允许开打 + 明确提示
print("\n=== ③ 页数 0 且无统计线程（坏件）⇒ 允许开始，但提示按 0 页计费 ===")
win._worker = None
tab2 = seed([make_job(PDF, 2, "正常.pdf"),
             make_job(os.path.join(_SANDBOX, "坏件.docx"), 0, "坏件.docx")])
win._convert_workers = []
reset_recorders()
LOGS.clear()

ok3 = win._start_print_worker(win._get_current_jobs())
check("返回 True（不拦，否则这单永远打不出去）", ok3 is True, f"ret={ok3}")
check("标签页被冻结（真的开打了）", tab2.frozen is True, f"frozen={tab2.frozen}")
check("确实上报了（按 0 页计费）", len(UPLOAD_CALLS) == 1, f"{len(UPLOAD_CALLS)} 次")
check("日志写明「页数未知」与「按 0 页计费上报」",
      any("页数未知" in _m and "0 页" in _m for _m in LOGS),
      " / ".join(LOGS[-2:])[:200])
check("状态栏也写了「页数未知」",
      "页数未知" in (win._status_bar.currentMessage() or ""),
      win._status_bar.currentMessage())
check("**不弹模态框**（无人值守机器不能被对话框卡住）",
      sum(len(v) for v in MODALS.values()) == 0,
      {k: len(v) for k, v in MODALS.items()})
check("提示里给了处置线索（损坏/转换失败）",
      any("损坏" in _m or "转换失败" in _m for _m in LOGS),
      " / ".join(LOGS[-2:])[:200])

# ══════════════════════════════════════════════════════════════════════════
print("\n=== ④ 静态接线：守卫在冻结 / files_data **之前** ===")
spw = body_of("_start_print_worker")
check("抓到了 _start_print_worker 正文", len(spw) > 2000, f"{len(spw)} 字符")

_i_guard = spw.find("_pending_page_count_stats(flat_jobs)")
_i_frozen = spw.find("tab.frozen = True")
_i_files = spw.find("files_data = []")
_i_btn_off = spw.find("self._btn_start.setEnabled(False)")
check("守卫调用了 _pending_page_count_stats", _i_guard > 0, f"idx={_i_guard}")
check("守卫在 tab.frozen = True 之前", 0 < _i_guard < _i_frozen,
      f"guard={_i_guard} frozen={_i_frozen}")
check("守卫在 files_data 构造之前", 0 < _i_guard < _i_files,
      f"guard={_i_guard} files_data={_i_files}")
check("守卫在禁用按钮之前", 0 < _i_guard < _i_btn_off,
      f"guard={_i_guard} btn_off={_i_btn_off}")
_guard_seg = spw[_i_guard:_i_frozen]
check("守卫段里有 return False（真的中止）", "return False" in _guard_seg)
check("守卫段里恢复按钮（setEnabled(True)）",
      "setEnabled(True)" in _guard_seg)
check("守卫段里清掉 _print_starting",
      "self._print_starting = False" in _guard_seg)
check("守卫段里写状态栏", "_status_bar.showMessage" in _guard_seg)
check("守卫段里没有新定时器", "QTimer" not in _guard_seg)
check("守卫段里没有模态框",
      "QMessageBox" not in _guard_seg, "（模态框会卡住无人值守）")

ppc = body_of("_pending_page_count_stats")
check("判据用 page_count <= 0", "page_count" in ppc and "<=" in ppc)
check("判据要求统计线程 isRunning()", "isRunning()" in ppc)
check("数据源是 _convert_workers", "_convert_workers" in ppc)
check("按文件路径比对（不是按行号猜）",
      "normcase" in ppc and "_file_path" in ppc)
check("路径规范化用 normcase + abspath", "normcase" in ppc and "abspath" in ppc)

check("静态文案：⏳ 还有 N 个文件正在统计页数",
      "⏳ 还有" in SRC and "正在统计页数" in SRC)
check("静态文案：页数未知按 0 页计费上报",
      "页数未知" in SRC and "按 0 页计费上报" in SRC)
check("没有 markdown 星号混进这两条界面文案",
      "**" not in "⏳ 还有 {n} 个文件正在统计页数"
      and "**" not in "⚠ 有 N 个文件页数未知，将按 0 页计费上报")

_guards = [m.start() for m in re.finditer(r"_pending_page_count_stats\(", SRC)]
check("_pending_page_count_stats 只有一个调用点（不散落）", len(_guards) == 2,
      f"{len(_guards)} 处（含 def 本身）")

print("\n" + ("PASS 全部通过" if ok_all else "FAIL 有失败项"))
sys.exit(0 if ok_all else 1)
