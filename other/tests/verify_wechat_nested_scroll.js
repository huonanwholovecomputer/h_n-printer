/* eslint-disable */
/**
 * 文件列表嵌套滚动「边界交接」验收（需求④）
 *
 * 用户原话：「上传多个文件后文件列表有内部滚动，滚到底后继续滑应由外层接管（顶部同理），
 * 不是卡在内部列表底部。这问题解决很久了」。
 *
 * 仓库里已有接力机制（utils/scroll.wxs 的 nested/nestedAllowed + 逻辑层 _refreshListEdges），
 * 本脚本不重写它，只钉**两处真实缺口**（它们会让接力失效、表现成「卡在内部列表底部」）：
 *
 *   缺口 A：`_fileListRealPx`（bindscroll 实测的真实内容高）只在「列表不可滚」时被清零。
 *           删掉文件后若估值仍判「可滚」，旧的大内容高残留 ⇒ maxScroll 过大 ⇒ atBottom 恒 false
 *           ⇒ WXS 把滑动让给已经滚不动的内层 ⇒ 页面纹丝不动；而内层滚不动就不会再发 scroll
 *           事件来纠正自己（自锁）。
 *   缺口 B：`listOverflow` 用**估值**内容高与可视高比，而估值比真实渲染高 4~7%（代码注释已记录）
 *           ⇒「刚好放得下」的列表被判成可滚 ⇒ WXS touchstart 里 nestedAllowed=true
 *           ⇒ 手势被一个根本不能滚的内层吃掉。
 *
 * 用法：node tests/verify_wechat_nested_scroll.js   （exit 0 = 全绿）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..', '..');
const MP_DIR = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'index');
const INDEX_JS = path.join(MP_DIR, 'index.js');
const INDEX_WXML = path.join(MP_DIR, 'index.wxml');
const SCROLL_WXS = path.join(REPO, 'mobile_apps', 'h_n_print', 'utils', 'scroll.wxs');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }

function stripJsComments(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"\\])\/\/[^\n]*/g, '$1');
}

