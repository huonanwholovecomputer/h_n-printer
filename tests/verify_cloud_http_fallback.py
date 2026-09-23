# -*- coding: utf-8 -*-
"""验收脚本 · 断线期间的 HTTP 兜底拉单（D）

服务端设备的「在线」只由 socketio 心跳维持（本地 30s ping / 服务端 90s 超时），而
/api/pull_queued_orders 又被同一心跳门控。socket 掉线但网络正常时（本机代理掐断长连接、
namespace 握手超时、服务端连接重建 —— 日志里 4 次「One or more namespaces failed to
connect」都是这类），工具会彻底隐身：服务端把订单排队等它上线，它却在等 socket 恢复。

本脚本验收客户端侧兜底：未连接时按 PULL_FALLBACK_INTERVAL 走 HTTP（既刷服务端心跳，
又把排队任务领回来），socket 正常时完全不发这个请求；间隔必须小于服务端心跳超时；
并发不叠加；断网期间不刷屏。服务端侧（拉单入口刷新心跳）见
tests/verify_http_heartbeat_channel.py。

无网络（api_url 指向 127.0.0.1:1，连接立即被拒）：
    python tests/verify_cloud_http_fallback.py      # exit 0 = 全绿
"""
import io
import logging
import os
import sys
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import cloud_client as cc                 # noqa: E402
from cloud_client import CloudClient      # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


print("=== 1. 间隔必须小于服务端心跳超时（90s），否则两次轮询之间就被判离线 ===")
check("PULL_FALLBACK_INTERVAL < 90", cc.PULL_FALLBACK_INTERVAL < 90,
      f"{cc.PULL_FALLBACK_INTERVAL}s")
check("间隔不太激进（≥30s，避免无谓请求）", cc.PULL_FALLBACK_INTERVAL >= 30,
      f"{cc.PULL_FALLBACK_INTERVAL}s")

print("\n=== 2. 未连接时轮询、连上后不轮询 ===")
client = CloudClient(api_url="http://127.0.0.1:1", ws_url="", token="tok",
                     client_id="fallback-test")
calls = []


def _fake_pull(quiet=False):
    calls.append(quiet)
    time.sleep(0.02)


client.pull_pending = _fake_pull
_orig_interval = cc.PULL_FALLBACK_INTERVAL
cc.PULL_FALLBACK_INTERVAL = 0.05          # 加速：等待逻辑读的是模块常量

client._connected = False
th = threading.Thread(target=client._pull_fallback_loop, daemon=True)
th.start()
time.sleep(0.45)
n_offline = len(calls)
check("未连接时按间隔发 HTTP 拉单", n_offline >= 2, f"{n_offline} 次 / 0.45s")
check("兜底调用带 quiet=True（长断网不刷屏）", calls and all(calls),
      str(calls))

client._connected = True
time.sleep(0.3)
check("socket 正常时不再发 HTTP 请求（避免与推送/连线拉取重复）",
      len(calls) == n_offline, f"{n_offline} → {len(calls)}")

client._stop_event.set()
th.join(timeout=2)
check("停止后兜底线程退出", not th.is_alive())

print("\n=== 3. 网络卡住时不叠加并发（上一轮没回来就跳过本轮） ===")
active = 0
max_active = 0
done = threading.Event()


def _slow_pull(quiet=False):
    global active, max_active
    active += 1
    max_active = max(max_active, active)
    time.sleep(0.2)                       # 单次请求比间隔长（真实场景：pull 超时 10s）
    active -= 1
    if active == 0:
        done.set()


c2 = CloudClient(api_url="http://127.0.0.1:1", ws_url="", token="tok",
                 client_id="fallback-serial")
c2.pull_pending = _slow_pull
c2._connected = False
cc.PULL_FALLBACK_INTERVAL = 0.02
th2 = threading.Thread(target=c2._pull_fallback_loop, daemon=True)
th2.start()
time.sleep(0.6)
c2._stop_event.set()
th2.join(timeout=2)
check("并发始终为 1（_pull_lock 生效）", max_active == 1, f"max_active={max_active}")

print("\n=== 4. quiet 只影响日志级别（手动/连线拉取的失败仍要可见） ===")
records = []


class _Cap(logging.Handler):
    def emit(self, record):
        records.append(record)


_cap = _Cap()
cc.logger.addHandler(_cap)
_old_level = cc.logger.level
cc.logger.setLevel(logging.DEBUG)

c3 = CloudClient(api_url="http://127.0.0.1:1", ws_url="", token="tok", client_id="fallback-log")
c3.pull_pending(quiet=True)               # 真实实现：连接被拒 → 走 except 分支
check("quiet=True 不产生 WARNING", not [r for r in records if r.levelno >= logging.WARNING],
      str([r.getMessage() for r in records]))
records.clear()
c3.pull_pending()
check("默认（手动/连线拉取）仍 WARNING",
      any(r.levelno >= logging.WARNING for r in records),
      str([r.getMessage() for r in records]))
cc.logger.removeHandler(_cap)
cc.logger.setLevel(_old_level)

print("\n=== 5. start() 起兜底线程，且不重复起 ===")
c4 = CloudClient(api_url="", ws_url="", token="", client_id="fallback-start")
c4._run_loop = lambda: None               # 免去真实连接尝试
c4.start()
time.sleep(0.15)
first = c4._pull_thread
check("start() 已启动兜底线程", first is not None and first.is_alive())
c4.start()
check("重复 start() 不另起线程", c4._pull_thread is first)
c4.stop()
check("stop() 后停止事件已置位（线程随之为 daemon 退出）", c4._stop_event.is_set())

cc.PULL_FALLBACK_INTERVAL = _orig_interval
print("\n" + ("全部通过 ✅" if ok_all else "存在失败项 ❌"))
sys.exit(0 if ok_all else 1)
