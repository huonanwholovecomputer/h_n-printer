# -*- coding: utf-8 -*-
"""预约单 start_print 兜底重发的「节流 + 不判失败」验收

现场（2026-11-10，订单 HN20261110-0005 / id 1252）：
  用户 13:35 的预约单到点了，本机日志里只有一行行
      「⏸ 预约打印订单 #1252：含双面文件，而当前打印机「HP LaserJet 1020」不支持自动双面
        （需人工翻纸）—— 本机已挂起（不回报失败），请人工点「开始打印」」
  之后 35 分钟里每 30 秒一条「☁ 预约单 #1252 收到开始打印指令」，却什么都不发生。

根因（代码事实，本脚本逐条钉住）：
  · 双面单遇到手动双面机型，客户端 `_hold_for_manual_duplex()` 是**有意挂起**（不回报失败），
    随后 `_cleanup_scheduled_order(..., "需人工翻纸，转为人工打印")` 结束预约状态机；
  · 服务端 `process_scheduled_orders()` 每 30 秒扫 `waiting` 子任务、到点就发 start_print，
    **没有任何次数上限**；客户端状态机早没了 ⇒ 指令每次都落空 ⇒ 无限重发 + 无限刷日志。
  · 同时 `order_files.status` 永远停在 waiting：既不算失败、也不会自己好 —— 单子卡死。

修法（本次）：
  ① 服务端 `_start_print_push_due()` 给兜底重发上节流：前 10 次 30 秒节奏，之后 600 秒一次，
     最多 16 次就停发（`[SKIP]` 一行日志说明），**且绝不因为「没人理」就把子任务判失败** ——
     人工点「开始打印」照常能打、打完照常回报成功；
  ② 客户端 `_explain_ignored_start_print()` 把「指令为什么落空」讲清楚（10 分钟一次），
     日志不再只重复「收到开始打印指令」。

用法: python other/tests/verify_scheduled_start_print_backoff.py   （exit 0 = 全部通过）
"""
import io
import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


tmp = tempfile.mkdtemp(prefix="hn_start_print_")
dst = os.path.join(tmp, "printer-backend")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs", "venv", "*.bak-*"))
sys.path.insert(0, dst)
os.chdir(dst)

try:
    import app as backend
    print(f"imported app.py from {dst}")
except Exception:
    traceback.print_exc()
    sys.exit("无法导入 app.py")

backend.init_db()

CLIENT = "R7000-AHP10-test"
EMITS = []


class _FakeSocketIO:
    """只实现 `emit(event, data, to=...)`。"""

    def emit(self, event, data=None, to=None, **kw):
        EMITS.append((event, dict(data or {}), to))


backend.socketio = _FakeSocketIO()
backend.printer_clients[CLIENT] = {"sid": "sid-test", "heartbeat": datetime.now()}


def table_cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def insert(table, **values):
    """按数据库真实列裁剪后插入（init_db 的迁移会按版本加减列，不写死列名）。"""
    conn = backend.get_db()
    allowed = table_cols(conn, table)
    data = {k: v for k, v in values.items() if k in allowed}
    cols = ", ".join(data)
    qs = ", ".join("?" for _ in data)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", tuple(data.values()))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


NOW = datetime.now()
PAST = (NOW.replace(microsecond=0)).strftime("%Y-%m-%d %H:%M:%S")
ORDER = insert("orders", file="sched.docx", copies=1, status="printing",
               created_at=PAST, page_count=2, total_price=0.4,
               schedule_mode="at", scheduled_at=PAST, schedule_frozen=0,
               target_client=CLIENT)
OF = insert("order_files", order_id=ORDER, file_name="sched.docx", copies=1,
            page_count=2, status="waiting", created_at=PAST, duplex="on",
            operator_client=CLIENT)   # 子任务被该设备领取：start_print 发给它


def of_status():
    conn = backend.get_db()
    row = conn.execute("SELECT status, COALESCE(reject_reason,'') AS rr "
                       "FROM order_files WHERE id=?", (OF,)).fetchone()
    conn.close()
    return row["status"], row["rr"]


# ══════════════════════════════════════════════════════════════════════════
print("\n=== 1. 到点 → 发一次 start_print；紧接着再扫不发（30 秒节奏被守住）===")
EMITS.clear()
backend.process_scheduled_orders()
_sends = [e for e in EMITS if e[0] == "start_print"]
check("首轮扫描发出 1 条 start_print", len(_sends) == 1, f"{len(_sends)} 条")
check("推给的是这个子任务所属设备", bool(_sends) and _sends[0][2] == "sid-test",
      f"{_sends[0][2] if _sends else None}")
check("task_ids 带的是子任务 id", bool(_sends) and _sends[0][1].get("task_ids") == [OF],
      f"{_sends[0][1] if _sends else None}")
EMITS.clear()
backend.process_scheduled_orders()
check("同一个 30 秒窗口内再扫不发（这就是旧行为每 30 秒刷一条的源头）",
      len([e for e in EMITS if e[0] == "start_print"]) == 0, f"{len(EMITS)} 条")

