# -*- coding: utf-8 -*-
"""验收 · v7.2：结算页「页数 / 张数 / 总页数 / 趋势-纸张」改**有效口径**（按页码范围折算）

现场（2026-10-22）：订单 HN20261022-0007 的 7 个 Word 文件都是「2 页、单面、1 份、
页码范围 1」（只打第 1 页，实收 ¥0.30/个）。结算页却显示「页数 2页 (1)」「张数 2张」，
订单行「页数 14 / 张数 14」—— 而真实出纸是 7 张。钱是对的、纸是对的，只有这三个数偏高：
展示口径按**整份页数**算，没扣页码范围。

本脚本钉住修复后的口径（**折算只在后端算，页面只展示** —— 两份解析必然漂移）：
  A) 云端 `/api/admin/statistics/revenue`（真实 Flask + 真实 SQLite）：
     文件级 effective_pages/sheets/total_pages/total_sheets 与订单级
     total_page_count/total_sheet_count 全部按范围折算；revenue_summary.total_pages 同步。
  B) 本地 `/api/local/orders`（真实 sqlite 离线库 + 真实 handler）：同一套字段、同一套算法
     （复用 printer_config._count_pages_in_range，**不得**另写一份解析）。
  C) 静态接线：settlement.html 只消费后端字段；订单行/KPI/趋势/来源/成员统计都走
     _orderPages / _orderSheets；页面内**不许**出现第二份页码范围解析。
  D) 实跑（Node VM 里加载页面里那四个小函数）：有后端字段时按字段显示；字段缺失时退回整份口径（降级）。
  E) 变异自证：把折算改回整份 → A/D 必红；把页面改回读 page_count → D 必红。

用法：python tests/verify_effective_pages_settlement.py      （exit 0 = 全部通过）
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

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")
LOCAL = os.path.join(REPO, "local_print_tool")
SETTLEMENT = os.path.join(LOCAL, "finance", "settlement.html")
STATS_SERVER = os.path.join(LOCAL, "stats_server.py")

bad = 0


def chk(name, cond, detail=""):
    global bad
    if not cond:
        bad += 1
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + detail) if (not cond and detail) else ""))


# ============================================================
# A. 云端 revenue 接口（真实 Flask + SQLite）
# ============================================================
print("=== A. 云端 revenue：页数/张数按页码范围折算 ===")

TMP = tempfile.mkdtemp(prefix="hn_effpages_")
DST = os.path.join(TMP, "printer-backend")
shutil.copytree(BACKEND, DST, ignore=shutil.ignore_patterns(
    "__pycache__", "config.py", "config.py.example",
    "orders.db", "users.db", "uploads", "logs", "*.db-wal", "*.db-shm"))
_cwd = os.getcwd()
os.chdir(DST)
os.environ["FLASK_ENV"] = "development"
sys.path.insert(0, DST)
import app as backend  # noqa: E402
# 刻意**不** chdir 回仓库：app 里 DATABASE/USER_DATABASE/devices.json/uploads 都是相对
# CWD 的名字（get_db 懒连接、devices.json 写入时才落盘）⇒ 中途切回仓库就会把测试数据
# 写进仓库根目录（实测留下过 orders.db / devices.json / uploads/）。整段测试都在临时副本里跑。
backend.DATABASE = os.path.join(DST, "orders.db")
backend.USER_DATABASE = os.path.join(DST, "users.db")

backend.init_db()
backend.PRINTER_TOKEN = "test-token"
client = backend.app.test_client()
HDR = {"X-Printer-Token": "test-token"}

# 三个文件覆盖三种典型：
#   f1 2 页单面只打第 1 页（现场那条）      → 有效 1 页 / 1 张
#   f2 3 页单面整份、2 份                  → 有效 3 页 / 3 张，合计 6 页 / 6 张
#   f3 5 页双面、只打 2-3（2 页 → 1 张）    → 有效 2 页 / 1 张
CASES = [
    # (file_name, page_count, copies, duplex, page_range, eff, sheets, tp, ts)
    ("a.doc", 2, 1, "off", "1", 1, 1, 1, 1),
    ("b.doc", 3, 2, "off", "", 3, 3, 6, 6),
    ("c.doc", 5, 1, "on", "2-3", 2, 1, 2, 1),
]
EXPECT_PAGES = sum(c[7] for c in CASES)    # 9
EXPECT_SHEETS = sum(c[8] for c in CASES)   # 8
NAIVE_PAGES = sum(c[1] * c[2] for c in CASES)   # 整份口径 2*1 + 3*2 + 5*1 = 13（必须与 9 不同，否则判据不判别）

conn = backend.get_db()
cur = conn.execute(
    "INSERT INTO orders (file, copies, status, created_at, openid, duplex, page_count,"
    " total_price, order_number, source, is_admin_print)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    ("a.doc", 1, "sent", "2026-10-22 10:00:00", "openid-eff", "off", 2,
     3.60, "HN20261022-9001", "local", 0))
OID = cur.lastrowid
for (fn, pc, cp, dx, pr, _e, _s, _tp, _ts) in CASES:
    conn.execute(
        "INSERT INTO order_files (order_id, file_name, copies, page_count, total_price,"
        " status, created_at, page_range, duplex)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (OID, fn, cp, pc, 1.20, "sent", "2026-10-22 10:00:00", pr, dx))
conn.commit()
conn.close()

r = client.get("/api/admin/statistics/revenue?start_date=2026-10-22&end_date=2026-10-22",
               headers=HDR)
chk("revenue 接口可调用（HTTP 200）", r.status_code == 200, "rc=%s" % r.status_code)
data = (r.get_json() or {}).get("data") or {}
orders = data.get("orders") or []
mine = [o for o in orders if o.get("order_number") == "HN20261022-9001"]
chk("能取到该订单", len(mine) == 1, "orders=%d" % len(orders))

if mine:
    o = mine[0]
    files = {f["file_name"]: f for f in o.get("files") or []}
    for (fn, pc, cp, dx, pr, eff, sh, tp, ts) in CASES:
        f = files.get(fn) or {}
        chk("文件 %s 下发有效字段（eff/sheets/tp/ts = %d/%d/%d/%d）" % (fn, eff, sh, tp, ts),
            (f.get("effective_pages"), f.get("sheets"), f.get("total_pages"),
             f.get("total_sheets")) == (eff, sh, tp, ts),
            "实际 %r / page_count=%r range=%r" % (
                (f.get("effective_pages"), f.get("sheets"), f.get("total_pages"),
                 f.get("total_sheets")), f.get("page_count"), f.get("page_range")))
    chk("★ 订单行页数 = 折范围后的 Σ 有效页（%d，不是整份 %d）" % (EXPECT_PAGES, NAIVE_PAGES),
        o.get("total_page_count") == EXPECT_PAGES,
        "实际 %r" % (o.get("total_page_count"),))
    chk("★ 订单行张数 = 折范围后的 Σ 有效张（%d）" % EXPECT_SHEETS,
        o.get("total_sheet_count") == EXPECT_SHEETS,
        "实际 %r" % (o.get("total_sheet_count"),))
    chk("★ 判据可判别：整份口径会算出不同的数（%d ≠ %d）" % (NAIVE_PAGES, EXPECT_PAGES),
        NAIVE_PAGES != EXPECT_PAGES)

    rs = data.get("revenue_summary") or {}
    chk("KPI 汇总 total_pages 同步为有效页数（%d）" % EXPECT_PAGES,
        rs.get("total_pages") == EXPECT_PAGES, "实际 %r" % (rs.get("total_pages"),))
    chk("按来源页数 local_pages 同步为有效页数（%d）" % EXPECT_PAGES,
        rs.get("local_pages") == EXPECT_PAGES, "实际 %r" % (rs.get("local_pages"),))
    by_user = data.get("by_user") or []
    mine_u = [u for u in by_user if u.get("openid") == "openid-eff"]
    chk("按成员页数同步为有效页数（%d）" % EXPECT_PAGES,
        bool(mine_u) and mine_u[0].get("total_pages") == EXPECT_PAGES,
        "实际 %r" % ([u.get("total_pages") for u in mine_u],))

# ============================================================
# B. 本地 /api/local/orders（真实离线库 + 真实 handler）
# ============================================================
print("\n=== B. 本地订单库：同一套字段与算法 ===")
sys.path.insert(0, LOCAL)
import stats_server  # noqa: E402

LOCAL_DB = os.path.join(TMP, "printer-local.db")
_lc = __import__("sqlite3").connect(LOCAL_DB)
_lc.execute("""CREATE TABLE offline_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_number TEXT NOT NULL UNIQUE,
    files_json TEXT NOT NULL, total_price REAL NOT NULL, created_at TEXT NOT NULL,
    owner_name TEXT DEFAULT '', is_admin_print INTEGER DEFAULT 1,
    synced INTEGER DEFAULT 0, remark TEXT DEFAULT '')""")
_local_files = [{
    "file_name": fn, "copies": cp, "page_count": pc, "cost": 1.2,
    "duplex": dx, "page_range": pr,
} for (fn, pc, cp, dx, pr, _e, _s, _tp, _ts) in CASES]
_lc.execute(
    "INSERT INTO offline_orders (order_number, files_json, total_price, created_at,"
    " owner_name, is_admin_print, synced) VALUES (?, ?, ?, ?, ?, ?, ?)",
    ("HN20261022-9002", json.dumps(_local_files), 3.6, "2026-10-22 10:00:00", "本机", 1, 0))
_lc.commit()
_lc.close()

_saved = {}


class _FakeHandler:
    def __init__(self):
        class _S:
            def json(self):  # noqa: D401
                return _saved
        self._s = _S()
        self._status = None

    def _send_json(self, payload, status=200):
        _saved.clear()
        _saved.update(payload)
        self._status = status


_orig_db = stats_server._LOCAL_DB_FILE
stats_server._LOCAL_DB_FILE = LOCAL_DB
try:
    h = _FakeHandler()
    stats_server._StatsHandler._handle_local_orders(h, "")
finally:
    stats_server._LOCAL_DB_FILE = _orig_db

lorders = ((_saved.get("data") or {}).get("orders") or [])
lchk = [o for o in lorders if o.get("order_number") == "HN20261022-9002"]
chk("本地订单可取到", len(lchk) == 1, "orders=%r" % (lorders[:1],))
if lchk:
    lo = lchk[0]
    lfiles = {f["file_name"]: f for f in lo.get("files") or []}
    for (fn, pc, cp, dx, pr, eff, sh, tp, ts) in CASES:
        f = lfiles.get(fn) or {}
        chk("本地文件 %s 有效字段一致（%d/%d/%d/%d）" % (fn, eff, sh, tp, ts),
            (f.get("effective_pages"), f.get("sheets"), f.get("total_pages"),
             f.get("total_sheets")) == (eff, sh, tp, ts),
            "实际 %r" % ((f.get("effective_pages"), f.get("sheets"),
                          f.get("total_pages"), f.get("total_sheets")),))
    chk("★ 本地订单 total_page_count = %d（与云端同口径）" % EXPECT_PAGES,
        lo.get("total_page_count") == EXPECT_PAGES, "实际 %r" % (lo.get("total_page_count"),))
    chk("★ 本地订单 total_sheet_count = %d" % EXPECT_SHEETS,
        lo.get("total_sheet_count") == EXPECT_SHEETS, "实际 %r" % (lo.get("total_sheet_count"),))

_src = open(STATS_SERVER, encoding="utf-8").read()
chk("本地侧复用 printer_config 的权威解析（不另写一份）",
    "from printer_config import _count_pages_in_range" in _src
    and not re.search(r"(?m)^[ \t]*def _parse_range_parts\s*\(", _src))

# ============================================================
# C. 静态接线（结算页只展示、不折算）
# ============================================================
print("\n=== C. 结算页静态接线 ===")
html = open(SETTLEMENT, encoding="utf-8").read()


def _fn_body(name, src=None):
    """抽出顶层 function 定义体：行首 `function name(` 到其配对的行首 `}`（含右花括号）。"""
    s = html if src is None else src
    m = re.search(r"(?ms)^function %s\([^)]*\) \{\n.*?^\}" % re.escape(name), s)
    return (m.group(0) if m else "")


_fp = _fn_body("_filePages")
_fs = _fn_body("_fileSheets")
_op = _fn_body("_orderPages")
_os = _fn_body("_orderSheets")
chk("_filePages 读后端 effective_pages", "effective_pages" in _fp, _fp[:80])
chk("_fileSheets 优先读后端 total_sheets", "total_sheets" in _fs, _fs[:80])
chk("_orderPages 读后端 total_page_count", "total_page_count" in _op, _op[:80])
chk("_orderSheets 优先读后端 total_sheet_count", "total_sheet_count" in _os, _os[:80])

chk("订单行「页数」走 _orderPages(o)",
    "_orderPages(o)" in html and re.search(r"<td title=\"有效打印页数[^\"]*\">' \+ \(_orderPages\(o\)", html) is not None)
chk("订单行「张数」走 _orderSheets(o)（标题写明已折范围）",
    re.search(r"有效纸张张数（按页码范围折算", html) is not None
    and "_osheets = _orderSheets(o)" in html)
chk("文件行「页数」用有效页数 + 括号注「共 N 页」",
    "_effPg = _filePages(f)" in html and "(共" in html and "_totPg" in html)
chk("文件行「张数」用 _fileSheets(f)（读后端值）", "+ _fileSheets(f) + '张" in html)

# 所有「页数」聚合都走同一个入口（订单级），不得再有 o.total_page_count || 0 的散装读法
chk("★ 页面内不再散装读整份页数（o.total_page_count || 0 已清零）",
    "o.total_page_count || 0" not in html)
chk("★ KPI/趋势/来源/成员聚合均走 _orderPages（≥4 处）",
    html.count("_orderPages(o)") >= 4, "实际 %d 处" % html.count("_orderPages(o)"))

# 页面内**不许**出现第二份页码范围解析（口径只能有一处：后端）
_RANGE_NAMES = ("countPagesInRange", "_countRangePages", "countRangePages",
                "_parse_page_range", "_parse_range_parts", "_count_pages_in_range",
                "_countRangePages", "_countPagesInRange")
_leak = [n for n in _RANGE_NAMES if re.search(r"function\s+%s\s*\(" % re.escape(n), html)]
chk("★ 页面内没有第二份页码范围解析（否则两份口径必漂移）", not _leak, "发现 %r" % (_leak,))

# ============================================================
# D. 实跑（Node VM 加载页面里那四个小函数）
# ============================================================
print("\n=== D. 实跑（Node）===")
if not (_fp and _fs and _op and _os):
    chk("能从结算页抽出 _filePages/_fileSheets/_orderPages/_orderSheets", False)
else:
    probe = os.path.join(TMP, "probe.js")
    open(probe, "w", encoding="utf-8").write(
        _fp + "\n" + _fs + "\n" + _op + "\n" + _os + r"""

