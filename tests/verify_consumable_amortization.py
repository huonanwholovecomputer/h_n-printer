# -*- coding: utf-8 -*-
"""验收 · v7.9「长期耗材（A4纸 / 硒鼓）按月摊销」（2026-10-27 用户需求）

需求原话：「A4纸和硒鼓是能够长期使用的，但是现在的按打印比例分配采购，导致了采购月的
分配严重失调……1. 算出每张纸的成本，然后累积，每月给A4纸采购者原价返回；
2. 算出硒鼓的'寿命损耗'，即：这个月打印的页数/硒鼓一共能打的页数，进行折旧计算，
每月给硒鼓采购者原价返回。」
补充：「采购部分需要有常驻的'硒鼓'和'A4纸'类型，选择后，会多出一列，需要填写A4纸的
张数和硒鼓的使用寿命（能打印多少），用于清算。」

判据四组：
  A. 静态接线 —— 数据类型（kind / units）、常驻枚举、记账页「类型 + 数量」列、
     calcMonth 用 opts.amort 替换整笔采购、用量逐日 / 张数字段、摊销区块与重试入口、
     结算公式与版本说明。
  B. 纯函数实测（Node VM 里跑**真实的** consumableAmort / calcMonth / _usageFromOrders /
     consumableAmortSectionHTML）：
     · 购买月只算**采购日之后**的用量（10-21 的纸不算 10-22 买入的那批）；
     · A4纸按「出纸张数」、硒鼓按「打印页数」分别摊销；
     · 多批同类型先进先出（早买的先用完、后买的接手）；
     · 每月金额用累计取整差 ⇒ 用完时 Σ 恰好等于采购价（3 份 / 3 个月也不漂移）；
     · 用超库存不超收、不出现 NaN / Infinity，超支显式记 uncovered；
     · calcMonth 集成：整笔耗材不再计入购买月、摊销记在采购人名下、三条恒等式不变；
     · 不传 amort ⇒ 旧口径零回归；传了 amort 但 rows 为空（用量未取到）⇒ 整笔也不计入；
     · 没填数量的耗材**暂按普通采购全额计入**（钱不凭空消失）；采购人不在名单的照旧按孤儿处理；
     · 用量口径：只算 sent、张数取 total_sheets（含份数）、逐日聚合；
     · 摊销区块的四类文案（正常 / 缺数量 / 无采购 / 未就绪）。
  C. 变异自证 —— 把「整笔采购替换 / 累计取整差 / 采购日边界 / 先进先出 / 封顶」任一改坏，
     对应断言必须转红。

用法：python tests/verify_consumable_amortization.py
exit 0 = 全部通过。
"""
import io
import os
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
section("A. 静态接线（类型枚举 / 记账列 / 摊销算法 / 结算合并 / 版本说明）")

check("常驻枚举 CONSUMABLE_KIND：A4纸按张、硒鼓按页",
      "var CONSUMABLE_KIND = {" in src and "paper: { label: 'A4纸', unit: '张'" in src
      and "toner: { label: '硒鼓', unit: '页'" in src
      and "EXPENSE_KIND_OPTIONS = [['other', '普通采购'], ['paper', 'A4纸'], ['toner', '硒鼓']]" in src)
check("旧记录按项目名自动识别（A4纸/打印纸/复印纸 → paper；硒鼓/碳粉/墨粉 → toner）",
      "/(硒鼓|碳粉|墨粉)/.test(s)" in src
      and "/(A4\\s*纸|A4\\s*打印纸|A4\\s*复印纸|打印纸|复印纸|纸张)/i.test(s)" in src)
check("「能参与摊销」的唯一判据 isAmortizableExpense（数量 > 0 / 金额 > 0 / 日期合法 / 采购人仍是成员）",
      "function isAmortizableExpense(e) {" in src and "if (expenseUnits(e) <= 0 || expenseAmountCents(e) <= 0) return false;" in src
      and "return !!getMember(e.payer);" in src)
check("摊销金额走整数分 expenseAmountCents（虚拟行自带 amountCents，避免浮点尾差）",
      "function expenseAmountCents(e) {" in src and "if (e && e.amountCents != null) return Math.max(0, Math.round(e.amountCents));" in src)
check("纯函数 consumableAmort + 月份范围 monthRangeKeys",
      "function consumableAmort(purchases, usageByMonth, targetKey) {" in src
      and "function monthRangeKeys(startKey, endKey) {" in src)
check("逐日 FIFO：同一类型按采购日期排队、每天用量先喂最早的批次",
      "var queue = list.slice(), qi = 0, active = [];" in src
      and "while (qi < queue.length && queue[qi].date <= d) active.push(queue[qi++]);" in src
      and "var take = Math.min(lot.remaining, need);" in src)
check("采购日之前不算这批的用量（逐日过滤）",
      "var days = Object.keys(dayMap).filter(function (d) { return d >= firstDate; }).sort();" in src)
check("每月金额 = 累计取整差（Σ各月恰好 = 采购价，不逐月四舍五入漂移）",
      "var cents = Math.round(lot.priceCents * cum / lot.units);" in src
      and "lot.months[mk] = { units: lot.perMonth[mk] || 0, cents: cents - prevCents, cumUnits: cum };" in src)
