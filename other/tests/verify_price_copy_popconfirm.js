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
 *   ② 「复制价格」= `{订单号} — ¥{金额}`（em dash + 两侧空格；无订单号只 `¥金额`），
 *      与本地打印工具 gui.py:_on_copy_total 逐字一致；
 *   ③ 「复制详细价格」= 本地打印工具 gui.py:_on_copy_detail 的逐字格式：
 *      `订单号: {号}` / 14 个 ASCII 短横线 / `计费明细` / 短横线 /
 *      每文件 `1. 文件名` + `   {份数}x 双面|单面 范围:{范围或全部}` + `   {计费公式} = ¥{实收}` /
 *      （有附加服务费时）`附加服务费: ¥x.xx` / 短横线 / `💰合计: ¥{实收}`。
 *      计费公式按 calc_cost 形状复算（`3张×0.40`、`(…)x2份`），但**金额永远是订单实收值**；
 *   ④ 算式复算 ≠ 实收（历史单价/人工改价）→ 该文件只写 `¥{实收}`，不编算式；
 *      附加服务费只在「真的勾了附加服务且残差与算式一致」时才写，其余只给实收合计；
 *   ⑤ 气泡在 page-root 外 + position:fixed —— 订单卡片 `.order-card-detail` 有
 *      overflow:hidden / max-height，气泡放在卡片里会被裁掉；
 *   ⑥ 两端（微信小程序 / Android APP）同款，且切 tab、订单刷新、卡片收起时气泡自动收起。
 *
 * 用法：node tests/verify_price_copy_popconfirm.js     （exit 0 = 全绿）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..', '..');
const MP_ME_JS = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'me', 'me.js');
const MP_ME_WXML = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'me', 'me.wxml');
const MP_ME_WXSS = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'me', 'me.wxss');
const APP_ME_JS = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'me.js');
const APP_APP_JS = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'app.js');
const APP_CSS = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'styles.css');
const MP_ME_JSON = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'me', 'me.json');
/* 2026-11-09：文案/算式抽成 utils/price_copy.js，「我」页与 user-orders 子视图共用；
   气泡样式抽成 utils/price_pop.wxss，两页 @import。下面的守卫脚本据此改读这三处。 */
const MP_PRICE_COPY_JS = path.join(REPO, 'mobile_apps', 'h_n_print', 'utils', 'price_copy.js');
const MP_PRICE_POP_WXSS = path.join(REPO, 'mobile_apps', 'h_n_print', 'utils', 'price_pop.wxss');
const MP_USER_JS = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'user-orders', 'user-orders.js');
const MP_USER_WXML = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'user-orders', 'user-orders.wxml');
const MP_USER_WXSS = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'user-orders', 'user-orders.wxss');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }

/* me.js 2026-10 起 require('../../utils/avatar_crop')（头像裁剪几何）：**真实加载**该模块，
   文件缺失/语法错/导出缺失直接抛 —— 不用空对象桩，免得 me.js 的依赖被静默放过。 */
const AVATAR_CROP_MOD = (() => {
  const p = path.join(REPO, 'mobile_apps', 'h_n_print', 'utils', 'avatar_crop.js');
  const m = { exports: {} };
  new Function('module', 'exports', 'require', fs.readFileSync(p, 'utf8'))(m, m.exports, (dep) => {
    throw new Error('真实模块 utils/avatar_crop.js 意外 require 了 ' + dep);
  });
  if (typeof m.exports.fitState !== 'function' || typeof m.exports.cropRect !== 'function') {
    throw new Error('utils/avatar_crop.js 未导出 fitState/cropRect（头像裁剪几何变了？）');
  }
  return m.exports;
})();

/* utils/price_copy.js 是文案与算式的唯一实现：**真实加载**它（不是桩），
   只把它的 './pages' 依赖换成与 loadMiniProgram 同口径的页数 shim。
   这样「两个页面都走同一份实现」这件事本身也在被验证。 */
