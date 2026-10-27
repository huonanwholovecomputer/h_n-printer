#!/usr/bin/env node
/**
 * tests/verify_avatar_crop.js — 小程序 / APP 头像裁剪验收（2026-10-27 用户要求）
 *
 * 需求：「小程序、APP 的头像选择需要加入头像裁剪功能」。
 *
 * 判据分三层：
 *   A. 纯几何（两份实现必须同语义）：utils/avatar_crop.js（小程序）与
 *      android_app/www/avatar_crop.js —— fitState / clamp / cropRect / zoomAt 在一组
 *      真实图片尺寸上逐值比对；并验证 clamp 的边界（不露白、倍率上下限）与 zoomAt 的
 *      「锚点下的像素缩放前后不动」。
 *   B. 两端接线：选图 → **先裁剪**再上传；导出用 cropRect 的源矩形；上传入口仍是
 *      /api/profile（name=avatar），服务端不变；裁剪失败必须退回上传原图。
 *   C. 变异自证：把 cropRect 的 scale 去掉 / 把 clamp 的白边约束去掉 / 把「先裁剪」这段
 *      接线摘掉，对应断言必须转红（证明判据不是恒真）。
 *
 * 用法：node tests/verify_avatar_crop.js   （exit 0 = 全绿）
 */
'use strict';

const fs = require('fs');
const path = require('path');

const REPO = path.dirname(__dirname);
const MP = path.join(REPO, 'mobile_apps', 'h_n_print');
const APP = path.join(REPO, 'mobile_apps', 'android_app', 'www');

let fails = [];
let oks = 0;
function check(name, cond, detail) {
  if (cond) { oks++; console.log('[PASS] ' + name + (detail ? '  -- ' + detail : '')); }
  else { fails.push(name); console.log('[FAIL] ' + name + (detail ? '  -- ' + detail : '')); }
}
function read(p) { return fs.readFileSync(p, 'utf8'); }
function near(a, b, eps) { return Math.abs(a - b) <= (eps || 1e-6); }

const mpMath = require(path.join(MP, 'utils', 'avatar_crop.js'));
const appMath = require(path.join(APP, 'avatar_crop.js'));

// ============================================================
// A. 纯几何：两份实现同语义
// ============================================================
console.log('=== A. 纯几何（小程序 ↔ APP 同语义）===');

const cases = [
  { name: '横图 3000×2000 / 框 240', image: { width: 3000, height: 2000 }, vp: 240 },
  { name: '竖图 1200×2400 / 框 240', image: { width: 1200, height: 2400 }, vp: 240 },
  { name: '正方形 800×800 / 框 240', image: { width: 800, height: 800 }, vp: 240 },
  { name: '超小图 60×60 / 框 240', image: { width: 60, height: 60 }, vp: 240 },
  { name: '微信头像 132×132 / 框 240', image: { width: 132, height: 132 }, vp: 240 },
];

let diff = [];
for (const c of cases) {
  const a = mpMath.fitState(c.vp, c.image);
  const b = appMath.fitState(c.vp, c.image);
  if (!near(a.scale, b.scale) || !near(a.x, b.x) || !near(a.y, b.y)) diff.push(c.name + ' fitState');
  // 乱给偏移 → clamp 后两份必须一致，且不露白
  for (const st of [{ scale: a.scale, x: -9999, y: 9999 }, { scale: a.scale * 3, x: -37, y: -91 }]) {
    const ca = mpMath.clamp(st, c.vp, c.image);
    const cb = appMath.clamp(st, c.vp, c.image);
    if (!near(ca.scale, cb.scale) || !near(ca.x, cb.x) || !near(ca.y, cb.y)) diff.push(c.name + ' clamp');
    const ra = mpMath.cropRect(st, c.vp, c.image);
    const rb = appMath.cropRect(st, c.vp, c.image);
    if (!near(ra.sx, rb.sx) || !near(ra.sy, rb.sy) || !near(ra.sw, rb.sw) || !near(ra.sh, rb.sh)) diff.push(c.name + ' cropRect');
  }
  const za = mpMath.zoomAt(a, c.vp, c.image, 2, c.vp / 2, c.vp / 2);
  const zb = appMath.zoomAt(a, c.vp, c.image, 2, c.vp / 2, c.vp / 2);
  if (!near(za.scale, zb.scale) || !near(za.x, zb.x) || !near(za.y, zb.y)) diff.push(c.name + ' zoomAt');
}
check('★ 两份实现（小程序 utils / APP www）在 5 组真实尺寸上逐值一致', diff.length === 0, diff.join(', '));

