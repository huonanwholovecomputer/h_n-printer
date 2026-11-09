/* eslint-disable */
/**
 * 小程序「内层文件列表 ↔ 页面」滚动接力验收（手势序列级）
 *
 * 用户原话（2026-11-09）：「APP 端滑内部列表时整个界面也在滑；小程序端内部列表滑到底/顶时
 * 无法让整个界面接管。这个问题已经持续了将近 1 个月。打印界面的文件列表也是同样的道理。」
 *
 * 本轮把内层列表从「原生 scroll-view + catchtouchmove 兜住」改成「WXS 直驱的 view 框 + track」：
 * 手势在视图层逐帧结算，内层先吃它还能走的行程，剩下的余量当帧就给页面；反向外层到顶再把
 * 手势交还内层。逻辑层不再参与贴边判定（bindscroll → setData → change:prop 三跳就是死区）。
 *
 * 本脚本只管**手势序列级**的行为（一次完整滑动的手感）：逐帧位移、贴边那一帧的余量、
 * 反向交还、抬手惯性归属、同页多卡不串。单帧判据/结构接线在 verify_nested_scroll_handoff.js。
 *
 * 用法：node tests/verify_wechat_nested_scroll.js   （exit 0 = 全绿）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..', '..');
const H5 = path.join(REPO, 'mobile_apps', 'h_n_print');
const WXS = path.join(H5, 'utils', 'scroll.wxs');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (detail ? '  -- ' + detail : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }

/* ---------------- 引擎桩 ---------------- */
function loadWxsEngine() {
  const src = fs.readFileSync(WXS, 'utf8');
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
      top: top, bottom: top + height, height: height, left: 0, right: 375, width: 375,
    }),
    selectComponent: (sel) => (/track$/.test(String(sel || '')) ? body : null),
    _body: body,
  };
}

function makeWxsOwner(engine, cfg, boxes) {
  const state = {};
  const rendered = [];
  const ns = (cfg && cfg.nestedSel) || '.detail-files-scroll';
  const content = { setStyle: (s) => rendered.push(s.transform) };
  const owner = {
    getState: () => state,
    requestAnimationFrame: (cb) => (owner._rafs.push(cb), owner._rafs.length),
    callMethod: () => {},
    selectComponent: (sel) => {
      if (sel === '.scroll-content') return content;
      if (sel === ns) return (boxes || [])[0] || null;
      return null;
    },
    selectAllComponents: (sel) => (sel === ns ? (boxes || []) : []),
    _rafs: [],
  };
  engine.onConfig(cfg, null, owner, null);
  return owner;
}

function ev(y) { return { touches: [{ identifier: 1, clientX: 100, clientY: y }] }; }

/* 一次完整手势：按 deltas（手指位移，负数=上滑）逐帧喂 touchmove，返回末帧触点 y */
function drag(engine, owner, startY, deltas, onFrame) {
  let y = startY;
  engine.touchstart(ev(y), owner);
  for (let i = 0; i < deltas.length; i++) {
    y += deltas[i];
    engine._clock.t += 16;                 // 真机上相邻 touchmove 有真实时间间隔，桩里补上
    engine.touchmove(ev(y), owner);
    if (onFrame) onFrame(i);
  }
  return y;
}
function lift(engine, owner, y) { engine._clock.t += 16; engine.touchend({ touches: [] }, owner); }

function runFrames(owner, engine, n) {
  for (let i = 0; i < n && owner._rafs.length; i++) {
    engine._clock.t += 16;
    const cbs = owner._rafs;
    owner._rafs = [];
    cbs.forEach((cb) => cb());
  }
}

function pageCfg(extra) {
  return Object.assign({
    minY: 0, maxY: 1200, scrollerH: 800, contentH: 2000,
    nestedSel: '.detail-files-scroll', nestedTrackSel: '.detail-files-track',
  }, extra || {});
}

