"""隔离 staging 公共库（S0-01 / S2-12 验收用）

拓扑（全部 127.0.0.1，与生产同形状：边缘 → 单进程多线程后端）：

    client ──► nginx 边缘 127.0.0.1:18080 ──► backend 127.0.0.1:18081
               （复刻生产 S0-01 指令）          （socketserver + 16 线程池 ≈ gunicorn gthread）

隔离边界（本文件是唯一真相，脚本不得绕过）：
  · 实例目录 `_tmp_staging/instance/` 是 `mobile_apps/printer-backend` 的**整份拷贝**，
    库文件（orders.db / users.db）是仓库里的开发副本，**不连生产、不读生产库**；
  · config.py 由本文件生成，`TOKEN` 是 staging 专用假 token（与生产不同）；
  · 后端进程用 `HN_CONFIG_PATH` 显式指向实例 config.py（不改 app.py）；
  · 上游微信调用一律打桩（staging 不出网）；
  · nginx 只用本文件渲染的配置（`_tmp_staging/nginx/conf/hn-staging.conf`），
    `client_body_temp_path` / 日志 / pid 全在 `_tmp_staging/` 内。

环境坑（本仓库已知）：
  · `PYTHONPATH` 必须置空（被注入的 shim 会替换 os.remove）；
  · `PYTHONUTF8=1`；
  · 子进程一律 `NO_PROXY=127.0.0.1,localhost`（本机代理会掐断回环长连接）。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ---------------- 路径 ----------------
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))          # 仓库根
BACKEND_SRC = os.path.join(PROJECT, "mobile_apps", "printer-backend")
WORK = os.path.join(PROJECT, "_tmp_staging")
INSTANCE = os.path.join(WORK, "instance")
LOGS = os.path.join(WORK, "logs")
RUN = os.path.join(WORK, "run")
RESULTS = os.path.join(WORK, "results")
NGINX_ROOT = os.path.join(WORK, "nginx")                  # nginx -p
NGINX_HOME = os.path.join(WORK, "vendor", "nginx-1.29.0")
NGINX_EXE = os.path.join(NGINX_HOME, "nginx.exe")
NGINX_CONF = os.path.join(NGINX_ROOT, "conf", "hn-staging.conf")
NGINX_CONF_REL = "conf/hn-staging.conf"      # nginx 命令行只认相对路径（见 render_nginx_conf）
BACKEND_PID = os.path.join(RUN, "backend.pid")
NGINX_PID = os.path.join(NGINX_ROOT, "logs", "nginx.pid")
BACKEND_LOG = os.path.join(LOGS, "backend.log")
NGINX_ERROR_LOG = os.path.join(NGINX_ROOT, "logs", "error.log")
NGINX_ACCESS_LOG = os.path.join(NGINX_ROOT, "logs", "access.log")
NGINX_TEMP = os.path.join(NGINX_ROOT, "temp")

# ---------------- 端口 ----------------
EDGE_PORT = 18080          # nginx：所有验收打这个（与计划建议一致）
APP_PORT = 18081           # backend（只在边缘后面，正常不直连）
CTL_PORT = 18082           # 旁路控制端口（池满时仍可观测）
POOL_SIZE = 16             # 生产 gunicorn threads=16

PY = sys.executable

# ---------------- staging 专用假凭据 ----------------
STAGING_TOKEN = "staging-only-fake-token-2026-10-25-8f3a1c"
# 多源回环 IP 池：S2-12 用不同源 IP 模拟不同门店设备（nginx limit_conn 按 source IP 计数）
DEVICE_IPS = ["127.0.0.%d" % i for i in range(2, 32)]

FAKE_CONFIG = '''# staging 专用假配置（tools/staging/staging_common.py 生成）—— 不含任何生产值
WECHAT_APPID = "wxSTAGINGFAKE00001"
WECHAT_APPSECRET = "STAGINGFAKESECRET0000000000000000"
SECRET_KEY = "staging-fake-secret-key-0000000000000001"
ADMIN_OPENIDS = ["staging_admin_openid"]
SUPER_ADMIN_OPENID = "staging_admin_openid"
PUBLIC_BASE_URL = "http://127.0.0.1:18080"
CLOUD_API_URL = "http://127.0.0.1:18080"
WEBSOCKET_URL = "http://127.0.0.1:18080"
TOKEN = "%s"
PRINTER_NAME = ""
''' % STAGING_TOKEN


# ---------------- 子进程环境 ----------------
def child_env(extra=None):
    e = dict(os.environ)
    e["PYTHONPATH"] = ""
    e["PYTHONUTF8"] = "1"
    e["PYTHONIOENCODING"] = "utf-8"
    e["NO_PROXY"] = "127.0.0.1,localhost"
    e["no_proxy"] = "127.0.0.1,localhost"
    e.pop("FLASK_DEBUG", None)
    e.pop("HN_RUNTIME", None)          # staging 用 gthread 语义（与生产一致）
    e["HN_CONFIG_PATH"] = os.path.join(INSTANCE, "config.py")
    if extra:
        e.update(extra)
    return e


def ensure_dirs():
    for d in (WORK, LOGS, RUN, RESULTS, NGINX_ROOT,
              os.path.join(NGINX_ROOT, "conf"), os.path.join(NGINX_ROOT, "logs"),
              os.path.join(NGINX_TEMP, "client_body_temp")):
        os.makedirs(d, exist_ok=True)


# ---------------- 实例副本 ----------------
def create_instance(force=False):
    """建 `_tmp_staging/instance/`：backend 整份拷贝 + 假 config + 库副本。

    幂等：已存在且非 force 时直接返回。返回 (文件数, 秒数)。
    """
    if os.path.isdir(INSTANCE) and not force:
        return (None, 0.0)
    t0 = time.time()
    if os.path.isdir(INSTANCE):
        shutil.rmtree(INSTANCE, ignore_errors=True)
    os.makedirs(INSTANCE, exist_ok=True)
    copied = 0
    for root, dirs, files in os.walk(BACKEND_SRC):
        rel = os.path.relpath(root, BACKEND_SRC)
        parts = [] if rel == "." else rel.split(os.sep)
        if "__pycache__" in parts or (parts and parts[0] in ("logs", "uploads", "build", "dist")):
            continue
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "build", "dist")]
        dst_root = os.path.join(INSTANCE, rel) if parts else INSTANCE
        os.makedirs(dst_root, exist_ok=True)
        for fn in files:
            if fn.endswith(".pyc") or fn == "config.py":
                continue
            shutil.copy2(os.path.join(root, fn), os.path.join(dst_root, fn))
            copied += 1
    for d in ("uploads", "logs", os.path.join("logs", "audit")):
        os.makedirs(os.path.join(INSTANCE, d), exist_ok=True)
    with open(os.path.join(INSTANCE, "config.py"), "w", encoding="utf-8") as f:
        f.write(FAKE_CONFIG)
    assert STAGING_TOKEN in open(os.path.join(INSTANCE, "config.py"),
                                 encoding="utf-8").read()
    dt = time.time() - t0
    print("[staging] 实例已建: %s（%d 文件）" % (INSTANCE, copied))
    print("[staging] 库副本: orders.db=%s users.db=%s" % (
        _fmt_size(os.path.join(INSTANCE, "orders.db")),
        _fmt_size(os.path.join(INSTANCE, "users.db"))))
    return (copied, dt)


def _fmt_size(p):
    return ("%.1f KB" % (os.path.getsize(p) / 1024.0)) if os.path.exists(p) else "缺失"


# ---------------- 端口/进程工具 ----------------
def port_open(port, host="127.0.0.1", timeout=0.4):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_port(port, timeout=30.0, host="127.0.0.1"):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if port_open(port, host):
            return True
        time.sleep(0.1)
    raise RuntimeError("端口 %d 未就绪（%.0fs）" % (port, timeout))


def pids_on_port(port):
    """Windows: netstat 找出 LISTENING 在该端口的 pid（可能为空）。"""
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                         text=True, errors="replace").stdout
    pids = set()
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 5 and f[0].upper() == "TCP" and f[1].endswith(":%d" % port) \
                and f[3].upper() == "LISTENING":
            try:
                pids.add(int(f[4]))
            except ValueError:
                pass
    return pids


def kill_pid(pid, label=""):
    if not pid:
        return
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=15)
        print("[staging] 已结束进程 %s %s" % (pid, label))
    except Exception as e:                                    # noqa: BLE001
        print("[staging] 结束进程 %s 失败: %s" % (pid, e))


def free_port(port, label=""):
    """确保端口干净：有残留监听 pid 就杀掉（只对 1808x 的 staging 端口调用）。"""
    pids = pids_on_port(port)
    for pid in pids:
        kill_pid(pid, "%s(port %d)" % (label, port))
    if pids:
        time.sleep(0.6)


# ---------------- 后端 ----------------
def start_backend(pool_size=POOL_SIZE):
    ensure_dirs()
    create_instance()
    if port_open(APP_PORT):
        pids = pids_on_port(APP_PORT)
        if BACKEND_PID_file() and BACKEND_PID_file() in pids:
            print("[staging] backend 已在运行（pid=%s, :%d）" % (BACKEND_PID_file(), APP_PORT))
            return
        free_port(APP_PORT, "旧 backend")
    log = open(BACKEND_LOG, "ab")
    cmd = [PY, os.path.join(HERE, "staging_server.py"),
           "--instance-dir", INSTANCE, "--port", str(APP_PORT),
           "--control-port", str(CTL_PORT), "--pool-size", str(pool_size)]
    p = subprocess.Popen(cmd, cwd=INSTANCE, env=child_env(),
                         stdout=log, stderr=subprocess.STDOUT,
                         creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    with open(BACKEND_PID, "w", encoding="utf-8") as f:
        f.write(str(p.pid))
    wait_port(APP_PORT, 40)
    wait_port(CTL_PORT, 40)
    print("[staging] backend 已起: pid=%d app=:%d ctl=:%d pool=%d"
          % (p.pid, APP_PORT, CTL_PORT, pool_size))


def BACKEND_PID_file():
    try:
        return int(open(BACKEND_PID, encoding="utf-8").read().strip())
    except Exception:                                          # noqa: BLE001
        return None


def stop_backend():
    pid = BACKEND_PID_file()
    if pid and pid in pids_on_port(APP_PORT):
        kill_pid(pid, "backend")
    else:
        free_port(APP_PORT, "backend")
    free_port(CTL_PORT, "backend-ctl")
    try:
        os.remove(BACKEND_PID)
    except OSError:
        pass


# ---------------- nginx ----------------
def render_nginx_conf():
    """用生产 nginx 配置（mobile_apps/printer-backend/nginx-production-hn-space.conf
    + nginx-conf-d-00-security-hardening.conf）1:1 改写成本机 staging 版：
    去掉 TLS/域名/ACME，proxy_pass 指向 127.0.0.1:18081，其余指令取值**保持生产原值**。

    ⚠ 配置里只写**相对路径**（相对 nginx 根 = `_tmp_staging/nginx`）。原因是
    nginx for Windows 的 `main()` 用 ANSI argv：命令行/配置里带中文绝对路径会
    `CreateFile() ... failed (1113: No mapping for the Unicode character ...)`。
    从 cwd 起（`-p` 缺省）时 nginx 走宽字符 API 取前缀，中文目录正常 —— 所以启动器
    一律 `cwd=NGINX_ROOT` + `-c conf/hn-staging.conf`，配置内全用相对路径。
    """
    ensure_dirs()
    # mime.types 拷到 nginx 根下，配置里用 conf/mime.types 相对引用
    src = os.path.join(NGINX_HOME, "conf", "mime.types")
    dst = os.path.join(NGINX_ROOT, "conf", "mime.types")
    if os.path.isfile(src):
        shutil.copyfile(src, dst)
    tpl = open(os.path.join(HERE, "nginx-staging.conf"), encoding="utf-8").read()
    conf = (tpl.replace("__EDGE_PORT__", str(EDGE_PORT))
               .replace("__APP_PORT__", str(APP_PORT)))
    with open(NGINX_CONF, "w", encoding="utf-8", newline="\n") as f:
        f.write(conf)
    return NGINX_CONF


def _nginx_run(args, timeout=30):
    return subprocess.run([NGINX_EXE] + args, cwd=NGINX_ROOT, capture_output=True,
                          text=True, errors="replace", timeout=timeout)


def nginx_test():
    render_nginx_conf()
    r = _nginx_run(["-c", NGINX_CONF_REL, "-t"])
    return r.returncode, (r.stdout + r.stderr).strip()


def nginx_dump():
    """`nginx -T`（等同生产 `nginx -T`）：返回完整生效配置文本。"""
    render_nginx_conf()
    r = _nginx_run(["-c", NGINX_CONF_REL, "-T"])
    return r.returncode, (r.stdout + r.stderr)


def start_nginx():
    ensure_dirs()
    create_instance()
    code, out = nginx_test()
    if code != 0:
        raise RuntimeError("nginx -t 失败:\n" + out)
    if port_open(EDGE_PORT):
        print("[staging] nginx 已在监听 :%d" % EDGE_PORT)
        return
    p = subprocess.Popen([NGINX_EXE, "-c", NGINX_CONF_REL],
                         cwd=NGINX_ROOT, env=child_env(),
                         stdout=open(os.path.join(LOGS, "nginx-stdout.log"), "ab"),
                         stderr=subprocess.STDOUT)
    wait_port(EDGE_PORT, 20)
    with open(os.path.join(RUN, "nginx_launcher.pid"), "w", encoding="utf-8") as f:
        f.write(str(p.pid))
    print("[staging] nginx 已起: pid=%s edge=:%d -> app=:%d"
          % (_nginx_pid() or p.pid, EDGE_PORT, APP_PORT))


def _nginx_pid():
    try:
        return int(open(NGINX_PID, encoding="utf-8").read().strip())
    except Exception:                                          # noqa: BLE001
        return None


def stop_nginx():
    if not port_open(EDGE_PORT) and not _nginx_pid():
        return
    # 先优雅 quit（-s 需要同一 prefix/conf；Windows 下等价于发控制信号）
    try:
        _nginx_run(["-c", NGINX_CONF_REL, "-s", "quit"], timeout=15)
    except Exception:                                          # noqa: BLE001
        pass
    for _ in range(30):
        if not port_open(EDGE_PORT):
            break
        time.sleep(0.1)
    pid = _nginx_pid()
    if pid:
        kill_pid(pid, "nginx")
    free_port(EDGE_PORT, "nginx")
    try:
        os.remove(NGINX_PID)
    except OSError:
        pass


def restart_nginx():
    """重启 nginx —— limit_req/limit_conn 的共享内存 zone 随之清零。

    验收脚本每次开跑先调它，保证「每个源 IP 的桶是满的」；否则同一分钟内连跑两遍，
    第二次会被上一遍的请求桶污染（表现为所有 /socket.io/ 直接 429）。
    """
    stop_nginx()
    wait_port_clear(EDGE_PORT)
    start_nginx()


def wait_port_clear(port, timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not port_open(port):
            return True
        time.sleep(0.1)
    return False


def edge_base():
    return "http://127.0.0.1:%d" % EDGE_PORT


# ---------------- HTTP 打点 ----------------
def http_request(method, path, body=None, headers=None, source_ip=None,
                 timeout=10.0, port=None, host="127.0.0.1"):
    """发一个 HTTP 请求（可绑源 IP）。返回 (status|'timeout'|'reset'|'error', body, ms, headers)。

    用 http.client 而不是 urllib：`source_address` 是官方支持的，正好用来模拟
    「不同门店设备来自不同 IP」（nginx limit_conn/limit_req 都按源 IP 计数）。
    """
    import http.client
    p = port or EDGE_PORT
    t0 = time.perf_counter()
    try:
        conn = http.client.HTTPConnection(
            host, p, timeout=timeout,
            source_address=(source_ip, 0) if source_ip else None)
    except OSError as e:                                       # noqa: BLE001
        return "error", repr(e)[:160], (time.perf_counter() - t0) * 1000, {}
    try:
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        data = r.read()
        return (r.status, data.decode("utf-8", "replace"),
                (time.perf_counter() - t0) * 1000, dict(r.getheaders()))
    except (BrokenPipeError, ConnectionResetError) as e:
        ms = (time.perf_counter() - t0) * 1000
        # 服务端提前回 413/429 后直接断开：已到达的响应仍要读出来（否则会把 413 记成断连）
        try:
            conn.close()
            conn = http.client.HTTPConnection(
                host, p, timeout=timeout,
                source_address=(source_ip, 0) if source_ip else None)
            r = conn.getresponse()
            return r.status, r.read().decode("utf-8", "replace"), ms, dict(r.getheaders())
        except Exception:                                      # noqa: BLE001
            return "reset", repr(e)[:160], ms, {}
    except (socket.timeout, TimeoutError) as e:
        return "timeout", repr(e)[:160], (time.perf_counter() - t0) * 1000, {}
    except OSError as e:
        return "error", repr(e)[:160], (time.perf_counter() - t0) * 1000, {}
    finally:
        try:
            conn.close()
        except Exception:                                      # noqa: BLE001
            pass


def http_get(path_or_url, timeout=5.0, headers=None, source_ip=None, port=None):
    path = path_or_url
    if path.startswith("http://"):
        path = "/" + path.split("/", 3)[3] if path.count("/") >= 3 else "/"
    st, body, ms, _h = http_request("GET", path, headers=headers, source_ip=source_ip,
                                    timeout=timeout, port=port)
    return st, body, ms


def http_post(url, data, timeout=60.0, headers=None, source_ip=None, port=None):
    path = url
    if path.startswith("http://"):
        path = "/" + path.split("/", 3)[3] if path.count("/") >= 3 else "/"
    st, body, ms, _h = http_request("POST", path, body=data, headers=headers,
                                    source_ip=source_ip, timeout=timeout, port=port)
    return st, body, ms


def pct(vals, p):
    vals = sorted(v for v in vals if isinstance(v, (int, float)))
    if not vals:
        return None
    k = min(len(vals) - 1, max(0, int(round((p / 100.0) * (len(vals) - 1)))))
    return round(vals[k], 2)


# ---------------- Engine.IO / WS（原始握手：可绑源 IP） ----------------
class RawWS:
    """一条建好的 Engine.IO WebSocket（原始 socket，服务端帧不掩码）。"""

    def __init__(self, sock, open_pkt, headers):
        self.sock = sock
        self.open_pkt = open_pkt
        self.headers = headers
        self.closed = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            # 尽量发一个 WS Close 帧（0x88），失败就直接断开
            self.sock.sendall(b"\x88\x80" + b"\x00\x00\x00\x00")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _read_frame(sock):
    """读一个 WS 帧，返回 (opcode, payload)。仅实现服务端→客户端（不掩码）。"""
    hdr = _recv_exact(sock, 2)
    if not hdr:
        return None, b""
    b0, b1 = hdr[0], hdr[1]
    opcode = b0 & 0x0F
    ln = b1 & 0x7F
    if ln == 126:
        ln = int.from_bytes(_recv_exact(sock, 2), "big")
    elif ln == 127:
        ln = int.from_bytes(_recv_exact(sock, 8), "big")
    payload = _recv_exact(sock, ln) if ln else b""
    return opcode, payload


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return buf
        if not chunk:
            return buf
        buf += chunk
    return buf


def ws_connect(source_ip=None, token=None,
               path="/socket.io/?EIO=4&transport=websocket",
               timeout=8.0, port=None, host="127.0.0.1"):
    """完成一次 Engine.IO WebSocket 握手（自己发 HTTP Upgrade）。

    返回 (ws|None, open_pkt|None, err|None)：
      · 成功 → (RawWS, "0{...}", None)，调用方负责 close()
      · 被边缘挡 → (None, None, {"kind":"http","status":429,...})
      · 被 S0-02 握手门挡 → status=403
    这样能**指定源 IP**（nginx limit_conn 按 source IP 计数），也便于 S2-12 模拟多门店。
    """
    import base64
    p = port or EDGE_PORT
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    t0 = time.perf_counter()
    try:
        if source_ip:
            sock.bind((source_ip, 0))
        sock.connect((host, p))
    except OSError as e:
        sock.close()
        return None, None, {"kind": "error", "status": None, "err": repr(e)[:160]}
    key = base64.b64encode(os.urandom(16)).decode()
    lines = [
        "GET %s HTTP/1.1" % path,
        "Host: %s:%d" % (host, p),
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Key: %s" % key,
        "Sec-WebSocket-Version: 13",
    ]
    if token:
        lines.append("X-Printer-Token: %s" % token)
    try:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    except OSError as e:
        sock.close()
        return None, None, {"kind": "error", "status": None, "err": repr(e)[:160]}
    head, _, rest = buf.partition(b"\r\n\r\n")
    status = None
    headers = {}
    for i, ln in enumerate(head.split(b"\r\n")):
        if i == 0:
            parts = ln.split(b" ")
            if len(parts) >= 2 and parts[0].startswith(b"HTTP/"):
                try:
                    status = int(parts[1])
                except ValueError:
                    status = None
            continue
        if b":" in ln:
            k, _, v = ln.partition(b":")
            headers[k.decode("latin-1").strip().lower()] = v.decode("latin-1").strip()
    if status != 101:
        try:
            sock.close()
        except OSError:
            pass
        return None, None, {"kind": "http", "status": status,
                            "err": "HTTP %s" % status, "headers": headers}
    # 读 Engine.IO OPEN 包（服务端第一帧 "0{...}"）
    open_pkt = None
    try:
        if rest:
            # 极少数情况下 body 与头一起到，交给下面按帧解析（此处简单处理：忽略）
            pass
        opcode, payload = _read_frame(sock)
        if payload:
            open_pkt = payload.decode("utf-8", "replace")[:80]
    except OSError:
        pass
    return RawWS(sock, open_pkt, headers), open_pkt, None


def save_result(name, payload):
    ensure_dirs()
    p = os.path.join(RESULTS, name + ".json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("[result] %s" % p)
    return p


def ctl_stats(timeout=3.0):
    # 必须显式指定 CTL_PORT：控制端口不是 edge，走 http_get 默认 18080 会打到 Flask（404）
    st, body, ms, _h = http_request("GET", "/stats", timeout=timeout, port=CTL_PORT)
    if st != 200:
        return None
    try:
        d = json.loads(body)
        d["_elapsed_ms"] = round(ms, 2)
        return d
    except json.JSONDecodeError:
        return None


def wait_ctl(timeout=40.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = ctl_stats(1.0)
        if d:
            return d
        time.sleep(0.15)
    raise RuntimeError("控制端口 %d 未就绪" % CTL_PORT)


def status():
    return {
        "edge_port": EDGE_PORT, "app_port": APP_PORT, "ctl_port": CTL_PORT,
        "edge_listening": port_open(EDGE_PORT), "app_listening": port_open(APP_PORT),
        "ctl_listening": port_open(CTL_PORT),
        "backend_pid": BACKEND_PID_file(), "nginx_pid": _nginx_pid(),
        "instance": INSTANCE, "instance_exists": os.path.isdir(INSTANCE),
        "nginx_conf": NGINX_CONF, "results": RESULTS,
    }


if __name__ == "__main__":
    print(json.dumps(status(), ensure_ascii=False, indent=2))
