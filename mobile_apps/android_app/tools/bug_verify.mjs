/* bug_verify.mjs — 13 个 BUG 的针对性验证脚本（渲染 + 计算样式 + 几何测量）
 * 用法: node tools/bug_verify.mjs
 */
import { createRequire } from 'node:module';
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const APP_DIR = 'D:/打印机项目/mobile_apps/android_app/www';
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.json': 'application/json',
};

// ---- mock 数据 ----
const manyOrders = Array.from({ length: 25 }, (_, i) => ({
  id: 100 + i,
  order_number: 'HN202608' + String(100 + i),
  status: i % 3 === 0 ? 'sent' : (i % 3 === 1 ? 'queued' : 'canceled'),
  file_summary: '文档' + i + '.pdf 等文件',
  total_pages: 5 + i, total_copies: 2, created_at: '2026-08-12 10:' + String(i % 60).padStart(2, '0'),
  total_price: (i * 0.7 + 1.2).toFixed(2),
  files: [],
}));

const mockUsers = [
  {
    openid: 'openid_lisi', nickname: '李四', avatar_url: '', license_type: 'both', status: 'expired',
    openid_short: 'lisi***', order_count: 25, last_order: '2026-08-11 09:30',
    records: [
      { key: 'ADMIN0001', type: 'admin', status: 'used', created_at: '2026-07-01 10:00', used_at: '2026-07-01 10:01', expires_at: '', order_id: null },
      { key: 'XYZ78901', type: 'temp', status: 'used', created_at: '2026-08-01 10:00', used_at: '2026-08-02 09:00', expires_at: '2026-08-02 10:00', order_id: 42 },
    ],
  },
  {
    openid: 'openid_zhangsan', nickname: '张三', avatar_url: '', license_type: 'temp', status: 'active',
    openid_short: 'zhang***', order_count: 3, last_order: '2026-08-11 09:30',
    records: [
      { key: 'XYZ78901', type: 'temp', status: 'used', created_at: '2026-08-01 10:00', used_at: '2026-08-02 09:00', expires_at: '2026-08-02 10:00', order_id: 42 },
    ],
  },
];

let mockKeys = [];
const newKey = () => ({ key: 'ABC' + String(12345 + mockKeys.length * 11111), status: 'unused', type: 'temp', expires_at: '2099-01-01 00:00' });
mockKeys.push(newKey());

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  const path = url.pathname;
  if (path === '/favicon.ico') { res.writeHead(204); res.end(); return; }
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
    if (path === '/api/upload') return send({ success: true, file_id: 'mock-file-1', page_count: 3 });
    if (path === '/api/license/active') return send({ success: true, keys: mockKeys });
    if (path === '/api/license/revoke') return send({ success: true });
    if (path === '/api/license/create') { mockKeys.push(newKey()); return send({ success: true }); }
    if (path === '/api/authorized_users') return send({ success: true, users: mockUsers });
    if (path === '/api/orders') {
      const per = parseInt(url.searchParams.get('per_page') || '10', 10);
      const page = parseInt(url.searchParams.get('page') || '1', 10);
      const total = manyOrders.length;
      const start = (page - 1) * per;
      return send({ success: true, orders: manyOrders.slice(start, start + per), total });
    }
    if (path === '/api/admin/user_detail') return send({ success: true, role: 'user', nickname: '李四', is_super: false, avatar_url: '', license_info: null });
    if (path === '/api/admin/temp_users') return send({ success: true, users: [] });
    if (path === '/api/admin/admins') return send({ success: true, admins: [] });
    if (path === '/api/storage_stats') return send({ success: true, data: { total_files: 1, total_size_display: '1.2 MB' }, retention_days: 7, retention_hours: 0 });
    if (path === '/api/security_config') return send({ success: true, data: {} });
    if (path === '/api/bind/devices') return send({ success: true, devices: [{ dev_openid: 'dev_abc', nickname: '我的手机', used_at: '2026-08-01 10:00' }] });
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

await page.goto(`http://127.0.0.1:${port}/index.html`);
await page.waitForTimeout(1500);

const results = [];
const check = (name, ok, detail) => { results.push({ name, ok, detail }); console.log((ok ? '✓' : '✗') + ' ' + name + (detail ? ' — ' + detail : '')); };

// ========== BUG 1: 主题切换按钮位置 ==========
await page.evaluate(() => document.querySelector('.tab-item[data-tab="me"]').click());
await page.waitForTimeout(800);
const b1 = await page.evaluate(() => {
  const toggle = document.getElementById('themeToggle');
  const nav = document.getElementById('navBar');
  const t = toggle.getBoundingClientRect();
  const n = nav.getBoundingClientRect();
  return { toggleTop: Math.round(t.top), toggleBottom: Math.round(t.bottom), navBottom: Math.round(n.bottom), covered: t.top < n.bottom };
});
check('BUG1 主题按钮在导航栏下方', !b1.covered && b1.toggleTop >= b1.navBottom, JSON.stringify(b1));

