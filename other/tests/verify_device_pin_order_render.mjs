/* verify_device_pin_order_render.mjs — 「接单设备显示顺序」（置顶 / 置顶组内调优先级）渲染级验收
 *
 * 覆盖 Task 的 B1–B7 + B10（B8 由 tests/verify_device_pin_order_ui.js 静态验收，B9 是回归脚本）：
 *   B1 普通 user 看不到入口按钮；admin 看得到（真实 430×932 渲染 + 点击行为）
 *   B2 点入口 ⇒ 弹窗出现、遮罩在屏、背景模糊（backdrop-filter）或半透明兜底、标题文案
 *   B3 行数 == 接单设备数、每行置顶开关 + ↑/↓；置顶后移入置顶组；↑/↓ 改变置顶组内顺序（DOM 顺序）
 *   B4 点「完成」⇒ POST body.order == 弹窗最终顺序；成功后列表顺序 == 服务端响应顺序
 *   B5 桩返回 403 ⇒ 可见错误提示 + 本地顺序回滚 + 不显示成功态
 *   B6 置顶一台**离线**设备 ⇒ 默认选中它；无置顶 ⇒ 仍选第一个在线设备（老行为）
 *   B7 用户手点另一台 ⇒ 刷新不抢；该设备从列表消失 ⇒ 回落置顶第一项
 *   B10 截图（弹窗含置顶行 / 普通用户无入口 / 置顶设备被默认选中）
 *
 * 附加：小程序侧 wxss 无法无头渲染 ⇒ 用真实 index.wxss 规则按 1rpx = 0.5px 同构复刻，
 * 验「置顶徽标不撑高设备条」这条硬约束（含一条变异自证：把徽标字号放大必须量出更高）。
 *
 * 隔离：APP 的 DEFAULT_BASE_URL 是线上域名 ⇒ addInitScript 写 window.__HN_TEST_BASE_URL__
 * 指向本地桩 + page.route('**\/api/**') 兜底，收尾断言全程零外部请求。
 *
 * 用法: node tests/verify_device_pin_order_render.mjs
 * 截图目录: 截图展示/设备显示顺序/
 */
import { createRequire } from 'node:module';
import http from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { extname, join } from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const REPO = 'D:/打印机项目';
const APP_DIR = REPO + '/mobile_apps/android_app/www';
const MP_DIR = REPO + '/mobile_apps/h_n_print';
const SHOTS = REPO + '/截图展示/设备显示顺序';
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (detail ? '  -- ' + detail : ''));
}

/* ======================================================================
   桩数据：设备 / 置顶顺序 / 角色 / POST 行为（全部可在用例之间改）
   ====================================================================== */
const BASE_DEVICES = [
  { client_id: 'dev_a', label: '一号打印机', online: true, auto_duplex: true },
  { client_id: 'dev_b', label: '二号打印机', online: true, auto_duplex: true },
  { client_id: 'dev_c', label: '三号打印机', online: false, auto_duplex: true },
];
let DEVICES = BASE_DEVICES.slice();
let PIN_ORDER = [];        // 置顶 client_id（按优先级；服务端 pin_index 就是它的下标）
let ROLE = 'admin';
let POST_MODE = 'ok';      // 'ok' | '403'
let SORT = true;           // false = 桩按给的顺序原样返回（验客户端自己的「第一个在线」分支）
const posts = [];          // 收到的 /api/admin/device_pin_order body（按序）

/* 复刻后端 claiming_devices 的排序 key（app.py:8163）：
   (pin_index is None, pin_index or 0, not online, client_id.lower())
   ⇒ 置顶组在前按 pin_index 升序（组内完全不看在线）→ 未置顶组在线优先 → client_id */
function servedDevices() {
  const rows = DEVICES.map((d) => {
    const pi = PIN_ORDER.indexOf(d.client_id);
    return Object.assign({}, d, {
      pin: pi >= 0, pin_index: pi >= 0 ? pi : null,
      stack_mode: null, stack_busy: false, stack_can_toggle: true, stack_reason: '',
    });
  });
  if (!SORT) return rows;
  rows.sort((a, b) => {
    const na = a.pin_index === null, nb = b.pin_index === null;
    if (na !== nb) return na ? 1 : -1;
    const ia = a.pin_index === null ? 0 : a.pin_index;
    const ib = b.pin_index === null ? 0 : b.pin_index;
    if (ia !== ib) return ia - ib;
    const oa = a.online ? 0 : 1, ob = b.online ? 0 : 1;
    if (oa !== ob) return oa - ob;
    return a.client_id.toLowerCase() < b.client_id.toLowerCase() ? -1 : 1;
  });
  return rows;
}

