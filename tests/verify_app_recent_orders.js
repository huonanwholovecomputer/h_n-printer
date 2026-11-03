/* eslint-disable */
/**
 * APP「近 N 条订单」卡片 · 验收（2026-11 需求①）
 *
 * 用户要求：「我的」页给管理员加一张「近 N 条订单」卡片（N 由超管设置，默认 10），
 * 每行显示归属人（脱敏 owner_masked，点一下看 owner），价格可复制（复用 APP 已有的复制价格气泡）。
 *
 * 后端契约：
 *   GET /api/admin/recent_orders        → { success:true, limit:10, orders:[{order_id, order_number,
 *        owner, owner_masked, created_at, file_count, page_count, total_price, status, source, is_admin_print}] }
 *   GET /api/admin/recent_orders_limit  → { success:true, limit:10, range:[1,100] }   ← 超管设置面板用，本卡片只读 limit
 *
 * 前端口径：
 *   · 可见性只认 `state.role === 'admin'`（后端 compute_role 把超管折叠成 admin，超管另有 isSuperAdmin）；
 *   · 非管理员**不渲染也不发请求**；
 *   · 进「我的」页拉一次（loadIfStale 30s 新鲜度），**不轮询**；
 *   · 复制价格复用 me.js 的 openPricePop（同一气泡、同一 copyText 文案），不新写剪贴板。
 *
 * 用法：node tests/verify_app_recent_orders.js   （exit 0 = 全绿）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..');
const APP_WWW = path.join(REPO, 'mobile_apps', 'android_app', 'www');
const APP_ME = path.join(APP_WWW, 'me.js');
const APP_HTML = path.join(APP_WWW, 'index.html');
const APP_JS = path.join(APP_WWW, 'app.js');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}
function section(title) { console.log('\n================ ' + title + ' ================'); }

const meJs = fs.readFileSync(APP_ME, 'utf8');
const html = fs.readFileSync(APP_HTML, 'utf8');
const appJs = fs.readFileSync(APP_JS, 'utf8');

/* ============================================================
   一、index.html 接线
   ============================================================ */
section('一、index.html 接线');
const adminOpen = html.indexOf('<view id="adminSections">');
const blockIdx = html.indexOf('id="recentOrdersBlock"');
const keysIdx = html.indexOf('id="adminBlockKeys"');
check('① 卡片在 #adminSections 内、许可密钥块之前',
      adminOpen > 0 && blockIdx > adminOpen && keysIdx > blockIdx,
      `adminSections=${adminOpen} block=${blockIdx} keys=${keysIdx}`);
check('② 卡片带 admin-collapsible（默认收起，复用既有折叠/展开动画）',
      /class="[^"]*admin-collapsible[^"]*" id="recentOrdersBlock"/.test(html) ||
      /id="recentOrdersBlock"[^>]*class="[^"]*admin-collapsible/.test(html));
check('③ 标题默认「近 10 条订单」', /id="recentOrdersTitle"[^>]*>近 10 条订单</.test(html));
check('④ 数量徽标 #recentOrderCount 默认隐藏', /id="recentOrderCount"[^>]*style="display:\s*none"/.test(html));
check('⑤ 列表容器 #recentOrderList 存在', /id="recentOrderList"/.test(html));

/* ============================================================
   二、me.js 接线（可见性 / 拉取时机 / 事件委托）
   ============================================================ */
section('二、me.js 接线');
const loadTimesBlock = (meJs.match(/const _adminLoadTimes = \{[\s\S]*?\};/) || [''])[0];
check('① _adminLoadTimes 里有 recentOrders 键（漏了 loadIfStale 会永远不触发）',
      /recentOrders\s*:/.test(loadTimesBlock), loadTimesBlock.replace(/\s+/g, ' '));
check('② 进「我的」页经 loadIfStale 拉一次', /loadIfStale\('recentOrders',\s*loadRecentOrders\)/.test(meJs));
check('③ 30s 新鲜度、无定时器（不轮询）',
      !/setInterval\([^)]*[Rr]ecentOrders/.test(meJs) && !/_recentOrdersPollTimer/.test(meJs));