// ========== BUG 2: 头像为 img 且可显示真实图片 ==========
const b2 = await page.evaluate(() => {
  const img = document.getElementById('avatarImg');
  return { tag: img.tagName, srcPrefix: img.getAttribute('src').slice(0, 40) };
});
check('BUG2 头像元素为 <img>', b2.tag === 'IMG' && b2.srcPrefix.startsWith('data:image/svg'), b2.tag + ' ' + b2.srcPrefix);

// ========== BUG 3: 复制按钮宽且居中 ==========
await page.evaluate(() => document.getElementById('generateKeyBtn').click());
await page.waitForTimeout(600);
const b3 = await page.evaluate(() => {
  const btn = document.querySelector('#activeKeys .key-actions .copy-btn');
  if (!btn) return null;
  const r = btn.getBoundingClientRect();
  const actions = btn.closest('.key-actions').getBoundingClientRect();
  return { w: Math.round(r.width), centered: Math.abs((r.left + r.width / 2) - (actions.left + actions.width / 2)) < 8, countdown: (document.querySelector('.key-countdown') || {}).textContent };
});
check('BUG3 复制按钮宽(≈184px)且居中', !!b3 && b3.w >= 170 && b3.centered, JSON.stringify(b3));

// ========== BUG 8: 倒计时选择器 分/秒 不重叠 ==========
await page.evaluate(() => openScheduleCountdownPicker());
await page.waitForTimeout(300);
const b8 = await page.evaluate(() => {
  const units = [...document.querySelectorAll('#scheduleCountdownModal .wheel-unit')];
  const rs = units.map(u => { const r = u.getBoundingClientRect(); return { text: u.textContent, x: Math.round(r.x), w: Math.round(r.width), y: Math.round(r.y), h: Math.round(r.height) }; });
  const overlap = rs.length === 2 ? !(rs[0].x + rs[0].w <= rs[1].x || rs[1].x + rs[1].w <= rs[0].x) : false;
  return { rs, overlap };
});
check('BUG8 分/秒单位不重叠', b8.rs.length === 2 && !b8.overlap, JSON.stringify(b8.rs));
await page.evaluate(() => closeModal('scheduleCountdownModal'));

// ========== BUG 11: 范围选择 全部/单页/双页 按钮外观 ==========
await page.evaluate(() => document.querySelector('.tab-item[data-tab="print"]').click());
await page.waitForTimeout(700);
await page.setInputFiles('#fileInput', [
  { name: '测试文档.docx', mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer: Buffer.from('mock docx') },
]);
await page.waitForTimeout(1200);
await page.evaluate(() => document.querySelector('#fileList [data-action="open-range"]').click());
await page.waitForTimeout(400);
const b11 = await page.evaluate(() => {
  const btn = document.getElementById('rangePickerAll');
  const cs = getComputedStyle(btn);
  return { border: cs.borderColor, shadow: cs.boxShadow !== 'none', bg: cs.backgroundColor };
});
check('BUG11 范围按钮有边框+阴影', b11.border !== 'rgba(0, 0, 0, 0)' && b11.shadow, JSON.stringify(b11));
await page.evaluate(() => closeModal('rangePickerModal'));
await page.waitForTimeout(300);

// ========== BUG 7: 确认页码范围后卡片不整表重绘（元素引用保持，无闪烁） ==========
await page.evaluate(() => document.querySelector('#fileList [data-action="open-range"]').click());
await page.waitForTimeout(300);
await page.evaluate(() => {
  const card = document.querySelector('#fileList .file-card');
  card._uid = 'keep-me';  // 元素身份标记：若整表重绘会被重建，标记丢失
});
await page.evaluate(() => { document.getElementById('rangePickerAll').click(); document.getElementById('rangePickerOk').click(); });
await page.waitForTimeout(400);
const b7 = await page.evaluate(() => {
  const card = document.querySelector('#fileList .file-card');
  const summary = card.querySelector('.range-summary-text');
  const status = card.querySelector('.status-label.done');
  return { sameEl: card._uid === 'keep-me', summary: summary ? summary.textContent : null, statusText: status ? status.textContent : null };
});
check('BUG7 确认后卡片原地更新（元素未被重建，无 statusFadeIn 闪烁）', b7.sameEl && !!b7.summary, JSON.stringify(b7));

