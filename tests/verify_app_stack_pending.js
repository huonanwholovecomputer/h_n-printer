/* eslint-disable */
/**
 * APP「离线设备也能调攒批」· 验收（2026-11 需求②）
 *
 * 后端契约（本轮已改，另一人负责）：
 *   对离线设备 POST /api/printer/stack_mode 仍返回
 *     { success: true, pending: true, message: '设备离线，已记录：上线后自动应用' }
 *   设备列表 GET /api/claiming_devices 的每台设备新增 `stack_pending`。
 *
 * 前端口径（本文件验收的就是它）：
 *   1) 离线设备的攒批开关**不再灰死**（disabled 仍由「在线 + 服务端不可切换」与「请求中」决定）；
 *   2) 响应带 pending:true ⇒ 该设备标 stack_pending，徽标旁边挂「⏳ 待应用」；
 *      徽标本身显示**目标状态**（后端 C4 的 stack_desired，缺字段回退 stack_mode）；
 *   3) 徽标三态仍互斥单行（375px 不能换行，见 app.js 里那段注释），待应用是独立角标；
 *   4) 离线不再弹「设备离线，无法远程切换」。
 *
 * 用真实 www/app.js 里抽出来的四个函数，在桩环境里跑（APP 无真浏览器可自动化）。
 * 用法：node tests/verify_app_stack_pending.js   （exit 0 = 全绿）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..');
const APP_WWW = path.join(REPO, 'mobile_apps', 'android_app', 'www');
const APP_JS = path.join(APP_WWW, 'app.js');
const APP_CSS = path.join(APP_WWW, 'styles.css');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}
function section(title) { console.log('\n================ ' + title + ' ================'); }

const appJs = fs.readFileSync(APP_JS, 'utf8');
const css = fs.readFileSync(APP_CSS, 'utf8');

/* ============================================================
   一、从真实 app.js 抽取「攒批」这一组函数
   ============================================================ */
const START = 'function normalizeDeviceStack(d) {';
const END_MARK = "  stackTogglingId = '';\n  renderDeviceList();\n}";
function extractStackBlock() {
  const start = appJs.indexOf(START);
  const endMark = appJs.indexOf(END_MARK);
  if (start < 0 || endMark < 0 || endMark <= start) return null;
  return appJs.slice(start, endMark + END_MARK.length);
}

function loadStackModule() {
  const block = extractStackBlock();
  if (!block) return null;
  const prelude = `
    let claimingDevices = [];
    let stackTogglingId = '';
    let apiCalls = [];
    let nextResponse = { data: { success: true } };
    const state = { role: 'admin' };
    const toasts = [];
    function showToast(msg, ms) { toasts.push({ msg: msg, ms: ms }); }
    function renderDeviceList() {}
    function esc(s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }
    async function api(path, opts) { apiCalls.push({ path: path, opts: opts }); return nextResponse; }
  `;
  const harness = `
    globalThis.__H = {
      setRole: (r) => { state.role = r; },
      setDevices: (list) => { claimingDevices = list; },
      getDevices: () => claimingDevices,
      setResponse: (r) => { nextResponse = r; },
      reset: () => { apiCalls.length = 0; toasts.length = 0; stackTogglingId = ''; },
      calls: () => apiCalls,
      toasts: () => toasts,
      stackBadgeHtml: stackBadgeHtml,
      stackToggleBtnHtml: stackToggleBtnHtml,
      toggleStackMode: toggleStackMode,
      isAdminRole: isAdminRole,
    };
  `;
  const sandbox = { console, Math, JSON, Date, Object, Array, String, Number, Boolean, Promise, setTimeout, clearTimeout };
  sandbox.document = { getElementById: () => null, querySelectorAll: () => [] };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(prelude + '\n' + block + '\n' + harness, sandbox, { filename: 'stack(extracted)' });
  return { H: vm.runInContext('__H', sandbox), block };
}

const badAutoDuplex = (over) => Object.assign({
  client_id: 'dev-1', label: 'HP-01', online: true,
  auto_duplex: false, stack_mode: false, stack_can_toggle: true,
  stack_busy: false, stack_pending: false,
}, over || {});

/* ============================================================
   二、徽标：pending 优先、三态互斥
   ============================================================ */
