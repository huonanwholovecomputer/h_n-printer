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
check('第一张图 URL 带 token 与 &page=1&size=p&fmt=webp（<image> 不能带头，只能拼查询串）',
  decorated.previewShots.length === 1 &&
  decorated.previewShots[0].url === 'https://api.test/api/file_preview/9?token=tok%2B9%2F1&page=1&size=p&fmt=webp',
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
check('30 页文档不再被截成 12 页（旧上限已取消，按真实页数切）', s3.shots.length === 30);
const sCap = CARD.shots('https://api.test', 't', '9', 300, 0, []);
check('安全上限 200 页（超长文档不能一次建几千个节点）', sCap.shots.length === 200);
check('只让当前页 ±1 页真的发请求', s3.shots.filter(s => s.ready).length === 2 &&
  s3.shots[0].ready && s3.shots[1].ready && !s3.shots[2].ready);
const sKeep = CARD.shots('https://api.test', 't', '9', 3, 2, s3.shots.map(s => ({ ...s, loaded: s.page === 1 })));
check('翻页时已加载的页保持 loaded（不重新拉图）', sKeep.shots[0].loaded === true &&
  sKeep.index === 2 && sKeep.shots[2].ready);

console.log('\n--- 4) 「我」页：无 file_id 不再弹「已清理」 ---');
const orderHandler = (ME_JS.match(/onOpenOrderFileDetail\(e\) \{[\s\S]{0,2600}?\n    \},/) || [''])[0];
check('抠到 onOpenOrderFileDetail', orderHandler.length > 200, orderHandler.length + ' 字符');
check('没有 file_id 时先摊本地面板，再问本地缩略图有几页（2026-11-23）',
  /if \(!fileId\) \{[\s\S]{0,400}?showLocal\('local'\)/.test(orderHandler) &&
  /showLocal\('local'\)[\s\S]{0,240}?_loadLocalThumbs\(ds\.onum/.test(orderHandler));
check('接口 403/404 后退到「副本已清理」面板，而不是只弹 toast',
  /cleaned/.test(orderHandler));
check('模板：本地单走 local_only 分支（有提示 + 参数行），其余才走预览图',
  /wx:if="\{\{f\.local_only\}\}"/.test(ME_WXML) &&
  /wx:for="\{\{f\.localRows\}\}"/.test(ME_WXML) &&
  /myfile-local-tip/.test(ME_WXML));

console.log('\n--- 5) 管理入口 pages/user-orders 复用同一份模板 ---');
check('user-orders 引入 me.wxml 的模板（<import> 只取 template，不带页面结构）',
  /<import src="\.\.\/me\/me\.wxml" \/>/.test(UO_WXML));
check('文件行可点开详情（catchtap 防冒泡收卡 + data-id/oid/findex）',
  /class="detail-file-row"[\s\S]{0,240}?catchtap="onOpenOrderFileDetail"/.test(UO_WXML) &&
  /data-id="\{\{f\.file_id\}\}"/.test(UO_WXML) && /data-oid="\{\{item\.id\}\}"/.test(UO_WXML) &&
  /data-findex="\{\{index\}\}"/.test(UO_WXML));
check('弹窗在 page-root 之外（.scroll-content 是变换层，fixed 会被裁）',
  /class="modal-mask[\s\S]{0,1200}?filedetail-sheet/.test(UO_WXML) &&
  /\n<view wx:if="\{\{fileDetailVisible\}\}"[\s\S]*?<\/view>\s*(?:<!--[\s\S]*?-->\s*)?<template is="previewViewer"/.test(UO_WXML));
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
check('全屏交给自绘预览层（双击放大 / 双指捏合 / 滑页），不再用 wx.previewImage',
  /pvCore\(\)\.open\(this, urls, base\)/.test(UO_JS) && !/wx\.previewImage/.test(UO_JS) &&
  /function pvCore\(\) \{/.test(UO_JS) &&
  /return require\('\.\.\/\.\.\/utils\/preview_viewer'\)/.test(UO_JS) &&
  ['onPvTouchStart', 'onPvTouchMove', 'onPvTouchEnd', 'onPvTouchCancel', 'onPvClose', 'onPvPrev', 'onPvNext']
    .every((n) => new RegExp('\\n    ' + n + '\\([^)]*\\)\\s*\\{').test(UO_JS)));
check('管理页同样挂全屏层模板（与「我」页同一份）',
  /<import src="\.\.\/me\/preview-viewer\.wxml" \/>/.test(UO_WXML) &&
  /<template is="previewViewer" data="\{\{pv: pv\}\}" \/>/.test(UO_WXML) &&
  /pv: \{ visible: false, url: ''/.test(UO_JS));
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
  /const onum = findOrderNumber\(fileTap\.dataset\.orderOid\)/.test(APP_JS) &&
  /if \(fid\) openOrderFileDetail\(fid, rowFile, onum\);/.test(APP_JS) &&
  /else if \(rowFile\) openLocalOrderFile\(rowFile, 'local', onum\);/.test(APP_JS) &&
  !/else showToast\('该文件已清理'\)/.test(APP_JS));
check('本地参数面板：本地面板 HTML + 打开入口（都带单号，本地单靠它找缩略图）',
  /function localFilePanelHTML\(rowFile, reason, opt\)/.test(APP_JS) &&
  /function openLocalOrderFile\(rowFile, reason, orderNumber\)/.test(APP_JS) &&
  /myfile-local-tip/.test(APP_JS) && /myfile-detail-title/.test(APP_JS));
check('403/404 与网络异常都退到「副本已清理」面板（有该行参数时）',
  /if \(rowFile\) \{ openLocalOrderFile\(rowFile, 'cleaned', orderNumber\); return; \}/.test(APP_JS));
check('管理子视图（user-orders）的文件行同样可点',
  /document\.getElementById\('userOrdersList'\)\.addEventListener\('click', \(e\) => \{[\s\S]{0,900}?data-order-file/.test(APP_JS));
check('样式：.myfile-local-tip / .myfile-detail-title 有定义',
  /\.myfile-local-tip \{/.test(APP_CSS) && /\.myfile-detail-title \{/.test(APP_CSS));

console.log('\n--- 7) 本地单的打印稿缩略图（2026-11-23 用户：略缩图还没在移动端生效） ---');
// 口径：本地单的源文件从来不上云（order_files.file_id 恒空），所以既不能走
// /api/file_preview/<id>，也不能拿 order_files.id 当键（/api/local_orders 每次上报都
// DELETE + 重插，行 id 会变）。键只能是 (订单号, 文件名)：店主那台电脑打印收尾时渲好
// 前几页回传，移动端按订单号 + 文件名取图。
const LRO = { file_name: 'a.docx', original_name: 'A 显示名.docx', copies: 1, page_count: 9, total_sheets: 5, duplex: 'on', status: 'sent', total_price: 1.9 };
const lWith = CARD.localDetail(LRO, 'local', {
  baseUrl: 'https://api.test', token: 't+1/2', orderNumber: 'HN20261123-0009', pages: 9,
});
check('有缩略图时铺 swiper：最多 6 页（详情卡不铺整单）', lWith.localShots.length === 6, lWith.localShots.length);
check('图片地址的键是 (order_number, name, page) + token 查询串，不带 file_id',
  lWith.localShots[0].url === 'https://api.test/api/local_order_thumb?order_number=HN20261123-0009&name=a.docx&page=1&token=t%2B1%2F2',
  lWith.localShots[0].url);
check('拼地址一律用 file_name，不是 original_name（回传时用的就是它，md5 才对得上）',
  lWith.localShots[0].url.indexOf('name=a.docx') > 0 && lWith.localShots[0].url.indexOf('A%20') < 0);
check('有图时文案改成「预览图是店主电脑在打印时渲染的」',
  lWith.localNote.indexOf('预览图') > 0 && lWith.localNote.indexOf('没有预览图') < 0, lWith.localNote);
check('infoUrl 指向 /api/local_order_thumb/info（先问几页再铺图）',
  CARD.localInfoUrl('https://api.test', 'HN20261123-0009', 'a.docx', 't') ===
  'https://api.test/api/local_order_thumb/info?order_number=HN20261123-0009&name=a.docx&token=t');
check('「副本已被清理」那一路永远不铺图（图在云端才谈得上清理）',
  CARD.localDetail(LRO, 'cleaned', { orderNumber: 'HN20261123-0009', pages: 9 }).localShots.length === 0);
check('没问过页数（opt.pages 缺省）⇒ 照旧只摊参数，绝不去猜',
  CARD.localDetail(LRO, 'local', { orderNumber: 'HN20261123-0009' }).localShots.length === 0);
check('模板新增本地图块：swiper + 翻页角标 + 单页失败摘掉', 
  /wx:if="\{\{f\.localShots\.length\}\}"[\s\S]{0,700}?bindchange="onLocalThumbChange"/.test(ME_WXML) &&
  /f\.localShots\.length > 1[\s\S]{0,80}?f\.localPreviewIndex \+ 1/.test(ME_WXML) &&
  /binderror="onLocalThumbError"/.test(ME_WXML));
check('「我」页处理器齐全（取页数 / 翻页 / 单页失败）',
  ['_localThumbShots', '_loadLocalThumbs', 'onLocalThumbChange', 'onLocalThumbError']
    .every(n => new RegExp('\\n    ' + n + '\\(').test(ME_JS)));
check('「我」页文件行把单号带下来（data-onum），否则本地单找不到图',
  /data-onum="\{\{item\.order_number\}\}"/.test(ME_WXML) && /_loadLocalThumbs\(ds\.onum/.test(ME_JS));
check('管理页（user-orders）同一套：单号 + 取页数 + 两个图处理器',
  /data-onum="\{\{item\.order_number\}\}"/.test(UO_WXML) &&
  /_uoLoadLocalThumbs\(ds\.onum/.test(UO_JS) &&
  ['_uoLoadLocalThumbs', 'onLocalThumbChange', 'onLocalThumbError'].every(n => new RegExp('\\n    ' + n + '\\(').test(UO_JS)));
check('管理页调 localDetail 时把 baseUrl/token 传进去（默认值不跨页面复用）',
  /fileDetailCard\.localDetail\(rowFile, reason, o\)/.test(UO_JS));
check('APP 端同款：localThumbURL / localThumbPages / 面板里有图带',
  /function localThumbURL\(orderNumber, name, page, token\)/.test(APP_JS) &&
  /function localThumbPages\(orderNumber, name\)/.test(APP_JS) &&
  /myfile-local-thumbs/.test(APP_JS) &&
  /localThumbPages\(num, name\)\.then\(\(pages\)/.test(APP_JS));
check('APP 端只有真的拿到页数才重刷面板（没图保持"只摊参数"）',
  /if \(!pages \|\| seq !== myFileDetailSeq\) return;/.test(APP_JS));
check('APP 样式：图带横向可滑、单图固定比例（不新增裸选择器覆盖既有断言）',
  /\.myfile-local-thumbs \{[\s\S]{0,200}?overflow-x: auto/.test(APP_CSS) &&
  /\.myfile-local-thumb \{[\s\S]{0,200}?object-fit: contain/.test(APP_CSS));

console.log('\n结果：通过 ' + pass + ' 条，失败 ' + fail + ' 条');
if (fail > 0) {
  console.log('❌ 订单文件详情验收未通过');
  process.exit(1);
}
console.log('全部通过 ✅（订单卡文件列表：本地单给参数面板、云端单给缩略图）');
