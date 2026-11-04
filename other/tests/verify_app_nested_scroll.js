/* eslint-disable */
/**
 * APP 文件列表「嵌套滚动边界交接」· 验收（2026-11 用户要求，需求④）
 *
 * 用户原话：「上传多个文件后文件列表有内部滚动，滚到底后继续滑应由外层接管（顶部同理），
 * 不是卡在内部列表底部。这问题解决很久了」。
 *
 * 本轮定位到的两条真因（详见 www/styles.css / www/app.js 注释）：
 *   1) `.file-list-scroll` 从来没写 `overscroll-behavior` ⇒ 内层触底时 Chromium 先给内层做
 *      overscroll 拉伸/回弹；而外层 `.scroller` 是 `overflow:hidden` + transform 自绘
 *      （FlingEngine），原生滚动链根本交不到它手上 ⇒ 观感「卡在列表底部」。
 *      → 内层加 `overscroll-behavior: contain`（不做 overscroll 效果、不往外交链），
 *        边界交接完全交给 FlingEngine 的贴边接管，跟手且不抖。
 *   2) 旧 `_handleTouchMove` 在「内层只剩一点余量」的那一帧整帧丢位移：内层原生吃掉 room px，
 *      外层本帧位移 0，下一帧才接管 ⇒ 边界处掉帧卡顿。
 *      → 改成余量接力：外层只补 `|ddy| - room`，本帧总位移仍 1:1。
 *      ⚠ 不能改成「本帧整帧给外层」：内层同帧还会被原生滚 room px，会双重位移跳一下。
 *
 * `html, body { overscroll-behavior: none }` 必须保留（Android WebView 整页下拉刷新/橡皮筋
 * 靠它压住），所以本轮**不**走「放开全局 none」那条路。
 *
 * 用法：node tests/verify_app_nested_scroll.js   （exit 0 = 全绿）
 *      需配合 tests/verify_nested_scroll_handoff.js（既有八条口径，必须同时绿）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..', '..');
const APP_WWW = path.join(REPO, 'mobile_apps', 'android_app', 'www');
const APP_JS = path.join(APP_WWW, 'app.js');
const APP_CSS = path.join(APP_WWW, 'styles.css');
const APP_HTML = path.join(APP_WWW, 'index.html');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}
function section(title) { console.log('\n================ ' + title + ' ================'); }

const css = fs.readFileSync(APP_CSS, 'utf8');
const html = fs.readFileSync(APP_HTML, 'utf8');
const appJs = fs.readFileSync(APP_JS, 'utf8');

/* 取某个选择器规则的声明块：同名选择器在本文件里可能出现多次（历史重复定义），
   返回全部，由调用方按内容挑选真正生效的那条。 */
function ruleBodies(text, selector) {
  const out = [];
  let from = 0;
  for (;;) {
    const idx = text.indexOf(selector, from);
    if (idx < 0) break;
    const open = text.indexOf('{', idx);
    if (open < 0) break;
    const close = text.indexOf('}', open);
    if (close < 0) break;
    out.push(text.slice(open + 1, close));
    from = idx + selector.length;
  }
  return out;
}
/* 优先取含 overflow-y 的那条（真正的滚动容器定义），没有就取最后一条 */
function ruleBody(text, selector) {
  const bodies = ruleBodies(text, selector);
  if (!bodies.length) return '';
  const scroller = bodies.filter((b) => /overflow-y\s*:/.test(b));
  return scroller.length ? scroller[scroller.length - 1] : bodies[bodies.length - 1];
}

/* ============================================================
   一、CSS：内层不产生 overscroll、不再往外抛滚动链
   ============================================================ */
section('一、CSS：.file-list-scroll 的 overscroll 契约');

const flsBody = ruleBody(css, '.file-list-scroll {');
check('① .file-list-scroll 规则存在', flsBody.length > 0);
check('② 仍保留 overflow-y: auto（内层要能原生滚）', /overflow-y\s*:\s*auto/.test(flsBody), flsBody.slice(0, 120));
check('③ 已加 overscroll-behavior-y: contain（内层不做回弹）',
      /overscroll-behavior-y\s*:\s*contain/.test(flsBody));
check('④ 已加 overscroll-behavior-x: contain（横滑也不外抛）',
      /overscroll-behavior-x\s*:\s*contain/.test(flsBody));
check('⑤ 没有把内层改成 auto/none（auto 会把链抛给 WebView 触发整页下拉刷新）',
      !/overscroll-behavior-y\s*:\s*(auto|none)/.test(flsBody),
      (flsBody.match(/overscroll-behavior-y\s*:\s*\w+/) || [''])[0]);

const htmlBody = ruleBody(css, 'html, body {');
check('⑥ 全局 overscroll-behavior: none 仍在（WebView 下拉刷新/橡皮筋闸门）',
      /overscroll-behavior\s*:\s*none/.test(htmlBody), htmlBody.replace(/\s+/g, ' ').trim());

check('⑦ index.html 的 #fileListScroll 仍带 file-list-scroll 类',
      /class="file-list-scroll"[^>]*id="fileListScroll"/.test(html) ||
      /id="fileListScroll"[^>]*class="file-list-scroll"/.test(html));

