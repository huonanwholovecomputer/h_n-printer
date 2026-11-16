/* eslint-disable */
/**
 * 小程序底部 Tab 栏「玻璃滑块 + 拖动手势」验收
 *
 * 用户原话（m01291）：「好，现在来考虑小程序的tab栏和界面切换，小程序应该没办法和APP的效果
 * 做的完全一致，你看看最多能做成怎么样」→ 评估后用户选「A+B」：
 *   A 档：玻璃胶囊外观、点击 160ms 平移、深色黑玻璃、按下船式起伏、整排 wave
 *   B 档：拖滑块 / 拖整条栏跟手（85ms 滞后、越界橡皮筋、整条栏位移 ≤10px）、松手按位置切页
 *
 * 小程序没有 APP 那套「单页双 pane」，两条做不到：拖动时页面跟手位移、转场中反向调头
 * （每个 tab 页各持一份 tabBar 实例；wx.switchTab 原子）。切页仍是「旧页先播退出动画
 * 160ms → 200ms 后 switchTab」，本脚本钉的是这之内的手感与口径。
 *
 * 用法：node other/tests/verify_wechat_tabbar.js   （exit 0 = 全绿）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..', '..');
const H5 = path.join(REPO, 'mobile_apps', 'h_n_print');
const TAB_DIR = path.join(H5, 'custom-tab-bar');
const WXS = path.join(H5, 'utils', 'tabbar.wxs');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (detail ? '  -- ' + detail : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }

/* ---------------- 引擎桩 ---------------- */
function loadEngine() {
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
  vm.runInContext(src, sandbox, { filename: 'tabbar.wxs' });
  const exp = sandbox.module.exports;
  exp._clock = clock;
  return exp;
}

function makeOwner(engine, cfg) {
  const state = {};
  const rafs = [];
  const logs = [];
  const el = (key) => ({ setStyle: (s) => logs.push({ key, s }) });
  const els = { '.tab-thumb': el('thumb'), '.tab-bar': el('bar'), '.tab-glass': el('glass') };
  const calls = [];
  const owner = {
    getState: () => state,
    requestAnimationFrame: (cb) => (owner._rafs.push(cb), owner._rafs.length),
    callMethod: (name, arg) => calls.push({ name, arg }),
    selectComponent: (sel) => els[sel] || null,
    _rafs: rafs,
    _logs: logs,
    _calls: calls,
  };
  if (cfg) engine.onConfig(cfg, null, owner);
  return owner;
}

function lastStyle(owner, key) {
  for (let i = owner._logs.length - 1; i >= 0; i--) {
    if (owner._logs[i].key === key) return owner._logs[i].s;
  }
  return null;
}
function pillX(owner) {
  const s = lastStyle(owner, 'thumb');
  if (!s || !s.transform) return NaN;
  const m = /translate3d\((-?[\d.]+)px,\s*0,\s*0\)/.exec(s.transform);
  return m ? parseFloat(m[1]) : NaN;
}
function barX(owner) {
  const s = lastStyle(owner, 'bar');
  if (!s || !s.transform) return NaN;
  const m = /translate3d\((-?[\d.]+)px,\s*0,\s*0\)/.exec(s.transform);
  return m ? parseFloat(m[1]) : NaN;
}

function runFrames(owner, engine, n) {
  for (let i = 0; i < n && owner._rafs.length; i++) {
    engine._clock.t += 16;
    const cbs = owner._rafs;
    owner._rafs = [];
    cbs.forEach((cb) => cb());
  }
}

function touch(x, y, index) {
  return { touches: [{ identifier: 1, clientX: x, clientY: y }], currentTarget: { dataset: { index: index === undefined ? 0 : index } } };
}
const lift = { touches: [] };

/* 375 宽真机口径：barW 295、slot 147.5、inset 4.5px、over 0.0305 */
const GEO = { step: 147.5, maxIdx: 1, over: 4.5 / 147.5, sel: 0 };

