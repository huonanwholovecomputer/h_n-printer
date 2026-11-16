# -*- coding: utf-8 -*-
"""验收 · 移动端 Excel（.xls/.xlsx）打印：页数必须由本地打印工具按**打印同一引擎**分析

背景（2026-11-15 用户需求）：「移动端还不支持 Excel 表格类型？本地打印工具已经足够成熟了，
需要提供 Excel 支持，功能口径（页边距等）和本地打印工具一致。」

打印侧其实早就通了：后端白名单收 xls/xlsx，converter 把表格交给 xlsx_printer（Excel→WPS→LO）；
顾客云端上传固定走 LibreOffice（S1-04）。两条引擎都按**工作簿自身的页面设置**导出
（纸张/缩放/页边距/打印区域），云端订单从不传 print_options ⇒ 与本地打印工具默认口径一致。

真正缺的是「页数」：get_file_page_count 对 xls/xlsx 落到"默认 1 页"（假数）——
多页表格按 1 页少收费、页码区间校验永远按 1 页判；设备端分析函数也只认 doc/docx/pdf，
表格落到 else 直接回报 1 页。本脚本验收修复后的全链路：

  A. 静态接线：后端 6 处 + device 分析分支 + 两个前端（小程序 / APP）的扩展名清单；
  B. 行为（真 Flask + 真 SQLite）：xlsx 页数未知 → 1 页兜底计价但 files 保持 0/0 →
     补推判据命中 → /api/file_page 惰性补推 → 回报后金额与父单页数一起重算；
  C. 变异自证：把 xls/xlsx 从 get_file_page_count 的待验证元组里删掉 ⇒ A 的判据必须变红。

用法：PYTHONPATH= PYTHONUTF8=1 <python> tests/verify_mobile_excel_page_count.py  （exit 0 = 全绿）
"""
import io
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")
MP_INDEX = os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.js")
APP_PRINT = os.path.join(REPO, "mobile_apps", "android_app", "www", "print.js")
CLOUD_CLIENT = os.path.join(REPO, "local_print_tool", "cloud_client.py")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


TMP = tempfile.mkdtemp(prefix="hn_excel_pages_")
DST = os.path.join(TMP, "printer-backend")
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(
    "__pycache__", "config.py", "config.py.example",
    "orders.db", "users.db", "uploads", "logs", "*.db-wal", "*.db-shm"))
os.chdir(DST)
os.environ["FLASK_ENV"] = "development"
sys.path.insert(0, DST)

import app as backend  # noqa: E402

backend.init_db()

OID = "openid-excel-pages"
backend.ADMIN_OPENIDS.add(OID)
HDR = {"Authorization": "Bearer " + backend.token_serializer.dumps(OID)}
client = backend.app.test_client()


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线（后端 + 设备端 + 前端）===")
src = _read(os.path.join(DST, "app.py"))
cc = _read(CLOUD_CLIENT)
mp = _read(MP_INDEX)
ap = _read(APP_PRINT)

check("get_file_page_count 把 xls/xlsx 归入「等本地工具分析」",
      'if file_type in ("doc", "docx", "xls", "xlsx"):' in src)
check("同函数对上述类型返回 0（待验证）而非 1",
      "等待本地打印工具分析页数" in src and "return 0" in src)
check("上传成功后的补推条件含 xls/xlsx",
      'if page_count == 0 and ext_lower in ("doc", "docx", "xls", "xlsx")' in src)
check("/api/file_page 补推判据含 xls/xlsx",
      'or ext in (".doc", ".docx", ".xls", ".xlsx"))):' in src)
check("/api/file_page 服务端数页排除表含 xls/xlsx",
      'if ext and ext not in (".doc", ".docx", ".xls", ".xlsx"):' in src)
check("_pending_page_analysis_rows 的 SQL 覆盖 .xls/.xlsx",
      "LIKE '%.xls'" in src and "LIKE '%.xlsx'" in src)
check("_plan_order_files 的 verified 判定含 xls/xlsx",
      'page_count_verified = ext not in ("doc", "docx", "xls", "xlsx")' in src)
check("设备端页数分析分支含 xls/xlsx（与打印同一引擎）",
      'if ext in (".doc", ".docx", ".xls", ".xlsx"):' in cc)
