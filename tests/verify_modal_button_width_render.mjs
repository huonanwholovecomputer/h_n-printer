/* verify_modal_button_width_render.mjs — 移动端弹层按钮宽度「渲染级」验收
 *
 * 病根（机制）：.modal-sheet 是 align-items:center 的纵向 flex 容器，
 *   每个子项在交叉轴上都收缩为**内容宽**。直接挂在 sheet 上的按钮有
 *   .btn-primary{width:100%}（百分比对 sheet 的确定宽度求值 ⇒ 撑满）；
 *   但中间多一层 width:auto 的 view 时，那一层自己先被收缩成内容宽，
 *   里面的 width:100% 就只是「100% 的文案宽」⇒ 长文案撑开、短文案塌成窄条。
 *
 * 本脚本验两件事（真实浏览器几何，不是猜）：
 *   A) APP（真实 index.html + styles.css + 本地桩）：隐私弹层操作行 / 按钮
 *      宽度 == 弹层内容宽（±2px），7 字文案与 2 字文案宽度完全相同。
 *   B) 小程序（无法无头渲染 wxss）：把 h_n_print 的真实 wxss 规则按 1rpx = 0.5px
 *      （375px 基准）等价复刻成同构 HTML，量同样的两组数字。
 *   另验「机制判别」不变量：直接挂在 .modal-sheet 上的按钮本来就铺满
 *   （⇒ 病在中间层，不在按钮），两种模式下都必须成立。
 *
 * 用法:
 *   node tests/verify_modal_button_width_render.mjs            # 修后：必须全绿
 *   node tests/verify_modal_button_width_render.mjs --revert   # 修前（git HEAD 的 CSS）：病态断言必须转红
 * 截图写入 截图展示/按钮宽度修复/。
 */
import { createRequire } from 'node:module';
import http from 'node:http';
import { execFileSync } from 'node:child_process';
import { readFile, mkdir } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const REPO = 'D:/打印机项目';
const APP_DIR = REPO + '/mobile_apps/android_app/www';
const MP_DIR = REPO + '/mobile_apps/h_n_print';
const SHOTS = REPO + '/截图展示/按钮宽度修复';
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';

const MODE = process.argv.includes('--revert') ? 'revert' : 'fix';
const TAG = MODE === 'fix' ? '修后' : '修前';

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
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (detail ? '  -- ' + detail : ''));
}
/* 病态断言：fix 模式要求成立；revert 模式（修前 CSS）要求**不**成立（必须转红） */
function expect(name, cond, detail) {
  const want = MODE === 'fix';
  const pass = want ? cond : !cond;
  if (!pass) failures++;
  const label = want ? name : '(revert: 期望转红) ' + name;
  console.log('[' + (pass ? 'PASS' : 'FAIL') + '] ' + label + (detail ? '  -- ' + detail : ''));
}

/* 只在 fix 模式成立的断言（revert 模式只报数，不判定） */
function checkFixOnly(name, cond, detail) {
  if (MODE !== 'fix') {
    console.log('[INFO] ' + name + (detail ? '  -- ' + detail : ''));
    return;
  }
  if (!cond) failures++;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (detail ? '  -- ' + detail : ''));
}

const gitShow = (spec) => execFileSync('git', ['-C', REPO, 'show', spec], { maxBuffer: 64 * 1024 * 1024 }).toString('utf8');

/* 修前状态 = git HEAD。--revert 时服务器发 HEAD 的 index.html + styles.css，
   小程序同构页用 HEAD 的 app.wxss + index.wxss —— 真正的「把修复回退后跑一次」。 */
const HEAD = {};
if (MODE === 'revert') {
  HEAD['index.html'] = gitShow('HEAD:mobile_apps/android_app/www/index.html');
  HEAD['styles.css'] = gitShow('HEAD:mobile_apps/android_app/www/styles.css');
}

