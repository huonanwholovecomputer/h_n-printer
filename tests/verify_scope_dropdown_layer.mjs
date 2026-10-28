/* 验收 · 口径栏「成员/来源」多选下拉的**图层顺序**（2026-10-27 现场：样式偶尔像丢了）
 *
 * 现象（用户截图 PixPin_2026-10-27_18-12-08）：点开口径栏「成员（可多选）」后，浮层
 * 没有白色/深色底卡，底下的「⚠️ 该口径云端收益记录比手动代收入多 ¥1.93」警示条和 KPI 卡
 * 的文字压在面板上，看起来像「样式没加载」。偶尔发生、切个页又自己好了。
 *
 * 根因：switchTab() 末尾的 animateTab() 给每个 tab 子元素挂 fadeUp 入场动画，填充模式写的是
 * both —— fadeUp 的 to 是 transform: translateY(0)，both 会把这一帧**留在元素上**，
 * 于是每个 tab 子元素（含 .scope-bar 与 .alert-banner）长期都是层叠上下文；
 * .dd-panel 的 z-index:70 只在 .scope-bar 这个上下文内部有效，跳不出去，
 * 后面的兄弟卡片就盖在浮层上。改成 backwards 后动画一结束就回到 transform: none，
 * 浮层的 z-index 恢复全局语义。（空白期只有入场动画的 ~0.85 秒，动画一结束自动恢复。）
 *
 * 为什么必须真浏览器：这是「谁画在上面」的问题 —— 静态 grep 只能证明代码里写了 z-index，
 * 证明不了它有没有被祖先的层叠上下文关起来。这里用两条证据钉住：
 *   ① 层叠上下文的**成因**：动画结束后 .scope-bar / .alert-banner 的 computed transform 必须是 none
 *      （before：matrix(1, 0, 0, 1, 0, 0) ⇒ 必红）；
 *   ② 画在谁的上面：浮层与警示条真实重叠的那一点做 elementFromPoint，命中的必须是浮层内部
 *      （命中警示条 ⇒ 浮层被盖住 ⇒ 必红）。
 *
 * 做法：起本地 HTTP 服务（静态文件来自 local_print_tool/finance/，/api/** 全部打桩，
 * 不发外网请求），Playwright 打开 settlement.html → 切「云端数据」→ 断言 ①②。
 * 产物：截图存 截图展示/口径栏下拉层级/。
 * 用法：node tests/verify_scope_dropdown_layer.mjs
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
const SETTLEMENT = path.join(FINANCE, 'settlement.html');
const OUT_DIR = path.join(HERE, '..', '截图展示', '口径栏下拉层级');
fs.mkdirSync(OUT_DIR, { recursive: true });

let bad = 0, total = 0;
function chk(name, cond, detail) {
  total++; if (!cond) bad++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (!cond && detail !== undefined ? `  -- ${detail}` : ''));
}

/* ── 打桩数据：两位成员 + 一张已完成的本地订单（有收益、无手动代收 ⇒ 对账有差额 ⇒ 出警示条） ── */
const STORE = {
  config: { investRate: 0.05, scoreCap: 15, passwords: {},
            members: [{ id: 'ma', name: '霍楠', color: '#4472C4' }, { id: 'mb', name: '姚懿祥', color: '#548235' }],
            orderPrinterMap: {}, printers: [] },
  records: { expenses: [], income: [], scores: [], transfers: [] },
  notes: {}, meta: { version: 2 },
};
const ORDER = {
  order_id: 1, order_number: 'HN20261020-0003', created_at: '2026-10-20 10:00:00',
  source: 'local', openid: 'local', owner_name: '霍楠', status: 'sent', is_admin_print: 0,
  total_price: 1.93, received_client: 'R7000-1', received_label: '霍楠的设备 | R7000-1',
  files_count: 1, total_page_count: 6,
  files: [{ id: 'f1', status: 'sent', page_count: 6, copies: 1, duplex: 'off', page_range: '',
            total_price: 1.93, is_free: false, effective_pages: 6, sheets: 6, total_pages: 6, total_sheets: 6 }],
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
  if (p === '/api/proxy/admin/statistics/revenue') return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: [ORDER] } });
  if (p === '/api/proxy/printer/devices') return json(res, { success: true, devices: [] });
  if (p === '/api/local/device') return json(res, { success: true, device: { client_id: 'R7000-1', device_name: 'R7000-AHP10', take_orders: true } });
  return json(res, { success: true, data: null });
});

const port = await new Promise((r) => server.listen(0, '127.0.0.1', () => r(server.address().port)));
const browser = await chromium.launch({ args: [`--explicitly-allowed-ports=${port}`] });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, colorScheme: 'dark', deviceScaleFactor: 1.4 });
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));   // 只收未捕获异常；控制台里的 CDN 噪音不算
page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

await page.goto(`http://127.0.0.1:${port}/settlement.html`, { waitUntil: 'load' });
await page.waitForTimeout(800);
chk('页面没有 JS 异常（打桩数据下）', errors.length === 0, errors.slice(0, 3).join(' | '));

/* ── 切到「云端数据」：switchTab() 会调用 animateTab()（问题就出在这里的填充模式） ── */
await page.click('#tabNav button[data-tab="cloud"]');
await page.waitForTimeout(1400);   // 等数据落地 + 入场动画（.45s + 延迟）跑完

