/* 生成 README 用的三张界面图（全部为模拟数据）：
 *   ① docs/screenshots/cloud-settlement.jpg      收支清算「云端数据」看板（真实 settlement.html）
 *   ② docs/screenshots/mini-program-print.jpg    小程序/APP 打印页（真实 android_app/www/）
 *   ③ docs/screenshots/mini-program-me.jpg       小程序/APP「我」页（真实 android_app/www/）
 *
 * 做法与仓库里的渲染验收脚本一致：本地起静态服务 + `/api/**` 全部打桩，
 * Playwright 打开真实页面、灌**虚构数据**（成员 张伟/李娜/王芳、设备 DESKTOP-PRINT01、
 * 订单 HN2026…），截图落 docs/screenshots/。**绝不发外网请求、绝不连生产库。**
 *
 * 用法：node tools/readme_screenshots/gen_web_shots.mjs [--only settlement|print|me]
 */
import { createRequire } from 'node:module';
import http from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { extname, join, normalize } from 'node:path';

const require = createRequire(import.meta.url);
const PW = [
  'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright',
  'playwright',
].find((p) => { try { return !!require.resolve(p); } catch (e) { return false; } });
if (!PW) { console.error('找不到 playwright'); process.exit(2); }
const { chromium } = require(PW);

const REPO = 'D:/打印机项目';
const FINANCE_DIR = `${REPO}/local_print_tool/finance`;
const APP_DIR = `${REPO}/mobile_apps/android_app/www`;
const OUT_DIR = `${REPO}/docs/screenshots`;
const CHROME = [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
].find(existsSync);

const ONLY = (() => {
  const i = process.argv.indexOf('--only');
  return i >= 0 ? process.argv[i + 1] : '';
})();

/* ══════════════ 虚构数据（README 三张图共用同一套人/设备/单号） ══════════════ */
const MEMBERS = [
  { id: 'm1', name: '张伟', color: '#4472C4' },
  { id: 'm2', name: '李娜', color: '#ED7D31' },
  { id: 'm3', name: '王芳', color: '#70AD47' },
];
const DEVICES = [
  { client_id: 'DESKTOP-PRINT01-7c1f', device_name: 'DESKTOP-PRINT01', owner_name: '张伟',
    online: true, auto_duplex: false, virtual: false, take_orders: true, stack_mode: true, stack_busy: false },
  { client_id: 'R7000-AHP10-3ab9', device_name: 'R7000-AHP10', owner_name: '李娜',
    online: false, auto_duplex: true, virtual: false, take_orders: false, stack_mode: null, stack_busy: false },
];
const PRICING = {
  simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1,
  delivery_locations: ['1号楼北楼', '2号楼南楼'],
  delivery_percentages: { '1号楼北楼': 0, '2号楼南楼': 5 },
  urgency_levels: ['低', '中', '高'], urgency_prices: { '低': 0, '中': 1, '高': 2 },
};
const STORE = {
  config: { investRate: 0.05, scoreCap: 15, passwords: {}, members: MEMBERS,
            // 云端用户 → 成员绑定（对账口径用；不绑的话这些单会算作「未绑定用户未计入对账」）
            memberBindings: MEMBERS.map((m) => ({ openid: `o_demo_${m.id}`, memberId: m.id })) },
  records: { expenses: [], income: [], scores: [], transfers: [] },
  notes: {}, meta: { version: 1 },
};
const OPENID_OF = { m1: 'o_demo_m1', m2: 'o_demo_m2', m3: 'o_demo_m3' };

const FILE_POOL = [
  ['高等数学作业1.pdf', 30], ['离散数学作业2.pdf', 10], ['实验报告模板.docx', 12],
  ['毕业论文初稿.docx', 48], ['课件图表.png', 1], ['报名统计表.xlsx', 7],
  ['英语听力原文.pdf', 16], ['课程设计说明书.docx', 22], ['期末复习提纲.pdf', 36],
  ['社团招新海报.png', 1], ['会议纪要.txt', 3], ['实验数据表.xlsx', 5],
];
const STATUS_POOL = [
  'sent', 'sent', 'sent', 'sent', 'sent', 'sent', 'sent', 'sent', 'sent', 'sent',
  'sent', 'sent', 'sent', 'sent', 'failed', 'abandoned', 'canceled', 'rejected', 'reserved',
];

