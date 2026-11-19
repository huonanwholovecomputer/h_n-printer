/* eslint-disable */
/**
 * 小程序底部 Tab 栏验收（第 18 轮口径：没有药丸动画、没有手动滑动）
 *
 * 用户原话（第 18 轮起点）：「那小程序端就不要药丸动画了，直接切换就行，手动滑动也删掉。」
 *
 * 从这一版起组件的契约只有三条：
 *   ① 药丸是静态指示块：data.thumbX === 本页格 × 步长，任何时刻都成立；
 *   ② 点按切页只做「记目标格 + 播页面退出动画 + switchTab」，本页一次 setData 都不发；
 *   ③ 视图层没有任何手势与动画：没有 utils/tabbar.wxs、没有 bindtouch*、没有 transition/animation。
 *
 * 历次排查留下的结论仍然生效（细节见 mobile_apps/docs/移动端_小程序.md 的 Tab 栏各轮小节）：
 *   · _tabWant（1.5s 内）必须优先于 route —— 切页落地那一刻页面栈可能还是旧页，而旧 route 会
 *     「命中」旧格（第 4 轮现场：切到「我」先落 0、两帧后被 onShow 拽回来）；
 *   · thumbReady 显隐门与落位放进同一次 setData，first paint 的默认 0 看不见；
 *   · 落位口径第 ② 层用模块内存 sessionCur，绝不回读 storage 的 _tabCur（第 7 轮现场）；
 *   · 450ms 落地核对在 pageLifetimes.hide 之后不许清 _tabWant（第 9 轮现场：清掉就永久停错格）；
 *   · 「一致地错」（selected 与 thumbX 一起被旧 route 带偏）必须拿真实 route 校一次才会自愈。
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

/* 375 宽真机口径：barW 295、一格 147.5px */
const STEP = 147.5;

/* ---------------- 实例级桩 ---------------- */
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
      nextTick: (fn) => { sandbox.__ticks.push(fn); return sandbox.__ticks.length; },
      setBackgroundColor: () => {},
      setNavigationBarColor: () => {},
      switchTab: (o) => { sandbox.__switchTo = (o && o.url) || ''; },
    },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: 'custom-tab-bar/index.js' });
  return { def: def, src: src, store: store, sandbox: sandbox, timers: timers };
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

// 观察者接线：要复现「页面 setData(selected) → 补画」这条链
function instantiateWired(def) {
  const inst = instantiate(def);
  const observers = def.observers || {};
  const prev = {};
  Object.keys(observers).forEach((k) => { prev[k] = JSON.stringify(inst.data[k] === undefined ? null : inst.data[k]); });
  const rawSet = inst.setData;
  inst.setData = function (patch) {
    rawSet(patch);
    Object.keys(observers).forEach((k) => {
      if (!(k in inst.data)) return;
      const now = JSON.stringify(inst.data[k] === undefined ? null : inst.data[k]);
      if (now === prev[k]) return;
      const old = prev[k];
      prev[k] = now;                       // 存 JSON 串（和上面的比较同型），存原值会导致观察者无限递归
      observers[k].call(inst, inst.data[k], old);
    });
  };
  return inst;
}

