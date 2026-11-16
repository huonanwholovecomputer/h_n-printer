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
 * 2026-11-16 第 4 轮（用户录屏取证）：切到「我」落地后药丸先闪回「打印」再跳回来。
 * 根因不是手势，是**落地那一刻的落位口径**：新页面 tabBar 实例的 _refresh 里
 * getCurrentPages() 还是旧页（route=pages/index/index），而旧 route 会「命中」第 1 格，
 * 于是把兜底（_tabWant）只写在「route 未命中」的分支里等于没生效 → 落 0，两帧后
 * 被页面 onShow 的 setData(selected:1) → _repaint 拽回 1，看上去就是闪一下。
 * 修法两条：① _tabWant（1.5s 内）优先于 route；② 另加 class 显隐门（thumbReady），
 * 新实例在「落位 + cfg」那一次 setData 到达前不显形，first paint 的默认 0 也看不见。
 *
 * 2026-11-16 第 5 轮（用户复核：闪仍在）：第 4 轮修的是「新实例落位」，漏了**旧实例的 data**。
 * 拖/点切页时药丸是视图层 setStyle 挪过去的，旧实例 data.thumbX 还停在旧格；换页那一瞬
 * 框架若重下发本组件的 style，就按旧值把药丸拽回上一格 —— 屏上顺序是「右 → 左 → 右」，
 * 正好是用户录屏里那 2 帧。修法：_goTab 的 200ms 回调里先把本实例 thumbX 改成目标格再
 * switchTab（值就等于屏上位置，重下发无感）。
 *
 * 2026-11-16 第 7 轮（用户截图：打印页高亮、药丸却在「我」上，一直不恢复）：
 * 与第 4 轮同形，但这次药丸**不是**闪一下，而是稳稳定在错格 —— 说明补画被永久吞掉。
 * 两条独立的病根：① 落位口径第 ② 层回读了 storage 的 _tabCur（跨启动持久化），冷启动/
 * 新实例在页面栈还空的那一瞬会落到「上次那个格」—— 打印页、药丸在「我」；改成模块内存
 * sessionCur（本次启动内的已落定格），冷启动从 -1 开始、绝不回读 storage。
 * ② 页面 onShow 的 setData({selected}) 若恰好落在 _markQuiet() 与 wx.nextTick 清闸之间，
 * 补画被吞且之后没有任何事件能纠回来 —— selected 观察者不再看 _quiet（已对上就不打
 * setData），页面另在 setData 后主动调 syncPill()（同值 setData 观察者不开火那条路）。
 * ③ 顺带补一条落地核对：_goTab 480ms 内 switchTab 没真落地（转场中重复点/低端机超时）时，
 * 按真实 route 把药丸收回原格（_reconcile）。
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
  if (cfg) engine.onConfig(Object.assign({ snap: true }, cfg), null, owner);
  return owner;
}