/** 单个文件价格：双面 0.4/张（奇数页最后一张按单面 0.3）——与后端 calculate_price 同口径 */
function filePrice(pages, duplex) {
  if (duplex !== 'on' || pages <= 1) return +(pages * PRICING.simplex_price).toFixed(2);
  const sheets = Math.floor(pages / 2), odd = pages % 2;
  return +(sheets * PRICING.duplex_price + odd * PRICING.simplex_price).toFixed(2);
}

/** 造一批订单：2026-10-01 ~ 2026-10-18（与页面默认的「本月」口径一致），覆盖各状态/来源/设备 */
function buildOrders() {
  const orders = [];
  let seq = 0;
  for (let day = 0; day < 18; day++) {
    const date = new Date(Date.UTC(2026, 9, 1 + day));       // 无时区漂移：按 UTC 生成再拼字符串
    const ymd = date.toISOString().slice(0, 10).replace(/-/g, '');
    const count = day % 7 === 5 || day % 7 === 6 ? 6 : 4;    // 周末多一点，趋势图有起伏
    for (let k = 0; k < count; k++) {
      seq += 1;
      const status = STATUS_POOL[(seq * 7 + k) % STATUS_POOL.length];
      const member = MEMBERS[(seq + k) % MEMBERS.length];
      const isLocal = (seq % 5 === 0);
      const device = DEVICES[(seq + k) % DEVICES.length];
      const nFiles = 1 + ((seq + k) % 3);
      const files = [];
      for (let f = 0; f < nFiles; f++) {
        const [name, pages] = FILE_POOL[(seq + k + f * 3) % FILE_POOL.length];
        const copies = 1 + ((seq + f) % 5);
        const duplex = pages > 1 && (seq + f) % 2 === 0 ? 'on' : 'off';
        const price = +(filePrice(pages, duplex) * copies).toFixed(2);
        files.push({
          id: `f${seq}${f}`, file_name: name, copies, page_count: pages, duplex,
          page_range: '', total_price: price,
          status: status === 'sent' ? 'sent' : status, is_free: false,
        });
      }
      const orderPrice = +files.reduce((s, f) => s + f.total_price, 0).toFixed(2);
      const hh = String(9 + (k % 9)).padStart(2, '0');
      const mm = String((seq * 13) % 60).padStart(2, '0');
      const created = `${date.toISOString().slice(0, 10)} ${hh}:${mm}:${String((seq * 7) % 60).padStart(2, '0')}`;
      const isAdmin = seq % 11 === 0;
      orders.push({
        order_id: 900000 + seq,
        order_number: `HN${ymd}-${String(k + 1).padStart(4, '0')}`,
        source: isLocal ? 'local' : 'cloud',
        openid: isLocal ? 'local' : OPENID_OF[member.id],
        nickname: isLocal ? '' : member.name,
        owner_name: isAdmin ? '张伟' : member.name,
        status,
        total_price: orderPrice,
        created_at: created,
        print_started_at: status === 'sent' || status === 'failed' ? created : '',
        wait_seconds: 20 + (seq * 11) % 240,
        is_admin_print: isAdmin ? 1 : 0,
        files_count: files.length,
        total_page_count: files.reduce((s, f) => s + f.page_count * f.copies, 0),
        received_client: device.client_id,
        received_label: `${device.owner_name}的设备 | ${device.device_name}`,
        remark: seq % 9 === 0 ? '宿舍 5 人合并打印' : '',
        files,
      });
    }
  }
  return orders;
}
const ORDERS = buildOrders();

/* ══════════════ 静态服务 + 桩 API ══════════════ */
const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
};
const json = (res, obj) => {
  res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(obj));
};

