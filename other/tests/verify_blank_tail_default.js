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
 *   · 提交体：`(!f.isImage && f.pageCount !== 1 && f.excludeBlankTail) ? 1 : 0` ——
 *     图片与只有 1 页的文档没有末尾空白页可排，一律报 0（2026-11-16 用户要求）；
 *   · 重印/回填**只认后端回显**（旧订单当时没排除就不该被改写），不套默认值；
 *   · 图片 / 只有 1 页的文件不出这块开关，详情里也不显示「已排除末尾空白页」
 *     （历史单同样按 page_count > 1 挡住；打印端也会再判一次）。
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
check('①c 提交体：图片 / 1 页一律报 0，其余按开关',
      /blank_tail_exclude:\s*\(!f\.isImage && f\.pageCount !== 1 && f\.excludeBlankTail\)\s*\?\s*1\s*:\s*0/.test(mpJs));
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
check('③c 提交体：图片 / 1 页一律报 0，其余按开关',
      /blank_tail_exclude:\s*\(!f\.isImage && f\.pageCount !== 1 && f\.excludeBlankTail\)\s*\?\s*1\s*:\s*0/.test(appJs));

console.log('\n=== ④ APP：文案与版式 ===');
check('④a 开关标题是新文案', appJs.includes(`>${NEW_TITLE}</text>`));
check('④b 旧文案已不在标题里', !appJs.includes(`>${OLD_TITLE}</text>`));
check('④c 标题允许折行（line-height）',
      /\.file-switch-title\s*\{[^}]*line-height\s*:\s*1\.\d+/.test(appCss));
check('④d 开关仍用 data-file-sw="blank-tail"',
      /data-file-sw="blank-tail"/.test(appJs));
check('④e 点击切换仍只切布尔', /excludeBlankTail\s*=\s*!sf\.excludeBlankTail/.test(appJs));

console.log('\n=== ⑤ 详情展示：只有多页文档才显示「已排除」（图片 / 1 页连历史单也不显示）===');
const HNP = path.join(REPO, 'mobile_apps', 'h_n_print');
const mpMeWxml = fs.readFileSync(path.join(HNP, 'pages', 'me', 'me.wxml'), 'utf8');
const mpUoWxml = fs.readFileSync(path.join(HNP, 'pages', 'user-orders', 'user-orders.wxml'), 'utf8');
const mpMeJs = fs.readFileSync(path.join(HNP, 'pages', 'me', 'me.js'), 'utf8');
const mpCard = fs.readFileSync(path.join(HNP, 'utils', 'file_detail_card.js'), 'utf8');
const appMeJs = fs.readFileSync(path.join(APP_WWW, 'me.js'), 'utf8');
check('⑤a 小程序订单详情：blank_tail_exclude 且 page_count > 1 才显示',
      /wx:if="\{\{f\.blank_tail_exclude && f\.page_count > 1\}\}"[\s\S]{0,80}已排除末尾空白页/.test(mpMeWxml));
check('⑤b 小程序 user-orders 详情：同一口径',
      /wx:if="\{\{f\.blank_tail_exclude && f\.page_count > 1\}\}"[\s\S]{0,80}已排除末尾空白页/.test(mpUoWxml));
check('⑤c 小程序 pages/me/me.js 本地打单面板：判 page_count > 1',
      /file\.blank_tail_exclude && Number\(file\.page_count\) > 1/.test(mpMeJs));
check('⑤d 小程序 utils/file_detail_card.js：判 page_count > 1',
      /f\.blank_tail_exclude && Number\(f\.page_count\) > 1/.test(mpCard));
check('⑤e APP 订单详情文件行：判 page_count > 1',
      /blank_tail_exclude\) === 1 && Number\(f\.page_count\) > 1\)[\s\S]{0,120}已排除末尾空白页/.test(appMeJs));
check('⑤f APP「我的文件」本地打单面板：判 pages > 1',
      /blank_tail_exclude\) === 1 && pages > 1\)[\s\S]{0,80}末尾空白页/.test(appMeJs));

console.log('\n=== ⑥ 真算一遍：提交体与详情判定对样例文件的结果 ===');
function evalExpr(src, re, label) {
  const m = src.match(re);
  if (!m) { check('⑥ ' + label + '：抠不出表达式', false); return null; }
  try { return new Function('f', 'return (' + m[1] + ');'); }
  catch (err) { check('⑥ ' + label + '：表达式不可执行', false, String(err)); return null; }
}
const pay = evalExpr(mpJs, /blank_tail_exclude:\s*(\(!f\.isImage && f\.pageCount !== 1 && f\.excludeBlankTail\) \? 1 : 0)/, '小程序提交体');
if (pay) {
  check('⑥a 1 页图片（开关默认开）→ 报 0', pay({ isImage: true, pageCount: 1, excludeBlankTail: true }) === 0);
  check('⑥b 1 页文档（开关默认开）→ 报 0', pay({ isImage: false, pageCount: 1, excludeBlankTail: true }) === 0);
  check('⑥c 5 页文档（开关默认开）→ 报 1', pay({ isImage: false, pageCount: 5, excludeBlankTail: true }) === 1);
  check('⑥d 5 页文档（手关开关）→ 报 0', pay({ isImage: false, pageCount: 5, excludeBlankTail: false }) === 0);
}
const show = (f) => !!(f.blank_tail_exclude && Number(f.page_count) > 1);
check('⑥e 详情判定：1 页历史单（当年误报 1）不显示', show({ blank_tail_exclude: 1, page_count: 1 }) === false);
check('⑥f 详情判定：6 页历史单照旧显示', show({ blank_tail_exclude: 1, page_count: 6 }) === true);

console.log('');
if (failures) {
  console.log(`❌ 有 ${failures} 条失败`);
  process.exit(1);
}
console.log('✅ 全部通过');
process.exit(0);
