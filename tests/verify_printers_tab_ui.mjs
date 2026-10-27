/* 验收 · 收支清算「🖨️ 打印机折旧」标签页（v7.6，2026-10-25 用户需求）
 *
 * 需求（用户原话）：
 *   ① 「打印机折旧」另开一个标签页专门放 —— 云端数据页只展示云端收益/订单
 *      （订单明细那张表已经挤到不能再加「打印机」一列）；
 *   ② 「打印机台账」卡片从「🔒 授权」也搬到这页；
 *   ③ 「订单 → 打印机」的指定列照订单明细的 🔒 做法：默认锁定、输密码才可改，密码在设置里建；
 *   ④ 该列不再用彩色底纹，成员名用普通文字；
 *   ⑤ 「所有者」+「计费方式」合成一个下拉（两列在账上不冗余，但交叉起来反直觉）。
 *
 * 为什么必须真浏览器：标签页、锁定态只读、解锁后出现下拉、改价格后每页成本立即重算 ——
 * 全是渲染结果，只有真布局能量（静态 grep 只能证明"代码里写了"）。
 *
 * 做法：起本地 HTTP 服务（静态文件来自 local_print_tool/finance/，/api/** 全部打桩，
 * 绝不发外网请求），Playwright 打开 settlement.html → 点「🖨️ 打印机折旧」→ 逐场景断言。
 * 产物：截图存 截图展示/打印机折旧页/。
 *
 * 用法：node tests/verify_printers_tab_ui.mjs
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
const OUT_DIR = path.join(HERE, '..', '截图展示', '打印机折旧页');
fs.mkdirSync(OUT_DIR, { recursive: true });

let bad = 0, total = 0;
function chk(name, cond, detail) {
  total++; if (!cond) bad++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (!cond && detail !== undefined ? `  -- ${detail}` : ''));
}

const ORDERS = [
  { order_id: 1, order_number: 'HN20261025-0007', created_at: '2026-10-25 14:55:17', source: 'local',
    openid: 'local', owner_name: '姚懿祥', status: 'sent', is_admin_print: 0, total_price: 1.7,
    received_client: 'DESKTOP-A-1', received_label: '姚懿祥的设备 | DESKTOP-A-1',
    files_count: 3, total_page_count: 7, files: [
      { id: 'f1', status: 'sent', page_count: 7, copies: 1, duplex: 'off', page_range: '', total_price: 1.7, is_free: false } ] },
  { order_id: 2, order_number: 'HN20261025-0006', created_at: '2026-10-25 14:50:18', source: 'local',
    openid: 'local', owner_name: '霍楠', status: 'sent', is_admin_print: 0, total_price: 0.8,
    received_client: 'R7000-1', received_label: '霍楠的设备 | R7000-1',
    files_count: 1, total_page_count: 4, files: [
      { id: 'f2', status: 'sent', page_count: 4, copies: 1, duplex: 'off', page_range: '', total_price: 0.8, is_free: false } ] },
  /* 2026-10-25 用户反馈的那一行：接单设备为空 ⇒ 表格里既认不出机型、也该给人工指定的下拉 */
  { order_id: 3, order_number: 'IN20261025-0004', created_at: '2026-10-25 10:00:00', source: 'cloud',
    openid: 'local', owner_name: '霍楠', status: 'sent', is_admin_print: 0, total_price: 0.6,
    received_client: '', received_label: '',
    files_count: 1, total_page_count: 2, files: [
      { id: 'f3', status: 'sent', page_count: 2, copies: 1, duplex: 'off', page_range: '', total_price: 0.6, is_free: false } ] },
];
const STORE = {
  config: { investRate: 0.05, scoreCap: 15, passwords: {},
            members: [{ id: 'ma', name: '霍楠', color: '#4472C4' }, { id: 'mb', name: '姚懿祥', color: '#548235' }],
            orderPrinterMap: {},
            printers: [
              { id: 'p1', name: 'HP LaserJet 1020', model: 'HP LaserJet 1020', price: 1200, life_pages: 3000,
                client_id: 'R7000-1', status: 'active', owner: 'ma', billing: 'team', note: '霍楠的设备' },
              { id: 'p2', name: 'HP LaserJet MFP M232-M237', model: 'HP LaserJet MFP M232-M237', price: 1500,
                life_pages: 5000, client_id: '', status: 'active', owner: 'shared', billing: 'wear' } ] },
  records: { expenses: [], income: [], scores: [], transfers: [] },
  notes: {}, meta: { version: 1 },
};
const DEVICES = [
  { client_id: 'R7000-1', device_name: 'R7000-AHP10', owner_name: '霍楠', online: true, is_active: true, auto_duplex: false },
  { client_id: 'DESKTOP-A-1', device_name: 'DESKTOP-EJGEB1V', owner_name: '姚懿祥', online: false, is_active: true, auto_duplex: true },
];
const json = (res, obj) => { res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(obj)); };
const writes = [];

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
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => {
      let parsed = {}; try { parsed = JSON.parse(body || '{}'); } catch (e) {}
      const mod = String(parsed.module || ''); const pwd = String(parsed.password || '');
      if (pwd !== 'FinPass2026') return json(res, { success: false, code: 'wrong_password', message: '密码错误' }, 403);
      return json(res, { success: true, session: 'stub-sess-' + mod, expires_in: 1800, modules: [mod], upgraded: false });
    });
    return;
  }
  if (p === '/api/local/finance_auth/logout') return json(res, { success: true, revoked: true });
  if (p === '/api/proxy/finance/config') {
    if (req.method === 'POST') {
      let body = ''; req.on('data', (c) => { body += c; });
      req.on('end', () => { writes.push(body); json(res, { success: true, server_version: 2 }); });
      return;
    }
    return json(res, { success: true, data: STORE });
  }
  if (p === '/api/proxy/pricing') return json(res, { success: true, data: { simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1, delivery_percentages: {}, urgency_prices: {} } });
  if (p === '/api/proxy/admin/statistics/months') return json(res, { success: true, data: { months: [{ month: '2026-10' }] } });
  if (p === '/api/proxy/admin/statistics/revenue') return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: ORDERS } });
  if (p === '/api/proxy/printer/devices') return json(res, { success: true, devices: DEVICES });
  if (p === '/api/local/device') return json(res, { success: true, device: { client_id: 'R7000-1', device_name: 'R7000-AHP10', take_orders: true } });
  if (p === '/api/local/data') return json(res, { success: true, data: null });
  if (p === '/api/local/bindings') return json(res, { success: true, data: {} });
  return json(res, { success: true, data: null });
});

