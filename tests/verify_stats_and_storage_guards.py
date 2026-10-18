# -*- coding: utf-8 -*-
"""验收 · 🟡-D5 第 7、8 条（后端两处守卫）

第 7 条 `/api/statistics/my`
    `year`/`month` 直接取自 query，原实现把值原样塞进 SQL 比较、末尾又 `int(year)`/`int(month)`
    ⇒ 任意已登录用户 `GET ?year=abc` 就能触发 **500**（`int()` ValueError）。
    修法：显式校验（年份 1970-2100、月份 1-12，允许 "7" 并归一），非法 → **400**（不静默回落到本月，
    那样会给用户看错月份的统计，比报错更糟）。

第 8 条 `/api/admin/storage` 的 DELETE
    旧实现把 UPLOAD_DIR 里的文件**全删**并 `UPDATE files SET path='' `，不看有没有在飞/预约子任务
    —— 预约单的文件到点前一直躺在服务器上，清空后到点必然「文件不存在」，订单永久打不出来，
    而且连二次确认都没有。修法：默认 **409 拒绝**（列出未完成子任务数与样本订单号），
    确实要清就显式 `?force=1`（管理员的第二道确认），响应里如实回传波及了多少未完成子任务。

用法: python tests/verify_stats_and_storage_guards.py     （exit 0 = 全绿）
"""
import io
import os
import shutil
import sys
import tempfile

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


TMP = tempfile.mkdtemp(prefix="hn_guard_")
DST = os.path.join(TMP, "printer-backend")
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(
    "__pycache__", "config.py", "config.py.example",
    "orders.db", "users.db", "uploads", "logs", "*.db-wal", "*.db-shm"))
os.chdir(DST)
os.environ["FLASK_ENV"] = "development"
sys.path.insert(0, DST)

import app as backend            # noqa: E402

backend.init_db()

ADMIN = "openid-admin-guard"
USER = "openid-user-guard"
backend.ADMIN_OPENIDS.add(ADMIN)


def HDR(openid):
    return {"Authorization": "Bearer " + backend.token_serializer.dumps(openid)}


client = backend.app.test_client()


