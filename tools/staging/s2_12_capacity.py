"""S2-12 复跑 · staging 三档容量（3 / 6 / 10 台设备）+ p95/线程/内存/磁盘

依据：docs/安全加固修复计划_2026-10-23.md §S2-12（第 506 行）
  「staging 用 k6/locust 复跑 docs/DDoS评估_2026-10/scripts/01–07；记录 p95/线程/内存/磁盘；
    按 §11.2 设备增长模型（required_threads = ceil(在线设备×2.5)+4）分别复核 3/6/10 台三档」

与 2026-10-24 那次的区别（这次是**真边缘**）：
  · 客户端 → nginx(:18080, 生产 S0-01 全部 limit_*/body/timeout) → backend(:18081, 16 线程池)
  · **每台设备一个源 IP**（127.0.0.2+i），与生产「不同门店不同公网 IP」同形状 ⇒
    nginx limit_conn 6/12 按设备各自计数，不会把单源压测误判成边缘限流；
  · 每档开跑前重启 nginx + backend（清 zone / 重置线程池），避免档间污染。

判据（计划原文）：
  · 三档都要给出连接成功率、p95、线程占用、RSS、磁盘；
  · 与 required_threads 对比，给出 S3-01 触发判定。

用法（先 staging_up.py）：
  PYTHONPATH= PYTHONUTF8=1 "<python311>" tools/staging/s2_12_capacity.py
"""
import json
import math
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import staging_common as sc  # noqa: E402

TIERS = [3, 6, 10]          # 在线设备档位
DEVICE_IP_BASE = 2          # 127.0.0.2 起，每台设备一个源 IP
PROBE_IP = "127.0.0.30"     # 业务探针单独一个 IP（不与设备共享 hn_api 桶）
WS_TIMEOUT = 12.0
T0 = time.time()


def log(m):
    print("[%6.1fs] %s" % (time.time() - T0, m), flush=True)


def required_threads(devices):
    """§11.2：required_threads = ceil(在线设备×2.5) + 4"""
    return int(math.ceil(devices * 2.5)) + 4


def disk_bytes():
    total = 0
    parts = {}
    for name in ("orders.db", "orders.db-wal", "orders.db-shm",
                 "users.db", "users.db-wal", "users.db-shm"):
        p = os.path.join(sc.INSTANCE, name)
        n = os.path.getsize(p) if os.path.exists(p) else 0
        parts[name] = n
        total += n
    logs = 0
    for root, _d, files in os.walk(os.path.join(sc.INSTANCE, "logs")):
        for fn in files:
            try:
                logs += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    parts["logs"] = logs
    parts["total"] = total + logs
    return parts


def stats_snapshot():
    """旁路控制端口：线程池占用 / 线程角色 / RSS / Engine.IO 会话。"""
    st = sc.ctl_stats(timeout=3)
    if not st:
        return {"reachable": False}
    return {
        "reachable": True,
        "pool": st.get("pool"),
        "engineio_sessions": st.get("engineio_sessions"),
        "thread_buckets": st.get("thread_buckets"),
        "active_count": st.get("active_count"),
        "rss_kb": st.get("rss_kb"),
        "runtime": st.get("runtime"),
    }


