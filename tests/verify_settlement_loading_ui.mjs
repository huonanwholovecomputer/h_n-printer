/* 验收 · 收支清算「取数中 → 拿到结果」的动效（2026-10-29 用户需求）
 *
 * 需求原话（m00003 第 2 条）：「图中这里是在读取，所以改成什么加载的样式，比如让这个⌛️转起来
 *   或者改成圆圈都行，总之就是要看出来是在加载而不是卡住了」
 * 需求原话（m00034 第 3 条）：「这种分段加载会导致画面闪烁，所以统计完成后，需要给下面的卡片的
 *   变化播放入场动画，不然太突兀了」
 *
 * 为什么必须真浏览器：这两条都只存在于「渲染出来的样子」里 ——
 *   ① 静态 grep 只能证明源码写了 loading-spin，证明不了它**在转**（computed animationName、
 *      动画时间轴是否真的在推进）；把 CSS 删了、把 animation 写成 paused，grep 照样绿。
 *   ② 入场动画只在「上一次是 loading、这一次不是」那一次重画后触发；这要求真跑一遍分段取数，
 *      还得证明**不重复播**（否则切个设置就整页抖一下）。
 *
 * 做法：本地 HTTP 服务（静态文件来自 local_print_tool/finance/，/api/** 全部打桩、绝不发外网；
 * 两条取数接口**首次请求延迟 1200ms 才回**，把页面停在「正在统计…」态）→ Playwright 打开
 * settlement.html（默认就是收支清算页）：
 *   ① 加载态：横幅里出现 .loading-spin，computed animationName === 'spin'、时长 ≠ 0s、
 *      playState 是 running，且时间轴确实在推进（两次读 currentTime，后者更大）；
 *   ② 横幅文案不再用静态 ⏳（那是旧口径，一眼看不出死没死）；
 *   ③ 拿到结果那一次重画：KPI 卡与下方区块都被 animate() 播了入场（记录 klass 含 kpi-card /
 *      section，条数 ≥ 4），delay 逐条递增（stagger）；KPI 卡本身 4 张都在；
 *   ④ 不重复播：再调一次 renderAll()（状态 ready→ready），animate() 记录条数**不增加**；
 *   ⑤ prefers-reduced-motion: reduce 的上下文里一条入场动画都不播（无障碍口径）；
 *   ⑥ 全程零 JS 异常、零外网请求。
 *   ⑨⑩ **首屏两次结构跳变**（配置延迟 900ms 复现现场时序：「暂无成员」占位 → KPI 卡）：
 *      占位那一版自己也要淡入，成员载入换成卡片时同样要播 —— 用户 m00208 原话
 *      「第三张图的动画有了，但是前两个没有」。判据取「卡片出现的那一刻」的记录，
 *      不等 settle（否则会拿 ③ 的动画冒充 ② 的）。
 *   ⑦ **变异自证**（两次独立运行，必须按预期转红）：
 *      · 'no-spin'：把横幅里的 loading-spin 换回「⏳ 」= 修前行为 ⇒ ① 必须红，③ 仍绿；
 *      · 'no-entrance'：让 playSettledEntrance 直接 return ⇒ ③ 必须红，① 仍绿；
 *      · 'no-shape'：结构跳变不开入场窗口（= 修前行为）⇒ ⑨⑩ 必须同时红。
 *
 * 用法：node tests/verify_settlement_loading_ui.mjs
 * exit 0 = 全部通过。
 */
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require_ = createRequire(import.meta.url);
const { chromium } = require_('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FINANCE = path.join(HERE, '..', 'local_print_tool', 'finance');
const HTML_PATH = path.join(FINANCE, 'settlement.html');
const SRC = fs.readFileSync(HTML_PATH, 'utf8');

const CUR = new Date();
const CUR_MONTH = `${CUR.getFullYear()}-${String(CUR.getMonth() + 1).padStart(2, '0')}`;
const D1 = `${CUR_MONTH}-06`;
const D2 = `${CUR_MONTH}-09`;

const STORE = {
  config: {
    investRate: 0.05, scoreCap: 15, passwords: {},
    members: [{ id: 'ma', name: '霍楠', color: '#4472C4' }, { id: 'mb', name: '姚懿祥', color: '#548235' }],
    orderPrinterMap: {}, printers: [], purchaseBasis: 'usage',
  },
  records: {
    expenses: [{ id: 'e1', date: D1, item: 'A4纸', amount: 100, payer: 'ma', units: 500 }],
    income: [
      { id: 'i1', date: `${CUR_MONTH}-01`, memberId: 'ma', amount: 120 },
      { id: 'i2', date: `${CUR_MONTH}-02`, memberId: 'mb', amount: 80 },
    ],
    transfers: [],
    scores: [
      { id: 's1', date: `${CUR_MONTH}-01`, memberId: 'ma', item: '值班', score: 5 },
      { id: 's2', date: `${CUR_MONTH}-01`, memberId: 'mb', item: '客服', score: 5 },
    ],
  },
  notes: {}, meta: { version: 2 },
};
const ORDERS = [
  { order_id: 'o1', order_number: 'HN-A1', created_at: `${D1} 10:00:00`, openid: 'local', owner_name: '霍楠',
    files: [{ status: 'sent', total_pages: 60, total_sheets: 60, effective_pages: 60, page_count: 60, copies: 1, duplex: 'off', total_price: 18.0 }] },
  { order_id: 'o2', order_number: 'HN-A2', created_at: `${D2} 11:00:00`, openid: 'local', owner_name: '姚懿祥',
    files: [{ status: 'sent', total_pages: 40, total_sheets: 40, effective_pages: 40, page_count: 40, copies: 1, duplex: 'off', total_price: 12.0 }] },
];

/* 首次请求延迟 1200ms：把页面稳稳停在「正在统计…」态，才能验到加载样式 */
const FIRST_DELAY_MS = 1200;
let slowSeen = {};
const json = (res, obj) => { res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(obj)); };
function delayedJson(res, obj, key) {
  const wait = slowSeen[key] ? 0 : FIRST_DELAY_MS;
  slowSeen[key] = true;
  setTimeout(() => json(res, obj), wait);
}

