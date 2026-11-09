# -*- coding: utf-8 -*-
"""验收 · 收支清算「订单用户」改归属（v7.18，2026-11-09 用户需求）

需求原文：
  在收支清算中，给订单列表中的「用户」一列也加上和「状态」一致的密码锁 + 调整策略，
  从而实现快捷调整此订单所属用户的功能。

口径（与状态/类型订正的区别，改错一处都会静默弄坏生产线）：
  · 改的是 orders.openid —— 订单的**实际归属用户**，小程序 / APP 的「我的订单」跟着变；
  · 不碰 status / locked_at / order_files / 设备通知，也不改 files.openid（上传者与保留期配额）；
  · 与状态/类型共用 unlocked.orderstatus 与同一个密码模块（不新增 PASSWORD_KEYS 项）；
  · 只接云端订单；目标 openid 必须已存在于 users 表（否则这单会从所有人的「我的订单」里消失）。

本脚本分两部分：

  A. 静态接线（settlement.html / app.py / stats_server.py 同源判据）
  B. 行为验收（真实 Flask + 真实 SQLite，复制一份后端到临时目录）

用法: PYTHONPATH= PYTHONUTF8=1 <python> tests/verify_order_user_edit.py   （exit 0 = 全绿）
"""
import io
import os
import re
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HTML_PATH = os.path.join(REPO, "local_print_tool", "finance", "settlement.html")
APP_PATH = os.path.join(REPO, "mobile_apps", "printer-backend", "app.py")
STATS_PATH = os.path.join(REPO, "local_print_tool", "stats_server.py")
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

bad = 0


def chk(name, cond, detail=""):
    global bad
    if not cond:
        bad += 1
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + str(detail)) if (not cond and detail != "") else ""))


html = open(HTML_PATH, encoding="utf-8").read()
app_src = open(APP_PATH, encoding="utf-8").read()
stats_src = open(STATS_PATH, encoding="utf-8").read()

# ============================================================================
print("=== A. 静态接线 ===")

# A1 表头：用户列带列键 + 挂自己的锁（状态那一句必须原样保留）
chk("ORDER_FIXED_COLS 的「用户」列带列键 'user'（列宽仍 104，总宽不变）",
    re.search(r"\['用户', 104, 'user'\]", html) is not None)
chk("表头对 'user' 列追加 _orderUserLockHTML()",
    re.search(r"c\[2\] === 'user' \? _orderUserLockHTML\(\) : ''", html) is not None)
chk("状态表头的既有精确子串未被破坏（verify_order_status_edit A2）",
    re.search(r"c\[2\] === 'status' \? _orderStatusLockHTML\(\) : ''", html) is not None)

# A2 同一把锁：不新增密码模块，图标/开关注册照状态那一套
chk("_orderUserLockHTML 本地模式不渲染",
    re.search(r"function _orderUserLockHTML\(\) \{\s*\n\s*if \(FINANCE_MODE === 'local'\) return '';",
              html) is not None)
chk("_orderUserLockHTML 读的是 unlocked.orderstatus（与状态/类型共用一把锁）",
    re.search(r"function _orderUserLockHTML\(\)[\s\S]{0,400}?unlocked\.orderstatus", html) is not None)
chk("_orderUserLockHTML 的点击走 toggleOrderStatusLock（不另起一套解锁流程）",
    re.search(r"function _orderUserLockHTML\(\)[\s\S]{0,500}?toggleOrderStatusLock\(\)", html) is not None)
chk("锁图标默认 🔒、解锁后 🔓", "? '🔓' : '🔒'" in html)
chk("PASSWORD_KEYS 没有被顺手加一个 orderuser（模块清单保持原样）",
    "orderuser" not in re.search(r"var PASSWORD_KEYS = \[[^\]]*\]", html).group(0))

# A3 可编辑判据：本地/外部/非十进制一律不可改
_gate = re.search(r"function _orderUserEditable\(oidS\) \{([\s\S]*?)\n\}", html)
_gate_body = _gate.group(1) if _gate else ""
chk("_orderUserEditable 存在且覆盖本地模式 / 外部监视单 / 非十进制 id / 未解锁",
    bool(_gate_body)
    and re.search(r"FINANCE_MODE === 'local'[\s\S]{0,160}?return false", _gate_body) is not None
    and re.search(r"_isExternalOrder\(oidS\)[\s\S]{0,80}?return false", _gate_body) is not None
    and "/^[0-9]+$/" in _gate_body
    and "unlocked.orderstatus" in _gate_body,
    _gate_body.strip()[:160])