/* ---------------- 一、契约（源码 / 样式） ---------------- */
function runContractTests() {
  section('一、结构契约：两层两元素、外观与高度');
  const wxml = fs.readFileSync(path.join(TAB_DIR, 'index.wxml'), 'utf8');
  const wxss = fs.readFileSync(path.join(TAB_DIR, 'index.wxss'), 'utf8');
  const js = fs.readFileSync(path.join(TAB_DIR, 'index.js'), 'utf8');
  const wxs = fs.readFileSync(WXS, 'utf8');

  check('wxml 引入 WXS 引擎并挂 change:cfg',
        /<wxs module="tabbar" src="\.\.\/utils\/tabbar\.wxs"><\/wxs>/.test(wxml)
        && /cfg="\{\{cfg\}\}"/.test(wxml) && /change:cfg="\{\{tabbar\.onConfig\}\}"/.test(wxml));
  check('wxml 四个手势入口全部指向 WXS（start/move/end/cancel）',
        ['touchstart', 'touchmove', 'touchend', 'touchcancel'].every(
          (k) => new RegExp('bind' + k + '="\\{\\{tabbar\\.' + k + '\\}\\}"').test(wxml)));
  check('wxml 位移层与玻璃块是两个元素（.tab-thumb > .tab-pill > .tab-pill-gloss）',
        /<view class="tab-thumb">\s*<view class="tab-pill"><view class="tab-pill-gloss"><\/view><\/view>/.test(wxml));
  check('wxml 两格仍是 flex 子项并带 data-index',
        /class="tab-item \{\{wave \? 'wave ' \+ \(index === 0 \? 'w0' : 'w1'\) : ''\}\}"/.test(wxml)
        && /data-index="\{\{index\}\}"/.test(wxml));
  check('bindtap 兜底仍在（WXS 不可用时还能切页）',
        /bindtap="switchTab"/.test(wxml) && /switchTab\(e\) \{/.test(js)
        && /_handledTapAt/.test(js));

  const block = (css, sel) => {
    const re = new RegExp('(^|\\n)' + sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}');
    const m = re.exec(css);
    return m ? m[2] : '';
  };
  const barBlock = block(wxss, '.tab-bar');
  check('★ .tab-bar 高度仍是 110rpx（气泡避让的几何基准，不许动）',
        /height:\s*110rpx/.test(barBlock), barBlock.replace(/\s+/g, ' ').slice(0, 80));
  check('★ .tab-bar 只剩定位（背景/模糊/阴影搬到 .tab-glass）',
        !/backdrop-filter/.test(barBlock) && !/background/.test(barBlock)
        && /backdrop-filter:\s*saturate\(180%\) blur\(14px\)/.test(block(wxss, '.tab-glass')));
  const thumbBlock = block(wxss, '.tab-thumb');
  check('★ 位移层只平移：没有 scale / animation，pointer-events:none（命中面留给 tab-item）',
        !/scale/.test(thumbBlock) && !/animation/.test(thumbBlock)
        && /pointer-events:\s*none/.test(thumbBlock) && /will-change:\s*transform/.test(thumbBlock));
  check('★ 位移层与药丸的左右内缩一致（left 9rpx / width calc(50% - 18rpx)）',
        /left:\s*9rpx/.test(thumbBlock) && /width:\s*calc\(50% - 18rpx\)/.test(thumbBlock));
  const darkPill = block(wxss, '.theme-dark .tab-pill');
  const nums = (/rgba?\(([^)]*)\)/.exec(darkPill) || [])[1];
  const ch = nums ? nums.split(',').slice(0, 3).map((v) => parseInt(v, 10)) : [];
  check('★ 深色模式滑块是黑玻璃（三通道都 < 80）',
        ch.length === 3 && ch.every((v) => v < 80), ch.join(','));
  check('★ 整排 wave：两格 stagger 26ms',
        /@keyframes tabWave/.test(wxss) && /\.tab-item\.wave\.w1 \{ animation-delay: 0\.026s/.test(wxss));

  // 微信 WXS 编译器只吃 ES5 子集（scroll.wxs 曾被 1e9 咬过一次）
  const code = wxs.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '').replace(/\/\/.*$/gm, '');
  check('WXS 无科学计数法（会让微信编译器报 Unexpected token）',
        !/[0-9][eE][+-]?[0-9]/.test(code));
  check('WXS 无 ES6 语法（箭头 / const / let / 模板串 / 展开 / **）',
        !/=>/.test(code) && !/\bconst\b/.test(code) && !/\blet\b/.test(code)
        && !/\u0060/.test(code) && !/\.\.\./.test(code) && !/\*\*/.test(code));
  check('★ 手势期间没有任何 setData（WXS 全逐帧 setStyle，逻辑层不参与）',
        !/setData/.test(wxs) && /\.setStyle\(/.test(wxs));
  check('几何参数由逻辑层下发（_measure：slot = barW / 2、inset 9rpx）',
        /const slot = barW \/ 2/.test(js) && /const inset = 9 \* pxPerRpx/.test(js)
        && /cfg: \{ step: geo\.step, maxIdx: geo\.maxIdx, over: geo\.over, sel: idx/.test(js));
  check('切页仍是「先播退出动画 → 200ms 后 switchTab」',
        /cp\.animateExit\(dir\)/.test(js) && /wx\.switchTab\(\{ url: '\/' \+ target\.pagePath \}\) \}, 200\)/.test(js));
}

