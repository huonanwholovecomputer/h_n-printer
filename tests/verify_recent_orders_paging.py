#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收 · /api/admin/recent_orders 翻页（2026-11 task-1 C5）

背景：这个接口原来只返回「最近 N 条」（N = 超管在设置里配的 10/20/…），结算页/全站订单
想往前翻就必须改配置——而配置是全站共享的。现在补翻页：
  · 带 `page`（或 `per_page`）→ 翻页模式，返回 {success, orders, total, page, per_page, total_pages}；
  · **不带 page → 结构与老版一致**（{success, limit, orders}，只多一个 total），老前端不用跟版；
  · per_page 只接受 10/20/50/100（对齐前端的每页选择器），缺省 = 配置的默认条数；
  · 参数非法 → 400 且 message 明确；越界页 → 200 空列表 + 真实 total_pages（前端自己收敛页码，
    不要因为用户点了「下一页」而弹错误）。

用法（本机只有 Python312）：
  PYTHONPATH= PYTHONIOENCODING=utf-8 \
    C:/Users/Administrator/AppData/Local/Programs/Python/Python312/python.exe \
    tests/verify_recent_orders_paging.py
exit 0 = 全绿。
"""
import io
import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title):
    print(f"\n=== {title} ===")


TMP = tempfile.mkdtemp(prefix="hn_recent_paging_")
DST = os.path.join(TMP, "printer-backend")
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs", "venv",
    "config.py", "stack_intent.json", "recent_orders_config.json",
    "device_pin_order.json", "printer_claim.json", "devices.json"))

sys.path.insert(0, DST)
os.chdir(DST)
os.environ["FLASK_ENV"] = "development"
try:
    import app as backend
    print(f"imported app.py from {DST}")
except Exception:
    traceback.print_exc()
    sys.exit("无法导入 app.py")

backend.init_db()

ADMIN_OID = "openid-admin"
GUEST_OID = "openid-guest"
backend.compute_role = lambda oid: "admin" if oid == ADMIN_OID else "guest"
ADMIN = {"Authorization": f"Bearer {backend.token_serializer.dumps(ADMIN_OID)}"}
GUEST = {"Authorization": f"Bearer {backend.token_serializer.dumps(GUEST_OID)}"}
client = backend.app.test_client()

N = 23
TOTAL = N


def seed():
    """插 23 张单，created_at 递增（越靠后越新），第 1 张带 reject_reason 与人工订正留痕。"""
    conn = backend.get_db()
    try:
        base = datetime(2026, 10, 1, 8, 0, 0)
        for i in range(N):
            ts = (base + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
            by = "霍楠" if i == 0 else ""
            at = "2026-11-01 09:00:00" if i == 0 else ""
            conn.execute(
                """INSERT INTO orders (file, copies, status, created_at, openid, order_number,
                                       page_count, total_price, source, owner_name,
                                       is_admin_print, manual_status_by, manual_status_at)
                   VALUES (?, 1, 'sent', ?, 'openid-user', ?, 2, 1.0, 'wechat', '测试用户', 0, ?, ?)""",
                (f"单 {i}", ts, f"HNPAGE-{i:03d}", by, at))
            oid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            reason = "打印机断线，打印结果未知，已超时" if i == 0 else ""
            conn.execute(
                """INSERT INTO order_files (order_id, file_name, copies, page_count,
                                            total_price, status, duplex, created_at,
                                            page_range, reject_reason)
                   VALUES (?, ?, 1, 2, 1.0, 'sent', 'on', ?, '', ?)""",
                (oid, f"f{i}.pdf", ts, reason))
        conn.commit()
    finally:
        conn.close()


seed()


def get(**params):
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    url = "/api/admin/recent_orders" + (f"?{qs}" if qs else "")
    return client.get(url, headers=ADMIN)


# ==================== ① 不带 page：结构兼容 ====================
section("① 不带 page：老结构兼容（只多一个 total）")
r = get()
j = r.get_json() or {}
check("200", r.status_code == 200, f"{r.status_code} {j}")
check("仍返回 success/limit/orders 三个老字段",
      {"success", "limit", "orders"} <= set(j), str(sorted(j)))
check("orders 是列表且长度 = min(配置 limit, total)",
      isinstance(j.get("orders"), list) and len(j["orders"]) == min(j.get("limit", 0), TOTAL),
      f"len={len(j.get('orders') or [])} limit={j.get('limit')}")
check("额外带了 total（新增字段，不影响老前端）", j.get("total") == TOTAL,
      f"total={j.get('total')} expect={TOTAL}")
check("不带 page 时不返回 page/per_page/total_pages（老结构不变）",
      not any(k in j for k in ("page", "per_page", "total_pages")),
      str([k for k in ("page", "per_page", "total_pages") if k in j]))

# ==================== ② 翻页基本形状 ====================
section("② page=1&per_page=10")
r = get(page=1, per_page=10)
j = r.get_json() or {}
check("200", r.status_code == 200, str(j)[:120])
check("返回体含 total/page/per_page/total_pages",
      {"success", "orders", "total", "page", "per_page", "total_pages"} <= set(j),
      str(sorted(j)))
check("page=1 per_page=10 → 10 条", len(j.get("orders") or []) == 10,
      f"len={len(j.get('orders') or [])}")
check("total=23", j.get("total") == TOTAL, str(j.get("total")))
check("total_pages=ceil(23/10)=3", j.get("total_pages") == 3, str(j.get("total_pages")))
check("回显 page/per_page", j.get("page") == 1 and j.get("per_page") == 10,
      f"{j.get('page')}/{j.get('per_page')}")
first_ids = [o["order_id"] for o in j["orders"]]
check("排序 = created_at DESC, id DESC（第一条是最新的 HNPAGE-022）",
      j["orders"][0]["order_number"] == "HNPAGE-022", str(j["orders"][0]["order_number"]))

# ==================== ③ 翻完三页：不重不漏 ====================
section("③ 三页拼起来 = 全部 23 条（不重不漏）")
all_ids = []
for p in (1, 2, 3):
    jj = (get(page=p, per_page=10).get_json() or {})
    all_ids += [o["order_id"] for o in jj.get("orders") or []]
check("三页合计 23 条", len(all_ids) == TOTAL, f"n={len(all_ids)}")
check("三页之间没有重复", len(set(all_ids)) == len(all_ids), f"uniq={len(set(all_ids))}")
check("最后一页 3 条", len(all_ids) - 20 == 3, f"n={len(all_ids)}")
check("页间顺序与整体 DESC 一致（第 1 页的最后一条 = 第 2 页的第一条之前）",
      all_ids[:10] == first_ids, "跨页顺序错乱")

# ==================== ④ 越界页：200 + 空列表 + 真实 total_pages ====================
section("④ 越界页不报错（前端点「下一页」不该弹错误）")
r = get(page=4, per_page=10)
j = r.get_json() or {}
check("page=4 越界 → 200（不是 400/404）", r.status_code == 200, str(r.status_code))
check("越界页 → 空列表", j.get("orders") == [], str(j.get("orders")))
check("越界页仍返回真实 total_pages=3", j.get("total_pages") == 3, str(j.get("total_pages")))
check("越界页 total 仍为 23（不因越界变成 0）", j.get("total") == TOTAL, str(j.get("total")))

# ==================== ⑤ per_page 边界 ====================
section("⑤ per_page 允许值 10/20/50/100 与非法值")
for pp, expect_pages in ((20, 2), (50, 1), (100, 1)):
    jj = (get(page=1, per_page=pp).get_json() or {})
    check(f"per_page={pp} 接受，total_pages={expect_pages}",
          jj.get("per_page") == pp and jj.get("total_pages") == expect_pages,
          f"{jj.get('per_page')}/{jj.get('total_pages')}")
jj = (get(page=2, per_page=20).get_json() or {})
check("per_page=20 的第 2 页 → 3 条", len(jj.get("orders") or []) == 3,
      f"len={len(jj.get('orders') or [])}")

r = get(page=1, per_page=25)
check("per_page=25 → 400（不在白名单）", r.status_code == 400,
      f"{r.status_code} {(r.get_json() or {}).get('message')}")
check("per_page=25 的 message 点明白名单",
      "10/20/50/100" in ((r.get_json() or {}).get("message") or ""),
      str((r.get_json() or {}).get("message")))
r = get(page=1, per_page="abc")
check("per_page=abc → 400", r.status_code == 400, str(r.status_code))
r = get(page=1, per_page=7)
check("per_page=7 → 400（不接受任意值，避免前端分页错位）", r.status_code == 400,
      str(r.status_code))

# ==================== ⑥ page 边界 ====================
section("⑥ page 边界与非法值")
for bad, kw in (("0", "≥ 1"), ("-3", "≥ 1"), ("abc", "整数"), ("2.5", "整数")):
    r = get(page=bad, per_page=10)
    j = r.get_json() or {}
    check(f"page={bad} → 400 且 message 提到「{kw}」",
          r.status_code == 400 and kw in (j.get("message") or ""),
          f"{r.status_code} {j.get('message')}")

r = get(per_page=10)
j = r.get_json() or {}
check("只带 per_page 不带 page → 进翻页模式且 page 默认 1",
      j.get("page") == 1 and len(j.get("orders") or []) == 10,
      f"page={j.get('page')} len={len(j.get('orders') or [])}")

# ==================== ⑦ limit 与 per_page 的关系 ====================
section("⑦ limit（默认条数）与 per_page（本页条数）互不干扰")
r = get(limit=5)
j = r.get_json() or {}
check("limit=5 不带 page → 5 条、limit 回显 5",
      j.get("limit") == 5 and len(j.get("orders") or []) == 5,
      f"limit={j.get('limit')} len={len(j.get('orders') or [])}")
check("limit=5 时 total 仍是全量 23", j.get("total") == TOTAL, str(j.get("total")))
r = get(limit=999)
j = r.get_json() or {}
check("limit=999 被夹到 100", j.get("limit") == 100, str(j.get("limit")))
r = get(limit="abc")
check("limit=abc → 400（老行为不变）", r.status_code == 400, str(r.status_code))

old_limit = backend._RECENT_ORDERS_LIMIT
backend._RECENT_ORDERS_LIMIT = 20
j = (get(page=1).get_json() or {})
check("per_page 缺省 = 配置的默认条数（20）→ total_pages=2",
      j.get("per_page") == 20 and j.get("total_pages") == 2,
      f"per_page={j.get('per_page')} total_pages={j.get('total_pages')}")
check("per_page 缺省时 orders 长度 = 20", len(j.get("orders") or []) == 20,
      f"len={len(j.get('orders') or [])}")
backend._RECENT_ORDERS_LIMIT = old_limit

# ==================== ⑧ 权限与字段 ====================
section("⑧ 权限 + 新字段")
r = client.get("/api/admin/recent_orders?page=1&per_page=10", headers=GUEST)
check("非管理员 → 403（翻页不放松权限）", r.status_code == 403, str(r.status_code))
j = (get(page=3, per_page=10).get_json() or {})
oldest = (j.get("orders") or [])[-1] if j.get("orders") else {}
check("每条订单都带 status_reason / manual_status_by / manual_status_at",
      all(k in oldest for k in ("status_reason", "manual_status_by", "manual_status_at")),
      str(sorted(oldest.keys()))[:200])
j0 = (get(page=1, per_page=10).get_json() or {})
newest = (j0.get("orders") or [])[0]
check("最新一张（i=22）status_reason=''（没有失败原因时是空串不是 None）",
      newest.get("status_reason") == "", repr(newest.get("status_reason")))
jlast = (get(page=1, per_page=100).get_json() or {})
by_num = {o["order_number"]: o for o in jlast.get("orders") or []}
o0 = by_num.get("HNPAGE-000") or {}
check("带 reject_reason 的订单 status_reason 透出原因",
      o0.get("status_reason") == "打印机断线，打印结果未知，已超时", repr(o0.get("status_reason")))
check("人工订正留痕透出 manual_status_by/at",
      o0.get("manual_status_by") == "霍楠" and o0.get("manual_status_at") == "2026-11-01 09:00:00",
      f"{o0.get('manual_status_by')}/{o0.get('manual_status_at')}")

# ==================== 汇总 ====================
ok = all(RESULTS)
print("\n" + "=" * 68)
print(f"recent_orders 翻页验收：{sum(RESULTS)}/{len(RESULTS)} 条通过" + (" ✅" if ok else " ❌"))
sys.exit(0 if ok else 1)
