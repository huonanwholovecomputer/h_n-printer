# -*- coding: utf-8 -*-
"""钱与账：timeout 口径 / 离线状态重放 / 离线单失败回落 —— 三处「账单和纸对不上」。

对应体检报告 `docs/健壮性体检_2026-11-10.md` 的：
  🔴7  timeout 单在结算页算应收、后端 revenue 里是 0（口径双份，且不能人工订正）
  🔴22 离线状态重放只发 {"task_id"} → 丢 copies/duplex/page_range… → 实打单面按双面收
  🔴23 离线单本地打印失败，重连后固定上报 "sent" → 没出纸却记账

每条断言都要求「实现被改坏就会红」：
  · 前端 `_EXCLUDE_STATUS` 去掉 timeout → ① 红（node 实跑口径）
  · 后端口径去掉 timeout / 订正清单去掉 timeout → ② 红
  · `_queue_status_sync` 不存 payload 或重放只发 task_id → ③④ 红
  · `offline_sync` 不记 print_failed / gui 不回落上报 → ⑤ 红

跑法：
    PYTHONPATH= PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \
        C:/Users/Administrator/AppData/Local/Programs/Python/Python312/python.exe \
        other/tests/verify_money_alignment.py
"""
import os
import re
import sys
import json
import shutil
import tempfile
import subprocess

