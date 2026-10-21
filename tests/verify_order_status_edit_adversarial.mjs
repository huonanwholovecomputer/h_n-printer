/* 对抗式验证（真实渲染）· 收支清算「订单状态订正」v7.0 的**锁定绕过**与**本地模式跨库写入**
 *
 * 为什么必须真浏览器：需求 7 问的是「在锁定状态下直接调用 applyOrderStatus(oid,'sent')
 * （浏览器控制台等价路径）会不会被函数自身的守卫拦下」「锁定态是否真的不渲染箭头」
 * 「FINANCE_MODE='local' 时是否没有订正入口」——这三条都是**运行期行为**，静态正则只能看接线。
 * 这里真渲染、真执行函数、真观察有没有发出 POST（/api/** 全部本地打桩，绝不碰生产接口）。
 *
 * 与 tests/verify_order_status_edit_render.mjs 的差异（不复述它已覆盖的解锁/改密码/菜单项）：
 *   ① 锁定态**主动调用** applyOrderStatus / openOrderStatusMenu（控制台等价路径）→ 必须不发请求；
 *   ② 解锁态用**非白名单**状态调用 → 必须被函数自身拦下；
 *   ③ 连点两次（双击/脚本连调）→ 只允许发 1 个请求（_orderStatusSaving 闸门）；
 *   ④ 不存在的 order_id → 必须不发请求；
 *   ⑤ 重新锁定后再调用 → 必须不发请求；
 *   ⑥ ★ FINANCE_MODE='local' 回归守卫：入口本就不渲染，但**继续在控制台里调用**——
 *      _cloudData.orders 装的是**本地订单库的 id**（fetchLocalScopeData → _cloudData = data.data），
 *      若 applyOrderStatus 只判 unlocked、不判 FINANCE_MODE，就会把本地 id 当云端 id POST 出去
 *      （经本地代理附带真实 printer token 打到云端，改写云端同 id 的无关订单）。修复后守卫与
 *      渲染模板同源（_orderStatusEditable 含 FINANCE_MODE）：B3（默认锁定）/B4（控制台强制打开
 *      解锁位）都必须**零请求**、本地订单状态也不许落地；B5 用静态变异（把守卫退回旧版
 *      「只判 unlocked」）自证 B3/B4 的「零请求」判据确有判别力。
 *
 * 用法：node tests/verify_order_status_edit_adversarial.mjs    exit 0 = 未发现可证伪问题
 */
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require_ = createRequire(import.meta.url);
const { chromium } = require_('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, '..');
const FINANCE = path.join(ROOT, 'local_print_tool', 'finance');
const HTML_PATH = path.join(FINANCE, 'settlement.html');

let bad = 0;
let total = 0;
const findings = [];
function chk(name, cond, detail) {
  total++;
  if (!cond) bad++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (!cond && detail !== undefined ? `  -- ${detail}` : ''));
}
function note(m) { console.log('      · ' + m); }

/* ── 夹具：与既有渲染验收不同源（不同 id / 不同状态组合 / 不同成员） ── */
function ord(id, st, total, files) {
  return {
    order_id: id, order_number: 'ADV-' + id, source: 'app', openid: 'openid-adv', nickname: '',
    owner_name: '测试甲', status: st, total_price: total,
    created_at: '2026-10-18 15:0' + (id - 700000) + ':00', print_started_at: '', wait_seconds: null,
    is_admin_print: 0, files_count: files.length, total_page_count: 4 * files.length,
    received_client: '', received_label: '',
    files: files.map((f, i) => ({
      id: 'af' + id + '_' + i, file_name: 'adv' + i + '.pdf', copies: 1, page_count: 4,
      duplex: 'on', page_range: '', total_price: f, status: st, is_free: false,
    })),
  };
}
const CLOUD_ORDERS = [
  ord(700001, 'sent', 3.2, [3.2]),
  ord(700002, 'abandoned', 1.6, [1.6]),
  ord(700003, 'failed', 2.4, [2.4]),
];
/* 本地订单库：id 从 1 开始（与云端 id 空间天然碰撞） */
const LOCAL_ORDERS = [
  ord(1, 'sent', 8.8, [8.8]),
  ord(2, 'abandoned', 4.4, [4.4]),
];
const STORE = {
  config: { investRate: 0.05, scoreCap: 15, passwords: {}, members: [{ id: 'ma', name: '测试甲', color: '#4472C4' }] },
  records: { expenses: [], income: [], scores: [], transfers: [] },
  notes: {}, meta: { version: 1 },
};
const json = (res, o) => { res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(o)); };