const MOCK = {
  success: true, token: 'tok', openid: 'dev_test', nickname: '测试设备', role: 'admin',
  devices: [], orders: [], files: [], users: [], admins: [], keys: [], total: 0, count: 0,
  online: true, active: true, take_orders_online: true, active_client_id: 'pc-1',
  claiming_devices: [], delivery_locations: ['1号楼北楼'], urgency_levels: ['低'],
  retention_options: [], server_retention_hours: 168,
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
          delivery_locations: ['1号楼北楼'], delivery_percentages: {'1号楼北楼': 0},
          urgency_levels: ['低'], urgency_prices: {'低': 0},
        },
      }));
      return;
    }
    res.end(JSON.stringify(MOCK));
    return;
  }
  /* 注意：path.normalize 在 Windows 上会把 '/' 换成 '\\'，那会让 HEAD 里的静态资源查不到、
     结果悄悄回落成工作区文件（--revert 就名存实亡了）。这里只用字符串方式去前导斜杠。 */
  const rel = (url === '/' ? 'index.html' : url).replace(/^\/+/, '');
  if (HEAD[rel]) {
    res.writeHead(200, { 'Content-Type': MIME[extname(rel)] || 'application/octet-stream' });
    res.end(HEAD[rel]);
    return;
  }
  try {
    const body = await readFile(join(APP_DIR, rel));
    res.writeHead(200, { 'Content-Type': MIME[extname(rel)] || 'application/octet-stream' });
    res.end(body);
  } catch (e) {
    res.writeHead(404); res.end('not found');
  }
});

await new Promise((r) => server.listen(0, '127.0.0.1', r));
const PORT = server.address().port;
const LOCAL_BASE = 'http://127.0.0.1:' + PORT;
await mkdir(SHOTS, { recursive: true });

console.log('='.repeat(60));
console.log('模式: ' + MODE + '（' + (MODE === 'fix' ? '当前工作区 CSS' : 'git HEAD 的修前 CSS') + '）');
console.log('='.repeat(60));

const browser = await chromium.launch({ executablePath: CHROME });

/* ======================================================================
   一、APP 真渲染（430 × 932，与真实设备同宽）
   ====================================================================== */
const page = await browser.newPage({ viewport: { width: 430, height: 932 }, deviceScaleFactor: 2 });
page.on('pageerror', (e) => console.log('  [pageerror] ' + e.message));
page.on('console', (m) => { if (m.type() === 'error') console.log('  [console.error] ' + m.text()); });

const foreign = [];
page.on('request', (r) => {
  const u = r.url();
  if (!u.startsWith(LOCAL_BASE) && !u.startsWith('data:') && !u.startsWith('blob:')) foreign.push(u);
});
/* app 的 DEFAULT_BASE_URL 是线上域名（S2-09 起编译期常量）⇒ 必须隔离到本地桩 */
await page.addInitScript((base) => {
  try { window.__HN_TEST_BASE_URL__ = base; } catch (e) { /* 忽略 */ }
}, LOCAL_BASE);
await page.route('**/api/**', (route) => {
  const url = route.request().url();
  if (url.startsWith(LOCAL_BASE)) return route.continue();
  return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(MOCK) });
});
await page.goto(LOCAL_BASE + '/index.html', { waitUntil: 'load' });
await page.waitForFunction(
  () => !!document.getElementById('privacyAgreeBtn') && typeof printState !== 'undefined' && typeof openPrivacySheet === 'function',
  null, { timeout: 15000 },
);

/* APP 弹层几何：sheet 内容宽（去掉左右 padding）/ 操作行 / 按钮 / 保留期行 */
const measureApp = () => page.evaluate(() => {
  const q = (s) => document.querySelector('#privacySheet ' + s);
  const sheet = q('.modal-sheet');
  const cs = getComputedStyle(sheet);
  const sr = sheet.getBoundingClientRect();
  const contentW = sr.width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  const box = (el) => {
    const r = el.getBoundingClientRect();
    return { w: +r.width.toFixed(2), left: +(r.left - sr.left).toFixed(2), h: +r.height.toFixed(2) };
  };
  const actions = q('.privacy-sheet-actions');
  const btn = document.getElementById('privacyAgreeBtn');
  const row = q('.privacy-retention-row');
  return {
    sheetW: +sr.width.toFixed(2),
    contentW: +contentW.toFixed(2),
    actions: box(actions),
    btn: box(btn),
    row: box(row),
    text: btn.textContent.trim(),
    sheetVisible: getComputedStyle(document.getElementById('privacySheet')).display !== 'none',
  };
});

/* 两态：未同意（7 字「我已阅读并同意」）/ 已同意（2 字「完成」），走真实 renderPrivacySheetState */
await page.evaluate(() => {
  try { localStorage.removeItem(PRIVACY_AGREE_KEY); } catch (e) {}
  printState.privacyAgreed = false;
  openPrivacySheet();
});
await page.waitForTimeout(700);            /* 等 sheetSpringIn(0.4s) + fadeIn(0.2s) 收尾，transform 会影响 rect */
const app7 = await measureApp();

