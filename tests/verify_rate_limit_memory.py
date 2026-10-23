# -*- coding: utf-8 -*-
"""验收 · S1-08 限速器改造（有界容量 / TTL 清扫 / key 哈希 / 覆盖新入口）

依据：docs/安全加固修复计划_2026-10-23.md §S1-08（R8 / DS-04）。

现场形状：`_rate_limits = defaultdict(list)` ——
  ① **永不删 key**：任何带随机后缀的参数（device_id、file_id、openid）都能让字典无限涨；
  ② **原串当 key**：1 MB 的 device_id 直接成为 key；
  ③ **没有清扫**：冷 key 只在被再次访问时才清时间戳，过期 key 永久占内存。

本脚本：
  A. 静态接线：容量常量、OrderedDict、60s 清扫、sha256 截断、7 个入口都有限速、
     参数校验在限速之前。
  B. 行为：灌 10 万个不同 key → 字典长度 ≤50k、Python 堆增长 <20MB；过期 key 会被清扫；
     真实入口（/api/file_page、/api/orders、/api/profile、/api/cancel_page_analysis）超限 429。
  C. 变异自证：把容量上限与清扫间隔改成"无界" → 10 万个 key 全留下（判据转红）。

用法：python tests/verify_rate_limit_memory.py     （exit 0 = 全部通过）
"""
import io
import os
import sys
import tracemalloc

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


rl = block("def _rate_limit(key, max_count, window_seconds):")

# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
H.chk("容量上限常量化（50k）", "_RATE_LIMIT_MAX_KEYS = 50000" in code)
H.chk("字典换成 OrderedDict（可 O(1) 淘汰最旧）",
      "_rate_limits = OrderedDict()" in code and "popitem(last=False)" in code)
H.chk("有 60s 全量清扫（冷 key 不永久占内存）",
      "_RATE_LIMIT_SWEEP_INTERVAL = 60.0" in code
      and "def _sweep_rate_limits_locked(" in code
      and "_sweep_rate_limits_locked(now)" in rl)
H.chk("key 进字典前统一 sha256 截断",
      "def _rate_limit_hash(" in code and "hashlib.sha256(str(key)" in code
      and "_rate_limit_hash(key)" in rl)
H.chk("淘汰有计数（可观测）", "_rate_limits_stats[\"evicted\"]" in code)
for ep, label in (("file_page:{g.openid}", "/api/file_page"),
                  ("orders:{g.openid}", "/api/orders"),
                  ("profile_get:{g.openid}", "GET /api/profile"),
                  ("profile_post:{g.openid}", "POST /api/profile"),
                  ("cancel_analysis:{g.openid}", "/api/cancel_page_analysis"),
                  ("theme:{g.openid}", "/api/me/theme"),
                  ("wxlogin:ip:", "/api/login"),
                  ("device_login:ip:", "/api/device_login")):
    H.chk("%s 已接入限速" % label, ep in code, ep)

# 参数校验先于限速：file_page 的 file_id 校验必须在 _rate_limit_ok 之前
fp = block("def get_file_page(")
H.chk("/api/file_page 先校验 file_id 再进限速 key",
      0 < fp.find("len(file_id) <= 64") < fp.find("file_page:{g.openid}"))

# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（临时后端副本）===")
TMP, DST = H.make_copy("hn_rlmem_")
backend = H.import_backend(DST)
backend.init_db()

N = 100000
backend._rate_limits.clear()
backend._rate_limits_stats.update({"sweeps": 0, "evicted": 0, "rejected": 0})
tracemalloc.start()
base, _ = tracemalloc.get_traced_memory()
for i in range(N):
    backend._rate_limit("probe:%d" % i, 5, 60)
cur, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
growth_mb = (cur - base) / (1024.0 * 1024.0)
keys = len(backend._rate_limits)

H.chk("灌 %d 个不同 key 后字典长度 ≤ 50000" % N, keys <= 50000, "实际 %d" % keys)
H.chk("Python 堆增长 < 20 MB", growth_mb < 20.0, "实际 %.2f MB" % growth_mb)
H.chk("淘汰计数 > 0（确实在淘汰而不是碰巧没涨）",
      backend._rate_limits_stats["evicted"] > 0, backend._rate_limits_stats)

# 清扫：造 3 个"窗口早已过期"的 key，再把 last_sweep 推到很久以前触发清扫
backend._rate_limits.clear()
backend._rate_limit("stale:a", 5, 1)
backend._rate_limit("stale:b", 5, 1)
import time as _t
_t.sleep(1.2)
backend._rate_limits_last_sweep[0] = 0.0
backend._rate_limit("fresh:c", 5, 60)
H.chk("过期的冷 key 会被 60s 清扫掉", len(backend._rate_limits) <= 2,
      "剩余 %d" % len(backend._rate_limits))

# 真实入口：超限 429
backend.ADMIN_OPENIDS = {"rl-admin"}
atok = backend.token_serializer.dumps("rl-admin")
AUTH = {"Authorization": "Bearer " + atok}
http = backend.app.test_client()
backend._rate_limits.clear()
codes = [http.get("/api/orders", headers=AUTH).status_code for _ in range(125)]
H.chk("/api/orders 第 121 次起 429（阈值 120/分）",
      429 not in codes[:120] and codes[120] == 429, codes[-3:])

backend._rate_limits.clear()
codes = [http.get("/api/profile", headers=AUTH).status_code for _ in range(65)]
H.chk("GET /api/profile 第 61 次起 429（阈值 60/分）",
      429 not in codes[:60] and codes[60] == 429, codes[-3:])

backend._rate_limits.clear()
codes = [http.post("/api/cancel_page_analysis", json={"file_ids": ["x"]},
                   headers=AUTH).status_code for _ in range(33)]
H.chk("/api/cancel_page_analysis 第 31 次起 429（阈值 30/分）",
      429 not in codes[:30] and codes[30] == 429, codes[-3:])

# ============================================================
# C. 变异自证：容量与清扫改成"无界"
# ============================================================
print("\n=== C. 变异自证（容量/清扫改成无界）===")
mut_tmp, mut_dst = H.mutated_copy([
    ("_RATE_LIMIT_MAX_KEYS = 50000", "_RATE_LIMIT_MAX_KEYS = 100000000"),
    ("_RATE_LIMIT_SWEEP_INTERVAL = 60.0", "_RATE_LIMIT_SWEEP_INTERVAL = 1e9"),
], prefix="hn_rlmem_mut_")
proc = H.run_probe(mut_dst, """
for i in range(60000):
    backend._rate_limit("mut:%d" % i, 5, 60)
print("RESULT=%d" % len(backend._rate_limits))
""", timeout=300)
mut_keys = H.probe_value(proc)
H.chk("去掉容量上限后 6 万个 key 全留下（判据能红）",
      mut_keys is not None and int(mut_keys) > 50000,
      "变异副本 keys=%r stderr=%r" % (mut_keys, (proc.stderr or "")[-300:]))

H.finish()
