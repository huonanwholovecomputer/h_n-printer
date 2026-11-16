/* 验收（真实渲染）· 本地（离线）模式「📡 本地订单」明细与完成率
 *
 * 背景（2026-11-15）：本地模式复用云端驾驶舱渲染，数据源是 /api/local/orders（本地 SQLite）。
 * 两个只在本地下出现的口径缺口：
 *   ① 明细表「文件」列直接读 o.files_count，而 stats_server._handle_local_orders 的 payload
 *      没有这个键（云端 revenue 一直有）⇒ 离线模式整列显示 undefined。v7.24 后端补键 +
 *      页面按 files 长度兜底（老后端也不至于露 undefined）。
 *   ② 完成率 = done / (总数 - 取消)，_statusGroups 的 pending 漏了 unsynced ⇒
 *      「⏳ 离线未同步」既不算完成也不算进行中，环形图有一块但中心写 100%。v7.24 计入待处理。
 *
 * 判据（真实渲染，/api/** 打桩；桩里**故意不带 files_count**，验的就是页面兜底）：
 *   ① local：明细 4 行，文件列全部是 0~9 的数字且等于该单 files 长度（无 undefined/NaN）；
 *   ② local：页面正文不含 undefined / NaN；
 *   ③ local：完成率（环形图中心）= round(done / (总数 - 取消) * 100) = 2/(4-1) = 67%；
 *   ④ local：未同步单显示「⏳ 离线未同步」且不计收益（KPI 收益 = 两张 sent 的合计）；
 *   ⑤ cloud：回归 —— 后端带 files_count 时明细照常显示，完成率同一公式。
 *
 * 用法：node other/tests/verify_local_orders_table.mjs   exit 0 = 通过
 */
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require_ = createRequire(import.meta.url);
const { chromium } = require_('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, '..', '..');
const FINANCE = path.join(ROOT, 'local_print_tool', 'finance');
const HTML_PATH = path.join(FINANCE, 'settlement.html');

let bad = 0, total = 0;
function chk(name, cond, detail) {
  total++;
  if (!cond) bad++;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (!cond && detail !== undefined ? '  -- ' + detail : ''));
}
function note(m) { console.log('      · ' + m); }

const MODULES = ['score', 'expense', 'income', 'transfer', 'settings', 'auth', 'orderstatus', 'orderprinter'];
const MODE = { value: 'local' };

const STORE = {
  config: {
    investRate: 0.05, scoreCap: 15, passwords: {}, memberBindings: {},
    members: [{ id: 'ma', name: '张三', color: '#4472C4' }],
    printers: [{ id: 'p1', name: 'HP LaserJet 1020', model: 'HP LaserJet 1020', client_id: 'c1020',
                 price: 800, life_pages: 12000, life_mode: 'estimated', status: 'active',
                 billing: 'team', owner: 'shared', bought: '2026-09-01' }],
  },
  records: { expenses: [], income: [], scores: [], transfers: [] },
  notes: {}, meta: { version: 1 },
};

function mkFile(name, pages, duplex, copies, price) {
  const eff = pages, sheets = duplex === 'on' ? Math.ceil(eff / 2) : eff;
  return { id: 'f1', file_name: name, copies, page_count: pages, duplex, page_range: '',
           total_price: price, status: 'sent', is_free: false,
           effective_pages: eff, sheets, total_pages: eff * copies, total_sheets: sheets * copies };
}
function mkOrder(id, num, when, files, status) {
  let tp = 0, ts = 0;
  /* 与 stats_server._handle_local_orders 同口径：文件级状态 = 订单级状态（未同步/取消的
     文件也不许按「已完成」计收益，否则页面会把这笔钱算进 KPI） */
  files.forEach((f) => { f.status = status; tp += f.total_pages; ts += f.total_sheets; });
  return { order_id: id, order_number: num, created_at: when, source: 'local', openid: 'local',
           nickname: '张三', owner_name: '张三', status, unsynced: status !== 'sent',
           sync_status: status === 'sent' ? 'synced' : 'pending', is_admin_print: 0,
           total_page_count: tp, total_sheet_count: ts, total_price: files.reduce((a, f) => a + f.total_price, 0),
           files };   /* 故意不带 files_count：验页面兜底 */
}
const ORDERS = [
  mkOrder(4, 'HN-0004', '2026-11-12 09:00:00', [mkFile('a.pdf', 10, 'on', 1, 2.4), mkFile('b.pdf', 4, 'on', 1, 1.2)], 'sent'),
  mkOrder(3, 'HN-0003', '2026-11-11 16:00:00', [mkFile('c.docx', 8, 'on', 1, 2.0)], 'sent'),
  mkOrder(2, 'HN-0002', '2026-11-10 11:00:00', [mkFile('d.pptx', 6, 'off', 1, 1.8)], 'unsynced'),
  mkOrder(1, 'HN-0001', '2026-11-09 08:00:00', [mkFile('e.pptx', 5, 'off', 1, 1.5)], 'canceled'),
];
const EXPECT = {
  revenueCents: 240 + 120 + 200,          // 两张 sent 的合计（¥5.60）
  done: 2, total: 4, canceled: 1,
  rate: Math.round(2 / (4 - 1) * 100),    // 67
  filesByOrder: { 'HN-0004': 2, 'HN-0003': 1, 'HN-0002': 1, 'HN-0001': 1 },
};

