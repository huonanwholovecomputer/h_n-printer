/* 验收 · 收支清算「订单明细点文件名看首页缩略图」——**真浏览器**半区（v7.21，2026-11-19）
 *
 * 为什么必须真浏览器：这条功能的一半在页面里 —— 文件名要按 `file_id && has_preview`
 * 决定可不可点、点开要建预览窗、要拿 X-Launch-Token 去 fetch 二进制、要把 blob 塞进
 * <img>（CSP img-src 必须含 blob:，否则浏览器静默拦掉、图片永远空白）、要翻页、要能
 * 用 ✕ / 滚动 / 点外部收起、失败时把云端 message 显示出来。纯静态断言
 * （verify_settlement_file_preview.py 的 A 组）只能证明「代码写了」，证明不了「点了有用」。
 *
 * 这里用 Playwright 起真 Chromium + 本地桩服务：页面走**真实 stats_server 的 CSP**
 * （从 stats_server.py 里抽出 _HTML_CSP 原样下发），/api/local/file_preview/<id> 桩返回
 * 真 GIF 字节 + X-Preview-Page/X-Preview-Pages 头，其它 /api/* 全桩。
 *
 * 钉住的 12 条：
 *   U1  可预览文件行有 .fn.pv-can、带「点击查看首页预览（共 N 页）」title
 *   U2  不可预览文件行没有 pv-can、没有 title/onclick（点了不该发请求）
 *   U3  点文件名弹 #fPrevMenu，标题是文件名
 *   U4  <img> 最终真的解码出图（naturalWidth>0）且 src 以 blob: 开头
 *   U5  请求带 X-Launch-Token 且路径是 /api/local/file_preview/<file_id>?page=1
 *   U6  页码栏「第 1 / 2 页」，上一页禁用、下一页可用
 *   U7  点下一页 → 真的请求 page=2，页码栏变「第 2 / 2 页」，下一页禁用
 *   U8  点 ✕ 关闭：节点移除 + blob URL 被 revokeObjectURL 释放
 *   U8b 连续两次 close（点 ✕ 的同时又滚动/点外部）不许留 pm-closing 鬼影
 *      —— 本条在本测试里抓到过真 bug，见 U8b 处注释
 *   U9  打开后页面滚动 → 自动收起（滚动即关）
 *   U10 点预览窗外空白 → 自动收起
 *   U11 云端 404（has_preview 但文件已清理）→ 预览窗里显示云端 message，不是白屏
 *   U13 预览图可放大（v7.26.2）：小舞台 300×300 → 视口级大舞台（≥900），
 *      图片按原始像素 800×1600 显示、超高可在窗内滚动、窗口重贴不跑出屏幕、
 *      放大态下窗内滚轮不收起、再点还原回 300×300
 *   U12 全程无 pageerror、无外网请求
 *
 * 用法：node other/tests/verify_settlement_file_preview_ui.mjs [settlement.html 路径]
 * exit 0 = 全部通过。
 *
 * 变异自证（2026-11-19 手工跑过，均按预期变红、还原后重新全绿）：
 *   M1 页面预览 fetch 不带 X-Launch-Token      → U5 红
 *   M2 页面把 _pvCan 恒置 false（文件名不可点）→ U1 红
 *   M3 stats_server 的 CSP 去掉 img-src blob:  → 前置「CSP 取自真源码」红
 * 注意点击窗内按钮要用 JS dispatch：Playwright 的 scroll-into-view 会滚页面，
 * 而本浮层「滚动即关」，会把窗收掉后报 element detached（工具假象，不是页面 bug）。
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
const HTML_PATH = process.argv[2] || path.join(FINANCE, 'settlement.html');
const STATS_PY = path.join(ROOT, 'local_print_tool', 'stats_server.py');
const TOKEN = 'ui-test-token';

let bad = 0, total = 0;
function chk(name, cond, detail) {
  total++;
  if (!cond) bad++;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (!cond && detail !== undefined ? '  -- ' + JSON.stringify(detail) : ''));
}

/* 真实 CSP：从 stats_server.py 的 _HTML_CSP 里抽出来原样下发。
   抽不到就直接红 —— 这时候用一份手写 CSP 测等于没测（img-src 少了 blob: 会让 U4 假绿）。 */
function readRealCsp() {
  const src = fs.readFileSync(STATS_PY, 'utf8');
  const m = src.match(/_HTML_CSP\s*=\s*\(([\s\S]*?)\n\)/);
  if (!m) return null;
  const parts = [];
  for (const line of m[1].split('\n')) {
    const t = line.trim();
    if (!t.startsWith('"') && !t.startsWith("'")) continue;
    const q = t[0];
    const end = t.indexOf(q, 1);
    if (end < 0) continue;
    parts.push(t.slice(1, end));
  }
  return parts.join('') || null;
}

/* ── 夹具 ─────────────────────────────────────────────────────────────── */
const MEMBER = { id: 'ma', name: '张三', color: '#4472C4' };
const STORE = {
  config: {
    investRate: 0.05, scoreCap: 15, passwords: {}, memberBindings: [],
    members: [MEMBER],
    printers: [{ id: 'p1', name: 'HP LaserJet 1020', model: 'HP LaserJet 1020', client_id: 'c1020',
                price: 400, life_pages: 4000, life_mode: 'estimated', status: 'active',
                billing: 'team', owner: 'shared', measured: false }],
    purchaseBasis: 'usage', waste_paper: {},
  },
  records: { expenses: [], income: [], scores: [], transfers: [] },
  notes: {}, meta: { version: 1 },
};

