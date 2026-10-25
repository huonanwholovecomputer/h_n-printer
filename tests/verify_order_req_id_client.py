# -*- coding: utf-8 -*-
"""验收 · 本地工具侧「取号幂等键」接线（2026-10-25 现场：云端凭空多出空「已预留」订单）

配合后端 `tests/verify_order_number_idempotent.py`：这边验客户端真的把同一把钥匙
（`TabSettings.order_req_id`）带在**每一次**可能建占位单的取号请求上，并在重连换号时复用，
拿回当初那个号。

现场链路（local_tool.log 2026-10-25）：
  13:07:27 取号响应 TLS 丢包（服务端已建 HN20261025-0003）→ 客户端超时回落本地号
  13:17:01 重连换号又申请到 HN20261025-0004 ⇒ 云端留下空的「已预留」0003

本脚本钉住四件事：
  ① 键按标签页生成、随配置持久化（重启后重试还是同一把）；
  ② 「复制价格」取号（_ensure_order_number）与「开始打印」取号
     （_acquire_order_number_and_upload_net）都带 request_id；
  ③ 重连换号（_sync_local_orders_to_cloud）用**该本地号所属标签页**的那把键，
     而不是新生成一把（新生成 = 后端又建一行空预留）；离线库孤儿单没有键，不能瞎编；
  ④ 本地临时号（-L / LOCAL-）**绝不**走 /api/local_orders 预留上报（否则云端会多出一条
     "号长得像本地号"的订单）。

用法: PYTHONPATH= PYTHONUTF8=1 python.exe tests/verify_order_req_id_client.py
      exit 0 = 全绿
"""
import io
import json
import os
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_reqid_client_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from printer_config import PrinterConfig, PrintJob, TabSettings  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ============================================================
print("=== ① 幂等键随标签页持久化（配置 round-trip）===")
# 反向验证友好：字段不存在时记 FAIL，而不是抛 TypeError 糊掉判据
_serialize_ok = True
try:
    tab = TabSettings(order_req_id="req-roundtrip")
    d = tab.to_dict()
except TypeError as e:
    _serialize_ok = False
    d = {}
    print(f"    （TabSettings 没有 order_req_id 字段: {e}）")
check("★ 标签页设置带 order_req_id 字段", _serialize_ok and "order_req_id" in d, f"{d}")
check("to_dict 带 order_req_id", d.get("order_req_id") == "req-roundtrip", f"{d.get('order_req_id')}")
check("from_dict 还原 order_req_id",
      getattr(TabSettings.from_dict(d), "order_req_id", None) == "req-roundtrip", "")
check("老配置（没有这个字段）默认空字符串",
      getattr(TabSettings.from_dict({"jobs": []}), "order_req_id", "<missing>") == "", "")

# ============================================================
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

app = QApplication.instance() or QApplication([])
# 建窗口期间不弹阻塞框（本脚本自己不会触发业务弹窗）
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

import gui  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.ok = True
        self.status_code = 200
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _HttpRecorder:
    """记录所有请求；取号按 request_id 返回"服务端当初那个号"。"""

    def __init__(self):
        self.gets = []
        self.posts = []
        self.next_numbers = []     # 依次返回的号（取号请求）
        self.by_request_id = {}    # request_id → 号（模拟服务端幂等复用）

    def get(self, url, params=None, timeout=None, **kw):
        self.gets.append({"url": str(url), "params": dict(params or {}),
                          "headers": kw.get("headers")})
        if str(url).endswith("/api/next_order_number"):
            rid = (params or {}).get("request_id")
            if rid and rid in self.by_request_id:
                return _Resp({"success": True, "order_number": self.by_request_id[rid],
                              "reused": True})
            num = self.next_numbers.pop(0) if self.next_numbers else "HN20261025-0001"
            if rid:
                self.by_request_id[rid] = num
            return _Resp({"success": True, "order_number": num})
        return _Resp({"success": True})

    def post(self, url, params=None, json=None, timeout=None, **kw):
        self.posts.append({"url": str(url), "params": params, "json": json,
                           "headers": kw.get("headers")})
        return _Resp({"success": True})


HTTP = _HttpRecorder()
gui.http_requests = HTTP

win = gui.MainWindow(config_path=paths.config_path())
win.show()

# ── 反向验证友好：旧版 gui.py 没有 _order_request_id / request_id 形参 ──────────────
# 缺失时**记 FAIL**（断言红），而不是抛 AttributeError/TypeError 把判据糊掉。
import inspect  # noqa: E402

_NET_HAS_REQ = "request_id" in inspect.signature(
    win._acquire_order_number_and_upload_net).parameters


def _new_req_id(tab):
    fn = getattr(win, "_order_request_id", None)
    return fn(tab) if callable(fn) else ""


def _tab(**kwargs):
    rid = kwargs.pop("order_req_id", None)
    t = TabSettings(**kwargs)
    if rid is not None:
        try:
            t.order_req_id = rid
        except Exception:
            pass
    return t


