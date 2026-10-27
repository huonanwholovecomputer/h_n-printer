/* 验收 · v7.7「累计实测寿命」界面与结算闭环（2026-10-26 用户需求）
 *
 * 需求原话：「既然总寿命是未知的，那就加一个不提供总寿命的模式，一直累积用量，
 *            什么时候用坏了，再把所有用纸情况统计起来，然后再按照用量计算折旧费用。」
 *
 * 为什么必须真浏览器：这一整套都是渲染 + 异步（累计读数要拉区间订单、结算要弹窗预览、
 * 写回后历史月份的折旧要立刻回溯）—— 静态 grep 只能证明"代码里写了"。
 *
 * 做法：起本地 HTTP 服务（静态文件来自 local_print_tool/finance/，/api/** 全部打桩，
 * 绝不发外网请求），Playwright 打开 settlement.html → 切到「🖨️ 打印机折旧」→ 逐场景断言：
 *   ① 累计中的机器：寿命栏实时显示「累计 N 页」、每页成本「累计中 · 未计提」（不是红字"寿命未填"）；
 *   ② ⚙ 累计参数：起算前已用页数写进去后，累计读数立刻变大（这是很多人会漏的一项）；
 *   ③ 💥 报废结算：弹窗给出「实测寿命 / 每页成本 / 逐月金额」（Σ == 追踪期应承担的寿命价值）；
 *   ④ 写回台账：life_pages = 实测、measured、status = 退役，并把 settled 快照 POST 到云端配置；
 *   ⑤ 结算后：台账显示「实测」，2026-10 的「设备折旧」立刻出现该月金额（历史回溯）；
 *   ⑥ 预估口径的老机器照旧可编辑寿命（零回归），切到累计要确认、且会清零旧寿命。
 *
 * 产物：截图存 截图展示/累计实测寿命/。
 * 用法：node tests/verify_printer_life_mode_ui.mjs
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
const OUT_DIR = path.join(HERE, '..', '截图展示', '累计实测寿命');
fs.mkdirSync(OUT_DIR, { recursive: true });

let bad = 0, total = 0;
function chk(name, cond, detail) {
  total++; if (!cond) bad++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (!cond && detail !== undefined ? `  -- ${detail}` : ''));
}

/* 订单：2026-09 两单 200 页（ma 120 / mb 80），2026-10 一单 300 页（ma 300）。
   全部落在 M232（DESKTOP-A-1）上；1020 也有单，用来证明"不串台"。 */
const mk = (id, num, day, pages, mid, owner, cid, label) => ({
  order_id: id, order_number: num, created_at: day, source: 'local', openid: 'local',
  owner_name: owner, status: 'sent', is_admin_print: 0, total_price: pages * 0.1,
  received_client: cid, received_label: label, files_count: 1, total_page_count: pages,
  files: [{ id: 'f' + id, status: 'sent', page_count: pages, copies: 1, duplex: 'off', page_range: '',
            total_price: pages * 0.1, is_free: false, effective_pages: pages, sheets: pages,
            total_pages: pages, total_sheets: pages }],
});
const ORDERS = [
  mk(1, 'HN20260910-0001', '2026-09-10 10:00:00', 120, 'ma', '霍楠', 'DESKTOP-A-1', '姚懿祥的设备 | DESKTOP-A-1'),
  mk(2, 'HN20260918-0002', '2026-09-18 10:00:00', 80, 'mb', '姚懿祥', 'DESKTOP-A-1', '姚懿祥的设备 | DESKTOP-A-1'),
  mk(3, 'HN20261020-0003', '2026-10-20 10:00:00', 300, 'ma', '霍楠', 'DESKTOP-A-1', '姚懿祥的设备 | DESKTOP-A-1'),
  mk(4, 'HN20261021-0004', '2026-10-21 10:00:00', 50, 'ma', '霍楠', 'R7000-1', '霍楠的设备 | R7000-1'),
];
const STORE = {
  config: { investRate: 0.05, scoreCap: 15, passwords: {},
            members: [{ id: 'ma', name: '霍楠', color: '#4472C4' }, { id: 'mb', name: '姚懿祥', color: '#548235' }],
            orderPrinterMap: {},
            printers: [
              /* 1020：老的「预估总寿命」口径（零回归用） */
              { id: 'p1', name: 'HP LaserJet 1020', model: 'HP LaserJet 1020', price: 1200, life_pages: 100000,
                client_id: 'R7000-1', status: 'active', owner: 'ma', billing: 'team', note: '霍楠的设备' },
              /* M232：新的「累计实测」口径 —— 不知道总寿命，寿命留 0 */
              { id: 'p2', name: 'HP LaserJet MFP M232-M237', model: 'HP LaserJet MFP M232-M237', price: 1000,
                life_pages: 0, life_mode: 'accumulate', pre_pages: 0, started_at: '',
                client_id: 'DESKTOP-A-1', status: 'active', owner: 'shared', billing: 'wear' } ] },
  records: { expenses: [
              { id: 'e1', date: '2026-09-05', item: '纸', amount: 60, payer: 'ma' },
              { id: 'e2', date: '2026-10-06', item: '纸', amount: 80, payer: 'ma' } ],
             income: [], scores: [], transfers: [] },
  notes: {}, meta: { version: 2 },
};
const DEVICES = [
  { client_id: 'R7000-1', device_name: 'R7000-AHP10', owner_name: '霍楠', online: true, is_active: true, auto_duplex: false },
  { client_id: 'DESKTOP-A-1', device_name: 'DESKTOP-EJGEB1V', owner_name: '姚懿祥', online: false, is_active: true, auto_duplex: true },
];
const json = (res, obj) => { res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(obj)); };
const writes = [];
const rangeHits = [];   // 记录累计/结算实际请求的区间（证明按"起算日~今天"取数）

