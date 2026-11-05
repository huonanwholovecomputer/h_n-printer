/**
 * 「订单详情 → 文件列表」内部滚动的**渲染验收**（Android APP / Capacitor WebView 那一半）
 *
 * 为什么需要它（2026-11-04 现场截图）：用户的一个任务里有 10+ 个文件时，订单卡片详情区
 * 只显示到一半就断了 —— 详情容器是 `max-height + overflow:hidden`（展开靠 max-height 过渡），
 * 文件列表是里面唯一不受限生长的部分，文件一多就把容器顶穿，后半截文件与下面的
 * 附加服务/合计价格**永远看不到**（不是「滑不动」，是直接被裁掉）。
 *
 * 修法：文件列表自己进一个 `max-height` 的滚动框（小程序 <scroll-view scroll-y> / APP 定高 div），
 * 详情容器的高度就与文件数无关了。
 *
 * 本脚本用 Playwright + 真实 www/ 源码 + 桩后端渲染「我」页，做真实几何断言：
 *   ① 20 个文件的订单：文件列表框真的能滚（scrollHeight > clientHeight）
 *   ② 滚到底 → 最后一个文件行完整落在框内（不是被裁一半）
 *   ③ 详情卡片自己不裁内容（scrollHeight ≤ clientHeight）——这是"看不见后半截"的判据本身
 *   ④ 文件多时才有「列表可上下滑动」提示（2 个文件的订单不出现）
 *   ⑤ 2 个文件的订单：列表框不留白（高度随内容，不是固定 480rpx）
 *   ⑥ 变异自证：把滚动框的 max-height 去掉（等于改回旧版结构）→ ③ 必红
 *
 * 截图输出到 截图展示/订单文件列表滚动/。
 *
 * 用法: node tests/verify_order_files_scroll.mjs        （exit 0 = 全部通过）
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
const SHOTS = `${REPO}/截图展示/订单文件列表滚动`;
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.json': 'application/json; charset=utf-8',
  '.jpg': 'image/jpeg',
};

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}

/* ── 夹具 ────────────────────────────────────────────────────────────
   一张 20 文件的订单（复现现场：一个「国家助学金申请书」任务，一人一个 docx），
   一张 2 文件的订单（验证短列表不留白、不出提示）。 */
function mkFile(i) {
  return {
    id: i, file_id: 'f' + i,
    file_name: `学生${String(i).padStart(2, '0')}_国家助学金申请书.docx`,
    original_name: `学生${String(i).padStart(2, '0')}_国家助学金申请书.docx`,
    copies: 1, page_count: 2, duplex: 'on', page_range: '', status: 'sent',
    total_price: 0.8, total_sheets: 1, size: 240000, file_type: 'docx',
  };
}
const MANY = {
  id: 9001, order_number: 'HN20261104-0040', status: 'sent', source: 'wechat',
  file_summary: '学生01_国家助学金申请书.docx +19 个文件',
  total_pages: 40, total_copies: 20, total_sheets: 20,
  created_at: '2026-11-04 21:10:02', print_started_at: '2026-11-04 21:10:30',
  received_label: '霍楠的设备（R7000-AHP10-168dbdc3d1）',
  remark: '', is_admin_print: 0, urgency: '低', urgency_price: 0,
  cover_page: 0, cover_page_price: 0, delivery_enabled: 0, delivery_location: '1号楼北楼',
  delivery_percentage: 0, total_price: 16.0,
  files: Array.from({ length: 20 }, (_, i) => mkFile(i + 1)),
};
const FEW = {
  ...JSON.parse(JSON.stringify(MANY)),
  id: 9002, order_number: 'HN20261104-0041', file_summary: '学生21.docx +1 个文件',
  total_pages: 4, total_copies: 2, total_sheets: 2, total_price: 1.6,
  files: [mkFile(21), mkFile(22)],
};

function apiFor(rawUrl) {
  const [u] = rawUrl.split('?');
  if (u === '/api/me') {
    return { success: true, role: 'user', is_super_admin: false, openid: 'dev_test', nickname: '测试用户', license_info: null };
  }
  if (u === '/api/profile') return { success: true, nickname: '测试用户', avatar_url: '' };
  if (u === '/api/orders') return { success: true, total: 2, orders: [MANY, FEW] };
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
const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, hasTouch: true });
const page = await context.newPage();
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
    && document.querySelectorAll('#orderList .order-card').length === 2,
  null, { timeout: 15000 });