/* 两个文件：第一个可预览（fidOK1），第二个不可预览（has_preview=false 且没有 file_id）。 */
function filePreviewable() {
  return { id: 11, file_name: '可预览讲义.pdf', copies: 1, page_count: 3, total_pages: 3,
           total_sheets: 3, duplex: 'off', total_price: 0.9, status: 'sent',
           file_id: 'fidOK1', has_preview: true };
}
function filePlain() {
  return { id: 12, file_name: '不可预览.zip', copies: 1, page_count: 1, total_pages: 1,
           total_sheets: 1, duplex: 'off', total_price: 0.3, status: 'sent',
           file_id: '', has_preview: false };
}
function fileGone() {
  return { id: 13, file_name: '已被清理的旧单.pdf', copies: 1, page_count: 2, total_pages: 2,
           total_sheets: 2, duplex: 'off', total_price: 0.6, status: 'sent',
           file_id: 'fidERR9', has_preview: true };
}
/* v7.27：本地单（整单没有 file_id）—— 缩略图由本地打印工具渲染，走 /api/local/order_thumb */
function fileLocal() {
  return { id: 14, file_name: '本地单文件.pdf', copies: 1, page_count: 1, total_pages: 1,
           total_sheets: 1, duplex: 'off', total_price: 0.3, status: 'sent',
           file_id: '', has_preview: false };
}
function fileLocalMissing() {
  return { id: 15, file_name: '老单还没有图.pdf', copies: 1, page_count: 1, total_pages: 1,
           total_sheets: 1, duplex: 'off', total_price: 0.3, status: 'sent',
           file_id: '', has_preview: false };
}
function ord(id, files) {
  return { order_id: id, order_number: 'HN20261119-00' + id, source: 'local', openid: 'local',
           nickname: '', owner_name: '张三', status: 'sent',
           total_price: files.reduce((a, f) => a + f.total_price, 0),
           created_at: '2026-11-19 09:00:00', files_count: files.length,
           total_page_count: files.reduce((a, f) => a + f.total_pages, 0),
           total_sheet_count: files.reduce((a, f) => a + f.total_sheets, 0),
           received_client: 'c1020', received_label: '', files: files };
}
const ORDERS = [ord(9001, [filePreviewable(), filePlain()]), ord(9002, [fileGone()]),
                ord(9003, [fileLocal()]), ord(9004, [fileLocalMissing()])];
/* 本地缩略图桩：只有 9003 那单有图，9004 故意 404（验「没生成过」时的提示文案） */
const THUMB_ORDER_OK = 'HN20261119-009003';
const THUMB_ORDER_MISSING = 'HN20261119-009004';

/* 预览图夹具：800×1600 的 SVG（v7.26.2 起要验「放大按原始像素显示」，
   1×1 的 GIF 撑不出这个差异）。Chromium 能解码 <img> 里的 SVG，naturalWidth=800。 */
const PREVIEW_IMG = Buffer.from(
  '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="1600" viewBox="0 0 800 1600">' +
  '<rect width="800" height="1600" fill="#ffffff"/>' +
  '<rect x="40" y="40" width="720" height="120" fill="#4f6ef7"/>' +
  '<rect x="40" y="200" width="720" height="16" fill="#cfd7ff"/>' +
  '<rect x="40" y="240" width="600" height="16" fill="#cfd7ff"/>' +
  '<rect x="40" y="280" width="660" height="16" fill="#cfd7ff"/>' +
  '<rect x="40" y="1500" width="720" height="60" fill="#22c55e"/>' +
  '</svg>', 'utf8');
const PREVIEW_NAT_W = 800;
const PREVIEW_NAT_H = 1600;
const PAGES_OK = 2;

/* ── 桩服务 ───────────────────────────────────────────────────────────── */
const previewHits = [];
const thumbHits = [];
const json = (res, obj) => { res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(obj)); };

