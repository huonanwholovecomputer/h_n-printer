/* ui_preview_test.mjs — 本地冒烟测试：静态服务 + mock 接口，渲染并截图关键视图
 * 用法: node tools/ui_preview_test.mjs
 * 输出: C:/Users/Administrator/.codex/visualizations/2026/08/12/019ff495-9242-7a43-b9b1-45125c7cb2f9/shots/
 */
import { createRequire } from 'node:module';
import http from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const APP_DIR = 'D:/打印机项目/mobile_apps/android_app';
const SHOTS = 'C:/Users/Administrator/.codex/visualizations/2026/08/12/019ff495-9242-7a43-b9b1-45125c7cb2f9/shots';
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.json': 'application/json',
};

const mockUsers = [
  {
    openid: 'openid_zhangsan', nickname: '张三', avatar_url: '', license_type: 'temp', status: 'active',
    openid_short: 'zhang***', order_count: 3, last_order: '2026-08-11 09:30',
    keys: [
      { key: 'XYZ78901', type: 'temp', status: 'used', created_at: '2026-08-01 10:00', used_at: '2026-08-02 09:00', expires_at: '2026-08-02 10:00', order_id: 42 },
      { key: 'AAA11111', type: 'temp', status: 'revoked', created_at: '2026-07-20 12:00', used_at: '', expires_at: '2026-07-20 12:10', order_id: null },
    ],
  },
  {
    openid: 'openid_lisi', nickname: '李四', avatar_url: '', license_type: 'both', status: 'expired',
    openid_short: 'lisi***', order_count: 0, last_order: '',
    keys: [],
  },
];

const mockOrder = {
  id: 42, order_number: 'HN202608120001', status: 'queued', file_summary: '文档.pdf 等 2 个文件',
  total_pages: 12, total_copies: 3, created_at: '2026-08-12 10:00', total_price: 3.6,
  urgency: '高', urgency_price: 1, cover_page: 1, cover_page_price: 0.1, delivery_enabled: false,
  license_info: { key: 'XYZ78901', used_at: '2026-08-12 10:01', expires_at: '2026-08-12 10:10' },
  files: [
    { original_name: '文档.pdf', file_name: '文档.pdf', size: 123456, file_type: 'pdf', copies: 3, page_count: 12, duplex: 'on', page_range: '1-12', status: 'queued' },
    { original_name: '表格.xlsx', file_name: '表格.xlsx', size: 20480, file_type: 'xlsx', copies: 1, page_count: 0, duplex: '', page_range: '', status: 'queued' },
  ],
};

const mockUserDetail = {
  success: true, role: 'user', nickname: '张三', is_super: false, avatar_url: '',
  license_info: {
    key: 'XYZ78901', type: 'temp', expired: false, created_at: '2026-08-01 10:00', used_at: '2026-08-02 09:00',
    expires_at: '2026-08-02 10:00', validity_minutes: 60, creator_nickname: '测试管理员',
  },
};

let mockKeys = [];
const newKey = () => ({ key: 'ABC' + String(12345 + mockKeys.length * 11111), status: 'unused', type: 'temp', expires_at: '2099-01-01 00:00' });

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  const path = url.pathname;
  if (path === '/favicon.ico') {
    res.writeHead(204);
    res.end();
    return;
  }
  const send = (obj, status = 200) => {
    res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify(obj));
  };
  if (path.startsWith('/api/')) {
    if (path === '/api/device_login') return send({ success: true, token: 'mock-token', openid: 'mock-openid' });
    if (path === '/api/me') return send({ success: true, role: 'admin', is_super_admin: true, license_info: null, has_temp_access: false, temp_until: '' });
    if (path === '/api/profile') return send({ success: true, nickname: '测试管理员', avatar_url: '' });
    if (path === '/api/pricing') return send({ success: true, data: {} });
    if (path === '/api/printer_status') return send({ success: true, online: false });
    if (path === '/api/upload') return send({ success: true, file_id: 'mock-file-' + Math.floor(Math.random() * 1e6), page_count: 3 });
    if (path === '/api/license/active') return send({ success: true, keys: mockKeys });
    if (path === '/api/license/revoke') return send({ success: true });
    if (path === '/api/license/create') { mockKeys.push(newKey()); return send({ success: true }); }
    if (path === '/api/authorized_users') return send({ success: true, users: mockUsers });
    if (path === '/api/orders') return send({ success: true, orders: [mockOrder], total: 1 });
    if (path === '/api/admin/user_detail') return send(mockUserDetail);
    if (path === '/api/admin/temp_users') return send({ success: true, users: [] });
    if (path === '/api/admin/admins') return send({ success: true, admins: [{ openid: 'openid_admin1', nickname: '管理员甲', is_super: false }] });
    if (path === '/api/storage_stats') return send({ success: true, data: { total_files: 12, total_size_display: '1.2 MB' }, retention_days: 7, retention_hours: 0 });
    if (path === '/api/security_config') return send({ success: true, data: {} });
    return send({ success: true });
  }
  try {
    let p = decodeURIComponent(path);
    if (p === '/') p = '/index.html';
    const file = join(APP_DIR, normalize(p).replace(/^([/\\])+/, ''));
    const data = await readFile(file);
    res.writeHead(200, { 'Content-Type': MIME[extname(file)] || 'application/octet-stream' });
    res.end(data);
  } catch {
    res.writeHead(404);
    res.end('not found');
  }
});