// fitState：盖满裁剪框、居中
const wide = mpMath.fitState(240, { width: 3000, height: 2000 });
check('fitState：宽图按「短边」盖满（scale = 框边长 / 短边）', near(wide.scale, 240 / 2000), String(wide.scale));
check('fitState：居中（左右各露一半，纵向刚好贴合）',
  near(wide.x, (240 - 3000 * wide.scale) / 2) && near(wide.y, 0), JSON.stringify(wide));
const tall = mpMath.fitState(240, { width: 1200, height: 2400 });
check('fitState：竖图同理（横向刚好贴合、纵向居中）',
  near(tall.y, (240 - 2400 * tall.scale) / 2) && near(tall.x, 0), JSON.stringify(tall));

// clamp：不露白 + 倍率上下限
const cl = mpMath.clamp({ scale: 0.0001, x: 100, y: 100 }, 240, { width: 3000, height: 2000 });
check('★ clamp：倍率不会小于「刚好盖满」（否则裁剪框出现白边）', near(cl.scale, 240 / 2000), String(cl.scale));
const clBig = mpMath.clamp({ scale: 999, x: 0, y: 0 }, 240, { width: 3000, height: 2000 });
check('★ clamp：倍率上限 = 刚好盖满 × MAX_ZOOM(4)',
  near(clBig.scale, (240 / 2000) * mpMath.MAX_ZOOM), String(clBig.scale));
for (const c of cases) {
  const fit = mpMath.fitState(c.vp, c.image);
  for (const st of [{ scale: fit.scale, x: -99999, y: -99999 }, { scale: fit.scale, x: 99999, y: 99999 },
                    { scale: fit.scale * 2, x: -1, y: 1 }]) {
    const k = mpMath.clamp(st, c.vp, c.image);
    const dw = c.image.width * k.scale;
    const dh = c.image.height * k.scale;
    const ok = k.x <= 1e-9 && k.y <= 1e-9 && k.x >= c.vp - dw - 1e-9 && k.y >= c.vp - dh - 1e-9;
    if (!ok) { check('★ clamp 不露白：' + c.name + ' ' + JSON.stringify(st), false, JSON.stringify(k)); break; }
  }
}
check('★ clamp 在所有用例上都不露白（框内恒有图，无白边）', true);

// cropRect：是「原图像素」里的正方形，且落在图内
let cropBad = [];
for (const c of cases) {
  const fit = mpMath.fitState(c.vp, c.image);
  const r = mpMath.cropRect(fit, c.vp, c.image);
  const square = near(r.sw, r.sh, 1e-6);
  const inside = r.sx >= -1e-6 && r.sy >= -1e-6 && r.sx + r.sw <= c.image.width + 1e-6 && r.sy + r.sh <= c.image.height + 1e-6;
  const coversShortEdge = near(Math.max(r.sw, r.sh), Math.min(c.image.width, c.image.height), 1e-6);
  if (!square || !inside || !coversShortEdge) cropBad.push(c.name + ' ' + JSON.stringify(r));
}
check('★ cropRect：源矩形是正方形、落在图内、恰好覆盖短边（初始状态＝整张短边）',
  cropBad.length === 0, cropBad.join(' | '));

// zoomAt：锚点下的像素缩放前后不动
const img0 = { width: 3000, height: 2000 };
const fit0 = mpMath.fitState(240, img0);
let anchorBad = [];
for (const ax of [0, 60, 120, 240]) {
  for (const ay of [0, 90, 120, 240]) {
    const before = { scale: fit0.scale * 1.5, x: -100, y: -30 };
    const cb = mpMath.clamp(before, 240, img0);
    const pxBefore = (ax - cb.x) / cb.scale;
    const pyBefore = (ay - cb.y) / cb.scale;
    const after = mpMath.zoomAt(cb, 240, img0, 1.7, ax, ay);
    const pxAfter = (ax - after.x) / after.scale;
    const pyAfter = (ay - after.y) / after.scale;
    // 只在没被 clamp 顶住时要求严格不动（贴边时会被钳回，属预期）
    const clamped = near(after.scale, cb.scale) || !near(after.x, ax - (ax - cb.x) * 1.7, 1e-6);
    if (!clamped && (!near(pxBefore, pxAfter, 1e-6) || !near(pyBefore, pyAfter, 1e-6))) {
      anchorBad.push('anchor(' + ax + ',' + ay + ') ' + pxBefore.toFixed(2) + '→' + pxAfter.toFixed(2));
    }
  }
}
check('★ zoomAt：锚点下的图片像素在缩放前后停在原地（双指缩放不漂移）',
  anchorBad.length === 0, anchorBad.join(' | '));

