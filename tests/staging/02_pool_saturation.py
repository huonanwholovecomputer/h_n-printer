"""02 · 线程池饱和证明：N 条**认证** Engine.IO WebSocket → /api/ping 队列化/超时

> 2026-10-24（S2-12 复跑）改版：未认证握手已被 S0-02 的 token 门 403（见 01 的 A 段），
> 攻击者拿不到连接。本实验改用**生产设备形态**（带 printer token 的长连接）——
> 它衡量的是「真实设备规模 × gthread 线程池」的容量边界，而不是攻击面。
> 结论对 §11.2 的 required_threads = ceil(在线设备×2.5)+4 直接可用。

服务模型：srv.py --mode pool --pool-size 16
  = socketserver.TCPServer + ThreadPoolExecutor(16) 包裹 Flask-SocketIO WSGI app（backend.app），
    语义对齐 gunicorn gthread（Gunicorn 不支持 Windows ⇒ 本实验属「模拟」，见文档 §三/2）。

做法：连接数单调递增 N ∈ {0,4,8,12,14,15,16}，每档测：
  · 5 次**串行** GET /api/ping（timeout 3s）
  · 4 次**并发** GET /api/ping（timeout 3s）
记录状态码分布 / p50 / p95 / 超时率，并从旁路控制端口读「已用 worker 线程数」。

用法：
  PYTHONPATH= PYTHONUTF8=1 "C:/.../python.exe" "tests/staging/02_pool_saturation.py"
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (COPY, PRINTER_TOKEN, ServerProc, http_get, open_eio_ws,  # noqa: E402
                    save_result, summarize_http)

APP_PORT, CTL_PORT = 18081, 18098
POOL = 16
N_LIST = [0, 4, 8, 12, 14, 15, 16]
PING = f"http://127.0.0.1:{APP_PORT}/api/ping"


def seq_pings(n=5, timeout=3.0):
    return [http_get(PING, timeout=timeout) for _ in range(n)]


def conc_pings(n=4, timeout=3.0):
    out = [None] * n
    t0 = time.time()

    def one(i):
        out[i] = http_get(PING, timeout=timeout)

    ths = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return out, (time.time() - t0) * 1000


def main():
    print(f"[ENV] 副本: {COPY}  模型: 固定 {POOL} 线程池（gthread 模拟）")
    rows = []
    conns = []
    deep = None
    with ServerProc(APP_PORT, CTL_PORT, mode="pool", pool_size=POOL) as srv:
        time.sleep(0.8)
        base = srv.stats()
        print(f"[BASE] request_threads_in_use=0 active={base['active_count']} rss={base['rss_kb']}KB")
        try:
            for target in N_LIST:
                while len(conns) < target:
                    conns.append(open_eio_ws(APP_PORT, token=PRINTER_TOKEN))
                time.sleep(0.4)
                st = srv.stats()
                seq = summarize_http(seq_pings())
                conc, conc_wall = conc_pings()
                conc = summarize_http(conc)
                row = {
                    "connections": target,
                    "pool": st.get("pool"),
                    "app_worker_threads_in_use": (st.get("pool") or {}).get("inflight"),
                    "active_count": st["active_count"],
                    "rss_kb": st["rss_kb"],
                    "seq_ping": seq,
                    "conc4_ping": conc,
                    "conc4_wall_ms": round(conc_wall, 1),
                }
                rows.append(row)
                print(f"[N={target:<2}] 池内 inflight={row['app_worker_threads_in_use']:<2} "
                      f"pending={(st.get('pool') or {}).get('pending')} "
                      f"串行: 200={seq['ok_200']}/5 超时={seq['timeout']} "
                      f"p50={seq['p50_ms']}ms  |  并发4: 200={conc['ok_200']}/4 "
                      f"超时={conc['timeout']} wall={row['conc4_wall_ms']}ms")
        finally:
            if len(conns) >= POOL:
                t = [None] * 6

                def one(i):
                    t[i] = http_get(PING, timeout=10.0)

                ths = [threading.Thread(target=one, args=(i,)) for i in range(6)]
                for x in ths:
                    x.start()
                for x in ths:
                    x.join()
                deep = summarize_http(t)
                print(f"[N=16 · timeout=10s] {deep}")
                st2 = srv.stats()
                print(f"[N=16 · 超时后池状态] {st2.get('pool')}")
            for ws in conns:
                try:
                    ws.close()
                except Exception:
                    pass
    out = {
        "experiment": "16 线程池 + 认证设备 WS 连接 → HTTP 饱和（容量边界）",
        "model": "gthread 模拟：ThreadPoolExecutor(16) 包裹 Flask-SocketIO WSGI app（backend.app）",
        "pool_size": POOL,
        "ping_path": "/api/ping",
        "rows": rows,
        "n16_timeout10s": deep,
    }
    save_result("02_pool_saturation", out)
    print("\n[CONCLUSION]", end=" ")
    sat = [r for r in rows if r["connections"] >= POOL]
    if sat:
        r = sat[-1]
        print(f"{r['connections']} 条认证 WS 连接 ⇒ 占满 {POOL}/{POOL} 个 worker 线程，"
              f"/api/ping 串行超时率 {r['seq_ping']['timeout_rate']}, "
              f"并发 4 条全超时={r['conc4_ping']['timeout'] == 4}")


if __name__ == "__main__":
    main()
