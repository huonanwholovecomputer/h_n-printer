/* eslint-disable */
/**
 * 嵌套滚动接力验收（内层文件列表 ↔ 外层页面）
 *
 * 问题：文件列表内部内容高于列表可视高时，手势由内层列表滚动；一旦滚到列表底部/顶部，
 * 手指继续同向拖动时滚动**没有交给外层页面** → 手势卡死在列表里，整个界面滚不动。
 * 反向也需要接力：外层滚到边界后手指反向拖，应交还内层列表（原生嵌套滚动的双向行为）。
 *
 * 修法（两端同口径）：
 *   1) 小程序 WXS（utils/scroll.wxs）：内层贴边状态由逻辑层 bindscroll 同步进 scrollConfig
 *      （listAtTop / listAtBottom），touchmove 里据此决定「让原生滚」还是「外层接管」，
 *      并在外层到边界时交还内层；
 *   2) APP FlingEngine（www/app.js）：读内层元素实时 scrollTop 做同样判定；并把外层
 *      `.scroller.js-scroll` 的 touch-action 从 none 改成 pan-y —— touch-action 按
 *      "元素 + 祖先交集"生效，祖先 none 会让内层真滚动容器彻底滚不动。
 *
 * 本脚本加载**两份真实引擎代码**（WXS 直接 module.exports；FlingEngine 从 app.js 抽取类体），
 * 用桩 ownerInstance / 桩 DOM 逐帧喂手势，断言接力行为。exit 0 = 全绿。
 *
 * 用法：node tests/verify_nested_scroll_handoff.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..');
const WXS = path.join(REPO, 'mobile_apps', 'h_n_print', 'utils', 'scroll.wxs');
const APP_JS = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'app.js');
const APP_CSS = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'styles.css');
const MP_JS = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'index', 'index.js');
const MP_WXML = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'index', 'index.wxml');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }

/* setData 路径写入（'a[1].b' → data.a[1].b = v），供小程序页面桩使用 */
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

/* ============================================================
   一、小程序 WXS 滚动引擎（真实 utils/scroll.wxs）
   ============================================================ */
function loadWxsEngine() {
  const src = fs.readFileSync(WXS, 'utf8');
  const sandbox = {
    console,
    Math, JSON, String, Number, Object, Array, isNaN, parseInt, parseFloat,
    getDate: () => ({ getTime: () => Date.now() }),
    module: { exports: {} },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: 'scroll.wxs' });
  return sandbox.module.exports;
}

/* 桩 ownerInstance：逻辑层同步配置 → 引擎状态；selectComponent 返回桩组件 */
function makeWxsOwner(engine, cfg) {
  const state = {};
  const moved = [];
  const inner = {
    getBoundingClientRect: () => ({ top: 200, bottom: 500, left: 0, right: 375 }),
  };
  const content = { setStyle: (s) => moved.push(s.transform) };
  const owner = {
    getState: () => state,
    requestAnimationFrame: () => 0,   // 不跑物理循环：断言只针对拖拽期的位移
    callMethod: () => {},
    selectComponent: (sel) => {
      if (sel === '.file-list-scroll') return inner;
      if (sel === '.scroll-content') return content;
      if (sel === '.logo-wrap' || sel === '.header-area') return { setStyle() {} };
      return null;
    },
    _moved: moved,
  };
  engine.onConfig(cfg, null, owner, null);
  return owner;
}

function wxsTouch(engine, owner, type, clientY, clientX) {
  const t = { identifier: 1, clientX: clientX === undefined ? 100 : clientX, clientY };
  const ev = type === 'touchend' ? { touches: [] } : { touches: [t] };
  return engine[type](ev, owner);
}