let MUTATE = null;
/* 首屏「暂无成员」占位的复现开关：配置（成员表）延迟回来，页面就先渲染占位、再换成卡片 */
let CONFIG_DELAY_MS = 0;
function makeHtml(mutate) {
  if (!mutate) return SRC;
  if (mutate === 'no-spin') {
    /* 变异：横幅回到静态 ⏳（修前行为） */
    const from = '<span class="loading-spin"></span>正在统计';
    const hits = SRC.split(from).length - 1;
    if (hits !== 2) throw new Error('变异失败：横幅里 loading-spin 出现 ' + hits + ' 次（期望 2，源码格式变了？）');
    return SRC.split(from).join('⏳ 正在统计');
  }
  if (mutate === 'no-shape') {
    /* 变异：结构跳变不再开入场窗口（= 首屏占位 / 成员载入都是硬跳，修前行为） */
    const a = '  if (trackDashShape()) _dashEntranceUntil = now + ENTRANCE_WINDOW_MS;';
    const b = '    playSettledEntrance(usageStateOfMonth(currentKey), \'none\');';
    if (SRC.indexOf(a) < 0) throw new Error('变异失败：没找到 trackDashShape 那一行（源码格式变了？）');
    if (SRC.indexOf(b) < 0) throw new Error('变异失败：没找到空态分支的 playSettledEntrance 调用（源码格式变了？）');
    return SRC.replace(a, '  trackDashShape();').replace(b, '    trackDashShape();');
  }
  if (mutate === 'no-entrance') {
    /* 变异：入场动画整体停摆（= 没做第 3 条需求） */
    const from = '  if (!_dashEntranceUntil || now > _dashEntranceUntil) return;';
    if (SRC.indexOf(from) < 0) throw new Error('变异失败：没找到 playSettledEntrance 的守卫行（源码格式变了？）');
    return SRC.replace(from, '  if (true) return;');
  }
  throw new Error('未知变异: ' + mutate);
}

