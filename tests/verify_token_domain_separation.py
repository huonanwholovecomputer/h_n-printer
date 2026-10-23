# -*- coding: utf-8 -*-
"""验收 · S0-07 令牌域分离（登录 token ⇄ 下载 token 不可互换）

依据：docs/安全加固修复计划_2026-10-23.md §S0-07、DS-11、D-3（已接受一次全员重登）。

现场形状：两个 `URLSafeTimedSerializer` 用**同一个 key、同一个默认 salt** ⇒
`download_serializer.loads(login_token)` 能成功。于是任何一个令牌被拿到（例如被
nginx access.log 记录的下载 URL）就等于拿到另一个域的权限；反方向同样成立。

本脚本：
  A. 静态接线：两个 serializer 各自带独立 salt（`login-v2` / `dl-v1`），salt 不相等。
  B. 行为：正向各自可用；**两个方向互换都必须抛 BadSignature**。
  C. 变异自证：把两个 salt 改回相同 → 互换成功（证明断言绑在 salt 上）。

用法：python tests/verify_token_domain_separation.py     （exit 0 = 全部通过）
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()
code = H.strip_comments(H.read_app_source())

# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
import re

m_tok = re.search(r'token_serializer\s*=\s*URLSafeTimedSerializer\([^)]*salt\s*=\s*[\'"]([^\'"]+)', code)
m_dl = re.search(r'download_serializer\s*=\s*URLSafeTimedSerializer\([^)]*salt\s*=\s*[\'"]([^\'"]+)', code)
H.chk("登录 serializer 带独立 salt", bool(m_tok), m_tok.group(0) if m_tok else "未找到")
H.chk("下载 serializer 带独立 salt", bool(m_dl), m_dl.group(0) if m_dl else "未找到")
H.chk("两个 salt 不相等", bool(m_tok and m_dl) and m_tok.group(1) != m_dl.group(1),
      "%r vs %r" % (m_tok.group(1) if m_tok else None, m_dl.group(1) if m_dl else None))
H.chk("salt 是常量化字面量（不是从配置/环境读，避免两端漂移）",
      bool(m_tok and m_dl) and m_tok.group(1) in ("login-v2",) and m_dl.group(1) in ("dl-v1",))

# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（临时后端副本）===")
TMP, DST = H.make_copy("hn_salt_")
backend = H.import_backend(DST)
backend.init_db()

login_tok = backend.token_serializer.dumps("salt-openid")
dl_tok = backend.download_serializer.dumps("salt-openid")

H.chk("登录 token 能被登录 serializer 读回（正向可用）",
      backend.token_serializer.loads(login_tok) == "salt-openid")
H.chk("下载 token 能被下载 serializer 读回（正向可用）",
      backend.download_serializer.loads(dl_tok) == "salt-openid")

cross1 = cross2 = "未抛异常"
try:
    cross1 = backend.download_serializer.loads(login_tok)
except Exception as e:  # noqa: BLE001
    cross1 = type(e).__name__
H.chk("★ download.loads(登录 token) 必须抛异常（原实现成功 ⇒ 域之间可互相冒充）",
      cross1 not in ("salt-openid", "未抛异常") and "Signature" in str(cross1), cross1)

try:
    cross2 = backend.token_serializer.loads(dl_tok)
except Exception as e:  # noqa: BLE001
    cross2 = type(e).__name__
H.chk("★ token.loads(下载 token) 必须抛异常", cross2 not in ("salt-openid", "未抛异常")
      and "Signature" in str(cross2), cross2)

# 下载令牌仍然只用在下发 URL 的路由里（静态：download_serializer 的使用点）
users = [ln.strip() for ln in code.splitlines()
         if "download_serializer" in ln and not ln.lstrip().startswith("#")]
H.chk("download_serializer 只用于签发/校验下载 URL（使用点可枚举）", len(users) >= 2, users)

# ============================================================
# C. 变异自证：两个 salt 改回相同
# ============================================================
print("\n=== C. 变异自证（两个 salt 改成同一个）===")
mut_tmp, mut_dst = H.mutated_copy([
    ('salt="dl-v1"', 'salt="login-v2"'),
], prefix="hn_salt_mut_")
proc = H.run_probe(mut_dst, """
login_tok = backend.token_serializer.dumps("x")
try:
    v = backend.download_serializer.loads(login_tok)
    print("RESULT=OK(%s)" % v)
except Exception as e:
    print("RESULT=RAISED(%s)" % type(e).__name__)
""")
mut_val = H.probe_value(proc)
H.chk("salt 相同后两个方向又可互换（证明断言绑在 salt 上，不是恒绿的假守卫）",
      mut_val is not None and mut_val.startswith("OK"),
      "变异副本 %r stderr=%r" % (mut_val, (proc.stderr or "")[-300:]))

H.finish()