function runWxsTests() {
  section('小程序 WXS 引擎（utils/scroll.wxs 真实代码）');
  const engine = loadWxsEngine();
  check('scroll.wxs 可在桩环境加载并导出事件处理器',
        engine && typeof engine.touchstart === 'function' && typeof engine.touchmove === 'function'
        && typeof engine.onConfig === 'function');

  const CFG_BASE = {
    minY: 0, maxY: 1000, scrollerH: 600, contentH: 1600,
    listOverflow: true, listAtTop: false, listAtBottom: false,
  };

  /* ① 内层未贴边：手势全给内层，外层一动不动 */
  let owner = makeWxsOwner(engine, CFG_BASE);
  let st = owner.getState();
  wxsTouch(engine, owner, 'touchstart', 300);
  check('①触点落在列表内 → 让位内层（nested=true）', st.nested === true);
  let ret = wxsTouch(engine, owner, 'touchmove', 280);   // 手指上滑 20px
  check('①内层未贴底：touchmove 不拦截（返回 true 交原生）', ret === true, 'ret=' + ret);
  check('①内层未贴底：外层位移为 0（页面不动）', st.y === 0, 'y=' + st.y);

  /* ② 内层贴底 + 手指继续上滑 → 外层接管（本次增量生效，再叠加一次也生效） */
  engine.onConfig(Object.assign({}, CFG_BASE, { listAtBottom: true }), null, owner, null);
  ret = wxsTouch(engine, owner, 'touchmove', 260);       // 再上滑 20px
  check('②内层贴底：外层接管（touchmove 返回 false，preventDefault）', ret === false, 'ret=' + ret);
  check('②内层贴底：外层按本次增量位移 20px', Math.round(st.y) === 20, 'y=' + st.y);
  ret = wxsTouch(engine, owner, 'touchmove', 200);       // 继续上滑 60px
  check('②接管后持续跟手（60px 增量继续生效）', ret === false && Math.round(st.y) === 80, 'y=' + st.y);
  wxsTouch(engine, owner, 'touchend', 200);

  /* ③ 内层贴顶 + 手指下滑 → 外层接管（页面往上滚） */
  owner = makeWxsOwner(engine, Object.assign({}, CFG_BASE, { listAtTop: true }));
  st = owner.getState();
  // 先把页面滚到中间，便于观察"往下滚"的位移
  engine.onConfig(Object.assign({}, CFG_BASE, { listAtTop: true }), null, owner, null);
  st.y = 300;
  wxsTouch(engine, owner, 'touchstart', 300);
  ret = wxsTouch(engine, owner, 'touchmove', 320);       // 手指下滑 20px
  check('③内层贴顶 + 手指下滑：外层接管', ret === false, 'ret=' + ret);
  check('③外层按本次增量向上滚 20px（y: 300 → 280）', Math.round(st.y) === 280, 'y=' + st.y);
  wxsTouch(engine, owner, 'touchend', 320);

  /* ④ 反向接力：外层已在顶部(0)、手指继续下滑、内层未贴顶 → 交还内层 */
  owner = makeWxsOwner(engine, Object.assign({}, CFG_BASE, { listAtTop: false, listAtBottom: true }));
  st = owner.getState();
  wxsTouch(engine, owner, 'touchstart', 300);
  wxsTouch(engine, owner, 'touchmove', 280);             // 手指上滑：内层贴底 → 外层接管
  check('④接力前：外层已接管且开始位移', st.nested === false && st.y > 0, 'y=' + st.y);
  st.y = 0;                                             // 模拟外层滚回顶部
  engine.onConfig(Object.assign({}, CFG_BASE, { listAtTop: false, listAtBottom: true }), null, owner, null);
  ret = wxsTouch(engine, owner, 'touchmove', 300);       // 手指下滑
  check('④外层到顶 + 内层未贴顶：交还内层（返回 true，不拦截）', ret === true, 'ret=' + ret);
  check('④交还后外层不再位移（y 保持 0）', st.y === 0, 'y=' + st.y);
  wxsTouch(engine, owner, 'touchend', 300);

  /* ⑤ 列表不可滚（listOverflow=false）→ 外层直接接管，不让位 */
  owner = makeWxsOwner(engine, Object.assign({}, CFG_BASE, { listOverflow: false }));
  st = owner.getState();
  wxsTouch(engine, owner, 'touchstart', 300);
  check('⑤列表不可滚：不设 nested', st.nested === false);
  ret = wxsTouch(engine, owner, 'touchmove', 280);
  check('⑤列表不可滚：外层直接跟手 20px', ret === false && Math.round(st.y) === 20, 'y=' + st.y);
  wxsTouch(engine, owner, 'touchend', 280);

  /* ⑥ 触点落在列表外（如页头）→ 外层直接接管 */
  owner = makeWxsOwner(engine, CFG_BASE);
  st = owner.getState();
  wxsTouch(engine, owner, 'touchstart', 100);            // 列表 rect 200~500 之外
  check('⑥列表外起手：不设 nested', st.nested === false);
  ret = wxsTouch(engine, owner, 'touchmove', 80);
  check('⑥列表外起手：外层跟手 20px', ret === false && Math.round(st.y) === 20, 'y=' + st.y);
  wxsTouch(engine, owner, 'touchend', 80);

  /* ⑦ ddy=0 的重复事件不能误判成"贴边"而抢滚 */
  owner = makeWxsOwner(engine, CFG_BASE);
  st = owner.getState();
  wxsTouch(engine, owner, 'touchstart', 300);
  ret = wxsTouch(engine, owner, 'touchmove', 300);       // 位移 0
  check('⑦ddy=0 时仍让位内层（不抢滚）', ret === true && st.nested === true && st.y === 0, 'y=' + st.y);
  wxsTouch(engine, owner, 'touchend', 300);
}

