# -*- coding: utf-8 -*-
"""设备「自动双面」能力的 收集 / 解析 / 下发 验收

背景（现场）：组织里有两台设备 —— 甲成员的 HP MFP M232（支持自动双面）、丙成员的
HP LaserJet 1020（没有双面器，只有驱动的「手动双面」）。程序过去一律假设"打印机支持
自动双面"，于是双面订单在 1020 上会**静默打成单面却报成功**（客户按双面付费、店里出
双倍纸）。所以能力必须显式收集、随设备上报、并让下游看得到。

本脚本钉住的代码事实：

  · 能力存在设备注册表 devices.json 的 printer 子对象里，**分两层**：
    probe_*（本地打印工具自动探测）/ manual_*（人工盖章），解析优先级 manual > probe > unknown；
  · **人工结论不会被自动探测覆盖**（探测只写 probe_* 字段）—— HP 这类驱动把「手动双面」
    也算进双面能力，DC_DUPLEX 会虚报 true，"有没有双面器"只有人能盖章；
  · **老版本地工具不带能力参数 → 注册表保持原状**，不会被一次旧连接抹掉（零回归）；
  · 能力随三条链路下发：/api/claiming_devices（移动端选设备）、/api/printer/devices
    （收支清算授权页）、printer_state（本机）；
  · 下游执行（2026-10 补全）：服务端 `check_auto_print_duplex_conflict` 在建单前拦下
    「无障碍/预约 + 双面 + 不支持自动双面的设备」；移动端两侧做「双面 × 无人值守」互斥
    （选双面 / 开无障碍 / 切换设备三个入口 + 提交兜底 + 设备列表标记）。
    阶段①口径：只有**已确认不支持**（auto_duplex === false）才拦，未确认(null)一律放行。

用法: python tests/verify_duplex_capability.py   （exit 0 = 全部通过）
"""
import io
import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


tmp = tempfile.mkdtemp(prefix="hn_duplex_cap_")
dst = os.path.join(tmp, "printer-backend")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs", "venv"))
sys.path.insert(0, dst)
os.chdir(dst)

try:
    import app as backend
    print(f"imported app.py from {dst}")
except Exception:
    traceback.print_exc()
    sys.exit("无法导入 app.py")

backend.init_db()

# ==================== 桩：打印机 token / 在线设备 / 接单设备 / socketio ====================
backend.PRINTER_TOKEN = "test-token"
CID_NEW = "DESKTOP-YAO-a1b2"      # 支持自动双面（新机）
CID_OLD = "DESKTOP-YAN-c3d4"      # 不支持（HP LaserJet 1020）
CID_UNKNOWN = "DESKTOP-XXX-e5f6"  # 从未上报能力（老客户端）
CLAIMING = [CID_NEW, CID_OLD, CID_UNKNOWN]
ONLINE = {CID_NEW, CID_OLD, CID_UNKNOWN}


def _set_online(ids):
    """把设备标记为在线（printer_clients 里有条目 + sid，broadcast_printer_state 才发得出去）。"""
    ONLINE.clear()
    ONLINE.update(ids)
    backend.printer_clients.clear()
    for cid in ids:
        backend.printer_clients[cid] = {
            "sid": f"sid-{cid}", "heartbeat": datetime.now(), "connected_at": datetime.now(),
        }


_set_online([CID_NEW, CID_OLD, CID_UNKNOWN])


class FakeSocketIO:
    def __init__(self):
        self.sent = []

    def emit(self, event, payload=None, to=None):
        self.sent.append({"event": event, "payload": payload, "to": to})


fake_sio = FakeSocketIO()
backend.socketio = fake_sio
backend.get_active_clients = lambda: [c for c in CLAIMING if c in ONLINE]
backend.get_claiming_devices = lambda: {c: {"claimed_at": "", "owner_name": ""} for c in CLAIMING}
backend.get_claiming_device_ids = lambda: list(CLAIMING)

backend.register_device(CID_NEW, "DESKTOP-YAO")
backend.register_device(CID_OLD, "DESKTOP-YAN")
backend.register_device(CID_UNKNOWN, "DESKTOP-XXX")

# ==================== ① 三态解析优先级（人工 > 探测 > 未确认）====================
print("\n=== ① 三态解析优先级 ===")
cap = backend.get_device_capability(CID_NEW)
check("未上报任何能力 → auto_duplex=None / source=unknown",
      cap["auto_duplex"] is None and cap["source"] == "unknown", f"{cap['auto_duplex']}/{cap['source']}")

