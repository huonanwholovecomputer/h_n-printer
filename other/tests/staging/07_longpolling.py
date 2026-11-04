"""07 · 500 条 Engine.IO **long-polling** 连接 vs WebSocket：线程/内存对比（S2-12 复跑）

> 改版：握手门（S0-02）要求无 sid 的首次握手带 printer token，故握手请求补 `X-Printer-Token`
> 头（真实设备形态）。其余口径不变：一条已占住 worker 的 poll 连接 = 1 worker。

服务模型：srv.py --mode pool --pool-size 16（与实验 2 同一 gthread 模拟）
做法：
  1) 对每条连接先做 Engine.IO long-polling 握手（GET 拿 open 包 → 得 sid）；
  2) 再发第二个 GET（带 sid）——服务端 poll() 阻塞最多 ping_interval+ping_timeout(=45s)，
     这 45s 里它**占住 1 个 worker 线程**（不额外开线程，和 WS 完全不同）；
  3) 8 / 16 条已占满 worker 后，再**开火** 484 条 long-polling 请求（只发不读）⇒ 合计 ~500 条连接，
     它们排在执行器队列里（pending），测线程数/RSS/ping。

结论对比：WS 每条连接占 1 个 worker + 2~3 条额外 Python 线程；long-polling 只占 1 个 worker。
即「打满 16 worker」时 long-polling 的进程线程开销更小、更省攻击者资源，而 nginx 对
/socket.io/ 不设限速（nginx-https.conf），因此是更廉价的线程耗尽路径。

用法：
  PYTHONPATH= PYTHONUTF8=1 "C:/.../python.exe" "tests/staging/07_longpolling.py"
"""
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (COPY, PRINTER_TOKEN, ServerProc, http_get,  # noqa: E402
                    hold_polling, open_raw_polling, save_result, summarize_http)

APP_PORT, CTL_PORT = 18084, 18095
POOL = 16
PING = f"http://127.0.0.1:{APP_PORT}/api/ping"
POLL_REQ = ("GET /socket.io/?EIO=4&transport=polling HTTP/1.1\r\n"
            "Host: 127.0.0.1:{port}\r\nConnection: keep-alive\r\n\r\n")


def fire_polling(port, n):
    """只发请求不读响应：请求会被 accept 进执行器队列（pending），不占 worker。"""
    socks = []
    for _ in range(n):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=6.0)
            s.sendall(POLL_REQ.format(port=port).encode())
            socks.append(s)
        except OSError as e:
            print(f"    [WARN] 第 {len(socks)+1} 条连接失败: {e}")
            break
    return socks


def snap(srv, label, n_conn):
    st = srv.stats()
    pool = st.get("pool") or {}
    ping = summarize_http([http_get(PING, timeout=3.0) for _ in range(3)])
    row = {"connections": n_conn, "pool_inflight": pool.get("inflight"),
           "pool_pending": pool.get("pending"), "active_count": st["active_count"],
           "rss_kb": st["rss_kb"], "ping": ping}
    print(f"[{label}] 连接={n_conn:<3} inflight={row['pool_inflight']} pending={row['pool_pending']} "
          f"active={row['active_count']} rss={row['rss_kb']}KB ping 200={ping['ok_200']}/3 "
          f"超时={ping['timeout']} p50={ping['p50_ms']}ms")
    return row


def main():
    print(f"[ENV] 副本: {COPY}  模型: {POOL} 线程池（gthread 模拟）+ long-polling")
    rows = []
    held, handshakes, fired = [], [], []
    with ServerProc(APP_PORT, CTL_PORT, mode="pool", pool_size=POOL) as srv:
        time.sleep(0.8)
        base = srv.stats()
        print(f"[BASE] inflight=0 active={base['active_count']} rss={base['rss_kb']}KB")
        try:
            for target in (8, 16):
                while len(held) < target:
                    s, sid, openpkt = open_raw_polling(APP_PORT, token=PRINTER_TOKEN)
                    handshakes.append(s)
                    if not sid:
                        raise RuntimeError(f"握手未拿到 sid: {openpkt[:120]!r}")
                    held.append(hold_polling(APP_PORT, sid))
                time.sleep(0.8)
                rows.append(snap(srv, f"N={target}", target))
            # 16 条已占满：第 17 条握手会一直等不到 worker（客户端 6s 超时）—— 实测见日志
            try:
                s17, sid17, _ = open_raw_polling(APP_PORT, timeout=4.0)
                print(f"[第 17 条握手] 意外成功 sid={sid17}")
                handshakes.append(s17)
            except (socket.timeout, TimeoutError, OSError) as e:
                print(f"[第 17 条握手] 超时/失败（预期：无空闲 worker）: {type(e).__name__}")
            # 再开火 484 条，合计约 500 条连接
            fired = fire_polling(APP_PORT, 484)
            time.sleep(1.0)
            rows.append(snap(srv, "N≈500", 16 + len(fired)))
        finally:
            for s in held + handshakes + fired:
                try:
                    s.close()
                except Exception:
                    pass
            time.sleep(0.5)
    out = {
        "experiment": "500 级别 long-polling 连接对线程/内存的影响",
        "model": "gthread 模拟（16 线程池）",
        "poll_block_seconds": 45,
        "rows": rows,
        "surface": "nginx /socket.io/ 无限速、无 limit_conn；每条待发 poll 占 1 worker 且不额外开线程",
        "note": "WS 每条占 1 worker + 2~3 线程；long-polling 每条仅占 1 worker",
    }
    save_result("07_longpolling", out)
    sat = [r for r in rows if (r["pool_inflight"] or 0) >= POOL]
    if sat:
        r = sat[0]
        big = rows[-1]
        print(f"\n[CONCLUSION] {r['connections']} 条 long-polling 即可打满 {POOL} 个 worker"
              f"（inflight={r['pool_inflight']}）并让 /api/ping 超时率 {r['ping']['timeout_rate']}；"
              f"{big['connections']} 条连接时进程线程仅 {big['active_count']}（同数量 WS 约需 "
              f"{4 * big['connections'] + 3} 条），RSS {big['rss_kb']}KB。")


if __name__ == "__main__":
    main()
