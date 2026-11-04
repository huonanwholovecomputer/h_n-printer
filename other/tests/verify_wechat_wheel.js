/* eslint-disable */
/**
 * 时间滚轮「惯性/慢滑」验收（需求③）
 *
 * 用户原话：「小程序滑动阻力正常但惯性行为有问题——小程序对惯性的处理是先算出本次滑动最终会停在
 * 哪个数字，然后直接将高亮定位到那个数字，并不会扫过每一个数字，这导致在缓慢滑动时，每次都定位到了
 * 开始滑动的那个数字，这导致缓慢的滑动无效」。
 *
 * 病根（已定位）：官方 picker-view 的 `value` 是「让原生列滚到该索引」的**指令**。原实现在
 * bindchange（拖动中每跨一格都触发，因为 immediate-change）里把 `timeWheelValue` 写回当前索引，
 * 等于每跨一格就把正在拖的列拽回 value 对应位置 —— 表现就是「直接定位到最终数字」「慢滑无效」。
 * 今天的小时列变化还会在拖动中重建 minuteItems，整棵 picker-view 子树重渲染，正在拖的列被重置。
 *
 * 修法（本脚本验收的口径）：
 *   · 拖动中（bindchange）只更新派生索引（决定 .wheel-item-active 高亮）→ 逐格跟手；
 *   · value 只在松手（bindpickend）同步一次 —— 那时轮子本来就在该索引上，写回零位移；
 *   · 分钟列级联重建也推迟到松手。
 *   · 仍用官方 picker-view，不自研 canvas 滚轮。
 *
 * 用法：node tests/verify_wechat_wheel.js   （exit 0 = 全绿）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..', '..');
const MP_DIR = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'index');
const INDEX_JS = path.join(MP_DIR, 'index.js');
const INDEX_WXML = path.join(MP_DIR, 'index.wxml');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }

/* 取一个方法的函数体（按 4 空格缩进的方法声明定位，括号配平） */
function methodBody(src, name) {
  const idx = src.indexOf('\n    ' + name + '(');
  if (idx < 0) return '';
  const open = src.indexOf('{', idx);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    const c = src[i];
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) return src.slice(open, i + 1); }
  }
  return '';
}

function stripJsComments(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"\\])\/\/[^\n]*/g, '$1');
}

