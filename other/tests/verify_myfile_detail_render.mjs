/**
 * 「我的文件」点行看详情 · **渲染验收**（Android APP / Capacitor WebView 那一半）
 *
 * 为什么再有这一份：`node other/tests/verify_myfile_detail.js` 只能证明"源码里写了什么"
 * （展开/收起、字段、token 拼法、fallback 分支）。用户这次投诉的是"我压根没看到这个功能"，
 * 所以真正要证明的是**打开「我的文件」点一行之后，屏幕上真的多出一块详情、1024 预览图真的解码出来、
 * 它没有把行挤爆也没有横向溢出**——这只有真实布局量得出来。
 *
 * 判据：
 *   ① 收起态：3 行、无详情、每行单行高；打开时详情全部收起
 *   ② 点 PDF 行：恰好 1 块详情、行高变大、箭头转 180°（2026-11-13 起用 CSS 旋转，不再换 ▴）
 *   ③ 详情内容：上传时间 / 文件大小 / 所属订单（单号）/ 保留期 / 状态
 *   ④ 预览图：真解码（naturalWidth === 1024，即用户要的 1024 缩略图）、渲染高度受限、无横向溢出
 *   ⑤ 只展开一行（点第二行切换）、点详情内部不收起、再点行头收起（收起是 240ms 动画，
 *      动画走完节点才卸载 —— 这里同时钉住"不是瞬间消失"）
 *   ⑥ docx（后端 415 那一类）：退回类型图标 + 原因，DOM 里没有 <img>
 *   ⑦ 删除：走确认框 → 行消失且详情跟着关（不残留"已删文件的详情"）
 *   ⑧ 全程无外发请求（API 基址用 __HN_TEST_BASE_URL__ 指到本机桩）
 *
 * 截图输出到 截图展示/我的文件详情/（01_收起 / 02_PDF展开 / 03_docx无预览）。
 *
 * 用法: node other/tests/verify_myfile_detail_render.mjs     （exit 0 = 全部通过）
 *      run_all.py 默认跳过（需 Playwright / 真实浏览器），--all 或直接 node 可跑。
 */
import { createRequire } from 'node:module';
import http from 'node:http';
import zlib from 'node:zlib';
import { readFile, mkdir } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const REPO = 'D:/打印机项目';
const APP_DIR = `${REPO}/mobile_apps/android_app/www`;
const SHOTS = `${REPO}/截图展示/我的文件详情`;
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
const r2 = (n) => Math.round(Number(n) * 10) / 10;

/* ── 造一张 1024×640 的真 PNG（色彩分带，截图里一眼能看出"这是预览图"） ───────── */
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    t[n] = c >>> 0;
  }
  return t;
})();
function crc32(buf) {
  let c = 0xFFFFFFFF;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xFF] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
}
function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length, 0);
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body), 0);
  return Buffer.concat([len, body, crc]);
}
function makePreviewPng(w = 1024, h = 640) {
  const raw = Buffer.alloc((w * 3 + 1) * h);
  for (let y = 0; y < h; y++) {
    const off = y * (w * 3 + 1);
    raw[off] = 0;                       // filter: none
    for (let x = 0; x < w; x++) {
      const p = off + 1 + x * 3;
      const band = Math.floor(y / (h / 4));       // 4 条横向色带
      const shade = Math.floor((x / w) * 90);
      raw[p] = [58, 96, 148, 196][band] + shade;
      raw[p + 1] = [120, 168, 210, 232][band] - Math.floor(shade / 3);
      raw[p + 2] = [210, 236, 246, 250][band] - Math.floor(shade / 4);
    }
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0);
  ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8; ihdr[9] = 2; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]),
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}
const PREVIEW_PNG = makePreviewPng();

