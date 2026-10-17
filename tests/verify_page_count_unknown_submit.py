# -*- coding: utf-8 -*-
"""验收 · 「Word 页数未分析完就提交」不得把页数永久冻结在 1 页（2026-10-17）

现场问题（2026-10-17 复盘）：
  顾客上传 docx（后端 files.page_count=0、page_count_verified=0）→ 页数还没回报就提交订单
  → `submit_order` 用 `get_file_page_count(...) or 1` 兜底成 1 页，**并把 1 写回 files.page_count**
  → 而所有补推页数分析的路径（接单设备上线补推、`/api/file_page` 惰性补推）都以
  `files.page_count = 0` 为条件 ⇒ 这个文件从此不会被送去分析：
    · page_count_verified 永远 0 → 价格永久停在 1 页（多页 Word 严重少收）；
    · 前端「复制价格」永远被拦（页数不可信）；
    · 「我的」订单永远显示 1 页。

本脚本用**真实 Flask 接口 + 真实 SQLite**（复制一份后端到临时目录）验收修复后的行为：
  A. 兜底计价保留：本单按 1 页计价（order_files.page_count=1 / total_price=1 页价），
     但 **files.page_count 保持 0、verified 保持 0**（绝不写回兜底值）。
  B. 补推判据 `_pending_page_analysis_rows()`：命中"页数未知的 docx"与"存量被冻结的 docx"，
     不命中"PDF 等上传时已由服务端数页的 verified=0 行"（避免每次上线白下载转换）。
  C. `/api/file_page` 惰性补推：设备在线时对 docx（page_count=0 **或** page_count>0 未验证）
     都补推分析；对 PDF 行不补推。
  D. 页数回报后：`_recalc_prices_for_file` 重算子任务金额**并同步父订单 page_count**
     （否则会出现「合计 1 页 / 金额却是 7 页价」）。
  E. 静态回归：submit_order 里不再出现 `get_file_page_count(...) or 1` 的写回写法。

用法: PYTHONPATH= PYTHONUTF8=1 <python> tests/verify_page_count_unknown_submit.py   （exit 0 = 全绿）
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


TMP = tempfile.mkdtemp(prefix="hn_pagecount_")
DST = os.path.join(TMP, "printer-backend")
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(
    "__pycache__", "config.py", "config.py.example",
    "orders.db", "users.db", "uploads", "logs", "*.db-wal", "*.db-shm"))
os.chdir(DST)
os.environ["FLASK_ENV"] = "development"
sys.path.insert(0, DST)

import app as backend  # noqa: E402

backend.init_db()

OID = "openid-pagecount"
backend.ADMIN_OPENIDS.add(OID)
HDR = {"Authorization": "Bearer " + backend.token_serializer.dumps(OID)}
client = backend.app.test_client()

DOCX = os.path.abspath(os.path.join(backend.UPLOAD_DIR, "docx", "unknown-pages.docx"))
PDF = os.path.abspath(os.path.join(backend.UPLOAD_DIR, "pdf", "known-5p.pdf"))
os.makedirs(os.path.dirname(DOCX), exist_ok=True)
os.makedirs(os.path.dirname(PDF), exist_ok=True)


def _mk_file(fid, name, path, pages, verified, md5):
    with open(path, "wb") as fh:
        fh.write(b"fixture")
    conn = backend.get_db()
    conn.execute(
        """INSERT INTO files (id, original_name, saved_name, path, size, created_at,
                              page_count, page_count_verified, md5, openid)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (fid, name, name, path, os.path.getsize(path), "2026-10-17 19:00:00",
         pages, int(verified), md5, OID))
    conn.commit()
    conn.close()
    return fid


# ① 顾客上传后页数未知的 docx（真实场景：Word 一律等本地工具回报）
FID = _mk_file("pc-docx-unknown", "第二章作业.docx", DOCX, 0, False, "md5-docx-unknown")
# ② 存量被历史兜底值冻结的 docx（page_count=1 但从未验证）
FID_FROZEN = _mk_file("pc-docx-frozen", "旧单.docx", DOCX, 1, False, "md5-docx-frozen")
# ③ PDF：上传时服务端直接数页，verified=0 只是"没经过本地工具"（不该被反复推分析）
FID_PDF = _mk_file("pc-pdf", "刊物.pdf", PDF, 5, False, "md5-pdf")

print("=== A. 页数未知时提交：只兜底计价，不写回 files.page_count ===")
r = client.post("/api/submit_order", headers=HDR,
                json={"files": [{"file_id": FID, "copies": 2, "duplex": "on"}],
                      "duplex": "on", "client": "wechat"})
