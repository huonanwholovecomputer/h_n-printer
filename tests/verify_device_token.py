# -*- coding: utf-8 -*-
"""验收 · S1-01 per-device token（scope 矩阵 / 吊销 / 迁移窗口 / client_id 绑定 + 客户端半）

依据：docs/安全加固修复计划_2026-10-23.md §S1-01（DS-02 根治、R7）。

现场形状：全站只有 config.py 里**一个** TOKEN —— 泄露一次要全站轮换、无法单独吊销某台设备、
也无法区分"这台设备能做什么"。多台设备共用一把钥匙 = 任意一台被物理接触就等于全站管理员。
另外 token 一直拼在 URL query 里（会落进 nginx/代理/异常日志）。

本脚本：
  A. 静态接线：devices.json 里的 token_hash/scopes/issued_at/revoked_at、统一认证入口
     `_authenticate_printer()`、所有 printer 路由都走它、admin 路由有 scope 装饰器、
     签发/吊销接口、只存哈希（明文只返回一次）。
  B. 行为：全局 TOKEN 迁移窗口可用；签发本机 token；print/admin scope 矩阵；
     吊销后立即 403；per-device token 只能以自己的 client_id 连接（防冒充）；
     握手门同样认 per-device token 与 X-Printer-Token **请求头**、吊销后 403。
  C. 变异自证：把 scope 校验改成永真 → print-only token 能调 admin 接口（判据转红）。
  D–F. **客户端半**（2026-10-24）：
       D. 静态：token 全部走 X-Printer-Token（源码里无 `params={"token"` / `?token=`）、
          Socket.IO 用带默认头的 session + websocket_extra_options、启动自签、
          `cloud_device_token` 配置字段、GUI 重新签发按钮与失效清除接线；
       E. 行为（真实 CloudClient + 本机假 HTTP 服务）：HTTP 带请求头且 query 无 token、
          自签成功切换 token 且只签一次、已有本机 token 时优先使用、配置字段往返；
       F. 变异自证：把 `auth_headers()` 改成空 dict → 请求不再带 token（判据能红）。

用法：python tests/verify_device_token.py     （exit 0 = 全部通过）
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

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


# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
H.chk("devices.json 记录 token_hash / scopes / 签发与吊销时间",
      '"token_hash": _hash_printer_token(token)' in code
      and '"scopes": scopes' in code and '"token_issued_at": now_str' in code
      and '"revoked_at": ""' in code)
H.chk("只存哈希：签发函数里没有把明文写进 devices.json",
      "token_hash" in block("def issue_device_token(")
      and '"token": token' not in block("def issue_device_token("))
H.chk("统一认证入口 _authenticate_printer()（per-device 优先，全局 TOKEN 兜底）",
      "def _authenticate_printer(" in code
      and "_find_device_by_token(tok)" in block("def _authenticate_printer(")
      and "_printer_token_ok(tok)" in block("def _authenticate_printer("))
H.chk("已吊销的 token 直接判无效",
      "if rec.get(\"revoked_at\"):" in block("def _find_device_by_token("))
n_auth = code.count("_authenticate_printer(token)")
H.chk("★ 所有 printer 路由都改走统一认证（≥25 处）", n_auth >= 25, "实际 %d" % n_auth)
H.chk("_printer_token_ok 只作为\"全局 TOKEN 兼容\"留在 _authenticate_printer 内部",
      code.count("_printer_token_ok(") <= 4, code.count("_printer_token_ok("))
H.chk("scope 装饰器 require_printer_scope 存在",
      "def require_printer_scope(" in code and "@require_printer_scope(\"admin\")" in code)
H.chk("管理类路由都挂了 admin scope",
      sum(code.count('@require_printer_scope("admin")') for _ in [0]) >= 10,
      code.count('@require_printer_scope("admin")'))
H.chk("签发/吊销接口存在",
      '@app.route("/api/printer/token/create"' in code
      and '@app.route("/api/printer/token/revoke"' in code)
H.chk("per-device token 只能以自己的 client_id 连接（防冒充）",
      "_auth[\"client_id\"] != client_id" in code)

# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（临时后端副本）===")
TMP, DST = H.make_copy("hn_devtok_")
backend = H.import_backend(DST)
backend.init_db()
GLOBAL_TOK = "global-migration-token"
backend.PRINTER_TOKEN = GLOBAL_TOK
http = backend.app.test_client()
G = {"X-Printer-Token": GLOBAL_TOK}


def H_(tok):
    return {"X-Printer-Token": tok}


H.chk("迁移窗口：全局 TOKEN 仍可调 print 接口",
      http.get("/api/printer/devices", headers=G).status_code == 200)

r = http.post("/api/printer/token/create", json={"client_id": "dev-store-A", "label": "门店A"},
              headers=G)
tok_a = (r.get_json() or {}).get("token")
H.chk("用全局 TOKEN 为本机签发 token", r.status_code == 200 and bool(tok_a))
H.chk("默认 scopes = print + admin（门店机要拉单也要看统计）",
      (r.get_json() or {}).get("scopes") == ["print", "admin"], r.get_json())
H.chk("devices.json 里只有哈希、没有明文",
      tok_a not in open(os.path.join(DST, "devices.json"), encoding="utf-8").read())

H.chk("本机 token 调 print 接口 200",
      http.get("/api/printer/devices", headers=H_(tok_a)).status_code == 200)
H.chk("本机 token 调 admin 接口 200",
      http.get("/api/admin/statistics/months", headers=H_(tok_a)).status_code == 200)

r = http.post("/api/printer/token/create",
              json={"client_id": "dev-store-P", "scopes": ["print"]}, headers=H_(tok_a))
tok_p = (r.get_json() or {}).get("token")
H.chk("签发 print-only token", r.status_code == 200 and bool(tok_p))
H.chk("★ print-only 能调 print 接口",
      http.get("/api/printer/devices", headers=H_(tok_p)).status_code == 200)
H.chk("★ print-only 调 admin 接口 → 403（scope 生效）",
      http.get("/api/admin/statistics/months", headers=H_(tok_p)).status_code == 403)
H.chk("print-only 调订单状态订正 → 403",
      http.post("/api/admin/order_status", json={"order_id": 1, "status": "sent"},
                headers=H_(tok_p)).status_code == 403)

H.chk("未知 token → 403",
      http.get("/api/printer/devices", headers=H_("nope")).status_code == 403)

# client_id 绑定
c = backend.socketio.test_client(backend.app,
                                 query_string="?token=%s&client_id=dev-store-A" % tok_a)
H.chk("本机 token 以自己的 client_id 连接成功", c.is_connected())
c.disconnect()
c2 = backend.socketio.test_client(backend.app,
                                  query_string="?token=%s&client_id=dev-OTHER" % tok_a)
H.chk("★ 用 A 的 token 冒充 B 连接 → 被拒", not c2.is_connected())
try:
    c2.disconnect()
except Exception:
    pass

# 吊销
r = http.post("/api/printer/token/revoke", json={"client_id": "dev-store-A"}, headers=G)
H.chk("吊销 dev-store-A → 200", r.status_code == 200, r.get_json())
H.chk("★ 吊销后该 token 立即 403（其它设备不受影响）",
      http.get("/api/printer/devices", headers=H_(tok_a)).status_code == 403
      and http.get("/api/printer/devices", headers=H_(tok_p)).status_code == 200)
H.chk("吊销不影响全局 TOKEN（紧急通道）",
      http.get("/api/printer/devices", headers=G).status_code == 200)
H.chk("吊销后握手门也 403",
      http.get("/socket.io/?EIO=4&transport=polling&token=" + tok_a).status_code == 403)
H.chk("有效 per-device token 握手 200",
      http.get("/socket.io/?EIO=4&transport=polling&token=" + tok_p).status_code == 200)
# S1-01 客户端半：token 走请求头（服务端必须同等接受，否则客户端一升级就连不上）
H.chk("★ 握手门接受 X-Printer-Token 请求头（客户端不再需要把 token 拼进 URL）",
      http.get("/socket.io/?EIO=4&transport=polling", headers=H_(tok_p)).status_code == 200)
H.chk("无 token 的匿名握手 → 403（门没被绕开）",
      http.get("/socket.io/?EIO=4&transport=polling").status_code == 403)
_c3 = backend.socketio.test_client(backend.app,
                                   query_string="?client_id=dev-store-P", headers=H_(tok_p))
H.chk("on_connect 认请求头 token（无 query token 也能建连并绑定 client_id）",
      _c3.is_connected())
try:
    _c3.disconnect()
except Exception:
    pass
H.chk("重复吊销 → 404（如实报告没有可吊销的 token）",
      http.post("/api/printer/token/revoke", json={"client_id": "dev-store-A"},
                headers=G).status_code == 404)

# ============================================================
# C. 变异自证：scope 校验永真
# ============================================================
print("\n=== C. 变异自证（scope 校验改成永真）===")
mut_tmp, mut_dst = H.mutated_copy([
    ("def _printer_scope_ok(tok, scope):\n    rec = _authenticate_printer(tok)\n"
     "    return bool(rec and scope in rec[\"scopes\"])",
     "def _printer_scope_ok(tok, scope):\n    rec = _authenticate_printer(tok)\n"
     "    return bool(rec)"),
], prefix="hn_devtok_mut_")
proc = H.run_probe(mut_dst, """
backend.PRINTER_TOKEN = "g"
r = http.post("/api/printer/token/create", json={"client_id": "p1", "scopes": ["print"]},
              headers={"X-Printer-Token": "g"})
