"""08 · 三档设备规模的容量复核（3 / 6 / 10 台）—— S2-12 主实验

策略取自 docs/安全加固修复计划_2026-10-23.md §11.2：
    required_threads = ceil(在线设备数 × 2.5) + 4
（2.5 = 每台设备 1 条 Socket.IO 长连接 + 1 条 HTTP 拉单/轮询 + 0.5 余量；
 +4 = 运维/告警/页数分析等常驻需求。生产 gthread threads=16。）

做法（隔离副本，pool 模式 = gthread 模拟，**不碰生产**）：
  1. 建 1 万单 + 每单 1 个子任务的 SQLite（对齐 S2-01 的 10k 订单口径），
     用 `token_serializer.dumps()` 造一个普通用户 token（副本 SECRET_KEY 是假值）；
  2. 每档：建立 ceil(设备×2.5) 条**带 printer token** 的 Engine.IO 长连接（真实设备形态），
     量「占住多少 worker / 进程线程 / RSS / 磁盘」；
  3. 同时打三种业务探针，记录 p50/p95/超时率：
       · GET /api/ping（基线探针）
       · GET /api/orders（鉴权 + 10k 订单分页，SQLite 读路径）
       · 16 并发 ping 吞吐（rps，含每 worker 占用）
  4. 档间关闭连接、等 worker 回收，确认是「被占住」而不是「被打坏」；
  5. 逐档给 required_threads 与实测边界比对，输出 S3-01 触发判定。

用法：
  PYTHONPATH= PYTHONUTF8=1 "C:/.../python.exe" "tests/staging/08_staging_tiers.py"
  可选：--pool 16 --orders 10000 --tiers 3,6,10 --conn-per-device 2.5
"""
import argparse
import json
import math
import os
import sqlite3
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (COPY, PRINTER_TOKEN, ServerProc, db_bytes, http_get,  # noqa: E402
                    open_eio_ws, save_result, seed_bench, summarize_http)

APP_PORT, CTL_PORT = 18085, 18094
USER_OPENID = "bench-user-0001"


