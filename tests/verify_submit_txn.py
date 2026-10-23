# -*- coding: utf-8 -*-
"""验收 · S2-02 submit_order 缩短写事务 + 响应体保护（后端）

依据：docs/安全加固修复计划_2026-10-23.md §S2-02（DDoS 报告 A3 / R6）。

现场形状（修复前）：
  「父单 INSERT → 逐文件 SELECT files + get_file_page_count(读 PDF，磁盘 IO) + 计费 + INSERT」
  全在同一个 SQLite 写事务里。写者锁是全站唯一的 ⇒ 页数解析的磁盘 IO 直接摊到所有写路径上
  （报告实测 1 MB 请求可把写锁占 1.7 s，拉单/推送/取消/定时任务全部排队）。
  响应还会把**整个请求体**（`data`）原样回显，以及逐条回显全部子任务。

本脚本：
  A. 静态：预计算抽成 `_plan_order_files` 且在写事务**之前**调用；写事务段内不再出现
     读盘/解析/读价（`get_file_page_count` / `_load_paper_prices` / `SELECT path`）；
     响应体有 `files_total`、不再回显 `data`（正常与重复提交两支）。
  B. 行为（临时副本 + 真 SQLite + 真 Flask）：
     · 正常建单：子任务页数/价格/状态正确、`files.page_count` 回写正确；
     · **并发写者不被慢 IO 挡住**（把 `get_file_page_count` 换成 sleep(0.4)，
       另开连接 `BEGIN IMMEDIATE` 必须几乎立刻拿到锁）—— 这是本条修复的核心判据；
     · 响应体：无 `data` 键、有 `files_total`；重复提交同样不回显请求体；
     · 21 个文件仍被入口上限拒绝（S0-04 的闸门没被这次重构绕掉）。
  C. 变异自证（子进程）：把预计算挪回**显式写事务内**（BEGIN IMMEDIATE → 预计算 → commit），
     并发写者必须被阻塞 ≥0.3 s（判据能红）。

用法：python tests/verify_submit_txn.py     （exit 0 = 全部通过）
"""
import io
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()
src = H.read_app_source()
code = H.strip_comments(src)


def block(marker):
    i = code.find(marker)
    if i < 0:
        return ""
    lines = code[i:].splitlines(keepends=True)
    out = [lines[0]]
    for ln in lines[1:]:
        if ln.strip() and not ln[:1].isspace():
            break
        out.append(ln)
    return "".join(out)


plan_fn = block("def _plan_order_files(")
submit_fn = block("def submit_order(")
dup_fn = block("def _duplicate_order_response(")

# 写事务段 = 从「预计算用的只读连接 close 之后」到 conn.commit()
# （注释已被 strip_comments 去掉，所以用代码锚点而不是注释锚点）
_i = submit_fn.find("pre_conn.close()")
_j = submit_fn.find("conn.commit()", _i) if _i >= 0 else -1
txn_span = submit_fn[_i:_j] if (_i >= 0 and _j > _i) else ""

# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
H.chk("预计算抽成独立函数 _plan_order_files", "def _plan_order_files(" in code)
H.chk("★ submit_order 在写事务前调用 _plan_order_files（读盘在锁外）",
      _i > 0 and submit_fn.find("_plan_order_files(") < _i,
      (submit_fn.find("_plan_order_files("), _i))
H.chk("预计算用**只读连接**（pre_conn，用完即 close）",
      "pre_conn = get_db()" in submit_fn and "pre_conn.close()" in submit_fn)
H.chk("★ 写事务段内没有任何读盘/解析/读价",
      txn_span != "" and "get_file_page_count" not in txn_span
      and "_load_paper_prices" not in txn_span and "SELECT path, original_name" not in txn_span)
H.chk("页数回写（files.page_count）仍在写事务内做（persist_pages）",
      "persist_pages" in plan_fn and "UPDATE files SET page_count = ?" in txn_span)
H.chk("单价只读一次并传进预计算（与 S2-01 的缓存/循环外口径衔接）",
      "prices=prices" in plan_fn and "_plan_order_files(pre_conn" in submit_fn
      and "schedule_mode, _load_paper_prices())" in submit_fn)