const posted = [];       // /api/proxy/admin/order_status 收到的体
const postedType = [];   // /api/proxy/admin/order_type 收到的体（v7.1）
const otherApi = [];     // 其它 /api/** 调用（诊断用）
/* B5 变异自证用：把产品 HTML 在**内存里**改成「守卫退回旧版」再喂给浏览器（绝不落盘）。 */
let htmlOverride = null;
/* B5 变异自证用：让这一次写请求回 success:false，避免变异页在本地模式下继续 renderCloud。 */
let stubFailNext = false;
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  const p = decodeURIComponent(u.pathname);
  if (p.startsWith('/api/')) {
    otherApi.push(req.method + ' ' + p);
    if (p === '/api/health') return json(res, { success: true, cloud: true });
    if (p === '/api/proxy/admin/order_status') {
      let body = '';
      req.on('data', (c) => { body += c; });
      req.on('end', () => {
        let parsed = {};
        try { parsed = JSON.parse(body || '{}'); } catch (e) { parsed = { _raw: body }; }
        posted.push(parsed);
        if (stubFailNext) {
          return json(res, { success: false, message: '自证桩：本次不回落地' });
        }
        return json(res, { success: true, message: '订单状态已订正', order_id: parsed.order_id,
                           status: parsed.status, previous_status: 'sent', files_updated: 1 });
      });
      return;
    }
    if (p === '/api/proxy/admin/order_type') {
      let body = '';
      req.on('data', (c) => { body += c; });
      req.on('end', () => {
        let parsed = {};
        try { parsed = JSON.parse(body || '{}'); } catch (e) { parsed = { _raw: body }; }
        postedType.push(parsed);
        return json(res, { success: true, message: '订单类型已订正', order_id: parsed.order_id,
                           order_type: parsed.order_type, previous_order_type: 'receive',
                           is_admin_print: parsed.order_type === 'self' ? 1 : 0 });
      });
      return;
    }
    if (p === '/api/proxy/finance/config') return json(res, { success: true, data: STORE });
    if (p === '/api/proxy/pricing') return json(res, { success: true, data: { simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1, delivery_percentages: {}, urgency_prices: {} } });
    if (p === '/api/proxy/admin/statistics/months') return json(res, { success: true, data: { months: [{ month: '2026-10' }] } });
    if (p === '/api/proxy/admin/statistics/revenue') return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: CLOUD_ORDERS } });
    if (p === '/api/local/orders') return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: LOCAL_ORDERS } });
    if (p === '/api/local/data') return json(res, { success: true, data: null });
    if (p === '/api/local/bindings') return json(res, { success: true, data: {} });
    return json(res, { success: true, data: null });
  }
  if (p === '/favicon.ico') { res.writeHead(204); return res.end(); }
  /* gui.py 打开页面时的真实 URL：{url}/settlement.html?token=…&mode=cloud|local
     （mode 既由服务器注入、也由 URL 覆盖；这里两者一致，模拟真实启动路径） */
  const isSettlement = p === '/' || p === '/settlement.html';
  const file = p === '/' ? 'settlement.html' : p.replace(/^\//, '');
  const fp = isSettlement ? HTML_PATH : path.join(FINANCE, file);
  if (!fs.existsSync(fp) || fs.statSync(fp).isDirectory()) { res.writeHead(404); return res.end('nf'); }
  const ext = path.extname(fp);
  let body = fs.readFileSync(fp);
  if (isSettlement) {
    const qMode = u.searchParams.get('mode') === 'local' ? 'local' : 'cloud';
    let html = (htmlOverride !== null) ? htmlOverride : body.toString('utf8');
    html = html.split('__LAUNCH_TOKEN__').join('adv-launch-token');
    html = html.split('__FINANCE_MODE__').join(qMode);   // 占位符出现两次，必须全替换
    body = Buffer.from(html, 'utf8');
  }
  res.writeHead(200, { 'Content-Type': ext === '.html' ? 'text/html; charset=utf-8'
    : ext === '.js' ? 'application/javascript; charset=utf-8' : 'application/octet-stream' });
  res.end(body);
});

