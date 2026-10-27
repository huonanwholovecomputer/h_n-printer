/* 验收 · v7.9「长期耗材（A4纸 / 硒鼓）按月摊销」—— 真浏览器渲染 / 交互
 *
 * 需求原话：「A4纸和硒鼓是能够长期使用的，但是现在的按打印比例分配采购，导致了采购月的
 *            分配严重失调……A4纸按每张成本累积、每月给采购者原价返回；硒鼓按本月打印页数 /
 *            总可打印页数做折旧。」
 * 补充：「采购部分需要有常驻的'硒鼓'和'A4纸'类型，选择后，会多出一列，需要填写A4纸的张数
 *        和硒鼓的使用寿命（能打印多少），用于清算。」
 *
 * 为什么必须真浏览器：三件事只有渲染后才能证明 ——
 *   ① 「类型选 A4纸 之后才多出数量列」是**渲染结果**，静态 grep 只能证明代码里写了条件；
 *   ② 数量填完后的「单价 ¥x/张」提示、结算页「采购支出」KPI 从整笔 ¥100 变成本月摊销 ¥20，
 *      都是真实 DOM 数字；
 *   ③ 摊销区块的「本月用量 / 本月摊销 / 剩余未摊销 / 返还给采购人」是否按真实订单用量算出来。
 *
 * 做法：本地 HTTP 服务（静态文件来自 local_print_tool/finance/，/api/** 全部打桩，
 * 绝不发外网请求；revenue 接口按请求月份下发真实订单）→ Playwright 打开 settlement.html：
 *   ① 基线：两条普通采购（门禁卡 ¥100 / 电费 ¥50），没有「数量」列；
 *   ② 把门禁卡类型改成 A4纸 ⇒ 立刻多出「数量」列，该行出现「总张数」输入框；
 *   ③ 填 500 张 ⇒ 单价提示 ¥0.2000/张（金额 ¥100 ÷ 500 张）；
 *   ④ 电费行在数量列是「—」（普通采购不需要填）；
 *   ⑤ 切到收支清算：采购支出 KPI = ¥70（电费 50 + 本月摊销 20），不是整笔 ¥150；
 *   ⑥ 「采购明细」里只有摊销行（¥20.00），没有整笔 ¥100；
 *   ⑦ 摊销区块：总量 500 张 / 单价 ¥0.2000 / 累计 100 张 / 已摊销 ¥20.00 / 本月 100 张 /
 *      本月摊销 ¥20.00 / 剩余 ¥80.00 / 返还给采购人 霍楠；
 *   ⑧ 购买日之前的 40 张不算（订单：月初 40 张在购买日之前、之后 60+40 张）；
 *   ⑨ 按用量分摊的表：霍楠 100 页、姚懿祥 40 页（采购按实际出纸页数分）；
 *   ⑩ 三条恒等式（ΣN ≡ 0、ΣB ≡ ΣC ≡ Π）仍成立；零 JS 异常；配置真的写回云端。
 *   ⑪ **变异自证**：把 calcMonth 的 `if (amort)` 改回 `if (false)`（= 整笔采购照旧进购买月）⇒
 *      ⑤ 必须转红（¥150），其余（① 类型列 / ③ 单价）不受影响。
 *
 * 产物：截图存 截图展示/长期耗材摊销/。
 * 用法：node tests/verify_consumable_amortization_ui.mjs
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
const OUT_DIR = path.join(HERE, '..', '截图展示', '长期耗材摊销');
fs.mkdirSync(OUT_DIR, { recursive: true });

const SRC = fs.readFileSync(HTML_PATH, 'utf8');
const CUR = new Date();
const CUR_MONTH = `${CUR.getFullYear()}-${String(CUR.getMonth() + 1).padStart(2, '0')}`;
const CUR_LABEL = `${CUR.getFullYear()}年${CUR.getMonth() + 1}月`;
const NEXT_MONTH = (() => {
  const d = new Date(CUR.getFullYear(), CUR.getMonth() + 1, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
})();
const NEXT_DAY = `${NEXT_MONTH}-02`;
const PURCHASE_DAY = `${CUR_MONTH}-05`;   // 采购日
const BEFORE_DAY = `${CUR_MONTH}-04`;     // 采购日之前（这 40 张不该进这批摊销）
const AFTER_DAY1 = `${CUR_MONTH}-06`;
const AFTER_DAY2 = `${CUR_MONTH}-07`;

const STORE = {
  config: {
    investRate: 0.05, scoreCap: 15, passwords: {},
    members: [{ id: 'ma', name: '霍楠', color: '#4472C4' }, { id: 'mb', name: '姚懿祥', color: '#548235' }],
    orderPrinterMap: {}, printers: [], purchaseBasis: 'usage',
  },
  records: {
    expenses: [
      { id: 'e1', date: PURCHASE_DAY, item: '门禁卡', amount: 100, payer: 'ma' },  // 会被改成 A4纸
      { id: 'e2', date: PURCHASE_DAY, item: '电费', amount: 50, payer: 'mb' },
    ],
    income: [
      { id: 'i1', date: FIRST_OF_MONTH(), memberId: 'ma', amount: 100 },
      { id: 'i2', date: FIRST_OF_MONTH(), memberId: 'mb', amount: 100 },
      /* 下个月放一笔小额代收，只为让月份下拉能选到下个月（验证「购买月之后」摊销区块） */
      { id: 'i3', date: NEXT_DAY, memberId: 'ma', amount: 10 },
    ],
    transfers: [],
    scores: [
      { id: 's1', date: FIRST_OF_MONTH(), memberId: 'ma', item: '值班', score: 5 },
      { id: 's2', date: FIRST_OF_MONTH(), memberId: 'mb', item: '客服', score: 5 },
    ],
  },
  notes: {}, meta: { version: 2 },
};
function FIRST_OF_MONTH() { return `${CUR_MONTH}-01`; }