const json = (res, o) => { res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(o)); };
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  const p = decodeURIComponent(u.pathname);
  if (p.startsWith('/api/')) {
    req.resume();
    if (p === '/api/health') return json(res, { success: true, cloud: MODE.value === 'cloud' });
    if (p === '/api/proxy/finance/config') return json(res, { success: true, data: STORE, server_version: 2 });
    if (p === '/api/local/data') return json(res, { success: true, data: STORE });
    if (p === '/api/local/finance_auth/status') return json(res, { success: true, modules: {}, needs_setup: [], session: { active: true, token: 'probe', modules: MODULES, expires_in: 1800 }, session_ttl: 1800, read_only: false, integrity: 'ok' });
    if (p === '/api/local/device') return json(res, { success: true, device: { client_id: 'c1020', device_name: '验收机-A', take_orders: true } });
    if (p === '/api/local/bindings') return json(res, { success: true, data: {} });
    if (p === '/api/local/orders') return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: ORDERS } });
    if (p === '/api/local/external_prints') return json(res, { success: true, orders: [], count: 0, skipped: 0, simplex_price: 0.3, duplex_price: 0.4, owner_name: '' });
    if (p === '/api/proxy/pricing') return json(res, { success: true, data: { simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1, delivery_percentages: {}, urgency_prices: {} } });
    if (p === '/api/proxy/printer/devices') return json(res, { success: true, devices: [] });
    if (p === '/api/proxy/admin/statistics/months') return json(res, { success: true, data: { months: [{ month: '2026-11' }] } });
    /* cloud 侧：后端会带 files_count，验「页面兜底没有把正常字段覆盖坏」 */
    if (p === '/api/proxy/admin/statistics/revenue') {
      const withCount = ORDERS.map((o) => Object.assign({}, o, { files_count: (o.files || []).length }));
      return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: withCount } });
    }
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
const BASE = 'http://127.0.0.1:' + port;
const browser = await chromium.launch();

async function openMode(mode) {
  MODE.value = mode;
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 }, deviceScaleFactor: 1 });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e.message)));
  await page.goto(BASE + '/settlement.html?mode=' + mode, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1200);
  await page.evaluate((mods) => {
    try {
      window._setFinanceSession('probe', 1800, mods);
      if (window.unlocked) mods.forEach((k) => { window.unlocked[k] = true; });
    } catch (e) {}
  }, MODULES);
  await page.waitForTimeout(600);
  await page.click('#tabNav button[data-tab="cloud"]').catch(() => {});
  /* 明细表是两段式（本地单 + 外部监视单）渲染的，等它落定 */
  await page.waitForFunction(() => !!document.querySelector('#tab-cloud table.order-table tbody tr.order-row'), { timeout: 8000 }).catch(() => {});
  await page.waitForTimeout(1500);
  const snap = await page.evaluate(() => {
    var el = document.getElementById('tab-cloud') || document.body;
    var rows = [...el.querySelectorAll('table.order-table tbody tr.order-row')].map(function (tr) {
      var tds = [...tr.querySelectorAll('td')];
      return {
        num: (tds[1] ? tds[1].textContent.trim() : ''),
        status: (tds[4] ? tds[4].textContent.trim() : ''),
        money: (tds[5] ? tds[5].textContent.trim() : ''),
        files: (tds[6] ? tds[6].textContent.trim() : ''),
      };
    });
    var center = el.querySelector('.donut-center .dc-num');
    var kpi = [...el.querySelectorAll('.hero-card')].map(function (c) { return c.innerText.replace(/\s+/g, ' ').trim(); });
    return {
      rows: rows,
      rate: center ? center.textContent.trim() : '(none)',
      text: (el.innerText || ''),
      kpi: kpi,
    };
  });
  await page.screenshot({ path: 'C:/Users/Administrator/.dsh/tmp_modediff/orders_' + mode + '_v724.png', fullPage: true });
  await page.close();
  return { snap, errors };
}