tok = (r.get_json() or {}).get("token")
print("RESULT=%s" % http.get("/api/admin/statistics/months",
                             headers={"X-Printer-Token": tok}).status_code)
""")
H.chk("scope 校验永真后 print-only 也能调 admin 接口（判据能红）",
      H.probe_value(proc) == "200", H.probe_value(proc))

# ============================================================
# D. 客户端静态接线（token 走请求头 + 启动自签 + 配置字段）
# ============================================================
print("\n=== D. 客户端静态接线（header 化 / 自签 / 配置 / GUI）===")


def _read_local(name):
    with open(os.path.join(LOCAL_TOOL, name), "r", encoding="utf-8", newline="") as f:
        return f.read()


cc_src = _read_local("cloud_client.py")
gui_src = _read_local("gui.py")
os_src = _read_local("offline_sync.py")
pc_src = _read_local("printer_config.py")

H.chk("★ 客户端 token 一律走请求头（源码里不再有 params={\"token\" / URL 里的 ?token=）",
      'params={"token"' not in cc_src and 'params={"token"' not in gui_src
      and 'params={"token"' not in os_src
      and "ws_url}?token=" not in cc_src
      and "?token={self.token}" not in cc_src)
H.chk("auth_headers() 是唯一入口，CloudClient._auth_headers 复用它",
      "def auth_headers(token: str) -> dict:" in cc_src
      and "return auth_headers(self.auth_token)" in cc_src)
H.chk("★ Socket.IO 握手带请求头（polling 走 session 默认头 + WS 升级走 websocket_extra_options）",
      "http_session=self._make_sio_session()" in cc_src
      and 'websocket_extra_options={"header": self._auth_headers()}' in cc_src
      and 'sess.headers.update(hdrs)' in cc_src)
H.chk("connect_url 只带 client_id / device_name（不含 token）",
      'connect_url = (f"{self.ws_url}?client_id={self.client_id}"' in cc_src)
H.chk("★ 启动自签本机 token（连上之前调用；已有/被拒则跳过）",
      "def request_device_token(" in cc_src and "def _ensure_device_token(" in cc_src
      and "self._ensure_device_token()" in cc_src
      and "self.device_token = new_tok" in cc_src)
H.chk("本机 token 失效 → 回退全局 token 并通知 GUI 清除",
      "self.device_token = \"\"" in cc_src and "self.token_invalid.emit()" in cc_src)
H.chk("GUI 接线：token_issued / token_invalid + 重新签发按钮 + 换全局 token 清本机 token",
      "token_issued.connect(self._on_cloud_token_issued)" in gui_src
      and "token_invalid.connect(self._on_cloud_token_invalid)" in gui_src
      and "重新签发本机 token" in gui_src
      and "if self._config.cloud_token != old_token:" in gui_src
      and "self._config.cloud_device_token = \"\"" in gui_src)
H.chk("配置字段 cloud_device_token 可往返（to_dict / from_dict）",
      "cloud_device_token: str = \"\"" in pc_src
      and '"cloud_device_token": self.cloud_device_token' in pc_src
      and 'cloud_device_token=data.get("cloud_device_token", "")' in pc_src)
H.chk("离线补报也用请求头（offline_sync 无 query token）",
      "headers=_auth_headers(token)" in os_src)

# ============================================================
# E. 客户端行为（真实 CloudClient + 本机假 HTTP 服务）
# ============================================================
print("\n=== E. 客户端行为（真实 CloudClient + 假 HTTP 服务）===")
import http.server  # noqa: E402

_CLIENT_SANDBOX = tempfile.mkdtemp(prefix="hn_devtok_client_")
os.environ["APPDATA"] = _CLIENT_SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = os.environ["NO_PROXY"]


class _MockBackend(http.server.BaseHTTPRequestHandler):
    records: list = []
    reply: dict = {}

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            try:
                self.rfile.read(length)
            except Exception:
                pass
        _MockBackend.records.append({
            "method": self.command,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
        })
        payload = json.dumps(_MockBackend.reply or {}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def log_message(self, *args):
        pass


_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockBackend)
threading.Thread(target=_srv.serve_forever, daemon=True).start()
_API = "http://127.0.0.1:%d" % _srv.server_address[1]

import cloud_client as cc  # noqa: E402
import printer_config as pc  # noqa: E402

H.chk("auth_headers() 基线 = X-Printer-Token 头",
      cc.auth_headers("tok") == {"X-Printer-Token": "tok"}
      and cc.auth_headers("") == {})

_MockBackend.reply = {"success": True, "orders": [], "count": 0, "take_orders": True}
client = cc.CloudClient(api_url=_API, ws_url="ws://127.0.0.1:9",
                        token="global-tok", client_id="dev-client-E")
client.pull_pending()
_rec = _MockBackend.records[-1]
H.chk("★ 真实 HTTP 拉单请求带 X-Printer-Token，URL query 里没有 token",
      _rec["headers"].get("x-printer-token") == "global-tok"
      and "token=" not in _rec["path"],
      _rec)
H.chk("auth_token 在无本机 token 时回退全局 token", client.auth_token == "global-tok")

# 自签：换成本机 token，且只签一次
_MockBackend.reply = {"success": True, "token": "dev-tok-E", "scopes": ["print", "admin"]}
issued = []
client2 = cc.CloudClient(api_url=_API, ws_url="ws://127.0.0.1:9",
                         token="global-tok", client_id="dev-client-F")
client2.token_issued.connect(lambda t, s: issued.append((t, s)))
client2._ensure_device_token()
H.chk("★ 启动自签成功：device_token 切换 + token_issued 信号带 scopes",
      client2.device_token == "dev-tok-E" and issued == [("dev-tok-E", ["print", "admin"])],
      (client2.device_token, issued))
_n_issue = sum(1 for r in _MockBackend.records if "/api/printer/token/create" in r["path"])
client2._ensure_device_token()
H.chk("自签只做一次（第二次 _ensure_device_token 不再请求签发，避免每次启动轮换）",
      sum(1 for r in _MockBackend.records if "/api/printer/token/create" in r["path"]) == _n_issue,
      _n_issue)
_MockBackend.reply = {"success": True, "devices": []}
client2.get_printer_devices()
_rec2 = _MockBackend.records[-1]
H.chk("★ 自签后的请求用本机 token（不是全局 token）",
      _rec2["headers"].get("x-printer-token") == "dev-tok-E", _rec2)

H.chk("已有本机 token 时 auth_token 优先用它",
      cc.CloudClient(api_url=_API, token="global-tok", device_token="dev-tok-X",
                     client_id="dev-client-G").auth_token == "dev-tok-X")

_cfg = pc.PrinterConfig.from_dict({"cloud_device_token": "dt-1"})
H.chk("PrinterConfig 往返保留 cloud_device_token；旧配置缺字段 → 空串",
      _cfg.cloud_device_token == "dt-1"
      and _cfg.to_dict().get("cloud_device_token") == "dt-1"
      and pc.PrinterConfig.from_dict({}).cloud_device_token == "")

_srv.shutdown()

# ============================================================
# E2. 真实 Socket.IO 握手（与 CloudClient 相同的构造方式）
# ============================================================
print("\n=== E2. 真实 Socket.IO 握手（session 默认头 + websocket_extra_options）===")
from flask import Flask, request as _flask_request  # noqa: E402
from flask_socketio import SocketIO as _FSIO  # noqa: E402
import socketio as _socketio  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

_ws_app = Flask("hn_devtok_ws")
_ws_sio = _FSIO(_ws_app, async_mode="threading", cors_allowed_origins="*")


@_ws_sio.on("connect")
def _ws_connect():
    # 只认请求头（模拟 S1-01 之后的客户端；query token 也接受，但本用例不带）
    return (_flask_request.headers.get("X-Printer-Token")
            or _flask_request.args.get("token")) == "good"


_ws_inner = _ws_app.wsgi_app


def _ws_gate(environ, start_response):
    """与后端 `_sio_handshake_gate` 同口径：无 sid 的握手必须带有效 token（头或 query）。"""
    if environ.get("PATH_INFO", "").startswith("/socket.io/"):
        qs = environ.get("QUERY_STRING") or ""
        params = dict(urllib.parse.parse_qsl(qs, keep_blank_values=True))
        if "sid" not in params:
            tok = environ.get("HTTP_X_PRINTER_TOKEN") or params.get("token")
            if tok != "good":
                body = b"Forbidden"
                start_response("403 Forbidden", [("Content-Type", "text/plain"),
                                                 ("Content-Length", str(len(body)))])
                return [body]
    return _ws_inner(environ, start_response)


_ws_app.wsgi_app = _ws_gate
_ws_srv = make_server("127.0.0.1", 0, _ws_app, threaded=True)
threading.Thread(target=_ws_srv.serve_forever, daemon=True).start()
_ws_url = "http://127.0.0.1:%d" % _ws_srv.server_port

_anon = _socketio.Client(reconnection=False)
try:
    _anon.connect(_ws_url, wait_timeout=5)
except Exception:
    pass
H.chk("无 token 的真实 Socket.IO 握手被拒（门生效）", not _anon.connected)
try:
    _anon.disconnect()
except Exception:
    pass

_ws_sess = cc.http_requests.Session()
_ws_sess.headers.update({k: v for k, v in cc.auth_headers("good").items()})
_ws_client = _socketio.Client(
    reconnection=False, http_session=_ws_sess,
    websocket_extra_options={"header": cc.auth_headers("good")})
_ws_ok = False
try:
    _ws_client.connect(_ws_url + "?client_id=dev-ws", wait_timeout=10)
    _ws_ok = bool(_ws_client.connected)
finally:
    try:
        _ws_client.disconnect()
    except Exception:
        pass
    _ws_srv.shutdown()
H.chk("★ 与客户端相同的构造方式（session 默认头 + websocket 头）能真实建连", _ws_ok)

# ============================================================
# F. 客户端变异自证：auth_headers 变空 dict
# ============================================================
print("\n=== F. 客户端变异自证（auth_headers 返回空 → 请求不再带 token）===")
_mut_dir = tempfile.mkdtemp(prefix="hn_devtok_mut_")
with open(os.path.join(LOCAL_TOOL, "cloud_client.py"), "r", encoding="utf-8") as f:
    _cc_src = f.read()
_anchor = 'return {"X-Printer-Token": token} if token else {}'
if _cc_src.count(_anchor) != 1:
    raise AssertionError("变异锚点未命中: %r" % _anchor)
with open(os.path.join(_mut_dir, "cloud_client.py"), "w", encoding="utf-8") as f:
    f.write(_cc_src.replace(_anchor, "return {}  # 变异：不再带请求头", 1))

_probe_code = (
    "import io, os, sys\n"
    "sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')\n"
    "sys.path.insert(0, %r)\n"
    "sys.path.insert(0, %r)\n"
    "import cloud_client as cc\n"
    "print('RESULT=%%r' %% cc.auth_headers('tok'))\n" % (LOCAL_TOOL, _mut_dir)
)
_env = dict(os.environ)
_env["PYTHONPATH"] = ""
_env["APPDATA"] = _CLIENT_SANDBOX
_proc = subprocess.run([sys.executable, "-c", _probe_code], env=_env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
_mut_val = H.probe_value(_proc)
H.chk("auth_headers 改成空 dict 后请求不再带 token（E 的判据能红）",
      _mut_val == "{}", "probe=%r stderr=%r" % (_mut_val, (_proc.stderr or "")[-200:]))

for _d in (_CLIENT_SANDBOX, _mut_dir):
    shutil.rmtree(_d, ignore_errors=True)

H.finish()