const MOCK = {
  success: true, token: 'tok', openid: 'dev_stub', nickname: '测试设备', role: 'admin',
  devices: [], orders: [], files: [], users: [], admins: [], keys: [], total: 0, count: 0,
  online: true, active: true, take_orders_online: true, active_client_id: 'dev_a',
  claiming_devices: [], delivery_locations: ['1号楼北楼'], urgency_levels: ['低'],
  retention_options: [], server_retention_hours: 168,
};

const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.svg': 'image/svg+xml',
  '.json': 'application/json',
};

function readBody(req) {
  return new Promise((resolve) => {
    let raw = '';
    req.on('data', (c) => { raw += c; });
    req.on('end', () => resolve(raw));
  });
}

const server = http.createServer(async (req, res) => {
  const url = req.url.split('?')[0];
  const json = (code, obj) => {
    res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify(obj));
  };
  if (url.startsWith('/api/')) {
    if (url === '/api/device_login') return json(200, { success: true, token: 'stub-token', openid: 'dev_stub' });
    if (url === '/api/me') return json(200, { success: true, role: ROLE, is_super_admin: ROLE === 'super_admin', nickname: '测试设备' });
    if (url === '/api/profile') return json(200, { success: true, nickname: '测试设备', avatar_url: '' });
    if (url === '/api/claiming_devices') return json(200, { success: true, devices: servedDevices(), pin_order: PIN_ORDER.slice() });
    if (url === '/api/admin/device_pin_order') {
      const raw = await readBody(req);
      let body = null;
      try { body = JSON.parse(raw || '{}'); } catch (e) { body = null; }
      posts.push(body);
      if (POST_MODE === '403') return json(403, { success: false, message: '需要管理员权限' });
      /* 服务端行为：去重保序、丢不可用 cid、截到 50 项 */
      const known = DEVICES.map((d) => d.client_id);
      const next = [];
      ((body && body.order) || []).forEach((cid) => {
        if (known.indexOf(cid) >= 0 && next.indexOf(cid) < 0 && next.length < 50) next.push(cid);
      });
      PIN_ORDER = next;
      return json(200, { success: true, pin_order: PIN_ORDER.slice() });
    }
    if (url.startsWith('/api/pricing')) {
      return json(200, {
        success: true,
        pricing: {
          simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1,
          delivery_locations: ['1号楼北楼'], delivery_percentages: { '1号楼北楼': 0 },
          urgency_levels: ['低'], urgency_prices: { '低': 0 },
        },
      });
    }
    return json(200, MOCK);
  }
  const rel = (url === '/' ? 'index.html' : url).replace(/^\/+/, '');
  try {
    const body = await readFile(join(APP_DIR, rel));
    res.writeHead(200, { 'Content-Type': MIME[extname(rel)] || 'application/octet-stream' });
    res.end(body);
  } catch (e) {
    res.writeHead(404); res.end('not found');
  }
});

await new Promise((r) => server.listen(0, '127.0.0.1', r));
const LOCAL_BASE = 'http://127.0.0.1:' + server.address().port;
await mkdir(SHOTS, { recursive: true });

console.log('='.repeat(64));
console.log('接单设备显示顺序（置顶）· 渲染验收（430×932，本地桩 ' + LOCAL_BASE + '）');
console.log('='.repeat(64));

const browser = await chromium.launch({ executablePath: CHROME });
const page = await browser.newPage({ viewport: { width: 430, height: 932 }, deviceScaleFactor: 2 });
page.on('pageerror', (e) => console.log('  [pageerror] ' + e.message));