check("用超库存记 uncovered（不凭空计费；v7.10 起单机桶另记 uncoveredPrinter）",
      "self.uncovered[kind][mk] = (self.uncovered[kind][mk] || 0) + need;" in src
      and "uncoveredPrinter[scope] = self.uncoveredPrinter[scope] || {};" in src)
check("calcMonth 接收 opts.amort：能摊销的整笔采购不计入本月，只合并摊销行",
      "var amort = opts.amort || null;" in src
      and "exps = exps.filter(function (e) { return !isAmortizableExpense(e); });" in src
      and "exps.push({ id: a.id, date: m.key + '-01', item: a.item, amount: a.cents / 100," in src
      and "payer: a.payer, virtual: true, kind: a.kind, units: a.units });" in src)
check("calcMonth 返回 expenses（计费行）与 amort（摊销明细），页面不另算一份",
      "expenses: exps, amort: amort };" in src)
check("monthFromRecords 原样带上 kind / units（旧写法只挑 5 个字段会把耗材数量丢掉）",
      "payer: r.payer, kind: r.kind, units: r.units });" in src)
check("结算入口 calcMonthWithUsage 带上 amort（loading / failed 时 rows 为空、state 明确）",
      "function calcMonthWithUsage(key) {" in src
      and "var st = amortStateOfMonth(key);" in src
      and "amort = consumableAmort(consumablePurchases(), um, key);" in src)
check("用量未就绪 ⇒ 退回旧口径（整笔采购照旧计入），绝不让整笔钱静默消失；r.amort 仍带 state 供提示",
      "amort: (st === 'ready' || st === 'none') ? amort : null" in src
      and "r.amort = amort;" in src)
check("摊销区块早退同时看「本月采购」「摊销批次」与「取数中」（购买月之后的月份不能显示“还没有采购”）",
      "if (!raw.length && !(am && am.lots && am.lots.length) && !_busy) {" in src
      and "var _busy = !!(am && (am.state === 'loading' || am.state === 'failed'));" in src)
check("历史记录对未就绪月份标「耗材暂按整笔」",
      "耗材暂按整笔" in src and "耗材暂按整笔" in src.split("function renderHistory")[1])
check("用量未就绪时禁止一键填入建议转账（添加转账也不预填金额）",
      "if (_st === 'loading' || _st === 'failed') {" in src
      and "先点红条「重试」再填" in src
      and "if (_st === 'ready' || _st === 'none') {" in src)
check("无 created_at 的旧订单不硬塞月末：单独记 undated（不把购买日之前的用量摊进这批）",
      "u.undatedPages = Math.max(0, u.pages - pDay);" in src
      and "u.undatedSheets = Math.max(0, u.sheets - sDay);" in src
      and "undatedPages: 0, undatedSheets: 0," in src)
check("非法/超范围月份直接判失败并回调（不让 ensureUsageMany 串行链永久卡死）",
      "if (!range) {" in src and "_usageFailed[key] = true;" in src
      and "var token = ++_usageReqSeq;" in src and "if (_usageLoading[key] !== token) return;" in src)
check("invalidateUsage 作废在飞请求并清 loading（旧口径不会写回缓存）",
      "_usageGen++;" in src and "_usageLoading = {};" in src
      and "if (gen !== _usageGen) {" in src)
check("类型自动识别收紧：裸 A4 / 墨盒服务费不误判为长期耗材",
      "if (/(硒鼓|碳粉|墨粉)/.test(s)) return 'toner';" in src
      and "if (/(A4\\s*纸|A4\\s*打印纸|A4\\s*复印纸|打印纸|复印纸|纸张)/i.test(s)) return 'paper';" in src)
check("移除长期耗材采购人时提示「剩余未摊销不再返还」",
      "的采购人就是该成员" in src and "剩余未摊销的采购款既不会返还给他" in src)
check("摊销月份范围 / 就绪状态 / 串行补取 / 重试",
      "function amortNeededMonths(key) {" in src and "function amortStateOfMonth(key) {" in src
      and "function kickAmortUsage(key, rerender) {" in src and "function retryAmortUsage() {" in src)
check("用量多记 sheets 与逐日表（A4纸摊销的原料）",
      "pagesByDay: {}, sheetsByDay: {}," in src
      and "sheets += _fileSheets(f);" in src
      and "u.pagesByDay[day] = (u.pagesByDay[day] || 0) + pages;" in src
      and "u.sheetsByDay[day] = (u.sheetsByDay[day] || 0) + sheets;" in src)
check("无 created_at 的老订单单独记 undated（不硬塞月末、不把购买日之前的用量摊进这批）",
      "u.undatedPages = Math.max(0, u.pages - pDay);" in src
      and "u.undatedSheets = Math.max(0, u.sheets - sDay);" in src)
check("摊销区块对 undated 用量显式提示「无法按采购日摊销，未计入」",
      "无法按采购日摊销，这部分未计入" in src)
check("记账页：类型选择 updExpType + 数量列（仅当本月有长期耗材时出现）",
      "function updExpType(id, val) {" in src
      and "var hasConsumable = m.expenses.some(isConsumableExpense);" in src
      and "onchange=\"updExpType(\\'' + eid + '\\',this.value)\"" in src
      and "填数量后按月摊销" in src)
check("记账页填数量后重绘单价提示；切回普通采购清掉 units",
      "if (field === 'units' && monthKeyOf(r.date) === currentKey) renderEntry();" in src
      and "if (r.kind === 'other') delete r.units;" in src)
