# -*- coding: utf-8 -*-
"""验收脚本 · 设备密钥（S1-06）宽限期与轮换口径

**审计发现（2026-11-17）**：`_check_device_secret` 的老客户端轮换分支把
`issued_at_ts` 重置成 now —— 老客户端每次登录都把 14 天宽限往后推，
「宽限到期强制设备凭证」这件事永远不会发生（fail-closed 名存实亡）；
而客户端（android_app/www）至今**既没保存也没回传** device_secret，
所以一旦按注释把 DEVICE_SECRET_GRACE_SECONDS 改成 0，所有设备直接 403 锁死。

本脚本在临时目录复制 printer-backend 后导入 app.py（不碰真实 device_secrets.json），钉住：
  ① 首次见到 device_id → 下发密钥，记录含 issued_at_ts；
  ② 带正确密钥 → 通过且**不轮换**（secret_hash 不变）；
  ③ 带错误密钥 → 拒绝（message 含「设备凭证无效」）；
  ④ 老客户端（不带密钥）宽限期内 → 放行 + 轮换，但 **issued_at_ts 不得被重置**；
  ⑤ 老客户端超过宽限 → 拒绝（无论之前轮换过多少次）；
  ⑥ 网页端（android_app/www/app.js）确实会把 device_login 响应里的 device_secret
     存到本地、并在后续登录回传（静态接线断言）。

    python tests/verify_device_secret_grace.py     # exit 0 = 全绿
"""
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

_tmp = tempfile.mkdtemp(prefix="hn_devsecret_")
_dst = os.path.join(_tmp, "printer-backend")
shutil.copytree(SRC, _dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs", "device_secrets.json"))
sys.path.insert(0, _dst)
os.chdir(_dst)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


try:
    import app as backend
except Exception as e:
    print(f"\u2717 \u65e0\u6cd5\u5bfc\u5165 app.py: {e}")
    sys.exit(1)

SECRETS = os.path.join(_tmp, "device_secrets.json")
backend.DEVICE_SECRETS_FILE = SECRETS
GRACE = int(backend.DEVICE_SECRET_GRACE_SECONDS)


def key_of(device_id):
    return hashlib.sha256(("device_secret:" + device_id).encode("utf-8")).hexdigest()[:32]


def seed(device_id, issued_at, secret="orig-secret"):
    backend._save_device_secrets({key_of(device_id): {
        "secret_hash": backend._hash_device_secret(secret),
        "issued_at_ts": issued_at,
        "last_used_at": issued_at,
    }})


def rec(device_id):
    return backend._load_device_secrets().get(key_of(device_id)) or {}


print(f"宽限期 = {GRACE} 秒（{GRACE / 86400:.1f} 天）")
check("宽限期是有限值（>0 且 < 365 天）", 0 < GRACE < 365 * 86400, f"{GRACE}")

# ① 首次注册
os.path.exists(SECRETS) and os.remove(SECRETS)
ok, msg, issued = backend._check_device_secret("DEV-FIRST-1", "")
check("① 首次注册放行并下发密钥", ok and bool(issued) and bool(rec("DEV-FIRST-1").get("secret_hash")))
check("① 记录含 issued_at_ts", float(rec("DEV-FIRST-1").get("issued_at_ts") or 0) > 0)

# ② 正确密钥
before = rec("DEV-FIRST-1").get("secret_hash")
ok, msg, issued2 = backend._check_device_secret("DEV-FIRST-1", issued)
check("② 带正确密钥放行", ok, msg)
check("② 正确密钥不轮换（hash 不变、不下发新密钥）",
      rec("DEV-FIRST-1").get("secret_hash") == before and not issued2)

# ③ 错误密钥
ok, msg, _ = backend._check_device_secret("DEV-FIRST-1", "wrong-secret")
check("③ 错误密钥被拒", not ok and "设备凭证无效" in msg, msg)
check("③ 错误密钥不覆盖已存 hash", rec("DEV-FIRST-1").get("secret_hash") == before)

# ④ 老客户端在宽限期内：放行 + 轮换，但 issued_at 不重置
old_issued = time.time() - 3 * 86400
seed("DEV-LEGACY-1", old_issued)
ok, msg, issued3 = backend._check_device_secret("DEV-LEGACY-1", "")
r = rec("DEV-LEGACY-1")
check("④ 宽限期内老客户端放行并下发新密钥", ok and bool(issued3), msg)
check("④ 轮换后 issued_at_ts 保持首次签发时刻（不得重置为 now）",
      abs(float(r.get("issued_at_ts") or 0) - old_issued) < 2,
      f"issued_at={float(r.get('issued_at_ts') or 0):.0f} old={old_issued:.0f} now={time.time():.0f}")
check("④ 轮换留下 rotated_for_legacy 标记", bool(r.get("rotated_for_legacy")))

# ⑤ 超过宽限：拒绝（即使一直有轮换）
seed("DEV-STALE-1", time.time() - (GRACE + 3600))
ok, msg, issued4 = backend._check_device_secret("DEV-STALE-1", "")
check("⑤ 超过宽限期的老客户端被拒", not ok and not issued4, msg)

# ⑤b 连轮换多次也不会把宽限续命
seed("DEV-STALE-2", time.time() - (GRACE - 600))
for _ in range(3):
    backend._check_device_secret("DEV-STALE-2", "")
r2 = rec("DEV-STALE-2")
check("⑤b 多次轮换后 issued_at_ts 仍是最初时刻", abs(float(r2.get("issued_at_ts") or 0) - (time.time() - (GRACE - 600))) < 5,
      f"issued_at={float(r2.get('issued_at_ts') or 0):.0f}")

# ⑥ 网页端接线
appjs = open(os.path.join(REPO, "mobile_apps", "android_app", "www", "app.js"),
             encoding="utf-8").read()
check("⑥ 网页端会保存 device_secret",
      re.search(r"DEVICE_SECRET_KEY\s*=\s*['\"]hn_device_secret", appjs) is not None
      and "setItem(DEVICE_SECRET_KEY" in appjs)
check("⑥ 网页端 device_login 会回传 device_secret",
      re.search(r"device_login[\s\S]{0,600}device_secret\s*:", appjs) is not None)
check("⑥ 403 时会清掉本地密钥再试（自愈）", "clearDeviceSecret" in appjs)

print()
print("设备密钥宽限期：" + ("全部通过" if ok_all else "有失败项"))
sys.exit(0 if ok_all else 1)