check('④ 展开/收起走既有 setBlock', /setBlock\('recentOrdersBlock',\s*isAdmin\)/.test(meJs));

const delegIdx = meJs.indexOf("getElementById('recentOrderList')");
const delegSrc = delegIdx > 0 ? meJs.slice(delegIdx, delegIdx + 1200) : '';
check('⑤ #recentOrderList 有 click 委托', delegSrc.includes("addEventListener('click'"));
check('⑥ 价格点击复用 openPricePop（→ 气泡 → copyText，不新写剪贴板）',
      /openPricePop\(priceEl,\s*priceEl\.dataset\.recentPrice\)/.test(delegSrc),
      (delegSrc.match(/openPricePop\([^)]*\)/) || [''])[0]);
check('⑦ 该委托里没有新的 navigator.clipboard',
      !/navigator\.clipboard/.test(delegSrc));
check('⑧ 归属人点击在脱敏名/真名之间就地切换',
      /ownerEl\.textContent\s*=\s*\(ownerEl\.textContent === full\)\s*\?\s*masked\s*:\s*full/.test(delegSrc),
      (delegSrc.match(/ownerEl\.textContent[^;]*;/) || [''])[0]);
check('⑨ 价格分支先于归属人分支（价格在第二行，避免误触）',
      delegSrc.indexOf('data-recent-price') >= 0 &&
      delegSrc.indexOf('data-recent-price') < delegSrc.indexOf("closest('[data-recent-owner]')"));

/* ============================================================
   三、me.js：内嵌列表 → 入口卡（#3 改造后）
   ============================================================ */
section('三、me.js 近 N 条订单改成入口卡');
const rosJs = fs.readFileSync(path.join(APP_WWW, 'recent-orders.js'), 'utf8');
const css = fs.readFileSync(path.join(APP_WWW, 'styles.css'), 'utf8');
const renderSrc = meJs.slice(meJs.indexOf('function renderRecentOrders'), meJs.indexOf('function _clampRecentOrdersLimit'));
check('① renderRecentOrders 渲染入口卡并调 openRecentOrdersView',
      /recent-orders-entry[\s\S]{0,800}openRecentOrdersView\(\)/.test(renderSrc),
      (renderSrc.match(/openRecentOrdersView[^\n]*/) || [''])[0]);
check('② 入口卡标题「查看最近订单」+ 说明带动态 N',
      renderSrc.indexOf('查看最近订单') >= 0 && /全部管理员的最近/.test(renderSrc));
check('③ 行渲染器 recentOrderRowHTML 已移除（列表搬到独立页）',
      !/function recentOrderRowHTML/.test(meJs));
check('④ 标题与数量徽标仍按接口 limit 更新',
      /recentOrdersTitle/.test(renderSrc) && /recentOrderCount/.test(renderSrc));
check('⑤ 入口卡不再挂行内价格/归属人交互',
      renderSrc.indexOf('data-recent-price') < 0 && renderSrc.indexOf('data-recent-owner') < 0);

/* ============================================================
   四、最近订单子视图接线（index.html / app.js / styles.css）
   ============================================================ */
section('四、子视图接线');
const viewStart = html.indexOf('id="view-recent-orders"');
const viewEnd = html.indexOf('<!-- ===== Tab 栏');
const viewHtml = viewStart > 0 && viewEnd > viewStart ? html.slice(viewStart, viewEnd) : '';
check('① index.html 有 #view-recent-orders 子视图根', viewStart > 0);
check('② 复用同一套滚动引擎骨架（scroller + scroll-content）',
      viewHtml.indexOf('id="scroller-recent-orders"') > 0 &&
      viewHtml.indexOf('id="scrollContentRecentOrders"') > 0);
