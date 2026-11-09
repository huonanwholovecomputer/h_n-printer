/* eslint-disable */
/**
 * 移动端订单卡片「合计页数 / 合计份数 / 合计张数」显示口径验收
 *
 * 需求（2026-11-07 用户，附订单 HN20261106-0001 截图）：
 *   一张 .xlsx 订单，文件行明明写着「1 份 × 1 页 | 1 张 单面」，但订单卡片与展开详情里
 *   合计页数/份数/张数全是「不适用」。用户问「之前移动端这里不适用，现在应该适用了吧？」
 *
 * 口径（本脚本钉死）：
 *   ① 不再按扩展名分叉：Excel 订单一律「不适用」是页数分析还不覆盖表格年代的老口径，
 *      那时后端只给兜底 1 页、显示出来是假数；现在表格也会经本地工具回报真实页数
 *      （order_files.page_count → 后端 effective_pages/total_pages/total_sheets）。
 *   ② 数据驱动：后端给了数就显示（`1 页` / `1 份` / `1 张`）；
 *      确实未知（page_count=0 ⇒ total_* 为 0）才写「不适用」，绝不拿兜底的 1 页充数。
 *   ③ 三项各自独立判断：页数未知不影响份数（份数来自 copies，一定知道）。
 *   ④ 张数/份数在后端字段缺失时用派生值兜底（total_sheets 缺失看 totalSheetsDisplay，
 *      total_copies 缺失看 copies）。
 *   ⑤ 两条数据路径共用一份预处理：列表加载 + 轮询「结构变化 → 全量刷新」。
 *      轮询那条会整片替换 orders，派生字段不重算就会全丢（卡片三个数字变空白）。
 *   ⑥ 两端同款：微信小程序（me / user-orders）与 Android APP（www/me.js）。
 *
 * 用法：node other/tests/verify_mobile_order_totals.js     （exit 0 = 全绿）
 */
const fs = require('fs');
const path = require('path');

const REPO = path.resolve(__dirname, '..', '..');
const MP_ME_JS = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'me', 'me.js');
const MP_ME_WXML = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'me', 'me.wxml');
const MP_UO_JS = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'user-orders', 'user-orders.js');
const MP_UO_WXML = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'user-orders', 'user-orders.wxml');
const APP_ME_JS = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'me.js');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }

/* 从源码里抠出一个顶层 function 的完整文本（按大括号配对，不依赖缩进） */
function extractFunction(src, name) {
  const head = 'function ' + name + '(';
  const at = src.indexOf(head);
  if (at < 0) throw new Error('源码里找不到 ' + name);
  let i = src.indexOf('{', at);
  let depth = 0;
  for (; i < src.length; i++) {
    const ch = src[i];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return src.slice(at, i + 1);
    }
  }
  throw new Error(name + ' 的大括号没配对');
}

const meSrc = fs.readFileSync(MP_ME_JS, 'utf8');
const uoSrc = fs.readFileSync(MP_UO_JS, 'utf8');
const appSrc = fs.readFileSync(APP_ME_JS, 'utf8');
const meWxml = fs.readFileSync(MP_ME_WXML, 'utf8');
const uoWxml = fs.readFileSync(MP_UO_WXML, 'utf8');

/* ── 取样三份实现（小程序两份同名，APP 一份驼峰名） ── */
const totalsImpls = [
  { label: '小程序 me.js', name: 'orderTotalsText', fn: extractFunction(meSrc, '_orderTotalsText') },
  { label: '小程序 user-orders.js', name: 'orderTotalsText', fn: extractFunction(uoSrc, '_orderTotalsText') },
  { label: 'APP www/me.js', name: 'orderTotalsText', fn: extractFunction(appSrc, 'orderTotalsText') },
];
function compile(fnText) {
  return new Function(fnText + '\nreturn ' + fnText.slice(9, fnText.indexOf('(')) + ';')();
}

section('① 源码层面：不再按 Excel 扩展名分叉');
[['小程序 me.wxml', meWxml], ['小程序 user-orders.wxml', uoWxml]].forEach(([label, text]) => {
  check(label + ' 无「Excel 卡片固定不适用」分支',
    !/wx:if="\{\{item\.isExcel\}\}"[^>]*order-card-meta/.test(text));
  check(label + ' 卡片三项改用派生字段 statPages/statCopies/statSheets',
    /📄 \{\{item\.statPages\}\}/.test(text)
    && /📋 \{\{item\.statCopies\}\}/.test(text)
    && /🖨 \{\{item\.statSheets\}\}/.test(text));
  check(label + ' 详情三项改用派生字段',
    (text.match(/\{\{item\.statPages\}\}/g) || []).length >= 2
    && (text.match(/\{\{item\.statCopies\}\}/g) || []).length >= 2
    && (text.match(/\{\{item\.statSheets\}\}/g) || []).length >= 2);
});
[[ '小程序 me.js', meSrc ], [ '小程序 user-orders.js', uoSrc ], [ 'APP www/me.js', appSrc ]].forEach(([label, text]) => {
  check(label + ' 无 isExcel ? 不适用 的硬编码', !/isExcel\s*\?\s*'不适用'/.test(text));
});

