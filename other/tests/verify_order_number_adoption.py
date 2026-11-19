# -*- coding: utf-8 -*-
"""晚到云端号接管本地临时号（重复单 ③）验收。

现场（2026-11-19 19:26 用户截图）：结算页订单明细里 HN20261119-0040（17:32:51）与
HN20261119-0041（17:42:00）两张单的文件、份数、页面、金额、备注（殷建泷）、接单设备
**完全相同** —— 同一次物理打印被记了两笔。日志复盘：

  17:32:36  分配本地临时号 L0059
  17:33:04  「订单号获取超时（>4s），改用本地临时号」→ L0060 落离线库并开打
  17:33:12  「订单已上报云端: HN20261119-0040」  ← 被主线程放弃的那个后台线程晚到的成功
  17:41:53  「已同步 1 个本地订单号到云端: HN20261119-0041」← 离线库 L0060 又换了个新号

三件事必须钉住：
  ① `offline_orders` 现在有 `request_id` 列，取号幂等键（per-标签页）能存能取，
     换号/改名后**不丢**（换号流程靠它拿回同一个号，而不是又申请一个新号）；
  ② 晚到的云端号会**接管**本地临时号：离线库那一行改名、jobs 的 order_number 改号、
     并写日志；此后 `list_pending_orders(like='%-L%')` 里不再有这张单 ⇒ 换号流程
     没有第二张单可报（这才是 0040/0041 的根因）；
  ③ 接线：超时分支在 `_cache_local_order_locally(...)` 之后调用 `_watch_late_order_number`；
     换号流程的离线库分支带 `request_id_of(num)`；退出前（closeEvent）还会再 poll 一次
     （后台线程刚上报、1 秒定时器还没轮到就关工具的情况）。

不需要打印机、不起 HTTP 服务、一次真网都不发；数据库落在临时目录。
用法: python tests/verify_order_number_adoption.py   （exit 0 = 全部通过）
"""
import io
import os
import re
import sys
import tempfile
from types import SimpleNamespace

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

import offline_sync as osync      # noqa: E402
import gui                        # noqa: E402

PASSED = 0
FAILED = 0
TMP = tempfile.mkdtemp(prefix="hn_adopt_verify_")
GUI_PATH = os.path.join(LOCAL_TOOL, "gui.py")


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")


def gui_src():
    with open(GUI_PATH, "r", encoding="utf-8") as f:
        return f.read()


FILES = [{"name": "第三次作业-选择结构1.docx", "pages": 4, "copies": 5, "duplex": True}]


def new_store(tag):
    db = os.path.join(TMP, f"{tag}.db")
    return osync.OfflineSync(db_path=db)


class _Timer:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


class _FakeSignal:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)


class _FakeQTimer:
    """替身 QTimer：SimpleNamespace 不是 QObject，真 QTimer(self) 会直接 TypeError。"""
    instances = []

    def __init__(self, parent=None):
        self.parent = parent
        self._interval = 0
        self.timeout = _FakeSignal()
        self.running = False
        _FakeQTimer.instances.append(self)

    def setInterval(self, ms):
        self._interval = ms

    def interval(self):
        return self._interval

    def start(self):
        self.running = True

    def isActive(self):
        return self.running

    def stop(self):
        self.running = False


_REAL_QTIMER = gui.QTimer
gui.QTimer = _FakeQTimer        # 本脚本全程用替身（库退出后不影响别处）


def make_fake(store, watch, timer, log):
    """假的 MainWindow：只带本次要走的属性/方法，方法本体用真的（unbound + 显式 self）。"""
    fake = SimpleNamespace(
        _offline_sync=store,
        _late_number_watch=watch,
        _late_number_timer=timer,
        _log=log.append,
        _save_config=lambda **kw: None,
        _refresh_offline_badge=lambda: None,
        _tabDisplayRefresh=SimpleNamespace(emit=lambda: None),
        _order_request_id=lambda tab: (getattr(tab, "order_req_id", "") or ""),
    )
    mw = gui.MainWindow
    # unbound 自取会丢掉 self，这里显式补上（SimpleNamespace 不会自动绑方法）
    fake._poll_late_order_numbers = lambda: mw._poll_late_order_numbers(fake)
    fake._watch_late_order_number = (
        lambda result, fallback, jobs, tab: mw._watch_late_order_number(
            fake, result, fallback, jobs, tab))
    fake._adopt_late_order_number = (
        lambda fallback, cloud, jobs, tab: mw._adopt_late_order_number(
            fake, fallback, cloud, jobs, tab))
    return fake


