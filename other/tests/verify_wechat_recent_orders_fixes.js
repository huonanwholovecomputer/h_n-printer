/* eslint-disable */
/**
 * 微信小程序「最近订单」页 —— 2026-11-03 四个问题的验收（#1 #2 #3 #4）
 *
 * 判据：
 *   #1 「查看最近订单」只显示「我」页设置的最近 N 条：
 *      - 先 GET /api/admin/recent_orders_limit 拿 N，再拉列表（顺序不能反）；
 *      - total 先按 N 截断再算 totalPages，行数也按 N 截断（后端 paging 的 total 是全站量，且不认 limit）；
 *      - 设置接口失败时按默认 10 兜底，绝不退回「全部订单」。
 *   #2 从本页返回「我」页只播 fade：入口走 _navigateWithAnimation（写入 _meReturnFromSub），
 *      且「我」页 show() 里 returnFromSub 分支必须排在 tabFrom===0（page-enter-right）之前。
 *   #3 原因气泡是 .theme-dark 遮罩的子节点（深色 CSS 变量才作用到 --bg-card/--text-primary）。
 *   #4 _decorate 透传 manual_status_by/manual_status_at/status_reason；
 *      文案口径与 settlement.html 一致：{标记人}在{YYYY-MM-DD HH:MM:SS}被标记为{状态}。
 *
 * 用法：node other/tests/verify_wechat_recent_orders_fixes.js   （exit 0 = 全绿）
 *
 * 「能红」自证：把 HEAD 版本复制到临时目录，再用 MP_ROOT 指过去，四个问题应一起报错：
 *   mkdir -p /tmp/mp_head/mobile_apps/h_n_print/pages/{recent-orders,me}
 *   git show HEAD:mobile_apps/h_n_print/pages/recent-orders/recent-orders.js   > /tmp/mp_head/.../recent-orders.js
 *   git show HEAD:mobile_apps/h_n_print/pages/recent-orders/recent-orders.wxml > /tmp/mp_head/.../recent-orders.wxml
 *   git show HEAD:mobile_apps/h_n_print/pages/me/me.js                         > /tmp/mp_head/.../me.js
 *   MP_ROOT=/tmp/mp_head node other/tests/verify_wechat_recent_orders_fixes.js   # 期望 exit 1（23 项 FAIL）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

// MP_ROOT：只想验证「旧代码会红」时，把 HEAD 版本复制到临时目录再指过来（见本文件末尾注释）
const REPO = process.env.MP_ROOT ? path.resolve(process.env.MP_ROOT) : path.resolve(__dirname, '..', '..');
const RO_JS = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'recent-orders', 'recent-orders.js');
const RO_WXML = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'recent-orders', 'recent-orders.wxml');
const ME_JS = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'me', 'me.js');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }

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

/* ---------------- 加载真实 pages/recent-orders/recent-orders.js ---------------- */
function loadRecentPage(opts) {
  const o = opts || {};
  const src = fs.readFileSync(RO_JS, 'utf8');
  let cfg = null;
  const store = Object.assign({ token: 'tk-1', userRole: 'admin' }, o.store || {});
  const calls = { requests: [], toasts: [] };
  let responder = o.responder || null;

  const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map,
    parseInt, parseFloat, isFinite, isNaN,
    setTimeout: (fn) => { try { fn(); } catch (e) {} },
    clearTimeout() {}, setInterval() {}, clearInterval() {},
    require: (p) => {
      if (p.indexOf('utils/config') >= 0) return { CONFIG: { BASE_URL: 'https://example.test' } };
      if (p.indexOf('utils/request') >= 0) {
        return {
          request: (opt) => {
            calls.requests.push(opt);
            const r = responder && responder(opt);
            if (r) {
              if (opt.success) opt.success(r);
              if (opt.complete) opt.complete(r);
            }
          },
        };
      }
      throw new Error('unexpected require: ' + p);
    },
    Component: (c) => { cfg = c; },
    Page: (c) => { cfg = c; },
    getApp: () => ({ globalData: { isDarkMode: true } }),
    wx: {
      getStorageSync: (k) => (k in store ? store[k] : ''),
      setStorageSync: (k, v) => { store[k] = v; },
      removeStorageSync: (k) => { delete store[k]; },
      showToast: (x) => calls.toasts.push(x),
      showModal() {},
      setClipboardData() {},
      getWindowInfo: () => ({ windowWidth: 375, windowHeight: 667 }),
      getSystemInfoSync: () => ({ windowWidth: 375, windowHeight: 667 }),
      createSelectorQuery: () => ({
        select() { return this; },
        boundingClientRect() { return this; },
        exec(cb) { cb && cb([{ top: 100, bottom: 140, left: 200, right: 300, width: 100, height: 500 }]); },
      }),
      nextTick: (fn) => fn(),
    },
  };
  sandbox.global = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: 'recent-orders.js' });
  if (!cfg) throw new Error('recent-orders.js 没有调用 Component()');

  const page = Object.assign({}, cfg, cfg.methods, cfg.lifetimes || {});
  page.pageLifetimes = cfg.pageLifetimes || {};
  page.data = JSON.parse(JSON.stringify(cfg.data || {}));
  page.setData = function (patch, cb) {
    Object.keys(patch || {}).forEach((k) => setByPath(this.data, k, patch[k]));
    if (cb) cb();
  };
  page.createSelectorQuery = sandbox.wx.createSelectorQuery;
  // 滚动引擎与本次断言无关，直接跳过测量（否则会在 attached 里发 selectorQuery）
  page._initScrollEngine = () => {};

  return {
    page,
    calls,
    store,
    enter() { page.attached(); },
    setResponder(fn) { responder = fn; },
  };
}

