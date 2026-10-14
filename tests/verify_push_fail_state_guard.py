# -*- coding: utf-8 -*-
"""审计修复验收 · 🔴-11 推送失败分支无条件盲写 queued → 撤销已判定的 rejected

事故形状：submit_order 在事务外推送子任务，push_print_task_to_client 返回 False 时
执行 `UPDATE order_files SET status='queued' WHERE id=?`，**没有状态守卫**。而该函数
返回 False 有多种原因，其中：
 · rowcount == 0（任务已不是 queued，例如页数越界刚被判 rejected）→ 打 [SKIP] 后
   return False ⇒ 这里把 rejected 又改回 queued ⇒ 30 秒后照常推送 ⇒ **越界文件被
   真实打印**，顾客看到"排队中"而不是"被打回"；
 · 任务已被**另一条路径**（process_pending_orders / 打印机上线上线重推）领取为
   printing ⇒ 这里把它改回 queued ⇒ **重复打印**。本段推送在事务外，与那些路径
   存在真实并发窗口。
而且 push_print_task_to_client 内部对每种失败路径都自带**带状态守卫**的回滚
（只改 printing/downloading 的行），这里属于重复且有害的第二次回滚。

验收方式：Flask 测试客户端走完整 submit_order 接口，用桩 push 精确模拟上述两种
"状态已被别人改走"的情形，断言提交后任务状态**不被改写**。
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

tmp = tempfile.mkdtemp(prefix="hn_verify_push_")
dst = os.path.join(tmp, "printer-backend")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs"))
sys.path.insert(0, dst)
os.chdir(dst)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


import app as backend          # noqa: E402

backend.PRINTER_TOKEN = "testtok"
backend.init_db()

OPENID = "verify-openid-0001"
uconn = backend.get_user_db()
uconn.execute("INSERT OR REPLACE INTO users (openid, nickname, updated_at, role)"
              " VALUES (?, ?, datetime('now'), 'user')", (OPENID, "验证用户"))
uconn.commit()
uconn.close()

FILE_ID = "file-verify-0001"
conn = backend.get_db_conn()
conn.execute("INSERT OR REPLACE INTO files"
             " (id, original_name, saved_name, path, size, created_at, openid,"
             "  page_count, page_count_verified)"
             " VALUES (?, 'spec.pdf', 'spec.pdf', '', 1234, datetime('now'), ?, 3, 1)",
             (FILE_ID, OPENID))
conn.commit()
conn.close()

HEADERS = {"Authorization": "Bearer " + backend.token_serializer.dumps(OPENID)}
client = backend.app.test_client()

# 让提交路径认为"有一台在线接单设备"，从而进入推送循环
backend.get_active_printer_client = lambda: "devA"


def submit(push_stub):
    """提交一单（单文件），推送用给定桩，返回 (order_id, 子任务行 or None)。"""
    backend.push_print_task_to_client = push_stub
    resp = client.post("/api/submit_order", headers=HEADERS,
                       json={"files": [{"file_id": FILE_ID, "file": "spec.pdf", "copies": 1}],
                             "duplex": "on"})
    if resp.status_code != 200:
        return None, None, resp
    body = resp.get_json() or {}
    oid = body.get("order_id")
    c = backend.get_db_conn()
    row = c.execute("SELECT id, status, reject_reason FROM order_files WHERE order_id = ?"
                    " ORDER BY id DESC LIMIT 1", (oid,)).fetchone()
    c.close()
    return oid, (dict(row) if row else None), resp


def stub_setting(status, reason=""):
    """桩：模拟"任务已被别的路径改走状态"，然后返回 False（等价于 rowcount==0 的 [SKIP]）。"""
    def _stub(sub_task_id, file_id, file_name, copies, duplex, page_range, client_id, **kw):
        c = backend.get_db_conn()
        c.execute("UPDATE order_files SET status = ?, reject_reason = ? WHERE id = ?",
                  (status, reason, sub_task_id))
        c.commit()
        c.close()
        return False
    return _stub


print("=== 1. 任务已被判 rejected（页数越界）—— 不得被改回 queued ===")
oid, row, resp = submit(stub_setting("rejected", "页数超出限制，已打回"))
check("接口返回 200", resp.status_code == 200,
      f"status={resp.status_code} body={resp.get_data(as_text=True)[:200]}")
check("子任务状态保持 rejected（改前被盲写回 queued ⇒ 越界文件照常打印）",
      row and row["status"] == "rejected", f"{row}")
check("打回原因未被清空", row and row["reject_reason"] == "页数超出限制，已打回", f"{row}")
body = resp.get_json() or {}
check("pushed_count 为 0", body.get("pushed_count") == 0, f"{body.get('pushed_count')}")
# 注：响应体 status 取自 orders 表在**事务内**的聚合快照，与推送分支无关（改前改后相同），
# 故不作为断言；顾客侧看到的状态由订单详情接口实时读取，本脚本用子任务状态（上方）覆盖。

print("\n=== 2. 任务已被另一条推送路径领取（printing）—— 不得被改回 queued ===")
oid, row, resp = submit(stub_setting("printing"))
check("接口返回 200", resp.status_code == 200, f"status={resp.status_code}")
check("子任务状态保持 printing（改前被改回 queued ⇒ 重复打印）",
      row and row["status"] == "printing", f"{row}")
body = resp.get_json() or {}
check("pushed_count 为 0", body.get("pushed_count") == 0, f"{body.get('pushed_count')}")

print("\n=== 3. 真推送失败但状态本就是 queued（幂等，无副作用）===")


def stub_plain_fail(sub_task_id, file_id, file_name, copies, duplex, page_range,
                    client_id, **kw):
    return False         # 库中状态不动（模拟 emit 异常已被内部守卫回滚为 queued）


oid, row, resp = submit(stub_plain_fail)
check("接口返回 200", resp.status_code == 200, f"status={resp.status_code}")
check("子任务保持 queued（函数内部已回滚，此处无需再写）",
      row and row["status"] == "queued", f"{row}")
body = resp.get_json() or {}
check("响应状态为 queued", body.get("status") == "queued", f"status={body.get('status')!r}")

print("\n=== 4. 推送成功路径不受影响 ===")


def stub_ok(sub_task_id, file_id, file_name, copies, duplex, page_range, client_id, **kw):
    c = backend.get_db_conn()
    c.execute("UPDATE order_files SET status = 'printing', operator_client = ? WHERE id = ?",
              (client_id, sub_task_id))
    c.commit()
    c.close()
    return True


oid, row, resp = submit(stub_ok)
body = resp.get_json() or {}
check("接口返回 200", resp.status_code == 200, f"status={resp.status_code}")
check("子任务为 printing", row and row["status"] == "printing", f"{row}")
check("pushed_count 为 1", body.get("pushed_count") == 1, f"{body.get('pushed_count')}")

print("\n" + ("全部通过" if ok_all else "存在失败项"))
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if ok_all else 1)
