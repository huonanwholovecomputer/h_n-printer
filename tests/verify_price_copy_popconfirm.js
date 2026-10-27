/* eslint-disable */
/**
 * 「我 → 订单详情 → 合计价格」复制（Popconfirm 气泡）验收
 *
 * 需求（2026-10-26 用户）：「给这个价格加上一个复制功能，点击之后弹出一个 Popconfirm 气泡，
 * 两个按钮是『复制价格』和『复制详细价格』。」
 *
 * 口径（本脚本钉死）：
 *   ① 点击「¥xx.xx」→ 气泡出现在**价格上方**（上方放不下翻到下方、箭头朝上），
 *      再点一次价格收起；点气泡外（透明 mask）收起；复制成功后收起；
 *   ② 「复制价格」= 订单号 + 空格 + ¥金额，与首页提交成功弹窗里的「复制价格」同格式；
 *   ③ 「复制详细价格」= 逐文件（份 × 页 | 单/双面 | 页码范围 | 实收小计）+ 附加服务
 *      （加急 / 首页 / 派送，派送费 = round(文件小计 × 百分比 / 100, 2)，与后端
 *      submit_order 同口径）+ 合计；金额一律取订单**已定稿**的实收值，前端不重算单价；
 *   ④ 逐项相加 ≠ 订单实收时只写实收合计，**不编算式**；
 *   ⑤ 气泡在 page-root 外 + position:fixed —— 订单卡片 `.order-card-detail` 有
 *      overflow:hidden / max-height，气泡放在卡片里会被裁掉；
 *   ⑥ 两端（微信小程序 / Android APP）同款，且切 tab、订单刷新、卡片收起时气泡自动收起。
 *
 * 用法：node tests/verify_price_copy_popconfirm.js     （exit 0 = 全绿）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..');
const MP_ME_JS = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'me', 'me.js');
const MP_ME_WXML = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'me', 'me.wxml');
const MP_ME_WXSS = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'me', 'me.wxss');
const APP_ME_JS = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'me.js');
const APP_APP_JS = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'app.js');
const APP_CSS = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'styles.css');
const MP_ME_JSON = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'me', 'me.json');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }

/* ── 夹具：形状对齐 /api/orders（order_files 已带 effective_pages/sheets/total_*） ── */
function makeOrder(over) {
  return Object.assign({
    id: 3,
    order_number: 'HN20261025-0013',
    status: 'sent',
    source: 'wechat',
    total_price: 3.8,
    urgency: '低',
    urgency_price: 0,
    cover_page: 0,
    cover_page_price: 0,
    delivery_enabled: 0,
    delivery_location: '1号楼北楼',
    delivery_percentage: 0,
    files: [
      { id: 11, file_id: 'f11', file_name: '示波器的使用.pdf', original_name: '示波器的使用.pdf', copies: 2, page_count: 6, duplex: 'on', page_range: '', total_price: 2.4, pagesLine: '2 份 × 6 页', total_sheets: 6 },
      { id: 12, file_id: 'f12', file_name: '数据结构-第3次.docx', original_name: '数据结构-第3次.docx', copies: 2, page_count: 2, duplex: 'on', page_range: '', total_price: 0.8, pagesLine: '2 份 × 2 页', total_sheets: 2 },
      { id: 13, file_id: 'f13', file_name: '示波器数据表格.pdf', original_name: '示波器数据表格.pdf', copies: 2, page_count: 1, duplex: 'off', page_range: '', total_price: 0.6, pagesLine: '2 份 × 1 页', total_sheets: 2 },
    ],
  }, over || {});
}

/* ============================================================
   一、小程序 pages/me/me.js：气泡定位 + 两种复制的文本
   ============================================================ */
