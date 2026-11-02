/* eslint-disable */
/**
 * APP 时间滚轮「阻力感太大」· 验收（2026-11 需求③）
 *
 * 用户原话：「APP的时间选择器行为正常，但是滑动的时候阻力感太大」。
 *
 * 定位到的根因（不是几何，几何我算过是自洽的）：
 *   `styles.css` 的 `.wheel-viewport { scroll-snap-type: y mandatory }` +
 *   `.wheel-item { scroll-snap-align: center }` —— Chromium 在**每次松手**时就把惯性收敛到
 *   最近的吸附点，一次手势几乎只走一格，于是"轻扫 = 一格"、"越划越黏"。
 *   （全仓从未出现 `scroll-snap-stop: always`，它不是原因；`touch-action` 也没问题。）
 *
 * 改法：彻底不做 CSS snap（拖动/惯性 100% 原生、1:1 跟手），停下 140ms 后由 JS
 * 平滑吸附到最近一格中央；`readWheelIndex()` / `onChange` 契约一个字没动。
 *
 * 验收：静态锁 CSS 契约 + 用桩 DOM 跑真实 buildWheel（拖动中不吸附、松手后吸附一格、
 *       吸附目标 = 最近项中心、onChange 仍按最近索引回调）。
 *
 * 用法：node tests/verify_app_wheel.js   （exit 0 = 全绿）
 *      手感（跟手程度、吸附动画观感）**需真机确认**。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..');
const APP_WWW = path.join(REPO, 'mobile_apps', 'android_app', 'www');
const APP_CSS = path.join(APP_WWW, 'styles.css');
const APP_PRINT = path.join(APP_WWW, 'print.js');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}
function section(title) { console.log('\n================ ' + title + ' ================'); }

const rawCss = fs.readFileSync(APP_CSS, 'utf8');
const css = rawCss.replace(/\/\*[\s\S]*?\*\//g, ''); // 注释里会引用旧写法，判据要只看生效声明
const printJs = fs.readFileSync(APP_PRINT, 'utf8');
/* 判据只看"生效代码"：注释里会引用旧写法（scroll-snap-type: y mandatory 之类），
   所以把整行注释去掉再判断，避免注释把自己的判据判红。 */
const printJsCode = printJs.split('\n')
  .map((l) => (/^\s*(\/\/|\*|\/\*)/.test(l) ? '' : l))
  .join('\n');

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
function ruleBody(text, selector, prefer) {
  const bodies = ruleBodies(text, selector);
  if (!bodies.length) return '';
  if (prefer) {
    const hit = bodies.filter((b) => prefer.test(b));
    if (hit.length) return hit[hit.length - 1];
  }
  return bodies[bodies.length - 1];
}

/* ============================================================
   一、CSS：不再有强制吸附，且内层不外抛滚动链
   ============================================================ */
section('一、CSS 契约');

check('① 全文件已无 scroll-snap-type（含旧小程序块）', !/scroll-snap-type\s*:/.test(css),
      (css.match(/scroll-snap-type\s*:[^;}]*/) || [''])[0]);
check('② 全文件已无 scroll-snap-align', !/scroll-snap-align\s*:/.test(css),
      (css.match(/scroll-snap-align\s*:[^;}]*/) || [''])[0]);

const vpBody = ruleBody(css, '.wheel-viewport', /overflow-y\s*:/);
check('③ .wheel-viewport 仍可原生滚动（overflow-y: auto）', /overflow-y\s*:\s*auto/.test(vpBody),
      vpBody.replace(/\s+/g, ' ').trim().slice(0, 120));
check('④ .wheel-viewport 加了 overscroll-behavior: contain（刷到头不再把整页拖起来）',
      /overscroll-behavior\s*:\s*contain/.test(vpBody), vpBody.replace(/\s+/g, ' ').trim());
check('⑤ .wheel-viewport 保留 iOS 惯性滚动', /-webkit-overflow-scrolling\s*:\s*touch/.test(vpBody));

const itemBody = ruleBody(css, '.wheel-item {', /height\s*:/);
check('⑥ .wheel-item 高度与视口 5 格对齐（10.56cqw，视口 52.8cqw）',
      /height\s*:\s*10\.56cqw/.test(itemBody), itemBody.replace(/\s+/g, ' ').trim().slice(0, 160));