await new Promise((r) => server.listen(0, '127.0.0.1', r));
const port = server.address().port;

const browser = await chromium.launch({ executablePath: CHROME, headless: true });
const page = await browser.newPage({ viewport: { width: 430, height: 920 }, hasTouch: true });
const cdp = await page.context().newCDPSession(page);
const errors = [];
page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

await page.addInitScript((p) => {
  localStorage.setItem('hn_base_url', 'http://127.0.0.1:' + p);
  localStorage.setItem('hn_token', 'mock-token');
  localStorage.setItem('hn_openid', 'mock-openid');
  localStorage.setItem('hn_role', 'admin');
  localStorage.setItem('hn_is_super', '1');
  localStorage.setItem('hn_nickname', '测试管理员');
}, port);

await mkdir(SHOTS, { recursive: true });
await page.goto(`http://127.0.0.1:${port}/index.html`);
await page.waitForTimeout(1000);
const entranceState = await page.evaluate(() => ({
  animating: [...document.querySelectorAll('#page-print .card-entering-fade')].map(el => el.id).sort(),
  delays: ['headerArea', 'printerStatus', 'fileSection', 'extParamsCard', 'autoPrintSection', 'submitArea'].map(id => {
    const el = document.getElementById(id);
    return el ? getComputedStyle(el).animationDelay : null;
  }),
}));
console.log('入场动画(首次启动):', JSON.stringify(entranceState));
console.log('  6元素全入场:', entranceState.animating.length === 6 ? '✓' : '✗');
console.log('  延迟 0.5/0.6/0.7/0.8/0.9/1.0s:', JSON.stringify(entranceState.delays) === JSON.stringify(['0.5s', '0.6s', '0.7s', '0.8s', '0.9s', '1s']) ? '✓' : '✗ ' + JSON.stringify(entranceState.delays));
await page.waitForTimeout(1800);
const entranceCleared = await page.evaluate(() => ({
  remainingAnim: document.querySelectorAll('#page-print .card-entering-fade').length,
  remainingPreload: document.querySelectorAll('#page-print .card-preload').length,
}));
console.log('入场动画清理:', JSON.stringify(entranceCleared), entranceCleared.remainingAnim === 0 && entranceCleared.remainingPreload === 0 ? '✓' : '✗');
const floatNav = await page.evaluate(() => {
  const nav = document.getElementById('navBar');
  const navR = nav.getBoundingClientRect();
  const logoTop = document.getElementById('logoWrap').getBoundingClientRect().top;
  const eng = scrollEngines.print;
  eng.y = 300;
  eng.applyY();
  const logoTopScrolled = document.getElementById('logoWrap').getBoundingClientRect().top;
  eng.y = 0;
  eng.applyY();
  return {
    navPos: getComputedStyle(nav).position,
    navBottom: Math.round(navR.bottom),
    logoBelowNav: logoTop >= navR.bottom,
    logoUnderNavWhenScrolled: logoTopScrolled < navR.bottom,
  };
});
console.log('导航悬浮:', JSON.stringify(floatNav), floatNav.navPos === 'absolute' && floatNav.logoBelowNav && floatNav.logoUnderNavWhenScrolled ? '✓' : '✗');
await page.screenshot({ path: join(SHOTS, '01-print.png') });

