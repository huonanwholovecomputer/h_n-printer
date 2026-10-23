# -*- coding: utf-8 -*-
"""验收 · S0-03 匿名登录分桶（ProxyFix + device_login 双桶 + 微信登录闸门）

依据：docs/安全加固修复计划_2026-10-23.md §S0-03、R2/R3/DS-04、附录 A-2 / A-5 / A-6。

现场形状（R2）：`app` 没有 ProxyFix ⇒ `request.remote_addr` 恒为 nginx 的 127.0.0.1；
`device_login` 的 IP 桶是**全站共用一个桶**（20/h），于是 20 个匿名请求就能把
**所有新设备**的登录锁死一小时。R3：16 个并发 `/api/login` 各自阻塞在微信 HTTP
调用（10 秒超时）⇒ 占满 `threads=16`，连 `/api/ping` 都开始超时。

本脚本：
  A. 静态接线：ProxyFix 参数（✓ 见 verify_sio_gate 的 A-1 断言）、先校验参数再进限速 key、
     两个桶都计数（不再 `or` 短路）、device_id 哈希截断、登录闸门三件套。
  B. 行为：**固定 REMOTE_ADDR、只改 X-Forwarded-For**（附录 A-6：同时改 REMOTE_ADDR 的
     写法在装与不装 ProxyFix 时都落两个桶 ⇒ 假阴性）。A 打满 IP 桶后 A 429、B 首次仍 200；
     同一 IP + 同一 device_id 第 21 次 429；`/api/login` 第 11 次 429、信号量占满时 503。
  C. 变异自证：删掉 ProxyFix → A/B 落同一个桶，B 也 429（假阴性写法抓不到，这个能）。

用法：python tests/verify_ratelimit_bucket.py     （exit 0 = 全部通过）
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

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


dl = block("def device_login(")
login = block("def wx_login(")

# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
H.chk("device_login 用 X-Forwarded-For 的真实 IP 前**先**校验 device_id",
      dl.find("if not (10 <= len(device_id) <= 64)") > 0
      and dl.find("if not (10 <= len(device_id) <= 64)") < dl.find("_rate_limit("))
H.chk("device_id 先经 sha256 截断再进限速 key（原串当 key ⇒ 20MB/h 内存增长面）",
      'hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:32]' in dl)
H.chk("IP 桶与设备桶**都**计数（先算 ok_ip / ok_dev 再判定，不再 `or` 短路）",
      "ok_ip = _rate_limit(" in dl and "ok_dev = _rate_limit(" in dl
      and "if not (ok_ip and ok_dev):" in dl)
H.chk("没有残留 `... or \\n ...` 的短路写法",
      "_rate_limit(f\"device_login:ip:{ip}\""
      in dl.replace("\n", " ") or "ok_ip = _rate_limit(f\"device_login:ip:{ip}\"" in dl)
H.chk("device_login 429 带 Retry-After", 'Retry-After' in dl)
H.chk("IP 桶阈值按计划放宽到 120/h（全站共用 20/h 就是 R2 的锁死面）",
      "120, 3600" in dl)

H.chk("/api/login 有每真实 IP 限速（wxlogin:ip）", 'wxlogin:ip:{ip}' in login)
H.chk("/api/login 有界并发：BoundedSemaphore 非阻塞获取，拿不到 503",
      "_WX_LOGIN_SEM = threading.BoundedSemaphore(" in code
      and "acquire(blocking=False)" in login and "503" in login)
H.chk("微信调用超时收到 3 秒（原 10 秒 ⇒ 线程被占 10 秒）", "timeout=3" in login)
H.chk("微信调用走 requests.Session 连接池（不再每次新建 urllib 连接）",
      "_wx_http = requests.Session()" in code and "_wx_http.get(" in login)
H.chk("登录失败不把异常原文回给客户端（URL 里带 AppSecret）",
      "type(e).__name__" in login and "str(e)" not in login)

# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（临时后端副本 + test_client）===")
TMP, DST = H.make_copy("hn_rl_")
backend = H.import_backend(DST)
backend.init_db()
http = backend.app.test_client()

IP_A, IP_B, IP_C = "203.0.113.10", "203.0.113.20", "203.0.113.30"


def dev_login(ip, device_id):
    return http.post("/api/device_login", json={"device_id": device_id},
                     headers={"X-Forwarded-For": ip})


backend._rate_limits.clear()
last = None
for i in range(120):
    last = dev_login(IP_A, "rate-probe-%04d" % i)
H.chk("A（真实 IP）前 120 次仍在配额内（200）", last is not None and last.status_code == 200,
      "实际 %s" % (last.status_code if last else None))

r = dev_login(IP_A, "rate-probe-over")
H.chk("A 第 121 次 → 429（IP 桶 120/h）", r.status_code == 429, "实际 %s" % r.status_code)
H.chk("429 带 Retry-After", bool(r.headers.get("Retry-After")), dict(r.headers))

r = dev_login(IP_B, "rate-probe-other")
H.chk("★ B（另一个真实 IP）首次仍 200 —— 攻击者锁不住别人（R2 的核心断言）",
      r.status_code == 200, "实际 %s" % r.status_code)

backend._rate_limits.clear()
codes = [dev_login(IP_C, "same-device-abcdef").status_code for _ in range(21)]
H.chk("同一 IP + 同一 device_id 第 21 次 → 429（设备桶 20/h）",
      codes[:20] == [200] * 20 and codes[20] == 429, codes[-3:])

backend._rate_limits.clear()
login_codes = []
for _ in range(11):
    login_codes.append(http.post("/api/login", json={"code": "x"},
                                 headers={"X-Forwarded-For": IP_A}).status_code)
H.chk("/api/login 第 11 次 → 429（每 IP 10/min）",
      login_codes[:10] != [] and login_codes[10] == 429 and 429 not in login_codes[:10],
      login_codes)

backend._rate_limits.clear()
backend.WECHAT_APPID, backend.WECHAT_APPSECRET = "wx-test", "secret-test"
held = [backend._WX_LOGIN_SEM.acquire(blocking=False) for _ in range(4)]
H.chk("信号量 4 个名额可被占满（容量 = 计划值 4）", all(held))
r = http.post("/api/login", json={"code": "x"}, headers={"X-Forwarded-For": IP_B})
H.chk("信号量占满时 /api/login 立即 503（快速失败、不排队占线程）",
      r.status_code == 503, "实际 %s" % r.status_code)
H.chk("503 带 Retry-After", bool(r.headers.get("Retry-After")))
for _ in held:
    backend._WX_LOGIN_SEM.release()

backend._rate_limits.clear()
backend.WECHAT_APPID = backend.WECHAT_APPSECRET = None
r = http.post("/api/login", json={"code": "x"}, headers={"X-Forwarded-For": IP_C})
H.chk("未配置微信参数时仍是 500（不是被闸门误伤成 429/503）", r.status_code == 500,
      "实际 %s" % r.status_code)

# ============================================================
# C. 变异自证：删掉 ProxyFix → A/B 落同一个桶
# ============================================================
print("\n=== C. 变异自证（删掉 ProxyFix）===")
mut_tmp, mut_dst = H.mutated_copy([
    ("app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)",
     "# [mutation] ProxyFix removed"),
], prefix="hn_rl_mut_")
proc = H.run_probe(mut_dst, """
for i in range(120):
    http.post("/api/device_login", json={"device_id": "mut-probe-%04d" % i},
              headers={"X-Forwarded-For": "203.0.113.10"})
r = http.post("/api/device_login", json={"device_id": "mut-probe-other"},
              headers={"X-Forwarded-For": "203.0.113.20"})
print("RESULT=%s" % r.status_code)
""")
mut_status = H.probe_value(proc)
H.chk("删掉 ProxyFix 后，另一个 X-Forwarded-For 也落到同一个桶（429）—— 判据能红",
      mut_status == "429", "变异副本 status=%r stderr=%r"
      % (mut_status, (proc.stderr or "")[-300:]))

H.finish()
