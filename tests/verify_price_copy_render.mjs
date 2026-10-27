/**
 * 「合计价格 → 复制气泡（Popconfirm）」渲染验收（Android APP / Capacitor WebView 那一半）
 *
 * 为什么需要它：tests/verify_price_copy_popconfirm.js 只证明"文本/接线对"，
 * 证明不了"气泡真的出现在价格上方、没被卡片 overflow 裁掉、复制按钮真的写进剪贴板"。
 * 本脚本用 Playwright + 真实 www/ 源码 + 桩后端渲染「我」页，做真实点击与几何断言。
 *
 * 判据：
 *   ① 点「¥xx.xx」→ 气泡出现，且它挂在 body 下（不在 .order-card-detail 里，不会被 overflow 裁）
 *   ② 气泡底边在价格顶边上方 12px（Popconfirm 语义：锚在触发元素上方）
 *   ③ 点价格不会把订单卡片收起（stopPropagation：详情仍展开）
 *   ④ 「复制价格」→ 剪贴板 = 订单号 + 空格 + ¥金额；气泡收起
 *   ⑤ 「复制详细价格」→ 剪贴板含逐文件小计与合计；气泡收起
 *   ⑥ 点 mask（气泡外）→ 收起
 *   ⑦ 上方放不下 → 翻到价格下方（.price-pop-below）
 *   ⑧ 深色模式下气泡仍有背景色（不是透明）
 *
 * 截图输出到 截图展示/价格复制气泡/。
 *
 * 用法: node tests/verify_price_copy_render.mjs     （exit 0 = 全部通过）
 *      run_all.py 默认跳过（需 Playwright / 真实浏览器），--all 或直接 node 可跑。
 */
import { createRequire } from 'node:module';
import http from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const REPO = 'D:/打印机项目';
const APP_DIR = `${REPO}/mobile_apps/android_app/www`;
const SHOTS = `${REPO}/截图展示/价格复制气泡`;
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.json': 'application/json',
  '.jpg': 'image/jpeg',
};

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}

/* ── 桩数据（形状对齐 /api/orders，价格用订单已定稿的实收值） ─────────── */
const ORDER = {
  id: 2001,
  order_number: 'HN20261025-0013',
  status: 'sent',
  source: 'wechat',
  file_summary: '示波器的使用.pdf +2 个文件',
  total_pages: 18,
  total_copies: 6,
  total_sheets: 10,
  created_at: '2026-10-25 22:56:02',
  print_started_at: '2026-10-26 00:28:13',
  received_label: '闫承翰的设备（LAPTOP-CDEPTI74）',
  remark: '崔清阳',
  is_admin_print: 0,
  urgency: '低',
  urgency_price: 0,
  cover_page: 0,
  cover_page_price: 0,
  delivery_enabled: 0,
  delivery_location: '1号楼北楼',
  delivery_percentage: 0,
  total_price: 3.8,
  files: [
    { id: 1, file_id: 'f1', file_name: '示波器的使用.pdf', original_name: '示波器的使用.pdf', copies: 2, page_count: 6, duplex: 'on', page_range: '', status: 'sent', total_price: 2.4, total_sheets: 6 },
    { id: 2, file_id: 'f2', file_name: '数据结构-第3次.docx', original_name: '数据结构-第3次.docx', copies: 2, page_count: 2, duplex: 'on', page_range: '', status: 'sent', total_price: 0.8, total_sheets: 2 },
    { id: 3, file_id: 'f3', file_name: '示波器数据表格.pdf', original_name: '示波器数据表格.pdf', copies: 2, page_count: 1, duplex: 'off', page_range: '', status: 'sent', total_price: 0.6, total_sheets: 2 },
  ],
};

