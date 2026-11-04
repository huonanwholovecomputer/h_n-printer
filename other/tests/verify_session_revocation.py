# -*- coding: utf-8 -*-
"""验收 · S1-06a 会话可吊销（token 带 jti/ver + users.token_version + 60s 缓存）

依据：docs/安全加固修复计划_2026-10-23.md §S1-06（DS-11）。

现场形状：登录 token 就是 `dumps(openid)` —— 一旦签发，**7 天内无法吊销**：改密码、
怀疑泄露、"退出全部设备"都做不到（服务端没有会话表，也没法让某个 token 失效）。

本脚本：
  A. 静态接线：payload 带 openid/ver/jti、users.token_version 迁移、60s 版本缓存、
     "退出全部设备"入口、旧形态（纯字符串 payload）兼容。
  B. 行为：签发 → 可用；logout_all → 旧 token 立即 401；重新签发 → 可用；
     旧形态 token（ver=0）在 bump 前可用、bump 后 401；缓存被立即失效（不靠 60s 自然过期）。
  C. 变异自证：去掉 ver 校验 → "退出全部设备"后旧 token 仍然 200（判据转红）。

用法：python tests/verify_session_revocation.py     （exit 0 = 全部通过）
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


# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
H.chk("token_version 落独立表（guest 账号在 users 表里没有行）",
      "CREATE TABLE IF NOT EXISTS token_versions" in code
      and "ON CONFLICT(openid) DO UPDATE SET version = version + 1" in code)
H.chk("token payload 带 openid / ver / jti",
      'def _issue_login_token(' in code and '"o": openid' in code
      and '"v": _token_version_of(openid)' in code and '"j": uuid.uuid4().hex' in code)
H.chk("校验侧比对 ver（不一致即吊销）",
      "if ver != _token_version_of(openid):" in code and "raise TokenRevoked(" in code)
H.chk("版本缓存 60s（避免每请求打库）",
      "TOKEN_VERSION_CACHE_TTL = 60" in code and "_token_version_cache" in code)
H.chk("logout_all 递增版本并立即失效缓存",
      '@app.route("/api/logout_all"' in code and "def _bump_token_version(" in code
      and "_token_version_cache[openid] = (new_ver, time.time())" in code)
H.chk("两个鉴权装饰器都走 _load_login_token（不再直接 loads）",
      block("def login_required(").count("_load_login_token(token)") == 1
      and block("def require_printer_access(").count("_load_login_token(token)") == 1)
H.chk("旧形态（纯字符串 payload）按 ver=0 兼容",
      "if isinstance(payload, str):" in code and "openid, ver = payload, 0" in code)

# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（临时后端副本）===")
TMP, DST = H.make_copy("hn_sess_")
backend = H.import_backend(DST)
backend.init_db()
backend.ADMIN_OPENIDS = {"sess-admin"}
http = backend.app.test_client()


def bearer(tok):
    return {"Authorization": "Bearer " + tok}


tok = backend._issue_login_token("sess-admin")
H.chk("新签发的 token 可用（/api/me 200）",
      http.get("/api/me", headers=bearer(tok)).status_code == 200)

legacy = backend.token_serializer.dumps("sess-admin")
H.chk("旧形态 token（纯字符串 payload）bump 前仍可用（存量登录态不被打断）",
      http.get("/api/me", headers=bearer(legacy)).status_code == 200)

r = http.post("/api/logout_all", headers=bearer(tok))
H.chk("退出全部设备 200", r.status_code == 200, r.get_json())
H.chk("返回新 token_version", (r.get_json() or {}).get("token_version") == 1, r.get_json())
H.chk("★ 旧 token 立即 401（不需要等 60s 缓存过期）",
      http.get("/api/me", headers=bearer(tok)).status_code == 401)
H.chk("★ 旧形态 token 也立即 401（ver=0 ≠ 当前 1）",
      http.get("/api/me", headers=bearer(legacy)).status_code == 401)

new_tok = backend._issue_login_token("sess-admin")
H.chk("重新签发后可用（新 ver=1）",
      http.get("/api/me", headers=bearer(new_tok)).status_code == 200)

H.chk("第二次 logout_all 让版本继续递增",
      (http.post("/api/logout_all", headers=bearer(new_tok)).get_json() or {}
       ).get("token_version") == 2)
H.chk("上一个新 token 也随之失效",
      http.get("/api/me", headers=bearer(new_tok)).status_code == 401)

bad = tok[:-3] + ("aaa" if not tok.endswith("aaa") else "bbb")
H.chk("篡改签名 → 401（不是 500）",
      http.get("/api/me", headers=bearer(bad)).status_code == 401)
H.chk("无 token → 401", http.get("/api/me").status_code == 401)

# ============================================================
# C. 变异自证：去掉 ver 校验
# ============================================================
print("\n=== C. 变异自证（去掉 ver 校验）===")
mut_tmp, mut_dst = H.mutated_copy([
    ("    if ver != _token_version_of(openid):\n        raise TokenRevoked(openid)",
     "    if False:\n        raise TokenRevoked(openid)"),
], prefix="hn_sess_mut_")
proc = H.run_probe(mut_dst, """
backend.ADMIN_OPENIDS = {"sess-admin"}
tok = backend._issue_login_token("sess-admin")
h = {"Authorization": "Bearer " + tok}
http.post("/api/logout_all", headers=h)
print("RESULT=%s" % http.get("/api/me", headers=h).status_code)
""")
mut_status = H.probe_value(proc)
H.chk("去掉 ver 校验后，退出全部设备后旧 token 仍然 200（判据能红）",
      mut_status == "200", "变异副本 status=%r stderr=%r"
      % (mut_status, (proc.stderr or "")[-300:]))

H.finish()
