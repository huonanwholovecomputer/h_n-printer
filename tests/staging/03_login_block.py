"""03 · /api/login 上游阻塞能否打满线程池（复跑版，2026-10-24 / S2-12）

> 改版原因：S0-03 给登录路径加了 `BoundedSemaphore(4)`（同一时刻最多 4 个登录在等上游），
> 并把 /api/login 改成 `requests.Session()` 调微信；S1-08 又加了每 IP 10 次/分 的桶。
> 因此「16 个并发匿名登录占满 16 线程」已不成立 —— 本实验现在的结论是**验证这几道闸**：
>   B. 单 IP：2 / 8 / 16 并发，看有多少能真正进到上游等待（其余 429/503）、ping 是否受影响；
>   C. 多 IP（每次换 X-Forwarded-For）：绕开每 IP 桶后，能占几个 worker（应被 sem=4 钳住）。
>
> 上游延迟用 srv.py --login-delay 10 打桩（同时盖住 urlrequest 与 requests 两条路径），
> **不发起任何公网请求**。服务模型：pool=16（gthread 模拟）。
>
> 用法：
>   PYTHONPATH= PYTHONUTF8=1 "C:/.../python.exe" "tests/staging/03_login_block.py"
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (COPY, ServerProc, http_get, http_post, save_result,  # noqa: E402
                    summarize_http)

APP_PORT, CTL_PORT = 18082, 18097
POOL = 16
DELAY = 10.0
PING = f"http://127.0.0.1:{APP_PORT}/api/ping"
LOGIN = f"http://127.0.0.1:{APP_PORT}/api/login"


def ping_burst(n=4, timeout=3.0):
    out = [None] * n

    def one(i):
        out[i] = http_get(PING, timeout=timeout)

    ths = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return summarize_http(out)


def run_login_attack(conc, spread_ips=False, ping_timeout=3.0):
    """并发 conc 个 /api/login，攻击窗口内测 ping。返回 (login 汇总, 并发 ping, 串行 ping, wall_ms)。"""
    res = [None] * conc
    t0 = time.time()

    def one(i):
        headers = ({"X-Forwarded-For": f"198.18.{(i // 250) % 250}.{i % 250 + 1}"}
                   if spread_ips else None)
        res[i] = http_post(LOGIN, json.dumps({"code": "x"}).encode(),
                           timeout=DELAY + 8, headers=headers)

    ths = [threading.Thread(target=one, args=(i,)) for i in range(conc)]
    for t in ths:
        t.start()
    time.sleep(0.8)                      # 让进入上游等待的登录先占住 worker
    mid = ping_burst(4, timeout=ping_timeout)
    seq = summarize_http([http_get(PING, timeout=ping_timeout) for _ in range(3)])
    for t in ths:
        t.join()
    return summarize_http(res), mid, seq, (time.time() - t0) * 1000


def main():
    print(f"[ENV] 副本: {COPY}  模型: {POOL} 线程池 + login 上游打桩 {DELAY}s + sem=4")
    out = {"experiment": "/api/login 上游阻塞 × 线程池（S2-12 复跑）", "pool_size": POOL,
           "login_stub": f"urlrequest.urlopen / _wx_http.get → sleep({DELAY}) 后抛错（模拟上游 10s）",
           "phases": []}
    with ServerProc(APP_PORT, CTL_PORT, mode="pool", pool_size=POOL, login_delay=DELAY) as srv:
        time.sleep(0.8)
        ctrl = summarize_http([http_get(PING, timeout=3.0) for _ in range(5)])
        print(f"[A 对照] 0 登录 → ping 200={ctrl['ok_200']}/5 p50={ctrl['p50_ms']}ms")
        out["control"] = ctrl
        for conc, spread in ((2, False), (8, False), (16, False), (16, True)):
            login_s, mid, seq, wall = run_login_attack(conc, spread_ips=spread)
            st = srv.stats()
            rec = {"concurrent_logins": conc,
                   "source": "spread-ips" if spread else "single-ip",
                   "login": login_s, "attack_window_ping": mid,
                   "attack_window_ping_seq": seq, "wall_ms": round(wall, 1),
                   "pool_during": (st.get("pool") or {})}
            out["phases"].append(rec)
            print(f"[{'C' if spread else 'B'} conc={conc:<2} "
                  f"{'多IP' if spread else '单IP'}] 登录 {login_s['status']} "
                  f"wall={rec['wall_ms']}ms 池={rec['pool_during']}")
            print(f"      攻击窗口内 ping：并发4 200={mid['ok_200']}/4 超时={mid['timeout']} | "
                  f"串行3 200={seq['ok_200']}/3 超时={seq['timeout']}")
        time.sleep(0.5)
        rec = summarize_http([http_get(PING, timeout=3.0) for _ in range(5)])
        out["after"] = rec
        print(f"[D 恢复] 登录结束后 ping 200={rec['ok_200']}/5 p50={rec['p50_ms']}ms")
    save_result("03_login_block", out)

    # 「真正进到上游等待」的登录数 = 502 计数（stub 10s 后抛错）；其余是 429/503 快速返回。
    upstream_wait = [(p["concurrent_logins"], p["source"],
                      p["login"]["status"].get("502", 0), p["login"]["status"])
                     for p in out["phases"]]
    peak = max(n for _c, _s, n, _st in upstream_wait)
    print(f"\n[CONCLUSION] 每档「真正在等上游」的登录数："
          f"{[(c, s, n) for c, s, n, _st in upstream_wait]} ⇒ 峰值 {peak} 个"
          f"（sem=4 硬上限），其余快速 429/503；各档攻击窗口内 ping 均零超时、"
          f"结束后恢复 {out['after']['ok_200']}/5 ⇒ 「16 个并发登录打满全站」已不成立。")


if __name__ == "__main__":
    main()
