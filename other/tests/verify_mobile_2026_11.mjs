/* eslint-disable */
/**
 * 移动端 2026-11 需求验收（小程序 + Android APP 的 www）
 *
 * 覆盖五件事：
 *   ① 「我的文件」与「接单设备显示顺序」两个弹窗加宽，列表文字不再出界；
 *   ② 攒批随时可切、三态读期望值（stack_desired/stack_view）+「⏳ 待应用」角标；
 *   ③ 管理员「最近订单」独立页（分页 10/20/50/100、总数、上下页），原「我」页只留入口卡；
 *   ④ 异常状态（failed/timeout/canceled/rejected/abandoned）原因气泡：复制 / 关闭；
 *   ⑤ 新状态 timeout（中文「超时」）在两端的状态映射与配色里都有，且与 failed 的红区分开。
 *
 * 口径参照：docs 的移动端两份文档（气泡必须 page-root 外 + position:fixed）、
 * 后端 task-1 契约 C2（status_reason / manual_status_by / manual_status_at）与
 * C4（stack_desired / stack_pending）、C5（/api/admin/recent_orders 分页）。
 *
 * 用法：node tests/verify_mobile_2026_11.mjs   （exit 0 = 全绿；不依赖真实后端）
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '..', '..');
const MP = path.join(REPO, 'mobile_apps', 'h_n_print');
const WWW = path.join(REPO, 'mobile_apps', 'android_app', 'www');

const F = {
  mpMeJs: path.join(MP, 'pages', 'me', 'me.js'),
  mpMeWxml: path.join(MP, 'pages', 'me', 'me.wxml'),
  mpMeWxss: path.join(MP, 'pages', 'me', 'me.wxss'),
  mpIndexJs: path.join(MP, 'pages', 'index', 'index.js'),
  mpIndexWxml: path.join(MP, 'pages', 'index', 'index.wxml'),
  mpIndexWxss: path.join(MP, 'pages', 'index', 'index.wxss'),
  mpAppWxss: path.join(MP, 'app.wxss'),
  mpAppJson: path.join(MP, 'app.json'),
  mpUserOrdersJs: path.join(MP, 'pages', 'user-orders', 'user-orders.js'),
  mpAuthUsersJs: path.join(MP, 'pages', 'authorized-users', 'authorized-users.js'),
  mpRoJs: path.join(MP, 'pages', 'recent-orders', 'recent-orders.js'),
  mpRoWxml: path.join(MP, 'pages', 'recent-orders', 'recent-orders.wxml'),
  mpRoWxss: path.join(MP, 'pages', 'recent-orders', 'recent-orders.wxss'),
  appJs: path.join(WWW, 'app.js'),
  appMeJs: path.join(WWW, 'me.js'),
  appRoJs: path.join(WWW, 'recent-orders.js'),
  appHtml: path.join(WWW, 'index.html'),
  appCss: path.join(WWW, 'styles.css'),
};
const read = (p) => fs.readFileSync(p, 'utf8');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }

/* ══════════════════════════════════════════════════════════════
   一、① 两个弹窗加宽（其余弹窗宽度不变）
   ══════════════════════════════════════════════════════════════ */