print("=" * 72)
print("① offline_orders.request_id：存得进、查得到、改名不丢")
print("=" * 72)
s1 = new_store("rid")
s1.save_order_offline("HN20261119-L0060", FILES, 11.0, "2026-11-19 17:33:04",
                     owner_name="霍楠", is_admin_print=False, remark="殷建泷",
                     request_id="9f2c0d1e4b8a4c7f")
check("request_id_of 读回同一条键",
      s1.request_id_of("HN20261119-L0060") == "9f2c0d1e4b8a4c7f",
      repr(s1.request_id_of("HN20261119-L0060")))
check("改名后 request_id 跟着走（换号不丢幂等键）",
      (s1.rename_order_number("HN20261119-L0060", "HN20261119-0041") == 1
       and s1.request_id_of("HN20261119-0041") == "9f2c0d1e4b8a4c7f"),
      repr(s1.request_id_of("HN20261119-0041")))
check("未知订单号 ⇒ 空串（调用方可以安全拼接）",
      s1.request_id_of("HN20261119-9999") == "")
check("set_request_id 可补写（晚到接管场景）",
      s1.set_request_id("HN20261119-0041", "abcd1234") == 1
      and s1.request_id_of("HN20261119-0041") == "abcd1234")
row = s1.find_pending_by_number("HN20261119-0041")
check("list_pending_orders 元组仍是 8 元（不许悄悄改元数）",
      len(row) == 8, f"len={len(row)}")
check("save_order_offline 默认 request_id 为空（旧调用方不受影响）",
      (s1.save_order_offline("HN20261119-L0061", FILES, 1.0, "2026-11-19 18:00:00")
       and s1.request_id_of("HN20261119-L0061") == ""))

print("=" * 72)
print("② 晚到云端号接管临时号：离线库改名 + jobs 改号 + 不再有 -L 可换")
print("=" * 72)
s2 = new_store("adopt")
s2.save_order_offline("HN20261119-L0060", FILES, 11.0, "2026-11-19 17:33:04",
                      owner_name="霍楠", is_admin_print=False, remark="殷建泷",
                      request_id="RID-0040")
tab = SimpleNamespace(jobs=[], order_req_id="RID-0040")
jobs = [SimpleNamespace(order_number="HN20261119-L0060", sent=True)]
tab.jobs = jobs
result = {"order_number": "HN20261119-L0060", "uploaded": False}
log = []
timer = _Timer()
fake = make_fake(s2, None, None, log)

# 后台线程超时那一刻：主线程已经用 -L 号开打并落离线库
fake._watch_late_order_number(result, "HN20261119-L0060", jobs, tab)
check("监听登记了一条（带 90 秒期限）",
      len(fake._late_number_watch) == 1
      and fake._late_number_watch[0]["fallback"] == "HN20261119-L0060")
fake._poll_late_order_numbers()
check("云端号还没到 ⇒ 不接管、也不清掉监听",
      jobs[0].order_number == "HN20261119-L0060" and len(fake._late_number_watch) == 1)

# 17:33:12 后台线程晚到拿到 0040 并上报成功
result["order_number"] = "HN20261119-0040"
fake._poll_late_order_numbers()
check("jobs 改成云端号（同一次打印只有一个身份）",
      jobs[0].order_number == "HN20261119-0040", jobs[0].order_number)
check("离线库那一行改名成 0040",
      s2.find_pending_by_number("HN20261119-0040") is not None
      and s2.find_pending_by_number("HN20261119-L0060") is None)
check("幂等键落到新号上（换号流程能拿回同一个号）",
      s2.request_id_of("HN20261119-0040") == "RID-0040", s2.request_id_of("HN20261119-0040"))
check("接管后有日志（现场能看出来发生了什么）",
      any("晚到的云端号已接管" in m and "HN20261119-0040" in m for m in log), repr(log))
check("监听出队 + 定时器停掉（不再空转）",
      fake._late_number_watch == []
      and _FakeQTimer.instances and _FakeQTimer.instances[-1].running is False)
check("★ 换号流程再也捞不到这张单（重复单根因已断）",
      all(r[1] != "HN20261119-L0060" for r in s2.list_pending_orders(like="%-L%")))
