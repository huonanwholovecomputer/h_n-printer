# -*- coding: utf-8 -*-
"""验收 · v7.4「采购按用量分摊 + 打印机寿命（折旧）」（2026-10-25 用户需求）

判据四组：
  A. 静态接线 —— 数据模型（printers / purchaseBasis）、迁移与净化、三个界面入口
     （授权页台账 / 云端数据打印机列表 / 收支清算设备折旧）、已知机型映射、设置开关。
  B. 函数级实测（Node VM 里跑**真实 calcMonth**，传入手工构造的 usage）——
     · 默认仍是均摊（零回归）；切到按用量后 E 按各人页数比例分摊；
     · 折旧 = 页数 × (价格 ÷ 寿命页数)，按人页数分摊，Σ分摊 ≡ 该机折旧；
     · 恒等式依旧成立：ΣN ≡ 0（折旧不进转账）、ΣC ≡ ΣB ≡ Π = I − E − D；
     · 未归属成员的折旧不进任何人的账（depUnallocated）；退役机不计提；寿命 0 不产生 Infinity；
     · 「按用量但没有页数」自动退回均摊并标记 basisFallback。
  C. 真实渲染（加载整页脚本、真 fetch 桩、真实 renderDash）——
     · 收支清算出现「设备折旧 / 采购应摊 / 按用量」且金额正确；
     · 云端数据打印机列表能回答"这单是哪台机打的"，未登记机型也能显示已知机型名；
     · 授权页台账可增删改、一键登记两台已知机型并自动绑定设备号。
  D. 变异自证 —— 改坏折旧 / 分摊权重 / 已知机型映射，对应断言必须转红。

用法：python tests/verify_usage_depreciation.py
exit 0 = 全部通过。
"""
import io
import os
import re
import subprocess
import sys
import tempfile
import shutil

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
section("A. 静态接线（数据模型 / 迁移 / 三个界面入口）")

check("calcMonth 接受 opts.usage（缺省时完全不涉及折旧 —— 旧调用零回归）",
      "function calcMonth(m, opts) {" in src and "var usage = opts.usage || null;" in src)
check("采购分摊口径有 getter（默认 equal）",
      "function getPurchaseBasis() { return STORE.config.purchaseBasis === 'usage' ? 'usage' : 'equal'; }" in src)
check("migrateConfig 补 printers 数组 + purchaseBasis 默认 equal",
      "if (!Array.isArray(c.printers)) c.printers = [];" in src
      and "if (c.purchaseBasis !== 'usage') c.purchaseBasis = 'equal';" in src)
check("sanitizeStore 净化打印机（价格非法整条丢弃；寿命 0 丢弃但**累计实测模式**例外、同一设备只留一台）",
      "if (!(p.price >= 0)) { console.warn('导入：跳过价格非法的打印机', p.id); return false; }" in src
      and "if (!(p.life_pages > 0) && printerLifeState(p) !== 'accumulating') {" in src
      and "同一个设备绑了两台打印机" in src)
check("mergeStores 按 id 并集合并 printers（缺一台就会让那台机的折旧消失）",
      "(lc.printers || []).concat(cc.printers || [])" in src)
check("折旧算法唯一落点 printerDepCents（整数分、寿命 0 返回 0）",
      "function printerDepCents(p, pages) {" in src and "if (!p || life <= 0 || n <= 0) return 0;" in src)
check("已知机型表：霍楠 = HP LaserJet 1020 / 姚懿祥 = HP LaserJet MFP M232-M237",
      "'霍楠': 'HP LaserJet 1020'" in src and "'姚懿祥': 'HP LaserJet MFP M232-M237'" in src)
check("订单 → 打印机 归属走 printerForOrder（绑定设备 → 已知机型 → 单机兜底）",
      "function printerForOrder(o) {" in src
      and "var bound = printerByClient(cid);" in src
      and "if (!cid) {" in src)
check("订单 → 展示机型 printerNameForOrder（未登记也显示已知机型）",
      "function printerNameForOrder(o) {" in src)

# v7.6（2026-10-25 用户要求）：打印机台账 / 用量折旧 / 订单归属全部收拢到独立的
# 「🖨️ 打印机折旧」标签页 —— 云端数据页只讲云端收益与订单；授权页只管设备授权。
check("新标签页「🖨️ 打印机折旧」已接线（导航按钮 + tab 容器 + switchTab/renderAll）",
      'data-tab="printers"' in src and 'id="tab-printers"' in src
      and "else if (name === 'printers') { renderPrinters();" in src
      and "function renderPrinters() {" in src
      and "renderCloud(); renderPrinters(); renderHistory();" in src)
check("🚫 云端数据页不再渲染打印机区块（订单明细那张表也**不加**打印机列）",
      "printerUsageSectionHTML(filteredOrders)" not in src)
_ra = re.search(r"function renderAuth\(\) \{([\s\S]*?)^\}", src, re.M)
check("🚫 授权页不再渲染打印机台账（只剩本设备 + 云端设备列表）",
      _ra is not None and "printerAdminHTML" not in _ra.group(1)
      and "打印机台账" in _ra.group(1))
check("打印机折旧页：台账 + 用量折旧 + 订单→打印机三块都在 renderPrinters 里",
      "function renderPrinters() {" in src
      and "h += printerAdminHTML();" in src
      and "h += printerUsageSectionHTML(_cloudFilteredOrders());" in src
      and "function printerUsageSectionHTML(orders) {" in src)
check("台账：增删改 + 一键登记已知机型仍在",
      "function addPrinter() {" in src and "function updPrinter(id, field, val) {" in src
      and "function delPrinter(id) {" in src and "function seedKnownPrinters() {" in src)
check("新密码模块 orderprinter：PASSWORD_KEYS / DEFAULT_PASSWORDS / MODULE_NAMES / unlocked 齐全",
      re.search(r"var PASSWORD_KEYS = \[[^\]]*'orderprinter'", src) is not None
      and re.search(r"var DEFAULT_PASSWORDS = \{[^}]*orderprinter: ''", src) is not None
      and re.search(r"MODULE_NAMES = \{[^}]*orderprinter: '[^']+'", src) is not None
      and re.search(r"var unlocked = \{[^}]*orderprinter: false", src) is not None)