const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  const p = decodeURIComponent(u.pathname);
  if (!p.startsWith('/api/')) {
    const file = path.join(FINANCE, p === '/' ? 'settlement.html' : p.replace(/^\//, ''));
    if (!fs.existsSync(file)) { res.writeHead(404); return res.end('not found'); }
    const ext = path.extname(file);
    const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8' };
    res.writeHead(200, { 'Content-Type': types[ext] || 'application/octet-stream' });
    return res.end(fs.readFileSync(file));
  }
  if (p === '/api/health') return json(res, { success: true, cloud: true });
  if (p === '/api/local/finance_auth/status') {
    return json(res, { success: true, modules: {}, needs_setup: [],
                       session: { active: false, modules: [], expires_in: 0 }, session_ttl: 1800,
                       read_only: false, integrity: 'ok' });
  }
  if (p === '/api/local/finance_auth/unlock') {
    return json(res, { success: true, session: 'stub-sess', expires_in: 1800, modules: ['orderprinter'], upgraded: false });
  }
  if (p === '/api/local/finance_auth/logout') return json(res, { success: true, revoked: true });
  if (p === '/api/proxy/finance/config') {
    if (req.method === 'POST') {
      let body = ''; req.on('data', (c) => { body += c; });
      req.on('end', () => { writes.push(body); json(res, { success: true, server_version: 3 }); });
      return;
    }
    return json(res, { success: true, data: STORE });
  }
  if (p === '/api/proxy/pricing') return json(res, { success: true, data: { simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1, delivery_percentages: {}, urgency_prices: {} } });
  if (p === '/api/proxy/admin/statistics/months') return json(res, { success: true, data: { months: [{ month: '2026-09' }, { month: '2026-10' }] } });
  if (p === '/api/proxy/admin/statistics/revenue') {
    /* 真实接口按区间过滤 —— 这里照做，才能证明累计/结算真的用了「起算日 ~ 今天」而不是"全部订单" */
    const s = (u.searchParams.get('start_date') || '').trim();
    const e = (u.searchParams.get('end_date') || '').trim();
    if (s || e) rangeHits.push((s || '') + '~' + (e || ''));
    const list = ORDERS.filter((o) => {
      const d = o.created_at.slice(0, 10);
      if (s && d < s) return false;
      if (e && d > e) return false;
      return true;
    });
    return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: list } });
  }
  if (p === '/api/proxy/printer/devices') return json(res, { success: true, devices: DEVICES });
  if (p === '/api/local/device') return json(res, { success: true, device: { client_id: 'R7000-1', device_name: 'R7000-AHP10', take_orders: true } });
  if (p === '/api/local/data') return json(res, { success: true, data: null });
  if (p === '/api/local/bindings') return json(res, { success: true, data: {} });
  return json(res, { success: true, data: null });
});

