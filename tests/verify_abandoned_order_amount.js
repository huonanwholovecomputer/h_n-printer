/* 验收 · 收支清算订单明细「放弃订单金额不再清零」（Node + VM 内真实执行 settlement.html）
 *
 * 背景（用户反馈，2026-10-08）：本地打印工具里「放弃打印」后，订单明细的整单金额被显示成 ¥0.00，
 * 但展开后的文件级金额（¥1.20 / ¥0.80）照常显示 —— 同一行内自相矛盾。
 * 根因：订单行金额用的是「云端收益记录」口径 `_orderMoney(o).record`，而该口径把
 * canceled/abandoned/rejected/failed 一律剔除（金额自然为 0）；文件级金额用的是原样 total_price。
 *
 * 期望（v6.5 最终口径）：金额列一律显示「订单本身金额」= 父订单 total_price
 * （缺失/为 0 时退回子任务费合计），覆盖三种情形：
 *   ① 整单被排除（放弃/取消/打回/失败）→ 取状态色（放弃=紫 #7B1FA2），悬停「订单金额 … · 放弃打印，不计入收益」；
 *   ② 混合单（同一订单既有已打印完成、又有被放弃的文件）→ 显示整单金额 + 末尾 *，悬停「订单金额 … · 计入收益 …」；
 *   ③ 历史老订单（v5 之前无 order_files 明细）→ 父订单 total_price + 末尾 *，悬停注明未计入收益统计。
 * 而收益统计口径完全不变：_cloudAgg / KPI / 图表仍剔除放弃、取消、打回、失败订单。
 *
 * 用法：node tests/verify_abandoned_order_amount.js [settlement.html 路径]
 * exit 0 = 全部通过。
 */
