"""
agent_api.py — 本机智能助手（DSH）控制接口：回环 HTTP + 令牌 + 线程到 GUI 的泵

为什么要有这一层
----------------
本地打印工具原来的「外部把文件放进标签页」只有两条路：人手拖放/选文件，或云端任务
（SocketIO 下发）。私人打印助手场景要由 DSH（或别的本机自动化脚本）按顾客在微信里
说好的要求，把附件填进某个标签页、改份数/单双面/页码范围/附加服务，再回报结果。
这两个动作都没有可编程入口 —— 本模块提供它。

形态（刻意选最小、最不容易出事的）
----------------------------------
* **只监听 127.0.0.1**，端口优先固定（默认 47610），被占用就退到系统分配的空闲口
  （判定用先探一次，不靠 bind 失败 —— Windows 上 SO_REUSEADDR 允许重复绑定同一个口）；
  实际端口 + 令牌写进 %APPDATA%\\HN打印工具\\agent_api.json，调用方读它即可。
* **令牌**：每次安装生成一次、32 字节随机 hex，放在同一个文件里（同用户目录，
  与 print_config.json 同信任边界）。浏览器发起的跨站请求带不上自定义头
  （X-Agent-Token 会触发预检，我们不做 CORS）⇒ 挡住网页打本机端口的常见玩法。
* **Host 校验**：只认 127.0.0.1/localhost + 实际端口，防 DNS rebinding。
* **不做常驻 QThread**：HTTP 线程只负责排队 + 等结果，真正执行在 GUI 主线程
  （QTimer 里调 pump()）。打印工具的配置/控件都不是线程安全的，任何 op 都必须落在
  主线程上 —— 这也是本模块只依赖标准库、不 import Qt 的原因（可离线单测）。

调用约定
--------
    GET  /ping                    无需令牌；返回 {"ok":true,"app":"h_n-printer",...}
    POST /api   {"op": ..., "params": {...}}
                header: X-Agent-Token: <token>
                → {"ok": true, "result": {...}}
                → {"ok": false, "error": {"code": "...", "message": "..."}}

错误码：unauthorized / bad_request / too_large / unknown_op / timeout / shutdown / internal，
业务错误码由 agent_ops 抛 AgentOpError 决定（not_found / frozen / busy / invalid / ...）。
`request()` / `ping()` 这类本地入口在连不上或响应不合法时另回 not_running / http_error / io_error。
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import queue
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import paths

logger = logging.getLogger(__name__)

SCHEMA = 1
APP_TAG = "h_n-printer"
DEFAULT_PORT = 47610
MAX_BODY = 1 << 20          # 1 MiB：op 参数里只有路径与标量，用不到更大
DEFAULT_TIMEOUT = 60.0      # GUI 侧最慢的是「加文件」（要复制暂存副本）
STATE_FILENAME = "agent_api.json"

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class AgentOpError(Exception):
    """业务错误：带稳定错误码，供调用方程序化处理（消息给人看）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ── 状态文件（令牌 + 端口 + 开关）───────────────────────────────────────────


def state_path() -> str:
    """控制接口状态文件（与 print_config.json 同目录）。"""
    return os.path.join(paths.get_app_data_dir(), STATE_FILENAME)


def _new_token() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def read_state(path: str | None = None) -> dict:
    """读状态文件；不存在/损坏返回 {}（调用方按默认值处理，绝不因此启动失败）。"""
    p = path or state_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning(f"[agent-api] 读取 {p} 失败: {e}")
        return {}


def write_state(data: dict, path: str | None = None) -> None:
    """原子写状态文件（先写 .tmp 再替换，避免调用方读到半个 JSON）。"""
    p = path or state_path()
    tmp = p + ".tmp"
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError as e:
        logger.warning(f"[agent-api] 写入 {p} 失败: {e}")


def _port_busy(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    """本机该端口上是否已经有进程在监听。

    Windows 上 SO_REUSEADDR 允许两个 socket 绑**同一个**端口（实测：先 bind 一个
    listener，第二个带 SO_REUSEADDR 的 bind 照样成功），所以「绑得上」并不等于
    「这个口归我」——必须先探一次，否则本进程会把请求发到旧进程手里（表现为令牌
    莫名其妙的 unauthorized，或者对方停了我们却还在被应答）。
    """
    if not port:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, int(port))) == 0
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def is_enabled(path: str | None = None) -> bool:
    """是否启用（默认启用；文件里明确写 false 才关）。"""
    st = read_state(path)
    return bool(st.get("enabled", True))


