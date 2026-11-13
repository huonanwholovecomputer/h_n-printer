# ============================================================================
# S3-01（2026-10-24）：**可选运行时** gevent —— 一条实时连接不再独占 worker
#
# 背景（S2-12 压测实测）：threading 模式下每条 WebSocket 连接占住一个 gthread worker，
# 生产 `threads=16` ⇒ 15 条设备连接就把 HTTP 全部堵死（3 台约需 8、6 台约需 15、10 台需 25，
# 按 §11.2 required_threads=ceil(设备×2.5)+4）。gevent 下一条连接 = 一个 greenlet，
# 不再占 OS 线程，因此 16/24/32 条连接时 HTTP 仍然可用（`tests/staging/09_gevent_runtime.py`）。
#
# 开关：环境变量 `HN_RUNTIME=gevent`（**同时**决定 gunicorn worker_class，见 gunicorn_config.py）。
#   · 不设 / 其它值 = 现状（async_mode="threading" + gthread worker），行为与性能零变化；
#   · 设了 = 下面先 monkey-patch（必须在**任何网络/线程模块导入之前**，这是 gevent 的硬要求），
#     再让 SocketIO 用 async_mode="gevent"；回滚 = 去掉该环境变量并重启。
#
# ⚠ 已知取舍（验证数据见 09 实验与 docs/容量压测报告）：
#   · **SQLite 不受 monkey-patch 影响**（C 扩展）：一次 `BEGIN IMMEDIATE` 等写锁会阻塞整个
#     greenlet 事件循环（线程模型下只阻塞一个线程）。所以必须保持写事务短（S2-02 已做）；
#     `tests/staging/09` 里专门量了「32 条连接 + 并发写」时 HTTP p95。
#   · `workers` 仍必须为 1（printer_clients/pushed_tasks/限速桶/sid 映射都是进程内内存）；
#     workers>1 需要把这几样迁到 Redis —— 那是 S3-01 的**后半段**，本次不做。
# ============================================================================
import os

_RUNTIME = (os.environ.get("HN_RUNTIME", "") or "").strip().lower()
_GEVENT = _RUNTIME == "gevent"
if _GEVENT:                                                    # pragma: no cover
    try:
        from gevent import monkey as _gevent_monkey
        _gevent_monkey.patch_all()
    except ImportError as _e:                                  # 明确失败，不静默降级
        raise RuntimeError(
            "HN_RUNTIME=gevent 但未安装 gevent：pip install -r requirements.txt") from _e

import copy
import glob
import hashlib
import hmac
import io
import json
import logging
import shutil
import socket
import sqlite3
import string
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import math
import re
import urllib.parse
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta
from functools import wraps
from urllib import request as urlrequest, parse as urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, g, request, jsonify, send_file
from flask_socketio import SocketIO, emit, disconnect, join_room
from flask_cors import CORS
import requests

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

# S3-04（DS-24）：文件访问审计（谁、读了哪个文件、何时）。
# 独立模块：不 import Flask、不依赖 app 的任何全局量；坏了也只影响"记不记"，不影响主流程。
import file_audit

try:
    from PIL import Image
    HAS_PIL = True
    # 防解压炸弹（P0-2）：单张图片像素数上限 5000 万，超限抛 DecompressionBombError
    Image.MAX_IMAGE_PIXELS = 50_000_000
except ImportError:
    HAS_PIL = False

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 最大上传 50MB
# HTTP CORS：桌面浏览器预览（file:// 或 localhost）与 Capacitor WebView（https://localhost）
# 均需跨域访问 /api/*。认证走 Bearer token（非 Cookie），放行任意 Origin 无越权风险，
# 与下方 SocketIO 的 cors_allowed_origins="*" 保持一致。只配此一层即可，nginx 不要再加
# Access-Control-* 头，避免出现重复头。
# max_age=86400：浏览器缓存 OPTIONS 预检 1 天，避免每次刷新都重发预检导致 nginx 限流 503
#
# S2-04（DS-13）：从 "*" 收窄到**白名单**。鉴权走 Bearer 不是 Cookie，所以"任意 Origin"
# 不算凭据泄露，但它意味着任何网页都能以访客浏览器身份打接口、读状态（含设备列表）。
# 说明：
#   · 小程序原生 wx.request 不带 Origin ⇒ 不受 CORS 影响；
#   · Android Capacitor WebView 的 Origin 是 https://localhost（新版本）/ capacitor://localhost；
#   · 本地开发端口（127.0.0.1 / localhost 任意端口）用正则放行；
#   · SocketIO 那一层刻意**不收窄**：本地打印工具的 python-socketio 不发 Origin，
#     而实时通道本身已经有 S0-02 的握手 token 门 + 事件归属校验，收窄收益小、掉线风险大。
CORS_ALLOWED_ORIGINS = [
    "https://hn-space.cn",
    "https://www.hn-space.cn",
    "https://localhost",
    "capacitor://localhost",
    r"http://localhost(:\d+)?",
    r"http://127\.0\.0\.1(:\d+)?",
]
CORS(app, resources={r"/api/*": {"origins": CORS_ALLOWED_ORIGINS}}, max_age=86400)
# S1-07（R7）：`async_handlers=False` —— 事件在处理线程内**同步**执行。本仓 handler
# 全是同步 SQL，异步化只会把"一个请求占线程"搬成"占住该设备的事件线程"，症状更隐蔽
# （计划 §1.4 依赖 2：先做 collect_all 的异步化，再改这里 —— 见 log_collect_all）。
# engineio 参数同时收紧：心跳 15s / 超时 10s（更快发现死连接、释放线程），
# 单帧缓冲 256 KB（默认 1 MB；事件载荷正常远小于它）。
# S3-01：async_mode 跟随运行时开关（HN_RUNTIME=gevent ⇒ "gevent"，否则维持 "threading"）。
# **不能混用**：gevent 的 WSGI 服务器 + async_mode="threading" 会重演 2026-09 那次
# eventlet/gthread 混用的"连接建立与数据面脱节"（见 gunicorn_config.py 末尾的现场证据）。
_ASYNC_MODE = "gevent" if _GEVENT else "threading"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=_ASYNC_MODE,
                    async_handlers=False, ping_interval=15, ping_timeout=10,
                    max_http_buffer_size=262144)

# ===== S0-02：Engine.IO 握手前置 token 门 =====
# 背景（安全加固计划 S0-02 / DDoS R1）：token 校验原先只在 Socket.IO 的 "40 CONNECT" 里做，
# 攻击者只要完成 Engine.IO 握手（不发 40）就能占住一个 gthread —— 16 条连接即打满全站。
# 这里把校验前移到**首次握手**（无 sid 的 /socket.io/ 请求）：无 token 直接 403，一个线程都不占。
# 只拦无 sid 的握手、放行带 sid 的长轮询后续请求：第三方自研设备/旧客户端可能只在首次握手带
# token，若后续 poll 被 403 会表现为"连上又掉"；而伪造 sid 会被 engineio 快速 400，不构成线程耗尽。
# ⚠ 赋值次序不可调换（计划附录 A-4）：必须在 SocketIO() 之后赋值，否则会被 Flask-SocketIO
# 当成"内层 WSGI app"捕获，engineio 处理 /socket.io/ 时根本不调用它（门看着装上了、一条都拦不住）；
# 且 ProxyFix 必须在 gate 外层，否则真实 IP 永远是 nginx 的 127.0.0.1。
_SIO_GATE_STATS = {"rejected": 0, "accepted": 0}
_SIO_GATE_MAX_QUERY = 8192

# S1-07：sid → client_id 映射（on_ping O(1) 用；connect 建、disconnect 删）
_sid_to_client = {}

# ===== S1-07：每连接事件令牌桶 =====
# 一条连接发 1000 事件/秒时，原实现会逐个进 handler（同步 SQL）⇒ 占死该连接的处理线程。
# 桶：20 事件/秒补充、突发上限 40；超出的**丢弃并计数**（不报错、不断连接 —— 正常客户端
# 不会有这种流量，断开只会让"网络抖动导致的重发"变成掉线）。
_EVENT_BUCKET_RATE = 20.0
_EVENT_BUCKET_CAP = 40.0
_event_buckets = {}
_event_buckets_lock = threading.Lock()
_event_bucket_stats = {"dropped": 0, "conns": 0}


def _event_rate_ok():
    """该连接当前是否还有事件配额。取不到 sid（非事件上下文）时一律放行。"""
    try:
        sid = request.sid
    except Exception:
        return True
    now = time.time()
    with _event_buckets_lock:
        b = _event_buckets.get(sid)
        if b is None:
            b = [_EVENT_BUCKET_CAP, now]
            _event_buckets[sid] = b
            _event_bucket_stats["conns"] += 1
            if len(_event_buckets) > 5000:
                # 兜底清理：60s 没动静的连接直接删（正常情况下 disconnect 会删）
                for k in [k for k, v in _event_buckets.items() if now - v[1] > 60]:
                    _event_buckets.pop(k, None)
        else:
            b[0] = min(_EVENT_BUCKET_CAP, b[0] + (now - b[1]) * _EVENT_BUCKET_RATE)
            b[1] = now
        if b[0] < 1.0:
            _event_bucket_stats["dropped"] += 1
            return False
        b[0] -= 1.0
        return True


def _with_event_bucket(fn):
    """给 socketio 事件处理器套一层令牌桶（connect/disconnect 不套）。"""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not _event_rate_ok():
            return None
        return fn(*args, **kwargs)
    return wrapper


def sio_event(name):
    """`@sio_event(...)` 的替代：注册时套上事件令牌桶（S1-07）。

    刻意用同一个装饰器入口替换全部 `@sio_event(...)`：这样"以后新增的 handler 忘了
    加限速"在结构上不会发生（漏写装饰器 = 事件根本没注册，验收脚本会立刻发现）。
    """
    def deco(fn):
        if name in ("connect", "disconnect"):
            socketio.on(name)(fn)
            return fn
        wrapped = _with_event_bucket(fn)
        socketio.on(name)(wrapped)
        return wrapped
    return deco


def _sio_handshake_gate(wsgi_app):
    """WSGI 门：拦截无 token 的 Engine.IO 首次握手。失败关闭（门内异常也回 403）。"""

    def middleware(environ, start_response):
        def _forbid():
            body = b"Forbidden"
            start_response("403 Forbidden", [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        try:
            path = environ.get("PATH_INFO") or ""
            if path.startswith("/socket.io/"):
                qs = environ.get("QUERY_STRING") or ""
                # 超长 query 直接当"无 token"（403），避免 parse_qsl 成为 CPU 放大面
                params = (dict(urllib.parse.parse_qsl(qs, keep_blank_values=True))
                          if len(qs) <= _SIO_GATE_MAX_QUERY else {})
                if "sid" not in params:
                    tok = environ.get("HTTP_X_PRINTER_TOKEN") or params.get("token") or ""
                    # S1-01：per-device token 与全局 TOKEN 都走统一认证
                    if not _authenticate_printer(tok):
                        _SIO_GATE_STATS["rejected"] += 1
                        return _forbid()
                    _SIO_GATE_STATS["accepted"] += 1
        except Exception:
            return _forbid()
        return wsgi_app(environ, start_response)

    return middleware


app.wsgi_app = _sio_handshake_gate(app.wsgi_app)
# ===== S0-03：ProxyFix（nginx 用 $proxy_add_x_forwarded_for 追加，x_for=1 取最右一项 =
# nginx 看到的真实客户端；客户端伪造左侧 XFF 无效）。注意 werkzeug 没有 x_real_ip 参数
# （计划附录 A-1：传了会 TypeError、后端直接起不来）。
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
# 注意：以上两行依赖下方定义的 _printer_token_ok()，在请求期解析（模块导入完成后才收请求）。


# ===== S2-04（DS-13/R4）：安全响应头 =====
# 这些头对所有响应生效（含上传目录里被下载的文件）：
#   · nosniff —— 浏览器不得把上传的文件按内容猜成 HTML/JS 执行（文件下载面必配）；
#   · X-Frame-Options + CSP frame-ancestors —— 任何站点都不能把接口/文件嵌进 iframe；
#   · Referrer-Policy —— 带 file_id/下载 token 的 URL 不随 Referer 外发；
#   · HSTS 只在 https 下加（本机 http 调试不加，避免"localhost 被强制 https"）。
# 刻意不加 Cross-Origin-Resource-Policy：APK 的 WebView 与第三方客户端会跨源取图，
# 收紧它容易变成"图片不显示"的线上疑难杂症，收益（防 no-cors 嵌入）远小于代价。
@app.after_request
def _apply_security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Content-Security-Policy",
                            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
    resp.headers.setdefault("Permissions-Policy",
                            "geolocation=(), microphone=(), camera=()")
    try:
        if request.is_secure:
            resp.headers.setdefault("Strict-Transport-Security",
                                    "max-age=31536000; includeSubDomains")
    except Exception:
        pass
    return resp


DATABASE = "orders.db"
USER_DATABASE = "users.db"
UPLOAD_DIR = "uploads"
# 注意（P0-2）：avatars/ 目录（用户头像）不参与保留期清理与缓存统计——用户数据不随
# 打印文件过期删除是设计使然，但头像目前没有数量/大小配额与独立清理机制，长期运行
# 可能持续占用磁盘（每次上传 ≤2MB，但无人回收）。如需要回收需另加独立策略（如
# 按用户头像数量上限或未活跃时间清理）。
AVATAR_DIR = os.path.join(UPLOAD_DIR, "avatars")
MD5_INDEX_FILE = os.path.join(UPLOAD_DIR, "md5_index.json")
RETENTION_CONFIG_FILE = os.path.join(UPLOAD_DIR, "retention_config.json")

def _resolve_files_path(stored_path: str) -> str:
    """把 `files.path` 变成真实可访问的路径（两种历史形态都认）。

    2026-11-13 修的真 bug：上传时 `file_path = os.path.join(UPLOAD_DIR, subdir, saved_name)`
    ⇒ `files.path` 里**已经带 uploads/ 前缀**（如 `uploads/docx/x.docx`）。旧代码又拼了一次
    `UPLOAD_DIR` ⇒ 前缀被拼了两遍（uploads 套 uploads）恒不存在 ⇒ `/api/file_preview/<id>` 对**所有类型**
    都回 404（用户看到的"怎么都没有缩略图"一直是这个，不是渲染能力问题）。
    这里：绝对路径原样返回；相对路径先按原样试，不存在才补 UPLOAD_DIR 前缀（兼容更老的库）。
    """
    p = str(stored_path or "").strip()
    if not p:
        return ""
    if os.path.isabs(p):
        return p
    direct = os.path.normpath(p)
    if os.path.isfile(direct):
        return direct
    alt = os.path.normpath(os.path.join(UPLOAD_DIR, direct))
    return alt if os.path.isfile(alt) else direct



# ===== S2-11（DS-20）：日志脱敏助手 =====
# 日志进 journald（会留 30 天、可被 collect_all 打包回传），所以**顾客原文件名与绝对路径
# 一律不落盘**：排查靠 file_id / MD5 前缀已经够用，原文件名属于顾客隐私，绝对路径会暴露
# 部署目录结构（DS-10 同一类信息面）。错误文本同理：OSError 的 str 里通常带完整路径。
def _redact_filename(name: str) -> str:
    """日志用文件名：只保留扩展名（xxx.pdf → ***.pdf）。"""
    ext = os.path.splitext(name or "")[1].lower()
    return ("***" + ext) if ext else "***"


_PATH_IN_TEXT_RE = re.compile(r"(?:[A-Za-z]:\[^\s'\"<>]+|/(?:[\w.\-]+/)*[\w.\-]+)")


def _redact_paths(text: str) -> str:
    """把一段文本里的绝对路径（C:\\... 或 /a/b/c）替换成 <path>。

    用于异常文本：`str(e)` 经常带完整路径（OSError/FileNotFoundError），而异常文本会进
    journald 并被「日志管理」打包回传。只留诊断用的类名 + 少量上下文，路径不落盘。"""
    return _PATH_IN_TEXT_RE.sub("<path>", text or "")


def _rel_display(path: str) -> str:
    """日志用路径：uploads 下的给相对路径，其余只给 basename（绝不打印绝对路径）。"""
    if not path:
        return ""
    try:
        base = os.path.abspath(UPLOAD_DIR)
        full = os.path.abspath(path)
        if full == base or full.startswith(base + os.sep):
            return os.path.relpath(full, base)
    except (OSError, ValueError):
        pass
    return os.path.basename(path)


# ===== 2026-10-25：原文件名（「我 → 我的文件」显示的是微信临时名） =====
# 现场：/api/my_files 列出的是 tmp_617c7e76….pdf 这种名字。根因是**上传** ——
# 小程序 `wx.uploadFile` 的 multipart 文件名取自「临时路径的 basename」，微信把它变成
# `tmp_<40~64 位十六进制>.<ext>`（真机实测），而 `files.original_name` 直接存了这个值。
# 真正对顾客有意义的名字记录在 `order_files.file_name`（提交订单时客户端把自己选的文件名
# 带上来）。四条落地（缺一不可）：
#   ① 客户端在 /api/upload 表单里带真实文件名 `file_name`（小程序 chooseMessageFile 的 name）；
#   ② 服务端优先用它当 `files.original_name`（`clean_original_name` 规整：basename、
#      去控制字符、限长、扩展名必须与本次上传一致；不通过就退回上传头里的名字，不拒单）；
#   ③ 存量行：`/api/my_files` 用 `order_files.file_name`（最近一次下单用过的名字）兜底显示；
#   ④ 存量行自愈：提交订单时若该行仍是临时名，按本次提交的真实名回填（同扩展名才写）。
_TEMP_UPLOAD_NAME_RE = re.compile(r"^tmp_[0-9a-fA-F]{16,}\.[A-Za-z0-9]{1,8}$")


def looks_like_temp_upload_name(name) -> bool:
    """是否是微信上传留下的临时文件名（tmp_<hex>.<ext>）—— 对顾客没有意义。"""
    return bool(_TEMP_UPLOAD_NAME_RE.match(str(name or "").strip()))


def clean_original_name(name, ext_lower="", max_len=200) -> str:
    """把客户端自报的原文件名规整成可安全落库/展示的名字；不可用返回 ""。

    只做**展示/审计用**的规整，不参与落盘路径（落盘名仍然由服务端自己生成）：
      · 只取 basename（同时兼容 `\\` 与 `/`）—— 客户端不能借路径改写任何东西；
      · 去掉控制字符/换行，长度截到 max_len（保留扩展名）；
      · 给了 ext_lower 时要求扩展名一致（否则 ""：绝不让展示名与真实格式脱钩，
        页数分析/白名单等按扩展名分支的逻辑都读这个字段）。
    """
    raw = str(name or "").strip()
    if not raw:
        return ""
    base = raw.replace("\\", "/").split("/")[-1].strip()
    if not base or base in (".", ".."):
        return ""
    base = "".join(ch for ch in base if ch >= " " and ch != "\x7f")
    if not base:
        return ""
    if len(base) > max_len:
        stem, ext = os.path.splitext(base)
        base = (stem[:max(1, max_len - len(ext))] or "file") + ext
    if ext_lower and os.path.splitext(base)[1].lower().lstrip(".") != str(ext_lower).lower():
        return ""
    return base


# ===== S3-04（DS-24）：文件访问审计的唯一入口 =====
def _audit_ext(name) -> str:
    """从文件名/路径里只取扩展名（S2-11：审计只记扩展名，不记原文件名）。"""
    try:
        return os.path.splitext(str(name or ""))[1].lower()
    except Exception:  # noqa: BLE001
        return ""


def _audit_file_access(action, result, file_id="", openid="", actor="",
                       task_id=None, ext="", count=None):
    """记一条文件访问审计（谁、读了哪个文件、何时）。

    fail-open 的第二层：`file_audit.record()` 自身吞一切异常，这里再兜一层 —— 审计模块/字段
    出任何问题都**不许**改变下载、删除、列表的正常返回。`ip` 用 `request.remote_addr`
    （ProxyFix 已还原 nginx 之后的真实 IP），**不信任**原始 `X-Forwarded-For`。
    """
    try:
        try:
            ip = request.remote_addr or ""
        except Exception:  # noqa: BLE001  非请求上下文（后台任务复用本函数）时没有 IP
            ip = ""
        file_audit.record(action, result, file_id=file_id, openid=openid, actor=actor,
                          task_id=task_id, ip=ip, ext=ext, count=count)
    except Exception as e:  # noqa: BLE001
        print("  [AUDIT] 文件访问审计记录失败（已忽略，不影响主流程）: %s" % type(e).__name__)

# 打印机设备注册表 / 接单接管状态（2026-11 新增：多设备共连服务器时的唯一接管者机制）
# devices.json: { client_id: {device_name, owner_name, first_seen, last_seen} }
DEVICES_FILE = "devices.json"
# device_pin_order.json: {"order": [client_id, ...], "updated_at": <epoch 秒>, "updated_by": <openid|"">}
# 「接单设备置顶顺序」（2026-10）—— 全局共享一份，第一项就是默认选中的设备。
DEVICE_PIN_FILE = "device_pin_order.json"
# S1-06：APP/网页设备的"设备密钥"（不是 printer token）。{sha256(device_id)[:32]: {...}}
# 只存 secret 的哈希；首次登录下发一次，之后凭 secret 换 token —— 不再只凭自报 device_id。
DEVICE_SECRETS_FILE = "device_secrets.json"
# 迁移宽限：老 APP 不会带 device_secret，窗口内放行并（轮换）下发一次；窗口后强制。
# 解除条件：门店/用户手机全部 ≥ 会存 device_secret 的客户端版本后，把这里改成 0 并观察 24h。
DEVICE_SECRET_GRACE_SECONDS = 14 * 24 * 3600
_device_secrets_lock = threading.Lock()

# printer_claim.json: { active_client_id, owner_name, claimed_at } — 当前启用「接单」的设备
CLAIM_FILE = "printer_claim.json"

# ── 测试/联调设备的识别（2026-10）─────────────────────────────────────────────
# 联调与部署冒烟脚本会以固定前缀注册设备。历史上它们直连生产，把 48 台 debug-* /
# socketio-debug-* 灌进了生产 devices.json，授权页一屏全是"测试设备"、还留着僵尸接单记录。
# 真机的 client_id 形态是 "<计算机名>-<8~12 位十六进制>"，下面这些是脚本自造的名字，不会撞车。
# 识别出来后：① 列表接口默认隐藏（附 hidden_test_devices 计数，页面可提示）；
# ② 定时任务把**离线**的它们连同接单记录一起清掉（在线的不动，避免打断正在跑的机器）；
# ③ 授权页有「清理测试设备」按钮（先 dry-run 预览再确认）。
TEST_DEVICE_PREFIXES = (
    "debug-", "socketio-debug", "deploy-check", "wscheck-",
    "fallback-", "staging-", "test-device", "pc-test", "PRODTEST",
)
TEST_DEVICE_IDS = {"debug", "test"}


def is_test_device_id(client_id: str) -> bool:
    """是否像测试/联调脚本注册的设备（只认明确前缀，避免误伤真机）。"""
    cid = (client_id or "").strip()
    if not cid:
        return False
    return cid in TEST_DEVICE_IDS or cid.startswith(TEST_DEVICE_PREFIXES)


def _delete_devices(client_ids, reason: str = "") -> list[str]:
    """批量删除设备并清掉它们的接单记录（返回真正删掉的 id 列表）。"""
    ids = [c for c in dict.fromkeys(client_ids or []) if c]
    if not ids:
        return []
    devices = load_devices()
    removed = [c for c in ids if devices.pop(c, None) is not None]
    if removed:
        save_devices(devices)
    cleared = []
    with _claim_lock:
        claim = _load_claim_impl()
        claiming = claim.get("claiming_devices")
        if isinstance(claiming, dict):
            for c in ids:
                if claiming.pop(c, None) is not None:
                    cleared.append(c)
            if cleared:
                _save_claim_impl(claim)
    if removed or cleared:
        broadcast_printer_state()
        print(f"[DEV] 清理设备 {len(removed)} 台（{reason}）"
              f"{f'，同时清空 {len(cleared)} 条接单记录' if cleared else ''}: {removed[:8]}")
    return removed

# 默认保留时间：7 天
DEFAULT_RETENTION = {"days": 7, "hours": 0}

# ===== S1-03（D-6 / D-7 已拍板）：客户保留模式 =====
# `-1` = 打印完成后立即从服务器删除；`0` = 遵从管理员在「服务器存储」里的当前设置。
# 正数小时仍保留给旧版客户端，并夹到 1..MAX_RETENTION_HOURS。
MAX_RETENTION_HOURS = 168
IMMEDIATE_DELETE_RETENTION_HOURS = -1
DEFAULT_FILE_RETENTION_HOURS = 0
RETENTION_CHOICES_HOURS = (IMMEDIATE_DELETE_RETENTION_HOURS, 0)


def _clamp_retention_hours(value):
    """规范化客户保留模式；非法/缺失返回 0（= 遵从服务器当前设置）。"""
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return 0
    if hours == IMMEDIATE_DELETE_RETENTION_HOURS:
        return IMMEDIATE_DELETE_RETENTION_HOURS
    if hours <= 0:
        return 0
    return min(hours, MAX_RETENTION_HOURS)


def get_retention_limits():
    """对外暴露的保留期口径（移动端选择器用；也用于测试）。"""
    cfg = load_retention_config()
    server_retention_hours = max(
        0,
        int(cfg.get("days", 0) or 0) * 24 + int(cfg.get("hours", 0) or 0),
    )
    return {
        "max_retention_hours": MAX_RETENTION_HOURS,
        "default_retention_hours": DEFAULT_FILE_RETENTION_HOURS,
        "choices_hours": list(RETENTION_CHOICES_HOURS),
        "immediate_delete_hours": IMMEDIATE_DELETE_RETENTION_HOURS,
        "server_retention_hours": server_retention_hours,
    }

# -------- 防滥用 / DDoS 防护阈值 --------
# 管理员/超管可通过 /api/admin/security 查看和调整，持久化到 uploads/security_config.json，
# 运行中保存立即生效（无需重启）。0 表示关闭对应检查。各项说明：
#   - user_quota_mb           每用户累计文件配额（MB），超限拒绝上传（防存储型 DDoS）
#   - disk_min_free_mb        磁盘剩余空间守卫（MB），低于阈值拒绝上传
#   - queued_timeout_hours    排队子任务超时淘汰（小时），释放被 queued 订单钉住的磁盘文件
#   - upload_rate_limit       每用户每分钟上传次数上限
#   - submit_order_rate_limit 每用户每分钟提交订单次数上限
#   - device_login_rate_limit 每 IP / 每设备每小时设备登录次数上限
#   - redeem_rate_limit       每用户每分钟密钥兑换次数上限
#   - log_report_rate_limit   每 IP 每分钟日志上报条数上限
SECURITY_CONFIG_FILE = os.path.join(UPLOAD_DIR, "security_config.json")

DEFAULT_SECURITY = {
    "user_quota_mb": 2048,
    "disk_min_free_mb": 5120,
    "queued_timeout_hours": 24,
    "upload_rate_limit": 20,
    "submit_order_rate_limit": 20,
    "device_login_rate_limit": 20,
    "redeem_rate_limit": 10,
    "log_report_rate_limit": 30,
}

# 每项允许范围 (min, max)，POST 保存时校验（0 是否允许见 DEFAULT_SECURITY 注释）
SECURITY_RANGES = {
    "user_quota_mb": (0, 102400),
    "disk_min_free_mb": (0, 102400),
    "queued_timeout_hours": (0, 720),
    "upload_rate_limit": (1, 600),
    "submit_order_rate_limit": (1, 600),
    "device_login_rate_limit": (1, 600),
    "redeem_rate_limit": (1, 600),
    "log_report_rate_limit": (1, 600),
}

_SECURITY = dict(DEFAULT_SECURITY)


def load_security_config():
    """加载防滥用配置（文件缺失/损坏时回退默认值），返回配置副本。"""
    global _SECURITY
    cfg = dict(DEFAULT_SECURITY)
    if os.path.exists(SECURITY_CONFIG_FILE):
        try:
            with open(SECURITY_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k in cfg:
                if k in data and isinstance(data[k], (int, float)):
                    cfg[k] = int(data[k])
        except (json.JSONDecodeError, IOError):
            pass
    _SECURITY = cfg
    return dict(cfg)


def save_security_config(cfg):
    """保存防滥用配置并立即更新内存副本（运行时生效，无需重启）。"""
    global _SECURITY
    _SECURITY = dict(cfg)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with open(SECURITY_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(_SECURITY, f, ensure_ascii=False, indent=2)


# 模块加载即读取已保存的配置（若无则用默认值）
_SECURITY = load_security_config()

# -------- 「最近订单」条数（2026-11）--------
# 管理端「最近 N 条订单」卡片显示多少条：默认 10，超管可在 1..100 之间调整。
# 与防滥用阈值同款持久化（uploads/recent_orders_config.json：启动载入内存副本、保存即时生效），
# 但**写权限只给超管**（见 /api/admin/recent_orders_limit；判据是 SUPER_ADMIN_OPENID，
# 不是 compute_role —— compute_role 会把超管折叠成 "admin"，照抄 /api/admin/security 会让谁都改得动）。
RECENT_ORDERS_CONFIG_FILE = os.path.join(UPLOAD_DIR, "recent_orders_config.json")
DEFAULT_RECENT_ORDERS_LIMIT = 10
RECENT_ORDERS_LIMIT_RANGE = (1, 100)
# 2026-11：翻页模式只接受这几档每页条数（与结算页/前端下拉框对齐，避免任意值把响应撑大）
RECENT_ORDERS_PER_PAGE_CHOICES = (10, 20, 50, 100)
_RECENT_ORDERS_LIMIT = DEFAULT_RECENT_ORDERS_LIMIT


def load_recent_orders_config():
    """加载「最近订单」条数（文件缺失/损坏/越界 → 回退默认 10）。

    越界值也回退：手改文件写出 limit=99999 时，宁可显示默认 10 条，
    也不让一张卡片把 /api/admin/recent_orders 变成全表扫描入口。
    """
    global _RECENT_ORDERS_LIMIT
    limit = DEFAULT_RECENT_ORDERS_LIMIT
    if os.path.exists(RECENT_ORDERS_CONFIG_FILE):
        try:
            with open(RECENT_ORDERS_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            v = data.get("limit") if isinstance(data, dict) else None
            if (isinstance(v, int) and not isinstance(v, bool)
                    and RECENT_ORDERS_LIMIT_RANGE[0] <= v <= RECENT_ORDERS_LIMIT_RANGE[1]):
                limit = v
        except (json.JSONDecodeError, OSError):
            pass
    _RECENT_ORDERS_LIMIT = limit
    return limit


def save_recent_orders_config(limit):
    """保存「最近订单」条数并立即更新内存副本（原子写：临时文件 + os.replace）。

    先落盘再改内存：写盘失败时旧值仍然有效，接口层能如实回 500，
    不会出现"接口报错但内存已经变了、重启又变回去"的错位。
    """
    global _RECENT_ORDERS_LIMIT
    limit = int(limit)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    tmp = RECENT_ORDERS_CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"limit": limit}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, RECENT_ORDERS_CONFIG_FILE)
    _RECENT_ORDERS_LIMIT = limit


# 模块加载即读取已保存的条数（若无/损坏则用默认值）
_RECENT_ORDERS_LIMIT = load_recent_orders_config()

# 已知扩展名 → 子目录映射
EXT_DIR_MAP = {
    "pdf": "pdf", "doc": "doc", "docx": "docx", "xls": "xls", "xlsx": "xlsx",
    "ppt": "ppt", "pptx": "pptx", "txt": "txt", "csv": "csv",
    "png": "png", "jpg": "jpg", "jpeg": "jpg", "gif": "gif", "bmp": "bmp",
    "webp": "webp", "tiff": "tiff", "tif": "tiff", "svg": "svg",
    "zip": "zip", "rar": "rar", "7z": "7z",
}


# -------- 全局数据库锁与独立连接（供后台线程和加锁事务使用）--------
db_lock = threading.Lock()

# -------- MD5 索引文件锁（P2-5）：load/save 均需持锁，防并发读写损坏索引 --------
_md5_index_lock = threading.Lock()

# -------- 内存限速器（P1-6；S1-08 改造）--------
# 滑动窗口 {key_hash: {"ts": [timestamp, ...], "win": 窗口秒数}}。
# 注意：内存计数仅对单 worker 生效，生产环境多 worker / 多实例时需要在 nginx 层再加
# IP 维度限速（S0-01 已加）。
#
# S1-08 改造（docs/安全加固修复计划_2026-10-23.md §S1-08 / R8）——原来有三个洞：
#   ① **无界增长**：`defaultdict(list)` 永不删 key，任何"带随机后缀"的参数（device_id、
#      file_id、openid）都能让字典无限涨（R8 的内存增长面）；
#   ② **key 是原串**：一个 1 MB 的 device_id 直接成为 key（S0-03 已在调用侧用
#      sha256 截断，这里再统一兜一层，避免下一个入口又忘了）；
#   ③ **无清扫**：过期时间戳只在被再次访问时才清理，冷 key 永久占内存。
# 现在：key 一律 sha256[:16]；容量上限 50k（超了先清过期、再按插入顺序淘汰最旧）；
# 每 60 秒做一次全量清扫（摊薄成本，不在每次请求里扫全表）。
_RATE_LIMIT_MAX_KEYS = 50000
_RATE_LIMIT_SWEEP_INTERVAL = 60.0
_rate_limits = OrderedDict()
_rate_limits_lock = threading.Lock()
_rate_limits_last_sweep = [0.0]
_rate_limits_stats = {"sweeps": 0, "evicted": 0, "rejected": 0}


def _rate_limit_hash(key):
    """限速 key 统一 sha256 截断：原串不进字典（R8 的内存增长面 / 长 key 放大面）。"""
    return hashlib.sha256(str(key).encode("utf-8", "replace")).hexdigest()[:16]


def _sweep_rate_limits_locked(now):
    """清掉「窗口内已无有效时间戳」的 key。**必须在持锁下调用**。

    桶结构刻意用**单个 list**（`[window_seconds, ts1, ts2, ...]`）而不是
    `{"ts": [...], "win": n}`：50k 个 key 时前者约 14 MB 堆、后者约 24 MB（实测），
    而 §S1-08 的验收线是"灌 10 万个 key 后堆增长 < 20 MB"。少一层容器就是少一半开销。
    """
    dead = []
    for k, bucket in _rate_limits.items():
        if len(bucket) <= 1 or bucket[-1] <= now - bucket[0]:
            dead.append(k)
    for k in dead:
        _rate_limits.pop(k, None)
    _rate_limits_last_sweep[0] = now
    _rate_limits_stats["sweeps"] += 1

# 页数分析惰性触发防抖：file_id → 最近一次主动推送时间戳
# （前端 /api/file_page 轮询时，若打印机在线且文件未分析，可主动补推；
#   30s 防抖避免每 2s 轮询刷爆分析请求，也避免补推 + 轮询双重推送）
_last_page_analysis_push = {}
_last_page_analysis_push_lock = threading.Lock()
# 页数分析推送目标记录（file_id → {"sid": …, "client_id": …}）：
#   · sid       供取消分析时精确转发（to=sid）
#   · client_id 供 page_count_result 的**归属判定**（🔴-D3）——sid 会随重连变化，
#     设备分析到一半断线重连后仍应能回报，故判据取 client_id
_analysis_push_sid = {}

# 回报页数的上限（🔴-D3）：页数是计费输入（calculate_price × 份数 + 父订单回溯重算），
# 离谱数值会炸掉价格。真机最大作业量远低于此值（A3 图纸/合订本量级也在千页内）。
MAX_REPORTED_PAGES = 20000

# ===== S0-04：无上限输入一族的边界（R5/R6/R8/DS-20/DS-21）=====
# 1 MB JSON 能塞进的元素数远大于任何真实业务（最大单通常十几份），超限一律 400。
MAX_ORDER_FILES = 20          # /api/submit_order 的 files 数组上限
MAX_PAGE_RANGE_LEN = 200      # 单条页码范围字符串上限（库里另有 500 字符截断兜底）
MAX_ECHO_FILES = 50           # submit_order 响应回显的子任务上限（超出只给 files_total/files_omitted）
MAX_EVENT_TASK_IDS = 100      # SocketIO 事件的 task_ids 上限

# ===== S0-05（D-2 已拍板：永久拒绝，不做开关）=====
# 这些类型在店铺机的转换/浏览器管线里可执行脚本或读本机文件 ⇒ 顾客文件可读
# file:///…/print_config.json 并把全站 printer token 外发（DS-01）。
DANGEROUS_UPLOAD_EXTS = frozenset({"html", "htm", "shtml", "xhtml", "svg", "js", "mjs"})

# ===== S2-03：上传类型白名单 + 魔数一致性 =====
# 与 S0-05 的 denylist 是**两层**：denylist 挡"已知危险"，白名单保证"只收认识的格式" ——
# 以后出现新花样（.svgz / .hta / .mhtml …）默认进不来，不用等有人想起来往 denylist 里补。
# 口径 = 两端客户端 supportedExts（pdf/doc/docx/txt/csv/md/图片）+ 客户端明确列为"不支持"
# 的办公格式（xls/xlsx/ppt/pptx，保留兼容，服务端不主动扩大拒绝面）。压缩包与未知类型不再收。
UPLOAD_EXT_WHITELIST = frozenset({
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "md",
    "jpg", "jpeg", "png", "bmp", "gif", "webp", "tiff", "tif",
})

# 扩展名 → 允许的"真实家族"（魔数判定结果）。file 头对不上 ⇒ 改名/伪装，拒绝。
#   ole  = OLE2 复合文档（doc/xls/ppt 的经典格式）
#   zip  = OOXML 容器（docx/xlsx/pptx），本质是 zip
#   text = 无魔数，只粗判"是否二进制"（见 _looks_binary_text）
_UPLOAD_EXT_FAMILIES = {
    "pdf": ("pdf",),
    "doc": ("ole", "rtf"), "xls": ("ole",), "ppt": ("ole",),
    "docx": ("zip",), "xlsx": ("zip",), "pptx": ("zip",),
    "jpg": ("jpeg",), "jpeg": ("jpeg",), "png": ("png",), "gif": ("gif",),
    "bmp": ("bmp",), "webp": ("webp",), "tiff": ("tiff",), "tif": ("tiff",),
}
_TEXT_UPLOAD_EXTS = frozenset({"txt", "csv", "md"})

# 明确的"危险 MIME"：即便扩展名看着正常也拒绝（与 denylist 同一条链的纵深）
_DANGEROUS_MIMES = frozenset({"text/html", "application/xhtml+xml", "image/svg+xml",
                              "application/javascript", "text/javascript",
                              "application/x-javascript"})
# MIME → 家族（只用于"扩展名与 MIME 明确冲突"时拒绝；octet-stream/空值一律放行，
# 真实客户端（微信 wx.uploadFile）大量使用 application/octet-stream，不能因此误杀）
_MIME_FAMILIES = {
    "application/pdf": "pdf",
    "application/msword": "ole",
    "application/vnd.ms-excel": "ole",
    "application/vnd.ms-powerpoint": "ole",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "zip",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "zip",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "zip",
    "image/jpeg": "jpeg", "image/png": "png", "image/gif": "gif", "image/bmp": "bmp",
    "image/webp": "webp", "image/tiff": "tiff",
    "text/plain": "text", "text/csv": "text", "text/markdown": "text",
}

# Markdown 在 wkhtmltopdf 里被渲染成 HTML：扫描危险语法（与客户端 converter 同一份口径）
_UNSAFE_MD_PATTERNS = (
    ("<script", re.compile(r"<\s*script", re.I)),
    ("<iframe", re.compile(r"<\s*iframe", re.I)),
    ("<object", re.compile(r"<\s*object", re.I)),
    ("<embed", re.compile(r"<\s*embed", re.I)),
    ("on-event", re.compile(r"\bon[a-z]+\s*=", re.I)),
    ("data:text/html", re.compile(r"data\s*:\s*text/html", re.I)),
    ("file-url", re.compile(r"\bfile\s*:", re.I)),
    ("unc", re.compile(r"\\\\[A-Za-z0-9_.$-]+\\")),
)


def _scan_unsafe_markdown(text):
    """返回命中的规则名（没命中返回 ""）。空文档/编码异常一律放行交给后续流程。"""
    if not text:
        return ""
    for name, pat in _UNSAFE_MD_PATTERNS:
        if pat.search(text):
            return name
    return ""


# 日志脱敏（S0-06 动作 3）：把 URL 里的 token / t / key / access_token 值替换成 ***。
# requests 的异常文本与前端上报的日志都可能带完整 URL。
_SCRUB_RE = re.compile(r"([?&](?:token|t|key|access_token)=)[^&\s\"'<>]+", re.I)


def _scrub_secret(text):
    if not text:
        return text
    try:
        return _SCRUB_RE.sub(r"\1***", str(text))
    except Exception:
        return "[scrub-failed]"


def _json_dict():
    """统一 JSON 请求体解析（S0-04）：只有 dict 才算有效请求体。

    数组/字符串/数字/布尔、畸形 JSON 一律 None（调用方回 400）；超深嵌套抛的 RecursionError
    由全局 errorhandler 兜成 400。原实现直接 `data.get(...)` ⇒ `[1]` 会 500。
    """
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _rate_limit(key, max_count, window_seconds):
    """滑动窗口限速：key 在 window_seconds 秒内最多 max_count 次，超限返回 False。

    key 只用于寻址（内部 sha256[:16]），容量与清扫见上面的 `_RATE_LIMIT_*`。"""
    now = time.time()
    hkey = _rate_limit_hash(key)
    with _rate_limits_lock:
        # 定期全量清扫（每 60s 一次；冷 key 不会永久占内存）
        if now - _rate_limits_last_sweep[0] >= _RATE_LIMIT_SWEEP_INTERVAL:
            _sweep_rate_limits_locked(now)
        bucket = _rate_limits.get(hkey)
        if bucket is None:
            # 容量上限：按插入顺序淘汰最旧（OrderedDict，O(1)）。
            # ⚠ 这里**绝不**做全量清扫（S2-12 压测实测：满 50k 后每插入一个 key 就扫 50k 条
            #   ⇒ O(N×M)，灌 10 万 key 从 0.3 s 劣化到 64 s，等于给攻击者一个 CPU 放大器）。
            #   过期 key 由上面「每 60s 一次」的周期清扫回收，够用且成本摊薄。
            if len(_rate_limits) >= _RATE_LIMIT_MAX_KEYS:
                while len(_rate_limits) >= _RATE_LIMIT_MAX_KEYS:
                    _rate_limits.popitem(last=False)
                    _rate_limits_stats["evicted"] += 1
            bucket = [window_seconds]
            _rate_limits[hkey] = bucket
        else:
            bucket[0] = window_seconds
            _rate_limits.move_to_end(hkey)
        # 清理窗口外的旧时间戳（下标 1 是队头，最旧）
        while len(bucket) > 1 and bucket[1] <= now - window_seconds:
            del bucket[1]
        if len(bucket) - 1 >= max_count:
            _rate_limits_stats["rejected"] += 1
            return False
        bucket.append(now)
        return True


def _rate_limit_ok(key, max_count, window_seconds):
    """语义别名（S1-08 起新入口统一用这个读起来更直白）。"""
    return _rate_limit(key, max_count, window_seconds)


class OrderRejected(Exception):
    """提交订单业务校验失败（返回 400）：在事务内抛出以中止并回滚。"""
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def get_db_conn():
    """独立的数据库连接，不依赖 Flask 应用上下文，专供后台线程和加锁事务使用"""
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_ext_dir(ext):
    """根据扩展名返回对应的子目录名，若未知则返回 'other'"""
    ext = ext.lower().lstrip(".")
    return EXT_DIR_MAP.get(ext, "other")


def get_file_md5(file_path):
    """分块计算文件 MD5，支持大文件"""
    md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            md5.update(chunk)
    return md5.hexdigest()


def load_md5_index():
    """加载 MD5 索引文件，不存在则返回空字典。
    兼容两种格式：旧 {md5: rel_path} 和 新 {md5: {path, original_name, page_count, page_count_verified}}"""
    with _md5_index_lock:
        if not os.path.exists(MD5_INDEX_FILE):
            return {}
        try:
            with open(MD5_INDEX_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}


def save_md5_index(index):
    """保存 MD5 索引到文件"""
    with _md5_index_lock:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        with open(MD5_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)


def _md5_entry_get(index, md5):
    """读取 MD5 条目，兼容旧格式 {md5: rel_path}"""
    val = index.get(md5)
    if val is None:
        return None
    if isinstance(val, str):
        return {"path": val, "original_name": "", "page_count": 0, "page_count_verified": False}
    return val


def _md5_entry_set(index, md5, path=None, original_name=None, page_count=None,
                   page_count_verified=None, openid=None):
    """写入/更新 MD5 条目，自动升级旧格式。返回更新后的 index。"""
    existing = index.get(md5)
    if isinstance(existing, str):
        existing = {"path": existing, "original_name": "", "page_count": 0, "page_count_verified": False}
    elif existing is None:
        existing = {}
    if path is not None:
        existing["path"] = path
    if original_name is not None:
        existing["original_name"] = original_name
    if page_count is not None:
        existing["page_count"] = page_count
    if page_count_verified is not None:
        existing["page_count_verified"] = page_count_verified
    if openid is not None:
        existing["openid"] = openid
    index[md5] = existing
    return index


def _md5_entry_has_verified_count(index, md5):
    """检查某 MD5 是否有已验证的页数"""
    entry = _md5_entry_get(index, md5)
    if not entry:
        return False
    return entry.get("page_count", 0) > 0 and entry.get("page_count_verified", False)


def _cached_verified_page_count(file_md5, openid, md5_index=None):
    """按 MD5 取「已验证页数」缓存（S2-05）。**只认同一属主**的缓存。

    为什么要有 files 表这条兜底：MD5 索引条目是"md5 → 某一个副本的路径 + 页数"，
    副本被属主删除或过期清理时该条目会被摘掉 —— 但"这版文档共几页"是**内容属性**，
    与哪一个副本还在无关。以前只认索引 ⇒ 删掉一个副本就把页数缓存一起忘了，
    后来者重新上传同一版 Word 又要等设备重新分析（顾客多等一轮、多一次下载转换）。

    为什么限定 openid（DS-14 的存在性预言机）：跨用户复用页数缓存会让"上传即拿到页数"
    与"上传后要等设备分析"形成可观测差异 —— 攻击者拿一份候选文档来试，就能判断
    "这份内容是否已经在系统里被别人传过"（`reused` 字段虽然拆了，这条旁路会重新打开）。
    只认自己的缓存后，第二个用户上传同一内容与上传全新内容的响应完全一致。
    """
    if not file_md5 or not openid:
        return 0
    idx = md5_index if md5_index is not None else load_md5_index()
    entry = _md5_entry_get(idx, file_md5) or {}
    try:
        if (entry.get("openid") == openid and entry.get("page_count_verified")
                and int(entry.get("page_count") or 0) > 0):
            return int(entry["page_count"])
    except (TypeError, ValueError):
        pass
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT page_count FROM files WHERE md5 = ? AND openid = ? AND page_count_verified = 1 "
            "AND page_count > 0 ORDER BY created_at DESC LIMIT 1",
            (file_md5, openid),
        ).fetchone()
    finally:
        conn.close()
    return int(row["page_count"]) if row else 0


def _path_reference_index(conn):
    """S2-05：物理路径 → 仍指向它的 files 行 id 集合（批量删除时用一次扫描）。

    新上传每行都有**自己的副本**，正常情况下一个路径只对应一行；本索引主要给存量数据
    兜底：历史 MD5 去重让多行共享同一物理文件，任一行到期都不能把别人的文件带走。
    """
    refs = {}
    for row in conn.execute("SELECT id, path FROM files WHERE path != ''"):
        p = row["path"]
        if not p:
            continue
        refs.setdefault(os.path.normpath(p), set()).add(row["id"])
    return refs


def _path_shared_by_others(conn, file_path, file_id):
    """该物理路径是否仍被**其它** files 行引用（单行删除用；批量见 _path_reference_index）。"""
    if not file_path:
        return False
    norm = os.path.normpath(file_path)
    for row in conn.execute("SELECT id, path FROM files WHERE path != '' AND id != ?", (file_id,)):
        if row["path"] and os.path.normpath(row["path"]) == norm:
            return True
    return False


def _purge_file_after_print(file_id):
    """按“立即删除”模式清理刚打印完成的文件。

    只删除物理文件并清空 files.path；订单/文件元数据保留，历史订单详情仍可展示。
    若同一物理文件仍被其它 files 行引用（历史 MD5 去重），只清当前行，绝不误删
    其它订单仍在使用的文件。
    """
    if not file_id:
        return False
    conn = get_db()
    path = ""
    shared = False
    row = None
    try:
        row = conn.execute(
            "SELECT id, path, original_name, openid, retention_hours "
            "FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
        if not row or int(row["retention_hours"] or 0) != IMMEDIATE_DELETE_RETENTION_HOURS:
            return False

        active_statuses = ('queued', 'printing', 'accepted', 'reserved',
                           'scheduled', 'waiting', 'downloading')
        placeholders = ",".join("?" for _ in active_statuses)
        busy = conn.execute(
            f"SELECT 1 FROM order_files WHERE file_id = ? AND status IN ({placeholders}) LIMIT 1",
            (file_id, *active_statuses),
        ).fetchone()
        if not busy:
            busy = conn.execute(
                f"SELECT 1 FROM orders WHERE file_id = ? AND status IN ({placeholders}) LIMIT 1",
                (file_id, *active_statuses),
            ).fetchone()
        if busy:
            print(f"  [DEL] 立即删除暂缓：文件 {file_id[:8]}... 仍被其它活跃订单使用")
            return False

        path = row["path"] or ""
        if not path:
            return False
        shared = _path_shared_by_others(conn, path, file_id)
        if not shared and os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                print(f"  [DEL] 立即删除文件失败 {_rel_display(path)}: {type(e).__name__}")
                return False

        conn.execute("UPDATE files SET path = '', size = 0 WHERE id = ?", (file_id,))
        conn.commit()
    finally:
        conn.close()

    # 2026-11：文件没了，预览缓存也不该留着（否则 uploads/previews 只增不减）
    _drop_previews(file_id)

    if path and not shared:
        try:
            rel = os.path.relpath(path, UPLOAD_DIR)
            index = load_md5_index()
            dead = [
                k for k, v in index.items()
                if (isinstance(v, str) and v == rel)
                or (isinstance(v, dict) and v.get("path") == rel)
            ]
            for k in dead:
                index.pop(k, None)
            if dead:
                save_md5_index(index)
        except Exception as e:  # noqa: BLE001
            print(f"  [DEL] 立即删除后清理 MD5 索引失败: {type(e).__name__}")

    if shared:
        print(f"  [DEL] 文件 {file_id[:8]}... 已按立即删除清空；物理文件仍被其它记录引用")
    else:
        print(f"  [DEL] 文件 {file_id[:8]}... 打印完成后已立即从服务器删除")
    if row is not None:
        _audit_file_access(
            "delete", "ok", file_id=file_id, openid=row["openid"] or "",
            actor="system", ext=_audit_ext(row["original_name"]),
        )
    return True


def build_md5_index():
    """扫描 uploads/ 目录下所有文件，构建 MD5 索引。已有索引则跳过全量重建但会补全缺失条目。"""
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    # 提取所有已索引的相对路径（兼容新旧格式）
    def _indexed_paths(idx):
        paths = set()
        for v in idx.values():
            if isinstance(v, str):
                paths.add(v)
            elif isinstance(v, dict) and v.get("path"):
                paths.add(v["path"])
        return paths

    existing = load_md5_index()
    if existing:
        indexed = _indexed_paths(existing)
        for root, dirs, files in os.walk(UPLOAD_DIR):
            if os.path.basename(root) == "avatars":
                continue
            for fname in files:
                if fname == "md5_index.json":
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, UPLOAD_DIR)
                if rel not in indexed:
                    try:
                        md5 = get_file_md5(fpath)
                        if md5 not in existing:
                            # 新格式：存储字典
                            ext = os.path.splitext(fname)[1].lower().lstrip(".")
                            existing[md5] = {
                                "path": rel, "original_name": fname,
                                "page_count": 0, "page_count_verified": False,
                            }
                            print(f"  [MD5] 补充索引: {md5[:8]}... → {rel}")
                    except Exception as e:
                        print(f"  [MD5] 扫描文件失败 {_rel_display(fpath)}: {type(e).__name__}")
        save_md5_index(existing)
        print(f"  [MD5] 索引已更新，共 {len(existing)} 条记录")
        return

    # 首次构建：全量扫描，使用新格式
    print("  [MD5] 首次构建 MD5 索引...")
    index = {}
    for root, dirs, files in os.walk(UPLOAD_DIR):
        if os.path.basename(root) == "avatars":
            continue
        for fname in files:
            if fname == "md5_index.json":
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, UPLOAD_DIR)
            try:
                md5 = get_file_md5(fpath)
                ext = os.path.splitext(fname)[1].lower().lstrip(".")
                if md5 in index:
                    print(f"  [MD5] 重复文件: {_rel_display(fpath)}")
                index[md5] = {
                    "path": rel, "original_name": fname,
                    "page_count": 0, "page_count_verified": False,
                }
            except Exception as e:
                print(f"  [MD5] 扫描文件失败 {_rel_display(fpath)}: {type(e).__name__}")
    save_md5_index(index)
    print(f"  [MD5] 索引构建完成，共 {len(index)} 条记录")


def load_retention_config():
    """加载保留时间配置，不存在则返回默认值"""
    if not os.path.exists(RETENTION_CONFIG_FILE):
        return dict(DEFAULT_RETENTION)
    try:
        with open(RETENTION_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # 确保必要字段存在
        cfg.setdefault("days", DEFAULT_RETENTION["days"])
        cfg.setdefault("hours", DEFAULT_RETENTION["hours"])
        return cfg
    except (json.JSONDecodeError, IOError):
        return dict(DEFAULT_RETENTION)


def save_retention_config(cfg):
    """保存保留时间配置到文件"""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with open(RETENTION_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def cleanup_expired_files():
    """清理过期文件（删磁盘 + 清空路径）与幽灵文件记录。

    - 过期文件：删磁盘文件、清空 files.path（保留记录，历史/引用完整性）
    - 幽灵记录（path 已清空或物理文件缺失 + 未关联活跃订单）：直接删除 files 记录，
      否则会污染页数分析补推——每次打印机上线重推已清理文件 → 下载 404 刷屏

    跳过仍在活跃订单中引用的文件（reserved/queued/printing/accepted/scheduled/
    waiting/downloading），防止清理掉尚未完成或崩溃后待恢复的打印任务所需文件。
    幽灵清理不受保留期限制（物理已清理的记录无需等待过期）。"""
    cfg = load_retention_config()
    days = cfg.get("days", 0)
    hours = cfg.get("hours", 0)
    no_retention = (days == 0 and hours == 0)

    conn = get_db()

    # 收集所有活跃订单引用的 file_id
    # （P2-3）补全遗漏状态：scheduled（预约待下发）/ waiting（文件就绪等打印）/
    # downloading（文件下载中）——之前漏掉会误删预约单尚未打印所需的文件
    active_statuses = ('queued', 'printing', 'accepted', 'reserved',
                       'scheduled', 'waiting', 'downloading')
    status_placeholders = ",".join("?" for _ in active_statuses)
    active_file_ids = set()
    for (fid,) in conn.execute(
        f"SELECT DISTINCT file_id FROM orders WHERE file_id IS NOT NULL AND status IN ({status_placeholders})",
        active_statuses,
    ).fetchall():
        if fid:
            active_file_ids.add(fid)
    for (fid,) in conn.execute(
        f"SELECT DISTINCT file_id FROM order_files WHERE file_id IS NOT NULL AND status IN ({status_placeholders})",
        active_statuses,
    ).fetchall():
        if fid:
            active_file_ids.add(fid)

    if active_file_ids:
        print(f"  [CLEANUP] 跳过 {len(active_file_ids)} 个活跃订单引用的文件")

    # v4.4 修复：MD5 去重复用会让多个 files 行共享同一物理文件（上传时复用 existing_path）。
    # 收集活跃行引用的全部物理路径——即使某行已过期且非活跃，只要其物理路径仍被其他活跃行
    # 引用，删除时必须跳过；否则排队/打印中订单的文件会被提前删掉（下载 404，如订单 failed）。
    active_paths = set()
    if active_file_ids:
        _ph = ",".join("?" for _ in active_file_ids)
        for (_p,) in conn.execute(
            f"SELECT path FROM files WHERE id IN ({_ph}) AND path != ''", list(active_file_ids)
        ).fetchall():
            if _p:
                active_paths.add(os.path.normpath(_p))

    # S2-05（DS-14）：路径 → 仍引用它的行集合。一次扫描，循环里只做集合判断。
    path_refs = _path_reference_index(conn)

    # ---- 幽灵记录清理（不受保留期限制）----
    ghost_ids = []
    for (fid, fpath) in conn.execute("SELECT id, path FROM files").fetchall():
        if fid in active_file_ids:
            continue
        if not fpath or not os.path.exists(fpath):
            ghost_ids.append(fid)
    if ghost_ids:
        placeholders = ",".join("?" for _ in ghost_ids)
        conn.execute(f"DELETE FROM files WHERE id IN ({placeholders})", ghost_ids)
        conn.commit()
        print(f"  [CLEANUP] 已删除 {len(ghost_ids)} 条幽灵文件记录（物理已清理，防止页数分析补推刷屏）")

    cutoff = datetime.now() - timedelta(days=days, hours=hours)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    # ===== S1-03：客户自选保留期的文件按**各自的** retention_hours 到期 =====
    # 这一段必须在 `no_retention` 早退**之前**：管理员把全局策略设成"永不过期"时，
    # 客户明确选了 24h 的文件仍然必须按时删除（D-6 的口径是"由客户决定删除时间"）。
    per_file_rows = conn.execute(
        """SELECT id, path, retention_hours FROM files
            WHERE path != '' AND COALESCE(retention_hours, 0) > 0
              AND datetime(created_at, '+' || retention_hours || ' hours')
                  <= datetime('now', 'localtime')"""
    ).fetchall()
    # “立即删除”订单若在 print_success 之后清理失败（例如短暂文件占用），定时扫描兜底。
    # 仅命中已经打印成功、且没有活跃订单引用的文件，上传后尚未下单的文件不会被误删。
    immediate_rows = conn.execute(
        """SELECT DISTINCT f.id, f.path, f.retention_hours FROM files f
           WHERE f.path != '' AND COALESCE(f.retention_hours, 0) = ?
             AND EXISTS (
                 SELECT 1 FROM order_files done
                 WHERE done.file_id = f.id AND done.status = 'sent'
             )
             AND NOT EXISTS (
                 SELECT 1 FROM order_files busy
                 WHERE busy.file_id = f.id AND busy.status IN
                     ('queued', 'printing', 'accepted', 'reserved',
                      'scheduled', 'waiting', 'downloading')
             )
             AND NOT EXISTS (
                 SELECT 1 FROM orders busy_order
                 WHERE busy_order.file_id = f.id AND busy_order.status IN
                     ('queued', 'printing', 'accepted', 'reserved',
                      'scheduled', 'waiting', 'downloading')
             )""",
        (IMMEDIATE_DELETE_RETENTION_HOURS,),
    ).fetchall()
    # 0 天 0 小时 = 全局策略永不过期（但上面的"客户自选到期"仍要执行）
    if no_retention:
        rows = []
        if not per_file_rows and not immediate_rows:
            conn.close()
            return
    else:
        rows = conn.execute(
            "SELECT id, path FROM files WHERE created_at < ? AND path != ''",
            (cutoff_str,),
        ).fetchall()
    # 合并去重（同一个文件可能同时命中全局与自选口径）
    _seen_ids = {r["id"] for r in rows}
    for _r in per_file_rows:
        if _r["id"] not in _seen_ids:
            rows = list(rows) + [_r]
            _seen_ids.add(_r["id"])
    for _r in immediate_rows:
        if _r["id"] not in _seen_ids:
            rows = list(rows) + [_r]
            _seen_ids.add(_r["id"])

    md5_index = load_md5_index()
    deleted_count = 0
    skipped_active = 0
    shared_skipped = 0

    for row in rows:
        file_id = row["id"]
        file_path = row["path"]

        # 跳过活跃订单引用的文件
        if file_id in active_file_ids:
            skipped_active += 1
            continue

        # v4.4：共享物理文件保护——本行过期且非活跃，但物理路径仍被其他活跃行引用 → 跳过删除
        if file_path and os.path.normpath(file_path) in active_paths:
            skipped_active += 1
            continue

        # S2-05（DS-14）存量共享文件保护：本行到期，但仍有**别的行**指向同一物理路径
        # （历史 MD5 去重的产物）⇒ 只清自己的这一行、**绝不删盘**：另一方（保留期更长、
        # 还没到期）还要下载，删掉就是"一方到期把另一方文件弄悬空"。等最后一个引用它的
        # 行也到期时，才会走下面的正常删除分支。
        _norm_path = os.path.normpath(file_path) if file_path else ""
        _ref_ids = path_refs.get(_norm_path) if _norm_path else None
        if _ref_ids and len(_ref_ids - {file_id}) > 0:
            conn.execute("UPDATE files SET path = '', size = 0 WHERE id = ?", (file_id,))
            _drop_previews(file_id)
            _ref_ids.discard(file_id)
            deleted_count += 1
            shared_skipped += 1
            continue
        if _ref_ids is not None:
            _ref_ids.discard(file_id)

        # 删除磁盘文件
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError as e:
                print(f"  [CLEANUP] 删除文件失败 {_rel_display(file_path)}: {type(e).__name__}")
                continue

        # 从 MD5 索引中移除（兼容新旧格式）
        rel_path = os.path.relpath(file_path, UPLOAD_DIR) if file_path else None
        if rel_path:
            keys_to_remove = [
                k for k, v in md5_index.items()
                if (isinstance(v, str) and v == rel_path) or
                   (isinstance(v, dict) and v.get("path") == rel_path)
            ]
            for k in keys_to_remove:
                del md5_index[k]

        # 清空 files 表中的路径（保留记录本身，下轮清理时作为幽灵删除）
        conn.execute("UPDATE files SET path = '', size = 0 WHERE id = ?", (file_id,))
        _drop_previews(file_id)
        deleted_count += 1

    conn.commit()
    conn.close()

    if skipped_active > 0:
        print(f"  [CLEANUP] 已跳过 {skipped_active} 个活跃订单引用的过期文件")
    if shared_skipped > 0:
        print(f"  [CLEANUP] {shared_skipped} 个过期行与其它行共享物理文件：只清记录，文件保留给另一方")
    if deleted_count > 0:
        save_md5_index(md5_index)
        print(f"  [CLEANUP] 已清理 {deleted_count} 个过期文件（cutoff={cutoff_str}）")
        # 通知所有在线打印机同步清理本地缓存
        _notify_clients("storage_config_updated", {
            "retention_days": days,
            "retention_hours": hours,
        })

    # 清理 uploads/ 根目录下的孤儿临时文件（上传中断遗留，无 files 表记录）
    import re
    try:
        orphan_deleted = 0
        for fname in os.listdir(UPLOAD_DIR):
            fpath = os.path.join(UPLOAD_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            # 临时文件命名格式: <32位hex uuid>.<ext>
            if not re.match(r'^[0-9a-f]{32}\.\w+$', fname):
                continue
            # 保留至少 1 小时，防止误删正在上传中的文件
            mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
            if (datetime.now() - mtime).total_seconds() < 3600:
                continue
            try:
                os.remove(fpath)
                orphan_deleted += 1
            except OSError:
                pass
        if orphan_deleted > 0:
            print(f"  [CLEANUP] 已清理 {orphan_deleted} 个孤儿临时文件")
    except Exception as e:
        print(f"  [CLEANUP] 清理孤儿临时文件出错: {e}")


# -------- 加载配置（内部默认值 + config.py 覆盖）--------
# 所有敏感信息使用占位符/空值作为内部默认值，
# 部署后通过 config.py 覆盖（config.py 已排除在 git 外）。
WECHAT_APPID = None
WECHAT_APPSECRET = None
SECRET_KEY = "fallback-dev-key-please-change-in-production"
PUBLIC_BASE_URL = "http://127.0.0.1:5000"
ADMIN_OPENIDS = set()
SUPER_ADMIN_OPENID = None
PRINTER_TOKEN = None
# S0-06：token 轮换过渡期的旧值（config.py 可选 TOKEN_PREV，窗口 ≤24h）
PRINTER_TOKEN_PREV = None
PRINTER_NAME = ""

# 订单归属默认占位名（未显式指定归属时使用通用占位，避免把真实用户名硬编码为默认值）
DEFAULT_OWNER_NAME = "张三"

# ===== S1-10（DS-10 / DS-18）：配置由**服务器侧**维护，不进程序目录、不进部署包 =====
# 生产：/etc/hn-printer/config.py（root:root 600）。程序目录里的 config.py 只作为
# 开发机/旧部署的兼容回退。这样"部署时把开发机配置覆盖到生产"在结构上不可能发生
# （部署 tar 也已经 --exclude=config.py，见 更新后端服务.txt）。
_CONFIG_PATHS = [
    os.environ.get("HN_CONFIG_PATH", "/etc/hn-printer/config.py"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py"),
]


def _load_config_module():
    """按顺序找第一个存在的配置文件并作为模块执行；都没有返回 None。"""
    import importlib.util
    for _p in _CONFIG_PATHS:
        if not _p or not os.path.exists(_p):
            continue
        try:
            _spec = importlib.util.spec_from_file_location("hn_printer_config", _p)
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            print(f"已加载配置: {_p}")
            return _mod
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] 配置文件 {_p} 加载失败: {type(e).__name__}: {e}")
    return None


try:
    config = _load_config_module()
    if config is None:
        raise ImportError("no config file")
    WECHAT_APPID = getattr(config, "WECHAT_APPID", WECHAT_APPID)
    WECHAT_APPSECRET = getattr(config, "WECHAT_APPSECRET", WECHAT_APPSECRET)
    SECRET_KEY = getattr(config, "SECRET_KEY", SECRET_KEY)
    PUBLIC_BASE_URL = getattr(config, "PUBLIC_BASE_URL", PUBLIC_BASE_URL)
    PRINTER_TOKEN = getattr(config, "TOKEN", PRINTER_TOKEN)
    PRINTER_TOKEN_PREV = getattr(config, "TOKEN_PREV", PRINTER_TOKEN_PREV)
    PRINTER_NAME = getattr(config, "PRINTER_NAME", PRINTER_NAME)
    ADMIN_OPENIDS = set(getattr(config, "ADMIN_OPENIDS", []))
    SUPER_ADMIN_OPENID = getattr(config, "SUPER_ADMIN_OPENID", SUPER_ADMIN_OPENID)
    if WECHAT_APPID and WECHAT_APPSECRET and SECRET_KEY != "fallback-dev-key-please-change-in-production":
        print("已加载 config.py 微信配置")
    else:
        print("[WARN] config.py 中缺少微信配置项（WECHAT_APPID / WECHAT_APPSECRET / SECRET_KEY），登录功能不可用")
    if ADMIN_OPENIDS:
        print(f"已加载 {len(ADMIN_OPENIDS)} 个管理员 openid")
except ImportError:
    print("[INFO] 未找到 config.py，使用内部默认配置。复制 config.py.example → config.py 后可自定义。")

# 安全策略（P2-7）：生产环境禁止使用内置默认 SECRET_KEY。
# 仅开发模式（FLASK_ENV=development 或命令行带 --debug）才允许默认 key。
_DEV_MODE = os.environ.get("FLASK_ENV") == "development" or "--debug" in " ".join(sys.argv)
if SECRET_KEY == "fallback-dev-key-please-change-in-production" and not _DEV_MODE:
    raise RuntimeError(
        "SECRET_KEY 未配置：生产环境必须在 config.py 中设置 SECRET_KEY，"
        "或设置 FLASK_ENV=development 以开发模式运行"
    )


def is_admin(openid):
    """判断给定 openid 是否为管理员"""
    return openid in ADMIN_OPENIDS


def operator_display_name(openid):
    """人工操作者的**展示名**：优先 users.nickname（微信/APP 昵称），没有昵称才退回角色名。

    2026-10-27 用户要求：设备端日志里显示"操作者：admin"没用（那是角色，不是人）。
    这里只读 nickname，不拼 openid（日志会被窗口展示、也会回传设备端）。
    """
    oid = (openid or "").strip()
    if not oid:
        return ""
    nick = ""
    try:
        conn = get_user_db()
        row = conn.execute("SELECT nickname FROM users WHERE openid = ?", (oid,)).fetchone()
        conn.close()
        nick = (row["nickname"] if row else "") or ""
    except Exception:
        nick = ""
    nick = " ".join(str(nick).split())[:20]
    if nick:
        return nick
    role = compute_role(oid)
    return {"super_admin": "超级管理员", "admin": "管理员"}.get(role, "操作员")


def compute_role(openid):
    """计算用户角色: admin / user / guest（含 DB 中 admin 角色和临时授权）"""
    if is_admin(openid):
        return "admin"
    conn = get_user_db()
    row = conn.execute("SELECT role, temp_until FROM users WHERE openid = ?", (openid,)).fetchone()
    conn.close()
    if row:
        if row["role"] == "admin":
            return "admin"
        if row["role"] == "user":
            return "user"
        # 临时授权：temp_until 未过期视为 user
        # （P2-17）时区说明：temp_until/scheduled_at 等均为本地时间字符串（%Y-%m-%d %H:%M:%S），
        # 依赖服务器系统时区；部署时必须把服务器时区设为中国时区（Asia/Shanghai），
        # 否则预约到点/临时授权过期判定会偏移。不改存储格式（破坏性风险）。
        if row["temp_until"]:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if row["temp_until"] > now_str:
                return "user"
    return "guest"


def get_avatar_url(openid, avatar_path):
    """根据 avatar_path 生成头像 URL，用文件 mtime 做缓存破坏参数"""
    if not avatar_path or not os.path.exists(avatar_path):
        return ""
    mtime = int(os.path.getmtime(avatar_path))
    return f"{PUBLIC_BASE_URL}/api/avatar?openid={openid}&v={mtime}"


_AVATAR_MAX_SIZE = 2 * 1024 * 1024  # 头像大小上限 2MB（P0-2）


def _is_valid_avatar(stream):
    """校验头像文件魔数（P0-2）：仅允许 jpeg/png/webp/gif 头，读前 8 字节。
    读取后把流位置复位到 0，供后续 save 使用。"""
    head = stream.read(8)
    stream.seek(0)
    if not head:
        return False
    if head.startswith(b"\xff\xd8\xff"):            # JPEG
        return True
    if head.startswith(b"\x89PNG\r\n\x1a\n"):       # PNG
        return True
    if head[:4] == b"RIFF" and head[4:8] == b"WEBP":  # WebP (RIFF....WEBP)
        return True
    if head.startswith(b"GIF8"):                    # GIF87a / GIF89a
        return True
    return False


def compress_avatar(file_path, max_size=200):
    """压缩头像：缩放到 max_size×max_size 以内，转 JPEG 质量 80。
    返回压缩后的文件路径（可能不同于原路径，因为扩展名变为 .jpg）；
    无法处理（损坏/解压炸弹/非图片）时返回 None，由调用方删除已保存文件并拒绝（P0-2）。"""
    if not HAS_PIL:
        return file_path  # 没有 Pillow 就原样存储

    try:
        img = Image.open(file_path)
        img.load()  # 提前完整解码，尽早暴露损坏文件/解压炸弹
        # 统一为 RGB（去掉透明通道）
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        # 等比缩放到 max_size 以内
        img.thumbnail((max_size, max_size), Image.LANCZOS)
        # 输出为 JPEG
        jpg_path = os.path.splitext(file_path)[0] + '.jpg'
        img.save(jpg_path, 'JPEG', quality=80, optimize=True)
        # 如果原文件与压缩文件不同，删除原文件
        if jpg_path != file_path and os.path.exists(file_path):
            os.remove(file_path)
        return jpg_path
    except Exception as e:
        print(f"[WARN] 头像压缩失败: {e}")
        return None


# Token 签名器（依赖 SECRET_KEY，必须在配置加载之后初始化）
app.config["SECRET_KEY"] = SECRET_KEY
TOKEN_MAX_AGE = 7 * 24 * 3600  # token 有效期 7 天
# S0-07（D-3 已拍板：接受一次全员重登）：登录令牌与下载令牌**各用独立 salt**，
# 两个 serializer 不再可互换（此前同 key 同 salt ⇒ download.loads(login_token) 能成功，
# 任何一个令牌被拿到就等于拿到另一个域的权限）。
# ⚠ 换 salt 会一次性作废全部 7 天登录态与 1 小时内的旧下载 URL —— 这是必须公告的上线事件。
token_serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="login-v2")

# 文件下载签名器（短时效，供打印机客户端下载文件用）
download_serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="dl-v1")

# -------- 打印机客户端状态 --------
# { client_id: {"sid": socketio_sid, "heartbeat": datetime, "connected_at": datetime} }
printer_clients = {}
printer_clients_lock = threading.Lock()

# -------- 攒批模式（多任务合并翻纸）远程状态（2026-10）--------
# { client_id: {"mode": True/False/None, "busy": bool, "note": str, "updated_at": datetime} }
# 与 auto_duplex 能力同源：**以设备端为准**。本地工具连上后 + 状态每次变化时用 `stack_state`
# 事件上报，服务端只做镜像（给移动端展示）+ 转发控制指令（移动端 → 设备端），并把设备端的
# 执行结果回给移动端。mode=None = 从未上报（旧版客户端/刚接入）→ 移动端显示未知、不可远程切换。
printer_stack_state = {}
printer_stack_state_lock = threading.Lock()
# 在途的远程控制请求：{ client_id: threading.Event }；设备端回报 stack_state 时唤醒等待方
printer_stack_waiters = {}
printer_stack_results = {}
STACK_TOGGLE_WAIT_SEC = 3.0      # 移动端下发指令后等设备端回报的上限（秒）


# -------- 云端 Word 转换引擎（2026-10-26 晚定稿：隐式规则，没有开关/镜像）--------
# 云端 .doc/.docx 默认强制 LibreOffice（S1-04，不执行宏）。**唯一例外**：该子任务是
# 「管理员自打」（orders.is_admin_print）**且**文件由管理员上传（files/orders 的 openid
# 属于 admin / super_admin）⇒ 设备端允许 Word/WPS COM（仍带宏/弹窗/外链硬化）。
# 用户口径：「管理员直接默认不强制使用 LO 即可，普通用户必须强制使用 LO」——
# 判定本体在设备端 `printer_config.cloud_word_com_allowed()`，服务端只负责如实下发
# `is_admin_print` + `uploader_is_admin` 两个位；不再有策略镜像 / 远程切换端点。
# ⚠ 光看 is_admin_print 不够：它是可变的业务列（人工类型订正 / 机位勾选 / print_success
#   都会改写），顾客上传的文件一旦被订正成"自打"，就会把顾客内容送进 COM。内容归属只看
#   上传者 ⇒ 第二道位 uploader_is_admin 必须同时成立。


def is_admin_uploader(openid) -> bool:
    """该 openid 是不是「管理员」（admin 或 super_admin）—— uploader_is_admin 的唯一算法。

    与 `compute_role()` 的区别只有一条：**super_admin 也算**（它有自己的 openid 配置项，
    不保证同时出现在 ADMIN_OPENIDS 里；漏掉会让超级管理员的自打单永远退回强制 LO）。
    """
    if not openid:
        return False
    return compute_role(openid) == "admin" or bool(
        SUPER_ADMIN_OPENID and openid == SUPER_ADMIN_OPENID)


def _stack_state_of(client_id: str) -> dict:
    """读某设备的攒批镜像状态（从未上报时 mode=None）。"""
    with printer_stack_state_lock:
        st = printer_stack_state.get(client_id) or {}
    return {"mode": st.get("mode"), "busy": bool(st.get("busy")),
            "note": st.get("note") or "", "updated_at": st.get("updated_at")}


def _set_stack_state(client_id: str, mode, busy: bool, note: str = ""):
    """写攒批镜像状态（mode = True / False / None）。"""
    with printer_stack_state_lock:
        printer_stack_state[client_id] = {
            "mode": mode, "busy": bool(busy), "note": (note or "")[:120],
            "updated_at": datetime.now(),
        }


def _stack_needs_manual_flip(client_id: str) -> bool:
    """该设备的机型**是否需要攒批**：没有自动双面器、双面必须人工翻纸才有意义。

    自动双面的机器一次成型，多任务合并翻纸纯属多余 ⇒ 这类设备不提供攒批入口、也不接受远程
    开启（2026-10 用户要求）。只认「已确认不支持自动双面」（auto_duplex is False）；支持(true)
    与未确认(null) 都算"不需要攒批" —— 这不是拿能力去拦单（那种拦单已按要求全面放开，见
    tests/verify_duplex_capability.py ⑥），只是不给攒批这个功能。

    ⚠ 别与当年用于 409 拦单的那个同名旧函数（已按要求移除）混为一谈，那个不许再加回来；
    本函数只服务攒批模式的准入。注意：源码里**不要**再出现那三个旧标识符 —— 验收脚本
    tests/verify_duplex_capability.py ⑥ 是按字符串钉住"拦截确已移除"的。
    """
    return get_device_capability(client_id).get("auto_duplex") is False


def _stack_toggle_problem(client_id: str, enable=None) -> str:
    """远程切换攒批模式的前置校验：返回 "" = 允许；非空 = 给用户看的原因。

    enable=None 时只判"状态本身可不可操作"（供 /api/claiming_devices 算 stack_can_toggle）。
    前提（与本地工具侧同口径）：
      · 该机型**确实需要攒批**（不支持自动双面、双面需人工翻纸）—— 自动双面/未确认的设备
        在设备列表里根本不渲染攒批开关（2026-10 用户要求，见 _stack_needs_manual_flip）；
      · **空闲** + 状态与目标相反 —— 忙（正在打印 / 等待人工翻纸打第二趟 / 有排队待打的请求）
        时一律不许改动攒批模式。
    """
    if not _stack_needs_manual_flip(client_id):
        return ("该设备支持自动双面（或双面能力未确认），不需要攒批模式 —— "
                "攒批只对「无自动双面器、需人工翻纸」的机型有意义")
    st = _stack_state_of(client_id)
    if st["mode"] is None:
        return "该设备尚未上报攒批状态（旧版客户端或刚接入），暂不能远程切换"
    if st["busy"]:
        return ("打印机正忙（正在打印 / 等待人工翻纸打第二趟 / 有排队待打的请求），"
                "空闲后才能改动攒批模式")
    if enable is True and st["mode"]:
        return "攒批模式已经是开启状态"
    if enable is False and not st["mode"]:
        return "攒批模式已经是关闭状态"
    return ""


# -------- 攒批模式「期望状态」（2026-11：离线也能先记下来，设备上线自动应用）--------
# 现场口径：设备离线时移动端点攒批开关，原先直接拒绝；现在改成"记下期望，等它上线照办"。
# 存 uploads/stack_intent.json：{client_id: {"enable": bool, "by": str, "by_role": str,
#   "ts": "YYYY-MM-DD HH:MM:SS", "applied": bool}}；启动载入内存副本，写盘原子（临时文件 + os.replace）。
# 语义：**期望**（人想让它变成什么），不是实际状态 —— 实际状态永远以设备端 `stack_state` 上报为准
#   （printer_stack_state 只是镜像）。设备上线后第一次上报时比对：一致 → 清期望不下发；
#   不一致 → 下发 set_stack_mode（reason=offline_intent）；设备忙/拒绝 → 保留期望，下次上线再试。
STACK_INTENT_FILE = os.path.join(UPLOAD_DIR, "stack_intent.json")
_stack_intent_lock = threading.RLock()
_stack_intent = {}
# 下发去重 + 重试冷却：{client_id: {"enable": bool, "at": datetime, "count": int}}
# 用来打断「设备拒绝 → 再回报 stack_state → 又比对不一致 → 再下发」这条死循环。
# 2026-11 口径变更：不再是「每次上线最多试一次」（那样设备端升级前/偶发拒绝就永远不重试），
# 改成**可重试**：期望仍在、设备空闲、仍不一致时，距上次下发超过
# STACK_INTENT_RETRY_COOLDOWN_SEC 秒就再发一次；设备接受（mode == 期望）即清期望。
# 设备重连（on_connect）时清空该设备的记录 ⇒ 上线立刻可以再试一次（不等冷却）。
_stack_intent_dispatched = {}
# 两次自动下发之间的最小间隔（秒）：防抖（设备反复回报 state 时不至于刷屏）+ 给设备端留
# 出「拒绝并回报原因」的时间窗。设备端每次拒绝后仍保留期望，只是要等冷却结束再试。
STACK_INTENT_RETRY_COOLDOWN_SEC = 20


def load_stack_intent():
    """加载攒批期望状态（缺失/损坏 → 空；逐条清洗，脏条目直接丢掉而不是让整个文件失效）。"""
    global _stack_intent
    data = {}
    if os.path.exists(STACK_INTENT_FILE):
        try:
            with open(STACK_INTENT_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError):
            raw = {}
        if isinstance(raw, dict):
            for cid, ent in raw.items():
                if (not isinstance(cid, str) or not cid or len(cid) > 64
                        or not isinstance(ent, dict)):
                    continue
                enable = ent.get("enable")
                if not isinstance(enable, bool):
                    continue
                data[cid] = {
                    "enable": enable,
                    "by": str(ent.get("by") or "")[:40],
                    "by_role": str(ent.get("by_role") or "")[:20],
                    "ts": str(ent.get("ts") or ""),
                    "applied": bool(ent.get("applied")),
                }
    with _stack_intent_lock:
        _stack_intent = data
        _stack_intent_dispatched.clear()
    if data:
        print(f"[stack_intent] 已载入攒批期望 {len(data)} 条: {sorted(data)}")
    return {k: dict(v) for k, v in data.items()}


def _save_stack_intent_locked():
    """把内存里的期望状态原子落盘（调用方必须持有 _stack_intent_lock）。"""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    tmp = STACK_INTENT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_stack_intent, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STACK_INTENT_FILE)


def _stack_intent_of(client_id: str):
    """读某设备的攒批期望（没有 → None）。"""
    with _stack_intent_lock:
        ent = _stack_intent.get(client_id)
        return dict(ent) if ent else None


def _stack_intent_pending(client_id: str) -> bool:
    """该设备是否有「待应用的攒批期望」（设备列表 / printer_state 下发用）。"""
    with _stack_intent_lock:
        return client_id in _stack_intent


def _stack_desired_of(client_id: str):
    """该设备攒批的**期望**值：True / False / None（= 没有人留下未落地的期望）。

    与 stack_pending 的分工：pending 只回答"有没有期望"，desired 回答"期望是什么"。
    设备列表 / printer_state 两个都下发，前端据此区分「期望开启但设备还没照办」
    （desired=True + pending=True + mode=False）与「期望关闭」这两种完全不同的高亮。
    """
    ent = _stack_intent_of(client_id)
    if not ent:
        return None
    return bool(ent.get("enable"))


def _set_stack_intent(client_id, enable, by="", by_role="", applied=False) -> bool:
    """记录/覆盖某设备的攒批期望并落盘；返回是否写盘成功（内存副本仍会更新）。"""
    with _stack_intent_lock:
        _stack_intent[client_id] = {
            "enable": bool(enable),
            "by": (by or "")[:40],
            "by_role": (by_role or "")[:20],
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "applied": bool(applied),
        }
        try:
            _save_stack_intent_locked()
        except OSError as e:
            print(f"[stack_intent] {client_id} 期望落盘失败（内存已更新）: {e}")
            return False
    print(f"[stack_intent] {client_id} 记录期望: {'开启' if enable else '关闭'}"
          f"（by={by or '-'} / {by_role or '-'}）")
    return True


def _clear_stack_intent(client_id, why="") -> bool:
    """清掉某设备的攒批期望（已应用/已一致/被在线指令取代）。"""
    with _stack_intent_lock:
        ent = _stack_intent.pop(client_id, None)
        if ent is None:
            return False
        _stack_intent_dispatched.pop(client_id, None)
        try:
            _save_stack_intent_locked()
        except OSError as e:
            print(f"[stack_intent] {client_id} 清除期望落盘失败: {e}")
    print(f"[stack_intent] {client_id} 清除攒批期望" + (f"（{why}）" if why else ""))
    return True


def _apply_stack_intent_on_online(client_id: str, sid: str = "") -> None:
    """设备上线后拿到**新上报**的 stack_state 时，比对「期望 vs 实际」并落地。

    为什么要等上报（而不是在 on_connect 里直接发）：连接瞬间镜像里是上次会话的旧值，
    拿旧值比对会「一致就误清期望」或「不一致就白发一条」。`stack_state` 是设备刚上报的
    真实状态，比它可靠。

    2026-11（可重试，取代旧的「每次上线最多一次」）：
      · 实际 == 期望 → 清期望（已达成，没别的可做）；
      · 设备忙 → **期望一律保留、绝不清掉**；本会话第一次比对仍下发一次（让设备自己裁决：
        设备端的 toggle 闸门会拒并回 note，服务器不自作主张替设备判死），之后靠冷却不重发；
      · 距上次下发 < STACK_INTENT_RETRY_COOLDOWN_SEC → 先不发（防抖，等设备端把上一条
        处理完并回报）；冷却过后仍不一致 → 再发，直到设备接受或人改主意；
      · 后写覆盖先写：期望变了（dispatched 里的 enable 与当前期望不同）⇒ 立刻按新期望发，
        不受冷却限制（人的新指令优先）。
    """
    ent = _stack_intent_of(client_id)
    if not ent:
        return
    st = _stack_state_of(client_id)
    want = bool(ent["enable"])
    if st["mode"] is want:
        _clear_stack_intent(client_id, f"上线后实际状态已一致：{'开启' if want else '关闭'}")
        return
    if not sid:
        print(f"[stack_intent] {client_id} 有期望但当前无在线会话，暂不下发（下次上线再试）")
        return
    with _stack_intent_lock:
        rec = dict(_stack_intent_dispatched.get(client_id) or {})
    if rec and rec.get("enable") is want:
        at = rec.get("at")
        age = (datetime.now() - at).total_seconds() if isinstance(at, datetime) else 1e9
        if age < STACK_INTENT_RETRY_COOLDOWN_SEC:
            print(f"[stack_intent] {client_id} 期望仍为{'开启' if want else '关闭'}，"
                  f"距上次下发 {age:.0f}s（冷却 {STACK_INTENT_RETRY_COOLDOWN_SEC}s）"
                  f"→ 本轮不重发，保留期望"
                  + ("（设备正忙，期望保留到空闲）" if st["busy"] else ""))
            return
    if st["busy"]:
        print(f"[stack_intent] {client_id} 期望{'开启' if want else '关闭'}待应用，"
              f"设备正忙 → 先下发一次由设备裁决，期望保留到设备接受")
    request_id = uuid.uuid4().hex[:8]
    try:
        socketio.emit("set_stack_mode",
                      {"enable": want,
                       "by": ent["by"] or "系统",
                       "by_role": ent["by_role"] or "",
                       "reason": "offline_intent",
                       "request_id": request_id},
                      to=sid)
    except Exception as e:
        print(f"[stack_intent] {client_id} 下发离线期望失败: {e}")
        return
    with _stack_intent_lock:
        _stack_intent_dispatched[client_id] = {
            "enable": want,
            "at": datetime.now(),
            "count": int(rec.get("count") or 0) + 1 if rec.get("enable") is want else 1,
        }
    actual = "开启" if st["mode"] else ("关闭" if st["mode"] is False else "未知")
    print(f"[stack_intent] {client_id} 上线校验不一致（期望{'开启' if want else '关闭'}，"
          f"实际{actual}）→ 已下发 set_stack_mode"
          f"（reason=offline_intent, request_id={request_id}）")


# 模块加载即读取已保存的期望（若无则空字典，不打印噪音）
_stack_intent = load_stack_intent()


# 推送后等待反馈的任务: { order_id: {"pushed_at": datetime, "client_id": str} }

pushed_tasks = {}
pushed_tasks_lock = threading.Lock()

CLIENT_HEARTBEAT_TIMEOUT = 90    # 心跳超时秒数（超过此值视为离线）
PRINT_FEEDBACK_TIMEOUT = 180     # 打印反馈超时秒数（3 分钟，断线回滚兜底）
ACCEPT_WAIT_TIMEOUT = 600        # 推送后未被 accept 的任务（打印机端等待用户确认）：超过该时长停止计时，但不判失败（任务保持等待，由孤儿回收+重推兜底）
# 断线未知超时秒数（30 分钟）：客户端断线后任务置 offline_unknown，等待原客户端重连回报；
# 超时仍未解决 → 保守标记 failed（方案 B：不重复打印，用户可手动重发）
OFFLINE_UNKNOWN_TIMEOUT = 1800
# 预约单「下载不动」的判定（🔴14）：push 时写过 locked_at，但客户端心跳还在、进度早就不动。
# 设备连着网却永远传不完时，子任务会挂在 downloading 上——既不打印也不失败、零日志。
# 30 分钟没刷新 locked_at ⇒ 回退 scheduled 重下（重下是幂等的，不会重复出纸）。
DOWNLOAD_STALL_TIMEOUT = 1800
# 预约单「冻结」的自愈上限（🔴13）：文件迟迟没到齐 ⇒ schedule_frozen=1、scheduled_at=''，
# 而唯一的解冻点是 file_ready；该事件可能永远不再来（设备掉线/上传失败），订单就永久冻结。
# 冻结超过该时长：全就绪就解冻重排，否则把没就绪的那几份标 timeout，让订单有结论。
FROZEN_STUCK_TIMEOUT = 1800

def get_active_clients():
    """返回心跳未超时的客户端 ID 列表"""
    now = datetime.now()
    active = []
    with printer_clients_lock:
        for cid, info in list(printer_clients.items()):
            if (now - info["heartbeat"]).total_seconds() < CLIENT_HEARTBEAT_TIMEOUT:
                active.append(cid)
            else:
                del printer_clients[cid]  # 清理超时客户端
    return active


def touch_printer_heartbeat(client_id, sid=None):
    """刷新设备心跳 —— HTTP 通道同样保活，让「双通道」真正互为备份。

    设备「在线」只看 printer_clients 里的 heartbeat（CLIENT_HEARTBEAT_TIMEOUT=90s），
    而过去只有 socketio 的 connect/ping 会写它。socket 掉线但网络正常时（本机代理、
    握手超时、服务端重建连接），HTTP 拉单本身是通的，设备却已被判离线 →
    /api/pull_queued_orders 里的 get_active_printer_client() 返回 None，永远拉不到任务，
    订单只能干等客户端重连。

    sid=None 表示该设备此刻没有 socket 连接：所有 emit 路径都有 `if sid` 守卫，
    push_print_task_to_client 取不到 sid 会把子任务回滚成 queued（[WAIT] 日志）等下一次
    HTTP 拉取，不会误广播、也不会把任务卡在 printing。"""
    now = datetime.now()
    with printer_clients_lock:
        info = printer_clients.get(client_id)
        if info is None:
            printer_clients[client_id] = {
                "sid": sid,
                "heartbeat": now,
                "connected_at": now,
            }
        else:
            info["heartbeat"] = now
            if sid:                       # 别用 None 覆盖已存在的真实 sid
                info["sid"] = sid


# ==================== 打印机设备注册表 + 接单接管（多设备唯一接管者） ====================
# 需求背景：多台设备可同时连接同一台服务器，但只有「启用接单」的那一台能接收订单。
#   · devices.json 持久化每台设备的计算机名 / 所有者 / 首末次在线时间（跨重启保留）；
#   · printer_claim.json 持久化当前「接单」设备（谁启用了接单），重启后端不丢失。
_devices_lock = threading.Lock()
_claim_lock = threading.Lock()

# ==================== 小 JSON 文件的进程内读取缓存（S2-01）====================
# 背景：devices.json / printer_claim.json / pricing.json 都是"每个请求都读一遍再 json.loads"
# 的小文件，而命中它们的路径全是高频的 —— 心跳 / printer_state 广播（每次广播遍历全部设备）、
# 设备列表、接单状态、**每一笔计价**。文件不大，但磁盘 IO + 解析被放大成每秒数百次，
# 且都发生在持有业务锁的调用方附近。
#
# 判据：文件内容的"指纹" = (st_mtime_ns, st_size, st_ino)。带 inode 是刻意的 ——
# 本仓所有写盘都走"临时文件 + os.replace"，换的是 inode，所以哪怕 mtime 精度粗、
# 同一秒内改两次，也不会被误判成"没变"。
_devices_cache = {"sig": None, "data": None}
_claim_cache = {"sig": None, "data": None}


def _json_file_sig(path):
    """文件指纹；不存在/不可读返回 None。"""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (st.st_mtime_ns, st.st_size, getattr(st, "st_ino", 0))


def _cached_json_read(path, cache, lock=None):
    """读取小 JSON 文件，命中缓存则直接返回**深拷贝**（调用方可随意改写返回值）。

    lock=None 表示调用方已经在更粗的锁内（`_load_claim_impl` 的既有契约就是"无锁实现"，
    它的调用方都持 `_claim_lock`；在那里再取同一把非重入 Lock 会自锁）。
    文件缺失或损坏返回 None，由调用方决定缺省值。
    """
    sig = _json_file_sig(path)
    if sig is None:
        return None
    if lock is None:
        if cache.get("sig") == sig and cache.get("data") is not None:
            return copy.deepcopy(cache["data"])
    else:
        with lock:
            if cache.get("sig") == sig and cache.get("data") is not None:
                return copy.deepcopy(cache["data"])
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if lock is None:
        cache["sig"], cache["data"] = sig, data
    else:
        with lock:
            cache["sig"], cache["data"] = sig, data
    return copy.deepcopy(data)


def load_devices() -> dict:
    """读取设备注册表；文件缺失/损坏返回空 dict。"""
    data = _cached_json_read(DEVICES_FILE, _devices_cache, _devices_lock)
    return data if isinstance(data, dict) else {}


def save_devices(data: dict) -> None:
    """原子写入设备注册表。"""
    with _devices_lock:
        tmp = DEVICES_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, DEVICES_FILE)
            # 写回后立刻刷新缓存（省掉下一次 stat+解析，也保证紧随其后的读一定看到新值）
            _devices_cache["sig"] = _json_file_sig(DEVICES_FILE)
            _devices_cache["data"] = copy.deepcopy(data)
        except OSError as e:
            print(f"  [DEV] 保存设备注册表失败: {e}")


def register_device(client_id: str, device_name: str = "", printer: dict | None = None) -> None:
    """登记设备上线（连接/心跳/领取时调用）：记录计算机名与在线时间。

    printer：可选的能力上报（见 _printer_probe_from_request / _merge_printer_probe），
    只写探测字段、保留人工盖章字段；不传则完全不动已有能力记录。"""
    if not client_id:
        return
    devices = load_devices()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = devices.get(client_id) or {}
    entry["device_name"] = (device_name or entry.get("device_name", "") or client_id)[:64]
    entry["first_seen"] = entry.get("first_seen", now_str)
    entry["last_seen"] = now_str
    if printer:
        entry["printer"] = _merge_printer_probe(entry.get("printer"), printer)
    devices[client_id] = entry
    save_devices(devices)


def get_device_entry(client_id: str) -> dict:
    """读取单台设备的注册信息（不存在返回空 dict）。"""
    if not client_id:
        return {}
    return load_devices().get(client_id) or {}


def get_device_owner(client_id: str) -> str:
    """读取设备绑定的所有者姓名（未绑定返回空串）。"""
    return (get_device_entry(client_id) or {}).get("owner_name", "") or ""


# ── 打印机能力：自动双面（2026-10）──────────────────────────────────────────
# 需求背景：现场有「不支持自动双面、只有手动双面」的旧机型（HP LaserJet 1018/1020/1022），
# 它们靠驱动弹「双面打印说明」框指挥用户翻纸，机器本身没有双面器。程序过去一律假设
# 「打印机支持自动双面」，在这种机器上会**静默打成单面却报成功**（客户付双面的钱、店里
# 出双倍纸）。所以能力必须显式收集、随设备上报、并让下游（移动端禁用无障碍打印）能看到。
#
# 判定分两层，且**人工盖章优先、不可被自动探测覆盖**：
#   · probe_*  —— 本地打印工具探测上报（DeviceCapabilities(DC_DUPLEX) + DEVMODE 合并回读
#                 是否被驱动改回 SIMPLEX），下一次探测可以刷新它；
#   · manual_* —— 人工确认的结论（授权页「自动双面」下拉 / 本地工具勾选），只有再人工改
#                 才会变。**刻意不让探测覆盖人工**：这类 HP 驱动会把「手动双面」也算进双面
#                 能力（DC_DUPLEX 可能报 1），"有没有双面器"这个事实只有人能盖章确认。
# 解析优先级 manual > probe > unknown（不设"设备名/驱动名预设库"作为判据：队列名用户可改、
# 驱动名随版本变，预设只能当提示，放在本地工具侧，不进服务端判定链路）。

def _as_bool_or_none(value):
    """归一化三态布尔：True/False/None（未知）。接受 bool / 0/1 / "1"/"0"/"true"/"false"/""。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None


def _mutate_device(client_id: str, fn) -> bool:
    """对单台设备做一次读-改-写（设备未登记返回 False）。fn(entry, now_str) 就地修改 entry。"""
    if not client_id:
        return False
    devices = load_devices()
    entry = devices.get(client_id)
    if not isinstance(entry, dict):
        return False
    fn(entry, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    devices[client_id] = entry
    save_devices(devices)
    return True


def _merge_printer_probe(current: dict | None, incoming: dict) -> dict:
    """把一次自动探测上报并入 printer 子对象。

    **只写探测字段（queue/driver/port/probe_*），manual_* 人工字段原样保留** —— 这是
    「人工盖章不可被探测覆盖」这条规则的实现处。incoming 里没有 "auto_duplex" 键时
    不动探测结论（只补打印机名等描述信息）。"""
    p = dict(current or {})
    for key in ("queue", "driver", "port"):
        val = str(incoming.get(key) or "").strip()[:120]
        if val:
            p[key] = val
    if "auto_duplex" in incoming:
        p["probe_auto_duplex"] = _as_bool_or_none(incoming.get("auto_duplex"))
        p["probe_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return p


def set_device_probe(client_id: str, incoming: dict) -> bool:
    """写入自动探测结果（本地工具上报；不触碰人工盖章字段）。"""
    return _mutate_device(
        client_id,
        lambda entry, _now: entry.__setitem__(
            "printer", _merge_printer_probe(entry.get("printer"), incoming)),
    )


def set_device_manual_capability(client_id: str, auto_duplex, by: str = "") -> bool:
    """人工确认设备能否自动双面（True/False；None = 清除人工结论、回到自动探测结果）。"""
    def _mut(entry: dict, now_str: str):
        p = dict(entry.get("printer") or {})
        verdict = _as_bool_or_none(auto_duplex)
        if verdict is None:
            for key in ("manual_auto_duplex", "manual_by", "manual_at"):
                p.pop(key, None)
        else:
            p["manual_auto_duplex"] = verdict
            p["manual_by"] = str(by or "").strip()[:30]
            p["manual_at"] = now_str
        entry["printer"] = p

    return _mutate_device(client_id, _mut)


def get_device_capability(client_id: str) -> dict:
    """解析一台设备的自动双面能力（人工 > 探测 > 未知）。

    Returns:
        {
            "auto_duplex": True|False|None,   # None = 未确认
            "source": "manual"|"probe"|"unknown",
                manual  = 人工盖章的结论；
                probe   = 结论来自自动探测（含"探测过但没得出 True/False"这种情况，
                          此时 auto_duplex 仍为 None——UI 可据此显示"探测过、未确认"）；
                unknown = 既没人工盖章、也没探测上报过（老客户端）。
            "printer_name"/"printer_driver"/"printer_port": str,   # 本地工具上报的描述信息
            "verified_by"/"verified_at": str, # 人工确认者与时间
            "probe_auto_duplex"/"probe_at": ...,  # 原始探测值（UI 可用于显示"探测与人工不一致"）
        }
    """
    printer = (get_device_entry(client_id) or {}).get("printer") or {}
    manual = printer.get("manual_auto_duplex")
    manual = manual if isinstance(manual, bool) else None
    probe = printer.get("probe_auto_duplex")
    probe = probe if isinstance(probe, bool) else None
    # probe_at 只在"上报里带了 auto_duplex 键"时才写 → 它存在就说明探测过（哪怕结论是未确认）
    probed = bool(printer.get("probe_at")) or probe is not None
    if manual is not None:
        auto, source = manual, "manual"
    elif probe is not None:
        auto, source = probe, "probe"
    elif probed:
        auto, source = None, "probe"
    else:
        auto, source = None, "unknown"
    return {
        "auto_duplex": auto,
        "source": source,
        "printer_name": printer.get("queue", "") or "",
        "printer_driver": printer.get("driver", "") or "",
        "printer_port": printer.get("port", "") or "",
        "verified_by": printer.get("manual_by", "") or "",
        "verified_at": printer.get("manual_at", "") or "",
        "probe_auto_duplex": probe,
        "probe_at": printer.get("probe_at", "") or "",
    }


def _device_capability_fields(client_id: str) -> dict:
    """设备相关的各类 payload 共用的能力字段（列表接口 / 接单状态都用同一套键名）。"""
    cap = get_device_capability(client_id)
    return {
        "auto_duplex": cap["auto_duplex"],           # true / false / null(未确认)
        "capability_source": cap["source"],          # manual / probe / unknown
        "printer_name": cap["printer_name"],
        "capability_verified_by": cap["verified_by"],
        "capability_verified_at": cap["verified_at"],
        "capability_probe": cap["probe_auto_duplex"],
    }


def _printer_probe_from_request() -> dict | None:
    """从连接/心跳请求的 query 里取本地工具上报的打印机能力。

    参数名：printer_name（打印队列）/ printer_driver / printer_port / auto_duplex。
    **老版本地工具不带这些参数 → 返回 None**，注册表里的能力保持原状（解析为 unknown），
    不会因为一次旧客户端连接就把已确认的能力抹掉。"""
    args = request.args
    if not any(k in args for k in ("auto_duplex", "printer_name", "printer_driver", "printer_port")):
        return None
    probe: dict = {}
    if "auto_duplex" in args:
        probe["auto_duplex"] = args.get("auto_duplex")
    for key, arg in (("queue", "printer_name"),
                     ("driver", "printer_driver"),
                     ("port", "printer_port")):
        val = (args.get(arg) or "").strip()
        if val:
            probe[key] = val
    return probe or None


def _load_claim_impl() -> dict:
    """读取接单设备集合（无锁实现，供持锁调用方在 _claim_lock 内使用）。
    新格式: {"claiming_devices": {client_id: {"claimed_at": ..., "owner_name": ...}}}
    兼容旧格式: {"active_client_id": "xxx", ...} → 读取时自动归一化为 claiming_devices。
    读取走进程内缓存（S2-01）；缓存本身不加锁 —— 本函数的所有调用方要么已持
    `_claim_lock`，要么是只读展示路径，缓存写入是单条赋值（CPython 下原子）。"""
    data = _cached_json_read(CLAIM_FILE, _claim_cache, None)
    if not isinstance(data, dict):
        return {}
    # 旧格式（唯一接管者）→ 新格式（多设备集合）
    if "claiming_devices" not in data:
        old = data.get("active_client_id", "")
        devices = {}
        if old:
            devices[old] = {
                "claimed_at": data.get("claimed_at", ""),
                "owner_name": data.get("owner_name", ""),
            }
        return {"claiming_devices": devices}
    devices = data.get("claiming_devices")
    if not isinstance(devices, dict):
        devices = {}
    return {"claiming_devices": devices}


def load_claim() -> dict:
    """读取接单接管状态。"""
    with _claim_lock:
        return _load_claim_impl()


def _save_claim_impl(data: dict) -> None:
    """原子写入接单接管状态（无锁实现，供持锁调用方在 _claim_lock 内使用）。"""
    tmp = CLAIM_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CLAIM_FILE)
        # 写回后刷新缓存（S2-01；与 save_devices 同一口径）
        _claim_cache["sig"] = _json_file_sig(CLAIM_FILE)
        _claim_cache["data"] = copy.deepcopy(data)
    except OSError as e:
        print(f"  [CLAIM] 保存接单状态失败: {e}")


def save_claim(data: dict) -> None:
    """原子写入接单接管状态。"""
    with _claim_lock:
        _save_claim_impl(data)


def get_claiming_devices() -> dict:
    """返回所有启用接单的设备集合 {client_id: {"claimed_at", "owner_name"}}。"""
    claim = load_claim()
    devices = claim.get("claiming_devices") or {}
    return devices


def get_claiming_device_ids() -> list:
    """返回所有启用接单的设备 client_id 列表。"""
    return list(get_claiming_devices().keys())


def is_claiming(client_id: str) -> bool:
    """指定设备是否已启用接单。"""
    return bool(client_id) and client_id in get_claiming_devices()


def get_active_printer_client():
    """返回当前「启用接单」且在线的打印机 client_id（取第一个在线接单设备）；无则 None。
    多设备接单模式下仅作兼容用途（页数分析/预约兜底等需要「任一接单设备」的场景）；
    指定设备分发请用 get_claiming_devices + is_claiming。"""
    online = set(get_active_clients())
    for cid in get_claiming_device_ids():
        if cid in online:
            return cid
    return None


def is_printer_available() -> bool:
    """是否有「启用接单」的打印机（含离线设备——离线时任务排队等其上线，提交仍被接受）。"""
    return len(get_claiming_device_ids()) > 0


# ── 接单设备置顶顺序（2026-10）──────────────────────────────────────────────
# 需求：设备列表原先按「在线优先」排；用户可以把某台设备**置顶**，置顶后它成为默认选中的
# 设备（不管在不在线），多台置顶时还能再排优先级。顺序存服务端、全局共享（小程序与 APP
# 同一份），只有 admin / super_admin 能改。
#
# 为什么单独一个文件而不是塞进 devices.json：顺序是一份**有序列表**，而 devices.json 是字典
# —— 键顺序会被其它写盘路径（load_devices/save_devices 的读-改-写）与 JSON 序列化打乱；
# 而且它与注册表的生命周期不同（设备被删/改名不该连带把顺序清掉）。
# 结构：{"order": [client_id, ...], "updated_at": <epoch 秒>, "updated_by": <openid 或 "">}
# fail-open：文件缺失/损坏/结构不对一律当空列表 —— 这只是「默认选中哪台」的偏好，读不出来就
# 退回「在线优先」的老口径，绝不让公开的设备列表接口 500。
DEVICE_PIN_MAX = 50         # 置顶列表上限（单次提交也按它截断）
DEVICE_PIN_CID_MAXLEN = 64  # 单个 client_id 的最大长度（与 /api/printer/bind_owner 同口径）
_device_pin_lock = threading.Lock()
_device_pin_cache = {"sig": None, "data": None}


def load_device_pin_order() -> list:
    """读取持久化的置顶顺序（**原始值**，尚未按当前设备集合过滤）；缺失/损坏返回空列表。"""
    data = _cached_json_read(DEVICE_PIN_FILE, _device_pin_cache, _device_pin_lock)
    if not isinstance(data, dict):
        return []
    order = data.get("order")
    if not isinstance(order, list):
        return []
    return [c for c in order if isinstance(c, str)]


def save_device_pin_order(order: list, updated_by: str = "") -> bool:
    """原子写入置顶顺序；成功 True，失败 False（**旧文件保持原样**）。

    与 save_devices() 同一条路：临时文件 + os.replace（换 inode ⇒ 指纹缓存不会把半截文件
    当成新内容），且只在真的写好之后才刷缓存。
    """
    payload = {"order": list(order), "updated_at": int(time.time()),
               "updated_by": (updated_by or "")[:128]}
    with _device_pin_lock:
        tmp = DEVICE_PIN_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, DEVICE_PIN_FILE)
        except OSError as e:
            print(f"  [DEV] 保存设备置顶顺序失败: {e}")
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False
        _device_pin_cache["sig"] = _json_file_sig(DEVICE_PIN_FILE)
        _device_pin_cache["data"] = copy.deepcopy(payload)
    return True


def get_pin_valid_cids() -> set:
    """当前**可被置顶**的设备集合 = 启用接单 ∩ 注册表里存在。

    两个条件都要：接单集合才是「能被选为打印目标」的真实来源（移动端只列这些）；
    注册表里有登记才说明这台设备真的连过服务器（接单记录可能是历史残留）。
    """
    claiming = set(get_claiming_devices().keys())
    if not claiming:
        return set()
    devices = load_devices()
    return {cid for cid in claiming if isinstance(cid, str) and cid in devices}


def sanitize_pin_order(raw, valid_ids: set) -> list:
    """归一化成「实际生效」的顺序：逐项校验 → 去重保序 → 丢掉不可用 cid → 截断上限。

    元素不是 str 在这里**静默丢弃**（这条函数也服务于读路径）；请求体结构本身不对
    （非 dict / order 不是 list / 元素不是 str）由 admin_device_pin_order 直接判 400。
    """
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for cid in raw:
        if not isinstance(cid, str):
            continue
        cid = cid.strip()
        if not cid or len(cid) > DEVICE_PIN_CID_MAXLEN:
            continue
        if cid in seen or cid not in valid_ids:
            continue
        seen.add(cid)
        out.append(cid)
        if len(out) >= DEVICE_PIN_MAX:
            break
    return out


def get_effective_pin_order() -> list:
    """本次**实际生效**的置顶顺序（已按当前设备集合过滤 —— 被过滤项不占序位）。"""
    return sanitize_pin_order(load_device_pin_order(), get_pin_valid_cids())


def _mask_person_name(name: str) -> str:
    """S2-04：对外接口里的真实姓名脱敏（保留首字，其余 *）。

    背景（DS-12/DS-13）：`/api/printer_status` 与 `/api/claiming_devices` 是**公开**接口
    （不需要登录），却直接下发设备所有者的真实姓名（如「姚懿祥的设备 | DESKTOP-…」）——
    任何人扫一眼接口就能拿到店里员工的真实姓名 + 机器标识。管理侧的
    `/api/printer/devices`（printer token + admin scope）仍然返回真名，绑定/对账不受影响。
    """
    name = (name or "").strip()
    if not name:
        return ""
    return name[0] + "*" * max(1, len(name) - 1)


def device_display_label(client_id: str, mask: bool = False) -> str:
    """设备展示名：「{所有者}的设备 | {client_id}」，如「姚懿祥的设备 | DESKTOP-EJGEB1V-a6c1a1365e」。
    未绑定所有者 → 直接显示 client_id。mask=True 时所有者姓名按 _mask_person_name 脱敏。"""
    if not client_id:
        return ""
    entry = get_device_entry(client_id)
    owner = entry.get("owner_name", "") or ""
    if mask:
        owner = _mask_person_name(owner)
    if owner:
        return f"{owner}的设备 | {client_id}"
    return client_id


def _printer_state_payload(client_id: str) -> dict:
    """构造发给某设备的接单状态 payload（本机是否启用接单、全部接单设备、本机所有者）。
    2026-12：多设备接单 —— claiming_devices 为全部启用接单设备，is_active 表示本机是否在其中。
    2026-10：附带本机自动双面能力（本地工具据此调整双面 UI / 提示"需人工翻纸"）。"""
    claiming = get_claiming_devices()
    mine = get_device_entry(client_id)
    return {
        "claiming_devices": {cid: (entry.get("owner_name", "") or "") for cid, entry in claiming.items()},
        "is_active": client_id in claiming,
        # 本机设备信息：owner_name = 本机绑定的所有者（授权页绑定），新建标签页默认归属者用
        "owner_name": mine.get("owner_name", "") or "",
        "device_name": mine.get("device_name", "") or client_id,
        # 兼容旧字段（旧版本地工具读取 active_client_id 判断「唯一接管者」）
        "active_client_id": client_id if client_id in claiming else "",
        "active_owner_name": (claiming.get(client_id) or {}).get("owner_name", ""),
        "active_device_name": mine.get("device_name", "") if client_id in claiming else "",
        # 本机自动双面能力（服务端为准：人工盖章 > 自动探测 > 未确认）
        **_device_capability_fields(client_id),
        # 本机攒批模式镜像（2026-10：本地工具上报后这里回显，便于两端对齐）
        "stack_mode": _stack_state_of(client_id)["mode"],
        "stack_busy": _stack_state_of(client_id)["busy"],
        # 2026-11：本机是否有一条"离线时期望"还没落地（设备端可据此提示"上线后自动应用"）
        "stack_pending": _stack_intent_pending(client_id),
        # 2026-11：期望值本身（true/false/null）—— 设备端据此显示"期望开启，等待应用"
        "stack_desired": _stack_desired_of(client_id),
    }


def broadcast_printer_state():
    """向所有在线客户端推送当前接单状态（接管变化实时同步，设备端据此刷新 UI）。"""
    active = get_active_clients()
    if not active:
        return
    for cid in active:
        with printer_clients_lock:
            info = printer_clients.get(cid)
            sid = info["sid"] if info else None
        if not sid:
            continue
        try:
            socketio.emit("printer_state", _printer_state_payload(cid), to=sid)
        except Exception as e:
            print(f"  [CLAIM] 推送接单状态到 {cid} 失败: {e}")


class TokenRevoked(Exception):
    """S1-06：token 的 ver 与用户当前 token_version 不一致（= 已"退出全部设备"）。"""


# -------- S1-06：会话可吊销（jti/ver + users.token_version，60s 缓存避免每请求打库）--------
TOKEN_VERSION_CACHE_TTL = 60
_token_version_cache = {}
_token_version_lock = threading.Lock()


def _token_version_of(openid):
    """读 users.token_version（60s 内存缓存）。取不到按 0（老库/异常时保持可用）。"""
    now = time.time()
    with _token_version_lock:
        hit = _token_version_cache.get(openid)
        if hit and now - hit[1] < TOKEN_VERSION_CACHE_TTL:
            return hit[0]
    ver = 0
    try:
        conn = get_user_db()
        try:
            row = conn.execute(
                "SELECT version FROM token_versions WHERE openid = ?", (openid,)).fetchone()
            if row and row["version"] is not None:
                ver = int(row["version"])
        finally:
            conn.close()
    except Exception:
        ver = 0
    with _token_version_lock:
        _token_version_cache[openid] = (ver, now)
    return ver


def _invalidate_token_version_cache(openid):
    with _token_version_lock:
        _token_version_cache.pop(openid, None)


def _bump_token_version(openid):
    """递增该用户的 token_version（"退出全部设备"）：所有旧 token 立即失效。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_user_db()
    try:
        # UPSERT：首次就是 1；已存在则 +1（guest 账号没有 users 行也能吊销）
        conn.execute(
            "INSERT INTO token_versions (openid, version, updated_at) VALUES (?, 1, ?) "
            "ON CONFLICT(openid) DO UPDATE SET version = version + 1, updated_at = excluded.updated_at",
            (openid, now_str))
        conn.commit()
        row = conn.execute(
            "SELECT version FROM token_versions WHERE openid = ?", (openid,)).fetchone()
        new_ver = int(row["version"]) if row and row["version"] is not None else 1
    finally:
        conn.close()
    with _token_version_lock:
        _token_version_cache[openid] = (new_ver, time.time())
    return new_ver


def _issue_login_token(openid):
    """签发登录 token（S1-06）：payload 带 openid + ver + jti。

    · `ver` = 该用户当前 token_version ⇒ "退出全部设备"后旧 token 的 ver 对不上；
    · `jti` = 单次签发标识（供后续按会话吊销/审计；本轮只做 ver 吊销）。
    · 兼容：S1-06 之前签发的 payload 是纯字符串 openid，`_load_login_token` 按 ver=0 处理
      （老库 token_version 默认 0 ⇒ 存量登录态不会因为这次改动被踢下线）。
    """
    return token_serializer.dumps({
        "o": openid,
        "v": _token_version_of(openid),
        "j": uuid.uuid4().hex,
    })


def _load_login_token(token):
    """解析登录 token → openid；被吊销抛 TokenRevoked，签名/过期抛原始异常。"""
    payload = token_serializer.loads(token, max_age=TOKEN_MAX_AGE)
    if isinstance(payload, str):
        openid, ver = payload, 0          # S1-06 之前的形态
    elif isinstance(payload, dict):
        openid = str(payload.get("o") or "")
        try:
            ver = int(payload.get("v") or 0)
        except (TypeError, ValueError):
            raise BadSignature("bad token payload")
    else:
        raise BadSignature("bad token payload")
    if not openid:
        raise BadSignature("empty openid")
    if ver != _token_version_of(openid):
        raise TokenRevoked(openid)
    return openid


def make_download_url(file_id, openid="", task_id=None):
    """生成带签名的文件下载 URL（1 小时有效）。

    S1-06：**绑定 openid + file_id + task_id**（不再是"谁拿到 URL 谁下"）。payload 里
    写入这三者，下载时校验：文件属于该任务、任务属于该 openid。泄露的 URL 只能下载
    "这一个任务的这一个文件"，且 1 小时过期。分析请求等没有任务的场景传空即可。
    """
    payload = {"f": file_id, "o": openid or ""}
    if task_id is not None:
        payload["t"] = int(task_id)
    token = download_serializer.dumps(payload)
    return f"{PUBLIC_BASE_URL}/api/download/{file_id}?t={token}"


def _iso_to_ts(s):
    """"%Y-%m-%d %H:%M:%S" → epoch 秒；解析失败返回 0"""
    try:
        return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception:
        return 0


def _get_printer_token():
    """获取打印机认证 token（P1-3）：优先 X-Printer-Token 请求头，回退 query 参数 ?token=（向后兼容）。
    安全说明：
    - query 传参会出现在 nginx 访问日志中，长期暴露有泄露风险，应优先走请求头；
    - PRINTER_TOKEN 应定期轮换（修改 config.py 中 TOKEN 后重启后端生效）。"""
    tok = request.headers.get("X-Printer-Token", "")
    if not tok:
        tok = request.args.get("token", "")
    return tok


def _printer_token_ok(tok):
    """唯一的 printer token 判定（S0-06）：恒时比较 + 过渡期双接受 + 未配置即拒绝。

    · 双接受：config.py 的 TOKEN（现行）+ 可选的 TOKEN_PREV（轮换过渡窗口 ≤24h）；
    · fail-closed：PRINTER_TOKEN 未配置时**一律拒绝**（旧写法 `if not PRINTER_TOKEN or ...`
      在未配置时也是拒绝，语义保持一致）；
    · hmac.compare_digest：避免按字节短路比较泄漏前缀信息；
    · 以前这里有 26 处内联 `token != PRINTER_TOKEN`，逐处改双接受必漏，故统一收敛到本函数。
    """
    if not isinstance(tok, str) or not tok:
        return False
    # 现行 token 缺失 ⇒ 失败关闭（配置坏了也不能"谁都能进"；PREV 只是轮换期的**补充**，
    # 不能替代现行值 —— 否则把 TOKEN 删掉只剩 TOKEN_PREV 时会静默继续接受旧令牌）。
    if not isinstance(PRINTER_TOKEN, str) or not PRINTER_TOKEN:
        return False
    candidates = [PRINTER_TOKEN]
    if isinstance(PRINTER_TOKEN_PREV, str) and PRINTER_TOKEN_PREV:
        candidates.append(PRINTER_TOKEN_PREV)
    ok = False
    for cand in candidates:
        try:
            if hmac.compare_digest(tok, cand):
                ok = True
        except TypeError:
            continue
    return ok


def _hash_device_secret(secret):
    return hashlib.sha256(("hn-device:" + str(secret)).encode("utf-8")).hexdigest()


def _load_device_secrets_unlocked():
    try:
        with open(DEVICE_SECRETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _load_device_secrets():
    with _device_secrets_lock:
        return _load_device_secrets_unlocked()


def _save_device_secrets(data):
    """原子写（临时文件 + os.replace），读写都在锁内。"""
    with _device_secrets_lock:
        tmp = DEVICE_SECRETS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, DEVICE_SECRETS_FILE)


def _check_device_secret(device_id, provided):
    """S1-06 设备密钥校验。返回 (ok, message, issued_secret|None)。

    · 首次见到该 device_id：生成密钥、存哈希、**下发一次**（客户端应保存）；
    · 已有记录且带密钥：恒时比对，错误 → 拒绝；
    · 已有记录但不带密钥（老 APP）：宽限窗口内**轮换并再次下发**（下次能存下来），
      窗口后拒绝 —— 这是计划 §S1-06「不再只凭自报 device_id」的迁移路径。
    """
    key = hashlib.sha256(("device_secret:" + device_id).encode("utf-8")).hexdigest()[:32]
    rec = _load_device_secrets().get(key) or {}
    now_ts = time.time()
    provided = provided.strip() if isinstance(provided, str) else ""

    if rec.get("secret_hash"):
        if provided:
            if not hmac.compare_digest(_hash_device_secret(provided), rec["secret_hash"]):
                return False, "设备凭证无效，请重新登录", None
            return True, "", None
        issued_at = float(rec.get("issued_at_ts") or 0)
        # 宽限期从**首次签发时刻**起算；时间戳缺失/为 0 一律按"已过期"处理（fail-closed）
        grace_left = (DEVICE_SECRET_GRACE_SECONDS - (now_ts - issued_at)) if issued_at else 0
        if grace_left <= 0:
            return False, "请更新 APP 后重新登录（设备凭证已启用）", None
        # 宽限期内：轮换并下发（老版本会忽略，新版本会存下来）
        new_secret = secrets.token_urlsafe(32)
        secrets_map = _load_device_secrets()
        secrets_map[key] = {
            "secret_hash": _hash_device_secret(new_secret),
            "issued_at_ts": now_ts,
            "last_used_at": now_ts,
            "rotated_for_legacy": True,
        }
        _save_device_secrets(secrets_map)
        print(f"  [AUTH] 设备 {device_id[:12]}... 未带 device_secret（老客户端），宽限期内轮换下发一次")
        return True, "", new_secret

    # 首次注册
    new_secret = secrets.token_urlsafe(32)
    secrets_map = _load_device_secrets()
    secrets_map[key] = {
        "secret_hash": _hash_device_secret(new_secret),
        "issued_at_ts": now_ts,
        "last_used_at": now_ts,
    }
    _save_device_secrets(secrets_map)
    return True, "", new_secret


# ===== S1-01：per-device token（DS-02 根治 / R7）=====
# 现状：全站只有 config.py 里的**一个** TOKEN —— 泄露一次就要全站轮换、无法单独吊销某台
# 设备、也无法区分"这台设备能做什么"。现在每台设备在 devices.json 里各有一条：
#   token_hash / scopes / token_issued_at / revoked_at / label（**只存哈希**，明文只在
#   签发响应里出现一次）。
# 迁移窗口：全局 TOKEN 继续被接受（`legacy=True`，映射成 print+admin 全部 scope），
# 等两台门店都换成本机 token 后再把它降权/删除（见 docs/安全加固修复计划 §S1-01）。
_PRINTER_SCOPES = ("print", "admin")
_DEFAULT_DEVICE_SCOPES = ("print", "admin")   # 门店机既要拉单/回报，也要看统计/订正


def _hash_printer_token(tok):
    """printer token 的存储哈希（与全局 TOKEN 的恒时比较分开：这里只用于查表）。"""
    return hashlib.sha256(("hn-printer:" + str(tok)).encode("utf-8")).hexdigest()


def _find_device_by_token(tok):
    """按 token 哈希查设备记录；已吊销返回 None。设备表只有几十条，线性扫描足够。"""
    if not isinstance(tok, str) or not tok:
        return None
    h = _hash_printer_token(tok)
    for cid, rec in load_devices().items():
        if not isinstance(rec, dict) or not rec.get("token_hash"):
            continue
        try:
            if not hmac.compare_digest(str(rec["token_hash"]), h):
                continue
        except TypeError:
            continue
        if rec.get("revoked_at"):
            return None
        scopes = rec.get("scopes")
        if not isinstance(scopes, list) or not scopes:
            scopes = list(_DEFAULT_DEVICE_SCOPES)
        return {"client_id": cid, "scopes": set(scopes), "legacy": False}
    return None


def _authenticate_printer(tok):
    """printer 认证的**唯一入口**（S1-01）：返回 {client_id, scopes, legacy} 或 None。

    顺序：per-device token 优先；其次全局 TOKEN（迁移窗口，legacy）；都不匹配 → None。
    注意：全局 TOKEN 只作为**迁移兼容**存在，它的存在意味着"一把钥匙全站通用"这个洞
    还没完全关掉 —— 两台门店换完本机 token 后应把 config.py 的 TOKEN 删掉。
    """
    dev = _find_device_by_token(tok)
    if dev:
        return dev
    if _printer_token_ok(tok):
        return {"client_id": "", "scopes": set(_DEFAULT_DEVICE_SCOPES), "legacy": True}
    return None


def _printer_scope_ok(tok, scope):
    rec = _authenticate_printer(tok)
    return bool(rec and scope in rec["scopes"])


def _request_printer_token():
    """取本次请求携带的 printer token（头 → query → JSON body，与 _get_printer_token 同源）。"""
    tok = _get_printer_token()
    if tok:
        return tok
    # 深嵌套请求体（depth ≥ 1000）解析会抛 RecursionError：这里**吞掉并当成"没有 token"**，
    # 让 scope 装饰器回 403（v7.0b 的契约：未认证一律 403，绝不因为畸形体变成 400/500）。
    try:
        body = request.get_json(silent=True)
    except RecursionError:
        return ""
    if isinstance(body, dict):
        return str(body.get("token") or "")
    return ""


def require_printer_scope(scope):
    """装饰器：printer token 必须具备指定 scope（S1-01）。

    用在"管理类"路由上（订单订正/统计/日志/设备管理）。**不替换**各路由内部原有的
    `_printer_token_ok` 判定 —— 多一道校验比少一道好，且旧客户端/旧测试不会因为
    "没带 scope 概念"而改变行为（全局 TOKEN 映射为 print+admin）。
    """
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not _printer_scope_ok(_request_printer_token(), scope):
                return jsonify({"success": False,
                                "message": "token 无效或缺少 %s 权限" % scope}), 403
            return fn(*args, **kwargs)
        return wrapper
    return deco


def issue_device_token(client_id, label="", scopes=None):
    """为设备签发（或轮换）token。返回明文 token（**只此一次**），devices.json 存哈希。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scopes = [s for s in (scopes or list(_DEFAULT_DEVICE_SCOPES)) if s in _PRINTER_SCOPES]
    if not scopes:
        scopes = list(_DEFAULT_DEVICE_SCOPES)
    token = secrets.token_urlsafe(32)
    devices = load_devices()
    rec = devices.get(client_id)
    if not isinstance(rec, dict):
        rec = {}
    rec.update({
        "token_hash": _hash_printer_token(token),
        "scopes": scopes,
        "token_issued_at": now_str,
        "revoked_at": "",
        "label": (label or rec.get("label") or "")[:64],
    })
    devices[client_id] = rec
    save_devices(devices)
    print(f"[TOKEN] 已为设备 {client_id} 签发 token（scopes={scopes}）")
    return token, scopes


def revoke_device_token(client_id):
    """吊销某台设备的 token（保留其余记录，便于审计/恢复）。"""
    devices = load_devices()
    rec = devices.get(client_id)
    if not isinstance(rec, dict) or not rec.get("token_hash"):
        return False
    rec["revoked_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rec["token_hash"] = ""          # 立即失效（不留哈希，避免"吊销后又被恢复"）
    devices[client_id] = rec
    save_devices(devices)
    print(f"[TOKEN] 已吊销设备 {client_id} 的 token")
    return True


def _get_pricing_config():
    """读取 pricing.json（不缓存，供提交订单时按服务端配置重算附加费），失败返回空 dict。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _get_bound_owner_name(openid: str) -> str:
    """反查 openid 在收支清算成员绑定表中绑定的成员名（2026-12 修复）。
    顾客订单推送时附带该名字，本地打印工具据此把标签页归属设为下单用户绑定的成员，
    而不是回退到「第一个成员」。无绑定 / 绑定成员已删除 / 未配置返回空串。"""
    if not openid:
        return ""
    try:
        conn = get_db()
        try:
            row = conn.execute("SELECT data FROM finance_config WHERE id = 1").fetchone()
        finally:
            conn.close()
        if not row or not row["data"]:
            return ""
        cfg = json.loads(row["data"])
        cc = cfg.get("config") or {}
        members = {}
        for m in (cc.get("members") or []):
            if isinstance(m, dict) and m.get("id"):
                members[str(m["id"])] = str(m.get("name", "")).strip()
        for b in (cc.get("memberBindings") or []):
            if isinstance(b, dict) and b.get("openid") == openid and b.get("memberId"):
                return members.get(str(b["memberId"]), "") or ""
        return ""
    except Exception as e:
        print(f"  [BIND] 反查绑定成员失败: {e}")
        return ""


# 多打印机负载均衡（P2-12）：轮询选择客户端，避免总推给 active_clients[0] 造成负载不均
_client_round_robin_idx = 0
_client_round_robin_lock = threading.Lock()


def _pick_client(active_clients):
    """从活跃客户端列表中轮询选一个（多打印机负载均衡），列表为空返回 None。"""
    global _client_round_robin_idx
    if not active_clients:
        return None
    with _client_round_robin_lock:
        idx = _client_round_robin_idx % len(active_clients)
        _client_round_robin_idx += 1
        return active_clients[idx]


def _content_disposition(name: str) -> str:
    """构造**对中文安全**的 Content-Disposition（打印机客户端下载文件用）。

    为什么不用 `send_file(download_name=<中文名>)`：Werkzeug 遇到非 ASCII 名会把 ASCII
    兜底写成 `encode("ascii", "ignore")` 的结果 —— 纯中文名直接退化成 `.pdf`（主名被吃光，
    只剩扩展名），真名只留在 `filename*=UTF-8''` 里。老客户端只读第一个 `filename=`，
    于是把文件落成 `.pdf`（点开头 ⇒ os.path.splitext 认为没有扩展名）→ 类型判定为空 →
    **中文文件名的云端订单必然打印失败**（线上 2026-10：#1004~#1007）。

    这里保证：ASCII 兜底**一定有主名**（中文名 → `download.pdf`），真名照旧放 filename*。
    这样即使客户端是旧版（只认 filename=），最多是文件名变通用，不会再打出"不支持的类型"。"""
    raw = str(name or "").strip() or "download"
    stem, ext = os.path.splitext(raw)
    ascii_stem = re.sub(r"[^A-Za-z0-9._-]", "", stem)[:60].strip("._-") or "download"
    ascii_ext = re.sub(r"[^A-Za-z0-9.]", "", ext)
    ascii_name = (ascii_stem + ascii_ext) or "download"
    quoted = urllib.parse.quote(raw, safe="")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}'


@app.route("/api/download/<file_id>")
def download_file(file_id):
    """打印机客户端下载文件（用签名 token 验证）"""
    token = request.args.get("t", "")
    try:
        payload = download_serializer.loads(token, max_age=3600)
    except (BadSignature, SignatureExpired):
        # S3-04：过期/伪造链接也是一次"试图读文件" ⇒ 记 denied（此时拿不到任何绑定信息）
        _audit_file_access("download", "denied", file_id=file_id, actor="signed_url")
        return jsonify({"success": False, "message": "下载链接无效或已过期"}), 403

    # S1-06：三种 payload 形态
    #   ① dict（新）：{"f": file_id, "o": openid, "t": task_id?} → 校验绑定
    #   ② str（S0-07 之后、S1-06 之前的短窗口）：只有 file_id，按旧口径放行
    if isinstance(payload, dict):
        fid = str(payload.get("f") or "")
        bound_openid = str(payload.get("o") or "")
        bound_task = payload.get("t")
    elif isinstance(payload, str):
        fid, bound_openid, bound_task = payload, "", None
    else:
        _audit_file_access("download", "denied", file_id=file_id, actor="signed_url")
        return jsonify({"success": False, "message": "下载链接无效或已过期"}), 403
    if fid != file_id:
        _audit_file_access("download", "denied", file_id=file_id, openid=bound_openid,
                           actor="signed_url")
        return jsonify({"success": False, "message": "下载链接无效或已过期"}), 403
    if bound_task is not None:
        try:
            bound_task = int(bound_task)
        except (TypeError, ValueError):
            _audit_file_access("download", "denied", file_id=file_id, openid=bound_openid,
                               actor="signed_url")
            return jsonify({"success": False, "message": "下载链接无效或已过期"}), 403
        conn = get_db()
        try:
            trow = conn.execute(
                "SELECT order_id FROM order_files WHERE id = ? AND file_id = ?",
                (bound_task, file_id)).fetchone()
            if not trow:
                _audit_file_access("download", "denied", file_id=file_id, openid=bound_openid,
                                   task_id=bound_task, actor="signed_url")
                return jsonify({"success": False, "message": "下载链接无效或已过期"}), 403
            if bound_openid:
                orow = conn.execute(
                    "SELECT openid FROM orders WHERE id = ?", (trow["order_id"],)).fetchone()
                if not orow or (orow["openid"] or "") != bound_openid:
                    _audit_file_access("download", "denied", file_id=file_id, openid=bound_openid,
                                       task_id=bound_task, actor="signed_url")
                    return jsonify({"success": False, "message": "下载链接无效或已过期"}), 403
        finally:
            conn.close()

    conn = get_db()
    row = conn.execute("SELECT path, original_name FROM files WHERE id = ?", (file_id,)).fetchone()
    conn.close()

    if not row or not os.path.exists(row["path"]):
        # S3-04：行没了/盘上没了都记 not_found（ext 从原文件名取，审计里仍不落原名）
        _audit_file_access("download", "not_found", file_id=file_id, openid=bound_openid,
                           task_id=bound_task, actor="signed_url",
                           ext=_audit_ext(row["original_name"]) if row else "")
        return jsonify({"success": False, "message": "文件不存在"}), 404

    # S3-04：成功必须在**返回响应之前**记（resp 一旦返回，send_file 的流可能立刻被消费）
    _audit_file_access("download", "ok", file_id=file_id, openid=bound_openid,
                       task_id=bound_task, actor="signed_url",
                       ext=_audit_ext(row["original_name"] or row["path"]))
    resp = send_file(row["path"], as_attachment=False)
    resp.headers["Content-Disposition"] = _content_disposition(
        row["original_name"] or os.path.basename(row["path"]))
    return resp


# ==================== 文件页数分析（统计基础）====================


def get_file_page_count(file_path, file_type=None):
    """
    根据文件路径计算页数：
    - PDF: 使用 pypdf 读取实际页数
    - DOC/DOCX: 通过 LibreOffice 转换为 PDF 后统计页数（不可用时返回 0）
    - 图片 (png/jpg/jpeg/gif/bmp/webp): 按 1 页计算
    - 其他: 默认返回 1 页
    file_path 为 None 时（文件不存在于磁盘），仅根据 file_type 判断，返回 0 表示未知
    """
    if file_type is None and file_path:
        file_type = os.path.splitext(file_path)[1].lower().lstrip(".")

    # PDF 文件：使用 pypdf 读取页数（需要文件存在于磁盘）
    if file_type in ("pdf",):
        if file_path and os.path.exists(file_path):
            try:
                from pypdf import PdfReader
                reader = PdfReader(file_path)
                page_count = len(reader.pages)
                print(f"  [PAGE] PDF 文件 {os.path.basename(file_path)}: {page_count} 页")
                return max(page_count, 1)
            except Exception as e:
                print(f"  [WARN] 读取 PDF 页数失败 ({e})，按 1 页计算")
                return 1
        else:
            print(f"  [PAGE] PDF 文件不在磁盘，默认按 1 页计算")
            return 1

    # Word 文档：交本地打印工具转换计数（Word/WPS 比服务器 LibreOffice 更可靠）
    if file_type in ("doc", "docx"):
        name = os.path.basename(file_path) if file_path else "未知文件"
        print(f"  [PAGE] {file_type.upper()} {name}: 等待本地打印工具分析页数")
        return 0  # 0 表示"待验证"，由本地打印工具通过 Word/WPS 转换后回报

    # 图片文件：按 1 页计算
    if file_type in ("png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff", "tif"):
        name = os.path.basename(file_path) if file_path else "未知文件"
        print(f"  [PAGE] 图片文件 {name}: 按 1 页计算")
        return 1

    # 默认：1 页
    print(f"  [PAGE] 未知类型 .{file_type}，默认按 1 页计算")
    return 1


# ==================== 数据库 ====================

def get_db():
    # timeout=30 + busy_timeout=30000：多线程并发写冲突时，写者最多等待 30s
    # 而不是立即抛出 sqlite3.OperationalError: database is locked。
    # （配合 init_db() 中开启的 WAL 模式，可彻底消除绝大多数锁冲突。）
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn

def get_user_db():
    """独立的用户数据库连接（users / license_keys 表）"""
    conn = sqlite3.connect(USER_DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _retry_on_lock(fn, *args, max_attempts=3, **kwargs):
    """在 database is locked 错误时自动重试（指数退避：0.2s / 0.4s / 0.8s）。
    后端两个写路径（pull_queued_orders 的 HTTP handler 和 APScheduler 的
    process_pending_orders）可能在并发的协程/线程中同时写入 order_files，
    即使 WAL 模式下也会短暂互斥。重试让冲突方自动等待而非报 500。"""
    import random
    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < max_attempts - 1:
                delay = 0.2 * (2 ** attempt) + random.uniform(0, 0.05)
                time.sleep(delay)
                continue
            raise


def _cleanup_once_on_startup():
    """S1-03：**启动时**也做一次过期清理（原来只有 10 分钟定时任务）。

    计划 §S1-03 要求"启动 + 定时双触发"：门店机/服务器重启后，到期文件不必再等一个
    定时周期；也避免"进程长期不重启、定时任务被跳过"时保留期形同虚设。
    """
    # S3-02 迁移演练（ops/db_migration_drill.py）：演练副本不搬 uploads 目录，启动清理会把
    # 所有 files 行当成"物理文件已消失的幽灵"删掉 ⇒ 行数比对失去意义。演练只需要验证
    # schema/迁移本身，用显式环境变量跳过（正常启动/生产**不会**设置它）。
    if os.environ.get("HN_DRILL_MODE") == "1":
        print("  [DRILL] HN_DRILL_MODE=1：跳过启动清理（迁移演练副本无 uploads）")
        return None

    def _run():
        try:
            cleanup_expired_files()
            print("  [CLEANUP] 启动时过期清理完成")
        except Exception as e:  # noqa: BLE001
            print(f"  [CLEANUP] 启动时过期清理失败: {type(e).__name__}: {e}")
    t = threading.Thread(target=_run, name="startup-cleanup", daemon=True)
    t.start()
    return t


def _build_md5_index_in_background():
    """S1-10：把"扫全盘算 MD5"移出启动关键路径。

    原实现每次启动都在 init_db 里同步跑 build_md5_index()（逐文件读全量算 MD5）——
    文件数一多，重启到可服务的时间随文件数线性变长（计划 §S1-10）。现在放后台线程：
    服务先起来，索引补全在后台完成。并发安全：上传侧只读索引、写入走 `_md5_index_lock`；
    后台补全期间上传去重可能漏掉"尚未入索引"的老文件（最多多存一份，不会丢数据）。
    """
    if os.environ.get("HN_DRILL_MODE") == "1":
        print("  [DRILL] HN_DRILL_MODE=1：跳过 MD5 索引补全（演练副本无 uploads）")
        return None

    def _run():
        try:
            build_md5_index()
            print("  [MD5] 后台索引补全完成")
        except Exception as e:  # noqa: BLE001
            print(f"  [MD5] 后台索引补全失败: {type(e).__name__}: {e}")
    t = threading.Thread(target=_run, name="md5-index-builder", daemon=True)
    t.start()
    return t


def init_db():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(AVATAR_DIR, exist_ok=True)

    # 构建/补全 MD5 文件索引（用于上传去重）—— S1-10：改后台线程，不阻塞启动
    _build_md5_index_in_background()

    conn = get_db()
    user_conn = get_user_db()
    for db_conn, db_name in [(conn, "orders.db"), (user_conn, "users.db")]:
        try:
            db_conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError as e:
            print(f"  [WARN] {db_name} 开启 WAL 模式失败: {e}")
        db_conn.commit()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            id            TEXT PRIMARY KEY,
            original_name TEXT    NOT NULL,
            saved_name    TEXT    NOT NULL,
            path          TEXT    NOT NULL,
            size          INTEGER NOT NULL,
            created_at    TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id          TEXT,
            file             TEXT    NOT NULL,
            copies           INTEGER NOT NULL,
            status           TEXT    NOT NULL DEFAULT 'printing',
            created_at       TEXT    NOT NULL,
            openid           TEXT    DEFAULT '',
            duplex           TEXT    DEFAULT 'on',
            page_count       INTEGER DEFAULT 1,
            price_per_page   REAL    DEFAULT 0.25,
            total_price      REAL    DEFAULT 0,
            is_free          INTEGER DEFAULT 0,
            manual_status_by TEXT    DEFAULT '',
            manual_status_at TEXT    DEFAULT '',
            FOREIGN KEY (file_id) REFERENCES files(id)
        )
        """
    )

    # 兼容旧数据库：添加可能不存在的新列（如果已存在则跳过）
    # 旧列（v1 迁移）
    for col, col_type, default in [
        ("openid", "TEXT", "''"),
        ("duplex", "TEXT", "'on'"),
    ]:
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {col_type} DEFAULT {default}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    # 新列（v2 统计系统迁移）
    for col, col_type, default in [
        ("page_count", "INTEGER", "1"),
        ("price_per_page", "REAL", "0.25"),
        ("total_price", "REAL", "0"),
        ("is_free", "INTEGER", "0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {col_type} DEFAULT {default}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    # v3 迁移：删除预约字段（SQLite 3.35+ 支持 DROP COLUMN）
    for col in ["reservation_date", "reservation_time"]:
        try:
            conn.execute(f"ALTER TABLE orders DROP COLUMN {col}")
            conn.commit()
            print(f"  已删除旧字段: {col}")
        except sqlite3.OperationalError:
            pass  # 字段不存在或 SQLite 版本不支持，忽略

    # v20 迁移：订单备注（用户提交时填写，≤100 字）
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN remark TEXT DEFAULT ''")
        conn.commit()
        print("  已添加 orders.remark 列")
    except sqlite3.OperationalError:
        pass

    # ============ 用户数据库 (users.db) ============
    # 用户表
    user_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            openid      TEXT PRIMARY KEY,
            nickname    TEXT DEFAULT '',
            avatar_path TEXT DEFAULT '',
            updated_at  TEXT NOT NULL
        )
        """
    )
    try:
        user_conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'guest'")
        user_conn.commit()
        print("  已添加 users.role 列")
    except sqlite3.OperationalError:
        pass
    try:
        user_conn.execute("ALTER TABLE users ADD COLUMN temp_until TEXT DEFAULT NULL")
        user_conn.commit()
        print("  已添加 users.temp_until 列")
    except sqlite3.OperationalError:
        pass
    try:
        user_conn.execute("ALTER TABLE users ADD COLUMN theme_mode TEXT DEFAULT 'auto'")
        user_conn.commit()
        print("  已添加 users.theme_mode 列")
    except sqlite3.OperationalError:
        pass

    # 许可密钥表
    user_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS license_keys (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            key              TEXT    UNIQUE NOT NULL,
            created_by       TEXT    NOT NULL,
            used_by          TEXT    DEFAULT NULL,
            validity_minutes INTEGER NOT NULL,
            created_at       TEXT    NOT NULL,
            expires_at       TEXT    NOT NULL,
            used_at          TEXT    DEFAULT NULL
        )
        """
    )
    try:
        user_conn.execute("ALTER TABLE license_keys ADD COLUMN type TEXT DEFAULT 'temp'")
        user_conn.commit()
        print("  已添加 license_keys.type 列")
    except sqlite3.OperationalError:
        pass
    try:
        user_conn.execute("ALTER TABLE license_keys ADD COLUMN order_id INTEGER DEFAULT NULL")
        user_conn.commit()
        print("  已添加 license_keys.order_id 列")
    except sqlite3.OperationalError:
        pass
    # 密钥生命周期状态：unused → used → revoked/finished/archived。
    # 已使用的密钥作为授权记录永久保留，绝不硬删；作废/结束只改状态。
    try:
        user_conn.execute("ALTER TABLE license_keys ADD COLUMN status TEXT DEFAULT 'used'")
        user_conn.commit()
        print("  已添加 license_keys.status 列")
    except sqlite3.OperationalError:
        pass
    # 迁移纠偏：已使用（used_by 非空）的行保持 used，未使用的行标记 unused
    user_conn.execute("UPDATE license_keys SET status = 'unused' WHERE used_by IS NULL")
    user_conn.commit()

    # 用户移除记录（被谁、何时移除；重新授权时清空，密钥记录仍保留）
    try:
        user_conn.execute("ALTER TABLE users ADD COLUMN removed_at TEXT DEFAULT NULL")
        user_conn.commit()
        print("  已添加 users.removed_at 列")
    except sqlite3.OperationalError:
        pass
    try:
        user_conn.execute("ALTER TABLE users ADD COLUMN removed_by TEXT DEFAULT NULL")
        user_conn.commit()
        print("  已添加 users.removed_by 列")
    except sqlite3.OperationalError:
        pass

    # 微信账号绑定：dev 设备账号 → 微信 openid（个人认证密钥绑定后写入）
    try:
        user_conn.execute("ALTER TABLE users ADD COLUMN bound_openid TEXT DEFAULT ''")
        user_conn.commit()
        print("  已添加 users.bound_openid 列")
    except sqlite3.OperationalError:
        pass

    # S1-03：客户自选保留期（0 = 用全局默认；>0 = created_at + N 小时到期）
    try:
        conn.execute("ALTER TABLE files ADD COLUMN retention_hours INTEGER DEFAULT 0")
        conn.commit()
        print("  已添加 files.retention_hours 列（S1-03 客户自选保留期）")
    except sqlite3.OperationalError:
        pass

    # S1-06：会话版本号 —— "退出全部设备"递增它，旧 token 立即失效（jti/ver 吊销的锚点）。
    # ⚠ 刻意**不**放 users 表：guest 设备账号（device_login 的 dev_ 前缀）在 users 表里
    # 根本没有行（惰性注册，见 device_login 的注释），改 users 列会 UPDATE 0 行 ⇒ 版本
    # 永远停在 0、"退出全部设备"对它们静默失效。独立表对"有行/无行"两种账号一视同仁。
    user_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS token_versions (
            openid     TEXT PRIMARY KEY,
            version    INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT    NOT NULL DEFAULT ''
        )
        """)
    user_conn.commit()

    # 个人认证密钥表：微信账号生成 → APP 设备兑换，完成身份绑定。
    # 与 license_keys 语义分离（授权 vs 身份），已使用的记录作为绑定历史永久保留。
    user_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bind_keys (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            key        TEXT    UNIQUE NOT NULL,
            created_by TEXT    NOT NULL,
            created_at TEXT    NOT NULL,
            expires_at TEXT    NOT NULL,
            status     TEXT    NOT NULL DEFAULT 'unused',
            used_by    TEXT    DEFAULT NULL,
            used_at    TEXT    DEFAULT NULL
        )
        """
    )
    user_conn.commit()
    user_conn.close()

    # v5 迁移：订单附加服务字段（派送/紧急/首页/地址）
    for col, col_type, default in [
        ("delivery_enabled", "INTEGER", "0"),
        ("delivery_location", "TEXT", "''"),
        ("delivery_percentage", "REAL", "0"),
        ("urgency", "TEXT", "'低'"),
        ("urgency_price", "REAL", "0"),
        ("cover_page", "INTEGER", "0"),
        ("cover_page_price", "REAL", "0.10"),
        ("pickup_address", "TEXT", "''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {col_type} DEFAULT {default}")
            conn.commit()
            print(f"  已添加 orders.{col} 列")
        except sqlite3.OperationalError:
            pass

    # v6 迁移：无障碍打印标记
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN auto_print INTEGER DEFAULT 0")
        conn.commit()
        print("  已添加 orders.auto_print 列")
    except sqlite3.OperationalError:
        pass

    # v19 迁移：无障碍打印预约形式（立即/指定时间/倒计时 → 折算为绝对时间 scheduled_at）
    for col, col_type, default in [
        ("schedule_mode", "TEXT", "'now'"),      # now | at | countdown
        ("scheduled_at", "TEXT", "''"),          # 绝对时间 "%Y-%m-%d %H:%M:%S"，仅预约单有值
        ("schedule_frozen", "INTEGER", "0"),     # 1 = 到点文件未就绪，已冻结暂停
        # 冻结发生的时刻（🔴13 自愈用）：旧行没有值 ⇒ 用 scheduled_at/created_at 兜底判断
        ("schedule_frozen_at", "TEXT", "''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {col_type} DEFAULT {default}")
            conn.commit()
            print(f"  已添加 orders.{col} 列")
        except sqlite3.OperationalError:
            pass

    # v21 迁移（2026-11）：人工订正留痕 —— 谁在什么时候把订单订正成了终态。
    # 为什么另开两列而不是复用 reject_reason：订正人/订正时间属于**订单级审计**，
    # reject_reason 是子任务级的"为什么没打出来"；两者在订单详情里并排展示（status_reason
    # 由子任务 reject_reason 汇总而来），混在一列会让"设备迟到成功救回"时的清理逻辑失真。
    for col, col_type, default in [
        ("manual_status_by", "TEXT", "''"),   # 订正操作者昵称（≤32 字符，空 = 非人工订正）
        ("manual_status_at", "TEXT", "''"),   # 订正时刻（服务器时间 YYYY-MM-DD HH:MM:SS）
    ]:
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {col_type} DEFAULT {default}")
            conn.commit()
            print(f"  已添加 orders.{col} 列")
        except sqlite3.OperationalError:
            pass

    # v22 迁移（2026-11）：订单**归属用户**人工调整的留痕 —— 与 v21 同款理由：
    # 谁在什么时候把这一单改到了哪个用户名下（空 = 非人工调整；审计列不复用 reject_reason）。
    for col, col_type, default in [
        ("manual_user_by", "TEXT", "''"),   # 调整操作者昵称（≤32 字符）
        ("manual_user_at", "TEXT", "''"),   # 调整时刻（服务器时间 YYYY-MM-DD HH:MM:SS）
    ]:
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {col_type} DEFAULT {default}")
            conn.commit()
            print(f"  已添加 orders.{col} 列")
        except sqlite3.OperationalError:
            pass

    # v5 迁移：订单文件子任务表（一次提交可包含多个文件，每个文件独立份数/状态）
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_files (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id       INTEGER NOT NULL,
            file_id        TEXT,
            file_name      TEXT    NOT NULL,
            copies         INTEGER DEFAULT 1,
            page_count     INTEGER DEFAULT 1,
            price_per_page REAL    DEFAULT 0.25,
            total_price    REAL    DEFAULT 0,
            is_free        INTEGER DEFAULT 0,
            status         TEXT    NOT NULL DEFAULT 'printing',
            created_at     TEXT    NOT NULL
        )
        """
    )

    # v6 迁移：order_files 添加 page_range 列（指定打印页码范围，如 "1-5,7,9"）
    try:
        conn.execute("ALTER TABLE order_files ADD COLUMN page_range TEXT DEFAULT ''")
        conn.commit()
        print("  已添加 order_files.page_range 列")
    except sqlite3.OperationalError:
        pass

    # v7 迁移：order_files 添加 operator_client 列（记录领取任务的打印机客户端 ID）
    try:
        conn.execute("ALTER TABLE order_files ADD COLUMN operator_client TEXT DEFAULT ''")
        conn.commit()
        print("  已添加 order_files.operator_client 列（用于记录领取任务的打印机）")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v8 迁移：order_files 添加 duplex 列（双面打印模式从订单级下沉到文件级）
    try:
        conn.execute("ALTER TABLE order_files ADD COLUMN duplex TEXT DEFAULT 'on'")
        conn.commit()
        print("  已添加 order_files.duplex 列（文件级双面打印模式）")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v12 迁移：orders 添加 order_number 列（HN20260720-0001 格式订单号）
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN order_number TEXT DEFAULT ''")
        conn.commit()
        print("  已添加 orders.order_number 列")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v13 迁移：files 添加 page_count 列（缓存文件页数，避免重复转换）
    try:
        conn.execute("ALTER TABLE files ADD COLUMN page_count INTEGER DEFAULT 0")
        conn.commit()
        print("  已添加 files.page_count 列")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v14 迁移：files 添加 page_count_verified 列（页数是否经本地工具验证）
    try:
        conn.execute("ALTER TABLE files ADD COLUMN page_count_verified INTEGER DEFAULT 0")
        conn.commit()
        print("  已添加 files.page_count_verified 列")
    except sqlite3.OperationalError:
        pass

    # v15 迁移：order_files 添加 page_range_original / page_range_truncated 列
    for col, col_type, default in [
        ("page_range_original", "TEXT", "''"),
        ("page_range_truncated", "INTEGER", "0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE order_files ADD COLUMN {col} {col_type} DEFAULT {default}")
            conn.commit()
            print(f"  已添加 order_files.{col} 列")
        except sqlite3.OperationalError:
            pass

    # v16 迁移：files 添加 md5 列（用于本地工具 MD5 缓存命中，避免重复下载）
    try:
        conn.execute("ALTER TABLE files ADD COLUMN md5 TEXT DEFAULT ''")
        conn.commit()
        print("  已添加 files.md5 列")
    except sqlite3.OperationalError:
        pass

    # v17 迁移：orders 添加 source 列（区分云端/本地来源）
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN source TEXT DEFAULT 'cloud'")
        conn.commit()
        print("  已添加 orders.source 列")
    except sqlite3.OperationalError:
        pass

    # v18 迁移：order_files 添加 image_orientation 列（图片打印方向: auto/landscape/portrait）
    try:
        conn.execute("ALTER TABLE order_files ADD COLUMN image_orientation TEXT DEFAULT 'auto'")
        conn.commit()
        print("  已添加 order_files.image_orientation 列")
    except sqlite3.OperationalError:
        pass

    # v18 迁移：order_files 添加 reject_reason 列（打回原因）
    try:
        conn.execute("ALTER TABLE order_files ADD COLUMN reject_reason TEXT DEFAULT ''")
        conn.commit()
        print("  已添加 order_files.reject_reason 列")
    except sqlite3.OperationalError:
        pass

    # v20 迁移：order_files 添加 locked_at 列（任务被锁定/accept 的时刻，P0-1/P1-8）
    # 用于：孤儿 printing 任务回收（按锁定时间而非创建时间，避免无限 push/回退循环）
    #       与超时判定（accept 后按 accept 时刻起算反馈超时）。
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(order_files)").fetchall()]
        if "locked_at" not in cols:
            conn.execute("ALTER TABLE order_files ADD COLUMN locked_at TEXT DEFAULT ''")
            conn.commit()
            print("  已添加 order_files.locked_at 列")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v21 迁移：files 添加 openid 列（文件归属校验，P2-10）
    # 每个上传都会在 files 表新建一行（MD5 去重只复用磁盘文件），openid 记录归属者，
    # 提交订单时校验 file_id 归属，防止跨用户引用他人文件。
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(files)").fetchall()]
        if "openid" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN openid TEXT DEFAULT ''")
            conn.commit()
            print("  已添加 files.openid 列")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v23 迁移：orders.source 语义升级 —— cloud/local → wechat/app/local（发起端标记）。
    # 历史云端订单无法区分旧版 APP 还是小程序，统一按小程序（wechat）回填；
    # 新提交由客户端显式携带 client 字段（wechat/app）。
    cols = [r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
    if "source" in cols:
        conn.execute("UPDATE orders SET source = 'wechat' WHERE source = 'cloud'")
        conn.commit()
        print("  orders.source 已升级: cloud → wechat（历史云端订单按小程序计）")

    # v22 迁移：orders 添加 client_request_id 列（提交幂等键，防双击/重试重复建单）
    # 小程序每次提交生成唯一 ID，后端对同 openid + 同 ID 且 10 分钟内的订单去重返回。
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
        if "client_request_id" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN client_request_id TEXT DEFAULT ''")
            conn.commit()
            print("  已添加 orders.client_request_id 列")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v29 迁移（D5）：给幂等键 client_request_id 加**部分唯一索引**。
    # 为什么：原实现是"先查再插"（submit_order 里 SELECT 之后才 INSERT），毫秒级并发双击
    # （小程序/APP 双击、弱网自动重试）两边都查不到对方就都插入 ⇒ 重复建单 = 重复印 + 重复计费。
    # ① 先去重：同一 (openid, client_request_id) 只保留**最早**一条，其余把该列清空 ——
    #    不动订单本身、不动 order_number，这些订单只是失去幂等键（重启后新提交仍会被正确拦）；
    # ② 再建**部分**唯一索引（空串不参与索引 ⇒ 未带幂等键的提交与历史数据不受影响）。
    # 两步都幂等，重复执行安全。
    try:
        _had_idx = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_orders_client_req'"
        ).fetchone()
        dup_cleared = conn.execute(
            """UPDATE orders SET client_request_id = ''
               WHERE client_request_id IS NOT NULL AND client_request_id != ''
                 AND id NOT IN (SELECT MIN(id) FROM orders
                                WHERE client_request_id IS NOT NULL AND client_request_id != ''
                                GROUP BY openid, client_request_id)"""
        ).rowcount
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_client_req
               ON orders(openid, client_request_id) WHERE client_request_id != ''"""
        )
        conn.commit()
        if dup_cleared:
            print(f"  已清空 {dup_cleared} 条重复的 client_request_id（保留最早一条）")
        if not _had_idx:
            print("  已建立 orders 幂等键唯一索引（idx_orders_client_req）")
    except sqlite3.OperationalError as e:
        print(f"  [WARN] 幂等键唯一索引建立失败（不影响启动）: {e}")

    # v24 迁移：订单归属标记 —— 谁处理了这笔订单 + 是否为管理员自行打印。
    #   owner_name     TEXT    归属管理员姓名（本地工具下拉选择；云端订单由打印机回报时盖章）
    #   is_admin_print INTEGER 1=管理员自行打印（非顾客订单）；0=顾客订单（接单打印）
    # 首次建列后一次性迁移：历史无标记订单全部记为管理员"霍楠"、管理员自行打印。
    # 注意：迁移 UPDATE 必须放在 ALTER 成功的同一分支内执行（仅在首次建列时跑一次），
    #       否则每次启动都会把新产生的未盖章云端订单（owner_name=''）错误地改记为霍楠自打。
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
        if "owner_name" not in cols and "is_admin_print" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN owner_name TEXT DEFAULT ''")
            conn.execute("ALTER TABLE orders ADD COLUMN is_admin_print INTEGER DEFAULT 0")
            conn.commit()
            conn.execute(
                "UPDATE orders SET owner_name = '霍楠', is_admin_print = 1 "
                "WHERE owner_name IS NULL OR owner_name = ''"
            )
            conn.commit()
            print("  已添加 orders.owner_name / orders.is_admin_print 列")
            print("  历史无标记订单已迁移至管理员“霍楠”名下（管理员自行打印）")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v26 迁移：orders 添加 print_started_at 列（订单实际开始打印的时刻）。
    # 用于对比「发起订单」与「订单开始打印」的时间差（数据传输/文件转换/队列等待/延迟自动打印所致）。
    # 本地工具只发 start_printing 信号不带时间，后端收到即用**服务器时钟**幂等写入（仅首次，不覆盖），
    # 与 created_at 同一时钟源 → 差值无漂移，等待时长由 calc_wait_seconds() 统一下发 wait_seconds。
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
        if "print_started_at" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN print_started_at TEXT DEFAULT ''")
            conn.commit()
            print("  已添加 orders.print_started_at 列")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v27 迁移：多设备接单 —— orders 记录「目标设备」(target_client) 与「实际接收设备」(received_client)。
    #   · target_client：移动端提交时指定的可接单设备（空 = 未指定，由任一接单设备接收）
    #   · received_client：任务实际被哪台设备推送/拉取/打印（首次写入，幂等）
    #   存量订单没有接收设备记录，统一回填为历史打印机「姚懿祥的设备（DESKTOP-EJGEB1V-a6c1a1365e）」。
    LEGACY_PRINTER_CLIENT = "DESKTOP-EJGEB1V-a6c1a1365e"
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
        added = False
        if "received_client" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN received_client TEXT DEFAULT ''")
            added = True
        if "target_client" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN target_client TEXT DEFAULT ''")
            added = True
        if added:
            # 存量订单统一归属历史打印机（迁移时刻一次性回填；此后新订单由接收流程写入）
            conn.execute(
                "UPDATE orders SET received_client = ? WHERE received_client = ''",
                (LEGACY_PRINTER_CLIENT,),
            )
            conn.commit()
            print("  已添加 orders.received_client / orders.target_client 列，存量订单归属历史打印机")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v28 迁移：order_files 添加 last_operator_client 列（**上次**领取该子任务的设备）。
    #   背景（🔴-D3）：事件处理器的归属校验写成「operator_client 为空即放行」，而
    #   operator_client 会被**断线回滚**（on_disconnect）与**孤儿回收**
    #   （recover_orphaned_printing_tasks）清空 —— 清空后任何持全局 printer token 的设备
    #   都能把别人的任务标成功/判失败、或抢占。operator_client 一清空就丢失了归属信息，
    #   所以另立一列专门记「上次是谁在打」，**回滚/回收都不清空**，供归属判定兜底。
    #   与 operator_client 的区别：operator_client = 当前领取者（可被清空）；
    #   last_operator_client = 最后一次领取者（只被新的领取动作覆盖）。
    #   存量行照抄当前 operator_client（此刻它就是"上次领取者"）。
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(order_files)").fetchall()]
        if "last_operator_client" not in cols:
            conn.execute("ALTER TABLE order_files ADD COLUMN last_operator_client TEXT DEFAULT ''")
            conn.execute(
                "UPDATE order_files SET last_operator_client = operator_client"
                " WHERE operator_client != '' AND last_operator_client = ''"
            )
            conn.commit()
            print("  已添加 order_files.last_operator_client 列（归属判定兜底，回滚不清空）")
    except sqlite3.OperationalError:
        pass  # 字段已存在

    # v16（2026-11-02）：「封面单独打印」与「排除末尾空白页」—— 这两个选项原先只有本地
    # 打印工具有，云端订单拿到设备端时直接丢失：顾客以为自己选了"封面单面"，实际按整份
    # 双面出纸，价格与出纸两边都对不上。见 docs/云打印系统架构.md 的同名小节。
    #   simplex_pages      ：需要**单面**打印的页（每页各占一张纸、背面留空），如 "1,16-23"
    #   blank_tail_exclude ：1 = 设备端把文件末尾的连续空白页排除后再打，价格按排除后的
    #                        页数重算（设备端回报 page_range_truncated 触发）
    try:
        _of_cols = [r[1] for r in conn.execute("PRAGMA table_info(order_files)").fetchall()]
        for _col, _ddl in (
            ("simplex_pages", "ALTER TABLE order_files ADD COLUMN simplex_pages TEXT DEFAULT ''"),
            ("blank_tail_exclude", "ALTER TABLE order_files ADD COLUMN blank_tail_exclude INTEGER DEFAULT 0"),
            # 🔴-B10（2026-11-10 体检）：实际打印配置同步失败必须留痕，等重算补丁来收。
            # 旧写法：on_print_success 先把子任务 commit 成 sent，随后 _sync_subtask_config()
            # 里改价异常被 `except: print + rollback()` 吞掉 —— 而 _recalc_prices_for_file 的
            # SQL 又跳过终态，于是这一单永远停在"估价"，重推/补算都不会再来。
            ("needs_repricing", "ALTER TABLE order_files ADD COLUMN needs_repricing INTEGER DEFAULT 0"),
        ):
            if _col not in _of_cols:
                conn.execute(_ddl)
                conn.commit()
                print("  已添加 order_files.%s 列（封面单独打印 / 排除末尾空白页）" % _col)
    except sqlite3.OperationalError:
        pass  # 列已存在

    # 确保历史打印机在设备注册表中（owner=姚懿祥），供「接单设备」列展示
    try:
        _dev = load_devices()
        if LEGACY_PRINTER_CLIENT not in _dev or not _dev[LEGACY_PRINTER_CLIENT].get("owner_name", ""):
            entry = _dev.get(LEGACY_PRINTER_CLIENT) or {}
            entry["device_name"] = entry.get("device_name", "") or "DESKTOP-EJGEB1V"
            entry["owner_name"] = entry.get("owner_name", "") or "姚懿祥"
            entry["last_seen"] = entry.get("last_seen", "")
            _dev[LEGACY_PRINTER_CLIENT] = entry
            save_devices(_dev)
            print("  设备注册表已补录历史打印机（姚懿祥）")
    except Exception:
        pass

    # 收支清算配置（单行 JSON blob，id 恒为 1；随 orders.db 一起被 backup.sh 备份）
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS finance_config (
            id         INTEGER PRIMARY KEY,
            data       TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    # 体检 🔴8（2026-11-10）：收支清算配置多端并发写，需要**服务端单调修订号**才能判冲突。
    # 旧实现只有 data/updated_at，而前端 `_saveToServer` 等的是 `res.server_version` ——
    # save_finance_config 从不返回它 ⇒ `conflict` 恒 false、冲突确认框永远不弹，
    # 两台设备（或同一人两个标签页）同时改账就是「后保存的把先保存的整份覆盖」。
    try:
        _fc_cols = [r[1] for r in conn.execute("PRAGMA table_info(finance_config)").fetchall()]
        if "rev" not in _fc_cols:
            conn.execute("ALTER TABLE finance_config ADD COLUMN rev INTEGER DEFAULT 0")
            conn.commit()
            print("  已添加 finance_config.rev 列（多端并发写的修订号）")
    except sqlite3.OperationalError:
        pass

    # ===== S2-01：高频查询补索引（原来只有幂等键那一条唯一索引，其余全是全表扫）=====
    # 依据：安全加固计划 §S2-01 / DDoS 报告 R5。逐条对应真实热点查询：
    #   · orders(openid, id)    —— /api/orders 默认路径 `WHERE openid=? ORDER BY id DESC`；
    #      索引键是 (openid, rowid)，同 openid 段内按 rowid 递增 ⇒ 反向扫即得倒序，不需要临时排序。
    #   · orders(created_at)    —— 收支清算 /api/admin/statistics/revenue 的时间区间扫描。
    #   · order_files(order_id) —— 订单列表中「每个父单查一次子任务」的 N+1、状态聚合、
    #      结算页的 `order_id IN (...)` 批量查询。
    #   · order_files(file_id)  —— 过期清理的两条 `SELECT DISTINCT file_id ... status IN (...)`、
    #      「删除我的文件」的活跃引用校验。
    #   · files(openid)         —— 存储用量 SUM(size)、按用户删除文件。
    # 全部 IF NOT EXISTS ⇒ 幂等；已存在的索引不会重建，重复启动零开销。
    # 放在所有迁移之后（files.openid 是迁移列，之前建会报 no such column）。
    _S2_INDEXES = (
        ("idx_orders_openid_id", "CREATE INDEX IF NOT EXISTS idx_orders_openid_id ON orders(openid, id)"),
        ("idx_orders_created", "CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)"),
        ("idx_order_files_order",
         "CREATE INDEX IF NOT EXISTS idx_order_files_order ON order_files(order_id)"),
        ("idx_order_files_file",
         "CREATE INDEX IF NOT EXISTS idx_order_files_file ON order_files(file_id)"),
        ("idx_files_openid", "CREATE INDEX IF NOT EXISTS idx_files_openid ON files(openid)"),
    )
    _have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()}
    for _name, _sql in _S2_INDEXES:
        try:
            conn.execute(_sql)
            if _name not in _have:
                print(f"  已建立查询索引（{_name}）")
        except sqlite3.OperationalError as e:
            print(f"  [WARN] 索引 {_name} 建立失败（不影响启动）: {e}")

    conn.commit()
    conn.close()


    # S1-03：启动时也做一次保留期清理（与 10 分钟定时任务双触发）。
    # ⚠ 必须放在**所有迁移之后**：放在 init_db 开头时后台线程会抢在
    #   `ALTER TABLE files ADD COLUMN retention_hours` 之前跑，直接报
    #   'no such column: retention_hours'（2026-10-23 首次部署实测撞到）。
    _cleanup_once_on_startup()

    # 🟡3：把上次进程的「兜底 start_print 重发了多少次 / 是否已停发」读回来。
    # 不读回来，后端一重启就重新开始刷 16 次 —— 对一张永远不接单的预约单毫无意义。
    _start_print_loaded = _load_start_print_attempts()
    if _start_print_loaded:
        print(f"  [SCHEDULER] 已恢复 {_start_print_loaded} 个预约单的兜底重发计数")

# ==================== 订单号生成 ====================

@app.route("/api/next_order_number", methods=["GET"])
def next_order_number():
    """本地打印工具获取下一个可用订单号（需 token 认证）。
    调用即分配，同时在数据库创建 reserved 状态占位记录，
    防止订单号被浪费。若后续未调用 /api/local_orders 提交，
    超时后由定时任务自动标记为 abandoned。

    **取号幂等（2026-10-25 现场）**：调用方可带 `request_id`（同一标签页/同一订单固定不变，
    由本地工具生成并随标签页持久化）。同一个 request_id 已经建过 reserved 占位单时，
    **直接返回那个订单号**，不再新建：
      现场 LaserJet 1020 机器上 `/api/next_order_number` 的响应在 TLS 层丢了
      (`SSLEOFError`)，服务端其实已经建好占位单 HN20261025-0003，客户端却按超时回落本地
      临时号；十几分钟后重连同步换号时又申请到 HN20261025-0004 ⇒ 云端留下一个**永远空的
      「已预留」0003**（结算页可见、还吃掉一个订单号）。带上 request_id 后，那次重连拿到
      的是 0003 本身，订单信息写进同一行，不再有多余的空预留单。
    只复用 `status='reserved'` 的行（纯占位/预留上报都还没定稿）：已完成/失败/取消的行
    绝不复用，否则会把新订单挂到旧单号上。
    """
    token = _get_printer_token()
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403

    # 取号幂等键（可选；旧版客户端不带）
    request_id = (request.args.get("request_id") or request.form.get("request_id") or "").strip()[:128]

    # 预留即记归属（与 local_orders 上报口径一致）：先把当前归属者写进占位单，
    # 这样即使预留后未打印上报（被标 abandoned），也不会留下 owner_name 为空的“无归属”残留。
    # owner_name/is_admin_print 可选携带：支持界面上“选中哪个归属者默认就是哪个”的语义，
    # 未携带时默认使用占位名 / 管理员自行打印（与 local_orders 的缺省值保持一致）。
    owner_name = (request.args.get("owner_name") or request.form.get("owner_name") or "").strip()
    is_admin_print_raw = request.args.get("is_admin_print") or request.form.get("is_admin_print")
    if is_admin_print_raw is not None:
        is_admin_print = 1 if str(is_admin_print_raw) not in ("", "0", "false", "False") else 0
    else:
        is_admin_print = 1  # 本地预留默认视为管理员自行打印
    owner_name = owner_name or DEFAULT_OWNER_NAME

    def _existing_row(conn_, req):
        """同一 request_id 的本地订单行（openid='local'；带状态，供判定能否复用）。

        查询失败（锁/表结构异常）一律当"没有"处理：这段只是**防重复建占位单的优化**，
        绝不能让取号本身 500（旧版没有这次查询，照样要能取到号）。
        """
        if not req:
            return None
        try:
            return conn_.execute(
                """SELECT order_number, status FROM orders
                   WHERE openid = 'local' AND client_request_id = ?
                   ORDER BY id DESC LIMIT 1""",
                (req,),
            ).fetchone()
        except Exception:
            return None

    conn = get_db()
    order_number = ""
    insert_req = request_id      # 写进新占位单的幂等键（可能因为"键已被终态单占用"而清空）
    try:
        # ① 幂等复用：上次取号的响应丢了/客户端重试 → 返回同一个号（不再新建占位单）
        row = _existing_row(conn, request_id)
        if row is not None and row["status"] == "reserved":
            try:
                app.logger.info("next_order_number 复用已预留订单号: request_id=%s → %s",
                                request_id, row["order_number"])
            except Exception:
                pass
            return jsonify({"success": True, "order_number": row["order_number"], "reused": True})
        if row is not None:
            # 这把钥匙已经绑在一张**终态**订单上（已完成/失败/取消…）：新订单必须换号，
            # 而且不能再用同一把钥匙（(openid, client_request_id) 是部分唯一索引）——
            # 否则会把新订单挂到旧单号上，或者插入直接撞唯一索引。
            insert_req = ""

        order_number = generate_order_number()
        # 创建占位订单记录（状态 reserved），后续 /api/local_orders 会更新为 sent
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            conn.execute(
                """INSERT INTO orders (file, copies, status, created_at, openid, order_number, source,
                                       owner_name, is_admin_print, client_request_id)
                   VALUES (?, 1, 'reserved', ?, 'local', ?, 'local', ?, ?, ?)""",
                ("（预留位置）", created_at, order_number, owner_name, is_admin_print, insert_req),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # 同一 request_id 并发取号：另一个请求先插进去了 ⇒ 回滚后返回它那个号
            conn.rollback()
            row = _existing_row(conn, insert_req)
            if row is not None and row["status"] == "reserved":
                return jsonify({"success": True, "order_number": row["order_number"],
                                "reused": True})
            # 其余完整性冲突（极少）：占位单插不进去不影响订单号分配
        except Exception:
            pass  # 占位插入失败不影响订单号分配
    finally:
        conn.close()

    return jsonify({"success": True, "order_number": order_number})


# ==================== 订单号生成（内部函数）====================

_ORDER_COUNTER_LOCK = threading.Lock()
ORDER_COUNTER_FILE = os.path.join(UPLOAD_DIR, "order_counter.json")


def _find_order_by_client_request_id(openid: str, client_request_id: str,
                                      minutes: "int | None" = 10):
    """按幂等键回查既有订单，返回 dict 或 None。

    minutes=None → **不限时间窗**（并发兜底路径用：同一把钥匙刚被别人抢先插入，无论多久都算重复）。
    单独开连接查询（不复用建单事务的连接），因此读到的是**已提交**的对方订单。
    """
    if not openid or not client_request_id:
        return None
    conn = get_db()
    try:
        if minutes is None:
            row = conn.execute(
                """SELECT id, order_number, status, created_at FROM orders
                   WHERE openid = ? AND client_request_id = ?
                   ORDER BY id DESC LIMIT 1""",
                (openid, client_request_id),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT id, order_number, status, created_at FROM orders
                   WHERE openid = ? AND client_request_id = ? AND created_at > ?
                   ORDER BY id DESC LIMIT 1""",
                (openid, client_request_id,
                 (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")),
            ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _duplicate_order_response(dup_row: dict, data: dict):
    """重复提交的统一响应（与正常建单同形；前端靠 is_duplicate 区分，可提示"已提交过"）。

    S2-02：不再回显请求体 `data`（放大面，客户端不读）；`files` 也不回 ——
    重复提交的调用方只需要订单号与状态。"""
    return jsonify({
        "success": True,
        "is_duplicate": True,
        "message": "检测到重复提交，已返回原订单",
        "order_id": dup_row["id"],
        "order_number": dup_row["order_number"],
    })


def generate_order_number():
    """生成订单号 HN{YYYYMMDD}-{4位当日序号}，线程安全，跨天自动归零。"""
    today = datetime.now().strftime("%Y%m%d")

    with _ORDER_COUNTER_LOCK:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        try:
            with open(ORDER_COUNTER_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {"date": today, "counter": 0}

        if data.get("date") != today:
            data = {"date": today, "counter": 0}

        data["counter"] += 1
        counter = data["counter"]

        with open(ORDER_COUNTER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())  # P2-15：写盘并落盘，防断电/崩溃丢失已分配序号

    return f"HN{today}-{counter:04d}"


# ==================== 价格计算 ====================

# ---- 「封面单独打印」/「排除末尾空白页」的同口径工具（2026-11-02 用户需求） ----
# 口径唯一来源：local_print_tool/manual_duplex.py 的 split_simplex_runs / charge_units。
# 单面页各占一张纸（背面留空），其余页两页一张纸；双面段页数为奇数时末页按单面计费。
# 后端 / 小程序 / APP / 本地工具必须算出**同一个张数与金额**，改这里先改口径文档。

_SIMPLEX_MAX_CHARS = 500


def _iter_range_tokens(raw):
    """把 '1,2-3' 切成 [(start, end|None)]；无法解析的段以 (None, 原文) 返回。

    与 _parse_page_range_set 的差别：那个是"打印够用"的宽松解析，会静默丢掉非法段；
    「封面单独打印」的页填错了必须打回（否则顾客以为封面单面、实际整份双面还多收钱），
    所以这里把非法段原样交回给调用方去决定。
    """
    out = []
    text = str(raw or "").strip()
    for ch in ("、", "，", "；", " ", "\u3000"):
        text = text.replace(ch, ",")
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                start, end = int(a), int(b)
            except ValueError:
                out.append((None, part))
                continue
            if start < 1 or end <= start:
                out.append((None, part))
                continue
            out.append((start, end))
        else:
            try:
                n = int(part)
            except ValueError:
                out.append((None, part))
                continue
            if n < 1:
                out.append((None, part))
                continue
            out.append((n, None))
    return out


def _format_page_selection(pages):
    """页集合 → '1,2,16-23'（升序、连续段合并）—— 与本地 format_page_selection 同口径。"""
    nums = sorted({int(p) for p in pages})
    if not nums:
        return ""
    parts = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(str(start) if start == prev else "%d-%d" % (start, prev))
        start = prev = n
    parts.append(str(start) if start == prev else "%d-%d" % (start, prev))
    return ",".join(parts)


def _selected_page_list(page_count, page_range=""):
    """页码范围 → 升序页列表（空范围 / 全非法范围 = 全部页，与 _count_pages_in_range 一致）。"""
    if page_count <= 0:
        return []
    if (page_range or "").strip():
        pages = sorted(_parse_page_range_set(page_range, page_count))
        if pages:
            return pages
    return list(range(1, page_count + 1))


def _simplex_page_set(page_count, simplex_pages=""):
    """「封面单独打印」页集合（宽松解析：用于已入库数据与统计，提交校验另走 _simplex_pages_checked）。"""
    if page_count <= 0 or not (simplex_pages or "").strip():
        return set()
    return _parse_page_range_set(simplex_pages, page_count)


def _charge_units(page_count, page_range="", simplex_pages=""):
    """双面打印的纸张拆分 → (双面张数, 单面张数)。逐字对齐 manual_duplex.charge_units。

    按"这一页是否属于单面页"把页码范围切成连续段：单面段每页一张（背面留空）；
    双面段 len//2 张 + len%2 张单面收尾（末页另占一张纸）。
    """
    pages = _selected_page_list(page_count, page_range)
    if not pages:
        return (0, 0)
    simplex = _simplex_page_set(page_count, simplex_pages)
    dup = single = 0
    i, n = 0, len(pages)
    while i < n:
        flag = pages[i] in simplex
        j = i
        while j < n and (pages[j] in simplex) == flag:
            j += 1
        seg = j - i
        if flag:
            single += seg
        else:
            dup += seg // 2
            single += seg % 2
        i = j
    return (dup, single)


def _simplex_pages_checked(raw, page_range, page_count, page_count_verified):
    """校验并归一化「封面单独打印」页串 → (归一化字符串, 打回原因)。

    打回原因（逐字，与本地工具提示同一套）：
      「封面单独打印」页码格式不正确：{raw}（应为如 1-5 或 7）
      「封面单独打印」第 {n} 页超出文件总页数（文件共 {pc} 页）
      「封面单独打印」第 {n} 页不在页码范围「{range}」内
    单面打印时该配置无意义，调用方负责跳过。
    """
    text = (raw or "").strip()[:_SIMPLEX_MAX_CHARS]
    if not text:
        return "", ""
    tokens = _iter_range_tokens(text)
    bad = [part for (start, part) in tokens if start is None]
    if bad:
        return "", "「封面单独打印」页码格式不正确：%s（应为如 1-5 或 7）" % bad[0]
    pages = set()
    for start, end in tokens:
        if start is None:
            continue
        if end is None:
            pages.add(start)
        else:
            pages.update(range(start, end + 1))
    if not pages:
        return "", "「封面单独打印」页码格式不正确：%s（应为如 1-5 或 7）" % text
    if page_count_verified and page_count > 0:
        over = sorted(p for p in pages if p > page_count)
        if over:
            return "", "「封面单独打印」第 %d 页超出文件总页数（文件共 %d 页）" % (over[0], page_count)
        scope = set(_selected_page_list(page_count, page_range))
        outside = sorted(p for p in pages if p not in scope)
        if outside:
            return "", ("「封面单独打印」第 %d 页不在页码范围「%s」内"
                        % (outside[0], (page_range or "").strip()))
    return _format_page_selection(pages), ""


def _row_simplex_pages(row):
    """从 sqlite3.Row / dict 里安全取 simplex_pages（老库/老查询没有该列时当空）。"""
    try:
        return (row["simplex_pages"] or "") if row["simplex_pages"] is not None else ""
    except (IndexError, KeyError, TypeError):
        return ""


def _row_blank_tail_exclude(row):
    """从 sqlite3.Row / dict 里安全取 blank_tail_exclude（缺列当 0）。"""
    try:
        return int(row["blank_tail_exclude"] or 0)
    except (IndexError, KeyError, TypeError, ValueError):
        return 0



def calculate_price(page_count, duplex, page_range="", prices=None, simplex_pages=""):
    """根据有效打印页数（范围过滤后）和双面模式计算价格（单份文件，不含份数倍率）。
    单面打印: simplex_price 元/页（默认 0.2）
    双面打印: duplex_price 元/张（默认 0.3，每张纸可印两页，奇数页最后一张按单面价计费）
    价格来源 pricing.json（与 /api/pricing、前端计费显示、本地打印工具一致，
    在「收支统计 → 设置」中统一维护）；读取失败回退默认 0.2/0.3。
    page_range 为空 → 按整份页数计。
    simplex_pages：「封面单独打印」的页（每页各占一张纸、背面留空）。给了就按
    「单面段每页一张 + 双面段两页一张」摊纸（见 _charge_units），与本地工具同口径；
    留空时行为与老版本**逐字一致**（ceil(eff/2) 双面张 + 奇数末页单面）。
    prices：可选 (单面, 双面) 单价。批量计价（如 submit_order 的文件循环）应在循环外
    调一次 `_load_paper_prices()` 传进来，避免每个文件各走一次读盘/缓存查询。"""
    if page_count <= 0:
        return 0.0
    simplex_price, duplex_price = prices or _load_paper_prices()
    effective = _count_pages_in_range(page_range or "", page_count)
    if duplex == "on":
        dup_sheets, single_sheets = _charge_units(page_count, page_range, simplex_pages)
        if dup_sheets + single_sheets <= 0:
            # 兜底：页数/范围异常时退回老口径，绝不因口径函数空转而算成 0 元
            dup_sheets = math.ceil(effective / 2)
            single_sheets = effective % 2
        price = dup_sheets * duplex_price + single_sheets * simplex_price
    else:
        price = effective * simplex_price
    return round(price, 2)


_paper_prices_cache = {"sig": None, "data": None}
_paper_prices_lock = threading.Lock()


def _load_paper_prices() -> tuple[float, float]:
    """读取 pricing.json 的单双面价格，失败/非法回退默认 0.2/0.3。

    S2-01：带进程内缓存（按文件指纹失效）—— 原实现**每一笔计价**都读盘 + json.loads，
    而计价出现在提交订单的每个文件、每个订单的金额重算、结算页每次刷新里。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing.json")
    cfg = _cached_json_read(path, _paper_prices_cache, _paper_prices_lock)
    if not isinstance(cfg, dict):
        return 0.2, 0.3
    try:
        sp = float(cfg.get("simplex_price", 0.2) or 0.2)
        dp = float(cfg.get("duplex_price", 0.3) or 0.3)
    except (TypeError, ValueError):
        return 0.2, 0.3
    return (sp if sp > 0 else 0.2), (dp if dp > 0 else 0.3)


# ==================== 订单状态聚合 ====================

# 状态优先级：超时/失败 > 打印中/排队中 > 已完成 > 被打回 > 已取消
# 用于把多个子任务 (order_files) 的状态聚合为父订单 (orders) 的状态
# 2026-11：新增 "timeout"（超时）—— 与 failed 同列（优先级 9），结算口径里也按失败处理，
# 但**不**进 NO_LONGER_PRINTABLE_STATUSES：设备迟到的 print_success 仍能把它救回 sent。
_STATUS_PRIORITY = {"timeout": 9, "failed": 9, "printing": 8, "waiting": 7, "accepted": 6,
                    "downloading": 5, "offline_unknown": 5, "scheduled": 4, "queued": 4,
                    "sent": 3, "abandoned": 2, "rejected": 1, "canceled": 0}

# 「超时」状态（2026-11）：设备已确认但迟迟不回报 / 断线未回报 / 排队过期 —— 判定为
# "这一单不会再有结果了"，与 failed 一样是终态（结算里算失败、不再参与推送与排队）。
# ⚠ 但**不是** NO_LONGER_PRINTABLE_STATUSES：设备是最终裁决者，迟到的 print_success
#   仍按 on_print_success 的白名单把它救回 sent（现场：断线恢复后补打成功的那单）。
TIMEOUT_STATUS = "timeout"


# 「这单不该再有纸出来」的终态（2026-10-18 审查 🔴 的统一判据 = 「云端不要再主动让它打印」）。
# 人工订正成这三种状态后：① 必须通知设备端停止/丢弃在飞队列（admin_order_status →
# _notify_order_no_longer_printable）；② 预约单到点也不许开始打印（start_printing）。
# ⚠ 这三态**不是**对设备的最终裁决（2026-10-18 用户口径）：设备若仍把纸打出来并如实回报成功
# （典型是它当时离线、没收到通知），云端按回报记 sent —— 见 on_print_success 的白名单与日志。
# failed 刻意不在本常量里：它同样可以被设备迟到的成功回报救回（2026-09 的既有决定）。
NO_LONGER_PRINTABLE_STATUSES = ("canceled", "abandoned", "rejected")


def aggregate_order_status(conn, order_id):
    """根据 order_files 的状态聚合父订单状态：
    - 全部 sent → sent
    - 全部 rejected → rejected
    - 混合终态：用户主动取消优先 > 超时 > 已完成 > 断线未知 > 已确认 > 被打回 > 打回失败
    - 其余取优先级最高者（printing 优先于 queued；timeout/failed 优先级 9）
    - 无子任务时保持原状态

    2026-11：timeout（超时）是终态但不是 NO_LONGER_PRINTABLE —— 进"混合终态"集合是为了
    让它参与聚合（否则会被当成活跃状态取 max）；与 failed 一样**压过 sent**：一个子任务
    超时、另一个打印完成时，父单显示"超时"（整单并未真正完成，不能给顾客看"已完成"）。
    """
    rows = conn.execute(
        "SELECT status FROM order_files WHERE order_id = ?", (order_id,)
    ).fetchall()
    if not rows:
        return None
    statuses = [r["status"] for r in rows]
    if all(s == "sent" for s in statuses):
        return "sent"
    if all(s == "rejected" for s in statuses):
        return "rejected"
    if all(s in ("sent", "accepted", "offline_unknown", "abandoned", "canceled", "rejected",
                 "timeout") for s in statuses):
        # 混合终态：用户主动取消优先于已完成——
        # 已取消订单即使有部分文件在取消前已打印完成，父订单仍显示"已取消"
        #（否则取消后迟到的 print_success 会把聚合状态刷回 sent，用户看到"已完成"）
        if any(s == "canceled" for s in statuses):
            return "canceled"
        # 超时压过「已完成」：部分超时 + 部分完成 ≠ 整单完成（与 failed 同口径）
        if any(s == "timeout" for s in statuses):
            return "timeout"
        if any(s == "sent" for s in statuses):
            return "sent"
        if any(s == "offline_unknown" for s in statuses):
            return "offline_unknown"
        if any(s == "accepted" for s in statuses):
            return "accepted"
        if any(s == "abandoned" for s in statuses):
            return "abandoned"
        if any(s == "rejected" for s in statuses):
            return "rejected"
        return "canceled"
    # 取优先级最高的非终态
    active = [s for s in statuses if s not in ("sent", "accepted", "offline_unknown",
                                               "abandoned", "canceled", "rejected", "timeout")]
    if not active:
        return "sent"
    return max(active, key=lambda s: _STATUS_PRIORITY.get(s, 0))


def refresh_order_status(conn, order_id):
    """重算并写入父订单的聚合状态，返回新状态（无子任务时返回 None）"""
    new_status = aggregate_order_status(conn, order_id)
    if new_status:
        conn.execute(
            "UPDATE orders SET status = ? WHERE id = ?", (new_status, order_id)
        )
    return new_status


def order_status_reason(conn, order_id):
    """订单级「为什么没打出来」：取该订单子任务里**第一条非空** reject_reason（无则 ''）。

    为什么要订单级的一个字段：订单详情/结算明细展示的是父订单的状态（聚合值），
    但原因只存在于子任务行上。前端拿到 status='timeout' 却没有任何一句话解释，用户只能猜；
    给一个订单级的 status_reason（子任务级 reject_reason 照旧保留，一个文件一条原因），
    列表页/详情页一行就能显示。多文件订单只取第一条：真要看每个文件的原因，
    前端仍可读 order.files[i].reject_reason。
    """
    row = conn.execute(
        "SELECT reject_reason FROM order_files WHERE order_id = ?"
        " AND reject_reason IS NOT NULL AND TRIM(reject_reason) != ''"
        " ORDER BY id ASC LIMIT 1",
        (order_id,),
    ).fetchone()
    return (row["reject_reason"] or "").strip() if row else ""


def calc_wait_seconds(created_at, print_started_at):
    """计算「下单 → 实际开始打印」的等待秒数（后端权威时钟）。

    created_at（下单时）与 print_started_at（收到 start_printing 时）两端都由服务器写入，
    故差值不存在跨设备时钟漂移；本地工具只发信号、不上报时间，杜绝客户端时钟不准导致
    等待时长失真（甚至为负）的问题。

    返回 int 秒数；任一端缺失、格式异常或结果为负（脏数据）→ None。
    """
    if not created_at or not print_started_at:
        return None
    try:
        t0 = datetime.strptime(str(created_at)[:19], "%Y-%m-%d %H:%M:%S")
        t1 = datetime.strptime(str(print_started_at)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None
    delta = int((t1 - t0).total_seconds())
    return delta if delta >= 0 else None


def expire_stale_queued_orders():
    """防滥用：淘汰排队超过 queued_timeout_hours 的子任务（标记 timeout 并刷新父订单状态）。
    目的：释放被 queued 状态钉住的磁盘文件——cleanup_expired_files 会跳过活跃状态
    （含 queued）引用的文件；若无打印机认领，攻击者可用排队订单让文件永驻服务器。
    超时淘汰后状态变为 timeout（非活跃状态），清理任务即可删除对应文件。
    2026-11：原先标 failed，与"打印失败"混为一谈（探测里看不出是排队过期）；
    改标 timeout 并写 reject_reason，用户/结算看到的是"超时"而不是"失败"。"""
    timeout_hours = _SECURITY["queued_timeout_hours"]
    if timeout_hours <= 0:
        return
    cutoff = datetime.now() - timedelta(hours=timeout_hours)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn = get_db_conn()
        rows = conn.execute(
            "SELECT id, order_id, operator_client FROM order_files"
            " WHERE status = 'queued' AND created_at < ?",
            (cutoff_str,),
        ).fetchall()
        if not rows:
            conn.close()
            return
        # 🟡10：淘汰时要写清「是没人接单，还是设备一直不在线」。
        # 以前一律写「排队超过 N 小时未打印」，用户看到只会以为系统卡了，
        # 实际最常见的根因是那台机器根本没开机 —— 排查方向完全不同。
        try:
            _online_now = set(get_active_clients() or [])
        except Exception:
            _online_now = set()
        order_ids = set()
        for r in rows:
            _oc = str(r["operator_client"] or "")
            if _oc and _oc not in _online_now:
                _why = f"（目标设备「{_oc}」期间一直不在线）"
            elif _oc and _oc in _online_now:
                _why = f"（目标设备「{_oc}」在线但一直没接这一单）"
            elif not _online_now:
                _why = "（期间没有任何接单设备在线：请检查设备是否开机/登录）"
            else:
                _why = "（当时有设备在线，但没有设备认领这一单）"
            conn.execute(
                "UPDATE order_files SET status = ?, reject_reason = ? WHERE id = ?",
                (TIMEOUT_STATUS,
                 f"排队超过 {timeout_hours} 小时未打印{_why}，已超时取消", r["id"]),
            )
            order_ids.add(r["order_id"])
        for oid in order_ids:
            refresh_order_status(conn, oid)
        conn.commit()
        conn.close()
    print(f"[SECURITY] 已淘汰 {len(rows)} 个排队超时子任务（> {timeout_hours} 小时）")


def expire_stale_scheduled_orders():
    """预约单到点后长期没有任何设备把它打出来 ⇒ 超时取消，释放磁盘文件。

    2026-11-10 体检（🔴12）：`expire_stale_queued_orders()` 只扫 queued，而 scheduled / waiting
    出现在 `cleanup_expired_files()` 的 active_statuses 里 —— 于是「预约到点、目标设备永远不出现」
    这一单会无限期把文件钉在服务器上：既不打、也不清理、也不会超时。现场 HN20261110-0005
    就是这类（客户端挂起后状态机已结束，服务端子任务一直 waiting）。

    计时基准：`orders.scheduled_at`（到点之后才开始计时——提前几天下一周后的预约是正常用法，
    绝不能按 created_at 判），为空时退回子任务 `created_at`。阈值沿用
    `security.queued_timeout_hours`，与排队超时同一把尺子。

    口径：只改 scheduled / waiting 两种「还没开始打」的状态；标 `timeout`（不是 failed，
    结算页显示「超时」）+ 写 reject_reason。timeout **不在** NO_LONGER_PRINTABLE_STATUSES，
    所以设备日后真把纸打出来并回报成功时，仍按 on_print_success 白名单救回 sent。
    """
    timeout_hours = _SECURITY["queued_timeout_hours"]
    if timeout_hours <= 0:
        return
    cutoff_str = (datetime.now() - timedelta(hours=timeout_hours)).strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn = get_db_conn()
        rows = conn.execute(
            """SELECT of.id, of.order_id
               FROM order_files of
               JOIN orders o ON of.order_id = o.id
               WHERE of.status IN ('scheduled', 'waiting')
                 AND o.schedule_mode != 'now'
                 AND o.status NOT IN ('canceled', 'abandoned', 'rejected')
                 AND COALESCE(NULLIF(o.scheduled_at, ''), of.created_at) < ?""",
            (cutoff_str,),
        ).fetchall()
        if not rows:
            conn.close()
            return
        order_ids = set()
        for r in rows:
            conn.execute(
                "UPDATE order_files SET status = ?, reject_reason = ? WHERE id = ?",
                (TIMEOUT_STATUS,
                 f"预约到点后 {timeout_hours} 小时仍未打印（设备未接单），已超时取消",
                 r["id"]),
            )
            order_ids.add(r["order_id"])
        for oid in order_ids:
            refresh_order_status(conn, oid)
        conn.commit()
        conn.close()
    print(f"[SCHEDULER] 已超时取消 {len(rows)} 个长期未打印的预约子任务（> {timeout_hours} 小时）")


def release_stuck_frozen_orders():
    """冻结的预约单自愈（2026-11-10 体检 🔴13）。

    现场：`on_download_delayed` 把订单置 ``schedule_frozen=1, scheduled_at=''``，而**唯一的解冻点**
    是 `on_file_ready` 里「父单冻结 + 该单所有子任务都就绪」那一段。可只要设备掉线 / 上传失败 /
    这个事件从此不再来，订单就永久冻结：`process_scheduled_orders` 见冻结直接 continue、
    `expire_stale_scheduled_orders` 只扫 scheduled/waiting（downloading 扫不到），
    于是既不打印也不失败，零日志、无入口 —— 顾客只会看到「预约到点了但没打」。

    自愈口径（冻结超过 ``FROZEN_STUCK_TIMEOUT``）：
      * 所有子任务已就绪 ⇒ 直接解冻并重排到 now+30s（等价于迟到的 file_ready），发 start_print；
      * 还有没就绪的 ⇒ 把这几份标 ``timeout`` 并写明原因（**不是 failed**：设备日后真打出来
        仍能被 print_success 救回），然后解冻，让订单有个结论、文件也进入正常清理流程。
    旧数据没有 ``schedule_frozen_at`` ⇒ 退回 ``scheduled_at`` / ``created_at`` 判断，不至于永远漏掉。
    """
    cutoff_str = (datetime.now() - timedelta(seconds=FROZEN_STUCK_TIMEOUT)).strftime("%Y-%m-%d %H:%M:%S")
    resume = []          # [(order_id, target_str, ready_task_ids)]
    resolved = []        # [(order_id, n_timeout)]
    with db_lock:
        conn = get_db_conn()
        try:
            rows = conn.execute(
                """SELECT id, schedule_frozen_at, scheduled_at, created_at
                   FROM orders
                   WHERE COALESCE(schedule_frozen, 0) = 1
                     AND schedule_mode != 'now'
                     AND status NOT IN ('canceled', 'abandoned', 'rejected', 'sent', 'timeout')""",
            ).fetchall()
            if rows:
                conn.execute("BEGIN IMMEDIATE")
            for o in rows:
                oid = o["id"]
                basis = (str(o["schedule_frozen_at"] or "") or str(o["scheduled_at"] or "")
                         or str(o["created_at"] or ""))
                if not basis or basis >= cutoff_str:
                    continue
                if _all_subtasks_ready(conn, oid):
                    target_str = (datetime.now() + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute(
                        "UPDATE orders SET schedule_frozen = 0, schedule_frozen_at = '',"
                        " scheduled_at = ? WHERE id = ?",
                        (target_str, oid),
                    )
                    ready = [r["id"] for r in conn.execute(
                        "SELECT id FROM order_files WHERE order_id = ? AND status = 'waiting'",
                        (oid,)).fetchall()]
                    resume.append((oid, target_str, ready))
                else:
                    stuck = conn.execute(
                        "SELECT id FROM order_files WHERE order_id = ?"
                        " AND status IN ('scheduled', 'downloading')",
                        (oid,)).fetchall()
                    for r in stuck:
                        conn.execute(
                            "UPDATE order_files SET status = ?, reject_reason = ?, locked_at = ''"
                            " WHERE id = ? AND status IN ('scheduled', 'downloading')",
                            (TIMEOUT_STATUS,
                             f"预约到点后文件 {FROZEN_STUCK_TIMEOUT // 60} 分钟内一直没就绪（设备没传上来），"
                             "已超时取消这一份",
                             r["id"]),
                        )
                    conn.execute(
                        "UPDATE orders SET schedule_frozen = 0, schedule_frozen_at = '' WHERE id = ?",
                        (oid,),
                    )
                    refresh_order_status(conn, oid)
                    resolved.append((oid, len(stuck)))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    for oid, target_str, ready in resume:
        print(f"[UNFREEZE] 订单 #{oid} 冻结超过 {FROZEN_STUCK_TIMEOUT // 60} 分钟但文件已就绪，"
              f"解冻并重设 {target_str}")
        if ready:
            try:
                emit("start_print", {"order_id": oid, "task_ids": ready,
                                     "scheduled_at": target_str,
                                     "scheduled_ts": _iso_to_ts(target_str)})
            except Exception as e:
                print(f"  [WARN] 解冻后发 start_print 失败: {e}")
    for oid, n in resolved:
        print(f"[UNFREEZE] 订单 #{oid} 冻结超过 {FROZEN_STUCK_TIMEOUT // 60} 分钟、{n} 份文件仍没传上来，"
              f"已标超时并解冻（设备日后真打出来仍可救回 sent）")


# ==================== 原子任务领取 ====================


def fetch_and_lock_task(client_id):
    """原子化地获取一个 queued 任务并立即锁定为 printing，返回完整任务字典或 None。
    使用全局 db_lock 确保多台打印机并发拉取时不会重复分配同一任务。
    2026-12：多设备接单 —— 只拉取「指定给本设备」的任务（target_client=本机），
    以及「未指定目标」的任务（仅启用接单的设备可拉取，其他设备不参与分配）。"""
    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT of.id FROM order_files of
                   JOIN orders o ON of.order_id = o.id
                   WHERE of.status = 'queued'
                     AND (o.target_client = ? OR (o.target_client = '' AND ? = 1))
                   ORDER BY of.created_at ASC LIMIT 1""",
                (client_id, 1 if is_claiming(client_id) else 0),
            ).fetchone()
            if not row:
                conn.rollback()
                return None
            task_id = row["id"]
            # 锁定任务并记录领取者（locked_at = 锁定时刻，P0-1：孤儿回收/超时判定以它为准，
            # 不再依赖 created_at，避免"任务被反复 push/回退导致 created_at 很旧被误回收"的循环）
            conn.execute(
                "UPDATE order_files SET status = 'printing', operator_client = ?, locked_at = ?, last_operator_client = ? WHERE id = ?",
                (client_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), client_id, task_id)
            )
            # 刷新父订单聚合状态
            parent_row = conn.execute(
                "SELECT order_id FROM order_files WHERE id = ?", (task_id,)
            ).fetchone()
            if parent_row:
                refresh_order_status(conn, parent_row["order_id"])
                # 记录实际接收设备（首次写入，幂等）
                conn.execute(
                    "UPDATE orders SET received_client = COALESCE(NULLIF(received_client, ''), ?) WHERE id = ?",
                    (client_id, parent_row["order_id"]),
                )
            conn.commit()
            # 重新查询完整数据返回（含父订单信息 + 文件 MD5）
            full_task = conn.execute(
                """SELECT of.*, o.order_number, o.delivery_enabled, o.delivery_location,
                          o.urgency, o.cover_page, o.cover_page_price, o.auto_print,
                          o.owner_name, o.is_admin_print, o.openid, o.remark, o.source,
                          f.md5 as source_md5, f.retention_hours as retention_hours
                   FROM order_files of
                   LEFT JOIN orders o ON of.order_id = o.id
                   LEFT JOIN files f ON of.file_id = f.id
                   WHERE of.id = ?""",
                (task_id,),
            ).fetchone()
            return dict(full_task)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ==================== 定时任务：扫描并推送打印任务 ====================


def process_pending_orders():
    """扫描排队中的子任务（order_files）：当目标打印机客户端上线时，推送排队任务。
    2026-12：多设备接单 —— 订单指定了目标设备（target_client）则只发往该设备，
    未指定则发往任一在线接单设备；目标设备离线 → 保持排队等其上线。"""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT of.id AS of_id, of.order_id, of.file_id, of.file_name,
               of.copies, of.page_range, of.duplex, of.image_orientation,
               of.simplex_pages, of.blank_tail_exclude,
               o.auto_print, o.target_client
        FROM order_files of
        JOIN orders o ON of.order_id = o.id
        WHERE of.status = 'queued'
        ORDER BY of.created_at ASC
        """
    ).fetchall()
    conn.close()

    if not rows:
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    online = set(get_active_clients())
    claiming_ids = get_claiming_device_ids()
    print(f"\n[{now_str}] 扫描到 {len(rows)} 个排队子任务, {len(online)} 个活跃客户端, "
          f"{len(claiming_ids)} 台可接单设备")

    if not claiming_ids:
        print("  无启用接单的打印机，订单保持排队（等待接单设备上线）")
        return

    for row in rows:
        of_id = row["of_id"]
        order_id = row["order_id"]
        file_id = row["file_id"]
        file_name = row["file_name"]
        copies = row["copies"]
        page_range = row["page_range"] or ""
        duplex = row["duplex"]
        target_client = row["target_client"] or ""

        # 确定目标设备：指定设备 → 仅该设备（须在接单集合且在线）；未指定 → 任一在线接单设备
        if target_client:
            if target_client not in claiming_ids or target_client not in online:
                print(f"  [WAIT] 子任务 #{of_id}: 目标设备 {target_client} 未在线/未接单，保持排队")
                continue
            dest_client = target_client
        else:
            dest_client = next((c for c in claiming_ids if c in online), None)
            if not dest_client:
                print(f"  [WAIT] 子任务 #{of_id}: 无在线接单设备，保持排队")
                continue

        # 查找文件路径
        file_path = None
        if file_id:
            conn = get_db()
            frow = conn.execute("SELECT path, original_name FROM files WHERE id = ?", (file_id,)).fetchone()
            conn.close()
            if frow and os.path.exists(frow["path"]):
                file_path = frow["path"]
                # 文件名以 order_files.file_name（顾客看到的原始名）为准；files.original_name
                # 是**上传时的临时名**（小程序 tmp_xxx.pdf），只在订单元数据为空时兜底。
                # 2026-10-25 现场 HN20261025-0005：同一订单两个文件，走 HTTP 拉单的那个显示
                # 「示波器的使用.pdf」，走本推送的这个显示「tmp_3de4….pdf」—— 两条通道口径
                # 不一致，操作员在标签页里看到的就是一串乱码名。
                if not (file_name or "").strip():
                    file_name = frow["original_name"]

        if not file_path:
            print(f"  [FAIL] 子任务 #{of_id}: 文件不存在")
            conn = get_db()
            # 2026-11：失败必须带原因（用户/结算页要看得出"为什么没打"），
            # 这条不是超时（文件真的没了），所以保持 failed 而不是 timeout。
            _retry_on_lock(
                conn.execute,
                "UPDATE order_files SET status = 'failed', reject_reason = ?"
                " WHERE id = ? AND status = 'queued'",
                ("订单文件已失效（服务器上找不到该文件），无法打印，请联系客服重传", of_id),
            )
            refresh_order_status(conn, order_id)
            _retry_on_lock(conn.commit)
            conn.close()
            continue

        pushed = push_print_task_to_client(of_id, file_id, file_name, copies, duplex,
                                           page_range, dest_client,
                                           auto_print=bool(row["auto_print"]),
                                           image_orientation=row["image_orientation"] or "auto",
                                           simplex_pages=_row_simplex_pages(row),
                                           blank_tail_exclude=_row_blank_tail_exclude(row))
        if pushed:
            continue

        # 推送失败：保持 queued，等待下次扫描
        print(f"  [WAIT] 子任务 #{of_id}: 推送失败，保持排队")


# ── 预约单 start_print 兜底推送的节流状态（2026-11-10 现场 HN20261110-0005）──────────
# 症状：一张预约单到点后每 30 秒收到一次「开始打印指令」，却一次也没打出来，日志刷到几百行。
# 根因链（三段都真实存在，改任何一段都要一起看）：
#   ① 本机是手动双面机型（HP LaserJet 1020）。预约单到点命中 `_hold_for_manual_duplex()`
#      后**故意挂起**（顾客按双面付了钱，没人翻纸就不能打），并结束自己的预约状态机；
#   ② 服务端这边子任务仍是 waiting、scheduled_at 已过、未冻结 ⇒ 每 30 秒发一次兜底 start_print；
#   ③ 设备端状态机已结束 ⇒ 收到只记一行「收到开始打印指令」就丢弃。于是**指令永远无效、推送永远不停**
#      —— 现场看上去就是「开始打印指令失效」，其实是一直在被重发。
# 设备端目前**没有**「我挂起了，别再发」的回执通道（新增要动协议 + 双端版本兼容），
# 所以服务端按次数退避：前 10 次（≈5 分钟）保持 30 秒节奏（覆盖设备重启/时钟偏差的兜底），
# 之后每 10 分钟一次，到上限就停发并只打一行日志。订单仍是 waiting ⇒ 人工点「开始打印」照常能打、
# 打完照常回报成功（设备是最终裁决者）。**别改成「到点一次就把子任务判失败」**：那会让一张
# 只是等人翻纸的单子变成「打印失败」，顾客要重下单。
_START_PRINT_FAST_LIMIT = 10        # 前 10 次保持 30 秒节奏
_START_PRINT_SLOW_GAP = 600         # 之后每 10 分钟一次
_START_PRINT_MAX = 16               # 一共最多 16 次（≈ 5 分钟 + 100 分钟）
_start_print_attempts = {}          # of_id -> {"n": int, "last": float, "stopped": bool}
_start_print_lock = threading.Lock()


def _start_print_state_path():
    """兜底重发计数落盘位置（与 orders.db 同目录，独立小 JSON）。"""
    return os.path.join(os.path.dirname(DATABASE) or ".", "start_print_attempts.json")


def _load_start_print_attempts():
    """启动时读回上次的兜底重发计数（🟡3）。

    为什么要落盘：这份计数原来只在进程内存里 —— 后端一重启，「已经重发 16 次、
    决定停发」这件事就忘了，对同一张永远不接单的预约单重新再刷 16 轮（每轮都有
    一条日志、一条 socket）。落盘之后重启不会重置上限，也不会把"已停发"丢掉。
    """
    try:
        with open(_start_print_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return 0
        with _start_print_lock:
            for k, v in data.items():
                try:
                    _of_id = int(k)
                    _n = int((v or {}).get("n") or 0)
                    _last = float((v or {}).get("last") or 0.0)
                    _stopped = bool((v or {}).get("stopped"))
                except (TypeError, ValueError):
                    continue
                if _n <= 0 and not _stopped:
                    continue
                _start_print_attempts[_of_id] = {"n": _n, "last": _last,
                                                 "stopped": _stopped}
        return len(_start_print_attempts)
    except FileNotFoundError:
        return 0
    except Exception as e:      # 坏文件/没权限都不能挡住调度器起来
        print(f"  [SCHEDULER] 兜底重发计数读取失败（当作从零开始）: {e}")
        return 0


def _save_start_print_attempts():
    """把兜底重发计数写盘（原子替换；调用方已持锁或接受轻微竞态）。"""
    try:
        with _start_print_lock:
            data = {str(k): {"n": int(v.get("n") or 0),
                             "last": float(v.get("last") or 0.0),
                             "stopped": bool(v.get("stopped"))}
                    for k, v in _start_print_attempts.items()}
        path = _start_print_state_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        print(f"  [SCHEDULER] 兜底重发计数写盘失败（不影响本轮发送）: {e}")


def _start_print_push_due(of_id, now_ts):
    """这次扫描要不要给该子任务发 start_print。返回 (是否发, 是否刚停发)。

    🟡3：判定"该发"之后**先不计数** —— 上层真正 emit 成功才调
    `_mark_start_print_sent()`。以前 `st["n"] += 1` 写在返回之前，发送失败
    （设备刚掉、socket 层抛错）也算一次，16 次上限被白白烧掉。
    """
    with _start_print_lock:
        st = _start_print_attempts.setdefault(
            int(of_id), {"n": 0, "last": 0.0, "stopped": False})
        if st["stopped"]:
            return False, False
        if st["n"] >= _START_PRINT_MAX:
            st["stopped"] = True
            return False, True
        gap = 30 if st["n"] < _START_PRINT_FAST_LIMIT else _START_PRINT_SLOW_GAP
        if st["last"] and (now_ts - st["last"]) < gap - 1:   # -1 容忍扫描抖动
            return False, False
        return True, False


def _mark_start_print_sent(of_id, now_ts):
    """真正发出去了才计数（见 `_start_print_push_due` 的说明）。"""
    with _start_print_lock:
        st = _start_print_attempts.setdefault(
            int(of_id), {"n": 0, "last": 0.0, "stopped": False})
        st["n"] += 1
        st["last"] = now_ts
    _save_start_print_attempts()


def _forget_start_print_attempts(keep_ids):
    """子任务离开 waiting（打出来了 / 作废 / 取消）⇒ 丢掉它的节流计数，别让内存无限涨。"""
    changed = False
    with _start_print_lock:
        for k in list(_start_print_attempts):
            if k not in keep_ids:
                _start_print_attempts.pop(k, None)
                changed = True
    if changed:
        _save_start_print_attempts()


def resync_pending_prices():
    """把 needs_repricing=1 的子任务按**当前**配置重算价格，并汇总父订单总额。

    为什么需要它（🔴-B10，2026-11-10 体检）：`_sync_subtask_config()` 是"设备回报实际
    打印配置后改价"的唯一入口，它整段在一个 try 里 —— 任何一步抛异常就 rollback，
    而调用方 `on_print_success` 早已把子任务 commit 成 sent，`_recalc_prices_for_file`
    又只处理非终态，于是这单永远停在估价。现在失败时打 needs_repricing=1，由这个
    10 分钟一次的扫描补算（幂等：算完清标记；失败保持标记下轮再来）。
    """
    fixed = 0
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT id, order_id, page_count, copies, duplex, page_range, simplex_pages"
            " FROM order_files WHERE COALESCE(needs_repricing, 0) = 1 LIMIT 200"
        ).fetchall()
        for row in rows:
            try:
                per_copy = calculate_price(row["page_count"] or 1, row["duplex"] or "on",
                                           row["page_range"] or "",
                                           simplex_pages=_row_simplex_pages(row))
                new_total = round(per_copy * (row["copies"] or 1), 2)
                conn.execute("UPDATE order_files SET total_price = ?, needs_repricing = 0"
                             " WHERE id = ?", (new_total, row["id"]))
                oid = row["order_id"]
                sub_sum = conn.execute(
                    "SELECT COALESCE(SUM(total_price), 0) FROM order_files WHERE order_id = ?",
                    (oid,)).fetchone()[0]
                o = conn.execute(
                    "SELECT delivery_enabled, delivery_percentage, urgency_price,"
                    " cover_page, cover_page_price FROM orders WHERE id = ?", (oid,)).fetchone()
                if o:
                    delivery_fee = (round(float(sub_sum) * (o["delivery_percentage"] or 0) / 100, 2)
                                    if o["delivery_enabled"] else 0)
                    urgency_fee = float(o["urgency_price"] or 0)
                    cover_fee = (round(float(o["cover_page_price"] or 0) * o["cover_page"], 2)
                                 if o["cover_page"] else 0)
                    parent_total = round(float(sub_sum) + urgency_fee + cover_fee + delivery_fee, 2)
                    conn.execute("UPDATE orders SET total_price = ? WHERE id = ?",
                                 (parent_total, oid))
                fixed += 1
            except Exception as e:
                print(f"  [REPRICE] 子任务 #{row['id']} 重算失败（下轮再试）: {e}")
        conn.commit()
        conn.close()
        if fixed:
            print(f"  [REPRICE] 已补算 {fixed} 个子任务的实价")
    except Exception as e:
        print(f"  [REPRICE] 扫描待补算子任务失败: {e}")
    return fixed


def process_scheduled_orders():
    """预约订单扫描（每 30s）：
    - scheduled 子任务（阶段①文件尚未下发，如提交时打印机离线）→ 有在线客户端时下发文件（downloading）
    - waiting 子任务且 scheduled_at 已到、未冻结 → 向拥有该文件的客户端发 start_print 兜底
      （本地通常已按 scheduled_ts 自触发，此信号为幂等冗余，用于恢复/兜底）
    """
    conn = get_db()
    rows = conn.execute(
        """
        SELECT of.id AS of_id, of.order_id, of.file_id, of.file_name,
               of.copies, of.page_range, of.duplex, of.status AS of_status,
               of.operator_client, of.image_orientation,
               of.simplex_pages, of.blank_tail_exclude,
               o.auto_print, o.schedule_mode, o.scheduled_at, o.schedule_frozen,
               o.target_client
        FROM order_files of
        JOIN orders o ON of.order_id = o.id
        WHERE of.status IN ('scheduled', 'waiting')
          AND o.schedule_mode != 'now'
          AND o.status != 'canceled'   -- 已取消订单的子任务不再下发/兜底（P2-9）
        ORDER BY of.created_at ASC
        """
    ).fetchall()
    conn.close()
    if not rows:
        # 没有待下发/待兜底的子任务 ⇒ 节流计数全部作废（否则「单子已打完」之后这份 dict 会一直挂着）
        _forget_start_print_attempts(set())
        return

    active_printer = get_active_printer_client()  # 兜底：未指定目标时任一在线接单设备
    online = set(get_active_clients())
    now = datetime.now()
    seen_waiting = set()   # 本轮仍是 waiting 的子任务（离开 waiting 的清掉节流计数）

    for row in rows:
        order_id = row["order_id"]

        # 确定下发目标设备：指定目标 → 该设备（须在线）；未指定 → 任一在线接单设备
        dest_client = row["target_client"] or ""
        if dest_client:
            if dest_client not in online:
                continue
        else:
            dest_client = active_printer
            if not dest_client:
                continue

        if row["of_status"] == "scheduled":
            # 阶段①：文件下发（下发到目标设备/任一在线接单设备；无可用设备则保持 scheduled 等待）
            if not dest_client:
                continue
            file_id = row["file_id"]
            if not file_id:
                continue
            conn = get_db()
            frow = conn.execute("SELECT path FROM files WHERE id = ?", (file_id,)).fetchone()
            conn.close()
            if not frow or not os.path.exists(frow["path"]):
                # 文件已被清理 → 标记失败（2026-11：必须写原因，否则用户只看到"失败"）
                conn = get_db()
                _retry_on_lock(
                    conn.execute,
                    "UPDATE order_files SET status = 'failed', reject_reason = ?"
                    " WHERE id = ? AND status = 'scheduled'",
                    ("预约订单的文件已被清理，无法打印，请联系客服重传", row["of_id"]),
                )
                refresh_order_status(conn, order_id)
                _retry_on_lock(conn.commit)
                conn.close()
                continue
            push_print_task_to_client(row["of_id"], file_id, row["file_name"],
                                      row["copies"], row["duplex"] or "on",
                                      row["page_range"] or "", dest_client,
                                      auto_print=True,
                                      scheduled_download=True,
                                      schedule_mode=row["schedule_mode"],
                                      scheduled_at=row["scheduled_at"] or "",
                                      schedule_frozen=row["schedule_frozen"],
                                      image_orientation=row["image_orientation"] or "auto",
                                      simplex_pages=_row_simplex_pages(row),
                                      blank_tail_exclude=_row_blank_tail_exclude(row))

        elif row["of_status"] == "waiting":
            # 阶段②兜底：到点且未冻结 → 发 start_print（本地通常已自触发，幂等）
            seen_waiting.add(int(row["of_id"]))
            if row["schedule_frozen"]:
                continue
            sat = row["scheduled_at"] or ""
            if not sat:
                continue
            try:
                target = datetime.strptime(sat, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if target > now:
                continue
            client_id = row["operator_client"] or active_printer
            if not client_id:
                continue
            with printer_clients_lock:
                info = printer_clients.get(client_id)
                sid = info["sid"] if info else None
            if not sid:
                continue
            # 兜底重发要节流（见 _start_print_push_due 顶部现场记录）：设备端可能已经挂起等人工
            # 翻纸并结束了状态机，此时每 30 秒重发只会让「指令无效」看起来像故障、日志无限刷。
            _due, _just_stopped = _start_print_push_due(row["of_id"], time.time())
            if not _due:
                if _just_stopped:
                    print(f"  [SKIP] 预约单 #{row['of_id']}: start_print 兜底已重发 "
                          f"{_START_PRINT_MAX} 次仍无动静，停止重发"
                          f"（子任务保持 waiting：人工点「开始打印」照常可打、打完照常回报成功）")
                continue
            try:
                socketio.emit("start_print", {
                    "order_id": order_id,
                    "task_ids": [row["of_id"]],
                    "scheduled_at": sat,
                    "scheduled_ts": _iso_to_ts(sat),
                }, to=sid)
                # 🟡3：真的发出去了才记一次（发射抛异常不烧额度）
                _mark_start_print_sent(row["of_id"], time.time())
            except Exception as e:
                print(f"  [FAIL] 预约单 #{row['of_id']} start_print 兜底推送失败: {e}")

    _forget_start_print_attempts(seen_waiting)


def push_print_task_to_client(sub_task_id, file_id, file_name, copies, duplex, page_range, client_id,
                              auto_print=False, scheduled_download=False,
                              schedule_mode="now", scheduled_at="", schedule_frozen=0,
                              image_orientation="auto", simplex_pages="", blank_tail_exclude=0):
    """通过 SocketIO 推送子任务 (order_files) 到指定打印机客户端。
    sub_task_id = order_files.id。

    - 普通推送：子任务 queued → printing（登记 pushed_tasks，3 分钟超时兜底）。
    - scheduled_download=True（预约单阶段①）：子任务 scheduled → downloading，仅下发文件
      提前下载（把 scheduled_at 一并带给本地，本地到点自触发打印），不登记 pushed_tasks，
      避免"提前下发文件"被 3 分钟打印超时误杀。

    关键顺序：先在数据库中把子任务标记状态（并登记 pushed_tasks），再通过 SocketIO emit。
    这样即使 emit 之后客户端来不及回报，数据库状态也是一致的；若数据库写失败（锁等），
    任务保持原状态不会被推送，避免"客户端已处理但数据库仍旧状态"的幽灵任务。"""
    if not file_id:
        return False

    # 1. 原子锁定任务（db_lock 内使用独立连接，防止与 pull 接口并发重复推送）
    order_id = None
    with db_lock:
        conn = get_db_conn()
        try:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if scheduled_download:
                cur = conn.execute(
                    "UPDATE order_files SET status = 'downloading', operator_client = ?, locked_at = ?, last_operator_client = ? WHERE id = ? AND status = 'scheduled'",
                    (client_id, now_str, client_id, sub_task_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE order_files SET status = 'printing', operator_client = ?, locked_at = ?, last_operator_client = ? WHERE id = ? AND status = 'queued'",
                    (client_id, now_str, client_id, sub_task_id),
                )
            # cur.rowcount == 0 表示该任务已不是 queued（正被其他流程处理或已结束）→ 跳过
            if cur.rowcount == 0:
                print(f"  [SKIP] 子任务 #{sub_task_id}: 非 queued 状态，跳过推送")
                return False
            order_row = conn.execute(
                "SELECT order_id FROM order_files WHERE id = ?", (sub_task_id,)
            ).fetchone()
            if order_row:
                order_id = order_row["order_id"]
                refresh_order_status(conn, order_id)
                # 记录实际接收设备（首次写入，幂等）
                conn.execute(
                    "UPDATE orders SET received_client = COALESCE(NULLIF(received_client, ''), ?) WHERE id = ?",
                    (client_id, order_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # 2. 登记推送等待反馈（必须在 emit 之前登记，避免回调先到导致漏清理）
    #    预约阶段①只下发文件不等待打印反馈，不登记（否则 3 分钟未打印会被超时误杀）
    if not scheduled_download:
        with pushed_tasks_lock:
            pushed_tasks[sub_task_id] = {   # key = order_files.id
                "pushed_at": datetime.now(),
                "client_id": client_id,
            }

    # 3. 取出客户端 sid 并 emit
    with printer_clients_lock:
        client_info = printer_clients.get(client_id)
        sid = client_info["sid"] if client_info else None

    # S1-06：把 openid + task_id 绑进下载 URL（泄露也只能下这一单的这一个文件）
    _bind_openid = ""
    if order_id:
        _c = get_db()
        try:
            _r = _c.execute("SELECT openid FROM orders WHERE id = ?", (order_id,)).fetchone()
            _bind_openid = (_r["openid"] or "") if _r else ""
        finally:
            _c.close()
    download_url = make_download_url(file_id, openid=_bind_openid, task_id=sub_task_id)

    # 查询订单号
    order_number = ""
    if order_id:
        conn = get_db()
        row = conn.execute(
            "SELECT order_number FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        conn.close()
        if row:
            order_number = row["order_number"] or ""

    task_msg = {
        "type": "print_task",
        "task_id": sub_task_id,          # 语义切换为 order_files.id
        "order_id": order_id,
        "order_number": order_number,
        "file_url": download_url,
        "file_name": file_name,          # 原始文件名（含扩展名）
        "source_md5": "",               # 由后续代码填充
        "auto_print": auto_print,       # 无障碍打印：本地工具收到后自动建标签页并开始打印
        # 预约打印（无障碍打印的预约形式）：阶段①先下发文件，本地到 scheduled_ts 再开始打印
        "schedule_mode": schedule_mode,          # now | at | countdown
        "scheduled_at": scheduled_at,            # 绝对时间 "%Y-%m-%d %H:%M:%S"
        "scheduled_ts": _iso_to_ts(scheduled_at) if scheduled_at else 0,  # epoch 秒
        "schedule_frozen": int(schedule_frozen or 0),
        "options": {
            "copies": copies,
            "duplex": duplex or "on",
            "page_range": page_range or "",
            "image_orientation": image_orientation or "auto",
            # 2026-11-02：「封面单独打印」所列页 / 「排除末尾空白页」开关 —— 设备端据此
            # 分两趟摊纸（单面页各占一张）并在真排除掉空白页后回报 page_range_truncated。
            "simplex_pages": simplex_pages or "",
            "blank_tail_exclude": int(blank_tail_exclude or 0),
        },
    }

    # 查询父订单的附加服务配置，传递给本地工具
    if order_id:
        conn2 = get_db()
        o_row = conn2.execute(
            "SELECT delivery_enabled, delivery_location, urgency, cover_page, cover_page_price, owner_name, is_admin_print, openid, remark, source FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        conn2.close()
        if o_row:
            task_msg["delivery_enabled"] = bool(o_row["delivery_enabled"])
            task_msg["delivery_location"] = o_row["delivery_location"] or ""
            task_msg["urgency"] = o_row["urgency"] or "低"
            task_msg["cover_page"] = bool(o_row["cover_page"])
            task_msg["cover_page_price"] = float(o_row["cover_page_price"] or 0.10)
            # 订单备注（本地工具展示）
            task_msg["remark"] = o_row["remark"] or ""
            # v24.1：订单归属标记随任务下发，本地工具据此预勾选"管理员自行打印"
            task_msg["owner_name"] = o_row["owner_name"] or ""
            task_msg["is_admin_print"] = bool(o_row["is_admin_print"])
            # 2026-10-26（隐式规则）：内容归属看**上传者**（is_admin_print 是可变的业务列：
            # 人工类型订正 / 机位勾选 / print_success 都会改写它，光看它会把顾客内容送进 COM）。
            task_msg["uploader_is_admin"] = is_admin_uploader(o_row["openid"] or "")
            # 2026-12：顾客订单的标签页归属 = 下单用户绑定的成员名（收支清算成员绑定）。
            # 管理员自行打印订单沿用 orders.owner_name（提交者昵称/前端指定）。
            task_msg["bound_owner_name"] = _get_bound_owner_name(o_row["openid"] or "")
            # 订单来源（wechat/app/local），本地工具云端任务列表展示
            task_msg["source"] = o_row["source"] or "wechat"

    # 查询文件 MD5（供本地工具 PDF 缓存命中，避免重复下载，P1-7）
    if file_id:
        conn = get_db()
        row = conn.execute(
            "SELECT md5, path, retention_hours FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        conn.close()
        # S1-03：把该文件的保留模式一并下发（-1 立即删除 / 0 服务器管理 / 正数旧客户端）
        if row is not None:
            try:
                task_msg["retention_hours"] = int(row["retention_hours"] or 0)
            except (KeyError, TypeError, ValueError):
                task_msg["retention_hours"] = 0
        if row and row["md5"]:
            task_msg["source_md5"] = row["md5"]
        elif row and row["path"]:
            # 兜底：老记录 md5 列为空 → 从 MD5 索引按路径反查（load_md5_index 自带锁）
            rel = os.path.relpath(row["path"], UPLOAD_DIR)
            md5_index = load_md5_index()
            for k, v in md5_index.items():
                p = v if isinstance(v, str) else (v or {}).get("path", "")
                if p == rel:
                    task_msg["source_md5"] = k
                    break

    if not sid:
        # 客户端刚断开：预约单回退 scheduled，普通单回退 queued，等下次扫描重试
        with db_lock:
            conn = get_db_conn()
            try:
                if scheduled_download:
                    conn.execute(
                        "UPDATE order_files SET status = 'scheduled', locked_at = '' WHERE id = ? AND status = 'downloading'",
                        (sub_task_id,),
                    )
                else:
                    conn.execute(
                        "UPDATE order_files SET status = 'queued', locked_at = '' WHERE id = ? AND status = 'printing'",
                        (sub_task_id,),
                    )
                if order_id:
                    refresh_order_status(conn, order_id)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        if not scheduled_download:
            with pushed_tasks_lock:
                pushed_tasks.pop(sub_task_id, None)
        print(f"  [WAIT] 子任务 #{sub_task_id}: 客户端已离线，保持排队")
        return False

    try:
        socketio.emit("print_task", task_msg, to=sid)
        print(f"  [PUSH] 子任务 #{sub_task_id}: 已推送到客户端 {client_id}")
        return True
    except Exception as e:
        print(f"  [FAIL] 子任务 #{sub_task_id}: 推送失败: {e}")
        # emit 失败：预约单回退 scheduled，普通单回退 queued，清理登记
        with db_lock:
            conn = get_db_conn()
            try:
                if scheduled_download:
                    conn.execute(
                        "UPDATE order_files SET status = 'scheduled', locked_at = '' WHERE id = ? AND status = 'downloading'",
                        (sub_task_id,),
                    )
                else:
                    conn.execute(
                        "UPDATE order_files SET status = 'queued', locked_at = '' WHERE id = ? AND status = 'printing'",
                        (sub_task_id,),
                    )
                if order_id:
                    refresh_order_status(conn, order_id)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        if not scheduled_download:
            with pushed_tasks_lock:
                pushed_tasks.pop(sub_task_id, None)
        return False


def check_printing_timeout():
    """检查超过超时时间未反馈的 printing/accepted/offline_unknown 子任务 (order_files)，
    标记为 **timeout（超时）** 并聚合父订单。

    2026-11 口径变更：这三条超时路径原先一律标 failed —— 用户看到「失败」却没有任何原因，
    分不清「打印机坏了」「纸没出来」还是「纯粹没人管/设备掉线」。现在统一标 timeout，
    并且**每条都写 reject_reason**（订单详情/结算页直接展示给用户）。

    P1-8 超时判定（按是否已被客户端 accept 区分）：
    - 未 accept（status='printing'，打印机端窗口等待用户确认）：**不再判失败**——
      订单提交后一直未确认是常见场景（打印机端管理员未处理/忙），超时仅清 pushed_tasks
      计时条目；任务保持 printing，由 recover_orphaned_printing_tasks 5 分钟回退 queued 后
      重新推送，直到打印机确认/打回/用户取消（或排队超 queued_timeout_hours 被淘汰）。
    - 已 accept（status='accepted'）：accept 时刻（locked_at，由 accept_order 刷新）起算 3 分钟
      （PRINT_FEEDBACK_TIMEOUT）；locked_at 距今不足 3 分钟 → 跳过（仍在反馈窗口内）。
      ⚠ 这条就是现场「添加到本地十来分钟还没打，订单被判失败」的真实落点之一：客户端
      accept 之后若用户没点开始打印（job 只进了标签页、不在保活集合里），locked_at 不再被
      print_keepalive 刷新，3 分钟后就落到这里。
    - 断线未知（status='offline_unknown'）：accept 时刻起算 OFFLINE_UNKNOWN_TIMEOUT（30 分钟）。
    """
    now = datetime.now()
    timeout_sub_tasks = []

    # 1) 未 accept 的任务：等待打印机确认，不判失败；仅清理 pushed_tasks 计时条目
    #    （条目只用于"推送后多久未反馈"计时，不清理会随进程一直累积）
    with pushed_tasks_lock:
        for sub_task_id, info in list(pushed_tasks.items()):
            if (now - info["pushed_at"]).total_seconds() > ACCEPT_WAIT_TIMEOUT:
                print(f"  [WAIT] 子任务 #{sub_task_id}: 推送后 {ACCEPT_WAIT_TIMEOUT // 60} 分钟未被打印机确认，"
                      f"停止计时，保持等待（不判失败）")
                del pushed_tasks[sub_task_id]

    # 2) 已 accept 的任务：locked_at（accept 时刻）起算 3 分钟
    conn = get_db()
    try:
        acc_rows = conn.execute(
            "SELECT id, order_id, locked_at FROM order_files WHERE status = 'accepted'"
        ).fetchall()
        for r in acc_rows:
            la = r["locked_at"] or ""
            if not la:
                continue  # 旧数据无 locked_at，不参与超时判定
            try:
                la_dt = datetime.strptime(la, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if (now - la_dt).total_seconds() > PRINT_FEEDBACK_TIMEOUT:
                timeout_sub_tasks.append(r["id"])

        # 3) offline_unknown（断线未知）：客户端断线且未回报结果，超时后保守标 timeout
        #    （方案 B：不自动重新入队，避免重复打印；用户可在订单列表看到超时后手动重发。
        #     ⚠ 不是 failed：结果未知 ≠ 确定没打出来，设备迟到的 print_success 仍能救回 sent）
        off_rows = conn.execute(
            "SELECT id, order_id, locked_at FROM order_files WHERE status = 'offline_unknown'"
        ).fetchall()
        for r in off_rows:
            la = r["locked_at"] or ""
            if not la:
                continue  # 无 accept 时刻，不参与超时判定
            try:
                la_dt = datetime.strptime(la, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if (now - la_dt).total_seconds() <= OFFLINE_UNKNOWN_TIMEOUT:
                continue
            print(f"  [TIMEOUT] 子任务 #{r['id']}: 断线未知超过 {OFFLINE_UNKNOWN_TIMEOUT // 60} 分钟，标记为超时")
            conn.execute(
                "UPDATE order_files SET status = ?, locked_at = '',"
                " reject_reason = '打印机断线，打印结果未知，已超时' WHERE id = ?",
                (TIMEOUT_STATUS, r["id"]),
            )
            refresh_order_status(conn, r["order_id"])
            conn.commit()

        if not timeout_sub_tasks:
            return

        print(f"\n[TIMEOUT] 超时检查: {len(timeout_sub_tasks)} 个子任务超时")
        for sub_task_id in timeout_sub_tasks:
            of_row = conn.execute(
                "SELECT id, status, order_id, locked_at FROM order_files WHERE id = ?", (sub_task_id,)
            ).fetchone()

            if not of_row or of_row["status"] != "accepted":
                if of_row:
                    print(f"  [INFO] 子任务 #{sub_task_id}: 状态已变更为 {of_row['status']}，跳过")
                continue
            # 已 accept 但 locked_at 距今 < 3 分钟 → 刚 accept 不久，仍在反馈窗口内，跳过
            la = of_row["locked_at"] or ""
            if la:
                try:
                    la_dt = datetime.strptime(la, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    la_dt = None
                if la_dt and (now - la_dt).total_seconds() < PRINT_FEEDBACK_TIMEOUT:
                    print(f"  [INFO] 子任务 #{sub_task_id}: 刚被 accept，仍在反馈窗口内，跳过")
                    continue
            print(f"  [TIMEOUT] 子任务 #{sub_task_id}: 已确认但超时未反馈，标记为超时")
            conn.execute(
                "UPDATE order_files SET status = ?, locked_at = '',"
                " reject_reason = ? WHERE id = ? AND status = 'accepted'",
                (TIMEOUT_STATUS,
                 "打印机已确认，但 3 分钟内未开始打印/未回报结果，已超时",
                 sub_task_id),
            )
            refresh_order_status(conn, of_row["order_id"])
            conn.commit()
    finally:
        conn.close()


# 🔴-A5：printing 任务的硬上限。设备仍在线时先把 5 分钟回退窗口放宽到这个时长，
# 超过它才认定「在线但卡死」并回退 queued（见 recover_orphaned_printing_tasks）。
_ORPHAN_ONLINE_GRACE_MIN = 30


def recover_orphaned_printing_tasks():
    """扫描超过 5 分钟仍处于 printing 的任务，检查文件是否存在：
    - 文件存在 → 回退为 queued（让其他打印机重试）
    - 文件不存在 → 标记为 failed（避免无限循环）
    覆盖极端场景：服务器断电/客户端失联但未触发 disconnect 事件。

    P0-1：判定基准从 created_at 改为 locked_at（任务被锁定的时刻）。
    旧逻辑用 created_at，任务被"push → 客户端断线回滚 → 再 push"循环时会不断刷新
    created_at 附近的锁定，但 created_at 是创建时间不会变——实际问题是 created_at 很久
    的任务若被反复回退/重推，会立刻被回收造成无限循环；locked_at 只在锁定瞬间写入，
    回退时清空（locked_at=''），新锁定重新写入，语义正确且兼容旧数据（'' 视为未锁定，
    直接按超时处理）。

    🔴-A5（2026-11-10 体检）：**领取任务的那台设备还在线、且锁定时间还在硬上限内，就不许
    回退**。旧逻辑只看 locked_at 超过 5 分钟 —— 而现场一次手动双面/攒批动辄十分钟，只要
    保活断了几拍（网络抖动、切网、客户端卡在 COM 转换），这里就会把「纸正在这张一叠里、
    第 2 趟还没开始」的任务回退 queued 再推给别的设备打一遍：同一份文件出两次纸，订单状态
    来回翻转。参照 `recover_stale_downloading()` 的在线判据，这里给它补上：
      · 设备离线（或 operator_client 为空，历史数据）→ 照旧 5 分钟回退（真孤儿）；
      · 设备在线 → 继续等，直到 locked_at 超过 `_ORPHAN_ONLINE_GRACE_MIN` 分钟（硬上限）
        才回退 —— 在线却半小时毫无动静，说明它卡死了、纸也不可能还在出，此时回退/重推才是
        正确处置。
    · 文件已被清理（path 不存在）仍然立刻判 failed：没有重出纸的风险，早失败早通知。
    """
    cutoff_time = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    hard_cutoff = (datetime.now() - timedelta(minutes=_ORPHAN_ONLINE_GRACE_MIN)).strftime(
        "%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn = get_db_conn()
        rows = conn.execute(
            """SELECT of.id, of.order_id, of.file_id, of.operator_client, of.locked_at, f.path
               FROM order_files of
               LEFT JOIN files f ON of.file_id = f.id
               WHERE of.status = 'printing' AND (of.locked_at = '' OR of.locked_at < ?)""",
            (cutoff_time,)
        ).fetchall()
        if not rows:
            conn.close()
            return

        online = set(get_active_clients())

        reset_ids = []  # → queued（文件还在）
        fail_ids = []   # → failed（文件已删）
        kept_ids = []   # 在线设备的在飞作业：先不动（避免重复出纸）
        for r in rows:
            file_path = r["path"]
            if not (file_path and os.path.isfile(file_path)):
                fail_ids.append(r["id"])
                continue
            _oc = str(r["operator_client"] or "")
            _lk = str(r["locked_at"] or "")
            if _oc and _oc in online and _lk and _lk >= hard_cutoff:
                # 设备还在线、锁定时间还在硬上限内：它多半真的在打（手动双面等翻纸 / 攒批
                # 整叠第 2 趟）。回退 queued 会被别的设备再打一遍。
                kept_ids.append(r["id"])
                continue
            reset_ids.append(r["id"])

        if reset_ids:
            placeholders = ",".join("?" for _ in reset_ids)
            conn.execute(
                f"UPDATE order_files SET status = 'queued', operator_client = '', locked_at = '' WHERE id IN ({placeholders})",
                [str(x) for x in reset_ids]
            )
        if fail_ids:
            placeholders = ",".join("?" for _ in fail_ids)
            conn.execute(
                f"UPDATE order_files SET status = 'failed', locked_at = '',"
                f" reject_reason = '打印机异常，任务文件已被清理，无法打印' WHERE id IN ({placeholders})",
                [str(x) for x in fail_ids]
            )

        all_ids = set(reset_ids + fail_ids)
        for r in rows:
            if r["id"] in all_ids:
                refresh_order_status(conn, r["order_id"])
        conn.commit()
        conn.close()

        if kept_ids:
            print(f"[ORPHAN] {len(kept_ids)} 个 printing 任务的设备仍在线（未超 "
                  f"{_ORPHAN_ONLINE_GRACE_MIN} 分钟硬上限），本轮不回退")
        if reset_ids:
            print(f"[ORPHAN] 已回收 {len(reset_ids)} 个孤儿 printing 任务 → queued")
        if fail_ids:
            print(f"[ORPHAN] 文件不存在，{len(fail_ids)} 个孤儿任务标记为 failed")


def recover_stale_downloading():
    """预约单兜底：downloading 子任务回退 scheduled，等 process_scheduled_orders 重新下发。

    两条判据（2026-11-10 体检 🔴14）：

    1) 领取的客户端**已不在线**（服务端重启漏了 disconnect 事件等）；
    2) 客户端**心跳还在但下载早就不动了** —— push 时写过 ``locked_at``，旧实现全文件没人读它
       这个字段，于是「设备连着网、文件永远传不完」会把子任务挂到天荒地老：
       顾客的预约单既不打印也不失败，零日志、无入口。

    只有 1) 时，网络抖一下、或者 200MB 的文件慢慢传，都会被误伤；加上 2) 才能既不误杀慢速
    下载、又不会永久卡死。回退时清 ``operator_client`` 和 ``locked_at``，重新排队等于给
    设备一次重试机会（同一份文件重下是幂等的，不涉及重复出纸）。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, order_id, operator_client, locked_at FROM order_files WHERE status = 'downloading'"
    ).fetchall()
    conn.close()
    if not rows:
        return

    active = set(get_active_clients())
    now_dt = datetime.now()
    with db_lock:
        conn2 = get_db_conn()
        try:
            conn2.execute("BEGIN IMMEDIATE")
            off_count = 0
            stall_count = 0
            for r in rows:
                _oc = r["operator_client"] or ""
                _lk = r["locked_at"] or ""
                _stalled = False
                if _lk:
                    try:
                        _lk_dt = datetime.strptime(_lk, "%Y-%m-%d %H:%M:%S")
                        _stalled = (now_dt - _lk_dt).total_seconds() > DOWNLOAD_STALL_TIMEOUT
                    except ValueError:
                        _stalled = False
                if _oc in active and not _stalled:
                    continue
                # 状态守卫：SELECT 在锁外读取，防止与 cancel 竞态把已取消子任务翻回 scheduled
                cur = conn2.execute(
                    "UPDATE order_files SET status = 'scheduled', operator_client = '', locked_at = ''"
                    " WHERE id = ? AND status = 'downloading'",
                    (r["id"],),
                )
                if cur.rowcount:
                    refresh_order_status(conn2, r["order_id"])
                    if _oc in active:
                        stall_count += 1
                    else:
                        off_count += 1
            conn2.commit()
            if off_count:
                print(f"[RECOVER] {off_count} 个 downloading 子任务的客户端已不在线，回退 scheduled")
            if stall_count:
                print(f"[RECOVER] {stall_count} 个 downloading 子任务在线但 {DOWNLOAD_STALL_TIMEOUT // 60}"
                      f" 分钟没有任何进度（locked_at 未刷新），回退 scheduled 重下")
        except Exception:
            conn2.rollback()
            raise
        finally:
            conn2.close()


# ==================== SocketIO 事件 ====================


@sio_event("connect")
def on_connect(auth=None):
    """打印机客户端连接 -- 验证 URL 查询参数中的 token"""
    token = _get_printer_token()
    if not token and auth and isinstance(auth, dict):
        token = auth.get("token", "")
    _auth = _authenticate_printer(token)
    if not _auth:
        print(f"[WARN] 打印机客户端认证失败: token 无效")
        emit("auth_fail", {"message": "token 无效"})
        disconnect()
        return False

    # P1-2.3：client_id 来源校验（非空、长度合理），避免脏数据/异常连接污染 printer_clients
    client_id = (request.args.get("client_id", request.sid) or "").strip()
    if not client_id or len(client_id) > 64:
        print(f"[WARN] 打印机客户端连接: client_id 非法，拒绝")
        emit("auth_fail", {"message": "client_id 无效"})
        disconnect()
        return False
    # S1-01：per-device token 只能以自己的 client_id 连接（防止拿到 A 的 token 冒充 B）
    if _auth.get("client_id") and _auth["client_id"] != client_id:
        print(f"[WARN] 打印机客户端连接: token 属于 {_auth['client_id']}，却以 {client_id} 连接，拒绝")
        emit("auth_fail", {"message": "token 与 client_id 不匹配"})
        disconnect()
        return False

    # 设备注册：计算机名随连接参数上报（本地工具 hostname），写入设备注册表（跨重启保留）
    # printer= 可选能力上报（打印队列 / 驱动 / 端口 / 是否自动双面）；老客户端不带则为 None
    device_name = (request.args.get("device_name", "") or "").strip()[:64]
    register_device(client_id, device_name, printer=_printer_probe_from_request())

    with printer_clients_lock:
        old = printer_clients.get(client_id)
        if old and old["sid"] != request.sid:
            # 同一 client_id 重复连接：优雅替换（旧连接断线回滚会按 sid 反查兜底），记录警告
            print(f"[WARN] 打印机客户端 {client_id} 重复连接（旧 sid={old['sid']}），已替换为新连接")
        printer_clients[client_id] = {
            "sid": request.sid,
            "heartbeat": datetime.now(),
            "connected_at": datetime.now(),
        }
    join_room(client_id)
    _sid_to_client[request.sid] = client_id   # S1-07：on_ping 的 O(1) 映射
    # 攒批「离线期望」：新会话允许自动重试一次（上一次会话里设备忙拒绝过的那条）
    with _stack_intent_lock:
        _stack_intent_dispatched.pop(client_id, None)
    print(f"[LINK] 打印机客户端已连接: {client_id}")

    # 上线即同步接单状态（本机是否接管 / 当前接管者是谁），供设备端 UI 与自动续接单判断
    try:
        socketio.emit("printer_state", _printer_state_payload(client_id), to=request.sid)
    except Exception as e:
        print(f"  [CLAIM] 推送接单状态失败: {e}")

    # 打印机上线后，重推之前因离线而未能送达的页数分析请求
    # LEFT JOIN 覆盖两类待分析文件：
    #   ① 未关联任何订单的上传文件（"上传界面还在"，订单未提交）→ of.id IS NULL
    #   ② 属于活跃任务的文件（queued/printing/accepted）
    # 已取消/打回/完成的订单文件不再分析；page_count_verified=1 后自然不再命中。
    try:
        conn = get_db()
        pending = _pending_page_analysis_rows(conn)
        conn.close()
        pushed = 0
        for row in pending:
            fname = row["original_name"] or ""
            ext = os.path.splitext(fname)[1].lower()
            if ext in (".doc", ".docx"):
                # 刚上线的这台优先分析自己的待分析文件（它才是这些订单的接管设备）
                if request_page_analysis(row["id"], fname, prefer_client=client_id):
                    pushed += 1
        if pushed > 0:
            print(f"  [PAGE] 打印机上线后重推 {pushed} 个待分析文档（含未提交订单的上传文件）")
    except Exception as e:
        print(f"  [PAGE] 重推待分析文档时出错: {e}")

    # 打印机上线后，同步当前缓存保留策略（离线期间可能已变更）
    try:
        cfg = load_retention_config()
        socketio.emit("storage_config_updated", {
            "retention_days": cfg.get("days", 7),
            "retention_hours": cfg.get("hours", 0),
        }, to=request.sid)
        print(f"  [CACHE] 已同步保留策略到 {client_id}: {cfg.get('days', 7)}天{cfg.get('hours', 0)}小时")
    except Exception as e:
        print(f"  [CACHE] 同步保留策略失败: {e}")


@sio_event("disconnect")
def on_disconnect(reason=None):
    """打印机客户端断开 — 立即回滚其名下所有 printing 任务为 queued

    🔴-2：同一 client_id 快速重连（sid A → B）后，旧连接 A 的 disconnect 不得影响
    B。判据是本连接是否仍是 printer_clients 中该 client_id 的当前 sid（连接时按
    sid 替换，见 on_connect:2761-2770）。若已被替换则**整体跳过回滚与注销**：
    回滚 SQL 只按 operator_client 过滤，而 operator_client 存的就是 client_id，
    无法区分 A/B 两条连接 ⇒ 旧连接断开会把 B 正在打印的任务误翻回 queued
    （随后 30 秒照常推送 ⇒ 重复打印），并把 B 的注册记录一并删掉。
    """
    client_id = request.args.get("client_id")
    sid = request.sid
    _sid_to_client.pop(sid, None)   # S1-07：清掉 on_ping 的 sid→client 映射

    # 先判定本连接是否已被同 client_id 的新连接取代
    replaced = False
    with printer_clients_lock:
        if client_id and client_id in printer_clients:
            if printer_clients[client_id]["sid"] != sid:
                replaced = True
        elif not client_id:
            # disconnect 事件可能不带查询参数 → 按 sid 反查兜底（原逻辑位置）
            for _cid, info in printer_clients.items():
                if info["sid"] == sid:
                    client_id = _cid
                    break

    if replaced:
        # 旧连接：不碰注册表（B 才是当前连接）、不回滚任何任务
        print(f"[LINK] 打印机客户端旧连接断开（已被同 client_id 的新连接取代）: {client_id}")
        return

    if client_id:
        with db_lock:
            conn = get_db_conn()
            try:
                # 查找该客户端名下所有 printing 子任务 → 回滚为 queued
                rows = conn.execute(
                    "SELECT id, order_id FROM order_files WHERE status = 'printing' AND operator_client = ?",
                    (client_id,)
                ).fetchall()
                if rows:
                    ids = [str(r["id"]) for r in rows]
                    placeholders = ",".join("?" for _ in rows)
                    # P0-1.4：回退 queued 时同时清空 locked_at，防止旧锁定时间被孤儿回收误判
                    conn.execute(
                        f"UPDATE order_files SET status = 'queued', operator_client = '', locked_at = '' WHERE id IN ({placeholders}) AND status = 'printing'",
                        ids
                    )
                    for r in rows:
                        refresh_order_status(conn, r["order_id"])
                    print(f"[RECOVER] 客户端 {client_id} 断开，已回滚 {len(rows)} 个任务")

                # 预约单：文件下载中的子任务回退 scheduled（文件未接收完，可重新下发）
                dl_rows = conn.execute(
                    "SELECT id, order_id FROM order_files WHERE status = 'downloading' AND operator_client = ?",
                    (client_id,)
                ).fetchall()
                if dl_rows:
                    dl_ids = [str(r["id"]) for r in dl_rows]
                    dl_placeholders = ",".join("?" for _ in dl_rows)
                    conn.execute(
                        f"UPDATE order_files SET status = 'scheduled', operator_client = '', locked_at = '' WHERE id IN ({dl_placeholders}) AND status = 'downloading'",
                        dl_ids
                    )
                    for r in dl_rows:
                        refresh_order_status(conn, r["order_id"])
                    print(f"[RECOVER] 客户端 {client_id} 断开，{len(dl_rows)} 个下载中子任务回退 scheduled")

                # 已接受但未打印的任务：标记为"断线未知"
                accepted_rows = conn.execute(
                    "SELECT id, order_id FROM order_files WHERE status = 'accepted' AND operator_client = ?",
                    (client_id,)
                ).fetchall()
                if accepted_rows:
                    a_ids = [str(r["id"]) for r in accepted_rows]
                    a_placeholders = ",".join("?" for _ in accepted_rows)
                    conn.execute(
                        f"UPDATE order_files SET status = 'offline_unknown' WHERE id IN ({a_placeholders}) AND status = 'accepted'",
                        a_ids
                    )
                    for r in accepted_rows:
                        refresh_order_status(conn, r["order_id"])
                    print(f"[RECOVER] 客户端 {client_id} 断开，{len(accepted_rows)} 个已接受任务标记为断线未知")

                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # 清理客户端注册信息（🔴-2：只注销"属于本连接"的那条记录）
    with printer_clients_lock:
        if client_id and client_id in printer_clients:
            if printer_clients[client_id]["sid"] == sid:
                del printer_clients[client_id]
            # sid 不匹配 ⇒ 已被新连接取代，保留新连接（replaced 分支已提前 return，
            # 此处守卫用于防止将来 on_connect 的替换逻辑变更后退化）
        else:
            # 兜底：通过 sid 反向查找（client_id 可能不在 request.args 中）
            for cid, info in list(printer_clients.items()):
                if info["sid"] == sid:
                    del printer_clients[cid]
                    break
    # 更新设备最后在线时间（注册表保留离线记录，供收支清算「授权」页展示）
    if client_id:
        devices = load_devices()
        if client_id in devices:
            devices[client_id]["last_seen"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_devices(devices)
    print(f"[LINK] 打印机客户端已断开: {client_id or request.sid}")


@sio_event("ping")
def on_ping():
    """心跳 -- 更新最后心跳时间。

    S1-07：改成 O(1)（原实现每次 ping 都遍历全部 printer_clients；设备数一多、
    ping 一密就是 O(n²) 的 CPU 放大面）。映射在 connect 建立、disconnect 删除。
    """
    cid = _sid_to_client.get(request.sid)
    if cid:
        with printer_clients_lock:
            info = printer_clients.get(cid)
            if info and info.get("sid") == request.sid:
                info["heartbeat"] = datetime.now()
    emit("pong")


def _subtask_ownership(task_id, client_id, conn=None):
    """🔴-D3 子任务归属判定（全部打印机事件的唯一判据）。

    返回 (ok, info)：info 含 order_id / status / owner / basis，供调用方打精确日志。

    规则（**fail-closed**）：
      ① 有当前领取者（operator_client 非空）→ 必须与来源连接一致；
      ② 无当前领取者 → 用 last_operator_client（**回滚/孤儿回收都不清空**）兜底 ——
         它覆盖真实且常见的路径：设备打印到一半断线（任务被回滚成 queued 且
         operator_client 被清空），设备恢复后从离线队列补报 print_success；
      ③ 两条都没有（从未领取过）⇒ 归属不可确定，**拒绝**。
         修前这里是"放行"，于是任意持全局 printer token 的设备猜个自增 id 就能改写
         别人的任务（标成功/判失败/抢占/冻结/续命）。

    为什么不用 pushed_tasks 兜底：它是内存计时器，ACCEPT_WAIT_TIMEOUT 之后条目即被删，
    而离线补报可能远晚于它 —— 用它会拒掉合法补报（重复打印）。
    """
    q = ("SELECT order_id, status, operator_client, last_operator_client"
         " FROM order_files WHERE id = ?")
    if conn is not None:
        row = conn.execute(q, (task_id,)).fetchone()
    else:
        c = get_db()
        try:
            row = c.execute(q, (task_id,)).fetchone()
        finally:
            c.close()
    if not row:
        return False, {"order_id": None, "status": "", "owner": "", "basis": "任务不存在"}
    claimed = (row["operator_client"] or "").strip()
    last = (row["last_operator_client"] or "").strip()
    if claimed:
        ok, owner, basis = (claimed == client_id), claimed, "当前领取者"
    elif last:
        ok, owner, basis = (last == client_id), last, "上次领取者"
    else:
        ok, owner, basis = False, "", "无归属记录"
    return ok, {"order_id": row["order_id"], "status": row["status"],
                "owner": owner, "basis": basis}


def _event_task_ids(data, event_name):
    """S0-04：SocketIO 事件的 task_ids 统一清洗。

    返回 int 列表；`None` 表示"丢弃本事件"（非法 / 超上限）。原实现在 1 MB 事件里塞
    5 万个 id 时会逐个 SELECT + 逐个改状态（事件线程被占死），现在上限 100 条。"""
    if not isinstance(data, dict):
        print(f"  [WARN] {event_name} 收到非 dict 载荷，已忽略")
        return None
    task_ids = data.get("task_ids") or data.get("task_id")
    if isinstance(task_ids, int):
        task_ids = [task_ids]
    if not task_ids:
        return None
    if not isinstance(task_ids, (list, tuple)):
        print(f"  [WARN] {event_name} 收到非法 task_ids 类型，已忽略")
        return None
    if len(task_ids) > MAX_EVENT_TASK_IDS:
        print(f"  [WARN] {event_name} task_ids 数量 {len(task_ids)} 超过上限 "
              f"{MAX_EVENT_TASK_IDS}，已整条丢弃")
        return None
    try:
        return [int(t) for t in task_ids]
    except (ValueError, TypeError):
        print(f"  [WARN] {event_name} 收到非法 task_ids 元素，已忽略")
        return None


def _get_event_client_id():
    """事件来源校验（P1-2）：用当前连接的 request.sid 反查 printer_clients 中的 client_id，
    查不到（连接未注册/伪造 sid）返回 None。"""
    return _find_client_id_by_sid(request.sid)


@sio_event("print_success")
def on_print_success(data):
    """打印成功 -- 更新子任务状态为 sent，并聚合父订单状态"""
    task_id = data.get("task_id")
    if not task_id:
        return

    try:
        task_id = int(task_id)
    except (ValueError, TypeError):
        print(f"  [WARN] print_success 收到非法 task_id: {task_id!r}，已忽略")
        return

    # 来源校验（P1-2）：仅该任务的领取客户端可回报成功
    client_id = _get_event_client_id()
    if not client_id:
        print(f"  [WARN] print_success 来自未注册连接（sid={request.sid}），已忽略")
        return

    # 来源校验（P1-2 + 🔴-D3）：仅该任务的归属设备可回报成功。
    # 修前写法是 `if row and row["operator_client"] and row["operator_client"] != client_id`
    # —— operator_client 被断线回滚清空后**完全放行**，任何注册设备都能把别人的任务标 sent。
    conn = get_db()
    row = conn.execute(
        "SELECT order_id, operator_client, file_id FROM order_files WHERE id = ?", (task_id,)
    ).fetchone()
    ok, own = _subtask_ownership(task_id, client_id, conn=conn)
    if not ok:
        print(f"  [WARN] print_success 子任务 #{task_id} 归属校验不过"
              f"（{own['basis']}={own['owner'] or '<空>'}，来源 {client_id}），已拒绝")
        conn.close()
        return
    # 回报前的状态：用于事后如实日志（failed / 三终态被覆盖时各说清原因）。
    prev_status = (own.get("status") or "").strip()

    # P1-5：状态集加入 queued（断线回滚闭环：回滚后客户端回报成功也能收敛为 sent）；
    # P0-1.4：完成时清空 locked_at
    # 2026-09：状态集加入 failed —— check_printing_timeout 的 3 分钟反馈窗口对缺纸/卡纸这类
    # 物理中断太短，任务被误判 failed 后，迟到的成功回报原本被白名单挡掉（静默丢弃，订单永久失败）。
    # 2026-10-18（用户口径）：**设备是这张单的最终裁决者** —— canceled/abandoned/rejected 也放行。
    # 场景：订正/取消发生在设备离线期间，这台机照旧把纸打了出来并如实回报成功；纸确实出来了，
    # 账就该算（人工订正只保证"云端不再主动让它打印"，不否定设备已出纸的事实）。
    # 反过来说：设备**没**打出来时根本不会有成功回报，所以这条放行不会凭空把取消单算成收益。
    # reject_reason：凡是从"失败/超时/打回/取消/放弃"翻成 sent 都要清掉（旧文案会误导以为这单没成）。
    conn.execute(
        "UPDATE order_files SET reject_reason = '' WHERE id = ?"
        " AND status IN ('failed', 'timeout', 'rejected', 'canceled', 'abandoned')",
        (task_id,),
    )
    conn.execute(
        "UPDATE order_files SET status = 'sent', locked_at = '' WHERE id = ? AND status IN ('printing', 'accepted', 'offline_unknown', 'waiting', 'downloading', 'queued', 'failed', 'timeout', 'canceled', 'abandoned', 'rejected')",
        (task_id,),
    )
    # 获取父订单 ID 并刷新聚合状态
    if row:
        # 2026-11：设备把「失败/超时/取消/放弃/打回」翻成 sent 时（救回），订单级的人工订正
        # 留痕也一并清掉 —— 这单已由设备最终裁决，再显示"由 XX 订正为失败"会误导用户与结算。
        # 只在这几种"救回"场景清；人工订正成 sent 的记录不会被一次迟到的成功回报抹掉。
        if (prev_status in ("failed", TIMEOUT_STATUS)
                or prev_status in NO_LONGER_PRINTABLE_STATUSES):
            conn.execute(
                "UPDATE orders SET manual_status_by = '', manual_status_at = '' WHERE id = ?",
                (row["order_id"],),
            )
        refresh_order_status(conn, row["order_id"])
        # 归属标记：本地打印工具回报成功时附带操作管理员姓名（订单号右侧下拉选择）。
        # 云端订单为顾客订单（is_admin_print 保持 0），仅盖章 owner_name；
        # 若客户端显式上报 is_admin_print 则一并写入。
        owner_name = (data.get("owner_name") or "").strip()
        admin_print = data.get("is_admin_print")
        if owner_name or admin_print is not None:
            if owner_name and admin_print is not None:
                conn.execute(
                    "UPDATE orders SET owner_name = ?, is_admin_print = ? WHERE id = ?",
                    (owner_name, 1 if admin_print else 0, row["order_id"]),
                )
            elif owner_name:
                conn.execute(
                    "UPDATE orders SET owner_name = ? WHERE id = ?",
                    (owner_name, row["order_id"]),
                )
    conn.commit()
    conn.close()

    if prev_status in NO_LONGER_PRINTABLE_STATUSES:
        # 设备最终裁决（2026-10-18 用户口径）：这台机真把纸打出来了，人工订正不否定这个事实 ⇒
        # 记 sent（进收益）。留一行日志，便于事后核对"账面与人工订正不一致"的原因。
        print(f"  [FIX] print_success 子任务 #{task_id}: 设备成功回报覆盖云端终态 "
              f"{prev_status} → sent（设备是最终裁决者）")
    elif prev_status == "failed":
        # 2026-09 的既有决定：3 分钟反馈超时把任务误判 failed 后，迟到的成功回报就是
        # 「纸确实出来了」的证据 ⇒ 救回 sent。人工订正为 failed 的订单同样适用，别加拦截。
        print(f"  [FIX] print_success 子任务 #{task_id} 由 failed 救回 sent（迟到的成功回报）")
    elif prev_status == TIMEOUT_STATUS:
        # 2026-11：超时（timeout）与 failed 同口径 —— 迟到的成功回报照样救回 sent，
        # 因为 timeout 的本意是"结果未知"，而不是"确定没打出来"。
        print(f"  [FIX] print_success 子任务 #{task_id} 由 timeout 救回 sent（迟到的成功回报）")

    with pushed_tasks_lock:
        pushed_tasks.pop(task_id, None)

    # 同步本地回报的实际打印配置（份数/双面/范围/页数）并重算价格
    _sync_subtask_config(task_id, data.get("copies"), data.get("duplex"),
                         data.get("page_range"), data.get("page_count"))
    if row and row["file_id"]:
        _purge_file_after_print(row["file_id"])

    print(f"  [OK] 子任务 #{task_id}: 客户端确认打印成功")
    # 🔴-D20：回执。python-socketio 的 emit 是"塞进缓冲就返回"，链路半开（后端重启/代理抖动）
    # 时客户端既收不到异常也收不到回应 ⇒ 它以为报上去了，3 分钟后 check_printing_timeout
    # 把这一单判 failed，纸却已经被顾客拿走。回一个 ack，客户端 15 秒收不到就转离线队列重放。
    emit("print_success_ack", {"task_id": task_id})


@sio_event("print_fail")
def on_print_fail(data):
    """打印失败 -- 更新子任务状态为 failed，并聚合父订单状态"""
    task_id = data.get("task_id")
    error = data.get("error", "未知错误")

    if not task_id:
        return

    try:
        task_id = int(task_id)
    except (ValueError, TypeError):
        print(f"  [WARN] print_fail 收到非法 task_id: {task_id!r}，已忽略")
        return

    # 来源校验（P1-2）：仅该任务的领取客户端可回报失败
    client_id = _get_event_client_id()
    if not client_id:
        print(f"  [WARN] print_fail 来自未注册连接（sid={request.sid}），已忽略")
        return

    print(f"  [FAIL] 子任务 #{task_id}: 客户端上报打印失败 ({error})")
    conn = get_db()
    row = conn.execute(
        "SELECT order_id, operator_client FROM order_files WHERE id = ?", (task_id,)
    ).fetchone()
    # 来源校验（🔴-D3）：同 print_success，统一走 _subtask_ownership（空领取者不再放行）
    ok, own = _subtask_ownership(task_id, client_id, conn=conn)
    if not ok:
        print(f"  [WARN] print_fail 子任务 #{task_id} 归属校验不过"
              f"（{own['basis']}={own['owner'] or '<空>'}，来源 {client_id}），已拒绝")
        conn.close()
        return
    # P1-5：禁止 sent → failed 降级（已完成的任务不能被迟到的失败回报改状态）；
    # 同时排除 canceled——用户已取消的任务不能被迟到的失败回报覆盖成 failed
    #（否则取消正在打印的订单时，本地工具对剩余任务回报的"已取消"失败会把 canceled
    #  覆盖成 failed，父订单显示"打印失败"）。
    # 与 print_success **刻意不对称**（2026-10-18 用户口径）：成功回报 = 「纸出来了」，设备说了算，
    # 连人工终态都能覆盖成 sent；失败回报不带来收益，且终态里的 canceled/abandoned/rejected 被覆盖成
    # failed 只会把「已取消」标签弄丢 —— 最典型的来源是「取消正在打印的订单」时本地工具对剩余任务
    # 回报的"已取消"失败（那是同一次取消的副产物，不是设备独立打的标记）。所以白名单只放
    # 分发/领取过程中的工作状态 + sent 排除（禁止 sent → failed 降级）。
    # 这边不含 failed 的原因是重复写入只会白白覆盖 reject_reason。
    # 失败原因写入 reject_reason（移动端订单详情/收支结算可展示）
    cur = conn.execute(
        "UPDATE order_files SET status = 'failed', locked_at = '', reject_reason = ? WHERE id = ?"
        " AND status IN ('printing', 'accepted', 'offline_unknown', 'waiting', 'downloading', 'queued')",
        (("打印失败: " + str(error))[:200], task_id),
    )
    # 获取父订单 ID 并刷新聚合状态
    if row:
        refresh_order_status(conn, row["order_id"])
    conn.commit()
    conn.close()

    if (cur.rowcount or 0) == 0:
        # 白名单挡下（已 sent / canceled·abandoned·rejected 终态 / 本就 failed）：状态该保持不动，
        # 但旧写法只在上方打一句「标记为失败」就完事，与实际未改动矛盾 —— 留一行如实日志。
        print(f"  [WARN] print_fail 子任务 #{task_id} 当前状态为 {own.get('status') or ''}"
              f"（不在可改白名单），迟到失败回报已忽略")
    else:
        print(f"  [FAIL] 子任务 #{task_id}: 状态已置为 failed")

    with pushed_tasks_lock:
        pushed_tasks.pop(task_id, None)

    # 🔴-D20：与 print_success 同口径 —— 回执让客户端知道"云端真的处理了这次失败回报"，
    # 15 秒收不到就转离线队列重放（半开链路下 emit 不会抛异常）。
    emit("print_fail_ack", {"task_id": task_id})


# ==================== 预约打印（无障碍打印的预约形式） ====================


def _find_client_id_by_sid(sid):
    """根据 SocketIO sid 反查 client_id（断线处理同款逻辑）"""
    with printer_clients_lock:
        for cid, info in printer_clients.items():
            if info["sid"] == sid:
                return cid
    return None


def _all_subtasks_ready(conn, order_id):
    """该预约订单的所有子任务是否已全部就绪（waiting/sent 等，无 downloading/scheduled/failed）"""
    rows = conn.execute(
        "SELECT status FROM order_files WHERE order_id = ?", (order_id,)
    ).fetchall()
    if not rows:
        return False
    return all(r["status"] in ("waiting", "sent", "accepted", "offline_unknown") for r in rows)


@sio_event("file_ready")
def on_file_ready(data):
    """预约单阶段①文件下载完成：downloading → waiting。
    若该订单之前被冻结（到点文件未就绪），且本任务补齐后全部就绪 → 解除冻结，
    重设 scheduled_at = now + 30s 缓冲，并向本地发 start_print 让其按新目标倒计时/打印。"""
    task_ids = _event_task_ids(data, "file_ready")
    if task_ids is None:
        return
    order_id = data.get("order_id")

    # 来源校验（P1-2）：仅领取该下载任务的客户端可回报就绪
    client_id = _get_event_client_id()
    if not client_id:
        print(f"  [WARN] file_ready 来自未注册连接（sid={request.sid}），已忽略")
        return

    resume_target = None
    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for tid in task_ids:
                # 来源校验（🔴-D3）：统一归属判定（空领取者不再放行）
                ok, own = _subtask_ownership(tid, client_id, conn=conn)
                if not ok:
                    print(f"  [WARN] file_ready 子任务 #{tid} 归属校验不过"
                          f"（{own['basis']}={own['owner'] or '<空>'}，来源 {client_id}），跳过")
                    continue
                row = conn.execute(
                    "SELECT order_id FROM order_files WHERE id = ? AND status = 'downloading'", (tid,)
                ).fetchone()
                if not row:
                    continue
                # 父订单已取消 → 迟到的下载完成不恢复为 waiting，直接保持 canceled（防已取消订单复活）
                o = conn.execute(
                    "SELECT status FROM orders WHERE id = ?", (row["order_id"],)
                ).fetchone()
                if o and o["status"] == "canceled":
                    print(f"  [SKIP] file_ready 子任务 #{tid}: 父订单已取消，保持 canceled")
                    continue
                conn.execute(
                    "UPDATE order_files SET status = 'waiting' WHERE id = ? AND status = 'downloading'",
                    (tid,),
                )
                refresh_order_status(conn, row["order_id"])
            if order_id:
                o = conn.execute(
                    "SELECT schedule_mode, schedule_frozen FROM orders WHERE id = ?", (order_id,)
                ).fetchone()
                if o and o["schedule_mode"] != "now" and o["schedule_frozen"] and _all_subtasks_ready(conn, order_id):
                    new_target = datetime.now() + timedelta(seconds=30)
                    new_target_str = new_target.strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute(
                        "UPDATE orders SET schedule_frozen = 0, schedule_frozen_at = '',"
                        " scheduled_at = ? WHERE id = ?",
                        (new_target_str, order_id),
                    )
                    resume_target = new_target_str
                    print(f"  [RESUME] 订单 #{order_id} 冻结解除，重设 {new_target_str}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    if resume_target and order_id:
        emit("start_print", {"order_id": order_id, "task_ids": task_ids,
                             "scheduled_at": resume_target,
                             "scheduled_ts": _iso_to_ts(resume_target)})
    print(f"  [READY] 预约单文件就绪: {task_ids}")


@sio_event("download_delayed")
def on_download_delayed(data):
    """预约单到点文件未就绪 → 冻结订单（暂停倒计时）。
    本地文件补齐后会重新发 file_ready，届时解除冻结并重设目标。"""
    task_ids = data.get("task_ids") or data.get("task_id")
    if isinstance(task_ids, int):
        task_ids = [task_ids]
    order_id = data.get("order_id")
    if not order_id:
        return

    # 来源校验（P1-2）：冻结操作必须来自该订单下载中任务的领取客户端
    client_id = _get_event_client_id()
    if not client_id:
        print(f"  [WARN] download_delayed 来自未注册连接（sid={request.sid}），已忽略")
        return
    conn_chk = get_db()
    chk_rows = conn_chk.execute(
        "SELECT id FROM order_files WHERE order_id = ? AND status = 'downloading'",
        (order_id,),
    ).fetchall()
    # 来源校验（🔴-D3）：订单里所有"下载中"的子任务都必须归属本机（任一不符 ⇒ 整体拒绝）；
    # 一条都没有时（例如下载中的任务已被回滚成 scheduled）退到订单维度：本机必须曾经领取过
    # 该订单的某个活跃子任务 —— 否则"从没碰过这个订单的设备"可以随手把它冻结。
    bad_basis = ""
    for _r in chk_rows:
        _ok, _own = _subtask_ownership(_r["id"], client_id, conn=conn_chk)
        if not _ok:
            bad_basis = f"子任务 #{_r['id']} {_own['basis']}={_own['owner'] or '<空>'}"
            break
    if not bad_basis and not chk_rows:
        act_rows = conn_chk.execute(
            "SELECT id FROM order_files WHERE order_id = ?"
            " AND status NOT IN ('sent', 'failed', 'timeout', 'canceled', 'rejected', 'abandoned')",
            (order_id,),
        ).fetchall()
        owned = any(_subtask_ownership(_r["id"], client_id, conn=conn_chk)[0]
                    for _r in act_rows)
        if not act_rows or not owned:
            bad_basis = "订单无可归属的活跃子任务"
    conn_chk.close()
    if bad_basis:
        print(f"  [WARN] download_delayed 订单 #{order_id} 归属校验不过"
              f"（{bad_basis}，来源 {client_id}），已拒绝")
        return

    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            o = conn.execute(
                "SELECT schedule_mode, status FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            # 父订单已取消 → 不再冻结（子任务已取消，无需续排）
            if o and o["schedule_mode"] != "now" and o["status"] != "canceled":
                conn.execute(
                    "UPDATE orders SET schedule_frozen = 1, schedule_frozen_at = ?, scheduled_at = ''"
                    " WHERE id = ?",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), order_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    print(f"  [FREEZE] 订单 #{order_id} 到点文件未就绪，已冻结")
    emit("download_delayed_ack", {"order_id": order_id, "frozen": 1})


@sio_event("start_printing")
def on_start_printing(data):
    """本地工具开始打印：记录订单实际开始打印的时刻（print_started_at，幂等写入），
    并驱动预约单 waiting/downloading → printing，登记 pushed_tasks
    启用 3 分钟超时兜底（与普通单一致）。断网时本地也可能直接打完后报 print_success，
    那时走 print_success 的 waiting 兼容分支。

    print_started_at 一律取**服务器时钟**（收到本事件的时刻），不接受客户端上报的时间：
    客户端时钟可能不准，且与 created_at（服务器时钟）跨设备相减会得出错误的等待时长。
    等待时长由后端用 calc_wait_seconds() 统一计算后下发 wait_seconds 字段。"""
    task_ids = _event_task_ids(data, "start_printing")
    if task_ids is None:
        return

    # 来源校验（P1-2）：仅领取该预约下载任务的客户端可上报开始打印
    client_id = _get_event_client_id()
    if not client_id:
        print(f"  [WARN] start_printing 来自未注册连接（sid={request.sid}），已忽略")
        return

    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            started_at = now_str  # 服务器时钟为唯一权威时间源
            for tid in task_ids:
                # 来源校验（🔴-D3）：统一归属判定（空领取者不再放行，防抢占别人的任务）
                ok, own = _subtask_ownership(tid, client_id, conn=conn)
                if not ok:
                    print(f"  [WARN] start_printing 子任务 #{tid} 归属校验不过"
                          f"（{own['basis']}={own['owner'] or '<空>'}，来源 {client_id}），跳过")
                    continue
                row = conn.execute(
                    "SELECT order_id FROM order_files WHERE id = ?", (tid,)
                ).fetchone()
                if not row:
                    continue
                # 父订单一律不许复活：canceled/abandoned/rejected = 「这单不该再有纸出来」
                # （结算页人工订正成这三种状态后，预约单到点也不该再开始打印）。
                # failed 刻意不在列 —— 它仍可被设备迟到的成功回报救回（见 NO_LONGER_PRINTABLE_STATUSES）。
                o = conn.execute(
                    "SELECT status FROM orders WHERE id = ?", (row["order_id"],)
                ).fetchone()
                if o and o["status"] in NO_LONGER_PRINTABLE_STATUSES:
                    print(f"  [SKIP] start_printing 子任务 #{tid}: 父订单状态为 {o['status']}，跳过")
                    continue
                # 记录实际开始打印时刻（幂等：仅首次，不覆盖早于本次上报的开始时间）
                conn.execute(
                    "UPDATE orders SET print_started_at = ? WHERE id = ?"
                    " AND (print_started_at IS NULL OR print_started_at = '')",
                    (started_at, row["order_id"]),
                )
                conn.execute(
                    "UPDATE order_files SET status = 'printing', operator_client = ?, locked_at = ?, last_operator_client = ? WHERE id = ? AND status IN ('waiting', 'downloading')",
                    (client_id, now_str, client_id, tid),
                )
                refresh_order_status(conn, row["order_id"])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    with pushed_tasks_lock:
        for tid in task_ids:
            pushed_tasks[tid] = {"pushed_at": datetime.now(), "client_id": client_id}
    print(f"  [START] 本地工具开始打印: {task_ids}")


@sio_event("stack_state")
def on_stack_state(data):
    """设备端上报「🖐 攒批模式」状态（2026-10）：连接成功后 + 每次状态/忙闲变化时发一次。

    data: {"mode": true|false|null, "busy": bool, "note": str?, "request_id": str?}
    服务端只镜像；若移动端正有在途的控制请求在等结果，这里顺带唤醒它并带回设备端的执行结果。
    """
    client_id = _get_event_client_id()
    if not client_id:
        print(f"  [WARN] stack_state 来自未注册连接（sid={request.sid}），已忽略")
        return
    payload = data or {}
    mode = _as_bool_or_none(payload.get("mode"))
    busy = bool(payload.get("busy"))
    note = str(payload.get("note") or "")
    _set_stack_state(client_id, mode, busy, note)
    with printer_stack_state_lock:
        waiter = printer_stack_waiters.pop(client_id, None)
        if waiter is not None:
            printer_stack_results[client_id] = {
                "mode": mode, "busy": busy, "note": note,
                "request_id": str(payload.get("request_id") or ""),
            }
    if waiter is not None:
        waiter.set()
    show = "开启" if mode else ("关闭" if mode is False else "未知")
    print(f"  [STACK] {client_id} 攒批模式={show} 忙={busy}" + (f"（{note}）" if note else ""))
    # 离线期望落地：这次上报是设备端刚给的真实状态 ⇒ 现在比对「期望 vs 实际」最可靠
    try:
        _apply_stack_intent_on_online(client_id, sid=request.sid)
    except Exception as e:
        print(f"[stack_intent] {client_id} 上线校验出错（已忽略，不影响状态镜像）: {e}")


@sio_event("print_keepalive")
def on_print_keepalive(data):
    """打印中保活：刷新仍在打印中的子任务的 locked_at，避免被超时机制误判。

    为什么需要（2026-10，手动双面/攒批模式）：
      · check_printing_timeout 对**已 accept** 的子任务按 locked_at 起算
        PRINT_FEEDBACK_TIMEOUT（3 分钟）判失败 —— 无自动双面器的机型（HP LaserJet 1020）
        一次作业要分两趟 + 人工翻纸，攒批模式下更要把多个订单的第 1 趟先堆起来再统一打
        第 2 趟，从 accept 到 print_success 轻易超过 3 分钟 → 顾客会看到「打印失败」。
      · recover_orphaned_printing_tasks 会把 locked_at 超过 5 分钟的孤儿 printing 任务
        回退 queued 重推 → 正在打印的任务被**重复打印**。
    两者都以 locked_at 为时间基准，所以本地工具在作业进行中每 60 秒发一次本事件把
    locked_at 往后刷即可；作业真正结束（print_success/print_fail）后停止发送，
    超时机制照常兜底（进程崩溃/断线时不再有保活，3 分钟后仍会被判失败，不会永远挂住）。

    data: {task_ids: [id, ...]}；来源必须是持有该子任务的接单设备。
    """
    task_ids = (data or {}).get("task_ids") or (data or {}).get("task_id")
    if isinstance(task_ids, int):
        task_ids = [task_ids]
    if not task_ids:
        return
    try:
        task_ids = [int(t) for t in task_ids]
    except (ValueError, TypeError):
        print(f"  [WARN] print_keepalive 收到非法 task_ids: {task_ids!r}，已忽略")
        return
    client_id = _get_event_client_id()
    if not client_id:
        print(f"  [WARN] print_keepalive 来自未注册连接（sid={request.sid}），已忽略")
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    refreshed = 0
    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for tid in task_ids:
                # 来源校验（🔴-D3）：统一归属判定 + 状态守卫（不是本机在打的别替它续命）
                ok, own = _subtask_ownership(tid, client_id, conn=conn)
                if not ok:
                    continue
                if own["status"] not in ("printing", "accepted"):
                    continue  # 已终结（sent/failed/取消）不再保活
                cur = conn.execute(
                    "UPDATE order_files SET locked_at = ? WHERE id = ?"
                    " AND status IN ('printing', 'accepted')",
                    (now_str, tid),
                )
                refreshed += cur.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    if refreshed:
        print(f"  [KEEPALIVE] {client_id} 保活 {refreshed} 个子任务（刷新 locked_at）")
    try:
        socketio.emit("print_keepalive_ack", {"task_ids": task_ids, "refreshed": refreshed},
                      to=request.sid)
    except Exception:
        pass


@sio_event("page_count_result")
def on_page_count_result(data):
    """本地打印工具回报文件页数分析结果。
    data: {file_id, page_count, orientation, success}"""
    file_id = data.get("file_id", "")
    try:
        page_count = int(data.get("page_count", 0) or 0)
    except (ValueError, TypeError):
        page_count = 0
    orientation = data.get("orientation", "")
    success = data.get("success", True)

    # 来源校验（P1-2 + 🔴-D3）：只有**被请求做这次分析的那台设备**可以回报。
    # 判据取 client_id 而不是 sid：sid 会随重连变化，设备分析到一半断线重连后
    # 仍应能回报（用 sid 会把合法回报挡掉，页数永远停在"分析中"）。
    client_id = _get_event_client_id()
    if not client_id:
        print(f"  [WARN] page_count_result 来自未注册连接（sid={request.sid}），已忽略")
        return

    with _last_page_analysis_push_lock:
        _asked = _analysis_push_sid.get(file_id) or {}
    _asked_cid = _asked.get("client_id") if isinstance(_asked, dict) else ""
    if not _asked_cid or _asked_cid != client_id:
        print(f"  [WARN] page_count_result 文件 {file_id[:8]}... 的分析请求发给"
              f" {_asked_cid or '<无记录>'}，来源却是 {client_id}，已拒绝")
        return

    if not file_id or page_count <= 0:
        print(f"  [PAGE] page_count_result 无效: file_id={file_id}, pages={page_count}")
        return
    # 🔴-D3：页数上限。页数是**计费输入**（calculate_price × 份数，还会回溯重算父订单总额），
    # 离谱的数值会直接炸掉价格并使回溯重算扫过所有引用该文件的子任务。
    if page_count > MAX_REPORTED_PAGES:
        print(f"  [PAGE] page_count_result 页数离谱（{page_count} > {MAX_REPORTED_PAGES}），已拒绝")
        return

    conn = get_db()
    conn.execute(
        "UPDATE files SET page_count = ?, page_count_verified = 1 WHERE id = ?",
        (page_count, file_id),
    )
    if page_count == 1:
        # 单页文件无法双面：用户在未知页数时可能选了双面 →
        # 强制引用该文件的活跃子任务改为单面，后端显示与价格随之一致
        conn.execute(
            "UPDATE order_files SET duplex = 'off' WHERE file_id = ?"
            " AND status IN ('queued', 'printing', 'accepted', 'waiting', 'downloading')",
            (file_id,),
        )
    # 同步更新 MD5 索引：下次同 MD5 文件上传直接复用页数，无需再次分析
    row = conn.execute("SELECT path, openid FROM files WHERE id = ?", (file_id,)).fetchone()
    conn.commit()
    conn.close()
    if row and row["path"] and os.path.exists(row["path"]):
        try:
            file_md5 = get_file_md5(row["path"])
            md5_index = load_md5_index()
            _md5_entry_set(md5_index, file_md5, page_count=page_count, page_count_verified=True,
                           openid=row["openid"])
            save_md5_index(md5_index)
            print(f"  [PAGE] MD5 索引已更新: {file_md5[:8]}... → {page_count} 页")
        except Exception as e:
            print(f"  [WARN] 更新 MD5 页数缓存失败: {e}")
    print(f"  [PAGE] ✓ 本地工具回报: {file_id[:8]}... → {page_count} 页 ({orientation})")

    # 验证使用该文件的所有活跃子任务的页码范围是否超出总页数
    _validate_page_ranges_for_file(file_id, page_count)

    # P2-1（docx 少计费）：页数修正后回溯更新引用该文件的子任务页数与价格、父订单总额
    _recalc_prices_for_file(file_id, page_count)


def _recalc_prices_for_file(file_id, page_count):
    """页数回报后回溯修正子任务价格与父订单总额（P2-1）：
    对引用该文件、状态未终结（非 sent/failed/canceled/rejected/abandoned）的子任务，
    用新页数重算 total_price（calculate_price × copies），并重算父订单 total_price
    （基础打印费 + 附加服务费，口径与 submit_order 一致），同时刷新聚合状态。"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, order_id, copies, duplex, page_range, simplex_pages FROM order_files"
            " WHERE file_id = ? AND status NOT IN ('sent','failed','timeout','canceled','rejected','abandoned')",
            (file_id,),
        ).fetchall()
        if not rows:
            return
        order_ids = set()
        for r in rows:
            per_copy = calculate_price(page_count, r["duplex"] or "on", r["page_range"] or "",
                                       simplex_pages=_row_simplex_pages(r))
            new_total = round(per_copy * (r["copies"] or 1), 2)
            conn.execute(
                "UPDATE order_files SET page_count = ?, total_price = ? WHERE id = ?",
                (page_count, new_total, r["id"]),
            )
            order_ids.add(r["order_id"])
        for oid in order_ids:
            sub_sum = conn.execute(
                "SELECT COALESCE(SUM(total_price), 0) FROM order_files WHERE order_id = ?",
                (oid,),
            ).fetchone()[0]
            o = conn.execute(
                "SELECT delivery_enabled, delivery_percentage, urgency_price, cover_page, cover_page_price"
                " FROM orders WHERE id = ?",
                (oid,),
            ).fetchone()
            if o:
                delivery_fee = round(float(sub_sum) * (o["delivery_percentage"] or 0) / 100, 2) if o["delivery_enabled"] else 0
                urgency_fee = float(o["urgency_price"] or 0)
                cover_fee = round(float(o["cover_page_price"] or 0) * o["cover_page"], 2) if o["cover_page"] else 0
                parent_total = round(float(sub_sum) + urgency_fee + cover_fee + delivery_fee, 2)
                # 2026-10-17：父订单页数一起刷新 —— 否则「我的」订单详情会出现
                # 「合计页数 1 页 / 金额却是 7 页的价」这种自相矛盾的展示。
                _pc = conn.execute(
                    "SELECT COALESCE(SUM(page_count * copies), 0) FROM order_files WHERE order_id = ?",
                    (oid,),
                ).fetchone()[0]
                conn.execute("UPDATE orders SET total_price = ?, page_count = ? WHERE id = ?",
                             (parent_total, int(_pc or 0), oid))
            refresh_order_status(conn, oid)
        conn.commit()
        print(f"  [PRICE] 已回溯重算 {len(rows)} 个子任务的价格（页数修正为 {page_count} 页）")
    except Exception as e:
        print(f"  [WARN] 价格回溯重算失败: {e}")
        conn.rollback()
    finally:
        conn.close()


def _sync_subtask_config(task_id, copies, duplex, page_range, page_count):
    """本地回报实际打印配置后，同步 order_files 并重算子任务价格与父订单总额。
    场景：用户在小程序选了份数/双面，本地打印工具可能在打印前修改（含单页强制单面），
    打印成功回报时带上实际配置，后端据此更新记录与价格。"""
    conn = get_db()
    try:
        row = conn.execute("SELECT id, order_id FROM order_files WHERE id = ?", (task_id,)).fetchone()
        if not row:
            conn.close()
            return
        set_parts = []
        params = []
        if copies is not None:
            try:
                c = int(copies)
                if 1 <= c <= 999:
                    set_parts.append("copies = ?"); params.append(c)
            except (ValueError, TypeError):
                pass
        if duplex in ("on", "off"):
            set_parts.append("duplex = ?"); params.append(duplex)
        if page_range is not None:
            set_parts.append("page_range = ?"); params.append(str(page_range)[:500])
        if page_count is not None:
            try:
                pc = int(page_count)
                if pc > 0:
                    set_parts.append("page_count = ?"); params.append(pc)
            except (ValueError, TypeError):
                pass
        if not set_parts:
            conn.close()
            return
        params.append(task_id)
        conn.execute(f"UPDATE order_files SET {', '.join(set_parts)} WHERE id = ?", params)
        # 用更新后的值重算子任务价格
        cur = conn.execute(
            "SELECT page_count, copies, duplex, page_range, simplex_pages FROM order_files WHERE id = ?", (task_id,)
        ).fetchone()
        if cur:
            per_copy = calculate_price(cur["page_count"] or 1, cur["duplex"] or "on",
                                       cur["page_range"] or "",
                                       simplex_pages=_row_simplex_pages(cur))
            new_total = round(per_copy * (cur["copies"] or 1), 2)
            conn.execute("UPDATE order_files SET total_price = ? WHERE id = ?", (new_total, task_id))
            # 重算父订单总额（基础打印费 + 附加服务费，口径与 submit_order 一致）
            oid = row["order_id"]
            sub_sum = conn.execute(
                "SELECT COALESCE(SUM(total_price), 0) FROM order_files WHERE order_id = ?", (oid,)
            ).fetchone()[0]
            o = conn.execute(
                "SELECT delivery_enabled, delivery_percentage, urgency_price, cover_page, cover_page_price"
                " FROM orders WHERE id = ?", (oid,)
            ).fetchone()
            if o:
                delivery_fee = round(float(sub_sum) * (o["delivery_percentage"] or 0) / 100, 2) if o["delivery_enabled"] else 0
                urgency_fee = float(o["urgency_price"] or 0)
                cover_fee = round(float(o["cover_page_price"] or 0) * o["cover_page"], 2) if o["cover_page"] else 0
                parent_total = round(float(sub_sum) + urgency_fee + cover_fee + delivery_fee, 2)
                _pc = conn.execute(
                    "SELECT COALESCE(SUM(page_count * copies), 0) FROM order_files WHERE order_id = ?",
                    (oid,),
                ).fetchone()[0]
                conn.execute("UPDATE orders SET total_price = ?, page_count = ? WHERE id = ?",
                             (parent_total, int(_pc or 0), oid))
            refresh_order_status(conn, oid)
        conn.commit()
        print(f"  [SYNC] 子任务 #{task_id} 实际打印配置已同步后端")
    except Exception as e:
        print(f"  [WARN] 子任务配置同步失败: {e}")
        conn.rollback()
        # 🔴-B10：回滚掉之后这一单的价格就停在估价上了（sent 已是终态，_recalc 不会再碰它）。
        # 至少把"待重算"标记落盘，交给 resync_pending_prices() 慢慢补 —— 不能让一次
        # 改价失败变成永久错账（顾客看到的价和实际纸张不符）。
        try:
            _c2 = get_db()
            _c2.execute("UPDATE order_files SET needs_repricing = 1 WHERE id = ?", (task_id,))
            _c2.commit()
            print(f"  [WARN] 子任务 #{task_id} 已标记 needs_repricing=1，等待定时重算")
        except Exception as e2:
            print(f"  [WARN] 标记 needs_repricing 失败: {e2}")
        finally:
            try:
                _c2.close()
            except Exception:
                pass
    finally:
        conn.close()


def _parse_page_range_max(range_str: str) -> int:
    """解析页码范围字符串（如 '1-5,7,9'），返回最大页码。空字符串或无效返回 0。"""
    if not range_str or not range_str.strip():
        return 0
    max_page = 0
    for part in range_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                max_page = max(max_page, int(a.strip()), int(b.strip()))
            except ValueError:
                pass
        else:
            try:
                max_page = max(max_page, int(part))
            except ValueError:
                pass
    return max_page


def _parse_page_range_set(range_str: str, total_pages: int) -> set:
    """解析页码范围字符串为页码集合（对齐本地 printer_config._parse_range_parts）。
    支持 '1-5,7,9'、中文逗号/顿号分隔、智能拆分 '23-4' → {2,3,4}；越界部分忽略。

    P0（2026-10 审查 🔴-1）：迭代前先把区间收窄到 [1, total_pages]。**结果集合完全不变**
    （原先"全区间遍历 + 逐个过滤"），但不再为 "1-999999999" 建十亿个 int —— 实测
    1e7 区间 = 198ms，线性外推 1e9 ≈ 20s/次，而 `workers=1/threads=16` 下单个已登录
    请求即可占满线程并（配合 INSERT 持有的 SQLite 写锁）拖垮全站。
    同款写法见 local_print_tool/pdf_printer.py:299-305（其 docstring 要求"两处同步"）。
    """
    pages: set = set()
    if not range_str or not range_str.strip():
        return pages
    raw = range_str.strip().replace("、", ",").replace("，", ",").replace("；", ",").replace(" ", "")
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                start, end = int(a), int(b)
                if start < end:
                    for p in range(max(start, 1), min(end, total_pages) + 1):
                        pages.add(p)
                elif start > end and len(a) > 1:
                    # 智能拆分: "23-4" → 页码 2 + 范围 3-4
                    prefix = int(a[:-1])
                    last = int(a[-1])
                    if prefix < end:
                        for p in range(max(last, 1), min(end, total_pages) + 1):
                            pages.add(p)
                        if 1 <= prefix <= total_pages:
                            pages.add(prefix)
            except ValueError:
                pass
        else:
            try:
                p = int(part)
                if 1 <= p <= total_pages:
                    pages.add(p)
            except ValueError:
                pass
    return pages


def _count_pages_in_range(page_range: str, total_pages: int) -> int:
    """返回页码范围覆盖的有效打印页数；空范围返回总页数（与本地 calc_cost 同口径）。"""
    if not page_range or not page_range.strip():
        return total_pages
    pages = _parse_page_range_set(page_range, total_pages)
    return len(pages) if pages else total_pages


def _file_range_stats(page_count, copies, duplex, page_range="", simplex_pages=""):
    """文件页数/纸张统计（口径与 calculate_price 一致，张数模型对齐本地工具 _fileSheets）：
    - effective_pages: 每份有效打印页数（页码范围过滤后，空范围 = 整份）
    - sheets:          每份纸张张数（单面每页 1 张；双面每 2 页 1 张，奇数页最后一张仍占 1 张）
    - total_pages:     文件合计页数 = 有效页数 × 份数
    - total_sheets:    文件合计张数 = 每份张数 × 份数
    simplex_pages：「封面单独打印」页（每页各占一张纸）；非空时双面张数按
    _charge_units 摊纸（不是 ceil(eff/2)：31 页里 9 页单面是 20 张，不是 16 张）。
    """
    eff = _count_pages_in_range(page_range or "", page_count)
    if duplex == "on":
        _dup, _single = _charge_units(page_count, page_range, simplex_pages)
        sheets = (_dup + _single) if (_dup + _single) > 0 else math.ceil(eff / 2)
    else:
        sheets = eff
    return {
        "effective_pages": eff,
        "sheets": sheets,
        "total_pages": eff * copies,
        "total_sheets": sheets * copies,
    }


def _validate_page_ranges_for_file(file_id: str, page_count: int):
    """检查使用指定文件的所有活跃子任务，若页码范围超出总页数则自动打回。"""
    conn = get_db()
    tasks = conn.execute(
        "SELECT id, order_id, page_range, simplex_pages FROM order_files"
        " WHERE file_id = ? AND status IN ('queued', 'printing')",
        (file_id,),
    ).fetchall()

    rejected = 0
    rejected_order_ids = set()
    for t in tasks:
        max_page = _parse_page_range_max(t["page_range"] or "")
        if max_page > page_count:
            conn.execute(
                "UPDATE order_files SET status = 'rejected', reject_reason = ? WHERE id = ?",
                (f"页码范围超出文件总页数（填写 {max_page} 页，文件共 {page_count} 页）", t["id"]),
            )
            rejected += 1
            rejected_order_ids.add(t["order_id"])
            continue
        # 「封面单独打印」的页同样要拿**真实页数**复核一遍：提交时若页数还没分析出来
        # （page_count_verified=0）就没法校验，等设备端回报页数时在这里补打回。
        _sim, _sim_reason = _simplex_pages_checked(
            _row_simplex_pages(t), t["page_range"] or "", page_count, True)
        if _sim_reason:
            conn.execute(
                "UPDATE order_files SET status = 'rejected', reject_reason = ? WHERE id = ?",
                (_sim_reason, t["id"]),
            )
            print(f"  [REJECT] 页数验证时打回 simplex: {_sim_reason}")
            rejected += 1
            rejected_order_ids.add(t["order_id"])

    if rejected > 0:
        conn.commit()
        for oid in rejected_order_ids:
            refresh_order_status(conn, oid)
        conn.commit()
        print(f"  [REJECT] 页数验证完成: {file_id[:8]}... → 打回 {rejected} 个子任务（{len(rejected_order_ids)} 个订单），页数 {page_count}")
    conn.close()


@sio_event("page_range_truncated")
def on_page_range_truncated(data):
    """本地打印工具回报：某任务的页码范围被截断。
    data: {task_id, original_range, effective_range, total_pages}"""
    task_id = data.get("task_id")
    original = data.get("original_range", "")
    effective = data.get("effective_range", "")
    total_pages = data.get("total_pages", 0)

    if not task_id:
        return

    try:
        task_id = int(task_id)
    except (ValueError, TypeError):
        print(f"  [WARN] page_range_truncated 收到非法 task_id: {task_id!r}，已忽略")
        return

    # 来源校验（🔴-D3）：本区段此前是唯一**既无来源校验、又无状态守卫、又无长度上限**的
    # 事件处理器 —— 任意注册设备都能把它人的 page_range 改写成任意（超长）字符串，
    # 也就是把 🔴-1 的毒串投进别人的订单记录。三样一起补：
    client_id = _get_event_client_id()
    if not client_id:
        print(f"  [WARN] page_range_truncated 来自未注册连接（sid={request.sid}），已忽略")
        return
    conn = get_db()
    try:
        ok, own = _subtask_ownership(task_id, client_id, conn=conn)
        if not ok:
            print(f"  [WARN] page_range_truncated 子任务 #{task_id} 归属校验不过"
                  f"（{own['basis']}={own['owner'] or '<空>'}，来源 {client_id}），已拒绝")
            return
        # 状态守卫：已终结/已取消的任务不再接受改写（记录已定，改写只会污染对账）
        if own["status"] in ("canceled", "rejected", "abandoned", "sent"):
            print(f"  [SKIP] page_range_truncated 子任务 #{task_id} 状态为 {own['status']}，不再改写")
            return
        # 长度上限：与同文件 _sync_subtask_config 的写路径同款（那边的 page_range 也是 [:500]）
        original = str(original or "")[:500]
        effective = str(effective or "")[:500]
        print(f"  [TRUNC] 子任务 #{task_id}: 页码范围被截断 {original} → {effective} (总 {total_pages} 页)")
        conn.execute(
            "UPDATE order_files SET page_range_original = ?, page_range_truncated = 1, page_range = ? WHERE id = ?",
            (original, effective, task_id),
        )
        # 2026-11-02：截断多半是「排除末尾空白页」造成的（有效页数变少）⇒ 必须按新页数
        # 重算子任务价格与父订单总额，否则"排除掉的空白页不收钱"这句口径就落不了地。
        _row = conn.execute(
            "SELECT order_id, copies, duplex, page_range, page_count, simplex_pages"
            " FROM order_files WHERE id = ?", (task_id,)
        ).fetchone()
        if _row:
            _pc = max(int(_row["page_count"] or 0), int(total_pages or 0))
            _sim = _row_simplex_pages(_row)
            if _pc > 0 and _sim:
                # 单面页可能整段落在被排掉的尾巴里 ⇒ 收敛到「新页码范围」上再计价
                _keep = (_simplex_page_set(_pc, _sim)
                         & set(_selected_page_list(_pc, _row["page_range"] or "")))
                _sim = _format_page_selection(_keep)
                conn.execute("UPDATE order_files SET simplex_pages = ? WHERE id = ?", (_sim, task_id))
            _eff_pc = _pc if _pc > 0 else 1
            _per_copy = calculate_price(_eff_pc, _row["duplex"] or "on",
                                        _row["page_range"] or "", simplex_pages=_sim)
            _new_total = round(_per_copy * (_row["copies"] or 1), 2)
            conn.execute("UPDATE order_files SET total_price = ? WHERE id = ?", (_new_total, task_id))
            _oid = _row["order_id"]
            _sub_sum = conn.execute(
                "SELECT COALESCE(SUM(total_price), 0) FROM order_files WHERE order_id = ?", (_oid,)
            ).fetchone()[0]
            _o = conn.execute(
                "SELECT delivery_enabled, delivery_percentage, urgency_price, cover_page, cover_page_price"
                " FROM orders WHERE id = ?", (_oid,)
            ).fetchone()
            if _o:
                _delivery = (round(float(_sub_sum) * (_o["delivery_percentage"] or 0) / 100, 2)
                             if _o["delivery_enabled"] else 0)
                _urgency = float(_o["urgency_price"] or 0)
                _cover = (round(float(_o["cover_page_price"] or 0) * _o["cover_page"], 2)
                          if _o["cover_page"] else 0)
                conn.execute(
                    "UPDATE orders SET total_price = ? WHERE id = ?",
                    (round(float(_sub_sum) + _urgency + _cover + _delivery, 2), _oid),
                )
            conn.commit()
            print(f"  [PRICE] 截断后重算 子任务 #{task_id}: {_new_total} 元（simplex={_sim or '无'}）")
    finally:
        conn.close()


# ── 在线设备日志收集（日志管理页发起：后端下发 request_log → 各在线设备回报本机日志）──
_collect_logs_lock = threading.Lock()
# S1-07：用 Condition 替代 `time.sleep(0.2)` 轮询 —— 设备回报即唤醒，最坏只等
# `COLLECT_MAX_WAIT` 秒（原实现可占线程 20 秒，是 R1 的直接放大器）。
_collect_cond = threading.Condition(_collect_logs_lock)
# 全局只允许一次收集在飞（第二次直接 503，不排队、不占线程）
_collect_sem = threading.Semaphore(1)
COLLECT_MAX_WAIT = 3.0
_collect_logs: dict = {}      # client_id → 设备回报的日志内容
_collect_request_id = 0       # 区分多次收集请求，防旧回报串台
_COLLECT_LOG_MAX_BYTES = 200 * 1024  # 每台设备最多回报 200KB 尾部日志


@sio_event("logs")
def on_device_logs(data):
    """在线设备回报日志（配合 /api/log/collect_all 下发的 request_log 事件）。"""
    client_id = _find_client_id_by_sid(request.sid)
    if not client_id:
        return
    if not isinstance(data, dict):
        return
    # 仅接受本次收集请求（request_id 匹配）的回报，防上一轮迟到的回报串台
    with _collect_logs_lock:
        if data.get("request_id") != _collect_request_id:
            return
        content = str(data.get("content", "") or "")
        _collect_logs[client_id] = content[:_COLLECT_LOG_MAX_BYTES]
        try:
            _collect_cond.notify_all()   # S1-07：唤醒等待中的收集请求（持锁调用）
        except RuntimeError:
            pass


# ==================== 页面分析请求（推送到本地打印工具）====================


def _notify_clients(event: str, data: dict):
    """向所有活跃的打印机客户端广播事件（如储存配置更新、清空缓存等）。"""
    active = get_active_clients()
    if not active:
        return
    for client_id in active:
        with printer_clients_lock:
            info = printer_clients.get(client_id)
            sid = info["sid"] if info else None
        if sid:
            try:
                socketio.emit(event, data, to=sid)
            except Exception as e:
                print(f"  [NOTIFY] 推送 {event} 到 {client_id} 失败: {e}")


def _resolve_analysis_client(prefer_client: str, file_id: str) -> tuple:
    """选「由哪台设备做页数分析」。返回 (client_id, 依据说明)，都不满足返回 ("", 原因)。

    页数分析要把整个文件下载到那台机器上、用 Word/WPS 转 PDF 才能数页（并把 PDF 存进
    它的本地缓存），所以"发给谁"是有成本的，不能随便挑第一台在线设备。优先级：
      ① 调用方指定（前端当前选中的接单设备 / 刚刚上线的设备）；
      ② 该文件**活跃订单**的目标设备（orders.target_client）——谁接这单谁分析；
      ③ 兜底：任一在线接单设备（文件与订单无关时，早点拿到页数对用户更有价值）。
    仅考虑「在线 + 启用接单」的设备；目标设备离线 → 返回空，等它上线由 on_connect 补推。"""
    online = set(get_active_clients())
    claiming = get_claiming_device_ids()

    def _usable(cid):
        return bool(cid) and cid in claiming and cid in online

    if _usable(prefer_client):
        return prefer_client, "调用方指定"
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT o.target_client FROM order_files of JOIN orders o ON of.order_id = o.id"
            " WHERE of.file_id = ? AND o.target_client != ''"
            " AND of.status IN ('scheduled', 'downloading', 'waiting', 'queued', 'printing', 'accepted')"
            " ORDER BY of.id DESC LIMIT 1",
            (file_id,),
        ).fetchone()
        conn.close()
        if row and _usable(row["target_client"]):
            return row["target_client"], "订单目标设备"
    except Exception:
        pass
    for cid in claiming:
        if cid in online:
            return cid, "任一在线接单设备"
    return "", "无在线接单设备"


def _pending_page_analysis_rows(conn, limit: int = 20):
    """「还需要本地打印工具分析页数」的文件行（接单设备上线补推的判据；/api/file_page 同源）。

    2026-10-17 修复：判据原先是写死的 `f.page_count = 0`，而 submit_order 过去会把
    「页数未知」的 Word 按 1 页兜底值**写回 files.page_count** —— 顾客一旦在分析完成
    前提交，这个文件就再也不满足 page_count = 0，从此不会被送去分析：verified 永远 0、
    价格永久停在 1 页（少收），前端「复制价格」永久被拦。

    现在按「是否已被本地工具验证过」判断：
      · page_count = 0                 —— 还没数过（Word 等需转换的类型）；
      · 或 doc/docx 且 verified = 0     —— 存量被兜底值冻结的 Word 行（自愈）。
    PDF / 图片 / 文本类在上传时服务端直接数页，verified = 0 只表示「没经过本地工具」，
    不需要也不该再推一次分析（否则每次上线都白下载转换一遍）。
    """
    return conn.execute(
        "SELECT DISTINCT f.id, f.original_name FROM files f"
        " LEFT JOIN order_files of ON f.id = of.file_id"
        " WHERE f.page_count_verified = 0 AND f.path != ''"
        " AND (f.page_count = 0 OR lower(f.original_name) LIKE '%.doc'"
        "      OR lower(f.original_name) LIKE '%.docx')"
        " AND (of.id IS NULL OR of.status IN ('queued', 'printing', 'accepted'))"
        " LIMIT ?",
        (int(limit),),
    ).fetchall()


def request_page_analysis(file_id: str, file_name: str, prefer_client: str = "") -> bool:
    """请求本地打印工具下载文件并分析页数。成功推送返回 True。

    prefer_client：优先由哪台设备分析（前端当前选中的接单设备 / 刚上线的设备）；
    为空时按 _resolve_analysis_client 的优先级（订单目标设备 → 任一在线接单设备）选。"""
    client_id, why = _resolve_analysis_client(prefer_client or "", file_id)
    if not client_id:
        print(f"  [PAGE] {why}，跳过页数分析请求: {file_name} (file_id={file_id[:8]}...)")
        return False

    # 幽灵文件校验：物理文件已清理/路径已清空 → 跳过（下载必然 404，
    # 避免打印机上线补推 / 前端轮询触发时反复推送已清理文件刷屏）
    try:
        conn = get_db()
        prow = conn.execute(
            "SELECT path, page_count, page_count_verified, md5, openid FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
        conn.close()
        if not prow or not prow["path"] or not os.path.isfile(prow["path"]):
            print(f"  [PAGE] 文件已清理，跳过页数分析: {file_name} (file_id={file_id[:8]}...)")
            return False
        # 防御性自检：DB 已存验证页数 → 不再发起分析（各调用路径可能遗漏此判断，
        # 例如 MD5 索引丢失但 files 表仍有验证值）
        if prow["page_count_verified"] and (prow["page_count"] or 0) > 0:
            print(f"  [PAGE] 页数已验证 ({prow['page_count']} 页)，跳过分析: {file_name}")
            return False
        file_md5 = prow["md5"] or ""
    except Exception:
        return False

    print(f"  [PAGE] 分析设备: {client_id}（{why}）→ {file_name}")

    # 统一去重（30s）：on_connect 补推 / 前端 file_page 轮询触发 / 上传共用此字典，
    # 避免同一文件在短时间内被多个路径重复推送（"补推 + 轮询"双保险不能变成双推）。
    # 幽灵校验在前（幽灵不占防抖槽）；有效推送记录时间戳。
    with _last_page_analysis_push_lock:
        last = _last_page_analysis_push.get(file_id, 0)
        now = time.time()
        if now - last < 30:
            print(f"  [PAGE] 30s 内已推送过 {file_name}，跳过重复分析请求")
            return False
        _last_page_analysis_push[file_id] = now
        if len(_last_page_analysis_push) > 5000:
            _last_page_analysis_push.clear()   # 超限清空，防止字典无限增长

    download_url = make_download_url(file_id)

    with printer_clients_lock:
        client_info = printer_clients.get(client_id)
        sid = client_info["sid"] if client_info else None

    if not sid:
        return False

    # 记录推送目标，供取消分析时精确转发、以及 page_count_result 的归属判定（🔴-D3）
    # （同一 file_id 重新推送会覆盖为最新目标）
    with _last_page_analysis_push_lock:
        _analysis_push_sid[file_id] = {"sid": sid, "client_id": client_id}

    try:
        socketio.emit("analyze_page_count", {
            "file_id": file_id,
            "file_name": file_name,
            "download_url": download_url,
            "source_md5": file_md5,   # 本地可直接按 MD5 命中 PDF 缓存，无需下载文件
            # 分析用哪个引擎必须与**打印**同一口径（2026-10-28 起只看上传者）：
            # `files.openid` 就是这份副本的上传者（S2-05 起每次上传各自一行、不共享物理文件），
            # 因此这里给出的是与打印任务 `uploader_is_admin` 完全同源的证据。
            # 缺该字段的旧客户端照旧强制 LibreOffice（S1-04 默认口径不破）。
            "uploader_is_admin": is_admin_uploader(prow["openid"] or ""),
        }, to=sid)
        print(f"  [PAGE] 已推送页数分析请求: {file_name} (file_id={file_id[:8]}...) → {client_id}")
        return True
    except Exception as e:
        print(f"  [PAGE] 推送分析请求失败: {e}")
        return False


# ==================== 认证 ====================


def login_required(f):
    """装饰器：验证 Bearer token，将 openid 注入 g.openid"""

    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"success": False, "message": "未登录，请先授权"}), 401
        token = auth[7:]  # "Bearer " 之后的内容
        try:
            g.openid = _load_login_token(token)
        except TokenRevoked:
            # S1-06：ver 对不上 = 该账号已"退出全部设备"
            return jsonify({"success": False, "message": "登录已失效，请重新登录"}), 401
        except SignatureExpired:
            return jsonify({"success": False, "message": "登录已过期，请重新登录"}), 401
        except BadSignature:
            return jsonify({"success": False, "message": "无效的登录凭证"}), 401
        return f(*args, **kwargs)

    return decorated


def require_printer_access(f):
    """装饰器：验证登录 + 检查非访客（需管理员或许可用户）"""

    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"success": False, "message": "未登录，请先授权"}), 401
        token = auth[7:]
        try:
            g.openid = _load_login_token(token)
        except TokenRevoked:
            return jsonify({"success": False, "message": "登录已失效，请重新登录"}), 401
        except SignatureExpired:
            return jsonify({"success": False, "message": "登录已过期，请重新登录"}), 401
        except BadSignature:
            return jsonify({"success": False, "message": "无效的登录凭证"}), 401

        role = compute_role(g.openid)
        if role not in ("admin", "user"):
            # 检查临时授权（temp_until > 当前时间）
            # （P2-17）时区说明：temp_until 为服务器本地时间，依赖服务器时区，
            # 部署时须配置为 Asia/Shanghai
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn = get_user_db()
            row = conn.execute(
                "SELECT temp_until FROM users WHERE openid = ?", (g.openid,)
            ).fetchone()
            conn.close()
            if not row or not row["temp_until"] or row["temp_until"] <= now_str:
                return jsonify({"success": False, "message": "请先出示管理员许可"}), 403
        g.user_role = role
        return f(*args, **kwargs)

    return decorated


# ==================== API 路由 ====================


@app.before_request
def limit_json_body():
    """P2-11：JSON 请求体上限 1MB（全局 MAX_CONTENT_LENGTH=50MB 只防文件上传撑爆内存，
    JSON 接口正常数据远小于 1MB，超大 JSON 多为异常请求）。multipart 文件上传不受影响。"""
    if request.is_json:
        cl = request.content_length or 0
        if cl > 1024 * 1024:
            return jsonify({"success": False, "message": "请求体过大"}), 413


# ===== S0-04：统一错误面（R8 / DS-20）=====
@app.errorhandler(RecursionError)
def _on_recursion_error(e):
    """深嵌套 JSON（depth ≥ 1000）会抛 RecursionError —— 它不是 ValueError，
    `get_json(silent=True)` 挡不住；不处理时 Werkzeug 回 HTML 500 并打完整 traceback。
    统一回 400（fail-closed），只记一行。"""
    print(f"[BOUNDS] RecursionError {request.method} {request.path} ip={request.remote_addr}")
    return jsonify({"success": False, "message": "请求体格式不合法"}), 400


@app.errorhandler(Exception)
def _on_unhandled_exception(e):
    """兜底错误面：HTTP 异常原样放行（否则 404/403 会被改成 500）；
    其余只记**一行**结构化日志，不打印完整 traceback（traceback 会带绝对路径与内部变量）。"""
    if isinstance(e, HTTPException):
        return e
    print(f"[ERROR] {request.method} {request.path} ip={request.remote_addr} "
          f"exc={type(e).__name__}: {_redact_paths(_scrub_secret(str(e)))[:160]}")
    return jsonify({"success": False, "message": "服务器内部错误"}), 500


@app.route("/api/ping")
def ping():
    return {"msg": "pong", "status": "ok"}


@app.route("/api/printer_status", methods=["GET"])
def printer_status():
    """返回打印机在线状态（P2-13：基于心跳统计 get_active_clients，剔除心跳超时的僵尸注册）。
    2026-12：多设备接单 —— claiming_count 为启用接单的设备数（含离线），
    take_orders_online 表示是否有在线接单设备。"""
    active = get_active_clients()
    online_count = len(active)
    claiming_ids = get_claiming_device_ids()
    online_claiming = [c for c in claiming_ids if c in set(active)]
    return jsonify({
        "success": True,
        "online": online_count > 0,
        "active": online_count > 0,
        "count": online_count,
        "client_count": online_count,
        # 接单设备（多设备集合）
        "take_orders_online": len(online_claiming) > 0,
        "claiming_count": len(claiming_ids),
        "active_client_id": online_claiming[0] if online_claiming else "",
        # S2-04：公开接口不下发真实姓名（真名只在 /api/printer/devices 的管理 scope 里）
        "active_owner_name": _mask_person_name(
            get_device_owner(online_claiming[0]) if online_claiming else ""),
    })


@app.route("/api/pricing", methods=["GET"])
def get_pricing():
    """返回打印定价配置（地点、优先级、首页费等），前端加载后与本地打印工具保持一致。"""
    pricing_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing.json")
    try:
        with open(pricing_path, "r", encoding="utf-8") as f:
            pricing = json.load(f)
        return jsonify({"success": True, "pricing": pricing})
    except (FileNotFoundError, json.JSONDecodeError) as e:
        return jsonify({"success": False, "message": f"定价配置不可用: {e}"}), 500


@app.route("/api/pricing", methods=["POST"])
@require_printer_scope("admin")
def save_pricing():
    """更新打印定价配置（pricing.json，需 printer token 认证）。
    价格权威源：小程序/APP 计费显示、后端订单计价（calculate_price / submit_order）、
    本地打印工具打印首页全部从 pricing.json 读取，因此统一在此处维护。
    body: { "pricing": { ...完整 pricing.json 结构... } }（兼容直接传 pricing 对象）
    """
    token = _get_printer_token() or (request.get_json(silent=True) or {}).get("token", "")
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    payload = data.get("pricing", data)
    if not isinstance(payload, dict):
        return jsonify({"success": False, "message": "缺少 pricing"}), 400

    pricing_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing.json")
    # 读取现有配置作为缺失字段的兜底（允许部分更新）
    try:
        with open(pricing_path, "r", encoding="utf-8") as f:
            current = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        current = {}

    merged = dict(current)

    # P1-4：边界钳制 + 类型校验，非法值直接 400 拒绝（不写盘）。
    # 选择性合并：标量字段按 payload 覆盖；delivery_percentages / urgency_prices 为空 dict
    # 时视为未修改（保留现有），防御前端误传空表清空服务器配置。
    try:
        for _k in ("simplex_price", "duplex_price", "cover_page_price", "pickup_address"):
            if _k in payload:
                merged[_k] = payload[_k]
        merged["simplex_price"] = max(0.01, min(99.99, float(merged.get("simplex_price", 0.2))))
        merged["duplex_price"] = max(0.01, min(99.99, float(merged.get("duplex_price", 0.3))))
        merged["cover_page_price"] = max(0.0, min(99.99, float(merged.get("cover_page_price", 0.1))))
        merged["pickup_address"] = str(merged.get("pickup_address", "") or "")[:100]
        dp = payload.get("delivery_percentages")
        if dp is not None and not isinstance(dp, dict):
            return jsonify({"success": False, "message": "delivery_percentages 格式不正确"}), 400
        if dp:
            merged["delivery_percentages"] = {
                str(k): max(0.0, min(100.0, float(v))) for k, v in dp.items()
            }
        merged["delivery_locations"] = [str(x) for x in (merged.get("delivery_locations") or list(merged.get("delivery_percentages", {}).keys()))]
        up = payload.get("urgency_prices")
        if up is not None and not isinstance(up, dict):
            return jsonify({"success": False, "message": "urgency_prices 格式不正确"}), 400
        if up:
            ordered_up = {str(k): max(0.0, min(99.99, float(v))) for k, v in up.items()}
            # 按档位业务顺序重排（低→中→高），未知档位按原顺序追加在后
            sorted_up = {lvl: ordered_up[lvl] for lvl in ("低", "中", "高") if lvl in ordered_up}
            for _k, _v in ordered_up.items():
                if _k not in sorted_up:
                    sorted_up[_k] = _v
            merged["urgency_prices"] = sorted_up
        merged["urgency_levels"] = [str(x) for x in (merged.get("urgency_levels") or list(merged.get("urgency_prices", {}).keys()))]
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "定价参数格式不正确"}), 400

    # 原子写盘：先写临时文件再替换，避免进程中断留下半个 pricing.json
    tmp_path = pricing_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, pricing_path)
    except OSError:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return jsonify({"success": False, "message": "定价配置写入失败"}), 500

    print(f"定价配置已更新: 单面={merged['simplex_price']} 双面={merged['duplex_price']} "
          f"首页费={merged['cover_page_price']} 派送地点={len(merged['delivery_percentages'])} 个")
    return jsonify({"success": True, "message": "定价配置已更新", "pricing": merged})


@app.route("/api/file_page/<file_id>", methods=["GET"])
@login_required
def get_file_page(file_id):
    """查询文件的页数信息（供前端轮询本地工具分析结果）。
    返回 page_count 和 page_count_verified 标志。

    S1-08：每用户限速。客户端每 5s 轮询一个文件（12 次/分），6 个待验证文件 ≈72 次/分，
    故阈值取 300/分（正常用量的 4 倍），只拦"一个 token 刷爆轮询"的情形。

    惰性触发：打印机在线但文件页数仍未知（上传时离线、补推遗漏）→ 主动补推分析请求
    （去重由 request_page_analysis 内部统一 30s 防抖，与 on_connect 补推共享）。"""
    # S1-08：先校验参数（file_id 规范长度）再进限速 key
    if not isinstance(file_id, str) or not (0 < len(file_id) <= 64):
        return jsonify({"success": False, "message": "文件不存在"}), 404
    if not _rate_limit_ok(f"file_page:{g.openid}", 300, 60):
        return jsonify({"success": False, "message": "请求过于频繁，请稍后再试"}), 429
    conn = get_db()
    row = conn.execute(
        "SELECT page_count, page_count_verified, original_name, path FROM files WHERE id = ?", (file_id,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"success": False, "message": "文件不存在"}), 404
    printer_online = is_printer_available()  # 2026-11：接单设备在线才算打印机可用
    fname = row["original_name"] or ""
    ext = os.path.splitext(fname)[1].lower()
    # 2026-10-17：判据与 on_connect 的补推同源（_pending_page_analysis_rows）——
    # doc/docx 只要 page_count_verified=0 就补推，哪怕 files.page_count 被历史兜底值写成 1
    # （否则「页数未知时提交」的 Word 会被永久冻结在 1 页）。
    if (printer_online and not row["page_count_verified"]
            and ((row["page_count"] or 0) <= 0 or ext in (".doc", ".docx"))):
        # 前端把当前选中的接单设备带上来 → 优先由它分析（用户选了哪台就哪台下载/转换）；
        # 目标设备离线时 _resolve_analysis_client 会退回订单目标/任一在线接单设备
        prefer_client = (request.args.get("target_client_id", "") or "").strip()[:64]
        # 统一防抖在 request_page_analysis 内部：30s 内已被补推/轮询推过则跳过
        request_page_analysis(file_id, fname, prefer_client=prefer_client)
    # v4.4：PDF 等后端可直接数页的类型，若历史/异常导致页数缺失，轮询时服务端补数——
    # 不依赖本地打印工具（本地工具离线也不影响这类文件确认页数）
    # P1: sqlite3.Row 既没有 .get()（AttributeError），也不支持字段赋值（TypeError）——
    # v13 迁移前历史 files 行 page_count=0 时本接口连环 500。统一改用局部变量。
    page_count = row["page_count"] or 0
    verified = bool(row["page_count_verified"])
    if page_count <= 0 and not verified:
        ext = os.path.splitext(row["original_name"] or "")[1].lower()
        if ext and ext not in (".doc", ".docx"):
            fpath = _resolve_files_path(row["path"])
            if fpath and os.path.exists(fpath):
                new_count = get_file_page_count(fpath, ext.lstrip("."))
                if new_count > 0:
                    conn = get_db()
                    conn.execute(
                        "UPDATE files SET page_count = ?, page_count_verified = 1 WHERE id = ?",
                        (new_count, file_id),
                    )
                    conn.commit()
                    conn.close()
                    page_count = new_count
                    verified = True
    return jsonify({
        "success": True,
        "page_count": page_count,
        "verified": verified,
        "printer_online": printer_online,
    })


@app.route("/api/cancel_page_analysis", methods=["POST"])
@login_required
def cancel_page_analysis():
    """取消页数分析（小程序删除文件 / 关闭页面时调用）。

    对每个 file_id：清空 30s 防抖槽（避免取消后重新轮询被误拦），
    并向曾接收该分析请求的打印机客户端转发 cancel_page_analysis 事件，
    让本地工具中止正在进行的下载/转换。未记录目标时广播兜底。"""
    data = _json_dict()
    file_ids = (data or {}).get("file_ids", [])
    if not isinstance(file_ids, list) or not file_ids:
        return jsonify({"success": False, "message": "缺少 file_ids"}), 400
    # S1-08：每用户限速（取消分析是"删除文件/关页面"时的动作，30/分 足够）
    if not _rate_limit_ok(f"cancel_analysis:{g.openid}", 30, 60):
        return jsonify({"success": False, "message": "操作过于频繁，请稍后再试"}), 429
    # S0-04：上限 50 条、每条 ≤64 字符。原实现 1 MB JSON 塞 5 万个 id ⇒ 5 万次 emit
    # 全在锁内同步完成（CPU/带宽放大面）。
    if len(file_ids) > 50:
        return jsonify({"success": False, "message": "file_ids 数量过多（最多 50 个）"}), 400
    clean_ids = []
    for fid in file_ids:
        if isinstance(fid, str) and 0 < len(fid) <= 64:
            clean_ids.append(fid)
        else:
            return jsonify({"success": False, "message": "file_ids 元素不合法"}), 400

    # 锁内**只收集目标**：emit 是网络动作，放在锁里会让一个慢客户端阻塞所有页数分析。
    targets = []
    with _last_page_analysis_push_lock:
        for fid in clean_ids:
            _last_page_analysis_push.pop(fid, None)      # 清防抖槽，允许立即重新发起
            _entry = _analysis_push_sid.pop(fid, None)
            # 兼容两种形态：🔴-D3 起存的是 {"sid","client_id"}，旧形态是纯 sid 字符串
            sid = _entry.get("sid") if isinstance(_entry, dict) else _entry
            targets.append((fid, sid))
    for fid, sid in targets:
        try:
            if sid:
                socketio.emit("cancel_page_analysis", {"file_id": fid}, to=sid)
            else:
                # 广播兜底保留（覆盖面优先），但已被上面的 50 条上限限频
                socketio.emit("cancel_page_analysis", {"file_id": fid})
            print(f"  [PAGE] 已请求取消页数分析: {fid[:8]}... → sid={sid}")
        except Exception as e:
            print(f"  [WARN] 取消分析转发失败: {e}")
    return jsonify({"success": True})


# ===== S0-03：微信登录闸门（真实 IP 限速 + 有界并发 + 短超时 + 连接池）=====
# 未认证入口是"一口气打瘫全站"的首选目标：并发 /api/login 会让每个线程阻塞在微信 HTTP
# 调用上，16 个请求即占满 threads=16。三处收敛：
#   ① 每真实 IP 10 次/分（ProxyFix 之后桶才按真实 IP 分，否则全站共用一个 127.0.0.1 桶）；
#   ② 全局 BoundedSemaphore(4) **非阻塞**获取 —— 拿不到立即 503（快速失败，不排队占线程）；
#   ③ 微信调用 timeout=3 + requests.Session 连接池（原 urllib 每次新建连接、超时 10 秒）。
_WX_LOGIN_SEM = threading.BoundedSemaphore(4)
_wx_http = requests.Session()
_wx_http.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=4, pool_maxsize=8, max_retries=0))


@app.route("/api/retention_config", methods=["GET"])
def retention_config():
    """保留期口径（S1-03）：移动端提交页的选择器上限来自这里，不在客户端硬编码。"""
    data = dict(get_retention_limits())
    data["success"] = True
    return jsonify(data)


@app.route("/api/file/<file_id>", methods=["DELETE"])
@login_required
def delete_my_file(file_id):
    """删除我的文件（S1-03 / D-7）。

    规则：
      · **只有属主**能删（openid 必须一致）；
      · 被**活跃订单**引用时拒绝（409）—— 不能删掉正在排队/打印的文件，
        否则设备下载 404、顾客的单永久卡住；
      · 删除物理文件 + 清 files 行 + 从 MD5 索引移除（清引用，避免"删了还能被复用"）。
    """
    if not isinstance(file_id, str) or not (0 < len(file_id) <= 64):
        _audit_file_access("delete", "not_found", file_id=file_id, openid=g.openid, actor="user")
        return jsonify({"success": False, "message": "文件不存在"}), 404
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, path, original_name, openid FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        if not row:
            _audit_file_access("delete", "not_found", file_id=file_id, openid=g.openid,
                               actor="user")
            return jsonify({"success": False, "message": "文件不存在"}), 404
        if (row["openid"] or "") != g.openid:
            _audit_file_access("delete", "denied", file_id=file_id, openid=g.openid, actor="user")
            return jsonify({"success": False, "message": "无权删除该文件"}), 403
        active_statuses = ('queued', 'printing', 'accepted', 'reserved',
                           'scheduled', 'waiting', 'downloading')
        placeholders = ",".join("?" for _ in active_statuses)
        busy = conn.execute(
            f"SELECT 1 FROM order_files WHERE file_id = ? AND status IN ({placeholders}) LIMIT 1",
            (file_id, *active_statuses)).fetchone()
        if busy:
            _audit_file_access("delete", "conflict", file_id=file_id, openid=g.openid,
                               actor="user")
            return jsonify({"success": False,
                            "message": "该文件正在被订单使用（排队/打印中），暂不能删除"}), 409
        path = row["path"] or ""
        # S2-05（DS-14）：物理文件若还被**别的 files 行**引用（历史 MD5 去重的存量数据），
        # 只删自己这一行、不删盘 —— 否则另一方当场悬空（下载 404、订单永久卡住）。
        shared = _path_shared_by_others(conn, path, file_id)
        # 先删盘（失败只记日志：记录仍按"已删除"处理，避免留下永远删不掉的幽灵行）
        if path and not shared and os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                print(f"  [DEL] 删除文件失败 {_rel_display(path)}: {type(e).__name__}")
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        conn.commit()
        if shared:
            print(f"  [DEL] 物理文件仍被其它记录引用，保留: {_rel_display(path)}")
    finally:
        conn.close()

    # 从 MD5 索引里摘掉（同一物理文件可能被多行引用：只摘"指向该路径"的条目；
    # 共享时保留 —— 另一方还要靠它命中页数缓存，等最后一个引用消失时会自己过期）
    try:
        if path and not shared:
            rel = os.path.relpath(path, UPLOAD_DIR)
            index = load_md5_index()
            dead = [k for k, v in index.items()
                    if (isinstance(v, str) and v == rel)
                    or (isinstance(v, dict) and v.get("path") == rel)]
            for k in dead:
                index.pop(k, None)
            if dead:
                save_md5_index(index)
    except Exception as e:  # noqa: BLE001
        print(f"  [DEL] 清理 MD5 索引失败: {type(e).__name__}")
    # S3-04：删除成功（只记 file_id + 扩展名，不记原文件名/路径）
    _audit_file_access("delete", "ok", file_id=file_id, openid=g.openid, actor="user",
                       ext=_audit_ext(row["original_name"]))
    print(f"  [DEL] 用户 {g.openid[:8]}... 删除了文件 {file_id[:8]}...")
    return jsonify({"success": True, "file_id": file_id, "message": "已删除"})


def _my_file_item(row, display_name: str, order_item: dict, busy: bool,
                  global_hours: int) -> dict:
    """一行 files 记录 + 上下文 → 列表项（`/api/my_files` 与 `/api/file_detail` 共用）。

    两个接口必须同形状：订单卡片里的「文件列表」点开的详情弹窗要跟「我的文件」
    长得一模一样（用户 2026-11-13 原话「行为与文件管理界面一致，可复用代码逻辑」）。
    """
    own_hours = int(row["retention_hours"] or 0)
    immediate = own_hours == IMMEDIATE_DELETE_RETENTION_HOURS
    eff_hours = own_hours if own_hours > 0 else global_hours
    auto_delete_at = ""
    if not immediate and eff_hours > 0 and row["created_at"]:
        try:
            auto_delete_at = (datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
                              + timedelta(hours=eff_hours)).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            auto_delete_at = ""
    if immediate:
        retention_label = "打印完成后立即删除"
    elif eff_hours > 0:
        retention_label = (
            f"保留 {eff_hours // 24} 天"
            if eff_hours >= 24 and eff_hours % 24 == 0
            else f"保留 {eff_hours} 小时"
        )
    else:
        retention_label = "不自动删除"
    name_or_path = row["original_name"] or row["path"] or ""
    has_preview = _has_preview_for(name_or_path)
    return {
        "file_id": row["id"],
        # 展示名：老客户端上传留下的微信临时名（tmp_<hex>.pdf）用
        # order_files.file_name（该文件最近一次下单用的名字）兜底；没有下单记录时照实回。
        "original_name": display_name,
        # 原始落库值（诊断用：想区分「这行到底存了什么」时看它）
        "stored_original_name": row["original_name"],
        "size": int(row["size"] or 0),
        "page_count": int(row["page_count"] or 0),
        "created_at": row["created_at"],
        "retention_hours": -1 if immediate else eff_hours,
        "retention_mode": "immediate" if immediate else "server",
        "retention_label": retention_label,
        "auto_delete_at": auto_delete_at,
        "in_active_order": bool(busy),
        # 2026-11：详情页需要「所属订单」与「预览图」两件事
        "order_id": int((order_item or {}).get("order_id") or 0),
        "order_number": (order_item or {}).get("order_number", "") or "",
        "has_preview": has_preview,
        # 2026-11-13：多页预览的页数（0=前端要再问 /api/file_preview/<id>/info）
        "preview_pages": _preview_pages_hint(row["id"], name_or_path,
                                             int(row["page_count"] or 0)),
        # 前端（小程序 <image>/APP <img>）不能带 Authorization 头 ⇒ 预览鉴权支持 ?token=，
        # 拼接方式是 `<base>/api/file_preview/<id>?token=<登录token>`，前端自己拼。
        "preview_url": (f"/api/file_preview/{row['id']}" if has_preview else ""),
    }


def _file_detail_payload(conn, row, global_hours: int) -> dict:
    """单个文件的详情项（补齐「最近一次下单用的名字/订单」「是否被活跃订单占用」）。"""
    fid = row["id"]
    active_statuses = ('queued', 'printing', 'accepted', 'reserved',
                       'scheduled', 'waiting', 'downloading')
    _sp = ",".join("?" for _ in active_statuses)
    busy = bool(conn.execute(
        f"SELECT 1 FROM order_files WHERE file_id = ? AND status IN ({_sp}) LIMIT 1",
        (fid, *active_statuses)).fetchone())
    if not busy:
        busy = bool(conn.execute(
            f"SELECT 1 FROM orders WHERE file_id = ? AND status IN ({_sp}) LIMIT 1",
            (fid, *active_statuses)).fetchone())
    name_row = conn.execute(
        "SELECT file_name FROM order_files WHERE file_id = ? AND file_name != '' "
        "ORDER BY id DESC LIMIT 1", (fid,)).fetchone()
    order_row = conn.execute(
        "SELECT of1.file_id AS fid, o.id AS oid, o.order_number AS onum "
        "FROM order_files of1 LEFT JOIN orders o ON o.id = of1.order_id "
        "WHERE of1.file_id = ? ORDER BY of1.id DESC LIMIT 1", (fid,)).fetchone()
    order_item = {}
    if order_row is not None:
        order_item = {"order_id": int(order_row["oid"] or 0),
                      "order_number": (order_row["onum"] or "").strip()}
    display = ((name_row["file_name"] or "").strip() if name_row is not None else "") \
        or row["original_name"]
    return _my_file_item(row, display, order_item, busy, global_hours)


@app.route("/api/file_detail/<file_id>", methods=["GET"])
@login_required
def get_file_detail(file_id):
    """单个文件的详情（与 `/api/my_files` 列表项**同一形状**）。

    用途：订单卡片里「文件列表」点一行 → 独立详情弹窗（用户 2026-11-13 需求 4：
    「行为与文件管理界面一致，可复用代码逻辑」）。前端拿到的字段与「我的文件」
    完全一样，所以小程序 / APP 都复用同一套详情渲染。
    """
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT id, original_name, size, created_at, page_count, retention_hours,
                      path, openid FROM files WHERE id = ?""", (file_id,)).fetchone()
        if row is None:
            return jsonify({"success": False, "message": "文件不存在或已清理"}), 404
        if (row["openid"] or "") != g.openid and not is_admin(g.openid):
            return jsonify({"success": False, "message": "无权查看该文件"}), 403
        cfg = load_retention_config()
        global_hours = int(cfg.get("days", 0) or 0) * 24 + int(cfg.get("hours", 0) or 0)
        item = _file_detail_payload(conn, row, global_hours)
    finally:
        conn.close()
    return jsonify({"success": True, "file": item})


@app.route("/api/my_files", methods=["GET"])
@login_required
def list_my_files():
    """我的文件（S2-08 / D-7）：供「删除我的文件」入口使用。

    · 只回**自己的**行（openid 一致），已过期/已清理（path=''）的不回；
    · `in_active_order`：仍被排队/打印中的订单引用 ⇒ 前端把「删除」置灰
      （服务端 DELETE 也会再判一次，返回 409）；
    · `retention_mode` / `retention_label` / `auto_delete_at`：把保留模式变成
      顾客看得见的事实（服务器管理时按当前全局设置；立即删除时不给到期时间）。
    """
    if not _rate_limit_ok(f"my_files:{g.openid}", 60, 60):
        return jsonify({"success": False, "message": "请求过于频繁，请稍后再试"}), 429
    page = max(1, request.args.get("page", 1, type=int))
    per_page = max(1, min(50, request.args.get("per_page", 20, type=int)))
    offset = (page - 1) * per_page

    cfg = load_retention_config()
    global_hours = int(cfg.get("days", 0) or 0) * 24 + int(cfg.get("hours", 0) or 0)

    conn = get_db()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM files WHERE openid = ? AND path != ''", (g.openid,)
        ).fetchone()[0]
        rows = conn.execute(
            """SELECT id, original_name, size, created_at, page_count, retention_hours, path
               FROM files WHERE openid = ? AND path != ''
               ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?""",
            (g.openid, per_page, offset),
        ).fetchall()
        ids = [r["id"] for r in rows]
        busy_ids = set()
        if ids:
            _ph = ",".join("?" for _ in ids)
            active_statuses = ('queued', 'printing', 'accepted', 'reserved',
                               'scheduled', 'waiting', 'downloading')
            _sp = ",".join("?" for _ in active_statuses)
            for r in conn.execute(
                f"SELECT DISTINCT file_id FROM order_files WHERE file_id IN ({_ph}) "
                f"AND status IN ({_sp})", (*ids, *active_statuses)).fetchall():
                if r["file_id"]:
                    busy_ids.add(r["file_id"])
            # 旧数据（没有 order_files）退化按父单引用判断
            for r in conn.execute(
                f"SELECT DISTINCT file_id FROM orders WHERE file_id IN ({_ph}) "
                f"AND status IN ({_sp})", (*ids, *active_statuses)).fetchall():
                if r["file_id"]:
                    busy_ids.add(r["file_id"])
        # 展示名（2026-10-25 现场）：老客户端上传时 multipart 文件名是微信临时名
        # （tmp_<hex>.pdf），不该拿它给顾客看。真正有意义的名字在
        # order_files.file_name（提交订单时客户端带上来的），按「该文件最近一次下单用过的
        # 名字」兜底；字段名仍叫 original_name，前端（小程序/APP）不用改。
        display_names = {}
        # 2026-11「我的文件」详情页需求：每个文件要能显示「所属订单」。
        # 取该文件**最近一次**被下单时用的订单（order_files.id 倒序第一条），
        # 与 display_names 同一套口径（都是「最近一次下单」），前端不需要再查一次。
        order_info = {}
        if ids:
            _ph2 = ",".join("?" for _ in ids)
            for r in conn.execute(
                f"SELECT of1.file_id AS fid, o.id AS oid, o.order_number AS onum "
                f"FROM order_files of1 LEFT JOIN orders o ON o.id = of1.order_id "
                f"WHERE of1.file_id IN ({_ph2}) ORDER BY of1.id DESC", tuple(ids),
            ).fetchall():
                _fid = r["fid"]
                if _fid and _fid not in order_info:
                    order_info[_fid] = {
                        "order_id": int(r["oid"] or 0),
                        "order_number": (r["onum"] or "").strip(),
                    }
            for r in conn.execute(
                f"SELECT file_id, file_name FROM order_files WHERE file_id IN ({_ph2}) "
                f"AND file_name != '' ORDER BY id DESC", tuple(ids),
            ).fetchall():
                _fid = r["file_id"]
                _nm = (r["file_name"] or "").strip()
                if _fid and _nm and _fid not in display_names:
                    display_names[_fid] = _nm
    finally:
        conn.close()

    out = [_my_file_item(r, display_names.get(r["id"]) or r["original_name"],
                         order_info.get(r["id"]) or {}, r["id"] in busy_ids, global_hours)
           for r in rows]
    # S3-04：列表成功记一条（只记本页条数，不逐条记 file_id —— 否则一次列表放大 50 倍）
    _audit_file_access("list", "ok", openid=g.openid, actor="user", count=len(out))
    return jsonify({
        "success": True,
        "files": out,
        "total": total,
        "page": page,
        "per_page": per_page,
        "has_more": (page * per_page) < total,
    })


# ==================== 文件预览图（2026-11「我的文件」详情页） ====================
#
# 需求（用户原话）：「云端保存该文件的 1024×1024 jpg 图片，需按需显示」。
# 落地口径：
#   · **懒生成**：第一次请求才渲染，结果落盘缓存到 `uploads/previews/<file_id>_<mtime>.jpg`，
#     之后直接 send_file（不动原文件、不重复解码、mtime 变了自动换新文件）；
#   · 覆盖面（2026-11-13 追加「缩略图尽量显示一下」后从「图片 + PDF」扩到 Office）：
#       图片类 → PIL 直读；PDF → PyMuPDF，兜底 pdftoppm；
#       Word/Excel/PPT（doc docx xls xlsx ppt pptx）→ LibreOffice 转 PDF 再取第 1 页。
#       txt/csv/md 仍然 `has_preview=False`（纯文本没有"首页"可言，前端显示类型图标）；
#   · **能力探测而非硬依赖**：没装 soffice / PyMuPDF 的部署照样启动，只是对应类型回落 415。
#     不许为了预览图让后端起不来（预览图是锦上添花，不是启动前置条件）；
#   · **串行 + 超时 + 负缓存**：soffice 一次转换 2~5 秒、峰值内存约 200MB，本机只有 2GB
#     ⇒ 全局一把锁（同时只跑一个 soffice）、120 秒硬超时、失败结果 10 分钟内不再重试；
#   · 前端收到 415 **静默退回图标**（不是错误弹窗）；
#   · 鉴权：小程序的 <image src> 与 WebView 的 <img> 都带不了请求头 ⇒ 本端点接受
#     `Authorization: Bearer` **或** `?token=`，校验走同一个 `_load_login_token`
#     （撤销/过期/签名判据与 login_required 不分叉），并且**只放行属主或管理员**
#     （与 `/api/file/<id>` 下载同口径）。
PREVIEW_DIR = os.path.join(UPLOAD_DIR, "previews")
PREVIEW_MAX_PX = 1024                      # 长边像素上限（需求写的就是 1024×1024）
_PREVIEW_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff")
# Office 家族：必须同时落在 UPLOAD_EXT_WHITELIST 里（见本文件"上传校验"一节）
_PREVIEW_OFFICE_EXTS = (".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")
_PREVIEW_OFFICE_TIMEOUT = 120              # 秒；soffice 卡死时不能把 gunicorn 线程一起拖住
_PREVIEW_FAIL_TTL = 600                    # 秒；同一 (file_id, mtime) 渲染失败后 10 分钟内不重试
_PREVIEW_RENDER_LOCK = threading.Lock()    # 全局串行：2GB 内存同时起两个 soffice 会打到 swap
_preview_fail_cache = {}                   # {(file_id, mtime): (过期时间戳, 失败原因)}
_soffice_path = None                       # None=还没探过；""=探过但没有；其它=可执行路径
# 2026-11-13（用户要求）：上传即把缩略图/首页预览**暂存**下来（跟着文件保留期一起删），
# 并且支持多页预览。原来只懒生成第 1 页 ⇒ 用户点开 Word 要等 2 秒、且只能看第一页。
PREVIEW_MAX_PAGES = 12                     # 上传时预生成的页数上限（更多页按需单页生成）
_PREVIEW_LOCK_GUARD = threading.Lock()
_preview_page_locks = {}
_preview_meta_index_cache = {}                   # file_id -> Lock（同一文件别并发跑两个 soffice）


def _has_preview_for(name_or_path: str) -> bool:
    """这个文件名/路径有没有可见预览图（图片类 + PDF + Office 文档）。"""
    ext = os.path.splitext(str(name_or_path or ""))[1].lower()
    return ext in _PREVIEW_IMAGE_EXTS or ext == ".pdf" or ext in _PREVIEW_OFFICE_EXTS


def _openid_for_media() -> str:
    """媒体端点（<image src> 用）的登录判定：请求头优先，其次 ?token=。

    返回空串 = 未登录/凭证失效（过期、撤销、坏签名在这一层**不区分**，
    与 login_required 一致：不外泄失败原因）。
    """
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if not token:
        token = (request.args.get("token") or request.args.get("access_token") or "").strip()
    if not token:
        return ""
    try:
        return _load_login_token(token)
    except Exception:  # noqa: BLE001  任何凭证异常都当未登录
        return ""



def _preview_stem(file_id: str, mtime_token: str) -> str:
    """预览缓存文件的公共前缀：只留 id 的安全字符（防目录穿越），mtime 当版本号。"""
    safe_id = re.sub(r"[^0-9A-Za-z_-]", "", str(file_id))[:64] or "x"
    safe_mt = re.sub(r"[^0-9]", "", str(mtime_token))[:20] or "0"
    return f"{safe_id}_{safe_mt}"


def _preview_cache_path(file_id: str, mtime_token: str, page: int = 1) -> str:
    """第 1 页沿用旧文件名（历史缓存继续命中）；第 N 页加 `_pN` 后缀。"""
    stem = _preview_stem(file_id, mtime_token)
    page = int(page or 1)
    if page <= 1:
        return os.path.join(PREVIEW_DIR, f"{stem}.jpg")
    return os.path.join(PREVIEW_DIR, f"{stem}_p{page}.jpg")


def _preview_pdf_path(file_id: str, mtime_token: str) -> str:
    """Office 转换出来的中间 PDF：多页翻页靠它，避免每翻一页重跑一次 soffice。"""
    return os.path.join(PREVIEW_DIR, f"{_preview_stem(file_id, mtime_token)}.pdf")


def _preview_meta_path(file_id: str, mtime_token: str) -> str:
    """页数元信息（{pages, rendered}）——让「我的文件」列表能直接给出总页数，不触发生成。"""
    return os.path.join(PREVIEW_DIR, f"{_preview_stem(file_id, mtime_token)}.json")


def _read_preview_meta(file_id: str, mtime_token: str) -> dict:
    try:
        with open(_preview_meta_path(file_id, mtime_token), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_preview_meta(file_id: str, mtime_token: str, pages: int, rendered: int) -> None:
    path = _preview_meta_path(file_id, mtime_token)
    try:
        os.makedirs(PREVIEW_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"pages": int(pages), "rendered": int(rendered)}, fh)
        os.replace(tmp, path)
    except OSError as e:
        print(f"  [PREVIEW] 写元信息失败 {file_id}: {e}")


def _preview_lock_for(file_id: str):
    """每个文件一把锁：并发点开同一文件时只跑一次 soffice / PyMuPDF。"""
    with _PREVIEW_LOCK_GUARD:
        if len(_preview_page_locks) > 500:      # 这只是"别重复干活"的短锁，不是业务数据
            _preview_page_locks.clear()
        lock = _preview_page_locks.get(file_id)
        if lock is None:
            lock = threading.Lock()
            _preview_page_locks[file_id] = lock
        return lock


def _preview_meta_index() -> dict:
    """PREVIEW_DIR 里的 {file_id: 真实页数} 快照（目录 mtime 变了才重建）。

    列表一次要给 N 个文件算页数，逐文件 glob 是 O(N × 缓存文件数)；这里整目录扫一次
    并缓存，预热/删除会改目录 mtime ⇒ 自动失效。
    """
    global _preview_meta_index_cache
    try:
        stamp = os.stat(PREVIEW_DIR).st_mtime_ns
    except OSError:
        return {}
    cached = _preview_meta_index_cache
    if cached and cached.get("dir") == PREVIEW_DIR and cached.get("stamp") == stamp:
        return cached.get("map") or {}
    index = {}
    try:
        names = os.listdir(PREVIEW_DIR)
    except OSError:
        return {}
    for name in names:
        if not name.endswith(".json"):
            continue
        # 文件名是 <safe_id>_<mtime>.json；safe_id 本身可能带下划线，从右边切
        safe_id = name.rsplit("_", 1)[0]
        if not safe_id:
            continue
        try:
            with open(os.path.join(PREVIEW_DIR, name), "r", encoding="utf-8") as fh:
                pages = int((json.load(fh) or {}).get("pages") or 0)
        except Exception:  # noqa: BLE001  坏 meta 当没有
            continue
        if pages > 0:
            index[safe_id] = max(index.get(safe_id, 0), pages)
    _preview_meta_index_cache = {"dir": PREVIEW_DIR, "stamp": stamp, "map": index}
    return index


def _preview_pages_hint(file_id: str, name_or_path: str, page_count_hint: int = 0) -> int:
    """列表里能直接给前端的页数（**不触发生成**）。

    优先级：图片恒 1 页 → 预热写下的 meta.pages（真实渲染页数，Office 转成 PDF 后
    可能比 files.page_count 多，样例 docx page_count=2 实际 3 页）→ files.page_count。
    """
    if not _has_preview_for(name_or_path):
        return 0
    ext = os.path.splitext(str(name_or_path or ""))[1].lower()
    if ext in _PREVIEW_IMAGE_EXTS:
        return 1
    try:
        safe_id = re.sub(r"[^0-9A-Za-z_-]", "", str(file_id))[:64]
        if safe_id:
            pages = int(_preview_meta_index().get(safe_id) or 0)
            if pages > 0:
                return pages
    except Exception:  # noqa: BLE001  读不到 meta 就退回 page_count
        pass
    hint = int(page_count_hint or 0)
    return hint if hint > 0 else 0


def _flatten_to_rgb(im):
    """任意模式（RGBA/LA/P/CMYK…）压成白底 RGB —— JPEG 存不了透明通道。"""
    from PIL import Image, ImageOps            # 函数内导入：PIL 缺失不影响后端启动
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return im.convert("RGB")


def _render_image_preview(src_path: str) -> bytes:
    """图片 → 1024 以内 JPEG bytes。只缩不放（小图保持原样，避免糊）。"""
    from PIL import Image
    with Image.open(src_path) as im:
        im = _flatten_to_rgb(im)
        im.thumbnail((PREVIEW_MAX_PX, PREVIEW_MAX_PX), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82, optimize=True)
        return buf.getvalue()


def _render_pdf_page_bytes(pdf_path: str, page_index: int) -> bytes:
    """PDF 第 page_index 页（0 基）→ 长边 ≤ PREVIEW_MAX_PX 的 JPEG bytes。

    能力降级：PyMuPDF → pdftoppm → 抛 RuntimeError（上层回落 415）。
    """
    page_index = max(0, int(page_index or 0))
    try:
        import fitz                            # PyMuPDF（可选依赖）
    except ImportError:
        fitz = None
    if fitz is not None:
        with fitz.open(pdf_path) as doc:
            if doc.page_count <= 0:
                raise RuntimeError("PDF 没有页面")
            page = doc.load_page(min(page_index, doc.page_count - 1))
            w = float(page.rect.width) or 1.0
            h = float(page.rect.height) or 1.0
            zoom = min(PREVIEW_MAX_PX / w, PREVIEW_MAX_PX / h, 4.0)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            return pix.tobytes("jpeg")
    exe = shutil.which("pdftoppm")
    if not exe:
        raise RuntimeError("服务端没有 PDF 光栅化能力（缺 PyMuPDF 与 pdftoppm）")
    tmpdir = tempfile.mkdtemp(prefix="hn_prev_")
    try:
        n = page_index + 1
        subprocess.run(
            [exe, "-f", str(n), "-l", str(n), "-scale-to", str(PREVIEW_MAX_PX),
             "-jpeg", "-singlefile", pdf_path, os.path.join(tmpdir, "page")],
            timeout=60, check=False, capture_output=True)
        hits = sorted(glob.glob(os.path.join(tmpdir, "page*.jpg")))
        if not hits:
            raise RuntimeError("pdftoppm 没有输出页面")
        return _render_image_preview(hits[0])   # 再统一压到 1024 以内
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _pdf_page_total(pdf_path: str) -> int:
    """PDF 总页数（读不出来就当 1 页 —— 预览是锦上添花，不能让翻页把接口拖红）。"""
    try:
        import fitz
    except ImportError:
        return 1
    try:
        with fitz.open(pdf_path) as doc:
            return max(1, int(doc.page_count))
    except Exception:  # noqa: BLE001
        return 1


def _render_pdf_preview(src_path: str) -> bytes:
    """PDF 第 1 页 → 1024 以内 JPEG bytes（兼容旧调用）。"""
    return _render_pdf_page_bytes(src_path, 0)


def _soffice_bin() -> str:
    """LibreOffice 可执行文件的路径（懒探测一次；没有就返回空串，绝不抛）。

    ⚠ systemd 单元里 `Environment=PATH=/home/printer-backend/venv/bin` 是**唯一**的 PATH
    （加固时故意清掉了系统路径），`shutil.which("soffice")` 在 gunicorn 里恒返回 None ——
    2026-11-13 线上实测就踩了这个：apt 装的 /usr/bin/soffice 明明在，预览却报
    「服务端没有 Office 渲染能力（缺 LibreOffice）」。所以 which 之后必须再查固定路径。
    """
    global _soffice_path
    if _soffice_path is None:
        hit = ""
        for name in ("soffice", "libreoffice"):
            try:
                found = shutil.which(name)
            except Exception:  # noqa: BLE001
                found = None
            if found:
                hit = found
                break
        if not hit:
            # 常见安装位置（apt / snap / 官方 tar 包）
            for cand in ("/usr/bin/soffice", "/usr/bin/libreoffice",
                         "/usr/lib/libreoffice/program/soffice",
                         "/opt/libreoffice/program/soffice",
                         "/snap/bin/libreoffice"):
                try:
                    if os.path.isfile(cand) and os.access(cand, os.X_OK):
                        hit = cand
                        break
                except OSError:
                    continue
        _soffice_path = hit
    return _soffice_path


def _preview_fail_hit(key) -> bool:
    """这个 (file_id, mtime) 10 分钟内是不是已经失败过（别让用户每点一次都重转一遍）。"""
    item = _preview_fail_cache.get(key)
    if not item:
        return False
    if item[0] <= time.time():
        _preview_fail_cache.pop(key, None)
        return False
    return True


def _preview_fail_note(key, reason) -> None:
    # 只留最近 500 条：这是"别重复重试"的短路缓存，不是业务数据
    if len(_preview_fail_cache) > 500:
        _preview_fail_cache.clear()
    _preview_fail_cache[key] = (time.time() + _PREVIEW_FAIL_TTL, str(reason)[:200])


# 表格导出：整张表压进一页（缩略图上能看出表格形状，而不是只看到被切掉的左上角）
_CALC_ONE_PAGE_FILTER = (
    'pdf:calc_pdf_Export:{"SinglePageSheets":{"type":"boolean","value":"true"}}'
)


def _soffice_to_pdf(exe: str, src_path: str, outdir: str, filter_spec: str) -> str:
    """跑一次 soffice 转换，成功返回输出 PDF 路径，失败返回空串（绝不抛）。"""
    profile = os.path.join(outdir, "profile")
    os.makedirs(profile, exist_ok=True)
    # 每次调用一个独立 profile：共享 profile 被上一个进程锁住时 soffice 会直接退出
    # （表现成"没有输出文件"的假失败）；HOME 指到可写 tmpdir，避免 root 的 HOME 不可写。
    env = dict(os.environ)
    env["HOME"] = outdir
    # ⚠ /usr/bin/soffice 是个 shell 包装脚本，内部要 dirname/basename/sed/grep/uname 才能算出
    # oosplash 的位置；systemd 单元里 PATH 只有 venv/bin ⇒ 它们全都找不到，soffice 以 127 退出
    # （2026-11-13 线上实测：报「LibreOffice 没有输出 PDF」），所以这里必须补齐系统路径。
    path_parts = [p for p in (os.path.dirname(exe) or "",) + tuple(
        os.environ.get("PATH", "").split(os.pathsep)) if p]
    for extra in ("/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if extra not in path_parts:
            path_parts.append(extra)
    env["PATH"] = os.pathsep.join(path_parts)
    if not _PREVIEW_RENDER_LOCK.acquire(timeout=_PREVIEW_OFFICE_TIMEOUT):
        raise RuntimeError("Office 转换排队超时（有别的文件正在转）")
    try:
        subprocess.run(
            [exe, "--headless", "--norestore", "--invisible", "--nologo",
             "--nodefault", "--nolockcheck", "--nofirststartwizard",
             "-env:UserInstallation=file://" + profile,
             "--convert-to", filter_spec, "--outdir", outdir, src_path],
            timeout=_PREVIEW_OFFICE_TIMEOUT, check=False,
            capture_output=True, env=env)
    finally:
        _PREVIEW_RENDER_LOCK.release()
    pdfs = sorted(glob.glob(os.path.join(outdir, "*.pdf")))
    return pdfs[0] if pdfs else ""


def _ensure_office_pdf(src_path: str, cache_path: str) -> str:
    """Office → PDF（结果落 cache_path 并复用）。失败抛 RuntimeError（上层回落 415）。

    多页预览必须复用同一份中间 PDF：soffice 一次转换 2~5 秒，每翻一页重跑一次
    等于把 2GB 内存的机器按在地上摩擦。
    """
    if cache_path and os.path.isfile(cache_path) and os.path.getsize(cache_path) > 0:
        return cache_path
    exe = _soffice_bin()
    if not exe:
        raise RuntimeError("服务端没有 Office 渲染能力（缺 LibreOffice）")
    ext = os.path.splitext(src_path)[1].lower()
    # 表格先试"整表一页"，老 LibreOffice 不认这个过滤器选项 ⇒ 再退回普通导出
    filters = ["pdf"]
    if ext in (".xls", ".xlsx"):
        filters = [_CALC_ONE_PAGE_FILTER, "pdf"]
    tmpdir = tempfile.mkdtemp(prefix="hn_office_")
    try:
        pdf_path = ""
        for i, spec in enumerate(filters):
            outdir = os.path.join(tmpdir, "out%d" % i)
            os.makedirs(outdir, exist_ok=True)
            pdf_path = _soffice_to_pdf(exe, src_path, outdir, spec)
            if pdf_path:
                break
        if not pdf_path:
            raise RuntimeError("LibreOffice 没有输出 PDF（文件损坏或格式不支持）")
        if not cache_path:
            raise RuntimeError("缺少中间 PDF 缓存路径")
        os.makedirs(os.path.dirname(cache_path) or PREVIEW_DIR, exist_ok=True)
        try:
            shutil.copyfile(pdf_path, cache_path)
        except OSError as e:
            raise RuntimeError("无法缓存 Office 转换结果: %s" % e)
        return cache_path
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _render_office_preview(src_path: str) -> bytes:
    """Office 文档第 1 页 → 1024 以内 JPEG bytes（兼容旧调用；多页走 _render_preview_page）。"""
    if not _soffice_bin():
        raise RuntimeError("服务端没有 Office 渲染能力（缺 LibreOffice）")
    tmpdir = tempfile.mkdtemp(prefix="hn_office_prev_")
    try:
        pdf_path = _ensure_office_pdf(src_path, os.path.join(tmpdir, "cache.pdf"))
        return _render_pdf_page_bytes(pdf_path, 0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _preview_pdf_for(src_path: str, ext: str, file_id: str, mtime_token: str) -> str:
    """把可预览文件变成一份（可缓存的）PDF：PDF 原样用，Office 走 soffice 并缓存结果。"""
    if ext == ".pdf":
        return src_path
    return _ensure_office_pdf(src_path, _preview_pdf_path(file_id, mtime_token))


def _render_preview_page(src_path: str, display_name: str, file_id: str,
                         mtime_token: str, page_index: int) -> bytes:
    """任意可预览文件第 page_index 页（0 基）→ JPEG bytes。"""
    ext = os.path.splitext(display_name or src_path)[1].lower()
    if ext in _PREVIEW_IMAGE_EXTS:
        if page_index > 0:
            raise RuntimeError("图片只有一页")
        return _render_image_preview(src_path)
    pdf_path = _preview_pdf_for(src_path, ext, file_id, mtime_token)
    return _render_pdf_page_bytes(pdf_path, page_index)


def _preview_total_pages(src_path: str, display_name: str, file_id: str,
                         mtime_token: str) -> int:
    """总页数：图片 1 页；PDF/Office 取光栅化源（Office 会顺带把中间 PDF 缓存下来）。"""
    ext = os.path.splitext(display_name or src_path)[1].lower()
    if ext in _PREVIEW_IMAGE_EXTS:
        return 1
    return _pdf_page_total(_preview_pdf_for(src_path, ext, file_id, mtime_token))


def _prewarm_previews(file_id: str) -> bool:
    """生成前 PREVIEW_MAX_PAGES 页 + 页数元信息（同一文件只跑一次）。

    2026-11-13 用户要求：「暂存的云端文件同时需要暂存一份略缩图，略缩图也服从
    云端的自动删除处理」。上传成功后后台线程调这里；用户点开详情就是秒开。
    失败只打日志（`[PREVIEW]`），绝不把上传/列表搞红。
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, original_name, path FROM files WHERE id = ?", (file_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None or not row["path"]:
        return False
    src_path = _resolve_files_path(row["path"])
    if not src_path or not os.path.isfile(src_path):
        return False
    display = row["original_name"] or src_path
    if not _has_preview_for(display):
        return False
    try:
        mtime_token = str(int(os.path.getmtime(src_path)))
    except OSError:
        mtime_token = "0"
    if _read_preview_meta(file_id, mtime_token).get("pages"):
        return True                      # 已预热过（同 mtime）
    lock = _preview_lock_for(file_id)
    if not lock.acquire(blocking=False):
        # 别的线程正在生成：等它（最多 30 秒），别重复跑 soffice
        if not lock.acquire(timeout=30):
            return False
        lock.release()
        return bool(_read_preview_meta(file_id, mtime_token).get("pages"))
    try:
        if _read_preview_meta(file_id, mtime_token).get("pages"):
            return True
        total = _preview_total_pages(src_path, display, file_id, mtime_token)
        limit = max(1, min(total, PREVIEW_MAX_PAGES))
        os.makedirs(PREVIEW_DIR, exist_ok=True)
        rendered = 0
        for i in range(limit):
            cache_path = _preview_cache_path(file_id, mtime_token, i + 1)
            if os.path.isfile(cache_path):
                rendered += 1
                continue
            try:
                data = _render_preview_page(src_path, display, file_id, mtime_token, i)
            except Exception as e:      # noqa: BLE001  单页失败不影响其它页
                print(f"  [PREVIEW] 预热第 {i + 1} 页失败 file_id={file_id}: {e}")
                break
            try:
                with open(cache_path, "wb") as fh:
                    fh.write(data)
                rendered += 1
            except OSError as e:
                print(f"  [PREVIEW] 写缓存失败 {cache_path}: {e}")
                break
        _write_preview_meta(file_id, mtime_token, total, rendered)
        print(f"  [PREVIEW] 已预热 {_redact_filename(display)} 共 {total} 页"
              f"（已生成 {rendered} 页）")
        return True
    finally:
        lock.release()


def _spawn_preview_prewarm(file_id: str, display_name: str) -> None:
    """上传成功后起一个后台线程预热预览（不阻塞上传响应）。"""
    if not _has_preview_for(display_name or ""):
        return

    def _run():
        try:
            _prewarm_previews(file_id)
        except Exception as e:          # noqa: BLE001  后台线程绝不能把异常漏进主流程
            print(f"  [PREVIEW] 预热异常 file_id={file_id}: {e}")

    try:
        threading.Thread(target=_run, name="preview-prewarm", daemon=True).start()
    except Exception as e:              # noqa: BLE001
        print(f"  [PREVIEW] 无法启动预热线程 file_id={file_id}: {e}")


def _drop_previews(file_id: str) -> None:
    """删掉某个文件的全部预览缓存（文件被删除/过期清理时调用）。失败只打日志。"""
    try:
        safe_id = re.sub(r"[^0-9A-Za-z_-]", "", str(file_id))[:64]
        if not safe_id or not os.path.isdir(PREVIEW_DIR):
            return
        # 2026-11-13：缓存里不止 .jpg 了（多页 JPEG / Office 中间 PDF / 页数元信息）
        for p in glob.glob(os.path.join(PREVIEW_DIR, f"{safe_id}_*")):
            try:
                os.remove(p)
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001  清理失败不影响主流程
        print(f"  [PREVIEW] 清理预览缓存失败 {file_id}: {e}")


def _preview_request_context(file_id, openid):
    """预览类端点的公共前置：鉴权后的属主判定、物理文件、类型。

    返回 dict（`row/src_path/display/mtime_token`）；不满足时返回 Flask 响应元组
    （404/403/415），调用方直接 `return` 出去。
    ⚠ 成功分支**绝不能**返回 tuple：调用方用 `isinstance(ctx, tuple)` 判断"这是错误响应"，
      两边都是 tuple 时视图会把 ctx 当响应元组交给 Flask ⇒ 500
      `TypeError: The view function did not return a valid response tuple.`
      （2026-11-13 线上真踩：所有预览请求一律 500）
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, original_name, path, openid FROM files WHERE id = ?", (file_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        _audit_file_access("preview", "not_found", file_id=file_id, openid=openid, actor="user")
        return jsonify({"success": False, "message": "文件不存在或已清理"}), 404
    owner = str(row["openid"] or "")
    if owner != openid and not is_admin(openid):
        _audit_file_access("preview", "denied", file_id=file_id, openid=openid, actor="user")
        return jsonify({"success": False, "message": "无权查看该文件"}), 403
    src_path = _resolve_files_path(row["path"])
    if not src_path or not os.path.isfile(src_path):
        _audit_file_access("preview", "not_found", file_id=file_id, openid=openid, actor="user")
        return jsonify({"success": False, "message": "文件不存在或已清理"}), 404
    display = row["original_name"] or src_path
    if not _has_preview_for(display):
        _audit_file_access("preview", "ok", file_id=file_id, openid=openid, actor="user",
                           ext=os.path.splitext(display)[1].lower().lstrip("."))
        return jsonify({"success": False, "message": "该类型没有预览图"}), 415
    try:
        mtime_token = str(int(os.path.getmtime(src_path)))
    except OSError:
        mtime_token = "0"
    return {"row": row, "src_path": src_path, "display": display,
            "mtime_token": mtime_token}


def _send_preview_file(path: str, page_no: int, pages: int):
    """发图 + 页码/总页数响应头（多页 swiper 用得到）。"""
    resp = send_file(path, mimetype="image/jpeg", max_age=86400, conditional=True)
    resp.headers["X-Preview-Page"] = str(int(page_no))
    resp.headers["X-Preview-Pages"] = str(int(pages))
    return resp


@app.route("/api/file_preview/<file_id>", methods=["GET"])
def file_preview(file_id):
    """文件预览图（≤1024×1024 JPEG）。见本节顶部的口径说明。

    参数：`page`（1 基，默认 1）—— 多页文档翻页用（2026-11-13 用户要求支持多页）。
    返回：
      · 200 + image/jpeg —— 命中缓存或刚生成；响应头 X-Preview-Page / X-Preview-Pages；
      · 404 —— 文件不存在/已清理；
      · 403 —— 不是你的文件（管理员可看）；
      · 415 —— 该类型没有预览图 / 服务端暂时渲染不了（前端静默退回类型图标）。
    """
    openid = _openid_for_media()
    if not openid:
        return jsonify({"success": False, "message": "未登录，请先授权"}), 401
    ctx = _preview_request_context(file_id, openid)
    if isinstance(ctx, tuple):
        return ctx
    src_path = ctx["src_path"]
    display = ctx["display"]
    mtime_token = ctx["mtime_token"]
    page_no = request.args.get("page", 1, type=int) or 1
    page_no = max(1, min(999, int(page_no)))
    meta = _read_preview_meta(file_id, mtime_token)
    cache_path = _preview_cache_path(file_id, mtime_token, page_no)
    if os.path.isfile(cache_path):
        _audit_file_access("preview", "ok", file_id=file_id, openid=openid, actor="user")
        return _send_preview_file(cache_path, page_no, int(meta.get("pages") or page_no))
    ext = os.path.splitext(display)[1].lower()
    fail_key = (str(file_id), mtime_token, int(page_no))
    if _preview_fail_hit(fail_key):
        # 刚失败过（典型：损坏的 docx / 服务器内存吃紧）：这次不再重试，直接回落图标。
        _audit_file_access("preview", "ok", file_id=file_id, openid=openid, actor="user",
                           ext=ext.lstrip("."))
        return jsonify({"success": False, "message": "该文件暂时没有预览图"}), 415
    try:
        data = _render_preview_page(src_path, display, file_id, mtime_token, page_no - 1)
    except Exception as e:  # noqa: BLE001  预览失败绝不 500
        print(f"  [PREVIEW] 生成失败 file_id={file_id} page={page_no} ext={ext}: {e}")
        _preview_fail_note(fail_key, e)
        data = None
    if not data:
        _audit_file_access("preview", "ok", file_id=file_id, openid=openid, actor="user",
                           ext=ext.lstrip("."))
        return jsonify({"success": False, "message": "该文件暂时没有预览图"}), 415
    try:
        os.makedirs(PREVIEW_DIR, exist_ok=True)
        with open(cache_path, "wb") as fh:
            fh.write(data)
    except OSError as e:
        # 缓存写不进去也照样回图（磁盘满/权限问题时不让预览彻底不可用）
        print(f"  [PREVIEW] 写缓存失败 {cache_path}: {e}")
    pages = int(meta.get("pages") or 0)
    if not pages:
        try:
            pages = _preview_total_pages(src_path, display, file_id, mtime_token)
        except Exception:  # noqa: BLE001
            pages = page_no
        _write_preview_meta(file_id, mtime_token, pages, 1)
    _audit_file_access("preview", "ok", file_id=file_id, openid=openid, actor="user")
    resp = send_file(io.BytesIO(data), mimetype="image/jpeg", max_age=86400)
    resp.headers["X-Preview-Page"] = str(int(page_no))
    resp.headers["X-Preview-Pages"] = str(int(pages))
    return resp


@app.route("/api/file_preview/<file_id>/info", methods=["GET"])
def file_preview_info(file_id):
    """预览元信息（总页数）：先把前 N 页预热出来，再回 `{success, pages, rendered}`。

    前端在展开详情时调用它 —— 拿到 pages 就能渲染多页 swiper；预热本身也在这里
    兜底（老文件上传时还没有预热机制）。渲染不了的类型回 415（前端显示类型图标）。
    """
    openid = _openid_for_media()
    if not openid:
        return jsonify({"success": False, "message": "未登录，请先授权"}), 401
    ctx = _preview_request_context(file_id, openid)
    if isinstance(ctx, tuple):
        return ctx
    mtime_token = ctx["mtime_token"]
    try:
        _prewarm_previews(file_id)
    except Exception as e:  # noqa: BLE001  预热失败不 500：前端回落类型图标
        print(f"  [PREVIEW] 预热失败 file_id={file_id}: {e}")
    meta = _read_preview_meta(file_id, mtime_token)
    pages = int(meta.get("pages") or 0)
    if pages <= 0:
        _audit_file_access("preview", "ok", file_id=file_id, openid=openid, actor="user")
        return jsonify({"success": False, "message": "该文件暂时没有预览图"}), 415
    _audit_file_access("preview", "ok", file_id=file_id, openid=openid, actor="user")
    return jsonify({"success": True, "pages": pages,
                    "rendered": int(meta.get("rendered") or 0)})


@app.route("/api/admin/file_audit", methods=["GET"])
@login_required
def admin_file_audit():
    """S3-04（DS-24）：管理员查询「文件访问审计」（谁、读了哪个文件、何时）。

    只读旁路日志，不碰业务库。参数（全部可选）：
      file_id / openid / action / result / since / until / limit(1..500，默认 100) / stats=1
    返回 `{success, entries, count}`；`stats=1` 时多一个 `stats`（季度报告口径）。
    审计日志里**没有**原文件名/绝对路径，只有 file_id + 扩展名（S2-11）。
    """
    role = compute_role(g.openid)
    if role not in ("admin", "super_admin"):
        return jsonify({"success": False, "message": "需要管理员权限"}), 403
    if not _rate_limit_ok(f"file_audit:{g.openid}", 30, 60):
        return jsonify({"success": False, "message": "查询过于频繁，请稍后再试"}), 429

    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(500, limit))  # 上限 500：避免一次请求把几十万行读进内存

    since = (request.args.get("since") or "").strip()[:40]
    until = (request.args.get("until") or "").strip()[:40]
    try:
        entries = file_audit.query(
            file_id=(request.args.get("file_id") or "").strip()[:64],
            openid=(request.args.get("openid") or "").strip()[:128],
            action=(request.args.get("action") or "").strip()[:32],
            result=(request.args.get("result") or "").strip()[:16],
            since=since or None, until=until or None, limit=limit)
    except ValueError:
        # 不把异常文本回给客户端（S2-11）：给出固定的参数说明即可
        return jsonify({"success": False,
                        "message": "时间参数格式不合法（支持 ISO8601 / YYYY-MM-DD[ HH:MM:SS]）"}), 400

    body = {"success": True, "entries": entries, "count": len(entries)}
    if (request.args.get("stats") or "").strip().lower() in ("1", "true", "yes"):
        try:
            body["stats"] = file_audit.stats(since=since or None, until=until or None)
        except ValueError:
            return jsonify({"success": False,
                            "message": "时间参数格式不合法（支持 ISO8601 / YYYY-MM-DD[ HH:MM:SS]）"}), 400
    return jsonify(body)


@app.route("/api/login", methods=["POST"])
def wx_login():
    """微信小程序登录：用 code 换取 openid 并返回 token"""
    ip = request.remote_addr or "unknown"
    if not _rate_limit(f"wxlogin:ip:{ip}", 10, 60):
        resp = jsonify({"success": False, "message": "登录过于频繁，请稍后再试"})
        resp.headers["Retry-After"] = "60"
        return resp, 429

    data = _json_dict()
    if data is None:
        return jsonify({"success": False, "message": "请提供 JSON 数据"}), 400

    code = data.get("code", "")
    if not isinstance(code, str) or not code:
        return jsonify({"success": False, "message": "缺少 code 参数"}), 400
    if len(code) > 256:
        return jsonify({"success": False, "message": "code 参数不合法"}), 400

    if not WECHAT_APPID or not WECHAT_APPSECRET:
        return jsonify({"success": False, "message": "服务器未配置微信 AppID/AppSecret"}), 500

    if not _WX_LOGIN_SEM.acquire(blocking=False):
        resp = jsonify({"success": False, "message": "服务器繁忙，请稍后重试"})
        resp.headers["Retry-After"] = "3"
        return resp, 503
    try:
        # 调用微信 jscode2session 接口
        wx_resp = _wx_http.get(
            "https://api.weixin.qq.com/sns/jscode2session",
            params={
                "appid": WECHAT_APPID,
                "secret": WECHAT_APPSECRET,
                "js_code": code,
                "grant_type": "authorization_code",
            },
            timeout=3,
        )
        wx_data = wx_resp.json()
        if not isinstance(wx_data, dict):
            raise ValueError("微信接口返回格式异常")
    except Exception as e:
        # 不把异常原文回给客户端：URL 里带 AppSecret（日志只记异常类名）
        print(f"[LOGIN] 微信登录接口调用失败: {type(e).__name__}")
        return jsonify({"success": False, "message": "登录服务暂时不可用，请稍后重试"}), 502
    finally:
        _WX_LOGIN_SEM.release()

    if "errcode" in wx_data and wx_data["errcode"] != 0:
        return jsonify(
            {
                "success": False,
                "message": f"微信登录失败: {wx_data.get('errmsg', '未知错误')}",
                "errcode": wx_data["errcode"],
            }
        ), 400

    openid = wx_data["openid"]
    # session_key 不返回给前端 ---- 服务端解密用户数据时使用
    token = _issue_login_token(openid)

    # 首次登录：创建用户记录并分配默认昵称
    conn = get_user_db()
    existing = conn.execute("SELECT openid FROM users WHERE openid = ?", (openid,)).fetchone()
    if not existing:
        # 生成唯一默认昵称: user_ + 8位随机字母数字
        for _ in range(10):
            suffix = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
            nickname = f"user_{suffix}"
            dup = conn.execute("SELECT openid FROM users WHERE nickname = ?", (nickname,)).fetchone()
            if not dup:
                break
        conn.execute(
            "INSERT INTO users (openid, nickname, avatar_path, updated_at) VALUES (?, ?, '', ?)",
            (openid, nickname, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    conn.close()

    print(f"用户登录成功: openid={openid[:8]}...")

    return jsonify(
        {
            "success": True,
            "message": "登录成功",
            "token": token,
            "openid": openid,
        }
    )


@app.route("/api/device_login", methods=["POST"])
def device_login():
    """Android/Web 设备登录：用 device_id 换取设备 token，无需微信。

    惰性注册（方案 A）：首次登录只签发 guest 级设备 token，**不**在 users 表建号，
    避免"打开即建号"产生一堆从未使用的空账号。
    真正使用（兑换授权密钥 /api/license/redeem）时才会在 users 表惰性创建账号。
    """
    data = _json_dict()
    if data is None:
        return jsonify({"success": False, "message": "请提供 JSON 数据"}), 400

    raw_device_id = data.get("device_id")
    if not isinstance(raw_device_id, str):
        return jsonify({"success": False, "message": "device_id 无效（至少 10 位）"}), 400
    device_id = raw_device_id.strip()

    # S0-03：**先校验参数、再进限速 key**（R2/R3/DS-04 + R8 的内存增长面）。
    # 原实现把未截断的 device_id 原串直接当字典 key、且字典永不删 ⇒ 20 个 1 MB 级
    # device_id ≈ 20 MB/小时；"先限速后校验"还让垃圾请求消耗真实配额。
    if not (10 <= len(device_id) <= 64) or not re.match(r"^[A-Za-z0-9._:\-]+$", device_id):
        return jsonify({"success": False,
                        "message": "device_id 无效（10-64 位字母/数字/._:-）"}), 400

    # 限速：IP 桶 120/小时（ProxyFix 后是真实 IP）、设备桶沿用管理员可调阈值（默认 20/小时）。
    # **两个桶都要计数**（先算 ok_ip / ok_dev 再判定）：原写法 `A or B` 短路，IP 桶一满
    # 设备桶就永远不被计数，而且全站被 nginx 的 127.0.0.1 共用一个桶（20 次即锁死所有人）。
    ip = request.remote_addr or "unknown"
    _dev_login_limit = _SECURITY["device_login_rate_limit"]
    dev_key = hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:32]
    ok_ip = _rate_limit(f"device_login:ip:{ip}", 120, 3600)
    ok_dev = _rate_limit(f"device_login:dev:{dev_key}", _dev_login_limit, 3600)
    if not (ok_ip and ok_dev):
        resp = jsonify({"success": False, "message": "注册过于频繁，请稍后再试"})
        resp.headers["Retry-After"] = "600"
        return resp, 429

    # ===== S1-06：设备密钥校验（不再只凭自报 device_id 换 token）=====
    _secret_ok, _secret_msg, _issued_secret = _check_device_secret(
        device_id, data.get("device_secret"))
    if not _secret_ok:
        print(f"  [AUTH] 设备登录被拒（device_secret 校验失败）: {device_id[:12]}...")
        return jsonify({"success": False, "message": _secret_msg}), 403

    # 用 device_id 生成稳定的设备 openid
    dev_openid = "dev_" + hashlib.sha256(device_id.encode()).hexdigest()[:24]

    conn = get_user_db()
    existing = conn.execute(
        "SELECT openid, bound_openid FROM users WHERE openid = ?", (dev_openid,)
    ).fetchone()

    registered = bool(existing)      # users 表是否已有该设备账号
    if existing:
        # 设备已绑定微信账号 → 直接签发微信账号的 token（同一账号身份、同一权限）
        bound_openid = existing["bound_openid"] or ""
        token_openid = bound_openid or dev_openid
    else:
        # 惰性注册：不建号，签发 guest 级设备 token。compute_role 对不存在的 openid 返回 guest，
        # 直到真正兑换授权密钥（/api/license/redeem）时才惰性建号。
        bound_openid = ""
        token_openid = dev_openid

    token = _issue_login_token(token_openid)
    conn.close()

    print(f"设备登录: device_id={device_id[:12]}..., openid={token_openid[:8]}..., bound={bool(bound_openid)}, registered={registered}")

    _resp = {
        "success": True,
        "token": token,
        "openid": token_openid,
        "bound": bool(bound_openid),
        "registered": registered,
    }
    if _issued_secret:
        # 只在"首次注册 / 宽限期轮换"时下发一次；客户端应存到本地并在后续登录时回传
        _resp["device_secret"] = _issued_secret
    return jsonify(_resp)


# ==================== 微信账号绑定（个人认证密钥） ====================

_BIND_KEY_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_BIND_DEVICE_LIMIT = 3   # 每微信账号最多绑定的设备数
_BIND_UNUSED_LIMIT = 3   # 每微信账号同时未使用的密钥数上限


@app.route("/api/bind/create", methods=["POST"])
@login_required
def bind_key_create():
    """任意登录用户生成个人认证密钥：在 APP 端填写后，设备账号绑定到当前微信账号。
    密钥 1-10 分钟有效、一次性；已绑定设备数与未使用密钥数均有上限。
    """
    if g.openid.startswith("dev_"):
        return jsonify({"success": False, "message": "请使用微信小程序生成绑定密钥"}), 400

    # 限速（P1-6）：每用户每小时最多生成 5 个
    if not _rate_limit(f"bind_create:{g.openid}", 5, 3600):
        return jsonify({"success": False, "message": "生成过于频繁，请稍后再试"}), 429

    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    data = request.get_json() or {}
    validity = int(data.get("validity_minutes", 5))
    validity = max(1, min(10, validity))
    expires = now + timedelta(minutes=validity)

    conn = get_user_db()
    # 已绑定设备数上限（status='used' 的绑定密钥即绑定关系记录）
    used_count = conn.execute(
        "SELECT COUNT(*) FROM bind_keys WHERE created_by = ? AND status = 'used'",
        (g.openid,),
    ).fetchone()[0]
    if used_count >= _BIND_DEVICE_LIMIT:
        conn.close()
        return jsonify({"success": False, "message": f"绑定设备已达上限（{_BIND_DEVICE_LIMIT} 台），请先解绑"}), 403
    # 未使用密钥数上限
    unused_count = conn.execute(
        "SELECT COUNT(*) FROM bind_keys WHERE created_by = ? AND status = 'unused' AND expires_at > ?",
        (g.openid, now_str),
    ).fetchone()[0]
    if unused_count >= _BIND_UNUSED_LIMIT:
        conn.close()
        return jsonify({"success": False, "message": "未使用密钥过多，请等待过期后再生成"}), 429

    # 生成唯一密钥（全量查重 + UNIQUE 约束双保险）
    bind_key = ""
    for _ in range(20):
        candidate = ''.join(secrets.choice(_BIND_KEY_ALPHABET) for _ in range(8))
        dup = conn.execute("SELECT id FROM bind_keys WHERE key = ?", (candidate,)).fetchone()
        if not dup:
            bind_key = candidate
            break
    if not bind_key:
        conn.close()
        return jsonify({"success": False, "message": "生成失败，请重试"}), 500

    conn.execute(
        """INSERT INTO bind_keys (key, created_by, created_at, expires_at, status)
           VALUES (?, ?, ?, ?, 'unused')""",
        (bind_key, g.openid, now_str, expires.strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()

    print(f"用户 {g.openid[:8]}... 生成个人认证密钥: {bind_key}, 有效期 {validity} 分钟")
    return jsonify({
        "success": True,
        "key": bind_key,
        "expires_at": expires.strftime("%Y-%m-%d %H:%M:%S"),
        "validity_minutes": validity,
    })


@app.route("/api/bind/redeem", methods=["POST"])
@login_required
def bind_key_redeem():
    """APP 设备账号兑换个人认证密钥：绑定到密钥创建者的微信账号。
    - 仅 dev_ 设备账号可兑换（微信账号自身不可兑换）
    - 兑换成功后设备账号的历史订单/文件迁移到微信账号，来源标记为 APP
    - 返回微信账号的新 token，APP 切换到微信账号身份
    """
    if not g.openid.startswith("dev_"):
        return jsonify({"success": False, "message": "请使用 APP 设备账号完成绑定"}), 400

    if not _rate_limit(f"bind_redeem:{g.openid}", _SECURITY["redeem_rate_limit"], 60):
        return jsonify({"success": False, "message": "兑换过于频繁，请稍后再试"}), 429

    data = request.get_json() or {}
    raw_key = (data.get("key", "") or "").strip().upper()
    if len(raw_key) != 8:
        return jsonify({"success": False, "message": "密钥格式不正确"}), 400

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dev_openid = g.openid
    conn = get_user_db()

    # 设备账号不可重复绑定（users.bound_openid 或 bind_keys 历史记录任一命中即已绑定；
    # 兼容早期 bug 只把绑定写入 bind_keys 的脏数据，避免重复绑定产生多条 used 记录）
    dev_row = conn.execute(
        "SELECT bound_openid FROM users WHERE openid = ?", (dev_openid,)
    ).fetchone()
    if dev_row and dev_row["bound_openid"]:
        conn.close()
        return jsonify({"success": False, "message": "该设备已绑定微信账号，请先解绑"}), 400
    if conn.execute(
        "SELECT id FROM bind_keys WHERE used_by = ? AND status = 'used'", (dev_openid,)
    ).fetchone():
        conn.close()
        return jsonify({"success": False, "message": "该设备已绑定微信账号，请先解绑"}), 400

    # 原子认领：仅未使用且未过期才生效
    conn.execute(
        """UPDATE bind_keys SET status = 'used', used_by = ?, used_at = ?
           WHERE key = ? AND status = 'unused' AND expires_at > ?""",
        (dev_openid, now_str, raw_key, now_str),
    )
    conn.commit()
    if conn.total_changes == 0:
        row = conn.execute(
            "SELECT used_by, expires_at FROM bind_keys WHERE key = ?", (raw_key,)
        ).fetchone()
        conn.close()
        if not row:
            return jsonify({"success": False, "message": "密钥不存在"}), 404
        if row["used_by"] is not None:
            return jsonify({"success": False, "message": "密钥已被使用"}), 400
        return jsonify({"success": False, "message": "密钥已过期"}), 400

    key_row = conn.execute(
        "SELECT created_by FROM bind_keys WHERE key = ?", (raw_key,)
    ).fetchone()
    wx_openid = key_row["created_by"] if key_row else ""

    # 写入绑定关系。注意 /api/device_login 是惰性注册：设备从未兑换授权密钥/更新资料时，
    # users 表没有该 dev 账号的行，直接 UPDATE 会静默影响 0 行——绑定关系只落在 bind_keys，
    # 导致解绑/重命名时查 users.bound_openid 为空、报"该设备未绑定微信账号"。
    # 因此先补齐 users 行，再写 bound_openid。
    if not conn.execute(
        "SELECT openid FROM users WHERE openid = ?", (dev_openid,)
    ).fetchone():
        dev_nickname = "device_" + ''.join(
            secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8)
        )
        conn.execute(
            "INSERT INTO users (openid, nickname, avatar_path, updated_at) VALUES (?, ?, '', ?)",
            (dev_openid, dev_nickname, now_str),
        )
    conn.execute(
        "UPDATE users SET bound_openid = ?, updated_at = ? WHERE openid = ?",
        (wx_openid, now_str, dev_openid),
    )
    conn.commit()
    conn.close()

    # 历史订单/文件迁移：dev 账号此前的订单并入微信账号（统计"同一用户"口径连续）。
    # 先回填旧版 APP 订单来源为 app，再迁移归属。
    orders_conn = get_db()
    try:
        orders_conn.execute(
            "UPDATE orders SET source = 'app' WHERE openid = ? AND source = 'wechat'",
            (dev_openid,),
        )
        orders_conn.execute(
            "UPDATE orders SET openid = ? WHERE openid = ?", (wx_openid, dev_openid)
        )
        orders_conn.execute(
            "UPDATE files SET openid = ? WHERE openid = ?", (wx_openid, dev_openid)
        )
        orders_conn.commit()
    except Exception:
        orders_conn.rollback()
        raise
    finally:
        orders_conn.close()

    # 返回微信账号 token，APP 切换身份
    token = _issue_login_token(wx_openid)
    profile_conn = get_user_db()
    prow = profile_conn.execute(
        "SELECT nickname FROM users WHERE openid = ?", (wx_openid,)
    ).fetchone()
    profile_conn.close()
    nickname = (prow["nickname"] if prow else "") or ""

    print(f"设备 {dev_openid[:10]}... 已绑定微信账号 {wx_openid[:8]}...（密钥 {raw_key}）")
    return jsonify({
        "success": True,
        "message": "绑定成功，APP 已切换到微信账号身份",
        "token": token,
        "openid": wx_openid,
        "dev_openid": dev_openid,
        "nickname": nickname,
        "role": compute_role(wx_openid),
    })


@app.route("/api/bind/devices", methods=["GET"])
@login_required
def bind_devices():
    """查询当前微信账号已绑定的设备列表（含设备昵称、绑定时间、密钥）"""
    conn = get_user_db()
    rows = conn.execute(
        """SELECT b.used_by AS dev_openid, b.used_at, b.key, u.nickname
           FROM bind_keys b
           LEFT JOIN users u ON b.used_by = u.openid
           WHERE b.created_by = ? AND b.status = 'used'
           ORDER BY b.used_at DESC""",
        (g.openid,),
    ).fetchall()
    conn.close()
    devices = [{
        "dev_openid": r["dev_openid"] or "",
        "nickname": r["nickname"] or "",
        "key": r["key"],
        "used_at": r["used_at"] or "",
    } for r in rows]
    return jsonify({"success": True, "devices": devices, "device_count": len(devices)})


@app.route("/api/bind/rename", methods=["POST"])
@login_required
def bind_rename():
    """小程序重命名已绑定设备：仅绑定关系中的微信账号可操作。
    名称写入该 dev_ 账号的 users.nickname，作为设备在“已绑定设备”列表中的展示名。
    body: { "dev_openid": "dev_...", "nickname": "办公室平板" }
    """
    data = request.get_json() or {}
    dev_openid = (data.get("dev_openid", "") or "").strip()
    nickname = (data.get("nickname", "") or "").strip()
    if not dev_openid.startswith("dev_"):
        return jsonify({"success": False, "message": "设备账号无效"}), 400
    if not nickname:
        return jsonify({"success": False, "message": "名称不能为空"}), 400
    if len(nickname) > 20:
        return jsonify({"success": False, "message": "名称最长 20 个字符"}), 400

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_user_db()
    row = conn.execute(
        "SELECT bound_openid FROM users WHERE openid = ?", (dev_openid,)
    ).fetchone()
    if not row or not row["bound_openid"]:
        # 兜底：兼容早期绑定只写入 bind_keys 的脏数据（users 行缺失或 bound_openid 为空），
        # 与解绑逻辑一致，以 bind_keys 中 status='used' 的记录作为绑定关系判据。
        bk = conn.execute(
            "SELECT created_by FROM bind_keys WHERE used_by = ? AND status = 'used' "
            "ORDER BY used_at DESC LIMIT 1",
            (dev_openid,),
        ).fetchone()
        if not bk:
            conn.close()
            return jsonify({"success": False, "message": "该设备未绑定微信账号"}), 404
        bound_wx = bk["created_by"]
    else:
        bound_wx = row["bound_openid"]
    if g.openid != bound_wx:
        conn.close()
        return jsonify({"success": False, "message": "仅账号本人可重命名"}), 403

    # 写入昵称；设备从未建号时先补齐 users 行（bound_openid 一并写入，保持两处绑定关系一致）
    if not row:
        conn.execute(
            "INSERT INTO users (openid, nickname, avatar_path, bound_openid, updated_at) "
            "VALUES (?, ?, '', ?, ?)",
            (dev_openid, nickname, bound_wx, now_str),
        )
    else:
        conn.execute(
            "UPDATE users SET nickname = ?, updated_at = ? WHERE openid = ?",
            (nickname, now_str, dev_openid),
        )
    conn.commit()
    conn.close()

    print(f"微信账号 {g.openid[:8]}... 重命名设备 {dev_openid[:10]}... → {nickname}")
    return jsonify({"success": True, "nickname": nickname})


@app.route("/api/bind/revoke", methods=["POST"])
@login_required
def bind_revoke():
    """解除设备绑定：仅绑定关系中的微信账号可操作（小程序本人或已绑定的 APP）。
    解绑后设备账号回退为独立 dev_ 账号；APP 侧可拿到新的 dev_ token 切换身份。
    """
    data = request.get_json() or {}
    dev_openid = (data.get("dev_openid", "") or "").strip()
    if not dev_openid.startswith("dev_"):
        return jsonify({"success": False, "message": "设备账号无效"}), 400

    conn = get_user_db()
    row = conn.execute(
        "SELECT bound_openid FROM users WHERE openid = ?", (dev_openid,)
    ).fetchone()
    if not row or not row["bound_openid"]:
        # 兜底：兼容早期绑定只写入 bind_keys 的脏数据（设备从未建号，或建号晚于绑定导致
        # users.bound_openid 为空），以 bind_keys 中 status='used' 的记录作为绑定关系判据，
        # 保证已绑定设备始终可解绑。
        bk = conn.execute(
            "SELECT created_by FROM bind_keys WHERE used_by = ? AND status = 'used' "
            "ORDER BY used_at DESC LIMIT 1",
            (dev_openid,),
        ).fetchone()
        if not bk:
            conn.close()
            return jsonify({"success": False, "message": "该设备未绑定微信账号"}), 404
        wx_openid = bk["created_by"]
    else:
        wx_openid = row["bound_openid"]
    if g.openid != wx_openid:
        conn.close()
        return jsonify({"success": False, "message": "仅账号本人可解除绑定"}), 403

    conn.execute(
        "UPDATE users SET bound_openid = '' WHERE openid = ?", (dev_openid,)
    )
    conn.execute(
        """UPDATE bind_keys SET status = 'revoked'
           WHERE created_by = ? AND used_by = ? AND status = 'used'""",
        (wx_openid, dev_openid),
    )
    conn.commit()
    conn.close()

    dev_token = _issue_login_token(dev_openid)
    print(f"微信账号 {wx_openid[:8]}... 已解绑设备 {dev_openid[:10]}...")
    return jsonify({
        "success": True,
        "message": "已解除绑定",
        "token": dev_token,
        "openid": dev_openid,
    })


def _sniff_upload_family(head: bytes):
    """按文件头判断真实家族（只认本系统关心的几种）；认不出返回 None。

    S2-03：客户端的 Content-Type 与文件名都不可信，**只看内容**。这里只做"是不是它声称的
    那一类"的判定，不做格式完整性解析（上传面的目标是拦住改名伪装，不是校验文件可用性）。"""
    if b"%PDF-" in head[:1024]:
        # PDF 规范允许头部前有少量垃圾字节 ⇒ 在前 1 KB 内找，别把正常 PDF 判成伪装
        return "pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head.startswith(b"BM"):
        return "bmp"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole"
    if head.lstrip()[:5].lower() == b"{\\rtf":
        return "rtf"
    return None


def _check_upload_type(ext_lower, head, client_mime):
    """S2-03：上传类型的三方一致性检查（白名单 / MIME / 魔数）。

    返回 (ok: bool, message: str)。**不碰磁盘**，调用方决定拒绝时机。
      · 扩展名不在白名单 → 直接拒（这是永久防线，新格式默认进不来）；
      · 客户端 MIME 是明确的危险类型（text/html、svg、js）→ 拒（与 denylist 同一条链）；
      · 客户端 MIME 是**具体类型**且与扩展名家族冲突（如 .pdf 却报 image/jpeg）→ 拒；
        octet-stream / 空 MIME 一律放行 —— 微信 wx.uploadFile 就是这么发的，误杀会打断业务；
      · 二进制类型的**魔数**必须落在该扩展名允许的家族里（挡住"改名伪装"）；
      · 文本类（txt/csv/md）无魔数，只拒"前 8 KB 出现 NUL"的伪文本。
    """
    if ext_lower not in UPLOAD_EXT_WHITELIST:
        return False, (f"本服务不支持 .{ext_lower} 文件；支持 PDF/Word/Excel/PPT/TXT/CSV/"
                       "Markdown/图片，请转换后重试")
    if client_mime in _DANGEROUS_MIMES:
        return False, "本服务不支持 HTML/SVG/JS 文件；请另存为 PDF/DOCX 后重试"
    ext_families = _UPLOAD_EXT_FAMILIES.get(ext_lower)
    if client_mime:
        mime_family = _MIME_FAMILIES.get(client_mime)
        if mime_family and ext_families and mime_family not in ext_families:
            return False, "文件类型与扩展名不一致，已拒绝——请用原始文件或另存为 PDF 后重试"
    if ext_families is None:
        # 文本类
        if _looks_like_binary(head):
            return False, "文件内容与扩展名不一致（疑似二进制文件），已拒绝"
        return True, ""
    actual = _sniff_upload_family(head)
    if actual is None:
        return False, ("无法识别的文件内容（或扩展名与实际格式不一致），已拒绝——"
                       "请用原始文件或另存为 PDF 后重试")
    if actual not in ext_families:
        return False, ("文件内容与扩展名不一致（可能是改名后的其他格式），已拒绝——"
                       "请用原始文件或另存为 PDF 后重试")
    return True, ""


def _looks_like_binary(head: bytes) -> bool:
    """文本类上传的粗判：前 8 KB 出现 NUL 即视为二进制（改名冒充 .txt/.csv/.md）。"""
    return b"\x00" in head


@app.route("/api/upload", methods=["POST"])
@require_printer_access
def upload_file():
    # 限速（P1-6 / 防滥用）：每用户每分钟最多上传次数（阈值管理员可调，单 worker 内存计数）
    if not _rate_limit(f"upload:{g.openid}", _SECURITY["upload_rate_limit"], 60):
        return jsonify({"success": False, "message": "上传过于频繁，请稍后再试"}), 429

    # 磁盘剩余空间守卫（防存储型 DDoS）：低于阈值直接拒绝，避免磁盘被写满
    min_free_bytes = _SECURITY["disk_min_free_mb"] * 1024 * 1024
    if min_free_bytes > 0:
        try:
            free_bytes = shutil.disk_usage(UPLOAD_DIR).free
            if free_bytes < min_free_bytes:
                return jsonify({
                    "success": False,
                    "message": "服务器存储空间不足，请稍后再试",
                    "free_mb": round(free_bytes / (1024 * 1024), 1),
                }), 503
        except OSError:
            pass  # 磁盘信息不可用时放行，交由配额兜底

    if "file" not in request.files:
        return jsonify({"success": False, "message": "未找到上传文件"}), 400

    f = request.files["file"]
    if f.filename == "":
        return jsonify({"success": False, "message": "文件名为空"}), 400

    file_id = uuid.uuid4().hex
    # S1-03（D-6）：-1 = 打印完成立即删除；0 = 遵从服务器当前设置。
    # 正数仍兼容旧客户端，并夹到 1..168。
    _retention_hours = _clamp_retention_hours(request.form.get("retention_hours"))
    ext = os.path.splitext(f.filename)[1]  # 保留原始大小写
    ext_lower = ext.lower().lstrip(".")
    # P2-9：扩展名白名单正则校验（1-8 位字母数字），非法 → 400（原实现会把非法扩展名
    # 直接拼进路径导致 500 或目录穿越风险）
    if not re.match(r"^[a-zA-Z0-9]{1,8}$", ext_lower):
        return jsonify({"success": False, "message": "文件扩展名无效"}), 400
    # S0-05（D-2 已拍板：永久拒绝，不做开关）：危险类型 denylist。
    # 顾客上传 .html/.svg/.js 会在店铺机的转换/浏览器管线里执行脚本或读本机文件 ⇒
    # 可读 file:///…/print_config.json 并把全站 printer token 外发（DS-01 的完整链路）。
    # 放在落盘之前拒绝，不写临时文件；对存量客户端立即生效（客户端侧入口由 S0-09 删除）。
    if ext_lower in DANGEROUS_UPLOAD_EXTS:
        print(f"  [UPLOAD] 拒绝危险类型上传: {_redact_filename(f.filename)!r} ext={ext_lower}")
        return jsonify({
            "success": False,
            "message": "本服务不支持 HTML/SVG/JS 文件；请另存为 PDF/DOCX 后重试",
        }), 400
    # 2026-10-25：真实文件名（客户端自报）优先于 multipart 里的临时名。
    # `f.filename` 在小程序上传时是 `tmp_<hex>.<ext>`（微信临时路径 basename）；
    # 客户端在 4.5.36+ / 小程序同版起把顾客选的文件名放在表单字段 `file_name`。
    # 校验不通过（路径、异扩展名、控制字符）一律退回 f.filename —— 可选字段不应让上传失败。
    display_name = clean_original_name(request.form.get("file_name"), ext_lower) or f.filename
    # S2-03：白名单 + MIME 冲突（落盘之前就能判的两项；魔数要读内容，放在保存之后）。
    # 白名单先行 ⇒ 压缩包/未知类型连临时文件都不写。
    if ext_lower not in UPLOAD_EXT_WHITELIST:
        print(f"  [UPLOAD] 拒绝白名单外类型: {_redact_filename(f.filename)!r} ext={ext_lower}")
        return jsonify({
            "success": False,
            "message": (f"本服务不支持 .{ext_lower} 文件；支持 PDF/Word/Excel/PPT/TXT/CSV/"
                        "Markdown/图片，请转换后重试"),
        }), 400
    _client_mime = (f.mimetype or "").lower().strip()
    if _client_mime in _DANGEROUS_MIMES:
        print(f"  [UPLOAD] 拒绝危险 MIME: {_redact_filename(f.filename)!r} mime={_client_mime}")
        return jsonify({
            "success": False,
            "message": "本服务不支持 HTML/SVG/JS 文件；请另存为 PDF/DOCX 后重试",
        }), 400
    saved_name = f"{file_id}{ext}"

    # 1. 先保存到 uploads/ 根目录作为临时文件
    temp_path = os.path.join(UPLOAD_DIR, saved_name)
    f.save(temp_path)
    file_size = os.path.getsize(temp_path)

    # S2-03：魔数一致性（只剩这一项要读内容）。不通过 ⇒ 删临时文件 + 400，
    # 绝不把"改名伪装"的文件留在 uploads 里（与下面的 Markdown 扫描同一收尾口径）。
    try:
        with open(temp_path, "rb") as _sig_fh:
            _head = _sig_fh.read(8192)
    except OSError:
        _head = b""
    _sig_ok, _sig_msg = _check_upload_type(ext_lower, _head, _client_mime)
    if not _sig_ok:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        print(f"  [UPLOAD] 拒绝类型不一致上传: {_redact_filename(f.filename)!r} ext={ext_lower} "
              f"mime={_client_mime!r} 原因={_sig_msg}")
        return jsonify({"success": False, "message": _sig_msg}), 400

    # S0-05：Markdown 会被渲染成 HTML 交给 wkhtmltopdf —— 扫危险语法（≤1 MB）。
    # 客户端 converter 有同一份扫描（S0-09），这里是**服务端持久防线**：旧客户端仍在跑时
    # 依然拦得住。命中即删临时文件并拒绝，不给"另存为"以外的替代路径。
    if ext_lower == "md":
        try:
            with open(temp_path, "rb") as _md_fh:
                _md_text = _md_fh.read(1024 * 1024).decode("utf-8", errors="ignore")
        except OSError:
            _md_text = ""
        _hit = _scan_unsafe_markdown(_md_text)
        if _hit:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            print(f"  [UPLOAD] 拒绝含危险语法的 Markdown: {_redact_filename(f.filename)!r} 命中={_hit}")
            return jsonify({
                "success": False,
                "message": "文档包含不安全内容，已拒绝——请另存为 PDF/DOCX 后重试",
            }), 400

    # 防滥用：每用户累计配额检查（含本次文件；超限拒绝并清理临时文件，防存储型 DDoS）。
    # 注：S2-05 起每个上传都落**自己的一份副本**（不再共享物理文件），所以"配额合计"
    # 就是"实际磁盘占用"，口径一致。
    quota_bytes = _SECURITY["user_quota_mb"] * 1024 * 1024
    if quota_bytes > 0:
        conn = get_db()
        used = conn.execute(
            "SELECT COALESCE(SUM(size), 0) AS used FROM files WHERE openid = ? AND path != ''",
            (g.openid,),
        ).fetchone()["used"] or 0
        conn.close()
        if used + file_size > quota_bytes:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            return jsonify({
                "success": False,
                "message": "存储配额已满，请删除旧文件后再上传",
                "quota_mb": _SECURITY["user_quota_mb"],
                "used_mb": round(used / (1024 * 1024), 1),
            }), 413

    # 2. 计算 MD5（S2-05 起**只**用于「已验证页数」缓存命中，不再共享物理文件）
    file_md5 = get_file_md5(temp_path)
    md5_index = load_md5_index()
    existing_entry = _md5_entry_get(md5_index, file_md5)
    cached_page_count = _cached_verified_page_count(file_md5, g.openid, md5_index)
    cached_page_verified = cached_page_count > 0

    # 索引条目指向的物理文件已丢失（属主删过/过期清理过）→ 只作废**这个条目**；
    # 页数缓存另有 files 表兜底（见 _cached_verified_page_count），不会因为删了一个副本
    # 就把"这版文档有几页"这个事实忘掉。
    if existing_entry:
        _stale_rel = existing_entry.get("path", "") or ""
        _stale_abs = os.path.join(UPLOAD_DIR, _stale_rel) if _stale_rel else ""
        if not _stale_abs or not os.path.exists(_stale_abs):
            md5_index.pop(file_md5, None)
            save_md5_index(md5_index)
            existing_entry = None
            print(f"  [MD5] 索引记录失效（文件丢失），改为保存新副本: {_stale_rel}")

    # 3. 确定扩展名子目录，把**本次上传**的文件移进去。
    #    S2-05（DS-14）：每个上传都保存**自己的副本**，不再把新行指向别人的物理文件 ——
    #    旧行为让多个 files 行共享同一路径，「任一行到期即删盘」会让另一方文件悬空
    #    （下载 404、订单卡死），且 `reused:true` 变成"跨用户内容存在性"预言机。
    subdir = get_ext_dir(ext_lower)
    target_dir = os.path.join(UPLOAD_DIR, subdir)
    os.makedirs(target_dir, exist_ok=True)
    final_path = os.path.join(target_dir, saved_name)
    if os.path.exists(final_path):
        saved_name = f"{file_id}_{uuid.uuid4().hex[:6]}{ext}"
        final_path = os.path.join(target_dir, saved_name)
    shutil.move(temp_path, final_path)
    file_path = final_path

    # 4. 更新 MD5 索引（页数缓存；path 指向本行自己的副本）
    rel_path = os.path.relpath(file_path, UPLOAD_DIR)
    _md5_entry_set(md5_index, file_md5, path=rel_path, original_name=display_name,
                   page_count=cached_page_count, page_count_verified=cached_page_verified,
                   openid=g.openid)
    save_md5_index(md5_index)
    print(f"  [MD5] 已保存副本: {file_md5[:8]}... → {rel_path}"
          f"{', 页数缓存=' + str(cached_page_count) if cached_page_verified else ''}")

    # 5. 计算/复用文件页数
    if cached_page_verified and cached_page_count > 0:
        # 同 MD5 文件已由本地工具验证 → 直接复用页数，跳过分析
        page_count = cached_page_count
        print(f"  [PAGE] 复用已验证页数: {_redact_filename(display_name)} → {page_count} 页 (from MD5 cache)")
    else:
        page_count = get_file_page_count(file_path, ext_lower)

    # 6. 写入 files 表（含 page_count 缓存、md5、openid 归属，P2-10）
    conn = get_db()
    conn.execute(
        """
        INSERT INTO files (id, original_name, saved_name, path, size, created_at, page_count, page_count_verified, md5, openid, retention_hours)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (file_id, display_name, saved_name, file_path, file_size,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S"), page_count,
         int(cached_page_verified), file_md5, g.openid, _retention_hours),
    )
    conn.commit()
    conn.close()

    # 2026-11-13（用户要求）：上传即把缩略图/首页预览预生成并暂存下来（跟着文件
    # 保留期一起删）。后台线程做，不拖慢上传响应。
    _spawn_preview_prewarm(file_id, display_name)

    # 对需要本地工具分析的文档格式，请求直连的打印客户端下载并分析页数
    # 但若同 MD5 已有验证页数，跳过分析
    # prefer：前端当前选中的接单设备（用户选了哪台就哪台分析；离线则退回订单目标/任一在线接单设备）
    if page_count == 0 and ext_lower in ("doc", "docx") and not cached_page_verified:
        prefer_client = (request.form.get("target_client_id", "") or "").strip()[:64]
        request_page_analysis(file_id, display_name, prefer_client=prefer_client)

    print(f"文件上传: {_redact_filename(display_name)} -> {_rel_display(file_path)} "
          f"(id={file_id}, pages={page_count}, md5={file_md5[:8]}...)")

    # S2-05（DS-14）：响应**不再**带 `reused` 字段，也不再有「文件已存在」文案 ——
    # 否则任何人上传一份候选内容就能问出"这份内容是否已经在系统里"（跨用户存在性预言机）。
    # 客户端（小程序/APP）本来就不读这个字段，只有 `message` 被展示。
    return jsonify(
        {
            "success": True,
            "message": "文件上传成功",
            "file_id": file_id,
            "original_name": display_name,
            "size": file_size,
            "page_count": page_count,
        }
    )


def _plan_order_files(conn, files_input, openid, default_duplex, schedule_mode, prices):
    """（S2-02）订单子任务的**只读**预计算：页数 / 单价 / 初始状态 / 打回原因。

    ⚠ 只允许在**写事务之外**调用：本函数会读 PDF 数页（磁盘 IO，可能是整份文件解析），
    而 SQLite 的写者锁是全站唯一的 —— 放在写事务里等于把文件 IO 的成本摊到所有写路径上
    （DDoS 报告 A3/R6：1 MB 请求能把写锁占 1.7 s）。submit_order 的写事务里只许
    INSERT/UPDATE，不许出现任何读盘操作。

    返回：计划列表，每项一个 dict（不碰数据库）。抛 OrderRejected 表示整单应被拒 ——
    与"事务内抛异常回滚"的语义一致（400 且一条行都不落），只是现在连事务都还没开。
    """
    plan = []
    for f in files_input:
        f_id = f.get("file_id", "") or ""
        f_name = f.get("file", f.get("file_name", ""))
        # P1-4.4：份数钳制 1-999，非法类型 → 400
        try:
            f_copies = max(1, min(999, int(f.get("copies", 1))))
        except (ValueError, TypeError):
            raise OrderRejected("份数必须是 1-999 的整数")
        f_page_range = (f.get("page_range", "") or "").strip()

        # 计算页数与价格（优先从 files 表缓存读取）
        page_count = 1
        page_count_verified = False
        persist_pages = None          # (page_count, verified)：需要写回 files 表（写事务里做）
        persist_original_name = None  # 展示名自愈：临时名 → 本次提交的真实名（写事务里做）
        if f_id:
            frow = conn.execute(
                "SELECT path, original_name, page_count, page_count_verified, openid FROM files WHERE id = ?",
                (f_id,),
            ).fetchone()
            if frow:
                # P2-10：文件归属校验（openid 为空 = 迁移前的旧记录，兼容放行）
                if frow["openid"] and frow["openid"] != openid:
                    raise OrderRejected("文件归属校验失败，无权使用该文件")
                cached = frow["page_count"] or 0
                if cached > 0:
                    page_count = cached
                    page_count_verified = bool(frow["page_count_verified"])
                elif frow["path"] and os.path.exists(frow["path"]):
                    # 缓存未命中，重新计算（PDF 可当场获取；doc/docx 返回 0 = 等本地工具）
                    ext = os.path.splitext(frow["path"])[1].lower().lstrip(".")
                    _computed = get_file_page_count(frow["path"], ext) or 0
                    if _computed > 0:
                        page_count = _computed
                        page_count_verified = ext not in ("doc", "docx")
                        persist_pages = (page_count, page_count_verified)
                    else:
                        # 2026-10-17：页数未知时**只对本单按 1 页兜底计价**，绝不把兜底值
                        # 写回 files.page_count —— 补推页数分析的路径以
                        # 「page_count_verified = 0」为前提（见 _pending_page_analysis_rows），
                        # 而历史 bug 正是把 0 写成 1 之后：该文件再也不会被送去分析，
                        # verified 永远 0、价格永久冻在 1 页（少收），前端复制价格永久被拦。
                        page_count = 1
                        page_count_verified = False
                if not f_name:
                    f_name = frow["original_name"]
                # 展示名自愈（2026-10-25）：老客户端上传留下的微信临时名（tmp_<hex>.pdf）
                # 在这份文件第一次以「真实文件名」下单时订正掉 —— 之后 /api/my_files、
                # 下载（Content-Disposition）与页数分析（按扩展名分支）都拿到顾客认得的名字。
                # 只认**同扩展名**的真实名，且只改临时名（别覆盖用户已经正确的名字）。
                _stored_name = (frow["original_name"] or "").strip()
                _submitted = (f_name or "").strip()
                if (looks_like_temp_upload_name(_stored_name) and _submitted
                        and not looks_like_temp_upload_name(_submitted)):
                    _new_name = clean_original_name(_submitted)
                    if _new_name and (os.path.splitext(_new_name)[1].lower()
                                      == os.path.splitext(_stored_name)[1].lower()):
                        persist_original_name = _new_name
            else:
                ext = os.path.splitext(f_name)[1].lower().lstrip(".")
                page_count = get_file_page_count(None, ext) or 1
        else:
            ext = os.path.splitext(f_name)[1].lower().lstrip(".")
            page_count = get_file_page_count(None, ext) or 1

        if not f_name:
            f_name = "未知文件"

        # 读取每文件的双面设置（优先 files 数组中的值，其次顶层 duplex）
        f_duplex = f.get("duplex", default_duplex) or "on"

        # 「封面单独打印」（2026-11-02 用户需求）：所列页各占一张纸、其余页两页一张纸。
        # 页码填错 / 超出总页数 / 不在「页码范围」之内 ⇒ 该文件打回（不静默忽略：
        # 否则顾客以为封面单面、实际按整份双面出纸，价格与出纸都对不上）。
        f_simplex_pages = ""
        _simplex_reason = ""
        if f_duplex == "on":
            f_simplex_pages, _simplex_reason = _simplex_pages_checked(
                f.get("simplex_pages", "") or "", f_page_range, page_count, page_count_verified)
            if _simplex_reason:
                # 打回原因先存着，等 sub_status / reject_reason 初始化之后再落 ——
                # 在这里直接赋值会被下面几行的初始化重置回 queued（实测踩过）。
                print("  [REJECT] 提交时打回: %s simplex=%r %s"
                      % (f_name, f.get("simplex_pages"), _simplex_reason))
        # 「排除末尾空白页」：只认 0/1；真正的空白页判定在设备端（后端没有 PyMuPDF），
        # 本地排除后立刻回报 page_range_truncated，后端据此重算价格。
        f_blank_tail_exclude = 1 if str(f.get("blank_tail_exclude", 0)).strip() in ("1", "true", "True") else 0

        per_copy_price = calculate_price(page_count, f_duplex, f_page_range, prices=prices,
                                         simplex_pages=f_simplex_pages)
        total_price = round(per_copy_price * f_copies, 2)  # 始终计算真实价格，is_free 控制是否收费
        # 普通单从 queued 开始，由 push_print_task_to_client 原子锁定为 printing；
        # 预约单从 scheduled 开始，阶段①下发文件时为 downloading（不进 queued，避免被普通链路误分发）
        sub_status = "scheduled" if schedule_mode != "now" else "queued"
        reject_reason = ""

        # 「封面单独打印」填错 ⇒ 打回（必须排在 sub_status / reject_reason 初始化之后，
        # 否则会被上面两行重置；见 2026-11-02 实测）
        if _simplex_reason and sub_status != "rejected":
            sub_status = "rejected"
            reject_reason = _simplex_reason

        # 若页数已确认，立即校验页码范围
        if page_count_verified and page_count > 0 and f_page_range:
            max_page = _parse_page_range_max(f_page_range)
            if max_page > page_count:
                sub_status = "rejected"
                reject_reason = f"页码范围超出文件总页数（填写 {max_page} 页，文件共 {page_count} 页）"
                print(f"  [REJECT] 提交时打回: {f_name} range={f_page_range} 超出 {page_count} 页")

        f_image_orientation = f.get("image_orientation", "auto") or "auto"
        if f_image_orientation not in ("auto", "landscape", "portrait"):
            f_image_orientation = "auto"

        plan.append({
            "file_id": f_id or None,
            "file_name": f_name,
            "copies": f_copies,
            "page_count": page_count,
            "page_count_verified": page_count_verified,
            "persist_pages": persist_pages,
            "persist_original_name": persist_original_name,
            "page_range": f_page_range,
            "total_price": total_price,
            "status": sub_status,
            "duplex": f_duplex,
            "image_orientation": f_image_orientation,
            "simplex_pages": f_simplex_pages,
            "blank_tail_exclude": f_blank_tail_exclude,
            "reject_reason": reject_reason,
        })
    return plan


@app.route("/api/submit_order", methods=["POST"])
@require_printer_access
def submit_order():
    # 限速（P1-6 / 防滥用）：每用户每分钟提交订单次数上限（管理员可调，单 worker 内存计数）
    if not _rate_limit(f"submit_order:{g.openid}", _SECURITY["submit_order_rate_limit"], 60):
        return jsonify({"success": False, "message": "提交过于频繁，请稍后再试"}), 429

    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "请提供 JSON 数据"}), 400

    # 发起端标记：小程序 / APP（写入 orders.source，供统计页区分下单渠道）。
    # 绑定后 APP 与小程序共享 openid，必须依赖显式标记而非 openid 前缀。
    client = (data.get("client", "wechat") or "wechat").strip().lower()
    if client not in ("wechat", "app"):
        client = "wechat"

    # 幂等键（P0-2）：客户端每次提交生成唯一 ID；同 openid + 同 ID 且 10 分钟内
    # 已成功建单 → 直接返回原订单，防双击/网络重试造成重复订单。
    client_request_id = (data.get("client_request_id", "") or "").strip()

    duplex = data.get("duplex", "on")  # 顶层 duplex 作为默认值（向后兼容）
    files_input = data.get("files", None)

    # ---- v5 新增：附加服务参数 ----
    # P1-4：全部做边界钳制（delivery/urgency/cover 均为非负金额或百分比），
    # 非法类型（ValueError/TypeError）→ 400 明确错误。
    try:
        delivery_enabled = int(data.get("delivery_enabled", 0) or 0)
        delivery_location = data.get("delivery_location", "")
        delivery_percentage = max(0.0, min(100.0, float(data.get("delivery_percentage", 0) or 0)))
        urgency = data.get("urgency", "低")
        urgency_price = max(0.0, min(100.0, float(data.get("urgency_price", 0) or 0)))
        cover_page = int(data.get("cover_page", 0) or 0)
        cover_page_price = max(0.0, min(100.0, float(data.get("cover_page_price", 0.10) or 0)))
        pickup_address = data.get("pickup_address", "")
        auto_print = int(data.get("auto_print", 0) or 0)  # 无障碍打印：提交后自动开始打印
        # 订单备注：≤100 字，多余截断
        remark = str(data.get("remark", "") or "").strip()[:100]
    except (ValueError, TypeError):
        return jsonify({"success": False, "message": "附加服务参数格式不正确"}), 400

    # P1-4.7：服务端价格优先——pricing.json 中存在的配置项覆盖客户端金额字段
    # （delivery 按地点百分比、urgency 按档位价、cover 按统一首页费），忽略客户端传入值。
    pricing_cfg = _get_pricing_config()
    if pricing_cfg:
        if delivery_enabled and delivery_location in (pricing_cfg.get("delivery_percentages") or {}):
            delivery_percentage = float(pricing_cfg["delivery_percentages"][delivery_location])
        elif not delivery_enabled:
            delivery_percentage = 0.0
        if urgency in (pricing_cfg.get("urgency_prices") or {}):
            urgency_price = float(pricing_cfg["urgency_prices"][urgency])
        if cover_page and "cover_page_price" in pricing_cfg:
            cover_page_price = float(pricing_cfg["cover_page_price"])

    # ---- 无障碍打印预约：立即/指定时间/倒计时 → 折算为绝对时间 scheduled_at ----
    # （P2-17）scheduled_at 为服务器本地时间字符串，依赖服务器时区；
    # 部署时必须将服务器时区设为 Asia/Shanghai，否则预约到点判定偏移。
    schedule_mode = (data.get("schedule_mode", "now") or "now").strip()
    if schedule_mode not in ("now", "at", "countdown"):
        schedule_mode = "now"
    scheduled_at = ""
    if schedule_mode == "at":
        # 指定时间：日期（0=今天 1=明天 2=后天）+ HH:MM
        schedule_day = int(data.get("schedule_day", 0) or 0)
        schedule_time = (data.get("schedule_time", "") or "").strip()
        m = re.match(r"^(\d{1,2}):(\d{2})$", schedule_time)
        if not m:
            return jsonify({"success": False, "message": "请选择预约时间"}), 400
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 23 or mm > 59 or schedule_day not in (0, 1, 2):
            return jsonify({"success": False, "message": "预约时间格式不正确"}), 400
        target = (datetime.now() + timedelta(days=schedule_day)).replace(
            hour=hh, minute=mm, second=0, microsecond=0)
        if target <= datetime.now():
            return jsonify({"success": False, "message": "预约时间已过，请重新选择"}), 400
        scheduled_at = target.strftime("%Y-%m-%d %H:%M:%S")
    elif schedule_mode == "countdown":
        # 倒计时：___分___秒
        # P1-4.5：范围 1 秒 ~ 7 天（604800），非法（含 0/负数/非数字）→ 400
        try:
            countdown_seconds = int(data.get("countdown_seconds", 0) or 0)
        except (ValueError, TypeError):
            return jsonify({"success": False, "message": "倒计时时长必须是数字"}), 400
        if not (1 <= countdown_seconds <= 604800):
            return jsonify({"success": False, "message": "倒计时时长必须在 1 秒到 7 天之间"}), 400
        scheduled_at = (datetime.now() + timedelta(seconds=countdown_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    if schedule_mode != "now":
        auto_print = 1  # 预约本质上是无障碍自动打印

    # ---- 兼容旧格式：单文件字段转为新格式数组 ----
    if files_input is None:
        file_id = data.get("file_id", "")
        file_name = data.get("file", "")
        copies = data.get("copies", 1)

        # 回填文件名
        if file_id and not file_name:
            conn = get_db()
            row = conn.execute("SELECT original_name FROM files WHERE id = ?", (file_id,)).fetchone()
            conn.close()
            if row:
                file_name = row["original_name"]

        if not file_name:
            return jsonify({"success": False, "message": "请提供 file 或 file_id 字段"}), 400

        files_input = [{"file_id": file_id or "", "file": file_name, "copies": copies}]

    # ---- 校验 files 数组 ----
    if not files_input or not isinstance(files_input, list):
        return jsonify({"success": False, "message": "files 字段必须是非空数组"}), 400
    # S0-04（R6）：数组规模与元素形态边界。1 MB JSON 能塞 5 万个元素 ⇒ 5 万次 INSERT
    # + 秒级写锁；正常业务最大单十几份，上限 20（分批/异步写是 S2-02 的事）。
    if len(files_input) > MAX_ORDER_FILES:
        return jsonify({"success": False,
                        "message": f"单个订单最多 {MAX_ORDER_FILES} 个文件"}), 400
    for _item in files_input:
        if not isinstance(_item, dict):
            return jsonify({"success": False, "message": "files 元素必须是对象"}), 400
        _pr = _item.get("page_range", "")
        if _pr not in (None, "") and not isinstance(_pr, str):
            return jsonify({"success": False, "message": "page_range 必须是字符串"}), 400
        if isinstance(_pr, str) and len(_pr) > MAX_PAGE_RANGE_LEN:
            return jsonify({"success": False, "message": "页码范围过长"}), 400
        _fid = _item.get("file_id", "")
        if _fid not in (None, "") and not isinstance(_fid, str):
            return jsonify({"success": False, "message": "file_id 不合法"}), 400

    # S2-08（D-6）：客户在下单页选的保留模式。新客户端用 retention_mode 区分
    # immediate/server，确保提交前从“立即删除”切回“遵从服务器管理”时 0 能真正覆盖上传值；
    # 旧客户端不带 mode 时，沿用“正数 retention_hours 才改写”的兼容语义。
    retention_mode = (data.get("retention_mode") or "").strip().lower()
    if retention_mode == "immediate":
        order_retention_hours = IMMEDIATE_DELETE_RETENTION_HOURS
        apply_order_retention = True
    elif retention_mode == "server":
        order_retention_hours = 0
        apply_order_retention = True
    else:
        order_retention_hours = _clamp_retention_hours(data.get("retention_hours"))
        apply_order_retention = order_retention_hours > 0

    user_is_admin = (g.user_role == "admin")
    # v24.1：管理员自行打印标记（仅管理员可设置；非管理员一律强制 0，防止伪造）
    admin_print = 1 if (user_is_admin and data.get("is_admin_print")) else 0
    owner_name = ""
    if admin_print:
        owner_name = (data.get("owner_name") or "").strip()
        if not owner_name:
            # 默认归属 = 提交者昵称（users.nickname），与收支清算成员名单对齐
            try:
                _uconn = get_user_db()
                _urow = _uconn.execute(
                    "SELECT nickname FROM users WHERE openid = ?", (g.openid,)
                ).fetchone()
                _uconn.close()
                if _urow and _urow["nickname"]:
                    owner_name = str(_urow["nickname"]).strip()
            except Exception:
                pass
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ---- 幂等去重（P0-2）：同用户同 client_request_id 的订单 10 分钟内返回原单 ----
    # 原单已取消则不拦（用户可能在取消后重新提交）。
    if client_request_id:
        dup_row = _find_order_by_client_request_id(g.openid, client_request_id)
        if dup_row and dup_row["status"] != "canceled":
            return _duplicate_order_response(dup_row, data)
        if dup_row:
            # 原单已取消 → 用户是在取消后重新提交：**换一把新幂等键**，免得与那条已取消的
            # 记录撞 (openid, client_request_id) 唯一索引（idx_orders_client_req）。
            client_request_id = f"{client_request_id}-r{secrets.token_hex(4)}"

    order_number = generate_order_number()

    # ---- 目标设备（多设备接单）与打印机状态 ----
    # 2026-12：移动端提交时选择「可接单设备」作为目标；未指定/目标未启用接单 → 回退为任一接单设备接收
    target_client_id = (data.get("target_client_id") or "").strip()
    if target_client_id and not is_claiming(target_client_id):
        target_client_id = ""
    printer_online = is_printer_available()  # 有启用接单的设备（含离线：离线设备被指定时任务排队等其上线）

    # ---- 阶段 A：只读预计算（S2-02，在开写事务之前）----
    # 原来「父单 INSERT → 逐文件 SELECT + get_file_page_count(读 PDF) + 计费 + INSERT」
    # 全在同一个 SQLite 写事务里，写者锁是全站唯一的 ⇒ 文件解析的磁盘 IO 会变成所有写路径的排队。
    # 现在页数/单价/状态全部在写事务**之前**用只读连接算完；失败直接 400，一行都不写。
    pre_conn = get_db()
    try:
        file_plan = _plan_order_files(pre_conn, files_input, g.openid, duplex,
                                      schedule_mode, _load_paper_prices())
    except OrderRejected as e:
        return jsonify({"success": False, "message": e.message}), 400
    finally:
        pre_conn.close()

    # ---- 事务：插 1 条 orders + N 条 order_files（只做 INSERT/UPDATE，无读盘）----
    conn = get_db()
    user_conn = get_user_db()
    try:
        # 先插父订单（聚合字段用首文件填充，后续严格通过 order_files 聚合）
        first_file_name = files_input[0].get("file", files_input[0].get("file_name", ""))
        # D5：父订单插入加幂等兜底。要在 while 里重试，是因为「换钥匙重插」那条支路
        # 需要重来一次（只重试父订单这一条 INSERT，此刻还没写任何 order_files）。
        _idem_attempt = 0
        while True:
            _idem_attempt += 1
            try:
                conn.execute(
                    """INSERT INTO orders (file_id, file, copies, status, created_at, openid, duplex,
                                           page_count, price_per_page, total_price, is_free,
                                           delivery_enabled, delivery_location, delivery_percentage,
                                           urgency, urgency_price, cover_page, cover_page_price, pickup_address,
                                           order_number, source, auto_print,
                                           schedule_mode, scheduled_at, schedule_frozen,
                                           client_request_id, owner_name, is_admin_print, remark, target_client)
                       VALUES (?, ?, ?, 'queued', ?, ?, ?, 1, 0, 0, ?,
                               ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)""",
                    (files_input[0].get("file_id") or None, first_file_name, 0,
                     created_at, g.openid, duplex,
                     0,  # is_free 恒为 0 — 价格仅用于统计
                     delivery_enabled, delivery_location, delivery_percentage,
                     urgency, urgency_price, cover_page, cover_page_price, pickup_address,
                     order_number, client, 1 if auto_print else 0,
                     schedule_mode, scheduled_at,
                     client_request_id, owner_name, admin_print, remark, target_client_id),
                    # is_free 恒为 0 — 价格仅用于统计，无免费策略
                )
                break
            except sqlite3.IntegrityError as _ie:
                # 并发/复用同一把钥匙：唯一索引把后来者挡在这里。三种情形分开处理 ——
                #  ① 真·并发双击 / 弱网重试（**10 分钟窗口内**同一把钥匙）→ 返回原单，
                #     不重复印、不重复计费（这一支正是本条修复要堵的洞）；
                #  ② 原单已取消、或**窗口外**复用同一把钥匙（老语义本来就允许新建一单）
                #     → **换一把新钥匙重插**，保住老语义；
                #  ③ 其余完整性错误（外键等）原样抛出。
                if "client_request_id" not in str(_ie) and "idx_orders_client_req" not in str(_ie):
                    raise
                # 父订单插入失败即回滚（此刻还没插任何 order_files，重试是干净的）
                conn.rollback()
                _fresh = _find_order_by_client_request_id(g.openid, client_request_id)
                if _fresh and _fresh["status"] != "canceled":
                    conn.close()
                    user_conn.close()
                    return _duplicate_order_response(_fresh, data)
                if _idem_attempt >= 3:
                    raise
                client_request_id = f"{client_request_id}-r{secrets.token_hex(4)}"
        order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # 插每个子任务（S2-02：本段只做 INSERT/UPDATE —— 页数/单价已在写事务之外算好，
        # 见 _plan_order_files 的注释；这里绝不允许再出现读盘/PDF 解析）
        sub_tasks = []
        for item in file_plan:
            if item["persist_pages"] is not None:
                conn.execute(
                    "UPDATE files SET page_count = ?, page_count_verified = ? WHERE id = ?",
                    (item["page_count"], int(item["page_count_verified"]), item["file_id"]),
                )
            # 展示名自愈（2026-10-25）：临时名 → 本次提交的真实名。纯 UPDATE，不读盘，
            # 符合 S2-02 对写事务的约束；归属校验与 _plan_order_files 同一口径（空 openid = 旧数据）。
            if item.get("persist_original_name"):
                conn.execute(
                    "UPDATE files SET original_name = ? WHERE id = ? "
                    "AND (openid = ? OR openid IS NULL OR openid = '')",
                    (item["persist_original_name"], item["file_id"], g.openid),
                )
            conn.execute(
                """INSERT INTO order_files (order_id, file_id, file_name, copies, page_count,
                                            page_range, price_per_page, total_price, is_free, status, created_at, duplex, reject_reason, image_orientation,
                                            simplex_pages, blank_tail_exclude)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, item["file_id"], item["file_name"], item["copies"], item["page_count"],
                 item["page_range"], 0, item["total_price"], 0, item["status"], created_at,
                 item["duplex"], item["reject_reason"], item["image_orientation"],
                 item.get("simplex_pages", ""), item.get("blank_tail_exclude", 0)),
            )
            sub_task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            sub_tasks.append({
                "id": sub_task_id, "file_id": item["file_id"] or "",
                "file_name": item["file_name"],
                "copies": item["copies"], "page_count": item["page_count"],
                "page_count_verified": item["page_count_verified"],
                "page_range": item["page_range"],
                "total_price": item["total_price"], "status": item["status"],
                "duplex": item["duplex"],
                "image_orientation": item["image_orientation"],
                "simplex_pages": item.get("simplex_pages", ""),
                "blank_tail_exclude": item.get("blank_tail_exclude", 0),
                "reject_reason": item["reject_reason"],
            })

        # S2-08（D-6）：本单文件的保留期以提交时的选择定稿（文件是先上传、后选保留期的）。
        # 只 UPDATE（不读盘，符合 S2-02 对写事务的约束）；只动自己名下的文件。
        if apply_order_retention:
            _ret_ids = [it["file_id"] for it in file_plan if it.get("file_id")]
            if _ret_ids:
                _rph = ",".join("?" for _ in _ret_ids)
                conn.execute(
                    f"UPDATE files SET retention_hours = ? WHERE openid = ? AND id IN ({_rph})",
                    [order_retention_hours, g.openid] + _ret_ids,
                )

        # 汇总父订单 total_price 和 page_count
        parent_base_price = sum(st["total_price"] for st in sub_tasks)
        parent_page_count = sum(st["page_count"] * st["copies"] for st in sub_tasks)
        # 计算最终价格 = 基础打印费 + 附加服务费
        urgency_fee = urgency_price if urgency_price else 0
        cover_fee = round(cover_page_price * cover_page, 2) if cover_page else 0
        delivery_fee = round(parent_base_price * delivery_percentage / 100, 2) if delivery_percentage else 0
        parent_total_price = round(parent_base_price + urgency_fee + cover_fee + delivery_fee, 2)

        conn.execute(
            "UPDATE orders SET total_price = ?, page_count = ? WHERE id = ?",
            (parent_total_price, parent_page_count, order_id),
        )

        # 聚合初态并写入父订单
        new_status = aggregate_order_status(conn, order_id) or "queued"
        conn.execute("UPDATE orders SET status = ? WHERE id = ?", (new_status, order_id))

        # P2-4：临时授权原子消费（原"先查 temp_until 再清空"非原子，并发提交可重复消费）。
        # 改为条件 UPDATE（temp_until 仍存在且未过期才生效），影响行数为 0 → 拒绝本单并回滚；
        # 包在 db_lock 内，防止与兑换/其他提交交错。
        if not user_is_admin:
            urow = user_conn.execute(
                "SELECT temp_until FROM users WHERE openid = ?", (g.openid,),
            ).fetchone()
            if urow and urow["temp_until"]:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with db_lock:
                    cur2 = user_conn.execute(
                        "UPDATE users SET temp_until = NULL, updated_at = ? WHERE openid = ? AND temp_until IS NOT NULL AND temp_until > ?",
                        (now_str, g.openid, now_str),
                    )
                if cur2.rowcount == 0:
                    raise OrderRejected("临时授权已失效或被其他订单使用，请重新出示许可")
                # P1: 标准 SQLite 构建未开 SQLITE_ENABLE_UPDATE_DELETE_LIMIT，
                # `UPDATE ... ORDER BY ... LIMIT` 直接 OperationalError（临时授权用户下单必 500）。
                # 改为子查询定位唯一目标行，语义等价且全平台可用。
                cur3 = user_conn.execute(
                    "UPDATE license_keys SET order_id = ? WHERE id = ("
                    "  SELECT id FROM license_keys"
                    "  WHERE used_by = ? AND order_id IS NULL"
                    "  ORDER BY id DESC LIMIT 1)",
                    (order_id, g.openid),
                )
                if cur3.rowcount == 0:
                    # 授权已被消费但找不到对应密钥记录（历史数据）：不阻断下单，仅记录
                    app.logger.warning(
                        f"临时授权已消费但未匹配到 license_keys 记录: openid={g.openid}, order_id={order_id}")
                user_conn.commit()

        conn.commit()
    except OrderRejected as e:
        conn.rollback()
        try:
            user_conn.rollback()
        except Exception:
            pass
        conn.close()
        user_conn.close()
        return jsonify({"success": False, "message": e.message}), 400
    except Exception:
        conn.rollback()
        try:
            user_conn.rollback()
        except Exception:
            pass
        conn.close()
        user_conn.close()
        raise

    # ---- 推送（事务外，避免长事务） ----
    # 2026-12：多设备接单 —— 指定了目标设备则只发往该设备（离线则保持 queued，等其上线后由
    # process_pending_orders 推送）；未指定目标 → 任一在线接单设备。
    pushed_count = 0
    if target_client_id:
        client_id = target_client_id if target_client_id in get_active_clients() else None
    else:
        client_id = get_active_printer_client()
    if client_id:
        for st in sub_tasks:
            if st["file_id"]:
                if schedule_mode != "now":
                    # 预约单：阶段①先下发文件（downloading + 预约时间），本地到点再自动打印。
                    # 推送失败则保持 scheduled，等 process_scheduled_orders 扫描重试。
                    if push_print_task_to_client(st["id"], st["file_id"], st["file_name"],
                                                  st["copies"], st.get("duplex", duplex),
                                                  st.get("page_range", ""), client_id,
                                                  auto_print=True,
                                                  scheduled_download=True,
                                                  schedule_mode=schedule_mode,
                                                  scheduled_at=scheduled_at,
                                                  image_orientation=st.get("image_orientation", "auto"),
                                                  simplex_pages=st.get("simplex_pages", ""),
                                                  blank_tail_exclude=st.get("blank_tail_exclude", 0)):
                        pushed_count += 1
                    continue
                if push_print_task_to_client(st["id"], st["file_id"], st["file_name"],
                                              st["copies"], st.get("duplex", duplex),
                                              st.get("page_range", ""), client_id,
                                              auto_print=bool(auto_print),
                                              image_orientation=st.get("image_orientation", "auto"),
                                              simplex_pages=st.get("simplex_pages", ""),
                                              blank_tail_exclude=st.get("blank_tail_exclude", 0)):
                    pushed_count += 1
                # 🔴-11：此处原有 `UPDATE order_files SET status='queued' WHERE id=?` 的失败
                # 回滚，已删除 —— push_print_task_to_client 对每种失败路径都自带**带状态守卫**
                # 的回滚（只改 printing/downloading 的行），这里属于重复且有害的第二次回滚：
                #  · 任务刚被判 rejected（页数越界）→ 被改回 queued ⇒ 30 秒后照常推送 ⇒
                #    越界文件被真实打印，顾客在订单详情看到「排队中」而不是「被打回」；
                #  · 任务已被**另一条推送路径**（process_pending_orders / 打印机上线重推）
                #    领取为 printing → 被改回 queued ⇒ 重复打印。本段推送在事务外，与那些
                #    路径存在真实并发窗口（该函数正是靠 `AND status='queued'` 的 rowcount
                #    为 0 来识别这种竞争的）。
                # 失败原因里唯一"状态未变"的情形（rowcount==0 已非 queued / file_id 为空）
                # 本就不需要回滚。响应体的聚合状态取自 orders 表（下方 final_status）。

    conn.close()

    # 重新读取最终聚合状态
    conn = get_db()
    final_status = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    final_status = final_status["status"] if final_status else "queued"

    print(f"收到任务 (order_id={order_id}): {len(sub_tasks)} 个文件, "
          f"status={final_status}, pushed={pushed_count}/{len(sub_tasks)}, "
          f"openid={g.openid[:8]}...")
    # P1-3.2：不再全量回显请求体（含派送地址等隐私），只打印关键摘要
    print(f"  [SUBMIT] openid={g.openid[:8]}..., files={len(sub_tasks)}, "
          f"delivery_enabled={delivery_enabled}, delivery_location={delivery_location!r}, "
          f"urgency={urgency!r}, cover_page={cover_page}, "
          f"schedule_mode={schedule_mode}, scheduled_at={scheduled_at}, "
          f"is_admin_print={admin_print}, owner_name={owner_name!r}")

    # S2-02：响应体保护 —— 子任务列表超过 MAX_ECHO_FILES 条时只回前 N 条 + 汇总
    # （现在入口上限是 MAX_ORDER_FILES=20，正常永远不触发；留着是为了将来放宽上限时
    #  不会把一个 MB 级的列表一次回显出去）。
    # 同时**不再回显请求体 data**：那是客户端原样上传的 JSON（可带任意垃圾键），
    # 回显等于把请求体再原样发回去一倍，属于纯粹的放大面（客户端两侧都不读这个字段）。
    resp_files = sub_tasks[:MAX_ECHO_FILES]
    payload = {
        "success": True,
        "message": "任务已接收" + ("，已推送打印" if pushed_count > 0 else "，排队等待打印"),
        "order_id": order_id,
        "order_number": order_number,
        "status": final_status,
        "files": resp_files,
        "files_total": len(sub_tasks),
        "pushed_count": pushed_count,
    }
    if len(sub_tasks) > MAX_ECHO_FILES:
        payload["files_omitted"] = len(sub_tasks) - MAX_ECHO_FILES
    return jsonify(payload)



@app.route("/api/pull_queued_orders", methods=["GET"])
def pull_queued_orders():
    """打印机客户端拉取排队中的子任务（原子取锁，每次返回一个任务，防止多打印机重复领取）
    2026-11：仅「启用接单」的设备可领取；未启用/无接管者时一律返回空，杜绝多设备同时接收。"""
    token = _get_printer_token()
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403

    client_id = request.args.get("client_id", "") or socket.gethostname()
    # 登记设备（HTTP 拉取路径的连接也纳入设备注册表，供收支清算「授权」页展示）
    # 能力上报同 socket 连接：HTTP 心跳顺带刷新打印机信息（老客户端不带 → None，不改动旧记录）
    register_device(client_id, request.args.get("device_name", "") or client_id,
                    printer=_printer_probe_from_request())
    # HTTP 通道心跳：socket 掉线但仍能 HTTP 拉单时保持「在线」（否则下面 get_active_printer_client
    # 判本机离线 → 永远返回空，双通道实为单通道）。必须在 get_active_printer_client() 之前刷新。
    touch_printer_heartbeat(client_id)

    # 2026-10（D5 修复）：原判据是「client_id == get_active_printer_client()」，而后者只返回
    # **第一台**在线接单设备 ⇒ 多台接单设备时其余设备**永远** take_orders=False：第二台在
    # socket 掉线、靠 HTTP 兜底拉单时拉不到**指定给自己**的任务（"双通道独立保活"要兜的
    # 场景被废掉）。正确判据是「本机是**启用接单**的设备」—— 本机在线由上面
    # touch_printer_heartbeat() 保证；"能拿到哪一个任务"由 fetch_and_lock_task 内的
    # target_client 过滤决定（本机为目标 或 未指定目标且本机接单），因此绝不会抢别人的单。
    if not is_claiming(client_id):
        return jsonify({"success": True, "orders": [], "count": 0,
                        "take_orders": False,
                        "active_client_id": get_active_printer_client() or ""})

    task = fetch_and_lock_task(client_id)
    if not task:
        return jsonify({"success": True, "orders": [], "count": 0, "take_orders": True})

    # 每文件 duplex 已存入 order_files 表，直接从 task 读取
    duplex = task.get("duplex", "on") or "on"
    image_orientation = task.get("image_orientation", "auto") or "auto"

    # 构建与旧格式兼容的响应体（单元素数组）
    item = {
        "id": task["id"],
        "order_id": task["order_id"],
        "order_number": task.get("order_number", ""),
        "file_id": task["file_id"],
        "file": task["file_name"],           # 兼容旧客户端字段名
        "file_name": task["file_name"],
        "source_md5": task.get("source_md5", "") or "",
        "copies": task["copies"],
        "page_range": task.get("page_range", "") or "",
        "status": task["status"],
        "created_at": task["created_at"],
        "duplex": duplex,
        "image_orientation": image_orientation,
        "task_id": task["id"],               # 客户端用 task_id = order_files.id
        "options": {
            "copies": task["copies"],
            "duplex": duplex,
            "page_range": task.get("page_range", "") or "",
            "image_orientation": image_orientation,
        },
        "delivery_enabled": bool(task.get("delivery_enabled", False)),
        "delivery_location": task.get("delivery_location", "") or "",
        "urgency": task.get("urgency", "低") or "低",
        "cover_page": bool(task.get("cover_page", False)),
        "cover_page_price": float(task.get("cover_page_price", 0.10) or 0.10),
        "remark": task.get("remark", "") or "",
        "auto_print": bool(task.get("auto_print", False)),
        "owner_name": task.get("owner_name", "") or "",
        "is_admin_print": bool(task.get("is_admin_print", False)),
        # 2026-10-26（隐式规则）：文件上传者是否管理员（与 push 通道同口径；旧客户端忽略该字段）
        "uploader_is_admin": is_admin_uploader(task.get("openid") or ""),
        # 2026-12：顾客订单标签页归属 = 下单用户绑定的成员名（与 push 通道一致）
        "bound_owner_name": _get_bound_owner_name(task.get("openid") or ""),
        # 订单来源（wechat/app/local），本地工具云端任务列表展示
        "source": task.get("source", "") or "wechat",
        # S1-03：该文件的保留模式（-1 立即删除 / 0 服务器管理 / 正数旧客户端）
        "retention_hours": int(task.get("retention_hours") or 0),
    }
    if task["file_id"]:
        # S1-06：绑定 openid + 子任务 id
        item["download_url"] = make_download_url(
            task["file_id"], openid=(task.get("openid") or ""), task_id=task.get("id"))

    return jsonify({
        "success": True,
        "orders": [item],
        "count": 1,
        "take_orders": True,   # 与「无任务/未接单」两支对齐（客户端不读，仅接口一致性）
    })


# ==================== 接单接管 / 设备授权（2026-11） ====================
# 多设备共连服务器时的唯一接管者机制：
#   · 仅「启用接单」的设备可接收订单（push / pull 均已门控到 active printer）；
#   · 启用接单时若已有其他在线设备接管 → 拒绝并告知接管者（计算机名 + 所有者）；
#   · 设备所有者绑定（计算机名 → 成员姓名）在收支清算「授权」页维护。


@app.route("/api/printer/claim", methods=["POST"])
def printer_claim():
    """启用接单（加入可接单设备集合，多设备可同时启用）。
    2026-12 重构：不再是「唯一接管者」独占，勾选接单的设备都会进入可接单设备列表，
    移动端提交订单时选择目标设备；任务只会发往目标设备（未指定目标时取任一在线接单设备）。"""
    token = _get_printer_token()
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or "").strip()
    if not client_id or len(client_id) > 64:
        return jsonify({"success": False, "message": "client_id 无效"}), 400

    device_name = (data.get("device_name") or "").strip()[:64]
    register_device(client_id, device_name)

    with _claim_lock:
        claim = _load_claim_impl()
        devices = claim.get("claiming_devices")
        if not isinstance(devices, dict):
            devices = {}
        devices[client_id] = {
            "claimed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "owner_name": get_device_owner(client_id),  # 以绑定所有者为准
        }
        claim["claiming_devices"] = devices
        _save_claim_impl(claim)
    broadcast_printer_state()
    print(f"[CLAIM] 设备 {client_id} 已启用接单（当前共 {len(devices)} 台可接单设备）")
    return jsonify({"success": True, "message": "接单已启用", "active": True,
                    "claiming_count": len(devices)})


@app.route("/api/printer/release", methods=["POST"])
def printer_release():
    """关闭接单（从可接单设备集合移除本机）。"""
    token = _get_printer_token()
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or "").strip()
    with _claim_lock:
        claim = _load_claim_impl()
        devices = claim.get("claiming_devices")
        if isinstance(devices, dict) and client_id in devices:
            devices.pop(client_id, None)
            claim["claiming_devices"] = devices
            _save_claim_impl(claim)
            broadcast_printer_state()
            print(f"[CLAIM] 设备 {client_id} 已关闭接单（剩余 {len(devices)} 台可接单设备）")
            return jsonify({"success": True, "message": "接单已关闭"})
    return jsonify({"success": True, "message": "当前设备未启用接单"})


@app.route("/api/printer/token/create", methods=["POST"])
@require_printer_scope("admin")
def printer_token_create():
    """为设备签发/轮换 printer token（S1-01）。

    body: {"client_id": "...", "label": "门店机", "scopes": ["print","admin"]}
    response: {"success": true, "token": "<明文，只此一次>", "scopes": [...]}

    引导：新设备还没有自己的 token 时，用全局 TOKEN（迁移窗口，具备 admin scope）调一次；
    拿到本机 token 后应立刻写进 print_config.json 并改用它。
    """
    data = _json_dict() or {}
    client_id = (data.get("client_id") or "").strip()
    if not client_id or len(client_id) > 64:
        return jsonify({"success": False, "message": "client_id 无效"}), 400
    label = (data.get("label") or "").strip()
    scopes = data.get("scopes")
    if scopes is not None and not isinstance(scopes, list):
        return jsonify({"success": False, "message": "scopes 必须是数组"}), 400
    token, scopes = issue_device_token(client_id, label=label, scopes=scopes)
    return jsonify({
        "success": True,
        "client_id": client_id,
        "token": token,
        "scopes": scopes,
        "message": "请立刻保存：明文只返回这一次",
    })


@app.route("/api/printer/token/revoke", methods=["POST"])
@require_printer_scope("admin")
def printer_token_revoke():
    """吊销某台设备的 token（S1-01）。吊销后该设备的请求立即 403（全局 TOKEN 仍有效）。"""
    data = _json_dict() or {}
    client_id = (data.get("client_id") or "").strip()
    if not client_id:
        return jsonify({"success": False, "message": "缺少 client_id"}), 400
    if not revoke_device_token(client_id):
        return jsonify({"success": False, "message": "该设备没有已签发的 token"}), 404
    return jsonify({"success": True, "client_id": client_id, "message": "已吊销"})


@app.route("/api/printer/devices", methods=["GET"])
def printer_devices():
    """返回连接过云服务器的全部设备：计算机名称、在线状态、所有者、是否接单、自动双面能力。
    供收支清算「授权」页展示（打印机 token 鉴权，本地打印工具经 stats_server 代理调用）。"""
    token = _get_printer_token()
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403

    online_set = set(get_active_clients())
    devices = load_devices()
    result = []
    hidden_test = 0
    for cid, entry in devices.items():
        if is_test_device_id(cid):
            # 测试/联调设备默认不展示（否则授权页一屏全是 debug-*），只回计数供页面提示
            hidden_test += 1
            continue
        result.append({
            "client_id": cid,
            "device_name": entry.get("device_name", "") or cid,
            "online": cid in online_set,
            "owner_name": entry.get("owner_name", "") or "",
            "first_seen": entry.get("first_seen", ""),
            "last_seen": entry.get("last_seen", ""),
            "is_active": is_claiming(cid),
            # 攒批「离线期望」是否还没落地（2026-11）：true = 有人趁着离线点过开关，
            # 设备上线后会自动应用；移动端据此显示"已记录，上线后自动应用"
            "stack_pending": _stack_intent_pending(cid),
            # 期望值本身（true/false/null）：与 stack_pending 配对，前端据此区分
            # 「期望开启但未落地」与「期望关闭但未落地」（2026-11）
            "stack_desired": _stack_desired_of(cid),
            # 自动双面能力（三态；授权页据此标注"需人工翻纸"）
            **_device_capability_fields(cid),
        })
    # 在线设备排前，其余按计算机名排序
    result.sort(key=lambda d: (not d["online"], d["device_name"].lower()))
    return jsonify({
        "success": True,
        "devices": result,
        "claiming_count": len(get_claiming_device_ids()),
        # 被隐藏的测试设备数（页面据此提示「已隐藏 N 台测试设备」+ 一键清理）
        "hidden_test_devices": hidden_test,
    })


@app.route("/api/printer/stack_mode", methods=["POST"])
@login_required
def printer_stack_mode():
    """移动端远程切换某台设备的「🖐 攒批模式」（2026-10，仅管理员/超管）。

    body: {"client_id": "...", "enable": true|false}
    前提（与本地工具侧同口径，缺一不可）：
      · 该设备**确实需要攒批**（不支持自动双面、双面需人工翻纸）—— 自动双面 / 能力未确认的
        设备在移动端根本不渲染这个开关，服务端这里再拦一次（见 _stack_needs_manual_flip）；
      · 开启：该设备当前**未开**攒批 + **空闲**；
      · 关闭：该设备当前**已开**攒批 + **空闲**；
      · 忙（正在打印 / 等待人工翻纸打第二趟 / 有排队待打的请求）时一律拦下并给原因。
    实现：服务端先按镜像状态校验 → emit `set_stack_mode` 给该设备 → 等设备端回报（≤3s）
    → 用回报后的最新状态回复。设备端收到指令后会**再自查一次**（防两端状态竞态），
    最终「以设备端为准」。
    离线（2026-11）：不再拒绝 —— 记录「期望」（uploads/stack_intent.json，重启不丢），
    返回 {"success": true, "pending": true, "message": "设备离线，已记录：上线后自动应用"}；
    设备上线第一次上报 stack_state 时与期望比对，不一致就下发 `set_stack_mode`
    （带 `"reason": "offline_intent"`），设备忙/拒绝则保留期望、下次上线再试。
    ⚠ 离线路径**不校验忙闲**（状态未知），但仍要求该机型需要攒批；离线时也**不**下发任何指令。
    一律 HTTP 200，靠 body 的 success 判断（前端只读 success / message / stack_mode / stack_busy，
    离线时多读一个 pending）。
    """
    role = compute_role(getattr(g, "openid", "") or "")
    if role not in ("admin", "super_admin"):
        return jsonify({"success": False, "message": "仅管理员可以切换攒批模式"})
    data = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or "").strip()[:64]
    enable = _as_bool_or_none(data.get("enable"))
    if not client_id or enable is None:
        return jsonify({"success": False, "message": "参数不完整（需要 client_id 与 enable）"})

    # 设备是否在线（心跳表）。离线不再直接拒绝 —— 见下面的期望记录分支。
    with printer_clients_lock:
        info = printer_clients.get(client_id)
    sid = info.get("sid") if info else None
    hb = info.get("heartbeat") if info else None
    online = bool(sid) and hb is not None and \
        (datetime.now() - hb).total_seconds() <= CLIENT_HEARTBEAT_TIMEOUT

    # 设备离线（2026-11）：不再拒绝，改成记录「期望」——设备上线第一次上报 stack_state 时
    # 由 _apply_stack_intent_on_online 比对并下发（reason=offline_intent）。
    # ⚠ 离线路径**不校验忙闲**（设备状态未知，判不出来，用户口径就是"先记下来"）；
    #   唯一保留的闸门是"该机型是否需要攒批"——自动双面/能力未确认的设备不留无意义的期望。
    if not online:
        st = _stack_state_of(client_id)
        if not _stack_needs_manual_flip(client_id):
            print(f"[stack_intent] {client_id} 离线期望被拒：该机型不需要攒批")
            return jsonify({"success": False,
                            "message": _stack_toggle_problem(client_id, enable),
                            "stack_mode": st["mode"], "stack_busy": st["busy"]})
        by = operator_display_name(getattr(g, "openid", "")) or role
        saved = _set_stack_intent(client_id, enable, by=by, by_role=role)
        print(f"[stack_intent] {client_id} 设备离线 → 期望已记录"
              f"（{'开启' if enable else '关闭'}，by={by}，落盘={'成功' if saved else '失败'}）"
              f"，上线后自动应用")
        return jsonify({"success": True, "pending": True,
                        "message": "设备离线，已记录：上线后自动应用",
                        "stack_mode": st["mode"], "stack_busy": st["busy"]})

    problem = _stack_toggle_problem(client_id, enable)
    if problem:
        st = _stack_state_of(client_id)
        return jsonify({"success": False, "message": problem,
                        "stack_mode": st["mode"], "stack_busy": st["busy"]})

    # 下发指令并等设备端回报（设备端会再自查一次，最终状态以它为准）
    ev = threading.Event()
    with printer_stack_state_lock:
        printer_stack_waiters[client_id] = ev
        printer_stack_results.pop(client_id, None)
    try:
        # by = **操作者的昵称**（不是角色名）：设备端日志/窗口直接显示"操作者：张三"。
        # by_role 另带一份角色，便于排查权限问题（旧客户端只读 by，多一个字段无副作用）。
        socketio.emit("set_stack_mode",
                      {"enable": bool(enable),
                       "by": operator_display_name(getattr(g, "openid", "")) or role,
                       "by_role": role,
                       "request_id": uuid.uuid4().hex[:8]},
                      to=sid)
    except Exception as e:
        with printer_stack_state_lock:
            printer_stack_waiters.pop(client_id, None)
        st = _stack_state_of(client_id)
        return jsonify({"success": False, "message": f"下发失败：{e}",
                        "stack_mode": st["mode"], "stack_busy": st["busy"]})

    got = ev.wait(STACK_TOGGLE_WAIT_SEC)
    with printer_stack_state_lock:
        result = printer_stack_results.pop(client_id, None) or {}
        printer_stack_waiters.pop(client_id, None)
    st = _stack_state_of(client_id)
    if not got:
        return jsonify({"success": False, "message": "设备端未在 3 秒内回应，请稍后重试或刷新",
                        "stack_mode": st["mode"], "stack_busy": st["busy"]})
    ok = result.get("mode") is bool(enable)
    msg = result.get("note") or ("攒批模式已开启" if enable else "攒批模式已关闭")
    if not ok and not result.get("note"):
        msg = "设备端拒绝了本次切换"
    if ok:
        # 在线指令已生效 ⇒ 之前那条离线期望没有存在意义了（否则下次上报还会被拿出来比对）
        _clear_stack_intent(client_id, "在线指令已生效，清除离线期望")
    return jsonify({"success": bool(ok), "message": msg,
                    "stack_mode": st["mode"], "stack_busy": st["busy"]})


@app.route("/api/printer/capability", methods=["POST"])
def printer_capability():
    """写入设备的「自动双面」能力（printer token 鉴权）。

    body: {"client_id": "...", "auto_duplex": true|false|null,
           "source": "manual"(默认) | "probe",
           "printer_name"/"printer_driver"/"printer_port": 可选描述信息,
           "by": 可选（人工确认者，如成员姓名）}

    · source=manual —— 人工盖章（授权页「自动双面」下拉）。优先于自动探测，且只有再人工改
      才会变；auto_duplex=null 表示清除人工结论、回到自动探测结果。
    · source=probe —— 本地打印工具探测上报，只写探测字段，不覆盖人工结论。

    下游消费（2026-10 起）：
      · 能力随 `/api/claiming_devices`、`/api/printer/devices`、`printer_state` 下发给移动端与本地
        工具，设备列表据此标「🖐 需人工翻纸」（仅提示）、本地工具据此调整双面 UI 与提示；
      · **不再用它拦截下单**（2026-10 晚些时候按要求放开）：原「无障碍/预约 × 双面 × 该设备」的
        移动端三处入口 + 提交兜底 + 服务端 409 已全部移除，提交一律放行；订单是否真能无人值守
        完成由设备端自己负责（本地工具仍会在无人值守遇双面时挂起等人翻纸）。
    """
    token = _get_printer_token()
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403

    data = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or "").strip()[:64]
    if not client_id:
        return jsonify({"success": False, "message": "client_id 不能为空"}), 400
    if client_id not in load_devices():
        return jsonify({"success": False,
                        "message": "设备未登记（该设备需先连接过云服务器）"}), 404

    source = (data.get("source") or "manual").strip().lower()
    if source not in ("manual", "probe"):
        return jsonify({"success": False, "message": "source 只能是 manual 或 probe"}), 400

    if source == "probe":
        probe = {}
        if "auto_duplex" in data:
            probe["auto_duplex"] = data.get("auto_duplex")
        for key, field in (("queue", "printer_name"), ("driver", "printer_driver"),
                           ("port", "printer_port")):
            val = str(data.get(field) or "").strip()
            if val:
                probe[key] = val
        if not probe:
            return jsonify({"success": False, "message": "没有任何可写入的探测字段"}), 400
        ok = set_device_probe(client_id, probe)
    else:
        ok = set_device_manual_capability(client_id, data.get("auto_duplex"),
                                          by=data.get("by") or "")
    if not ok:
        return jsonify({"success": False, "message": "设备不存在或写入失败"}), 404

    cap = get_device_capability(client_id)
    label = {True: "支持自动双面", False: "不支持（需人工翻纸）", None: "未确认"}[cap["auto_duplex"]]
    print(f"[DEV] 设备 {client_id} 自动双面能力已更新（{source}）: {label}"
          + (f" @ {cap['printer_name']}" if cap["printer_name"] else ""))
    # 让本地工具（printer_state）与其它在线设备立刻看到新能力
    broadcast_printer_state()
    return jsonify({"success": True, "capability": cap})


@app.route("/api/claiming_devices", methods=["GET"])
def claiming_devices():
    """返回全部启用接单的设备列表（含在线状态；离线设备也允许被选择发送任务，任务排队等其上线）。
    供移动端任务发起界面选择目标设备（公开接口，与 printer_status 一致）。
    2026-10：附带 auto_duplex 三态能力 —— 移动端据此禁用「不支持自动双面」设备的无障碍打印
    （null=未确认，按安全侧处理），双面文件在该设备上需人工翻纸。
    2026-10（置顶顺序）：每台设备多带 pin（true/false）与 pin_index（0 基序位，未置顶为
    null），顶层多带 pin_order（本次**实际生效**的顺序，已过滤掉不存在的/不接单的 cid）。
    置顶的设备排在最前且按置顶顺序，**在线与否不影响它的位置** —— 移动端据此默认选中
    pin_index==0 的那台（离线也照样默认选中，任务排队等它上线）。"""
    online_set = set(get_active_clients())
    claiming = get_claiming_devices()
    # 置顶顺序（读盘 + 按当前设备集合过滤）。fail-open：读不出来就是空列表 = 老口径。
    pin_order = get_effective_pin_order()
    pin_index_of = {cid: i for i, cid in enumerate(pin_order)}
    result = []
    for cid, entry in claiming.items():
        dev = get_device_entry(cid)
        online = cid in online_set
        st = _stack_state_of(cid)
        # 攒批模式（2026-10）：给移动端展示「🖐 攒批中 / 攒批未开 / 未知」+ 管理员远程开关。
        # stack_can_toggle 由服务端先算一遍（该机型需要攒批 + 在线 + 已上报 + 空闲 + 状态可反向），
        # 移动端据此置灰/提示；真正切换时 /api/printer/stack_mode 还会再校验一次（并让设备端自查）。
        # ⚠ 攒批只对「不支持自动双面、需人工翻纸」的机型有意义：auto_duplex 为 true/未确认的设备
        #    在移动端**整个攒批区块都不渲染**（两边同一条判据：auto_duplex === false）。
        result.append({
            "client_id": cid,
            "device_name": dev.get("device_name", "") or cid,
            # S2-04：公开接口 —— 姓名脱敏（真名只在 /api/printer/devices 的管理 scope 里）
            "owner_name": _mask_person_name(dev.get("owner_name", "") or ""),
            "online": online,
            "label": device_display_label(cid, mask=True),
            # 自动双面能力（三态；移动端无障碍打印的判定依据之一）
            **_device_capability_fields(cid),
            "stack_mode": st["mode"],
            "stack_busy": st["busy"],
            "stack_can_toggle": bool(online) and not _stack_toggle_problem(cid),
            "stack_reason": ("设备离线" if not online else _stack_toggle_problem(cid)),
            # 攒批「离线期望」是否还没落地（2026-11）：设备离线时点过开关 → true，
            # 上线自动应用后清掉。移动端据此把开关置灰并显示"已记录，上线后自动应用"
            "stack_pending": _stack_intent_pending(cid),
            # 期望值本身（true/false/null）：与 stack_pending 配对，前端据此区分
            # 「期望开启但未落地」与「期望关闭但未落地」（2026-11）
            "stack_desired": _stack_desired_of(cid),
            # 置顶（2026-10）：pin_index 是「在生效顺序里的 0 基序位」，未置顶为 null（不是 -1：
            # 前端一律用 pin_index === 0 判默认选中，null/缺字段都能被它排除掉）。
            "pin": cid in pin_index_of,
            "pin_index": pin_index_of.get(cid),
        })
    # 排序：① 置顶的在前、按置顶顺序（**离线不降级** —— 置顶就是"不管在不在线都优先"）；
    # ② 未置顶的沿用老口径「在线优先，其余按 client_id」。两组内部各自保持原口径。
    result.sort(key=lambda d: (
        d["pin_index"] is None,
        d["pin_index"] if d["pin_index"] is not None else 0,
        not d["online"],
        d["client_id"].lower(),
    ))
    return jsonify({"success": True, "devices": result, "pin_order": pin_order})


@app.route("/api/admin/device_pin_order", methods=["POST"])
@login_required
def admin_device_pin_order():
    """设置「接单设备置顶顺序」（2026-10；仅 admin / super_admin）。

    body: {"order": [client_id, ...]} —— 第一项就是移动端默认选中的设备。
    服务端归一化（顺序固定）：去重保序 → 丢弃不在「启用接单 ∩ 注册表存在」集合里的 cid
    （长度 > 64 的也丢） → 截断到最多 50 项；返回 {"success": true, "pin_order": [...]}。
    畸形请求体（非 dict / order 不是 list / 元素不是 str）一律 400，绝不 500 —— 这条接口
    一旦 500，置顶就会变成把整张设备选择页弄白的故障点。
    写盘失败：旧文件原样保留（原子写天然保证），返回 success:false 并带上当前生效顺序。
    """
    role = compute_role(getattr(g, "openid", "") or "")
    if role not in ("admin", "super_admin"):
        return jsonify({"success": False, "message": "需要管理员权限"}), 403

    try:
        data = request.get_json(silent=True)
    except RecursionError:
        # 深嵌套请求体会让 json.loads 抛 RecursionError（不是 ValueError，silent=True 挡不住）
        return jsonify({"success": False, "message": "请求体格式不合法"}), 400
    if not isinstance(data, dict):
        return jsonify({"success": False, "message": "请求体必须是 JSON 对象"}), 400
    raw = data.get("order")
    if not isinstance(raw, list):
        return jsonify({"success": False, "message": "order 必须是数组"}), 400
    if any(not isinstance(cid, str) for cid in raw):
        return jsonify({"success": False, "message": "order 里每一项都必须是字符串"}), 400

    order = sanitize_pin_order(raw, get_pin_valid_cids())
    if not save_device_pin_order(order, getattr(g, "openid", "") or ""):
        # 原子写失败 ⇒ 旧文件没动过；如实回报，别让前端以为改成功了
        return jsonify({"success": False, "message": "保存失败，置顶顺序未改变",
                        "pin_order": get_effective_pin_order()}), 500
    print(f"[DEV] 接单设备置顶顺序已更新（{role}）: {order}")
    return jsonify({"success": True, "pin_order": order})


@app.route("/api/printer/bind_owner", methods=["POST"])
def printer_bind_owner():
    """绑定设备所有者（计算机名 → 收支成员姓名）。"""
    token = _get_printer_token()
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or "").strip()
    if not client_id or len(client_id) > 64:
        return jsonify({"success": False, "message": "client_id 无效"}), 400
    owner_name = (data.get("owner_name") or "").strip()[:30]

    devices = load_devices()
    entry = devices.get(client_id) or {}
    entry["owner_name"] = owner_name
    devices[client_id] = entry
    save_devices(devices)
    # 该设备正是当前接管者 → 同步接单记录中的所有者名（claim 消息用）
    # P1: _load_claim_impl 始终归一化为 {"claiming_devices": {...}}，旧键 active_client_id
    # 永不存在（原先的判断恒 False = 死代码）。
    with _claim_lock:
        claim = _load_claim_impl()
        claiming = claim.get("claiming_devices")
        if isinstance(claiming, dict) and client_id in claiming:
            entry_claim = claiming.get(client_id)
            if not isinstance(entry_claim, dict):
                entry_claim = {}
            entry_claim["owner_name"] = owner_name
            claiming[client_id] = entry_claim
            _save_claim_impl(claim)
    # 2026-12：绑定后总是广播接单状态（含各设备自己的 owner_name），
    # 使本地工具无需重启即可立即用新所有者作为新建标签页的默认归属者
    broadcast_printer_state()
    print(f"[DEV] 设备 {client_id} 所有者绑定为「{owner_name or '未绑定'}」")
    return jsonify({"success": True, "message": "所有者已保存"})


@app.route("/api/printer/devices/delete", methods=["POST"])
@require_printer_scope("admin")
def printer_devices_delete():
    """删除设备注册表中的一台设备（清理历史遗留 / 重复 client_id，如软件升级后产生的新 ID）。
    若删除的是当前「接单」设备则同时清空接管记录；在线设备下次连接会自动重新登记。"""
    token = _get_printer_token()
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or "").strip()
    if not client_id or len(client_id) > 64:
        return jsonify({"success": False, "message": "client_id 无效"}), 400

    devices = load_devices()
    existed = client_id in devices
    devices.pop(client_id, None)
    save_devices(devices)

    cleared_claim = False
    # P1: 接管记录已归一化为 claiming_devices 字典，必须按 client_id 查该字典；
    # 原 `claim.get("active_client_id")` 恒为 None → 删设备永不清接管记录，
    # 被删设备留在接单列表里，移动端仍可选中导致任务永久排队。
    with _claim_lock:
        claim = _load_claim_impl()
        claiming = claim.get("claiming_devices")
        if isinstance(claiming, dict) and client_id in claiming:
            claiming.pop(client_id, None)
            _save_claim_impl(claim)
            cleared_claim = True

    if not existed and not cleared_claim:
        return jsonify({"success": False, "message": "设备不存在"}), 404

    broadcast_printer_state()
    print(f"[DEV] 已删除设备 {client_id}（接单接管{'已清空' if cleared_claim else '未受影响'}）")
    return jsonify({"success": True, "message": "设备已删除"})


@app.route("/api/printer/devices/cleanup", methods=["POST"])
@require_printer_scope("admin")
def printer_devices_cleanup():
    """批量清理测试设备 / 长期离线的无主设备（printer token 鉴权）。

    授权页「🧹 清理测试设备」按钮调它。body（字段都可省）:
      {"dry_run": true|false,               # true=只列出将删的 id，不真删 —— **默认 true** 防误点
       "include_offline_unbound": bool,     # 额外清理"长期离线 + 从未绑定所有者"的设备
       "min_offline_days": int}             # 上面的"长期"阈值，默认 7 天

    默认只清「测试设备 id」且**离线**的：在线设备正在跑，删了它会重登但会打断当前接单/打印，
    所以一律跳过并回报。删除同时清掉这些 id 的接单记录 —— 否则僵尸「接单中」会一直挂在
    授权页，且 get_active_printer_client() 可能挑到一台永远不会干活的机器。"""
    token = _get_printer_token()
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403

    data = request.get_json(silent=True) or {}
    dry_run = bool(data.get("dry_run", True))
    include_unbound = bool(data.get("include_offline_unbound", False))
    try:
        min_days = max(0, int(data.get("min_offline_days", 7)))
    except (TypeError, ValueError):
        min_days = 7

    online = set(get_active_clients())
    devices = load_devices()
    cutoff = datetime.now() - timedelta(days=min_days)
    targets, skipped_online = [], []
    for cid, entry in devices.items():
        is_test = is_test_device_id(cid)
        if is_test:
            if cid in online:
                skipped_online.append(cid)      # 在线的不动（可能正在跑测试/接单）
            else:
                targets.append(cid)
        elif include_unbound:
            if cid in online or (entry.get("owner_name") or "").strip():
                continue
            # 人工盖过章的设备一律不碰：那是一条人工结论（例如"这台 1020 不支持自动双面"），
            # 删掉就得让人重新确认一遍。只有探测/描述信息的，丢了无所谓。
            if isinstance((entry.get("printer") or {}).get("manual_auto_duplex"), bool):
                continue
            seen = _seen_dt(entry.get("last_seen", ""))
            if seen is not None and seen < cutoff:
                targets.append(cid)

    if dry_run:
        return jsonify({
            "success": True, "dry_run": True, "count": len(targets),
            "client_ids": targets[:200], "skipped_online": skipped_online[:50],
            "message": f"将清理 {len(targets)} 台（在线跳过 {len(skipped_online)} 台）",
        })

    removed = _delete_devices(targets, reason="授权页批量清理")
    return jsonify({
        "success": True, "dry_run": False, "removed": len(removed),
        "client_ids": removed[:200], "skipped_online": skipped_online[:50],
        "message": f"已清理 {len(removed)} 台测试/无主设备",
    })


def _seen_dt(value) -> datetime | None:
    """解析 devices.json 里的时间串（容错：拿不到就 None，调用方按"不满足条件"处理）。"""
    try:
        return datetime.strptime(str(value or "").strip(), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def purge_test_devices() -> int:
    """定时清理：把**离线**的测试设备连僵尸接单记录一起删掉（在线的一律不动）。

    为什么必须自动清：联调/冒烟脚本每次运行都可能注册新 client_id，人不会记得去授权页点删除，
    攒起来就是几十台"测试设备"（2026-10 现场：48 台）。删除是幂等的 —— 真机再连会自动重登，
    而测试设备本就该消失。返回删除台数。"""
    try:
        devices = load_devices()
        online = set(get_active_clients())
        targets = [cid for cid in devices if is_test_device_id(cid) and cid not in online]
        return len(_delete_devices(targets, reason="定时清理测试设备"))
    except Exception as e:
        print(f"[DEV] 清理测试设备异常: {e}")
        return 0


def _target_openid_arg() -> str:
    """S2-09（DS-22）：管理员"查看指定用户"的目标 openid。

    优先读请求头 `X-Target-Openid`，回退 query 参数 `?openid=`（老客户端兼容）。
    换成头的原因：openid 是跨设备稳定的用户标识，放进 URL 会落进 nginx/gunicorn
    访问日志、浏览器历史与 Referer。**它本身不是凭据** —— 两个调用点（/api/orders、
    /api/admin/user_detail）在取到目标后仍各自做角色/授权校验，伪造头得不到额外数据。
    """
    val = (request.headers.get("X-Target-Openid", "") or "").strip()
    if not val:
        val = (request.args.get("openid", "") or "").strip()
    return val


@app.route("/api/printer/orders", methods=["GET"])
@require_printer_scope("admin")
def printer_orders_view():
    """智能助手（小印）查看云端订单数据 —— 只读、**不限时间**。

    为什么单独开一个口：`/api/orders`、`/api/admin/recent_orders` 都走 `@login_required`
    （微信用户/管理员会话），本地打印工具手里只有**设备 token**，拿不到用户会话；
    而它正是打印现场那台机器，让小印经由它看订单是最短的一条路。

    鉴权：printer token 且必须有 admin scope（与其它 /api/printer/* 管理口同一把闸门）。
    只读：没有任何写路径 —— 不改状态、不改金额、不通知设备。

    查询参数（全部可省；**默认不按时间过滤 = 全量历史**）：
      order    订单号精确（HN20261105-0015）或 order_id；
      status   逗号分隔的聚合状态（sent,failed,timeout,abandoned,canceled…）；
      source   wechat / app / local（local 的别名 cloud = wechat）；
      owner    归属名模糊匹配（orders.owner_name）；
      file     文件名模糊匹配（order_files.file_name）；
      since    起始时间（含），YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS；
      until    截止时间（含）；
      limit    1..1000，默认 200；offset 默认 0。

    返回：{success, total, limit, offset, since, until, orders, summary}
      · total/summary 都是**过滤后、分页前**的统计；
      · summary = {orders, amount, by_status, by_source, first_at, last_at}；
      · orders 每项字段与「最近订单」卡片一致（页数/张数/金额同一份口径）。
    """
    client_key = ((request.headers.get("X-Client-Id") or "").strip()
                  or (request.remote_addr or ""))
    if not _rate_limit_ok(f"printer_orders:{client_key}", 60, 60):
        return jsonify({"success": False, "message": "请求过于频繁，请稍后再试"}), 429

    def _norm_time(raw, is_until):
        """把 YYYY-MM-DD / YYYY-MM-DD HH:MM / 完整时间统一成 created_at 的存储格式。"""
        text = str(raw or "").strip()
        if not text:
            return ""
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
            except ValueError:
                continue
            if fmt == "%Y-%m-%d":
                # 只给日期时：since 取当天 00:00:00，until 取当天 23:59:59（含整天）
                dt = dt.replace(hour=23 if is_until else 0,
                                minute=59 if is_until else 0,
                                second=59 if is_until else 0)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        raise ValueError(text)

    try:
        since = _norm_time(request.args.get("since"), False)
        until = _norm_time(request.args.get("until"), True)
    except ValueError as e:
        return jsonify({"success": False,
                        "message": f"时间格式不对：{e}（用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS）"}), 400
    try:
        limit = int(request.args.get("limit") or 200)
    except ValueError:
        return jsonify({"success": False, "message": "limit 必须是整数"}), 400
    limit = max(1, min(1000, limit))
    try:
        offset = int(request.args.get("offset") or 0)
    except ValueError:
        return jsonify({"success": False, "message": "offset 必须是整数"}), 400
    offset = max(0, offset)

    where, params = [], []
    order_arg = (request.args.get("order") or "").strip()
    if order_arg:
        if order_arg.isdigit():
            where.append("id = ?")
            params.append(int(order_arg))
        else:
            where.append("order_number = ?")
            params.append(order_arg)
    src = (request.args.get("source") or "").strip().lower()
    if src in ("wechat", "app", "local"):
        where.append("source = ?")
        params.append(src)
    elif src == "cloud":
        where.append("source = 'wechat'")
    owner_kw = (request.args.get("owner") or "").strip()
    if owner_kw:
        where.append("owner_name LIKE ?")
        params.append(f"%{owner_kw}%")
    if since:
        where.append("created_at >= ?")
        params.append(since)
    if until:
        where.append("created_at <= ?")
        params.append(until)
    file_kw = (request.args.get("file") or "").strip()
    if file_kw:
        where.append("id IN (SELECT order_id FROM order_files WHERE file_name LIKE ?)")
        params.append(f"%{file_kw}%")
    status_filter = [s.strip() for s in (request.args.get("status") or "").split(",") if s.strip()]

    sql = ("SELECT id, order_number, openid, owner_name, source, created_at, copies, "
           "page_count, duplex, total_price, status, is_admin_print, manual_status_by, "
           "manual_status_at FROM orders")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC"

    conn = get_db()
    try:
        rows = conn.execute(sql, params).fetchall()
        files_by_order = {}
        ids = [r["id"] for r in rows]
        for i in range(0, len(ids), 500):      # SQLite 老版本变量上限 999，分批查
            chunk = ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for f in conn.execute(
                f"SELECT order_id, page_count, copies, duplex, page_range, simplex_pages, "
                f"reject_reason, file_name FROM order_files WHERE order_id IN ({ph}) "
                f"ORDER BY order_id, id", chunk
            ).fetchall():
                files_by_order.setdefault(f["order_id"], []).append(dict(f))
        profiles = {}
        openids = sorted({str(r["openid"] or "") for r in rows
                          if r["openid"] and str(r["openid"]) != "local"})
        if openids:
            # 昵称在独立的 users.db（orders.db 里没有 users 表）
            user_conn = get_user_db()
            try:
                for i in range(0, len(openids), 500):
                    chunk = openids[i:i + 500]
                    ph = ",".join("?" * len(chunk))
                    for u in user_conn.execute(
                        f"SELECT openid, nickname FROM users WHERE openid IN ({ph})", chunk
                    ).fetchall():
                        profiles[u["openid"]] = {"nickname": u["nickname"] or ""}
            finally:
                user_conn.close()
        orders = [_order_view_dict(conn, r, files_by_order.get(r["id"]) or [], profiles)
                  for r in rows]
    finally:
        conn.close()

    if status_filter:
        # 状态是**聚合**出来的（一个订单多个子任务），只能在组装后过滤，不能在 SQL 里筛
        orders = [o for o in orders if o["status"] in status_filter]

    by_status = {}
    by_source = {}
    for o in orders:
        by_status[o["status"]] = by_status.get(o["status"], 0) + 1
        by_source[o["source"]] = by_source.get(o["source"], 0) + 1
    summary = {
        "orders": len(orders),
        "amount": round(sum(o["total_price"] for o in orders), 2),
        "by_status": by_status,
        "by_source": by_source,
        "first_at": orders[-1]["created_at"] if orders else "",
        "last_at": orders[0]["created_at"] if orders else "",
    }
    total = len(orders)
    page_orders = orders[offset:offset + limit]
    print(f"[printer_orders] {client_key} 查询订单 total={total}"
          f"（since={since or '-'} until={until or '-'} "
          f"status={','.join(status_filter) or '-'}）→ 返回 {len(page_orders)} 条")
    return jsonify({"success": True, "total": total, "limit": limit, "offset": offset,
                    "since": since, "until": until,
                    "orders": page_orders, "summary": summary})


@app.route("/api/orders", methods=["GET"])
@login_required
def get_orders():
    """返回任务列表。默认仅返回当前用户自己的订单。
    管理员可通过 X-Target-Openid 头（兼容 ?openid=）查看指定授权用户的订单。
    超级管理员可通过 ?view=all 查看全部订单，或指定 openid 查看指定用户。
    支持分页: ?page=1&per_page=20
    """
    # S1-08：每用户限速（结算页/订单列表会轮询；120/分 是正常用量的数倍，
    # 只拦"一个 token 把 /api/orders 当扫描器用"）。
    if not _rate_limit_ok(f"orders:{g.openid}", 120, 60):
        return jsonify({"success": False, "message": "请求过于频繁，请稍后再试"}), 429
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    page = max(1, page)
    per_page = max(1, min(100, per_page))
    offset = (page - 1) * per_page

    role = compute_role(g.openid)
    is_super = SUPER_ADMIN_OPENID and g.openid == SUPER_ADMIN_OPENID
    target_openid = _target_openid_arg()
    view_all = is_super and request.args.get("view", "") == "all"
    source_filter = (request.args.get("source", "") or "").strip()

    conn = get_db()

    # 构建查询条件
    source_clause = ""
    source_params = []
    if source_filter in ("wechat", "app", "local"):
        source_clause = " AND source = ?"
        source_params = [source_filter]
    elif source_filter == "cloud":
        # 兼容旧版统计页：cloud 语义已并入 wechat（历史云端订单统一按小程序计）
        source_clause = " AND source = 'wechat'"
        source_params = []

    def _user_orders_clause(uid: str):
        """构造「某用户的任务列表」查询条件。

        默认按 openid=uid 过滤；若该 openid 在收支清算成员绑定表中绑定了成员，
        则额外并入本地打印工具创建的、归属该成员的本地订单
        （openid='local' AND source='local' AND owner_name=成员名）——
        使「历史授权用户 / 管理管理员 / 我的订单」的任务列表能看到名下本地打印的订单
        （v4.4 修复：此前本地订单 openid='local' 永远匹配不到具体用户）。
        带 source 过滤时保持原语义（只在指定来源内查询），不做并入。"""
        base = "openid = ?"
        p = [uid]
        if not source_filter:
            bound = _get_bound_owner_name(uid)
            if bound:
                base = "(openid = ? OR (source = 'local' AND owner_name = ?))"
                p = [uid, bound]
        return base + source_clause, p + source_params

    if view_all:
        # 超级管理员查看全部订单
        where_clause = "1 = 1" + source_clause
        params = source_params.copy()
    elif source_filter == "local" and (is_super or role == "admin"):
        # 管理员查看本地打印工具订单（openid='local'，不是微信 openid）
        where_clause = "openid = 'local'"
        params = []
    elif is_super and target_openid:
        # 超级管理员查看指定用户
        where_clause, params = _user_orders_clause(target_openid)
    elif role == "admin" and target_openid:
        # 管理员查看指定用户：需验证该用户是否由本管理员授权
        user_conn = get_user_db()
        auth_row = user_conn.execute(
            "SELECT used_by FROM license_keys WHERE created_by = ? AND used_by = ? LIMIT 1",
            (g.openid, target_openid),
        ).fetchone()
        user_conn.close()
        if not auth_row:
            conn.close()
            return jsonify({"success": False, "message": "无权查看该用户的订单"}), 403
        where_clause, params = _user_orders_clause(target_openid)
    else:
        # 默认：所有角色只看自己的订单（含归属自己绑定成员的本地订单）
        where_clause, params = _user_orders_clause(g.openid)

    # 查询总数
    total = conn.execute(
        f"SELECT COUNT(*) FROM orders WHERE {where_clause}",
        params,
    ).fetchone()[0]

    # 分页查询
    orders_rows = conn.execute(
        f"""
        SELECT id, file_id, file, copies, status, created_at, openid, duplex,
               page_count, is_free, total_price, order_number,
               delivery_enabled, delivery_location, delivery_percentage,
               urgency, urgency_price, cover_page, cover_page_price, pickup_address,
               schedule_mode, scheduled_at, schedule_frozen,
               source, owner_name, is_admin_print, remark, print_started_at,
               received_client, target_client, manual_status_by, manual_status_at
        FROM orders
        WHERE {where_clause}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        params + [per_page, offset],
    ).fetchall()

    orders = []
    for o_row in orders_rows:
        order = dict(o_row)
        # 接单设备展示名（{所有者}的设备（{client_id}））；空值显示 — 
        order["received_label"] = device_display_label(order.get("received_client", ""))
        oid = order["id"]

        # 查询子任务
        of_rows = conn.execute(
            """SELECT id, file_id, file_name, copies, page_count, page_range,
                      page_range_original, page_range_truncated,
                      simplex_pages, blank_tail_exclude,
                      reject_reason,
                      total_price, is_free, status, created_at, duplex
               FROM order_files WHERE order_id = ? ORDER BY id ASC""",
            (oid,),
        ).fetchall()

        if of_rows:
            files = [dict(r) for r in of_rows]
            total_copies = 0
            total_pages = 0
            total_sheets = 0
            for f in files:
                # 有效页数/张数按页码范围折算（v1.1.21：指定页面选择时合计页数取所选页，不再用整份页数）
                st = _file_range_stats(f["page_count"] or 0, f["copies"] or 1,
                                       f["duplex"] or "on", f["page_range"] or "",
                                       f.get("simplex_pages") or "")
                f["effective_pages"] = st["effective_pages"]   # 每份有效打印页数（范围过滤后）
                f["sheets"] = st["sheets"]                     # 每份纸张张数
                f["total_pages"] = st["total_pages"]           # 文件合计页数（有效页数 × 份数）
                f["total_sheets"] = st["total_sheets"]         # 文件合计张数（每份张数 × 份数）
                total_copies += f["copies"] or 1
                total_pages += st["total_pages"]
                total_sheets += st["total_sheets"]
            # 文件名摘要
            names = [f["file_name"] for f in files]
            if len(names) == 1:
                file_summary = names[0]
            else:
                file_summary = f"{names[0]} +{len(names) - 1} 个文件"
            # 用聚合状态覆盖父订单的旧状态
            order["status"] = aggregate_order_status(conn, oid) or order["status"]
        else:
            # 旧数据降级：没有 order_files → 用 orders 自身字段构造（无 page_range，按整份口径）
            legacy_copies = order.get("copies", 1) or 1
            legacy_st = _file_range_stats(order.get("page_count", 1) or 0, legacy_copies,
                                          order.get("duplex", "on"), "")
            order_file = {
                "file_id": order.get("file_id"),
                "file_name": order.get("file", "未知文件"),
                "copies": legacy_copies,
                "page_count": order.get("page_count", 1),
                "page_range": "",
                "total_price": order.get("total_price", 0),
                "is_free": order.get("is_free", 0),
                "status": order["status"],
                "duplex": order.get("duplex", "on"),
                "effective_pages": legacy_st["effective_pages"],
                "sheets": legacy_st["sheets"],
                "total_pages": legacy_st["total_pages"],
                "total_sheets": legacy_st["total_sheets"],
            }
            files = [order_file]
            total_copies = legacy_copies
            total_pages = legacy_st["total_pages"]
            total_sheets = legacy_st["total_sheets"]
            names = [order["file"]]
            file_summary = order["file"]

        order["files"] = files
        order["total_copies"] = total_copies
        order["total_pages"] = total_pages
        order["total_sheets"] = total_sheets
        order["file_summary"] = file_summary
        # 订单级失败/超时原因（与 /api/order/<id> 详情同口径：第一条非空 reject_reason）。
        # 列表页据此显示一行原因，避免用户只看到"失败"却不知道为什么。
        order["status_reason"] = next(
            (str(f.get("reject_reason") or "").strip()
             for f in files if str(f.get("reject_reason") or "").strip()), "")
        # 向前端保持一致：旧字段仍保留（兼容性），但语义标注为聚合值
        order["file"] = file_summary
        order["copies"] = total_copies
        # 等待时长（下单 → 开始打印）：后端用服务器时钟统一计算，前端不再自行相减
        order["wait_seconds"] = calc_wait_seconds(order.get("created_at"), order.get("print_started_at"))
        orders.append(order)

    # 关联临时许可密钥：每个订单显示它消费的密钥（license_keys.order_id 单向关联）
    license_map = {}
    order_ids = [o["id"] for o in orders]
    if order_ids:
        user_conn = get_user_db()
        try:
            placeholders = ",".join("?" for _ in order_ids)
            lrows = user_conn.execute(
                f"""SELECT order_id, key, type, created_at, used_at, expires_at, status
                    FROM license_keys
                    WHERE order_id IN ({placeholders})""",
                order_ids,
            ).fetchall()
            for lr in lrows:
                license_map[lr["order_id"]] = dict(lr)
        finally:
            user_conn.close()
    for o in orders:
        o["license_info"] = license_map.get(o["id"]) or None

    conn.close()
    return jsonify({
        "success": True,
        "orders": orders,
        "count": len(orders),
        "total": total,
        "page": page,
        "per_page": per_page,
        "has_more": (page * per_page) < total,
    })


@app.route("/api/order/<order_id>", methods=["GET"])
@login_required
def get_order_detail(order_id):
    """获取单个任务详情（仅限当前用户），含子任务文件列表"""
    conn = get_db()
    row = conn.execute(
        """
        SELECT o.id, o.file_id, o.file, o.copies,
               o.status, o.created_at, o.openid, o.duplex,
               o.page_count, o.is_free, o.total_price,
               o.delivery_enabled, o.delivery_location, o.delivery_percentage,
               o.urgency, o.urgency_price, o.cover_page, o.cover_page_price,
               o.pickup_address,
               o.schedule_mode, o.scheduled_at, o.schedule_frozen,
               o.owner_name, o.is_admin_print, o.remark, o.print_started_at,
               o.received_client, o.target_client,
               o.manual_status_by, o.manual_status_at
        FROM orders o
        WHERE o.id = ? AND o.openid = ?
        """,
        (order_id, g.openid),
    ).fetchone()

    if not row:
        conn.close()
        return jsonify({"success": False, "message": "任务不存在或无权访问"}), 404

    order = dict(row)
    # 接单设备展示名（{所有者}的设备（{client_id}））；空值显示 — 
    order["received_label"] = device_display_label(order.get("received_client", ""))

    # 查询子任务
    of_rows = conn.execute(
        """SELECT of.id, of.file_id, of.file_name, of.copies, of.page_count,
                  of.page_range, of.page_range_original, of.page_range_truncated,
                  of.total_price, of.is_free, of.status, of.duplex, of.image_orientation,
                  of.simplex_pages, of.blank_tail_exclude, of.reject_reason
           FROM order_files of WHERE of.order_id = ? ORDER BY of.id ASC""",
        (order_id,),
    ).fetchall()

    if of_rows:
        files = []
        for of_row in of_rows:
            f = dict(of_row)
            # 关联 files 表获取文件大小和类型
            if f["file_id"]:
                frow = conn.execute("SELECT size, original_name, saved_name FROM files WHERE id = ?",
                                    (f["file_id"],)).fetchone()
                if frow:
                    f["size"] = frow["size"]
                    f["original_name"] = frow["original_name"]
                    ext = os.path.splitext(frow["original_name"] or f["file_name"])[1]
                    f["file_type"] = ext.lstrip(".").upper() if ext else "未知"
                else:
                    f["size"] = 0
                    f["original_name"] = f["file_name"]
                    ext = os.path.splitext(f["file_name"])[1]
                    f["file_type"] = ext.lstrip(".").upper() if ext else "未知"
            else:
                f["size"] = 0
                f["original_name"] = f["file_name"]
                ext = os.path.splitext(f["file_name"])[1]
                f["file_type"] = ext.lstrip(".").upper() if ext else "未知"

            files.append(f)

        total_copies = 0
        total_pages = 0
        total_sheets = 0
        for f in files:
            # 有效页数/张数按页码范围折算（合计页数取所选页，不再用整份页数）
            st = _file_range_stats(f["page_count"] or 0, f["copies"] or 1,
                                   f["duplex"] or "on", f["page_range"] or "",
                                   f.get("simplex_pages") or "")
            f["effective_pages"] = st["effective_pages"]
            f["sheets"] = st["sheets"]
            f["total_pages"] = st["total_pages"]
            f["total_sheets"] = st["total_sheets"]
            total_copies += f["copies"] or 1
            total_pages += st["total_pages"]
            total_sheets += st["total_sheets"]
        order["files"] = files
        order["total_copies"] = total_copies
        order["total_pages"] = total_pages
        order["total_sheets"] = total_sheets
        # 用聚合状态
        order["status"] = aggregate_order_status(conn, order_id) or order["status"]
        # 订单级失败原因（子任务级 reject_reason 仍逐个保留在 files 里）
        order["status_reason"] = order_status_reason(conn, order_id)
    else:
        # 旧数据降级
        conn2 = get_db()
        frow = conn2.execute("SELECT original_name, size, saved_name FROM files WHERE id = ?",
                             (order.get("file_id"),)).fetchone()
        conn2.close()
        original_name = frow["original_name"] if frow else order["file"]
        file_type = "未知"
        ext = os.path.splitext(original_name or order["file"])[1]
        file_type = ext.lstrip(".").upper() if ext else "未知"
        legacy_copies = order.get("copies", 1) or 1
        legacy_st = _file_range_stats(order.get("page_count", 1) or 0, legacy_copies,
                                      order.get("duplex", "on"), "")
        order["files"] = [{
            "id": None,
            "file_id": order.get("file_id"),
            "file_name": order.get("file", "未知文件"),
            "original_name": original_name,
            "copies": legacy_copies,
            "page_count": order.get("page_count", 1),
            "total_price": order.get("total_price", 0),
            "is_free": order.get("is_free", 0),
            "status": order["status"],
            "page_range": "",
            "size": frow["size"] if frow else 0,
            "file_type": file_type,
            "duplex": order.get("duplex", "on"),
            "effective_pages": legacy_st["effective_pages"],
            "sheets": legacy_st["sheets"],
            "total_pages": legacy_st["total_pages"],
            "total_sheets": legacy_st["total_sheets"],
        }]
        order["total_copies"] = legacy_copies
        order["total_pages"] = legacy_st["total_pages"]
        order["total_sheets"] = legacy_st["total_sheets"]
        # 老单没有子任务行 → 没有失败原因可汇总（字段照给，前端不用判 undefined）
        order["status_reason"] = ""

    # 关联临时许可密钥（每个订单消费的密钥）
    user_conn = get_user_db()
    try:
        lrow = user_conn.execute(
            "SELECT order_id, key, type, created_at, used_at, expires_at, status FROM license_keys WHERE order_id = ? LIMIT 1",
            (order_id,),
        ).fetchone()
        order["license_info"] = dict(lrow) if lrow else None
    finally:
        user_conn.close()
    order["wait_seconds"] = calc_wait_seconds(order.get("created_at"), order.get("print_started_at"))

    conn.close()
    return jsonify({"success": True, "order": order})


@app.route("/api/order_price/<order_id>", methods=["GET"])
@login_required
def get_order_price(order_id):
    """获取订单的价格明细（供结算/确认使用）。
    返回每份文件的价格明细和订单总价。
    """
    conn = get_db()
    # 🟡-C4b：归属过滤原写法 `WHERE id = ? AND openid = ?` 让管理员读**他人**订单恒 404，
    # 而本接口唯一调用方就是管理员的「活跃密钥 → 结算」按钮（小程序 onEndPrintTask /
    # APP settleOrder：把结算详情复制给顾客）⇒ 该按钮线上从未可用。管理员本就可读
    # 全站订单（订单列表 / 统计 / 许可密钥等接口同理），故只有管理员不过滤 openid；
    # 其余用户保持「本人订单 + 同一条 404 文案」（不区分"不存在"与"无权"，不泄露存在性）。
    if compute_role(g.openid) == "admin":
        row = conn.execute(
            "SELECT id, total_price, is_free FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, total_price, is_free FROM orders WHERE id = ? AND openid = ?",
            (order_id, g.openid),
        ).fetchone()

    if not row:
        conn.close()
        return jsonify({"success": False, "message": "任务不存在或无权访问"}), 404

    order = dict(row)

    # 查询子任务价格明细
    of_rows = conn.execute(
        """SELECT of.id, of.file_name, of.copies, of.page_count, of.duplex, of.page_range,
                  of.total_price, of.is_free, of.status,
                  of.simplex_pages, of.blank_tail_exclude
           FROM order_files of WHERE of.order_id = ? ORDER BY of.id ASC""",
        (order_id,),
    ).fetchall()

    files = []
    for of_row in of_rows:
        f = dict(of_row)
        # 附上单价明细（用于前端展示）
        per_copy_price = calculate_price(f["page_count"], f.get("duplex", "on"),
                                         f.get("page_range", ""),
                                         simplex_pages=_row_simplex_pages(f))
        f["per_copy_price"] = per_copy_price
        # 附上有效页数/张数（供结算文案等展示，含页码范围折算）
        st = _file_range_stats(f["page_count"] or 0, f["copies"] or 1,
                               f.get("duplex", "on"), f.get("page_range", "") or "",
                               _row_simplex_pages(f))
        f["effective_pages"] = st["effective_pages"]
        f["sheets"] = st["sheets"]
        f["total_pages"] = st["total_pages"]
        f["total_sheets"] = st["total_sheets"]
        f["unit"] = "元/张" if f.get("duplex") == "on" else "元/页"
        files.append(f)

    # 汇总
    # 🔴-C4：总价一律取**订单级** orders.total_price —— 它由 submit_order 建单时写成
    # 「文件小计 + 加急费 + 首页费 + 派送费」，页数被修正时的整单重算路径也维持同一口径。
    # 原实现用 order_files 小计相加，三类附加费全丢，而前端把本字段直接当「结算总价」
    # 复制给顾客（me.js「打印任务结算」）⇒ 只要勾了首页（默认 ¥0.10）就必现少收。
    all_free = all(f.get("is_free", 0) for f in files)
    total_price = round(float(order.get("total_price") or 0), 2)

    conn.close()
    return jsonify({
        "success": True,
        "order_id": int(order_id),
        "is_free": bool(all_free),
        "files": files,
        "total_price": total_price,
    })


@app.route("/api/cancel_order", methods=["POST"])
@login_required
def cancel_order():
    """取消任务（限 queued/printing/waiting/downloading/scheduled 状态且属于当前用户）。
    取消后通过 SocketIO 通知已连接的打印机客户端。

    P2-2：整个"读状态 → 校验 → 更新"纳入 db_lock（BEGIN IMMEDIATE），
    防止与 push/pull 并发时读到旧状态后覆盖对方写入。
    允许取消 printing 任务存在"打印机已开始打印"的小窗口：客户端收到
    order_canceled 事件后自行中止，可接受的窗口（现状即如此，保持并注明）。

    P2-9：允许取消预约单（scheduled/downloading/waiting）——用户预约了打印，
    到点前应能反悔；且取消时一次性把所有未终结子任务（含预约单的
    scheduled/downloading/waiting）置为 canceled，避免取消不完整导致
    process_scheduled_orders 把已取消订单的文件重新下发/打印。

    2026-11-10 体检（🔴15 / 🟡9）：再放开 accepted / offline_unknown / timeout 三种父状态。
    以前顾客在这三种「打印机已确认/断线未知/已超时」状态下点取消，会得到
    「任务状态为 accepted，无法取消」——纸可能根本没出来，人却只能找客服手工改库。
    口径不变：取消只是把**还没终结**的子任务置 canceled 并通知设备清队列；
    设备是最终裁决者 —— 它若真把纸打出来了，迟到的 print_success 仍覆盖 canceled 为 sent。"""
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "请提供 JSON 数据"}), 400

    order_id = data.get("order_id", "")
    if not order_id:
        return jsonify({"success": False, "message": "缺少 order_id"}), 400

    sub_tasks = []
    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, status, openid FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()

            if not row:
                return jsonify({"success": False, "message": "任务不存在"}), 404

            if row["openid"] != g.openid:
                return jsonify({"success": False, "message": "无权操作此任务"}), 403

            if row["status"] not in ("queued", "printing", "waiting", "downloading", "scheduled",
                                     "accepted", "offline_unknown", "timeout"):
                return jsonify({"success": False, "message": f"任务状态为 {row['status']}，无法取消"}), 400

            # 查询被取消的子任务 ID 和已连接的打印机客户端。
            # 覆盖预约单的 scheduled/downloading/waiting（到点前也允许取消）
            sub_tasks = conn.execute(
                "SELECT id, operator_client FROM order_files WHERE order_id = ?"
                " AND status NOT IN ('sent', 'failed', 'timeout', 'canceled', 'rejected', 'abandoned')",
                (order_id,),
            ).fetchall()

            # 取消父订单和所有未终结子任务（同时清空 locked_at）。
            # 一次性覆盖 queued/printing/waiting/downloading/scheduled 等全部非终态，
            # 避免取消不完整导致已取消订单被 process_scheduled_orders 重新下发/卡死。
            conn.execute(
                "UPDATE order_files SET status = 'canceled', locked_at = '' WHERE order_id = ?"
                " AND status NOT IN ('sent', 'failed', 'timeout', 'canceled', 'rejected', 'abandoned')",
                (order_id,),
            )
            conn.execute(
                "UPDATE orders SET status = 'canceled' WHERE id = ?",
                (order_id,),
            )
            # 🔴-D15：上面那条只动「非终态」，timeout 是终态但**没出纸** —— 顾客点取消
            # 就是想让这单闭嘴（不然列表里一直挂着「超时」）。只把 timeout 翻成 canceled，
            # sent（真打出来了）/ failed / rejected / abandoned 一概不碰。
            conn.execute(
                "UPDATE order_files SET status = 'canceled' WHERE order_id = ? AND status = ?",
                (order_id, TIMEOUT_STATUS),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # 通过 SocketIO 通知已连接的打印机客户端
    task_ids = [t["id"] for t in sub_tasks]
    notified_clients = set()
    for t in sub_tasks:
        client_id = t["operator_client"]
        if client_id and client_id not in notified_clients:
            notified_clients.add(client_id)
            with printer_clients_lock:
                info = printer_clients.get(client_id)
                sid = info["sid"] if info else None
            if sid:
                try:
                    socketio.emit("order_canceled", {
                        "order_id": order_id,
                        "task_ids": task_ids,
                    }, to=sid)
                    print(f"  [CANCEL] 已通知打印机 {client_id}: 订单 #{order_id} 已取消")
                except Exception as e:
                    print(f"  [CANCEL] 通知打印机失败: {e}")

    return jsonify({"success": True, "message": "任务已取消"})


@app.route("/api/accept_order", methods=["POST"])
def accept_order():
    """打印机确认接受订单（需 token 认证）。将状态从 printing 改为 accepted（终端状态）。

    P2-9：订单已被用户取消 → 拒绝接受（409）。原先父订单被无条件翻成 accepted，
    已取消订单在窗口期被"添加"会把 canceled 污染成 accepted，且本地会打印已取消订单。
    改为 db_lock 原子校验 + 仅当确有子任务被接受时才更新父订单。"""
    token = _get_printer_token() or (request.get_json(silent=True) or {}).get("token", "")
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id", "") or request.args.get("order_id", "")
    if not order_id:
        return jsonify({"success": False, "message": "缺少 order_id"}), 400
    # P1-8：accept 时刷新 locked_at（复用 P0-1 的 locked_at 列）——
    # check_printing_timeout 对已 accept 任务按 accept 时刻起算 3 分钟反馈超时
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            o = conn.execute(
                "SELECT status FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if not o:
                return jsonify({"success": False, "message": "任务不存在"}), 404
            if o["status"] == "canceled":
                print(f"  [SKIP] accept 订单 #{order_id}: 已被用户取消，拒绝接受")
                return jsonify({"success": False, "message": "订单已被取消，无法接受"}), 409
            cur = conn.execute(
                "UPDATE order_files SET status = 'accepted', locked_at = ? WHERE order_id = ? AND status = 'printing'",
                (now_str, order_id),
            )
            if cur.rowcount > 0:
                conn.execute(
                    "UPDATE orders SET status = 'accepted' WHERE id = ?",
                    (order_id,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    print(f"  [ACCEPT] 订单 #{order_id} 已被打印机接受")
    return jsonify({"success": True, "message": "订单已接受"})


@app.route("/api/reject_order", methods=["POST"])
def reject_order():
    """打印机打回订单（需 token 认证）。将订单状态设为 rejected。

    P2-9：已取消订单不允许打回（409），避免 canceled 被覆盖成 rejected；
    且仅当确有子任务被打回时才更新父订单（防空转污染）。"""
    token = _get_printer_token() or (request.get_json(silent=True) or {}).get("token", "")
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403

    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id", "") or request.args.get("order_id", "")
    if not order_id:
        return jsonify({"success": False, "message": "缺少 order_id"}), 400

    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            o = conn.execute(
                "SELECT status FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if not o:
                return jsonify({"success": False, "message": "任务不存在"}), 404
            if o["status"] == "canceled":
                print(f"  [SKIP] reject 订单 #{order_id}: 已被用户取消，拒绝打回")
                return jsonify({"success": False, "message": "订单已被取消，无法打回"}), 409
            # 将子任务和父订单都设为 rejected
            cur = conn.execute(
                "UPDATE order_files SET status = 'rejected' WHERE order_id = ? AND status IN ('queued', 'printing')",
                (order_id,),
            )
            if cur.rowcount > 0:
                conn.execute(
                    "UPDATE orders SET status = 'rejected' WHERE id = ?",
                    (order_id,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    print(f"  [REJECT] 订单 #{order_id} 已被打印机打回")
    return jsonify({"success": True, "message": "订单已打回"})


# ==================== 收支清算配置（云端存储） ====================


@app.route("/api/finance/config", methods=["GET"])
def get_finance_config():
    """读取收支清算云端配置（需 printer token 认证）。data 为 null 表示尚未保存。"""
    token = _get_printer_token()
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT data, COALESCE(rev, 0) AS rev FROM finance_config WHERE id = 1").fetchone()
    finally:
        conn.close()
    if row and row["data"]:
        try:
            return jsonify({"success": True, "data": json.loads(row["data"]),
                            "server_version": int(row["rev"] or 0)})
        except Exception:
            return jsonify({"success": False, "message": "配置数据损坏"}), 500
    return jsonify({"success": True, "data": None, "server_version": 0})


@app.route("/api/finance/config", methods=["POST"])
def save_finance_config():
    """保存收支清算云端配置（需 printer token 认证）。body: {"data": {...完整配置}}"""
    token = _get_printer_token() or (request.get_json(silent=True) or {}).get("token", "")
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    payload = data.get("data", None)
    if payload is None:
        return jsonify({"success": False, "message": "缺少 data"}), 400
    if isinstance(payload, dict):
        payload.pop("_filepath", None)  # 清理内部字段
    with db_lock:
        conn = get_db()
        try:
            # 体检 🔴8（2026-11-10）：每次成功保存都把 rev +1，并**返回**新修订号。
            # 前端 `_saveToServer` 拿它和本地 clientVersion 比：`server_version > clientVersion + 1`
            # 就说明期间有别的设备写过 —— 这时必须由人来选「重新加载云端」还是「强制覆盖」，
            # 不能像以前那样静默整份覆盖（后端不返回 server_version 时前端 conflict 恒 false）。
            conn.execute(
                "INSERT INTO finance_config (id, data, updated_at, rev) VALUES (1, ?, ?, 1) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data, "
                "updated_at = excluded.updated_at, "
                "rev = COALESCE(finance_config.rev, 0) + 1",
                (json.dumps(payload, ensure_ascii=False),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
            _rev_row = conn.execute(
                "SELECT COALESCE(rev, 0) FROM finance_config WHERE id = 1").fetchone()
            _rev = int(_rev_row[0]) if _rev_row else 0
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return jsonify({"success": True, "server_version": _rev, "message": "配置已保存"})


@app.route("/api/abandon_order", methods=["POST"])
def abandon_order():
    """打印机放弃已接受的订单（需 token 认证）。"""
    token = _get_printer_token() or (request.get_json(silent=True) or {}).get("token", "")
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id", "") or request.args.get("order_id", "")
    if not order_id:
        return jsonify({"success": False, "message": "缺少 order_id"}), 400
    conn = get_db()
    # 放弃非 sent 子任务：queued/printing/accepted/offline_unknown/waiting/downloading 常规状态，
    # 以及 failed（任务可能已被自动超时判败，但用户显式放弃应覆盖为 abandoned）；
    # 仅 sent（已打印完成）不触碰——避免误伤已完成打印的订单
    conn.execute(
        "UPDATE order_files SET status = 'abandoned' WHERE order_id = ?"
        " AND status IN ('queued', 'printing', 'accepted', 'offline_unknown',"
        "                'waiting', 'downloading', 'failed')",
        (order_id,),
    )
    conn.commit()
    # 用聚合状态重算父订单（而非无条件覆盖为 abandoned）：
    # 全部子任务 sent → 父订单保持 sent；被放弃的文件 → 父订单变为 abandoned
    refresh_order_status(conn, order_id)
    conn.commit()
    conn.close()
    print(f"  [ABANDON] 订单 #{order_id} 放弃完成（非终态子任务→abandoned，父订单按聚合重算）")
    return jsonify({"success": True, "message": "订单已标记为放弃打印"})


@app.route("/api/abandon_reserved_order", methods=["POST"])
def abandon_reserved_order():
    """本地打印工具放弃未提交的预留订单（按 order_number，需 token 认证）。
    场景：用户在本地工具中点击"复制"获得了订单号但未点击"开始打印"，
    直接关闭窗口或删除标签页时调用，将 reserved 标记为 abandoned，
    同时补记价格（本地工具在获取订单号时已知文件/份数/双面配置）。"""
    token = _get_printer_token() or (request.get_json(silent=True) or {}).get("token", "")
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = request.get_json(silent=True) or {}
    order_number = data.get("order_number", "") or request.args.get("order_number", "")
    if not order_number:
        return jsonify({"success": False, "message": "缺少 order_number"}), 400
    total_price = data.get("total_price", None)
    # 归属 / 「管理员自行打印」标记：新版本地工具会随放弃请求一起上报（与预留上报同口径）。
    # 未携带时（旧版工具）才回退到历史缺省——补记占位名并视为「管理员自行打印」，
    # 否则会把「接单打印」的顾客订单误盖章成自打（类型列/结算收益分类/管理员订单统计
    # 都按 is_admin_print 区分，实测 2026-10-08 HN20261008-0025/0026 就是这样被误标的）。
    owner_name_in = (data.get("owner_name") or "").strip()
    is_admin_print_in = data.get("is_admin_print")
    if is_admin_print_in is not None:
        is_admin_print_in = 1 if is_admin_print_in else 0
    conn = get_db()
    try:
        if total_price is not None:
            # 已配置打印（工具放弃时已知文件/份数/双面 → 有金额）：保留为 abandoned 订单，
            # 并补记归属（工具已上报则以其为准；未上报才用缺省占位名 / 管理员自行打印）
            cursor = conn.execute(
                """UPDATE orders SET status = 'abandoned', total_price = ?,
                                     owner_name = COALESCE(NULLIF(?, ''), NULLIF(owner_name, ''), ?),
                                     is_admin_print = COALESCE(?,
                                         CASE WHEN is_admin_print = 0 THEN 1 ELSE is_admin_print END)
                   WHERE order_number = ? AND status = 'reserved'""",
                (float(total_price), owner_name_in, DEFAULT_OWNER_NAME,
                 is_admin_print_in, order_number),
            )
            price_info = f"，价格 ¥{float(total_price):.2f}"
        else:
            # 未配置的纯占位单（直接关闭/删除标签页）：无金额无归属，直接删除整行，不留残留
            cursor = conn.execute(
                """DELETE FROM orders WHERE order_number = ? AND status = 'reserved'
                   AND file = '（预留位置）' AND total_price <= 0
                   AND NOT EXISTS (SELECT 1 FROM order_files f WHERE f.order_id = orders.id)""",
                (order_number,),
            )
            price_info = ""
        count = cursor.rowcount
        if count > 0:
            conn.commit()
            action = "清除（纯占位）" if total_price is None else "放弃打印" + price_info
            print(f"  [ABANDON] 预留订单 {order_number} 已被{action}")
        if total_price is not None:
            # 预留单的子任务同步置 abandoned（无条件：父订单可能已被放弃，残留 reserved
            # 子任务会让结算/列表的实时聚合把 abandoned 回退成 reserved）
            conn.execute(
                "UPDATE order_files SET status = 'abandoned'"
                " WHERE order_id IN (SELECT id FROM orders WHERE order_number = ?) AND status = 'reserved'",
                (order_number,),
            )
            conn.commit()
        return jsonify({"success": True, "message": f"已处理 {count} 个预留订单"})
    except Exception as e:
        conn.rollback()
        print(f"[ERROR] {request.method} {request.path} exc={type(e).__name__}: "
              f"{_redact_paths(_scrub_secret(str(e)))[:160]}")
        return jsonify({"success": False, "message": "服务器内部错误"}), 500
    finally:
        conn.close()


# ==================== 本地订单上报（本地打印工具使用）====================

@app.route("/api/local_orders", methods=["POST"])
def local_orders():
    """本地打印工具上报本地打印任务。需 token 认证。

    v4.6：支持预留上报——本地工具点击「复制价格」后先以 status='reserved' 上报完整
    订单信息（文件明细 + 备注 + 价格），使"已预留"订单在后端非空占位；打印完成再以
    status='sent' 覆盖（或断线重连按本地是否已打印选择状态）。两次上报都会删旧插新
    覆盖文件明细与备注，保证后端信息与本地一致。"""
    token = _get_printer_token() or (request.get_json(silent=True) or {}).get("token", "")
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403

    data = request.get_json(silent=True) or {}
    order_number = data.get("order_number", "")
    files = data.get("files", [])
    total_price = data.get("total_price", 0)
    created_at = data.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    # created_at 口径（2026-11，硬规矩）：**created_at = 订单首次生成（首次与服务器交互）时刻，
    # 此后不可覆盖**。它只在 INSERT 时写入：
    #   · /api/next_order_number 预留单 → 服务器 now；
    #   · 本地工具离线首同步（本接口 INSERT 分支）→ 允许用 payload 里的 created_at。
    # 已存在的行（预留单转正式单 / 重复上报）**一律不写 created_at** —— 否则同一张单的
    # "下单时间"会随每次上报漂移：等待时长（print_started_at − created_at）、按日期统计与
    # 结算区间都跟着变，甚至出现负等待。
    # 与 2026-11-02 的 finished_at 口径取舍：finished_at（打印完成时刻）允许设备上报覆盖，
    # 因为它描述的是"最后一次真的出纸"的物理事实；created_at 是账期归属的锚点，不能被改写。
    # 订单归属标记（本地工具订单号右侧下拉 + 勾选框）
    owner_name = (data.get("owner_name", "") or "").strip()
    is_admin_print = data.get("is_admin_print")
    if is_admin_print is not None:
        is_admin_print = 1 if is_admin_print else 0
    # 上报状态：'sent'（打印完成）/ 'reserved'（预留占位，复制价格后未打印）
    status = data.get("status", "sent")
    if status not in ("sent", "reserved"):
        status = "sent"
    # 订单备注（≤100 字）
    remark = str(data.get("remark", "") or "")[:100]
    # 接收设备（多设备接单）：本地工具上报本机 client_id，用于记录订单「接单设备」
    # 2026-11-03（HN20261103-0014/0035/0036 三单归属空白）加一道兜底：payload 里没有
    # client_id 时回落请求头 `X-Client-Id`（客户端会话级设备身份）。旧客户端两条都缺，
    # 下面 sent 分支会记一条 warning，不再静默丢归属。
    report_client_id = (data.get("client_id") or "").strip()
    if not report_client_id:
        try:
            report_client_id = (request.headers.get("X-Client-Id") or "").strip()
        except Exception:
            report_client_id = ""

    if not order_number or not files:
        return jsonify({"success": False, "message": "缺少 order_number 或 files"}), 400

    conn = get_db()
    try:
        # 查询是否已存在占位记录（由 /api/next_order_number 创建，或离线同步重复提交）
        existing = conn.execute(
            "SELECT id, status FROM orders WHERE order_number = ?", (order_number,)
        ).fetchone()

        order_file_label = f"本地打印 {len(files)} 个文件"

        if existing:
            order_id = existing["id"]
            old_status = existing["status"]
            # 如果已经是 sent/accepted 等终态 → 幂等跳过（预留上报不覆盖已完成的订单）
            if old_status in ("sent", "accepted"):
                conn.close()
                return jsonify({"success": True, "message": "订单已存在，跳过同步", "order_id": order_id})

            # 🔴 来源闸门（2026-10-18 现场：小程序订单打印后变「本地」）：
            # 本接口只该改写**本地打印工具自己开的单**（`/api/next_order_number` 预留时写
            # openid='local', source='local'）。云端用户订单（openid=微信/设备号、
            # source='wechat'/'app'）**绝不能**被这里改成 local —— 那会让结算页的来源徽标、
            # 「订单构成」「本地/云端」筛选、按来源对账全部错位，顾客身份也丢了。
            # 修复后的副作用评估：云端单的完成/失败仍由 print_success / print_failed
            # （按 task_id）回报，不依赖本接口；本地单的预留→正式流程不受影响。
            src_row = conn.execute(
                "SELECT openid, source FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            is_local_order = bool(
                src_row
                and (str(src_row["openid"] or "") == "local"
                     or str(src_row["source"] or "") in ("", "local"))
            )
            if not is_local_order:
                conn.close()
                try:
                    app.logger.warning(
                        "local_orders 拒绝改写云端订单来源: order_number=%s openid=%s source=%s",
                        order_number,
                        src_row["openid"] if src_row else "",
                        src_row["source"] if src_row else "",
                    )
                except Exception:
                    pass
                return jsonify({
                    "success": True,
                    "message": "该订单号属于云端订单，已忽略本地来源改写",
                    "order_id": order_id,
                    "ignored": True,
                })

            # 更新占位记录为正式订单（reserved 占位 → reserved 完整 或 sent），并覆盖备注。
            # ⚠ 刻意**不写 created_at**（2026-11）：订单首次生成时刻不可被重复上报改写，
            # 详见本函数开头 created_at 口径注释。重复上报只更新内容，不动时间锚点。
            conn.execute(
                """UPDATE orders SET file = ?, total_price = ?, status = ?,
                                     source = 'local', remark = ?
                   WHERE id = ?""",
                (order_file_label, total_price, status, remark, order_id),
            )
            # 归属标记：本地工具上报时随订单写入（缺失则默认占位名/管理员自行打印，
            # 与历史无标记订单的处理保持一致）
            conn.execute(
                "UPDATE orders SET owner_name = ?, is_admin_print = ? WHERE id = ?",
                (owner_name or DEFAULT_OWNER_NAME, is_admin_print if is_admin_print is not None else 1, order_id),
            )
            # 清除旧子任务（如果有），重新插入
            conn.execute("DELETE FROM order_files WHERE order_id = ?", (order_id,))
        else:
            # 新插入（离线同步或直接调用场景）
            conn.execute(
                """INSERT INTO orders (file, copies, status, created_at, openid, order_number,
                                       total_price, source, owner_name, is_admin_print, remark)
                   VALUES (?, 1, ?, ?, 'local', ?, ?, 'local', ?, ?, ?)""",
                (order_file_label, status, created_at, order_number, total_price,
                 owner_name or DEFAULT_OWNER_NAME, is_admin_print if is_admin_print is not None else 1,
                 remark),
            )
            order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # 本地订单的开始打印时间（print_started_at）：sent 上报发生在打印启动那一刻，
        # 由服务器时钟写入（与 created_at 同源，等待时长才可信）。幂等——仅首次写入，
        # 重打/重复上报不覆盖；reserved 预留上报（复制价格时刻）不写。
        if status == "sent":
            conn.execute(
                """UPDATE orders SET print_started_at =
                       COALESCE(NULLIF(print_started_at, ''), ?)
                   WHERE id = ?""",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), order_id),
            )
            # 记录接收设备（首次写入，幂等；本地直接打印的订单归属本机）
            if report_client_id:
                conn.execute(
                    "UPDATE orders SET received_client = COALESCE(NULLIF(received_client, ''), ?) WHERE id = ?",
                    (report_client_id, order_id),
                )
            else:
                # 上报里没有任何设备身份 ⇒ 这单的「接单设备」永远补不回来（上面的幂等返回
                # 挡住了后续任何重试）。以前这里完全静默，2026-11-03 三单空白只能靠翻
                # gunicorn 日志倒推；现在留一条可 grep 的 warning，直接点名订单号。
                try:
                    _rc = conn.execute(
                        "SELECT received_client FROM orders WHERE id = ?", (order_id,)
                    ).fetchone()
                    if not (_rc["received_client"] or "").strip():
                        app.logger.warning(
                            "local_orders 收到 sent 上报但缺 client_id（接单设备将为空）: "
                            "order_number=%s status=%s keys=%s",
                            order_number, status, sorted(data.keys()),
                        )
                except Exception:
                    pass

        # 子任务状态：预留单用 'reserved'（不进 queued 推送链路，聚合也不污染为 sent），
        # 打印完成上报用 'sent'
        sub_status = "reserved" if status == "reserved" else "sent"
        for f_info in files:
            conn.execute(
                """INSERT INTO order_files (order_id, file_name, copies, page_count,
                                            total_price, status, duplex, created_at, page_range)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, f_info.get("file_name", ""), f_info.get("copies", 1),
                 f_info.get("page_count", 0), f_info.get("cost", 0),
                 sub_status, f_info.get("duplex", "on"), created_at, f_info.get("page_range", "")),
            )
        # 父单 page_count 汇总（2026-10-30 修）：旧写法从不写这一列 —— 新建单走 INSERT 建表默认值
        # （DEFAULT 1）、预留单走 UPDATE 也不带它，于是「本地打印 5 个文件」这类多文件单在结算页
        # 明细里永远显示 1 页（现场 HN20261030-0021：实际 116 页、父单记 1 页、钱只收了 6.90）。
        # 只在汇总值 > 0 时写：子行页数为 0（转换还没统计完就点了开始打印的脏数据）时宁可留下旧值，
        # 也别把父单写成 0 页。单价列（price_per_page）不动 —— 本地单的钱是各子行合计，父单没有单一单价。
        if files:
            try:
                _parent_pages = sum(int(f.get("page_count") or 0) for f in files)
            except Exception:
                _parent_pages = 0
            if _parent_pages > 0:
                conn.execute(
                    "UPDATE orders SET page_count = ? WHERE id = ?",
                    (_parent_pages, order_id),
                )
        conn.commit()
        return jsonify({"success": True, "order_id": order_id})
    except Exception as e:
        conn.rollback()
        print(f"[ERROR] {request.method} {request.path} exc={type(e).__name__}: "
              f"{_redact_paths(_scrub_secret(str(e)))[:160]}")
        return jsonify({"success": False, "message": "服务器内部错误"}), 500
    finally:
        conn.close()


@app.route("/api/local_order_status", methods=["POST"])
def local_order_status():
    """本地打印工具回报**本地订单**的失败（2026-10-23 现场：缺纸中止 / 放弃整叠后仍是「已完成」）。

    为什么需要这个接口：本地单的 `status='sent'` 是**打印启动那一刻**由 `/api/local_orders`
    写下的（单据化 + 幂等：重复上报直接跳过），那条链路**没有失败分支** —— 打印缺纸中止、
    操作员在 Windows 打印管理器里取消作业、攒批模式「放弃整叠」，订单一律留在「已完成」，
    结算页照算收益（HN20261023-0010 / -0013 现场）。云端单不受影响：它们的终态由
    print_success / print_failed 按 task_id 管，此接口对它们一律忽略。

    只允许把 `reserved` / `sent` 的**本地单**改成 `failed`：
      · 云端单（source = wechat/app）→ 忽略（按订单号改会绕过任务归属校验）；
      · `canceled` / `abandoned` / `rejected`（人工订正成"云端不要再打"的终态）→ 不覆盖；
      · `failed` → 幂等成功（重复上报不重复写）。
    父单与它的**全部**子任务一起改（`aggregate_order_status()` 口径：含 failed → failed），
    并清空子任务 `locked_at`（终态不该再被反馈超时 / 孤儿回收盯上）。

    畸形请求体（数组/字符串/数字/布尔、深嵌套 RecursionError）一律 fail-closed：
    未认证 403 / 带 token 400，绝不 500。
    """
    try:
        data = request.get_json(silent=True)
    except Exception:
        data = None
    if not isinstance(data, dict):
        data = {}
    token = _get_printer_token() or str(data.get("token") or "")
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403

    order_number = data.get("order_number")
    if not isinstance(order_number, str):
        return jsonify({"success": False, "message": "缺少 order_number"}), 400
    order_number = order_number.strip()
    if not order_number or len(order_number) > 64:
        return jsonify({"success": False, "message": "order_number 不合法"}), 400
    raw_status = data.get("status", "failed")
    status = raw_status.strip().lower() if isinstance(raw_status, str) else ""
    if status != "failed":
        return jsonify({"success": False, "message": "只支持 status=failed"}), 400
    reason = str(data.get("reason") or "")[:200]

    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, status, openid, source FROM orders WHERE order_number = ?",
                (order_number,),
            ).fetchone()
            if not row:
                conn.rollback()
                return jsonify({"success": True, "changed": False,
                                "message": "订单不存在（可能还没上报成功）"})
            order_id = row["id"]
            old_status = str(row["status"] or "")
            is_local = (str(row["openid"] or "") == "local"
                        or str(row["source"] or "") in ("", "local"))
            if not is_local:
                conn.rollback()
                try:
                    app.logger.warning(
                        "local_order_status 忽略云端订单: order_number=%s openid=%s source=%s",
                        order_number, row["openid"], row["source"])
                except Exception:
                    pass
                return jsonify({"success": True, "changed": False, "ignored": True,
                                "message": "该订单属于云端订单，已忽略（终态由 task 回报决定）"})
            if old_status in ("canceled", "abandoned", "rejected", "timeout"):
                conn.rollback()
                return jsonify({"success": True, "changed": False, "order_id": order_id,
                                "message": f"订单已是终态 {old_status}，不覆盖"})
            if old_status == "failed":
                conn.rollback()
                return jsonify({"success": True, "changed": False, "order_id": order_id,
                                "message": "订单已是失败态"})
            # 2026-11：客户端带上来的失败原因落进子任务 reject_reason —— 原先解析了却直接丢弃，
            # 用户只看到"打印失败"四个字，查不出是缺纸、卡纸还是驱动报错。
            conn.execute(
                "UPDATE order_files SET status = 'failed', locked_at = '', reject_reason = ?"
                " WHERE order_id = ?",
                (reason, order_id),
            )
            conn.execute("UPDATE orders SET status = 'failed' WHERE id = ?", (order_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    print(f"  [LOCAL-FAIL] 本地订单 {order_number} #{order_id} {old_status} → failed（{reason}）")
    return jsonify({"success": True, "changed": True, "order_id": order_id,
                    "previous_status": old_status, "status": "failed"})


# ==================== 用户身份接口 ====================


@app.route("/api/logout_all", methods=["POST"])
@login_required
def logout_all_devices():
    """退出全部设备（S1-06）：递增 token_version ⇒ 该账号所有已签发的 token 立即失效。

    这是"会话可吊销"的唯一入口：不需要记住 jti 清单，也不需要服务端会话表 ——
    token 里带 ver，与 users.token_version 不一致即视为已吊销。
    """
    ver = _bump_token_version(g.openid)
    print(f"[AUTH] 用户 {g.openid[:8]}... 退出全部设备（token_version → {ver}）")
    return jsonify({"success": True, "token_version": ver, "message": "已退出全部设备"})


@app.route("/api/me", methods=["GET"])
@login_required
def get_me():
    """返回当前用户的 openid、角色、临时授权信息（含许可密钥详情）"""
    role = compute_role(g.openid)
    temp_until = None
    has_temp_access = False
    license_info = None
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_user_db()
    row = conn.execute(
        "SELECT temp_until, role, theme_mode FROM users WHERE openid = ?", (g.openid,)
    ).fetchone()
    if row and row["temp_until"]:
        temp_until = row["temp_until"]
        has_temp_access = row["temp_until"] > now_str
    # 查询关联的许可密钥：temp 用户或通过许可密钥升级的 admin
    need_license = has_temp_access or (
        row and row["role"] == "admin" and not is_admin(g.openid)
    )
    if need_license:
        lk_row = conn.execute(
            """SELECT lk.key, lk.created_by, lk.expires_at, lk.type,
                      lk.created_at, lk.used_at, lk.validity_minutes,
                      u.nickname AS creator_nickname
               FROM license_keys lk
               LEFT JOIN users u ON lk.created_by = u.openid
               WHERE lk.used_by = ?
               ORDER BY lk.id DESC LIMIT 1""",
            (g.openid,),
        ).fetchone()
        if lk_row:
            # 当前角色是管理员（永久授权）或密钥本身是 admin 类型 → 视为永久；
            # 只有临时许可才按到期时间判断是否过期。
            is_permanent = (role == "admin") or (lk_row["type"] == "admin")
            license_info = {
                "key": lk_row["key"],
                "type": lk_row["type"] or "temp",
                "creator_openid": lk_row["created_by"],
                "creator_nickname": lk_row["creator_nickname"] or "",
                "created_at": lk_row["created_at"],
                "used_at": lk_row["used_at"],
                "expires_at": lk_row["expires_at"],
                "validity_minutes": lk_row["validity_minutes"],
                "expired": False if is_permanent else not (
                    lk_row["expires_at"] and lk_row["expires_at"] > now_str
                ),
            }
    conn.close()
    is_super = SUPER_ADMIN_OPENID and g.openid == SUPER_ADMIN_OPENID
    theme_mode = (row["theme_mode"] if row and row["theme_mode"] else "auto") if row else "auto"
    return jsonify({
        "success": True,
        "openid": g.openid,
        "is_admin": role == "admin",
        "is_super_admin": bool(is_super),
        "role": role,
        "temp_until": temp_until,
        "has_temp_access": has_temp_access,
        "license_info": license_info,
        "theme_mode": theme_mode,
    })


# ==================== 主题偏好同步 ====================


@app.route("/api/me/theme", methods=["PUT"])
@require_printer_access
def update_theme():
    """保存用户主题偏好：'auto' | 'light' | 'dark'

    S0-04：改 `require_printer_access`（guest 不再能写）。原实现 guest 设备 token 也能进来，
    而下面的分支会 INSERT 一条 users 行 ⇒ 可被用来无限建号（写库放大面）；再加每用户限速。"""
    if not _rate_limit(f"theme:{g.openid}", 30, 60):
        return jsonify({"success": False, "message": "操作过于频繁，请稍后再试"}), 429
    data = _json_dict()
    if data is None:
        return jsonify({"success": False, "message": "请提供 JSON 数据"}), 400
    theme_mode = data.get("theme_mode", "auto")
    if theme_mode not in ("auto", "light", "dark"):
        return jsonify({"success": False, "message": "无效的主题模式"}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_user_db()
    existing = conn.execute("SELECT openid FROM users WHERE openid = ?", (g.openid,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE users SET theme_mode = ?, updated_at = ? WHERE openid = ?",
            (theme_mode, now, g.openid),
        )
    else:
        conn.execute(
            "INSERT INTO users (openid, theme_mode, updated_at) VALUES (?, ?, ?)",
            (g.openid, theme_mode, now),
        )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "theme_mode": theme_mode})


# ==================== 授权用户列表（管理员查看）====================


@app.route("/api/authorized_users", methods=["GET"])
@login_required
def authorized_users():
    """管理员/超级管理员查看自己的“历史授权用户”：所有被本管理员授权过的用户
    （管理员 + 临时用户，含已移除/已过期），每个用户附全部密钥记录（支持多次授权）。
    """
    role = compute_role(g.openid)
    if role != "admin":
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_user_db()
    rows = conn.execute(
        """SELECT DISTINCT lk.used_by AS openid
           FROM license_keys lk
           WHERE lk.created_by = ? AND lk.used_by IS NOT NULL""",
        (g.openid,),
    ).fetchall()
    user_openids = [r["openid"] for r in rows]

    # 从 orders.db 批量查询每个用户的最近订单和订单数
    order_stats = {}
    if user_openids:
        orders_conn = get_db()
        try:
            # 用 IN 一次查询所有用户的最近订单时间
            placeholders = ",".join("?" for _ in user_openids)
            last_rows = orders_conn.execute(
                f"""SELECT openid, MAX(created_at) AS last_order, COUNT(*) AS order_count
                    FROM orders
                    WHERE openid IN ({placeholders})
                    GROUP BY openid""",
                user_openids,
            ).fetchall()
            for lr in last_rows:
                order_stats[lr["openid"]] = {
                    "last_order": lr["last_order"] or "",
                    "order_count": lr["order_count"] or 0,
                }
            # v4.4：并入归属本地订单 —— 用户绑定收支成员后，该成员名下的本地打印订单
            # （source='local', owner_name=成员名）计入该用户的关联订单数与最近订单时间，
            # 与任务列表 /api/orders?openid= 的归并口径保持一致。
            bound_map = {}  # openid → 绑定的成员名
            for oid in user_openids:
                b = _get_bound_owner_name(oid)
                if b:
                    bound_map[oid] = b
            if bound_map:
                names = sorted(set(bound_map.values()))
                ph = ",".join("?" for _ in names)
                local_rows = orders_conn.execute(
                    f"""SELECT owner_name, MAX(created_at) AS last_order, COUNT(*) AS order_count
                        FROM orders
                        WHERE source = 'local' AND owner_name IN ({ph})
                        GROUP BY owner_name""",
                    names,
                ).fetchall()
                local_by_name = {r["owner_name"]: r for r in local_rows}
                for oid, bname in bound_map.items():
                    lr = local_by_name.get(bname)
                    if not lr:
                        continue
                    st = order_stats.setdefault(oid, {"last_order": "", "order_count": 0})
                    st["order_count"] += lr["order_count"] or 0
                    lo = lr["last_order"] or ""
                    if lo and (not st["last_order"] or lo > st["last_order"]):
                        st["last_order"] = lo
        finally:
            orders_conn.close()

    users = []
    for r in rows:
        openid = r["openid"]
        urow = conn.execute(
            "SELECT nickname, avatar_path, role, temp_until, removed_at, removed_by FROM users WHERE openid = ?",
            (openid,),
        ).fetchone()
        key_rows = conn.execute(
            """SELECT key, type, created_at, used_at, expires_at, validity_minutes, order_id, status
               FROM license_keys
               WHERE created_by = ? AND used_by = ?
               ORDER BY id DESC""",
            (g.openid, openid),
        ).fetchall()

        cur_role = compute_role(openid)
        removed = bool(urow and urow["removed_at"])
        if removed:
            status = "removed"
        elif cur_role == "admin":
            status = "permanent"
        elif urow and urow["temp_until"] and urow["temp_until"] > now_str:
            status = "active"
        else:
            status = "expired"

        types = sorted({(k["type"] or "temp") for k in key_rows})
        license_type = "admin" if "admin" in types else ("temp" if "temp" in types else "")
        license_type_label = "both" if len(types) > 1 else license_type

        records = [{
            "key": k["key"],
            "type": k["type"] or "temp",
            "created_at": k["created_at"],
            "used_at": k["used_at"],
            "expires_at": k["expires_at"],
            "validity_minutes": k["validity_minutes"],
            "order_id": k["order_id"],
            "status": k["status"],
        } for k in key_rows]
        latest_key = records[0] if records else None

        stats = order_stats.get(openid, {})
        users.append({
            "openid": openid,
            "openid_short": openid[:8] + "..." if openid else "",
            "nickname": ((urow["nickname"] if urow and urow["nickname"] else "") or "未知用户"),
            "avatar_url": get_avatar_url(openid, (urow["avatar_path"] if urow else "") or ""),
            "role": cur_role,
            "is_super": bool(SUPER_ADMIN_OPENID and openid == SUPER_ADMIN_OPENID),
            "removed": removed,
            "removed_at": (urow["removed_at"] if urow else None),
            "removed_by": (urow["removed_by"] if urow else None),
            "status": status,
            "license_type": license_type_label,
            "latest_key": latest_key,
            "records": records,
            "last_order": stats.get("last_order", ""),
            "order_count": stats.get("order_count", 0),
        })
    conn.close()
    # 按最近使用密钥时间降序排列
    users.sort(key=lambda u: (u["latest_key"] or {}).get("used_at") or "", reverse=True)

    return jsonify({"success": True, "users": users, "count": len(users)})


@app.route("/api/admin/temp_users", methods=["GET"])
@login_required
def admin_temp_users():
    """“已临时授权的普通用户”：本管理员创建的临时密钥所授权、且未被移除的普通用户。
    卡片仅用于展示 + 左滑移除（无跳转）。
    """
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_user_db()
    rows = conn.execute(
        """SELECT DISTINCT lk.used_by AS openid
           FROM license_keys lk
           WHERE lk.created_by = ? AND lk.type = 'temp' AND lk.used_by IS NOT NULL""",
        (g.openid,),
    ).fetchall()

    users = []
    for r in rows:
        openid = r["openid"]
        urow = conn.execute(
            "SELECT nickname, avatar_path, temp_until, removed_at, role FROM users WHERE openid = ?",
            (openid,),
        ).fetchone()
        if not urow or urow["removed_at"]:
            continue
        if compute_role(openid) == "admin":
            continue  # 已是管理员 → 显示在“管理管理员”，不在此模块
        lrow = conn.execute(
            """SELECT key, type, created_at, used_at, expires_at, order_id, status
               FROM license_keys
               WHERE used_by = ? AND type = 'temp'
               ORDER BY id DESC LIMIT 1""",
            (openid,),
        ).fetchone()
        active = bool(urow["temp_until"] and urow["temp_until"] > now_str)
        users.append({
            "openid": openid,
            "openid_short": openid[:8] + "..." if openid else "",
            "nickname": (urow["nickname"] or "") or "未知用户",
            "avatar_url": get_avatar_url(openid, urow["avatar_path"] or ""),
            "status": "active" if active else "expired",
            "temp_until": urow["temp_until"],
            "license_key": (lrow["key"] if lrow else ""),
            "license_used_at": (lrow["used_at"] if lrow else ""),
            "license_expires_at": (lrow["expires_at"] if lrow else ""),
            "order_id": (lrow["order_id"] if lrow else None),
        })
    conn.close()
    users.sort(key=lambda u: u["license_used_at"] or "", reverse=True)
    return jsonify({"success": True, "users": users, "count": len(users)})


@app.route("/api/admin/remove_user", methods=["POST"])
@login_required
def admin_remove_user():
    """管理员/超级管理员手动移除“已临时授权的普通用户”：
    清除其临时授权并记录移除信息（removed_at/removed_by），密钥记录保留。
    普通管理员只能移除自己授权过的用户；超级管理员可移除任意用户。
    """
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    data = request.get_json() or {}
    target_openid = (data.get("openid", "") or "").strip()
    if not target_openid:
        return jsonify({"success": False, "message": "缺少 openid 参数"}), 400

    if not (SUPER_ADMIN_OPENID and g.openid == SUPER_ADMIN_OPENID):
        conn = get_user_db()
        try:
            licensed = conn.execute(
                "SELECT 1 FROM license_keys WHERE created_by = ? AND used_by = ? AND type = 'temp' LIMIT 1",
                (g.openid, target_openid),
            ).fetchone()
        finally:
            conn.close()
        if not licensed:
            return jsonify({"success": False, "message": "无权移除该用户"}), 403

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_user_db()
    try:
        conn.execute(
            "UPDATE users SET temp_until = NULL, removed_at = ?, removed_by = ?, updated_at = ? WHERE openid = ?",
            (now_str, g.openid, now_str, target_openid),
        )
        conn.commit()
    finally:
        conn.close()
    print(f"管理员 {g.openid[:8]}... 移除了临时用户 {target_openid[:8]}...")
    return jsonify({"success": True, "message": "已移除该用户"})


@app.route("/api/admin/user_detail", methods=["GET"])
@login_required
def admin_user_detail():
    """管理员/超级管理员查看指定用户的卡片信息（头像、昵称、角色、许可密钥详情）。
    超级管理员可查任意用户；普通管理员只能查看自己授权过的用户（保护隐私）。
    license_info 与 /api/me 一致：取该用户最近一条已使用的密钥（不限是否过期），
    并附带创建时间/使用时间/有效期/是否过期等详细字段。
    """
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    openid = _target_openid_arg()
    if not openid:
        return jsonify({"success": False, "message": "缺少 openid 参数"}), 400

    # 普通管理员只能查看自己创建密钥授权过的用户
    if not (SUPER_ADMIN_OPENID and g.openid == SUPER_ADMIN_OPENID):
        conn = get_user_db()
        try:
            licensed = conn.execute(
                "SELECT 1 FROM license_keys WHERE created_by = ? AND used_by = ? LIMIT 1",
                (g.openid, openid),
            ).fetchone()
        finally:
            conn.close()
        if not licensed:
            return jsonify({"success": False, "message": "无权查看该用户"}), 403

    conn = get_user_db()
    try:
        urow = conn.execute(
            "SELECT nickname, avatar_path FROM users WHERE openid = ?", (openid,)
        ).fetchone()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lk_row = conn.execute(
            """SELECT lk.key, lk.type, lk.created_at, lk.used_at, lk.expires_at,
                      lk.validity_minutes, lk.created_by,
                      u2.nickname AS creator_nickname
               FROM license_keys lk
               LEFT JOIN users u2 ON lk.created_by = u2.openid
               WHERE lk.used_by = ?
               ORDER BY lk.id DESC LIMIT 1""",
            (openid,),
        ).fetchone()
    finally:
        conn.close()

    target_role = compute_role(openid)
    license_info = None
    if lk_row:
        # 用户当前是管理员（永久授权）或密钥本身是 admin 类型 → 视为永久；
        # 只有临时许可才按到期时间判断是否过期。
        is_permanent = (target_role == "admin") or (lk_row["type"] == "admin")
        license_info = {
            "key": lk_row["key"],
            "type": lk_row["type"] or "temp",
            "creator_openid": lk_row["created_by"],
            "creator_nickname": lk_row["creator_nickname"] or "",
            "created_at": lk_row["created_at"],
            "used_at": lk_row["used_at"],
            "expires_at": lk_row["expires_at"],
            "validity_minutes": lk_row["validity_minutes"],
            "expired": False if is_permanent else not (
                lk_row["expires_at"] and lk_row["expires_at"] > now_str
            ),
        }

    return jsonify({
        "success": True,
        "openid": openid,
        "nickname": (urow["nickname"] if urow and urow["nickname"] else "") or "微信用户",
        "avatar_url": get_avatar_url(openid, (urow["avatar_path"] if urow else "") or ""),
        "role": target_role,
        "is_super": bool(SUPER_ADMIN_OPENID and openid == SUPER_ADMIN_OPENID),
        "license_info": license_info,
    })


# ==================== 用户资料接口 ====================


@app.route("/api/profile", methods=["GET"])
@login_required
def get_profile():
    """获取当前用户的头像和昵称"""
    # S1-08：每用户限速（APP 启动/切页会拉，60/分 足够）
    if not _rate_limit_ok(f"profile_get:{g.openid}", 60, 60):
        return jsonify({"success": False, "message": "请求过于频繁，请稍后再试"}), 429
    conn = get_user_db()
    row = conn.execute(
        "SELECT nickname, avatar_path FROM users WHERE openid = ?",
        (g.openid,),
    ).fetchone()
    conn.close()

    if row:
        nickname = row["nickname"] or ""
        avatar_url = get_avatar_url(g.openid, row["avatar_path"])
        return jsonify({
            "success": True,
            "nickname": nickname,
            "avatar_url": avatar_url,
        })
    else:
        return jsonify({
            "success": True,
            "nickname": "",
            "avatar_url": "",
        })


@app.route("/api/profile", methods=["POST"])
@login_required
def update_profile():
    """更新用户昵称和头像（支持 JSON 或 multipart）"""
    # S0-04：**碰 request.files 之前**先看 Content-Length（低于全局 50 MB 也直接 413）。
    # 头像本身上限 2 MB；原实现要先解析完 multipart（可能落临时文件）才检查大小。
    if (request.content_length or 0) > 3 * 1024 * 1024:
        return jsonify({"success": False, "message": "请求体过大"}), 413
    # S1-08：每用户限速（改昵称/头像 30/分 足够；也挡住"反复写库"的放大面）
    if not _rate_limit_ok(f"profile_post:{g.openid}", 30, 60):
        return jsonify({"success": False, "message": "操作过于频繁，请稍后再试"}), 429
    nickname = ""
    avatar_file = None

    if request.is_json:
        data = _json_dict()
        nickname = (data or {}).get("nickname", "")
    else:
        nickname = request.form.get("nickname", "")
        avatar_file = request.files.get("avatar")

    avatar_path = None
    if avatar_file and avatar_file.filename:
        # P0-2：大小校验（≤ 2MB），超限拒绝，避免头像填盘
        avatar_file.seek(0, 2)
        avatar_size = avatar_file.tell()
        avatar_file.seek(0)
        if avatar_size > _AVATAR_MAX_SIZE:
            return jsonify({"success": False, "message": "头像文件不能超过 2MB"}), 400

        # P0-2：魔数校验（jpeg/png/webp/gif），非图片文件直接拒绝
        if not _is_valid_avatar(avatar_file.stream):
            return jsonify({"success": False, "message": "头像文件格式不支持（仅支持 jpg/png/webp/gif）"}), 400

        ext = os.path.splitext(avatar_file.filename)[1] or ".jpg"
        saved_name = f"{g.openid}{ext}"
        file_path = os.path.join(AVATAR_DIR, saved_name)
        avatar_file.save(file_path)
        avatar_path = compress_avatar(file_path)
        if not avatar_path:
            # P0-2：PIL 打不开（损坏/解压炸弹等）→ 删除已保存文件并拒绝，不再原样保留
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except OSError:
                pass
            return jsonify({"success": False, "message": "头像文件无法处理，请更换图片"}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_user_db()
    existing = conn.execute("SELECT openid FROM users WHERE openid = ?", (g.openid,)).fetchone()

    if existing:
        if avatar_path:
            conn.execute(
                "UPDATE users SET nickname = ?, avatar_path = ?, updated_at = ? WHERE openid = ?",
                (nickname, avatar_path, now, g.openid),
            )
        else:
            conn.execute(
                "UPDATE users SET nickname = ?, updated_at = ? WHERE openid = ?",
                (nickname, now, g.openid),
            )
    else:
        conn.execute(
            "INSERT INTO users (openid, nickname, avatar_path, updated_at) VALUES (?, ?, ?, ?)",
            (g.openid, nickname, avatar_path or "", now),
        )
    conn.commit()

    # 查询最终状态，正确返回 avatar_url
    final = conn.execute(
        "SELECT nickname, avatar_path FROM users WHERE openid = ?", (g.openid,)
    ).fetchone()
    conn.close()

    final_avatar = final["avatar_path"] if final else ""
    avatar_url = get_avatar_url(g.openid, final_avatar)

    return jsonify({
        "success": True,
        "nickname": nickname or (final["nickname"] if final else ""),
        "avatar_url": avatar_url,
    })


@app.route("/api/avatar")
def get_avatar():
    """获取用户头像（无需登录，通过 openid 查询）"""
    openid = request.args.get("openid", "")
    if not openid:
        return jsonify({"success": False, "message": "缺少 openid"}), 400

    conn = get_user_db()
    row = conn.execute(
        "SELECT avatar_path FROM users WHERE openid = ?",
        (openid,),
    ).fetchone()
    conn.close()

    if row and row["avatar_path"] and os.path.exists(row["avatar_path"]):
        resp = send_file(row["avatar_path"], mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    # 返回默认头像
    default = os.path.join(os.path.dirname(__file__), "static", "default-avatar.png")
    if os.path.exists(default):
        return send_file(default, mimetype="image/png")
    return jsonify({"success": False, "message": "无头像"}), 404


# ==================== 许可密钥接口 ====================


@app.route("/api/license/create", methods=["POST"])
@login_required
def license_create():
    """管理员创建一次性限时许可密钥（1-10 分钟有效）。
    支持 type 参数: 'temp'（临时许可，默认）或 'admin'（永久管理员权限）。
    admin 类型仅超级管理员可创建。
    """
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    data = request.get_json() or {}
    key_type = (data.get("type", "temp") or "temp").strip().lower()
    if key_type not in ("temp", "admin"):
        return jsonify({"success": False, "message": "type 只能为 temp 或 admin"}), 400

    # admin 类型密钥仅超级管理员可创建
    if key_type == "admin":
        if not SUPER_ADMIN_OPENID or g.openid != SUPER_ADMIN_OPENID:
            return jsonify({"success": False, "message": "仅超级管理员可创建 admin 类型密钥"}), 403

    import secrets
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    conn = get_user_db()
    # 生成唯一密钥（全量查重 + 数据库 UNIQUE 约束双保险，避免任何碰撞）
    for _ in range(20):
        license_key = ''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8))
        dup = conn.execute(
            "SELECT id FROM license_keys WHERE key = ?",
            (license_key,),
        ).fetchone()
        if not dup:
            break

    # admin 和 temp 都使用 1-10 分钟倒计时（admin 被兑换后变永久，temp 兑换后临时授权）
    validity = int(data.get("validity_minutes", 5))
    validity = max(1, min(10, validity))
    expires = now + timedelta(minutes=validity)
    conn.execute(
        """INSERT INTO license_keys (key, created_by, validity_minutes, created_at, expires_at, type, status)
           VALUES (?, ?, ?, ?, ?, ?, 'unused')""",
        (license_key, g.openid, validity,
         now.strftime("%Y-%m-%d %H:%M:%S"),
         expires.strftime("%Y-%m-%d %H:%M:%S"),
         key_type),
    )
    conn.commit()
    conn.close()

    print(f"管理员 {g.openid[:8]}... 创建{key_type}许可密钥: {license_key}, 有效期 {validity} 分钟")
    return jsonify({
        "success": True,
        "key": license_key,
        "type": key_type,
        "expires_at": expires.strftime("%Y-%m-%d %H:%M:%S"),
        "validity_minutes": validity,
    })


@app.route("/api/license/redeem", methods=["POST"])
@login_required
def license_redeem():
    """用户兑换许可密钥。
    - temp 类型: 设置 users.temp_until = expires_at（临时打印权限）
    - admin 类型: 设置 users.role = 'admin'（永久管理员）
    """
    # 限速（P1-6 / 防滥用）：每用户每分钟兑换次数上限（管理员可调，单 worker 内存计数）
    if not _rate_limit(f"license_redeem:{g.openid}", _SECURITY["redeem_rate_limit"], 60):
        return jsonify({"success": False, "message": "兑换过于频繁，请稍后再试"}), 429

    data = request.get_json() or {}
    raw_key = (data.get("key", "") or "").strip().upper()
    if len(raw_key) != 8:
        return jsonify({"success": False, "message": "密钥格式不正确"}), 400

    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    conn = get_user_db()
    # 原子条件 UPDATE：仅当未使用且未过期才生效；兑换后标记 status='used'（记录永久保留）
    conn.execute(
        """UPDATE license_keys SET used_by = ?, used_at = ?, status = 'used'
           WHERE key = ? AND used_by IS NULL AND expires_at > ?""",
        (g.openid, now_str, raw_key, now_str),
    )
    conn.commit()

    if conn.total_changes == 0:
        # 密钥不存在、已使用或已过期
        row = conn.execute("SELECT used_by, expires_at FROM license_keys WHERE key = ?", (raw_key,)).fetchone()
        conn.close()
        if not row:
            return jsonify({"success": False, "message": "密钥不存在"}), 404
        if row["used_by"] is not None:
            return jsonify({"success": False, "message": "密钥已被使用"}), 400
        return jsonify({"success": False, "message": "密钥已过期"}), 400

    # 读取密钥类型和过期时间
    key_row = conn.execute(
        "SELECT type, expires_at FROM license_keys WHERE key = ?", (raw_key,)
    ).fetchone()
    key_type = key_row["type"] if key_row else "temp"
    expires_at = key_row["expires_at"] if key_row else now_str

    # 根据密钥类型处理用户权限
    existing = conn.execute("SELECT openid FROM users WHERE openid = ?", (g.openid,)).fetchone()
    if key_type == "admin":
        # admin 类型: 设为管理员，清除临时授权；同时清除历史移除标记（重新授权）
        if existing:
            conn.execute(
                "UPDATE users SET role = 'admin', temp_until = NULL, removed_at = NULL, removed_by = NULL, updated_at = ? WHERE openid = ?",
                (now_str, g.openid),
            )
        else:
            conn.execute(
                "INSERT INTO users (openid, role, temp_until, nickname, avatar_path, updated_at) VALUES (?, 'admin', NULL, '', '', ?)",
                (g.openid, now_str),
            )
    else:
        # temp 类型: 设置临时授权截止时间；同时清除历史移除标记（重新授权）
        if existing:
            conn.execute(
                "UPDATE users SET temp_until = ?, removed_at = NULL, removed_by = NULL, updated_at = ? WHERE openid = ?",
                (expires_at, now_str, g.openid),
            )
        else:
            conn.execute(
                "INSERT INTO users (openid, role, temp_until, nickname, avatar_path, updated_at) VALUES (?, 'guest', ?, '', '', ?)",
                (g.openid, expires_at, now_str),
            )
    conn.commit()
    conn.close()

    print(f"用户 {g.openid[:8]}... 成功兑换{key_type}许可密钥: {raw_key}")
    return jsonify({"success": True, "message": "许可验证成功，您已获得打印权限"})


@app.route("/api/license/active", methods=["GET"])
@login_required
def license_active():
    """查询当前管理员所有未过期的许可密钥（支持同时创建多个密钥）。
    每个密钥状态: unused（未兑换）/ used_waiting（已兑换等待提交任务）/ used_done（已提交任务）
    """
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_user_db()
    try:
        rows = conn.execute(
            """SELECT id, key, type, used_by, order_id, validity_minutes, created_at, expires_at, status
               FROM license_keys
               WHERE created_by = ? AND expires_at > ?
                 AND (status IS NULL OR status NOT IN ('revoked', 'archived', 'finished'))
               ORDER BY id DESC""",
            (g.openid, now_str),
        ).fetchall()

        if not rows:
            return jsonify({"success": True, "active": False, "keys": []})

        keys = []
        # 收集所有 used_by 用户，批量查昵称头像（避免 N+1）
        used_by_set = set()
        for row in rows:
            ub = row["used_by"]
            if ub:
                used_by_set.add(ub)
        user_info = {}
        if used_by_set:
            for ub in used_by_set:
                urow = conn.execute(
                    "SELECT nickname, avatar_path FROM users WHERE openid = ?", (ub,)
                ).fetchone()
                if urow:
                    user_info[ub] = {
                        "nickname": urow["nickname"] or "微信用户",
                        "avatar_url": get_avatar_url(ub, urow["avatar_path"] or ""),
                    }
                else:
                    user_info[ub] = {"nickname": "微信用户", "avatar_url": ""}

        # 批量查订单状态（orders.db）
        all_used = [row["used_by"] for row in rows if row["used_by"]]
        order_map = {}
        if all_used:
            orders_conn = get_db()
            try:
                # 查各用户最近订单
                for ub in set(all_used):
                    orow = orders_conn.execute(
                        "SELECT id, status, total_price FROM orders WHERE openid = ? ORDER BY id DESC LIMIT 1",
                        (ub,),
                    ).fetchone()
                    if orow:
                        order_map[ub] = {
                            "order_id": orow["id"],
                            "order_status": orow["status"],
                            "order_total_price": orow["total_price"] or 0,
                        }
            finally:
                orders_conn.close()

        for row in rows:
            r = dict(row)
            used_by = r.get("used_by") or None
            status = "unused"
            order_info = {}
            if used_by:
                oi = order_map.get(used_by)
                # 优先用 license_keys.order_id 关联的订单
                license_oid = r.get("order_id")
                if license_oid and oi and oi.get("order_id") == license_oid:
                    order_info = oi
                    status = "used_done"
                elif oi:
                    order_info = oi
                    status = "used_done"
                else:
                    status = "used_waiting"

            ui = user_info.get(used_by, {})
            keys.append({
                "key": r["key"],
                "type": r.get("type") or "temp",
                "status": status,
                "expires_at": r["expires_at"],
                "validity_minutes": r["validity_minutes"],
                "created_at": r["created_at"],
                "used_by": used_by,
                "used_by_nickname": ui.get("nickname", ""),
                "used_by_avatar_url": ui.get("avatar_url", ""),
                "order_id": order_info.get("order_id"),
                "order_status": order_info.get("order_status"),
                "order_total_price": order_info.get("order_total_price"),
            })

        return jsonify({"success": True, "active": len(keys) > 0, "keys": keys})
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[ERROR] /api/license/active 查询失败: {e}")
        return jsonify({"success": False, "message": "服务器内部错误"}), 500
    finally:
        conn.close()


@app.route("/api/license/revoke", methods=["POST"])
@login_required
def license_revoke():
    """管理员作废/归档指定的许可密钥（行保留，不影响已获得的授权）：
    - 未使用密钥 → status='revoked'（作废，他人无法再使用）
    - 已使用密钥 → status='archived'（从“活跃密钥”列表移除卡片，授权记录保留在历史授权用户中）
    body: { "key": "ABCD1234" }
    不传 key 则处理该管理员全部密钥（兼容旧版）。
    """
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    data = request.get_json() or {}
    target_key = (data.get("key", "") or "").strip().upper()

    conn = get_user_db()
    # 软处理，行永久保留：未使用 → revoked（作废）；已使用 → archived（归档，移出活跃列表）。
    if target_key:
        conn.execute(
            """UPDATE license_keys
               SET status = CASE WHEN used_by IS NULL THEN 'revoked' ELSE 'archived' END
               WHERE key = ? AND created_by = ?""",
            (target_key, g.openid),
        )
    else:
        # 兼容旧版：处理该管理员全部密钥（未使用作废，已使用归档）
        conn.execute(
            """UPDATE license_keys
               SET status = CASE WHEN used_by IS NULL THEN 'revoked' ELSE 'archived' END
               WHERE created_by = ?""",
            (g.openid,),
        )
    conn.commit()
    conn.close()

    print(f"管理员 {g.openid[:8]}... 作废/归档许可密钥: {target_key or '全部'}")
    return jsonify({"success": True})


@app.route("/api/license/finish", methods=["POST"])
@login_required
def license_finish():
    """管理员结束打印任务：查询许可证关联的订单价格详情，标记许可证为已完成。
    body: { "key": "ABCD1234" }
    """
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    data = request.get_json() or {}
    raw_key = (data.get("key", "") or "").strip().upper()

    if len(raw_key) != 8:
        return jsonify({"success": False, "message": "密钥格式不正确"}), 400

    conn = get_user_db()
    lrow = conn.execute(
        "SELECT id, key, used_by, order_id FROM license_keys WHERE key = ? AND created_by = ?",
        (raw_key, g.openid),
    ).fetchone()

    if not lrow:
        conn.close()
        return jsonify({"success": False, "message": "密钥不存在或不属于您"}), 404

    order_id = lrow["order_id"]
    price_detail = None

    if order_id:
        # 查询订单价格明细（orders / order_files 在 orders.db）
        orders_conn = get_db()
        try:
            orow = orders_conn.execute(
                "SELECT id, total_price, is_free FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            if orow:
                order = dict(orow)
                of_rows = orders_conn.execute(
                    """SELECT of.id, of.file_name, of.copies, of.page_count, of.duplex, of.page_range,
                              of.total_price, of.is_free, of.status
                       FROM order_files of WHERE of.order_id = ? ORDER BY of.id ASC""",
                    (order_id,),
                ).fetchall()
                files = []
                for of_row in of_rows:
                    f = dict(of_row)
                    f["per_copy_price"] = calculate_price(f["page_count"], f.get("duplex", "on"), f.get("page_range", ""))
                    f["unit"] = "元/张" if f.get("duplex") == "on" else "元/页"
                    files.append(f)
                price_detail = {
                    "order_id": order["id"],
                    "is_free": bool(order.get("is_free", 0)),
                    # 🔴-C4：与 /api/order_price 同口径 —— 订单级总价（含加急/首页/派送附加费），
                    # 不再用 order_files 小计相加（那会漏掉附加费，管理员结算金额偏低）
                    "total_price": round(float(order.get("total_price") or 0), 2),
                    "files": files,
                }
        finally:
            orders_conn.close()

    # 标记为已完成（status → finished，行保留作为授权记录，绝不硬删）
    conn.execute(
        "UPDATE license_keys SET status = 'finished' WHERE id = ?",
        (lrow["id"],),
    )
    conn.commit()
    conn.close()

    print(f"管理员 {g.openid[:8]}... 结束了密钥 {raw_key} 的打印任务")
    return jsonify({
        "success": True,
        "message": "任务已结束",
        "price_detail": price_detail,
    })


def cleanup_expired_license_keys():
    """清理已过期且未使用的许可密钥 / 个人认证密钥（超过 1 小时）"""
    cutoff = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_user_db()
    conn.execute("DELETE FROM license_keys WHERE used_by IS NULL AND expires_at < ?", (cutoff,))
    conn.execute("DELETE FROM bind_keys WHERE status = 'unused' AND expires_at < ?", (cutoff,))
    conn.commit()
    conn.close()


@app.route("/api/admin/users", methods=["GET"])
@login_required
def admin_users_list():
    """管理员查看所有普通用户列表（含头像、昵称、许可时间）"""
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "仅限管理员操作"}), 403

    conn = get_user_db()
    rows = conn.execute(
        """
        SELECT u.openid, u.nickname, u.avatar_path,
               (SELECT lk.key FROM license_keys lk
                WHERE lk.used_by = u.openid
                ORDER BY lk.used_at DESC LIMIT 1) as license_key,
               (SELECT lk.used_at FROM license_keys lk
                WHERE lk.used_by = u.openid
                ORDER BY lk.used_at DESC LIMIT 1) as licensed_at
        FROM users u
        WHERE u.role = 'user'
        ORDER BY licensed_at DESC
        """
    ).fetchall()
    conn.close()

    users = []
    for row in rows:
        entry = dict(row)
        entry["nickname"] = entry.get("nickname") or "微信用户"
        entry["avatar_url"] = get_avatar_url(entry["openid"], entry.get("avatar_path") or "")
        entry["licence_key"] = entry.get("license_key") or ""
        entry["licensed_at"] = entry.get("licensed_at") or ""
        users.append(entry)

    return jsonify({
        "success": True,
        "users": users,
        "count": len(users),
    })


# ==================== 管理员：管理员列表/移除 ====================


@app.route("/api/admin/admins", methods=["GET"])
@login_required
def admin_admins_list():
    """超级管理员查看所有管理员列表（含昵称、头像、openid）。
    支持可选分页参数: page（页码，1 起始）、page_size（每页条数，默认 20）。
    """
    if not SUPER_ADMIN_OPENID or g.openid != SUPER_ADMIN_OPENID:
        return jsonify({"success": False, "message": "仅限超级管理员操作"}), 403

    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 20, type=int)
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    offset = (page - 1) * page_size

    conn = get_user_db()
    # 统计总数
    total = conn.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin'"
    ).fetchone()[0]

    rows = conn.execute(
        """SELECT openid, nickname, avatar_path, updated_at
           FROM users WHERE role = 'admin'
           ORDER BY updated_at DESC
           LIMIT ? OFFSET ?""",
        (page_size, offset),
    ).fetchall()
    conn.close()

    admins = []
    for row in rows:
        entry = dict(row)
        entry["nickname"] = entry.get("nickname") or "微信用户"
        entry["avatar_url"] = get_avatar_url(entry["openid"], entry.get("avatar_path") or "")
        entry["is_super"] = (entry["openid"] == SUPER_ADMIN_OPENID)
        admins.append(entry)

    return jsonify({
        "success": True,
        "admins": admins,
        "count": len(admins),
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@app.route("/api/admin/remove_admin", methods=["POST"])
@login_required
def admin_remove_admin():
    """超级管理员移除某个管理员（将其 role 改为 guest，清除 temp_until，并记录移除信息）。
    不能移除超级管理员自己。
    被移除的管理员会出现在超级管理员的“历史授权用户”中（状态=已移除）。
    """
    if not SUPER_ADMIN_OPENID or g.openid != SUPER_ADMIN_OPENID:
        return jsonify({"success": False, "message": "仅限超级管理员操作"}), 403

    data = request.get_json() or {}
    target_openid = (data.get("openid", "") or "").strip()

    if not target_openid:
        return jsonify({"success": False, "message": "缺少 openid 参数"}), 400

    if target_openid == g.openid:
        return jsonify({"success": False, "message": "不能移除超级管理员自己"}), 400

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_user_db()
    existing = conn.execute(
        "SELECT role FROM users WHERE openid = ? AND role = 'admin'",
        (target_openid,),
    ).fetchone()
    if not existing:
        conn.close()
        return jsonify({"success": False, "message": "用户不是管理员或不存在"}), 404

    conn.execute(
        "UPDATE users SET role = 'guest', temp_until = NULL, removed_at = ?, removed_by = ?, updated_at = ? WHERE openid = ?",
        (now_str, g.openid, now_str, target_openid),
    )
    # 若该管理员没有任何 admin 类型密钥记录（历史硬删导致）→ 补一条归档记录，
    # 保证“移除的管理员必进历史授权用户、授权记录不凭空消失”。
    has_admin_key = conn.execute(
        "SELECT 1 FROM license_keys WHERE used_by = ? AND type = 'admin' LIMIT 1",
        (target_openid,),
    ).fetchone()
    if not has_admin_key:
        import uuid
        archive_key = 'ARCHIVED' + uuid.uuid4().hex[:6].upper()
        conn.execute(
            """INSERT INTO license_keys
               (key, created_by, used_by, validity_minutes, created_at, expires_at, used_at, type, status)
               VALUES (?, ?, ?, 0, ?, ?, ?, 'admin', 'archived')""",
            (archive_key, g.openid, target_openid, now_str, now_str, now_str),
        )
    conn.commit()
    conn.close()

    print(f"超级管理员 {g.openid[:8]}... 移除了管理员 {target_openid[:8]}...")
    return jsonify({"success": True, "message": "已移除该管理员权限"})


# ==================== 管理员：存储统计 ====================


def _storage_live_subtasks():
    """返回「还在等文件」的子任务（status, 个数）与几个订单号样本。

    D5 第 8 条：/api/admin/storage 的 DELETE 会把 UPLOAD_DIR 里的文件删光并清空
    `files.path`，却不检查有没有**在飞/预约**的子任务 —— 预约单的文件到点前一直躺在服务器上，
    清空后到点必然「文件不存在」，订单永久打不出来（旧实现连二次确认都没有）。
    判据取**白名单式排除**（只把明确已终结的状态排除），未知状态一律算"还在等"，宁可拦住也不误删。
    """
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT status, COUNT(*) AS n FROM order_files
               WHERE status NOT IN ('sent', 'failed', 'timeout', 'canceled', 'rejected', 'abandoned')
               GROUP BY status ORDER BY n DESC"""
        ).fetchall()
        samples = [r[0] for r in conn.execute(
            """SELECT DISTINCT o.order_number FROM order_files of
               JOIN orders o ON of.order_id = o.id
               WHERE of.status NOT IN ('sent', 'failed', 'timeout', 'canceled', 'rejected', 'abandoned')
                 AND o.order_number != ''
               ORDER BY of.id DESC LIMIT 5"""
        ).fetchall()]
        return [dict(r) for r in rows], samples
    finally:
        conn.close()


def _format_size(size_bytes):
    """将字节数格式化为人类可读的字符串"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


@app.route("/api/admin/storage", methods=["GET", "POST", "DELETE"])
@login_required
def admin_storage():
    """管理员查看/设置服务器缓存文件统计与保留时间"""
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "需要管理员权限"}), 403

    # ---- DELETE: 删除全部缓存文件 ----
    if request.method == "DELETE":
        # D5 第 8 条：删之前先看有没有"还在等文件"的子任务（预约单/排队单）。
        # 默认**拒绝**（409）并把风险如实报出来；确实要清就显式带 ?force=1（第二道确认）。
        _force = (request.args.get("force", "") or "").strip().lower() in ("1", "true", "yes")
        _live_rows, _live_samples = _storage_live_subtasks()
        _live_total = sum(r["n"] for r in _live_rows)
        if _live_total and not _force:
            _detail = "、".join(f"{r['status']} {r['n']} 个" for r in _live_rows)
            _sample_txt = f"（例如订单 {'、'.join(_live_samples)}）" if _live_samples else ""
            return jsonify({
                "success": False,
                "code": "storage_in_use",
                "message": (f"有 {_live_total} 个子任务尚未完成（{_detail}）{_sample_txt}，"
                            "它们的文件还在服务器上：现在清空会让这些订单（如预约单）到点打不出来。"
                            "请先等它们打完/取消，或显式带 ?force=1 强制清空。"),
                "live_tasks": _live_total,
                "live_by_status": {r["status"]: r["n"] for r in _live_rows},
                "samples": _live_samples,
            }), 409

        deleted_count = 0
        deleted_size = 0

        md5_index = load_md5_index()

        for root, dirs, files in os.walk(UPLOAD_DIR):
            # 跳过 avatars 子树（用户头像）
            if os.path.basename(root) == "avatars":
                dirs[:] = []  # 阻止继续递归子目录
                continue
            for fname in files:
                # 跳过配置文件
                if fname in ("md5_index.json", "retention_config.json", "security_config.json"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    size = os.path.getsize(fpath)
                    os.remove(fpath)
                    deleted_count += 1
                    deleted_size += size
                except OSError as e:
                    print(f"  [DELETE-ALL] 删除失败 {_rel_display(fpath)}: {type(e).__name__}")

        # 清空 MD5 索引
        save_md5_index({})

        # 清空 files 表中的路径引用
        conn = get_db()
        conn.execute("UPDATE files SET path = '', size = 0")
        conn.commit()
        conn.close()

        # 同步通知所有在线本地打印工具清空 PDF 缓存
        _notify_clients("clear_local_cache", {
            "message": f"管理员清空了服务器缓存 ({deleted_count} 个文件)",
        })

        print(f"  [DELETE-ALL] 已删除 {deleted_count} 个文件, 释放 {_format_size(deleted_size)}")
        if _live_total and _force:
            print(f"  [DELETE-ALL] ⚠ 强制清空：波及 {_live_total} 个未完成子任务"
                  f"（{ {r['status']: r['n'] for r in _live_rows} }）")
        return jsonify({
            "success": True,
            "message": f"已删除 {deleted_count} 个文件"
                       + (f"（强制清空，波及 {_live_total} 个未完成子任务）" if _live_total else ""),
            "deleted_count": deleted_count,
            "deleted_size_display": _format_size(deleted_size),
            "live_tasks_deleted": _live_total,
        })

    # ---- POST: 设置保留时间 ----
    if request.method == "POST":
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "message": "请提供 JSON 数据"}), 400

        days = data.get("retention_days", None)
        hours = data.get("retention_hours", None)

        if days is None or hours is None:
            return jsonify({"success": False, "message": "请提供 retention_days 和 retention_hours"}), 400

        try:
            days = int(days)
            hours = int(hours)
        except (ValueError, TypeError):
            return jsonify({"success": False, "message": "天数/小时数必须为整数"}), 400

        if days < 0 or days > 365:
            return jsonify({"success": False, "message": "天数范围: 0-365"}), 400
        if hours < 0 or hours > 23:
            return jsonify({"success": False, "message": "小时数范围: 0-23"}), 400

        cfg = {"days": days, "hours": hours}
        save_retention_config(cfg)

        # 保存后立即执行一次清理
        cleanup_expired_files()

        # 同步通知所有在线本地打印工具更新缓存保留时间
        _notify_clients("storage_config_updated", {
            "retention_days": days,
            "retention_hours": hours,
        })

        return jsonify({"success": True, "message": "保留时间已更新"})

    # ---- GET: 查看存储统计 + 保留时间 ----
    total_files = 0
    total_size = 0

    for root, dirs, files in os.walk(UPLOAD_DIR):
        # 跳过 avatars 子树（用户头像不参与缓存统计）
        if os.path.basename(root) == "avatars":
            dirs[:] = []  # 阻止继续递归
            continue
        for fname in files:
            # 跳过配置文件
            if fname in ("md5_index.json", "retention_config.json", "order_counter.json", "security_config.json"):
                continue
            fpath = os.path.join(root, fname)
            try:
                stat = os.stat(fpath)
                total_files += 1
                total_size += stat.st_size
            except OSError:
                pass

    cfg = load_retention_config()

    return jsonify({
        "success": True,
        "total_files": total_files,
        "total_size": total_size,
        "total_size_display": _format_size(total_size),
        "retention_days": cfg["days"],
        "retention_hours": cfg["hours"],
    })


@app.route("/api/admin/security", methods=["GET", "POST"])
@login_required
def admin_security():
    """超管/管理员查看、调整防滥用（DDoS 防护）阈值。
    与 /api/admin/storage 同款模式：GET 返回当前阈值，POST 校验并保存到 security_config.json，
    保存后内存副本即时生效（限速/配额/磁盘守卫/排队超时均读取 _SECURITY）。"""
    if compute_role(g.openid) != "admin":
        return jsonify({"success": False, "message": "需要管理员权限"}), 403

    if request.method == "POST":
        data = request.get_json() or {}
        cfg = dict(_SECURITY)
        for key, (lo, hi) in SECURITY_RANGES.items():
            if key not in data or data[key] is None:
                continue
            try:
                v = int(data[key])
            except (TypeError, ValueError):
                return jsonify({"success": False, "message": f"{key} 必须为整数"}), 400
            if not (lo <= v <= hi):
                return jsonify({"success": False, "message": f"{key} 范围: {lo}-{hi}"}), 400
            cfg[key] = v
        save_security_config(cfg)
        return jsonify({"success": True, "message": "防滥用阈值已更新"})

    # GET：返回当前阈值
    cfg = dict(_SECURITY)
    return jsonify({"success": True, **cfg})


# ---------- 「最近 N 条订单」速览（2026-11）----------
# 现场要一块"最近打了什么"的卡片：跨所有用户、只给最近 N 条、不提供翻页 —— 目的是让管理员
# 一眼看到全站最新动态，而不是把 /api/orders 的权限放大成"能查全站历史"。N 由超管设置
# （/api/admin/recent_orders_limit，默认 10，范围 1..100，落 uploads/recent_orders_config.json）。


def _recent_orders_owner(row, profiles):
    """订单「谁下的」显示名（脱敏由调用方用 _mask_person_name 做，这里给真名）。

    · source='local'（本机打印工具直打）没有用户账号，owner 就是 orders.owner_name
      （打印时盖章的成员名）；连这个都没有 → "本机打印"。
    · 云端单（wechat/app）优先 users.nickname；没填昵称才退回 orders.owner_name
      （那通常是执行打印的管理员），最后退回 openid 末四位。
    """
    source = str(row["source"] or "")
    openid = str(row["openid"] or "")
    owner_name = str(row["owner_name"] or "").strip()
    if source == "local" or openid in ("", "local"):
        return owner_name or "本机打印"
    nick = str((profiles.get(openid) or {}).get("nickname") or "").strip()
    if nick:
        return nick
    if owner_name:
        return owner_name
    return ("用户" + openid[-4:]) if len(openid) >= 4 else "微信用户"


def _order_view_dict(conn, r, files, profiles):
    """把一行 orders + 它的子任务行组装成一张「订单卡」（最近订单 / 小印查看接口共用）。

    口径唯一（硬规矩 7）：页数/张数走 `_file_range_stats`（v7.2 唯一口径），
    金额读库值不重算（重算会把人工改价/免单的历史单算歪），
    状态用 `aggregate_order_status` 聚合子任务，取不到才退回 orders.status。
    """
    oid = r["id"]
    file_rows = []
    if files:
        page_count = 0
        sheet_count = 0
        for f in files:
            st = _file_range_stats(f.get("page_count") or 1,
                                   f.get("copies") or 1,
                                   f.get("duplex") or "off",
                                   f.get("page_range") or "",
                                   f.get("simplex_pages") or "")
            page_count += st["total_pages"]
            sheet_count += st["total_sheets"]
            _name = str(f.get("file_name") or "").strip() or "未命名文件"
            _dup = str(f.get("duplex") or "off").strip().lower()
            file_rows.append({
                "file_name": _name,
                "copies": int(f.get("copies") or 1),
                "page_count": st["total_pages"],
                "sheet_count": st["total_sheets"],
                "duplex": "on" if _dup not in ("", "off", "0", "false", "none") else "off",
                "duplex_label": "单面" if _dup in ("", "off", "0", "false", "none") else "双面",
            })
    else:
        # 老单 / 子任务被自动清理过的兜底：按订单自身字段算 1 个文件，口径同上
        st = _file_range_stats(r["page_count"] or 1, r["copies"] or 1,
                               r["duplex"] or "off")
        page_count = st["total_pages"]
        sheet_count = st["total_sheets"]
        _dup0 = str(r["duplex"] or "off").strip().lower()
        file_rows = [{
            "file_name": "订单文件",
            "copies": int(r["copies"] or 1),
            "page_count": st["total_pages"],
            "sheet_count": st["total_sheets"],
            "duplex": "on" if _dup0 not in ("", "off", "0", "false", "none") else "off",
            "duplex_label": "单面" if _dup0 in ("", "off", "0", "false", "none") else "双面",
        }]
    owner = _recent_orders_owner(r, profiles)
    return {
        "order_id": oid,
        "order_number": r["order_number"] or "",
        "owner": owner,
        "owner_masked": _mask_person_name(owner) if owner else "",
        "created_at": r["created_at"] or "",
        "file_count": len(files) or 1,
        "page_count": page_count,
        "sheet_count": sheet_count,
        "files": file_rows,
        "total_price": round(r["total_price"] or 0.0, 2),
        "status": aggregate_order_status(conn, oid) or (r["status"] or ""),
        "source": r["source"] or "wechat",
        "is_admin_print": bool(r["is_admin_print"]),
        # 2026-11：订单级原因 + 人工订正留痕（与订单详情/结算明细同口径）
        "status_reason": next(
            (str(f.get("reject_reason") or "").strip()
             for f in files if str(f.get("reject_reason") or "").strip()), ""),
        "manual_status_by": r["manual_status_by"] or "",
        "manual_status_at": r["manual_status_at"] or "",
    }


@app.route("/api/admin/recent_orders", methods=["GET"])
@login_required
def admin_recent_orders():
    """全站订单：默认最近 N 条；带 `page`（或 `per_page`）时按页返回（仅 admin/super_admin；只读）。

    查询参数：
      · `limit=`  —— 临时覆盖"默认条数" N（夹在 1..100），不改变超管设置的默认值；
      · `page=`   —— 1 起；**带上它就切到翻页模式**，返回体里多 total/page/per_page/total_pages；
      · `per_page=` —— 每页条数，只接受 10/20/50/100，缺省 = 超管设置的默认条数（limit）。
    兼容性（2026-11 新增翻页）：**不带 page 时返回结构不变**（{success, limit, orders}），
    只额外多一个 total —— 老前端读 orders/limit 完全不受影响，不需要跟版。

    返回字段（与移动端约定，勿改名）：
      order_id / order_number / owner / owner_masked / created_at /
      file_count / page_count / total_price / status / source / is_admin_print
      + status_reason（订单级第一条非空 reject_reason）/ manual_status_by / manual_status_at
    · page_count = 有效页数合计（含份数）：与结算页订单明细同源，逐文件走
      `_file_range_stats(...)`（内部只认 `_count_pages_in_range`，v7.2 唯一口径）；
    · sheet_count = 纸张张数合计（双面折算）—— 附赠字段，方便前端显示"8 页 / 4 张"；
    · total_price 直接读库值，**不重算**（重算会把人工改价/免单的历史单算歪）；
    · status 用 aggregate_order_status 聚合子任务状态，取不到才退回 orders.status。
    """
    role = compute_role(getattr(g, "openid", "") or "")
    if role != "admin":
        return jsonify({"success": False, "message": "需要管理员权限"}), 403
    if not _rate_limit_ok(f"recent_orders:{g.openid}", 60, 60):
        return jsonify({"success": False, "message": "请求过于频繁，请稍后再试"}), 429

    lo, hi = RECENT_ORDERS_LIMIT_RANGE
    try:
        limit = int(_RECENT_ORDERS_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_RECENT_ORDERS_LIMIT
    limit = max(lo, min(hi, limit))
    raw = (request.args.get("limit") or "").strip()
    if raw:
        try:
            limit = max(lo, min(hi, int(raw)))
        except ValueError:
            return jsonify({"success": False,
                            "message": f"limit 必须为整数（{lo}-{hi}）"}), 400

    # ── 翻页参数（2026-11）──
    # 只要出现 page/per_page 就进翻页模式；两个都不带 = 老行为（最近 limit 条）。
    paging = ("page" in request.args) or ("per_page" in request.args)
    page = 1
    per_page = limit
    if paging:
        raw_page = (request.args.get("page") or "1").strip()
        try:
            page = int(raw_page)
        except ValueError:
            return jsonify({"success": False, "message": "page 必须为整数（从 1 起）"}), 400
        if page < 1:
            return jsonify({"success": False, "message": "page 必须 ≥ 1"}), 400
        raw_pp = (request.args.get("per_page") or "").strip()
        if raw_pp:
            try:
                per_page = int(raw_pp)
            except ValueError:
                return jsonify({"success": False,
                                "message": "per_page 只能取 10/20/50/100"}), 400
            if per_page not in RECENT_ORDERS_PER_PAGE_CHOICES:
                return jsonify({"success": False,
                                "message": "per_page 只能取 10/20/50/100"}), 400
        # 越界页不报错：返回空列表 + 真实 total_pages，前端自己收敛页码
        offset = (page - 1) * per_page

    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        if paging:
            rows = conn.execute(
                "SELECT id, order_number, openid, owner_name, source, created_at, "
                "copies, page_count, duplex, total_price, status, is_admin_print, "
                "manual_status_by, manual_status_at "
                "FROM orders ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (per_page, offset)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, order_number, openid, owner_name, source, created_at, "
                "copies, page_count, duplex, total_price, status, is_admin_print, "
                "manual_status_by, manual_status_at "
                "FROM orders ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        files_by_order = {r["id"]: [] for r in rows}
        if files_by_order:
            ids = list(files_by_order.keys())
            ph = ",".join("?" * len(ids))
            for f in conn.execute(
                f"SELECT order_id, page_count, copies, duplex, page_range, simplex_pages, "
                f"reject_reason, file_name, file_id "
                f"FROM order_files WHERE order_id IN ({ph}) ORDER BY order_id, id",
                ids
            ).fetchall():
                files_by_order.setdefault(f["order_id"], []).append(dict(f))
        profiles = {}
        openids = sorted({str(r["openid"] or "") for r in rows
                          if r["openid"] and str(r["openid"]) != "local"})
        if openids:
            # 昵称在独立的 users.db（orders.db 里没有 users 表）——与 revenue 明细同源
            user_conn = get_user_db()
            try:
                ph = ",".join("?" * len(openids))
                for u in user_conn.execute(
                    f"SELECT openid, nickname FROM users WHERE openid IN ({ph})", openids
                ).fetchall():
                    profiles[u["openid"]] = {"nickname": u["nickname"] or ""}
            finally:
                user_conn.close()
        orders = [_order_view_dict(conn, r, files_by_order.get(r["id"]) or [],
                  profiles)
                  for r in rows]
    finally:
        conn.close()
    if paging:
        total_pages = (total + per_page - 1) // per_page if per_page > 0 else 0
        print(f"[recent_orders] {getattr(g, 'openid', '')} 读取第 {page}/{total_pages} 页"
              f"（per_page={per_page}，返回 {len(orders)} 条 / 全站 {total} 条）")
        return jsonify({"success": True, "orders": orders, "total": total, "page": page,
                        "per_page": per_page, "total_pages": total_pages,
                        "limit": per_page})
    print(f"[recent_orders] {getattr(g, 'openid', '')} 读取最近 {limit} 条订单"
          f"（返回 {len(orders)} 条）")
    return jsonify({"success": True, "limit": limit, "total": total, "orders": orders})


@app.route("/api/admin/recent_orders_limit", methods=["GET", "POST"])
@login_required
def admin_recent_orders_limit():
    """「最近 N 条订单」的 N：GET 所有管理员可读，POST **仅超管**可写。

    GET  → {"success": true, "limit": 10, "range": [1, 100]}
    POST {"limit": 15} → 校验 1..100 → 原子落盘 uploads/recent_orders_config.json → 即时生效。
    ⚠ compute_role 只返回 admin/user/guest（**超管被折叠成 admin**），所以"仅超管"必须用
      SUPER_ADMIN_OPENID 判据；写成 role != "admin" 就等于"任何管理员都能改"（/api/admin/security
      那种写法在放开给普通管理员用，不能照抄）。
    """
    role = compute_role(getattr(g, "openid", "") or "")
    if role != "admin":
        return jsonify({"success": False, "message": "需要管理员权限"}), 403
    lo, hi = RECENT_ORDERS_LIMIT_RANGE
    is_super = bool(SUPER_ADMIN_OPENID) and g.openid == SUPER_ADMIN_OPENID

    if request.method == "POST":
        if not is_super:
            print(f"[recent_orders] {g.openid} 无权修改条数上限（仅超管）")
            return jsonify({"success": False,
                            "message": "仅超级管理员可以修改这个设置"}), 403
        if not _rate_limit_ok(f"recent_orders_limit:{g.openid}", 20, 60):
            return jsonify({"success": False, "message": "请求过于频繁，请稍后再试"}), 429
        data = request.get_json(silent=True) or {}
        raw = data.get("limit")
        if raw is None or isinstance(raw, bool):
            return jsonify({"success": False, "message": "limit 必须为整数"}), 400
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "limit 必须为整数"}), 400
        if not (lo <= limit <= hi):
            return jsonify({"success": False,
                            "message": f"limit 范围: {lo}-{hi}"}), 400
        try:
            save_recent_orders_config(limit)
        except OSError as e:
            print(f"[recent_orders] 保存条数失败: {e}")
            return jsonify({"success": False, "message": f"保存失败：{e}"}), 500
        print(f"[recent_orders] 超管 {g.openid} 把「最近订单」条数改为 {limit}")
        return jsonify({"success": True, "limit": limit, "range": [lo, hi],
                        "message": f"已更新为最近 {limit} 条"})

    return jsonify({"success": True, "limit": _RECENT_ORDERS_LIMIT, "range": [lo, hi]})


# ==================== 统计与报表接口 ====================


@app.route("/api/statistics/my", methods=["GET"])
@login_required
def statistics_my():
    """当前登录用户查看自己的月度打印统计（基于 order_files 聚合）"""
    _now = datetime.now()
    year = request.args.get("year", str(_now.year))
    month = request.args.get("month", str(_now.month))
    # D5：参数来自 query，**必须校验**后再用。原实现把 year/month 原样塞进 SQL 比较，
    # 末尾又 int(year)/int(month) ⇒ 任意已登录用户 GET ?year=abc 就能拿到 500。
    # 这里显式收敛（年份 1970..2100、月份 1..12；允许 "7" 这种不带前导零的写法并归一），
    # 非法值直接 400 —— 不静默回落到本月：那会给用户看**错月份**的统计，比报错更糟。
    try:
        _year_int = int(str(year).strip())
        if not (1970 <= _year_int <= 2100):
            raise ValueError
        _month_int = int(str(month).strip())
        if not (1 <= _month_int <= 12):
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"success": False,
                        "message": "参数非法：year 需为 1970-2100、month 需为 1-12"}), 400
    year = f"{_year_int:04d}"
    month = f"{_month_int:02d}"

    conn = get_db()
    row = conn.execute(
        """
        SELECT SUM(COALESCE(of_count.pages, o.page_count * o.copies)) AS total_pages,
               COUNT(DISTINCT o.id) AS total_orders
        FROM orders o
        LEFT JOIN (
            SELECT order_id,
                   SUM(page_count * copies) AS pages
            FROM order_files
            WHERE status != 'canceled'
            GROUP BY order_id
        ) of_count ON o.id = of_count.order_id
        WHERE o.openid = ?
          AND strftime('%Y', o.created_at) = ?
          AND strftime('%m', o.created_at) = ?
          AND o.status != 'canceled'
        """,
        (g.openid, year, month.zfill(2)),
    ).fetchone()
    conn.close()

    total_pages = row["total_pages"] or 0
    total_orders = row["total_orders"] or 0

    return jsonify({
        "success": True,
        "year": _year_int,
        "month": _month_int,
        "stats": {
            "total_pages": total_pages,
            "total_orders": total_orders,
        },
    })


# ==================== 订单状态人工订正（收支清算「云端数据 → 订单明细」）====================
# 现场需求（2026-10-18）：打印出错时，订单状态与实际不符（该打的没打出来却显示已完成、
# 打好了却被超时误判失败……），而状态直接决定结算口径（sent 计入收益，其余不计）。
# 原先只能人工连服务器改库（前端页面无入口）。现提供页面内订正接口。
#
# 只允许改成**终态**：queued/printing/waiting 等非终态是任务分发与预约调度的工作状态，
# 人工把订单改成它们会让 push/pull/预约调度重新把这张单捡起来打印 —— 页面上的「订正」
# 不该有这个副作用，后端用白名单一并拦下（前端只列出这几项，两侧同源）。
# 🔴-B7（2026-11-10 体检）：`timeout` 也是终态，必须可人工订正。
# 预约单到点后设备一直没接单 → 后端 expire_stale_scheduled_orders() 把它置 timeout
# （一张纸都没出，revenue 里也排除它）。若操作员后来手工打出来了，只能靠这里订正成 sent，
# 否则那笔钱永远是 0、订单永远停在"超时"。前端 ORDER_FIXABLE_STATUSES 是同一份清单。
MANUAL_FIX_STATUSES = ("sent", "failed", "abandoned", "canceled", "rejected")


def _notify_order_no_longer_printable(order_id, status, task_ids, owner_clients, target_client=""):
    """人工订正成「不该再有纸出来」的终态后，通知设备端停止/丢弃这一单（2026-10-18 审查 🔴）。

    为什么必须发这一枪：订正只落库时，设备侧在飞的四个入口（标签页任务 / 自动打印补打队列
    `_auto_print_retry` / 攒批排队 `_stack_wait` / 预约状态机 `_scheduled_orders`）照旧会把这单
    打出来 —— 纸出来了、库里却是终态不计收益，前后端都没有一行日志。这里直接复用现成的
    `order_canceled` 链路（本地工具 `_on_cloud_order_canceled` → `_purge_canceled_print_state`
    真的清掉那四处），因此**旧版客户端（≤4.5.24）不认 reason/status 也照样清理**，
    后端热部署即刻对线上存量客户端生效；`reason`/`status` 只给新版客户端做准确文案
    （顾客主动取消的事件不带这两个字段）。

    发给谁：先给「摸过这单的设备」（子任务 operator_client / last_operator_client）与订单目标设备
    （orders.target_client）中在线的；一个都不在线 → 兜底广播给在线接单设备（在无关设备上清理是空操作，
    只会多一行状态提示）。每个 sid 只发一次；通知失败只记日志，不影响订正本身的成功回执。

    **离线设备收不到这一枪，且这是被允许的**（2026-10-18 用户口径「设备是最终裁决者」）：
    它本地标签页里那单仍会被打出来 —— 那就按设备回报记 sent（on_print_success 会覆盖云端的
    人工终态）。所以这条链路的目标是「在线设备别浪费纸」，不是「绝对不许出纸」；无需为离线场景兜底。
    """
    try:
        order_no = int(order_id)
    except (TypeError, ValueError):
        return
    candidates = []
    seen = set()
    for cid in list(owner_clients or []) + ([target_client] if target_client else []):
        cid = (cid or "").strip()
        if cid and cid not in seen:
            seen.add(cid)
            candidates.append(cid)

    def _sid_of(client_id):
        with printer_clients_lock:
            info = printer_clients.get(client_id)
        return info["sid"] if info else None

    targets = []
    for cid in candidates:
        sid = _sid_of(cid)
        if sid:
            targets.append((cid, sid))
    if not targets:
        claiming = set(get_claiming_device_ids())
        for cid in get_active_clients():
            if cid in seen or cid not in claiming:
                continue
            seen.add(cid)
            sid = _sid_of(cid)
            if sid:
                targets.append((cid, sid))

    payload = {
        "order_id": order_no,
        "task_ids": list(task_ids or []),
        "reason": "manual_fix",
        "status": status,
    }
    for cid, sid in targets:
        try:
            socketio.emit("order_canceled", payload, to=sid)
            print(f"  [FIX] 已通知打印机 {cid}: 订单 #{order_no} 已订正为 {status}，本机不得再打印")
        except Exception as e:
            print(f"  [FIX] 通知打印机 {cid} 失败: {e}")
    if not targets:
        print(f"  [FIX] 订单 #{order_no} 订正为 {status}: 无在线设备可通知"
              f"（离线设备本地标签页里的这单仍需人工删除，否则仍可能被打出来）")


@app.route("/api/admin/order_status", methods=["POST"])
@require_printer_scope("admin")
def admin_order_status():
    """收支清算页人工订正订单状态（需 printer token 认证）。

    为什么父订单与子任务一起改：`aggregate_order_status()` 才是状态的权威口径
    （订单明细展示的也是聚合值），只改 orders.status 会被下一次聚合/回报刷回去 ——
    把全部子任务一并写成目标终态，聚合结果就等于目标状态（无子任务的历史老单直接改父订单）。

    locked_at 一并清空：终态不该再被 3 分钟反馈超时 / 孤儿回收 / 保活逻辑盯上。
    子任务里已 sent 的也照改（订正的是账目口径，与 abandon_order「只动非终态、语义是打印机放弃」不同）。

    **五终态不都是"最终裁决"**（2026-10-18 审查 🟡 + 用户口径）：
      · canceled / abandoned / rejected = 「云端不再主动让它打印」⇒ 提交成功后**必须通知设备端**
        （`_notify_order_no_longer_printable` 复用 order_canceled 链路，真的清掉在线设备侧在飞队列）。
        ⚠ 但它**不是**对设备的最终裁决：设备离线时收不到通知、照旧把纸打出来并如实回报成功的话，
        `on_print_success` 会按回报把它记成 sent（**打印这张单的机器说了算**，纸出来了账就要算）。
      · failed = 「本轮没成功」：设备迟到的 print_success 同样会把它救回 sent
        （2026-09 已为"迟到的成功回报被白名单挡掉 ⇒ 订单永久失败"付过代价）。
        前端订正为 failed 后不要假设库内恒为 failed，页面刷新/切口径会重新拉数。
      · sent 天然不会再被改动（除非设备真的又回报一次成功，那也是 sent）。
      · 反向不成立：设备报**失败**不会把人工终态改掉（`on_print_fail` 的白名单不含这三态与 sent）——
        那是「被取消后剩余任务回报失败」的副产物，放行只会把「已取消」标签弄成「打印失败」，与钱无关。

    畸形请求体（JSON 数组/字符串/数字/布尔）必须 fail-closed：请求体解析一次并做 dict 守卫，
    不能让鉴权表达式自己崩成 500（未认证也能 500 属可用性缺陷）。
    **深嵌套请求体**（depth ≥ 1000）会让 json.loads 抛 RecursionError —— 它不是 ValueError，
    `silent=True` 挡不住，所以解析本身也要包住（2026-10-18 审查 🟡）。
    order_id 只认**规范十进制**（纯 ASCII 数字、无前后空白、无前导零）：SQLite 的 INTEGER
    亲和性会把 '1.0' / '01' / '+1' / ' 1 ' / '1e0' 都折算成 id=1 并**真的改写订单 1**，
    「id 写错」绝不能命中另一张单。
    """
    try:
        data = request.get_json(silent=True)
    except Exception:
        # RecursionError（深嵌套）/ 解析期其它异常：当成"没有请求体"，交给下面的鉴权与参数校验
        data = None
    if not isinstance(data, dict):
        data = {}
    token = _get_printer_token() or str(data.get("token") or "")
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403

    raw_id = data.get("order_id")
    if raw_id is None or raw_id == "":
        return jsonify({"success": False, "message": "缺少 order_id"}), 400
    if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
        return jsonify({"success": False, "message": "order_id 不合法"}), 400
    # 只认**规范十进制**（纯 ASCII 数字、无前后空白、无前导零）：
    # '1.0' / '+1' / '1e0' / ' 1 ' 一律拒绝，'01' 还要单独挡 ——
    # SQLite 的 INTEGER 亲和性会把它们都折成 id=1，从而改错单（id 写错绝不能命中另一张单）。
    s_id = raw_id if isinstance(raw_id, str) else str(raw_id)
    if not re.fullmatch(r"[0-9]+", s_id) or (len(s_id) > 1 and s_id[0] == "0"):
        return jsonify({"success": False, "message": "order_id 不合法"}), 400
    order_id = s_id
    raw_status = data.get("status")
    new_status = raw_status.strip() if isinstance(raw_status, str) else ""
    if new_status not in MANUAL_FIX_STATUSES:
        return jsonify({"success": False, "message": "不允许改为该状态"}), 400
    # 2026-11：订正人留痕（可选字段 by，≤32 字符）。空/缺省 = ''（不记录操作者，
    # 兼容老版本前端只发 {order_id, status} 的请求体）。超长直接截断而不是 400 ——
    # 昵称类字段不值得为长度把整次订正打回。
    raw_by = data.get("by")
    by = raw_by.strip()[:32] if isinstance(raw_by, str) else ""
    fixed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    files_updated = 0
    old_status = ""
    task_ids = []
    owner_clients = set()
    target_client = ""
    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, target_client FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if not row:
                return jsonify({"success": False, "message": "订单不存在"}), 404
            old_status = row["status"]
            target_client = (row["target_client"] or "").strip()
            # 通知设备用的目标清单（在改写之前收集）：谁摸过这单 + 这单的目标设备。
            # 改写只动 status/locked_at，不碰 operator_client/last_operator_client，先后无差别。
            for r in conn.execute(
                "SELECT id, operator_client, last_operator_client FROM order_files WHERE order_id = ?",
                (order_id,),
            ).fetchall():
                task_ids.append(r["id"])
                for cid in (r["operator_client"], r["last_operator_client"]):
                    cid = (cid or "").strip()
                    if cid:
                        owner_clients.add(cid)
            cur = conn.execute(
                "UPDATE order_files SET status = ?, locked_at = '' WHERE order_id = ?",
                (new_status, order_id),
            )
            files_updated = cur.rowcount or 0
            # 订单级人工订正留痕（2026-11）：谁、什么时候把这单订正成了终态。
            # 与子任务 status 同事务写入 —— 半截成功（改了状态没记人）在审计上更糟。
            conn.execute(
                "UPDATE orders SET status = ?, manual_status_by = ?, manual_status_at = ?"
                " WHERE id = ?",
                (new_status, by, fixed_at, order_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    print(f"  [FIX] 订单 #{order_id} 状态人工订正: {old_status} → {new_status}"
          f"（子任务 {files_updated} 个，操作者：{by or '未记录'}）")
    if new_status in NO_LONGER_PRINTABLE_STATUSES:
        # 「这单不该再有纸出来」⇒ 必须通知设备端清掉在飞队列（否则照打、账是终态、零日志）
        _notify_order_no_longer_printable(order_id, new_status, task_ids,
                                          owner_clients, target_client)
    return jsonify({
        "success": True,
        "message": "订单状态已订正",
        "order_id": order_id,
        "status": new_status,
        "previous_status": old_status,
        "files_updated": files_updated,
        "manual_status_by": by,
        "manual_status_at": fixed_at,
    })


# ==================== 订单**类型**人工订正（接单 / 自打）====================
# 2026-10-20 用户需求：类型列也要能订正，且与「状态」共用**同一个锁**（同一密码、任一边解锁
# 两边一起解锁）。只允许「接单 / 自打」两种 —— 类型列其余槽位（自动/预约/免费/派送/加急/封面）
# 都是订单属性派生出来的，改它们等于伪造订单参数；而接单/自打只是**结算归类**：
# 自打订单不计入收益（前端 `_orderMoney(o, true)` 与全部 KPI 同口径），顾客单被误标成自打会少收钱、
# 本地自打单被误标成接单会虚增收益 —— 所以它和状态订正一样默认锁定、共用同一把锁。
#
# 与状态订正**刻意不同**（别照抄）：不改任何 status、不清 locked_at、不通知设备端 ——
# 纸打没打、还能不能打，一点没变；只写 orders.is_admin_print（order_files 没有该列，
# 展示与统计都按订单级算）。
MANUAL_FIX_ORDER_TYPES = ("receive", "self")


@app.route("/api/admin/order_type", methods=["POST"])
@require_printer_scope("admin")
def admin_order_type():
    """收支清算页人工订正订单类型（接单 / 自打；需 printer token）。

    校验与 /api/admin/order_status 同一套（dict 守卫 / 规范十进制 id / 白名单 /
    db_lock + BEGIN IMMEDIATE）——两处口径一致，畸形请求一律 fail-closed，绝不 500。
    """
    try:
        data = request.get_json(silent=True)
    except Exception:
        data = None
    if not isinstance(data, dict):
        data = {}
    token = _get_printer_token() or str(data.get("token") or "")
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403

    raw_id = data.get("order_id")
    if raw_id is None or raw_id == "":
        return jsonify({"success": False, "message": "缺少 order_id"}), 400
    if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
        return jsonify({"success": False, "message": "order_id 不合法"}), 400
    s_id = raw_id if isinstance(raw_id, str) else str(raw_id)
    if not re.fullmatch(r"[0-9]+", s_id) or (len(s_id) > 1 and s_id[0] == "0"):
        return jsonify({"success": False, "message": "order_id 不合法"}), 400

    raw_type = data.get("order_type")
    new_type = raw_type.strip() if isinstance(raw_type, str) else ""
    if new_type not in MANUAL_FIX_ORDER_TYPES:
        return jsonify({"success": False, "message": "不允许改为该类型"}), 400
    is_admin_print = 1 if new_type == "self" else 0

    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT is_admin_print FROM orders WHERE id = ?", (s_id,)
            ).fetchone()
            if not row:
                return jsonify({"success": False, "message": "订单不存在"}), 404
            prev = 1 if int(row["is_admin_print"] or 0) else 0
            conn.execute(
                "UPDATE orders SET is_admin_print = ? WHERE id = ?", (is_admin_print, s_id)
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    print(f"  [FIX] 订单 #{s_id} 类型人工订正: "
          f"{'自打' if prev else '接单'} → {'自打' if is_admin_print else '接单'}")
    return jsonify({
        "success": True,
        "message": "订单类型已订正",
        "order_id": s_id,
        "order_type": new_type,
        "previous_order_type": "self" if prev else "receive",
        "is_admin_print": is_admin_print,
    })


# 2026-11 用户需求：订单明细「用户」列也要能快捷调整（与「状态」同一把锁 / 同一个密码）。
# 与状态/类型订正的区别（别照抄状态那一套）：
#   · 改的是 orders.openid —— 订单的**实际归属用户**，小程序 / APP 的「我的订单」随新 openid 变；
#   · 不碰任何 status / locked_at / 子任务状态、不通知设备：纸打没打、还能不能打，一点没变；
#   · 只接云端订单（当前 openid 非空且 != 'local'），目标 openid 必须已存在于 users 表；
#   · files.openid 是「这份副本的上传者」（S2-05 起每次上传各自一行），**不跟着改** ——
#     那是上传者归属与保留期配额的事实记录，跟着改等于篡改审计。
@app.route("/api/admin/order_user", methods=["POST"])
@require_printer_scope("admin")
def admin_order_user():
    """收支清算页人工调整订单所属用户（需 printer token）。

    校验与 /api/admin/order_status 同一套（dict 守卫 / 规范十进制 id / db_lock +
    BEGIN IMMEDIATE）—— 两处口径一致，畸形请求一律 fail-closed，绝不 500。
    """
    try:
        data = request.get_json(silent=True)
    except Exception:
        data = None
    if not isinstance(data, dict):
        data = {}
    token = _get_printer_token() or str(data.get("token") or "")
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403

    raw_id = data.get("order_id")
    if raw_id is None or raw_id == "":
        return jsonify({"success": False, "message": "缺少 order_id"}), 400
    if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
        return jsonify({"success": False, "message": "order_id 不合法"}), 400
    s_id = raw_id if isinstance(raw_id, str) else str(raw_id)
    if not re.fullmatch(r"[0-9]+", s_id) or (len(s_id) > 1 and s_id[0] == "0"):
        return jsonify({"success": False, "message": "order_id 不合法"}), 400

    raw_openid = data.get("openid")
    new_openid = raw_openid.strip() if isinstance(raw_openid, str) else ""
    raw_owner = data.get("owner_name")
    new_owner = raw_owner.strip() if isinstance(raw_owner, str) else ""
    # v7.19：两种归属口径互斥 —— 云端订单改 openid（真的转给某个账号），本地订单改
    # owner_name（只改变展示归属，不涉及登录）。同时给或都不给一律 fail-closed。
    if new_openid and new_owner:
        return jsonify({"success": False, "message": "openid 与 owner_name 只能二选一"}), 400
    if not new_openid and not new_owner:
        return jsonify({"success": False,
                        "message": "缺少目标用户（openid 或 owner_name）"}), 400
    if new_openid:
        if (len(new_openid) > 128
                or new_openid == "local" or new_openid.startswith("dev_")
                or not re.fullmatch(r"[A-Za-z0-9_.\-]+", new_openid)):
            return jsonify({"success": False, "message": "目标用户不合法"}), 400
        target = None
    else:
        # 所属人只是一个展示名（统计/分账按它分组），没有账号可校验；只挡空值、
        # 超长和控制字符，避免把不可见字符写进 /api/admin/statistics/owners 的分组键。
        if (len(new_owner) > 32 or not new_owner
                or re.search(r"[\x00-\x1f]", new_owner)):
            return jsonify({"success": False, "message": "目标所属人不合法"}), 400

    by = data.get("by")
    by = by.strip()[:32] if isinstance(by, str) else ""
    fixed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 目标用户必须真实存在：把订单转给一个没人能登录的 openid，等于让这单从所有
    # 「我的订单」里消失（旧用户看不到、新用户不存在）—— 这才是这次调整最危险的失败态。
    new_nickname = ""
    if new_openid:
        user_conn = get_user_db()
        try:
            target = user_conn.execute(
                "SELECT openid, nickname FROM users WHERE openid = ?", (new_openid,)
            ).fetchone()
        finally:
            user_conn.close()
        if not target:
            return jsonify({"success": False, "message": "目标用户不存在"}), 404
        new_nickname = (target["nickname"] or "").strip()

    with db_lock:
        conn = get_db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT openid, owner_name FROM orders WHERE id = ?", (s_id,)
            ).fetchone()
            if not row:
                conn.rollback()
                return jsonify({"success": False, "message": "订单不存在"}), 404
            prev_openid = row["openid"] or ""
            prev_owner = row["owner_name"] or ""
            if prev_openid == "local":
                # 本地归属订单没有可转的账号：只允许改展示所属人（owner_name）。
                if not new_owner:
                    conn.rollback()
                    return jsonify({"success": False,
                                    "message": "本地归属订单不支持改云端用户，"
                                               "只能调整所属人"}), 400
                if prev_owner == new_owner:
                    conn.commit()
                    return jsonify({
                        "success": True,
                        "message": "订单已属于该所属人",
                        "order_id": s_id,
                        "owner_name": new_owner,
                        "previous_owner_name": prev_owner,
                        "manual_user_by": by,
                        "manual_user_at": "",
                    })
                conn.execute(
                    "UPDATE orders SET owner_name = ?, manual_user_by = ?, "
                    "manual_user_at = ? WHERE id = ?",
                    (new_owner, by, fixed_at, s_id),
                )
                conn.commit()
                print(f"  [FIX] 订单 #{s_id} 归属人人工调整: {prev_owner} → {new_owner}"
                      f"（by={by or '-'}）")
                return jsonify({
                    "success": True,
                    "message": "订单所属人已调整",
                    "order_id": s_id,
                    "owner_name": new_owner,
                    "previous_owner_name": prev_owner,
                    "manual_user_by": by,
                    "manual_user_at": fixed_at,
                })
            if new_owner:
                conn.rollback()
                return jsonify({"success": False,
                                "message": "云端订单请用 openid 调整所属用户"}), 400
            if prev_openid == new_openid:
                conn.commit()
                return jsonify({
                    "success": True,
                    "message": "订单已属于该用户",
                    "order_id": s_id,
                    "openid": new_openid,
                    "nickname": new_nickname,
                    "previous_openid": prev_openid,
                    "manual_user_by": by,
                    "manual_user_at": "",
                })
            conn.execute(
                "UPDATE orders SET openid = ?, manual_user_by = ?, manual_user_at = ? "
                "WHERE id = ?",
                (new_openid, by, fixed_at, s_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    print(f"  [FIX] 订单 #{s_id} 归属用户人工调整: {prev_openid} → {new_openid}"
          f"（by={by or '-'}）")
    return jsonify({
        "success": True,
        "message": "订单所属用户已调整",
        "order_id": s_id,
        "openid": new_openid,
        "nickname": new_nickname,
        "previous_openid": prev_openid,
        "manual_user_by": by,
        "manual_user_at": fixed_at,
    })


@app.route("/api/admin/statistics/revenue", methods=["GET"])
@require_printer_scope("admin")
def admin_statistics_revenue():
    """管理员收益统计接口（printer token 鉴权）。
    查询指定日期区间内的所有订单收益，按来源/用户/月份分组。
    参数:
        start_date  - 起始日期 (YYYY-MM-DD)，必填
        end_date    - 结束日期 (YYYY-MM-DD)，必填
        status      - 订单状态筛选，逗号分隔，默认不含 canceled/rejected/abandoned/reserved
        token       - 打印机认证 token
    金额口径: 已入账 = sent 子任务合计；自打订单（is_admin_print=1）不计入收益金额，
              订单/文件/页数等运营量仍计入。
    """
    token = _get_printer_token()
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403

    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    if not start_date or not end_date:
        return jsonify({"success": False, "message": "缺少 start_date 或 end_date 参数"}), 400

    # 状态筛选（排除已取消/已拒绝/已放弃/已预留）
    status_param = request.args.get("status", "").strip()
    if status_param:
        allowed_statuses = [s.strip() for s in status_param.split(",") if s.strip()]
    else:
        # 2026-11：默认口径把 timeout（超时）与 failed 同列 —— 结算里都算"没有成功入账"，
        # 明细行各自带 status_reason 区分原因
        allowed_statuses = ["sent", "printing", "queued", "failed", "timeout", "offline_unknown"]

    conn = get_db()
    # 构建状态占位符
    status_placeholders = ",".join("?" for _ in allowed_statuses)
    params = [start_date + " 00:00:00", end_date + " 23:59:59"] + allowed_statuses

    # ── 汇总 / 按用户分组在订单明细构建后由 Python 统一聚合 ──
    # 金额口径 = 文件费（按子任务状态归入已入账/应收未收）+ 订单级附加服务费
    # （派送/加急/首页费，按订单有效文件状态归入）。自打订单金额不计。
    revenue_summary = {"total": 0.0,
                       "wechat": 0.0, "app": 0.0, "local": 0.0,
                       "wechat_orders": 0, "app_orders": 0, "local_orders": 0,
                       "wechat_files": 0, "app_files": 0, "local_files": 0,
                       "total_pages": 0,
                       "wechat_pages": 0, "app_pages": 0, "local_pages": 0}
    by_user = []

    # ── 订单明细（时间倒序） ──
    # v25 重构：LEFT JOIN 保留无子任务的订单（如本地放弃单），子任务状态条件移入 ON；
    #          补充附加服务/自动打印/预约/地址等字段供收支清算云端页展示。
    order_rows = conn.execute(
        f"""SELECT o.id, o.order_number, o.source, o.openid, o.status,
                   o.total_price, o.created_at, o.delivery_enabled,
                   o.delivery_location, o.urgency, o.cover_page,
                   o.owner_name, o.is_admin_print,
                   o.auto_print, o.schedule_mode, o.scheduled_at,
                   o.delivery_percentage, o.urgency_price, o.cover_page_price,
                   o.pickup_address, o.remark, o.print_started_at,
                   o.received_client, o.target_client,
                   o.manual_status_by, o.manual_status_at,
                   o.manual_user_by, o.manual_user_at
            FROM orders o
            LEFT JOIN order_files of ON o.id = of.order_id AND of.status IN ({status_placeholders})
            WHERE o.created_at >= ? AND o.created_at <= ?
              AND o.status IN ({status_placeholders})
            GROUP BY o.id
            ORDER BY o.created_at DESC""",
        allowed_statuses + [start_date + " 00:00:00", end_date + " 23:59:59"] + allowed_statuses,
    ).fetchall()

    # 收集所有订单 ID 用于批量查询子文件
    order_ids = [r["id"] for r in order_rows]
    files_by_order = {}
    if order_ids:
        of_placeholders = ",".join("?" for _ in order_ids)
        of_rows = conn.execute(
            f"""SELECT id, order_id, file_name, copies, page_count, duplex,
                       page_range, total_price, status, created_at, image_orientation, is_free,
                       reject_reason
                FROM order_files
                WHERE order_id IN ({of_placeholders})
                ORDER BY order_id, id""",
            order_ids,
        ).fetchall()
        for of_row in of_rows:
            oid = of_row["order_id"]
            if oid not in files_by_order:
                files_by_order[oid] = []
            # 有效口径（v7.2）：页数/张数一律按**页码范围折算**后下发，前端只展示不再自行折算
            # ——页数 = 实际要打的页（折范围）、张数 = 实际出纸张数（单面每页 1 张 / 双面每 2 页 1 张）。
            # 与 calculate_price / _file_range_stats 同源，避免结算页与金额对不上。
            # 🟡2：simplex_pages（封面单独打印的页）必须一起传，否则 `_charge_units` 摊不出来
            # ——31 页里 9 页单面是 20 张，不传就按 ceil(31/2)=16 张少算 4 张。
            _st = _file_range_stats(of_row["page_count"] or 0, of_row["copies"] or 1,
                                    of_row["duplex"] or "on", of_row["page_range"] or "",
                                    _row_simplex_pages(of_row))
            files_by_order[oid].append({
                "id": of_row["id"],
                "file_name": of_row["file_name"],
                "copies": of_row["copies"] or 1,
                "page_count": of_row["page_count"] or 0,
                "duplex": of_row["duplex"] or "on",
                "page_range": of_row["page_range"] or "",
                "image_orientation": of_row["image_orientation"] or "auto",
                "total_price": round(of_row["total_price"] or 0.0, 2),
                "status": of_row["status"],
                "is_free": bool(of_row["is_free"]),
                "created_at": of_row["created_at"],
                "reject_reason": of_row["reject_reason"] or "",
                "effective_pages": _st["effective_pages"],
                "sheets": _st["sheets"],
                "total_pages": _st["total_pages"],
                "total_sheets": _st["total_sheets"],
            })

    # 全部子任务费用合计（含被排除状态；订单级附加服务费 = 父订单 total_price − 全部子任务费）
    all_file_sum = {}
    if order_ids:
        all_sum_rows = conn.execute(
            f"""SELECT order_id, SUM(total_price) AS file_sum
                FROM order_files
                WHERE order_id IN ({of_placeholders})
                GROUP BY order_id""",
            order_ids,
        ).fetchall()
        all_file_sum = {r["order_id"]: (r["file_sum"] or 0.0) for r in all_sum_rows}

    # 批量查昵称和头像
    all_openids = list(set(r["openid"] for r in order_rows if r["openid"] and r["openid"] != "local"))
    all_profiles = {}
    if all_openids:
        user_conn2 = get_user_db()
        ap_placeholders = ",".join("?" for _ in all_openids)
        nn_rows = user_conn2.execute(
            f"SELECT openid, nickname, avatar_path FROM users WHERE openid IN ({ap_placeholders})",
            all_openids,
        ).fetchall()
        for nr in nn_rows:
            all_profiles[nr["openid"]] = {
                "nickname": nr["nickname"] or "",
                "avatar_url": get_avatar_url(nr["openid"], nr["avatar_path"] or ""),
            }
        user_conn2.close()

    orders = []
    for row in order_rows:
        oid = row["id"]
        prof = all_profiles.get(row["openid"], {})
        files = files_by_order.get(oid, [])
        # 状态与移动端完全一致：用子任务实时聚合（orders.status 存储值可能滞后，
        # 如任务推送后回退 queued 未刷新父订单），聚合逻辑与 /api/orders 相同。
        agg_status = aggregate_order_status(conn, oid) or row["status"]
        # 金额口径：已入账 = sent 子任务合计；应收未收 = 其余未排除状态（排队/打印中/断线未知）合计；
        # 取消/放弃/打回/失败不计（失败单大概率不收费，仍可在明细状态列看到）。
        # 文件级 is_free 免费文件不计入金额但单独计数。
        # v5.1：自打订单（is_admin_print=1）不计入收益金额（明细文件单价仍原样展示）。
        paid_revenue = 0.0
        receivable = 0.0
        free_files = 0
        is_self_print = bool(row["is_admin_print"])
        for f in files:
            if f["is_free"]:
                free_files += 1
            if is_self_print:
                continue
            if f["status"] == "sent":
                paid_revenue += f["total_price"]
            elif f["status"] not in ("canceled", "abandoned", "rejected", "failed", "timeout"):
                receivable += f["total_price"]
        # 订单级附加服务费（派送/加急/首页费）= 父订单 total_price − 全部子任务费。
        # 仅当订单存在「有效文件」（非取消/放弃/打回/失败）时计入，且按有效文件状态整体
        # 归入已入账（全部 sent）/ 应收未收（有未 sent）；自打订单 / 无有效文件（如整单放弃）不计。
        extra_fee = max(0.0, (row["total_price"] or 0.0) - all_file_sum.get(oid, 0.0))
        if extra_fee > 0 and not is_self_print and files:
            active_files = [f for f in files if f["status"] == "sent" or f["status"] not in ("canceled", "abandoned", "rejected", "failed", "timeout")]
            if active_files:
                if all(f["status"] == "sent" for f in active_files):
                    paid_revenue += extra_fee
                else:
                    receivable += extra_fee
        orders.append({
            "order_id": oid,
            "order_number": row["order_number"] or "",
            "source": row["source"] or "wechat",
            "openid": row["openid"] or "",
            "nickname": prof.get("nickname", ""),
            "avatar_url": prof.get("avatar_url", ""),
            "status": agg_status,
            "total_price": round(row["total_price"] or 0.0, 2),
            "created_at": row["created_at"],
            "owner_name": row["owner_name"] or "",
            "is_admin_print": bool(row["is_admin_print"]),
            "delivery_enabled": bool(row["delivery_enabled"]),
            "delivery_location": row["delivery_location"] or "",
            "delivery_percentage": row["delivery_percentage"] or 0,
            "urgency": row["urgency"] or "低",
            "urgency_price": row["urgency_price"] or 0,
            "cover_page": bool(row["cover_page"]),
            "cover_page_price": row["cover_page_price"] or 0,
            "pickup_address": row["pickup_address"] or "",
            "remark": row["remark"] or "",
            "auto_print": bool(row["auto_print"]),
            "schedule_mode": row["schedule_mode"] or "now",
            "scheduled_at": row["scheduled_at"] or "",
            "print_started_at": row["print_started_at"] or "",
            "wait_seconds": calc_wait_seconds(row["created_at"], row["print_started_at"]),
            # 接单设备展示名（{所有者}的设备（{client_id}））；空值显示 —
            "received_client": row["received_client"] or "",
            "received_label": device_display_label(row["received_client"] or ""),
            "files_count": len(files),
            # 有效口径（v7.2）：页数 = Σ(折范围后的每份有效页 × 份数)，张数 = Σ(折范围后的张数 × 份数)。
            # 与各文件 total_pages/total_sheets 完全同源，保证「订单行 = 文件行之和」，也与金额口径一致。
            "total_page_count": sum(f["total_pages"] for f in files),
            "total_sheet_count": sum(f["total_sheets"] for f in files),
            "revenue": round(paid_revenue, 2),
            "receivable": round(receivable, 2),
            "free_files": free_files,
            # 2026-11：订单级原因与人工订正留痕 —— 结算明细里"为什么这单没钱/为什么状态被改"
            # 一眼可见（子任务级原因仍逐个在 files[i].reject_reason）。
            "status_reason": next((str(f.get("reject_reason") or "").strip()
                                   for f in files if str(f.get("reject_reason") or "").strip()), ""),
            "manual_status_by": row["manual_status_by"] or "",
            "manual_status_at": row["manual_status_at"] or "",
            # v7.18：订单归属用户的人工调整留痕（用户列 tooltip 显示"谁在何时改的归属"）
            "manual_user_by": row["manual_user_by"] or "",
            "manual_user_at": row["manual_user_at"] or "",
            "files": files,
        })

    # ── 基于订单明细统一聚合：按来源汇总 + 按用户分组 ──
    # 金额 = 已入账 + 应收未收（v5.17 合并口径，含订单级附加服务费）；自打订单金额已在上方剔除。
    for o in orders:
        src = o["source"] if o["source"] in ("wechat", "app", "local") else "wechat"
        rev = o["revenue"] + o["receivable"]
        revenue_summary["total"] += rev
        revenue_summary[src] += rev
        revenue_summary[f"{src}_orders"] += 1
        revenue_summary[f"{src}_files"] += o["files_count"]
        revenue_summary[f"{src}_pages"] += o["total_page_count"]
        revenue_summary["total_pages"] += o["total_page_count"]
    for _k in ("total", "wechat", "app", "local"):
        revenue_summary[_k] = round(revenue_summary[_k], 2)

    by_user_map = {}
    for o in orders:
        if not o["openid"] or o["openid"] == "local":
            continue
        key = o["openid"]
        u = by_user_map.get(key)
        if u is None:
            u = by_user_map[key] = {
                "openid": key,
                "nickname": o["nickname"],
                "avatar_url": o["avatar_url"],
                "revenue": 0.0,
                "order_count": 0,
                "wechat_orders": 0,
                "app_orders": 0,
                "file_count": 0,
                "total_pages": 0,
            }
        u["revenue"] += o["revenue"] + o["receivable"]
        u["order_count"] += 1
        if o["source"] == "wechat":
            u["wechat_orders"] += 1
        elif o["source"] == "app":
            u["app_orders"] += 1
        u["file_count"] += o["files_count"]
        u["total_pages"] += o["total_page_count"]
    by_user = sorted(by_user_map.values(), key=lambda u: u["revenue"], reverse=True)
    for u in by_user:
        u["revenue"] = round(u["revenue"], 2)

    conn.close()

    # 2026-11：订单「用户」列人工调整的候选名单 —— 只列真实用户（排除设备账号 dev_* 与
    # 已移除账号），否则菜单里会出现一堆点不进去的名字。用户表很小，一次带走即可，
    # 不必为此单开一个代理 GET 接口（少一条白名单 = 少一个 fail-open 面）。
    assignable_users = []
    try:
        user_conn = get_user_db()
        assignable_rows = user_conn.execute(
            """SELECT openid, nickname, role FROM users
               WHERE openid != '' AND openid != 'local'
                 AND substr(openid, 1, 4) != 'dev_'
                 AND (removed_at IS NULL OR removed_at = '')
               ORDER BY nickname COLLATE NOCASE, openid
               LIMIT 500"""
        ).fetchall()
        user_conn.close()
        # 2026-11-10 现场：候选名单里混进了「第一次登录就被自动分配占位昵称、之后从没下过单」
        # 的账号（如 user_s0iaj5b0）—— 结算页的「用户」列本来只该出现能对得上账的人。
        # 判据：占位昵称（user_ + 8 位小写字母数字，见首次登录那段生成规则）**且**名下没有任何
        # 有效订单。两个条件缺一不可：真下过单的占位昵称要保留（不然老订单的归属人消失）。
        _placeholder_nick = re.compile(r"^user_[a-z0-9]{8}$")
        _order_openids: set = set()
        _orders_known = False
        try:
            _main_conn = get_db()
            _order_rows = _main_conn.execute(
                """SELECT DISTINCT openid FROM orders
                   WHERE openid != '' AND status NOT IN ('canceled', 'abandoned', 'rejected')"""
            ).fetchall()
            _main_conn.close()
            _order_openids = {r["openid"] for r in _order_rows}
            _orders_known = True
        except Exception as e:
            # 查不到订单就**别过滤**：宁可多列一个名字，也不能把真实用户从名单里吞掉。
            print(f"[WARN] 统计用户订单数失败，候选名单不做占位账号过滤：{e}")
            _orders_known = False
        assignable_users = [
            {"openid": r["openid"], "nickname": r["nickname"] or "",
             "role": r["role"] or "guest"}
            for r in assignable_rows
            if not (_orders_known
                    and _placeholder_nick.match(str(r["nickname"] or ""))
                    and r["openid"] not in _order_openids)
        ]
    except Exception:
        assignable_users = []

    return jsonify({
        "success": True,
        "data": {
            "period": {"start": start_date, "end": end_date},
            "revenue_summary": revenue_summary,
            "by_user": by_user,
            "orders": orders,
            "assignable_users": assignable_users,
        },
    })


@app.route("/api/admin/statistics/owners", methods=["GET"])
@require_printer_scope("admin")
def admin_statistics_owners():
    """管理员订单归属统计（printer token 鉴权）。
    按订单归属管理员（orders.owner_name）分组，拆分"管理员自己的订单"（is_admin_print=1）
    与"接单打印的顾客订单"（is_admin_print=0），供本地打印工具收支清算页展示每位管理员的
    自打/接单比例与整体明细。
    参数: start_date / end_date / status（同 revenue 接口）
    """
    token = _get_printer_token()
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403

    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    if not start_date or not end_date:
        return jsonify({"success": False, "message": "缺少 start_date 或 end_date 参数"}), 400

    # 状态筛选（排除已取消/已拒绝/已放弃/已预留），口径与 revenue 接口一致
    status_param = request.args.get("status", "").strip()
    if status_param:
        allowed_statuses = [s.strip() for s in status_param.split(",") if s.strip()]
    else:
        # 2026-11：默认口径把 timeout（超时）与 failed 同列 —— 结算里都算"没有成功入账"，
        # 明细行各自带 status_reason 区分原因
        allowed_statuses = ["sent", "printing", "queued", "failed", "timeout", "offline_unknown"]

    conn = get_db()
    status_placeholders = ",".join("?" for _ in allowed_statuses)
    params = [start_date + " 00:00:00", end_date + " 23:59:59"] + allowed_statuses

    # ── 按归属管理员分组（自打 / 接单两维度） ──
    rows = conn.execute(
        f"""SELECT o.owner_name,
                   COUNT(DISTINCT o.id) AS order_count,
                   COUNT(DISTINCT CASE WHEN o.is_admin_print = 1 THEN o.id END) AS own_orders,
                   COUNT(DISTINCT CASE WHEN o.is_admin_print = 0 THEN o.id END) AS accepted_orders,
                   COUNT(DISTINCT CASE WHEN o.is_admin_print = 1 THEN of.id END) AS own_files,
                   COUNT(DISTINCT CASE WHEN o.is_admin_print = 0 THEN of.id END) AS accepted_files,
                   SUM(CASE WHEN o.is_admin_print = 1 THEN of.page_count * of.copies ELSE 0 END) AS own_pages,
                   SUM(CASE WHEN o.is_admin_print = 0 THEN of.page_count * of.copies ELSE 0 END) AS accepted_pages,
                   SUM(CASE WHEN o.is_admin_print = 1 AND of.status = 'sent' THEN of.total_price ELSE 0 END) AS own_revenue,
                   SUM(CASE WHEN o.is_admin_print = 0 AND of.status = 'sent' THEN of.total_price ELSE 0 END) AS accepted_revenue
            FROM orders o
            INNER JOIN order_files of ON o.id = of.order_id
            WHERE o.created_at >= ? AND o.created_at <= ?
              AND of.status IN ({status_placeholders})
            GROUP BY o.owner_name
            ORDER BY order_count DESC""",
        params,
    ).fetchall()
    conn.close()

    owners = []
    totals = {"order_count": 0,
              "own_orders": 0, "accepted_orders": 0,
              "own_files": 0, "accepted_files": 0,
              "own_pages": 0, "accepted_pages": 0,
              "own_revenue": 0.0, "accepted_revenue": 0.0}
    for row in rows:
        name = (row["owner_name"] or "").strip() or "未归属"
        own = row["own_orders"] or 0
        acc = row["accepted_orders"] or 0
        own_rev = round(row["own_revenue"] or 0.0, 2)
        acc_rev = round(row["accepted_revenue"] or 0.0, 2)
        owners.append({
            "owner_name": name,
            "order_count": row["order_count"] or 0,
            "own_orders": own,
            "accepted_orders": acc,
            "own_ratio": round(own / (own + acc), 4) if (own + acc) else 0,
            "own_files": row["own_files"] or 0,
            "accepted_files": row["accepted_files"] or 0,
            "own_pages": row["own_pages"] or 0,
            "accepted_pages": row["accepted_pages"] or 0,
            "own_revenue": own_rev,
            "accepted_revenue": acc_rev,
        })
        totals["order_count"] += row["order_count"] or 0
        for k in ("own_orders", "accepted_orders", "own_files", "accepted_files",
                  "own_pages", "accepted_pages"):
            totals[k] += row[k] or 0
        totals["own_revenue"] = round(totals["own_revenue"] + own_rev, 2)
        totals["accepted_revenue"] = round(totals["accepted_revenue"] + acc_rev, 2)

    return jsonify({
        "success": True,
        "data": {
            "period": {"start": start_date, "end": end_date},
            "owners": owners,
            "summary": totals,
        },
    })


@app.route("/api/admin/statistics/months", methods=["GET"])
@require_printer_scope("admin")
def admin_statistics_months():
    """按月份统计订单/文件数与收益（printer token 鉴权），供收支清算云端模块自动列出月份。
    收益口径沿用 revenue 接口：of.status='sent' 的 total_price 合计。
    """
    token = _get_printer_token()
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403

    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT strftime('%Y-%m', o.created_at) AS month,
                      COUNT(DISTINCT o.id) AS order_count,
                      COUNT(of.id) AS file_count,
                      SUM(CASE WHEN of.status = 'sent' THEN of.total_price ELSE 0 END) AS revenue
               FROM orders o
               INNER JOIN order_files of ON o.id = of.order_id
               WHERE o.created_at IS NOT NULL
               GROUP BY month
               ORDER BY month"""
        ).fetchall()
    finally:
        conn.close()

    months = []
    for row in rows:
        month = row["month"]
        try:
            y, m = month.split("-")
            label = f"{int(y)}年{int(m)}月"
        except Exception:
            label = month
        months.append({
            "month": month,
            "label": label,
            "orders": row["order_count"] or 0,
            "files": row["file_count"] or 0,
            "revenue": round(row["revenue"] or 0.0, 2),
        })
    return jsonify({"success": True, "data": {"months": months}})


def recover_stale_printing_tasks():
    """启动时（Gunicorn `post_worker_init`）的 printing 残留检查。

    🟡11（2026-11-10 体检）：这里原来是**无条件**把所有 `printing` 翻回 `queued`，
    既不看设备是否还在打、也不清 `operator_client`。后果：后端一重启，正好赶上某台
    设备在打一叠手动双面/攒批，任务被判成「崩溃残留」重推 → 同一批纸再出一遍。

    现在一律交给 `recover_orphaned_printing_tasks()`：它按「锁多久没刷新 + 设备是否
    在线 + 30 分钟硬上限」判断，在线且未超上限的任务留给设备自己收尾。
    保留这个函数名是因为 `gunicorn_config.post_worker_init()` 直接 import 它。
    """
    try:
        recover_orphaned_printing_tasks()
    except Exception as e:
        print(f"  [RECOVER] 启动时 printing 残留检查失败（不阻塞启动）: {e}")
    return 0


# ==================== 定时任务调度器（模块级，供 Gunicorn worker 钩子引用）========

scheduler = BackgroundScheduler()


def cleanup_abandoned_reserved_orders():
    """清理超过 30 分钟仍未提交的 reserved 占位订单。
    场景：用户在本地工具中点击了"复制"（分配了订单号）但从未点击"开始打印"，
    或者获取订单号后离线了且从未同步成功。
    纯占位单（未配置任何文件/子任务，file 仍为‘（预留位置）’、total_price=0）没有归属与金额，
    直接删除整行，避免留下 owner_name 为空、统计口径外的“无归属 abandoned”残留订单。"""
    cutoff = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        # 先删除纯占位单（从未配置文件/子任务，无金额）→ 不留无归属残留
        cursor = conn.execute(
            """DELETE FROM orders WHERE status = 'reserved'
               AND created_at < ?
               AND file = '（预留位置）'
               AND total_price <= 0
               AND NOT EXISTS (SELECT 1 FROM order_files f WHERE f.order_id = orders.id)""",
            (cutoff,),
        )
        count = cursor.rowcount
        if count > 0:
            conn.commit()
            print(f"[CLEANUP] 已删除 {count} 个超时纯占位订单（无归属残留）")
        # 剩余带配置的 reserved（含复制价格后完整预留的订单）标记 abandoned，子任务同步置 abandoned
        cursor2 = conn.execute(
            "UPDATE orders SET status = 'abandoned' WHERE status = 'reserved' AND created_at < ?",
            (cutoff,),
        )
        count2 = cursor2.rowcount
        if count2 > 0:
            conn.execute(
                "UPDATE order_files SET status = 'abandoned'"
                " WHERE order_id IN (SELECT id FROM orders WHERE status = 'abandoned'"
                "   AND created_at < ?) AND status NOT IN ('sent', 'failed', 'timeout', 'canceled')",
                (cutoff,),
            )
            conn.commit()
            print(f"[CLEANUP] 已将 {count2} 个超时 reserved 订单标记为 abandoned（含子任务）")
    except Exception as e:
        conn.rollback()
        print(f"[CLEANUP] 清理 reserved 订单失败: {e}")
    finally:
        conn.close()


# ==================== 日志系统 ====================

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

_server_log = logging.getLogger("printer_server")
_server_log.setLevel(logging.WARNING)
if not _server_log.handlers:
    _sh = logging.FileHandler(os.path.join(LOG_DIR, "server.log"), encoding="utf-8")
    _sh.setLevel(logging.WARNING)
    _sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    _server_log.addHandler(_sh)

_frontend_log = logging.getLogger("printer_frontend")
_frontend_log.setLevel(logging.WARNING)
if not _frontend_log.handlers:
    _fh = logging.FileHandler(os.path.join(LOG_DIR, "frontend.log"), encoding="utf-8")
    _fh.setLevel(logging.WARNING)
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    _frontend_log.addHandler(_fh)


_FRONTEND_LOG_PATH = os.path.join(LOG_DIR, "frontend.log")
# 单次「拉取后端日志」最多回 200KB（journald 里可能夹着超长行，别把内存和响应撑爆）
_LOG_FETCH_MAX_BYTES = 200 * 1024
# 读 journal 用的 unit 名（与 printer-backend.service 的 unit 一致）
_JOURNAL_UNIT = os.environ.get("HN_JOURNAL_UNIT", "printer-backend")


def _journalctl_exe() -> str:
    """journalctl 的可执行路径。

    ⚠ 服务单元的 `Environment=PATH=/home/printer-backend/venv/bin` 把系统 PATH 覆盖了，
    所以**不能**只写 "journalctl" —— subprocess 会 FileNotFoundError（2026-11-02 现场：
    端点回 0 字节，检查发现是 PATH 里没有 /usr/bin）。这里显式解析并兜底绝对路径。
    """
    p = shutil.which("journalctl")
    if p:
        return p
    for cand in ("/usr/bin/journalctl", "/bin/journalctl", "/usr/sbin/journalctl"):
        if os.path.exists(cand):
            return cand
    return ""


def _journalctl_tail(lines: int) -> str:
    """取 systemd journal 里本服务的最近 N 行 —— **后端日志的真相源**。

    为什么需要兜底：`LOG_DIR/server.log` 只由 `printer_server` logger 写，而全仓没有任何
    一处往这个 logger 打日志（2026-11-02 现场核查：该文件从 2026-08-03 起一直是 0 字节）；
    gunicorn 的 access / error 日志走 stdout / stderr → journald。所以「拉取前后端日志」
    只读文件时永远显示 0 字节，看上去像拉取失败。这里回落到 journalctl，并做 token 脱敏
    （access_log_format 已去掉 query，但 journal 里别处仍可能带密钥）。
    """
    exe = _journalctl_exe()
    if not exe:
        return ""
    try:
        proc = subprocess.run(
            [exe, "-u", _JOURNAL_UNIT, "-n", str(max(1, int(lines))),
             "--no-pager", "--output=short-iso"],
            capture_output=True, text=True, timeout=12,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return _scrub_secret(proc.stdout or "")


_FRONTEND_LOG_MAX_BYTES = 50 * 1024 * 1024  # 超过 50MB 轮转（P1-6）


def _write_frontend_log(level, message):
    """写前端日志（手动追加 + 超 50MB 轮转）。
    logging.FileHandler 在 Windows 上持有文件句柄，写期间无法重命名，故手动管理。"""
    if os.path.exists(_FRONTEND_LOG_PATH) and os.path.getsize(_FRONTEND_LOG_PATH) > _FRONTEND_LOG_MAX_BYTES:
        try:
            if os.path.exists(_FRONTEND_LOG_PATH + ".1"):
                os.remove(_FRONTEND_LOG_PATH + ".1")
            os.rename(_FRONTEND_LOG_PATH, _FRONTEND_LOG_PATH + ".1")
            print(f"[LOG] frontend.log 超过 {_FRONTEND_LOG_MAX_BYTES // (1024 * 1024)}MB，已轮转为 .1")
        except OSError as e:
            print(f"[LOG] 前端日志轮转失败: {e}")
    try:
        with open(_FRONTEND_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level.upper()}] {message}\n")
    except OSError as e:
        print(f"[LOG] 写前端日志失败: {e}")


@app.route("/api/log/report", methods=["POST"])
def log_report():
    """前端上报错误/警告日志。
    P1-6.2：新增鉴权（打印机 token 或登录态 Bearer）、每 IP 每分钟 30 条限速、
    message 长度限制 4KB（超长截断）、frontend.log 超 50MB 自动轮转。"""
    # 鉴权：打印机 token（X-Printer-Token / ?token=）或登录态（Bearer token）二选一
    authorized = False
    if _authenticate_printer(_get_printer_token()):
        authorized = True
    else:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            try:
                _load_login_token(auth[7:])
                authorized = True
            except Exception:
                authorized = False
    if not authorized:
        return jsonify({"success": False, "message": "未授权"}), 403

    # 限速（P1-6 / 防滥用）：每 IP 每分钟日志上报条数上限（管理员可调，单 worker 内存计数）
    ip = request.remote_addr or "unknown"
    if not _rate_limit(f"log_report:{ip}", _SECURITY["log_report_rate_limit"], 60):
        return jsonify({"success": False, "message": "请求过于频繁"}), 429

    data = request.get_json(silent=True) or {}
    level = data.get("level", "warning")
    message = data.get("message", "")
    if not message:
        return jsonify({"success": False, "message": "缺少日志内容"}), 400
    if len(message) > 4096:
        message = message[:4096]  # 截断而非拒绝，防止日志文件被撑爆
    # S0-06：设备端上报的日志常带完整 URL（含 ?token=），落盘前脱敏
    message = _scrub_secret(message)
    if level in ("error", "critical"):
        _write_frontend_log("error", message)
    else:
        _write_frontend_log("warning", message)
    return jsonify({"success": True})


@app.route("/api/log/fetch", methods=["GET"])
@require_printer_scope("admin")
def log_fetch():
    """本地打印工具拉取后端/前端日志（需 token 认证）。
    P2-14：支持 ?tail=N 只返回末尾 N 行（默认 500，0 = 不返回内容，仅取大小）。"""
    token = _get_printer_token()
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403
    log_type = request.args.get("type", "server")
    if log_type not in ("server", "frontend"):
        return jsonify({"success": False, "message": "type 只能为 server 或 frontend"}), 400
    try:
        tail = int(request.args.get("tail", "500"))
    except (ValueError, TypeError):
        tail = 500
    tail = max(0, tail)
    log_path = os.path.join(LOG_DIR, f"{log_type}.log")
    content = ""
    source = "file"
    if tail and os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        content = "".join(lines[-tail:])
    # server.log 常年为空（没人往 printer_server logger 写）⇒ 回落 journald，
    # 否则这个按钮永远只报「0 字节」，看着像网络问题。
    if tail and log_type == "server" and not content.strip():
        content = _journalctl_tail(tail)
        source = "journald" if content.strip() else "file"
    if len(content.encode("utf-8")) > _LOG_FETCH_MAX_BYTES:
        content = content.encode("utf-8")[-_LOG_FETCH_MAX_BYTES:].decode("utf-8", "ignore")
    return jsonify({"success": True, "size": len(content.encode("utf-8")),
                    "content": content, "source": source})


@app.route("/api/log/collect_all", methods=["POST"])
@require_printer_scope("admin")
def log_collect_all():
    """收集所有在线打印设备的日志（本地打印工具「日志管理」页发起，需打印机 token）。

    向所有在线客户端 SocketIO 推送 request_log（附本次 request_id），等待各设备
    回报本机日志尾部（logs/YYYY-MM-DD.log 末尾 200KB，按天分文件见客户端 daily_log）；超时未回报的设备标记 error。
    body: {"timeout": 秒（2-20，默认 8）}
    response: {"success": true, "logs": [{client_id, device_name, online, content | error}]}
    """
    global _collect_request_id
    token = _get_printer_token()
    if not _authenticate_printer(token):
        return jsonify({"success": False, "message": "token 无效"}), 403
    data = _json_dict() or {}
    try:
        timeout = max(2, min(20, int(data.get("timeout", 8) or 8)))
    except (ValueError, TypeError):
        timeout = 8
    # S1-07：单次收集最长等 3 秒（原实现最多占线程 20 秒）
    timeout = min(float(timeout), COLLECT_MAX_WAIT)

    active = get_active_clients()
    if not active:
        return jsonify({"success": True, "logs": []})

    # S1-07：全局只允许一次收集在飞；第二次立即 503（快速失败，不排队）
    if not _collect_sem.acquire(blocking=False):
        return jsonify({"success": False,
                        "message": "上一次日志收集尚未结束，请稍后重试"}), 503
    try:
        with _collect_cond:
            _collect_request_id += 1
            request_id = _collect_request_id
            _collect_logs.clear()

        # 向所有在线打印机客户端下发收集指令
        for cid in active:
            with printer_clients_lock:
                info = printer_clients.get(cid)
                sid = info["sid"] if info else None
            if sid:
                try:
                    socketio.emit("request_log",
                                  {"request_id": request_id,
                                   "max_bytes": _COLLECT_LOG_MAX_BYTES}, to=sid)
                except Exception as e:
                    print(f"  [LOG] 下发 request_log 到 {cid} 失败: {e}")

        # 等待各设备回报：Condition 唤醒（不再 sleep 轮询），最多 timeout 秒
        deadline = time.time() + timeout
        with _collect_cond:
            while len(_collect_logs) < len(active):
                remain = deadline - time.time()
                if remain <= 0:
                    break
                _collect_cond.wait(remain)
            collected = dict(_collect_logs)
    finally:
        _collect_sem.release()

    devices = load_devices()
    logs = []
    for cid in active:
        entry = devices.get(cid) or {}
        item = {
            "client_id": cid,
            "device_name": entry.get("device_name", "") or cid,
            "online": True,
        }
        content = collected.get(cid)
        if content is None:
            item["error"] = "超时未回报"
        else:
            item["content"] = content
        logs.append(item)
    collected_count = sum(1 for l in logs if "content" in l)
    print(f"[LOG] 在线设备日志收集完成: {collected_count}/{len(logs)} 台回报（timeout={timeout}s）")
    return jsonify({"success": True, "logs": logs, "collected": collected_count})


# ==================== 启动 ====================

if __name__ == "__main__":
    init_db()
    print("数据库已初始化")

    scheduler.add_job(process_pending_orders, "interval", seconds=30, id="scan_orders")
    scheduler.add_job(process_scheduled_orders, "interval", seconds=30, id="scan_scheduled")
    scheduler.add_job(check_printing_timeout, "interval", seconds=60, id="check_timeout")
    scheduler.add_job(cleanup_expired_license_keys, "interval", minutes=10, id="cleanup_licenses")
    scheduler.add_job(cleanup_expired_files, "interval", minutes=10, id="cleanup_files")
    scheduler.add_job(recover_orphaned_printing_tasks, "interval", minutes=2, id="recover_orphans")
    scheduler.add_job(recover_stale_downloading, "interval", minutes=2, id="recover_stale_downloads")
    scheduler.add_job(cleanup_abandoned_reserved_orders, "interval", minutes=5, id="cleanup_reserved")
    scheduler.add_job(expire_stale_queued_orders, "interval", minutes=10, id="expire_stale_queued")
    # 🔴-A4：预约单到点后长期没人打出来（目标设备一直不出现）也要超时取消，否则
    # scheduled/waiting 在 cleanup_expired_files 里算「活跃」⇒ 文件永久钉在服务器上。
    scheduler.add_job(expire_stale_scheduled_orders, "interval", minutes=10,
                      id="expire_stale_scheduled")
    # 测试设备（联调/冒烟脚本注册的 debug-* 等）离线后自动清掉，避免授权页越积越多
    scheduler.add_job(purge_test_devices, "interval", minutes=30, id="purge_test_devices")
    # 🔴-B10：改价失败的单在这里补算（幂等，算完清 needs_repricing）
    scheduler.add_job(resync_pending_prices, "interval", minutes=10, id="resync_prices")
    # 🔴-D13：冻结的预约单自愈（file_ready 从此不来的情形），冻结超 30 分钟就解冻/标超时
    scheduler.add_job(release_stuck_frozen_orders, "interval", minutes=5,
                      id="release_stuck_frozen")
    scheduler.start()
    print("定时扫描已启动（任务扫描每 30s，预约扫描每 30s，超时检查每 60s，预留订单清理每 5min）")

    # P2-8：debug 直跑仅限开发（FLASK_DEBUG=1）；生产必须用 gunicorn（systemd 服务），
    # 直跑模式是单进程开发服务器，不用于生产。
    socketio.run(app, host="127.0.0.1", port=5000,
                 debug=os.environ.get("FLASK_DEBUG") == "1", use_reloader=False,
                 allow_unsafe_werkzeug=True)
