/* 验收 · 收支清算「趋势下钻 → 单日逐时」（v6.8）
 *
 * 需求（用户原话）：
 *   ① 点击趋势柱状图的任意一根柱 → 图变成那一天的 00:00~23:00 每小时一柱（对齐 DeepSeek 开放平台）；
 *   ② 时间范围加「今天」「近7天」；
 *   ③ 选了某一天 / 今天 / 任意单日 → 都展示这一天每小时的数据。
 *
 * 分工：
 *   - 静态接线：源码里确实有这些入口（不允许只写 UI 不接线）。
 *   - 函数级实测（Node + VM 内跑**真实**函数，非复刻）：逐时聚合 24 桶 / 边界小时 /
 *     跨天不漏不错 / 收益与 KPI 同源 / 逐时合计 == 当天日柱 / 下钻与返回的口径状态机。
 *   - 渲染级：真实加载整页脚本、注入订单后 renderCloud()，断言趋势卡片 HTML（逐时口径文案、
 *     「← 返回区间」按钮、canvas）。
 *   DOM 用 Proxy 桩（无需 jsdom / 浏览器），与 verify_actual_transfer_render.js 同套路。
 *
 * 用法：node tests/verify_trend_hourly.js [settlement.html 路径]
 * exit 0 = 全部通过。
 */
const fs = require('fs'), vm = require('vm'), path = require('path');
const HTML_PATH = process.argv[2] || path.join(__dirname, '..', 'local_print_tool', 'finance', 'settlement.html');
const html = fs.readFileSync(HTML_PATH, 'utf8');
const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const js = scripts.join('\n;\n');
/* 剔除注释后的纯代码：注释里会解释历史口径（如 o.revenue），不能当作违规 */
const code = js.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');

let bad = 0;
function chk(name, cond, detail) {
  if (!cond) bad++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (!cond && detail ? '  -- ' + detail : ''));
}

/* ══════════════════════════════════════════════════════════════
   1) 静态接线
   ══════════════════════════════════════════════════════════════ */
console.log('=== 静态接线 ===');
chk('时间范围新增「今天」（r:today）与「近7天」（r:7d）',
  html.includes('<option value="r:today"') && html.includes("['7d', '近7天']"),
  '未找到今天/近7天选项');
chk('单日 → 逐时粒度（days <= 1 → hour）', code.includes("if (days <= 1) return 'hour';"));
chk('_scopeRange 支持 day 模式（单日起止同一天）',
  /if \(t\.mode === 'day'\)/.test(code) && code.includes('var day = t.day || todayStr();'));
chk('点柱下钻接线（图表 onClick → drillTrendDay）', code.includes('drillTrendDay(p.date)'));
chk('仅日粒度可点（逐时/切片不再下钻）', code.includes("var drillable = granularity === 'day';"));
chk('「← 返回区间」接线（_trendDrillPrev + undoTrendDrill）',
  code.includes('function undoTrendDrill()') && code.includes('undoTrendDrill()') && code.includes('_trendDrillPrev'));
chk('逐时聚合存在且复用同一累加函数（不是第二套口径）',
  code.includes('function _computeHourlyTrend(') && code.includes('_trendAccumulate(slots[hh], o)')
  && code.includes('_trendAccumulate(map[day], o)'));
chk('收益唯一口径落点：_trendAccumulate + 保留的 _trendByBucket = 2 处',
  (code.match(/revenue \+= _orderMoney\(o, true\)\.record \/ 100;/g) || []).length === 2,
  String((code.match(/revenue \+= _orderMoney\(o, true\)\.record \/ 100;/g) || []).length) + ' 处');
chk('全代码零 o.revenue / o.receivable 读取', !code.includes('o.revenue') && !code.includes('o.receivable'));
chk('渲染分派：slice → 折线，其余 → 柱状（hour/day）',
  code.includes("if (tv.gran === 'slice') _renderDailyTrend('cloud-chart-trend', tv.points, _cloudTrendMetric);")
  && code.includes("_renderBucketTrend('cloud-chart-trend', tv.points, _cloudTrendMetric, tv.gran);"));