const port = await new Promise((r) => server.listen(0, '127.0.0.1', () => r(server.address().port)));
const browser = await chromium.launch({ args: [`--explicitly-allowed-ports=${port}`] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1100 }, colorScheme: 'dark', deviceScaleFactor: 1.4 });
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

await page.goto(`http://127.0.0.1:${port}/settlement.html`, { waitUntil: 'load' });
await page.waitForTimeout(700);
chk('页面没有 JS 异常', errors.length === 0, errors.slice(0, 3).join(' | '));

await page.click('#tabNav button[data-tab="printers"]');
await page.waitForTimeout(1200);

/* ── ① 锁定态：累计中的机器也能看出"这不是漏填" ── */
const lockedCell = await page.$eval('#printer-row-p2', (tr) => tr.innerText.replace(/\s+/g, ' ').trim());
chk('锁定态台账：M232 的寿命栏写「累计中」（不是空白 / 不是 0 页）',
  lockedCell.indexOf('累计中') >= 0, lockedCell);
chk('锁定态台账：「每页成本」不再显示红色「寿命未填」',
  lockedCell.indexOf('寿命未填') < 0, lockedCell);
await page.screenshot({ path: path.join(OUT_DIR, '01-台账-累计中-锁定.png') });

/* 解锁（直接翻状态位，避免走密码弹窗） */
await page.evaluate(() => { FINANCE_SESSION = 'stub-sess'; unlocked.orderprinter = true; renderPrinters(); });
await page.waitForTimeout(900);

/* ── ② 累计读数：进页就拉「起算日 ~ 今天」的订单，实时显示累计页数 ── */
const cum = await page.$eval('#printer-cum-p2', (el) => el.innerText.replace(/\s+/g, ' ').trim());
chk('累计读数 = 500 页（2026-09 的 200 + 2026-10 的 300，只算这台机）',
  cum.indexOf('累计 500 页') >= 0, cum);
chk('累计读数的 title 里注明「500 页有单」（起算前 0 页，统计区间也写清）',
  (await page.$eval('#printer-cum-p2 span', (el) => el.getAttribute('title') || '')).indexOf('500 页') >= 0,
  await page.$eval('#printer-cum-p2 span', (el) => el.getAttribute('title')));
chk('累计按「起算日 ~ 今天」取数（请求区间不是写死的整年/单月）',
  rangeHits.some((h) => /^2000-01-01~\d{4}-\d{2}-\d{2}$/.test(h)), JSON.stringify(rangeHits.slice(0, 4)));
const unitCell = await page.$eval('#printer-unit-p2', (el) => el.innerText.replace(/\s+/g, ' ').trim());
chk('每页成本：累计中 · 未计提（寿命未知时不乱算单价）', unitCell.indexOf('累计中 · 未计提') >= 0, unitCell);
chk('累计模式不会出现可手填寿命的输入框（避免又回到"猜一个总数"）',
  (await page.$eval('#printer-row-p2', (tr) => tr.querySelectorAll('input[onchange*="life_pages"]').length)) === 0);
await page.screenshot({ path: path.join(OUT_DIR, '02-台账-累计中-解锁.png') });

/* ── ③ ⚙ 累计参数：起算前已用页数（机器进系统之前打掉的量） ── */
await page.click('#printer-row-p2 a[title*="累计参数"]');
await page.waitForTimeout(400);
const modal1 = await page.$eval('.modal-card', (el) => el.innerText.replace(/\s+/g, ' ').trim());
chk('⚙ 累计参数弹窗给出「累计起算日」与「起算前已用页数」两项',
  modal1.indexOf('累计起算日') >= 0 && modal1.indexOf('起算前已用页数') >= 0, modal1);