const port = await new Promise((r) => server.listen(0, '127.0.0.1', () => r(server.address().port)));
/* 随机端口可能落在 Chrome 的 unsafe-port 黑名单里（实测抽到 5060/sip ⇒ ERR_UNSAFE_PORT），
   显式放行本次用的这一个端口，免得整脚本随机挂掉。 */
const browser = await chromium.launch({ args: [`--explicitly-allowed-ports=${port}`] });
/* 用户的真实环境是深色主题 + Windows 150% 缩放（实测截图约为 1.39×），照它渲染，
   截图才和现场看到的一致（几何断言用 CSS px，与缩放无关）。 */
const page = await browser.newPage({ viewport: { width: 1600, height: 1100 }, colorScheme: 'dark', deviceScaleFactor: 1.4 });
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

await page.goto(`http://127.0.0.1:${port}/settlement.html`, { waitUntil: 'load' });
await page.waitForTimeout(700);

chk('页面没有 JS 异常', errors.length === 0, errors.slice(0, 3).join(' | '));
const tabBtns = await page.$$eval('#tabNav button', (els) => els.map((e) => e.dataset.tab));
chk('导航里出现「打印机折旧」标签（在云端数据之后）',
  tabBtns.indexOf('printers') > 0 && tabBtns.indexOf('printers') === tabBtns.indexOf('cloud') + 1, tabBtns.join(','));

