# -*- coding: utf-8 -*-
"""验收脚本 · HTTP 通道同样刷新设备心跳（让「双通道」真正互为备份）

过去只有 socketio 的 connect/ping 会写 printer_clients 的 heartbeat，而
/api/pull_queued_orders 又被 get_active_printer_client()（依赖同一心跳）门控 ——
socket 掉线但网络正常时设备被判离线，HTTP 拉单永远返回空，「双通道」实为单通道。

在服务器上跑（导入真实 app，用 Flask test_client 走真实路由）：
    /home/printer-backend/venv/bin/python /tmp/d_heartbeat_check.py [后端目录]
后端目录默认 /home/printer-backend；部署前想先验新代码，可传一份拷贝目录
（app.py 里 DATABASE 是相对路径，按 CWD 解析 —— 用拷贝目录跑就不会碰生产库与
devices.json/printer_claim.json）。脚本不启端口、不连网络；库里有排队任务时跳过
HTTP 那一步，避免把真实订单标成 printing。
"""
import os
import sys
from datetime import datetime

BACKEND = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "/home/printer-backend"
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import app as backend                    # noqa: E402

TEST_CLIENT = "wscheck-http-heartbeat"
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


with backend.printer_clients_lock:
    backend.printer_clients.pop(TEST_CLIENT, None)

print("=== 1. 助手函数：HTTP 心跳建条目并计入在线 ===")
check("测试前不在 printer_clients", TEST_CLIENT not in backend.printer_clients)
backend.touch_printer_heartbeat(TEST_CLIENT)
info = backend.printer_clients.get(TEST_CLIENT)
check("已登记条目", info is not None)
check("sid 为空（无 socket 连接）", info and info["sid"] is None, str(info))
check("heartbeat 是当前时间",
      info and (datetime.now() - info["heartbeat"]).total_seconds() < 5,
      str(info and info["heartbeat"]))
check("计入 get_active_clients()（离线判定即看它）",
      TEST_CLIENT in backend.get_active_clients())
check("心跳超时常量仍为 90s（客户端兜底间隔 45s 必须小于它）",
      backend.CLIENT_HEARTBEAT_TIMEOUT == 90, str(backend.CLIENT_HEARTBEAT_TIMEOUT))

print("\n=== 2. 已有真实 sid 时不被 None 覆盖 ===")
with backend.printer_clients_lock:
    backend.printer_clients[TEST_CLIENT] = {
        "sid": "real-sid-xyz", "heartbeat": datetime.now(), "connected_at": datetime.now()}
backend.touch_printer_heartbeat(TEST_CLIENT)
check("sid 保持不变", backend.printer_clients[TEST_CLIENT]["sid"] == "real-sid-xyz")
backend.touch_printer_heartbeat(TEST_CLIENT, sid="new-sid")
check("显式传 sid 时更新", backend.printer_clients[TEST_CLIENT]["sid"] == "new-sid")

print("\n=== 3. 心跳过期后会被清理（不会永远算在线） ===")
with backend.printer_clients_lock:
    backend.printer_clients[TEST_CLIENT]["heartbeat"] = datetime(2000, 1, 1)
check("过期条目不在在线列表", TEST_CLIENT not in backend.get_active_clients())
check("过期条目已被删除", TEST_CLIENT not in backend.printer_clients)

print("\n=== 4. HTTP 拉单路由真的会刷新心跳（端到端接线） ===")
queued = None
try:
    conn = backend.get_db()
    queued = conn.execute(
        "SELECT COUNT(*) c FROM order_files WHERE status = 'queued'").fetchone()["c"]
    conn.close()
except Exception as e:
    # 拷贝目录跑（没带真实库）会走到这里；该路由对「未接单设备」在碰库前就返回了
    print(f"[INFO] 读排队数失败，按空队列继续: {e}")
if queued:
    print(f"[SKIP] 库里有 {queued} 个排队任务，跳过 HTTP 调用（避免把真实订单标成 printing）")
else:
    with backend.printer_clients_lock:
        backend.printer_clients.pop(TEST_CLIENT, None)
    client = backend.app.test_client()
    resp = client.get(
        f"/api/pull_queued_orders?token={backend.PRINTER_TOKEN}&client_id={TEST_CLIENT}")
    check("HTTP 200", resp.status_code == 200, str(resp.status_code))
    check("拉单后该设备已在 printer_clients（心跳被刷新）",
          TEST_CLIENT in backend.printer_clients)
    check("并计入在线列表", TEST_CLIENT in backend.get_active_clients())

print("\n=== 5. 静态接线：拉单入口调用了心跳刷新，且在在线判定之前 ===")
src = open(os.path.join(BACKEND, "app.py"), encoding="utf-8").read()
# 只看函数体代码行（注释里也提到这两个名字，按整行匹配 + 跳过注释行）
body = src.split("def pull_queued_orders")[1].splitlines()[:40]
code = [ln for ln in body if not ln.strip().startswith("#")]


def _line_of(needle):
    for i, ln in enumerate(code):
        if needle in ln:
            return i
    return -1


i_touch = _line_of("touch_printer_heartbeat(client_id)")
i_active = _line_of("= get_active_printer_client()")
check("pull_queued_orders 内调用 touch_printer_heartbeat", i_touch >= 0, f"line={i_touch}")
check("调用位置在 get_active_printer_client() 之前（否则当次拉取仍被判离线）",
      i_touch >= 0 and i_active >= 0 and i_touch < i_active,
      f"touch@{i_touch} active@{i_active}")

with backend.printer_clients_lock:
    backend.printer_clients.pop(TEST_CLIENT, None)
print("\n" + ("全部通过 ✅" if ok_all else "存在失败项 ❌"))
sys.exit(0 if ok_all else 1)
