# -*- coding: utf-8 -*-
"""云端订单落地：每文件「封面单独打印」+「排除末尾空白页」（2026-11-02 用户需求）

移动端（小程序/APP）现在可以按**每个文件**选这两项，后端随 `task_msg["options"]`
下发 `simplex_pages` / `blank_tail_exclude`。设备端必须：

  ① 「排除末尾空白页」：空白页判定只有本机有 PyMuPDF ⇒ 真排除，并**立刻回报**
     `page_range_truncated`，后端据此重算价格（排掉的空白页不收钱）；排除结果落在
     `page_range` 上（`blank_undo_range` 记原值，可一键恢复），不新增第二口径。
  ② 「封面单独打印」：归一化后写进 `job.simplex_pages`；填错 / 超页数 / 不在页码范围内
     ⇒ **把这一单打回**（`report_fail` + `reject_task`），绝不静默按整份双面打出去。
  ③ 旧后端不下发这两个字段 ⇒ 行为与改动前完全一致（零回归）。

跑法：python tests/verify_cloud_simplex_blank.py（退出码 0 = 全绿）
"""
import os
import shutil
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")

_SANDBOX = tempfile.mkdtemp(prefix="hn_simplex_cloud_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

_app = QApplication.instance() or QApplication([])
QMessageBox.information = staticmethod(lambda *a, **kw: QMessageBox.Ok)
QMessageBox.warning = staticmethod(lambda *a, **kw: QMessageBox.Ok)
QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.Yes)

import gui          # noqa: E402
import paths        # noqa: E402
import printer_config as pc  # noqa: E402
import cloud_client as cc    # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ── 造一份 31 页、末尾 2 页空白的 PDF（空白页判定要真栅格）──────────────
from reportlab.pdfgen import canvas  # noqa: E402

PDF_31 = os.path.join(_SANDBOX, "simplex31.pdf")
_c = canvas.Canvas(PDF_31)
for i in range(1, 32):
    if i <= 29:
        _c.setFont("Helvetica", 18)
        _c.drawString(80, 700, f"page {i} content")
    _c.showPage()
_c.save()


class FakeTask:
    """`_add_cloud_task_to_new_tab` 会读的字段；两个新字段默认不传（旧后端）。"""

    def __init__(self, task_id=1, order_id=501, pdf=PDF_31, simplex_pages="",
                 blank_tail_exclude=False, duplex="on", page_range="", copies=1):
        self.task_id = task_id
        self.order_id = order_id
        self.status = "ready"
        self.auto_print = False
        self.schedule_mode = "now"
        self.file_name = os.path.basename(pdf)
        self.local_path = pdf
        self.source_md5 = ""
        self.image_orientation = "auto"
        self.copies = copies
        self.duplex = duplex
        self.page_range = page_range
        self.order_number = f"HN20261102-{order_id}"
        self.delivery_enabled = False
        self.delivery_location = ""
        self.urgency = "低"
        self.cover_page = False
        self.cover_page_price = 0.1
        self.remark = ""
        self.is_admin_print = False
        self.owner_name = ""
        self.bound_owner_name = ""
        self.simplex_pages = simplex_pages
        self.blank_tail_exclude = blank_tail_exclude


class CloudStub:
    def __init__(self):
        self.truncated = []
        self.failed = []
        self.rejected = []

    def accept_task(self, task_id):
        pass

    def report_page_range_truncated(self, task_id, original_range, effective_range, total_pages):
        self.truncated.append((task_id, original_range, effective_range, total_pages))

    def report_fail(self, task_id, error):
        self.failed.append((task_id, error))

    def reject_task(self, task_id):
        self.rejected.append(task_id)


def new_win(stub=None):
    w = gui.MainWindow(config_path=paths.config_path())
    w._cloud_client = stub
    w._start_print_worker = lambda jobs, **kw: True
    w._manual_flip_required_for = lambda jobs: False
    w._auto_print_queue = {}
    w._auto_print_timers = {}
    w._enqueued_cloud_tasks = set()
    w._auto_print_retry = []
    w._config.tabs = {"1": gui.TabSettings()}
    w._current_tab = "1"
    w._config.active_tab = "1"
    w._rebuild_table()
    w._refresh_tab_display()
    return w


