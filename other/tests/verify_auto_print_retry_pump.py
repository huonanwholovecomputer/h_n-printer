# -*- coding: utf-8 -*-
"""验收脚本 · 无障碍自动打印重试队列必须「自己驱动排空」（GUI 集成，offscreen）

2026-11-02 现场 HN20261102-0023（微信下单 / ⚡无障碍 / auto_print=1 / schedule_mode=now /
5 个文件）：13:37:51 任务到手、13:38:00 自动建标签页、13:38:01 就"自动开始打印"了，
可真正出纸是 13:40:00 —— 白等 2 分钟。原因不是字体闸门（日志里明写
`▶ 自动打印任务：已记录字体问题，继续打印`），而是：

① `_start_print_worker` 的页数守卫（`_pending_page_count_stats`）发现 3 个 Office 文件
   还在转 PDF 数页数 → 直接 `return False`，只记一句「⏳ 还有 N 个文件正在统计页数」；
② 无障碍自动打印拿到 False 就入 `_auto_print_retry` 重试队列（文案还错写成"打印机正忙"）；
③ 而 `_auto_print_retry` 原来**只在** `_on_all_finished`（上一批打印收尾）里排空 ——
   上一批 13:36:24 就打完了，下一批收尾要等到 13:40:00 ⇒ 队列空转 2 分钟。

本脚本钉住修好的语义：
① 入队即开一个 2 秒单次定时器，由 `_pump_auto_print_retry` 自己抽水（不重置、不叠加）；
② 页数没统计完 / 打印机在忙 时**不启动**、也不刷屏，只静默重排下一次 tick；
③ 条件成熟时用 `interactive=False` 后台启动，启动成功才出队；启动失败留在队首；
④ `_drain_auto_print_retry` 从 `_on_all_finished` 搬出来后行为一字未改
   （tab_key 失效按订单号反查、已打完的跳过、无文件跳过）；
⑤ `_on_all_finished` 的事件驱动排空仍然保留（两条驱动并存，不互相顶掉）。

    python tests/verify_auto_print_retry_pump.py     # exit 0 = 全绿
"""
import inspect
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_autoretry_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

import converter as _converter  # noqa: E402
import gui as _gui  # noqa: E402

# 引擎探测会真的 Dispatch Word/WPS COM → 桩成可用（同其它 GUI 验收脚本）
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}
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


print("… 正在创建主窗口", flush=True)
win = MainWindow(config_path=paths.config_path())
win.resize(1200, 800)
win.show()
app.processEvents()

LOGS = []
_orig_log = win._log


def _capture(msg):
    LOGS.append(str(msg))
    try:
        _orig_log(msg)
    except Exception:
        pass


win._log = _capture


class _FakeWorker:
    """假线程：只要 isRunning()/`_file_path` 两个属性（`_pending_page_count_stats` 就看这两个）。"""

    def __init__(self, file_path="", running=True):
        self._file_path = file_path
        self._running = running

    def isRunning(self):
        return self._running


STARTED = []


def _stub_start(result=True):
    STARTED.clear()

    def _fake(jobs, tab_key=None, interactive=True, stack_entry=None):
        STARTED.append({
            "jobs": [getattr(j, "display_name", "") or os.path.basename(j.file_path) for j in jobs],
            "tab_key": tab_key,
            "interactive": interactive,
        })
        return result

    win._start_print_worker = _fake


def seed(jobs, tab_key="1"):
    tab = TabSettings()
    tab.jobs = jobs
    win._config.tabs = {tab_key: tab}
    win._current_tab = tab_key
    win._config.active_tab = tab_key
    win._rebuild_table()
    win._refresh_tab_display()
    app.processEvents()
    return tab_key


def job_of(name, page_count=2, order_id=974, sent=False):
    p = os.path.join(_SANDBOX, name)
    with open(p, "wb") as f:
        f.write(b"not a real file, only used as a path")
    j = PrintJob(file_path=p, copies=1, page_count=page_count, engine="word",
                 display_name=name)
    j.order_id = order_id
    j.sent = sent
    return j


def reset(queue=None):
    """把窗口恢复成「空闲、无定时器」状态。"""
    win._auto_print_retry = list(queue or [])
    t = getattr(win, "_auto_print_retry_timer", None)
    if t is not None and t.isActive():
        t.stop()
    win._worker = None
    win._stack_flush_worker = None
    win._print_starting = False
    win._convert_workers = []
    LOGS.clear()


# ═══════════════ ① 定时器：单次、可复用、不叠加 ═══════════════
reset()
win._arm_auto_print_retry_timer()
_t1 = getattr(win, "_auto_print_retry_timer", None)
check("①a 抽水定时器已建立并启动", _t1 is not None and _t1.isActive())
check("①b 单次定时器（不是每 2 秒无限重入的周期定时器）",
      _t1 is not None and _t1.isSingleShot())
