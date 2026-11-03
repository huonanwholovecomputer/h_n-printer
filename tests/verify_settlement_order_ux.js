/* 验收 · 结算页 v7.15 订单明细交互（Node + VM 内真实执行 settlement.html 内联 JS）
 *
 * 覆盖四件事（都要求「真跑代码」，不是 grep 源码）：
 *   #4 订单明细搜索：文件名 / 备注、大小写不敏感、命中片段 <mark class="hit">、XSS 必须转义、
 *      搜索只影响列表（_cloudFilteredOrders 仍是全量口径，KPI/图表/页脚不受影响）；
 *   #9 状态 tooltip 三支：manual_status_by（谁在什么时候标记成什么）→ status_reason → 通用文案；
 *      timeout=超时（琥珀色 class / 中文名）；applyOrderStatus 请求体带 by 且成功后本地落地标记人；
 *   #5 价格复制格式：单行 `{订单号} — ¥{金额}`（em dash 两侧空格）；详细复制逐字复刻本地打印工具
 *      （订单号 / 14 短横线 / 计费明细 / 每文件三行 / 可选附加服务费 / 14 短横线 / 💰合计）；
 *   标记人下拉：默认成员名单第一个，localStorage 记住上次选择，名单变了要退回第一个。
 *
 * 用法：node tests/verify_settlement_order_ux.js [settlement.html 路径]
 * exit 0 = 全部通过。
 */
const fs = require('fs'), vm = require('vm'), path = require('path');
const HTML_PATH = process.argv[2] || path.join(__dirname, '..', 'local_print_tool', 'finance', 'settlement.html');
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
const LS = {};
const storage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(LS, k) ? LS[k] : null),
  setItem: (k, v) => { LS[k] = String(v); },
  removeItem: (k) => { delete LS[k]; },
};
const windowProto = {
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  addEventListener: () => {},
  removeEventListener: () => {},
  location: { search: '', hash: '', href: 'http://localhost/', origin: 'http://localhost' },
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

let bad = 0;
function chk(name, cond, detail) {
  if (!cond) bad++;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (!cond && detail !== undefined ? '  -- ' + detail : ''));
}

/* ==================== 注入数据 ==================== */
const ORDERS = [
  /* 1：正常单，两个文件（1 个双面 2 页 ¥0.40；1 个单面 2 份 ¥0.60），订单实收 ¥2.00 → 派送费 ¥1.00 */
  { order_id: 1, order_number: 'HN20261103-0001', source: 'local', openid: 'local', owner_name: '甲成员',
    status: 'sent', total_price: 2.00, files_count: 2, created_at: '2026-11-03 09:00:00',
    delivery_enabled: 1, delivery_percentage: 100,
    remark: '加急 电桥 实验报告',
    files: [
      { id: 11, file_name: '电桥法测电阻.pdf', copies: 1, page_count: 2, duplex: 'on', page_range: '',
        total_price: 0.40, effective_pages: 2, status: 'sent' },
      { id: 12, file_name: '作业.docx', copies: 2, page_count: 1, duplex: 'off', page_range: '1',
        total_price: 0.60, effective_pages: 1, status: 'failed', status_reason: '纸张卡住' } ] },
  /* 2：人工订正过的取消单（tooltip 第一支） */
  { order_id: 2, order_number: 'HN20261102-0002', source: 'local', openid: 'local', owner_name: '甲成员',
    status: 'canceled', total_price: 1.00, files_count: 1, created_at: '2026-11-02 09:00:00',
    manual_status_by: '乙成员', manual_status_at: '2026-11-02T09:30:00',
    files: [{ id: 21, file_name: '取消.pdf', copies: 1, page_count: 1, duplex: 'on',
      total_price: 1.00, effective_pages: 1, status: 'canceled' }] },
  /* 3：超时单（新状态） */
  { order_id: 3, order_number: 'HN20261102-0003', source: 'local', openid: 'local', owner_name: '甲成员',
    status: 'timeout', total_price: 0.30, files_count: 1, created_at: '2026-11-02 08:00:00',
    files: [{ id: 31, file_name: '超时.pdf', copies: 1, page_count: 1, duplex: 'off',
      total_price: 0.30, effective_pages: 1, status: 'timeout' }] },
  /* 4：后端 status_reason（tooltip 第二支） */
  { order_id: 4, order_number: 'HN20261101-0004', source: 'local', openid: 'local', owner_name: '甲成员',
    status: 'failed', total_price: 0.30, files_count: 1, created_at: '2026-11-01 08:00:00',
    status_reason: '打印机离线', files: [] },
  /* 5：备注里带 HTML（搜索命中的那段必须被转义，不能当标签执行）；带一个文件才会渲染备注行 */
  { order_id: 5, order_number: 'HN20261101-0005', source: 'local', openid: 'local', owner_name: '甲成员',
    status: 'sent', total_price: 0.30, files_count: 1, created_at: '2026-11-01 07:00:00',
    remark: '<img src=x onerror=alert(1)>电桥',
    files: [{ id: 51, file_name: 'note.pdf', copies: 1, page_count: 1, duplex: 'off',
      total_price: 0.30, effective_pages: 1, status: 'sent' }] },
];
sandbox.__ORDERS = ORDERS;

