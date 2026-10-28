/**
 * 移动端「我」页内容布局重排的**渲染验收**（Android APP / Capacitor WebView 那一半）
 *
 * 为什么需要它：静态脚本 tests/verify_me_layout_order.py 只能证明"源码里的区块顺序对"，
 * 证明不了"用户打开页面第一眼看到的是什么"。本脚本用 Playwright + 真实 www/ 源码 +
 * 桩后端渲染「我」页，量**真实几何与 DOM 顺序**，并同时渲染**重排前**（重排提交 26e0594 的父提交
 * 那一版 index.html）作对照 —— 重排前必须量不到"首屏有我的打印任务"，重排后必须量得到。
 *
 * 判据（超管，12 单）：
 *   ① 重排后：「我的打印任务」区块顶边落在首屏（< 视口高）内
 *   ② 重排后：orders-section 在 #adminBlockKeys 之前（我的内容 → 管理工具）
 *   ③ 重排前：① ② 都**不**成立（否则本判据测不到这次重排）
 * 判据（访客）：
 *   ④ 重排后：「验证许可密钥」在「账号绑定」之前（先解决"我能不能打印"）
 *   ⑤ 重排前：顺序相反
 *
 * 截图输出到 截图展示/我界面布局/（重排前_* / 重排后_*）。
 *
 * 用法: node tests/verify_me_layout_render.mjs        （exit 0 = 全部通过）
 *      run_all.py 默认跳过（需 Playwright / 真实浏览器），--all 或直接 node 可跑。
 */
import { createRequire } from 'node:module';
import http from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { execFileSync } from 'node:child_process';

const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const REPO = 'D:/打印机项目';
const APP_DIR = `${REPO}/mobile_apps/android_app/www`;
const SHOTS = `${REPO}/截图展示/我界面布局`;
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
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? `  -- ${detail}` : ''));
}

/* ── 桩数据（形状对齐后端真实返回，见 me.js 的各 loader） ─────────────── */
const ORDER = (i) => ({
  id: 1000 - i,
  order_number: `HN2026101${i}-000${i}`,
  status: i % 3 === 0 ? 'printing' : 'sent',
  source: 'cloud',
  file_summary: `离散数学作业${i}.pdf +1 个文件`,
  total_pages: 30,
  total_copies: 10,
  created_at: `2026-10-${String(17 - (i % 9)).padStart(2, '0')} 13:40:28`,
  total_price: 6.0,
  files: [{ id: i, original_name: `离散数学作业${i}.pdf`, page_count: 4, copies: 5, duplex: 'on', page_range: '' }],
});

const PROFILE = {
  success: true, nickname: '孤Gu孤fu',
  avatar_url: 'data:image/svg+xml;charset=utf-8,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' viewBox=\'0 0 108 108\'%3E%3Ccircle cx=\'54\' cy=\'54\' r=\'54\' fill=\'%23C7E0FF\'/%3E%3C/svg%3E',
};

function meJson(role) {
  const isAdmin = role === 'admin';
  return {
    success: true,
    role,
    is_super_admin: isAdmin,
    openid: 'dev_test',
    nickname: PROFILE.nickname,
    license_info: {
      key: 'AB12CD34', type: 'admin', expired: false, creator_nickname: '孤Gu孤fu',
      created_at: '2026-09-06 12:00:00', used_at: '2026-09-06 12:00:01',
      expires_at: '—', validity_minutes: null,
    },
  };
}

function apiFor(rawUrl, role) {
  const [u, qs] = rawUrl.split('?');
  const q = new URLSearchParams(qs || '');
  const isAdmin = role === 'admin';
  const orders = Array.from({ length: 12 }, (_, i) => ORDER(i + 1));
  if (u === '/api/me') return meJson(role);
  if (u === '/api/profile') return PROFILE;
  if (u === '/api/orders') {
    const all = role === 'guest' ? [] : orders;
    const per = Number(q.get('per_page') || 10) || 10;
    const page = Number(q.get('page') || 1) || 1;
    return { success: true, total: all.length, orders: all.slice((page - 1) * per, page * per) };
  }
  if (u === '/api/pricing') {
    return {
      success: true,
      pricing: {
        simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1,
        delivery_locations: ['1号楼北楼'], delivery_percentages: { '1号楼北楼': 0 },
        urgency_levels: ['低'], urgency_prices: { '低': 0 },
      },
    };
  }
  if (u === '/api/license/active') {
    return {
      success: true, keys: [{
        key: 'EF56GH78', type: 'temp', status: 'unused', expired: false,
        countdownText: '5 分 00 秒', created_at: '2026-10-17 23:00:00',
      }],
    };
  }
  if (u === '/api/bind/devices') return { success: true, devices: [{ openid: 'dev_test', nickname: '本机' }] };
  if (u === '/api/admin/temp_users') return { success: true, users: [] };
  if (u === '/api/admin/admins') {
    return {
      success: true, admins: [
        { openid: 'a1', nickname: '管理员甲', avatar_url: '', is_super: false },
        { openid: 'a2', nickname: '管理员乙', avatar_url: '', is_super: false },
      ],
    };
  }
  if (u === '/api/admin/storage') {
    return { success: true, total_files: 3, total_size_display: '498.3 KB', retention_days: 1, retention_hours: 0 };
  }
  if (u === '/api/admin/security') {
    return {
      success: true,
      storage_quota_mb: 2048, disk_min_free_mb: 5120, queue_timeout_hours: 24,
      upload_rate_limit: 20, submit_rate_limit: 20, device_register_rate_limit: 20,
      redeem_rate_limit: 10, log_rate_limit: 30,
    };
  }
  if (u === '/api/claiming_devices') return { success: true, devices: [], hidden_test_devices: 0 };
  return { success: true, isAdmin, orders: [], devices: [], files: [], users: [], admins: [], keys: [], total: 0, count: 0 };
}