win._arm_auto_print_retry_timer()          # 再排一次：不该新建对象 / 不该叠加
_t2 = win._auto_print_retry_timer
check("①c 重复 arm 复用同一个定时器对象", _t1 is _t2)
_t1.stop()

# ═══════════════ ② 页数没统计完 ⇒ 不许启动、但要自己重排下一次 ═══════════════
_j = job_of("王睿豪.pdf", page_count=0)
seed([_j])
win._convert_workers = [_FakeWorker(_j.file_path, running=True)]     # 模拟 ConvertWorker 还在跑
reset([{"order_id": 974, "tab_key": "1", "task_ids": [1971]}])
win._convert_workers = [_FakeWorker(_j.file_path, running=True)]
_stub_start(True)
win._pump_auto_print_retry()
check("②a 页数未统计完 ⇒ 不启动打印（别按 0 页上报）", STARTED == [], str(STARTED))
check("②b 订单留在重试队列里", len(win._auto_print_retry) == 1)
check("②c 已自动重排下一次抽水",
      win._auto_print_retry_timer is not None and win._auto_print_retry_timer.isActive())
check("②d 没有把「⏳ 还有 N 个文件正在统计页数」刷进日志",
      not any("正在统计页数" in m for m in LOGS), " | ".join(LOGS[-3:]))

# 页数统计完了（线程结束 + 页数写回）⇒ 同一份队列应立刻开打，不必等任何人收尾
win._convert_workers = []
_j.page_count = 2
win._auto_print_retry = [{"order_id": 974, "tab_key": "1", "task_ids": [1971]}]
LOGS.clear()
_stub_start(True)
win._pump_auto_print_retry()
check("②e 页数统计完 ⇒ 立刻后台开打", len(STARTED) == 1, str(STARTED))
check("②f 走后台线：interactive=False（无人值守不许弹窗）",
      bool(STARTED) and STARTED[0]["interactive"] is False)
check("②g 打的是这一单的标签页", bool(STARTED) and STARTED[0]["tab_key"] == "1")
check("②h 启动成功 ⇒ 出队", win._auto_print_retry == [])
check("②i 日志说明是「不必等下一批收尾」", any("自动打印重试队列" in m for m in LOGS),
      " | ".join(LOGS[-3:]))

# ═══════════════ ③ 打印机正在忙 ⇒ 这一 tick 不碰，等下一次 ═══════════════
reset([{"order_id": 974, "tab_key": "1", "task_ids": [1971]}])
win._worker = _FakeWorker("", running=True)
_stub_start(True)
win._pump_auto_print_retry()
check("③a 打印机在忙 ⇒ 不并发塞打印任务", STARTED == [], str(STARTED))
check("③b 队列原样保留", len(win._auto_print_retry) == 1)
check("③c 已重排下一次 tick",
      win._auto_print_retry_timer is not None and win._auto_print_retry_timer.isActive())

reset([{"order_id": 974, "tab_key": "1", "task_ids": [1971]}])
win._print_starting = True                      # 正在取号/上报订单
_stub_start(True)
win._pump_auto_print_retry()
check("③d 正在取号 ⇒ 也不并发启动", STARTED == [])

# ③e 攒批模式（第 1 趟堆在出纸槽里等第 2 趟）⇒ 绝不插新单（出纸槽独占铁律）
reset([{"order_id": 974, "tab_key": "1", "task_ids": [1971]}])
seed([job_of("a.pdf")])
_stub_start(True)
_entries = win._stack.entries
_entries.append({"seq": 999, "fake": True})
try:
    win._pump_auto_print_retry()
finally:
    _entries.clear()
check("③e 攒批第 2 趟待确认期间不插单（出纸槽独占）", STARTED == [], str(STARTED))
check("③f 队列仍在，等攒批腾空后再打", len(win._auto_print_retry) == 1)

# ═══════════════ ④ 排空逻辑搬运后行为不变 ═══════════════
# ④a 启动失败 ⇒ 留在队首（🔴-B4-3 的"防静默丢单"）
reset([{"order_id": 974, "tab_key": "1", "task_ids": [1971]}])
seed([job_of("a.pdf")])
_stub_start(False)
_popped = win._drain_auto_print_retry()
check("④a 启动失败 ⇒ 不出队、返回 False", _popped is False and len(win._auto_print_retry) == 1)

