# -*- coding: utf-8 -*-
"""验收脚本 · 后端部署后线上冒烟（在服务器上跑）

验证两件部署后才能确认的事：
  1. gthread worker（与 app.py 的 async_mode="threading" 匹配）下，**经真实 nginx 的
     WebSocket** 能建立并且连上即收到服务端推送 —— 打印任务走的就是这条路；
  2. HTTP 拉单通道能保活设备在线（`/api/pull_queued_orders` 刷新心跳），且心跳过期后
     不留幽灵在线设备（不会把在线数永久虚增）。

安全性：启动前先查库，有 queued/printing 子任务就退出（不打扰真实订单）；只在
127.0.0.1:5000 上调用 API，不做任何写订单的操作；结束时删除自己注册的测试设备。
代价：约 100s（要等一次 90s 心跳超时），期间在线设备数会临时 +1（不接单、不影响派单）。

    /home/printer-backend/venv/bin/python tests/verify_deploy_smoke.py
（客户端侧需要 websocket-client：后端 venv 没有时可
 `pip install --target=/tmp/wscheck_libs websocket-client` 并加 PYTHONPATH）
"""
import os
import sqlite3
import sys
import time

BACKEND = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "/home/printer-backend"
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import config                        # noqa: E402
import requests                      # noqa: E402
import socketio                      # noqa: E402

TOKEN = getattr(config, "TOKEN", "")
BASE = "http://127.0.0.1:5000"
PUBLIC_WS = "wss://hn-space.cn"
CID = "deploy-check-" + os.urandom(3).hex()
WS_CID = CID + "-ws"
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


if not TOKEN:
    print("[ABORT] config.py 里没有 TOKEN")
    sys.exit(2)

print("=== 0. 前置：队列必须为空（不打扰真实订单） ===")
con = sqlite3.connect("file:orders.db?mode=ro", uri=True)
n_busy = con.execute(
    "SELECT COUNT(*) FROM order_files WHERE status IN ('queued', 'printing')").fetchone()[0]
con.close()
check("无 queued/printing 子任务", n_busy == 0, f"count={n_busy}")
if n_busy:
    sys.exit(2)


def status():
    return requests.get(f"{BASE}/api/printer_status", timeout=5).json()


s0 = status()
print(f"基线：在线 {s0['count']} 台 / 接单设备 {s0['claiming_count']} 台 / 接单在线 {s0['take_orders_online']}")
check("基线有设备在线（否则说明打印机没连上）", s0["count"] >= 1, f"count={s0['count']}")

print("\n=== 1. HTTP 通道保活（D） ===")
r = requests.get(f"{BASE}/api/pull_queued_orders", timeout=10,
                 params={"token": TOKEN, "client_id": CID, "device_name": "deploy-check"})
check("HTTP 拉单 200/success", r.status_code == 200 and r.json().get("success") is True,
      f"{r.status_code} {r.text[:80]}")
time.sleep(1)
s1 = status()
check("纯 HTTP 设备被计入在线（心跳由拉单刷新）", s1["count"] == s0["count"] + 1,
      f"{s0['count']} → {s1['count']}")
check("不影响「接单在线」判定（测试设备未启用接单）",
      s1["take_orders_online"] == s0["take_orders_online"])

print("\n=== 2. 经 nginx 的 WebSocket 冒烟（E 的关键路径） ===")
got = {}
c = socketio.Client(reconnection=False, request_timeout=20)


@c.on("printer_state")
def _on_printer_state(data):
    got["printer_state"] = data


try:
    c.connect(f"{PUBLIC_WS}?token={TOKEN}&client_id={WS_CID}&device_name=deploy-check-ws",
              transports=["websocket"], wait_timeout=20)
    check("WebSocket 连接建立（wss 经 nginx → gunicorn）", bool(c.connected))
    check("传输方式 = websocket", c.transport() == "websocket", str(c.transport()))
    for _ in range(30):
        if got.get("printer_state"):
            break
        time.sleep(0.2)
    ps = got.get("printer_state") or {}
    check("连上即收到服务端推送 printer_state（推送通道可用）",
          isinstance(ps, dict) and "is_active" in ps, str(ps)[:140])
finally:
    try:
        c.disconnect()
    except Exception:
        pass

print("\n=== 3. 心跳过期后不留幽灵在线设备 ===")
print("等待 95s（> CLIENT_HEARTBEAT_TIMEOUT=90s）……")
time.sleep(95)
s2 = status()
check("在线数回到基线", s2["count"] == s0["count"], f"{s1['count']} → {s2['count']}")
check("接单设备与基线一致",
      s2["claiming_count"] == s0["claiming_count"]
      and s2["take_orders_online"] == s0["take_orders_online"],
      f"claiming {s2['claiming_count']} / online {s2['take_orders_online']}")

print("\n=== 4. 清理测试设备 ===")
for cid in (CID, WS_CID):
    rr = requests.post(f"{BASE}/api/printer/devices/delete", timeout=10,
                       params={"token": TOKEN}, json={"client_id": cid})
    print(f"  删除 {cid}: HTTP {rr.status_code} {str(rr.text)[:70]}")

print("\n" + ("全部通过 ✅" if ok_all else "存在失败项 ❌"))
sys.exit(0 if ok_all else 1)