/* ---------------- 一、手势序列 ---------------- */
function runGestureTests() {
  section('一、「我」页订单详情：6 个文件（内层只有 46px 行程）');

  const engine = loadWxsEngine();
  const box = makeBox(200, 250, 296, 0);   // 5 行可视、6 行内容 ⇒ 行程 46px
  const owner = makeWxsOwner(engine, pageCfg(), [box]);
  const st = owner.getState();
  engine.touchstart(ev(300), owner);   // 手势入口才量框（配置注入时 nb 本来就是 null）
  check('手势入口量到内层行程 46px', !!st.nb && Math.round(st.nb.maxY) === 46,
        st.nb ? 'maxY=' + st.nb.maxY : 'nb=null');

  const seen = [];
  const endY = drag(engine, owner, 300, [-10, -10, -10, -10, -10, -10], () => {
    seen.push({ inner: Math.round(box._body._y), page: Math.round(st.y) });
  });
  check('前 40px 全给内层，页面一动不动',
        JSON.stringify(seen.slice(0, 4)) === JSON.stringify([
          { inner: 10, page: 0 }, { inner: 20, page: 0 },
          { inner: 30, page: 0 }, { inner: 40, page: 0 },
        ]), JSON.stringify(seen.slice(0, 4)));
  check('贴底那一帧只把「剩下的 4px」交给页面（不是整帧搬过去）',
        seen[4].inner === 46 && seen[4].page === 4, JSON.stringify(seen[4]));
  check('内层滚不动的后续帧，页面按手指位移 1:1 跟手',
        seen[5].inner === 46 && seen[5].page === 14, JSON.stringify(seen[5]));
  check('整段手势内层没有超过自身行程', box._body._y === 46, String(box._body._y));
  lift(engine, owner, endY);

  section('二、「我」页订单详情：20 个文件（内层行程很大）');
  const engine2 = loadWxsEngine();
  const box2 = makeBox(200, 250, 987, 0);   // 行程 737px
  const owner2 = makeWxsOwner(engine2, pageCfg(), [box2]);
  const st2 = owner2.getState();
  let pageMoved = false;
  const y2 = drag(engine2, owner2, 300, new Array(12).fill(-10), () => {
    if (Math.abs(st2.y) > 0.5) pageMoved = true;
  });
  check('内层够走的 120px 里，页面位移恒为 0（不再内外同滚）',
        !pageMoved && Math.round(box2._body._y) === 120 && st2.y === 0,
        'page=' + st2.y + ' inner=' + box2._body._y);

  section('三、内层到底后继续滑：接力给页面，位移不跳变');
  const engine3 = loadWxsEngine();
  const box3 = makeBox(200, 250, 296, 0);   // 46px
  const owner3 = makeWxsOwner(engine3, pageCfg(), [box3]);
  const st3 = owner3.getState();
  const pageSteps = [];
  const y3 = drag(engine3, owner3, 300, new Array(10).fill(-10), () => pageSteps.push(st3.y));
  // pageSteps: 0,0,0,0,4,14,24,34,44,54
  let monotone = true;
  for (let i = 1; i < pageSteps.length; i++) if (pageSteps[i] < pageSteps[i - 1] - 0.01) monotone = false;
  const tail = pageSteps.slice(5);
  const evenTail = tail.every((v, i) => i === 0 || Math.abs((v - tail[i - 1]) - 10) < 0.001);
  check('页面位移逐帧单调递增（不会先回弹再往下）', monotone, JSON.stringify(pageSteps));
  check('贴边之后每帧严格等于手指位移（10px/帧，不失真、不加速）', evenTail, JSON.stringify(tail));
  lift(engine3, owner3, y3);

  section('四、反向：页面在底部时下滑，先让页面滚回，页面到顶再交还内层');
  const engine4 = loadWxsEngine();
  const box4 = makeBox(200, 250, 296, 0);
  const owner4 = makeWxsOwner(engine4, pageCfg({ maxY: 300 }), [box4]);
  const st4 = owner4.getState();
  st4.y = 120;                               // 页面停在中间（相当于上次手势留下的位置）
  st4.nb = null;
  drag(engine4, owner4, 300, new Array(4).fill(10), () => {});   // 下滑 40px
  check('页面被外层吃回（120 → 80），内层不动', Math.round(st4.y) === 80 && box4._body._y === 0,
        'page=' + st4.y + ' inner=' + box4._body._y);
  const engine4b = loadWxsEngine();
  const box4b = makeBox(200, 250, 296, 40);   // 内层已滚到 40px，页面停在顶部
  const owner4b = makeWxsOwner(engine4b, pageCfg({ maxY: 300 }), [box4b]);
  const st4b = owner4b.getState();
  drag(engine4b, owner4b, 300, new Array(3).fill(10), () => {});   // 下划 30px
  check('页面到顶后，下滑交给内层（track 40 → 10，页面停在 0）',
        Math.round(box4b._body._y) === 10 && st4b.y === 0,
        'page=' + st4b.y + ' inner=' + box4b._body._y);

  section('五、抬手惯性：谁吃下最后一帧，惯性就归谁');
  const engine5 = loadWxsEngine();
  const box5 = makeBox(200, 250, 600, 0);    // 行程 350px
  const owner5 = makeWxsOwner(engine5, pageCfg(), [box5]);
  const st5 = owner5.getState();
  const y5 = drag(engine5, owner5, 300, [-15, -15, -15, -15]);   // 上滑 60px（速度中等）
  check('手势内只有内层在动', Math.round(box5._body._y) === 60 && st5.y === 0,
        'inner=' + box5._body._y + ' page=' + st5.y);
  lift(engine5, owner5, y5);
  check('抬手后惯性挂在内层（nbRun=true、外层速度归零）', st5.nbRun === true && st5.vel === 0,
        'nbRun=' + st5.nbRun + ' vel=' + st5.vel);
  const pageAfter = [];
  for (let i = 0; i < 6; i++) { runFrames(owner5, engine5, 1); pageAfter.push(Math.round(st5.y)); }
  check('内层惯性阶段页面保持不动', pageAfter.every((v) => v === 0), JSON.stringify(pageAfter));
  runFrames(owner5, engine5, 200);
  check('内层惯性撞底后余速交给页面（页面继续滑）', st5.y > 0, 'page=' + st5.y);

  section('六、同页多张卡：按触点选框，不串卡');
  const engine6 = loadWxsEngine();
  const a = makeBox(200, 250, 400, 0);
  const b = makeBox(600, 250, 400, 0);
  const owner6 = makeWxsOwner(engine6, pageCfg(), [a, b]);
  const st6 = owner6.getState();
  drag(engine6, owner6, 700, [-10, -10, -10], () => {});   // 触点落在第二张卡
  check('只滚第二张卡的列表（A 不动、B +30）', a._body._y === 0 && b._body._y === 30,
        'A=' + a._body._y + ' B=' + b._body._y);

  section('七、内层不可滚 / 触点不在框上 / 慢速拖动');
  const engine7 = loadWxsEngine();
  const flat = makeBox(200, 250, 250, 0);   // 内容高 = 框高，行程 0
  const owner7 = makeWxsOwner(engine7, pageCfg(), [flat]);
  const st7 = owner7.getState();
  drag(engine7, owner7, 300, [-20], () => {});
  check('内层放得下时第一帧就由页面接管（不白吃一帧）', st7.y === 20 && flat._body._y === 0,
        'page=' + st7.y);

  const engine8 = loadWxsEngine();
  const box8 = makeBox(200, 250, 400, 0);
  const owner8 = makeWxsOwner(engine8, pageCfg(), [box8]);
  const st8 = owner8.getState();
  drag(engine8, owner8, 100, [-20], () => {});   // 触点在内层框上方（标题区）
  check('触点不在内层框上 ⇒ 直接滚页面', st8.y === 20 && box8._body._y === 0,
        'page=' + st8.y);

  const engine9 = loadWxsEngine();
  const box9 = makeBox(200, 250, 400, 0);
  const owner9 = makeWxsOwner(engine9, pageCfg(), [box9]);
  drag(engine9, owner9, 300, [-10, -0.3, -0.3, -0.3], () => {});
  check('慢速拖动（<0.5px/帧）也由内层吃，不漏进外层',
        Math.abs(box9._body._y - 10.9) < 0.01 && owner9.getState().y === 0,
        'inner=' + box9._body._y + ' page=' + owner9.getState().y);
}

