# -*- coding: utf-8 -*-
"""验收 · S1-07 Socket.IO 事件层加固

依据：docs/安全加固修复计划_2026-10-23.md §S1-07（R7）+ §1.4 依赖 2
（collect_all 异步化**先于** async_handlers=False）。

现场形状：
  · `async_handlers=True`（默认）⇒ 每个事件在独立线程里跑，"一个请求占线程"被搬成
    "占住该设备的事件线程"，症状更隐蔽；
  · 一条连接发 1000 事件/秒 ⇒ 1000 次同步 SQL；
  · `/api/log/collect_all` 用 `time.sleep(0.2)` 轮询最多 **20 秒**，直接占死一个 gthread；
  · `on_ping` 每次遍历全部 `printer_clients`（O(n)，ping 密时 O(n²)）；
  · engineio 默认 ping 25s/超时 20s、单帧 1 MB。

本脚本：
  A. 静态接线：async_handlers=False、engineio 参数、事件令牌桶 + 统一注册装饰器、
     collect_all 的 Condition/信号量/3s 上限、on_ping 走 sid 映射、disconnect 清映射。
  B. 行为（真实 Flask-SocketIO test_client）：
     `server.async_handlers is False`；13 个事件全部注册；事件洪水被丢弃且计数增长、
     正常速率不被误伤；collect_all 最长 ≤3.5s（占位设备不回报时）；on_ping 只碰自己的
     连接（O(1) 映射）。
  C. 变异自证：把 async_handlers 改回 True / 把令牌桶关掉 → 判据转红。

用法：python tests/verify_socket_event_limits.py     （exit 0 = 全部通过）
"""
import io
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()
src = H.read_app_source()
code = H.strip_comments(src)


def block(marker):
    i = code.find(marker)
    if i < 0:
        return ""
    lines = code[i:].splitlines(keepends=True)
    out = [lines[0]]
    for ln in lines[1:]:
        if ln.strip() and not ln[:1].isspace():
            break
        out.append(ln)
    return "".join(out)


collect = block("def log_collect_all(")

# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
H.chk("SocketIO 显式 async_handlers=False", "async_handlers=False" in code)
H.chk("engineio 参数收紧（15s / 10s / 256KB）",
      "ping_interval=15" in code and "ping_timeout=10" in code
      and "max_http_buffer_size=262144" in code)
H.chk("每连接事件令牌桶（20/s，突发 40）",
      "_EVENT_BUCKET_RATE = 20.0" in code and "_EVENT_BUCKET_CAP = 40.0" in code
      and "def _event_rate_ok(" in code)
H.chk("统一注册装饰器 sio_event（新 handler 不会漏限速）",
      "def sio_event(" in code and "@sio_event(" in code
      and "@socketio.on(" not in code)
H.chk("connect/disconnect 不套令牌桶（连接本身不该被限速）",
      'if name in ("connect", "disconnect"):' in code)
H.chk("collect_all 用 Condition 等待（不再 sleep 轮询）",
      "_collect_cond = threading.Condition(" in code and "time.sleep(0.2)" not in collect
      and "_collect_cond.wait(remain)" in collect)
H.chk("collect_all 全局只允许一次在飞（Semaphore 非阻塞 → 503）",
      "_collect_sem = threading.Semaphore(1)" in code
      and "_collect_sem.acquire(blocking=False)" in collect and "503" in collect)
H.chk("collect_all 等待上限 3s（原 20s）",
      "COLLECT_MAX_WAIT = 3.0" in code and "min(float(timeout), COLLECT_MAX_WAIT)" in collect)
H.chk("设备回报时唤醒等待者", "_collect_cond.notify_all()" in code)
H.chk("on_ping 走 sid→client 映射（O(1)）",
      "_sid_to_client.get(request.sid)" in block("def on_ping(")
      and "for cid, info in printer_clients.items()" not in block("def on_ping("))
H.chk("connect 建映射 / disconnect 删映射",
      "_sid_to_client[request.sid] = client_id" in code
      and "_sid_to_client.pop(sid, None)" in code)

# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（临时后端副本 + 真实 SocketIO test_client）===")
TMP, DST = H.make_copy("hn_sioev_")
backend = H.import_backend(DST)
backend.init_db()
TOK = "sioev-token"
backend.PRINTER_TOKEN = TOK