// 关掉过渡动画：max-height 过渡期间量到的几何是中间态
await page.addStyleTag({ content: '*,*::before,*::after{animation:none!important;transition:none!important}' });
await page.waitForTimeout(1200);

async function expandCard(index) {
  for (let i = 0; i < 3; i++) {
    await page.evaluate((idx) => document.querySelectorAll('#orderList .order-card')[idx].click(), index);
    try {
      await page.waitForFunction(
        (idx) => {
          const cards = document.querySelectorAll('#orderList .order-card');
          const d = cards[idx] && cards[idx].querySelector('.order-card-detail');
          return !!d && d.classList.contains('detail-expanded');
        }, index, { timeout: 1500 });
      return true;
    } catch (e) { /* 被并发重渲染收起了，再点一次 */ }
  }
  return false;
}

/* ── 展开 20 文件的那张卡 ─────────────────────────────────────────── */
check('订单卡片已渲染（2 张）', await page.locator('#orderList .order-card').count() === 2);
check('① 20 文件订单已展开', await expandCard(0));
check('① 文件列表有独立滚动框 .detail-files-scroll',
      await page.locator('#orderList .order-card').first().locator('.detail-files-scroll').count() === 1);

const m1 = await page.evaluate(() => {
  const card = document.querySelectorAll('#orderList .order-card')[0];
  const detail = card.querySelector('.order-card-detail');
  const box = card.querySelector('.detail-files-scroll');
  const rows = box.querySelectorAll('.detail-file-row');
  return {
    boxClient: box.clientHeight, boxScroll: box.scrollHeight,
    rows: rows.length,
    detailClient: detail.clientHeight, detailScroll: detail.scrollHeight,
    detailMaxH: getComputedStyle(detail).maxHeight,
    cardBottom: card.getBoundingClientRect().bottom,
    hint: (card.querySelector('.detail-files-hint') || {}).textContent || '',
  };
});
check('① 文件列表真的能滚（scrollHeight 明显大于可视高度）',
      m1.boxScroll > m1.boxClient + 5,
      `client=${m1.boxClient} scroll=${m1.boxScroll} rows=${m1.rows}`);
check('③ 详情卡片自己不裁内容（scrollHeight ≤ clientHeight = 没有 overflow 裁切）',
      m1.detailScroll <= m1.detailClient + 1,
      `detail client=${m1.detailClient} scroll=${m1.detailScroll} max-height=${m1.detailMaxH}`);
await page.screenshot({ path: `${SHOTS}/20文件_滚动框内部滚动.png` });

/* ── ② 滚到底 → 最后一文件行完整可见 ─────────────────────────────── */
const m2 = await page.evaluate(() => {
  const card = document.querySelectorAll('#orderList .order-card')[0];
  const box = card.querySelector('.detail-files-scroll');
  box.scrollTop = box.scrollHeight;
  const rows = box.querySelectorAll('.detail-file-row');
  const last = rows[rows.length - 1];
  const b = box.getBoundingClientRect();
  const r = last.getBoundingClientRect();
  return {
    scrollTop: box.scrollTop, boxTop: b.top, boxBottom: b.bottom,
    lastTop: r.top, lastBottom: r.bottom, lastName: (rows[rows.length - 1].textContent || '').trim().slice(0, 24),
  };
});
check('② 列表滚到底（scrollTop > 0，确实滚动过）', m2.scrollTop > 0, `scrollTop=${Math.round(m2.scrollTop)}`);
check('② 最后一文件行完整落在滚动框内（top/bottom 都在框内 ±1px）',
      m2.lastTop >= m2.boxTop - 1 && m2.lastBottom <= m2.boxBottom + 1,
      `last=${m2.lastTop.toFixed(1)}..${m2.lastBottom.toFixed(1)} box=${m2.boxTop.toFixed(1)}..${m2.boxBottom.toFixed(1)}`);
await page.screenshot({ path: `${SHOTS}/20文件_滚到底最后一行.png` });

/* ── ②b 真实手势：手指在列表内上滑 → 列表自己滚，页面不动 ──────────
   APP 的 FlingEngine 会 preventDefault 接管手势；只有它认出 .detail-files-scroll
   是内层可滚容器（app.js onTouchStart 的 closest 选择器 + 双向接力），
   原生滚动才不会被吞掉。这里用真实 touch 事件走一遍。 */
