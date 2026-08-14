/* key_sync_test.mjs — 密钥卡片生命周期多端同步机制验证（APP 端行为）
 * 模拟：生成 → 被使用(used_waiting/used_done) → 被删除(不在 active 列表) → 过期
 * 用法: node tools/key_sync_test.mjs
 */
import { createRequire } from 'node:module';
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const APP_DIR = 'D:/打印机项目/mobile_apps/android_app/www';
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.svg': 'image/svg+xml', '.json': 'application/json' };

// 可变的密钥库（模拟另一端的操作）
let keyStore = [];
let genCount = 0;
const localStr = (d) => {
  const p = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
};
const makeKey = () => {
  genCount++;
  const exp = new Date(Date.now() + 5 * 60 * 1000); // 5 分钟有效期（本地时间）
  return {
    key: 'KEY' + String(10000 + genCount),
    type: 'temp',
    status: 'unused',
    expires_at: localStr(exp),
    validity_minutes: 5,
    created_at: localStr(new Date()),
    used_by: null, used_by_nickname: '', used_by_avatar_url: '',
    order_id: null, order_status: '', order_total_price: 0,
  };
};

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  const path = url.pathname;
  if (path === '/favicon.ico') { res.writeHead(204); res.end(); return; }
  const send = (obj, status = 200) => { res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(obj)); };
  // Node 原生 http 不解析请求体，手动收集
  const readBody = async () => {
    try {
      const chunks = [];
      for await (const c of req) chunks.push(c);
      return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
    } catch { return {} }
  };
  if (path === '/__mock/keys') {
    // 测试控制接口：{ action: 'use'|'done'|'remove'|'expire' } 修改密钥库，模拟另一端操作
    const body = await readBody();
    if (body && body.action && keyStore[0]) {
      const k = keyStore[0];
      if (body.action === 'use') { k.status = 'used_waiting'; k.used_by = 'openid_user1'; k.used_by_nickname = '张三'; }
      else if (body.action === 'done') { k.status = 'used_done'; k.order_id = 42; k.order_status = 'queued'; }
      else if (body.action === 'remove') { keyStore = []; }
      else if (body.action === 'expire') { k.status = 'unused'; k.expires_at = '2020-01-01 00:00:00'; }
    }
    return send({ success: true });
  }
  if (path.startsWith('/api/')) {
    if (path === '/api/device_login') return send({ success: true, token: 'mock-token', openid: 'mock-openid' });
    if (path === '/api/me') return send({ success: true, role: 'admin', is_super_admin: true, license_info: null, has_temp_access: false, temp_until: '' });
    if (path === '/api/profile') return send({ success: true, nickname: '测试管理员', avatar_url: '' });
    if (path === '/api/pricing') return send({ success: true, data: {} });
    if (path === '/api/printer_status') return send({ success: true, online: false });
    if (path === '/api/license/active') {
      // 模拟真实后端：只返回未过期且未归档/作废/完成的密钥
      const nowStr = localStr(new Date());
      const keys = keyStore.filter(k => k.expires_at > nowStr);
      return send({ success: true, active: keys.length > 0, keys });
    }
    if (path === '/api/license/create') { keyStore.unshift(makeKey()); return send({ success: true }); }
    if (path === '/api/license/revoke') {
      const body = await readBody();
      keyStore = keyStore.filter(k => k.key !== (body.key || ''));
      return send({ success: true });
    }
    if (path === '/api/orders') return send({ success: true, orders: [], total: 0 });
    if (path === '/api/authorized_users') return send({ success: true, users: [] });
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
await page.evaluate(() => document.querySelector('.tab-item[data-tab="me"]').click());
await page.waitForTimeout(900);

const results = [];
const check = (name, ok, detail) => { results.push({ name, ok }); console.log((ok ? '✓' : '✗') + ' ' + name + (detail ? ' — ' + detail : '')); };

const snapshot = () => page.evaluate(() => ({
  cards: [...document.querySelectorAll('#activeKeys [data-key-swipe]')].map(c => ({
    key: c.dataset.keySwipe,
    countdown: (c.querySelector('.key-countdown') || {}).textContent,
    userInfo: !!c.querySelector('.key-user-info'),
    buttons: [...c.querySelectorAll('.key-actions .copy-btn')].map(b => b.textContent.trim()),
    settle: !!c.querySelector('[data-settle]'),
    wrapClass: c.parentElement.className,
  })),
  badge: document.getElementById('activeKeyCount').textContent,
}));

// 1. 生成密钥 → 卡片显示（unused + 倒计时）
await page.evaluate(() => document.getElementById('generateKeyBtn').click());
await page.waitForTimeout(600);
let s = await snapshot();
check('生成后显示卡片(unused+倒计时)', s.cards.length === 1 && /^\d+:\d{2}$/.test(s.cards[0].countdown || ''), JSON.stringify(s.cards[0] && { key: s.cards[0].key, countdown: s.cards[0].countdown }));

// 2. 另一端使用密钥（used_waiting：已兑换未提交任务）
await page.evaluate(() => fetch('/__mock/keys', { method: 'POST', body: JSON.stringify({ action: 'use' }) }).then(() => loadActiveKeys()));
await page.waitForTimeout(500);
s = await snapshot();
check('被使用→显示"已使用"', s.cards.length === 1 && s.cards[0].countdown === '已使用' && s.cards[0].userInfo, JSON.stringify(s.cards[0] && { countdown: s.cards[0].countdown, buttons: s.cards[0].buttons }));

// 3. 用户提交任务（used_done + order_id）→ 显示结算按钮
await page.evaluate(() => fetch('/__mock/keys', { method: 'POST', body: JSON.stringify({ action: 'done' }) }).then(() => loadActiveKeys()));
await page.waitForTimeout(500);
s = await snapshot();
check('提交任务→显示结算按钮', s.cards.length === 1 && s.cards[0].settle === true, JSON.stringify(s.cards[0] && { buttons: s.cards[0].buttons, settle: s.cards[0].settle }));

// 4. 另一端删除（archived → 不在 active 列表）→ 卡片离场移除
await page.evaluate(() => fetch('/__mock/keys', { method: 'POST', body: JSON.stringify({ action: 'remove' }) }).then(() => loadActiveKeys()));
await page.waitForTimeout(200);
const during = await page.evaluate(() => document.querySelectorAll('#activeKeys [data-key-swipe]').length);
await page.waitForTimeout(500);
const after = await snapshot();
check('另一端删除→本端不显示', during >= 0 && after.cards.length === 0, `移除后卡片数=${after.cards.length}`);

// 5. 再次生成 → 验证轮询间隔可恢复（不验证 15s，只验证再次生成正常）
await page.evaluate(() => document.getElementById('generateKeyBtn').click());
await page.waitForTimeout(600);
s = await snapshot();
check('再次生成正常', s.cards.length === 1, JSON.stringify({ cards: s.cards.length }));

// 6. 密钥过期（expires_at 已过 → 后端不再返回）→ 卡片自动移除
await page.evaluate(() => fetch('/__mock/keys', { method: 'POST', body: JSON.stringify({ action: 'expire' }) }).then(() => loadActiveKeys()));
await page.waitForTimeout(500);
s = await snapshot();
check('过期后自动移除', s.cards.length === 0, JSON.stringify({ cards: s.cards.length }));

console.log('\nJS 错误:', errors.length ? errors.join('\n') : '无');
const failed = results.filter(r => !r.ok);
console.log('结果: ' + (results.length - failed.length) + '/' + results.length + ' 通过');
await browser.close();
server.close();
process.exit(failed.length ? 1 : 0);