/* ============================================================
   二、APP FlingEngine（从真实 www/app.js 抽取类体）
   ============================================================ */
function extractFlingEngine() {
  const src = fs.readFileSync(APP_JS, 'utf8');
  const start = src.indexOf('class FlingEngine {');
  const end = src.indexOf('function initScrollEngines()');
  if (start < 0 || end < 0 || end <= start) return null;
  return src.slice(start, end);
}

function loadFlingEngine() {
  const cls = extractFlingEngine();
  if (!cls) return null;
  const sandbox = {
    console,
    Math, JSON, Date, Object, Array, String, Number, isNaN, parseInt, parseFloat,
    requestAnimationFrame: () => 0,
    cancelAnimationFrame: () => {},
    getComputedStyle: () => ({ getPropertyValue: () => '0' }),
    document: { documentElement: {} },
    gestureBus: { horizontal: false },
    FLING: { releaseWindowMs: 50, maxVelocity: 8, boost: false, stopVelocity: 0.01 },
  };
  sandbox.window = sandbox;
  sandbox.addEventListener = () => {};
  sandbox.removeEventListener = () => {};
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(cls, sandbox, { filename: 'FlingEngine(extracted)' });
  const FlingEngine = vm.runInContext('FlingEngine', sandbox);
  return { FlingEngine, src: cls };
}

function fakeEl(over) {
  const el = {
    style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    _listeners: {},
    addEventListener(type, fn, opts) { this._listeners[type] = { fn, passive: !!(opts && opts.passive) }; },
    removeEventListener(type) { delete this._listeners[type]; },
    getBoundingClientRect: () => ({ height: 600, top: 0, left: 0, right: 375, bottom: 600 }),
    clientWidth: 375, scrollHeight: 1800, clientHeight: 300, scrollTop: 0,
  };
  return Object.assign(el, over || {});
}

