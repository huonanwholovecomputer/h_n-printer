/* eslint-disable */
/**
 * 滚动几何自愈验收（2026-10-25 现场：小程序「提交打印」滚到最顶端内容位置不对、
 * 滚到最底端多出一大块空白，偶发）
 *
 * 独立诊断结论（_tmp_scroll/诊断报告.md，Chromium 真几何 + 真实 scroll.wxs 探针）：
 *   · 顶端错位 K = 222.4px（无设备卡）/ 386.9px（2 台设备卡）——**正好等于整块
 *     「header-area + status-bar [+ 设备卡]」** ⇒ 不是页头塌陷（页头最小 92.95px，
 *     实测只剩 1.0px），而是 `.scroll-content` 的内联 `translateY` 与 WXS `state.y`
 *     失同步（H5）：`ensureState()` 把 y 归零却不重绘，屏幕上仍停着上一帧的位移。
 *   · 底端留白 D = 532.7px —— 逻辑层 `maxY` 是**一次性快照**（H1），之后内容变短
 *     （2 个 word-grid 文件列表塌陷 521.0px / 设备卡消失 153.3px）没人重测。
 *   否掉：H2（padding 只值 26.9~35.8px）、H3（方向相反，偏小 117px）、H7（vp 与可见区一致）。
 *
 * 修法（本脚本逐条钉住，且都做变异自证 —— 判据必须能红）：
 *   A. 视图层 `syncGeometry()`：**手势入口**用真实 rect 重算 maxY
 *      （= 内容高 - 视口高 + padY），并把 y 钳回 [minY, maxY] ⇒ 底端留白自愈。
 *      只在 touchstart 量（微信的 getBoundingClientRect 只在 touch 回调里保证是当前值，
 *      change:prop 里可能给旧帧 —— 旧值会把 maxY 写错）。
 *   B. `onConfig` 每次都重绘（逻辑层推 `rev` 自增，值不变也会触发）⇒ H5 失同步收敛。
 *   C. 逻辑层补上可证的漏测点：设备卡张数变化、提交成功收列表、logo 解码、
 *      切 tab 回来（pageLifetimes.show）；`_measure` 加 vp/ch 闸门（隐藏页 vp=0 会
 *      一次污染 +一整屏）。
 *
 * 用法：node tests/verify_scroll_geometry_selfheal.js      （exit 0 = 全部通过）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..');
const MP = path.join(REPO, 'mobile_apps', 'h_n_print');
const WXS = path.join(MP, 'utils', 'scroll.wxs');
const INDEX_JS = path.join(MP, 'pages', 'index', 'index.js');
const INDEX_WXML = path.join(MP, 'pages', 'index', 'index.wxml');
const ME_JS = path.join(MP, 'pages', 'me', 'me.js');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }
function read(p) { return fs.readFileSync(p, 'utf8'); }

/* ---------------- 加载真实 WXS（可传入变异后的源码） ---------------- */
function loadWxs(src) {
  const sandbox = {
    console,
    Math, JSON, String, Number, Object, Array, isNaN, parseInt, parseFloat,
    getDate: () => ({ getTime: () => Date.now() }),
    module: { exports: {} },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src || read(WXS), sandbox, { filename: 'scroll.wxs' });
  return sandbox.module.exports;
}

/* ---------------- 桩 ownerInstance：几何可调、记录每次读写 ---------------- */
function makeOwner(engine, geom) {
  const state = {};
  const log = { transforms: [], rectReads: 0 };
  const g = Object.assign({ vp: 600, ch: 2500, domY: 0, rects: true }, geom || {});
  const scroller = g.rects
    ? { getBoundingClientRect: () => { log.rectReads++; return { top: 100, height: g.vp }; } }
    : {};
  const content = {
    setStyle: (s) => { if (s && s.transform !== undefined) log.transforms.push(s.transform); },
  };
  if (g.rects) {
    content.getBoundingClientRect = () => {
      log.rectReads++;
      return { top: 100 - g.domY, height: g.ch };
    };
  }
  const owner = {
    getState: () => state,
    requestAnimationFrame: () => 0,        // 不跑物理循环，只断言拖拽期/配置期的状态
    callMethod: () => {},
    selectComponent: (sel) => {
      if (sel === '.scroller') return scroller;
      if (sel === '.scroll-content') return content;
      if (sel === '.logo-wrap' || sel === '.header-area') return { setStyle() {} };
      return null;
    },
  };
  owner._log = log;
  owner._geom = g;
  return owner;
}