def ping_batch(n_conn, n_each, ip, timeout=5.0):
    """并发 HTTP 探针（每连接 n_each 次 GET /api/ping），返回耗时/状态分布。"""
    lats, codes = [], {}
    lock = threading.Lock()
    t0 = time.time()

    def w():
        for _ in range(n_each):
            st, _b, ms = sc.http_get("/api/ping", timeout=timeout, source_ip=ip)
            with lock:
                codes[str(st)] = codes.get(str(st), 0) + 1
                if st == 200:
                    lats.append(ms)

    ts = [threading.Thread(target=w) for _ in range(n_conn)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
    dt = time.time() - t0
    total = sum(codes.values())
    return {
        "n": total, "ok": codes.get("200", 0), "status": codes,
        "seconds": round(dt, 2), "rps": round(total / max(dt, .001), 1),
        "p50_ms": sc.pct(lats, 50), "p95_ms": sc.pct(lats, 95),
        "max_ms": round(max(lats), 2) if lats else None,
    }


def open_ws_tier(devices, total_conns):
    """按「每台设备一个源 IP」的形态并发建 total_conns 条 WS（每 IP ≤ ceil(2.5)）。"""
    plan = []
    per = int(math.ceil(total_conns / devices))
    for i in range(devices):
        ip = "127.0.0.%d" % (DEVICE_IP_BASE + i)
        for k in range(per):
            if len(plan) < total_conns:
                plan.append((ip, k))
    plan = plan[:total_conns]
    out = [None] * len(plan)
    barrier = threading.Barrier(len(plan))

    def worker(idx, ip):
        try:
            barrier.wait()
            t0 = time.perf_counter()
            ws, pkt, err = sc.ws_connect(token=sc.STAGING_TOKEN, source_ip=ip,
                                         timeout=WS_TIMEOUT)
            out[idx] = {"ip": ip, "ms": round((time.perf_counter() - t0) * 1000, 1),
                        "ok": bool(ws),
                        "status": 101 if ws else (err or {}).get("status") or "error",
                        "_ws": ws}
        except Exception as e:                                 # noqa: BLE001
            out[idx] = {"ip": ip, "ok": False, "status": "error", "err": repr(e)[:100]}

    ts = [threading.Thread(target=worker, args=(i, ip)) for i, (ip, _k) in enumerate(plan)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(WS_TIMEOUT + 15)
    return out


def run_tier(devices):
    total_conns = int(math.ceil(devices * 2.5))
    req = required_threads(devices)
    log("== %d 台档：目标 %d 条认证 WS（每设备一个源 IP），§11.2 需要 %d 线程 / 现有 16 =="
        % (devices, total_conns, req))

    log("   重启 nginx + backend（清 zone / 重置线程池）")
    sc.restart_nginx()
    sc.stop_backend()
    sc.start_backend()
    time.sleep(0.5)
    base = stats_snapshot()
    before = ping_batch(1, 5, PROBE_IP)
    log("   基线: pool=%s ping p95=%s ms" % (
        (base.get("pool") or {}).get("inflight"), before["p95_ms"]))

    t_open = time.time()
    conns = open_ws_tier(devices, total_conns)
    open_s = time.time() - t_open
    est = [c for c in conns if c and c["ok"]]
    rej = [c for c in conns if c and not c["ok"]]
    codes = {}
    for c in rej:
        codes[c["status"]] = codes.get(c["status"], 0) + 1
    log("   建连: %d/%d 成功（%.1fs），拒绝 %s" % (len(est), total_conns, open_s, codes))

    time.sleep(0.8)
    during = stats_snapshot()
    # 业务探针：8 并发 × 12 次 = 96 次（单 IP burst 150 之内，测的是后端不是边缘限速）
    probe = ping_batch(8, 12, PROBE_IP)
    st_probe = sc.http_get("/api/printer_status", timeout=6, source_ip=PROBE_IP)
    disk = disk_bytes()
    log("   探针: %d/%d ok, p95=%s ms, %.0f req/s; /api/printer_status=%s(%sms)"
        % (probe["ok"], probe["n"], probe["p95_ms"], probe["rps"],
           st_probe[0], round(st_probe[2], 1)))
    log("   pool=%s threads=%s rss=%s MB" % (
        (during.get("pool") or {}), during.get("thread_buckets"),
        round((during.get("rss_kb") or 0) / 1024.0, 1)))

    for c in conns:
        ws = (c or {}).get("_ws")
        if ws:
            try:
                ws.close()
            except Exception:                                  # noqa: BLE001
                pass
    time.sleep(2.5)
    after = stats_snapshot()
    recover = ping_batch(1, 5, PROBE_IP)
    recovered = recover["ok"] == 5 and ((after.get("pool") or {}).get("inflight") or 0) == 0
    log("   释放后: inflight=%s, ping %d/5 %s"
        % ((after.get("pool") or {}).get("inflight"), recover["ok"],
           "恢复" if recovered else "未恢复"))

    row = {
        "devices": devices, "target_conns": total_conns, "required_threads": req,
        "thread_budget": sc.POOL_SIZE,
        "established": len(est), "rejected": len(rej), "reject_status": codes,
        "open_seconds": round(open_s, 2),
        "pool": during.get("pool"), "engineio_sessions": during.get("engineio_sessions"),
        "thread_buckets": during.get("thread_buckets"),
        "active_count": during.get("active_count"),
        "rss_mb": round((during.get("rss_kb") or 0) / 1024.0, 1),
        "baseline_ping": before, "probe": probe,
        "printer_status": {"status": st_probe[0], "ms": round(st_probe[2], 1)},
        "disk": disk, "recovered": recovered, "recovery_ping": recover,
        "ws_detail": [{k: v for k, v in (c or {}).items() if k != "_ws"} for c in conns],
    }
    # 判定：连接全建成 + 探针无超时/无 429；余量 = 16 - required
    capacity = ("超限" if req > sc.POOL_SIZE else
                ("无余量" if req == sc.POOL_SIZE else "有余量"))
    row["verdict"] = {
        "capacity": capacity,
        "headroom_threads": sc.POOL_SIZE - req,
        "all_conns_established": len(rej) == 0,
        "probe_ok": probe["ok"] == probe["n"] and "429" not in probe["status"],
        "pass": len(rej) == 0 and probe["ok"] == probe["n"] and recovered,
    }
    log("   判定: %s（余量 %d 线程）；全建成=%s 探针全 200=%s"
        % (capacity, sc.POOL_SIZE - req, len(rej) == 0, probe["ok"] == probe["n"]))
    return row


def main():
    if not sc.port_open(sc.EDGE_PORT):
        sys.exit("edge 未监听，请先跑 tools/staging/staging_up.py")
    rows = [run_tier(d) for d in TIERS]

    # S3-01 触发判定（计划：在线 >6 台 / 注册 >10 台 / p95 >300ms）
    trigger = []
    for r in rows:
        if r["required_threads"] > sc.POOL_SIZE:
            trigger.append("%d 台档需要 %d 线程 > %d" % (
                r["devices"], r["required_threads"], sc.POOL_SIZE))
    payload = {
        "task": "S2-12 staging 三档容量复跑（真 nginx 边缘 + 每设备独立源 IP）",
        "plan": "docs/安全加固修复计划_2026-10-23.md §S2-12 + §11.2",
        "topology": "client(127.0.0.2..) -> nginx:%d -> backend:%d pool=%d" % (
            sc.EDGE_PORT, sc.APP_PORT, sc.POOL_SIZE),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s": round(time.time() - T0, 1),
        "tiers": rows,
        "s3_01_trigger": {"triggered": bool(trigger), "reasons": trigger},
    }
    sc.save_result("s2_12", payload)
    print("\n==== S2-12 三档汇总 ====")
    print("%-6s %-8s %-8s %-8s %-10s %-9s %-8s %-8s" %
          ("设备", "目标连接", "建成", "需线程", "池占用", "ping p95", "吞吐", "判定"))
    for r in rows:
        print("%-6d %-8d %-8d %-8d %-10s %-9s %-8s %-8s" % (
            r["devices"], r["target_conns"], r["established"], r["required_threads"],
            "%s/16" % ((r["pool"] or {}).get("max_inflight")),
            r["probe"]["p95_ms"], r["probe"]["rps"], r["verdict"]["capacity"]))
    print("\nS3-01 触发: %s" % (trigger or "未触发"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