/* ---------------- 一、结构契约 ---------------- */
function runContractTests() {
  section('一、结构契约：没有手势、没有动画、没有再引用 WXS');
  const wxmlRaw = fs.readFileSync(path.join(TAB_DIR, 'index.wxml'), 'utf8');
  const wxml = wxmlRaw.replace(/<!--[\s\S]*?-->/g, '');      // 注释里会提到 bindtouch，先剥掉
  const wxss = fs.readFileSync(path.join(TAB_DIR, 'index.wxss'), 'utf8');
  const c = loadController();
  const code = c.src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');

  check('★ utils/tabbar.wxs 已删除（手动滑动整个删掉，不是留着不用）', !fs.existsSync(WXS), WXS);
  check('★ WXML 不再引用 wxs，也没有任何 bindtouch*（点按只走 bindtap）',
        !/<wxs/.test(wxml) && !/bindtouch/.test(wxml) && /bindtap="switchTab"/.test(wxml));
  check('WXML 只保留两层结构 + 静态药丸位移层',
        /class="tab-bar/.test(wxml) && /class="tab-glass/.test(wxml) && /class="tab-thumb"/.test(wxml)
        && /class="tab-pill"/.test(wxml) && /translate3d\(\{\{thumbX\}\}px, 0, 0\)/.test(wxml));
  check('★ WXML 不再有 cfg / wave / snap-once / landed / bump 这些动效开关',
        !/cfg=/.test(wxml) && !/wave/.test(wxml) && !/snap-once/.test(wxml)
        && !/landed/.test(wxml) && !/bump/.test(wxml) && !/change:cfg/.test(wxml));
  check('★ WXSS 里没有任何 transition / animation / keyframes（药丸不再动）',
        !/transition\s*:/.test(wxss) && !/animation\s*:/.test(wxss) && !/@keyframes/.test(wxss));
  check('WXSS 仍保留几何与配色（药丸宽度 calc(50% - 18rpx)、深色黑玻璃）',
        /\.tab-thumb\s*\{[^}]*width:\s*calc\(50% - 18rpx\)/.test(wxss) && /\.theme-dark \.tab-pill/.test(wxss));
  check('★ index.js 不再有第 6~17 轮的任何机制（peers / _glideFrom / _handoffPending / snapOnce / landed / bump / wave / cfg）',
        !/peers/.test(code) && !/_glideFrom/.test(code) && !/_handoffPending/.test(code)
        && !/snapOnce/.test(code) && !/landed/.test(code) && !/bump/.test(code)
        && !/\bwave\b/.test(code) && !/cfg/.test(code));
  check('index.js 仍暴露页面依赖的三个入口：setData({selected}) 观察者、syncPill()、hideBorder 观察者',
        /selected\(\)/.test(code) && /syncPill\(\)/.test(code) && /hideBorder\(\)/.test(code));
}

/* ---------------- 二、落位不变量 ---------------- */
function runLandingTests() {
  section('二、落位：药丸位置永远等于「本页那一格」');
  const c1 = loadController();
  c1.sandbox.__pages = [{ route: 'pages/index/index' }];
  const i1 = instantiateWired(c1.def);
  c1.def.lifetimes.attached.call(i1);
  const p1 = i1.setDataCalls[i1.setDataCalls.length - 1] || {};
  check('打印页实例：落在第 1 格（0px）', Math.abs(i1.data.thumbX) < 0.01 && i1.data.selected === 0, String(i1.data.thumbX));
  check('★ 落位与 thumbReady 在同一次 setData（显形即正确，first paint 的默认 0 看不见）',
        p1.thumbReady === true && Math.abs(p1.thumbX) < 0.01 && i1.setDataCalls.length === 1,
        i1.setDataCalls.length + ' 次 setData');
  check('选中态同一次 setData 带过去（图标/文字不会先点错格）',
        i1.data.list[0].active === true && i1.data.list[1].active === false,
        JSON.stringify([i1.data.list[0].active, i1.data.list[1].active]));

  const c2 = loadController();
  c2.sandbox.__pages = [{ route: 'pages/me/me' }];
  const i2 = instantiateWired(c2.def);
  c2.def.lifetimes.attached.call(i2);
  check('★ 「我」页实例：直接落在第 2 格（147.5px），不先落 0',
        Math.abs(i2.data.thumbX - STEP) < 0.01 && i2.data.selected === 1, String(i2.data.thumbX));

  // 切页落地那一刻页面栈可能还是旧页 —— _tabWant 必须压过 route
  const c3 = loadController();
  c3.store._tabWant = 1;
  c3.store._tabWantAt = Date.now();
  c3.sandbox.__pages = [{ route: 'pages/index/index' }];
  const i3 = instantiateWired(c3.def);
  c3.def.lifetimes.attached.call(i3);
  check('★ 落地时页面栈还是旧页（route 命中旧格）→ _tabWant 优先，仍落第 2 格',
        Math.abs(i3.data.thumbX - STEP) < 0.01 && i3.data.selected === 1,
        JSON.stringify({ thumbX: i3.data.thumbX, selected: i3.data.selected }));

  // 过期的 _tabWant 不许再生效
  const c4 = loadController();
  c4.store._tabWant = 1;
  c4.store._tabWantAt = Date.now() - 5000;
  c4.sandbox.__pages = [{ route: 'pages/index/index' }];
  const i4 = instantiateWired(c4.def);
  c4.def.lifetimes.attached.call(i4);
  check('过期的 _tabWant（>1.5s）不生效，按真实 route 落第 1 格',
        Math.abs(i4.data.thumbX) < 0.01 && i4.data.selected === 0, String(i4.data.thumbX));

  // route 拿不到时用模块内存 sessionCur，绝不回读 storage 的 _tabCur
  const c5 = loadController();
  c5.store._tabCur = 1;                 // 上一条命留下的镜像，绝不能被读
  c5.sandbox.__pages = [];
  const i5 = instantiateWired(c5.def);
  c5.def.lifetimes.attached.call(i5);
  check('★ route 为空 + storage 里有 _tabCur=1 → 冷启动仍落第 1 格（sessionCur 从 -1 起）',
        Math.abs(i5.data.thumbX) < 0.01 && i5.data.selected === 0,
        JSON.stringify({ thumbX: i5.data.thumbX, selected: i5.data.selected }));
}

