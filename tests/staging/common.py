"""DDoS 可用性评估 · 公共工具（实验 D）

本文件不导入被测后端，只提供：路径常量、子进程环境、HTTP 打点、RSS 读取、
控制端口启停、控制端口查询、WS/长轮询原始连接工具。

所有实验只操作隔离副本 %TEMP%\\ddos_audit_copy，绝不触碰生产目录/生产库。
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from urllib import request as urlreq, error as urlerr

# ---- 固定路径（本机可重跑）----
# 用「当前解释器」：run_all / 手动跑都用同一个 Python（依赖 Flask/APScheduler 的那个）。
PY = sys.executable
# 本文件在 <repo>/tests/staging/common.py ⇒ 上三级是仓库根（不写死盘符，换机器可用）。
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND = os.path.join(PROJECT, "mobile_apps", "printer-backend")
COPY = os.path.join(tempfile.gettempdir(), "ddos_audit_copy")  # = %TEMP%\ddos_audit_copy
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(SCRIPTS, "results")
# 隔离副本 config.py 里的假 printer token（由 00_make_copy.py 写入；与生产无关）
PRINTER_TOKEN = "fake-printer-token-for-audit"


def child_env():
    """子进程环境：PYTHONPATH 必须置空（被注入的 shim 会改 os.remove 等语义）。"""
    e = dict(os.environ)
    e["PYTHONPATH"] = ""
    e["PYTHONUTF8"] = "1"
    e["PYTHONIOENCODING"] = "utf-8"
    e["NO_PROXY"] = "127.0.0.1,localhost"
    e["no_proxy"] = "127.0.0.1,localhost"
    e.pop("FLASK_DEBUG", None)
    return e


def save_result(name, payload):
    os.makedirs(RESULTS, exist_ok=True)
    p = os.path.join(RESULTS, name + ".json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[RESULT] {p}")
    return p


# ---------------- HTTP 打点 ----------------
def http_get(url, timeout=3.0, headers=None):
    """返回 (status, body_text, elapsed_ms)；异常返回 (None|'timeout', repr(e), elapsed_ms)。"""
    req = urlreq.Request(url, headers=headers or {})
    t0 = time.perf_counter()
    try:
        with urlreq.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, body, (time.perf_counter() - t0) * 1000
    except urlerr.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return e.code, body, (time.perf_counter() - t0) * 1000
    except (urlerr.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        return "timeout", repr(e), (time.perf_counter() - t0) * 1000
    except Exception as e:  # pragma: no cover
        return "error", repr(e), (time.perf_counter() - t0) * 1000


def http_post(url, body: bytes, timeout=3.0, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urlreq.Request(url, data=body, headers=h, method="POST")
    t0 = time.perf_counter()
    try:
        with urlreq.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), (time.perf_counter() - t0) * 1000
    except urlerr.HTTPError as e:
        try:
            b = e.read().decode("utf-8", "replace")
        except Exception:
            b = ""
        return e.code, b, (time.perf_counter() - t0) * 1000
    except (urlerr.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        return "timeout", repr(e), (time.perf_counter() - t0) * 1000
    except Exception as e:  # pragma: no cover
        return "error", repr(e), (time.perf_counter() - t0) * 1000


def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return round(s[k], 2)


def summarize(lat_ms):
    """把一列耗时（含 'timeout' 字符串）汇总成 p50/p95/超时率。"""
    ok = [v for v in lat_ms if isinstance(v, (int, float))]
    to = [v for v in lat_ms if not isinstance(v, (int, float))]
    return {
        "n": len(lat_ms),
        "ok": len(ok),
        "timeout": len(to),
        "timeout_rate": round(len(to) / len(lat_ms), 3) if lat_ms else 0.0,
        "p50_ms": pct(ok, 50),
        "p95_ms": pct(ok, 95),
        "max_ms": round(max(ok), 2) if ok else None,
    }


def summarize_http(results):
    """results = [(status|'timeout', body, elapsed_ms), ...] → 状态分布 + 耗时分位数。"""
    from collections import Counter
    codes = Counter()
    lat_ok = []
    lat_all = []
    for st, body, ms in results:
        if st == "timeout":
            codes["timeout"] += 1
            continue
        codes[str(st)] += 1
        lat_all.append(ms)
        if st == 200:
            lat_ok.append(ms)
    n = len(results)
    return {
        "n": n,
        "status": dict(codes),
        "ok_200": codes.get("200", 0),
        "timeout": codes.get("timeout", 0),
        "timeout_rate": round(codes.get("timeout", 0) / n, 3) if n else 0.0,
        "p50_ms": pct(lat_ok, 50),
        "p95_ms": pct(lat_ok, 95),
        "max_ms": round(max(lat_ok), 2) if lat_ok else None,
        "p50_ms_all": pct(lat_all, 50),
        "p95_ms_all": pct(lat_all, 95),
        "max_ms_all": round(max(lat_all), 2) if lat_all else None,
    }


# ---------------- RSS（本进程 / 指定 pid）----------------
def rss_kb(pid=None):
    """Windows WorkingSetSize（KB）。不依赖 psutil。"""
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    if pid is None:
        h = k32.GetCurrentProcess()
        close = False
    else:
        h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            return None
        close = True
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    ok = psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
    if close:
        k32.CloseHandle(h)
    return pmc.WorkingSetSize // 1024 if ok else None


# ---------------- 控制端口（同进程旁路，绕开被占满的 app 线程池）----------------
def read_stats(control_port, timeout=2.0):
    st, body, ms = http_get(f"http://127.0.0.1:{control_port}/stats", timeout=timeout)
    if st != 200:
        return None
    try:
        d = json.loads(body)
        d["_elapsed_ms"] = round(ms, 2)
        return d
    except json.JSONDecodeError:
        return None


def wait_control(control_port, timeout=25.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            d = read_stats(control_port, timeout=1.0)
            if d:
                return d
        except Exception:
            pass
        time.sleep(0.15)
    raise RuntimeError(f"控制端口 {control_port} 未就绪")


def wait_port(port, timeout=25.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"端口 {port} 未就绪")


class ServerProc:
    """启动 srv.py 子进程；收集 stdout 便于文档引用。"""

    def __init__(self, port, control_port, mode="werkzeug", pool_size=16, login_delay=0.0):
        self.port = port
        self.control_port = control_port
        self.proc = None
        self.log_path = os.path.join(RESULTS, f"srv_{mode}_{port}.log")
        os.makedirs(RESULTS, exist_ok=True)
        self._f = open(self.log_path, "wb")
        cmd = [
            PY, os.path.join(SCRIPTS, "srv.py"),
            "--port", str(port),
            "--control-port", str(control_port),
            "--mode", mode,
            "--pool-size", str(pool_size),
            "--login-delay", str(login_delay),
        ]
        self.cmd = cmd
        self.proc = subprocess.Popen(
            cmd, cwd=COPY, env=child_env(), stdout=self._f, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

    def wait(self):
        wait_port(self.port)
        return wait_control(self.control_port)

    def stats(self, timeout=2.0):
        return read_stats(self.control_port, timeout=timeout)

    def tail_log(self, n=40):
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            return "".join(lines[-n:])
        except OSError:
            return ""

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=8)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        try:
            self._f.close()
        except Exception:
            pass

    def __enter__(self):
        self.wait()
        return self

    def __exit__(self, *a):
        self.stop()
        return False


# ---------------- 原始 Engine.IO WebSocket 客户端 ----------------
def eio_ws_status(port, token=None, timeout=6.0):
    """尝试一次 Engine.IO WebSocket 握手，返回 (status, note)。

    status = 101 表示升级成功（收到 Engine.IO OPEN 包 "0{...}"）；
    403 = 被 S0-02 的握手门拦下（无 token / 错 token）；"error" = 其它异常。
    token 走 `X-Printer-Token` 请求头（与生产设备一致，不进 URL）。
    """
    import websocket
    headers = [f"X-Printer-Token: {token}"] if token else []
    try:
        ws = websocket.create_connection(
            f"ws://127.0.0.1:{port}/socket.io/?EIO=4&transport=websocket",
            timeout=timeout, enable_multithread=True,
            http_no_proxy=["127.0.0.1", "localhost"], header=headers or None,
        )
        pkt = ws.recv()
        ws.close()
        return (101, pkt[:40]) if pkt.startswith("0") else ("bad-packet", pkt[:40])
    except websocket.WebSocketBadStatusException as e:
        return (e.status_code, "")
    except Exception as e:  # pragma: no cover
        return ("error", repr(e)[:120])


def open_eio_ws(port, timeout=6.0, path="/socket.io/?EIO=4&transport=websocket", token=None):
    """建立一条 Engine.IO WebSocket。

    · token=None：未认证握手 —— S0-02 之后会被门拦成 403（`eio_ws_status` 用来断言这一点）
    · token=<printer token>：真实设备形态，握手门放行
    返回 websocket.WebSocket 对象；调用方负责 close()。
    """
    import websocket  # websocket-client
    headers = [f"X-Printer-Token: {token}"] if token else []
    ws = websocket.create_connection(
        f"ws://127.0.0.1:{port}{path}",
        timeout=timeout,
        enable_multithread=True,
        http_no_proxy=["127.0.0.1", "localhost"],
        header=headers or None,
    )
    open_pkt = ws.recv()  # Engine.IO OPEN "0{...}"
    assert open_pkt.startswith("0"), open_pkt[:80]
    return ws


def open_raw_polling(port, timeout=6.0, token=None):
    """完成 Engine.IO long-polling 握手（GET 拿 open 包），返回 (sock, sid, open_pkt)。

    token 走请求头（S0-02 的门对无 sid 的握手要求 token）。
    返回的 sock 仍连着（服务端回 Connection: close，调用方读完丢弃即可）。
    """
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    auth = f"X-Printer-Token: {token}\r\n" if token else ""
    req = (
        f"GET /socket.io/?EIO=4&transport=polling HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n{auth}Connection: close\r\n\r\n"
    )
    s.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    # 按 Content-Length 读满 body
    clen = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            try:
                clen = int(line.split(b":", 1)[1].strip())
            except ValueError:
                clen = 0
    while len(rest) < clen:
        chunk = s.recv(4096)
        if not chunk:
            break
        rest += chunk
    if clen == 0:  # 兜底：读到含 sid 的 JSON 结束
        while b'"sid"' not in rest or not rest.rstrip().endswith(b"}"):
            chunk = s.recv(4096)
            if not chunk:
                break
            rest += chunk
    text = rest.decode("utf-8", "replace")
    sid = None
    i = text.find('"sid"')
    if i >= 0:
        j = text.find('"', text.find(":", i) + 1)
        k = text.find('"', j + 1)
        sid = text[j + 1:k]
    return s, sid, text


def hold_polling(port, sid, timeout=6.0):
    """发第二个 long-polling GET（带 sid）——服务端会阻塞到有包可发，占住一个 worker 线程。"""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    req = (
        f"GET /socket.io/?EIO=4&transport=polling&sid={sid} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\nConnection: keep-alive\r\n\r\n"
    )
    s.sendall(req.encode())
    return s