check("台账与「订单→打印机」共用同一把锁（两个表头 🔒 都调 togglePrinterLedgerLock）",
      "function _printerLedgerLockHTML()" in src and "function _printerAssignLockHTML()" in src
      and src.count("togglePrinterLedgerLock()") >= 2
      and "_printerAssignLockHTML()" in src)
check("锁定态不给下拉（printerPickerHTML 先判 unlocked.orderprinter）",
      re.search(r"function printerPickerHTML\(o, info\) \{[\s\S]{0,1200}?if \(!unlocked\.orderprinter\)", src) is not None)
# 2026-10-25 用户反馈：认不出机型的行只写了一句「可在右侧下拉指定」，却没渲染 select
# （指着一个不存在的控件）。现在未识别行也走同一个出口，解锁后必须给出真下拉。
check("未识别的订单行也走 printerPickerHTML（解锁时必须给真下拉，不许只写一句提示）",
      "printerPickerHTML(o, { name: row.name, registered: row.registered })" in src
      and "可在右侧下拉指定" not in src
      and re.search(r"if \(!listed\) \{ ph = '未指定 · 台账无机器'", src) is not None)
# 2026-10-25 用户反馈（第二句）：「不要把行高搞那么高」⇒ 这一格只能有一行，
# 提示一律同行内的小字 + title，绝不用 div 另起一行。
_pk = re.search(r"function printerPickerHTML\(o, info\) \{([\s\S]*?)\n\}", src)
check("「订单→打印机」单元格只排一行（printerPickerHTML 里没有 <div，提示放 title）",
      _pk is not None and "<div" not in _pk.group(1)
      and "margin-left:5px;white-space:nowrap" in _pk.group(1)
      and 'h += \'<td style="white-space:nowrap">\' + printerPickerHTML' in src)
# 2026-10-25 用户反馈（第三句）：「我这里是歪着的」⇒ td 是居中的，解锁态在下拉旁边
# 再挂任何 inline 元素都会把这一行的下拉整体顶偏 ⇒ 状态必须写进下拉自己的占位项。
check("解锁态下拉旁边不挂任何文字（状态写进占位项 + 橙色边框，保证每行下拉左边缘一致）",
      _pk is not None
      and re.search(r"h \+= '</select>';\s*\n\s*return h;", _pk.group(1)) is not None
      and "未识别 · 请指定" in _pk.group(1) and "border-color:var(--orange)" in _pk.group(1))
check("收支清算：🖨️ 设备折旧区块 + 采购分摊比例表",
      "🖨️ 设备折旧" in src and "分摊到人" in src and "本月出纸页数" in src)
check("设置页：采购分摊 均摊/按用量 单选（走 setPurchaseBasis）",
      'name="purchaseBasis" value="equal"' in src and 'value="usage"' in src
      and "function setPurchaseBasis(basis) {" in src)
check("用量统计：按月缓存 + 失败显式标记 + 重试（绝不把失败当 0 页）",
      "function ensureUsage(key, cb) {" in src and "_usageFailed[key] = true;" in src
      and "function retryUsage() {" in src)
check("只有 status='sent' 的子任务计入用量",
      "if ((f.status || '') !== 'sent') return;" in src)

# ══════════════════════════════════════════════════════════════
# B/C/D. Node VM 侧
# ══════════════════════════════════════════════════════════════

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const htmlPath = process.argv[2];
const html = fs.readFileSync(htmlPath, 'utf8');
const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const js = scripts.join('\n;\n');

const results = [];
const P = (n, c, d) => results.push([n, !!c, d || '']);
const MB = () => ({ // 新的一套沙箱
  console: { log() {}, warn() {}, error() {} },
  Math, JSON, Object, Array, String, Number, Boolean, Date, RegExp, Error, Promise,
  Set, Map, isNaN, isFinite, parseFloat, parseInt, encodeURIComponent, decodeURIComponent,
  setTimeout, clearTimeout, setInterval, clearInterval,
  DEFAULT_MEMBERS: [], DEFAULT_SCORE_CAP: 15, DEFAULT_RATE: 0.05,
  PASSWORD_KEYS: ['score', 'expense', 'income', 'transfer', 'settings', 'auth', 'orderstatus'],
  DEFAULT_PASSWORDS: {}, DEFAULT_PRICING: { simplex_price: 0.2, duplex_price: 0.3, cover_page_price: 0.1,
    delivery_percentages: {}, urgency_prices: {} },
  FINANCE_MODE: 'cloud', STORE: null, _authDevices: null,
});

/* ── 半截沙箱：只装「数据模型 → calcMonth」这一段（含打印机助手），做纯函数级断言 ── */
function calcSandbox() {
  const sb = MB();
  vm.createContext(sb);
  const start = js.indexOf('function migrateConfig(data)');
  const end = js.indexOf('/* ==================== 保存 ====================');
  if (start < 0 || end <= start) { console.log('SNIPPET_FAIL'); process.exit(3); }
  vm.runInContext(js.slice(start, end), sb);
  return sb;
}
const MEMBERS = [
  { id: 'ma', name: '成员A', color: '#4472C4' },
  { id: 'mb', name: '成员B', color: '#548235' },
];
function store2(records, extraCfg) {
  const cfg = Object.assign({ investRate: 0.05, scoreCap: 15, passwords: {}, members: MEMBERS,
                              printers: [], purchaseBasis: 'equal' }, extraCfg || {});
  return { config: cfg, records: Object.assign({ expenses: [], income: [], scores: [], transfers: [] }, records || {}),
           notes: {}, meta: { version: 0 } };
}
/* 500 页的一台机：¥1200 / 3000 页 = ¥0.40/页 */
const PRINTER = { id: 'p1', name: 'HP LaserJet 1020', model: 'HP LaserJet 1020', price: 1200,
                  life_pages: 3000, client_id: '', status: 'active' };
