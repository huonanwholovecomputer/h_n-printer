/* verify_preview_render.mjs — 文件预览「渲染级」验收（真实 index.html + styles.css + print.js）
 *
 * 前一份 tests/verify_file_preview.js 用桩环境验行为分派；本脚本用 Playwright + 真实页面
 * 验「看得见的东西」：
 *   1) 卡片头部「预览」胶囊不改变 header 行高（小程序卡片高度是硬编码常量，行高必须不变）
 *   2) 图片预览：objectURL 真正渲染出图片（naturalWidth > 0）
 *   3) 文本预览：等宽 pre、white-space: pre-wrap、抽屉高度足够、截断提示
 *   4) 关闭：内容清空（objectURL 回收）
 * 顺带产出截图（写入 .gitignore 的 截图展示/文件预览/）。
 *
 * 用法: node tests/verify_preview_render.mjs
 */
import { createRequire } from 'node:module';
import http from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const APP_DIR = 'D:/打印机项目/mobile_apps/android_app/www';
const SHOTS = 'D:/打印机项目/截图展示/文件预览';
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.json': 'application/json',
};

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? `  -- ${detail}` : ''));
}

const MOCK = {
  success: true, token: 'tok', openid: 'dev_test', nickname: '测试设备', role: 'admin',
  devices: [], orders: [], files: [], users: [], admins: [], keys: [], total: 0, count: 0,
  online: true, active: true, take_orders_online: true, active_client_id: 'pc-1',
  claiming_devices: [], delivery_locations: ['1号楼北楼'], urgency_levels: ['低'],
};

const server = http.createServer(async (req, res) => {
  const url = req.url.split('?')[0];
  if (url.startsWith('/api/')) {
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
    if (url.startsWith('/api/pricing')) {
      res.end(JSON.stringify({
        success: true,
        pricing: {
          simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1,
          delivery_locations: ['1号楼北楼'], delivery_percentages: { '1号楼北楼': 0 },
          urgency_levels: ['低'], urgency_prices: { '低': 0 },
        },
      }));
      return;
    }
    res.end(JSON.stringify(MOCK));
    return;
  }
  const rel = normalize(url === '/' ? '/index.html' : url).replace(/^[\\/]+/, '');
  try {
    const body = await readFile(join(APP_DIR, rel));
    res.writeHead(200, { 'Content-Type': MIME[extname(rel)] || 'application/octet-stream' });
    res.end(body);
  } catch (e) {
    res.writeHead(404); res.end('not found');
  }
});

await new Promise((r) => server.listen(0, '127.0.0.1', r));
const port = server.address().port;
await mkdir(SHOTS, { recursive: true });

const browser = await chromium.launch({ executablePath: CHROME });
const page = await browser.newPage({ viewport: { width: 430, height: 932 }, deviceScaleFactor: 2 });
page.on('pageerror', (e) => console.log('  [pageerror] ' + e.message));
page.on('console', (m) => { if (m.type() === 'error') console.log('  [console.error] ' + m.text()); });

/* 必须隔离到本地服务器：app 的 DEFAULT_BASE_URL 是 https://hn-space.cn，
   不覆盖 hn_base_url 会打到生产接口（device_login / claiming_devices / pricing…）。 */
const LOCAL_BASE = `http://127.0.0.1:${port}`;
const foreign = [];
page.on('request', (r) => {
  const u = r.url();
  if (!u.startsWith(LOCAL_BASE) && !u.startsWith('data:') && !u.startsWith('blob:')) foreign.push(u);
});
await page.addInitScript((base) => {
  try { localStorage.setItem('hn_base_url', base); } catch (e) { /* 忽略 */ }
}, LOCAL_BASE);
// 双保险：即使有漏网的接口调用，也在网络层拦掉（绝不落到生产）
await page.route('**/api/**', (route) => {
  const url = route.request().url();
  if (url.startsWith(LOCAL_BASE)) return route.continue();
  return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(MOCK) });
});
await page.goto(`${LOCAL_BASE}/index.html`, { waitUntil: 'load' });
// printState 是脚本顶层 const（全局词法绑定，不是 window 属性）→ 直接在全局作用域里求值
await page.waitForFunction(
  () => !!document.getElementById('fileList') && typeof printState !== 'undefined' && typeof renderFileList === 'function',
  null, { timeout: 15000 },
);

