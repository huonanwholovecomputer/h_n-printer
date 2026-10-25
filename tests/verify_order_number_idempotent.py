# -*- coding: utf-8 -*-
"""验收 · `/api/next_order_number` 取号幂等（2026-10-25 现场：云端凭空多出一个空「已预留」订单）

现场（订单明细截图 + local_tool.log）：
  13:07:27  本地工具取号 → 服务端建好占位单 **HN20261025-0003**，
            但那次 HTTPS 响应在 TLS 层丢了（`SSLEOFError: UNEXPECTED_EOF_WHILE_READING`）
  13:07:28  客户端只等到超时 → 回落本地临时号打印；上报 /api/local_orders 也失败（同一个 SSL EOF）
  13:17:01  重连后换号：**又申请了一个新号** HN20261025-0004（服务端又建一个占位单），
            订单写进 0004 并标记已完成
  ⇒ 云端留下 0003：0 文件、¥0.00、永远「已预留」。用户看到的就是这个「莫名其妙的空预留订单」。

修法：取号带幂等键 `request_id`（本地工具按标签页生成并持久化）。同一个 request_id 再问一次，
服务端**返回当初那个号**，不再新建占位单；重连换号因此写回 0003 本身，不再产生空预留单。
只复用 `status='reserved'` 的行（已完成/失败/取消的行绝不复用，否则新单会挂到旧号上）。

用法: PYTHONPATH= PYTHONUTF8=1 python.exe tests/verify_order_number_idempotent.py
      exit 0 = 全绿
"""
import io
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