const port = await new Promise((r) => server.listen(0, '127.0.0.1', () => r(server.address().port)));
const BASE = `http://127.0.0.1:${port}`;
const browser = await chromium.launch();
const external = [];
const pageErrors = [];

async function openPage(mode) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on('request', (r) => { if (!r.url().startsWith(BASE) && !r.url().startsWith('data:')) external.push(r.url()); });
  page.on('pageerror', (e) => pageErrors.push(String(e.message)));
  // 与 gui.py 相同的启动 URL（mode 显式传存储模式）
  await page.goto(`${BASE}/settlement.html?token=adv-launch-token&mode=${mode}`, { waitUntil: 'domcontentloaded' });
  await page.click('#tabNav button[data-tab="cloud"]');
  try {
    await page.waitForSelector('.order-table tbody tr.order-row', { timeout: 12000 });
  } catch (e) {
    await page.evaluate(() => {
      if (typeof fetchLocalScopeData === 'function') fetchLocalScopeData(true);
      else if (typeof fetchScopeData === 'function') fetchScopeData(true);
    });
    await page.waitForSelector('.order-table tbody tr.order-row', { timeout: 12000 });
  }
  return page;
}

/* F8c（2026-10-18 审查）：原先只有 try/finally —— 功能整体缺失时会红在
 * waitForSelector / waitForFunction 的 TimeoutError 或 page.evaluate 里的 ReferenceError
 * （例如 applyOrderStatus 未定义）：finally 关掉浏览器后异常照旧抛穿，**不打印 FAIL 摘要**。
 * 加 catch 后：任何未捕获异常也计入 FAIL，摘要照打、exit 1。 */