# 旧机型：驱动虚报「支持双面」（正是 HP 1018/1020/1022 的行为）
backend.set_device_probe(CID_OLD, {"auto_duplex": True, "queue": "HP LaserJet 1020",
                                   "driver": "HP LaserJet 1020", "port": "USB001"})
cap = backend.get_device_capability(CID_OLD)
check("探测上报 → source=probe", cap["source"] == "probe" and cap["auto_duplex"] is True,
      f"{cap['auto_duplex']}/{cap['source']}")
check("探测顺带记录打印队列/驱动/端口（授权页展示用）",
      cap["printer_name"] == "HP LaserJet 1020" and cap["printer_driver"] == "HP LaserJet 1020"
      and cap["printer_port"] == "USB001", f"{cap['printer_name']}|{cap['printer_port']}")

backend.set_device_manual_capability(CID_OLD, False, by="丙成员")
cap = backend.get_device_capability(CID_OLD)
check("人工盖章「不支持」→ 覆盖探测的 true", cap["source"] == "manual" and cap["auto_duplex"] is False,
      f"{cap['auto_duplex']}/{cap['source']}")
check("保留原始探测值供 UI 显示「探测与人工不一致」", cap["probe_auto_duplex"] is True)
check("记录人工确认者", cap["verified_by"] == "丙成员")

# 关键：探测再来一次（本地工具每次连接都会上报）不能把人工结论翻回去
backend.set_device_probe(CID_OLD, {"auto_duplex": True})
cap = backend.get_device_capability(CID_OLD)
check("★ 人工「不支持」不被后续探测覆盖（探测只写 probe_* 字段）",
      cap["source"] == "manual" and cap["auto_duplex"] is False
      and cap["probe_auto_duplex"] is True, f"{cap['auto_duplex']}/{cap['source']}")

backend.set_device_manual_capability(CID_OLD, None)
cap = backend.get_device_capability(CID_OLD)
check("人工结论传 None → 清除盖章、回到探测结果",
      cap["source"] == "probe" and cap["auto_duplex"] is True, f"{cap['auto_duplex']}/{cap['source']}")

backend.set_device_manual_capability(CID_OLD, False, by="丙成员")   # 复位为最终态
for raw, want in ((True, True), ("1", True), (1, True), ("true", True),
                  (False, False), ("0", False), (0, False), ("false", False),
                  ("", None), (None, None), ("maybe", None)):
    got = backend._as_bool_or_none(raw)
    check(f"三态归一化 {raw!r} → {want}", got is want, f"got={got!r}")

# ==================== ② 老客户端兼容（不带能力参数 → 不抹掉已有结论）====================
print("\n=== ② 老版本地工具兼容（零回归）===")
backend.set_device_probe(CID_NEW, {"auto_duplex": True, "queue": "HP LaserJet MFP M232-M237"})
before = backend.get_device_capability(CID_NEW)
backend.register_device(CID_NEW, "DESKTOP-YAO")                       # 老客户端：不传 printer
after = backend.get_device_capability(CID_NEW)
check("老客户端重连（不带能力参数）→ 已有能力原样保留",
      after["auto_duplex"] == before["auto_duplex"] and after["source"] == before["source"]
      and after["printer_name"] == before["printer_name"], f"{after['auto_duplex']}/{after['source']}")

backend.register_device(CID_NEW, "DESKTOP-YAO", printer={"queue": "HP LaserJet MFP M232-M237"})
after2 = backend.get_device_capability(CID_NEW)
check("只补打印机描述（无 auto_duplex 键）→ 探测结论不被清空",
      after2["auto_duplex"] is True and after2["printer_name"] == "HP LaserJet MFP M232-M237",
      f"{after2['auto_duplex']}/{after2['printer_name']}")

backend.register_device(CID_UNKNOWN, "DESKTOP-XXX", printer={"auto_duplex": ""})
check("上报空值 auto_duplex='' → 记为未确认（None），不是 False",
      backend.get_device_capability(CID_UNKNOWN)["auto_duplex"] is None
      and backend.get_device_capability(CID_UNKNOWN)["source"] == "probe")

# ==================== ③ 连接/心跳 query 参数解析 ====================
print("\n=== ③ 连接/心跳上报参数解析 ===")
with backend.app.test_request_context(
        "/socket.io/?token=test-token&client_id=C1&device_name=D1"
        "&auto_duplex=0&printer_name=HP+LaserJet+1020&printer_driver=HP+LaserJet+1020"
        "&printer_port=USB001"):
    probe = backend._printer_probe_from_request()
