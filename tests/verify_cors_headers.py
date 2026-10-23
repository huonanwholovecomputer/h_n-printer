# -*- coding: utf-8 -*-
"""验收 · S2-04 接口收敛 + CORS 收窄 + 安全响应头（服务端）

依据：docs/安全加固修复计划_2026-10-23.md §S2-04（DS-12/DS-13、R4）。

现场形状（修复前）：
  · `/api/printer_status` 与 `/api/claiming_devices` 是**公开**接口，却直接下发设备所有者的
    **真实姓名**（「姚懿祥的设备 | DESKTOP-…」）—— 谁都能扫到店里员工的姓名 + 机器标识；
  · `CORS(origins="*")` —— 任何网页都能以访客浏览器身份打 `/api/*` 并读响应；
  · 无任何安全响应头（nosniff / XFO / CSP / Referrer-Policy / HSTS）。

本脚本：
  A. 静态：CORS 白名单（不含 "*"、含 Capacitor 与本地开发源）、姓名脱敏接线、
     安全响应头中间件、nginx 生产配置（脱敏入库的那份）里加固项仍在、**没有** http2。
  B. 行为（真 Flask）：
     · 响应头齐全；HSTS 只在 https（X-Forwarded-Proto: https）下出现；
     · CORS：白名单源回显 ACAO、陌生源（含预检）不返回 ACAO；
     · 脱敏：公开接口返回「姚**」，管理接口（printer token）仍返回真名。
  C. 变异自证：把 `_mask_person_name` 改成原样返回 ⇒ 公开接口泄漏真名（判据能红）。

用法：python tests/verify_cors_headers.py     （exit 0 = 全部通过）
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NGINX = os.path.join(REPO, "mobile_apps", "printer-backend", "nginx-production-hn-space.conf")
NGINX_HARD = os.path.join(REPO, "mobile_apps", "printer-backend",
                          "nginx-conf-d-00-security-hardening.conf")
src = H.read_app_source()
code = H.strip_comments(src)
nginx = open(NGINX, encoding="utf-8").read() if os.path.exists(NGINX) else ""
nginx_hard = open(NGINX_HARD, encoding="utf-8").read() if os.path.exists(NGINX_HARD) else ""


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


cors_block = block("CORS_ALLOWED_ORIGINS =")
mask_fn = block("def _mask_person_name(")
status_fn = block("def printer_status(")
claim_fn = block("def claiming_devices(")

# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
H.chk('★ CORS 不再是 "*"（有白名单常量）', 'origins": "*"' not in code
      and "CORS_ALLOWED_ORIGINS" in code and 'origins": CORS_ALLOWED_ORIGINS' in code)
H.chk("白名单含正式站点 + Capacitor WebView + 本地开发源",
      "https://hn-space.cn" in cors_block and "https://localhost" in cors_block
      and "capacitor://localhost" in cors_block and "127" in cors_block, cors_block[:200])
H.chk("SocketIO 那层保持不收窄（工具端不发 Origin，收窄只会掉线）",
      "cors_allowed_origins=\"*\"" in code)

H.chk("公开接口脱敏：printer_status 的 active_owner_name",
      "_mask_person_name(" in status_fn)
H.chk("公开接口脱敏：claiming_devices 的 owner_name 与 label 都脱敏",
      "_mask_person_name(dev.get(\"owner_name\"" in claim_fn
      and "device_display_label(cid, mask=True)" in claim_fn)
H.chk("脱敏只保留首字（3 字名 ⇒ 1 字 + 2 个 *）", "max(1, len(name) - 1)" in mask_fn)
H.chk("管理接口不受影响（/api/printer/devices 仍回真名）",
      '"owner_name": entry.get("owner_name", "") or ""' in code)

H.chk("安全响应头中间件存在且覆盖 nosniff/XFO/CSP/Referrer/Permissions",
      "@app.after_request" in code and "_apply_security_headers" in code
      and "X-Content-Type-Options" in code and "X-Frame-Options" in code
      and "Content-Security-Policy" in code and "Referrer-Policy" in code
      and "Permissions-Policy" in code)
H.chk("HSTS 只在 https 下加（request.is_secure 闸门）",
      "request.is_secure" in code and "Strict-Transport-Security" in code)

# nginx 生产配置（脱敏入库的那份）—— 加固项不能静默回退
H.chk("nginx 生产配置已入库（脱敏）", bool(nginx) and "hn-space.cn" in nginx)
H.chk("★ nginx **没有**开 http2（报告结论：刻意不开）",
      all("listen 443 ssl http2" not in ln for ln in nginx.splitlines()))
H.chk("加固项仍在：hn_sio / hn_login / hn_conn / burst=150 / body 2m / 50m",
      "hn_sio" in nginx and "hn_login" in nginx and "limit_conn_zone" in nginx
      and "burst=150" in nginx and "client_max_body_size 2m" in nginx
      and "client_max_body_size 50m" in nginx)
H.chk("慢连接超时与 429 状态码在 conf.d 文件里",
      "client_header_timeout 10s" in nginx_hard and "limit_req_status 429" in nginx_hard
      and "limit_conn_status 429" in nginx_hard)

# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（真 Flask）===")
TMP, DST = H.make_copy("hn_hdr_")
backend = H.import_backend(DST)
backend.init_db()
backend.PRINTER_TOKEN = "hdr-tok"
http = backend.app.test_client()

r = http.get("/api/ping")
hd = {k.lower(): v for k, v in r.headers.items()}
H.chk("响应带 nosniff", hd.get("x-content-type-options") == "nosniff", hd.get("x-content-type-options"))
H.chk("响应带 X-Frame-Options: DENY", hd.get("x-frame-options") == "DENY")
H.chk("响应带 Referrer-Policy: no-referrer", hd.get("referrer-policy") == "no-referrer")
H.chk("响应带 CSP（default-src 'none' + frame-ancestors none）",
      "default-src 'none'" in (hd.get("content-security-policy") or "")
      and "frame-ancestors 'none'" in (hd.get("content-security-policy") or ""))
H.chk("http 下**不**加 HSTS", "strict-transport-security" not in hd)
r = http.get("/api/ping", headers={"X-Forwarded-Proto": "https"})
H.chk("https（X-Forwarded-Proto）下加 HSTS",
      "strict-transport-security" in {k.lower(): v for k, v in r.headers.items()})

r = http.get("/api/ping", headers={"Origin": "https://hn-space.cn"})
H.chk("白名单源 ⇒ 回显 ACAO",
      r.headers.get("Access-Control-Allow-Origin") == "https://hn-space.cn",
      r.headers.get("Access-Control-Allow-Origin"))
r = http.get("/api/ping", headers={"Origin": "https://localhost"})
H.chk("Capacitor WebView 源 ⇒ 回显 ACAO",
      r.headers.get("Access-Control-Allow-Origin") == "https://localhost",
      r.headers.get("Access-Control-Allow-Origin"))
r = http.get("/api/ping", headers={"Origin": "https://evil.example"})
H.chk("★ 陌生源 ⇒ **不**返回 ACAO", r.headers.get("Access-Control-Allow-Origin") is None,
      r.headers.get("Access-Control-Allow-Origin"))
r = http.options("/api/ping", headers={"Origin": "https://evil.example",
                                       "Access-Control-Request-Method": "GET"})
H.chk("★ 陌生源预检 ⇒ 不返回 ACAO", r.headers.get("Access-Control-Allow-Origin") is None,
      r.headers.get("Access-Control-Allow-Origin"))

# 脱敏：造一台绑定所有者的接单设备 + 心跳（让 printer_status 认为它在线）
backend.save_devices({"dev1": {"device_name": "DESKTOP-TEST", "owner_name": "姚懿祥"}})
backend.save_claim({"claiming_devices": {"dev1": {"owner_name": "姚懿祥",
                                                  "claimed_at": "2026-10-23 22:00:00"}}})
backend.register_device("dev1", "DESKTOP-TEST")
backend.touch_printer_heartbeat("dev1")   # heartbeat 存的是 datetime，别手工塞 float

j = http.get("/api/claiming_devices").get_json() or {}
dev = (j.get("devices") or [{}])[0]
H.chk("★ /api/claiming_devices 的 owner_name 脱敏（姚懿祥 → 姚**）",
      dev.get("owner_name") == "姚**", dev.get("owner_name"))
H.chk("★ label 也脱敏（不含完整姓名）",
      "姚**" in (dev.get("label") or "") and "姚懿祥" not in (dev.get("label") or ""),
      dev.get("label"))
j2 = http.get("/api/printer_status").get_json() or {}
H.chk("★ /api/printer_status 的 active_owner_name 脱敏",
      j2.get("active_owner_name") == "姚**", j2.get("active_owner_name"))

j3 = http.get("/api/printer/devices", headers={"X-Printer-Token": "hdr-tok"}).get_json() or {}
names = [d.get("owner_name") for d in (j3.get("devices") or [])]
H.chk("管理接口仍回真名（绑定/对账不受影响）", "姚懿祥" in names, names)

# ============================================================
# C. 变异自证：脱敏改成原样返回
# ============================================================
print("\n=== C. 变异自证（关掉脱敏）===")
mut_tmp, mut_dst = H.mutated_copy([
    ('    return name[0] + "*" * max(1, len(name) - 1)\n',
     '    return name\n'),
], prefix="hn_hdr_mut_")
proc = H.run_probe(mut_dst, r"""
backend.PRINTER_TOKEN = "probe-tok"
backend.save_devices({"dev1": {"device_name": "DESKTOP-TEST", "owner_name": "姚懿祥"}})
backend.save_claim({"claiming_devices": {"dev1": {"owner_name": "姚懿祥", "claimed_at": "x"}}})
j = http.get("/api/claiming_devices").get_json() or {}
d = (j.get("devices") or [{}])[0]
print("OWNER=%s" % d.get("owner_name"))
""")
mut_owner = H.probe_value(proc, "OWNER")
H.chk("★ 变异（脱敏改成原样返回）⇒ 公开接口泄漏真名（判据能红）",
      mut_owner == "姚懿祥", mut_owner)

H.finish()