const fs = require('fs'), vm = require('vm');
const HTML_PATH = process.argv[2] || require('path').join(__dirname, '..', 'local_print_tool', 'finance', 'settlement.html');
const html = fs.readFileSync(HTML_PATH, 'utf8');
const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const js = scripts.join('\n;\n');

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
  addEventListener: () => {},
  removeEventListener: () => {},
  location: { search: '', href: 'http://localhost/', origin: 'http://localhost' },
  innerWidth: 1400, innerHeight: 900, devicePixelRatio: 1,
  localStorage: storage, sessionStorage: storage, document: document,
};
const sandbox = {
  console, Math, JSON, Object, Array, String, Number, Boolean, Date, RegExp, Error, Promise,
  Set, Map, WeakMap, isNaN, isFinite, parseFloat, parseInt, encodeURIComponent, decodeURIComponent,
  setTimeout, clearTimeout, setInterval, clearInterval,
  requestAnimationFrame: (f) => { try { f(0); } catch (e) {} return 0; },
  cancelAnimationFrame: () => {},
  document: document,
  window: proxied(windowProto),
  localStorage: storage,
  sessionStorage: storage,
  location: windowProto.location,
  navigator: { userAgent: 'node', clipboard: {} },
  fetch: () => Promise.resolve({ json: () => Promise.resolve({ success: false }), ok: true }),
  getComputedStyle: () => ({ colorScheme: 'light', getPropertyValue: () => '' }),
  FINANCE_MODE: 'cloud',
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

/* ==================== 注入数据（截图场景复刻） ==================== */
const ORDERS = [
  /* ① 截图里的整单放弃：HN20261008-0021，2 个文件 ¥1.20 + ¥0.80，父订单 total_price 2.00 */
  { order_id: 1, order_number: 'HN20261008-0021', source: 'local', openid: 'local', owner_name: '甲成员',
    status: 'abandoned', total_price: 2.00, files_count: 2, total_page_count: 11,
    created_at: '2026-10-08 21:15:00', remark: '',
    files: [
      { id: 11, file_name: '电桥法测电阻实验报告(1).pdf', copies: 1, page_count: 6, duplex: 'on', total_price: 1.20, status: 'abandoned' },
      { id: 12, file_name: '第一次线下作业(2).docx', copies: 1, page_count: 5, duplex: 'on', total_price: 0.80, status: 'abandoned' } ] },
  /* ② 整单放弃但父订单 total_price 缺失/为 0 → 退回子任务费合计 ¥2.70（历史/异常数据兜底） */
  { order_id: 2, order_number: 'HN20261008-0023', source: 'local', openid: 'local', owner_name: '甲成员',
    status: 'abandoned', total_price: 0, files_count: 8, total_page_count: 11,
    created_at: '2026-10-08 21:13:00',
    files: [0.40, 0.40, 0.40, 0.30, 0.30, 0.30, 0.30, 0.30].map((p, i) => ({
      id: 20 + i, file_name: 'f' + i + '.jpg', copies: 1, page_count: 1, duplex: 'off', total_price: p, status: 'abandoned' })) },
  /* ③ 已完成订单（回归：金额 / 颜色都不变） */
  { order_id: 3, order_number: 'HN20261008-0020', source: 'local', openid: 'local', owner_name: '甲成员',
    status: 'sent', total_price: 3.20, files_count: 1, total_page_count: 16, created_at: '2026-10-08 18:53:00',
    files: [{ id: 30, file_name: 'c.pdf', copies: 1, page_count: 16, duplex: 'on', total_price: 3.20, status: 'sent' }] },
  /* ④ 自打已完成（回归：蓝色 var(--primary) 常量色） */
  { order_id: 4, order_number: 'HN20261008-0019', source: 'local', openid: 'local', owner_name: '甲成员',
    is_admin_print: true, status: 'sent', total_price: 1.00, files_count: 1, total_page_count: 4,
    created_at: '2026-10-08 18:00:00',
    files: [{ id: 40, file_name: 'd.pdf', copies: 1, page_count: 4, duplex: 'on', total_price: 1.00, status: 'sent' }] },
  /* ⑤ 部分放弃（混合单）：1 完成 + 1 放弃 → 显示整单金额 ¥1.50 并标 *，计入收益仍只有 ¥1.00 */
  { order_id: 5, order_number: 'HN20261008-0018', source: 'wechat', openid: 'oX', nickname: '甲成员',
    status: 'sent', total_price: 1.50, files_count: 2, total_page_count: 9, created_at: '2026-10-08 17:00:00',
    files: [
      { id: 50, file_name: 'e.pdf', copies: 1, page_count: 6, duplex: 'on', total_price: 1.00, status: 'sent' },
      { id: 51, file_name: 'f.pdf', copies: 1, page_count: 3, duplex: 'off', total_price: 0.50, status: 'abandoned' } ] },
  /* ⑥ 纯占位取消单：无文件、无金额 → 仍显示 ¥0.00（不凭空造钱） */
  { order_id: 6, order_number: 'HN20261008-0017', source: 'local', openid: 'local', owner_name: '甲成员',
    status: 'canceled', total_price: 0, files_count: 0, total_page_count: 0, created_at: '2026-10-07 10:00:00', files: [] },
  /* ⑦ 历史老订单（v5 之前无 order_files 明细，父订单只有 total_price）→ 此前显示 ¥0.00 */
  { order_id: 7, order_number: 'HN20260901-0001', source: 'wechat', openid: 'oOld', nickname: '老用户',
    status: 'sent', total_price: 3.00, files_count: 0, total_page_count: 0, created_at: '2026-09-01 10:00:00', files: [] },
  /* ⑧ 父子金额口径不一致（线上真实数据：HN20261008-0026 父订单按本地 0.2/0.3 算出 ¥9.20，
        文件级按云端 0.3/0.4 算出 ¥12.30）→ 不能把差异原因误述成「含被放弃的文件」 */
  { order_id: 8, order_number: 'HN20261008-0026', source: 'local', openid: 'local', owner_name: '乙成员',
    is_admin_print: true, status: 'sent', total_price: 9.20, files_count: 1, total_page_count: 61,
    created_at: '2026-10-08 21:52:53',
    files: [{ id: 80, file_name: '模拟电子技术实验指导书.docx', copies: 1, page_count: 61, duplex: 'on', total_price: 12.30, status: 'sent' }] },
];
sandbox.__ORDERS = ORDERS;

sandbox.__RENDER = null;
vm.runInContext(`
  STORE = { config: { members: [{ id: 'ma', name: '甲成员', color: '#4472C4' }], memberBindings: [], passwords: {} },
            records: { expenses: [], income: [], scores: [], transfers: [] }, notes: {}, meta: {} };
  _cloudData = { orders: __ORDERS };
  _cloudStatusFilter = 'all'; _scopeMembers = null; _scopeSources = null;
  _ordersPage = 1; _ordersPerPage = 20; _cloudExpanded = {};
  __RENDER = { html: _renderOrderListHTML(), agg: _cloudAgg(__ORDERS),
               aggOne: _cloudAgg([__ORDERS[0]]), aggSix: _cloudAgg([__ORDERS[5]]),
               aggSeven: _cloudAgg([__ORDERS[6]]), aggEight: _cloudAgg([__ORDERS[7]]) };
`, sandbox);

let bad = 0;
function chk(name, cond, detail) {
  if (!cond) bad++;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (!cond && detail ? '  -- ' + detail : ''));
}