const activeBody = ruleBody(css, '.wheel-item.wheel-item-active', /font-size\s*:/);
check('⑦ active 项字号显式覆盖为 4.32cqw（挡住旧小程序块泄漏的 4.08cqw）',
      /font-size\s*:\s*4\.32cqw/.test(activeBody), activeBody.replace(/\s+/g, ' ').trim());
check('⑧ active 项 text-shadow: none（挡住旧块泄漏的橙色光晕）',
      /text-shadow\s*:\s*none/.test(activeBody), activeBody.replace(/\s+/g, ' ').trim());

check('⑨ 滚轮尺寸仍是 5 格几何（.wheel-col 高 52.8cqw）',
      /height\s*:\s*52\.8cqw/.test(ruleBody(css, '.wheel-col', /height\s*:/)));

/* ============================================================
   一之二、生成器模板必须同步（否则任何人跑一次 tools/wxss2web.mjs
   就会把 styles.css 整份重新生成、静默回退本次修复）
   ============================================================ */
section('一之二、生成器模板（tools/wxss2web.mjs）同步');

const GEN = path.join(REPO, 'mobile_apps', 'android_app', 'tools', 'wxss2web.mjs');
const genRaw = fs.readFileSync(GEN, 'utf8');
const genCss = genRaw.replace(/\/\*[\s\S]*?\*\//g, ''); // 模板里有 CSS 注释，同样要先剥

check('⑩ 模板已无 scroll-snap-type（重跑生成器不会把强制吸附加回来）',
      !/scroll-snap-type\s*:/.test(genCss),
      (genCss.match(/scroll-snap-type\s*:[^;}]*/) || [''])[0]);
check('⑪ 模板已无 scroll-snap-align', !/scroll-snap-align\s*:/.test(genCss),
      (genCss.match(/scroll-snap-align\s*:[^;}]*/) || [''])[0]);

const genVp = ruleBody(genCss, '.wheel-viewport', /overflow-y\s*:/);
check('⑫ 模板 .wheel-viewport 有 overscroll-behavior: contain',
      /overscroll-behavior\s*:\s*contain/.test(genVp), genVp.replace(/\s+/g, ' ').trim());
check('⑬ 模板 .wheel-viewport 有 -webkit-overflow-scrolling: touch',
      /-webkit-overflow-scrolling\s*:\s*touch/.test(genVp));
check('⑭ 模板 .wheel-item 无 scroll-snap-align、active 段钉死字号与 text-shadow',
      !/scroll-snap-align/.test(ruleBody(genCss, '.wheel-item {', /height\s*:/)) &&
      /font-size\s*:\s*4\.8cqw/.test(ruleBody(genCss, '.wheel-item.wheel-item-active', /font-size\s*:/)) &&
      /text-shadow\s*:\s*none/.test(ruleBody(genCss, '.wheel-item.wheel-item-active', /font-size\s*:/)));

/* ============================================================
   二、JS：从真实 print.js 抽 buildWheel + readWheelIndex 跑桩 DOM
   ============================================================ */
function extractWheelBlock() {
  const start = printJs.indexOf('const WHEEL_SNAP_IDLE_MS');
  const end = printJs.indexOf('function openScheduleCountdownPicker()');
  if (start < 0 || end < 0 || end <= start) return null;
  const build = printJs.slice(start, end);
  // 再把 readWheelIndex 单独带上（契约：与吸附目标同一套「中心最近」规则）
  const rStart = printJs.indexOf('function readWheelIndex(wheelId) {');
  const rEnd = printJs.indexOf('function validateSchedule()');
  if (rStart < 0 || rEnd < 0 || rEnd <= rStart) return null;
  return build + '\n' + printJs.slice(rStart, rEnd);
}

const ITEM_H = 40;
const VP_H = 200;
const PAD = 80;   // .wheel-items 上下各 2 格
const COUNT = 10;

