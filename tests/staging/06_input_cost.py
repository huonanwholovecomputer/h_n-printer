"""06 · 单请求输入成本：深嵌套 JSON / 1MB JSON / 超大 header / 404 洪水

服务模型：srv.py --mode pool --pool-size 16 --login-delay 0.001
  login 的 urlopen 被替换成「1ms 后抛 URLError」——只切断公网调用（本实验不测上游），
  解析/路由/鉴权成本与真实代码一致。

测量项（每项 5 次，报状态码分布 + p50）：
  A 深嵌套 JSON（depth 900 / 1100 / 5000）打 /api/login（无鉴权、必走解析）
  B 深嵌套 JSON 打 /api/admin/order_status（有 RecursionError 兜底 → 期望 403 而非 500）
  C 1MB+ JSON 与 ~1MB JSON（before_request 的 1MB 闸门）
  D 单条 header 8KB / 32KB
  E 404 洪水 1000 次（顺序）
  最后确认服务仍存活（/api/ping 200）

用法：
  PYTHONPATH= PYTHONUTF8=1 "C:/.../python.exe" "tests/staging/06_input_cost.py"
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (COPY, ServerProc, http_get, http_post, save_result,  # noqa: E402
                    summarize_http)

APP_PORT, CTL_PORT = 18083, 18096
BASE = f"http://127.0.0.1:{APP_PORT}"


# S2-12 复跑：/api/login 现在有每 IP 10 次/分的桶，连续打点会撞 429、把「输入解析成本」
# 这个被测项遮住。每次请求换一个 X-Forwarded-For（ProxyFix x_for=1 ⇒ 各自一个桶），
# 让实验测的是解析/路由成本，而不是限速器。
_IP_SEQ = [0]


def _xff():
    _IP_SEQ[0] += 1
    n = _IP_SEQ[0]
    return {"X-Forwarded-For": f"198.19.{(n // 250) % 250}.{n % 250 + 1}"}


def nested_json(depth):
    return (('{"a":' * depth) + "1" + ("}" * depth)).encode()


def bench(label, fn, reps=5):
    res = [fn() for _ in range(reps)]
    s = summarize_http(res)
    print(f"[{label}] status={s['status']} p50={s['p50_ms_all']}ms max={s['max_ms_all']}ms "
          f"body={str(res[0][1])[:90]!r}")
    return s


def throughput(label, fn, n_threads=16, per_thread=50):
    """n_threads 条并发连接各跑 per_thread 次 → 服务端单请求成本 = n_threads / rps。"""
    import threading
    totals = {"n": 0, "status": {}}
    lock = threading.Lock()

    def worker():
        local = {}
        for _ in range(per_thread):
            st, _, _ = fn()
            local[str(st)] = local.get(str(st), 0) + 1
        with lock:
            for k, v in local.items():
                totals["status"][k] = totals["status"].get(k, 0) + v
            totals["n"] += per_thread

    t0 = time.perf_counter()
    ths = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    dt = time.perf_counter() - t0
    rps = totals["n"] / dt
    rec = {"n": totals["n"], "status": totals["status"], "wall_ms": round(dt * 1000, 1),
           "rps": round(rps, 1), "worker_occupancy_ms": round(n_threads / rps * 1000, 3)}
    print(f"[THROUGHPUT {label}] {totals['n']} 次 / {dt*1000:.0f}ms = {rps:.0f} req/s "
          f"（{n_threads} 并发；客户端与服务器同机，故为下界）→ 每 worker 占用 "
          f"{n_threads/rps*1000:.2f}ms/请求；status={totals['status']}")
    return rec


def main():
    print(f"[ENV] 副本: {COPY}")
    out = {"experiment": "单请求输入成本", "server": f"127.0.0.1:{APP_PORT} (pool 16)", "cases": {}}
    with ServerProc(APP_PORT, CTL_PORT, mode="pool", pool_size=16, login_delay=0.001) as srv:
        time.sleep(0.8)
        # 基线
        out["cases"]["baseline_ping"] = bench(
            "baseline /api/ping", lambda: http_get(f"{BASE}/api/ping", timeout=5))
        out["cases"]["login_tiny"] = bench(
            "POST /api/login {code:x}", lambda: http_post(f"{BASE}/api/login",
                                                          json.dumps({"code": "x"}).encode(),
                                                          timeout=5, headers=_xff()))

        # A 深嵌套 JSON → /api/login（同时量 500 的日志放大：werkzeug/gunicorn 都把
        #   未捕获异常连同 traceback 写进错误日志）
        log0 = os.path.getsize(srv.log_path) if os.path.exists(srv.log_path) else 0
        for depth in (900, 1100, 5000):
            payload = nested_json(depth)
            out["cases"][f"login_depth_{depth}"] = bench(
                f"A /api/login depth={depth} ({len(payload)}B)",
                lambda p=payload: http_post(f"{BASE}/api/login", p, timeout=10,
                                            headers=_xff()))
            # 每次深嵌套后确认服务存活
            st, _, _ = http_get(f"{BASE}/api/ping", timeout=5)
            out.setdefault("alive_after", {})[f"depth_{depth}"] = st
        time.sleep(0.3)
        log1 = os.path.getsize(srv.log_path) if os.path.exists(srv.log_path) else 0
        n_500 = out["cases"]["login_depth_1100"]["n"] + out["cases"]["login_depth_5000"]["n"]
        out["five_hundred_log_growth"] = {
            "log_bytes_before": log0, "log_bytes_after": log1,
            "bytes_added": log1 - log0, "n_500": n_500,
            "bytes_per_500": round((log1 - log0) / n_500, 1) if n_500 else None,
        }
        print(f"[A·日志放大] {n_500} 个 500 → 错误日志 +{log1 - log0}B "
              f"（{(log1 - log0) / max(n_500, 1):.0f}B/请求）")

        # B 深嵌套 JSON → /api/admin/order_status（自带 RecursionError 兜底）
        payload = nested_json(1100)
        out["cases"]["order_status_depth_1100"] = bench(
            "B /api/admin/order_status depth=1100",
            lambda: http_post(f"{BASE}/api/admin/order_status", payload, timeout=10,
                              headers=_xff()))

        # C 1MB JSON
        big = b'{"code":"' + b"a" * (1024 * 1024 - 10) + b'"}'
        out["cases"]["json_1mb"] = bench(
            f"C /api/login JSON {len(big)}B (>1MB 闸门)",
            lambda: http_post(f"{BASE}/api/login", big, timeout=10, headers=_xff()))
        near = b'{"code":"' + b"a" * (1024 * 1024 - 64) + b'"}'
        out["cases"]["json_near_1mb"] = bench(
            f"C /api/login JSON {len(near)}B (略低于 1MB)",
            lambda: http_post(f"{BASE}/api/login", near, timeout=10, headers=_xff()))

        # D 超大 header
        for size in (8 * 1024, 32 * 1024):
            out["cases"][f"header_{size}"] = bench(
                f"D /api/ping header X-Pad={size}B",
                lambda s=size: http_get(f"{BASE}/api/ping", timeout=5,
                                        headers={"X-Pad": "a" * s}))

        # E 404 洪水（顺序 1000 次）
        t0 = time.perf_counter()
        st404 = {}
        for i in range(1000):
            st, _, _ = http_get(f"{BASE}/no-such-{i}", timeout=5)
            st404[str(st)] = st404.get(str(st), 0) + 1
        dt = time.perf_counter() - t0
        out["cases"]["404_flood"] = {"n": 1000, "status": st404,
                                     "total_ms": round(dt * 1000, 1),
                                     "mean_ms": round(dt * 1000 / 1000, 3),
                                     "rps": round(1000 / dt, 1)}
        print(f"[E 404×1000] status={st404} 总耗时={dt*1000:.0f}ms 均值={dt*1000/1000:.3f}ms "
              f"≈{1000/dt:.0f} req/s")

        out["alive_final"] = http_get(f"{BASE}/api/ping", timeout=5)[0]
        print(f"[存活] 全部实验后 /api/ping → {out['alive_final']}")

        # F 并发吞吐（16 worker 全开）——扣掉客户端/建连开销，得到服务端单请求成本
        out["throughput"] = {}
        out["throughput"]["ping"] = throughput(
            "GET /api/ping", lambda: http_get(f"{BASE}/api/ping", timeout=5))
        out["throughput"]["404"] = throughput(
            "GET /no-such (404)", lambda: http_get(f"{BASE}/no-such-x", timeout=5))
        out["throughput"]["login_depth_1100"] = throughput(
            "POST /api/login depth=1100 (500)", 
            lambda: http_post(f"{BASE}/api/login", nested_json(1100), timeout=10,
                              headers=_xff()),
            n_threads=16, per_thread=10)
    save_result("06_input_cost", out)
    c = out["cases"]
    print("\n[CONCLUSION] 深嵌套 JSON 在 /api/login 上（depth≥1100）→ "
          f"{c['login_depth_1100']['status']}；"
          f"/api/admin/order_status 同样输入 → {c['order_status_depth_1100']['status']}"
          f"（已兜底，不 500）。"
          f"1MB+ JSON → {c['json_1mb']['status']}（1MB 闸门），"
          f"404 单请求均值 {c['404_flood']['mean_ms']}ms。")


if __name__ == "__main__":
    main()