check("小程序 supportedExts 收 .xls/.xlsx",
      "const supportedExts = ['.pdf', '.doc', '.docx', '.xls', '.xlsx'" in mp)
check("小程序 unsupportedExts 已剔除 .xls/.xlsx",
      "const unsupportedExts = ['.ppt', '.pptx', '.dwg', '.dxf']" in mp)
check("APP SUPPORTED_EXTS 收 .xls/.xlsx",
      "'.docx', '.xls', '.xlsx', '.txt'" in ap)
check("APP UNSUPPORTED_EXTS 已剔除 .xls/.xlsx",
      "const UNSUPPORTED_EXTS = ['.ppt', '.pptx', '.dwg', '.dxf'];" in ap)
# 页数可信闸门：表格与 Word 同属「必须本地分析」一类，未 verified 的页数不得被当成可信价
check("小程序 PAGE_COUNT_LOCAL_EXTS 含 xls/xlsx（未 verified 不可信）",
      "const PAGE_COUNT_LOCAL_EXTS = ['doc', 'docx', 'md', 'txt', 'csv', 'xls', 'xlsx']" in mp)
check("小程序 PAGE_COUNT_LOCAL_TYPE_LABELS 有表格文案",
      "xls: '表格', xlsx: '表格'" in mp)
check("APP PAGE_COUNT_LOCAL_EXTS 含 xls/xlsx（未 verified 不可信）",
      "const PAGE_COUNT_LOCAL_EXTS = ['doc', 'docx', 'md', 'txt', 'csv', 'xls', 'xlsx'];" in ap)
check("APP PAGE_COUNT_LOCAL_TYPE_LABELS 有表格文案",
      "xls: '表格', xlsx: '表格'" in ap)
check("小程序页数轮询的 needLocal 含表格（设备离线要提示而不是无限等待）",
      "const needLocal = fext === 'doc' || fext === 'docx' || fext === 'xls' || fext === 'xlsx'" in mp)
check("APP 页数轮询的 needLocal 含表格",
      "const needLocal = fext === 'doc' || fext === 'docx' || fext === 'xls' || fext === 'xlsx';" in ap)

# ============================================================
# B. 行为（真 Flask + 真 SQLite）
# ============================================================
print("\n=== B1. get_file_page_count：表格不再冒充 1 页 ===")
XLSX = os.path.abspath(os.path.join(backend.UPLOAD_DIR, "xlsx", "mobile-excel.xlsx"))
os.makedirs(os.path.dirname(XLSX), exist_ok=True)
with open(XLSX, "wb") as fh:
    fh.write(b"PK-fake-xlsx-fixture")
PDF = os.path.abspath(os.path.join(backend.UPLOAD_DIR, "pdf", "known.pdf"))
os.makedirs(os.path.dirname(PDF), exist_ok=True)
with open(PDF, "wb") as fh:
    fh.write(b"%PDF-1.4\n%%EOF\n")

check("磁盘上的 .xlsx → 0（等待本地工具）", backend.get_file_page_count(XLSX, "xlsx") == 0,
      str(backend.get_file_page_count(XLSX, "xlsx")))
check("只给类型不带文件 → 0", backend.get_file_page_count(None, "xls") == 0)
check("PDF 仍由服务端直接数页", backend.get_file_page_count(PDF, "pdf") >= 1)
check("图片仍按 1 页", backend.get_file_page_count(None, "jpg") == 1)


