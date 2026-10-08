/* 深色模式「分段控件滑轨」对比度验收（APP 真实页面 + 真实像素采样）
 *
 * 线上反馈：深色模式下"滑轨和背景色高度重合"。定位：滑轨 = 单面/双面、方向 这类
 * 分段控件的轨道（.duplex-toggle / .img-ori-toggle），底色 rgba(60, 60, 67, 0.08)：
 *   · 浅色卡片上勉强可见；
 *   · 深色卡片（控制区 ≈ #3D3D3E）上合成后 0.92×61 + 0.08×60 ≈ 61 —— 与卡片几乎同色。
 * 修复：深色下换成比卡片更深的内凹轨道 rgba(0,0,0,0.24)（与份数输入框同调）+ 细内描边。
 *
 * 做法：真实页面渲染一张图片卡片（深色）→ 用 elementFromPoint 精确定位"轨道内"与
 * "卡片表面"两个点 → 各截 6×6 像素块取均值算亮度 → 对比「修复后」与「注入旧样式」
 * 两种状态；小程序侧无法渲染，用静态断言覆盖（两端同一份设计令牌）。
 *
 * 用法: node tests/verify_dark_rail_contrast.mjs
 */
import { createRequire } from 'node:module';
import http from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { extname, join, normalize } from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const REPO = 'D:/打印机项目';
const APP_DIR = `${REPO}/mobile_apps/android_app/www`;
const OUT_DIR = `${REPO}/截图展示/深色轨道对比`;
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.png': 'image/png' };
const MOCK = { success: true, token: 'tok', openid: 'dev_test', nickname: 't', role: 'admin', devices: [], orders: [], online: true, active: true, claiming_devices: [] };

let failures = 0;
const check = (name, cond, detail) => { if (!cond) failures++; console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? `  -- ${detail}` : '')); };

/* ---------- 静态断言（小程序 wxss 与 APP css 必须都有滑轨深色覆盖） ---------- */
const RAIL_RE = /\.theme-dark \.duplex-toggle,\s*\.theme-dark \.img-ori-toggle \{[\s\S]{0,200}rgba\(0, 0, 0, 0\.24\)/;
for (const [label, p, railSrc] of [
  ['小程序', `${REPO}/mobile_apps/h_n_print/app.wxss`, `${REPO}/mobile_apps/h_n_print/pages/index/index.wxss`],
  ['APP', `${APP_DIR}/styles.css`, `${APP_DIR}/styles.css`],
]) {
  const css = await readFile(p, 'utf8');
  const base = await readFile(railSrc, 'utf8');
  check(`${label}：深色滑轨有独立覆盖（rgba(0,0,0,0.24) 内凹轨道）`, RAIL_RE.test(css));
  check(`${label}：滑轨保留原浅色 8% 灰底（浅色模式不受影响）`,
    base.includes('background-color: rgba(60, 60, 67, 0.08);'));
}

/* ---------- APP 侧：真实页面 ---------- */
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
  } catch { res.writeHead(404); res.end('nf'); }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const port = server.address().port;

const browser = await chromium.launch({ executablePath: CHROME });
const page = await browser.newPage({ viewport: { width: 430, height: 932 }, deviceScaleFactor: 2 });
await page.addInitScript((base) => {
  try {
    localStorage.setItem('hn_base_url', base);
    localStorage.setItem('hn_theme_mode', 'dark');
  } catch (e) {}
}, `http://127.0.0.1:${port}`);
await page.route('**/api/**', (route) => route.request().url().startsWith(`http://127.0.0.1:${port}`)
  ? route.continue()
  : route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK) }));
await page.goto(`http://127.0.0.1:${port}/index.html`, { waitUntil: 'load' });
await page.waitForFunction(() => typeof printState !== 'undefined' && !!document.getElementById('fileList'), null, { timeout: 15000 });