function fileRanged()  { return {page_count: 2, copies: 1, duplex: 'off', page_range: '1', effective_pages: 1, sheets: 1, total_pages: 1, total_sheets: 1}; }
function fileLegacy()  { return {page_count: 2, copies: 1, duplex: 'off', page_range: '1'}; }          // 后端未给折算值
function fileNoRange() { return {page_count: 3, copies: 2, duplex: 'off', page_range: '', effective_pages: 3, sheets: 3, total_pages: 6, total_sheets: 6}; }
function orderRanged() { return {total_page_count: 9, total_sheet_count: 8, files: [fileRanged(), fileNoRange()]}; }
function orderLegacy() { return {files: [fileRanged(), fileNoRange()]}; }
out = {
  pagesRanged: _filePages(fileRanged()),          // 1
  sheetsRanged: _fileSheets(fileRanged()),        // 1
  pagesLegacy: _filePages(fileLegacy()),          // 降级 → 2（整份）
  sheetsLegacy: _fileSheets(fileLegacy()),        // 降级 → 2
  sheetsNoRange: _fileSheets(fileNoRange()),      // 6
  orderPages: _orderPages(orderRanged()),         // 9
  orderSheets: _orderSheets(orderRanged()).total, // 8
  orderSheetsSingle: _orderSheets(orderRanged()).single,  // 1 + 6 = 7（都单面）
  orderSheetsDuplex: _orderSheets(orderRanged()).duplex,  // 0
  legacyPages: _orderPages(orderLegacy()),        // 回算 1 + 6 = 7
  legacySheets: _orderSheets(orderLegacy()).total // 回算 1 + 6 = 7
};
process.stdout.write(JSON.stringify(out));
""")
    rj = subprocess.run(["node", probe], capture_output=True, text=True, encoding="utf-8")
    if rj.returncode != 0:
        chk("Node 探针可运行", False, (rj.stderr or "")[:300])
    else:
        got = json.loads(rj.stdout)
        chk("有后端字段：文件页数/张数按折算值（1 / 1）",
            (got["pagesRanged"], got["sheetsRanged"]) == (1, 1), repr(got))
        chk("字段缺失：退回整份口径（2 / 2，降级显示）",
            (got["pagesLegacy"], got["sheetsLegacy"]) == (2, 2), repr(got))
        chk("整份 3 页 2 份单面 → 6 张", got["sheetsNoRange"] == 6, repr(got))
        chk("★ 订单级页数/张数用后端值（9 / 8）",
            (got["orderPages"], got["orderSheets"]) == (9, 8), repr(got))
        chk("订单级单双面拆分按文件累计（单面 7 / 双面 0）",
            (got["orderSheetsSingle"], got["orderSheetsDuplex"]) == (7, 0), repr(got))
        chk("订单级字段缺失：用文件行回算（7 / 7）",
            (got["legacyPages"], got["legacySheets"]) == (7, 7), repr(got))

# ============================================================
# E. 变异自证（改回旧口径必须打红）
# ============================================================
print("\n=== E. 变异自证 ===")
# E1 后端：整份口径公式（旧代码）算出来的数必须与本轮期望不同
_naive = sum(c[1] * c[2] for c in CASES)
_naive_sheets = sum((((c[1] + 1) // 2) if c[3] == "on" else c[1]) * c[2] for c in CASES)
chk("★ 变异 ①：整份口径的订单页数/张数（%d / %d）与有效口径（%d / %d）不同"
    % (_naive, _naive_sheets, EXPECT_PAGES, EXPECT_SHEETS),
    (_naive, _naive_sheets) != (EXPECT_PAGES, EXPECT_SHEETS))

# E2 后端源码里不许再出现「Σ page_count × copies」这条旧公式
app_src = open(os.path.join(BACKEND, "app.py"), encoding="utf-8").read()
chk("★ 变异 ②：revenue 接口不再用 Σ(page_count × copies) 作总页数",
    not re.search(r'"total_page_count":\s*sum\(f\["page_count"\]\s*\*\s*f\["copies"\]', app_src))

# E3 页面：把 _fileSheets 改回整份口径（删掉 total_sheets 分支）→ D 节的 1/1 必须变 2/2
if _fs:
    _mut_fs = ("function _fileSheets(f) {\n"
               "  var pg = (f && f.page_count) || 0;\n"
               "  if (pg <= 0) return 0;\n"
               "  var per = (f.duplex === 'off') ? 1 : 2;\n"
               "  return Math.ceil(pg / per) * (f.copies || 1);\n"
               "}\n")
    mprobe = os.path.join(TMP, "probe_mut.js")
    open(mprobe, "w", encoding="utf-8").write(
        _fp + "\n" + _mut_fs +
        "function fileRanged() { return {page_count: 2, copies: 1, duplex: 'off', page_range: '1',"
        " effective_pages: 1, sheets: 1, total_pages: 1, total_sheets: 1}; }\n"
        "process.stdout.write(JSON.stringify([_fileSheets(fileRanged()), _filePages(fileRanged())]));\n")
    rm = subprocess.run(["node", mprobe], capture_output=True, text=True, encoding="utf-8")
    ok_mut = rm.returncode == 0 and json.loads(rm.stdout) == [2, 1]
    chk("★ 变异 ③：页面改回整份口径后张数变 2（与金额对不上 → 被判据抓住）", ok_mut,
        "rc=%s stdout=%r stderr=%s" % (rm.returncode, (rm.stdout or "")[:120],
                                       (rm.stderr or "")[:200]))

# E4 页面：把订单行改回散装读整份页数 → C 节判据必须报
chk("★ 变异 ④：把订单行改回 'o.total_page_count || 0' 会被 C 节判据打红",
    "o.total_page_count || 0" not in html)

os.chdir(REPO)          # 先离开临时目录，Windows 下才能整棵删掉
shutil.rmtree(TMP, ignore_errors=True)
print("\n" + "=" * 62)
print("有效口径验收：", "全部通过 ✅" if bad == 0 else "存在失败 ❌（%d 条）" % bad)
sys.exit(0 if bad == 0 else 1)
