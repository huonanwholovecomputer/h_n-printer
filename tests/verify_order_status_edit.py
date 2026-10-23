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
     · 已 sent 的子任务也照改（人工订正只表达「云端不再主动让它打印」）；无子任务的老单只改父订单
     · 幂等：重复提交同一状态仍成功
     · 端到端：改成 sent 后 /api/admin/statistics/revenue 里该单状态为 sent 且计入收益；
       此前为 abandoned 时不计入收益（状态决定结算口径 —— 这正是本功能的意义）
     · B6：**设备是这张单的最终裁决者**（2026-10-18 用户口径）—— 设备离线期间照旧出纸并如实回报
       成功时，print_success 把人工三终态（canceled/abandoned/rejected）也覆盖成 sent（纸确实出来了、
       账要算）；反过来 print_fail **不覆盖**这三态（「取消正在打印的订单」时剩余任务回报的"已取消"
       失败不该把标签改成「打印失败」，与钱无关）

用法: PYTHONPATH= PYTHONUTF8=1 <python> tests/verify_order_status_edit.py   （exit 0 = 全绿）
"""
import io
import os
import re
import shutil
import sys
import tempfile
import time

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

# A4b F4（2026-10-18 审查 🟡）：内联 onclick 里的 order_id 必须是**引号字符串**，否则会被当
# JS 表达式求值（无引号的 `...event,' + oidS + ')'` 渲染出 `openOrderStatusMenu(event,1e3)`，
# JS 求值成 1000 —— 页面「订单 1000」的标签真的会变）；同时可编辑闸门只认**规范十进制**。
# ⚠ 这三条必须**判别性**：现有 A3 那两条（`!!oidS` 前缀 / `...openOrderStatusMenu(event,` 前缀）
# 是超集匹配，改前源码同样成立，防不住 F4。改前红在第 1/2/3 条上。
chk("F4a 行内 onclick 把 oidS 拼成**引号字符串**：openOrderStatusMenu(event,\\'' + oidS + '\\')"
    "（改前 `...event,' + oidS + ')'` 会被当 JS 表达式求值：'1e3' → 1000，写到另一张单）",
    re.search(r"openOrderStatusMenu\(event,\\'' \+ oidS \+ '\\'\)", html) is not None)
chk("F4a 旧形态（未加引号）已消失：openOrderStatusMenu(event,' + oidS + ')",
    "openOrderStatusMenu(event,' + oidS + ')" not in html)
chk("F4a 同款老写法 _toggleOrderExpand 也改成引号字符串（旧形态已消失）",
    re.search(r"_toggleOrderExpand\(\\'' \+ oidS \+ '\\'\)", html) is not None
    and "_toggleOrderExpand(' + oidS + ')" not in html)
chk("F4b _orderStatusEditable 函数体内只认十进制 id（/^[0-9]+$/，与后端 "
    "re.fullmatch(r\"[0-9]+\") 同源）—— 改前只有 !!oidS：safeId 白名单放行 '1e3'/'12-34'",
    re.search(r"function _orderStatusEditable\(oidS\)\s*\{[\s\S]{0,600}?/\^\[0-9\]\+\$/",
              html) is not None)

# A4c F5（2026-10-18 审查 🟡）：订正成功后必须把口径缓存整体失效 —— 每个口径的响应是独立对象
# （_cloudData 与 _scopeCache[key] 同一引用），只改当前这一个，切回先前访问过的口径会命中缓存
# 显示订正前的旧状态（库里已是新值）。判据限定在 applyOrderStatus **函数体内**且位于
# `o.status = status;` **之后**：全文件搜 `_scopeCache = {}` 会被 fetchScopeData(force) 的两处
# 命中而恒真（新旧源码都成立）。改前红在本条（函数体内没有这句）。
_m_apply = re.search(r"function applyOrderStatus\(oid, status\) \{([\s\S]*?)\n\}", html)
_apply_body = _m_apply.group(1) if _m_apply else ""
_i_land = _apply_body.find("o.status = status;")
_i_cache = _apply_body.find("_scopeCache = {}")
chk("F5 applyOrderStatus 函数体内、本地落地（o.status = status;）之后清空口径缓存 "
    "_scopeCache = {}（否则切回旧口径看到订正前状态）",
    _i_land >= 0 and _i_cache > _i_land,
    "land=%s cache=%s" % (_i_land, _i_cache))

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
    # F8b（2026-10-18）：窗口从 {0,900} 放宽到 {0,2500} —— `def admin_order_status():` 到
    # `_get_printer_token()` 的距离会随后端 docstring 增长（本批 F1/F2/F3 已从 848 涨到 1435，
    # 旧窗口余量只剩 52，docstring 再加一行就假红）。判据没变：接口存在 + 取 token 后再比对。
    # S0-06（2026-10-23）：26 处内联 `token != PRINTER_TOKEN` 已收敛到统一助手
    # `_printer_token_ok()`（轮换双接受 + 恒时比较）。判据随之接受新形态，语义不变：
    # 取 token → 判定。把这一步删掉（不判 token）仍会红。
    re.search(r"def admin_order_status\(\):[\s\S]{0,2500}?_get_printer_token\(\)[\s\S]{0,300}?"
              r"(?:token != PRINTER_TOKEN|_printer_token_ok\(token\))", app_src) is not None)
chk("父订单与全部子任务在**同一个事务**里改写（BEGIN IMMEDIATE）",
    # F8b 同款：窗口也随后端 docstring/函数体增长（本批 F1 在 BEGIN 之前加了「收集归属设备」
    # 一段，`def → BEGIN IMMEDIATE` 实测已涨到 2724，旧窗口 {0,2000} 直接假红）。
    # 判据没变：同一个函数里 BEGIN IMMEDIATE → 改 order_files → 改 orders 的顺序。
    re.search(r"def admin_order_status\(\):[\s\S]{0,4000}?BEGIN IMMEDIATE[\s\S]{0,2500}?"
              r"UPDATE order_files SET status = \?[\s\S]{0,400}?UPDATE orders SET status = \?", app_src) is not None)
chk("子任务改写同时清空 locked_at（终态不再被超时/孤儿回收盯上）",
    "UPDATE order_files SET status = ?, locked_at = '' WHERE order_id = ?" in app_src)
chk("无子任务的历史老单也会把父订单改成目标状态（UPDATE orders 独立执行）",
    re.search(r"UPDATE order_files SET status = \?, locked_at = '' WHERE order_id = \?\"?,\s*\n\s*\(new_status, order_id\),[\s\S]{0,300}?"
              r"UPDATE orders SET status = \?", app_src) is not None)

# A7 三个对抗式验证查出的问题（2026-10-18，同一轮修掉，防止再退回去）
chk("请求体先做 dict 守卫再取 token（畸形 JSON 数组/字符串不得把鉴权表达式打成 500）",
    # F8b（2026-10-18，随 F3 新形态同步）：深嵌套请求体会让 json.loads 抛 RecursionError，
    # `silent=True` 挡不住 ⇒ 解析本身也被 try/except 包住（data = None），后面仍做 dict 守卫再取 token。
    # 静态只钉写法；**真正的证据是 B7**（depth 1000/2000/5000：无 token 403 / 带 token 400、500 计数 0）。
    re.search(r"try:\s*\n\s*data = request\.get_json\(silent=True\)\s*\n\s*except Exception:\s*\n"
              r"[\s\S]{0,200}?data = None\s*\n\s*if not isinstance\(data, dict\):\s*\n\s*data = \{\}\s*\n\s*token = "
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

# A8 三终态常量的后端契约（F1；判据查函数体里是否真做了这件事）
# ⚠ 常量语义（2026-10-18 用户口径后收窄）=「**云端**不要再主动让它打印」，
# 不否定设备已出纸：设备迟到的成功回报照样能把这三态覆盖成 sent（见 B6）。
# 事件名/payload 契约见 B5；这里是**常量**与 **start_printing 父订单守卫**两处静态接线。
chk("后端终态常量 NO_LONGER_PRINTABLE_STATUSES = (canceled, abandoned, rejected)"
    "（failed 刻意不在列——它仍可被迟到的成功回报救回，见 B6）",
    re.search(r'NO_LONGER_PRINTABLE_STATUSES = \("canceled", "abandoned", "rejected"\)',
              app_src) is not None)
_m_sp = re.search(r"def on_start_printing\(data\):([\s\S]*?)(?=\n@socketio\.on|\n@app\.route|\ndef )",
                  app_src)
_sp_body = _m_sp.group(1) if _m_sp else ""
chk("start_printing 读父订单状态并按**三终态常量**跳过（改前只认 `== \"canceled\"`："
    "abandoned/rejected 的预约单到点仍会复活开打）",
    "NO_LONGER_PRINTABLE_STATUSES" in _sp_body
    and re.search(r'\[\s*"status"\s*\]\s+in\s+NO_LONGER_PRINTABLE_STATUSES', _sp_body) is not None
    and "FROM orders WHERE id = ?" in _sp_body)
chk("start_printing 不再只认单一 canceled（改前形态已消失）",
    _sp_body != "" and '== "canceled"' not in _sp_body)


# A9 v7.1 订单**类型**订正（接单 ⇄ 自打）——与状态**共用同一把锁**，不设第二个密码
chk("MODULE_NAMES 的 orderstatus 描述已包含「类型」（设置页密码那一行读的就是它）",
    re.search(r"orderstatus: '订单状态/类型订正'", html) is not None)
chk("类型表头挂 _orderTypeLockHTML()（锁在表头，不是每行）",
    "类型' + _orderTypeLockHTML()" in html)
chk("类型锁按钮走**同一个** toggleOrderStatusLock（不另开一把锁/不另要一次密码）",
    re.search(r"function _orderTypeLockHTML\(\)[\s\S]{0,700}?onclick=\"event\.stopPropagation\(\);toggleOrderStatusLock\(\)\"",
              html) is not None)
chk("类型可编辑判据与状态同源（_orderTypeEditable → _orderStatusEditable）",
    re.search(r"function _orderTypeEditable\(oidS\) \{ return _orderStatusEditable\(oidS\); \}", html) is not None)
chk("前端只允许 receive/self（与后端 MANUAL_FIX_ORDER_TYPES 同源）",
    re.search(r"var ORDER_FIXABLE_TYPES = \['receive', 'self'\];", html) is not None
    and re.search(r'MANUAL_FIX_ORDER_TYPES = \("receive", "self"\)', app_src) is not None)
chk("类型槽在解锁后才有可点入口（锁定态是纯标签）",
    re.search(r"if \(!_orderTypeEditable\(oidT\)\) return mtag;", html) is not None
    and "openOrderTypeMenu(event," in html)
chk("类型提交打到 /api/proxy/admin/order_type（POST，带 order_id + order_type）",
    re.search(r"fetch\('/api/proxy/admin/order_type', \{\s*\n\s*method: 'POST'", html) is not None
    and re.search(r"body: JSON\.stringify\(\{ order_id: String\(oid\), order_type: type \}\)", html) is not None)
chk("类型落地只改 is_admin_print（不动 status/文件级状态）",
    re.search(r"o\.is_admin_print = \(type === 'self'\);", html) is not None)
chk("设置页给 orderstatus 补了一句「这把密码同时管状态与类型两列」",
    "同时管订单明细的<b>「状态」</b>与<b>「类型」</b>两把锁" in html)
chk("代理白名单透传类型回执（order_type/previous_order_type/is_admin_print）",
    all(k in open(os.path.join(REPO, "local_print_tool", "stats_server.py"),
                  encoding="utf-8").read() for k in
        ('"order_type"', '"previous_order_type"', '"is_admin_print"')))

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

print("\n-- B5 通知设备（🔴 核心）：订正为「云端不再主动让它打印」的三终态必须通知到设备 --")
# 契约（冻结）：事件名 order_canceled；payload {"order_id": int, "task_ids": [int],
#                 "reason": "manual_fix", "status": "<新状态>"}
# 改前红在哪：app.py 的 admin_order_status 全函数无 emit —— 归属设备 get_received() 为空，
#             下面每一条 B5① 的第一半（收到 1 个 order_canceled）都会红。
DEV_A, DEV_B = "fix-dev-A", "fix-dev-B"
for _cid in (DEV_A, DEV_B):
    _rc = client.post("/api/printer/claim", headers=HDR, json={"client_id": _cid})
    chk("B5 夹具：设备 %s 启用接单（兜底广播只发给在线且接单的设备）" % _cid,
        _rc.status_code == 200 and (_rc.get_json() or {}).get("success") is True, _rc.get_json())


def _sock(cid):
    """真实 SocketIO 连接（test_client）。query_string 只能是 "?k=v&…" 字符串。"""
    c = backend.socketio.test_client(backend.app,
                                     query_string="?token=%s&client_id=%s" % (TOKEN, cid))
    chk("B5 夹具：socket 设备 %s 已连接" % cid, c.is_connected(), cid)
    c.get_received()          # 丢掉 connect 阶段的 printer_state / storage_config_updated
    return c


SOCK_A, SOCK_B = _sock(DEV_A), _sock(DEV_B)


def _sub_ids(oid):
    conn = backend.get_db()
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM order_files WHERE order_id = ? ORDER BY id", (oid,)).fetchall()]
    conn.close()
    return ids


def _set_owner(oid, operator="", last="", target=""):
    """给夹具订单补归属：子任务 operator_client / last_operator_client + 父订单 target_client。"""
    conn = backend.get_db()
    conn.execute("UPDATE order_files SET operator_client = ?, last_operator_client = ?"
                 " WHERE order_id = ?", (operator, last, oid))
    conn.execute("UPDATE orders SET target_client = ? WHERE id = ?", (target, oid))
    conn.commit()
    conn.close()


def _cancel_evs(c):
    """取该连接收到的 order_canceled 事件（并清空收件箱）。"""
    evs = c.get_received()
    return [e for e in evs if e.get("name") == "order_canceled"], \
           [e.get("name") for e in evs]


_n_b5 = [0]


def _b5_case(target, *, operator="", last="", target_client=""):
    _n_b5[0] += 1
    oid = mk_order("queued", [("printing", 2.30, "2026-10-18 10:00:00")],
                   number="HN-B5-%d" % _n_b5[0])
    _set_owner(oid, operator=operator, last=last, target=target_client)
    tids = _sub_ids(oid)
    SOCK_A.get_received()
    SOCK_B.get_received()
    r = post({"order_id": oid, "status": target})
    evA, namesA = _cancel_evs(SOCK_A)
    evB, namesB = _cancel_evs(SOCK_B)
    return oid, tids, r, evA, namesA, evB, namesB


for _st in ("canceled", "abandoned", "rejected"):
    oid, tids, r, evA, namesA, evB, namesB = _b5_case(_st, operator=DEV_A)
    chk("B5① 订正为 %s：operator_client 归属设备 A 收到 order_canceled，B 零事件（改前 A 也是零）"
        % _st, r.status_code == 200 and len(evA) == 1 and not evB,
        "A=%s B=%s" % (namesA, namesB))
    if evA:
        p = evA[0].get("args", [{}])[0] or {}
        chk("B5① %s payload 契约：order_id(int)=%s / task_ids=%s / reason=manual_fix / status=%s"
            % (_st, oid, tids, _st),
            p.get("order_id") == oid and isinstance(p.get("order_id"), int)
            and p.get("task_ids") == tids and p.get("reason") == "manual_fix"
            and p.get("status") == _st and len(tids) == 1, p)

# 断线回滚后 operator_client 为空、只有 last_operator_client —— 也是「摸过这单的设备」
oid, tids, r, evA, namesA, evB, namesB = _b5_case("canceled", last=DEV_A)
chk("B5② 只有 last_operator_client 记录（operator_client 被断线回滚清空）也命中归属设备",
    len(evA) == 1 and not evB, "A=%s B=%s" % (namesA, namesB))

# 无归属记录、但有订单目标设备
oid, tids, r, evA, namesA, evB, namesB = _b5_case("canceled", target_client=DEV_A)
chk("B5③ orders.target_client 命中的在线设备也收到（三来源并集）",
    len(evA) == 1 and not evB, "A=%s B=%s" % (namesA, namesB))

# 订正为 sent / failed 不进「云端不再主动打印」集合 → 零事件（设备仍可打完 / 迟到回报仍可覆盖）
for _st in ("sent", "failed"):
    oid, tids, r, evA, namesA, evB, namesB = _b5_case(_st, operator=DEV_A)
    chk("B5④ 订正为 %s：**零事件**（get_received() 为空；设备侧不清理，failed 例外语义见 B6）" % _st,
        r.status_code == 200 and not namesA and not namesB,
        "A=%s B=%s" % (namesA, namesB))

# 目标设备一个都不在线 → 兜底广播给所有「在线且接单」的设备（各一次）
oid, tids, r, evA, namesA, evB, namesB = _b5_case(
    "abandoned", operator="ghost-op", last="ghost-last", target_client="ghost-target")
chk("B5⑤ 归属/目标设备全离线 → 在线且接单的 A、B **各收到一次**（兜底广播；无关设备上清理是空操作）",
    len(evA) == 1 and len(evB) == 1, "A=%s B=%s" % (namesA, namesB))
if evA and evB:
    pa = evA[0].get("args", [{}])[0] or {}
    pb = evB[0].get("args", [{}])[0] or {}
    chk("B5⑤ 兜底广播的 payload 与直发同契约（reason/status/order_id/task_ids）",
        pa == pb and pa.get("reason") == "manual_fix" and pa.get("status") == "abandoned"
        and pa.get("order_id") == oid and pa.get("task_ids") == tids, (pa, pb))

print("\n-- B6 迟到回报语义（2026-10-18 用户口径：设备是这张单的最终裁决者）--")
# 用户原话：「打印出来就打印出来吧……离线的本地工具最终给这个订单标记的状态是啥，最终云端的
#   该订单就覆盖啥，决定权在打印这个订单的机器手里。」
# ⇒ 设备离线没收到 order_canceled、照旧把纸打出来并如实回报成功时，print_success 把人工三终态
#   （canceled/abandoned/rejected）也覆盖成 sent：纸确实出来了、账要算上；人工订正只表达
#   「云端不再主动让它打印」，不否定设备已出纸的事实。
# 改前红在哪：改前白名单不含这三态 ⇒ `UPDATE … WHERE status IN (…'queued','failed')` 命中 0 行
#   ⇒ 子任务停在原终态，B6① 的「覆盖成 sent」那半直接红（下面第 4 轮的行内变异反向验证复现）。


def _set_reject_reason(tid, text):
    conn = backend.get_db()
    conn.execute("UPDATE order_files SET reject_reason = ? WHERE id = ?", (text, tid))
    conn.commit()
    conn.close()


def _reject_reason(tid):
    conn = backend.get_db()
    row = conn.execute("SELECT reject_reason FROM order_files WHERE id = ?", (tid,)).fetchone()
    conn.close()
    return (row["reject_reason"] or "") if row else None


for _st in ("canceled", "abandoned", "rejected"):
    oid = mk_order("queued", [("printing", 2.30, "2026-10-18 10:00:00")],
                   number="HN-B6-%s" % _st)
    _set_owner(oid, operator=DEV_A, last=DEV_A)
    tid = _sub_ids(oid)[0]
    post({"order_id": oid, "status": _st})
    _set_reject_reason(tid, "订正留下的旧原因")        # 翻成 sent 时必须一并清掉
    SOCK_A.get_received()
    SOCK_A.emit("print_success", {"task_id": tid})     # 归属设备 A 的成功回报
    time.sleep(0.05)
    SOCK_A.get_received()
    _o_st, _f_list, _agg = db_state(oid)
    chk("B6① 订正 %s 后设备成功回报 → 父+子**都覆盖成 sent**（设备是最终裁决者；"
        "改前白名单不含三态 ⇒ 命中 0 行 ⇒ 停在 %s）" % (_st, _st),
        _o_st == "sent" and _f_list[0][0] == "sent", (_o_st, _f_list))
    chk("B6① 订正 %s → sent 覆盖时清空 reject_reason（已成 sent 的单不该留着旧失败原因）" % _st,
        _reject_reason(tid) == "", _reject_reason(tid))

oid = mk_order("queued", [("printing", 2.30, "2026-10-18 10:00:00")], number="HN-B6-failed")
_set_owner(oid, operator=DEV_A, last=DEV_A)
tid = _sub_ids(oid)[0]
post({"order_id": oid, "status": "failed"})
_set_reject_reason(tid, "订正留下的旧原因")
SOCK_A.get_received()
SOCK_A.emit("print_success", {"task_id": tid})
time.sleep(0.05)
SOCK_A.get_received()
_f_st, _f_list, _f_agg = db_state(oid)
chk("B6② 订正 failed 后迟到 print_success → 救回 sent（2026-09 既有决定，必须保留）",
    _f_st == "sent" and _f_list[0][0] == "sent", (_f_st, _f_list))

# 反向判别：**失败回报不覆盖**这三态 —— on_print_fail 的白名单刻意不含三态与 sent（与 print_success
# 刻意不对称）。典型来源是「取消正在打印的订单」时本地工具对剩余任务回报的 error="已取消"：
# 放行只会把「已取消」标签弄成「打印失败」（release 现场可见的假回归），且失败回报不带来收益。
for _st in ("canceled", "abandoned", "rejected"):
    oid = mk_order("queued", [("printing", 2.30, "2026-10-18 10:00:00")],
                   number="HN-B6f-%s" % _st)
    _set_owner(oid, operator=DEV_A, last=DEV_A)
    tid = _sub_ids(oid)[0]
    post({"order_id": oid, "status": _st})
    SOCK_A.get_received()
    SOCK_A.emit("print_fail", {"task_id": tid, "error": "已取消"})
    time.sleep(0.05)
    SOCK_A.get_received()
    _o_st, _f_list, _agg = db_state(oid)
    chk("B6③ 订正 %s 后设备发 print_fail(error=\"已取消\") → 状态**保持 %s**"
        "（失败回报不覆盖终态；否则「已取消」标签会变成「打印失败」）" % (_st, _st),
        _o_st == _st and _f_list[0][0] == _st, (_o_st, _f_list))

print("\n-- B7 深嵌套请求体（depth≥1000）不得打出 500（fail-closed）--")


def _deep_body(depth):
    """{"order_id":1,"status":"sent","junk":[[[…]]]} —— json.loads 会抛 RecursionError。"""
    return ('{"order_id": 1, "status": "sent", "junk": '
            + "[" * depth + "]" * depth + "}").encode("utf-8")


_b7 = []
for depth in (1000, 2000, 5000):
    r_no = client.post(URL, headers={"Content-Type": "application/json"}, data=_deep_body(depth))
    _b7.append(("no-token", depth, r_no.status_code))
    chk("B7 depth %d + 无 token → 403（改前 RecursionError 打出 500；silent=True 挡不住）" % depth,
        r_no.status_code == 403, _b7[-1])
    r_tok = client.post(URL, headers=HDR, data=_deep_body(depth), content_type="application/json")
    _b7.append(("token", depth, r_tok.status_code))
    chk("B7 depth %d + 带 token → 400（请求体不可用，不是 500）" % depth,
        r_tok.status_code == 400, _b7[-1])
chk("B7 深嵌套请求的 500 计数 = 0", sum(1 for _ in _b7 if _[2] == 500) == 0, _b7)

print("\n-- B5⑥/B6b start_printing 父订单守卫（行为）：三终态不许复活 waiting 单；failed 放行 --")
# 这条行为断言必须构造「父订单=终态 **且** 子任务=waiting/downloading」——那正是 start_printing
# 的子任务闸门 `status IN ('waiting','downloading')` 会放行、而父订单守卫要拦下的状态。
# 只走 API 订正是不够判别的：订正会把子任务一并写成终态，子任务闸门自己也会挡下，
# 改前（守卫只认 canceled）与改后结果相同 = 不判别。因此订正后用 _set_child_status 把子任务
# 放回 waiting（模拟「设备侧仍在等 / 另一条路径把子任务放回等待」），此时：
#   改前（只认 canceled）→ abandoned/rejected 不跳过 → 子任务被写成 printing（断言红）；
#   改后（三终态常量）→ 整单跳过 → 子任务仍 waiting、父订单不变、print_started_at 仍为空。


def _set_child_status(tid, st):
    conn = backend.get_db()
    conn.execute("UPDATE order_files SET status = ? WHERE id = ?", (st, tid))
    conn.commit()
    conn.close()


def _print_started_at(oid):
    conn = backend.get_db()
    row = conn.execute("SELECT print_started_at FROM orders WHERE id = ?", (oid,)).fetchone()
    conn.close()
    return (row["print_started_at"] or "") if row else ""


for _st in ("abandoned", "rejected", "canceled"):
    oid = mk_order("waiting", [("waiting", 2.30, "")], number="HN-B56-%s" % _st)
    _set_owner(oid, operator=DEV_A, last=DEV_A)
    tid = _sub_ids(oid)[0]
    post({"order_id": oid, "status": _st})          # 父订单=waiting、子任务=waiting → 订正为终态
    _set_child_status(tid, "waiting")               # 还原设备侧状态（见上方说明）
    _parent_before = db_state(oid)[0]
    SOCK_A.get_received()
    SOCK_A.emit("start_printing", {"task_ids": [tid]})
    time.sleep(0.05)
    SOCK_A.get_received()
    _o_now, _f_now, _agg = db_state(oid)
    chk("B5⑥ 父订单 %s + 子任务 waiting：start_printing 不得复活（子任务不许被写成 printing）"
        "—— 改前只认 canceled：abandoned/rejected 会复活成 printing" % _st,
        _f_now[0][0] == "waiting", _f_now)
    chk("B5⑥ 父订单 %s：终态不被改写、print_started_at 仍为空（守卫命中即整单跳过，不算开始打印）"
        % _st,
        _o_now == _st and _parent_before == _st and _print_started_at(oid) == "",
        (_o_now, _parent_before, _print_started_at(oid)))

# 对照组：failed 不在三终态常量里（纸可能已经出来了，仍可被救回/继续打完）→ start_printing 放行
oid = mk_order("waiting", [("waiting", 2.30, "")], number="HN-B56-failed")
_set_owner(oid, operator=DEV_A, last=DEV_A)
tid = _sub_ids(oid)[0]
post({"order_id": oid, "status": "failed"})
_set_child_status(tid, "waiting")
SOCK_A.get_received()
SOCK_A.emit("start_printing", {"task_ids": [tid]})
time.sleep(0.05)
SOCK_A.get_received()
chk("B6b 对照组：父订单 failed + 子任务 waiting → start_printing 允许开始（写成 printing；"
    "证明这道闸门不是把所有状态都拦死）",
    db_state(oid)[1][0][0] == "printing", db_state(oid))

# 判据自证（**不碰源码**，只在本次进程里把常量退回改前口径）：改前守卫是
# `if o and o["status"] == "canceled":`，等价于常量只剩 ("canceled",)。退回后同款场景必须
# **真的复活**成 printing —— 否则 B5⑥ 的「不许复活」就是空转（永远为绿的死判据）。
# ⚠ getattr 兜底：改前版本（反向验证基线 0ea71f3）没有这个常量，直接取会 AttributeError
# 把整个脚本崩在异常上（不是红在断言）。缺常量时 _mut_st 留 None ⇒ 下面 chk 判红。
_orig_nlp = getattr(backend, "NO_LONGER_PRINTABLE_STATUSES", None)
_mut_st = None
try:
    if _orig_nlp is not None:
        backend.NO_LONGER_PRINTABLE_STATUSES = ("canceled",)
        oid_m = mk_order("waiting", [("waiting", 2.30, "")], number="HN-B56-mut")
        _set_owner(oid_m, operator=DEV_A, last=DEV_A)
        tid_m = _sub_ids(oid_m)[0]
        post({"order_id": oid_m, "status": "abandoned"})
        _set_child_status(tid_m, "waiting")
        SOCK_A.get_received()
        SOCK_A.emit("start_printing", {"task_ids": [tid_m]})
        time.sleep(0.05)
        SOCK_A.get_received()
        _mut_st = db_state(oid_m)[1][0][0]
finally:
    if _orig_nlp is not None:
        backend.NO_LONGER_PRINTABLE_STATUSES = _orig_nlp
chk("[自证] 把守卫退回改前口径（常量只剩 canceled）→ 同款 abandoned 场景**真的复活**成 printing"
    "（证明 B5⑥ 的「不许复活」有判别力）",
    _mut_st == "printing", _mut_st)

print("\n-- B8 订单**类型**订正（接单 / 自打，v7.1）--")


def admin_flag(oid):
    conn = backend.get_db()
    row = conn.execute("SELECT is_admin_print FROM orders WHERE id = ?", (oid,)).fetchone()
    conn.close()
    return int(row["is_admin_print"] or 0) if row else None


TYPE_URL = "/api/admin/order_type"


def post_type(payload, headers=HDR):
    return client.post(TYPE_URL, headers=headers, json=payload)


oid_t = mk_order("sent", [("sent", 4.60, "")], number="HN-TYPE-1", is_admin_print=0)
_before_t = db_state(oid_t)
r = post_type({"order_id": oid_t, "order_type": "self"}, headers={})
chk("类型订正：无 token → 403 且库未动",
    r.status_code == 403 and admin_flag(oid_t) == 0 and db_state(oid_t) == _before_t,
    "%s %s" % (r.status_code, r.get_json()))
r = post_type({"order_id": oid_t, "order_type": "self"}, headers={"X-Printer-Token": "wrong"})
chk("类型订正：错误 token → 403", r.status_code == 403)
r = post_type({"order_id": oid_t})
chk("类型订正：缺 order_type → 400", r.status_code == 400)
for badt in ("auto", "scheduled", "free", "delivery", "urgent", "cover", "SELF", "", None):
    rr = post_type({"order_id": oid_t, "order_type": badt})
    if rr.status_code != 400:
        break
chk("类型订正：只允许 receive/self（自动/预约/免费/派送/加急/封面/大小写变体一律 400）",
    rr.status_code == 400, "%s %s" % (rr.status_code, rr.get_json()))
chk("被拦下的类型请求没有改动 is_admin_print", admin_flag(oid_t) == 0, admin_flag(oid_t))
r = post_type({"order_id": 99999999, "order_type": "self"})
chk("类型订正：订单不存在 → 404", r.status_code == 404, r.get_json())
for aff in ("1.0", "01", "+1", " 1 ", "1e0", True, 1.0, ["1"], {"id": 1}, None, ""):
    rr = post_type({"order_id": aff, "order_type": "self"})
    if rr.status_code != 400:
        break
chk("类型订正：order_id 亲和性/类型变体一律 400（绝不改错单）",
    rr.status_code == 400, "%s %s" % (rr.status_code, rr.get_json()))
chk("上述畸形类型请求一条都没改动数据库", db_state(oid_t) == _before_t, db_state(oid_t))
for raw in (b'["self"]', b'"self"', b'123', b'true', b''):
    rr = client.post(TYPE_URL, headers=HDR, data=raw, content_type="application/json")
    if rr.status_code != 400:
        break
chk("类型订正：带 token + 非对象请求体 → 400（不是 500）", rr.status_code == 400,
    "%s %s" % (rr.status_code, rr.get_json()))
rr = client.post(TYPE_URL, headers={}, data=b'["self"]', content_type="application/json")
chk("类型订正：未认证 + 畸形请求体 → 403（不是 500）", rr.status_code == 403,
    "%s %s" % (rr.status_code, rr.get_json()))

r = post_type({"order_id": oid_t, "order_type": "self"})
j = r.get_json() or {}
chk("改为自打：200 + is_admin_print=1 + 回执 order_type/previous_order_type",
    r.status_code == 200 and j.get("success") and j.get("order_type") == "self"
    and j.get("previous_order_type") == "receive" and j.get("is_admin_print") == 1
    and admin_flag(oid_t) == 1, j)
chk("类型订正**不动**状态 / 子任务状态 / locked_at（与状态订正是两条独立链路）",
    db_state(oid_t) == _before_t, db_state(oid_t))
o_self = revenue_of(oid_t)
chk("改自打后：订单明细里该单收益归零（自打不计入收益——这正是类型影响结算的口径）",
    o_self is not None and round(o_self["revenue"] + o_self["receivable"], 2) == 0.0,
    o_self and (o_self["revenue"], o_self["receivable"]))
r = post_type({"order_id": oid_t, "order_type": "receive"})
j = r.get_json() or {}
o_back = revenue_of(oid_t) or {}
chk("改回接单：is_admin_print=0，回执 previous=self，收益恢复 4.60",
    r.status_code == 200 and j.get("previous_order_type") == "self" and admin_flag(oid_t) == 0
    and round(o_back.get("revenue", 0) + o_back.get("receivable", 0), 2) == 4.60, j)
chk("幂等：重复提交同一类型仍 200 且值不变",
    post_type({"order_id": oid_t, "order_type": "receive"}).status_code == 200
    and admin_flag(oid_t) == 0)

print("\n" + "=" * 52)
if bad:
    print("订单状态订正验收：存在失败 ❌ (%d)" % bad)
else:
    print("订单状态订正验收：全部通过 ✅")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if bad else 0)