vm.runInContext(`
  STORE = { config: { members: [{ id: 'ma', name: '甲成员', color: '#4472C4' },
                               { id: 'mb', name: '乙成员', color: '#ED7D31' }],
                      memberBindings: [], passwords: {} },
            records: { expenses: [], income: [], scores: [], transfers: [] }, notes: {}, meta: {} };
  _cloudData = { orders: __ORDERS };
  _cloudStatusFilter = 'all'; _scopeMembers = null; _scopeSources = null;
  _ordersPage = 1; _ordersPerPage = 20; _cloudExpanded = {}; _ordersSearch = '';
`, sandbox);

/* ==================== #4 搜索：命中 / 高亮 / 转义 / 口径隔离 ==================== */
console.log('--- #4 搜索：文件名与备注、大小写、高亮、XSS ---');
vm.runInContext(`_ordersSearch = '作业'; __H1 = _currentOrderList().map(function (o) { return o.order_id; });`, sandbox);
chk('搜文件名「作业」只命中订单 1', JSON.stringify(sandbox.__H1) === '[1]', JSON.stringify(sandbox.__H1));

vm.runInContext(`_ordersSearch = '电桥'; __H2 = _currentOrderList().map(function (o) { return o.order_id; });`, sandbox);
chk('搜「电桥」同时命中文件名（订单1）与备注（订单5）',
  JSON.stringify(sandbox.__H2) === '[1,5]', JSON.stringify(sandbox.__H2));

vm.runInContext(`_ordersSearch = 'DOCX'; __H3 = _currentOrderList().map(function (o) { return o.order_id; });`, sandbox);
chk('大小写不敏感（DOCX 命中 .docx）', JSON.stringify(sandbox.__H3) === '[1]', JSON.stringify(sandbox.__H3));

vm.runInContext(`_ordersSearch = '查无此单'; __H4 = _currentOrderList().length;`, sandbox);
chk('无命中时列表为空（空态由渲染层处理）', sandbox.__H4 === 0, String(sandbox.__H4));

vm.runInContext(`
  _ordersSearch = '电桥';
  __SEARCH_HTML = _renderOrderListHTML();
  __ALL_COUNT = _cloudFilteredOrders().length;
`, sandbox);
const sh = sandbox.__SEARCH_HTML;
chk('搜索不影响口径链：_cloudFilteredOrders 仍是全部 5 单', sandbox.__ALL_COUNT === 5, String(sandbox.__ALL_COUNT));
chk('命中片段包 <mark class="hit">', sh.includes('<mark class="hit">电桥</mark>'), sh.slice(0, 300));
chk('备注里的 <img onerror> 被转义（XSS 不可执行）',
  sh.includes('&lt;img src=x onerror=alert(1)&gt;') && !sh.includes('<img src=x'), sh.slice(0, 400));
chk('未命中的订单不渲染（订单 2 不在列表里）', !sh.includes('HN20261102-0002'), '');
chk('搜索框带输入即筛与清空按钮', sh.includes('id="ordersSearchInput"')
  && sh.includes('oninput="onOrdersSearch(this.value)"') && sh.includes('onOrdersSearch(&#39;&#39;)') || sh.includes("onOrdersSearch('')"));

/* 搜索与状态筛选叠加：abnormal 里没有 sent 单，所以「电桥」也搜不到订单 1 */
vm.runInContext(`
  _ordersSearch = '电桥'; _cloudStatusFilter = 'abnormal';
  __H5 = _currentOrderList().map(function (o) { return o.order_id; });
  _cloudStatusFilter = 'all'; _ordersSearch = '';
`, sandbox);
chk('搜索与状态筛选叠加（abnormal 下 sent 单不出现）', JSON.stringify(sandbox.__H5) === '[]', JSON.stringify(sandbox.__H5));