/* ---------------- 三、点按切页 ---------------- */
function runTapTests() {
  section('三、点按切页：本页一次 setData 都不发（药丸不动）');
  const c = loadController();
  c.sandbox.__pages = [{ route: 'pages/index/index' }];
  const i = instantiateWired(c.def);
  c.def.lifetimes.attached.call(i);
  const before = i.setDataCalls.length;
  i.switchTab({ currentTarget: { dataset: { index: 1 } } });
  check('★ 点按那一刻本页 0 次 setData（药丸在页内从不移动）',
        i.setDataCalls.length === before, (i.setDataCalls.length - before) + ' 次');
  check('目标格落盘给新页那份实例（_tabWant / _tabWantAt）',
        c.store._tabWant === 1 && Number(c.store._tabWantAt) > 0, JSON.stringify({ want: c.store._tabWant }));
  check('页面入场动画要用的 _tabFrom / _tabTo 一起写（pages 会读）',
        c.store._tabFrom === 0 && c.store._tabTo === 1, JSON.stringify({ from: c.store._tabFrom, to: c.store._tabTo }));
  check('排了两个定时器：200ms 切页 + 450ms 落地核对',
        c.timers.length === 2 && c.timers.some((t) => t.ms === 200) && c.timers.some((t) => t.ms === 450),
        JSON.stringify(c.timers.map((t) => t.ms)));
  const t200 = c.timers.find((t) => t.ms === 200);
  if (t200) t200.fn();
  check('200ms 后 switchTab 到「我」', c.sandbox.__switchTo === '/pages/me/me', c.sandbox.__switchTo);

  const c2 = loadController();
  c2.sandbox.__pages = [{ route: 'pages/index/index' }];
  const i2 = instantiateWired(c2.def);
  c2.def.lifetimes.attached.call(i2);
  const before2 = i2.setDataCalls.length;
  i2.switchTab({ currentTarget: { dataset: { index: 0 } } });
  check('点当前格：什么都不做（0 次 setData、0 个定时器、不写 _tabWant）',
        i2.setDataCalls.length === before2 && c2.timers.length === 0 && !('_tabWant' in c2.store),
        JSON.stringify({ timers: c2.timers.length, want: c2.store._tabWant }));

  // 弹窗分支：页面自己接管切页（animateExit 返回 false），但落地核对必须已经排上
  const c3 = loadController();
  c3.sandbox.__pages = [{ route: 'pages/index/index', animateExit: () => false }];
  const i3 = instantiateWired(c3.def);
  c3.def.lifetimes.attached.call(i3);
  i3.switchTab({ currentTarget: { dataset: { index: 1 } } });
  check('★ 弹窗接管（animateExit 返回 false）→ 自己不再 switchTab，但 450ms 核对照样排上',
        !c3.timers.some((t) => t.ms === 200) && c3.timers.some((t) => t.ms === 450),
        JSON.stringify(c3.timers.map((t) => t.ms)));
}