section('② 合计文案：有数就显示');
totalsImpls.forEach(({ label, fn }) => {
  const t = compile(fn);
  // 截图里的那张单：1 个文件、1 份、1 页、单面 ⇒ 1 页 / 1 份 / 1 张
  const one = t({ total_pages: 1, total_copies: 1, total_sheets: 1 });
  check(label + ' 1 页订单 → 「1 页 / 1 份 / 1 张」',
    one.pages === '1 页' && one.copies === '1 份' && one.sheets === '1 张', JSON.stringify(one));
  const multi = t({ total_pages: 8, total_copies: 2, total_sheets: 5 });
  check(label + ' 8 页 2 份 5 张 → 原样显示',
    multi.pages === '8 页' && multi.copies === '2 份' && multi.sheets === '5 张', JSON.stringify(multi));
});

section('③ 确实未知才写「不适用」（且三项各自独立）');
totalsImpls.forEach(({ label, fn }) => {
  const t = compile(fn);
  // 页数还没分析出来（page_count=0 ⇒ total_pages/total_sheets 都是 0），但份数一定知道
  const unknown = t({ total_pages: 0, total_copies: 1, total_sheets: 0, copies: 1 });
  check(label + ' 页数未知 → 「不适用 / 1 份 / 不适用」',
    unknown.pages === '不适用' && unknown.copies === '1 份' && unknown.sheets === '不适用',
    JSON.stringify(unknown));
  const blank = t({});
  check(label + ' 全空 → 三项都不适用（不冒充 0 页）',
    blank.pages === '不适用' && blank.copies === '不适用' && blank.sheets === '不适用',
    JSON.stringify(blank));
});

section('④ 后端字段缺失时的兜底（旧后端/派生值）');
totalsImpls.forEach(({ label, fn }) => {
  const t = compile(fn);
  const fb = t({ total_pages: 3, totalSheetsDisplay: '2', copies: 2 });
  check(label + ' total_sheets 缺失 → 用 totalSheetsDisplay 兜底张数',
    fb.sheets === '2 张', JSON.stringify(fb));
  check(label + ' total_copies 缺失 → 用 copies 兜底份数',
    fb.copies === '2 份', JSON.stringify(fb));
  const nan = t({ total_pages: 'x', total_copies: null, total_sheets: undefined });
  check(label + ' 脏值不崩、按未知处理',
    nan.pages === '不适用' && nan.copies === '不适用' && nan.sheets === '不适用', JSON.stringify(nan));
});

section('⑤ 两条数据路径共用一份预处理（轮询全量刷新不能丢字段）');
[['小程序 me.js', meSrc], ['小程序 user-orders.js', uoSrc]].forEach(([label, text]) => {
  const calls = (text.match(/_normalizeOrderRow\(\s*order\s*\)/g) || []).length;
  check(label + ' _normalizeOrderRow 被加载与轮询两处调用（≥2）', calls >= 2, 'calls=' + calls);
  const rowSrc = extractFunction(text, '_normalizeOrderRow');
  check(label + ' 预处理里写了 statPages/statCopies/statSheets',
    /order\.statPages\s*=/.test(rowSrc) && /order\.statCopies\s*=/.test(rowSrc) && /order\.statSheets\s*=/.test(rowSrc));
  check(label + ' 预处理复用了 _orderTotalsText（口径只有一份）',
    /_orderTotalsText\(order\)/.test(rowSrc));
});
const appMaps = (appSrc.match(/\.map\(normalizeOrder\)/g) || []).length;
check('APP 列表加载与轮询都走 normalizeOrder（≥2）', appMaps >= 2, 'calls=' + appMaps);
const appNorm = extractFunction(appSrc, 'normalizeOrder');
check('APP normalizeOrder 写入了 statPages/statCopies/statSheets',
  /o\.statPages\s*=/.test(appNorm) && /o\.statCopies\s*=/.test(appNorm) && /o\.statSheets\s*=/.test(appNorm));

section('⑥ 小程序卡片/详情仍渲染这三项（没被顺手删掉）');
check('me.wxml 仍有三项图标与标签',
  /📄 \{\{item\.statPages\}\}/.test(meWxml)
  && meWxml.includes('合计页数') && meWxml.includes('合计份数') && meWxml.includes('合计张数'));
check('user-orders.wxml 仍有三项图标与标签',
  /📄 \{\{item\.statPages\}\}/.test(uoWxml)
  && uoWxml.includes('合计页数') && uoWxml.includes('合计份数') && uoWxml.includes('合计张数'));
check('APP 卡片仍渲染三项',
  /📄 \$\{o\.statPages\}/.test(appSrc)
  && appSrc.includes('合计页数') && appSrc.includes('合计份数') && appSrc.includes('合计张数'));

console.log('\n' + (failures === 0 ? '✅ 移动端合计页数/份数/张数口径：全部通过' : `❌ ${failures} 项失败`));
process.exit(failures === 0 ? 0 : 1);
