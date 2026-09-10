# -*- coding: utf-8 -*-
"""三端计费一致性核对

在「线上现行单价」下逐组合比对**四份真实实现**（不重新实现一遍）：
  1. 后端   mobile_apps/printer-backend/app.py :: calculate_price
  2. 本地   local_print_tool/printer_config.py :: calc_cost
  3. APP    mobile_apps/android_app/www/print.js :: calcCost
  4. 小程序 mobile_apps/h_n_print/pages/index/index.js :: _calcCost

并核对三处容易漂移的口径：
  C) 取整时机（后端「先把每份价取整再乘份数」vs 前端「先乘份数最后取整」）
  D) 附加服务取整（后端「先把派送费 round 再相加」vs 前端「先相加最后 toFixed」）
     —— 用 print.js 里的**原始表达式**在 Node 中求值，不用 Python 模拟 JS 的 toFixed
  E) 张数口径（结算页 _fileSheets 按整份页数 vs 后端 _file_range_stats 按范围折算）

用法：python tests/verify_pricing_consistency.py
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")
LOCAL = os.path.join(REPO, "local_print_tool")
PRINT_JS = os.path.join(REPO, "mobile_apps", "android_app", "www", "print.js")
INDEX_JS = os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.js")
SETTLEMENT = os.path.join(LOCAL, "finance", "settlement.html")

# 线上现行配置（/api/pricing 实测值）
PROD_SIMPLEX, PROD_DUPLEX, PROD_COVER = 0.3, 0.4, 0.10
PROD_DELIVERY_PCTS = [0.0, 5.0, 10.0, 15.0, 20.0]
PROD_URGENCY = [0.0, 0.08, 0.15]

failures = []
findings = []


def check(name, cond, detail=""):
    if not cond:
        failures.append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def finding(title, detail):
    findings.append((title, detail))
    print(f"  ⚠ {title}\n     {detail}")


tmp = tempfile.mkdtemp(prefix="hn_pricing_")

# ============================================================
# 载入后端（隔离临时副本，不碰真实 DB）
# ============================================================
dst = os.path.join(tmp, "printer-backend")
shutil.copytree(BACKEND, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs"))
sys.path.insert(0, dst)
_cwd = os.getcwd()
os.chdir(dst)
import app as backend  # noqa: E402
os.chdir(_cwd)

# ============================================================
# 载入本地
# ============================================================
sys.path.insert(0, LOCAL)
import printer_config as local_cfg  # noqa: E402


def backend_impl(pc, copies, duplex, prange, sp, dp):
    backend._load_paper_prices = lambda: (sp, dp)
    return (round(backend.calculate_price(pc, duplex, prange) * copies, 2),
            backend._count_pages_in_range(prange or "", pc))


def local_impl(pc, copies, duplex, prange, sp, dp):
    cost, _f = local_cfg.calc_cost(pc, copies, duplex, sp, dp, prange)
    return round(cost, 2), local_cfg._count_pages_in_range(prange or "", pc)


# ============================================================
# Node 探针：跑两份前端真实源码
# ============================================================
PROBE = r'''
const fs = require('fs');
const vm = require('vm');

const EL = () => ({
  innerHTML: '', textContent: '', value: '', style: {}, dataset: {},
  classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  querySelector: () => null, querySelectorAll: () => [], appendChild() {},
  addEventListener() {}, getBoundingClientRect: () => ({ height: 0 }),
  parentNode: null, children: [],
});
const BASE = {
  console: { log() {}, warn() {}, error() {} },
  JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map,
  parseInt, parseFloat, isNaN, encodeURIComponent, decodeURIComponent,
  setTimeout, clearTimeout, setInterval, clearInterval,
  FormData: class { append() {} },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  navigator: { userAgent: 'node' },
};

function android(sp, dp) {
  const sandbox = Object.assign({}, BASE, {
    XMLHttpRequest: class { constructor() { this.upload = {}; } open() {} setRequestHeader() {} abort() {} send() {} },
    state: { token: '' }, BASE_URL: '', api: () => Promise.resolve({}), ensureLogin: () => Promise.resolve(true),
    showToast() {}, openModal() {}, closeModal() {}, esc: (s) => String(s == null ? '' : s),
    escHtml: (s) => String(s == null ? '' : s), sanitizeColor: (c) => c,
    measureAll() {}, scheduleMeasureSoon() {}, renderPricing() {}, getApp: () => ({ globalData: {} }),
  });
  sandbox.window = sandbox; sandbox.globalThis = sandbox;
  sandbox.document = { getElementById: () => EL(), querySelector: () => null, querySelectorAll: () => [],
                       createElement: () => EL(), addEventListener() {}, body: EL() };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox, { filename: 'print.js' });
  const ps = vm.runInContext('printState', sandbox);
  ps.simplexPrice = sp; ps.duplexPrice = dp;
  return sandbox;
}

function mp(sp, dp) {
  let cfg = null;
  const sandbox = Object.assign({}, BASE, {
    require: (p) => (p.indexOf('utils/config') >= 0 ? { CONFIG: { BASE_URL: '' } } : { request: () => {} }),
    Page: (c) => { cfg = c; }, Component: (c) => { cfg = c; },
    getApp: () => ({ globalData: { isDarkMode: false, themeMode: 'light', _pageRegistry: [] } }),
    wx: {
      getStorageSync: () => '', setStorageSync() {}, removeStorageSync() {},
      setBackgroundColor() {}, showToast() {}, switchTab() {},
      getWindowInfo: () => ({ windowWidth: 375, windowHeight: 800 }),
      createSelectorQuery: () => ({ selectAll: () => ({ boundingClientRect() { return this; } }), exec(cb) { cb && cb([[]]); } }),
      uploadFile: () => ({ abort() {}, onProgressUpdate() {} }),
      nextTick: (fn) => setTimeout(fn, 0),
      getSystemInfoSync: () => ({ windowWidth: 375, windowHeight: 800 }),
    },
  });
  sandbox.global = sandbox; sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(process.argv[3], 'utf8'), sandbox, { filename: 'index.js' });
  const page = Object.assign({}, cfg, cfg.methods);
  page.data = { simplexPrice: sp, duplexPrice: dp };
  sandbox.__page = page;
  return sandbox;
}

const sp = parseFloat(process.argv[4]), dp = parseFloat(process.argv[5]);
const A = android(sp, dp), M = mp(sp, dp);
const callA = (expr) => vm.runInContext(expr, A);
const callM = (expr) => vm.runInContext(expr, M);

const matrix = JSON.parse(fs.readFileSync(process.argv[6], 'utf8'));
const main = matrix.map(([pc, copies, duplex, pr]) => ({
  a: callA(`calcCost(${pc}, ${copies}, ${JSON.stringify(duplex)}, ${JSON.stringify(pr)}).cost`),
  ap: callA(`countPagesInRange(${JSON.stringify(pr)}, ${pc})`),
  m: callM(`__page._calcCost(${pc}, ${copies}, ${JSON.stringify(duplex)}, ${JSON.stringify(pr)}).cost`),
  mp: callM(`countPagesInRange(${JSON.stringify(pr)}, ${pc})`),
}));

// 附加服务总价：用 print.js 里的**原始表达式**求值（读源码抽取，不手写复刻）
const src = fs.readFileSync(process.argv[2], 'utf8');
const want = ['let total = baseTotal;', 'if (p.deliveryEnabled) total +=', 'total += p.urgencyPrice;', 'if (p.coverPage) total +='];
const expr = want.map((w) => src.split('\n').find((l) => l.trim().startsWith(w)));
if (expr.some((e) => !e)) {
  process.stderr.write('抽取前端总价表达式失败: ' + JSON.stringify(expr) + '\n');
  process.exit(3);
}
const feFn = new Function('baseTotal', 'p', expr.join('\n') + '\nreturn total;');

const fees = JSON.parse(fs.readFileSync(process.argv[7], 'utf8')).map(([base, pct, urg, cover]) => {
  const p = { deliveryEnabled: pct > 0, deliveryPercent: pct, urgencyPrice: urg, coverPage: !!cover, coverPagePrice: 0.10 };
  const raw = feFn(base, p);
  return [Number(raw.toFixed(2)), raw];
});

process.stdout.write(JSON.stringify({ main, fees, exprUsed: expr.map((e) => e.trim()) }));
'''

probe_path = os.path.join(tmp, "probe.js")
open(probe_path, "w", encoding="utf-8").write(PROBE)

RANGES = ["", "1", "1-3", "2-5", "1,3,5", "1、3、5-7", "23-4", "5-3", "1-9999",
          "abc", "0", "-5", "5-", "1；3", "1 - 3", " 1 - 3 ", "1 -3", "1- 3", "1 3"]
MATRIX = [[pc, copies, duplex, pr]
          for pc in (1, 2, 3, 4, 5, 6, 9, 10, 23)
          for copies in (1, 2, 3, 5)
          for duplex in ("on", "off")
          for pr in RANGES]

# 只用「线上现价下真实可达」的文件费取值：单/双面 × 页数 × 份数 的组合结果
achievable = set()
for pc in range(1, 31):
    for copies in range(1, 6):
        for duplex in ("on", "off"):
            b, _ = backend_impl(pc, copies, duplex, "", PROD_SIMPLEX, PROD_DUPLEX)
            if b > 0:
                achievable.add(b)
FEE_MATRIX = [[base, pct, urg, cover]
              for base in sorted(achievable)
              for pct in PROD_DELIVERY_PCTS
              for urg in PROD_URGENCY
              for cover in (0, 1)]

print(f"线上可达文件费取值 {len(achievable)} 种: {sorted(achievable)[:8]} … 最大 ¥{max(achievable)}")
print(f"=== A/B) 四端计费一致性（线上单价 单面{PROD_SIMPLEX}/双面{PROD_DUPLEX}，{len(MATRIX)} 组合）===")
# 矩阵可能上千条，走临时文件传递（Windows 命令行有长度上限）
matrix_path = os.path.join(tmp, "matrix.json")
fees_path = os.path.join(tmp, "fees.json")
open(matrix_path, "w", encoding="utf-8").write(json.dumps(MATRIX))
open(fees_path, "w", encoding="utf-8").write(json.dumps(FEE_MATRIX))
res = subprocess.run(["node", probe_path, PRINT_JS, INDEX_JS, str(PROD_SIMPLEX), str(PROD_DUPLEX),
                      matrix_path, fees_path],
                     capture_output=True, text=True, encoding="utf-8")
if res.returncode != 0:
    print(res.stdout, res.stderr)
    sys.exit("Node 探针执行失败")
probe = json.loads(res.stdout)
js_main, js_fees = probe["main"], probe["fees"]

amount_diff, page_diff = [], []
for (pc, copies, duplex, pr), j in zip(MATRIX, js_main):
    b_cost, b_pages = backend_impl(pc, copies, duplex, pr, PROD_SIMPLEX, PROD_DUPLEX)
    l_cost, l_pages = local_impl(pc, copies, duplex, pr, PROD_SIMPLEX, PROD_DUPLEX)
    vals = {"后端": b_cost, "本地": l_cost, "APP": j["a"], "小程序": j["m"]}
    if len(set(vals.values())) > 1:
        amount_diff.append((pc, copies, duplex, pr, vals))
    pvals = {"后端": b_pages, "本地": l_pages, "APP": j["ap"], "小程序": j["mp"]}
    if len(set(pvals.values())) > 1:
        page_diff.append((pc, copies, duplex, pr, pvals))

check(f"文件级金额四端逐分一致（{len(MATRIX)} 组合）", not amount_diff,
      "" if not amount_diff else f"不一致 {len(amount_diff)} 组，例: {amount_diff[:2]}")
check("有效页数四端一致（含中文标点/越界/非法/倒序/含空格）", not page_diff,
      "" if not page_diff else f"不一致 {len(page_diff)} 组，例: {page_diff[:2]}")

if page_diff:
    bad_ranges = sorted({d[3] for d in page_diff})
    finding(
        f"前后端「页码范围含空格」解析分歧（{len(page_diff)} 组）",
        f"后端/本地把空白**删除**（\"1 - 3\"→1-3，3 页），前端把空白**替换成逗号**"
        f"（\"1 - 3\"→\"1,-,3\"，只认到 1 与 3 两页）→ 前端显示价低于后端实收。"
        f"涉及输入: {bad_ranges}。可达性已确认：前端 parseSingleRange 用 parseInt 剥空白，"
        f"\"1 - 3\" 被判合法并原样存进 pageRange（print.js:988 用 e.value 而非解析结果）。")

# ============================================================
# C) 取整时机
# ============================================================
print("\n=== C) 取整时机（后端先取整每份价 vs 前端先乘份数）===")


def base_price(eff, duplex, sp, dp):
    return (eff // 2) * dp + (eff % 2) * sp if duplex == "on" else eff * sp


bad_prod, bad_3dp = [], []
for eff in range(1, 41):
    for copies in (1, 2, 3, 5, 7):
        for duplex in ("on", "off"):
            for sp, dp, bucket in ((0.3, 0.4, bad_prod), (0.335, 0.445, bad_3dp)):
                base = base_price(eff, duplex, sp, dp)
                if round(round(base, 2) * copies, 2) != round(base * copies, 2):
                    bucket.append((eff, copies, duplex, round(round(base, 2) * copies, 2), round(base * copies, 2)))

check("线上 2 位小数单价下取整时机不产生差异", not bad_prod, str(bad_prod[:2]))
if bad_3dp:
    finding("单价一旦取到 3 位小数，后端与前端会差 1 分",
            f"{len(bad_3dp)} 组，例：有效页数={bad_3dp[0][0]} 份数={bad_3dp[0][1]} {bad_3dp[0][2]} "
            f"→ 后端 ¥{bad_3dp[0][3]} vs 前端 ¥{bad_3dp[0][4]}。现价 0.3/0.4 是 2 位小数，暂无影响。")

# ============================================================
# D) 附加服务取整（真实 JS 求值）
# ============================================================
print("\n=== D) 附加服务取整（后端 round 后相加 vs 前端相加后 toFixed）===")
print(f"  前端参与求值的原始表达式: {probe['exprUsed']}")


def backend_total(base, pct, urg, cover):
    delivery_fee = round(base * pct / 100, 2) if pct else 0
    cover_fee = round(PROD_COVER * cover, 2) if cover else 0
    return round(base + (urg if urg else 0) + cover_fee + delivery_fee, 2)


fee_diff = []
for (base, pct, urg, cover), (fe_display, _raw) in zip(FEE_MATRIX, js_fees):
    b = backend_total(base, pct, urg, cover)
    if abs(b - fe_display) >= 0.005:
        fee_diff.append((base, pct, urg, cover, b, fe_display))

by_pct = {}
for d in fee_diff:
    by_pct[d[1]] = by_pct.get(d[1], 0) + 1
check("附加服务费两端取整一致", not fee_diff,
      "" if not fee_diff else f"{len(fee_diff)} 组不一致，按派送%分布: {by_pct}")
if fee_diff:
    e = fee_diff[0]
    finding(f"派送费取整时机两端不一致（{len(fee_diff)} 组，集中在派送 5%/15%）",
            f"后端 `round(文件费×pct/100, 2)` 先取整再相加；前端 `total += 文件费×(pct/100)` "
            f"最后 toFixed(2)。例：文件费 ¥{e[0]:.2f} + 派送 {e[1]:g}% → 后端实收 ¥{e[4]:.2f}，"
            f"前端显示 ¥{e[5]:.2f}（差 1 分）。10%/20% 因恰好整除不受影响。")

# ============================================================
# E) 张数口径
# ============================================================
print("\n=== E) 张数口径（结算页 vs 后端）===")
settle_src = open(SETTLEMENT, encoding="utf-8").read()
m = re.search(r"function _fileSheets\(f\) \{.*?\n\}", settle_src, re.S)
if not m:
    check("能从结算页提取 _fileSheets", False)
else:
    sheets_js = os.path.join(tmp, "sheets.js")
    cases = [[pc, copies, duplex, pr] for pc in (1, 2, 3, 4, 5, 9, 10)
             for copies in (1, 2, 3) for duplex in ("on", "off")
             for pr in ("", "1-3", "2", "1,3,5")]
    open(sheets_js, "w", encoding="utf-8").write(
        m.group(0) + "\nconst cs = JSON.parse(process.argv[2]);\n"
        "process.stdout.write(JSON.stringify(cs.map(([pc,copies,duplex,pr]) => "
        "_fileSheets({page_count:pc,copies:copies,duplex:duplex,page_range:pr}))));\n")
    r2 = subprocess.run(["node", sheets_js, json.dumps(cases)],
                        capture_output=True, text=True, encoding="utf-8")
    settle_sheets = json.loads(r2.stdout)
    sheets_diff = []
    for (pc, copies, duplex, pr), ss in zip(cases, settle_sheets):
        stats = backend._file_range_stats(pc, copies, duplex, pr)
        if ss != stats["total_sheets"]:
            sheets_diff.append((pc, copies, duplex, pr, ss, stats["total_sheets"]))
    if sheets_diff:
        finding(f"张数口径不同（结算页 {len(sheets_diff)} 组与后端不一致）",
                f"结算页 `_fileSheets` 按**整份页数**折算（注释写明与「总页数」口径一致），"
                f"后端 `_file_range_stats` 按**页码范围**折算。例：{sheets_diff[0][0]} 页 {sheets_diff[0][1]} 份 "
                f"范围'{sheets_diff[0][3]}' → 结算页 {sheets_diff[0][4]} 张 vs 后端 {sheets_diff[0][5]} 张。"
                "两者都不算错，但对账时容易被误读为不一致。")
    else:
        check("张数口径一致", True)

shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 62)
print("计费核对:", "无 FAIL ✅" if not failures else f"{len(failures)} 项 FAIL ❌")
if findings:
    print(f"\n共 {len(findings)} 处发现：")
    for i, (t, _d) in enumerate(findings, 1):
        print(f"  {i}. {t}")
sys.exit(1 if failures else 0)
