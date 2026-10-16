/* 验收 · 收支清算「趋势下钻 → 单日逐时」真实浏览器端到端（v6.8）
 *
 * 为什么要有这一份：tests/verify_trend_hourly.js 用 Node + VM 桩验了算法与口径状态机，
 * 但「点柱子能不能真的点到、点了之后图是不是真的变成 24 根小时柱」只有真浏览器 + 真 Chart.js 才算数。
 *
 * 做法：起一个本地 HTTP 服务（静态文件来自 local_print_tool/finance/，/api/** 全部打桩），
 * 用 Playwright 打开 settlement.html → 切到「云端数据」→ **用鼠标点真实柱子的像素位置** →
 * 断言时间口径切到那天、图表对象变成 24 个 'HH:00' 标签、收益合计与日柱一致 →
 * 再点「← 返回区间」/下拉选「今天」，各断言一次。全程只连 127.0.0.1（脚本最后一条断言兜住）。
 *
 * 产物：截图存 截图展示/趋势下钻逐时/（月视图日柱 / 点击后逐时 / 口径栏）。
 * 用法：node tests/verify_trend_hourly_render.mjs [settlement.html 路径]
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
const OUT_DIR = path.join(ROOT, '截图展示', '趋势下钻逐时');

let bad = 0;
function chk(name, cond, detail) {
  if (!cond) bad++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (!cond && detail ? `  -- ${detail}` : ''));
}

/* ── 夹具：全部围绕「今天」生成，测试与运行日期无关 ───────────────────────── */
const pad = (n) => String(n).padStart(2, '0');
const ymd = (d) => d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
const now = new Date();
const TODAY = ymd(now);
const MONTH = TODAY.slice(0, 7);
const yesterday = new Date(now); yesterday.setDate(yesterday.getDate() - 1);
/* 同一自然月内的昨天（跨月就没有「过去某天」可点，例如每月 1 号跑） */
const PREV_DAY = (yesterday.getFullYear() === now.getFullYear() && yesterday.getMonth() === now.getMonth())
  ? ymd(yesterday) : null;

function ord(createdAt, cents, pages, duplex, status, isSelf) {
  return {
    order_id: createdAt, order_number: 'HN' + createdAt.replace(/[-: ]/g, ''), source: 'local',
    openid: 'local', nickname: '', owner_name: '成员A', status, total_price: cents / 100,
    created_at: createdAt, is_admin_print: isSelf ? 1 : 0,
    files_count: 1, total_page_count: pages,
    files: [{ id: 'f1', file_name: 'a.pdf', copies: 1, page_count: pages, duplex, page_range: '',
              total_price: cents / 100, status, is_free: false }],
  };
}
/* 今天：00:05 ¥3.00 / 12:40 ¥5.00（文件 4.00 + 附加 1.00）/ 17:05 ¥1.00；昨天（同月时）另有一单 ¥7.00 */
const ORDERS = [
  ord(TODAY + ' 00:05:00', 300, 2, 'off', 'sent', false),
  ord(TODAY + ' 12:40:00', 500, 5, 'on', 'queued', false),
  ord(TODAY + ' 17:05:00', 100, 1, 'off', 'sent', false),
];
if (PREV_DAY) ORDERS.push(ord(PREV_DAY + ' 12:00:00', 700, 1, 'off', 'sent', false));
const TODAY_TOTAL = 9.00;                       // 3.00 + 5.00 + 1.00（均非自打）
const ordersIn = (start, end) => ORDERS.filter((o) => o.created_at.slice(0, 10) >= start && o.created_at.slice(0, 10) <= end);

const STORE = {
  config: { investRate: 0.05, scoreCap: 15, passwords: {}, members: [{ id: 'ma', name: '成员A', color: '#4472C4' }] },
  records: { expenses: [], income: [], scores: [], transfers: [] },
  notes: {}, meta: { version: 1 },
};