const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  const p = decodeURIComponent(u.pathname);
  if (p.startsWith('/api/')) {
    if (p === '/api/health') return json(res, { success: true, cloud: true });
    if (p === '/api/proxy/finance/config') return json(res, { success: true, data: STORE });
    if (p === '/api/local/finance_auth/status') {
      return json(res, { success: true, modules: {}, needs_setup: [],
                         session: { active: false, modules: [], expires_in: 0 },
                         session_ttl: 1800, read_only: false, integrity: 'ok' });
    }
    if (p === '/api/local/finance_auth/unlock') {
      let b = '';
      req.on('data', (c) => { b += c; });
      return req.on('end', () => {
        let mod = 'orderstatus';
        try { mod = JSON.parse(b).module || 'orderstatus'; } catch (e) {}
        json(res, { success: true, code: 'ok', message: '', session: 'test-session-' + mod,
                    expires_in: 1800, stored_hash: null,
                    modules: ['expense', 'income', 'transfer', 'settings', 'score', 'auth',
                              'orderstatus', 'orderprinter'] });
      });
    }
    if (p === '/api/proxy/pricing') {
      return json(res, { success: true, data: { simplex_price: 0.3, duplex_price: 0.4,
        cover_page_price: 0.1, delivery_percentages: {}, urgency_prices: {} } });
    }
    if (p === '/api/proxy/admin/statistics/months') {
      return json(res, { success: true, data: { months: [{ month: '2026-11' }] } });
    }
    if (p === '/api/proxy/admin/statistics/revenue') {
      return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: ORDERS } });
    }
    if (p === '/api/local/orders') return json(res, { success: true, data: { orders: [] } });
    if (p === '/api/local/data') return json(res, { success: true, data: null });
    if (p === '/api/local/bindings') return json(res, { success: true, data: {} });
    if (p === '/api/local/external_prints') {
      return json(res, { success: true, orders: [], count: 0, skipped: 0,
                         simplex_price: 0.3, duplex_price: 0.4, owner_name: '' });
    }
    /* 本地二进制预览通道：真页面就是这个路径，桩只负责按 id 回图 / 回错。 */
    if (p.startsWith('/api/local/order_thumb/')) {
      const num = p.slice('/api/local/order_thumb/'.length);
      thumbHits.push({ order: num, name: u.searchParams.get('name') || '',
                       token: req.headers['x-launch-token'] || '' });
      if (num === THUMB_ORDER_MISSING) {
        res.writeHead(404, { 'Content-Type': 'application/json; charset=utf-8' });
        return res.end(JSON.stringify({ success: false,
          message: '这一单还没有本地缩略图（换新版本地工具重打一次即可生成）' }));
      }
      res.writeHead(200, { 'Content-Type': 'image/svg+xml', 'Content-Length': PREVIEW_IMG.length,
                           'X-Preview-Page': '1', 'X-Preview-Pages': '1',
                           'Cache-Control': 'no-store' });
      return res.end(PREVIEW_IMG);
    }
    if (p.startsWith('/api/local/file_preview/')) {
      const id = p.slice('/api/local/file_preview/'.length);
      const page = parseInt(u.searchParams.get('page') || '1', 10);
      previewHits.push({ id: id, page: page, token: req.headers['x-launch-token'] || '' });
      if (id === 'fidERR9') {
        res.writeHead(404, { 'Content-Type': 'application/json; charset=utf-8' });
        return res.end(JSON.stringify({ success: false, code: 'notfound', message: '文件已清理，无法预览' }));
      }
      res.writeHead(200, { 'Content-Type': 'image/svg+xml', 'Content-Length': PREVIEW_IMG.length,
                           'X-Preview-Page': String(page), 'X-Preview-Pages': String(PAGES_OK),
                           'Cache-Control': 'no-store' });
      return res.end(PREVIEW_IMG);
    }
    return json(res, { success: false, code: 'unknown', message: 'unhandled ' + p });
  }
  const file = p === '/' ? 'settlement.html' : p.replace(/^\//, '');
  const fp = file === 'settlement.html' ? HTML_PATH : path.join(FINANCE, file);
  if (!fs.existsSync(fp) || fs.statSync(fp).isDirectory()) { res.writeHead(404); return res.end('not found'); }
  const ext = path.extname(fp);
  const head = { 'Content-Type': ext === '.html' ? 'text/html; charset=utf-8'
    : ext === '.js' ? 'application/javascript; charset=utf-8' : 'application/octet-stream' };
  let body = fs.readFileSync(fp);
  if (ext === '.html') {
    head['Content-Security-Policy'] = CSP;
    /* 与 stats_server._serve_injected_html 同款：把 __LAUNCH_TOKEN__ 换成真令牌 */
    body = Buffer.from(body.toString('utf8').split('__LAUNCH_TOKEN__').join(TOKEN), 'utf8');
  }
  res.writeHead(200, head);
  res.end(body);
});

const CSP = readRealCsp();
const port = await new Promise((r) => server.listen(0, '127.0.0.1', () => r(server.address().port)));
const BASE = 'http://127.0.0.1:' + port;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 });
const pageErrors = [];
const externalReq = [];
page.on('pageerror', (e) => pageErrors.push(e.message));
page.on('request', (r) => { if (!r.url().startsWith(BASE) && !r.url().startsWith('data:') && !r.url().startsWith('blob:')) externalReq.push(r.url()); });

/* revokeObjectURL 记录（U8 要证明 blob 没泄漏） */
const PAGE_HELPERS = `
window.__revoked = [];
/* 关掉平滑滚动：页面 CSS 里有 scroll-behavior:smooth，滚动会拖几百毫秒。
   预览窗是「滚动即关」，平滑滚动的尾巴会在开窗之后继续触发 scroll 事件把窗收掉
   （真人不滚页面，测试工具却会滚）。测试要量的是窗本身，不是滚动动画。 */
(function () {
  var st = document.createElement('style');
  st.textContent = 'html, body, * { scroll-behavior: auto !important; }' +
    /* 明细行容器也别让它能滚：开窗后 Chromium 会把这个内层容器一点点滚回顶部
       （实测 20→15→10→6→1→0，每帧 5px），每个 scroll 事件都会触发「滚动即关」。
       内容不再溢出，后面只靠 window 滚动验「滚动即关」。 */
    '.file-expand-inner { max-height: none !important; overflow: visible !important; }';
  (document.head || document.documentElement).appendChild(st);
})();
(function () {
  var orig = URL.revokeObjectURL.bind(URL);
  URL.revokeObjectURL = function (u) { window.__revoked.push(String(u)); return orig(u); };
})();
true;
`;

