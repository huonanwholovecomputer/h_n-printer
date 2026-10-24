"""09 · S3-01 运行时对比：gevent（一条连接一个 greenlet）vs threading/gthread（一条连接一个 worker）

背景（S2-12 实测）：threading + gthread threads=16 时，**15 条**认证 WS 就把 16 个 worker 占满、
HTTP 全线超时（`08_staging_tiers.py` 的 6 台档只剩 1 个 worker）。gevent 下一条连接只占一个
greenlet，不再占 OS 线程，因此 24/32/48 条连接时 HTTP 仍然可用。

本实验（隔离副本，**不碰生产**）：
  A. gevent 模式（`srv.py --mode gevent`，app.py 导入时 monkey-patch）：连接数 16 → 24 → 32 → 48，
     每档量：串行 ping p50/p95、鉴权 `/api/orders`（1 万单）、并发写（10 个 device_login）、
     写并发期间的 ping、greenlet 数 / OS 线程数 / RSS；并在 32 条连接时跑一次
     `POST /job`（真实定时任务 `process_pending_orders`）验证 APScheduler 代码路径没被 greenlet 打坏。
  B. pool 对照（gthread 模拟，16 线程）：24 条连接 ⇒ 期望 HTTP 超时（证明判据有区分度）。

用法（先跑 00 建副本；99_run_all.py 会自动按顺序跑）：
  PYTHONPATH= PYTHONUTF8=1 "C:/.../python.exe" "tests/staging/09_gevent_runtime.py"
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (COPY, PRINTER_TOKEN, ServerProc, db_bytes, http_get,  # noqa: E402
                    http_post, open_eio_ws, save_result, seed_bench, summarize_http)

GEV_PORT, GEV_CTL = 18087, 18092
POOL_PORT, POOL_CTL = 18088, 18091
TIERS = [16, 24, 32, 48]
POOL_SIZE = 16


def ping(base, n=10, timeout=3):
    return summarize_http([http_get(base + "/api/ping", timeout=timeout) for _ in range(n)])


def orders(base, token, n=10, timeout=5):
    return summarize_http([
        http_get(base + "/api/orders?page=%d&per_page=20" % (i + 1), timeout=timeout,
                 headers={"Authorization": "Bearer " + token})
        for i in range(n)])


def concurrent_writes(base, n=10, timeout=6):
    """n 个并发 device_login（写 SQLite + 设备注册）。gevent 下这些写会在 greenlet 里串行完成：
    既要确认都成功，也要量出「写并发期间的 ping」—— SQLite 的 C 调用不受 monkey-patch 影响，
    一次等锁会阻塞整个事件循环，这是 gevent 方案唯一的实质风险点。"""
    out = [None] * n

    def one(i):
        out[i] = http_post(
            base + "/api/device_login",
            json.dumps({"device_id": "gev-load-%03d" % i}).encode(), timeout=timeout,
            headers={"X-Forwarded-For": "198.20.0.%d" % (i + 1)})

    t0 = time.time()
    ths = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in ths:
        t.start()
    during = [http_get(base + "/api/ping", timeout=timeout) for _ in range(5)]
    for t in ths:
        t.join()
    return summarize_http(out), summarize_http(during), round((time.time() - t0) * 1000, 1)


def post_job(ctl_port):
    import urllib.request as ur
    try:
        req = ur.Request("http://127.0.0.1:%d/job" % ctl_port, data=b"{}", method="POST")
        with ur.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)[:120]}


def main():
    print(f"[ENV] 副本={COPY}")
    token, seed_secs = seed_bench(10000, "bench-user-0001")
    disk = db_bytes()
    print(f"[SEED] 1 万单 + 1 万子任务，{seed_secs:.1f}s，磁盘 {disk['total']}B")

    out = {"experiment": "S3-01 gevent vs gthread：连接数 → HTTP 可用性",
           "tiers": TIERS, "pool_size": POOL_SIZE, "seed_seconds": round(seed_secs, 1),
           "disk_after_seed": disk, "gevent": [], "pool_control": None}

    # ---------------- A. gevent ----------------
    base = "http://127.0.0.1:%d" % GEV_PORT
    conns = []
    with ServerProc(GEV_PORT, GEV_CTL, mode="gevent") as srv:
        time.sleep(1.0)
        st0 = srv.stats()
        assert st0.get("runtime") == "gevent", st0
        print(f"[BASE] runtime={st0['runtime']} greenlets={st0.get('greenlets')} "
              f"threads={st0['active_count']} rss={st0['rss_kb']}KB")
        for target in TIERS:
            while len(conns) < target:
                conns.append(open_eio_ws(GEV_PORT, token=PRINTER_TOKEN, timeout=8.0))
            time.sleep(0.5)
            st = srv.stats()
            p = ping(base)
            o = orders(base, token)
            w, w_ping, w_ms = concurrent_writes(base)
            job = post_job(GEV_CTL) if target == 32 else None
            row = {
                "connections": target,
                "greenlets": st.get("greenlets"),
                "os_threads": st["active_count"],
                "rss_kb": st["rss_kb"],
                "ping": p, "orders": o, "writes": w,
                "ping_during_writes": w_ping, "writes_wall_ms": w_ms,
                "scheduler_job": job,
            }
            out["gevent"].append(row)
            print(f"[gevent N={target:<2}] ping 200={p['ok_200']}/10 p95={p['p95_ms']}ms | "
                  f"orders 200={o['ok_200']}/10 p95={o['p95_ms']}ms | "
                  f"写 200={w['ok_200']}/10 wall={w_ms}ms 写期间 ping 200={w_ping['ok_200']}/5 "
                  f"| greenlets={row['greenlets']} threads={row['os_threads']} rss={row['rss_kb']}KB"
                  + (f" | job={job}" if job else ""))
        for ws in conns:
            try:
                ws.close()
            except Exception:
                pass
        conns = []

    # ---------------- B. pool 对照（16 线程 + 24 条连接）----------------
    pbase = "http://127.0.0.1:%d" % POOL_PORT
    pconns, upgraded, fails = [], 0, {}
    with ServerProc(POOL_PORT, POOL_CTL, mode="pool", pool_size=POOL_SIZE) as srv:
        time.sleep(0.8)
        for _ in range(24):
            try:
                pconns.append(open_eio_ws(POOL_PORT, token=PRINTER_TOKEN, timeout=3.0))
                upgraded += 1
            except Exception as e:  # noqa: BLE001
                fails[type(e).__name__] = fails.get(type(e).__name__, 0) + 1
        time.sleep(0.5)
        st = srv.stats()
        p = ping(pbase, n=5, timeout=2)
        out["pool_control"] = {
            "target": 24, "upgraded": upgraded, "failures": fails,
            "pool": st.get("pool"), "ping": p,
        }
        print(f"[pool N=24] 升级 {upgraded}/24 失败={fails} 池={st.get('pool')} ⇒ "
              f"ping 200={p['ok_200']}/5 超时={p['timeout']}")
        for ws in pconns:
            try:
                ws.close()
            except Exception:
                pass

    save_result("09_gevent_runtime", out)

    # ---------------- 结论 ----------------
    g = out["gevent"]
    worst = g[-1]
    all_ok = all(r["ping"]["ok_200"] == 10 and r["orders"]["ok_200"] == 10
                 and r["writes"]["ok_200"] == 10 for r in g)
    pool = out["pool_control"]
    print("\n" + "=" * 70)
    print("S3-01 运行时对比结论")
    print("=" * 70)
    for r in g:
        print(f"  gevent {r['connections']:>2} 条：ping p95={r['ping']['p95_ms']}ms | "
              f"orders p95={r['orders']['p95_ms']}ms | 写 p95={r['writes']['p95_ms']}ms | "
              f"greenlets={r['greenlets']} OS线程={r['os_threads']}")
    print(f"  gthread 16 线程 + 24 条：ping 200={pool['ping']['ok_200']}/5、"
          f"超时 {pool['ping']['timeout']}（连接升级 {pool['upgraded']}/24）")
    print(f"\n[CONCLUSION] gevent 下 {worst['connections']} 条连接时 HTTP 仍全部 200"
          f"（ping p95={worst['ping']['p95_ms']}ms、orders p95={worst['orders']['p95_ms']}ms、"
          f"写 {worst['writes']['ok_200']}/10 成功），OS 线程仅 {worst['os_threads']} 条；"
          f" 对照 gthread 16 线程在 24 条连接下 HTTP 超时 {pool['ping']['timeout']}/5。"
          f"\n             全档判据 {'全部通过' if all_ok else '存在不达标档位'}；"
          f"定时任务 process_pending_orders 在 gevent 下 "
          f"{'成功' if (g[2]['scheduler_job'] or {}).get('ok') else '失败'}"
          f"（{(g[2]['scheduler_job'] or {}).get('ms')}ms）。")
    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