section('一、弹窗加宽 + 文字出界');
{
  const mpMeWxss = read(F.mpMeWxss);
  const mpIndexWxss = read(F.mpIndexWxss);
  const mpAppWxss = read(F.mpAppWxss);
  const appCss = read(F.appCss);

  // 根因：.modal-sheet 是 align-items:center 的纵向 flex，中间层 width:auto 会先按
  // max-content 收缩（被最长文件名撑开）⇒ 必须给列表/行显式 width:100%，弹窗本身给更宽的固定值。
  const sheetRule = (mpMeWxss.match(/\.myfiles-sheet\s*\{[^}]*\}/) || [''])[0];
  check('小程序「我的文件」弹窗加宽到 660rpx（原 540rpx）',
    /width:\s*660rpx/.test(sheetRule), sheetRule.replace(/\s+/g, ' '));
  check('小程序「我的文件」弹窗窄屏不越界（max-width: calc(100vw - 48rpx)）',
    /max-width:\s*calc\(100vw\s*-\s*48rpx\)/.test(sheetRule)
    && /box-sizing:\s*border-box/.test(sheetRule));
  const listRule = (mpMeWxss.match(/\.myfiles-list\s*\{[^}]*\}/) || [''])[0];
  check('小程序文件列表显式 width:100%（不再按 max-content 撑破弹窗）',
    /width:\s*100%/.test(listRule) && /box-sizing:\s*border-box/.test(listRule), listRule.replace(/\s+/g, ' '));
  const rowRule = (mpMeWxss.match(/\.myfile-row\s*\{[^}]*\}/) || [''])[0];
  check('小程序文件行 width:100% + box-sizing（行宽跟随列表）',
    /width:\s*100%/.test(rowRule) && /box-sizing:\s*border-box/.test(rowRule));
  const metaRule = (mpMeWxss.match(/\.myfile-meta\s*\{[^}]*\}/) || [''])[0];
  check('小程序文件时间/大小单行省略（不换行撑高、不溢出）',
    /overflow:\s*hidden/.test(metaRule) && /text-overflow:\s*ellipsis/.test(metaRule)
    && /white-space:\s*nowrap/.test(metaRule), metaRule.replace(/\s+/g, ' '));
  const mainRule = (mpMeWxss.match(/\.myfile-main\s*\{[^}]*\}/) || [''])[0];
  check('小程序文件主列 min-width:0 + overflow:hidden（flex 子项可收缩）',
    /min-width:\s*0/.test(mainRule) && /overflow:\s*hidden/.test(mainRule), mainRule.replace(/\s+/g, ' '));

  const orderSheet = (mpIndexWxss.match(/\.device-order-sheet\s*\{[^}]*\}/) || [''])[0];
  check('小程序「接单设备显示顺序」弹窗同样加宽到 660rpx + 窄屏安全',
    /width:\s*660rpx/.test(orderSheet) && /max-width:\s*calc\(100vw\s*-\s*48rpx\)/.test(orderSheet),
    orderSheet.replace(/\s+/g, ' '));

  // 其他弹窗宽度**不变**：这是用户明确要求（只加宽这两个）
  check('小程序其他弹窗宽度未动（.modal-sheet 仍 540rpx）',
    /\.modal-sheet\s*\{[^}]*width:\s*540rpx/.test(mpAppWxss));

  const appMyFiles = (appCss.match(/\.myfiles-sheet\s*\{[^}]*\}/) || [''])[0];
  check('APP「我的文件」弹窗加宽到 79.2cqw（= 660rpx，原 64.8cqw）+ 窄屏 92cqw',
    /width:\s*79\.2cqw/.test(appMyFiles) && /max-width:\s*92cqw/.test(appMyFiles),
    appMyFiles.replace(/\s+/g, ' '));
  const appOrderSheet = (appCss.match(/\.device-order-sheet\s*\{[^}]*\}/) || [''])[0];
  check('APP「接单设备显示顺序」弹窗加宽到 79.2cqw + 窄屏 92cqw',
    /width:\s*79\.2cqw/.test(appOrderSheet) && /max-width:\s*92cqw/.test(appOrderSheet),
    appOrderSheet.replace(/\s+/g, ' '));
  const appListRule = (appCss.match(/\.myfiles-list\s*\{[^}]*\}/) || [''])[0];
  check('APP 文件列表同样显式 width:100%',
    /width:\s*100%/.test(appListRule) && /box-sizing:\s*border-box/.test(appListRule));
  check('APP 其他弹窗宽度未动（.modal-sheet 仍 64.8cqw）',
    /\.modal-sheet\s*\{[^}]*width:\s*64\.8cqw/.test(appCss));

  // 「我的文件」弹窗里曾有一个游离的字面 \n 文本节点（会渲染成一行 "\n"）
  const literalBackslashN = String.fromCharCode(92) + 'n';
  check('小程序 me.wxml 不再有游离的 "\n" 文本节点',
    read(F.mpMeWxml).indexOf(literalBackslashN) < 0);
}

/* ══════════════════════════════════════════════════════════════
   二、⑤ 新状态 timeout（超时）：四处映射 + 两端配色
   ══════════════════════════════════════════════════════════════ */