/* ---------------- 一、静态判据 ---------------- */
function runStatic() {
  section('一、静态判据（源码接线）');
  const js = fs.readFileSync(INDEX_JS, 'utf8');
  const wxml = fs.readFileSync(INDEX_WXML, 'utf8');
  const wxs = fs.readFileSync(SCROLL_WXS, 'utf8');

  const m = js.match(/const FILE_CARD_HEIGHT_EST_OVER\s*=\s*([0-9.]+)/);
  check('①有「估值折算系数」常量 FILE_CARD_HEIGHT_EST_OVER', !!m, m ? m[1] : 'missing');
  check('①折算系数落在 1.03~1.08（注释口径：估值比真实高 4~7%）',
        !!m && Number(m[1]) > 1.03 && Number(m[1]) < 1.08, m ? m[1] : '-');

  const push = (() => {
    const i = js.indexOf('_pushScrollConfig() {');
    if (i < 0) return '';
    const open = js.indexOf('{', i);
    let depth = 0;
    for (let k = open; k < js.length; k++) {
      if (js[k] === '{') depth++;
      else if (js[k] === '}') { depth--; if (!depth) return js.slice(open, k + 1); }
    }
    return '';
  })();
  check('②_pushScrollConfig 用折算系数判 listOverflow（缺口 B）',
        push.indexOf('FILE_CARD_HEIGHT_EST_OVER') >= 0 && /listOverflow\s*=/.test(push));
  check('②_fileListContentPx 仍存**原始估值**（拿不到真实高时作保守兜底，不能存折算值）',
        /this\._fileListContentPx\s*=\s*contentPx/.test(push));
  check('③内容高变化时作废上次实测值（缺口 A）',
        /_lastContentEst/.test(push) && /this\._fileListRealPx\s*=\s*0/.test(push));
  check('③"作废"只发生在内容确实变化时（不是每次 measure 都丢实测值）',
        /Math\.abs\(contentPx\s*-\s*this\._lastContentEst\)\s*>\s*1/.test(push));

  const refresh = (() => {
    const i = js.indexOf('_refreshListEdges(scrollTop, realContentH) {');
    if (i < 0) return '';
    const open = js.indexOf('{', i);
    let depth = 0;
    for (let k = open; k < js.length; k++) {
      if (js[k] === '{') depth++;
      else if (js[k] === '}') { depth--; if (!depth) return js.slice(open, k + 1); }
    }
    return '';
  })();
  check('④贴边判定优先用真实 scrollHeight（bindscroll），估值只兜底',
        /this\._fileListRealPx\s*\|\|\s*this\._fileListContentPx/.test(refresh));
  check('⑤只在贴边状态翻转时推送 scrollConfig（避免滚动中每帧 setData）',
        /if \(atTop === this\._listAtTop && atBottom === this\._listAtBottom\) return/.test(refresh));

  /* 接力机制本身（已有实现，防止被顺手改坏） */
  check('⑥WXS 仍有 nested / nestedAllowed 双向接力',
        /nestedAllowed/.test(wxs) && /innerCanGo/.test(wxs) && /innerCanReturn/.test(wxs));
  check('⑥WXS 贴边判定走 listAtTop / listAtBottom（由逻辑层真实滚动算出来）',
        /state\.listAtTop/.test(wxs) && /state\.listAtBottom/.test(wxs));
  check('⑥内层 scroll-view 仍上报 bindscroll（真实 scrollHeight 的唯一来源）',
        /class="file-list-scroll"[\s\S]{0,400}bindscroll="onFileListScroll"/.test(wxml));
  check('⑥onFileListScroll 把 scrollTop + scrollHeight 交给 _refreshListEdges',
        /_refreshListEdges\(\s*d\.scrollTop\s*,\s*d\.scrollHeight\s*\)/.test(js));
}

/* ---------------- 二、行为判据（真实 _pushScrollConfig / _refreshListEdges） ---------------- */
function loadIndexPage() {
  const src = fs.readFileSync(INDEX_JS, 'utf8');
  const PAGES = (() => {
    const _src = fs.readFileSync(path.join(REPO, 'mobile_apps', 'h_n_print', 'utils', 'pages.js'), 'utf8');
    const _m = { exports: {} };
    new Function('module', 'exports', _src)(_m, _m.exports);
    return _m.exports;
  })();
  let pageConfig = null;
  const storage = { token: 'tok' };
  const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map,
    parseInt, parseFloat, isNaN,
    setTimeout, clearTimeout, setInterval, clearInterval,
    require: (p) => {
      if (p.indexOf('utils/pages') >= 0) return PAGES;
      if (p.indexOf('utils/config') >= 0) return { CONFIG: { BASE_URL: 'https://example.test' } };
      if (p.indexOf('utils/request') >= 0) {
        return {
          request: () => {},
          createPollScheduler: () => ({ start() {}, stop() {}, isRunning() { return false; } }),
        };
      }
      throw new Error('unexpected require: ' + p);
    },
    Page: (cfg) => { pageConfig = cfg; },
    Component: (cfg) => { pageConfig = cfg; },
    getApp: () => ({ globalData: { isDarkMode: false, themeMode: 'light', _pageRegistry: [] } }),
    wx: {
      getStorageSync: (k) => (storage[k] !== undefined ? storage[k] : ''),
      setStorageSync: (k, v) => { storage[k] = v; },
      removeStorageSync: (k) => { delete storage[k]; },
      getWindowInfo: () => ({ windowWidth: 375, windowHeight: 800 }),
      getSystemInfoSync: () => ({ windowWidth: 375, windowHeight: 800, platform: 'devtools' }),
      createSelectorQuery: () => ({
        selectAll: () => ({ boundingClientRect() { return this; } }),
        select: () => ({ boundingClientRect() { return this; }, scrollOffset() { return this; } }),
        in() { return this; },
        exec(cb) { cb && cb([[], null]); },
      }),
      showToast: () => {},
      showModal: () => {},
      setClipboardData() {},
      nextTick: (fn) => setTimeout(fn, 0),
      setBackgroundColor() {},
      switchTab() {},
      hideLoading() {},
      showLoading() {},
    },
  };
  sandbox.global = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: 'index.js' });

  const page = Object.assign({}, pageConfig, pageConfig.methods);
  page.data = JSON.parse(JSON.stringify(pageConfig.data || {}));
  page.setData = function (patch, cb) {
    Object.keys(patch || {}).forEach((k) => { this.data[k] = patch[k]; });
    if (cb) cb();
  };
  page.getTabBar = () => null;
  page._scheduleMeasure = () => {};
  page._animateFileListHeight = () => {};
  // 滚动引擎的其余状态（_measure 里才有值，这里直接给）
  page._maxY = 0;
  page._scrollerH = 800;
  page._contentH = 900;
  page._bottomPad = 20;
  page._tabOverlayPx = 0;
  page._scrollRev = 0;
  page._listAtTop = true;
  page._listAtBottom = false;
  page._fileListRealPx = 0;
  page._fileListContentPx = 0;
  page._lastContentEst = undefined;
  return { page };
}