/* 用量订单：采购日之前 40 张（不该进这批摊销）+ 之后 60/40 张（该进）。
   页数 = 张数（单面），用于「按用量分摊」的权重（霍楠 100 页 / 姚懿祥 40 页）。 */
const ORDERS = [
  { order_id: 'o1', order_number: 'HN-BEFORE', created_at: `${BEFORE_DAY} 10:00:00`, openid: 'local', owner_name: '霍楠',
    files: [{ status: 'sent', total_pages: 40, total_sheets: 40, effective_pages: 40, page_count: 40, copies: 1, duplex: 'off', total_price: 12.00 }] },
  { order_id: 'o2', order_number: 'HN-A1', created_at: `${AFTER_DAY1} 10:00:00`, openid: 'local', owner_name: '霍楠',
    files: [{ status: 'sent', total_pages: 60, total_sheets: 60, effective_pages: 60, page_count: 60, copies: 1, duplex: 'off', total_price: 18.00 }] },
  { order_id: 'o3', order_number: 'HN-A2', created_at: `${AFTER_DAY2} 10:00:00`, openid: 'local', owner_name: '姚懿祥',
    files: [{ status: 'sent', total_pages: 40, total_sheets: 40, effective_pages: 40, page_count: 40, copies: 1, duplex: 'off', total_price: 12.00 }] },
  /* 下个月再打 50 张：验证「购买月之后」仍按实际用量摊销（50 张 × ¥0.20 = ¥10） */
  { order_id: 'o4', order_number: 'HN-NEXT', created_at: `${NEXT_DAY} 10:00:00`, openid: 'local', owner_name: '姚懿祥',
    files: [{ status: 'sent', total_pages: 50, total_sheets: 50, effective_pages: 50, page_count: 50, copies: 1, duplex: 'off', total_price: 15.00 }] },
];

const json = (res, obj) => { res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(obj)); };