/* ==================== #9 tooltip / timeout / 标记人 ==================== */
console.log('--- #9 状态 tooltip、timeout、标记人 ---');
const tip = (oid, st) => vm.runInContext(`_statusTip(_findCloudOrder('${oid}'), ${st ? `'${st}'` : 'undefined'})`, sandbox);
chk('tooltip 第一支：谁在什么时候被标记为什么（时间截到秒、T 变空格）',
  tip('2') === '乙成员在2026-11-02 09:30:00被标记为已取消', tip('2'));
chk('tooltip 第二支：后端 status_reason', tip('4') === '失败：打印机离线', tip('4'));
chk('tooltip 第三支：通用文案', tip('3') === '当前状态：超时', tip('3'));
chk('缺时间时不出现「undefined」', tip('2', 'failed') === '乙成员在2026-11-02 09:30:00被标记为失败', tip('2', 'failed'));

chk('statusLabel(timeout) = 超时', sandbox.statusLabel('timeout') === '超时', sandbox.statusLabel('timeout'));
chk('statusClass(timeout) = st-timeout', sandbox.statusClass('timeout') === 'st-timeout', sandbox.statusClass('timeout'));
chk('图表状态色补了 timeout（琥珀 #F59E0B）',
  sandbox.CHART_COLORS && sandbox.CHART_COLORS.status && sandbox.CHART_COLORS.status.timeout === '#F59E0B',
  JSON.stringify(sandbox.CHART_COLORS && sandbox.CHART_COLORS.status));
chk('timeout 计入异常分组（总数恒等式不丢单）', (() => {
  const g = vm.runInContext(`_statusGroups({ sent: 1, timeout: 2, failed: 1 })`, sandbox);
  return g.abnormal === 3 && g.done === 1 && g.rate === 25;   // done 1 / (总数 4 - 取消 0)
})());
chk('ORDER_FIXABLE_STATUSES 不含 timeout（人工订正白名单不变）',
  vm.runInContext(`ORDER_FIXABLE_STATUSES.indexOf('timeout')`, sandbox) === -1);

chk('标记人默认取成员名单第一个', vm.runInContext(`_manualStatusBy()`, sandbox) === '甲成员',
  vm.runInContext(`_manualStatusBy()`, sandbox));
LS['finance.manualStatusBy.v1'] = '乙成员';
chk('标记人记住上次选择（localStorage）', vm.runInContext(`_manualStatusBy()`, sandbox) === '乙成员',
  vm.runInContext(`_manualStatusBy()`, sandbox));
LS['finance.manualStatusBy.v1'] = '已离职的人';
chk('名单里没有的名字自动退回第一个', vm.runInContext(`_manualStatusBy()`, sandbox) === '甲成员',
  vm.runInContext(`_manualStatusBy()`, sandbox));
LS['finance.manualStatusBy.v1'] = '甲成员';
chk('标记人下拉列出全部成员且当前项选中',
  /<option value="甲成员" selected>/.test(vm.runInContext(`_manualBySelectHTML()`, sandbox))
  && vm.runInContext(`_manualBySelectHTML()`, sandbox).includes('乙成员'));

/* 订单行/文件行渲染里必须带 tooltip，状态标签仍是 tag */
vm.runInContext(`_ordersSearch = ''; __UX_HTML = _renderOrderListHTML();`, sandbox);
const ux = sandbox.__UX_HTML;
chk('订单行状态标签带 title=标记人文案', ux.includes('title="乙成员在2026-11-02 09:30:00被标记为已取消"'), '');
chk('文件行状态标签带 title=status_reason 文案', ux.includes('title="失败：纸张卡住"'), '');
chk('金额格可点（price-copy + openPriceMenu）',
  ux.includes('class="price-copy"') && ux.includes('openPriceMenu(event,'), '');

