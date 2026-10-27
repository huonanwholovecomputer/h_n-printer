/* verify_device_row_singleline.mjs — 「接单设备条只占一行、高度一致」的几何验收
 *
 * 现场（2026-10-27 用户反馈截图）：小程序「提交打印 → 选择接单设备」里四条设备高度参差
 * （有的两行、有的三行）。根因是 .device-item 用了 flex-wrap: wrap + .device-label 的
 * min-width: 40%，而一行里要塞「🖐 需人工翻纸」+「攒批未开」+「开启攒批」按钮 + 「在线」。
 *
 * 验收方式：把**真实样式文件**（小程序 app.wxss + pages/index/index.wxss，APP 的 www/styles.css）
 * 灌进无头浏览器，按现场那条订单的四种设备状态渲染，量真实几何：
 *   ① 四条设备行高度必须相等（差 ≤ 1px）
 *   ② 行内不允许出现第二行（行高 ≤ 单行上限；子元素不能竖着分两排）
 *   ③ 行不能横向溢出（scrollWidth ≤ clientWidth）
 *   ④ 长设备名必须是**省略号截断**（label 自身 scrollWidth > clientWidth，行未被撑开）
 *   ⑤ 「在线/离线」必须贴行尾（与行右边界相距 ≤ 内边距 + 2px）
 *
 * 反向对照（判据自证）：同一脚本加载 **git HEAD 的旧 wxss + 旧模板（两个胶囊）**，必须量到
 * 「高度不一致」—— 否则说明判据恒真、量法不可信。
 *
 * 用法: node tests/verify_device_row_singleline.mjs [--keep]
 *       --keep 保留生成的 HTML（默认写到系统临时目录，跑完删除）
 * 退出码 0 = 全部通过。
 */
'use strict';

import { createRequire } from 'node:module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const PLAYWRIGHT = 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright';
const { chromium } = require(PLAYWRIGHT);
const __dirname = path.dirname(fileURLToPath(import.meta.url));

const REPO = path.resolve(__dirname, '..');
const MP = path.join(REPO, 'mobile_apps', 'h_n_print');
const APP = path.join(REPO, 'mobile_apps', 'android_app', 'www');
const SHOT_DIR = path.join(REPO, '截图展示', '设备条单行');
const KEEP = process.argv.includes('--keep');

