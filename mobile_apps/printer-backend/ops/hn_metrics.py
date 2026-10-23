#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hn_metrics.py — S0-08 主机/业务指标采集（cron 每分钟跑一次，零额外依赖）

依据：docs/安全加固修复计划_2026-10-23.md §S0-08（R9/DS-24）+ §11.2（D-8 容量模型）。

为什么不用 node_exporter：本机没装、也不是 Prometheus 栈（§S0-08 原文「或等价采集 /
最省事的做法：cron + textfile collector」）。这里用 cron 直接落两种产物：

  ① Prometheus textfile（若 /var/lib/node_exporter/textfile/ 存在就写 hn.prom，
     以后接 Prometheus 时零改动；不存在就跳过）；
  ② 每日 CSV：/home/printer-backend/logs/metrics-YYYYMMDD.csv
     —— "按日留曲线"是 D-8 的判据来源（在线 >6 台 / 注册 >10 台 / ping p95 >300ms
     任一命中就要提前排 S3-01，不等季度）。

采集项（对应 §11.2 的数据来源）：
  online_devices / registered_devices / claiming_devices / gunicorn_threads /
  estab_5000 / disk_avail_mb / mem_avail_mb / ping_ms / nginx_429 / nginx_499 / nginx_503

告警（阈值命中写 logs/alerts.log，并可选 POST HN_ALERT_WEBHOOK）：
  · 在线设备 > 6 或 注册设备 > 10        → S3-01 提前排期信号（D-8）
  · ping_ms > 300                        → 容量/被攻击信号
  · 磁盘可用 < 2048 MB                   → 磁盘满风险（R4）
  · nginx 429 每分钟 > 30（且非演练标记） → 可能误伤或正被刷

用法：
  python3 hn_metrics.py              # 采集一次（cron 用）
  python3 hn_metrics.py --dry-run    # 只打印不上报
  python3 hn_metrics.py --alert-test # 演练：强制走一次告警通道
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # /home/printer-backend
LOG_DIR = os.path.join(BASE, "logs")
CSV = os.path.join(LOG_DIR, "metrics-%s.csv" % datetime.now().strftime("%Y%m%d"))
ALERT_LOG = os.path.join(LOG_DIR, "alerts.log")
TEXTFILE_DIR = os.environ.get("HN_TEXTFILE_DIR", "/var/lib/node_exporter/textfile")
STATE_FILE = os.path.join(LOG_DIR, ".metrics_state.json")
API = "http://127.0.0.1:5000"

# D-8 容量模型（§11.2）
MAX_ONLINE_DEVICES = int(os.environ.get("HN_MAX_ONLINE_DEVICES", "6"))
MAX_REGISTERED_DEVICES = int(os.environ.get("HN_MAX_REGISTERED_DEVICES", "10"))
MAX_PING_MS = float(os.environ.get("HN_MAX_PING_MS", "300"))
MIN_DISK_MB = int(os.environ.get("HN_MIN_DISK_MB", "2048"))
MAX_429_PER_RUN = int(os.environ.get("HN_MAX_429_PER_RUN", "30"))
WEBHOOK = os.environ.get("HN_ALERT_WEBHOOK", "")


# ---------------------------------------------------------------- 基础采集
def _get_json(path, timeout=3.0):
    try:
        with urllib.request.urlopen(API + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")), None
    except Exception as e:  # noqa: BLE001
        return None, type(e).__name__


def ping_ms():
    t0 = time.time()
    data, err = _get_json("/api/ping", timeout=5.0)
    if err or not isinstance(data, dict) or data.get("status") != "ok":
        return -1.0
    return round((time.time() - t0) * 1000.0, 1)


def registered_devices():
    try:
        with open(os.path.join(BASE, "devices.json"), encoding="utf-8") as f:
            return len(json.load(f) or {})
    except Exception:  # noqa: BLE001
        return -1


def gunicorn_threads():
    """gunicorn master 名下的线程数（`ps -L`；R1/R9 的线程占用判据）。"""
    try:
        pids = subprocess.run(["pgrep", "-f", "gunicorn.*app:app"], capture_output=True,
                              text=True, timeout=5).stdout.split()
        if not pids:
            return -1
        out = subprocess.run(["ps", "-L", "-p", pids[0]], capture_output=True, text=True,
                             timeout=5).stdout
        return max(0, len(out.strip().splitlines()) - 1)
    except Exception:  # noqa: BLE001
        return -1


def estab_5000():
    try:
        out = subprocess.run(["ss", "-tn", "state", "established", "( sport = :5000 )"],
                             capture_output=True, text=True, timeout=5).stdout
        return max(0, len(out.strip().splitlines()) - 1)
    except Exception:  # noqa: BLE001
        return -1


def disk_avail_mb():
    try:
        st = os.statvfs("/")
        return int(st.f_bavail * st.f_frsize / (1024 * 1024))
    except Exception:  # noqa: BLE001
        return -1


def mem_avail_mb():
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:  # noqa: BLE001
        pass
    return -1


def nginx_status_counts():
    """自上次运行以来 access.log 新增行里的 429 / 499 / 503 计数（按字节偏移，不重扫全文件）。"""
    path = "/var/log/nginx/access.log"
    res = {"429": 0, "499": 0, "503": 0}
    try:
        size = os.path.getsize(path)
        state = {}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, encoding="utf-8") as f:
                state = json.load(f) or {}
        offset = int(state.get("nginx_offset", 0))
        if offset > size:  # 日志轮转过
            offset = 0
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read(8 * 1024 * 1024)  # 单次最多读 8MB，避免爆内存
            new_offset = offset + len(chunk)
        for raw in chunk.splitlines():
            parts = raw.split()
            if len(parts) >= 9:
                st = parts[8]
                if st in res:
                    res[st] += 1
        state["nginx_offset"] = new_offset
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:  # noqa: BLE001
        pass
    return res


