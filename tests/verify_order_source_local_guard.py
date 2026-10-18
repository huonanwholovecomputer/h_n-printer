# -*- coding: utf-8 -*-
"""验收 · 本地工具不得把**云端订单**改写成「本地」（2026-10-18 现场）

现场：小程序下了一单（HN20261018-0023），本地工具打印后，结算页「云端数据 → 订单明细」
的来源徽标显示 **本地**（截图确认）。根因是后端 `/api/local_orders` 对**已存在的订单行**
无条件 `UPDATE ... source = 'local'`：

    云端任务的 PrintJob 带着**云端订单号**（local_print_tool/cloud_client.py:
        PrintJob(order_number=task.order_number)）
    → `MainWindow._acquire_order_number_and_upload` 复用该号（"复用已有订单号"）
    → POST /api/local_orders（status=sent）
    → 后端按 order_number 命中那行云端单 → source='local'、owner_name 被本地归属覆盖
    → 结算页来源徽标「本地」、订单构成/按来源对账错位（openid 仍是微信，所以小程序里订单还在）

两侧都要有闸门：
  · 服务端：`local_orders()` 只允许改写 openid='local' 或 source∈{local, 空} 的行
    （热部署即对全部存量客户端生效）；
  · 客户端（4.5.25+）：云端任务（task_id>0）根本不发起这个请求。

用法: PYTHONPATH= PYTHONUTF8=1 python.exe tests/verify_order_source_local_guard.py
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

_tmp = tempfile.mkdtemp(prefix="hn_src_guard_")
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

TOKEN = "guard-token"
backend.PRINTER_TOKEN = TOKEN
backend.init_db()
client = backend.app.test_client()

CLOUD_NUM = "HN20261018-0023"


def _insert(order_number, openid, source, status="printing", owner="霍楠", file_="x.pdf"):
    conn = backend.get_db()
    conn.execute(
        """INSERT INTO orders (file, copies, status, created_at, openid, order_number,
                               total_price, source, owner_name, is_admin_print, remark)
           VALUES (?, 1, ?, '2026-10-18 21:00:00', ?, ?, 0.4, ?, ?, 0, '')""",
        (file_, status, openid, order_number, source, owner),
    )
    oid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return oid


def _post(order_number, owner="孤Gu鬼fu", status="sent"):
    return client.post(f"/api/local_orders?token={TOKEN}", json={
        "order_number": order_number, "status": status, "total_price": 0.4,
        "files": [{"file_name": "离散数学作业2.pdf", "copies": 1, "page_count": 1,
                   "cost": 0.4, "duplex": "off", "page_range": ""}],
        "created_at": "2026-10-18 22:00:00", "owner_name": owner,
        "is_admin_print": False, "client_id": "guard-client",
    })


def _row(oid):
    conn = backend.get_db()
    r = dict(conn.execute("SELECT openid, source, status, owner_name FROM orders WHERE id = ?",
                          (oid,)).fetchone())
    conn.close()
    return r


# ============================================================
print("=== ① 云端小程序订单：必须拒改 ===")
cid = _insert(CLOUD_NUM, "wx_openid_abc", "wechat", status="printing", owner="霍楠")
before = _row(cid)
resp = _post(CLOUD_NUM)
after = _row(cid)
check("source 仍是 wechat", after["source"] == "wechat", f"{after['source']}")
check("openid 未变", after["openid"] == "wx_openid_abc", f"{after['openid']}")
check("归属人未被本地覆盖", after["owner_name"] == before["owner_name"],
      f"{before['owner_name']!r} → {after['owner_name']!r}")
check("状态未被本地回报改写", after["status"] == "printing", f"{after['status']}")
check("HTTP 200（不抛错、不静默失败）", resp.status_code == 200, f"{resp.status_code}")
check("响应标记 ignored，客户端日志可读", bool(resp.get_json().get("ignored")),
      f"{resp.get_json()}")
conn = backend.get_db()
n = conn.execute("SELECT COUNT(*) FROM orders WHERE order_number = ?", (CLOUD_NUM,)).fetchone()[0]
conn.close()
check("没有产生第二行订单", n == 1, f"{n} 行")

print("\n=== ② APP 订单（source='app'）与未知来源：同样拒改 ===")
aid = _insert("HN20261018-0030", "dev_abc123", "app", status="queued")
r2 = _post("HN20261018-0030")
check("APP 订单 source 仍是 app", _row(aid)["source"] == "app", f"{_row(aid)['source']}")
check("APP 单也被标记 ignored", bool(r2.get_json().get("ignored")), f"{r2.get_json()}")

print("\n=== ③ 本地预留单：照旧可写（零回归，别把闸门开过头）===")
# /api/next_order_number 预留时写 openid='local', source='local', status='reserved'
lid = _insert("HN20261018-0100", "local", "local", status="reserved", owner="甲成员",
              file_="（预留位置）")
r3 = _post("HN20261018-0100", owner="甲成员")
row3 = _row(lid)
check("source 仍为 local", row3["source"] == "local", f"{row3['source']}")
check("status 被更新为 sent", row3["status"] == "sent", f"{row3['status']}")
check("归属人写入", row3["owner_name"] == "甲成员", f"{row3['owner_name']}")
check("未被标记 ignored", not r3.get_json().get("ignored"), f"{r3.get_json()}")

print("\n=== ④ 无 openid 的本地单（老数据/异常态）：仍允许本地接管 ===")
nid = _insert("HN20261018-0101", "", "", status="reserved", owner="", file_="（预留位置）")
r4 = _post("HN20261018-0101", owner="乙成员")
check("空 openid/来源的行仍可被本地单接管（不误伤历史数据）",
      _row(nid)["source"] == "local" and _row(nid)["status"] == "sent",
      f"{_row(nid)}")

print("\n=== ⑤ 不存在的订单号：照旧走 INSERT（本地新单）===")
r5 = _post("HN20261018-0200", owner="丙成员")
conn = backend.get_db()
row5 = dict(conn.execute("SELECT openid, source FROM orders WHERE order_number = ?",
                         ("HN20261018-0200",)).fetchone())
conn.close()
check("新号插入为本地单（openid/source = local）",
      row5["openid"] == "local" and row5["source"] == "local", f"{row5}")
check("未被标记 ignored", not r5.get_json().get("ignored"), f"{r5.get_json()}")

print("\n=== ⑥ 终态幂等（sent/accepted 直接跳过，与来源闸门不冲突）===")
sid = _insert("HN20261018-0300", "wx_openid_xyz", "wechat", status="sent")
r6 = _post("HN20261018-0300", status="reserved")
check("已 sent 的订单不被改写", _row(sid)["source"] == "wechat", f"{_row(sid)}")
check("走的是幂等分支（无 ignored 字段）", "ignored" not in r6.get_json(),
      f"{r6.get_json()}")

shutil.rmtree(_tmp, ignore_errors=True)
print("\n" + ("=" * 60))
print("来源闸门验收：全部通过 ✅" if ok_all else "存在失败项 ❌")
sys.exit(0 if ok_all else 1)