/* ---------------- 一、静态判据 ---------------- */
function runStatic() {
  section('一、静态判据（源码接线）');
  const js = fs.readFileSync(INDEX_JS, 'utf8');
  const wxml = fs.readFileSync(INDEX_WXML, 'utf8');
  const jsCode = stripJsComments(js);

  const changeBody = methodBody(jsCode, 'onTimeWheelChange');
  const pickEndBody = methodBody(jsCode, 'onTimeWheelPickEnd');
  const cdChangeBody = methodBody(jsCode, 'onCountdownWheelChange');
  const cdEndBody = methodBody(jsCode, 'onCountdownWheelPickEnd');

  check('①onTimeWheelChange 存在且非空', changeBody.length > 40, changeBody.length + ' chars');
  check('①拖动中**不写回** timeWheelValue（这是「慢滑无效」的唯一病根）',
        changeBody.indexOf('timeWheelValue') < 0);
  check('①拖动中只更新派生索引 hourWheelIndex / minuteWheelIndex（逐格高亮跟手）',
        changeBody.indexOf('hourWheelIndex') >= 0 && changeBody.indexOf('minuteWheelIndex') >= 0);
  check('①拖动中不重建 minuteItems（重建会让 picker-view 子树重渲染、正在拖的列被重置）',
        changeBody.indexOf('_buildMinuteItems') < 0 && changeBody.indexOf('minuteItems') < 0);

  check('②松手 onTimeWheelPickEnd 才同步 timeWheelValue（贴合最近项、零位移）',
        pickEndBody.indexOf('timeWheelValue') >= 0);
  check('②松手才做分钟列级联重建（今天的小时变化 → 分钟列按新小时重建）',
        pickEndBody.indexOf('_buildMinuteItems') >= 0 && /scheduleDayIndex === 0/.test(pickEndBody));
  check('③bindpickstart 接线（拖动开始标记）',
        /bindpickstart="onTimeWheelPickStart"/.test(wxml));
  check('③bindpickend 接线（拖动结束同步 value）',
        /bindpickend="onTimeWheelPickEnd"/.test(wxml));
  check('③倒计时滚轮同样只记索引、不写回 value',
        cdChangeBody.length > 20 && cdChangeBody.indexOf('countdownWheelValue') < 0);
  check('③倒计时滚轮松手同步 countdownWheelValue',
        cdEndBody.indexOf('countdownWheelValue') >= 0
        && /bindpickend="onCountdownWheelPickEnd"/.test(wxml));
  check('④仍用官方 picker-view（未自研 canvas 滚轮）',
        wxml.indexOf('<picker-view') >= 0
        && !/createCanvasContext\(\s*['"][^'"]*wheel/i.test(js)
        && !/<canvas[^>]*wheel/i.test(wxml));
  check('④时间滚轮 value 仍由 timeWheelValue 驱动（系统 picker 同款居中引擎）',
        /<picker-view[^>]*value="\{\{timeWheelValue\}\}"/.test(wxml));
  check('④picker-view 保留 immediate-change（拖动中逐格触发 change，才能跟手）',
        /<picker-view[^>]*immediate-change[^>]*bindchange="onTimeWheelChange"/.test(wxml)
        || /<picker-view[^>]*bindchange="onTimeWheelChange"[^>]*immediate-change/.test(wxml));
  check('④高亮由索引驱动（.wheel-item-active 跟 hourWheelIndex/minuteWheelIndex）',
        /wheel-item \{\{index === hourWheelIndex \? 'wheel-item-active' : ''\}\}/.test(wxml)
        && /wheel-item \{\{index === minuteWheelIndex \? 'wheel-item-active' : ''\}\}/.test(wxml));
  check('④onTimeWheelChange 的注释里写明了病根（后续维护别又写回 value）',
        /拖动中绝不写回 timeWheelValue/.test(js));
}

/* ---------------- 二、行为判据（真实 handler + 桩渲染层） ---------------- */
function loadIndexPage() {
  const src = fs.readFileSync(INDEX_JS, 'utf8');
  const PAGES = (() => {
    const _src = fs.readFileSync(path.join(REPO, 'mobile_apps', 'h_n_print', 'utils', 'pages.js'), 'utf8');
    const _m = { exports: {} };
    new Function('module', 'exports', _src)(_m, _m.exports);
    return _m.exports;
  })();
  let pageConfig = null;
  const calls = { toast: [], requests: [], setDataKeys: [] };
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
          request: (opt) => calls.requests.push(opt),
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
        select: () => ({ boundingClientRect() { return this; } }),
        in() { return this; },
        exec(cb) { cb && cb([[], null]); },
      }),
      showToast: (o) => calls.toast.push(o && o.title),
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
    Object.keys(patch || {}).forEach((k) => { calls.setDataKeys.push(k); this.data[k] = patch[k]; });
    if (cb) cb();
  };
  page.getTabBar = () => null;
  page._scheduleMeasure = () => {};
  page._animateFileListHeight = () => {};
  return { page, calls };
}