/* ── 打桩服务器（静态文件 + /api/**，绝不发出网请求） ───────────────────────── */
const json = (res, obj, code = 200) => {
  const body = JSON.stringify(obj);
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(body);
};
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  const p = decodeURIComponent(u.pathname);
  if (p.startsWith('/api/')) {
    if (p === '/api/health') return json(res, { success: true, cloud: true });
    if (p === '/api/proxy/finance/config') {
      if (req.method === 'POST') return json(res, { success: true, server_version: 2 });
      return json(res, { success: true, data: STORE });
    }
    if (p === '/api/proxy/pricing') {
      return json(res, { success: true, data: { simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1, delivery_percentages: {}, urgency_prices: {} } });
    }
    if (p === '/api/proxy/admin/statistics/months') {
      return json(res, { success: true, data: { months: [{ month: MONTH }, { month: '2026-09' }] } });
    }
    if (p === '/api/proxy/admin/statistics/revenue') {
      const start = u.searchParams.get('start_date') || '0000-01-01';
      const end = u.searchParams.get('end_date') || '9999-12-31';
      return json(res, { success: true, data: { period: { start, end }, revenue_summary: {}, by_user: [], orders: ordersIn(start, end) } });
    }
    if (p === '/api/local/data') return json(res, { success: true, data: null });
    if (p === '/api/local/bindings') return json(res, { success: true, data: {} });
    return json(res, { success: true, data: null });
  }
  const file = p === '/' ? 'settlement.html' : p.replace(/^\//, '');
  const fp = file === 'settlement.html' ? HTML_PATH : path.join(FINANCE, file);
  if (!fp.startsWith(path.dirname(HTML_PATH)) && !fp.startsWith(FINANCE)) { res.writeHead(404); return res.end('not found'); }
  if (!fs.existsSync(fp) || fs.statSync(fp).isDirectory()) { res.writeHead(404); return res.end('not found'); }
  const ext = path.extname(fp);
  const ct = ext === '.html' ? 'text/html; charset=utf-8' : ext === '.js' ? 'application/javascript; charset=utf-8' : 'application/octet-stream';
  res.writeHead(200, { 'Content-Type': ct });
  res.end(fs.readFileSync(fp));
});

const port = await new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
const BASE = `http://127.0.0.1:${port}`;
fs.mkdirSync(OUT_DIR, { recursive: true });
console.log(`本地服务 ${BASE}（静态 ${FINANCE}）`);
console.log(`夹具：今天 ${TODAY}（3 单 ¥${TODAY_TOTAL.toFixed(2)}，含 00/12/17 三个小时）` + (PREV_DAY ? `，昨天 ${PREV_DAY}（1 单 ¥7.00）` : '，本月还没有「昨天」可点'));

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
/* 截图前等柱体入场动画（Chart.js 默认 1200ms）放完，否则截到的是长到一半的柱子 */
const settle = () => page.waitForTimeout(1400);
const pageErrors = [];
const external = [];
page.on('pageerror', (e) => pageErrors.push(process.env.PW_DEBUG_ERRORS ? String(e.stack || e.message) : e.message));
page.on('request', (r) => { if (!r.url().startsWith(BASE) && !r.url().startsWith('data:')) external.push(r.url()); });

