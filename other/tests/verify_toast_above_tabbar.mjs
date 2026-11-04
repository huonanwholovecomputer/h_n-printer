/* verify_toast_above_tabbar.mjs — APP 通知气泡（#toast）避让悬浮 tabBar 的渲染级验收
 *
 * 背景（2026-10 用户截图反馈）：首页底部浮出的「设备离线，无法远程切换」气泡下沿被
 * 悬浮 tabBar 盖住一截（截图实测重叠 ≈8px）。
 *
 * 根因：.tab-bar 的底距把 Android 导航栏安全区算进去了
 *         bottom: calc(1.44cqw + var(--safe-bottom, …))；height: 13.2cqw
 *       ⇒ tabBar 顶缘 = 14.64cqw + 安全区；
 *       而 .toast 的底距只有 `bottom: 17.1cqw`（不含安全区）⇒ 安全区大的机型上
 *       tabBar 整体被抬高、气泡不动，2.46cqw 的间隙（≈9px）被吃掉。
 * 用户那台机的安全区 ≈19px（1080×2400 @3x 截图按 360×800 CSS 反推）。
 *
 * 本脚本用真实 index.html + styles.css：
 *   ① 静态：.toast 的底距必须含安全区、且恒大于 tabBar 顶缘（模板也不许把 bug 带回来）
 *   ② 渲染：注入 0 / 19 / 34 / 48px 安全区，量气泡与 tabBar 的真实几何 —— 必须不重叠
 *   ③ 变异自证：把底距改回 17.1cqw（旧写法）必须**转红**，证明判据不是恒真
 *   ④ 顺带出「修复前 / 修复后」对比截图（写入 截图展示/，.gitignore 已忽略）
 *
 * 用法: node tests/verify_toast_above_tabbar.mjs      # exit 0 = 全绿
 */
import { createRequire } from 'node:module';
import http from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { readFileSync } from 'node:fs';
import { extname, join, normalize } from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const REPO = 'D:/打印机项目';
const APP_DIR = `${REPO}/mobile_apps/android_app/www`;
const GEN = `${REPO}/mobile_apps/android_app/tools/wxss2web.mjs`;
const SHOTS = `${REPO}/截图展示/toast避让tabbar`;
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

/* ── 解析：从 CSS 里抠出某个注释锚点后第一条规则的 body ───────────────
   （.toast / .tab-bar 在文件里都有多条同名规则：Web 适配补丁里那条只有
     `position: absolute`，按选择器找必然抓错，所以按注释锚点定位） */
function cssBlock(css, anchor) {
  const i = css.indexOf(anchor);
  if (i < 0) return '';
  const open = css.indexOf('{', i + anchor.length);
  const close = css.indexOf('}', open);
  return open < 0 || close < 0 ? '' : css.slice(open + 1, close);
}
function decl(body, prop) {
  const m = body.match(new RegExp(`(?:^|[;{\\s])${prop}\\s*:([^;]*)`));
  return m ? m[1].trim() : '';
}
const px1 = (n) => Math.round(n * 100) / 100;
const cqwOf = (v) => { const m = String(v).match(/([\d.]+)cqw/); return m ? parseFloat(m[1]) : NaN; };

/* ── ① 静态：CSS 与生成模板 ──────────────────────────────────────────── */
console.log('=== ① 静态：.toast 底距必须含安全区，且恒在 tabBar 之上 ===');
const css = readFileSync(`${APP_DIR}/styles.css`, 'utf8');
const toastBody = cssBlock(css, '/* Toast（小程序无此组件） */');
const toastBottom = decl(toastBody, 'bottom');
/* tabBar 的几何分散在两处：高度在 custom-tab-bar 段，底距由「底部安全区」补丁覆写（含安全区） */
const tabBarHeight = decl(cssBlock(css, '/* ── Tab Bar ── */'), 'height');
const tabBarBottom = decl(cssBlock(css, '/* 底部安全区：'), 'bottom');
const tabTopConst = cqwOf(tabBarBottom) + cqwOf(tabBarHeight);

