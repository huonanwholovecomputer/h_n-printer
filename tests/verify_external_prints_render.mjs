/* 验收 · 收支清算「订单明细」合并外部打印监视（v7.3，2026-10-28 用户需求）
 *
 * 需求：外部打印监视（print_watch 账本里人工确认「已处理」的作业 = 绕过本工具直接打印、
 *       经人工确认出纸的纸）要并进「云端数据 → 订单明细」：
 *       ① 来源列显示新标签「外部监视」（独立配色，不与其他三色撞）；
 *       ② 类型列按现有「接单」槽渲染（不新增类型槽）；
 *       ③ 金额/收益计入（KPI 涨这一笔），用户列归到设备所有者；
 *       ④ 该行状态/类型**点不出**订正菜单（它不在云端 orders 表里，改不了）；
 *       ⑤ 「来源」筛选可单独只看外部作业；
 *       ⑥ 外部数据源 500 / 挂住时页面照常渲染云端订单（绝不白屏、不弹错）。
 *
 * 为什么必须真浏览器：来源标签是渲染结果（文字+配色+列宽）、类型槽是表头宽度的函数、
 * KPI 是同一份数组的聚合结果、订正入口是 DOM 上的 .st-edit、筛选是真实下拉交互 ——
 * 这些都只有真渲染能回答。
 *
 * 做法：起本地 HTTP 服务（静态文件来自 local_print_tool/finance/，/api/** 全部打桩，
 * 绝不发外网请求），Playwright 打开 settlement.html，按 pass 逐场景断言：
 *   A 基线（外部端点返回空）→ 记下 KPI / 页数 / 张数 / 类型列宽 / 槽数；
 *   B 外部端点返回 1 条已确认作业 → ① ② ③ ④ ⑤ 全量断言 + 截图；
 *   C 外部端点 500 → ⑥；D 外部端点挂住不响应 → ⑥（更强的版本：外部请求永不返回）；
 *   E 外部端点返回一条无 owner_name 的记录 → 用户列「未绑定」；
 *   F ?mode=local（本地取数路径）→ 外部订单同样被合并。
 *
 * 用法：node tests/verify_external_prints_render.mjs
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
const OUT_DIR = path.join(ROOT, '截图展示', '外部监视订单明细');
const STAMP = new Date().toISOString().replace(/[:.]/g, '-').replace('T', '_').slice(0, 19);

let bad = 0;
let total = 0;
function chk(name, cond, detail) {
  total++;
  if (!cond) bad++;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (!cond && detail !== undefined ? '  -- ' + detail : ''));
}

/* 三个来源标签的「文字 ↔ 颜色 class」对照仍与改动前一致（顺序由页面排序决定，按集合比对） */
function sameTagSet(tags) {
  const want = { '小程序': 'tag g', 'APP': 'tag b', '本地': 'tag y' };
  if (tags.length !== 3) return false;
  return tags.every((t) => want[t.txt] === t.cls);
}