/* usage 契约（_usageFromOrders 产出的形状） */
function usageOf(pages, perMember, perMemberPrinter) {
  const u = { month: '2026-06', pages: 0, orders: 1, files: 1,
              pagesByMember: perMember || {}, pagesByMemberPrinter: perMemberPrinter || {},
              pagesByPrinter: {}, unmatchedPrinterPages: 0, unattributedPages: 0, fetchedAt: '' };
  Object.keys(u.pagesByMember).forEach(k => { u.pages += u.pagesByMember[k]; });
  Object.keys(u.pagesByMemberPrinter).forEach(k => {
    const pid = k.split('|')[1];
    u.pagesByPrinter[pid] = (u.pagesByPrinter[pid] || 0) + u.pagesByMemberPrinter[k];
  });
  if (pages != null) u.pages = pages;
  return u;
}

/* ═════ B1：默认均摊（零回归） ═════ */
{
  const sb = calcSandbox();
  sb.STORE = store2({ expenses: [{ id: 'e1', date: '2026-06-03', item: '纸', amount: 100, payer: 'ma' }] });
  const r = sb.calcMonth(sb.monthFromRecords('2026-06'));
  P('B1 无 usage 时仍是均摊（每人 ¥50.00）', r.perPersonShare.ma === 5000 && r.perPersonShare.mb === 5000,
    r.perPersonShare.ma + '/' + r.perPersonShare.mb);
  P('B1 无打印机时折旧为 0、口径标记为 equal',
    r.depTotal === 0 && r.depByMember.ma === 0 && r.basis === 'equal' && r.useUsage === false);
}

/* ═════ B2：按用量分摊 + 折旧（3:1） ═════ */
{
  const sb = calcSandbox();
  sb.STORE = store2({
    expenses: [{ id: 'e1', date: '2026-06-03', item: '纸', amount: 100, payer: 'ma' }],
    income: [{ id: 'i1', date: '2026-06-05', memberId: 'ma', amount: 200 }],
    transfers: [{ id: 't1', month: '2026-06', from: 'mb', to: 'ma', amount: 50 }],
  }, { purchaseBasis: 'usage', printers: [PRINTER] });
  const u = usageOf(400, { ma: 300, mb: 100 }, { 'ma|p1': 300, 'mb|p1': 100 });
  const r = sb.calcMonth(sb.monthFromRecords('2026-06'), { usage: u });
  P('B2 按用量：采购 ¥100 按 3:1 分成 ¥75 / ¥25',
    r.perPersonShare.ma === 7500 && r.perPersonShare.mb === 2500,
    r.perPersonShare.ma + '/' + r.perPersonShare.mb);
  P('B2 折旧：400 页 × ¥0.40 = ¥160，再按 3:1 分成 ¥120 / ¥40',
    r.depTotal === 16000 && r.depByMember.ma === 12000 && r.depByMember.mb === 4000,
    r.depTotal + ' / ' + r.depByMember.ma + '/' + r.depByMember.mb);
  const sumN = r.net.ma + r.net.mb;
  const sumB = (r.redist.ma - r.perPersonShare.ma - r.depByMember.ma) + (r.redist.mb - r.perPersonShare.mb - r.depByMember.mb);
  const sumC = r.actualPL.ma + r.actualPL.mb;
  const teamPL = r.totalInc - r.totalExp - r.depTotal;
  P('B2 Σ建议转账 ≡ 0（折旧不进转账指令）', sumN === 0, String(sumN));
  P('B2 Σ应得盈亏 ≡ Σ实际盈亏 ≡ Π = Σ代收 − Σ采购 − Σ折旧',
    sumB === teamPL && sumC === teamPL, sumB + ' / ' + sumC + ' / ' + teamPL);
  P('B2 团队盈亏 = 200 − 100 − 160 = −60 元', teamPL === -6000, String(teamPL));
  P('B2 打印机明细：页数 400、成本 ¥160、单页成本 ¥0.40',
    r.depByPrinter.length === 1 && r.depByPrinter[0].pages === 400
    && r.depByPrinter[0].cost === 16000 && Math.abs(r.depByPrinter[0].unit - 0.4) < 1e-9,
    JSON.stringify(r.depByPrinter[0] && { pages: r.depByPrinter[0].pages, cost: r.depByPrinter[0].cost }));
}

/* ═════ B3：未归属成员的折旧不进任何人的账 ═════ */
{
  const sb = calcSandbox();
  sb.STORE = store2({ expenses: [] }, { printers: [PRINTER] });
  const u = usageOf(400, { ma: 300 }, { 'ma|p1': 300 });
  u.pagesByPrinter = { p1: 400 };     // 另外 100 页归属不到成员
  const r = sb.calcMonth(sb.monthFromRecords('2026-06'), { usage: u });
  P('B3 只按「能归属到成员」的 300 页计提折旧（¥120）', r.depTotal === 12000, String(r.depTotal));
  P('B3 未归属的 100 页折旧单独汇总（¥40 / 100 页），不进任何成员',
    r.depUnallocated === 4000 && r.depPagesUnattributed === 100 && r.depByMember.mb === 0,
    r.depUnallocated + ' / ' + r.depPagesUnattributed);
}

/* ═════ B4：退役机 / 寿命 0 / 设备未登记 ═════ */
{
  const sb = calcSandbox();
  const retired = Object.assign({}, PRINTER, { id: 'p2', status: 'retired' });
  const noLife = Object.assign({}, PRINTER, { id: 'p3', life_pages: 0 });
  sb.STORE = store2({}, { printers: [retired, noLife] });
  const u = usageOf(400, { ma: 200, mb: 200 }, { 'ma|p2': 200, 'mb|p3': 200 });
  const r = sb.calcMonth(sb.monthFromRecords('2026-06'), { usage: u });
  P('B4 退役机与寿命未填的机器都不计提折旧（也绝不出现 Infinity）',
    r.depTotal === 0 && Number.isFinite(r.depTotal), String(r.depTotal));
}

/* ═════ B5：选择「按用量」但本月 0 页 → 退回均摊并标记 ═════ */
{
  const sb = calcSandbox();
  sb.STORE = store2({ expenses: [{ id: 'e1', date: '2026-06-03', item: '纸', amount: 100, payer: 'ma' }] },
                    { purchaseBasis: 'usage' });
  const r = sb.calcMonth(sb.monthFromRecords('2026-06'), { usage: usageOf(0, {}, {}) });
  P('B5 0 页时退回均摊（¥50/¥50）且 basisFallback=true',
    r.perPersonShare.ma === 5000 && r.perPersonShare.mb === 5000 && r.basisFallback === true,
    r.perPersonShare.ma + ' / fallback=' + r.basisFallback);
}