/* ---------------- 二、源码接线 ---------------- */
function runSourceChecks() {
  section('二、源码接线（三页都不再有原生内层滚动 / 不再有 flag 同步）');
  const read = (p) => fs.readFileSync(path.join(H5, 'pages', p), 'utf8');
  const meWxml = read('me/me.wxml');
  const uoWxml = read('user-orders/user-orders.wxml');
  const idxWxml = read('index/index.wxml');
  const idxJs = read('index/index.js');
  const wxs = fs.readFileSync(WXS, 'utf8');

  check('me：内层是 view.detail-files-scroll + view.detail-files-track',
        /class="detail-files-scroll"/.test(meWxml) && /class="detail-files-track"/.test(meWxml)
        && !/<scroll-view[^>]*detail-files-scroll/.test(meWxml));
  check('user-orders：同上', /class="detail-files-scroll"/.test(uoWxml)
        && /class="detail-files-track"/.test(uoWxml)
        && !/<scroll-view[^>]*detail-files-scroll/.test(uoWxml));
  check('index：内层是 view.file-list-scroll + view.file-list-track',
        /class="file-list-scroll"/.test(idxWxml) && /class="file-list-track"/.test(idxWxml)
        && !/<scroll-view[^>]*file-list-scroll/.test(idxWxml));
  check('index：不再有 bindscroll 贴边同步',
        !/bindscroll="onFileListScroll"/.test(idxWxml) && !/_refreshListEdges/.test(idxJs));
  const innerTag = (src, cls) => {
    const m = new RegExp('<view class="' + cls + '"[^>]*>').exec(src);
    return m ? m[0] : '';
  };
  check('三页都不再 catchtouchmove 兜内层（手势交给 WXS 逐帧结算）',
        !!innerTag(meWxml, 'detail-files-scroll') && !/catchtouchmove/.test(innerTag(meWxml, 'detail-files-scroll'))
        && !!innerTag(uoWxml, 'detail-files-scroll') && !/catchtouchmove/.test(innerTag(uoWxml, 'detail-files-scroll'))
        && !!innerTag(idxWxml, 'file-list-scroll') && !/catchtouchmove/.test(innerTag(idxWxml, 'file-list-scroll')));
  check('WXS 不再有 listOverflow / listAtTop / listAtBottom 消费',
        !/newValue\.listOverflow/.test(wxs) && !/newValue\.listAtTop/.test(wxs)
        && !/newValue\.listAtBottom/.test(wxs));
  check('WXS 用 selectAllComponents 逐个量框（不是只认第一个）',
        /selectAllComponents/.test(wxs) && /function measureNested/.test(wxs));
}

runGestureTests();
runSourceChecks();

console.log('\n' + '='.repeat(52));
if (failures) {
  console.log('小程序嵌套滚动接力验收: 存在 ' + failures + ' 项失败 ❌');
  process.exit(1);
}
console.log('小程序嵌套滚动接力验收: 全部通过 ✅');
process.exit(0);
