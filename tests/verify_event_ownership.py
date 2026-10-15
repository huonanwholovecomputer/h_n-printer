# -*- coding: utf-8 -*-
"""验收 · 🔴-D3 后端 SocketIO 事件处理器的归属/状态校验缺口

现场形状（审查报告 D3）：
  `print_success` / `print_fail` 的归属校验统一写成
      if row and row["operator_client"] and row["operator_client"] != client_id: 拒绝
  —— **`operator_client` 为空即完全放行**。而它确实会为空：
  `on_disconnect` 的断线回滚（`SET status='queued', operator_client=''`）与
  `recover_orphaned_printing_tasks` 的孤儿回收（同款清空）都会把它抹掉。
  加上 `task_id` 是自增整数，任何持全局 printer token 的设备只要猜个 id 就能：
    · 把**别人的**任务判 failed / 标成功（订单账目、顾客体验被改写）；
    · 用 `start_printing` 把**别人的** waiting/queued 任务抢成自己的 printing（劫持）；
    · 用 `print_keepalive` 替别人的任务续命（阻碍超时回收 ⇒ 卡单）；
    · 用 `download_delayed` 冻结别人的预约单。

  **实测把同族处理器全查了一遍：都是同一个写法**（不止报告点名的 2 处）——
  print_success / print_fail / file_ready / download_delayed / start_printing /
  print_keepalive，外加 `page_count_result`（无归属校验）与 `page_range_truncated`
  （既无来源校验、又无状态守卫、又无长度上限）。本脚本盯的是**整族**。

修法（本次）：
  ① 新增**持久**列 `order_files.last_operator_client`：在 4 个领取/派发写点与
     `operator_client` 一同写入，**断线回滚/孤儿回收都不清空** ⇒ 得到"上次是谁在打"。
  ② 统一归属判定 `_subtask_ownership()`：有当前领取者 → 必须一致；
     无领取者 → 用 last_operator_client 兜底；再无记录 ⇒ **拒绝**（fail-closed）。
     同族处理器全部改用它，不再各写一份"空值放行"。
  ③ `page_count_result`：`_analysis_push_sid` 从纯 sid 改为 `{"sid","client_id"}`，
     归属判据取 **client_id**（跨重连仍成立，比计划里的"sid 匹配"更稳），
     并给上报页数加数值上限。
  ④ `page_range_truncated`：补来源校验 + 活跃状态守卫 + 长度上限 `[:500]`
     （与同文件 `_sync_subtask_config` 的写路径同款）。

本脚本：
  A. 静态接线（判据集中在 `audit_static()`，可被 C 节对拍复用）。
  B. 行为断言：**真实后端 + Flask-SocketIO test_client**，设备 A、B 两条**真实连接**
     （各自的 sid 与 client_id）对同一批任务发事件 —— 断言"非归属者一律被拒、
     归属者仍被接受（断线回滚闭环不回归）、无归属记录 fail-closed"。
  C. 对拍：把代码逐个喂回"改前形态"，`audit_static()` 必须报出对应违规
     （否则它就是"永远为绿"的假守卫）。

用法：python tests/verify_event_ownership.py     （exit 0 = 全部通过）
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
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")
APP = os.path.join(SRC, "app.py")

bad = 0


def chk(name, cond, detail=""):
    global bad
    if not cond:
        bad += 1
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + detail) if (not cond and detail) else ""))


# ============================================================
# 判据（集中在一处，供 A 节实跑 + C 节对拍复用）
# ============================================================

HANDLERS = ["on_print_success", "on_print_fail", "on_file_ready", "on_download_delayed",
            "on_start_printing", "on_print_keepalive", "on_page_range_truncated"]

# 判据 key 的**全量清单**：C 节结束后要核对"每条判据都被某次变异打红过"，
# 否则可能存在**死判据**（写了却永远不触发 = 另一种假守卫）。
KEYS = ("MIGRATION", "BACKFILL", "WRITE_SITE", "WRITE_SITE_COUNT", "ROLLBACK_CLEARS",
        "OLD_PATTERN", "NO_HELPER", "HANDLER_NO_GUARD", "ANALYSIS_SID", "CANCEL_SID",
        "PC_GUARD", "PC_CAP", "TRUNC_CAP", "TRUNC_STATUS")

# 「活跃状态守卫」的三种合法写法（判据取**语义**不取字面）：
#   ① SQL 字面量：`... status NOT IN ('sent','canceled') ...`
#   ② Python 裸名：`if status in ("canceled", ...)`
#   ③ Python 下标：`if own["status"] in ("canceled", ...)` / row['status'] in (...)
#   ⚠ 第一版只写了 `['"]status['"]`（**漏了闭括号**），对 ③ 恒不命中 ⇒
#     TRUNC_STATUS 变成"永远报红"的死判据，连带把两条对拍也污染成红。已修。
_STATUS_GUARD = re.compile(
    r"(?:status\s+(?:NOT\s+)?IN\b)"                                  # ① SQL
    r"|(?:\bstatus\s+(?:NOT\s+)?in\s*\()"                            # ② 裸名
    r"|(?:\[\s*['\"]status['\"]\s*\]\s+(?:NOT\s+)?in\s*\()"          # ③ 下标 own["status"]
)


def _top_level_defs(src):
    """{函数名: 函数体文本} —— 按**列 0 的 `def `** 切分，避免把下一个处理器吞进来。"""
    marks = [(m.start(), m.group(1)) for m in re.finditer(r"(?m)^def (\w+)\(", src)]
    marks.append((len(src), ""))
    return {marks[i][1]: src[marks[i][0]:marks[i + 1][0]] for i in range(len(marks) - 1)}


def _sql_statements(src):
    """抽出源码里所有含 `UPDATE` 的字符串字面量（单行双/单引号字面量）。
    用于把"检查 SET 子句"限定在**单条 SQL** 内，不跨语句误伤。"""
    out = re.findall(r'"([^"\n]*UPDATE[^"\n]*)"', src)
    out += re.findall(r"'([^'\n]*UPDATE[^'\n]*)'", src)
    return out


def _blank_comment_lines(text):
    """整行注释置空 —— 判据必须吃**代码**不吃注释。
    （注释里天然会引用被考代码片段，比如 `[:500]`、`"sid"`，不剥就会**假绿**：
     真代码删了、注释还留在那里，判据照样命中。）"""
    return "\n".join("" if ln.lstrip().startswith("#") else ln for ln in text.splitlines())


def audit_static(src):
    """静态判据：返回违规列表（空 = 合规）。每条违规带 key，供对拍精确定位。"""
    v = []
    defs = _top_level_defs(src)
    code = _blank_comment_lines(src)          # 判别只看代码，注释一律置空

    # ① 迁移
    if "ALTER TABLE order_files ADD COLUMN last_operator_client" not in src:
        v.append("MIGRATION: 缺 order_files.last_operator_client 列的迁移语句")
    if not re.search(r"UPDATE order_files SET last_operator_client = operator_client", src):
        v.append("BACKFILL: 存量行未回填 last_operator_client（老任务会全部 fail-closed）")

    # ② 四个领取/派发写点带新列
    #   printing 3 处：fetch_and_lock_task（HTTP 拉单）/ 派发 / start_printing
    #   downloading 1 处：派发（预约单阶段①）
    write_pat = r"UPDATE order_files SET status = '%s', operator_client = \?, locked_at = \?[^\"]*last_operator_client = \?"
    for st, expect in (("printing", 3), ("downloading", 1)):
        got = len(re.findall(write_pat % st, src))
        if got != expect:
            v.append("WRITE_SITE: `status = '%s'` 的领取/派发语句应 %d 条带 last_operator_client，实际 %d"
                     % (st, expect, got))
    if len(re.findall(r"last_operator_client = \?", src)) != 4:
        v.append("WRITE_SITE_COUNT: 带 last_operator_client 的写点应为 4 处，实际 %d"
                 % len(re.findall(r"last_operator_client = \?", src)))

    # ③ 回滚/回收不得清空新列 —— 判据只看**每条 SQL 的 SET 子句**。
    #    整串搜会被 v28 的存量回填误伤（它把 `last_operator_client = ''` 写在 WHERE 里），
    #    用 `SET…WHERE` 的粗窗口也一样会跨语句误伤（相邻 SQL 字面量的 WHERE 被卷进来）。
    for _sql in _sql_statements(src):
        if "last_operator_client" not in _sql:
            continue
        _set_clause = _sql.split("WHERE", 1)[0]
        if re.search(r"last_operator_client\s*=\s*['\"]{2}", _set_clause):
            v.append("ROLLBACK_CLEARS: 有 SET 子句清空了 last_operator_client（退化成「无归属」）")
            break

    # ④ 旧的"空值放行"写法必须消失（**排除注释行** —— 我的说明注释里引用了它）+ 统一判定必须存在
    live_hits = [ln for ln in src.splitlines()
                 if '["operator_client"] and ' in ln and not ln.lstrip().startswith("#")]
    if live_hits:
        v.append("OLD_PATTERN: 仍有 `[\"operator_client\"] and ` 空值放行写法（%d 处）" % len(live_hits))
    if "def _subtask_ownership(" not in src:
        v.append("NO_HELPER: 缺统一归属判定 _subtask_ownership()")
    for fn in HANDLERS:
        if "_subtask_ownership(" not in defs.get(fn, ""):
            v.append("HANDLER_NO_GUARD: %s 未调用统一归属判定" % fn)

    # ⑤ page_count_result 的归属形态（dict + client_id）与数值上限
    if not re.search(r'_analysis_push_sid\[file_id\] = \{\s*"sid":\s*sid,\s*"client_id":\s*client_id',
                     code):
        v.append("ANALYSIS_SID: _analysis_push_sid 未记录 {\"sid\",\"client_id\"}")
    # 取消分析必须从**新形态**的条目里取出 sid —— 判据要绑定 `_analysis_push_sid.pop(...)`
    # 后紧跟的 `.get("sid")` 提取，否则会命中满仓的 `sid = info["sid"]` 而**假绿**
    #（第一版就踩了：`re.search(r'sid = .*\["sid"\]')` 满仓皆中）。
    if not re.search(r"_analysis_push_sid\.pop\([^\n]*\n(?:[^\n]*\n){0,4}[^\n]*\.get\(\s*[\"']sid[\"']",
                     code):
        v.append("CANCEL_SID: 取消分析未从 _analysis_push_sid 的条目里取 sid（改成 dict 后会退化为广播）")
    pc = _blank_comment_lines(defs.get("on_page_count_result", ""))
    if "_analysis_push_sid.get(" not in pc or re.search(r'[\'"]client_id[\'"]', pc) is None:
        v.append("PC_GUARD: page_count_result 未用 _analysis_push_sid 的 client_id 判定归属")
    if "MAX_REPORTED_PAGES" not in pc:
        v.append("PC_CAP: page_count_result 缺上报页数上限")

    # ⑥ page_range_truncated 的长度上限与状态守卫
    pt = _blank_comment_lines(defs.get("on_page_range_truncated", ""))
    if "[:500]" not in pt:
        v.append("TRUNC_CAP: page_range_truncated 缺长度上限 [:500]（与 _sync_subtask_config 同款）")
    if not _STATUS_GUARD.search(pt):
        v.append("TRUNC_STATUS: page_range_truncated 缺活跃状态守卫")
    return v


src = open(APP, encoding="utf-8").read()

# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线（audit_static）===")
violations = audit_static(src)
chk("★ 静态判据全绿（%d 条判据）" % len(KEYS), not violations, "；".join(violations))

# 逐条打印关键接线（便于人读）
_defs = _top_level_defs(src)
for _fn in HANDLERS:
    chk("   %s 调用统一归属判定" % _fn, "_subtask_ownership(" in _defs.get(_fn, ""))
_m = re.search(r"def _subtask_ownership\(([^)]*)\)", src)
print("   _subtask_ownership 签名: (%s)" % (_m.group(1) if _m else "?"))

# ============================================================
# B. 行为断言：真实后端 + 两条真实 socket 连接
# ============================================================
print("\n=== B. 真实链路（临时后端副本 + Flask-SocketIO test_client）===")

TMP = tempfile.mkdtemp(prefix="hn_own_")
DST = os.path.join(TMP, "printer-backend")
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(
    "__pycache__", "config.py", "config.py.example",
    "orders.db", "users.db", "uploads", "logs", "*.db-wal", "*.db-shm"))
os.chdir(DST)
os.environ["FLASK_ENV"] = "development"
sys.path.insert(0, DST)

try:
    import app as backend
except Exception as e:  # noqa: BLE001
    print("✗ 无法导入 app.py: %r" % (e,))
    os.chdir(REPO)
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1)

backend.init_db()

TOK = "test-token-d3"
backend.PRINTER_TOKEN = TOK          # 无 config.py 时默认 None ⇒ 连接会被拒
DEV_A, DEV_B, DEV_C = "own-dev-A", "own-dev-B", "own-dev-C"

http = backend.app.test_client()
r = http.post("/api/printer/claim", headers={"X-Printer-Token": TOK},
              json={"client_id": DEV_A})
assert (r.get_json() or {}).get("success") is True, "claim 失败: %s" % r.get_json()


def _connect(cid):
    # 注意：本版本 flask_socketio 的 query_string 只接受 "?k=v&..." 字符串
    # （传 dict 会在 test_client.py 里抛 KeyError: 0）
    qs = "?token=%s&client_id=%s" % (TOK, cid)
    c = backend.socketio.test_client(backend.app, query_string=qs)
    assert c.is_connected(), "连接失败: %s" % cid
    c.get_received()          # 丢掉 connect 阶段的推送，避免与后面的 get_received 串台
    return c


A = _connect(DEV_A)
B = _connect(DEV_B)

_seq = [0]


def mk_order(**kw):
    """建一条 orders 行（只有 file/copies/created_at 是 NOT NULL）。"""
    _seq[0] += 1
    oid = kw.pop("id", None) or (90000 + _seq[0])
    cols = {"file": "x.pdf", "copies": 1, "created_at": "2026-01-01 00:00:00",
            "status": "printing", "openid": "own-guest", "target_client": "",
            "schedule_mode": "now", "schedule_frozen": 0, "scheduled_at": ""}
    cols.update(kw)
    conn = backend.get_db()
    conn.execute("INSERT INTO orders (%s) VALUES (%s)" % (
        ", ".join(["id"] + list(cols)), ", ".join(["?"] * (len(cols) + 1))),
        [oid] + list(cols.values()))
    conn.commit()
    conn.close()
    return oid


def _table_cols(table):
    """实际存在的列名集合 —— 夹具据此过滤，使**改前**（新列还没迁移）也能跑起来、
    红在行为断言上，而不是崩在 INSERT 上（反向验证要求"红必须落在修复点"）。"""
    conn = backend.get_db()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
    conn.close()
    return cols


def mk_sub(order_id, status="queued", owner="", last="", **kw):
    # 注意：status 是具名参数，必须自己塞进 cols —— 否则 INSERT 会落到表默认值
    # （order_files.status DEFAULT 'printing'），让"waiting/queued/canceled"等场景
    # 名不副实（第一版就这么错过，S4/S6/S10 全变成"任务本来就是 printing"）。
    cols = {"file_id": "", "file_name": "x.pdf", "copies": 1, "page_count": 2,
            "created_at": "2026-01-01 00:00:00", "page_range": "", "status": status,
            "locked_at": "", "operator_client": owner, "last_operator_client": last}
    cols.update(kw)
    have = _table_cols("order_files")
    cols = {k: v for k, v in cols.items() if k in have}     # 改前没有 last_operator_client
    conn = backend.get_db()
    cur = conn.execute("INSERT INTO order_files (%s) VALUES (%s)" % (
        ", ".join(["order_id"] + list(cols)), ", ".join(["?"] * (len(cols) + 1))),
        [order_id] + list(cols.values()))
    conn.commit()
    tid = cur.lastrowid
    # 父订单聚合状态与该子任务一致（否则 refresh_order_status 会按聚合改写，干扰断言）
    conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    conn.commit()
    conn.close()
    return tid


def _row(table, key, val):
    conn = backend.get_db()
    row = conn.execute("SELECT * FROM %s WHERE %s = ?" % (table, key), (val,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def sub(tid):
    return _row("order_files", "id", tid)


def order(oid):
    return _row("orders", "id", oid)


def mk_file(fid, pages=0, verified=0):
    sub_dir = os.path.join(backend.UPLOAD_DIR, "pdf")
    os.makedirs(sub_dir, exist_ok=True)
    path = os.path.abspath(os.path.join(sub_dir, fid + ".pdf"))
    with open(path, "wb") as fh:
        fh.write(b"%PDF-1.4\n% fixture\n")
    conn = backend.get_db()
    conn.execute(
        """INSERT INTO files (id, original_name, saved_name, path, size, created_at,
                              page_count, page_count_verified, md5, openid)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (fid, fid + ".pdf", fid + ".pdf", path, os.path.getsize(path),
         "2026-01-01 00:00:00", pages, verified, "md5-" + fid, "own-guest"))
    conn.commit()
    conn.close()
    return fid


