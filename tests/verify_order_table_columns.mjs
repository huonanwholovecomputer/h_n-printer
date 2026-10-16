/* 验收 · 收支清算「订单明细」类型列按需取列 + 右侧三列完整显示（v6.9）
 *
 * 为什么要有这一份：类型列原来恒占 7 个固定槽（432px），而这个月常常只用到第 1~2 槽 ——
 * 整列大段空槽，右侧「时间 / 开始打印 / 接单设备」反被挤到截断。改法（**当前页**无人使用的槽整列移除、
 * 类型列变窄、腾出的宽度补给右侧三列）**必须用真实布局量**才算数：JS 里算的列宽是不是真的落到了
 * 表格上、文字是不是真的不再被 ellipsis 截掉，只有真浏览器 + 真 CSS 能回答。
 *
 * 做法：起本地 HTTP 服务（静态文件来自 local_print_tool/finance/，/api/** 全部打桩，绝不发外网请求），
 * Playwright 打开 settlement.html → 切到「云端数据」→ 直接换 _cloudData.orders + 局部重绘，
 * 逐场景断言：表头列宽、类型槽位数与 grid-template-columns、右侧三列 scrollWidth ≤ clientWidth
 * （= 文字完整显示）、空槽标签确已消失、跨行槽位仍竖直对齐；翻页 / 改每页条数按**新一页**的内容
 * 重排列宽（判据是当前页，不是整段时间轴），且任何订单的标签都不会因隐藏而丢失。
 * 产物：截图存 截图展示/订单明细列宽/。
 *
 * 用法：node tests/verify_order_table_columns.mjs [settlement.html 路径]
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
const OUT_DIR = path.join(ROOT, '截图展示', '订单明细列宽');

let bad = 0;
function chk(name, cond, detail) {
  if (!cond) bad++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (!cond && detail !== undefined ? `  -- ${detail}` : ''));
}

/* ── 夹具 ──────────────────────────────────────────────────────────────────
   接单设备列取「最坏情况」的真实格式：4 字所有者 + DESKTOP-<7>-<12> 客户端 id
   （后端 device_display_label = 「{所有者}的设备 | {client_id}」）。 */
const DEVICE_LABEL = '欧阳娜娜的设备 | DESKTOP-EJGEB1V-a6c1a1365e';
const DEVICE_ID = 'DESKTOP-EJGEB1V-a6c1a1365e';
function ord(i, extra) {
  const base = {
    order_id: 900000 + i,
    order_number: 'HN20261012-' + String(1000 + i),
    source: 'local', openid: 'local', nickname: '', owner_name: '欧阳娜娜',
    status: 'sent', total_price: 4.6,
    created_at: '2026-10-12 12:38:0' + (i % 10),
    print_started_at: '2026-10-12 12:38:0' + (i % 10),
    wait_seconds: 42,
    is_admin_print: 0, files_count: 1, total_page_count: 22,
    received_client: DEVICE_ID, received_label: DEVICE_LABEL,
    files: [{ id: 'f' + i, file_name: 'a.pdf', copies: 1, page_count: 22, duplex: 'on',
              page_range: '', total_price: 4.6, status: 'sent', is_free: false }],
  };
  const o = Object.assign(base, extra || {});
  if (o._free) {  // 便利标记：造一个免费文件（免费槽的判据是 money.free > 0）
    delete o._free;
    o.files = o.files.map(function (f) { return Object.assign({}, f, { is_free: true, total_price: 0 }); });
  }
  return o;
}
const ALL_SLOTS = { auto_print: 1, schedule_mode: 'at', scheduled_at: '2026-10-13 09:00',
                    delivery_enabled: 1, delivery_percentage: 10, urgency: '高', urgency_price: 1,
                    cover_page: 1, cover_page_price: 0.1 };
