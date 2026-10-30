# -*- coding: utf-8 -*-
"""日志保留 30 天：按天日志过期自动删除（2026-10-30 用户第 5 条要求）。

用户原话：「日志管理：保留近30天的日志，自动删除30天以上的日志。」

口径（都在 local_print_tool/daily_log.py，读写清三件事只走这一个模块）：
  · 判据是**文件名里的日期**，不是 mtime —— 拷贝/解压会改 mtime；
  · 只删 DAILY_RE 命中的 `YYYY-MM-DD.log`；`local_tool.log`（旧版单文件）与
    `finance_audit.log`（收支审计流水）**一个都不许动**（审计要长期可追）；
  · 今天那个文件、未来日期文件、文件名日期解析不出来的，一律不删（宁可漏删不误删）；
  · 触发时机：Handler 打开日志文件时（启动首次 + 每次跨午夜换文件），不额外起定时器；
  · 清理失败永不抛异常（它跑在启动与日志链路里，不能影响打印）。

跑法：python tests/verify_log_retention.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

import daily_log                     # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return cond


def section(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


TMP = tempfile.mkdtemp(prefix="hn_log_keep_")
NOW = datetime(2026, 10, 30, 12, 0, 0)          # 固定"今天"，夹具日期全部相对于它算


def mkdir(tag):
    path = os.path.join(TMP, tag)
    os.makedirs(path, exist_ok=True)
    return path


def touch(path, text="x\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def day_file(log_dir, offset_days):
    """相对"今天"偏移 offset_days 天的那份日日志（负=过去）。"""
    day = (NOW.date() + timedelta(days=offset_days)).strftime("%Y-%m-%d")
    return touch(os.path.join(log_dir, day + ".log"), f"line of {day}\n")


# ══════════════════════════════════════════════════════════════════
section("① 保留窗口边界：30 天以上的删、正好 30 天前与今天的留")
# ══════════════════════════════════════════════════════════════════

d1 = mkdir("boundary")
f_today = day_file(d1, 0)
f_29 = day_file(d1, -29)          # 29 天前 → 留
f_30 = day_file(d1, -30)          # 正好 30 天前 → 留（"30 天以上"才删）
f_31 = day_file(d1, -31)          # 31 天前 → 删
f_60 = day_file(d1, -60)          # 60 天前 → 删
gone = daily_log.prune_old_logs(d1, 30, now_fn=lambda: NOW)
left = sorted(os.path.basename(p) for p in os.listdir(d1))

check("正好 30 天前的 2026-09-30.log 保留（不删边界那一天）", os.path.basename(f_30) in left,
      f"剩 {left}")
check("29 天前的保留", os.path.basename(f_29) in left)
check("今天的绝不删（正被 Handler 写着，Windows 下也删不掉）", os.path.basename(f_today) in left)
check("31 天前 / 60 天前的删掉了", os.path.basename(f_31) not in left
      and os.path.basename(f_60) not in left)
check("返回值就是真正删掉的那两个文件", sorted(gone) == sorted([f_31, f_60]),
      f"{[os.path.basename(p) for p in gone]}")
check("删完之后目录里只剩 3 份日志", len(left) == 3, f"{left}")

# ══════════════════════════════════════════════════════════════════
section("② 非日文件一个都不许动：旧版 local_tool.log、收支审计、用户自己的文件")
# ══════════════════════════════════════════════════════════════════

d2 = mkdir("foreign")
old_day = day_file(d2, -100)                              # 该删的日文件（证明清理真的跑了）
legacy = touch(os.path.join(d2, "local_tool.log"), "old single file\n")
audit = touch(os.path.join(d2, "finance_audit.log"), "审计流水\n")
notes = touch(os.path.join(d2, "说明.txt"), "用户自己的文件\n")
weird = touch(os.path.join(d2, "2026-13-40.log"), "文件名不是合法日期\n")
backup = touch(os.path.join(d2, "2026-01-01.log.bak"), "备份后缀不匹配\n")
daily_log.prune_old_logs(d2, 30, now_fn=lambda: NOW)
left2 = sorted(os.listdir(d2))

check("100 天前的日日志删掉了（清理确实执行）", os.path.basename(old_day) not in left2, f"{left2}")
check("旧版 local_tool.log 保留（老机器上还在用，删了就再也读不回来）",
      os.path.basename(legacy) in left2)
check("finance_audit.log 保留（收支审计流水，必须长期可追）", os.path.basename(audit) in left2)
check("非日志文件（说明.txt）保留", os.path.basename(notes) in left2)
check("文件名日期解析不出来的 2026-13-40.log 保留（宁可不删）",
      os.path.basename(weird) in left2)
check("后缀不匹配的 2026-01-01.log.bak 保留", os.path.basename(backup) in left2)

# ══════════════════════════════════════════════════════════════════
section("③ 时钟异常：未来日期的文件不许被删（时钟回拨/测试夹具）")
# ══════════════════════════════════════════════════════════════════

d3 = mkdir("future")
future = day_file(d3, +5)
old = day_file(d3, -400)
daily_log.prune_old_logs(d3, 30, now_fn=lambda: NOW)
left3 = sorted(os.listdir(d3))
check("未来日期的 2026-11-04.log 保留", os.path.basename(future) in left3, f"{left3}")
check("400 天前的照删（未来日期不能把整条清理带停）", os.path.basename(old) not in left3)

# ══════════════════════════════════════════════════════════════════
section("④ 判据是文件名日期，不是 mtime（拷贝/解压会改 mtime）")
# ══════════════════════════════════════════════════════════════════

d4 = mkdir("mtime")
old_named_new_mtime = day_file(d4, -90)          # 名字很老 → 该删
recent_named_old_mtime = day_file(d4, -3)        # 名字很新 → 该留
recent_ts = NOW.timestamp()
for path, ts in ((old_named_new_mtime, recent_ts),
                 (recent_named_old_mtime, (NOW - timedelta(days=900)).timestamp())):
    os.utime(path, (ts, ts))
daily_log.prune_old_logs(d4, 30, now_fn=lambda: NOW)
left4 = sorted(os.listdir(d4))
check("名字是 90 天前、但 mtime 被改成了今天 ⇒ 照删（不按 mtime 判）",
      os.path.basename(old_named_new_mtime) not in left4, f"{left4}")
check("名字是 3 天前、但 mtime 被改成了两年前 ⇒ 保留（不按 mtime 判）",
      os.path.basename(recent_named_old_mtime) in left4)

# ══════════════════════════════════════════════════════════════════
section("⑤ 失败与畸形输入一律不抛（它跑在启动与日志链路里）")
# ══════════════════════════════════════════════════════════════════

try:
    empty = daily_log.prune_old_logs(os.path.join(TMP, "不存在的目录"), 30, now_fn=lambda: NOW)
    check("目录不存在 ⇒ 返回空列表、不抛", empty == [])
except Exception as e:                # noqa: BLE001
    check("目录不存在 ⇒ 返回空列表、不抛", False, f"{type(e).__name__}: {e}")

d5 = mkdir("clamp")
day_file(d5, 0)                                # 今天：任何情况下都不删
for off in (-1, -2, -3, -5):
    day_file(d5, off)
daily_log.prune_old_logs(d5, 0, now_fn=lambda: NOW)          # 0 天 → 钳到 1 天（只删 1 天以上）
left5 = sorted(os.path.basename(p) for p in os.listdir(d5))
check("keep_days=0 被钳到 1 天：今天的和 1 天前的留下，2 天以上的删掉（不会一次性清空）",
      left5 == sorted([(NOW.date()).strftime("%Y-%m-%d") + ".log",
                       (NOW.date() - timedelta(days=1)).strftime("%Y-%m-%d") + ".log"]),
      f"{left5}")
daily_log.prune_old_logs(d5, "坏参数", now_fn=lambda: NOW)   # 字符串参数 → 回落到默认 30
check("keep_days 传字符串 ⇒ 回落默认 30（窗口内的都不动）、不抛",
      len(os.listdir(d5)) == 2, f"{sorted(os.listdir(d5))}")

d6 = mkdir("callback")
day_file(d6, -50)
day_file(d6, -51)
seen = []
daily_log.prune_old_logs(d6, 30, now_fn=lambda: NOW, on_delete=seen.append)
check("on_delete 回调拿到每一个真正删掉的文件", len(seen) == 2, f"{[os.path.basename(p) for p in seen]}")

d7 = mkdir("boom")
day_file(d7, -50)


def _explode(_path):
    raise RuntimeError("回调炸了")


try:
    daily_log.prune_old_logs(d7, 30, now_fn=lambda: NOW, on_delete=_explode)
    check("on_delete 回调抛异常不会把清理带崩（文件照删）", len(os.listdir(d7)) == 0)
except Exception as e:                # noqa: BLE001
    check("on_delete 回调抛异常不会把清理带崩（文件照删）", False, f"{type(e).__name__}: {e}")

# ══════════════════════════════════════════════════════════════════
section("⑥ 接到 Handler 上：启动打开 + 跨午夜换文件时各清一次（不额外起定时器）")
# ══════════════════════════════════════════════════════════════════

d8 = mkdir("handler")
old_one = day_file(d8, -45)
kept_one = day_file(d8, -5)
hook_calls = []
clock = {"now": datetime(2026, 10, 30, 23, 59, 0)}
handler = daily_log.make_handler(d8, now_fn=lambda: clock["now"],
                                 on_prune=lambda gone: hook_calls.append(list(gone)))
logger_name = "hn_log_keep_probe"
import logging                        # noqa: E402

probe = logging.getLogger(logger_name)
probe.setLevel(logging.DEBUG)
probe.addHandler(handler)
probe.info("第一行：跨午夜前")
check("启动首次打开今天的文件时就把 45 天前那份清掉了", not os.path.exists(old_one))
check("窗口内的 5 天前那份保留", os.path.exists(kept_one))
check("on_prune 收到删除列表（GUI 据此落一行「🧹 日志清理：删除 N 个」）",
      hook_calls and len(hook_calls[0]) == 1, f"{hook_calls}")

clock["now"] = datetime(2026, 10, 31, 0, 0, 30)       # 跨午夜
probe.info("第二行：跨午夜后")
day2 = os.path.join(d8, "2026-10-31.log")
check("跨午夜后自动落到新的一天文件", os.path.isfile(day2))
check("跨天换文件时又清了一次（旧 5 天前那份仍在窗口内 ⇒ 这次没有删除）",
      len(hook_calls) == 1, f"{len(hook_calls)} 次回调")
check("清理不会删掉刚打开的今天文件", os.path.isfile(os.path.join(d8, "2026-10-30.log"))
      and os.path.isfile(day2))
check("清完之后 read_tail 仍读得到最新内容（清理没把日志链路弄断）",
      "第二行：跨午夜后" in daily_log.read_tail(d8), repr(daily_log.read_tail(d8)[-60:]))
probe.removeHandler(handler)
handler.close()

# ══════════════════════════════════════════════════════════════════
section("⑦ 现场接线：KEEP_LOG_DAYS=30 + GUI 挂上 on_prune（静态核对）")
# ══════════════════════════════════════════════════════════════════

check("保留天数是 30（用户口径）", daily_log.KEEP_LOG_DAYS == 30, f"{daily_log.KEEP_LOG_DAYS}")
gui_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
check("GUI 建 Handler 时把 keep_days 传进去（不靠默认值隐式生效）",
      "keep_days=daily_log.KEEP_LOG_DAYS" in gui_src)
check("GUI 传了 on_prune 回调（清理要留痕，不能静默删文件）",
      "on_prune=_on_logs_pruned" in gui_src)
check("清理结果写进界面日志（现场可核）", "日志清理：删除" in gui_src)
check("GUI 没有重新长出第二个日志文件口径（不自己拼 YYYY-MM-DD.log）",
      "daily_log.make_handler" in gui_src and "strftime(\"%Y-%m-%d\")" not in gui_src.split("daily_log.make_handler")[0][-400:])

print("\n" + "=" * 78)
print("全部通过 ✅" if ok_all else "有失败项 ❌")
print("=" * 78)
sys.exit(0 if ok_all else 1)