# ---------------------------------------------------------------- 告警
def emit_alert(kind, detail, dry_run=False):
    line = "%s\t%s\t%s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), kind, detail)
    if dry_run:
        print("[ALERT]", line.strip())
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(ALERT_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        pass
    if WEBHOOK:
        try:
            body = json.dumps({"text": "[HN打印告警] %s\n%s\n主机 %s" % (
                kind, detail, socket.gethostname())}).encode("utf-8")
            req = urllib.request.Request(WEBHOOK, data=body,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:  # noqa: BLE001
            pass
    print("[ALERT]", line.strip())


# ---------------------------------------------------------------- 主流程
def main():
    dry = "--dry-run" in sys.argv
    alert_test = "--alert-test" in sys.argv

    ping = ping_ms()
    ps, _ = _get_json("/api/printer_status")
    online = int((ps or {}).get("count", -1)) if isinstance(ps, dict) else -1
    claiming = int((ps or {}).get("claiming_count", -1)) if isinstance(ps, dict) else -1
    reg = registered_devices()
    threads = gunicorn_threads()
    estab = estab_5000()
    disk = disk_avail_mb()
    mem = mem_avail_mb()
    ng = nginx_status_counts()

    row = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "online_devices": online,
        "registered_devices": reg,
        "claiming_devices": claiming,
        "gunicorn_threads": threads,
        "estab_5000": estab,
        "disk_avail_mb": disk,
        "mem_avail_mb": mem,
        "ping_ms": ping,
        "nginx_429": ng["429"],
        "nginx_499": ng["499"],
        "nginx_503": ng["503"],
    }

    if dry:
        print(json.dumps(row, ensure_ascii=False))
    else:
        os.makedirs(LOG_DIR, exist_ok=True)
        write_header = not os.path.exists(CSV)
        with open(CSV, "a", encoding="utf-8") as f:
            if write_header:
                f.write(",".join(row.keys()) + "\n")
            f.write(",".join(str(v) for v in row.values()) + "\n")
        # Prometheus textfile（装了 node_exporter 时自动生效）
        try:
            if os.path.isdir(TEXTFILE_DIR):
                with open(os.path.join(TEXTFILE_DIR, "hn_printer.prom"), "w",
                          encoding="utf-8") as f:
                    for k, v in row.items():
                        if k in ("ts",):
                            continue
                        f.write("hn_%s %s\n" % (k, v))
        except Exception:  # noqa: BLE001
            pass

    # ---- 阈值 / 告警 ----
    if alert_test:
        emit_alert("演练", "这是一条演练告警（--alert-test）", dry_run=dry)
        return 0
    if online > MAX_ONLINE_DEVICES:
        emit_alert("容量-在线设备", "在线 %d 台 > 阈值 %d：按 §11.2 提前排 S3-01"
                   % (online, MAX_ONLINE_DEVICES), dry_run=dry)
    if reg > MAX_REGISTERED_DEVICES:
        emit_alert("容量-注册设备", "注册 %d 台 > 阈值 %d：清理僵尸设备 / 评估 S3-01"
                   % (reg, MAX_REGISTERED_DEVICES), dry_run=dry)
    if ping > MAX_PING_MS:
        emit_alert("可用性-ping", "本机 /api/ping %.1fms > %.0fms" % (ping, MAX_PING_MS),
                   dry_run=dry)
    if 0 <= ping < 0:
        pass
    if ping < 0:
        emit_alert("可用性-故障", "/api/ping 不可用（后端未响应）", dry_run=dry)
    if 0 <= disk < MIN_DISK_MB:
        emit_alert("磁盘", "可用 %d MB < %d MB（R4：清 client_body_temp / journald vacuum）"
                   % (disk, MIN_DISK_MB), dry_run=dry)
    if ng["429"] > MAX_429_PER_RUN:
        emit_alert("限速", "上一分钟内 nginx 429 = %d > %d：核对是按 IP 聚合的误伤还是正在被刷"
                   % (ng["429"], MAX_429_PER_RUN), dry_run=dry)
    if threads > 12:
        emit_alert("线程", "gunicorn 线程 %d/16 长期占用 >70%%：R1 风险（看 ss ESTAB 与来源 IP）"
                   % threads, dry_run=dry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