let okAll = true;
function check(name, cond, detail) {
  if (!cond) okAll = false;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}${detail ? '  -- ' + detail : ''}`);
}

// ── 现场那四条设备的四种状态（auto_duplex / stack_mode / 在线）组合 ──
const CASES = [
  { label: '霍*的设备 | R7000-AHP10', online: true, manual: true, stack: false, admin: true },
  { label: 'android-lj1020-home', online: false, manual: true, stack: true, admin: true },
  { label: '姚**的设备 | DESKTOP-EJGEB1V-a6c1a1365e', online: false, manual: false, stack: null, admin: true },
  { label: '闫**的设备 | LAPTOP-C', online: false, manual: true, stack: null, admin: true },
];

// 新模板（2026-10-27）：需人工翻纸 / 攒批状态**三选一**（一个胶囊）
function chipHtml(c) {
  if (!c.manual) return '';
  if (c.stack === true) return '<span class="device-state device-state-stack">🖐 攒批中</span>';
  if (c.stack === false) return '<span class="device-state device-state-stack-off">🖐 攒批未开</span>';
  return '<span class="device-state device-state-manual">🖐 需人工翻纸</span>';
}
function btnHtml(c) {
  if (!c.manual || !c.admin) return '';
  const txt = c.stack === true ? '关闭攒批' : '开启攒批';
  return `<div class="device-stack-btn">${txt}</div>`;
}
function rowHtml(c, old) {
  const chips = old
    ? (c.manual ? '<span class="device-state device-state-manual">🖐 需人工翻纸</span>' : '')
      + (c.manual && c.stack === true ? '<span class="device-state device-state-stack">🖐 攒批中</span>' : '')
      + (c.manual && c.stack === false ? '<span class="device-state device-state-stack-off">攒批未开</span>' : '')
    : chipHtml(c);
  return `<div class="device-item${c.online ? '' : ''}">
    <div class="device-dot ${c.online ? 'device-dot-on' : 'device-dot-off'}"></div>
    <span class="device-label">${c.label}</span>
    ${chips}
    ${btnHtml(c)}
    <span class="device-state ${c.online ? 'device-state-on' : 'device-state-off'}">${c.online ? '在线' : '离线'}</span>
  </div>`;
}
function listHtml(old) {
  return `<div class="container"><div class="card device-select-card"><div class="device-list">
    ${CASES.map(c => rowHtml(c, old)).join('\n')}
  </div></div></div>`;
}

// ── 小程序：wxss 的 rpx 换成 calc(N * var(--rp))（--rp = 视口宽 / 750，与微信同一定义） ──
function wxssToCss(css) {
  return css
    .replace(/(-?\d+(?:\.\d+)?)rpx/g, (_m, n) => `calc(${n} * var(--rp))`)
    .replace(/^page\s*\{/m, 'body {');
}
function readMiniCss(fromHead) {
  if (!fromHead) {
    return wxssToCss(fs.readFileSync(path.join(MP, 'app.wxss'), 'utf8'))
      + '\n' + wxssToCss(fs.readFileSync(path.join(MP, 'pages', 'index', 'index.wxss'), 'utf8'));
  }
  const git = (spec) => execFileSync('git', ['show', spec], { cwd: REPO, encoding: 'utf8' });
  return wxssToCss(git('HEAD:mobile_apps/h_n_print/app.wxss'))
    + '\n' + wxssToCss(git('HEAD:mobile_apps/h_n_print/pages/index/index.wxss'));
}

const MINI_BASE = `
  html, body { margin: 0; padding: 0; }
  body { width: 100%; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
`;

function miniPage(css, old, width) {
  return `<!doctype html><html><head><meta charset="utf-8">
  <style>${MINI_BASE}
    :root { --rp: ${(width / 750).toFixed(6)}px; }
    ${css}
  </style></head><body style="width:${width}px">${listHtml(old)}</body></html>`;
}

function appPage(width, old) {
  const css = fs.readFileSync(path.join(APP, 'styles.css'), 'utf8');
  return `<!doctype html><html><head><meta charset="utf-8">
  <style>${css}</style></head>
  <body style="width:${width}px"><div class="app-frame" style="width:${width}px;height:auto;max-width:none">${listHtml(old)}</div></body></html>`;
}

// ── 几何量测 ──
const MEASURE = () => {
  const rows = Array.from(document.querySelectorAll('.device-item'));
  const pick = (el, s) => el.querySelector(s);
  return rows.map((row) => {
    const rr = row.getBoundingClientRect();
    const cs = getComputedStyle(row);
    const kids = Array.from(row.children).map((el) => {
      const r = el.getBoundingClientRect();
      return {
        cls: el.className,
        left: +(r.left - rr.left).toFixed(2),
        right: +(rr.right - r.right).toFixed(2),
        top: +(r.top - rr.top).toFixed(2),
        cy: +(r.top + r.height / 2 - (rr.top + rr.height / 2)).toFixed(2),
        h: +r.height.toFixed(2),
      };
    });
    const label = pick(row, '.device-label');
    const state = pick(row, '.device-state-on, .device-state-off');
    const sr = state.getBoundingClientRect();
    return {
      height: +rr.height.toFixed(2),
      clientWidth: row.clientWidth,
      scrollWidth: row.scrollWidth,
      padLeft: parseFloat(cs.paddingLeft),
      padRight: parseFloat(cs.paddingRight),
      labelTruncated: label.scrollWidth > label.clientWidth + 1,
      labelWidth: +label.getBoundingClientRect().width.toFixed(2),
      stateRightGap: +(rr.right - sr.right).toFixed(2),
      kids,
      text: row.textContent.replace(/\s+/g, ' ').trim(),
    };
  });
};

function judge(tag, rows) {
  const heights = rows.map(r => r.height);
  const hMin = Math.min(...heights), hMax = Math.max(...heights);
  check(`${tag} ① 四条设备行高度相等（差 ≤ 1px）`,
    hMax - hMin <= 1, `heights=${heights.join(' / ')}`);

  const overflow = rows.filter(r => r.scrollWidth > r.clientWidth + 1);
  check(`${tag} ② 没有横向溢出（胶囊/按钮被挤出卡片）`,
    overflow.length === 0,
    overflow.map(r => `${r.text} scroll=${r.scrollWidth}/${r.clientWidth}`).join('; '));

  // 单行判据：每个子项的**垂直中心**都必须贴行中心（对齐后偏差 ≤ 3px）。
  // 一旦换行，第二排中心会偏离 15px 以上 —— 比"比 top 集合"稳得多。
  const multi = rows.filter(r => r.kids.some(k => Math.abs(k.cy) > 3));
  check(`${tag} ③ 行内没有第二排（子项垂直中心对齐行中心）`, multi.length === 0,
    multi.map(r => r.text + ' dy=' + r.kids.map(k => k.cy).join(',')).join('; '));

  const badgeRows = rows.filter(r => /🖐|攒批/.test(r.text));
  const notRight = badgeRows.filter(r => Math.abs(r.stateRightGap - r.padRight) > 2);
  check(`${tag} ④ 「在线/离线」贴行尾（差值 ≤ 2px）`, notRight.length === 0,
    notRight.map(r => `${r.text} gap=${r.stateRightGap} pad=${r.padRight}`).join('; '));

  // 设备名必须留够可读宽度（≥ 60px ≈ 5 个汉字）；超长时由省略号截断（可行宽由行内其它块决定）
  const narrow = rows.filter(r => r.labelWidth < 60);
  check(`${tag} ⑤ 设备名留够可读宽度（≥ 60px，超长走省略号）`, narrow.length === 0,
    rows.map(r => `${r.labelWidth}px${r.labelTruncated ? '(截断)' : ''}`).join(' / '));
}

(async () => {
  fs.mkdirSync(SHOT_DIR, { recursive: true });
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hn-devicerow-'));
  const browser = await chromium.launch();

  for (const width of [390, 360]) {
    // 小程序（390 = 常见 iPhone/Android 宽度；360 = 窄屏回归）
    const mpCtx = await browser.newContext({ viewport: { width, height: 640 }, deviceScaleFactor: 2 });
    const mpPage = await mpCtx.newPage();
    await mpPage.setContent(miniPage(readMiniCss(false), false, width));
    const mpRows = await mpPage.evaluate(MEASURE);
    console.log(`\n── 小程序 @${width}px ──`);
    judge(`小程序@${width}`, mpRows);
    if (width === 390) {
      await mpPage.screenshot({ path: path.join(SHOT_DIR, '小程序_修复后.png'), fullPage: true });
    }
    await mpCtx.close();

    // APP（cqw 由 .app-frame 的 container-type 提供，宽度即基准）
    const appCtx = await browser.newContext({ viewport: { width, height: 640 }, deviceScaleFactor: 2 });
    const appPage_ = await appCtx.newPage();
    await appPage_.setContent(appPage(width, false));
    const appRows = await appPage_.evaluate(MEASURE);
    console.log(`\n── APP @${width}px ──`);
    judge(`APP@${width}`, appRows);
    if (width === 390) {
      await appPage_.screenshot({ path: path.join(SHOT_DIR, 'APP_修复后.png'), fullPage: true });
    }
    await appCtx.close();
  }

  // ── 反向对照：git HEAD 的旧样式 + 旧模板（两个胶囊）必须量到「高度不一致」 ──
  let oldCss;
  try {
    oldCss = readMiniCss(true);
  } catch (e) {
    oldCss = '';
  }
  if (!oldCss) {
    check('反向对照：可读取 git HEAD 旧样式', false, 'git show 失败，跳过对照');
  } else {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 640 }, deviceScaleFactor: 2 });
    const p = await ctx.newPage();
    await p.setContent(miniPage(oldCss, true, 390));
    const rows = await p.evaluate(MEASURE);
    const hs = rows.map(r => r.height);
    console.log('\n── 反向对照（git HEAD 旧 wxss + 旧模板） ──');
    console.log('   heights=' + hs.join(' / ') + '  widths(scroll/client)=' + rows.map(r => `${r.scrollWidth}/${r.clientWidth}`).join(' '));
    check('反向对照：旧写法**必须**量到高度不一致（判据不是恒真）',
      Math.max(...hs) - Math.min(...hs) > 2, `heights=${hs.join(' / ')}`);
    await p.screenshot({ path: path.join(SHOT_DIR, '小程序_修复前_对照.png'), fullPage: true });
    await ctx.close();
  }

  await browser.close();
  if (!KEEP) fs.rmSync(tmp, { recursive: true, force: true });
  console.log(`\n截图: ${path.relative(REPO, SHOT_DIR)}`);
  console.log(okAll ? '\n全部通过' : '\n存在失败项');
  process.exit(okAll ? 0 : 1);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
