"""staging_up.py · 一键起隔离 staging（nginx 边缘 18080 → backend 18081）

用法：
  PYTHONPATH= PYTHONUTF8=1 "<python311>" tools/staging/staging_up.py
  PYTHONPATH= PYTHONUTF8=1 "<python311>" tools/staging/staging_up.py --fresh   # 重建实例（清库/清日志）

幂等：已在跑就只报告状态。停止用 staging_down.py。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import staging_common as sc  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="重建实例副本（清空库/上传/日志）")
    ap.add_argument("--pool-size", type=int, default=sc.POOL_SIZE)
    a = ap.parse_args()

    sc.ensure_dirs()
    if a.fresh:
        sc.stop_nginx()
        sc.stop_backend()
        sc.create_instance(force=True)
    else:
        sc.create_instance()

    code, out = sc.nginx_test()
    print("[staging] nginx -t → exit=%d" % code)
    for line in out.splitlines():
        print("          " + line)
    if code != 0:
        sys.exit(2)

    sc.start_backend(a.pool_size)
    sc.start_nginx()

    st = sc.ctl_stats()
    ping = sc.http_get(sc.edge_base() + "/api/ping", timeout=5)
    info = {
        "topology": "client -> nginx:%d -> backend:%d (pool=%d, ≈gunicorn gthread)" % (
            sc.EDGE_PORT, sc.APP_PORT, a.pool_size),
        "edge": sc.edge_base(),
        "edge_ping": {"status": ping[0], "body": ping[1][:80], "ms": round(ping[2], 1)},
        "backend_stats": {k: st.get(k) for k in ("runtime", "pool", "engineio_sessions",
                                                 "thread_buckets", "rss_kb",
                                                 "printer_token_is_staging")} if st else None,
        "instance": sc.INSTANCE,
        "logs": {"backend": sc.BACKEND_LOG, "nginx_access": sc.NGINX_ACCESS_LOG,
                 "nginx_error": sc.NGINX_ERROR_LOG},
        "token_cmd": "$env:HN_STAGING_TOKEN",
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
    print("\n[staging] READY  edge=%s  backend=:%d  ctl=:%d" %
          (sc.edge_base(), sc.APP_PORT, sc.CTL_PORT))
    print("[staging] 假 printer token（staging 专用，与生产无关）: %s" % sc.STAGING_TOKEN)


if __name__ == "__main__":
    main()
