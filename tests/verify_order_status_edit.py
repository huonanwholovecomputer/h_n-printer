# -*- coding: utf-8 -*-
"""验收 · 收支清算「订单状态订正」（v7.0，2026-10-18 用户需求）

需求原文（现场：打印出错时的便捷处理）：
  云端数据 → 订单明细列表，「状态」列可直接展开状态并调节；但为防误触**默认锁定**，
  「状态」右侧显示 🔒 → 点击输密码解锁 → 🔓 → 此时每个订单的状态标签才带小箭头，
  点击后直接选择正确的状态，**改动直接生效**；再点 🔓 变回 🔒。该密码在设置界面管理，
  默认 123456。

本脚本分两部分：

  A. 静态接线（settlement.html + app.py 同源判据）
     · 新密码模块 orderstatus：PASSWORD_KEYS / DEFAULT_PASSWORDS(=123456 的 sha256) /
       MODULE_NAMES / unlocked 初始为 false（=默认锁定）；设置页按 PASSWORD_KEYS 渲染所以自动可改
     · 锁开关只挂在「状态」表头（ORDER_FIXED_COLS 的 'status' 列键）；箭头只在**已解锁**时渲染
     · 提交走 /api/proxy/admin/order_status（POST），本地先落地再重绘（不整页拉数）
     · 前端可选状态 = 后端白名单（少一处就会「页面能选、后端 400」）
     · 本地模式（FINANCE_MODE==='local'）不给入口

  B. 行为验收（真实 Flask + 真实 SQLite，复制一份后端到临时目录）
     · 403（无 token）/ 400（缺 order_id / 非法状态 / 非终态）/ 404（订单不存在）
     · 改成 sent / failed / abandoned / canceled / rejected：**父订单与其全部子任务一起改**，
       聚合状态 == 目标（只改 orders.status 会被 aggregate_order_status 刷回去）
     · locked_at 清空（终态不再被 3 分钟超时/孤儿回收盯上）
     · 已 sent 的子任务也照改（人工订正是最终裁决）；无子任务的历史老单只改父订单
     · 幂等：重复提交同一状态仍成功
     · 端到端：改成 sent 后 /api/admin/statistics/revenue 里该单状态为 sent 且计入收益；
       此前为 abandoned 时不计入收益（状态决定结算口径 —— 这正是本功能的意义）

用法: PYTHONPATH= PYTHONUTF8=1 <python> tests/verify_order_status_edit.py   （exit 0 = 全绿）
"""
import io
import os
import re
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(REPO, "local_print_tool", "finance", "settlement.html")
APP = os.path.join(REPO, "mobile_apps", "printer-backend", "app.py")
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

bad = 0


def chk(name, cond, detail=""):
    global bad
    if not cond:
        bad += 1
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + str(detail)) if (not cond and detail != "") else ""))


html = open(HTML, encoding="utf-8").read()
app_src = open(APP, encoding="utf-8").read()

# ============================================================================
print("=== A. 静态接线 ===")

# A1 密码模块注册（默认 123456 = sha256:8d969eef…，与其它模块同一个常量）
chk("PASSWORD_KEYS 含 orderstatus（迁移补默认值 / 设置页密码管理共用同一份清单）",
    re.search(r"var PASSWORD_KEYS = \[[^\]]*'orderstatus'", html) is not None)
chk("DEFAULT_PASSWORDS 里 orderstatus 默认 123456 的 SHA-256（不存明文）",
    re.search(r"DEFAULT_PASSWORDS = \{[^}]*orderstatus: _PW_123456", html) is not None)
chk("MODULE_NAMES 有 orderstatus → 中文名（解锁弹窗/设置页/提示共用）",
    re.search(r"MODULE_NAMES = \{[^}]*orderstatus: '[^']+'", html) is not None)
chk("unlocked 初始含 orderstatus: false（默认锁定，刷新页面即回到锁定）",
    re.search(r"var unlocked = \{[^}]*orderstatus: false", html) is not None)