/* ── 静态文件 + 桩 API 服务器（index.html 可被"重排前"版本覆盖） ────────── */
let overrideHtml = null;   // 非 null 时用它替代 www/index.html
let role = 'admin';
let servingOld = false;

const server = http.createServer(async (req, res) => {
  const url = req.url.split('?')[0];
  if (url.startsWith('/api/')) {
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify(apiFor(url, role)));
    return;
  }
  const rel = normalize(url === '/' ? '/index.html' : url).replace(/^[\\/]+/, '');
  try {
    const body = (overrideHtml && rel === 'index.html')
      ? Buffer.from(overrideHtml, 'utf8')
      : await readFile(join(APP_DIR, rel));
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

/* 渲染一个场景，返回 { firstScreenHas(sel), orderOf(aSel,bSel), shot(...) } */
async function render({ old, roleName, tag }) {
  servingOld = !!old;
  role = roleName;
  const page = await browser.newPage({ viewport: { width: 360, height: 800 }, deviceScaleFactor: 2 });
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
      localStorage.setItem('hn_nickname', '孤Gu孤fu');
    } catch (e) { /* 忽略 */ }
  }, BASE);
  await page.route('**/api/**', (route) => {
    const url = route.request().url();
    if (url.startsWith(BASE)) return route.continue();
    return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: '{}' });
  });

  await page.goto(`${BASE}/index.html`, { waitUntil: 'load' });
  await page.waitForFunction(() => typeof switchTab === 'function', null, { timeout: 15000 });
  await page.evaluate(() => switchTab('me'));
  // 等订单/许可渲染出来
  await page.waitForFunction(
    () => document.getElementById('page-me') && document.getElementById('page-me').style.display !== 'none'
      && document.getElementById('orderList')
      && (document.getElementById('orderList').children.length > 0
          || document.getElementById('ordersEmpty').style.display !== 'none'),
    null, { timeout: 15000 });
  // 关掉动画，量到终态几何（入场动画从 opacity:0 开始，开着会量到隐藏态）
  await page.addStyleTag({ content: '*,*::before,*::after{animation:none!important;transition:none!important}' });
  await page.waitForTimeout(400);

  const geo = await page.evaluate(() => {
    const q = (s) => document.querySelector(s);
    const top = (s) => { const e = q(s); return e ? Math.round(e.getBoundingClientRect().top) : null; };
    const pos = (a, b) => {           // a 在 b 之前 → true
      const ea = q(a), eb = q(b);
      if (!ea || !eb) return null;
      return !!(ea.compareDocumentPosition(eb) & Node.DOCUMENT_POSITION_FOLLOWING);
    };
    const cards = document.querySelectorAll('#orderList .order-card');
    const contentH = Math.round(document.getElementById('scrollContentMe').getBoundingClientRect().height);
    return {
      viewH: window.innerHeight,
      topOrders: top('.orders-section'),
      topGuest: top('#guestSection'),
      topUserLic: top('#userLicenseSection'),
      topAdminKeys: top('#adminBlockKeys'),
      topBind: top('.page-root#page-me .role-actions'),
      ordersBeforeAdmin: pos('.orders-section', '#adminBlockKeys'),
      guestBeforeBind: pos('#guestSection', '.page-root#page-me .role-actions'),
      orderCards: cards.length,
      contentH,
      navTitle: (document.querySelector('#navTitle') || {}).textContent || '',
    };
  });

  const shot = async (name, full) => {
    if (full) {
      await page.setViewportSize({ width: 360, height: Math.min(geo.contentH + 120, 20000) });
      await page.waitForTimeout(500);
    }
    await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: false });
    if (full) await page.setViewportSize({ width: 360, height: 800 });
  };

  return { page, geo, foreign, shot };
}