H.chk("★ 响应体不再回显请求体 data，且带 files_total",
      '"data": data' not in submit_fn and '"files_total"' in submit_fn)
H.chk("响应回显的子任务有上限 MAX_ECHO_FILES",
      "MAX_ECHO_FILES = 50" in code and "sub_tasks[:MAX_ECHO_FILES]" in submit_fn)
H.chk("重复提交响应也不再回显 data", '"data": data' not in dup_fn)
H.chk("入口上限仍是 MAX_ORDER_FILES（S0-04 的闸门没被绕掉）",
      "MAX_ORDER_FILES" in submit_fn and "MAX_ORDER_FILES = 20" in code)

# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（临时副本 + 真 SQLite / 真 Flask）===")
TMP, DST = H.make_copy("hn_txn_")
backend = H.import_backend(DST)
backend.init_db()
backend.PRINTER_TOKEN = "txn-tok"
backend.ADMIN_OPENIDS = {"txn-user"}
http = backend.app.test_client()
AUTH = {"Authorization": "Bearer " + backend.token_serializer.dumps("txn-user")}

# 造一个"页数未缓存"的 PDF 行：路径真实存在 ⇒ submit_order 会去解析页数
pdf_path = os.path.join(DST, "uploads", "txn_sample.pdf")
with open(pdf_path, "wb") as f:
    f.write(b"%PDF-1.4\n%fake\n")
conn = backend.get_db()
conn.execute(
    "INSERT INTO files (id, original_name, saved_name, path, size, created_at, openid) "
    "VALUES (?,?,?,?,?,?,?)",
    ("txn-f1", "样例.pdf", "txn_sample.pdf", pdf_path, 16, "2026-10-23 10:00:00", "txn-user"))
conn.commit()
conn.close()

# `get_file_page_count` 换成「慢 IO」：0.4 s 后才返回 3 页
_orig_gfpc = backend.get_file_page_count


def _slow_page_count(path, ext):
    time.sleep(0.4)
    return 3


backend.get_file_page_count = _slow_page_count
try:
    result = {}


    def _submit():
        try:
            r = http.post("/api/submit_order",
                          json={"files": [{"file_id": "txn-f1", "copies": 2}],
                                "client_request_id": "txn-req-1"},
                          headers=AUTH)
            result["status"] = r.status_code
            result["json"] = r.get_json()
        except Exception as e:                     # noqa: BLE001
            result["error"] = repr(e)


    import threading
    th = threading.Thread(target=_submit)
    th.start()
    time.sleep(0.12)                               # 让它进入预计算（慢 IO 进行中）
    c = backend.get_db()
    t0 = time.time()
    try:
        c.execute("BEGIN IMMEDIATE")               # 另一个写者（拉单/推送/取消的典型动作）
        lock_wait = time.time() - t0
    finally:
        c.rollback()
        c.close()
    th.join(timeout=30)
finally:
    backend.get_file_page_count = _orig_gfpc

H.chk("★ 慢 IO 解析页数期间，另一个写者 0.2 s 内就拿到写锁（未被挡住）",
      lock_wait < 0.2, round(lock_wait, 3))
H.chk("请求本身成功（预计算在锁外，结果照样正确）",
      result.get("status") == 200 and (result.get("json") or {}).get("success") is True,
      result.get("status"))

body = result.get("json") or {}
files = body.get("files") or []
H.chk("子任务页数=解析结果 3、份数=2", files and files[0]["page_count"] == 3
      and files[0]["copies"] == 2, files[:1])
H.chk("响应有 files_total 且等于子任务数", body.get("files_total") == len(files) == 1,
      (body.get("files_total"), len(files)))
H.chk("★ 响应不再回显请求体 data", "data" not in body, sorted(body.keys()))

conn = backend.get_db()
row = conn.execute("SELECT page_count, page_count_verified FROM files WHERE id='txn-f1'").fetchone()
conn.close()
H.chk("页数回写仍落在 files 表（3 / verified=1）",
      row and row["page_count"] == 3 and int(row["page_count_verified"]) == 1,
      (row["page_count"], row["page_count_verified"]) if row else None)