chk('逐时 x 轴刻度减半（24 柱不全挤在一起）', code.includes('maxTicksLimit: hourly ? 12 : 24'));
chk('口径切换统一走 _fetchScope 分派（本地模式不打云端代理）',
  code.includes('function _fetchScope(force)')
  && code.includes("return FINANCE_MODE === 'local' ? fetchLocalScopeData(force) : fetchScopeData(force);")
  && !/^\s+fetchScopeData\(\);$/m.test(code)
  && code.includes('onclick="_fetchScope(true)"'));

/* ══════════════════════════════════════════════════════════════
   2) 函数级实测（真实函数，非复刻）
   ══════════════════════════════════════════════════════════════ */
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
const els = {};
const documentProto = {
  querySelector: (s) => (els[s] = els[s] || stub()),
  querySelectorAll: () => [],
  getElementById: (id) => (els['#' + id] = els['#' + id] || stub()),
  createElement: () => stub(),
  createTextNode: () => stub(),
  addEventListener: () => {},
  removeEventListener: () => {},
  readyState: 'complete',
  documentElement: stub(),
  head: stub(),
  body: stub(),
};
const document = proxied(documentProto);
const storage = { getItem: () => null, setItem() {}, removeItem() {} };
const windowProto = {
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  addEventListener: () => {}, removeEventListener() {},
  location: { search: '', href: 'http://localhost/', origin: 'http://localhost' },
  innerWidth: 1400, innerHeight: 900, devicePixelRatio: 1,
  localStorage: storage, sessionStorage: storage, document: document,
};
const sandbox = {
  console, Math, JSON, Object, Array, String, Number, Boolean, Date, RegExp, Error, Promise,
  Set, Map, WeakMap, isNaN, isFinite, parseFloat, parseInt, encodeURIComponent, decodeURIComponent,
  /* 定时器一律不真跑：图表创建不在本脚本验收范围，且沙箱里没有 Chart（真跑会抛在 timer 里） */
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
  document: document,
  window: proxied(windowProto),
  localStorage: storage, sessionStorage: storage,
  location: windowProto.location,
  navigator: { userAgent: 'node', clipboard: {} },
  fetch: () => Promise.resolve({ json: () => Promise.resolve({ success: false }), ok: true }),
  getComputedStyle: () => ({ colorScheme: 'light', getPropertyValue: () => '' }),
  FINANCE_MODE: 'cloud',
  __fetchCalls: [],
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
vm.createContext(sandbox);
try {
  vm.runInContext(js, sandbox, { filename: 'settlement.js' });
} catch (e) {
  console.log('LOAD_FAIL: ' + e.message);
  process.exit(2);
}
console.log('LOAD_OK');
/* 下钻会走正常口径切换链路（会真发请求）：这里把取数换成探针，只验口径状态机 */
vm.runInContext(`
  fetchScopeData = function (force) { __fetchCalls.push({ force: !!force, range: _scopeRange() }); };
`, sandbox);

const today = vm.runInContext('todayStr()', sandbox);
const prevDay = (() => { const d = new Date(today + 'T12:00:00'); d.setDate(d.getDate() - 1); return d.toISOString().slice(0, 10); })();
const month = today.slice(0, 7);
const shortDate = (s) => (+s.slice(5, 7)) + '/' + (+s.slice(8, 10));   // '2026-10-16' → '10/16'

/* 夹具：今天 5 单（含 1 单自打、1 单带附加服务费、跨到 23:59），昨天 1 单（不该漏进今天的逐时） */
const ORDERS = [
  { created_at: today + ' 00:00:05', openid: 'local', owner_name: '成员A', source: 'local', status: 'sent', nickname: '',
    is_admin_print: 0, total_price: 3.00, total_page_count: 2, files_count: 1,
    files: [{ file_name: 'a.pdf', total_price: 3.00, status: 'sent', duplex: 'off', page_count: 2, copies: 1 }] },
  { created_at: today + ' 00:59:59', openid: 'local', owner_name: '成员A', source: 'local', status: 'sent', nickname: '',
    is_admin_print: 0, total_price: 3.00, total_page_count: 2, files_count: 1,
    files: [{ file_name: 'b.pdf', total_price: 3.00, status: 'sent', duplex: 'off', page_count: 2, copies: 1 }] },
  { created_at: today + ' 10:00:00', openid: 'local', owner_name: '成员A', source: 'local', status: 'sent', nickname: '',
    is_admin_print: 1, total_price: 9.90, total_page_count: 3, files_count: 1,
    files: [{ file_name: 'self.docx', total_price: 9.90, status: 'sent', duplex: 'off', page_count: 3, copies: 1 }] },
  { created_at: today + ' 15:30:00', openid: 'local', owner_name: '成员A', source: 'local', status: 'queued', nickname: '',
    is_admin_print: 0, total_price: 5.00, total_page_count: 5, files_count: 1,
    files: [{ file_name: 'c.docx', total_price: 4.00, status: 'queued', duplex: 'on', page_count: 5, copies: 1 }] },
  { created_at: today + ' 23:59:59', openid: 'local', owner_name: '成员A', source: 'local', status: 'sent', nickname: '',
    is_admin_print: 0, total_price: 1.00, total_page_count: 1, files_count: 1,
    files: [{ file_name: 'd.pdf', total_price: 1.00, status: 'sent', duplex: 'off', page_count: 1, copies: 1 }] },
  { created_at: prevDay + ' 12:00:00', openid: 'local', owner_name: '成员A', source: 'local', status: 'sent', nickname: '',
    is_admin_print: 0, total_price: 7.00, total_page_count: 1, files_count: 1,
    files: [{ file_name: 'y.pdf', total_price: 7.00, status: 'sent', duplex: 'off', page_count: 1, copies: 1 }] },
];

const STORE_JS = `STORE = {
  config: { investRate: 0.05, scoreCap: 15, passwords: {}, members: [ { id: 'ma', name: '成员A', color: '#4472C4' } ] },
  records: { expenses: [], income: [], scores: [], transfers: [] },
  notes: {}, meta: { version: 0 }
};`;
vm.runInContext(STORE_JS, sandbox);
vm.runInContext('__orders = ' + JSON.stringify(ORDERS) + ';', sandbox);

console.log('\n=== 口径与粒度 ===');
{
  const r = vm.runInContext(`(function () {
    _scopeTime = { mode: 'day', day: todayStr(), month: '${month}', start: '', end: '' };
    var out = { range: _scopeRange(), label: _scopeTimeLabel() };
    _scopeTime = { mode: '7d', month: '${month}', start: '', end: '' };
    out.d7 = _scopeRange();
    out.l7 = _scopeTimeLabel();
    _scopeTime = { mode: '30d', month: '${month}', start: '', end: '' };
    out.d30 = _scopeRange();
    _scopeTime = { mode: 'custom', month: '${month}', start: todayStr(), end: todayStr() };
    out.dCustom = _scopeRange();
    out.lCustom = _scopeTimeLabel();
    out.granDay = _trendGranularity(todayStr(), todayStr());
    out.gran7 = _trendGranularity(out.d7.start, out.d7.end);
    out.granBig = _trendGranularity('2025-01-01', '2026-10-16');
    return out;
  })()`, sandbox);
  chk('今天 → 单日（起止同一天）+ 标签「今天」',
    r.range.start === today && r.range.end === today && r.label === '今天',
    JSON.stringify(r.range) + ' / ' + r.label);
  chk('近7天 → 含今天往前 6 天，标签「近7天」',
    r.d7.end === today && r.d7.start === (() => { const d = new Date(today + 'T12:00:00'); d.setDate(d.getDate() - 6); return d.toISOString().slice(0, 10); })()
    && r.l7 === '近7天', JSON.stringify(r.d7) + ' / ' + r.l7);
  chk('近30天仍是 29 天前 → 今天（老口径不回归）',
    r.d30.end === today && r.d30.start === (() => { const d = new Date(today + 'T12:00:00'); d.setDate(d.getDate() - 29); return d.toISOString().slice(0, 10); })(),
    JSON.stringify(r.d30));
  chk('自定义起止选到同一天 → 也是单日，标签收敛为「今天」',
    r.dCustom.start === today && r.dCustom.end === today && r.lCustom === '今天');
  chk('粒度：单日 → hour；7 天 → day；>60 天 → slice',
    r.granDay === 'hour' && r.gran7 === 'day' && r.granBig === 'slice',
    [r.granDay, r.gran7, r.granBig].join(' / '));
}

console.log('\n=== 逐时聚合（真实 _computeHourlyTrend / _computeDailyTrend）===');
{
  const r = vm.runInContext(`(function () {
    var h = _computeHourlyTrend(__orders, todayStr());
    var d = _computeDailyTrend(__orders);
    var dBucket = null, pBucket = null;
    d.forEach(function (p) { if (p.date === todayStr()) dBucket = p; if (p.date === '${prevDay}') pBucket = p; });
    var sum = { count: 0, revenue: 0, pages: 0, files: 0, sheets: 0 };
    h.forEach(function (p) { sum.count += p.count; sum.revenue += p.revenue; sum.pages += p.pages; sum.files += p.files; sum.sheets += p.sheets; });
    return { h: h, dBucket: dBucket, pBucket: pBucket, sum: sum };
  })()`, sandbox);

  chk('固定 24 个整点桶', r.h.length === 24, String(r.h.length));
  chk('标签为 HH:00（00:00 ~ 23:00）',
    r.h[0].label === '00:00' && r.h[23].label === '23:00' && r.h[15].label === '15:00',
    r.h[0].label + ' / ' + r.h[23].label);
  chk('小时字段 0~23 递增且 date 都是被测那天',
    r.h.every((p, i) => p.hour === i && p.date === today) , JSON.stringify(r.h.map(p => p.hour).slice(0, 3)));
  chk('00:00:05 与 00:59:59 归到 00 点整（边界不落到前一天/下一格）',
    r.h[0].count === 2 && Math.abs(r.h[0].revenue - 6.00) < 1e-9 && r.h[0].sheets === 4,
    'count=' + r.h[0].count + ' revenue=' + r.h[0].revenue + ' sheets=' + r.h[0].sheets);
  chk('23:59:59 归到 23 点整（当天最后一分钟不漏）',
    r.h[23].count === 1 && Math.abs(r.h[23].revenue - 1.00) < 1e-9, 'count=' + r.h[23].count);
  chk('自打订单：计入订单数/页数/张数，但收益为 0（与 KPI 同口径）',
    r.h[10].count === 1 && r.h[10].revenue === 0 && r.h[10].pages === 3 && r.h[10].sheets === 3,
    JSON.stringify(r.h[10]));
  chk('附加服务费计入（15 点那单：文件 4.00 + 附加 1.00 = 5.00，仍是应收未收）',
    r.h[15].count === 1 && Math.abs(r.h[15].revenue - 5.00) < 1e-9, String(r.h[15].revenue));
  chk('没有订单的小时补 0 保留位置（1/2/3…点全为 0）',
    [1, 2, 3, 4, 5, 20].every(i => r.h[i].count === 0 && r.h[i].revenue === 0));
  chk('别的一天（昨天）不混进今天的逐时',
    r.h.reduce((s, p) => s + p.count, 0) === 5, String(r.h.reduce((s, p) => s + p.count, 0)));
  chk('★ 逐时合计 == 当天日柱（收益口径唯一：_orderMoney().record）',
    Math.abs(r.sum.revenue - r.dBucket.revenue) < 1e-9 && r.sum.count === r.dBucket.count
    && r.sum.pages === r.dBucket.pages && r.sum.files === r.dBucket.files && r.sum.sheets === r.dBucket.sheets,
    JSON.stringify(r.sum) + ' vs ' + JSON.stringify({ count: r.dBucket.count, revenue: r.dBucket.revenue, pages: r.dBucket.pages, files: r.dBucket.files, sheets: r.dBucket.sheets }));
  chk('当天合计 12.00 元 / 5 单 / 13 页 / 11 张（自打不计钱、昨天不计入）',
    Math.abs(r.sum.revenue - 12.00) < 1e-9 && r.sum.count === 5 && r.sum.pages === 13 && r.sum.sheets === 11,
    JSON.stringify(r.sum));
  chk('昨天那一单在日趋势里仍是独立一天（¥7.00）',
    r.pBucket && Math.abs(r.pBucket.revenue - 7.00) < 1e-9, JSON.stringify(r.pBucket || null));
  chk('_hourOf 边界：只有日期没有时间的老数据返回 null（不假归到 00 点）',
    vm.runInContext("_hourOf('2026-10-09') === null && _hourOf('2026-10-09 07:05:00') === 7", sandbox));
}

console.log('\n=== 趋势视图状态（_trendViewState）===');
{
  const r = vm.runInContext(`(function () {
    _cloudData = { orders: __orders };
    _cloudLoading = false;
    _scopeTime = { mode: 'day', day: todayStr(), month: '${month}', start: '', end: '' };
    var day = _trendViewState();
    _scopeTime = { mode: 'month', month: '${month}', start: '', end: '' };
    var mon = _trendViewState();
    var mBucket = null;
    mon.points.forEach(function (p) { if (p.date === todayStr()) mBucket = p; });
    return { dayGran: day.gran, dayN: day.points.length, dayRev: day.points.reduce(function (s, p) { return s + p.revenue; }, 0),
             dayLabel: day.points[0].label, monGran: mon.gran, monBucket: mBucket };
  })()`, sandbox);
  chk('单日口径 → gran=hour 且 24 根柱', r.dayGran === 'hour' && r.dayN === 24, r.dayGran + ' / ' + r.dayN);
  chk('逐时柱标签是小时而非日期', r.dayLabel === '00:00', r.dayLabel);
  chk('逐时收益合计 = 当天收益（¥12.00）', Math.abs(r.dayRev - 12.00) < 1e-9, String(r.dayRev));
  chk('同一天在月视图里仍是一根日柱，数值与逐时一致（gran=day）',
    r.monGran === 'day' && r.monBucket && Math.abs(r.monBucket.revenue - 12.00) < 1e-9,
    r.monGran + ' / ' + JSON.stringify(r.monBucket || null));
}

console.log('\n=== 下钻 / 返回（口径状态机 + 取数调用）===');
{
  const r = vm.runInContext(`(function () {
    __fetchCalls.length = 0;
    _scopeTime = { mode: 'month', month: '${month}', start: '', end: '' };
    _trendDrillPrev = null;
    drillTrendDay('${prevDay}');
    var a = { mode: _scopeTime.mode, day: _scopeTime.day, prev: _trendDrillPrev && _trendDrillPrev.mode,
              calls: __fetchCalls.length, range: __fetchCalls[0] && __fetchCalls[0].range };
    drillTrendDay('${prevDay}');            // 同一天重复点：不重复请求
    var b = { calls: __fetchCalls.length, prev: !!_trendDrillPrev };
    undoTrendDrill();                       // 返回下钻前区间
    var c = { mode: _scopeTime.mode, month: _scopeTime.month, prev: _trendDrillPrev, calls: __fetchCalls.length,
              range: __fetchCalls[__fetchCalls.length - 1] && __fetchCalls[__fetchCalls.length - 1].range };
    return { a: a, b: b, c: c };
  })()`, sandbox);
  chk('点柱 → 口径切到那一天（mode=day）', r.a.mode === 'day' && r.a.day === prevDay, JSON.stringify(r.a));
  chk('下钻前口径被记住（可返回）', r.a.prev === 'month', String(r.a.prev));
  chk('下钻按单日口径取数（start == end == 该日）',
    r.a.calls === 1 && r.a.range && r.a.range.start === prevDay && r.a.range.end === prevDay, JSON.stringify(r.a.range));
  chk('同一天重复点不重复取数（也无副作用）', r.b.calls === 1 && r.b.prev === true, JSON.stringify(r.b));
  chk('「← 返回区间」回到原口径（month）且清空返回点',
    r.c.mode === 'month' && r.c.month === month && r.c.prev === null, JSON.stringify(r.c));
  chk('返回同样走正常取数链路（回到整月范围）',
    r.c.calls === 2 && r.c.range && r.c.range.start === month + '-01', JSON.stringify(r.c.range));
  const cleared = vm.runInContext(`(function () {
    _scopeTime = { mode: 'month', month: '${month}', start: '', end: '' };
    drillTrendDay('${prevDay}');
    onScopeTimeChange('r:today');           // 手动换口径 = 放弃返回点
    return { prev: _trendDrillPrev, mode: _scopeTime.mode, day: _scopeTime.day, label: _scopeTimeLabel() };
  })()`, sandbox);
  chk('手动切到「今天」→ 单日逐时，且不再保留返回点',
    cleared.prev === null && cleared.mode === 'day' && cleared.day === today && cleared.label === '今天',
    JSON.stringify(cleared));
}

console.log('\n=== 渲染级（口径栏 + 趋势卡片 HTML）===');
{
  const bar = vm.runInContext(`(function () {
    _cloudData = null;
    _scopeTime = { mode: 'month', month: '${month}', start: '', end: '' };
    var a = _renderScopeBar(['${month}']);
    _scopeTime = { mode: 'day', day: '${prevDay}', month: '${month}', start: '', end: '' };
    var b = _renderScopeBar(['${month}']);
    _scopeTime = { mode: 'day', day: todayStr(), month: '${month}', start: '', end: '' };
    var c = _renderScopeBar(['${month}']);
    return { month: a, drilled: b, todaySel: c };
  })()`, sandbox);
  chk('口径栏渲染出「今天」「近7天」（任意区间组内）',
    bar.month.includes('<option value="r:today"') && bar.month.includes('<option value="r:7d"') && bar.month.includes('近7天'),
    '');
  chk('月份口径下「今天」不是选中的（默认仍按自然月）',
    !/value="r:today" selected/.test(bar.month));
  chk('下钻到某日 → 下拉出现「单日 YYYY-MM-DD」并选中',
    bar.drilled.includes('<option value="r:day" selected>单日 ' + prevDay + '</option>'), '');
  chk('选「今天」→ 今天那项选中、且没有多余的「单日」项',
    /value="r:today" selected/.test(bar.todaySel) && !bar.todaySel.includes('value="r:day"'), '');
}

{
  const out = vm.runInContext(`(function () {
    STORE = { config: { investRate: 0.05, scoreCap: 15, passwords: {}, members: [ { id: 'ma', name: '成员A', color: '#4472C4' } ] },
              records: { expenses: [], income: [], scores: [], transfers: [] }, notes: {}, meta: { version: 0 } };
    _cloudMonthsReady = true;
    _cloudMonthsCache = ['${month}'];
    _cloudLoading = false;
    _cloudData = { orders: __orders, revenue_summary: {}, by_user: [] };
    _cloudDataFetchedAt = '12:00:00';
    _scopeTime = { mode: 'day', day: '${prevDay}', month: '${month}', start: '', end: '' };
    _trendDrillPrev = { mode: 'month', month: '${month}', start: '', end: '' };
    _cloudTrendMetric = 'revenue';
    renderCloud();
    return $('#tab-cloud').innerHTML;
  })()`, sandbox);
  chk('趋势卡片标题为「逐时」（单日口径）', out.includes('逐时（00:00~23:00 每小时一柱）'), '未找到逐时文案');
  chk('单日口径：口径摘要标「（单日 · 逐时）」', out.includes(prevDay + '（单日 · 逐时）'), '未找到单日摘要');
  chk('下钻态出现「← 返回区间」按钮', out.includes('← 返回区间') && out.includes('undoTrendDrill()'));
  chk('趋势 canvas 已渲染', out.includes('id="cloud-chart-trend"'));
  chk('峰值标注用的是小时标签（不是日期）', /峰值 ¥7\.00 · \d{2}:00/.test(out), (out.match(/峰值[^<]*/) || [''])[0]);

  const mon = vm.runInContext(`(function () {
    _scopeTime = { mode: 'month', month: '${month}', start: '', end: '' };
    _trendDrillPrev = null;
    renderCloud();
    return $('#tab-cloud').innerHTML;
  })()`, sandbox);
  chk('月口径提示可点柱下钻', mon.includes('点击柱状图可下钻到当天逐时'), '未找到下钻提示');
  chk('月口径不出现「← 返回区间」', !mon.includes('返回区间'));
  chk('月口径不再显示逐时文案', !mon.includes('逐时（00:00~23:00'));

  const week = vm.runInContext(`(function () {
    _scopeTime = { mode: '7d', month: '${month}', start: '', end: '' };
    renderCloud();
    return $('#tab-cloud').innerHTML;
  })()`, sandbox);
  chk('近7天：口径栏选中 + 日粒度柱状图（7 根柱的区间）',
    /value="r:7d" selected/.test(week) && week.includes('近7天（') && week.includes('点击柱状图可下钻到当天逐时'), '');
}

console.log('\n' + '='.repeat(52));
console.log(bad ? '趋势下钻 / 单日逐时验收：存在失败 ❌' : '趋势下钻 / 单日逐时验收：全部通过 ✅');
process.exit(bad ? 1 : 0);