await page.evaluate(() => {
  printState.privacyAgreed = true;
  renderPrivacySheetState();
});
await page.waitForTimeout(150);
const app2 = await measureApp();

console.log('\n--- APP 实测（430px 宽，cqw 缩放）---');
console.log('  弹层宽 ' + app7.sheetW + 'px / 内容宽 ' + app7.contentW + 'px');
console.log('  「' + app7.text + '」操作行 ' + app7.actions.w + 'px，按钮 ' + app7.btn.w + 'x' + app7.btn.h + 'px');
console.log('  「' + app2.text + '」操作行 ' + app2.actions.w + 'px，按钮 ' + app2.btn.w + 'x' + app2.btn.h + 'px');

check('APP：隐私弹层确实打开了（量到的是真渲染尺寸）', app7.sheetVisible && app2.sheetVisible);
expect('APP(1) 操作行宽度 == 弹层内容宽（±2px）',
  Math.abs(app7.actions.w - app7.contentW) <= 2,
  'actions=' + app7.actions.w + ' content=' + app7.contentW);
expect('APP(2) 7 字按钮宽度 == 弹层内容宽（±2px）',
  Math.abs(app7.btn.w - app7.contentW) <= 2,
  'btn=' + app7.btn.w + ' content=' + app7.contentW);
expect('APP(3) 2 字「完成」按钮宽度 == 弹层内容宽（±2px）',
  Math.abs(app2.btn.w - app2.contentW) <= 2,
  'btn=' + app2.btn.w + ' content=' + app2.contentW);
expect('APP(4) 7 字与 2 字两态按钮宽度完全相同（±0.5px）',
  Math.abs(app7.btn.w - app2.btn.w) <= 0.5,
  app7.btn.w + ' vs ' + app2.btn.w);
expect('APP(5) 两态操作行宽度完全相同（±0.5px）',
  Math.abs(app7.actions.w - app2.actions.w) <= 0.5,
  app7.actions.w + ' vs ' + app2.actions.w);
/* 保留期行不是判别性断言：修前它常被 chips 的宽内容钳到内容宽（= 看起来已经对），
   所以只在 fix 模式要求成立，revert 模式只报数。 */
checkFixOnly('APP(6) 保留期行（label + chips，space-between）铺满内容宽（±2px）',
  Math.abs(app7.row.w - app7.contentW) <= 2,
  'row=' + app7.row.w + ' content=' + app7.contentW);

/* 截图：A2/A6 —— 修前（--revert）/ 修后各一组 */
await page.screenshot({ path: join(SHOTS, TAG + '_APP_隐私弹层_完成.png'), fullPage: false });
await page.locator('#privacySheet .modal-sheet').screenshot({ path: join(SHOTS, TAG + '_APP_弹层_完成.png') });
await page.evaluate(() => { printState.privacyAgreed = false; renderPrivacySheetState(); });
await page.waitForTimeout(150);
await page.locator('#privacySheet .modal-sheet').screenshot({ path: join(SHOTS, TAG + '_APP_弹层_我已阅读并同意.png') });
console.log('  截图: ' + TAG + '_APP_弹层_完成.png / ' + TAG + '_APP_弹层_我已阅读并同意.png');

/* ======================================================================
   二、小程序同构几何（wxss 无法无头渲染 ⇒ 把真实规则按 1rpx = 0.5px 复刻）
   ====================================================================== */
const CSS_APP = MODE === 'revert' ? gitShow('HEAD:mobile_apps/h_n_print/app.wxss') : await readFile(MP_DIR + '/app.wxss', 'utf8');
const CSS_IDX = MODE === 'revert' ? gitShow('HEAD:mobile_apps/h_n_print/pages/index/index.wxss') : await readFile(MP_DIR + '/pages/index/index.wxss', 'utf8');

const decl = (css, selector) => {
  const esc = selector.replace(/[.*+?^$()|[\]\\]/g, '\\$&');
  const m = css.match(new RegExp('(?:^|\n)\\s*' + esc + '\\s*\\{([^}]*)\\}'));
  return m ? m[1].trim() : null;
};
const rpx2px = (d) => d.replace(/(-?\d*\.?\d+)rpx/g, (_, n) => (parseFloat(n) * 0.5) + 'px');

const SHEET_SELS = [['app.wxss', CSS_APP, '.modal-mask'], ['app.wxss', CSS_APP, '.modal-sheet'],
  ['app.wxss', CSS_APP, '.btn-primary'], ['app.wxss', CSS_APP, '.title-medium']];
