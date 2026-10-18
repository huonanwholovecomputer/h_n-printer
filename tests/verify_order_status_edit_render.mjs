/* 验收 · 收支清算「订单状态订正」交互（v7.0，2026-10-18 用户需求）
 *
 * 需求：云端数据 → 订单明细的「状态」列可直接订正状态；为防误触**默认锁定**，
 *       「状态」右侧一个 🔒 → 点击输密码解锁 → 🔓 → 此时每个订单的状态标签才带小箭头，
 *       点击后直接选正确状态、改动直接生效；再点 🔓 回到 🔒。密码在设置界面管理（默认 123456）。
 *
 * 为什么必须真浏览器：解锁是一次真实密码校验（Web Crypto SHA-256）、弹窗是真实 DOM；
 * 「锁定态没有箭头 / 解锁态每行都有箭头」是渲染结果；提交后 KPI 与状态标签要真的跟着变。
 * 这些只有真渲染能回答。
 *
 * 做法：起本地 HTTP 服务（静态文件来自 local_print_tool/finance/，/api/** 全部打桩，
 * 绝不发外网请求），Playwright 打开 settlement.html → 切「云端数据」→ 逐场景断言：
 *   ① 默认锁定：表头 🔒、行内无箭头/无点击入口；
 *   ② 密码错 → 仍锁定；密码对（默认 123456）→ 🔓 且每行出现 ▾；
 *   ③ 点状态标签 → 菜单列出 5 个终态、当前项 ✓ 当前 且不可点；
 *   ④ 选「打印失败」→ 真实 POST /api/proxy/admin/order_status（order_id + status），
 *      行内标签变「打印失败」、文件级状态一起变、云端收益记录 KPI 跟着少掉该单金额；
 *   ⑤ 点 🔓 锁定 → 箭头消失、菜单收起，锁定态点状态标签不弹菜单；
 *   ⑥ 设置页「订单状态订正 密码」可改（默认 123456）→ 改后旧密码失效、新密码可解锁。
 * 产物：截图存 截图展示/订单状态订正/。
 *
 * 用法：node tests/verify_order_status_edit_render.mjs
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
const ROOT = path.join(HERE, '..');
const FINANCE = path.join(ROOT, 'local_print_tool', 'finance');
const HTML_PATH = process.argv[2] || path.join(FINANCE, 'settlement.html');
const OUT_DIR = path.join(ROOT, '截图展示', '订单状态订正');

let bad = 0;
function chk(name, cond, detail) {
  if (!cond) bad++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (!cond && detail !== undefined ? `  -- ${detail}` : ''));
}

/* ── 夹具：3 单（2 单 sent 计入收益、1 单 abandoned 不计入） ───────────────── */
function ord(id, st, total, files) {
  return {
    order_id: id,
    order_number: 'HN20261018-00' + (id - 900000),
    source: 'local', openid: 'local', nickname: '', owner_name: '欧阳娜娜',
    status: st, total_price: total,
    created_at: '2026-10-18 12:38:0' + (id - 900000),
    print_started_at: '2026-10-18 12:38:1' + (id - 900000),
    wait_seconds: 42, is_admin_print: 0,
    files_count: files.length, total_page_count: 8 * files.length,
    received_client: '', received_label: '',
    files: files.map((f, i) => ({
      id: 'f' + id + i, file_name: 'a' + i + '.pdf', copies: 1, page_count: 8,
      duplex: 'on', page_range: '', total_price: f, status: st, is_free: false,
    })),
  };
}
const ORDERS = [
  ord(900001, 'sent', 4.6, [4.6]),
  ord(900002, 'abandoned', 2.3, [2.3]),
  ord(900003, 'sent', 1.5, [1.5, 0]),
];
const STORE = {
  config: { investRate: 0.05, scoreCap: 15, passwords: {},
            members: [{ id: 'ma', name: '欧阳娜娜', color: '#4472C4' }] },
  records: { expenses: [], income: [], scores: [], transfers: [] },
  notes: {}, meta: { version: 1 },
};
const json = (res, obj) => { res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(obj)); };

