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
   三、行为：抽取真实函数在桩环境里跑
   ============================================================ */
function extractOrdersBlock() {
  const start = meJs.indexOf('function _recentOrderTime(s) {');
  const end = meJs.indexOf('// 详细价格文本：逐文件');
  if (start < 0 || end < 0 || end <= start) return null;
  return meJs.slice(start, end);
}
function extractFn(src, marker) {
  const start = src.indexOf(marker);
  if (start < 0) return '';
  const end = src.indexOf('\n}', start);
  if (end < 0) return '';
  return src.slice(start, end + 2);
}

function loadOrdersModule() {
  const block = extractOrdersBlock();
  if (!block) return null;
  const statusMapSrc = (appJs.match(/const ORDER_STATUS_MAP = \{[\s\S]*?\};/) || [''])[0];
  const statusCls = extractFn(meJs, 'function orderStatusClass(status)');
  const prelude = `
    const state = { role: 'admin', isSuperAdmin: false };
    const meState = {
      orders: [], recentOrders: [], recentOrdersLimit: 10, recentOrdersLoading: false,
      recentOrdersLimitEditing: false, recentOrdersLimitDraft: 10, recentOrdersLimitSaving: false,
      userOrdersView: { orders: [] },
    };
    let apiCalls = [];
    let nextResponse = { data: { success: true, limit: 10, orders: [] } };
    let measureCalls = 0;
    const toasts = [];
    const els = {};
    function mkEl(id) {
      const cls = {};
      return {
        id: id, textContent: '', innerHTML: '', style: { display: '' }, _id: id, _cls: cls,
        classList: {
          toggle(name, on) { cls[name] = !!on; },
          add(name) { cls[name] = true; },
          remove(name) { delete cls[name]; },
          contains(name) { return !!cls[name]; },
        },
      };
    }
    function getEl(id) { if (!els[id]) els[id] = mkEl(id); return els[id]; }
    const document = { getElementById: getEl };
    function measureAll() { measureCalls++; }
    function showToast(msg, ms) { toasts.push({ msg: msg, ms: ms }); }
    function escHtml(s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }
    async function api(path, opts) { apiCalls.push({ path: path, opts: opts }); return nextResponse; }
    ${statusMapSrc}
    ${statusCls}
  `;
  const harness = `
    globalThis.__H = {
      setRole: (r) => { state.role = r; },
      setSuper: (v) => { state.isSuperAdmin = !!v; },
      setResponse: (r) => { nextResponse = r; },
      reset: () => { apiCalls.length = 0; measureCalls = 0; toasts.length = 0; },
      calls: () => apiCalls,
      toasts: () => toasts,
      els: els,
      meState: meState,
      loadRecentOrders: loadRecentOrders,
      renderRecentOrders: renderRecentOrders,
      recentOrderRowHTML: recentOrderRowHTML,
      _recentOrderTime: _recentOrderTime,
      _priceOrderById: _priceOrderById,
      _priceOrderTotal: _priceOrderTotal,
      renderRecentOrderLimitEditor: renderRecentOrderLimitEditor,
      openRecentOrdersLimitEditor: openRecentOrdersLimitEditor,
      stepRecentOrdersLimit: stepRecentOrdersLimit,
      saveRecentOrdersLimit: saveRecentOrdersLimit,
      _clampRecentOrdersLimit: _clampRecentOrdersLimit,
    };
  `;
  const sandbox = {
    console, Math, JSON, Date, Object, Array, String, Number, Boolean, isFinite, isNaN,
    parseInt, parseFloat, Promise, setTimeout, clearTimeout,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(prelude + '\n' + block + '\n' + harness, sandbox, { filename: 'recentOrders(extracted)' });
  return { H: vm.runInContext('__H', sandbox), block };
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

(async () => {
  const loaded = loadOrdersModule();
  if (!loaded) {
    check('可从 me.js 抽取近 N 条订单区块', false);
    console.log('\n❌ 1 项失败');
    process.exit(1);
    return;
  }
  const H = loaded.H;

  section('三、可见性与请求时机');
  H.setRole('user');
  H.reset();
  H.setResponse({ data: { success: true, limit: 10, orders: [ORDER] } });
  let ok = await H.loadRecentOrders();
  check('① 普通用户：不发请求（role !== admin 直接 return）', H.calls().length === 0 && ok === false);

  H.setRole('guest');
  await H.loadRecentOrders();
  check('② 游客：同样不发请求', H.calls().length === 0);

  H.setRole('admin');
  H.reset();
  await H.loadRecentOrders();
  check('③ 管理员：恰好发一次 GET /api/admin/recent_orders',
        H.calls().length === 1 && H.calls()[0].path === '/api/admin/recent_orders',
        JSON.stringify(H.calls().map(c => c.path)));

  section('四、渲染内容');
  const box = H.els.recentOrderList;
  const title = H.els.recentOrdersTitle;
  const count = H.els.recentOrderCount;
  check('① 标题显示后端给的 N（10）', title.textContent === '近 10 条订单', title.textContent);
  check('② 数量徽标显示条数并恢复显示',
        count.textContent === '1' && count.style.display === '', `${count.textContent}/${count.style.display}`);
  check('③ 渲染出 1 行订单', (box.innerHTML.match(/recent-order-row/g) || []).length === 1);

  check('④ 归属人显示脱敏名 owner_masked', box.innerHTML.includes('>张*</text>'),
        (box.innerHTML.match(/recent-order-owner[^>]*>[^<]*/) || [''])[0]);
  check('⑤ 行内 data-owner-full 带真名（点击才显示，不一开始就露出）',
        /data-owner-full="&lt;b&gt;张三&lt;\/b&gt;"/.test(box.innerHTML),
        (box.innerHTML.match(/data-owner-full="[^"]*"/) || [''])[0]);
  check('⑥ 真名被 escHtml 转义（不会当 HTML 执行）', !box.innerHTML.includes('<b>张三</b>'));
  check('⑦ 时间格式化成 MM-DD HH:MM', box.innerHTML.includes('11-02 10:05'));
  check('⑧ 状态用中文标签 + 既有 status class',
        /recent-order-status status-sent">已完成</.test(box.innerHTML),
        (box.innerHTML.match(/recent-order-status[^>]*>[^<]*/) || [''])[0]);
  check('⑨ 价格两位小数 + ¥', box.innerHTML.includes('¥12.30'),
        (box.innerHTML.match(/¥[\d.]+/) || [''])[0]);
  check('⑩ 价格行带 data-recent-price（用于复用复制气泡）',
        /data-recent-price="ord-1"/.test(box.innerHTML));
  check('⑪ 价格旁有既有 📋 图标', box.innerHTML.includes('price-copy-icon'));
  check('⑫ meta 拼出 文件数/页数/管理员自打/来源',
        box.innerHTML.includes('3 个文件 · 5 页 · 管理员自打 · 云端'),
        (box.innerHTML.match(/recent-order-meta">([^<]*)/) || [''])[1]);
  check('⑬ 数据登记进 meState（供 _priceOrderById 回查）',
        H.meState.recentOrders.length === 1 && String(H.meState.recentOrders[0].id) === 'ord-1');

  section('五、复制价格复用既有气泡');
  const hit = H._priceOrderById('ord-1');
  check('① _priceOrderById 能在近 N 条订单池里命中（openPricePop 才不 return）', !!hit,
        JSON.stringify(hit && { id: hit.id, n: hit.order_number }));
  check('② 命中项带 order_number / total_price（气泡两种文案都靠它）',
        hit && hit.order_number === 'HN20261102001' && H._priceOrderTotal(hit) === 12.3);
  check('③ 气泡依赖的 copyText 文案未被改写（已复制价格 / 已复制详细价格）',
        meJs.includes("'已复制价格'") && meJs.includes("'已复制详细价格'"));

  section('六、边界：limit 缺省 / 空列表 / 失败保留 / 并发去重');
  H.reset();
  H.setResponse({ data: { success: true, orders: [ORDER] } });
  await H.loadRecentOrders();
  check('① 响应没给 limit 时兜底 10', H.els.recentOrdersTitle.textContent === '近 10 条订单',
        H.els.recentOrdersTitle.textContent);

  H.reset();
  H.setResponse({ data: { success: true, limit: 25, orders: [] } });
  await H.loadRecentOrders();
  check('② N 跟随后端（25）', H.els.recentOrdersTitle.textContent === '近 25 条订单');
  check('③ 空列表显示「暂无订单」并隐藏数量徽标',
        H.els.recentOrderList.innerHTML.includes('暂无订单') && H.els.recentOrderCount.style.display === 'none',
        H.els.recentOrderList.innerHTML.trim());

  H.reset();
  H.setResponse({ data: { success: true, limit: 7, orders: [ORDER] } });
  await H.loadRecentOrders();
  const beforeTitle = H.els.recentOrdersTitle.textContent;
  const beforeHtml = H.els.recentOrderList.innerHTML;
  H.reset();
  H.setResponse({ data: { success: false } });
  const bad = await H.loadRecentOrders();
  check('④ 失败（success:false）返回 false 且保留旧数据/旧标题',
        bad === false && H.els.recentOrdersTitle.textContent === beforeTitle &&
        H.els.recentOrderList.innerHTML === beforeHtml,
        'title=' + H.els.recentOrdersTitle.textContent);

  H.reset();
  H.setResponse({ data: { success: true, limit: 10, orders: [ORDER] } });
  const p1 = H.loadRecentOrders();
  const p2 = H.loadRecentOrders();
  await Promise.all([p1, p2]);
  check('⑤ 并发调用只发一次请求（loading 去重）', H.calls().length === 1, 'calls=' + H.calls().length);
  check('⑥ 请求结束后 loading 复位', H.meState.recentOrdersLoading === false);

  H.reset();
  H.setResponse({ data: { success: true, limit: 10, orders: [{ ...ORDER, created_at: '难以解析' }] } });
  await H.loadRecentOrders();
  check('⑦ 时间解析不了时原样显示（不显示空白）',
        H.els.recentOrderList.innerHTML.includes('难以解析'));

  /* ============================================================
     七、超管设置 N（用户原话「近{超管设置的数字（默认10）}条订单」）
     ============================================================ */
  section('七、超管设置 N：静态接线与契约');
  check('① index.html 有「设置」入口 #recentOrderLimitEntry（默认隐藏）',
        /id="recentOrderLimitEntry"[^>]*style="display:\s*none"/.test(html) ||
        /style="display:\s*none"[^>]*id="recentOrderLimitEntry"/.test(html));
  const blockHtml = (html.match(/<view class="license-section admin-collapsible" id="recentOrdersBlock">[\s\S]*?<view class="recent-orders-card"/) || [''])[0];
  check('② 入口在「近 N 条订单」卡片内、标题栏里',
        blockHtml.includes('recentOrderLimitEntry') && blockHtml.indexOf('recentOrderLimitEntry') > blockHtml.indexOf('section-title'));
  check('③ 步进器 #recentOrderLimitEditor 与 ± 步进按钮在同一卡片内',
        blockHtml.includes('recentOrderLimitEditor') &&
        (blockHtml.match(/data-limit-step="-1"/g) || []).length === 1 &&
        (blockHtml.match(/data-limit-step="1"/g) || []).length === 1);
  check('④ 步进器有保存/取消与数值位', blockHtml.includes('recentOrderLimitSave') &&
        blockHtml.includes('recentOrderLimitCancel') && blockHtml.includes('recentOrderLimitValue'));

  /* 只截「超管设置 N」自己那段源码做静态判据，避免全文其它地方的同名写法干扰 */
  const limitSrc = (meJs.match(/\/\* ---------- 超管设置 N[\s\S]*?\/\* ================= 合计价格复制/) || [''])[0];
  /* 去掉注释再判：注释里正写着「不能写 role === 'super_admin'」这种反例说明 */
  const limitCode = limitSrc.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  check('⑤ 超管判据用 state.isSuperAdmin（不是 role === "super_admin"）',
        /state\.isSuperAdmin/.test(limitCode) && !/role\s*===?\s*'super_admin'/.test(limitCode),
        (limitCode.match(/state\.isSuperAdmin/g) || []).length + ' 处');
  check('⑥ POST 路径 /api/admin/recent_orders_limit + method POST + body 字段名 limit',
        limitSrc.includes("api('/api/admin/recent_orders_limit'") &&
        /method:\s*'POST'/.test(limitSrc) &&
        /JSON\.stringify\(\{\s*limit:\s*limit\s*\}\)/.test(limitSrc));
  check('⑦ 1..100 的边界写死在前端（越界先夹紧再发）',
        /RECENT_ORDER_LIMIT_MIN\s*=\s*1/.test(limitSrc) && /RECENT_ORDER_LIMIT_MAX\s*=\s*100/.test(limitSrc));
  check('⑧ 不弹遮罩：这段不用 openModal / modal-mask，只用 showToast',
        !/openModal|modal-mask|price-pop-mask/.test(limitSrc));
  check('⑨ 入口/步进器的事件绑定在 setupMeButtons 里',
        /getElementById\('recentOrderLimitEntry'\)[\s\S]{0,200}?addEventListener\('click'/.test(meJs) &&
        /getElementById\('recentOrderLimitEditor'\)[\s\S]{0,300}?addEventListener\('click'/.test(meJs));
  check('⑩ styles.css 有入口/步进器样式', /\.recent-order-limit-entry\s*\{/.test(fs.readFileSync(path.join(APP_WWW, 'styles.css'), 'utf8')));

  section('八、超管设置 N：行为');
  H.reset();
  H.setRole('admin');
  H.setSuper(false);
  H.meState.recentOrdersLimit = 10;
  H.renderRecentOrderLimitEditor();
  check('① 非超管：入口 display:none', H.els.recentOrderLimitEntry.style.display === 'none',
        'display=' + H.els.recentOrderLimitEntry.style.display);
  H.openRecentOrdersLimitEditor(true);
  check('② 非超管：点了也打不开步进器', H.meState.recentOrdersLimitEditing === false &&
        H.els.recentOrderLimitEditor.style.display === 'none');
  H.reset();
  const denied = await H.saveRecentOrdersLimit();
  check('③ 非超管：不会发 POST（后端 403 也不会被触发）', denied === false && H.calls().length === 0);

  H.setSuper(true);
  H.renderRecentOrderLimitEditor();
  check('④ 超管：入口显示', H.els.recentOrderLimitEntry.style.display === '');
  H.openRecentOrdersLimitEditor(true);
  check('⑤ 超管：点开步进器，数值初值 = 当前 N',
        H.els.recentOrderLimitEditor.style.display === '' &&
        H.els.recentOrderLimitValue.textContent === '10',
        H.els.recentOrderLimitValue.textContent);

  H.stepRecentOrdersLimit(1);
  check('⑥ + 一次 = 11', H.els.recentOrderLimitValue.textContent === '11');
  H.stepRecentOrdersLimit(-1);
  check('⑦ − 一次回到 10', H.els.recentOrderLimitValue.textContent === '10');
  H.meState.recentOrdersLimitDraft = 100;
  H.stepRecentOrdersLimit(1);
  check('⑧ 上界 100 夹紧', H.meState.recentOrdersLimitDraft === 100);
  H.meState.recentOrdersLimitDraft = 1;
  H.stepRecentOrdersLimit(-1);
  check('⑨ 下界 1 夹紧', H.meState.recentOrdersLimitDraft === 1);
  check('⑩ 非法值兜底为 1', H._clampRecentOrdersLimit('abc') === 1);
  check('⑪ 小数四舍五入', H._clampRecentOrdersLimit('7.6') === 8);

  /* 保存成功：POST 用返回 limit 刷新标题、收起编辑器、再按新 N 重拉一次列表 */
  H.reset();
  H.meState.recentOrdersLimitDraft = 12;
  H.meState.recentOrdersLimitEditing = true;
  H.setResponse({ data: { success: true, limit: 12, range: [1, 100], message: '已更新为最近 12 条' } });
  const okSave = await H.saveRecentOrdersLimit();
  const post = H.calls()[0];
  check('⑫ 保存发 POST 且 body 为 {"limit":12}',
        !!post && post.path === '/api/admin/recent_orders_limit' && post.opts.method === 'POST' &&
        post.opts.body === JSON.stringify({ limit: 12 }),
        post ? post.opts.body : 'no call');
  check('⑬ 保存成功后编辑器收起', H.meState.recentOrdersLimitEditing === false &&
        H.els.recentOrderLimitEditor.style.display === 'none');
  check('⑭ 保存成功用返回的 limit 立刻刷新标题', H.els.recentOrdersTitle.textContent === '近 12 条订单',
        H.els.recentOrdersTitle.textContent);
  check('⑮ 保存成功 toast 用后端 message',
        H.toasts().some(t => /已更新为最近 12 条/.test(t.msg)), H.toasts().map(t => t.msg).join(' | '));
  check('⑯ 保存成功后再按新 N 拉一次列表（POST + GET 共 2 次）',
        H.calls().length === 2 && H.calls()[1].path === '/api/admin/recent_orders',
        H.calls().map(c => c.path).join(','));
  check('⑰ 返回值 true', okSave === true);

  /* 保存失败（403）：保持旧值、不关编辑器、不重拉 */
  H.reset();
  H.meState.recentOrdersLimit = 12;
  H.meState.recentOrdersLimitDraft = 30;
  H.meState.recentOrdersLimitEditing = true;
  H.setResponse({ status: 403, data: { success: false, message: '仅超级管理员可修改' } });
  const badSave = await H.saveRecentOrdersLimit();
  check('⑱ 保存失败返回 false', badSave === false);
  check('⑲ 保存失败保持旧 N（不进 meState.recentOrdersLimit）', H.meState.recentOrdersLimit === 12);
  check('⑳ 保存失败不重拉列表（只发了一次 POST）',
        H.calls().length === 1, H.calls().map(c => c.path).join(','));
  check('㉑ 保存失败编辑器仍开着（方便改个数重试）', H.meState.recentOrdersLimitEditing === true);
  check('㉒ 保存失败只轻提示，用后端 message',
        H.toasts().some(t => /仅超级管理员可修改/.test(t.msg)), H.toasts().map(t => t.msg).join(' | '));

  /* 身份降级：入口/编辑器立刻收回 */
  H.setSuper(false);
  H.renderRecentOrderLimitEditor();
  check('㉓ 掉超管身份后入口收回且编辑器强制关闭',
        H.els.recentOrderLimitEntry.style.display === 'none' &&
        H.meState.recentOrdersLimitEditing === false &&
        H.els.recentOrderLimitEditor.style.display === 'none');

  /* ===== 九、生成器模板同步（防止重跑 wxss2web.mjs 静默抹掉卡片样式）===== */
  section('九、生成器模板同步');
  const GEN = path.join(REPO, 'mobile_apps', 'android_app', 'tools', 'wxss2web.mjs');
  const genSrc = fs.readFileSync(GEN, 'utf8');
  check('㉔ 生成器模板里有 .recent-orders-card（重跑生成器不会抹掉卡片样式）',
        genSrc.includes('.recent-orders-card {'));
  check('㉕ 生成器模板里有 .recent-order-limit-entry（超管设置入口样式）',
        genSrc.includes('.recent-order-limit-entry {'));
  check('㉖ 生成器模板里有 .device-state-stack-pending（离线待应用徽标）',
        genSrc.includes('.device-state-stack-pending {'));
  check('㉗ 生成器模板里有 .recent-order-row / -meta / -price / -empty 全套',
        ['.recent-order-row {', '.recent-order-meta {', '.recent-order-price {', '.recent-order-empty {']
          .every((s) => genSrc.includes(s)));
  // 模板是 JS 模板字符串：注释里出现反引号会提前闭合模板，生成器直接语法错
  const genWheelSeg = genSrc.slice(genSrc.indexOf('.time-wheel-row {'), genSrc.indexOf('.wheel-indicator {'));
  check('㉘ 生成器模板滚轮段内没有反引号（模板字符串安全）',
        genWheelSeg.length > 0 && genWheelSeg.indexOf('`') === -1);

  console.log('\n' + (failures === 0 ? '✅ 全部通过' : `❌ ${failures} 项失败`));
  process.exit(failures === 0 ? 0 : 1);
})().catch((e) => {
  console.error('运行异常：', e);
  process.exit(1);
});