def pages_of(fid):
    return _row("files", "id", fid).get("page_count")


def send(c, ev, data):
    """发一个事件（test_client 的 emit 是同步派发；这里只做保险 + 清空收件箱）。"""
    c.emit(ev, data)
    time.sleep(0.02)
    c.get_received()


# ---- S1 有明确领取者：非领取者必须被拒 ----
print("\n--- S1 明确领取者（owner=A）---")
oid = mk_order()
t1 = mk_sub(oid, status="printing", owner=DEV_A, last=DEV_A)
send(B, "print_success", {"task_id": t1})
chk("S1 print_success：非领取者 B 发 → 仍 printing", sub(t1)["status"] == "printing",
    "实际 %s" % sub(t1)["status"])
send(A, "print_success", {"task_id": t1})
chk("S1 print_success：领取者 A 发 → sent", sub(t1)["status"] == "sent",
    "实际 %s" % sub(t1)["status"])

# ---- S2 领取者为空（断线回滚后）：非"上次领取者"必须被拒 ← 修前的洞 ----
# 每个场景用**两条独立任务**：攻击者 B 打 t_att、归属者 A 打 t_own。
# 合成一条会让"归属者仍被接受"的断言在改前**假绿**（任务已被 B 改过了）。
print("\n--- S2 领取者为空（回滚后，last=A）---")
t2 = mk_sub(mk_order(status="queued"), status="queued", owner="", last=DEV_A)
send(B, "print_success", {"task_id": t2})
chk("★ S2 print_success：B 发 → 必须仍 queued（不许被他人标记成功）",
    sub(t2)["status"] == "queued", "实际 %s" % sub(t2)["status"])