/* 订单 fixture：第 page 页、每页 perPage 条，手动订正留痕只挂在第 1 页第 2 条 */
function makeOrders(page, perPage, total) {
  const rows = [];
  for (let i = 0; i < perPage; i++) {
    const idx = (page - 1) * perPage + i;
    if (idx >= total) break;
    rows.push({
      order_id: 1000 + idx,
      order_number: 'HN-' + (1000 + idx),
      owner: '张三',
      owner_masked: '张*',
      created_at: '2026-11-02 10:00:00',
      file_count: 1,
      page_count: 2,
      total_price: 0.6,
      status: 'sent',
      source: 'cloud',
      is_admin_print: false,
      status_reason: '',
      manual_status_by: '',
      manual_status_at: '',
    });
  }
  if (page === 1 && rows.length > 1) {
    rows[1].status = 'canceled';
    rows[1].manual_status_by = '张管理员';
    rows[1].manual_status_at = '2026-11-01 09:00:00';
  }
  if (page === 1 && rows.length > 2) {
    rows[2].status = 'failed';
    rows[2].status_reason = '打印机断线';
  }
  return rows;
}

function standardResponder(cfg) {
  return (opt) => {
    if (opt.url.indexOf('/api/admin/recent_orders_limit') >= 0) {
      if (cfg.limitFails) return { statusCode: 500, data: { success: false } };
      return { statusCode: 200, data: { success: true, limit: cfg.limit } };
    }
    if (opt.url.indexOf('/api/admin/recent_orders') >= 0) {
      return {
        statusCode: 200,
        data: {
          success: true,
          orders: makeOrders(opt.data.page, opt.data.per_page, cfg.total),
          total: cfg.total,
          page: opt.data.page,
          per_page: opt.data.per_page,
          total_pages: Math.ceil(cfg.total / opt.data.per_page),
        },
      };
    }
    throw new Error('unexpected request: ' + opt.url);
  };
}

/* ==================== #1：只显示设置的最近 N 条 ==================== */
section('#1 「查看最近订单」按设置截断（N=5，全站 23 条）');
{
  const h = loadRecentPage({ responder: standardResponder({ limit: 5, total: 23 }) });
  h.enter();
  const p = h.page;
  const urls = h.calls.requests.map((r) => r.url.split('/api/')[1]);
  check('先取 limit 再拉订单（顺序不能反）',
        urls.length === 2 && urls[0] === 'admin/recent_orders_limit' && urls[1] === 'admin/recent_orders',
        JSON.stringify(urls));
  check('拿 N=5（来自设置接口，不是本地常量）', p.data.recentLimit === 5, String(p.data.recentLimit));
  check('只显示 5 条（全站 total=23 被截断）', p.data.orders.length === 5, 'rows=' + p.data.orders.length);
  check('汇总 totalOrders=5（不是 23）', p.data.totalOrders === 5, String(p.data.totalOrders));
  check('totalPages=1（按 N 算页数）', p.data.totalPages === 1, String(p.data.totalPages));
  check('serverTotalOrders=23（仅用于提示「全站共 23 条」）',
        p.data.serverTotalOrders === 23, String(p.data.serverTotalOrders));
  check('首屏 loading 已收起', p.data.loading === false);
  const before = h.calls.requests.length;
  p.onNextPage();
  check('N=5 时「下一页」无效（不再请求第 2 页）', h.calls.requests.length === before,
        'requests=' + h.calls.requests.length);
}

