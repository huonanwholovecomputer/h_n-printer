/* eslint-disable */
/**
 * 微信小程序「我」页 —— 「近 N 条订单」卡片验收（需求①）
 *
 * 契约：
 *   GET /api/admin/recent_orders        → {success, limit, orders:[{order_id, order_number, owner,
 *                                          owner_masked, created_at, file_count, page_count,
 *                                          total_price, status, source, is_admin_print}]}
 *   GET /api/admin/recent_orders_limit  → {success, limit, range:[1,100]}（**只读**，小程序端不做 N 编辑器）
 * 判据：
 *   1) 只有管理员才渲染、才发请求（非管理员 0 请求）；角色判据必须是 role === 'admin'
 *      —— 后端 compute_role() 只返回 admin/user/guest，超管被折叠成 admin，
 *      写 role === 'super_admin' 永远不命中；
 *   2) 标题 N 取接口返回的 limit（默认 10），不用本地常量；
 *   3) 每行有订单号 / 归属人（owner_masked 优先，点击看完整 owner）/ 文件数·页数 / 金额；
 *   4) 金额点击复用既有「复制价格」气泡与 _copyText 格式（订单号 + 空格 + ¥金额）；
 *   5) 进入页面拉一次（15s 内去重）+ 手动刷新，不轮询；失败静默保留旧数据。
 *
 * 用法：node tests/verify_wechat_recent_orders.js   （exit 0 = 全绿）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..', '..');
const ME_JS = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'me', 'me.js');
const ME_WXML = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'me', 'me.wxml');
const ME_WXSS = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'me', 'me.wxss');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }

/* 去掉注释后再做「判据不能是 xxx」类静态断言：注释里提到反例是好事，不算违规 */
function stripJsComments(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"\\])\/\/[^\n]*/g, '$1');
}
function stripHtmlComments(src) {
  return src.replace(/<!--[\s\S]*?-->/g, '');
}

/* 'a[1].b' → data.a[1].b = v */
function setByPath(obj, key, val) {
  const m = key.match(/^([A-Za-z0-9_$]+)((\[\d+\]|\.[A-Za-z0-9_$]+)*)$/);
  if (!m) { obj[key] = val; return; }
  const parts = m[2].match(/\[\d+\]|\.[A-Za-z0-9_$]+/g) || [];
  if (!parts.length) { obj[m[1]] = val; return; }
  let cur = obj[m[1]];
  if (cur === undefined || cur === null) { obj[m[1]] = cur = {}; }
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    const isLast = i === parts.length - 1;
    const k = p[0] === '[' ? Number(p.slice(1, -1)) : p.slice(1);
    if (isLast) { cur[k] = val; return; }
    cur = cur[k];
    if (cur === undefined || cur === null) throw new Error('bad path ' + key);
  }
}

/* utils/price_copy.js：文案唯一实现（2026-11-09 从 me.js 抽出，两页共用）——真实加载它，
   只把它的 './pages' 依赖换成同一份页数 shim，避免 me.js 的依赖被静默放过。 */
const PRICE_COPY_MOD = (() => {
  const cp = path.join(REPO, 'mobile_apps', 'h_n_print', 'utils', 'price_copy.js');
  const m = { exports: {} };
  new Function('module', 'exports', 'require', fs.readFileSync(cp, 'utf8'))(m, m.exports, (dep) => {
    if (dep === './pages') return { countPagesInRange: () => 1 };
    throw new Error('utils/price_copy.js unexpected require: ' + dep);
  });
  if (typeof m.exports.priceDetailLines !== 'function') {
    throw new Error('utils/price_copy.js 未导出 priceDetailLines（复制详细价格会失效）');
  }
  return m.exports;
})();

