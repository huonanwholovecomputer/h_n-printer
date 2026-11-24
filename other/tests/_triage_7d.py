"""7 天日志体检 —— 本地打印工具 daily_log + 崩溃日志 + 财务审计。

不做语义判断，只做结构统计 + 两层关键字扫描：
  1) 显性故障：异常/失败/错误/超时/拒绝 等
  2) 隐性故障：像成功但数值不对（失败 0 之外的 0 页、0 字节、负数、空结果）
  3) 假阳性：含"失败"字样但实际是成功汇总
输出按严重度排序的事件簇。
"""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

LOG_DIR = Path.home() / "AppData" / "Roaming" / "HN打印工具" / "logs"
LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] (.*)$")
END = datetime(2026, 11, 24, 9, 0)
START = END - timedelta(days=7)

# ---------- 归一化 ----------
MD5 = re.compile(r"\b[0-9a-fA-F]{8,32}\b")
PATH = re.compile(r"[A-Za-z]:\\[^\s\"']+")
ORDER = re.compile(r"(?:HN|ORD|订单号?)[:=：]?\s*\w{6,}")
NUM = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?")


def norm(msg: str) -> str:
    s = MD5.sub("<HASH>", msg)
    s = PATH.sub("<PATH>", s)
    s = ORDER.sub("<ORDER>", s)
    s = NUM.sub("<N>", s)
    return s.strip()


# ---------- 关键字 ----------
HARD = ["异常", "失败", "错误", "超时", "拒绝", "无法", "不存在", "未找到", "崩溃",
        "Error", "ERROR", "Traceback", "Exception", "timeout", "refused", "denied",
        "failed", "Failed", "warn", "WARN", "警告"]
# 含"失败"但是成功汇总的假阳性
BENIGN = re.compile(r"成功\s*\d+\s*[，,]\s*失败\s*0|失败\s*0\s*[，,]\s*成功")
# 隐性：0 值 / 空结果
SUSPECT = [
    (re.compile(r"→\s*0\s*页"), "缓存/转换得到 0 页"),
    (re.compile(r"0\s*字节"), "0 字节上传"),
    (re.compile(r"页数\s*[:=]?\s*0\b"), "页数为 0"),
    (re.compile(r"0\s*个文件"), "0 个文件"),
    (re.compile(r"空白率\s*[:=]?\s*100"), "空白率 100%"),
    (re.compile(r"-\d+\s*[张页]"), "负数张页"),
]

rows = []
per_day = Counter()
day_first, day_last = {}, {}

files = sorted(p for p in LOG_DIR.glob("*.log")
               if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.log", p.name))
for p in files:
    day = p.stem
    try:
        dt = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        continue
    if not (START.date() <= dt.date() <= END.date()):
        continue
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for ln, raw in enumerate(fh, 1):
            raw = raw.rstrip("\n")
            m = LINE.match(raw)
            if not m:
                if raw.strip():
                    rows.append((day, ln, "?", "", raw, "UNPARSED"))
                continue
            ts, lvl, msg = m.groups()
            per_day[day] += 1
            day_first.setdefault(day, ts)
            day_last[day] = ts
            rows.append((day, ln, ts, lvl, msg, norm(msg)))

print("=" * 78)
print(f"窗口 {START:%Y-%m-%d %H:%M} → {END:%Y-%m-%d %H:%M}")
print("=" * 78)
print("\n[1] 每日行数 / 首末记录")
for d in sorted(per_day):
    print(f"  {d}  {per_day[d]:6d} 行   {day_first[d][11:]} → {day_last[d][11:]}")

print("\n[2] 级别分布")
lv = Counter(r[3] for r in rows if r[3] != "?")
for k, v in lv.most_common():
    print(f"  {k:10s} {v}")

# ---------- 事件聚类 ----------
clusters = defaultdict(lambda: {"n": 0, "first": None, "last": None, "sample": "", "days": set(), "kind": set()})
for day, ln, ts, lvl, msg, nm in rows:
    if lvl == "?":
        key = ("UNPARSED", nm[:80])
        clusters[key]["n"] += 1
        clusters[key]["days"].add(day)
        clusters[key]["kind"].add("unparsed")
        continue
    kind = None
    if BENIGN.search(msg):
        kind = "benign"
    elif any(k in msg for k in HARD):
        kind = "hard"
    for rx, why in SUSPECT:
        if rx.search(msg):
            kind = "suspect"
            break
    if kind in (None, "benign"):
        continue
    key = (kind, nm)
    c = clusters[key]
    c["n"] += 1
    c["days"].add(day)
    c["kind"].add(kind)
    if c["first"] is None or ts < c["first"]:
        c["first"] = ts
    if c["last"] is None or ts > c["last"]:
        c["last"] = ts
    if not c["sample"]:
        c["sample"] = msg

print(f"\n[3] 故障类事件簇（hard+suspect，共 {len(clusters)} 类 / {sum(c['n'] for c in clusters.values())} 次）")
for (kind, nm), c in sorted(clusters.items(), key=lambda kv: -kv[1]["n"]):
    print(f"\n  [{kind}] x{c['n']}  {c['first']} → {c['last']}  ({len(c['days'])}天)")
    print(f"    样本: {c['sample'][:160]}")

# ---------- 断档检测 ----------
print("\n[4] 日志断档（>90 分钟无记录，工作时间窗口内）")
prev = None
for day, ln, ts, lvl, msg, nm in rows:
    if lvl == "?" or not ts or ts == "?":
        continue
    t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    if prev and (t - prev).total_seconds() > 5400:
        gap = (t - prev).total_seconds() / 3600
        if 7 <= prev.hour <= 22 or 7 <= t.hour <= 22:
            print(f"  {prev} → {t}   {gap:.1f}h")
    prev = t

# ---------- 进程启动 ----------
print("\n[5] 进程启动/停止标记")
for day, ln, ts, lvl, msg, nm in rows:
    if re.search(r"启动|退出|关闭|停止|初始化|加载完成", msg) and re.search(r"工具|程序|应用|主窗|启动", msg):
        print(f"  {ts}  {msg[:110]}")