function appTouch(engine, type, clientY, target) {
  const t = { identifier: 1, clientX: 100, clientY };
  const ev = {
    touches: type === 'touchend' ? [] : [t],
    target: target || null,
    preventDefault() { ev._prevented = true; },
    _prevented: false,
  };
  if (type === 'touchstart') engine.onTouchStart(ev);
  else if (type === 'touchmove') {
    // 真实环境里被动监听命中时走 onTouchMovePassive（引擎按当前耗时切换过监听）
    if (engine._passiveNested) engine.onTouchMovePassive(ev);
    else engine.onTouchMove(ev);
  } else engine.onTouchEnd(ev);
  return ev;
}

function runFlingTests() {
  section('APP FlingEngine（www/app.js 真实类体）');
  const loaded = loadFlingEngine();
  if (!loaded) { check('可从 app.js 抽取 FlingEngine 类', false); return; }
  const { FlingEngine, src } = loaded;
  check('可从 app.js 抽取 FlingEngine 类', typeof FlingEngine === 'function');
  check('抽取到的类确实含嵌套滚动分支', /_nestedScroll/.test(src) && /_nestedEl/.test(src));

  const mkEngine = () => {
    const el = fakeEl();
    const content = fakeEl();
    const g = new FlingEngine(el, content, {});
    g.maxY = 1000;
    g.scrollerH = 600;
    g.contentH = 1600;
    return { g, el };
  };
  // 内层滚动容器桩：closest 命中 → 引擎视为内层列表
  const innerTarget = (inner) => ({
    closest: (sel) => (sel.indexOf('file-list-scroll') >= 0 ? inner : null),
  });
  const outerTarget = { closest: () => null };

  /* ① 内层未贴边：让原生滚，外层不动 */
  let { g } = mkEngine();
  const inner = fakeEl({ scrollTop: 0, scrollHeight: 1800, clientHeight: 300 });
  let ev = appTouch(g, 'touchstart', 300, innerTarget(inner));
  check('①内层元素已记录（_nestedScroll/_nestedEl）', g._nestedScroll === inner && g._nestedEl === inner);
  ev = appTouch(g, 'touchmove', 280, innerTarget(inner));      // 手指上滑 20px，内层能继续滚
  check('①内层未贴底：不 preventDefault（交原生）', ev._prevented === false);
  check('①内层未贴底：外层位移 0', g.y === 0, 'y=' + g.y);

  /* ② 内层贴底 → 外层接管（本手势起于内层可滚列表 → 走 passive，故不调 preventDefault；
        边界处内层本就滚不动、祖先也不可滚，原生无动作，不冲突） */
  inner.scrollTop = 1500;                                      // maxTop = 1800-300 = 1500
  ev = appTouch(g, 'touchmove', 260, innerTarget(inner));
  check('②内层贴底：外层接管（passive 手势无需 preventDefault）',
        ev._prevented === false && Math.round(g.y) === 20, `y=${g.y} prevented=${ev._prevented}`);
  ev = appTouch(g, 'touchmove', 200, innerTarget(inner));
  check('②接管后持续跟手（+60px）', Math.round(g.y) === 80, 'y=' + g.y);
  appTouch(g, 'touchend', 200, innerTarget(inner));

  /* ③ 内层贴顶 + 手指下滑 → 外层接管 */
  ({ g } = mkEngine());
  const inner3 = fakeEl({ scrollTop: 0, scrollHeight: 1800, clientHeight: 300 });
  g.y = 300;
  appTouch(g, 'touchstart', 300, innerTarget(inner3));
  ev = appTouch(g, 'touchmove', 320, innerTarget(inner3));
  check('③内层贴顶 + 手指下滑：外层接管并向上滚 20px',
        Math.round(g.y) === 280, 'y=' + g.y);
  appTouch(g, 'touchend', 320, innerTarget(inner3));

  /* ④ 反向接力：外层到顶 + 手指下滑 + 内层未贴顶 → 交还内层 */
  ({ g } = mkEngine());
  const inner4 = fakeEl({ scrollTop: 1500, scrollHeight: 1800, clientHeight: 300 });
  appTouch(g, 'touchstart', 300, innerTarget(inner4));
  appTouch(g, 'touchmove', 280, innerTarget(inner4));          // 内层贴底 → 外层接管
  check('④接力前：外层已接管且位移 > 0', g._nestedScroll === null && g.y > 0, 'y=' + g.y);
  g.y = 0;                                                     // 外层回到顶部
  inner4.scrollTop = 800;                                      // 内层未贴顶 → 可以往上滚
  ev = appTouch(g, 'touchmove', 300, innerTarget(inner4));     // 手指下滑
  check('④外层到顶：交还内层（不 preventDefault）', ev._prevented === false, 'prevented=' + ev._prevented);
  check('④交还后外层不再位移', g.y === 0, 'y=' + g.y);
  check('④_nestedScroll 重新指向内层', g._nestedScroll === inner4);
  appTouch(g, 'touchend', 300, innerTarget(inner4));

  /* ⑤ ddy=0 的重复事件不能把内层踢走 */
  ({ g } = mkEngine());
  const inner5 = fakeEl({ scrollTop: 500, scrollHeight: 1800, clientHeight: 300 });
  appTouch(g, 'touchstart', 300, innerTarget(inner5));
  ev = appTouch(g, 'touchmove', 300, innerTarget(inner5));
  check('⑤ddy=0：仍让位内层且不拦住原生', g._nestedScroll === inner5 && ev._prevented === false);
  appTouch(g, 'touchend', 300, innerTarget(inner5));

  /* ⑥ 列表外起手 → 外层直接接管 */
  ({ g } = mkEngine());
  appTouch(g, 'touchstart', 300, outerTarget);
  ev = appTouch(g, 'touchmove', 280, outerTarget);
  check('⑥列表外起手：外层直接跟手 20px', ev._prevented === true && Math.round(g.y) === 20, 'y=' + g.y);
  appTouch(g, 'touchend', 280, outerTarget);

  /* ⑦ 卡顿修复：起手在内层可滚动列表 → 本手势改用 passive touchmove
     （祖先挂着非 passive touchmove 会让内层滚动只能走主线程 → 滑到列表区域很卡） */
  ({ g } = mkEngine());
  const inner7 = fakeEl({ scrollTop: 0, scrollHeight: 1800, clientHeight: 300 });
  check('⑦初始为非 passive touchmove 监听',
        !!g.el._listeners.touchmove && g.el._listeners.touchmove.passive === false,
        JSON.stringify(g.el._listeners.touchmove));
  appTouch(g, 'touchstart', 300, innerTarget(inner7));
  check('⑦起手在内层可滚列表：切成 passive 监听',
        g._passiveNested === true && !!g.el._listeners.touchmove
        && g.el._listeners.touchmove.passive === true,
        JSON.stringify(g.el._listeners.touchmove));
  ev = appTouch(g, 'touchmove', 280, innerTarget(inner7));   // 内层还能滚 → 让原生
  check('⑦passive 模式下内层能滚时不动外层、也不调 preventDefault',
        g.y === 0 && ev._prevented === false, 'y=' + g.y);
  inner7.scrollTop = 1500;                                   // 内层到底
  ev = appTouch(g, 'touchmove', 260, innerTarget(inner7));
  check('⑦passive 模式下内层到底：外层仍被驱动（边界处原生无动作，无需 cancel）',
        Math.round(g.y) === 20 && ev._prevented === false, 'y=' + g.y);
  appTouch(g, 'touchend', 260, innerTarget(inner7));
  check('⑦手势结束恢复非 passive 监听',
        g._passiveNested === false && g.el._listeners.touchmove.passive === false,
        JSON.stringify(g.el._listeners.touchmove));

  /* ⑧ 起手在内层但内层不可滚（内容不足）→ 保持非 passive（外层全权接管） */
  ({ g } = mkEngine());
  const inner8 = fakeEl({ scrollTop: 0, scrollHeight: 300, clientHeight: 300 });
  appTouch(g, 'touchstart', 300, innerTarget(inner8));
  check('⑧内层不可滚：不切 passive（外层直接用非 passive 全权接管）',
        g._passiveNested === false && g.el._listeners.touchmove.passive === false);
  ev = appTouch(g, 'touchmove', 280, innerTarget(inner8));
  check('⑧内层不可滚：外层跟手且 preventDefault 生效',
        ev._prevented === true && Math.round(g.y) === 20, 'y=' + g.y);
  appTouch(g, 'touchend', 280, innerTarget(inner8));
}