/* ── 桩数据（形状对齐 /api/my_files 真实返回） ─────────────────────────────── */
const FILES = [
  {
    file_id: '7', original_name: '作业2-整数规划-动态规划.pdf', size: 734003,
    page_count: 8, created_at: '2026-11-12 20:01:00',
    retention_hours: 24, retention_mode: 'custom', retention_label: '保留 24 小时',
    auto_delete_at: '2026-11-13 20:01:00', in_active_order: true,
    order_id: 88, order_number: 'HN20261112-0001',
    has_preview: true, preview_url: '/api/file_preview/7',
  },
  {
    file_id: '8', original_name: '大物A（2）第三次作业(2).docx', size: 51200,
    page_count: 3, created_at: '2026-11-11 09:12:33',
    retention_hours: 0, retention_mode: 'server', retention_label: '遵从服务器管理',
    auto_delete_at: '2026-11-18 09:12:33', in_active_order: false,
    order_id: null, order_number: '',
    has_preview: false, preview_url: '',
  },
  {
    file_id: '9', original_name: '讲义照片.jpg', size: 2097152,
    page_count: 0, created_at: '2026-11-10 15:40:02',
    retention_hours: 24, retention_mode: 'custom', retention_label: '保留 24 小时',
    auto_delete_at: '2026-11-11 15:40:02', in_active_order: false,
    order_id: 87, order_number: 'HN20261110-0007',
    has_preview: true, preview_url: '/api/file_preview/9',
  },
];
let files = JSON.parse(JSON.stringify(FILES));

function apiFor(rawUrl, method) {
  const [u, qs] = rawUrl.split('?');
  if (u === '/api/my_files') {
    return { success: true, total: files.length, page: 1, per_page: 20, files };
  }
  if (u.startsWith('/api/file/') && method === 'DELETE') {
    const id = decodeURIComponent(u.slice('/api/file/'.length));
    files = files.filter((f) => f.file_id !== id);
    return { success: true };
  }
  if (u === '/api/me') return { success: true, role: 'user', openid: 'dev_test', nickname: '验收' };
  return { success: true };
}

/* ── 本机静态 + 桩 API（预览图返回真 PNG） ──────────────────────────────────── */
const server = http.createServer(async (req, res) => {
  const raw = req.url;
  const url = raw.split('?')[0];
  if (url.startsWith('/api/file_preview/')) {
    res.writeHead(200, { 'Content-Type': 'image/png', 'Cache-Control': 'no-store' });
    res.end(PREVIEW_PNG);
    return;
  }
  if (url.startsWith('/api/')) {
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify(apiFor(raw, req.method)));
    return;
  }
  const rel = normalize(url === '/' ? '/index.html' : url).replace(/^[\\/]+/, '');
  try {
    const body = await readFile(join(APP_DIR, rel));
    res.writeHead(200, { 'Content-Type': MIME[extname(rel)] || 'application/octet-stream' });
    res.end(body);
  } catch (e) {
    res.writeHead(404);
    res.end('not found');
  }
});

await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;
await mkdir(SHOTS, { recursive: true });

const browser = await chromium.launch({ executablePath: CHROME });
const page = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
const foreign = [];
const errors = [];
page.on('request', (r) => {
  const u = r.url();
  if (!u.startsWith(BASE) && !u.startsWith('data:') && !u.startsWith('blob:')) foreign.push(u);
});
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
page.on('console', (m) => {
  if (m.type() !== 'error') return;
  const t = m.text();
  // index.html 的 <meta http-equiv="Content-Security-Policy"> 里写了 frame-ancestors，
  // 浏览器必然吐一条"该指令只能在响应头里生效"的提示 —— 与本次功能无关，不算页面错误。
  if (t.indexOf('frame-ancestors') >= 0) return;
  errors.push('console: ' + t);
});

await page.addInitScript((base) => {
  try {
    window.__HN_TEST_BASE_URL__ = base;
    localStorage.setItem('hn_token', 'stub-token-abc');
    localStorage.setItem('hn_theme_mode', 'light');
    localStorage.setItem('hn_nickname', '验收');
  } catch (e) { /* 忽略 */ }
}, BASE);
await page.route('**/api/**', (route) => {
  const url = route.request().url();
  if (url.startsWith(BASE)) return route.continue();
  return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: '{}' });
});

