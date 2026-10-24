#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收 · S2-10 本地 HTTP 服务加固收尾（LT-11 / DS-23）

依据：
  · docs/安全加固修复计划_2026-10-23.md §S2-10（本地服务加固测试；浏览器历史无 token）
  · docs/审查报告_2026-10/data-security-2026-10-23/frag_local.md **[LT-11]**（权威缺陷清单）

本脚本验的是**行为**（真起 StatsServer + 假云端），不是"源码里有这行字"：

  ① 一次性 nonce：`?nonce=` 首用 200 且注入真实 launch token；同一 nonce 重放 403；
     伪造/过期 403；老入口 `?token=` 仍可用；nonce 只对 HTML 生效（拿去调 API 一律 403，
     而且不会被顺带消费掉）。
  ② Host 白名单：只认 127.0.0.1 / localhost / [::1] + **实际端口**；evil 域名、缺 Host、
     别的端口一律 403（静态入口与 API 都过同一道门）。
  ③ ACAO 收窄：本服务 origin → 回该 origin；外部 origin / `null` → **没有** ACAO
     （预检同理，且 Allow-Headers 含 X-Launch-Token / X-Finance-Session / Content-Type）。
  ④ 恒时比较：`_token_equal` 用 hmac.compare_digest（静态）+ 请求期真的调到它（行为打桩）。
  ⑤ 代理路径归一化 + 白名单：`..` / 未知 / 空段 / 编码越界 → 4xx 且假云端**零请求**；
     白名单内路径 → 上游真收到。白名单必须 ⊇ settlement.html 里所有 `/api/proxy/...` 调用。
  ⑥ 出站 token 改请求头：假云端断言收到 `X-Printer-Token: test-token`，且 path/query
     **不含** `token=`；客户端 query 里的 token 仍被剥掉。
  ⑦ 安全响应头：所有响应 XFO/nosniff/Referrer-Policy；HTML 另加 CSP（放行内联
     script/style 与 jsdelivr 兜底，**没有**因此删掉 CDN 兜底）。
  ⑧ 并发上限：沿用的 `_LimitedThreadingHTTPServer`（S1-09）仍在，且行为上真的超额 503。

另有 **9 条变异自证**（临时改源码 → 子进程里跑同一判据 → 必须变红 → try/finally 恢复）：
反向证明上面这些断言不是"永远绿"的摆设。

用法（口径与 run_all 一致）：
    PYTHONPATH= PYTHONUTF8=1 "C:/Users/.../Python311/python.exe" tests/verify_local_service_hardening.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO, "local_print_tool")
SERVER = os.path.join(TOOL, "stats_server.py")
GUI = os.path.join(TOOL, "gui.py")
HTML = os.path.join(TOOL, "finance", "settlement.html")

# 假云端在本机回环：别让系统/环境代理插手（net_direct 也会补，这里先兜住）
for _k in ("NO_PROXY", "no_proxy"):
    _cur = [x for x in (os.environ.get(_k) or "").split(",") if x.strip()]
    for _h in ("127.0.0.1", "localhost"):
        if _h not in _cur:
            _cur.append(_h)
    os.environ[_k] = ",".join(_cur)

TMP = tempfile.mkdtemp(prefix="hn_s210_")
sys.path.insert(0, TOOL)

import stats_server as ss                                            # noqa: E402

_TOTAL = [0]
_FAILS = []


def chk(name, cond, detail=""):
    """计数式断言（仓库既有验收风格）：失败退出非 0。"""
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


# ═══════════════════════════════════════════════════════════════════════════
# 假云端：记录每一个上游请求（方法 / path（含 query）/ 头 / body），回固定 JSON
# ═══════════════════════════════════════════════════════════════════════════