/* ---------------- 四、落地核对 ---------------- */
function runReconcileTests() {
  section('四、450ms 落地核对：还记得「切页没落地」与「只是栈还没换」的区别');
  const c = loadController();
  c.sandbox.__pages = [{ route: 'pages/index/index' }];
  const i = instantiateWired(c.def);
  c.def.lifetimes.attached.call(i);
  i.switchTab({ currentTarget: { dataset: { index: 1 } } });
  const t450 = c.timers.find((t) => t.ms === 450);
  if (t450) t450.fn();
  check('★ switchTab 没落地（route 仍是本页、也没 hide）→ 清掉 _tabWant 并回落到真实页',
        !('_tabWant' in c.store) && Math.abs(i.data.thumbX) < 0.01 && i.data.selected === 0,
        JSON.stringify({ want: c.store._tabWant, thumbX: i.data.thumbX }));

  const c2 = loadController();
  c2.sandbox.__pages = [{ route: 'pages/index/index' }];
  const i2 = instantiateWired(c2.def);
  c2.def.lifetimes.attached.call(i2);
  i2.switchTab({ currentTarget: { dataset: { index: 1 } } });
  c2.def.pageLifetimes.hide.call(i2);          // 页面真的被换走了，只是栈还没换
  const t2 = c2.timers.find((t) => t.ms === 450);
  if (t2) t2.fn();
  check('★ 已经 hide（切页落地了，只是 getCurrentPages 还旧）→ 不许清 _tabWant',
        c2.store._tabWant === 1, String(c2.store._tabWant));

  const c3 = loadController();
  c3.sandbox.__pages = [{ route: 'pages/index/index' }];
  const i3 = instantiateWired(c3.def);
  c3.def.lifetimes.attached.call(i3);
  i3.switchTab({ currentTarget: { dataset: { index: 1 } } });
  c3.sandbox.__pages = [{ route: 'pages/me/me' }];   // 栈已经换成新页了
  const t3 = c3.timers.find((t) => t.ms === 450);
  if (t3) t3.fn();
  check('栈已换成新页 → 核对什么都不做（不 setData、不动 want）',
        c3.store._tabWant === 1 && i3.data.thumbX === 0, JSON.stringify({ want: c3.store._tabWant, thumbX: i3.data.thumbX }));
}

/* ---------------- 五、补画与自愈 ---------------- */
function runSelfHealTests() {
  section('五、补画与自愈：页面 setData / syncPill 都不能把药丸留在错格');
  // hideBorder 观察者：页面提交时切它，框架会重下发节点样式（位移被抹掉）→ 必须按 data 补画
  const c0 = loadController();
  c0.sandbox.__pages = [{ route: 'pages/me/me' }];
  const i0 = instantiateWired(c0.def);
  c0.def.lifetimes.attached.call(i0);
  i0.data.thumbX = 0;                       // 模拟「节点样式被重下发，位移整条没了」
  i0.setData({ hideBorder: true });
  check('页面 setData({hideBorder}) → 观察者按 data 补画回第 2 格',
        Math.abs(i0.data.thumbX - STEP) < 0.01, String(i0.data.thumbX));

  // 页面 onShow 声明自己的格：selected 真的变了 ⇒ 观察者开火 ⇒ 落位跟着走
  const c1 = loadController();
  c1.sandbox.__pages = [{ route: 'pages/index/index' }];
  const i1 = instantiateWired(c1.def);
  c1.def.lifetimes.attached.call(i1);
  c1.sandbox.__pages = [{ route: 'pages/me/me' }];      // 页面已经变成「我」
  i1.setData({ selected: 1, 'list[0].active': false, 'list[1].active': true });
  check('页面 setData({selected:1})（本页就是「我」）→ 观察者把药丸落回第 2 格',
        Math.abs(i1.data.thumbX - STEP) < 0.01 && i1.data.selected === 1,
        JSON.stringify({ thumbX: i1.data.thumbX, selected: i1.data.selected }));

  // 页面 onShow 的 setData(selected: 同一个值) 观察者不开火 → 靠 syncPill() 兜
  const c2 = loadController();
  c2.sandbox.__pages = [{ route: 'pages/me/me' }];
  const i2 = instantiateWired(c2.def);
  c2.def.lifetimes.attached.call(i2);
  i2.data.thumbX = 0;
  i2.setData({ selected: 1 });          // 已经是 1：观察者不会开火
  i2.data.thumbX = 0;                   // 再抹一次，模拟「同值 setData 之后节点样式被重下发」
  i2.syncPill();
  check('syncPill：位置与 selected 不一致 → 按 selected 补画回第 2 格（同值 setData 的兜底）',
        Math.abs(i2.data.thumbX - STEP) < 0.01, String(i2.data.thumbX));

  // 「一致地错」：selected 与 thumbX 被旧 route 一起带偏 —— 只有拿真实 route 校才能发现
  const c3 = loadController();
  c3.sandbox.__pages = [{ route: 'pages/me/me' }];
  const i3 = instantiateWired(c3.def);
  c3.def.lifetimes.attached.call(i3);
  i3.data.selected = 0;
  i3.data.thumbX = 0;
  i3.syncPill();
  check('★ 一致地错（selected=0 且 thumbX=0，但真实页面是「我」）→ syncPill 拿 route 校一次、落到第 2 格',
        Math.abs(i3.data.thumbX - STEP) < 0.01 && i3.data.selected === 1,
        JSON.stringify({ thumbX: i3.data.thumbX, selected: i3.data.selected }));

  // 本页已 hide：当前 route 指向的是别人那一页，不许拿它校自己
  const c4 = loadController();
  c4.sandbox.__pages = [{ route: 'pages/index/index' }];
  const i4 = instantiateWired(c4.def);
  c4.def.lifetimes.attached.call(i4);
  c4.def.pageLifetimes.hide.call(i4);
  c4.sandbox.__pages = [{ route: 'pages/me/me' }];   // 栈已经换成别人
  const calls4 = i4.setDataCalls.length;
  i4.syncPill();
  check('本页已 hide 时 syncPill 不拿别人的 route 校自己（位置本来就对 ⇒ 一次 setData 都不发）',
        i4.setDataCalls.length === calls4 && Math.abs(i4.data.thumbX) < 0.01,
        JSON.stringify({ calls: i4.setDataCalls.length - calls4, thumbX: i4.data.thumbX }));

  // show 清闸门 + 按真实 route 落位
  const c5 = loadController();
  c5.sandbox.__pages = [{ route: 'pages/index/index' }];
  const i5 = instantiateWired(c5.def);
  c5.def.lifetimes.attached.call(i5);
  c5.def.pageLifetimes.hide.call(i5);
  c5.sandbox.__pages = [{ route: 'pages/me/me' }];
  c5.def.pageLifetimes.show.call(i5);
  check('回到屏上：清闸门 + 按真实 route 落位（第 2 格）',
        Math.abs(i5.data.thumbX - STEP) < 0.01 && i5.data.selected === 1,
        JSON.stringify({ thumbX: i5.data.thumbX, selected: i5.data.selected }));
}