let caught = null;
try {
  /* ══════════════ 云端模式：控制台锁定绕过 ══════════════ */
  console.log('=== A. FINANCE_MODE=cloud：锁定态 / 解锁态的控制台等价路径 ===');
  const page = await openPage('cloud');
  chk('A0 页面脚本无语法错误', pageErrors.length === 0, pageErrors.join(' | '));

  const snap = () => page.evaluate(() => ({
    locked: window.unlocked.orderstatus !== true,
    lockIcon: (document.querySelector('.order-table thead th:nth-child(5) .st-lock') || {}).textContent || '',
    rows: document.querySelectorAll('tr.order-row').length,
    edits: document.querySelectorAll('tr.order-row td:nth-child(5) .st-edit').length,
    carets: document.querySelectorAll('tr.order-row td:nth-child(5) .st-caret').length,
    typeEdits: document.querySelectorAll('tr.order-row td:nth-child(10) .st-edit').length,
    typeCarets: document.querySelectorAll('tr.order-row td:nth-child(10) .st-caret').length,
    typeMenu: !!document.getElementById('tyMenu'),
    menu: !!document.getElementById('stMenu'),
    toasts: [...document.querySelectorAll('#toastWrap .toast')].map((t) => t.textContent),
    statuses: (window._cloudData.orders || []).map((o) => o.status),
    localFound: (typeof _findCloudOrder === 'function') && !!_findCloudOrder(700001),
  }));

  // A1 锁定态：DOM 上没有入口
  let s = await snap();
  chk('A1 默认锁定：unlocked.orderstatus=false、🔒、0 个 .st-edit / 0 个 .st-caret',
    s.locked && s.lockIcon.trim() === '🔒' && s.edits === 0 && s.carets === 0 && s.rows === 3,
    JSON.stringify(s));
  chk('A1b 锁定态类型列同样没有入口（两列共用 unlocked.orderstatus）',
    s.typeEdits === 0 && s.typeCarets === 0, `typeEdits=${s.typeEdits}`);

  // A2 ★ 锁定态控制台直接调用 applyOrderStatus → 函数自身守卫必须拦下（不发请求）
  let n0 = posted.length;
  await page.evaluate(() => applyOrderStatus(700001, 'sent'));
  await page.waitForTimeout(500);
  s = await snap();
  chk('A2 锁定态控制台 applyOrderStatus(700001,"sent") → 不发任何 POST，且本地状态未被落地',
    posted.length === n0 && s.statuses[0] === 'sent' && s.toasts.some((t) => t.includes('🔒')),
    `posted=${posted.length - n0} statuses=${JSON.stringify(s.statuses)} toasts=${JSON.stringify(s.toasts)}`);
  if (posted.length !== n0) findings.push('锁定态 applyOrderStatus 仍然发出了写请求（守卫失效）');

  // A3 ★ 锁定态控制台直接调用 openOrderStatusMenu → 不得创建菜单
  await page.evaluate(() => openOrderStatusMenu({ stopPropagation() {}, currentTarget: null }, 700001));
  await page.waitForTimeout(300);
  s = await snap();
  chk('A3 锁定态控制台 openOrderStatusMenu(...) → 不创建 #stMenu',
    !s.menu && s.toasts.some((t) => t.includes('🔒')),
    `menu=${s.menu} toasts=${JSON.stringify(s.toasts)}`);
  if (s.menu) findings.push('锁定态 openOrderStatusMenu 仍能弹出菜单（守卫失效）');

  // A3b ★ 锁定态控制台直接调用 applyOrderType → 必须被函数自身守卫拦下
  let nT0 = postedType.length;
  await page.evaluate(() => applyOrderType(700001, 'self'));
  await page.waitForTimeout(400);
  s = await snap();
  chk('A3b 锁定态控制台 applyOrderType(700001,"self") → 不发任何 POST，标签不变',
    postedType.length === nT0 && s.toasts.some((t) => t.includes('🔒')),
    `postedType=${postedType.length - nT0} toasts=${JSON.stringify(s.toasts)}`);
  // A3c ★ 锁定态 openOrderTypeMenu → 不创建 #tyMenu
  await page.evaluate(() => openOrderTypeMenu({ stopPropagation() {}, currentTarget: null }, 700001));
  await page.waitForTimeout(300);
  s = await snap();
  chk('A3c 锁定态控制台 openOrderTypeMenu(...) → 不创建 #tyMenu',
    !s.typeMenu, `typeMenu=${s.typeMenu}`);

  // A4 正常解锁（真实密码流程，默认 123456）
  await page.click('.order-table thead th:nth-child(5) .st-lock');
  await page.waitForSelector('#modalRoot .modal-card', { timeout: 5000 });
  await page.fill('#modalRoot .modal-card input[type=password]', '123456');
  await page.click('#modalRoot .modal-card [data-act=ok]');
  await page.waitForTimeout(500);
  s = await snap();
  chk('A4 解锁后：🔓、每行 .st-edit/.st-caret 各 3 个', !s.locked && s.edits === 3 && s.carets === 3,
    JSON.stringify([s.locked, s.edits, s.carets]));
  chk('A4b 同一把锁：解锁状态列后类型列每行也各 3 个入口（不需要第二个密码）',
    s.typeEdits === 3 && s.typeCarets === 3, JSON.stringify([s.typeEdits, s.typeCarets]));

  // A5 解锁态：非白名单状态 → 函数自身必须拦（不发请求）
  n0 = posted.length;
  await page.evaluate(() => applyOrderStatus(700001, 'printing'));
  await page.waitForTimeout(500);
  s = await snap();
  chk('A5 解锁态但状态非白名单（printing）→ 不发 POST、提示「不允许改为该状态」',
    posted.length === n0 && s.toasts.some((t) => t.includes('不允许')),
    `posted=${posted.length - n0} toasts=${JSON.stringify(s.toasts)}`);
  if (posted.length !== n0) findings.push('非白名单状态仍被前端发出（白名单守卫失效）');

  // A5b ★ 解锁态但类型非白名单（auto）→ 不发 POST
  nT0 = postedType.length;
  await page.evaluate(() => applyOrderType(700001, 'auto'));
  await page.waitForTimeout(400);
  s = await snap();
  chk('A5b 解锁态但类型非白名单（auto）→ 不发 POST、提示「不允许改为该类型」',
    postedType.length === nT0 && s.toasts.some((t) => t.includes('不允许')),
    `postedType=${postedType.length - nT0} toasts=${JSON.stringify(s.toasts)}`);
  // A5c ★ 不存在的订单 id → 不发 POST
  nT0 = postedType.length;
  await page.evaluate(() => applyOrderType(424242, 'self'));
  await page.waitForTimeout(400);
  s = await snap();
  chk('A5c 类型订正：不在当前列表的 order_id → 不发 POST',
    postedType.length === nT0, `postedType=${postedType.length - nT0}`);

  // A6 连调两次（双击等价）→ 只允许 1 个请求，且本地状态正确落地
  n0 = posted.length;
  await page.evaluate(() => { applyOrderStatus(700002, 'failed'); applyOrderStatus(700002, 'failed'); });
  await page.waitForTimeout(800);
  s = await snap();
  chk('A6 脚本连调两次同一单同一状态 → 只发出 1 个 POST（_orderStatusSaving 闸门）',
    posted.length - n0 === 1 && s.statuses[1] === 'failed',
    `posted=${posted.length - n0} statuses=${JSON.stringify(s.statuses)}`);
  if (posted.length - n0 === 2) findings.push('双击/连调导致重复写库（闸门未拦住第二次）');

  // A7 不存在的订单 id → 不发请求
  n0 = posted.length;
  await page.evaluate(() => applyOrderStatus(424242, 'sent'));
  await page.waitForTimeout(400);
  s = await snap();
  chk('A7 不在当前列表的 order_id → 不发 POST、提示刷新',
    posted.length === n0 && s.toasts.some((t) => t.includes('不在当前列表')),
    `posted=${posted.length - n0} toasts=${JSON.stringify(s.toasts)}`);

  // A8 重新锁定后再调 → 仍被拦
  await page.click('.order-table thead th:nth-child(5) .st-lock');
  await page.waitForTimeout(300);
  n0 = posted.length;
  await page.evaluate(() => applyOrderStatus(700003, 'sent'));
  await page.waitForTimeout(400);
  s = await snap();
  chk('A8 重新锁定（🔒）后控制台调用 → 不发 POST、箭头已收起',
    posted.length === n0 && s.locked && s.edits === 0 && s.carets === 0,
    `posted=${posted.length - n0} locked=${s.locked} edits=${s.edits} carets=${s.carets}`);
  chk('A8b 重新锁定后类型列入口也一起收起（一把锁管两列）',
    s.typeEdits === 0 && s.typeCarets === 0, `typeEdits=${s.typeEdits}`);
  await page.close();

  /* ══════════════ 本地模式：入口不渲染 + 控制台跨库写入 ══════════════ */
  console.log('\n=== B. FINANCE_MODE=local：入口缺失 / 控制台路径 ===');
  const lp = await openPage('local');
  const ls = await lp.evaluate(() => ({
    mode: window.FINANCE_MODE,
    lockHTML: (typeof _orderStatusLockHTML === 'function') ? _orderStatusLockHTML() : 'N/A',
    lockBtn: document.querySelectorAll('.st-lock').length,
    edits: document.querySelectorAll('tr.order-row td:nth-child(5) .st-edit').length,
    carets: document.querySelectorAll('tr.order-row td:nth-child(5) .st-caret').length,
    typeEdits: document.querySelectorAll('tr.order-row td:nth-child(10) .st-edit').length,
    rows: document.querySelectorAll('tr.order-row').length,
    orderIds: (window._cloudData.orders || []).map((o) => o.order_id),
    firstTag: (document.querySelector('tr.order-row td:nth-child(5) .tag') || {}).textContent || '',
  }));
  chk('B1 FINANCE_MODE 注入为 local、本地订单已渲染（id 是本地库的 1..N）',
    ls.mode === 'local' && ls.rows === 2 && JSON.stringify(ls.orderIds) === '[1,2]',
    JSON.stringify([ls.mode, ls.rows, ls.orderIds]));
  chk('B2 本地模式没有订正入口：_orderStatusLockHTML()==="" 且 .st-lock/.st-edit/.st-caret 全为 0',
    ls.lockHTML === '' && ls.lockBtn === 0 && ls.edits === 0 && ls.carets === 0,
    JSON.stringify(ls));
  chk('B2b 本地模式类型列同样没有入口（类型锁与状态锁同一判据）',
    ls.typeEdits === 0, `typeEdits=${ls.typeEdits}`);

  // B3/B4 ★ 回归守卫（本块原为「断言缺陷存在」的探针，功能方修复后已翻转）：
  //     本地模式下 _cloudData.orders 装的是**本地订单库的 id**（1..N），而 /api/proxy/admin/order_status
  //     打到的是云端 —— 一旦 applyOrderStatus 只判 unlocked 不判 FINANCE_MODE，控制台就能把本地 id
  //     当云端 id 写进云端库。修复后守卫与渲染模板同源（_orderStatusEditable 含 FINANCE_MODE 判据）：
  //     默认（锁定）与「控制台强制打开解锁位」两种情况都必须**一个写请求都不发**、本地状态不许落地。
  //     必须选一个「状态与目标不同」的本地单（本地 #2 = abandoned），否则会撞上
  //     `o.status === status` 的空操作分支而看不出判别力。
  const readLocal = () => lp.evaluate(() => ({
    status2: (window._cloudData.orders || []).filter((o) => String(o.order_id) === '2')
      .map((o) => o.status)[0],
    toasts: [...document.querySelectorAll('#toastWrap .toast')].map((t) => t.textContent),
  }));
  const probe = await lp.evaluate(() => ({
    locked: window.unlocked.orderstatus !== true,
    found: (typeof _findCloudOrder === 'function') ? !!_findCloudOrder(2) : null,
    cur: (window._cloudData.orders || []).filter((o) => String(o.order_id) === '2')
      .map((o) => o.status)[0],
    hasModeGuard: /FINANCE_MODE/.test(String(applyOrderStatus)),
  }));
  note(`_findCloudOrder(2) 在本地模式下命中本地订单 = ${probe.found}（当前状态 ${probe.cur}）`
       + `；applyOrderStatus 源码里是否提到 FINANCE_MODE = ${probe.hasModeGuard}`);

  // B3 默认（锁定）态：控制台调用 → 零请求
  const n1 = posted.length;
  await lp.evaluate(() => applyOrderStatus(2, 'sent'));
  await lp.waitForTimeout(600);
  const p1 = posted.slice(n1);
  const l1 = await readLocal();
  note(`本地模式锁定态控制台调用后收到的写请求 = ${JSON.stringify(p1)}`);
  chk('B3 本地模式默认（锁定）态控制台 applyOrderStatus(2,"sent") → 不发任何写请求、'
      + '本地订单状态未被落地、提示「本地模式下不支持」',
    p1.length === 0 && l1.status2 === probe.cur && l1.toasts.some((t) => t.includes('本地模式')),
    `posted=${JSON.stringify(p1)} status2=${l1.status2}/${probe.cur} toasts=${JSON.stringify(l1.toasts)}`);
  if (p1.length > 0) findings.push('本地模式锁定态仍发出了写请求（守卫失效）');

  // B4 控制台把解锁位打开（等价于「先解锁、再切到本地模式」）→ 同源判据仍必须拦住
  const n2 = posted.length;
  await lp.evaluate(() => { window.unlocked.orderstatus = true; applyOrderStatus(2, 'sent'); });
  await lp.waitForTimeout(600);
  const p2 = posted.slice(n2);
  const l2 = await readLocal();
  note(`本地模式强制解锁位后收到的写请求 = ${JSON.stringify(p2)}`);
  chk('B4 本地模式 + 控制台强制打开解锁位 → 仍不发任何 /api/proxy/admin/order_status 请求、'
      + '本地订单状态未被落地（FINANCE_MODE 同源判据兜底）',
    p2.length === 0 && l2.status2 === probe.cur && l2.toasts.some((t) => t.includes('本地模式')),
    `posted=${JSON.stringify(p2)} status2=${l2.status2}/${probe.cur} toasts=${JSON.stringify(l2.toasts)}`);
  if (p2.length > 0) {
    findings.push('【跨库 id 泄露】FINANCE_MODE=local 时 applyOrderStatus 只判 unlocked 不判模式，'
      + `把本地订单 id ${JSON.stringify(p2.map((b) => b.order_id))} POST 到 /api/proxy/admin/order_status`
      + '（POST 经本地代理会附带真实 printer token 打到云端，改写云端同 id 的无关订单）');
  }

  // B5 ★ 判据自证（静态变异）：把产品源码里的同源守卫退回旧版「只判 unlocked」，同一探针**必须**
  //     真的发出写请求 —— 否则 B3/B4 的「零请求」可能只是计数器/桩没接上（判据空转）。
  //     变异只发生在内存里（桩按 htmlOverride 喂给浏览器），产品文件一个字节都不动。
  await lp.close();
  const rawHtml = fs.readFileSync(HTML_PATH, 'utf8');
  const GUARD_NEW = 'if (!_orderStatusEditable(oid)) {';
  const GUARD_OLD = 'if (!unlocked.orderstatus) {';
  const nGuard = rawHtml.split(GUARD_NEW).length - 1;
  // 2026-10-18（lead 按上一轮报告的统一守卫建议改了页面）：同一同源守卫现在在页面里有**两处**
  // （openOrderStatusMenu 与 applyOrderStatus）—— 自证必须把**所有**出现都退回旧版，
  // 否则被探针直接调用的 applyOrderStatus 仍是新守卫、变异体发不出请求，自证会假红。
  htmlOverride = (nGuard >= 1) ? rawHtml.split(GUARD_NEW).join(GUARD_OLD) : null;
  let mutPosted = [];
  let mutErr = null;
  if (htmlOverride !== null) {
    const mp = await openPage('local');
    const n3 = posted.length;
    stubFailNext = true;                     // 让变异请求回 success:false，避免变异页继续 renderCloud
    try {
      await mp.evaluate(() => { window.unlocked.orderstatus = true; applyOrderStatus(2, 'sent'); });
      await mp.waitForTimeout(700);
    } catch (e) { mutErr = String(e); }
    stubFailNext = false;
    mutPosted = posted.slice(n3);
    await mp.close();
  }
  htmlOverride = null;
  chk('B5 [自证] 静态变异：守卫退回旧版「只判 unlocked」后，同一探针**确实**发出 1 个写请求'
      + '（证明 B3/B4 的「零请求」判据有判别力）',
    nGuard >= 1 && mutErr === null && mutPosted.length === 1
      && String(mutPosted[0].order_id) === '2' && mutPosted[0].status === 'sent',
    `guardMatches=${nGuard} posted=${JSON.stringify(mutPosted)} err=${mutErr}`);
  if (nGuard < 1 || mutErr !== null || mutPosted.length !== 1) {
    findings.push('判据自证失败：变异体（旧守卫）没有发出预期写请求 —— B3/B4 的「零请求」判据可能空转');
  }

  chk('B6 全程无外部网络请求（无 CDN / 无生产接口）', external.length === 0, external.slice(0, 3).join(' | '));
  chk('B7 全程无页面 JS 异常', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
} catch (e) {
  caught = e;
  chk('主流程未抛异常（崩溃 = 页面/功能整体缺失或选择器失效）', false,
    String((e && e.stack) || e).split('\n').slice(0, 4).join(' | '));
} finally {
  await browser.close();
  server.close();
}

console.log('\n' + '='.repeat(60));
console.log(`已跑断言 ${total} 项，失败 ${bad} 项`);
if (caught) {
  console.log('异常终止：' + String((caught && caught.stack) || caught).split('\n')[0]);
}
if (findings.length) {
  console.log('发现的问题（需上报）：');
  for (const f of findings) console.log('  ✗ ' + f);
} else {
  console.log('未发现可证伪的问题');
}
console.log(`FAIL 计数 = ${bad}`);
process.exit(bad ? 1 : 0);