def set_enabled(enabled: bool, path: str | None = None) -> None:
    st = read_state(path)
    st["enabled"] = bool(enabled)
    st.setdefault("port", 0)
    st.setdefault("token", "")
    write_state(st, path)


# ── 调用对象 ────────────────────────────────────────────────────────────────


@dataclass
class Call:
    op: str
    params: dict
    event: threading.Event = field(default_factory=threading.Event)
    response: dict | None = None
    created: float = field(default_factory=time.monotonic)   # 用来丢弃「调用方已经等超时」的排队项
    abandoned: bool = False                                  # 调用方已收 504，这一条绝不能再执行


# ── 服务器 ──────────────────────────────────────────────────────────────────


class AgentApiServer:
    """回环 HTTP 控制口。`executor(op, params)` 必须在 pump() 所在线程（GUI 主线程）执行。"""

    def __init__(self, executor: Callable[[str, dict], Any], *,
                 port: int = DEFAULT_PORT,
                 state_file: str | None = None,
                 app_version: str = "",
                 call_timeout: float = DEFAULT_TIMEOUT):
        self._executor = executor
        self._state_file = state_file or state_path()
        self._preferred_port = int(port or 0)
        self._app_version = app_version
        self._call_timeout = float(call_timeout)
        self._queue: "queue.Queue[Call]" = queue.Queue()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._pumping = False
        self.port = 0
        self.token = ""
        self.started_at = 0.0
        self._lock = threading.Lock()

    # ---- 生命周期 ----

    def start(self) -> int:
        """绑定端口并起服务线程；返回实际端口。

        首选端口不可用时退到系统分配口。判定「不可用」用的是**先探一次**（见
        `_port_busy`），不能只靠 bind 是否抛 OSError —— Windows 的 SO_REUSEADDR
        会让重复绑定成功，那会把调用方悄悄送给另一个进程。
        """
        st = read_state(self._state_file)
        token = str(st.get("token") or "").strip()
        if len(token) < 32:                     # 首次运行 / 文件被清空 → 生成新令牌
            token = _new_token()

        candidates: list[int] = []
        if self._preferred_port:
            if _port_busy(self._preferred_port):
                logger.info(f"[agent-api] 端口 {self._preferred_port} 已被占用，改用系统分配口")
            else:
                candidates.append(self._preferred_port)
        candidates.append(0)

        last_err: Exception | None = None
        httpd = None
        for port in candidates:
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler())
                break
            except OSError as e:                # 端口被占（EADDRINUSE）等
                last_err = e
                httpd = None
        if httpd is None:
            raise OSError(f"无法绑定回环端口: {last_err}")

        httpd.daemon_threads = True
        httpd.agent = self                   # 处理器通过 server.agent 取令牌/队列
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self.token = token
        self.started_at = time.time()

        self._thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2},
                                        name="agent-api-http", daemon=True)
        self._thread.start()

        write_state({
            "enabled": bool(st.get("enabled", True)),
            "schema": SCHEMA,
            "host": "127.0.0.1",
            "port": self.port,
            "token": self.token,
            "pid": os.getpid(),
            "app_version": self._app_version,
            "app": APP_TAG,
            "started_at": int(self.started_at),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, self._state_file)
        logger.info(f"[agent-api] 已启动 http://127.0.0.1:{self.port}（v{self._app_version}）")
        if self._preferred_port and self.port != self._preferred_port:
            logger.info(f"[agent-api] 首选端口 {self._preferred_port} 不可用，实际用 {self.port}")
        return self.port

    def stop(self) -> None:
        """停止服务；未完成的调用立即收到 shutdown 错误（不静默丢掉调用方）。"""
        with self._lock:
            httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception as e:              # 关停阶段的异常不该影响退出流程
                logger.debug(f"[agent-api] shutdown 异常: {e}")
            try:
                httpd.server_close()
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        while True:                             # 把还排着的调用一律回错误，别让 HTTP 线程干等
            try:
                call = self._queue.get_nowait()
            except queue.Empty:
                break
            call.response = {"ok": False, "error": {"code": "shutdown",
                                                   "message": "控制接口已停止"}}
            call.event.set()
        logger.info("[agent-api] 已停止")

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}" if self.port else ""

    # ---- GUI 主线程侧 ----

    def pump(self, budget: int = 32) -> int:
        """在 GUI 主线程执行排队中的调用。返回处理条数。

        刻意带重入守卫：主线程一旦弹出模态框就会跑嵌套事件循环，QTimer 会在里面再次
        触发 —— 没有守卫就会在「加文件弹了警告框」的当口再执行一遍 op。
        """
        if self._pumping:
            return 0
        self._pumping = True
        done = 0
        try:
            while done < budget:
                try:
                    call = self._queue.get_nowait()
                except queue.Empty:
                    break
                # 调用方早就等超时了（HTTP 已回 504）⇒ 绝不能再执行：否则「加文件」这类
                # 有副作用的 op 会在人重试之后迟到执行一次，变成重复加两份。
                if call.abandoned or (time.monotonic() - call.created) > self._call_timeout:
                    logger.debug(f"[agent-api] 丢弃过期调用 op={call.op}")
                    continue
                call.response = self._invoke(call.op, call.params)
                call.event.set()
                done += 1
        finally:
            self._pumping = False
        return done

    def _invoke(self, op: str, params: dict) -> dict:
        try:
            result = self._executor(op, params)
            return {"ok": True, "result": result}
        except AgentOpError as e:
            return {"ok": False, "error": {"code": e.code, "message": e.message}}
        except Exception as e:                  # 任何没料到的异常都要变成结构化错误
            logger.exception(f"[agent-api] op={op} 执行异常: {e}")
            return {"ok": False, "error": {"code": "internal",
                                          "message": f"{type(e).__name__}: {e}"}}

    # ---- HTTP 线程侧 ----

    def submit(self, op: str, params: dict) -> dict:
        """排一次调用并等结果（HTTP 线程调用）。超时返回 timeout 错误。"""
        call = Call(op=op, params=params)
        self._queue.put(call)
        if not call.event.wait(self._call_timeout):
            with self._lock:
                call.abandoned = True      # 标记作废：pump 就算碰到也必须跳过
            return {"ok": False, "error": {"code": "timeout",
                                          "message": f"主程序 {self._call_timeout:.0f}s 内未响应"
                                                     f"（可能被模态对话框挡住或正在打印）"}}
        return call.response or {"ok": False, "error": {"code": "internal",
                                                       "message": "无响应内容"}}

    def pending(self) -> int:
        return self._queue.qsize()