const foreign = [];
page.on('request', (r) => {
  const u = r.url();
  if (!u.startsWith(LOCAL_BASE) && !u.startsWith('data:') && !u.startsWith('blob:')) foreign.push(u);
});
/* 每次导航都从「游客」起步（localStorage 清空）⇒ 角色只由桩的 /api/me 决定，可反复切角色 */
await page.addInitScript(() => {
  try {
    localStorage.clear();
    /* 「文件保留与隐私」首次使用告知层会盖在整页之上、拦截所有点击（print.js:158）⇒
       置为已同意，本脚本只验设备显示顺序。 */
    localStorage.setItem('hn_privacy_agreed_v1', '1');
    localStorage.setItem('hn_retention_hours_v1', '0');
  } catch (e) {}
});
await page.addInitScript((base) => { try { window.__HN_TEST_BASE_URL__ = base; } catch (e) {} }, LOCAL_BASE);
await page.route('**/api/**', (route) => {
  const url = route.request().url();
  if (url.startsWith(LOCAL_BASE)) return route.continue();
  return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(MOCK) });
});

const waitFor = async (pred, timeout = 8000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    if (await pred()) return true;
    await new Promise((r) => setTimeout(r, 100));
  }
  return false;
};

/* 载入一次 APP（真角色 / 真置顶数据），等角色落地后走一次真实重绘
   （角色来自 /api/me，晚于首帧；下一次 30s 轮询就会重绘，这里直接调同一个函数） */
async function loadApp(opt) {
  const o = opt || {};
  ROLE = o.role || 'admin';
  PIN_ORDER = (o.pin || []).slice();
  DEVICES = (o.devices || BASE_DEVICES).slice();
  SORT = !o.raw;
  await page.goto(LOCAL_BASE + '/index.html', { waitUntil: 'load' });
  await page.waitForFunction(() => typeof loadClaimingDevices === 'function' && typeof state !== 'undefined', null, { timeout: 15000 });
  await page.waitForFunction((r) => state.role === r, ROLE, { timeout: 15000 });
  await page.evaluate(() => {
    renderDeviceList();
    const c = document.getElementById('deviceSelectCard');
    if (c) c.classList.remove('card-preload', 'entrance-play');   // 只影响截图（入场动画未播时 opacity:0）
  });
  await page.waitForTimeout(250);
}

const rowsInfo = () => page.evaluate(() => Array.from(document.querySelectorAll('#deviceOrderBody .device-order-row')).map((r) => ({
  id: r.dataset.id,
  pinned: r.classList.contains('device-order-row-pinned'),
  pinText: (r.querySelector('.device-order-pin') || {}).textContent || '',
  name: (r.querySelector('.device-order-name') || {}).textContent || '',
  state: (r.querySelector('.device-state') || {}).textContent || '',
  moves: r.querySelectorAll('.device-order-move').length,
})));
const listInfo = () => page.evaluate(() => ({
  order: Array.from(document.querySelectorAll('#deviceList .device-item')).map((r) => r.dataset.id),
  selected: (() => { const on = document.querySelector('#deviceList .device-item-on'); return on ? on.dataset.id : ''; })(),
  badges: Array.from(document.querySelectorAll('#deviceList .device-pin-badge')).length,
}));
const sheetState = () => page.evaluate(() => {
  const mask = document.getElementById('deviceOrderSheet');
  const cs = getComputedStyle(mask);
  const sheet = mask.querySelector('.modal-sheet');
  const ss = getComputedStyle(sheet);
  const err = document.getElementById('deviceOrderError');
  const btn = document.getElementById('deviceOrderSaveBtn');
  const toast = document.getElementById('toast');
  return {
    display: cs.display,
    backdrop: cs.backdropFilter || cs.webkitBackdropFilter || 'none',
    maskBg: cs.backgroundColor,
    sheetBg: ss.backgroundColor,
    sheetW: +sheet.getBoundingClientRect().width.toFixed(2),
    title: (document.querySelector('#deviceOrderSheet .title-medium') || {}).textContent || '',
    errText: err ? err.textContent : '',
    errDisplay: err ? getComputedStyle(err).display : '',
    btnText: btn ? btn.textContent.trim() : '',
    btnDisabled: btn ? !!btn.disabled : null,
    toastText: toast ? toast.textContent : '',
    toastShown: toast ? toast.classList.contains('show') : false,
  };
});
const openSheet = async () => {
  await page.locator('#deviceOrderEntry').click();
  await page.waitForTimeout(700);   // sheetSpringIn 0.4s + fadeIn 0.2s 收尾，rect 才稳定
};
const clickRow = (sel) => page.locator('#deviceOrderBody ' + sel).first().click();
const rowSel = (id, part) => '[data-id="' + id + '"]' + (part || '');