/* ============================================================
   三、小程序「内层是否到底」的判定（真实 index.js）
   ============================================================ */
function loadMiniProgramPage() {
  const src = fs.readFileSync(MP_JS, 'utf8');
  let pageConfig = null;
  const sandbox = {
    console: { log() {}, warn: console.warn, error: console.error },
    JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map,
    parseInt, parseFloat, isNaN,
    setTimeout, clearTimeout, setInterval, clearInterval,
    require: (p) => {
      if (p.indexOf('utils/config') >= 0) return { CONFIG: { BASE_URL: 'https://example.test' } };
      if (p.indexOf('utils/request') >= 0) return { request: () => {} };
      throw new Error('unexpected require: ' + p);
    },
    Page: (cfg) => { pageConfig = cfg; },
    Component: (cfg) => { pageConfig = cfg; },
    getApp: () => ({ globalData: { isDarkMode: false, themeMode: 'light' } }),
    wx: {
      getStorageSync: () => '', setStorageSync() {}, removeStorageSync() {},
      setBackgroundColor() {}, switchTab() {}, showToast() {}, showLoading() {}, hideLoading() {},
      getWindowInfo: () => ({ windowWidth: 375, windowHeight: 800 }),
      getSystemInfoSync: () => ({ windowWidth: 375, windowHeight: 800, platform: 'devtools' }),
      createSelectorQuery: () => ({
        select: () => ({ boundingClientRect() { return this; }, scrollOffset() { return this; } }),
        selectAll: () => ({ boundingClientRect() { return this; } }),
        in: function () { return this; }, exec(cb) { cb && cb([[], null, { scrollTop: 0 }]); },
      }),
      nextTick: (fn) => setTimeout(fn, 0),
      uploadFile: () => ({ abort() {}, onProgressUpdate() {} }),
      chooseMessageFile: () => {},
    },
  };
  sandbox.global = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: 'index.js' });
  const page = Object.assign({}, pageConfig, pageConfig.methods);
  page.data = JSON.parse(JSON.stringify(pageConfig.data || {}));
  page.setData = function (patch, cb) {
    Object.keys(patch || {}).forEach(k => setByPath(this.data, k, patch[k]));
    if (cb) cb();
  };
  page._scheduleMeasure = () => {};
  page.getTabBar = () => null;
  return page;
}

