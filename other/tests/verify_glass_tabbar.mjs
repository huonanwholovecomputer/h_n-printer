/* verify_glass_tabbar.mjs — APP 底部「玻璃滑块」Tab 栏（2026-11-16 落地）
 *
 * 为什么要渲染级验收：滑块是「位移层 + 形变层」两个 DOM 叠出来的，静态 grep 只能
 * 证明代码里写了 transform，证不了：
 *   ① 药丸真的落在当前选中格子的正中（而不是栏的两个端头）；
 *   ② 切换时只有位移 + 轻微拉伸，**落位宽度不变**（一旦有人把位移改回 left/width 动画，
 *      或把两层合并成一个元素，就会量到宽度变化 / 高度抖动）；
 *   ③ 药丸不探出玻璃栏的圆角轮廓；
 *   ④ 图标 bump 与整排 wave 真的被触发（而不是只加了 class 没有 keyframes）；
 *   ⑤ 拖动真的跟手（1:1）、越界真的被橡皮筋压住、过半格才吸附、按下去真的先压缩再
 *      船式回弹 —— 这几条只有真手势才量得出来，静态 grep 最多证明"写了 pointermove"；
 *   ⑥ 页面转场（480ms 平铺推拉）途中照样能起手拖、松手按药丸停止的位置决定切哪页。
 *
 * 探针自证：把 transform 钉死（模拟「滑块不动」的坏实现），点击「我」之后药丸
 * 若仍然落在第 1 格 —— 说明这个探针量的是真实位置，红/绿都有意义。
 *
 * 需要 Playwright + 本机 Chrome（run_all 里登记为「需真实浏览器」）。
 */
import { createRequire } from 'node:module';
import http from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { readFileSync } from 'node:fs';
import { extname, join, normalize } from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const REPO = 'D:/打印机项目';
const APP_DIR = REPO + '/mobile_apps/android_app/www';
const SHOTS = REPO + '/截图展示/玻璃滑块Tab栏';
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
               '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.svg': 'image/svg+xml',
               '.json': 'application/json' };

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (detail ? '  -- ' + detail : ''));
}

/* ── ① 静态：结构必须是「两层两元素」，动画必须只动 transform ───────────── */
function cssRule(src, selector) {
  const i = src.indexOf('\n' + selector + ' {');
  if (i < 0) return '';
  const open = src.indexOf('{', i);
  const close = src.indexOf('}', open);
  return close < 0 ? '' : src.slice(open + 1, close);
}
function cssBlock(src, anchor) {
  const i = src.indexOf(anchor);
  if (i < 0) return '';
  const open = src.indexOf('{', i + anchor.length);
  const close = src.indexOf('}', open);
  return open < 0 || close < 0 ? '' : src.slice(open + 1, close);
}
const decl = (body, prop) => {
  const m = body.match(new RegExp('(?:^|[;{\\s])' + prop + '\\s*:([^;]*)'));
  return m ? m[1].trim() : '';
};
const px1 = (n) => Math.round(n * 100) / 100;

console.log('=== ① 静态：位移层与形变层必须是两个元素，动画只动 transform ===');
const html = readFileSync(APP_DIR + '/index.html', 'utf8');
const css = readFileSync(APP_DIR + '/styles.css', 'utf8');
const js = readFileSync(APP_DIR + '/app.js', 'utf8');

const thumbOpen = html.search(/<view class="tab-thumb" id="tabThumb"[^>]*>/);
const pillOpen = html.search(/<view class="tab-pill" id="tabPill"/);
const thumbClose = html.indexOf('</view>', pillOpen);
check('index.html 有位移层 #tabThumb', thumbOpen > 0);
check('#tabPill 嵌在 #tabThumb 里面（两层两个元素）',
      thumbOpen > 0 && pillOpen > thumbOpen && thumbClose > pillOpen,
      'thumb@' + thumbOpen + ' pill@' + pillOpen);
check('滑块节点在 .tab-bar 内、tab-item 之前（z 序与命中面都靠它）',
      thumbOpen > html.indexOf('id="tabBar"') && thumbOpen < html.indexOf('class="tab-item"'));

const thumbBody = cssRule(css, '.tab-thumb');
const pillBody = cssRule(css, '.tab-pill');
check('取到了 .tab-thumb / .tab-pill 规则', !!thumbBody && !!pillBody);
check('位移层只做 transform 过渡（260ms cubic-bezier(.2,0,0,1)，无回弹）',
      /transform 260ms cubic-bezier\(0?\.2, 0, 0, 1\)/.test(thumbBody),
      'transition: ' + decl(thumbBody, 'transition'));
check('位移层不参与缩放/动画（scale 与 animation 都必须在形变层）',
      !/scale/.test(thumbBody) && !/animation/.test(thumbBody));
