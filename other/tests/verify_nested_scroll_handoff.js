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

const REPO = path.resolve(__dirname, '..', '..');

// utils/pages.js —— 小程序「页码范围 → 有效页数」的**唯一实现**（index.js 现在 require 它）。
// 这里必须**真的把它装进来**：垫片只认 utils/config / utils/request 时，
// require('../../utils/pages') 会落到下面的 throw ⇒ 脚本**假红**（存根没跟上实现）。
const __PAGES_EXPORTS = (() => {
  // **失败关闭**：读不到文件、或没导出 countPagesInRange，就直接抛。
  // （若这里静默返回 {}，pages.js 被改坏时本脚本可能悄悄变绿 = 守卫失去意义。）
  const _src = fs.readFileSync(
    path.join(REPO, 'mobile_apps', 'h_n_print', 'utils', 'pages.js'), 'utf8');
  const _m = { exports: {} };
  new Function('module', 'exports', _src)(_m, _m.exports);
  if (typeof _m.exports.countPagesInRange !== 'function') {
    throw new Error('utils/pages.js 未导出 countPagesInRange（垫片拿不到口径，不能静默放行）');
  }
  return _m.exports;
})();

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
  // 可控时钟：惯性/回弹是逐帧循环，必须能推进时间才能断言（否则 dt 恒 0 永远跑不完）
  const clock = { t: 1700000000000 };
  const sandbox = {
    console,
    Math, JSON, String, Number, Object, Array, isNaN, parseInt, parseFloat,
    getDate: () => ({ getTime: () => clock.t }),
    requestAnimationFrame: () => 0,
    module: { exports: {} },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: 'scroll.wxs' });
  const exp = sandbox.module.exports;
  exp._clock = clock;
  return exp;
}

/* 内层滚动框桩：box（裁剪框）+ track（内容轨）。
 * track 的 rect 跟着 setStyle 写进来的 translateY 走 —— 这样「新开一次手势重新量」时
 * 量到的起点就是上一次手势滚到的位置，与真机一致。 */
function makeBox(top, height, trackH, y0) {
  const body = {
    _y: y0 || 0,
    setStyle(s) {
      const m = s && typeof s.transform === 'string'
        && /translateY\((-?[\d.]+)px\)/.exec(s.transform);
      if (m) body._y = -parseFloat(m[1]);
    },
    getBoundingClientRect: () => ({
      top: top - body._y, bottom: top - body._y + trackH, height: trackH,
      left: 0, right: 375, width: 375,
    }),
  };
  return {
    getBoundingClientRect: () => ({
      top, bottom: top + height, height, left: 0, right: 375, width: 375,
    }),
    // 轨道选择器随页面不同（.detail-files-track / .file-list-track），桩按后缀认
    selectComponent: (sel) => (/track$/.test(String(sel || '')) ? body : null),
    _body: body,
  };
}

/* 桩 ownerInstance：逻辑层同步配置 → 引擎状态；selectAllComponents 返回内层框桩 */
function makeWxsOwner(engine, cfg, boxes) {
  const state = {};
  const rendered = [];
  const ns = (cfg && cfg.nestedSel) || '.detail-files-scroll';   // 本页下发的内层框选择器
  const content = { setStyle: (s) => rendered.push(s.transform) };
  const owner = {
    getState: () => state,
    requestAnimationFrame: (cb) => (owner._rafs.push(cb), owner._rafs.length),
    callMethod: () => {},
    selectComponent: (sel) => {
      if (sel === '.scroll-content') return content;
      if (sel === '.logo-wrap' || sel === '.header-area') return { setStyle() {} };
      if (sel === ns) return (boxes || [])[0] || null; // 退路：单框
      return null;
    },
    selectAllComponents: (sel) => (sel === ns ? (boxes || []) : []),
    _rendered: rendered,
    _rafs: [],
  };
  engine.onConfig(cfg, null, owner, null);
  return owner;
}