check('取到了 .toast 的 bottom 声明', /cqw/.test(toastBottom), `bottom: ${toastBottom}`);
check('.toast 底距把安全区算进来（var(--safe-bottom)）',
      toastBottom.includes('calc(') && toastBottom.includes('var(--safe-bottom'));
check('底距里的 cqw 常量 > tabBar 顶缘常量（气泡不会被 tabBar 顶住）',
      cqwOf(toastBottom) > tabTopConst,
      `toast=${cqwOf(toastBottom)}cqw > tabTop=${px1(tabTopConst)}cqw`);
check('tabBar 顶缘常量取自真实声明（底距 + 高）',
      /1\.44cqw \+ var\(--safe-bottom/.test(tabBarBottom) && !!tabBarHeight,
      `bottom: ${tabBarBottom} / height: ${tabBarHeight}`);
check('tabBar 高度仍是 13.2cqw', tabBarHeight === '13.2cqw', `height: ${tabBarHeight}`);

const gen = readFileSync(GEN, 'utf8');
const genToast = cssBlock(gen, '/* Toast（小程序无此组件） */');
check('生成模板 wxss2web.mjs 的 toast 底距同口径（重新生成不会把 bug 带回来）',
      decl(genToast, 'bottom').includes('var(--safe-bottom'),
      `模板 bottom: ${decl(genToast, 'bottom')}`);

/* ── 渲染环境 ────────────────────────────────────────────────────────── */
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
/* 与用户那台机同口径：1080×2400 @3x ⇒ 360×800 CSS px（.app-frame 宽 360 ⇒ 1cqw = 3.6px） */
const page = await browser.newPage({ viewport: { width: 360, height: 800 }, deviceScaleFactor: 3 });
page.on('pageerror', (e) => console.log('  [pageerror] ' + e.message));
page.on('console', (m) => { if (m.type() === 'error') console.log('  [console.error] ' + m.text()); });

/* 隔离到本地桩：app 的 DEFAULT_BASE_URL 是线上域名，不覆盖会打到生产接口 */
const LOCAL_BASE = `http://127.0.0.1:${port}`;
const foreign = [];
page.on('request', (r) => {
  const u = r.url();
  if (!u.startsWith(LOCAL_BASE) && !u.startsWith('data:') && !u.startsWith('blob:')) foreign.push(u);
});
await page.addInitScript((base) => {
  try {
    window.__HN_TEST_BASE_URL__ = base;
    localStorage.setItem('hn_theme_mode', 'dark');   // 与用户截图同为深色主题
  } catch (e) { /* 忽略 */ }
}, LOCAL_BASE);
await page.route('**/api/**', (route) => {
  const url = route.request().url();
  if (url.startsWith(LOCAL_BASE)) return route.continue();
  return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(MOCK) });
});
await page.goto(`${LOCAL_BASE}/index.html`, { waitUntil: 'load' });
await page.waitForFunction(() => typeof showToast === 'function' && !!document.getElementById('toast'),
                          null, { timeout: 15000 });

const CQW = 3.6;   // CSS px / cqw（.app-frame 宽 360px）

/* 量一次几何：注入安全区 → 弹出气泡 → 取 #toast 与 #tabBar 的真实矩形 */
async function measure(safeBottomPx, extraCss) {
  await page.evaluate(({ sb, extra }) => {
    document.documentElement.style.setProperty('--safe-bottom', sb + 'px');
    let tag = document.getElementById('__probe_override');
    if (tag) tag.remove();
    if (extra) {
      tag = document.createElement('style');
      tag.id = '__probe_override';
      tag.textContent = extra;
      document.head.appendChild(tag);
    }
    document.getElementById('toast').classList.remove('show');
    showToast('设备离线，无法远程切换', 600000);
  }, { sb: safeBottomPx, extra: extraCss || '' });
  await page.waitForTimeout(400);   // 等 0.25s 的位移过渡走完（隐藏态 translateY(16px) 会影响测量）
  const geo = await page.evaluate(() => {
    const t = document.getElementById('toast').getBoundingClientRect();
    const b = document.getElementById('tabBar').getBoundingClientRect();
    const cs = getComputedStyle(document.getElementById('toast'));
    const vh = window.innerHeight;
    return {
      toastBottom: t.bottom, toastTop: t.top, toastLeft: t.left, toastRight: t.right,
      tabTop: b.top, tabBottom: b.bottom, vh,
      opacity: cs.opacity,
      gap: b.top - t.bottom,                       // >0 = 气泡整体在 tabBar 之上
      overlap: t.bottom - b.top,                    // >0 = 重叠（被盖住的像素）
    };
  });
  return geo;
}