const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  const p = decodeURIComponent(u.pathname);
  if (!p.startsWith('/api/')) {
    if (p === '/' || p === '/settlement.html') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(makeHtml(MUTATE));
    }
    const file = path.join(FINANCE, p.replace(/^\//, ''));
    if (!fs.existsSync(file)) { res.writeHead(404); return res.end('not found'); }
    res.writeHead(200, { 'Content-Type': 'application/octet-stream' });
    return res.end(fs.readFileSync(file));
  }
  if (p === '/api/health') return json(res, { success: true, cloud: true });
  if (p === '/api/local/finance_auth/status') {
    return json(res, { success: true, modules: {}, needs_setup: [],
                       session: { active: false, modules: [], expires_in: 0 }, session_ttl: 1800,
                       read_only: false, integrity: 'ok' });
  }
  if (p === '/api/proxy/finance/config') {
    if (req.method === 'POST') { req.resume(); return req.on('end', () => json(res, { success: true, server_version: 2 })); }
    const payload = { success: true, data: STORE };
    return CONFIG_DELAY_MS ? delayedJson(res, payload, 'config') : json(res, payload);
  }
  if (p === '/api/proxy/pricing') return json(res, { success: true, data: { simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1, delivery_percentages: {}, urgency_prices: {} } });
  if (p === '/api/proxy/admin/statistics/months') return json(res, { success: true, data: { months: [{ month: CUR_MONTH }] } });
  if (p === '/api/proxy/admin/statistics/revenue') {
    return delayedJson(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: ORDERS } }, 'revenue');
  }
  if (p === '/api/local/orders') {
    return delayedJson(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: [] } }, 'orders');
  }
  if (p === '/api/local/external_prints') return json(res, { success: true, orders: [], count: 0, skipped: 0, simplex_price: 0.3, duplex_price: 0.4, owner_name: '' });
  if (p === '/api/local/data') return json(res, { success: true, data: null });
  if (p === '/api/local/bindings') return json(res, { success: true, data: {} });
  return json(res, { success: true, data: null });
});

const port = await new Promise((r) => server.listen(0, '127.0.0.1', () => r(server.address().port)));
const BASE = 'http://127.0.0.1:' + port;
const browser = await chromium.launch();

/* 记录每一次 WAAPI animate() 调用（用于证明「播了入场动画」且「只播一次」）
   必须在页面脚本之前注入。 */
const INIT = () => {
  window.__anim = [];
  const orig = Element.prototype.animate;
  Element.prototype.animate = function (kf, opts) {
    try {
      window.__anim.push({ cls: String(this.className || ''), delay: (opts && opts.delay) || 0,
                           dur: (opts && opts.duration) || 0 });
    } catch (e) {}
    return orig.apply(this, arguments);
  };
};