/* ---------------- 加载真实 pages/me/me.js（Component 模式） ---------------- */
function loadMePage() {
  const src = fs.readFileSync(ME_JS, 'utf8');
  let cfg = null;
  const store = { userRole: 'admin', token: 'tk-1' };
  const calls = { requests: [], toasts: [], modals: [], clipboard: [], setData: 0 };
  let nextResponse = { statusCode: 200, data: { success: true } };
  let requestHook = null;
  let responder = null;

  const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map,
    parseInt, parseFloat, isNaN,
    setTimeout, clearTimeout, setInterval, clearInterval,
    require: (p) => {
      if (p.indexOf('utils/config') >= 0) return { CONFIG: { BASE_URL: 'https://example.test' } };
      if (p.indexOf('utils/request') >= 0) {
        return {
          request: (opt) => {
            calls.requests.push(opt);
            if (responder) {
              const r = responder(opt);
              if (r) {
                if (opt.success) opt.success(r);
                if (opt.complete) opt.complete(r);
                return;
              }
            }
            if (requestHook) requestHook(opt);
            else {
              if (opt.success) opt.success(nextResponse);
              if (opt.complete) opt.complete(nextResponse);
            }
          },
          createPollScheduler: () => ({ start() {}, stop() {} }),
        };
      }
      if (p.indexOf('utils/pages') >= 0) return { countPagesInRange: () => 1 };
      if (p.indexOf('utils/price_copy') >= 0) return PRICE_COPY_MOD;
      if (p.indexOf('utils/avatar_crop') >= 0) return {};
      throw new Error('unexpected require: ' + p);
    },
    Component: (c) => { cfg = c; },
    Page: (c) => { cfg = c; },
    getApp: () => ({ globalData: {}, syncThemeFromServer() {} }),
    wx: {
      getStorageSync: (k) => (k in store ? store[k] : ''),
      setStorageSync: (k, v) => { store[k] = v; },
      removeStorageSync: (k) => { delete store[k]; },
      showToast: (o) => calls.toasts.push(o),
      showModal: (o) => calls.modals.push(o),
      hideToast() {},
      setClipboardData: (o) => {
        calls.clipboard.push(o.data);
        if (o.success) o.success();
      },
      getWindowInfo: () => ({ windowWidth: 375, windowHeight: 667, safeArea: { bottom: 667 } }),
      getSystemInfoSync: () => ({ windowWidth: 375, windowHeight: 667, platform: 'devtools' }),
      createSelectorQuery: () => ({
        select: () => ({
          boundingClientRect() { return this; },
        }),
        exec(cb) {
          cb && cb([{ top: 100, bottom: 140, left: 200, right: 300, width: 100, height: 40 }]);
        },
      }),
      nextTick: (fn) => setTimeout(fn, 0),
      setNavigationBarTitle() {},
      switchTab() {},
      showLoading() {},
      hideLoading() {},
    },
  };
  sandbox.global = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: 'me.js' });
  if (!cfg) throw new Error('pages/me/me.js 没有调用 Component()/Page()');

  const page = Object.assign({}, cfg, cfg.methods);
  page.data = JSON.parse(JSON.stringify(cfg.data || {}));
  page.setData = function (patch, cb) {
    calls.setData++;
    Object.keys(patch || {}).forEach((k) => setByPath(this.data, k, patch[k]));
    if (cb) cb();
  };
  page._scheduleMeasure = () => {};
  page.createSelectorQuery = sandbox.wx.createSelectorQuery;

  return {
    page,
    calls,
    store,
    setResponse(r) { nextResponse = r; },
    hookRequest(fn) { requestHook = fn; },
    hookRespond(fn) { responder = fn; },
    reset() {
      calls.requests.length = 0;
      calls.toasts.length = 0;
      calls.modals.length = 0;
      calls.clipboard.length = 0;
    },
  };
}

const ORDER_FIXTURE = {
  success: true,
  limit: 10,
  orders: [
    {
      order_id: 7, order_number: 'HN-0007', owner: '张三（full）', owner_masked: '张*',
      created_at: '2026-11-02 10:00', file_count: 2, page_count: 5, total_price: 3.5,
      status: 'sent', source: 'cloud', is_admin_print: false,
    },
    {
      order_id: 8, order_number: '', owner: '李四', owner_masked: '',
      created_at: '2026-11-02 10:05', file_count: 1, page_count: 1, total_price: 0,
      status: 'queued', source: 'local', is_admin_print: true,
    },
  ],
};