chk("用户列的可编辑分支才渲染 st-caret 小箭头",
    re.search(r"_orderUserEditable\(oidS\)[\s\S]{0,400}?st-caret", html) is not None)
chk("用户标签 onclick 调 openOrderUserMenu",
    re.search(r"openOrderUserMenu\(event,\\'' \+ oidS", html) is not None)

# A4 菜单：body 级、同单再点收起、候选来自 revenue 的 assignable_users、点外部关闭
chk("用户菜单 id=usMenu 且带 data-oid（toggle 判据）",
    "m.id = 'usMenu'" in html and "setAttribute('data-oid'" in html)
chk("openOrderUserMenu 同单再点 = 收起（先 closeOrderUserMenu）",
    re.search(r"function openOrderUserMenu\(ev, oid\) \{[\s\S]{0,400}?closeOrderUserMenu\(\)",
              html) is not None)
chk("候选名单读 _cloudData.assignable_users（不另开 GET 接口）",
    re.search(r"_cloudData && _cloudData\.assignable_users", html) is not None)
chk("点外部关闭三个菜单（stMenu / tyMenu / usMenu 都在 _stMenuOutside 里）",
    re.search(r"function _stMenuOutside\(e\) \{[\s\S]{0,600}?usMenu", html) is not None)
chk("列表重绘时先收状态菜单、再收用户菜单（菜单挂在 body 上，不随区块替换）",
    re.search(r"function _renderOrderListHTML\(\) \{\s*\n\s*closeOrderStatusMenu\(\);\s*\n\s*closeOrderUserMenu\(\);",
              html) is not None)

# A5 提交：走代理写接口，成功后本地改 openid/nickname + 清缓存 + 重绘
chk("applyOrderUser 走 /api/proxy/admin/order_user（POST）",
    re.search(r"fetch\('/api/proxy/admin/order_user', \{\s*\n\s*method: 'POST'", html) is not None)
chk("POST body = {order_id, openid, by}",
    re.search(r"body: JSON\.stringify\(\{ order_id: String\(oid\), openid: pick\.openid, by: by \}\)",
              html) is not None)
chk("成功后本地改 openid 与 nickname",
    re.search(r"o\.openid = res\.openid \|\| pick\.openid;[\s\S]{0,200}?o\.nickname =", html) is not None)
chk("成功后清 _scopeCache 并 invalidateUsage（成员归组/KPI 一起重算）",
    re.search(r"o\.nickname =[\s\S]{0,600}?_scopeCache = \{\};", html) is not None
    and re.search(r"o\.nickname =[\s\S]{0,600}?invalidateUsage\(\);", html) is not None)
chk("最后 renderCloud() 重绘（归属变了，成员胶囊/by_user 必须跟着变）",
    re.search(r"invalidateUsage\(\);[\s\S]{0,600}?renderCloud\(\);", html) is not None)
chk("保存中有 _orderUserSaving 守卫（防连点重复提交）",
    "var _orderUserSaving = false;" in html)

# A6 设置页说明：只追加、不改既有精确子串
chk("设置页保留「同时管订单明细的...」原句（verify_order_status_edit A9）",
    "同时管订单明细的<b>「状态」</b>与<b>「类型」</b>两把锁" in html)
chk("设置页追加了用户列归属说明",
    re.search(r"订单「用户」列也归这把密码管", html) is not None)

# A7 后端：路由/鉴权/事务/白名单字段
chk("后端有 /api/admin/order_user 且要求 printer token",
    re.search(r"@app\.route\(\"/api/admin/order_user\", methods=\[\"POST\"\]\)\s*\n"
              r"@require_printer_scope\(\"admin\"\)", app_src) is not None)
chk("后端 BEGIN IMMEDIATE 事务",
    re.search(r"def admin_order_user\(\)[\s\S]{0,3000}?BEGIN IMMEDIATE", app_src) is not None)
chk("后端只 UPDATE orders.openid（不碰 order_files）",
    re.search(r"UPDATE orders SET openid = \?, manual_user_by = \?, manual_user_at = \?", app_src) is not None
    and re.search(r"def admin_order_user\(\)([\s\S]*?)@app\.route", app_src).group(1).count(
        "UPDATE order_files") == 0)