const PRICE_COPY_MOD = (() => {
  const p = MP_PRICE_COPY_JS;
  const m = { exports: {} };
  new Function('module', 'exports', 'require', fs.readFileSync(p, 'utf8'))(m, m.exports, (dep) => {
    if (dep === './pages') {
      return { countPagesInRange: (range, count) => (range ? 0 : (Number(count) || 0)) };
    }
    throw new Error('utils/price_copy.js 意外 require 了 ' + dep);
  });
  ['priceLine', 'priceDetailLines', 'createPriceRulesGetter', 'orderTotal'].forEach((k) => {
    if (typeof m.exports[k] !== 'function') throw new Error('utils/price_copy.js 未导出 ' + k);
  });
  return m.exports;
})();

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
      if (dep.indexOf('utils/pages') >= 0) {
        // 计费算式要用真实页数：无范围时 = page_count（与 utils/pages 口径一致）
        return { countPagesInRange: (range, count) => (range ? 0 : (Number(count) || 0)) };
      }
      if (dep.indexOf('utils/avatar_crop') >= 0) return AVATAR_CROP_MOD;
      if (dep.indexOf('price_copy') >= 0) return PRICE_COPY_MOD;
      if (dep.indexOf('utils/request') >= 0) {
        return {
          // 详细价格要先拿单价才能写算式：pricing 请求同步回调（0.3/0.4 = pricing.json 权威值）
          request: (opts) => {
            if (opts && typeof opts.url === 'string' && opts.url.indexOf('/api/pricing') >= 0) {
              opts.success({ statusCode: 200, data: { success: true, pricing: { simplex_price: 0.3, duplex_price: 0.4 } } });
            }
          },
          createPollScheduler: () => ({ start() {}, stop() {} }),
        };
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
  check('③ 复制价格 = `{订单号} — ¥金额`（与本地打印工具 Ctrl+C 同格式）',
        calls.clipboard[0] === 'HN20261025-0013 — ¥3.80', `got=${JSON.stringify(calls.clipboard[0])}`);
  // 没有订单号 → 只复制金额（不留下孤零零的破折号）
  calls.clipboard.length = 0;
  const noNumber = makeOrder({ order_number: '' });
  ctx = makeCtx(component, [noNumber]);
  ctx.onCopyOrderPrice({ currentTarget: { dataset: { id: noNumber.id } } });
  check('③ 无订单号时只复制 `¥金额`', calls.clipboard[0] === '¥3.80', `got=${JSON.stringify(calls.clipboard[0])}`);
  check('③ 复制后气泡收起 + 成功 toast', ctx.data.showPricePop === false && calls.toast[0] === '已复制价格');

  // ④ 复制详细价格（无附加服务：逐文件 + 合计算式）
  calls.clipboard.length = 0; calls.toast.length = 0;
  ctx = makeCtx(component, [order]);
  ctx.onCopyOrderDetailPrice({ currentTarget: { dataset: { id: order.id } } });
  const detail = calls.clipboard[0] || '';
  const DASH = '-'.repeat(14);
  const detailLines = detail.split('\n');
  check('④ 详细价格 = 本地工具逐字结构（订单号 → 短横线 → 计费明细 → 短横线）',
        detailLines[0] === '订单号: HN20261025-0013'
        && detailLines[1] === DASH
        && detailLines[2] === '计费明细'
        && detailLines[3] === DASH,
        JSON.stringify(detailLines.slice(0, 4)));
  check('④ 三个文件都列出（`1. 原名` 连续编号）',
        detail.indexOf('1. 示波器的使用.pdf') > 0
        && detail.indexOf('2. 数据结构-第3次.docx') > 0
        && detail.indexOf('3. 示波器数据表格.pdf') > 0);
  check('④ 每个文件带「份数x 单/双面 范围:…」+ 计费公式行',
        detail.indexOf('   2x 双面 范围:全部') > 0
        && detail.indexOf('   2x 单面 范围:全部') > 0
        && detail.indexOf('   (3张×0.40)×2份 = ¥2.40') > 0
        && detail.indexOf('   (1张×0.30)×2份 = ¥0.60') > 0,
        detailLines.join(' / '));
  check('④ 合计 = 订单实收值（不拿逐项相加当合计）',
        detail.indexOf('💰合计: ¥3.80') > 0 && detailLines[detailLines.length - 2] === DASH,
        detailLines.slice(-2).join(' / '));
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
  check('⑤ 附加服务费合并成一行（派送 0.38 + 加急 0.30 + 首页 0.20 = 0.88）',
        d2.indexOf('附加服务费: ¥0.88') > 0, d2);
  check('⑤ 合计 = 订单实收值 4.68', d2.indexOf('💰合计: ¥4.68') > 0
        && d2.split('\n').slice(-1)[0] === '💰合计: ¥4.68', d2.split('\n').slice(-1)[0]);

  // ⑥ 逐项相加 ≠ 订单实收（历史数据/人工调整）→ 只写实收合计，不编算式
  const mismatch = makeOrder({ total_price: 9.99 });
  calls.clipboard.length = 0;
  ctx = makeCtx(component, [mismatch]);
  ctx.onCopyOrderDetailPrice({ currentTarget: { dataset: { id: mismatch.id } } });
  const d3 = calls.clipboard[0] || '';
  check('⑥ 口径不一致时只给实收合计（不编算式、也不编附加服务费理由）',
        d3.split('\n').slice(-1)[0] === '💰合计: ¥9.99'
        && d3.indexOf('附加服务费') < 0
        && d3.indexOf('=¥9.99') < 0, d3.split('\n').slice(-1)[0]);

  // ⑥b 派送单被人工改过价 → 残差与「小计 × 百分比」差太多，派送行不写金额、合计仍按实收
  const oddDelivery = makeOrder({
    total_price: 9.99, delivery_enabled: 1, delivery_location: '1号楼北楼', delivery_percentage: 10,
  });
  calls.clipboard.length = 0;
  ctx = makeCtx(component, [oddDelivery]);
  ctx.onCopyOrderDetailPrice({ currentTarget: { dataset: { id: oddDelivery.id } } });
  const d4 = calls.clipboard[0] || '';
  check('⑥b 残差与算式对不上时不把差额归因到附加服务（整行不出现，合计=实收）',
        d4.indexOf('附加服务费') < 0
        && d4.split('\n').slice(-1)[0] === '💰合计: ¥9.99', d4.split('\n').slice(-2).join(' / '));

  // ⑦ 订单不在当前页 → 不复制、不崩
  calls.clipboard.length = 0;
  ctx = makeCtx(component, [order]);
  ctx.onCopyOrderPrice({ currentTarget: { dataset: { id: 999 } } });
  check('⑦ 找不到订单 → 不写剪贴板且气泡收起',
        calls.clipboard.length === 0 && ctx.data.showPricePop === false);

  // ⑧ 点气泡外（页面 touchstart 捕获）→ 收起
  ctx = makeCtx(component, [order]);
  ctx.data.showPricePop = true;
  ctx.onClosePricePop();
  check('⑧ 点气泡外 → 收起', ctx.data.showPricePop === false);

  // ⑧b 2026-11-09 用户反馈：气泡是「非模态」的，页面一碰（含滑动）就先收起，
  //     手势本身照常交给 WXS 滚动引擎 —— 不能再靠 catchtouchmove 把整页锁住。
  ctx = makeCtx(component, [order]);
  ctx.data.showPricePop = true;
  ctx.onPricePopDismiss();
  check('⑧b 页面 touchstart（capture）→ 收起气泡', ctx.data.showPricePop === false);
  ctx.onPricePopDismiss();
  check('⑧b2 已收起时重复触发不报错、状态不变', ctx.data.showPricePop === false);
}