section('#1 分页仍按 N 截断（N=25，全站 40 条，每页 20）');
{
  const h = loadRecentPage({ responder: standardResponder({ limit: 25, total: 40 }) });
  h.enter();
  const p = h.page;
  // 旧代码不发 limit 请求，requests[1] 不存在 —— 用空对象兜底，让断言干净地红而不是抛类型错误
  const reqOf = (i) => h.calls.requests[i] || { data: {} };
  check('第 1 页请求 page=1 / per_page=20',
        reqOf(1).data.page === 1 && reqOf(1).data.per_page === 20,
        JSON.stringify(reqOf(1).data));
  check('totalOrders=25（截断）、totalPages=2', p.data.totalOrders === 25 && p.data.totalPages === 2,
        p.data.totalOrders + '/' + p.data.totalPages);
  check('第 1 页 20 行', p.data.orders.length === 20, 'rows=' + p.data.orders.length);
  p.onNextPage();
  check('第 2 页请求 page=2', reqOf(2).data.page === 2, JSON.stringify(reqOf(2).data));
  check('第 2 页只剩 5 行（后端的 20 行被 N 截掉）', p.data.orders.length === 5,
        'rows=' + p.data.orders.length);
  check('第 2 页 currentPage=2、totalPages=2', p.data.currentPage === 2 && p.data.totalPages === 2);
  p.onNextPage();
  check('N=25 时第 3 页不存在（请求数不再增长）', h.calls.requests.length === 3,
        'requests=' + h.calls.requests.length);
}

section('#1 设置接口失败：按默认 10 兜底，不退化成全部订单');
{
  const h = loadRecentPage({ responder: standardResponder({ limit: 0, total: 40, limitFails: true }) });
  h.enter();
  const p = h.page;
  check('limit 失败后 recentLimit=10（默认）', p.data.recentLimit === 10, String(p.data.recentLimit));
  check('仍只显示 10 条（全站 40）', p.data.orders.length === 10 && p.data.totalOrders === 10,
        'rows=' + p.data.orders.length + ' total=' + p.data.totalOrders);
}

/* ==================== #4：原因文案 ==================== */
section('#4 手动标记订单的原因文案（与 settlement.html 三分支同口径）');
{
  const h = loadRecentPage({ responder: standardResponder({ limit: 10, total: 3 }) });
  h.enter();
  const rows = h.page.data.orders;
  const byId = (id) => rows.filter((r) => r.order_id === id)[0] || {};
  check('manual_status_by/at 已透传进行数据',
        byId(1001).manual_status_by === '张管理员' && byId(1001).manual_status_at === '2026-11-01 09:00:00',
        byId(1001).manual_status_by + ' / ' + byId(1001).manual_status_at);
  check('第一支：{标记人}在{YYYY-MM-DD HH:MM:SS}被标记为{状态}',
        byId(1001).statusReasonText === '张管理员在2026-11-01 09:00:00被标记为已取消',
        byId(1001).statusReasonText);
  check('第一支缺时间：写「未知时间」而不是空串/破折号（含 settlement 口径）',
        h.page._statusReasonText({ status: 'canceled', manual_status_by: '张管理员', manual_status_at: '' })
          === '张管理员在未知时间被标记为已取消',
        h.page._statusReasonText({ status: 'canceled', manual_status_by: '张管理员', manual_status_at: '' }));
  check('第二支：没有人工留痕时用后端 status_reason',
        byId(1002).statusReasonText === '打印机断线', byId(1002).statusReasonText);
  check('第三支：都没有时仍有可复制文案（不返回空串）',
        byId(1000).statusReasonText === '该订单未提供具体原因', byId(1000).statusReasonText);
}