check("带能力参数 → 解析出 auto_duplex/queue/driver/port",
      probe == {"auto_duplex": "0", "queue": "HP LaserJet 1020",
                "driver": "HP LaserJet 1020", "port": "USB001"}, f"{probe}")

with backend.app.test_request_context("/socket.io/?token=t&client_id=C1&device_name=D1"):
    probe = backend._printer_probe_from_request()
check("老客户端连接（无能力参数）→ None（注册表不动）", probe is None, f"{probe}")

with backend.app.test_request_context("/socket.io/?client_id=C1&auto_duplex="):
    probe = backend._printer_probe_from_request()
check("auto_duplex 空值 → 仍算一次上报（值 None=未确认）",
      probe is not None and probe.get("auto_duplex") == "", f"{probe}")

# ==================== ④ POST /api/printer/capability ====================
print("\n=== ④ 能力写入接口 ===")
client = backend.app.test_client()


def post_cap(body, token="test-token"):
    url = "/api/printer/capability" + (f"?token={token}" if token else "")
    return client.post(url, json=body)


r = client.post("/api/printer/capability", json={"client_id": CID_OLD, "auto_duplex": False})
check("无 token → 403", r.status_code == 403, f"status={r.status_code}")
r = post_cap({"client_id": CID_OLD, "auto_duplex": False}, token="wrong")
check("token 错误 → 403", r.status_code == 403, f"status={r.status_code}")
r = post_cap({"client_id": "", "auto_duplex": False})
check("client_id 空 → 400", r.status_code == 400, f"status={r.status_code}")
r = post_cap({"client_id": "NOT-REGISTERED", "auto_duplex": False})
check("未登记设备 → 404（先连过云服务器才能标注）", r.status_code == 404, f"status={r.status_code}")
r = post_cap({"client_id": CID_OLD, "source": "nonsense", "auto_duplex": False})
check("非法 source → 400", r.status_code == 400, f"status={r.status_code}")

# 人工标注：授权页的「不支持（需人工翻纸）」
r = post_cap({"client_id": CID_OLD, "source": "manual", "auto_duplex": False})
cap = (r.get_json() or {}).get("capability") or {}
check("人工标注「不支持」→ 成功且回读一致",
      r.status_code == 200 and cap.get("auto_duplex") is False and cap.get("source") == "manual",
      f"status={r.status_code} cap={cap.get('auto_duplex')}/{cap.get('source')}")
check("写入能力后广播 printer_state 到本机（本地工具即时刷新）",
      any(s["event"] == "printer_state" and s["to"] == f"sid-{CID_OLD}" for s in fake_sio.sent),
      f"sent={[(s['event'], s['to']) for s in fake_sio.sent][:4]}")

# 探测上报（本地工具）不能翻转人工结论
r = post_cap({"client_id": CID_OLD, "source": "probe", "auto_duplex": True,
              "printer_name": "HP LaserJet 1020", "printer_port": "USB001"})
cap = (r.get_json() or {}).get("capability") or {}
check("★ 探测上报 true 不能覆盖人工「不支持」",
      cap.get("auto_duplex") is False and cap.get("source") == "manual"
      and cap.get("probe_auto_duplex") is True, f"{cap.get('auto_duplex')}/{cap.get('source')}")

r = post_cap({"client_id": CID_OLD, "source": "manual", "auto_duplex": None})
cap = (r.get_json() or {}).get("capability") or {}
check("人工结论传 null → 清除盖章、回到探测（true）",
      cap.get("auto_duplex") is True and cap.get("source") == "probe", f"{cap.get('auto_duplex')}/{cap.get('source')}")

r = post_cap({"client_id": CID_OLD, "source": "probe"})
check("probe 模式无任何字段 → 400", r.status_code == 400, f"status={r.status_code}")

# 复位成现场应有状态：1020 这台 = 人工确认「不支持」
post_cap({"client_id": CID_OLD, "source": "manual", "auto_duplex": False, "by": "丙成员"})
post_cap({"client_id": CID_NEW, "source": "manual", "auto_duplex": True, "by": "甲成员"})

