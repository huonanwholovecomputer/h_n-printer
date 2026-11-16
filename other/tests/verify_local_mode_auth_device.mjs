/* 验收（真实渲染）· 收支清算「🔐 授权」页在**本地（离线）模式**下不再整页空白
 *
 * 背景（2026-11-15）：云端模式与本页是同一份 settlement.html，只是 __FINANCE_MODE__ 不同。
 * v7.6 把打印机台账搬走后，本地模式里授权页只剩一句"本机信息与设备列表仅云端模式可用"，
 * 导航还把入口整个隐藏了 ⇒ 离线用户以为这个功能在离线版里"没有"，与云端版看起来差很多。
 * v7.22 起：本地模式保留入口 + 渲染「💻 本设备」卡片（/api/local/device，纯本机快照，离线可取）；
 * 云端设备列表 / 所有者绑定 / 自动双面标注仍需云端模式 ⇒ 本地模式**不发** /api/proxy/printer/devices。
 *
 * 判据（全部真实渲染，/api/** 本地打桩，绝不碰生产接口）：
 *   ① local：导航里有「授权」入口（不再 display:none）；
 *   ② local：卡片显示 计算机名称 / 设备 ID / 接单状态（取自 /api/local/device 的 take_orders）；
 *   ③ local：**不渲染** #auth-devices（云端设备列表），且**零请求** /api/proxy/printer/devices；
 *   ④ local：页面里不再出现"仅云端模式可用"那句整页说明；
 *   ⑤ cloud：回归 —— 两张卡片都在、两条请求都发出（本改动没有把云端页做坏）。
 *
 * 用法：node other/tests/verify_local_mode_auth_device.mjs   exit 0 = 通过
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
const HITS = { device: 0, proxyDevices: 0 };

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
const ORDERS = [{
  order_id: 1, order_number: 'HN-0001', source: 'local', openid: 'local', nickname: '', owner_name: '张三',
  status: 'sent', total_price: 1.8, created_at: '2026-11-10 10:00:00', received_client: 'c1020',
  is_admin_print: 0, files_count: 1, total_page_count: 6, total_sheet_count: 3,
  files: [{ status: 'sent', total_sheets: 3, total_pages: 6, duplex: 'on', copies: 1, page_count: 6, file_name: 'a.pdf', total_price: 1.8 }],
}];

const json = (res, o) => { res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(o)); };
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  const p = decodeURIComponent(u.pathname);
  if (p.startsWith('/api/')) {
    if (p === '/api/health') return json(res, { success: true, cloud: MODE.value === 'cloud' });
    if (p === '/api/proxy/finance/config') { req.resume(); return json(res, { success: true, data: STORE, server_version: 2 }); }
    if (p === '/api/local/data') { req.resume(); return json(res, { success: true, data: STORE }); }
    if (p === '/api/local/finance_auth/status') {
      req.resume();
      return json(res, { success: true, modules: {}, needs_setup: [], session: { active: true, token: 'probe', modules: MODULES, expires_in: 1800 }, session_ttl: 1800, read_only: false, integrity: 'ok' });
    }
    /* 真实形态（stats_server._handle_get_device）：client_id / device_name / take_orders */
    if (p === '/api/local/device') { HITS.device++; req.resume(); return json(res, { success: true, device: { client_id: 'c1020', device_name: '验收机-A', take_orders: true } }); }
    if (p === '/api/local/bindings') { req.resume(); return json(res, { success: true, data: {} }); }
    if (p === '/api/proxy/printer/devices') { HITS.proxyDevices++; req.resume(); return json(res, { success: true, devices: [{ client_id: 'c1020', owner_name: '张三', auto_duplex: true, printer_name: 'HP LaserJet 1020' }] }); }
    if (p === '/api/proxy/pricing') return json(res, { success: true, data: { simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1, delivery_percentages: {}, urgency_prices: {} } });
    if (p === '/api/proxy/admin/statistics/months') return json(res, { success: true, data: { months: [{ month: '2026-11' }] } });
    if (p === '/api/proxy/admin/statistics/revenue') return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: ORDERS } });
    if (p === '/api/local/orders') return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: ORDERS } });
    if (p === '/api/local/external_prints') return json(res, { success: true, orders: [], count: 0, skipped: 0, simplex_price: 0.3, duplex_price: 0.4, owner_name: '' });
    if (p.indexOf('/api/proxy/admin/') >= 0) return json(res, { success: true, data: { devices: [], months: [] } });
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
  HITS.device = 0; HITS.proxyDevices = 0;
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 }, deviceScaleFactor: 1 });
  const errors = [], reqs = [];
  page.on('pageerror', (e) => errors.push(String(e.message)));
  page.on('request', (r) => { if (r.url().includes('/api/')) reqs.push(new URL(r.url()).pathname); });
  await page.goto(BASE + '/settlement.html?mode=' + mode, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1200);
  await page.evaluate((mods) => {
    try {
      window._setFinanceSession('probe', 1800, mods);
      if (window.unlocked) mods.forEach((k) => { window.unlocked[k] = true; });
    } catch (e) {}
  }, MODULES);
  await page.waitForTimeout(300);
  const vis = await page.evaluate(() => [...document.querySelectorAll('#tabNav button')]
    .filter((b) => !!(b.offsetWidth || b.offsetHeight || b.getClientRects().length))
    .map((b) => ({ tab: b.dataset.tab, text: b.textContent.trim() })));
  /* 页面首屏（dash）本来就会取 2 次 /api/local/device（页面别处的设备角标/绑定下拉），
     与本页无关 —— 先清零，只数「点进授权页」这一次渲染发了几个请求。 */
  HITS.device = 0; HITS.proxyDevices = 0;
  await page.click('#tabNav button[data-tab="auth"]').catch(() => {});
  await page.waitForTimeout(1200);
  const snap = await page.evaluate(() => {
    const box = document.getElementById('auth-local-device');
    const dev = document.getElementById('auth-devices');
    const el = document.getElementById('tab-auth') || document.body;
    return {
      hasLocalBox: !!box,
      localText: box ? box.innerText.trim() : '',
      hasDevicesBox: !!dev,
      devicesVisible: !!(dev && (dev.offsetWidth || dev.offsetHeight || dev.getClientRects().length)),
      pageText: (el.innerText || ''),
      sections: [...el.querySelectorAll('.section')].map((s) => (s.querySelector('h2') ? s.querySelector('h2').textContent.trim() : '(no h2)')),
    };
  });
  await page.screenshot({ path: 'C:/Users/Administrator/.dsh/tmp_modediff/auth_' + mode + '_v722.png', fullPage: true });
  await page.close();
  return { vis, snap, errors, reqs, hits: { ...HITS } };
}

