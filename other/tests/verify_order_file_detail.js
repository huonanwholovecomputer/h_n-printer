#!/usr/bin/env node
// verify_order_file_detail.js — 「订单卡文件列表点开详情」（2026-11-13 晚用户报的问题）
//
// 报的现象：「我的打印任务」里任意文件点开都提示「该文件已清理」。根因是本地打单的
// order_files.file_id 本来就是空（生产库 2000+ 行），云端没有这份文件 —— 但打印参数
// （份数/页数/张数/单双面/页码范围/状态）都还挂在这行上。以前只看 file_id 有没有，于是
// 一律弹「已清理」。
//
// 这份验收钉住三条口径：
//   ① 没有 file_id ⇒ 走「本地面板」（local_only + localRows），绝不弹「已清理」；
//   ② 有 file_id 但接口 403/404 ⇒ 也要退到本地面板并说明「云端副本已被清理」；
//   ③ 「管理 → 某人的任务」页（pages/user-orders）复用同一份模板与装配，不再各写一套。
//
// 跑法：node other/tests/verify_order_file_detail.js（退出码 0 才算过）

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const MP = path.join(ROOT, 'mobile_apps', 'h_n_print');
const read = (p) => fs.readFileSync(p, 'utf8');

let pass = 0;
let fail = 0;
const check = (name, cond, extra) => {
  if (cond) { pass++; console.log('[PASS] ' + name + (extra ? '  -- ' + extra : '')); }
  else { fail++; console.log('[FAIL] ' + name + (extra ? '  -- ' + extra : '')); }
};

const CARD = require(path.join(MP, 'utils', 'file_detail_card.js'));
const ME_WXML = read(path.join(MP, 'pages', 'me', 'me.wxml'));
const ME_JS = read(path.join(MP, 'pages', 'me', 'me.js'));
const UO_WXML = read(path.join(MP, 'pages', 'user-orders', 'user-orders.wxml'));
const UO_JS = read(path.join(MP, 'pages', 'user-orders', 'user-orders.js'));
const UO_WXSS = read(path.join(MP, 'pages', 'user-orders', 'user-orders.wxss'));

console.log('--- 1) 公共装配 utils/file_detail_card.js ---');
const decorated = CARD.decorate({
  file_id: '9', original_name: '申请表.docx', size: 2048, page_count: 3,
  created_at: '2026-11-08 23:18:00', has_preview: true,
  preview_url: '/api/file_preview/9', preview_pages: 3,
  order_number: 'HN20261108-0013', retention_hours: 720, in_active_order: true,
}, { baseUrl: 'https://api.test', token: 'tok+9/1' });
check('decorate 的字段名与 myfileDetailCard 模板一致',
  decorated.sizeText === '2.0 KB' && decorated.createdAtText === '2026-11-08 23:18:00' &&
  decorated.orderText === 'HN20261108-0013' && decorated.typeIcon === '📘');
check('第一张图 URL 带 token 与 &page=1（<image> 不能带头，只能拼查询串）',
  decorated.previewShots.length === 1 &&
  decorated.previewShots[0].url === 'https://api.test/api/file_preview/9?token=tok%2B9%2F1&page=1',
  decorated.previewShots.length ? decorated.previewShots[0].url : '(空)');
check('页数先用列表给的 preview_pages（再问 /info 纠偏）', decorated.previewPages === 3);
check('没有预览能力时不拼图片地址（走图标 + 提示）',
  CARD.decorate({ file_id: '10', original_name: 'a.txt', has_preview: false },
    { baseUrl: 'https://api.test', token: 't' }).previewUrl === '' &&
  CARD.decorate({ file_id: '10', original_name: 'a.txt', has_preview: false },
    { baseUrl: 'https://api.test', token: 't' }).previewTip.indexOf('Word / Excel / PPT') > 0);

console.log('\n--- 2) 本地面板（没有云端副本时展示打印参数） ---');
const local = CARD.localDetail({
  file_name: '放弃医保承诺书.doc', copies: 2, page_count: 3, total_sheets: 2,
  duplex: 'on', page_range: '1-3', status: 'sent', total_price: 1.2,
}, 'local');
check('local_only=true，模板走「无图」分支', local.local_only === true && local.localRows.length > 0);
check('本地单文案说的是"只保存在店主的电脑上"，不是"已清理"',
  local.localNote.indexOf('本地打单') > 0 && local.localNote.indexOf('清理') < 0);