/* ── 夹具：3 张云端订单（小程序 / APP / 本地各一，都已打印完成）+ 1 条外部监视记录 ── */
function ord(id, src, total0, pages, opts) {
  return Object.assign({
    order_id: id,
    order_number: 'HN20261018-00' + String(id).slice(-2),
    source: src, openid: src === 'local' ? 'local' : 'o' + id, nickname: src === 'local' ? '' : '顾客' + id,
    owner_name: src === 'local' ? '张三' : '',
    status: 'sent', total_price: total0,
    created_at: '2026-10-18 1' + String(id).slice(-1) + ':00:00',
    print_started_at: '2026-10-18 1' + String(id).slice(-1) + ':00:30',
    wait_seconds: 30, is_admin_print: false,
    files_count: 1, total_page_count: pages, total_sheet_count: Math.ceil(pages / 2),
    received_client: '', received_label: '',
    files: [{ id: 'f' + id, file_name: 'a' + id + '.pdf', copies: 1, page_count: pages,
              duplex: 'on', page_range: '', total_price: total0, status: 'sent', is_free: false }],
  }, opts || {});
}
const CLOUD_ORDERS = [
  ord('900001', 'wechat', 3.0, 10),
  ord('900002', 'app', 1.2, 4),
  ord('900003', 'local', 0.9, 2),
];
/* 与接口契约逐字段一致（stats_server GET /api/local/external_prints 的订单行） */
const EXT_ORDER = {
  order_id: 'ext:8:2026-10-28 18:00:13',
  order_number: 'EXT-20261028-180013-8',
  created_at: '2026-10-28 18:00:13',
  total_price: 0.7,
  total_page_count: 3,
  total_sheet_count: 2,
  owner_name: '张三',
  is_admin_print: false,
  source: 'external',
  openid: 'local',
  nickname: '',
  auto_print: false,
  schedule_mode: null,
  delivery_enabled: false,
  urgency: '低',
  cover_page: false,
  status: 'sent',
  unsynced: false,
  sync_status: 'synced',
  external: true,
  files: [{
    id: 'ext-8', file_name: '三页文档测试.pdf', copies: 1, page_count: 3,
    duplex: 'on', page_range: '', total_price: 0.7, status: 'sent',
    is_free: false, effective_pages: 3, sheets: 2, total_pages: 3, total_sheets: 2,
  }],
};
/* 账本里没记设备所有者的一条（页面必须显示「未绑定」而不是「本地任务」） */
const EXT_UNBOUND = Object.assign(JSON.parse(JSON.stringify(EXT_ORDER)), {
  /* 时刻刻意比云端三行都**旧**：用来证明外部行是「按时间插入」而不是「永远插到最前面」 */
  order_id: 'ext:9:2026-10-01 09:15:02', order_number: 'EXT-20261001-091502-9',
  created_at: '2026-10-01 09:15:02', total_price: 0.3, total_page_count: 1, total_sheet_count: 1,
  owner_name: '',
  files: [Object.assign({}, EXT_ORDER.files[0], {
    id: 'ext-9', copies: 1, page_count: 1, total_price: 0.3, effective_pages: 1, sheets: 1,
    total_pages: 1, total_sheets: 1, duplex: 'off',
  })],
});
const LOCAL_ORDERS = [
  ord('ln001', 'local', 1.5, 4),
  ord('ln002', 'local', 0.6, 2),
];
const STORE = {
  config: { investRate: 0.05, scoreCap: 15, passwords: {},
            members: [{ id: 'ma', name: '张三', color: '#4472C4' }] },
  records: { expenses: [], income: [], scores: [], transfers: [] },
  notes: {}, meta: { version: 1 },
};
const json = (res, obj) => { res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(obj)); };