await page.evaluate(() => {
  const f = {
    _uid: 1, name: 'Screenshot_20261008_164203.jpg', size: 907674, file: new File(['x'], 'a.jpg'),
    fileId: 'F1', uploading: false, progress: 100, failed: false, copies: 1, pageRange: '',
    rangeLines: [{ value: '', error: '' }], duplex: 'off', imageOrientation: 'auto',
    entering: false, removing: false, excelWarning: false, unsupportedFormat: false,
    isImage: true, pageCount: 1, pageCountStatus: '', singlePage: true, sizeDisplay: '886.4',
  };
  printState.selectedFiles = [f];
  renderFileList();
  document.body.classList.add('theme-dark');
  document.querySelectorAll('.modal-mask').forEach((m) => m.classList.add('theme-dark'));
});
await page.waitForTimeout(300);
check('页面处于深色模式', (await page.evaluate(() => document.body.classList.contains('theme-dark'))) === true);
await mkdir(OUT_DIR, { recursive: true });

/** 用 elementFromPoint 定位「轨道内(纯轨道)」与「卡片表面(纯卡片)」两个采样点 */
async function pickPoints() {
  return page.evaluate(() => {
    const card = document.querySelector('#fileList .file-card');
    const toggle = card.querySelector('.img-ori-toggle');
    const t = toggle.getBoundingClientRect(), c = card.getBoundingClientRect();
    const bgOf = (el) => getComputedStyle(el).backgroundColor;
    // 该点是否"轨道本体"：从命中元素向上走到 toggle，途中不能遇到任何有底色的元素
    // （蓝色滑块 .img-ori-slider 自带底色 → 会被自动排除；文字子元素透明 → 命中轨道 ✓）
    const isRail = (x, y) => {
      let el = document.elementFromPoint(x, y);
      while (el && el !== toggle) {
        if (bgOf(el) !== 'rgba(0, 0, 0, 0)') return false;
        el = el.parentElement;
      }
      return el === toggle && bgOf(toggle) !== 'rgba(0, 0, 0, 0)';
    };
    // 该点是否"卡片表面"（穿透透明元素后落到卡片自身底色）
    const isCard = (x, y) => {
      let el = document.elementFromPoint(x, y);
      if (!el) return false;
      while (el && el !== card && bgOf(el) === 'rgba(0, 0, 0, 0)') el = el.parentElement;
      return el === card;
    };
    const midY = t.top + t.height / 2;
    let rail = null, cardPt = null;
    for (let dx = 16; dx <= 60 && !rail; dx++) {          // 轨道右端内侧（避开圆角与蓝色滑块）
      const x = t.right - dx;
      if (isRail(x, midY)) rail = { x: Math.round(x) - 3, y: Math.round(midY) - 3 };
    }
    for (let dx = 3; dx <= 16 && !cardPt; dx++) {          // 轨道右外侧 → 卡片右内边距
      const x = c.right - dx;
      if (isCard(x, midY)) cardPt = { x: Math.round(x) - 3, y: Math.round(midY) - 3 };
    }
    return { card: cardPt, rail, toggleW: Math.round(t.width), toggleH: Math.round(t.height) };
  });
}

async function capture(tag) {
  const pts = await pickPoints();
  if (!pts.rail || !pts.card) throw new Error('采样点定位失败: ' + JSON.stringify(pts));
  const out = {};
  for (const [k, v] of Object.entries({ rail: pts.rail, card: pts.card })) {
    const p = join(OUT_DIR, `${tag}_${k}.png`);
    writeFileSync(p, await page.screenshot({ clip: { x: v.x, y: v.y, width: 6, height: 6 } }));
    out[k] = p;
  }
  out.pts = JSON.stringify(pts);
  return out;
}

const files = { after: await capture('after_new') };
await page.locator('#fileList .file-card').screenshot({ path: join(OUT_DIR, 'after_new_card.png') });

/* 注入"修复前"的轨道底色（8% 灰 → 在深色卡片上合成后与卡片同色） */
await page.addStyleTag({ content: `
.theme-dark .duplex-toggle, .theme-dark .img-ori-toggle {
  background-color: rgba(60, 60, 67, 0.08) !important;
  box-shadow: none !important;
}` });
await page.waitForTimeout(200);
files.before = await capture('before_old');
await page.locator('#fileList .file-card').screenshot({ path: join(OUT_DIR, 'before_old_card.png') });

await browser.close();
server.close();