# 重复提交：同一把幂等键 → is_duplicate，且同样不回显请求体
r2 = http.post("/api/submit_order",
               json={"files": [{"file_id": "txn-f1", "copies": 2}],
                     "client_request_id": "txn-req-1"}, headers=AUTH)
j2 = r2.get_json() or {}
H.chk("重复提交被幂等拦下（is_duplicate）且不回显 data",
      j2.get("is_duplicate") is True and "data" not in j2, sorted(j2.keys()))

# 入口上限没被重构绕掉
many = [{"file_id": "txn-f1", "copies": 1} for _ in range(21)]
r3 = http.post("/api/submit_order", json={"files": many}, headers=AUTH)
H.chk("21 个文件仍 400（入口上限照旧生效）", r3.status_code == 400, r3.status_code)

# 页数非法（份数）仍在开事务前就被拒：库里不应多出任何订单行
conn = backend.get_db()
before = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
conn.close()
r4 = http.post("/api/submit_order",
               json={"files": [{"file_id": "txn-f1", "copies": "abc"}]}, headers=AUTH)
conn = backend.get_db()
after = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
conn.close()
H.chk("份数非法 → 400 且**一行订单都没落库**（预计算阶段就挡住）",
      r4.status_code == 400 and before == after, (r4.status_code, before, after))

# ============================================================
# C. 变异自证：把预计算挪回显式写事务内
# ============================================================
print("\n=== C. 变异自证（预计算挪回 BEGIN IMMEDIATE 内）===")
mut_tmp, mut_dst = H.mutated_copy([
    ("    pre_conn = get_db()\n"
     "    try:\n"
     "        file_plan = _plan_order_files(pre_conn, files_input, g.openid, duplex,\n"
     "                                      schedule_mode, _load_paper_prices())\n"
     "    except OrderRejected as e:\n"
     "        return jsonify({\"success\": False, \"message\": e.message}), 400\n"
     "    finally:\n"
     "        pre_conn.close()\n",
     "    pre_conn = get_db()\n"
     "    try:\n"
     "        pre_conn.execute(\"BEGIN IMMEDIATE\")\n"
     "        file_plan = _plan_order_files(pre_conn, files_input, g.openid, duplex,\n"
     "                                      schedule_mode, _load_paper_prices())\n"
     "        pre_conn.commit()\n"
     "    except OrderRejected as e:\n"
     "        return jsonify({\"success\": False, \"message\": e.message}), 400\n"
     "    finally:\n"
     "        pre_conn.close()\n"),
], prefix="hn_txn_mut_")

proc = H.run_probe(mut_dst, r"""
import threading, time, os
backend.PRINTER_TOKEN = "probe-tok"
backend.ADMIN_OPENIDS = {"txn-user"}
http = backend.app.test_client()
AUTH = {"Authorization": "Bearer " + backend.token_serializer.dumps("txn-user")}
pdf = os.path.join(os.getcwd(), "uploads", "probe_sample.pdf")
open(pdf, "wb").write(b"%PDF-1.4\n%fake\n")
conn = backend.get_db()
conn.execute("INSERT INTO files (id, original_name, saved_name, path, size, created_at, openid) "
             "VALUES ('p1','a.pdf','probe_sample.pdf',?,16,'2026-10-23 10:00:00','txn-user')", (pdf,))
conn.commit(); conn.close()
def slow(path, ext):
    time.sleep(0.4)
    return 3
backend.get_file_page_count = slow
res = {}
def run():
    r = http.post("/api/submit_order", json={"files": [{"file_id": "p1", "copies": 1}]}, headers=AUTH)
    res["status"] = r.status_code
th = threading.Thread(target=run); th.start()
time.sleep(0.12)
c = backend.get_db(); t0 = time.time()
c.execute("BEGIN IMMEDIATE"); wait = time.time() - t0
c.rollback(); c.close()
th.join(timeout=30)
print("WAIT=%.3f" % wait)
print("STATUS=%s" % res.get("status"))
""")
mut_wait = H.probe_value(proc, "WAIT") or "0"
H.chk("★ 变异（预计算在写事务内）⇒ 并发写者被阻塞 ≥0.3 s（判据能红）",
      float(mut_wait) >= 0.3, mut_wait)

H.finish()