/* ---------------- 六、静态：药丸不可能被挪到别处 ---------------- */
function runStaticTests() {
  section('六、静态：只有 _refresh / _repaint 能写 thumbX，且都只写「本页格」');
  const c = loadController();
  const code = c.src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');

  check('★ _goTab 代码段里没有 setData（点按绝不挪药丸）',
        (() => {
          const seg = code.slice(code.indexOf('_goTab(index)'));
          return !/setData\(/.test(seg) && !/thumbX/.test(seg);
        })());
  check('★ 写 thumbX 的两处都只由 idx / selected 决定（idx * geo.step / sel * geo.step）',
        /thumbX:\s*idx \* geo\.step/.test(code) && /thumbX:\s*sel \* geo\.step/.test(code));
  check('★ thumbReady 只在落位那两次 setData 里出现（显隐门与落位同一次）',
        (code.match(/thumbReady:\s*true/g) || []).length === 2,
        String((code.match(/thumbReady:\s*true/g) || []).length));
  check('没有定时器在切页途中改数据（唯一的数据改动在 show / 核对 / 补画）',
        !/setTimeout\([\s\S]{0,120}thumbX/.test(code));
  check('页面依赖的 API 全在：switchTab（bindtap）、syncPill、pageLifetimes.show/hide、hideBorder 观察者',
        /bindtap="switchTab"/.test(fs.readFileSync(path.join(TAB_DIR, 'index.wxml'), 'utf8'))
        && /switchTab\(e\)/.test(code) && /syncPill\(\)/.test(code)
        && /pageLifetimes/.test(code) && /hideBorder\(\)/.test(code));
}

runContractTests();
runLandingTests();
runTapTests();
runReconcileTests();
runSelfHealTests();
runStaticTests();

console.log('\n====================================================');
if (failures === 0) {
  console.log('小程序 Tab 栏验收（第 18 轮口径）: 全部通过 ✅');
} else {
  console.log('小程序 Tab 栏验收: ' + failures + ' 条失败 ❌');
}
process.exit(failures === 0 ? 0 : 1);