/* ── ② 渲染：多种安全区下都必须不重叠 ────────────────────────────────── */
console.log('\n=== ② 渲染（真实 360×800 @3x）：气泡下沿必须始终在 tabBar 顶缘之上 ===');
const INSETS = [0, 19, 34, 48];
const results = {};
for (const sb of INSETS) {
  const g = results[sb] = await measure(sb, '');
  const wantGap = (cqwOf(toastBottom) - tabTopConst) * CQW;   // 2.46cqw ≈ 8.86px
  check(`安全区 ${sb}px：气泡整体在 tabBar 之上（gap=${px1(g.gap)}px）`, g.gap >= 1,
        `toast.bottom=${px1(g.toastBottom)} tabBar.top=${px1(g.tabTop)} overlap=${px1(g.overlap)}`);
  check(`安全区 ${sb}px：间隙与设计值一致（${px1(wantGap)}px ±1.5）`,
        Math.abs(g.gap - wantGap) <= 1.5, `gap=${px1(g.gap)}`);
  check(`安全区 ${sb}px：气泡已显示且水平居中`,
        parseFloat(g.opacity) > 0.9
        && Math.abs((g.toastLeft + g.toastRight) / 2 - 180) <= 1,
        `opacity=${g.opacity} center=${px1((g.toastLeft + g.toastRight) / 2)}`);
  check(`安全区 ${sb}px：气泡没跑到屏幕外（底部仍在视口内）`, g.toastBottom < g.vh);
}

/* ── ③ 变异自证：旧写法（底距 17.1cqw，不含安全区）必须被抓住 ─────────── */
console.log('\n=== ③ 变异自证：把底距改回旧值 17.1cqw（这就是现场那个 bug）===');
const buggy = await measure(19, '#toast { bottom: 17.1cqw !important; }');
const oldOverlap = px1(buggy.overlap);
check('★ 旧写法在安全区 19px 下**重叠**（与现场截图一致：气泡下沿被 tabBar 盖住）',
      buggy.overlap > 4, `overlap=${oldOverlap}px（现场截图实测 ≈8px）`);
check('★ 新写法的间隙确实由安全区项贡献（差值 ≈ 19px）',
      Math.abs((results[19].gap - buggy.gap) - 19) <= 1.5,
      `new.gap=${px1(results[19].gap)} old.gap=${px1(buggy.gap)} 差=${px1(results[19].gap - buggy.gap)}`);
check('★ 无安全区时新旧完全一致（0px inset 零回归）',
      Math.abs((await measure(0, '#toast { bottom: 17.1cqw !important; }')).gap - results[0].gap) <= 0.5);

/* ── ④ 对比截图 ──────────────────────────────────────────────────────── */
console.log('\n=== ④ 对比截图 ===');
async function shot(sb, extra, file) {
  await measure(sb, extra);
  await page.screenshot({ path: `${SHOTS}/${file}`, clip: { x: 0, y: 560, width: 360, height: 240 } });
  console.log(`  · ${file}`);
}
await shot(19, '#toast { bottom: 17.1cqw !important; }', '修复前_安全区19px_气泡被tabBar盖住.png');
await shot(19, '', '修复后_安全区19px_气泡在tabBar之上.png');
await shot(48, '', '修复后_安全区48px_气泡在tabBar之上.png');

check('全程没有打到生产接口（只访问本地桩）', foreign.length === 0, foreign.slice(0, 3).join(' | '));

await browser.close();
server.close();

console.log('\n' + (failures ? `有 ${failures} 项失败 ❌` : '全绿 ✅'));
process.exit(failures ? 1 : 0);
