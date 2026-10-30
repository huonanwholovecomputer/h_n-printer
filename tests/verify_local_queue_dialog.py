# -*- coding: utf-8 -*-
"""验收脚本 · 「🖨 本机队列」并进既有常驻窗 StackQueueDialog（offscreen，不真打印）

用户场景（2026-10-30）：打印机忙的时候，第二个、第三个任务要有**总体队列**显示，
参考「攒批队列」那个常驻窗的设计；且不能因为"不支持自动双面"就弹一堆窗口出来。
用户已选方案：**不新增窗口** —— 在 `StackQueueDialog` 里加第三块「🖨 本机队列」，
列 = 状态 / 来源 / 内容 / 张数 / 预计完成；空闲自动隐藏；底栏按钮显示读数。

钉住的几条口径：

  ① 空闲（没有在打、没有排队）= 该区域隐藏 + 常驻窗根本不出现；
  ② 打印机忙时的总体队列：进行中 1 单 + 排队 2 单 ⇒ 3 行、状态/来源/内容正确；
     **队列内容由 MainWindow 组装成纯数据后喂 setter**（对话框不摸 MainWindow 状态）；
  ③ 无自动双面机型的手动双面批次：两趟要显示成「第 1 趟进行中 / 等待翻纸 / 第 2 趟进行中」，
     其中**翻纸等待算进行中**（不是等待中；操作员正在动手，不是"还没轮到"）；
  ④ 底栏按钮 `_btn_stack` 在本机队列非空时给读数（`🖨 打印中 N · 排队 M`），
     空纸叠 + 空队列仍旧隐藏（旧规则不变）；
  ⑤ `_sync_stack_list_visible` 三种显隐组合（空闲隐藏 / 只有本机队列也要在场 /
     攒批模式开且在适用机型上常驻），外加"操作员主动收起后不抢着弹回来"；
  ⑥ 静态接线：2 秒心跳 `_stack_state_tick()` 里调 `_sync_local_queue()`、
     没有新建窗口类、复用现有速度模型算预计完成。

    python tests/verify_local_queue_dialog.py     # exit 0 = 全绿
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

_SANDBOX = tempfile.mkdtemp(prefix="hn_localq_")
os.environ["APPDATA"] = _SANDBOX                  # 用户数据目录 → 沙箱（不碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui  # noqa: E402
import duplex_stack as ds  # noqa: E402
import paths  # noqa: E402
from printer_config import PrintJob, PrinterProfile  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


SRC = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()


def _body(name, src=None):
    """取一个方法的源码正文（到下一个 `def ` 或类尾），静态接线断言用。"""
    src = SRC if src is None else src
    m = re.search(rf"\n    def {re.escape(name)}\(", src)
    if not m:
        return ""
    rest = src[m.end():]
    nxt = re.search(r"\n    def |\n\nclass ", rest)
    return rest[:nxt.start()] if nxt else rest


# 真打印一律不碰：MainWindow 用的对话框/警告一律给安全答案
gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.No)
gui.QMessageBox.warning = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)
gui.QMessageBox.information = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)
gui.QMessageBox.critical = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)

MANUAL = PrinterProfile(auto_duplex=False, capability_source="manual", manual_duplex=True,
                        first="odd", reverse_second=True, second_rotate_180=True,
                        verified_at="2026-10-12 22:14:49", verified_by="测试成员",
                        stack_capacity=90, stack_warn_after_sec=1800)
AUTO = PrinterProfile(auto_duplex=True, capability_source="probe", manual_duplex=False,
                      stack_capacity=90, stack_warn_after_sec=1800)


def make_job(name, pages=4, duplex="on", task_id=0, order_id=0):
    return PrintJob(file_path=os.path.join(_SANDBOX, name), copies=1, duplex=duplex,
                    page_range="", page_count=pages, orientation="portrait",
                    engine="pdf", display_name=name, task_id=task_id, order_id=order_id)


class FakeWorker:
    """只提供「本机队列」读取所需的那几个属性（isRunning / _jobs / _stack_mode / _pass_state）。"""

    def __init__(self, jobs, stack_mode=False, pass_state="", running=True):
        self._jobs = list(jobs)
        self._stack_mode = bool(stack_mode)
        self._pass_state = pass_state
        self._running = bool(running)

    def isRunning(self):
        return self._running


def tree_rows(tree):
    out = []
    for i in range(tree.topLevelItemCount()):
        it = tree.topLevelItem(i)
        out.append([it.text(c) for c in range(tree.columnCount())])
    return out


ETA_RE = re.compile(r"^\d\d:\d\d · \d\d:\d\d$")

# ============================================================
# ① 空闲：本机队列区隐藏、常驻窗不出现
# ============================================================
print("\n=== ① 空闲自动隐藏 ===")

dlg = gui.StackQueueDialog()
check("列口径 = 状态/来源/内容/张数/预计完成",
      list(dlg.LOCAL_COLS) == ["状态", "来源", "内容", "张数", "预计完成"],
      str(dlg.LOCAL_COLS))
check("「内容」列是 Stretch（长文件名差别大）",
      dlg._tree_local.header().sectionResizeMode(2).name == "Stretch",
      dlg._tree_local.header().sectionResizeMode(2).name)
check("新建对话框：本机队列区默认隐藏", dlg.local_queue_visible() is False)

dlg.set_local_queue([])
check("喂空列表：仍然隐藏", dlg.local_queue_visible() is False)
check("喂空列表：表格也清空", dlg._tree_local.topLevelItemCount() == 0)

dlg.set_local_queue(None)
check("喂 None：同样隐藏（不崩）", dlg.local_queue_visible() is False)

win = gui.MainWindow(config_path=paths.config_path())
win._config.printer_name = "Fake Print"
win._config.printer_profiles["Fake Print"] = MANUAL
win._config.duplex_stack_mode = False
win._stack_list_user_closed = False
win._worker = None
win._stack_flush_worker = None
win._stack_wait = []
win._stack_entry_wait = []
win._auto_print_retry = []
win._scheduled_orders = {}
try:
    win._stack_state_timer.stop()          # 心跳由测试自己按需调，避免异步插一脚
except Exception:
    pass

check("空闲：本机队列 0 行", win._local_queue_rows() == [], str(win._local_queue_rows()))
check("空闲：读数 (0, 0)", tuple(win._local_queue_counts()) == (0, 0),
      str(win._local_queue_counts()))
win._sync_local_queue()
check("空闲：连常驻窗都不造（不弹空窗）",
      getattr(win, "_stack_queue_dialog", None) is None)
check("空闲：本机队列区不显示",
      getattr(win, "_stack_queue_dialog", None) is None
      or not win._stack_queue_dialog.local_queue_visible())

# ============================================================
# ② 打印中 1 单 + 排队 2 单（纯数据结构，不真打印）
# ============================================================
print("\n=== ② 打印机忙：进行中 1 单 + 等待中 2 单 ===")

job_a = make_job("在打.pdf", pages=4, duplex="on")
job_b = make_job("排队一.pdf", pages=6, duplex="on")
job_c1 = make_job("排队二-1.pdf", pages=2, duplex="off")
job_c2 = make_job("排队二-2.pdf", pages=2, duplex="off")

win._worker = FakeWorker([job_a])
win._stack_wait = [
    {"jobs": [job_b], "tab_key": "1", "reason": "容量不足：先打第 2 趟腾空"},
    {"jobs": [job_c1, job_c2], "tab_key": "1", "reason": "出纸槽被当前纸叠独占"},
]
rows = win._local_queue_rows()
check("进行中 1 + 等待 2 = 3 行", len(rows) == 3, str(len(rows)))
check("第 1 行状态「进行中」", rows[0]["state"] == "进行中", rows[0]["state"])
check("第 1 行算进行中（active）", rows[0]["active"] is True)
check("来源「本机点击」", rows[0]["source"] == "本机点击", rows[0]["source"])
check("内容：单文件给文件名", rows[0]["content"] == "在打.pdf", rows[0]["content"])
check("后两行都是「等待中」且不算进行中",
      [r["state"] for r in rows[1:]] == ["等待中", "等待中"]
      and not any(r["active"] for r in rows[1:]),
      str([r["state"] for r in rows[1:]]))
check("内容：多个文件给「2 个文件」", rows[2]["content"] == "2 个文件", rows[2]["content"])
# 4 页双面对驱动的自动配页 ⇒ 2 张纸；6 页 ⇒ 3 张；2 页单面 ×2 ⇒ 4 张
check("张数走 duplex_stack 唯一口径（4 页双面 = 2 张）", rows[0]["sheets"] == "2",
      rows[0]["sheets"])
check("等待行张数正确（6 页双面 = 3 张 / 2 页单面 ×2 = 4 张）",
      rows[1]["sheets"] == "3" and rows[2]["sheets"] == "4",
      f"{rows[1]['sheets']} / {rows[2]['sheets']}")
check("预计完成 = 时长 · 墙钟（复用 fit_print_speed + format_duration）",
      bool(ETA_RE.match(rows[0]["eta"])), rows[0]["eta"])
check("等待行的预计完成比进行行更晚（累计占用）",
      rows[1]["eta"] != rows[0]["eta"], f"{rows[0]['eta']} / {rows[1]['eta']}")

dlg.set_local_queue(rows)
check("喂进对话框：区域显示出来", dlg.local_queue_visible() is True)
check("3 行都渲染出来", dlg._tree_local.topLevelItemCount() == 3,
      str(dlg._tree_local.topLevelItemCount()))
check("标题带读数「进行中 1 / 等待中 2」",
      "进行中 1" in dlg._local_group.title() and "等待中 2" in dlg._local_group.title(),
      dlg._local_group.title())
tl = tree_rows(dlg._tree_local)
check("状态列文本对得上", [r[0] for r in tl] == ["进行中", "等待中", "等待中"], str([r[0] for r in tl]))
check("来源列文本对得上", [r[1] for r in tl] == ["本机点击", "本机点击", "本机点击"],
      str([r[1] for r in tl]))
check("预计完成列没有空格子", all(r[4] for r in tl), str([r[4] for r in tl]))

_first_item = dlg._tree_local.topLevelItem(0)
dlg.set_local_queue([dict(r) for r in rows])
check("内容没变 ⇒ 不重建表格（2 秒心跳不闪）",
      dlg._tree_local.topLevelItem(0) is _first_item)

# 打完之后回到空闲 ⇒ 区域自己收起来（不是"亮过一次就永远亮着"）
dlg.set_local_queue([])
check("打完再喂空列表 ⇒ 区域重新隐藏（空闲自动隐藏）",
      dlg.local_queue_visible() is False)
check("打完再喂空列表 ⇒ 行清空", dlg._tree_local.topLevelItemCount() == 0,
      str(dlg._tree_local.topLevelItemCount()))
dlg.set_local_queue(rows)   # 恢复，后面还要用

# 来源：云端自动 / 预约
win._worker = FakeWorker([make_job("云端.pdf", task_id=9001, order_id=555)])
check("来源：云端任务给「云端自动」", win._local_queue_rows()[0]["source"] == "云端自动",
      win._local_queue_rows()[0]["source"])
win._scheduled_orders = {777: {"target_ts": 0, "ready": {1: object()}, "pending": {},
                               "tab_key": "1", "printed": False}}
win._worker = FakeWorker([make_job("预约.pdf", task_id=0, order_id=777)])
check("来源：预约单给「预约」", win._local_queue_rows()[0]["source"] == "预约",
      win._local_queue_rows()[0]["source"])
win._scheduled_orders = {}
win._worker = None
win._stack_wait = []

# ============================================================
# ③ 无自动双面机型：两趟状态文案
# ============================================================
print("\n=== ③ 手动双面两趟：第 1 趟 / 等待翻纸 / 第 2 趟 ===")

job_dup = make_job("双面四页.pdf", pages=4, duplex="on")
wdup = FakeWorker([job_dup], pass_state="第 1 趟进行中")
win._worker = wdup
r = win._local_queue_rows()
check("第 1 趟：状态「第 1 趟进行中」", r and r[0]["state"] == "第 1 趟进行中",
      str(r[0]["state"] if r else None))
check("第 1 趟：算进行中", bool(r) and r[0]["active"] is True)

wdup._pass_state = "等待翻纸"
r = win._local_queue_rows()
check("等待翻纸：状态「等待翻纸」", r and r[0]["state"] == "等待翻纸",
      str(r[0]["state"] if r else None))
check("等待翻纸：仍算进行中（不是等待中）", bool(r) and r[0]["active"] is True)
check("等待翻纸：预计完成给「—」（要人动手，估时无意义）",
      bool(r) and r[0]["eta"] == "—", str(r[0]["eta"] if r else None))

wdup._pass_state = "第 2 趟进行中"
r = win._local_queue_rows()
check("第 2 趟：状态「第 2 趟进行中」", r and r[0]["state"] == "第 2 趟进行中",
      str(r[0]["state"] if r else None))

win._worker = None
wdup._running = False
_ent = ds.StackEntry(seq=1, title="HN-A", page_count=3, sheet_count=2, rotate=True,
                     pdf="e1.pdf", first="odd", state=ds.STATE_DONE,
                     file_plans=[{"label": "A.pdf", "pages": [1, 2, 3], "duplex": True}])
win._stack.entries.append(_ent)
r = win._local_queue_rows()
check("纸叠在等翻纸（没有 worker）：还是「等待翻纸」且算进行中",
      bool(r) and r[0]["state"] == "等待翻纸" and r[0]["active"] is True,
      str([(x["state"], x["active"]) for x in r]))
check("纸叠在等翻纸：内容写明整叠几单几张",
      bool(r) and "整叠 1 单" in r[0]["content"], str(r[0]["content"] if r else None))

fl = FakeWorker([], running=True)          # 整叠第 2 趟在飞
win._stack_flush_worker = fl
r = win._local_queue_rows()
check("整叠第 2 趟在飞：状态「第 2 趟进行中」", bool(r) and r[0]["state"] == "第 2 趟进行中",
      str(r[0]["state"] if r else None))
win._stack_flush_worker = None
win._stack.clear()

# ============================================================
# ④ 底栏按钮读数
# ============================================================
print("\n=== ④ 底栏按钮读数 ===")

win._worker = FakeWorker([make_job("在打.pdf")])
win._stack_wait = [{"jobs": [make_job("排队一.pdf")], "tab_key": "1", "reason": "容量不足"},
                   {"jobs": [make_job("排队二.pdf")], "tab_key": "1", "reason": "容量不足"}]
win._sync_stack_button()
check("有本机队列 ⇒ 按钮出现并显示读数",
      not win._btn_stack.isHidden()
      and win._btn_stack.text() == "🖨 打印中 1 · 排队 2",
      f"{win._btn_stack.text()} hidden={win._btn_stack.isHidden()}")

_ent2 = ds.StackEntry(seq=1, title="HN-B", page_count=3, sheet_count=2, rotate=True,
                      pdf="e2.pdf", first="odd")
win._stack.entries.append(_ent2)
win._sync_stack_button()
_txt = win._btn_stack.text()
check("有纸叠时保留旧文案（🖐 攒批 N张/M单）",
      _txt.startswith("🖐 攒批 "), _txt)
check("有纸叠 + 有本机队列时合并读数", "🖨 打印中 1 · 排队 2" in _txt, _txt)
win._stack.clear()

win._worker = None
win._stack_wait = []
win._sync_stack_button()
check("空纸叠 + 空队列 ⇒ 按钮仍隐藏（旧规则不变）",
      win._btn_stack.isHidden() and win._btn_stack.text() == "🖐 攒批列表",
      f"{win._btn_stack.text()} hidden={win._btn_stack.isHidden()}")

# ============================================================
# ⑤ _sync_stack_list_visible 三种显隐组合
# ============================================================
print("\n=== ⑤ 窗口显隐三种组合 ===")

# 组合 A：空闲（模式关 + 空纸叠 + 空队列）⇒ 窗口不在场
win._config.printer_name = "Fake Print"
win._config.printer_profiles["Fake Print"] = AUTO      # 自动双面机：攒批不适用
win._config.duplex_stack_mode = False
win._stack_list_user_closed = False
d0 = getattr(win, "_stack_queue_dialog", None)
if d0 is not None:
    d0.hide()
win._sync_stack_list_visible()
d0 = getattr(win, "_stack_queue_dialog", None)
check("组合 A（空闲）⇒ 窗口不在场", d0 is None or not d0.isVisible(),
      f"dlg={d0} visible={getattr(d0, 'isVisible', lambda: None)()}")

# 组合 B：纸叠空 + 攒批不适用 + **本机队列有内容** ⇒ 也要在场
win._worker = FakeWorker([make_job("在打.pdf")])
win._sync_stack_list_visible()
d1 = getattr(win, "_stack_queue_dialog", None)
check("组合 B（自动双面机 + 只有本机队列）⇒ 窗口也要在场",
      d1 is not None and d1.isVisible(),
      f"dlg={d1} visible={getattr(d1, 'isVisible', lambda: None)()}")
check("组合 B：本机队列区是显示的",
      d1 is not None and d1.local_queue_visible())
check("组合 B：攒批那套对它不适用（判据没被放宽）", win._stack_applicable() is False)

# 组合 C：攒批模式开 + 适用机型 + 空纸叠空队列 ⇒ 常驻（老口径不变）
win._worker = None
win._config.printer_name = "Fake Print"
win._config.printer_profiles["Fake Print"] = MANUAL
win._config.duplex_stack_mode = True
win._stack_list_user_closed = False
if d1 is not None:
    d1.hide()
win._sync_stack_list_visible()
d2 = getattr(win, "_stack_queue_dialog", None)
check("组合 C（模式开 + 手动机型）⇒ 窗口常驻",
      d2 is not None and d2.isVisible(),
      f"visible={getattr(d2, 'isVisible', lambda: None)()}")
check("组合 C：本机队列空 ⇒ 该区域隐藏",
      d2 is not None and d2.local_queue_visible() is False)

# 额外：操作员主动收起后不抢着弹回来
win._stack_list_user_closed = True
if d2 is not None:
    d2.hide()
win._worker = FakeWorker([make_job("在打.pdf")])
win._sync_stack_list_visible()
check("主动收起后：本机队列有内容也不抢着弹回来",
      d2 is not None and not d2.isVisible(),
      f"visible={getattr(d2, 'isVisible', lambda: None)()}")
win._stack_list_user_closed = False
win._worker = None
if d2 is not None:
    d2.hide()

# ============================================================
# ⑥ 静态接线
# ============================================================
print("\n=== ⑥ 静态接线 ===")

check("对话框有 set_local_queue setter", "def set_local_queue(" in SRC)
check("主窗口把**纯数据**喂给 setter（对话框不摸 MainWindow 状态）",
      "dlg.set_local_queue(rows)" in SRC)
check("第三块标题是「🖨 本机队列」", "🖨 本机队列" in SRC)
check("LOCAL_COLS 常量存在", "LOCAL_COLS" in SRC)

tick = _body("_stack_state_tick")
check("2 秒心跳里调了 _sync_local_queue（没新起定时器）",
      "_sync_local_queue()" in tick, tick.strip()[:80])
check("心跳里没新建 QTimer（复用现有 timer）", "QTimer(" not in tick)

check("没有新建窗口类（本机队列并进 StackQueueDialog）",
      re.search(r"class\s+\w*LocalQueue\w*", SRC) is None
      and SRC.count("class StackQueueDialog") == 1)

visible = _body("_sync_stack_list_visible")
check("_sync_stack_list_visible 认「本机队列非空」", "has_local" in visible)
check("纸叠为空但本机队列非空时也让它在场",
      "not has_entries and not has_local and not self._stack_applicable()" in visible)

btn = _body("_sync_stack_button")
check("底栏按钮有本机队列读数文案", "🖨 打印中" in btn)
check("空纸叠 + 空队列仍是隐藏（旧规则）",
      'btn.setText("🖐 攒批列表")' in btn and "btn.setVisible(False)" in btn)

refresh = _body("_refresh_stack_queue")
check("攒批列表刷新时顺带刷本机队列", "_sync_local_queue()" in refresh)

check("预计完成复用现有速度模型（fit_print_speed 已 import）",
      "fit_print_speed" in SRC.split("class PrintWorker")[0]
      and "fit_print_speed" in _body("_local_queue_speed"))
check("时长文案复用 format_duration",
      "format_duration(" in _body("_local_queue_eta_text"))

check("PrintWorker 有 _pass_state（两趟状态只读展示）",
      SRC.count('self._pass_state = "第 1 趟进行中"') == 1
      and SRC.count('self._pass_state = "等待翻纸"') == 1
      and SRC.count('self._pass_state = "第 2 趟进行中"') == 1,
      f"{SRC.count('_pass_state')} 处")
check("_pass_state 只在 _print_manual_batch 里赋值 4 处（两趟 + 翻纸 + 清空）",
      _body("_print_manual_batch").count("self._pass_state =") == 4,
      f"{_body('_print_manual_batch').count('self._pass_state =')} 处")
check("_pass_state 只在 __init__ 里初始化一次",
      SRC.count('self._pass_state = ""') == 2,
      f"{SRC.count('self._pass_state = ' + chr(34) + chr(34))} 处")

rows_src = _body("_local_queue_rows")
for _need in ("_stack_flush_worker", "_worker", "_stack_entry_wait", "_stack_wait",
              "_auto_print_retry", "_scheduled_orders"):
    check(f"数据源接上 {_need}", _need in rows_src)

print("\n" + ("PASS 全部通过" if ok_all else "FAIL 有失败项"))
sys.exit(0 if ok_all else 1)