/* 切到新标签页 */
await page.click('#tabNav button[data-tab="printers"]');
await page.waitForTimeout(900);
const html = await page.$eval('#tab-printers', (el) => el.innerHTML);
/* 订单表里某一行（订单号）的 DOM 摘要 —— 用户反馈的那一行要靠它逐项核对 */
const orderRow = (num) => page.evaluate((n) => {
  const tbl = [...document.querySelectorAll('#tab-printers table')].find((t) => t.textContent.indexOf('订单号') >= 0);
  if (!tbl) return null;
  const row = [...tbl.querySelectorAll('tbody tr')].find((r) => r.textContent.indexOf(n) >= 0);
  if (!row) return null;
  const sel = row.querySelector('select');
  return { hasSelect: !!sel, options: sel ? [...sel.options].map((o) => o.text) : [],
           value: sel ? sel.value : '', text: row.innerText.replace(/\s+/g, ' ').trim(),
           dep: row.cells[row.cells.length - 1].textContent.trim(),
           cell: row.cells[4].innerHTML };
}, num);
chk('新页有口径栏（时间/成员/来源）', html.indexOf('scope-bar') >= 0 && html.indexOf('时间范围') >= 0);
chk('新页有打印机台账卡片', html.indexOf('打印机台账') >= 0);
chk('新页有本口径用量与折旧 + 订单→打印机', html.indexOf('本口径用量与折旧') >= 0 && html.indexOf('订单 → 打印机') >= 0);
chk('默认锁定：台账没有输入框/添加按钮，只有只读文本 + 解锁入口',
  html.indexOf('printer-name-') < 0 && html.indexOf('addPrinter()') < 0 && html.indexOf('unlockFlow(') >= 0);
chk('两处表头都有 🔒（台账 + 打印机列）', (html.match(/st-lock/g) || []).length >= 2);
chk('订单→打印机：成员列是普通文字（该表内无 member-tag 底纹）', (await page.evaluate(() => {
  const tbl = [...document.querySelectorAll('#tab-printers table')].find((t) => t.textContent.indexOf('订单号') >= 0);
  return tbl ? tbl.querySelectorAll('.member-tag').length : -1;
})) === 0, 'scope-bar 里的成员多选确实带 member-tag，这里只看订单表');
chk('锁定态下订单行没有下拉（防误点）', (await page.$eval('#tab-printers', (el) => el.querySelectorAll('table select').length)) === 0);
/* 未识别行（用户反馈的那一行）锁定态：只有文字，说明放 title —— 但**不谎称**这里有控件 */
const umLocked = await orderRow('IN20261025-0004');
chk('未识别订单（锁定态）：显示「未识别」+ 解锁说明在 title 里，且没有下拉',
  umLocked && umLocked.text.indexOf('未识别') >= 0
  && umLocked.cell.indexOf('后可在这一格下拉指定') >= 0
  && !umLocked.hasSelect, JSON.stringify(umLocked));
const cloudHtml = await page.$eval('#tab-cloud', (el) => el.innerHTML);
chk('云端数据页不再有打印机区块（台账/用量/订单→打印机都搬走了）',
  cloudHtml.indexOf('打印机台账') < 0 && cloudHtml.indexOf('订单 → 打印机') < 0 && cloudHtml.indexOf('本口径寿命消耗') < 0);
chk('云端数据页其余内容没被连带打坏（KPI + 订单明细 + 图表 canvas 都还在）',
  cloudHtml.indexOf('订单明细') >= 0
  && (await page.$eval('#tab-cloud', (el) => el.querySelectorAll('canvas').length)) >= 1);
await page.screenshot({ path: path.join(OUT_DIR, '01-打印机折旧页-锁定.png'), fullPage: false });