def conc_pings(n=16, per_thread=20):
    """16 并发 × per_thread 次 GET /api/ping → rps（客户端与服务端同机，属下界）。"""
    out = {"n": 0, "status": {}}
    lock = threading.Lock()

    def work():
        local = {}
        for _ in range(per_thread):
            st, _b, _ms = http_get(f"http://127.0.0.1:{APP_PORT}/api/ping", timeout=2)
            local[str(st)] = local.get(str(st), 0) + 1
        with lock:
            for k, v in local.items():
                out["status"][k] = out["status"].get(k, 0) + v
            out["n"] += per_thread

    ths = [threading.Thread(target=work) for _ in range(n)]
    t0 = time.perf_counter()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    dt = time.perf_counter() - t0
    out["wall_ms"] = round(dt * 1000, 1)
    out["rps"] = round(out["n"] / dt, 1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=16, help="gthread 线程数（生产 16）")
    ap.add_argument("--orders", type=int, default=10000)
    ap.add_argument("--tiers", default="3,6,10")
    ap.add_argument("--conn-per-device", type=float, default=2.5)
    args = ap.parse_args()
    tiers = [int(x) for x in args.tiers.split(",") if x.strip()]

    print(f"[ENV] 副本={COPY} pool={args.pool} 订单={args.orders} 档位={tiers} "
          f"连接/台={args.conn_per_device}")
    token, seed_secs = seed_bench(args.orders, USER_OPENID)
    disk_seeded = db_bytes()
    print(f"[SEED] {args.orders} 单 + {args.orders} 子任务，用时 {seed_secs:.1f}s；"
          f"磁盘 orders.db={disk_seeded['orders.db']}B wal={disk_seeded['orders.db-wal']}B "
          f"合计 {disk_seeded['total']}B")

    out = {"experiment": "三档设备规模容量复核（S2-12）",
           "model": "required_threads = ceil(在线设备×2.5)+4（计划 §11.2）",
           "pool_size": args.pool, "orders": args.orders, "seed_seconds": round(seed_secs, 1),
           "disk_after_seed": disk_seeded, "tiers": []}
    conns = []
    with ServerProc(APP_PORT, CTL_PORT, mode="pool", pool_size=args.pool) as srv:
        time.sleep(0.8)
        base = srv.stats()
        print(f"[BASE] inflight=0 active={base['active_count']} rss={base['rss_kb']}KB")
        for devices in tiers:
            target = max(1, math.ceil(devices * args.conn_per_device))
            upgraded = 0
            fail_reasons = {}
            for _ in range(target):
                try:
                    conns.append(open_eio_ws(APP_PORT, token=PRINTER_TOKEN, timeout=6.0))
                    upgraded += 1
                except Exception as e:  # noqa: BLE001
                    name = type(e).__name__
                    fail_reasons[name] = fail_reasons.get(name, 0) + 1
            time.sleep(0.6)
            st = srv.stats()
            pool_snap = st.get("pool") or {}

            serial = summarize_http(
                [http_get(f"http://127.0.0.1:{APP_PORT}/api/ping", timeout=3) for _ in range(10)])
            # 池已满（串行探针全超时）时，继续打 320 次业务点只是把同一结论再确认 40 秒：
            # 记录「饱和」并跳过，让报告跑得完、也让判据停在「连不上/全超时」这一层。
            saturated = (serial["timeout"] or 0) >= 9
            if saturated:
                conc = {"skipped": True, "reason": "池满（串行 ping 全超时），跳过 320 次打点"}
                orders_sum = {"skipped": True, "reason": "同上"}
            else:
                conc = conc_pings()
                orders_res = []
                for page in range(1, 11):
                    orders_res.append(http_get(
                        f"http://127.0.0.1:{APP_PORT}/api/orders?page={page}&per_page=20",
                        timeout=5, headers={"Authorization": "Bearer " + token}))
                orders_sum = summarize_http(orders_res)
            disk = db_bytes()

            required = math.ceil(devices * args.conn_per_device) + 4
            row = {
                "devices": devices,
                "connections_target": target,
                "connections_upgraded": upgraded,
                "connect_failures": fail_reasons,
                "required_threads": required,
                "pool_size": args.pool,
                "headroom": args.pool - required,
                "pool": pool_snap,
                "active_count": st["active_count"],
                "rss_kb": st["rss_kb"],
                "disk": disk,
                "ping_serial": serial,
                "ping_16x25": conc,
                "orders_authed": orders_sum,
            }
            out["tiers"].append(row)
            print(f"\n[档 {devices} 台] 长连接 {upgraded}/{target}（失败 {fail_reasons or '无'}）"
                  f" 占住 worker={pool_snap.get('inflight')}/{args.pool} "
                  f"pending={pool_snap.get('pending')} 线程={st['active_count']} "
                  f"RSS={st['rss_kb']}KB")
            if saturated:
                print(f"  串行 ping: 200={serial['ok_200']}/10 超时={serial['timeout']} "
                      f"⇒ 池已满，跳过后续 320 次业务打点")
            else:
                print(f"  串行 ping: 200={serial['ok_200']}/10 超时={serial['timeout']} "
                      f"p95={serial['p95_ms']}ms | 16×{20} ping: rps={conc['rps']} "
                      f"status={conc['status']}")
                print(f"  /api/orders（10k 单）: 200={orders_sum['ok_200']}/10 "
                      f"p50={orders_sum['p50_ms']}ms p95={orders_sum['p95_ms']}ms "
                      f"max={orders_sum['max_ms']}ms")
            print(f"  磁盘 {disk['total']}B | required_threads={required} "
                  f"⇒ 余量 {args.pool - required}")

            # 档间清理：关掉本档连接，等 worker 回收到池里
            for ws in conns:
                try:
                    ws.close()
                except Exception:
                    pass
            conns = []
            time.sleep(2.5)
            after = srv.stats()
            row["after_close"] = {"pool": after.get("pool") or {},
                                  "active_count": after["active_count"],
                                  "ping": summarize_http(
                                      [http_get(f"http://127.0.0.1:{APP_PORT}/api/ping",
                                                timeout=3) for _ in range(5)])}
            print(f"  关闭后：worker={row['after_close']['pool'].get('inflight')} "
                  f"恢复 ping 200={row['after_close']['ping']['ok_200']}/5")
    save_result("08_staging_tiers", out)

    # ---------- 结论 ----------
    print("\n" + "=" * 66)
    print("三档容量结论（生产 threads=16，按 §11.2 公式）")
    print("=" * 66)
    for r in out["tiers"]:
        # 判定以 §11.2 公式为主、实测为辅：公式超限即「无余量」，实测出现连不上/超时/吞吐塌方为「已超限」。
        over_formula = r["required_threads"] > args.pool
        broken = (r["connections_upgraded"] < r["connections_target"]
                  or (r["ping_serial"]["timeout"] or 0) > 0
                  or (not r["ping_16x25"].get("skipped")
                      and (r["ping_16x25"].get("rps") or 0) < 200))
        verdict = ("已超限（实测连不上/超时）" if broken
                   else ("无余量（公式超限，实测尚可）" if over_formula else "通过"))
        print(f"  {r['devices']:>2} 台：需 {r['required_threads']:>2} 线程 / 现有 {args.pool} ⇒ {verdict}；"
              f"实测长连接 {r['connections_upgraded']}/{r['connections_target']}、"
              f"串行 ping p95={r['ping_serial']['p95_ms']}ms、"
              f"16×20 吞吐="
              f"{'跳过(池满)' if r['ping_16x25'].get('skipped') else str(r['ping_16x25']['rps']) + ' req/s'}")
    trig = [r["devices"] for r in out["tiers"] if r["required_threads"] > args.pool]
    print(f"\n[S3-01 触发判定] required_threads 超过 {args.pool} 的档位：{trig or '无'}"
          f" ⇒ 按计划『在线 >6 台 / 注册 >10 台』提前排 S3-01；"
          f"3 台（需 {math.ceil(3 * args.conn_per_device) + 4}）在现有 16 线程内有余量。")


if __name__ == "__main__":
    main()