const PRIV_SELS = ['.privacy-sheet', '.privacy-sheet-actions', '.privacy-agree-btn', '.privacy-sheet-footnote',
  '.privacy-retention-row', '.privacy-retention-label', '.privacy-chips', '.privacy-chip',
  '.privacy-retention-hint', '.privacy-retention-hint-line', '.privacy-sheet-link-row', '.privacy-link'];

const missing = [];
const cssParts = [];
for (const [f, css, sel] of SHEET_SELS) {
  const d = decl(css, sel);
  if (!d) missing.push(f + ' ' + sel); else cssParts.push(sel + ' {' + rpx2px(d) + '}');
}
for (const sel of PRIV_SELS) {
  const d = decl(CSS_IDX, sel);
  if (!d) missing.push('index.wxss ' + sel); else cssParts.push(sel + ' {' + rpx2px(d) + '}');
}
console.log('\n--- 小程序同构页 ---');
console.log('  从真实 wxss 抽到的规则: ' + cssParts.length + ' 条，缺失: ' + (missing.length ? missing.join(', ') : '无'));
check('小程序：真实 wxss 里所需的规则全部抽到了（同构页没有凭空造样式）', missing.length === 0, missing.join(', '));

const VARS = ':root{--bg-card:#fff;--text-primary:#000;--text-secondary:#666;--text-tertiary:#999;'
  + '--radius-xl:14px;--radius-md:10px;--radius-sm:8px;--shadow-lg:none;--blue:#007AFF;--green:#34C759;'
  + '--gray6:#f2f2f7;--separator-strong:#ddd;--glass-light:#f7f7f9;--glass-rim:none;--glass-shadow-sm:none;--glass-border-dark:#e5e5ea}';

/* index.wxml:398-423 的结构（view->div，text->span，微信 button 是块级；文案两态由脚本替换） */
const isoHtml = '<!doctype html><html><head><meta charset="utf-8"><style>'
  + VARS + 'body{margin:0;font-family:-apple-system,"PingFang SC",sans-serif}'
  + cssParts.join('\n') + '</style></head><body>'
  + '<div class="modal-mask" id="isoMask"><div class="modal-sheet privacy-sheet" id="isoSheet">'
  + '<span class="title-medium">文件保留与隐私</span>'
  + '<div class="privacy-retention-row"><span class="privacy-retention-label">文件保留</span>'
  + '<div class="privacy-chips"><span class="privacy-chip privacy-chip-active">遵从服务器管理</span>'
  + '<span class="privacy-chip">立即删除</span></div></div>'
  + '<div class="privacy-retention-hint"><span class="privacy-retention-hint-line">立即删除：打印完成后立即从服务器删除。</span>'
  + '<span class="privacy-retention-hint-line">遵从服务器管理：遵从服务器当前设置。</span></div>'
  + '<div class="privacy-sheet-link-row"><span class="privacy-link">查看《文件使用与隐私说明》</span></div>'
  + '<div class="privacy-sheet-actions"><div class="btn-primary privacy-agree-btn" id="isoBtn">我已阅读并同意</div></div>'
  + '<span class="privacy-sheet-footnote">同意后不再重复询问，也不会每单重新勾选。</span>'
  + '</div></div>'
  /* 机制判别：同一个 sheet 里「直接挂按钮」vs「中间多一层」——病在中间层，不在按钮 */
  + '<div class="modal-mask" id="isoMask2"><div class="modal-sheet" id="isoSheet2">'
  + '<span class="title-medium">直接挂按钮</span>'
  + '<div class="btn-primary" id="isoDirectBtn">我已阅读并同意</div>'
  + '</div></div>'
  + '</body></html>';

const iso = await browser.newPage({ viewport: { width: 375, height: 812 } });
await iso.setContent(isoHtml, { waitUntil: 'load' });
await iso.waitForTimeout(700);   /* sheetSpringIn 的 transform 会污染 rect */

const measureIso = (sheetId, btnId) => iso.evaluate(([sid, bid]) => {
  const sheet = document.getElementById(sid);
  const cs = getComputedStyle(sheet);
  const sr = sheet.getBoundingClientRect();
  const contentW = sr.width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  const btn = document.getElementById(bid);
  const br = btn.getBoundingClientRect();
  const acts = sheet.querySelector('.privacy-sheet-actions');
  const row = sheet.querySelector('.privacy-retention-row');
  const w = (el) => el ? +el.getBoundingClientRect().width.toFixed(2) : null;
  return {
    sheetW: +sr.width.toFixed(2), contentW: +contentW.toFixed(2),
    actionsW: w(acts), rowW: w(row), btnW: +br.width.toFixed(2), btnH: +br.height.toFixed(2),
    text: btn.textContent.trim(),
  };
}, [sheetId, btnId]);