def _net_acquire(existing, files, total, extra, tab, created, request_id=""):
    kw = dict(existing_order=existing, files_data=files, total_price=total,
              extra_fields=extra, tab=tab, created_at=created)
    if _NET_HAS_REQ and request_id:
        kw["request_id"] = request_id
    return win._acquire_order_number_and_upload_net(**kw)


class _FakeClient:
    def __init__(self):
        self.api_url = "https://example.test"
        self.token = "tok"
        self.auth_token = "tok"
        self.device_token = ""
        self.client_id = "pc-test"
        self.last_printer_state = {}
        self.take_orders = False
        self._connected = True
        self.calls = []

    def is_connected(self):
        return self._connected

    def __getattr__(self, name):
        def _noop(*a, **k):
            self.calls.append((name, a))
            return None
        return _noop


print("\n=== ② 首次需要取号时生成键并落盘；再问一次是同一把 ===")
win._config.tabs = {"1": TabSettings(owner_name="甲成员")}
win._config.active_tab = "1"
win._current_tab = "1"
win._cloud_client = fake = _FakeClient()

_t = win._config.tabs["1"]
check("★ 客户端有取号幂等键生成器（_order_request_id）",
      callable(getattr(win, "_order_request_id", None)), "")
rid1 = _new_req_id(_t)
rid2 = _new_req_id(_t)
check("生成非空键", bool(rid1) and len(rid1) >= 16, f"{rid1!r}")
check("同一个标签页再取 → 同一个键（不会每次换）", rid1 == rid2, f"{rid1} / {rid2}")
_disk = json.load(open(paths.config_path(), encoding="utf-8"))
check("★ 键已写进配置文件（重启后重试仍用同一把 → 后端才能还回同一个号）",
      (_disk.get("tabs", {}).get("1", {}) or {}).get("order_req_id") == rid1,
      f"{(_disk.get('tabs', {}).get('1', {}) or {}).get('order_req_id')!r}")

print("\n=== ③ 「复制价格」取号带 request_id；已有订单号时不重复取号 ===")
win._config.tabs["1"].jobs = [PrintJob(file_path=os.path.join(_SANDBOX, "a.pdf"), page_count=2)]
HTTP.gets.clear()
HTTP.next_numbers = ["HN20261025-0001"]
_GET_BEFORE = len(HTTP.gets)
num = win._ensure_order_number()
_gets = [g for g in HTTP.gets[_GET_BEFORE:] if g["url"].endswith("/api/next_order_number")]
check("取到云端号", num == "HN20261025-0001", f"{num}")
check("★ 取号请求带 request_id（就是该标签页那把键）",
      bool(_gets) and _gets[0]["params"].get("request_id") == rid1,
      f"{_gets[0]['params'] if _gets else None}")
check("订单号写回 job", win._config.tabs["1"].jobs[0].order_number == num, "")
_n = len(HTTP.gets)
win._ensure_order_number()
check("再次调用直接用已有订单号（不再打取号接口，零回归）",
      len([g for g in HTTP.gets[_n:] if g["url"].endswith("/api/next_order_number")]) == 0, "")

print("\n=== ④ 「开始打印」取号（后台线程那条路）同样带 request_id ===")
HTTP.gets.clear()
HTTP.posts.clear()
HTTP.next_numbers = ["HN20261025-0002"]
HTTP.by_request_id.clear()
_rid = _new_req_id(win._config.tabs["1"])
out = _net_acquire(
    "", [{"file_name": "a.pdf", "copies": 1, "page_count": 2, "cost": 0.4,
          "duplex": "on", "page_range": ""}], 0.4, {}, win._config.tabs["1"],
    "2026-10-25 13:07:24", request_id=_rid)
check("返回云端号", out.get("order_number") == "HN20261025-0002", f"{out}")
check("★ 取号 URL 带 request_id", bool(HTTP.gets)
      and HTTP.gets[0]["params"].get("request_id") == _rid, f"{HTTP.gets[:1]}")
check("随后按这个号上报 /api/local_orders（status=sent）",
      bool(HTTP.posts) and HTTP.posts[0]["json"].get("order_number") == "HN20261025-0002"
      and HTTP.posts[0]["json"].get("status") == "sent", f"{HTTP.posts[:1]}")

print("\n=== ④b 上一轮留下了本地临时号：不当云端号上报，先取号（同一把键能拿回旧占位号）===")
HTTP.gets.clear()
HTTP.posts.clear()
HTTP.next_numbers = ["HN20261025-0003"]
HTTP.by_request_id = {"REQ-LOCAL": "HN20261025-0002"}   # 服务端上次已建好的那个占位单
out2 = _net_acquire(
    "HN20261025-L0007", [{"file_name": "b.pdf", "copies": 1, "page_count": 1,
                          "cost": 0.3, "duplex": "off", "page_range": ""}], 0.3, {},
    win._config.tabs["1"], "2026-10-25 13:07:24", request_id="REQ-LOCAL")
check("★ -L 号不作为最终订单号直接上报", out2.get("order_number") != "HN20261025-L0007", f"{out2}")
check("★ 而是带着取号键重新取号 → 拿回服务端已建好的那个号",
      out2.get("order_number") == "HN20261025-0002", f"{out2}")
