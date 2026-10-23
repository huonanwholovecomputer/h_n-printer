# -*- coding: utf-8 -*-
"""验收 · 本地单打印失败必须回落成「失败」（2026-10-23 现场 HN20261023-0010 / -0013）

现场：HP LaserJet 1020 + 攒批模式，打印机里没纸 ⇒ 第 1 趟中断（程序按「第 1 趟成功」收下，
纸数只能人工核对）⇒ 在攒批列表里点「放弃整叠」⇒ **订单在结算页仍是「已完成」**。

根因（代码级）：
  · 本地单（来源=本地，`task_id == 0`）的 `status='sent'` 是**打印启动那一刻**由
    `/api/local_orders` 写下的（单据化 + 幂等：重复上报直接跳过）；
  · 那条链路**没有失败分支** —— `gui._report_job_result(job, success=False)` 里
    `if not (task_id and self._cloud_client): return`，task_id=0 时直接返回，一个字都不报；
  · 于是缺纸中断 / Windows 里取消作业 / 攒批放弃，订单都留在「已完成」并把金额算进收益。

修法（双侧）：
  · 后端新增 `POST /api/local_order_status`（printer token）：只允许把 **本地单** 的
    reserved/sent 改成 failed（父单 + 全部子任务 + 清 `locked_at`）；云端单忽略；
    人工终态（canceled/abandoned/rejected）不覆盖；失败态幂等。
  · 客户端 `_report_job_result(False)` 对本地单调 `_report_local_order_failed()`（后台线程）。
    云端单口径不变（print_success / print_failed 按 task_id）。

本脚本 A 段用**真实 Flask + 真实 SQLite**（复制一份后端到临时目录）跑接口，
B 段用真实 `MainWindow` + 桩 HTTP 跑客户端回报路径。exit 0 = 全绿。
"""
import io
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    return bool(cond)


# ═══════════════════ A. 后端接口（真实 Flask + SQLite）═══════════════════

TMP = tempfile.mkdtemp(prefix="hn_localfail_")
DST = os.path.join(TMP, "printer-backend")
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(
    "__pycache__", "config.py", "config.py.example",
    "orders.db", "users.db", "uploads", "logs", "*.db-wal", "*.db-shm"))
os.chdir(DST)
os.environ["FLASK_ENV"] = "development"
sys.path.insert(0, DST)

import app as backend  # noqa: E402

backend.init_db()
backend.PRINTER_TOKEN = "test-token"
client = backend.app.test_client()
URL = "/api/local_order_status"
TOK = {"token": "test-token"}