function api(path, qs, method) {
  /* —— 收支清算（settlement.html 走 /api/proxy/* 代理） —— */
  if (path === '/api/health') return { success: true, cloud: true };
  if (path === '/api/proxy/finance/config') return { success: true, data: STORE, server_version: 2 };
  if (path === '/api/proxy/pricing') return { success: true, data: PRICING };
  if (path === '/api/proxy/admin/statistics/months')
    return { success: true, data: { months: [{ month: '2026-09' }, { month: '2026-10' }] } };
  if (path === '/api/proxy/admin/statistics/revenue')
    return { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: ORDERS } };
  if (path === '/api/proxy/admin/statistics/revenue/compare') return { success: true, data: { total: 0 } };
  if (path === '/api/proxy/printer/devices')
    return { success: true, devices: DEVICES, active_client_id: DEVICES[0].client_id,
             active_owner_name: '张伟', hidden_test_devices: 0 };
  if (path === '/api/local/data') return { success: true, data: null };
  if (path === '/api/local/bindings')
    return { success: true, bindings: Object.fromEntries(MEMBERS.map((m) => [OPENID_OF[m.id], m.id])) };
  /* —— 移动端（android_app/www） —— */
  // 后端 /api/printer_status 的 active 在**顶层**（app.js 的 api() 返回 {status, data: 整个 body}）
  if (path === '/api/printer_status')
    return { success: true, active: true, online_count: 1, owner_name: '张伟' };
  if (path === '/api/pricing') return { success: true, pricing: PRICING };
  if (path === '/api/me')
    return { success: true, role: 'admin', is_super_admin: true, openid: 'dev_demo',
             nickname: '张伟', license_info: { key: 'AB12CD34', type: 'admin', expired: false,
             creator_nickname: '张伟', created_at: '2026-09-06 12:00:00', used_at: '2026-09-06 12:00:01',
             expires_at: '—', validity_minutes: null } };
  if (path === '/api/profile')
    return { success: true, nickname: '张伟',
             avatar_url: "data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 108 108'%3E%3Ccircle cx='54' cy='54' r='54' fill='%23C7E0FF'/%3E%3C/svg%3E" };
  if (path === '/api/orders') {
    const per = Number(qs.get('per_page') || 10) || 10;
    const page = Number(qs.get('page') || 1) || 1;
    /* 转成 me 页订单卡形状（file_summary / total_pages / total_copies） */
    const list = ORDERS.slice().reverse().map((o) => ({
      id: o.order_id, order_number: o.order_number,
      status: o.status === 'sent' ? 'sent' : o.status,
      source: o.source, file_summary: `${o.files[0].file_name}${o.files.length > 1 ? ` +${o.files.length - 1} 个文件` : ''}`,
      total_pages: o.total_page_count, total_copies: o.files.reduce((s, f) => s + f.copies, 0),
      created_at: o.created_at, total_price: o.total_price,
      received_client: o.received_client, received_label: o.received_label,
      print_started_at: o.print_started_at, remark: o.remark, is_admin_print: o.is_admin_print,
      files: o.files.map((f) => ({
        id: f.id, original_name: f.file_name, page_count: f.page_count,
        copies: f.copies, duplex: f.duplex, page_range: f.page_range,
      })),
    }));
    return { success: true, total: list.length, orders: list.slice((page - 1) * per, page * per) };
  }
  if (path === '/api/claiming_devices')
    return { success: true, devices: DEVICES.map((d) => ({
      client_id: d.client_id, device_name: d.device_name, device_label: `${d.owner_name}的设备 | ${d.device_name}`,
      owner_name: d.owner_name, online: d.online, auto_duplex: d.auto_duplex, virtual: d.virtual,
      take_orders: d.take_orders, stack_mode: d.stack_mode, stack_busy: d.stack_busy,
      stack_can_toggle: d.auto_duplex === false, stack_reason: '',
    })), hidden_test_devices: 0 };
  if (path === '/api/license/active')
    return { success: true, keys: [{ key: 'EF56GH78', type: 'temp', status: 'unused', expired: false,
             countdownText: '5 分 00 秒', created_at: '2026-10-18 15:00:00' }] };
  if (path === '/api/bind/devices') return { success: true, devices: [{ openid: 'dev_demo', nickname: '本机' }] };
  if (path === '/api/admin/temp_users') return { success: true, users: [] };
  if (path === '/api/admin/admins')
    return { success: true, admins: [{ openid: 'a1', nickname: '李娜', avatar_url: '', is_super: false }] };
  if (path === '/api/admin/storage')
    return { success: true, total_files: 12, total_size_display: '3.6 MB', retention_days: 7, retention_hours: 0 };
  if (path === '/api/admin/security')
    return { success: true, storage_quota_mb: 2048, disk_min_free_mb: 5120, queue_timeout_hours: 24,
             upload_rate_limit: 20, submit_rate_limit: 20, device_register_rate_limit: 20,
             redeem_rate_limit: 10, log_rate_limit: 30 };
  return { success: true, data: null, orders: [], files: [], users: [], admins: [], keys: [], devices: [], total: 0, count: 0 };
}