chk("设置页密码区按 PASSWORD_KEYS 渲染（新模块自动可改密码，无需另加 UI）",
    re.search(r"PASSWORD_KEYS\.forEach\(function \(k\) \{\s*\n\s*h \+= '<div class=\"settings-row\"><label>' \+ MODULE_NAMES\[k\]",
              html) is not None)
chk("设置页写密码仍走 hashPassword（只存哈希）",
    re.search(r"function updatePassword\(key\)[\s\S]{0,600}?hashPassword\(v\)", html) is not None)

# A2 锁开关挂在「状态」表头（且只挂这一列）
chk("ORDER_FIXED_COLS 的「状态」列带列键 'status'",
    re.search(r"\['状态', 84, 'status'\]", html) is not None)
chk("表头渲染对 'status' 列追加 _orderStatusLockHTML()（锁开关在表头、不是每行）",
    re.search(r"c\[2\] === 'status' \? _orderStatusLockHTML\(\) : ''", html) is not None)
chk("锁图标默认 🔒、解锁后 🔓",
    "? '🔓' : '🔒'" in html)
chk("点锁 = toggleOrderStatusLock（已解锁 → 直接锁定；未解锁 → requestPassword 解锁）",
    re.search(r"function toggleOrderStatusLock\(\)[\s\S]{0,700}?requestPassword\('orderstatus'", html) is not None)
chk("解锁成功后只重绘订单区块（_rerenderOrdersOnly），不整页闪烁",
    re.search(r"function toggleOrderStatusLock\(\)[\s\S]{0,900}?if \(ok\) _rerenderOrdersOnly\(\)", html) is not None)
chk("锁定态同时收起状态菜单（closeOrderStatusMenu）",
    re.search(r"function closeOrderStatusMenu\(\)[\s\S]{0,200}?m\.remove\(\)", html) is not None)

# A3 解锁后才有小箭头 / 点击入口（锁定态点状态什么都不发生）
chk("行内状态单元格由 _orderStatusEditable 门控（仅云端 + 已解锁 + id 合法）",
    re.search(r"function _orderStatusEditable\(oidS\) \{\s*\n\s*return FINANCE_MODE !== 'local' && !!unlocked\.orderstatus && !!oidS",
              html) is not None)
chk("小箭头 st-caret 只出现在可编辑分支里",
    re.search(r"_orderStatusEditable\(oidS\)[\s\S]{0,400}?st-caret", html) is not None)
chk("锁定分支就是纯标签（没有箭头 / 没有 onclick）",
    re.search(r": _stTag\) \+ '</td>';", html) is not None)
chk("点状态标签 stopPropagation（否则会同时展开文件明细）",
    "onclick=\"event.stopPropagation();openOrderStatusMenu(event," in html)

# A4 写路径
chk("提交打到 /api/proxy/admin/order_status（POST）",
    re.search(r"fetch\('/api/proxy/admin/order_status', \{\s*\n\s*method: 'POST'", html) is not None)
chk("提交带 order_id 与 status",
    re.search(r"body: JSON\.stringify\(\{ order_id: String\(oid\), status: status \}\)", html) is not None)
chk("失败不落地（success 校验失败即 return，不改本地状态）",
    re.search(r"if \(!\(res && res\.success\)\) \{ toast\('❌ '[\s\S]{0,200}?return; \}[\s\S]{0,900}?o\.status = status;", html) is not None)
chk("本地落地同时改文件级状态（金额/收益/KPI 都由文件状态现算）",
    re.search(r"o\.status = status;\s*\n\s*\(o\.files \|\| \[\]\)\.forEach\(function \(f\) \{ f\.status = status; \}\);", html) is not None)
chk("落地后重绘整页（renderCloud）→ KPI/图表/明细按新状态重算",
    re.search(r"o\.status = status;[\s\S]{0,300}?renderCloud\(\);", html) is not None)