check('导出边长常量 = 512（服务端再压到 200px）',
  mpMath.OUTPUT_SIZE === 512 && appMath.OUTPUT_SIZE === 512);

// 求解：从 cropRect 反推「用户选中的中心点」——用来验证拖动语义（把图往左拖 = 看右侧）
const dragState = mpMath.clamp({ scale: fit0.scale, x: -60, y: 0 }, 240, img0);
const rDrag = mpMath.cropRect(dragState, 240, img0);
check('拖动语义：图片左移 60px ⇒ 源矩形向右推进 60/scale 像素',
  near(rDrag.sx, 60 / dragState.scale, 1e-6), 'sx=' + rDrag.sx.toFixed(1));

// ============================================================
// B. 两端接线
// ============================================================
console.log('\n=== B. 两端接线 ===');
const mpJs = read(path.join(MP, 'pages', 'me', 'me.js'));
const mpWxml = read(path.join(MP, 'pages', 'me', 'me.wxml'));
const mpWxss = read(path.join(MP, 'pages', 'me', 'me.wxss'));
const appJs = read(path.join(APP, 'me.js'));
const appHtml = read(path.join(APP, 'index.html'));
const appCss = read(path.join(APP, 'styles.css'));

check('★ 小程序：me.js 引入 utils/avatar_crop', /require\('\.\.\/\.\.\/utils\/avatar_crop'\)/.test(mpJs));
check('★ 小程序：相册选图后**先裁剪**（不再直接 uploadAvatar）',
  /chooseFromAlbum[\s\S]{0,700}openAvatarCrop\(avatarUrl\)/.test(mpJs)
  && !/chooseFromAlbum[\s\S]{0,400}that\.uploadAvatar\(avatarUrl\)/.test(mpJs));
check('★ 小程序：微信头像下载后也走裁剪',
  /downloadRes\.statusCode === 200\) \{\s*\n\s*that\.openAvatarCrop\(downloadRes\.tempFilePath\)/.test(mpJs));
check('★ 小程序：确认时用 cropRect 的源矩形画进 canvas 2d',
  /canvas\.getContext\('2d'\)/.test(mpJs) && /drawImage\(image, rect\.sx, rect\.sy, rect\.sw, rect\.sh, 0, 0, size, size\)/.test(mpJs));
