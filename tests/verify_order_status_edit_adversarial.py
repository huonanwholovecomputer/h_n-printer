# -*- coding: utf-8 -*-
"""对抗式验证 · 收支清算「订单状态订正」(v7.0)

目标：**证伪** app.py:/api/admin/order_status 与 settlement.html 的订单状态订正
在安全性/一致性上的说法，而不是复述实现说明。

与 tests/verify_order_status_edit.py 的差异（刻意不同源，避免同一条思路自证）：
  · 夹具：raw sqlite3 直连 backend.DATABASE 手写 INSERT（不是 backend.get_db()），
    订单形态更刁钻（5 文件各不相同终态 / 混合终态 / 全 sent / 无子任务 / 非终态混排），
    并且每次订正都带一张 **canary 订单**验证 WHERE 作用域没有溢出；
  · 判据：不是「聚合函数返回值 == 目标」而是「调 refresh_order_status 回写后父订单仍是目标」
    （真正的“被刷回去”路径）；非法请求一律比对**全库逻辑指纹**（所有表所有行）+ 原始文件哈希；
  · 额外攻击面（原验收未覆盖）：
      ① 白名单绕过：40+ 种畸形 status（前后空白/大小写/SQL/非字符串/全角/西里尔/空字节）
         并要求「一个字节都不能变」+ 不得 500；
      ② 鉴权传法矩阵：header / query / body 三路 + 冲突组合 + 空 header 回退；
      ③ 事务性：SQLite 触发器注入「子任务已改、父订单 UPDATE 抛错」→ 必须整体回滚；
         4 线程并发两目标 → 终态必须自洽；
      ④ 404 早返回是否遗留写锁（另开连接 BEGIN IMMEDIATE 验证）；
      ⑤ 非 dict 的 JSON 请求体（["sent"] / "sent" / 123 / true / null / 坏 JSON）→
         未认证只能 400/403/404 绝不 500；带 token 一律 400（不是 500）；
      ⑥ 口径漂移：静态比对 + 用 _STATUS_PRIORITY 全量动态探测；
      ⑦ 金额/结算：sent 计入、failed/abandoned/canceled/rejected 退出收益（含 summary）；
      ⑧ 前端锁定守卫：源码判据（同源守卫 _orderStatusEditable 先于白名单校验与 fetch、
         守卫分支是 toast+return、锁定态不渲染箭头、local 模式无入口）。
  · 判据自证（变异）：⑤ 与 ⑧ 都带自证 —— 把鉴权表达式退回旧版（直接 .get()）、把守卫删掉/
    移到 fetch 之后/退化成只判 unlocked/去掉 return、或去掉 _orderStatusEditable 里的
    FINANCE_MODE 判据，对应判据必须**立刻转红**（证明判据不是空转）。

用法: cd /d/打印机项目 && PYTHONPATH= PYTHONUTF8=1 \
        "C:/Users/Administrator/AppData/Local/Programs/Python/Python311/python.exe" \
        tests/verify_order_status_edit_adversarial.py
退出码 0 = 未发现可证伪问题；发现 FAIL → 1（漏洞如实报出）。
"""
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import traceback

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(REPO, "local_print_tool", "finance", "settlement.html")
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

bad = 0
findings = []


def chk(name, cond, detail=""):
    global bad
    if not cond:
        bad += 1
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + str(detail)) if (not cond and detail != "") else ""))


def note(msg):
    print("      · " + msg)


def sec(t):
    print("\n" + "=" * 70 + "\n" + t + "\n" + "=" * 70)


# ============================================================================
sec("0. 夹具准备（复制后端到临时目录，raw sqlite3 直连建单）")

TMP = tempfile.mkdtemp(prefix="hn_statusadv_")
DST = os.path.join(TMP, "printer-backend")
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(
    "__pycache__", "config.py", "config.py.example",
    "orders.db", "users.db", "uploads", "logs", "*.db-wal", "*.db-shm"))
os.chdir(DST)
os.environ["FLASK_ENV"] = "development"
sys.path.insert(0, DST)

import app as backend  # noqa: E402

backend.init_db()
ADV_TOKEN = "adv-token-7f3c9e-UNGUESSABLE"
backend.PRINTER_TOKEN = ADV_TOKEN
client = backend.app.test_client()
URL = "/api/admin/order_status"
HDR = {"X-Printer-Token": ADV_TOKEN}
print("backend = %s" % backend.__file__)
print("DATABASE = %s" % os.path.abspath(backend.DATABASE))
print("PRINTER_TOKEN = %r（自设值，非原验收的 test-token）" % backend.PRINTER_TOKEN)

raw_conn = sqlite3.connect(backend.DATABASE)
raw_conn.row_factory = sqlite3.Row


def raw():
    c = sqlite3.connect(backend.DATABASE)
    c.row_factory = sqlite3.Row
    return c