function runStaticChecks() {
  section('一、静态判据（源码接线）');
  const js = fs.readFileSync(ME_JS, 'utf8');
  const wxml = fs.readFileSync(ME_WXML, 'utf8');
  const wxss = fs.readFileSync(ME_WXSS, 'utf8');
  const APP_JSON = path.join(REPO, 'mobile_apps', 'h_n_print', 'app.json');
  const RO_DIR = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'recent-orders');
  const appJson = fs.readFileSync(APP_JSON, 'utf8');
  const roJs = fs.readFileSync(path.join(RO_DIR, 'recent-orders.js'), 'utf8');
  const roWxml = fs.readFileSync(path.join(RO_DIR, 'recent-orders.wxml'), 'utf8');
  const roWxss = fs.readFileSync(path.join(RO_DIR, 'recent-orders.wxss'), 'utf8');
  const roJson = fs.readFileSync(path.join(RO_DIR, 'recent-orders.json'), 'utf8');

  check('me.js 有 loadRecentOrders（进入页面拉一次）', /loadRecentOrders\s*\(/.test(js));
  check('me.js 有 onRefreshRecentOrders（手动刷新）', /onRefreshRecentOrders\s*\(/.test(js));
  check('me.js 有 onRecentOrderOwnerTap（归属人点击看完整昵称）',
        /onRecentOrderOwnerTap\s*\(/.test(js));
  check('data 初始值：recentOrders / recentOrdersLimit / recentOrdersLoading / pricePopRecent',
        /recentOrders:\s*\[\]/.test(js) && /recentOrdersLimit:\s*10/.test(js)
        && /recentOrdersLoading:\s*false/.test(js) && /pricePopRecent:\s*false/.test(js));
  check('请求的是 /api/admin/recent_orders', js.indexOf("'/api/admin/recent_orders'") >= 0);
  check('不带 method 兜底 POST：显式 GET', /url:\s*CONFIG\.BASE_URL \+ '\/api\/admin\/recent_orders',\s*\n\s*method:\s*'GET'/.test(js)
        || /\/api\/admin\/recent_orders[\s\S]{0,120}method:\s*'GET'/.test(js));
  check('带 Authorization: Bearer', /'Authorization':\s*'Bearer ' \+ token/.test(js));
  check('管理员判据用 role === \'admin\'（后端把超管折叠成 admin）',
        js.indexOf("this.data.userRole !== 'admin'") >= 0);
  check('「近 N 条订单」相关代码里不出现 super_admin 判据（永不命中）',
        !/['"]super_admin['"]/.test(stripJsComments(js))
        && !/super_admin/.test(stripHtmlComments(wxml).replace(/is_super_admin/g, '')));
  check('15s 新鲜度守卫（切 tab 回来不重复请求）', js.indexOf('15000') >= 0 && /_recentOrdersAt/.test(js));
  check('并发守卫（刷新中不重入）', /_recentOrdersLoading/.test(js));
  check('不轮询：没有为 recent_orders 建轮询调度器',
        !/createPollScheduler\([\s\S]{0,200}recent_orders/.test(js)
        && js.indexOf('_startRecentOrdersPolling') < 0);
  check('失败静默：成功分支之外不弹错误遮罩',
        !/recent_orders[\s\S]{0,200}showModal/.test(js));
  check('N 上限夹取 [1,100]（契约 range）', /Math\.min\(100/.test(js) && /Math\.max\(1/.test(js));

  check('WXML：卡片根节点 recent-orders-section + admin-collapsible',
        wxml.indexOf('recent-orders-section') >= 0 && /recent-orders-section admin-collapsible/.test(wxml));
  check('WXML：管理员门控 userRole === \'admin\'', /recent-orders-section[\s\S]{0,200}userRole === 'admin'/.test(wxml));
  check('WXML：标题动态「近 {{recentOrdersLimit}} 条订单」',
        wxml.indexOf('近 {{recentOrdersLimit}} 条订单') >= 0);
  check('WXML：近 N 条改成入口卡（列表搬到独立页 recent-orders）',
        /recent-orders-entry[\s\S]{0,240}catchtap="onOpenRecentOrders"/.test(wxml));
  check('WXML：入口卡标题「查看最近订单」', wxml.indexOf('recent-orders-entry-title">查看最近订单') >= 0);
  check('WXML：入口卡说明带动态 N',
        /recent-orders-entry-desc">全部管理员的最近 \{\{recentOrdersLimit\}\} 条/.test(wxml));
  check('js：入口卡 onOpenRecentOrders 走 _navigateWithAnimation（返回本页纯淡入，不播位移）',
        /onOpenRecentOrders\s*\([^)]*\)\s*\{[\s\S]{0,400}_navigateWithAnimation\(\s*'\/pages\/recent-orders\/recent-orders'\s*\)/.test(js));
  check('WXML：内嵌列表已移除（无 recent-order-row / price-tap-r-）',
        wxml.indexOf('recent-order-row') < 0 && wxml.indexOf('price-tap-r-') < 0);
  check('WXML：价格气泡仍按来源隐藏「复制详细价格」',
        /wx:if="\{\{!pricePopRecent\}\}"[\s\S]{0,120}onCopyOrderDetailPrice/.test(wxml));
  check('app.json 注册 pages/recent-orders/recent-orders',
        appJson.indexOf('"pages/recent-orders/recent-orders"') >= 0);
  check('新页 json：navigationStyle custom + navigation-bar 组件',
        /"navigationStyle"\s*:\s*"custom"/.test(roJson) && /navigation-bar/.test(roJson));
  check('新页 js：调 /api/admin/recent_orders 且带 page/per_page',
        roJs.indexOf("'/api/admin/recent_orders'") >= 0
        && /data:\s*\{\s*page:\s*[\s\S]{0,60}per_page:/.test(roJs));
  check('新页 js：per_page 白名单 10/20/50/100', /\[10,\s*20,\s*50,\s*100\]/.test(roJs));
  check('新页 js：先取 /api/admin/recent_orders_limit 再拉列表（#1：首屏不闪全站订单）',
        roJs.indexOf('/api/admin/recent_orders_limit') >= 0
        && roJs.indexOf('/api/admin/recent_orders_limit') < roJs.indexOf("'/api/admin/recent_orders'"));
  check('新页 js：total 先按「最近 N 条」截断，再换算 totalPages',
        /const cappedTotal = Math\.min\(totalSafe, cap\)/.test(roJs)
        && /Math\.ceil\(\s*cappedTotal\s*\/\s*perPage\s*\)/.test(roJs));
  check('新页 js：本页行按上限截断（slice(0, cappedTotal - offset)）',
        /\.slice\(0, Math\.max\(0, cappedTotal - offset\)\)/.test(roJs));
  check('新页 js：上一页/下一页/页码 + 页码算法',
        /onPrevPage\s*\(/.test(roJs) && /onNextPage\s*\(/.test(roJs)
        && /onPageChange\s*\(/.test(roJs) && /getPageNumbers\s*\(/.test(roJs));
  check('新页 js：非管理员直接空态（noPermission，不发请求）',
        /noPermission/.test(roJs) && /role !== 'admin'/.test(roJs));
  check('新页 js：statusMap 含 timeout=超时', /timeout\s*:\s*'超时'/.test(roJs));
  check('新页 wxml：分页条 + 页码 + 条数下拉',
        roWxml.indexOf('ro-pagebar') >= 0 && roWxml.indexOf('pageNumbers') >= 0
        && roWxml.indexOf('onSelectPageSize') >= 0);
  check('新页 wxml：金额行点击复制价格（onPriceCopy）', /onPriceCopy/.test(roWxml));
  check('新页 wxml：异常状态可点（onStatusReasonTap）', /onStatusReasonTap/.test(roWxml));
  check('新页 wxml：#3 原因气泡是 .theme-dark 遮罩的子节点（深色变量才能作用到白底/文字）',
        /class="ro-pop-mask \{\{isDarkMode \? 'theme-dark' : ''\}\}"/.test(roWxml)
        && roWxml.indexOf('class="ro-pop-mask') < roWxml.indexOf('class="ro-pop ')
        && roWxml.indexOf('wx:if="{{showStatusPop}}"') === roWxml.lastIndexOf('wx:if="{{showStatusPop}}"'));
  check('新页 js：#4 _decorate 透传 manual_status_by/at + status_reason（否则永远走兜底文案）',
        /manual_status_by:\s*src\.manual_status_by/.test(roJs)
        && /manual_status_at:\s*src\.manual_status_at/.test(roJs)
        && /status_reason:\s*src\.status_reason/.test(roJs));
  check('新页 wxss：气泡 fixed（出 page-root，不被 scroll-content 变换层裁）',
        /\.ro-pop-mask\s*\{[^}]*position:\s*fixed/.test(roWxss)
        && /\.ro-pop\s*\{[^}]*position:\s*fixed/.test(roWxss));
  check('新页 wxss：状态色含 timeout（橙，区别于 failed 红）',
        /\.status-timeout\s*\{[^}]*#EF6C00/.test(roWxss));
  check('WXSS：卡片样式就位（不引入新色板，全走既有变量）',
        /\.recent-orders-card\s*\{/.test(wxss) && /\.recent-order-row\s*\{/.test(wxss)
        && /\.recent-order-amount\s*\{/.test(wxss));
  const block = (wxss.match(/\.recent-orders-section\.admin-expanded\s*\{[^}]*\}/) || [''])[0];
  check('WXSS：admin-expanded 上限放大（100 行不被 3100rpx 裁半截）',
        /max-height:\s*(\d+)rpx/.test(block) && Number(RegExp.$1) > 3100, block.replace(/\s+/g, ' '));
  const newColors = (wxss.slice(wxss.indexOf('.recent-orders-section'), wxss.indexOf('.license-card, .storage-card')))
    .match(/#[0-9a-fA-F]{3,8}/g) || [];
  check('WXSS：新卡片不引入新色值（0 个硬编码 hex）', newColors.length === 0, newColors.join(','));

  /* ---- 超管专属 N 编辑器（POST /api/admin/recent_orders_limit）---- */
  const editorSrc = js.slice(js.indexOf('_canEditRecentLimit'), js.indexOf('onCancelRecentOrdersLimit'));
  check('N 编辑器：超管判据用 isSuperAdmin 标记（不是 userRole === super_admin）',
        /isSuperAdmin === true/.test(editorSrc) && editorSrc.indexOf("'super_admin'") < 0);
  check('N 编辑器：写接口 POST /api/admin/recent_orders_limit',
        editorSrc.indexOf("'/api/admin/recent_orders_limit'") >= 0);
  check('N 编辑器：method POST + body 字段名 limit',
        /method:\s*'POST'/.test(editorSrc) && /data:\s*\{\s*limit:\s*n\s*\}/.test(editorSrc));
  check('N 编辑器：本地夹取 1..100（契约 range）', /Math\.max\(1,\s*Math\.min\(100/.test(editorSrc));
  check('N 编辑器：失败只轻提示、不弹遮罩（无 showModal）',
        editorSrc.indexOf('showModal') < 0 && /showToast\(\{\s*title:\s*'保存失败/.test(editorSrc));
  check('WXML：入口只对 isSuperAdmin 渲染',
        /wx:if="\{\{isSuperAdmin\}\}"[\s\S]{0,240}onEditRecentOrdersLimit/.test(wxml));
  check('WXML：编辑器面板 wx:if isSuperAdmin && recentLimitEditor',
        /wx:if="\{\{isSuperAdmin && recentLimitEditor\}\}"/.test(wxml));
  check('WXML：输入框 bindinput → onRecentOrdersLimitInput',
        /bindinput="onRecentOrdersLimitInput"/.test(wxml));
  check('WXML：+/− 步进 data-delta', /data-delta="-1"[\s\S]{0,400}data-delta="1"/.test(wxml));
  check('WXML：保存/取消接线', /catchtap="onSaveRecentOrdersLimit"/.test(wxml)
        && /catchtap="onCancelRecentOrdersLimit"/.test(wxml));
  check('WXSS：编辑器样式就位', /\.recent-limit-editor\s*\{/.test(wxss) && /\.recent-limit-input\s*\{/.test(wxss));
}

function runBehaviorChecks() {
  section('二、行为判据（真实 loadRecentOrders / 气泡 / 复制格式）');
  const h = loadMePage();
  const { page, calls } = h;

  /* ① 非管理员：不发请求 */
  page.data.userRole = 'user';
  h.store.userRole = 'user';
  h.reset();
  page.loadRecentOrders(true);
  check('①非管理员：0 请求', calls.requests.length === 0, 'req=' + calls.requests.length);

  /* ② 管理员：发一次 GET，带 Bearer */
  page.data.userRole = 'admin';
  h.store.userRole = 'admin';
  h.store.token = 'tk-1';
  h.reset();
  h.setResponse({ statusCode: 200, data: ORDER_FIXTURE });
  page.loadRecentOrders(true);
  check('②管理员：发 1 次请求', calls.requests.length === 1, 'req=' + calls.requests.length);
  const req = calls.requests[0] || {};
  check('②URL = /api/admin/recent_orders', String(req.url).indexOf('/api/admin/recent_orders') >= 0, req.url);
  check('②method = GET', req.method === 'GET', req.method);
  check('②Authorization: Bearer tk-1', (req.header || {}).Authorization === 'Bearer tk-1',
        JSON.stringify(req.header));

  /* ③ 成功：limit 取接口值，行字段齐全 */
  check('③limit 取接口返回的 10', page.data.recentOrdersLimit === 10, String(page.data.recentOrdersLimit));
  check('③渲染 2 行', page.data.recentOrders.length === 2, String(page.data.recentOrders.length));
  const r0 = page.data.recentOrders[0] || {};
  const r1 = page.data.recentOrders[1] || {};
  check('③归属人优先 owner_masked（张*）', r0.ownerText === '张*', r0.ownerText);
  check('③无 owner_masked 时退回 owner（李四）', r1.ownerText === '李四', r1.ownerText);
  check('③金额 toFixed(2)（3.50 / 0.00）', r0.priceDisplay === '3.50' && r1.priceDisplay === '0.00',
        r0.priceDisplay + ',' + r1.priceDisplay);
  check('③文件数·页数文案（2 文件 · 5 页）', r0.statText === '2 文件 · 5 页', r0.statText);
  check('③状态文案走既有 statusMap（sent→已完成 / queued→排队中）',
        r0.statusText === page.data.statusMap.sent && r1.statusText === page.data.statusMap.queued,
        r0.statusText + ',' + r1.statusText);
  check('③loading 复位', page.data.recentOrdersLoading === false);

  /* ④ 15s 新鲜度：不重复拉；force 才拉 */
  h.reset();
  page.loadRecentOrders();
  check('④15s 内再进页面：0 新请求', calls.requests.length === 0, 'req=' + calls.requests.length);
  h.reset();
  page.loadRecentOrders(true);
  check('④手动刷新（force）：1 次新请求', calls.requests.length === 1, 'req=' + calls.requests.length);

  /* ⑤ 失败静默：保留旧数据、不弹遮罩 */
  h.reset();
  h.setResponse({ statusCode: 500, data: { success: false } });
  const before = JSON.stringify(page.data.recentOrders);
  page.loadRecentOrders(true);
  check('⑤失败：旧数据保留（不闪空）', JSON.stringify(page.data.recentOrders) === before);
  check('⑤失败：不弹 modal / toast（静默）', calls.modals.length === 0 && calls.toasts.length === 0,
        'modals=' + calls.modals.length + ' toasts=' + calls.toasts.length);
  check('⑤失败：loading 复位', page.data.recentOrdersLoading === false);

  /* ⑥ limit 夹取：契约 range [1,100] */
  h.reset();
  h.setResponse({ statusCode: 200, data: Object.assign({}, ORDER_FIXTURE, { limit: 500 }) });
  page.loadRecentOrders(true);
  check('⑥limit=500 → 夹到 100', page.data.recentOrdersLimit === 100, String(page.data.recentOrdersLimit));
  h.reset();
  h.setResponse({ statusCode: 200, data: Object.assign({}, ORDER_FIXTURE, { limit: 0 }) });
  page.loadRecentOrders(true);
  check('⑥limit=0 → 回落默认 10', page.data.recentOrdersLimit === 10, String(page.data.recentOrdersLimit));

  /* ⑦ 行数不超过 limit */
  h.reset();
  const many = Object.assign({}, ORDER_FIXTURE, {
    limit: 3,
    orders: [0, 1, 2, 3, 4, 5].map((i) => Object.assign({}, ORDER_FIXTURE.orders[0], { order_id: 100 + i })),
  });
  h.setResponse({ statusCode: 200, data: many });
  page.loadRecentOrders(true);
  check('⑦orders 多于 limit 时只留前 limit 条', page.data.recentOrders.length === 3,
        String(page.data.recentOrders.length));

  /* ⑧ _findOrderById：r- 前缀查近 N 条；普通 id 查 orders */
  h.reset();
  h.setResponse({ statusCode: 200, data: ORDER_FIXTURE });
  page.loadRecentOrders(true);
  check('⑧_findOrderById("r-7") 命中近 N 条（不在分页 orders 里也能复制价格）',
        (page._findOrderById('r-7') || {}).order_number === 'HN-0007');
  check('⑧_findOrderById("r-999") → null（不误取 orders 里的同 id）',
        page._findOrderById('r-999') === null);
  page.data.orders = [{ id: 42, order_number: 'HN-0042', total_price: 9 }];
  check('⑧_findOrderById("42") 仍走既有 orders 查找',
        (page._findOrderById('42') || {}).order_number === 'HN-0042');

  /* ⑨ 金额气泡：pricePopRecent 区分来源；复制格式与既有口径一致 */
  h.reset();
  page.data.showPricePop = false;
  page.onPriceTap({ currentTarget: { dataset: { id: 'r-7' } } });
  check('⑨近 N 条行：pricePopRecent=true（气泡只留「复制价格」）', page.data.pricePopRecent === true);
  check('⑨近 N 条行：pricePopOrderId=r-7', String(page.data.pricePopOrderId) === 'r-7');
  page.data.showPricePop = false;
  page.onPriceTap({ currentTarget: { dataset: { id: '42' } } });
  check('⑨订单详情行：pricePopRecent=false（保留「复制详细价格」）', page.data.pricePopRecent === false);

  page.data.showPricePop = false;
  page.onCopyOrderPrice({ currentTarget: { dataset: { id: 'r-7' } } });
  check('⑨复制文本格式 = `订单号 — ¥金额`（对齐本地工具 _on_copy_total）',
        calls.clipboard[0] === 'HN-0007 — ¥3.50', JSON.stringify(calls.clipboard[0]));
  check('⑨toast 文案「已复制价格」（复用 _copyText）',
        (calls.toasts[0] || {}).title === '已复制价格', JSON.stringify(calls.toasts[0]));

  /* ⑩ 归属人点击：有 owner → modal 显示完整昵称；无 owner → toast */
  h.reset();
  page.onRecentOrderOwnerTap({ currentTarget: { dataset: { owner: '张三（full）' } } });
  check('⑩点归属人：modal 显示完整 owner', (calls.modals[0] || {}).content === '张三（full）',
        JSON.stringify(calls.modals[0]));
  h.reset();
  page.onRecentOrderOwnerTap({ currentTarget: { dataset: { owner: '' } } });
  check('⑩无归属人：toast「该订单未记录归属人」',
        (calls.toasts[0] || {}).title === '该订单未记录归属人', JSON.stringify(calls.toasts[0]));

  /* ⑪ 无 token：不发请求（避免 401 噪音） */
  h.store.token = '';
  h.reset();
  page.loadRecentOrders(true);
  check('⑪无 token：0 请求', calls.requests.length === 0, 'req=' + calls.requests.length);
}

function runLimitEditorChecks() {
  section('三、超管 N 编辑器（POST /api/admin/recent_orders_limit）');
  const h = loadMePage();
  const { page, calls } = h;

  page.data.userRole = 'admin';
  h.store.userRole = 'admin';
  h.store.token = 'tk-1';
  h.reset();
  h.setResponse({ statusCode: 200, data: ORDER_FIXTURE });
  page.loadRecentOrders(true);

  /* ① 普通管理员（非超管）：入口无效、不发写请求 */
  page.data.isSuperAdmin = false;
  h.reset();
  page.onEditRecentOrdersLimit();
  check('①普通管理员：点设置不打开编辑器', page.data.recentLimitEditor === false);
  page.setData({ recentLimitDraft: '15' });
  page.onSaveRecentOrdersLimit();
  check('①普通管理员：onSave 不发写请求',
        calls.requests.filter((r) => r.method === 'POST').length === 0);

  /* ② 超管：打开编辑器，草稿预填当前 N */
  page.data.isSuperAdmin = true;
  h.reset();
  page.onEditRecentOrdersLimit();
  check('②超管：编辑器打开', page.data.recentLimitEditor === true);
  check('②超管：草稿预填当前 N（10）', page.data.recentLimitDraft === '10', page.data.recentLimitDraft);
  page.onEditRecentOrdersLimit();
  check('②再点一次收起并清空草稿',
        page.data.recentLimitEditor === false && page.data.recentLimitDraft === '');

  /* ③ 步进与 1..100 夹取 */
  page.setData({ recentLimitEditor: true, recentLimitDraft: '10' });
  page.onRecentOrdersLimitStep({ currentTarget: { dataset: { delta: 1 } } });
  check('③+1 → 11', page.data.recentLimitDraft === '11', page.data.recentLimitDraft);
  page.onRecentOrdersLimitStep({ currentTarget: { dataset: { delta: -1 } } });
  page.onRecentOrdersLimitStep({ currentTarget: { dataset: { delta: -1 } } });
  check('③−2 → 9', page.data.recentLimitDraft === '9', page.data.recentLimitDraft);
  page.setData({ recentLimitDraft: '100' });
  page.onRecentOrdersLimitStep({ currentTarget: { dataset: { delta: 1 } } });
  check('③上限夹到 100', page.data.recentLimitDraft === '100', page.data.recentLimitDraft);
  page.setData({ recentLimitDraft: '1' });
  page.onRecentOrdersLimitStep({ currentTarget: { dataset: { delta: -1 } } });
  check('③下限夹到 1', page.data.recentLimitDraft === '1', page.data.recentLimitDraft);

  /* ④ 输入过滤：只留数字 */
  page.onRecentOrdersLimitInput({ detail: { value: '1a2.5 ' } });
  check('④输入只保留数字（1a2.5 → 125）', page.data.recentLimitDraft === '125', page.data.recentLimitDraft);

  /* ⑤ 非法值：不请求、轻提示 */
  h.reset();
  page.setData({ recentLimitDraft: '0' });
  page.onSaveRecentOrdersLimit();
  check('⑤草稿 0：不发请求', calls.requests.length === 0);
  check('⑤草稿 0：toast「请输入 1~100 的整数」',
        (calls.toasts[0] || {}).title === '请输入 1~100 的整数', JSON.stringify(calls.toasts[0]));
  h.reset();
  page.setData({ recentLimitDraft: '' });
  page.onSaveRecentOrdersLimit();
  check('⑤草稿空：不发请求', calls.requests.length === 0);

  /* ⑥ 合法保存：POST 契约 + 成功后标题/列表立刻刷新 */
  h.reset();
  page.setData({ recentLimitDraft: '15' });
  h.hookRespond((opt) => {
    if (String(opt.url).indexOf('/api/admin/recent_orders_limit') >= 0) {
      return { statusCode: 200, data: { success: true, limit: 15, range: [1, 100], message: '已更新为最近 15 条' } };
    }
    if (String(opt.url).indexOf('/api/admin/recent_orders') >= 0) {
      return { statusCode: 200, data: Object.assign({}, ORDER_FIXTURE, { limit: 15 }) };
    }
    return null;
  });
  page.onSaveRecentOrdersLimit();
  const post = calls.requests.filter((r) => r.method === 'POST')[0] || {};
  check('⑥写请求 method POST', post.method === 'POST', post.method);
  check('⑥写请求 URL = /api/admin/recent_orders_limit',
        String(post.url).indexOf('/api/admin/recent_orders_limit') >= 0, post.url);
  check('⑥body 字段名 limit=15', (post.data || {}).limit === 15, JSON.stringify(post.data));
  check('⑥带 Authorization: Bearer', (post.header || {}).Authorization === 'Bearer tk-1');
  check('⑥成功后标题立刻变成 15', page.data.recentOrdersLimit === 15, String(page.data.recentOrdersLimit));
  check('⑥成功后编辑器收起、草稿清空',
        page.data.recentLimitEditor === false && page.data.recentLimitDraft === '');
  check('⑥成功 toast 用后端 message',
        (calls.toasts[0] || {}).title === '已更新为最近 15 条', JSON.stringify(calls.toasts[0]));
  const getAfter = calls.requests.filter((r) => r.method === 'GET'
    && String(r.url).indexOf('/api/admin/recent_orders') >= 0);
  check('⑥成功后立刻按新 N 重拉列表（GET 1 次）', getAfter.length === 1, 'gets=' + getAfter.length);

  /* ⑦ POST 失败（403）：保持旧值、草稿保留、轻提示、不弹遮罩 */
  h.reset();
  page.setData({ recentOrdersLimit: 15, recentLimitEditor: true, recentLimitDraft: '30' });
  h.hookRespond((opt) => {
    if (String(opt.url).indexOf('/api/admin/recent_orders_limit') >= 0) {
      return { statusCode: 403, data: { success: false, error: '仅超级管理员可修改' } };
    }
    return null;
  });
  page.onSaveRecentOrdersLimit();
  check('⑦失败：标题保持旧值 15', page.data.recentOrdersLimit === 15, String(page.data.recentOrdersLimit));
  check('⑦失败：草稿保留（可改后重试）', page.data.recentLimitDraft === '30', page.data.recentLimitDraft);
  check('⑦失败：不弹遮罩', calls.modals.length === 0);
  check('⑦失败：轻提示「保存失败，请稍后重试」',
        (calls.toasts[0] || {}).title === '保存失败，请稍后重试', JSON.stringify(calls.toasts[0]));
  check('⑦失败：saving 复位', page.data.recentLimitSaving === false);

  /* ⑧ 无 token：不发写请求 */
  h.store.token = '';
  h.reset();
  page.setData({ recentLimitDraft: '20' });
  page.onSaveRecentOrdersLimit();
  check('⑧无 token：0 写请求', calls.requests.length === 0);
}

runStaticChecks();
runBehaviorChecks();
runLimitEditorChecks();

console.log('\n' + '='.repeat(52));
if (failures) {
  console.log(`「近 N 条订单」验收: ${failures} 项失败 ❌`);
  process.exit(1);
}
console.log('「近 N 条订单」验收: 全部通过 ✅');
process.exit(0);
