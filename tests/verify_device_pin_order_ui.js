/* eslint-disable */
/**
 * tests/verify_device_pin_order_ui.js — 「接单设备显示顺序（置顶）」· 静态 / 同构验收
 *
 * 覆盖（B8）：小程序无法无头渲染 wxss ⇒ 这一侧只做静态断言 + 与 APP CSS 的同构核对：
 *   1) 入口按钮**只在** admin / super_admin 渲染（小程序 userRole / APP isAdminRole）+ JS 兜底；
 *   2) 弹窗结构：标题「接单设备显示顺序」/ 逐行 / 置顶开关 / ↑↓ / 「完成」；
 *   3) 保存链路：POST /api/admin/device_pin_order，body 的 order 只带**置顶**的 client_id；
 *   4) 默认选中三条分支：有置顶 → pin_index===0（离线也选）；无置顶 → 第一个在线；
 *      用户手动选过且仍可用 ⇒ 不抢（_deviceChosenByUser / deviceChosenByUser）；
 *   5) 同构：新样式在两个 CSS 里都有（APP 侧 cqw，小程序侧 rpx），操作行整行宽；
 *      置顶徽标与 .device-state 同档字号/行高（设备条「每条一行、恒定高度」不破）；
 *   6) 变异自证：把角色闸门 / pin_index===0 / 操作行 width:100% 摘掉，对应断言必须转红。
 *
 * 运行：node tests/verify_device_pin_order_ui.js
 */
const fs = require('fs');
const path = require('path');

const REPO = path.resolve(__dirname, '..');
const MP = path.join(REPO, 'mobile_apps', 'h_n_print');
const APP = path.join(REPO, 'mobile_apps', 'android_app', 'www');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (detail ? '  -- ' + detail : ''));
}
const read = (p) => fs.readFileSync(p, 'utf8');

/* 取某选择器第一条规则的声明块（比 verify_modal_button_width.js 的版本多支持「选择器与声明同行」
   的单行写法，如 `.device-state { font-size: 21rpx; ... }` —— 否则比对字号时两边都是 null） */
function decl(css, selector) {
  const lines = css.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const parts = lines[i].split('{');
    const sels = parts[0].split(',').map((s) => s.trim());
    if (!sels.includes(selector)) continue;
    let body = parts.length > 1 ? parts.slice(1).join('{') : '';
    if (body.includes('}')) return body;
    for (let j = i + 1; j < lines.length; j++) {
      body += lines[j] + '\n';
      if (lines[j].includes('}')) return body;
    }
    return body;
  }
  return null;
}
const num = (declText, prop) => {
  const m = (declText || '').match(new RegExp('(?:^|[;\\s])' + prop + ':\\s*([0-9.]+)'));
  return m ? parseFloat(m[1]) : null;
};

const SOURCES = {
  wxml: read(path.join(MP, 'pages', 'index', 'index.wxml')),
  mpjs: read(path.join(MP, 'pages', 'index', 'index.js')),
  mpwxss: read(path.join(MP, 'pages', 'index', 'index.wxss')),
  html: read(path.join(APP, 'index.html')),
  appjs: read(path.join(APP, 'app.js')),
  appcss: read(path.join(APP, 'styles.css')),
};

/* ---------------- 判据函数（单独抽出来，便于变异自证） ---------------- */

/* 入口按钮：捕获 .device-order-entry 那个元素块，要求它带 admin/super_admin 的角色闸门 */
function entryHasRoleGuard(src, cls) {
  const re = new RegExp('<view([^>]*class="' + cls + '"[^>]*)>');
  const m = src.match(re);
  if (!m) return false;
  return /userRole === 'admin' \|\| userRole === 'super_admin'/.test(m[1]);
}

/* 默认选中：置顶优先（pin_index===0，离线也选）→ 否则第一个在线 → 否则第一个 */
function defaultSelectionBranches(js) {
  return /d\.pin === true && d\.pin_index === 0/.test(js)
    && /devices\.find\(d => d\.online\)/.test(js)
    && /(?:this\.)?_?deviceChosenByUser(?: !== true| !== true\))/.test(js)
    && /_deviceOrderJustSaved|deviceOrderJustSaved/.test(js);
}

/* 保存：order 只带置顶项 */
function orderOnlyPinned(js) {
  return /filter\(r => r\.pin\)\.map\(r => r\.client_id\)/.test(js);
}