function loadMiniProgram() {
  const src = fs.readFileSync(MP_ME_JS, 'utf8');
  const captured = { component: null, toast: [], clipboard: [] };
  const sandbox = {
    console,
    JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map,
    parseInt, parseFloat, isNaN,
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    require: (dep) => {
      if (dep.indexOf('utils/config') >= 0) return { CONFIG: { BASE_URL: 'https://example.invalid' } };
      if (dep.indexOf('utils/pages') >= 0) return { countPagesInRange: () => 1 };
      if (dep.indexOf('utils/request') >= 0) {
        return { request: () => {}, createPollScheduler: () => ({ start() {}, stop() {} }) };
      }
      throw new Error('shim 未提供的 require: ' + dep);
    },
    wx: {
      getStorageSync: () => '',
      setStorageSync: () => {},
      removeStorageSync: () => {},
      getWindowInfo: () => ({ windowWidth: 375, windowHeight: 800, safeArea: { bottom: 800 } }),
      getSystemInfoSync: () => ({ windowWidth: 375, windowHeight: 800, platform: 'devtools' }),
      showToast: (o) => captured.toast.push(o && o.title),
      setClipboardData: (o) => {
        captured.clipboard.push(o && o.data);
        if (o && o.success) o.success({});
      },
      createSelectorQuery: () => {
        const q = { select: () => q, boundingClientRect: () => q, exec: () => {} };
        return q;
      },
      nextTick: (fn) => fn && fn(),
    },
    getApp: () => ({ globalData: { isDarkMode: false, themeMode: 'light' } }),
    Component: (cfg) => { captured.component = cfg; },
    Page: (cfg) => { captured.component = cfg; },
  };
  sandbox.global = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: 'me.js' });
  if (!captured.component || !captured.component.methods) {
    throw new Error('me.js 未暴露 Component.methods（夹具需同步更新）');
  }
  return { component: captured.component, calls: captured };
}

// 造一个「组件实例」：setData 同步合并、选择器按队列返回几何
function makeCtx(component, orders, rects) {
  const data = JSON.parse(JSON.stringify(component.data || {}));
  data.orders = orders;
  const ctx = { data, patches: [] };
  ctx.setData = (patch, cb) => {
    Object.assign(ctx.data, patch);
    ctx.patches.push(patch);
    if (cb) cb();
  };
  const queue = (rects || []).slice();
  ctx.createSelectorQuery = () => {
    const q = {
      select: () => q,
      boundingClientRect: () => q,
      exec: (cb) => {
        const r = queue.length ? queue.shift() : null;
        if (cb) cb([r, null]);
      },
    };
    return q;
  };
  Object.keys(component.methods).forEach((k) => { ctx[k] = component.methods[k].bind(ctx); });
  return ctx;
}