/* 场景（每个都是「当前筛选后的整批订单」） */
const SC = {
  /* 典型月：只有 自打/接单 + 自动 → 2 槽 */
  typical: [ord(1, { auto_print: 1 }), ord(2, { is_admin_print: 1 }),
            ord(3, { source: 'wechat', openid: 'o1', nickname: '顾客甲' })],
  /* 极端：7 槽全用（含免费文件、预约、派送、加急、封面） */
  all: [ord(1, ALL_SLOTS), ord(2, Object.assign({}, ALL_SLOTS, { _free: 1 })),
        ord(3, { is_admin_print: 1, auto_print: 1 })],
  /* 6 槽：无人用「自动」，其余附加服务都有人用 */
  six: [ord(1, { schedule_mode: 'countdown', scheduled_at: '2026-10-13 09:00', delivery_enabled: 1,
                 delivery_percentage: 10, urgency: '中', urgency_price: 0.5, cover_page: 1, cover_page_price: 0.1 }),
        ord(2, { _free: 1 }), ord(3, {})],
  /* 1 槽：任何附加服务都没人用 */
  minimal: [ord(1, {}), ord(2, { is_admin_print: 1 }), ord(3, {})],
};
/* 期望值（按 _typeColWidth/TYPE_COL_BASE 独立手算，不读页面里的常量，避免「用实现验实现」）
   固定 9 列合计 = 34+140+64+104+84+88+58+96+88 = 756
   类型列宽 = 58n + 4(n-1) + 30；腾出 = 432 − 类型列宽；按 时间→开始打印→接单设备 补到 146/146/320 */
const FIXED_W = 756;
const EXP = {
  typical: { slots: 2, type: 150, time: 146, print: 146, device: 320, table: FIXED_W + 150 + 146 + 146 + 320 },   // 1518
  all:     { slots: 7, type: 460, time: 132, print: 132, device: 200, table: FIXED_W + 460 + 132 + 132 + 200 },   // 1680
  six:     { slots: 6, type: 398, time: 146, print: 146, device: 206, table: FIXED_W + 398 + 146 + 146 + 206 },   // 1652
  minimal: { slots: 1, type: 88,  time: 146, print: 146, device: 320, table: FIXED_W + 88 + 146 + 146 + 320 },    // 1456
};
const SLOT_TAGS = { auto: '自动', sched: '预约', free: '免费', deliv: '派送', cover: '封面' };

const STORE = {
  config: { investRate: 0.05, scoreCap: 15, passwords: {},
            members: [{ id: 'ma', name: '欧阳娜娜', color: '#4472C4' }] },
  records: { expenses: [], income: [], scores: [], transfers: [] },
  notes: {}, meta: { version: 1 },
};
const json = (res, obj) => { res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(obj)); };
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  const p = decodeURIComponent(u.pathname);
  if (p.startsWith('/api/')) {
    if (p === '/api/health') return json(res, { success: true, cloud: true });
    if (p === '/api/proxy/finance/config') {
      if (req.method === 'POST') return json(res, { success: true, server_version: 2 });
      return json(res, { success: true, data: STORE });
    }
    if (p === '/api/proxy/pricing') return json(res, { success: true, data: { simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1, delivery_percentages: {}, urgency_prices: {} } });
    if (p === '/api/proxy/admin/statistics/months') return json(res, { success: true, data: { months: [{ month: '2026-10' }] } });
    if (p === '/api/proxy/admin/statistics/revenue') return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: SC.typical } });
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
/* 断言用视口：1440（#cloud-orders 撑满视口左右各留 20px ⇒ 容器 1400 < 各场景 min-width ⇒
   表格严格按各列指定宽渲染，量出来的就是分配结果本身） */
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
const pageErrors = [];
const external = [];
page.on('pageerror', (e) => pageErrors.push(process.env.PW_DEBUG_ERRORS ? String(e.stack || e.message) : e.message));
page.on('request', (r) => { if (!r.url().startsWith(BASE) && !r.url().startsWith('data:')) external.push(r.url()); });