/* ---------------- 二、落位 / 点按 ---------------- */
function runSettleTests() {
  section('二、落位与点按（船式起伏 + 点击平移）');
  const engine = loadEngine();

  const o1 = makeOwner(engine, { step: GEO.step, maxIdx: 1, over: GEO.over, sel: 1 });
  check('页面出现在第 2 格：药丸直接落在 147.5px（不做「滑过来」）',
        Math.abs(pillX(o1) - 147.5) < 0.01, String(pillX(o1)));
  check('首帧顺带显形（opacity 1）', lastStyle(o1, 'thumb').opacity === '1');
  check('整条栏位移为 0', barX(o1) === 0, String(barX(o1)));

  const o2 = makeOwner(engine, { step: GEO.step, maxIdx: 1, over: GEO.over, sel: 0 });
  engine.touchstart(touch(40, 700, 1), o2);
  const down = lastStyle(o2, 'glass');
  check('★ 按住时整条玻璃缩到 0.965（110ms 下沉）',
        /scale\(0\.965\)/.test(down.transform) && /110ms/.test(down.transition),
        down.transform + ' / ' + down.transition);

  engine.touchend(lift, o2);
  const up = lastStyle(o2, 'glass');
  check('★ 抬手用过冲曲线弹回（340ms cubic-bezier(.34,1.56,.64,1)）',
        /scale\(1\)/.test(up.transform) && /340ms/.test(up.transition)
        && /cubic-bezier\(0\.34, 1\.56, 0\.64, 1\)/.test(up.transition),
        up.transition);
  check('★ 点按由视图层上报：onTabTap { index: 1 }（不用等 bindtap）',
        o2._calls.length === 1 && o2._calls[0].name === 'onTabTap' && o2._calls[0].arg.index === 1,
        JSON.stringify(o2._calls));

  runFrames(o2, engine, 2);
  const mid = pillX(o2);
  check('★ 药丸开始滑向被点的那一格（160ms 内先走一半，不是瞬移）',
        mid > 20 && mid < 130, String(mid));
  runFrames(o2, engine, 12);
  check('★ 160ms 后精确落位 147.5px、整条栏回 0',
        Math.abs(pillX(o2) - 147.5) < 0.01 && Math.abs(barX(o2)) < 0.01,
        pillX(o2) + ' / ' + barX(o2));
  check('吸附结束后不再留动画帧（rAF 队列清空）', o2._rafs.length === 0, String(o2._rafs.length));
}