// 文件选择与格式校验（docx 应通过，xlsx 应提示不支持但加入，xyz 应被拦截）
await page.setInputFiles('#fileInput', [
  { name: '测试文档.docx', mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer: Buffer.from('mock docx') },
  { name: '表格.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: Buffer.from('mock xlsx') },
  { name: '未知.xyz', mimeType: 'application/octet-stream', buffer: Buffer.from('xyz') },
]);
await page.waitForTimeout(1500);
const fileTest = await page.evaluate(() => ({
  names: printState.selectedFiles.map(f => f.name),
  count: printState.selectedFiles.length,
  docxStatus: (printState.selectedFiles.find(f => f.name.endsWith('.docx')) || {}).pageCountStatus,
  xlsxWarning: (printState.selectedFiles.find(f => f.name.endsWith('.xlsx')) || {}).excelWarning,
  listText: (document.getElementById('fileList').innerText || '').replace(/\n+/g, ' | ').slice(0, 220),
}));
console.log('文件选择:', JSON.stringify(fileTest));
const themeOnPrint = await page.evaluate(() => document.getElementById('themeToggle').style.display);
console.log('主题按钮(打印页):', themeOnPrint);

// 无障碍打印 → 时间选择器布局（横排）
await page.evaluate(() => openScheduleTimePicker());
await page.waitForTimeout(300);
const timeLayout = await page.evaluate(() => {
  const row = document.querySelector('#scheduleTimeModal .time-wheel-row');
  const h = document.getElementById('hourWheel').getBoundingClientRect();
  const m = document.getElementById('minuteWheel').getBoundingClientRect();
  return {
    display: getComputedStyle(row).display,
    flexDirection: getComputedStyle(row).flexDirection,
    hour: { x: Math.round(h.x), y: Math.round(h.y), w: Math.round(h.width) },
    minute: { x: Math.round(m.x), y: Math.round(m.y), w: Math.round(m.width) },
  };
});
console.log('时间选择器布局:', JSON.stringify(timeLayout));
await page.screenshot({ path: join(SHOTS, '08-time-picker.png') });

await page.evaluate(() => openScheduleCountdownPicker());
await page.waitForTimeout(300);
const cdLayout = await page.evaluate(() => {
  const row = document.querySelector('#scheduleCountdownModal .time-wheel-row');
  const a = document.getElementById('countdownMinuteWheel').getBoundingClientRect();
  const b = document.getElementById('countdownSecondWheel').getBoundingClientRect();
  return {
    display: getComputedStyle(row).display,
    flexDirection: getComputedStyle(row).flexDirection,
    minute: { x: Math.round(a.x), y: Math.round(a.y) },
    second: { x: Math.round(b.x), y: Math.round(b.y) },
  };
});
console.log('倒计时选择器布局:', JSON.stringify(cdLayout));
await page.evaluate(() => closeModal('scheduleTimeModal'));
await page.evaluate(() => closeModal('scheduleCountdownModal'));

// 开始方式卡片：指定时间/倒计时均为无标签并排瓷块（对齐小程序）
const sched = await page.evaluate(() => new Promise((resolve) => {
  printState.autoPrintEnabled = true;
  printState.scheduleMode = 'at';
  updateScheduleUI();
  setTimeout(() => {
    const day = document.getElementById('scheduleDayTrigger').getBoundingClientRect();
    const time = document.getElementById('scheduleTimeTrigger').getBoundingClientRect();
    resolve({
      day: { x: Math.round(day.x), y: Math.round(day.y), w: Math.round(day.width) },
      time: { x: Math.round(time.x), y: Math.round(time.y), w: Math.round(time.width) },
      labels: document.querySelectorAll('.schedule-label').length,
    });
  }, 400);
}));
console.log('开始方式-指定时间:', JSON.stringify(sched));

const cdSched = await page.evaluate(() => new Promise((resolve) => {
  setScheduleMode('countdown');
  setTimeout(() => {
    const a = document.getElementById('scheduleCountdownMinTrigger').getBoundingClientRect();
    const b = document.getElementById('scheduleCountdownSecTrigger').getBoundingClientRect();
    resolve({
      min: { x: Math.round(a.x), y: Math.round(a.y), w: Math.round(a.width) },
      sec: { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width) },
      text: document.getElementById('scheduleCountdownMinValue').textContent +
        document.querySelector('#scheduleCountdownMinTrigger .countdown-unit').textContent + ' | ' +
        document.getElementById('scheduleCountdownSecValue').textContent +
        document.querySelector('#scheduleCountdownSecTrigger .countdown-unit').textContent,
    });
  }, 600);
}));
console.log('开始方式-倒计时:', JSON.stringify(cdSched));
await page.screenshot({ path: join(SHOTS, '09-schedule-card.png') });

