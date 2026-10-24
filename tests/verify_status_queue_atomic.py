# -*- coding: utf-8 -*-
"""审计修复验收 · 🔴-2 联动：status_queue.json 读-改-写并发安全

事故形状：status_queue.json 有两个写者 ——
 · _queue_status_sync（GUI 线程：状态上报失败时暂存）
 · sync_pending_statuses（连接线程：联网后重放）
两者都是「读整个文件 → 改 → 覆盖写回」，且**全程无锁**。并发时后者会用重放前
的旧快照覆盖前者刚追加的条目 ⇒ 状态上报永久丢失（后端订单状态卡住 →
重推打印）。此外 Windows 上并发 os.replace 会与并发读冲突。

本脚本直接构造 CloudClient 实例（不连网），把 http 换成桩，覆盖：
 · N 线程并发追加不丢；
 · 重放期间并发追加的条目不被覆盖吞掉；
 · 重放失败的条目保留、成功的移除、歧义条目按原语义丢弃；
 · 损坏文件隔离、上限截断、全清后删文件。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import types

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import cloud_client as cc                 # noqa: E402
from cloud_client import CloudClient      # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


TMP = tempfile.mkdtemp(prefix="hn_sq_")
QUEUE = os.path.join(TMP, "status_queue.json")


def make_client():
    c = CloudClient(api_url="", ws_url="", token="tok", client_id="sq-verify")
    c._status_queue_path = lambda: QUEUE          # 隔离到临时目录，不碰真实缓存
    return c


def read_queue():
    if not os.path.exists(QUEUE):
        return []
    with open(QUEUE, "r", encoding="utf-8") as f:
        return json.load(f)


def reset_queue(items=None):
    with open(QUEUE, "w", encoding="utf-8") as f:
        json.dump(items or [], f, ensure_ascii=False)


class Resp:
    def __init__(self, code):
        self.status_code = code
        self.ok = 200 <= code < 300


print("=== 1. 并发追加不丢（原实现无锁 ⇒ 覆盖写互相吞条目）===")
client = make_client()
reset_queue()
TH, PER = 8, 25
errors = []
barrier = threading.Barrier(TH)


def worker(tid):
    try:
        barrier.wait(timeout=10)                 # 尽量同时起跑，放大争用
        for i in range(PER):
            client._queue_status_sync(9000 + tid, "accepted")
    except Exception as e:                       # Windows 并发 os.replace/读 冲突
        errors.append(f"线程{tid}: {e!r}")


ths = [threading.Thread(target=worker, args=(t,)) for t in range(TH)]
for t in ths:
    t.start()
for t in ths:
    t.join(timeout=60)
q = read_queue()
check(f"{TH} 线程 × {PER} 条全部落盘（共 {TH * PER} 条）", len(q) == TH * PER,
      f"实得 {len(q)} 条，丢失 {TH * PER - len(q)} 条")
check("并发过程中无异常（Windows 下并发 replace/read 冲突）", not errors,
      "; ".join(errors[:3]))
check("条目内容正确（每线程条数一致）", all(
    sum(1 for it in q if it["order_id"] == 9000 + t) == PER for t in range(TH)),
    "各线程计数: " + str([sum(1 for it in q if it["order_id"] == 9000 + t) for t in range(TH)]))

print("\n=== 2. 重放期间并发追加的条目不得被覆盖吞掉 ===")
client = make_client()
reset_queue([{"order_id": 1001, "status": "accepted", "queued_at": "2026-10-14 10:00:00"},
             {"order_id": 1002, "status": "abandoned", "queued_at": "2026-10-14 10:00:01"}])
posted = []
injected = {"done": False}


def post_inject(url, params=None, json=None, timeout=None, headers=None):
    posted.append(json)
    if not injected["done"]:
        injected["done"] = True
        # 模拟 GUI 线程在重放途中暂存了一条新状态
        client._queue_status_sync(1003, "accepted")
    return Resp(200)


_orig_http = cc.http_requests
cc.http_requests = types.SimpleNamespace(post=post_inject)
try:
    client._last_replay_attempt = 0.0
    client.sync_pending_statuses()
finally:
    cc.http_requests = _orig_http
q = read_queue()
check("重放期间追加的条目仍在队列中（原实现直接删除文件 ⇒ 丢失）",
      any(it.get("order_id") == 1003 for it in q), f"queue={q}")
check("已成功上报的条目已从队列移除",
      not any(it.get("order_id") in (1001, 1002) for it in q), f"queue={q}")
check("两条都确实发出去了", len(posted) == 2, f"posted={posted}")

print("\n=== 3. 重放失败条目保留、业务拒绝(4xx)条目丢弃 ===")
client = make_client()
reset_queue([{"order_id": 2001, "status": "accepted", "queued_at": "2026-10-14 11:00:00"},
             {"order_id": 2002, "status": "accepted", "queued_at": "2026-10-14 11:00:01"},
             {"order_id": 2003, "status": "abandoned", "queued_at": "2026-10-14 11:00:02"}])


def post_mixed(url, params=None, json=None, timeout=None, headers=None):
    oid = (json or {}).get("order_id")
    if oid == 2002:
        raise RuntimeError("网络断了")            # 失败 → 保留
    if oid == 2003:
        return Resp(400)                         # 业务拒绝 → 丢弃
    return Resp(200)                             # 成功 → 移除


cc.http_requests = types.SimpleNamespace(post=post_mixed)
try:
    client._last_replay_attempt = 0.0
    client.sync_pending_statuses()
finally:
    cc.http_requests = _orig_http
ids = [it.get("order_id") for it in read_queue()]
check("失败条目(#2002)保留待下次重放", ids == [2002], f"queue ids={ids}")

print("\n=== 4. 未知状态 / 无 task_id 的死条目按原语义丢弃 ===")
client = make_client()
reset_queue([{"order_id": 1, "status": "??", "queued_at": "x"},
             {"order_id": 0, "status": "sent", "queued_at": "x"},
             {"order_id": 1, "status": "accepted", "queued_at": "x"}])


def post_ok(url, params=None, json=None, timeout=None, headers=None):
    return Resp(200)


cc.http_requests = types.SimpleNamespace(post=post_ok)
try:
    client._last_replay_attempt = 0.0
    client._connected = False                     # sent 走 SocketIO 分支，未连接 → 保留
    client.sync_pending_statuses()
finally:
    cc.http_requests = _orig_http
q = read_queue()
check("未知状态条目已丢弃", not any(it.get("status") == "??" for it in q), f"queue={q}")
check("无 task_id 的死条目已丢弃",
      not any(it.get("status") == "sent" for it in q), f"queue={q}")

print("\n=== 5. 全成功后删除文件；损坏文件隔离；超上限截断最旧 ===")
client = make_client()
reset_queue([{"order_id": 3001, "status": "accepted", "queued_at": "x"}])
cc.http_requests = types.SimpleNamespace(post=post_ok)
try:
    client._last_replay_attempt = 0.0
    client.sync_pending_statuses()
finally:
    cc.http_requests = _orig_http
check("全部同步成功后删除队列文件", not os.path.exists(QUEUE), f"exists={os.path.exists(QUEUE)}")

with open(QUEUE, "w", encoding="utf-8") as f:
    f.write("{ 这不是 JSON")
client._queue_status_sync(4001, "accepted")
corrupts = [n for n in os.listdir(TMP) if ".corrupt-" in n]
check("损坏文件被改名保留（非静默清空）", len(corrupts) >= 1, f"corrupts={corrupts}")
check("损坏后队列从空继续且含新条目",
      [it.get("order_id") for it in read_queue()] == [4001], f"queue={read_queue()}")

old_max = cc.STATUS_QUEUE_MAX
cc.STATUS_QUEUE_MAX = 5
try:
    for i in range(8):
        client._queue_status_sync(5000 + i, "accepted")
finally:
    cc.STATUS_QUEUE_MAX = old_max
ids = [it["order_id"] for it in read_queue()]
check("超过上限时丢弃最旧、保留最新（上限 5 保留 #5003~5007）",
      ids == [5003, 5004, 5005, 5006, 5007], f"ids={ids}")

print("\n" + ("全部通过" if ok_all else "存在失败项"))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if ok_all else 1)
