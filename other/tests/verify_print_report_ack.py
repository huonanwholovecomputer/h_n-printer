# -*- coding: utf-8 -*-
"""上报成功/失败要有回执（🔴20 / 批次 D）

问题：`report_success` / `report_fail` 只 `_sio.emit(...)`，而 python-socketio 的 emit 是
「塞进发送缓冲就返回」—— 链路半开（后端重启、代理抖动）时它既不抛异常、服务端也收不到。
于是客户端以为报上去了，3 分钟后 `check_printing_timeout` 把这一单判 failed；
纸却已经被顾客拿走（`_processed_cloud_tasks` 早写过，迟到的重推会被无声吞掉）。

修法：
  · 服务端在 `on_print_success` / `on_print_fail` 末尾回 `print_success_ack` / `print_fail_ack`；
  · 客户端 emit 成功后登记 `_pending_ack[task_id]`，收到 ack 就撤掉；
  · 15 秒没 ack ⇒ 当作没送到，把整份 payload（含 copies/duplex/page_range/page_count）
    转进离线队列，重连后重放。

用法: python other/tests/verify_print_report_ack.py   （exit 0 = 全部通过）
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
APP_PY = os.path.join(REPO, "mobile_apps", "printer-backend", "app.py")

_SANDBOX = tempfile.mkdtemp(prefix="hn_ack_")
os.environ["APPDATA"] = _SANDBOX
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import cloud_client as cc  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


class FakeSio:
    """只会"发送成功"的假 socket：服务端其实什么都没收到（半开链路的等价物）。"""

    def __init__(self):
        self.sent = []

    def emit(self, event, data=None, **kw):
        self.sent.append((event, data))
        return None


def make_client():
    c = cc.CloudClient(api_url="", ws_url="", token="", client_id="ack-test")
    c._sio = FakeSio()
    c._connected = True
    try:
        os.remove(c._status_queue_path())
    except OSError:
        pass
    return c


def read_queue(c):
    p = c._status_queue_path()
    if not os.path.isfile(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════
print("\n=== 1. 服务端：两个回报处理函数都回 ack ===")
_src = open(APP_PY, encoding="utf-8", errors="replace").read()


def body_of(fn, src):
    i = src.find(f"def {fn}(")
    if i < 0:
        return ""
    j = src.find("\n@sio_event(", i)
    k = src.find("\ndef ", i + 10)
    end = min(x for x in (j, k, len(src)) if x > 0)
    return src[i:end]


_suc = body_of("on_print_success", _src)
_fail = body_of("on_print_fail", _src)
check("1-a on_print_success 回 print_success_ack", 'emit("print_success_ack"' in _suc)
check("1-b on_print_fail 回 print_fail_ack", 'emit("print_fail_ack"' in _fail)
check("1-c ack 里带 task_id", '{"task_id": task_id}' in _suc and '{"task_id": task_id}' in _fail)

print("\n=== 2. 上报成功后登记待回执（还没收到 ack 不算完）===")
c1 = make_client()
c1.report_success(4242, {"copies": 2, "duplex": "off", "page_range": "1-2",
                        "page_count": 2})
check("2-a 已发出 print_success 事件",
      any(e == "print_success" for e, _ in c1._sio.sent), c1._sio.sent)
check("2-b 登记了待回执", 4242 in c1._pending_ack, list(c1._pending_ack))
check("2-c 待回执里存了完整 payload",
      (c1._pending_ack.get(4242, {}).get("payload") or {}).get("duplex") == "off",
      c1._pending_ack.get(4242))
check("2-d 此时还没进离线队列（先给云端机会）", read_queue(c1) == [], read_queue(c1))

print("\n=== 3. 收到 ack ⇒ 撤掉待回执，不再重放 ===")
c2 = make_client()
c2.report_success(777, {"duplex": "off"})
c2._clear_pending_ack(777)
time.sleep(0.1)
check("3-a 待回执已清空", 777 not in c2._pending_ack, list(c2._pending_ack))
check("3-b 离线队列仍然是空的", read_queue(c2) == [], read_queue(c2))
check("3-c ack 里没带 task_id 也不炸", (c2._clear_pending_ack(None) is None))

print("\n=== 4. 15 秒没 ack ⇒ 整份 payload 进离线队列（重放时才不会丢配置）===")
c3 = make_client()
c3.ACK_TIMEOUT_SEC = 0.05          # 缩短等待，测试跑得快
c3.report_success(9001, {"copies": 1, "duplex": "off", "page_range": "3",
                        "page_count": 1, "owner_name": "张三", "is_admin_print": 0})
time.sleep(0.45)
_q = read_queue(c3)
_item = _q[-1] if _q else {}
check("4-a 离线队列落了一条", len(_q) == 1, len(_q))
check("4-b 状态是 sent", _item.get("status") == "sent", _item.get("status"))
check("4-c 带 task_id", int(_item.get("task_id") or 0) == 9001, _item.get("task_id"))
_pl = _item.get("payload") or {}
check("4-d payload 里 duplex 没丢", _pl.get("duplex") == "off", _pl)
check("4-e payload 里 owner_name 没丢", _pl.get("owner_name") == "张三", _pl)
check("4-f 待回执已出队（不会重复重放）", 9001 not in c3._pending_ack, list(c3._pending_ack))

print("\n=== 5. 失败回报同理 ===")
c4 = make_client()
c4.ACK_TIMEOUT_SEC = 0.05
c4.report_fail(9002, "卡纸")
time.sleep(0.45)
_q4 = read_queue(c4)
_i4 = _q4[-1] if _q4 else {}
check("5-a 队列里是 failed", _i4.get("status") == "failed", _i4.get("status"))
check("5-b 失败原因保留", ((_i4.get("payload") or {}).get("error")) == "卡纸",
      _i4.get("payload"))

print("\n=== 6. 假回执（别的 task_id）不会误清当前这条 ===")
c5 = make_client()
c5.report_success(5150, {"duplex": "on"})
c5._clear_pending_ack(9999)
check("6-a 5150 仍在等回执", 5150 in c5._pending_ack, list(c5._pending_ack))

print("\n" + "=" * 60)
if ok_all:
    print("上报回执验收: 全部通过")
    sys.exit(0)
print("上报回执验收: 存在失败 ❌")
sys.exit(1)