def _mk_order_file(status="queued", openid=USER, fid="guard-file-1"):
    """造一个真实存在的上传文件 + files 行 + 订单/子任务行，返回 (path, order_number)。"""
    sub = os.path.join(backend.UPLOAD_DIR, "pdf")
    os.makedirs(sub, exist_ok=True)
    path = os.path.abspath(os.path.join(sub, fid + ".pdf"))
    with open(path, "wb") as fh:
        fh.write(b"%PDF-1.4\n% guard fixture\n" * 20)
    conn = backend.get_db()
    conn.execute(
        """INSERT OR REPLACE INTO files (id, original_name, saved_name, path, size, created_at,
                                        page_count, page_count_verified, md5, openid)
           VALUES (?, ?, ?, ?, ?, ?, 3, 1, ?, ?)""",
        (fid, fid + ".pdf", fid + ".pdf", path, os.path.getsize(path),
         "2026-06-01 12:00:00", "md5-" + fid, openid))
    conn.execute(
        """INSERT INTO orders (file, copies, status, created_at, openid, duplex, page_count,
                               price_per_page, total_price, is_free, order_number, source)
           VALUES (?, 1, 'queued', '2026-06-01 12:00:00', ?, 'on', 3, 0.3, 0.9, 0, ?, 'wechat')""",
        (fid + ".pdf", openid, "HN-TEST-G1"))
    oid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO order_files (order_id, file_id, file_name, copies, page_count,
                                    price_per_page, total_price, is_free, status, created_at)
           VALUES (?, ?, ?, 1, 3, 0.3, 0.9, 0, ?, '2026-06-01 12:00:00')""",
        (oid, fid, fid + ".pdf", status))
    conn.commit()
    conn.close()
    return path, "HN-TEST-G1"


print("=== ⑦ /api/statistics/my：参数非法必须 400（改前 500）===")
_path, _num = _mk_order_file(status="queued")
for q, tag in (("?year=abc", "year 非数字"), ("?year=99", "year 越界"), ("?month=13", "month 越界"),
               ("?month=0", "month 为 0"), ("?month=", "month 为空"), ("?year=2026&month=x", "month 非数字")):
    r = client.get("/api/statistics/my" + q, headers=HDR(USER))
    check(f"★ {tag}（{q}）→ 400（改前 int() 抛错 500）",
          r.status_code == 400, f"HTTP {r.status_code} {str(r.get_json())[:80]}")
    check(f"   {tag}：响应是明确的参数错误提示",
          (r.get_json() or {}).get("success") is False
          and "参数" in ((r.get_json() or {}).get("message") or ""),
          str(r.get_json())[:100])

r_ok = client.get("/api/statistics/my?year=2026&month=6", headers=HDR(USER))
j_ok = r_ok.get_json() or {}
check("合法参数 → 200，且 year/month 回显为整数", r_ok.status_code == 200
      and j_ok.get("year") == 2026 and j_ok.get("month") == 6, str(j_ok)[:120])
check("统计口径未回归（该月有 1 单、3 页）",
      j_ok.get("stats", {}).get("total_orders") == 1
      and j_ok.get("stats", {}).get("total_pages") == 3, str(j_ok.get("stats")))

r_nz = client.get("/api/statistics/my?year=2026&month=07", headers=HDR(USER))
check("不带前导零/带前导零都归一（'7' 与 '07' 等价）",
      r_nz.status_code == 200 and (r_nz.get_json() or {}).get("month") == 7,
      str((r_nz.get_json() or {}).get("month")))

r_def = client.get("/api/statistics/my", headers=HDR(USER))
check("不带参数仍按当前年月（默认值路径不受影响）", r_def.status_code == 200,
      f"HTTP {r_def.status_code}")

print("\n=== ⑧ /api/admin/storage DELETE：有未完成子任务时默认拒绝且**不删文件** ===")
r_del = client.delete("/api/admin/storage", headers=HDR(ADMIN))
j_del = r_del.get_json() or {}
check("★ 有 queued 子任务 → 409 + code=storage_in_use",
      r_del.status_code == 409 and j_del.get("code") == "storage_in_use",
      f"HTTP {r_del.status_code} {str(j_del)[:160]}")
check("★ 拒绝时**文件真的还在**（这是本条的因果判据）", os.path.isfile(_path), _path)
check("响应如实报出未完成子任务数与状态分布",
      j_del.get("live_tasks") == 1 and j_del.get("live_by_status", {}).get("queued") == 1,
      str({k: j_del.get(k) for k in ("live_tasks", "live_by_status", "samples")}))
check("样本里带上具体订单号（管理员知道是哪些单）",
      "HN-TEST-G1" in (j_del.get("samples") or []), str(j_del.get("samples")))
check("files.path 未被清空（拒绝路径不写库）",
      backend.get_db().execute("SELECT path FROM files WHERE id = ?",
                               ("guard-file-1",)).fetchone()["path"] == _path)

print("\n=== ⑧b 显式 ?force=1 → 放行，但如实回传波及面 ===")
r_force = client.delete("/api/admin/storage?force=1", headers=HDR(ADMIN))
j_force = r_force.get_json() or {}
check("强制清空 → 200", r_force.status_code == 200 and j_force.get("success") is True,
      f"HTTP {r_force.status_code} {str(j_force)[:140]}")
check("★ 响应回传被波及的未完成子任务数（后果可见）", j_force.get("live_tasks_deleted") == 1,
      str(j_force))
check("强制路径确实删了文件", not os.path.isfile(_path), _path)
check("files.path 已被清空（强制路径的执行结果）",
      backend.get_db().execute("SELECT path FROM files WHERE id = ?",
                               ("guard-file-1",)).fetchone()["path"] == "")

print("\n=== ⑧c 全部已完成 → 正常清空（零回归）===")
# 先把前面留下的未完成子任务标成已完成（模拟"订单都打完了"），否则仍会被守卫拦住
_conn = backend.get_db()
_conn.execute("UPDATE order_files SET status = 'sent'")
_conn.commit()
_conn.close()
_path2, _ = _mk_order_file(status="sent", fid="guard-file-2")
r_ok2 = client.delete("/api/admin/storage", headers=HDR(ADMIN))
j_ok2 = r_ok2.get_json() or {}
check("没有未完成子任务时 DELETE 正常成功",
      r_ok2.status_code == 200 and j_ok2.get("success") is True, f"{r_ok2.status_code} {str(j_ok2)[:120]}")
check("已完成任务的文件被清掉", not os.path.isfile(_path2), _path2)
check("已完成路径不报「波及未完成」", j_ok2.get("live_tasks_deleted") == 0, str(j_ok2))

print("\n=== ⑧d 权限回归 ===")
r_403 = client.delete("/api/admin/storage", headers=HDR(USER))
check("普通用户 DELETE → 403（未因本次改动放宽）", r_403.status_code == 403,
      f"HTTP {r_403.status_code}")

print("\n=== ⑨ 代理白名单：订单状态订正回执的 4 个字段原样透传（F6，2026-10-18）===")
# `/api/proxy/admin/order_status` 这一跳走 stats_server._extract_proxy_body 的白名单；
# 旧白名单只放 {success,message,data,pricing,devices,...} ⇒ 新端点的
# order_id/status/previous_status/files_updated 被**静默剥掉**（前端目前只读 success/message
# 所以今天不坏，但代理这跳无守卫覆盖）。这里用**真实函数 + 假 resp 对象**钉住透传。
try:
    _lpt = os.path.join(REPO, "local_print_tool")
    sys.path.insert(0, _lpt)
    import stats_server as _ss                                          # noqa: E402

    class _FakeResp:
        def __init__(self, payload, code=200):
            self._payload = payload
            self.status_code = code

        def json(self):
            if isinstance(self._payload, Exception):
                raise self._payload
            return self._payload

    _full = {"files_updated": 1, "message": "订单状态已订正", "order_id": "12",
             "previous_status": "abandoned", "status": "sent", "success": True}
    _out = _ss._extract_proxy_body(_FakeResp(_full))
    check("★ 代理透传 order_id/status/previous_status/files_updated（改前这 4 个被白名单剥掉）",
          _out.get("order_id") == "12" and _out.get("status") == "sent"
          and _out.get("previous_status") == "abandoned" and _out.get("files_updated") == 1,
          str(_out))
    check("  原有白名单字段未回归（success/message 照旧透传）",
          _out.get("success") is True and _out.get("message") == "订单状态已订正", str(_out))
    check("  字段**原样**透传，不做字符串化、也不把 0 当缺失（整数 id 与 files_updated=0 都保留）",
          _ss._extract_proxy_body(_FakeResp({"success": True, "order_id": 12, "status": "failed",
                                             "previous_status": "queued",
                                             "files_updated": 0})).get("order_id") == 12
          and _ss._extract_proxy_body(_FakeResp({"success": True,
                                                 "files_updated": 0})).get("files_updated") == 0,
          str(_ss._extract_proxy_body(_FakeResp({"success": True, "order_id": 12,
                                                 "files_updated": 0}))))
    _min = _ss._extract_proxy_body(_FakeResp({"success": True, "message": "ok"}))
    check("  字段缺失时不凭空补（「有才透传」，沿用既有白名单语义）",
          not any(k in _min for k in ("order_id", "status", "previous_status", "files_updated")),
          str(_min))
    _leak = _ss._extract_proxy_body(_FakeResp({"success": True, "token": "secret", "openid": "x"}))
    check("  白名单外字段仍被剥掉（没有退化成整体透传）",
          "token" not in _leak and "openid" not in _leak, str(_leak))
    _err = _ss._extract_proxy_body(_FakeResp(RuntimeError("boom"), 502))
    check("  非 JSON 响应仍转通用错误且带上 HTTP 码（错误路径零回归）",
          _err.get("success") is False and "502" in (_err.get("message") or ""), str(_err))
except Exception as _e:                                                 # noqa: BLE001
    check("⑨ 可跑（真实 stats_server 可导入 + 函数可取）", False, repr(_e))

os.chdir(REPO)
sys.path.remove(DST)
shutil.rmtree(TMP, ignore_errors=True)

print("=" * 60)
print("统计参数 / 存储删除守卫验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