check("收支清算：采购明细改用 r.expenses（含摊销行）并给两类标记",
      "(r.expenses || []).forEach(function (e) {" in src
      and "长期耗材摊销" in src and "耗材 · 未摊销" in src)
check("收支清算：长期耗材摊销区块（总量/单价/累计/已摊销/本月用量/本月摊销/剩余/返还对象）",
      "function consumableAmortSectionHTML(r, m) {" in src
      and "本月按垫付返还给采购人" in src
      and "本月摊销合计" in src and "剩余未摊销" in src)
check("摊销加载/失败有明确提示与重试；未就绪时暂按整笔计入（绝不静默少记）",
      "正在统计长期耗材（A4纸 / 硒鼓）的逐日用量" in src
      and "暂按整笔采购计入" in src and "onclick=\"retryAmortUsage()\"" in src)
check("两条「正在统计…」横条用转圈动效，不再用静态 ⏳（静态表情看不出在加载，像卡死）",
      '<span class="loading-spin"></span>正在统计「' in src
      and '<span class="loading-spin"></span>正在统计长期耗材' in src
      and "⏳ 正在统计" not in src
      and ".alert-banner .loading-spin {" in src)
check("取数完成（loading → 终态）给 KPI/区块播放入场动画；紧跟的重画要重播（否则新 DOM 上一条动画都不剩），窗口外不重复播",
      "function playSettledEntrance(ustate, amortState) {" in src
      and "_dashLoadStates" in src and "wasLoading" in src and "settledNow" in src
      and "ENTRANCE_WINDOW_MS" in src and "_dashEntranceUntil" in src
      and "if (wasLoading && settledNow) _dashEntranceUntil = now + ENTRANCE_WINDOW_MS;" in src
      and "playSettledEntrance(ustate, r.amort ? r.amort.state : 'none');" in src)
check("可见结构跳变也播入场（首屏占位 → 成员载入 → 卡片）：只认「取数完成」会让前后两次硬跳没动画",
      "function dashShapeKey() {" in src and "var _dashShape = '';" in src
      and "if (trackDashShape()) _dashEntranceUntil = now + ENTRANCE_WINDOW_MS;" in src
      and "playSettledEntrance(usageStateOfMonth(currentKey), 'none');   // 首屏占位也要淡入" in src
      and "trackDashShape();   // 记下" not in src)
check("缺数量的耗材显式提示「暂按普通采购全额计入本月」（钱不凭空消失）",
      "有 ' + pending.length + ' 笔长期耗材未参与摊销（<b>暂按普通采购全额计入本月</b>）" in src)
check("历史记录补取「最早采购月 ~ 各结算月」的用量（摊销才和历史一致）",
      "amortNeededMonths(k).forEach(function (x) { _needMap[x] = true; });" in src)
check("垫付清算提示「整笔采购一次付清、结算只按本月用掉的部分记垫付」",
      "长期耗材（A4纸 / 硒鼓）的整笔采购款是采购人一次付清的" in src)
check("结算公式：E 含长期耗材本月摊销（购买月不再整笔计入）",
      "「有效采购」E = 付款人仍在成员名单里的记录 <b>+ 长期耗材本月摊销</b>" in src)
check("版本说明已补 v7.9", "v7.9：<b>长期耗材按用量摊销</b>" in src)
check("calcMonth 内不再用 parseCents(e.amount)（避免虚拟行浮点尾差）",
      "parseCents(e.amount)" not in src.split("function calcMonth(m, opts) {")[1].split("function save()")[0])
check("导入净化保留合法 kind / 归一 units（非法 kind 删掉，留给自动识别）",
      "if (r.kind !== 'paper' && r.kind !== 'toner' && r.kind !== 'other') delete r.kind;" in src
      and "r.units = Math.max(0, Math.round(+r.units || 0));" in src)

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
    DEFAULT_MEMBERS: [], DEFAULT_SCORE_CAP: 15, DEFAULT_RATE: 0.05,
    PASSWORD_KEYS: ['score', 'expense', 'income', 'transfer', 'settings', 'auth', 'orderprinter'],
    DEFAULT_PASSWORDS: {}, DEFAULT_PRICING: { simplex_price: 0.2, duplex_price: 0.3, cover_page_price: 0.1,
      delivery_percentages: {}, urgency_prices: {} },
    FINANCE_MODE: 'cloud', STORE: null, _authDevices: null,
  };
}
/* 半截沙箱：「数据模型 → calcMonth」整段 + 用量解析（_usageFromOrders）+ 摊销区块渲染。
   _orderMemberKey / printerForOrder / _fmtDate 用桩 —— 成员映射与打印机归属不是本验收的对象。 */
