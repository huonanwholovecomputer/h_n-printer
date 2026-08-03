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
from http.server import HTTPServer, SimpleHTTPRequestHandler

import requests as http_requests

logger = logging.getLogger(__name__)

# 静态文件根目录（支持 PyInstaller 打包和开发环境）
if getattr(sys, "frozen", False):
    _STATIC_DIR = os.path.join(sys._MEIPASS, "finance")
else:
    _STATIC_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "finance"))
# 绑定配置文件
_BINDINGS_FILE = os.path.join(os.path.dirname(__file__), "user_bindings.json")
# 默认数据文件（用户收支数据）
_DEFAULT_DATA_FILE = os.path.join(os.path.dirname(__file__), "finance", "print_data.json")


def load_bindings() -> dict:
    """读取 openid → 成员 绑定配置"""
    if os.path.exists(_BINDINGS_FILE):
        try:
            with open(_BINDINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_bindings(data: dict) -> None:
    """保存 openid → 成员 绑定配置"""
    os.makedirs(os.path.dirname(_BINDINGS_FILE), exist_ok=True)
    with open(_BINDINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class _StatsHandler(SimpleHTTPRequestHandler):
    """自定义请求处理器"""

    # 由 StatsServer 在构造时注入
    api_url: str = ""
    token: str = ""
    launch_token: str = ""  # 启动令牌：仅本地打印工具启动时附带，每次启动随机

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
        length = int(self.headers.get("Content-Length", 0))
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

        # 启动令牌门：所有 /api/* 请求均须携带有效令牌（页面经 X-Launch-Token）
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

        # 本地数据加载
        if path == "/api/local/data":
            return self._handle_get_data()

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

    # ── 静态文件 ──

    def _serve_static(self, path: str):
        """提供静态文件，根路径默认返回 settlement.html。
        HTML 入口要求启动令牌（?token=），并将 __LAUNCH_TOKEN__ 替换为本次启动的真实令牌。"""
        serve_html = (path in ("", "/", "/settlement.html"))
        if serve_html:
            if not self._check_launch_token(allow_query=True):
                return self._serve_403_page()
            return self._serve_injected_html()
        self.path = path
        return super().do_GET()

    def _serve_injected_html(self):
        """读取 settlement.html 并注入本次启动令牌。"""
        html_path = os.path.join(_STATIC_DIR, "settlement.html")
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.error(f"读取页面失败: {e}")
            return self._send_json({"success": False, "message": f"读取页面失败: {e}"}, 500)
        content = content.replace("__LAUNCH_TOKEN__", self.launch_token)
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
            resp = http_requests.get(f"{self.api_url}/api/ping", timeout=8)
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
        url = f"{self.api_url}{target_path}"
        if query:
            url += f"?{query}&token={self.token}"
        else:
            url += f"?token={self.token}"

        try:
            resp = http_requests.get(url, timeout=30)
            self._send_json(resp.json(), resp.status_code)
        except Exception as e:
            logger.error(f"代理请求失败 GET {url}: {e}")
            self._send_json({"success": False, "message": f"请求云端失败: {e}"}, 502)

    def _proxy_post(self, path: str, query: str):
        """POST 代理到云端 API"""
        target_path = path.replace("/api/proxy", "/api", 1)
        url = f"{self.api_url}{target_path}"
        if query:
            url += f"?{query}&token={self.token}"
        else:
            url += f"?token={self.token}"

        body = self._read_body()
        try:
            resp = http_requests.post(url, data=body, timeout=30,
                                      headers={"Content-Type": "application/json"})
            self._send_json(resp.json(), resp.status_code)
        except Exception as e:
            logger.error(f"代理请求失败 POST {url}: {e}")
            self._send_json({"success": False, "message": f"请求云端失败: {e}"}, 502)

    # ── 本地绑定 ──

    def _handle_get_bindings(self):
        data = load_bindings()
        self._send_json({"success": True, "bindings": data})

    def _handle_post_bindings(self):
        try:
            body = json.loads(self._read_body())
            save_bindings(body)
            self._send_json({"success": True, "message": "绑定已保存"})
        except Exception as e:
            self._send_json({"success": False, "message": str(e)}, 400)

    # ── 本地数据读写（无浏览器安全弹窗） ──

    def _handle_get_data(self):
        """读取本地数据文件，返回 JSON 内容；文件不存在时返回路径供 UI 展示"""
        data_file = getattr(self, "_data_file", _DEFAULT_DATA_FILE)
        if os.path.exists(data_file):
            try:
                with open(data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._send_json({"success": True, "data": data, "path": data_file, "exists": True})
            except Exception as e:
                self._send_json({"success": False, "message": f"读取失败: {e}"}, 500)
        else:
            self._send_json({"success": True, "data": None, "path": data_file, "exists": False})

    def _handle_post_data(self):
        """写入本地数据文件"""
        try:
            body = json.loads(self._read_body())
            filepath = body.get("_filepath", "") if isinstance(body, dict) else ""
            data = body.get("data", body) if isinstance(body, dict) else body
            # 去除内部字段
            if isinstance(data, dict):
                data.pop("_filepath", None)

            data_file = filepath or getattr(self, "_data_file", _DEFAULT_DATA_FILE)
            os.makedirs(os.path.dirname(data_file), exist_ok=True)
            with open(data_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._send_json({"success": True, "message": "数据已保存", "path": data_file})
        except Exception as e:
            logger.error(f"保存数据失败: {e}")
            self._send_json({"success": False, "message": f"保存失败: {e}"}, 500)


class StatsServer:
    """统计 HTTP 服务器（后台线程）"""

    def __init__(self, api_url: str = "", token: str = "", port: int = 0, data_file: str = ""):
        self.api_url = api_url.rstrip("/") if api_url else ""
        self.token = token
        self.data_file = data_file or _DEFAULT_DATA_FILE
        self._port = port
        self._launch_token = secrets.token_hex(16)  # 每次启动随机，仅本地打印工具持有
        self._server: HTTPServer | None = None
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

        self._server = HTTPServer(("127.0.0.1", self._port), _StatsHandler)
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
        # 等待服务器就绪
        import time
        time.sleep(0.3)

    def stop(self):
        """停止服务器"""
        if self._server:
            self._server.shutdown()
            self._server = None
        self._running = False
        logger.info("统计服务器已停止")