await page.evaluate(() => {
  document.querySelectorAll('#orderList .order-card')[0].querySelector('.detail-files-scroll').scrollTop = 0;
});
const geoBox = await page.evaluate(() => {
  const b = document.querySelectorAll('#orderList .order-card')[0].querySelector('.detail-files-scroll').getBoundingClientRect();
  return { cx: b.left + b.width / 2, cy: b.top + b.height / 2, top: b.top, bottom: b.bottom };
});
// 直接读引擎写下去的 translateY 数值：''/'none' 与 'translate3d(0px,0px,0px)' 都等于 0，不能按字符串比
const readPageY = () => page.evaluate(() => {
  const el = document.getElementById('scrollContentMe');
  const t = (el && el.style.transform) || '';
  const m = t.match(/translate3?d?\(([^)]*)\)/);
  if (!m) return 0;
  const parts = m[1].split(',').map((s) => parseFloat(s));
  return parts.length >= 2 ? parts[1] : 0;
});
const pageY0 = await readPageY();
const cdp = await context.newCDPSession(page);
// 起手点必须在列表可视区内、且避开底部自定义 tabBar（那里会截走手势）
const startY = geoBox.top + 30;
const tp = (y) => [{ x: Math.round(geoBox.cx), y: Math.round(y) }];
await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: tp(startY) });
for (let i = 1; i <= 8; i++) {
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: tp(startY - i * 10) });
  await page.waitForTimeout(16);
}
await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
await page.waitForTimeout(400);
const ges = await page.evaluate(() => ({
  listScrollTop: document.querySelectorAll('#orderList .order-card')[0].querySelector('.detail-files-scroll').scrollTop,
}));
const pageY1 = await readPageY();
check('②b 手指在文件列表内上滑 → 列表自己滚起来（没有被页面滚动引擎吞掉）',
      ges.listScrollTop > 0, `listScrollTop=${Math.round(ges.listScrollTop)}`);
check('②b 同一手势没把页面一起滚走（内层优先）',
      Math.abs(pageY1 - pageY0) <= 0.5, `pageY before=${pageY0} after=${pageY1}`);
await page.screenshot({ path: `${SHOTS}/手势_列表内上滑.png` });

/* ── ④ 文件多才有提示 ────────────────────────────────────────────── */
check('④ 20 文件时出现「列表可上下滑动」提示', m1.hint.indexOf('可上下滑动') >= 0, JSON.stringify(m1.hint));

/* ── ⑤ 2 文件订单：不留白、不出提示 ──────────────────────────────── */
check('⑤ 2 文件订单已展开', await expandCard(1));
const m3 = await page.evaluate(() => {
  const card = document.querySelectorAll('#orderList .order-card')[1];
  const box = card.querySelector('.detail-files-scroll');
  const detail = card.querySelector('.order-card-detail');
  return {
    boxClient: box.clientHeight, boxScroll: box.scrollHeight,
    detailClient: detail.clientHeight, detailScroll: detail.scrollHeight,
    hint: (card.querySelector('.detail-files-hint') || {}).textContent || '',
  };
});
check('⑤ 短列表高度随内容（不留 480rpx 的空白：scrollHeight == clientHeight）',
      Math.abs(m3.boxScroll - m3.boxClient) <= 1,
      `client=${m3.boxClient} scroll=${m3.boxScroll}`);
check('⑤ 2 文件时不出现滑动提示', m3.hint === '', JSON.stringify(m3.hint));
check('⑤ 2 文件订单详情也不裁内容', m3.detailScroll <= m3.detailClient + 1,
      `detail client=${m3.detailClient} scroll=${m3.detailScroll}`);
await page.screenshot({ path: `${SHOTS}/2文件_短列表不留白.png` });