async function runScenario(mutate, opts) {
  const o = opts || {};
  MUTATE = mutate;
  CONFIG_DELAY_MS = 0;
  slowSeen = {};
  const ctx = await browser.newContext({ viewport: { width: 1500, height: 1000 },
                                         reducedMotion: o.reducedMotion ? 'reduce' : 'no-preference' });
  const page = await ctx.newPage();
  const errors = [];
  const outbound = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('request', (r) => { if (!r.url().startsWith(BASE) && !r.url().startsWith('data:')) outbound.push(r.url()); });
  await page.addInitScript(INIT);

  const rows = [];
  const chk = (name, cond, detail) => rows.push([name, !!cond, detail === undefined ? '' : String(detail)]);

  await page.goto(BASE + '/settlement.html', { waitUntil: 'domcontentloaded' });

  /* ── ① 加载态：转圈真的在转 ── */
  let load = { n: 0 };
  try {
    await page.waitForSelector('#tab-dash .alert-banner .loading-spin', { timeout: 8000 });
    load = await page.evaluate(() => {
      const spins = [...document.querySelectorAll('#tab-dash .alert-banner .loading-spin')];
      const banners = [...document.querySelectorAll('#tab-dash .alert-banner')];
      const s = spins[0];
      const cs = s ? getComputedStyle(s) : null;
      const anims = s && s.getAnimations ? s.getAnimations() : [];
      return {
        n: spins.length,
        name: cs ? cs.animationName : '',
        dur: cs ? cs.animationDuration : '',
        iter: cs ? cs.animationIterationCount : '',
        states: anims.map((a) => a.playState),
        box: s ? { w: Math.round(s.getBoundingClientRect().width), h: Math.round(s.getBoundingClientRect().height) } : null,
        stale: banners.filter((b) => b.textContent.indexOf('⏳') >= 0).length,
      };
    });
  } catch (e) {
    load = { n: 0, err: String(e.message || e).slice(0, 120) };
  }
  const spinOk = load.n >= 1 && load.name === 'spin' && load.dur && load.dur !== '0s'
    && (!load.states.length || load.states.indexOf('running') >= 0);
  chk('① 加载态横幅里是转圈（.loading-spin，computed animationName=spin、时长≠0、playState=running）', spinOk,
    `横幅数 ${load.n}、name=${load.name}、dur=${load.dur}、state=${(load.states || []).join('|')}${load.err ? '、err=' + load.err : ''}`);
  chk('② 横幅文案不再用静态 ⏳（旧口径看不出死没死）', load.n >= 1 && load.stale === 0, `含 ⏳ 的横幅 ${load.stale} 条`);

  /* ② 时间轴真的在推进（不是 paused / 动效被关掉的定格画面） */
  let t1 = null, t2 = null;
  try {
    t1 = await page.evaluate(() => {
      const s = document.querySelector('#tab-dash .alert-banner .loading-spin');
      const a = s && s.getAnimations ? s.getAnimations()[0] : null;
      return a ? a.currentTime : null;
    });
    await page.waitForTimeout(200);
    t2 = await page.evaluate(() => {
      const s = document.querySelector('#tab-dash .alert-banner .loading-spin');
      const a = s && s.getAnimations ? s.getAnimations()[0] : null;
      return a ? a.currentTime : null;
    });
  } catch (e) { /* 已 settle：交给断言去红 */ }
  chk('③ 转圈的时间轴在推进（两次读 currentTime，后者更大）',
    typeof t1 === 'number' && typeof t2 === 'number' && t2 > t1, `${t1} → ${t2}`);

  /* ── ④ 拿到结果那一次重画：KPI 卡与区块播入场动画（стagger） ── */
  const settled = await page.waitForFunction(() => !document.querySelector('#tab-dash .alert-banner .loading-spin'), null, { timeout: 20000 })
    .then(() => true).catch(() => false);
  await page.waitForTimeout(120);
  const after = await page.evaluate(() => {
    const anim = window.__anim || [];
    const kpis = anim.filter((a) => /kpi-card/.test(a.cls));
    const secs = anim.filter((a) => /(^|\s)section(\s|$)/.test(a.cls));
    const delays = anim.map((a) => a.delay);
    let inc = true;
    for (let i = 1; i < Math.min(delays.length, 13); i++) if (!(delays[i] >= delays[i - 1])) inc = false;
    return {
      total: anim.length, kpis: kpis.length, secs: secs.length, delays,
      increasing: inc,
      kpiNodes: document.querySelectorAll('#tab-dash .kpi-card').length,
      spinGone: !document.querySelector('#tab-dash .alert-banner .loading-spin'),
    };
  });
  if (!o.reducedMotion) {
    chk('④ 取数完成那一次重画给 KPI 卡与下方区块播了入场动画（animate 记录 ≥ 4 条，含 kpi-card 与 section）',
      settled && after.spinGone && after.total >= 4 && after.kpis >= 1 && after.secs >= 1,
      `settled=${settled}、记录 ${after.total}（kpi ${after.kpis} / section ${after.secs}）、KPI 节点 ${after.kpiNodes}`);
    chk('⑤ 入场按顺序 stagger（delay 逐条不减，末尾封顶 12*45ms）', after.increasing && after.delays.length >= 2,
      'delays=' + after.delays.slice(0, 14).join(','));
  } else {
    chk('⑤ prefers-reduced-motion: reduce ⇒ 一条入场动画都不播（无障碍口径）',
      settled && after.total === 0, `记录 ${after.total} 条（期望 0）`);
  }

  /* ⑥ 取数回调往往**连着**触发好几次重画（云端月份、设备列表、外部账本各通知一次）：
     上一次播到一半的 DOM 会被下一份 innerHTML 整个换掉 ⇒ 紧跟的那次重画必须重新播，
     否则用户看到的是一版完全没有动画的新 DOM（2026-10-29 现场逐帧截图就是这样）。 */
  let live = { running: 0, nodes: 0 };
  if (!o.reducedMotion) {
    await page.evaluate(() => { if (typeof renderAll === 'function') renderAll(); });
    live = await page.evaluate(() => {
      const nodes = [...document.querySelectorAll('#tab-dash .kpi-card, #tab-dash .section')];
      const anims = nodes.flatMap((n) => (n.getAnimations ? n.getAnimations() : []))
        .filter((a) => a.playState === 'running' || a.playState === 'pending');
      return { running: anims.length, nodes: nodes.length,
               withAnim: nodes.filter((n) => (n.getAnimations ? n.getAnimations() : []).length > 0).length };
    });
    chk('⑥ 取数完成紧跟的又一次重画（全新 DOM）上仍有在跑的入场动画', live.running >= 4 && live.withAnim >= 4,
      `节点 ${live.nodes}、带动画 ${live.withAnim}、在跑 ${live.running}`);
  }

  /* ⑦ 稳定后（入场窗口之外）不再重复播 —— 切月份 / 改设置引起的重画应当即时响应 */
  let before = 0, afterRe = 0;
  if (!o.reducedMotion) {
    await page.waitForTimeout(700);
    before = await page.evaluate(() => (window.__anim || []).length);
    await page.evaluate(() => { if (typeof renderAll === 'function') renderAll(); });
    await page.waitForTimeout(80);
    afterRe = await page.evaluate(() => (window.__anim || []).length);
    chk('⑦ 稳定后（入场窗口之外）再调 renderAll() 不重复播', afterRe === before,
      `${before} → ${afterRe}`);
  }

  chk('⑧ 全程零 JS 异常、零外网请求', errors.length === 0 && outbound.length === 0,
    `err=${errors.slice(0, 2).join(' | ') || '无'}、外网=${outbound.slice(0, 2).join(' | ') || '无'}`);

  await ctx.close();
  return rows;
}