check('③ 容器 id 齐全（汇总/列表/顶底分页/空态/下拉）',
      ['roNoPermission', 'roSection', 'roSummary', 'roSummaryText', 'roPager', 'roPageNumbers',
       'roPrev', 'roNext', 'roList', 'roPagerBottom', 'roPageNumbersBottom', 'roPrevBottom',
       'roNextBottom', 'roEndHint', 'roRefresh', 'roPageSizeSelector', 'roPageSizeDropdown',
       'roPageSizeText', 'roPageSizeArrow'].every((id) => viewHtml.indexOf('id="' + id + '"') >= 0),
      viewHtml ? 'len=' + viewHtml.length : 'no view');
check('④ 条数下拉 10/20/50/100 四个选项',
      ['10', '20', '50', '100'].every((n) => viewHtml.indexOf('data-size="' + n + '"') >= 0));
check('⑤ recent-orders.js 已挂载且在 me.js 之后',
      html.indexOf('src="recent-orders.js"') > html.indexOf('src="me.js"') &&
      html.indexOf('src="recent-orders.js"') > 0);
check('⑥ app.js 注册滚动引擎 recentOrders',
      /\[\s*'recentOrders'\s*,\s*'scroller-recent-orders'\s*,\s*'scrollContentRecentOrders'\s*\]/.test(appJs));
check('⑦ 启动时调用 initRecentOrdersView()', /initRecentOrdersView\(\)/.test(appJs));
check('⑧ 返回键/切 tab 收起原因气泡与条数下拉',
      /closeStatusReasonPop\(\)/.test(appJs) && /closeRoPageSizePickerIfOpen\(\)/.test(appJs));
check('⑨ styles.css 有入口卡与行样式',
      ['.recent-orders-entry {', '.ro-row {', '.ro-price-tap {', '.ro-owner {']
        .every((s) => css.indexOf(s) >= 0));