# ==================== ⑤ 三条下发链路都带能力字段 ====================
print("\n=== ⑤ 能力下发（移动端 / 授权页 / 本机）===")
r = client.get("/api/claiming_devices")
data = r.get_json() or {}
byd = {d["client_id"]: d for d in data.get("devices", [])}
check("/api/claiming_devices 返回三台设备", len(byd) == 3, f"{list(byd)}")
check("★ 1020 这台下发 auto_duplex=false（移动端据此禁用无障碍打印）",
      byd.get(CID_OLD, {}).get("auto_duplex") is False, f"{byd.get(CID_OLD, {}).get('auto_duplex')}")
check("新机下发 auto_duplex=true", byd.get(CID_NEW, {}).get("auto_duplex") is True)
check("未确认设备下发 null（不是 false，下游可区分「未确认」与「确认不支持」）",
      byd.get(CID_UNKNOWN, {}).get("auto_duplex") is None
      and byd.get(CID_UNKNOWN, {}).get("capability_source") == "probe")
check("下发来源标记与打印机名（授权页/移动端展示）",
      byd.get(CID_OLD, {}).get("capability_source") == "manual"
      and byd.get(CID_OLD, {}).get("printer_name") == "HP LaserJet 1020",
      f"{byd.get(CID_OLD, {}).get('capability_source')}/{byd.get(CID_OLD, {}).get('printer_name')}")

r = client.get("/api/printer/devices?token=test-token")
data = r.get_json() or {}
byd2 = {d["client_id"]: d for d in data.get("devices", [])}
check("/api/printer/devices（授权页）带 auto_duplex/capability_source/printer_name",
      r.status_code == 200 and byd2.get(CID_OLD, {}).get("auto_duplex") is False
      and byd2.get(CID_OLD, {}).get("capability_source") == "manual"
      and byd2.get(CID_OLD, {}).get("capability_verified_by") == "丙成员",
      f"{byd2.get(CID_OLD, {}).get('auto_duplex')}/{byd2.get(CID_OLD, {}).get('capability_verified_by')}")
r = client.get("/api/printer/devices")
check("/api/printer/devices 无 token → 403", r.status_code == 403, f"status={r.status_code}")

state = backend._printer_state_payload(CID_OLD)
check("printer_state 带本机能力（本地工具据此调整双面 UI/提示）",
      state.get("auto_duplex") is False and state.get("capability_source") == "manual"
      and state.get("printer_name") == "HP LaserJet 1020", f"{state.get('auto_duplex')}")
check("printer_state 旧字段未被破坏（claiming_devices/is_active/owner_name）",
      set(state.get("claiming_devices", {}).keys()) == set(CLAIMING) and state.get("is_active") is True)

# ==================== ⑥ 下游执行：无障碍打印 × 双面 的冲突拦截（2026-10）====================
# 「记录能力」只是前半段；后半段是**真的拦**——目标设备已确认不支持自动双面时，双面必须人工
# 翻纸，无障碍/预约（无人值守）只会打完第 1 趟就停下等人 → 订单卡死。
print("\n=== ⑥ 无障碍打印 × 双面 冲突拦截 ===")
_conflict = backend.check_auto_print_duplex_conflict
check("★ 老机型(人工盖章不支持) + 无障碍 + 双面 → 拦下并给出可执行提示",
      _conflict(1, CID_OLD, True) != "" and "人工翻纸" in _conflict(1, CID_OLD, True),
      f"{_conflict(1, CID_OLD, True)}")
check("★ 老机型 + 无障碍 + **全是单面** → 放行（那台机器可以无人值守打单面）",
      _conflict(1, CID_OLD, False) == "")
check("支持自动双面的设备 → 放行", _conflict(1, CID_NEW, True) == "")
check("★ 能力**未确认**的设备 → 放行（阶段①：未确认按现状，别让没标注的机器不能下单）",
      _conflict(1, CID_UNKNOWN, True) == "")
check("没开无障碍 → 放行（手动开始打印本来就要人在场）", _conflict(0, CID_OLD, True) == "")
check("没指定目标设备 → 放行", _conflict(1, "", True) == "")
check("★ 设备不存在（设备表里没有）→ 放行，不误伤", _conflict(1, "NO-SUCH-DEVICE", True) == "")

app_src = open(os.path.join(dst, "app.py"), encoding="utf-8").read()
check("★ submit_order 在建单之前调用冲突校验（避免先写库再报错）",
      "conflict = check_auto_print_duplex_conflict(auto_print, target_client_id, has_duplex_file)"
      in app_src
      and app_src.index("check_auto_print_duplex_conflict(auto_print") < app_src.index(
          "# ---- 事务：插 1 条 orders + N 条 order_files ----"))