# ④b tab_key 失效（标签页重编号/删除）⇒ 按订单号反查真正的标签页，绝不按旧 key 猜
reset([{"order_id": 974, "tab_key": "9", "task_ids": [1971]}])
seed([job_of("a.pdf", order_id=974)], tab_key="1")
_stub_start(True)
win._drain_auto_print_retry()
check("④b tab_key 失效 ⇒ 按订单号反查到标签页 1",
      bool(STARTED) and STARTED[0]["tab_key"] == "1", str(STARTED))

# ④c 标签页里的文件都已 sent ⇒ 出队跳过，不重打
reset([{"order_id": 974, "tab_key": "1", "task_ids": [1971]}])
seed([job_of("a.pdf", sent=True)])
_stub_start(True)
_popped = win._drain_auto_print_retry()
check("④c 已全部打完 ⇒ 出队跳过、不重打", _popped is True and STARTED == []
      and win._auto_print_retry == [])
check("④d 跳过时有日志", any("已全部打印完成" in m for m in LOGS), " | ".join(LOGS[-3:]))

# ④e 标签页已删除 ⇒ 出队
reset([{"order_id": 974, "tab_key": "9", "task_ids": [1971]}])
win._config.tabs = {}
_stub_start(True)
_popped = win._drain_auto_print_retry()
check("④e 标签页已不存在 ⇒ 出队不再赖着", _popped is True and win._auto_print_retry == [])

# ═══════════════ ⑤ 事件驱动那条线没被拆坏 ═══════════════
reset([{"order_id": 974, "tab_key": "1", "task_ids": [1971]}])
seed([job_of("a.pdf")])
_stub_start(True)
try:
    win._on_all_finished(1, 0)
    _raised = ""
except Exception as e:                                  # noqa: BLE001
    _raised = f"{type(e).__name__}: {e}"
check("⑤a `_on_all_finished` 不再内嵌整段重试逻辑、改为调用排空函数",
      "_drain_auto_print_retry()" in inspect.getsource(win._on_all_finished))
check("⑤b 上一批收尾时仍然会补打（事件驱动没被抽水逻辑顶掉）",
      not _raised and bool(STARTED), _raised or str(STARTED))

# ═══════════════ ⑥ 入队日志不再把两种原因混为一谈 ═══════════════
_src = inspect.getsource(_gui.MainWindow)
check("⑥a 入队日志区分「页数还没统计完」与「打印机正忙」",
      "页数还没统计完（Office 正在转 PDF）" in _src and "打印机正忙" in _src)
check("⑥b 旧文案「打印完成后自动补打」不再出现（它正是「要等下一批收尾」的病根）",
      "打印完成后自动补打" not in _src)

# ═══════════════ ⑦ 重入安全：嵌套事件循环里再进来一次不许把队列 pop 空 ═══════════════
# 2026-11-12 现场 HN20261112-0023（云端 status=timeout、结算页里没有它）：
# 21:25:05 开始打印，21:25:30 进程崩掉，crash_traceback.txt 写的是
#   IndexError: pop from empty list   （gui.py:_pump_auto_print_retry → _drain_auto_print_retry）
# 触发链：`_start_print_worker` 取订单号时 processEvents 跑嵌套事件循环，嵌套里另一条路径
# 已经把队首处理掉，外层回来照旧 pop(0) ⇒ 空队列弹出 ⇒ Qt 槽里抛异常 = 整个进程被杀。
reset([{"order_id": 974, "tab_key": "1", "task_ids": [1971]}])
seed([job_of("a.pdf")])
_STARTED7 = []
_depth7 = {"n": 0}


def _reentrant_start(jobs, tab_key=None, interactive=True, stack_entry=None):
    _STARTED7.append([getattr(j, "display_name", "") for j in jobs])
    _depth7["n"] += 1
    if _depth7["n"] == 1:
        # 模拟 processEvents 期间嵌套进来的另一个排空者（_on_all_finished / 抽水定时器）
        _gui.MainWindow._drain_auto_print_retry(win)
        _gui.MainWindow._drain_auto_print_retry(win)
    return True


win._start_print_worker = _reentrant_start
try:
    win._drain_auto_print_retry()
    _err7 = ""
except Exception as e:                                  # noqa: BLE001
    _err7 = f"{type(e).__name__}: {e}"
check("⑦a ★ 嵌套排空不再 IndexError: pop from empty list（2026-11-12 崩溃点）",
      not _err7, _err7)
check("⑦b 队首只出队一次、队列清空", win._auto_print_retry == [], str(win._auto_print_retry))
check("⑦c 嵌套那次直接退让 ⇒ 同一单只启动一次（不会重复出纸）",
      len(_STARTED7) == 1, str(_STARTED7))
check("⑦d 重入闸门用完复位", getattr(win, "_auto_retry_draining", "missing") is False,
      str(getattr(win, "_auto_retry_draining", "missing")))