/* 解锁（直接翻状态位，避免走密码弹窗）→ 应出现可编辑控件 */
await page.evaluate(() => { unlocked.orderprinter = true; renderPrinters(); });
await page.waitForTimeout(500);
const html2 = await page.$eval('#tab-printers', (el) => el.innerHTML);
chk('解锁后台账可编辑（价格/寿命输入框 + 归属与计费下拉）',
  html2.indexOf('printer-name-') >= 0 && html2.indexOf('updPrinterBilling(') >= 0);
chk('解锁后订单行出现「打印机」下拉（人工指定归属）',
  (await page.$eval('#tab-printers', (el) => el.querySelectorAll('select').length)) > 2);
chk('归属/计费只剩一个下拉（不再有独立的 owner / billing 两列）',
  html2.indexOf("'owner'") < 0 && html2.indexOf("'billing'") < 0);

/* ── 2026-10-25 用户反馈：认不出机型的行只写「可在右侧下拉指定」却没有下拉 ──
   解锁后这一行的「打印机」格里必须有真 select，且能真的把人指定的机器写进账本。 */
const umOpen = await orderRow('IN20261025-0004');
chk('未识别订单（解锁态）：单元格里**真的**有下拉，两台在役机都可选',
  umOpen && umOpen.hasSelect && umOpen.options.length >= 3
  && umOpen.options.indexOf('HP LaserJet 1020') >= 0
  && umOpen.options.indexOf('HP LaserJet MFP M232-M237') >= 0
  && umOpen.value === '', JSON.stringify(umOpen));
chk('「未识别」写进下拉自己的占位项里（不再挂在下拉旁边把控件挤歪）',
  umOpen && umOpen.text.indexOf('未识别 · 请指定') >= 0
  && umOpen.cell.trim().slice(-9) === '</select>', JSON.stringify({ text: umOpen.text, cell: umOpen.cell }));
/* 用户第三句反馈「我这里是歪着的」：td 居中 + 旁边多挂一个小标签 ⇒ 这一行的下拉被顶偏。
   现在每行格子里的内容都只有一个 select，量真实几何证明左边缘/宽度完全一致。 */
const selAlign = await page.evaluate(() => {
  const tbl = [...document.querySelectorAll('#tab-printers table')].find((t) => t.textContent.indexOf('订单号') >= 0);
  const rects = [...tbl.querySelectorAll('tbody tr select')].map((s) => s.getBoundingClientRect());
  const uniq = (a) => [...new Set(a)];
  return { n: rects.length, lefts: uniq(rects.map((r) => Math.round(r.left))),
           widths: uniq(rects.map((r) => Math.round(r.width))) };
});
chk('所有行的「打印机」下拉左边缘与宽度完全一致（整列竖直对齐，不再歪）',
  selAlign.n >= 3 && selAlign.lefts.length === 1 && selAlign.widths.length === 1, JSON.stringify(selAlign));
chk('全页不再出现「可在右侧下拉指定」这种指向空处的文案',
  html2.indexOf('可在右侧下拉指定') < 0);
/* 用户第二句反馈：这一格不许另起一行把行高撑高 —— 直接量真实行高，与普通行比 */
const rowHeights = await page.evaluate(() => {
  const tbl = [...document.querySelectorAll('#tab-printers table')].find((t) => t.textContent.indexOf('订单号') >= 0);
  const rows = [...tbl.querySelectorAll('tbody tr')];
  const h = (n) => { const r = rows.find((x) => x.textContent.indexOf(n) >= 0); return r ? r.getBoundingClientRect().height : null; };
  return { unmatch: h('IN20261025-0004'), a: h('HN20261025-0007'), b: h('HN20261025-0006'),
           cellDivs: (() => {
             const r = rows.find((x) => x.textContent.indexOf('IN20261025-0004') >= 0);
             return r ? r.querySelectorAll('td div').length : -1;
           })() };
});
chk('未识别行与普通行同高（提示只在同一行内，不撑高行高）',
  rowHeights.unmatch !== null && rowHeights.a !== null && rowHeights.b !== null
  && Math.abs(rowHeights.unmatch - rowHeights.a) <= 1.5
  && Math.abs(rowHeights.unmatch - rowHeights.b) <= 1.5
  && rowHeights.cellDivs === 0, JSON.stringify(rowHeights));