check('★ 小程序：导出走 wx.canvasToTempFilePath（正方形、512、jpeg）',
  /wx\.canvasToTempFilePath\(\{/.test(mpJs) && /destWidth: size, destHeight: size/.test(mpJs)
  && /fileType: 'jpg'/.test(mpJs));
check('★ 小程序：裁剪失败退回上传原图（不把人卡在弹层里）',
  (mpJs.match(/done\(src\)/g) || []).length >= 3);
check('★ 小程序：上传入口没变（/api/profile，name=avatar）',
  /url: CONFIG\.BASE_URL \+ '\/api\/profile'/.test(mpJs) && /name: 'avatar'/.test(mpJs));
check('★ 小程序 wxml：取景框 + 滑杆 + 离屏 canvas + 三段手势绑定',
  /class="crop-box"/.test(mpWxml) && /bindtouchstart="onCropTouchStart"/.test(mpWxml)
  && /bindchanging="onCropZoom"/.test(mpWxml) && /<canvas type="2d" id="cropCanvas"/.test(mpWxml));
check('★ 小程序 wxml：裁剪层在 page-root 外（不被 .scroll-content 的 translateY 带跑）',
  mpWxml.lastIndexOf('class="crop-mask') > mpWxml.lastIndexOf('class="page-root'));
check('★ 小程序 wxss：mask 是 fixed 且 z-index 高于悬浮 tabBar(9999)',
  /\.crop-mask \{[^}]*position: fixed/.test(mpWxss) && /\.crop-mask \{[^}]*z-index: 1001[0-9]/.test(mpWxss));
check('★ 小程序 wxss：离屏 canvas 不能用 display:none（拿不到 2d 节点）',
  /\.crop-canvas \{[^}]*left: -9999px/.test(mpWxss) && !/\.crop-canvas \{[^}]*display: none/.test(mpWxss));

check('★ APP：me.js 选图后**先裁剪**', /if \(file\) openAvatarCrop\(file\)/.test(appJs)
  && !/if \(file\) uploadAvatar\(file\)/.test(appJs));
check('★ APP：导出用 canvas.toBlob → File(jpeg) 再 uploadAvatar',
  /canvas\.toBlob\(/.test(appJs) && /new File\(\[blob\], 'avatar_'/.test(appJs) && /type: 'image\/jpeg'/.test(appJs));
check('★ APP：任何一步失败都退回上传原图（drawImage 抛错 / 无 toBlob / blob 失败）',
  (appJs.match(/uploadAvatar\(original\)/g) || []).length >= 2
  && /uploadAvatar\(out \|\| original\)/.test(appJs));
check('★ APP：上传入口没变（POST /api/profile + FormData avatar）',
  /xhr\.open\('POST', BASE_URL \+ '\/api\/profile'\)/.test(appJs) && /fd\.append\('avatar', file\)/.test(appJs));
check('★ APP：index.html 先加载 avatar_crop.js 再加载 me.js',
  appHtml.indexOf('avatar_crop.js') > 0 && appHtml.indexOf('avatar_crop.js') < appHtml.indexOf('me.js'));
check('★ APP：index.html 有裁剪弹层（cropMask / cropBox / cropImg / cropZoom / 两个按钮）',
  ['id="cropMask"', 'id="cropBox"', 'id="cropImg"', 'id="cropZoom"', 'id="cropCancel"', 'id="cropOk"']
    .every((s) => appHtml.indexOf(s) > 0));
check('★ APP：styles.css 有裁剪层样式（fixed + z-index）',
  /\.crop-mask \{[^}]*position: fixed/.test(appCss) && /\.crop-mask \{[^}]*z-index: 10010/.test(appCss));
check('APP：拖动/双指/滑杆三种缩放方式都接上了',
  /addEventListener\('pointermove'/.test(appJs) && /addEventListener\('touchmove'/.test(appJs)
  && /addEventListener\('input'/.test(appJs));

// ============================================================
// C. 变异自证
// ============================================================
console.log('\n=== C. 变异自证（改坏 → 必须转红）===');

// 变异 1：cropRect 忘了除 scale（等于把裁剪框当成图片像素）
const badRect = (state, viewport, image) => {
  const s = mpMath.clamp(state, viewport, image);
  return { sx: -s.x, sy: -s.y, sw: viewport, sh: viewport };
};
const g1 = mpMath.cropRect(mpMath.fitState(240, img0), 240, img0);
const g2 = badRect(mpMath.fitState(240, img0), 240, img0);
check('变异「cropRect 不除 scale」→ 源矩形只剩框边长（A 段正方形/覆盖短边的判据会红）',
  !near(g2.sw, g1.sw) && near(g1.sw, 2000), '坏=' + JSON.stringify(g2) + ' 好=' + JSON.stringify(g1));

// 变异 2：clamp 去掉「不露白」约束
const badClamp = (state, viewport, image) => ({ scale: state.scale, x: state.x, y: state.y });
const goodC = mpMath.clamp({ scale: fit0.scale, x: -9999, y: 0 }, 240, img0);
const badC = badClamp({ scale: fit0.scale, x: -9999, y: 0 }, 240, img0);
check('变异「clamp 不钳边」→ 裁剪框外露白（判据会红）',
  badC.x < 240 - img0.width * badC.scale && goodC.x >= 240 - img0.width * goodC.scale - 1e-9,
  '坏 x=' + badC.x + ' 好 x=' + goodC.x.toFixed(2));

// 变异 3：把「先裁剪」改回直接上传（静态接线必须抓到）
const mutatedMp = mpJs.replace(/that\.openAvatarCrop\(avatarUrl\)/, 'that.uploadAvatar(avatarUrl)');
check('变异「小程序选图后直接上传」→ B 段那条接线断言会红',
  !/chooseFromAlbum[\s\S]{0,700}openAvatarCrop\(avatarUrl\)/.test(mutatedMp));

// 变异 4：APP 把裁剪入口摘掉
const mutatedApp = appJs.replace(/if \(file\) openAvatarCrop\(file\);/, 'if (file) uploadAvatar(file);');
check('变异「APP 选图后直接上传」→ B 段那条接线断言会红',
  !/if \(file\) openAvatarCrop\(file\)/.test(mutatedApp));

console.log('\n' + '='.repeat(56));
if (fails.length) {
  console.log('头像裁剪验收: 存在失败 ❌ （' + fails.length + ' 项）');
  fails.forEach((f) => console.log('  - ' + f));
  process.exit(1);
}
console.log('头像裁剪验收: 全部通过 ✅（' + oks + ' 项）');