/* ---------------- 三、拖动：跟手 + 滞后 ---------------- */
function runDragTests() {
  section('三、拖动跟手与「脱手」滞后（85ms 时间常数）');
  const engine = loadEngine();

  const o = makeOwner(engine, { step: GEO.step, maxIdx: 1, over: GEO.over, sel: 0 });
  engine.touchstart(touch(40, 700, 0), o);
  engine._clock.t += 16;
  engine.touchmove(touch(100, 700, 0), o);   // +60px = 0.4068 格
  check('★ 手指一下走 60px，药丸当帧还没动（滞后从 0 开始追）',
        Math.abs(pillX(o)) < 0.01, String(pillX(o)));
  check('整条栏当帧就跟手：60px × 12% = 7.2px（≤10px 上限）',
        Math.abs(barX(o) - 7.2) < 0.05, String(barX(o)));

  runFrames(o, engine, 2);
  const lagged = pillX(o) + barX(o);          // 屏幕上看得见的位置 = 药丸 + 整条栏
  check('★ 32ms 后只跟到约 4 成（远小于 60px，看得出「脱手」）',
        lagged > 12 && lagged < 40, String(lagged));

  runFrames(o, engine, 30);
  const settled = pillX(o) + barX(o);
  check('★ 手停下后 0.5s 内追平（屏幕位置 ≈ 手指 60px ± 1.5）',
        Math.abs(settled - 60) < 1.5, String(settled));
  check('★ 药丸内部行程扣掉了整条栏那 7.2px（不叠成双倍）',
        Math.abs(pillX(o) - 52.8) < 1.5, String(pillX(o)));
  check('拖动中整条栏位移始终 ≤10px', Math.abs(barX(o)) <= 10.001, String(barX(o)));
  engine.touchend(lift, o);
  runFrames(o, engine, 12);
}

/* ---------------- 四、越界橡皮筋 ---------------- */
function runRubberTests() {
  section('四、越界：药丸被挡在玻璃栏内 + 整条栏反向跟随（±10px）');
  const engine = loadEngine();

  const o = makeOwner(engine, { step: GEO.step, maxIdx: 1, over: GEO.over, sel: 0 });
  engine.touchstart(touch(200, 700, 0), o);
  engine._clock.t += 16;
  engine.touchmove(touch(120, 700, 0), o);    // -80px
  runFrames(o, engine, 45);                    // 滞后追上之后才看药丸位置（85ms 常数、跨满格要 ~0.7s）
  const px = pillX(o);
  check('★ 左越界：药丸探出量被夹在 over 内（≤4.5px，不越出圆角）',
        px >= -4.51 && px <= -4.4, String(px));
  check('★ 左越界：整条栏跟着往左（约 -9.8px，绝对值 ≤10）',
        barX(o) < -5 && Math.abs(barX(o)) <= 10.001, String(barX(o)));
  check('★ 药丸左沿仍在玻璃栏内（内缩 4.5px + 药丸位移 ≥ -0.5px）',
        4.5 + px >= -0.51, String(4.5 + px));

  const o2 = makeOwner(engine, { step: GEO.step, maxIdx: 1, over: GEO.over, sel: 0 });
  engine.touchstart(touch(100, 700, 0), o2);
  engine._clock.t += 16;
  engine.touchmove(touch(300, 700, 0), o2);   // +200px，远超右边界
  runFrames(o2, engine, 45);
  check('★ 右越界：药丸停在 maxIdx + over（约 152.0px）',
        Math.abs(pillX(o2) - 152.03) < 0.1, String(pillX(o2)));
  check('★ 右越界：整条栏跟随 +10px（夹住）',
        Math.abs(barX(o2) - 10) < 0.01, String(barX(o2)));
  engine.touchend(lift, o2);
  runFrames(o2, engine, 12);
}