function wxsTouch(engine, owner, type, clientY, clientX) {
  const t = { identifier: 1, clientX: clientX === undefined ? 100 : clientX, clientY };
  const ev = type === 'touchend' ? { touches: [] } : { touches: [t] };
  return engine[type](ev, owner);
}

/* 逐帧推进物理循环（惯性/回弹都挂在 owner.requestAnimationFrame 上） */
function runFrames(owner, engine, n) {
  for (let i = 0; i < n && owner._rafs.length; i++) {
    engine._clock.t += 16;                     // 一帧 16ms，与真机一致
    const cbs = owner._rafs;
    owner._rafs = [];
    cbs.forEach((cb) => cb());
  }
}

function runWxsTests() {
  section('小程序 WXS 引擎（utils/scroll.wxs 真实代码）：内层框先吃、余量给外层');
  const engine = loadWxsEngine();
  check('scroll.wxs 可在桩环境加载并导出事件处理器',
        engine && typeof engine.touchstart === 'function' && typeof engine.touchmove === 'function'
        && typeof engine.onConfig === 'function');

  const CFG = {
    minY: 0, maxY: 1000, scrollerH: 600, contentH: 1600,
    nestedSel: '.detail-files-scroll', nestedTrackSel: '.detail-files-track',
  };
  const mk = (boxes) => makeWxsOwner(engine, CFG, boxes);

  /* ① 内层滚得动：本帧整帧给内层，外层一动不动（用户要的「先滚列表」） */
  let b = makeBox(200, 300, 800, 120);          // 框高 300、内容轨 800 ⇒ 行程 500，当前偏 120
  let o = mk([b]);
  let st = o.getState();
  wxsTouch(engine, o, 'touchstart', 300);
  check('①触点落在内层框内 → 量出行程与当前位置',
        !!st.nb && st.nb.maxY === 500 && Math.round(st.nb.y) === 120,
        st.nb ? `maxY=${st.nb.maxY} y=${st.nb.y}` : 'nb=null');
  let ret = wxsTouch(engine, o, 'touchmove', 280);            // 手指上滑 20px
  check('①内层吃得下：不拦原生、不动外层（return true）', ret === true && st.y === 0, `ret=${ret} y=${st.y}`);
  check('①内层吃得下：内层轨自己 translateY 了 20px',
        Math.round(b._body._y) === 140, 'trackY=' + b._body._y);

  /* ② 贴底当帧：内层只吃剩下的 10px，余量 10px 同帧交给外层 */
  let b2 = makeBox(200, 300, 800, 490);
  o = mk([b2]); st = o.getState();
  wxsTouch(engine, o, 'touchstart', 300);
  ret = wxsTouch(engine, o, 'touchmove', 280);
  check('②内层贴底：余量交给外层（只给余量，不是整帧）',
        ret === false && Math.round(st.y) === 10 && Math.round(st.nb.y) === 500,
        `ret=${ret} y=${st.y} nbY=${st.nb.y}`);
  ret = wxsTouch(engine, o, 'touchmove', 260);               // 内层已到底，整帧给外层
  check('②继续上滑：外层持续跟手', ret === false && Math.round(st.y) === 30, 'y=' + st.y);

  /* ③ 反向：外层在中间时手指下滑 → 外层自己滚（内层不动） */
  st.y = 30;
  ret = wxsTouch(engine, o, 'touchmove', 280);               // 手指下滑 20px
  check('③外层不在顶：下滑由外层自己吃', ret === false && Math.round(st.y) === 10, 'y=' + st.y);

  /* ④ 外层到顶 + 继续下滑 → 交还内层（双向接力） */
  st.y = 0;
  ret = wxsTouch(engine, o, 'touchmove', 300);
  check('④外层到顶：位移交还内层（return true，外层不动）',
        ret === true && st.y === 0 && Math.round(st.nb.y) === 480,
        `ret=${ret} y=${st.y} nbY=${st.nb.y}`);
  wxsTouch(engine, o, 'touchend', 300);

  /* ⑤ 慢速拖动（单帧 0.3px）也必须先给内层：旧实现按 >0.5px 过滤，整帧会漏进外层 */
  let b5 = makeBox(200, 300, 800, 120);
  o = mk([b5]); st = o.getState();
  wxsTouch(engine, o, 'touchstart', 300);
  ret = wxsTouch(engine, o, 'touchmove', 290);              // 先过 5px 方向锁
  check('⑤起手 10px 由内层吃掉', ret === true && Math.round(b5._body._y) === 130, 'trackY=' + b5._body._y);
  ret = wxsTouch(engine, o, 'touchmove', 289.7);            // 单帧 0.3px
  check('⑤亚像素帧（0.3px）仍由内层消费，外层不动',
        ret === true && st.y === 0 && Math.abs(b5._body._y - 130.3) < 0.01,
        `ret=${ret} y=${st.y} trackY=${b5._body._y}`);
  wxsTouch(engine, o, 'touchend', 289.7);

  /* ⑥ 内层不可滚（内容没超框）→ 量不出框 → 外层直接接管 */
  o = mk([makeBox(200, 300, 300, 0)]); st = o.getState();
  wxsTouch(engine, o, 'touchstart', 300);
  check('⑥内层不可滚：不接管手势', st.nb === null);
  ret = wxsTouch(engine, o, 'touchmove', 280);
  check('⑥内层不可滚：外层直接跟手 20px', ret === false && Math.round(st.y) === 20, 'y=' + st.y);
  wxsTouch(engine, o, 'touchend', 280);

  /* ⑦ 触点落在框外（页头/卡片标题）→ 外层直接接管 */
  o = mk([makeBox(200, 300, 800, 120)]); st = o.getState();
  wxsTouch(engine, o, 'touchstart', 100);
  check('⑦框外起手：不接管', st.nb === null);
  ret = wxsTouch(engine, o, 'touchmove', 80);
  check('⑦框外起手：外层跟手 20px', ret === false && Math.round(st.y) === 20, 'y=' + st.y);
  wxsTouch(engine, o, 'touchend', 80);

  /* ⑧ ddy=0 的重复事件：不推进、不抢滚、不拦原生 */
  o = mk([makeBox(200, 300, 800, 120)]); st = o.getState();
  wxsTouch(engine, o, 'touchstart', 300);
  ret = wxsTouch(engine, o, 'touchmove', 300);
  check('⑧ddy=0：不误判成贴边、外层不动、不拦原生', ret === true && st.y === 0, `ret=${ret} y=${st.y}`);
  wxsTouch(engine, o, 'touchend', 300);

  /* ⑨ me 页可同时展开多张订单卡：触点命中哪个框就滚哪个，绝不串卡 */
  const bA = makeBox(100, 200, 600, 50);
  const bB = makeBox(400, 200, 600, 0);
  o = mk([bA, bB]); st = o.getState();
  wxsTouch(engine, o, 'touchstart', 500);                    // 落在第二个框里
  check('⑨两个可滚框：按触点选中第二个', !!st.nb && st.nb.idx === 1, st.nb ? 'idx=' + st.nb.idx : 'nb=null');
  wxsTouch(engine, o, 'touchmove', 480);
  check('⑨只滚被命中的那个框（另一个原地不动）',
        Math.round(bB._body._y) === 20 && Math.round(bA._body._y) === 50,
        `A=${bA._body._y} B=${bB._body._y}`);
  wxsTouch(engine, o, 'touchend', 480);

  /* ⑩ 抬手后：谁吃下最后一帧谁走惯性 —— 内层自己滑，外层不动 */
  let b10 = makeBox(200, 300, 800, 100);
  o = mk([b10]); st = o.getState();
  const drag = (y) => { engine._clock.t += 16; return wxsTouch(engine, o, 'touchmove', y); };
  wxsTouch(engine, o, 'touchstart', 300);
  drag(280); drag(260); drag(240);
  wxsTouch(engine, o, 'touchend', 240);
  check('⑩抬手：惯性归内层（nbRun=true、外层速度清零）',
        st.nbRun === true && st.vel === 0, `nbRun=${st.nbRun} vel=${st.vel}`);
  runFrames(o, engine, 2);                     // 只跑前两帧：内层还有 340px 行程
  check('⑩惯性开头只动内层（轨继续前进、外层位移仍是 0）',
        b10._body._y > 160 && st.y === 0, `trackY=${b10._body._y} y=${st.y}`);
  runFrames(o, engine, 120);                   // 跑到停：撞底后余速交外层（见 ⑪）

  /* ⑪ 内层惯性撞到底 → 剩下的速度交给外层（不做"滚到边就地停死"） */
  let b11 = makeBox(200, 300, 800, 480);        // 离底只剩 20px
  o = mk([b11]); st = o.getState();
  const drag11 = (y) => { engine._clock.t += 16; return wxsTouch(engine, o, 'touchmove', y); };
  wxsTouch(engine, o, 'touchstart', 300);
  drag11(280); drag11(260);
  wxsTouch(engine, o, 'touchend', 260);
  runFrames(o, engine, 120);
  check('⑪内层惯性撞底：余速把外层带起来（外层位移 > 0）',
        st.y > 0, 'y=' + st.y);
  check('⑪交棒后内层停在底（钳在 maxY）',
        Math.round(st.nb.y) === 500, 'nbY=' + st.nb.y);
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

  /* ② 内层已贴底（起手时就在底）→ 手指继续上滑，外层接管
        （本手势起于内层可滚列表 → 走 passive，故不调 preventDefault；
        边界处内层本就滚不动、祖先也不可滚，原生无动作，不冲突） */
  ({ g } = mkEngine());
  const inner2 = fakeEl({ scrollTop: 1500, scrollHeight: 1800, clientHeight: 300 });  // maxTop=1500
  appTouch(g, 'touchstart', 300, innerTarget(inner2));
  ev = appTouch(g, 'touchmove', 280, innerTarget(inner2));
  check('②内层贴底：外层接管（passive 手势无需 preventDefault）',
        ev._prevented === false && Math.round(g.y) === 20, `y=${g.y} prevented=${ev._prevented}`);
  ev = appTouch(g, 'touchmove', 220, innerTarget(inner2));
  check('②接管后持续跟手（+60px）', Math.round(g.y) === 80, 'y=' + g.y);
  appTouch(g, 'touchend', 220, innerTarget(inner2));

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
  const inner7 = fakeEl({ scrollTop: 1480, scrollHeight: 1800, clientHeight: 300 });
  check('⑦初始为非 passive touchmove 监听',
        !!g.el._listeners.touchmove && g.el._listeners.touchmove.passive === false,
        JSON.stringify(g.el._listeners.touchmove));
  appTouch(g, 'touchstart', 300, innerTarget(inner7));
  check('⑦起手在内层可滚列表：切成 passive 监听',
        g._passiveNested === true && !!g.el._listeners.touchmove
        && g.el._listeners.touchmove.passive === true,
        JSON.stringify(g.el._listeners.touchmove));
  ev = appTouch(g, 'touchmove', 280, innerTarget(inner7));   // 内层还剩 20px：让原生
  check('⑦passive 模式下内层能滚时不动外层、也不调 preventDefault',
        g.y === 0 && ev._prevented === false, 'y=' + g.y);
  ev = appTouch(g, 'touchmove', 250, innerTarget(inner7));   // 内层已到底：外层接管
  check('⑦passive 模式下内层到底：外层仍被驱动（边界处原生无动作，无需 cancel）',
        Math.round(g.y) === 30 && ev._prevented === false, 'y=' + g.y);
  appTouch(g, 'touchend', 250, innerTarget(inner7));
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
      if (p.indexOf('utils/pages') >= 0) return __PAGES_EXPORTS;
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
  section('小程序三页：内层框结构 + 选择器下发（真实源码）');
  const MP = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages');
  const rd = (p) => fs.readFileSync(path.join(MP, p), 'utf8');
  const meWxml = rd('me/me.wxml'), meWxss = rd('me/me.wxss'), meJs = rd('me/me.js');
  const uoWxml = rd('user-orders/user-orders.wxml'), uoWxss = rd('user-orders/user-orders.wxss');
  const uoJs = rd('user-orders/user-orders.js');

  check('me：文件列表是 view 框 + view 轨（不再是原生 scroll-view）',
        /class="detail-files-scroll"[\s\S]{0,200}?<view class="detail-files-track"/.test(meWxml)
        && !/<scroll-view[^>]*detail-files-scroll/.test(meWxml));
  check('me：不再用 catchtouchmove 兜内层（手势全交给 WXS）',
        !/scroll-view[^>]*catchtouchmove="noop"/.test(meWxml)
        && !/catchtouchmove="noop"[^>]*detail-files/.test(meWxml));
  check('me.wxss：内层框 overflow:hidden（由 track transform 驱动）',
        /\.detail-files-scroll \{[\s\S]{0,160}?overflow: hidden;/.test(meWxss));
  check('me.js：scrollConfig 下发 nestedSel/nestedTrackSel',
        /nestedSel: '\.detail-files-scroll'/.test(meJs)
        && /nestedTrackSel: '\.detail-files-track'/.test(meJs));

  check('user-orders：同构（view 框 + view 轨、无 scroll-view）',
        /class="detail-files-scroll"[\s\S]{0,200}?<view class="detail-files-track"/.test(uoWxml)
        && !/<scroll-view[^>]*detail-files-scroll/.test(uoWxml));
  check('user-orders.wxss：内层框 overflow:hidden',
        /\.detail-files-scroll \{[\s\S]{0,160}?overflow: hidden;/.test(uoWxss));
  check('user-orders.js：scrollConfig 下发 nestedSel/nestedTrackSel',
        /nestedSel: '\.detail-files-scroll'/.test(uoJs)
        && /nestedTrackSel: '\.detail-files-track'/.test(uoJs));

  /* 首页（打印页）文件列表：同样是 view 框 + view 轨，高度仍由 fileListHeight 封顶 */
  const page = loadMiniProgramPage();
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
  if (page._fileListTween) { clearTimeout(page._fileListTween); page._fileListTween = null; }
  page.data.fileListHeight = page._fileListPx;
  page._pushScrollConfig();

  const mpJs = fs.readFileSync(MP_JS, 'utf8');
  check('index.js：scrollConfig 下发 .file-list-scroll / .file-list-track',
        page.data.scrollConfig.nestedSel === '.file-list-scroll'
        && page.data.scrollConfig.nestedTrackSel === '.file-list-track',
        JSON.stringify({ s: page.data.scrollConfig.nestedSel, t: page.data.scrollConfig.nestedTrackSel }));
  check('index.js：不再往 WXS 推 listOverflow/listAtTop/listAtBottom（贴边逐帧量，不靠逻辑层）',
        page.data.scrollConfig.listOverflow === undefined
        && page.data.scrollConfig.listAtTop === undefined
        && page.data.scrollConfig.listAtBottom === undefined);
  check('index.js：删掉了 bindscroll 那套贴边同步（onFileListScroll/_refreshListEdges）',
        !/onFileListScroll/.test(mpJs) && !/_refreshListEdges/.test(mpJs));
  check('index.js：不再查内层 scrollOffset（选择器查询里只剩外层几何）',
        !/file-list-scroll'\)\.scrollOffset\(\)/.test(mpJs));
  check('index.js：作废了估算高折算常量（贴边不再靠估值）',
        !/FILE_CARD_HEIGHT_EST_OVER/.test(mpJs));
  check('index：列表高度仍有上限（内容超出才需要内层滚）',
        page.data.fileListHeight > 0 && page._fileListPx === page.data.fileListHeight,
        `h=${page.data.fileListHeight} px=${page._fileListPx}`);

  /* 端到端：首页真实 config 喂真实 WXS → 内层先滚、外层不动 */
  const engine = loadWxsEngine();
  const h = page.data.fileListHeight;
  const box = makeBox(200, h, h + 300, 0);
  const owner = makeWxsOwner(engine, Object.assign({}, page.data.scrollConfig, {
    minY: 0, maxY: 600, scrollerH: 800, contentH: 1400,
  }), [box]);
  const st = owner.getState();
  wxsTouch(engine, owner, 'touchstart', 300);
  check('端到端：首页 config 能被 WXS 认出来（量到行程 300px）', !!st.nb && st.nb.maxY === 300,
        st.nb ? 'maxY=' + st.nb.maxY : 'nb=null');
  const ret = wxsTouch(engine, owner, 'touchmove', 280);
  check('端到端：内层吃得下时页面不动', ret === true && st.y === 0 && Math.round(box._body._y) === 20,
        `ret=${ret} y=${st.y} trackY=${box._body._y}`);
  wxsTouch(engine, owner, 'touchend', 280);
}

/* ============================================================
   四、接线核对（模板/样式/引擎的配套改动）
   ============================================================ */
function runWiringChecks() {
  section('接线核对（WXS 契约 + APP 引擎）');
  const wxs = fs.readFileSync(WXS, 'utf8');
  const appJs = fs.readFileSync(APP_JS, 'utf8');
  const css = fs.readFileSync(APP_CSS, 'utf8');

  check('WXS：不再消费 listOverflow/listAtTop/listAtBottom',
        !/newValue\.listOverflow/.test(wxs) && !/newValue\.listAtTop/.test(wxs)
        && !/newValue\.listAtBottom/.test(wxs));
  check('WXS：onConfig 消费 nestedSel/nestedTrackSel',
        /newValue\.nestedSel/.test(wxs) && /newValue\.nestedTrackSel/.test(wxs));
  check('WXS：按触点逐个量框（selectAllComponents，多卡不串）',
        /selectAllComponents/.test(wxs) && /function measureNested/.test(wxs));
  check('WXS：内外层位移同帧结算（内层吃剩的余量给外层）',
        /var rest = need - take;/.test(wxs) && /ddy = -rest;/.test(wxs));
  check('WXS：内层惯性撞边把余速交给外层',
        /function startNbDecel/.test(wxs) && /state\.vel = carry/.test(wxs));

  check('APP：.scroller.js-scroll 的 touch-action 不再是 none（改 pan-y）',
        /\.scroller\.js-scroll \{[\s\S]{0,200}touch-action: pan-y;/.test(css)
        && !/\.scroller\.js-scroll \{[\s\S]{0,200}touch-action: none;/.test(css));
  check('APP：FlingEngine 内联 touchAction 同步改 pan-y',
        /this\.el\.style\.touchAction = 'pan-y'/.test(appJs));
  check('APP：内层滚动容器保持原生纵向滚动（详情列表也带 overscroll-behavior）',
        /\.file-list-scroll \{[\s\S]{0,120}overflow-y: auto;/.test(css)
        && /\.detail-files-scroll \{[\s\S]{0,200}overflow-y: auto;/.test(css));
  check('APP：引擎用手势内累计估计代替实时 scrollTop（消除贴边死区）',
        /this\._nestEst/.test(appJs) && /const est = Math\.max\(0, Math\.min\(maxTop, this\._nestEst\)\);/.test(appJs));
  check('APP：慢速拖动（<0.5px/帧）也推进估计值',
        /if \(ddy !== 0\) \{/.test(appJs));
}


/* ============================================================ */
runWxsTests();
runFlingTests();
runMiniProgramEdgeTests();
runWiringChecks();
console.log('\n' + '='.repeat(52));
console.log('嵌套滚动接力验收:', failures === 0 ? '全部通过 ✅' : `存在 ${failures} 项失败 ❌`);
process.exit(failures ? 1 : 0);
