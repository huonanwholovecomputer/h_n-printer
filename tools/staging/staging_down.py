"""staging_down.py · 停掉隔离 staging（nginx + backend），并清掉端口残留。

用法：
  PYTHONPATH= PYTHONUTF8=1 "<python311>" tools/staging/staging_down.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import staging_common as sc  # noqa: E402


def main():
    sc.stop_nginx()
    sc.stop_backend()
    left = {"edge": sc.port_open(sc.EDGE_PORT), "app": sc.port_open(sc.APP_PORT),
            "ctl": sc.port_open(sc.CTL_PORT)}
    print("[staging] 已停止；端口状态 %s" % left)
    if any(left.values()):
        print("[staging] ⚠ 仍有端口被占（不是本脚本起的进程？请人工确认 pid）")
        for p in (sc.EDGE_PORT, sc.APP_PORT, sc.CTL_PORT):
            print("   :%d -> pids %s" % (p, sorted(sc.pids_on_port(p))))


if __name__ == "__main__":
    main()