function apiFor(rawUrl) {
  const [u] = rawUrl.split('?');
  if (u === '/api/me') {
    return {
      success: true, role: 'user', is_super_admin: false, openid: 'dev_test',
      nickname: '测试用户', license_info: null,
    };
  }
  if (u === '/api/profile') return { success: true, nickname: '测试用户', avatar_url: '' };
  if (u === '/api/orders') return { success: true, total: 1, orders: [JSON.parse(JSON.stringify(ORDER))] };
  if (u === '/api/pricing') {
    return {
      success: true,
      pricing: {
        simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1,
        delivery_locations: ['1号楼北楼'], delivery_percentages: { '1号楼北楼': 10 },
        urgency_levels: ['低', '高'], urgency_prices: { '低': 0, '高': 0.3 },
      },
    };
  }
  if (u === '/api/license/active') return { success: true, keys: [] };
  if (u === '/api/bind/devices') return { success: true, devices: [] };
  if (u === '/api/claiming_devices') return { success: true, devices: [], hidden_test_devices: 0 };
  return { success: true, orders: [], devices: [], files: [], users: [], admins: [], keys: [], total: 0, count: 0 };
}

const server = http.createServer(async (req, res) => {
  const url = req.url.split('?')[0];
  if (url.startsWith('/api/')) {
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify(apiFor(url)));
    return;
  }
  const rel = normalize(url === '/' ? '/index.html' : url).replace(/^[\\/]+/, '');
  try {
    const body = await readFile(join(APP_DIR, rel));
    res.writeHead(200, { 'Content-Type': MIME[extname(rel)] || 'application/octet-stream' });
    res.end(body);
  } catch (e) {
    res.writeHead(404);
    res.end('not found');
  }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const port = server.address().port;
const BASE = `http://127.0.0.1:${port}`;
await mkdir(SHOTS, { recursive: true });

const browser = await chromium.launch({ executablePath: CHROME });
const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: BASE });
const page = await context.newPage();
const foreign = [];
page.on('request', (r) => {
  const u = r.url();
  if (!u.startsWith(BASE) && !u.startsWith('data:') && !u.startsWith('blob:')) foreign.push(u);
});
page.on('pageerror', (e) => console.log('  [pageerror] ' + e.message));
page.on('console', (m) => { if (m.type() === 'error') console.log('  [console.error] ' + m.text()); });

await page.addInitScript((base) => {
  try {
    window.__HN_TEST_BASE_URL__ = base;
    localStorage.setItem('hn_token', 'stub-token');
    localStorage.setItem('hn_theme_mode', 'light');
    localStorage.setItem('hn_nickname', '测试用户');
  } catch (e) { /* 忽略 */ }
}, BASE);
await page.route('**/api/**', (route) => {
  if (route.request().url().startsWith(BASE)) return route.continue();
  return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: '{}' });
});

await page.goto(`${BASE}/index.html`, { waitUntil: 'load' });
await page.waitForFunction(() => typeof switchTab === 'function', null, { timeout: 15000 });
await page.evaluate(() => switchTab('me'));
await page.waitForFunction(
  () => document.getElementById('page-me') && document.getElementById('page-me').style.display !== 'none'
    && document.querySelectorAll('#orderList .order-card').length > 0,
  null, { timeout: 15000 });
await page.addStyleTag({ content: '*,*::before,*::after{animation:none!important;transition:none!important}' });
// 等首屏的 loadOrders 全部落定（loadOrders 会重置 expandedOrders，正好在点击时到达会把刚展开的卡片又收起）
await page.waitForTimeout(1500);

// 展开订单卡片（DOM click 走既有委托；卡片内部有绝对居中元素，真实鼠标点会被子元素截走）
for (let i = 0; i < 3; i++) {
  await page.evaluate(() => document.querySelector('#orderList .order-card').click());
  try {
    await page.waitForFunction(
      () => document.querySelectorAll('#orderList .order-card-detail.detail-expanded').length === 1,
      null, { timeout: 1500 });
    break;
  } catch (e) { /* 被并发重渲染收起了，再点一次 */ }
}
await page.waitForTimeout(350);
check('订单详情已展开（准备好点价格）',
      await page.locator('#orderList .order-card-detail.detail-expanded').count() === 1);