/* ============================================================
   二、小程序：WXML / WXSS 接线（气泡必须在 page-root 外 + fixed）
   ============================================================ */
function checkMiniProgramMarkup() {
  section('小程序 me.wxml / me.wxss 接线');
  const wxml = fs.readFileSync(MP_ME_WXML, 'utf8');
  // 气泡样式已抽到 utils/price_pop.wxss（两页 @import）：检查「页面 + 共用样式」合并后的结果
  const wxss = fs.readFileSync(MP_ME_WXSS, 'utf8') + '\n' + fs.readFileSync(MP_PRICE_POP_WXSS, 'utf8');
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
  check('价格气泡不是模态：遮罩 is-pass-through（不参与命中测试）+ 两个按钮',
        /class="price-pop-mask is-pass-through[^>]*>/.test(wxml)
        && wxml.indexOf('catchtap="onCopyOrderPrice"') > 0
        && wxml.indexOf('catchtap="onCopyOrderDetailPrice"') > 0
        && wxml.indexOf('>复制价格<') > 0
        && wxml.indexOf('>复制详细价格<') > 0);
  check('价格遮罩上不许再挂 catchtouchmove / catchtap（那正是把页面锁住的写法）',
        !/class="price-pop-mask is-pass-through[^>]*catchtouchmove/.test(wxml)
        && !/class="price-pop-mask is-pass-through[^>]*catchtap=/.test(wxml));
  check('页面 page-root 用 capture-bind:touchstart 收起气泡（滑动=先关再滚）',
        /class="page-root[\s\S]{0,200}?capture-bind:touchstart="onPricePopDismiss"/.test(wxml));
  // wxml 里挂的处理器必须在 JS 里真实存在（写错名字微信只会静默告警）
  const dismissBinding = (wxml.match(/capture-bind:touchstart="([^"]+)"/) || [])[1] || '';
  check('page-root 挂的 touchstart 处理器在 me.js 里存在且是函数',
        dismissBinding === 'onPricePopDismiss'
        && /^\s*onPricePopDismiss\(\)\s*\{/m.test(fs.readFileSync(MP_ME_JS, 'utf8')));
  check('气泡内点击不穿透（气泡自身 catchtap="noop"）',
        /class="price-pop \{\{pricePopBelow[\s\S]{0,400}catchtap="noop"/.test(wxml));

  check('WXSS：mask 与气泡都是 position:fixed（逃出 translateY 变换层 + 不被卡片裁剪）',
        /\.price-pop-mask\s*\{[^}]*position:\s*fixed/.test(wxss)
        && /\.price-pop\s*\{[^}]*position:\s*fixed/.test(wxss));
  check('WXSS：is-pass-through 遮罩 pointer-events:none，气泡 pointer-events:auto',
        /\.price-pop-mask\.is-pass-through\s*\{[^}]*pointer-events:\s*none/.test(wxss)
        && /\.price-pop\s*\{[^}]*pointer-events:\s*auto/.test(wxss));
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

  const simple = g.buildPriceSimpleText(order);
  check('APP「复制价格」= `{订单号} — ¥金额`（与小程序/本地工具同格式）',
        simple === 'HN20261025-0013 — ¥3.80', simple);

  const RULES = { simplex: 0.3, duplex: 0.4 };
  const DASH = '-'.repeat(14);
  const detail = g.buildPriceDetailText(order, RULES);
  const dLines = detail.split('\n');
  check('APP「复制详细价格」与小程序逐行同款（无附加服务时）',
        dLines[0] === '订单号: HN20261025-0013' && dLines[1] === DASH
        && dLines[2] === '计费明细' && dLines[3] === DASH
        && detail.indexOf('   2x 双面 范围:全部') > 0
        && detail.indexOf('   (3张×0.40)×2份 = ¥2.40') > 0
        && dLines.slice(-1)[0] === '💰合计: ¥3.80', dLines.join(' / '));

  const withExtras = makeOrder({
    total_price: 4.68, urgency: '高', urgency_price: 0.3,
    cover_page: 2, cover_page_price: 0.1,
    delivery_enabled: 1, delivery_location: '1号楼北楼', delivery_percentage: 10,
  });
  const d2 = g.buildPriceDetailText(withExtras, RULES);
  check('APP 附加服务费合并成一行（0.38 + 0.30 + 0.20 = 0.88）+ 合计取实收',
        d2.indexOf('附加服务费: ¥0.88') > 0 && d2.indexOf('💰合计: ¥4.68') > 0);
  const d3 = g.buildPriceDetailText(makeOrder({ total_price: 9.99 }), RULES);
  check('APP 口径不一致时只给实收合计（无附加服务费行）',
        d3.indexOf('附加服务费') < 0 && d3.split('\n').slice(-1)[0] === '💰合计: ¥9.99');
  const d4 = g.buildPriceDetailText(makeOrder({
    total_price: 9.99, delivery_enabled: 1, delivery_location: '1号楼北楼', delivery_percentage: 10,
  }), RULES);
  check('APP 残差不可信时不把差额归因到附加服务',
        d4.indexOf('附加服务费') < 0 && d4.split('\n').slice(-1)[0] === '💰合计: ¥9.99');
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

  // 只看 openPricePop 函数体（状态原因气泡仍是模态，它那条 touchmove preventDefault 是对的）
  const openPopBody = (me.match(/function openPricePop\([\s\S]*?\n}/) || [''])[0];
  check('APP 价格气泡非模态：遮罩 is-pass-through，且不再 touchmove preventDefault',
        /mask\.className = 'price-pop-mask is-pass-through'/.test(openPopBody)
        && openPopBody.length > 500
        && !/addEventListener\('touchmove'/.test(openPopBody));
  check('APP 失去焦点即关闭：touchstart/mousedown capture + 任一滚动收起',
        /document\.addEventListener\('touchstart', onDocTouchStart, \{ capture: true, passive: true \}\)/.test(me)
        && /document\.addEventListener\('mousedown', onDocTouchStart, \{ capture: true \}\)/.test(me)
        && /window\.addEventListener\('scroll', onDocScroll, \{ capture: true, passive: true \}\)/.test(me));
  check('APP CSS：.price-pop-mask.is-pass-through 不吃指针、不锁触摸；气泡自己可点',
        /\.price-pop-mask\.is-pass-through\s*\{[^}]*pointer-events:\s*none[^}]*touch-action:\s*auto/.test(css)
        && /\.price-pop\s*\{[^}]*pointer-events:\s*auto/.test(css));

  check('styles.css：气泡 fixed + 上下两种箭头 + 深色覆盖',
        /\.price-pop-mask\s*\{[^}]*position:\s*fixed/.test(css)
        && /\.price-pop\s*\{[^}]*position:\s*fixed/.test(css)
        && /\.price-pop:not\(\.price-pop-below\) \.price-pop-caret\s*\{[^}]*bottom:/.test(css)
        && /\.price-pop\.price-pop-below \.price-pop-caret\s*\{[^}]*top:/.test(css)
        && /\.theme-dark \.price-pop\s*\{/.test(css));
}

/* ============================================================
   四·补、APP「复制详细价格」反查不得被「最近订单」缓存行抢走
   ------------------------------------------------------------
   现场 bug（2026-11-09 用户 HN20261108-0009）：在「管理管理员 → 某人任务」子视图里点
   「复制详细价格」，复制出来只有 订单号 / 计费明细 / 合计，逐文件三行全没了。
   根因：/api/admin/recent_orders 按契约**不下发逐文件明细**，me.js 把它们登记成 `files: []`
   的行并且把 order_id 映射成 id；`_priceOrderById` 按 id 反查且「先到先得」，
   于是同一个订单被最近订单的缓存行命中 ⇒ buildPriceDetailText 没有文件可写。
   本段钉住：同一 id 在多个池子里出现时，**带 files 的完整行必须赢**；
   只有缓存行可用时才退回它（那时只剩标题+合计是设计口径，不许编造分项）。
   ============================================================ */
function checkAppPriceLookupPools() {
  section('APP 价格反查池（最近订单缓存行不得抢走完整行）');
  const g = loadAppMe();
  const order = makeOrder();            // id=3，3 个文件，合计 ¥3.80
  check('夹具订单 id=3 且带 3 个文件', order.id === 3 && order.files.length === 3);
  // 「最近订单」缓存行：同一 id，只有标题 + 合计（与 loadRecentOrders 里的映射同形状）
  const cachedRow = {
    id: order.id, order_number: order.order_number, total_price: order.total_price,
    files: [], owner: '姚懿祥', owner_masked: '姚**', created_at: order.created_at,
    file_count: 3, page_count: 10, status: order.status, source: 'local', is_admin_print: false,
  };
  // 两个池子都登记同一个 id：完整行在用户订单子视图，缓存行在「近 N 条订单」
  g.meStateRef = null;
  vm.runInContext('meState.orders = []; meState.recentOrderRows = [];'
    + ' meState.recentOrders = [__cached];'
    + ' meState.userOrdersView = { orders: [__full] };',
    Object.assign(g, { __cached: cachedRow, __full: order }));
  const hit = vm.runInContext('_priceOrderById(3)', g);
  check('同 id 时反查命中的是带 files 的完整行', !!(hit && hit.files && hit.files.length === 3),
        hit ? ('files=' + ((hit.files || []).length)) : 'null');
  const detail = g.buildPriceDetailText(hit, { simplex: 0.3, duplex: 0.4 });
  check('「复制详细价格」逐文件三行齐全（不再只剩合计）',
        detail.indexOf('1. 示波器的使用.pdf') > 0
        && detail.indexOf('2. 数据结构-第3次.docx') > 0
        && detail.indexOf('3. 示波器数据表格.pdf') > 0
        && detail.split('\n').slice(-1)[0] === '💰合计: ¥3.80',
        detail.split('\n').slice(4, 6).join(' / '));

  // 反向：只有缓存行可用（例如刚打开 APP，还没进过用户订单子视图）→ 退回缓存行，
  // 详细价格就只剩标题+合计 —— 这是设计口径（不编造分项），不是本 bug 的回归。
  vm.runInContext('meState.userOrdersView = { orders: [] };', g);
  const hit2 = vm.runInContext('_priceOrderById(3)', g);
  check('只有缓存行可用时退回缓存行（不报错、不编造）',
        !!(hit2 && hit2.id === 3 && (hit2.files || []).length === 0));
  const detail2 = g.buildPriceDetailText(hit2, { simplex: 0.3, duplex: 0.4 });
  check('缓存行兜底时只剩标题+合计（14 短横线 结构不变）',
        detail2.indexOf('计费明细') > 0 && detail2.indexOf('. ') < 0
        && detail2.split('\n').slice(-1)[0] === '💰合计: ¥3.80');

  // 「带明细优先」独立于池子顺序：完整行排在**更后面的池子**里也必须赢
  // （池顺序只是顺带优化；真正的判据是「有 files 的行优先」，否则换个池子顺序又会复发）
  vm.runInContext('meState.orders = [__cached]; meState.userOrdersView = { orders: [] };'
    + ' meState.recentOrderRows = []; meState.recentOrders = [__full];', g);
  const hit3 = vm.runInContext('_priceOrderById(3)', g);
  check('完整行在后面的池子里也优先于前面的缓存行（不靠池子顺序）',
        !!(hit3 && hit3.files && hit3.files.length === 3),
        hit3 ? ('files=' + ((hit3.files || []).length)) : 'null');
}

/* ============================================================
   五、跨端一致性：两端文案/字段名不得漂移
   ============================================================ */
function checkCrossClient() {
  section('两端一致性（小程序两个页面共用 utils/price_copy.js）');
  const pc = fs.readFileSync(MP_PRICE_COPY_JS, 'utf8');
  const meJs = fs.readFileSync(MP_ME_JS, 'utf8');
  const userJs = fs.readFileSync(MP_USER_JS, 'utf8');
  // 小程序侧的全部源：两个页面 + 共用实现（文案只允许出现在后者）
  const mp = meJs + '\n' + userJs + '\n' + pc;
  const app = fs.readFileSync(APP_ME_JS, 'utf8');
  const labels = ['计费明细', '复制价格', '复制详细价格', '💰合计', '附加服务费', '订单号: ', '范围:'];
  labels.forEach((label) => {
    check(`两端都有「${label}」文案`, mp.indexOf(label) > 0 && app.indexOf(label) > 0);
  });
  // 单价只用来拼「计费公式」文案，金额永远写实收值：逐文件 f.total_price、合计 total_price。
  check('两端金额一律取实收（逐文件 f.total_price / 合计 total_price）',
        /Math\.round\(\(Number\(f\.total_price\)/.test(pc) && /f\.total_price/.test(app)
        && pc.indexOf("'💰合计: ¥' + total.toFixed(2)") > 0
        && app.indexOf("'💰合计: ¥' + total.toFixed(2)") > 0
        && !/baseTotal \+ /.test(pc.slice(pc.indexOf('function priceDetailLines'), pc.indexOf('function createPriceRulesGetter')))
        && !/baseTotal \+ /.test(app.slice(app.indexOf('function buildPriceDetailText'), app.indexOf('function closePricePop'))));

  // ── 单源守卫（2026-11-09）──
  // 文案/算式只许有一份实现。历史教训：mission 里多份副本各自漂移（utils/pages.js 顶部
  // 记的是同一类事故；这里是它的价格版）。
  // 只认「真的在拼这段文案」的代码（注释里提一句不算副本）
  check('「计费明细」只在 utils/price_copy.js 里被写进文本',
        pc.indexOf("lines.push('计费明细')") > 0
        && meJs.indexOf("lines.push('计费明细')") < 0
        && userJs.indexOf("lines.push('计费明细')") < 0);
  check('14 短横线 / 💰合计 的拼接也只在共用实现里',
        meJs.indexOf("'-'.repeat(14)") < 0 && userJs.indexOf("'-'.repeat(14)") < 0
        && meJs.indexOf('💰合计: ¥') < 0 && userJs.indexOf('💰合计: ¥') < 0
        && pc.indexOf("PRICE_DASH = '-'.repeat(14)") > 0);
  check('两个页面 require 同一份实现（不是各拼一份）',
        /require\('\.\.\/\.\.\/utils\/price_copy'\)/.test(meJs)
        && /require\('\.\.\/\.\.\/utils\/price_copy'\)/.test(userJs));
  check('气泡样式只有 utils/price_pop.wxss 一份，两页都 @import',
        fs.readFileSync(MP_ME_WXSS, 'utf8').indexOf('utils/price_pop.wxss') > 0
        && fs.readFileSync(MP_USER_WXSS, 'utf8').indexOf('utils/price_pop.wxss') > 0
        && fs.readFileSync(MP_ME_WXSS, 'utf8').indexOf('.price-pop-mask') < 0
        && fs.readFileSync(MP_USER_WXSS, 'utf8').indexOf('.price-pop-mask') < 0
        && fs.readFileSync(MP_PRICE_POP_WXSS, 'utf8').indexOf('.price-pop-mask') > 0);
}

/* ============================================================
   四、小程序 pages/user-orders（管理管理员 → 某人任务 子视图）
   ------------------------------------------------------------
   现场问题（2026-11-09 用户）：这个子视图的价格行原来是**纯文本**，压根没有复制入口
   （与「上传没上传」无关，是功能没做）。现在它和「我」页共用 utils/price_copy.js：
   价格可点 → 同一套气泡 → 同一份文案。本段钉住接线，并**实际跑一遍**两个入口的文本一致。
   ============================================================ */
function loadMiniProgramUserOrders() {
  const src = fs.readFileSync(MP_USER_JS, 'utf8');
  const captured = { component: null, toast: [], clipboard: [] };
  const sandbox = {
    console,
    JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map,
    parseInt, parseFloat, isNaN, isFinite,
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    require: (dep) => {
      if (dep.indexOf('utils/config') >= 0) return { CONFIG: { BASE_URL: 'https://example.invalid' } };
      if (dep.indexOf('utils/pages') >= 0) {
        return { countPagesInRange: (range, count) => (range ? 0 : (Number(count) || 0)) };
      }
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
      if (dep.indexOf('price_copy') >= 0) return PRICE_COPY_MOD;
      if (dep.indexOf('order_stats') >= 0) return { fallbackSheets: () => 0 };
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
    getApp: () => ({ globalData: {} }),
    Component: (cfg) => { captured.component = cfg; },
    Page: (cfg) => { captured.component = cfg; },
  };
  sandbox.global = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: 'user-orders.js' });
  if (!captured.component || !captured.component.methods) {
    throw new Error('user-orders.js 未暴露 Component.methods（夹具需同步更新）');
  }
  return { component: captured.component, calls: captured };
}

function checkMiniProgramUserOrders() {
  section('小程序 pages/user-orders（管理入口 → 某人任务）价格复制');
  const wxml = fs.readFileSync(MP_USER_WXML, 'utf8');
  const js = fs.readFileSync(MP_USER_JS, 'utf8');

  check('价格行可点：id 供测量 + data-id + catchtap（不冒泡去收卡片）',
        wxml.indexOf('id="price-tap-{{item.id}}"') > 0
        && /class="detail-price-tap"[\s\S]{0,300}catchtap="onPriceTap"/.test(wxml)
        && wxml.indexOf('class="price-copy-icon"') > 0);
  // 气泡必须在 page-root 外：否则被 .scroll-content 的 translateY / 卡片 overflow 裁剪
  const popIdx = wxml.indexOf('class="price-pop-mask');
  const pageRootIdx = wxml.lastIndexOf('class="page-root');
  check('气泡在 page-root 外（fixed 定位才不被滚动层/卡片裁剪）',
        popIdx > 0 && pageRootIdx > 0 && popIdx > pageRootIdx, `pop=${popIdx} root=${pageRootIdx}`);
  check('价格气泡不是模态：遮罩 is-pass-through（与「我」页同口径）',
        /class="price-pop-mask is-pass-through[^>]*>/.test(wxml)
        && !/class="price-pop-mask is-pass-through[^>]*catchtouchmove/.test(wxml)
        && !/class="price-pop-mask is-pass-through[^>]*catchtap=/.test(wxml)
        && /class="page-root[\s\S]{0,200}?capture-bind:touchstart="onPricePopDismiss"/.test(wxml)
        && wxml.indexOf('catchtap="onCopyOrderPrice"') > 0
        && wxml.indexOf('catchtap="onCopyOrderPrice"') > 0
        && wxml.indexOf('catchtap="onCopyOrderDetailPrice"') > 0
        && wxml.indexOf('>复制价格<') > 0
        && wxml.indexOf('>复制详细价格<') > 0);
  check('气泡内点击不穿透（气泡自身 catchtap="noop"）',
        /class="price-pop \{\{pricePopBelow[\s\S]{0,300}catchtap="noop"/.test(wxml));
  const wxss = fs.readFileSync(MP_USER_WXSS, 'utf8') + '\n' + fs.readFileSync(MP_PRICE_POP_WXSS, 'utf8');
  check('WXSS：mask 与气泡都是 position:fixed（共用样式已 @import）',
        /\.price-pop-mask\s*\{[^}]*position:\s*fixed/.test(wxss)
        && /\.price-pop\s*\{[^}]*position:\s*fixed/.test(wxss));
  const uoDismiss = (wxml.match(/capture-bind:touchstart="([^"]+)"/) || [])[1] || '';
  check('user-orders：page-root 挂的 touchstart 处理器在 JS 里存在',
        uoDismiss === 'onPricePopDismiss' && /^\s*onPricePopDismiss\(\)\s*\{/m.test(js));
  check('JS：换页 / 刷新 / 展开卡片都收起气泡',
        /currentPage: page, showPricePop: false/.test(js)
        && /expandedOrders: expanded, showPricePop: false/.test(js)
        && /\/\/ 换页\/刷新[^\n]*\n\s*showPricePop: false/.test(js));

  // ── 行为：同一订单在两个入口复制出的文本必须逐字相同 ──
  const { component, calls } = loadMiniProgramUserOrders();
  check('新增方法齐全（onPriceTap / 两个复制 / 关闭 / 找单 / 剪贴板）',
        ['onPriceTap', 'onClosePricePop', 'onCopyOrderPrice', 'onCopyOrderDetailPrice',
         '_orderById', '_copyPriceText'].every((k) => typeof component.methods[k] === 'function'));

  const order = makeOrder();
  const data = JSON.parse(JSON.stringify(component.data || {}));
  data.orders = [order];
  const ctx = { data };
  ctx.setData = (patch, cb) => { Object.assign(ctx.data, patch); if (cb) cb(); };
  ctx.createSelectorQuery = () => {
    const q = { select: () => q, boundingClientRect: () => q, exec: (cb) => { if (cb) cb([null]); } };
    return q;
  };
  Object.keys(component.methods).forEach((k) => { ctx[k] = component.methods[k].bind(ctx); });

  const LF = String.fromCharCode(10);
  const expected = PRICE_COPY_MOD.priceDetailLines(order, { simplex_price: 0.3, duplex_price: 0.4 }).join(LF);

  calls.clipboard.length = 0;
  ctx.onCopyOrderDetailPrice({ currentTarget: { dataset: { id: order.id } } });
  const got = calls.clipboard[0] || '';
  check('子视图「复制详细价格」与「我」页逐字相同（同一份实现）', got === expected,
        JSON.stringify(got.split(LF).slice(0, 4)));
  check('三个文件子项都在（现场 bug 的回归锚点：以前只有标题+合计）',
        got.split(LF).filter((l) => /^\d+\. /.test(l)).length === 3, JSON.stringify(got));
  check('末行是实收合计', got.split(LF).slice(-1)[0] === '💰合计: ¥3.80');

  calls.clipboard.length = 0;
  ctx.onCopyOrderPrice({ currentTarget: { dataset: { id: order.id } } });
  check('子视图「复制价格」= `{订单号} — ¥金额`',
        calls.clipboard[0] === 'HN20261025-0013 — ¥3.80', JSON.stringify(calls.clipboard[0]));
  check('复制成功后气泡收起', ctx.data.showPricePop === false);

  // 展开/收起卡片、换页时会收起气泡（避免气泡悬在已换掉的列表上）
  ctx.data.showPricePop = true;
  ctx.onClosePricePop();
  check('点气泡外 → 收起', ctx.data.showPricePop === false);
  ctx.data.showPricePop = true;
  ctx.onPricePopDismiss();
  check('页面 touchstart（capture）→ 收起（与「我」页同一套非模态行为）',
        ctx.data.showPricePop === false);

  // 订单不在当前页 → 不复制、不崩
  calls.clipboard.length = 0;
  ctx.onCopyOrderPrice({ currentTarget: { dataset: { id: 999 } } });
  check('找不到订单 → 不写剪贴板且气泡收起',
        calls.clipboard.length === 0 && ctx.data.showPricePop === false);
}

try {
  runMiniProgram();
  checkMiniProgramMarkup();
  checkMiniProgramUserOrders();
  runApp();
  checkAppPriceLookupPools();
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