/* ======================================================================
   B1 —— 入口按钮的角色门控（普通 user 看不到；admin 看得到）
   ====================================================================== */
console.log('\n--- B1 入口按钮仅 admin / super_admin ---');
await loadApp({ role: 'user', pin: ['dev_c'] });
const entryUser = await page.evaluate(() => {
  const el = document.getElementById('deviceOrderEntry');
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  return { display: cs.display, w: +r.width.toFixed(2), h: +r.height.toFixed(2) };
});
check('B1 普通用户（role=user）看不到「显示顺序」入口', entryUser.display === 'none' && entryUser.w === 0,
  JSON.stringify(entryUser));
await page.evaluate(() => openDeviceOrderSheet());   // 绕过 UI 直接调，验 JS 兜底闸门
await page.waitForTimeout(200);
const userSheet = await page.evaluate(() => getComputedStyle(document.getElementById('deviceOrderSheet')).display);
check('B1b 普通用户直调 openDeviceOrderSheet() 也不弹（JS 兜底角色闸门）', userSheet === 'none', 'display=' + userSheet);
await page.locator('#deviceSelectCard').screenshot({ path: join(SHOTS, 'APP_普通用户_无入口.png') });

await loadApp({ role: 'admin', pin: ['dev_c'] });
const entryAdmin = await page.evaluate(() => {
  const el = document.getElementById('deviceOrderEntry');
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  return { display: cs.display, w: +r.width.toFixed(2), h: +r.height.toFixed(2), text: el.textContent.trim() };
});
check('B1c admin 看得到并量得到入口按钮', entryAdmin.display !== 'none' && entryAdmin.w > 20 && entryAdmin.h > 10,
  JSON.stringify(entryAdmin));
check('B1d 入口文案 == 显示顺序', entryAdmin.text === '显示顺序', entryAdmin.text);

/* ======================================================================
   B2 —— 点入口 ⇒ 弹窗 + 遮罩 + 背景模糊 + 标题
   ====================================================================== */