function runBehavior() {
  section('二、行为判据（拖动中 value 不抖、松手才贴合）');
  const { page } = loadIndexPage();

  /* 非今天（scheduleDayIndex !== 0）：没有级联，纯粹验证「拖动中不写回 value」 */
  page.data.scheduleDayIndex = 1;
  page.data.hourItems = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, '0'));
  page.data.minuteItems = Array.from({ length: 60 }, (_, i) => String(i).padStart(2, '0'));
  page.data.hourWheelIndex = 8;
  page.data.minuteWheelIndex = 30;
  page.data.timeWheelValue = [8, 30];
  page._wheelHi = 8;
  page._wheelMi = 30;
  page._wheelCascadeHi = 8;

  /* ① 慢滑：一格一格跨过 8→9→10→11，value 全程必须钉在起点（否则就是「被拽回」） */
  page.onTimeWheelPickStart();
  const snapshots = [];
  [9, 10, 11, 12].forEach((hi) => {
    page.onTimeWheelChange({ detail: { value: [hi, 5] } });
    snapshots.push(JSON.stringify(page.data.timeWheelValue));
    check(`①慢滑跨到小时 ${hi}：高亮索引跟手（hourWheelIndex=${hi}）`,
          page.data.hourWheelIndex === hi && page.data.minuteWheelIndex === 5,
          page.data.hourWheelIndex + ',' + page.data.minuteWheelIndex);
  });
  check('①慢滑全程 timeWheelValue 一次都没被改写（= 不会「定位回开始滑动的那个数字」）',
        snapshots.every((s) => s === '[8,30]'), snapshots.join(' → '));

  /* ② 松手：value 贴合最近项（此刻轮子就在 12/5 上，写回零位移） */
  page.onTimeWheelPickEnd();
  check('②松手后 value 贴合最近项 [12,5]',
        JSON.stringify(page.data.timeWheelValue) === '[12,5]',
        JSON.stringify(page.data.timeWheelValue));

  /* ③ 快扫：一次跳到很远的索引，拖动中 value 也不动，松手才同步 */
  page.onTimeWheelPickStart();
  page.onTimeWheelChange({ detail: { value: [21, 59] } });
  check('③快扫（8→21）拖动中 value 仍不动', JSON.stringify(page.data.timeWheelValue) === '[12,5]',
        JSON.stringify(page.data.timeWheelValue));
  check('③快扫拖动中高亮已到 21/59（逐格跟手不靠 value）',
        page.data.hourWheelIndex === 21 && page.data.minuteWheelIndex === 59);
  page.onTimeWheelPickEnd();
  check('③快扫松手 value = [21,59]',
        JSON.stringify(page.data.timeWheelValue) === '[21,59]',
        JSON.stringify(page.data.timeWheelValue));

  /* ④ 同索引 change（原生列回弹到同一格）：不应产生无谓 setData */
  const before = page.data.timeWheelValue.slice();
  page.onTimeWheelChange({ detail: { value: [21, 59] } });
  check('④同索引 change 不重复写索引（避免无谓渲染）',
        page.data.hourWheelIndex === 21 && page.data.minuteWheelIndex === 59
        && JSON.stringify(page.data.timeWheelValue) === JSON.stringify(before));

  /* ⑤ 非法事件（缺 detail / 单元素数组）：直接忽略，不炸 */
  let threw = false;
  try {
    page.onTimeWheelChange({});
    page.onTimeWheelChange({ detail: {} });
    page.onTimeWheelChange({ detail: { value: [3] } });
  } catch (e) { threw = true; }
  check('⑤非法 change 事件不抛异常、不改状态', !threw && page.data.hourWheelIndex === 21);

  /* ⑥ 今天的小时变化：级联推迟到松手，且分钟回到该小时第一个可用分钟 */
  page.data.scheduleDayIndex = 0;
  page.data.hourStart = 0;
  page.data.minuteStart = 0;
  page.data.hourWheelIndex = 8;
  page.data.minuteWheelIndex = 30;
  page.data.timeWheelValue = [8, 30];
  page.data.minuteItems = Array.from({ length: 60 }, (_, i) => String(i).padStart(2, '0'));
  page._wheelHi = 8;
  page._wheelMi = 30;
  page._wheelCascadeHi = 8;

  page.onTimeWheelPickStart();
  page.onTimeWheelChange({ detail: { value: [9, 40] } });
  check('⑥今天：拖动中不重建 minuteItems（重建会重置正在拖的列）',
        page.data.minuteItems.length === 60 && page.data.minuteWheelIndex === 40);
  check('⑥今天：拖动中 value 不动', JSON.stringify(page.data.timeWheelValue) === '[8,30]',
        JSON.stringify(page.data.timeWheelValue));
  page.onTimeWheelPickEnd();
  check('⑥今天：松手才级联（分钟归 0、value=[9,0]）',
        page.data.minuteWheelIndex === 0
        && JSON.stringify(page.data.timeWheelValue) === '[9,0]',
        JSON.stringify(page.data.timeWheelValue));
  check('⑥今天：级联后 minuteItems 已按新小时重建', page.data.minuteItems.length > 0);

  /* ⑦ 松手但没拖过（_wheelHi 未初始化）：不写 value，保持原样 */
  page._wheelHi = undefined;
  page._wheelMi = undefined;
  page.data.timeWheelValue = [9, 0];
  page.onTimeWheelPickEnd();
  check('⑦无拖动直接松手：不写 value', JSON.stringify(page.data.timeWheelValue) === '[9,0]',
        JSON.stringify(page.data.timeWheelValue));

  /* ⑧ 倒计时滚轮：同一病根，同一修法 */
  page.data.countdownMinWheelIndex = 5;
  page.data.countdownSecWheelIndex = 10;
  page.data.countdownWheelValue = [5, 10];
  page._cdWheelMi = 5;
  page._cdWheelSi = 10;
  page.onCountdownWheelChange({ detail: { value: [6, 20] } });
  check('⑧倒计时：拖动中索引跟手（6/20）',
        page.data.countdownMinWheelIndex === 6 && page.data.countdownSecWheelIndex === 20);
  check('⑧倒计时：拖动中 value 不动', JSON.stringify(page.data.countdownWheelValue) === '[5,10]',
        JSON.stringify(page.data.countdownWheelValue));
  page.onCountdownWheelPickEnd();
  check('⑧倒计时：松手 value = [6,20]',
        JSON.stringify(page.data.countdownWheelValue) === '[6,20]',
        JSON.stringify(page.data.countdownWheelValue));
}

runStatic();
runBehavior();

console.log('\n' + '='.repeat(52));
if (failures) {
  console.log(`时间滚轮「惯性/慢滑」验收: ${failures} 项失败 ❌`);
  process.exit(1);
}
console.log('时间滚轮「惯性/慢滑」验收: 全部通过 ✅（手感仍需真机确认）');
process.exit(0);