section('二、timeout（超时）状态映射与配色');
{
  const mapHasTimeout = (src, re) => re.test(src);
  check('小程序「我」页 statusMap 有 timeout: 超时',
    mapHasTimeout(read(F.mpMeJs), /timeout:\s*'超时'/));
  check('小程序「用户订单」页 orderStatusMap 有 timeout: 超时',
    mapHasTimeout(read(F.mpUserOrdersJs), /timeout:\s*'超时'/));
  check('小程序「授权用户」页 orderStatusMap 有 timeout: 超时',
    mapHasTimeout(read(F.mpAuthUsersJs), /timeout:\s*'超时'/));
  check('小程序「最近订单」页 statusMap 有 timeout: 超时',
    mapHasTimeout(read(F.mpRoJs), /timeout:\s*'超时'/));
  check('APP ORDER_STATUS_MAP 有 timeout: 超时',
    mapHasTimeout(read(F.appJs), /timeout:\s*'超时'/));

  const mpMeWxss = read(F.mpMeWxss);
  check('小程序 me.wxss 有 .status-timeout（琥珀 #EF6C00，区别于 failed 的红）',
    /\.status-timeout\s*\{[^}]*#EF6C00/.test(mpMeWxss));
  check('小程序 me.wxss 的 .status-failed 仍是红色（两色不撞）',
    /\.status-failed\s*\{[^}]*#C62828/.test(mpMeWxss));
  check('小程序最近订单页自带 .status-timeout + 深色覆盖',
    /\.status-timeout\s*\{[^}]*#EF6C00/.test(read(F.mpRoWxss))
    && /\.theme-dark \.status-timeout\s*\{/.test(read(F.mpRoWxss)));

  const appCss = read(F.appCss);
  check('APP styles.css 有 .status-timeout（#EF6C00）',
    /\.status-timeout\s*\{[^}]*#EF6C00/.test(appCss));
  check('APP styles.css 有 .theme-dark .status-timeout',
    /\.theme-dark \.status-timeout\s*\{/.test(appCss));
}

/* ══════════════════════════════════════════════════════════════
   三、② 攒批：随时可切 + 三态读期望值 + 待应用角标
   ══════════════════════════════════════════════════════════════ */
section('三、攒批随时可切（三态 + 待应用）');
{
  const mpIndexJs = read(F.mpIndexJs);
  const mpIndexWxml = read(F.mpIndexWxml);
  const appJs = read(F.appJs);

  check('小程序 index.js 有 _normalizeDeviceStack（stack_desired → stack_view，三态含 null）',
    /_normalizeDeviceStack\s*\(/.test(mpIndexJs)
    && /stack_desired === true \|\| d\.stack_desired === false|hasDesired/.test(mpIndexJs));
  check('小程序 index.js 不再用 stack_can_toggle 做前端拦截（注释提及不算）',
    !/if\s*\([^)]*stack_can_toggle/.test(mpIndexJs));
  check('小程序 WXML 设备条三态读 stack_view（true / false / else 需人工翻纸）',
    /item\.stack_view === true/.test(mpIndexWxml) && /item\.stack_view === false/.test(mpIndexWxml));
  check('小程序 WXML 有「⏳ 待应用」独立角标，且与三态胶囊并存',
    /device-pending-badge/.test(mpIndexWxml) && /item\.stack_pending/.test(mpIndexWxml));
  check('小程序 WXML 开关按钮只按 stackTogglingId 灰显（在线忙不再灰死）',
    /stackTogglingId === item\.client_id \? 'device-stack-btn-disabled'/.test(mpIndexWxml));

  check('APP app.js 有 normalizeDeviceStack / stackDesiredOf',
    /function normalizeDeviceStack/.test(appJs) && /function stackDesiredOf/.test(appJs));
  check('APP app.js 不再用 stack_can_toggle 做前端拦截（注释提及不算）',
    !/if\s*\([^)]*stack_can_toggle/.test(appJs));
  check('APP stackBadgeHtml 三态读期望值 + 追加待应用角标',
    /stackDesiredOf\(d\)/.test(appJs)
    && /device-pending-badge">⏳ 待应用/.test(appJs));
  check('APP 待应用角标有独立样式（不再是整体替换胶囊）',
    /\.device-pending-badge\s*\{/.test(read(F.appCss)));
}

/* ══════════════════════════════════════════════════════════════
   四、③ 小程序「最近订单」独立页（真跑 js）
   ══════════════════════════════════════════════════════════════ */
function makeRectQuery(rects) {
  const queue = rects.slice();
  return () => {
    const q = {
      select: () => q,
      boundingClientRect: () => q,
      exec: (cb) => { const r = queue.length ? queue.shift() : null; if (cb) cb(r ? [r] : []); },
    };
    return q;
  };
}
function makeCtx(component, extra) {
  const ctx = Object.assign({
    data: JSON.parse(JSON.stringify(component.data || {})),
    createSelectorQuery: makeRectQuery([]), setData: null, _loading: false,
  }, extra || {});
  ctx.setData = (patch, cb) => { Object.assign(ctx.data, patch); if (cb) cb(); };
  Object.keys(component.methods || {}).forEach((k) => { ctx[k] = component.methods[k].bind(ctx); });
  return ctx;
}

/* utils/price_copy.js（2026-11-09 从 me.js 抽出的「复制价格/详细价格」文案唯一实现）：
   真实加载；它的 './pages' 依赖接成与各沙箱同口径的页数 shim。 */
const PRICE_COPY_MOD = (() => {
  const m = { exports: {} };
  new Function('module', 'exports', 'require', read(path.join(MP, 'utils', 'price_copy.js')))(m, m.exports, (dep) => {
    if (dep === './pages') return { countPagesInRange: (range, count) => (range ? 0 : (Number(count) || 0)) };
    throw new Error('utils/price_copy.js 意外 require 了 ' + dep);
  });
  return m.exports;
})();

function loadRecentOrdersPage({ role = 'admin', token = 'tk' } = {}) {
  const captured = { component: null, requests: [], clipboard: [], toasts: [], modals: [], storage: {} };
  captured.storage.userRole = role;
  captured.storage.token = token;
  const sandbox = {
    console, JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, isFinite, parseInt, parseFloat,
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
    require: (dep) => {
      if (dep.indexOf('utils/config') >= 0) return { CONFIG: { BASE_URL: 'https://mp.invalid' } };
      if (dep.indexOf('utils/request') >= 0) {
        return {
          request: (opts) => {
            captured.requests.push(opts);
            if (captured.respond) captured.respond(opts);
          },
          createPollScheduler: () => ({ start() {}, stop() {} }),
        };
      }
      if (dep.indexOf('utils/price_copy') >= 0) return PRICE_COPY_MOD;
      throw new Error('shim 未提供的 require: ' + dep);
    },
    wx: {
      getStorageSync: (k) => (k in captured.storage ? captured.storage[k] : ''),
      setStorageSync: (k, v) => { captured.storage[k] = v; },
      removeStorageSync: (k) => { delete captured.storage[k]; },
      getWindowInfo: () => ({ windowWidth: 375, windowHeight: 800 }),
      showToast: (o) => captured.toasts.push(o && o.title),
      showModal: (o) => captured.modals.push(o),
      setClipboardData: (o) => { captured.clipboard.push(o && o.data); if (o && o.success) o.success({}); },
      nextTick: (fn) => fn && fn(),
    },
    getApp: () => ({ globalData: { isDarkMode: false } }),
    Component: (cfg) => { captured.component = cfg; },
  };
  sandbox.global = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(read(F.mpRoJs), sandbox, { filename: 'recent-orders.js' });
  if (!captured.component) throw new Error('recent-orders.js 未暴露 Component');
  return captured;
}

section('四、最近订单独立页（分页 / 总数 / 行渲染）');
{
  // 入口：从「我」页跳到独立页，且「我」页不再内嵌列表
  const meWxml = read(F.mpMeWxml);
  const meJs = read(F.mpMeJs);
  check('「我」页只剩入口卡（catchtap="onOpenRecentOrders" + 标题「查看最近订单」）',
    /catchtap="onOpenRecentOrders"/.test(meWxml) && meWxml.indexOf('查看最近订单') > 0);
  check('「我」页不再内嵌订单行（recent-order-row / price-tap-r- 均已移除）',
    meWxml.indexOf('recent-order-row') < 0 && meWxml.indexOf('price-tap-r-') < 0);
  check('入口走 _navigateWithAnimation 跳 /pages/recent-orders/recent-orders（返回「我」页只播淡入）',
    /_navigateWithAnimation\(\s*'\/pages\/recent-orders\/recent-orders'\s*\)/.test(meJs));
  check('app.json 注册了新页面', read(F.mpAppJson).indexOf('pages/recent-orders/recent-orders') > 0);

  // 管理员进入 → 立刻按 page=1 / per_page=20 拉数据
  const p1 = loadRecentOrdersPage({ role: 'admin' });
  // 2026-11-03 #1：进页面先读「最近 N 条」设置，再按 page/per_page 拉列表；
  // 这里让 N=45 = 全站总量，后面的分页断言（3 页、[1,2,3]）仍成立。
  p1.respond = (opts) => {
    if (opts.url.indexOf('/api/admin/recent_orders_limit') >= 0) {
      return opts.success({ statusCode: 200, data: { success: true, limit: 45 } });
    }
    opts.success({
      statusCode: 200,
      data: {
        success: true, total: 45, page: 1, per_page: 20, total_pages: 3,
        orders: [
          { order_id: 'o1', order_number: 'HN20261102001', owner: '张三', owner_masked: '张*', created_at: '2026-11-02 10:05:31', file_count: 3, page_count: 5, total_price: 12.3, status: 'sent', source: 'wechat' },
          { order_id: 'o2', order_number: 'HN20261102002', owner: '李四', owner_masked: '李*', created_at: '2026-11-02 09:00:00', file_count: 1, page_count: 1, total_price: 0.3, status: 'timeout', status_reason: '打印机 30 分钟未取单' },
        ],
      },
    });
  };
  let ctx = makeCtx(p1.component);
  ctx.attached ? ctx.attached() : p1.component.lifetimes.attached.call(ctx);
  check('管理员进入先读设置 N（/api/admin/recent_orders_limit，带 Authorization）',
    p1.requests.length === 2
    && p1.requests[0].url === 'https://mp.invalid/api/admin/recent_orders_limit'
    && /^Bearer /.test(p1.requests[0].header.Authorization),
    p1.requests.length ? p1.requests[0].url : 'no request');
  check('再拉 /api/admin/recent_orders（带 Authorization）',
    p1.requests.length === 2
    && p1.requests[1].url === 'https://mp.invalid/api/admin/recent_orders'
    && /^Bearer /.test(p1.requests[1].header.Authorization));
  check('列表请求必带 page=1 + per_page=20（后端 C5：不带 page 是旧结构）',
    p1.requests.length === 2 && p1.requests[1].data.page === 1 && p1.requests[1].data.per_page === 20,
    JSON.stringify(p1.requests.length ? p1.requests[1].data : null));
  check('total=45 / perPage=20 ⇒ 3 页，总数 45',
    ctx.data.totalPages === 3 && ctx.data.totalOrders === 45,
    `pages=${ctx.data.totalPages} total=${ctx.data.totalOrders}`);
  check('行装饰：金额两位小数、状态中文、超时行原因可点',
    ctx.data.orders[0].priceDisplay === '12.30'
    && ctx.data.orders[0].statusText === '已完成'
    && ctx.data.orders[0].statusReasonTappable === false
    && ctx.data.orders[1].statusText === '超时'
    && ctx.data.orders[1].statusReasonTappable === true,
    JSON.stringify(ctx.data.orders.map((o) => [o.statusText, o.statusReasonTappable])));
  check('行时间只留 MM-DD HH:MM',
    ctx.data.orders[0].timeShort === '11-02 10:05', ctx.data.orders[0].timeShort);
  check('页码数组为 [1,2,3]', JSON.stringify(ctx.data.pageNumbers) === JSON.stringify([1, 2, 3]),
    JSON.stringify(ctx.data.pageNumbers));

  // 翻页：下一页 → page=2 且带上 per_page
  p1.respond = (opts) => opts.success({ statusCode: 200, data: { success: true, total: 45, orders: [] } });
  ctx.onNextPage();
  check('下一页 → currentPage=2 并重新请求 page=2',
    ctx.data.currentPage === 2 && p1.requests.length === 3 && p1.requests[2].data.page === 2,
    `page=${ctx.data.currentPage} reqs=${p1.requests.length}`);

  // 每页条数：记忆到 storage；非法值忽略；同值只收起
  p1.respond = null;
  ctx.onSelectPageSize({ currentTarget: { dataset: { size: 50 } } });
  check('选 50 条/页 → perPage=50 + 回第 1 页 + 记进本地存储',
    ctx.data.perPage === 50 && ctx.data.currentPage === 1 && p1.storage.recentOrdersPerPage === 50,
    `perPage=${ctx.data.perPage} stored=${p1.storage.recentOrdersPerPage}`);
  const reqsBefore = p1.requests.length;
  ctx.onSelectPageSize({ currentTarget: { dataset: { size: 33 } } });
  check('非白名单条数（33）被忽略，不发请求',
    ctx.data.perPage === 50 && p1.requests.length === reqsBefore, `perPage=${ctx.data.perPage}`);

  // 页码折叠：10 页、当前 5 → 1 … 4 5 6 … 10
  ctx.setData({ totalPages: 10, currentPage: 5 });
  check('页数多时折叠为 1 … 4 5 6 … 10',
    JSON.stringify(ctx.getPageNumbers()) === JSON.stringify([1, '...', 4, 5, 6, '...', 10]),
    JSON.stringify(ctx.getPageNumbers()));

  // 非管理员：不发请求、给「仅管理员可查看」空态
  const p2 = loadRecentOrdersPage({ role: 'user' });
  const ctx2 = makeCtx(p2.component);
  p2.component.lifetimes.attached.call(ctx2);
  check('非管理员进入：不发请求 + noPermission 空态',
    p2.requests.length === 0 && ctx2.data.noPermission === true && ctx2.data.loading === false);

  // 旧版后端（不带 total）：退化成单页，不出现 NaN 页码
  const p3 = loadRecentOrdersPage({ role: 'super_admin' });
  p3.respond = (opts) => opts.success({ statusCode: 200, data: { success: true, orders: [{ order_id: 'x', status: 'sent' }] } });
  const ctx3 = makeCtx(p3.component);
  p3.component.lifetimes.attached.call(ctx3);
  check('后端没给 total 时退化成单页（totalPages=1，页码无 NaN）',
    ctx3.data.totalPages === 1 && JSON.stringify(ctx3.data.pageNumbers) === JSON.stringify([1]),
    `pages=${ctx3.data.totalPages} nums=${JSON.stringify(ctx3.data.pageNumbers)}`);

  // 金额复制：与本地工具同格式（em dash）
  const p4 = loadRecentOrdersPage({ role: 'admin' });
  p4.respond = (opts) => opts.success({
    statusCode: 200,
    data: { success: true, total: 1, orders: [{ order_id: 'o9', order_number: 'HN-0007', total_price: 3.5, status: 'sent' }] },
  });
  const ctx4 = makeCtx(p4.component);
  p4.component.lifetimes.attached.call(ctx4);
  ctx4.onPriceCopy({ currentTarget: { dataset: { id: 'o9' } } });
  check('最近订单行复制价格 = `HN-0007 — ¥3.50`（本地工具格式）',
    p4.clipboard[0] === 'HN-0007 — ¥3.50' && p4.toasts[0] === '已复制价格',
    JSON.stringify(p4.clipboard));
}

/* ══════════════════════════════════════════════════════════════
   五、④ 异常状态原因气泡（两端文本口径 + 真跑小程序气泡）
   ══════════════════════════════════════════════════════════════ */
const AVATAR_CROP_MOD = (() => {
  const p = path.join(MP, 'utils', 'avatar_crop.js');
  const m = { exports: {} };
  new Function('module', 'exports', 'require', read(p))(m, m.exports, (dep) => {
    throw new Error('真实模块 utils/avatar_crop.js 意外 require 了 ' + dep);
  });
  return m.exports;
})();

function loadMpMeJs() {
  const captured = { component: null, clipboard: [], toasts: [] };
  const sandbox = {
    console, JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map, parseInt, parseFloat, isNaN,
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
    require: (dep) => {
      if (dep.indexOf('utils/config') >= 0) return { CONFIG: { BASE_URL: 'https://mp.invalid' } };
      if (dep.indexOf('utils/pages') >= 0) return { countPagesInRange: (range, count) => (range ? 0 : (Number(count) || 0)) };
      if (dep.indexOf('utils/avatar_crop') >= 0) return AVATAR_CROP_MOD;
      if (dep.indexOf('utils/request') >= 0) {
        return {
          request: (opts) => {
            if (opts && typeof opts.url === 'string' && opts.url.indexOf('/api/pricing') >= 0) {
              opts.success({ statusCode: 200, data: { success: true, pricing: { simplex_price: 0.3, duplex_price: 0.4 } } });
            }
          },
          createPollScheduler: () => ({ start() {}, stop() {} }),
        };
      }
      if (dep.indexOf('utils/price_copy') >= 0) return PRICE_COPY_MOD;
      throw new Error('shim 未提供的 require: ' + dep);
    },
    wx: {
      getStorageSync: () => '', setStorageSync: () => {}, removeStorageSync: () => {},
      getWindowInfo: () => ({ windowWidth: 375, windowHeight: 800 }),
      getSystemInfoSync: () => ({ windowWidth: 375, windowHeight: 800, platform: 'devtools' }),
      showToast: (o) => captured.toasts.push(o && o.title),
      showModal: () => {},
      setClipboardData: (o) => { captured.clipboard.push(o && o.data); if (o && o.success) o.success({}); },
      createSelectorQuery: () => { const q = { select: () => q, boundingClientRect: () => q, exec: () => {} }; return q; },
      nextTick: (fn) => fn && fn(),
    },
    getApp: () => ({ globalData: { isDarkMode: false, themeMode: 'light' } }),
    Component: (cfg) => { captured.component = cfg; },
  };
  sandbox.global = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(read(F.mpMeJs), sandbox, { filename: 'me.js' });
  return captured;
}

function loadAppMeJs() {
  const sandbox = {
    console, JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map, parseInt, parseFloat, isNaN,
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
    window: { addEventListener: () => {}, innerWidth: 375, innerHeight: 800 },
    document: { addEventListener: () => {}, getElementById: () => null, querySelectorAll: () => [], createElement: () => null },
    navigator: {},
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    escHtml: (s) => String(s == null ? '' : s),
    esc: (s) => String(s == null ? '' : s),
    showToast: () => {},
    copyText: () => {},
    api: () => Promise.resolve({ status: 200, data: { success: true } }),
    state: { token: 'tk', role: 'admin', openid: 'dev_test' },
    meState: { orders: [], recentOrders: [], recentOrderRows: [], userOrdersView: { orders: [] } },
    ORDER_STATUS_MAP: { timeout: '超时', failed: '失败', canceled: '已取消', rejected: '被打回', abandoned: '放弃打印' },
    orderStatusClass: () => '',
    createPollScheduler: () => ({ start() {}, stop() {} }),
    loadMeTab: () => {},
    measureAll: () => {},
  };
  sandbox.global = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(read(F.appMeJs), sandbox, { filename: 'www/me.js' });
  return sandbox;
}

section('五、异常状态原因气泡（文本 + 复制 + 定位）');
{
  const mp = loadMpMeJs();
  if (!mp.component) throw new Error('小程序 me.js 未暴露 Component');
  check('小程序新增原因气泡方法齐全',
    ['_statusReasonText', 'onStatusReasonTap', 'onCloseStatusPop', 'onCopyStatusReason']
      .every((k) => typeof mp.component.methods[k] === 'function'));

  const statusMap = mp.component.data.statusMap || {};
  check('statusMap 含 timeout', statusMap.timeout === '超时', JSON.stringify(statusMap.timeout));

  const mk = (over) => Object.assign({
    id: 7, order_number: 'HN20261102-0007', status: 'failed', total_price: 3.8,
    status_reason: '', manual_status_by: '', manual_status_at: '',
  }, over || {});

  let ctx = makeCtx(mp.component);
  check('人工订正优先：`{标记人}在{时间}被标记为{状态中文名}`',
    ctx._statusReasonText(mk({ manual_status_by: '王老师', manual_status_at: '2026-11-02T10:05:31', status: 'timeout' }))
    === '王老师在2026-11-02 10:05:31被标记为超时',
    ctx._statusReasonText(mk({ manual_status_by: '王老师', manual_status_at: '2026-11-02T10:05:31', status: 'timeout' })));
  check('没有人工记录时用后端 status_reason',
    ctx._statusReasonText(mk({ status_reason: '  打印机缺纸，已通知管理员  ' })) === '打印机缺纸，已通知管理员');
  check('两者都空 → 通用文案（气泡不会空白）',
    ctx._statusReasonText(mk({})) === '该订单未提供具体原因');

  // 点状态 → 气泡弹在状态上方；再点同一处收起
  const statusRect = { left: 300, right: 360, top: 500, width: 60, height: 22, bottom: 522 };
  ctx = makeCtx(mp.component, { createSelectorQuery: makeRectQuery([statusRect, { top: statusRect.top - 120, height: 108 }]) });
  ctx.data.orders = [mk({ status_reason: '打印机缺纸' })];
  ctx.data.showPricePop = true;
  ctx.onStatusReasonTap({ currentTarget: { dataset: { id: 7 } } });
  check('点异常状态 → 打开原因气泡并带上原因文本',
    ctx.data.showStatusPop === true && ctx.data.statusPopText === '打印机缺纸',
    String(ctx.data.statusPopText));
  check('同一时刻只留一个气泡（价格气泡被收起）', ctx.data.showPricePop === false);
  check('气泡锚在状态文字上方（bottom = 视口高 - 文字顶边 + 12）',
    Math.abs(ctx.data.statusPopBottom - (800 - statusRect.top + 12)) < 0.01 && ctx.data.statusPopBelow === false,
    `bottom=${ctx.data.statusPopBottom}`);
  check('水平夹在视口内、箭头不越界',
    ctx.data.statusPopLeft >= 8
    && ctx.data.statusPopLeft + ctx.data.statusPopWidth <= 375 - 8 + 0.01
    && ctx.data.statusPopCaretLeft >= 18
    && ctx.data.statusPopCaretLeft <= ctx.data.statusPopWidth - 18,
    `left=${ctx.data.statusPopLeft} w=${ctx.data.statusPopWidth} caret=${ctx.data.statusPopCaretLeft}`);

  mp.clipboard.length = 0; mp.toasts.length = 0;
  ctx.onCopyStatusReason();
  check('「复制」按钮把原因原文写进剪贴板',
    mp.clipboard[0] === '打印机缺纸' && mp.toasts[0] === '已复制原因',
    JSON.stringify([mp.clipboard[0], mp.toasts[0]]));

  ctx.onStatusReasonTap({ currentTarget: { dataset: { id: 7 } } });
  check('再点同一处 → 收起', ctx.data.showStatusPop === false);
  ctx.data.showStatusPop = true;
  ctx.onCloseStatusPop();
  check('「关闭」/ 点气泡外 → 收起', ctx.data.showStatusPop === false);

  // APP 端纯函数同口径
  const g = loadAppMeJs();
  const appText = (o) => vm.runInContext('orderStatusReasonText', g)(o);
  check('APP orderStatusReasonText 与小程序同口径（人工订正优先）',
    appText({ status: 'timeout', manual_status_by: '王老师', manual_status_at: '2026-11-02 10:05:31' })
    === '王老师在2026-11-02 10:05:31被标记为超时',
    appText({ status: 'timeout', manual_status_by: '王老师', manual_status_at: '2026-11-02 10:05:31' }));
  check('APP 回退 status_reason / 通用文案',
    appText({ status: 'failed', status_reason: '设备重启' }) === '设备重启'
    && appText({ status: 'failed' }) === '该订单未提供具体原因');
  const isReason = vm.runInContext('isReasonStatus', g);
  check('APP isReasonStatus 覆盖终态非 sent（含 timeout），不含 queued/sent',
    isReason('timeout') && isReason('failed') && isReason('canceled') && isReason('rejected') && isReason('abandoned')
    && !isReason('sent') && !isReason('queued') && !isReason('printing'));
}

/* ══════════════════════════════════════════════════════════════
   六、气泡结构与定位（page-root 外 + fixed，两端）
   ══════════════════════════════════════════════════════════════ */
section('六、气泡结构 / fixed 定位（不被 overflow 裁剪）');
{
  const mpWxml = read(F.mpMeWxml);
  // 2026-11-09：价格气泡样式抽到 utils/price_pop.wxss（两个页面 @import）——读「页面 + 共用样式」
  const mpWxss = read(F.mpMeWxss) + '\n' + read(path.join(MP, 'utils', 'price_pop.wxss'));
  const mpRoWxml = read(F.mpRoWxml);
  const mpRoWxss = read(F.mpRoWxss);

  const statusPopIdx = mpWxml.indexOf('showStatusPop');
  const modalStartIdx = mpWxml.indexOf('<!-- 昵称选择弹窗 -->');
  const myFilesIdx2 = mpWxml.indexOf('我的文件（查看保留期 + 删除');
  check('小程序原因气泡挂在 page-root 外（modals 区、我的文件弹窗之前）',
    statusPopIdx > modalStartIdx && modalStartIdx > 0 && statusPopIdx < myFilesIdx2,
    `pop=${statusPopIdx} modal=${modalStartIdx} myfiles=${myFilesIdx2}`);
  check('小程序气泡：mask 点外部关闭 + 气泡自身 catchtap="noop"',
    /class="price-pop-mask[\s\S]{0,200}catchtap="onCloseStatusPop"/.test(mpWxml)
    && /class="price-pop status-pop[\s\S]{0,300}catchtap="noop"/.test(mpWxml));
  check('小程序气泡按钮是「复制」「关闭」',
    /catchtap="onCopyStatusReason"[^>]*>复制</.test(mpWxml)
    && /catchtap="onCloseStatusPop"[^>]*>关闭</.test(mpWxml));
  check('小程序气泡复用 .price-pop（position:fixed）外壳，且原因区限高可滚',
    /\.price-pop\s*\{[^}]*position:\s*fixed/.test(mpWxss)
    && /\.status-pop-text-wrap\s*\{[^}]*max-height/.test(mpWxss));
  check('小程序气泡文字允许长串折行（word-break:break-all）',
    /\.status-pop-text\s*\{[^}]*word-break:\s*break-all/.test(mpWxss));
  check('小程序状态文字有下划线虚线的可点提示', /\.status-reason-tap\s*\{[^}]*underline/.test(mpWxss));
  check('最近订单页原因气泡：mask / 气泡都 fixed（同一套逃逸手法）',
    /\.ro-pop-mask\s*\{[^}]*position:\s*fixed/.test(mpRoWxss)
    && /\.ro-pop\s*\{[^}]*position:\s*fixed/.test(mpRoWxss)
    && /class="ro-pop-mask/.test(mpRoWxml));

  const appMeJs = read(F.appMeJs);
  check('APP 原因气泡 append 到 document.body（不在 .scroll-content 变换层内）',
    /openStatusReasonPop[\s\S]{0,1200}document\.body\.appendChild/.test(appMeJs));
  check('APP 气泡按钮「复制」「关闭」齐全',
    /data-status-act="copy"[\s\S]{0,200}>复制</.test(appMeJs)
    && /data-status-act="close"[\s\S]{0,200}>关闭</.test(appMeJs));
  const appCss = read(F.appCss);
  check('APP .status-pop-mask / .status-pop 都是 fixed',
    /\.status-pop-mask\s*\{[^}]*position:\s*fixed/.test(appCss)
    && /\.status-pop\s*\{[^}]*position:\s*fixed/.test(appCss));
  check('APP 订单卡状态文字异常态可点（data-status-reason）',
    /data-status-reason="\$\{o\.id\}"/.test(appMeJs) || /data-status-reason/.test(appMeJs));
  check('APP 回归按钮顺序：原因点击挡在卡片折叠之前',
    appMeJs.indexOf('data-status-reason') < appMeJs.indexOf("closest('[data-order-id]')")
    || /closest\('\[data-status-reason\]'\)[\s\S]{0,300}return;/.test(appMeJs));
}

console.log('\n' + (failures === 0 ? '✅ 全部通过' : `❌ ${failures} 项失败`));
process.exit(failures === 0 ? 0 : 1);
