"""staging_server.py · staging 后端启动器（不改 app.py，只注入运行时/环境）

用法（一般由 staging_up.py 拉起）：
  python staging_server.py --instance-dir <副本目录> --port 18081 --control-port 18082 \
      --pool-size 16

服务模型 = `socketserver.TCPServer + ThreadPoolExecutor(16)` 包裹后端真实 WSGI 应用，
语义对齐生产 gunicorn `-k gthread --threads 16`（gunicorn 不支持 Windows）：
一个进行中的请求占一个 worker，处理器阻塞时线程不回收到池里。

旁路控制端口（同进程独立线程，**不占 app 线程池**）：池被打满时仍能读线程数/RSS/会话数。

隔离：
  · 在 import app 之前把 `HN_CONFIG_PATH` 指向副本的 config.py；
  · `--stub-upstream`（默认开）把微信上游打桩成立刻失败 —— staging 不出网；
  · 只在副本目录里 chdir / 建库 / 写日志。
"""
import argparse
import json
import os
import sys
import threading
import time

ap = argparse.ArgumentParser()
ap.add_argument("--instance-dir", required=True)
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--control-port", type=int, required=True)
ap.add_argument("--pool-size", type=int, default=16)
ap.add_argument("--no-stub-upstream", action="store_true",
                help="不打断微信上游（默认打桩：staging 不出网）")
args = ap.parse_args()

INSTANCE = os.path.abspath(args.instance_dir)
CFG = os.path.join(INSTANCE, "config.py")
assert os.path.isfile(CFG), "缺少实例 config.py: %s" % CFG
os.environ["HN_CONFIG_PATH"] = CFG
os.chdir(INSTANCE)
sys.path.insert(0, INSTANCE)

import app as backend  # noqa: E402

# ---- 微信上游打桩：立刻失败，绝不出网 ----
if not args.no_stub_upstream:
    import urllib.error as _urlerr

    def _fake_urlopen(req, timeout=None, *a, **k):
        raise _urlerr.URLError("staging-stub: upstream disabled (no network)")

    backend.urlrequest.urlopen = _fake_urlopen
    if hasattr(backend, "_wx_http"):
        import requests as _rq

        def _fake_requests_get(url, *a, **k):
            raise _rq.exceptions.ConnectionError(
                "staging-stub: upstream disabled (no network)")
        backend._wx_http.get = _fake_requests_get

STATS = {"started_at": time.time(), "pool_size": args.pool_size,
         "instance": INSTANCE, "stub_upstream": not args.no_stub_upstream}


def rss_kb():
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    h = k32.GetCurrentProcess()
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    ok = psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
    return pmc.WorkingSetSize // 1024 if ok else None


def _thread_names():
    return [t.name for t in threading.enumerate()]


class CtlHandler(__import__("http.server", fromlist=["BaseHTTPRequestHandler"]).BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, info):
        body = json.dumps(info, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self._json({"ok": True, "ts": round(time.time() - STATS["started_at"], 3)})
            return
        if self.path != "/stats":
            self.send_error(404)
            return
        from collections import Counter
        names = _thread_names()
        buckets = Counter()
        for n in names:
            if "process_request_thread" in n or "Thread-" in n:
                buckets["app_request_thread"] += 1
            elif "simple_websocket.Base._thread" in n:
                buckets["ws_reader"] += 1
            elif "(writer)" in n:
                buckets["engineio_writer"] += 1
            elif "(_send_ping)" in n:
                buckets["engineio_ping"] += 1
            elif "(_service_task)" in n:
                buckets["service_task"] += 1
            elif "gthread" in n:
                buckets["pool_worker"] += 1
            elif n.startswith("ctl"):
                buckets["ctl"] += 1
            else:
                buckets["other"] += 1
        self._json({
            "ts": round(time.time() - STATS["started_at"], 3),
            "pool_size": STATS["pool_size"],
            "instance": STATS["instance"],
            "stub_upstream": STATS["stub_upstream"],
            "active_count": threading.active_count(),
            "thread_buckets": dict(buckets),
            "thread_names": names,
            "engineio_sessions": len(getattr(backend.socketio.server, "sockets", {}) or {}),
            "pool": STATS["pool_ref"].pool_snapshot() if STATS.get("pool_ref") else None,
            "runtime": getattr(backend, "_ASYNC_MODE", "threading"),
            "scheduler_running": bool(getattr(getattr(backend, "scheduler", None), "running", False)),
            "rss_kb": rss_kb(),
            "printer_token_is_staging": "staging-only" in str(getattr(backend, "PRINTER_TOKEN", "")),
        })


def start_control(port):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", port), CtlHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="ctl", daemon=True).start()
    return srv


# ---------------- 固定线程池 WSGI 服务器（≈ gthread）----------------
import socketserver                                           # noqa: E402
from concurrent.futures import ThreadPoolExecutor             # noqa: E402
from werkzeug.serving import WSGIRequestHandler               # noqa: E402


class PooledWSGIServer(socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 1024
    ssl_context = None
    multithread = True
    multiprocess = False
    passthrough_errors = False
    protocol_version = "HTTP/1.1"

    def __init__(self, addr, wsgi_app, workers):
        self.app = wsgi_app
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gthread")
        self._plock = threading.Lock()
        self._inflight = 0
        self._pending = 0
        self._max_inflight = 0
        STATS["pool_ref"] = self
        super().__init__(addr, Handler)

    def log(self, level, msg):
        pass

    def pool_snapshot(self):
        with self._plock:
            return {"inflight": self._inflight, "pending": self._pending,
                    "max_inflight": self._max_inflight,
                    "created_threads": len(getattr(self.pool, "_threads", []))}

    def process_request(self, request, client_address):
        with self._plock:
            self._pending += 1
        self.pool.submit(self._serve, request, client_address)

    def _serve(self, request, client_address):
        with self._plock:
            self._pending -= 1
            self._inflight += 1
            self._max_inflight = max(self._max_inflight, self._inflight)
        try:
            self.finish_request(request, client_address)
        except Exception:                                      # noqa: BLE001
            pass
        finally:
            with self._plock:
                self._inflight -= 1
            try:
                self.shutdown_request(request)
            except Exception:                                  # noqa: BLE001
                pass


class Handler(WSGIRequestHandler):
    def log_message(self, *a):
        pass

    def handle(self):
        try:
            super().handle()
        except Exception:                                      # noqa: BLE001
            pass


def main():
    try:
        backend.init_db()
        print("[SRV] init_db ok", flush=True)
    except Exception as e:                                     # noqa: BLE001
        print("[SRV] init_db 失败: %s: %s" % (type(e).__name__, e), flush=True)
    start_control(args.control_port)
    print("[SRV] instance=%s app=:%d ctl=:%d pool=%d runtime=%s stub_upstream=%s"
          % (INSTANCE, args.port, args.control_port, args.pool_size,
             getattr(backend, "_ASYNC_MODE", "?"), not args.no_stub_upstream), flush=True)
    srv = PooledWSGIServer(("127.0.0.1", args.port), backend.app, args.pool_size)
    print("[SRV] pool server ready", flush=True)
    srv.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    main()