t2o = mk_sub(mk_order(status="queued"), status="queued", owner="", last=DEV_A)
send(A, "print_success", {"task_id": t2o})
chk("★ S2 print_success：上次领取者 A 发 → sent（断线回滚闭环不回归）",
    sub(t2o)["status"] == "sent", "实际 %s" % sub(t2o)["status"])

t3 = mk_sub(mk_order(status="queued"), status="queued", owner="", last=DEV_A)
send(B, "print_fail", {"task_id": t3, "error": "他人伪造"})
s3 = sub(t3)
chk("★ S2 print_fail：B 发 → 必须仍 queued 且不接受其 reject_reason",
    s3["status"] == "queued" and "他人伪造" not in (s3.get("reject_reason") or ""),
    "status=%s reason=%r" % (s3["status"], s3.get("reject_reason")))
t3o = mk_sub(mk_order(status="queued"), status="queued", owner="", last=DEV_A)
send(A, "print_fail", {"task_id": t3o, "error": "真失败"})
chk("★ S2 print_fail：上次领取者 A 发 → failed", sub(t3o)["status"] == "failed",
    "实际 %s" % sub(t3o)["status"])

# ---- S3 从未派发过（无任何归属记录）⇒ fail-closed ----
print("\n--- S3 无归属记录（从未派发）---")
oid = mk_order()
t4 = mk_sub(oid, status="queued", owner="", last="")
send(A, "print_success", {"task_id": t4})
chk("★ S3 print_success：无归属记录 → 必须仍 queued（不许放行）",
    sub(t4)["status"] == "queued", "实际 %s" % sub(t4)["status"])

