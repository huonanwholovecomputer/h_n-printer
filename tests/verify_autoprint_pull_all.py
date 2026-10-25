# -*- coding: utf-8 -*-
"""验收脚本 · 同一订单的两个文件不得被拆成两个打印作业（2026-10-25 现场 HN20261025-0005）

事故形状（订单 753 / HN20261025-0005，生产库实测）：
  · 14:30:18 顾客提交，两条子任务**同一秒**入库：#1525「示波器的使用.pdf」6 页·双面、
    #1526「示波器数据表格.pdf」1 页·单面；此刻本机还没启动（工具 14:30:23 才起）⇒ 都排队。
  · 14:30:28 本机上线并启用接单，连上后只调**一次** `pull_pending()`，而
    `/api/pull_queued_orders` 每次只返回**一个**任务（后端 fetch_and_lock_task 的 LIMIT 1）
    ⇒ 只领到 #1525。
  · 14:30:34 自动打印防抖（AUTO_PRINT_DEBOUNCE_MS=8s）到期，队列里只有 1 个文件 ⇒ 开打。
  · 14:30:42 后端 30 秒扫描把 #1526 用 SocketIO 补推下来 ⇒ 打印机正忙 ⇒ 进补打队列。
  · 14:31:18 第一批打完，接着补打 #1526 ⇒ 操作员看到「一个订单打了两次」。
纸与钱都没错（3 张 + 1 张 = 订单要的 4 张、合计 ¥1.50 只收一次），错的是**作业边界**：
整单被拆成两批，多一次「打印机忙」的空窗，且两批纸分开出。

本脚本钉住两件事（改动前这两节都红）：
  A. 客户端连上后必须**连续拉单到空**（`CloudClient.pull_pending_all`），只拉一次的话
     同一订单的文件必然分批到 ⇒ 必然拆单；HTTP 失败要立刻停（不死循环）、轮数有护栏。
  B. 推送路径的文件名必须用 `order_files.file_name`（顾客看到的原名）；
     `files.original_name` 是上传时的临时名（小程序 `tmp_xxx.pdf`），只在订单元数据为空时
     兜底。本案同一订单两个文件在标签页里一个叫「示波器的使用.pdf」、另一个叫
     「tmp_3de462b0….pdf」—— 两条通道（HTTP 拉单 / SocketIO 推送）口径不一致。

跑法：
    PYTHONPATH= PYTHONUTF8=1 python tests/verify_autoprint_pull_all.py   # exit 0 = 全绿
"""
import inspect
import io
import os
import shutil
import sys
import tempfile
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
SRC_BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ══════════════════════════════════════════════════════════════════════════
# A. 客户端：连上后必须连续拉单到空
# ══════════════════════════════════════════════════════════════════════════
print("=== A1. 真实 CloudClient.pull_pending_all：一次连上把同单两个文件都领回来 ===")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
_old_cwd = os.getcwd()
os.chdir(LOCAL_TOOL)

try:
    from cloud_client import CloudClient      # noqa: E402
except Exception as e:                          # pragma: no cover
    check("导入 cloud_client", False, f"{type(e).__name__}: {e}")
    raise SystemExit(1)


def make_client(tag):
    return CloudClient(api_url="http://127.0.0.1:1", ws_url="", token="tok",
                       client_id=tag)


# 桩：模拟后端「每次只给一个任务」，队列里是 #1525 / #1526
c = make_client("pull-all-drain")
queue = [1525, 1526]
calls = []


def fake_pull(quiet=False):
    calls.append(1)
    if queue:
        # 只需要 key 稳定（pop 出去清空）；值内容本测试不关心
        c._pending_tasks[queue.pop(0)] = object()
    return True


c.pull_pending = fake_pull
got = c.pull_pending_all()
check("两个文件都被领回（旧实现只有 1 个）", got == 2, f"领到 {got} 个")
check("调用次数 = 文件数 + 1（最后再拉一次确认队列已空）", len(calls) == 3, f"{len(calls)} 次")
check("领回的任务都在 _pending_tasks 里", set(c._pending_tasks) == {1525, 1526},
      str(sorted(c._pending_tasks)))

print("\n=== A2. 反证：只调一次 pull_pending（旧行为）必然只剩 1 个文件 ===")
c_old = make_client("pull-all-mutant")
queue2 = [1525, 1526]
c_old.pull_pending = lambda quiet=False: (c_old._pending_tasks.__setitem__(queue2.pop(0), object()), True)[1]
c_old.pull_pending()
check("只拉一次 ⇒ 只有 1 个文件 ⇒ 另一份要等 30 秒扫描补推 ⇒ 拆单",
      len(c_old._pending_tasks) == 1, str(sorted(c_old._pending_tasks)))

print("\n=== A3. HTTP 不通时立刻停（不得在这里空转重试） ===")
c_fail = make_client("pull-all-fail")
fail_calls = []
c_fail.pull_pending = lambda quiet=False: (fail_calls.append(1), False)[1]
check("失败返回 0 个任务", c_fail.pull_pending_all() == 0)
check("失败只调用 1 次（重试交给 _pull_fallback_loop 的退避）", len(fail_calls) == 1,
      f"{len(fail_calls)} 次")

print("\n=== A4. 轮数护栏：后端异常持续返任务也不会无限循环 ===")
c_guard = make_client("pull-all-guard")
guard_calls = []


def always_new(quiet=False):
    guard_calls.append(1)
    c_guard._pending_tasks[9000 + len(guard_calls)] = object()
    return True