/* ── ⑥ 变异自证：去掉滚动框 max-height（= 恢复旧结构） → ③ 必红 ──── */
await page.addStyleTag({ content: '.detail-files-scroll{max-height:none!important;overflow:visible!important}' });
await page.evaluate(() => document.querySelectorAll('#orderList .order-card')[0].click());
await page.waitForTimeout(200);
await page.evaluate(() => {
  const cards = document.querySelectorAll('#orderList .order-card');
  const d0 = cards[0].querySelector('.order-card-detail');
  if (!d0.classList.contains('detail-expanded')) cards[0].click();
});
await page.waitForTimeout(300);
const mut = await page.evaluate(() => {
  const card = document.querySelectorAll('#orderList .order-card')[0];
  const detail = card.querySelector('.order-card-detail');
  return { client: detail.clientHeight, scroll: detail.scrollHeight };
});
check('⑥ 变异自证：去掉滚动框 max-height 后详情确实被裁（旧结构能红）',
      mut.scroll > mut.client + 100,
      `detail client=${mut.client} scroll=${mut.scroll}`);
await page.screenshot({ path: `${SHOTS}/变异_去掉滚动框后内容被裁.png` });

/* ── ⑦ 变异自证：app.js 的 nested 选择器不认这个类 → ②b 必红 ──────
   把 app.js 里 onTouchStart 的 closest 选择器去掉 .detail-files-scroll（= 改回旧版），
   引擎就会把这个手势当成"页面滚动"接走并 preventDefault —— 列表一格都滚不动。 */
const page2 = await context.newPage();
await page2.addInitScript((base) => {
  try {
    window.__HN_TEST_BASE_URL__ = base;
    localStorage.setItem('hn_token', 'stub-token');
    localStorage.setItem('hn_theme_mode', 'light');
  } catch (e) { /* 忽略 */ }
}, BASE);
await page2.route('**/app.js', async (route) => {
  const src = await readFile(join(APP_DIR, 'app.js'), 'utf8');
  const mutated = src.replace("'.file-list-scroll, .wheel-viewport, .detail-files-scroll'", "'.file-list-scroll, .wheel-viewport'");
  const hit = mutated !== src;
  console.log(`  [mutation] app.js 选择器改写${hit ? '成功' : '失败（没匹配到目标字符串）'}`);
  route.fulfill({ status: 200, contentType: 'text/javascript; charset=utf-8', body: mutated });
});
await page2.route('**/api/**', (route) => {
  if (route.request().url().startsWith(BASE)) return route.continue();
  return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: '{}' });
});
await page2.goto(`${BASE}/index.html`, { waitUntil: 'load' });
await page2.waitForFunction(() => typeof switchTab === 'function', null, { timeout: 15000 });
await page2.evaluate(() => switchTab('me'));
await page2.waitForFunction(
  () => document.getElementById('page-me') && document.getElementById('page-me').style.display !== 'none'
    && document.querySelectorAll('#orderList .order-card').length === 2,
  null, { timeout: 15000 });
await page2.addStyleTag({ content: '*,*::before,*::after{animation:none!important;transition:none!important}' });
await page2.waitForTimeout(1200);
for (let i = 0; i < 3; i++) {
  await page2.evaluate(() => document.querySelectorAll('#orderList .order-card')[0].click());
  try {
    await page2.waitForFunction(
      () => {
        const d = document.querySelectorAll('#orderList .order-card')[0].querySelector('.order-card-detail');
        return !!d && d.classList.contains('detail-expanded');
      }, null, { timeout: 1500 });
    break;
  } catch (e) { /* 再点一次 */ }
}
const mBox = await page2.evaluate(() => {
  const box = document.querySelectorAll('#orderList .order-card')[0].querySelector('.detail-files-scroll');
  box.scrollTop = 0;
  const b = box.getBoundingClientRect();
  return { cx: b.left + b.width / 2, top: b.top };
});
const cdp2 = await context.newCDPSession(page2);
await cdp2.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: Math.round(mBox.cx), y: Math.round(mBox.top + 30) }] });
for (let i = 1; i <= 8; i++) {
  await cdp2.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: Math.round(mBox.cx), y: Math.round(mBox.top + 30 - i * 10) }] });
  await page2.waitForTimeout(16);
}
await cdp2.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
await page2.waitForTimeout(400);
const mutScroll = await page2.evaluate(() =>
  document.querySelectorAll('#orderList .order-card')[0].querySelector('.detail-files-scroll').scrollTop);
check('⑦ 变异自证：引擎不认识内层滚动容器时，同样的手势滚不动列表（②b 能红）',
      mutScroll <= 0, `listScrollTop=${mutScroll}`);

await browser.close();
server.close();

console.log(`\n${failures === 0 ? '✅ 全部通过' : `❌ ${failures} 项失败`}`);
process.exit(failures === 0 ? 0 : 1);