// ========== BUG 13: 每页条数下拉后滚动目标 ==========
await page.evaluate(() => document.querySelector('.tab-item[data-tab="me"]').click());
await page.waitForTimeout(900);
// 复现用户场景：先滚动到订单区域（模拟用户正看着分页栏），再切换条数
await page.evaluate(() => {
  const engine = scrollEngines.me;
  engine.measure();
  engine.scrollTo(Math.min(engine.maxY, 1500), 0);
});
await page.waitForTimeout(400);
const b13pre = await page.evaluate(() => {
  const engine = scrollEngines.me;
  const section = document.querySelector('#page-me .orders-section');
  const scroller = engine.el;
  return { y0: Math.round(engine.y), maxY0: Math.round(engine.maxY), sectionOffset: Math.round(section.getBoundingClientRect().top - scroller.getBoundingClientRect().top + engine.y) };
});
await page.evaluate(() => { document.getElementById('ordersPageSize').click(); });
await page.waitForTimeout(200);
await page.evaluate(() => document.querySelector('#pageSizePicker [data-size="50"]').click());
await page.waitForTimeout(900);
const b13 = await page.evaluate(() => {
  const engine = scrollEngines.me;
  const section = document.querySelector('#page-me .orders-section');
  return { y1: Math.round(engine.y), maxY1: Math.round(engine.maxY), sectionTop: Math.round(section.getBoundingClientRect().top) };
});
// 校验点：切换条数后目标应把订单区带到顶部附近（sectionTop ≈ 导航栏 48 + 间距 20），
// 而不是从当前位置继续向下滚出大量距离（旧公式多加了 +y）
const navH = 48;
const scrollDelta = b13.y1 - b13pre.y0;
check('BUG13 切换条数后滚动目标正确(订单区在顶部附近, 不向下漂移)', Math.abs(b13.sectionTop - (navH + 20)) < 80, JSON.stringify({ pre: b13pre, post: b13, scrollDelta }));

// ========== BUG 4: 深色模式订单卡片金额无白色底纹 ==========
await page.evaluate(() => { localStorage.setItem('hn_theme_mode', 'dark'); applyTheme('dark', { skipServer: true }); });
await page.waitForTimeout(500);
const b4 = await page.evaluate(() => {
  const orderCard = document.querySelector('#orderList [data-order-id]');
  if (!orderCard) return null;
  orderCard.classList.add('order-expanded');
  const detail = orderCard.querySelector('.order-card-detail');
  if (detail) detail.classList.add('detail-expanded');
  const price = orderCard.querySelector('.detail-price-value');
  const cs = price ? getComputedStyle(price) : null;
  return cs ? { bg: cs.backgroundColor, color: cs.color } : null;
});
check('BUG4 我页金额透明背景(深色)', b4 && b4.bg === 'rgba(0, 0, 0, 0)', JSON.stringify(b4));

// ========== BUG 5: 历史授权用户 admin 密钥关联订单展开 ==========
await page.evaluate(() => document.getElementById('btnAuthorizedUsers').click());
await page.waitForTimeout(900);
const b5pre = await page.evaluate(() => {
  const toggles = [...document.querySelectorAll('[data-key-order-toggle]')];
  const rows = [...document.querySelectorAll('.record-order-toggle')];
  return { toggles: toggles.length, countText: rows.length ? rows[0].querySelector('.record-order-count').textContent : null,
    tempOrderLine: [...document.querySelectorAll('.record-row')].some(r => r.textContent.includes('订单 #42')) };
});
await page.evaluate(() => document.querySelector('[data-key-order-toggle]').click());
await page.waitForTimeout(800);
const b5 = await page.evaluate(() => {
  const wrap = document.querySelector('[data-key-orders]');
  const items = [...document.querySelectorAll('.record-order-item')].length;
  const status = (wrap.querySelector('.record-orders-status') || {}).textContent;
  return { expanded: wrap.classList.contains('record-orders-expanded'), items, status, innerScroll: getComputedStyle(wrap).overflowY };
});
check('BUG5 admin密钥:显示数量+点击展开订单列表', b5pre.toggles === 1 && b5pre.countText === '25 个' && b5.items > 0 && b5.expanded, JSON.stringify({ pre: b5pre, post: b5 }));

// ========== BUG 9: 已绑定设备使用卡片外观 ==========
await page.evaluate(() => { document.querySelector('.tab-item[data-tab="me"]').click(); });
await page.waitForTimeout(600);
await page.evaluate(() => document.getElementById('btnBindAccount').click());
await page.waitForTimeout(1200);
const b9 = await page.evaluate(() => {
  const list = document.querySelector('.bind-device-list');
  if (!list) return null;
  const cs = getComputedStyle(list);
  return { bg: cs.backgroundColor, shadow: cs.boxShadow !== 'none', radius: cs.borderRadius };
});
check('BUG9 已绑定设备列表为卡片外观(底色+阴影)', !!b9 && b9.bg !== 'rgba(0, 0, 0, 0)' && b9.shadow, JSON.stringify(b9));

