#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收：「最近 N 条订单」两张接口（task-1）。

覆盖（逐条对应任务契约）：
  ① 默认 N = 10（超管没设置过 ⇒ DEFAULT_RECENT_ORDERS_LIMIT）；
  ② 超管改 N 立即生效 + 落盘 uploads/recent_orders_config.json + 重新 load 仍生效（重启不丢）；
  ③ 范围校验 1..100：0 / 101 / 非整数 / true 一律 400，且不污染已保存值；
  ④ admin 可读不可写：GET 200，POST 403（超管被 compute_role 折叠成 admin，
     所以"仅超管"必须靠 SUPER_ADMIN_OPENID 判据，不能靠 role 字符串）；
  ⑤ 非管理员两个接口都 403（不是 200+success:false —— 这是新接口，按任务要求给 403）；
  ⑥ 条数与排序：跨用户全站最近 N 条，ORDER BY created_at DESC, id DESC（同刻按 id 倒序）；
  ⑦ owner / owner_masked：云端优先昵称，本机单用盖章成员名，脱敏走 _mask_person_name「姚**」；
  ⑧ 金额 / 页数口径：total_price 只读库值不重算；page_count 走 _file_range_stats
     （内部只认 _count_pages_in_range，含份数），sheet_count 是双面折算后的纸张数。

用法（Windows / GBK 控制台必须带 -X utf8）：
  PYTHONUTF8=1 PYTHONPATH= python -X utf8 tests/verify_recent_orders_api.py
  PYTHONUTF8=1 PYTHONPATH= python -X utf8 tests/verify_recent_orders_api.py --mutate auth
      ↑ 故意在临时副本里改坏一处（不碰仓库），用来证明本脚本真能红。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import traceback

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

# 变异自证：只在临时副本上改，仓库源码不动
MUTATIONS = {
    "sort": ('ORDER BY created_at DESC, id DESC LIMIT ?',
             'ORDER BY id DESC LIMIT ?'),
    "auth": ('        if not is_super:', '        if False:'),
    "pages": ('page_count += st["total_pages"]', 'page_count += st["effective_pages"]'),
    "owner": ('return owner_name or "本机打印"', 'return "本机打印"'),
}
MUTATE = ""
if "--mutate" in sys.argv:
    i = sys.argv.index("--mutate")
    MUTATE = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
    if MUTATE and MUTATE not in MUTATIONS:
        sys.exit(f"未知变异：{MUTATE}（可选 {sorted(MUTATIONS)}）")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title):
    print(f"\n=== {title} ===")


tmp = tempfile.mkdtemp(prefix="hn_recent_orders_")
dst = os.path.join(tmp, "printer-backend")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs", "venv",
    "config.py", "recent_orders_config.json", "stack_intent.json",
    "device_pin_order.json", "printer_claim.json", "devices.json"))

if MUTATE:
    app_path = os.path.join(dst, "app.py")
    old, new = MUTATIONS[MUTATE]
    with open(app_path, "r", encoding="utf-8") as f:
        src = f.read()
    if old not in src:
        sys.exit(f"[mutate] 锚点消失，请更新 MUTATIONS['{MUTATE}'] 的锚点字符串")
    with open(app_path, "w", encoding="utf-8") as f:
        f.write(src.replace(old, new, 1))
    print(f"[mutate] 已把 app.py 改坏一处（{MUTATE}），期望本脚本转红")

sys.path.insert(0, dst)
os.chdir(dst)
os.environ["FLASK_ENV"] = "development"   # 没有 config.py 时 app.py 需要它才肯用开发默认 SECRET_KEY
try:
    import app as backend
    print(f"imported app.py from {dst}")
except Exception:
    traceback.print_exc()
    sys.exit("无法导入 app.py")

backend.init_db()