function calcSandbox(src) {
  const sb = MB();
  vm.createContext(sb);
  const start = src.indexOf('function migrateConfig(data)');
  const end = src.indexOf('/* ==================== 保存 ====================');
  if (start < 0 || end <= start) { console.log('SNIPPET_FAIL'); process.exit(3); }
  let snippet = src.slice(start, end);
  snippet += '\n' + defOf('_usageEmpty');
  snippet += '\n' + defOf('_filePagesOf');
  snippet += '\n' + defOf('_fileSheets');
  snippet += '\n' + defOf('_usageFromOrders');
  snippet += '\n' + defOf('fmtInt');
  snippet += '\n' + defOf('consumableAmortSectionHTML');
  snippet += "\nfunction _orderMemberKey(o) { return (o && o.mid) ? ('member:' + o.mid) : 'local:none'; }";
  snippet += "\nfunction printerForOrder(o) { return null; }";
  snippet += "\nfunction _fmtDate(d) { return String(d || '').slice(0, 10); }";
  /* usageOfMonth 在真身里是「缓存读取器」（切片之外）—— 摊销区块的 undated 提示会用到它 */
  snippet += "\nfunction usageOfMonth(key) { return null; }";
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
/* usageByMonth 形状：{ 'YYYY-MM': { sheetsByDay:{}, pagesByDay:{} } } */
function usageMonths(months) {
  const out = {};
  Object.keys(months).forEach(function (mk) {
    out[mk] = { sheetsByDay: months[mk].sheets || {}, pagesByDay: months[mk].pages || {} };
  });
  return out;
}
function usageOf(pagesMap) {
  const pages = Object.keys(pagesMap || {}).reduce((s, k) => s + pagesMap[k], 0);
  return { pages: pages, sheets: 0, pagesByMember: pagesMap || {}, pagesByMemberPrinter: {},
           pagesByPrinter: {}, pagesByDay: {}, sheetsByDay: {},
           unmatchedPrinterPages: 0, unattributedPages: 0, orders: 1, files: 1, month: '2026-10' };
}
function runCases() {

/* ═════ B1：A4纸 —— 按张摊销、只算采购日之后 ═════ */
{
  const sb = calcSandbox(js);
  const e = { id: 'e1', kind: 'paper', date: '2026-10-22', item: 'A4纸采购', amount: 100, units: 500, payer: 'ma' };
  const um = usageMonths({
    '2026-10': { sheets: { '2026-10-21': 50, '2026-10-23': 60, '2026-10-31': 40 } },
    '2026-11': { sheets: { '2026-11-10': 250 } },
    '2026-12': { sheets: { '2026-12-05': 300 } },
  });
  const r10 = sb.consumableAmort([e], um, '2026-10');
  P('B1 购买月只算采购日之后的用量（10-21 的 50 张不算）⇒ 100 张 × ¥0.20 = ¥20.00',
    r10.rows.length === 1 && r10.rows[0].cents === 2000 && r10.rows[0].units === 100
    && r10.lots[0].usedUnits === 100 && r10.lots[0].remainingCents === 8000,
    JSON.stringify(r10.rows));
  P('B1 本月返还对象记在采购人名下（byBuyer）',
    r10.byBuyer.ma === 2000 && r10.totalCents === 2000, JSON.stringify(r10.byBuyer));
  const r11 = sb.consumableAmort([e], um, '2026-11');
  P('B1 11 月累计 350 张：本月摊销 = round(10000×350/500) − 2000 = ¥50.00，剩余 ¥30.00',
    r11.rows[0].cents === 5000 && r11.lots[0].usedUnits === 350 && r11.lots[0].remainingCents === 3000,
    JSON.stringify(r11.lots[0]));
  const r12 = sb.consumableAmort([e], um, '2026-12');
  P('B1 12 月用超 150 张：摊销封顶到采购价、剩余 0、超支显式记 uncovered（不凭空多收）',
    r12.rows[0].cents === 3000 && r12.lots[0].remainingCents === 0
    && r12.uncovered.paper['2026-12'] === 150 && r12.lots[0].chargedCents === 10000,
    JSON.stringify({ row: r12.rows[0], unc: r12.uncovered }));
  P('B1 三个月摊销合计 = 采购价 ¥100（不漂移）',
    r10.rows[0].cents + r11.rows[0].cents + r12.rows[0].cents === 10000);
}

/* ═════ B2：硒鼓 —— 按「打印页数 / 可打印页数」折旧 ═════ */
{
  const sb = calcSandbox(js);
  const e = { id: 'e2', kind: 'toner', date: '2026-10-01', item: '硒鼓购买', amount: 120, units: 1600, payer: 'mb' };
  const um = usageMonths({
    '2026-10': { pages: { '2026-10-10': 400 } },
    '2026-11': { pages: { '2026-11-10': 800 } },
    '2026-12': { pages: { '2026-12-10': 400 } },
  });
  const r10 = sb.consumableAmort([e], um, '2026-10');
  const r11 = sb.consumableAmort([e], um, '2026-11');
  const r12 = sb.consumableAmort([e], um, '2026-12');
  P('B2 硒鼓 400/1600 页 ⇒ ¥30.00；800 页 ⇒ ¥60.00；400 页 ⇒ ¥30.00',
    r10.rows[0].cents === 3000 && r11.rows[0].cents === 6000 && r12.rows[0].cents === 3000,
    JSON.stringify([r10.rows[0].cents, r11.rows[0].cents, r12.rows[0].cents]));
  P('B2 用满 1600 页：剩余 0、合计 = ¥120（寿命损耗折完即停）',
    r12.lots[0].remainingCents === 0
    && r10.rows[0].cents + r11.rows[0].cents + r12.rows[0].cents === 12000);
}

/* ═════ B3：多批同类型 —— 先进先出，早买的先用完、后买的接手 ═════ */
{
  const sb = calcSandbox(js);
  const e1 = { id: 'e1', kind: 'paper', date: '2026-10-01', item: 'A4纸(第一批)', amount: 100, units: 500, payer: 'ma' };
  const e2 = { id: 'e2', kind: 'paper', date: '2026-11-15', item: 'A4纸(第二批)', amount: 150, units: 500, payer: 'mb' };
  const um = usageMonths({
    '2026-10': { sheets: { '2026-10-05': 400 } },
    '2026-11': { sheets: { '2026-11-10': 150, '2026-11-20': 50 } },
    '2026-12': { sheets: { '2026-12-10': 300 } },
  });
  const r10 = sb.consumableAmort([e1, e2], um, '2026-10');
  const r11 = sb.consumableAmort([e1, e2], um, '2026-11');
  const r12 = sb.consumableAmort([e1, e2], um, '2026-12');
  P('B3 10 月：第一批吃 400 张（¥80），第二批还没买 ⇒ 只有一行',
    r10.rows.length === 1 && r10.rows[0].cents === 8000, JSON.stringify(r10.rows));
  P('B3 11 月：第一批剩 100 张先吃 100（¥20）、第二批 11-15 才生效 ⇒ 只能吃 11-20 的 50 张（¥15）',
    r11.rows.length === 2
    && r11.rows.filter(r => r.payer === 'ma')[0].cents === 2000
    && r11.rows.filter(r => r.payer === 'mb')[0].cents === 1500,
    JSON.stringify(r11.rows));
  P('B3 11-10 第二批尚未到货的 50 张记 uncovered（没库存就不摊）',
    r11.uncovered.paper['2026-11'] === 50, JSON.stringify(r11.uncovered));
  P('B3 12 月：第二批剩 450、吃 300 张 ⇒ ¥90.00（累计 350 张 / ¥105）',
    r12.rows.length === 1 && r12.rows[0].cents === 9000
    && r12.lots.filter(l => l.id === 'e2')[0].usedUnits === 350
    && r12.lots.filter(l => l.id === 'e2')[0].chargedCents === 10500,
    JSON.stringify(r12.rows));
}

/* ═════ B4：取整 —— 累计取整差，3 份/3 个月也不漂移 ═════ */
{
  const sb = calcSandbox(js);
  const e = { id: 'e', kind: 'paper', date: '2026-10-01', item: '纸', amount: 100, units: 3, payer: 'ma' };
  const um = usageMonths({
    '2026-10': { sheets: { '2026-10-02': 1 } },
    '2026-11': { sheets: { '2026-11-02': 1 } },
    '2026-12': { sheets: { '2026-12-02': 1 } },
  });
  const a = sb.consumableAmort([e], um, '2026-10').rows[0].cents;
  const b = sb.consumableAmort([e], um, '2026-11').rows[0].cents;
  const c = sb.consumableAmort([e], um, '2026-12').rows[0].cents;
  P('B4 ¥100 / 3 张、每月用 1 张 ⇒ 3333 / 3334 / 3333 分，合计恰好 10000', a === 3333 && b === 3334 && c === 3333,
    JSON.stringify([a, b, c, a + b + c]));
}

/* ═════ B5：用超库存 —— 封顶、可解释、绝不 NaN ═════ */
{
  const sb = calcSandbox(js);
  const e = { id: 'e', kind: 'paper', date: '2026-10-01', item: '纸', amount: 50, units: 100, payer: 'ma' };
  const um = usageMonths({ '2026-10': { sheets: { '2026-10-20': 150 } } });
  const r = sb.consumableAmort([e], um, '2026-10');
  P('B5 只有 100 张库存却用了 150 张 ⇒ 只摊 ¥50、剩 0、uncovered 50、数值有限',
    r.rows[0].cents === 5000 && r.lots[0].remainingCents === 0
    && r.uncovered.paper['2026-10'] === 50 && Number.isFinite(r.totalCents), JSON.stringify(r));
}

/* ═════ B6：calcMonth 集成 —— 整笔采购不再进购买月、摊销记在采购人名下、恒等式不变 ═════ */
{
  const sb = calcSandbox(js);
  sb.STORE = store2({
    expenses: [
      { id: 'e1', date: '2026-10-05', item: '电费', amount: 50, payer: 'ma' },
      { id: 'e2', kind: 'paper', date: '2026-10-22', item: 'A4纸采购', amount: 100, units: 500, payer: 'ma' },
    ],
    income: [{ id: 'i1', date: '2026-10-10', memberId: 'ma', amount: 100 },
             { id: 'i2', date: '2026-10-11', memberId: 'mb', amount: 100 }],
    scores: [{ id: 's1', date: '2026-10-10', memberId: 'ma', item: 'x', score: 5 },
             { id: 's2', date: '2026-10-10', memberId: 'mb', item: 'x', score: 5 }],
  });
  const usage = usageOf({ ma: 50, mb: 50 });
  const am = { rows: [{ id: 'amort:e2', kind: 'paper', payer: 'ma', item: 'A4纸摊销（本月 100 张）', cents: 2000, units: 100 }],
               lots: [], byBuyer: { ma: 2000 }, totalCents: 2000, totalUnits: 100,
               uncovered: { paper: {}, toner: {} } };
  const m = sb.monthFromRecords('2026-10');
  const r = sb.calcMonth(m, { usage: usage, amort: am });
  P('B6 采购支出 = 普通采购 ¥50 + 本月摊销 ¥20（整笔 ¥100 不进购买月）',
    r.totalExp === 7000, String(r.totalExp));
  P('B6 摊销记在采购人名下：paid.ma = ¥50 + ¥20 = ¥70', r.paid.ma === 7000 && r.paid.mb === 0,
    JSON.stringify(r.paid));
  P('B6 三条恒等式不变：ΣN ≡ 0、ΣB ≡ ΣC ≡ Π = I − E − D',
    (r.net.ma + r.net.mb) === 0
    && (r.bookPL.ma + r.bookPL.mb) === (r.totalInc - r.totalExp - r.depTotal)
    && (r.actualPL.ma + r.actualPL.mb) === (r.totalInc - r.totalExp - r.depTotal),
    JSON.stringify({ n: r.net, b: r.bookPL, c: r.actualPL }));
  P('B6 页面计费行 = 普通采购 + 摊销虚拟行（不含整笔 e2）',
    r.expenses.length === 2 && r.expenses.some(x => x.virtual && Math.round(x.amountCents) === 2000)
    && !r.expenses.some(x => x.id === 'e2'), JSON.stringify(r.expenses));
  const rOld = sb.calcMonth(m, { usage: usage });
  P('B6 不传 amort ⇒ 旧口径零回归（电费 ¥50 + 整笔纸 ¥100 = ¥150）', rOld.totalExp === 15000, String(rOld.totalExp));
  const rEmpty = sb.calcMonth(m, { usage: usage, amort: { rows: [], lots: [], byBuyer: {}, totalCents: 0 } });
  P('B6 amort 传入但 rows 为空（用量未取到）⇒ 整笔采购也不计入（¥50），不在"看起来正常"的数字里撑爆购买月',
    rEmpty.totalExp === 5000, String(rEmpty.totalExp));
}

/* ═════ B7：没填数量 / 采购人不在名单 —— 钱不凭空消失 ═════ */
{
  const sb = calcSandbox(js);
  sb.STORE = store2({ expenses: [
    { id: 'e1', kind: 'paper', date: '2026-10-05', item: 'A4纸', amount: 100, units: 0, payer: 'ma' },
  ] });
  const am = { rows: [], lots: [], byBuyer: {}, totalCents: 0 };
  const r = sb.calcMonth(sb.monthFromRecords('2026-10'), { usage: usageOf({}), amort: am });
  P('B7 没填数量 ⇒ 暂按普通采购全额计入（¥100），不参与摊销',
    r.totalExp === 10000 && r.amort && r.amort.rows.length === 0, String(r.totalExp));
  const sb2 = calcSandbox(js);
  sb2.STORE = store2({ expenses: [
    { id: 'e2', kind: 'toner', date: '2026-10-05', item: '硒鼓', amount: 100, units: 1600, payer: 'gone' },
  ] });
  const r2 = sb2.calcMonth(sb2.monthFromRecords('2026-10'), { usage: usageOf({}), amort: { rows: [], lots: [], byBuyer: {}, totalCents: 0 } });
  P('B7 采购人不在成员名单 ⇒ 不算摊销、按原有「未纳入结算」处理（orphanPayerExp = ¥100）',
    r2.totalExp === 0 && r2.orphanPayerExp === 10000, JSON.stringify({ exp: r2.totalExp, orphan: r2.orphanPayerExp }));
}

/* ═════ B8：用量解析 —— 只算 sent、张数取 total_sheets（含份数）、逐日聚合 ═════ */
{
  const sb = calcSandbox(js);
  const orders = [
    { created_at: '2026-10-22 09:00:00', mid: 'ma', received_client: 'c1', files: [
      { status: 'sent', total_pages: 4, total_sheets: 2 },
      { status: 'failed', total_pages: 99, total_sheets: 9 } ] },
    { created_at: '2026-10-22 15:00:00', mid: 'ma', received_client: 'c1', files: [
      { status: 'sent', total_pages: 2, total_sheets: 2 } ] },
    { created_at: '2026-10-23 10:00:00', mid: 'mb', received_client: 'c1', files: [
      { status: 'sent', page_count: 3, effective_pages: 3, copies: 2, duplex: 'on' } ] },
  ];
  const u = sb._usageFromOrders(orders);
  P('B8 只算 sent（failed 的 99 页 / 9 张不算）：页数 12、张数 8（缺 total_* 时按有效页×份数折算）',
    u.pages === 12 && u.sheets === 8, JSON.stringify({ pages: u.pages, sheets: u.sheets }));
  P('B8 逐日聚合：10-22 = 6 页 / 4 张，10-23 = 6 页 / 4 张',
    u.pagesByDay['2026-10-22'] === 6 && u.sheetsByDay['2026-10-22'] === 4
    && u.pagesByDay['2026-10-23'] === 6 && u.sheetsByDay['2026-10-23'] === 4,
    JSON.stringify({ p: u.pagesByDay, s: u.sheetsByDay }));
  P('B8 没绑定成员的订单单独计 unattributedPages（不塞进任何人的用量）',
    u.unattributedPages === 0 && u.pagesByMember.ma === 6 && u.pagesByMember.mb === 6,
    JSON.stringify(u.pagesByMember));
}

/* ═════ B9：摊销区块渲染（正常 / 缺数量 / 无采购 / 未就绪） ═════ */
{
  const sb = calcSandbox(js);
  const e = { id: 'e1', kind: 'paper', date: '2026-10-22', item: 'A4纸采购', amount: 100, units: 500, payer: 'ma' };
  const um = usageMonths({ '2026-10': { sheets: { '2026-10-23': 100 } } });
  sb.STORE = store2({ expenses: [e] });
  const m = sb.monthFromRecords('2026-10');
  const am = sb.consumableAmort([e], um, '2026-10');
  am.state = 'ready';
  const r = sb.calcMonth(m, { usage: usageOf({ ma: 100 }), amort: am });
  const h = sb.consumableAmortSectionHTML(r, m);
  P('B9 正常：列出批次、本月用量、本月摊销 ¥20.00、返还对象',
    h.indexOf('长期耗材摊销') >= 0 && h.indexOf('100 张') >= 0
    && h.indexOf('¥20.00') >= 0 && h.indexOf('返还给采购人') >= 0 && h.indexOf('成员A') >= 0, h.slice(0, 200));
  P('B9 累计使用 / 已摊销 / 剩余未摊销三列都在',
    h.indexOf('累计使用') >= 0 && h.indexOf('已摊销') >= 0 && h.indexOf('剩余未摊销') >= 0 && h.indexOf('¥80.00') >= 0);
  const sbq = calcSandbox(js);
  sbq.STORE = store2({ expenses: [
    { id: 'e0', kind: 'paper', date: '2026-10-05', item: 'A4纸', amount: 100, units: 0, payer: 'ma' },
  ] });
  const mq = sbq.monthFromRecords('2026-10');
  const rq = sbq.calcMonth(mq, { usage: usageOf({}), amort: { rows: [], lots: [], byBuyer: {}, totalCents: 0, state: 'ready', uncovered: { paper: {}, toner: {} } } });
  const hq = sbq.consumableAmortSectionHTML(rq, mq);
  P('B9 缺数量：显式提示「未填数量」「暂按普通采购全额计入本月」',
    hq.indexOf('未填数量') >= 0 && hq.indexOf('暂按普通采购全额计入本月') >= 0, hq.slice(0, 200));
  const sb3 = calcSandbox(js);
  sb3.STORE = store2({ expenses: [] });
  const m3 = sb3.monthFromRecords('2026-10');
  const r3 = sb3.calcMonth(m3, { usage: usageOf({}), amort: { rows: [], lots: [], byBuyer: {}, totalCents: 0, state: 'none' } });
  P('B9 无采购：提示去记账里选 A4纸 / 硒鼓并填数量',
    sb3.consumableAmortSectionHTML(r3, m3).indexOf('还没有长期耗材采购') >= 0);
  const rLoad = sb3.calcMonth(m3, { usage: usageOf({}), amort: { rows: [], lots: [], state: 'loading', needed: ['2026-10'] } });
  const hLoad = sb3.consumableAmortSectionHTML(rLoad, m3);
  P('B9 取数中 + 本月无采购（购买月之后的常态）：显示「正在统计」，不是「还没有长期耗材采购」',
    hLoad.indexOf('正在统计用量') >= 0 && hLoad.indexOf('还没有长期耗材采购') < 0, hLoad.slice(0, 200));
  const am4 = { rows: [], lots: [], byBuyer: {}, totalCents: 0, state: 'failed', needed: ['2026-10'] };
  /* 未就绪时 calcMonthWithUsage 会退回旧口径（整笔计入），这里模拟展示层：r.amort 带 failed */
  const r4 = sb.calcMonth(m, { usage: usageOf({}), amort: null });
  r4.amort = am4;
  P('B9 未就绪：明说「暂按整笔采购计入」（不静默少记、也不假报 ¥0）',
    sb.consumableAmortSectionHTML(r4, m).indexOf('暂按整笔采购计入') >= 0);
}

/* ═════ B10：类型识别 / 导入净化 ═════ */
{
  const sb = calcSandbox(js);
  P('B10 旧记录按项目名识别：A4纸采购 → paper、硒鼓购买 → toner、打印电费 → other',
    sb.expenseKind({ item: 'A4纸采购' }) === 'paper'
    && sb.expenseKind({ item: '硒鼓购买' }) === 'toner'
    && sb.expenseKind({ item: '打印电费' }) === 'other',
    JSON.stringify([sb.expenseKind({ item: 'A4纸采购' }), sb.expenseKind({ item: '硒鼓购买' }), sb.expenseKind({ item: '打印电费' })]));
  const data = sb.sanitizeStore({ config: { members: MEMBERS }, records: {
    expenses: [{ id: 'e1', date: '2026-10-05', item: 'x', amount: 1, payer: 'ma', kind: 'BAD', units: '12.6' }],
    income: [], scores: [], transfers: [] } });
  P('B10 导入净化：非法 kind 删掉（保留自动识别）、units 归整为非负整数',
    !data.records.expenses[0].kind && data.records.expenses[0].units === 13,
    JSON.stringify(data.records.expenses[0]));
  P('B10b 类型识别收紧：A4文件夹 / A4相纸 / A4封面纸 / 墨盒加粉服务费 不误判为耗材',
    sb.expenseKind({ item: 'A4文件夹' }) === 'other'
    && sb.expenseKind({ item: 'A4相纸' }) === 'other'
    && sb.expenseKind({ item: 'A4封面纸(彩色)' }) === 'other'
    && sb.expenseKind({ item: '墨盒加粉服务费' }) === 'other'
    && sb.expenseKind({ item: 'A4 纸' }) === 'paper'
    && sb.expenseKind({ item: 'A4打印纸' }) === 'paper'
    && sb.expenseKind({ item: '碳粉盒' }) === 'toner',
    JSON.stringify(['A4文件夹', 'A4相纸', 'A4封面纸(彩色)', '墨盒加粉服务费'].map((x) => sb.expenseKind({ item: x }))));
}

/* ═════ B11：批次在结算月之前不出现 / 之后正常 ═════ */
{
  const sb = calcSandbox(js);
  const e = { id: 'e', kind: 'paper', date: '2026-11-01', item: '纸', amount: 100, units: 500, payer: 'ma' };
  const um = usageMonths({ '2026-10': { sheets: { '2026-10-10': 300 } }, '2026-11': { sheets: { '2026-11-02': 100 } } });
  const r10 = sb.consumableAmort([e], um, '2026-10');
  const r11 = sb.consumableAmort([e], um, '2026-11');
  P('B11 11 月才买的纸不在 10 月摊销里（10 月的 300 张属于更早的库存）',
    r10.rows.length === 0 && r10.lots.length === 0, JSON.stringify(r10));
  P('B11 11 月只算 11 月之后的用量：100 张 ⇒ ¥20', r11.rows[0].cents === 2000, JSON.stringify(r11.rows));
  P('B11 系统开始记账之前的用量不算「库存超支」（10 月的 300 张出现在最早采购日之前 → 不入 uncovered）',
    !r11.uncovered.paper['2026-10'] && !r11.uncovered.paper['2026-11'], JSON.stringify(r11.uncovered));
}

/* ═════ B12：购买月之后的月份，摊销区块不能变成「还没有长期耗材采购」 ═════
   （当月 expenses 里当然没有那笔采购，但 r.amort.lots 里还挂着它 —— 早退条件只看
   本月采购会让购买月之后的**每个月**整块失效，本月摊销/剩余/返还对象全部消失。） */
{
  const sb = calcSandbox(js);
  const e = { id: 'e1', kind: 'paper', date: '2026-10-22', item: 'A4纸采购', amount: 100, units: 500, payer: 'ma' };
  const um = usageMonths({
    '2026-10': { sheets: { '2026-10-23': 100 } },
    '2026-11': { sheets: { '2026-11-10': 250 } },
  });
  sb.STORE = store2({ expenses: [e] });
  const m11 = sb.monthFromRecords('2026-11');   // 11 月的 expenses 里没有这笔采购
  const am11 = sb.consumableAmort([e], um, '2026-11');
  am11.state = 'ready';
  const r11 = sb.calcMonth(m11, { usage: usageOf({}), amort: am11 });
  const h11 = sb.consumableAmortSectionHTML(r11, m11);
  P('B12 购买月之后：区块仍显示本月摊销（250 张 / ¥50.00 / 剩余 ¥30.00），不是「还没有长期耗材采购」',
    h11.indexOf('本月摊销合计') >= 0 && h11.indexOf('还没有长期耗材采购') < 0
    && h11.indexOf('250 张') >= 0 && h11.indexOf('¥50.00') >= 0 && h11.indexOf('¥30.00') >= 0,
    h11.slice(0, 260));
  P('B12 购买月之后仍给出返还对象（成员A）',
    h11.indexOf('返还给采购人') >= 0 && h11.indexOf('成员A') >= 0);
  P('B12 11 月的采购支出 = 本月摊销 ¥50.00（整笔 ¥100 不在 11 月）', r11.totalExp === 5000, String(r11.totalExp));
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


section("B. 纯函数实测（真跑 consumableAmort / calcMonth / _usageFromOrders / 区块渲染）")
tmpdir = tempfile.mkdtemp(prefix="hn_consumable_")
code, out_text = run_harness(HTML, tmpdir)
print(out_text.rstrip())
check("函数级断言全部通过", code == 0, f"exit={code}")

# ══════════════════════════════════════════════════════════════
# C. 变异自证
# ══════════════════════════════════════════════════════════════
section("C. 变异自证（改坏 → 必须变红）")

MUTANTS = [
    ("calcMonth 不替换整笔采购（回到购买月全额）",
     "if (amort) {\n    exps = exps.filter(function (e) { return !isAmortizableExpense(e); });",
     "if (false) {\n    exps = exps.filter(function (e) { return !isAmortizableExpense(e); });"),
    ("每月金额不再取「累计差」（逐月重复计提累计额）",
     "lot.months[mk] = { units: lot.perMonth[mk] || 0, cents: cents - prevCents, cumUnits: cum };",
     "lot.months[mk] = { units: lot.perMonth[mk] || 0, cents: cents, cumUnits: cum };"),
    ("采购日边界失效（批次在买入前就可被消耗）",
     "while (qi < queue.length && queue[qi].date <= d) active.push(queue[qi++]);",
     "while (qi < queue.length) active.push(queue[qi++]);"),
    ("先进先出改后进先出",
     ".sort(function (a, b) { return a.date === b.date ? (a.order - b.order) : (a.date < b.date ? -1 : 1); });",
     ".sort(function (a, b) { return a.date === b.date ? (a.order - b.order) : (a.date < b.date ? 1 : -1); });"),
    ("不封顶（用超库存也继续摊）",
     "var take = Math.min(lot.remaining, need);",
     "var take = need;"),
]
for idx, (name, old, new) in enumerate(MUTANTS):
    if old not in src:
        check(f"变异「{name}」：锚点存在（锚点没了 ⇒ 变异自证空转）", False, old[:70])
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
print("长期耗材摊销（v7.9）验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