function runMiniProgram() {
  section('小程序 pages/me/me.js');
  const { component, calls } = loadMiniProgram();

  check('新增方法齐全（onPriceTap / 两个复制 / 关闭 / 找单 / 取整）',
        ['onPriceTap', 'onClosePricePop', 'onCopyOrderPrice', 'onCopyOrderDetailPrice',
         '_findOrderById', '_orderTotal', '_copyText'].every((k) => typeof component.methods[k] === 'function'));

  const order = makeOrder();
  const priceRect = { left: 232, right: 350, top: 620, width: 118, height: 24, bottom: 644 };

  // ① 点击价格 → 气泡出现在价格上方
  let ctx = makeCtx(component, [order], [priceRect, { top: priceRect.top - 110, height: 98 }]);
  ctx.onPriceTap({ currentTarget: { dataset: { id: order.id } } });
  check('① 点价格 → 气泡打开且记住订单', ctx.data.showPricePop === true
        && String(ctx.data.pricePopOrderId) === String(order.id));
  check('① 气泡锚在价格上方（bottom = 视口高 - 价格顶边 + 12）',
        Math.abs(ctx.data.pricePopBottom - (800 - priceRect.top + 12)) < 0.01 && ctx.data.pricePopBelow === false,
        `bottom=${ctx.data.pricePopBottom}`);
  check('① 气泡水平位置夹在视口内（右边对齐价格右边，左不越界）',
        ctx.data.pricePopLeft >= 8 && ctx.data.pricePopLeft + ctx.data.pricePopWidth <= 375 - 8 + 0.01,
        `left=${ctx.data.pricePopLeft} w=${ctx.data.pricePopWidth}`);
  check('① 箭头落在气泡内（不被裁到边上）',
        ctx.data.pricePopCaretLeft >= 18 && ctx.data.pricePopCaretLeft <= ctx.data.pricePopWidth - 18,
        `caret=${ctx.data.pricePopCaretLeft}`);

  // 再点一次同一订单 → 收起
  ctx.onPriceTap({ currentTarget: { dataset: { id: order.id } } });
  check('① 再点一次价格 → 收起', ctx.data.showPricePop === false);

  // ② 价格贴近屏幕顶部 → 翻到下方
  const topRect = { left: 232, right: 350, top: 40, width: 118, height: 24, bottom: 64 };
  ctx = makeCtx(component, [order], [topRect, { top: -26, height: 98 }]);
  ctx.onPriceTap({ currentTarget: { dataset: { id: order.id } } });
  check('② 上方放不下 → 气泡翻到价格下方（箭头朝上）',
        ctx.data.showPricePop === true && ctx.data.pricePopBelow === true
        && Math.abs(ctx.data.pricePopTop - (topRect.top + topRect.height + 12)) < 0.01,
        `below=${ctx.data.pricePopBelow} top=${ctx.data.pricePopTop}`);

  // ③ 复制价格
  calls.clipboard.length = 0; calls.toast.length = 0;
  ctx = makeCtx(component, [order]);
  ctx.onCopyOrderPrice({ currentTarget: { dataset: { id: order.id } } });
  check('③ 复制价格 = 订单号 + 空格 + ¥金额（与首页同格式）',
        calls.clipboard[0] === 'HN20261025-0013 ¥3.80', `got=${JSON.stringify(calls.clipboard[0])}`);
  check('③ 复制后气泡收起 + 成功 toast', ctx.data.showPricePop === false && calls.toast[0] === '已复制价格');

  // ④ 复制详细价格（无附加服务：逐文件 + 合计算式）
  calls.clipboard.length = 0; calls.toast.length = 0;
  ctx = makeCtx(component, [order]);
  ctx.onCopyOrderDetailPrice({ currentTarget: { dataset: { id: order.id } } });
  const detail = calls.clipboard[0] || '';
  check('④ 详细价格含标题/订单号/三个文件', detail.indexOf('计费明细') === 0
        && detail.indexOf('HN20261025-0013') > 0
        && detail.indexOf('示波器的使用.pdf') > 0
        && detail.indexOf('数据结构-第3次.docx') > 0
        && detail.indexOf('示波器数据表格.pdf') > 0);
  check('④ 每个文件带「份 × 页 | 单/双面 | 页码 | 实收小计」',
        detail.indexOf('2 份 × 6 页 | 双面 | 全部页 | ¥2.40') > 0
        && detail.indexOf('2 份 × 1 页 | 单面 | 全部页 | ¥0.60') > 0);
  check('④ 合计算式 = 2.40+0.80+0.60=￥3.80（金额取自订单实收值）',
        detail.indexOf('💰合计: 2.40+0.80+0.60=￥3.80') > 0, detail.split('\n').slice(-1)[0]);
  check('④ 详细价格复制后气泡收起 + toast', ctx.data.showPricePop === false
        && calls.toast[0] === '已复制详细价格');

  // ⑤ 附加服务（加急/首页/派送）：派送费取实收残差，与后端算式同值
  const withExtras = makeOrder({
    total_price: 4.68, urgency: '高', urgency_price: 0.3,
    cover_page: 2, cover_page_price: 0.1,
    delivery_enabled: 1, delivery_location: '1号楼北楼', delivery_percentage: 10,
  });
  calls.clipboard.length = 0;
  ctx = makeCtx(component, [withExtras]);
  ctx.onCopyOrderDetailPrice({ currentTarget: { dataset: { id: withExtras.id } } });
  const d2 = calls.clipboard[0] || '';
  // 后端算式：total = round(3.80 + 0.30 + 0.20 + round(3.80 × 10 / 100, 2), 2) = 4.68
  // 前端取残差 4.68 − 3.80 − 0.30 − 0.20 = 0.38（不重写取整口径，.5 边界不会与后端差 1 分）
  check('⑤ 派送费 = 实收残差（与后端 round(文件小计 × 百分比 / 100, 2) 同值）',
        d2.indexOf('派送：是 | 1号楼北楼 10% | ￥0.38') > 0, d2);
  check('⑤ 加急 / 首页分别按实收值列出', d2.indexOf('优先级：高 | ￥0.30') > 0
        && d2.indexOf('打印首页信息 | 0.20') > 0);
  check('⑤ 合计 = 2.40+0.80+0.60+0.30+0.20+0.38=￥4.68',
        d2.indexOf('💰合计: 2.40+0.80+0.60+0.30+0.20+0.38=￥4.68') > 0, d2.split('\n').slice(-1)[0]);

  // ⑥ 逐项相加 ≠ 订单实收（历史数据/人工调整）→ 只写实收合计，不编算式
  const mismatch = makeOrder({ total_price: 9.99 });
  calls.clipboard.length = 0;
  ctx = makeCtx(component, [mismatch]);
  ctx.onCopyOrderDetailPrice({ currentTarget: { dataset: { id: mismatch.id } } });
  const d3 = calls.clipboard[0] || '';
  check('⑥ 口径不一致时只给实收合计（不编算式）',
        d3.indexOf('💰合计: ￥9.99') > 0 && d3.indexOf('=￥9.99') < 0, d3.split('\n').slice(-1)[0]);

  // ⑥b 派送单被人工改过价 → 残差与「小计 × 百分比」差太多，派送行不写金额、合计仍按实收
  const oddDelivery = makeOrder({
    total_price: 9.99, delivery_enabled: 1, delivery_location: '1号楼北楼', delivery_percentage: 10,
  });
  calls.clipboard.length = 0;
  ctx = makeCtx(component, [oddDelivery]);
  ctx.onCopyOrderDetailPrice({ currentTarget: { dataset: { id: oddDelivery.id } } });
  const d4 = calls.clipboard[0] || '';
  check('⑥b 残差不可信时不把差额归因到派送（派送行无金额，合计=实收）',
        d4.indexOf('派送：是 | 1号楼北楼 10%\n') > 0
        && d4.indexOf('派送：是 | 1号楼北楼 10% | ￥') < 0
        && d4.indexOf('💰合计: ￥9.99') > 0, d4.split('\n').slice(-2).join(' / '));

  // ⑦ 订单不在当前页 → 不复制、不崩
  calls.clipboard.length = 0;
  ctx = makeCtx(component, [order]);
  ctx.onCopyOrderPrice({ currentTarget: { dataset: { id: 999 } } });
  check('⑦ 找不到订单 → 不写剪贴板且气泡收起',
        calls.clipboard.length === 0 && ctx.data.showPricePop === false);

  // ⑧ 点 mask 关闭
  ctx = makeCtx(component, [order]);
  ctx.data.showPricePop = true;
  ctx.onClosePricePop();
  check('⑧ 点气泡外（mask）→ 收起', ctx.data.showPricePop === false);
}

