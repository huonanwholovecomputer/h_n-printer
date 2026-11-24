import re, io
from pathlib import Path
from datetime import datetime
LOG = Path.home()/"AppData"/"Roaming"/"HN打印工具"/"logs"
# 进程启动锚点（crash log）
anchors=[]
pat=re.compile(r"^===== 进程启动 (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) ")
for line in io.open(LOG/"crash_faulthandler.log",encoding="utf-8",errors="replace"):
    m=pat.match(line)
    if m: anchors.append((m.group(1),m.group(2)))
from collections import Counter
print("=== 崩溃取证锚点（真实进程启动）按天 ===")
for d,c in sorted(Counter(a[0] for a in anchors).items()):
    if "2026-11-1" in d or "2026-11-2" in d: print(f"  {d}: {c}")
print()
# 每日日志：启动横幅 / 干净停止
BANNER=re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[INFO\] HN 本地打印工具启动 · 版本 (v[\d.]+)")
STOP=re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[INFO\] 👀 外部打印监视已停止")
print("=== 启动横幅 vs 干净停止；缺干净停止=上一次是异常终止 ===")
for day in [f"2026-11-{i:02d}" for i in range(17,24)]:
    f=LOG/f"{day}.log"
    if not f.exists(): continue
    starts=[];stops=[]
    for line in io.open(f,encoding="utf-8",errors="replace"):
        m=BANNER.match(line)
        if m: starts.append((m.group(1),m.group(2)))
        m=STOP.match(line)
        if m: stops.append(m.group(1))
    stop_set=set(stops)
    orphan=[s for s in starts if not any(
        (datetime.strptime(s[0],"%Y-%m-%d %H:%M:%S")-datetime.strptime(t,"%Y-%m-%d %H:%M:%S")).total_seconds() in range(0,120)
        and False for t in stop_set)]
    # 更简单：每个启动前 120s 内是否有干净停止
    def has_stop(ts):
        t=datetime.strptime(ts,"%Y-%m-%d %H:%M:%S")
        return any(0 <= (t-datetime.strptime(s,"%Y-%m-%d %H:%M:%S")).total_seconds() <= 120 for s in stop_set)
    crashy=[s for s in starts if not has_stop(s[0]) and s[0][11:]!="00:00:00"]
    print(f"  {day}  启动={len(starts):2d} 干净停止={len(stops):2d}  │ 无前置干净停止的启动={len(crashy)}")
    for s in crashy: print(f"        ⚠ {s[0]} {s[1]}")