await page.screenshot({ path: path.join(OUT_DIR, '03-未识别订单-可下拉指定.png'), fullPage: false });
/* 再把那一行滚进视野拍一张（上面那张是整页顶部，未识别行常在折线以下） */
await page.evaluate(() => {
  const tbl = [...document.querySelectorAll('#tab-printers table')].find((t) => t.textContent.indexOf('订单号') >= 0);
  const row = [...tbl.querySelectorAll('tbody tr')].find((r) => r.textContent.indexOf('IN20261025-0004') >= 0);
  row.scrollIntoView({ block: 'center' });
});
await page.waitForTimeout(300);
await page.screenshot({ path: path.join(OUT_DIR, '03b-未识别订单行-放大.png'), fullPage: false });

/* 真的选一台 → 归属立即生效（这是那一行存在的意义：店员能就地补指定） */
await page.evaluate(() => {
  const tbl = [...document.querySelectorAll('#tab-printers table')].find((t) => t.textContent.indexOf('订单号') >= 0);
  const row = [...tbl.querySelectorAll('tbody tr')].find((r) => r.textContent.indexOf('IN20261025-0004') >= 0);
  const sel = row.querySelector('select');
  sel.value = 'p1';
  sel.dispatchEvent(new Event('change'));
});
await page.waitForTimeout(600);
const umAfter = await orderRow('IN20261025-0004');
chk('下拉指定 1020 后立即生效：不再「未识别」、下拉选中它、2 页折旧 −¥0.80',
  umAfter && umAfter.hasSelect && umAfter.value === 'p1' && umAfter.text.indexOf('未识别') < 0
  && umAfter.dep.indexOf('0.80') >= 0, JSON.stringify(umAfter));
chk('指定写进了 STORE.config.orderPrinterMap（随 finance 配置同步）',
  (await page.evaluate(() => (STORE.config.orderPrinterMap || {})['3'])) === 'p1',
  await page.evaluate(() => JSON.stringify(STORE.config.orderPrinterMap)));
await page.screenshot({ path: path.join(OUT_DIR, '02-打印机折旧页-解锁.png'), fullPage: false });

/* 改一格价格 → 真的落库（POST finance/config） */
await page.evaluate(() => {
  const inp = document.querySelector('#tab-printers input[type=number]');
  inp.value = '1350';
  inp.dispatchEvent(new Event('change'));
});
await page.waitForTimeout(500);
chk('改价格落到 STORE（updPrinter 真的写进打印机台账）',
  (await page.evaluate(() => STORE.config.printers[0].price)) === 1350,
  await page.evaluate(() => JSON.stringify(STORE.config.printers[0])));
chk('每页成本随价格立即重算（1350/3000 = ¥0.4500/页，不必整页重绘）',
  (await page.$eval('#tab-printers', (el) => el.innerText)).indexOf('0.4500') >= 0);

/* 切走再切回：标签页选中态与内容都还在 */
await page.click('#tabNav button[data-tab="cloud"]');
await page.waitForTimeout(400);
const cloudVisibleAfter = await page.$eval('#tab-cloud', (el) => el.classList.contains('on'));
await page.click('#tabNav button[data-tab="printers"]');
await page.waitForTimeout(500);
const printersVisible = await page.$eval('#tab-printers', (el) => el.classList.contains('on'));
chk('标签切换正常（云端数据 → 打印机折旧）', cloudVisibleAfter && printersVisible);
chk('切换往返后没有新的 JS 异常', errors.length === 0, errors.slice(0, 3).join(' | '));

await browser.close();
server.close();
console.log('\n' + '='.repeat(50));
console.log(`已跑断言 ${total} 项，失败 ${bad} 项`);
process.exit(bad ? 1 : 0);