try {
  await page.goto(BASE + '/settlement.html', { waitUntil: 'domcontentloaded' });
  chk('页面脚本加载无语法错误', pageErrors.length === 0, pageErrors.join(' | '));
  chk('Chart.js 由本地文件加载（不依赖 CDN）',
    await page.evaluate(() => typeof window.Chart === 'function'));

  /* 切到「云端数据」标签页（真实点击，走 switchTab → 拉口径数据） */
  await page.click('#tabNav button[data-tab="cloud"]');
  await page.waitForFunction(() => window._cloudData && window._cloudCharts && window._cloudCharts['cloud-chart-trend'],
    null, { timeout: 20000 });
  console.log('云端数据页已渲染，趋势图已创建');

  const month = await page.evaluate(() => {
    const c = window._cloudCharts['cloud-chart-trend'];
    return { type: c.config.type || c.data.datasets[0].type, labels: c.data.labels, data: c.data.datasets[0].data, gran: _trendViewState().gran };
  });
  chk('月口径：日粒度柱状图（每天一柱）', month.type === 'bar' && month.gran === 'day', JSON.stringify(month.gran));
  chk(`月视图里今天那根柱 = ¥${TODAY_TOTAL.toFixed(2)}（与后端/夹具一致）`,
    month.data[month.labels.indexOf((+TODAY.slice(5, 7)) + '/' + (+TODAY.slice(8, 10)))] === TODAY_TOTAL,
    JSON.stringify(month.labels.map((l, i) => l + '=' + month.data[i])));
  chk('标题提示可点柱下钻', await page.evaluate(() => document.querySelector('#cloud-trend .hint').textContent.includes('点击柱状图可下钻')));

  const todayIdx = month.labels.indexOf((+TODAY.slice(5, 7)) + '/' + (+TODAY.slice(8, 10)));
  /* 时间范围下拉的选项（原生 select 展开态截不到，改成读 options 文本 + 断言） */
  const opts = await page.evaluate(() => [...document.getElementById('scopeTimeSel').options].map((o) => o.text));
  chk('时间范围选项含 今天 / 近7天 / 近30天 / 近90天 / 近1年 / 自定义日期…',
    ['今天', '近7天', '近30天', '近90天', '近1年'].every((t) => opts.includes(t)) && opts.some((t) => t.indexOf('自定义') === 0),
    opts.join(' , '));
  console.log('  时间范围选项：' + opts.join(' / '));
  await page.locator('#cloud-trend').scrollIntoViewIfNeeded();
  await settle();
  await page.locator('#cloud-trend').screenshot({ path: path.join(OUT_DIR, '01-月视图-逐日柱.png') });
  await page.evaluate(() => window.scrollTo(0, 0));
  await settle();
  await page.screenshot({ path: path.join(OUT_DIR, '03-口径栏与KPI.png') });

  /* ── 真鼠标点「今天」那根柱的中心 ── */
  await page.locator('#cloud-trend').scrollIntoViewIfNeeded();
  const pos = await page.evaluate((idx) => {
    const c = window._cloudCharts['cloud-chart-trend'];
    const meta = c.getDatasetMeta(0).data[idx];
    const rect = c.canvas.getBoundingClientRect();
    return { x: rect.left + meta.x, y: rect.top + meta.y, label: c.data.labels[idx] };
  }, todayIdx);
  console.log(`点击柱「${pos.label}」像素位置 (${Math.round(pos.x)}, ${Math.round(pos.y)})`);
  await page.mouse.click(Math.round(pos.x), Math.round(pos.y));

  await page.waitForFunction(`(function () {
    if (_scopeTime.mode !== 'day' || _scopeTime.day !== '${TODAY}') return false;
    var c = window._cloudCharts['cloud-chart-trend'];
    return !!c && c.data.labels.length === 24 && c.data.labels[0] === '00:00';
  })()`, null, { timeout: 20000 });
  console.log('下钻成功：口径已切到今天，趋势图已变为 24 小时柱');

  const hour = await page.evaluate(() => {
    const c = window._cloudCharts['cloud-chart-trend'];
    const d = c.data.datasets[0].data;
    const hourRows = [];
    for (let i = 0; i < 24; i++) if (d[i]) hourRows.push(c.data.labels[i] + '=¥' + d[i]);
    return {
      n: c.data.labels.length, first: c.data.labels[0], last: c.data.labels[23],
      sum: Math.round(d.reduce((a, b) => a + b, 0) * 100) / 100, nonZero: hourRows,
      hint: document.querySelector('#cloud-trend .hint').textContent,
      hasBack: !!document.querySelector('#cloud-trend button[onclick="undoTrendDrill()"]'),
      selValue: document.getElementById('scopeTimeSel').value,
      summary: document.querySelector('.scope-summary').textContent,
      /* KPI 数字是 countUp 动画跑出来的，别读瞬时文本；读 data-count（单位：分） */
      kpiCents: Number(document.querySelector('.hero-card .num[data-count]').getAttribute('data-count')),
    };
  });
  chk('柱子标签是 00:00 ~ 23:00（每小时一根，共 24 根）',
    hour.n === 24 && hour.first === '00:00' && hour.last === '23:00', `${hour.n} 根 ${hour.first}~${hour.last}`);
  chk(`逐时合计 = ¥${TODAY_TOTAL.toFixed(2)}（与月视图那根日柱一致）`,
    Math.abs(hour.sum - TODAY_TOTAL) < 1e-9, String(hour.sum));
  chk('00/12/17 三个小时有柱，其余为 0 柱（位置保留）',
    hour.nonZero.length === 3 && /00:00=¥3/.test(hour.nonZero[0]) && /12:00=¥5/.test(hour.nonZero[1]) && /17:00=¥1/.test(hour.nonZero[2]),
    JSON.stringify(hour.nonZero));
  chk('趋势标题写明「逐时（00:00~23:00 每小时一柱）」', hour.hint.includes('逐时（00:00~23:00 每小时一柱）'), hour.hint);
  chk('趋势卡片出现「← 返回区间」按钮', hour.hasBack);
  chk('口径栏选中「今天」、摘要标「（单日 · 逐时）」',
    hour.selValue === 'r:today' && hour.summary.includes(TODAY + '（单日 · 逐时）'),
    hour.selValue + ' / ' + hour.summary);
  chk(`KPI 跟着切到当天（单日口径，¥${TODAY_TOTAL.toFixed(2)}）`, hour.kpiCents === Math.round(TODAY_TOTAL * 100), String(hour.kpiCents));
  await settle();
  await page.locator('#cloud-trend').screenshot({ path: path.join(OUT_DIR, '02-点击后-当天逐时.png') });

  /* ── 返回区间（下钻前的月口径） ── */
  await page.click('#cloud-trend button[onclick="undoTrendDrill()"]');
  await page.waitForFunction(`(function () {
    if (_scopeTime.mode !== 'month') return false;
    var c = window._cloudCharts['cloud-chart-trend'];
    return !!c && c.data.labels.length > 2;
  })()`, null, { timeout: 20000 });
  const back = await page.evaluate(() => ({
    mode: _scopeTime.mode, day: _scopeTime.day, prev: _trendDrillPrev,
    hasBack: !!document.querySelector('#cloud-trend button[onclick="undoTrendDrill()"]'),
    label: _cloudCharts['cloud-chart-trend'].data.labels.length,
  }));
  chk('「← 返回区间」回到月口径，且按钮消失、返回点清空',
    back.mode === 'month' && back.day === '' && back.prev === null && !back.hasBack, JSON.stringify(back));

  /* ── 下拉选「今天」也进逐时；选「近7天」是日柱 ── */
  await page.selectOption('#scopeTimeSel', 'r:today');
  await page.waitForFunction(`(function () {
    var c = window._cloudCharts['cloud-chart-trend'];
    return _scopeTime.mode === 'day' && _scopeTime.day === '${TODAY}' && !!c && c.data.labels.length === 24;
  })()`, null, { timeout: 20000 });
  const today2 = await page.evaluate(() => ({
    n: _cloudCharts['cloud-chart-trend'].data.labels.length,
    sel: document.getElementById('scopeTimeSel').value,
    hint: document.querySelector('#cloud-trend .hint').textContent,
  }));
  chk('时间范围选「今天」→ 直接逐时（24 根柱）', today2.n === 24 && today2.sel === 'r:today', JSON.stringify(today2));
  chk('「今天」口径不显示返回按钮（不是下钻来的，没有返回点）',
    await page.evaluate(() => !document.querySelector('#cloud-trend button[onclick="undoTrendDrill()"]')));

  await page.selectOption('#scopeTimeSel', 'r:7d');
  await page.waitForFunction(`(function () {
    var c = window._cloudCharts['cloud-chart-trend'];
    return _scopeTime.mode === '7d' && !!c && c.data.labels.length === 7;
  })()`, null, { timeout: 20000 });
  const week = await page.evaluate(() => ({
    n: _cloudCharts['cloud-chart-trend'].data.labels.length,
    sel: document.getElementById('scopeTimeSel').value,
    gran: _trendViewState().gran,
  }));
  chk('时间范围选「近7天」→ 7 根日柱（gran=day）', week.n === 7 && week.sel === 'r:7d' && week.gran === 'day', JSON.stringify(week));

  /* ── 过去某天：下钻后下拉显示「单日 YYYY-MM-DD」 ── */
  if (PREV_DAY) {
    await page.selectOption('#scopeTimeSel', 'm:' + MONTH);
    await page.waitForFunction(() => window._cloudData && window._cloudCharts['cloud-chart-trend'], null, { timeout: 20000 });
    const prevIdx = await page.evaluate((lbl) => window._cloudCharts['cloud-chart-trend'].data.labels.indexOf(lbl),
      (+PREV_DAY.slice(5, 7)) + '/' + (+PREV_DAY.slice(8, 10)));
    if (prevIdx >= 0) {
      await page.locator('#cloud-trend').scrollIntoViewIfNeeded();
      const p2 = await page.evaluate((idx) => {
        const c = window._cloudCharts['cloud-chart-trend'];
        const meta = c.getDatasetMeta(0).data[idx];
        const rect = c.canvas.getBoundingClientRect();
        return { x: rect.left + meta.x, y: rect.top + meta.y };
      }, prevIdx);
      await page.mouse.click(Math.round(p2.x), Math.round(p2.y));
      await page.waitForFunction(`(function () {
        var c = window._cloudCharts['cloud-chart-trend'];
        return _scopeTime.mode === 'day' && _scopeTime.day === '${PREV_DAY}' && !!c && c.data.labels.length === 24;
      })()`, null, { timeout: 20000 });
      const pd = await page.evaluate(() => ({
        opts: [...document.getElementById('scopeTimeSel').options].map((o) => o.value + '|' + o.text + (o.selected ? '|SEL' : '')),
        sel: document.getElementById('scopeTimeSel').value,
        sum: Math.round(_cloudCharts['cloud-chart-trend'].data.datasets[0].data.reduce((a, b) => a + b, 0) * 100) / 100,
      }));
      chk(`下钻过去的 ${PREV_DAY} → 下拉出现并选中「单日 ${PREV_DAY}」`,
        pd.sel === 'r:day' && pd.opts.some((o) => o === 'r:day|单日 ' + PREV_DAY + '|SEL'), pd.opts.join(' , '));
      chk('过去某天的逐时合计 = ¥7.00', Math.abs(pd.sum - 7.00) < 1e-9, String(pd.sum));
      await settle();
      await page.locator('#cloud-trend').screenshot({ path: path.join(OUT_DIR, '04-下钻过去某天-逐时.png') });
    } else {
      chk('月视图里能找到昨天那根柱（用于下钻过去某天）', false, '未找到柱标签');
    }
  }

  chk('全程无外部网络请求（无 CDN / 无生产接口）', external.length === 0, external.slice(0, 3).join(' | '));
  chk('全程无页面 JS 异常', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
  console.log(`截图已存 → ${path.relative(ROOT, OUT_DIR)}`);
} finally {
  await browser.close();
  server.close();
}

console.log('\n' + '='.repeat(52));
console.log(bad ? '趋势下钻真实浏览器验收：存在失败 ❌' : '趋势下钻真实浏览器验收：全部通过 ✅');
process.exit(bad ? 1 : 0);
