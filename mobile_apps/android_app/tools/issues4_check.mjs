import { createRequire } from 'node:module';
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const APP_DIR = 'D:/打印机项目/mobile_apps/android_app/www';
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.json': 'application/json' };

const manyOrders = Array.from({ length: 25 }, (_, i) => ({
  id: 100 + i, order_number: 'HN202608' + String(100 + i), status: 'queued',
  file_summary: 'doc' + i + '.pdf', total_pages: 5, total_copies: 2,
  created_at: '2026-08-12 10:00', total_price: 3.6, files: [],
}));

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  const path = url.pathname;
  if (path === '/favicon.ico') { res.writeHead(204); res.end(); return; }
  const send = (obj, status = 200) => { res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(obj)); };
  if (path.startsWith('/api/')) {
    if (path === '/api/device_login') return send({ success: true, token: 'mock-token', openid: 'mock-openid' });
    if (path === '/api/me') return send({ success: true, role: 'admin', is_super_admin: true, license_info: null, has_temp_access: false, temp_until: '' });
    if (path === '/api/profile') return send({ success: true, nickname: 'T', avatar_url: '' });
    if (path === '/api/pricing') return send({ success: true, data: {} });
    if (path === '/api/printer_status') return send({ success: true, online: false });
    if (path === '/api/license/active') return send({ success: true, keys: [] });
    if (path === '/api/upload') return send({ success: true, file_id: 'f1', page_count: 1 });
    if (path === '/api/orders') {
      const per = parseInt(url.searchParams.get('per_page') || '10', 10);
      const page = parseInt(url.searchParams.get('page') || '1', 10);
      return send({ success: true, orders: manyOrders.slice((page - 1) * per, (page - 1) * per + per), total: manyOrders.length });
    }
    if (path === '/api/authorized_users') return send({ success: true, users: [{ openid: 'u1', nickname: 'u1', records: [{ key: 'K1', type: 'temp', status: 'used', order_id: 42 }], order_count: 1 }] });
    if (path === '/api/admin/user_detail') return send({ success: true, role: 'user', nickname: 'u1', license_info: null });
    if (path === '/api/admin/temp_users') return send({ success: true, users: [] });
    if (path === '/api/admin/admins') return send({ success: true, admins: [] });
    if (path === '/api/storage_stats') return send({ success: true, data: { total_files: 0, total_size_display: '0 B' }, retention_days: 7, retention_hours: 0 });
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
  } catch { res.writeHead(404); res.end('nf'); }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const port = server.address().port;

const browser = await chromium.launch({ executablePath: CHROME, headless: true });
const page = await browser.newPage({ viewport: { width: 430, height: 920 }, hasTouch: true });
const errors = [];
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
await page.addInitScript((p) => {
  localStorage.setItem('hn_base_url', 'http://127.0.0.1:' + p);
  localStorage.setItem('hn_token', 'mock-token');
  localStorage.setItem('hn_openid', 'mock-openid');
  localStorage.setItem('hn_role', 'admin');
  localStorage.setItem('hn_is_super', '1');
}, port);
await page.goto(`http://127.0.0.1:${port}/index.html`);
await page.waitForTimeout(1500);
const results = [];
const check = (name, ok, detail) => { results.push({ name, ok }); console.log((ok ? '✓' : '✗') + ' ' + name + (detail ? ' — ' + detail : '')); };

// ===== 问题 1：大图自动压缩 =====
// 生成 >2MB 的 BMP（无压缩，2500x2500 ≈ 18MB），验证上传前被压缩
const bigBmp = await page.evaluate(() => {
  const canvas = document.createElement('canvas');
  canvas.width = 2500; canvas.height = 2500;
  const ctx = canvas.getContext('2d');
  const grad = ctx.createLinearGradient(0, 0, 2500, 2500);
  grad.addColorStop(0, '#ff8800'); grad.addColorStop(1, '#0044ff');
  ctx.fillStyle = grad; ctx.fillRect(0, 0, 2500, 2500);
  for (let i = 0; i < 2000; i++) { ctx.fillStyle = '#' + Math.floor(Math.random() * 0xffffff).toString(16).padStart(6, '0'); ctx.fillRect(Math.random() * 2500, Math.random() * 2500, 40, 40); }
  return canvas.toDataURL('image/bmp');
});
const bmpBuffer = Buffer.from(bigBmp.split(',')[1], 'base64');
console.log('  生成 BMP 大小: ' + Math.round(bmpBuffer.length / 1024) + 'KB');
await page.setInputFiles('#fileInput', [{ name: 'big.bmp', mimeType: 'image/bmp', buffer: bmpBuffer }]);
await page.waitForTimeout(2500);
const compress = await page.evaluate(() => {
  const f = printState.selectedFiles[0];
  return f ? { size: f.size, sizeKB: (f.size / 1024).toFixed(1), name: f.name, fileType: f.file.type, fileSize: f.file.size, failed: f.failed } : null;
});
check('问题1 大图(>2MB BMP)自动压缩后上传', !!compress && compress.size < 2 * 1024 * 1024 && compress.fileType === 'image/jpeg' && !compress.failed, JSON.stringify(compress && { sizeKB: compress.sizeKB, fileType: compress.fileType }));
const uploadFn = await page.evaluate(() => ({ type: typeof uploadFile, inWindow: 'uploadFile' in window }));
console.log('  uploadFile 全局检查:', JSON.stringify(uploadFn));