// ========== BUG 6: 防滥用 stepper 原地更新（按钮元素保持） ==========
await page.evaluate(() => { document.querySelector('.tab-item[data-tab="me"]').click(); });
await page.waitForTimeout(500);
await page.evaluate(() => {
  const sum = document.getElementById('securitySummary');
  sum.click();
});
await page.waitForTimeout(500);
const b6 = await page.evaluate(() => {
  const plusBtn = document.querySelector('#securityItems [data-sec-plus]');
  if (!plusBtn) return null;
  const el = plusBtn;
  el.click();
  const stillSame = document.querySelector('#securityItems [data-sec-plus]') === el;
  return { stillSame, value: document.querySelector('#securityItems .security-input').value };
});
check('BUG6 防滥用按钮点击后元素保持(动画可完整播放)', !!b6 && b6.stillSame, JSON.stringify(b6));

// ========== BUG 10: 收起订单卡片后延迟重测（滚动边界收缩） ==========
await page.evaluate(() => {
  const card = document.querySelector('#orderList [data-order-id]');
  if (card) card.click();
});
await page.waitForTimeout(500);
const b10 = await page.evaluate(() => {
  const card = document.querySelector('#orderList [data-order-id]');
  card.click();  // 收起
  return true;
});
await page.waitForTimeout(600);
const b10post = await page.evaluate(() => {
  const engine = scrollEngines.me;
  return { y: Math.round(engine.y), maxY: Math.round(engine.maxY) };
});
check('BUG10 收起卡片后滚动边界已重测(y<=maxY)', b10 && b10post.y <= b10post.maxY + 1, JSON.stringify(b10post));

// ========== BUG 4b: 历史授权用户/本地打印任务(#view-user-orders)金额无白色底纹 ==========
// 从历史授权用户进入某用户的订单视图
await page.evaluate(() => document.querySelector('#authorizedUserList [data-user-openid]').click());
await page.waitForTimeout(1100);
await page.evaluate(() => {
  const card = document.querySelector('#userOrdersList [data-order-id]');
  if (card) { card.classList.add('order-expanded'); card.querySelector('.order-card-detail').classList.add('detail-expanded'); }
});
const b4b = await page.evaluate(() => {
  const price = document.querySelector('#view-user-orders .detail-price-value');
  const cs = price ? getComputedStyle(price) : null;
  return cs ? { bg: cs.backgroundColor, color: cs.color } : null;
});
check('BUG4b 用户订单视图金额透明背景(深色)', b4b && b4b.bg === 'rgba(0, 0, 0, 0)', JSON.stringify(b4b));

// ========== BUG 13b: 子视图(user-orders)切换条数后滚动目标 ==========
await page.evaluate(() => {
  const engine = scrollEngines.userOrders;
  engine.measure();
  engine.scrollTo(Math.min(engine.maxY, 600), 0);
});
await page.waitForTimeout(300);
const b13bpre = await page.evaluate(() => {
  const engine = scrollEngines.userOrders;
  return { y0: Math.round(engine.y) };
});
await page.evaluate(() => { document.getElementById('uoPageSizeSelector').click(); });
await page.waitForTimeout(200);
await page.evaluate(() => document.querySelector('#uoPageSizeDropdown [data-size="50"]').click());
await page.waitForTimeout(900);
const b13b = await page.evaluate(() => {
  const engine = scrollEngines.userOrders;
  const section = document.querySelector('#view-user-orders .orders-section');
  return { y1: Math.round(engine.y), sectionTop: Math.round(section.getBoundingClientRect().top) };
});
check('BUG13b 子视图切换条数后滚动目标正确', Math.abs(b13b.sectionTop - (navH + 12)) < 80, JSON.stringify({ pre: b13bpre, post: b13b }));

// ========== BUG 6b: 文件卡片份数按钮点击后元素保持（动画可完整播放） ==========
await page.evaluate(() => document.querySelector('.tab-item[data-tab="print"]').click());
await page.waitForTimeout(600);
const b6b = await page.evaluate(() => {
  const plusBtn = document.querySelector('#fileList [data-action="copies-plus"]');
  if (!plusBtn) return null;
  const el = plusBtn;
  el.click();
  const stillSame = document.querySelector('#fileList [data-action="copies-plus"]') === el;
  const val = document.querySelector('#fileList .file-card .stepper-input').value;
  return { stillSame, val };
});
check('BUG6b 份数按钮点击后元素保持(动画可完整播放)', !!b6b && b6b.stillSame && b6b.val === '2', JSON.stringify(b6b));

console.log('\nJS 错误:', errors.length ? errors.join('\n') : '无');
const failed = results.filter(r => !r.ok);
console.log('\n结果: ' + (results.length - failed.length) + '/' + results.length + ' 通过');
if (failed.length) console.log('未通过: ' + failed.map(f => f.name).join('; '));

await browser.close();
server.close();
process.exit(failed.length ? 1 : 0);