check('参数齐全：份数/页数、合计张数、单双面、页码范围、打印状态、金额',
  local.localRows.some(r => r.k === '份数 / 页数' && r.v === '2 份 × 3 页') &&
  local.localRows.some(r => r.k === '合计张数' && r.v === '2 张') &&
  local.localRows.some(r => r.k === '单双面' && r.v === '双面') &&
  local.localRows.some(r => r.k === '页码范围' && r.v === '1-3') &&
  local.localRows.some(r => r.k === '打印状态' && r.v === '已完成') &&
  local.localRows.some(r => r.k === '这一行金额' && r.v === '¥1.20'));
check('云端副本已被清理时文案换成「副本已被清理」，仍然展示参数',
  CARD.localDetail({ file_name: 'a.pdf' }, 'cleaned').localNote.indexOf('已被清理') > 0);
check('source 文件名缺失也不炸（兜底 未知文件）',
  CARD.localDetail({}, 'local').original_name === '未知文件');

console.log('\n--- 3) 多页切片口径 ---');
const s3 = CARD.shots('https://api.test', 't', '9', 30, 0, []);
check('12 页上限（不能把 30 页 PDF 一次拉满）', s3.shots.length === 12);
check('只让当前页 ±1 页真的发请求', s3.shots.filter(s => s.ready).length === 2 &&
  s3.shots[0].ready && s3.shots[1].ready && !s3.shots[2].ready);
const sKeep = CARD.shots('https://api.test', 't', '9', 3, 2, s3.shots.map(s => ({ ...s, loaded: s.page === 1 })));
check('翻页时已加载的页保持 loaded（不重新拉图）', sKeep.shots[0].loaded === true &&
  sKeep.index === 2 && sKeep.shots[2].ready);