j = r.get_json() or {}
check("提交成功", r.status_code == 200 and j.get("success"), f"{r.status_code} {j}")
sub = (j.get("files") or [{}])[0]
conn = backend.get_db()
frow = conn.execute("SELECT page_count, page_count_verified FROM files WHERE id = ?", (FID,)).fetchone()
ofrow = conn.execute("SELECT page_count, total_price, copies, duplex FROM order_files ORDER BY id DESC LIMIT 1").fetchone()
orow = conn.execute("SELECT page_count, total_price FROM orders ORDER BY id DESC LIMIT 1").fetchone()
conn.close()
expect_per_copy = backend.calculate_price(1, "on", "")
check("提交响应按 1 页兜底计价", sub.get("page_count") == 1 and sub.get("page_count_verified") is False,
      str(sub))
check("order_files 按 1 页 × 2 份计价",
      ofrow["page_count"] == 1 and abs(ofrow["total_price"] - round(expect_per_copy * 2, 2)) < 1e-6,
      f"{dict(ofrow)} 期望单份 {expect_per_copy}")
check("★ files.page_count 保持 0（不被兜底值覆盖）", frow["page_count"] == 0, str(dict(frow)))
check("★ files.page_count_verified 保持 0", int(frow["page_count_verified"]) == 0, str(dict(frow)))
check("父订单按兜底页数记账", orow["page_count"] == 2, str(dict(orow)))

print("\n=== B. 补推判据 _pending_page_analysis_rows() ===")
conn = backend.get_db()
pending = {row["id"] for row in backend._pending_page_analysis_rows(conn)}
conn.close()
check("页数未知的 docx 在待分析列表里", FID in pending, str(pending))
check("存量被冻结的 docx（page_count=1/verified=0）在待分析列表里（自愈）", FID_FROZEN in pending, str(pending))
check("PDF（服务端已数页，verified=0）不在待分析列表里", FID_PDF not in pending, str(pending))

print("\n=== C. /api/file_page 惰性补推 ===")
calls = []
_orig_req = backend.request_page_analysis


def _spy(file_id, file_name, prefer_client=""):
    calls.append((file_id, file_name))
    return True


backend.request_page_analysis = _spy
backend.is_printer_available = lambda: True
try:
    calls.clear()
    resp = client.get(f"/api/file_page/{FID}", headers=HDR)
    body = resp.get_json() or {}
    check("页数未知 docx：轮询触发补推分析",
          len(calls) == 1 and calls[0][0] == FID, str(calls))
    check("响应如实回 page_count=0 / verified=false",
          body.get("page_count") == 0 and body.get("verified") is False, str(body))
    calls.clear()
    resp = client.get(f"/api/file_page/{FID_FROZEN}", headers=HDR)
    check("存量冻结 docx：轮询也补推（自愈）", len(calls) == 1 and calls[0][0] == FID_FROZEN, str(calls))
    calls.clear()
    resp = client.get(f"/api/file_page/{FID_PDF}", headers=HDR)
    check("PDF：不补推（不白下载转换）", len(calls) == 0, str(calls))
finally:
    backend.request_page_analysis = _orig_req
    backend.is_printer_available = lambda: len(backend.get_claiming_device_ids()) > 0

print("\n=== D. 页数回报后：金额与页数一起回溯修正 ===")
conn = backend.get_db()
conn.execute("UPDATE files SET page_count = 7, page_count_verified = 1 WHERE id = ?", (FID,))
conn.commit()
conn.close()
backend._recalc_prices_for_file(FID, 7)
conn = backend.get_db()
ofrow = conn.execute("SELECT page_count, total_price, copies, duplex FROM order_files ORDER BY id DESC LIMIT 1").fetchone()
orow = conn.execute("SELECT page_count, total_price FROM orders ORDER BY id DESC LIMIT 1").fetchone()
conn.close()
expect7 = round(backend.calculate_price(7, "on", "") * 2, 2)
check("子任务页数 / 金额按 7 页重算",
      ofrow["page_count"] == 7 and abs(ofrow["total_price"] - expect7) < 1e-6,
      f"{dict(ofrow)} 期望 {expect7}")
check("★ 父订单总价跟着重算", abs(orow["total_price"] - expect7) < 1e-6, str(dict(orow)))
check("★ 父订单 page_count 同步为 7×2 = 14（不再停在兜底值）",
      orow["page_count"] == 14, str(dict(orow)))

print("\n=== E. 静态回归：兜底值不得再写回 files ===")
with open(os.path.join(DST, "app.py"), encoding="utf-8") as fh:
    src = fh.read()
check("submit_order 不再有 `get_file_page_count(...) or 1` 写回",
      'get_file_page_count(frow["path"], ext) or 1' not in src)
check("存在补推判据函数 _pending_page_analysis_rows",
      "def _pending_page_analysis_rows(" in src)
check("on_connect 使用统一判据（不再内联 page_count = 0 查询）",
      "pending = _pending_page_analysis_rows(conn)" in src)

print("\n" + ("=" * 60))
print("页数未知提交 · 验收: " + ("全部通过 ✅" if ok_all else "存在失败 ❌"))
sys.exit(0 if ok_all else 1)