# ---- S4 start_printing 劫持 ----
print("\n--- S4 start_printing（抢占）---")
oid5 = mk_order(status="waiting")
t5 = mk_sub(oid5, status="waiting", owner="", last=DEV_A)
send(B, "start_printing", {"order_id": oid5, "task_ids": [t5]})
s5 = sub(t5)
chk("★ S4 start_printing：B 发 → 仍 waiting 且 operator_client 不被改写",
    s5["status"] == "waiting" and (s5.get("operator_client") or "") != DEV_B,
    "status=%s owner=%r" % (s5["status"], s5.get("operator_client")))
oid5o = mk_order(status="waiting")
t5o = mk_sub(oid5o, status="waiting", owner="", last=DEV_A)
send(A, "start_printing", {"order_id": oid5o, "task_ids": [t5o]})
s5o = sub(t5o)
chk("S4 start_printing：A 发 → printing 且 owner=A",
    s5o["status"] == "printing" and s5o["operator_client"] == DEV_A,
    "status=%s owner=%r" % (s5o["status"], s5o.get("operator_client")))

# ---- S5 print_keepalive 替别人续命 ----
# 夹具刻意用 owner=""（**断线回滚之后**）：给 owner=DEV_A 的话走的是"有当前领取者"分支，
# 旧代码在那里本来就会拒（改前也绿 ⇒ 探不到洞）。必须让 current 为空、只留 last 才能打到点。
print("\n--- S5 print_keepalive（续命，回滚后 operator_client 为空）---")
oid = mk_order()
t6 = mk_sub(oid, status="printing", owner="", last=DEV_A,
            locked_at="2020-01-01 00:00:00")