check("★ 冲突返回 409 + code（前端按 message 提示，code 供程序判断）",
      '"code": "auto_print_duplex_conflict"' in app_src and '}), 409' in app_src)
check("双面目判定口径与打印一致（duplex=on 才算双面）",
      'str(f.get("duplex", duplex) or "on").strip().lower() == "on"' in app_src)

# 移动端两侧的「双面 × 无人值守」互斥（三个入口 + 提交兜底 + 设备标记）
mp = open(os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.js"),
          encoding="utf-8").read()
wxml = open(os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.wxml"),
            encoding="utf-8").read()
wxss = open(os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.wxss"),
            encoding="utf-8").read()
apk = open(os.path.join(REPO, "mobile_apps", "android_app", "www", "print.js"),
           encoding="utf-8").read()
apk_app = open(os.path.join(REPO, "mobile_apps", "android_app", "www", "app.js"),
               encoding="utf-8").read()
apk_css = open(os.path.join(REPO, "mobile_apps", "android_app", "www", "styles.css"),
               encoding="utf-8").read()

check("★ 小程序：选双面时拦（无障碍开着 + 该设备不支持自动双面）",
      "value === 'on' && this.data.autoPrintEnabled && this._targetNeedsManualFlip()" in mp)
check("★ 小程序：开无障碍时拦（有双面文件 + 该设备不支持自动双面）",
      "turningOn && this._targetNeedsManualFlip() && this._hasDuplexFile()" in mp)
check("★ 小程序：切换设备时自动关无障碍并说明（不静默改单面）",
      "this.data.autoPrintEnabled && this._targetNeedsManualFlip(id) && this._hasDuplexFile()" in mp
      and "已关闭「无障碍打印」" in mp)
check("★ 小程序：提交兜底（与后端 409 同一口径）",
      "this.data.autoPrintEnabled && this._targetNeedsManualFlip() && this._hasDuplexFile()" in mp
      and "目标设备不支持自动双面：请关闭「无障碍打印」" in mp)
check("★ 小程序：设备列表标出「🖐 需人工翻纸」(+ 样式)",
      'item.auto_duplex === false' in wxml and "需人工翻纸" in wxml
      and ".device-state-manual" in wxss)

check("★ APK：选双面时拦", "val === 'on' && printState.autoPrintEnabled && targetNeedsManualFlip()" in apk)
check("★ APK：开无障碍时拦",
      "!printState.autoPrintEnabled && targetNeedsManualFlip() && hasDuplexFile()" in apk)
check("★ APK：切换设备时自动关无障碍并说明",
      "function onTargetDeviceChanged()" in apk and "已关闭「无障碍打印」" in apk
      and "if (typeof onTargetDeviceChanged === 'function') onTargetDeviceChanged();" in apk_app)
check("★ APK：提交兜底", "printState.autoPrintEnabled && targetNeedsManualFlip() && hasDuplexFile()" in apk
      and "目标设备不支持自动双面：请关闭「无障碍打印」" in apk)
check("★ APK：设备列表标出「🖐 需人工翻纸」(+ 样式)",
      "d.auto_duplex === false" in apk_app and "需人工翻纸" in apk_app
      and ".device-state-manual" in apk_css)

# ==================== ⑦ 静态接线（防止改别处时把上报/标注路径拆掉）====================
print("\n=== ⑦ 静态接线核对 ===")
check("socket 连接与 HTTP 拉单两条注册路径都上报能力",
      app_src.count("printer=_printer_probe_from_request()") == 2,
      f"count={app_src.count('printer=_printer_probe_from_request()')}")
check("register_device 支持 printer 参数",
      "def register_device(client_id: str, device_name: str = \"\", printer: dict | None = None)" in app_src)

html = open(os.path.join(REPO, "local_print_tool", "finance", "settlement.html"), encoding="utf-8").read()
check("授权页有「自动双面」三态下拉（data-device-cap + 事件委托）",
      "data-device-cap" in html and "function capCellHtml" in html
      and "addEventListener('change'" in html)
check("授权页保存走 stats_server 代理 /api/proxy/printer/capability",
      "/api/proxy/printer/capability" in html and "function saveDeviceCapability" in html)
check("授权页表格新增「自动双面」列 + 语义说明",
      "<th>自动双面</th>" in html and "不支持（需人工翻纸）" in html)

print("\n" + "=" * 56)
print("设备自动双面能力验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if ok_all else 1)