/* ============================ 1) 小程序侧 ============================ */
console.log('\n--- 1) 小程序（h_n_print/pages/index）---');
check('WXML 有「显示顺序」入口，且仅 admin/super_admin 渲染',
  entryHasRoleGuard(SOURCES.wxml, 'device-order-entry'),
  (SOURCES.wxml.match(/<view[^>]*device-order-entry[^>]*>/) || ['**未找到入口**'])[0].replace(/\s+/g, ' ').slice(0, 120));
check('入口点击走 onOpenDeviceOrderSheet（view + catchtap，不用 button）',
  /catchtap="onOpenDeviceOrderSheet"/.test(SOURCES.wxml));
check('设备条里有置顶标记（仅 item.pin 时渲染）',
  /wx:if="\{\{item\.pin\}\}"[^>]*class="device-pin-badge"/.test(SOURCES.wxml));
check('弹窗标题为「接单设备显示顺序」', /接单设备显示顺序/.test(SOURCES.wxml));
check('弹窗是 .modal-mask + .modal-sheet 家族（复用既有视觉语言，遮罩带模糊/半透明兜底）',
  /class="modal-mask \{\{deviceOrderClosing/.test(SOURCES.wxml) && /class="modal-sheet device-order-sheet"/.test(SOURCES.wxml));
check('弹窗逐行渲染 deviceOrderRows（wx:for + wx:key=client_id）',
  /wx:for="\{\{deviceOrderRows\}\}"[^>]*wx:key="client_id"/.test(SOURCES.wxml.replace(/\s+/g, ' ')));
check('每行有置顶开关（onToggleDevicePin）', /catchtap="onToggleDevicePin"/.test(SOURCES.wxml));
check('每行有 ↑ / ↓（onMoveDevicePin + data-dir）',
  (SOURCES.wxml.match(/catchtap="onMoveDevicePin"/g) || []).length >= 2 && /data-dir="up"/.test(SOURCES.wxml) && /data-dir="down"/.test(SOURCES.wxml));
check('底部「完成」走 onSaveDeviceOrder', /bindtap="onSaveDeviceOrder"/.test(SOURCES.wxml) && /完成/.test(SOURCES.wxml));
check('JS 兜底角色闸门（非 admin/super_admin 直接 return）',
  /const role = this\.data\.userRole;?\s*\n?\s*if \(role !== 'admin' && role !== 'super_admin'\) return/.test(SOURCES.mpjs)
  || /role !== 'admin' && role !== 'super_admin'\) return/.test(SOURCES.mpjs));
check('调用 POST /api/admin/device_pin_order（复用既有 request 封装 + Bearer token）',
  /CONFIG\.BASE_URL \+ '\/api\/admin\/device_pin_order'/.test(SOURCES.mpjs)
  && /method: 'POST'/.test(SOURCES.mpjs));
check('保存 body 的 order 只带**置顶**的 client_id', orderOnlyPinned(SOURCES.mpjs));
check('默认选中：置顶优先（pin_index===0，不看在线）→ 否则第一个在线 → 否则第一个',
  defaultSelectionBranches(SOURCES.mpjs));
check('手动选择不抢：onSelectDevice 置 _deviceChosenByUser = true', /onSelectDevice\(e\)[\s\S]{0,600}this\._deviceChosenByUser = true/.test(SOURCES.mpjs));
check('保存成功 ⇒ 一次性标记 + 重拉列表（_deviceOrderJustSaved）',
  /this\._deviceOrderJustSaved = true/.test(SOURCES.mpjs) && /this\.loadClaimingDevices\(\)/.test(SOURCES.mpjs));