/* ═════ B6：全额结清时 实际盈亏 == 应得盈亏（含折旧） ═════ */
{
  const sb = calcSandbox();
  sb.STORE = store2({
    expenses: [{ id: 'e1', date: '2026-06-03', item: '纸', amount: 100, payer: 'ma' }],
    income: [{ id: 'i1', date: '2026-06-05', memberId: 'ma', amount: 200 }],
  }, { purchaseBasis: 'usage', printers: [PRINTER] });
  const u = usageOf(400, { ma: 300, mb: 100 }, { 'ma|p1': 300, 'mb|p1': 100 });
  const r1 = sb.calcMonth(sb.monthFromRecords('2026-06'), { usage: u });
  /* 把建议转账原样录成实际转账（每条都在当月、双方都是成员） */
  sb.STORE.records.transfers = r1.transfers.map((t, i) => ({
    id: 'tf' + i, month: '2026-06', from: t.from, to: t.to, amount: t.amount / 100, note: '' }));
  const r2 = sb.calcMonth(sb.monthFromRecords('2026-06'), { usage: u });
  const ok = ['ma', 'mb'].every(id =>
    r2.actualPL[id] === (r2.redist[id] - r2.perPersonShare[id] - r2.depByMember[id]));
  P('B6 建议转账全部照做后：实际盈亏 == 应得盈亏（含折旧，差额 0）', ok,
    JSON.stringify({ a: r2.actualPL, b: { ma: r2.redist.ma - r2.perPersonShare.ma - r2.depByMember.ma } }));
  P('B6 此时未结清 g ≡ 0', r2.settleGap.ma === 0 && r2.settleGap.mb === 0,
    r2.settleGap.ma + '/' + r2.settleGap.mb);
}

/* ═════ B7：订单 → 打印机（绑定设备 / 已知机型 / 单机兜底） ═════ */
{
  const sb = calcSandbox();
  sb._authDevices = [];
  sb.STORE = store2({}, { printers: [PRINTER] });   // 只登记了 1020，没绑设备
  const o1 = { received_client: 'DESKTOP-A-1', received_label: '霍楠的设备 | DESKTOP-A-1' };
  const o2 = { received_client: 'DESKTOP-B-2', received_label: '姚懿祥的设备 | DESKTOP-B-2' };
  const o3 = { received_client: '', received_label: '' };
  const n1 = sb.printerNameForOrder(o1);
  const n2 = sb.printerNameForOrder(o2);
  const n3 = sb.printerNameForOrder(o3);
  P('B7 霍楠的设备 → 命中已登记的 HP LaserJet 1020（registered=true）',
    n1.name === 'HP LaserJet 1020' && n1.registered === true, n1.name);
  P('B7 姚懿祥的设备 → 未登记也按已知机型显示 M232-M237（registered=false）',
    n2.name === 'HP LaserJet MFP M232-M237' && n2.registered === false, n2.name);
  P('B7 没有设备号的订单 → 只有一台在役机时归到它',
    n3.name === 'HP LaserJet 1020' && n3.registered === true, n3.name);

  /* 已登记机器优先于已知机型表（用户把 M232 也登记上之后，霍楠的单不会被抢走） */
  sb.STORE.config.printers.push({ id: 'p2', name: 'HP LaserJet MFP M232-M237', model: 'HP LaserJet MFP M232-M237',
    price: 1500, life_pages: 5000, client_id: 'DESKTOP-B-2', status: 'active' });
  const m1 = sb.printerNameForOrder(o1), m2 = sb.printerNameForOrder(o2);
  P('B7 两台都登记后：各自按绑定设备/机型对上，不串台',
    m1.name === 'HP LaserJet 1020' && m2.name === 'HP LaserJet MFP M232-M237' && m2.registered === true,
    m1.name + ' / ' + m2.name);
}

/* ═════ B9：收损耗费（共有）—— 使用者付给其他所有者，团队不承担 ═════ */
{
  const sb = calcSandbox();
  const co = { id: 'p2', name: 'HP LaserJet MFP M232-M237', model: 'HP LaserJet MFP M232-M237',
               price: 1500, life_pages: 5000, client_id: '', status: 'active',
               owner: 'shared', billing: 'wear' };   // ¥0.30/页，两人共有
  sb.STORE = store2({
    expenses: [{ id: 'e1', date: '2026-06-03', item: '纸', amount: 100, payer: 'ma' }],
    income: [{ id: 'i1', date: '2026-06-05', memberId: 'ma', amount: 200 }],
  }, { printers: [co] });
  const u = usageOf(200, { ma: 200 }, { 'ma|p2': 200 });   // 只有 ma 用了 200 页
  const r = sb.calcMonth(sb.monthFromRecords('2026-06'), { usage: u });

  P('B9 损耗费总额 = 200 页 × ¥0.30 = ¥60（不进团队成本）',
    r.wearTotal === 6000 && r.depTotal === 0, r.wearTotal + ' / depTotal=' + r.depTotal);
  P('B9 ma 自己那份 ¥30 不付现金，只付给 mb ¥30',
    r.wearByMember.ma.own === 3000 && r.wearByMember.ma.pay === 3000
    && r.wearTransfers.length === 1 && r.wearTransfers[0].from === 'ma' && r.wearTransfers[0].to === 'mb'
    && r.wearTransfers[0].amount === 3000,
    JSON.stringify(r.wearByMember.ma) + ' ' + JSON.stringify(r.wearTransfers));
  P('B9 损耗费净额零和：ma −¥30 / mb +¥30',
    r.wearNet.ma === -3000 && r.wearNet.mb === 3000, r.wearNet.ma + '/' + r.wearNet.mb);
  P('B9 团队盈亏不被损耗费影响：Π = 200 − 100 = ¥100',
    (r.bookPL.ma + r.bookPL.mb) === 10000, String(r.bookPL.ma + r.bookPL.mb));
  P('B9 ma 的应得盈亏 = 应得收入 − 采购应摊 − ¥30 = −¥30（他一个人用了整台机）',
    r.bookPL.ma === (r.redist.ma - r.perPersonShare.ma - 3000),
    r.bookPL.ma + ' vs ' + (r.redist.ma - r.perPersonShare.ma - 3000));
  P('B9 建议转账里含一条 kind=wear 的损耗费，且 Σnet ≡ 0',
    r.transfers.some(x => x.kind === 'wear') && (r.net.ma + r.net.mb) === 0,
    (r.net.ma + r.net.mb) + ' transfers=' + JSON.stringify(r.transfers.map(x => [x.kind, x.amount])));

  /* 把建议转账（含损耗费）全部记账 ⇒ 实际盈亏 == 应得盈亏 */
  sb.STORE.records.transfers = r.transfers.map((tt, i) => ({
    id: 'tf' + i, month: '2026-06', from: tt.from, to: tt.to, amount: tt.amount / 100, note: '' }));
  const r2 = sb.calcMonth(sb.monthFromRecords('2026-06'), { usage: u });
  P('B9 损耗费真的照做后：实际盈亏 == 应得盈亏（C = B）',
    ['ma', 'mb'].every(id => r2.actualPL[id] === r2.bookPL[id]),
    JSON.stringify(r2.actualPL) + ' vs ' + JSON.stringify(r2.bookPL));
  P('B9 此时未结清 g ≡ 0', r2.settleGap.ma === 0 && r2.settleGap.mb === 0,
    r2.settleGap.ma + '/' + r2.settleGap.mb);
}