/* 换一批订单并局部重绘（走真实渲染路径 _rerenderOrdersOnly） */
async function useOrders(list) {
  await page.evaluate((orders) => { window._cloudData.orders = orders; _ordersPage = 1; _rerenderOrdersOnly(); }, list);
  await page.waitForTimeout(120);
}
async function snap() {
  return page.evaluate(() => {
    const table = document.querySelector('.order-table');
    const ths = [...table.querySelectorAll('thead th')];
    const rows = [...table.querySelectorAll('tr.order-row')];
    const cell = (td) => ({ w: Math.round(td.getBoundingClientRect().width), sw: td.scrollWidth, cw: td.clientWidth, txt: td.textContent.trim() });
    return {
      cols: ths.length,
      th: ths.map((e) => ({ t: e.textContent.trim(), w: Math.round(e.getBoundingClientRect().width) })),
      tableW: Math.round(table.getBoundingClientRect().width),
      minW: table.style.minWidth,
      typeW: Math.round(table.querySelector('tr.order-row td:nth-child(10)').getBoundingClientRect().width),
      grids: rows.map((r) => {
        const g = r.querySelector('.type-grid');
        const cells = [...g.children];
        return { n: cells.length, cols: getComputedStyle(g).gridTemplateColumns,
                 lefts: cells.map((c) => Math.round(c.getBoundingClientRect().left)),
                 typeText: r.children[9].textContent.trim(),
                 time: cell(r.children[10]), print: cell(r.children[11]), device: cell(r.children[12]) };
      }),
    };
  });
}
/* 文字完整显示 = 内容没有超出内容盒（溢出才轮到 text-overflow: ellipsis 截断） */
const fits = (c) => c.sw <= c.cw;