# ── HTTP 处理器 ─────────────────────────────────────────────────────────────


def _make_handler():
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "HNPrintAgentApi/" + str(SCHEMA)

        # 默认实现把每个请求都写 stderr，太吵；统一进 logger
        def log_message(self, fmt, *args):
            logger.debug("[agent-api] " + fmt % args)

        # ---- 公共 ----

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _err(self, status: int, code: str, message: str) -> None:
            self._send(status, {"ok": False, "error": {"code": code, "message": message}})

        def _host_ok(self) -> bool:
            """防 DNS rebinding：Host 必须是回环地址 + 本服务实际端口。"""
            agent = getattr(self.server, "agent", None)
            if agent is None:
                return False
            host = (self.headers.get("Host") or "").strip().lower()
            if not host:
                return False
            name, _, port = host.rpartition(":")
            if not name:                        # 没有端口：只接受裸回环名
                name, port = host, str(agent.port)
            name = name.strip("[]")
            return name in LOOPBACK_HOSTS and port == str(agent.port)

        def _loopback_peer(self) -> bool:
            try:
                return self.client_address[0] in ("127.0.0.1", "::1")
            except Exception:
                return False

        # ---- 路由 ----

        def do_GET(self):
            if not self._loopback_peer() or not self._host_ok():
                self._err(403, "forbidden", "只接受本机回环访问")
                return
            agent = self.server.agent
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path in ("/ping", "/api/ping"):
                self._send(200, {"ok": True, "app": APP_TAG, "schema": SCHEMA,
                                 "version": agent._app_version, "pid": os.getpid(),
                                 "port": agent.port})
                return
            self._err(404, "not_found", f"未知路径: {path}")

        def do_POST(self):
            if not self._loopback_peer() or not self._host_ok():
                self._err(403, "forbidden", "只接受本机回环访问")
                return
            agent = self.server.agent
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path not in ("/api", "/api/call", "/api/rpc"):
                self._err(404, "not_found", f"未知路径: {path}")
                return
            # 令牌：常量时间比较，顺带挡住「没配令牌却放行」的分支
            token = self.headers.get("X-Agent-Token") or ""
            if not agent.token or not hmac.compare_digest(token.strip(), agent.token):
                self._err(401, "unauthorized", "缺少或错误的 X-Agent-Token")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._err(400, "bad_request", "Content-Length 非法")
                return
            if length <= 0:
                self._err(400, "bad_request", "请求体为空")
                return
            if length > MAX_BODY:
                self._err(413, "too_large", f"请求体超过 {MAX_BODY} 字节")
                return
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as e:
                self._err(400, "bad_request", f"JSON 解析失败: {e}")
                return
            if not isinstance(data, dict):
                self._err(400, "bad_request", "请求体必须是 JSON 对象")
                return
            op = data.get("op")
            params = data.get("params") or {}
            if not isinstance(op, str) or not op.strip():
                self._err(400, "bad_request", "缺少 op")
                return
            if not isinstance(params, dict):
                self._err(400, "bad_request", "params 必须是 JSON 对象")
                return
            resp = agent.submit(op.strip(), params)
            status = 200 if resp.get("ok") else 400
            if not resp.get("ok"):
                code = (resp.get("error") or {}).get("code")
                if code == "timeout":
                    status = 504
                elif code == "too_large":
                    status = 413
                elif code == "internal":
                    status = 500
                elif code == "shutdown":
                    status = 503        # 程序正在退出，让调用方等它起来
                elif code in ("not_found", "unknown_op"):
                    status = 404
                elif code in ("approval_denied", "wrong_password"):
                    status = 403        # 人没批准 / 财务密码不对：不是状态问题，是权限问题
                elif code in ("frozen", "busy", "cloud_locked",
                              "confirm_required", "no_member", "pages_pending",
                              "print_blocked", "needs_setup", "finance_error",
                              "finance_timeout"):
                    status = 409        # 状态冲突：请求本身没错，是「现在不能做」
            self._send(status, resp)

    return Handler


