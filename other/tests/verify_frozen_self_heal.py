# -*- coding: utf-8 -*-
"""冻结的预约单必须能自己走出来（🔴13 / 批次 D）

问题：`on_download_delayed` 把订单置 `schedule_frozen=1, scheduled_at=''`，而**唯一的解冻点**
是 `on_file_ready` 里「父单冻结 + 所有子任务都就绪」那一段。可设备掉线 / 上传失败 / 这个事件
从此不再来时，订单就永久冻结：`process_scheduled_orders` 见冻结直接 continue、
`expire_stale_scheduled_orders` 只扫 scheduled/waiting（downloading 扫不到）——
既不打印也不失败，零日志、无入口。顾客只会看到「预约到点了但没打」。

修法：新增 `release_stuck_frozen_orders()`（每 5 分钟跑一次），冻结超过 `FROZEN_STUCK_TIMEOUT`
（30 分钟）时：
  · 所有子任务已就绪 ⇒ 解冻并重排到 now+30s（等价于迟到的 file_ready），发 start_print；
  · 还有没就绪的 ⇒ 把这几份标 `timeout` 并写明原因，然后解冻 —— 订单要有结论、
    文件要进入正常清理流程；`timeout` 不在 NO_LONGER_PRINTABLE，设备日后真打出来仍可救回。

用法: python other/tests/verify_frozen_self_heal.py   （exit 0 = 全部通过）
"""
import io
import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")
APP_PY = os.path.join(SRC, "app.py")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


tmp = tempfile.mkdtemp(prefix="hn_frozen_heal_")
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

NOW = datetime.now()
EMITS = []