section('#1 汇总行 wx:if / wx:elif 链未被新增行打断');
{
  // WXML 的 wx:elif 必须紧跟 wx:if/wx:elif 兄弟节点；中间插一个带 wx:if 的节点会让 elif 失去配对
  const wxmlNoComment = fs.readFileSync(RO_WXML, 'utf8').replace(/<!--[\s\S]*?-->/g, '');
  const lines = wxmlNoComment.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  const idx = lines.findIndex((l) => l.indexOf('wx:elif=') >= 0);
  check('「加载中…」的 wx:elif 紧跟在 wx:if 之后（没有被子节点打断）',
        idx > 0 && /wx:(if|elif)=/.test(lines[idx - 1]),
        'idx=' + idx + ' prev=' + JSON.stringify(lines[idx - 1] || ''));
}

/* ==================== #2：返回「我」页只播淡入 ==================== */
section('#2 二级页面返回只播 fade（不播位移）');
{
  const meJs = fs.readFileSync(ME_JS, 'utf8');
  const meNoComment = meJs.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"\\])\/\/[^\n]*/g, '$1');
  check('入口 onOpenRecentOrders 走 _navigateWithAnimation（写入 _meReturnFromSub）',
        /onOpenRecentOrders\s*\([^)]*\)\s*\{[\s\S]{0,400}_navigateWithAnimation\(\s*'\/pages\/recent-orders\/recent-orders'\s*\)/.test(meNoComment));
  check('入口不再直接 wx.navigateTo（否则 returnFromSub 标记永远不写）',
        !/onOpenRecentOrders\s*\([^)]*\)\s*\{[^}]*wx\.navigateTo/.test(meNoComment));
  const idxReturn = meNoComment.indexOf('if (returnFromSub)');
  const idxTab = meNoComment.indexOf('} else if (tabFrom === 0)');
  check('show()：returnFromSub 分支排在 tabFrom===0 之前', idxReturn > 0 && idxTab > idxReturn,
        'returnFromSub@' + idxReturn + ' tabFrom@' + idxTab);
  const between = idxReturn > 0 && idxTab > idxReturn ? meNoComment.slice(idxReturn, idxTab) : '';
  check('show()：returnFromSub 分支用 page-fade-in（纯淡入）',
        between.indexOf("animationClass = 'page-fade-in'") >= 0);
}

/* ==================== #3：气泡跟随主题 ==================== */
section('#3 原因气泡跟随深色主题（静态结构判据）');
{
  const wxml = fs.readFileSync(RO_WXML, 'utf8').replace(/<!--[\s\S]*?-->/g, '');
  check('遮罩带 theme-dark 动态类',
        /class="ro-pop-mask \{\{isDarkMode \? 'theme-dark' : ''\}\}"/.test(wxml));
  check('气泡是遮罩的子节点（同一 DOM 块内，不是兄弟节点）',
        wxml.indexOf('class="ro-pop ') > wxml.indexOf('class="ro-pop-mask'));
  check('遮罩只此一个（气泡不再被 wx:if 重复渲染成同级节点）',
        wxml.indexOf('wx:if="{{showStatusPop}}"') === wxml.lastIndexOf('wx:if="{{showStatusPop}}"'));
  const js = fs.readFileSync(RO_JS, 'utf8');
  check('气泡本体的 catchtap 有真实 handler（noop）', /\bnoop\s*\(\)\s*\{\s*\}/.test(js));
  // 嵌套改结构最容易漏闭合标签：这里做一次标签栈平衡校验（注释先剥掉，自闭合标签不压栈）
  const bare = wxml.replace(/<!--[\s\S]*?-->/g, '');
  const tagRe = /<(\/?)([a-zA-Z][\w-]*)((?:[^>"']|"[^"]*"|'[^']*')*?)(\/?)>/g;
  const stack = [];
  let ok = true;
  let m;
  while ((m = tagRe.exec(bare))) {
    if (m[4] === '/') continue;
    if (m[1] === '/') {
      if (stack.pop() !== m[2]) { ok = false; break; }
    } else {
      stack.push(m[2]);
    }
  }
  check('WXML 标签闭合平衡（气泡嵌套没有漏闭合）', ok && stack.length === 0, 'unclosed=' + stack.join(','));
}

console.log('\n====================================================');
console.log(failures === 0 ? '「最近订单」四问题验收: 全部通过 ✅' : `「最近订单」四问题验收: ${failures} 项失败 ❌`);
process.exit(failures === 0 ? 0 : 1);
