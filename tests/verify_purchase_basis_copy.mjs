/* 验收 · 「采购分摊」口径与页面文案同步（2026-10-27 用户要求）
 *
 * 需求原话：「这个文案需要与设置中选择的模式同步，比如我现在选择了按用量（各人本月实际出纸页数比例）」
 * —— 用户截图框住的是「📊 采购支出（谁垫付了什么，AA 均摊）」这行小标题：
 *    设置里选了「按用量」，页面却还写死「AA 均摊」，而这一步正好决定谁出多少钱。
 *
 * 需要钉住的三件事：
 *   ① 按用量口径：「手动记账 → 采购支出」小标题写「按各人本月实际出纸页数分摊」，不再出现「AA 均摊」；
 *   ② 均摊口径：文案原样保留（零回归，老用户界面不变）；
 *   ③ 「收支清算 → 垫付清算」第三列表头同样跟着口径走（按用量时不能把按用量算出来的数叫「应均摊」）。
 *
 * 为什么必须真浏览器：文案由 renderEntry()/renderDash() 现算，静态 grep 只能证明函数存在，
 * 证明不了「设置里选了按用量、页面上真的改了」。这里两种口径各渲染一次对拍。
 * 产物：截图存 截图展示/采购分摊文案/。
 * 用法：node tests/verify_purchase_basis_copy.mjs
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
const OUT_DIR = path.join(HERE, '..', '截图展示', '采购分摊文案');
fs.mkdirSync(OUT_DIR, { recursive: true });

let bad = 0, total = 0;
function chk(name, cond, detail) {
  total++; if (!cond) bad++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (!cond && detail !== undefined ? `  -- ${detail}` : ''));
}

/* 打桩：两位成员 + 一笔垫付采购（采购人 ma），口径初始 = usage（对应用户截图里的选择） */
const STORE = {
  config: { investRate: 0.05, scoreCap: 15, passwords: {}, purchaseBasis: 'usage',
            members: [{ id: 'ma', name: '霍楠', color: '#4472C4' }, { id: 'mb', name: '姚懿祥', color: '#548235' }],
            orderPrinterMap: {}, printers: [] },
  records: { expenses: [{ id: 'e1', date: '2026-10-05', item: 'A4纸采购', amount: 99.67, payer: 'ma', kind: 'other' }],
             income: [], scores: [], transfers: [] },
  notes: {}, meta: { version: 2 },
};
const json = (res, obj) => { res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(obj)); };

const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  const p = decodeURIComponent(u.pathname);
  if (!p.startsWith('/api/')) {
    const file = path.join(FINANCE, p === '/' ? 'settlement.html' : p.replace(/^\//, ''));
    if (!fs.existsSync(file)) { res.writeHead(404); return res.end('not found'); }
    const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8' };
    res.writeHead(200, { 'Content-Type': types[path.extname(file)] || 'application/octet-stream' });
    return res.end(fs.readFileSync(file));
  }
  if (p === '/api/health') return json(res, { success: true, cloud: true });
  if (p === '/api/local/finance_auth/status') {
    return json(res, { success: true, modules: {}, needs_setup: [],
                       session: { active: false, modules: [], expires_in: 0 }, session_ttl: 1800,
                       read_only: false, integrity: 'ok' });
  }
  if (p === '/api/local/finance_auth/unlock') return json(res, { success: true, session: 'stub-sess', expires_in: 1800, modules: [], upgraded: false });
  if (p === '/api/local/finance_auth/logout') return json(res, { success: true, revoked: true });
  if (p === '/api/proxy/finance/config') return json(res, { success: true, data: STORE });
  if (p === '/api/proxy/pricing') return json(res, { success: true, data: { simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1, delivery_percentages: {}, urgency_prices: {} } });
  if (p === '/api/proxy/admin/statistics/months') return json(res, { success: true, data: { months: [{ month: '2026-10' }] } });
  if (p === '/api/proxy/admin/statistics/revenue') return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: [] } });
  if (p === '/api/proxy/printer/devices') return json(res, { success: true, devices: [] });
  if (p === '/api/local/device') return json(res, { success: true, device: { client_id: 'R7000-1', device_name: 'R7000-AHP10', take_orders: true } });
  return json(res, { success: true, data: null });
});