/* ============================================================
   二、小程序：WXML / WXSS 接线（气泡必须在 page-root 外 + fixed）
   ============================================================ */
function checkMiniProgramMarkup() {
  section('小程序 me.wxml / me.wxss 接线');
  const wxml = fs.readFileSync(MP_ME_WXML, 'utf8');
  const wxss = fs.readFileSync(MP_ME_WXSS, 'utf8');
  const js = fs.readFileSync(MP_ME_JS, 'utf8');

  check('价格可点：id 供测量 + data-id + catchtap（不冒泡收起卡片）',
        wxml.indexOf('id="price-tap-{{item.id}}"') > 0
        && /class="detail-price-tap"[\s\S]{0,200}catchtap="onPriceTap"/.test(wxml));
  check('价格旁有复制图标（可点提示）', wxml.indexOf('class="price-copy-icon"') > 0
        && /detail-price-value[\s\S]{0,200}price-copy-icon/.test(wxml));

  const popIdx = wxml.indexOf('class="price-pop-mask');
  const myFilesIdx = wxml.indexOf('我的文件（查看保留期 + 删除');
  const modalIdx = wxml.indexOf('<!-- 昵称选择弹窗 -->');
  check('气泡在 page-root 外（在 modals 区、我的文件弹窗之前）',
        popIdx > 0 && modalIdx > 0 && popIdx > modalIdx && popIdx < myFilesIdx,
        `pop=${popIdx} modal=${modalIdx} myfiles=${myFilesIdx}`);
  check('气泡是「点外部关闭」的 mask + 两个按钮（复制价格 / 复制详细价格）',
        /class="price-pop-mask[\s\S]{0,600}catchtap="onClosePricePop"/.test(wxml)
        && wxml.indexOf('catchtap="onCopyOrderPrice"') > 0
        && wxml.indexOf('catchtap="onCopyOrderDetailPrice"') > 0
        && wxml.indexOf('>复制价格<') > 0
        && wxml.indexOf('>复制详细价格<') > 0);
  check('气泡内点击不穿透（气泡自身 catchtap="noop"）',
        /class="price-pop \{\{pricePopBelow[\s\S]{0,400}catchtap="noop"/.test(wxml));

  check('WXSS：mask 与气泡都是 position:fixed（逃出 translateY 变换层 + 不被卡片裁剪）',
        /\.price-pop-mask\s*\{[^}]*position:\s*fixed/.test(wxss)
        && /\.price-pop\s*\{[^}]*position:\s*fixed/.test(wxss));
  check('WXSS：箭头默认朝下、翻到下方时朝上',
        /\.price-pop-above \.price-pop-caret\s*\{[^}]*bottom:/.test(wxss)
        && /\.price-pop-below \.price-pop-caret\s*\{[^}]*top:/.test(wxss));
  check('WXSS：深色模式有独立覆盖（theme-dark）',
        /\.theme-dark \.price-pop\s*\{/.test(wxss) && /\.theme-dark \.price-pop-btn\s*\{/.test(wxss));

  check('JS：卡片收起 / 订单刷新 / 切回本页 都收起气泡',
        /this\.setData\(\{ expandedOrders: expanded, showPricePop: false \}\)/.test(js)
        && /expandedOrders: this\.data\.expandedOrders, showPricePop: false/.test(js)
        && /expandedOrders: \{\},\s*\n\s*showPricePop: false/.test(js)
        && /if \(this\.data\.showPricePop\) this\.setData\(\{ showPricePop: false \}\)/.test(js));
  check('me.json 无需新增组件（页面只加原生 view）',
        fs.readFileSync(MP_ME_JSON, 'utf8').indexOf('price-pop') < 0);
  // WXSS 花括号配平（改样式时最常犯的错：少一个 } 会把后半个文件吃掉）
  const opens = (wxss.match(/{/g) || []).length;
  const closes = (wxss.match(/}/g) || []).length;
  check('WXSS 花括号配平', opens === closes, `${opens} vs ${closes}`);
}

/* ============================================================
   三、Android APP www/me.js：同一套文本 + 卡片里带可点价格
   ============================================================ */
function loadAppMe() {
  const src = fs.readFileSync(APP_ME_JS, 'utf8');
  const sandbox = {
    console,
    JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map,
    parseInt, parseFloat, isNaN,
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    window: { addEventListener: () => {}, innerWidth: 375, innerHeight: 800 },
    document: { addEventListener: () => {}, getElementById: () => null, querySelectorAll: () => [] },
    navigator: {},
    // app.js 提供的全局（这里只测纯文本/模板函数）
    escHtml: (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    showToast: () => {},
    copyText: () => {},
    ORDER_STATUS_MAP: { queued: '排队中', printing: '待添加', sent: '已完成', failed: '失败' },
    esc: (s) => String(s == null ? '' : s),
    api: () => Promise.resolve({ status: 200, data: { success: true } }),
    state: { token: 'stub', role: 'admin', openid: 'dev_test' },
    createPollScheduler: () => ({ start() {}, stop() {} }),
    loadMeTab: () => {},
  };
  sandbox.global = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: 'www/me.js' });
  return sandbox;
}

function runApp() {
  section('Android APP www/me.js');
  const g = loadAppMe();

  check('APP 侧函数齐全（openPricePop / closePricePop / buildPriceDetailText）',
        typeof g.openPricePop === 'function' && typeof g.closePricePop === 'function'
        && typeof g.buildPriceDetailText === 'function');

  const order = makeOrder();
  const html = g.orderCardHTML(order, true, true);
  check('订单卡片里「合计价格」可点（data-price-copy + 复制图标）',
        /class="detail-price-tap" data-price-copy="3"/.test(html)
        && /detail-price-tap[\s\S]{0,260}price-copy-icon[\s\S]{0,40}📋/.test(html),
        (html.match(/detail-price-tap[^>]*>/) || [''])[0]);

  const simple = ((order.order_number ? order.order_number + ' ' : '') + '¥' + Number(order.total_price).toFixed(2));
  check('APP「复制价格」文本与小程序同格式', simple === 'HN20261025-0013 ¥3.80', simple);

  const detail = g.buildPriceDetailText(order);
  check('APP「复制详细价格」与小程序逐行同款（无附加服务时）',
        detail.indexOf('计费明细') === 0
        && detail.indexOf('2 份 × 6 页 | 双面 | 全部页 | ¥2.40') > 0
        && detail.indexOf('💰合计: 2.40+0.80+0.60=￥3.80') > 0, detail.split('\n').slice(-1)[0]);

  const withExtras = makeOrder({
    total_price: 4.68, urgency: '高', urgency_price: 0.3,
    cover_page: 2, cover_page_price: 0.1,
    delivery_enabled: 1, delivery_location: '1号楼北楼', delivery_percentage: 10,
  });
  const d2 = g.buildPriceDetailText(withExtras);
  check('APP 附加服务与后端同口径（派送 0.38 / 加急 0.30 / 首页 0.20）',
        d2.indexOf('派送：是 | 1号楼北楼 10% | ￥0.38') > 0
        && d2.indexOf('优先级：高 | ￥0.30') > 0
        && d2.indexOf('打印首页信息 | 0.20') > 0
        && d2.indexOf('💰合计: 2.40+0.80+0.60+0.30+0.20+0.38=￥4.68') > 0);
  const d3 = g.buildPriceDetailText(makeOrder({ total_price: 9.99 }));
  check('APP 口径不一致时只给实收合计', d3.indexOf('💰合计: ￥9.99') > 0 && d3.indexOf('=￥9.99') < 0);
  const d4 = g.buildPriceDetailText(makeOrder({
    total_price: 9.99, delivery_enabled: 1, delivery_location: '1号楼北楼', delivery_percentage: 10,
  }));
  check('APP 残差不可信时派送行不写金额（不把差额归因到派送）',
        d4.indexOf('派送：是 | 1号楼北楼 10%\n') > 0
        && d4.indexOf('派送：是 | 1号楼北楼 10% | ￥') < 0
        && d4.indexOf('💰合计: ￥9.99') > 0);
}

/* ============================================================
   四、Android APP：事件接线 / 切页收起 / 样式
   ============================================================ */
function checkAppWiring() {
  section('Android APP 接线');
  const me = fs.readFileSync(APP_ME_JS, 'utf8');
  const app = fs.readFileSync(APP_APP_JS, 'utf8');
  const css = fs.readFileSync(APP_CSS, 'utf8');

  check('两个订单列表都拦下价格点击（不能让卡片折叠抢先）',
        /'orderList'\)\.addEventListener\('click'[\s\S]{0,600}data-price-copy[\s\S]{0,200}openPricePop/.test(me)
        && /'userOrdersList'\)\.addEventListener\('click'[\s\S]{0,600}data-price-copy[\s\S]{0,200}openPricePop/.test(me));
  check('气泡挂到 body（fixed 定位逃出 transform 层）',
        /document\.body\.appendChild\(mask\)/.test(me));
  check('点 mask / 复制成功都会移除气泡', /function closePricePop\(\)/.test(me)
        && /mask\.addEventListener\('click'/.test(me) && /closePricePop\(\)/.test(me));
  check('上方放不下时翻到价格下方', /if \(top < 8\) \{ below = true; top = rect\.bottom \+ 12; \}/.test(me));
  check('切 tab（app.js switchTab）收起气泡', /closePricePop/.test(app));
  check('pagehide 收起气泡', /pagehide'[\s\S]{0,80}closePricePop\(\)/.test(me));

  check('styles.css：气泡 fixed + 上下两种箭头 + 深色覆盖',
        /\.price-pop-mask\s*\{[^}]*position:\s*fixed/.test(css)
        && /\.price-pop\s*\{[^}]*position:\s*fixed/.test(css)
        && /\.price-pop:not\(\.price-pop-below\) \.price-pop-caret\s*\{[^}]*bottom:/.test(css)
        && /\.price-pop\.price-pop-below \.price-pop-caret\s*\{[^}]*top:/.test(css)
        && /\.theme-dark \.price-pop\s*\{/.test(css));
}

/* ============================================================
   五、跨端一致性：两端文案/字段名不得漂移
   ============================================================ */
function checkCrossClient() {
  section('两端一致性');
  const mp = fs.readFileSync(MP_ME_JS, 'utf8');
  const app = fs.readFileSync(APP_ME_JS, 'utf8');
  const labels = ['计费明细', '复制价格', '复制详细价格', '💰合计', '优先级：', '打印首页信息', '派送：是'];
  labels.forEach((label) => {
    check(`两端都有「${label}」文案`, mp.indexOf(label) > 0 && app.indexOf(label) > 0);
  });
  check('两端都用订单实收值（total_price），不在「我」页重算单价',
        mp.indexOf('f.total_price') > 0 && app.indexOf('f.total_price') > 0
        && !/simplexPrice|duplexPrice/.test(mp.slice(mp.indexOf('onCopyOrderDetailPrice'), mp.indexOf('onDetailCancelOrder')))
        && !/simplexPrice|duplexPrice/.test(app.slice(app.indexOf('buildPriceDetailText('), app.indexOf('function closePricePop'))));
}

try {
  runMiniProgram();
  checkMiniProgramMarkup();
  runApp();
  checkAppWiring();
  checkCrossClient();
} catch (e) {
  failures++;
  console.log('[FAIL] 脚本异常: ' + (e && e.stack ? e.stack : e));
}

if (failures) {
  console.log(`\n${failures} 项未通过`);
  process.exit(1);
}
console.log('\n全部通过');