/* ── ① local ── */
console.log('\n=== ① 本地（离线）模式：授权页 ===');
const L = await openMode('local');
chk('①-1 导航里有「授权」入口（不再隐藏）', L.vis.some((b) => b.tab === 'auth'), JSON.stringify(L.vis.map((b) => b.text)));
chk('①-2 导航仍是「📡 本地订单」（不是「云端数据」）', L.vis.some((b) => b.tab === 'cloud' && b.text.indexOf('本地订单') >= 0), JSON.stringify(L.vis));
chk('②-1 渲染「💻 本设备」卡片', L.snap.hasLocalBox && L.snap.sections.some((s) => s.indexOf('本设备') >= 0), JSON.stringify(L.snap.sections));
chk('②-2 显示计算机名称（/api/local/device 的 device_name）', L.snap.localText.indexOf('验收机-A') >= 0, L.snap.localText);
chk('②-3 显示设备 ID（client_id）', L.snap.localText.indexOf('c1020') >= 0, L.snap.localText);
chk('②-4 显示接单状态（take_orders=true ⇒ 接单中）', L.snap.localText.indexOf('接单中') >= 0, L.snap.localText);
chk('③-1 不渲染 #auth-devices（云端设备列表）', !L.snap.hasDevicesBox && !L.snap.devicesVisible, JSON.stringify({ has: L.snap.hasDevicesBox, vis: L.snap.devicesVisible }));
chk('③-2 零请求 /api/proxy/printer/devices', L.hits.proxyDevices === 0, 'hits=' + L.hits.proxyDevices);
chk('③-3 渲染本设备卡只发 1 次 /api/local/device（不重复拉取）', L.hits.device === 1, 'hits=' + L.hits.device);
chk('④ 不再出现整页「本机信息与设备列表仅云端模式可用」', L.snap.pageText.indexOf('仅云端模式可用') < 0, L.snap.pageText.slice(0, 160));
chk('④-2 页面提示云端设备列表需要云端模式', L.snap.pageText.indexOf('需要云端模式') >= 0, L.snap.pageText.slice(0, 200));
chk('④-3 无 JS 运行期异常', L.errors.length === 0, JSON.stringify(L.errors.slice(0, 3)));

/* ── ⑤ cloud 回归 ── */
console.log('\n=== ⑤ 云端模式回归：授权页两张卡片都在 ===');
const C = await openMode('cloud');
chk('⑤-1 本设备卡片存在且拿到设备', C.snap.hasLocalBox && C.snap.localText.indexOf('c1020') >= 0, C.snap.localText);
chk('⑤-2 云端设备列表容器 #auth-devices 仍被渲染', C.snap.hasDevicesBox, JSON.stringify(C.snap.sections));
chk('⑤-3 仍请求 /api/proxy/printer/devices', C.hits.proxyDevices >= 1, 'hits=' + C.hits.proxyDevices);
chk('⑤-4 无 JS 运行期异常', C.errors.length === 0, JSON.stringify(C.errors.slice(0, 3)));

note('截图：C:/Users/Administrator/.dsh/tmp_modediff/auth_{local,cloud}_v722.png');
await browser.close();
server.close();
console.log('\n' + (bad === 0 ? 'ALL PASS' : 'FAILED ' + bad + ' / ' + total));
process.exit(bad === 0 ? 0 : 1);