check('⑩ styles.css 有 .status-timeout（橙，区别于 failed 红）',
      /\.status-timeout\s*\{[^}]*#EF6C00/.test(css));
check('⑪ styles.css 原因气泡 fixed + 出变换层',
      /\.status-pop-mask\s*\{[^}]*position:\s*fixed/.test(css) && /\.status-pop\s*\{[^}]*position:\s*fixed/.test(css));

/* ============================================================
   五、行为：把真实 recent-orders.js 放进桩环境跑
   ============================================================ */
function loadRecentOrdersViewModule() {
  const statusMapSrc = (appJs.match(/const ORDER_STATUS_MAP = \{[\s\S]*?\};/) || [''])[0];
  const statusCls = (() => {
    const s = meJs.indexOf('function orderStatusClass(status)');
    const e = meJs.indexOf('\n}', s);
    return s >= 0 && e > s ? meJs.slice(s, e + 2) : '';
  })();
  const prelude = `
    const state = { role: 'admin' };
    const meState = { orders: [], recentOrders: [], recentOrderRows: [], recentOrdersLimit: 10 };
    const els = {};
    function mkEl(id) {
      const cls = {};
      return {
        id: id, innerHTML: '', textContent: '', style: { display: '' }, dataset: {}, _cls: cls,
        classList: {
          toggle(name, on) { cls[name] = !!on; },
          add(name) { cls[name] = true; },
          remove(name) { delete cls[name]; },
          contains(name) { return !!cls[name]; },
        },
        addEventListener() {},
        closest() { return null; },
      };
    }
    const document = {
      getElementById(id) { if (!els[id]) els[id] = mkEl(id); return els[id]; },
      querySelectorAll() { return []; },
      addEventListener() {},
    };
    const _ls = {};
    const localStorage = {
      getItem(k) { return Object.prototype.hasOwnProperty.call(_ls, k) ? _ls[k] : null; },
      setItem(k, v) { _ls[k] = String(v); },
    };
    let apiCalls = [];
    let nextResponse = { status: 200, data: { success: true, total: 0, orders: [] } };
    let measureCalls = 0;
    const views = [];
    const copied = [];
    const reasons = [];
    const ddOpened = [];
    let closedStatus = 0;
    let closedPrice = 0;
    async function api(path, opts) { apiCalls.push({ path: path, opts: opts }); return nextResponse; }
    function showView(id, title) { views.push({ id: id, title: title }); }
    function measureAll() { measureCalls++; }
    function copyText(text, msg) { copied.push({ text: text, msg: msg }); }
    function openStatusReasonPop(el, id) { reasons.push({ id: id }); }
    function closeStatusReasonPop() { closedStatus++; }
    function closePricePop() { closedPrice++; }
    function openDropdownFixed(dd, trigger) { ddOpened.push({ dd: dd, trigger: trigger }); }
    function escHtml(s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }
    function buildPriceSimpleText(o) { return (o && o.order_number ? o.order_number + ' — ' : '') + '¥' + Number(o.total_price || 0).toFixed(2); }
    function isReasonStatus(status) { return ['failed', 'timeout', 'canceled', 'rejected', 'abandoned'].indexOf(status) >= 0; }
    const scrollEngines = { recentOrders: { y: 300, maxY: 240, _scrolled: null, measure() {}, scrollTo(y, d) { this._scrolled = { y: y, d: d }; this.y = y; } } };
    ${statusMapSrc}
    ${statusCls}
  `;
  const harness = `
    globalThis.__H = {
      roState: roState,
      els: els,
      calls: () => apiCalls,
      copied: () => copied,
      reasons: () => reasons,
      views: () => views,
      ddOpened: () => ddOpened,
      closed: () => ({ status: closedStatus, price: closedPrice }),
      measureCalls: () => measureCalls,
      scrollEngine: () => scrollEngines.recentOrders,
      reset() { apiCalls = []; copied.length = 0; reasons.length = 0; views.length = 0; ddOpened.length = 0; measureCalls = 0; },
      setRole(r) { state.role = r; },
      setResponse(r) { nextResponse = r; },
      storage: _ls,
      openRecentOrdersView: openRecentOrdersView,
      loadRecentOrdersView: loadRecentOrdersView,
      roDecorate: roDecorate,
      roShortTime: roShortTime,
      roRowHTML: roRowHTML,
      renderRecentOrdersView: renderRecentOrdersView,
      roPageNumbersHTML: roPageNumbersHTML,
      selectRoPageSize: selectRoPageSize,
      changeRoPage: changeRoPage,
      roOwnerTap: roOwnerTap,
      roPriceTap: roPriceTap,
      roPerPageInitial: roPerPageInitial,
      toggleRoPageSizePicker: toggleRoPageSizePicker,
      closeRoPageSizePickerIfOpen: closeRoPageSizePickerIfOpen,
      PER_PAGE_KEY: RO_PER_PAGE_KEY,
    };
  `;
  const sandbox = {
    console, Math, JSON, Date, Object, Array, String, Number, Boolean, isFinite, isNaN,
    parseInt, parseFloat, Promise, setTimeout, clearTimeout,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(prelude + '\n' + rosJs + '\n' + harness, sandbox, { filename: 'recent-orders(web)' });
  return vm.runInContext('__H', sandbox);
}

const ORDER = {
  order_id: 'ord-1',
  order_number: 'HN20261102001',
  owner: '<b>张三</b>',
  owner_masked: '张*',
  created_at: '2026-11-02T10:05:31',
  file_count: 3,
  page_count: 5,
  total_price: 12.3,
  status: 'sent',
  source: 'cloud',
  is_admin_print: true,
};

const tick = () => new Promise((r) => setTimeout(r, 0));

(async () => {
  const H = loadRecentOrdersViewModule();

  section('五、可见性与请求契约');
  H.setRole('user');
  H.reset();
  H.openRecentOrdersView();
  check('① 非管理员：noPermission 空态，不发请求',
        H.roState.noPermission === true && H.calls().length === 0, 'calls=' + H.calls().length);
  check('② 非管理员：仍进入子视图（而不是停在「我」页）',
        H.views().length === 1 && H.views()[0].id === 'view-recent-orders',
        JSON.stringify(H.views()[0]));
  check('③ 空态节点显示、列表区隐藏',
        H.els.roNoPermission.style.display === '' && H.els.roSection.style.display === 'none');

  H.setRole('admin');
  H.setResponse({ status: 200, data: { success: true, total: 45, orders: [ORDER] } });
  H.reset();
  H.openRecentOrdersView();
  await tick();
  check('④ 管理员：进入即拉第 1 页（page/per_page 上屏）',
        H.calls().length === 1 && H.calls()[0].path === '/api/admin/recent_orders?page=1&per_page=20',
        JSON.stringify(H.calls().map((c) => c.path)));
  check('⑤ 标题传「最近订单」', H.views()[0].title === '最近订单');
  check('⑥ total=45 / perPage=20 → 3 页',
        H.roState.total === 45 && H.roState.totalPages === 3, H.roState.totalPages + '');
  check('⑦ 汇总文案「共 45 条 · 第 1/3 页」',
        H.els.roSummaryText.textContent === '共 45 条 · 第 1/3 页', H.els.roSummaryText.textContent);
  check('⑧ 行数据留在 roState.orders（价格/原因气泡反查）',
        H.roState.orders.length === 1, 'orders=' + H.roState.orders.length);

  section('六、渲染内容');
  const box = H.els.roList;
  check('① 渲染 1 行 .ro-row', (box.innerHTML.match(/ro-row"/g) || []).length >= 1);
  check('② 订单号上屏', box.innerHTML.indexOf('HN20261102001') >= 0);
  check('③ 归属人显示脱敏名', box.innerHTML.indexOf('👤 张*') >= 0,
        (box.innerHTML.match(/ro-owner[^>]*>[^<]*/) || [''])[0]);
  check('④ 真名进 data-owner-full 且被转义', /data-owner-full="&lt;b&gt;张三&lt;\/b&gt;"/.test(box.innerHTML),
        (box.innerHTML.match(/data-owner-full="[^"]*"/) || [''])[0]);
  check('⑤ 真名不会当 HTML 执行', box.innerHTML.indexOf('<b>张三</b>') < 0);
  check('⑥ 时间 MM-DD HH:MM', box.innerHTML.indexOf('11-02 10:05') >= 0);
  check('⑦ 状态中文 + status class', /order-status status-sent">已完成</.test(box.innerHTML),
        (box.innerHTML.match(/order-status[^>]*>[^<]*/) || [''])[0]);
  check('⑧ 金额两位小数', box.innerHTML.indexOf('¥12.30') >= 0);
  check('⑨ 金额行带 data-ro-price（点击复制）', /data-ro-price="ord-1"/.test(box.innerHTML));
  check('⑩ 文件数·页数 meta', box.innerHTML.indexOf('3 文件 · 5 页') >= 0);
  check('⑪ 管理员自打徽标', box.innerHTML.indexOf('ro-self-badge') >= 0);

  section('七、边界：空列表 / 失败 / 无 total');
  H.setResponse({ status: 200, data: { success: true, total: 0, orders: [] } });
  await H.loadRecentOrdersView();
  check('① 空列表显示空态文案，汇总隐藏',
        H.els.roList.innerHTML.indexOf('还没有订单') >= 0 && H.els.roSummary.style.display === 'none');
  check('② 空列表时 totalPages 兜底 1', H.roState.totalPages === 1);

  H.setResponse({ status: 200, data: { success: true, orders: [ORDER] } });
  await H.loadRecentOrdersView();
  check('③ 后端不给 total：退化为本页条数、单页',
        H.roState.total === 1 && H.roState.totalPages === 1);

  H.setResponse({ status: 500, data: { success: false, message: '仅管理员可见' } });
  await H.loadRecentOrdersView();
  check('④ 失败写内联提示（不弹遮罩）',
        H.els.roList.innerHTML.indexOf('仅管理员可见') >= 0, H.els.roList.innerHTML.slice(0, 80));

  section('八、分页交互');
  H.setResponse({ status: 200, data: { success: true, total: 45, orders: [ORDER] } });
  H.roState.perPage = 20;
  H.roState.page = 1;
  H.roState.totalPages = 3;
  H.reset();
  H.changeRoPage(3);
  await tick();
  check('① 跳第 3 页发 page=3',
        H.calls().length === 1 && H.calls()[0].path.indexOf('page=3') >= 0,
        JSON.stringify(H.calls().map((c) => c.path)));
  check('② 回顶（滚动引擎收到 scrollTo）', !!H.scrollEngine()._scrolled,
        JSON.stringify(H.scrollEngine()._scrolled));
  H.reset();
  H.changeRoPage(9);
  await tick();
  check('③ 越界页码不发请求', H.calls().length === 0);
  H.reset();
  H.changeRoPage(3);
  await tick();
  check('④ 点当前页不发请求（不重复拉）', H.calls().length === 0);

  H.reset();
  H.selectRoPageSize(50);
  await tick();
  check('⑤ 切条数：perPage=50、回到第 1 页',
        H.roState.perPage === 50 && H.roState.page === 1, H.roState.perPage + '/' + H.roState.page);
  check('⑥ 切条数写 localStorage（下次进入沿用）', H.storage[H.PER_PAGE_KEY] === '50',
        JSON.stringify(H.storage));
  check('⑦ 切条数后按新 per_page 重拉', H.calls().length === 1 && H.calls()[0].path.indexOf('per_page=50') >= 0,
        JSON.stringify(H.calls().map((c) => c.path)));
  H.reset();
  H.selectRoPageSize(33);
  await tick();
  check('⑧ 非法条数被忽略（白名单 10/20/50/100）', H.calls().length === 0 && H.roState.perPage === 50);

  section('九、页码算法');
  H.roState.totalPages = 3;
  H.roState.page = 2;
  const p3 = H.roPageNumbersHTML();
  check('① 3 页全列（1/2/3）',
        p3.indexOf('data-ro-page="1"') >= 0 && p3.indexOf('data-ro-page="2"') >= 0
        && p3.indexOf('data-ro-page="3"') >= 0);
  check('② 当前页高亮 page-num-active', /page-num page-num-active" data-ro-page="2"/.test(p3),
        (p3.match(/page-num-active[^>]*>[^<]*/) || [''])[0]);
  H.roState.totalPages = 10;
  H.roState.page = 5;
  const p10 = H.roPageNumbersHTML();
  check('③ 多页折叠成 1 … 4 5 6 … 10',
        p10.indexOf('data-ro-page="1"') >= 0 && p10.indexOf('data-ro-page="10"') >= 0
        && p10.indexOf('page-ellipsis') >= 0
        && p10.indexOf('data-ro-page="4"') >= 0 && p10.indexOf('data-ro-page="6"') >= 0,
        (p10.match(/data-ro-page="\d+"/g) || []).join(','));
  check('④ 折叠时不列 3/7', p10.indexOf('data-ro-page="3"') < 0 && p10.indexOf('data-ro-page="7"') < 0);

  section('十、行交互（价格复制 / 归属人 / 异常原因）');
  H.roState.orders = [H.roDecorate(Object.assign({}, ORDER, { status: 'failed', total_price: 3.5 }))];
  H.roState.page = 1;
  H.roState.totalPages = 1;
  H.renderRecentOrdersView();
  const failedHtml = H.els.roList.innerHTML;
  check('① 异常状态可点（status-reason-tap + data-ro-status）',
        failedHtml.indexOf('status-reason-tap') >= 0 && /data-ro-status="ord-1"/.test(failedHtml));
  check('② 异常状态带 ⓘ 提示', failedHtml.indexOf('ⓘ') >= 0);
  check('③ timeout 中文标签 = 超时',
        H.roRowHTML(Object.assign({}, ORDER, { status: 'timeout' })).indexOf('超时') >= 0);
  check('④ 正常状态不可点',
        H.roRowHTML(Object.assign({}, ORDER, { status: 'sent' })).indexOf('status-reason-tap') < 0);

  H.reset();
  H.roPriceTap('ord-1');
  check('⑤ 点金额复制 `订单号 — ¥金额`',
        H.copied().length === 1 && H.copied()[0].text === 'HN20261102001 — ¥3.50',
        JSON.stringify(H.copied()[0] || {}));
  check('⑥ 复制 toast「已复制价格」', (H.copied()[0] || {}).msg === '已复制价格');
  H.reset();
  H.roPriceTap('ord-404');
  check('⑦ 未知订单不复制（不误写剪贴板）', H.copied().length === 0);

  const ownerEl = { dataset: { ownerFull: '张三（真名）', ownerMasked: '张*' }, textContent: '👤 张*' };
  H.roOwnerTap(ownerEl);
  check('⑧ 点归属人切到真名', ownerEl.textContent === '👤 张三（真名）', ownerEl.textContent);
  H.roOwnerTap(ownerEl);
  check('⑨ 再点切回脱敏名', ownerEl.textContent === '👤 张*', ownerEl.textContent);

  section('十一、时间格式化');
  check('① ISO 时间 → MM-DD HH:MM', H.roShortTime('2026-11-02T10:05:31') === '11-02 10:05',
        H.roShortTime('2026-11-02T10:05:31'));
  check('② 空格分隔也认', H.roShortTime('2026-11-02 10:05') === '11-02 10:05');
  check('③ 解析不了原样返回', H.roShortTime('难以解析') === '难以解析');
  check('④ 空值给空串', H.roShortTime(null) === '');
  const dec = H.roDecorate({ order_id: 9, total_price: '4.5', status: 'queued' });
  check('⑤ roDecorate 补 id（供 _priceOrderById 反查）', dec.id === 9);
  check('⑥ 金额两位小数', dec.priceDisplay === '4.50', dec.priceDisplay);
  check('⑦ 无归属人显示 —', dec.ownerText === '—', dec.ownerText);
  check('⑧ meta 用 0 兜底', dec.statText === '0 文件 · 0 页', dec.statText);

  section('十二、条数下拉（fixed，逃出 transform 层）');
  H.roState.perPage = 20;
  H.roState.showPageSizePicker = false;
  H.reset();
  H.toggleRoPageSizePicker();
  check('① 打开下拉走 openDropdownFixed（不靠 absolute）', H.ddOpened().length === 1);
  check('② 状态置为展开、文案同步', H.roState.showPageSizePicker === true
        && H.els.roPageSizeText.textContent === '20条/页', H.els.roPageSizeText.textContent);
  H.closeRoPageSizePickerIfOpen();
  check('③ 退出时收起', H.roState.showPageSizePicker === false);

  /* ===== 十三、生成器模板同步（防止重跑 wxss2web.mjs 静默抹掉卡片样式）===== */
  section('十三、生成器模板同步');
  const GEN = path.join(REPO, 'mobile_apps', 'android_app', 'tools', 'wxss2web.mjs');
  const genSrc = fs.readFileSync(GEN, 'utf8');
  check('① 生成器模板里有 .recent-orders-card（重跑生成器不会抹掉卡片样式）',
        genSrc.includes('.recent-orders-card {'));
  check('② 生成器模板里有 .recent-order-limit-entry（超管设置入口样式）',
        genSrc.includes('.recent-order-limit-entry {'));
  check('③ 生成器模板里有 .device-state-stack-pending（离线待应用徽标）',
        genSrc.includes('.device-state-stack-pending {'));
  check('④ 生成器模板里有 .recent-order-row / -meta / -price / -empty 全套',
        ['.recent-order-row {', '.recent-order-meta {', '.recent-order-price {', '.recent-order-empty {']
          .every((s) => genSrc.includes(s)));
  const genWheelSeg = genSrc.slice(genSrc.indexOf('.time-wheel-row {'), genSrc.indexOf('.wheel-indicator {'));
  check('⑤ 生成器模板滚轮段内没有反引号（模板字符串安全）',
        genWheelSeg.length > 0 && genWheelSeg.indexOf('`') === -1);

  console.log('\n' + (failures === 0 ? '✅ 全部通过' : `❌ ${failures} 项失败`));
  process.exit(failures === 0 ? 0 : 1);
})().catch((e) => {
  console.error('运行异常：', e);
  process.exit(1);
});