await page.fill('#im-pre_pages', '100');
await page.screenshot({ path: path.join(OUT_DIR, '03-累计参数.png') });
await page.click('.modal-card [data-act=ok]');
await page.waitForTimeout(900);
const cum2 = await page.$eval('#printer-cum-p2', (el) => el.innerText.replace(/\s+/g, ' ').trim());
chk('填了「起算前已用 100 页」后累计读数变成 600 页，title 里注明 500 页有单 + 起算前 100',
  cum2.indexOf('累计 600 页') >= 0
  && (await page.$eval('#printer-cum-p2 span', (el) => el.getAttribute('title') || '')).indexOf('起算前已用 100 页') >= 0,
  cum2);

/* ── ④ 收支清算：累计中的机器本月折旧为 0，且文案说清"不是漏算" ── */
await page.click('#tabNav button[data-tab="dash"]');
await page.waitForTimeout(1200);
const dashBefore = await page.$eval('#tab-dash', (el) => el.innerText.replace(/\s+/g, ' '));
chk('收支清算 · 设备折旧：累计中的机器写「累计中」（不是红字寿命未填）',
  dashBefore.indexOf('累计中') >= 0 && dashBefore.indexOf('寿命未填') < 0,
  dashBefore.slice(0, 400));
chk('收支清算 · 设备折旧：页脚说明"累计实测 ⇒ 暂不计提，报废时一次性回溯"',
  dashBefore.indexOf('暂不计提') >= 0 || dashBefore.indexOf('报废时点') >= 0,
  dashBefore.slice(dashBefore.indexOf('每页成本 ='), dashBefore.indexOf('每页成本 =') + 260));
await page.screenshot({ path: path.join(OUT_DIR, '04-收支清算-累计中不计提.png') });

/* ── ⑤ 💥 报废结算：预览（实测寿命 / 每页成本 / 逐月金额），确认后写回台账 ── */
await page.click('#tabNav button[data-tab="printers"]');
await page.waitForTimeout(1000);
await page.click('#printer-row-p2 button[title*="报废结算"]');
await page.waitForTimeout(900);
const modal2 = await page.$eval('.modal-card', (el) => el.innerText.replace(/\s+/g, ' ').trim());
/* 实测寿命 600 页（500 有单 + 100 起算前）；价格 ¥1000；每页 ¥1.6667
   追踪期应承担 = 1000 × 500 / 600 = ¥833.33；2026-09（200 页）¥333.33、2026-10（300 页）¥500.00 */
chk('结算弹窗：实测寿命 600 页 + 每页 ¥1.6667', modal2.indexOf('600 页') >= 0 && modal2.indexOf('1.6667') >= 0, modal2);
chk('结算弹窗：逐月明细 2026-09 / 2026-10 都在，且合计 ¥833.33（起算前 100 页不进本账）',
  modal2.indexOf('2026-09') >= 0 && modal2.indexOf('2026-10') >= 0
  && modal2.indexOf('¥833.33') >= 0 && modal2.indexOf('¥166.67') >= 0, modal2);
chk('结算弹窗说清「确认后会标记报废 / 退役」', modal2.indexOf('报废') >= 0 && modal2.indexOf('退役') >= 0, modal2);
await page.screenshot({ path: path.join(OUT_DIR, '05-报废结算-预览.png') });
await page.click('.modal-card [data-act=ok]');
await page.waitForTimeout(1200);

const settledRow = await page.$eval('#printer-row-p2', (tr) => tr.innerText.replace(/\s+/g, ' ').trim());
chk('写回后台账：寿命显示「600 页 · 实测」，状态变退役',
  settledRow.indexOf('600 页') >= 0 && settledRow.indexOf('实测') >= 0 && settledRow.indexOf('退役') >= 0, settledRow);
chk('写回后每页成本按实测值算（¥1000 / 600 页 = ¥1.6667/页）',
  (await page.$eval('#printer-unit-p2', (el) => el.innerText.replace(/\s+/g, ' ').trim())).indexOf('1.6667') >= 0);
await page.screenshot({ path: path.join(OUT_DIR, '06-台账-结算后实测.png') });

/* 云端配置真的收到了快照（不只是内存里改了） */
const lastWrite = writes.length ? JSON.parse(writes[writes.length - 1]) : null;
const posted = lastWrite && lastWrite.data && lastWrite.data.config && Array.isArray(lastWrite.data.config.printers)
  ? lastWrite.data.config.printers.filter((x) => x.id === 'p2')[0] : null;