def mk_order(num, status, source="local", openid="local", file_status="sent"):
    conn = backend.get_db()
    conn.execute(
        """INSERT INTO orders (file, copies, status, created_at, openid, order_number,
                               total_price, source, owner_name, is_admin_print, print_started_at)
           VALUES ('本地打印 1 个文件', 1, ?, '2026-10-23 13:08:00', ?, ?, 1.1, ?,
                   '霍楠', 1, '2026-10-23 13:08:04')""",
        (status, openid, num, source),
    )
    oid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO order_files (order_id, file_name, copies, page_count, total_price,
                                    status, duplex, created_at, page_range, locked_at)
           VALUES (?, '静电场描绘预习(5).pdf', 1, 5, 1.1, ?, 'on', '2026-10-23 13:08:00', '', ?)""",
        (oid, file_status, "2026-10-23 13:09:00"),
    )
    conn.commit()
    conn.close()
    return oid


def read_order(oid):
    conn = backend.get_db()
    r = conn.execute("SELECT status FROM orders WHERE id = ?", (oid,)).fetchone()
    files = conn.execute("SELECT status, locked_at FROM order_files WHERE order_id = ?",
                         (oid,)).fetchall()
    conn.close()
    return (r["status"] if r else None), [(f["status"], f["locked_at"] or "") for f in files]


print("=== A. POST /api/local_order_status（后端真实接口）===")

oid = mk_order("HN20261023-9001", "sent")
r = client.post(URL, query_string=TOK, json={"order_number": "HN20261023-9001",
                                       "status": "failed", "reason": "攒批被人工放弃"})
body = r.get_json() or {}
st, files = read_order(oid)
check("A1 sent 本地单 → 父单改 failed", r.status_code == 200 and body.get("changed") is True,
      f"HTTP {r.status_code} {body}")
check("A1 父单状态真的写库了", st == "failed", str(st))
check("A1 子任务也改 failed 且清 locked_at",
      files == [("failed", "")], str(files))
check("A1 回报里有 previous_status", body.get("previous_status") == "sent", str(body))

r = client.post(URL, query_string=TOK, json={"order_number": "HN20261023-9001", "status": "failed"})
body = r.get_json() or {}
st, files = read_order(oid)
check("A2 重复上报幂等（不再改写、不报错）",
      r.status_code == 200 and body.get("changed") is False and st == "failed",
      f"HTTP {r.status_code} {body}")

oid_c = mk_order("HN20261023-9002", "sent", source="wechat", openid="oWeChatUser")
r = client.post(URL, query_string=TOK, json={"order_number": "HN20261023-9002", "status": "failed"})
body = r.get_json() or {}
st_c, files_c = read_order(oid_c)
check("A3 云端单不按订单号改写（终态归 task 回报管）",
      r.status_code == 200 and body.get("ignored") is True and st_c == "sent",
      f"{body} status={st_c}")
check("A3 云端单子任务也没被动", files_c == [("sent", "2026-10-23 13:09:00")], str(files_c))

oid_x = mk_order("HN20261023-9003", "canceled")
r = client.post(URL, query_string=TOK, json={"order_number": "HN20261023-9003", "status": "failed"})
body = r.get_json() or {}
st_x, _ = read_order(oid_x)
check("A4 人工终态（已取消）不被失败回落覆盖",
      r.status_code == 200 and body.get("changed") is False and st_x == "canceled",
      f"{body} status={st_x}")

r = client.post(URL, query_string=TOK, json={"order_number": "HN20261023-9999", "status": "failed"})
body = r.get_json() or {}
check("A5 订单不存在 → 200 + changed=False（不炸、不 404 报警）",
      r.status_code == 200 and body.get("changed") is False, f"HTTP {r.status_code} {body}")

r = client.post(URL, query_string=TOK, json={"order_number": "HN20261023-9001", "status": "abandoned"})
check("A6 只接受 status=failed（其它值 400）", r.status_code == 400, f"HTTP {r.status_code}")

r = client.post(URL, query_string=TOK, json={"status": "failed"})
check("A7 缺 order_number → 400", r.status_code == 400, f"HTTP {r.status_code}")

r = client.post(URL, json={"order_number": "HN20261023-9001", "status": "failed"})
check("A8 无 token → 403", r.status_code == 403, f"HTTP {r.status_code}")

r = client.post(URL, json=["not", "a", "dict"])
check("A9 畸形请求体（数组）fail-closed，绝不 500", r.status_code in (400, 403),
      f"HTTP {r.status_code}")
deep = "[" * 1500 + "]" * 1500
r = client.post(URL, query_string=TOK, data=deep, content_type="application/json")
check("A10 深嵌套请求体（depth≥1000）也是 400/403，绝不 500", r.status_code in (400, 403),
      f"HTTP {r.status_code}")

oid_w = mk_order("HN20261023-9004", "reserved", file_status="reserved")
r = client.post(URL, query_string=TOK, json={"order_number": "HN20261023-9004", "status": "failed"})
body = r.get_json() or {}
st_w, files_w = read_order(oid_w)
check("A11 预留单（reserved）也能回落成 failed",
      body.get("changed") is True and st_w == "failed" and files_w == [("failed", "")],
      f"{body} {st_w} {files_w}")

# ═══════════════════ B. 客户端回报路径（真实 MainWindow + 桩 HTTP）═══════════════════

LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
_SANDBOX = tempfile.mkdtemp(prefix="hn_localfail_gui_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
if os.path.isdir(_FONTS):
    os.environ.setdefault("QT_QPA_FONTDIR", _FONTS)
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import converter as _converter  # noqa: E402
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}

import gui  # noqa: E402
import paths  # noqa: E402
from printer_config import PrintJob, TabSettings  # noqa: E402

gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.Yes)
gui.QMessageBox.warning = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)
gui.QMessageBox.information = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)

print("\n=== B. 客户端：_report_job_result 的失败回落 ===")


class _Resp:
    def __init__(self, ok=True, body=None, text=""):
        self.ok = ok
        self._body = body if body is not None else {"success": True, "changed": True}
        self.text = text or str(self._body)

    def json(self):
        return self._body


class FakeClient:
    api_url = "https://example.invalid"
    token = "tok"
    client_id = "cid-test"

    def __init__(self):
        self.success = []
        self.fail = []

    def report_success(self, tid, cfg):
        self.success.append((tid, cfg))

    def report_fail(self, tid, msg):
        self.fail.append((tid, msg))


POSTS = []
_orig_post = gui.http_requests.post


def fake_post(url, **kw):
    POSTS.append((url, kw))
    return _Resp()


gui.http_requests.post = fake_post

win = gui.MainWindow(config_path=paths.config_path())
win.resize(1200, 800)
win.show()
_app.processEvents()
win._log = lambda m: None
win._save_config = lambda *a, **k: None      # 别在测试里写真实配置文件


def wait_posts(n=1, timeout=5.0):
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        _app.processEvents()
        if len(POSTS) >= n:
            return True
        time.sleep(0.02)
    return False


def mk_job(order_number="HN20261023-9001", task_id=0):
    return PrintJob(file_path=os.path.join(_SANDBOX, "x.pdf"), copies=1, duplex="on",
                    page_count=5, order_number=order_number, task_id=task_id)


fc = FakeClient()
win._cloud_client = fc
POSTS.clear()
win._report_job_result(mk_job(), False, "攒批被人工放弃（只打了第 1 趟，纸是半成品）")
ok = wait_posts(1)
url, kw = POSTS[0] if POSTS else ("", {})
payload = kw.get("json") or {}
check("B1 本地单失败会上报 /api/local_order_status",
      ok and url.endswith("/api/local_order_status"), f"{url}")
check("B1 带 printer token（query）", (kw.get("params") or {}).get("token") == "tok",
      str(kw.get("params")))
check("B1 payload: status=failed + 订单号 + 原因",
      payload.get("status") == "failed"
      and payload.get("order_number") == "HN20261023-9001"
      and "放弃" in str(payload.get("reason")), str(payload))

POSTS.clear()
win._report_job_result(mk_job(), True, "")
_app.processEvents()
check("B2 本地单成功**不**打这个接口（完成状态在打印启动时就写了）", not POSTS, str(POSTS))

POSTS.clear()
fc.fail.clear()
win._report_job_result(mk_job(task_id=77), False, "缺纸")
_app.processEvents()
check("B3 云端单失败照旧走 report_fail（不走本地接口）",
      not POSTS and fc.fail and fc.fail[0][0] == 77, f"posts={POSTS} fail={fc.fail}")

POSTS.clear()
fc.success.clear()
win._report_job_result(mk_job(task_id=78), True, "")
_app.processEvents()
check("B3 云端单成功照旧走 report_success",
      not POSTS and fc.success and fc.success[0][0] == 78,
      f"posts={POSTS} success={fc.success}")

POSTS.clear()
win._report_job_result(mk_job(order_number="HN20261023-L0007"), False, "离线临时号")
_app.processEvents()
check("B4 离线临时号（-L）不上报（云端没有这笔单）", not POSTS, str(POSTS))

POSTS.clear()
win._cloud_client = None
win._report_job_result(mk_job(), False, "无云端客户端")
_app.processEvents()
check("B5 没有云端客户端时不炸、不请求", not POSTS, str(POSTS))
win._cloud_client = fc

gui.http_requests.post = _orig_post
win._cloud_client = None      # 别让 closeEvent 去 stop 桩对象
win.close()

print("\n" + "=" * 70)
print("全绿 ✅" if ok_all else "有 FAIL ❌（见上）")
sys.exit(0 if ok_all else 1)