const posted = [];      // 收到的 /api/proxy/admin/order_status 请求体
const saved = [];       // 收到的 finance/config 保存
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  const p = decodeURIComponent(u.pathname);
  if (p.startsWith('/api/')) {
    if (p === '/api/health') return json(res, { success: true, cloud: true });
    if (p === '/api/proxy/admin/order_status') {
      let body = '';
      req.on('data', (c) => { body += c; });
      req.on('end', () => {
        let parsed = {};
        try { parsed = JSON.parse(body || '{}'); } catch (e) { parsed = { _raw: body }; }
        posted.push(parsed);
        // 桩不做真实写库（真后端行为由 verify_order_status_edit.py 钉住），只回执
        return json(res, { success: true, message: '订单状态已订正', order_id: parsed.order_id,
                           status: parsed.status, previous_status: 'sent', files_updated: 1 });
      });
      return;
    }
    if (p === '/api/proxy/finance/config') {
      if (req.method === 'POST') {
        let body = '';
        req.on('data', (c) => { body += c; });
        req.on('end', () => { try { saved.push(JSON.parse(body)); } catch (e) { saved.push({ _raw: body }); }
                              json(res, { success: true, server_version: 2 }); });
        return;
      }
      return json(res, { success: true, data: STORE });
    }
    if (p === '/api/proxy/pricing') return json(res, { success: true, data: { simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1, delivery_percentages: {}, urgency_prices: {} } });
    if (p === '/api/proxy/admin/statistics/months') return json(res, { success: true, data: { months: [{ month: '2026-10' }] } });
    if (p === '/api/proxy/admin/statistics/revenue') return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: ORDERS } });
    if (p === '/api/local/data') return json(res, { success: true, data: null });
    if (p === '/api/local/bindings') return json(res, { success: true, data: {} });
    return json(res, { success: true, data: null });
  }
  const file = p === '/' ? 'settlement.html' : p.replace(/^\//, '');
  const fp = file === 'settlement.html' ? HTML_PATH : path.join(FINANCE, file);
  if (!fs.existsSync(fp) || fs.statSync(fp).isDirectory()) { res.writeHead(404); return res.end('not found'); }
  const ext = path.extname(fp);
  res.writeHead(200, { 'Content-Type': ext === '.html' ? 'text/html; charset=utf-8' : ext === '.js' ? 'application/javascript; charset=utf-8' : 'application/octet-stream' });
  res.end(fs.readFileSync(fp));
});