c_guard.pull_pending = always_new
check("max_rounds=5 时最多 5 轮", c_guard.pull_pending_all(max_rounds=5) == 5 and
      len(guard_calls) == 5, f"{len(guard_calls)} 轮")

print("\n=== A5. 静态接线：_post_connect_sync 必须走 pull_pending_all ===")
src = inspect.getsource(CloudClient._post_connect_sync)
check("连上后调用 pull_pending_all（不是单次 pull_pending）", "self.pull_pending_all(" in src)
check("不再残留 self.pull_pending() 单次调用", "self.pull_pending()" not in src)

os.chdir(_old_cwd)


# ══════════════════════════════════════════════════════════════════════════
# B. 后端：推送用 order_files.file_name，不用 files.original_name
# ══════════════════════════════════════════════════════════════════════════
print("\n=== B. 真实 Flask+SQLite：process_pending_orders 推送的文件名 ===")

tmp_root = tempfile.mkdtemp(prefix="hn_verify_pullall_")
dst = os.path.join(tmp_root, "printer-backend")
shutil.copytree(SRC_BACKEND, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs", "backups"))
sys.path.insert(0, dst)
os.chdir(dst)

import app as backend      # noqa: E402

backend.PRINTER_TOKEN = "testtok"
backend.init_db()

TMP_NAME = "tmp_3de462b0466ca374a3409c81044956abb71f2cfe289e0565.pdf"
DISPLAY_NAME = "示波器数据表格.pdf"

# 落一份真实存在的文件，否则 process_pending_orders 会判「文件不存在」直接标失败
real_pdf = os.path.join(tmp_root, "real.pdf")
with open(real_pdf, "wb") as f:
    f.write(b"%PDF-1.4\n%verify\n")

conn = backend.get_db_conn()
conn.execute("INSERT OR REPLACE INTO files"
             " (id, original_name, saved_name, path, size, created_at, openid,"
             "  page_count, page_count_verified)"
             " VALUES (?, ?, ?, ?, 100, ?, 'verify-openid', 1, 1)",
             ("file-1526", TMP_NAME, TMP_NAME, real_pdf,
              datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
for oid, ofid, display in ((9753, 1526, DISPLAY_NAME), (9754, 1527, "")):
    conn.execute("INSERT INTO orders (id, file_id, file, copies, status, created_at, openid,"
                 " duplex, page_count, order_number, auto_print, target_client, source)"
                 " VALUES (?, ?, ?, 1, 'queued', ?, 'verify-openid', 'off', 1, ?, 1, 'devA', 'wechat')",
                 (oid, "file-1526", display or TMP_NAME,
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  "HN20261025-%04d" % (oid - 9748)))
    conn.execute("INSERT INTO order_files (id, order_id, file_id, file_name, copies, status,"
                 " created_at, duplex, page_range)"
                 " VALUES (?, ?, 'file-1526', ?, 1, 'queued', ?, 'off', '')",
                 (ofid, oid, display,
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
conn.commit()
row = conn.execute("SELECT original_name FROM files WHERE id = 'file-1526'").fetchone()
check("前置：files.original_name 确实是上传临时名", row["original_name"] == TMP_NAME)
conn.close()

# 让扫描认为「有在线接单设备」
backend.get_active_clients = lambda: ["devA"]
backend.get_claiming_device_ids = lambda: ["devA"]
backend.printer_clients["devA"] = {"sid": "sid-devA", "heartbeat": datetime.now(),
                                   "connected_at": datetime.now()}

pushed = []


class _FakeSio:
    def emit(self, event, payload=None, to=None, **kw):
        pushed.append((event, dict(payload or {}), to))


backend.socketio = _FakeSio()
backend.process_pending_orders()

by_task = {}
for ev, payload, to in pushed:
    if ev == "print_task":
        by_task[payload.get("task_id")] = payload

check("两条排队子任务都被推送", set(by_task) == {1526, 1527}, str(sorted(by_task)))
check("文件名取 order_files.file_name（顾客看到的原名）",
      by_task.get(1526, {}).get("file_name") == DISPLAY_NAME,
      repr(by_task.get(1526, {}).get("file_name")))
check("不再出现上传临时名（改前这里就是 tmp_3de4….pdf）",
      TMP_NAME not in (by_task.get(1526, {}).get("file_name") or ""),
      repr(by_task.get(1526, {}).get("file_name")))

# 兜底：订单元数据为空时仍要给出名字（不能推出空文件名）
fallback = (by_task.get(1527, {}).get("file_name") or "").strip()
check("order_files.file_name 为空时兜底用 files.original_name（不留空名）",
      fallback == TMP_NAME, repr(fallback))

check("payload 其余字段未被影响（订单号 / 无障碍标记）",
      by_task.get(1526, {}).get("order_number") == "HN20261025-0005"
      and by_task.get(1526, {}).get("auto_print") is True,
      f"order_number={by_task.get(1526, {}).get('order_number')!r} "
      f"auto_print={by_task.get(1526, {}).get('auto_print')!r}")

# 状态机未被这轮改动带偏：推送即 printing（既有契约）
conn = backend.get_db_conn()
st = {r["id"]: r["status"] for r in conn.execute(
    "SELECT id, status FROM order_files WHERE id IN (1526, 1527)")}
conn.close()
check("推送后子任务仍按既有契约置 printing", st == {1526: "printing", 1527: "printing"}, str(st))

os.chdir(_old_cwd)
try:
    shutil.rmtree(tmp_root, ignore_errors=True)
except Exception:
    pass

print("\n" + ("=== 全部通过 ===" if ok_all else "=== 存在失败项 ==="))
sys.exit(0 if ok_all else 1)