check('位移层用 --tab-i 驱动（不靠 left/width 动画）',
      /--tab-i/.test(decl(thumbBody, 'transform')) && !/transition:[^;]*\bleft\b/.test(thumbBody),
      'transform: ' + decl(thumbBody, 'transform'));
/* 硬质玻璃（2026-11-16 第五轮，用户口径「滑块不需要有弹性」）：滑块只有位移，没有形变 */
check('★ 滑块不再有弹性：没有 goo keyframes、没有 .tab-pill.goo、没有 --pill-scale',
      !/@keyframes tabPillGoo/.test(css) && !/\.tab-pill\.goo/.test(css)
      && !/--pill-scale/.test(css) && !/--pill-origin/.test(css)
      && !/animation/.test(pillBody) && !/scale/.test(pillBody),
      'pill animation: ' + (decl(pillBody, 'animation') || 'none'));
const darkPillCss = cssRule(css, '.theme-dark .tab-pill');
const darkBg = decl(darkPillCss, 'background');
const darkNums = (darkBg.match(/rgba?\((\d+), (\d+), (\d+)/g) || [])
  .map((s) => s.match(/\d+/g).slice(0, 3).map(Number));
check('★ 深色主题滑块是黑玻璃（背景三通道都 < 80）',
      !!darkBg && darkNums.length >= 2 && darkNums.every((c) => c[0] < 80 && c[1] < 80 && c[2] < 80),
      darkBg || '(缺 .theme-dark .tab-pill)');
check('整排 wave 与图标 bump 的 keyframes 都在',
      /@keyframes tabWave/.test(css) && /@keyframes tabIconBump/.test(css)
      && /\.tab-bar\.wave \.tab-item/.test(css) && /\.tab-icon-wrap\.bump/.test(css));
/* 回归守门：气泡避让 tabBar 的验收读的就是这条高度（见 verify_toast_above_tabbar.mjs） */
const tabBarHeight = decl(cssBlock(css, '/* ── Tab Bar ── */'), 'height');

/* 船式起伏（2026-11-16 第六轮）：整条栏用独立 scale 下沉 + 弹簧回弹，
   不能挂在 transform 上 —— transform 已被 --tab-bar-dx 的拖动位移占用 */
const boatRule = cssRule(css, '.tab-bar.boat');
const boatAnim = cssRule(css, '.tab-bar.boat-up');
const boatAt = css.indexOf('@keyframes tabBarBoat');
const boatKey = boatAt < 0 ? '' : css.slice(boatAt, boatAt + 200);
check('★ 船式起伏：.tab-bar 用独立 scale 属性（不抢 transform）',
      /scale: var\(--tab-boat, 1\)/.test(cssBlock(css, '.tab-bar'))
      && !/transform:.*scale\(/.test(cssBlock(css, '.tab-bar')),
      'scale: var(--tab-boat, 1)');
check('★ 按住时整条栏压到 0.965（与网站证书的按压同一个数）',
      /--tab-boat: 0\.965/.test(boatRule), boatRule.replace(/\s+/g, ' ').slice(0, 80));
check('★ 松手弹簧回弹：0.965 → 1.012 过冲 → 1（560ms 弹性曲线）',
      /animation: tabBarBoat/.test(boatAnim)
      && /scale: 0\.965/.test(boatKey) && /scale: 1\.012/.test(boatKey)
      && /scale: 1;/.test(boatKey),
      (boatKey.match(/scale: [0-9.]+/g) || []).join(' → '));
check('tabBar 高度仍是 13.2cqw（气泡避让的几何基准不许动）', tabBarHeight === '13.2cqw', 'height: ' + tabBarHeight);

check('switchTab 会驱动滑块位移', /slideTabThumb\(/.test(js));
check('启动时首帧定位，不从左边滑一遍', /initTabThumb\(\)/.test(js));
/* 拖动（2026-11-16 第二轮）：允许拖，但必须「只在栏体自己的命中面上起手」
   —— 页面内容是 tabBar 的兄弟节点，拖栏不许把页面里那套横滑手势（gestureBus）带起来 */
check('拖动监听绑在 #tabBar 上，不绑 document（不抢页面手势）',
      /bar\.addEventListener\('pointerdown', _tabDragStart\)/.test(js) &&
      /bar\.addEventListener\('pointermove', _tabDragMove\)/.test(js) &&
      !/document\.addEventListener\('pointer(down|move)', _tabDrag/.test(js));
check('拖动时位移层关掉过渡（否则跟不上手指）',
      /\.tab-bar\.dragging \.tab-thumb \{ transition: none; \}/.test(css));
check('整条栏弹性偏移与按下/回弹的规则都在（位移落在 .tab-bar 上，不再挪栏内图标）',
      /--tab-bar-dx/.test(js) && /\.tab-bar\.dragging \{/.test(css) &&
      !/\.tab-bar\.dragging \.tab-item \{/.test(css) &&
      /\.tab-item\.press \{/.test(css) && /@keyframes tabPressRebound/.test(css) &&
      /tab-item\.rebound \{/.test(css));
check('拖动过半格按药丸位置切页（复用 switchTab），且抑制拖动收尾的冒泡 click',
      /_tabSuppressClickUntil/.test(js) && /switchTab\(targetTab\)/.test(js) &&
      /_tabQueuedTab/.test(js) && /_tabVisualIndex\(/.test(js));
check('旧的 .tab-hover 缩放不再叠加到图标弹跳上', !/classList\.add\('tab-hover'\)/.test(js));

/* ── ② 渲染环境（桩后端 + 真实 www/） ──────────────────────────────── */
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
const page = await browser.newPage({ viewport: { width: 360, height: 800 }, deviceScaleFactor: 3 });
page.on('pageerror', (e) => console.log('  [pageerror] ' + e.message));
page.on('console', (m) => { if (m.type() === 'error') console.log('  [console.error] ' + m.text()); });

const LOCAL_BASE = 'http://127.0.0.1:' + port;
const foreign = [];
page.on('request', (r) => {
  const u = r.url();
  if (!u.startsWith(LOCAL_BASE) && !u.startsWith('data:') && !u.startsWith('blob:')) foreign.push(u);
});
await page.addInitScript((base) => {
  try {
    window.__HN_TEST_BASE_URL__ = base;
    localStorage.setItem('hn_theme_mode', 'light');
  } catch (e) { /* 忽略 */ }
}, LOCAL_BASE);
await page.route('**/api/**', (route) => {
  const url = route.request().url();
  if (url.startsWith(LOCAL_BASE)) return route.continue();
  return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(MOCK) });
});
await page.goto(LOCAL_BASE + '/index.html', { waitUntil: 'load' });
await page.waitForFunction(() => typeof switchTab === 'function' && !!document.getElementById('tabThumb'),
                           null, { timeout: 15000 });
await page.waitForTimeout(600);   // 首屏入场动画落定

const geo = () => page.evaluate(() => {
  const rect = (el) => { const r = el.getBoundingClientRect(); return { l: r.left, r: r.right, t: r.top, b: r.bottom, w: r.width, h: r.height }; };
  const bar = rect(document.getElementById('tabBar'));
  const pill = rect(document.getElementById('tabPill'));
  const items = [...document.querySelectorAll('.tab-item')].map((t) => {
    const wrap = t.querySelector('.tab-icon-wrap');
    return Object.assign({ tab: t.dataset.tab, on: t.classList.contains('active'),
                           bump: wrap.classList.contains('bump') },
                         rect(t), { txt: rect(t.querySelector('.tab-text')) });
  });
  return { bar: bar, pill: pill, items: items, thumbI: document.getElementById('tabThumb').getAttribute('data-i'),
           wave: document.getElementById('tabBar').classList.contains('wave'),
           pillAmin: getComputedStyle(document.getElementById('tabPill')).animationName };
});
const centerOf = (r) => (r.l + r.r) / 2;

console.log('\n=== ② 渲染（360×800 @3x）：药丸必须落在选中格子的正中 ===');
const g0 = await geo();
console.log('  tabBar=' + JSON.stringify({ l: px1(g0.bar.l), w: px1(g0.bar.w), h: px1(g0.bar.h) }) +
            '  pill=' + JSON.stringify({ l: px1(g0.pill.l), w: px1(g0.pill.w), h: px1(g0.pill.h) }));
const slot0 = g0.items[0];
const restW = px1(g0.pill.w), restH = px1(g0.pill.h);

check('初始（打印页）：药丸居中在第 1 格', Math.abs(centerOf(g0.pill) - centerOf(slot0)) <= 1.5,
      'pill.c=' + px1(centerOf(g0.pill)) + ' slot1.c=' + px1(centerOf(slot0)));
check('初始：data-i = 0（首帧定位生效）', g0.thumbI === '0', 'data-i=' + g0.thumbI);
check('药丸整体在玻璃栏轮廓内（不探出圆角）',
      g0.pill.l >= g0.bar.l - 0.5 && g0.pill.r <= g0.bar.r + 0.5
      && g0.pill.t >= g0.bar.t - 0.5 && g0.pill.b <= g0.bar.b + 0.5,
      'pill=[' + px1(g0.pill.l) + ',' + px1(g0.pill.r) + '] bar=[' + px1(g0.bar.l) + ',' + px1(g0.bar.r) + ']');
check('药丸四周留白合理（每边 2~6px）',
      (g0.bar.h - restH) / 2 >= 2 && (g0.bar.h - restH) / 2 <= 6,
      'pill.h=' + restH + ' 栏高=' + px1(g0.bar.h));
check('两个 tab 的文字都还在栏内（药丸不能挤掉标签）',
      g0.items.every((it) => it.txt.b <= g0.bar.b + 0.5 && it.txt.t >= g0.bar.t - 0.5),
      g0.items.map((it) => it.tab + ' txt=[' + px1(it.txt.t) + ',' + px1(it.txt.b) + ']').join(' | '));

console.log('\n=== ③ 切换途中：只许位移与 scaleX（落位宽度恒定是重心）===');
await page.evaluate(() => switchTab('me'));
const samples = [];
for (let k = 0; k < 6; k++) {
  await page.waitForTimeout(k === 0 ? 60 : 55);
  const g = await geo();
  samples.push({ t: (k + 1) * 55, cx: centerOf(g.pill), w: px1(g.pill.w), h: px1(g.pill.h), goo: g.pillAmin });
}
await page.waitForTimeout(400);
const g1 = await geo();
const slot1Center = centerOf(g1.items[1]);
console.log('  采样宽度: ' + samples.map((s) => s.w + 'px').join(' '));
check('切换后药丸居中在第 2 格', Math.abs(centerOf(g1.pill) - slot1Center) <= 1.5,
      'pill.c=' + px1(centerOf(g1.pill)) + ' slot2.c=' + px1(slot1Center));
check('data-i 跟着走', g1.thumbI === '1', 'data-i=' + g1.thumbI);
check('落位后宽度与初始完全一致（位移不是宽度动画）', Math.abs(px1(g1.pill.w) - restW) <= 0.5,
      'rest=' + restW + ' now=' + px1(g1.pill.w));
check('落位后高度与初始一致（没有残留 skew）', Math.abs(px1(g1.pill.h) - restH) <= 0.5,
      'rest=' + restH + ' now=' + px1(g1.pill.h));
check('★ 切换途中药丸宽度恒定（硬质玻璃：全程不拉伸）',
      samples.every((s) => Math.abs(s.w - restW) <= 0.6),
      samples.map((s) => px1(s.w)).join(' '));
check('位移过程中药丸始终在栏内', samples.every((s) => s.cx > g0.bar.l && s.cx < g0.bar.r));
check('位置单调朝第 2 格走（没有来回弹）',
      samples.every((s, i) => i === 0 || s.cx >= samples[i - 1].cx - 3),
      samples.map((s) => px1(s.cx)).join(' -> '));


console.log('\n=== ④ 点击反馈：wave 与图标 bump ===');
await page.waitForTimeout(400);
await page.evaluate(() => switchTab('print'));
await page.waitForTimeout(60);
const gw = await geo();
check('点击后整排 wave 立即触发', gw.wave === true);
/* 跨格时图标 bump 故意延后 170ms（滑块先到位、图标再落），所以要在窗口里取两次 */
await page.waitForTimeout(180);
const gb = await geo();
const bumpAmin = await page.evaluate(() => {
  const wrap = document.querySelector('.tab-item.active .tab-icon-wrap');
  return wrap ? getComputedStyle(wrap).animationName : '';
});
check('被点中的格子图标带 bump', gb.items[0].bump === true,
      'items=' + gb.items.map((i) => i.tab + ':' + i.bump).join(' '));
check('bump 跑的是 tabIconBump（真的有 keyframes，不是只挂了个空 class）',
      /tabIconBump/.test(bumpAmin), 'animationName=' + bumpAmin);
await page.waitForTimeout(900);
const gw2 = await geo();
check('动画结束会收干净（class 不残留，不会卡在半途缩放）',
      gw2.wave === false && gw2.items.every((i) => !i.bump));

console.log('\n=== ⑤ 探针自证：把 transform 钉死（模拟滑块不动的坏实现）===');
await page.evaluate(() => {
  const tag = document.createElement('style');
  tag.id = '__probe_pin';
  tag.textContent = '.tab-thumb { transform: none !important; }';
  document.head.appendChild(tag);
});
await page.evaluate(() => switchTab('me'));
await page.waitForTimeout(600);
const gp = await geo();
check('★ 坏实现下药丸真的不动（说明探针量的是真实位置，不是 DOM 里的数字）',
      Math.abs(centerOf(gp.pill) - centerOf(slot0)) <= 1.5 && gp.thumbI === '1',
      'pill.c=' + px1(centerOf(gp.pill)) + '（此时 data-i 已是 1，但药丸还在第 1 格）');
await page.evaluate(() => document.getElementById('__probe_pin').remove());

console.log('\n=== ⑥ 拖动：跟手 / 越界阻尼 / 过半格吸附 / 按下船式回弹 ===');
await page.evaluate(() => switchTab('print'));
await page.waitForTimeout(800);
const barBox = await page.evaluate(() => {
  const r = document.getElementById('tabBar').getBoundingClientRect();
  return { x: r.left, y: r.top, w: r.width, h: r.height };
});
const stepPx = await page.evaluate(() => {
  const items = document.querySelectorAll('.tab-item');
  return items[1].getBoundingClientRect().left - items[0].getBoundingClientRect().left;
});
const dragGeo = () => page.evaluate(() => {
  const bar = document.getElementById('tabBar');
  const thumb = document.getElementById('tabThumb');
  const items = [...document.querySelectorAll('.tab-item')];
  /* 船式起伏会让整条栏 scale 到 .965，getBoundingClientRect 拿到的就是缩过的坐标。
     这里统一按栏的视觉缩放比还原成「未缩放」坐标系（缩放绕中心），
     否则 60px 的拖动只会量到 57.9px，越界与栏内夹取那些断言全会被一个系数带偏。 */
  const rawBar = bar.getBoundingClientRect();
  const s = rawBar.width / bar.offsetWidth || 1;
  const cx = rawBar.left + rawBar.width / 2;
  const ux = (x) => cx - bar.offsetWidth / 2 + (x - rawBar.left) / s;
  const rect = (el) => {
    if (!el) return { l: 0, r: 0, w: 0 };
    const r = el.getBoundingClientRect();
    return { l: ux(r.left), r: ux(r.right), w: r.width / s };
  };
  const scaleOf = (el) => {
    const m = getComputedStyle(el).transform.match(/matrix\(([^)]*)\)/);
    return m ? parseFloat(m[1].split(',')[0]) : 1;
  };
  return {
    thumb: rect(thumb),
    frac: parseFloat(getComputedStyle(thumb).getPropertyValue('--tab-i')),
    barDx: parseFloat(getComputedStyle(bar).getPropertyValue('--tab-bar-dx')) || 0,
    barL: ux(rawBar.left),
    items: items.map((t) => Object.assign(rect(t), {
      press: t.classList.contains('press'),
      rebound: t.classList.contains('rebound'),
      scale: scaleOf(t),
      amin: getComputedStyle(t).animationName,
    })),
    pill: rect(document.getElementById('tabPill')),
    dragging: bar.classList.contains('dragging'),
    settling: bar.classList.contains('settling'),
    boat: { down: bar.classList.contains('boat'), up: bar.classList.contains('boat-up'), scale: s },
    dataI: thumb.getAttribute('data-i'),
    tab: typeof currentTab === 'string' ? currentTab : '',
    meHidden: getComputedStyle(document.getElementById('page-me')).display === 'none',
  };
});
const cx0 = barBox.x + barBox.w * 0.25;   // 第 1 格中心附近
const cy = barBox.y + barBox.h / 2;
const d0 = await dragGeo();
const thumb0L = d0.thumb.l, thumb0W = d0.thumb.w, item0L = d0.items[0].l;
console.log('  栏=' + px1(barBox.w) + 'px 格距=' + px1(stepPx) + 'px 药丸宽=' + px1(thumb0W) + 'px 起点x=' + px1(cx0));

/* A. 小拖（不到半格）：跟手但不切页，松手弹回 */
await page.mouse.move(cx0, cy);
await page.mouse.down();
await page.mouse.move(cx0 + 20, cy, { steps: 3 });
await page.mouse.move(cx0 + 60, cy, { steps: 5 });
await page.waitForTimeout(150);   // 整排跟随带 90ms 过渡：等它落定再量，量的是稳态偏移
const dA = await dragGeo();
check('拖动激活后进入 dragging（位移层过渡已关）', dA.dragging === true);
check('位移层 1:1 跟手', Math.abs((dA.thumb.l - thumb0L) - 60) <= 2,
      'Δthumb=' + px1(dA.thumb.l - thumb0L) + 'px / 手指 60px');
check('整条栏朝滑动方向错开（12%，最多 10px）',
      Math.abs((dA.barL - barBox.x) - 60 * 0.12) <= 1.5 && Math.abs(dA.barL - barBox.x) <= 10,
      'Δbar=' + px1(dA.barL - barBox.x) + 'px（手指 60px）');
check('栏内图标相对栏体不动（动的是整条栏，不是各图标各自平移）',
      Math.abs((dA.items[0].l - item0L) - (dA.barL - barBox.x)) <= 0.6,
      'Δitem=' + px1(dA.items[0].l - item0L) + ' Δbar=' + px1(dA.barL - barBox.x));
check('药丸内部行程扣掉整条栏位移（屏幕上仍停在手指下）',
      Math.abs(dA.thumb.l - thumb0L - 60) <= 2, 'Δthumb(screen)=' + px1(dA.thumb.l - thumb0L));
check('★ 拖动中药丸宽度不变（硬质玻璃：只平移、不形变）',
      Math.abs(dA.pill.w - restW) <= 0.6 && Math.abs(dA.thumb.w - thumb0W) <= 0.6,
      'pill.w=' + px1(dA.pill.w) + ' rest=' + restW);
check('没松手就不切页', dA.meHidden === true && dA.tab === 'print');
check('★ 按住时整条栏下沉（scale ≈ 0.965，像一块硬玻璃被按下去）',
      dA.boat.down === true && Math.abs(dA.boat.scale - 0.965) <= 0.005,
      'scale=' + dA.boat.scale.toFixed(4) + ' boat=' + dA.boat.down);
await page.mouse.up();
await page.waitForTimeout(480);
const dA2 = await dragGeo();
check('未过半格松手 → 弹回原格', dA2.dataI === '0' && Math.abs(dA2.thumb.l - thumb0L) <= 1.5,
      'Δthumb=' + px1(dA2.thumb.l - thumb0L) + ' data-i=' + dA2.dataI);
check('收尾后整条栏归零、dragging/settling 都收干净',
      Math.abs(dA2.barL - barBox.x) < 0.6 && Math.abs(dA2.barDx) < 0.2 &&
      dA2.dragging === false && dA2.settling === false,
      'Δbar=' + px1(dA2.barL - barBox.x));
await page.waitForTimeout(260);   // 船式回弹 560ms 收尾
const dBoat = await dragGeo();
check('★ 松手后整条栏弹回原尺寸（scale 回到 1，boat / boat-up 收干净）',
      Math.abs(dBoat.boat.scale - 1) <= 0.004 && dBoat.boat.down === false && dBoat.boat.up === false,
      'scale=' + dBoat.boat.scale.toFixed(4));

/* B. 越界：往左拖出第 1 格，位移应被橡皮筋压到 35% */
await page.mouse.move(cx0, cy);
await page.mouse.down();
await page.mouse.move(cx0 - 25, cy, { steps: 3 });
await page.mouse.move(cx0 - 60, cy, { steps: 5 });
await page.waitForTimeout(150);
const dB = await dragGeo();
// 越界时整条栏也一起挪了，判定要跟「挪过之后的栏体」比，而不是静止位置
check('左越界时药丸仍被挡在玻璃栏内（不探出圆角轮廓）',
      dB.thumb.l >= dB.barL - 0.2 && dB.thumb.l <= dB.barL + 4.4,
      'thumb.l=' + px1(dB.thumb.l) + ' bar.l=' + px1(dB.barL));
check('越界量被压在阻尼里（不是 1:1，也不会被拉走）',
      dB.thumb.l - thumb0L <= -8 && dB.thumb.l - thumb0L >= -14,
      'Δthumb(screen)=' + px1(dB.thumb.l - thumb0L) + 'px（手指 -60；整条栏 -7.35 + 栏内夹取 -4.07）');
check('越界时整条栏继续同向错开（拉扯感落在栏体上，仍 ≤10px）',
      dB.barL - barBox.x < -6 && dB.barL - barBox.x > -10,
      'Δbar=' + px1(dB.barL - barBox.x) + 'px');
await page.mouse.up();
await page.waitForTimeout(480);
const dB2 = await dragGeo();
check('越界松手 → 回第 1 格', dB2.dataI === '0' && Math.abs(dB2.thumb.l - thumb0L) <= 1.5);

/* C. 竖向拖：方向锁定让位，滑块不许动 */
await page.mouse.move(cx0, cy);
await page.mouse.down();
await page.mouse.move(cx0 + 2, cy - 40, { steps: 5 });
const dC = await dragGeo();
check('竖向拖不激活滑块（方向锁定让位）',
      dC.dragging === false && Math.abs(dC.thumb.l - thumb0L) <= 1.5);
await page.mouse.up();
await page.waitForTimeout(320);

/* D. 点按：同格按下先压缩，松手船式回弹 */
await page.mouse.move(cx0, cy);
await page.mouse.down();
await page.waitForTimeout(130);
const dD = await dragGeo();
check('按下格子先压缩（scale ≈ 0.92）',
      dD.items[0].press === true && Math.abs(dD.items[0].scale - 0.92) <= 0.02,
      'press=' + dD.items[0].press + ' scale=' + dD.items[0].scale);
await page.mouse.up();
await page.waitForTimeout(70);
const dD2 = await dragGeo();
check('松手船式回弹（rebound 动画带过冲）',
      dD2.items[0].rebound === true && /tabPressRebound/.test(dD2.items[0].amin),
      'rebound=' + dD2.items[0].rebound + ' anim=' + dD2.items[0].amin);
await page.waitForTimeout(520);
const dD3 = await dragGeo();
check('回弹收干净（class 不残留）', dD3.items.every((i) => !i.press && !i.rebound));

/* E. 过半格松手 → 吸附相邻格并切页 */
await page.mouse.move(cx0, cy);
await page.mouse.down();
await page.mouse.move(cx0 + 50, cy, { steps: 4 });
await page.mouse.move(cx0 + 110, cy, { steps: 6 });
await page.mouse.up();
await page.waitForTimeout(850);
const dE = await dragGeo();
check('过半格松手 → 吸附第 2 格并切到「我」页',
      dE.dataI === '1' && dE.tab === 'me' && dE.meHidden === false &&
      Math.abs(centerOf(dE.items[1]) - centerOf(dE.pill)) <= 2,
      'data-i=' + dE.dataI + ' tab=' + dE.tab + ' pill.c=' + px1(centerOf(dE.pill)) +
      ' slot2.c=' + px1(centerOf(dE.items[1])));

/* F. 真机路径：触摸事件（CDP 派发 touch，Chrome 会合成 pointer 事件）——鼠标能拖不等于手指能拖 */
await page.evaluate(() => switchTab('print'));
await page.waitForTimeout(700);
const cdp = await page.context().newCDPSession(page);
await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 });
const t0 = await dragGeo();
const scroll0 = await page.evaluate(() => window.scrollY);
async function touch(type, x, y) {
  await cdp.send('Input.dispatchTouchEvent', {
    type,
    touchPoints: type === 'touchEnd' ? [] : [{ x, y, id: 1 }],
  });
}
await touch('touchStart', cx0, cy);
for (const dx of [6, 18, 34, 52, 60]) {
  await touch('touchMove', cx0 + dx, cy);
  await page.waitForTimeout(24);
}
await page.waitForTimeout(160);
const dF = await dragGeo();
check('手指拖也能激活（touch → pointer 合成路径通）', dF.dragging === true && dF.tab === 'print',
      'dragging=' + dF.dragging + ' tab=' + dF.tab);
check('手指拖同样 1:1 跟手', Math.abs((dF.thumb.l - t0.thumb.l) - 60) <= 3,
      'Δthumb=' + px1(dF.thumb.l - t0.thumb.l) + 'px / 手指 60px');
check('触摸拖栏不把页面滚走（touch-action 生效）',
      (await page.evaluate(() => window.scrollY)) === scroll0);
await touch('touchEnd', cx0 + 60, cy);
await page.waitForTimeout(480);
const dF2 = await dragGeo();
check('手指松手同样弹回原格、收干净',
      dF2.dataI === '0' && dF2.dragging === false && Math.abs(dF2.thumb.l - t0.thumb.l) <= 1.5,
      'data-i=' + dF2.dataI + ' Δthumb=' + px1(dF2.thumb.l - t0.thumb.l));
await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: false });
await cdp.detach();

/* G. 转场（480ms 平铺推拉）途中：滑块必须始终可滑，松手按药丸位置决定切哪页 */
await page.evaluate(() => switchTab('print'));
await page.waitForTimeout(700);
await page.evaluate(() => switchTab('me'));      // 起一趟转场，不等它落定
await page.waitForTimeout(120);
await page.mouse.move(cx0, cy);
await page.mouse.down();
await page.waitForTimeout(80);
const dG0 = await dragGeo();            // 按下那一刻冻住的位置（转场跑了一半）
/* 冻结点可能已经很靠近第 2 格（transition 260ms 比 320ms 跑得快），
   行程按「离终点还剩多少」算，否则 frac 会被 1.028 的越界夹取吃掉，测到的跟手距离变小 */
const roomPx = (1 + 0.028) * stepPx - (dG0.thumb.l - thumb0L);
const dz = Math.max(8, Math.min(24, roomPx * 0.6));
await page.mouse.move(cx0 + dz, cy, { steps: 5 });
await page.waitForTimeout(120);
const dG = await dragGeo();
check('★ 页面转场途中也能起手拖动（滑块不再被 _tabSwitching 锁死）',
      dG.dragging === true, 'dragging=' + dG.dragging + ' tab=' + dG.tab);
check('转场途中起手是「冻在当前视觉位置」再跟手（不跳到终点）',
      (dG0.thumb.l - thumb0L) > 8 && (dG0.thumb.l - thumb0L) < stepPx - 8 &&
      Math.abs(dG.thumb.l - dG0.thumb.l - dz) <= 3 && dG.thumb.l - thumb0L < stepPx - 1,
      '冻结在 ' + px1(dG0.thumb.l - thumb0L) + 'px（应在 8~' + px1(stepPx - 8) +
      ' 之间，离夹取端 ' + px1(roomPx) + 'px）→ 再跟手 ' + px1(dG.thumb.l - dG0.thumb.l) +
      'px / 手指 ' + px1(dz) + 'px，且仍没到终点（终点 ' + px1(stepPx) + '）');
await page.mouse.up();
await page.waitForTimeout(1100);
const dG2 = await dragGeo();
check('松手按药丸位置切页（拖过半格 → 落在第 2 格并切到「我」）',
      dG2.dataI === '1' && dG2.tab === 'me' && dG2.meHidden === false,
      'data-i=' + dG2.dataI + ' tab=' + dG2.tab);

/* 反向：转场跑到一半把药丸拖回第 1 格，松手必须回到「打印」 */
await page.evaluate(() => switchTab('print'));
await page.waitForTimeout(700);
await page.evaluate(() => switchTab('me'));
await page.waitForTimeout(120);
await page.mouse.move(cx0, cy);
await page.mouse.down();
await page.mouse.move(cx0 - 90, cy, { steps: 5 });
await page.waitForTimeout(120);
await page.mouse.up();
await page.waitForTimeout(1500);
const dG3 = await dragGeo();
check('★ 转场途中反向拖回去 → 等这一趟落定后按手指位置切回「打印」',
      dG3.dataI === '0' && dG3.tab === 'print' && dG3.meHidden === true,
      'data-i=' + dG3.dataI + ' tab=' + dG3.tab + ' meHidden=' + dG3.meHidden);

console.log('\n=== ⑦ 截图 ===');
async function shot(mode, tab, file) {
  await page.evaluate((m) => {
    localStorage.setItem('hn_theme_mode', m);
    if (typeof applyTheme === 'function') applyTheme(m, { skipServer: true });
  }, mode);
  await page.evaluate((t) => switchTab(t), tab);
  await page.waitForTimeout(800);
  await page.screenshot({ path: SHOTS + '/' + file, clip: { x: 0, y: 640, width: 360, height: 160 } });
  console.log('  · ' + file);
}
await page.evaluate(() => switchTab('print'));
await page.waitForTimeout(700);
await shot('light', 'print', '浅色_打印选中.png');
await shot('light', 'me', '浅色_我选中.png');
await shot('dark', 'me', '深色_我选中.png');
await shot('dark', 'print', '深色_打印选中.png');

/* 深色模式实测：滑块必须变成黑玻璃（静态 CSS 对了也可能被别的规则盖掉） */
const darkPillRt = await page.evaluate(() => {
  const p = document.getElementById('tabPill');
  if (!p) return { img: '', nums: [] };
  const img = getComputedStyle(p).backgroundImage;
  return { img, nums: (img.match(/\d+/g) || []).slice(0, 3).map(Number) };
});
check('★ 深色模式实测：滑块背景三通道都 < 80（黑玻璃，不是白药丸）',
      darkPillRt.nums.length >= 3 && darkPillRt.nums.every((n) => n < 80),
      'bg=' + String(darkPillRt.img).replace(/\s+/g, ' ').slice(0, 90));

/* 拖动中的两张：整条栏错开 + 药丸在手指下（静态图看不出这个，必须留证据） */
await page.evaluate(() => switchTab('print'));
await page.waitForTimeout(700);
await page.mouse.move(cx0, cy);
await page.mouse.down();
await page.mouse.move(cx0 + 34, cy, { steps: 4 });
await page.mouse.move(cx0 + 60, cy, { steps: 5 });
await page.waitForTimeout(180);
await page.screenshot({ path: SHOTS + '/拖动中_右拖.png', clip: { x: 0, y: 640, width: 360, height: 160 } });
console.log('  · 拖动中_右拖.png');
await page.mouse.up();
await page.waitForTimeout(480);
await page.mouse.move(cx0, cy);
await page.mouse.down();
await page.mouse.move(cx0 - 30, cy, { steps: 4 });
await page.mouse.move(cx0 - 60, cy, { steps: 5 });
await page.waitForTimeout(180);
await page.screenshot({ path: SHOTS + '/拖动中_左越界.png', clip: { x: 0, y: 640, width: 360, height: 160 } });
console.log('  · 拖动中_左越界.png');
await page.mouse.up();
await page.waitForTimeout(480);

/* 按住瞬间：整条栏下沉（船式起伏的第一拍），松手后应当弹回原尺寸 */
await page.evaluate(() => {
  localStorage.setItem('hn_theme_mode', 'light');
  if (typeof applyTheme === 'function') applyTheme('light', { skipServer: true });
});
await page.evaluate(() => switchTab('print'));
await page.waitForTimeout(700);
await page.mouse.move(cx0, cy);
await page.mouse.down();
await page.waitForTimeout(180);
await page.screenshot({ path: SHOTS + '/按下_船式起伏.png', clip: { x: 0, y: 640, width: 360, height: 160 } });
console.log('  · 按下_船式起伏.png');
await page.mouse.up();
await page.waitForTimeout(700);

check('全程没有打到生产接口（只访问本地桩）', foreign.length === 0, foreign.slice(0, 3).join(' | '));

await browser.close();
server.close();

console.log('\n' + (failures ? '有 ' + failures + ' 项失败 ❌' : '全绿 ✅'));
process.exit(failures ? 1 : 0);