/* 外部端点行为：empty / ok / ok_unbound / error(500) / hang(永不响应) */
let EXT_MODE = 'empty';
const extHits = [];          // 收到的 /api/local/external_prints 查询（断言带上了日期参数）
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  const p = decodeURIComponent(u.pathname);
  if (p.startsWith('/api/')) {
    if (p === '/api/health') return json(res, { success: true, cloud: true });
    if (p === '/api/local/external_prints') {
      extHits.push({ start: u.searchParams.get('start_date'), end: u.searchParams.get('end_date') });
      if (EXT_MODE === 'hang') return;     // 挂住：页面必须照常渲染云端订单（⑥ 的强版本）
      if (EXT_MODE === 'error') { res.writeHead(500, { 'Content-Type': 'text/html' }); return res.end('<h1>500 Internal Error</h1>'); }
      if (EXT_MODE === 'empty') return json(res, { success: true, orders: [], count: 0, skipped: 0,
                                                   simplex_price: 0.3, duplex_price: 0.4, owner_name: '' });
      if (EXT_MODE === 'ok_unbound') return json(res, { success: true, orders: [EXT_UNBOUND], count: 1,
                                                        skipped: 0, simplex_price: 0.3, duplex_price: 0.4, owner_name: '' });
      return json(res, { success: true, orders: [EXT_ORDER], count: 1, skipped: 1,
                         simplex_price: 0.3, duplex_price: 0.4, owner_name: '张三' });
    }
    if (p === '/api/proxy/finance/config') {
      if (req.method === 'POST') {
        req.resume();
        return req.on('end', () => json(res, { success: true, server_version: 2 }));
      }
      return json(res, { success: true, data: STORE });
    }
    if (p === '/api/local/finance_auth/status') {
      return json(res, { success: true, modules: {}, needs_setup: [],
                         session: { active: false, modules: [], expires_in: 0 },
                         session_ttl: 1800, read_only: false, integrity: 'ok' });
    }
    if (p === '/api/proxy/pricing') return json(res, { success: true, data: { simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1, delivery_percentages: {}, urgency_prices: {} } });
    if (p === '/api/proxy/admin/statistics/months') return json(res, { success: true, data: { months: [{ month: '2026-10' }] } });
    if (p === '/api/proxy/admin/statistics/revenue') return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: CLOUD_ORDERS } });
    if (p === '/api/local/orders') return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: LOCAL_ORDERS } });
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
const BASE = 'http://127.0.0.1:' + port;
fs.mkdirSync(OUT_DIR, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1100 }, deviceScaleFactor: 1 });
const pageErrors = [];
const externalReq = [];
page.on('pageerror', (e) => pageErrors.push(process.env.PW_DEBUG_ERRORS ? String(e.stack || e.message) : e.message));
page.on('request', (r) => { if (!r.url().startsWith(BASE) && !r.url().startsWith('data:')) externalReq.push(r.url()); });

/* 一次完整的「打开页面 → 切云端数据页 → 等订单表」；waitRows = 期望的订单行数 */
async function openCloud(mode, waitRows) {
  await page.goto(BASE + '/settlement.html' + (mode ? '?mode=' + mode : ''), { waitUntil: 'domcontentloaded' });
  await page.click('#tabNav button[data-tab="cloud"]');
  await page.waitForFunction(() => window._cloudData && window._cloudData.orders && document.querySelector('.order-table'), null, { timeout: 20000 });
  if (waitRows !== undefined) {
    await page.waitForFunction((n) => document.querySelectorAll('tr.order-row').length === n, waitRows, { timeout: 8000 })
      .catch(() => {});   // 不到期望行数交给断言去红，不在这里抛
  }
  await page.waitForTimeout(250);
}

const snap = () => page.evaluate(() => {
  const rows = [...document.querySelectorAll('tr.order-row')];
  const ths = [...document.querySelectorAll('.order-table thead th')];
  const cellText = (i) => rows.map((r) => (r.children[i] ? r.children[i].textContent.trim() : ''));
  const tagOf = (sel) => [...document.querySelectorAll(sel)].map((t) => ({
    txt: t.textContent.trim(), cls: t.className, title: t.getAttribute('title') || '',
    color: getComputedStyle(t).color, bg: getComputedStyle(t).backgroundColor,
    w: Math.round(t.getBoundingClientRect().width),
  }));
  const extTag = document.querySelector('tr.order-row td:nth-child(3) .tag.ext');
  const numOf = (sel) => {
    const el = document.querySelector(sel);
    return el ? parseInt(String(el.textContent).replace(/[^0-9]/g, ''), 10) : -1;
  };
  return {
    rows: rows.length,
    cols: ths.length,
    srcW: ths[2] ? Math.round(ths[2].getBoundingClientRect().width) : -1,
    typeW: ths[9] ? Math.round(ths[9].getBoundingClientRect().width) : -1,
    typeHeaderTxt: ths[9] ? ths[9].textContent.trim() : '',
    slots: rows.map((r) => r.querySelectorAll('td:nth-child(10) .type-cell').length),
    gridCols: rows.map((r) => { const g = r.querySelector('td:nth-child(10) .type-grid'); return g ? getComputedStyle(g).gridTemplateColumns : ''; }),
    srcTags: tagOf('tr.order-row td:nth-child(3) .tag'),
    typeTags: tagOf('tr.order-row td:nth-child(10) .type-cell .tag'),
    amounts: cellText(5),
    orderNos: cellText(1),
    users: cellText(3),
    times: cellText(10),
    statusEdit: rows.map((r) => r.querySelectorAll('td:nth-child(5) .st-edit').length),
    statusCaret: rows.map((r) => r.querySelectorAll('td:nth-child(5) .st-caret').length),
    typeEdit: rows.map((r) => r.querySelectorAll('td:nth-child(10) .st-edit').length),
    typeCaret: rows.map((r) => r.querySelectorAll('td:nth-child(10) .st-caret').length),
    kpi: parseInt(document.querySelector('.hero-card.c-green .num').getAttribute('data-count'), 10),
    pages: numOf('.hero-card.c-blue .num'),
    sheets: numOf('.hero-card.c-purple .num'),
    emptyBlock: !!document.querySelector('#cloud-orders .empty'),
    toasts: [...document.querySelectorAll('#toastWrap .toast')].map((t) => t.textContent),
    fixedSum: window.ORDER_FIXED_W,
    extTagW: extTag ? Math.round(extTag.getBoundingClientRect().width) : -1,
    extTdContentW: extTag ? Math.round(extTag.parentElement.getBoundingClientRect().width) - 28 : -1,
    hasSourcesDD: !!document.getElementById('dd-sources'),
    ddBtns: [...document.querySelectorAll('.dd > .dd-btn')].map((b) => b.textContent.trim()),
  };
});