section('二、徽标 stackBadgeHtml');
const loaded = loadStackModule();
if (!loaded) {
  check('可从 app.js 抽取攒批函数组', false);
} else {
  const H = loaded.H;

  check('① 抽取到的源码含 pending 分支', /stack_pending === true/.test(loaded.block));

  const pendingBadge = H.stackBadgeHtml(badAutoDuplex({ stack_pending: true, stack_mode: false }));
  check('② pending=true 挂「⏳ 待应用」角标', /待应用/.test(pendingBadge), pendingBadge);
  check('③ 待应用是独立角标 device-pending-badge（不顶替状态胶囊）',
        /device-pending-badge/.test(pendingBadge), pendingBadge);
  check('④ pending + 期望值=false ⇒ 同时显示「攒批未开」（看得出改成了什么）',
        /攒批未开/.test(pendingBadge) && !/攒批中/.test(pendingBadge), pendingBadge);

  const pendingWhileOn = H.stackBadgeHtml(badAutoDuplex({ stack_pending: true, stack_mode: true }));
  check('⑤ pending + 期望值=true ⇒ 「攒批中」+ 待应用',
        /攒批中/.test(pendingWhileOn) && /待应用/.test(pendingWhileOn), pendingWhileOn);

  const desiredWins = H.stackBadgeHtml(badAutoDuplex({ stack_desired: true, stack_mode: false }));
  check('⑤b stack_desired 优先于 stack_mode（设备还没对齐时显示目标态）',
        /攒批中/.test(desiredWins), desiredWins);

  check('⑥ stack_mode=true → 攒批中',
        /device-state-stack"/.test(H.stackBadgeHtml(badAutoDuplex({ stack_mode: true }))));
  check('⑦ stack_mode=false → 攒批未开',
        /device-state-stack-off/.test(H.stackBadgeHtml(badAutoDuplex({ stack_mode: false }))));
  check('⑧ stack_mode=null → 需人工翻纸',
        /device-state-manual/.test(H.stackBadgeHtml(badAutoDuplex({ stack_mode: null }))));
  check('⑨ auto_duplex=true（自动双面机）不渲染攒批徽标',
        H.stackBadgeHtml(badAutoDuplex({ auto_duplex: true, stack_pending: true })) === '');

  /* ============================================================
     三、开关按钮：离线不再灰死
     ============================================================ */
  section('三、开关按钮 stackToggleBtnHtml');

  H.setRole('admin');
  const offlineBtn = H.stackToggleBtnHtml(badAutoDuplex({ online: false }));
  check('① 离线设备按钮**不**灰显（需求核心）',
        !/device-stack-btn-disabled/.test(offlineBtn), offlineBtn);
  check('② 离线设备按钮仍渲染且带 data-id',
        /device-stack-btn/.test(offlineBtn) && /data-id="dev-1"/.test(offlineBtn), offlineBtn);

  const offlineBlockedBtn = H.stackToggleBtnHtml(badAutoDuplex({ online: false, stack_can_toggle: false }));
  check('③ 离线 + 旧的 stack_can_toggle=false 也不灰显（后端会记 pending）',
        !/device-stack-btn-disabled/.test(offlineBlockedBtn), offlineBlockedBtn);

  const onlineBusyBtn = H.stackToggleBtnHtml(badAutoDuplex({ online: true, stack_can_toggle: false }));
  check('④ 在线 + 服务端不可切换 → 也不再灰显（随时可切，忙时后端记 pending）',
        !/device-stack-btn-disabled/.test(onlineBusyBtn), onlineBusyBtn);

  check('⑤ 超管（role=super_admin）也能看到开关', (H.setRole('super_admin'),
        /device-stack-btn/.test(H.stackToggleBtnHtml(badAutoDuplex({ online: false })))));
  check('⑥ 普通用户看不到开关', (H.setRole('user'),
        H.stackToggleBtnHtml(badAutoDuplex({ online: false })) === ''));
  check('⑦ 自动双面机不渲染开关', (H.setRole('admin'),
        H.stackToggleBtnHtml(badAutoDuplex({ auto_duplex: true })) === ''));
  check('⑧ 已开攒批时按钮文案为「关闭攒批」',
        /关闭攒批/.test(H.stackToggleBtnHtml(badAutoDuplex({ stack_mode: true }))));
  check('⑨ 未开时文案为「开启攒批」',
        /开启攒批/.test(H.stackToggleBtnHtml(badAutoDuplex({ stack_mode: false }))));

  /* ============================================================
     四、点击行为：离线不再前端拦下，pending 记到设备上
     ============================================================ */
  section('四、toggleStackMode 行为');

  (async () => {
    /* ① 离线设备：必须发请求，且不弹「设备离线，无法远程切换」 */
    H.reset();
    H.setRole('admin');
    H.setDevices([badAutoDuplex({ online: false })]);
    H.setResponse({ data: { success: true, pending: true, message: '设备离线，已记录：上线后自动应用' } });
    await H.toggleStackMode('dev-1');
    const calls = H.calls();
    check('① 离线设备点击确实发出请求', calls.length === 1 && calls[0].path === '/api/printer/stack_mode',
          JSON.stringify(calls.map(c => c.path)));
    check('② 请求体带 client_id / enable',
          calls.length === 1 && JSON.parse(calls[0].opts.body).client_id === 'dev-1' &&
          JSON.parse(calls[0].opts.body).enable === true,
          calls.length ? calls[0].opts.body : '');
    check('③ 不再弹「设备离线，无法远程切换」',
          !H.toasts().some(t => /设备离线，无法远程切换/.test(t.msg)),
          H.toasts().map(t => t.msg).join(' | '));
    check('④ toast 用后端文案「设备离线，已记录：上线后自动应用」',
          H.toasts().some(t => /已记录/.test(t.msg)), H.toasts().map(t => t.msg).join(' | '));

    const afterPending = H.getDevices()[0];
    check('⑤ pending 响应 ⇒ 设备标记 stack_pending=true', afterPending.stack_pending === true,
          JSON.stringify(afterPending));
    check('⑥ pending 时 stack_view 写成目标态 true', afterPending.stack_view === true,
          JSON.stringify(afterPending));
    check('⑦ pending 后徽标显示「待应用」', /待应用/.test(H.stackBadgeHtml(afterPending)));

    /* ② 再来一次：pending 态重复点击仍按当前 stack_mode 推导 enable（幂等，不是反向开关） */
    H.reset();
    H.setResponse({ data: { success: true, pending: true } });
    await H.toggleStackMode('dev-1');
    check('⑧ pending（待应用）后再点 ⇒ 按目标态取反 = enable=false（以最后一次为准）',
          H.calls().length === 1 && JSON.parse(H.calls()[0].opts.body).enable === false,
          H.calls().length ? H.calls()[0].opts.body : '');

    /* ③ 在线设备正常切换：pending 清除、stack_mode 落地 */
    H.reset();
    H.setDevices([badAutoDuplex({ online: true, stack_mode: false })]);
    H.setResponse({ data: { success: true, stack_mode: true, stack_busy: false } });
    await H.toggleStackMode('dev-1');
    const online = H.getDevices()[0];
    check('⑨ 在线成功 ⇒ stack_mode=true', online.stack_mode === true);
    check('⑩ 在线成功 ⇒ stack_pending 被清掉', online.stack_pending === false);
    check('⑪ 在线成功 ⇒ stack_busy 同步', online.stack_busy === false);
    check('⑫ 成功后徽标变「攒批中」', /攒批中/.test(H.stackBadgeHtml(online)));

    /* ④ 在线 + 服务端不可切换：也放行（需求 2），由后端记 pending */
    H.reset();
    H.setDevices([badAutoDuplex({ online: true, stack_can_toggle: false, stack_reason: '正在打印，暂不能切换' })]);
    H.setResponse({ data: { success: true, stack_pending: true, stack_desired: true, message: '打印机正忙，已记录' } });
    await H.toggleStackMode('dev-1');
    check('⑬ 在线不可切换：仍发请求（不再前端拦截）',
          H.calls().length === 1 && JSON.parse(H.calls().length ? H.calls()[0].opts.body : '{}').client_id === 'dev-1');
    check('⑭ 在线不可切换：toast 用后端文案，而不是前端编的原因',
          H.toasts().some(t => /已记录/.test(t.msg)), H.toasts().map(t => t.msg).join(' | '));

    /* ⑤ 自动双面机：仍有前置提示 */
    H.reset();
    H.setDevices([badAutoDuplex({ auto_duplex: true, online: false })]);
    await H.toggleStackMode('dev-1');
    check('⑮ 自动双面机：不发请求且提示不需要攒批',
          H.calls().length === 0 && H.toasts().some(t => /不需要攒批/.test(t.msg)),
          H.toasts().map(t => t.msg).join(' | '));

    /* ⑥ 非管理员：不发请求 */
    H.reset();
    H.setRole('user');
    H.setDevices([badAutoDuplex({ online: false })]);
    await H.toggleStackMode('dev-1');
    check('⑯ 普通用户点击不发请求', H.calls().length === 0);
    H.setRole('admin');

    /* ⑦ 请求中防重入 */
    H.reset();
    H.setDevices([badAutoDuplex({ online: false })]);
    H.setResponse({ data: { success: true, pending: true } });
    const p1 = H.toggleStackMode('dev-1');
    const p2 = H.toggleStackMode('dev-1');
    await Promise.all([p1, p2]);
    check('⑰ 请求中防重复点击（只发一次）', H.calls().length === 1, 'calls=' + H.calls().length);

    /* ============================================================
       五、样式与静态护栏
       ============================================================ */
    section('五、样式与静态护栏');
    check('① styles.css 有 .device-state-stack-pending',
          /\.device-state-stack-pending\s*\{/.test(css));
    const darkIdx = css.indexOf('.theme-dark .device-state-stack-pending');
    check('② 深色模式有对应提亮规则', darkIdx > 0);
    check('③ 待应用徽标沿用信息橙 #e67e22（与「攒批中」同色系）',
          /\.device-state-stack-pending\s*\{[^}]*#e67e22/.test(css));
    check('③b 新角标 .device-pending-badge 有独立样式（橙色、不换行）',
          /\.device-pending-badge\s*\{[^}]*e67e22/.test(css));
    check('④ app.js 里已删除「设备离线，无法远程切换」这句拦截',
          !/设备离线，无法远程切换/.test(appJs));
    check('⑤ 前端没有新增轮询/pending 定时器（pending 只靠列表返回）',
          !/_stackPendingTimer|setInterval\([^)]*stack_pending/.test(appJs));

    console.log('\n' + (failures === 0 ? '✅ 全部通过' : `❌ ${failures} 项失败`));
    process.exit(failures === 0 ? 0 : 1);
  })().catch((e) => {
    console.error('运行异常：', e);
    process.exit(1);
  });
}