def ts(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


# 抓 start_print（解冻时要重排并通知设备）
_orig_emit = backend.emit


def _fake_emit(event, data=None, **kw):
    EMITS.append((event, data))
    return None


backend.emit = _fake_emit


def table_cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def insert(table, **values):
    conn = backend.get_db()
    cols = table_cols(conn, table)
    data = {k: v for k, v in values.items() if k in cols}
    cur = conn.execute(
        f"INSERT INTO {table} ({', '.join(data)}) VALUES ({', '.join('?' for _ in data)})",
        tuple(data.values()))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def make_order(frozen_min_ago=40, of_status="waiting", frozen_at=True, scheduled_at=""):
    oid = insert("orders", file="a.docx", copies=1, status="scheduled",
                 created_at=ts(120), page_count=2, total_price=0.4,
                 schedule_mode="at", scheduled_at=scheduled_at,
                 schedule_frozen=1,
                 schedule_frozen_at=ts(frozen_min_ago) if frozen_at else "")
    of = insert("order_files", order_id=oid, file_name="a.docx", copies=1,
                page_count=2, status=of_status, created_at=ts(120), duplex="on")
    return oid, of


def read(oid, of_id):
    conn = backend.get_db()
    o = conn.execute("SELECT status, schedule_frozen, schedule_frozen_at AS fa, "
                     "scheduled_at AS sat FROM orders WHERE id = ?", (oid,)).fetchone()
    f = conn.execute("SELECT status, COALESCE(reject_reason,'') AS rr FROM order_files WHERE id = ?",
                     (of_id,)).fetchone()
    conn.close()
    return o, f


# ══════════════════════════════════════════════════════════════════════════
print("\n=== 0. 迁移：orders.schedule_frozen_at 列存在 ===")
_c = backend.get_db()
_has_col = "schedule_frozen_at" in table_cols(_c, "orders")
_c.close()
check("0-a 已加 orders.schedule_frozen_at 列（自愈的计时基准）", _has_col)

print("\n=== 1. 冻结 40 分钟、文件已就绪 ⇒ 解冻 + 重排到 30 秒后 + 发 start_print ===")
O1, F1 = make_order(frozen_min_ago=40, of_status="waiting")
EMITS.clear()
backend.release_stuck_frozen_orders()
_o1, _f1 = read(O1, F1)
check("1-a 解冻（schedule_frozen=0）", int(_o1["schedule_frozen"] or 0) == 0, _o1["schedule_frozen"])
check("1-b 冻结时刻已清空", not str(_o1["fa"] or ""), _o1["fa"])
check("1-c 重排了 scheduled_at", bool(str(_o1["sat"] or "")), _o1["sat"])
check("1-d 子任务仍是 waiting（没被打成终态）", _f1["status"] == "waiting", _f1["status"])
_sp = [e for e in EMITS if e[0] == "start_print"]
check("1-e 解冻后向设备发了 start_print", len(_sp) == 1, len(_sp))
check("1-f start_print 带上了就绪的 task_ids",
      bool(_sp) and _sp[0][1].get("task_ids") == [F1],
      _sp[0][1] if _sp else "")

print("\n=== 2. 冻结 40 分钟、文件还没传上来 ⇒ 标 timeout + 解冻（不许永远挂着）===")
O2, F2 = make_order(frozen_min_ago=40, of_status="downloading")
EMITS.clear()
backend.release_stuck_frozen_orders()
_o2, _f2 = read(O2, F2)
check("2-a 没就绪的那份标 timeout（不是 failed）", _f2["status"] == "timeout", _f2["status"])
check("2-b 原因写明「文件…一直没就绪」", "没就绪" in _f2["rr"], _f2["rr"])
check("2-c 订单解冻了（否则永远停在冻结）", int(_o2["schedule_frozen"] or 0) == 0, _o2["schedule_frozen"])
check("2-d 父订单聚合成 timeout（有结论）", _o2["status"] == "timeout", _o2["status"])

print("\n=== 3. 刚冻结 5 分钟 ⇒ 不许动（设备可能只是慢）===")
O3, F3 = make_order(frozen_min_ago=5, of_status="downloading")
backend.release_stuck_frozen_orders()
_o3, _f3 = read(O3, F3)
check("3-a 仍冻结", int(_o3["schedule_frozen"] or 0) == 1, _o3["schedule_frozen"])
check("3-b 子任务仍 downloading", _f3["status"] == "downloading", _f3["status"])

print("\n=== 4. 旧数据没有 schedule_frozen_at ⇒ 退回 scheduled_at / created_at 判断 ===")
O4, F4 = make_order(frozen_min_ago=40, of_status="waiting", frozen_at=False,
                    scheduled_at=ts(200))
backend.release_stuck_frozen_orders()
_o4, _f4 = read(O4, F4)
check("4-a 旧行也被自愈（解冻）", int(_o4["schedule_frozen"] or 0) == 0, _o4["schedule_frozen"])

print("\n=== 5. 非冻结订单零回归 ===")
O5 = insert("orders", file="b.docx", copies=1, status="scheduled", created_at=ts(40),
            page_count=2, total_price=0.4, schedule_mode="at", scheduled_at=ts(-5),
            schedule_frozen=0, schedule_frozen_at="")
F5 = insert("order_files", order_id=O5, file_name="b.docx", copies=1, page_count=2,
            status="waiting", created_at=ts(40), duplex="on")
backend.release_stuck_frozen_orders()
_o5, _f5 = read(O5, F5)
check("5-a 没冻结的订单不动", _f5["status"] == "waiting" and int(_o5["schedule_frozen"] or 0) == 0,
      f'{_f5["status"]}/{_o5["schedule_frozen"]}')

print("\n=== 6. 静态：定时任务已注册 + 解冻逻辑只这一处 ===")
_src = open(APP_PY, encoding="utf-8", errors="replace").read()
check("6-a 定时任务注册了 release_stuck_frozen",
      'scheduler.add_job(release_stuck_frozen_orders' in _src)
check("6-b 冻结时写了 schedule_frozen_at",
      "schedule_frozen = 1, schedule_frozen_at = ?" in _src)
check("6-c file_ready 解冻时清掉 schedule_frozen_at",
      "schedule_frozen = 0, schedule_frozen_at = ''," in _src)
check("6-d 自愈时长是可配常量而不是魔法数", "FROZEN_STUCK_TIMEOUT = " in _src)

backend.emit = _orig_emit
print("\n" + "=" * 60)
if ok_all:
    print("冻结自愈验收: 全部通过")
    sys.exit(0)
print("冻结自愈验收: 存在失败 ❌")
sys.exit(1)