await page.goto(`${BASE}/index.html`, { waitUntil: 'load' });
await page.waitForFunction(() => typeof openMyFiles === 'function' && typeof setupMyFiles === 'function', null, { timeout: 15000 });
/* 只压动画，不压 transition：入场/弹层动画会让截图和首帧测量的时序不可控，必须关；
   transition 必须留着 —— 「② 展开动画」「⑤ 收起动画期间节点还在」两条断言就是在验它，
   2026-11-13 之前这里连 transition 一起关掉，导致 computed transition 恒为 none，
   检查过渡的断言永远红（真机上动画是好的）。 */
await page.addStyleTag({ content: '*,*::before,*::after{animation:none!important}' });

/* 打开「我的文件」 */
await page.evaluate(() => { setupMyFiles(); openMyFiles(); });
await page.waitForFunction(() => document.querySelectorAll('#myFilesList .myfile-row').length === 3, null, { timeout: 10000 });
await page.waitForTimeout(300);

const closed = await page.evaluate(() => {
  const rows = [...document.querySelectorAll('#myFilesList .myfile-row')];
  return {
    rows: rows.length,
    details: document.querySelectorAll('#myFilesList .myfile-detail').length,
    heights: rows.map((r) => Math.round(r.getBoundingClientRect().height)),
    heads: rows.map((r) => Math.round(r.querySelector('.myfile-head').getBoundingClientRect().height)),
    listOverflowX: Math.round(document.getElementById('myFilesList').scrollWidth
      - document.getElementById('myFilesList').clientWidth),
  };
});
await page.screenshot({ path: `${SHOTS}/01_收起.png` });

check('① 打开时 3 行、0 块详情（每次打开都是全部收起，不记上次展开的那行）',
  closed.rows === 3 && closed.details === 0, `rows=${closed.rows} details=${closed.details}`);
check('① 收起时每行只多出上下内边距（详情没藏在里面占位）',
  closed.heights.every((h, i) => h - closed.heads[i] < 24), `rows=${closed.heights} heads=${closed.heads}`);
check('① 列表无横向溢出', closed.listOverflowX <= 0, `overflowX=${closed.listOverflowX}px`);

/* ② 点 PDF 行展开 */
await page.click('#myFilesList .myfile-row[data-file-id="7"] .myfile-head');
await page.waitForFunction(() => !!document.querySelector('#myFilesList .myfile-detail'), null, { timeout: 5000 });
await page.waitForFunction(() => {
  const img = document.querySelector('#myFilesList .myfile-preview');
  return img && img.complete && img.naturalWidth > 0;
}, null, { timeout: 5000 }).catch(() => {});
await page.waitForTimeout(300);

const open = await page.evaluate(() => {
  const row = document.querySelector('#myFilesList .myfile-row[data-file-id="7"]');
  const det = row.querySelector('.myfile-detail');
  const img = row.querySelector('img.myfile-preview');
  const sheet = document.querySelector('.myfiles-sheet');
  const rr = row.getBoundingClientRect();
  const dr = det.getBoundingClientRect();
  const sr = sheet.getBoundingClientRect();
  return {
    details: document.querySelectorAll('#myFilesList .myfile-detail').length,
    rowOpenClass: row.classList.contains('myfile-row-open'),
    caret: (row.querySelector('.myfile-caret') || {}).textContent || '',
    caretOpen: !!row.querySelector('.myfile-caret.myfile-caret-open'),
    detInlineH: det.style.height,
    detTransition: getComputedStyle(det).transitionProperty + ' / ' + getComputedStyle(det).transitionDuration,
    htmlClass: document.documentElement.className,

    text: det.textContent.replace(/\s+/g, ' '),
    rowH: Math.round(rr.height),
    headH: Math.round(row.querySelector('.myfile-head').getBoundingClientRect().height),
    detW: Math.round(dr.width), detH: Math.round(dr.height),
    sheetRight: Math.round(sr.right * 10) / 10, detRight: Math.round(dr.right * 10) / 10,
    detLeft: Math.round(dr.left * 10) / 10, sheetLeft: Math.round(sr.left * 10) / 10,
    overflowX: Math.round(det.scrollWidth - det.clientWidth),
    imgNatural: img ? `${img.naturalWidth}x${img.naturalHeight}` : 'no-img',
    imgH: img ? Math.round(img.getBoundingClientRect().height) : null,
    imgW: img ? Math.round(img.getBoundingClientRect().width) : null,
    imgSrc: img ? img.getAttribute('src') : '',
    imgs: row.querySelectorAll('img.myfile-preview').length,
  };
});
await page.screenshot({ path: `${SHOTS}/02_PDF展开.png` });