async function openOrders() {
  await page.goto(BASE + '/settlement.html', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => typeof window.financeUnlock === 'function', null, { timeout: 20000 });
  await page.evaluate(() => window.financeUnlock('orderstatus', '123456'));
  await page.waitForFunction(() => !!window.FINANCE_SESSION, null, { timeout: 10000 });
  await page.evaluate(PAGE_HELPERS);
  await page.evaluate(() => window.switchTab('cloud'));
  await page.waitForFunction(() => document.querySelectorAll('tr.order-row').length > 0, null, { timeout: 20000 });
  /* 展开第一个订单，文件明细行才会可见（tr.order-row 的 onclick 就是 _toggleOrderExpand）。
     点 ▶ 箭头而不是整行中心：行中心可能正好落在金额格（.price-copy 带 stopPropagation）。 */
  await page.click('tr.order-row .expand-icon');
  await page.waitForSelector('tr.file-expand.open');
}

const menuGone = async () => {
  try {
    await page.waitForFunction(() => !document.getElementById('fPrevMenu'), null, { timeout: 5000 });
  } catch (e) {
    const st = await page.evaluate(() => {
      const all = Array.prototype.slice.call(document.querySelectorAll('#fPrevMenu'));
      return { n: all.length, cls: all.map((m) => m.className), parent: all.map((m) => m.parentNode && m.parentNode.tagName),
               timer: String(window._fpCloseTimer) };
    }).catch(() => null);
    console.log('  [诊断] 预览窗没收起：' + JSON.stringify(st));
    throw e;
  }
};