/* 注入三个文件（图片 / Word / Markdown），与真实选文件后的数据结构一致 */
await page.evaluate(() => {
  const mk = (over) => Object.assign({
    _uid: 1, name: 'a.txt', size: 1024, file: null, fileId: 'F1', uploading: false, progress: 100,
    failed: false, copies: 1, pageRange: '', rangeLines: [{ value: '', error: '' }], duplex: 'on',
    imageOrientation: 'auto', entering: false, removing: false, excelWarning: false,
    unsupportedFormat: false, isImage: false, pageCount: 1, pageCountStatus: 'confirmed',
    singlePage: false, sizeDisplay: '12.0',
  }, over || {});
  const c = document.createElement('canvas');
  c.width = 240; c.height = 160;
  const ctx = c.getContext('2d');
  ctx.fillStyle = '#0A84FF'; ctx.fillRect(0, 0, 240, 160);
  ctx.fillStyle = '#fff'; ctx.font = '20px sans-serif'; ctx.fillText('示例图片', 60, 88);
  const dataUrl = c.toDataURL('image/png');
  const bstr = atob(dataUrl.split(',')[1]);
  const arr = new Uint8Array(bstr.length);
  for (let i = 0; i < bstr.length; i++) arr[i] = bstr.charCodeAt(i);
  const imgFile = new File([arr], '示例照片.png', { type: 'image/png' });
  const mdText = Array.from({ length: 40 }, (_, i) => `第 ${i + 1} 行：这是一段用于预览验收的示例文本。`).join('\n');
  printState.selectedFiles = [
    mk({ _uid: 1, name: '示例照片.png', isImage: true, duplex: 'off', singlePage: true, sizeDisplay: '24.0', file: imgFile, fileId: 'F1' }),
    mk({ _uid: 2, name: '打印说明.md', sizeDisplay: '3.2', file: new File([mdText], '打印说明.md', { type: 'text/markdown' }), fileId: 'F2' }),
    mk({ _uid: 3, name: '报告.docx', sizeDisplay: '48.0', file: new File(['x'], '报告.docx'), fileId: 'F3', pageCount: 0, pageCountStatus: 'analyzing' }),
  ];
  renderFileList();
});
await page.waitForTimeout(600);

/* 1) 预览入口不影响卡片行高（小程序卡片高度是硬编码常量，行高必须不变） */
const headerH = await page.evaluate(() => {
  const top = document.querySelector('#fileList .file-card-top');
  const pill = top.querySelector('.file-preview-btn');
  const withPill = top.getBoundingClientRect().height;
  pill.style.display = 'none';
  const withoutPill = top.getBoundingClientRect().height;
  pill.style.display = '';
  return { withPill, withoutPill };
});
check('①预览胶囊不改变卡片 header 行高（保持每类型恒定高度）',
  Math.abs(headerH.withPill - headerH.withoutPill) < 0.5,
  `有胶囊 ${headerH.withPill.toFixed(2)}px vs 无胶囊 ${headerH.withoutPill.toFixed(2)}px`);

const pillVisible = await page.locator('#fileList .file-card-top .file-preview-btn').first().isVisible();
check('①卡片头部渲染出「预览」胶囊', pillVisible);

await page.screenshot({ path: join(SHOTS, '01-卡片预览入口.png'), clip: { x: 0, y: 200, width: 430, height: 420 } });

