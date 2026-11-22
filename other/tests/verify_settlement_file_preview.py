#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""收支清算「订单明细 · 点文件名看首页缩略图」（v7.21）验收。

为什么这条链路要单独验：
  页面（settlement.html）跑在**本地** stats_server 上，预览图在**云端**；
  图片不能走 /api/proxy（那条只透传 {success,message,data} 白名单字段，image/jpeg 会被
  吃成「云端响应格式错误」），也不能用 <img src> 直连（<img> 带不了 X-Launch-Token /
  X-Printer-Token 请求头，等于把令牌拼进 URL → 进 access log）。
  所以新增了一条二进制透传：本地 `GET /api/local/file_preview/<file_id>?page=N`
  → 云端 `GET /api/admin/file_preview/<file_id>?page=N`（admin scope，出站走请求头）。

验什么（A 静态接线 / B 真起 StatsServer 的行为）：
  A1 app.py 的 admin 预览路由挂了 require_printer_scope("admin")，且复用同一个渲染主体
     （allow_any=True 只跳过属主判定，不复制一份渲染逻辑 = 两端口径不会漂）
  A2 用户端 /api/file_preview 仍然是 openid 属主口径（管理端放宽没有顺带放宽用户端）
  A3 结算取数端点下发 file_id + has_preview（页面据此决定文件名可不可点）
  A4 stats_server 有本地路由 + 只认启动令牌头 + **没**混进 /api/proxy 白名单
  A5 CSP 的 img-src 放行 blob:（页面用 createObjectURL 显示）
  A6 页面：文件名 pv-can 可点 / fetch 带 X-Launch-Token / blob + revoke / seq 守卫
  B1 200 图片原样透传 + X-Preview-Page/Pages + no-store
  B2 出站带 X-Printer-Token 请求头，query 里没有 token=
  B3 非法 file_id → 400 且**零上游请求**（不把任意路径拼进 URL）
  B4 上游 403/404/415 → 同状态码 + message 原样（页面要 toast 出来）
  B5 上游 500 / 200 但不是图片 → 502（不把 HTML 错误页当图片塞给 <img>）
  B6 没令牌 → 403 且零上游；没连云端 → 503
  B7 图片超上限 → 502
  + 变异自证：放宽 id 正则 / 把上限改成 1 字节，对应判据必须变红。

