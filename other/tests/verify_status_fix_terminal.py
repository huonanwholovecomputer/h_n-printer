# -*- coding: utf-8 -*-
"""验收 · 订单状态人工订正的**前置条件**：当前状态必须已是终态（v7.19 / L1-2）

背景（审计结论 L1-2）
--------------------
`admin_order_status`（mobile_apps/printer-backend/app.py）原来只校验**目标**状态在白名单里，
从不看**当前**状态。于是「排队中 / 打印中 / 等待打印 / 文件传输中 / 预约 / 待添加」这些活跃态
也能被人工订正 —— 而活跃态意味着任务分发（push/pull）与预约调度还会把这张单捡起来打印、
设备可能正把纸打出来：页面点一下就把状态改成终态，纸照样出、账却按终态算，钱和纸两边错开。

本版口径（用户 2026-11-17 决定）
------------------------------
· 父订单**和**它的每个子任务当前状态都必须是终态（MANUAL_FIX_STATUSES 同一份清单）；
· 只要有一个还在活跃态 ⇒ 返回 **409** + 一句人话（中文、无 markdown 星号），什么都不改；
· 全是终态 ⇒ 照旧 200（订正能力不能被这道闸门弄丢）。

判据
----
A. 静态：app.py 的 admin_order_status 里有「当前状态必须是终态」的 409 分支；
B. 行为（真实 Flask + 真实 SQLite，复制一份后端到临时目录）：
   B1 八种活跃态父订单 → 409，且父/子状态、locked_at 一个字节都没变；
   B2 父订单终态但子任务活跃（单独一个 / 混在别的终态里）→ 409，子任务不变；
   B3 六种终态父订单 → 200，父+子一起改（含 timeout，v7.19）；
   B4 409 的文案：中文、提到「终态」、没有 markdown 星号。

用法：PYTHONPATH= PYTHONIOENCODING=utf-8 PYTHONUTF8=1 python other/tests/verify_status_fix_terminal.py
exit 0 = 全绿。
"""
import io
import os
import re
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "mobile_apps", "printer-backend", "app.py")
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

bad = 0


def chk(name, cond, detail=""):
    global bad
    if not cond:
        bad += 1
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + str(detail)) if (not cond and detail != "") else ""))


app_src = open(APP, encoding="utf-8").read()

TERMINAL = ("sent", "failed", "abandoned", "canceled", "rejected", "timeout")
ACTIVE = ("queued", "printing", "waiting", "downloading", "scheduled", "accepted",
          "offline_unknown", "reserved")

# ============================================================================
print("=== A. 静态：admin_order_status 必须有「当前状态是终态」前置 ===")

_i = app_src.find("def admin_order_status():")
_body = app_src[_i:_i + 8000] if _i >= 0 else ""
chk("A1 admin_order_status 存在", _i >= 0)
chk("A2 函数体里有 409 分支（拒绝活跃态订正）",
    re.search(r"\), 409", _body) is not None)
chk("A3 函数体里读了父订单当前状态并判定「不是终态」",
    re.search(r"(not in MANUAL_FIX_STATUSES|_TERMINAL)", _body) is not None
    and "终态" in _body)
chk("A4 函数体里连子任务状态一起读（status 列 + FROM order_files WHERE order_id）",
    re.search(r"SELECT[^;]*\bstatus\b[^;]*FROM order_files[\s\S]{0,80}?WHERE order_id",
              _body) is not None)
chk("A5 MANUAL_FIX_STATUSES 含 timeout（v7.19：超时单可订正）",
    re.search(r'MANUAL_FIX_STATUSES = \("sent", "failed", "abandoned", "canceled",\s*'
              r'"rejected", "timeout"\)', app_src) is not None)

# ============================================================================
print("\n=== B. 行为验收（真实 Flask + 真实 SQLite）===")

TMP = tempfile.mkdtemp(prefix="hn_stfix_term_")
DST = os.path.join(TMP, "printer-backend")
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(
    "__pycache__", "config.py", "config.py.example",
    "orders.db", "users.db", "uploads", "logs", "*.db-wal", "*.db-shm"))
os.chdir(DST)
os.environ["FLASK_ENV"] = "development"
sys.path.insert(0, DST)

import app as backend  # noqa: E402

backend.init_db()
backend.PRINTER_TOKEN = "test-token"
TOKEN = backend.PRINTER_TOKEN
client = backend.app.test_client()
HDR = {"X-Printer-Token": TOKEN}
URL = "/api/admin/order_status"

_seq = [0]


