# -*- coding: utf-8 -*-
"""接单设备置顶顺序（pin order）验收 —— 真实 Flask + 临时目录

需求（2026-10 用户原话）：「设备列表当前按上线/离线排序；现在需要加入置顶功能，用户将某个
设备置顶之后，会默认选中置顶的这个设备，不管该设备是否在线；若有多个置顶设备，则可以进一步
调整优先级顺序。」
已定口径：存服务端、全局共享（小程序与 APP 同一份）；一份有序置顶列表，第一项即默认选中；
只有 admin / super_admin 能改。

本脚本钉住的契约：
  A1 未置顶时行为与旧版一致（在线优先），新增字段 pin=false / pin_index=null / pin_order=[]
  A2 置顶 2 台（其中一台离线）→ pin_index 0/1，排最前且按置顶序，离线那台仍占其置顶位
  A3 未知 cid / 重复 / 超长 / 51 项 → 过滤去重截断到 50，返回的 pin_order 与落盘文件一致；
     手工改过的文件里那些不存在的 cid 被读路径过滤且不占序位
  A4 鉴权：未登录 401 / 普通 user 403 / admin 与 super_admin 成功（状态码钉在这里）
  A5 畸形请求体 4 组（+ 缺失 order / 非 JSON / 深嵌套）全部 400 或 403，全程 0 个 500
  A6 损坏的 device_pin_order.json 不影响 /api/claiming_devices（fail-open 按空列表）
  A7 变异自证：--mutate sort 时 A2 必须转红；--mutate auth 时 A4 必须转红

用法:
  python tests/verify_device_pin_order.py                 # 全绿则 exit 0
  python tests/verify_device_pin_order.py --mutate sort   # 期望 A2 转红（排序改回旧口径）
  python tests/verify_device_pin_order.py --mutate auth   # 期望 A4 转红（去掉角色闸门）
"""
import io
import json
import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

MUTATE = ""
for _i, _a in enumerate(sys.argv):
    if _a == "--mutate" and _i + 1 < len(sys.argv):
        MUTATE = sys.argv[_i + 1].strip().lower()

RESULTS = []          # [(section, name, ok)]
SECTION = "A1"


def check(name, cond, detail=""):
    ok = bool(cond)
    RESULTS.append((SECTION, name, ok))
    print("[%s] %s · %s%s" % ("PASS" if ok else "FAIL", SECTION, name,
                              ("  -- " + str(detail)) if detail else ""))
    return ok


# ==================== 变异锚点（只改临时副本，真实 app.py 不动）====================
MUT_SORT_OLD = """    result.sort(key=lambda d: (
        d["pin_index"] is None,
        d["pin_index"] if d["pin_index"] is not None else 0,
        not d["online"],
        d["client_id"].lower(),
    ))"""
MUT_SORT_NEW = """    result.sort(key=lambda d: (not d["online"], d["client_id"].lower()))"""

MUT_AUTH_OLD = """    写盘失败：旧文件原样保留（原子写天然保证），返回 success:false 并带上当前生效顺序。
    \"\"\"
    role = compute_role(getattr(g, "openid", "") or "")
    if role not in ("admin", "super_admin"):
        return jsonify({"success": False, "message": "需要管理员权限"}), 403
"""
MUT_AUTH_NEW = """    写盘失败：旧文件原样保留（原子写天然保证），返回 success:false 并带上当前生效顺序。
    \"\"\"
    role = compute_role(getattr(g, "openid", "") or "")
"""

tmp = tempfile.mkdtemp(prefix="hn_pin_order_")
dst = os.path.join(tmp, "printer-backend")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs", "venv",
    "device_pin_order.json"))

if MUTATE:
    _path = os.path.join(dst, "app.py")
    with open(_path, encoding="utf-8") as _f:
        _text = _f.read()
    if MUTATE == "sort":
        _old, _new = MUT_SORT_OLD, MUT_SORT_NEW
    elif MUTATE == "auth":
        _old, _new = MUT_AUTH_OLD, MUT_AUTH_NEW
    else:
        sys.exit("未知的 --mutate 值: %s（只支持 sort / auth）" % MUTATE)
    if _old not in _text:
        sys.exit("变异锚点没找到（%s）—— app.py 结构已变，请更新本脚本的锚点" % MUTATE)
    with open(_path, "w", encoding="utf-8") as _f:
        _f.write(_text.replace(_old, _new, 1))
    print("!! 变异模式 %s：已在临时副本上改动 app.py（真实文件未动）" % MUTATE)