let caught = null;
try {
  /* ══ A 基线：外部端点返回空数组（页面只应有 3 张云端订单） ═══════════════ */
  EXT_MODE = 'empty';
  await openCloud('', 3);
  let s = await snap();
  const uniq = (a) => JSON.stringify([...new Set(a)]);
  const base = { kpi: s.kpi, pages: s.pages, sheets: s.sheets, typeW: s.typeW, srcW: s.srcW,
                 slots: uniq(s.slots), grid: uniq(s.gridCols), rows: s.rows, cols: s.cols };
  chk('A1 基线：页面脚本加载无语法错误', pageErrors.length === 0, pageErrors.join(' | '));
  chk('A2 基线：只有 3 张云端订单，外部端点返回空 → 明细仍是 3 行', s.rows === 3, s.rows);
  chk('A3 基线：索取了 /api/local/external_prints 且带 start_date/end_date（与 _scopeRange 同口径）',
    extHits.length >= 1 && /^\d{4}-\d{2}-\d{2}$/.test(extHits[0].start || '') && /^\d{4}-\d{2}-\d{2}$/.test(extHits[0].end || ''),
    JSON.stringify(extHits[0]));
  chk('A4 基线：三色来源标签仍是 小程序/APP/本地 且 class 各为 tag g / tag b / tag y',
    sameTagSet(s.srcTags.filter((t) => t.txt !== '外部监视')),
    JSON.stringify(s.srcTags.map((t) => [t.txt, t.cls])));
  chk('A5 基线：类型列宽 / 槽位 / 网格 / 表头列数已记录（供 ② 对比）',
    s.cols === 13 && Number.isFinite(s.typeW), JSON.stringify(base));
  /* 回归守门（审查项）：外部端点没给数据时**一个字节都不许动** data.orders ——
     夹具刻意是「时间递增」的后端顺序，前端一旦无条件重排就会整表反过来。 */
  chk('A6 ★ 外部端点为空时，订单顺序与后端返回一字不差（不 concat、不 sort、不重排既有行）',
    JSON.stringify(s.orderNos) === JSON.stringify(['HN20261018-0001', 'HN20261018-0002', 'HN20261018-0003']),
    JSON.stringify(s.orderNos));

  /* ══ B 外部端点返回 1 条「已处理」作业 ═══════════════════════════════════ */
  EXT_MODE = 'ok';
  await openCloud('', 4);
  s = await snap();
  const extRowIdx = s.orderNos.indexOf('EXT-20261028-180013-8');
  const extSel = 'tr.order-row[data-order="extrow-ext-8-2026-10-28-18-00-13"]';

  /* ① 来源标签 */
  chk('①1 明细里出现外部监视那一行（订单号 EXT-20261028-180013-8，共 4 行）',
    extRowIdx >= 0 && s.rows === 4, JSON.stringify([s.rows, s.orderNos]));
  chk('①2 该行来源列文字是「外部监视」，class 为独立的 tag ext（不与其他三色混用）',
    extRowIdx >= 0 && s.srcTags[extRowIdx].txt === '外部监视' && /(^|\s)tag ext(\s|$)/.test(s.srcTags[extRowIdx].cls),
    JSON.stringify(s.srcTags[extRowIdx] || null));
  chk('①3 外部监视标签的配色与 小程序(绿)/APP(蓝)/本地(橙) 三个标签都不同（文字色与底色都不撞）',
    (() => {
      const e = s.srcTags[extRowIdx];
      if (!e) return false;
      return s.srcTags.filter((t) => t.txt !== '外部监视').every((t) => t.color !== e.color && t.bg !== e.bg);
    })(), JSON.stringify(s.srcTags.map((t) => [t.txt, t.color, t.bg])));
  chk('①4 同页其他来源标签仍是原来的文字与颜色 class（小程序/APP/本地 一个都没被改）',
    sameTagSet(s.srcTags.filter((t) => t.txt !== '外部监视')),
    JSON.stringify(s.srcTags.map((t) => [t.txt, t.cls])));
  chk('①5 标签 title 写清「绕过本工具直接打印、经人工确认出纸的作业」',
    extRowIdx >= 0 && s.srcTags[extRowIdx].title === '绕过本工具直接打印、经人工确认出纸的作业',
    JSON.stringify(s.srcTags[extRowIdx] && s.srcTags[extRowIdx].title));
  chk('①6 标签整块显示在「来源」列里（标签宽 ≤ 该列可用宽 +6px，「来源」列仍与基线同宽且 ≥88px）',
    s.extTagW > 0 && s.extTagW <= s.extTdContentW + 6 && s.srcW === base.srcW && s.srcW >= 88,
    JSON.stringify({ tagW: s.extTagW, tdW: s.extTdContentW, colW: s.srcW, baseColW: base.srcW }));

  /* ② 类型列：按现有「接单」槽渲染，不新增槽 */
  const extTypeTxt = (await page.evaluate((sel) => (document.querySelector(sel + ' td:nth-child(10) .type-cell .tag') || {}).textContent || '', extSel)).trim();
  chk('②1 该行类型列显示「🤝接单」（is_admin_print:false → 走现有接单槽）',
    extRowIdx >= 0 && extTypeTxt === '🤝接单', JSON.stringify([extTypeTxt, s.typeTags.map((t) => t.txt)]));
  chk('②2 没有新增类型槽：表头「类型」列宽、槽位数、网格与基线完全一致（1 个槽、58px 网格）',
    s.typeW === base.typeW && uniq(s.slots) === base.slots && uniq(s.gridCols) === base.grid &&
    s.slots.every((n) => n === 1) && s.typeHeaderTxt.indexOf('类型') === 0,
    JSON.stringify({ now: s.typeW, base: base.typeW, slots: s.slots, baseSlots: JSON.parse(base.slots), header: s.typeHeaderTxt }));
  chk('②3 表头列数仍是 13 列（类型列只在槽内取列，不删表格列）', s.cols === 13 && s.cols === base.cols, JSON.stringify([s.cols, base.cols]));
  chk('②4 固定 9 列合计仍是 756（来源列 64→88 的宽度是从文件/页数/张数挪来的，不撑大表格口径）',
    s.fixedSum === 756, String(s.fixedSum));

  /* ③ 金额与收益口径 */
  chk('③1 该行金额列 = 后端给的 total_price（¥0.70）',
    extRowIdx >= 0 && s.amounts[extRowIdx] === '¥0.70', JSON.stringify(s.amounts));
  chk('③2 KPI「云端收益记录」把这一笔算进去了（比基线多 70 分 = ¥0.70）',
    s.kpi - base.kpi === 70, base.kpi + ' → ' + s.kpi);
  chk('③3 同一份数组聚合：总页数 +3、纸张张数 +2（KPI 卡片跟着外部订单一起变）',
    s.pages - base.pages === 3 && s.sheets - base.sheets === 2,
    JSON.stringify({ pages: [base.pages, s.pages], sheets: [base.sheets, s.sheets] }));
  chk('③4 用户列归到设备所有者「张三」（走本地归属人分支，与成员同名时复用彩色胶囊）',
    extRowIdx >= 0 && s.users[extRowIdx].indexOf('张三') === 0, JSON.stringify(s.users));
  chk('③5 外部行按 created_at 倒序插到合适位置（10-28 18:00:13 比所有云端行都新 → 排最前）',
    extRowIdx === 0 && String(s.times[0]).indexOf('2026-10-28 18:00:13') === 0,
    JSON.stringify(s.times));
  chk('③6 ★ 并入外部行**不重排既有行**：三条云端行的相对顺序仍与后端返回一致',
    JSON.stringify(s.orderNos.filter((n) => n !== 'EXT-20261028-180013-8')) ===
    JSON.stringify(['HN20261018-0001', 'HN20261018-0002', 'HN20261018-0003']),
    JSON.stringify(s.orderNos));

  /* ④ 订正闸门 */
  const gate = await page.evaluate(() => {
    window.unlocked = window.unlocked || {};
    window.unlocked.orderstatus = true;          // 直接解锁，专测「外部订单仍然点不出菜单」
    window._rerenderOrdersOnly();
    return {
      extStatus: window._orderStatusEditable('ext:8:2026-10-28 18:00:13'),
      extType: window._orderTypeEditable('ext:8:2026-10-28 18:00:13'),
      cloudStatus: window._orderStatusEditable('900001'),
      cloudType: window._orderTypeEditable('900001'),
    };
  });
  await page.waitForTimeout(250);
  s = await snap();
  const isExtRow = (i) => s.orderNos[i] === 'EXT-20261028-180013-8';
  chk('④1 解锁后云端订单每行都有状态/类型订正入口（说明解锁确实生效，不是整体不可点）',
    s.statusEdit.filter((n) => n === 1).length === 3 && s.typeEdit.filter((n) => n === 1).length === 3,
    JSON.stringify({ statusEdit: s.statusEdit, typeEdit: s.typeEdit }));
  chk('④2 外部订单的状态/类型列都没有 .st-edit / .st-caret（即使已解锁）',
    extRowIdx >= 0 && s.statusEdit[extRowIdx] === 0 && s.statusCaret[extRowIdx] === 0 &&
    s.typeEdit[extRowIdx] === 0 && s.typeCaret[extRowIdx] === 0,
    JSON.stringify({ statusEdit: s.statusEdit, typeEdit: s.typeEdit, extRowIdx, isExt: s.orderNos.map(isExtRow) }));
  chk('④3 点外部订单的状态标签弹不出订正菜单',
    await (async () => {
      await page.click(extSel + ' td:nth-child(5)');
      await page.waitForTimeout(200);
      const has = !!(await page.$('#stMenu'));
      await page.keyboard.press('Escape');
      return !has;
    })());
  chk('④4 点外部订单的类型标签也弹不出菜单（两列同一把闸门）',
    await (async () => {
      await page.click(extSel + ' td:nth-child(10)');
      await page.waitForTimeout(200);
      const has = !!(await page.$('#tyMenu'));
      await page.keyboard.press('Escape');
      return !has;
    })());
  chk('④5 闸门自身：_orderStatusEditable / _orderTypeEditable 对 ext: id 直接返回 false，对数字 id 返回 true',
    gate.extStatus === false && gate.extType === false &&
    gate.cloudStatus === true && gate.cloudType === true,
    JSON.stringify(gate));

  /* 截图（带时间戳，存 截图展示/外部监视订单明细/） */
  const shot = path.join(OUT_DIR, '外部监视订单明细-' + STAMP + '.png');
  await page.evaluate(() => { window.unlocked.orderstatus = false; window._rerenderOrdersOnly(); });
  await page.waitForTimeout(200);
  await page.locator('#cloud-orders').screenshot({ path: shot });
  chk('截图已生成（外部监视订单明细，带时间戳）', fs.existsSync(shot) && fs.statSync(shot).size > 5000,
    shot + ' ' + (fs.existsSync(shot) ? fs.statSync(shot).size : 0));

  /* ⑤ 来源筛选 */
  const ddHasExt = await page.evaluate(() => ((document.getElementById('dd-sources') || {}).textContent || '').indexOf('外部监视') >= 0);
  chk('⑤1 口径栏「来源」下拉里出现「外部监视」选项', s.hasSourcesDD && ddHasExt, JSON.stringify(s.ddBtns));
  await page.click('.dd:has(#dd-sources) > .dd-btn');
  await page.waitForTimeout(150);
  chk('⑤2 来源下拉可展开（浮层显示，含 全选/清空 与 4 个来源）',
    await page.evaluate(() => {
      const p = document.getElementById('dd-sources');
      return !!p && p.style.display === 'block' && p.querySelectorAll('.dd-opt').length === 4;
    }));
  await page.evaluate(() => { document.getElementById('dd-sources').querySelectorAll('.dd-head button')[1].click(); });
  await page.waitForTimeout(250);
  let s2 = await snap();
  chk('⑤3 清空来源后一行都不显示（空集 = 无匹配，证明筛选真的有作用）', s2.rows === 0, String(s2.rows));
  await page.evaluate(() => {
    const l = [...document.querySelectorAll('#dd-sources .dd-opt')].find((x) => x.textContent.indexOf('外部监视') >= 0);
    l.click();                                             // 只勾「外部监视」
  });
  await page.waitForTimeout(300);
  s2 = await snap();
  chk('⑤4 只勾「外部监视」→ 只剩外部订单那一行，且它的来源标签就是「外部监视」',
    s2.rows === 1 && s2.orderNos[0] === 'EXT-20261028-180013-8' && s2.srcTags[0].txt === '外部监视',
    JSON.stringify([s2.rows, s2.orderNos, s2.srcTags.map((t) => t.txt)]));
  await page.evaluate(() => { document.getElementById('dd-sources').querySelectorAll('.dd-head button')[0].click(); });
  await page.waitForTimeout(300);
  s2 = await snap();
  chk('⑤5 选回「全部来源」→ 4 行全部恢复（外部订单与云端订单都在）',
    s2.rows === 4 && s2.orderNos.indexOf('EXT-20261028-180013-8') >= 0, JSON.stringify(s2.orderNos));

  /* ══ C 外部端点 500 ═════════════════════════════════════════════════════ */
  EXT_MODE = 'error';
  await openCloud('', 3);
  s = await snap();
  chk('⑥1 外部端点 500 → 页面照常渲染 3 张云端订单（不会再出现外部那一行）',
    s.rows === 3 && s.orderNos.indexOf('EXT-20261028-180013-8') < 0, JSON.stringify(s.orderNos));
  chk('⑥2 外部端点 500 → 没有空白块/错误块，KPI 与基线一致（不弹错、不虚增）',
    !s.emptyBlock && s.kpi === base.kpi && !s.toasts.some((t) => t.indexOf('外部') >= 0),
    JSON.stringify({ empty: s.emptyBlock, kpi: [base.kpi, s.kpi], toasts: s.toasts }));
  chk('⑥3 外部端点 500 → 无页面 JS 异常（失败被静默吞掉）', pageErrors.length === 0, pageErrors.join(' | '));

  /* ══ D 外部端点挂住（永不响应）—— ⑥ 的强版本 ═══════════════════════════ */
  EXT_MODE = 'hang';
  await openCloud('', 3);
  s = await snap();
  chk('⑥4 外部端点挂住 → 云端订单照样立刻渲染出来（外部请求永不阻塞明细）',
    s.rows === 3 && !s.emptyBlock && s.kpi === base.kpi,
    JSON.stringify({ rows: s.rows, empty: s.emptyBlock, kpi: [base.kpi, s.kpi] }));
  chk('⑥5 外部端点挂住 → 页面无 JS 异常（挂住的请求只是留在 pending）',
    pageErrors.length === 0, pageErrors.join(' | '));

  /* ══ E 账本没记设备所有者 → 用户列「未绑定」 ═════════════════════════════ */
  EXT_MODE = 'ok_unbound';
  await openCloud('', 4);
  s = await snap();
  chk('E1 没有 owner_name 的外部记录 → 用户列显示「未绑定」（不是「本地任务」）',
    s.rows === 4 && s.users.some((u) => u === '未绑定'), JSON.stringify(s.users));
  chk('E1b ★ 更旧的外部行按时间插到末尾（不是永远插到最前），且既有 3 行顺序不变',
    s.rows === 4 && s.orderNos[3] === 'EXT-20261001-091502-9' &&
    JSON.stringify(s.orderNos.slice(0, 3)) === JSON.stringify(['HN20261018-0001', 'HN20261018-0002', 'HN20261018-0003']) &&
    s.users[3] === '未绑定',
    JSON.stringify([s.orderNos, s.users]));
  chk('E2 该记录的金额照后端给的算（¥0.30），KPI 比基线多 30 分',
    s.kpi - base.kpi === 30 && s.amounts.some((a) => a === '¥0.30'),
    JSON.stringify({ kpi: [base.kpi, s.kpi], amounts: s.amounts }));

  /* ══ F 本地取数路径（?mode=local）同样合并外部订单 ═══════════════════════ */
  EXT_MODE = 'ok';
  await openCloud('local', 3);
  s = await snap();
  chk('F1 本地模式（/api/local/orders）也把外部订单并进订单明细（2 张本地 + 1 张外部 = 3 行）',
    s.rows === 3 && s.orderNos.indexOf('EXT-20261028-180013-8') >= 0, JSON.stringify(s.orderNos));
  chk('F2 本地模式下外部行同样显示「外部监视」+「🤝接单」，且没有任何订正入口',
    s.srcTags.some((t) => t.txt === '外部监视') &&
    (await page.evaluate(() => {
      const r = document.querySelector('tr.order-row[data-order="extrow-ext-8-2026-10-28-18-00-13"]');
      return !!r && r.querySelectorAll('td:nth-child(5) .st-edit').length === 0 &&
             r.querySelectorAll('td:nth-child(10) .st-edit').length === 0 &&
             (r.querySelector('td:nth-child(10) .type-cell .tag') || {}).textContent === '🤝接单';
    })),
    JSON.stringify(s.srcTags.map((t) => t.txt)));

  chk('全程无外部网络请求（无 CDN / 无生产接口）', externalReq.length === 0, externalReq.slice(0, 3).join(' | '));
  chk('全程无页面 JS 异常', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
  console.log('截图已存 → ' + path.relative(ROOT, OUT_DIR));
} catch (e) {
  caught = e;
  chk('断言主流程未抛异常（崩溃 = 页面/功能整体缺失或选择器失效）', false,
    String((e && e.stack) || e).split('\n').slice(0, 4).join(' | '));
} finally {
  await browser.close();
  server.close();
}

console.log('\n' + '='.repeat(52));
console.log('已跑断言 ' + total + ' 项，失败 ' + bad + ' 项');
if (caught) console.log('异常终止：' + String((caught && caught.stack) || caught).split('\n')[0]);
console.log(bad ? '外部监视订单明细验收：存在失败 ❌ (' + bad + ')' : '外部监视订单明细验收：全部通过 ✅');
process.exit(bad ? 1 : 0);