/* applyOrderStatus：请求体带 by，成功后本地落地标记人（tooltip 立刻能显示） */
console.log('--- #9 订正请求体带标记人 ---');
sandbox.__BODY = null;
sandbox.__REQ = null;
vm.runInContext(`
  fetch = function (url, opts) {
    __REQ = url;
    try { __BODY = JSON.parse(opts.body); } catch (e) { __BODY = opts.body; }
    return Promise.resolve({ json: function () { return Promise.resolve({ success: true, manual_status_at: '2026-11-03 10:00:00' }); }, ok: true });
  };
  unlocked.orderstatus = true;
  applyOrderStatus('1', 'failed');
`, sandbox);
setTimeout(() => {
  chk('订正请求打到 /api/proxy/admin/order_status', String(sandbox.__REQ).includes('/api/proxy/admin/order_status'), sandbox.__REQ);
  chk('请求体含 order_id / status / by', sandbox.__BODY && String(sandbox.__BODY.order_id) === '1'
    && sandbox.__BODY.status === 'failed' && sandbox.__BODY.by === '甲成员', JSON.stringify(sandbox.__BODY));
  chk('成功后本地落地 manual_status_by / manual_status_at（优先服务器时间）',
    ORDERS[0].manual_status_by === '甲成员' && ORDERS[0].manual_status_at === '2026-11-03 10:00:00',
    JSON.stringify({ by: ORDERS[0].manual_status_by, at: ORDERS[0].manual_status_at }));
  chk('文件级也同步标记人（文件行 tooltip 与订单一致）',
    ORDERS[0].files[0].manual_status_by === '甲成员', JSON.stringify(ORDERS[0].files[0]));

  /* ==================== #5 价格复制格式 ==================== */
  console.log('--- #5 复制价格格式（与本地打印工具逐字一致）---');
  const line = vm.runInContext(`_priceLineText(_findCloudOrder('1'))`, sandbox);
  chk('单行复制 = `订单号 — ¥金额`（em dash U+2014、两侧各一空格、两位小数）',
    line === 'HN20261103-0001 \u2014 \u00a52.00', JSON.stringify(line));
  chk('单行复制不含「合计」「订单号:」等前缀', !/合计|订单号:/.test(line), line);

  const detail = vm.runInContext(`_priceDetailText(_findCloudOrder('1'))`, sandbox);
  const want = [
    '订单号: HN20261103-0001',
    '--------------',
    '计费明细',
    '--------------',
    '1. 电桥法测电阻.pdf',
    '   1x 双面 范围:全部',
    '   1张×0.40 = ¥0.40',
    '2. 作业.docx',
    '   2x 单面 范围:1',
    '   (1张×0.30)×2份 = ¥0.60',
    '附加服务费: ¥1.00',
    '--------------',
    '💰合计: ¥2.00',
  ].join('\n');
  chk('详细复制逐行一致（算式与本地 calc_cost 同形、含 3 条 14 短横线、缩进 3 空格、附加服务费在合计前）',
    detail === want, '\n--- got ---\n' + detail + '\n--- want ---\n' + want);

  const noExtra = vm.runInContext(`_priceDetailText(_findCloudOrder('2'))`, sandbox);
  chk('无附加服务费时不出现「附加服务费」行', !noExtra.includes('附加服务费'), noExtra);
  chk('无附加服务费时合计 = 订单实收', noExtra.trim().endsWith('💰合计: ¥1.00'), noExtra);
  chk('算式语法与本地一致：单面 `{n}张×{单价}`、双面 `{a}张×{DP}+{b}张×{SP}`、多份包括号', (() => {
    const single = vm.runInContext(`_fileCalc({ effective_pages: 3, copies: 1, duplex: 'off' }).formula`, sandbox);
    const multi = vm.runInContext(`_fileCalc({ effective_pages: 3, copies: 3, duplex: 'off' }).formula`, sandbox);
    const dual = vm.runInContext(`_fileCalc({ effective_pages: 3, copies: 1, duplex: 'on' }).formula`, sandbox);
    return single === '3张×0.30' && multi === '(3张×0.30)×3份' && dual === '1张×0.40+1张×0.30';
  })());

  /* 剪贴板：把 writeText 打桩，验证气泡按钮真的写进剪贴板 */
  console.log('--- #5 气泡按钮真的写剪贴板 ---');
  sandbox.__COPIED = null;
  /* 用普通对象当气泡节点：stub 的 Proxy 只认 innerHTML/value 等少数键，读 _priceOrder 会拿到新 stub */
  const __m = {
    _priceOrder: vm.runInContext(`_findCloudOrder('1')`, sandbox),
    _priceFile: null,
    _priceCents: 200,
    parentNode: null,
  };
  const origGet = documentProto.getElementById;
  documentProto.getElementById = (id) => (id === 'priceMenu' ? __m : origGet(id));
  sandbox.__COPY_ERR = null;
  try {
    vm.runInContext(`
      navigator.clipboard = { writeText: function (t) { __COPIED = t; return Promise.resolve(); } };
      copyPrice(false);
    `, sandbox);
  } catch (e) {
    sandbox.__COPY_ERR = e.message;
  }
  chk('「复制价格」写入剪贴板的正是单行格式', sandbox.__COPIED === 'HN20261103-0001 \u2014 \u00a52.00',
    JSON.stringify(sandbox.__COPIED) + (sandbox.__COPY_ERR ? ' err=' + sandbox.__COPY_ERR : ''));

  console.log(bad ? ('存在失败 ❌ ' + bad + ' 项') : '全部通过 ✅');
  process.exit(bad ? 1 : 0);
}, 30);