function makeHtml(mutate) {
  if (!mutate) return SRC;
  if (mutate === 'no-amort') {
    /* 变异：整笔耗材采购照旧计入购买月（= 修前行为），其余一切不变。
       注意源码是 CRLF，别用 \n 拼锚点（edit 工具会自动归一，fs 读出来不会）。 */
    const re = /  if \(amort\) \{\r?\n    exps = exps\.filter\(function \(e\) \{ return !isAmortizableExpense\(e\); \}\);/;
    if (!re.test(SRC)) throw new Error('变异失败：没找到 calcMonth 的 amort 分支（源码格式变了？）');
    return SRC.replace(re, (m) => m.replace('if (amort) {', 'if (false) {'));
  }
  throw new Error('未知变异: ' + mutate);
}

let writes = [];
let reqLog = [];
let MUTATE = null;
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
    if (req.method === 'POST') {
      let body = ''; req.on('data', (c) => { body += c; });
      req.on('end', () => { writes.push(body); json(res, { success: true, server_version: 3 }); });
      return;
    }
    return json(res, { success: true, data: STORE });
  }
  if (p === '/api/proxy/pricing') return json(res, { success: true, data: { simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1, delivery_percentages: {}, urgency_prices: {} } });
  if (p === '/api/proxy/admin/statistics/months') return json(res, { success: true, data: { months: [{ month: CUR_MONTH }] } });
  if (p === '/api/proxy/admin/statistics/revenue') {
    const start = u.searchParams.get('start_date') || '';
    const end = u.searchParams.get('end_date') || '';
    const orders = ORDERS.filter((o) => {
      const d = o.created_at.slice(0, 10);
      return d >= start && d <= end;
    });
    reqLog.push({ start, end, n: orders.length });
    return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders } });
  }
  if (p === '/api/proxy/printer/devices') return json(res, { success: true, devices: [] });
  if (p === '/api/local/device') return json(res, { success: true, device: { client_id: 'R7000-1', device_name: 'R7000-AHP10', take_orders: false } });
  if (p === '/api/local/data') return json(res, { success: true, data: null });
  if (p === '/api/local/bindings') return json(res, { success: true, data: {} });
  return json(res, { success: true, data: null });
});

const port = await new Promise((r) => server.listen(0, '127.0.0.1', () => r(server.address().port)));
const browser = await chromium.launch({ args: [`--explicitly-allowed-ports=${port}`] });

