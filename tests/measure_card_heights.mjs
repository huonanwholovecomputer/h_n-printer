/* measure_card_heights.mjs — 用真实页面实测「各类型文件卡片高度」，与小程序的
 * FILE_CARD_HEIGHT_RPX 硬编码常量对照（headless 版 _probeCardHeights）。
 *
 * 为什么要紧：小程序「内层列表滚到边 → 外层接管滚动」判据里用
 *   maxScroll = 估算内容高(常量累加) - 列表可视高
 * 若常量与实际渲染高度偏差大，atBottom 永远不成立（或提前成立），滚动接力就失效。
 *
 * 用法: node tests/measure_card_heights.mjs
 */
import { createRequire } from 'node:module';
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const APP_DIR = 'D:/打印机项目/mobile_apps/android_app/www';
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.png': 'image/png' };
const MOCK = { success: true, token: 'tok', openid: 'dev_test', nickname: 't', role: 'admin', devices: [], orders: [], online: true, active: true, claiming_devices: [] };

/* 小程序 FILE_CARD_HEIGHT_RPX（rpx）+ _fileCardTypeKey 的分派条件 */
const MP_CONST = {
  image: 312.8, 'word-grid': 451.1, 'word-grid-single': 385.1, 'word-text': 477.1,
  'word-text-single': 411.1, 'word-single': 311.0, excel: 151.2,
};
const FILE_CARD_GAP_RPX = 12;

const server = http.createServer(async (req, res) => {
  const url = req.url.split('?')[0];
  if (url.startsWith('/api/')) {
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify(MOCK));
    return;
  }
  const rel = normalize(url === '/' ? '/index.html' : url).replace(/^[\\/]+/, '');
  try {
    const body = await readFile(join(APP_DIR, rel));
    res.writeHead(200, { 'Content-Type': MIME[extname(rel)] || 'application/octet-stream' });
    res.end(body);
  } catch (e) { res.writeHead(404); res.end('nf'); }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const port = server.address().port;
const browser = await chromium.launch({ executablePath: CHROME });
const page = await browser.newPage({ viewport: { width: 430, height: 932 } });
await page.addInitScript((base) => { try { localStorage.setItem('hn_base_url', base); } catch (e) {} }, `http://127.0.0.1:${port}`);
await page.route('**/api/**', (route) => route.request().url().startsWith(`http://127.0.0.1:${port}`)
  ? route.continue()
  : route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK) }));
await page.goto(`http://127.0.0.1:${port}/index.html`, { waitUntil: 'load' });
await page.waitForFunction(() => !!document.getElementById('fileList') && typeof printState !== 'undefined', null, { timeout: 15000 });

const measured = await page.evaluate(() => {
  const mk = (over) => Object.assign({
    _uid: 1, name: 'a.pdf', size: 1024, file: new File(['x'], 'a.pdf'), fileId: 'F1',
    uploading: false, progress: 100, failed: false, copies: 1, pageRange: '',
    rangeLines: [{ value: '', error: '' }], duplex: 'on', imageOrientation: 'auto',
    entering: false, removing: false, excelWarning: false, unsupportedFormat: false,
    isImage: false, pageCount: 10, pageCountStatus: 'confirmed', singlePage: false, sizeDisplay: '12.0',
  }, over || {});
  /* 与小程序 _fileCardTypeKey 的 7 类一一对应 */
  const cases = {
    image: mk({ name: 'a.png', isImage: true, duplex: 'off', pageCount: 1, pageCountStatus: '' }),
    'word-grid': mk({ name: 'a.pdf', pageCount: 10, pageCountStatus: 'confirmed', singlePage: false }),
    'word-grid-single': mk({ name: 'a.pdf', pageCount: 10, pageCountStatus: 'confirmed', singlePage: true, pageRange: '5', rangeLines: [{ value: '5', error: '' }, { value: '', error: '' }] }),
    'word-text': mk({ name: 'a.docx', pageCount: 0, pageCountStatus: 'analyzing', singlePage: false }),
    'word-text-single': mk({ name: 'a.docx', pageCount: 0, pageCountStatus: 'analyzing', singlePage: true, pageRange: '5', rangeLines: [{ value: '5', error: '' }] }),
    'word-single': mk({ name: 'a.docx', pageCount: 1, pageCountStatus: 'confirmed', duplex: 'off' }),
    excel: mk({ name: 'a.xlsx', excelWarning: true, pageCount: 0, pageCountStatus: '' }),
  };
  const out = {};
  const frame = document.querySelector('.app-frame') || document.body;
  const frameW = frame.getBoundingClientRect().width || 430;
  for (const [key, f] of Object.entries(cases)) {
    printState.selectedFiles = [f];
    renderFileList();
    const card = document.querySelector('#fileList .file-card');
    const h = card ? card.getBoundingClientRect().height : 0;
    out[key] = { px: h, rpx: h / frameW * 750 };
  }
  return { out, frameW };
});

console.log(`\n帧宽 ${measured.frameW.toFixed(1)}px（rpx = px / 帧宽 × 750）\n`);
console.log('类型'.padEnd(20) + '实测rpx'.padStart(10) + '常量rpx'.padStart(10) + '偏差'.padStart(10) + '偏差%'.padStart(9));
let worst = 0;
for (const [k, v] of Object.entries(measured.out)) {
  const c = MP_CONST[k] || 0;
  const diff = v.rpx - c;
  const pct = c ? (diff / c) * 100 : 0;
  if (Math.abs(pct) > Math.abs(worst)) worst = pct;
  console.log(k.padEnd(20) + v.rpx.toFixed(1).padStart(10) + c.toFixed(1).padStart(10)
    + diff.toFixed(1).padStart(10) + (pct.toFixed(1) + '%').padStart(9));
}
console.log(`\n最大偏差 ${worst.toFixed(1)}%；卡片间距常量 ${FILE_CARD_GAP_RPX}rpx（实测 margin-bottom 见 .file-card）`);
await browser.close();
server.close();