check("云端号已经用掉 ⇒ 不再申请新号（不会出现 0041）",
      not any(re.match(r"^HN\d{8}-L\d+$", r[1]) for r in s2.list_pending_orders()))

print("=" * 72)
print("②b 边界：-L/过期/同号 不误接管")
print("=" * 72)
s3 = new_store("edge")
s3.save_order_offline("HN20261119-L0070", FILES, 2.0, "2026-11-19 18:10:00")
j3 = [SimpleNamespace(order_number="HN20261119-L0070")]
log3 = []
t3 = _Timer()
f3 = make_fake(s3, None, None, log3)
r3 = {"order_number": "HN20261119-L0070"}
f3._watch_late_order_number(r3, "HN20261119-L0070", j3, None)
r3["order_number"] = "HN20261119-L0071"     # 后台也拿本地号（取号失败）
f3._poll_late_order_numbers()
check("-L 号一律不算云端号（不接管、继续等）",
      j3[0].order_number == "HN20261119-L0070" and len(f3._late_number_watch) == 1)
r3["order_number"] = "LOCAL-0071"
f3._poll_late_order_numbers()
check("LOCAL- 前缀同样不算（老版本号段）",
      j3[0].order_number == "HN20261119-L0070" and len(f3._late_number_watch) == 1)
f3._late_number_watch[0]["until"] = 0        # 人为过期
f3._poll_late_order_numbers()
check("过期条目丢弃 + 定时器停（不无限挂着）",
      f3._late_number_watch == []
      and _FakeQTimer.instances and _FakeQTimer.instances[-1].running is False)

s4 = new_store("same")
s4.save_order_offline("HN20261119-L0080", FILES, 1.0, "2026-11-19 18:20:00")
j4 = [SimpleNamespace(order_number="HN20261119-L0080")]
log4 = []
f4 = make_fake(s4, None, None, log4)
f4._adopt_late_order_number("HN20261119-L0080", "HN20261119-L0080", j4, None)
check("同号不改名、不打日志（幂等）",
      j4[0].order_number == "HN20261119-L0080" and log4 == [])

print("=" * 72)
print("③ 接线：超时分支 / 换号分支 / 退出前 poll")
print("=" * 72)
src = gui_src()


def branch_body(anchor, before=900, after=80):
    i = src.find(anchor)
    return src[max(0, i - before):i + after] if i >= 0 else ""


# 以「登记监听」那一行为锚点向前看：同一分支里必须先落离线库再登记（顺序反了就等于
# 只有监听、没有本地副本 ⇒ 用户删了标签页这单还是丢）
CALL = 'self._watch_late_order_number(result, result["order_number"], flat_jobs, tab)'
timeout_body = branch_body(CALL)
check("超时分支：先落离线库，再登记晚到号监听",
      timeout_body.find('reason="取号超时"') >= 0
      and timeout_body.find("_cache_local_order_locally") >= 0
      and timeout_body.find("_cache_local_order_locally") < timeout_body.find(CALL),
      timeout_body[:200].replace("\n", " / "))
check("超时分支：监听带上 flat_jobs 与 tab（要能改到 jobs 的号）", CALL in src)
check("离线库换号分支带 request_id_of（后端据此还回同一个号）",
      '"request_id": (self._offline_sync.request_id_of(num)' in src)
check("接管里改名离线库 + 落幂等键",
      "self._offline_sync.rename_order_number(fallback, cloud)" in src
      and "set_request_id(cloud, self._order_request_id(tab))" in src)
close_i = src.find("def closeEvent(self, event):\n        \"\"\"关闭窗口时检查未完成任务")
check("closeEvent 里退出前再 poll 一次（赶在定时器之前收尾）",
      close_i > 0 and src.find("self._poll_late_order_numbers()", close_i, close_i + 4000) > 0)
check("轮询定时器 1 秒、队列为空即停（不常驻空转）",
      "timer.setInterval(1000)" in src and "timer.stop()" in src)
check("offline_sync 里 request_id 列有 ALTER 迁移（老库就地升级）",
      'ALTER TABLE offline_orders ADD COLUMN request_id' in
      open(os.path.join(LOCAL_TOOL, "offline_sync.py"), "r", encoding="utf-8").read())

print("=" * 72)
print(f"PASSED {PASSED} / FAILED {FAILED}")
print("=" * 72)
sys.exit(0 if FAILED == 0 else 1)