/* ── ① 点价格 → 气泡出现，挂在 body 下 ─────────────────────────────── */
await page.locator('#orderList .detail-price-tap').first().click();
await page.waitForSelector('.price-pop', { state: 'visible', timeout: 5000 });

const geo = await page.evaluate(() => {
  const pop = document.querySelector('.price-pop');
  const priceEs = document.querySelectorAll('#orderList .detail-price-tap');
  const price = priceEs[0].getBoundingClientRect();
  const r = pop.getBoundingClientRect();
  const caret = document.querySelector('.price-pop-caret').getBoundingClientRect();
  return {
    inBody: pop.closest('#orderList') === null && pop.closest('.order-card-detail') === null,
    parentIsMask: pop.parentElement && pop.parentElement.className.indexOf('price-pop-mask') >= 0,
    position: getComputedStyle(pop).position,
    popTop: r.top, popBottom: r.bottom, popHeight: r.height,
    popLeft: r.left, popRight: r.right, popWidth: r.width,
    priceTop: price.top, priceBottom: price.bottom,
    priceCenterX: price.left + price.width / 2,
    caretCenterX: caret.left + caret.width / 2,
    winW: window.innerWidth, winH: window.innerHeight,
    detailStillExpanded: !!document.querySelector('#orderList .order-card-detail.detail-expanded'),
  };
});
check('① 气泡挂在 body 下（不在订单卡片里，不会被 overflow:hidden 裁）',
      geo.inBody && geo.parentIsMask && geo.position === 'fixed');
check('① 气泡完整落在视口内（水平不越界、垂直不越界）',
      geo.popLeft >= 0 && geo.popRight <= geo.winW && geo.popTop >= 0 && geo.popBottom <= geo.winH,
      `pop=${Math.round(geo.popLeft)}..${Math.round(geo.popRight)} / ${Math.round(geo.popTop)}..${Math.round(geo.popBottom)} view=${geo.winW}x${geo.winH}`);
check('② 气泡底边锚在价格顶边上方 12px',
      Math.abs(geo.popBottom - (geo.priceTop - 12)) <= 1.5,
      `pop.bottom=${geo.popBottom.toFixed(1)} price.top=${geo.priceTop.toFixed(1)}`);
check('② 箭头指向价格中心（±40px 内）',
      Math.abs(geo.caretCenterX - geo.priceCenterX) <= 40,
      `caret=${geo.caretCenterX.toFixed(1)} price=${geo.priceCenterX.toFixed(1)}`);
check('③ 点价格没有把卡片收起（详情仍展开）', geo.detailStillExpanded === true);

await page.screenshot({ path: `${SHOTS}/气泡_位于价格上方.png` });

/* ── ④ 复制价格 ───────────────────────────────────────────────────── */
await page.locator('[data-price-act="simple"]').click();
await page.waitForTimeout(100);
check('④ 「复制价格」后气泡收起', await page.locator('.price-pop').count() === 0);
const clip1 = await page.evaluate(() => navigator.clipboard.readText());
check('④ 剪贴板 = 订单号 + 空格 + ¥金额（与小程序同格式）',
      clip1 === 'HN20261025-0013 ¥3.80', JSON.stringify(clip1));

/* ── ⑤ 复制详细价格 ───────────────────────────────────────────────── */
await page.locator('#orderList .detail-price-tap').first().click();
await page.waitForSelector('.price-pop', { state: 'visible' });
await page.locator('[data-price-act="detail"]').click();
await page.waitForTimeout(100);
const clip2 = await page.evaluate(() => navigator.clipboard.readText());
check('⑤ 「复制详细价格」后气泡收起', await page.locator('.price-pop').count() === 0);
check('⑤ 剪贴板含逐文件小计（0.80 等实收值）与订单号',
      clip2.indexOf('计费明细') === 0 && clip2.indexOf('HN20261025-0013') > 0
      && clip2.indexOf('2 份 × 6 页 | 双面 | 全部页 | ¥2.40') > 0,
      JSON.stringify(clip2.split('\n').slice(0, 4)));