await page.evaluate(() => document.getElementById('scheduleCountdownMinTrigger').click());
await page.waitForTimeout(300);
const cdModalVisible = await page.evaluate(() => document.getElementById('scheduleCountdownModal').style.display);
console.log('倒计时弹窗 display:', cdModalVisible);
await page.evaluate(() => closeModal('scheduleCountdownModal'));

const tabBarVisible = await page.evaluate(() => {
  const bar = document.getElementById('tabBar');
  const r = bar.getBoundingClientRect();
  return `tabBar rect=${JSON.stringify({ x: r.x, y: r.y, w: r.width, h: r.height })} display=${getComputedStyle(bar).display} printDisplay=${document.getElementById('page-print').style.display}`;
});
console.log('诊断:', tabBarVisible);

await page.evaluate(() => document.querySelector('.tab-item[data-tab="me"]').click());
await page.waitForTimeout(900);
await page.screenshot({ path: join(SHOTS, '02-me.png') });
const themeOnMe = await page.evaluate(() => document.getElementById('themeToggle').style.display);
console.log('主题按钮(我页):', themeOnMe);

const meText = await page.evaluate(() => (document.getElementById('page-me').innerText || '').slice(0, 300));
console.log('我页文本:', meText.replace(/\n+/g, ' | '));

// 生成动画：初始无密钥 → 点击「生成许可密钥」→ 第一把密钥也应播放入场动画
const initialEmpty = await page.evaluate(() => document.getElementById('activeKeys').innerText.trim() === '');
console.log('初始无密钥(无占位卡片):', initialEmpty ? '✓' : '✗');
await page.evaluate(() => document.getElementById('generateKeyBtn').click());
await page.waitForTimeout(500);
const genState = await page.evaluate(() => ({
  entering: document.querySelectorAll('#activeKeys .key-card-wrap.key-entering').length,
  keys: [...document.querySelectorAll('#activeKeys [data-key-swipe]')].map(c => c.dataset.keySwipe),
  animation: (() => { const el = document.querySelector('#activeKeys .key-card-wrap.key-entering .key-card'); return el ? getComputedStyle(el).transition : null; })(),
}));
console.log('生成动画(第一把密钥):', JSON.stringify(genState), genState.entering === 1 && genState.keys.length === 1 ? '✓ 入场动画' : '✗ 直接出现');
await page.waitForTimeout(1000);
const enterCleared = await page.evaluate(() => document.querySelectorAll('#activeKeys .key-card-wrap.key-entering').length);
console.log('入场动画清除:', enterCleared === 0 ? '✓' : '✗');

// 第二把密钥（服务端新增 → 重新加载）也应播放入场
mockKeys.push({ key: 'DEF67890', status: 'unused', type: 'temp', expires_at: '2099-01-01 00:00' });
await page.evaluate(() => loadActiveKeys());
await page.waitForTimeout(200);
const enter2 = await page.evaluate(() => ({
  entering: document.querySelectorAll('#activeKeys .key-card-wrap.key-entering').length,
  keys: [...document.querySelectorAll('#activeKeys [data-key-swipe]')].map(c => c.dataset.keySwipe),
}));
console.log('第二把密钥动画:', JSON.stringify(enter2), enter2.entering === 1 && enter2.keys.includes('DEF67890') ? '✓' : '✗');
await page.waitForTimeout(1000);