function makeWheelDom() {
  const items = [];
  for (let i = 0; i < COUNT; i++) {
    items.push({
      offsetTop: PAD + i * ITEM_H,
      offsetHeight: ITEM_H,
      textContent: String(i),
      _active: false,
      classList: {
        toggle(name, on) { if (name === 'wheel-item-active') items[i]._active = !!on; },
        add() {}, remove() {}, contains() { return false; },
      },
    });
  }
  const vp = {
    scrollTop: 0,
    clientHeight: VP_H,
    scrollHeight: PAD * 2 + COUNT * ITEM_H,
    _listeners: {},
    _scrollCalls: [],
    addEventListener(type, fn, opts) { this._listeners[type] = { fn, passive: !!(opts && opts.passive) }; },
    removeEventListener(type) { delete this._listeners[type]; },
    querySelector(sel) { return sel === '.wheel-item' ? items[0] : null; },
    querySelectorAll(sel) { return sel === '.wheel-item' ? items.slice() : []; },
    scrollTo(arg) {
      const top = (arg && typeof arg === 'object') ? arg.top : arg;
      this._scrollCalls.push({ top, behavior: arg && typeof arg === 'object' ? arg.behavior : undefined });
      this.scrollTop = top;
    },
    /* 模拟原生滚动：移动 scrollTop 并派发一次 scroll 事件 */
    scrollToRaw(top) {
      this.scrollTop = top;
      if (this._listeners.scroll) this._listeners.scroll.fn();
    },
    fire(type) { if (this._listeners[type]) this._listeners[type].fn(); },
  };
  const col = {
    innerHTML: '',
    querySelector(sel) { return sel === '.wheel-viewport' ? vp : null; },
    querySelectorAll(sel) { return sel === '.wheel-item' ? items.slice() : []; },
  };
  return { col, vp, items };
}

