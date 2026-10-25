"""staging_status.py · 看 staging 状态（端口/进程/池/边缘可用性）。

用法：
  PYTHONPATH= PYTHONUTF8=1 "<python311>" tools/staging/staging_status.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import staging_common as sc  # noqa: E402


def main():
    st = sc.status()
    st["pool"] = None
    st["engineio_sessions"] = None
    ctl = sc.ctl_stats(1.5)
    if ctl:
        for k in ("runtime", "pool", "engineio_sessions", "thread_buckets", "rss_kb",
                  "printer_token_is_staging", "scheduler_running"):
            st[k] = ctl.get(k)
    if st["edge_listening"]:
        s, body, ms = sc.http_get(sc.edge_base() + "/api/ping", timeout=3)
        st["edge_ping"] = {"status": s, "ms": round(ms, 1), "body": str(body)[:60]}
        st["edge_server_header"] = None
        try:
            import urllib.request
            r = urllib.request.urlopen(sc.edge_base() + "/api/ping", timeout=3)
            st["edge_server_header"] = r.headers.get("Server")
        except Exception:                                     # noqa: BLE001
            pass
    print(json.dumps(st, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
