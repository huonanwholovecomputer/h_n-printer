# -*- coding: utf-8 -*-
"""验收 · v7.7「打印机寿命第二口径：累计实测（不预估总寿命）」（2026-10-26 用户需求）

需求原话：「既然总寿命是未知的，那就加一个不提供总寿命的模式，一直累积用量，
什么时候用坏了，再把所有用纸情况统计起来，然后再按照用量计算折旧费用。」

判据四组：
  A. 静态接线 —— 数据模型（life_mode / pre_pages / started_at / measured / settled）、
     计提对象（在役 + 已结算报废机）、结算快照、台账/用量/折旧三处显示、版本说明。
  B. 纯函数实测（Node VM 里跑**真实的** printerLifeState / _printerPagesFromOrders /
     _buildSettledSnapshot / calcMonth，外加真实的台账单元格构造器）——
     · 累计中（寿命未知）⇒ 每页成本 0、折旧 0、绝不出现 Infinity / NaN；
     · 结算后：各月折旧取自快照、Σ各月 ≡ 追踪期应承担的寿命价值、按人页数分摊；
     · 快照与「订单归属」解耦（之后把 usage 换成别的页数，历史金额一分不变）；
     · 报废机（measured）仍留在历史账上；退役但**没**结算的老机器仍然不计提（零回归）；
     · pre_pages（起算前已用页数）只进分母、不进月份池；
     · 页数口径：只算 status='sent'、取 total_pages、按月聚合、别的机器的单不算。
  C. 变异自证 —— 把「计提对象 / 快照取值 / 起算前处理 / 累计中判定」任一改坏，对应断言必须转红。

用法：python tests/verify_printer_life_mode.py
exit 0 = 全部通过。
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(REPO, "local_print_tool", "finance", "settlement.html")
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(t):
    print("\n=== " + t + " ===")


src = open(HTML, encoding="utf-8").read()

# ══════════════════════════════════════════════════════════════
# A. 静态接线
# ══════════════════════════════════════════════════════════════
section("A. 静态接线（数据模型 / 计提对象 / 结算快照 / 三处显示）")

check("normalizePrinter 认得 life_mode / pre_pages / started_at / measured / settled_at / settled",
      all(k in src for k in ("p.life_mode = (p.life_mode === 'accumulate')",
                             "p.pre_pages = Math.max(0",
                             "p.started_at = _legalDateStr(p.started_at);",
                             "p.measured = !!p.measured && +p.life_pages > 0",
                             "p.settled_at = _legalDateStr(p.settled_at);",
                             "p.settled = null")))
check("日期走 _legalDateStr 白名单（2026-13-99 这类脏值不放行 —— 结算区间会变成非法 SQL 参数）",
      "function _legalDateStr(s) {" in src and "if (mo < 1 || mo > 12 || d < 1 || d > 31) return '';" in src
      and "p.settled.at = _legalDateStr(p.settled.at);" in src
      and "p.started_at = _legalDateStr(v.started_at);" in src)
check("normalizePrinter 净化结算快照（只留合法月份键与整数分，脏配置不会把某月算成 NaN）",
      "var _settledMonths = {};" in src and "if (!/^\\d{4}-\\d{2}$/.test(mk) || !mm || typeof mm !== 'object') return;" in src)
check("寿命口径唯一判据 printerLifeState（estimate / accumulating / measured）",
      "function printerLifeState(p) {" in src
      and "'estimate'" in src and "'accumulating'" in src and "'measured'" in src
      and "return (p.measured && +p.life_pages > 0) ? 'measured' : 'accumulating';" in src)
check("计提对象 getDepreciablePrinters = 在役 + 已按实测结算的报废机（否则历史折旧随退役消失）",
      "function getDepreciablePrinters() {" in src
      and "if (p.status !== 'retired') return true;" in src
      and "return !!(p.measured && +p.life_pages > 0);" in src
      and "getDepreciablePrinters().forEach(function (p) {" in src)
check("calcMonth 优先用结算快照（页数 / 金额 / 分摊都冻结）",
      "var snap = (p.settled && p.settled.months) ? p.settled.months[m.key] : null;" in src
      and "costAll = Math.max(0, Math.round(+snap.cents || 0));" in src
      and "var costOnThis = snap ? Math.max(0, costAll - orphanCost) : printerDepCents(p, pagesOnThis);" in src
      and "var cost = costOnThis;" in src and "var pool = costOnThis;" in src)
check("未归属成员的快照页按比例切出，不凭空丢分/多分",
      "var orphanCost = snap" in src and "Math.round(costAll * pagesOrphan / pagesAll)" in src
      and "wearUnallocated += orphanCost;" in src and "depUnallocated += orphanCost;" in src)
check("结算快照唯一算法 _buildSettledSnapshot（整数分 + 最大余数法 apportionCents）",
      "function _buildSettledSnapshot(priceCents, totalPages, trackedPages, monthsRaw) {" in src
      and "var pool = (totalPages > 0) ? Math.round(priceCents * trackedPages / totalPages) : 0;" in src
      and "apportionCents(pool, keys.map(function (k) { return monthsRaw[k].pages; })" in src)
check("订单 → 这台机的页数 _printerPagesFromOrders（只算 sent、取 total_pages、按月 / 按成员）",
      "function _printerPagesFromOrders(orders, p) {" in src
      and "if ((f.status || '') !== 'sent') return;" in src
      and "pages += _filePagesOf(f);" in src
      and "var b = res.months[mk] || (res.months[mk] = { pages: 0, byMember: {} });" in src)
check("已退役的机器重算时按自己的 client_id / 已知机型再认一次（不把别的机器的单算进来）",
      "var mine = (p.client_id && cid && cid === p.client_id)" in src
      and "var model = owner ? (KNOWN_PRINTER_MODELS[owner] || '') : '';" in src)
check("累计读数按区间一次取回（fetchOrdersRange，缓存到 invalidateUsage）",
      "function fetchOrdersRange(start, end, cb) {" in src
      and "function printerCumulative(p, cb) {" in src
      and "_rangeOrdersCache[key] = data.data.orders;" in src
      and "_rangeOrdersCache = {}; _cumCache = {};" in src)
check("报废结算入口 openPrinterSettle：没填价格不结算、没页数不结算（杜绝除零）",
      "function openPrinterSettle(id) {" in src
      and "(priceCents > 0)" in src and "if (!(agg.pages > 0))" in src
      and "p.settled = { at: at, pages: total, trackedPages: agg.pages, prePages: pre," in src)
check("结算后标记实测 + 报废：life_pages=实测页数、measured=true、status='retired'",
      "p.life_pages = total;" in src and "p.measured = true;" in src and "p.status = 'retired';" in src)
check("台账寿命单元格唯一出口 printerLifeCellHTML（累计中：读数 + 💥结算 + ⚙参数 + ⇄；实测：🧾重算）",
      "function printerLifeCellHTML(p, editable) {" in src
      and 'id="printer-cum-' in src and "openPrinterSettle(" in src
      and "openPrinterAccumModal(" in src and "setPrinterLifeMode(" in src)
check("台账不再直接写 life_pages 输入框（寿命列改走 printerLifeCellHTML）",
      "onchange=\"updPrinter(\\'' + pid + '\\',\\'life_pages\\',+this.value)\">" not in src
      or "printerLifeCellHTML(p, true)" in src)
check("每页成本单元格：累计中显示「累计中 · 未计提」，实测加「实测」标记",
      "累计中 · 未计提" in src and "st === 'measured'" in src)
check("用量表 / 收支清算设备折旧：累计中不再显示成「寿命未填」（红字误导）",
      src.count("累计中") >= 4)
check("寿命口径切换 setPrinterLifeMode 有确认 + 切回估算会解冻快照",
      "function setPrinterLifeMode(id, mode) {" in src
      and "p.settled = null;" in src and "会解除已冻结的历史折旧" in src)
check("累计模式机器不会被导入净化丢掉（寿命 0 但要留）",
      "if (!(p.life_pages > 0) && printerLifeState(p) !== 'accumulating') {" in src)
check("版本说明已补 v7.7", "v7.7：<b>寿命第二口径「累计实测」</b>" in src)
check("进本页才拉累计读数（隐藏页不抢接口）",
      "if (currentTab === 'printers') _kickCumulativeFetch();" in src)

# ══════════════════════════════════════════════════════════════
# B/C. Node VM 侧
# ══════════════════════════════════════════════════════════════

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const htmlPath = process.argv[2];
const html = fs.readFileSync(htmlPath, 'utf8');
const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const js = scripts.join('\n;\n');

const results = [];
const P = (n, c, d) => results.push([n, !!c, d == null ? '' : String(d)]);

/* 按名字抠出一个函数定义（大括号配平；这些函数体内没有字符串里的括号） */
function defOf(name) {
  const at = js.indexOf('function ' + name + '(');
  if (at < 0) return '';
  let i = js.indexOf('{', at), depth = 0;
  for (let j = i; j < js.length; j++) {
    if (js[j] === '{') depth++;
    else if (js[j] === '}') { depth--; if (!depth) return js.slice(at, j + 1); }
  }
  return '';
}

function MB() {
  return {
    console: { log() {}, warn() {}, error() {} },
    Math, JSON, Object, Array, String, Number, Boolean, Date, RegExp, Error, Promise,
    Set, Map, isNaN, isFinite, parseFloat, parseInt, encodeURIComponent, decodeURIComponent,
    setTimeout, clearTimeout, setInterval, clearInterval,
    DEFAULT_MEMBERS: [], DEFAULT_SCORE_CAP: 15, DEFAULT_RATE: 0.05,
    PASSWORD_KEYS: ['score', 'expense', 'income', 'transfer', 'settings', 'auth', 'orderprinter'],
    DEFAULT_PASSWORDS: {}, DEFAULT_PRICING: { simplex_price: 0.2, duplex_price: 0.3, cover_page_price: 0.1,
      delivery_percentages: {}, urgency_prices: {} },
    FINANCE_MODE: 'cloud', STORE: null, _authDevices: null,
  };
}
/* 半截沙箱：「数据模型 → calcMonth」整段 + 三样外部依赖（_filePagesOf 走真身，
   _orderMemberKey 用桩 —— 成员映射不是本验收的对象，页数/月份归属才是） */
function calcSandbox(src) {
  const sb = MB();
  vm.createContext(sb);
  const start = src.indexOf('function migrateConfig(data)');
  const end = src.indexOf('/* ==================== 保存 ====================');
  if (start < 0 || end <= start) { console.log('SNIPPET_FAIL'); process.exit(3); }
  let snippet = src.slice(start, end);
  snippet += '\n' + defOf('_filePagesOf');
  snippet += '\n' + defOf('fmtInt');
  snippet += '\n' + defOf('_printerPagesFromOrders');
  snippet += '\n' + defOf('_buildSettledSnapshot');
  snippet += '\n' + defOf('printerUnitCellHTML');
  snippet += '\n' + defOf('printerLifeCellHTML');
  /* _orderMemberKey 桩：订单带 mid 就算成员，否则未归属 */
  snippet += "\nfunction _orderMemberKey(o) { return (o && o.mid) ? ('member:' + o.mid) : 'local:none'; }";
  vm.runInContext(snippet, sb);
  return sb;
}
const MEMBERS = [{ id: 'ma', name: '成员A', color: '#4472C4' }, { id: 'mb', name: '成员B', color: '#548235' }];
function store2(records, extraCfg) {
  const cfg = Object.assign({ investRate: 0.05, scoreCap: 15, passwords: {}, members: MEMBERS,
                              printers: [], purchaseBasis: 'equal' }, extraCfg || {});
  return { config: cfg,
           records: Object.assign({ expenses: [], income: [], scores: [], transfers: [] }, records || {}),
           notes: {}, meta: { version: 0 } };
}
function usageOf(pages, perMember, perMemberPrinter) {
  const u = { month: '', pages: 0, orders: 1, files: 1, pagesByMember: perMember || {},
              pagesByMemberPrinter: perMemberPrinter || {}, pagesByPrinter: {},
              unmatchedPrinterPages: 0, unattributedPages: 0, fetchedAt: '' };
  Object.keys(u.pagesByMember).forEach(k => { u.pages += u.pagesByMember[k]; });
  Object.keys(u.pagesByMemberPrinter).forEach(k => {
    const pid = k.split('|')[1];
    u.pagesByPrinter[pid] = (u.pagesByPrinter[pid] || 0) + u.pagesByMemberPrinter[k];
  });
  if (pages != null) u.pages = pages;
  return u;
}
function sameCents(a, b) { return Math.abs((+a || 0) - (+b || 0)) <= 1; }

/* 所有 B 组用例装进一个函数：任何一个块中途抛异常，也要留下 FAIL 记录（否则变异自证会看到"退出码非 0 但 0 条 FAIL"，等于空转） */
function runCases() {

/* ═════ B1：累计中（总寿命未知）不产生折旧，也绝不 Infinity ═════ */
{
  const sb = calcSandbox(js);
  const acc = { id: 'p1', name: 'HP LaserJet MFP M232-M237', model: 'HP LaserJet MFP M232-M237',
                price: 1500, life_pages: 0, status: 'active', life_mode: 'accumulate',
                pre_pages: 0, started_at: '' };
  sb.STORE = store2({ expenses: [{ id: 'e1', date: '2026-06-03', item: '纸', amount: 100, payer: 'ma' }] },
                    { printers: [acc] });
  const p = sb.STORE.config.printers[0];
  sb.normalizePrinter(p);
  P('B1 累计中：printerLifeState = accumulating', sb.printerLifeState(p) === 'accumulating', sb.printerLifeState(p));
  P('B1 累计中：每页成本 / 折旧都是 0（不是 Infinity / NaN）',
    sb.printerUnitYuan(p) === 0 && sb.printerDepCents(p, 4000) === 0
    && Number.isFinite(sb.printerDepCents(p, 4000)));
  const u = usageOf(400, { ma: 400 }, { 'ma|p1': 400 });
  const r = sb.calcMonth(sb.monthFromRecords('2026-06'), { usage: u });
  P('B1 累计中：本月折旧 0（400 页也不计提，等报废结算再回溯）',
    r.depTotal === 0 && r.depByMember.ma === 0 && Number.isFinite(r.depTotal), String(r.depTotal));
  P('B1 累计中：Σ建议转账 ≡ 0、ΣB ≡ ΣC ≡ Π（恒等式不受影响）',
    (r.net.ma + r.net.mb) === 0
    && (r.bookPL.ma + r.bookPL.mb) === (r.incomeTotal == null ? (r.totalInc - r.totalExp - r.depTotal) : (r.totalInc - r.totalExp - r.depTotal))
    && (r.actualPL.ma + r.actualPL.mb) === (r.totalInc - r.totalExp - r.depTotal),
    JSON.stringify({ n: r.net, b: r.bookPL, c: r.actualPL, dep: r.depTotal }));
}

/* ═════ B2：结算快照 —— 各月折旧取自快照、Σ各月 ≡ 寿命价值、按人分摊、报废机仍在账上 ═════ */
{
  const sb = calcSandbox(js);
  const months = { '2026-05': { pages: 600, byMember: { ma: 400, mb: 200 } },
                   '2026-06': { pages: 400, byMember: { ma: 400 } } };
  const snap = sb._buildSettledSnapshot(120000, 1000, 1000, months);   // ¥1200 / 1000 页实测
  P('B2 快照：Σ各月金额 ≡ 整机价 ¥1200（最大余数法，不差一两分）',
    Object.keys(snap.months).reduce((s, k) => s + snap.months[k].cents, 0) === 120000
    && snap.poolCents === 120000,
    JSON.stringify(Object.values(snap.months).map(x => x.cents)));
  P('B2 快照：各月金额按页数比例（600:400 ⇒ 720:480 元）',
    snap.months['2026-05'].cents === 72000 && snap.months['2026-06'].cents === 48000,
    JSON.stringify([snap.months['2026-05'].cents, snap.months['2026-06'].cents]));
  const settled = { id: 'p1', name: 'HP LaserJet MFP M232-M237', model: 'HP LaserJet MFP M232-M237',
                    price: 1200, life_pages: 1000, status: 'retired', life_mode: 'accumulate',
                    measured: true, settled_at: '2026-07-01',
                    settled: { at: '2026-07-01', pages: 1000, trackedPages: 1000, prePages: 0,
                               cents: 120000, months: snap.months } };
  sb.normalizePrinter(settled);
  P('B2 结算后 printerLifeState = measured、status 仍是退役',
    sb.printerLifeState(settled) === 'measured' && settled.status === 'retired', sb.printerLifeState(settled));
  sb.STORE = store2({}, { printers: [settled] });
  P('B2 计提对象包含「已结算的报废机」（getDepreciablePrinters）',
    sb.getDepreciablePrinters().length === 1 && sb.getActivePrinters().length === 0);
  /* 关键：**完全不传 usage**（订单可能已被重指 / 设备已删）也要按快照计提 */
  const r5 = sb.calcMonth(sb.monthFromRecords('2026-05'));
  P('B2 无 usage 也按快照计提 2026-05：¥720，按 400:200 页分成 ¥480 / ¥240',
    r5.depTotal === 72000 && r5.depByMember.ma === 48000 && r5.depByMember.mb === 24000,
    JSON.stringify({ dep: r5.depTotal, by: r5.depByMember }));
  P('B2 快照月标记 frozen + 页数来自快照（600 页）',
    r5.depByPrinter.length === 1 && r5.depByPrinter[0].frozen === true
    && r5.depByPrinter[0].pages === 600 && r5.depByPrinter[0].measured === true,
    JSON.stringify(r5.depByPrinter[0] && { pages: r5.depByPrinter[0].pages, frozen: r5.depByPrinter[0].frozen }));
  const r6 = sb.calcMonth(sb.monthFromRecords('2026-06'));
  P('B2 2026-06：¥480，且只有 ma 一个人承接（mb 该月 0 页）',
    r6.depTotal === 48000 && r6.depByMember.ma === 48000 && r6.depByMember.mb === 0,
    JSON.stringify({ dep: r6.depTotal, by: r6.depByMember }));
  P('B2 两个月合计 ≡ 整机价 ¥1200（整机寿命价值恰好摊完，不多不少）',
    r5.depTotal + r6.depTotal === 120000, String(r5.depTotal + r6.depTotal));
  P('B2 恒等式依旧：Σnet ≡ 0、ΣB ≡ ΣC ≡ Π',
    (r5.net.ma + r5.net.mb) === 0 && (r6.net.ma + r6.net.mb) === 0
    && (r5.bookPL.ma + r5.bookPL.mb) === (r5.totalInc - r5.totalExp - r5.depTotal)
    && (r5.actualPL.ma + r5.actualPL.mb) === (r5.totalInc - r5.totalExp - r5.depTotal),
    JSON.stringify([r5.bookPL, r5.actualPL]));
}

/* ═════ B3：快照与订单归属解耦（之后改成别的页数，历史金额一分不变） ═════ */
{
  const sb = calcSandbox(js);
  const months = { '2026-06': { pages: 500, byMember: { ma: 500 } } };
  const snap = sb._buildSettledSnapshot(150000, 500, 500, months);    // ¥1500 / 500 页
  const settled = { id: 'p2', name: 'X', model: 'X', price: 1500, life_pages: 500, status: 'retired',
                    life_mode: 'accumulate', measured: true, settled: { at: '2026-07-01', pages: 500,
                    trackedPages: 500, prePages: 0, cents: 150000, months: snap.months } };
  sb.normalizePrinter(settled);
  sb.STORE = store2({}, { printers: [settled] });
  const wild = usageOf(4321, { ma: 4000, mb: 321 }, { 'ma|p2': 4000, 'mb|p2': 321 });
  const r = sb.calcMonth(sb.monthFromRecords('2026-06'), { usage: wild });
  P('B3 归属改成 4321 页也不影响：仍按快照 ¥1500、只算 500 页、只归 ma',
    r.depTotal === 150000 && r.depByPrinter[0].pages === 500
    && r.depByMember.ma === 150000 && r.depByMember.mb === 0,
    JSON.stringify({ dep: r.depTotal, pages: (r.depByPrinter[0] || {}).pages, by: r.depByMember }));
}

/* ═════ B4：退役但**没**结算的老机器仍不计提（老口径零回归） ═════ */
{
  const sb = calcSandbox(js);
  const retired = { id: 'p1', name: 'A', price: 1200, life_pages: 3000, status: 'retired' };
  const accNoSettle = { id: 'p2', name: 'B', price: 1500, life_pages: 0, status: 'active',
                        life_mode: 'accumulate' };
  sb.STORE = store2({}, { printers: [retired, accNoSettle] });
  const u = usageOf(400, { ma: 200, mb: 200 }, { 'ma|p1': 200, 'mb|p2': 200 });
  const r = sb.calcMonth(sb.monthFromRecords('2026-06'), { usage: u });
  P('B4 退役未结算 / 累计中都不计提（0 元，且不是 NaN / Infinity）',
    r.depTotal === 0 && Number.isFinite(r.depTotal), String(r.depTotal));
  P('B4 预估口径的机器照旧计提（零回归）：¥0.40/页 × 200 页 = ¥80',
    (function () {
      const s2 = calcSandbox(js);
      const normal = { id: 'p1', name: 'A', price: 1200, life_pages: 3000, status: 'active' };
      s2.STORE = store2({}, { printers: [normal] });
      const r2 = s2.calcMonth(s2.monthFromRecords('2026-06'),
        { usage: usageOf(200, { ma: 200 }, { 'ma|p1': 200 }) });
      return r2.depTotal === 8000 && r2.depByMember.ma === 8000;
    })());
}

/* ═════ B5：起算前已用页数（pre_pages）只进分母、不进月份池 ═════ */
{
  const sb = calcSandbox(js);
  const months = { '2026-05': { pages: 500, byMember: { ma: 500 } },
                   '2026-06': { pages: 500, byMember: { ma: 500 } } };
  /* 整机 ¥1500；实测总寿命 1000 + 起算前 500 = 1500 页 ⇒ 追踪期只承担 ¥1000 */
  const snap = sb._buildSettledSnapshot(150000, 1500, 1000, months);
  P('B5 Σ各月 = 整机价 × 追踪期页数 ÷ 实测总页数 = ¥1000（起算前那 500 页的价值不进本账）',
    snap.poolCents === 100000
    && Object.keys(snap.months).reduce((s, k) => s + snap.months[k].cents, 0) === 100000,
    JSON.stringify({ pool: snap.poolCents, months: Object.values(snap.months).map(x => x.cents) }));
  P('B5 两个 500 页的月份各 ¥500（按页数比例，余数不丢）',
    snap.months['2026-05'].cents === 50000 && snap.months['2026-06'].cents === 50000,
    JSON.stringify(snap.months));
}

/* ═════ B6：订单 → 页数（只算 sent / 取 total_pages / 按月按人 / 不串台） ═════ */
{
  const sb = calcSandbox(js);
  const p = { id: 'p1', name: 'HP LaserJet MFP M232-M237', model: 'HP LaserJet MFP M232-M237',
              price: 1500, life_pages: 0, status: 'retired', life_mode: 'accumulate',
              client_id: 'DESKTOP-A-1' };
  sb.STORE = store2({}, { printers: [p] });
  const orders = [
    { id: 1, created_at: '2026-05-10 09:00:00', received_client: 'DESKTOP-A-1', mid: 'ma',
      files: [{ status: 'sent', total_pages: 30 }, { status: 'failed', total_pages: 99 }] },
    { id: 2, created_at: '2026-05-20 10:00:00', received_client: 'DESKTOP-A-1', mid: 'mb',
      files: [{ status: 'sent', total_pages: 20 }] },
    { id: 3, created_at: '2026-06-01 10:00:00', received_client: 'DESKTOP-A-1', mid: 'ma',
      files: [{ status: 'sent', effective_pages: 5, copies: 2 }] },
    /* 别的设备（未绑定、也没有已知机型）⇒ 不该算到 p1 头上 */
    { id: 4, created_at: '2026-06-02 10:00:00', received_client: 'DESKTOP-Z-9', mid: 'ma',
      files: [{ status: 'sent', total_pages: 77 }] },
  ];
  const agg = sb._printerPagesFromOrders(orders, p);
  P('B6 只算 sent 子任务（failed 的 99 页不算）⇒ 合计 30+20+10 = 60 页',
    agg.pages === 60, String(agg.pages));
  P('B6 缺 total_pages 时退回「有效页 × 份数」（5 × 2 = 10 页）',
    agg.months['2026-06'] && agg.months['2026-06'].pages === 10,
    JSON.stringify(agg.months['2026-06']));
  P('B6 按月聚合：2026-05 = 50 页（ma 30 / mb 20）',
    agg.months['2026-05'] && agg.months['2026-05'].pages === 50
    && agg.months['2026-05'].byMember.ma === 30 && agg.months['2026-05'].byMember.mb === 20,
    JSON.stringify(agg.months['2026-05']));
  P('B6 别的设备的单不串到这台上（77 页被排除）', agg.pages !== 137 && agg.months['2026-06'].pages === 10);
  /* 在役 + 绑定设备 ⇒ 走正常 printerForOrder；退役后仍要按自己的 client_id 认（重算场景） */
  P('B6 机器在役时结果一致（归属路径不影响页数）',
    (function () {
      const s2 = calcSandbox(js);
      const q = Object.assign({}, p, { status: 'active' });
      s2.STORE = store2({}, { printers: [q] });
      return s2._printerPagesFromOrders(orders, q).pages === 60;
    })());
}

/* ═════ B7：normalizePrinter 卫生（脏字段不会把账算歪） ═════ */
{
  const sb = calcSandbox(js);
  const p = { id: 'p1', name: 'A', price: '1200', life_pages: '0', status: 'active',
              life_mode: 'ACCUMULATE!', pre_pages: '-5', started_at: '2026-13-99',
              measured: true, settled_at: 'xx',
              settled: { at: 'bad', pages: '1000', trackedPages: 900.7, prePages: -3, cents: '120000',
                         months: { '2026-05': { pages: 600, byMember: { ma: 600 }, cents: 72000 },
                                   'BADKEY': { pages: 5 }, '2026-06': { pages: 'x', byMember: { ma: 'x' } } } } };
  sb.normalizePrinter(p);
  P('B7 非法 life_mode 退回 estimate、负 pre_pages 归 0、非法日期清空',
    p.life_mode === 'estimate' && p.pre_pages === 0 && p.started_at === '' && p.settled_at === '');
  P('B7 life_pages=0 时 measured 必须为 false（否则会显示"实测 0 页"）', p.measured === false);
  P('B7 快照只留合法月份键、金额取整；2026-06 的脏页数归 0 但不产生 NaN',
    p.settled && Object.keys(p.settled.months).length === 2
    && p.settled.months['2026-05'].cents === 72000
    && Number.isFinite(p.settled.months['2026-06'].pages)
    && p.settled.pages === 1000 && p.settled.trackedPages === 901,
    JSON.stringify(p.settled && p.settled.months));
}

/* ═════ B8：导入净化 —— 累计中的机器（寿命 0）必须留下 ═════ */
{
  const sb = calcSandbox(js);
  const data = sb.sanitizeStore({ config: { printers: [
      { id: 'p1', name: '累计中', price: 1500, life_pages: 0, life_mode: 'accumulate', status: 'active' },
      { id: 'p2', name: '没填寿命', price: 1500, life_pages: 0, status: 'active' },
      { id: 'p3', name: '正常', price: 1500, life_pages: 50000, status: 'active' } ] },
    records: {}, notes: {}, meta: {} });
  const names = data.config.printers.map(x => x.name);
  P('B8 累计中（寿命 0）保留、没填寿命的预估机丢掉、正常机保留',
    names.indexOf('累计中') >= 0 && names.indexOf('没填寿命') < 0 && names.indexOf('正常') >= 0,
    JSON.stringify(names));
}

/* ═════ B9：台账单元格渲染（三种口径各说各的话） ═════ */
{
  const sb = calcSandbox(js);
  const est = { id: 'p1', name: 'A', price: 1200, life_pages: 100000, status: 'active' };
  const acc = { id: 'p2', name: 'B', price: 1500, life_pages: 0, status: 'active', life_mode: 'accumulate' };
  const mea = { id: 'p3', name: 'C', price: 1500, life_pages: 4072, status: 'retired',
                life_mode: 'accumulate', measured: true };
  [est, acc, mea].forEach(x => sb.normalizePrinter(x));
  const cEst = sb.printerLifeCellHTML(est, true);
  const cAcc = sb.printerLifeCellHTML(acc, true);
  const cMea = sb.printerLifeCellHTML(mea, true);
  P('B9 预估：给数字输入框 + 「累计」切换入口', cEst.indexOf('type="number"') >= 0
    && cEst.indexOf("setPrinterLifeMode('p1','accumulate')") >= 0, cEst);
  P('B9 累计中：累计读数占位 + 💥报废结算 + ⚙参数 + ⇄，且没有数字输入框（不许再手填寿命）',
    cAcc.indexOf('printer-cum-p2') >= 0 && cAcc.indexOf('>💥</button>') >= 0
    && cAcc.indexOf('title="报废结算') >= 0
    && cAcc.indexOf('openPrinterAccumModal(\'p2\')') >= 0 && cAcc.indexOf('type="number"') < 0, cAcc);
  P('B9 实测：显示实测页数 + 「实测」标记 + 🧾 重算入口',
    cMea.indexOf('4,072') >= 0 && cMea.indexOf('实测') >= 0
    && cMea.indexOf("openPrinterSettle('p3')") >= 0, cMea);
  P('B9 锁定态不给入口（printerLifeCellHTML(p,false) 只有文字）',
    sb.printerLifeCellHTML(acc, false).indexOf('<input') < 0
    && sb.printerLifeCellHTML(acc, false).indexOf('onclick') < 0);
  P('B9 每页成本：累计中 = 「累计中 · 未计提」（不是红色的"寿命未填"）',
    sb.printerUnitCellHTML(acc).indexOf('累计中 · 未计提') >= 0
    && sb.printerUnitCellHTML(acc).indexOf('寿命未填') < 0, sb.printerUnitCellHTML(acc));
  P('B9 每页成本：实测机显示单价 + 实测标记',
    sb.printerUnitCellHTML(mea).indexOf('¥0.3684') >= 0 && sb.printerUnitCellHTML(mea).indexOf('实测') >= 0,
    sb.printerUnitCellHTML(mea));
}
}
try { runCases(); } catch (e) { P('B 组用例中断（异常本身即失败）', false, e && (e.message || String(e))); }

let bad = 0;
for (const [n, c, d] of results) {
  if (!c) bad++;
  console.log(`[${c ? 'PASS' : 'FAIL'}] ${n}` + (d ? '  -- ' + d : ''));
}
process.exit(bad ? 1 : 0);
"""