class CloudStub:
    def __init__(self):
        self.requests = []
        self._lock = threading.Lock()
        stub = self

        class _H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _record(self, method):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    n = 0
                body = self.rfile.read(n) if n > 0 else b""
                with stub._lock:
                    stub.requests.append({"method": method, "path": self.path,
                                          "headers": dict(self.headers), "body": body})
                # data=None：本地模式的云端凭据来源取不到东西 → 走本机权威凭据
                payload = json.dumps({"success": True, "data": None},
                                     ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._record("GET")

            def do_POST(self):
                self._record("POST")

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_port
        self.url = "http://127.0.0.1:%d" % self.port
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def take(self):
        with self._lock:
            out = list(self.requests)
            self.requests.clear()
        return out

    def count(self):
        with self._lock:
            return len(self.requests)

    def stop(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:                                            # noqa: BLE001
            pass


# ═══════════════════════════════════════════════════════════════════════════
# 本机 HTTP 客户端：要能伪造 / 省略 Host（http.client 直接控制，urllib 做不到）
# ═══════════════════════════════════════════════════════════════════════════

_AUTO = object()


class Resp:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers            # 小写键 → 值列表
        self.body = body

    def get(self, key):
        vals = self.headers.get(key.lower()) or []
        return vals[0] if vals else None

    def has(self, key):
        return key.lower() in self.headers

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:                                            # noqa: BLE001
            return {}

    def text(self):
        return self.body.decode("utf-8", "replace")


class Client:
    def __init__(self, port):
        self.port = port

    def send(self, method, path, headers=None, body=None, host=_AUTO):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            if host is _AUTO:
                conn.putrequest(method, path, skip_accept_encoding=True)
            elif host is None:                    # 一个 Host 头都不发
                conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            else:                                 # 伪造 Host
                conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
                conn.putheader("Host", host)
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            if body is not None:
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                conn.putheader("Content-Length", str(len(raw)))
                conn.endheaders(raw)
            else:
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
            except Exception:                                        # noqa: BLE001
                pass


def boot(tag="main"):
    """起一个**真的** StatsServer（port=0 → 系统分配）+ 假云端。"""
    cloud = CloudStub()
    data_file = os.path.join(TMP, tag, "print_data.json")
    os.makedirs(os.path.dirname(data_file), exist_ok=True)
    srv = ss.StatsServer(api_url=cloud.url, token="test-token", port=0,
                         data_file=data_file, finance_mode="local")
    srv.start_in_thread()
    return srv, cloud


def tok(srv):
    return {"X-Launch-Token": srv.launch_token}


def jhead(srv, **extra):
    h = dict(tok(srv))
    h["Content-Type"] = "application/json"
    h.update(extra)
    return h


# ═══════════════════════════════════════════════════════════════════════════
# 静态判据（纯函数：输入源码文本 → bool；变异自证直接复用它）
# ═══════════════════════════════════════════════════════════════════════════

def static_constant_time(src):
    """④ 令牌比较路径必须用 hmac.compare_digest（header / query / nonce 三条路径）。"""
    m = re.search(r"def _token_equal\([\s\S]{0,900}?\n\ndef ", src)
    body = m.group(0) if m else ""
    return ("hmac.compare_digest(candidate, expected)" in body
            and re.search(r"def _check_launch_token\([\s\S]{0,1500}?_token_equal\(", src) is not None
            and re.search(r"def consume_nonce\([\s\S]{0,1500}?_token_equal\(", src) is not None
            and "== self.launch_token" not in src)


def static_concurrency(src):
    """⑧ S1-09 的并发上限仍在（类 + 信号量 + 超额 503 + 归还槽位 + 实际用上它）。"""
    m = re.search(r"^_MAX_CONCURRENT_REQUESTS\s*=\s*(\d+)", src, re.M)
    cap = int(m.group(1)) if m else 0
    return ("class _LimitedThreadingHTTPServer(ThreadingHTTPServer)" in src
            and "max_workers = _MAX_CONCURRENT_REQUESTS" in src
            and "BoundedSemaphore(self.max_workers)" in src
            and "acquire(blocking=False)" in src
            and "503 Service Unavailable" in src
            and "self._slots.release()" in src
            and re.search(r"self\._server = _LimitedThreadingHTTPServer\(\(", src) is not None
            and 0 < cap <= 64)


def proxy_calls_in_page(html_text):
    """settlement.html 里实际调用的 `/api/proxy/...` 面（→ 对应的云端目标路径集合）。

    注意**不要**要求路径后面紧跟引号：页面里有 `fetch('/api/proxy/…?start_date=…' + …)`
    这种拼查询串的写法，带引号的回溯会把它们整条漏掉（漏掉就变成「白名单里多了个页面
    用不到的项」的假红）。
    """
    found = set(re.findall(r"""['"](/api/proxy/[A-Za-z0-9_./-]+)""", html_text))
    return found, set("/api" + p[len("/api/proxy"):] for p in found)


# ═══════════════════════════════════════════════════════════════════════════
# A. 静态接线
# ═══════════════════════════════════════════════════════════════════════════

def read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def section_static():
    section("A. 静态接线（stats_server.py / gui.py / settlement.html）")
    src = read_text(SERVER)
    gui_src = read_text(GUI)
    html = read_text(HTML)

    chk("④ _token_equal 用 hmac.compare_digest，且 header/query/nonce 三条路径都走它",
        static_constant_time(src))
    chk("⑧ 并发上限（_LimitedThreadingHTTPServer + 信号量 + 超额 503）仍在",
        static_concurrency(src))
    chk("① 一次性 nonce 的「先作废再回报成功」写在 consume_nonce 里",
        "self._nonces.pop(matched, None)" in src and "def consume_nonce(" in src)
    chk("③ 源码里没有任何**无条件** ACAO `*`",
        '"Access-Control-Allow-Origin", "*"' not in src
        and "'Access-Control-Allow-Origin', '*'" not in src)
    chk("③ 预检 Allow-Headers 含页面真正用到的三个头",
        "X-Launch-Token, X-Finance-Session, Content-Type" in src)
    chk("⑥ 代理不再把 printer token 拼进 query（?token= 两处都清了）",
        "token={self.token}" not in src and 'params={"token"' not in src)
    chk("⑦ 安全响应头常量齐全（XFO / Referrer-Policy / nosniff）",
        '"X-Frame-Options", "DENY"' in src
        and '"Referrer-Policy", "no-referrer"' in src
        and '"X-Content-Type-Options", "nosniff"' in src)
    chk("⑦ CSP 放行内联脚本/样式 + jsdelivr 兜底 + frame-ancestors/object-src 收口",
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net" in src
        and "frame-ancestors 'none'" in src and "base-uri 'none'" in src
        and "object-src 'none'" in src)
    chk("⑦ CSP **没有**以删掉 CDN 兜底为代价（页面里那行兜底仍在）",
        "cdn.jsdelivr.net" in html and "chart.umd.min.js" in html)
    # 「CSP 真的不把页面打坏」的静态判据：页面只用 self + 内联 + jsdelivr，且没有 eval
    ext_hosts = set(re.findall(r"https?://([A-Za-z0-9._-]+)", html))
    chk("⑦ 页面外链只依赖 jsdelivr 一个域（default-src 'self' 不会误伤其它资源）",
        ext_hosts <= {"cdn.jsdelivr.net"}, sorted(ext_hosts))
    chk("⑦ 页面没有 eval/new Function（CSP 未开 'unsafe-eval' 也不会坏）",
        "eval(" not in html and "new Function" not in html)

    # gui.py：只改打开 URL 的构造（不再把长期有效的 launch token 拼进地址栏）
    i = gui_src.find("def _on_open_finance")
    blk = gui_src[i:i + 5000] if i >= 0 else ""
    chk("① gui.py 打开页面用一次性 nonce（issue_nonce → ?nonce=…&mode=）",
        "issue_nonce()" in blk and "?nonce={nonce}" in blk)
    chk("★ ① gui.py 里不再有把 launch_token 拼进 URL 的写法",
        "?token={self._stats_server.launch_token}" not in gui_src
        and "launch_token}&" not in gui_src)

    # settlement.html：加载后立刻把地址栏里的凭据清掉
    chk("① settlement.html 用 history.replaceState 清掉 nonce/token（只删这两个参数）",
        "history.replaceState" in html
        and "_q.delete('nonce')" in html and "_q.delete('token')" in html)


# ═══════════════════════════════════════════════════════════════════════════
# B. 行为（真 StatsServer）
# ═══════════════════════════════════════════════════════════════════════════

def section_behavior():
    section("B. 行为 · 真起 StatsServer（port=0 / 假云端 / token='test-token'）")
    srv, cloud = boot("main")
    C = Client(srv.port)
    port = srv.port
    own = "http://127.0.0.1:%d" % port
    try:
        _b1_nonce(srv, C)
        _b2_host(srv, C, port)
        _b3_cors(srv, C, port, own)
        _b4_constant_time(srv, C)
        _b5_proxy(srv, C, cloud)
        _b6_headers(srv, C)
    finally:
        srv.stop()
        cloud.stop()

    _b7_concurrency_behavior()


def _b1_nonce(srv, C):
    print("\n-- ① 一次性 nonce（不再 ?token= 进浏览器历史） --")
    n1 = srv.issue_nonce()
    r = C.send("GET", "/settlement.html?nonce=%s&mode=local" % n1)
    chk("① 一次性 nonce 首次打开 → 200（HTML）", r.status == 200 and "text/html" in (r.get("content-type") or ""),
        r.status)
    chk("★ ① HTML 仍注入真实 launch token（页面后续用它发 X-Launch-Token 头）",
        srv.launch_token.encode() in r.body and b"__LAUNCH_TOKEN__" not in r.body)
    chk("① 注入 finance_mode（mode 参数与深链语义未变）",
        b"var FINANCE_MODE = 'local'" in r.body)

    r2 = C.send("GET", "/settlement.html?nonce=%s&mode=local" % n1)
    chk("★ ① 同一 nonce 重放 → 403（一次性，服务端当场作废）", r2.status == 403, r2.status)
    chk("① 伪造 nonce → 403", C.send("GET", "/settlement.html?nonce=deadbeefdeadbeef").status == 403)
    n_exp = srv.issue_nonce(ttl=-1)
    chk("① 过期 nonce → 403", C.send("GET", "/settlement.html?nonce=%s" % n_exp).status == 403)

    r3 = C.send("GET", "/settlement.html?token=%s&mode=local" % srv.launch_token)
    chk("① 老入口 ?token= 仍然可用（老书签/旧流程不被打死）",
        r3.status == 200 and srv.launch_token.encode() in r3.body, r3.status)
    chk("① 老入口用错误 token → 403", C.send("GET", "/settlement.html?token=nope").status == 403)
    chk("① 无凭据打开 HTML 入口 → 403（根路径同样要凭据）", C.send("GET", "/").status == 403)

    # nonce 只对 HTML 入口有效：拿去调 API 一律 403，而且不会被顺带消费掉
    n2 = srv.issue_nonce()
    st_api = C.send("GET", "/api/health?nonce=%s" % n2).status
    st_html = C.send("GET", "/settlement.html?nonce=%s" % n2).status
    chk("★ ① nonce 不能当 API 凭据（非 HTML 入口一律 403），且不会被 API 请求顺带消费",
        st_api == 403 and st_html == 200, (st_api, st_html))

    # HEAD 也必须过同一道门（旧实现没覆写 do_HEAD ⇒ 基类直接发静态文件头，两道门全绕过）
    r_head = C.send("HEAD", "/settlement.html")
    chk("★ ①/② HEAD 无凭据 → 403（旧实现 HEAD 绕过令牌门与 Host 门）",
        r_head.status == 403 and not r_head.body, (r_head.status, len(r_head.body)))
    r_head_ok = C.send("HEAD", "/settlement.html", headers=tok(srv))
    chk("① HEAD 带凭据 → 200 且只回头（不写 body）",
        r_head_ok.status == 200 and not r_head_ok.body and r_head_ok.get("content-security-policy"),
        (r_head_ok.status, len(r_head_ok.body)))


def _b2_host(srv, C, port):
    print("\n-- ② Host 白名单（DNS rebinding） --")
    chk("② Host=127.0.0.1:<实际端口> → 200",
        C.send("GET", "/api/health", headers=tok(srv)).status == 200)
    chk("② Host=localhost:<实际端口> → 200",
        C.send("GET", "/api/health", headers=tok(srv), host="localhost:%d" % port).status == 200)
    chk("② Host=[::1]:<实际端口> → 200",
        C.send("GET", "/api/health", headers=tok(srv), host="[::1]:%d" % port).status == 200)
    chk("★ ② Host=evil.example（DNS rebinding 用的域名）→ 403",
        C.send("GET", "/api/health", headers=tok(srv), host="evil.example").status == 403)
    chk("★ ② 缺 Host 头 → 403（HTTP/1.0、原始 socket）",
        C.send("GET", "/api/health", headers=tok(srv), host=None).status == 403)
    chk("★ ② Host 端口不匹配（127.0.0.1:<别的端口>）→ 403",
        C.send("GET", "/api/health", headers=tok(srv), host="127.0.0.1:%d" % (port + 1)).status == 403)
    chk("② Host 门对静态入口同样生效（evil Host + 有效 token → 403）",
        C.send("GET", "/settlement.html?token=%s" % srv.launch_token,
               host="attacker.example").status == 403)
    chk("② Host 门对预检同样生效（OPTIONS + evil Host → 403）",
        C.send("OPTIONS", "/api/health", host="evil.example").status == 403)


def _b3_cors(srv, C, port, own):
    print("\n-- ③ ACAO 收窄（不再无条件 *） --")
    r = C.send("GET", "/api/health", headers=dict(tok(srv), Origin=own))
    chk("③ 本服务 origin → 回该 origin",
        r.status == 200 and r.get("access-control-allow-origin") == own,
        r.get("access-control-allow-origin"))
    r = C.send("GET", "/api/health", headers=dict(tok(srv), Origin="https://evil.example"))
    chk("★ ③ 外部 origin → 响应里**没有** ACAO",
        r.status == 200 and not r.has("access-control-allow-origin"),
        r.get("access-control-allow-origin"))
    r = C.send("GET", "/api/health", headers=dict(tok(srv), Origin="null"))
    chk("③ Origin: null → 没有 ACAO", r.status == 200 and not r.has("access-control-allow-origin"))
    own_l = "http://localhost:%d" % port
    r = C.send("GET", "/api/health", headers=dict(tok(srv), Origin=own_l))
    chk("③ localhost origin 也回显（与 127.0.0.1 同权）",
        r.get("access-control-allow-origin") == own_l, r.get("access-control-allow-origin"))

    r = C.send("OPTIONS", "/api/health", headers={"Origin": own})
    allow_headers = r.get("access-control-allow-headers") or ""
    chk("★ ③ 预检（本服务 origin）→ 204 + ACAO + Allow-Headers 含三个头",
        r.status == 204 and r.get("access-control-allow-origin") == own
        and all(k in allow_headers for k in ("X-Launch-Token", "X-Finance-Session", "Content-Type")),
        (r.status, allow_headers))
    r = C.send("OPTIONS", "/api/health", headers={"Origin": "https://evil.example"})
    chk("★ ③ 预检（外部 origin）→ 没有 ACAO",
        r.status == 204 and not r.has("access-control-allow-origin"))


def _b4_constant_time(srv, C):
    print("\n-- ④ 恒时比较（行为打桩：请求期真的走 compare_digest） --")
    calls = {"n": 0}
    real = ss.hmac.compare_digest

    def _spy(a, b):
        calls["n"] += 1
        return real(a, b)

    ss.hmac.compare_digest = _spy
    try:
        calls["n"] = 0
        st_ok = C.send("GET", "/api/health", headers=tok(srv)).status
        n_ok = calls["n"]
        st_bad = C.send("GET", "/api/health", headers={"X-Launch-Token": "wrong-token"}).status
        n_bad = calls["n"] - n_ok
    finally:
        ss.hmac.compare_digest = real
    chk("★ ④ 正确令牌的请求确实调到 hmac.compare_digest（不是裸 ==）",
        st_ok == 200 and n_ok >= 1, (st_ok, n_ok))
    chk("★ ④ 错误令牌同样走到 compare_digest（不匹配也不允许短路成 ==）",
        st_bad == 403 and n_bad >= 1, (st_bad, n_bad))


def _b5_proxy(srv, C, cloud):
    print("\n-- ⑤ 代理路径归一化 + 白名单；⑥ 出站 token 走请求头 --")
    cloud.take()
    r = C.send("GET", "/api/proxy/admin/statistics/months", headers=tok(srv))
    hits = cloud.take()
    chk("⑤ 白名单内路径 → 上游收到请求（链路没被打断）",
        r.status == 200 and len(hits) == 1
        and hits[0]["path"] == "/api/admin/statistics/months", (r.status, [h["path"] for h in hits]))
    chk("★ ⑥ 出站 token 在 X-Printer-Token 请求头里",
        bool(hits) and hits[0]["headers"].get("X-Printer-Token") == "test-token",
        hits[0]["headers"].get("X-Printer-Token") if hits else None)
    chk("★ ⑥ 上游 path/query **不含** token=（不再进 access log）",
        bool(hits) and "token=" not in hits[0]["path"], hits[0]["path"] if hits else None)

    bad_cases = (
        ("/api/proxy/../uploads/x", "`..` 越界"),
        ("/api/proxy/unknown/path", "不在白名单"),
        ("/api/proxy/printer/devices/", "尾斜杠（空段）"),
        ("/api/proxy/%2e%2e/uploads/x", "百分号编码的 `..`"),
        ("/api/proxy/admin/../pricing", "中段 `..`"),
    )
    for path, tag in bad_cases:
        cloud.take()
        r = C.send("GET", path, headers=tok(srv))
        hits = cloud.take()
        chk("★ ⑤ %s → 4xx 且假云端**零上游请求**" % tag,
            400 <= r.status < 500 and not hits, (r.status, len(hits)))

    # 客户端 query 里的 token 仍要剥掉（_strip_token_from_query 语义保留）
    cloud.take()
    r = C.send("GET", "/api/proxy/admin/statistics/revenue?start_date=2000-01-01&token=CLIENT-LEAK",
               headers=tok(srv))
    hits = cloud.take()
    upstream = hits[0]["path"] if hits else ""
    chk("⑥ 客户端 query 里的 token 仍被剥掉（start_date 等其它参数照旧透传）",
        r.status == 200 and "token=" not in upstream and "start_date=2000-01-01" in upstream,
        upstream)

    # POST 侧同一条白名单（要先解锁一个模块才过得了财务会话门）
    r = C.send("POST", "/api/local/finance_auth/setup", headers=jhead(srv),
               body={"module": "all", "new_password": "Str0ng-pass"})
    chk("⑤（前置）首次设置密码 → 200", r.status == 200 and r.json().get("success") is True,
        (r.status, r.text()[:80]))
    r = C.send("POST", "/api/local/finance_auth/unlock", headers=jhead(srv),
               body={"module": "auth", "password": "Str0ng-pass"})
    sess = (r.json() or {}).get("session") or ""
    chk("⑤（前置）解锁拿到 finance_session", r.status == 200 and bool(sess),
        (r.status, r.text()[:80]))

    cloud.take()
    r = C.send("POST", "/api/proxy/printer/devices/cleanup",
               headers=jhead(srv, **{"X-Finance-Session": sess}), body={"dry_run": True})
    hits = cloud.take()
    chk("★ ⑤/⑥ POST 白名单内路径 → 上游收到请求、token 在请求头、query 无 token",
        r.status == 200 and len(hits) == 1
        and hits[0]["headers"].get("X-Printer-Token") == "test-token"
        and "token=" not in hits[0]["path"],
        (r.status, [h["path"] for h in hits]))
    cloud.take()
    r = C.send("POST", "/api/proxy/unknown/path",
               headers=jhead(srv, **{"X-Finance-Session": sess}), body={"a": 1})
    hits = cloud.take()
    chk("★ ⑤ POST 未知代理路径 → 4xx 且零上游请求（不读 body、不转发）",
        400 <= r.status < 500 and not hits, (r.status, len(hits)))


def _b6_headers(srv, C):
    print("\n-- ⑦ 安全响应头 / CSP --")
    r = C.send("GET", "/api/health", headers=tok(srv))
    chk("⑦ 所有响应 X-Frame-Options: DENY", r.get("x-frame-options") == "DENY", r.get("x-frame-options"))
    chk("⑦ 所有响应 Referrer-Policy: no-referrer",
        r.get("referrer-policy") == "no-referrer", r.get("referrer-policy"))
    chk("⑦ 所有响应 X-Content-Type-Options: nosniff",
        r.get("x-content-type-options") == "nosniff", r.get("x-content-type-options"))

    n = srv.issue_nonce()
    r = C.send("GET", "/settlement.html?nonce=%s" % n)
    csp = r.get("content-security-policy") or ""
    chk("★ ⑦ HTML 响应带 CSP（default-src/script-src/style-src/connect-src/frame-ancestors）",
        all(x in csp for x in ("default-src 'self'", "script-src 'self'", "style-src 'self'",
                               "connect-src 'self'", "frame-ancestors 'none'")), csp[:120])
    chk("★ ⑦ CSP 放行页面内联脚本/样式与 jsdelivr 兜底（否则页面直接坏）",
        "'unsafe-inline'" in csp and "https://cdn.jsdelivr.net" in csp, csp[:120])
    chk("⑦ HTML 也带 XFO/nosniff/Referrer-Policy",
        r.get("x-frame-options") == "DENY" and r.get("x-content-type-options") == "nosniff"
        and r.get("referrer-policy") == "no-referrer")

    r = C.send("GET", "/settlement.html")
    chk("⑦ 403 提示页同样带安全头 + CSP",
        r.status == 403 and r.get("x-frame-options") == "DENY"
        and bool(r.get("content-security-policy")))

    r = C.send("GET", "/chart.umd.min.js")
    chk("⑦ 公共静态文件（chart.umd.min.js）也带安全头，且不要求令牌",
        r.status == 200 and r.get("x-frame-options") == "DENY"
        and (r.get("content-type") or "").find("javascript") >= 0,
        (r.status, r.get("content-type")))


def _b7_concurrency_behavior():
    print("\n-- ⑧ 并发上限（行为：单槽位服务器，超额 503） --")
    class _Slow(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            time.sleep(0.6)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    class _OneSlot(ss._LimitedThreadingHTTPServer):
        max_workers = 1

    one = _OneSlot(("127.0.0.1", 0), _Slow)
    threading.Thread(target=one.serve_forever, daemon=True).start()
    try:
        results = []

        def _hit():
            try:
                c = http.client.HTTPConnection("127.0.0.1", one.server_port, timeout=10)
                try:
                    c.request("GET", "/")
                    r = c.getresponse()
                    r.read()
                    results.append(r.status)
                finally:
                    c.close()
            except Exception as e:                                   # noqa: BLE001
                results.append(type(e).__name__)

        # 连接级的 503 与"客户端正在写请求时服务器已关连接"天然有竞态（第二条可能被
        # 判成连接重置而不是 503）。判据是"超额必须被拒"，所以给几次重试、任一次成立即算过。
        got, attempts = [], 0
        for attempts in range(1, 5):
            del results[:]
            t1 = threading.Thread(target=_hit)
            t1.start()
            time.sleep(0.2)                  # 让第一条占住唯一的槽位
            t2 = threading.Thread(target=_hit)
            t2.start()
            t1.join(20)
            t2.join(20)
            got = sorted(x for x in results if isinstance(x, int))
            if got == [200, 503] and one.rejected >= 1:
                break
            time.sleep(0.2)
    finally:
        one.shutdown()
        one.server_close()
    chk("★ ⑧ 并发上限行为：两个并发只放行 1 个，另一个立刻 503（不排队、不无限起线程）",
        got == [200, 503] and one.rejected >= 1, (got, one.rejected, "尝试 %d 次" % attempts))


# ═══════════════════════════════════════════════════════════════════════════
# C. 白名单 ⊇ 页面调用面
# ═══════════════════════════════════════════════════════════════════════════

def section_whitelist():
    section("C. 代理白名单 ⊇ settlement.html 的调用面（防止新接口忘加白名单）")
    html = read_text(HTML)
    found, targets = proxy_calls_in_page(html)
    allowed = set(ss._PROXY_ALLOWED_TARGETS)
    chk("⑤ 从 settlement.html 抓到 ≥11 条 `/api/proxy/...` 调用（判据没空转）",
        len(found) >= 11, sorted(found))
    missing = sorted(targets - allowed)
    chk("★ ⑤ 白名单覆盖页面所有代理调用（漏一个 = 页面功能当场 403）", not missing, missing)
    extra = sorted(allowed - targets)
    chk("⑤ 白名单没有页面用不到的项（攻击面保持最小）", not extra, extra)
    chk("⑤ 白名单是**硬编码集合**（不是从页面/网络动态推导）",
        "frozenset({" in read_text(SERVER) and "_PROXY_ALLOWED_TARGETS = frozenset({" in read_text(SERVER))


# ═══════════════════════════════════════════════════════════════════════════
# D. 变异自证（子进程里跑，父进程只负责改源码 / 恢复）
# ═══════════════════════════════════════════════════════════════════════════

# 每条：id / 说明 / (old, new) 替换对 / 探针
MUTATIONS = [
    {"id": "host_whitelist", "desc": "Host 白名单失效（_client_host_ok 恒 True）",
     "pairs": [("    def _client_host_ok(self) -> bool:\n",
                "    def _client_host_ok(self) -> bool:\n        return True  # MUT\n")]},
    {"id": "nonce_one_shot", "desc": "nonce 不作废（留着重放也能过）",
     "pairs": [("            self._nonces.pop(matched, None)",
                "            pass  # MUT: 不作废")]},
    {"id": "proxy_whitelist", "desc": "代理白名单失效（未知路径也放行）",
     "pairs": [("    if target not in _PROXY_ALLOWED_TARGETS:",
                "    if False:  # MUT")]},
    {"id": "cors_echo_all", "desc": "ACAO 退化成回显任何 Origin",
     "pairs": [("    def _cors_origin(self) -> str:\n",
                "    def _cors_origin(self) -> str:\n"
                "        return (self.headers.get(\"Origin\") or \"\").strip()  # MUT\n")]},
    {"id": "security_headers", "desc": "安全响应头不再注入",
     "pairs": [("                for _k, _v in _SECURITY_HEADERS:",
                "                for _k, _v in ():  # MUT")]},
    {"id": "outbound_header", "desc": "出站 token 回到「不带请求头」",
     "pairs": [('headers={"X-Printer-Token": self.token})',
                'headers={})  # MUT')]},
    {"id": "hmac_compare", "desc": "令牌比较退回裸 ==",
     "pairs": [("    return hmac.compare_digest(candidate, expected)",
                "    return candidate == expected  # MUT")]},
    {"id": "concurrency_acquire", "desc": "并发上限不 acquire（信号量形同虚设）",
     "pairs": [("        if not self._slots.acquire(blocking=False):",
                "        if False:  # MUT")]},
    {"id": "concurrency_cap", "desc": "max_workers 改成极大（上限失效）",
     "pairs": [("    max_workers = _MAX_CONCURRENT_REQUESTS",
                "    max_workers = 10 ** 9  # MUT")]},
]


def run_child(case_id):
    """在**子进程**里跑被变异的源码（进程隔离：绝不污染本进程已导入的模块）。"""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = ""                                        # 与 run_all 口径一致
    env["PYTHONPYCACHEPREFIX"] = tempfile.mkdtemp(prefix="hn_s210_pyc_")
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    proc = subprocess.run([sys.executable, os.path.abspath(__file__), "--mutant", case_id],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=120, env=env, cwd=REPO)
    out = (proc.stdout or "") + (proc.stderr or "")
    for line in out.strip().splitlines():
        print("    " + line)
    if "GUARD_BROKEN" in out:
        return "GUARD_BROKEN"
    if "GUARD_HELD" in out:
        return "GUARD_HELD"
    return "PROBE_ERROR"


def section_mutations():
    section("D. 变异自证（临时改源码 → 同一判据必须变红 → try/finally 恢复）")
    with open(SERVER, "rb") as f:
        original = f.read()
    digest0 = hashlib.sha256(original).hexdigest()
    text0 = original.decode("utf-8")
    # 仓库里 stats_server.py 是 CRLF：锚点用 LF 归一化副本匹配，写回时还原成原来的换行风格
    # （否则「恢复」会把整份文件的换行改掉，git diff 变成全文件重写）。
    crlf = "\r\n" in text0
    base = text0.replace("\r\n", "\n")

    def _emit(text):
        return (text.replace("\n", "\r\n") if crlf else text).encode("utf-8")

    try:
        for case in MUTATIONS:
            missing = [old for old, _new in case["pairs"] if old not in base]
            chk("[变异] %s：锚点在源码里存在" % case["id"], not missing, missing or "")
            mutated = base
            for old, new in case["pairs"]:
                mutated = mutated.replace(old, new, 1)
            try:
                with open(SERVER, "wb") as f:
                    f.write(_emit(mutated))
                marker = run_child(case["id"])
            finally:
                with open(SERVER, "wb") as f:                     # 逐条恢复，异常也恢复
                    f.write(original)
            chk("[变异] %s（%s）⇒ 判据必须变红" % (case["id"], case["desc"]),
                marker == "GUARD_BROKEN", marker)
    finally:
        with open(SERVER, "wb") as f:
            f.write(original)
    with open(SERVER, "rb") as f:
        digest1 = hashlib.sha256(f.read()).hexdigest()
    chk("变异自证结束后源码已逐字节恢复（工作区没被弄脏）", digest0 == digest1,
        (digest0[:12], digest1[:12]))


def child_main(case_id):
    """子进程：跑该变异对应的判据。全通过 = GUARD_HELD(0)；有失败 = GUARD_BROKEN(1)。"""
    fails = []

    def c(name, cond, detail=""):
        print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                              ("  -- %s" % (detail,)) if detail != "" else ""))
        if not cond:
            fails.append(name)

    srv = cloud = None
    try:
        src = read_text(SERVER)
        if case_id == "hmac_compare":
            c("④ 令牌比较必须走 hmac.compare_digest", static_constant_time(src))
        elif case_id in ("concurrency_acquire", "concurrency_cap"):
            c("⑧ 并发上限仍有效", static_concurrency(src))
        elif case_id == "host_whitelist":
            srv, cloud = boot("mut_host")
            C = Client(srv.port)
            c("② evil Host 必须 403",
              C.send("GET", "/api/health", headers=tok(srv), host="evil.example").status == 403)
        elif case_id == "nonce_one_shot":
            srv, cloud = boot("mut_nonce")
            C = Client(srv.port)
            n = srv.issue_nonce()
            st1 = C.send("GET", "/settlement.html?nonce=%s" % n).status
            st2 = C.send("GET", "/settlement.html?nonce=%s" % n).status
            c("① nonce 首用 200 / 重放 403", st1 == 200 and st2 == 403, (st1, st2))
        elif case_id == "proxy_whitelist":
            srv, cloud = boot("mut_proxy")
            C = Client(srv.port)
            cloud.take()
            st = C.send("GET", "/api/proxy/unknown/path", headers=tok(srv)).status
            hits = cloud.take()
            c("⑤ 未知代理路径必须 4xx 且零上游请求", 400 <= st < 500 and not hits, (st, len(hits)))
        elif case_id == "cors_echo_all":
            srv, cloud = boot("mut_cors")
            C = Client(srv.port)
            r = C.send("GET", "/api/health", headers=dict(tok(srv), Origin="https://evil.example"))
            c("③ 外部 origin 不得回 ACAO", not r.has("access-control-allow-origin"),
              r.get("access-control-allow-origin"))
        elif case_id == "security_headers":
            srv, cloud = boot("mut_hdr")
            C = Client(srv.port)
            r = C.send("GET", "/api/health", headers=tok(srv))
            c("⑦ 必须带 X-Frame-Options: DENY", r.get("x-frame-options") == "DENY",
              r.get("x-frame-options"))
        elif case_id == "outbound_header":
            srv, cloud = boot("mut_out")
            C = Client(srv.port)
            cloud.take()
            C.send("GET", "/api/proxy/admin/statistics/months", headers=tok(srv))
            hits = cloud.take()
            hdr = hits[0]["headers"].get("X-Printer-Token") if hits else None
            c("⑥ 上游必须收到 X-Printer-Token", hdr == "test-token", hdr)
        else:
            print("  [FAIL] 未知变异 id: %s" % case_id)
            fails.append(case_id)
    except Exception as e:                                           # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("PROBE_ERROR %s: %s" % (type(e).__name__, e))
        return 2
    finally:
        if srv is not None:
            try:
                srv.stop()
            except Exception:                                        # noqa: BLE001
                pass
        if cloud is not None:
            cloud.stop()
        shutil.rmtree(TMP, ignore_errors=True)
    if fails:
        print("GUARD_BROKEN (%d 条判据变红)" % len(fails))
        return 1
    print("GUARD_HELD")
    return 0


# ═══════════════════════════════════════════════════════════════════════════

def main():
    print("S2-10 本地 HTTP 服务加固收尾验收（LT-11）")
    print("  python : %s" % sys.version.split()[0])
    print("  server : %s" % SERVER)
    print("  temp   : %s" % TMP)
    try:
        section_static()
        section_behavior()
        section_whitelist()
        section_mutations()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n" + "=" * 74)
    if _FAILS:
        print("S2-10 本地服务加固验收：存在失败 ❌  （PASS %d / %d）"
              % (_TOTAL[0] - len(_FAILS), _TOTAL[0]))
        for name in _FAILS[:20]:
            print("  · " + name)
        return 1
    print("S2-10 本地服务加固验收：全部通过 ✅  （PASS %d / %d）" % (_TOTAL[0], _TOTAL[0]))
    return 0


if __name__ == "__main__":
    if "--mutant" in sys.argv:
        sys.exit(child_main(sys.argv[sys.argv.index("--mutant") + 1]))
    sys.exit(main())