const R = sandbox.__RENDER;
const out = R.html || '';
console.log('RENDER_OK, length =', out.length);

/* 取某订单的订单行 HTML（到 </tr> 为止，不含展开的文件行） */
function rowOf(htmlStr, oid) {
  for (const chunk of htmlStr.split('<tr class="order-row')) {
    if (chunk.includes('data-order="' + oid + '"')) return chunk.split('</tr>')[0];
  }
  return '';
}
/* 第 6 个 td 即「金额」列（含 <td ...> 开标签，便于断言颜色与悬停提示） */
function amountCell(row) {
  const m = [...row.matchAll(/<td[^>]*>[\s\S]*?<\/td>/g)];
  return m.length > 5 ? m[5][0] : '';
}

/* 取某订单的展开文件行区块（文件级金额都在这里） */
function expandOf(htmlStr, oid) {
  for (const chunk of htmlStr.split('<tr class="file-expand')) {
    if (chunk.includes('data-suborder="' + oid + '"')) return chunk;
  }
  return '';
}

console.log('--- 场景 1：整单放弃（截图 HN20261008-0021）---');
const r1 = rowOf(out, 1), a1 = amountCell(r1);
chk('订单行存在', r1.length > 0);
chk('金额不再清零（显示 ¥2.00）', a1.includes('¥2.00'), a1);
chk('金额不出现 ¥0.00', !a1.includes('¥0.00'), a1);
chk('金额仍用「放弃打印」状态色（紫 #7B1FA2）', /color:\s*#7B1FA2/i.test(a1), a1);
chk('悬停提示说明不计入收益', a1.includes('不计入收益') && a1.includes('放弃打印'), a1);
chk('悬停提示给出订单金额', a1.includes('订单金额 ¥2.00'), a1);
const e1 = expandOf(out, 1);
chk('文件级金额照常显示（¥1.20 / ¥0.80 未被牵连）', e1.includes('¥1.20') && e1.includes('¥0.80'), e1.slice(0, 220));

console.log('--- 场景 2：父订单 total_price 缺失 → 退回子任务费合计 ---');
const a2 = amountCell(rowOf(out, 2));
chk('金额 = 子任务合计 ¥2.70', a2.includes('¥2.70'), a2);
chk('金额不出现 ¥0.00', !a2.includes('¥0.00'), a2);

console.log('--- 场景 3：收益统计口径完全不变 ---');
chk('整单放弃订单：展示 ¥2.00 但收益记录为 ¥0.00', Math.round(R.aggOne.record * 100) === 0, String(R.aggOne.record));
chk('取消占位单：收益记录 ¥0.00', Math.round(R.aggSix.record * 100) === 0, String(R.aggSix.record));
chk('历史老订单：展示金额但不计入收益（记录 ¥0.00，口径不变）', Math.round(R.aggSeven.record * 100) === 0, String(R.aggSeven.record));
/* 全量：sent 3.20 + 自打 0（剔除）+ 混合单只计 sent 的 1.00 = 4.20 元（_cloudAgg 内部为整数分） */
chk('全量收益记录 = ¥4.20（放弃/取消/自打均剔除）', Math.round(R.agg.record) === 420, String(R.agg.record) + ' 分');
chk('收益全部落在已入账，应收未收为 0',
    Math.round(R.agg.paid) === 420 && Math.round(R.agg.receivable) === 0,
    'paid=' + R.agg.paid + ' receivable=' + R.agg.receivable);

console.log('--- 场景 4：其他状态行为回归（不受影响）---');
const a3 = amountCell(rowOf(out, 3));
chk('已完成订单仍是原金额 ¥3.20', a3.includes('¥3.20'), a3);
chk('已完成订单金额仍为绿色 #2E7D32', /color:\s*#2E7D32/i.test(a3), a3);
const a4 = amountCell(rowOf(out, 4));
chk('自打已完成订单金额仍为 var(--primary)', a4.includes('var(--primary)'), a4);
chk('自打已完成订单金额仍展示 ¥1.00', a4.includes('¥1.00'), a4);
const a5 = amountCell(rowOf(out, 5));
chk('混合单显示整单金额 ¥1.50（含被放弃文件的 ¥0.50）', a5.includes('¥1.50'), a5);
chk('混合单金额带 * 提示', a5.includes('>*</span>'), a5);
chk('混合单悬停给出「计入收益 ¥1.00」与原因', a5.includes('计入收益 ¥1.00') && a5.includes('含 1 个不计入收益的文件'), a5);
const e5 = expandOf(out, 5);
chk('混合单展开的文件级金额合计 = 行金额（1.00 + 0.50）', e5.includes('¥1.00') && e5.includes('¥0.50'), e5.slice(0, 220));
const a6 = amountCell(rowOf(out, 6));
chk('无金额的取消占位单仍为 ¥0.00', a6.includes('¥0.00'), a6);
chk('金额为 0 的订单不带 * 提示', !a6.includes('>*</span>'), a6);

console.log('--- 场景 4b：历史老订单（无 order_files 明细）---');
const a7 = amountCell(rowOf(out, 7));
chk('历史老订单金额不再清零（显示父订单 ¥3.00）', a7.includes('¥3.00'), a7);
chk('历史老订单金额带 * 提示', a7.includes('>*</span>'), a7);
chk('历史老订单悬停说明「无文件明细、未计入收益统计」',
    a7.includes('该订单无文件明细') && a7.includes('计入收益 ¥0.00'), a7);

console.log('--- 场景 4c：父子金额口径不一致（HN20261008-0026 真实形态）---');
const a8 = amountCell(rowOf(out, 8));
chk('显示父订单金额 ¥9.20', a8.includes('¥9.20'), a8);
chk('带 * 提示（金额 ≠ 收益记录）', a8.includes('>*</span>'), a8);
chk('悬停明确是「文件级与订单级金额不一致」而非「含被放弃的文件」',
    a8.includes('文件级与订单级金额不一致') && !a8.includes('不计入收益的文件'), a8);
chk('悬停按自打口径措辞（不是「计入收益 ¥12.30」）',
    a8.includes('自打订单仅展示、不计入收益') && !a8.includes('计入收益 ¥12.30'), a8);
chk('自打订单仍不计入收益（记录 ¥0.00）', Math.round(R.aggEight.record * 100) === 0, String(R.aggEight.record));
const a4x = amountCell(rowOf(out, 4));
chk('非部分差异的自打单悬停也说明「仅展示、不计入收益」', a4x.includes('自打订单仅展示、不计入收益'), a4x);

console.log('--- 场景 5：区块说明与旧文案 ---');
// S1-05（2026-10-23）：S1-09 新增「离线未同步 / 云端拒收」两态且不计收益 ⇒ 标题说明
// 同步扩成完整排除清单。判据取「必须出现」而不是整句字面（以后再加状态不至于假红），
// 但仍然钉住三件事：旧四类排除状态在、新增两态也被明确排除、结论句没被删。
chk('标题说明已含「放弃」等排除状态',
    out.includes('自打/放弃/取消/打回/失败')
    && out.includes('订单金额仅展示、不计入收益')
    && out.includes('离线未同步')
    && out.includes('云端拒收'));
chk('标题说明含 * 图例', out.includes('的金额含未计入收益的部分'));
chk('放弃订单的悬停提示不再是「云端收益记录 ¥0.00」', !a1.includes('云端收益记录'));

console.log('--- 场景 6：状态筛选/分页下不回归 ---');
sandbox.__ABNORMAL = null;
vm.runInContext(`
  _cloudStatusFilter = 'abnormal'; _ordersPage = 1;
  __ABNORMAL = _renderOrderListHTML();
`, sandbox);
const abn = sandbox.__ABNORMAL;
chk('异常筛选包含放弃订单且金额仍为 ¥2.00', abn.includes('¥2.00') && !amountCell(rowOf(abn, 1)).includes('¥0.00'));
chk('异常筛选不含已完成订单行', rowOf(abn, 3) === '');

console.log(bad ? ('存在失败 ❌ ' + bad + ' 项') : '全部通过 ✅');
process.exit(bad ? 1 : 0);