console.log('\n--- B2 弹窗 / 遮罩 / 背景模糊 / 标题 ---');
await openSheet();
const s2 = await sheetState();
check('B2 点入口 ⇒ 弹窗打开（.modal-mask display:flex）', s2.display === 'flex', 'display=' + s2.display);
const alpha = (() => {
  const m = /rgba?\(([^)]+)\)/.exec(s2.maskBg);
  if (!m) return 1;
  const parts = m[1].split(',').map((x) => parseFloat(x));
  return parts.length >= 4 ? parts[3] : 1;
})();
check('B2b 背景模糊生效（backdrop-filter 含 blur）或半透明兜底（alpha>0.2）',
  /blur\(/.test(s2.backdrop) || alpha > 0.2, 'backdrop=' + s2.backdrop + ' maskBg=' + s2.maskBg + ' alpha=' + alpha);
check('B2c 标题文案 == 「接单设备显示顺序」', s2.title.trim() === '接单设备显示顺序', s2.title);
check('B2d 弹窗宽度是真的（> 100px，不是塌成 0）', s2.sheetW > 100, s2.sheetW + 'px');

/* ======================================================================
   B3 —— 行数 / 置顶开关 / ↑↓ 与 DOM 顺序
   ====================================================================== */
console.log('\n--- B3 逐行置顶 + 置顶组内调序 ---');
await loadApp({ role: 'admin', pin: ['dev_a', 'dev_b'] });
await openSheet();
const r3a = await rowsInfo();
check('B3 弹窗行数 == 接单设备数（3）', r3a.length === 3, r3a.map((r) => r.id).join(','));
check('B3b 每行都有置顶开关与 ↑ / ↓', r3a.every((r) => r.pinText && r.moves === 2),
  r3a.map((r) => r.id + ':' + r.pinText + '/' + r.moves).join(' | '));
check('B3c 初始 DOM 顺序 == 服务端顺序（置顶组在前：dev_a,dev_b → dev_c）',
  r3a.map((r) => r.id).join(',') === 'dev_a,dev_b,dev_c', r3a.map((r) => r.id).join(','));
check('B3d 初始置顶态 = [true,true,false]', r3a.map((r) => r.pinned).join(',') === 'true,true,false',
  r3a.map((r) => r.pinned).join(','));
check('B3e 行内带在线/离线状态', r3a.map((r) => r.state).join(',') === '在线,在线,离线',
  r3a.map((r) => r.state).join(','));

await clickRow(rowSel('dev_c', ' .device-order-pin'));
await page.waitForTimeout(150);
const r3b = await rowsInfo();
check('B3f 点「置顶」⇒ 该行进入置顶组（三台全置顶）', r3b.every((r) => r.pinned),
  r3b.map((r) => r.id + ':' + r.pinned).join(' | '));
check('B3g 新置顶排到置顶组**末尾**、不抢第一（第一行仍是 dev_a）', r3b[0].id === 'dev_a' && r3b[2].id === 'dev_c',
  r3b.map((r) => r.id).join(','));

await clickRow(rowSel('dev_a', ' .device-order-pin'));
await page.waitForTimeout(150);
const r3c = await rowsInfo();
check('B3h 点「取消置顶」⇒ 该行移出置顶组（回到未置顶组开头）',
  r3c.map((r) => r.id).join(',') === 'dev_b,dev_c,dev_a' && r3c.map((r) => r.pinned).join(',') === 'true,true,false',
  r3c.map((r) => r.id + ':' + r.pinned).join(' | '));

await clickRow(rowSel('dev_c', ' .device-order-move[data-dir="up"]'));
await page.waitForTimeout(150);
const r3d = await rowsInfo();
check('B3i ↑ 改变置顶组内顺序（dev_c 上移到 dev_b 之前）',
  r3d.map((r) => r.id).join(',') === 'dev_c,dev_b,dev_a', r3d.map((r) => r.id).join(','));
/* ↓ 到组边界不动（无跨组、无越界） */
await clickRow(rowSel('dev_c', ' .device-order-move[data-dir="up"]'));
await page.waitForTimeout(150);
const r3e = await rowsInfo();
check('B3j 置顶组第一条再按 ↑ 不动（不跨组、不越界）', r3e.map((r) => r.id).join(',') === 'dev_c,dev_b,dev_a',
  r3e.map((r) => r.id).join(','));
check('B3k 未置顶行的 ↑/↓ 不生效（dev_a 未置顶）', await (async () => {
  await clickRow(rowSel('dev_a', ' .device-order-move[data-dir="up"]'));
  await page.waitForTimeout(150);
  const now = await rowsInfo();
  return now.map((r) => r.id).join(',') === 'dev_c,dev_b,dev_a';
})(), 'dev_a 未置顶 ⇒ 调序被忽略');

/* B10 截图：admin 弹窗（含置顶行，置顶组 2 台 + 未置顶 1 台） */
await page.screenshot({ path: join(SHOTS, 'APP_admin_设备显示顺序弹窗.png') });
await page.locator('#deviceOrderSheet .modal-sheet').screenshot({ path: join(SHOTS, 'APP_admin_弹窗特写.png') });

/* ======================================================================
   B4 —— 完成 ⇒ POST body.order == 弹窗最终顺序；重渲染顺序 == 响应顺序
   ====================================================================== */
console.log('\n--- B4 保存：POST body 与成功后的重渲染 ---');
const finalRows = await rowsInfo();
const expectOrder = finalRows.filter((r) => r.pinned).map((r) => r.id);
posts.length = 0;
POST_MODE = 'ok';
await page.locator('#deviceOrderSaveBtn').click();
const gotPost = await waitFor(() => posts.length > 0);
const sentOrder = gotPost ? (posts[0] && posts[0].order) : null;
check('B4 点「完成」确实发出 POST /api/admin/device_pin_order', gotPost, 'posts=' + posts.length);
check('B4b POST body.order == 弹窗最终置顶顺序（只带置顶，按序）',
  JSON.stringify(sentOrder) === JSON.stringify(expectOrder),
  'sent=' + JSON.stringify(sentOrder) + ' expect=' + JSON.stringify(expectOrder));
await page.waitForTimeout(500);   // closeModal 200ms + loadClaimingDevices + 重绘
const l4 = await listInfo();
const expectList = ['dev_c', 'dev_b', 'dev_a'];   // 服务端：置顶 dev_c,dev_b → 未置顶 dev_a（唯一）
check('B4c 成功后设备列表按服务端新顺序重渲染', l4.order.join(',') === expectList.join(','), l4.order.join(','));
check('B4d 保存后按新顺序选 pin_index===0 的那台（dev_c）', l4.selected === 'dev_c', 'selected=' + l4.selected);
check('B4e 置顶设备在设备条里有 📌 标记（2 台）', l4.badges === 2, 'badges=' + l4.badges);
const s4 = await sheetState();
check('B4f 保存成功 ⇒ 弹窗关闭且无错误提示', s4.display !== 'flex' && s4.errDisplay === 'none',
  'display=' + s4.display + ' err=' + s4.errDisplay);

/* ======================================================================
   B5 —— 403 ⇒ 可见错误 + 本地顺序回滚 + 不显示成功态
   ====================================================================== */
console.log('\n--- B5 失败分支（桩返回 403）---');
await loadApp({ role: 'admin', pin: ['dev_a', 'dev_b'] });
POST_MODE = '403';
posts.length = 0;
await openSheet();
const serverTruth = (await rowsInfo()).map((r) => r.id).join(',');
await clickRow(rowSel('dev_a', ' .device-order-pin'));     // 取消置顶 ⇒ 本地顺序变成 dev_b,dev_a,dev_c
await page.waitForTimeout(150);
const mutated = (await rowsInfo()).map((r) => r.id).join(',');
check('B5a 本地临时顺序确实被改过（回滚才有判别力）', mutated !== serverTruth,
  'server=' + serverTruth + ' local=' + mutated);
await page.locator('#deviceOrderSaveBtn').click();
const gotPost5 = await waitFor(() => posts.length > 0);
await page.waitForTimeout(400);
const s5 = await sheetState();
const after5 = (await rowsInfo()).map((r) => r.id).join(',');
check('B5b 403 也照样发出了请求（不是被本地闸门吞掉）', gotPost5, 'body=' + JSON.stringify(posts[0] || null));
check('B5c 弹窗内出现可见错误提示「需要管理员权限」',
  s5.errDisplay !== 'none' && /需要管理员权限/.test(s5.errText), 'err=' + s5.errText + ' display=' + s5.errDisplay);
check('B5d 本地顺序已回滚到服务端真值', after5 === serverTruth, 'after=' + after5 + ' server=' + serverTruth);
check('B5e 不显示成功态：弹窗仍开着、按钮回到「完成」可点、无「已保存」提示',
  s5.display === 'flex' && s5.btnText === '完成' && s5.btnDisabled === false && !/已保存/.test(s5.toastText),
  'display=' + s5.display + ' btn=' + s5.btnText + '/' + s5.btnDisabled + ' toast=' + s5.toastText);
check('B5f toast 也给出了失败原因（不静默）', /需要管理员权限/.test(s5.toastText), s5.toastText);
await page.locator('#deviceOrderSheet .modal-sheet').screenshot({ path: join(SHOTS, 'APP_保存失败_错误提示.png') });
POST_MODE = 'ok';

/* ======================================================================
   B6 —— 默认选中：置顶（离线也选）/ 无置顶仍选第一个在线
   ====================================================================== */
console.log('\n--- B6 默认选中口径 ---');
await loadApp({ role: 'admin', pin: ['dev_c'] });          // dev_c 离线且置顶
const l6a = await listInfo();
check('B6a 置顶的是离线设备 ⇒ 默认选它（不偷偷换在线设备）', l6a.selected === 'dev_c',
  'selected=' + l6a.selected + ' order=' + l6a.order.join(','));
check('B6b 列表里确实有在线设备 dev_a 排第 2（判别力：不是"因为没有在线设备才选它"）',
  l6a.order[1] === 'dev_a', l6a.order.join(','));
await page.locator('#deviceSelectCard').screenshot({ path: join(SHOTS, 'APP_置顶设备被默认选中.png') });

const noPins = [
  { client_id: 'dev_a', label: '一号打印机', online: false, auto_duplex: true },
  { client_id: 'dev_b', label: '二号打印机', online: true, auto_duplex: true },
  { client_id: 'dev_c', label: '三号打印机', online: false, auto_duplex: true },
];
/* 无置顶：服务端会把在线设备排前面，「第一个在线」与「第一项」在真实响应里重合 ⇒
   桩**故意按离线在前的顺序原样返回**，才能判别客户端走的是「第一个在线」还是「数组第一项」。 */
await loadApp({ role: 'admin', pin: [], devices: noPins, raw: true });
const l6c = await listInfo();
check('B6c 无置顶 ⇒ 老行为不变：选第一个**在线**设备（dev_b，不理会数组第一项 dev_a 离线）',
  l6c.selected === 'dev_b' && l6c.order[0] === 'dev_a',
  'selected=' + l6c.selected + ' order=' + l6c.order.join(','));
const allOffline = [
  { client_id: 'dev_a', label: '一号打印机', online: false, auto_duplex: true },
  { client_id: 'dev_b', label: '二号打印机', online: false, auto_duplex: true },
];
await loadApp({ role: 'admin', pin: [], devices: allOffline });
const l6d = await listInfo();
check('B6d 无置顶且一台都不在线 ⇒ 回落到第一台（老行为兜底，不选空）', l6d.selected === 'dev_a',
  'selected=' + l6d.selected);

/* ======================================================================
   B7 —— 手动选择不被抢；设备消失后回落置顶第一项
   ====================================================================== */
console.log('\n--- B7 手动选择不被 30s 轮询/刷新抢回 ---');
await loadApp({ role: 'admin', pin: ['dev_c'] });
const l7a = await listInfo();
check('B7a 起点：默认选中置顶的 dev_c', l7a.selected === 'dev_c', 'selected=' + l7a.selected);
await page.locator('#deviceList .device-item[data-id="dev_b"]').click();
await page.waitForTimeout(150);
const l7b = await listInfo();
const chosenFlag = await page.evaluate(() => deviceChosenByUser === true);
check('B7b 用户手点 dev_b ⇒ 选中切换且置「手动选择」状态位', l7b.selected === 'dev_b' && chosenFlag,
  'selected=' + l7b.selected + ' deviceChosenByUser=' + chosenFlag);
await page.evaluate(() => loadClaimingDevices());          // 等同 30s 轮询那一轮
await page.waitForTimeout(300);
const l7c = await listInfo();
check('B7c 刷新一轮后仍是用户点的那台（不被默认口径抢回）', l7c.selected === 'dev_b', 'selected=' + l7c.selected);
await page.evaluate(() => loadClaimingDevices());
await page.waitForTimeout(300);
const l7d = await listInfo();
check('B7d 再刷新一轮仍保持', l7d.selected === 'dev_b', 'selected=' + l7d.selected);
DEVICES = BASE_DEVICES.filter((d) => d.client_id !== 'dev_b');   // 用户点的那台不再可接单
await page.evaluate(() => loadClaimingDevices());
await page.waitForTimeout(300);
const l7e = await listInfo();
check('B7e 那台设备从列表消失后再刷新 ⇒ 回落置顶第一项（dev_c）', l7e.selected === 'dev_c', 'selected=' + l7e.selected);
DEVICES = BASE_DEVICES.slice();

/* ======================================================================
   收尾：外部网络 & 小程序同构几何（置顶徽标不撑高设备条）
   ====================================================================== */
check('全程未触碰生产/外部网络（BASE_URL 已隔离到本地桩）', foreign.length === 0, foreign.slice(0, 3).join(', '));

console.log('\n--- 小程序同构几何（真实 index.wxss，1rpx = 0.5px / 375px 基准）---');
const CSS_IDX = await readFile(MP_DIR + '/pages/index/index.wxss', 'utf8');
const CSS_APP = await readFile(MP_DIR + '/app.wxss', 'utf8');
const decl = (css, selector) => {
  const esc2 = selector.replace(/[.*+?^$()|[\]\\]/g, '\\$&');
  const m = css.match(new RegExp('(?:^|\n)\\s*' + esc2 + '\\s*\\{([^}]*)\\}'));
  return m ? m[1].trim() : null;
};
const rpx2px = (d) => d.replace(/(-?\d*\.?\d+)rpx/g, (_, n) => (parseFloat(n) * 0.5) + 'px');
const SEL = ['.device-item', '.device-dot', '.device-label', '.device-pin-badge', '.device-state', '.device-item-on'];
const parts = [];
const missing = [];
for (const sel of SEL) {
  const d = decl(CSS_IDX, sel);
  if (!d) missing.push(sel); else parts.push(sel + '{' + rpx2px(d) + '}');
}
check('同构：真实 wxss 里 6 条规则全部抽到（没有凭空造样式）', missing.length === 0, missing.join(', '));

const VARS = ':root{--bg-card:#fff;--text-primary:#000;--text-secondary:#666;--text-tertiary:#999;'
  + '--blue:#007AFF;--green:#34C759;--red:#FF3B30;--gray3:#c7c7cc;--gray4:#e5e5ea;--separator:#e5e5ea;'
  + '--radius-md:8px;--radius-sm:8px}';
const rowHtml = (badge, style) => '<div class="device-item device-item-on">'
  + '<div class="device-dot device-dot-on"></div>'
  + '<span class="device-label">一号打印机</span>'
  + (badge ? '<span class="device-pin-badge"' + (style ? ' style="' + style + '"' : '') + '>📌 置顶</span>' : '')
  + '<span class="device-state device-state-on">在线</span></div>';
const isoHtml = '<!doctype html><html><head><meta charset="utf-8"><style>'
  + VARS + 'body{margin:0;font-family:-apple-system,"PingFang SC",sans-serif;width:375px}'
  + parts.join('\n') + '</style></head><body>'
  + rowHtml(false) + rowHtml(true) + rowHtml(true, 'font-size:20px')   /* 第 3 行 = 变异自证：徽标被撑大 */
  + '</body></html>';
const iso = await browser.newPage({ viewport: { width: 375, height: 812 } });
await iso.setContent(isoHtml, { waitUntil: 'load' });
await iso.waitForTimeout(120);
const isoM = await iso.evaluate(() => {
  const rows = Array.from(document.querySelectorAll('.device-item'));
  return rows.map((r) => {
    const cs = getComputedStyle(r);
    const badge = r.querySelector('.device-pin-badge');
    return {
      h: +r.getBoundingClientRect().height.toFixed(2),
      minH: parseFloat(cs.minHeight),
      wrap: cs.flexWrap,
      badgeFont: badge ? parseFloat(getComputedStyle(badge).fontSize) : null,
      stateFont: parseFloat(getComputedStyle(r.querySelector('.device-state')).fontSize),
    };
  });
});
const [plain, badged, inflated] = isoM;
console.log('  无徽标 ' + plain.h + 'px（min-height ' + plain.minH + 'px）; 有徽标 ' + badged.h
  + 'px; 徽标字号 ' + badged.badgeFont + 'px vs 状态 ' + badged.stateFont + 'px; 变异行(20px 徽标) ' + inflated.h + 'px');
check('同构：置顶徽标不撑高设备条（有/无徽标高度相同 ±0.5px）', Math.abs(plain.h - badged.h) <= 0.5,
  plain.h + ' vs ' + badged.h);
check('同构：设备条仍是 nowrap 单行（高度 ≈ 一行，< 2×min-height）', plain.wrap === 'nowrap' && plain.h < plain.minH * 2,
  'wrap=' + plain.wrap + ' h=' + plain.h + ' minH=' + plain.minH);
check('同构：徽标字号与 .device-state 同档（±0.5px）', Math.abs(badged.badgeFont - badged.stateFont) <= 0.5,
  badged.badgeFont + ' vs ' + badged.stateFont);
check('同构自证：把徽标字号撑到 20px 时设备条确实变高（这条断言有判别力）', inflated.h > badged.h + 0.5,
  badged.h + ' vs ' + inflated.h);

await browser.close();
server.close();
console.log('\n' + '='.repeat(64));
console.log('接单设备显示顺序（置顶）· 渲染验收:', failures === 0 ? '全部通过' : '存在 ' + failures + ' 项失败');
console.log('截图目录: ' + SHOTS);
process.exit(failures ? 1 : 0);