def run_harness(html_path, workdir, tag="harness"):
    hp = os.path.join(workdir, tag + ".js")
    open(hp, "w", encoding="utf-8").write(HARNESS)
    r = subprocess.run(["node", hp, html_path], capture_output=True, text=True, encoding="utf-8")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


section("B. 纯函数实测（真跑 printerLifeState / _printerPagesFromOrders / _buildSettledSnapshot / calcMonth）")
tmpdir = tempfile.mkdtemp(prefix="hn_lifemode_")
code, out_text = run_harness(HTML, tmpdir)
print(out_text.rstrip())
check("函数级断言全部通过", code == 0, f"exit={code}")

# ══════════════════════════════════════════════════════════════
# C. 变异自证
# ══════════════════════════════════════════════════════════════
section("C. 变异自证（改坏 → 必须变红）")

MUTANTS = [
    ("计提对象退回「只在役」",
     "getDepreciablePrinters().forEach(function (p) {",
     "getActivePrinters().forEach(function (p) {"),
    ("结算快照不生效（仍按订单现算）",
     "var snap = (p.settled && p.settled.months) ? p.settled.months[m.key] : null;",
     "var snap = null;"),
    ("起算前页数也算进月份池（让现在的人替过去出钱）",
     "var pool = (totalPages > 0) ? Math.round(priceCents * trackedPages / totalPages) : 0;",
     "var pool = priceCents;"),
    ("累计中被当成「预估寿命 0」的老口径",
     "if (p.life_mode !== 'accumulate') return 'estimate';",
     "return 'estimate';"),
    ("快照金额不按页数比例（整机价无脑砸给每个月）",
     "cents: cents[k] || 0 };",
     "cents: cents[k] ? Math.round(cents[k] * 2) : 0 };"),
]
for idx, (name, old, new) in enumerate(MUTANTS):
    if old not in src:
        check(f"变异「{name}」：锚点存在（锚点没了 ⇒ 变异自证空转）", False, old[:60])
        continue
    mp = os.path.join(tmpdir, f"mutant_{idx}.html")
    open(mp, "w", encoding="utf-8").write(src.replace(old, new, 1))
    mcode, mout = run_harness(mp, tmpdir, tag=f"harness_m{idx}")
    reds = [ln for ln in mout.splitlines() if ln.startswith("[FAIL]")]
    check(f"变异「{name}」后必须转红（{len(reds)} 条 FAIL、exit={mcode}）",
          mcode != 0 and len(reds) >= 1,
          "；".join(x[:90] for x in reds[:2]) or mout[-200:])

shutil.rmtree(tmpdir, ignore_errors=True)

print("\n" + "=" * 50)
print("累计实测寿命（v7.7）验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