send(B, "print_keepalive", {"task_ids": [t6]})
chk("★ S5 print_keepalive：B 发 → locked_at 不被刷新",
    sub(t6)["locked_at"] == "2020-01-01 00:00:00",
    "实际 %r" % sub(t6)["locked_at"])
t6o = mk_sub(mk_order(), status="printing", owner="", last=DEV_A,
             locked_at="2020-01-01 00:00:00")
send(A, "print_keepalive", {"task_ids": [t6o]})
chk("S5 print_keepalive：上次领取者 A 发 → locked_at 被刷新（回滚后仍可续命）",
    sub(t6o)["locked_at"] != "2020-01-01 00:00:00",
    "实际 %r" % sub(t6o)["locked_at"])

# ---- S6 file_ready 把别人的下载任务标就绪 ----
print("\n--- S6 file_ready ---")
oid6 = mk_order(status="downloading")
t7 = mk_sub(oid6, status="downloading", owner="", last=DEV_A)
send(B, "file_ready", {"order_id": oid6, "task_ids": [t7]})
chk("★ S6 file_ready：B 发 → 仍 downloading", sub(t7)["status"] == "downloading",
    "实际 %s" % sub(t7)["status"])
oid6o = mk_order(status="downloading")
t7o = mk_sub(oid6o, status="downloading", owner="", last=DEV_A)
send(A, "file_ready", {"order_id": oid6o, "task_ids": [t7o]})
chk("S6 file_ready：A 发 → waiting", sub(t7o)["status"] == "waiting",
    "实际 %s" % sub(t7o)["status"])