# ==================== 桩环境 ====================
SUPER_OID = "openid-super"
ADMIN_OID = "openid-admin"
USER_OID = "openid-user"
ROLES = {SUPER_OID: "admin", ADMIN_OID: "admin", USER_OID: "user"}
backend.SUPER_ADMIN_OPENID = SUPER_OID
backend.compute_role = lambda oid: ROLES.get(oid, "guest")

TOK = {oid: backend.token_serializer.dumps(oid) for oid in ROLES}
client = backend.app.test_client()


def get(path, who):
    r = client.get(path, headers={"Authorization": f"Bearer {TOK[who]}"})
    return r, (r.get_json(silent=True) or {})


def post(path, body, who):
    r = client.post(path, json=body, headers={"Authorization": f"Bearer {TOK[who]}"})
    return r, (r.get_json(silent=True) or {})


def cfg_path():
    return os.path.join(os.getcwd(), backend.RECENT_ORDERS_CONFIG_FILE)


def read_cfg():
    try:
        with open(cfg_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ==================== 造数据：12 单跨 3 个用户 ====================
# 时间：id 11/12 同为 2026-10-11（验证同刻按 id 倒序），其余按日递增 ⇒ 期望顺序 12,11,10,…,3
conn = backend.get_db()
IDS = {}
try:
    user_conn = backend.get_user_db()
    try:
        for nick_oid, nick in [("wx_yao", "姚懿祥")]:
            user_conn.execute("INSERT OR REPLACE INTO users (openid, nickname, updated_at) "
                              "VALUES (?, ?, ?)", (nick_oid, nick, "2026-10-11 09:00:00"))
        user_conn.commit()
    finally:
        user_conn.close()

    def add_order(i, openid, source="wechat", owner_name="", page_count=1,
                  copies=1, duplex="off", total_price=0.0, status="printing",
                  is_admin_print=0, created_at=None):
        created_at = created_at or (f"2026-10-{min(i, 10):02d} 10:00:00"
                                    if i <= 10 else "2026-10-11 10:00:00")
        cur = conn.execute(
            "INSERT INTO orders (file, copies, status, created_at, openid, duplex, "
            "page_count, total_price, order_number, source, owner_name, is_admin_print) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("测试.pdf", copies, status, created_at, openid, duplex, page_count,
             total_price, f"NO2026100{i:02d}", source, owner_name, is_admin_print))
        return cur.lastrowid

    def add_file(order_id, page_count, copies, duplex, page_range="", status="sent",
                 total_price=0.0):
        conn.execute(
            "INSERT INTO order_files (order_id, file_id, file_name, copies, page_count, "
            "duplex, page_range, status, created_at, total_price) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (order_id, f"fid-{order_id}-{page_count}", "测试.pdf", copies, page_count,
             duplex, page_range, status, "2026-10-11 10:00:00", total_price))

    for i in range(1, 13):
        IDS[i] = add_order(i, f"wx_u{i}")

    # 12：云端单 + 昵称 + 两个子文件（验证页数/张数/金额/状态聚合）
    conn.execute("UPDATE orders SET openid=?, owner_name=?, total_price=?, status=?, "
                 "is_admin_print=1 WHERE id=?",
                 ("wx_yao", "", 7.5, "printing", IDS[12]))
    add_file(IDS[12], 5, 2, "on", "", "sent")      # 有效 5 页 × 2 份 = 10 页，3 张 × 2 = 6 张
    add_file(IDS[12], 3, 1, "off", "1-2", "failed")  # 有效 2 页 = 2 页，2 张（状态聚合 → failed）
    # 11：云端单 + 无昵称 → 退回 orders.owner_name；无子任务 → 走订单字段兜底
    conn.execute("UPDATE orders SET openid=?, owner_name=?, total_price=?, page_count=?, "
                 "copies=?, duplex=?, status=? WHERE id=?",
                 ("wx_li", "李四", 1.25, 3, 2, "on", "sent", IDS[11]))
    # 10：本机打印单（openid='local'）→ owner 用盖章成员名
    conn.execute("UPDATE orders SET openid='local', source='local', owner_name=?, "
                 "page_count=?, copies=?, duplex=?, total_price=? WHERE id=?",
                 ("王五", 4, 1, "off", 0.0, IDS[10]))
    # 3：云端单、无昵称、无 owner_name → "用户" + openid 末四位
    conn.execute("UPDATE orders SET openid=? WHERE id=?", ("wx_tail9527", IDS[3]))
    conn.commit()