/* 用 excel 卡片拼内容：每张 151.2rpx，卡间距 12rpx（rpx→px 按 375 宽 = ×0.5） */
function excelCards(n) {
  return Array.from({ length: n }, (_, i) => ({ excelWarning: true, id: i + 1, name: 'f' + i + '.xlsx' }));
}
const EXCEL_RPX = 151.2;
const GAP_RPX = 12;
function estPx(n) {
  const sumRpx = n * EXCEL_RPX + (n - 1) * GAP_RPX;
  return Math.round(sumRpx * 0.5);
}

function runBehavior() {
  section('二、行为判据（真实 _pushScrollConfig / _refreshListEdges）');
  const { page } = loadIndexPage();

  /* ①缺口 B：「刚好放得下」的列表不能被判成可滚 */
  const n10 = estPx(10);                       // 810px（估值）
  const real10 = Math.round(n10 / 1.055);      // 768px（折回真实）
  page.data.selectedFiles = excelCards(10);
  page.data.fileListHeight = 780;              // 真实放得下（768 < 780），估值放不下（810 > 781）
  page._listAtTop = false;
  page._listAtBottom = false;
  page._pushScrollConfig();
  check('①缺口B：估值 810 > 视口 780，但折回真实 768 < 780 ⇒ listOverflow=false',
        page.data.scrollConfig.listOverflow === false,
        `est=${n10} real=${real10} view=780 overflow=${page.data.scrollConfig.listOverflow}`);
  check('①缺口B：不可滚时两个方向都视为贴边（WXS 立刻把滑动交给外层）',
        page.data.scrollConfig.listAtTop === true && page.data.scrollConfig.listAtBottom === true);
  check('①缺口B：不可滚时作废实测内容高', page._fileListRealPx === 0);

  /* ②内容确实超出：仍要判可滚（别把接力功能一起关掉） */
  page.data.fileListHeight = 400;
  page._listAtTop = true;
  page._listAtBottom = false;
  page._pushScrollConfig();
  check('②内容 810 ≫ 视口 400 ⇒ listOverflow=true（接力仍然可用）',
        page.data.scrollConfig.listOverflow === true);
  check('②_fileListContentPx 存原始估值（810），不是折算值',
        page._fileListContentPx === n10, String(page._fileListContentPx));

  /* ③缺口 A：内容没变时不丢实测值（否则每次 measure 都把真实高扔掉） */
  page._fileListRealPx = 5000;
  page._pushScrollConfig();
  check('③内容未变：实测内容高保留（5000）', page._fileListRealPx === 5000,
        String(page._fileListRealPx));

  /* ④缺口 A：内容变了（删了文件）⇒ 实测值必须作废，否则 maxScroll 用旧值 ⇒ atBottom 恒 false */
  page.data.fileListHeight = 780;
  page._fileListRealPx = 5000;      // 删除文件前的旧实测值
  page._lastContentEst = 5000;      // 旧内容估值
  page._fileListContentPx = 5000;
  page.data.selectedFiles = excelCards(10);
  page._pushScrollConfig();
  check('④缺口A：内容变化后实测值作废（5000 → 0）', page._fileListRealPx === 0,
        String(page._fileListRealPx));
  page._listAtTop = false;
  page._listAtBottom = false;
  page._refreshListEdges(30, 0);    // 内层已滚到底（maxScroll = 810-780 = 30）
  check('④缺口A：内层到底 ⇒ atBottom=true（WXS 才会把后续滑动交给外层）',
        page._listAtBottom === true && page.data.scrollConfig.listAtBottom === true);

  /* ⑤反证：若实测值没被作废（旧代码），同样的滚动位置 atBottom 会恒为 false = 卡死 */
  page._fileListRealPx = 5000;
  page._listAtTop = false;
  page._listAtBottom = false;
  page._refreshListEdges(30, 0);
  check('⑤反证：残留旧实测值(5000)时 atBottom=false（这就是「卡在内部列表底部」的自锁）',
        page._listAtBottom === false);

  /* ⑥真实 scrollHeight 一到就接管判定：20 张 excel（估值 1626px）视口 780 ⇒ 可滚 */
  page.data.selectedFiles = excelCards(20);
  page.data.fileListHeight = 780;
  page._listAtTop = true;
  page._listAtBottom = false;
  page._fileListRealPx = 2000;     // 真实内容高
  page._refreshListEdges(1219, 2000);
  check('⑥真实高 2000：滚到 1219 还没到底（atBottom=false）', page._listAtBottom === false,
        'atTop=' + page._listAtTop + ' atBottom=' + page._listAtBottom);
  page._refreshListEdges(1220, 2000);
  check('⑥真实高 2000：滚到 1220（maxScroll = 2000-780）⇒ atBottom=true',
        page._listAtBottom === true, 'atBottom=' + page._listAtBottom);

  /* ⑦顶部同理：scrollTop=0 ⇒ atTop=true */
  page._refreshListEdges(0, 2000);
  check('⑦滚到顶 ⇒ atTop=true（顶部反向接力）', page._listAtTop === true);

  /* ⑧贴边状态不翻转时不推送 scrollConfig（滚动中不每帧 setData） */
  const revBefore = page.data.scrollConfig.rev;
  page._refreshListEdges(0, 2000);
  page._refreshListEdges(0, 2000);
  check('⑧状态未翻转：rev 不变（不重复推送）', page.data.scrollConfig.rev === revBefore,
        `${revBefore} → ${page.data.scrollConfig.rev}`);

  /* ⑨空列表：不可滚 + 双向贴边（避免空列表吃掉手势） */
  page.data.selectedFiles = [];
  page._listAtTop = false;
  page._listAtBottom = false;
  page._pushScrollConfig();
  check('⑨空列表 ⇒ listOverflow=false 且双向贴边',
        page.data.scrollConfig.listOverflow === false
        && page.data.scrollConfig.listAtTop === true
        && page.data.scrollConfig.listAtBottom === true);
}

runStatic();
runBehavior();

console.log('\n' + '='.repeat(52));
if (failures) {
  console.log(`文件列表嵌套滚动接力验收: ${failures} 项失败 ❌`);
  process.exit(1);
}
console.log('文件列表嵌套滚动接力验收: 全部通过 ✅（真机手感仍需确认）');
process.exit(0);