const iso7 = await measureIso('isoSheet', 'isoBtn');
await iso.evaluate(() => { document.getElementById('isoBtn').textContent = '完成'; });
await iso.waitForTimeout(60);
const iso2 = await measureIso('isoSheet', 'isoBtn');
const isoDirect = await measureIso('isoSheet2', 'isoDirectBtn');

console.log('  同构弹层宽 ' + iso7.sheetW + 'px / 内容宽 ' + iso7.contentW + 'px（375px 基准）');
console.log('  「' + iso7.text + '」操作行 ' + iso7.actionsW + 'px，按钮 ' + iso7.btnW + 'x' + iso7.btnH + 'px');
console.log('  「' + iso2.text + '」操作行 ' + iso2.actionsW + 'px，按钮 ' + iso2.btnW + 'x' + iso2.btnH + 'px');
console.log('  直接挂 sheet 的按钮（判别用）: ' + isoDirect.btnW + 'px / 内容宽 ' + isoDirect.contentW + 'px');

check('小程序同构：两态都量到了（按钮高度 > 0，不是没渲染）', iso7.btnH > 10 && iso2.btnH > 10,
  iso7.btnH + ' / ' + iso2.btnH);
expect('小程序(1) .privacy-sheet-actions 宽度 == 弹层内容宽（±2px）',
  Math.abs(iso7.actionsW - iso7.contentW) <= 2, 'actions=' + iso7.actionsW + ' content=' + iso7.contentW);
expect('小程序(2) 7 字按钮宽度 == 弹层内容宽（±2px）',
  Math.abs(iso7.btnW - iso7.contentW) <= 2, 'btn=' + iso7.btnW + ' content=' + iso7.contentW);
expect('小程序(3) 2 字「完成」按钮宽度 == 弹层内容宽（±2px）',
  Math.abs(iso2.btnW - iso2.contentW) <= 2, 'btn=' + iso2.btnW + ' content=' + iso2.contentW);
expect('小程序(4) 7 字与 2 字两态按钮宽度完全相同（±0.5px）',
  Math.abs(iso7.btnW - iso2.btnW) <= 0.5, iso7.btnW + ' vs ' + iso2.btnW);
checkFixOnly('小程序(5) 保留期行铺满内容宽（±2px）',
  Math.abs(iso7.rowW - iso7.contentW) <= 2, 'row=' + iso7.rowW + ' content=' + iso7.contentW);
/* 不变量：两种模式下都必须成立 —— 直接挂 sheet 的按钮本来就铺满，所以修复点是「中间层」 */
check('机制判别：直接挂在 .modal-sheet 上的按钮本就铺满内容宽（±2px，两模式都成立）',
  Math.abs(isoDirect.btnW - isoDirect.contentW) <= 2,
  'btn=' + isoDirect.btnW + ' content=' + isoDirect.contentW);

await iso.locator('#isoSheet').screenshot({ path: join(SHOTS, TAG + '_小程序同构_弹层_完成.png') });
await iso.evaluate(() => { document.getElementById('isoBtn').textContent = '我已阅读并同意'; });
await iso.waitForTimeout(60);
await iso.locator('#isoSheet').screenshot({ path: join(SHOTS, TAG + '_小程序同构_弹层_我已阅读并同意.png') });
console.log('  截图: ' + TAG + '_小程序同构_弹层_完成.png（同目录）');

/* ======================================================================
   三、收尾
   ====================================================================== */
check('全程未触碰生产/外部网络（BASE_URL 已隔离到本地桩）', foreign.length === 0, foreign.slice(0, 3).join(', '));

await browser.close();
server.close();
console.log('\n' + '='.repeat(60));
if (MODE === 'fix') {
  console.log('弹层按钮宽度渲染验收（修后）:', failures === 0 ? '全部通过' : '存在 ' + failures + ' 项失败');
} else {
  console.log('弹层按钮宽度渲染验收（修前 · 期望病态断言转红）:', failures === 0 ? '病态已复现 == 断言有效' : '存在 ' + failures + ' 项 != 预期');
}
console.log('截图目录: ' + SHOTS);
process.exit(failures ? 1 : 0);