srv = backend.socketio.server
H.chk("★ server.async_handlers is False", srv.async_handlers is False, srv.async_handlers)
H.chk("engineio: ping_interval=15 / ping_timeout=10 / buffer=256KB",
      srv.eio.ping_interval == 15 and srv.eio.ping_timeout == 10
      and srv.eio.max_http_buffer_size == 262144)
events = set(srv.handlers.get("/", {}).keys())
for ev in ("connect", "disconnect", "ping", "print_success", "print_fail", "file_ready",
           "download_delayed", "start_printing", "stack_state", "print_keepalive",
           "page_count_result", "page_range_truncated", "logs"):
    H.chk("事件 %s 仍已注册（装饰器替换没有丢事件）" % ev, ev in events, sorted(events))

c = backend.socketio.test_client(
    backend.app, query_string="?token=%s&client_id=sioev-dev" % TOK)
H.chk("合法设备仍能连接", c.is_connected())
c.get_received()

# 事件洪水：一条连接连发 200 个事件，超过突发上限的部分被丢弃并计数
backend._event_bucket_stats["dropped"] = 0
t0 = time.time()
for i in range(200):
    c.emit("ping")
flood_secs = time.time() - t0
dropped = backend._event_bucket_stats["dropped"]
H.chk("事件洪水被令牌桶丢弃（dropped > 0）", dropped > 0, dropped)
H.chk("洪水没有把处理线程拖住（200 事件 < 5s）", flood_secs < 5.0, "%.2fs" % flood_secs)

# 正常速率：等桶回满后少量事件应当全部被处理（不被误伤）
time.sleep(2.5)
backend._event_bucket_stats["dropped"] = 0
for i in range(10):
    c.emit("ping")
H.chk("正常速率（10 事件）不被误伤", backend._event_bucket_stats["dropped"] == 0,
      backend._event_bucket_stats)
H.chk("on_ping 的 sid 映射存在且指向本连接",
      "sioev-dev" in backend._sid_to_client.values(), backend._sid_to_client)

# collect_all：设备不回报时必须 ≤3.5s 返回（原实现最多 20s）
http = backend.app.test_client()
t0 = time.time()
r = http.post("/api/log/collect_all", json={"timeout": 20},
              headers={"X-Printer-Token": TOK})
elapsed = time.time() - t0
H.chk("collect_all 请求 timeout=20 时仍在 ≤3.5s 内返回（被夹到 3s）",
      r.status_code == 200 and elapsed <= 3.5, "%.2fs status=%s" % (elapsed, r.status_code))

# 第二次并发调用：信号量占满 → 503
backend._collect_sem.acquire(blocking=False)   # 模拟"上一次还在飞"
r2 = http.post("/api/log/collect_all", json={"timeout": 2},
               headers={"X-Printer-Token": TOK})
backend._collect_sem.release()
H.chk("并发第二次收集 → 503（快速失败，不排队占线程）", r2.status_code == 503,
      r2.status_code)

c.disconnect()
time.sleep(0.2)
H.chk("断开后 sid 映射被清掉", "sioev-dev" not in backend._sid_to_client.values(),
      backend._sid_to_client)

# ============================================================
# C. 变异自证
# ============================================================
print("\n=== C. 变异自证（async_handlers 改回 True / 关掉令牌桶）===")
mut_tmp, mut_dst = H.mutated_copy([
    ("                    async_handlers=False, ping_interval=15, ping_timeout=10,",
     "                    async_handlers=True, ping_interval=15, ping_timeout=10,"),
], prefix="hn_sioev_mut_")
proc = H.run_probe(mut_dst, 'print("RESULT=%s" % backend.socketio.server.async_handlers)')
H.chk("async_handlers 改回 True 后判据能红（读到 True）",
      H.probe_value(proc) == "True", H.probe_value(proc))

mut2_tmp, mut2_dst = H.mutated_copy([
    ("        if not _event_rate_ok():\n            return None",
     "        if False:\n            return None"),
], prefix="hn_sioev_mut2_")
proc2 = H.run_probe(mut2_dst, """
backend.PRINTER_TOKEN = "sioev-token"
c = backend.socketio.test_client(backend.app, query_string="?token=sioev-token&client_id=mut-dev")
c.get_received()
for i in range(200):
    c.emit("ping")
print("RESULT=%d" % backend._event_bucket_stats["dropped"])
""")
H.chk("关掉令牌桶后 200 个事件一个都没被丢（判据能红）",
      H.probe_value(proc2) == "0", H.probe_value(proc2))

H.finish()