try {
  await page.goto(BASE + '/settlement.html', { waitUntil: 'domcontentloaded' });
  chk('页面脚本加载无语法错误', pageErrors.length === 0, pageErrors.join(' | '));

  await page.click('#tabNav button[data-tab="cloud"]');
  await page.waitForFunction(() => window._cloudData && window._cloudData.orders && document.querySelector('.order-table'), null, { timeout: 20000 });
  console.log('云端数据页已渲染');

  /* ── 场景 1：典型月（只有 自打/接单 + 自动 两槽）──────────────────────── */
  await useOrders(SC.typical);
  let s = await snap();
  const e1 = EXP.typical;
  chk('表头仍是 13 列（只在槽内取列，不删表格列）', s.cols === 13, s.cols);
  chk(`类型列只剩实际用到的 ${e1.slots} 槽（整列移除无人使用的槽）`,
    s.grids.every((g) => g.n === e1.slots), s.grids.map((g) => g.n).join(','));
  chk(`槽位网格改为 repeat(${e1.slots},58px)（固定槽宽 → 跨订单竖直对齐不变）`,
    s.grids.every((g) => g.cols === new Array(e1.slots).fill('58px').join(' ')),
    s.grids[0] && s.grids[0].cols);
  chk(`类型列缩窄到 ${e1.type}px（原 432px，释放 ${432 - e1.type}px）`, s.typeW === e1.type, s.typeW);
  chk('类型列各列宽（类型/时间/开始打印/接单设备）按「腾出宽度补给右侧」分配',
    JSON.stringify(s.th.slice(9).map((c) => c.w)) === JSON.stringify([e1.type, e1.time, e1.print, e1.device]),
    JSON.stringify(s.th.slice(9)));
  chk(`表 min-width 收窄到列宽之和 ${e1.table}px（补不完的余量不留空白）`, s.minW === e1.table + 'px', s.minW);
  chk(`表格实宽 = ${e1.table}px（1440 视口下严格按分配渲染）`, s.tableW === e1.table, s.tableW);
  chk('「时间」「开始打印」完整显示到秒（不再 ... 截断）',
    s.grids.every((g) => fits(g.time) && fits(g.print)),
    JSON.stringify(s.grids.map((g) => [g.time.sw + '/' + g.time.cw, g.print.sw + '/' + g.print.cw])));
  chk('「时间」「开始打印」文字是完整的 19 字符时间戳',
    s.grids.every((g) => /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(g.time.txt) && g.time.txt.length === 19 &&
                          /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(g.print.txt)),
    s.grids[0] && s.grids[0].time.txt);
  chk(`「接单设备」完整显示「${DEVICE_LABEL}」（原 200px 必截断）`,
    s.grids.every((g) => fits(g.device)), JSON.stringify(s.grids.map((g) => g.device.sw + '/' + g.device.cw)));
  chk('跨订单槽位仍竖直对齐（每个 type-cell 的左边界逐行一致）',
    s.grids.every((g) => JSON.stringify(g.lefts) === JSON.stringify(s.grids[0].lefts)),
    JSON.stringify(s.grids.map((g) => g.lefts)));
  chk('无人使用的槽：整列不渲染（预约/免费/派送/加急/封面都不出现）',
    s.grids.every((g) => ['预约', '免费', '派送', '封面'].every((t) => !g.typeText.includes(t))),
    s.grids.map((g) => g.typeText).join(' | '));
  chk('用到的槽照常显示（自动 出现在用过它的订单行上、自打/接单 每行都有）',
    s.grids[0].typeText.includes('自动') && s.grids[1].typeText.includes('自打') &&
    s.grids.filter((g) => /自打|接单/.test(g.typeText)).length === s.grids.length,
    s.grids.map((g) => g.typeText).join(' | '));

  /* 翻页：判据是**当前页**渲染的那批订单（所见即所得）—— 本页没人用的服务不留空列。
     故意把独占「自动」的那条排在最后一页（第 25 条）：第 1 页应当没有该槽，翻到第 2 页才出现。 */
  const many = [];
  for (let i = 1; i <= 25; i++) many.push(ord(i, i <= 24 ? {} : { auto_print: 1 }));
  await useOrders(many);
  const p1 = await snap();
  chk('25 条跨 2 页：第 1 页 20 行，独占「自动」的那条在第 2 页（第 25 条）',
    p1.grids.length === 20 && p1.grids.every((g) => !g.typeText.includes('自动')),
    p1.grids.length + ' 行');
  chk('第 1 页本页无人用「自动」⇒ 该槽整列移除（类型列 88px，不是按整批留着的 150px）',
    p1.typeW === EXP.minimal.type && p1.grids.every((g) => g.n === 1),
    p1.typeW + ' / n=' + p1.grids[0].n);
  chk('第 1 页腾出的 344px 全给右侧三列（时间/开始打印/接单设备 全部完整显示）',
    p1.grids.every((g) => fits(g.time) && fits(g.print) && fits(g.device)) && p1.minW === EXP.minimal.table + 'px',
    p1.minW);
  /* 不变量：每行「该有的标签」都渲染出来了（槽位判据与渲染同页 ⇒ 不可能因隐藏而丢标签），
     且每行的槽位数都等于本页槽位数（跨行竖直对齐的前提） */
  const inv1 = await page.evaluate((orders) => [...document.querySelectorAll('tr.order-row')].map((r, i) => ({
    cells: r.querySelector('.type-grid').children.length,
    hasAuto: r.children[9].textContent.includes('自动'), wantAuto: !!orders[i].auto_print,
  })), many.slice(0, 20));
  chk('第 1 页 20 行：槽位数一致、且没有任何订单的标签被隐藏掉',
    inv1.every((r) => r.cells === 1 && r.hasAuto === r.wantAuto),
    JSON.stringify(inv1.filter((r) => r.cells !== 1 || r.hasAuto !== r.wantAuto).slice(0, 3)));

  await page.evaluate(() => onOrdersPage(2));
  await page.waitForTimeout(120);
  const p2 = await snap();
  chk('翻到第 2 页（本页有订单用「自动」）⇒ 该槽重新出现：类型列 150px、槽位数 2',
    p2.grids.length === 5 && p2.typeW === EXP.typical.type && p2.grids.every((g) => g.n === 2),
    p2.grids.length + ' 行 / ' + p2.typeW + ' / n=' + p2.grids[0].n);
  const inv2 = await page.evaluate((orders) => [...document.querySelectorAll('tr.order-row')].map((r, i) => ({
    cells: r.querySelector('.type-grid').children.length,
    hasAuto: r.children[9].textContent.includes('自动'), wantAuto: !!orders[i].auto_print,
  })), many.slice(20));
  chk('第 2 页 5 行：第 25 条显示「自动」、其余没有；每行槽位数都等于本页槽位数（2）',
    inv2.every((r) => r.cells === 2 && r.hasAuto === r.wantAuto) && inv2.filter((r) => r.hasAuto).length === 1,
    JSON.stringify(inv2));
  chk('两页列宽不同 —— 证明判据确实是「当前页」而不是整段时间轴',
    p1.typeW !== p2.typeW && p1.grids[0].n !== p2.grids[0].n, `${p1.typeW} vs ${p2.typeW}`);

  /* 改「每页条数」同样按新一页的内容重排（这也是刻意的：判据永远是眼前那批订单） */
  await page.evaluate(() => onOrdersPerPage(50));
  await page.waitForTimeout(120);
  const p3 = await snap();
  chk('每页改 50 条（第 1 页装下含「自动」的全部 25 条）⇒ 该槽回到本页：类型列 150px、25 行',
    p3.grids.length === 25 && p3.typeW === EXP.typical.type && p3.grids.every((g) => g.n === 2),
    p3.grids.length + ' 行 / ' + p3.typeW);

  /* ── 场景 2：7 槽全用（回归兜底 —— 与改动前一致）────────────────────── */
  await useOrders(SC.all);
  s = await snap();
  const e2 = EXP.all;
  chk('7 槽全用：槽位数 7、网格仍是 repeat(7,58px)', s.grids.every((g) => g.n === 7 && g.cols === new Array(7).fill('58px').join(' ')), s.grids[0] && s.grids[0].cols);
  chk(`7 槽全用：类型列 ${e2.type}px（旧的 432px 装不下 430px 网格，会把「封面」标签溢出压到时间列）`, s.typeW === e2.type, s.typeW);
  chk('7 槽全用：右侧三列回到旧硬编码宽 132/132/200（无宽度可腾）',
    JSON.stringify(s.th.slice(10).map((c) => c.w)) === JSON.stringify([e2.time, e2.print, e2.device]),
    JSON.stringify(s.th.slice(10)));
  chk('7 槽全用：类型标签不再溢出内容盒（原 430px 网格塞进 404px 内容盒会溢出 26px）',
    await page.evaluate(() => {
      const td = document.querySelector('tr.order-row td:nth-child(10)');
      return td.scrollWidth <= td.clientWidth;
    }),
    await page.evaluate(() => { const td = document.querySelector('tr.order-row td:nth-child(10)'); return td.scrollWidth + '/' + td.clientWidth; }));
  chk('7 槽全用：五个附加服务标签都渲染出来',
    Object.values(SLOT_TAGS).every((t) => s.grids.some((g) => g.typeText.includes(t))),
    s.grids.map((g) => g.typeText).join(' | '));

  /* ── 场景 3：6 槽（只腾出 34px）—— 余量不足时按优先级先满足 时间 / 开始打印 ──── */
  await useOrders(SC.six);
  s = await snap();
  let e = EXP.six;
  chk(`6 槽（无人用自动）：类型列 ${e.type}px、列宽分配 ${e.type}/${e.time}/${e.print}/${e.device}`,
    s.grids.every((g) => g.n === e.slots) && s.typeW === e.type &&
    JSON.stringify(s.th.slice(9).map((c) => c.w)) === JSON.stringify([e.type, e.time, e.print, e.device]),
    `n=${s.grids[0] && s.grids[0].n} type=${s.typeW} ` + JSON.stringify(s.th.slice(9).map((c) => c.w)));
  chk('6 槽：腾出的 34px 按 时间(+14) → 开始打印(+14) → 接单设备(+6) 的优先级分配',
    s.grids.every((g) => fits(g.time) && fits(g.print)),
    JSON.stringify(s.grids.map((g) => [g.time.sw + '/' + g.time.cw, g.print.sw + '/' + g.print.cw])));
  chk(`6 槽：余量只够「接单设备」长到 ${e.device}px（< 最坏标签所需 292px）⇒ 仍靠省略号 + title 悬停兜底`,
    s.grids.every((g) => !fits(g.device)) &&
    await page.evaluate(() => document.querySelector('tr.order-row td:nth-child(13)').title.length > 0),
    JSON.stringify(s.grids[0] && s.grids[0].device));
  chk(`6 槽：表 min-width=${e.table}px（腾出的宽度全部分配完，无余量）`, s.minW === e.table + 'px', s.minW);

  /* ── 场景 4：1 槽（腾出 344px）—— 三列全部拿满 ─────────────────────── */
  await useOrders(SC.minimal);
  s = await snap();
  e = EXP.minimal;
  chk(`1 槽（无任何附加服务）：类型列 ${e.type}px、列宽分配 ${e.type}/${e.time}/${e.print}/${e.device}`,
    s.grids.every((g) => g.n === e.slots) && s.typeW === e.type &&
    JSON.stringify(s.th.slice(9).map((c) => c.w)) === JSON.stringify([e.type, e.time, e.print, e.device]),
    `n=${s.grids[0] && s.grids[0].n} type=${s.typeW} ` + JSON.stringify(s.th.slice(9).map((c) => c.w)));
  chk(`1 槽：右侧三列全部完整显示，表 min-width 收窄到 ${e.table}px（补不完的余量不留空白）`,
    s.grids.every((g) => fits(g.time) && fits(g.print) && fits(g.device)) && s.minW === e.table + 'px',
    s.minW + ' / ' + JSON.stringify(s.grids[0] && [s.grids[0].device.sw, s.grids[0].device.cw]));

  /* ── 截图（视口放宽到 1620 让整张表可见）────────────────────────────── */
  await page.setViewportSize({ width: 1620, height: 1000 });
  /* 先留一张「当前页判据」的可视化证据：同一批 25 条订单 —— 第 1 页没人用「自动」⇒ 该槽不存在；
     第 2 页有一条用了「自动」⇒ 该槽出现（翻页即按新一页重排，这是刻意的） */
  await page.evaluate(() => onOrdersPerPage(20));
  await useOrders(many);   // useOrders 顺带把页码复位到第 1 页
  await page.locator('#cloud-orders').screenshot({ path: path.join(OUT_DIR, '06-当前页判定-第1页-无自动槽.png') });
  console.log('  截图 06-当前页判定-第1页-无自动槽.png —— 本页无人用「自动」，该槽整列不存在，宽度全给右侧三列');
  await page.evaluate(() => onOrdersPage(2));
  await page.waitForTimeout(120);
  await page.locator('#cloud-orders').screenshot({ path: path.join(OUT_DIR, '07-当前页判定-第2页-出现自动槽.png') });
  console.log('  截图 07-当前页判定-第2页-出现自动槽.png —— 本页有一条用了「自动」，该槽出现、类型列变宽');

  const shots = [
    ['typical', '01-典型月-仅接单与自动两槽.png', '典型月：类型列只剩「自打/接单 + 自动」，时间/开始打印/接单设备完整显示'],
    ['all', '02-七槽全用-回归不变.png', '七槽全用：类型列回到 460px（容纳 430px 网格不再溢出），右侧保持原宽'],
    ['minimal', '03-仅接单槽-类型列最窄.png', '只有一个槽：类型列 88px，接单设备拿到 320px'],
  ];
  for (const [key, file, note] of shots) {
    await useOrders(SC[key]);
    await page.locator('#cloud-orders').screenshot({ path: path.join(OUT_DIR, file) });
    console.log(`  截图 ${file} —— ${note}`);
  }

  chk('全程无外部网络请求（无 CDN / 无生产接口）', external.length === 0, external.slice(0, 3).join(' | '));
  chk('全程无页面 JS 异常', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
  console.log(`截图已存 → ${path.relative(ROOT, OUT_DIR)}`);
} finally {
  await browser.close();
  server.close();
}

console.log('\n' + '='.repeat(52));
console.log(bad ? `订单明细列宽验收：存在失败 ❌ (${bad})` : '订单明细列宽验收：全部通过 ✅');
process.exit(bad ? 1 : 0);