function touch(engine, owner, type, clientY) {
  const ev = type === 'touchend'
    ? { touches: [] }
    : { touches: [{ identifier: 1, clientX: 100, clientY }] };
  return engine[type](ev, owner);
}

const CFG = {
  rev: 1, minY: 0, maxY: 2022, scrollerH: 600, contentH: 2500, padPx: 122,
  listOverflow: false, listAtTop: true, listAtBottom: true,
};

/* ============================================================
   A. 视图层：syncGeometry 在手势入口用真实几何重算 maxY
   ============================================================ */
section('A. 视图层几何自愈（真实 utils/scroll.wxs）');
const engine = loadWxs();
check('scroll.wxs 导出 onConfig / touchstart（引擎可加载）',
      typeof engine.onConfig === 'function' && typeof engine.touchstart === 'function');

/* A1 配置路径**不**读 rect（微信里 change:prop 期间取值可能是旧帧/0，旧值会把 maxY 写错） */
{
  const owner = makeOwner(engine, { vp: 600, ch: 2500 });
  owner._log.rectReads = 0;
  engine.onConfig(CFG, null, owner, null);
  check('A1 onConfig 不调用 getBoundingClientRect（只在手势里量）',
        owner._log.rectReads === 0, 'rectReads=' + owner._log.rectReads);
  check('A1b onConfig 采信逻辑层推来的 maxY', owner.getState().maxY === 2022,
        'maxY=' + owner.getState().maxY);
}

/* A2 底端留白自愈：内容从 2500 塌到 1600（删掉两个 word-grid 文件），
      逻辑层还停在旧 maxY=2022；下一次 touchstart 必须用真实几何改回 1122 */
{
  const owner = makeOwner(engine, { vp: 600, ch: 2500, domY: 2022 });
  engine.onConfig(CFG, null, owner, null);
  const st = owner.getState();
  st.y = 2022;                              // 停在旧底端（现场：底下多出 500px 空白）
  owner._geom.ch = 1600;                    // 内容塌陷，logic 层还没来得及重测
  owner._log.transforms.length = 0;
  touch(engine, owner, 'touchstart', 400);
  check('A2 touchstart 用真实内容高重算 maxY（2500→1600 ⇒ 2022→1122）',
        Math.round(st.maxY) === 1122, 'maxY=' + st.maxY);
  check('A2b 越界的 y 被钳回新底端（不再停在 2022）',
        Math.round(st.y) === 1122, 'y=' + st.y);
  touch(engine, owner, 'touchend', 400);
}

/* A3 内容变长 → 底端重新可达 */
{
  const owner = makeOwner(engine, { vp: 600, ch: 1600, domY: 0 });
  engine.onConfig(Object.assign({}, CFG, { maxY: 1122, contentH: 1600 }), null, owner, null);
  const st = owner.getState();
  owner._geom.ch = 3000;
  touch(engine, owner, 'touchstart', 400);
  check('A3 内容变长：touchstart 把 maxY 抬到真实值（3000-600+122=2522）',
        Math.round(st.maxY) === 2522, 'maxY=' + st.maxY);
  touch(engine, owner, 'touchend', 400);
}

/* A4 老页面配置没有 padPx：从 maxY 反推一次 padY，之后照样能重算 */
{
  const cfg = Object.assign({}, CFG);
  delete cfg.padPx;
  const owner = makeOwner(engine, { vp: 600, ch: 2500 });
  engine.onConfig(cfg, null, owner, null);
  const st = owner.getState();
  check('A4 无 padPx 的老配置：从 maxY 反推 padY（2022-(2500-600)=122）',
        Math.round(st.padY) === 122, 'padY=' + st.padY);
  owner._geom.ch = 1600;
  touch(engine, owner, 'touchstart', 400);
  check('A4b 反推出的 padY 也参与重算（1600-600+122=1122）',
        Math.round(st.maxY) === 1122, 'maxY=' + st.maxY);
  touch(engine, owner, 'touchend', 400);
}