let useApp = false;    // true → 静态文件从 android_app/www 取
const server = http.createServer(async (req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  const p = decodeURIComponent(u.pathname);
  if (p.startsWith('/api/')) return json(res, api(p, u.searchParams, req.method));
  const rel = normalize(p === '/' ? '/index.html' : p).replace(/^[\\/]+/, '');
  const root = useApp || rel.startsWith('app/') ? APP_DIR : FINANCE_DIR;
  const file = rel.startsWith('app/') ? rel.slice(4) : rel;
  try {
    const body = await readFile(join(root, file));
    res.writeHead(200, { 'Content-Type': MIME[extname(file)] || 'application/octet-stream' });
    res.end(body);
  } catch (e) {
    res.writeHead(404); res.end('not found');
  }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;
await mkdir(OUT_DIR, { recursive: true });
console.log('桩服务:', BASE);

const browser = await chromium.launch(CHROME ? { executablePath: CHROME } : {});
const want = (n) => !ONLY || ONLY === n;

/* ══════════════ ① 收支清算 · 云端数据看板 ══════════════ */
if (want('settlement')) {
  const page = await browser.newPage({ viewport: { width: 1200, height: 1720 }, deviceScaleFactor: 2 });
  page.on('pageerror', (e) => console.log('  [pageerror] ' + e.message));
  await page.route('**/api/**', (route) => route.request().url().startsWith(BASE)
    ? route.continue() : route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }));
  await page.addInitScript(() => {
    try { localStorage.setItem('financeThemeMode', 'dark'); } catch (e) { /* 忽略 */ }
  });
  await page.goto(`${BASE}/settlement.html`, { waitUntil: 'load' });
  await page.waitForFunction(() => typeof renderCloud === 'function' && typeof switchTab === 'function',
    null, { timeout: 15000 });
  await page.evaluate(() => switchTab('cloud'));      // 切到「云端数据」
  // 等订单明细真的渲染出来（说明统计接口的数据已到）
  await page.waitForFunction(() => document.querySelectorAll('#cloud-orders tr.order-row').length > 0,
    null, { timeout: 15000 });
  await page.addStyleTag({ content: '*,*::before,*::after{animation:none!important;transition:none!important}' });

  /* 手动代收录入：**用页面自己的对账函数**算出按成员的云端收益，等额回填成"手动代收"记录
     → 顶部对账差额 = 0，呈现「账已对齐」的正常状态（不是一片告警）。
     自己在外面临摹收益规则会差几十块（自打/本地接单/费用口径各不相同），所以直接把页面的
     计算结果喂回去。 */
  const cents = await page.evaluate(() =>
    _reconcileScope(_scopeRange(), _cloudData.orders).cloudByMember);
  STORE.records.income = Object.keys(cents).map((mid, i) => ({
    id: 'inc' + (i + 1), date: '2026-10-18', memberId: mid, amount: +(cents[mid] / 100).toFixed(2),
  }));
  console.log('    手动代收回填:', JSON.stringify(STORE.records.income));
  await page.reload({ waitUntil: 'load' });
  await page.waitForFunction(() => typeof switchTab === 'function' && typeof renderCloud === 'function',
    null, { timeout: 15000 });
  await page.evaluate(() => switchTab('cloud'));
  await page.waitForFunction(() => document.querySelectorAll('#cloud-orders tr.order-row').length > 0,
    null, { timeout: 15000 });
  await page.addStyleTag({ content: '*,*::before,*::after{animation:none!important;transition:none!important}' });
  await page.waitForTimeout(1200);           // 趋势图/环图动画走完
  await page.screenshot({ path: `${OUT_DIR}/cloud-settlement.jpg`, type: 'jpeg', quality: 90 });
  console.log('  ✔ cloud-settlement.jpg');
  await page.close();
}