/* ---------------- 五、松手决策 ---------------- */
function runReleaseTests() {
  section('五、松手按药丸位置切页 / 拖回原格不切页 / 竖向手势不拦');
  const engine = loadEngine();

  const a = makeOwner(engine, { step: GEO.step, maxIdx: 1, over: GEO.over, sel: 0 });
  engine.touchstart(touch(40, 700, 0), a);
  engine._clock.t += 16;
  engine.touchmove(touch(150, 700, 0), a);    // +110px = 0.746 格，过半
  runFrames(a, engine, 12);
  engine.touchend(lift, a);
  check('★ 过半格松手 → onTabDragTo { to: 1 }（切到「我」）',
        a._calls.length === 1 && a._calls[0].name === 'onTabDragTo'
        && a._calls[0].arg.to === 1 && a._calls[0].arg.from === 0,
        JSON.stringify(a._calls));
  runFrames(a, engine, 12);
  check('松手后药丸吸附到第 2 格、整条栏回 0',
        Math.abs(pillX(a) - 147.5) < 0.01 && Math.abs(barX(a)) < 0.01,
        pillX(a) + ' / ' + barX(a));

  const b = makeOwner(engine, { step: GEO.step, maxIdx: 1, over: GEO.over, sel: 0 });
  engine.touchstart(touch(40, 700, 0), b);
  engine._clock.t += 16;
  engine.touchmove(touch(70, 700, 0), b);     // +30px = 0.2 格
  runFrames(b, engine, 12);
  engine.touchend(lift, b);
  check('★ 没过半格 → onTabDragBack（不切页，回原格）',
        b._calls.length === 1 && b._calls[0].name === 'onTabDragBack', JSON.stringify(b._calls));
  runFrames(b, engine, 12);
  check('拖回时药丸回第 1 格', Math.abs(pillX(b)) < 0.01, String(pillX(b)));

  const c = makeOwner(engine, { step: GEO.step, maxIdx: 1, over: GEO.over, sel: 0 });
  engine.touchstart(touch(40, 700, 0), c);
  engine._clock.t += 16;
  engine.touchmove(touch(42, 760, 0), c);     // 竖向 60px
  check('★ 竖向手势让位：药丸与整条栏都不动',
        Math.abs(pillX(c)) < 0.01 && Math.abs(barX(c)) < 0.01, pillX(c) + ' / ' + barX(c));
  engine.touchend(lift, c);
  check('★ 竖向手势松手不切页、也不算点按', c._calls.length === 0, JSON.stringify(c._calls));
  runFrames(c, engine, 12);

  const d = makeOwner(engine, { step: GEO.step, maxIdx: 1, over: GEO.over, sel: 1 });
  engine.touchstart(touch(40, 700, 1), d);
  engine._clock.t += 16;
  engine.touchmove(touch(-160, 700, 1), d);   // 从第 2 格往回拖 200px，远超左边界
  runFrames(d, engine, 45);
  engine.touchend(lift, d);
  check('从「我」页往左拖到底 → 药丸夹在 -over、整条栏跟着走',
        Math.abs(pillX(d) + 4.5) < 0.2 && barX(d) < -4, pillX(d) + ' / ' + barX(d));
}

/* ---------------- 六、页面出现时对齐 ---------------- */
function runRefreshTests() {
  section('六、切页后新实例对齐（每个 tab 页各持一份 tabBar）');
  const engine = loadEngine();
  const o = makeOwner(engine, { step: GEO.step, maxIdx: 1, over: GEO.over, sel: 0 });
  check('第 1 格起点 0px', Math.abs(pillX(o)) < 0.01, String(pillX(o)));
  engine.onConfig({ step: GEO.step, maxIdx: 1, over: GEO.over, sel: 1, rev: 2 }, null, o);
  check('★ 换到「我」页后新实例直接落在第 2 格（不从前一格滑）',
        Math.abs(pillX(o) - 147.5) < 0.01, String(pillX(o)));
  engine.onConfig({ step: GEO.step, maxIdx: 1, over: GEO.over, sel: 0, rev: 3 }, null, o);
  check('★ 换回「打印」页同理', Math.abs(pillX(o)) < 0.01, String(pillX(o)));
}

runContractTests();
runSettleTests();
runDragTests();
runRubberTests();
runReleaseTests();
runRefreshTests();

console.log('\n' + '='.repeat(52));
if (failures) {
  console.log('小程序 Tab 栏玻璃滑块验收: 存在 ' + failures + ' 项失败 ❌');
  process.exit(1);
}
console.log('小程序 Tab 栏玻璃滑块验收: 全部通过 ✅');
process.exit(0);
