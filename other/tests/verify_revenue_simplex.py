# -*- coding: utf-8 -*-
"""营收/结算口径必须认识 simplex_pages（封面单独打印的页）。

审计结论 L1-3 / L1-4（2026-11-17）：
  · /api/admin/statistics/revenue 的子任务 SELECT 没取 simplex_pages，却把行丢给
    _file_range_stats(..., _row_simplex_pages(row)) —— 缺列安全返回 ""，于是「封面单独打印」
    的单按全双面摊纸，金额/张数都对不上（4 页里 1 页单面 = 2 张双面 + 1 张单面 = 3 张，
    老口径 ceil(4/2) = 2 张）。
  · 结算页要显示「算式 = 实收」，就必须拿到同一份摊纸结果；后端用 calculate_price
    （内部 _charge_units）算出的单价一并下发，前端不再自己再算一遍。

判据（改坏会红在哪）：
  A1 子任务 SELECT 里没有 simplex_pages            → 红（真实执行 200 但 files[] 缺键）
  A2 files[] 没有 simplex_pages / calc_price        → 红
  A3 单价不是 calculate_price 算的（自己再算一遍）  → 红
  B  真实 Flask：4 页 + simplex_pages="1" 的单，calc_price 必须是 2×DP+1×SP（≠ ceil 口径）
  C  结算页 _fileCalc：simplex_pages 必须进算式；给了后端 calc_price 就以下发值为准

跑法：
    PYTHONPATH= PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \
        C:/Users/Administrator/AppData/Local/Programs/Python/Python312/python.exe \
        other/tests/verify_revenue_simplex.py
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")
APP = os.path.join(BACKEND, "app.py")
PRICING = os.path.join(BACKEND, "pricing.json")
SETTLE = os.path.join(REPO, "local_print_tool", "finance", "settlement.html")
STATS = os.path.join(REPO, "local_print_tool", "stats_server.py")
NODE = shutil.which("node") or "node"

bad = 0


def chk(name, cond, detail=""):
    global bad
    if not cond:
        bad += 1
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + str(detail)) if (not cond and detail != "") else ""))


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


app_src = read(APP)
html = read(SETTLE)
pricing = json.loads(read(PRICING))
SP = float(pricing["simplex_price"])
DP = float(pricing["duplex_price"])
# 4 页、第 1 页单面：manual_duplex.charge_units 按**连续段**摊纸 —— [1] 单面段 1 张、
# [2,3,4] 双面段 1 张 + 尾张 1 张 ⇒ 1 张双面 + 2 张单面（不是「2 双面 + 1 单面」的
# heuristic：单面页会把双面段切断）。这正是前后端各算一套时对不上的地方。
EXPECT_4P_1S = round(1 * DP + 2 * SP, 2)      # 0.4 + 0.6 = 1.00
EXPECT_UNITS = (1, 2)
OLD_CEIL = round(2 * DP, 2)                   # 老口径 ceil(4/2) = 2 张双面


def func_body(src, name):
    """取 def name(...) 到下一个顶层 def / 文件尾（够用的粗切）。"""
    m = re.search(r"\ndef %s\([\s\S]*?(?=\ndef |\n@app\.route|\Z)" % re.escape(name), src)
    return m.group(0) if m else ""


# ============================================================================
print("=== A. 静态接线：营收 SQL / files[] 字段 / 单价来源 ===")
rev = func_body(app_src, "admin_statistics_revenue")
chk("A0 取到 admin_statistics_revenue 函数体", bool(rev.strip()))

# A1 子任务 SELECT 必须带 simplex_pages（就是这一行漏列导致金额按全双面算）。
# 判据语义是「子任务 SELECT 取到了 simplex_pages」，与列顺序无关 ⇒ 不锚列名前缀：
# 2026-11-19 给这行加了 file_id（结算页点文件名看缩略图），锚 "id, order_id, file_name,"
# 的旧写法会假红（契约没破，破的是判据写法）。
_sel_text = ""
_fm = rev.find("FROM order_files")
if _fm >= 0:
    _st = rev.rfind("SELECT", 0, _fm)
    if _st >= 0:
        _sel_text = rev[_st:_fm + len("FROM order_files")]
chk("A1 子任务 SELECT 含 simplex_pages（漏列 ⇒ _row_simplex_pages 永远拿空串）",
    bool(_sel_text) and "simplex_pages" in _sel_text, (_sel_text[:200] or "无 SELECT 匹配"))

# A2 files[] 必须把 simplex_pages 与后端算价一起下发（前端才算得出算式）
chk("A2 files[] 下发 simplex_pages（结算页 _fileCalc 依据它摊纸）",
    '"simplex_pages": ' in rev or '"simplex_pages":' in rev)
_calc_keys = [k for k in ('"calc_price"', '"calc_dup_sheets"', '"calc_single_sheets"') if k in rev]
chk("A2b files[] 下发 calculate_price 结果（calc_price / calc_dup_sheets / calc_single_sheets）",
    len(_calc_keys) == 3, _calc_keys)

# A3 单价必须走 calculate_price(… simplex_pages=…)，不许自己再写一遍公式
chk("A3 营收单价走 calculate_price 且传了 simplex_pages（唯一定价入口）",
    re.search(r"calculate_price\([\s\S]{0,300}?simplex_pages\s*=", rev) is not None)

# A4 张数摊纸必须走 _file_range_stats（v7.2 唯一口径），且把 simplex 传进去
chk("A4 张数走 _file_range_stats 并传入同一份 simplex（与 calculate_price 同源）",
    re.search(r"_row_simplex_pages\(of_row\)", rev) is not None
    and re.search(r"_file_range_stats\([\s\S]{0,240}?_sim\)", rev) is not None)

# ============================================================================
print("=== B. 真实 Flask：4 页 + 第 1 页单面 ===")

TMP = tempfile.mkdtemp(prefix="hn_revsimplex_")
DST = os.path.join(TMP, "printer-backend")
shutil.copytree(BACKEND, DST, ignore=shutil.ignore_patterns(
    "__pycache__", "config.py", "config.py.example", "orders.db", "users.db",
    "uploads", "logs", "*.db-wal", "*.db-shm"))
os.chdir(DST)
os.environ["FLASK_ENV"] = "development"
sys.path.insert(0, DST)

import app as backend  # noqa: E402

backend.init_db()
backend.PRINTER_TOKEN = "test-token"
HDR = {"X-Printer-Token": "test-token"}
conn = backend.get_db()
oid = conn.execute(
    "INSERT INTO orders (file, copies, status, created_at, openid, duplex, page_count,"
    " total_price, order_number, source, is_admin_print)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    ("x.pdf", 1, "sent", "2026-11-17 10:00:00", "openid-rev", "on", 4,
     EXPECT_4P_1S, "HN-REV-SIM-1", "wechat", 0)).lastrowid
conn.execute(
    "INSERT INTO order_files (order_id, file_name, copies, page_count, duplex, page_range,"
    " simplex_pages, total_price, status, created_at, is_free)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    (oid, "cover.pdf", 1, 4, "on", "", "1", EXPECT_4P_1S, "sent", "2026-11-17 10:00:00", 0))
conn.commit()
conn.close()

resp = backend.app.test_client().get(
    "/api/admin/statistics/revenue?start_date=2026-11-17&end_date=2026-11-17", headers=HDR)
body = resp.get_json() or {}
chk("B0 revenue 200 且 success", resp.status_code == 200 and body.get("success"), resp.status_code)
_orders = ((body.get("data") or {}).get("orders") or [])
_row_o = next((o for o in _orders if o.get("order_number") == "HN-REV-SIM-1"), None)
chk("B0b 找到订单明细", _row_o is not None, [o.get("order_number") for o in _orders])
_f = ((_row_o or {}).get("files") or [{}])[0] if _row_o else {}

chk("B1 文件行带 simplex_pages='1'（老代码：缺键 → 前端 _fileCalc 摊不出单面页）",
    _f.get("simplex_pages") == "1", _f.get("simplex_pages"))
chk("B2 calc_price == calculate_price()（1 张双面 + 2 张单面 = %.2f；ceil 口径是 %.2f）"
    % (EXPECT_4P_1S, OLD_CEIL), _f.get("calc_price") == EXPECT_4P_1S,
    (_f.get("calc_price"), EXPECT_4P_1S))
chk("B3 摊纸张数 = 3（1 双面张 + 2 单面张；老 _file_range_stats 缺 simplex ⇒ ceil(4/2)=2）",
    _f.get("sheets") == 3 and _f.get("total_sheets") == 3,
    (_f.get("sheets"), _f.get("total_sheets")))
chk("B4 有效页数 = 4、总页数 = 4", _f.get("effective_pages") == 4 and _f.get("total_pages") == 4,
    (_f.get("effective_pages"), _f.get("total_pages")))
chk("B5 已入账 = DB total_price（金额不改口径，改的是张数/算式与它同源）",
    round((_row_o or {}).get("revenue", 0) or 0, 2) == EXPECT_4P_1S,
    (_row_o or {}).get("revenue"))

# ============================================================================
print("=== C. 结算页 _fileCalc：simplex 进算式 / 后端算价优先 ===")

_m_start = html.find("function _chargeUnitsOf(")
_m_end = html.find("/* 单行复制", _m_start)
_js_funcs = html[_m_start:_m_end] if (_m_start >= 0 and _m_end > _m_start) else ""
chk("C0 取到 _chargeUnitsOf/_fileCalc 源码块", bool(_js_funcs.strip()), len(_js_funcs))

if _js_funcs.strip():
    harness = """