check('⑤ 剪贴板合计给出算式（2.40+0.80+0.60=￥3.80，金额=订单实收）',
      clip2.indexOf('💰合计: 2.40+0.80+0.60=￥3.80') > 0,
      clip2.split('\n').slice(-1)[0]);

/* ── ⑥ 点 mask 收起 ───────────────────────────────────────────────── */
await page.locator('#orderList .detail-price-tap').first().click();
await page.waitForSelector('.price-pop', { state: 'visible' });
await page.locator('.price-pop-mask').click({ position: { x: 8, y: 8 } });
await page.waitForTimeout(100);
check('⑥ 点气泡外（mask）→ 收起', await page.locator('.price-pop').count() === 0);

/* ── ⑦ 上方放不下 → 翻到下方（真实几何：把价格滚到视口顶部附近） ──── */
await page.evaluate(() => {
  // 直接把订单卡片滚到视口顶端：用页面自己的滚动引擎，保证几何真实
  const card = document.querySelector('#orderList .order-card');
  const y = card.getBoundingClientRect().top;
  if (window.scrollEngines && scrollEngines.me) scrollEngines.me.scrollTo(y - 40, 0);
  else window.scrollTo(0, Math.max(0, window.scrollY + y - 40));
});
await page.waitForTimeout(200);
const flipped = await page.evaluate(() => {
  const price = document.querySelector('#orderList .detail-price-tap');
  const pr = price.getBoundingClientRect();
  // 造一个贴近顶部的锚点（与真实点击走同一个 openPricePop）
  const fakeAnchor = { getBoundingClientRect: () => ({ top: 24, bottom: 48, left: pr.right - 120, right: pr.right, width: 120, height: 24 }) };
  openPricePop(fakeAnchor, 2001);
  const pop = document.querySelector('.price-pop');
  const r = pop.getBoundingClientRect();
  return { below: pop.classList.contains('price-pop-below'), top: r.top, anchorBottom: 48, h: r.height,
           priceTop: pr.top };
});
check('⑦ 上方放不下 → 气泡翻到锚点下方（.price-pop-below，top = 锚点底 + 12）',
      flipped.below === true && Math.abs(flipped.top - (flipped.anchorBottom + 12)) <= 1.5,
      `below=${flipped.below} top=${flipped.top} h=${flipped.h}`);
await page.screenshot({ path: `${SHOTS}/气泡_翻到下方.png` });
await page.evaluate(() => closePricePop());

/* ── ⑧ 深色模式：气泡不透明、可读 ─────────────────────────────────── */
await page.evaluate(() => { applyTheme('dark'); });
await page.waitForTimeout(200);
await page.locator('#orderList .detail-price-tap').first().click();
await page.waitForSelector('.price-pop', { state: 'visible' });
const dark = await page.evaluate(() => {
  const pop = document.querySelector('.price-pop');
  const bg = getComputedStyle(pop).backgroundColor;
  return { bg, body: document.body.classList.contains('theme-dark') };
});
const rgb = (dark.bg.match(/\d+(\.\d+)?/g) || []).map(Number);
const alpha = rgb.length > 3 ? rgb[3] : 1;
check('⑧ 深色模式下气泡有不透明背景色（不是透明）',
      dark.body && (alpha === 1) && rgb.slice(0, 3).some((v) => v > 0),
      `bg=${dark.bg}`);
await page.screenshot({ path: `${SHOTS}/气泡_深色模式.png` });

/* ── 全程无外部请求 ───────────────────────────────────────────────── */
check('渲染全程无外部请求（未打到生产接口）', foreign.length === 0, foreign.slice(0, 2).join(', '));

console.log(`\n截图输出：${SHOTS}`);
await browser.close();
server.close();
if (failures) {
  console.log(`\n${failures} 项未通过`);
  process.exit(1);
}
console.log('\n全部通过');