# ⑦e 出队只认「队首还是我这一条」
reset([{"order_id": 1}, {"order_id": 2}])
_head = win._auto_print_retry[0]
_other = {"order_id": 2}
check("⑦e 传进来的不是队首 ⇒ 不弹（另一条路径已换过队，不能误删）",
      win._pop_retry_head(_other) is False and len(win._auto_print_retry) == 2)
check("⑦f 是队首 ⇒ 弹掉并返回 True",
      win._pop_retry_head(_head) is True and win._auto_print_retry == [{"order_id": 2}])
check("⑦g 队列已空时再弹不抛异常", win._pop_retry_head({"order_id": 9}) is False)

# ═══════════════ ⑧ 同一订单还在防抖收集文件 ⇒ 重试队列不许抢跑（2026-11-18）═══════════════
# 现场 HN20261118-0051（订单 #1451 / 3 个文件 / 全双面 / 需人工翻纸）：第一个文件 21:11:58
# 就绪并起 8 秒防抖，21:12:06 到期时只有它；另两个文件 21:12:07 / 21:12:09 才就绪、把防抖
# 重置到 21:12:17。中间的 21:12:09 重试队列抽水看到「标签页里已经有 job」就开打了 ⇒ 整单被
# 劈成两批（先 1 个文件 6 张纸，后 2 个文件 5 张纸），多翻一次纸、spooler 多两个作业。
win._auto_print_queue = {}
reset([{"order_id": 1451, "tab_key": "1", "task_ids": [2863]}])
seed([job_of("三次作业.docx", order_id=1451)])
win._auto_print_queue = {1451: [object()]}       # 还有文件在防抖里等批量入页
_stub_start(True)
_logs_before_pump = len(LOGS)
win._pump_auto_print_retry()
check("⑧a 同订单仍在防抖收集 ⇒ 抽水不抢跑（别把一单劈成两批）", STARTED == [], str(STARTED))
check("⑧b 队首留在重试队列里", len(win._auto_print_retry) == 1, str(win._auto_print_retry))
check("⑧c 已自动重排下一次 tick",
      win._auto_print_retry_timer is not None and win._auto_print_retry_timer.isActive())
# 让路的几个分支都不写日志（`_pump_auto_print_retry` 的预检注释里写明了这一点）⇒
# 「有没有伪报可以开打」用**日志条数没变**来判，比拿中文文案当契约稳（那条
# 「现在可以开打」的文案改一次就会假红，而且守卫的棘轮不许再加这种判据）。
check("⑧d 让路时一行日志都不刷（不伪报「现在可以开打」）",
      len(LOGS) == _logs_before_pump, " | ".join(LOGS[_logs_before_pump:][-3:]))

# 事件驱动那条（`_on_all_finished` → `_drain_auto_print_retry`）同样要让路
reset([{"order_id": 1451, "tab_key": "1", "task_ids": [2863]}])
seed([job_of("三次作业.docx", order_id=1451)])
win._auto_print_queue = {1451: [object()]}
_stub_start(True)
_p8 = win._drain_auto_print_retry()
check("⑧e `_on_all_finished` 直排路径同样让路", _p8 is False and STARTED == []
      and len(win._auto_print_retry) == 1, str(STARTED))

# 防抖到期 ⇒ 三个文件都并进同一标签页了，这一次要整单一次开打
win._auto_print_queue = {}                       # `_process_auto_print_order` 已 pop 掉队列
reset([{"order_id": 1451, "tab_key": "1", "task_ids": [2863]}])
seed([job_of("三次作业.docx", order_id=1451),
      job_of("实验报告 正反打印zc.docx", order_id=1451),
      job_of("gjq.docx", order_id=1451)])
_stub_start(True)
win._pump_auto_print_retry()
check("⑧f 防抖到期后整单一次开打（3 个文件同一批）",
      len(STARTED) == 1 and len(STARTED[0]["jobs"]) == 3, str(STARTED))
check("⑧g 启动成功 ⇒ 出队", win._auto_print_retry == [], str(win._auto_print_retry))

# ⑧h 闸门只认「同一订单」：别的订单在防抖里，不该挡住这一单
win._auto_print_queue = {1452: [object()]}
reset([{"order_id": 1451, "tab_key": "1", "task_ids": [2863]}])
seed([job_of("三次作业.docx", order_id=1451)])
_stub_start(True)
win._pump_auto_print_retry()
check("⑧h 别的订单在防抖里也照打（闸门按订单号认人）", len(STARTED) == 1, str(STARTED))
win._auto_print_queue = {}

print()
print("=" * 60)
print("全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
