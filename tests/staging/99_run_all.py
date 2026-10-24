"""99 · 一键复跑 DDoS/容量评估全系列（00 → 08）

用途：S2-12 的验收命令。逐个子进程跑，任一脚本退出码非 0 就整体失败；
最后把每个脚本的结论行与结果 JSON 路径打成一张清单。

前置：系统 Python311 + `PYTHONPATH=` 置空 + `PYTHONUTF8=1`（见仓库根 CLAUDE.md）。
所有实验只操作 `%TEMP%\\ddos_audit_copy` 隔离副本，**不碰生产**。

用法：
  PYTHONPATH= PYTHONUTF8=1 "C:/.../python.exe" "tests/staging/99_run_all.py"
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import COPY, PY, RESULTS, SCRIPTS, child_env, save_result

ORDER = [
    ("00_make_copy.py", "建隔离副本（假 config、清空 DB/uploads/logs）"),
    ("01_threads_per_ws.py", "WS 线程成本 + 握手门（S0-02）"),
    ("02_pool_saturation.py", "16 线程池饱和边界（认证连接）"),
    ("03_login_block.py", "登录上游阻塞 × sem=4（S0-03）"),
    ("04_proxyfix_ratelimit.py", "ProxyFix + 分桶（S0-03）"),
    ("05_rate_limits_memory.py", "_rate_limits 有界 + 清扫（S1-08）"),
    ("06_input_cost.py", "单请求输入成本与 1MB/深嵌套边界（S0-04）"),
    ("07_longpolling.py", "long-polling 的线程成本"),
    ("08_staging_tiers.py", "三档设备规模容量复核（3/6/10 台，§11.2）"),
    ("09_gevent_runtime.py", "S3-01 运行时对比：gevent 48 条连接 vs gthread 16 线程"),
]


def main():
    os.makedirs(RESULTS, exist_ok=True)
    print(f"[RUNNER] 副本={COPY}  python={PY}")
    rows = []
    t_all = time.time()
    for name, desc in ORDER:
        path = os.path.join(SCRIPTS, name)
        if not os.path.isfile(path):
            print(f"[FAIL] 缺少脚本 {name}")
            rows.append({"script": name, "desc": desc, "rc": None, "seconds": 0})
            continue
        t0 = time.time()
        print(f"\n{'=' * 70}\n[RUN] {name} — {desc}\n{'=' * 70}")
        p = subprocess.run([PY, "-u", path], cwd=SCRIPTS, env=child_env(),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        secs = time.time() - t0
        tail = [ln for ln in (p.stdout or "").splitlines()
                if ln.startswith("[CONCLUSION]") or ln.startswith("[SEED]")
                or ln.startswith("[S3-01") or ln.startswith("  ") and "⇒" in ln]
        print("\n".join((p.stdout or "").splitlines()[-18:]))
        if p.returncode != 0:
            print(f"[STDERR]\n{(p.stderr or '')[-1500:]}")
        rows.append({"script": name, "desc": desc, "rc": p.returncode,
                     "seconds": round(secs, 1),
                     "conclusion": [" ".join(x.strip().split()) for x in tail][:8]})
        print(f"[DONE] {name} rc={p.returncode} {secs:.1f}s")

    bad = [r for r in rows if r["rc"] != 0]
    payload = {"experiment": "DDoS/容量评估全系列复跑（S2-12）",
               "total_seconds": round(time.time() - t_all, 1),
               "scripts": rows, "failed": [r["script"] for r in bad]}
    save_result("99_run_all", payload)

    print("\n" + "=" * 70)
    print("复跑清单")
    print("=" * 70)
    for r in rows:
        mark = "PASS" if r["rc"] == 0 else f"FAIL(rc={r['rc']})"
        print(f"  [{mark}] {r['script']:<26} {r['seconds']:>5.1f}s  {r['desc']}")
    print(f"\n总耗时 {payload['total_seconds']}s；失败 {len(bad)} 个")
    if bad:
        print("失败脚本：" + ", ".join(r["script"] for r in bad))
        sys.exit(1)


if __name__ == "__main__":
    main()