/* 2) 文本预览（md）：抽屉 + 等宽 pre + pre-wrap + 截断提示隐藏 */
await page.evaluate(() => previewFile(1));
await page.waitForSelector('#previewMask', { state: 'visible', timeout: 5000 });
await page.waitForTimeout(350);
const textShot = await page.evaluate(() => {
  const sheet = document.querySelector('.preview-sheet');
  const pre = document.querySelector('#previewBody .preview-text');
  const cs = getComputedStyle(pre);
  return {
    title: document.getElementById('previewTitle').textContent,
    sheetH: sheet.getBoundingClientRect().height,
    vh: window.innerHeight,
    whiteSpace: cs.whiteSpace,
    fontFamily: cs.fontFamily,
    textLen: pre.textContent.length,
    firstLine: pre.textContent.split('\n')[0],
    truncatedDisplay: getComputedStyle(document.getElementById('previewTruncated')).display,
  };
});
check('②文本预览：标题为文件名', textShot.title === '打印说明.md', textShot.title);
check('②文本预览：抽屉占屏 80% 以上', textShot.sheetH / textShot.vh > 0.8, (textShot.sheetH / textShot.vh).toFixed(2));
check('②文本预览：pre-wrap 保留换行', textShot.whiteSpace === 'pre-wrap', textShot.whiteSpace);
check('②文本预览：等宽字体', /mono|Menlo|Consolas/i.test(textShot.fontFamily), textShot.fontFamily);
check('②文本预览：内容完整且首行正确', textShot.textLen > 800 && /第 1 行/.test(textShot.firstLine), `${textShot.textLen} 字符 / ${textShot.firstLine}`);
check('②文本预览：未截断时隐藏截断提示', textShot.truncatedDisplay === 'none', textShot.truncatedDisplay);
/* 悬浮 tabBar（z-index 9999）不得盖住抽屉底部正文 */
const stack = await page.evaluate(() => {
  const sheet = document.querySelector('.preview-sheet');
  const tab = document.querySelector('.tab-bar');
  const r = sheet.getBoundingClientRect();
  const hit = document.elementFromPoint(window.innerWidth / 2, Math.min(r.bottom - 6, window.innerHeight - 6));
  return {
    maskZ: getComputedStyle(document.getElementById('previewMask')).zIndex,
    tabZ: tab ? getComputedStyle(tab).zIndex : 'none',
    hitInPreview: !!(hit && hit.closest && hit.closest('.preview-sheet')),
    hitCls: hit ? (hit.className || hit.tagName) : 'none',
  };
});
check('②预览层压在悬浮 tabBar 之上（抽屉底部不被遮挡）',
  stack.hitInPreview && Number(stack.maskZ) > Number(stack.tabZ || 0),
  `maskZ=${stack.maskZ} tabZ=${stack.tabZ} 命中=${stack.hitCls}`);
await page.screenshot({ path: join(SHOTS, '02-文本预览.png') });

/* 3) 关闭预览：内容清空 */
await page.click('#previewClose');
await page.waitForTimeout(400);
const closed = await page.evaluate(() => ({
  visible: getComputedStyle(document.getElementById('previewMask')).display !== 'none',
  body: document.getElementById('previewBody').innerHTML,
}));
check('③关闭预览：遮罩隐藏且内容清空', !closed.visible && closed.body === '', `visible=${closed.visible}`);