check('保存失败 ⇒ 回滚本地顺序 + 可见错误（_failDeviceOrder 重建 rows + deviceOrderError）',
  /_failDeviceOrder\(msg\)/.test(SOURCES.mpjs)
  && /deviceOrderRows: this\._buildDeviceOrderRows\(this\.data\.claimingDevices/.test(SOURCES.mpjs)
  && /deviceOrderError: msg/.test(SOURCES.mpjs));

/* ============================ 2) APP 侧 ============================ */
console.log('\n--- 2) APP（android_app/www）---');
check('index.html 有入口 #deviceOrderEntry（默认 display:none，由角色切换）',
  /id="deviceOrderEntry"/.test(SOURCES.html) && /id="deviceOrderEntry" style="display:none"/.test(SOURCES.html));
check('renderDeviceList 按 isAdminRole() 切换入口显隐',
  /orderEntry\.style\.display = isAdminRole\(\) \? '' : 'none'/.test(SOURCES.appjs));
check('index.html 有弹窗 #deviceOrderSheet（.modal-mask + .modal-sheet）',
  /id="deviceOrderSheet"/.test(SOURCES.html) && /class="modal-sheet device-order-sheet"/.test(SOURCES.html));
check('APP 弹窗标题为「接单设备显示顺序」', /接单设备显示顺序/.test(SOURCES.html));
check('APP 行内容由 renderDeviceOrderRows 生成，含置顶开关与 ↑ / ↓',
  /data-id="\$\{esc\(r\.client_id\)\}">\$\{r\.pin \? '📌 置顶' : '置顶'\}/.test(SOURCES.appjs)
  && /data-dir="up"/.test(SOURCES.appjs) && /data-dir="down"/.test(SOURCES.appjs));
check('APP 保存走 POST /api/admin/device_pin_order（复用 api 封装）',
  /api\('\/api\/admin\/device_pin_order'/.test(SOURCES.appjs));
check('APP order 只带置顶的 client_id', orderOnlyPinned(SOURCES.appjs));
check('APP 默认选中三条分支（置顶优先 / 第一个在线 / 手动不抢）', defaultSelectionBranches(SOURCES.appjs));
check('APP 手动选择置 deviceChosenByUser = true（设备条事件委托里）',
  /deviceChosenByUser = true;\s*\n?\s*selectedDeviceId = item\.dataset\.id/.test(SOURCES.appjs));
check('APP 保存失败回滚 + 可见红字（failDeviceOrder）+ 不显示成功态',
  /function failDeviceOrder\(msg\)/.test(SOURCES.appjs)
  && /deviceOrderRows = buildDeviceOrderRows\(claimingDevices\)/.test(SOURCES.appjs)
  && /err\.textContent = msg; err\.style\.display = ''/.test(SOURCES.appjs));
check('APP 置顶徽标只在 d.pin 时进设备条', /\$\{d\.pin \? '<text class="device-pin-badge">📌 置顶<\/text>' : ''\}/.test(SOURCES.appjs));

/* ==================== 3) 同构：两端样式成对存在 + 操作行整行宽 ==================== */
console.log('\n--- 3) 同构核对（rpx ⇄ cqw 成对）---');
const PAIRS = [
  ['.device-order-entry', 'title-row 入口'],
  ['.device-pin-badge', '设备条置顶徽标'],
  ['.device-order-sheet', '弹窗容器'],
  ['.device-order-row', '弹窗行'],
  ['.device-order-pin', '置顶开关'],
  ['.device-order-move', '↑ / ↓ 按钮'],
  ['.device-order-actions', '操作行'],
  ['.device-order-error', '失败提示'],
];
for (const [sel, label] of PAIRS) {
  const mp = decl(SOURCES.mpwxss, sel);
  const app = decl(SOURCES.appcss, sel);
  check('两端都有 ' + sel + '（' + label + '）', !!mp && !!app,
    (mp ? '' : '**小程序缺**') + (app ? '' : ' **APP 缺**'));
}
const mpActions = decl(SOURCES.mpwxss, '.device-order-actions') || '';
const appActions = decl(SOURCES.appcss, '.device-order-actions') || '';
check('操作行两端都显式 width:100%（弹层操作行陷阱，见 app.wxss .modal-buttons 注释）',
  /width:\s*100%/.test(mpActions) && /width:\s*100%/.test(appActions));
check('操作行没有被写成固定像素/cqw 宽（不是逐个按钮硬顶）',
  !/width:\s*[0-9]+(px|cqw|rpx)/.test(mpActions) && !/width:\s*[0-9]+(px|cqw|rpx)/.test(appActions));
check('完成按钮跟随容器（width:100%，非硬编码宽）',
  /width:\s*100%/.test(decl(SOURCES.mpwxss, '.device-order-done-btn') || '')
  && /width:\s*100%/.test(decl(SOURCES.appcss, '.device-order-done-btn') || ''));

/* 设备条「每条一行、高度恒定」：置顶徽标字号/行高必须与 .device-state 同档 */
const mpState = decl(SOURCES.mpwxss, '.device-state') || '';
const mpBadge = decl(SOURCES.mpwxss, '.device-pin-badge') || '';
const appState = decl(SOURCES.appcss, '.device-state') || '';
const appBadge = decl(SOURCES.appcss, '.device-pin-badge') || '';
check('小程序 .device-pin-badge 字号 == .device-state 字号',
  num(mpBadge, 'font-size') !== null && num(mpBadge, 'font-size') === num(mpState, 'font-size'),
  num(mpBadge, 'font-size') + ' vs ' + num(mpState, 'font-size'));
check('APP .device-pin-badge 字号（cqw）== .device-state 字号',
  num(appBadge, 'font-size') !== null && num(appBadge, 'font-size') === num(appState, 'font-size'),
  num(appBadge, 'font-size') + ' vs ' + num(appState, 'font-size'));
check('两端 .device-pin-badge 行高都是 1.3（与 .device-state 同档，不撑高设备条）',
  /line-height:\s*1\.3/.test(mpBadge) && /line-height:\s*1\.3/.test(appBadge));
check('设备条仍是 nowrap 单行（.device-item 未被改成 wrap）',
  /flex-wrap:\s*nowrap/.test(decl(SOURCES.mpwxss, '.device-item') || '')
  && /flex-wrap:\s*nowrap/.test(decl(SOURCES.appcss, '.device-item') || ''));

/* 深色模式：新元素必须有 .theme-dark 覆盖（用既有令牌，不硬编码） */
for (const [css, where] of [[SOURCES.mpwxss, '小程序'], [SOURCES.appcss, 'APP']]) {
  check(where + ' 有 .theme-dark .device-order-entry / .device-order-row 覆盖',
    /\.theme-dark \.device-order-entry\b/.test(css) && /\.theme-dark \.device-order-row\b/.test(css));
}

/* 既有「刚修好的规则」必须原样保留（本改动不碰它们） */
check('.privacy-sheet-actions 两端仍 width:100%（本轮不动）',
  /width:\s*100%/.test(decl(SOURCES.mpwxss, '.privacy-sheet-actions') || '')
  && /width:\s*100%/.test(decl(SOURCES.appcss, '.privacy-sheet-actions') || ''));
check('.privacy-retention-row 两端仍 width:100%（本轮不动）',
  /width:\s*100%/.test(decl(SOURCES.mpwxss, '.privacy-retention-row') || '')
  && /width:\s*100%/.test(decl(SOURCES.appcss, '.privacy-retention-row') || ''));

/* ==================== 4) 变异自证（断言不是空转） ==================== */
console.log('\n--- 4) 变异自证（把修复摘掉，断言必须转红）---');
const wxmlNoGuard = SOURCES.wxml.replace(/\s*wx:if="\{\{userRole === 'admin' \|\| userRole === 'super_admin'\}\}"\s*\n\s*class="device-order-entry"/, ' class="device-order-entry"');
check('摘掉入口的角色闸门后，第 1 节判据不成立', !entryHasRoleGuard(wxmlNoGuard, 'device-order-entry'));
const jsNoPin = SOURCES.mpjs.replace(/d\.pin === true && d\.pin_index === 0/, 'd.pin === true && d.pin_index === 99');
check('把 pin_index===0 改成别的值后，默认选中判据不成立', !defaultSelectionBranches(jsNoPin));
const appcssNoWidth = SOURCES.appcss.replace(/(\.device-order-actions \{[^}]*?)width:\s*100%;/, '$1');
check('摘掉 APP .device-order-actions 的 width:100% 后，判据不成立',
  !/width:\s*100%/.test(decl(appcssNoWidth, '.device-order-actions') || ''));
const jsNoPinnedFilter = SOURCES.mpjs.replace(/filter\(r => r\.pin\)\.map\(r => r\.client_id\)/g, 'map(r => r.client_id)');
check('把 order 改成「带全部设备」后，判据不成立', !orderOnlyPinned(jsNoPinnedFilter));

console.log('\n' + '='.repeat(60));
if (failures) {
  console.log('接单设备显示顺序 · 静态验收: ' + failures + ' 项失败');
  process.exit(1);
}
console.log('接单设备显示顺序 · 静态验收: 全部通过');