const port = await new Promise((r) => server.listen(0, '127.0.0.1', () => r(server.address().port)));
const browser = await chromium.launch({ args: [`--explicitly-allowed-ports=${port}`] });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, colorScheme: 'dark', deviceScaleFactor: 1.4 });
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

await page.goto(`http://127.0.0.1:${port}/settlement.html`, { waitUntil: 'load' });
await page.waitForTimeout(900);
chk('页面没有 JS 异常（打桩数据下）', errors.length === 0, errors.slice(0, 3).join(' | '));
chk('打桩生效：配置里的采购口径 = usage', (await page.evaluate(() => getPurchaseBasis())) === 'usage',
  await page.evaluate(() => getPurchaseBasis()));

/* ── ① 按用量：手动记账 → 采购支出小标题 ── */
await page.click('#tabNav button[data-tab="entry"]');
await page.waitForTimeout(700);
const entryUsage = await page.$eval('#tab-entry', (el) => el.innerText.replace(/\s+/g, ' ').trim());
chk('按用量：「采购支出」小标题写「按各人本月实际出纸页数分摊」',
  entryUsage.indexOf('按各人本月实际出纸页数分摊') >= 0, entryUsage.slice(0, 120));
chk('按用量：不再出现写死的「AA 均摊」', entryUsage.indexOf('AA 均摊') < 0, entryUsage.slice(0, 120));
await page.screenshot({ path: path.join(OUT_DIR, '01-手动记账-采购支出-按用量.png') });

/* ── ③ 按用量：收支清算 → 垫付清算表头 ── */
await page.click('#tabNav button[data-tab="dash"]');
await page.waitForTimeout(900);
const dashUsage = await page.$eval('#tab-dash', (el) => el.innerHTML);
chk('按用量：「垫付清算」第三列表头写「应摊（按用量）」', dashUsage.indexOf('应摊（按用量）') >= 0,
  dashUsage.indexOf('垫付清算') >= 0 ? '未找到「应摊（按用量）」' : '垫付清算区块没渲染出来');
chk('按用量：表头不再把按用量算出来的数说成「应均摊」', dashUsage.indexOf('− 应均摊') < 0);
await page.screenshot({ path: path.join(OUT_DIR, '02-收支清算-垫付清算-按用量.png') });

/* ── ② 切回均摊口径：两处文案原样回来（零回归） ── */
await page.evaluate(() => { STORE.config.purchaseBasis = 'equal'; currentTab = 'entry'; renderEntry(); });
await page.waitForTimeout(500);
const entryEqual = await page.$eval('#tab-entry', (el) => el.innerText.replace(/\s+/g, ' ').trim());
chk('均摊：「采购支出」小标题回到「AA 均摊」', entryEqual.indexOf('AA 均摊') >= 0, entryEqual.slice(0, 120));
chk('均摊：不再出现按用量的措辞', entryEqual.indexOf('按各人本月实际出纸页数分摊') < 0);
await page.screenshot({ path: path.join(OUT_DIR, '03-手动记账-采购支出-均摊.png') });

await page.evaluate(() => { currentTab = 'dash'; renderDash(); });
await page.waitForTimeout(700);
const dashEqual = await page.$eval('#tab-dash', (el) => el.innerHTML);
chk('均摊：「垫付清算」表头回到「− 应均摊」', dashEqual.indexOf('− 应均摊') >= 0 && dashEqual.indexOf('应摊（按用量）') < 0);
await page.screenshot({ path: path.join(OUT_DIR, '04-收支清算-垫付清算-均摊.png') });

chk('全程没有 JS 异常', errors.length === 0, errors.slice(0, 3).join(' | '));

await browser.close();
server.close();
console.log(`\n${total - bad}/${total} 通过` + (bad ? `，${bad} 项失败` : ''));
process.exit(bad ? 1 : 0);