/* 4) 图片预览：objectURL 真正出图 */
await page.evaluate(() => previewFile(0));
await page.waitForSelector('#previewMask', { state: 'visible', timeout: 5000 });
await page.waitForFunction(() => {
  const img = document.querySelector('#previewBody .preview-image');
  return img && img.complete && img.naturalWidth > 0;
}, null, { timeout: 5000 }).catch(() => {});
const imgShot = await page.evaluate(() => {
  const img = document.querySelector('#previewBody .preview-image');
  return {
    src: img ? img.getAttribute('src').slice(0, 12) : '',
    natural: img ? `${img.naturalWidth}x${img.naturalHeight}` : 'none',
    maskBg: getComputedStyle(document.getElementById('previewMask')).backgroundColor,
  };
});
check('④图片预览：blob 地址渲染成功', imgShot.src.indexOf('blob:') === 0, imgShot.src);
check('④图片预览：图片实际解码（240x160）', imgShot.natural === '240x160', imgShot.natural);
check('④图片预览：遮罩有半透明背景', /rgba\(/.test(imgShot.maskBg), imgShot.maskBg);
await page.waitForTimeout(600);   // 等抽屉入场动画（0.28s）结束再截图
await page.screenshot({ path: join(SHOTS, '03-图片预览.png') });

/* 5) 超长文本截断提示 */
await page.evaluate(() => {
  printState.selectedFiles[1].file = new File(['y'.repeat(25000)], '超长.md', { type: 'text/markdown' });
  printState.selectedFiles[1].name = '超长文本.md';
});
await page.evaluate(() => closePreview());
await page.waitForTimeout(300);
await page.evaluate(() => previewFile(1));
await page.waitForSelector('#previewMask', { state: 'visible', timeout: 5000 });
await page.waitForTimeout(350);
const trunc = await page.evaluate(() => ({
  len: document.querySelector('#previewBody .preview-text').textContent.length,
  tip: getComputedStyle(document.getElementById('previewTruncated')).display !== 'none',
  title: document.getElementById('previewTitle').textContent,
}));
check('⑤超长文本：截断到 20000 字符并弹出截断提示', trunc.len === 20000 && trunc.tip, `${trunc.len} 字符 tip=${trunc.tip}`);
await page.screenshot({ path: join(SHOTS, '04-超长文本截断.png') });

/* 6) 上传进度只更新两个节点，不整表重渲染（否则 card-entering 入场动画每 500ms 重播） */
const progress = await page.evaluate(() => {
  const f = {
    _uid: 99, name: '上传中.pdf', size: 4096, file: null, fileId: null, uploading: true, progress: 10,
    failed: false, copies: 1, pageRange: '', rangeLines: [{ value: '', error: '' }], duplex: 'on',
    imageOrientation: 'auto', entering: true, removing: false, excelWarning: false,
    unsupportedFormat: false, isImage: false, pageCount: 0, pageCountStatus: '', singlePage: false,
    sizeDisplay: '4.0',
  };
  printState.selectedFiles = [f];
  renderFileList();
  const card0 = document.querySelector('#fileList .file-card');
  const anim0 = card0 && card0.getAnimations ? card0.getAnimations().length : 0;
  f.progress = 55;
  updateUploadProgressDOM(f);
  const card1 = document.querySelector('#fileList .file-card');
  return {
    sameNode: card0 === card1,
    pct: card1.querySelector('.upload-pct').textContent,
    width: card1.querySelector('.progress-fill').style.width,
    animations: anim0,
  };
});
check('⑥上传进度只改进度节点：卡片 DOM 未被重建（入场动画不会重播）',
  progress.sameNode === true && progress.pct === '55%' && progress.width === '55%',
  JSON.stringify(progress));
check('⑥上传中卡片确实在播入场动画（card-entering，说明"重建即重播"并非空谈）',
  progress.animations > 0, 'animations=' + progress.animations);

/* 7) PDF/Word 在 APP 内的明确提示（WebView 无原生渲染能力） */await page.evaluate(() => closePreview());
await page.waitForTimeout(300);
const unsupported = await page.evaluate(() => {
  printState.selectedFiles[0].name = '报告.docx';
  printState.selectedFiles[0].file = new File(['x'], '报告.docx');
  printState.selectedFiles[0].isImage = false;
  const before = document.getElementById('toast') ? document.getElementById('toast').textContent : '';
  previewFile(0);
  const el = document.getElementById('toast');
  return { before, after: el ? el.textContent : '', maskVisible: getComputedStyle(document.getElementById('previewMask')).display !== 'none' };
});
check('⑦Word：不打开预览层，toast 明确提示不支持',
  !unsupported.maskVisible && /暂不支持预览/.test(unsupported.after), unsupported.after);

await browser.close();
server.close();
check('⑧全程未触碰生产/外部网络（BASE_URL 已隔离到本地桩）',
  foreign.length === 0, foreign.slice(0, 3).join(', '));
console.log('\n' + '='.repeat(52));
console.log('文件预览渲染验收:', failures === 0 ? '全部通过 ✅' : `存在 ${failures} 项失败 ❌`);
console.log('截图目录: ' + SHOTS);
process.exit(failures ? 1 : 0);