function runMiniProgramEdgeTests() {
  section('小程序 index.js（真实代码）：内层贴边判定');
  const page = loadMiniProgramPage();

  /* 5 张 image 卡：常量估值 312.8rpx/张（实测仅 290.3），估值内容高 > 真实 → 列表可滚且被 85vh 封顶 */
  const mkFile = (i) => ({
    _uid: i + 1, name: 'p' + i + '.png', size: 2048, path: '/tmp/p' + i + '.png', sizeDisplay: '2.0',
    fileId: 'F' + i, uploading: false, progress: 100, failed: false, copies: 1, pageRange: '',
    rangeLines: [{ value: '', error: '' }], duplex: 'off', imageOrientation: 'auto',
    entering: false, removing: false, excelWarning: false, unsupportedFormat: false,
    isImage: true, pageCount: 1, pageCountStatus: '', singlePage: true,
  });
  page.data.selectedFiles = [0, 1, 2, 3, 4].map(mkFile);
  page._fileListPx = 0;
  page._recalcFileListHeight();
  // 结束高度补间（真实使用中用户滚动前动画已播完）：直接落到位再推配置
  const settleHeight = () => {
    if (page._fileListTween) { clearTimeout(page._fileListTween); page._fileListTween = null }
    page.data.fileListHeight = page._fileListPx;
    page._pushScrollConfig();
  };
  settleHeight();

  const viewH = page.data.fileListHeight;
  const estContent = page._fileListContentPx;
  const estMax = Math.max(0, estContent - viewH);
  check('列表可滚（估值内容高 > 可视高）', page.data.scrollConfig.listOverflow === true,
        `contentPx=${estContent} viewH=${viewH}`);
  check('估值 maxScroll > 0（说明确实用到了内容高估值）', estMax > 10, 'estMax=' + estMax);

  /* 真实内容高比估值小 7%（实测偏差）→ 真实 maxScroll 明显小于估值 maxScroll */
  const realContent = Math.round(estContent * 0.93);
  const realMax = Math.max(0, realContent - viewH);
  check('真实 maxScroll 比估值小（正是"滚到底也判不出到底"的根因）', realMax < estMax - 5,
        `realMax=${realMax} estMax=${estMax}`);

  /* ① 修复后：滚到真实底部（带真实 scrollHeight）→ 判定到底，并把 listAtBottom 推给 WXS */
  page._listAtTop = true;
  page._listAtBottom = false;
  page.onFileListScroll({ detail: { scrollTop: realMax, scrollHeight: realContent, scrollWidth: 0 } });
  check('①滚到真实底部（用 scrollHeight 判定）→ listAtBottom=true',
        page._listAtBottom === true, `listAtBottom=${page._listAtBottom} scrollTop=${realMax}`);
  check('①scrollConfig 已同步 listAtBottom=true（WXS 据此把滚动交给外层）',
        page.data.scrollConfig.listAtBottom === true);

  /* ② 反证：不带真实 scrollHeight（退回常量估值）时，同一 scrollTop 判不出到底 —— 旧实现的 bug */
  page._listAtTop = true;
  page._listAtBottom = false;
  page._fileListRealPx = 0;
  page.onFileListScroll({ detail: { scrollTop: realMax, scrollHeight: 0, scrollWidth: 0 } });
  check('②反证：只用常量估值 → 同一位置 listAtBottom 仍为 false（旧行为，接力失效）',
        page._listAtBottom === false, 'estMax=' + estMax + ' scrollTop=' + realMax);

  /* ③ 反过来：滚到顶部（scrollTop=0）→ listAtTop=true，用于"外层到顶交还内层" */
  page._listAtTop = false;
  page._listAtBottom = true;
  page.onFileListScroll({ detail: { scrollTop: 0, scrollHeight: realContent } });
  check('③滚到顶部 → listAtTop=true / listAtBottom=false',
        page._listAtTop === true && page._listAtBottom === false);

  /* ④ 把推出来的 config 喂给真实 WXS 引擎：内层贴底后继续上滑 → 外层接管 */
  page.onFileListScroll({ detail: { scrollTop: realMax, scrollHeight: realContent } });  // 回到"贴底"状态
  check('④喂 WXS 前配置确为 listAtBottom=true', page.data.scrollConfig.listAtBottom === true);
  const engine = loadWxsEngine();
  const owner = makeWxsOwner(engine, Object.assign({}, page.data.scrollConfig, {
    minY: 0, maxY: 600, scrollerH: 800, contentH: 1400,
  }));
  const st = owner.getState();
  wxsTouch(engine, owner, 'touchstart', 300);
  const ret = wxsTouch(engine, owner, 'touchmove', 270);
  check('④端到端：配置带 listAtBottom=true → 内层触点起手也能让外层接管',
        ret === false && st.y > 0, `ret=${ret} y=${st.y}`);
  wxsTouch(engine, owner, 'touchend', 270);

  /* ⑤ 列表不可滚（只 1 个文件）→ 两个方向都视为贴边，WXS 立刻把滚动交给外层 */
  page.data.selectedFiles = [mkFile(0)];
  page._fileListPx = 0;
  page._recalcFileListHeight();
  settleHeight();
  check('⑤内容不足一屏：listOverflow=false 且 listAtTop/listAtBottom 都为 true',
        page.data.scrollConfig.listOverflow === false
        && page.data.scrollConfig.listAtTop === true && page.data.scrollConfig.listAtBottom === true,
        JSON.stringify({ overflow: page.data.scrollConfig.listOverflow, top: page.data.scrollConfig.listAtTop, bottom: page.data.scrollConfig.listAtBottom }));
}