def mk(tag, parent_status, files, total_price=None, created_at="2026-10-18 08:00:00",
       is_admin_print=0, source="wechat", openid="openid-adv"):
    """files = [(status, price, locked_at_or_None), ...]；返回 order_id"""
    c = raw()
    tp = total_price if total_price is not None else round(sum(f[1] for f in files), 2)
    cur = c.execute(
        "INSERT INTO orders (file, copies, status, created_at, openid, total_price,"
        " order_number, source, is_admin_print) VALUES (?,?,?,?,?,?,?,?,?)",
        ("adv.pdf", 1, parent_status, created_at, openid, tp, "ADV-" + tag, source, is_admin_print))
    oid = cur.lastrowid
    for i, (st, price, locked) in enumerate(files):
        c.execute(
            "INSERT INTO order_files (order_id, file_name, copies, page_count, total_price,"
            " status, created_at, page_range, locked_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (oid, "adv%d.pdf" % i, 1, 2, price, st, created_at, "", locked))
    c.commit()
    c.close()
    return oid


def state(oid):
    """(父订单状态, [(子状态, locked_at), ...], 聚合状态)"""
    c = backend.get_db_conn()
    try:
        o = c.execute("SELECT status FROM orders WHERE id = ?", (oid,)).fetchone()
        f = c.execute("SELECT status, locked_at FROM order_files WHERE order_id = ? ORDER BY id",
                      (oid,)).fetchall()
        agg = backend.aggregate_order_status(c, oid)
        return (o["status"] if o else None,
                [(r["status"], r["locked_at"]) for r in f], agg)
    finally:
        c.close()


def refresh_parent(oid):
    """真实回写路径：refresh_order_status 后父订单是否仍等于目标（“被刷回去”的判定）"""
    c = backend.get_db_conn()
    try:
        backend.refresh_order_status(c, oid)
        c.commit()
        return c.execute("SELECT status FROM orders WHERE id = ?", (oid,)).fetchone()["status"]
    finally:
        c.close()


def db_fp():
    """全库逻辑指纹：所有表 + 列 + 所有行"""
    c = sqlite3.connect(backend.DATABASE)
    try:
        buf = []
        tabs = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for t in tabs:
            cols = [r[1] for r in c.execute("PRAGMA table_info(%s)" % t)]
            buf.append("%s(%s)" % (t, ",".join(cols)))
            try:
                rows = c.execute("SELECT * FROM %s ORDER BY rowid" % t).fetchall()
            except sqlite3.OperationalError:
                rows = c.execute("SELECT * FROM %s" % t).fetchall()
            for r in rows:
                buf.append(repr(tuple(r)))
        return hashlib.sha256("\n".join(buf).encode("utf-8")).hexdigest()
    finally:
        c.close()


def raw_fp():
    parts = []
    for name in ("orders.db", "orders.db-wal", "orders.db-shm"):
        p = os.path.join(os.getcwd(), name)
        if os.path.exists(p):
            with open(p, "rb") as f:
                parts.append("%s:%s" % (name, hashlib.sha256(f.read()).hexdigest()))
    return "|".join(parts)


def post(payload, headers=HDR, **kw):
    return client.post(URL, headers=headers, json=payload, **kw)


# 原始文件哈希在「无写入」时是否稳定？（WAL checkpoint 可能扰动 → 先探一次）
# F8a（2026-10-18 审查）：原先这里是裸 `assert r.status_code == 403` —— 一旦修前版
# （或任何回归）把它打成非 403，脚本直接抛 AssertionError 终止，**一个 chk 都不跑、
# 没有 FAIL 摘要**，红在异常而不是修复点。改成 chk 后继续跑完整套。
_r0 = raw_fp()
_r403_bad = []
for _ in range(3):
    r = post({"order_id": 987654321, "status": "sent"}, headers={})   # 403，绝无写入
    if r.status_code != 403:
        _r403_bad.append("%s %s" % (r.status_code, r.get_json()))
_r1 = raw_fp()
chk("无 token 的订正请求一律 403（原始文件哈希探针的前提；裸 assert → chk 后仍继续跑）",
    not _r403_bad, _r403_bad)
STRICT_RAW = (_r0 == _r1)
note("原始文件(order s.db/-wal)哈希在无写入请求前后%s → 本轮%s原始字节断言"
     % ("一致" if STRICT_RAW else "不一致", "启用" if STRICT_RAW else "仅作参考不判定"))

CANARY = mk("canary", "printing", [("printing", 2.30, "2026-10-18 08:00:00")],
            created_at="2026-10-18 07:00:00")
CANARY_BEFORE = state(CANARY)
note("canary 订单 #%d = %s（每次订正后校验它没被捎带改到）" % (CANARY, CANARY_BEFORE))

# F8a：白名单属性缺失时用 getattr 兜底继续跑（原先直接 `backend.MANUAL_FIX_STATUSES`
# 会 AttributeError 终止 → 红在异常而非断言）。兜底成空元组本身必须判红，否则后面的
# 矩阵会「0 组合 0 失败」空转成假绿。
FIX_TARGETS = tuple(getattr(backend, "MANUAL_FIX_STATUSES", ()) or ())
print("\n(夹具矩阵) 目标白名单 MANUAL_FIX_STATUSES = %s" % (FIX_TARGETS,))
chk("后端暴露终态白名单 MANUAL_FIX_STATUSES（缺了的话下面的 5×5 矩阵会空转）",
    len(FIX_TARGETS) == 5, FIX_TARGETS)


# ============================================================================
sec("1. 父子一致性（5 目标 × 5 形态，含混合终态/全 sent/无子任务/非终态）")

# 形态：tag → (父状态, [(子状态, 价格, locked_at), ...])
SHAPES = {
    "混合终态 sent+abandoned": ("sent", [("sent", 2.30, ""), ("abandoned", 2.30, "2026-10-18 08:00:00")]),
    "全部子任务已 sent": ("sent", [("sent", 2.30, "2026-10-18 08:00:00"), ("sent", 2.30, "2026-10-18 08:00:00")]),
    "无子任务历史老单": ("abandoned", []),
    "5 文件各不相同终态": ("failed", [("sent", 1.0, ""), ("failed", 1.0, "2026-10-18 08:00:00"),
                                      ("abandoned", 1.0, ""), ("canceled", 1.0, "2026-10-18 08:00:00"),
                                      ("rejected", 1.0, "2026-10-18 08:00:00")]),
    "非终态混排 printing+queued": ("queued", [("printing", 2.30, "2026-10-18 08:00:00"),
                                              ("queued", 2.30, "")]),
}

matrix_fail = 0
for target in FIX_TARGETS:
    for tag, (pst, files) in SHAPES.items():
        oid = mk("M-%s-%s" % (target, abs(hash(tag)) % 99999), pst, files)
        locked_before = [f[2] for f in files]
        if any(lk is None for lk in locked_before):
            pass  # 上面 SHAPES 里没有 None，另配一个 NULL 用例（见 ⑤）
        r = post({"order_id": oid, "status": target})
        j = r.get_json() or {}
        o_st, f_list, agg = state(oid)
        after_refresh = refresh_parent(oid)
        # 无子任务时 aggregate_order_status 返回 None（且 refresh 不写回）——此时父订单即权威值
        agg_ok = (agg == target) if files else (agg is None)
        ok = (r.status_code == 200 and j.get("success") is True
              and o_st == target
              and all(s == target for s, _ in f_list) and len(f_list) == len(files)
              and agg_ok
              and after_refresh == target          # ← refresh_order_status 回写后不被刷回
              and j.get("files_updated") == len(files)
              and j.get("previous_status") == pst
              and all(lk == "" for _, lk in f_list))
        if not ok:
            matrix_fail += 1
            print("   ✗ target=%s shape=%s → http=%s json=%s o=%s files=%s agg=%s refresh=%s"
                  % (target, tag, r.status_code, j, o_st, f_list, agg, after_refresh))
        if state(CANARY) != CANARY_BEFORE:
            chk("canary 未被波及 [%s/%s]" % (target, tag), False, state(CANARY))

chk("矩阵 25 组合（5 目标 × 5 形态：父订单+全部子任务+聚合+refresh 回写 全部等于目标）",
    len(FIX_TARGETS) == 5 and matrix_fail == 0, "%d 组合失败" % matrix_fail)
chk("canary 订单在所有订正后保持原状（WHERE order_id 作用域无溢出）",
    state(CANARY) == CANARY_BEFORE, "%s → %s" % (CANARY_BEFORE, state(CANARY)))

# 幂等 + 反向（sent → failed → sent），并验证中途不受 refresh 影响
oid_idem = mk("IDEM", "sent", [("sent", 2.30, "")])
r1 = post({"order_id": oid_idem, "status": "sent"})
r2 = post({"order_id": oid_idem, "status": "sent"})
o1, f1, a1 = state(oid_idem)
chk("幂等：重复提交同一状态仍 200 且父/子/聚合均不变",
    r1.status_code == 200 and r2.status_code == 200 and o1 == "sent" and a1 == "sent"
    and all(s == "sent" for s, _ in f1), (r2.status_code, o1, f1, a1))
post({"order_id": oid_idem, "status": "failed"})
o2, f2, a2 = state(oid_idem)
post({"order_id": oid_idem, "status": "sent"})
o3, f3, a3 = state(oid_idem)
chk("反向订正闭环 sent→failed→sent：每一步父/子/聚合三者同步",
    o2 == "failed" and all(s == "failed" for s, _ in f2) and a2 == "failed"
    and o3 == "sent" and all(s == "sent" for s, _ in f3) and a3 == "sent",
    [(o2, f2, a2), (o3, f3, a3)])


# ============================================================================
sec("2. 白名单绕过（40+ 畸形 status：必须被拒 + 库一个字节不变 + 不得 500）")

oid_wl = mk("WL", "abandoned", [("abandoned", 2.30, "2026-10-18 08:00:00")])
WL_BEFORE = state(oid_wl)

MUST_REJECT = [
    ("非终态 printing", "printing"), ("非终态 queued", "queued"),
    ("非终态 scheduled", "scheduled"), ("非终态 waiting", "waiting"),
    ("非终态 downloading", "downloading"), ("非终态 accepted", "accepted"),
    ("非终态 reserved", "reserved"), ("非终态 offline_unknown", "offline_unknown"),
    ("空串", ""), ("纯空格", "   "), ("纯制表换行", "\t\n"),
    ("大小写混合 sEnt", " sEnt "), ("首字母大写 Sent", "Sent"), ("全大写 SENT", "SENT"),
    ("随机大小写 SeNt", "SeNt"),
    ("SQL 注入 sent;DROP TABLE orders", "sent;DROP TABLE orders"),
    ("SQL 注释 sent'--", "sent'--"), ("SQL 双引号 sent\"--", "sent\"--"),
    ("SQL 括号 sent);DROP", "sent);DROP"),
    ("多值 sent,failed", "sent,failed"), ("多值空格 sent failed", "sent failed"),
    ("拆字 s e n t", "s e n t"), ("前缀 sender", "sender"), ("后缀 sentt", "sentt"),
    ("全角 ＳＥＮＴ", "ＳＥＮＴ"), ("西里尔 ѕent", "ѕent"),
    ("空字节 sent\\x00", "sent\x00"),
    ("数字字符串 0", "0"), ("数字字符串 -1", "-1"), ("字面量 null", "null"),
    ("字面量 true", "true"), ("字面量 undefined", "undefined"),
    ("非字符串 None", None), ("非字符串 123", 123), ("非字符串 1.0", 1.0),
    ("非字符串 True", True), ("非字符串 False", False),
    ("非字符串 ['sent']", ["sent"]), ("非字符串 {'a':1}", {"a": 1}),
    ("非字符串 []", []), ("非字符串 {}", {}),
    ("非字符串 ['s','ent']", ["s", "ent"]),
]

wl_fail = 0
for label, val in MUST_REJECT:
    before_fp, before_raw = db_fp(), raw_fp()
    before_st = state(oid_wl)
    try:
        r = post({"order_id": oid_wl, "status": val})
        code, body = r.status_code, (r.get_json() or {})
    except Exception as e:                                  # noqa: BLE001
        code, body = "EXC", "%s: %s" % (type(e).__name__, e)
    after_fp, after_raw = db_fp(), raw_fp()
    after_st = state(oid_wl)
    problems = []
    if code not in (400, 403, 404):
        problems.append("HTTP=%s(期望 400)" % code)
    if code == 500:
        problems.append("500")
    if body.get("success") is True:
        problems.append("success=true")
    if after_fp != before_fp:
        problems.append("逻辑指纹变了")
    if STRICT_RAW and after_raw != before_raw:
        problems.append("原始字节变了")
    if after_st != before_st:
        problems.append("订单行被改: %s → %s" % (before_st, after_st))
    if problems:
        wl_fail += 1
        print("   ✗ %-34s status=%r → %s | %s" % (label, val, code, "；".join(problems)))

chk("%d 项畸形 status 全被拒(400/403/404)、无 500、无 success、库(逻辑+%s)零变化"
    % (len(MUST_REJECT), "原始字节" if STRICT_RAW else "原始字节未判定"),
    wl_fail == 0, "%d 项异常" % wl_fail)
chk("白名单形态未被污染：订单 #%d 仍是 %s" % (oid_wl, WL_BEFORE), state(oid_wl) == WL_BEFORE)

# 归一化宽容面（观察项）：这些值 strip 后 == 'sent'。要么拒绝（安全），要么接受且**存储值就是 sent**。
print("\n-- 2b 归一化宽容面（观察项，判定「口径一致」而非「必须拒绝」）--")
NORMALIZE = [("前后空格 ' sent '", " sent "), ("尾部空格 'sent '", "sent "),
             ("首部空格 ' sent'", " sent"), ("制表换行 '\\tsent\\n'", "\tsent\n"),
             ("尾部制表 'sent\\t'", "sent\t"), ("尾部 CRLF 'sent\\r\\n'", "sent\r\n")]
for label, val in NORMALIZE:
    oid_n = mk("NORM", "abandoned", [("abandoned", 2.30, "")])
    r = post({"order_id": oid_n, "status": val})
    j = r.get_json() or {}
    o_st, f_list, agg = state(oid_n)
    if r.status_code == 200:
        # 若接受：校验「校验值 == 落库值」（否则就是 sent/'' 口径漂移 → 真 bug）
        consistent = (o_st == "sent" and all(s == "sent" for s, _ in f_list) and agg == "sent"
                      and j.get("status") == "sent")
        chk("%s 被归一化接受时，落库值严格为 'sent'（无哨兵值/无前后空白漂移）" % label,
            consistent, "o=%s files=%s agg=%s resp=%s" % (o_st, f_list, agg, j))
        note("%s → HTTP 200，落库 'sent'（输入被 strip 归一化）" % label)
    else:
        chk("%s 被拒绝时不改库" % label, state(oid_n) == (("abandoned", [("abandoned", "")], "abandoned")),
            state(oid_n))
        note("%s → HTTP %s（拒绝）" % (label, r.status_code))


# ============================================================================
sec("3. 鉴权矩阵（header / query / body 三路 + 冲突 + 空值回退）")

oid_auth = mk("AUTH", "abandoned", [("abandoned", 2.30, "2026-10-18 08:00:00")])
AUTH_BEFORE = state(oid_auth)


def try_auth(label, headers=None, query="", body_extra=None, expect_ok=False):
    payload = {"order_id": oid_auth, "status": "sent"}
    if body_extra:
        payload.update(body_extra)
    url = URL + (("?" + query) if query else "")
    fp0, raw0 = db_fp(), raw_fp()
    r = client.post(url, headers=headers or {}, json=payload)
    j = r.get_json() or {}
    fp1, raw1 = db_fp(), raw_fp()
    changed = (fp1 != fp0) or (STRICT_RAW and raw1 != raw0)
    ok = (r.status_code == 200 and j.get("success") is True) if expect_ok else (r.status_code == 403)
    if not expect_ok:
        ok = ok and not changed and state(oid_auth) == AUTH_BEFORE
        # 若某条「非法」真的改了库，说明放行了 → 明确报出
        if changed:
            chk("非法鉴权 %s 不得改库" % label, False, state(oid_auth))
    chk("%s → %s" % (label, "200 放行" if expect_ok else "403 且库不变"),
        ok, "http=%s json=%s changed=%s" % (r.status_code, j, changed))
    if changed and not expect_ok:
        findings.append("鉴权绕过：" + label)
    # 复原（合法请求会改库）
    if changed and expect_ok:
        c = raw()
        c.execute("UPDATE orders SET status='abandoned' WHERE id=?", (oid_auth,))
        c.execute("UPDATE order_files SET status='abandoned', locked_at='2026-10-18 08:00:00'"
                  " WHERE order_id=?", (oid_auth,))
        c.commit()
        c.close()


try_auth("① 完全无 token", headers={})
try_auth("② 错误 header token", headers={"X-Printer-Token": "wrong"})
try_auth("③ 正确 header token", headers=HDR, expect_ok=True)
try_auth("④ 正确 query token（无 header）", headers={}, query="token=" + ADV_TOKEN, expect_ok=True)
try_auth("⑤ 正确 body token（无 header/query）", headers={}, body_extra={"token": ADV_TOKEN}, expect_ok=True)
try_auth("⑥ 正确 header + 错误 query", headers=HDR, query="token=wrong", expect_ok=True)
try_auth("⑦ 错误 header + 正确 query", headers={"X-Printer-Token": "wrong"},
         query="token=" + ADV_TOKEN)
try_auth("⑧ 空 header（应回退 query）", headers={"X-Printer-Token": ""},
         query="token=" + ADV_TOKEN, expect_ok=True)
try_auth("⑨ header 带前后空格", headers={"X-Printer-Token": " " + ADV_TOKEN + " "})
try_auth("⑩ 前缀 token", headers={"X-Printer-Token": ADV_TOKEN + "x"})
try_auth("⑪ 大小写变更 token", headers={"X-Printer-Token": ADV_TOKEN.upper()})

# 无 token + 非 JSON 体：鉴权必须**先于**解析（否则会 500 泄栈）
r = client.post(URL, headers={}, data="not json", content_type="text/plain")
chk("无 token + 非 JSON 请求体 → 403（鉴权先于解析，不 500）",
    r.status_code == 403, "http=%s" % r.status_code)

# ★ 回归守卫（本条原为「断言缺陷存在」的探针，功能方修复后已翻转）：
# 缺陷曾是 —— 未认证时鉴权表达式直接对 `request.get_json()` 的结果 `.get("token")`，
# 请求体是数组/字符串/数字/布尔时抛 AttributeError → 无凭据者也能拿到 500
# （app.py 旧写法 `_get_printer_token() or (request.get_json() or {}).get("token")`）。
# 修复后：请求体只解析一次并做 dict 守卫 ⇒ 未认证畸形体一律 403（鉴权先于任何解析语义），
# 且**库零变化**；任何一条 500/EXC 都是回归。
MALFORMED_BODIES = [
    ("数组体 ['sent']", dict(json=["sent"])),
    ("字符串体 \"sent\"", dict(data='"sent"', content_type="application/json")),
    ("数字体 123", dict(data='123', content_type="application/json")),
    ("布尔体 true", dict(data='true', content_type="application/json")),
    ("null 体", dict(data='null', content_type="application/json")),
    ("空数组 []", dict(json=[])),
    ("空对象 {}", dict(json={})),
    ("坏 JSON '{'", dict(data='{', content_type="application/json")),
    ("空体（无 body）", dict(data='', content_type="application/json")),
]
UA500 = []
ua_bad = []
for label, kw in MALFORMED_BODIES:
    fp0, st0 = db_fp(), state(oid_auth)
    try:
        rr = client.post(URL, headers={}, **kw)
        code = rr.status_code
    except Exception as e:                                   # noqa: BLE001
        code = "EXC:" + type(e).__name__
    if code == 500 or (isinstance(code, str) and code.startswith("EXC")):
        UA500.append((label, code))
    if code not in (400, 403, 404):
        ua_bad.append((label, "HTTP=%s" % code))
    if db_fp() != fp0 or state(oid_auth) != st0:
        ua_bad.append((label, "库被改动"))
    chk("【未认证】无 token + %s → 只能 400/403/404、绝不 500/EXC（实际 %s）"
        % (label, code), code in (400, 403, 404),
        "http=%s  ← 修复前此处崩在鉴权表达式（AttributeError → 500）" % code)
chk("未认证畸形体**一条都不**触发 500/EXC（鉴权前解析崩溃已被 dict 守卫关闭）"
    "＋全部请求库零变化", not UA500 and not ua_bad,
    "500/EXC=%s 其它异常=%s" % (UA500, ua_bad))
if UA500 or ua_bad:
    findings.append("【未认证畸形体回归】请求体只解析一次并做 dict 守卫的修复被打破："
                    "500/EXC=%s 其它异常=%s" % (UA500, ua_bad))

# 带正确 token 的非对象/畸形体 → 400（不是 500）：鉴权先过，dict 守卫把 data 归一成 {}
tok_bad = []
for label, kw in MALFORMED_BODIES:
    try:
        rr = client.post(URL, headers=HDR, **kw)
        code, body = rr.status_code, rr.get_json(silent=True)
    except Exception as e:                                   # noqa: BLE001
        code, body = "EXC:" + type(e).__name__, None
    if not (code == 400 and isinstance(body, dict) and body.get("success") is not True):
        tok_bad.append((label, code, body))
chk("带正确 token + 非对象/畸形体 → 一律 400 且 success≠true（不是 500）",
    not tok_bad, tok_bad)
if tok_bad:
    findings.append("【已认证畸形体回归】带 token 时应 400，实际：%s" % (tok_bad,))

# ── 判据自证（进程内变异）：把「修复前」的鉴权表达式原样搬回一个临时请求上下文，
#    证明本节的检测器（把 500 / AttributeError 记为命中）确有判别力 —— 老表达式必然抛出。
with backend.app.test_request_context(URL, method="POST",
                                      data='["sent"]', content_type="application/json"):
    try:
        _ = backend._get_printer_token() or \
            (backend.request.get_json(silent=True) or {}).get("token", "")
        old_raised = None
    except Exception as e:                                   # noqa: BLE001
        old_raised = type(e).__name__
    _old_data = backend.request.get_json(silent=True)
    if not isinstance(_old_data, dict):
        _old_data = {}
    try:
        _ = backend._get_printer_token() or str(_old_data.get("token") or "")
        new_raised = None
    except Exception as e:                                   # noqa: BLE001
        new_raised = type(e).__name__
chk("[自证] 变异体（修复前 `(request.get_json() or {}).get(\"token\")`）对数组体抛 "
    "AttributeError，而修复后的 dict 守卫不抛 —— 本节判据非空转",
    old_raised == "AttributeError" and new_raised is None,
    "old=%s new=%s" % (old_raised, new_raised))

# PRINTER_TOKEN 未配置时必须 fail-closed
_saved = backend.PRINTER_TOKEN
backend.PRINTER_TOKEN = None
r = post({"order_id": oid_auth, "status": "sent"})
backend.PRINTER_TOKEN = _saved
chk("PRINTER_TOKEN 未配置（None）→ 403 fail-closed（任何 token 都不放行）",
    r.status_code == 403, "http=%s" % r.status_code)


# ============================================================================
sec("4. 事务性（注入 UPDATE 失败 → 必须整体回滚；4 线程并发 → 终态自洽）")

oid_tx = mk("TX", "queued", [("queued", 2.30, "2026-10-18 08:00:00"),
                             ("printing", 2.30, "2026-10-18 08:00:00")])
TX_BEFORE = state(oid_tx)
c = raw()
c.execute("""CREATE TRIGGER adv_tx_fail BEFORE UPDATE OF status ON orders
             WHEN NEW.id = %d BEGIN SELECT RAISE(ABORT, 'adv-injected'); END;""" % oid_tx)
c.commit()
c.close()
note("已注入触发器 adv_tx_fail（父订单 UPDATE 必然抛错），子任务 UPDATE 在其之前执行")

try:
    r = post({"order_id": oid_tx, "status": "failed"})
    tx_code, tx_body = r.status_code, (r.get_json() or {})
except Exception as e:                                       # noqa: BLE001
    tx_code, tx_body = "EXC", "%s: %s" % (type(e).__name__, e)
TX_AFTER = state(oid_tx)
chk("注入失败后：响应非 200/success（实际 %s）" % tx_code,
    tx_code not in (200,) and not (isinstance(tx_body, dict) and tx_body.get("success") is True),
    "%s %s" % (tx_code, tx_body))
chk("注入失败后：**子任务的写入也被回滚**（不留「父没改、子改了」的半截状态）",
    TX_AFTER == TX_BEFORE, "%s → %s" % (TX_BEFORE, TX_AFTER))
chk("注入失败后：父订单仍为 queued、子任务仍为原状态（无半截状态）",
    TX_AFTER == TX_BEFORE and TX_AFTER[0] == "queued", TX_AFTER)

c = raw()
c.execute("DROP TRIGGER adv_tx_fail")
c.commit()
c.close()
r = post({"order_id": oid_tx, "status": "failed"})
o_tx, f_tx, a_tx = state(oid_tx)
chk("移除注入后再提交 → 200 且库正常改写（会话未残留坏事务/未死锁）",
    r.status_code == 200 and o_tx == "failed" and a_tx == "failed"
    and all(s == "failed" for s, _ in f_tx), (r.status_code, o_tx, f_tx, a_tx))

# 4b 并发
oid_cc = mk("CC", "queued", [("queued", 2.30, "") for _ in range(4)])
results = []
lock_r = threading.Lock()


def worker(target):
    cl = backend.app.test_client()
    try:
        rr = cl.post(URL, headers=HDR, json={"order_id": oid_cc, "status": target})
        with lock_r:
            results.append((target, rr.status_code, (rr.get_json() or {}).get("status")))
    except Exception as e:                                   # noqa: BLE001
        with lock_r:
            results.append((target, "EXC", str(e)))


ths = [threading.Thread(target=worker, args=(t,))
       for t in ("sent", "failed", "sent", "failed")]
for t in ths:
    t.start()
for t in ths:
    t.join()
o_cc, f_cc, a_cc = state(oid_cc)
cc_statuses = set([s for s, _ in f_cc])
final_consistent = (o_cc == a_cc and len(cc_statuses) == 1 and o_cc in ("sent", "failed")
                    and all(s == o_cc for s, _ in f_cc))
chk("4 线程并发两种目标：父订单/全部子任务/聚合三者一致且等于其中一个目标（无交错半截）",
    final_consistent, "results=%s state=%s" % (results, (o_cc, f_cc, a_cc)))

# 4c 404 早返回是否遗留写锁
r = post({"order_id": 987654321, "status": "sent"})
chk("订单不存在 → 404", r.status_code == 404, r.status_code)
probe_conn = sqlite3.connect(backend.DATABASE, timeout=2.0)
try:
    probe_conn.execute("BEGIN IMMEDIATE")
    probe_conn.execute("UPDATE orders SET status = status WHERE id = ?", (oid_tx,))
    probe_conn.rollback()
    lock_free = True
    err = ""
except Exception as e:                                       # noqa: BLE001
    lock_free = False
    err = "%s: %s" % (type(e).__name__, e)
finally:
    probe_conn.close()
chk("404 早返回（try 内 return，未显式 rollback）之后另开连接可立刻 BEGIN IMMEDIATE"
    " → 写锁未泄漏", lock_free, err)


# ============================================================================
sec("5. locked_at 清空（含 NULL 行）+ 作用域")

oid_lk = mk("LK", "printing", [("printing", 2.30, "2026-10-18 08:00:00"),
                               ("accepted", 2.30, "2026-10-18 08:00:05")])
c = raw()
c.execute("INSERT INTO order_files (order_id, file_name, copies, page_count, total_price,"
          " status, created_at, page_range, locked_at) VALUES (?,?,?,?,?,?,?,?,NULL)",
          (oid_lk, "null_locked.pdf", 1, 2, 2.30, "printing", "2026-10-18 08:00:00", ""))
c.commit()
c.close()
before_lk = state(oid_lk)
chk("夹具里确实有一行 locked_at IS NULL",
    any(lk is None for _, lk in before_lk[1]), before_lk)
post({"order_id": oid_lk, "status": "sent"})
after_lk = state(oid_lk)
chk("locked_at：NULL 行 + 时间戳行全部变成 ''（不是 NULL、不是保留原值）",
    all(lk == "" for _, lk in after_lk[1]) and all(s == "sent" for s, _ in after_lk[1]),
    after_lk)
c = raw()
n_null = c.execute("SELECT COUNT(*) FROM order_files WHERE order_id=? AND locked_at IS NULL",
                   (oid_lk,)).fetchone()[0]
c.close()
chk("locked_at 物理上也不再是 NULL", n_null == 0, n_null)
chk("父订单也变成 sent", after_lk[0] == "sent", after_lk[0])
chk("锁清空只作用于该单（canary 与其它订单的 locked_at 未被动）",
    state(CANARY) == CANARY_BEFORE, state(CANARY))


# ============================================================================
sec("6. 口径漂移（前端 ORDER_FIXABLE_STATUSES ⇄ 后端 MANUAL_FIX_STATUSES）")

html = open(HTML, encoding="utf-8").read()
app_src = open(os.path.join(DST, "app.py"), encoding="utf-8").read()

m_fe = re.search(r"var ORDER_FIXABLE_STATUSES = \[([^\]]*)\]", html)
m_be = re.search(r"MANUAL_FIX_STATUSES = \(([^)]*)\)", app_src)
fe_list = [s.strip().strip("'\"") for s in m_fe.group(1).split(",") if s.strip()] if m_fe else []
be_list = [s.strip().strip("'\"") for s in m_be.group(1).split(",") if s.strip()] if m_be else []
chk("两侧清单都存在且非空", bool(fe_list) and bool(be_list), "fe=%s be=%s" % (fe_list, be_list))
chk("两侧集合与**顺序**完全一致（顺序不同也算漂移：菜单渲染顺序与白名单同源）",
    fe_list == be_list, "fe=%s be=%s" % (fe_list, be_list))
chk("前端清单 == 后端接口实际接受的集合（用接口动态探测，5/5 全 200）",
    all(post({"order_id": mk("DYN", "queued", [("queued", 1.0, "")]), "status": s}).status_code == 200
        for s in fe_list), fe_list)

prio_keys = re.findall(r'"([a-z_]+)":\s*\d+', app_src.split("_STATUS_PRIORITY = {")[1].split("}")[0])
note("_STATUS_PRIORITY 里的全部状态（后端已知状态全集）= %s" % sorted(set(prio_keys)))
mismatch = []
for s in sorted(set(prio_keys)):
    oid_probe = mk("PROBE", "queued", [("queued", 1.0, "")])
    code = post({"order_id": oid_probe, "status": s}).status_code
    expect = 200 if s in be_list else 400
    if code != expect:
        mismatch.append("%s→%s(期望 %s)" % (s, code, expect))
chk("后端已知状态全集中，只有白名单里的 5 个放行、其余一律 400（无暗门状态）",
    not mismatch, mismatch)
extra_non_terminal = [s for s in ("printing", "queued", "waiting", "downloading",
                                 "accepted", "scheduled", "offline_unknown", "reserved")
                      if s in be_list]
chk("白名单里没有任何非终态（否则能被推送/预约调度重新捡起打印）",
    not extra_non_terminal, extra_non_terminal)


# ============================================================================
sec("7. 前端锁定守卫（源码判据：同源守卫存在且顺序正确 + 静态变异自证）")

m_fn = re.search(r"function applyOrderStatus\(oid, status\) \{(.*?)\n\}", html, re.S)
body_fn = m_fn.group(1) if m_fn else ""


def _guard_order_ok(body_text):
    """判据本体抽成纯函数（不依赖真实源码），供下面的静态变异自证复用：
    守卫存在、是**同源判据** _orderStatusEditable(oid)、且位置早于白名单校验与 fetch。"""
    i_g = body_text.find("if (!_orderStatusEditable(oid))")
    i_w = body_text.find("ORDER_FIXABLE_STATUSES.indexOf(status) < 0")
    i_f = body_text.find("fetch('/api/proxy/admin/order_status'")
    return i_g >= 0 and i_w >= 0 and i_f >= 0 and i_g < i_w < i_f


# 守卫分支必须是「toast + return」（分支内不得出现 fetch）——锁定态/本地模式都只提示。
GUARD_BRANCH = re.compile(
    r"if \(!_orderStatusEditable\(oid\)\) \{\s*\n"
    r"\s*if \(FINANCE_MODE === 'local'\) toast\([^\n]*\);\s*\n"
    r"\s*else toast\([^\n]*\);\s*\n"
    r"\s*return;\s*\n"
    r"\s*\}")

i_guard = body_fn.find("if (!_orderStatusEditable(oid))")
i_wl = body_fn.find("ORDER_FIXABLE_STATUSES.indexOf(status) < 0")
i_fetch = body_fn.find("fetch('/api/proxy/admin/order_status'")
chk("applyOrderStatus 的守卫存在、与渲染模板**同源**（_orderStatusEditable：含 local 判据，"
    "不是只判 unlocked），且位置先于白名单校验与 fetch",
    _guard_order_ok(body_fn), "guard=%s wl=%s fetch=%s" % (i_guard, i_wl, i_fetch))
chk("守卫分支是 toast(...) + return;（分支内没有 fetch，锁定态/本地模式都不会写库）",
    GUARD_BRANCH.search(body_fn) is not None, body_fn[:160])


# ── 判据自证（静态变异）：把真实函数体改坏，上面两条判据必须立刻转红 ──
def _drop_guard(b):
    """变异 1：整段守卫删掉（老行为：直接进白名单校验 → fetch）。"""
    i = b.find("if (!_orderStatusEditable(oid))")
    j = b.find("if (ORDER_FIXABLE_STATUSES.indexOf(status) < 0)", i)
    return (b[:i] + b[j:]) if (i >= 0 and j > i) else b


def _move_guard_to_tail(b):
    """变异 2：守卫搬到白名单校验之后（守卫还在，但顺序错 = 先校验/可能先发请求）。"""
    i = b.find("if (!_orderStatusEditable(oid))")
    j = b.find("if (ORDER_FIXABLE_STATUSES.indexOf(status) < 0)", i)
    return (b[:i] + b[j:] + "\n" + b[i:j]) if (i >= 0 and j > i) else b


def _drop_finance_guard(b):
    """变异 3：同源判据退化成「只判 unlocked」（本地模式 / id 合法性判据被去掉）。"""
    return b.replace("if (!_orderStatusEditable(oid))", "if (!unlocked.orderstatus)")


def _drop_return(b):
    """变异 4：守卫分支去掉 return（会继续落到白名单校验 / fetch）。"""
    return b.replace("return;\n  }\n  if (ORDER_FIXABLE_STATUSES",
                     "}\n  if (ORDER_FIXABLE_STATUSES")


_m_drop, _m_tail = _drop_guard(body_fn), _move_guard_to_tail(body_fn)
_m_noflag, _m_noret = _drop_finance_guard(body_fn), _drop_return(body_fn)
chk("[自证] 静态变异：删掉守卫 / 守卫搬到白名单之后 → 顺序判据转红",
    _m_drop != body_fn and not _guard_order_ok(_m_drop)
    and _m_tail != body_fn and not _guard_order_ok(_m_tail),
    "drop_ok=%s tail_ok=%s" % (_guard_order_ok(_m_drop), _guard_order_ok(_m_tail)))
chk("[自证] 静态变异：守卫退化成「只判 unlocked」（去 FINANCE_MODE）→ 同源判据转红；"
    "守卫分支去掉 return → toast+return 判据转红",
    _m_noflag != body_fn and not _guard_order_ok(_m_noflag)
    and _m_noret != body_fn and GUARD_BRANCH.search(_m_noret) is None,
    "noflag_ok=%s noret_ok=%s" % (_guard_order_ok(_m_noflag),
                                 GUARD_BRANCH.search(_m_noret) is not None))
if not _guard_order_ok(body_fn) or GUARD_BRANCH.search(body_fn) is None:
    findings.append("【前端守卫回归】applyOrderStatus 的同源守卫缺失/顺序错误/不是 toast+return —— "
                    "本地模式订单 id 可能被当云端 id 写到 /api/proxy/admin/order_status")

m_menu = re.search(r"function openOrderStatusMenu\(ev, oid\) \{(.*?)\n\}", html, re.S)
body_menu = m_menu.group(1) if m_menu else ""
# 2026-10-18（lead 按上一轮报告的建议改了一行）：守卫已从「只判 unlocked」收敛为
# 与 applyOrderStatus / 渲染模板**同一判据** `_orderStatusEditable`（含 FINANCE_MODE 与 id 合法性），
# 判据同步收紧到这里 —— 仍然要求「守卫存在且早于创建菜单」。
i_mguard = body_menu.find("if (!_orderStatusEditable(oid))")
i_append = body_menu.find("document.body.appendChild")
chk("openOrderStatusMenu 用的是同一个同源守卫（_orderStatusEditable），且在创建菜单之前",
    0 <= i_mguard < i_append, "guard=%s append=%s" % (i_mguard, i_append))

# 2026-10-18：F4 落地后 `!!oidS`（safeId 字母白名单）收紧为 `/^[0-9]+$/`（与后端
# re.fullmatch(r"[0-9]+") 同源）—— 判据按**新源码形态**同步，不改回源码迁就正则。
EDITABLE_RE = re.compile(r"function _orderStatusEditable\(oidS\) \{\s*\n\s*return "
                         r"FINANCE_MODE !== 'local' && !!unlocked\.orderstatus && !!oidS"
                         r"( && /\^\[0-9\]\+\$/\.test\(String\(oidS\)\))?;")
chk("_orderStatusEditable 同时要求「非 local + 已解锁 + id 是规范十进制」",
    EDITABLE_RE.search(html) is not None
    and "/^[0-9]+$/.test(String(oidS))" in html)
chk("[自证] 静态变异：_orderStatusEditable 去掉 FINANCE_MODE 判据（退回只判 unlocked）→ 上式转红",
    EDITABLE_RE.search(html.replace(
        "return FINANCE_MODE !== 'local' && !!unlocked.orderstatus && !!oidS && /^[0-9]+$/.test(String(oidS));",
        "return !!unlocked.orderstatus && !!oidS;")) is None)
chk("local 模式锁开关不渲染（表头没有入口）",
    re.search(r"function _orderStatusLockHTML\(\) \{\s*\n\s*if \(FINANCE_MODE === 'local'\) return '';",
              html) is not None)
chk("锁定分支只渲染纯标签（无 onclick、无 .st-caret 箭头）",
    ": _stTag) + '</td>';" in html and
    re.search(r"var _stTag = '<span class=\"tag .*?</span>';", html) is not None)
chk(".st-caret 仅出现在 _orderStatusEditable 为真的分支内",
    re.search(r"_orderStatusEditable\(oidS\)\s*\n?\s*\? '<span class=\"st-edit can\"[\s\S]{0,300}?st-caret"
              r"[\s\S]{0,200}?: _stTag\)", html) is not None)
chk("提交走 /api/proxy/admin/order_status（POST），且只在 success 后本地落地",
    re.search(r"fetch\('/api/proxy/admin/order_status', \{\s*\n\s*method: 'POST'", html) is not None and
    re.search(r"if \(!\(res && res\.success\)\) \{[\s\S]{0,160}?return; \}", body_fn) is not None)
chk("前端可选状态集合 == 菜单渲染集合（ORDER_FIXABLE_STATUSES 单一定义，菜单 forEach 它）",
    html.count("var ORDER_FIXABLE_STATUSES") == 1 and
    "ORDER_FIXABLE_STATUSES.forEach(function (s) {" in html)
# F8a（2026-10-18 审查）：这里原先是 note()，理由写的是「行内 onclick 用 safeId(oidS) 白名单 ⇒
# 实际不可达（仅防御深度不一致）」—— **判据错误**：safeId 的白名单是 ^[A-Za-z0-9_-]+$，
# 放行字母与 e，'1e3' / '12-34' 都能过，在 JS 里分别被求值成 1000 与 -22（真的会写到另一张单）。
# 真正让内联表达式不可达的理由是「order_id 只认规范十进制」，所以升级为 chk，
# 盯前端同款闸门（与后端 re.fullmatch(r"[0-9]+") 同源）；缺了它 = 非规范十进制 id 可能被当表达式。
_DECIMAL_ID_GATE = re.compile(
    r"function _orderStatusEditable\(oidS\)\s*\{[\s\S]{0,600}?/\^\[0-9\]\+\$/")
chk("★ _orderStatusEditable 只认十进制 id（/^[0-9]+$/，与后端 re.fullmatch 同源）"
    "—— 行内 onclick 不可达的**真正**理由（safeId 字母白名单放行 '1e3'/'12-34'，不构成理由）",
    _DECIMAL_ID_GATE.search(html) is not None)


# ============================================================================
sec("8. 金额 / 结算口径（sent 计入 ⇄ failed/abandoned/canceled/rejected 退出）")

REV_ALL = ("/api/admin/statistics/revenue?start_date=2026-10-18&end_date=2026-10-18"
           "&status=sent,printing,queued,failed,canceled,rejected,accepted,abandoned,"
           "reserved,offline_unknown,scheduled,waiting,downloading&token=" + ADV_TOKEN)
REV_DEFAULT = ("/api/admin/statistics/revenue?start_date=2026-10-18&end_date=2026-10-18"
               "&token=" + ADV_TOKEN)


def rev(oid, default=False):
    j = client.get(REV_DEFAULT if default else REV_ALL).get_json() or {}
    data = j.get("data") or {}
    for o in (data.get("orders") or []):
        if str(o.get("order_id")) == str(oid):
            return o, data
    return None, data


oid_rev = mk("REV", "abandoned", [("abandoned", 2.30, ""), ("abandoned", 2.30, "")],
             total_price=5.10, created_at="2026-10-18 06:00:00")
before_o, before_data = rev(oid_rev)
chk("订正前（abandoned）：状态 abandoned、金额 0",
    before_o and before_o["status"] == "abandoned"
    and round(before_o["revenue"] + before_o["receivable"], 2) == 0.0,
    before_o and (before_o["status"], before_o["revenue"], before_o["receivable"]))
before_total = round((before_data.get("revenue_summary") or {}).get("total", 0), 2)

post({"order_id": oid_rev, "status": "sent"})
after_o, after_data = rev(oid_rev)
after_total = round((after_data.get("revenue_summary") or {}).get("total", 0), 2)
chk("→ sent：明细状态 sent，金额 = 文件 4.60 + 附加服务 0.50 = 5.10（附加费随订单级一起计入）",
    after_o and after_o["status"] == "sent"
    and round(after_o["revenue"], 2) == 5.10 and after_o["receivable"] == 0.0,
    after_o and (after_o["status"], after_o["revenue"], after_o["receivable"], after_o["total_price"]))
chk("→ sent：revenue_summary.total 恰好增加 5.10（聚合口径同步）",
    abs((after_total - before_total) - 5.10) < 0.005, "%s → %s" % (before_total, after_total))
chk("→ sent：文件级状态全部为 sent（展开行显示的即这一份）",
    after_o and all(f["status"] == "sent" for f in after_o["files"]),
    after_o and [f["status"] for f in after_o["files"]])

for tgt in ("failed", "abandoned", "canceled", "rejected"):
    post({"order_id": oid_rev, "status": tgt})
    o_t, d_t = rev(oid_rev)
    chk("→ %s：明细状态 %s、金额归零（退出收益统计）" % (tgt, tgt),
        o_t and o_t["status"] == tgt
        and round(o_t["revenue"] + o_t["receivable"], 2) == 0.0,
        o_t and (o_t["status"], o_t["revenue"], o_t["receivable"]))
    chk("→ %s：revenue_summary.total 回到订正前水平" % tgt,
        abs(round((d_t.get("revenue_summary") or {}).get("total", 0), 2) - before_total) < 0.005,
        (d_t.get("revenue_summary") or {}).get("total"))

# 默认筛选口径（不含 abandoned/canceled/rejected）：改成 abandoned 后该单应从默认查询消失
post({"order_id": oid_rev, "status": "sent"})
in_default_sent = rev(oid_rev, default=True)[0] is not None
post({"order_id": oid_rev, "status": "abandoned"})
in_default_abandoned = rev(oid_rev, default=True)[0] is not None
post({"order_id": oid_rev, "status": "failed"})
in_default_failed = rev(oid_rev, default=True)[0] is not None
chk("默认筛选（status 缺省）下：sent 在册、abandoned 消失、failed 仍在册（failed 属默认集合）",
    in_default_sent and not in_default_abandoned and in_default_failed,
    "sent=%s abandoned=%s failed=%s" % (in_default_sent, in_default_abandoned, in_default_failed))


# ============================================================================
sec("9. 额外攻击面（畸形请求体 / 注入 / 类型混淆）")

oid_x = mk("X", "abandoned", [("abandoned", 2.30, "")])
X_BEFORE = state(oid_x)

# 9a 非 dict 的 JSON 体：修复后必须**一律 400**（鉴权已过 + dict 守卫把 data 归一成 {} → 缺
#    order_id），不是 500；且库不变。（原探针只要求「不 500」，翻转后收紧到精确 400。）
BODY_SHAPES = [
    ("数组体 [\"sent\"]", "[\"sent\"]"),
    ("字符串体 \"sent\"", "\"sent\""),
    ("数字体 123", "123"),
    ("布尔体 true", "true"),
    ("null 体", "null"),
    ("空对象 {}", "{}"),
    ("空数组 []", "[]"),
    ("坏 JSON '{'", "{"),
]
for label, raw_body in BODY_SHAPES:
    fp0 = db_fp()
    try:
        r = client.post(URL, headers=HDR, data=raw_body, content_type="application/json")
        code, j = r.status_code, r.get_json(silent=True)
    except Exception as e:                                   # noqa: BLE001
        code, j = "EXC:" + type(e).__name__, None
    fp1 = db_fp()
    chk("请求体 %s → 精确 400（不是 500）且 success≠true、库不变（实际 %s）" % (label, code),
        code == 400 and isinstance(j, dict) and j.get("success") is not True and fp1 == fp0,
        "http=%s json=%s db_changed=%s" % (code, j, fp1 != fp0))
    if code != 400:
        findings.append("【已认证畸形体回归】携带正确 token 时非对象/坏 JSON 体（%s）应 400，"
                        "实际 %s（修复前崩在 `data.get(...)` → 500）" % (label, code))

# 9b Content-Type 变体
r = client.post(URL, headers=HDR, data=json.dumps({"order_id": oid_x, "status": "sent"}),
                content_type="text/plain")
chk("Content-Type: text/plain 的 JSON 体 → 400（get_json 返回 None → {}），不 500",
    r.status_code == 400, "http=%s" % r.status_code)
r = client.post(URL, headers=HDR, data=json.dumps({"order_id": oid_x, "status": "sent"}),
                content_type="application/json")
chk("Content-Type: application/json 显式 → 200", r.status_code == 200, "http=%s" % r.status_code)

# 9c order_id 注入 / 类型混淆（不得 500、不得越权改别的单）
INJ_IDS = [("SQL 注入 1 OR 1=1", "1 OR 1=1"), ("SQL 注入 ' OR '1'='1", "' OR '1'='1"),
           ("注释 1--", "1--"), ("分号 ;DROP", "1;DROP TABLE orders"),
           ("通配 %", "%"), ("下划线 _", "_"), ("空字节", "\x00"),
           ("超长字符串", "9" * 5000), ("负数", "-1"),
           ("列表 [" + str(CANARY) + "]", "[" + str(CANARY) + "]"),
           ("canary 的裸 id（合法但指向别的单）", str(CANARY))]
for label, oid_val in INJ_IDS:
    fp0 = db_fp()
    try:
        r = post({"order_id": oid_val, "status": "sent"})
        code, j = r.status_code, (r.get_json() or {})
    except Exception as e:                                   # noqa: BLE001
        code, j = "EXC:" + type(e).__name__, ""
    fp1 = db_fp()
    bad_here = (code not in (200, 400, 404)) and not (isinstance(code, str) and code.startswith("EXC"))
    chk("order_id=%s → 不 500、库自洽" % label,
        not bad_here and code != 500, "http=%s json=%s" % (code, j))
    if code == 500:
        findings.append("order_id 畸形触发 500：%s" % label)
    if label.startswith("canary 的裸 id"):
        chk("  ↑ 指向 canary 的合法请求确实改到了 canary（说明 9c 其余项没误伤）",
            state(CANARY)[0] == "sent", state(CANARY))
        c = raw()
        c.execute("UPDATE orders SET status='printing' WHERE id=?", (CANARY,))
        c.execute("UPDATE order_files SET status='printing', locked_at='2026-10-18 08:00:00'"
                  " WHERE order_id=?", (CANARY,))
        c.commit()
        c.close()
    else:
        chk("  ↑ 库未变", fp1 == fp0, "db_changed=%s" % (fp1 != fp0))

# 9c-2 数值型宽松匹配（SQLite INTEGER 亲和性）：非整数 id 是否被静默改成某个整数 id。
# 本条守护功能方修复 #3「order_id 只认规范十进制（纯 ASCII 数字、无前后空白、无前导零）」。
print("\n-- 9c-2 order_id 亲和性强制转换（对「只认规范十进制」的回归守卫）--")
CAND_IDS = sorted(set([1, 2, 3, 27, 28, CANARY]))
CAND_BASE = {cid: state(cid) for cid in CAND_IDS}

# 自证：SQLite 对 **INTEGER 亲和性列** 做比较时会先把文本折算成整数 —— 没有 re.fullmatch
# 守卫时 '01' / '1.0' / '+1' / ' 1 ' 都会命中订单 1（这正是修复前「改错单」的机制）。
_mc = sqlite3.connect(":memory:")
_mc.execute("CREATE TABLE t (id INTEGER)")
_mc.execute("INSERT INTO t VALUES (1)")
_aff_hit = _mc.execute("SELECT id FROM t WHERE id = ?", ("01",)).fetchall()
_mc.close()
chk("[自证] 变异体：SQLite 会把 '01' 折成整数 1（老实现无 re.fullmatch 守卫时必改订单 1）"
    "—— 本条判据非空转", _aff_hit == [(1,)], _aff_hit)


def restore(oid_rec, snap):
    c = raw()
    c.execute("UPDATE orders SET status=? WHERE id=?", (snap[0], oid_rec))
    c.execute("DELETE FROM order_files WHERE order_id=?", (oid_rec,))
    for st, lk in snap[1]:
        c.execute("INSERT INTO order_files (order_id, file_name, copies, page_count, total_price,"
                  " status, created_at, page_range, locked_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (oid_rec, "restore.pdf", 1, 2, 1.0, st, "2026-10-18 08:00:00", "", lk))
    c.commit()
    c.close()


for label, oid_val in [("'01'", "01"), ("'+1'", "+1"), ("'1e0'", "1e0"),
                       ("' 1 '", " 1 "), ("'1.0'", "1.0"), ("'1.5'", "1.5"),
                       ("'1abc'", "1abc"), ("'0x1'", "0x1")]:
    before = {cid: state(cid) for cid in CAND_IDS}
    try:
        r = post({"order_id": oid_val, "status": "sent"})
        code = r.status_code
    except Exception as e:                                   # noqa: BLE001
        code = "EXC:" + type(e).__name__
    changed = [cid for cid in CAND_IDS if state(cid) != before[cid]]
    if changed:
        note("order_id=%s → HTTP %s，实际命中订单 %s（SQLite 亲和性把非整数 id 折算成整数）"
             % (oid_val, code, changed))
        findings.append("order_id 亲和性宽松匹配：%r 静默解析到订单 %s（预期 404）"
                        % (oid_val, changed))
    else:
        note("order_id=%s → HTTP %s，无任何订单被改" % (oid_val, code))
    chk("order_id=%s 未触发 500 且仅可能命中其数值等价订单" % label,
        code != 500, code)
    chk("order_id=%s 未被 SQLite 亲和性折算到别的订单（应 400 且库零变化）" % label,
        not changed and code == 400, "http=%s 命中=%s" % (code, changed))
    for cid in changed:
        restore(cid, before[cid])
    chk("  ↑ 探针后库已复原（%s）" % label,
        all(state(cid) == before[cid] for cid in CAND_IDS), "复原失败")
chk("亲和性探针未污染 canary", state(CANARY) == CAND_BASE[CANARY],
    "%s vs %s" % (state(CANARY), CAND_BASE[CANARY]))

# 9d 重复 JSON 键：Python 取后者 → 必须按后者判（不得出现「校验一个值、写另一个值」）
fp0 = db_fp()
r = client.post(URL, headers=HDR, data='{"order_id": %d, "status": "sent", "status": "printing"}'
                % oid_x, content_type="application/json")
fp1 = db_fp()
chk("重复 status 键（sent + printing）→ 400（取后者 'printing'），库不变",
    r.status_code == 400 and fp1 == fp0, "http=%s db_changed=%s" % (r.status_code, fp1 != fp0))
r = client.post(URL, headers=HDR, data='{"order_id": %d, "status": "printing", "status": "sent"}'
                % oid_x, content_type="application/json")
chk("重复 status 键（printing + sent）→ 200 且落库 sent（与校验值同一个变量）",
    r.status_code == 200 and state(oid_x)[0] == "sent",
    (r.status_code, state(oid_x)))

# 9e status 是布尔/数字时 str() 后的字面量不会意外命中白名单（已在 §2 覆盖），补一个真值表断言
chk("str(True)/str(1)/str(None) 都不是白名单成员",
    all(s not in FIX_TARGETS for s in (str(True), str(1), str(None), str(0))))

chk("全部 9 组额外攻击后：oid_x 最终状态与最后一次合法请求一致（sent）、无畸形请求被误放行",
    state(oid_x)[0] == "sent", state(oid_x))


# ============================================================================
print("\n" + "=" * 70)
print("汇总")
print("=" * 70)
if findings:
    print("发现的问题（需上报）：")
    for f in sorted(set(findings)):
        print("  ✗ " + f)
else:
    print("未发现可证伪的问题（所有攻击面均被拦下/行为自洽）")
print("FAIL 计数 = %d" % bad)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if bad else 0)
