/* eslint-disable */
/**
 * 「自动检测末尾空白页并排除」默认开启 + 文案改口 · 验收（2026-11-09 用户要求）
 *
 * 用户原话：这个功能应该是默认开启的，且文案应该是「自动检测末尾空白页并排除」，
 * 来体现自动化去除的概念。
 *
 * 落地口径（小程序 / APP 两端同款）：
 *   · 新增文件时 `excludeBlankTail` 默认 **true**（新增文件即自动排除，不用顾客去勾）；
 *   · 标题文案统一成「自动检测末尾空白页并排除」；
 *   · 提交体仍是 `blank_tail_exclude: f.excludeBlankTail ? 1 : 0`（关掉也能显式传 0）；
 *   · 重印/回填**只认后端回显**（旧订单当时没排除就不该被改写），不套默认值；
 *   · 图片 / 只有 1 页的文件仍不出这块开关（打印端也会再判一次）。
 *
 * 用法：node tests/verify_blank_tail_default.js   （exit 0 = 全绿）
 */
const fs = require('fs');
const path = require('path');

const REPO = path.resolve(__dirname, '..', '..');
const MP_DIR = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'index');
const MP_JS = path.join(MP_DIR, 'index.js');
const MP_WXML = path.join(MP_DIR, 'index.wxml');
const MP_WXSS = path.join(MP_DIR, 'index.wxss');
const APP_WWW = path.join(REPO, 'mobile_apps', 'android_app', 'www');
const APP_PRINT = path.join(APP_WWW, 'print.js');
const APP_CSS = path.join(APP_WWW, 'styles.css');

const NEW_TITLE = '自动检测末尾空白页并排除';
const OLD_TITLE = '排除末尾空白页';

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}

const mpJs = fs.readFileSync(MP_JS, 'utf8');
const mpWxml = fs.readFileSync(MP_WXML, 'utf8');
const mpWxss = fs.readFileSync(MP_WXSS, 'utf8');
const appJs = fs.readFileSync(APP_PRINT, 'utf8');
const appCss = fs.readFileSync(APP_CSS, 'utf8');

console.log('\n=== ① 小程序：新增文件默认开启 ===');
check('①a 文件对象默认 excludeBlankTail: true',
      /excludeBlankTail:\s*true\b/.test(mpJs));
check('①b 没有残留 excludeBlankTail: false 的默认值',
      !/excludeBlankTail:\s*false\b/.test(mpJs));
check('①c 提交体按开关原样上报（关掉也能传 0）',
      /blank_tail_exclude:\s*f\.excludeBlankTail\s*\?\s*1\s*:\s*0/.test(mpJs));
check('①d 重印/回填只认后端回显，不套默认开',
      /excludeBlankTail:\s*!!\(Number\(f\.blank_tail_exclude\)\s*\|\|\s*0\)/.test(mpJs));

console.log('\n=== ② 小程序：文案与版式 ===');
check('②a 开关标题是新文案', mpWxml.includes(`>${NEW_TITLE}</text>`));
check('②b 旧文案已不在标题里', !mpWxml.includes(`>${OLD_TITLE}</text>`));
check('②c 文案加长后标题允许折行（line-height）',
      /\.toggle-title\s*\{[^}]*line-height\s*:\s*1\.\d+/.test(mpWxss));
check('②d 开关仍绑 onToggleExcludeBlankTail',
      /bindtap="onToggleExcludeBlankTail"/.test(mpWxml));
check('②e 图片 / 1 页仍不出这块开关',
      /wx:if="\{\{!item\.isImage && item\.pageCount !== 1\}\}"\s+class="toggle-block"/.test(mpWxml));

console.log('\n=== ③ APP：新增文件默认开启 ===');
check('③a 文件对象默认 excludeBlankTail: true',
      /excludeBlankTail:\s*true\b/.test(appJs));
check('③b 没有残留 excludeBlankTail: false 的默认值',
      !/excludeBlankTail:\s*false\b/.test(appJs));
check('③c 提交体按开关原样上报',
      /blank_tail_exclude:\s*f\.excludeBlankTail\s*\?\s*1\s*:\s*0/.test(appJs));

console.log('\n=== ④ APP：文案与版式 ===');
check('④a 开关标题是新文案', appJs.includes(`>${NEW_TITLE}</text>`));
check('④b 旧文案已不在标题里', !appJs.includes(`>${OLD_TITLE}</text>`));
check('④c 标题允许折行（line-height）',
      /\.file-switch-title\s*\{[^}]*line-height\s*:\s*1\.\d+/.test(appCss));
check('④d 开关仍用 data-file-sw="blank-tail"',
      /data-file-sw="blank-tail"/.test(appJs));
check('④e 点击切换仍只切布尔', /excludeBlankTail\s*=\s*!sf\.excludeBlankTail/.test(appJs));

console.log('\n=== ⑤ 旧订单回显文案保持事实口径 ===');
check('⑤a 小程序订单详情仍显示「已排除末尾空白页」',
      fs.readFileSync(path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'me', 'me.wxml'), 'utf8')
        .includes('已排除末尾空白页'));
check('⑤b APP 订单详情仍按 blank_tail_exclude 显示',
      /blank_tail_exclude[\s\S]{0,80}已排除末尾空白页/.test(
        fs.readFileSync(path.join(APP_WWW, 'me.js'), 'utf8')));

console.log('');
if (failures) {
  console.log(`❌ 有 ${failures} 条失败`);
  process.exit(1);
}
console.log('✅ 全部通过');
process.exit(0);