# ---- S7 download_delayed 冻结别人的预约单 ----
print("\n--- S7 download_delayed ---")
oid7 = mk_order(status="downloading", schedule_mode="scheduled", schedule_frozen=0)
t8 = mk_sub(oid7, status="downloading", owner="", last=DEV_A)
send(B, "download_delayed", {"order_id": oid7, "task_ids": [t8]})
chk("★ S7 download_delayed：B 发 → 订单不被冻结",
    int(order(oid7).get("schedule_frozen") or 0) == 0,
    "schedule_frozen=%r" % order(oid7).get("schedule_frozen"))
oid7o = mk_order(status="downloading", schedule_mode="scheduled", schedule_frozen=0)
t8o = mk_sub(oid7o, status="downloading", owner="", last=DEV_A)
send(A, "download_delayed", {"order_id": oid7o, "task_ids": [t8o]})
chk("S7 download_delayed：A 发 → 冻结生效",
    int(order(oid7o).get("schedule_frozen") or 0) == 1,
    "schedule_frozen=%r" % order(oid7o).get("schedule_frozen"))

# ---- S8 未注册连接（伪造 sid）—— 资格校验的第一道门 ----
print("\n--- S8 未注册连接 ---")
C = _connect(DEV_C)
with backend.printer_clients_lock:
    backend.printer_clients.pop(DEV_C, None)      # 模拟"连接未注册/已注销"
oid = mk_order()
t9 = mk_sub(oid, status="printing", owner=DEV_C, last=DEV_C)
send(C, "print_success", {"task_id": t9})
chk("S8 未注册连接发 print_success → 被拒（仍 printing）",
    sub(t9)["status"] == "printing", "实际 %s" % sub(t9)["status"])
C.disconnect()

# ---- S9 page_count_result 只认"被请求的那台设备" ----
print("\n--- S9 page_count_result（归属 + 数值上限）---")
fid = mk_file("own-f-page", pages=0, verified=0)
ok_push = backend.request_page_analysis(fid, "own-f-page.pdf", prefer_client=DEV_A)
_entry = backend._analysis_push_sid.get(fid)
_requested = _entry.get("client_id") if isinstance(_entry, dict) else ""
chk("S9 分析请求已推送且记录的目标设备是 A",
    ok_push is True and _requested == DEV_A,
    "ok=%r 记录=%r" % (ok_push, _entry))

send(B, "page_count_result", {"file_id": fid, "page_count": 9, "orientation": ""})
chk("★ S9 page_count_result：非被请求设备 B 报 → files.page_count 保持 0",
    pages_of(fid) == 0, "实际 %r" % pages_of(fid))

# 归属者用**另一个文件**（否则"写入 9"会因为 B 已经写过了而假绿）
fid_a = mk_file("own-f-page-a", pages=0, verified=0)
backend.request_page_analysis(fid_a, "own-f-page-a.pdf", prefer_client=DEV_A)
send(A, "page_count_result", {"file_id": fid_a, "page_count": 9, "orientation": ""})
chk("S9 page_count_result：被请求设备 A 报 → 写入 9",
    pages_of(fid_a) == 9, "实际 %r" % pages_of(fid_a))
send(A, "page_count_result", {"file_id": fid_a, "page_count": 10 ** 9, "orientation": ""})
chk("★ S9 page_count_result：离谱页数（1e9）→ 被拒（仍 9）",
    pages_of(fid_a) == 9, "实际 %r" % pages_of(fid_a))