chk("后端拒绝 local / dev_ / 非法字符 openid",
    re.search(r"new_openid == \"local\" or new_openid\.startswith\(\"dev_\"\)", app_src) is not None
    and re.search(r"re\.fullmatch\(r\"\[A-Za-z0-9_\.\\-\]\+\", new_openid\)", app_src) is not None)
chk("后端要求目标用户必须存在于 users 表",
    re.search(r"SELECT openid, nickname FROM users WHERE openid = \?", app_src) is not None)
chk("revenue 下发 assignable_users 且排除设备账号",
    re.search(r"assignable_users", app_src) is not None
    and re.search(r"substr\(openid, 1, 4\) != 'dev_'", app_src) is not None)
chk("revenue 订单明细带 manual_user_by / manual_user_at",
    re.search(r"\"manual_user_by\": row\[\"manual_user_by\"\]", app_src) is not None)

# A8 本机代理：白名单 / 写模块 / 回执透传 / 审计
chk("_PROXY_ALLOWED_TARGETS 登记 /api/admin/order_user",
    '"/api/admin/order_user"' in stats_src)
chk("FINANCE_WRITE_MODULES 把 order_user 归到 orderstatus 模块",
    re.search(r'"/api/proxy/admin/order_user": "orderstatus"', stats_src) is not None)
chk("回执透传补 openid / previous_openid / manual_user_by",
    re.search(r'for _k in \("openid", "nickname", "previous_openid",\s*\n\s*'
              r'"manual_user_by", "manual_user_at"\)', stats_src) is not None)
chk("审计扩成 status/type/user 三分支且 user 记 before=previous_openid",
    re.search(r'target_path\.endswith\("/order_user"\)', stats_src) is not None
    and re.search(r'previous_openid', stats_src) is not None)

# ============================================================================
print("\n=== B. 行为验收（真实 Flask + 真实 SQLite）===")

TMP = tempfile.mkdtemp(prefix="hn_userfix_")
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
TOKEN = backend.PRINTER_TOKEN
client = backend.app.test_client()
HDR = {"X-Printer-Token": TOKEN}
URL = "/api/admin/order_user"

OLD = "wx-old-openid-0001"
NEW = "wx-new-openid-0002"


def mk_user(openid, nickname, role="user"):
    conn = backend.get_user_db()
    conn.execute(
        "INSERT INTO users (openid, nickname, avatar_path, updated_at, role)"
        " VALUES (?, ?, '', '2026-11-09 09:00:00', ?)", (openid, nickname, role))
    conn.commit()
    conn.close()


def mk_order(openid="wx-old-openid-0001", status="sent", number="HN-USER-1"):
    conn = backend.get_db()
    cur = conn.execute(
        "INSERT INTO orders (file, copies, status, created_at, openid, duplex, page_count,"
        " total_price, order_number, source, is_admin_print)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("x.pdf", 1, status, "2026-11-09 10:00:00", openid, "on", 2, 4.60,
         number, "wechat", 0))
    oid = cur.lastrowid
    conn.execute(
        "INSERT INTO order_files (order_id, file_name, copies, page_count, total_price,"
        " status, created_at, page_range, locked_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (oid, "f.pdf", 1, 2, 4.60, status, "2026-11-09 10:00:00", "",
         "2026-11-09 10:00:00"))
    conn.commit()
    conn.close()
    return oid


def state(oid):
    conn = backend.get_db()
    o = conn.execute(
        "SELECT openid, status, manual_user_by, manual_user_at FROM orders WHERE id = ?",
        (oid,)).fetchone()
    files = conn.execute(
        "SELECT status, locked_at FROM order_files WHERE order_id = ? ORDER BY id",
        (oid,)).fetchall()
    conn.close()
    return (dict(o) if o else None, [(r["status"], r["locked_at"]) for r in files])


def post(payload, headers=HDR):
    return client.post(URL, headers=headers, json=payload)


mk_user(OLD, "旧用户")
mk_user(NEW, "新用户")
mk_user("dev_abcdef0123456789abcdef", "设备账号")
mk_user("wx-removed-0003", "已移除用户")
conn = backend.get_user_db()
conn.execute("UPDATE users SET removed_at = '2026-11-01 00:00:00' WHERE openid = ?",
             ("wx-removed-0003",))
conn.commit()
conn.close()