_tmp = tempfile.mkdtemp(prefix="hn_orderidem_")
_dst = os.path.join(_tmp, "printer-backend")
shutil.copytree(SRC, _dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs"))
sys.path.insert(0, _dst)
os.chdir(_dst)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


try:
    import app as backend
except Exception as e:
    print(f"✗ 无法导入 app.py: {e}")
    sys.exit(1)

TOKEN = "order-idem-token"
backend.PRINTER_TOKEN = TOKEN
backend.init_db()
client = backend.app.test_client()


def q(sql, args=()):
    conn = backend.get_db()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def rows_of(number):
    return q("SELECT id, order_number, status, client_request_id, file, total_price, openid, source,"
             " owner_name FROM orders WHERE order_number = ?", (number,))


def count_rows():
    return q("SELECT COUNT(*) FROM orders")[0][0]


def get_number(request_id=None, owner="甲成员"):
    url = f"/api/next_order_number?token={TOKEN}"
    if owner is not None:
        url += f"&owner_name={owner}"
    if request_id is not None:
        url += f"&request_id={request_id}"
    resp = client.get(url)
    assert resp.status_code == 200, resp.status_code
    return resp.get_json()


# ============================================================
print("=== ① 第一次取号：建 reserved 占位单并记下幂等键 ===")
r1 = get_number("req-A")
n1 = r1.get("order_number") or ""
check("取号成功", bool(r1.get("success")) and bool(n1), f"{r1}")
check("幂等键写进占位单（client_request_id）",
      bool(rows_of(n1)) and rows_of(n1)[0][3] == "req-A", f"{rows_of(n1)}")
check("占位单 status=reserved / file=（预留位置）/ 归属已记",
      rows_of(n1)[0][2] == "reserved" and rows_of(n1)[0][4] == "（预留位置）"
      and rows_of(n1)[0][8] == "甲成员", f"{rows_of(n1)}")
check("占位单 openid/source = local（来源闸门口径不变）",
      rows_of(n1)[0][6] == "local" and rows_of(n1)[0][7] == "local", f"{rows_of(n1)}")
check("此时库里只有 1 行", count_rows() == 1, f"{count_rows()}")

# ============================================================
print("\n=== ② 同一个 request_id 再问一次（模拟「响应丢了 → 重连换号」）===")
before = count_rows()
r2 = get_number("req-A")
check("★ 拿回的是**同一个**订单号（不再新建占位单）",
      r2.get("order_number") == n1 and bool(r2.get("reused")), f"{n1} → {r2}")
check("★ 没有多出第二行订单（现场那个空预留单的根因）", count_rows() == before, f"{count_rows()}")
check("订单号没有被消耗第二次（generate_order_number 不会被白调一次）",
      q("SELECT order_number FROM orders ORDER BY id")[0][0] == n1, "")

# ============================================================
print("\n=== ③ 换一个 request_id：照旧分配新号（零回归）===")
r3 = get_number("req-B")
n3 = r3.get("order_number") or ""
check("不同 request_id → 不同订单号", n3 and n3 != n1, f"{n1} / {n3}")
check("新占位单记下自己的幂等键", rows_of(n3)[0][3] == "req-B", f"{rows_of(n3)}")
check("现在共 2 行", count_rows() == 2, f"{count_rows()}")

# ============================================================
print("\n=== ④ 不带 request_id（旧版客户端）：行为与以前完全一致 ===")
r4 = get_number(None)
n4 = r4.get("order_number") or ""
check("照旧拿到新号", bool(n4) and n4 not in (n1, n3), f"{n1} / {n3} / {n4}")
check("旧客户端占位单不写幂等键（去重索引不受影响）",
      rows_of(n4)[0][3] in (None, ""), f"{rows_of(n4)}")
check("共 3 行", count_rows() == 3, f"{count_rows()}")

# ============================================================
print("\n=== ⑤ 重连换号把订单写进**原来那个号**：不再有空的已预留 ===")
# 客户端用同一把钥匙换号 → 拿到 n1（上一节已证）→ 再把完整订单上报到 n1
resp = client.post(f"/api/local_orders?token={TOKEN}", json={
    "order_number": n1, "status": "sent", "total_price": 0.4,
    "files": [{"file_name": "高等数学(1)作业3.pdf", "copies": 1, "page_count": 2,
               "cost": 0.4, "duplex": "on", "page_range": ""}],
    "created_at": "2026-10-25 13:17:01", "owner_name": "甲成员",
    "is_admin_print": False, "client_id": "R7000-AHP10-168dbdc3d1",
})
body = resp.get_json()
check("上报成功（就是同一行）", resp.status_code == 200 and bool(body.get("success")), f"{body}")
row = rows_of(n1)[0]
check("★ 那一行变成 sent + 有文件 + ¥0.40（不再是空预留）",
      row[2] == "sent" and row[4] != "（预留位置）" and float(row[5] or 0) > 0, f"{row}")
_empty_reserved = {r[0] for r in q(
    """SELECT order_number FROM orders WHERE openid='local' AND status='reserved'
       AND file='（预留位置）' AND total_price <= 0""")}
check("★ 现场那行（取号响应丢失的那一笔）不在「空预留」里 —— 它被同步写满了",
      n1 not in _empty_reserved, f"{_empty_reserved}")

# ============================================================
print("\n=== ⑥ 已完成的行绝不复用（否则新订单会被挂到旧单号上）===")
before = count_rows()
r6 = get_number("req-A")
check("同一个 request_id（那行已 sent）→ 分配**新号**", r6.get("order_number") != n1, f"{r6}")
check("并新建了一行占位单", count_rows() == before + 1, f"{count_rows()}")

# ============================================================
print("\n=== ⑦ 别的 openid 用了同一把钥匙：本地取号不串台 ===")
conn = backend.get_db()
conn.execute(
    """INSERT INTO orders (file, copies, status, created_at, openid, order_number,
                           total_price, source, owner_name, is_admin_print, client_request_id)
       VALUES ('x.pdf', 1, 'reserved', '2026-10-25 13:00:00', 'wx_openid_x',
               'HN20261025-0099', 0.4, 'wechat', '霍楠', 0, 'req-shared')""")
conn.commit()
conn.close()
r7 = get_number("req-shared")
check("★ 不会把云端用户的占位单号发给本地工具（openid='local' 限域）",
      r7.get("order_number") != "HN20261025-0099", f"{r7}")
check("本地占位单照旧建好（自己的行）", rows_of(r7["order_number"])[0][6] == "local", "")

# ============================================================
print("\n=== ⑧ 30 分钟清理把空占位单删掉之后：同一把钥匙重新分配（不炸）===")
r8a = get_number("req-old")
n8 = r8a.get("order_number") or ""
conn = backend.get_db()
conn.execute("UPDATE orders SET created_at = '2026-10-25 12:00:00' WHERE order_number = ?", (n8,))
conn.commit()
conn.close()
backend.cleanup_abandoned_reserved_orders()
check("超时的纯占位单被清理（原有行为不变）", not rows_of(n8), f"{rows_of(n8)}")
r8b = get_number("req-old")
check("清理后再用同一把钥匙 → 拿到一个有效新号（不会 500 / 不会返回空号）",
      bool(r8b.get("success")) and bool(r8b.get("order_number"))
      and r8b.get("order_number") != n8, f"{r8b}")

# ============================================================
print("\n=== ⑨ 修复前对照：不带幂等键的「响应丢失 + 重连换号」确实会留下空预留单 ===")
before = count_rows()
_lost = get_number(None)            # 现场：这次响应丢了（服务端已建占位单）
_later = get_number(None)           # 重连换号：旧客户端只能再要一个新号
empty = q("""SELECT order_number FROM orders WHERE openid='local' AND status='reserved'
             AND file='（预留位置）' AND total_price <= 0""")
check("对照证明修法有判别力：旧路径会多出一行空占位单",
      _lost.get("order_number") != _later.get("order_number")
      and count_rows() == before + 2 and empty, f"{_lost} / {_later} / {empty}")

print("\n" + "=" * 56)
print("取号幂等验收：" + ("全部通过 ✅" if ok_all else "有失败 ❌"))
sys.exit(0 if ok_all else 1)
