"""
stats_server.py — 内置 HTTP 服务器，为收支清算 HTML 提供：
  1. 静态文件服务（从 local_print_tool/finance/ 目录）
  2. 云端 API 代理（附带 printer token）
  3. 本地 openid→成员 绑定配置读写
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import logging
import os
import platform
import posixpath
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

import requests as http_requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# ===== S0-06：日志脱敏（安全加固计划 §S0-06 动作 3）=====
# requests 的异常文本里带**完整 URL**（含 ?token=…），这些行会落进 logs/*.log。
# 在 logger 上挂一道过滤器：本模块任何日志记录都先过 scrub，再交给 handler ——
# 比逐处改 f-string 可靠（漏一处就漏一条凭据）。
_SECRET_QUERY_RE = re.compile(r"([?&](?:token|t|key|access_token)=)[^&\s\"'<>]+", re.I)


def scrub_secrets(text):
    """把 URL 查询串里的 token / t / key / access_token 值替换成 ***。"""
    if not text:
        return text
    try:
        return _SECRET_QUERY_RE.sub(r"\1***", str(text))
    except Exception:
        return "[scrub-failed]"


class _SecretScrubFilter(logging.Filter):
    """logging.Filter：命中敏感查询参数就改写记录（不改变是否输出）。"""

    def filter(self, record):
        try:
            msg = record.getMessage()
            scrubbed = scrub_secrets(msg)
            if scrubbed != msg:
                record.msg = scrubbed
                record.args = ()
        except Exception:
            pass
        return True


logger.addFilter(_SecretScrubFilter())

# 「页码范围 → 有效页数」的**单一来源**：复用 printer_config 的权威实现。
# 本文件（以及 finance/settlement.html）**不许**再写第二份解析 —— 两份口径一漂移，
# 结算页的页数/张数就会与金额静默对不上（历史教训：旧副本把空白替换成逗号、parseInt 宽松解析）。
# 取不到实现（极端打包/单测环境）时退回整份口径而不是抛异常：这里的异常会被外层
# try/except 当成"这条订单的文件都坏了"→ 整单文件列表清空，代价远大于一次降级。
def _count_range_pages(page_range: str, page_count: int) -> int:
    total = int(page_count or 0)
    try:
        from printer_config import _count_pages_in_range as _impl
        return _impl(page_range or "", total)
    except Exception:
        return total

# ── 云端代理连接池（连接复用 + 自动重试）──
# 收支清算页的代理请求走共享 Session：keep-alive 复用 TCP 连接，配合重试适配器
# 消化校园/宿舍 NAT 等网络的瞬时连接波动（ConnectTimeout/ReadTimeout）——
# 服务器本身健康时也偶发建连失败，重试后可自动恢复，页面不再表现为「突然断连」。
# 超时拆分为 (connect, read)：建连 5s（正常建连 <1s，超过说明链路异常，快速失败重试），
# 读取 15s（放宽容灾/聚合类大响应）。重试仅针对建连/读取瞬断与 502/503/504。
_PROXY_TIMEOUT = (5, 15)

_proxy_session = None
_proxy_session_lock = threading.Lock()


def _get_proxy_session():
    """懒创建共享 requests.Session（线程安全，供 ThreadingHTTPServer 多线程复用）。"""
    global _proxy_session
    if _proxy_session is None:
        with _proxy_session_lock:
            if _proxy_session is None:
                s = http_requests.Session()
                retry = Retry(
                    total=3,                       # 1 次原始 + 最多 2 次重试
                    connect=3,                     # 建连失败重试（瞬时网络波动的直接对策）
                    read=1,                        # 读取超时重试 1 次
                    backoff_factor=0.3,            # 重试间隔 0.3s / 0.6s / 1.2s
                    status_forcelist=(502, 503, 504),
                    # 允许重试 POST：收支清算经代理的写接口（配置保存/绑定等）均幂等
                    allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"]),
                )
                s.mount("http://", HTTPAdapter(max_retries=retry))
                s.mount("https://", HTTPAdapter(max_retries=retry))
                _proxy_session = s
    return _proxy_session

# 静态文件根目录（支持 PyInstaller 打包和开发环境）
if getattr(sys, "frozen", False):
    _STATIC_DIR = os.path.join(sys._MEIPASS, "finance")
else:
    _STATIC_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "finance"))
# 用户数据统一存 %APPDATA%\HN打印工具（与程序目录解耦，自更新不丢数据）
import paths as _paths
# 绑定配置文件
_BINDINGS_FILE = _paths.bindings_path()
# 默认数据文件（用户收支数据）
_DEFAULT_DATA_FILE = _paths.finance_data_path()
# 本地订单库（OfflineSync 写入的 SQLite，供「本地订单统计」页读取）
_LOCAL_DB_FILE = _paths.local_db_path()

# 本地文件读写锁（数据文件与绑定文件各自一把，跨线程串行化）
_DATA_FILE_LOCK = threading.Lock()
_BINDINGS_FILE_LOCK = threading.Lock()
# 请求体大小上限（10MB）与读取超时（秒）
_MAX_BODY_SIZE = 10 * 1024 * 1024
_BODY_READ_TIMEOUT = 15
# S1-09：本地 HTTP 服务自身也要有资源上限（否则一次 POST 塞 1GB / 上百个并发就能把机器拖死）
_MAX_CONCURRENT_REQUESTS = 16     # 并发处理线程上限：超了直接 503，不排队、不无限起线程
_SOCKET_TIMEOUT = 30              # 连接级空闲/读写超时（秒）：慢速或半开连接 30 秒后断开
# 公共静态文件白名单：浏览器加载这些文件时不要求启动令牌（其余静态文件一律校验）
_PUBLIC_STATIC_FILES = {"/chart.umd.min.js"}

# ═══════════════════════════════════════════════════════════════════════════
# S2-10 本地 HTTP 服务加固收尾（docs/安全加固修复计划_2026-10-23.md §S2-10 / LT-11）
#
#   ① 启动令牌不再进地址栏：GUI 打开页面时签发**一次性 nonce**（`?nonce=`），HTML 入口校验
#      后**当场作废**（重放 403）；页面加载即 `history.replaceState` 清掉。`?token=` 作为老
#      入口**保留**（老书签/旧流程），但它只对 HTML 入口生效 —— API 一律只认 X-Launch-Token 头。
#   ② Host 白名单：只接受 127.0.0.1 / localhost / [::1] + **实际端口**（防 DNS rebinding）。
#   ③ ACAO 收窄：只回显本服务自己的 origin，其它 Origin（含 null / 外部域名）不发 CORS 头。
#   ④ 令牌比较一律 hmac.compare_digest（header / query / nonce 三条路径）。
#   ⑤ 代理路径归一化 + 显式白名单：越界/未知 → 4xx，且**不发出**上游请求。
#   ⑥ 出站 printer token 改 X-Printer-Token 请求头（不再拼 ?token=，否则每次打开页面都在
#      VPS 的 access log 里留下 token；后端 app.py:_get_printer_token 优先读该头）。
#   ⑦ 安全响应头：X-Frame-Options / Referrer-Policy / nosniff 加到**所有**响应，HTML 另加 CSP。
#   ⑧ 并发上限沿用 S1-09 的 _LimitedThreadingHTTPServer（本批**不重复实现**，只补验收断言）。
# ═══════════════════════════════════════════════════════════════════════════

# ① 一次性 nonce 有效期：要够浏览器从「点菜单」走到「首个请求」，但不能长到可被翻历史重放
NONCE_TTL = 120                  # 秒
_NONCE_MAX_PENDING = 32          # 未消费 nonce 上限（每次点开页面才签发一个；TTL 到期即清）

# ⑤ 代理白名单：`/api/proxy/<x>` 归一化后必须**精确**命中这些云端路径 —— 与
# settlement.html 里实际调用的 `/api/proxy/...` 一一对应。新增代理接口必须同时加进这里
# （验收脚本会拿 settlement.html 的调用面反查本集合，防止"页面加了接口、白名单忘了加"）。
_PROXY_PREFIX = "/api/proxy"
_PROXY_ALLOWED_TARGETS = frozenset({
    "/api/admin/order_status",
    "/api/admin/order_type",
    "/api/admin/statistics/months",
    "/api/admin/statistics/revenue",
    "/api/finance/config",
    "/api/pricing",
    "/api/printer/bind_owner",
    "/api/printer/capability",
    "/api/printer/devices",
    "/api/printer/devices/cleanup",
    "/api/printer/devices/delete",
})

# ⑦ 所有响应都带的安全头；HTML 额外加 CSP。CSP 必须真的不把页面打坏：页面有内联
# script/style（'unsafe-inline'）、Chart.js 本地文件（'self'）与 jsdelivr CDN 兜底
# （显式放行该域名 —— **不要**为了 CSP 把 CDN 兜底删掉）。
_SECURITY_HEADERS = (
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Content-Type-Options", "nosniff"),
)
_HTML_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
)
# 本机回环主机名（Host 白名单 ② 与 CORS 回显 ③ 共用同一份口径）
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")

# ═══════════════════════════════════════════════════════════════════════════
# S1-05 财务安全加固（docs/安全加固修复计划_2026-10-23.md §S1-05；D-5 已拍板
# 「接受首次强制改密 + 后端二次校验」）
#
#   ① 密码：PBKDF2-HMAC-SHA256 + **每模块独立 salt** + ≥200k 迭代。**本文件是唯一实现**，
#      前端不再自带哈希（前端算的哈希永远不能证明"用户知道密码"，那只证明"客户端愿意相信"）。
#      旧的无盐 SHA-256 / 明文在**首次解锁成功时**自动重算升级为 PBKDF2（幂等：升级过就不再动）。
#   ② 首次强制改密：代码里没有可用的默认密码。`123456` 只出现在**拒绝清单**与
#      「旧默认密码指纹」里 —— 命中即判 needs_setup，必须先在页面上设新密码。
#   ③ 后端二次校验：密码校验通过 → 签发 30 分钟的 `finance_session`（HMAC 签名 + 绑定本机 +
#      随机 sid，进程重启即全部作废）。**所有写操作**必须带它（未登记的写路由 fail-closed 同样要求），
#      读操作照旧放行（向后兼容）。
#   ④ 完整性：print_data.json 配旁路 HMAC（密钥由 Windows DPAPI 保护，取不到时降级为
#      0600 权限的随机密钥文件并**明确日志说明降级**）。启动时校验；被整份覆写 → 只读保护，
#      **不覆盖数据**，等操作员在页面上确认后按当前内容重建基线。
#   ⑤ 审计：财务写操作追加只读日志（%APPDATA%\HN打印工具\logs\finance_audit.log），
#      记时间/模块/字段/前后值摘要，**不含密码与密钥**。
# ═══════════════════════════════════════════════════════════════════════════

FINANCE_MODULES = ("score", "expense", "income", "transfer", "settings", "auth", "orderstatus", "orderprinter")
# D-5：解锁会话 30 分钟（超时需重新输入密码，是已接受的操作摩擦）
FINANCE_SESSION_TTL = 30 * 60
FINANCE_SESSION_HEADER = "X-Finance-Session"
PBKDF2_SCHEME = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 210_000          # ≥200k（计划 §S1-05）
PBKDF2_SALT_BYTES = 16               # 每模块独立 salt：每次设置/升级都重新生成
MIN_PASSWORD_LEN = 6
# 旧默认密码的指纹（sha256('123456')）。**只用于识别**「还是默认密码」的存量模块 → 强制改密；
# 任何校验路径都不会拿它当可通过的密码（识别出来就是 needs_setup）。
LEGACY_DEFAULT_SHA256 = "8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92"
# 服务端**先于校验**拒绝的弱口令/默认值：不管库里存的是什么，这些密码永远不能用来解锁
WEAK_PASSWORDS = frozenset({
    "123456", "1234567", "12345678", "123456789", "12345", "password", "passwd",
    "111111", "000000", "666666", "888888", "abc123", "qwerty", "admin", "iloveyou",
})
# 服务端权威凭据存储键：写在 print_data.json **顶层**（页面不读不写、不参与 hasRealData，
# 因此不会把「本机凭据副本」误判成"本地也有真实收支数据"而每次启动弹合并框）
CRED_KEY = "_finance_credentials"
# 写操作 → 必须解锁的模块（None = 任意已解锁模块即可；未登记的写路由按 None 处理 = fail-closed）
FINANCE_WRITE_MODULES = {
    "/api/proxy/admin/order_status": "orderstatus",
    "/api/proxy/admin/order_type": "orderstatus",
    "/api/proxy/finance/config": None,
    "/api/proxy/pricing": "settings",
    "/api/proxy/printer/bind_owner": "auth",
    "/api/proxy/printer/devices/delete": "auth",
    "/api/proxy/printer/devices/cleanup": "auth",
    "/api/proxy/printer/capability": "auth",
    "/api/local/data": None,
    "/api/local/bindings": None,
}
# 不需要会话的 POST（登录/首次设置/登出：它们本身就是"换会话"的动作）
FINANCE_AUTH_OPEN_PATHS = frozenset({
    "/api/local/finance_auth/unlock",
    "/api/local/finance_auth/setup",
    "/api/local/finance_auth/logout",
})
_DPAPI_MAGIC = b"HNDPAPI1"          # 密钥文件前缀：DPAPI 保护
_RAW_KEY_MAGIC = b"HNRAW001"        # 密钥文件前缀：降级为明文随机密钥
_AUDIT_MAX_BYTES = 2 * 1024 * 1024  # 审计日志超过 2MB 轮转一次


def _now_ts() -> int:
    return int(time.time())


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def machine_fingerprint() -> str:
    """本机指纹（会话绑定用）：计算机名 + 登录用户名。换机器/换账户的 token 一律作废。"""
    try:
        node = platform.node() or "?"
    except Exception:
        node = "?"
    try:
        user = getpass.getuser() or "?"
    except Exception:
        user = "?"
    return "%s|%s" % (node, user)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _dpapi_protect(raw: bytes) -> bytes:
    """Windows DPAPI 加密（当前用户范围）。取不到 pywin32/失败时由调用方降级。"""
    import win32crypt                                   # noqa: F401（不存在就抛 ImportError）
    return win32crypt.CryptProtectData(raw, "HN打印工具 财务完整性密钥", None, None, None, 0)


def _dpapi_unprotect(blob: bytes) -> bytes:
    import win32crypt                                   # noqa: F401
    _desc, raw = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
    return raw


def _money(value) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def finance_data_summary(data) -> dict:
    """数据摘要（审计日志的「前后值摘要」）：只记条数与合计金额，**绝不记密码/密钥/哈希**。"""
    if not isinstance(data, dict):
        return {}
    recs = data.get("records") if isinstance(data.get("records"), dict) else {}
    cfg = data.get("config") if isinstance(data.get("config"), dict) else {}
    out = {"members": len(cfg.get("members") or [])}
    for key in ("expenses", "income", "transfers", "scores"):
        arr = recs.get(key) or []
        if not isinstance(arr, list):
            arr = []
        total = 0.0
        for r in arr:
            if not isinstance(r, dict):
                continue
            if key == "scores":
                try:
                    total += float(r.get("score") or 0)
                except (TypeError, ValueError):
                    pass
            else:
                total += _money(r.get("amount"))
        out[key] = [len(arr), round(total, 2)]
    pw = cfg.get("passwords") if isinstance(cfg.get("passwords"), dict) else {}
    # 只记「哪几个模块有密码」，不记哈希、不记明文
    out["password_modules"] = sorted([k for k, v in pw.items() if v])
    return out


_AUDIT_SECRET_KEYS = re.compile(r"(password|passwd|pwd|secret|token|key|mac|hash|salt)", re.I)


def _scrub_audit(value, depth=0):
    """审计字段脱敏：键名命中 password/secret/token/key/mac/hash/salt 的一律替换成 ***。"""
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _AUDIT_SECRET_KEYS.search(str(k)):
                out[str(k)] = "***"
            else:
                out[str(k)] = _scrub_audit(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub_audit(v, depth + 1) for v in value[:50]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class FinanceSecurity:
    """收支清算页的财务安全门（密码 / 会话 / 数据完整性 / 审计）。

    一个数据文件对应一个实例（见 get_finance_security 的进程内缓存）：
      · 会话登记表在实例内存里，`_secret` 每进程随机 ⇒ 进程重启即全部作废；
      · 所有公开方法都在 self._lock 下串行（ThreadingHTTPServer 是多线程的）。
    """

    def __init__(self, data_file: str = "", session_ttl: int = 0,
                 api_url: str = "", token: str = "", finance_mode: str = "cloud"):
        self.data_file = data_file or _DEFAULT_DATA_FILE
        self.home_dir = os.path.dirname(self.data_file) or "."
        self.logs_dir = os.path.join(self.home_dir, "logs")
        self.audit_file = os.path.join(self.logs_dir, "finance_audit.log")
        # 密钥与 HMAC 旁路文件与数据文件同目录（生产 = %APPDATA%\HN打印工具）
        self.key_file = os.path.join(self.home_dir, "finance_hmac.key")
        self.mac_file = self.data_file + ".hmac"
        self.seal_mark_file = self.data_file + ".sealed"
        self.session_ttl = int(session_ttl or FINANCE_SESSION_TTL)
        self.api_url = (api_url or "").rstrip("/")
        self.token = token or ""
        self.finance_mode = finance_mode if finance_mode in ("cloud", "local") else "cloud"
        self.machine = machine_fingerprint()
        self._secret = secrets.token_bytes(32)
        self._sessions = {}                              # sid → {exp, mods, iat}
        self._lock = threading.RLock()
        self._hmac_key = None                            # bytes | None
        self._key_mode = ""                              # 'dpapi' / 'raw' / 'unreadable' / ''
        self._read_only = False
        self._integrity = "unknown"
        self._cloud_summary = None                       # 上一次看到的云端配置摘要（审计 before）
        self._cloud_cache = None                         # 云端 config.passwords 缓存
        self._cloud_cache_ts = 0
        self._cloud_cache_ready = False

    # ── 文件小工具（原子写 + 0600） ──

    @staticmethod
    def _read_json(path: str, default=None):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    @staticmethod
    def _write_bytes(path: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _write_json(self, path: str, data) -> None:
        self._write_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))

    # ── 完整性密钥（DPAPI / 降级明文） ──

    def _load_or_create_key(self):
        with self._lock:
            if self._hmac_key is not None:
                return self._hmac_key
            if os.path.exists(self.key_file):
                try:
                    with open(self.key_file, "rb") as f:
                        blob = f.read()
                except OSError:
                    blob = b""
                if blob.startswith(_DPAPI_MAGIC):
                    try:
                        key = _dpapi_unprotect(blob[len(_DPAPI_MAGIC):])
                    except Exception as e:
                        # 换了机器/换用户/DPAPI 损坏：拿不到密钥 ⇒ 无法验证既有基线（人工确认后重建）
                        logger.error("财务完整性：DPAPI 密钥解密失败（%s），既有 HMAC 无法校验"
                                     "（需人工确认后重建基线）", type(e).__name__)
                        self._key_mode = "unreadable"
                        return None
                    if len(key) == 32:
                        self._hmac_key, self._key_mode = key, "dpapi"
                        return key
                elif blob.startswith(_RAW_KEY_MAGIC):
                    key = blob[len(_RAW_KEY_MAGIC):]
                    if len(key) == 32:
                        self._hmac_key, self._key_mode = key, "raw"
                        return key
                logger.warning("财务完整性密钥文件格式异常 → 重新生成（旧基线需人工确认后重建）")
            key = self._new_key()
            return key

    def _new_key(self):
        """生成并落盘新密钥：优先 DPAPI；取不到就**降级**为 0600 随机密钥文件并明确记日志。"""
        key = secrets.token_bytes(32)
        try:
            blob = _dpapi_protect(key)
            self._write_bytes(self.key_file, _DPAPI_MAGIC + blob)
            self._key_mode = "dpapi"
            logger.info("财务完整性：已用 Windows DPAPI 保护 HMAC 密钥")
        except Exception as e:
            # 非 Windows / 没装 pywin32 / 服务账户 —— 降级但要让人一眼看见
            logger.warning(
                "财务完整性：Windows DPAPI 不可用（%s）→ 已**降级**为随机密钥文件 %s"
                "（0600 权限，位于当前用户配置目录；保护强度低于 DPAPI，仅本机用户可读）",
                type(e).__name__, os.path.basename(self.key_file))
            self._write_bytes(self.key_file, _RAW_KEY_MAGIC + key)
            self._key_mode = "raw"
        self._hmac_key = key
        return key

    def _mac_of(self, data) -> str:
        key = self._load_or_create_key()
        if not key:
            return ""
        try:
            raw = json.dumps(data, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            return ""
        return hmac.new(key, raw, hashlib.sha256).hexdigest()

    # ── 完整性：封存 / 校验 / 人工确认 ──

    def _seal(self, data, mac: str) -> None:
        self._write_json(self.mac_file, {
            "alg": "hmac-sha256", "v": 1, "mac": mac,
            "file": os.path.basename(self.data_file), "sealed_at": _iso_now(),
        })
        try:
            self._write_bytes(self.seal_mark_file, b"sealed\n")
        except OSError:
            pass

    def check_integrity(self) -> str:
        """启动时/每次写入前校验。返回状态：
        ok / sealed（首次启用保护，基线已建立）/ absent（还没有数据文件）/
        mismatch（被改过）/ missing_mac（基线文件被删）/ unreadable / key_unavailable。
        只有 ok / sealed / absent 允许写。"""
        with self._lock:
            if not os.path.exists(self.data_file):
                self._integrity, self._read_only = "absent", False
                return self._integrity
            data = self._read_json(self.data_file, default=None)
            if not isinstance(data, dict):
                self._integrity, self._read_only = "unreadable", True
                logger.error("财务数据完整性：print_data.json 无法解析 → 只读保护（不覆盖数据）")
                return self._integrity
            current = self._mac_of(data)
            if not current:
                self._integrity, self._read_only = "key_unavailable", True
                logger.error("财务数据完整性：HMAC 密钥不可用 → 只读保护（不覆盖数据）")
                return self._integrity
            macinfo = self._read_json(self.mac_file, default=None)
            if isinstance(macinfo, dict) and macinfo.get("mac"):
                if hmac.compare_digest(str(macinfo.get("mac")), current):
                    self._integrity, self._read_only = "ok", False
                else:
                    self._integrity, self._read_only = "mismatch", True
                    logger.error("财务数据完整性校验失败：print_data.json 与上次基线不一致"
                                 "（被整份覆写/外部改过）→ 已进入只读保护，不覆盖数据")
                    self.audit("integrity_mismatch", module="integrity",
                               fields={"baseline": str(macinfo.get("mac"))[:12] + "…",
                                       "actual": current[:12] + "…"})
                return self._integrity
            if os.path.exists(self.seal_mark_file):
                self._integrity, self._read_only = "missing_mac", True
                logger.error("财务数据完整性：基线文件 %s 缺失（曾被删除？）→ 只读保护，需人工确认",
                             os.path.basename(self.mac_file))
                return self._integrity
            # S1-05 之前写的历史数据：首次启用保护。**不改数据本身**，只补基线。
            self._seal(data, current)
            self._integrity, self._read_only = "sealed", False
            logger.info("财务数据完整性：首次为既有 print_data.json 建立 HMAC 基线")
            return self._integrity

    def acknowledge_integrity(self, confirm: bool) -> dict:
        """操作员在页面上确认「这确实是我自己的改动」→ 按当前内容重建基线。"""
        if confirm is not True:
            return {"ok": False, "code": "confirm_required", "message": "需要显式确认（confirm=true）"}
        with self._lock:
            data = self._read_json(self.data_file, default=None)
            if not isinstance(data, dict):
                return {"ok": False, "code": "unreadable",
                        "message": "数据文件不可读，无法建立新基线（请人工检查 print_data.json）"}
            self._hmac_key = None                     # 可能换了机器/密钥不可用 → 重新取或重建
            mac = self._mac_of(data)
            if not mac:
                return {"ok": False, "code": "key_unavailable", "message": "完整性密钥不可用"}
            self._seal(data, mac)
            self._integrity, self._read_only = "resealed", False
            self.audit("integrity_rebaselined", module="integrity",
                       fields={"mac": mac[:12] + "…"})
            logger.warning("财务数据完整性：操作员已确认 → 按当前文件内容重建基线，写入恢复")
            return {"ok": True, "integrity": "resealed"}

    def current_mac(self) -> str:
        data = self._read_json(self.data_file, default=None)
        if not isinstance(data, dict):
            return ""
        return self._mac_of(data)

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def integrity_state(self) -> str:
        return self._integrity

    def status_payload(self, session_token: str = "") -> dict:
        """给页面的状态：每个模块是否需要设置、是否有可用的解锁会话、完整性/只读。"""
        with self._lock:
            modules = {}
            for key in FINANCE_MODULES:
                stored, source = self._stored_for(key)
                unset = self._is_unset(stored) or self._is_default_marker(stored)
                modules[key] = {
                    "configured": not unset,
                    "needs_setup": unset,
                    "legacy": (not unset) and self._is_legacy(stored),
                    "source": source,
                }
            ok, _code, sess = self.verify_session(session_token)
            session_info = {
                "active": bool(ok),
                "modules": list(sess.get("mods") or []) if ok else [],
                "expires_in": max(0, int(sess.get("exp") or 0) - _now_ts()) if ok else 0,
            }
            return {
                "modules": modules,
                "needs_setup": [k for k, v in modules.items() if v["needs_setup"]],
                "session": session_info,
                "session_ttl": self.session_ttl,
                "read_only": self._read_only,
                "integrity": self._integrity,
                "key_mode": self._key_mode,
            }

    # ── 凭据读写 ──

    def _local_credentials(self) -> dict:
        """本机凭据：优先服务端权威存储（CRED_KEY），再吃掉旧版 config.passwords（兼容导入）。"""
        data = self._read_json(self.data_file, default=None)
        out = {}
        if isinstance(data, dict):
            cfg = data.get("config") if isinstance(data.get("config"), dict) else {}
            pw = cfg.get("passwords") if isinstance(cfg.get("passwords"), dict) else {}
            for k, v in pw.items():
                if isinstance(v, str) and v:
                    out[k] = v
            creds = data.get(CRED_KEY)
            if isinstance(creds, dict):
                for k, v in creds.items():
                    if isinstance(v, str) and v:
                        out[k] = v            # 权威存储覆盖旧字段
        return out

    def _cloud_credentials(self):
        """云端 config.passwords（仅云端模式且可达时）；任何失败都返回 None（不抛）。
        带 10 秒缓存：状态查询/解锁会为多个模块问同一份配置，不能每个模块都打一次云端。"""
        if not self.api_url or not self.token:
            return None
        now = _now_ts()
        with self._lock:
            if self._cloud_cache_ready and now - self._cloud_cache_ts < 10:
                return self._cloud_cache
        result = None
        try:
            # S2-10 ⑥：出站令牌走请求头（query 会进 nginx access log；后端头优先）
            resp = _get_proxy_session().get(
                "%s/api/finance/config" % self.api_url,
                headers={"X-Printer-Token": self.token}, timeout=(2, 3))
            if resp.ok:
                body = resp.json()
                data = body.get("data") if isinstance(body, dict) else None
                cfg = data.get("config") if isinstance(data, dict) else None
                pw = cfg.get("passwords") if isinstance(cfg, dict) else None
                if isinstance(pw, dict):
                    result = pw
        except Exception:
            result = None
        with self._lock:
            self._cloud_cache = result
            self._cloud_cache_ts = now
            self._cloud_cache_ready = True
        return result

    def _stored_for(self, module: str):
        """返回 (存储值, 来源)。规则（写进文档，避免两处口径漂移）：
        1. 本机已是 PBKDF2 ⇒ 本机权威（幂等迁移的落地结果）；
        2. 否则云端有值 ⇒ 云端（首次升级的源）；
        3. 否则用本机旧格式（sha256:/明文）。
        """
        local = self._local_credentials()
        stored = (local.get(module) or "").strip()
        if stored.startswith(PBKDF2_SCHEME + "$"):
            return stored, "local"
        cloud = self._cloud_credentials()
        if isinstance(cloud, dict):
            cstored = cloud.get(module)
            cstored = cstored.strip() if isinstance(cstored, str) else ""
            if cstored and not self._is_unset(cstored):
                return cstored, "cloud"
        return stored, "local"

    @staticmethod
    def _is_unset(stored) -> bool:
        return not isinstance(stored, str) or not stored.strip()

    @staticmethod
    def _is_default_marker(stored) -> bool:
        """旧默认密码指纹（'123456' 的各种历史存法）→ 必须改密。"""
        if not isinstance(stored, str):
            return False
        s = stored.strip()
        if s == "123456":
            return True
        body = s[7:] if s.lower().startswith("sha256:") else s
        return body.lower() == LEGACY_DEFAULT_SHA256

    @staticmethod
    def _is_legacy(stored) -> bool:
        if not isinstance(stored, str) or not stored:
            return False
        return not stored.startswith(PBKDF2_SCHEME + "$")

    @staticmethod
    def _scheme_of(stored) -> str:
        if isinstance(stored, str) and stored.startswith(PBKDF2_SCHEME + "$"):
            return PBKDF2_SCHEME
        if isinstance(stored, str) and stored.lower().startswith("sha256:"):
            return "sha256-unsalted"
        return "plaintext"

    @staticmethod
    def _hash_password(password: str) -> str:
        """PBKDF2-HMAC-SHA256 + 每次全新随机 salt（每模块独立）。"""
        salt = secrets.token_bytes(PBKDF2_SALT_BYTES)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
        return "%s$%d$%s$%s" % (PBKDF2_SCHEME, PBKDF2_ITERATIONS, salt.hex(), dk.hex())

    @classmethod
    def verify_password_value(cls, password: str, stored: str) -> bool:
        """纯函数式口令校验（不碰文件）：PBKDF2 / 旧 sha256 / 旧明文。"""
        if not isinstance(stored, str) or not stored:
            return False
        if stored.startswith(PBKDF2_SCHEME + "$"):
            try:
                _scheme, iterations, salt_hex, dk_hex = stored.split("$", 3)
                iterations = int(iterations)
            except (ValueError, TypeError):
                return False
            if iterations < 1 or iterations > 5_000_000:
                return False
            try:
                salt = bytes.fromhex(salt_hex)
                expected = bytes.fromhex(dk_hex)
            except ValueError:
                return False
            dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
            return hmac.compare_digest(dk, expected)
        if stored.lower().startswith("sha256:"):
            digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
            return hmac.compare_digest(digest, stored[7:].strip().lower())
        # 旧明文（极老的数据）：只在没有其他格式时才走到这里
        return hmac.compare_digest(password, stored)

    def _validate_new_password(self, password: str):
        if not isinstance(password, str) or not password:
            return False, "请输入新密码"
        if len(password) < MIN_PASSWORD_LEN:
            return False, "密码至少 %d 位" % MIN_PASSWORD_LEN
        if password in WEAK_PASSWORDS:
            return False, "该密码在弱口令清单里（含旧的默认密码），请换一个"
        if len(set(password)) == 1:
            return False, "密码不能是同一个字符重复"
        return True, ""

    def _store_credentials(self, module: str, stored_hash: str, source: str = "") -> bool:
        """把（升级后的）凭据写进 print_data.json：顶层权威键 + 本地模式同步 config.passwords。
        只读保护时不写（宁可下次再升级一次，也不改动被篡改的文件）。"""
        with self._lock:
            if self._read_only:
                logger.warning("财务凭据升级未落盘：数据处于只读保护（%s）", self._integrity)
                return False
            data = self._read_json(self.data_file, default=None)
            if not isinstance(data, dict):
                data = {}
            creds = data.get(CRED_KEY)
            if not isinstance(creds, dict):
                creds = {}
            creds[module] = stored_hash
            data[CRED_KEY] = creds
            # 本地模式同步旧字段（config.passwords），页面/云端合并逻辑照旧能看到密码状态。
            # 云端模式**不写**这里：那份 config 是"云端配置的本机镜像"，
            # 一旦写进密码哈希，页面的 hasRealData() 会把它判成"本地也有真实收支数据"，
            # 于是每次打开都弹「本地与云端均存在数据，是否合并」（S1-05 实测踩到）。
            if self.finance_mode != "cloud":
                cfg = data.get("config")
                if not isinstance(cfg, dict):
                    cfg = {}
                pw = cfg.get("passwords")
                if not isinstance(pw, dict):
                    pw = {}
                pw[module] = stored_hash
                cfg["passwords"] = pw
                data["config"] = cfg
            try:
                self._write_json(self.data_file, data)
                mac = self._mac_of(data)
                if mac:
                    self._seal(data, mac)
            except OSError as e:
                logger.error("财务凭据写入失败: %s", type(e).__name__)
                return False
            self._cloud_cache_ready = False     # 凭据变了 → 云端缓存作废（下次重新取）
            return True

    # ── 会话 ──

    def issue_session(self, modules) -> str:
        with self._lock:
            mods = sorted({m for m in (modules or []) if m in FINANCE_MODULES})
            sid = secrets.token_hex(16)
            iat, exp = _now_ts(), _now_ts() + self.session_ttl
            self._sessions[sid] = {"exp": exp, "mods": mods, "iat": iat}
            self._prune_sessions()
            payload = {"sid": sid, "iat": iat, "exp": exp, "host": self.machine, "mods": mods}
            raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            sig = hmac.new(self._secret, raw, hashlib.sha256).hexdigest()
            return "%s.%s" % (_b64e(raw), sig)

    def verify_session(self, token: str, module=None):
        """返回 (ok, code, session)。code ∈ ''/finance_session_required/invalid/expired/forbidden。"""
        with self._lock:
            if not token:
                return False, "finance_session_required", None
            if not isinstance(token, str) or "." not in token:
                return False, "finance_session_invalid", None
            raw_b64, sig = token.rsplit(".", 1)
            try:
                raw = _b64d(raw_b64)
            except Exception:
                return False, "finance_session_invalid", None
            expect = hmac.new(self._secret, raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expect, sig):
                return False, "finance_session_invalid", None
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                return False, "finance_session_invalid", None
            if not isinstance(payload, dict):
                return False, "finance_session_invalid", None
            if payload.get("host") != self.machine:      # 绑定本机：换机器/换账户一律作废
                return False, "finance_session_invalid", None
            sid = payload.get("sid") or ""
            sess = self._sessions.get(sid)
            if not sess:
                return False, "finance_session_invalid", None
            if int(payload.get("exp") or 0) <= _now_ts() or int(sess.get("exp") or 0) <= _now_ts():
                self._sessions.pop(sid, None)
                return False, "finance_session_expired", None
            mods = list(sess.get("mods") or [])
            if module and module not in mods:
                return False, "finance_session_forbidden", None
            return True, "", {"sid": sid, "exp": sess.get("exp"), "mods": mods}
    def revoke_session(self, token: str) -> bool:
        with self._lock:
            if not isinstance(token, str) or "." not in token:
                return False
            raw_b64 = token.rsplit(".", 1)[0]
            try:
                payload = json.loads(_b64d(raw_b64).decode("utf-8"))
            except Exception:
                return False
            sid = (payload or {}).get("sid") if isinstance(payload, dict) else ""
            return bool(self._sessions.pop(sid, None))

    def _prune_sessions(self) -> None:
        now = _now_ts()
        for sid in [k for k, v in self._sessions.items() if int(v.get("exp") or 0) <= now]:
            self._sessions.pop(sid, None)

    # ── 解锁 / 设置 / 改密 ──

    def unlock(self, module: str, password: str) -> dict:
        module = (module or "").strip()
        if module not in FINANCE_MODULES:
            return {"ok": False, "code": "unknown_module", "message": "未知的密码模块"}
        if not isinstance(password, str) or not password:
            return {"ok": False, "code": "empty_password", "message": "请输入密码"}
        stored, source = self._stored_for(module)
        if self._is_unset(stored) or self._is_default_marker(stored):
            self.audit("unlock_denied", module=module, fields={"reason": "needs_setup"})
            return {"ok": False, "code": "needs_setup",
                    "message": "该模块还没有设置密码（默认密码已停用），请先设置新密码"}
        if password in WEAK_PASSWORDS:
            # 先于校验拒绝：默认/弱口令永远不能用来解锁
            self.audit("unlock_denied", module=module, fields={"reason": "default_password"})
            return {"ok": False, "code": "default_password",
                    "message": "默认密码已停用，请先在「设置 → 敏感模块密码」里设置新密码"}
        if not self.verify_password_value(password, stored):
            self.audit("unlock_denied", module=module, fields={"reason": "wrong_password",
                                                               "scheme": self._scheme_of(stored)})
            return {"ok": False, "code": "wrong_password", "message": "密码错误"}
        upgraded = False
        stored_hash = ""
        if self._is_legacy(stored):
            # 旧的无盐 SHA-256 / 明文 → 首次解锁成功时自动升级为 PBKDF2，写回 print_data.json
            new_hash = self._hash_password(password)
            if self._store_credentials(module, new_hash, source):
                upgraded, stored_hash = True, new_hash
            self.audit("password_upgraded", module=module,
                       fields={"from": self._scheme_of(stored), "to": PBKDF2_SCHEME,
                               "persisted": upgraded})
        token = self.issue_session([module])
        self.audit("unlock_ok", module=module,
                   fields={"source": source, "upgraded": upgraded, "scheme":
                           PBKDF2_SCHEME if upgraded else self._scheme_of(stored)})
        out = {"ok": True, "session": token, "expires_in": self.session_ttl,
               "modules": [module], "upgraded": upgraded}
        if stored_hash:
            out["stored_hash"] = stored_hash
        return out

    def setup(self, module: str, new_password: str) -> dict:
        """首次设置密码（默认/未设置 → 新密码）。已配置的模块必须先解锁再改，不走这里。"""
        targets = list(FINANCE_MODULES) if (not module or module == "all") else [module]
        if any(m not in FINANCE_MODULES for m in targets):
            return {"ok": False, "code": "unknown_module", "message": "未知的密码模块"}
        allowed = []
        for m in targets:
            stored, _src = self._stored_for(m)
            if self._is_unset(stored) or self._is_default_marker(stored):
                allowed.append(m)
        if not allowed:
            return {"ok": False, "code": "already_configured",
                    "message": "该模块已设置密码，请先解锁再在「设置」里修改"}
        valid, msg = self._validate_new_password(new_password)
        if not valid:
            return {"ok": False, "code": "weak_password", "message": msg}
        stored_map, persisted = {}, []
        for m in allowed:
            h = self._hash_password(new_password)        # 每个模块各自独立 salt
            if self._store_credentials(m, h):
                persisted.append(m)
            stored_map[m] = h
        if not persisted:
            return {"ok": False, "code": "read_only",
                    "message": "数据处于只读保护（完整性校验失败），请先确认后再设置密码"}
        self.audit("password_setup", module=",".join(persisted), fields={"modules": persisted})
        return {"ok": True, "stored": {m: stored_map[m] for m in persisted}, "modules": persisted}

    def change_password(self, module: str, new_password: str, session_mods) -> dict:
        """改密（设置页）。必须已解锁「设置」模块 —— 与既有 UI 的权限口径一致。"""
        if module not in FINANCE_MODULES:
            return {"ok": False, "code": "unknown_module", "message": "未知的密码模块"}
        if "settings" not in (session_mods or []):
            return {"ok": False, "code": "finance_session_forbidden",
                    "message": "修改密码需要先解锁「设置」模块"}
        valid, msg = self._validate_new_password(new_password)
        if not valid:
            return {"ok": False, "code": "weak_password", "message": msg}
        h = self._hash_password(new_password)
        if not self._store_credentials(module, h):
            return {"ok": False, "code": "read_only", "message": "数据只读，改密未生效"}
        self.audit("password_change", module=module, fields={"to": PBKDF2_SCHEME})
        return {"ok": True, "stored_hash": h}

    # ── 数据读写（含完整性） ──

    def load_data(self):
        with self._lock:
            if not os.path.exists(self.data_file):
                return None, False
            data = self._read_json(self.data_file, default=None)
            return (data if isinstance(data, dict) else None), True

    def _preserve_credentials(self, existing, incoming) -> dict:
        """页面提交的整份数据落盘前的两道折返：
        ① 保留服务端凭据（页面不认这个键，整份覆盖会把它抹掉）；
        ② 服务端权威凭据覆盖页面提交的旧哈希（否则「保存页面状态」会把刚升级的密码回退成旧格式）。"""
        incoming = dict(incoming) if isinstance(incoming, dict) else {}
        creds = {}
        if isinstance(existing, dict) and isinstance(existing.get(CRED_KEY), dict):
            creds.update({k: v for k, v in existing[CRED_KEY].items() if isinstance(v, str) and v})
        if isinstance(incoming.get(CRED_KEY), dict):
            creds.update({k: v for k, v in incoming[CRED_KEY].items() if isinstance(v, str) and v})
        if not creds:
            return incoming
        incoming[CRED_KEY] = creds
        cfg = incoming.get("config")
        if not isinstance(cfg, dict):
            cfg = {}
        pw = cfg.get("passwords")
        if not isinstance(pw, dict):
            pw = {}
        for mod, stored in creds.items():
            pw[mod] = stored
        cfg["passwords"] = pw
        incoming["config"] = cfg
        return incoming

    def write_data(self, incoming) -> dict:
        """页面保存 → 完整性门禁 → 原子写 → 重建 HMAC 基线 → 审计。"""
        with self._lock:
            state = self.check_integrity()
            if state in ("mismatch", "missing_mac", "unreadable", "key_unavailable"):
                raise PermissionError("数据完整性校验失败（%s），已进入只读保护：本机拒绝覆盖数据" % state)
            existing, _exists = self.load_data()
            merged = self._preserve_credentials(existing, incoming)
            before = finance_data_summary(existing)
            after = finance_data_summary(merged)
            self._write_json(self.data_file, merged)
            mac = self._mac_of(merged)
            if mac:
                self._seal(merged, mac)
            if before != after:
                self.audit("local_data_write", module="data",
                           fields={"before": before, "after": after})
            return merged

    # ── 审计（只读日志） ──

    def _rotate_audit(self) -> None:
        try:
            if os.path.getsize(self.audit_file) > _AUDIT_MAX_BYTES:
                os.replace(self.audit_file, self.audit_file + ".1")
        except OSError:
            pass

    def audit(self, event: str, module: str = "", fields=None, extra=None) -> None:
        """追加一条审计；字段一律过脱敏（不含密码/密钥/哈希）。写失败只告警，绝不影响业务。"""
        try:
            rec = {"ts": _iso_now(), "event": event, "module": module}
            if fields:
                rec["fields"] = _scrub_audit(fields)
            if extra:
                rec["extra"] = _scrub_audit(extra)
            line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
            with self._lock:
                os.makedirs(self.logs_dir, exist_ok=True)
                self._rotate_audit()
                with open(self.audit_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                try:
                    os.chmod(self.audit_file, 0o600)
                except OSError:
                    pass
        except Exception as e:                            # noqa: BLE001
            logger.warning("财务审计日志写入失败: %s", type(e).__name__)


_FINANCE_CACHE = {}
_FINANCE_CACHE_LOCK = threading.Lock()


def get_finance_security(data_file: str = "", api_url: str = "", token: str = "",
                         finance_mode: str = "") -> FinanceSecurity:
    """按数据文件路径取（进程内单例）：会话登记表必须跨请求存活。"""
    path = os.path.abspath(data_file or _DEFAULT_DATA_FILE)
    with _FINANCE_CACHE_LOCK:
        sec = _FINANCE_CACHE.get(path)
        if sec is None:
            sec = FinanceSecurity(path, api_url=api_url, token=token,
                                  finance_mode=finance_mode or "cloud")
            _FINANCE_CACHE[path] = sec
        else:
            if api_url:
                sec.api_url = api_url.rstrip("/")
            if token:
                sec.token = token
            if finance_mode:
                sec.finance_mode = finance_mode
        return sec


def load_bindings() -> dict:
    """读取 openid → 成员 绑定配置"""
    with _BINDINGS_FILE_LOCK:
        if os.path.exists(_BINDINGS_FILE):
            try:
                with open(_BINDINGS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
    return {}


def save_bindings(data: dict) -> None:
    """保存 openid → 成员 绑定配置"""
    with _BINDINGS_FILE_LOCK:
        os.makedirs(os.path.dirname(_BINDINGS_FILE), exist_ok=True)
        with open(_BINDINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def _strip_token_from_query(query: str) -> str:
    """剥离客户端 query 中的 token 参数，防止客户端可控 token 覆盖后端令牌。"""
    if not query:
        return ""
    try:
        qs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    except Exception:
        return ""
    qs = [(k, v) for k, v in qs if k != "token"]
    return urllib.parse.urlencode(qs)


def load_local_data_file() -> dict | None:
    """读取本地收支数据文件（print_data.json），供 GUI 归属下拉同步成员名单。
    文件不存在或内容损坏时返回 None。与 HTTP 读写共用 _DATA_FILE_LOCK。"""
    with _DATA_FILE_LOCK:
        if not os.path.exists(_DEFAULT_DATA_FILE):
            return None
        try:
            with open(_DEFAULT_DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except Exception:
            return None


def clear_local_data_files() -> None:
    """删除本地收支数据文件与 openid 绑定文件（关闭云端 / CEO 迁移完成时调用）。"""
    with _DATA_FILE_LOCK, _BINDINGS_FILE_LOCK:
        for p in (_DEFAULT_DATA_FILE, _BINDINGS_FILE):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


def _extract_proxy_body(resp) -> dict:
    """只透传 {success, message, data} 白名单字段；非 JSON 响应统一转通用错误。
    额外透传 pricing（/api/pricing 返回顶层 pricing 而非 data 包装，
    供收支清算设置页的「打印价格」区块读写服务器定价配置）。
    额外透传 devices / active_client_id / active_owner_name（/api/printer/devices 返回顶层
    设备列表，供收支清算「授权」页展示设备与所有者——2026-11 修复：此前被白名单剥掉
    导致授权页设备列表空白、绑定后所有者不刷新）。
    额外透传 order_id / status / previous_status / files_updated（/api/admin/order_status 的
    回执字段；v7.0 订单状态订正——此前被白名单静默剥掉，页面拿不到改了几条、原来是什么状态）。
    额外透传 order_type / previous_order_type / is_admin_print（/api/admin/order_type 的回执；
    v7.1 订单类型订正——同款白名单问题，不补就「改成功但页面不知道原来是什么类型」）。
    一律「响应当中有才透传」，不存在时不填空值（语义同上面几个）。"""
    try:
        data = resp.json()
    except Exception:
        return {"success": False, "message": f"云端响应异常（HTTP {resp.status_code}）"}
    if not isinstance(data, dict):
        return {"success": False, "message": "云端响应格式错误"}
    out = {"success": bool(data.get("success"))}
    msg = data.get("message")
    if isinstance(msg, str):
        out["message"] = msg
    if "data" in data:
        out["data"] = data["data"]
    if "pricing" in data:
        out["pricing"] = data["pricing"]
    if "devices" in data:
        out["devices"] = data["devices"]
    if "active_client_id" in data:
        out["active_client_id"] = data["active_client_id"]
    if "active_owner_name" in data:
        out["active_owner_name"] = data["active_owner_name"]
    if "order_id" in data:
        out["order_id"] = data["order_id"]
    if "status" in data:
        out["status"] = data["status"]
    if "previous_status" in data:
        out["previous_status"] = data["previous_status"]
    if "files_updated" in data:
        out["files_updated"] = data["files_updated"]
    # v7.1 订单类型订正的回执（与上面同一条规矩：有才透传）
    for _k in ("order_type", "previous_order_type", "is_admin_print"):
        if _k in data:
            out[_k] = data[_k]
    return out


# ── S2-10 公共判定（模块级：handler / 服务器 / 验收脚本都能直接调） ──

def _token_equal(candidate, expected) -> bool:
    """启动令牌 / nonce 的**恒时**比较（S2-10 ④）。

    · 双方都必须是非空 str（令牌未注入时 fail-closed：空串永远不通过）；
    · 非 ASCII 直接判失败 —— `hmac.compare_digest` 对含非 ASCII 的 str 会抛 TypeError，
      而这些值来自攻击者可控的请求头/查询串，绝不能让它变成 500。
    """
    if not isinstance(candidate, str) or not isinstance(expected, str):
        return False
    if not candidate or not expected:
        return False
    try:
        if not (candidate.isascii() and expected.isascii()):
            return False
        return hmac.compare_digest(candidate, expected)
    except TypeError:                                   # 防御：类型/编码的极端情况
        return False


def allowed_hosts(port: int) -> frozenset:
    """Host 白名单（S2-10 ②）：只认本机回环地址 + **实际**端口。

    `port=0` 时由 `server_port` 反查，所以调用方必须传**绑定之后**的端口；
    取不到端口 → 空集合 = 一切 Host 都不匹配（fail-closed，宁可 403 也不放行）。
    """
    try:
        p = int(port or 0)
    except (TypeError, ValueError):
        return frozenset()
    if p <= 0:
        return frozenset()
    return frozenset("%s:%d" % (name, p) for name in _LOOPBACK_HOSTS)


def normalize_proxy_target(proxy_path: str):
    """`/api/proxy/<x>` → 云端目标路径。返回 `(target, code)`（S2-10 ⑤）。

    code 为空 = 通过；`bad_path` = 路径本身非法（`..` / 空段 / 反斜杠 / 编码越界）→ 400；
    `not_allowed` = 归一化后不在白名单 → 403。**两种失败都必须在发出上游请求之前返回**。
    """
    if not isinstance(proxy_path, str) or not proxy_path.startswith(_PROXY_PREFIX + "/"):
        return "", "bad_path"
    rest = proxy_path[len(_PROXY_PREFIX):]              # '/admin/order_status'
    decoded = urllib.parse.unquote(rest, errors="replace")
    for probe in (rest, decoded):
        if "\\" in probe or "\x00" in probe:
            return "", "bad_path"
        segs = probe.split("/")
        if segs and segs[0] == "":
            segs = segs[1:]
        if any(seg in ("", ".", "..") for seg in segs):
            return "", "bad_path"
    target = "/api" + rest
    if posixpath.normpath(target) != target:            # 归一化后必须**原样**（双斜杠等一律拒）
        return "", "bad_path"
    if target not in _PROXY_ALLOWED_TARGETS:
        return "", "not_allowed"
    return target, ""


class _StatsHandler(SimpleHTTPRequestHandler):
    """自定义请求处理器"""

    # 由 StatsServer 在构造时注入
    api_url: str = ""
    token: str = ""
    launch_token: str = ""  # 启动令牌：仅本地打印工具启动时附带，每次启动随机
    finance_mode: str = "cloud"  # 'cloud' 或 'local'：收支清算数据/配置的存储来源
    # 本机设备信息（2026-11：收支清算「授权」页展示本设备/绑定所有者）
    device_name: str = ""
    client_id: str = ""
    take_orders: bool = False
    # S1-05：上一次看到的云端收支配置摘要（审计 before，由 GET /api/proxy/finance/config 预热）
    _last_finance_summary = None
    # S1-09：连接级空闲/读写超时（秒）。socketserver.StreamRequestHandler.setup() 会把它
    # 设到 socket 上：半开连接/慢速客户端不会永远占着一个工作线程。
    timeout = _SOCKET_TIMEOUT

    def __init__(self, *args, **kwargs):
        # 设置静态文件根目录
        super().__init__(*args, directory=_STATIC_DIR, **kwargs)

    def log_message(self, format, *args):
        logger.debug(f"StatsServer: {format % args}")

    # ── S2-10 ⑦ 安全响应头：所有响应（含基类发的 404/304/静态文件）都带上 ──
    # 做法是在 send_response/send_header/end_headers 三个钩子上补齐，而不是逐个路由手写：
    # 少写一处就漏一处（favicon、403、静态文件、send_error 都是基类发的）。

    def send_response(self, code, message=None):
        self._sec_headers_done = False
        self._resp_is_html = False
        super().send_response(code, message)

    def send_header(self, keyword, value):
        try:
            if str(keyword).lower() == "content-type" and "text/html" in str(value).lower():
                self._resp_is_html = True
        except Exception:                               # noqa: BLE001
            pass
        super().send_header(keyword, value)

    def end_headers(self):
        if not getattr(self, "_sec_headers_done", True):
            self._sec_headers_done = True
            try:
                for _k, _v in _SECURITY_HEADERS:
                    self.send_header(_k, _v)
                if getattr(self, "_resp_is_html", False):
                    self.send_header("Content-Security-Policy", _HTML_CSP)
                self.send_header("Vary", "Origin")      # ACAO 按 Origin 变：缓存要按它分键
            except Exception:                           # noqa: BLE001
                pass                                    # 头发不出去时别把整个响应吞掉
        super().end_headers()

    # ── S2-10 ② Host 白名单 ──

    def _allowed_hosts(self) -> frozenset:
        """本机回环主机名 + **实际**端口。优先取服务器实例上的（端口只有绑定后才知道）。"""
        srv = getattr(self, "server", None)
        hosts = getattr(srv, "allowed_hosts", None)
        if isinstance(hosts, frozenset) and hosts:
            return hosts
        return allowed_hosts(getattr(srv, "server_port", 0))

    def _client_host_ok(self) -> bool:
        """Host 必须是 `127.0.0.1:<port>` / `localhost:<port>` / `[::1]:<port>`。

        缺 Host（HTTP/1.0、原始 socket）、DNS rebinding 用的域名、其它端口一律 False。
        """
        raw = (self.headers.get("Host") or "").strip().lower()
        if not raw:
            return False
        for allowed in self._allowed_hosts():
            if _token_equal(raw, allowed):
                return True
        return False

    def _guard_host(self, path: str = "") -> bool:
        """所有请求（含 HEAD/OPTIONS）先过 Host 白名单。返回 False = 已回包。"""
        if self._client_host_ok():
            return True
        logger.info("拒绝 Host=%r 的请求（只允许 127.0.0.1/localhost/[::1] + 实际端口）",
                    (self.headers.get("Host") or "")[:120])
        if path.startswith("/api/"):
            self._send_json({"success": False, "code": "bad_host",
                             "message": "Host 校验失败：本服务只接受本机回环地址访问"}, 403)
        else:
            self._serve_403_page()
        return False

    # ── S2-10 ① 一次性 nonce（HTML 入口用；由签发它的 StatsServer 校验并作废） ──

    def _query_nonce(self) -> str:
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        except Exception:                               # noqa: BLE001
            return ""
        return ((qs.get("nonce") or [""])[0] or "").strip()

    def _consume_nonce(self, nonce: str) -> bool:
        owner = getattr(getattr(self, "server", None), "nonce_owner", None)
        if owner is None:
            return False
        return bool(owner.consume_nonce(nonce))

    # ── 启动令牌（④ 恒时比较；API 只认请求头） ──

    def _check_launch_token(self, allow_query: bool = False) -> bool:
        """校验启动令牌。

        · API（`/api/*`）：**只**认 `X-Launch-Token` 请求头 —— 查询串令牌已停用
          （S2-10：令牌不再出现在 URL 里）。
        · `allow_query=True`：额外接受 `?token=`，仅给**老的 HTML 入口**做兼容
          （老书签/旧流程）；页面拿到的是服务端注入的那份令牌，后续请求照旧走请求头。
        比较一律 `hmac.compare_digest`（S2-10 ④）。
        """
        header = self.headers.get("X-Launch-Token") or ""
        if _token_equal(header, self.launch_token):
            return True
        if allow_query:
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            except Exception:                           # noqa: BLE001
                return False
            qtoken = (qs.get("token") or [""])[0]
            if _token_equal(qtoken, self.launch_token):
                return True
        return False

    def _cors_origin(self) -> str:
        """S2-10 ③：只回显**本服务自己的** origin（回环主机 + 实际端口）。

        其它 Origin（`null`、外部域名、其它端口）返回空串 ⇒ 响应里根本不放
        `Access-Control-Allow-Origin`，跨源页面读不到内容（同源请求本来也不需要它）。
        """
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return ""
        low = origin.lower()
        if not low.startswith("http://"):
            return ""
        host_part = low[len("http://"):]
        for allowed in self._allowed_hosts():
            if _token_equal(host_part, allowed):
                return origin
        return ""

    def _write_body(self, body: bytes):
        """写响应体；HEAD 请求按协议**不写** body（响应头照旧发，含 Content-Length）。"""
        if (self.command or "").upper() == "HEAD":
            return
        self.wfile.write(body)

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()
        self._write_body(body)

    # ── S1-05 财务安全（密码 / 会话 / 完整性 / 审计） ──

    def _fin(self) -> FinanceSecurity:
        """本请求对应的财务安全门（按数据文件路径取进程内单例）。"""
        return get_finance_security(
            getattr(self, "_data_file", _DEFAULT_DATA_FILE),
            api_url=getattr(self, "api_url", ""),
            token=getattr(self, "token", ""),
            finance_mode=getattr(self, "finance_mode", ""))

    def _finance_session_token(self) -> str:
        return (self.headers.get(FINANCE_SESSION_HEADER) or "").strip()

    def _finance_write_allowed(self, path: str) -> bool:
        """**所有写操作**的后端二次校验（S1-05 ③）。返回 True=放行（False=已回包）。

        规则：`FINANCE_WRITE_MODULES` 里登记的路径按模块校验；**未登记的写路由一律按
        "任意已解锁模块"要求**（fail-closed：以后新增写接口不会因为忘记登记而裸奔）。
        读操作不经过这里（向后兼容：没有 session 也能看数据）。
        """
        if path in FINANCE_AUTH_OPEN_PATHS:
            return True
        required = FINANCE_WRITE_MODULES.get(path)
        ok, code, _sess = self._fin().verify_session(self._finance_session_token(), required)
        if ok:
            return True
        if code == "finance_session_expired":
            status, msg = 401, "解锁会话已过期（30 分钟），请重新输入密码"
        elif code == "finance_session_forbidden":
            status, msg = 403, "该模块需要单独解锁后才能写入"
        else:
            status, msg = 403, "需要先解锁财务模块（finance_session）才能写入"
        self._fin().audit("write_denied", module=required or "-",
                          fields={"path": path, "code": code})
        self._send_json({"success": False, "code": code, "message": msg}, status)
        return False

    def _request_body_ok(self) -> bool:
        """S1-09：读之前先看 Content-Length —— 超限直接 413 并断开，绝不"先读完 1GB 再判断"。"""
        raw = self.headers.get("Content-Length", "")
        if not raw:
            return True
        try:
            length = int(raw)
        except (TypeError, ValueError):
            self._send_json({"success": False, "code": "bad_content_length",
                             "message": "非法的 Content-Length"}, 400)
            return False
        if length < 0 or length > _MAX_BODY_SIZE:
            self.close_connection = True     # 不读请求体，直接断开（避免把超大 body 收进来）
            logger.warning("拒绝超大请求体：Content-Length=%s（上限 %.0fMB）",
                           raw, _MAX_BODY_SIZE / (1024 * 1024))
            self._send_json({"success": False, "code": "body_too_large",
                             "message": "请求体过大（上限 %dMB）" % (_MAX_BODY_SIZE // (1024 * 1024))},
                            413)
            return False
        return True

    def _read_body(self) -> bytes:
        """读取请求体：校验 Content-Length 上限（10MB），并设置读取超时防挂起。"""
        raw = self.headers.get("Content-Length", "")
        if not raw:
            return b""
        try:
            length = int(raw)
        except (TypeError, ValueError):
            raise ValueError("非法的 Content-Length")
        if length < 0 or length > _MAX_BODY_SIZE:
            raise ValueError(f"请求体过大（上限 {_MAX_BODY_SIZE // (1024 * 1024)}MB）")
        self.connection.settimeout(_BODY_READ_TIMEOUT)
        return self.rfile.read(length)

    def do_OPTIONS(self):
        """CORS 预检（S2-10 ③：只对本服务 origin 回 CORS 头；Allow-Headers 补齐实际用的头）。"""
        if not self._guard_host(urllib.parse.urlparse(self.path).path):
            return
        self.send_response(204)
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "X-Launch-Token, X-Finance-Session, Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ── 路由分发 ──

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # S2-10 ②：Host 白名单（DNS rebinding 的第一道闸门）—— 所有请求都要过
        if not self._guard_host(path):
            return

        # 启动令牌门：所有 /api/* 请求均须携带有效令牌
        # （页面 API 请求经 X-Launch-Token 请求头；查询串令牌只对老 HTML 入口兼容）
        if path.startswith("/api/"):
            if not self._check_launch_token():
                return self._send_json({"success": False, "message": "未授权：请通过本地打印工具打开收支清算"}, 403)

        # 健康检查：本地服务存活 + 云端连通性
        if path == "/api/health":
            return self._handle_health()

        # S1-05：财务安全状态（只读接口：读操作不需要 finance_session）
        if path == "/api/local/finance_auth/status":
            return self._handle_finance_status()

        # 代理请求：/api/proxy/xxx → 云端 API
        if path.startswith("/api/proxy/"):
            return self._proxy_get(path, parsed.query)

        # 本地绑定读取
        if path == "/api/local/bindings":
            return self._handle_get_bindings()

        # 本机设备信息（收支清算「授权」页）
        if path == "/api/local/device":
            return self._handle_get_device()

        # 本地数据加载
        if path == "/api/local/data":
            return self._handle_get_data()

        # 本地订单统计（读本地订单库 SQLite）
        if path == "/api/local/orders":
            return self._handle_local_orders(parsed.query)

        # 默认：静态文件
        return self._serve_static(path)

    def do_HEAD(self):
        """S2-10：HEAD 也必须过同一套门。

        旧实现没有覆写 `do_HEAD`，基类 `SimpleHTTPRequestHandler.do_HEAD` 直接把静态文件的
        响应头发出去了 —— 启动令牌与 Host 两道闸门全被绕过（能探测/校验任意静态文件是否存在）。
        这里统一走 `_serve_static(head_only=True)`：同样校验 Host + 令牌，只是不写 body。
        """
        parsed = urllib.parse.urlparse(self.path)
        if not self._guard_host(parsed.path):
            return
        return self._serve_static(parsed.path, head_only=True)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if not self._guard_host(path):
            return

        # 启动令牌门（POST 一律要求有效令牌，且只认请求头）
        if not self._check_launch_token():
            return self._send_json({"success": False, "message": "未授权：请通过本地打印工具打开收支清算"}, 403)

        # S1-09：请求体上限（读之前就判：超限 = 413 + 断开，不把超大 body 收进内存）
        if not self._request_body_ok():
            return

        # S2-10 ⑤：代理路径先归一化 + 白名单 —— 越界/未知直接 4xx（不读请求体、不发上游请求）
        if path.startswith(_PROXY_PREFIX + "/") and normalize_proxy_target(path)[1]:
            return self._reject_proxy_path(path)

        # S1-05：**所有**写操作的财务会话二次校验（登录/首次设置/登出除外）
        if not self._finance_write_allowed(path):
            return

        # ── S1-05 财务安全接口（密码 / 会话 / 完整性确认） ──
        if path == "/api/local/finance_auth/status":
            return self._handle_finance_status()
        if path == "/api/local/finance_auth/unlock":
            return self._handle_finance_unlock()
        if path == "/api/local/finance_auth/setup":
            return self._handle_finance_setup()
        if path == "/api/local/finance_auth/password":
            return self._handle_finance_password()
        if path == "/api/local/finance_auth/logout":
            return self._handle_finance_logout()
        if path == "/api/local/finance_integrity/ack":
            return self._handle_finance_integrity_ack()

        # 代理 POST 请求
        if path.startswith("/api/proxy/"):
            return self._proxy_post(path, parsed.query)

        # 本地绑定写入
        if path == "/api/local/bindings":
            return self._handle_post_bindings()

        # 本地数据保存
        if path == "/api/local/data":
            return self._handle_post_data()

        # 未知
        self._send_json({"success": False, "message": "未找到"}, 404)

    def do_DELETE(self):
        """删除本地数据（启动令牌门校验后路由到本地数据删除）。"""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if not self._guard_host(path):
            return

        if not self._check_launch_token():
            return self._send_json({"success": False, "message": "未授权：请通过本地打印工具打开收支清算"}, 403)

        # S1-05：删除数据也是写操作，同样要财务会话
        if not self._finance_write_allowed(path):
            return

        # 本地数据删除（CEO 迁移到云端 / 关闭云端清空缓存时由页面调用）
        if path == "/api/local/data":
            return self._handle_delete_data()

        self._send_json({"success": False, "message": "未找到"}, 404)

    # ── 静态文件 ──

    def _serve_static(self, path: str, head_only: bool = False):
        """提供静态文件，根路径默认返回 settlement.html。

        HTML 入口（S2-10 ①）：优先消费**一次性 nonce**（`?nonce=`，用过即废、重放 403）；
        兼容老的 `?token=`（只对 HTML 入口生效 —— API 仍然只认 X-Launch-Token 请求头）。
        注入 __LAUNCH_TOKEN__（页面后续请求用它做请求头）与 __FINANCE_MODE__。
        公共库文件（chart.umd.min.js）豁免令牌；其余静态文件（含数据文件 print_data.json）
        一律校验令牌。`head_only=True`（HEAD 请求）只发响应头 —— body 由 `_write_body` 统一拦掉。
        """
        serve_html = (path in ("", "/", "/settlement.html"))
        if serve_html:
            nonce = self._query_nonce()
            if nonce:
                # 一次性 nonce：伪造 / 过期 / 重放一律 403（**不回退**到 ?token=）
                if not self._consume_nonce(nonce):
                    return self._serve_403_page()
                return self._serve_injected_html("settlement.html")
            if not self._check_launch_token(allow_query=True):
                return self._serve_403_page()
            return self._serve_injected_html("settlement.html")
        if path == "/favicon.ico":
            # 浏览器默认请求 favicon，走令牌门会 403（控制台红字）。返回 204 消除噪音。
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path in _PUBLIC_STATIC_FILES:
            self.path = path
            return super().do_HEAD() if head_only else super().do_GET()
        # 非公共静态文件：一律要求启动令牌，防止绕过令牌门直接读取数据文件
        if not self._check_launch_token(allow_query=True):
            return self._serve_403_page()
        self.path = path
        return super().do_HEAD() if head_only else super().do_GET()

    def _serve_injected_html(self, name: str = "settlement.html"):
        """读取指定 HTML 并注入启动令牌与存储模式。"""
        html_path = os.path.join(_STATIC_DIR, name)
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.error(f"读取页面失败: {e}")
            return self._send_json({"success": False, "message": f"读取页面失败: {e}"}, 500)
        content = content.replace("__LAUNCH_TOKEN__", self.launch_token)
        content = content.replace("__FINANCE_MODE__", self.finance_mode)
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # 禁止缓存：settlement.html 每次启动注入随机 token，且开发迭代频繁，
        # 浏览器缓存旧 HTML 会导致「改了不生效/功能缺失」（如滚动收起失效）
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self._write_body(body)

    def _serve_403_page(self):
        """未带有效启动凭据（或 Host/nonce 不合格）时返回 403 提示页。"""
        body = ("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='UTF-8'>"
                "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
                "<title>访问受限</title></head>"
                "<body style='font-family:Segoe UI,Microsoft YaHei,sans-serif;background:#f5f6fa;"
                "color:#1a1a2e;display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
                "<div style='text-align:center;padding:24px'><div style='font-size:56px'>🔒</div>"
                "<h2 style='margin:12px 0 6px'>访问受限</h2>"
                "<p style='color:#6b7280'>收支清算页面仅能从本地打印工具中打开。</p>"
                "<p style='color:#9ca3af;font-size:0.85rem'>请关闭本页，从打印工具的「📊 收支清算」菜单进入。</p>"
                "<p style='color:#9ca3af;font-size:0.8rem'>（本页的一次性入口用后即失效：刷新需重新从打印工具打开）</p>"
                "</div></body></html>").encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self._write_body(body)

    # ── 健康检查 ──

    def _handle_health(self):
        """本地服务存活 + 云端连通性检查（/api/ping 无需鉴权）。"""
        if not self.api_url:
            self._send_json({"success": True, "cloud": False, "message": "未配置云端地址"})
            return
        try:
            resp = _get_proxy_session().get(f"{self.api_url}/api/ping", timeout=_PROXY_TIMEOUT)
            if resp.status_code == 200:
                self._send_json({"success": True, "cloud": True})
            else:
                self._send_json({"success": True, "cloud": False, "message": f"云端响应 {resp.status_code}"})
        except Exception as e:
            self._send_json({"success": True, "cloud": False, "message": str(e)})

    # ── 代理 ──

    def _reject_proxy_path(self, path: str):
        """S2-10 ⑤：越界/未知的代理路径 —— 回 4xx，**不读请求体、不发上游请求**。"""
        _target, code = normalize_proxy_target(path)
        status = 400 if code == "bad_path" else 403
        logger.warning("拒绝越界代理路径 %r（%s）", (path or "")[:120], code or "bad_path")
        # 请求体还没读：这条连接上剩下的字节会被当成下一个请求解析 ⇒ 直接关掉
        self.close_connection = True
        self._send_json({"success": False, "code": "proxy_path_" + (code or "bad_path"),
                         "message": "代理路径越界或不在白名单内"}, status)

    def _proxy_get(self, path: str, query: str):
        """GET 代理到云端 API（S2-10 ⑤ 归一化+白名单；⑥ 出站 token 走请求头）"""
        # /api/proxy/admin/statistics/revenue → {api_url}/api/admin/statistics/revenue
        target_path, code = normalize_proxy_target(path)
        if code:
            return self._reject_proxy_path(path)
        safe_qs = _strip_token_from_query(query)  # 剥离客户端 token，防止覆盖后端令牌
        url = f"{self.api_url}{target_path}"
        if safe_qs:
            url += f"?{safe_qs}"

        try:
            # ⑥ 出站令牌走请求头：query 会进 nginx/网关的 access log（后端 app.py 的头优先）
            resp = _get_proxy_session().get(url, timeout=_PROXY_TIMEOUT,
                                            headers={"X-Printer-Token": self.token})
            out = _extract_proxy_body(resp)
            self._send_json(out, resp.status_code)
            # S1-05：记一份「当前云端配置摘要」当审计的 before（写配置时对照用，零额外请求）
            if target_path.endswith("/finance/config") and isinstance(out.get("data"), dict):
                type(self)._last_finance_summary = finance_data_summary(out["data"])
        except Exception as e:
            # 不拼接完整 URL / str(e)：其中可能包含凭据
            logger.error(f"代理请求失败 GET {target_path}: {type(e).__name__}")
            self._send_json({"success": False, "message": f"请求云端失败: {type(e).__name__}"}, 502)

    def _proxy_post(self, path: str, query: str):
        """POST 代理到云端 API（S1-05：财务写操作追加审计日志；S2-10 ⑤⑥ 同 GET）"""
        target_path, code = normalize_proxy_target(path)
        if code:
            return self._reject_proxy_path(path)
        safe_qs = _strip_token_from_query(query)  # 剥离客户端 token，防止覆盖后端令牌
        url = f"{self.api_url}{target_path}"
        if safe_qs:
            url += f"?{safe_qs}"

        try:
            body = self._read_body()
            payload = None
            try:
                payload = json.loads(body or b"{}")
            except Exception:
                payload = None
            # 服务端权威凭据不外发：云端配置里不需要 _finance_credentials（密码状态由 config.passwords 承载）
            if (target_path.endswith("/finance/config") and isinstance(payload, dict)
                    and isinstance(payload.get("data"), dict)):
                clean = {k: v for k, v in payload["data"].items() if k != CRED_KEY}
                payload = dict(payload, data=clean)
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            resp = _get_proxy_session().post(url, data=body, timeout=_PROXY_TIMEOUT,
                                             headers={"Content-Type": "application/json",
                                                      "X-Printer-Token": self.token})
            out = _extract_proxy_body(resp)
            self._send_json(out, resp.status_code)
            self._audit_proxy_write(target_path, payload, out, resp.status_code)
        except Exception as e:
            # 不拼接完整 URL / str(e)：其中可能包含凭据
            logger.error(f"代理请求失败 POST {target_path}: {type(e).__name__}")
            self._send_json({"success": False, "message": f"请求云端失败: {type(e).__name__}"}, 502)
            try:
                self._audit_proxy_write(target_path, None, {"success": False}, 502)
            except Exception:
                pass

    def _audit_proxy_write(self, target_path: str, payload, result, status: int) -> None:
        """把经本机代理的财务写操作记进只读审计日志（时间/模块/字段/前后值摘要）。

        只记**摘要**：订单订正记 order_id + 前后状态；配置写入记各区块条数与合计金额；
        设备相关只记 client_id/所有者/结论。**绝不记密码、token、密钥、哈希**（audit 再脱敏一层）。
        """
        sec = self._fin()
        ok = bool(result.get("success")) if isinstance(result, dict) else False
        body = payload if isinstance(payload, dict) else {}
        try:
            if target_path.endswith("/order_status") or target_path.endswith("/order_type"):
                is_status = target_path.endswith("/order_status")
                field = "status" if is_status else "order_type"
                before = result.get("previous_status" if is_status else "previous_order_type")
                sec.audit("order_fix", module="orderstatus",
                          fields={"order_id": body.get("order_id"), field: body.get(field),
                                  "before": before},
                          extra={"success": ok, "http": status, "path": target_path})
            elif target_path.endswith("/finance/config"):
                after = finance_data_summary(body.get("data") or {})
                before = getattr(type(self), "_last_finance_summary", None)
                sec.audit("finance_config_write", module="finance",
                          fields={"before": before or "unknown", "after": after},
                          extra={"success": ok, "http": status})
                if ok:
                    type(self)._last_finance_summary = after
            elif target_path.endswith("/pricing"):
                pricing = body.get("pricing")
                keys = sorted(pricing.keys()) if isinstance(pricing, dict) else []
                sec.audit("pricing_write", module="settings", fields={"keys": keys},
                          extra={"success": ok, "http": status})
            elif target_path.startswith("/api/printer/"):
                keep = ("client_id", "owner_name", "auto_duplex", "dry_run",
                        "include_offline_unbound", "min_offline_days")
                sec.audit("device_write", module="auth",
                          fields={k: body.get(k) for k in keep if k in body},
                          extra={"path": target_path, "success": ok, "http": status})
        except Exception as e:                            # noqa: BLE001
            logger.warning("代理写操作审计失败: %s", type(e).__name__)

    # ── S1-05 财务安全接口 ──

    def _handle_finance_status(self):
        """密码/会话/完整性状态（页面首载用它决定是否强制进改密流程）。"""
        sec = self._fin()
        state = sec.integrity_state
        if state == "unknown":
            state = sec.check_integrity()
        payload = sec.status_payload(self._finance_session_token())
        out = {"success": True}
        out.update(payload)
        out["integrity"] = state
        out["read_only"] = sec.read_only
        self._send_json(out)

    def _handle_finance_unlock(self):
        """密码校验（**唯一**的密码校验实现）→ 签发 30 分钟 finance_session。"""
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            return self._send_json({"success": False, "code": "bad_request",
                                    "message": "请求体不是合法 JSON"}, 400)
        if not isinstance(body, dict):
            return self._send_json({"success": False, "code": "bad_request",
                                    "message": "请求体必须是对象"}, 400)
        module = str(body.get("module") or "")
        password = body.get("password")
        if not isinstance(password, str):
            password = ""
        res = self._fin().unlock(module, password)
        if res.get("ok"):
            return self._send_json({"success": True, "session": res["session"],
                                    "expires_in": res.get("expires_in"),
                                    "modules": res.get("modules"),
                                    "upgraded": bool(res.get("upgraded")),
                                    "stored_hash": res.get("stored_hash") or ""})
        code = res.get("code") or "unlock_failed"
        status = 403 if code in ("needs_setup", "default_password", "wrong_password") else 400
        self._send_json({"success": False, "code": code,
                         "message": res.get("message") or "解锁失败"}, status)

    def _handle_finance_setup(self):
        """首次设置密码（默认密码已停用 → 必须走这里）。不发会话：设置完仍需解锁一次。"""
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            return self._send_json({"success": False, "code": "bad_request",
                                    "message": "请求体不是合法 JSON"}, 400)
        if not isinstance(body, dict):
            return self._send_json({"success": False, "code": "bad_request",
                                    "message": "请求体必须是对象"}, 400)
        module = str(body.get("module") or "all")
        new_password = body.get("new_password")
        res = self._fin().setup(module, new_password if isinstance(new_password, str) else "")
        if res.get("ok"):
            return self._send_json({"success": True, "modules": res.get("modules"),
                                    "stored": res.get("stored") or {}})
        code = res.get("code") or "setup_failed"
        status = 403 if code in ("already_configured", "read_only") else 400
        self._send_json({"success": False, "code": code,
                         "message": res.get("message") or "设置密码失败"}, status)

    def _handle_finance_password(self):
        """改密（需要已解锁「设置」模块）。"""
        ok, code, sess = self._fin().verify_session(self._finance_session_token(), "settings")
        if not ok:
            return self._send_json({"success": False, "code": code,
                                    "message": "修改密码需要先解锁「设置」模块"},
                                   401 if code == "finance_session_expired" else 403)
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            return self._send_json({"success": False, "code": "bad_request",
                                    "message": "请求体不是合法 JSON"}, 400)
        module = str((body or {}).get("module") or "") if isinstance(body, dict) else ""
        new_password = (body or {}).get("new_password") if isinstance(body, dict) else ""
        res = self._fin().change_password(module, new_password if isinstance(new_password, str) else "",
                                         sess.get("mods"))
        if res.get("ok"):
            return self._send_json({"success": True, "module": module,
                                    "stored_hash": res.get("stored_hash") or ""})
        code = res.get("code") or "password_change_failed"
        status = 403 if code == "finance_session_forbidden" else 400
        self._send_json({"success": False, "code": code,
                         "message": res.get("message") or "改密失败"}, status)

    def _handle_finance_logout(self):
        """登出（页面锁定模块时调用）。"""
        revoked = self._fin().revoke_session(self._finance_session_token())
        self._send_json({"success": True, "revoked": bool(revoked)})

    def _handle_finance_integrity_ack(self):
        """完整性校验失败后的人工确认：按当前文件内容重建 HMAC 基线（数据一个字不动）。"""
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            body = {}
        confirm = bool((body or {}).get("confirm")) if isinstance(body, dict) else False
        res = self._fin().acknowledge_integrity(confirm)
        if res.get("ok"):
            return self._send_json({"success": True, "integrity": res.get("integrity")})
        self._send_json({"success": False, "code": res.get("code"),
                         "message": res.get("message") or "确认失败"}, 400)

    # ── 本地绑定 ──

    def _handle_get_bindings(self):
        data = load_bindings()
        self._send_json({"success": True, "bindings": data})

    def _handle_get_device(self):
        """返回本机设备信息（client_id / 计算机名 / 是否接单），供收支清算「授权」页使用。
        take_orders 实时向后端确认（本机是否为当前接单设备），与设备列表的 is_active 同源，
        避免「授权页显示未接单、设备列表显示接单中」的不一致（2026-12 修复：
        此前为打开页面时的本地快照，云端设置里启用接单后不会同步）。"""
        client_id = getattr(self, "client_id", "") or ""
        device_name = getattr(self, "device_name", "") or ""
        take_orders = bool(getattr(self, "take_orders", False))
        # 实时接单状态：云端模式且配置了 api_url 时，向后端 /api/printer_status 确认本机是否接管
        if client_id and getattr(self, "api_url", ""):
            try:
                resp = _get_proxy_session().get(
                    f"{self.api_url}/api/printer_status",
                    # S2-10 ⑥：出站令牌走请求头（该接口本身不校验 token，更没理由放 query）
                    headers={"X-Printer-Token": getattr(self, "token", "")},
                    timeout=_PROXY_TIMEOUT,
                )
                if resp.ok:
                    data = resp.json()
                    active_cid = (data.get("active_client_id") or "") if isinstance(data, dict) else ""
                    take_orders = bool(active_cid) and active_cid == client_id
            except Exception:
                pass  # 离线/异常 → 回退本地快照
        self._send_json({"success": True, "device": {
            "client_id": client_id,
            "device_name": device_name,
            "take_orders": take_orders,
        }})

    def _handle_post_bindings(self):
        try:
            body = json.loads(self._read_body())
            save_bindings(body)
            self._send_json({"success": True, "message": "绑定已保存"})
        except Exception as e:
            self._send_json({"success": False, "message": str(e)}, 400)

    # ── 本地数据读写（无浏览器安全弹窗） ──

    def _handle_get_data(self):
        """读取本地数据文件，返回 JSON 内容；文件不存在时返回 exists=false。
        S1-05：额外返回 `integrity` / `read_only` —— 完整性校验失败时页面要挂只读横幅并要求确认，
        但**数据照常返回**（读操作不需要 finance_session，向后兼容）。"""
        data_file = getattr(self, "_data_file", _DEFAULT_DATA_FILE)
        sec = self._fin()
        with _DATA_FILE_LOCK:
            if not os.path.exists(data_file):
                self._send_json({"success": True, "data": None, "exists": False,
                                 "integrity": "absent", "read_only": False})
                return
            try:
                with open(data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.error(f"读取数据文件失败: {type(e).__name__}")
                self._send_json({"success": False, "message": f"读取失败: {type(e).__name__}"}, 500)
                return
        if sec.integrity_state == "unknown":
            sec.check_integrity()
        # 服务端权威凭据（PBKDF2 哈希）不下发给页面/云端：页面用不到，少一份外泄面
        if isinstance(data, dict) and CRED_KEY in data:
            data = {k: v for k, v in data.items() if k != CRED_KEY}
        payload = {"success": True, "data": data, "exists": True,
                   "integrity": sec.integrity_state, "read_only": sec.read_only}
        if sec.read_only:
            # 只读时把"当前文件真实指纹"给页面：确认框里展示给操作员对照
            payload["current_mac"] = sec.current_mac()[:16]
        self._send_json(payload)

    def _handle_delete_data(self):
        """删除本地数据文件（CEO 迁移到云端 / 关闭云端清空缓存时由页面调用）。
        S1-05：连完整性基线一起清（否则下一次写入会被旧基线判成篡改）；
        本地凭据一并清空 —— 云端配置里仍有哈希，下次解锁会从云端重新导入并升级。"""
        data_file = getattr(self, "_data_file", _DEFAULT_DATA_FILE)
        sec = self._fin()
        with _DATA_FILE_LOCK:
            try:
                tmp_file = data_file + ".tmp"
                for p in (data_file, tmp_file, data_file + ".hmac", data_file + ".sealed"):
                    if os.path.exists(p):
                        os.remove(p)
            except Exception as e:
                logger.error(f"删除本地数据文件失败: {type(e).__name__}")
                self._send_json({"success": False, "message": f"删除失败: {type(e).__name__}"}, 500)
                return
        sec.audit("local_data_delete", module="data", fields={"files": [os.path.basename(data_file)]})
        self._send_json({"success": True, "message": "本地数据已清空"})

    def _handle_local_orders(self, query: str = ""):
        """读取「本地订单库」SQLite（offline_orders 表），返回与云端 revenue 同构的订单结构。
        供本地模式的「收支清算 → 云端」驾驶舱复用云端渲染（仅数据源为本地）。
        金额一律用「整数分」（与云端口径一致）；本地订单视为已打印完成（status='sent'）。
        支持 start_date/end_date/owner 过滤。"""
        import sqlite3 as _sqlite
        orders = []
        try:
            qs = urllib.parse.parse_qs(query)
            owner = (qs.get("owner", [""])[0] or "").strip()
            start_date = (qs.get("start_date", [""])[0] or "").strip()
            end_date = (qs.get("end_date", [""])[0] or "").strip()
            with _DATA_FILE_LOCK:
                conn = _sqlite.connect(_LOCAL_DB_FILE)
                # S1-09：status 列由 offline_sync 迁移补上（旧库可能还没有）→ 先探测再用
                try:
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(offline_orders)").fetchall()}
                except _sqlite.Error:
                    cols = set()
                has_status = "status" in cols
                sql = ("SELECT id, order_number, files_json, total_price, created_at, "
                       "owner_name, is_admin_print, synced%s FROM offline_orders"
                       % (", COALESCE(status, 'pending') AS status" if has_status else ""))
                conds, args = [], []
                if owner:
                    conds.append("owner_name = ?")
                    args.append(owner)
                if start_date:
                    conds.append("created_at >= ?")
                    args.append(start_date + " 00:00:00")
                if end_date:
                    conds.append("created_at <= ?")
                    args.append(end_date + " 23:59:59")
                if conds:
                    sql += " WHERE " + " AND ".join(conds)
                sql += " ORDER BY created_at DESC, id DESC"
                rows = conn.execute(sql, args).fetchall()
                conn.close()
            for row in rows:
                (dbid, order_number, files_json, total_price, created_at,
                 owner_name, is_admin_print, synced) = row[:8]
                sync_status = (row[8] if len(row) > 8 else "synced") or "pending"
                # S1-09：**只有 truly 同步到云端的单才算「已完成」**。
                # 离线期间打的单还躺在本地库（synced=0）：显示成 sent 会让结算页把它算进
                # 「已完成 / 已入账」，钱还没进云端账就虚增收益。云端业务拒收（rejected）同理。
                if synced and sync_status == "synced":
                    row_status = "sent"
                elif sync_status == "rejected":
                    row_status = "unsynced_rejected"
                else:
                    row_status = "unsynced"
                files = []
                total_page_count = 0
                total_sheet_count = 0
                try:
                    fdata = json.loads(files_json) if files_json else []
                    for f in fdata or []:
                        if not isinstance(f, dict):
                            continue
                        seq = f.get("seq") or f.get("id") or ("f%d" % len(files))
                        copies = int(f.get("copies", 1) or 1)
                        page_count = int(f.get("page_count", 0) or 0)
                        cost = float(f.get("cost", 0) or 0)          # 元
                        is_free = bool(f.get("is_free", False))
                        duplex = (f.get("duplex") or "on")
                        page_range = f.get("page_range", "") or ""
                        # 有效口径（v7.2）：与云端 revenue 同构 —— 页数/张数按页码范围折算后下发，
                        # 结算页只展示不折算（云端 q 与本地 q 必须是同一套字段与同一套算法）。
                        eff = _count_range_pages(page_range, page_count)
                        per_sheets = (eff + 1) // 2 if duplex == "on" else eff
                        files.append({
                            "id": seq,
                            "file_name": f.get("file_name", ""),
                            "copies": copies,
                            "page_count": page_count,
                            "duplex": duplex,
                            "page_range": page_range,
                            "total_price": round(cost, 2),           # 元（与云端 revenue 的 total_price 单位一致）
                            "status": row_status,                    # 与订单级同口径（未同步 ≠ 已完成）
                            "is_free": is_free,
                            "effective_pages": eff,
                            "sheets": per_sheets,
                            "total_pages": eff * copies,
                            "total_sheets": per_sheets * copies,
                        })
                        total_page_count += eff * copies
                        total_sheet_count += per_sheets * copies
                except Exception:
                    files = []
                orders.append({
                    "order_id": dbid,
                    "order_number": order_number,
                    "created_at": created_at or "",
                    "total_page_count": total_page_count,
                    "total_sheet_count": total_sheet_count,
                    "owner_name": owner_name or "",
                    "is_admin_print": bool(is_admin_print),
                    "source": "local",
                    "openid": "local",
                    "nickname": owner_name or "",
                    # 本地订单无附加服务/计划字段 → 全置默认
                    "auto_print": False,
                    "schedule_mode": None,
                    "delivery_enabled": False,
                    "urgency": "低",
                    "cover_page": False,
                    "status": row_status,
                    # S1-09：未同步/被拒的可见标记（结算页据此显示 ⏳/⚠️ 且**不计收益**）
                    "unsynced": row_status != "sent",
                    "sync_status": sync_status,
                    "files": files,
                })
        except Exception as e:
            logger.error(f"读取本地订单库失败: {type(e).__name__}")
            self._send_json({"success": False, "message": f"读取失败: {type(e).__name__}"}, 500)
            return
        # 顶层结构与云端 revenue 一致（前端 renderCloud 只消费 orders）
        self._send_json({"success": True, "data": {"orders": orders}})

    def _handle_post_data(self):
        """写入本地数据文件：路径固定为默认数据文件（不支持客户端指定路径），
        原子写入 + 重签 HMAC（S1-05 ④）。完整性校验失败时**拒绝写入、不覆盖任何数据**（423）。"""
        with _DATA_FILE_LOCK:
            try:
                body = json.loads(self._read_body())
                data = body.get("data", body) if isinstance(body, dict) else body
                if not isinstance(data, dict):
                    raise ValueError("data 必须是对象")
                data_file = getattr(self, "_data_file", _DEFAULT_DATA_FILE)
                os.makedirs(os.path.dirname(data_file), exist_ok=True)
                self._fin().write_data(data)
            except PermissionError as e:
                # 只读保护：绝不覆盖（这是 S1-05 的核心承诺）
                logger.error("拒绝覆盖财务数据：%s", e)
                self._send_json({"success": False, "code": "integrity_readonly",
                                 "read_only": True, "message": str(e)}, 423)
                return
            except Exception as e:
                # 错误消息不含完整路径（只含文件名），避免泄露本地目录结构
                logger.error(f"保存数据失败: {type(e).__name__}")
                fname = os.path.basename(getattr(self, "_data_file", _DEFAULT_DATA_FILE))
                self._send_json({"success": False, "message": f"保存失败: {type(e).__name__}（{fname}）"}, 500)
                return
        self._send_json({"success": True, "message": "数据已保存"})


class _LimitedThreadingHTTPServer(ThreadingHTTPServer):
    """带**并发上限**的 ThreadingHTTPServer（S1-09）。

    ThreadingHTTPServer 每个请求开一条线程、没有上限：本地服务被异常/恶意并发打满时
    会把整机拖死。这里用一个信号量把处理线程钳在 `max_workers`：超了**直接回 503**
    （明确、可重试），不排队、不无限起线程；请求结束（含异常）必定归还槽位。

    S2-10：另外挂两个**实例级**属性（不能塞进 handler 类属性 —— handler 是类级共享的，
    端口/nonce 都是 per-server 的）：
      · `allowed_hosts`：绑定后的实际端口推出的 Host 白名单（防 DNS rebinding）；
      · `nonce_owner`：签发一次性 nonce 的 StatsServer（HTML 入口消费/作废它）。
    """

    max_workers = _MAX_CONCURRENT_REQUESTS

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(self.max_workers)
        self.rejected = 0            # 因并发上限被拒的请求数（诊断/验收用）
        self.nonce_owner = None      # StatsServer 在 start() 里注入
        self.allowed_hosts = frozenset()
        super().__init__(*args, **kwargs)
        # 端口只有绑定之后才知道（port=0 由系统分配）
        self.allowed_hosts = allowed_hosts(getattr(self, "server_port", 0))

    def process_request(self, request, client_address):
        try:
            request.settimeout(_SOCKET_TIMEOUT)
        except OSError:
            pass
        if not self._slots.acquire(blocking=False):
            self.rejected += 1
            logger.warning("本地统计服务并发已达上限（%d），拒绝一个请求", self.max_workers)
            self._reject_busy(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()        # 起线程失败也必须还槽位，否则上限会永久漏掉一个
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    @staticmethod
    def _reject_busy(request):
        """直接在工作线程之外回 503（不读请求体），随后关闭连接。"""
        body = json.dumps({"success": False, "code": "server_busy",
                           "message": "本地服务繁忙（并发上限 %d），请稍后重试"
                                      % _MAX_CONCURRENT_REQUESTS},
                          ensure_ascii=False).encode("utf-8")
        head = (b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                b"Connection: close\r\n"
                b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n")
        try:
            request.settimeout(2)
            request.sendall(head + body)
        except OSError:
            pass
        finally:
            try:
                request.close()
            except OSError:
                pass

    def handle_error(self, request, client_address):
        """超时/断连是正常现象（浏览器 keep-alive、用户关页），别刷 traceback。"""
        exc = sys.exc_info()[1]
        if isinstance(exc, (socket.timeout, TimeoutError, ConnectionResetError,
                            ConnectionAbortedError, BrokenPipeError)):
            logger.debug("本地统计服务连接中断（%s）", type(exc).__name__)
            return
        return super().handle_error(request, client_address)


class StatsServer:
    """统计 HTTP 服务器（后台线程）"""

    def __init__(self, api_url: str = "", token: str = "", port: int = 0, data_file: str = "",
                 finance_mode: str = "cloud", device_name: str = "", client_id: str = "",
                 take_orders: bool = False):
        self.api_url = api_url.rstrip("/") if api_url else ""
        self.token = token
        self.data_file = data_file or _DEFAULT_DATA_FILE
        self.finance_mode = finance_mode if finance_mode in ("cloud", "local") else "cloud"
        # 本机设备信息（收支清算「授权」页展示）
        self.device_name = (device_name or "").strip()[:64]
        self.client_id = (client_id or "").strip()
        self.take_orders = bool(take_orders)
        self._port = port
        self._launch_token = secrets.token_hex(16)  # 每次启动随机，仅本地打印工具持有
        # S2-10 ①：一次性 nonce（nonce → 过期时间戳）。只放内存、随进程结束消失。
        self._nonces = {}
        self._nonce_lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        # 收支清算页的云端代理请求同样绕开系统/环境代理（见 net_direct.py）
        if self.api_url:
            import net_direct
            net_direct.ensure_direct(self.api_url)

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    @property
    def launch_token(self) -> str:
        return self._launch_token

    @property
    def finance(self) -> FinanceSecurity:
        """财务安全门（S1-05）：启动时注入 handler，测试/调试也可从这里取。"""
        return get_finance_security(self.data_file, api_url=self.api_url, token=self.token,
                                    finance_mode=self.finance_mode)

    # ── S2-10 ① 一次性 nonce（GUI 打开页面时签发，HTML 入口消费） ──

    def issue_nonce(self, ttl: float = NONCE_TTL) -> str:
        """签发一个**一次性** nonce（放进打开页面的 URL：`?nonce=…`）。

        与启动令牌的区别：nonce 只有一个用途（换取一次 HTML 入口），
        用过即废、还带 TTL —— 所以它可以进地址栏，而长期有效的 launch token 不行。
        """
        nonce = secrets.token_urlsafe(32)
        try:
            expire = time.time() + max(0.0, float(ttl))
        except (TypeError, ValueError):
            expire = time.time() + NONCE_TTL
        with self._nonce_lock:
            now = time.time()
            for k in [k for k, exp in self._nonces.items() if exp <= now]:
                self._nonces.pop(k, None)              # 过期即清（不无限攒）
            while len(self._nonces) >= _NONCE_MAX_PENDING:
                oldest = next(iter(self._nonces), None)
                if oldest is None:
                    break
                self._nonces.pop(oldest, None)         # FIFO 丢最旧的（正常永远到不了这里）
            self._nonces[nonce] = expire
        return nonce

    def consume_nonce(self, nonce) -> bool:
        """校验并**当场作废**（一次性）。伪造 / 过期 / 重放一律 False。"""
        if not isinstance(nonce, str) or not nonce or len(nonce) > 128:
            return False
        now = time.time()
        with self._nonce_lock:
            for k in [k for k, exp in self._nonces.items() if exp <= now]:
                self._nonces.pop(k, None)
            matched = ""
            # 恒时比较（S2-10 ④）：未消费的 nonce 极少（≤32），线性扫一遍代价可忽略
            for pending in list(self._nonces):
                if _token_equal(nonce, pending):
                    matched = pending
            if not matched:
                return False
            self._nonces.pop(matched, None)            # 一次性：先作废，再回报成功
        return True

    def start(self):
        """启动服务器（阻塞式，应在后台线程调用）"""
        # 注入配置到 handler
        _StatsHandler.api_url = self.api_url
        _StatsHandler.token = self.token
        _StatsHandler._data_file = self.data_file
        _StatsHandler.launch_token = self._launch_token
        _StatsHandler.finance_mode = self.finance_mode
        _StatsHandler.device_name = self.device_name
        _StatsHandler.client_id = self.client_id
        _StatsHandler.take_orders = self.take_orders

        # S1-05 ④：启动时校验数据完整性（HMAC）。校验失败 → 只读保护（不覆盖数据），
        # 由页面横幅要求操作员确认后重建基线。
        try:
            state = self.finance.check_integrity()
            if self.finance.read_only:
                logger.error("财务数据完整性校验未通过（%s）：print_data.json 已进入只读保护，"
                             "需在收支清算页面确认后才会恢复写入", state)
            else:
                logger.info("财务数据完整性校验：%s", state)
        except Exception as e:                            # noqa: BLE001
            logger.error("财务数据完整性校验异常: %s", type(e).__name__)

        # 多线程 HTTP 服务器：避免单请求（如慢代理）阻塞其他请求；
        # S1-09：并发线程数封顶（超额 503）+ 连接级超时（见 _LimitedThreadingHTTPServer）
        self._server = _LimitedThreadingHTTPServer(("127.0.0.1", self._port), _StatsHandler)
        self._port = self._server.server_port  # 实际端口（port=0 时系统分配）
        # S2-10 ①②：Host 白名单要用**实际**端口；nonce 的一次性判定挂在 server 实例上
        # （handler 类是全局共享的，端口/nonce 都是 per-server 的东西，不能塞进类属性）
        self._server.allowed_hosts = allowed_hosts(self._port)
        self._server.nonce_owner = self
        logger.info(f"统计服务器启动: http://127.0.0.1:{self._port}")
        self._running = True

        try:
            self._server.serve_forever()
        except Exception as e:
            logger.error(f"统计服务器异常: {e}")
        finally:
            self._running = False

    def start_in_thread(self):
        """后台线程启动"""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.start, daemon=True)
        self._thread.start()
        # 轮询等待服务器就绪（最多 3 秒，每 50ms 检查端口可连接），替代固定 sleep
        import time
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if self._server is not None:
                try:
                    with socket.create_connection(("127.0.0.1", self._port), timeout=0.5):
                        return
                except OSError:
                    pass
            time.sleep(0.05)

    def stop(self):
        """停止服务器"""
        if self._server:
            self._server.shutdown()
            self._server = None
        with self._nonce_lock:
            self._nonces.clear()                           # 停服即作废所有未消费 nonce
        self._running = False
        logger.info("统计服务器已停止")

    def update_config(self, api_url: str = "", token: str = ""):
        """云端配置变更后同步（不重启服务器）。

        收支清算页可能在配置云端前就已启动 stats_server（当时 api_url 还是占位，
        如 https://your-server.com），配置云端后 if 不复用旧值会把代理请求打到不存在的
        占位域名 → SSLError（表现为首次配云端后打不开收支统计，重启后消失）。
        此方法更新自身并重新注入 handler 类属性，下一次代理请求即用新配置。
        """
        self.api_url = api_url.rstrip("/") if api_url else ""
        self.token = token
        if self._server:   # 已在运行 → 同步 handler 类属性（每次请求读取）
            _StatsHandler.api_url = self.api_url
            _StatsHandler.token = self.token
        # S1-05：云端凭据来源也要跟着换（云端模式下密码哈希从云端 config 读）
        try:
            self.finance.api_url = self.api_url
            self.finance.token = self.token
        except Exception:                                 # noqa: BLE001
            pass
        # 注意：这里只更新配置，服务器保持运行（切勿误以为已停止）
        logger.info("统计服务器配置已更新（服务器保持运行）")