/* A5 量不到节点（桩环境/首帧）→ 保留逻辑层快照，不崩、不写 0 */
{
  const owner = makeOwner(engine, { rects: false });
  engine.onConfig(CFG, null, owner, null);
  const st = owner.getState();
  touch(engine, owner, 'touchstart', 400);
  check('A5 拿不到 rect 时保留推来的 maxY（不把范围写坏）',
        Math.round(st.maxY) === 2022, 'maxY=' + st.maxY);
  touch(engine, owner, 'touchend', 400);
}

/* A6 视觉位移与 state.y 失同步（H5 现场形状）：新 state（y=0）但内联 transform 还是
      -386.9（整块「页头+状态条+设备卡」）。任何一次配置推送都必须把位移重绘回 state.y。 */
{
  const owner = makeOwner(engine, { vp: 600, ch: 2500, domY: 386.9 });
  engine.onConfig(Object.assign({}, CFG, { rev: 7 }), null, owner, null);
  const st = owner.getState();
  const last = owner._log.transforms[owner._log.transforms.length - 1];
  check('A6 失同步：onConfig 重绘把 transform 拉回 state.y=0（不是 -386.9）',
        Math.round(st.y) === 0 && last === 'translateY(0px)',
        'y=' + st.y + ' transform=' + last);
}

/* A7 同值重推（rev 变、其余不变）也必须重绘一笔 —— 这是 H5 收敛的唯一入口 */
{
  const owner = makeOwner(engine, { vp: 600, ch: 2500 });
  engine.onConfig(Object.assign({}, CFG, { rev: 1 }), null, owner, null);
  owner._log.transforms.length = 0;
  engine.onConfig(Object.assign({}, CFG, { rev: 2 }), null, owner, null);
  check('A7 rev 自增（其余字段相同）也会触发一次重绘',
        owner._log.transforms.length > 0, 'writes=' + owner._log.transforms.length);
}

/* A8 负向越界钳回（回弹残留/状态被重置到负数） */
{
  const owner = makeOwner(engine, { vp: 600, ch: 2500 });
  engine.onConfig(CFG, null, owner, null);
  owner.getState().y = -50;
  engine.onConfig(Object.assign({}, CFG, { rev: 9 }), null, owner, null);
  check('A8 静止态 y<minY 被钳回 0', Math.round(owner.getState().y) === 0,
        'y=' + owner.getState().y);
}

/* ============================================================
   B. 变异自证：把修法删掉 ⇒ A2/A6 必须转红
   ============================================================ */
section('B. 变异自证（判据能红）');
{
  const src = read(WXS);
  const mutated = src.replace(
    /syncGeometry\(state, ownerInstance\);\s*\n\s*if \(state\.y > state\.maxY\) state\.y = state\.maxY;/,
    'if (state.y > state.maxY) state.y = state.maxY;');
  check('B0 变异锚点命中（touchstart 的 syncGeometry 能被摘掉）',
        mutated !== src, 'changed=' + (mutated !== src));
  const eng2 = loadWxs(mutated);
  const owner = makeOwner(eng2, { vp: 600, ch: 2500, domY: 2022 });
  eng2.onConfig(CFG, null, owner, null);
  const st = owner.getState();
  st.y = 2022;                              // 与 A2 同一现场：停在旧底端
  owner._geom.ch = 1600;
  touch(eng2, owner, 'touchstart', 400);
  check('★ B1 摘掉 syncGeometry ⇒ maxY 停在旧的 2022（A2 判据能红）',
        Math.round(st.maxY) === 2022, 'maxY=' + st.maxY);
  check('★ B1b 摘掉 syncGeometry ⇒ y 仍停在 2022（底端留白不会自愈）',
        Math.round(st.y) === 2022, 'y=' + st.y);
  touch(eng2, owner, 'touchend', 400);
}
{
  const src = read(WXS);
  // 把 onConfig 末尾的重绘去掉（H5 收敛入口）
  const mutated = src.replace(
    /      \/\/ 内容重新变长：中止进行中的收尾弹簧（否则会继续向旧目标移动）\n      if \(state\.spring && state\.spring\.settle\) \{\n        state\.spring = null;\n        state\.running = false;\n      \}\n      applyRender\(state, ownerInstance, renderY\(state\)\);/,
    '      if (state.spring && state.spring.settle) {\n        state.spring = null;\n        state.running = false;\n      }');
  check('B2 变异锚点命中（onConfig 的重绘能被摘掉）', mutated !== src);
  const eng3 = loadWxs(mutated);
  const owner = makeOwner(eng3, { vp: 600, ch: 2500, domY: 386.9 });
  eng3.onConfig(CFG, null, owner, null);
  const last = owner._log.transforms[owner._log.transforms.length - 1];
  check('★ B3 摘掉重绘 ⇒ 失同步的位移不会被纠正（A6 判据能红）',
        last !== 'translateY(0px)', 'transform=' + last);
}