var _priceRules = function () {
  return { simplex_price: %s, duplex_price: %s };
};
function _filePages(f) {
  if (f && typeof f.effective_pages === 'number') return f.effective_pages;
  return (f && f.page_count) || 0;
}
%s
var out = {};
out.plain  = _fileCalc({ effective_pages: 4, copies: 1, duplex: 'on' });
out.simplex = _fileCalc({ effective_pages: 4, copies: 1, duplex: 'on', simplex_pages: '1' });
out.units  = _fileCalc({ effective_pages: 4, copies: 1, duplex: 'on', simplex_pages: '1',
                         calc_price: %s, calc_dup_sheets: %s, calc_single_sheets: %s });
out.server = _fileCalc({ effective_pages: 4, copies: 1, duplex: 'on', simplex_pages: '1',
                         calc_price: 2.50, calc_dup_sheets: %s, calc_single_sheets: %s });
console.log(JSON.stringify(out));
""" % (SP, DP, _js_funcs, EXPECT_4P_1S, EXPECT_UNITS[0], EXPECT_UNITS[1],
           EXPECT_UNITS[0], EXPECT_UNITS[1])
    _js_path = os.path.join(tempfile.gettempdir(), "hn_revenue_simplex.js")
    with open(_js_path, "w", encoding="utf-8") as fh:
        fh.write(harness)
    try:
        pr = subprocess.run([NODE, _js_path], capture_output=True, text=True, timeout=60)
        res = json.loads((pr.stdout or "").strip() or "{}")
    except Exception as e:  # noqa: BLE001
        res = {}
        print("    （node 跑不动：%s: %s）" % (type(e).__name__, e))
    chk("C1 无 simplex：4 页双面 = 2 张 × 双面价 = %.2f" % OLD_CEIL,
        abs((res.get("plain") or {}).get("cost", -1) - OLD_CEIL) < 1e-9, res.get("plain"))
    chk("C2 后端下发张数/单价时以它为准（1 张双面 + 2 张单面 = %.2f）" % EXPECT_4P_1S,
        abs((res.get("units") or {}).get("cost", -1) - EXPECT_4P_1S) < 1e-9, res.get("units"))
    chk("C2b 算式按后端摊纸写（'1张×0.40+2张×0.30'），不是前端 heuristic 的 '2张+1张'",
        "1张×" in ((res.get("units") or {}).get("formula") or "")
        and "2张×0.40+1张×0.30" != ((res.get("units") or {}).get("formula") or ""),
        res.get("units"))
    # 判别性用例：历史单价/人工改价时后端算价 2.50 ≠ 前端按当前价复算的 1.10。
    # 前端「自己再算一遍」的实现会给出 1.10 ⇒ 红；只有真的以下发值 2.50 为准才绿。
    chk("C3 后端下发 calc_price 时以它为准（历史单价 2.50 ≠ 前端复算 1.10，不许自己再算一遍）",
        abs((res.get("server") or {}).get("cost", -1) - 2.50) < 1e-9, res.get("server"))

print("=" * 52)
if bad:
    print("营收 simplex 口径验收：%d 条不过 ❌" % bad)
    sys.exit(1)
print("营收 simplex 口径验收：全部通过 ✅")
sys.exit(0)