finally:
    conn.close()


def ids_of(payload):
    return [o["order_id"] for o in (payload.get("orders") or [])]


def by_id(payload, oid):
    for o in payload.get("orders") or []:
        if o["order_id"] == oid:
            return o
    return {}


# ==================== ① 默认 10 条 + 排序 ====================
section("① 默认 N=10 / 全站最近 / 排序")
r, j = get("/api/admin/recent_orders", ADMIN_OID)
check("admin GET recent_orders → 200", r.status_code == 200, f"status={r.status_code}")
check("默认 limit 字段 = 10", j.get("limit") == 10, f"limit={j.get('limit')}")
check("返回条数 = 10（默认 N，不翻页）", len(j.get("orders") or []) == 10,
      f"len={len(j.get('orders') or [])}")
check("按 created_at DESC, id DESC 排序（同刻按 id 倒序）",
      ids_of(j) == [IDS[12], IDS[11], IDS[10], IDS[9], IDS[8],
                    IDS[7], IDS[6], IDS[5], IDS[4], IDS[3]],
      f"ids={ids_of(j)}")
check("最新单在最前（order_id 最大者）", ids_of(j)[0] == IDS[12])

# ==================== ② 超管改值 ====================
section("② 超管改 N：立即生效 + 落盘 + 重启仍在")
r, j = get("/api/admin/recent_orders_limit", ADMIN_OID)
check("admin GET limit → 200 / limit=10 / range=[1,100]",
      r.status_code == 200 and j.get("limit") == 10 and j.get("range") == [1, 100],
      f"status={r.status_code} body={j}")

r, j = post("/api/admin/recent_orders_limit", {"limit": 3}, SUPER_OID)
check("超管 POST limit=3 → 200 success", r.status_code == 200 and j.get("success") is True,
      f"status={r.status_code} body={j}")
check("落盘 recent_orders_config.json = {limit:3}", read_cfg() == {"limit": 3},
      f"file={read_cfg()}")
r, j = get("/api/admin/recent_orders_limit", ADMIN_OID)
check("改完 GET 立即变成 3（内存副本即时生效）", j.get("limit") == 3, f"body={j}")
r, j = get("/api/admin/recent_orders", ADMIN_OID)
check("recent_orders 立刻只给 3 条", j.get("limit") == 3 and len(j.get("orders") or []) == 3,
      f"limit={j.get('limit')} len={len(j.get('orders') or [])}")
# 重启路径：清内存 → 重新 load（等价于进程重启时的那次载入）
backend._RECENT_ORDERS_LIMIT = backend.DEFAULT_RECENT_ORDERS_LIMIT
backend.load_recent_orders_config()
check("重启（重新 load）后仍是 3", backend._RECENT_ORDERS_LIMIT == 3,
      f"_RECENT_ORDERS_LIMIT={backend._RECENT_ORDERS_LIMIT}")

# ==================== ③ 范围校验 ====================
section("③ 范围校验 1..100（400 且不污染已存值）")
for bad, label in [(0, "0"), (101, "101"), (-5, "-5")]:
    r, j = post("/api/admin/recent_orders_limit", {"limit": bad}, SUPER_OID)
    check(f"越界 {label} → 400", r.status_code == 400, f"status={r.status_code} body={j}")