/* ============================================================
   C. 逻辑层补测点 / 闸门（真实页面源码）
   ============================================================ */
section('C. 逻辑层接线（index.js / me.js / index.wxml）');
const idx = read(INDEX_JS);
const me = read(ME_JS);
const wxml = read(INDEX_WXML);

check('C1 首页 scrollConfig 带自增 rev（值不变也能触发观察器）',
      /this\._scrollRev = \(this\._scrollRev \|\| 0\) \+ 1/.test(idx)
      && /scrollConfig: \{\s*\n\s*rev: this\._scrollRev,/.test(idx));
check('C2 首页 scrollConfig 带 padPx（底部留白显式下发）',
      /padPx: \(this\._bottomPad \|\| 0\) \+ \(this\._tabOverlayPx \|\| 0\),/.test(idx));
check('C3 首页 _measure 加 vp/ch 闸门（隐藏页 vp=0 不再污染一整屏）',
      /if \(!\(vp > 100\) \|\| !\(ch > 0\)\) return/.test(idx));
check('C4 首页：设备卡张数变化后重测（可证的留白来源之一）',
      /if \(_devCountBefore !== devices\.length\) this\._scheduleMeasure\(200\)/.test(idx));
check('C5 首页：提交成功后列表收起要重测',
      /this\._animateFileListHeight\(0, 300\)\s*\n\s*this\._scheduleMeasure\(400\)/.test(idx));
check('C6 首页：logo 解码后重测（widthFix 解码前高 0 会漏整块页头）',
      /onLogoLoad\(\) \{/.test(idx) && /bindload="onLogoLoad"/.test(wxml));
check('C7 首页：pageLifetimes.show 会补测（切 tab 回来先对齐边界）',
      /回到本页先补测一次滚动边界/.test(idx));
check('C8 「我」页 scrollConfig 带 rev + padPx（同一套引擎同一口径）',
      /scrollConfig: \{\s*\n\s*rev: this\._scrollRev,/.test(me)
      && /padPx: \(this\._bottomPad \|\| 0\) \+ \(this\._tabOverlayPx \|\| 0\),/.test(me));
check('C9 「我」页 _measure 闸门 + show 补测',
      /if \(!\(vp > 100\) \|\| !\(ch > 0\)\) return/.test(me)
      && /回到本页先补测一次滚动边界/.test(me));

/* C10 变异：删掉设备卡重测那行 ⇒ C4 立刻转红（判据不是永真） */
{
  const mutated = idx.replace(
    '          if (_devCountBefore !== devices.length) this._scheduleMeasure(200)\n', '');
  check('★ C10 变异（删掉设备卡重测）⇒ C4 判据转红',
        !/if \(_devCountBefore !== devices\.length\) this\._scheduleMeasure\(200\)/.test(mutated));
}

console.log('\n' + (failures ? `❌ 有失败（${failures} 条）` : '✅ 全部通过'));
process.exit(failures ? 1 : 0);