/* ① 层叠上下文的成因：动画跑完后 tab 子元素不许残留 transform */
const tf = await page.evaluate(() => {
  const bar = document.querySelector('#tab-cloud .scope-bar');
  const banner = document.querySelector('#tab-cloud .alert-banner');
  const kids = Array.prototype.slice.call(document.querySelectorAll('#tab-cloud > *'));
  return {
    bar: bar ? getComputedStyle(bar).transform : 'MISSING',
    banner: banner ? getComputedStyle(banner).transform : 'MISSING',
    dirty: kids.filter((k) => getComputedStyle(k).transform !== 'none')
               .map((k) => (k.className || k.tagName) + '=' + getComputedStyle(k).transform),
  };
});
chk('口径栏（.scope-bar）入场动画结束后不留残余 transform（backwards，不是 both）',
  tf.bar === 'none', `实际 ${tf.bar}`);
chk('#tab-cloud 下没有任何子元素残留 transform ⇒ 不会再产生"关住浮层"的层叠上下文',
  tf.dirty.length === 0, JSON.stringify(tf.dirty.slice(0, 4)));

/* 前提：这份打桩数据必须真的渲染出警示条（它就是现场被压在浮层上的那个兄弟卡片） */
const hasBanner = await page.evaluate(() => !!document.querySelector('#tab-cloud .alert-banner'));
chk('对账有差额 ⇒ 渲染出 .alert-banner 警示条（图层顺序的对手就位）', hasBanner,
  await page.evaluate(() => (document.querySelector('#tab-cloud') || {}).innerText || '').then ? '' : '');
await page.screenshot({ path: path.join(OUT_DIR, '01-切到云端数据-入场合影.png') });

/* ── 打开「成员（可多选）」下拉（走页面自己的 onScopeDD） ── */
await page.evaluate(() => { const btn = document.querySelector('#tab-cloud .dd-btn'); btn.click(); });
await page.waitForTimeout(250);
const panelOpen = await page.evaluate(() => {
  const p = document.getElementById('dd-members');
  return p ? p.style.display : 'MISSING';
});
chk('点开「成员」下拉后 #dd-members 展开（display:block）', panelOpen === 'block', `实际 ${panelOpen}`);

/* ② 画在谁的上面：浮层与警示条真实重叠点的 elementFromPoint 必须命中浮层内部 */
const paint = await page.evaluate(() => {
  const panel = document.getElementById('dd-members');
  const banner = document.querySelector('#tab-cloud .alert-banner');
  if (!panel || !banner) return { err: 'panel/banner 缺失' };
  const a = panel.getBoundingClientRect(), b = banner.getBoundingClientRect();
  const x = Math.min(a.right, b.right) - Math.max(a.left, b.left);
  const y = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
  if (x <= 0 || y <= 0) return { err: '浮层与警示条没有重叠（area=' + x + 'x' + y + '）' };
  /* 取重叠区的右侧 1/4、垂直居中处：避开左边缘的圆角与勾选框文字 */
  const cx = Math.max(a.left, b.left) + x * 0.75;
  const cy = Math.max(a.top, b.top) + y * 0.5;
  const hit = document.elementFromPoint(cx, cy);
  return {
    area: { w: Math.round(x), h: Math.round(y) }, point: { x: Math.round(cx), y: Math.round(cy) },
    hitTag: hit ? hit.tagName : 'null',
    hitCls: hit ? String(hit.className || '') : '',
    inPanel: !!(hit && hit.closest && hit.closest('#dd-members')),
    bg: getComputedStyle(panel).backgroundColor,
    zIndex: getComputedStyle(panel).zIndex,
  };
});
chk('浮层与警示条确实重叠（否则这条验收没有意义）', !paint.err && paint.area && paint.area.w > 20 && paint.area.h > 10,
  JSON.stringify(paint));
chk('重叠处命中浮层内部 ⇒ 下拉画在警示条之上（现场"样式像丢了"的样子不再出现）',
  paint.inPanel === true, `命中 ${paint.hitTag}.${paint.hitCls}`);
chk('浮层自身有不透明背景色（不是 transparent —— 底卡颜色就是"样式"本身）',
  !!paint.bg && paint.bg !== 'transparent' && !/rgba\(0, 0, 0, 0\)/.test(paint.bg), `背景 ${paint.bg}`);
await page.screenshot({ path: path.join(OUT_DIR, '02-成员下拉-重叠警示条处.png') });

/* ── 顺手覆盖「打印机折旧」页的那一套浮层（idp='-p'，同一份 animateTab） ── */
await page.click('#tabNav button[data-tab="printers"]');
await page.waitForTimeout(1200);
const tf2 = await page.evaluate(() => {
  const bar = document.querySelector('#tab-printers .scope-bar');
  return bar ? getComputedStyle(bar).transform : 'MISSING';
});
chk('打印机折旧页的口径栏同样不留残余 transform（两页共用一份 animateTab）',
  tf2 === 'none', `实际 ${tf2}`);
await page.evaluate(() => { const p = document.querySelector('#tab-printers .dd-btn'); if (p) p.click(); });
await page.waitForTimeout(250);
const openP = await page.evaluate(() => {
  const p = document.getElementById('dd-members-p');
  return p ? p.style.display : 'MISSING';
});
chk('打印机折旧页的成员下拉（#dd-members-p）也能正常展开', openP === 'block', `实际 ${openP}`);
await page.screenshot({ path: path.join(OUT_DIR, '03-折旧页-成员下拉.png') });

/* ③ 静态守卫：填充模式被改回 both 时，第 ② 条在真机上偶发重现，这里先钉住源码口径 */
const src = fs.readFileSync(SETTLEMENT, 'utf8');
chk('源码：animateTab 用 backwards 填充（both 会残留 transform，见文件头注释）',
  src.indexOf("fadeUp .45s ease backwards") >= 0 && src.indexOf("fadeUp .45s ease both") < 0);

chk('全程没有 JS 异常', errors.length === 0, errors.slice(0, 3).join(' | '));

await browser.close();
server.close();
console.log(`\n${total - bad}/${total} 通过` + (bad ? `，${bad} 项失败` : ''));
process.exit(bad ? 1 : 0);