/* ============================================================
   四、接线核对（逻辑层/模板/样式的配套改动）
   ============================================================ */
function runWiringChecks() {
  section('接线核对（小程序逻辑层 + 两端 touch-action）');
  const mpJs = fs.readFileSync(MP_JS, 'utf8');
  const mpWxml = fs.readFileSync(MP_WXML, 'utf8');
  const appJs = fs.readFileSync(APP_JS, 'utf8');
  const css = fs.readFileSync(APP_CSS, 'utf8');

  check('小程序：scroll-view 绑 bindscroll 同步内层边界',
        /class="file-list-scroll"[\s\S]{0,200}bindscroll="onFileListScroll"/.test(mpWxml));
  check('小程序：scrollConfig 带 listAtTop/listAtBottom',
        /listAtTop: !!this\._listAtTop/.test(mpJs) && /listAtBottom: !!this\._listAtBottom/.test(mpJs));
  check('小程序：onFileListScroll → _refreshListEdges（仅翻转时 setData）',
        /onFileListScroll\(e\) \{/.test(mpJs) && /_refreshListEdges\(/.test(mpJs));
  check('小程序：_measure 同步查询内层 scrollOffset（内容增减后边界不失效）',
        /\.file-list-scroll'\)\.scrollOffset\(\)/.test(mpJs));
  check('小程序：贴边判定用滚动事件的真实 scrollHeight（不再只靠常量估值）',
        /_refreshListEdges\(d\.scrollTop, d\.scrollHeight\)/.test(mpJs)
        && /this\._fileListRealPx = real/.test(mpJs));
  check('小程序：内容不足一屏时两边都置贴边（外层立即接管）',
        /if \(!listOverflow\) \{[\s\S]{0,200}this\._listAtBottom = true/.test(mpJs));
  check('小程序：初始 config/state 带边界字段',
        /listOverflow: false, listAtTop: true, listAtBottom: false/.test(mpJs)
        && /state\.listAtTop = true;/.test(fs.readFileSync(WXS, 'utf8')));

  check('APP：.scroller.js-scroll 的 touch-action 不再是 none（改 pan-y）',
        /\.scroller\.js-scroll \{[\s\S]{0,200}touch-action: pan-y;/.test(css)
        && !/\.scroller\.js-scroll \{[\s\S]{0,200}touch-action: none;/.test(css));
  check('APP：FlingEngine 内联 touchAction 同步改 pan-y',
        /this\.el\.style\.touchAction = 'pan-y'/.test(appJs));
  check('APP：内层滚动容器仍保持原生纵向滚动（.file-list-scroll/.wheel-viewport）',
        /\.file-list-scroll \{[\s\S]{0,120}overflow-y: auto;/.test(css)
        && /\.wheel-viewport \{[\s\S]{0,160}overflow-y: auto;/.test(css));
}

/* ============================================================ */
runWxsTests();
runFlingTests();
runMiniProgramEdgeTests();
runWiringChecks();
console.log('\n' + '='.repeat(52));
console.log('嵌套滚动接力验收:', failures === 0 ? '全部通过 ✅' : `存在 ${failures} 项失败 ❌`);
process.exit(failures ? 1 : 0);
