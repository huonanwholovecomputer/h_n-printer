# -*- coding: utf-8 -*-
"""验收 · COS 安装包下载码分发体系（下载码 → 一次性令牌 → 预签名直链）

依据：用户 2026-11-22 的口径（m00297「那就按你推荐的」，方案 C）与
docs/部署与发版.md 的「COS 安装包下载码」章节。

要证明的三件事：
  A. 接线：路由/表/定时任务/只读签名真的在，且签名入口没有 method 参数
     （后端永远不该有往桶里写、删的能力）。
  B. 行为（真实 Flask test_client + 临时库副本）：
     内部码自动生成、凭码换令牌、令牌 302 到 COS、令牌过期/超重入被拒、
     错码/非法版本/设备数/次数/配额/总闸/未配置 全部按契约拒绝、
     已使用的码只作废不删除、内部码到期自动轮换且旧码有过渡期。
  C. 变异自证：把关键判据改回去 ⇒ 对应那条必须变红（证明是判据在拦，不是别的东西）。
     M1 令牌 TTL 改成负数 → 「令牌可重入」红
     M2 设备数判据短路      → 「第二台设备被拒」红
     M3 每日配额判据短路    → 「配额用尽」红
     M4 「已使用不可删」短路 → 「已使用的码不可删」红

用法：python other/tests/verify_download_codes.py   （exit 0 = 全部通过）
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()

FAIL = [0]


def chk(name, cond, detail=""):
    if not cond:
        FAIL[0] += 1
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                        ("  -- " + str(detail)) if (not cond and detail) else ""))
    return cond


# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
src = H.read_app_source()
code = H.strip_comments(src)

chk("路由：公开换令牌 /api/dl/grant", code.count('@app.route("/api/dl/grant"') == 1,
    code.count('@app.route("/api/dl/grant"'))
chk("路由：令牌取货 /dl/<path:token>", code.count('@app.route("/dl/<path:token>"') == 1,
    code.count('@app.route("/dl/<path:token>"'))
for _p in ("/api/admin/dl/codes", "/api/admin/dl/switch", "/api/admin/dl/grants"):
    chk("路由：超管 %s" % _p, _p in code)
chk("路由：设备端只读当前码 /api/printer/dl/code",
    '/api/printer/dl/code' in code and '@require_printer_scope("admin")' in code)

chk("建表：download_codes", "CREATE TABLE IF NOT EXISTS download_codes" in code)
chk("建表：download_grants", "CREATE TABLE IF NOT EXISTS download_grants" in code)
chk("建表：app_flags（总闸）", "CREATE TABLE IF NOT EXISTS app_flags" in code)

chk("定时任务：内部码轮换已注册",
    'scheduler.add_job(rotate_due_download_codes' in code)
chk("定时任务：轮换函数存在", "def rotate_due_download_codes(" in code)

_cos = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "mobile_apps", "printer-backend", "update_cos.py"),
    encoding="utf-8").read()
_sig = re.search(r"def presign_get_url\(([^)]*)\)", _cos)
chk("update_cos：签名函数存在", _sig is not None)
chk("update_cos：只签 GET（无 method 参数）",
    _sig is not None and "method" not in _sig.group(1), _sig.group(1) if _sig else "")
chk("update_cos：HttpString 以 get 开头", '"get\\n' in _cos or "get\\n" in _cos)
chk("update_cos：MAX_TTL 上限存在", "MAX_TTL" in _cos and "min(ttl_i, MAX_TTL)" in _cos)

# 令牌必须哈希入库（库被翻也拿不到可用的链接）
chk("令牌哈希入库", 'hashlib.sha256(token.encode("utf-8")).hexdigest()' in code)

# ============================================================
# 行为探针（子进程 + 临时库副本，绝不碰仓库里的 users.db）
# ============================================================
BODY = r'''
import hashlib
import re

B = backend
BAD = []

def chk(name, cond, detail=""):
    if not cond:
        BAD.append(name)
    print(("PASS " if cond else "FAIL ") + name +
          ("" if cond else "  -- " + str(detail)))

def grant(payload, clear=True):
    """大部分用例都清一次限速桶：这里考的是闸门判据，不是限速器本身。"""
    if clear:
        try:
            B._rate_limits.clear()
        except Exception:
            pass
    return http.post("/api/dl/grant", json=payload)

# —— 准备：假装 COS 已配置 + 造一个超管登录态 ——
B.SUPER_ADMIN_OPENID = "super-openid-x"
B.UPDATE_COS_SECRET_ID = "AKIDEXAMPLEKEYID0000000000000000"
B.UPDATE_COS_SECRET_KEY = "SECRETEXAMPLEKEY00000000000000000"
B.UPDATE_COS_BUCKET = "hn-printer-updates-1324480550"
B.UPDATE_COS_REGION = "ap-guangzhou"
B.UPDATE_DL_DAILY_QUOTA = 30
H = {"Authorization": "Bearer " + B._issue_login_token("super-openid-x")}
OTHER = {"Authorization": "Bearer " + B._issue_login_token("someone-else")}

conn = B.get_user_db()
tabs = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
conn.close()
chk("三张表已建", {"download_codes", "download_grants", "app_flags"} <= tabs, tabs)

# —— 鉴权边界 ——
r = http.get("/api/admin/dl/codes")
chk("超管接口：无令牌 401", r.status_code == 401, r.status_code)
r = http.get("/api/admin/dl/codes", headers=OTHER)
chk("超管接口：非超管 403", r.status_code == 403, r.status_code)

# —— 内部码自动生成 ——
r = http.get("/api/admin/dl/codes", headers=H)
j = r.get_json() or {}
cur = j.get("current") or {}
chk("GET codes 200", r.status_code == 200 and j.get("success"), j)
chk("内部码已自动创建", bool(cur.get("cdkey")), cur)
chk("码形如 XXXX-XXXX", bool(re.match(r"^[A-Z0-9]{4}-[A-Z0-9]{4}$", cur.get("code") or "")),
    cur.get("code"))
chk("内部码默认 30 天后过期", 29.0 <= (cur.get("days_left") or 0) <= 31.0, cur.get("days_left"))
r2 = http.get("/api/admin/dl/codes", headers=H)
chk("再查一次不重复建码", (r2.get_json() or {}).get("current", {}).get("cdkey") == cur.get("cdkey"),
    (r2.get_json() or {}).get("current"))
CDKEY = cur.get("cdkey") or ""
CID = cur.get("id")

# —— 凭码换令牌 ——
r = grant({"code": CDKEY, "version": "4.7.0", "hwid": "HW-AAA"})
j = r.get_json() or {}
chk("凭码换令牌 200", r.status_code == 200 and j.get("success"), j)
url = j.get("url") or ""
chk("返回 /dl/ 链接", "/dl/" in url, url)
chk("链接是 https + hn-space.cn", url.startswith("https://hn-space.cn/dl/"), url)
TOKEN = url.rsplit("/dl/", 1)[-1]

conn = B.get_user_db()
row = conn.execute("SELECT * FROM download_grants WHERE token_hash = ?",
                   (hashlib.sha256(TOKEN.encode("utf-8")).hexdigest(),)).fetchone()
plain = conn.execute("SELECT 1 FROM download_grants WHERE token_hash = ?", (TOKEN,)).fetchone()
conn.close()
chk("令牌按 sha256 入库", row is not None and plain is None, (row is not None, plain is not None))
chk("令牌记录带 hwid", (row["hwid"] if row else "") == "HW-AAA", row["hwid"] if row else None)

# —— 令牌换直链 ——
r = http.get("/dl/" + TOKEN)
loc = r.headers.get("Location") or ""
chk("/dl 302 到 COS", r.status_code == 302 and "q-signature=" in loc,
    (r.status_code, loc[:100]))
chk("直链指向本版本对象", "h_n-printer_setup_4.7.0.exe" in loc, loc[:160])
chk("直链是 v5 只读签名", "q-sign-algorithm=sha1" in loc and "q-header-list=host" in loc,
    loc[:160])
chk("直链不缓存（no-store）", "no-store" in (r.headers.get("Cache-Control") or ""),
    r.headers.get("Cache-Control"))

# 同一个令牌允许多次 GET（浏览器多连接/断点续传），但有上限
ok = 0
for _ in range(11):
    if http.get("/dl/" + TOKEN).status_code == 302:
        ok += 1
chk("令牌可重入（多连接下载不被掐）", ok == 11, ok)
r = http.get("/dl/" + TOKEN)
chk("超过重入上限 429", r.status_code == 429, r.status_code)

# 令牌过期
conn = B.get_user_db()
conn.execute("UPDATE download_grants SET expires_at = '2000-01-01 00:00:00' WHERE token_hash = ?",
             (hashlib.sha256(TOKEN.encode("utf-8")).hexdigest(),))
conn.commit()
conn.close()
r = http.get("/dl/" + TOKEN)
chk("过期令牌 410", r.status_code == 410, r.status_code)
r = http.get("/dl/not-a-real-token")
chk("无效令牌 404", r.status_code == 404, r.status_code)

# —— 错误码契约 ——
r = grant({"code": "ZZZZ-ZZZZ", "version": "4.7.0", "hwid": "x"})
chk("错码 400 invalid_code",
    r.status_code == 400 and (r.get_json() or {}).get("code") == "invalid_code", r.get_json())
r = grant({"code": "ABC", "version": "4.7.0", "hwid": "x"})
chk("码长度不对 400 invalid_code", r.status_code == 400, r.status_code)
r = grant({"code": CDKEY, "version": "../../etc/passwd", "hwid": "x"})
chk("非法版本 400 bad_version",
    r.status_code == 400 and (r.get_json() or {}).get("code") == "bad_version", r.get_json())
r = grant({"code": CDKEY, "version": "4.7.0"})
chk("缺 hwid 但无限设备时也能过", r.status_code == 200, r.get_json())

# —— 临时码：设备数 / 次数 ——
r = http.post("/api/admin/dl/codes", headers=H,
              json={"family": "temp", "hours": 24, "max_uses": 5, "max_devices": 1,
                    "note": "验收-设备数"})
j = r.get_json() or {}
TCODE = j.get("cdkey") or ""
TID = (j.get("item") or {}).get("id")
chk("建临时码 200", r.status_code == 200 and bool(TCODE), j)
chk("临时码默认 24 小时后过期",
    0.9 <= ((j.get("item") or {}).get("days_left") or 0) <= 1.01,
    (j.get("item") or {}).get("days_left"))

r = grant({"code": TCODE, "version": "4.7.0", "hwid": "DEV-1"})
chk("临时码首台设备可用", r.status_code == 200, r.get_json())
r = grant({"code": TCODE, "version": "4.7.0", "hwid": "DEV-2"})
chk("第二台设备被拒 403 device_limit",
    r.status_code == 403 and (r.get_json() or {}).get("code") == "device_limit", r.get_json())
r = grant({"code": TCODE, "version": "4.7.0", "hwid": "DEV-1"})
chk("同一设备可重复取令牌", r.status_code == 200, r.get_json())
r = grant({"code": TCODE, "version": "4.7.0", "hwid": ""})
chk("限设备的码缺 hwid 400 device_required",
    r.status_code == 400 and (r.get_json() or {}).get("code") == "device_required", r.get_json())

r = http.post("/api/admin/dl/codes", headers=H,
              json={"family": "temp", "hours": 1, "max_uses": 1, "max_devices": 0})
ONE = (r.get_json() or {}).get("cdkey") or ""
grant({"code": ONE, "version": "4.7.0", "hwid": "A1"})
r = grant({"code": ONE, "version": "4.7.0", "hwid": "A2"})
chk("次数用完 403 code_exhausted",
    r.status_code == 403 and (r.get_json() or {}).get("code") == "code_exhausted", r.get_json())

# —— 删除规则：用过的只作废不删除 ——
r = http.delete("/api/admin/dl/codes/%d" % TID, headers=H)
chk("已使用的码不可删 400", r.status_code == 400, r.get_json())
r = http.post("/api/admin/dl/codes", headers=H,
              json={"family": "temp", "hours": 2, "max_uses": 1, "max_devices": 1})
FRESH_ID = ((r.get_json() or {}).get("item") or {}).get("id")
r = http.delete("/api/admin/dl/codes/%d" % FRESH_ID, headers=H)
chk("未使用的码可删 200", r.status_code == 200, r.get_json())
r = http.delete("/api/admin/dl/codes/%d" % CID, headers=H)
chk("内部码不可删 400", r.status_code == 400, r.get_json())

# —— 内部码更换：新码生效，旧码有过渡期 ——
r = http.post("/api/admin/dl/codes/%d/rotate" % CID, headers=H)
j = r.get_json() or {}
NEWCD = j.get("cdkey") or ""
chk("立即更换 200 且码不同", r.status_code == 200 and NEWCD and NEWCD != CDKEY, j)
r = grant({"code": CDKEY, "version": "4.7.0", "hwid": "HW-OLD"})
chk("旧码在过渡期内仍可用", r.status_code == 200, r.get_json())
conn = B.get_user_db()
old = conn.execute("SELECT * FROM download_codes WHERE id = ?", (CID,)).fetchone()
conn.close()
chk("旧码指向新码", old is not None and old["superseded_by"] == (j.get("item") or {}).get("id"),
    old["superseded_by"] if old else None)
chk("旧码过渡期约 24 小时",
    old is not None and 23.0 <= (B._dl_parse(old["expires_at"]) - B._dl_now()).total_seconds() / 3600.0 <= 24.1,
    old["expires_at"] if old else None)

# —— 到期自动轮换（定时任务本体） ——
conn = B.get_user_db()
conn.execute("UPDATE download_codes SET expires_at = '2000-01-01 00:00:00'"
             " WHERE family = 'internal' AND status = 'active' AND superseded_by IS NULL")
conn.commit()
conn.close()
B.rotate_due_download_codes()
conn = B.get_user_db()
live = conn.execute("SELECT * FROM download_codes WHERE family = 'internal' AND status = 'active'"
                    " AND superseded_by IS NULL").fetchall()
conn.close()
chk("到期后自动轮换出唯一当前码", len(live) == 1, len(live))
chk("轮换出的码是新码", bool(live) and live[0]["code"] != NEWCD, live[0]["code"] if live else None)
chk("新当前码有效期回到 30 天",
    bool(live) and 29.0 <= (B._dl_parse(live[0]["expires_at"]) - B._dl_now()).total_seconds() / 86400.0 <= 30.1,
    live[0]["expires_at"] if live else None)
CDKEY2 = live[0]["code"] if live else ""
CID2 = live[0]["id"] if live else 0

# —— 作废 ——
r = http.post("/api/admin/dl/codes/%d/revoke" % CID2, headers=H)
chk("作废 200", r.status_code == 200, r.get_json())
r = grant({"code": CDKEY2, "version": "4.7.0", "hwid": "HW-X"})
chk("作废码 403 code_revoked",
    r.status_code == 403 and (r.get_json() or {}).get("code") == "code_revoked", r.get_json())
r = http.get("/api/admin/dl/codes", headers=H)
chk("作废后立刻补出新当前码", bool(((r.get_json() or {}).get("current") or {}).get("cdkey")))

# —— 总闸 ——
CDKEY3 = ((r.get_json() or {}).get("current") or {}).get("cdkey") or ""
r = http.post("/api/admin/dl/switch", headers=H, json={"enabled": False})
chk("关总闸 200", r.status_code == 200, r.get_json())
r = grant({"code": CDKEY3, "version": "4.7.0", "hwid": "HW-S"})
chk("总闸关闭 403 channel_closed",
    r.status_code == 403 and (r.get_json() or {}).get("code") == "channel_closed", r.get_json())
http.post("/api/admin/dl/switch", headers=H, json={"enabled": True})

# —— 每日配额 ——
conn = B.get_user_db()
used = B._dl_quota_used(conn)
conn.close()
B.UPDATE_DL_DAILY_QUOTA = max(1, used)
r = grant({"code": CDKEY3, "version": "4.7.0", "hwid": "HW-Q"})
chk("配额用尽 429 daily_quota",
    r.status_code == 429 and (r.get_json() or {}).get("code") == "daily_quota", r.get_json())
B.UPDATE_DL_DAILY_QUOTA = 30

# —— 未配置 COS：503 且给出 GitHub 回退地址 ——
B.UPDATE_COS_SECRET_ID = ""
r = grant({"code": CDKEY3, "version": "4.7.0", "hwid": "HW-C"})
j = r.get_json() or {}
chk("未配置 COS 503 cos_not_configured",
    r.status_code == 503 and j.get("code") == "cos_not_configured", j)
chk("503 带 GitHub 回退地址", "github.com" in (j.get("github_url") or ""), j.get("github_url"))
B.UPDATE_COS_SECRET_ID = "AKIDEXAMPLEKEYID0000000000000000"

# —— 限速器真的在（单 IP 20 次/10 分钟） ——
B.UPDATE_DL_DAILY_QUOTA = 9999
try:
    B._rate_limits.clear()
except Exception:
    pass
codes = []
for _ in range(24):
    rr = http.post("/api/dl/grant", json={"code": CDKEY3, "version": "4.7.0", "hwid": "HW-R"})
    codes.append(rr.status_code)
chk("单 IP 20 次后限速 429", codes.count(429) >= 1 and codes.count(200) <= 20,
    (codes.count(200), codes.count(429)))
B.UPDATE_DL_DAILY_QUOTA = 30

# —— 设备端只读当前码 ——
r = http.get("/api/printer/dl/code?token=probe-tok")
j = r.get_json() or {}
chk("设备端可读当前码", r.status_code == 200 and bool(j.get("cdkey")), j)
chk("设备端返回的是当前有效码", (j.get("cdkey") or "") == CDKEY3, (j.get("cdkey"), CDKEY3))
r = http.get("/api/printer/dl/code")
chk("设备端无令牌 403", r.status_code == 403, r.status_code)

print("BAD=%d" % len(BAD))
for b in BAD:
    print("BADNAME=" + b)
'''

print("=== B. 行为 ===")
tmp, dst = H.make_copy("hn_dl_")
p = H.run_probe(dst, BODY, timeout=180)
out = p.stdout or ""
sys.stdout.write(out if out.endswith("\n") else out + "\n")
if p.returncode != 0:
    print("--- probe stderr ---")
    print((p.stderr or "")[-3000:])
bad = H.probe_value(p, "BAD") if hasattr(H, "probe_value") else None
if bad is None:
    import re as _re
    m = _re.search(r"^BAD=(\d+)$", out, _re.M)
    bad = m.group(1) if m else None
chk("行为探针全部通过", p.returncode == 0 and bad == "0", "BAD=%s rc=%s" % (bad, p.returncode))

# ============================================================
# C. 变异自证
# ============================================================
print("=== C. 变异自证 ===")
MUTATIONS = [
    ("M1 令牌 TTL 改负", ("_DL_TOKEN_TTL = 600", "_DL_TOKEN_TTL = -5"), "令牌可重入"),
    ("M2 设备数判据短路", ("        if max_dev:\n", "        if False:\n"), "第二台设备被拒"),
    ("M3 每日配额判据短路",
     ("if UPDATE_DL_DAILY_QUOTA > 0 and _dl_quota_used(conn) >= UPDATE_DL_DAILY_QUOTA:",
      "if False:"), "配额用尽"),
    ("M4 已使用不可删短路",
     ('        if int(row["used_count"] or 0) > 0:\n', "        if False:\n"),
     "已使用的码不可删"),
]
for label, repl, expect_fail in MUTATIONS:
    try:
        _tmp, _dst = H.mutated_copy([repl], prefix="hn_dl_mut_")
    except AssertionError as e:
        chk("%s 锚点命中" % label, False, e)
        continue
    mp = H.run_probe(_dst, BODY, timeout=180)
    mout = mp.stdout or ""
    hit = ("FAIL " + expect_fail) in mout
    chk("%s ⇒ 「%s」变红" % (label, expect_fail), hit,
        "rc=%s 未出现 FAIL" % mp.returncode)

# ============================================================
print()
if FAIL[0]:
    print("❌ 有 %d 条失败" % FAIL[0])
    sys.exit(1)
print("✅ 全部通过")
sys.exit(0)