sys.path.insert(0, dst)
os.chdir(dst)
try:
    import app as backend
    print("imported app.py from %s" % dst)
except Exception:
    traceback.print_exc()
    sys.exit("无法导入 app.py")

backend.init_db()

# ==================== 桩环境 ====================
ADMIN_OID, SUPER_OID, USER_OID = "openid-admin", "openid-super", "openid-user"
ROLES = {ADMIN_OID: "admin", SUPER_OID: "super_admin", USER_OID: "user"}
backend.compute_role = lambda oid: ROLES.get(oid, "guest")
ADMIN_TOKEN = backend.token_serializer.dumps(ADMIN_OID)
SUPER_TOKEN = backend.token_serializer.dumps(SUPER_OID)
USER_TOKEN = backend.token_serializer.dumps(USER_OID)

CID_ON = "DESKTOP-ONLINE-a1"
CID_OFF = "DESKTOP-OFFLINE-b2"
CID_C = "DESKTOP-GAMMA-c3"
CID_D = "DESKTOP-DELTA-d4"
MAIN_CIDS = [CID_ON, CID_OFF, CID_C, CID_D]

client = backend.app.test_client()


def set_claiming(cids):
    """桩：接单集合（并确保每台设备都已在注册表里登记过）。"""
    backend.get_claiming_devices = lambda: {c: {"claimed_at": "", "owner_name": "张三"}
                                            for c in cids}
    backend.get_claiming_device_ids = lambda: list(cids)
    known = backend.load_devices()
    for c in cids:
        if c not in known:
            backend.register_device(c, c)


def set_online(online_cids):
    """桩：在线设备（心跳表）。"""
    now = datetime.now()
    with backend.printer_clients_lock:
        backend.printer_clients.clear()
        for c in online_cids:
            backend.printer_clients[c] = {"sid": "sid-" + c, "heartbeat": now,
                                          "connected_at": now}


def pin_path():
    return os.path.join(os.getcwd(), backend.DEVICE_PIN_FILE)


def clear_pin_file():
    p = pin_path()
    if os.path.exists(p):
        os.remove(p)
    backend._device_pin_cache["sig"] = None
    backend._device_pin_cache["data"] = None


def write_pin_file(raw_text):
    """手工写（模拟运维/别的进程直接改文件）。走临时文件 + replace，保证指纹变化。"""
    p = pin_path()
    with open(p + ".manual.tmp", "w", encoding="utf-8") as f:
        f.write(raw_text)
    os.replace(p + ".manual.tmp", p)


def read_pin_file():
    with open(pin_path(), encoding="utf-8") as f:
        return json.load(f)


def get_devices():
    r = client.get("/api/claiming_devices")
    return r.status_code, r.get_json(silent=True)


