"""
stats_server.py — 内置 HTTP 服务器，为收支清算 HTML 提供：
  1. 静态文件服务（从 local_print_tool/finance/ 目录）
  2. 云端 API 代理（附带 printer token）
  3. 本地 openid→成员 绑定配置读写
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import sys
import threading
import urllib.parse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

import requests as http_requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

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
# 公共静态文件白名单：浏览器加载这些文件时不要求启动令牌（其余静态文件一律校验）
_PUBLIC_STATIC_FILES = {"/chart.umd.min.js"}


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
    导致授权页设备列表空白、绑定后所有者不刷新）。"""
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
    return out


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

    def __init__(self, *args, **kwargs):
        # 设置静态文件根目录
        super().__init__(*args, directory=_STATIC_DIR, **kwargs)

    def log_message(self, format, *args):
        logger.debug(f"StatsServer: {format % args}")

    def _check_launch_token(self, allow_query: bool = False) -> bool:
        """校验启动令牌。API 用 header X-Launch-Token；HTML 页面加载用 ?token= 查询参数。"""
        header = self.headers.get("X-Launch-Token") or ""
        if header and header == self.launch_token:
            return True
        if allow_query:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            qtoken = qs.get("token", [""])[0]
            if qtoken and qtoken == self.launch_token:
                return True
        return False

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

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
        """CORS 预检"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── 路由分发 ──

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 启动令牌门：所有 /api/* 请求均须携带有效令牌
        # （页面 API 请求经 X-Launch-Token 请求头；HTML 页面首载经 ?token= 查询参数）
        if path.startswith("/api/"):
            if not self._check_launch_token():
                return self._send_json({"success": False, "message": "未授权：请通过本地打印工具打开收支清算"}, 403)

        # 健康检查：本地服务存活 + 云端连通性
        if path == "/api/health":
            return self._handle_health()

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

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 启动令牌门（POST 一律要求有效令牌）
        if not self._check_launch_token():
            return self._send_json({"success": False, "message": "未授权：请通过本地打印工具打开收支清算"}, 403)

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

        if not self._check_launch_token():
            return self._send_json({"success": False, "message": "未授权：请通过本地打印工具打开收支清算"}, 403)

        # 本地数据删除（CEO 迁移到云端 / 关闭云端清空缓存时由页面调用）
        if path == "/api/local/data":
            return self._handle_delete_data()

        self._send_json({"success": False, "message": "未找到"}, 404)

    # ── 静态文件 ──

    def _serve_static(self, path: str):
        """提供静态文件，根路径默认返回 settlement.html。
        HTML 入口要求启动令牌（?token=），并将 __LAUNCH_TOKEN__ 替换为本次启动的真实令牌。
        __FINANCE_MODE__ 替换为当前存储模式（cloud/local）。
        公共库文件（chart.umd.min.js）豁免令牌；其余静态文件（含数据文件 print_data.json）一律校验令牌。"""
        serve_html = (path in ("", "/", "/settlement.html"))
        if serve_html:
            if not self._check_launch_token(allow_query=True):
                return self._serve_403_page()
            return self._serve_injected_html("settlement.html")
        if path in _PUBLIC_STATIC_FILES:
            self.path = path
            return super().do_GET()
        # 非公共静态文件：一律要求启动令牌，防止绕过令牌门直接读取数据文件
        if not self._check_launch_token(allow_query=True):
            return self._serve_403_page()
        self.path = path
        return super().do_GET()

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
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _serve_403_page(self):
        """未带启动令牌时返回 403 提示页。"""
        body = ("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='UTF-8'>"
                "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
                "<title>访问受限</title></head>"
                "<body style='font-family:Segoe UI,Microsoft YaHei,sans-serif;background:#f5f6fa;"
                "color:#1a1a2e;display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
                "<div style='text-align:center;padding:24px'><div style='font-size:56px'>🔒</div>"
                "<h2 style='margin:12px 0 6px'>访问受限</h2>"
                "<p style='color:#6b7280'>收支清算页面仅能从本地打印工具中打开。</p>"
                "<p style='color:#9ca3af;font-size:0.85rem'>请关闭本页，从打印工具的「📊 收支清算」菜单进入。</p>"
                "</div></body></html>").encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

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

    def _proxy_get(self, path: str, query: str):
        """GET 代理到云端 API"""
        # /api/proxy/admin/statistics/revenue → {api_url}/api/admin/statistics/revenue
        target_path = path.replace("/api/proxy", "/api", 1)
        safe_qs = _strip_token_from_query(query)  # 剥离客户端 token，防止覆盖后端令牌
        url = f"{self.api_url}{target_path}"
        if safe_qs:
            url += f"?{safe_qs}&token={self.token}"
        else:
            url += f"?token={self.token}"

        try:
            resp = _get_proxy_session().get(url, timeout=_PROXY_TIMEOUT)
            self._send_json(_extract_proxy_body(resp), resp.status_code)
        except Exception as e:
            # 不拼接完整 URL / str(e)：其中可能包含 token 查询参数
            logger.error(f"代理请求失败 GET {target_path}: {type(e).__name__}")
            self._send_json({"success": False, "message": f"请求云端失败: {type(e).__name__}"}, 502)

    def _proxy_post(self, path: str, query: str):
        """POST 代理到云端 API"""
        target_path = path.replace("/api/proxy", "/api", 1)
        safe_qs = _strip_token_from_query(query)  # 剥离客户端 token，防止覆盖后端令牌
        url = f"{self.api_url}{target_path}"
        if safe_qs:
            url += f"?{safe_qs}&token={self.token}"
        else:
            url += f"?token={self.token}"

        try:
            body = self._read_body()
            resp = _get_proxy_session().post(url, data=body, timeout=_PROXY_TIMEOUT,
                                             headers={"Content-Type": "application/json"})
            self._send_json(_extract_proxy_body(resp), resp.status_code)
        except Exception as e:
            # 不拼接完整 URL / str(e)：其中可能包含 token 查询参数
            logger.error(f"代理请求失败 POST {target_path}: {type(e).__name__}")
            self._send_json({"success": False, "message": f"请求云端失败: {type(e).__name__}"}, 502)

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
                    params={"token": getattr(self, "token", "")},
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
        """读取本地数据文件，返回 JSON 内容；文件不存在时返回 exists=false"""
        data_file = getattr(self, "_data_file", _DEFAULT_DATA_FILE)
        with _DATA_FILE_LOCK:
            if not os.path.exists(data_file):
                self._send_json({"success": True, "data": None, "exists": False})
                return
            try:
                with open(data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.error(f"读取数据文件失败: {type(e).__name__}")
                self._send_json({"success": False, "message": f"读取失败: {type(e).__name__}"}, 500)
                return
        self._send_json({"success": True, "data": data, "exists": True})

    def _handle_delete_data(self):
        """删除本地数据文件（CEO 迁移到云端 / 关闭云端清空缓存时由页面调用）。"""
        with _DATA_FILE_LOCK:
            try:
                tmp_file = _DEFAULT_DATA_FILE + ".tmp"
                for p in (_DEFAULT_DATA_FILE, tmp_file):
                    if os.path.exists(p):
                        os.remove(p)
            except Exception as e:
                logger.error(f"删除本地数据文件失败: {type(e).__name__}")
                self._send_json({"success": False, "message": f"删除失败: {type(e).__name__}"}, 500)
                return
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
                sql = ("SELECT id, order_number, files_json, total_price, created_at, "
                       "owner_name, is_admin_print, synced FROM offline_orders")
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
            for (dbid, order_number, files_json, total_price, created_at,
                    owner_name, is_admin_print, synced) in rows:
                files = []
                total_page_count = 0
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
                        files.append({
                            "id": seq,
                            "file_name": f.get("file_name", ""),
                            "copies": copies,
                            "page_count": page_count,
                            "total_price": round(cost, 2),           # 元（与云端 revenue 的 total_price 单位一致）
                            "status": "sent",                        # 本地订单已打印完成
                            "is_free": is_free,
                        })
                        total_page_count += page_count * copies
                except Exception:
                    files = []
                orders.append({
                    "order_id": dbid,
                    "order_number": order_number,
                    "created_at": created_at or "",
                    "total_page_count": total_page_count,
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
                    "status": "sent",
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
        加线程锁并以「临时文件 + os.replace」原子写入，避免并发写与写半损坏。"""
        with _DATA_FILE_LOCK:
            try:
                body = json.loads(self._read_body())
                data = body.get("data", body) if isinstance(body, dict) else body
                data_file = getattr(self, "_data_file", _DEFAULT_DATA_FILE)
                os.makedirs(os.path.dirname(data_file), exist_ok=True)
                tmp_file = data_file + ".tmp"
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_file, data_file)
            except Exception as e:
                # 错误消息不含完整路径（只含文件名），避免泄露本地目录结构
                logger.error(f"保存数据失败: {type(e).__name__}")
                fname = os.path.basename(getattr(self, "_data_file", _DEFAULT_DATA_FILE))
                self._send_json({"success": False, "message": f"保存失败: {type(e).__name__}（{fname}）"}, 500)
                return
        self._send_json({"success": True, "message": "数据已保存"})


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
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    @property
    def launch_token(self) -> str:
        return self._launch_token

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

        # 多线程 HTTP 服务器：避免单请求（如慢代理）阻塞其他请求
        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), _StatsHandler)
        self._port = self._server.server_port  # 实际端口（port=0 时系统分配）
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
        # 注意：这里只更新配置，服务器保持运行（切勿误以为已停止）
        logger.info("统计服务器配置已更新（服务器保持运行）")