chk("防重复提交（_orderStatusSaving 闸门）",
    "_orderStatusSaving" in html and re.search(r"if \(_orderStatusSaving\) \{ toast", html) is not None)
chk("重绘订单区块会收起菜单（菜单挂 body，不随区块替换）",
    re.search(r"function _renderOrderListHTML\(\) \{\s*\n\s*closeOrderStatusMenu\(\);", html) is not None)

# A5 前端可选状态 == 后端白名单（两侧同源，少一处就是「页面能选、后端 400」）
m_fe = re.search(r"var ORDER_FIXABLE_STATUSES = \[([^\]]+)\]", html)
m_be = re.search(r"MANUAL_FIX_STATUSES = \(([^)]+)\)", app_src)
fe_list = [s.strip().strip("'") for s in m_fe.group(1).split(",")] if m_fe else []
be_list = [s.strip().strip('"') for s in m_be.group(1).split(",")] if m_be else []
chk("前端 ORDER_FIXABLE_STATUSES 存在且 = 后端 MANUAL_FIX_STATUSES",
    fe_list == be_list and len(fe_list) == 5, "fe=%s be=%s" % (fe_list, be_list))
chk("白名单只含终态（sent/failed/abandoned/canceled/rejected）——"
    "排队中/打印中会被推送与预约调度重新捡起来打印，不能人工改",
    set(fe_list) == {"sent", "failed", "abandoned", "canceled", "rejected"})
chk("本地模式不给订正入口（本地订单库存本地库、无对应写接口）",
    "FINANCE_MODE !== 'local' && !!unlocked.orderstatus" in html and
    re.search(r"function _orderStatusLockHTML\(\) \{\s*\n\s*if \(FINANCE_MODE === 'local'\) return '';", html) is not None)

# A6 后端：printer token 鉴权 + 父/子一起改 + 事务
chk("后端接口存在且需 printer token",
    re.search(r"@app\.route\(\"/api/admin/order_status\", methods=\[\"POST\"\]\)", app_src) is not None and
    re.search(r"def admin_order_status\(\):[\s\S]{0,900}?_get_printer_token\(\)[\s\S]{0,300}?token != PRINTER_TOKEN", app_src) is not None)
chk("父订单与全部子任务在**同一个事务**里改写（BEGIN IMMEDIATE）",
    re.search(r"def admin_order_status\(\):[\s\S]{0,2000}?BEGIN IMMEDIATE[\s\S]{0,1200}?"
              r"UPDATE order_files SET status = \?[\s\S]{0,400}?UPDATE orders SET status = \?", app_src) is not None)
chk("子任务改写同时清空 locked_at（终态不再被超时/孤儿回收盯上）",
    "UPDATE order_files SET status = ?, locked_at = '' WHERE order_id = ?" in app_src)
chk("无子任务的历史老单也会把父订单改成目标状态（UPDATE orders 独立执行）",
    re.search(r"UPDATE order_files SET status = \?, locked_at = '' WHERE order_id = \?\"?,\s*\n\s*\(new_status, order_id\),[\s\S]{0,300}?"
              r"UPDATE orders SET status = \?", app_src) is not None)

# A7 三个对抗式验证查出的问题（2026-10-18，同一轮修掉，防止再退回去）
chk("请求体先做 dict 守卫再取 token（畸形 JSON 数组/字符串不得把鉴权表达式打成 500）",
    re.search(r"data = request\.get_json\(silent=True\)\s*\n\s*if not isinstance\(data, dict\):\s*\n\s*data = \{\}\s*\n\s*token = "
              r"_get_printer_token\(\) or str\(data\.get\(\"token\"\) or \"\"\)", app_src) is not None)
chk("order_id 只认规范十进制（纯数字、无前后空白、无前导零）—— 防 SQLite 数值亲和性折算",
    re.search(r's_id = raw_id if isinstance\(raw_id, str\) else str\(raw_id\)\s*\n\s*if not re\.fullmatch'
              r'\(r"\[0-9\]\+", s_id\) or \(len\(s_id\) > 1 and s_id\[0\] == "0"\)', app_src) is not None)