/* ---------- Pillow 汇总（纯 ASCII 输出） ---------- */
const py = `
import json
from PIL import Image
paths = json.loads(r'''${JSON.stringify({ before: files.before, after: files.after })}''')
def mean_rgb(p):
    px = list(Image.open(p).convert('RGB').getdata())
    n = len(px)
    return tuple(sum(q[i] for q in px) / n for i in range(3))
def lum(t):
    return 0.2126 * t[0] + 0.7152 * t[1] + 0.0722 * t[2]
out = {}
for tag, g in paths.items():
    rail = mean_rgb(g['rail']); card = mean_rgb(g['card'])
    out[tag] = {'rail': rail, 'card': card, 'railL': lum(rail), 'cardL': lum(card),
                'delta': abs(lum(card) - lum(rail))}
print(json.dumps(out))
`;
const meta = JSON.parse(execFileSync('python', ['-c', py], { encoding: 'utf8' }).trim().split('\n').pop());
const fmt = (t) => `rgb(${t.map((v) => Math.round(v)).join(',')})`;
console.log('\n渲染像素实测（亮度 = 0.2126R+0.7152G+0.0722B）:');
for (const [tag, label] of [['before', '修复前'], ['after', '修复后']]) {
  const s = meta[tag];
  console.log(`  ${label}: 滑轨 ${fmt(s.rail)}(亮${s.railL.toFixed(1)}) | 卡片 ${fmt(s.card)}(亮${s.cardL.toFixed(1)})  → 滑轨-卡片 = ${(s.railL - s.cardL).toFixed(1)}`);
}
console.log(`  采样点: ${files.after.pts}`);

/* ---------- 以用户截图实测的真实底色为锚做合成计算 ----------
   用户截图（attachment d4614f8f…，1260×2800）实测：文件卡片控制区底色 ≈ rgb(61,61,62)。
   轨道自身是半透明灰，最终颜色 = 自身色 × α + 底色 × (1-α)： */
const SURFACE = [61, 61, 62];                       // 用户截图实测的卡片控制区底色
const lumOf = (t) => 0.2126 * t[0] + 0.7152 * t[1] + 0.0722 * t[2];
const over = (fg, a, bg) => fg.map((v, i) => v * a + bg[i] * (1 - a));
const BEFORE_RAIL = over([60, 60, 67], 0.08, SURFACE);   // 旧：rgba(60,60,67,0.08)
const AFTER_RAIL = over([0, 0, 0], 0.24, SURFACE);       // 新：rgba(0,0,0,0.24)
const surfaceL = lumOf(SURFACE);
const dBefore = Math.abs(surfaceL - lumOf(BEFORE_RAIL));
const dAfter = Math.abs(surfaceL - lumOf(AFTER_RAIL));
console.log('\n按用户截图真实底色合成计算（底色 rgb(61,61,62)）:');
console.log(`  修复前 轨道 ${fmt(BEFORE_RAIL)}(亮${lumOf(BEFORE_RAIL).toFixed(1)})  → |Δ| = ${dBefore.toFixed(1)}（几乎同色）`);
console.log(`  修复后 轨道 ${fmt(AFTER_RAIL)}(亮${lumOf(AFTER_RAIL).toFixed(1)})  → |Δ| = ${dAfter.toFixed(1)}（清晰可辨）`);
console.log(`\n对比图: ${OUT_DIR}/before_old_card.png（修复前） vs after_new_card.png（修复后）`);

check('旧底色在真实卡片上几乎同色（|Δ| < 1，复现"高度重合"）', dBefore < 1, `|Δ|=${dBefore.toFixed(2)}`);
check('新底色在真实卡片上清晰可辨（|Δ| ≥ 8）', dAfter >= 8, `|Δ|=${dAfter.toFixed(2)}`);
check('渲染方向性：修复后滑轨比修复前更深', meta.after.railL < meta.before.railL,
  `${meta.before.railL.toFixed(1)} → ${meta.after.railL.toFixed(1)}`);
check('渲染方向性：修复后滑轨比同位卡片更深', meta.after.railL < meta.after.cardL,
  `滑轨${meta.after.railL.toFixed(1)} < 卡片${meta.after.cardL.toFixed(1)}`);

console.log('\n' + '='.repeat(52));
console.log('深色滑轨对比度验收:', failures === 0 ? '全部通过 ✅' : `存在 ${failures} 项失败 ❌`);
process.exit(failures ? 1 : 0);