/* 生成器同步：styles.css 是 tools/wxss2web.mjs 生成出来的。模板不同步，
   任何人跑一次生成器就会把上面的 contain 全部抹掉、静默回退本次修复。 */
section('一之二、生成器模板（tools/wxss2web.mjs）同步');

const GEN = path.join(REPO, 'mobile_apps', 'android_app', 'tools', 'wxss2web.mjs');
const genRaw = fs.readFileSync(GEN, 'utf8');
const genCss = genRaw.replace(/\/\*[\s\S]*?\*\//g, ''); // 模板里也有 CSS 注释，判据只看生效声明

const genFls = ruleBody(genCss, '.file-list-scroll {');
check('⑧ 模板 .file-list-scroll 有 overscroll-behavior-y: contain',
      /overscroll-behavior-y\s*:\s*contain/.test(genFls), genFls.replace(/\s+/g, ' ').trim().slice(0, 140));
check('⑨ 模板 .file-list-scroll 有 overscroll-behavior-x: contain',
      /overscroll-behavior-x\s*:\s*contain/.test(genFls));
check('⑩ 模板 html, body 仍是 overscroll-behavior: none（WebView 闸门不能丢）',
      /overscroll-behavior\s*:\s*none/.test(ruleBody(genCss, 'html, body {')));

/* ============================================================
   二、JS：余量接力（抽取真实 FlingEngine 类体跑桩 DOM）
   ============================================================ */
function extractFlingEngine() {
  const start = appJs.indexOf('class FlingEngine {');
  const end = appJs.indexOf('function initScrollEngines()');
  if (start < 0 || end < 0 || end <= start) return null;
  return appJs.slice(start, end);
}

function loadFlingEngine() {
  const cls = extractFlingEngine();
  if (!cls) return null;
  const sandbox = {
    console, Math, JSON, Date, Object, Array, String, Number, isNaN, parseInt, parseFloat,
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
  return { FlingEngine: vm.runInContext('FlingEngine', sandbox), src: cls };
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
    if (engine._passiveNested) engine.onTouchMovePassive(ev);
    else engine.onTouchMove(ev);
  } else engine.onTouchEnd(ev);
  return ev;
}

section('二、JS：内层余量接力（container scrollHeight 1800 / clientHeight 300 ⇒ maxTop 1500）');
const loaded = loadFlingEngine();
if (!loaded) {
  check('可从 app.js 抽取 FlingEngine 类', false);
} else {
  const { FlingEngine, src } = loaded;
  check('① 可从 app.js 抽取 FlingEngine 类', typeof FlingEngine === 'function');
  check('② 交接帧不再整帧丢弃（源码含余量接力）',
        /const room\s*=\s*ddy\s*<\s*0\s*\?/.test(src) && /ddy\s*=\s*ddy\s*<\s*0\s*\?/.test(src));

  const mkEngine = () => {
    const g = new FlingEngine(fakeEl(), fakeEl(), {});
    g.maxY = 1000;
    g.scrollerH = 600;
    g.contentH = 1600;
    return g;
  };
  const innerTarget = (inner) => ({ closest: (sel) => (sel.indexOf('file-list-scroll') >= 0 ? inner : null) });

  /* 场景 A：内层只剩 8px 余量，单帧上滑 20px ⇒ 内层吃 8、外层补 12（本帧总位移 20，不掉帧） */
  let g = mkEngine();
  let inner = fakeEl({ scrollTop: 1492, scrollHeight: 1800, clientHeight: 300 }); // room = 1500-1492 = 8
  appTouch(g, 'touchstart', 300, innerTarget(inner));
  let ev = appTouch(g, 'touchmove', 280, innerTarget(inner)); // ddy = -20
  check('③ 余量那一帧外层只补剩余位移（8 归内层 + 12 归外层 = 20）',
        Math.round(g.y) === 12, 'y=' + g.y);
  check('④ 余量帧已交出接管权（_nestedScroll 清空）', g._nestedScroll === null);
  check('⑤ 余量帧仍走 passive（内层原生还会滚那 8px，不能 preventDefault）',
        ev._prevented === false);

  /* 场景 B：接管后下一帧完全 1:1 */
  ev = appTouch(g, 'touchmove', 260, innerTarget(inner)); // ddy = -20
  check('⑥ 接管后下一帧 1:1 跟手（+20 ⇒ y=32）', Math.round(g.y) === 32, 'y=' + g.y);

  /* 场景 C：累计位移与手指总行程严格相等（无跳变、无重复计数） */
  const fingerTravel = 300 - 260; // 40px
  const innerTook = 8;
  check('⑦ 总位移 = 手指行程 - 内层吃掉的部分（40 - 8 = 32）',
        Math.round(g.y) === fingerTravel - innerTook, `y=${g.y} travel=${fingerTravel} inner=${innerTook}`);
  appTouch(g, 'touchend', 260, innerTarget(inner));

  /* 场景 D：余量还够（room 30 > 20）⇒ 外层本帧完全不动 */
  g = mkEngine();
  inner = fakeEl({ scrollTop: 1470, scrollHeight: 1800, clientHeight: 300 });
  appTouch(g, 'touchstart', 300, innerTarget(inner));
  ev = appTouch(g, 'touchmove', 280, innerTarget(inner));
  check('⑧ 内层余量足够时外层完全不动', g.y === 0 && g._nestedScroll === inner, 'y=' + g.y);
  check('⑨ 余量足够时不抢滚（仍交原生）', ev._prevented === false);
  appTouch(g, 'touchend', 280, innerTarget(inner));

  /* 场景 E：顶部同理 —— 内层在 8px 处，手指下滑 20px ⇒ 内层吃 8、外层补 12（y 减小 12） */
  g = mkEngine();
  g.y = 500;
  inner = fakeEl({ scrollTop: 8, scrollHeight: 1800, clientHeight: 300 });
  appTouch(g, 'touchstart', 300, innerTarget(inner));
  ev = appTouch(g, 'touchmove', 320, innerTarget(inner)); // ddy = +20
  check('⑩ 顶部边界同样走余量接力（500 - 12 = 488）', Math.round(g.y) === 488, 'y=' + g.y);
  check('⑪ 顶部余量帧 passive 不 preventDefault', ev._prevented === false);
  appTouch(g, 'touchend', 320, innerTarget(inner));

  /* 场景 F：零余量（贴死边界）⇒ 整帧给外层，与既有口径一致 */
  g = mkEngine();
  inner = fakeEl({ scrollTop: 1500, scrollHeight: 1800, clientHeight: 300 });
  appTouch(g, 'touchstart', 300, innerTarget(inner));
  ev = appTouch(g, 'touchmove', 280, innerTarget(inner));
  check('⑫ 零余量：整帧 20px 归外层（既有口径保持）', Math.round(g.y) === 20, 'y=' + g.y);
  appTouch(g, 'touchend', 280, innerTarget(inner));

  /* 场景 F2：内层被橡皮筋拉过边界（scrollTop 1505 > maxTop 1500 ⇒ room = -5）
     ⇒ 负余量按 0 计，外层只吃本帧的 20px。若把 -5 当余量加回去，y 会变成 25（一次跳变）。 */
  g = mkEngine();
  inner = fakeEl({ scrollTop: 1505, scrollHeight: 1800, clientHeight: 300 });
  appTouch(g, 'touchstart', 300, innerTarget(inner));
  appTouch(g, 'touchmove', 280, innerTarget(inner)); // ddy = -20
  check('⑫-2 越界（负余量）不把回弹量加回位移：仍只走 20px', Math.round(g.y) === 20, 'y=' + g.y);
  appTouch(g, 'touchend', 280, innerTarget(inner));

  /* 场景 G：外层已到顶、内层还能回滚 ⇒ 交还内层，外层不动（顶部同理的既有行为） */
  g = mkEngine();
  g.y = 0;
  inner = fakeEl({ scrollTop: 600, scrollHeight: 1800, clientHeight: 300 });
  appTouch(g, 'touchstart', 300, innerTarget(inner));
  appTouch(g, 'touchmove', 280, innerTarget(inner)); // 先贴底交给外层
  appTouch(g, 'touchmove', 320, innerTarget(inner)); // 反向：外层到顶 → 交还内层
  check('⑬ 外层到顶后反向手势交还内层（外层不再位移）',
        g.y === 0 && g._nestedScroll === inner, `y=${g.y} nested=${!!g._nestedScroll}`);
  appTouch(g, 'touchend', 320, innerTarget(inner));

  /* 场景 H：ddy≈0 的重复事件不能当成贴边（否则列表滚一半就被抢走） */
  g = mkEngine();
  inner = fakeEl({ scrollTop: 700, scrollHeight: 1800, clientHeight: 300 });
  appTouch(g, 'touchstart', 300, innerTarget(inner));
  appTouch(g, 'touchmove', 300, innerTarget(inner)); // ddy = 0
  check('⑭ ddy=0 不抢内层滚动', g._nestedScroll === inner && g.y === 0, 'y=' + g.y);
  appTouch(g, 'touchend', 300, innerTarget(inner));
}

/* ============================================================
   三、回归护栏：这条改动不能顺手拆掉别的东西
   ============================================================ */
section('三、回归护栏');
check('① .file-list-scroll 仍隐藏滚动条（scrollbar-width: none）',
      /scrollbar-width\s*:\s*none/.test(flsBody));
check('② 文件列表高度动画入口仍在（print.js animateFileListHeight 未被动）',
      fs.readFileSync(path.join(APP_WWW, 'print.js'), 'utf8').includes('animateFileListHeight'));
check('③ 内层 overscroll 只在列表上收口，页面主体没有被一起改成 none',
      !/\.scroll-content\s*\{[^}]*overscroll-behavior/.test(css));
check('④ 滚轮视口也保持 contain（同一套「内层不外抛」策略）',
      /overscroll-behavior\s*:\s*contain/.test(ruleBody(css, '.wheel-viewport {')));

console.log('\n' + (failures === 0 ? '✅ 全部通过' : `❌ ${failures} 项失败`));
process.exit(failures === 0 ? 0 : 1);