// 许可密钥卡片：左滑露出作废按钮（鼠标拖动模拟 Pointer Events）
const keyCard = await page.evaluate(() => {
  const card = document.querySelector('[data-key-swipe]');
  if (!card) return null;
  const r = card.getBoundingClientRect();
  return { x0: Math.round(r.left + r.width - 15), y: Math.round(r.top + r.height / 2), x1: Math.round(r.left + r.width - 165) };
});
if (keyCard) {
  await page.mouse.move(keyCard.x0, keyCard.y);
  await page.mouse.down();
  await page.mouse.move(keyCard.x0 - 60, keyCard.y, { steps: 4 });
  await page.mouse.move(keyCard.x0 - 130, keyCard.y, { steps: 4 });
  await page.mouse.move(keyCard.x1, keyCard.y, { steps: 4 });
  await page.mouse.up();
  await page.waitForTimeout(400);
  const swipeState = await page.evaluate(() => {
    const card = document.querySelector('[data-key-swipe]');
    const del = card.parentElement.querySelector('.key-delete');
    return { transform: card.style.transform, deleteOpacity: del.style.opacity, deleteW: del.offsetWidth };
  });
  console.log('密钥卡片左滑:', JSON.stringify(swipeState));
  const slidePx = parseFloat((/translateX\((-?[\d.]+)px\)/.exec(swipeState.transform) || [])[1] || '0');
  console.log('滑动距离 vs 按钮宽度:', slidePx, 'vs', swipeState.deleteW, Math.abs(slidePx + swipeState.deleteW) <= 2 ? '✓ 距离=按钮宽度' : '✗ 距离不匹配');

  // 展开态右滑 30px：不应瞬移到原位，而应跟随到 -50 左右
  const rightDrag = await page.evaluate(() => {
    const card = document.querySelector('[data-key-swipe]');
    const r = card.getBoundingClientRect();
    return { x0: Math.round(r.left + r.width - 15), y: Math.round(r.top + r.height / 2) };
  });
  await page.mouse.move(rightDrag.x0, rightDrag.y);
  await page.mouse.down();
  await page.mouse.move(rightDrag.x0 + 30, rightDrag.y, { steps: 6 });
  const midTransform = await page.evaluate(() => document.querySelector('[data-key-swipe]').style.transform);
  await page.mouse.up();
  await page.waitForTimeout(400);
  const afterUp = await page.evaluate(() => document.querySelector('[data-key-swipe]').style.transform);
  console.log('展开态右滑30px 过程:', midTransform, '松手后:', afterUp);
  const midX = parseFloat((/translateX\((-?[\d.]+)px\)/.exec(midTransform) || [])[1] || '0');
  console.log('  ', midX > -60 && midX < -30 ? '✓ 无瞬移（跟随到 -50 附近）' : '✗ 位置异常');

  // 触摸短按作废按钮（click 可能被吞，pointerup 必须触发）
  const delPos = await page.evaluate(() => {
    const del = document.querySelector('.key-delete');
    const r = del.getBoundingClientRect();
    return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
  });
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: delPos.x, y: delPos.y }] });
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await page.waitForTimeout(250);
  console.log('作废确认弹窗:', await page.evaluate(() => document.getElementById('confirmModal').style.display));
  await page.evaluate(() => document.getElementById('confirmCancel').click());
  await page.waitForTimeout(300);
  await page.evaluate(() => {
    const card = document.querySelector('[data-key-swipe]');
    if (card) { card.style.transition = 'none'; card.style.transform = 'translateX(0)'; }
  });
  await page.waitForTimeout(200);
  // 确认作废 → 销毁动画：先收起，再 key-exiting，350ms 后移除
  await page.evaluate(() => {
    const del = document.querySelector('.key-delete');
    del.click();
  });
  await page.waitForTimeout(300);
  await page.evaluate(() => document.getElementById('confirmOk').click());
  await page.waitForTimeout(120);
  const collapseState = await page.evaluate(() => {
    const card = document.querySelector('[data-key-swipe="ABC12345"]');
    return card ? { transform: card.style.transform, delOpacity: card.parentElement.querySelector('.key-delete').style.opacity } : null;
  });
  console.log('作废后收起:', JSON.stringify(collapseState));
  await page.waitForTimeout(400);
  const exitingState = await page.evaluate(() => {
    const card = document.querySelector('[data-key-swipe="ABC12345"]');
    return card ? { wrapClass: card.parentElement.className } : null;
  });
  console.log('离场动画类:', exitingState && String(exitingState.wrapClass).includes('key-exiting') ? '✓ key-exiting' : '✗');
  await page.waitForTimeout(500);
  const removedState = await page.evaluate(() => ({
    cardGone: !document.querySelector('[data-key-swipe="ABC12345"]'),
    remaining: [...document.querySelectorAll('#activeKeys [data-key-swipe]')].map(c => c.dataset.keySwipe),
  }));
  console.log('销毁移除:', JSON.stringify(removedState), removedState.cardGone ? '✓ 已移除' : '✗');
}

