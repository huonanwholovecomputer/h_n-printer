# -*- coding: utf-8 -*-
"""验收 · 🟡-D5 第 6 条：提交幂等键必须有唯一约束（毫秒级双击不再重复建单）

缺陷（审查报告 D5）：`submit_order` 的幂等是**先查再插** —— `SELECT ... client_request_id`
之后才 `INSERT`，而 `orders` 表**没有唯一索引**。小程序/APP 双击、弱网自动重试这类毫秒级并发
会让两个请求都查不到对方、于是**都插入** ⇒ 重复建单 = 重复印 + 重复计费。

修复后的语义（本脚本钉住）：
  A. 串行重复提交（回归）：第二次返回 `is_duplicate` + 原 order_id，库里只有 1 行。
  B. **并发双击**（真线程 + barrier 强制两边都先过预查）：只建 1 单，两边拿到同一 order_id。
  C. 部分唯一索引 `idx_orders_client_req` 存在且 UNIQUE，空串不参与。
  D. 原单**已取消**后重新提交同一把钥匙 → 建**新单**（换钥匙），不撞索引（老语义保持）。
  E. **超出 10 分钟窗口**复用同一把钥匙 → 仍建新单（老语义保持；不能因为有了索引就返回旧单）。
  F. 不带幂等键（空串）的多次提交互不影响。
  G. 迁移幂等：重复 `init_db()` 不报错、索引仍在。
  H. 去重迁移：库里已有重复键时，`init_db()` 能把索引建起来（保留最早一条，其余清空该列）。

用法: python tests/verify_submit_idempotency.py     （exit 0 = 全绿）
"""
import io
import os
import shutil
import sys
import tempfile
import threading

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


TMP = tempfile.mkdtemp(prefix="hn_idem_")
DST = os.path.join(TMP, "printer-backend")
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(
    "__pycache__", "config.py", "config.py.example",
    "orders.db", "users.db", "uploads", "logs", "*.db-wal", "*.db-shm"))
os.chdir(DST)
os.environ["FLASK_ENV"] = "development"
sys.path.insert(0, DST)

import app as backend           # noqa: E402

backend.init_db()

OID = "openid-idem"
backend.ADMIN_OPENIDS.add(OID)     # 管理员提交无需许可密钥（本脚本不验许可链路）
HDR = {"Authorization": "Bearer " + backend.token_serializer.dumps(OID)}
client = backend.app.test_client()