try {
  chk('前置：CSP 取自 stats_server._HTML_CSP（不是手写副本）',
      !!CSP && CSP.indexOf('img-src') >= 0 && CSP.indexOf('blob:') >= 0, CSP);
  chk('前置：CSP 的 script-src 放行内联脚本（否则页面根本跑不起来，后面全是假红）',
      !!CSP && CSP.indexOf('unsafe-inline') >= 0, CSP);

  await openOrders();

  /* U1 / U2 只有可预览的文件名才可点 */
  const fnInfo = await page.evaluate(() => {
    const rows = Array.prototype.slice.call(document.querySelectorAll('tr.file-expand.open .file-sub-row'));
    return rows.map((r) => {
      const f = r.querySelector('.fn');
      return { text: (f.textContent || '').trim(), cls: f.className,
               title: f.getAttribute('title') || '', onclick: f.getAttribute('onclick') || '' };
    });
  });
  const pv = fnInfo.filter((f) => f.cls.indexOf('pv-can') >= 0);
  const plain = fnInfo.filter((f) => f.cls.indexOf('pv-can') < 0);
  chk('U1 可预览文件名带 pv-can + 「点击查看首页预览（共 3 页）」title',
      pv.length === 1 && pv[0].text.indexOf('可预览讲义.pdf') >= 0 &&
      pv[0].title.indexOf('共 3 页') >= 0 && pv[0].onclick.indexOf('openFilePreview(') >= 0, pv);
  chk('U2 不可预览文件名没有 pv-can / title / onclick',
      plain.length === 1 && plain[0].text.indexOf('不可预览.zip') >= 0 &&
      !plain[0].title && !plain[0].onclick, plain);

  /* U3 / U4 / U5 / U6 点开预览窗。
     用 JS 直接 dispatch（同 U7/U8 的理由）：page.click 末尾的 smooth scrollIntoView
     会在开窗后 60~100ms 还在滚页面，滚到一半就把窗按「滚动即关」收掉了（实测 651ms
     的 scroll 事件），后面全被带崩。真人点文件名不会滚动页面。 */
  await page.evaluate(() => {
    const el = document.querySelector('tr.file-expand.open .fn.pv-can');
    /* behavior:'instant' 很关键：页面里滚动容器是 scroll-behavior:smooth 的，平滑滚动会在
       开窗之后还继续几百毫秒，正好被「滚动即关」逮住（实测连续 8 个 scroll 事件把窗收掉）。 */
    if (el && el.scrollIntoView) el.scrollIntoView({ block: 'center', behavior: 'instant' });
    el.click();
  });
  await page.waitForTimeout(120);
  await page.waitForSelector('#fPrevMenu');
  /* 等入场动效收尾（pmIn .16s + both）：动效还在跑时整窗带 scale，量出来全是 0.94 倍 */
  /* 等入场动效收尾（pmIn .16s + fill both）：动效还在跑时整窗带约 1.004 的 scale，
     量出来 301 / 804 这种「差 1 像素」的假红。 */
  await page.waitForFunction(() => {
    const m = document.getElementById('fPrevMenu');
    if (!m) return false;
    const t = getComputedStyle(m).transform;
    if (t === 'none') return true;                       /* 动画收尾后 Chrome 给 none */
    const mm = t.match(/^matrix\(([-\d.]+), 0, 0, ([-\d.]+), ([-\d.]+), ([-\d.]+)\)$/);
    if (!mm) return false;
    /* 有的构建收尾停在 matrix(1,0,0,1,0,0) 或 1.004 的近似值：只要接近 1 就算收尾 */
    return Math.abs(parseFloat(mm[1]) - 1) < 0.002 && Math.abs(parseFloat(mm[2]) - 1) < 0.002 &&
           Math.abs(parseFloat(mm[3])) < 0.5 && Math.abs(parseFloat(mm[4])) < 0.5;
  }, null, { timeout: 5000 });
  await page.waitForFunction(() => {
    const img = document.querySelector('#fPrevMenu .fp-stage img');
    return img && img.naturalWidth > 0;
  }, null, { timeout: 15000 });
  const opened = await page.evaluate(() => {
    const m = document.getElementById('fPrevMenu');
    const img = m.querySelector('.fp-stage img');
    const bar = m.querySelector('.fp-bar');
    const btns = Array.prototype.slice.call(m.querySelectorAll('.fp-bar button'));
    const pageTxt = (m.querySelector('.fp-page') || {}).textContent || '';
    return { title: (m.querySelector('.fp-title') || {}).textContent || '',
             src: img.getAttribute('src') || '', natural: img.naturalWidth,
             pageTxt: pageTxt, btns: btns.map((b) => ({ t: (b.textContent || '').trim(), dis: b.disabled })),
             hint: (m.querySelector('.fp-hint') || {}).textContent || '',
             barExists: !!bar };
  });
  chk('U3 预览窗标题是文件名', opened.title.indexOf('可预览讲义.pdf') >= 0, opened.title);
  chk('U4 <img> 真的解码出图（naturalWidth>0）且 src 是 blob:',
      opened.natural > 0 && opened.src.indexOf('blob:') === 0, opened);
  const hit1 = previewHits[previewHits.length - 1] || {};
  chk('U5 请求带 X-Launch-Token 且路径是 /api/local/file_preview/fidOK1?page=1',
      hit1.id === 'fidOK1' && hit1.page === 1 && hit1.token === TOKEN, { hit1, hits: previewHits });
  chk('U6 页码栏「第 1 / 2 页」，上一页禁用、下一页可用',
      opened.pageTxt.indexOf('1 / 2') >= 0 &&
      (opened.btns.find((b) => b.t.indexOf('上一页') >= 0) || {}).dis === true &&
      (opened.btns.find((b) => b.t.indexOf('下一页') >= 0) || {}).dis === false, opened);

  /* U7 翻页真的请求 page=2
     注意：这里必须用 JS 直接 dispatch click，不能用 page.click —— Playwright 的
     「scroll into view if needed」会滚动页面，而预览窗的滚轮/滚动即关（与价格气泡同款）
     会立刻把窗收掉，于是报「element detached」/「<span> intercepts pointer events」。
     那是测试工具自己滚出来的，不是页面 bug：真人点窗内按钮不会触发滚动。 */
  await page.evaluate(() => document.querySelector('#fPrevMenu .fp-next').click());
  await page.waitForFunction(() => {
    const t = (document.querySelector('#fPrevMenu .fp-page') || {}).textContent || '';
    return t.indexOf('2 / 2') >= 0;
  }, null, { timeout: 10000 });
  const hit2 = previewHits[previewHits.length - 1] || {};
  const afterNext = await page.evaluate(() => {
    const m = document.getElementById('fPrevMenu');
    const btns = Array.prototype.slice.call(m.querySelectorAll('.fp-bar button'));
    return { pageTxt: (m.querySelector('.fp-page') || {}).textContent || '',
             nextDis: (btns.find((b) => (b.textContent || '').indexOf('下一页') >= 0) || {}).disabled };
  });
  chk('U7 下一页 → 请求 page=2，页码栏「第 2 / 2 页」，下一页禁用',
      hit2.page === 2 && afterNext.pageTxt.indexOf('2 / 2') >= 0 && afterNext.nextDis === true,
      { hit2, afterNext });

  /* U13 预览图可放大（v7.26.2）—— 小舞台 300×300 换成视口级大舞台，图片按原始像素显示。
     fixture 是 800×1600 的 SVG，所以「放大」在本条测试里可量：300 高 → 1600 高。 */
  const zoomBefore = await page.evaluate(() => {
    const m = document.getElementById('fPrevMenu');
    const st = m.querySelector('.fp-stage');
    const img = m.querySelector('.fp-stage img');
    const btn = m.querySelector('.fp-zoom-btn');
    return { zoomed: m.classList.contains('fp-zoom'), stageW: st.clientWidth,
             imgW: Math.round(img.getBoundingClientRect().width),
             imgH: Math.round(img.getBoundingClientRect().height), natW: img.naturalWidth,
             btn: (btn.textContent || '').trim() };
  });
  chk('U13a 默认是 300×300 小舞台（图按高度适配 300），按钮文案「放大」',
      /* 容差 3px：开窗动画 pmIn 的曲线 cubic-bezier(.2,.9,.3,1.25) 有回弹，
         正好量在回弹帧上会读到 301（不是布局错，是 transform 把 rect 撑了 0.3%） */
      zoomBefore.zoomed === false && zoomBefore.stageW === 300 &&
      Math.abs(zoomBefore.imgH - 300) <= 3 && zoomBefore.natW === PREVIEW_NAT_W &&
      zoomBefore.btn.indexOf('放大') >= 0, zoomBefore);

  await page.evaluate(() => document.querySelector('#fPrevMenu .fp-zoom-btn').click());
  /* U16（v7.29）：放大要有形变过渡 —— 点下去那刻 transform 正在过渡（0.32s），结束时清干净。
     先断过渡本身（时序稳定），再等它跑完才量尺寸，否则量到的是中间帧。 */
  const u16 = await page.evaluate(() => {
    const m = document.getElementById('fPrevMenu');
    const cs = getComputedStyle(m);
    const r = m.getBoundingClientRect();
    return { prop: cs.transitionProperty, dur: cs.transitionDuration,
             zooming: m.classList.contains('fp-zooming'),
             anim: cs.animationName,
             w: Math.round(r.width) };
  });
  chk('U16a 放大走 transform 过渡（0.32s），不是瞬间换尺寸',
      u16.prop.indexOf('transform') >= 0 && u16.dur === '0.32s' && u16.zooming === true
      && u16.w < 900, u16);
  chk('U16b 过渡期间开窗动画 pmIn 让位（fill:both 会压住 inline transform）',
      u16.anim === 'none', u16);
  await page.waitForFunction(() => {
    const m = document.getElementById('fPrevMenu');
    return !!m && !m._fpAnimTimer && !m.style.transform && !m.style.transition;
  }, null, { timeout: 3000 });
  await page.waitForFunction(() => {
    const m = document.getElementById('fPrevMenu');
    return !!m && m.classList.contains('fp-zoom');
  }, null, { timeout: 5000 });
  const zoomed = await page.evaluate(() => {
    const m = document.getElementById('fPrevMenu');
    const st = m.querySelector('.fp-stage');
    const img = m.querySelector('.fp-stage img');
    const btn = m.querySelector('.fp-zoom-btn');
    const r = m.getBoundingClientRect();
    return { stageW: st.clientWidth, stageH: st.clientHeight,
             display: getComputedStyle(st).display,
             imgW: Math.round(img.getBoundingClientRect().width),
             imgH: Math.round(img.getBoundingClientRect().height),
             scrollH: st.scrollHeight, clientH: st.clientHeight,
             btn: (btn.textContent || '').trim(),
             inView: r.left >= 0 && r.top >= 0 &&
                     r.right <= window.innerWidth + 1 && r.bottom <= window.innerHeight + 1 };
  });
  chk('U13b 放大：舞台变视口级（≥900 宽），图片按原始像素 ' + PREVIEW_NAT_W + '×' + PREVIEW_NAT_H + ' 显示',
      zoomed.display === 'block' && zoomed.stageW >= 900 &&
      zoomed.imgW === PREVIEW_NAT_W && zoomed.imgH === PREVIEW_NAT_H, zoomed);
  chk('U13c 放大后超高图可在窗内滚动（scrollHeight > 可视高度）',
      zoomed.scrollH > zoomed.clientH, zoomed);
  chk('U13d 放大后窗口重贴、不跑出屏幕，按钮变「还原」',
      zoomed.inView === true && zoomed.btn.indexOf('还原') >= 0, zoomed);

  /* 放大态下窗内滚轮只滚图片：_fpOnSlide 对窗内目标放行，预览窗不许被收掉
     （必须用真实 page.mouse.wheel —— 合成 WheelEvent 打不到 window 上的 capture 监听） */
  const stCenter = await page.evaluate(() => {
    const st = document.querySelector('#fPrevMenu .fp-stage');
    const r = st.getBoundingClientRect();
    return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
  });
  await page.mouse.move(stCenter.x, stCenter.y);
  await page.mouse.wheel(0, 300);
  await page.waitForTimeout(250);
  const stillHere = await page.evaluate(() => {
    const m = document.getElementById('fPrevMenu');
    const st = m && m.querySelector('.fp-stage');
    return { here: !!m, scrollTop: st ? Math.round(st.scrollTop) : -1, winScrollY: window.scrollY };
  });
  chk('U13e 放大态下窗内滚轮 → 预览窗不收起（只滚图片）',
      stillHere.here === true && stillHere.scrollTop > 0, stillHere);

  await page.evaluate(() => document.querySelector('#fPrevMenu .fp-stage img').click());
  await page.waitForFunction(() => {
    const m = document.getElementById('fPrevMenu');
    return !!m && !m.classList.contains('fp-zoom');
  }, null, { timeout: 5000 });
  /* v7.29：还原也有 320ms 过渡，等它跑完再量（否则量到中间帧） */
  await page.waitForFunction(() => {
    const m = document.getElementById('fPrevMenu');
    return !!m && !m._fpAnimTimer && !m.style.transform && !m.style.transition;
  }, null, { timeout: 3000 });
  const restored = await page.evaluate(() => {
    const m = document.getElementById('fPrevMenu');
    const st = m.querySelector('.fp-stage');
    const img = m.querySelector('.fp-stage img');
    const btn = m.querySelector('.fp-zoom-btn');
    return { stageW: st.clientWidth, imgH: Math.round(img.getBoundingClientRect().height),
             btn: (btn.textContent || '').trim() };
  });
  chk('U13f 点图片还原：回到 300×300 缩略舞台，按钮文案回到「放大」',
      restored.stageW === 300 && restored.imgH === 300 && restored.btn.indexOf('放大') >= 0,
      restored);

  /* U17（v7.29）：放大态下关闭也要有动画 —— 先缩回缩略舞台（220ms）再摘节点，
     不能一个视口级大窗硬切掉。用户当场问过「放大的状态没做关闭动画吗」。 */
  await page.evaluate(() => document.querySelector('#fPrevMenu .fp-zoom-btn').click());
  await page.waitForFunction(() => {
    const m = document.getElementById('fPrevMenu');
    return !!m && !m._fpAnimTimer && !m.style.transform && !m.style.transition;
  }, null, { timeout: 3000 });
  await page.evaluate(() => {
    const btns = Array.prototype.slice.call(document.querySelectorAll('#fPrevMenu .fp-bar button'));
    (btns.find((b) => (b.textContent || '').indexOf('✕') >= 0) || btns[btns.length - 1]).click();
  });
  const closing = await page.evaluate(() => {
    const m = document.getElementById('fPrevMenu');
    if (!m) return { here: false };
    const cs = getComputedStyle(m);
    return { here: true, prop: cs.transitionProperty, dur: cs.transitionDuration,
             closing: m.classList.contains('pm-closing'),
             stillZoom: m.classList.contains('fp-zoom'),
             w: Math.round(m.getBoundingClientRect().width) };
  });
  await page.waitForTimeout(120);
  const midClose = await page.evaluate(() => {
    const m = document.getElementById('fPrevMenu');
    return { here: !!m, w: m ? Math.round(m.getBoundingClientRect().width) : 0 };
  });
  await page.waitForTimeout(320);
  const closedAt = await page.evaluate(() => !document.getElementById('fPrevMenu'));
  chk('U17a 放大态关闭：先缩回缩略舞台（transform 过渡 0.22s），不是瞬间消失',
      closing.here === true && closing.prop.indexOf('transform') >= 0
      && closing.dur.indexOf('0.22s') >= 0 && closing.stillZoom === false && closing.w >= 600,
      closing);
  chk('U17b 放大态关闭：120ms 时浮层还在且正在缩小，约 440ms 后摘干净',
      midClose.here === true && midClose.w > 0 && midClose.w < closing.w && closedAt === true,
      { closingW: closing.w, midClose, closedAt });

  /* 重新开一个（U8 要用小窗去关） */
  await page.click('tr.file-expand.open .fn.pv-can');
  await page.waitForSelector('#fPrevMenu');
  await page.waitForTimeout(150);

  /* U8 ✕ 关闭 + blob 释放（同样用 JS dispatch，理由见 U7 上方的注释） */
  await page.evaluate(() => {
    const btns = Array.prototype.slice.call(document.querySelectorAll('#fPrevMenu .fp-bar button'));
    (btns.find((b) => (b.textContent || '').indexOf('✕') >= 0) || btns[btns.length - 1]).click();
  });
  /* 收起是 pmOut 动画（130ms）后再摘节点，先让动画跑完再断言，避免和动画抢时序 */
  await page.waitForTimeout(300);
  await menuGone();
  const revoked = await page.evaluate(() => window.__revoked.slice());
  chk('U8 ✕ 关闭：节点移除 + blob URL 被 revokeObjectURL 释放',
      revoked.length > 0 && revoked.every((u) => u.indexOf('blob:') === 0), revoked);

  /* U8b 连续两次 close 不许把浮层留在屏幕上
     2026-11-19 本测试在本条上抓到过真 bug：旧 closeFilePreview 先把上一个摘除定时器
     clearTimeout 掉、再因「已经 pm-closing」提前 return ⇒ 点 ✕ 的同时又滚动/点外部，
     就留下一个 class=pm-closing、没有定时器摘它的浮层，永久糊在页面上。 */
  await page.click('tr.file-expand.open .fn.pv-can');
  await page.waitForSelector('#fPrevMenu');
  await page.evaluate(() => { window.closeFilePreview(); window.closeFilePreview(); });
  await page.waitForTimeout(400);
  const ghost = await page.evaluate(() => {
    const n = document.querySelectorAll('#fPrevMenu').length;
    return { n: n, timer: String(window._fpCloseTimer) };
  });
  chk('U8b 连续两次 close → 浮层照样摘干净（不许留 pm-closing 鬼影）', ghost.n === 0, ghost);

  /* U9 滚动即关。开窗的 outside/scroll 监听是 setTimeout(0) 才挂的（否则开窗那一下的
     click 冒泡会立刻把窗关掉），所以这里必须等一拍再滚，否则「滚在监听挂上之前」就漏测了。 */
  await page.click('tr.file-expand.open .fn.pv-can');
  await page.waitForSelector('#fPrevMenu');
  await page.waitForTimeout(150);
  await page.evaluate(() => { document.body.style.minHeight = '3000px'; window.scrollTo(0, 200); });
  await menuGone();
  chk('U9 页面滚动 → 预览窗自动收起', true);

  /* U10 点外部即关（同样等一拍，理由见 U9） */
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(150);
  await page.click('tr.file-expand.open .fn.pv-can');
  await page.waitForSelector('#fPrevMenu');
  await page.waitForTimeout(150);
  await page.mouse.click(20, 20);
  await menuGone();
  chk('U10 点预览窗外空白 → 预览窗自动收起', true);

  /* U11 云端 404：显示云端 message（has_preview=true 但文件已清理） */
  await page.evaluate(() => {
    const rows = Array.prototype.slice.call(document.querySelectorAll('tr.order-row'));
    const second = rows.find((r) => r.getAttribute('data-order') &&
                                   r.getAttribute('data-order').indexOf('9002') >= 0);
    if (second && !second.classList.contains('expanded')) second.click();
  });
  await page.waitForFunction(() => {
    const rows = Array.prototype.slice.call(document.querySelectorAll('tr.file-expand.open .fn.pv-can'));
    return rows.some((f) => (f.textContent || '').indexOf('已被清理的旧单.pdf') >= 0);
  }, null, { timeout: 10000 });
  await page.evaluate(() => {
    const f = Array.prototype.slice.call(document.querySelectorAll('tr.file-expand.open .fn.pv-can'))
      .find((x) => (x.textContent || '').indexOf('已被清理的旧单.pdf') >= 0);
    f.click();
  });
  await page.waitForFunction(() => {
    const m = document.getElementById('fPrevMenu');
    if (!m) return false;
    const h = m.querySelector('.fp-hint');
    return h && (h.textContent || '').indexOf('文件已清理') >= 0;
  }, null, { timeout: 10000 });
  const errState = await page.evaluate(() => {
    const m = document.getElementById('fPrevMenu');
    const h = m.querySelector('.fp-hint');
    const img = m.querySelector('.fp-stage img');
    return { hint: (h.textContent || '').trim(), hintVisible: getComputedStyle(h).display !== 'none',
             imgShown: getComputedStyle(img).display !== 'none',
             hintColor: getComputedStyle(h).color };
  });
  chk('U11 云端 404 → 预览窗显示云端 message（不是白屏 / 不是裸 404）',
      errState.hint.indexOf('文件已清理') >= 0 && errState.hintVisible && !errState.imgShown, errState);

  /* U14 v7.27：本地单（整单没有 file_id）的文件名也该能点 —— 图由本地打印工具渲染，
     请求走 /api/local/order_thumb/<订单号>?name=<文件名>，只出首页（下一页禁用）。 */
  await page.evaluate(() => {
    const rows = Array.prototype.slice.call(document.querySelectorAll('tr.order-row'));
    const third = rows.find((r) => (r.textContent || '').indexOf('HN20261119-009003') >= 0);
    const icon = third && third.querySelector('.expand-icon');
    if (icon) icon.click();
  });
  await page.waitForSelector('tr.file-expand[data-suborder="9003"].open', { timeout: 10000 });
  await page.evaluate(() => {
    const el = document.querySelector('tr.file-expand[data-suborder="9003"] .fn.pv-can');
    if (el && el.scrollIntoView) el.scrollIntoView({ block: 'center', behavior: 'instant' });
    el.click();
  });
  await page.waitForFunction(() => {
    const img = document.querySelector('#fPrevMenu .fp-stage img');
    return img && img.naturalWidth > 0;
  }, null, { timeout: 15000 });
  const locHit = thumbHits[thumbHits.length - 1] || {};
  const locState = await page.evaluate(() => {
    const m = document.getElementById('fPrevMenu');
    const btns = Array.prototype.slice.call(m.querySelectorAll('.fp-bar button'));
    return { pageTxt: (m.querySelector('.fp-page') || {}).textContent || '',
             nextDis: (btns.find((b) => (b.textContent || '').indexOf('下一页') >= 0) || {}).disabled };
  });
  chk('U14 本地单文件名可点，请求走 /api/local/order_thumb/<订单号>?name=<文件名>（带令牌）',
      thumbHits.length === 1 && locHit.order === THUMB_ORDER_OK &&
      locHit.name === '本地单文件.pdf' && locHit.token === TOKEN, { hits: thumbHits });
  chk('U14b 本地缩略图只出首页：页码栏「第 1 页」且下一页禁用',
      locState.pageTxt.indexOf('第 1 页') >= 0 && locState.nextDis === true, locState);

  /* U15 本地图还没生成过（老订单）：404 的 message 要原样显示成提示，不许白屏。 */
  await page.evaluate(() => { window.closeFilePreview(); });
  await menuGone();
  await page.evaluate(() => {
    const rows = Array.prototype.slice.call(document.querySelectorAll('tr.order-row'));
    const fourth = rows.find((r) => (r.textContent || '').indexOf('HN20261119-009004') >= 0);
    const icon = fourth && fourth.querySelector('.expand-icon');
    if (icon) icon.click();
  });
  await page.waitForSelector('tr.file-expand[data-suborder="9004"].open', { timeout: 10000 });
  await page.evaluate(() => {
    const el = document.querySelector('tr.file-expand[data-suborder="9004"] .fn.pv-can');
    if (el && el.scrollIntoView) el.scrollIntoView({ block: 'center', behavior: 'instant' });
    el.click();
  });
  await page.waitForFunction(() => {
    const m = document.getElementById('fPrevMenu');
    if (!m) return false;
    const h = m.querySelector('.fp-hint');
    return h && (h.textContent || '').indexOf('本地缩略图') >= 0;
  }, null, { timeout: 10000 });
  const missState = await page.evaluate(() => {
    const m = document.getElementById('fPrevMenu');
    const h = m.querySelector('.fp-hint');
    const btns = Array.prototype.slice.call(m.querySelectorAll('.fp-bar button'));
    return { hint: (h.textContent || '').trim(),
             nextDis: (btns.find((b) => (b.textContent || '').indexOf('下一页') >= 0) || {}).disabled };
  });
  chk('U15 本地还没生成过图 → 显示接口的 message（不是白屏），下一页仍禁用',
      missState.hint.indexOf('本地缩略图') >= 0 && missState.nextDis === true, missState);
  chk('U15b 本地取图路径没有被误用到云端通道（previewHits 没有新增本地单请求）',
      previewHits.every((h) => h.id.indexOf('fid') === 0), previewHits);

  /* U12 全程干净 */
  chk('U12 无 JS 运行时错误（pageerror）', pageErrors.length === 0, pageErrors.slice(0, 3));
  chk('U12 无外网请求（全程只打本地桩）', externalReq.length === 0, externalReq.slice(0, 3));
} catch (e) {
  bad++;
  total++;
  console.log('[FAIL] 运行期异常  -- ' + (e && e.message ? e.message : String(e)));
} finally {
  await browser.close().catch(() => {});
  server.close();
}

console.log('');
console.log('==========================================================================');
if (bad === 0) {
  console.log('订单明细文件预览（真浏览器）验收：全部通过 ✅  （PASS ' + total + ' / ' + total + '）');
} else {
  console.log('订单明细文件预览（真浏览器）验收：失败 ❌  （FAIL ' + bad + ' / ' + total + '）');
}
process.exit(bad === 0 ? 0 : 1);