def _mk_file(fid, name, path, pages, verified, md5):
    with open(path, "wb") as fh:
        fh.write(b"fixture")
    conn = backend.get_db()
    conn.execute(
        """INSERT INTO files (id, original_name, saved_name, path, size, created_at,
                              page_count, page_count_verified, md5, openid)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (fid, name, name, path, os.path.getsize(path), "2026-11-15 21:00:00",
         pages, int(verified), md5, OID))
    conn.commit()
    conn.close()
    return fid


FID = _mk_file("excel-unknown", "成绩汇总表.xlsx", XLSX, 0, False, "md5-excel-unknown")

print("\n=== B2. 页数未知的表格：只兜底计价，不写回 files ===")
r = client.post("/api/submit_order", headers=HDR,
                json={"files": [{"file_id": FID, "copies": 2, "duplex": "on"}],
                      "duplex": "on", "client": "wechat"})
j = r.get_json() or {}
check("提交成功", r.status_code == 200 and j.get("success"), f"{r.status_code} {j}")
sub = (j.get("files") or [{}])[0]
conn = backend.get_db()
frow = conn.execute("SELECT page_count, page_count_verified FROM files WHERE id = ?", (FID,)).fetchone()
ofrow = conn.execute("SELECT page_count, total_price FROM order_files ORDER BY id DESC LIMIT 1").fetchone()
orow = conn.execute("SELECT page_count FROM orders ORDER BY id DESC LIMIT 1").fetchone()
conn.close()
expect_per_copy = backend.calculate_price(1, "on", "")
check("响应按 1 页兜底计价", sub.get("page_count") == 1 and sub.get("page_count_verified") is False, str(sub))
check("order_files 按 1 页 × 2 份计价",
      ofrow["page_count"] == 1 and abs(ofrow["total_price"] - round(expect_per_copy * 2, 2)) < 1e-6,
      f"{dict(ofrow)} 期望单份 {expect_per_copy}")
check("★ files.page_count 保持 0（兜底值不写回）", frow["page_count"] == 0, str(dict(frow)))
check("★ files.page_count_verified 保持 0", int(frow["page_count_verified"]) == 0, str(dict(frow)))
check("父订单按兜底页数记账", orow["page_count"] == 2, str(dict(orow)))

print("\n=== B3. 补推判据 + /api/file_page 惰性补推 ===")
conn = backend.get_db()
pending = {row["id"] for row in backend._pending_page_analysis_rows(conn)}
conn.close()
check("页数未知的 .xlsx 在待分析列表里", FID in pending, str(pending))

calls = []
_orig_req = backend.request_page_analysis


def _spy(file_id, file_name, prefer_client=""):
    calls.append((file_id, file_name))
    return True


backend.request_page_analysis = _spy
backend.is_printer_available = lambda: True
try:
    calls.clear()
    body = (client.get(f"/api/file_page/{FID}", headers=HDR).get_json() or {})
    check("表格轮询触发补推分析", len(calls) == 1 and calls[0][0] == FID, str(calls))
    check("响应如实回 page_count=0 / verified=false",
          body.get("page_count") == 0 and body.get("verified") is False, str(body))
finally:
    backend.request_page_analysis = _orig_req
    backend.is_printer_available = lambda: len(backend.get_claiming_device_ids()) > 0

print("\n=== B4. 页数回报后：金额与父单页数一起重算 ===")
conn = backend.get_db()
conn.execute("UPDATE files SET page_count = 4, page_count_verified = 1 WHERE id = ?", (FID,))
conn.commit()
conn.close()
backend._recalc_prices_for_file(FID, 4)
conn = backend.get_db()
ofrow = conn.execute("SELECT page_count, total_price FROM order_files ORDER BY id DESC LIMIT 1").fetchone()
orow = conn.execute("SELECT page_count, total_price FROM orders ORDER BY id DESC LIMIT 1").fetchone()
conn.close()
expect4 = round(backend.calculate_price(4, "on", "") * 2, 2)
check("子任务页数/金额按 4 页重算",
      ofrow["page_count"] == 4 and abs(ofrow["total_price"] - expect4) < 1e-6, str(dict(ofrow)))
check("★ 父订单总价与页数同步（4×2=8）",
      abs(orow["total_price"] - expect4) < 1e-6 and orow["page_count"] == 8, str(dict(orow)))

# ============================================================
# C. 变异自证
# ============================================================
print("\n=== C. 变异自证（判据必须能红）===")
mutated = src.replace('if file_type in ("doc", "docx", "xls", "xlsx"):',
                      'if file_type in ("doc", "docx"):', 1)
check("改动前判据为真", 'if file_type in ("doc", "docx", "xls", "xlsx"):' in src)
check("把 xls/xlsx 从待验证元组删掉后判据变红",
      'if file_type in ("doc", "docx", "xls", "xlsx"):' not in mutated)

print("\n" + ("=" * 60))
print("移动端 Excel 页数 · 验收: " + ("全部通过 ✅" if ok_all else "存在失败 ❌"))
sys.exit(0 if ok_all else 1)