// ===== 问题 2：分页栏布局（子界面 nav 靠右） =====
await page.evaluate(() => document.querySelector('.tab-item[data-tab="me"]').click());
await page.waitForTimeout(800);
await page.evaluate(() => document.getElementById('btnAuthorizedUsers').click());
await page.waitForTimeout(900);
await page.evaluate(() => document.querySelector('#authorizedUserList [data-user-openid]').click());
await page.waitForTimeout(1100);
const layout = await page.evaluate(() => {
  const pager = document.getElementById('userOrdersPager');
  const nav = document.querySelector('#userOrdersPager .page-nav');
  const pr = pager.getBoundingClientRect();
  const nr = nav.getBoundingClientRect();
  return { pagerRight: Math.round(pr.right), navRight: Math.round(nr.right), gap: Math.round(pr.right - nr.right) };
});
check('问题2 子界面分页栏页码靠右(与我的打印任务一致)', layout.gap < 12, JSON.stringify(layout));

// ===== 问题 3：uo 下拉 fixed + blur + 交互选择 =====
await page.evaluate(() => { document.getElementById('uoPageSizeSelector').click(); });
await page.waitForTimeout(300);
const uoDrop = await page.evaluate(() => {
  const el = document.getElementById('uoPageSizeDropdown');
  const cs = getComputedStyle(el);
  return { pos: cs.position, blur: cs.backdropFilter !== 'none', bg: cs.backgroundColor, inBody: el.parentElement === document.body, open: el.classList.contains('dropdown-show') };
});
check('问题3 uo下拉 fixed+blur+半透明+逃出变换层', uoDrop.pos === 'fixed' && uoDrop.blur && uoDrop.bg.includes('0.82') && uoDrop.inBody && uoDrop.open, JSON.stringify(uoDrop));
// 点击 50条/页 → 应选择并收起
await page.evaluate(() => { document.querySelector('#uoPageSizeDropdown [data-size="50"]').click(); });
await page.waitForTimeout(900);
const uoAfter = await page.evaluate(() => ({
  perPage: meState.userOrdersView.perPage,
  open: document.getElementById('uoPageSizeDropdown').classList.contains('dropdown-show'),
  text: document.getElementById('uoPageSizeText').textContent,
}));
check('问题3 uo下拉选择50条/页生效并收起', uoAfter.perPage === 50 && !uoAfter.open && uoAfter.text === '50条/页', JSON.stringify(uoAfter));

// me 页下拉同样验证
await page.evaluate(() => { document.getElementById('navBack').click(); });
await page.waitForTimeout(800);
await page.evaluate(() => { document.getElementById('ordersPageSize').click(); });
await page.waitForTimeout(300);
const meDrop = await page.evaluate(() => {
  const el = document.getElementById('pageSizePicker');
  const cs = getComputedStyle(el);
  return { pos: cs.position, blur: cs.backdropFilter !== 'none', inBody: el.parentElement === document.body, open: el.classList.contains('dropdown-show'), bg: cs.backgroundColor };
});
check('问题3 me下拉 fixed+blur+逃出变换层', meDrop.pos === 'fixed' && meDrop.blur && meDrop.inBody && meDrop.open, JSON.stringify(meDrop));
await page.evaluate(() => { document.querySelector('#pageSizePicker [data-size="20"]').click(); });
await page.waitForTimeout(900);
const meAfter = await page.evaluate(() => ({
  perPage: meState.ordersPerPage,
  open: document.getElementById('pageSizePicker').classList.contains('dropdown-show'),
  text: document.getElementById('ordersPageSizeText').textContent,
}));
check('问题3 me下拉选择20条/页生效并收起', meAfter.perPage === 20 && !meAfter.open && meAfter.text === '20条/页', JSON.stringify(meAfter));

console.log('\nJS 错误:', errors.length ? errors.join('\n') : '无');
const failed = results.filter(r => !r.ok);
console.log('结果: ' + (results.length - failed.length) + '/' + results.length + ' 通过');
await browser.close();
server.close();
process.exit(failed.length ? 1 : 0);