/* ── 场景 1/2：重排后（当前源码） ───────────────────────────────────────── */
console.log('=== 重排后（当前源码） ===');
console.log('-- 超管，12 单 --');
servingOld = false;
overrideHtml = null;
const afterAdmin = await render({ old: false, roleName: 'admin', tag: 'after-admin' });
check('① 首屏就能看到「我的打印任务」（区块顶边落在视口内）',
      afterAdmin.geo.topOrders !== null && afterAdmin.geo.topOrders < afterAdmin.geo.viewH,
      `orders.top=${afterAdmin.geo.topOrders}px < viewH=${afterAdmin.geo.viewH}px`);
check('② 「我的打印任务」排在管理工具（生成许可密钥）之前',
      afterAdmin.geo.ordersBeforeAdmin === true,
      `orders 在 adminBlockKeys 之前 = ${afterAdmin.geo.ordersBeforeAdmin}`);
check('   订单卡片照常渲染（10 条/页）', afterAdmin.geo.orderCards === 10, `cards=${afterAdmin.geo.orderCards}`);
check('   首屏第一个区块是身份卡（无管理面板抢占首屏）',
      afterAdmin.geo.topAdminKeys > afterAdmin.geo.viewH,
      `adminBlockKeys.top=${afterAdmin.geo.topAdminKeys}px`);
await afterAdmin.shot('重排后_超管首屏', false);
await afterAdmin.shot('重排后_超管整页', true);
check('渲染全程无外部请求（未打到生产接口）', afterAdmin.foreign.length === 0,
      afterAdmin.foreign.slice(0, 2).join(', '));

console.log('-- 访客 --');
const afterGuest = await render({ old: false, roleName: 'guest', tag: 'after-guest' });
check('④ 访客首屏先看到「验证许可密钥」，它在「账号绑定」之前',
      afterGuest.geo.guestBeforeBind === true
      && afterGuest.geo.topGuest !== null && afterGuest.geo.topGuest < afterGuest.geo.viewH,
      `guestBeforeBind=${afterGuest.geo.guestBeforeBind}, guest.top=${afterGuest.geo.topGuest}px`);
await afterGuest.shot('重排后_访客首屏', false);

/* ── 场景 3/4：重排前（26e0594^ 那一版 index.html） ────────────────────
   基线必须**钉死**在重排提交 26e0594 的父提交上：原先写 'HEAD:' 时，重排一旦被提交，
   对照组的"重排前"就成了重排后，③/⑤ 会永远红（2026-10-28 实测：HEAD 里 orders 已在
   adminSections 之前）。钉 rev 后语义稳定，与仓库里其他反向验证基线（0ea71f3 等）同一做法。 */
console.log('\n=== 重排前（26e0594^ 那一版 index.html，仅作对照） ===');
let oldHtml = null;
try {
  oldHtml = execFileSync('git', ['show', '26e0594^:mobile_apps/android_app/www/index.html'],
    { cwd: REPO, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 });
} catch (e) {
  console.log(`  （取不到基线版本，跳过对照：${e.message.split('\n')[0]}）`);
}
if (oldHtml) {
  overrideHtml = oldHtml;
  console.log('-- 超管，12 单 --');
  const beforeAdmin = await render({ old: true, roleName: 'admin', tag: 'before-admin' });
  check('③a 重排前：首屏**看不到**「我的打印任务」',
        beforeAdmin.geo.topOrders !== null && beforeAdmin.geo.topOrders > beforeAdmin.geo.viewH,
        `orders.top=${beforeAdmin.geo.topOrders}px > viewH=${beforeAdmin.geo.viewH}px`);
  check('③b 重排前：管理工具排在「我的打印任务」之前',
        beforeAdmin.geo.ordersBeforeAdmin === false,
        `orders 在 adminBlockKeys 之前 = ${beforeAdmin.geo.ordersBeforeAdmin}`);
  await beforeAdmin.shot('重排前_超管首屏', false);
  await beforeAdmin.shot('重排前_超管整页', true);

  console.log('-- 访客 --');
  const beforeGuest = await render({ old: true, roleName: 'guest', tag: 'before-guest' });
  check('⑤ 重排前：访客先看到「账号绑定」，「验证许可密钥」排在它后面',
        beforeGuest.geo.guestBeforeBind === false,
        `guestBeforeBind=${beforeGuest.geo.guestBeforeBind}`);
  await beforeGuest.shot('重排前_访客首屏', false);
}

console.log(`\n截图输出：${SHOTS}`);
await browser.close();
server.close();
if (failures) {
  console.log(`\n${failures} 项未通过`);
  process.exit(1);
}
console.log('\n全部通过');