console.log('\n--- 4) 「我」页：无 file_id 不再弹「已清理」 ---');
const orderHandler = (ME_JS.match(/onOpenOrderFileDetail\(e\) \{[\s\S]{0,2600}?\n    \},/) || [''])[0];
check('抠到 onOpenOrderFileDetail', orderHandler.length > 200, orderHandler.length + ' 字符');
check('没有 file_id 时先试本地面板（_uoShowLocal/_localFileDetail 路径）',
  /if \(!fileId\) \{[\s\S]{0,200}?_orderFileAt[\s\S]{0,200}?local/.test(orderHandler) ||
  /if \(!fileId\) \{[\s\S]{0,240}?showLocal\('local'\)/.test(orderHandler));
check('接口 403/404 后退到「副本已清理」面板，而不是只弹 toast',
  /cleaned/.test(orderHandler));
check('模板：本地单走 local_only 分支（有提示 + 参数行），其余才走预览图',
  /wx:if="\{\{f\.local_only\}\}"/.test(ME_WXML) &&
  /wx:for="\{\{f\.localRows\}\}"/.test(ME_WXML) &&
  /myfile-local-tip/.test(ME_WXML));

console.log('\n--- 5) 管理入口 pages/user-orders 复用同一份模板 ---');
check('user-orders 引入 me.wxml 的模板（<import> 只取 template，不带页面结构）',
  /<import src="\.\.\/me\/me\.wxml" \/>/.test(UO_WXML));
check('文件行可点开详情（bindtap + data-id/oid/findex）',
  /class="detail-file-row"[\s\S]{0,240}?bindtap="onOpenOrderFileDetail"/.test(UO_WXML) &&
  /data-id="\{\{f\.file_id\}\}"/.test(UO_WXML) && /data-oid="\{\{item\.id\}\}"/.test(UO_WXML) &&
  /data-findex="\{\{index\}\}"/.test(UO_WXML));
check('弹窗在 page-root 之外（.scroll-content 是变换层，fixed 会被裁）',
  /class="modal-mask[\s\S]{0,1200}?filedetail-sheet/.test(UO_WXML) &&
  /\n<view wx:if="\{\{fileDetailVisible\}\}"[\s\S]*?<\/view>\s*$/.test(UO_WXML));
check('弹窗里用同一个 myfileDetailCard 模板',
  /<template is="myfileDetailCard"/.test(UO_WXML));
check('样式复用 me/myfile-detail.wxss（不再各写一份）',
  UO_WXSS.indexOf('@import "../me/myfile-detail.wxss";') === 0);
check('处理器齐全：开/关/翻页/加载/失败/全屏',
  ['onOpenOrderFileDetail', 'onCloseFileDetail', 'onMyFilePreviewChange', 'onMyFilePreviewLoad',
    'onMyFilePreviewError', 'onOpenMyFilePreview', '_prepareFilePreview', '_uoFileAt']
    .every(n => new RegExp('\\n    ' + n + '\\(').test(UO_JS)));
check('请求 /api/file_detail/<id> 带 Bearer 头；页数走 /info 纠偏',
  /\/api\/file_detail\/' \+ encodeURIComponent\(fileId\)/.test(UO_JS) &&
  /'Authorization': 'Bearer ' \+ token/.test(UO_JS) &&
  /\/api\/file_preview\/' \+ encodeURIComponent\(fid\) \+[\s\S]{0,40}?'\/info\?token='/.test(UO_JS));
check('全屏交给 wx.previewImage（原生双指缩放 + 左右翻页）',
  /wx\.previewImage\(\{ urls, current: urls\[base\] \|\| urls\[0\] \}\)/.test(UO_JS));
check('关闭走 200ms 动画后再卸载（与「我」页同节奏）',
  /fileDetailClosing: true/.test(UO_JS) && /\}, 200\)/.test(UO_JS));

const APP_DIR = path.join(ROOT, 'mobile_apps', 'android_app', 'www');
const APP_JS = read(path.join(APP_DIR, 'me.js'));
const APP_CSS = read(path.join(APP_DIR, 'styles.css'));

console.log('\n--- 6) APP 端同款（Capacitor WebView） ---');
check('文件行带单号/下标（本地单也要能定位到那行参数）',
  /data-order-oid="\$\{esc\(String\(o\.id/.test(APP_JS) &&
  /data-order-findex="\$\{fi\}"/.test(APP_JS) &&
  /\(o\.files \|\| \[\]\)\.forEach\(\(f, fi\)/.test(APP_JS));
check('点击文件行：有 file_id 走详情弹窗，没有走本地面板（不再 toast「该文件已清理」）',
  /const rowFile = findOrderFile\(fileTap\.dataset\.orderOid, fileTap\.dataset\.orderFindex\)/.test(APP_JS) &&
  /if \(fid\) openOrderFileDetail\(fid, rowFile\);/.test(APP_JS) &&
  /else if \(rowFile\) openLocalOrderFile\(rowFile, 'local'\);/.test(APP_JS) &&
  !/else showToast\('该文件已清理'\)/.test(APP_JS));
check('本地参数面板：本地面板 HTML + 打开入口',
  /function localFilePanelHTML\(rowFile, reason\)/.test(APP_JS) &&
  /function openLocalOrderFile\(rowFile, reason\)/.test(APP_JS) &&
  /myfile-local-tip/.test(APP_JS) && /myfile-detail-title/.test(APP_JS));
check('403/404 与网络异常都退到「副本已清理」面板（有该行参数时）',
  /if \(rowFile\) \{ openLocalOrderFile\(rowFile, 'cleaned'\); return; \}/.test(APP_JS));
check('管理子视图（user-orders）的文件行同样可点',
  /document\.getElementById\('userOrdersList'\)\.addEventListener\('click', \(e\) => \{[\s\S]{0,900}?data-order-file/.test(APP_JS));
check('样式：.myfile-local-tip / .myfile-detail-title 有定义',
  /\.myfile-local-tip \{/.test(APP_CSS) && /\.myfile-detail-title \{/.test(APP_CSS));

console.log('\n结果：通过 ' + pass + ' 条，失败 ' + fail + ' 条');
if (fail > 0) {
  console.log('❌ 订单文件详情验收未通过');
  process.exit(1);
}
console.log('全部通过 ✅（订单卡文件列表：本地单给参数面板、云端单给缩略图）');
