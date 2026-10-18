# -*- coding: utf-8 -*-
"""验收脚本 · 测试设备清理（后端识别/隐藏/批量清理/定时清扫 + 授权页按钮）

**现场问题（2026-10）**：联调与部署冒烟脚本直连生产注册设备，`devices.json` 攒了 48 台
`socketio-debug-*` / `debug-*` 的"测试设备"，授权页一屏全是它们，其中还有僵尸「接单中」记录
（离线却仍在 claiming_devices → `get_active_printer_client()` 可能挑到一台永远不会干活的机器）。

本脚本在临时目录复制 printer-backend 后导入 app.py（不碰真实 orders.db/devices.json），钉住：
  ① **识别**只认明确前缀：真机 id 形如 `<计算机名>-<8~12 位十六进制>`，一律不误伤；
  ② **列表接口隐藏**测试设备并回报 `hidden_test_devices`（真机与在线状态照旧）；
  ③ **批量清理接口**：默认 `dry_run`（只列不删）、**跳过在线设备**、删除时同步清空接单记录；
  ④ **定时任务** `purge_test_devices` 自动清掉离线的测试设备（在线不动）；
  ⑤ 现场两台"灰区"设备（`DEMOTEST2` 之外的 `vm-demo-cloud-...`：无主、离线、名字不含测试前缀）
     由**人工触发的** `include_offline_unbound` 选项处理，不自动删（避免误伤真机）；
  ⑥ 授权页按钮与后端版本记录静态接线。

    python tests/verify_device_cleanup.py     # exit 0 = 全绿
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

_tmp = tempfile.mkdtemp(prefix="hn_devclean_")
_dst = os.path.join(_tmp, "printer-backend")
shutil.copytree(SRC, _dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs"))
sys.path.insert(0, _dst)
os.chdir(_dst)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


try:
    import app as backend
except Exception as e:
    print(f"✗ 无法导入 app.py: {e}")
    sys.exit(1)

TOKEN = "test-token"
backend.PRINTER_TOKEN = TOKEN
HDR = {"X-Printer-Token": TOKEN}

# ── 造一份"现场那样"的注册表 ──
REAL_IDS = ["PC-DEMO-1a2b3c4d5e6f", "DESKTOP-DEMO-a1b2c3d4e5"]
JUNK_OFFLINE = ["debug-None0", "debug-websocket3", "debug-polling11", "socketio-debug-test",
                "debug-profile-True-websocket", "debug-envclear", "debug-direct-poll-deadproxy",
                "deploy-check-abc123", "wscheck-http-heartbeat"]
ONLINE_JUNK = "debug-websocket5"          # 测试设备，但**在线**：自动清理必须跳过
GRAY_OLD = "vm-demo-cloud-6a056839c3"     # 无主 + 长期离线（灰区）
GRAY_NEW = "vm-demo-cloud-9f9f9f9f9f9f"   # 无主但**刚离线**（不该清）
OWNED_OFFLINE = "SOMEONE-PC-deadbeef"                            # 有主 + 长期离线（不该清）

old_seen = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
now_seen = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
recent_seen = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
devices = {}
for cid in REAL_IDS:
    devices[cid] = {"device_name": cid, "owner_name": "甲成员", "first_seen": old_seen,
                    "last_seen": now_seen}
for cid in JUNK_OFFLINE + [ONLINE_JUNK]:
    devices[cid] = {"device_name": "debug", "owner_name": "", "first_seen": old_seen,
                    "last_seen": old_seen}
devices[GRAY_OLD] = {"device_name": GRAY_OLD, "owner_name": "", "first_seen": old_seen,
                     "last_seen": old_seen}
devices[GRAY_NEW] = {"device_name": GRAY_NEW, "owner_name": "", "first_seen": now_seen,
                     "last_seen": recent_seen}
devices[OWNED_OFFLINE] = {"device_name": OWNED_OFFLINE, "owner_name": "乙成员",
                          "first_seen": old_seen, "last_seen": old_seen}
# 人工盖过章的设备（无主 + 长期离线）：那是一条人工结论，**不能**被批量清理删掉
STAMPED_OFFLINE = "STAMPED-PC-cafebabe"
devices[STAMPED_OFFLINE] = {"device_name": STAMPED_OFFLINE, "owner_name": "",
                            "first_seen": old_seen, "last_seen": old_seen,
                            "printer": {"manual_auto_duplex": False, "manual_at": old_seen}}
json.dump(devices, open("devices.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
# 僵尸接单记录：真实设备 + 测试设备 + 灰区设备都挂着"接单中"（现场就是这个样子）
json.dump({"claiming_devices": {cid: {} for cid in
                                REAL_IDS + JUNK_OFFLINE + [ONLINE_JUNK, GRAY_OLD]}},
          open("printer_claim.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# 让 ONLINE_JUNK 真的"在线"（心跳新鲜）
with backend.printer_clients_lock:
    backend.printer_clients[ONLINE_JUNK] = {
        "sid": "sid-online", "heartbeat": datetime.now(), "connected_at": datetime.now()}

client = backend.app.test_client()

# ============================================================
# ① 识别规则：真机不误伤
# ============================================================
print("\n=== ① 测试设备识别 ===")
for cid in JUNK_OFFLINE + [ONLINE_JUNK]:
    check(f"识别为测试设备: {cid}", backend.is_test_device_id(cid) is True)
for cid in REAL_IDS + [GRAY_OLD, GRAY_NEW, OWNED_OFFLINE]:
    check(f"真机/灰区不误判: {cid}", backend.is_test_device_id(cid) is False)
check("空串/None 安全", backend.is_test_device_id("") is False
      and backend.is_test_device_id(None) is False)

# ============================================================
# ② 列表接口：隐藏测试设备 + 计数
# ============================================================
print("\n=== ② /api/printer/devices 隐藏测试设备 ===")
r = client.get("/api/printer/devices", headers=HDR)
d = r.get_json() or {}
ids = [x["client_id"] for x in d.get("devices", [])]
check("200 + success", r.status_code == 200 and d.get("success") is True, f"{r.status_code}")
check("★ 测试设备不出现在列表里（10 台全隐藏）",
      not any(backend.is_test_device_id(c) for c in ids), f"{ids}")
check("★ hidden_test_devices 计数正确（9 离线 + 1 在线 = 10）",
      d.get("hidden_test_devices") == len(JUNK_OFFLINE) + 1,
      f"{d.get('hidden_test_devices')}")
check("真机与灰区设备仍在列表里", set(REAL_IDS) <= set(ids)
      and GRAY_OLD in ids and OWNED_OFFLINE in ids, f"{ids}")
check("真机的所有者/接单状态照旧", 
      all(x["owner_name"] == "甲成员" for x in d["devices"] if x["client_id"] in REAL_IDS)
      and all(x["is_active"] for x in d["devices"] if x["client_id"] in REAL_IDS))
check("无 token → 403", client.get("/api/printer/devices").status_code == 403)

# ============================================================
# ③ 批量清理：dry_run 只列不删 / 跳过在线 / 清接单记录
# ============================================================
print("\n=== ③ /api/printer/devices/cleanup ===")
r = client.post("/api/printer/devices/cleanup", headers=HDR, json={})
d = r.get_json() or {}
check("★ 默认 dry_run=True（防误点，不真删）", d.get("dry_run") is True)
check("★ dry_run 列出 9 台离线测试设备", d.get("count") == len(JUNK_OFFLINE),
      f"count={d.get('count')}")
check("★ 在线测试设备被跳过并列在 skipped_online",
      d.get("skipped_online") == [ONLINE_JUNK], f"{d.get('skipped_online')}")
check("dry_run 不动文件（10 台测试设备还在）",
      sum(1 for c in json.load(open("devices.json", encoding="utf-8"))
          if backend.is_test_device_id(c)) == len(JUNK_OFFLINE) + 1)
check("dry_run 不碰接单记录", ONLINE_JUNK in (json.load(open("printer_claim.json", encoding="utf-8"))
                                          .get("claiming_devices") or {}))

r = client.post("/api/printer/devices/cleanup", headers=HDR, json={"dry_run": False})
d = r.get_json() or {}
check("★ 真删：removed = 9", d.get("dry_run") is False and d.get("removed") == len(JUNK_OFFLINE),
      f"removed={d.get('removed')}")
left = json.load(open("devices.json", encoding="utf-8"))
check("★ 离线测试设备已删除，在线测试设备保留", 
      all(c not in left for c in JUNK_OFFLINE) and ONLINE_JUNK in left)
check("★ 真机与灰区设备一个没少",
      all(c in left for c in REAL_IDS + [GRAY_OLD, GRAY_NEW, OWNED_OFFLINE]))
claiming = (json.load(open("printer_claim.json", encoding="utf-8")).get("claiming_devices") or {})
check("★ 被删设备的僵尸接单记录一并清空", all(c not in claiming for c in JUNK_OFFLINE))
check("真机与在线测试设备的接单记录保留（在线设备不打断）",
      all(c in claiming for c in REAL_IDS) and ONLINE_JUNK in claiming
      and GRAY_OLD in claiming, f"{sorted(claiming)}")
check("清理后列表里没有测试设备了（只隐藏 1 台在线的）",
      client.get("/api/printer/devices", headers=HDR).get_json()
      .get("hidden_test_devices") == 1)

# ============================================================
# ④ 灰区设备：必须人工显式开启才清（不自动误伤真机）
# ============================================================
print("\n=== ④ include_offline_unbound（人工选项）===")
r = client.post("/api/printer/devices/cleanup", headers=HDR,
                json={"dry_run": True, "include_offline_unbound": True, "min_offline_days": 7})
d = r.get_json() or {}
check("★ 只挑出「无主 + 长期离线」的灰区设备（刚离线的/有主的/盖过章的都不算）",
      d.get("client_ids") == [GRAY_OLD], f"{d.get('client_ids')}")
check("默认（不带该选项）不碰灰区设备",
      (client.post("/api/printer/devices/cleanup", headers=HDR, json={}).get_json()
       .get("client_ids") or []) == [],
      "离线测试设备已在上一步清完")
# min_offline_days=0：连"今天才连过"的无主设备也能清 —— 正是现场那 2 台云主机客户端的情形
r0 = client.post("/api/printer/devices/cleanup", headers=HDR,
                 json={"dry_run": True, "include_offline_unbound": True, "min_offline_days": 0})
ids0 = r0.get_json().get("client_ids") or []
check("★ min_offline_days=0：今天才连过的无主离线设备也进名单（现场那 2 台）",
      set(ids0) == {GRAY_OLD, GRAY_NEW}, f"{ids0}")
check("★ 盖过章的 / 已绑定所有者的 / 在线的，永远不进名单",
      STAMPED_OFFLINE not in ids0 and OWNED_OFFLINE not in ids0 and ONLINE_JUNK not in ids0,
      f"stamped={STAMPED_OFFLINE in ids0} owned={OWNED_OFFLINE in ids0}")
r = client.post("/api/printer/devices/cleanup", headers=HDR,
                json={"dry_run": False, "include_offline_unbound": True, "min_offline_days": 7})
check("打开开关后灰区设备被清掉，有主的长期离线设备保留",
      GRAY_OLD not in json.load(open("devices.json", encoding="utf-8"))
      and OWNED_OFFLINE in json.load(open("devices.json", encoding="utf-8")))
check("★ 人工盖过章的设备即使无主又长期离线也不会被清（人工结论不能自动丢）",
      STAMPED_OFFLINE in json.load(open("devices.json", encoding="utf-8")))
check("无 token → 403", client.post("/api/printer/devices/cleanup",
                                    json={"dry_run": False}).status_code == 403)

# ============================================================
# ⑤ 定时清扫：自动清离线测试设备，在线不动
# ============================================================
print("\n=== ⑤ purge_test_devices（定时）===")
check("在线的测试设备不会被自动清（还在）",
      ONLINE_JUNK in json.load(open("devices.json", encoding="utf-8")))
with backend.printer_clients_lock:      # 让它下线
    backend.printer_clients.pop(ONLINE_JUNK, None)
n = backend.purge_test_devices()
after = json.load(open("devices.json", encoding="utf-8"))
check("★ 离线后定时任务把它清掉（返回删除数 1）", n == 1 and ONLINE_JUNK not in after, f"n={n}")
check("★ 真机完好无损", all(c in after for c in REAL_IDS))
check("★ 它的接单记录也清了（不留僵尸接单中）",
      ONLINE_JUNK not in (json.load(open("printer_claim.json", encoding="utf-8"))
                          .get("claiming_devices") or {}))
check("再跑一次是幂等的（返回 0）", backend.purge_test_devices() == 0)

# ============================================================
# ⑥ 静态接线（后端路由/定时 + 授权页按钮 + 版本记录）
# ============================================================
print("\n=== ⑥ 静态接线 ===")
src = open(os.path.join(SRC, "app.py"), encoding="utf-8").read()
html = open(os.path.join(REPO, "local_print_tool", "finance", "settlement.html"),
            encoding="utf-8").read()
check("后端有识别函数 + 前缀常量", "def is_test_device_id(" in src
      and "TEST_DEVICE_PREFIXES = (" in src)
check("列表接口隐藏并回报计数", "hidden_test_devices" in src
      and "if is_test_device_id(cid):" in src)
check("有批量清理接口 + dry-run 默认", '@app.route("/api/printer/devices/cleanup"' in src
      and 'dry_run = bool(data.get("dry_run", True))' in src)
check("删除走统一的 _delete_devices（同时清接单记录）",
      "def _delete_devices(" in src and "claiming.pop(c, None)" in src)
check("定时任务已注册（每 30 分钟）",
      "scheduler.add_job(purge_test_devices" in src and 'id="purge_test_devices"' in src)
check("授权页有「清理测试设备」按钮 + 计数提示",
      'onclick="cleanupTestDevices()"' in html and "hidden_test_devices" in html)
check("★ 授权页另有「清理无主离线设备」按钮（陌生人设备不必再找开发者清）",
      'onclick="cleanupUnboundDevices()"' in html
      and "function cleanupUnboundDevices()" in html
      and "include_offline_unbound: true, min_offline_days: 0" in html)
check("两个按钮共用同一套两段式（先 dry_run 预览、确认后才真删）",
      "function runDeviceCleanup(" in html
      and "Object.assign({ dry_run: true }, extraBody || {})" in html
      and "Object.assign({ dry_run: false }, extraBody || {})" in html)
check("授权页版本记录已写 v6.7（版本号只钉「不早于 v6.7」，不钉死具体值 —— "
      "每加一版功能都会递增，钉死字面量会误报）",
      "v6.7：测试设备不再堆在授权页" in html
      and (lambda m: bool(m) and (int(m.group(1)), int(m.group(2))) >= (6, 7))(
          re.search(r">v(\d+)\.(\d+)</div>", html)))
check("离线却挂着接单记录时显示「接单中（离线）」并提示可删除清空",
      "接单中（离线）" in html and "僵尸记录" in html)

print("\n" + "=" * 56)
print("测试设备清理验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
shutil.rmtree(_tmp, ignore_errors=True)
sys.exit(0 if ok_all else 1)