/* ═════ B10：收损耗费（个人所有）—— 全额付给所有者 ═════ */
{
  const sb = calcSandbox();
  const personal = { id: 'p3', name: 'HP LaserJet 1020', model: 'HP LaserJet 1020',
                     price: 1000, life_pages: 2000, client_id: '', status: 'active',
                     owner: 'ma', billing: 'wear' };   // ¥0.50/页，ma 个人所有
  sb.STORE = store2({}, { printers: [personal] });
  const u1 = usageOf(100, { mb: 100 }, { 'mb|p3': 100 });   // mb 用了 100 页
  const r1 = sb.calcMonth(sb.monthFromRecords('2026-06'), { usage: u1 });
  P('B10 个人所有：使用者把 100% 损耗费付给所有者（mb → ma ¥50）',
    r1.wearTransfers.length === 1 && r1.wearTransfers[0].from === 'mb' && r1.wearTransfers[0].to === 'ma'
    && r1.wearTransfers[0].amount === 5000,
    JSON.stringify(r1.wearTransfers));

  const u2 = usageOf(100, { ma: 100 }, { 'ma|p3': 100 });   // 所有者自己用
  const r2 = sb.calcMonth(sb.monthFromRecords('2026-06'), { usage: u2 });
  P('B10 所有者自己用：不产生任何损耗费（自己那一份=全额）',
    r2.wearTransfers.length === 0 && r2.wearNet.ma === 0 && r2.wearByMember.ma.own === 5000,
    JSON.stringify(r2.wearTransfers) + ' own=' + r2.wearByMember.ma.own);
}

/* ═════ B11：一键登记已知机型的预设（现场口径） ═════ */
{
  const sb = calcSandbox();
  sb.STORE = store2({}, { printers: [] });
  sb.STORE.config.members.push({ id: 'my', name: '闫承翰', color: '#ED7D31' });
  sb.save = function () {};
  sb.renderPrinters = function () {};
  sb.invalidateUsage = function () {};
  sb.toast = function () {};
  sb._authDevices = [];
  sb.unlocked = { orderprinter: true };
  sb.seedKnownPrinters();
  const list = sb.STORE.config.printers;
  const p1020 = list.filter(x => String(x.name).indexOf('1020') >= 0)[0];
  const pM232 = list.filter(x => String(x.name).indexOf('M232') >= 0)[0];
  P('B11 一键登记出两台机器', list.length === 2, JSON.stringify(list.map(x => x.name)));
  P('B11 1020 → 所有者 闫承翰 / 团队共担（没进过采购账）',
    p1020 && p1020.owner === 'my' && p1020.billing === 'team',
    p1020 && (p1020.owner + '/' + p1020.billing));
  P('B11 M232 → 共有 / 收损耗费（三人均摊买下）',
    pM232 && pM232.owner === 'shared' && pM232.billing === 'wear',
    pM232 && (pM232.owner + '/' + pM232.billing));
}

/* ═════ B8：migrateConfig / sanitizeStore 的打印机卫生 ═════ */
{
  const sb = calcSandbox();
  const cfg = sb.migrateConfig({ config: { printers: [
      { id: 'p1', name: 'A', price: 100, life_pages: 1000, client_id: 'c1' },
      { id: 'p2', name: 'B', price: -5, life_pages: 0, client_id: 'c1' } ] } });
  P('B8 migrateConfig 保留合法打印机、把 purchaseBasis 默认成 equal',
    cfg.config.printers.length >= 1 && cfg.config.purchaseBasis === 'equal',
    JSON.stringify(cfg.config.printers));
  const clean = sb.sanitizeStore({ config: { members: MEMBERS, printers: [
      { id: 'p1', name: 'A', price: 100, life_pages: 1000, client_id: 'c1' },
      { id: 'p2', name: 'B', price: 100, life_pages: 0, client_id: 'c1' },
      { id: 'p3', name: 'C', price: 100, life_pages: 500, client_id: 'c1' } ] },
    records: { expenses: [], income: [], scores: [], transfers: [] }, notes: {}, meta: { version: 0 } });
  P('B8 sanitizeStore 丢掉寿命 0 的机器、同一设备的第二台被忽略（避免订单归属歧义）',
    clean.config.printers.length === 1 && clean.config.printers[0].id === 'p1',
    JSON.stringify(clean.config.printers.map(p => p.id)));
}