await page.evaluate(() => document.getElementById('btnAuthorizedUsers').click());
await page.waitForTimeout(900);
await page.screenshot({ path: join(SHOTS, '03-authorized.png') });
const themeOnSubview = await page.evaluate(() => document.getElementById('themeToggle').style.display);
console.log('主题按钮(历史授权子页):', themeOnSubview);

const authText = await page.evaluate(() => document.getElementById('authorizedUserList').innerText);
console.log('授权用户页文本:', authText.replace(/\n+/g, ' | '));

await page.evaluate(() => document.querySelector('[data-toggle-records]').click());
await page.waitForTimeout(500);
await page.screenshot({ path: join(SHOTS, '04-authorized-expanded.png') });

await page.evaluate(() => document.querySelector('.user-card-main').click());
await page.waitForTimeout(1100);
await page.screenshot({ path: join(SHOTS, '05-user-orders.png') });

const uoText = await page.evaluate(() => document.getElementById('view-user-orders').innerText);
console.log('用户订单页文本:', uoText.replace(/\n+/g, ' | '));

await page.evaluate(() => document.querySelector('#view-user-orders [data-order-id]').click());
await page.waitForTimeout(400);
const uoDetailText = await page.evaluate(() => document.querySelector('#view-user-orders [data-order-id]').innerText);
console.log('用户订单展开详情:', uoDetailText.replace(/\n+/g, ' | '));

await page.evaluate(() => document.getElementById('uoLicenseBadge').click());
await page.waitForTimeout(500);
await page.screenshot({ path: join(SHOTS, '06-user-orders-license.png') });

const uoLicenseText = await page.evaluate(() => document.getElementById('uoLicenseDetail').innerText);
console.log('许可详情文本:', uoLicenseText.replace(/\n+/g, ' | '));

await page.evaluate(() => document.querySelector('.tab-item[data-tab="me"]').click());
await page.waitForTimeout(700);
await page.evaluate(() => document.getElementById('nicknameRow').click());
await page.waitForTimeout(500);
await page.screenshot({ path: join(SHOTS, '07-nickname-modal.png') });
const nickModalVisible = await page.evaluate(() => document.getElementById('nicknameModal').style.display);
console.log('昵称弹窗 display:', nickModalVisible);

await page.evaluate(() => document.getElementById('nicknameModalSave').click());
await page.evaluate(() => document.querySelector('.tab-item[data-tab="me"]').click());
await page.waitForTimeout(900);
const meOrdersText = await page.evaluate(() => (document.getElementById('orderList').innerText || '').replace(/\n+/g, ' | '));
console.log('我页订单列表:', meOrdersText);

// 管理管理员：点击管理员卡片 → 打开该管理员任务页（顶部用户卡片 + 许可徽章 → 点击展开密钥详情）
await page.evaluate(() => document.querySelector('[data-admin-openid]').click());
await page.waitForTimeout(1100);
const adminCardState = await page.evaluate(() => ({
  viewVisible: document.getElementById('view-user-orders').style.display !== 'none',
  badge: document.getElementById('uoLicenseBadge').style.display,
  title: document.getElementById('navTitle').textContent,
}));
console.log('管理员卡片点击:', JSON.stringify(adminCardState), adminCardState.viewVisible && adminCardState.badge === '' ? '✓ 打开任务页+许可徽章' : '✗');
await page.evaluate(() => document.getElementById('uoLicenseBadge').click());
await page.waitForTimeout(300);
const adminLicDetail = await page.evaluate(() => document.getElementById('uoLicenseDetail').innerText.replace(/\n+/g, ' | '));
console.log('管理员许可详情:', adminLicDetail.slice(0, 120));
console.log('  ', adminLicDetail.includes('当前状态') && adminLicDetail.includes('密钥类型') && adminLicDetail.includes('授权人') ? '✓ 详情已展开' : '✗');

// 切回打印 tab：入场动画不应重播
await page.evaluate(() => document.querySelector('.tab-item[data-tab="print"]').click());
await page.waitForTimeout(800);
const noRetrigger = await page.evaluate(() => document.querySelectorAll('#page-print .card-entering-fade').length);
console.log('切回打印tab(动画不重播):', noRetrigger === 0 ? '✓' : '✗ 重播了 ' + noRetrigger);

console.log('JS 错误:', errors.length ? errors.join('\n') : '无');
await browser.close();
server.close();