# ── 便捷入口（给 tools/agent_cli.py 与测试共用）────────────────────────────


def request(op: str, params: dict | None = None, *, timeout: float = 60.0,
            state_file: str | None = None) -> dict:
    """按状态文件里的端口/令牌调一次接口。返回 {"ok":..., "result"/"error":...}。

    依赖标准库 urllib（不引第三方）；连接失败也返回结构化错误，方便 CLI 直接打印。
    """
    import urllib.error
    import urllib.request

    st = read_state(state_file)
    port = int(st.get("port") or 0)
    token = str(st.get("token") or "")
    if not port or not token:
        return _local_error("not_running", "控制接口未启动（找不到 agent_api.json 里的端口/令牌）")
    body = json.dumps({"op": op, "params": params or {}}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-Agent-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return _local_error("http_error", f"HTTP {e.code}")
    except urllib.error.URLError as e:
        return _local_error("not_running", f"连不上 127.0.0.1:{port}（程序没开或接口已关）: {e.reason}")
    except (TimeoutError, socket.timeout):
        return _local_error("timeout", "请求超时")
    except OSError as e:
        return _local_error("io_error", f"网络错误: {e}")


def ping(timeout: float = 3.0, state_file: str | None = None) -> dict:
    """探活（不需要令牌）。

    端口只认状态文件里记账的那个：文件里没有端口 = 程序没起过/已清理，一律
    `not_running`。**不回退到默认端口** —— 那会把「另一个实例在监听 47610」
    误报成「本机程序在跑」（本文件 `_port_busy` 注释里的那个坑）。
    """
    import urllib.error
    import urllib.request

    st = read_state(state_file)
    port = int(st.get("port") or 0)
    if not port:
        return _local_error("not_running", "控制接口未启动（状态文件里没有端口）")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        return _local_error("not_running", f"连不上 127.0.0.1:{port}: {getattr(e, 'reason', e)}")
    except (TimeoutError, socket.timeout):
        return _local_error("timeout", "探活超时")
    except OSError as e:
        return _local_error("io_error", f"网络错误: {e}")


def _local_error(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}