def post_pin(payload=None, token=None, raw=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if raw is not None:
        headers["Content-Type"] = "application/json"
        r = client.post("/api/admin/device_pin_order", data=raw, headers=headers)
    else:
        r = client.post("/api/admin/device_pin_order", json=payload, headers=headers)
    return r.status_code, r.get_json(silent=True)


def by_id(payload):
    return {d["client_id"]: d for d in payload["devices"]}


# ==================== A1 未置顶：行为与旧版一致 ====================
SECTION = "A1"
print("\n=== A1 未置顶时与旧口径完全一致 ===")
clear_pin_file()
set_claiming(MAIN_CIDS)
set_online([CID_ON])                       # 只有 CID_ON 在线
_devs = backend.load_devices()                  # 给一台盖上所有者姓名（验脱敏仍在）
_devs[CID_ON]["owner_name"] = "张三"
backend.save_devices(_devs)
code, body = get_devices()
check("GET /api/claiming_devices 正常且列出 4 台",
      code == 200 and body.get("success") is True and len(body["devices"]) == 4,
      "code=%s n=%s" % (code, len(body.get("devices") or [])))
check("无置顶 → 顶层 pin_order == []", body.get("pin_order") == [], body.get("pin_order"))
check("每台设备 pin=false / pin_index=null",
      all(d.get("pin") is False and d.get("pin_index") is None for d in body["devices"]))
old_order = [d["client_id"] for d in body["devices"]]
expected_old = sorted(MAIN_CIDS, key=lambda c: (0 if c == CID_ON else 1, c.lower()))
check("排序仍是「在线优先 + client_id.lower()」老口径", old_order == expected_old,
      "%s / 期望 %s" % (old_order, expected_old))
NEEDED = {"client_id", "device_name", "owner_name", "online", "label",
          "auto_duplex", "capability_source", "printer_name",
          "capability_verified_by", "capability_verified_at", "capability_probe",
          "stack_mode", "stack_busy", "stack_can_toggle", "stack_reason"}
missing = sorted(NEEDED - set(body["devices"][0]))
check("既有字段一个都没少、没改名", not missing, "缺 %s" % missing)
check("既有 owner_name 仍是脱敏的（公开接口不泄露真名）",
      body["devices"][0]["owner_name"] == "张*", body["devices"][0]["owner_name"])

# ==================== A2 置顶 2 台（含离线）====================
SECTION = "A2"
print("\n=== A2 置顶 2 台（其中一台离线）===")
clear_pin_file()
set_claiming(MAIN_CIDS)
set_online([CID_ON])                       # 离线的是 CID_OFF（置顶第 1 位）
code, body = post_pin({"order": [CID_OFF, CID_C]}, token=ADMIN_TOKEN)
check("POST /api/admin/device_pin_order → 200 + success",
      code == 200 and body.get("success") is True, "code=%s body=%s" % (code, body))
check("返回的 pin_order 就是提交的顺序",
      body.get("pin_order") == [CID_OFF, CID_C], body.get("pin_order"))
check("落盘文件结构 {order, updated_at(epoch 秒), updated_by}",
      os.path.exists(pin_path())
      and read_pin_file().get("order") == [CID_OFF, CID_C]
      and isinstance(read_pin_file().get("updated_at"), int)
      and read_pin_file().get("updated_at") > 1600000000
      and read_pin_file().get("updated_by") == ADMIN_OID,
      open(pin_path(), encoding="utf-8").read().replace("\n", " ") if os.path.exists(pin_path()) else "无文件")

code, body = get_devices()
ids = [d["client_id"] for d in body["devices"]]
devs = by_id(body)
check("pin_index 为 0 / 1",
      devs[CID_OFF]["pin_index"] == 0 and devs[CID_C]["pin_index"] == 1,
      "%s / %s" % (devs[CID_OFF]["pin_index"], devs[CID_C]["pin_index"]))
check("pin 标记为 true（置顶的两台）",
      devs[CID_OFF]["pin"] is True and devs[CID_C]["pin"] is True)
check("未置顶的两台仍 pin=false / pin_index=null",
      devs[CID_ON]["pin"] is False and devs[CID_ON]["pin_index"] is None
      and devs[CID_D]["pin"] is False and devs[CID_D]["pin_index"] is None)
check("置顶的两台排在返回顺序最前两位且按置顶序", ids[:2] == [CID_OFF, CID_C], ids)
check("离线的置顶设备仍占第 1 位（没有被在线的 CID_ON 挤下去）",
      ids[0] == CID_OFF and devs[CID_OFF]["online"] is False, ids)
check("置顶组之后才是未置顶（内部仍老口径：在线优先）",
      ids[2:] == [CID_ON, CID_D], ids)
check("顶层 pin_order 与生效顺序一致", body.get("pin_order") == [CID_OFF, CID_C],
      body.get("pin_order"))

# 再换一次顺序：优先级可调（C 升到第一）
code, body = post_pin({"order": [CID_C, CID_OFF]}, token=ADMIN_TOKEN)
check("调整优先级：C 置顶第 1、离线设备退到第 2",
      code == 200 and body.get("pin_order") == [CID_C, CID_OFF], body.get("pin_order"))
code, body = get_devices()
ids = [d["client_id"] for d in body["devices"]]
check("调整后返回顺序同步改变", ids[:2] == [CID_C, CID_OFF]
      and by_id(body)[CID_C]["pin_index"] == 0, ids)

# ==================== A3 过滤 / 去重 / 截断 ====================
SECTION = "A3"
print("\n=== A3 未知 cid / 重复 / 超长 / 51 项 ===")
clear_pin_file()
MANY = ["PIN-%03d" % i for i in range(60)]      # 60 台可置顶设备，才验得出"截断到 50"
set_claiming(MANY)
set_online([MANY[0]])
LONG_CID = "X" * 65                             # 65 > 64 → 丢弃
payload = [MANY[0], MANY[0], "UNKNOWN-CID-zzz", LONG_CID] + MANY[1:60]
# 有效项 = MANY[0] .. MANY[59]（重复的只留一个、未知与超长的丢掉）→ 截断到前 50 项
code, body = post_pin({"order": payload}, token=ADMIN_TOKEN)
check("含脏项的提交 → 200 + success", code == 200 and body.get("success") is True,
      "code=%s body=%s" % (code, str(body)[:200]))
check("去重 / 丢未知 / 丢超长 / 截断到 50 项", body.get("pin_order") == MANY[:50],
      "len=%s head=%s" % (len(body.get("pin_order") or []), (body.get("pin_order") or [])[:3]))
check("落盘文件内容与返回的 pin_order 完全一致",
      read_pin_file().get("order") == MANY[:50])
code, body = get_devices()
check("GET 的 pin_order 与文件一致", body.get("pin_order") == MANY[:50])
devs = by_id(body)
check("置顶 50 台的 pin_index 恰好是 0..49",
      [devs[c]["pin_index"] for c in MANY[:50]] == list(range(50)))
check("超出 50 的未置顶设备 pin=false / pin_index=null",
      all(devs[c]["pin"] is False and devs[c]["pin_index"] is None for c in MANY[50:]))
ids = [d["client_id"] for d in body["devices"]]
check("返回顺序 = 置顶 50 台（按序）+ 其余",
      ids[:50] == MANY[:50] and sorted(ids[50:]) == sorted(MANY[50:]),
      "前 3: %s / 未置顶 %s 台" % (ids[:3], len(ids) - 50))

# 手工改文件（运维/别的进程）留下的幽灵 cid：读路径必须过滤，且**不占序位**
write_pin_file(json.dumps({"order": [MANY[0], "GHOST-cid", MANY[1]], "updated_at": 1,
                           "updated_by": "manual"}, ensure_ascii=False))
code, body = get_devices()
devs = by_id(body)
check("文件里的幽灵 cid 被读路径过滤掉", body.get("pin_order") == [MANY[0], MANY[1]],
      body.get("pin_order"))
check("被过滤项不占序位（PIN-001 的 pin_index 是 1 而不是 2）",
      devs[MANY[1]]["pin_index"] == 1, devs[MANY[1]]["pin_index"])
check("文件里那些超出集合的项不会被读接口回显", "GHOST-cid" not in (body.get("pin_order") or []))

# ==================== A4 鉴权 / 角色 ====================
SECTION = "A4"
print("\n=== A4 鉴权：401 / 403 / 200 ===")
clear_pin_file()
set_claiming(MAIN_CIDS)
set_online([CID_ON])
code, body = post_pin({"order": [CID_OFF]}, token=None)
check("未登录（无 Authorization）→ 401", code == 401, "code=%s" % code)
code, body = post_pin({"order": [CID_OFF]}, token="garbage-token")
check("无效 token → 401", code == 401, "code=%s" % code)
code, body = post_pin({"order": [CID_OFF]}, token=USER_TOKEN)
check("普通 user 角色 → 403（钉住实际值）", code == 403, "code=%s body=%s" % (code, body))
check("被拒的 user 请求没有写盘", not os.path.exists(pin_path()))
code, body = post_pin({"order": [CID_OFF]}, token=ADMIN_TOKEN)
check("admin → 200 + success + 生效列表",
      code == 200 and body.get("success") is True and body.get("pin_order") == [CID_OFF],
      "code=%s body=%s" % (code, body))
check("updated_by 记的是操作者 openid", read_pin_file().get("updated_by") == ADMIN_OID,
      read_pin_file().get("updated_by"))
code, body = post_pin({"order": [CID_C, CID_OFF]}, token=SUPER_TOKEN)
check("super_admin → 200 + success",
      code == 200 and body.get("success") is True and body.get("pin_order") == [CID_C, CID_OFF],
      "code=%s body=%s" % (code, body))
check("super_admin 也写进了 updated_by", read_pin_file().get("updated_by") == SUPER_OID)

# ==================== A5 畸形请求体：一律 400/403，0 个 500 ====================
SECTION = "A5"
print("\n=== A5 畸形请求体（0 个 500）===")
set_claiming(MAIN_CIDS)
set_online([CID_ON])
clear_pin_file()
BAD_CASES = [
    ("JSON 数组体", dict(raw=b"[1,2,3]")),
    ("JSON 字符串体", dict(raw=b'"abc"')),
    ("order 是字符串", dict(payload={"order": "abc"})),
    ("order 含数字", dict(payload={"order": [CID_C, 123]})),
    ("order 缺失", dict(payload={})),
    ("完全不是 JSON", dict(raw=b"{bad json")),
    ("深嵌套 1000 层", dict(raw=("[" * 1000 + "]" * 1000).encode())),
]
seen_500 = []
codes = []
for name, kw in BAD_CASES:
    code, body = post_pin(token=ADMIN_TOKEN, **kw)
    codes.append((name, code))
    ok = code in (400, 403) and code != 500 and (body is None or body.get("success") is not True)
    if code == 500:
        seen_500.append(name)
    check("admin + %s → 400/403 且 success 不为 true" % name, ok,
          "code=%s body=%s" % (code, str(body)[:120]))
check("畸形请求后没有建出置顶文件", not os.path.exists(pin_path()))

# 未登录时同样不能 500（深嵌套也不能把鉴权表达式打崩）
for name, kw in BAD_CASES:
    code, body = post_pin(token=None, **kw)
    if code == 500:
        seen_500.append(name + "(未登录)")
    check("未登录 + %s → 401/403" % name, code in (401, 403), "code=%s" % code)

check("A5 全程 0 个 500", not seen_500, seen_500)
check("A5 全部落在 400/401/403 白名单里",
      all(c in (400, 401, 403) for _, c in codes), codes)

# ==================== A6 损坏的置顶文件 → fail-open ====================
SECTION = "A6"
print("\n=== A6 损坏文件不影响 /api/claiming_devices ===")
set_claiming(MAIN_CIDS)
set_online([CID_ON])
CORRUPT = [
    ("非法 JSON", "{ this is not json !!!"),
    ("order 不是 list", json.dumps({"order": "not-a-list"})),
    ("顶层是数组", "[1,2,3]"),
    ("order 里混了非字符串", json.dumps({"order": [123, None, CID_C]})),
    ("空文件", ""),
]
bad = []
for name, text in CORRUPT:
    write_pin_file(text)
    code, body = get_devices()
    ok = (code == 200 and body.get("success") is True and isinstance(body.get("pin_order"), list)
          and len(body["devices"]) == 4)
    if not ok:
        bad.append((name, code, str(body)[:120]))
    check("损坏文件「%s」→ 200 且设备列表照常" % name, ok, "code=%s" % code)
    if name == "order 里混了非字符串":
        check("非字符串项被丢弃、字符串项保留", body.get("pin_order") == [CID_C],
              body.get("pin_order"))
    else:
        check("「%s」→ pin_order 视作空列表" % name, body.get("pin_order") == [],
              body.get("pin_order"))
        check("「%s」→ 所有设备 pin=false / pin_index=null" % name,
              all(d["pin"] is False and d["pin_index"] is None for d in body["devices"]))
check("A6 没有一次失败", not bad, bad)
# 坏文件之后仍能正常写入并生效（自愈）
code, body = post_pin({"order": [CID_D]}, token=ADMIN_TOKEN)
check("坏文件之后仍可写入并生效（自愈）",
      code == 200 and body.get("pin_order") == [CID_D], "code=%s body=%s" % (code, body))
code, body = get_devices()
check("自愈后 GET 立刻看到新顺序", body.get("pin_order") == [CID_D]
      and by_id(body)[CID_D]["pin_index"] == 0, body.get("pin_order"))

# ==================== A7 变异自证 ====================
SECTION = "A7"
print("\n=== A7 变异自证 ===")
if MUTATE == "sort":
    reds = [r for r in RESULTS if r[0] == "A2" and not r[2]]
    check("变异 sort（排序改回只按 online）→ A2 必须转红", len(reds) >= 1,
          "A2 红色 %d 条：%s" % (len(reds), [r[1] for r in reds]))
elif MUTATE == "auth":
    reds = [r for r in RESULTS if r[0] == "A4" and not r[2]]
    check("变异 auth（去掉角色闸门）→ A4 必须转红", len(reds) >= 1,
          "A4 红色 %d 条：%s" % (len(reds), [r[1] for r in reds]))
else:
    check("正常模式下 A2 / A4 必须全绿（变异自证用 --mutate 跑）",
          all(r[2] for r in RESULTS if r[0] in ("A2", "A4")),
          "提示：python tests/verify_device_pin_order.py --mutate sort|auth")

# ==================== 汇总 ====================
fails = [r for r in RESULTS if not r[2]]
print("\n=== 汇总：%d 条，失败 %d 条 ===" % (len(RESULTS), len(fails)))
for sec, name, _ok in fails:
    print("  RED %s · %s" % (sec, name))

if MUTATE:
    target = "A2" if MUTATE == "sort" else "A4"
    n = len([1 for r in RESULTS if r[0] == target and not r[2]])
    print("变异模式 %s：%s 红色 %d 条 → %s" % (
        MUTATE, target, n, "自证成立（变异被测出）" if n else "自证失败（变异没让用例转红）"))
    sys.exit(0 if n else 1)
sys.exit(0 if not fails else 1)