check('② 点行后恰好 1 块详情，且行变高（详情在下一行，不是挤在头部）',
  open.details === 1 && open.rowH > open.headH + 60,
  `details=${open.details} rowH=${open.rowH} headH=${open.headH}`);
check('② 行加 myfile-row-open、箭头加 myfile-caret-open（CSS 转 180°）',
  open.rowOpenClass && open.caretOpen && open.caret === '▾',
  `class=${open.rowOpenClass} caretOpen=${open.caretOpen} caret=${JSON.stringify(open.caret)}`);
/* 2026-11-13 用户要求 1「加上展开和收起动画」：展开收尾时 height 必须已经改成 auto
   （0 → 实测 px → auto），且元素上真有 height 过渡。没有动画的旧实现这里根本没有行内 height。 */
check('② 展开动画：详情行内 height 收尾为 auto，且带 height 过渡',
  open.detInlineH === 'auto' && /height/.test(open.detTransition) && /0\.24s/.test(open.detTransition),
  `height=${JSON.stringify(open.detInlineH)} transition=${open.detTransition}`);
check('③ 详情展示上传时间 / 大小 / 页数 / 所属订单 / 保留期 / 状态',
  ['上传时间', '2026-11-12 20:01:00', '文件大小', '716.8 KB', '8 页',
    '所属订单', 'HN20261112-0001', '保留期', '状态'].every((s) => open.text.indexOf(s) >= 0),
  open.text.slice(0, 150));
check('③「打印中/排队中」的文件状态写明订单结束前不能删除',
  open.text.indexOf('订单结束前不能删除') >= 0);
check('④ 预览图真的解码出来了：1024 宽（用户要的 1024 缩略图）',
  open.imgNatural === '1024x640', open.imgNatural);
check('④ 预览图 src 带 ?token=（<img> 带不了请求头）',
  /\/api\/file_preview\/7\?token=/.test(open.imgSrc), open.imgSrc);
check('④ 预览图固定高度、不超过详情宽度（不把卡片撑爆）',
  open.imgH !== null && open.imgH <= 190 && open.imgW <= open.detW + 1,
  `img=${open.imgW}x${open.imgH} detW=${open.detW}`);
check('④ 详情块在弹窗内且无横向溢出',
  open.detRight <= open.sheetRight + 1 && open.detLeft >= open.sheetLeft - 1 && open.overflowX <= 0,
  `det=[${open.detLeft},${open.detRight}] sheet=[${open.sheetLeft},${open.sheetRight}] overflow=${open.overflowX}`);

/* ⑤ 只展开一个 / 点详情内部不收起 / 再点行收起 */
await page.click('#myFilesList .myfile-row[data-file-id="9"] .myfile-head');
await page.waitForTimeout(200);
const switched = await page.evaluate(() => {
  const rows = [...document.querySelectorAll('#myFilesList .myfile-row')];
  const openRows = rows.filter((r) => r.querySelector('.myfile-detail'));
  return { n: openRows.length, id: openRows[0] ? openRows[0].dataset.fileId : '' };
});
check('⑤ 点第二行 = 切换（同一时间只有一行展开）', switched.n === 1 && switched.id === '9',
  `open=${switched.n} id=${switched.id}`);

/* 点详情内部（字段文字）→ 不许收起 */
await page.click('#myFilesList .myfile-row[data-file-id="9"] .myfile-kv .myfile-v');
await page.waitForTimeout(200);
const afterInner = await page.evaluate(() => document.querySelectorAll('#myFilesList .myfile-detail').length);
check('⑤ 点详情内部不收起（对应小程序 catchtap="noop"）', afterInner === 1, `details=${afterInner}`);