print("=== ① check_simplex_pages 与后端同一套判据/话术 ===")
check("①a 合法串归一化", pc.check_simplex_pages("", 31, "1, 16-23") == ("1,16-23", ""),
      pc.check_simplex_pages("", 31, "1, 16-23"))
check("①b 格式错 → 文案",
      pc.check_simplex_pages("", 31, "23-4")[1]
      == "「封面单独打印」页码格式不正确：23-4（应为如 1-5 或 7）",
      pc.check_simplex_pages("", 31, "23-4"))
check("①c 超页数 → 文案",
      pc.check_simplex_pages("", 31, "1,99")[1]
      == "「封面单独打印」第 99 页超出文件总页数（文件共 31 页）",
      pc.check_simplex_pages("", 31, "1,99"))
check("①d 不在范围内 → 文案",
      pc.check_simplex_pages("2-31", 31, "1")[1]
      == "「封面单独打印」第 1 页不在页码范围「2-31」内",
      pc.check_simplex_pages("2-31", 31, "1"))
check("①e 空串 = 未启用", pc.check_simplex_pages("", 31, "") == ("", ""))
check("①f 页数未知时只查格式", pc.check_simplex_pages("", 0, "1,16-23") == ("1,16-23", ""))

print("\n=== ② 「排除末尾空白页」：真排除 + 回报截断 ===")
stub2 = CloudStub()
w2 = new_win(stub2)
key2 = w2._add_cloud_tasks_to_new_tab([FakeTask(task_id=21, order_id=601, blank_tail_exclude=True)])
jobs2 = w2._config.tabs[key2].jobs
check("②a 文件已落地", len(jobs2) == 1, len(jobs2))
if jobs2:
    j2 = jobs2[0]
    check("②b 末尾 2 页空白被排除（page_range → 1-29）", j2.page_range == "1-29", j2.page_range)
    check("②c blank_undo_range 记原值（可一键恢复）", j2.blank_undo_range == "", j2.blank_undo_range)
    check("②d blank_tail 照旧标记（UI 显示 N 页）", j2.blank_tail == 2, j2.blank_tail)
    check("②e 有效页数按 29 页算（计费口径同一处）",
          pc._count_pages_in_range(j2.page_range, j2.page_count) == 29,
          pc._count_pages_in_range(j2.page_range, j2.page_count))
    check("②f 局面价格低于未排除（29 页 15 张 vs 31 页 16 张）",
          pc.calc_cost(31, 1, "on", 0.3, 0.4, "1-29")[0] < pc.calc_cost(31, 1, "on", 0.3, 0.4, "")[0],
          (pc.calc_cost(31, 1, "on", 0.3, 0.4, "1-29")[0], pc.calc_cost(31, 1, "on", 0.3, 0.4, "")[0]))
check("②g 回报了 page_range_truncated（1-31 → 1-29）",
      stub2.truncated == [(21, "", "1-29", 31)], stub2.truncated)

print("\n=== ③ 「封面单独打印」：归一化后落到 job ===")
stub3 = CloudStub()
w3 = new_win(stub3)
key3 = w3._add_cloud_tasks_to_new_tab([FakeTask(task_id=31, order_id=602, simplex_pages="1, 16-23")])
jobs3 = w3._config.tabs[key3].jobs
check("③a 文件已落地", len(jobs3) == 1, len(jobs3))
if jobs3:
    j3 = jobs3[0]
    check("③b simplex_enabled=True", j3.simplex_enabled is True)
    check("③c 页码归一化", j3.simplex_pages == "1,16-23", j3.simplex_pages)
    check("③d page_range 不被改写", j3.page_range == "", j3.page_range)
    check("③e job_simplex_pages 可被计费读到", pc.job_simplex_pages(j3) == "1,16-23",
          pc.job_simplex_pages(j3))
    check("③f 31 页 + 1,16-23 单面 = ¥7.10",
          abs(pc.calc_cost(31, 1, "on", 0.3, 0.4, "", pc.job_simplex_pages(j3))[0] - 7.1) < 1e-9,
          pc.calc_cost(31, 1, "on", 0.3, 0.4, "", pc.job_simplex_pages(j3)))
check("③g 没误报截断", stub3.truncated == [], stub3.truncated)