chk('settled 快照被写进云端配置（life_pages / measured / status / settled.months）',
  posted && posted.life_pages === 600 && posted.measured === true && posted.status === 'retired'
  && posted.settled && posted.settled.months && Object.keys(posted.settled.months).length === 2,
  JSON.stringify(posted && { life: posted.life_pages, m: posted.measured, st: posted.status }));
const sumCents = posted && posted.settled
  ? Object.keys(posted.settled.months).reduce((s, k) => s + (+posted.settled.months[k].cents || 0), 0) : -1;
chk('快照 Σ各月金额 ≡ 追踪期应承担的寿命价值（83333 分 = ¥833.33，逐月不差一分）',
  sumCents === 83333, String(sumCents));

/* ── ⑥ 历史回溯：2026-10 的「设备折旧」立刻出现该月金额（¥500.00） ── */
await page.click('#tabNav button[data-tab="dash"]');
await page.waitForTimeout(1500);
const dashAfter = await page.$eval('#tab-dash', (el) => el.innerText.replace(/\s+/g, ' '));
chk('结算后 2026-10 的设备折旧出现 ¥500.00（该月 300 页 × ¥1.6667 的整数分版本）',
  dashAfter.indexOf('¥500.00') >= 0, dashAfter.slice(dashAfter.indexOf('设备折旧'), dashAfter.indexOf('设备折旧') + 400));
/* 该行本身不再显示「累计中」（页脚说明文字里本来就写着这个词，不能拿整页搜） */
const dashRow = await page.evaluate(() => {
  const tbl = [...document.querySelectorAll('#tab-dash table')].find((t) => t.textContent.indexOf('本月成本') >= 0);
  if (!tbl) return '';
  const tr = [...tbl.querySelectorAll('tbody tr')].find((r) => r.textContent.indexOf('M232') >= 0);
  return tr ? tr.innerText.replace(/\s+/g, ' ').trim() : '';
});
chk('结算后该行改按实测单价计提（¥1.6667、不再是「累计中」）',
  dashRow.indexOf('1.6667') >= 0 && dashRow.indexOf('累计中') < 0, dashRow);
await page.screenshot({ path: path.join(OUT_DIR, '07-收支清算-回溯折旧.png') });

/* ── ⑦ 零回归：预估口径的 1020 照旧可改寿命；切到累计要确认并清零 ── */
await page.click('#tabNav button[data-tab="printers"]');
await page.waitForTimeout(1000);
const p1HasInput = await page.$eval('#printer-row-p1', (tr) => tr.querySelectorAll('input[onchange*="life_pages"]').length);
chk('预估口径的机器照旧给「寿命页数」输入框（零回归）', p1HasInput === 1, String(p1HasInput));
await page.click('#printer-row-p1 a:has-text("累计")');
await page.waitForTimeout(500);
const modal3 = await page.$eval('.modal-card', (el) => el.innerText.replace(/\s+/g, ' ').trim());
chk('切到「累计实测」会先确认，并明说现有预估寿命会被清零',
  modal3.indexOf('累计实测') >= 0 && modal3.indexOf('清零') >= 0, modal3);
await page.click('.modal-card [data-act=ok]');
await page.waitForTimeout(1200);
const p1Row = await page.$eval('#printer-row-p1', (tr) => tr.innerText.replace(/\s+/g, ' ').trim());
chk('切过去之后 1020 变成累计中（寿命清零、开始累计）',
  p1Row.indexOf('累计中') >= 0 && p1Row.indexOf('100,000') < 0, p1Row);
chk('切口径后云端配置也跟着更新（不是只在本地内存里）',
  (() => { const w = writes.length ? JSON.parse(writes[writes.length - 1]) : null;
           const q = w && w.data && w.data.config && w.data.config.printers.filter((x) => x.id === 'p1')[0];
           return !!q && q.life_mode === 'accumulate' && q.life_pages === 0; })());
chk('全程没有新的 JS 异常', errors.length === 0, errors.slice(0, 3).join(' | '));
await page.screenshot({ path: path.join(OUT_DIR, '08-切换口径后-两台都累计.png') });

await browser.close();
server.close();
console.log('\n' + '='.repeat(50));
console.log(`已跑断言 ${total} 项，失败 ${bad} 项`);
process.exit(bad ? 1 : 0);