async function runScenario(mutate, withScreenshots) {
  MUTATE = mutate;
  writes = [];
  const results = [];
  const chk = (name, cond, detail) => results.push([name, !!cond, detail === undefined ? '' : String(detail)]);

  const ctx = await browser.newContext({ viewport: { width: 1500, height: 1000 }, colorScheme: 'light', deviceScaleFactor: 1.2 });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

  await page.goto(`http://127.0.0.1:${port}/settlement.html`, { waitUntil: 'load' });
  await page.waitForTimeout(1100);
  /* 固定到「本月」—— STORE 里有一笔下月记录，加载后 currentKey 会落到最新月份 */
  await page.evaluate((mk) => {
    FINANCE_SESSION = 'stub-sess'; unlocked.expense = true; unlocked.settings = true;
    currentKey = mk;
    switchTab('entry');
  }, CUR_MONTH);
  await page.waitForTimeout(400);

  /* 表格结构：表头文字 + 行（按项目名定位） */
  const entryState = () => page.evaluate(() => {
    const sec = [...document.querySelectorAll('#tab-entry .section')].find((s) => s.textContent.indexOf('采购支出') >= 0);
    if (!sec) return null;
    const ths = [...sec.querySelectorAll('thead th')].map((th) => th.textContent.trim());
    const rows = [...sec.querySelectorAll('tbody tr')].map((tr) => {
      const tds = [...tr.querySelectorAll('td')];
      const vals = [...tr.querySelectorAll('input')].map((i) => i.value);
      const phs = [...tr.querySelectorAll('input')].map((i) => i.placeholder || '');
      return { text: (tr.innerText + ' ' + vals.join(' ') + ' ' + phs.join(' ')).replace(/\s+/g, ' ').trim(),
               tds: tds.map((td) => {
                 const inputs = [...td.querySelectorAll('input')];
                 return (td.innerText + ' ' + inputs.map((i) => i.value + ' ' + (i.placeholder || '')).join(' ')).replace(/\s+/g, ' ').trim();
               }) };
    });
    return { ths, rows };
  });
  const rowByName = (state, name) => state.rows.find((r) => r.text.indexOf(name) >= 0) || null;

  /* ① 基线：没有「数量」列（两条都是普通采购） */
  const s1 = await entryState();
  chk('① 基线：采购表有「类型」列、没有「数量」列（两条都是普通采购）',
    s1 && s1.ths.indexOf('类型') >= 0 && s1.ths.indexOf('数量') < 0 && s1.ths.length === 6,
    s1 ? s1.ths.join(' | ') : 'no table');

  /* ② 把「门禁卡」类型改成 A4纸 ⇒ 多出「数量」列，该行出现「总张数」输入框 */
  await page.evaluate(() => {
    const sel = [...document.querySelectorAll('#tab-entry select')]
      .find((x) => (x.getAttribute('onchange') || '').indexOf("updExpType('e1'") >= 0);
    if (!sel) return;
    sel.value = 'paper';
    sel.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await page.waitForTimeout(400);
  const s2 = await entryState();
  const r2 = rowByName(s2, '门禁卡');
  chk('② 类型选 A4纸 ⇒ 立刻多出「数量」列（表头 7 列），该行出现「总张数」输入框',
    s2 && s2.ths.indexOf('数量') >= 0 && s2.ths.length === 7
    && r2 && r2.tds.some((t) => t.indexOf('总张数') >= 0 || t === '填数量后按月摊销'),
    s2 ? s2.ths.join(' | ') : 'no table');
  chk('②b 「电费」是普通采购 ⇒ 数量列显示「—」（不需要填）',
    (() => { const r = rowByName(s2, '电费'); return r && r.tds.indexOf('—') >= 0; })(),
    JSON.stringify(s2.rows.map((r) => r.tds)));

  /* ③ 填 500 张 ⇒ 单价提示 ¥0.2000/张（金额 ¥100 ÷ 500 张） */
  await page.evaluate(() => {
    const inp = [...document.querySelectorAll('#tab-entry input[onchange]')]
      .find((i) => (i.getAttribute('onchange') || '').indexOf("updExp('e1','units'") >= 0);
    if (!inp) return;
    inp.value = '500';
    inp.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await page.waitForTimeout(500);
  const s3 = await entryState();
  const r3 = rowByName(s3, '门禁卡');
  chk('③ 填 500 张后显示单价 ¥0.2000/张（¥100 ÷ 500 张）',
    !!r3 && r3.text.indexOf('¥0.2000/张') >= 0, r3 ? r3.text : 'no row');
  if (withScreenshots) {
    await page.evaluate(() => {
      const h2 = [...document.querySelectorAll('#tab-entry h2')].find((h) => h.textContent.indexOf('采购支出') >= 0);
      if (h2) { h2.scrollIntoView({ block: 'start' }); window.scrollBy(0, -80); }
    });
    await page.screenshot({ path: path.join(OUT_DIR, '01-记账-类型与数量列.png'), fullPage: false });
  }

  /* ④ 收支清算：等用量取回（真实订单里 40 张在购买日之前、100 张在之后） */
  await page.click('#tabNav button[data-tab="dash"]');
  await page.waitForTimeout(1600);
  const dash = await page.evaluate(async () => {
    const kpiNum = document.querySelector('#tab-dash .kpi-card.c-blue .num');
    const mainTbl = [...document.querySelectorAll('#tab-dash .section')]
      .find((s) => s.textContent.indexOf('采购明细') >= 0);
    const rows = mainTbl ? [...mainTbl.querySelectorAll('tbody tr')].map((tr) => tr.innerText.replace(/\s+/g, ' ').trim()) : [];
    const cons = document.getElementById('consumableSection');
    const tbl = [...document.querySelectorAll('#tab-dash table')].find((t) => t.textContent.indexOf('分摊比例') >= 0);
    const alloc = tbl ? [...tbl.querySelectorAll('tbody tr')].map((tr) => [...tr.querySelectorAll('td')].map((td) => td.innerText.trim())) : [];
    const adv = [...document.querySelectorAll('#tab-dash .section')]
      .find((s) => s.textContent.indexOf('垫付清算') >= 0);
    const advRows = adv ? [...adv.querySelectorAll('tbody tr')].map((tr) => tr.innerText.replace(/\s+/g, ' ').trim()) : [];
    let probe = null;
    try {
      const raw = await fetch('/api/proxy/admin/statistics/revenue?' + new URLSearchParams({
        start_date: currentKey + '-01', end_date: currentKey + '-28' }).toString())
        .then((x) => x.json());
      const orders = (raw.data && raw.data.orders) || [];
      const uu = _usageFromOrders(orders);
      const c = _usageCache[currentKey];
      probe = { n: orders.length, keys: orders.map((o) => _orderMemberKey(o)),
                names: orders.map((o) => o.owner_name), byMember: uu.pagesByMember,
                pages: uu.pages, sheets: uu.sheets,
                cached: c ? { byMember: c.pagesByMember, pages: c.pages, sheets: c.sheets,
                              fetchedAt: c.fetchedAt, orders: c.orders, files: c.files } : null,
                members: getMembers().map((m) => m.name) };
    } catch (e) { probe = { err: String(e) }; }
    let ident = null;
    try {
      const r = calcMonthWithUsage(currentKey);
      const sum = (o) => Object.keys(o).reduce((s, k) => s + o[k], 0);
      ident = { net: sum(r.net), book: sum(r.bookPL), actual: sum(r.actualPL),
                teamPL: r.totalInc - r.totalExp - r.depTotal, totalExp: r.totalExp,
                basis: getPurchaseBasis(), useUsage: r.useUsage, usageTotal: r.usageTotal,
                usagePages: r.usagePages, cfgBasis: STORE.config.purchaseBasis };
    } catch (e) { ident = { err: String(e) }; }
    return {
      kpiCount: kpiNum ? kpiNum.getAttribute('data-count') : null,
      rows: rows,
      consText: cons ? cons.innerText.replace(/\s+/g, ' ').trim() : null,
      alloc: alloc,
      advRows: advRows,
      ident: ident,
      probe: probe,
    };
  });
  chk('⑤ 采购支出 KPI = ¥70（电费 50 + 本月摊销 20），不是整笔 ¥150', dash.kpiCount === '7000', dash.kpiCount);
  chk('⑥ 采购明细里只有摊销行 ¥20.00，没有整笔 ¥100.00',
    dash.rows.some((r) => r.indexOf('A4纸摊销') >= 0 && r.indexOf('¥20.00') >= 0)
    && !dash.rows.some((r) => r.indexOf('门禁卡') >= 0 && r.indexOf('¥100.00') >= 0)
    && !dash.rows.some((r) => r.indexOf('A4纸采购') >= 0),
    JSON.stringify(dash.rows));
  chk('⑥b 摊销行带「长期耗材摊销」标记、代付人是采购人 霍楠',
    dash.rows.some((r) => r.indexOf('A4纸摊销') >= 0 && r.indexOf('长期耗材摊销') >= 0 && r.indexOf('霍楠') >= 0));
  chk('⑦ 摊销区块：总量 500 张 / 单价 ¥0.2000 / 累计 100 张 / 已摊销 ¥20.00 / 本月 100 张 / 剩余 ¥80.00',
    !!dash.consText && dash.consText.indexOf('500 张') >= 0 && dash.consText.indexOf('¥0.2000/张') >= 0
    && dash.consText.indexOf('100 张') >= 0 && dash.consText.indexOf('¥20.00') >= 0
    && dash.consText.indexOf('¥80.00') >= 0 && dash.consText.indexOf('剩余未摊销') >= 0,
    dash.consText ? dash.consText.slice(0, 240) : 'no section');
  chk('⑦b 返还对象写明采购人 霍楠 ¥20.00',
    !!dash.consText && dash.consText.indexOf('返还给采购人') >= 0 && dash.consText.indexOf('霍楠') >= 0,
    dash.consText ? dash.consText.slice(-180) : '');
  chk('⑧ 购买日之前的 40 张没有算进本月摊销（本月用量仍是 100 张，不是 140 张）',
    !!dash.consText && dash.consText.indexOf('本月用量') >= 0
    && (dash.consText.match(/本月用量 100 张/) || dash.consText.match(/100 张/)) !== null
    && dash.consText.indexOf('140') < 0,
    dash.consText ? dash.consText.slice(0, 240) : '');
  const allocOf = (name) => dash.alloc.find((r) => r[0] === name) || [];
  chk('⑨ 按用量分摊：霍楠 100 页 → −¥50.00、姚懿祥 40 页 → −¥20.00',
    allocOf('霍楠')[1] === '100 页' && allocOf('霍楠')[3] === '−¥50.00'
    && allocOf('姚懿祥')[1] === '40 页' && allocOf('姚懿祥')[3] === '−¥20.00',
    JSON.stringify(dash.alloc.map((r) => r.slice(0, 4))) + ' / ident=' + JSON.stringify(dash.ident) + ' / probe=' + JSON.stringify(dash.probe));
  chk('⑩ 三条恒等式仍成立：ΣN ≡ 0、ΣB ≡ ΣC ≡ Π = I − E − D',
    dash.ident && Math.abs(dash.ident.net) <= 1 && dash.ident.book === dash.ident.actual
    && dash.ident.actual === dash.ident.teamPL && dash.ident.totalExp === 7000,
    JSON.stringify(dash.ident));
  chk('⑩b 垫付清算：霍楠 +¥20.00（耗材摊销）、姚懿祥 +¥50.00（电费）',
    dash.advRows.some((r) => r.indexOf('霍楠') >= 0 && r.indexOf('+¥20.00') >= 0)
    && dash.advRows.some((r) => r.indexOf('姚懿祥') >= 0 && r.indexOf('+¥50.00') >= 0),
    JSON.stringify(dash.advRows));
  if (withScreenshots) {
    await page.evaluate(() => {
      const h2 = [...document.querySelectorAll('#tab-dash h2')].find((h) => h.textContent.indexOf('长期耗材摊销') >= 0);
      if (h2) { h2.scrollIntoView({ block: 'start' }); window.scrollBy(0, -80); }
    });
    await page.waitForTimeout(200);
    await page.screenshot({ path: path.join(OUT_DIR, '02-收支清算-长期耗材摊销.png'), fullPage: false });
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: path.join(OUT_DIR, '03-收支清算-采购支出KPI.png'), fullPage: false });
  }

  /* ⑪ 购买月之后（下个月）：摊销区块仍要显示本月摊销，不能变成「还没有长期耗材采购」 */
  await page.selectOption('#dash-month', NEXT_MONTH).catch(async () => {
    await page.evaluate((mk) => {
      const sel = document.getElementById('dash-month');
      sel.value = mk;
      sel.dispatchEvent(new Event('change', { bubbles: true }));
    }, NEXT_MONTH);
  });
  await page.waitForTimeout(1800);
  const next = await page.evaluate(() => {
    const cons = document.getElementById('consumableSection');
    const kpi = document.querySelector('#tab-dash .kpi-card.c-blue .num');
    const kpiDep = document.querySelector('#tab-dash .kpi-card.c-blue .sub');
    return {
      consText: cons ? cons.innerText.replace(/\s+/g, ' ').trim() : null,
      kpi: kpi ? kpi.getAttribute('data-count') : null,
      sub: kpiDep ? kpiDep.innerText.trim() : null,
    };
  });
  chk('⑪ 购买月之后：摊销区块仍显示本月摊销（50 张 / ¥10.00 / 剩余 ¥70.00），不是「还没有长期耗材采购」',
    !!next.consText && next.consText.indexOf('本月摊销合计') >= 0
    && next.consText.indexOf('还没有长期耗材采购') < 0
    && next.consText.indexOf('50 张') >= 0 && next.consText.indexOf('¥10.00') >= 0
    && next.consText.indexOf('¥70.00') >= 0,
    next.consText ? next.consText.slice(0, 240) : 'no section');
  chk('⑪b 下个月采购支出 KPI = ¥10.00（只算本月 50 张摊销，整笔 ¥100 不在下个月）',
    next.kpi === '1000' && (next.sub || '').indexOf('含耗材摊销 ¥10.00') >= 0,
    `${next.kpi} / ${next.sub}`);

  /* ⑫ 历史记录：当月采购总额同口径 = ¥70.00（按月份标签定位，不依赖行序） */
  await page.click('#tabNav button[data-tab="history"]');
  await page.waitForTimeout(900);
  const hist = await page.evaluate(() => {
    const tbl = document.querySelector('#tab-history table');
    if (!tbl) return null;
    return [...tbl.querySelectorAll('tbody tr')].map((tr) => [...tr.querySelectorAll('td')].map((td) => td.innerText.trim()));
  });
  const histRow = (hist || []).find((r) => r[0] === CUR_LABEL) || [];
  chk('⑫ 历史记录当月「采购总额」= ¥70.00（与收支清算同口径，不是 ¥150）',
    !!histRow.length && histRow[1] === '¥70.00' && histRow[2] === '¥35.00', JSON.stringify(histRow.slice(0, 3)));

  /* ⑬ 配置真的写回（类型 + 数量） */
  await page.waitForTimeout(700);
  const lastWrite = writes.length ? writes[writes.length - 1] : '';
  chk('⑬ 类型 A4纸 + 数量 500 已写回云端配置',
    lastWrite.indexOf('"kind":"paper"') >= 0 && lastWrite.indexOf('"units":500') >= 0,
    lastWrite.length + ' bytes');

  chk('⑭ 全程零 JS 异常', errors.length === 0, errors.slice(0, 3).join(' | '));

  await ctx.close();
  return results;
}

/* ══════════════════════════════════════════════════════════════
   静态接线
   ══════════════════════════════════════════════════════════════ */
const staticResults = [];
{
  const chk = (name, cond, detail) => staticResults.push([name, !!cond, detail === undefined ? '' : String(detail)]);
  chk('静态① 记账页「数量」列是条件渲染（hasConsumable 才多出这一列）',
    SRC.indexOf("var hasConsumable = m.expenses.some(isConsumableExpense);") >= 0
    && SRC.indexOf("+ (hasConsumable ? '<th>数量</th>' : '')") >= 0);
  chk('静态② 类型下拉走 updExpType（重绘列结构），数量走 updExp(units)',
    SRC.indexOf("onchange=\"updExpType(\\'' + eid + '\\',this.value)\"") >= 0
    && SRC.indexOf("onchange=\"updExp(\\'' + eid + '\\',\\'units\\',+this.value)\"") >= 0);
  chk('静态③ 摊销区块有稳定 id（consumableSection）与「本月摊销合计」',
    SRC.indexOf('id="consumableSection"') >= 0 && SRC.indexOf('本月摊销合计') >= 0);
  chk('静态④ calcMonth 的摊销分支：能摊销的整笔采购不计入本月',
    SRC.indexOf('exps = exps.filter(function (e) { return !isAmortizableExpense(e); });') >= 0);
}

/* ══════════════════════════════════════════════════════════════
   跑两遍：正常源码 + 变异（整笔采购照旧计入）自证
   ══════════════════════════════════════════════════════════════ */
const normal = await runScenario(null, true);
const mutated = await runScenario('no-amort', false);

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

let bad = report('静态接线', staticResults);
bad += report('正常源码（真浏览器）', normal);

console.log('\n=== 变异自证（calcMonth 不再替换整笔采购 = 修前行为） ===');
const coreName = '⑤ 采购支出 KPI = ¥70（电费 50 + 本月摊销 20），不是整笔 ¥150';
const coreMut = mutated.find((r) => r[0] === coreName);
const typeMut = mutated.find((r) => r[0].indexOf('② 类型选 A4纸') === 0);
const priceMut = mutated.find((r) => r[0].indexOf('③ 填 500 张') === 0);
const okMut = !!coreMut && coreMut[1] === false
  && !!typeMut && typeMut[1] === true
  && !!priceMut && priceMut[1] === true;
if (!okMut) bad++;
console.log(`[${okMut ? 'PASS' : 'FAIL'}] 变异后核心判据 ⑤ 转红（KPI 实际 ${coreMut ? (coreMut[1] ? '仍绿' : '15000' ) : '缺失'}），`
  + `类型列 ② 与单价 ③ 不受影响（${typeMut && typeMut[1] ? '绿' : '红'} / ${priceMut && priceMut[1] ? '绿' : '红'}）`);

console.log('\n' + '='.repeat(60));
console.log(`已跑断言 ${staticResults.length + normal.length + 1} 项，失败 ${bad} 项`);
process.exit(bad ? 1 : 0);
