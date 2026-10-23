#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""health_probe.py — S0-08 外部拨测（**必须跑在另一台机器/另一条网络上**）

依据：docs/安全加固修复计划_2026-10-23.md §S0-08「不要用本机 cron 拨自己」。
本机拨自己的问题：被 DDoS 打瘫时，本机 cron 往往也被 CPU/线程/带宽压住，最需要告警的
时刻反而静默 —— 而那正是 §10 故障判定表里「全站超时 + CPU 0% + 16 条 ESTAB」的场景。

判据（与计划 §8.2 S0 出口一致）：
  · 每 30–60 s `GET /api/ping`；
  · **连续 2 次**失败/超时（默认 5 s）→ 告警；恢复后再告 1 次「已恢复」；
  · 告警通道用环境变量 `HN_ALERT_WEBHOOK`（企业微信/钉钉/Server酱/自建 HTTP 均兼容
    `{"text": "..."}` POST）。没有 webhook 时打印到 stdout（交给外部 cron 邮件转发）。

用法（放在**另一台**常开机器 / 云拨测 / 树莓派上）：
  HN_ALERT_WEBHOOK='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx' \
  python3 health_probe.py --url https://hn-space.cn --interval 60

  # 演练（验证"故障 → 告警"通道本身可用；计划要求 ≤2 分钟有人被叫醒）
  python3 health_probe.py --url https://hn-space.cn --selftest

退出码：0 = 一直正常或收到 SIGINT 正常退出；2 = 演练告警未送达。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

WEBHOOK = os.environ.get("HN_ALERT_WEBHOOK", "")


def log(msg):
    print("%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def send_alert(text):
    """发告警。返回 True=送达。没配 webhook 时打印算「已送达」（交给 cron 邮件）。"""
    line = "[HN打印拨测] %s" % text
    if not WEBHOOK:
        log("ALERT(未配 webhook，仅打印): " + text)
        return True
    body = json.dumps({"text": line, "msgtype": "text",
                       "text": {"content": line}}).encode("utf-8")
    try:
        req = urllib.request.Request(WEBHOOK, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            r.read()
        log("ALERT-SENT " + text)
        return True
    except Exception as e:  # noqa: BLE001
        log("ALERT-FAILED(%s) %s" % (type(e).__name__, text))
        return False


def probe(url, timeout):
    """返回 (ok, 耗时毫秒, 说明)。判据：HTTP 200 且 body 里 status=ok。"""
    t0 = time.time()
    try:
        with urllib.request.urlopen(url + "/api/ping", timeout=timeout) as r:
            raw = r.read(2048).decode("utf-8", "replace")
            ms = (time.time() - t0) * 1000
            ok = r.status == 200 and '"ok"' in raw.replace(" ", "")
            return ok, ms, "HTTP %s" % r.status
    except urllib.error.HTTPError as e:
        return False, (time.time() - t0) * 1000, "HTTP %s" % e.code
    except Exception as e:  # noqa: BLE001
        return False, (time.time() - t0) * 1000, type(e).__name__


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("HN_PROBE_URL", "https://hn-space.cn"))
    ap.add_argument("--interval", type=int, default=60, help="探测间隔秒（30–60 建议值）")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--fail-threshold", type=int, default=2, help="连续失败几次告警")
    ap.add_argument("--selftest", action="store_true", help="只做一次告警通道演练")
    args = ap.parse_args()

    if args.selftest:
        ok = send_alert("【演练】拨测告警通道自检（收到即说明告警链路可用）")
        return 0 if ok else 2

    url = args.url.rstrip("/")
    log("开始拨测 %s/api/ping，间隔 %ss，连续 %d 次失败即告警"
        % (url, args.interval, args.fail_threshold))
    fails = 0
    down = False
    while True:
        ok, ms, detail = probe(url, args.timeout)
        if ok:
            log("OK  %.0fms  %s" % (ms, detail))
            if down:
                send_alert("已恢复：%s/api/ping 正常（%.0fms）" % (url, ms))
                down = False
            fails = 0
        else:
            fails += 1
            log("FAIL %d/%d  %.0fms  %s" % (fails, args.fail_threshold, ms, detail))
            if fails >= args.fail_threshold and not down:
                send_alert("不可用：%s/api/ping 连续 %d 次失败（最后一次 %s，%.0fms）\n"
                           "第一步：见 §10 止血 Runbook —— 先看 ss -tn '( sport = :5000 )' 与 "
                           "ps -L，**不要重启**"
                           % (url, fails, detail, ms))
                down = True
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