fid2 = mk_file("own-f-unasked", pages=0, verified=0)
send(A, "page_count_result", {"file_id": fid2, "page_count": 3, "orientation": ""})
chk("★ S9 page_count_result：从未请求过的文件 → 被拒（仍 0，fail-closed）",
    pages_of(fid2) == 0, "实际 %r" % pages_of(fid2))

# ---- S10 page_range_truncated：来源 + 状态 + 长度 ----
print("\n--- S10 page_range_truncated ---")
oid = mk_order()
t10 = mk_sub(oid, status="printing", owner="", last=DEV_A, page_range="1-9999")
send(B, "page_range_truncated", {"task_id": t10, "original_range": "1-9999",
                                 "effective_range": "1-100", "total_pages": 100})
chk("★ S10 page_range_truncated：B 发 → page_range 不被改写",
    sub(t10)["page_range"] == "1-9999", "实际 %r" % sub(t10)["page_range"])
t10o = mk_sub(mk_order(status="printing"), status="printing", owner="", last=DEV_A,
              page_range="1-9999")
send(A, "page_range_truncated", {"task_id": t10o, "original_range": "1-9999",
                                 "effective_range": "1-100", "total_pages": 100})
s10 = sub(t10o)
chk("S10 page_range_truncated：A 发 → 生效（effective + original 落库）",
    s10["page_range"] == "1-100" and s10["page_range_original"] == "1-9999"
    and int(s10.get("page_range_truncated") or 0) == 1,
    "range=%r orig=%r flag=%r" % (s10["page_range"], s10["page_range_original"],
                                  s10.get("page_range_truncated")))
send(A, "page_range_truncated", {"task_id": t10o, "original_range": "A" * 900,
                                 "effective_range": "B" * 900, "total_pages": 1})
chk("★ S10 page_range_truncated：超长输入被截断到 500",
    len(sub(t10o)["page_range_original"] or "") == 500
    and len(sub(t10o)["page_range"] or "") == 500,
    "orig=%d eff=%d" % (len(sub(t10o)["page_range_original"] or ""),
                        len(sub(t10o)["page_range"] or "")))

oid = mk_order(status="canceled")
t11 = mk_sub(oid, status="canceled", owner="", last=DEV_A, page_range="1-5")
send(A, "page_range_truncated", {"task_id": t11, "original_range": "1-5",
                                 "effective_range": "1-2", "total_pages": 2})
chk("★ S10 page_range_truncated：已取消任务被状态守卫拒绝",
    sub(t11)["page_range"] == "1-5", "实际 %r" % sub(t11)["page_range"])

A.disconnect()
B.disconnect()

# ============================================================
# C. 对拍：audit_static 必须真会报错（否则是假守卫）
# ============================================================
print("\n=== C. 对拍（喂坏输入，audit_static 必须报）===")

_fired = []      # 所有变异真正打红过的判据 key —— 收尾核对"无死判据"


def _mut(name, text, expect_key):
    v = audit_static(text)
    keys = [x.split(":")[0] for x in v]
    _fired.extend(keys)
    chk("★ 对拍：%s → 必须报 %s" % (name, expect_key), expect_key in keys,
        "实际违规=%s" % (v[:4] or "无"))


# C1 写回旧的"空值放行"
_mut("写回 `[\"operator_client\"] and` 空值放行写法",
     src.replace("if not ok:", 'if ro and ro["operator_client"] and ro["operator_client"] != client_id:'),
     "OLD_PATTERN")

# C2 写点丢掉新列
_mut("4 个写点去掉 last_operator_client",
     re.sub(r", last_operator_client = \?", "", src), "WRITE_SITE_COUNT")

# C3 只改一个写点（计数仍对得上吗？—— 精确到语句的判据必须报）
_one = re.sub(r"(UPDATE order_files SET status = 'printing', operator_client = \?, locked_at = \?)",
              r"\1", src, count=1)
_mut("只从 3 个写点里去掉 1 个 last_operator_client",
     _one.replace("locked_at = ?, last_operator_client = ?", "locked_at = ?", 1),
     "WRITE_SITE")

# C4 回滚路径加清空
_mut("回滚路径清空 last_operator_client",
     src.replace("SET status = 'queued', operator_client = '', locked_at = ''",
                 "SET status = 'queued', operator_client = '', last_operator_client = '', locked_at = ''", 1),
     "ROLLBACK_CLEARS")

