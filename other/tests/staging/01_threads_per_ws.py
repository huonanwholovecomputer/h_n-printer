"""01 · 每 WebSocket 连接占几个服务端线程（真实代码 + 真实 Flask-SocketIO threading 模式）

> 2026-10-24（S2-12 复跑）改版：S0-02 给 Engine.IO 首次握手加了 token 门，
> **未认证连接已不可能建立**。所以本实验拆成两段：
>   A. 门的有效性：无 token / 错 token 的握手一律 403，且线程数不涨（攻击面已消失）。
>   B. 真实设备的线程成本：**带 printer token** 的连接（生产设备形态），
>      逐档加连接、量「连接数 → 进程线程数」曲线 + 线程角色分解 + 断连后回落。
>
> 这第二段才是 §11.2 容量模型（每台设备 2.5 条连接 × 每条占 1 个 worker）的实测依据。
>
> 启动：srv.py --mode werkzeug（socketio.run(threaded=True) → 每连接一条线程，上限由 OS 定）
> 用法：
>   PYTHONPATH= PYTHONUTF8=1 "C:/.../python.exe" "tests/staging/01_threads_per_ws.py"
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (COPY, PRINTER_TOKEN, ServerProc, eio_ws_status,  # noqa: E402
                    open_eio_ws, save_result)

APP_PORT, CTL_PORT = 18080, 18099
GATE_N_LIST = [1, 4, 16]
AUTH_N_LIST = [1, 2, 4, 8, 16]


def roles(names):
    """按真实线程名归类。

    注：控制端口自身也是 ThreadingHTTPServer，其处理线程同样叫
    ``Thread-N (process_request_thread)``；采样时恰有 1 条在处理本次 /stats，
    故 app 的请求线程数 = process_request_thread 计数 − 1。
    """
    r = {"request_thread(app WSGI)": 0, "ws_reader": 0, "engineio_writer": 0,
         "engineio_ping": 0, "service_task": 0, "other": 0}
    for n in names:
        if n.startswith("MainThread") or n == "ctl":
            continue
        if "process_request_thread" in n:
            r["request_thread(app WSGI)"] += 1
        elif "simple_websocket.Base._thread" in n:
            r["ws_reader"] += 1
        elif "(writer)" in n:
            r["engineio_writer"] += 1
        elif "(_send_ping)" in n:
            r["engineio_ping"] += 1
        elif "(_service_task)" in n:
            r["service_task"] += 1
        else:
            r["other"] += 1
    r["request_thread(app WSGI)"] = max(0, r["request_thread(app WSGI)"] - 1)
    return r


def snap(srv):
    st = srv.stats()
    return st, roles(st["thread_names"])


def main():
    print(f"[ENV] 副本: {COPY}")
    gate_rows, rows = [], []
    conns = []
    with ServerProc(APP_PORT, CTL_PORT, mode="werkzeug") as srv:
        time.sleep(0.8)
        base, r0 = snap(srv)
        print(f"[BASE] active={base['active_count']} roles={r0} rss={base['rss_kb']}KB")

        # ---------- A. 握手门：未认证 / 错 token ----------
        for n in GATE_N_LIST:
            codes = []
            for i in range(n):
                # 第 1 条故意用错 token，其余完全不带
                tok = "wrong-token" if i == 0 else None
                st, _note = eio_ws_status(APP_PORT, token=tok)
                codes.append(st)
            time.sleep(0.4)
            st, _r = snap(srv)
            gate_rows.append({
                "attempts": n, "statuses": codes,
                "active_count": st["active_count"],
                "delta_vs_base": st["active_count"] - base["active_count"],
                "engineio_sessions": st["engineio_sessions"],
            })
            print(f"[GATE] 尝试 {n:<2} 条无 token/错 token 连接 → 状态 {codes} "
                  f"线程增量={st['active_count'] - base['active_count']} "
                  f"engineio_sessions={st['engineio_sessions']}")
        assert all(c == 403 for r in gate_rows for c in r["statuses"]), gate_rows
        assert all(r["delta_vs_base"] == 0 for r in gate_rows), gate_rows

        # ---------- B. 真实设备形态：带 token 的长连接 ----------
        try:
            for target in AUTH_N_LIST:
                while len(conns) < target:
                    conns.append(open_eio_ws(APP_PORT, token=PRINTER_TOKEN))
                time.sleep(0.6)
                st, r = snap(srv)
                row = {
                    "connections": target,
                    "active_count": st["active_count"],
                    "delta_vs_base": st["active_count"] - base["active_count"],
                    "threads_per_conn": round(
                        (st["active_count"] - base["active_count"]) / target, 2),
                    "engineio_sessions": st["engineio_sessions"],
                    "rss_kb": st["rss_kb"],
                    "rss_delta_kb": (st["rss_kb"] or 0) - (base["rss_kb"] or 0),
                    "roles": r,
                }
                rows.append(row)
                print(f"[WS] conns={target:<3} active={row['active_count']:<4} "
                      f"delta={row['delta_vs_base']:<4} per_conn={row['threads_per_conn']:<5} "
                      f"rss={row['rss_kb']}KB (+{row['rss_delta_kb']}) roles={r}")
        finally:
            for ws in conns:
                try:
                    ws.close()
                except Exception:
                    pass
        # 断连后回落验证（engineio 的 _send_ping 每 15s 一轮，等 18s 看它是否也退出）
        time.sleep(3)
        after3 = srv.stats()
        print(f"[AFTER-CLOSE +3s ] active={after3['active_count']} "
              f"roles={roles(after3['thread_names'])}")
        time.sleep(18)
        after = srv.stats()
        print(f"[AFTER-CLOSE +21s] active={after['active_count']} "
              f"roles={roles(after['thread_names'])}")
        out = {
            "experiment": "每 WebSocket 连接的服务端线程占用（S0-02 门 + 认证设备形态）",
            "mode": "socketio.run(threaded=True) / Werkzeug ThreadingWSGIServer（真实代码路径）",
            "base_active_count": base["active_count"],
            "base_roles": r0,
            "gate_rows": gate_rows,
            "rows": rows,
            "after_close_3s": {"active_count": after3["active_count"],
                               "roles": roles(after3["thread_names"])},
            "after_close_21s": {"active_count": after["active_count"],
                                "roles": roles(after["thread_names"])},
        }
    save_result("01_threads_per_ws", out)
    if rows:
        tail = [r for r in rows if r["connections"] >= 8]
        per = sum(r["threads_per_conn"] for r in tail) / len(tail)
        print(f"\n[CONCLUSION] A：{sum(r['attempts'] for r in gate_rows)} 次无 token/错 token 握手"
              f"全部 403、线程零增长 ⇒ 匿名 WS 耗尽线程的攻击面已在握手层消失（S0-02）。"
              f"\n             B：≥8 条**认证**连接时每连接稳定增量 ≈ {per:.2f} 线程/连接；"
              f"{rows[-1]['connections']} 条 → {rows[-1]['active_count']} 条进程线程、"
              f"RSS +{rows[-1]['rss_delta_kb']}KB。")


if __name__ == "__main__":
    main()