function loadWheel(wheelId) {
  const block = extractWheelBlock();
  if (!block) return null;
  const dom = makeWheelDom();
  const changes = [];
  const sandbox = {
    console, Math, JSON, Date, Object, Array, String, Number, isNaN, parseInt, parseFloat,
    setTimeout, clearTimeout,
    requestAnimationFrame: (fn) => { fn(); return 0; },
    escHtml: (s) => String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
    window: { matchMedia: () => ({ matches: false }) },
    document: { getElementById: () => dom.col },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(block, sandbox, { filename: 'wheel(extracted)' });
  const buildWheel = vm.runInContext('buildWheel', sandbox);
  const readWheelIndex = vm.runInContext('readWheelIndex', sandbox);
  buildWheel(wheelId, Array.from({ length: COUNT }, (_, i) => String(i)), 3, (i) => changes.push(i));
  return { dom, changes, buildWheel, readWheelIndex, block };
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const targetTop = (i) => PAD + i * ITEM_H + ITEM_H / 2 - VP_H / 2; // = 40*i

(async () => {
  section('二、JS 静态契约');
  const block = extractWheelBlock();
  if (!block) {
    check('可从 print.js 抽取 buildWheel', false);
    console.log('\n❌ 1 项失败');
    process.exit(1);
    return;
  }
  check('① 有静止吸附的常量（WHEEL_SNAP_IDLE_MS）', /const WHEEL_SNAP_IDLE_MS\s*=\s*\d+/.test(block));
  check('② print.js 生效代码里已无 CSS 吸附残留（scroll-snap-*）',
        !/scroll-snap/.test(printJsCode),
        (printJsCode.split('scroll-snap')[1] || '').slice(0, 40));
  check('③ 仍保留「中心最近」索引判定（nearestIndex）', /const nearestIndex = \(\) =>/.test(block));
  check('④ 吸附目标与 centerItem 用同一算法（targetTopFor）', /const targetTopFor = \(i\) =>/.test(block));
  check('⑤ 拖动中不吸附（touching 守卫）', /if \(!touching && !snapping\) scheduleSnap\(\)/.test(block));
  check('⑥ onChange 仍是 120ms 去抖后回调（契约不变）',
        /deb = setTimeout\(\(\) => \{ if \(typeof onChange === 'function'\) onChange\(idx\); \}, 120\)/.test(block));
  check('⑦ 尊重 reduce-motion（吸附退化为瞬时）', /prefers-reduced-motion/.test(block));

  section('三、行为：初始定位 / 拖动 / 松手吸附');
  const w = loadWheel('hourWheel');
  check('① 初始把选中项居中（idx=3 ⇒ scrollTop=120）',
        Math.round(w.dom.vp.scrollTop) === targetTop(3), 'scrollTop=' + w.dom.vp.scrollTop);
  check('② 初始渲染就把选中项标成 active（模板里带 wheel-item-active）',
        /class="wheel-item wheel-item-active" data-i="3"/.test(w.dom.col.innerHTML),
        (w.dom.col.innerHTML.match(/class="wheel-item[^"]*" data-i="3"/) || [''])[0]);
  check('③ 下拉/拖动中：scroll 事件不触发吸附（200ms 内无 scrollTo）',
        await (async () => {
          w.dom.vp.fire('touchstart');
          w.dom.vp.scrollToRaw(300);
          await wait(60);
          w.dom.vp.scrollToRaw(320);
          await wait(220);
          return w.dom.vp._scrollCalls.length === 0;
        })(), JSON.stringify(w.dom.vp._scrollCalls));
  check('④ 拖动中 scrollTop 完全跟手（没被抢）', w.dom.vp.scrollTop === 320, 'scrollTop=' + w.dom.vp.scrollTop);

  /* 松手：轻扫过若干格（落点 320 = 第 8 格正中，但先给个 310 的"差一点"落点更像真实惯性） */
  w.dom.vp.scrollToRaw(310);
  w.dom.vp.fire('touchend');
  await wait(240);
  check('⑤ 松手静止后吸附一次（只一次）', w.dom.vp._scrollCalls.length === 1,
        JSON.stringify(w.dom.vp._scrollCalls));
  check('⑥ 吸附目标是最近一格的中心（310 → 第 8 格 320）',
        w.dom.vp._scrollCalls[0] && Math.round(w.dom.vp._scrollCalls[0].top) === targetTop(8),
        JSON.stringify(w.dom.vp._scrollCalls[0]));
  check('⑦ 吸附用 smooth（轻扫即走，不是硬跳）',
        w.dom.vp._scrollCalls[0] && w.dom.vp._scrollCalls[0].behavior === 'smooth');
  check('⑧ 吸附后 scrollTop 落在目标', Math.round(w.dom.vp.scrollTop) === targetTop(8));
  check('⑨ 高亮跟着走到第 8 格', w.dom.items[8]._active === true && w.dom.items[3]._active === false);

  section('四、契约：onChange / readWheelIndex 与吸附同规则');
  /* 上面 310 已经排过 120ms 的 onChange 去抖；等它落地 */
  await wait(160);
  check('① onChange 按最近索引回调（8）', w.changes.length > 0 && w.changes[w.changes.length - 1] === 8,
        JSON.stringify(w.changes));
  check('② readWheelIndex 与吸附目标同一索引（310 → 8）', w.readWheelIndex('hourWheel') === 8,
        'idx=' + w.readWheelIndex('hourWheel'));

  /* 再验一次"跨多格"：从 120 一路扫到 660（跨 13 格以上后回落），吸附应落在最近格而不是一格一格爬 */
  const w2 = loadWheel('minuteWheel');
  w2.dom.vp.fire('touchstart');
  for (const s of [140, 260, 380, 500, 620, 660]) { w2.dom.vp.scrollToRaw(s); await wait(16); }
  w2.dom.vp.fire('touchend');
  await wait(240);
  const last = w2.dom.vp._scrollCalls[w2.dom.vp._scrollCalls.length - 1];
  const clampIdx = Math.min(COUNT - 1, Math.round(660 / ITEM_H)); // 660 已超过末项极限，钳到最后一格
  check('③ 一次手势可跨多格、只吸附一次（660 ⇒ 最近格中心，越界钳到末项）',
        w2.dom.vp._scrollCalls.length === 1 && last && Math.round(last.top) === targetTop(clampIdx),
        JSON.stringify(w2.dom.vp._scrollCalls) + ' expect=' + targetTop(clampIdx));

  /* 吸附完成后不再自己和自己抢（snapping 期间不重复排） */
  const before = w2.dom.vp._scrollCalls.length;
  w2.dom.vp.fire('touchend');
  await wait(240);
  check('④ 已经在正中时不再重复吸附（差值 <0.5px 直接收尾）',
        w2.dom.vp._scrollCalls.length === before, 'calls=' + w2.dom.vp._scrollCalls.length);

  console.log('\n' + (failures === 0 ? '✅ 全部通过' : `❌ ${failures} 项失败`));
  process.exit(failures === 0 ? 0 : 1);
})().catch((e) => {
  console.error('运行异常：', e);
  process.exit(1);
});