console.log('\n=== ① 本地（离线）模式：明细「文件」列 ===');
const L = await openMode('local');
chk('①-1 明细 4 行', L.snap.rows.length === EXPECT.total, 'rows=' + L.snap.rows.length + ' ' + JSON.stringify(L.snap.rows.map((r) => r.num)));
const nums = L.snap.rows.map((r) => r.num);
const fileCells = L.snap.rows.map((r) => r.files);
chk('①-2 文件列全是数字（无 undefined/NaN）', fileCells.every((v) => /^[0-9]+$/.test(v)), JSON.stringify(fileCells));
Object.keys(EXPECT.filesByOrder).forEach((num, i) => {
  const row = L.snap.rows.find((r) => r.num === num);
  chk('①-3[' + (i + 1) + '] ' + num + ' 文件列 = ' + EXPECT.filesByOrder[num] + '（= files 长度）',
      !!row && row.files === String(EXPECT.filesByOrder[num]), row ? row.files : '(no row)');
});
chk('①-4 正文不含 undefined', L.snap.text.indexOf('undefined') < 0, L.snap.text.slice(0, 200));
chk('①-5 正文不含 NaN', L.snap.text.indexOf('NaN') < 0, L.snap.text.slice(0, 200));

console.log('\n=== ② 完成率与未同步口径 ===');
chk('②-1 完成率 = ' + EXPECT.rate + '%（done/(总数-取消)）', L.snap.rate === EXPECT.rate + '%', L.snap.rate);
const unsyncedRow = L.snap.rows.find((r) => r.num === 'HN-0002');
chk('②-2 未同步单显示「⏳ 离线未同步」', !!unsyncedRow && unsyncedRow.status.indexOf('离线未同步') >= 0, unsyncedRow ? unsyncedRow.status : '(no row)');
chk('②-3 未同步单不计收益（金额列带 ¥1.80 但 KPI 只算 2 张 sent）',
    !!unsyncedRow && unsyncedRow.money.indexOf('1.80') >= 0, unsyncedRow ? unsyncedRow.money : '(no row)');
chk('②-4 KPI 收益 = ¥' + (EXPECT.revenueCents / 100).toFixed(2),
    L.snap.kpi.some((t) => t.indexOf('5.60') >= 0), JSON.stringify(L.snap.kpi.slice(0, 6)));
chk('②-5 无 JS 运行期异常', L.errors.length === 0, JSON.stringify(L.errors.slice(0, 3)));

console.log('\n=== ③ 云端模式回归（后端带 files_count，兜底不得覆盖） ===');
const C = await openMode('cloud');
chk('③-1 明细 4 行', C.snap.rows.length === EXPECT.total, 'rows=' + C.snap.rows.length);
chk('③-2 文件列仍是数字且与 files 长度一致',
    C.snap.rows.every((r) => /^[0-9]+$/.test(r.files) && r.files === String(EXPECT.filesByOrder[r.num])),
    JSON.stringify(C.snap.rows.map((r) => r.num + ':' + r.files)));
chk('③-3 完成率同一公式 ' + EXPECT.rate + '%', C.snap.rate === EXPECT.rate + '%', C.snap.rate);
chk('③-4 无 JS 运行期异常', C.errors.length === 0, JSON.stringify(C.errors.slice(0, 3)));

note('截图：C:/Users/Administrator/.dsh/tmp_modediff/orders_{local,cloud}_v724.png');
await browser.close();
server.close();
console.log('\n' + (bad === 0 ? 'ALL PASS' : 'FAILED ' + bad + ' / ' + total));
process.exit(bad === 0 ? 0 : 1);