_SANDBOX = tempfile.mkdtemp(prefix="hn_money_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
SETTLE = os.path.join(LOCAL_TOOL, "finance", "settlement.html")
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend", "app.py")
NODE = r"C:/Program Files/nodejs/node.exe"
if not os.path.isfile(NODE):
    NODE = shutil.which("node") or "node"

sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

_ok = []
_fail = []


def check(name, cond, detail=""):
    if cond:
        _ok.append(name)
        print(f"  [PASS] {name}")
    else:
        _fail.append(name)
        print(f"  [FAIL] {name}" + (f"  ← {detail}" if detail else ""))


def read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import cloud_client as cc  # noqa: E402
import offline_sync  # noqa: E402

HTML = read(SETTLE)
SRC_GUI = read(os.path.join(LOCAL_TOOL, "gui.py"))
SRC_BE = read(BACKEND)

print("── ① 前端结算页：timeout 不算应收（与后端同一口径）──")
_m = re.search(r"var _EXCLUDE_STATUS = \{([^}]*)\}", HTML, re.S)
_map_src = _m.group(1) if _m else ""
check("①-a _EXCLUDE_STATUS 里有 timeout", "timeout" in _map_src, _map_src[:120])
_eval = (
    "const SRC = %s;\n"
    "const m = {};\n"
    "SRC.split(',').forEach(p => { const kv = p.split(':'); if (kv.length === 2) "
    "{ const k = kv[0].trim().replace(/['\"]/g,''); const v = kv[1].trim(); if (k) "
    "m[k] = Number(v); } });\n"
    "console.log(JSON.stringify({timeout: m['timeout'] || 0, failed: m['failed'] || 0, "
    "canceled: m['canceled'] || 0, sent: m['sent'] || 0}));\n"
    % json.dumps(_map_src)
)
_tmpjs = os.path.join(tempfile.gettempdir(), "hn_money_map.js")
with open(_tmpjs, "w", encoding="utf-8") as f:
    f.write(_eval)
try:
    _out = subprocess.run([NODE, _tmpjs], capture_output=True, text=True, timeout=30)
    _j = json.loads((_out.stdout or "{}").strip() or "{}")
except Exception as e:  # noqa: BLE001
    _j = {}
    print(f"    （node 跑不动：{type(e).__name__}: {e}）")
check("①-b 实跑口径：timeout 被排除、sent 不被排除",
      _j.get("timeout") == 1 and _j.get("failed") == 1
      and _j.get("canceled") == 1 and _j.get("sent") == 0, _j)

print("── ② 人工订正清单：timeout 不进（沿用既有口径），前后端必须同一份 ──")
_TERMINAL = ["sent", "failed", "abandoned", "canceled", "rejected"]
_fix = re.search(r"var ORDER_FIXABLE_STATUSES = \[([^\]]*)\]", HTML, re.S)
_fix_src = _fix.group(1) if _fix else ""
check("②-a 前端可订正清单去掉 timeout（timeout 是自动过期的终态，订正它等于伪造一次打印）",
      "timeout" not in _fix_src, _fix_src)
_be_fix = re.search(r"MANUAL_FIX_STATUSES = \(([^)]*)\)", SRC_BE, re.S)
_be_fix_src = _be_fix.group(1) if _be_fix else ""
check("②-b 后端可订正清单同样不含 timeout", "timeout" not in _be_fix_src, _be_fix_src)
check("②-b2 两份清单就是那五个终态（多一个少一个都不行）",
      sorted(re.findall(r"['\"]([a-z_]+)['\"]", _fix_src)) == sorted(_TERMINAL)
      and sorted(re.findall(r"['\"]([a-z_]+)['\"]", _be_fix_src)) == sorted(_TERMINAL),
      (sorted(re.findall(r"['\"]([a-z_]+)['\"]", _fix_src)),
       sorted(re.findall(r"['\"]([a-z_]+)['\"]", _be_fix_src))))
check("②-c 前端与后端清单逐项一致",
      sorted(re.findall(r"['\"]([a-z_]+)['\"]", _fix_src)) ==
      sorted(re.findall(r"['\"]([a-z_]+)['\"]", _be_fix_src)),
      (sorted(re.findall(r"['\"]([a-z_]+)['\"]", _fix_src)),
       sorted(re.findall(r"['\"]([a-z_]+)['\"]", _be_fix_src))))
check("②-d 后端 revenue 口径也排除 timeout",
      "timeout" in SRC_BE.split("elif f[\"status\"] not in", 1)[-1][:200]
      if "elif f[\"status\"] not in" in SRC_BE else False)

print("── ③ 离线状态队列：payload 必须随条目存下来 ──")
client = cc.CloudClient(api_url="", ws_url="", token="", client_id="verify-money")
client._queue_status_sync(0, "sent", task_id=4242,
                          payload={"copies": 1, "duplex": "off", "page_range": "1-2",
                                   "page_count": 2, "is_admin_print": False})
_q = client._read_status_queue_locked()
_item = [x for x in _q if x.get("task_id") == 4242]
check("③-a 入队成功", len(_item) == 1, len(_item))
check("③-b 条目里存了完整 payload（不只 task_id）",
      bool(_item) and (_item[0].get("payload") or {}).get("duplex") == "off",
      (_item[0] if _item else {}).get("payload"))
check("③-c payload 里 page_range/page_count 也在",
      bool(_item) and (_item[0].get("payload") or {}).get("page_range") == "1-2"
      and (_item[0].get("payload") or {}).get("page_count") == 2)

print("── ④ 重放：必须原样发回那份 payload（不能被剥成 task_id）──")
class _FakeSio:
    def __init__(self):
        self.sent = []

    def emit(self, event, data=None, **kw):
        self.sent.append((event, data))


client._sio = _FakeSio()
client._connected = True
client._last_replay_attempt = 0
client.sync_pending_statuses()
_sent = [(e, d) for (e, d) in client._sio.sent if d and d.get("task_id") == 4242]
check("④-a 真的重放了这条", len(_sent) == 1, client._sio.sent)
check("④-b 事件名是 print_success", bool(_sent) and _sent[0][0] == "print_success", _sent)
check("④-c 重放里带着 duplex=off / page_range / page_count（后端按实际配置记账）",
      bool(_sent) and _sent[0][1].get("duplex") == "off"
      and _sent[0][1].get("page_range") == "1-2"
      and _sent[0][1].get("page_count") == 2, _sent[:1])
check("④-d 重放后队列已清空（不会重复上报）",
      not [x for x in client._read_status_queue_locked() if x.get("task_id") == 4242])

print("── ⑤ 离线单本地打印失败：要记住，并在补报成功后回落成失败 ──")
db = os.path.join(_SANDBOX, "offline_test.db")
osync = offline_sync.OfflineSync(db_path=db)
osync._init_db()
_rid = osync.save_order_offline(
    "HN20261110-9999",
    [{"display_name": "a.docx", "page_count": 2, "copies": 1, "duplex": "on"}],
    0.4, "2026-11-10 16:00:00", owner_name="姚懿祥", is_admin_print=False)
check("⑤-a 离线单能存下来", _rid == "HN20261110-9999", _rid)
check("⑤-b 初始不算失败", osync.is_print_failed("HN20261110-9999") is False)
_okmark = osync.mark_print_failed("HN20261110-9999", "手动双面第 1 趟失败")
check("⑤-c mark_print_failed 返回成功", _okmark is True, _okmark)
check("⑤-d 之后读回是失败", osync.is_print_failed("HN20261110-9999") is True)
check("⑤-e 不存在的单号不误判", osync.is_print_failed("HN00000000-0000") is False)
check("⑤-f gui 的离线失败分支写了标记",
      "mark_print_failed(" in SRC_GUI and "is_print_failed(" in SRC_GUI)
check("⑤-g gui 补报成功后调用了回落上报",
      "_report_order_failed_after_sync(" in SRC_GUI)
check("⑤-h 回落上报的报文里有 status failed",
      re.search(r"def _report_order_failed_after_sync[\s\S]{0,1200}?\"status\": \"failed\"",
                SRC_GUI) is not None)

print()
print(f"PASS {len(_ok)}  FAIL {len(_fail)}")
if _fail:
    print("存在失败 ❌")
    sys.exit(1)
print("全部通过 ✅")