// 一次手势会分多次 setStyle（transform / transition 各写一条），真机上属性是合并的，
// 所以这里也要按顺序合并同一个元素的全部 style，只取最后一条会读到没有 transform 的那条。
function lastStyle(owner, key) {
  let out = null;
  for (const rec of owner._logs) {
    if (rec.key !== key) continue;
    out = Object.assign(out || {}, rec.s);
  }
  return out;
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
        /<view class="tab-thumb"[^>]*>\s*<view class="tab-pill"><view class="tab-pill-gloss"><\/view><\/view>/.test(wxml));
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
  check('★ 位移层本身不带 opacity（视图层逐帧写 style，逻辑层 setData 会把它抹掉）',
        !/opacity:\s*0/.test(thumbBlock.replace(/\/\*[\s\S]*?\*\//g, '')));
  check('★ 首帧显隐门开在 .tab-glass 的 class 上（data 驱动，setData 抹不掉）',
        /\.tab-glass \.tab-thumb \{\s*opacity: 0;\s*\}/.test(wxss)
        && /\.tab-glass\.ready \.tab-thumb \{\s*opacity: 1;\s*\}/.test(wxss)
        && /class="tab-glass \{\{thumbReady \? 'ready' : ''\}\}"/.test(wxml)
        && /thumbReady: false/.test(js) && /thumbReady: true/.test(js));
  check('★ 首个 setData 之前连选中态文字/图标都不点亮（否则「打印」会闪蓝）',
        /item\.active && thumbReady \? 'icon-active'/.test(wxml)
        && /item\.active && thumbReady \? 'text-active'/.test(wxml));
  check('★ 位移层有 data 兜底的落位（style 绑定 thumbX，被抹掉也落在正确格位）',
        /class="tab-thumb" style="transform: translate3d\(\{\{thumbX\}\}px, 0, 0\)"/.test(wxml)
        && /thumbX: idx \* geo\.step/.test(js) && /_repaint\(\)/.test(js));
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
  check('★ 选中态的淡蓝底不再铺在图标上（底衬交给药丸；这条同时能识别「工具还在用旧 wxss」）',
        /\.icon-active \{\s*background-color: transparent;/.test(wxss));
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
        !/setData/.test(wxs.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, ''))
        && /\.setStyle\(/.test(wxs));
  check('几何参数由逻辑层下发（_measure：slot = barW / 2、inset 9rpx）',
        /const slot = barW \/ 2/.test(js) && /const inset = 9 \* pxPerRpx/.test(js)
        && /cfg: \{ step: geo\.step, maxIdx: geo\.maxIdx, over: geo\.over, sel: idx/.test(js));
  // 点按那一刻是**唯一**允许 setData 的地方（第 6 轮）：把目标格写进 data，由 .tab-thumb 的
  // CSS 过渡滑过去。以前排在 200ms 之后、还跟在 switchTab 后面 —— 换页重下发 style 时用的仍是
  // 上一格，于是「已经滑到『我』又闪回『打印』」。
  const goTabAt = js.indexOf('_goTab(index) {')
  const goTabTimerAt = js.indexOf('setTimeout(', goTabAt)
  const goTabSrc = js.slice(goTabAt, goTabTimerAt > goTabAt ? goTabTimerAt : goTabAt + 2200)
  check('★ 点按那一刻就把目标格写进 data，且全函数只有这一次 setData（quiet：视图层不再写 transform）',
        (goTabSrc.replace(/^\s*\/\/.*$/gm, '').match(/setData\(/g) || []).length === 1
        && /thumbX: index \* geo\.step/.test(goTabSrc) && /quiet: true/.test(goTabSrc),
        goTabSrc.length + 'B')
  check('★ 选中态与 wave 交给切过去之后那一页的 _refresh（新实例自带正确落位）',
        /arriving/.test(js) && /patch\.wave = true/.test(js) && /_tabWaveAt/.test(js))
  const repaintSrc = js.slice(js.indexOf('_repaint() {'), js.indexOf('_repaint() {') + 700)
  check('★ 页面 setData 后按逻辑层 selected 重新落位（snap:true）—— 否则药丸停在上一格',
        /snap: true/.test(repaintSrc) && /sel[,:]/.test(repaintSrc), repaintSrc.length + 'B')
  const refreshSrc = js.slice(js.indexOf('_refresh() {'), js.indexOf('_markQuiet() {'))
  // 括号深度扫描：注释与字符串里的括号不算
  function braceDepthAt(src, needle) {
    let depth = 0, quote = '', i = 0;
    const end = src.indexOf(needle);
    if (end < 0) return -1;
    let inLine = false, inBlock = false;
    for (; i < end; i++) {
      const ch = src[i], nx = src[i + 1];
      if (inLine) { if (ch === '\n') inLine = false; continue; }
      if (inBlock) { if (ch === '*' && nx === '/') { inBlock = false; i++; } continue; }
      if (quote) { if (ch === '\\') { i++; continue; } if (ch === quote) quote = ''; continue; }
      if (ch === '/' && nx === '/') { inLine = true; i++; continue; }
      if (ch === '/' && nx === '*') { inBlock = true; i++; continue; }
      if (ch === "'" || ch === '"') { quote = ch; continue; }
      if (ch === '{') depth++;
      else if (ch === '}') depth--;
    }
    return depth;
  }
  const wantDepth = braceDepthAt(refreshSrc, "wx.getStorageSync('_tabWant')")
  check('★ 切页落地时页面栈可能还是旧页：_tabWant 优先于 route（不在 if (route) 的 else 里）',
        /_tabWantAt/.test(js) && wantDepth === 2, 'depth=' + wantDepth);
  check('切页仍是「先播退出动画 → 200ms 后 switchTab」',
        /cp\.animateExit\(dir\)/.test(js) && /\}, 200\)/.test(js)
        && /wx\.switchTab\(\{ url: '\/' \+ target\.pagePath \}\)/.test(js));
}

/* ---------------- 二、落位 / 点按 ---------------- */
function runSettleTests() {
  section('二、落位与点按（船式起伏 + 点击平移）');
  const engine = loadEngine();

  const o1 = makeOwner(engine, { step: GEO.step, maxIdx: 1, over: GEO.over, sel: 1 });
  check('页面出现在第 2 格：药丸直接落在 147.5px（不做「滑过来」）',
        Math.abs(pillX(o1) - 147.5) < 0.01, String(pillX(o1)));
  check('★ 位移层样式只由 transform 承载（不再靠视图层写死 opacity —— 那会被 setData 抹掉）',
        lastStyle(o1, 'thumb').opacity === undefined);
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
  check('★ 点按的滑行不再由视图层逐帧做（交给 data.thumbX + .tab-thumb 的 CSS 过渡，两处抢同一条 transform 就会抖）',
        Math.abs(pillX(o2)) < 0.01, String(pillX(o2)));
  check('★ 点按不排动画帧（松手不 settle，rAF 队列干净）', o2._rafs.length === 0, String(o2._rafs.length));
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
  engine.onConfig({ step: GEO.step, maxIdx: 1, over: GEO.over, sel: 1, rev: 2, snap: true }, null, o);
  check('★ 换到「我」页后新实例直接落在第 2 格（不从前一格滑）',
        Math.abs(pillX(o) - 147.5) < 0.01, String(pillX(o)));
  engine.onConfig({ step: GEO.step, maxIdx: 1, over: GEO.over, sel: 0, rev: 3, snap: true }, null, o);
  check('★ 换回「打印」页同理', Math.abs(pillX(o)) < 0.01, String(pillX(o)));
}

/* ---------------- 七、实例级：真跑 Component（复现「落地闪一下」） ---------------- */
function loadController() {
  const src = fs.readFileSync(path.join(TAB_DIR, 'index.js'), 'utf8');
  const store = Object.create(null);
  const timers = [];
  let def = null;
  const sandbox = {
    console, Math, JSON, String, Number, Object, Array, Date,
    Component: (d) => { def = d; },
    getApp: () => ({ globalData: { isDarkMode: false } }),
    getCurrentPages: () => sandbox.__pages,
    setTimeout: (fn, ms) => (timers.push({ fn, ms }), timers.length),
    clearTimeout: () => {},
    __pages: [{ route: 'pages/index/index' }],
    __switchTo: '',
    __ticks: [],
    wx: {
      getWindowInfo: () => ({ windowWidth: 375 }),
      getStorageSync: (k) => (k in store ? store[k] : ''),
      setStorageSync: (k, v) => { store[k] = v; },
      removeStorageSync: (k) => { delete store[k]; },
      // 队列化（不是立刻跑）：真机上 wx.nextTick 要等下一次渲染，页面 onShow 的 setData 完全可能
      // 插在 _markQuiet() 与清闸之间 —— 那正是「补画被吞、药丸停在错误格」的现场，桩里必须能复现。
      nextTick: (fn) => {
        sandbox.__ticks.push(fn)
        return sandbox.__ticks.length
      },
      setBackgroundColor: () => {},
      setNavigationBarColor: () => {},
      switchTab: (o) => { sandbox.__switchTo = (o && o.url) || ''; },
    },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: 'custom-tab-bar/index.js' });
  return { def: def, store: store, sandbox: sandbox, timers: timers };
}

function instantiate(def) {
  const inst = {};
  Object.keys(def.methods).forEach((k) => { inst[k] = def.methods[k]; });
  inst.data = JSON.parse(JSON.stringify(def.data));
  inst.setDataCalls = [];
  inst.setData = function (patch) {
    inst.setDataCalls.push(patch);
    Object.keys(patch).forEach((key) => {
      const m = /^(\w+)\[(\d+)\]\.(\w+)$/.exec(key);
      if (m) { inst.data[m[1]][Number(m[2])][m[3]] = patch[key]; return; }
      inst.data[key] = patch[key];
    });
  };
  return inst;
}

// 观察者接线（七节之前的用例不看观察者；八节要复现「页面 setData → 补画」这条链）
function instantiateWired(def) {
  const inst = instantiate(def);
  const observers = def.observers || {};
  const prev = {};
  Object.keys(observers).forEach((k) => { prev[k] = JSON.parse(JSON.stringify(inst.data[k] === undefined ? null : inst.data[k])); });
  const rawSet = inst.setData;
  inst.setData = function (patch) {
    rawSet(patch);
    Object.keys(observers).forEach((k) => {
      if (!(k in inst.data)) return;
      const now = JSON.stringify(inst.data[k] === undefined ? null : inst.data[k]);
      if (now === JSON.stringify(prev[k])) return;
      const old = prev[k];
      prev[k] = inst.data[k];
      observers[k].call(inst, inst.data[k], old);
    });
  };
  return inst;
}

// 把排队中的 wx.nextTick 放掉（= 下一次渲染之后）
function flushTicks(c) {
  const queue = c.sandbox.__ticks.splice(0, c.sandbox.__ticks.length);
  queue.forEach((fn) => fn());
}

function runInstanceTests() {
  section('七、实例级：落地那一刻药丸不闪（pageLifetimes.show → _refresh）');

  /* 7-1 现场 bug：切到「我」落地时 getCurrentPages() 还是旧页（route=pages/index/index） */
  const c1 = loadController();
  c1.store._tabWant = 1;
  c1.store._tabWantAt = Date.now();
  c1.sandbox.__pages = [{ route: 'pages/index/index' }];   // 旧栈：命中第 1 格
  const i1 = instantiate(c1.def);
  c1.def.lifetimes.attached.call(i1);
  check('★ 落地时页面栈还是旧页 → 直接落第 2 格（不先落 0 再被 onShow 拽回来）',
        i1.data.selected === 1 && i1.data.cfg.sel === 1 && Math.abs(i1.data.thumbX - 147.5) < 0.01,
        JSON.stringify({ selected: i1.data.selected, sel: i1.data.cfg.sel, thumbX: i1.data.thumbX }));
  check('★ 落位与 thumbReady 在同一次 setData（显形即正确，first paint 的默认 0 看不见）',
        i1.data.thumbReady === true && i1.setDataCalls.length === 1
        && i1.setDataCalls[0].thumbReady === true && 'cfg' in i1.setDataCalls[0],
        i1.setDataCalls.length + ' 次 setData');
  check('选中态列表同一次 setData 带过去（文字/图标不会先点错格）',
        i1.data.list[0].active === false && i1.data.list[1].active === true,
        JSON.stringify([i1.data.list[0].active, i1.data.list[1].active]));

  /* 7-2 反向：切回「打印」，落地时页面栈还是 me */
  const c2 = loadController();
  c2.store._tabWant = 0;
  c2.store._tabWantAt = Date.now();
  c2.sandbox.__pages = [{ route: 'pages/me/me' }];
  const i2 = instantiate(c2.def);
  c2.def.pageLifetimes.show.call(i2);
  check('★ 切回「打印」落地时页面栈还是旧页 → 落第 1 格', i2.data.selected === 0
        && Math.abs(i2.data.thumbX) < 0.01, String(i2.data.thumbX));

  /* 7-3 没有 _tabWant（1.5s 外 / 直接打开页面）→ route 作数 */
  const c3 = loadController();
  c3.sandbox.__pages = [{ route: 'pages/me/me' }];
  const i3 = instantiate(c3.def);
  c3.def.pageLifetimes.show.call(i3);
  check('没有切页记录时按 route 落位（「我」→ 第 2 格）',
        i3.data.selected === 1 && Math.abs(i3.data.thumbX - 147.5) < 0.01, String(i3.data.thumbX));

  /* 7-3b ★ route 拿不到（切页落地那一刻 page 栈还空）→ 按「本次启动已落定过的格」落位。
     迟到创建的那份实例（被隐藏的打印页）也走这条路，否则它会画回第 1 格叠在当前栏上，
     看上去就是「切页几秒后药丸又闪一下」。
     注意口径：只用内存态（同一次启动内跨实例共享），**不读 storage** —— 那里是上一条命的值。 */
  const c3b = loadController();
  c3b.sandbox.__pages = [{ route: 'pages/me/me' }];
  const first3b = instantiate(c3b.def);
  c3b.def.pageLifetimes.show.call(first3b);      // 本次启动已落定第 2 格
  c3b.sandbox.__pages = [];                       // 迟到创建的实例：栈还空
  const i3b = instantiate(c3b.def);
  c3b.def.pageLifetimes.show.call(i3b);
  check('★ route 拿不到时按全局 _tabCur 落位（迟到/隐藏页实例不会画回第 1 格）',
        i3b.data.selected === 1 && Math.abs(i3b.data.thumbX - 147.5) < 0.01,
        JSON.stringify({ sel: i3b.data.selected, thumbX: i3b.data.thumbX }));

  /* 7-4 过期的 _tabWant 不作数（否则很久以后回页面还会落错格） */
  const c4 = loadController();
  c4.store._tabWant = 1;
  c4.store._tabWantAt = Date.now() - 5000;
  c4.sandbox.__pages = [{ route: 'pages/index/index' }];
  const i4 = instantiate(c4.def);
  const i4b = instantiate(c4.def);
  c4.def.pageLifetimes.show.call(i4b);
  check('★ 超过 1.5s 的 _tabWant 不作数（按 route 落第 1 格）', i4b.data.selected === 0,
        String(i4b.data.selected));

  /* 7-5 _repaint：页面直接 setData 之后补画，同时保持门是开的 */
  const c5 = loadController();
  const i5 = instantiate(c5.def);
  i5.data.selected = 1;
  i5._repaint();
  check('★ _repaint 按逻辑层 selected 落位并保证 thumbReady（被 setData 抹掉后不会一直不显形）',
        Math.abs(i5.data.thumbX - 147.5) < 0.01 && i5.data.thumbReady === true,
        i5.data.thumbX + ' / ' + i5.data.thumbReady);

  /* 7-6 _goTab：点按那一刻就把目标格写进 data（第 6 轮），200ms 后才 switchTab */
  const c6 = loadController();
  const i6 = instantiate(c6.def);
  i6.data.selected = 0;
  i6._goTab(1);
  check('★ 点按那一刻就把目标格写进 data：只有一次 setData，thumbX=目标格 + quiet cfg',
        i6.setDataCalls.length === 1
        && Math.abs(Number(i6.setDataCalls[0].thumbX) - 147.5) < 0.01
        && i6.setDataCalls[0].cfg && i6.setDataCalls[0].cfg.quiet === true
        && i6.setDataCalls[0].cfg.sel === 1,
        JSON.stringify(i6.setDataCalls));
  check('切页前落下目标格/源格/wave 时间戳',
        c6.store._tabWant === 1 && c6.store._tabFrom === 0 && c6.store._tabTo === 1
        && c6.store._tabWantAt > 0 && c6.store._tabWaveAt > 0, JSON.stringify(c6.store));
  check('切页延迟 200ms（> 退出动画 160ms），另排一条落地位核对',
        c6.timers.length === 2 && c6.timers[0].ms === 200 && c6.timers[1].ms === 450,
        JSON.stringify(c6.timers.map((t) => t.ms)));
  c6.timers[0].fn();
  check('200ms 后真的 switchTab 到「我」', c6.sandbox.__switchTo === '/pages/me/me', c6.sandbox.__switchTo);
  check('★ 换页之后不再补 setData（药丸位置只由点按那一次下发决定）',
        i6.setDataCalls.length === 1, i6.setDataCalls.length + ' 次');

  /* 7-6b 静态：改落位必须排在 switchTab 之前（顺序反了等于没改） */
  const ctrlSrc = fs.readFileSync(path.join(TAB_DIR, 'index.js'), 'utf8');
  const goSeg = ctrlSrc.slice(ctrlSrc.indexOf('_goTab(index) {'));
  const iSet = goSeg.indexOf('thumbX: index * geo.step');
  const iSw = goSeg.indexOf('wx.switchTab(');
  check('★ 先改 thumbX 再 switchTab（顺序反了等于没改）', iSet > 0 && iSw > 0 && iSet < iSw,
        iSet + ' < ' + iSw);
  check('★ 点按滑动的曲线与时长交给 CSS 过渡（.tab-thumb 有 160ms，与页面退出动画同长）',
        /transition:\s*transform 160ms cubic-bezier\(0\.2, 0, 0, 1\)/.test(
          fs.readFileSync(path.join(TAB_DIR, 'index.wxss'), 'utf8'))
        && /SLIDE_MS:\s*160/.test(fs.readFileSync(WXS, 'utf8')));

  /* 7-6c ★ 有弹窗时 cp.animateExit 会自己接管切页并 return false —— 那条路上也必须已经落下 _tabWant，
     否则新页面的 tabBar 拿不到目标格、只能按旧 route 落 0，一帧后再被页面 onShow 拽回来（就是那 2 帧闪） */
  const c6c = loadController();
  c6c.sandbox.__pages = [{ route: 'pages/index/index', animateExit: () => false }];
  const i6c = instantiate(c6c.def);
  i6c.data.selected = 0;
  i6c._goTab(1);
  check('★ 弹窗分支（animateExit 返回 false 自己 switchTab）也已经落下 _tabWant',
        c6c.store._tabWant === 1 && c6c.store._tabWantAt > 0, JSON.stringify(c6c.store));
  const iWant = goSeg.indexOf("wx.setStorageSync('_tabWant'");
  const iExit = goSeg.indexOf('cp.animateExit(dir)');
  check('★ 落 _tabWant 必须排在 animateExit 之前', iWant > 0 && iExit > 0 && iWant < iExit,
        iWant + ' < ' + iExit);

  /* 7-7 同格点击不切页、不写 storage */
  const c7 = loadController();
  const i7 = instantiate(c7.def);
  i7.data.selected = 1;
  i7._goTab(1);
  check('点当前格：不切页也不写 _tabWant', c7.timers.length === 0 && !('_tabWant' in c7.store),
        JSON.stringify(c7.store));
}

/* ---------------- 八、药丸停在错误格（用户现场：在打印页、药丸却在「我」） ---------------- */
function runStaleTests() {
  section('八、药丸停在错误格：任何落位顺序都必须收敛回真实页面');

  const ctrlSrcNow = () => fs.readFileSync(path.join(TAB_DIR, 'index.js'), 'utf8');

  /* 8-1 冷启动：上一条命留在 storage 里的 _tabCur 不得参与落位 */
  const c1 = loadController();
  c1.store._tabCur = 1;                       // 上一轮停在「我」
  c1.sandbox.__pages = [];                    // 冷启动这一刻页面栈还没登记
  const i1 = instantiateWired(c1.def);
  c1.def.lifetimes.attached.call(i1);
  check('★ 冷启动不读上一条命的 _tabCur（栈还空时退回本实例默认格）',
        i1.data.selected === 0 && Math.abs(i1.data.thumbX) < 0.01,
        JSON.stringify({ sel: i1.data.selected, thumbX: i1.data.thumbX }));
  check('★ 静态：落位代码里不再出现 getStorageSync(\'_tabCur\')',
        !/getStorageSync\('_tabCur'\)/.test(ctrlSrcNow()));

  /* 8-2 现场那屏的复现：_refresh 落到第 2 格，页面 onShow 的 setData({selected:0})
     正好插在 _quiet 还开着的时候 —— 以前补画被吞，屏上就是「打印高亮 + 药丸在『我』」。 */
  const c2 = loadController();
  c2.store._tabWant = 1;
  c2.store._tabWantAt = Date.now();
  c2.sandbox.__pages = [{ route: 'pages/me/me' }];
  const i2 = instantiateWired(c2.def);
  c2.def.lifetimes.attached.call(i2);
  check('8-2 前置：先落到第 2 格',
        i2.data.selected === 1 && Math.abs(i2.data.thumbX - 147.5) < 0.01,
        JSON.stringify({ sel: i2.data.selected, thumbX: i2.data.thumbX }));
  i2.setData({ selected: 0, 'list[0].active': true, 'list[1].active': false });
  check('★ 页面声明 selected 与药丸不一致 → 即使 quiet 没清也立刻补画',
        i2.data.selected === 0 && Math.abs(i2.data.thumbX) < 0.01 && i2.data.cfg.sel === 0,
        JSON.stringify({ sel: i2.data.selected, thumbX: i2.data.thumbX, cfgSel: i2.data.cfg && i2.data.cfg.sel }));

  /* 8-2b 页面 onShow 里主动 syncPill()：值是同一个（不触发观察者）时也要能收敛。
     现场这条路最要命 —— _goTab 只挪了药丸没改 selected，页面再 setData({selected:0})
     等于什么都没发生，观察者根本不开火。 */
  const c3 = loadController();
  c3.sandbox.__pages = [{ route: 'pages/index/index' }];
  const i3 = instantiateWired(c3.def);
  c3.def.lifetimes.attached.call(i3);
  i3._goTab(1);                                // 用户点「我」：药丸乐观地先滑过去
  i3.setData({ selected: 0, 'list[0].active': true, 'list[1].active': false });   // 值没变
  check('8-2b 前置：值没变 → 药丸仍停在第 2 格（观察者不开火）',
        Math.abs(i3.data.thumbX - 147.5) < 0.01, String(i3.data.thumbX));
  if (typeof i3.syncPill === 'function') i3.syncPill();
  check('★ 页面 onShow 调 syncPill() → 药丸按真实选中格收敛',
        Math.abs(i3.data.thumbX) < 0.01 && i3.data.cfg.sel === 0,
        JSON.stringify({ thumbX: i3.data.thumbX, cfgSel: i3.data.cfg && i3.data.cfg.sel }));

  /* 8-3 switchTab 压根没落地：药丸已经乐观滑走，页面却还停在原格 */
  const c4 = loadController();
  c4.sandbox.__pages = [{ route: 'pages/index/index' }];
  const i4 = instantiateWired(c4.def);
  c4.def.lifetimes.attached.call(i4);
  i4._goTab(1);
  const sw = c4.timers.find((t) => t.ms === 200);
  check('8-3 前置：200ms 后 switchTab 已发出', !!sw, JSON.stringify(c4.timers.map((t) => t.ms)));
  if (sw) sw.fn();
  const settle = c4.timers.find((t) => t.ms === 450);
  check('★ 切页之后还排了一条落地位核对（否则 switchTab 失败时药丸永远停在『我』）',
        !!settle, JSON.stringify(c4.timers.map((t) => t.ms)));
  if (settle) {
    settle.fn();                               // 页面栈仍停在 index ⇒ 切页没落地
    check('★ 切页没落地 → 按真实 route 收回第 1 格，并清掉残留的 _tabWant',
          i4.data.selected === 0 && Math.abs(i4.data.thumbX) < 0.01 && i4.data.cfg.sel === 0
          && !('_tabWant' in c4.store),
          JSON.stringify({ sel: i4.data.selected, thumbX: i4.data.thumbX, store: c4.store }));
  }

  /* 8-4 静态：两个 tab 页 onShow 声明选中格之后都要叫一次 syncPill() */
  const ixSrc = fs.readFileSync(path.join(H5, 'pages', 'index', 'index.js'), 'utf8');
  const meSrc = fs.readFileSync(path.join(H5, 'pages', 'me', 'me.js'), 'utf8');
  check('★ 打印页 / 我页 onShow 里都调了 tabBar.syncPill()',
        /tabBar\.syncPill\(\)/.test(ixSrc) && /tabBar\.syncPill\(\)/.test(meSrc),
        (/tabBar\.syncPill\(\)/.test(ixSrc) ? 'index ✓' : 'index ✗') + ' / '
        + (/tabBar\.syncPill\(\)/.test(meSrc) ? 'me ✓' : 'me ✗'));
}

runContractTests();
runSettleTests();
runDragTests();
runRubberTests();
runReleaseTests();
runRefreshTests();
runInstanceTests();
runStaleTests();

console.log('\n' + '='.repeat(52));
if (failures) {
  console.log('小程序 Tab 栏玻璃滑块验收: 存在 ' + failures + ' 项失败 ❌');
  process.exit(1);
}
console.log('小程序 Tab 栏玻璃滑块验收: 全部通过 ✅');
process.exit(0);