/* ══════════════ ②③ 移动端（真实 android_app/www） ══════════════ */
if (want('print') || want('me')) {
  useApp = true;
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  page.on('pageerror', (e) => console.log('  [pageerror] ' + e.message));
  await page.addInitScript((base) => {
    try {
      window.__HN_TEST_BASE_URL__ = base;
      localStorage.setItem('hn_token', 'stub-token');
      localStorage.setItem('hn_theme_mode', 'dark');
      localStorage.setItem('hn_nickname', '张伟');
    } catch (e) { /* 忽略 */ }
  }, BASE);
  await page.route('**/api/**', (route) => route.request().url().startsWith(BASE)
    ? route.continue() : route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }));
  await page.goto(`${BASE}/index.html`, { waitUntil: 'load' });
  await page.waitForFunction(() => typeof switchTab === 'function' && typeof printState !== 'undefined',
    null, { timeout: 15000 });

  /** 长截：按内容高度把视口拉长后整屏截（手机页整页很高，按字段封顶避免图太长看不清）
   *  contentId 必须显式给：打印页与「我」页各有一个 scroll-content，隐藏的那个高度是 0，
   *  不能用 `A || B` 取（会永远量到第一个）。 */
  const longShot = async (path, maxH, contentId) => {
    const m = await page.evaluate(([cap, cid]) => {
      const el = document.getElementById(cid);
      const nav = document.getElementById('navBar');
      const rect = el ? el.getBoundingClientRect().height : 0;
      const scroll = el ? (el.scrollHeight || 0) : 0;
      const content = Math.max(rect, scroll);            // 自定义滚动引擎会把容器裁高，取两者较大
      const navH = nav ? nav.getBoundingClientRect().height : 0;
      return { content: Math.round(content), navH: Math.round(navH),
               h: Math.min(Math.round(content + navH + 100), cap) };   // +100：内容结尾躲开固定 tabBar
    }, [maxH, contentId]);
    console.log('    内容高度', m.content, '导航', m.navH, '→ 视口', m.h);
    await page.setViewportSize({ width: 390, height: m.h });
    await page.waitForTimeout(700);
    await page.screenshot({ path, type: 'jpeg', quality: 90 });
    await page.setViewportSize({ width: 390, height: 844 });
  };

  if (want('print')) {
    // 打印页：灌 4 个"已选文件"，并让接单设备在线（离线时页面显示「打印机离线」）
    await page.evaluate(() => {
      const mk = (over) => Object.assign({
        _uid: 1, name: 'a.txt', size: 1024, file: null, fileId: 'F1', uploading: false, progress: 100,
        failed: false, copies: 1, pageRange: '', rangeLines: [{ value: '', error: '' }], duplex: 'off',
        imageOrientation: 'auto', entering: false, removing: false, excelWarning: false,
        unsupportedFormat: false, isImage: false, pageCount: 1, pageCountStatus: 'confirmed',
        singlePage: false, sizeDisplay: '12.0',
      }, over || {});
      printState.selectedFiles = [
        mk({ _uid: 1, name: '高等数学作业1.pdf', copies: 5, pageCount: 30, singlePage: false,
             duplex: 'on', sizeDisplay: '1843' }),
        mk({ _uid: 2, name: '离散数学作业2.pdf', copies: 5, pageCount: 10, duplex: 'on',
             sizeDisplay: '2355' }),
        mk({ _uid: 3, name: '课件图表.png', isImage: true, copies: 3, pageCount: 1, singlePage: true,
             duplex: 'off', sizeDisplay: '480' }),
        mk({ _uid: 4, name: '报名统计表.xlsx', copies: 1, pageCount: 7, duplex: 'on',
             sizeDisplay: '96' }),
      ];
      if (typeof renderFileList === 'function') renderFileList();
      if (typeof recalcFileListHeight === 'function') recalcFileListHeight();
      if (typeof updatePrice === 'function') updatePrice();
      if (typeof updateTotalPrice === 'function') updateTotalPrice();
    });
    await page.addStyleTag({ content: '*,*::before,*::after{animation:none!important;transition:none!important}' });
    await page.waitForTimeout(900);
    await longShot(`${OUT_DIR}/mini-program-print.jpg`, 2400, 'scrollContentPrint');
    console.log('  ✔ mini-program-print.jpg');
  }

  if (want('me')) {
    await page.evaluate(() => switchTab('me'));
    await page.waitForFunction(() => {
      const el = document.getElementById('orderList');
      return el && el.children.length > 0;
    }, null, { timeout: 15000 });
    await page.addStyleTag({ content: '*,*::before,*::after{animation:none!important;transition:none!important}' });
    await page.waitForTimeout(900);
    await longShot(`${OUT_DIR}/mini-program-me.jpg`, 2700, 'scrollContentMe');
    console.log('  ✔ mini-program-me.jpg');
  }
  await page.close();
}

await browser.close();
server.close();
console.log('完成，输出目录:', OUT_DIR);