for bad, label in [("abc", "字符串"), (True, "布尔 true"), (None, "null")]:
    r, j = post("/api/admin/recent_orders_limit", {"limit": bad}, SUPER_OID)
    check(f"非法值 {label} → 400", r.status_code == 400,
          f"status={r.status_code} body={j}")
check("非法请求没有覆盖已保存值（仍 3）", read_cfg() == {"limit": 3}, f"file={read_cfg()}")
r, j = post("/api/admin/recent_orders_limit", {"limit": 100}, SUPER_OID)
check("边界 100 允许", r.status_code == 200 and j.get("limit") == 100, f"body={j}")
r, j = post("/api/admin/recent_orders_limit", {"limit": 1}, SUPER_OID)
check("边界 1 允许", r.status_code == 200 and j.get("limit") == 1, f"body={j}")
post("/api/admin/recent_orders_limit", {"limit": 3}, SUPER_OID)

# ==================== ④/⑤ 权限 ====================
section("④ admin 可读不可写 / ⑤ 非管理员 403")
r, j = get("/api/admin/recent_orders", ADMIN_OID)
check("admin 可读 recent_orders（200）", r.status_code == 200, f"status={r.status_code}")
r, j = post("/api/admin/recent_orders_limit", {"limit": 7}, ADMIN_OID)
check("admin POST limit → 403（仅超管）", r.status_code == 403,
      f"status={r.status_code} body={j}")
check("admin 被拒后设置没变（仍 3）", read_cfg() == {"limit": 3}, f"file={read_cfg()}")
r, j = get("/api/admin/recent_orders", USER_OID)
check("普通用户 GET recent_orders → 403", r.status_code == 403, f"status={r.status_code}")
r, j = get("/api/admin/recent_orders_limit", USER_OID)
check("普通用户 GET limit → 403", r.status_code == 403, f"status={r.status_code}")
r, j = post("/api/admin/recent_orders_limit", {"limit": 7}, USER_OID)
check("普通用户 POST limit → 403", r.status_code == 403, f"status={r.status_code}")
r = client.get("/api/admin/recent_orders")
check("未登录 GET recent_orders → 401", r.status_code == 401, f"status={r.status_code}")

# ==================== ⑥ ?limit= 覆盖 ====================
section("⑥ ?limit= 临时覆盖（夹在 1..100，不改保存值）")
r, j = get("/api/admin/recent_orders?limit=2", ADMIN_OID)
check("?limit=2 → 2 条 / limit 字段 2",
      j.get("limit") == 2 and len(j.get("orders") or []) == 2, f"body_limit={j.get('limit')}")
check("?limit=2 拿到的是最新的两条", ids_of(j) == [IDS[12], IDS[11]], f"ids={ids_of(j)}")
r, j = get("/api/admin/recent_orders?limit=999", ADMIN_OID)
check("?limit=999 夹到 100，返回全部 12 条",
      j.get("limit") == 100 and len(j.get("orders") or []) == 12,
      f"limit={j.get('limit')} len={len(j.get('orders') or [])}")
r, j = get("/api/admin/recent_orders?limit=0", ADMIN_OID)
check("?limit=0 夹到 1，返回 1 条",
      j.get("limit") == 1 and len(j.get("orders") or []) == 1, f"limit={j.get('limit')}")
r, j = get("/api/admin/recent_orders?limit=abc", ADMIN_OID)
check("?limit=abc → 400", r.status_code == 400, f"status={r.status_code} body={j}")
check("?limit= 不改保存值（仍 3）", read_cfg() == {"limit": 3}, f"file={read_cfg()}")

# ==================== ⑦ owner / owner_masked ====================
section("⑦ owner 解析 + S2-04 脱敏")
r, j = get("/api/admin/recent_orders?limit=100", ADMIN_OID)
o12, o11, o10 = by_id(j, IDS[12]), by_id(j, IDS[11]), by_id(j, IDS[10])
o3 = by_id(j, IDS[3])
check("云端单有昵称 → owner=昵称", o12.get("owner") == "姚懿祥", f"owner={o12.get('owner')}")
check("脱敏「姚**」同款", o12.get("owner_masked") == "姚**", f"masked={o12.get('owner_masked')}")
check("云端单无昵称 → 退回 orders.owner_name", o11.get("owner") == "李四",
      f"owner={o11.get('owner')}")
