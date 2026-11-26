/* eslint-disable */
/**
 * 时间滚轮验收：自研 scroll-view 滚轮（小程序「无障碍打印 → 指定时间 / 倒计时」）
 *
 * 用户原话（2026-11-26 第一轮反馈）：「缓慢滑动时，列表会发生闪烁 …… 在开始滑动时的数字和正在滑动的
 * 位置之间来回闪烁，松开手指之后，数字停留在开始滑动的那个数字上」；「点击任意一个未选中的列表中的
 * 数字时，那个数字会高亮，但是列表本身没有向那个数字进行滑动，这导致我的点击选择是无效的」。
 * 第二轮反馈：「有所改善，但是以极慢的速度滑动的时候依旧会闪烁」。
 *
 * 两轮都没根治的原因（本轮查明的口径）：
 *   picker-view 的列是**原生列**，绑定的 `value` 是「让列滚到该索引」的**指令**，不是可回读的状态。
 *   页面只要发生**一次重渲染**（第一轮是 item 换 `.wheel-item-active`；第二轮是「无障碍打印 ON」期间
 *   每 100ms 一次的呼吸光晕 setData），原生列就会把当前绑定的 value 重新应用一次 ⇒ 手指还按着也被
 *   拽回旧位置 = 闪烁；点选时原生只改高亮、随即被拽回 = 点了没反应。
 *   官方文档也写明「目前滚动过程中 bind:pickstart、bind:pickend 会被触发多次」，所以靠 pickend
 *   决定「何时写回 value」从原理上就不成立 —— 那是第二轮修复失效的原因。
 *
 * 修法（本脚本钉住的口径）：
 *   · 滚动手感交给 scroll-view 原生滚动（贴合/惯性/边界都在原生层），JS **不参与手势**；
 *   · 手势期间**一次 setData 都不发**：滚动事件只把位置记进实例字段；
 *   · 手指离手且静止 WHEEL_SNAP_IDLE_MS(140ms) 之后，一次性 setData 把列平滑吸附到最近一格
 *     （「居中第 i 项 ⇒ scrollTop = i × 一格高」，见 .wheel-pad 留白，无需量 DOM）；
 *   · 点某一格 = 一次显式定位（列表必然滚过去，不存在「只高亮不滚动」）；
 *   · 小时列级联（今天 → 分钟列裁剪）只在**吸附**时做，拖动中绝不重建分钟列；
 *   · 打开这两个弹窗期间停掉呼吸光晕那条 100ms 的 setData（关窗再续）。
 *
 * 用法：node other/tests/verify_wechat_wheel.js   （exit 0 = 全绿）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..', '..');
const MP_DIR = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'index');
const INDEX_JS = path.join(MP_DIR, 'index.js');
const INDEX_WXML = path.join(MP_DIR, 'index.wxml');
const INDEX_WXSS = path.join(MP_DIR, 'index.wxss');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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
  const wxss = fs.readFileSync(INDEX_WXSS, 'utf8');
  const jsCode = stripJsComments(js);

  const scrollBody = methodBody(jsCode, 'onWheelScroll');
  const touchStartBody = methodBody(jsCode, 'onWheelTouchStart');
  const touchEndBody = methodBody(jsCode, 'onWheelTouchEnd');
  const commitBody = methodBody(jsCode, '_wheelCommit');
  const openTimeBody = methodBody(jsCode, 'onOpenScheduleTimePicker');
  const closeTimeBody = methodBody(jsCode, 'onCloseScheduleTimePicker');
  const openCdBody = methodBody(jsCode, 'onOpenScheduleCountdownPicker');
  const closeCdBody = methodBody(jsCode, 'onCloseScheduleCountdownPicker');

  /* ① 官方 picker-view 彻底退场（它的 value 语义就是本 bug 的根） */
  check('①wxml 里已没有 <picker-view>（原生列 value 语义 = 一切症状的根）',
        !/<picker-view/.test(wxml) && !/picker-view-column/.test(wxml) && !/immediate-change/.test(wxml));
  check('①js 里没有残留的 picker-view 事件处理器（onTimeWheelChange / onTimeWheelPickEnd …）',
        !/onTimeWheelChange|onTimeWheelPickStart|onTimeWheelPickEnd|onCountdownWheel(Change|PickStart|PickEnd)/.test(jsCode));
  check('①data 里没有 timeWheelValue / countdownWheelValue 这类「指令型」状态',
        js.indexOf('timeWheelValue') < 0 && js.indexOf('countdownWheelValue') < 0);

  /* ② 四个列都是 scroll-view 滚轮，且 scroll-top / 动画 / 事件全部接上 */
  const scrollViews = wxml.match(/<scroll-view[^>]*class="wheel-scroll"[\s\S]*?>/g) || [];
  check('②四个滚轮列都是 <scroll-view class="wheel-scroll" scroll-y>', scrollViews.length === 4,
        scrollViews.length + ' 个');
  check('②每列都绑定了 scroll-top（吸附定位）与 scroll-with-animation（平滑吸附）',
        scrollViews.every((t) => /scroll-top="\{\{[a-zA-Z]+\}\}"/.test(t)
          && /scroll-with-animation="\{\{[a-zA-Z]+\}\}"/.test(t)));
  check('②每列都用 data-wheel 标明列名（一套通用 handler 服务四列）',
        scrollViews.every((t) => /data-wheel="(hour|minute|cdMin|cdSec)"/.test(t)));
  check('②每列都接了 bindscroll / bindtouchstart / bindtouchend / bindtouchcancel',
        scrollViews.every((t) => /bindscroll="onWheelScroll"/.test(t)
          && /bindtouchstart="onWheelTouchStart"/.test(t)
          && /bindtouchend="onWheelTouchEnd"/.test(t)
          && /bindtouchcancel="onWheelTouchEnd"/.test(t)));

  /* ③ 几何常量：JS 与 WXSS 必须同源同值（不一致就吸附错位） */
  check('③JS 常量 WHEEL_ITEM_RPX=96 / WHEEL_VIEW_RPX=440',
        /const WHEEL_ITEM_RPX = 96\b/.test(jsCode) && /const WHEEL_VIEW_RPX = 440\b/.test(jsCode));
  check('③JS 常量 WHEEL_SNAP_IDLE_MS=140（与 App 侧 buildWheel 同值）',
        /const WHEEL_SNAP_IDLE_MS = 140\b/.test(jsCode));
  check('③WXSS .wheel-item 高 96rpx、.wheel-scroll 高 440rpx、.wheel-pad 高 172rpx (= (440-96)/2)、指示带同高',
        /\.wheel-item\s*\{[^}]*height:\s*96rpx/.test(wxss)
        && /\.wheel-scroll\s*\{[^}]*height:\s*440rpx/.test(wxss)
        && /\.wheel-pad\s*\{[^}]*height:\s*172rpx/.test(wxss)
        && /\.wheel-indicator\s*\{[^}]*height:\s*96rpx/.test(wxss));
  check('③WXSS 不做 scroll-snap（WebView 内核会在松手瞬间把惯性收进吸附点 = 一次手势只走一格）',
        !/scroll-snap/.test(wxss.replace(/\/\*[\s\S]*?\*\//g, '')));

  /* ④ 手势期间零 setData —— 本文件的核心不变量 */
  check('④[WHEEL-ZERO-SETDATA] 不变量标记在 onWheelScroll 之前（注释钉住，别删）',
        js.indexOf('[WHEEL-ZERO-SETDATA]') > 0
        && js.indexOf('[WHEEL-ZERO-SETDATA]') < js.indexOf('onWheelScroll('));
  check('④onWheelScroll 函数体里**没有任何 setData**（手势中只写实例字段）',
        scrollBody.length > 80 && scrollBody.indexOf('setData') < 0,
        scrollBody.length + ' chars');
  check('④onWheelTouchStart / onWheelTouchEnd 里也没有 setData',
        touchStartBody.indexOf('setData') < 0 && touchEndBody.indexOf('setData') < 0);
  check('④滚动只更新实例字段（st.top / st.idx），吸附另起定时器',
        /st\.top = top/.test(scrollBody) && /st\.idx = this\._wheelIndexAt/.test(scrollBody)
        && /this\._wheelScheduleSnap\(name\)/.test(scrollBody));
  check('④吸附推迟到「手指离手 + 静止」之后（touching / snapping 两道闸门）',
        /if \(st\.touching \|\| st\.snapping\) return/.test(scrollBody)
        && /if \(st\.touching\) return/.test(methodBody(jsCode, '_wheelSnap')));
  check('④setData 只允许在 _wheelCommit 里写位置（除两个「开窗首次定位」）',
        commitBody.indexOf('this.setData(patch)') > 0
        && (() => {
          // 扫全文件的方法体：凡是 setData 里出现 scroll-top 字面量的，只能是开窗定位的那两个方法
          const re = /\n    ([A-Za-z_$][\w$]*)\([^\n]*\)\s*\{/g;
          const allowed = ['onOpenScheduleTimePicker', 'onOpenScheduleCountdownPicker'];
          let m; let bad = [];
          while ((m = re.exec(jsCode))) {
            const body = methodBody(jsCode, m[1]);
            if (body.indexOf('setData') < 0) continue;
            if (/\w+WheelTop:/.test(body) && allowed.indexOf(m[1]) < 0) bad.push(m[1]);
          }
          return bad.length === 0;
        })());

  /* ⑤ 点选必须真的滚过去（用户第二个 bug） */
  const itemTags = wxml.match(/<view[^>]*class="wheel-item[^"]*"[^>]*>/g) || [];
  check('⑤滚轮中的每一格都绑了 bindtap="onWheelTap" + data-wheel + data-index',
        itemTags.length >= 4
        && itemTags.every((t) => /bindtap="onWheelTap"/.test(t) && /data-wheel="/.test(t) && /data-index="\{\{index\}\}"/.test(t)),
        itemTags.length + ' 处');
  check('⑤格子的 class 是静态的 "wheel-item"（不带 data 驱动的 active class）',
        itemTags.every((t) => /class="wheel-item( wheel-item-(hour|minute|cdmin|cdsec))?"/.test(t))
        && wxml.indexOf('wheel-item-active') < 0 && wxss.indexOf('wheel-item-active') < 0);
  const tapBody = methodBody(jsCode, 'onWheelTap');
  check('⑤点选与拖动走同一个写入口（_wheelCommit / _wheelSnapHour）',
        tapBody.indexOf('_wheelCommit(') >= 0 && tapBody.indexOf('_wheelSnapHour(') >= 0);

  /* ⑥ 弹窗期间不许有 100ms 级别的页面级 setData（第二轮残留的闪烁来源） */
  check('⑥打开「指定时间」弹窗时停掉呼吸光晕定时器', openTimeBody.indexOf('_stopBreathingGlow()') >= 0);
  check('⑥关闭「指定时间」弹窗后续上呼吸光晕',
        closeTimeBody.indexOf('_startBreathingGlow()') >= 0);
  check('⑥倒计时弹窗同口径（开窗停、关窗续）',
        openCdBody.indexOf('_stopBreathingGlow()') >= 0 && closeCdBody.indexOf('_startBreathingGlow()') >= 0);
  check('⑥呼吸光晕本身仍是 100ms setData（正因如此才必须在弹窗期间停掉）',
        (() => {
          const g = methodBody(jsCode, '_startBreathingGlow');
          return /setInterval\(/.test(g) && /\},\s*100\)/.test(g);
        })());

  /* ⑦ 级联只在吸附时做，拖动中绝不重建分钟列 */
  const snapHourBody = methodBody(jsCode, '_wheelSnapHour');
  check('⑦小时级联在 _wheelSnapHour 里（吸附路径），而不是滚动路径',
        snapHourBody.indexOf('_buildMinuteItems') >= 0
        && /scheduleDayIndex !== 0/.test(snapHourBody)
        && scrollBody.indexOf('_buildMinuteItems') < 0);

  /* ⑧ data 键与 JS 常量表一一对应（防手滑改名后 wxml 静默失效） */
  const cols = jsCode.match(/WHEEL_COLUMNS = \{[\s\S]*?\n\}/);
  const colSrc = cols ? cols[0] : '';
  ['hourWheelTop', 'minuteWheelTop', 'cdMinWheelTop', 'cdSecWheelTop'].forEach((k) => {
    check(`⑧scroll-top 绑定的 ${k} 同时出现在 JS 常量表与 wxml 里`,
          colSrc.indexOf(k) >= 0 && wxml.indexOf('{{' + k + '}}') >= 0);
  });

  /* ⑨ 落点校准（2026-11-26 用户：「数字没停在最中心，向上/向下偏移，偏移有规律」）
     公式 top = i × 一格高 只是估算，真机上 rpx 折算取整 / 惯性尾巴 / 动画被内核提前收尾
     都会留下几像素偏差 ⇒ 吸附后必须用 SelectorQuery 量一次真实位置并补正。 */
  const alignBody = methodBody(jsCode, '_wheelAlign');
  const schedAlignBody = methodBody(jsCode, '_wheelScheduleAlign');
  check('⑨JS 有落点校准常量 WHEEL_ALIGN_DELAY_MS / WHEEL_ALIGN_EPS',
        /const WHEEL_ALIGN_DELAY_MS = \d+/.test(jsCode) && /const WHEEL_ALIGN_EPS = /.test(jsCode));
  check('⑨四个滚轮列在 wxml 里都有 id（校准要按 id 量矩形）且与 JS 的 dom 键一致',
        scrollViews.length === 4 && scrollViews.every((t) => /id="wheel-(hour|minute|cdmin|cdsec)"/.test(t))
        && ['wheel-hour', 'wheel-minute', 'wheel-cdmin', 'wheel-cdsec'].every((id) => colSrc.indexOf("'" + id + "'") >= 0));
  check('⑨_wheelAlign 用 SelectorQuery 量「目标格中心 vs 视口中心」并算 delta',
        alignBody.indexOf('createSelectorQuery') >= 0
        && alignBody.indexOf('boundingClientRect') >= 0
        && alignBody.indexOf('scrollOffset') >= 0
        && /delta/.test(alignBody));
  check('⑨校准只在「这一列真的停了」之后才量（手指在 / 吸附动画中 / 140ms 内有 bindscroll 都不量）',
        /st\.touching/.test(alignBody) && /_wheelIdleFor\(name, WHEEL_ALIGN_IDLE_MS\)/.test(alignBody)
        && /const WHEEL_ALIGN_IDLE_MS = /.test(jsCode));
  check('⑨校准补正走 _wheelCommit(name, idx) 且带 noAlign（不自我递归），并最多复测一次',
        /_wheelCommit\(name, idx, \{/.test(alignBody) && /noAlign: true/.test(alignBody)
        && /attempt < 1/.test(alignBody)
        && /_wheelIndexAt\(name, cur\)/.test(alignBody));
  check('⑨手势/吸附路径里没有直接调 _wheelAlign（只在 _wheelScheduleAlign 的定时器里跑）',
        scrollBody.indexOf('_wheelAlign') < 0 && commitBody.indexOf('_wheelAlign') < 0
        && schedAlignBody.indexOf('this._wheelAlign(') >= 0);
  check('⑨滚动条已隐藏（scroll-view enhanced + show-scrollbar=false + WXSS ::-webkit-scrollbar）',
        scrollViews.every((t) => /show-scrollbar="\{\{false\}\}"/.test(t))
        && /::-webkit-scrollbar/.test(wxss)
        // webview 渲染下 show-scrollbar 必须同时开 enhanced 才生效（官方社区 2023-11-10 口径）；
        // 只写 show-scrollbar 时安卓/iOS 都会照常画出那根灰条（用户 2026-11-26 23:15 截图）。
        && scrollViews.every((t) => /enhanced="\{\{true\}\}"/.test(t)));

  /* ⑩ 几何必须实测（2026-11-26 用户补充：「错位是累积的，但是到最底部却没有错位了」）
     ⇒ 真机一格 pitch ≠ 88rpx×winW/750 时，i*pitch 的估算误差随 i 累积；到底部时 scrollTop
     被钳在 maxScroll=(N−1)*pitch，反而正好居中。修法：吸附后量一次真实几何（pitch/c0/viewH）
     存进实例缓存，index→位置、位置→index 全走实测值。 */
  const measureBody = methodBody(jsCode, '_wheelMeasure');
  const storeGeomBody = methodBody(jsCode, '_wheelStoreGeom');
  const targetBody = methodBody(jsCode, '_wheelTarget');
  const indexAtBody = methodBody(jsCode, '_wheelIndexAt');
  check('⑩JS 有实测几何：_wheelMeasure 量列矩形 / 全部 item 矩形 / scrollOffset',
        measureBody.indexOf('createSelectorQuery') >= 0
        && measureBody.indexOf('boundingClientRect') >= 0
        && measureBody.indexOf('scrollOffset') >= 0);
  // 第七轮真凶：query.select('#id').selectAll(...) 在真机抛 TypeError（NodesRef 没有 selectAll），
  // 被 catch 吃掉后静默退回公式估算 —— 于是「实测几何」两轮都没生效。必须 selectAll 挂 query。
  check('⑩selectAll 挂在 query 上（不能接在 select() 后面），且按每列专属 class 取格子',
        /q\.selectAll\('\.' \+ col\.itemClass\)/.test(measureBody)
        && !/\.selectAll\(/.test(measureBody.replace(/q\.selectAll\(/g, ''))
        && /itemClass/.test(jsCode));
  check('⑩WXML 四列格子都带专属 class（.wheel-item-hour/minute/cdmin/cdsec）',
        ['hour', 'minute', 'cdmin', 'cdsec'].every((k) => wxml.indexOf('wheel-item-' + k) >= 0));
  check('⑩几何缓存记下 maxScroll（scrollHeight − 视口高），并在 index→位置 / 位置→index 里用上',
        /maxScroll/.test(storeGeomBody) && /maxScroll/.test(targetBody) && /maxScroll/.test(indexAtBody));
  check('⑩滚到最底一律判最后一格（底部留白让最后一格必居中；按估算会算成倒数第二格）',
        /maxScroll/.test(indexAtBody) && /return n - 1/.test(indexAtBody));
  check('⑩几何缓存存进实例（_wheelGeoms），不进 data、不 setData',
        /this\._wheelGeoms\[name\] = geom/.test(storeGeomBody) && storeGeomBody.indexOf('setData') < 0
        && measureBody.indexOf('setData') < 0);
  check('⑩缓存里含 pitch / c0 / viewH（index→scrollTop 换算要用这三个）',
        /pitch/.test(storeGeomBody) && /\bc0\b/.test(storeGeomBody) && /viewH/.test(storeGeomBody));
  check('⑩_wheelTarget 与 _wheelIndexAt 都优先用实测 pitch（没有才退回公式 itemPx）',
        targetBody.indexOf('_wheelGeom') >= 0 && /g\.pitch/.test(targetBody)
        && targetBody.indexOf('_wheelItemPx') >= 0
        && indexAtBody.indexOf('_wheelGeom') >= 0 && /g\.pitch/.test(indexAtBody)
        && indexAtBody.indexOf('_wheelItemPx') >= 0);
  check('⑩_wheelCommit 不再用 next*itemPx 估算落点，改调 _wheelTarget',
        commitBody.indexOf('_wheelTarget(') >= 0 && !/next \* itemPx/.test(commitBody));
  check('⑩吸附前先量几何（_wheelSnap 里 _wheelMeasure 在 _wheelCommit 之前）',
        (() => {
          const snapBody = methodBody(jsCode, '_wheelSnap');
          const im = snapBody.indexOf('_wheelMeasure');
          const ic = snapBody.indexOf('_wheelCommit');
          return im >= 0 && ic > im;
        })());
  check('⑩关窗时几何缓存作废（_wheelReleaseAll 里清 _wheelGeoms）',
        methodBody(jsCode, '_wheelReleaseAll').indexOf('this._wheelGeoms = null') >= 0);
}

/* ---------------- 二、行为判据（真实 handler + 桩渲染层，数 setData 次数） ---------------- */
function loadIndexPage() {
  const src = fs.readFileSync(INDEX_JS, 'utf8');
  const PAGES = (() => {
    const _src = fs.readFileSync(path.join(REPO, 'mobile_apps', 'h_n_print', 'utils', 'pages.js'), 'utf8');
    const _m = { exports: {} };
    new Function('module', 'exports', _src)(_m, _m.exports);
    return _m.exports;
  })();
  let pageConfig = null;
  const calls = { toast: [], requests: [], setDataCalls: [] };
  const storage = { token: 'tok' };
  // 落点校准桩状态：
  //   bias  = 真机上「公式落点」与「实际居中位置」的固定偏差（像素）
  //   pitch = 真机上一格实际多高（默认 48 = 375px 宽下 96rpx，与 JS 常量一致）
  const selector = { bias: 0, pitch: 48, jitter: 0 };
  // 真机上「列当前真实的 scrollTop」：拖动时跟手（scroll() 更新），程序化落位后跟 data 走。
  // 量测桩必须读它，不能读 data —— 否则「刚拖到 32 格、data 还停在 30」会量成第 30 格。
  const liveTop = { hour: null, minute: null, cdMin: null, cdSec: null };
  const TOP_KEY = { hour: 'hourWheelTop', minute: 'minuteWheelTop',
                    cdMin: 'cdMinWheelTop', cdSec: 'cdSecWheelTop' };
  let pageRef = null;
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
      if (p.indexOf('back_guard') >= 0) return { leave() { return false; }, closeTopLayer() { return false; } };
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
      // 落点校准桩：按 bias 构造「真实矩形」。bias=0 ⇒ 量出来正好居中（校准收手、不多发 setData）；
      // bias=12 ⇒ 模拟真机上公式落点差 12px，校准必须自己补回 12px。
      createSelectorQuery: () => {
        let sel = '';
        // NodesRef 只有 boundingClientRect / fields / scrollOffset —— 没有 selectAll。
        // 真机上 `query.select('#id').selectAll('.x')` 直接抛 TypeError，被调用方 catch 掉后
        // 静默退回公式估算（第七轮之前的两轮就是这样「改了但没生效」）。桩必须同形。
        const nodes = {
          boundingClientRect() { return nodes; },
          scrollOffset() { return nodes; },
        };
        const api = {
          in() { return api; },
          select(s) { sel = s; return nodes; },
          selectAll(s) {
            sel = ({ '.wheel-item-hour': '#wheel-hour', '.wheel-item-minute': '#wheel-minute',
                     '.wheel-item-cdmin': '#wheel-cdmin', '.wheel-item-cdsec': '#wheel-cdsec' })[s] || s;
            return nodes;
          },
          exec(cb) {
            if (typeof cb !== 'function') return;
            const name = ({ '#wheel-hour': 'hour', '#wheel-minute': 'minute',
                            '#wheel-cdmin': 'cdMin', '#wheel-cdsec': 'cdSec' })[sel];
            const col = (name && pageRef && pageRef._wheels) ? pageRef._wheels[name] : null;
            const META = {
              hour:   { items: 'hourItems',           top: 'hourWheelTop',   index: 'hourWheelIndex' },
              minute: { items: 'minuteItems',         top: 'minuteWheelTop', index: 'minuteWheelIndex' },
              cdMin:  { items: 'countdownMinItems',    top: 'cdMinWheelTop',  index: 'cdMinWheelIndex' },
              cdSec:  { items: 'countdownSecItems',    top: 'cdSecWheelTop',  index: 'cdSecWheelIndex' },
            }[name];
            const list = (pageRef && pageRef.data && pageRef.data[META.items]) || [];
            const COL_TOP = 100, COL_H = 220;   // 440rpx→220px（可视区）
            const scrollTop = (liveTop[name] != null) ? liveTop[name]
              : ((pageRef && pageRef.data) ? pageRef.data[META.top] : col.top);
            // 真机几何：一格 pitch 可调（selector.pitch），留白跟着 pitch 走 ——
            // CSS 里 pad = (可视区 − 单格)/2，桩必须同口径，否则「第 i 格正中」的位置本身就错。
            const P = selector.pitch;
            const PAD = (COL_H - P) / 2;
            // jitter：模拟真机每格高度取整不一致（相邻格 ±jitter 抖动）。
            // 这时「c0 + i×平均 pitch」这种线性外推会带残差，只有「取该格实测中心」才正好居中。
            const J = selector.jitter;
            const items = list.map((_, i) => ({
              top: COL_TOP + PAD + selector.bias + i * P + (J ? ((i % 3) - 1) * J : 0) - scrollTop,
              height: P,
            }));
            // scrollHeight：真机 scrollOffset 会带，用它算 maxScroll（最后一格能不能居中就看它）
            const scrollHeight = PAD * 2 + list.length * P;
            cb([{ top: COL_TOP, height: COL_H }, items, { scrollTop, scrollHeight }]);
          },
        };
        return api;
      },
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
    calls.setDataCalls.push(Object.keys(patch || {}));
    Object.keys(patch || {}).forEach((k) => {
      this.data[k] = patch[k];
      // 程序化落位 = 列真的滚到那儿了（真相机行为），量测桩要跟着走
      const name = Object.keys(TOP_KEY).find((n) => TOP_KEY[n] === k);
      if (name) liveTop[name] = patch[k];
    });
    if (cb) cb();
  };
  page.getTabBar = () => null;
  page._scheduleMeasure = () => {};
  page._animateFileListHeight = () => {};
  page._liveTop = liveTop;
  pageRef = page;
  return { page, calls, selector };
}

const ITEM_PX = 48;   // 375px 宽下一格 = 96rpx

function scroll(page, wheel, top) {
  if (page._liveTop) page._liveTop[wheel] = top;   // 手指把列拖到了这里
  page.onWheelScroll({ currentTarget: { dataset: { wheel } }, detail: { scrollTop: top } });
}

async function openTime(page, calls) {
  calls.setDataCalls.length = 0;
  page.data.scheduleTime = '08:30';
  page.onOpenScheduleTimePicker();
  // 开窗那次 setData + 260ms 后的首次定位校验（_wheelSettle 可能补一次）
  await sleep(320);
  calls.setDataCalls.length = 0;
}

async function runBehavior() {
  section('二、行为判据（手势期间零 setData、松手才吸附、点选真的滚过去）');
  const { page, calls, selector } = loadIndexPage();
  page.data.scheduleDayIndex = 1;   // 明天：不裁剪，小时 00-23 / 分钟 00-59 全量

  /* ① 开窗：定位到已选时间（08:30 → 352 / 1320 px） */
  await openTime(page, calls);
  check('①开窗定位：hourWheelTop = 8×48 = 384', Math.abs(page.data.hourWheelTop - 8 * ITEM_PX) < 0.5,
        String(page.data.hourWheelTop));
  check('①开窗定位：minuteWheelTop = 30×48 = 1440', Math.abs(page.data.minuteWheelTop - 30 * ITEM_PX) < 0.5,
        String(page.data.minuteWheelTop));
  check('①开窗后滚动状态已建立（hour/minute 两列都有实例态）',
        !!(page._wheels && page._wheels.hour && page._wheels.minute));

  /* ② 极慢滑动：手指按住期间连发 30 个 scroll 事件，全程必须 0 次 setData */
  page.onWheelTouchStart({ currentTarget: { dataset: { wheel: 'minute' } } });
  let mid = 0;
  for (let i = 0; i < 30; i++) {
    scroll(page, 'minute', 30 * ITEM_PX + i * 3);   // 每帧 3px ≈ 极慢
    mid += calls.setDataCalls.length;
  }
  check('②极慢滑动 30 帧全程 0 次 setData（页面不重渲染 ⇒ 列不会被拽回 ⇒ 不闪）', mid === 0, mid + ' 次');
  check('②拖动中 data 里的位置不变（只有实例字段在跟手）',
        Math.abs(page.data.minuteWheelTop - 30 * ITEM_PX) < 0.5, String(page.data.minuteWheelTop));

  /* ③ 松手：静止 140ms 后恰好 1 次 setData，吸附到最近一格 */
  const finalTop = 30 * ITEM_PX + 29 * 3 + 3;  // 1530px → 最近格 round(1530/48)=32
  calls.setDataCalls.length = 0;
  page.onWheelTouchEnd({ currentTarget: { dataset: { wheel: 'minute' } } });
  check('③松手瞬间不发 setData（要等惯性/静止判定）', calls.setDataCalls.length === 0);
  await sleep(220);
  check('③静止后恰好 1 次 setData（吸附）', calls.setDataCalls.length === 1,
        JSON.stringify(calls.setDataCalls));
  check('③吸附到最近一格 32：minuteWheelTop ≈ 32×48，data 索引同步',
        Math.abs(page.data.minuteWheelTop - 32 * ITEM_PX) < 0.5 && page.data.minuteWheelIndex === 32,
        'top=' + page.data.minuteWheelTop + ' idx=' + page.data.minuteWheelIndex);
  check('③吸附时打开 scroll-with-animation（平滑吸过去，不是瞬移）',
        page.data.minuteWheelAnim === true &&
        calls.setDataCalls[0].indexOf('minuteWheelAnim') >= 0);

  /* ④ 惯性：松手后还在滚（连续 scroll 事件）⇒ 吸附定时器被不断重置，只在真正停下后吸附 */
  await sleep(520);                       // 等 ③ 那次吸附的 420ms 锁走完，回到「惯性中」的真实状态
  check('③落点本来就居中时，校准不补写（除了吸附那一次没有额外 setData）',
        calls.setDataCalls.length === 1, JSON.stringify(calls.setDataCalls));
  calls.setDataCalls.length = 0;
  page.onWheelTouchEnd({ currentTarget: { dataset: { wheel: 'minute' } } });
  for (let i = 1; i <= 10; i++) { scroll(page, 'minute', 32 * ITEM_PX + i * 12); await sleep(40); }
  check('④惯性期间 0 次 setData（滚动事件把吸附定时器一直往后推）', calls.setDataCalls.length === 0,
        JSON.stringify(calls.setDataCalls));
  await sleep(260);
  check('④惯性停下后才吸附（1 次）', calls.setDataCalls.length === 1,
        JSON.stringify(calls.setDataCalls));

  /* ⑤ 点选：点第 12 格必须真的滚过去（用户第二个 bug） */
  calls.setDataCalls.length = 0;
  page.onWheelTap({ currentTarget: { dataset: { wheel: 'minute', index: 12 } } });
  check('⑤点未选中的第 12 格：1 次 setData 且位置 = 12×48（列表真的滚过去了）',
        calls.setDataCalls.length === 1
        && Math.abs(page.data.minuteWheelTop - 12 * ITEM_PX) < 0.5
        && page.data.minuteWheelIndex === 12,
        'calls=' + JSON.stringify(calls.setDataCalls) + ' top=' + page.data.minuteWheelTop);

  /* ⑥ 重复吸附到同一格：scroll-top 值不变就不会滚 ⇒ 必须靠 eps 抖动强制再写一次 */
  const top1 = page.data.minuteWheelTop;
  page.onWheelTap({ currentTarget: { dataset: { wheel: 'minute', index: 12 } } });
  const top2 = page.data.minuteWheelTop;
  check('⑥再点同一格：scroll-top 值发生了变化（否则 scroll-view 不会重新滚动）',
        top1 !== top2 && Math.abs(top2 - 12 * ITEM_PX) < 0.5, top1 + ' → ' + top2);

  /* ⑦ 小时列点选 + 今天级联：分钟列重建并落回第 1 个可用分钟 */
  page.data.scheduleDayIndex = 0;
  page.data.hourStart = 10;
  page.data.curHour = 10;
  page.data.curMin = 20;
  page.data.minuteStart = 21;
  page.data.hourItems = ['10', '11', '12'];
  page.data.minuteItems = Array.from({ length: 39 }, (_, i) => String(21 + i).padStart(2, '0'));
  page._wheelCascadeHi = 0;
  page._wheelState('hour').idx = 0;
  page._wheelState('minute').idx = 5;
  calls.setDataCalls.length = 0;
  page.onWheelTap({ currentTarget: { dataset: { wheel: 'hour', index: 1 } } });
  check('⑦点小时列第 1 格（11 点 > 当前 10 点）：分钟列重建为 60 项',
        page.data.minuteItems.length === 60, page.data.minuteItems.length + ' 项');
  check('⑦级联后分钟位置落回第 1 个可用分钟（top≈0、索引 0）',
        Math.abs(page.data.minuteWheelTop) < 0.5 && page.data.minuteWheelIndex === 0,
        'top=' + page.data.minuteWheelTop + ' idx=' + page.data.minuteWheelIndex);

  /* ⑧ 拖动中（只有 scroll、没有点选）绝不做级联 */
  calls.setDataCalls.length = 0;
  page.onWheelTouchStart({ currentTarget: { dataset: { wheel: 'hour' } } });
  scroll(page, 'hour', 0);
  scroll(page, 'hour', 60);
  scroll(page, 'hour', 120);
  check('⑧拖动小时列 3 帧：0 次 setData、分钟列未被重建',
        calls.setDataCalls.length === 0 && page.data.minuteItems.length === 60,
        JSON.stringify(calls.setDataCalls));
  page.onWheelTouchEnd({ currentTarget: { dataset: { wheel: 'hour' } } });
  await sleep(220);

  /* ⑨ 倒计时弹窗同口径 */
  await sleep(200);
  calls.setDataCalls.length = 0;
  page.data.countdownMin = 5;
  page.data.countdownSec = 10;
  page.onOpenScheduleCountdownPicker();
  await sleep(320);
  calls.setDataCalls.length = 0;
  check('⑨倒计时开窗定位：cdMinWheelTop = 5×48', Math.abs(page.data.cdMinWheelTop - 5 * ITEM_PX) < 0.5,
        String(page.data.cdMinWheelTop));
  page.onWheelTouchStart({ currentTarget: { dataset: { wheel: 'cdSec' } } });
  for (let i = 0; i < 20; i++) scroll(page, 'cdSec', 440 + i * 4);
  check('⑨倒计时拖动 20 帧：0 次 setData', calls.setDataCalls.length === 0,
        JSON.stringify(calls.setDataCalls));
  page.onWheelTouchEnd({ currentTarget: { dataset: { wheel: 'cdSec' } } });
  await sleep(220);
  check('⑨倒计时松手后吸附 1 次、索引同步',
        Math.abs(page.data.cdSecWheelTop - page.data.countdownSecWheelIndex * ITEM_PX) < 0.5,
        'top=' + page.data.cdSecWheelTop + ' idx=' + page.data.countdownSecWheelIndex);

  /* ⑩ 确定按钮读滚轮态（不是可能过期的 data 索引） */
  page._wheelState('cdMin').idx = 7;
  page._wheelState('cdSec').idx = 42;
  page.onConfirmScheduleCountdown();
  check('⑩确定倒计时读滚轮态（7 分 42 秒）',
        page.data.countdownMin === 7 && page.data.countdownSec === 42,
        page.data.countdownMin + ':' + page.data.countdownSec);
  await sleep(260);

  /* ⑬ 落点校准（用户 2026-11-26：「数字没停在最中心，偏移有规律」）—— 量偏差、自己补正。
     注意：第七轮起「按实测几何落位」真的生效了（之前 selectAll 链式调用在真机抛 TypeError，
     静默退回公式）。所以这里必须先清掉几何缓存，才能复现「先按公式落错 12px、校准再补回」。 */
  selector.bias = 12;                      // 模拟真机上公式落点与真实居中位置差 12px
  page._wheelGeoms = null;
  calls.setDataCalls.length = 0;
  page.onWheelTap({ currentTarget: { dataset: { wheel: 'minute', index: 20 } } });
  const tappedTop = page.data.minuteWheelTop;   // 公式落点 = 20×48 = 960（几何已清，走估算）
  calls.setDataCalls.length = 0;                // 只数「校准补写」，点选那次先记下
  await sleep(760);                             // 360ms 校准 + 复测一轮
  const calCalls = calls.setDataCalls.filter((keys) => keys.indexOf('minuteWheelTop') >= 0);
  check('⑬量出偏差 12px：校准补写一次 scroll-top = 原落点 + 12（索引不变）',
        calCalls.length === 1
        && Math.abs(page.data.minuteWheelTop - (tappedTop + 12)) < 0.5
        && page.data.minuteWheelIndex === 20,
        'calls=' + JSON.stringify(calCalls) + ' ' + tappedTop + ' → ' + page.data.minuteWheelTop);

  selector.bias = 0;
  calls.setDataCalls.length = 0;
  page.onWheelTap({ currentTarget: { dataset: { wheel: 'minute', index: 20 } } });
  page.onWheelTouchStart({ currentTarget: { dataset: { wheel: 'minute' } } });   // 手指落下接手
  await sleep(760);
  check('⑬手指落下后校准作废（拖动中绝不补写位置）', calls.setDataCalls.length === 1,
        JSON.stringify(calls.setDataCalls));
  page.onWheelTouchEnd({ currentTarget: { dataset: { wheel: 'minute' } } });
  await sleep(260);
  selector.bias = 0;
  calls.setDataCalls.length = 0;

  /* ⑭ 累积型错位（用户 2026-11-26 补充：「错位是累积的，但是到最底部却没有错位了」）
     真机一格 pitch 与 88rpx×winW/750 的估算值不等时：i*pitch 的误差随 i 累积（第 40 格差几十像素），
     而滚到最底部时 scrollTop 被钳在 maxScroll=(N−1)*pitch，反而正好居中 —— 正是用户看到的规律。
     修法：吸附后实测 pitch 并缓存，index→位置、位置→index 都用实测值。 */
  selector.pitch = 45;                 // 真机一格 45px（估算值仍是 44px）
  page._wheelGeoms = null;             // 模拟重新开窗（缓存作废，重新量）
  page._wheelMeasure('minute', () => {});
  const geom = page._wheelGeom('minute');
  check('⑭实测几何量出真机 pitch=45（不是公式估算的 48）',
        !!geom && Math.abs(geom.pitch - 45) < 1e-6 && Math.abs(geom.c0 - 110) < 0.01,
        JSON.stringify(geom));
  calls.setDataCalls.length = 0;
  page.onWheelTap({ currentTarget: { dataset: { wheel: 'minute', index: 40 } } });
  const wantTop40 = 110 + 40 * 45 - 110;        // = 1800（pitch 45 时 pad=(220-45)/2=87.5，c0=110）
  check('⑭第 40 格落点按实测 pitch 算（1800，且不是估算的 1920）',
        Math.abs(page.data.minuteWheelTop - wantTop40) < 0.5
        && Math.abs(page.data.minuteWheelTop - 40 * ITEM_PX) > 20,
        'top=' + page.data.minuteWheelTop + '（估算值 40×48=1920）');
  scroll(page, 'minute', wantTop40);
  check('⑭位置→索引也按实测 pitch（1800.5 对应第 40 格，不是第 41 格）',
        page._wheelState('minute').idx === 40,
        'idx=' + page._wheelState('minute').idx);
  // 底部：「钳在 maxScroll」的位置也必须算成最后一格
  const nMin = page.data.minuteItems.length;
  scroll(page, 'minute', (nMin - 1) * 45);
  check('⑭滚到最底部（maxScroll）算成最后一格（用户说的「到底部反而没错位」）',
        page._wheelState('minute').idx === nMin - 1,
        'top=' + ((nMin - 1) * 45) + ' idx=' + page._wheelState('minute').idx + '/' + (nMin - 1));
  /* ⑮ 真机每格高度可能取整不一致：落点必须取「该格的实测中心」，
     而不是「c0 + i×平均 pitch」的线性外推（后者残差随 i 抖动、照样能偏几像素）。 */
  selector.pitch = 44;
  selector.jitter = 6;                  // 相邻格 ±6px 抖动（平均 pitch 仍是 44）
  page._wheelGeoms = null;
  page._wheelMeasure('minute', () => {});
  const geomJ = page._wheelGeom('minute');
  check('⑮几何缓存保留每一格的实测中心（centers 整份，长度与列一致）',
        !!geomJ && Array.isArray(geomJ.centers) && geomJ.centers.length === page.data.minuteItems.length,
        geomJ ? ('centers=' + (geomJ.centers ? geomJ.centers.length : 'none')) : 'no geom');
  calls.setDataCalls.length = 0;
  page.onWheelTap({ currentTarget: { dataset: { wheel: 'minute', index: 41 } } });
  const jitter41 = ((41 % 3) - 1) * 6;                        // = +6
  const exact41 = 88 + 41 * 44 + jitter41 + 22 - 110;         // 该格实测中心 − 半列高 = 1810
  check('⑮落点取该格实测中心（不是 c0 + 41×平均 pitch 的线性外推）',
        Math.abs(page.data.minuteWheelTop - exact41) < 0.05
        && Math.abs(page.data.minuteWheelTop - (110 + 41 * 44 - 110)) > 3,
        'top=' + page.data.minuteWheelTop + ' 精确值=' + exact41
        + ' 线性外推=' + (110 + 41 * 44 - 110));
  scroll(page, 'minute', exact41);
  check('⑮位置→索引也认实测中心（1810 找回第 41 格，线性外推会算成 41.1 再四舍五入）',
        page._wheelState('minute').idx === 41, 'idx=' + page._wheelState('minute').idx);
  selector.jitter = 0;
  selector.pitch = 44;
  page._wheelGeoms = null;
  calls.setDataCalls.length = 0;

  selector.pitch = 44;
  page._wheelGeoms = null;
  calls.setDataCalls.length = 0;

  /* ⑯ 量「半路位置」= 把错的偏差写回去（用户 2026-11-26 14:18 四张截图：偏差不累积、
     每次大小都不一样 ⇒ 不是几何公式错，而是量的时候列还没停）。
     两条铁律：① 140ms 内有 bindscroll 就不许量；② 量完按「这一刻离中心最近的那一格」补正。 */
  page.data.showScheduleTimePicker = true;
  selector.pitch = 44;
  selector.jitter = 0;
  selector.bias = 0;
  page._wheelGeoms = null;
  page._wheelMeasure('minute', () => {});
  const st16 = page._wheelState('minute');
  calls.setDataCalls.length = 0;
  scroll(page, 'minute', 30 * 44);          // 刚滚动过 ⇒ 140ms 内不许量
  page._wheelAlign('minute', 0);
  await sleep(60);
  check('⑯刚有 bindscroll 时校准不量不写（量半路位置只会把错的偏差写回去）',
        calls.setDataCalls.length === 0, JSON.stringify(calls.setDataCalls));
  await sleep(400);                          // 静止够久，让排着的重试落地
  // 位置已落在第 30 格附近（差 18px），但索引还是旧的 12
  page._liveTop.minute = 30 * 44 + 18;
  page.data.minuteWheelTop = 30 * 44 + 18;
  st16.top = page.data.minuteWheelTop;
  st16.idx = 12;
  calls.setDataCalls.length = 0;
  page._wheelAlign('minute', 0);
  await sleep(30);
  check('⑯静止后按「此刻最近的那一格」补正（旧索引 12 不许把第 12 格拉到中心）',
        Math.abs(page.data.minuteWheelTop - 30 * 44) < 0.5 && st16.idx === 30,
        'top=' + page.data.minuteWheelTop + ' idx=' + st16.idx);

  /* ⑰ 用户 2026-11-26 15:21 现场（「15 : 59」，红框在 58 上、橙带压在 59 上）：
     分钟列 01..59 共 59 格，真机一格 47.4px 比估算的 48 小一点点。
     滚到最底时按估算 round(maxScroll/48)=57 ⇒ 看得见 59、确定出来 58，「59 永远选不到」。
     实测几何 + 「最底必是最后一格」必须给出 58（值 59）。 */
  page.data.showScheduleTimePicker = true;
  selector.bias = 0;
  selector.jitter = 0;
  selector.pitch = 47.4;
  page.data.minuteItems = Array.from({ length: 59 }, (_, i) => String(i + 1).padStart(2, '0'));
  page.data.minuteWheelIndex = 0;
  page.data.minuteWheelTop = 0;
  page._liveTop.minute = 0;
  page._wheelGeoms = null;
  page._wheelMeasure('minute', () => {});
  const g17 = page._wheelGeom('minute');
  const maxScroll17 = (g17 && g17.maxScroll) || 0;
  calls.setDataCalls.length = 0;
  scroll(page, 'minute', maxScroll17);
  const idx17 = page._wheelState('minute').idx;
  check('⑰真机一格比估算小一点时，滚到最底必须判最后一格（否则看得见 59、确定得 58）',
        Math.round(maxScroll17 / ITEM_PX) === 57
        && idx17 === 58 && page.data.minuteItems[idx17] === '59',
        'idx=' + idx17 + ' value=' + page.data.minuteItems[idx17]
        + '（按估算会得 ' + Math.round(maxScroll17 / ITEM_PX) + '）maxScroll=' + maxScroll17);
  check('⑰最后一格的落点正好是 maxScroll（写更大只会被内核钳住，不许写超）',
        Math.abs(page._wheelTarget('minute', 58) - maxScroll17) < 0.01,
        'target=' + page._wheelTarget('minute', 58) + ' maxScroll=' + maxScroll17);
  // 复位，别把 47.4 的几何带进后面的用例
  selector.pitch = 48;
  page._wheelGeoms = null;
  page.data.minuteItems = Array.from({ length: 60 }, (_, i) => String(i).padStart(2, '0'));
  page.data.minuteWheelIndex = 0;
  page.data.minuteWheelTop = 0;
  page._liveTop.minute = 0;
  page._wheelMeasure('minute', () => {});

  /* ⑪ 非法事件不炸、不写 */
  calls.setDataCalls.length = 0;
  let threw = false;
  try {
    page.onWheelScroll({});
    page.onWheelScroll({ currentTarget: {} });
    page.onWheelScroll({ currentTarget: { dataset: { wheel: 'nope' } }, detail: {} });
    page.onWheelTap({ currentTarget: { dataset: {} } });
    page.onWheelTap({ currentTarget: { dataset: { wheel: 'hour', index: 'x' } } });
    page.onWheelTouchStart({});
    page.onWheelTouchEnd({});
  } catch (e) { threw = true; }
  check('⑪非法/空事件：不抛异常、0 次 setData', !threw && calls.setDataCalls.length === 0,
        JSON.stringify(calls.setDataCalls));

  /* ⑫ 变异自证：把旧病根（滚动中同步 setData）装进克隆，本判据必须打红 */
  const buggy = Object.assign(Object.create(Object.getPrototypeOf(page)), page);
  buggy.data = JSON.parse(JSON.stringify(page.data));
  buggy.setData = page.setData.bind(buggy);
  buggy.onWheelScroll = function (e) {
    const name = e.currentTarget.dataset.wheel;
    this.setData({ [name + 'WheelTop']: e.detail.scrollTop });
  };
  calls.setDataCalls.length = 0;
  buggy.onWheelTouchStart({ currentTarget: { dataset: { wheel: 'minute' } } });
  for (let i = 0; i < 5; i++) buggy.onWheelScroll({ currentTarget: { dataset: { wheel: 'minute' } }, detail: { scrollTop: i * 3 } });
  check('⑫变异自证：滚动里 setData 的老写法会被「0 次 setData」判据抓到',
        calls.setDataCalls.length > 0, calls.setDataCalls.length + ' 次（应当 >0）');
}

(async () => {
  runStatic();
  await runBehavior();
  console.log('\n' + '='.repeat(52));
  if (failures) {
    console.log(`时间滚轮「scroll-view 自研滚轮」验收: ${failures} 项失败 ❌`);
    process.exit(1);
  }
  console.log('时间滚轮「scroll-view 自研滚轮」验收: 全部通过 ✅（真机手感仍需人眼确认）');
  process.exit(0);
})();