check("并只按云端号上报一次（没有出现 -L 号那一条）",
      [p["json"].get("order_number") for p in HTTP.posts] == ["HN20261025-0002"],
      f"{[p['json'].get('order_number') for p in HTTP.posts]}")

print("\n=== ⑤ 重连换号：用**本地号所属标签页**那把键 → 后端还回同一个号 ===")
win._config.tabs = {"3": _tab(
    owner_name="甲成员", is_admin_print=False,
    jobs=[PrintJob(file_path=os.path.join(_SANDBOX, "b.pdf"), page_count=2,
                   order_number="HN20261025-L0007")],
    order_req_id="REQ-L7")}
win._config.active_tab = "3"
win._current_tab = "3"
win._offline_sync = None
fake._connected = True
HTTP.gets.clear()
HTTP.posts.clear()
HTTP.next_numbers = ["HN20261025-9999"]        # 万一没带键才会用到它
HTTP.by_request_id = {"REQ-L7": "HN20261025-0003"}
win._sync_local_orders_to_cloud()
_gets = [g for g in HTTP.gets if g["url"].endswith("/api/next_order_number")]
_posts = [p for p in HTTP.posts if p["url"].endswith("/api/local_orders")]
check("★ 换号请求带的是原标签页那把键（不是新生成的）",
      bool(_gets) and _gets[0]["params"].get("request_id") == "REQ-L7",
      f"{_gets[0]['params'] if _gets else None}")
check("★ 拿回的是服务端当初建好的号（现场 0003 那种），不是又新申请一个",
      win._config.tabs["3"].jobs[0].order_number == "HN20261025-0003",
      f"{win._config.tabs['3'].jobs[0].order_number}")
check("并按新号上报（预留：该单尚未打印）",
      bool(_posts) and _posts[0]["json"].get("order_number") == "HN20261025-0003"
      and _posts[0]["json"].get("status") == "reserved", f"{_posts[:1]}")

print("\n=== ⑥ 离线库孤儿单（标签页已清理，没有键）：不许瞎编 request_id ===")


class _FakeOffline:
    def list_pending_orders(self, like=""):
        return [(9, "HN20261025-L0009", "[]", 0.0, "2026-10-25 12:00:00", "乙成员", 0, "")] \
            if like else []

    def rename_order_number(self, old, new):
        pass

    def mark_synced(self, num):
        pass

    def find_pending_by_number(self, num):
        return (9, num, json.dumps([{"file_name": "c.pdf", "copies": 1, "page_count": 1,
                                     "cost": 0.3, "duplex": "off", "page_range": ""}]),
                0.3, "2026-10-25 12:00:00", "乙成员", 0, "")


win._config.tabs = {"4": TabSettings(jobs=[])}       # 目标标签页里没有这个本地号的 job
win._current_tab = "4"
win._offline_sync = _FakeOffline()
HTTP.gets.clear()
HTTP.posts.clear()
HTTP.next_numbers = ["HN20261025-0010"]
HTTP.by_request_id = {}
win._sync_local_orders_to_cloud()
_gets = [g for g in HTTP.gets if g["url"].endswith("/api/next_order_number")]
check("★ 孤儿单的换号请求不带 request_id（没有归属标签页就不能编一把键）",
      bool(_gets) and not _gets[0]["params"].get("request_id"),
      f"{_gets[0]['params'] if _gets else None}")
check("孤儿单照旧换号上报（零回归）",
      any(p["json"].get("order_number") == "HN20261025-0010" for p in HTTP.posts),
      f"{[p['json'].get('order_number') for p in HTTP.posts]}")

print("\n=== ⑦ 本地临时号绝不做预留上报（否则云端多一条怪号订单）===")
win._config.tabs = {"5": TabSettings(
    owner_name="甲成员",
    jobs=[PrintJob(file_path=os.path.join(_SANDBOX, "d.pdf"), page_count=1,
                   order_number="HN20261025-L0011")])}
win._current_tab = "5"
win._config.active_tab = "5"
HTTP.posts.clear()
win._upload_reserved_order("HN20261025-L0011")
for _ in range(60):
    app.processEvents()
    time.sleep(0.01)
check("★ -L 号没有发 /api/local_orders", not HTTP.posts, f"{HTTP.posts}")
HTTP.posts.clear()
win._upload_reserved_order("HN20261025-0012")
for _ in range(160):
    app.processEvents()
    time.sleep(0.01)
check("云端号照旧预留上报（零回归）",
      bool(HTTP.posts) and HTTP.posts[0]["json"].get("status") == "reserved",
      f"{HTTP.posts[:1]}")

print("\n=== ⑧ 新标签页（复制到新标签页）不继承旧单的键 ===")
fresh = win._make_new_tab()
check("新标签页 order_req_id 为空（新单 = 新键）",
      getattr(fresh, "order_req_id", "<missing>") == "", f"{getattr(fresh, 'order_req_id', '<missing>')!r}")

print("\n" + "=" * 56)
print("客户端取号幂等键验收：" + ("全部通过 ✅" if ok_all else "有失败 ❌"))
sys.exit(0 if ok_all else 1)