# C5 迁移缺失
_mut("删掉 last_operator_client 的迁移语句",
     src.replace("ALTER TABLE order_files ADD COLUMN last_operator_client", "ALTER TABLE order_files ADD COLUMN zz_removed", 1),
     "MIGRATION")

# C6 回填缺失
_mut("删掉存量行回填语句",
     re.sub(r"UPDATE order_files SET last_operator_client = operator_client[^\"]*", "SELECT 1", src),
     "BACKFILL")

# C7 逐个处理器去掉统一判定调用
for _fn in HANDLERS:
    _body = _defs.get(_fn, "")
    _stripped = _body.replace("_subtask_ownership(", "_ownership_removed(")
    _mut("%s 去掉统一归属判定调用" % _fn, src.replace(_body, _stripped, 1), "HANDLER_NO_GUARD")

# C8 page_count_result 的归属判据与上限
_mut("page_count_result 去掉 _analysis_push_sid 归属判定",
     src.replace(_defs.get("on_page_count_result", "\0"), "\0", 1), "PC_GUARD")

# C9 取消分析不再从条目里取 sid（dict 形态被丢 ⇒ 退化为广播/漏发）
_mut("取消分析不再从条目里取 sid（丢弃 dict 形态）",
     re.sub(r'sid = _entry\.get\("sid"\) if isinstance\(_entry, dict\) else _entry',
            'sid = _entry if isinstance(_entry, str) else ""', src, count=1),
     "CANCEL_SID")

# C10 截断处理器去掉长度上限 / 状态守卫
_mut("page_range_truncated 去掉 [:500]",
     src.replace(_defs.get("on_page_range_truncated", "\0"),
                 _defs.get("on_page_range_truncated", "\0").replace("[:500]", ""), 1),
     "TRUNC_CAP")

# C12 截断处理器的状态守卫被改写成不认 status 的自造字段
#     （第一版判据恰恰在这里是**死判据**：它永远报红 ⇒ 这条对拍也不成立，必须钉死）
_mut("page_range_truncated 的状态守卫换成不认 status 的字段",
     src.replace('if own["status"] in ("canceled", "rejected", "abandoned", "sent"):',
                 'if own.get("record_state") in ("canceled", "rejected", "abandoned", "sent"):', 1),
     "TRUNC_STATUS")

# C10b 只删**代码里**的 [:500]，把注释里那句"…也是 [:500]"留着 ——
#      判据若不剥注释就会被蒙混过关（这条对拍就是钉这个）。
_ptb = _defs.get("on_page_range_truncated", "\0")
_mut("page_range_truncated 只在代码里去掉 [:500]（注释仍留 [:500]）",
     src.replace(_ptb, "\n".join(
         ln if ln.lstrip().startswith("#") else ln.replace("[:500]", "")
         for ln in _ptb.splitlines()), 1), "TRUNC_CAP")

# C14／C15／C16：这三条判据原先**没有任何变异覆盖**（C13 的死判据检查刚揪出来的），
#   补齐 —— 否则它们写了也等于没写（可能一直报红，也可能一直不报红）。
_mut("删掉 _subtask_ownership 的定义",
     src.replace("def _subtask_ownership(", "def _ownership_zz(", 1), "NO_HELPER")

_mut("_analysis_push_sid 退回纯 sid 形态（旧写法）",
     src.replace('_analysis_push_sid[file_id] = {"sid": sid, "client_id": client_id}',
                 "_analysis_push_sid[file_id] = sid", 1), "ANALYSIS_SID")

_pc_body = _defs.get("on_page_count_result", "\0")
_mut("page_count_result 去掉上报页数上限",
     src.replace(_pc_body, _pc_body.replace("MAX_REPORTED_PAGES", "ZZ_REMOVED_PAGES"), 1), "PC_CAP")

# C11 正常源码不误报（假绿也假、假红也假 —— 这一条钉住"别乱报"）
chk("★ 对拍：未改动的真实源码 audit_static 返回空（不误报）", not audit_static(src))

# C13 判据覆盖：每条判据都必须被某次变异打红过（否则是**死判据**）
_dead = [k for k in KEYS if k not in _fired]
chk("★ 对拍：%d 条判据全部被变异覆盖（无死判据）" % len(KEYS), not _dead,
    "从未被打红: %s" % _dead)

print("\n" + "=" * 56)
print("事件归属校验验收:", "全部通过 ✅" if bad == 0 else "存在失败 ❌（%d 条）" % bad)
try:
    os.chdir(REPO)
    shutil.rmtree(TMP, ignore_errors=True)
except Exception:
    pass
sys.exit(0 if bad == 0 else 1)