print("\n=== 2. 节奏与上限：前 10 次 30 秒，之后 600 秒，16 次封顶 ===")
_id = OF + 10000        # 用独立 id 测纯函数，别动真实子任务的计数
_t = 1_000_000.0
_sent = 0
_first_gap_ok = True
for i in range(12):
    if backend._start_print_push_due(_id, _t)[0]:
        _sent += 1
    _t += 30.0
check("30 秒节奏下前 10 次都发得出去", _sent >= 10, f"实际 {_sent} 次")
# 第 11、12 次必须已经切到 600 秒慢速档：30 秒推进不再有发
_t = 1_000_000.0 + 30.0 * 10
_due11, _ = backend._start_print_push_due(_id, _t)
check("第 11 次不再按 30 秒发（切到 600 秒慢速档）", _due11 is False, f"_due11={_due11}")
_due11b, _ = backend._start_print_push_due(_id, _t + 601.0)
check("过了 600 秒才再发一次", _due11b is True, f"_due11b={_due11b}")
# 一路推到封顶
_t = _t + 601.0
_sent2 = 0
_stopped_seen = False
_just_stopped_count = 0
for _ in range(30):
    _t += 601.0
    _due, _just = backend._start_print_push_due(_id, _t)
    if _due:
        _sent2 += 1
    if _just:
        _just_stopped_count += 1
        _stopped_seen = True
check("推到上限后出现「刚停发」的回报且只报一次", _stopped_seen and _just_stopped_count == 1,
      f"stopped={_stopped_seen} count={_just_stopped_count}")
_total = backend._start_print_attempts[_id]["n"]
check(f"总发送次数封顶在 _START_PRINT_MAX={backend._START_PRINT_MAX}",
      _total == backend._START_PRINT_MAX, f"实际 {_total} 次")
check("停发后再扫一律不发", backend._start_print_push_due(_id, _t + 99999.0) == (False, False),
      "仍返回 (False, False)")

print("\n=== 3. 铁律：停下来 ≠ 判失败（子任务必须仍是 waiting、无人写失败原因）===")
_st, _rr = of_status()
check("子任务状态仍是 waiting", _st == "waiting", _st)
check("没有写任何失败原因（不许自动判失败）", _rr == "", repr(_rr))
EMITS.clear()
for _ in range(5):
    backend.process_scheduled_orders()
check("连扫 5 轮也不改状态（人工点「开始打印」随时能打）", of_status()[0] == "waiting",
      of_status()[0])

print("\n=== 4. 子任务离开 waiting ⇒ 丢掉它的节流计数（内存不涨）===")
check("离开前计数里还有它", OF in backend._start_print_attempts,
      f"{sorted(backend._start_print_attempts)[:5]}")
conn = backend.get_db()
conn.execute("UPDATE order_files SET status='sent' WHERE id=?", (OF,))
conn.commit()
conn.close()
backend.process_scheduled_orders()
check("打完（离开 waiting）后计数被清掉", OF not in backend._start_print_attempts,
      f"{sorted(backend._start_print_attempts)[:5]}")

print("\n=== 5. 已冻结 / 未到点 / 已取消 一律不发（零回归）===")


def fresh_order(**over):
    vals = dict(file="x.docx", copies=1, status="printing", created_at=PAST,
                page_count=1, schedule_mode="at", scheduled_at=PAST,
                schedule_frozen=0, target_client=CLIENT)
    vals.update(over)
    oid = insert("orders", **vals)
    fid = insert("order_files", order_id=oid, file_name="x.docx", copies=1,
                 page_count=1, status="waiting", created_at=PAST)
    return oid, fid


for label, over, expect in [
    ("已冻结（本机未就绪）不发", dict(schedule_frozen=1), 0),
    ("未来时间点不发", dict(scheduled_at="2099-01-01 00:00:00"), 0),
    ("订单已取消不发", dict(status="canceled"), 0),
]:
    EMITS.clear()
    _oid, _fid = fresh_order(**over)
    backend.process_scheduled_orders()
    _n = len([e for e in EMITS if e[0] == "start_print" and e[1].get("task_ids") == [_fid]])
    check(label, _n == expect, f"{_n} 条")
    conn = backend.get_db()
    conn.execute("UPDATE order_files SET status='sent' WHERE id=?", (_fid,))
    conn.commit()
    conn.close()

print("\n=== 6. 客户端侧：这段修复的静态接线在位（防止有人把节流摘掉/把静默丢弃写回去）===")
GUI = os.path.join(REPO, "local_print_tool", "gui.py")
gui_src = open(GUI, encoding="utf-8").read()
check("★ 客户端 start_print 落空时会讲原因（不再静默 return）",
      "_explain_ignored_start_print(order_id, task_ids)" in gui_src)
check("★ 讲原因有 10 分钟节流（否则后端重发又会刷屏）",
      "self._start_print_ignored_ts" in gui_src and "< 600" in gui_src)
_handle = gui_src.split("def _explain_ignored_start_print", 1)
check("★ 说明里必须点出「不会自动开打」（行为不得被改）",
      len(_handle) > 1 and "绝不因为收到兜底指令就自动开打" in _handle[1][:2000])

print()
if ok_all:
    print("全部通过 ✅")
    sys.exit(0)
print("存在失败 ❌")
sys.exit(1)