chk("status 也做类型守卫（非字符串一律拒绝，不靠 str() 兜底）",
    re.search(r'raw_status = data\.get\("status"\)\s*\n\s*new_status = raw_status\.strip\(\) if isinstance\(raw_status, str\) else ""',
              app_src) is not None)
chk("前端 applyOrderStatus 的守卫与模板同源（_orderStatusEditable：含本地模式判据）",
    re.search(r"function applyOrderStatus\(oid, status\) \{\s*\n\s*closeOrderStatusMenu\(\);\s*\n\s*if \(!_orderStatusEditable\(oid\)\)",
              html) is not None)
chk("前端 openOrderStatusMenu 的守卫**同一判据**且在创建菜单之前（本地模式/锁定态不该弹出菜单）",
    re.search(r"function openOrderStatusMenu\(ev, oid\) \{[\s\S]{0,1200}?if \(!_orderStatusEditable\(oid\)\)[\s\S]{0,1600}?document\.body\.appendChild\(m\)",
              html) is not None)

# ============================================================================
print("=== B. 行为验收（真实 Flask + 真实 SQLite）===")

TMP = tempfile.mkdtemp(prefix="hn_statusfix_")
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
URL = "/api/admin/order_status"


def mk_order(status, files, number="HN20261018-0001", is_admin_print=0, total_price=4.60,
             created_at="2026-10-18 10:00:00"):
    """files = [(status, total_price, locked_at), ...]；返回 order_id"""
    conn = backend.get_db()
    cur = conn.execute(
        "INSERT INTO orders (file, copies, status, created_at, openid, duplex, page_count,"
        " total_price, order_number, source, is_admin_print)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("x.pdf", 1, status, created_at, "openid-fix", "on", 2, total_price,
         number, "wechat", is_admin_print))
    oid = cur.lastrowid
    for st, tp, locked in files:
        conn.execute(
            "INSERT INTO order_files (order_id, file_name, copies, page_count, total_price,"
            " status, created_at, page_range, locked_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (oid, "f.pdf", 1, 2, tp, st, created_at, "", locked))
    conn.commit()
    conn.close()
    return oid


def db_state(oid):
    conn = backend.get_db()
    o = conn.execute("SELECT status FROM orders WHERE id = ?", (oid,)).fetchone()
    f = conn.execute("SELECT status, locked_at FROM order_files WHERE order_id = ? ORDER BY id",
                     (oid,)).fetchall()
    agg = backend.aggregate_order_status(conn, oid)
    conn.close()
    return (o["status"] if o else None,
            [(r["status"], r["locked_at"]) for r in f], agg)


def post(payload, headers=HDR):
    return client.post(URL, headers=headers, json=payload)


print("\n-- B1 鉴权 / 参数校验 --")
oid_ab = mk_order("abandoned", [("abandoned", 4.60, "2026-10-18 10:00:00")], "HN-AUTH-1")
before = db_state(oid_ab)
r = post({"order_id": oid_ab, "status": "sent"}, headers={})
chk("无 token → 403，且库未被改动", r.status_code == 403 and db_state(oid_ab) == before,
    "%s %s" % (r.status_code, r.get_json()))
r = post({"order_id": oid_ab, "status": "sent"}, headers={"X-Printer-Token": "wrong"})
chk("错误 token → 403", r.status_code == 403)
r = post({"status": "sent"})
chk("缺 order_id → 400", r.status_code == 400, r.get_json())
r = post({"order_id": oid_ab})
chk("缺 status → 400", r.status_code == 400, r.get_json())
for badst in ("printing", "queued", "waiting", "scheduled", "offline_unknown", "BOGUS"):
    r = post({"order_id": oid_ab, "status": badst})
    if r.status_code != 400:
        break
chk("非终态（printing/queued/waiting/scheduled/offline_unknown…）一律 400 拦下"
    "（否则会被推送/预约调度重新捡起来打印）",
    r.status_code == 400, "%s %s" % (r.status_code, r.get_json()))
chk("非法状态被拦后库仍未被改动", db_state(oid_ab) == before, db_state(oid_ab))
r = post({"order_id": 99999999, "status": "sent"})
chk("订单不存在 → 404", r.status_code == 404, r.get_json())

print("\n-- B1b 畸形请求体 / order_id 亲和性（对抗式验证查出的三条）--")
before_ab = db_state(oid_ab)
# 请求体不是 JSON 对象时：鉴权表达式自己不能崩（未认证 500）。无 token → 403。
for raw in ([b'["sent"]', b'"sent"', b'123', b'true', b'', b'{bad json']):
    rr = client.post(URL, headers={"Content-Type": "application/json"}, data=raw)
    if rr.status_code not in (400, 403):
        break
chk("未认证 + 畸形请求体（数组/字符串/数字/布尔/空/坏 JSON）→ 403，绝不 500",
    rr.status_code == 403, "%s %s" % (rr.status_code, rr.get_json()))
# 带 token 的非对象请求体 → 400（不是 500）
for raw in ([b'["sent"]', b'"sent"', b'123', b'true']):
    rr = client.post(URL, headers=HDR, data=raw, content_type="application/json")
    if rr.status_code != 400:
        break
chk("带 token + 非对象请求体 → 400（不是 500）", rr.status_code == 400,
    "%s %s" % (rr.status_code, rr.get_json()))
# order_id 数值亲和性：这些写法在 SQLite 里都会命中 id=1 —— 必须拒绝而不是改掉订单 1
for aff in ("1.0", "01", "+1", " 1 ", "1e0", 1.0, True, ["1"], {"id": 1}, None, ""):
    rr = post({"order_id": aff, "status": "sent"})
    if rr.status_code != 400:
        break
chk("order_id 亲和性/类型变体（'1.0'/'01'/'+1'/' 1 '/'1e0'/1.0/True/['1']/{}…）一律 400",
    rr.status_code == 400, "%s %s" % (rr.status_code, rr.get_json()))
chk("上述畸形请求一条都没改动数据库", db_state(oid_ab) == before_ab, db_state(oid_ab))
r_int = post({"order_id": oid_ab, "status": "sent"})   # 整数 id 仍正常
chk("整数 order_id 照常可用（拦的是畸形，不是正常调用）", r_int.status_code == 200,
    r_int.get_json())

print("\n-- B2 五种终态：父订单 + 全部子任务一起改 --")
cases = [
    ("sent",       [("queued", 2.30, "2026-10-18 10:00:00"), ("printing", 2.30, "2026-10-18 10:00:05")]),
    ("failed",     [("printing", 2.30, "2026-10-18 10:00:00"), ("accepted", 2.30, "2026-10-18 10:00:05")]),
    ("abandoned",  [("queued", 2.30, "2026-10-18 10:00:00")]),
    ("canceled",   [("printing", 2.30, "2026-10-18 10:00:00"), ("sent", 2.30, "")]),
    ("rejected",   [("queued", 2.30, "2026-10-18 10:00:00"), ("failed", 2.30, "")]),
]
for i, (target, files) in enumerate(cases):
    oid = mk_order("queued", files, number="HN-FIX-%d" % i)
    r = post({"order_id": oid, "status": target})
    j = r.get_json() or {}
    o_st, f_list, agg = db_state(oid)
    chk("改为 %s：HTTP 200 + success + 父订单=%s" % (target, target),
        r.status_code == 200 and j.get("success") and o_st == target,
        "%s %s → %s" % (r.status_code, j, o_st))
    chk("改为 %s：全部子任务都写成 %s（含原本已 sent 的）" % (target, target),
        all(s == target for s, _ in f_list) and len(f_list) == len(files),
        f_list)
    chk("改为 %s：聚合状态 == %s（只改 orders.status 会被聚合刷回去）" % (target, target),
        agg == target, agg)
    chk("改为 %s：locked_at 全部清空（终态不再被 3 分钟超时/保活盯上）" % target,
        all(lk == "" for _, lk in f_list), f_list)
    chk("改为 %s：回执带 order_id/status/previous_status" % target,
        j.get("order_id") == str(oid) and j.get("status") == target and j.get("previous_status") == "queued",
        j)

print("\n-- B3 幂等 / 无子任务的历史老单 --")
oid_sent = mk_order("sent", [("sent", 2.30, "")], number="HN-FIX-IDEM")
r1 = post({"order_id": oid_sent, "status": "sent"})
r2 = post({"order_id": oid_sent, "status": "sent"})
chk("重复提交同一状态仍成功（幂等）",
    r1.status_code == 200 and r2.status_code == 200 and db_state(oid_sent)[0] == "sent",
    "%s / %s" % (r1.get_json(), r2.get_json()))
oid_hist = mk_order("abandoned", [], number="HN-FIX-HIST")
r = post({"order_id": oid_hist, "status": "sent"})
j = r.get_json() or {}
chk("无子任务的历史老单：父订单照改、files_updated=0、不报错",
    r.status_code == 200 and j.get("success") and db_state(oid_hist)[0] == "sent"
    and j.get("files_updated") == 0, "%s %s" % (r.status_code, j))

print("\n-- B4 端到端：状态决定结算口径（这正是本功能的意义）--")
oid_e2e = mk_order("abandoned", [("abandoned", 2.30, ""), ("abandoned", 2.30, "")],
                   number="HN-FIX-E2E", total_price=4.60,
                   created_at="2026-10-18 09:00:00")
REV = ("/api/admin/statistics/revenue?start_date=2026-10-18&end_date=2026-10-18"
       "&status=sent,printing,queued,failed,canceled,rejected,accepted,abandoned,reserved,"
       "offline_unknown,scheduled,waiting,downloading&token=" + TOKEN)


def revenue_of(oid):
    j = client.get(REV).get_json() or {}
    for o in (j.get("data") or {}).get("orders", []):
        if str(o.get("order_id")) == str(oid):
            return o
    return None


o_before = revenue_of(oid_e2e)
chk("订正前（abandoned）：订单明细里状态为 abandoned、收益 0（不计入结算）",
    o_before and o_before["status"] == "abandoned" and (o_before["revenue"] + o_before["receivable"]) == 0,
    o_before and (o_before["status"], o_before["revenue"], o_before["receivable"]))
post({"order_id": oid_e2e, "status": "sent"})
o_after = revenue_of(oid_e2e)
chk("订正为 sent 后：明细状态 = sent（aggregate_order_status 现算）",
    o_after and o_after["status"] == "sent", o_after and o_after["status"])
chk("订正为 sent 后：金额计入收益（2 文件 × ¥2.30 + 附加 ¥0.00 = ¥4.60）",
    o_after and round(o_after["revenue"] + o_after["receivable"], 2) == 4.60,
    o_after and (o_after["revenue"], o_after["receivable"]))
chk("订正后文件级状态也随之为 sent（前端展开行显示的即这一份）",
    o_after and all(f["status"] == "sent" for f in o_after["files"]),
    o_after and [f["status"] for f in o_after["files"]])
post({"order_id": oid_e2e, "status": "failed"})
o_fail = revenue_of(oid_e2e)
chk("再订正为 failed：状态变 failed、金额重新不计入收益（订正是双向的）",
    o_fail and o_fail["status"] == "failed"
    and round(o_fail["revenue"] + o_fail["receivable"], 2) == 0.0,
    o_fail and (o_fail["status"], o_fail["revenue"], o_fail["receivable"]))

print("\n" + "=" * 52)
if bad:
    print("订单状态订正验收：存在失败 ❌ (%d)" % bad)
else:
    print("订单状态订正验收：全部通过 ✅")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if bad else 0)