/* ═════ C：真实渲染（整页脚本 + fetch 桩 + 真实 renderDash） ═════ */
(async () => {
  const els = {};
  function symbolQ(k) { return typeof k === 'symbol'; }
  function stub() {
    const store = {};
    return new Proxy(function () {}, {
      get(t, k) {
        if (k === 'innerHTML' || k === 'textContent' || k === 'value') return store[k] || '';
        if (k === 'classList') return { add() {}, remove() {}, contains() { return false; }, toggle() {} };
        if (k === 'style') return {};
        if (k === 'dataset') return {};
        if (k === 'children') return [];
        if (k === 'files') return [];
        if (k === Symbol.toPrimitive) return () => '';
        if (k === 'toString' || k === 'valueOf') return () => '';
        if (symbolQ(k)) return undefined;
        return stub();
      },
      set(t, k, v) { store[k] = v; return true; },
      apply() { return stub(); },
    });
  }
  function proxied(fn) {
    return new Proxy({}, {
      get(t, k) {
        if (k === Symbol.toPrimitive) return () => '';
        if (k === 'toString' || k === 'valueOf') return () => '';
        if (symbolQ(k) || k === 'then' || k === 'toJSON') return undefined;
        if (fn[k]) return fn[k];
        return stub();
      },
      set() { return true; },
    });
  }
  const documentProto = {
    querySelector: (s) => (els[s] = els[s] || stub()),
    querySelectorAll: () => [],
    getElementById: (id) => (els['#' + id] = els['#' + id] || stub()),
    createElement: () => stub(), createTextNode: () => stub(),
    addEventListener: () => {}, removeEventListener: () => {},
    readyState: 'complete', documentElement: stub(), head: stub(), body: stub(),
  };
  const document = proxied(documentProto);
  const storage = { getItem: () => null, setItem() {}, removeItem() {} };
  const windowProto = {
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    addEventListener: () => {}, removeEventListener: () => {},
    location: { search: '', href: 'http://localhost/', origin: 'http://localhost' },
    innerWidth: 1400, innerHeight: 900, devicePixelRatio: 1,
    localStorage: storage, sessionStorage: storage, document: document,
  };

  /* fetch 桩：只认「按月统计用量」那条请求，别的都返回失败（本用例不涉及） */
  const MONTH_ORDERS = [
    { order_id: 1, order_number: 'HN20260601-0001', created_at: '2026-06-01 10:00:00', openid: 'oid1',
      owner_name: '', received_client: 'DESKTOP-A-1', received_label: '霍楠的设备 | DESKTOP-A-1',
      status: 'sent', is_admin_print: 0, total_price: 60,
      files: [{ status: 'sent', total_pages: 200, total_price: 60 }] },
    { order_id: 2, order_number: 'HN20260602-0002', created_at: '2026-06-02 11:00:00', openid: 'oid2',
      owner_name: '', received_client: 'DESKTOP-B-2', received_label: '姚懿祥的设备 | DESKTOP-B-2',
      status: 'sent', is_admin_print: 0, total_price: 30,
      files: [{ status: 'sent', total_pages: 100, total_price: 30 }] },
  ];
  const fetchStub = (url) => {
    if (String(url).indexOf('/admin/statistics/revenue') >= 0) {
      return Promise.resolve({ json: () => Promise.resolve({ success: true, data: { orders: MONTH_ORDERS } }) });
    }
    return Promise.resolve({ json: () => Promise.resolve({ success: false }) });
  };

  const sandbox = {
    console, Math, JSON, Object, Array, String, Number, Boolean, Date, RegExp, Error, Promise,
    Set, Map, WeakMap, isNaN, isFinite, parseFloat, parseInt, encodeURIComponent, decodeURIComponent,
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: (f) => { try { f(0); } catch (e) {} return 0; },
    cancelAnimationFrame: () => {},
    document, window: proxied(windowProto), localStorage: storage, sessionStorage: storage,
    location: windowProto.location, navigator: { userAgent: 'node', clipboard: {} },
    fetch: fetchStub,
    getComputedStyle: () => ({ colorScheme: 'light', getPropertyValue: () => '' }),
    FINANCE_MODE: 'cloud',
  };
  sandbox.globalThis = sandbox; sandbox.self = sandbox;
  vm.createContext(sandbox);
  try { vm.runInContext(js, sandbox, { filename: 'settlement.js' }); }
  catch (e) { console.log('LOAD_FAIL: ' + e.message); process.exit(3); }

  vm.runInContext(`
    STORE = {
      config: {
        investRate: 0.05, scoreCap: 15, passwords: {},
        members: [ { id:'ma', name:'霍楠', color:'#4472C4' }, { id:'mb', name:'姚懿祥', color:'#548235' } ],
        memberBindings: [ { openid:'oid1', memberId:'ma' }, { openid:'oid2', memberId:'mb' } ],
        purchaseBasis: 'usage',
        printers: [ { id:'p1', name:'HP LaserJet 1020', model:'HP LaserJet 1020', price:1200,
                      life_pages:3000, client_id:'DESKTOP-A-1', status:'active',
                      owner:'shared', billing:'team' },
                    { id:'p2', name:'HP LaserJet MFP M232-M237', model:'HP LaserJet MFP M232-M237',
                      price:1500, life_pages:5000, client_id:'', status:'active',
                      owner:'shared', billing:'wear' } ]
      },
      records: {
        expenses: [ { id:'e1', date:'2026-06-03', item:'A4纸', amount:100, payer:'ma' } ],
        income: [ { id:'i1', date:'2026-06-05', memberId:'ma', amount:200 } ],
        scores: [ { id:'s1', date:'2026-06-05', memberId:'ma', item:'打印', score:10 } ],
        transfers: []
      },
      notes: {}, meta: { version: 0 }
    };
    currentKey = '2026-06';
    currentTab = 'dash';
    unlocked.transfer = true; unlocked.auth = true;
    renderDash();
  `, sandbox);
  await new Promise(r => setTimeout(r, 120));   // 等 fetch 桩 → ensureUsage → 重绘
  let dash = els['#tab-dash'].innerHTML || '';

  P('C1 收支清算渲染出「🖨️ 设备折旧」区块', dash.indexOf('设备折旧') >= 0);
  P('C2 用量口径生效：KPI 副行显示「按用量分摊 · 共 300 页」',
    dash.indexOf('按用量分摊 · 共 300 页') >= 0,
    (dash.match(/按用量分摊[^<]*/) || [''])[0]);
  P('C3 采购按用量分摊：霍楠 200 页 / 姚懿祥 100 页 → ¥66.67 / ¥33.33',
    dash.indexOf('−¥66.67') >= 0 && dash.indexOf('−¥33.33') >= 0,
    (dash.match(/−¥6[0-9.]+|−¥3[0-9.]+/g) || []).join(','));
  P('C4 折旧按机器计提：200 页 × ¥0.40 = ¥80.00 归霍楠（姚懿祥那台未登记 ⇒ 不提）',
    dash.indexOf('−¥80.00') >= 0,
    (dash.match(/−¥[0-9.]+/g) || []).slice(0, 6).join(','));
  P('C5 设备折旧表列出机器名与「所有者 / 计费方式」',
    dash.indexOf('HP LaserJet 1020') >= 0 && dash.indexOf('团队共担') >= 0);
  P('C6 经营损益多出「− 设备折旧」列', dash.indexOf('− 设备折旧') >= 0);
  P('C7 实际盈亏表也含「− 设备折旧」列', dash.indexOf('− 设备折旧') >= 0);
  P('C5b 收损耗费的机器在区块里标出「收损耗费」与补偿对象',
    dash.indexOf('收损耗费') >= 0 && dash.indexOf('损耗费（不进团队成本）') >= 0);
  P('C5c 共同出资机的损耗费进入「资金重分配」的独立损耗费组',
    dash.indexOf('② 损耗费') >= 0 && dash.indexOf('把他们的那份付给他们') >= 0);
  P('C8 结算公式块更新到 10 步（含设备折旧与损耗费）',
    dash.indexOf('设备折旧 <code>Dᵢ</code>') >= 0 || dash.indexOf('ΣDᵢ = D') >= 0);

  /* 云端数据「🖨️ 打印机」区块：直接调渲染函数（不必跑整页图表） */
  const cloudHTML = vm.runInContext('printerUsageSectionHTML(' + JSON.stringify(MONTH_ORDERS) + ')', sandbox);
  P('C9 打印机列表：霍楠的 200 页归 1020、姚懿祥的 100 页显示已知机型 M232',
    cloudHTML.indexOf('HP LaserJet 1020') >= 0 && cloudHTML.indexOf('HP LaserJet MFP M232-M237') >= 0);
  P('C10 订单级明细回答「这单是哪台机打的」', cloudHTML.indexOf('订单 → 打印机') >= 0
    && cloudHTML.indexOf('HN20260601-0001') >= 0 && cloudHTML.indexOf('HN20260602-0002') >= 0);
  P('C11 已耗寿命百分比：200/3000 = 6.7%', cloudHTML.indexOf('6.7%') >= 0,
    (cloudHTML.match(/[0-9.]+%/g) || []).join(','));
  P('C12 云端打印机列表标出「所有者 / 计费方式」',
    cloudHTML.indexOf('团队共担') >= 0 && cloudHTML.indexOf('收损耗费') >= 0);

  /* 订单→打印机：成员列改为普通文字（2026-10-25 用户要求）+ 打印机列表头带锁 */
  P('C12b 订单→打印机：成员列不再用彩色底纹（无 member-tag），保持普通文字',
    cloudHTML.indexOf('member-tag') < 0 && cloudHTML.indexOf('霍楠') >= 0);
  P('C12c 「打印机」列表头带锁定开关（默认 🔒，与台账共用一把锁）',
    cloudHTML.indexOf('st-lock') >= 0 && cloudHTML.indexOf('togglePrinterLedgerLock()') >= 0);

  /* ── 2026-10-25 用户反馈：认不出机型的单只写了「可在右侧下拉指定」却没有下拉 ──
     这里用一个「接单设备为空 + 台账有两台在役机」的订单构造"未识别"行（不塞进 MONTH_ORDERS，
     免得改掉上面 C2/C3/C4 的 300 页口径）。 */
  const UNMATCHED = [ { order_id: 4, order_number: 'IN20261025-0004', created_at: '2026-10-25 10:00:00',
    owner_name: '', received_client: '', received_label: '', status: 'sent', is_admin_print: 0, total_price: 0.6,
    files: [ { status: 'sent', total_pages: 2, total_price: 0.6 } ] } ];
  const umLocked = vm.runInContext('printerUsageSectionHTML(' + JSON.stringify(UNMATCHED) + ')', sandbox);
  /* 取出该订单行「打印机」那一格（第 5 列）的 HTML：行高相关的断言都要落在这格里 */
  function printerCell(h, num) {
    const tr = (h.match(new RegExp('<tr>(?:(?!</tr>)[\\s\\S])*?' + num + '[\\s\\S]*?</tr>')) || [''])[0];
    const tds = []; const re = /<td[^>]*>([\s\S]*?)<\/td>/g; let m;
    while ((m = re.exec(tr))) tds.push(m[1]);
    return tds[4] || '';
  }
  const cLocked = printerCell(umLocked, 'IN20261025-0004');
  P('C12d 未识别订单（锁定态）：显示未识别 + 解锁说明在 title 里，且不给下拉（防误触）',
    cLocked.indexOf('未识别') >= 0 && cLocked.indexOf('后可在这一格下拉指定') >= 0
    && cLocked.indexOf('<select') < 0, cLocked);
  P('C12d2 锁定态这一格也只有一行（<div> 会撑高行高）', cLocked.indexOf('<div') < 0, cLocked);
  vm.runInContext('unlocked.orderprinter = true;', sandbox);
  const umOpen = vm.runInContext('printerUsageSectionHTML(' + JSON.stringify(UNMATCHED) + ')', sandbox);
  P('C12e 未识别订单（解锁态）：单元格里**真的**有下拉，两台已登记机型都可选',
    umOpen.indexOf('<select') >= 0 && umOpen.indexOf('>HP LaserJet 1020</option>') >= 0
    && umOpen.indexOf('>HP LaserJet MFP M232-M237</option>') >= 0
    && umOpen.indexOf("setOrderPrinter('4',this.value)") >= 0,
    (umOpen.match(/<select[\s\S]{0,240}?<\/select>/) || [''])[0]);
  const cOpen = printerCell(umOpen, 'IN20261025-0004');
  P('C12e2 解锁态这一格只有一个下拉：状态写在占位项里，select 之后不再挂任何文字（否则整列会歪）',
    cOpen.indexOf('<select') >= 0 && cOpen.indexOf('未识别 · 请指定') >= 0
    && cOpen.indexOf('<div') < 0 && cOpen.trim().slice(-9) === '</select>', cOpen);
  P('C12f 「可在右侧下拉指定」这种指向空处的文案已绝迹（源码 + 渲染产物都没有）',
    umOpen.indexOf('可在右侧下拉指定') < 0 && html.indexOf('可在右侧下拉指定') < 0);
  /* 指定之后必须立刻生效：归属命中那台机、折旧按 2 页计提（2 × ¥0.40 = ¥0.80） */
  vm.runInContext('currentTab = "printers"; save = function(){}; invalidateUsage = function(){};'
    + ' renderPrinters = function(){}; toast = function(){};', sandbox);
  vm.runInContext('setOrderPrinter(4, "p1")', sandbox);
  const umAssigned = vm.runInContext('printerUsageSectionHTML(' + JSON.stringify(UNMATCHED) + ')', sandbox);
  P('C12g 解锁后指定 1020：归属立即生效（不再未识别，2 页折旧 −¥0.80）',
    umAssigned.indexOf('未识别') < 0 && umAssigned.indexOf('HP LaserJet 1020') >= 0
    && umAssigned.indexOf('−¥0.80') >= 0,
    (umAssigned.match(/−¥[0-9.]+/g) || []).join(','));
  vm.runInContext('unlocked.orderprinter = false;', sandbox);   // 下面的台账用例要从锁定态起

  /* 打印机台账：v7.6 起从「🔒 授权」搬到「🖨️ 打印机折旧」，编辑需解锁 orderprinter */
  const lockedAdmin = vm.runInContext('printerAdminHTML()', sandbox);
  P('C13a 台账默认锁定：只读展示、不给输入框与改按钮，但给表头 🔒 与解锁入口',
    lockedAdmin.indexOf('printer-name-') < 0 && lockedAdmin.indexOf('updPrinter(') < 0
    && lockedAdmin.indexOf('addPrinter()') < 0
    && lockedAdmin.indexOf('st-lock') >= 0 && lockedAdmin.indexOf('unlockFlow(') >= 0
    && lockedAdmin.indexOf('HP LaserJet 1020') >= 0);
  vm.runInContext('unlocked.orderprinter = true;', sandbox);
  const adminHTML = vm.runInContext('printerAdminHTML()', sandbox);
  P('C13 解锁后台账列出机器并可编辑价格/寿命/默认设备',
    adminHTML.indexOf('HP LaserJet 1020') >= 0 && adminHTML.indexOf('updPrinter(') >= 0
    && adminHTML.indexOf('value="1200"') >= 0 && adminHTML.indexOf('value="3000"') >= 0);
  P('C13b 归属/计费合并成一个下拉（不再有独立的 owner / billing 两个下拉）',
    adminHTML.indexOf('updPrinterBilling(') >= 0
    && adminHTML.indexOf("updPrinter('p1','owner'") < 0
    && adminHTML.indexOf("updPrinter('p1','billing'") < 0);
  P('C14 台账显示每页成本 ¥0.4000/页', adminHTML.indexOf('¥0.4000') >= 0,
    (adminHTML.match(/¥[0-9.]+/g) || []).slice(0, 4).join(','));
  P('C15 两台已知机型都已登记时，不再显示「登记已知机型」入口',
    adminHTML.indexOf('登记已知机型') < 0);

  let bad = 0;
  for (const [n, c, d] of results) {
    if (!c) bad++;
    console.log(`[${c ? 'PASS' : 'FAIL'}] ${n}` + (d ? '  -- ' + d : ''));
  }
  process.exit(bad ? 1 : 0);
})();
"""


def run_harness(html_path, workdir):
    hp = os.path.join(workdir, "harness.js")
    open(hp, "w", encoding="utf-8").write(HARNESS)
    r = subprocess.run(["node", hp, html_path], capture_output=True, text=True, encoding="utf-8")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


tmpdir = tempfile.mkdtemp(prefix="hn_usage_dep_")
code, out_text = run_harness(HTML, tmpdir)
print(out_text.rstrip())
check("函数级 + 渲染级断言全部通过", code == 0, f"exit={code}")

# ══════════════════════════════════════════════════════════════
# D. 变异自证
# ══════════════════════════════════════════════════════════════
section("D. 变异自证（改坏 → 必须变红）")

MUTANTS = [
    ("折旧不计入实际盈亏",
     " - (depByMember[mem.id] || 0);",
     " ;"),
    ("采购不分摊（退回均摊）",
     "var purchaseWeights = members.map(function (mem) { return useUsage ? (usagePages[mem.id] || 0) : 1; });",
     "var purchaseWeights = members.map(function () { return 1; });"),
    ("已知机型映射错位",
     "'霍楠': 'HP LaserJet 1020', '姚懿祥': 'HP LaserJet MFP M232-M237'",
     "'霍楠': 'HP LaserJet MFP M232-M237', '姚懿祥': 'HP LaserJet 1020'"),
    ("折旧不按人分摊（只按整机计）",
     "members.forEach(function (mem) { depByMember[mem.id] += alloc[mem.id] || 0; });",
     "members.forEach(function (mem) { depByMember[mem.id] += Math.round(cost / members.length); });"),
]
for idx, (name, old, new) in enumerate(MUTANTS):
    if old not in src:
        check(f"变异「{name}」：锚点存在（锚点没找到 ⇒ 变异自证空转）", False, old[:60])
        continue
    mp = os.path.join(tmpdir, f"mutant_{idx}.html")
    open(mp, "w", encoding="utf-8").write(src.replace(old, new, 1))
    mcode, mout = run_harness(mp, tmpdir)
    reds = [ln for ln in mout.splitlines() if ln.startswith("[FAIL]")]
    check(f"变异「{name}」后必须转红（{len(reds)} 条 FAIL、exit={mcode}）",
          mcode != 0 and len(reds) >= 1,
          "；".join(x[:80] for x in reds[:2]) or mout[-200:])

shutil.rmtree(tmpdir, ignore_errors=True)

print("\n" + "=" * 50)
print("采购按用量分摊 + 打印机折旧 验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
