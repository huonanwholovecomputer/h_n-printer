"""srv.py · 实验用后端启动器（只改副本进程内的运行时行为，不改 app.py 源码）

用法（cwd = %TEMP%\\ddos_audit_copy，由 common.ServerProc 自动设置）：
  python srv.py --port 18080 --control-port 18099 --mode werkzeug
  python srv.py --port 18081 --control-port 18098 --mode pool --pool-size 16
  python srv.py --port 18082 --control-port 18097 --mode pool --pool-size 16 --login-delay 10

两种服务模型：
  werkzeug : socketio.run(app, threaded=True, allow_unsafe_werkzeug=True)
             —— Werkzeug ThreadingWSGIServer，每连接一条线程（无上限）。用于实验 1 数线程。
  pool     : socketserver.TCPServer（接受连接） + concurrent.futures.ThreadPoolExecutor(16)
             包裹 Flask-SocketIO 的 WSGI app —— 语义对齐 gunicorn gthread（Gunicorn 不支持
             Windows，故为「模拟」）：一个进行中的请求占一个 worker 线程，处理器阻塞时线程
             不回收到池里；池满后新连接只能排在操作系统 accept 队列里。

旁路控制端口（同进程、独立 HTTP 服务、独立线程）：/stats 返回线程普查 / RSS / Engine.IO 会话数。
它不被 app 线程池占用，所以池被打满时仍能观测。
"""
import argparse
import json
import os
import sys
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COPY = os.path.join(tempfile.gettempdir(), "ddos_audit_copy")
os.chdir(COPY)
sys.path.insert(0, COPY)

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--control-port", type=int, required=True)
ap.add_argument("--mode", choices=["werkzeug", "pool"], default="werkzeug")
ap.add_argument("--pool-size", type=int, default=16)
ap.add_argument("--login-delay", type=float, default=0.0,
                help=">0 时把 app.urlrequest.urlopen 打桩成 sleep(N) 后抛 URLError（模拟微信上游 10s 延迟）")
args = ap.parse_args()

import app as backend  # noqa: E402

if args.login_delay > 0:
    import urllib.error as _urlerr

    def _fake_urlopen(req, timeout=None, *a, **k):
        time.sleep(args.login_delay)
        raise _urlerr.URLError(
            f"audit-stub: simulated {args.login_delay}s upstream latency (no network)")
    # app.py 里是 `from urllib import request as urlrequest`，调用点查的是模块属性
    backend.urlrequest.urlopen = _fake_urlopen
    # 2026-10 起 /api/login 改用 requests.Session()（_wx_http）：同一段打桩也要盖住它，
    # 否则实验会真的打到 api.weixin.qq.com（假 appid 会被回 40013，且外发请求）。
    if hasattr(backend, "_wx_http"):
        import requests as _rq

        def _fake_requests_get(url, *a, **k):
            time.sleep(args.login_delay)
            raise _rq.exceptions.ConnectionError(
                f"audit-stub: simulated {args.login_delay}s upstream latency (no network)")

        backend._wx_http.get = _fake_requests_get

STATS = {"started_at": time.time(), "mode": args.mode, "pool_size": args.pool_size,
         "login_delay": args.login_delay}


def rss_kb():
    """本进程 WorkingSetSize（KB），不依赖 psutil。"""
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
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


def _thread_census():
    names = Counter()
    for t in threading.enumerate():
        n = t.name
        # 归并动态后缀（如 Thread-12 (_serve)）便于阅读
        n = n.split("(")[0].strip()
        names[n] += 1
    return dict(names.most_common())


class CtlHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path != "/stats":
            self.send_error(404)
            return
        info = {
            "ts": round(time.time() - STATS["started_at"], 3),
            "mode": STATS["mode"],
            "pool_size": STATS["pool_size"],
            "login_delay": STATS["login_delay"],
            "active_count": threading.active_count(),
            "threads": _thread_census(),
            "thread_names": [t.name for t in threading.enumerate()],
            "engineio_sessions": len(getattr(backend.socketio.server, "sockets", {}) or {}),
            "pool": STATS["pool_ref"].pool_snapshot() if STATS.get("pool_ref") else None,
            "rss_kb": rss_kb(),
            "alive": True,
        }
        body = json.dumps(info, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_control(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), CtlHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="ctl", daemon=True).start()
    return srv


# ---------------- 模型 B：固定 16 线程池的 WSGI 服务器（gthread 模拟）----------------
if args.mode == "pool":
    import socketserver
    from concurrent.futures import ThreadPoolExecutor
    from werkzeug.serving import WSGIRequestHandler

    class PooledWSGIServer(socketserver.TCPServer):
        """语义对齐 gunicorn gthread：一个进行中的请求 = 一个 worker 线程，不因阻塞而回收。"""
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
            self._inflight = 0   # 已开始执行、尚未结束的请求（= 被占住的 worker 线程数）
            self._pending = 0    # 已入池、还没轮到线程的请求
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
            # 入池；池满时任务在池队列里排队（等价于 gthread 的 accept 后等待空闲线程）
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
            except Exception:
                pass
            finally:
                with self._plock:
                    self._inflight -= 1
                try:
                    self.shutdown_request(request)
                except Exception:
                    pass

    class Handler(WSGIRequestHandler):
        def log_message(self, *a):
            pass

        def handle(self):
            try:
                super().handle()
            except Exception:
                pass


def main():
    start_control(args.control_port)
    print(f"[SRV] mode={args.mode} app=127.0.0.1:{args.port} ctl=127.0.0.1:{args.control_port} "
          f"pool={args.pool_size} login_delay={args.login_delay}", flush=True)
    if args.mode == "werkzeug":
        backend.socketio.run(
            backend.app, host="127.0.0.1", port=args.port,
            allow_unsafe_werkzeug=True, use_reloader=False,
        )
    else:
        # backend.app 的 wsgi_app 已被 Flask-SocketIO 的中间件包裹（/socket.io/ → engineio，
        # 其余 → Flask 路由），与 gunicorn 实际加载的 WSGI 对象一致
        srv = PooledWSGIServer(("127.0.0.1", args.port), backend.app, args.pool_size)
        print("[SRV] pool server ready", flush=True)
        srv.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    main()