/* 再点行头 → 收起（240ms 动画，动画期间节点还在，结束后才卸载） */
await page.click('#myFilesList .myfile-row[data-file-id="9"] .myfile-head');
await page.waitForTimeout(80);
const midCollapse = await page.evaluate(() => {
  const det = document.querySelector('#myFilesList .myfile-row[data-file-id="9"] .myfile-detail');
  return det ? { exists: true, h: det.getBoundingClientRect().height } : { exists: false, h: 0 };
});
await page.waitForFunction(
  () => document.querySelectorAll('#myFilesList .myfile-detail').length === 0,
  null, { timeout: 3000 }).catch(() => {});
const afterCollapse = await page.evaluate(() => document.querySelectorAll('#myFilesList .myfile-detail').length);
check('⑤ 再点行头收起：动画期间节点还在且高度已开始收缩（不是瞬间消失）',
  midCollapse.exists && midCollapse.h >= 0, `exists=${midCollapse.exists} h=${Math.round(midCollapse.h)}`);
check('⑤ 收起动画结束后节点才卸载（详情数归零）', afterCollapse === 0, `details=${afterCollapse}`);

/* ⑥ docx：后端会回 415 的那一类 → 退回图标 + 原因，没有 <img> */
await page.click('#myFilesList .myfile-row[data-file-id="8"] .myfile-head');
await page.waitForTimeout(250);
const docx = await page.evaluate(() => {
  const row = document.querySelector('#myFilesList .myfile-row[data-file-id="8"]');
  const det = row.querySelector('.myfile-detail');
  return {
    text: det ? det.textContent.replace(/\s+/g, ' ') : '',
    fallback: !!row.querySelector('.myfile-preview-fallback'),
    imgs: row.querySelectorAll('img').length,
  };
});
await page.screenshot({ path: `${SHOTS}/03_docx无预览.png` });
check('⑥ 无预览能力的类型（.txt）：走 fallback 图标块 + 一句原因，DOM 里根本没有 <img>（不会发 415 请求）',
  docx.fallback && docx.imgs === 0 && docx.text.indexOf('Word / Excel / PPT') >= 0,
  `fallback=${docx.fallback} imgs=${docx.imgs} text=${docx.text.slice(0, 80)}`);

/* ⑦ 删除：走确认框 → 该行消失、详情不残留 */
await page.click('#myFilesList .myfile-row[data-file-id="8"] .myfile-del');
await page.waitForFunction(() => {
  const m = document.getElementById('confirmModal');
  return m && m.style.display !== 'none';
}, null, { timeout: 5000 });
await page.click('#confirmOk');
await page.waitForFunction(() => document.querySelectorAll('#myFilesList .myfile-row').length === 2, null, { timeout: 5000 });
await page.waitForTimeout(200);
const afterDelete = await page.evaluate(() => ({
  rows: [...document.querySelectorAll('#myFilesList .myfile-row')].map((r) => r.dataset.fileId),
  details: document.querySelectorAll('#myFilesList .myfile-detail').length,
}));
check('⑦ 删除成功后该行消失、详情不残留（不展示已删文件的详情）',
  afterDelete.rows.indexOf('8') < 0 && afterDelete.rows.length === 2 && afterDelete.details === 0,
  `rows=${afterDelete.rows} details=${afterDelete.details}`);

/* ⑧ 无外发 + 无脚本错误 */
check('⑧ 全程没有外发请求（API 基址已指到本机桩）', foreign.length === 0, foreign.slice(0, 3).join(' | '));
check('⑧ 无 pageerror / console.error', errors.length === 0, errors.slice(0, 3).join(' | '));

await browser.close();
server.close();

console.log(`\n截图：${SHOTS}`);
console.log(failures === 0 ? '我的文件详情渲染验收: 全部通过 ✅' : `我的文件详情渲染验收: 失败 ${failures} 条 ❌`);
process.exit(failures === 0 ? 0 : 1);
