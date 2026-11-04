"""05 · _rate_limits 内存上限与过期清理（复跑版：验证 S1-08 的修复）

> 修复前（2026-10-23 审计）：`_rate_limits` 是普通 dict、**没有任何清理路径** ⇒
> 每个新 key 永久驻留（实测 ~1MB/万 key），长期运行会缓慢吃掉内存。
> 修复后（S1-08）：key 先 `sha256[:16]` 再入表（原串不进内存）、容量上限 50k、
> 每 60s 全量清扫一次（窗口内已无有效时间戳的 key 直接删掉）。
>
> 本实验（真代码、进程内直接调 `app._rate_limit`，与 HTTP 路径同一函数）验证：
>   1) 灌 20 万个 key 后：字典长度 ≤ 50k（有界）、RSS 增长 < 20MB、且存的都是 16 位哈希；
>   2) 过期 key 会被清扫掉（修复前「过期不释放键」）；
>   3) 打印 app.py 里 _rate_limits 的引用行作为静态证据。
>
> 用法：
>   PYTHONPATH= PYTHONUTF8=1 "C:/.../python.exe" "tests/staging/05_rate_limits_memory.py"
"""
import gc
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import COPY, rss_kb, save_result  # noqa: E402

os.chdir(COPY)
sys.path.insert(0, COPY)
import app as backend  # noqa: E402

N = 100_000


def rss_settled():
    gc.collect()
    time.sleep(0.2)
    gc.collect()
    return rss_kb()


def main():
    out = {"experiment": "_rate_limits 有界容量 + 过期清理（S2-12 复跑）", "n_per_batch": N,
           "max_keys": backend._RATE_LIMIT_MAX_KEYS, "copy": COPY}
    backend._rate_limits.clear()
    base = rss_settled()

    # ---- 1) 两批共 20 万个 key ----
    t0 = time.perf_counter()
    for i in range(N):
        backend._rate_limit(f"x:{i}", 1, 3600)
    dt = (time.perf_counter() - t0) * 1000
    after_short = rss_settled()
    t0 = time.perf_counter()
    for i in range(N):
        backend._rate_limit(f"device_login:dev:device-{i:040d}", 1, 3600)
    dt2 = (time.perf_counter() - t0) * 1000
    after_real = rss_settled()

    keys = len(backend._rate_limits)
    sample = list(backend._rate_limits)[:3]
    all_hashed = all(len(k) == 16 and all(c in "0123456789abcdef" for c in k)
                     for k in backend._rate_limits)
    d_short = after_short - base
    d_total = after_real - base
    print(f"[1] 灌 {N * 2} 个 key（{dt:.0f}ms + {dt2:.0f}ms）；字典长度 {keys}"
          f"（上限 {backend._RATE_LIMIT_MAX_KEYS}）；RSS {base}KB → {after_real}KB"
          f"（+{d_total}KB，第一批 +{d_short}KB）")
    print(f"[1] 键样本 {sample}；全部为 16 位哈希={all_hashed}")
    assert keys <= backend._RATE_LIMIT_MAX_KEYS, keys
    assert d_total < 20 * 1024, d_total
    assert all_hashed, sample

    # ---- 2) 过期 key 会被清扫 ----
    backend._rate_limits.clear()
    backend._rate_limit("__audit_expired__", 5, 0.05)
    k = backend._rate_limit_hash("__audit_expired__")
    n_before = len(backend._rate_limits)
    time.sleep(0.3)
    backend._rate_limits_last_sweep[0] = 0.0     # 强制下一次调用触发全量清扫
    backend._rate_limit("__audit_keepalive__", 5, 3600)
    still = k in backend._rate_limits
    n_after = len(backend._rate_limits)
    print(f"[2] 过期键 {k}：清扫前键数 {n_before} → 清扫后 {n_after}；过期键仍存在={still}")
    assert not still, "过期 key 未被清扫（修复应删除它）"

    # ---- 3) 静态证据 ----
    src = open(os.path.join(COPY, "app.py"), encoding="utf-8").read().splitlines()
    hits = [(i + 1, line.strip()) for i, line in enumerate(src) if "_rate_limits" in line]
    print("[3] app.py 中 _rate_limits 的引用：")
    for ln, line in hits:
        print(f"    app.py:{ln}: {line}")

    out.update({
        "rss_base_kb": base,
        "rss_after_200k_kb": after_real,
        "delta_total_kb": d_total,
        "keys_total": keys,
        "all_keys_hashed": all_hashed,
        "key_sample": sample,
        "expired_key_removed": (not still),
        "sweep_before_after": [n_before, n_after],
        "insert_ms_100k": round(dt, 1),
        "insert_ms_100k_real": round(dt2, 1),
        "grep_lines": [f"app.py:{ln}: {line}" for ln, line in hits],
    })
    save_result("05_rate_limits_memory", out)
    print(f"\n[CONCLUSION] 20 万个不同 key 后字典仍为 {keys} 个（≤{backend._RATE_LIMIT_MAX_KEYS}）、"
          f"RSS 仅 +{d_total}KB；过期 key 被 60s 全量清扫删掉 ⇒ 修复前「无清理、键永久驻留」"
          f"的内存增长面已关闭。")


if __name__ == "__main__":
    main()