退出码即判定：0 = 全绿。
"""

import http.client
import io
import json
import os
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(REPO, "local_print_tool")
APP = os.path.join(REPO, "mobile_apps", "printer-backend", "app.py")
SERVER = os.path.join(TOOL, "stats_server.py")
HTML = os.path.join(TOOL, "finance", "settlement.html")

for _k in ("NO_PROXY", "no_proxy"):
    _cur = [x for x in (os.environ.get(_k) or "").split(",") if x.strip()]
    for _h in ("127.0.0.1", "localhost"):
        if _h not in _cur:
            _cur.append(_h)
    os.environ[_k] = ",".join(_cur)

TMP = tempfile.mkdtemp(prefix="hn_preview_")
sys.path.insert(0, TOOL)

import stats_server as ss                                              # noqa: E402

_TOTAL = [0]
_FAILS = []


def chk(name, cond, detail=""):
    _TOTAL[0] += 1
    ok = bool(cond)
    if not ok:
        _FAILS.append(name)
    tail = ""
    if detail != "":
        text = detail if isinstance(detail, str) else repr(detail)
        tail = "  -- " + (text if len(text) <= 200 else text[:200] + "…")
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, tail))


def section(title):
    print("\n=== %s ===" % title)


def read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + b"PREVIEW-BODY-0123456789" * 3

# 桩云端各类失败的 (状态码, message)。桩与断言**共用这一份**：
# B4 要证明的是「云端 message 原样透传到页面」，不是「文案必须长这样」，
# 所以断言侧不许再抄一遍中文字面量（那样改文案就会假红，也过不了守卫的棘轮计数）。
STUB_ERRORS = {
    "forbidden": (403, "无权访问该文件"),
    "notfound": (404, "没有可预览的首页"),
    "unsupported": (415, "该格式暂不支持预览"),
    "boom": (500, "云端内部错误"),
}


# ═══════════════════════════════════════════════════════════════════════════
# 假云端：按 scenario 回包，逐个记录上游请求
# ═══════════════════════════════════════════════════════════════════════════

class CloudStub:
    def __init__(self):
        self.requests = []
        self.scenario = "ok"
        self._lock = threading.Lock()
        stub = self

        class _H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                with stub._lock:
                    stub.requests.append({"method": "GET", "path": self.path,
                                          "headers": dict(self.headers)})
                self._reply()

            def _reply(self):
                mode = stub.scenario
                if mode == "ok":
                    page = "1"
                    m = re.search(r"[?&]page=(\d+)", self.path)
                    if m:
                        page = m.group(1)
                    body = JPEG
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("X-Preview-Page", page)
                    self.send_header("X-Preview-Pages", "3")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if mode == "html":
                    body = b"<html><body>cloud error page</body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                code, msg = STUB_ERRORS.get(mode, (500, "未知"))
                payload = json.dumps({"success": False, "message": msg},
                                     ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_port
        self.url = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def take(self):
        with self._lock:
            out = list(self.requests)
            self.requests.clear()
        return out

    def stop(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:                                              # noqa: BLE001
            pass


class Resp:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def get(self, key):
        vals = self.headers.get(key.lower()) or []
        return vals[0] if vals else None

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:                                              # noqa: BLE001
            return {}


class Client:
    def __init__(self, port):
        self.port = port

    def get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.putrequest("GET", path, skip_accept_encoding=True)
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            conn.endheaders()
            r = conn.getresponse()
            data = r.read()
            hdrs = {}
            for k, v in r.getheaders():
                hdrs.setdefault(k.lower(), []).append(v)
            return Resp(r.status, hdrs, data)
        finally:
            try:
                conn.close()
            except Exception:                                          # noqa: BLE001
                pass


def boot(tag, api_url=None, token="test-token"):
    cloud = CloudStub()
    data_file = os.path.join(TMP, tag, "print_data.json")
    os.makedirs(os.path.dirname(data_file), exist_ok=True)
    srv = ss.StatsServer(api_url=(cloud.url if api_url is None else api_url),
                         token=token, port=0, data_file=data_file, finance_mode="local")
    srv.start_in_thread()
    return srv, cloud


def tok(srv):
    return {"X-Launch-Token": srv.launch_token}


# ═══════════════════════════════════════════════════════════════════════════
# A. 静态接线
# ═══════════════════════════════════════════════════════════════════════════

def _route_body(src, rule):
    """取某个 @app.route(...) 装饰的函数体（到下一个 @app.route 为止）。

    路由字符串后面常带 methods=["GET"]，所以按正则找而不是按精确串。
    """
    m = re.search(r"@app\.route\(\s*[\"']%s[\"']" % re.escape(rule), src)
    if not m:
        return ""
    i = m.start()
    j = src.find("@app.route(", i + 10)
    body = src[i:(j if j > 0 else len(src))]
    # 去掉注释行：两条路由之间的注释块常提到对方的参数名（allow_any 等），
    # 留着会让「用户端没漏管理端参数」这类判据假红。
    return "\n".join(ln for ln in body.splitlines()
                     if not ln.lstrip().startswith("#"))


def static_admin_route(app_src):
    body = _route_body(app_src, "/api/admin/file_preview/<file_id>")
    if not body:
        return False
    return ('@require_printer_scope("admin")' in body
            and "allow_any=True" in body
            and '_preview_request_context(' in body
            and '_preview_page_response(' in body
            and 'actor="printer"' in body)


def static_user_route_intact(app_src):
    body = _route_body(app_src, "/api/file_preview/<file_id>")
    if not body:
        return False
    # 用户端仍是 openid 属主口径；管理端的 allow_any 不许漏进来
    return ("_openid_for_media()" in body
            and "allow_any" not in body
            and '_preview_page_response(' in body)


def static_revenue_fields(app_src):
    """revenue 的明细行要带 file_id + has_preview（页面据此决定文件名可不可点）。"""
    i = app_src.find("files_by_order[oid].append({")
    if i < 0:
        return False
    seg = app_src[i:i + 2000]
    return ('"file_id": of_row["file_id"]' in seg
            and '"has_preview": bool(of_row["file_id"])' in seg
            and "_has_preview_for(" in seg)


def static_local_route(server_src):
    return (_LOCAL_PREFIX_OK(server_src)
            and "_handle_local_file_preview" in server_src
            and "path.startswith(_LOCAL_PREVIEW_PREFIX)" in server_src
            and "headers={\"X-Printer-Token\": self.token}" in server_src)


def _LOCAL_PREFIX_OK(server_src):
    return '"/api/local/file_preview/"' in server_src


def static_not_in_proxy_whitelist(server_src):
    """新接口不许混进 /api/proxy 白名单（那条只透传 JSON 字段）。"""
    i = server_src.find("_PROXY_ALLOWED_TARGETS")
    j = server_src.find("})", i)
    seg = server_src[i:j if j > 0 else i + 4000]
    return "file_preview" not in seg


def static_csp(server_src):
    m = re.search(r"_HTML_CSP\s*=\s*\(([\s\S]*?)\)\n", server_src)
    body = m.group(1) if m else ""
    hit = re.search(r"img-src[^;\"]*blob:", body)
    return bool(hit)


def static_page(html):
    checks = {
        "文件名有可点态 class": "pv-can" in html,
        "文件名点击 → openFilePreview": "openFilePreview(event," in html,
        # v7.27 起入口条件分两路：云端（file_id + has_preview）或本地单
        # （没有 file_id 的本地订单，靠本机 order_thumbs 渲的首页图）。
        "入口条件：云端 file_id + has_preview": re.search(
            r"var\s+_pvCloud\s*=\s*!!\(f\.file_id\s*&&\s*f\.has_preview\)", html) is not None,
        "入口条件：本地单 order_number + file_name（v7.27 本地缩略图）": re.search(
            r"var\s+_pvLocal\s*=", html) is not None
        and re.search(r"var\s+_pvCan\s*=\s*_pvCloud\s*\|\|\s*_pvLocal", html) is not None,
        "fetch 本地预览路由": "'/api/local/file_preview/' + encodeURIComponent(f.file_id)" in html,
        "带启动令牌头": "'X-Launch-Token': LAUNCH_TOKEN" in html,
        "读 X-Preview-Pages": "X-Preview-Pages" in html,
        "blob URL 显示": "URL.createObjectURL(r.blob)" in html,
        "收起时 revoke": "URL.revokeObjectURL(_fpObjUrl)" in html,
        "翻页竞态守卫": "seq !== m._fpSeq" in html,
        # v7.30：放大 = 全窗口覆盖层（#fpViewer）。小窗不再自己被撑成 1024 的大弹窗。
        "放大入口 → 全窗口查看器（小窗按钮 + 点图）":
            'onclick="openFpViewer()"' in html
            and '<div class="fp-stage" onclick="openFpViewer()"' in html
            and "⤢ 全屏" in html,
        "小窗不再走弹窗放大（fp-zoom 大舞台已删除）":
            "#fPrevMenu.fp-zoom" not in html,
        "覆盖层结构：fixed 全屏 + 背景 + 图片舞台":
            re.search(r"#fpViewer \{[^}]*position: fixed;[^}]*inset: 0;", html) is not None
            and ".fpv-bg" in html and ".fpv-stage" in html
            and "id = 'fpViewer'" in html,
        "淡入淡出：背景/主体 opacity 过渡 + 图片切页淡入":
            re.search(r"#fpViewer \.fpv-bg \{[^}]*opacity: 0;[^}]*transition: opacity", html) is not None
            and re.search(r"#fpViewer\.fpv-in \.fpv-bg \{ opacity: 1; \}", html) is not None
            and re.search(r"#fpViewer \.fpv-stage img \{[^}]*opacity: 0;", html) is not None
            and "classList.add('fpv-shown')" in html,
        "左右切换按钮（多页时）":
            'class="fpv-nav fpv-prev"' in html and 'class="fpv-nav fpv-next"' in html
            and "onclick=\"fpvGo(-1)\"" in html and "onclick=\"fpvGo(1)\"" in html,
        "键盘 ←/→ 翻页、Esc/✕ 关闭":
            "ev.key === 'ArrowLeft'" in html and "ev.key === 'ArrowRight'" in html
            and "ev.key === 'Escape'" in html and 'class="fpv-close"' in html,
        "底部缩略图条（点击切页 + 滚到视野内才取图）":
            'class="fpv-strip"' in html and "fpv-thumb" in html
            and "fpvGoTo(p)" in html and "IntersectionObserver" in html,
        "每页只取一次：大图 z 档、缩略图条 p 档各缓存各的，关闭时统一 revoke":
            "function _fpvFetchPage(st, page, small)" in html
            and "var cache = small ? st.thumbs : st.urls;" in html
            and "cache[served] = u;" in html
            and "(small ? 't' : 'z')" in html
            and "URL.revokeObjectURL(st.urls[k])" in html
            and "URL.revokeObjectURL(st.thumbs[k])" in html,
        "本地单缩略图按页取（&page=N）+ 页数等响应头":
            "f.file_name || '') + '&page=' + page" in html
            and "m._fpPages = 0;" in html,
        "滚轮缩放：下限是初始大小、上限 6 倍，以光标为锚点":
            "function _fpvWheel(ev)" in html and "var _FPV_ZOOM_MAX = 6;" in html
            and "Math.max(1, Math.min(_FPV_ZOOM_MAX, before * factor))" in html
            and "_fpvZoomAt(st, Math.pow(1.0015, -dy), ev.clientX, ev.clientY)" in html
            and "stage.addEventListener('wheel', _fpvWheel, { passive: false })" in html,
        "放大后左键拖动平移（贴边夹住、拖完不误关）":
            "function _fpvDown(ev)" in html and "function _fpvMove(ev)" in html
            and "img.classList.add('fpv-grabbing')" in html
            and "stage.addEventListener('mousedown', _fpvDown)" in html
            and "function _fpvBodyClick(ev)" in html,
        "点图片外关闭、点图/按钮/缩略图条不关":
            "closest('.fpv-img, .fpv-nav, .fpv-thumb, .fpv-close, .fpv-strip')" in html
            and "if (st.moved) { st.moved = false; return; }" in html,
        "放大过渡：尊重系统「减少动态效果」":
            "function _fpMotionOff()" in html and "_mqReduce && _mqReduce.matches" in html,
        "覆盖层开着时底表滚动/点外不再收掉小窗":
            re.search(r"function _fpOutside\(ev\) \{\s*if \(_fpvState\) return;", html) is not None
            and re.search(r"function _fpOnScroll\(ev\) \{\s*if \(_fpvState\) return;", html) is not None,
    }
    return checks


# ═══════════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════════

def main():
    app_src = read_text(APP)
    server_src = read_text(SERVER)
    html = read_text(HTML)

    section("A. 静态接线")
    chk("A1 云端 admin 预览路由：admin scope + 复用 _preview_request_context/"
        "_preview_page_response + actor=printer", static_admin_route(app_src))
    chk("A2 用户端 /api/file_preview 仍是 openid 属主口径（allow_any 没漏过来）",
        static_user_route_intact(app_src))
    chk("A3 结算端点下发 file_id + has_preview（页面据此决定可不可点）",
        static_revenue_fields(app_src))
    chk("A4 本地 /api/local/file_preview 路由 + 出站走 X-Printer-Token 请求头",
        static_local_route(server_src))
    chk("A5 新接口**没有**混进 /api/proxy 白名单（那条只透传 JSON 字段）",
        static_not_in_proxy_whitelist(server_src))
    chk("A6 CSP 的 img-src 放行 blob:", static_csp(server_src))
    for name, ok in static_page(html).items():
        chk("A7 " + name, ok)

    section("B. 行为 · 真起 StatsServer（port=0）+ 假云端")
    srv, cloud = boot("main")
    C = Client(srv.port)
    H = tok(srv)
    fid = "abc123DEF456"

    # ── B1 正常透传 ──
    cloud.take()
    r = C.get("/api/local/file_preview/%s?page=2" % fid, H)
    chk("B1 200 + 图片原样透传（字节一致）", r.status == 200 and r.body == JPEG,
        (r.status, len(r.body)))
    chk("B1 Content-Type 保持 image/jpeg",
        str(r.get("Content-Type") or "").startswith("image/jpeg"), r.get("Content-Type"))
    chk("B1 透传 X-Preview-Page / X-Preview-Pages（页面翻页用）",
        r.get("X-Preview-Page") == "2" and r.get("X-Preview-Pages") == "3",
        (r.get("X-Preview-Page"), r.get("X-Preview-Pages")))
    chk("B1 预览不缓存（Cache-Control: no-store）",
        (r.get("Cache-Control") or "").lower() == "no-store", r.get("Cache-Control"))

    hits = cloud.take()
    up = hits[-1] if hits else {}
    chk("B2 打到云端 admin 预览路由且保留 page 参数",
        up.get("path") == "/api/admin/file_preview/%s?page=2" % fid, up.get("path"))
    chk("B2 出站令牌在 X-Printer-Token 请求头里",
        (up.get("headers") or {}).get("X-Printer-Token") == "test-token")
    chk("B2 上游 path 里**没有** token=（不进 VPS access log）",
        "token=" not in (up.get("path") or ""))

    # ── B2b size / fmt 白名单透传（2026-11-19 分层尺寸 + 格式协商）──
    cloud.take()
    C.get("/api/local/file_preview/%s?page=1&size=t&fmt=webp" % fid, H)
    up2 = (cloud.take() or [{}])[-1]
    chk("B2b ?size=t&fmt=webp 原样透传到云端（结算页缩略图只要 320px）",
        up2.get("path") == "/api/admin/file_preview/%s?page=1&size=t&fmt=webp" % fid,
        up2.get("path"))
    cloud.take()
    C.get("/api/local/file_preview/%s?page=1&size=huge&fmt=png" % fid, H)
    up3 = (cloud.take() or [{}])[-1]
    chk("B2c 非法 size/fmt 被丢掉，不拼进上行 URL（防把任意串透给云端）",
        up3.get("path") == "/api/admin/file_preview/%s?page=1" % fid, up3.get("path"))

    # ── B3 非法 id：零上游 ──
    for bad in ("..%2F..%2Fetc%2Fpasswd", "a" * 65, "has%20space", ""):
        cloud.take()
        r = C.get("/api/local/file_preview/%s" % bad, H)
        n = len(cloud.take())
        chk("B3 非法 file_id %r → 400 且零上游请求" % (bad[:16] + ("…" if len(bad) > 16 else ""),),
            r.status == 400 and n == 0, (r.status, n))

    # ── B4 上游错误码与 message 透传 ──
    for mode, code in (("forbidden", 403), ("notfound", 404), ("unsupported", 415)):
        cloud.scenario = mode
        r = C.get("/api/local/file_preview/%s" % fid, H)
        j = r.json()
        chk("B4 上游 %s → 本地 %d 且 message 原样" % (mode, code),
            r.status == code and j.get("message") == STUB_ERRORS[mode][1],
            (r.status, j.get("message")))

    # ── B5 其它失败一律 502（不能把 HTML 错误页当图片塞给 <img>）──
    cloud.scenario = "html"
    r = C.get("/api/local/file_preview/%s" % fid, H)
    chk("B5 上游 200 但 Content-Type 不是 image/* → 502", r.status == 502,
        (r.status, r.get("Content-Type")))
    cloud.scenario = "boom"
    r = C.get("/api/local/file_preview/%s" % fid, H)
    chk("B5 上游 500 → 502", r.status == 502, r.status)
    cloud.scenario = "ok"

    # ── B6 令牌门 / 未连云端 ──
    cloud.take()
    r = C.get("/api/local/file_preview/%s" % fid)
    chk("B6 没有启动令牌 → 403 且零上游请求", r.status == 403 and len(cloud.take()) == 0,
        r.status)

    # 「未连接云端」用**同一台**服务器验：stats_server 把 api_url/token 挂在
    # _StatsHandler 的**类属性**上（start() 里注入），同进程起第二个 StatsServer 会把
    # 第一台的令牌一起覆盖掉 —— 所以这里改类属性并在 finally 里还原，不另起一台。
    old_api, old_token = ss._StatsHandler.api_url, ss._StatsHandler.token
    try:
        ss._StatsHandler.api_url = ""
        ss._StatsHandler.token = ""
        r = C.get("/api/local/file_preview/%s" % fid, H)
        chk("B6 未连接云端 → 503（不是 500，也不假装成功）", r.status == 503, r.status)
    finally:
        ss._StatsHandler.api_url = old_api
        ss._StatsHandler.token = old_token

    # ── B7 体积上限 ──
    old_cap = ss._LOCAL_PREVIEW_MAX_BYTES
    try:
        ss._LOCAL_PREVIEW_MAX_BYTES = 4
        r = C.get("/api/local/file_preview/%s" % fid, H)
        chk("B7 图片超过上限 → 502（不把超大 body 塞给页面）", r.status == 502, r.status)
    finally:
        ss._LOCAL_PREVIEW_MAX_BYTES = old_cap

    section("C. 变异自证（放宽判据 → 对应 PASS 必须变红，事后还原）")
    # C1 把 id 正则放宽成「任意字符」→ B3 的越界用例必须被拼进 URL（打进上游）
    old_re = ss._LOCAL_PREVIEW_ID_RE
    try:
        ss._LOCAL_PREVIEW_ID_RE = re.compile(r"^.*$")
        cloud.take()
        r = C.get("/api/local/file_preview/%s" % "a" * 65, H)
        n = len(cloud.take())
        chk("C1 变异：id 正则放宽后超长 id 真的打到了上游（说明 B3 有判别力）",
            r.status != 400 and n >= 1, (r.status, n))
    finally:
        ss._LOCAL_PREVIEW_ID_RE = old_re
    # C2 还原后同一请求必须重新变 400
    r = C.get("/api/local/file_preview/%s" % ("a" * 65), H)
    chk("C2 还原后超长 id 重新变 400", r.status == 400, r.status)

    # C3 静态判据的变异：把 Revenue 的 file_id 列删掉 → A3 必须变红
    mutated = app_src.replace('"file_id": of_row["file_id"]', '"": of_row["file_id"]', 1)
    chk("C3 变异：结算行字典去掉 file_id → A3 判据变红",
        mutated != app_src and static_revenue_fields(mutated) is False)

    cloud.stop()
    try:
        srv.stop()
    except Exception:                                              # noqa: BLE001
        pass

    print("\n" + "=" * 74)
    if _FAILS:
        print("订单明细文件预览验收：FAIL %d / %d" % (len(_FAILS), _TOTAL[0]))
        for n in _FAILS:
            print("  ✗ " + n)
        return 1
    print("订单明细文件预览验收：全部通过 ✅  （PASS %d / %d）" % (_TOTAL[0], _TOTAL[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