check("脱敏「李*」", o11.get("owner_masked") == "李*", f"masked={o11.get('owner_masked')}")
check("本机单（openid=local）→ 盖章成员名", o10.get("owner") == "王五",
      f"owner={o10.get('owner')}")
check("本机单脱敏「王*」", o10.get("owner_masked") == "王*",
      f"masked={o10.get('owner_masked')}")
check("云端单无昵称无 owner_name → 用户+末四位",
      o3.get("owner") == "用户9527", f"owner={o3.get('owner')}")
check("每个订单都带 owner + owner_masked",
      all(o.get("owner") and o.get("owner_masked") for o in (j.get("orders") or [])))

# ==================== ⑧ 金额 / 页数口径 ====================
section("⑧ 金额只读库值 / 页数走 _count_pages_in_range 有效口径")
check("12 号：page_count=10+2=12（有效页数×份数）", o12.get("page_count") == 12,
      f"page_count={o12.get('page_count')}")
check("12 号：sheet_count=6+2=8（双面折算纸张数）", o12.get("sheet_count") == 8,
      f"sheet_count={o12.get('sheet_count')}")
check("12 号：file_count=2（两个子文件）", o12.get("file_count") == 2,
      f"file_count={o12.get('file_count')}")
check("12 号：total_price=7.5（读库，不按 0.25/页 重算）",
      o12.get("total_price") == 7.5, f"total_price={o12.get('total_price')}")
check("12 号：status 走子任务聚合（sent+failed → failed，而非 orders.status=printing）",
      o12.get("status") == "failed", f"status={o12.get('status')}")
check("12 号：is_admin_print=true / source=wechat",
      o12.get("is_admin_print") is True and o12.get("source") == "wechat",
      f"is_admin_print={o12.get('is_admin_print')} source={o12.get('source')}")
check("11 号（无子任务兜底）：page_count=3×2=6", o11.get("page_count") == 6,
      f"page_count={o11.get('page_count')}")
check("11 号（无子任务兜底）：sheet_count=2×2=4", o11.get("sheet_count") == 4,
      f"sheet_count={o11.get('sheet_count')}")
check("11 号：file_count 兜底为 1", o11.get("file_count") == 1,
      f"file_count={o11.get('file_count')}")
check("10 号本机单：page_count=4 / total_price=0.0 / source=local",
      o10.get("page_count") == 4 and o10.get("total_price") == 0.0
      and o10.get("source") == "local",
      f"page_count={o10.get('page_count')} price={o10.get('total_price')} "
      f"source={o10.get('source')}")
check("字段齐全（11 个契约字段一个不少）",
      all(k in o12 for k in ("order_id", "order_number", "owner", "owner_masked",
                             "created_at", "file_count", "page_count", "total_price",
                             "status", "source", "is_admin_print")),
      f"keys={sorted(o12)}")
check("order_number 原样透出", o12.get("order_number") == "NO202610012", 
      f"order_number={o12.get('order_number')}")

# ==================== 汇总 ====================
print()
if MUTATE:
    print(f"[mutate={MUTATE}] 失败 {RESULTS.count(False)} / 共 {len(RESULTS)} 条")
    print("（自证模式：只要出现 FAIL 就说明能红；本次退出码恒为 0，不污染套件）")
    sys.exit(0)
if all(RESULTS):
    print(f"全部通过：{len(RESULTS)} 条检查")
    sys.exit(0)
print(f"存在失败：{RESULTS.count(False)} / {len(RESULTS)} 条")
sys.exit(1)