def _mk_file(fid, pages=3):
    sub = os.path.join(backend.UPLOAD_DIR, "pdf")
    os.makedirs(sub, exist_ok=True)
    path = os.path.abspath(os.path.join(sub, fid + ".pdf"))
    with open(path, "wb") as fh:
        fh.write(b"%PDF-1.4\n% fixture\n")
    conn = backend.get_db()
    conn.execute(
        """INSERT INTO files (id, original_name, saved_name, path, size, created_at,
                              page_count, page_count_verified, md5, openid)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (fid, fid + ".pdf", fid + ".pdf", path, os.path.getsize(path),
         "2026-01-01 00:00:00", pages, "md5-" + fid, OID))
    conn.commit()
    conn.close()
    return fid


FILE_ID = _mk_file("idem-file-1")


def body_for(key, fid=FILE_ID):
    b = {"files": [{"file_id": fid, "copies": 1, "duplex": "on"}], "duplex": "on",
         "client": "wechat"}
    if key is not None:
        b["client_request_id"] = key
    return b


def submit(key, fid=FILE_ID, cli=None):
    r = (cli or client).post("/api/submit_order", headers=HDR, json=body_for(key, fid))
    try:
        j = r.get_json() or {}
    except Exception:
        j = {}
    return r.status_code, j


def rows_for(key):
    conn = backend.get_db()
    rows = conn.execute(
        "SELECT id, order_number, status, client_request_id FROM orders"
        " WHERE openid = ? AND client_request_id = ? ORDER BY id ASC", (OID, key)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def all_rows():
    conn = backend.get_db()
    rows = conn.execute("SELECT id, client_request_id FROM orders ORDER BY id ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


print("=== A. 串行重复提交（回归：语义不变）===")
st1, j1 = submit("key-A")
st2, j2 = submit("key-A")
check("首次提交成功", st1 == 200 and j1.get("success") and not j1.get("is_duplicate"), str(j1)[:120])
check("第二次被判重复并返回原单",
      st2 == 200 and j2.get("is_duplicate") is True and j2.get("order_id") == j1.get("order_id"),
      f"{st2} {j2}")
check("库里该幂等键只有 1 行", len(rows_for("key-A")) == 1, str(rows_for("key-A")))

print("=== B. 并发双击（真线程 + barrier 强制两边都先过预查）===")
_barr = threading.Barrier(2, timeout=15)
_orig_gen = backend.generate_order_number


def _gen_barrier():
    """让两个线程都在「已过预查、尚未插入」的位置对齐 ⇒ 稳定复现毫秒级并发。"""
    try:
        _barr.wait()
    except threading.BrokenBarrierError:
        pass
    return _orig_gen()


backend.generate_order_number = _gen_barrier
_results = []


def _worker():
    cli = backend.app.test_client()
    try:
        _results.append(submit("key-B", cli=cli))
    except Exception as e:  # noqa: BLE001
        _results.append((0, {"exception": repr(e)}))


_t1 = threading.Thread(target=_worker)
_t2 = threading.Thread(target=_worker)
_t1.start()
_t2.start()
_t1.join(timeout=30)
_t2.join(timeout=30)
backend.generate_order_number = _orig_gen

_rows_b = rows_for("key-B")
check("★ 两个并发请求都成功返回（没有 500）",
      len(_results) == 2 and all(st == 200 and j.get("success") for st, j in _results),
      str([(st, j.get("message")) for st, j in _results]))
check("★ 并发双击只建了 **1** 单（改前是 2 单 = 重复印 + 重复计费）",
      len(_rows_b) == 1, f"实际 {len(_rows_b)} 行：{_rows_b}")
_ids = {j.get("order_id") for _st, j in _results}
check("★ 两个请求拿到同一个 order_id（后到的被识别为重复）",
      len(_ids) == 1 and _ids == {_rows_b[0]["id"]} if _rows_b else False,
      f"响应 order_id={_ids} 库 id={[r['id'] for r in _rows_b]}")
check("其中一个响应带 is_duplicate 标记（前端可提示「已提交过」）",
      any(j.get("is_duplicate") for _st, j in _results),
      str([j.get("is_duplicate") for _st, j in _results]))

print("=== C. 部分唯一索引在位 ===")
_conn = backend.get_db()
_idx = _conn.execute("PRAGMA index_list(orders)").fetchall()
_conn.close()
_names = {r[1]: r for r in _idx}
check("索引 idx_orders_client_req 存在", "idx_orders_client_req" in _names, str(list(_names)))
check("它是 UNIQUE 索引", bool(_names.get("idx_orders_client_req")) and
      _names["idx_orders_client_req"][2] == 1, str(_names.get("idx_orders_client_req")))

print("=== D. 原单已取消 → 重新提交同一把钥匙仍能建新单 ===")
_old_id = j1["order_id"]
_conn = backend.get_db()
_conn.execute("UPDATE orders SET status = 'canceled' WHERE id = ?", (_old_id,))
# 子任务状态同步（父状态口径：聚合自子任务；本脚本只关心建单，不动聚合逻辑）
_conn.execute("UPDATE order_files SET status = 'canceled' WHERE order_id = ?", (_old_id,))
_conn.commit()
_conn.close()
st3, j3 = submit("key-A")
check("★ 取消后用同一把钥匙重新提交 → 建出**新单**（不是返回那条已取消的旧单）",
      st3 == 200 and j3.get("success") and j3.get("order_id") != _old_id
      and not j3.get("is_duplicate"), f"{st3} {str(j3)[:140]}")
_new_rows = [r for r in all_rows() if r["id"] == j3["order_id"]]
check("★ 新单换了幂等键（key-A-r…，因此不撞唯一索引），旧单的键原样保留",
      len(_new_rows) == 1 and _new_rows[0]["client_request_id"].startswith("key-A-r")
      and rows_for("key-A")[0]["id"] == _old_id,
      f"新单={_new_rows} / 旧键行={rows_for('key-A')}")

print("=== E. 超出 10 分钟窗口复用同一把钥匙 → 仍建新单（老语义保持）===")
st4, j4 = submit("key-E")
_old_e = j4["order_id"]
_conn = backend.get_db()
# 把原单时间挪到 20 分钟前（超出幂等窗口）
_conn.execute("UPDATE orders SET created_at = ? WHERE id = ?",
              ((backend.datetime.now() - backend.timedelta(minutes=20))
               .strftime("%Y-%m-%d %H:%M:%S"), _old_e))
_conn.commit()
_conn.close()
st5, j5 = submit("key-E")
check("★ 窗口外复用同一把钥匙 → 建新单（不能因为有了唯一索引就退化成返回旧单）",
      st5 == 200 and j5.get("success") and j5.get("order_id") != _old_e
      and not j5.get("is_duplicate"), f"{st5} {str(j5)[:140]}")
check("窗口外的新单也换了钥匙，原单保留", len(rows_for("key-E")) == 1, str(rows_for("key-E")))

print("=== F. 不带幂等键（空串）的多次提交互不影响 ===")
st6, j6 = submit("")
st7, j7 = submit("")
check("空串不参与唯一索引：两次都建了各自的单",
      st6 == 200 and st7 == 200 and j6.get("order_id") != j7.get("order_id")
      and not j6.get("is_duplicate") and not j7.get("is_duplicate"),
      f"{j6.get('order_id')} / {j7.get('order_id')}")
check("空幂等键不参与去重：两行都以空串落库（部分索引只约束非空键）",
      len(rows_for("")) == 2, str(rows_for("")))

print("=== G. 迁移幂等（重复 init_db 不炸、索引仍在）===")
_ok = True
try:
    backend.init_db()
    backend.init_db()
except Exception as e:  # noqa: BLE001
    _ok = False
    print("      异常：", repr(e))
_conn = backend.get_db()
_n = _conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index'"
                   " AND name='idx_orders_client_req'").fetchone()[0]
_conn.close()
check("重复迁移不报错且索引仍在", _ok and _n == 1, f"ok={_ok} n={_n}")

print("=== H. 去重迁移：库里已有重复键时也能把索引建起来 ===")
_conn = backend.get_db()
_conn.execute("DROP INDEX IF EXISTS idx_orders_client_req")
_dup_key = "key-H"
_conn.execute(
    """INSERT INTO orders (file, copies, status, created_at, openid, duplex, page_count,
                           price_per_page, total_price, is_free, order_number, source,
                           client_request_id)
       VALUES ('h.pdf', 1, 'queued', '2026-01-01 00:00:00', ?, 'on', 1, 0, 0, 0,
               'HN-TEST-H1', 'wechat', ?)""", (OID, _dup_key))
_first = _conn.execute("SELECT last_insert_rowid()").fetchone()[0]
_conn.execute(
    """INSERT INTO orders (file, copies, status, created_at, openid, duplex, page_count,
                           price_per_page, total_price, is_free, order_number, source,
                           client_request_id)
       VALUES ('h.pdf', 1, 'queued', '2026-01-01 00:00:01', ?, 'on', 1, 0, 0, 0,
               'HN-TEST-H2', 'wechat', ?)""", (OID, _dup_key))
_conn.commit()
_conn.close()
backend.init_db()          # 迁移：先去重、再建唯一索引
_rows_h = rows_for(_dup_key)
check("★ 重复键只保留最早一条（其余清空该列，订单本身不动）",
      len(_rows_h) == 1 and _rows_h[0]["id"] == _first, str(_rows_h))
check("★ 去重后唯一索引成功建立",
      backend.get_db().execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index'"
                               " AND name='idx_orders_client_req'").fetchone()[0] == 1)
_moved = [r for r in all_rows() if r["id"] != _first and r["client_request_id"] == ""]
check("另一条的 order_number 未被破坏（只是失去幂等键）",
      any(r["id"] == _first + 1 for r in _moved), str(_moved))

os.chdir(REPO)
sys.path.remove(DST)
shutil.rmtree(TMP, ignore_errors=True)

print("=" * 60)
print("提交幂等验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