/* ── 首屏两次结构跳变（2026-10-29 用户反馈「第三张图的动画有了，但是前两个没有」）──
   真实现场三帧：① 页面刚打开、STORE 还没取回来 ⇒「暂无成员」占位；② 配置到达 ⇒ 整页换成
   KPI 卡 + 两条「正在统计…」；③ 取数完成。修前只有 ③ 有动画，①②是硬跳。
   这里把配置延迟 900ms 造出同一时序，然后在**卡片出现的那一刻**立刻读 animate() 记录
   （不等到 settle，避免把 ③ 的记录算进 ②的账）。 */
async function runMembersLate(mutate) {
  MUTATE = mutate || null;
  CONFIG_DELAY_MS = 900;
  slowSeen = {};
  const ctx = await browser.newContext({ viewport: { width: 1500, height: 1000 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.addInitScript(INIT);
  const rows = [];
  const chk = (name, cond, detail) => rows.push([name, !!cond, detail === undefined ? '' : String(detail)]);

  await page.goto(BASE + '/settlement.html', { waitUntil: 'domcontentloaded' });

  const placeholder = await page.waitForSelector('#tab-dash .section .empty h3', { timeout: 8000 })
    .then((el) => el.textContent()).catch(() => '');
  const animAtPlaceholder = await page.evaluate(() => {
    const anim = window.__anim || [];
    return { total: anim.length, secs: anim.filter((a) => /(^|\s)section(\s|$)/.test(a.cls)).length };
  });
  chk('⑨ 首屏「暂无成员」占位本身也播入场（占位块是 .section，animate 记录 ≥1 条）',
    placeholder.indexOf('暂无成员') >= 0 && animAtPlaceholder.secs >= 1,
    `占位文案「${placeholder}」、记录 ${animAtPlaceholder.total} 条（section ${animAtPlaceholder.secs}）`);

  const cardsOk = await page.waitForFunction(() => document.querySelectorAll('#tab-dash .kpi-card').length >= 2,
    null, { timeout: 20000, polling: 'raf' }).then(() => true).catch(() => false);
  const jump = await page.evaluate((n) => {
    const anim = (window.__anim || []).slice(n);
    return { added: anim.length, kpis: anim.filter((a) => /kpi-card/.test(a.cls)).length };
  }, animAtPlaceholder.total);
  chk('⑩ 成员载入（暂无成员 → KPI 卡）这次结构跳变同样播入场（新增记录里 ≥2 条 kpi-card）',
    cardsOk && jump.kpis >= 2, `新增 ${jump.added} 条、其中 kpi-card ${jump.kpis} 条`);

  chk('⑪ members-late 场景全程零 JS 异常', errors.length === 0, errors.slice(0, 2).join(' | ') || '无');
  await ctx.close();
  return rows;
}

const normal = await runScenario(null);
const reduced = await runScenario(null, { reducedMotion: true });
const mutSpin = await runScenario('no-spin');
const mutEntr = await runScenario('no-entrance');
const lateCfg = await runMembersLate();
const mutShape = await runMembersLate('no-shape');

await browser.close();
server.close();

function report(title, rows) {
  console.log('\n=== ' + title + ' ===');
  let fail = 0;
  for (const [n, c, d] of rows) {
    if (!c) fail++;
    console.log(`[${c ? 'PASS' : 'FAIL'}] ${n}` + (!c && d ? '  -- ' + d : ''));
  }
  return fail;
}

let bad = report('正常源码（真浏览器 · 云端取数 1200ms 延迟）', normal);
bad += report('首屏占位 → 成员载入 → 取数完成（配置延迟 900ms）', lateCfg);
bad += report('prefers-reduced-motion: reduce', reduced);

/* 变异自证：两次运行必须各有唯一的判据转红，其余不受牵连 */
console.log('\n=== 变异自证 ===');
const pick = (rows, prefix) => rows.find((r) => r[0].indexOf(prefix) === 0);
const spinRow = pick(mutSpin, '①'), spinEntrRow = pick(mutSpin, '④');
const entrRow = pick(mutEntr, '④'), entrSpinRow = pick(mutEntr, '①');
const okSpinMut = !!spinRow && spinRow[1] === false && !!spinEntrRow && spinEntrRow[1] === true;
const okEntrMut = !!entrRow && entrRow[1] === false && !!entrSpinRow && entrSpinRow[1] === true;
const shapePh = pick(mutShape, '⑨'), shapeCards = pick(mutShape, '⑩');
const okShapeMut = !!shapePh && shapePh[1] === false && !!shapeCards && shapeCards[1] === false;
if (!okSpinMut) bad++;
if (!okEntrMut) bad++;
if (!okShapeMut) bad++;
console.log(`[${okSpinMut ? 'PASS' : 'FAIL'}] 把横幅换回静态 ⏳（修前行为）⇒ ① 转圈判据转红、④ 入场仍绿`);
console.log(`[${okEntrMut ? 'PASS' : 'FAIL'}] 让 playSettledEntrance 直接 return ⇒ ④ 入场判据转红、① 转圈仍绿`);
console.log(`[${okShapeMut ? 'PASS' : 'FAIL'}] 结构跳变不开入场窗口（修前行为）⇒ ⑨ 首屏占位、⑩ 成员载入两条判据同时转红`);

console.log('\n' + '='.repeat(60));
console.log(`已跑断言 ${normal.length + lateCfg.length + reduced.length + 3} 项，失败 ${bad} 项`);
process.exit(bad ? 1 : 0);