print("\n=== ④ 填错就打回（不静默按整份双面打）===")
stub4 = CloudStub()
w4 = new_win(stub4)
key4 = w4._add_cloud_tasks_to_new_tab([FakeTask(task_id=41, order_id=603, simplex_pages="1,99")])
check("④a 打回：报失败并带原因", stub4.failed
      and stub4.failed[0][0] == 41
      and stub4.failed[0][1] == "「封面单独打印」第 99 页超出文件总页数（文件共 31 页）",
      stub4.failed)
check("④b 打回：reject_task 被调用", stub4.rejected == [41], stub4.rejected)
check("④c 打回：文件不落进标签页",
      all(len(t.jobs) == 0 for t in w4._config.tabs.values()),
      {k: len(v.jobs) for k, v in w4._config.tabs.items()})

stub4b = CloudStub()
w4b = new_win(stub4b)
w4b._add_cloud_tasks_to_new_tab([FakeTask(task_id=42, order_id=604,
                                          page_range="2-31", simplex_pages="1")])
check("④d 与页码范围冲突也打回",
      stub4b.failed and stub4b.failed[0][1] == "「封面单独打印」第 1 页不在页码范围「2-31」内",
      stub4b.failed)

stub4c = CloudStub()
w4c = new_win(stub4c)
w4c._add_cloud_tasks_to_new_tab([FakeTask(task_id=43, order_id=605, simplex_pages="23-4")])
check("④e 格式错也打回",
      stub4c.failed and stub4c.failed[0][1].startswith("「封面单独打印」页码格式不正确"),
      stub4c.failed)

print("\n=== ⑤ 旧后端（不下发两个字段）：行为零变化 ===")
stub5 = CloudStub()
w5 = new_win(stub5)
key5 = w5._add_cloud_tasks_to_new_tab([FakeTask(task_id=51, order_id=606)])
jobs5 = w5._config.tabs[key5].jobs
check("⑤a 文件照旧落地", len(jobs5) == 1, len(jobs5))
if jobs5:
    j5 = jobs5[0]
    check("⑤b page_range 原样（空=全部）", j5.page_range == "", j5.page_range)
    check("⑤c 没有 simplex 开关", j5.simplex_enabled is False and j5.simplex_pages == "")
    check("⑤d 没有排除记录", j5.blank_undo_range is None, j5.blank_undo_range)
    check("⑤e 只标不排（空白页仍然计入）",
          pc._count_pages_in_range(j5.page_range, j5.page_count) == 31,
          pc._count_pages_in_range(j5.page_range, j5.page_count))
check("⑤f 不回报截断、不打回", stub5.truncated == [] and stub5.failed == [] and stub5.rejected == [])

print("\n=== ⑥ 单面任务带 simplex：忽略、不报错 ===")
stub6 = CloudStub()
w6 = new_win(stub6)
key6 = w6._add_cloud_tasks_to_new_tab([FakeTask(task_id=61, order_id=607, duplex="off",
                                                simplex_pages="1")])
jobs6 = w6._config.tabs[key6].jobs
check("⑥a 文件落地且不带 simplex", len(jobs6) == 1
      and jobs6[0].simplex_enabled is False and jobs6[0].simplex_pages == "")
check("⑥b 不因 simplex 打回", stub6.failed == [])

print("\n=== ⑦ CloudTask 解析：options 优先、缺省零变化 ===")
t7 = cc.CloudTask({"task_id": 71, "options": {"simplex_pages": "1,16-23",
                                              "blank_tail_exclude": 1}})
check("⑦a options 里的两字段被读进来",
      t7.simplex_pages == "1,16-23" and t7.blank_tail_exclude is True,
      (t7.simplex_pages, t7.blank_tail_exclude))
check("⑦b to_dict 带上（重印/本地暂存不丢）",
      t7.to_dict()["simplex_pages"] == "1,16-23"
      and t7.to_dict()["blank_tail_exclude"] is True)
t7b = cc.CloudTask({"task_id": 72})
check("⑦c 旧后端不传 ⇒ 空/False", t7b.simplex_pages == "" and t7b.blank_tail_exclude is False)
check("⑦d 字段在 __slots__ 里（不会再 AttributeError）",
      "simplex_pages" in cc.CloudTask.__slots__ and "blank_tail_exclude" in cc.CloudTask.__slots__)

print()
if ok_all:
    print("✅ 全部通过")
    shutil.rmtree(_SANDBOX, ignore_errors=True)
    sys.exit(0)
print("❌ 有失败项")
sys.exit(1)