print("\n-- B1 鉴权 / 参数校验（fail-closed，且失败不改库） --")
oid = mk_order()
before = state(oid)
r = post({"order_id": oid, "openid": NEW}, headers={})
chk("无 token → 403 且库未动", r.status_code == 403 and state(oid) == before,
    "%s %s" % (r.status_code, r.get_json()))
r = post({"order_id": oid, "openid": NEW}, headers={"X-Printer-Token": "wrong"})
chk("错 token → 403", r.status_code == 403)
r = post({"openid": NEW})
chk("缺 order_id → 400", r.status_code == 400, r.get_json())
r = post({"order_id": "abc", "openid": NEW})
chk("非十进制 order_id → 400", r.status_code == 400)
r = post({"order_id": oid, "openid": ""})
chk("空 openid → 400", r.status_code == 400)
r = post({"order_id": oid, "openid": "local"})
chk("openid='local' → 400", r.status_code == 400)
r = post({"order_id": oid, "openid": "dev_abcdef0123456789abcdef"})
chk("设备账号 dev_* → 400", r.status_code == 400)
r = post({"order_id": oid, "openid": "bad openid!"})
chk("含非法字符 → 400", r.status_code == 400)

print("\n-- B2 目标用户必须真实存在 --")
r = post({"order_id": oid, "openid": "wx-nobody-9999"})
chk("目标用户不存在 → 404 且库未动", r.status_code == 404 and state(oid) == before,
    "%s %s" % (r.status_code, r.get_json()))

print("\n-- B3 正常调整归属 --")
r = post({"order_id": oid, "openid": NEW, "by": "管理员A"})
j = r.get_json()
st, files = state(oid)
chk("改成功 200", r.status_code == 200 and j.get("success") is True, j)
chk("orders.openid 已改", st["openid"] == NEW, st)
chk("回执带 previous_openid / nickname", j.get("previous_openid") == OLD
    and j.get("nickname") == "新用户", j)
chk("留痕 manual_user_by / manual_user_at 已写", st["manual_user_by"] == "管理员A"
    and bool(st["manual_user_at"]), st)
chk("订单状态没被顺手改（仍 sent）", st["status"] == "sent", st)
chk("子任务状态 / locked_at 一点没动", files == [("sent", "2026-11-09 10:00:00")], files)

print("\n-- B4 幂等：同一用户重复提交 --")
r = post({"order_id": oid, "openid": NEW, "by": "管理员B"})
j = r.get_json()
st2, _ = state(oid)
chk("同 openid 仍 200 成功（不报错、不重写留痕）", r.status_code == 200
    and j.get("success") is True and j.get("previous_openid") == NEW, j)
chk("幂等提交不覆盖原留痕（manual_user_by 仍是管理员A）", st2["manual_user_by"] == "管理员A", st2)

print("\n-- B5 本地归属订单（openid='local'）无此入口 --")
oid_local = mk_order(openid="local", number="HN-USER-LOCAL")
r = post({"order_id": oid_local, "openid": NEW})
chk("local 单 → 400 且归属未变", r.status_code == 400
    and state(oid_local)[0]["openid"] == "local", r.get_json())

print("\n-- B6 订单不存在 --")
r = post({"order_id": 99999999, "openid": NEW})
chk("订单不存在 → 404", r.status_code == 404, r.get_json())

print("\n-- B7 结算接口跟着新归属走 --")
rev = client.get("/api/admin/statistics/revenue?start_date=2026-11-01&end_date=2026-11-30"
                 "&token=" + TOKEN)
rj = rev.get_json()
orders = ((rj or {}).get("data") or {}).get("orders") or []
row = next((o for o in orders if str(o.get("order_id")) == str(oid)), None)
chk("revenue 里该单 openid 已是新用户", row is not None and row.get("openid") == NEW, row)
chk("revenue 里带 manual_user_by / manual_user_at", row is not None
    and row.get("manual_user_by") == "管理员A" and bool(row.get("manual_user_at")), row)
users = ((rj or {}).get("data") or {}).get("assignable_users") or []
uids = [u.get("openid") for u in users]
chk("assignable_users 含新旧用户", OLD in uids and NEW in uids, uids)
chk("assignable_users 排除设备账号与已移除账号",
    not any(str(x).startswith("dev_") for x in uids) and "wx-removed-0003" not in uids, uids)

shutil.rmtree(TMP, ignore_errors=True)

print("\n%s" % ("✅ 全部通过" if bad == 0 else "❌ %d 项失败" % bad))
sys.exit(0 if bad == 0 else 1)
