# -*- coding: utf-8 -*-
"""验收脚本 · 离线单「换号补报」必须带接单设备 + 真实打印时刻

线上现场（2026-11-02，HN20261102-0011）：那台机器 09:50 断网，12:14:19 分配本地临时号
`HN20261102-L0005`，12:14:40–12:14:47 打完 3 个文件（成功 3/3），12:15:15 重连后由
`_sync_local_orders_to_cloud()` 换成正号 `HN20261102-0011` 补报。结果：

  ① 结算页「接单设备」是「—」—— 补报 payload 里**没有 client_id**，后端
     `app.py:9478 report_client_id` 取到空串 ⇒ `orders.received_client` 永远不写；
  ② 订单 `created_at` 变成补报时刻 12:15:15，比真实打印晚 35 秒 —— payload 写的是
     `datetime.now()` 而不是 `PrintJob.finished_at`。

本脚本钉住四处：
  1. 换号补报 payload 带 client_id + created_at = 任务真实完成时刻；
  2. 仅预留（未打印）的单：created_at 才回落到当前时刻（不能凭空编一个过去的时间）；
  3. 离线库补报（`offline_sync._upload_order_detail`）payload 也带 client_id，且 created_at
     用它记录的打印时刻；
  4. `_report_job_result(success=True)` 要把 `finished_at` 写进任务并持久化（否则第 1 条没素材）。

    python tests/verify_order_attribution_sync.py     # exit 0 = 全绿
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_attribution_")
os.environ["APPDATA"] = _SANDBOX                 # 用户数据目录 → 沙箱（.client_id 不落真机）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths                       # noqa: E402
from printer_config import PrintJob  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ── 出网拦截：GET 取号返回固定新号，POST 全记录（不发真请求）──
class _Resp:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self.ok = ok
        self.text = json.dumps(payload, ensure_ascii=False)
        self.status_code = 200 if ok else 500

    def json(self):
        return self._payload


class _HttpRecorder:
    def __init__(self):
        self.posts = []
        self.gets = []
        self.next_order_number = "HN20261102-0011"

    def get(self, url, params=None, timeout=None, **kw):
        self.gets.append({"url": url, "params": params, "headers": kw.get("headers")})
        if str(url).endswith("/api/next_order_number"):
            return _Resp({"success": True, "order_number": self.next_order_number})
        return _Resp({"success": True})

    def post(self, url, params=None, json=None, timeout=None, **kw):
        self.posts.append({"url": url, "params": params, "json": json,
                           "headers": kw.get("headers")})
        return _Resp({"success": True})


HTTP = _HttpRecorder()

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

import gui as _gui  # noqa: E402

_gui.http_requests = HTTP          # 必须早于 MainWindow（它会构造 CloudClient/推价）
_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

from gui import MainWindow  # noqa: E402

CFG_PATH = paths.config_path()
win = MainWindow(config_path=CFG_PATH)
win.show()


class _FakeClient:
    """只实现补报路径用到的接口；云端**当作已连接**（这正是现场 12:15:15 的状态）。"""

    def __init__(self):
        self.api_url = "https://example.test"
        self.token = "tok"
        self.auth_token = "tok"
        self.device_token = ""
        self.client_id = "pc-test-abc123"
        self._connected = True
        self.calls = []
        self.last_printer_state = {}
        self.take_orders = False

    def is_connected(self):
        return self._connected

    def __getattr__(self, name):
        def _noop(*a, **k):
            self.calls.append((name, a))
        return _noop


FAKE = _FakeClient()
win._cloud_client = FAKE


def wait_posts(count, timeout=6.0):
    deadline = time.time() + timeout
    while len(HTTP.posts) < count and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    return len(HTTP.posts) >= count


def local_order_posts():
    return [p for p in HTTP.posts if str(p["url"]).endswith("/api/local_orders")]


print("=== 1. 已打印的离线单换号补报：带接单设备 + 真实打印时刻 ===")
tab = win._config.tabs.get(win._current_tab)
tab.owner_name = "乙成员"
job = PrintJob(
    file_path=os.path.join(_SANDBOX, "示波器的使用(1).pdf"),
    copies=3, duplex="on", page_count=6,
    order_number="HN20261102-L0005",
    sent=True, finished_at="2026-11-02 12:14:47",
)
tab.jobs = [job]
win._set_current_jobs([job])

HTTP.posts.clear()
HTTP.gets.clear()
win._sync_local_orders_to_cloud()
wait_posts(1)

check("换号补报前确实向云端取了新订单号",
      any(str(g["url"]).endswith("/api/next_order_number") for g in HTTP.gets),
      json.dumps([g["url"] for g in HTTP.gets]))
posts = local_order_posts()
check("换号补报打到 /api/local_orders", len(posts) == 1,
      json.dumps([p["url"] for p in HTTP.posts]))
body = posts[0]["json"] if posts else {}
check("★ 补报改用云端正式号", body.get("order_number") == "HN20261102-0011",
      str(body.get("order_number")))
check("★ 补报带接单设备 client_id（修复前缺失 ⇒ 结算页「接单设备」永远显示 —）",
      body.get("client_id") == "pc-test-abc123", repr(body.get("client_id")))
check("★ created_at = 任务真实打印完成时刻（不是补报那一刻）",
      body.get("created_at") == "2026-11-02 12:14:47", str(body.get("created_at")))
check("已打印 ⇒ 状态 sent", body.get("status") == "sent", str(body.get("status")))
check("文件明细照旧（1 个文件 / 3 份 / 6 页）",
      len(body.get("files") or []) == 1
      and (body.get("files") or [{}])[0].get("copies") == 3
      and (body.get("files") or [{}])[0].get("page_count") == 6,
      json.dumps(body.get("files"), ensure_ascii=False))
check("价格照旧（¥3.60 / 3 份双面 6 页 @ on 价）",
      isinstance(body.get("total_price"), (int, float)) and body["total_price"] > 0,
      str(body.get("total_price")))
check("身份照旧（owner_name / is_admin_print）",
      body.get("owner_name") == "乙成员" and body.get("is_admin_print") is True,
      f"{body.get('owner_name')}/{body.get('is_admin_print')}")
check("打印机 token 仍走请求头",
      (posts[0]["headers"] or {}).get("X-Printer-Token") == "tok" if posts else False,
      json.dumps(posts[0]["headers"] if posts else None))

print("=== 2. 仅预留（还没打印）的离线单：created_at 才回落到当前时刻 ===")
job2 = PrintJob(
    file_path=os.path.join(_SANDBOX, "只复制了价格.pdf"),
    copies=1, duplex="off", page_count=2,
    order_number="HN20261102-L0006",
    sent=False, finished_at="",     # 没打印 ⇒ 没有真实完成时刻
)
tab.jobs = [job2]
win._set_current_jobs([job2])

HTTP.posts.clear()
HTTP.next_order_number = "HN20261102-0012"
win._sync_local_orders_to_cloud()
wait_posts(1)
posts2 = local_order_posts()
body2 = posts2[0]["json"] if posts2 else {}
check("未打印 ⇒ 状态仍为 reserved（不被误标成已完成）",
      body2.get("status") == "reserved", str(body2.get("status")))
check("未打印 ⇒ 也带 client_id", body2.get("client_id") == "pc-test-abc123",
      repr(body2.get("client_id")))
_now = time.strftime("%Y-%m-%d %H:%M:%S")
check("未打印 ⇒ created_at 为当前时刻（±180s 内）",
      bool(re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$", str(body2.get("created_at") or "")))
      and abs(time.mktime(time.strptime(str(body2["created_at"]), "%Y-%m-%d %H:%M:%S"))
              - time.mktime(time.strptime(_now, "%Y-%m-%d %H:%M:%S"))) <= 180,
      f"{body2.get('created_at')} vs now {_now}")

print("=== 3. 打印成功回报时写入 finished_at（第 1 条的时间素材来源）===")
job3 = PrintJob(file_path=os.path.join(_SANDBOX, "x.pdf"), copies=1, page_count=1,
                order_number="HN20261102-0099")
win._report_job_result(job3, True, "ok")
check("成功 ⇒ sent=True", job3.sent is True, str(job3.sent))
check("成功 ⇒ finished_at 是合法时间戳",
      bool(re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$", job3.finished_at or "")),
      repr(job3.finished_at))
_rt = PrintJob.from_dict(job3.to_dict())
check("finished_at 随配置持久化（重载不丢，否则补报又退回 datetime.now()）",
      _rt.finished_at == job3.finished_at, f"{_rt.finished_at} vs {job3.finished_at}")
check("旧配置（无 finished_at 键）读取不炸、默认空串",
      PrintJob.from_dict({"file_path": "a.pdf"}).finished_at == "",
      repr(PrintJob.from_dict({"file_path": "a.pdf"}).finished_at))
_before = job3.finished_at
app.processEvents()
win._report_job_result(job3, True, "ok")
check("重复回报不覆盖首次完成时刻（幂等）", job3.finished_at == _before,
      f"{job3.finished_at} vs {_before}")

print("=== 4. 离线库补报（offline_sync）也带 client_id + 原打印时刻 ===")
import offline_sync as _offline  # noqa: E402

_OFF_HTTP = _HttpRecorder()
_offline.http_requests = _OFF_HTTP          # 只影响本模块内的补报请求

osy = _offline.OfflineSync(db_path=os.path.join(_SANDBOX, "offline_orders.db"))
osy.client_id = "pc-test-abc123"
osy.save_order_offline(
    "HN20261101-7777",
    [{"file_name": "a.pdf", "copies": 2, "page_count": 3, "cost": 1.2,
      "duplex": "on", "page_range": ""}],
    total_price=1.2, created_at="2026-11-01 10:00:00",
    owner_name="乙成员", is_admin_print=True, remark="离线打的",
)
_n = osy.sync_all_pending_orders("https://example.test", "tok")
check("离线单已上传成功", _n == 1, f"synced={_n}")
_p = [p for p in _OFF_HTTP.posts if str(p["url"]).endswith("/api/local_orders")]
_pb = _p[0]["json"] if _p else {}
check("★ 离线补报带 client_id", _pb.get("client_id") == "pc-test-abc123",
      repr(_pb.get("client_id")))
check("离线补报的 created_at 用库里记的打印时刻（不被改写）",
      _pb.get("created_at") == "2026-11-01 10:00:00", str(_pb.get("created_at")))
check("离线补报其它字段照旧（owner/remark/明细）",
      _pb.get("owner_name") == "乙成员" and _pb.get("remark") == "离线打的"
      and len(_pb.get("files") or []) == 1,
      json.dumps({k: _pb.get(k) for k in ("owner_name", "remark")}, ensure_ascii=False))

print("=== 5. 本机 client_id：未启用云端 / 未连接时也要拿得到（离线补报的前提）===")
_win2 = MainWindow(config_path=os.path.join(_SANDBOX, "cfg2.json"))
_win2.show()
check("云端未启用时 _cloud_client 为 None（沙箱默认配置）", _win2._cloud_client is None,
      type(_win2._cloud_client).__name__)
_id_offline = _win2._local_client_id()
check("★ 无云端连接时 _local_client_id() 仍返回稳定 ID（hostname-后缀）",
      bool(_id_offline) and "-" in _id_offline, repr(_id_offline))
check("每次调用一致（不随机变，否则云端会多出一台设备）",
      _win2._local_client_id() == _id_offline, repr(_win2._local_client_id()))
check("离线库实例已注入同一个 client_id",
      getattr(_win2._offline_sync, "client_id", "") == _id_offline,
      repr(getattr(_win2._offline_sync, "client_id", "")))
check("插着云端时优先用 CloudClient 的 client_id",
      win._local_client_id() == "pc-test-abc123", repr(win._local_client_id()))
_win2.close()
app.processEvents()

print("=== 6. 收尾 ===")
win.close()
app.processEvents()
shutil.rmtree(_SANDBOX, ignore_errors=True)
print("\n" + ("全部通过 ✅" if ok_all else "存在失败项 ❌"))
sys.exit(0 if ok_all else 1)