def mk_order(status, files, total_price=4.60):
    """files = [(status, total_price, locked_at), ...]；返回 order_id"""
    conn = backend.get_db()
    _seq[0] += 1
    cur = conn.execute(
        "INSERT INTO orders (file, copies, status, created_at, openid, duplex, page_count,"
        " total_price, order_number, source, is_admin_print)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("x.pdf", 1, status, "2026-11-17 10:00:00", "openid-term", "on", 2, total_price,
         "HN-TERM-%03d" % _seq[0], "wechat", 0))
    oid = cur.lastrowid
    for st, tp, locked in files:
        conn.execute(
            "INSERT INTO order_files (order_id, file_name, copies, page_count, total_price,"
            " status, created_at, page_range, locked_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (oid, "f.pdf", 1, 2, tp, st, "2026-11-17 10:00:00", "", locked))
    conn.commit()
    conn.close()
    return oid


def db_state(oid):
    conn = backend.get_db()
    row = conn.execute("SELECT status FROM orders WHERE id = ?", (oid,)).fetchone()
    files = conn.execute(
        "SELECT status, locked_at FROM order_files WHERE order_id = ? ORDER BY id",
        (oid,)).fetchall()
    conn.close()
    return (row["status"] if row else None,
            [(f["status"], f["locked_at"] or "") for f in files])


def post(payload):
    return client.post(URL, json=payload, headers=HDR)


# ---------------------------------------------------------------- B1 活跃态
print("\n-- B1 父订单还是活跃态：必须 409，且一个字节都不改 --")
for st in ACTIVE:
    oid = mk_order(st, [(st, 2.30, "2026-11-17 09:00:00")])
    before = db_state(oid)
    r = post({"order_id": oid, "status": "sent"})
    j = r.get_json() or {}
    after = db_state(oid)
    chk("B1 父订单 %s → 订正成 sent 被拒（409）" % st, r.status_code == 409,
        "%s %s" % (r.status_code, j))
    chk("B1 父订单 %s：父/子状态与 locked_at 全都没变" % st, after == before,
        "%s → %s" % (before, after))
    _msg = str(j.get("error") or j.get("message") or "")
    chk("B1 父订单 %s：回执给人话（提到终态、无星号）" % st,
        "终态" in _msg and "*" not in _msg, _msg)

print("\n-- B2 父订单是终态、但有子任务还在活跃态：必须 409 --")
oid = mk_order("sent", [("printing", 2.30, "2026-11-17 09:00:00")])
before = db_state(oid)
r = post({"order_id": oid, "status": "canceled"})
chk("B2 唯一子任务 printing → 409", r.status_code == 409, "%s %s" % (r.status_code, r.get_json()))
chk("B2 唯一子任务 printing：父/子状态没变", db_state(oid) == before, db_state(oid))

oid = mk_order("sent", [("sent", 2.30, ""), ("queued", 2.30, "2026-11-17 09:00:00")])
before = db_state(oid)
r = post({"order_id": oid, "status": "abandoned"})
chk("B2 终态子任务里混一个 queued → 409（不许只改到一半）",
    r.status_code == 409, "%s %s" % (r.status_code, r.get_json()))
chk("B2 混一个 queued：两个子任务都没变", db_state(oid) == before, db_state(oid))

# 反向对照：子任务全是终态时不能连坐 —— 否则这道闸门就等于「谁都改不了」
oid = mk_order("sent", [("sent", 2.30, ""), ("failed", 2.30, "")])
r = post({"order_id": oid, "status": "abandoned"})
chk("B2 子任务全是终态 → 放行（闸门只拦活跃态，不是拦一切）", r.status_code == 200,
    "%s %s" % (r.status_code, r.get_json()))

# ---------------------------------------------------------------- B3 终态放行
print("\n-- B3 六种终态父订单：订正必须成功（父+子一起改，含 timeout）--")
for cur in TERMINAL:
    target = "sent" if cur != "sent" else "canceled"
    oid = mk_order(cur, [(cur, 2.30, "")])
    r = post({"order_id": oid, "status": target})
    j = r.get_json() or {}
    o_st, f_list = db_state(oid)
    chk("B3 终态 %s → 订正成 %s：200" % (cur, target),
        r.status_code == 200 and j.get("success") and o_st == target,
        "%s %s → %s" % (r.status_code, j, o_st))
    chk("B3 终态 %s → %s：子任务一起改" % (cur, target),
        f_list == [(target, "")], f_list)

# 活跃态 → 活跃态目标本来就被白名单拦（400），确认 409 没把它顶掉
oid = mk_order("queued", [("queued", 2.30, "")])
r = post({"order_id": oid, "status": "printing"})
chk("B3 目标状态不在白名单 → 仍是 400（不是 409）", r.status_code == 400,
    "%s %s" % (r.status_code, r.get_json()))

print()
print("=" * 52)
print("订单状态订正·终态前置：全部通过 ✅" if bad == 0
      else "订单状态订正·终态前置：存在失败项 ❌ (%d)" % bad)
sys.exit(0 if bad == 0 else 1)