const port = await new Promise((r) => server.listen(0, '127.0.0.1', () => r(server.address().port)));
const BASE = `http://127.0.0.1:${port}`;
fs.mkdirSync(OUT_DIR, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
const pageErrors = [];
const external = [];
page.on('pageerror', (e) => pageErrors.push(process.env.PW_DEBUG_ERRORS ? String(e.stack || e.message) : e.message));
page.on('request', (r) => { if (!r.url().startsWith(BASE) && !r.url().startsWith('data:')) external.push(r.url()); });

const snap = () => page.evaluate(() => ({
  locked: window.unlocked.orderstatus !== true,
  lockIcon: (document.querySelector('.order-table thead th:nth-child(5) .st-lock') || {}).textContent || '',
  rows: document.querySelectorAll('tr.order-row').length,
  edits: document.querySelectorAll('tr.order-row .st-edit').length,
  carets: document.querySelectorAll('tr.order-row .st-caret').length,
  menu: !!document.getElementById('stMenu'),
  menuItems: [...document.querySelectorAll('#stMenu .st-menu-item')].map((b) => b.textContent.trim()),
  menuCur: [...document.querySelectorAll('#stMenu .st-menu-item.cur')].map((b) => b.textContent.trim()),
  tags: [...document.querySelectorAll('tr.order-row td:nth-child(5) .tag')].map((t) => t.textContent.trim()),
  tagCls: [...document.querySelectorAll('tr.order-row td:nth-child(5) .tag')].map((t) => t.className),
  kpi: (document.querySelector('.hero-card.c-green .num') || {}).getAttribute
        ? document.querySelector('.hero-card.c-green .num').getAttribute('data-count') : '',
  statuses: (window._cloudData.orders || []).map((o) => o.status),
  fileStatuses: (window._cloudData.orders || []).map((o) => (o.files || []).map((f) => f.status).join('/')),
  toasts: [...document.querySelectorAll('#toastWrap .toast')].map((t) => t.textContent),
}));
/* 走真实解锁流程：点锁 → 弹窗输入密码 → 点「解锁」 */
async function tryUnlock(pwd) {
  await page.click('.order-table thead th:nth-child(5) .st-lock');
  await page.waitForSelector('#modalRoot .modal-card', { timeout: 5000 });
  await page.fill('#modalRoot .modal-card input[type=password]', pwd);
  await page.click('#modalRoot .modal-card [data-act=ok]');
  await page.waitForTimeout(500);
}

try {
  await page.goto(BASE + '/settlement.html', { waitUntil: 'domcontentloaded' });
  chk('页面脚本加载无语法错误', pageErrors.length === 0, pageErrors.join(' | '));

  await page.click('#tabNav button[data-tab="cloud"]');
  await page.waitForFunction(() => window._cloudData && window._cloudData.orders && document.querySelector('.order-table'), null, { timeout: 20000 });
  console.log('云端数据页已渲染');

  /* ── ① 默认锁定 ───────────────────────────────────────────────────────── */
  let s = await snap();
  chk('默认锁定：unlocked.orderstatus 为 false', s.locked, JSON.stringify(s.locked));
  chk('「状态」表头右侧有锁图标且是 🔒', s.lockIcon.trim() === '🔒', JSON.stringify(s.lockIcon));
  chk('锁定态行内没有任何状态订正入口（.st-edit / .st-caret 均为 0）',
    s.edits === 0 && s.carets === 0, `edits=${s.edits} carets=${s.carets}`);
  chk('锁定态点状态标签不弹菜单',
    await (async () => { await page.click('tr.order-row td:nth-child(5)'); await page.waitForTimeout(200);
                         return !(await page.$('#stMenu')); })());
  await page.keyboard.press('Escape');
  await page.locator('#cloud-orders').screenshot({ path: path.join(OUT_DIR, '01-默认锁定-状态列只有标签.png') });

  /* ── ② 密码：错 → 仍锁定；对 → 解锁，每行出现 ▾ ──────────────────────── */
  await tryUnlock('000000');
  s = await snap();
  chk('密码错误 → 仍锁定、无箭头，且提示密码错误',
    s.locked && s.edits === 0 && s.toasts.some((t) => t.includes('密码错误')),
    JSON.stringify({ locked: s.locked, edits: s.edits, toasts: s.toasts }));
  chk('密码错误 → 弹窗已关闭', await page.$('#modalRoot .modal-card') === null);

  await tryUnlock('123456');
  s = await snap();
  chk('密码正确（默认 123456）→ 解锁：unlocked.orderstatus = true', !s.locked, JSON.stringify(s.locked));
  chk('解锁后锁图标变 🔓', s.lockIcon.trim() === '🔓', JSON.stringify(s.lockIcon));
  chk('解锁后每行都有状态订正入口与 ▾（行数一致）',
    s.edits === s.rows && s.carets === s.rows && s.rows === 3, `rows=${s.rows} edits=${s.edits} carets=${s.carets}`);
  chk('解锁后状态标签仍是原状态（未误改）',
    JSON.stringify(s.tags) === JSON.stringify(['已完成', '放弃打印', '已完成']) &&
    JSON.stringify(s.statuses) === JSON.stringify(['sent', 'abandoned', 'sent']),
    JSON.stringify([s.tags, s.statuses]));
  await page.locator('#cloud-orders').screenshot({ path: path.join(OUT_DIR, '02-解锁后-每单带小箭头.png') });

  /* ── ③ 菜单：5 个终态 + 当前项标注 ───────────────────────────────────── */
  await page.click('tr.order-row:nth-child(1) td:nth-child(5) .st-edit');
  await page.waitForSelector('#stMenu', { timeout: 3000 });
  s = await snap();
  chk('点状态标签弹出菜单', s.menu);
  chk('菜单列出 5 个终态（已完成/失败/放弃打印/已取消/被打回，标签与其他列同一套 statusLabel）',
    ['已完成', '失败', '放弃打印', '已取消', '被打回'].every((t) => s.menuItems.some((x) => x.indexOf(t) === 0)) &&
    s.menuItems.length === 5, JSON.stringify(s.menuItems));
  chk('菜单标明当前状态（✓ 当前）且该项不可点',
    s.menuCur.length === 1 && s.menuCur[0].includes('已完成') &&
    await page.evaluate(() => document.querySelector('#stMenu .st-menu-item.cur').disabled === true),
    JSON.stringify(s.menuCur));
  chk('菜单里没有非终态（排队中/打印中/添加中…不可选）',
    s.menuItems.every((t) => !/排队中|打印中|待添加|已添加|预留|预约|等待打印|传输中/.test(t)),
    JSON.stringify(s.menuItems));
  await page.screenshot({ path: path.join(OUT_DIR, '03-状态菜单.png') });

  /* ── ④ 选「失败」→ 真实提交 + 立即生效（标签 / 文件级 / KPI） ────── */
  const kpiBefore = parseInt(s.kpi, 10);
  const item = await page.evaluateHandle(() =>
    [...document.querySelectorAll('#stMenu .st-menu-item')].find((b) => b.textContent.indexOf('失败') === 0));
  await item.asElement().click();
  await page.waitForTimeout(600);
  s = await snap();
  chk('提交打到 /api/proxy/admin/order_status，body = {order_id, status}',
    posted.length === 1 && String(posted[0].order_id) === '900001' && posted[0].status === 'failed',
    JSON.stringify(posted));
  chk('菜单提交后自动收起', !s.menu);
  chk('行内标签立即变「失败」（无需刷新）', s.tags[0] === '失败', JSON.stringify(s.tags));
  chk('标签配色跟着变（st-failed）', /st-failed/.test(s.tagCls[0]), JSON.stringify(s.tagCls));
  chk('本地落地：订单状态与文件级状态一起变（展开行显示的就是新状态）',
    s.statuses[0] === 'failed' && s.fileStatuses[0] === 'failed', JSON.stringify([s.statuses, s.fileStatuses]));
  chk('KPI 云端收益记录按新状态重算（该单 ¥4.60 从收益里扣掉）',
    kpiBefore - parseInt(s.kpi, 10) === 460, `${kpiBefore} → ${s.kpi}`);
  chk('提示「状态已改为『失败』」',
    s.toasts.some((t) => t.includes('状态已改为') && t.includes('失败')), JSON.stringify(s.toasts));
  await page.locator('#cloud-orders').screenshot({ path: path.join(OUT_DIR, '04-订正为失败-立即生效.png') });

  /* ── ⑤ 点 🔓 回到锁定 ────────────────────────────────────────────────── */
  await page.click('.order-table thead th:nth-child(5) .st-lock');
  await page.waitForTimeout(300);
  s = await snap();
  chk('点 🔓 → 重新锁定：锁图标回到 🔒', s.locked && s.lockIcon.trim() === '🔒', JSON.stringify([s.locked, s.lockIcon]));
  chk('重新锁定后箭头与入口全部收起（.st-edit / .st-caret = 0）',
    s.edits === 0 && s.carets === 0, `edits=${s.edits} carets=${s.carets}`);
  chk('重新锁定后菜单不残留', !s.menu);
  chk('锁定态点状态标签仍不弹菜单',
    await (async () => { await page.locator('tr.order-row').nth(1).locator('td:nth-child(5)').click();
                         await page.waitForTimeout(200);
                         return !(await page.$('#stMenu')); })());
  chk('订正结果不会被锁定动作吞掉（第 1 单仍是「失败」）',
    (await page.locator('tr.order-row').nth(0).locator('td:nth-child(5) .tag').textContent()).trim() === '失败');
  await page.locator('#cloud-orders').screenshot({ path: path.join(OUT_DIR, '05-重新锁定-箭头收起.png') });

  /* ── ⑥ 密码在设置页管理：改成 654321 后旧密码失效 ────────────────────── */
  await page.click('#tabNav button[data-tab="settings"]');
  await page.waitForTimeout(200);
  chk('设置已锁定 → 先解锁设置（默认 123456）',
    await (async () => {
      const btn = await page.$('#tab-settings .btn.pri');
      if (btn) { await btn.click(); await page.waitForSelector('#modalRoot .modal-card');
                 await page.fill('#modalRoot .modal-card input[type=password]', '123456');
                 await page.click('#modalRoot .modal-card [data-act=ok]'); await page.waitForTimeout(500); }
      return !!(await page.$('#pwd-orderstatus'));
    })());
  chk('设置页有「订单状态订正 密码」输入框（新模块自动出现在密码管理里）',
    await page.$eval('label + input#pwd-orderstatus', () => true).catch(() => false) ||
    !!(await page.$('#pwd-orderstatus')));
  await page.evaluate(() => { document.getElementById('pwd-orderstatus').value = '654321'; });
  await page.evaluate(() => updatePassword('orderstatus'));
  await page.waitForTimeout(600);
  const stored = await page.evaluate(() => (window.STORE.config.passwords || {}).orderstatus || '');
  chk('保存后只存 SHA-256 哈希（不存明文）',
    /^sha256:[0-9a-f]{64}$/.test(stored) && !stored.includes('654321'), JSON.stringify(stored));
  chk('保存会同步到云端配置（POST /api/proxy/finance/config）', saved.length >= 1, JSON.stringify(saved.length));

  await page.click('#tabNav button[data-tab="cloud"]');
  await page.waitForSelector('.order-table', { timeout: 5000 });
  await tryUnlock('123456');
  s = await snap();
  chk('改了密码之后：旧密码 123456 不再能解锁', s.locked && s.edits === 0,
    JSON.stringify({ locked: s.locked, toasts: s.toasts }));
  await tryUnlock('654321');
  s = await snap();
  chk('新密码 654321 可解锁（密码确实由设置页管理）', !s.locked && s.edits === s.rows,
    JSON.stringify({ locked: s.locked, edits: s.edits, rows: s.rows }));
  await page.locator('#cloud-orders').screenshot({ path: path.join(OUT_DIR, '06-设置页改密码后新密码可解锁.png') });

  /* ── ⑦ 深色主题（用户现场截图就是深色界面）───────────────────────────── */
  await page.evaluate(() => { document.documentElement.setAttribute('data-theme', 'dark'); });
  await page.waitForTimeout(200);
  await page.locator('tr.order-row').nth(0).locator('td:nth-child(5) .st-edit').click();
  await page.waitForSelector('#stMenu', { timeout: 3000 });
  const dark = await page.evaluate(() => {
    const m = document.getElementById('stMenu');
    const r = m.getBoundingClientRect();
    const sent = [...m.querySelectorAll('.tag')].find((t) => t.textContent.trim() === '已完成');
    return {
      size: m.offsetWidth + 'x' + m.offsetHeight,
      inside: r.left >= 0 && r.top >= 0 && r.right <= window.innerWidth + 1 && r.bottom <= window.innerHeight + 1,
      sentBg: sent ? getComputedStyle(sent).backgroundColor : '',
      menuBg: getComputedStyle(m).backgroundColor,
      itemCount: m.querySelectorAll('.st-menu-item').length,
    };
  });
  chk('深色主题下菜单可见且 5 项齐全', dark.size !== '0x0' && dark.itemCount === 5, JSON.stringify(dark));
  chk('深色主题下菜单不越出视口（贴边翻转逻辑生效）', dark.inside, JSON.stringify(dark));
  chk('深色主题下状态标签走深色配色（浅色 #E8F5E9 → 深色 rgba(129,199,132,.15)）',
    dark.sentBg === 'rgba(129, 199, 132, 0.15)', JSON.stringify(dark));
  chk('深色主题下菜单底色是深色卡片（不是白）',
    dark.menuBg !== 'rgb(255, 255, 255)', JSON.stringify(dark));
  await page.screenshot({ path: path.join(OUT_DIR, '07-深色主题-菜单.png') });
  await page.evaluate(() => {
    document.documentElement.setAttribute('data-theme', 'light');
    closeOrderStatusMenu();
  });

  chk('全程无外部网络请求（无 CDN / 无生产接口）', external.length === 0, external.slice(0, 3).join(' | '));
  chk('全程无页面 JS 异常', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
  console.log(`截图已存 → ${path.relative(ROOT, OUT_DIR)}`);
} finally {
  await browser.close();
  server.close();
}

console.log('\n' + '='.repeat(52));
console.log(bad ? `订单状态订正交互验收：存在失败 ❌ (${bad})` : '订单状态订正交互验收：全部通过 ✅');
process.exit(bad ? 1 : 0);
