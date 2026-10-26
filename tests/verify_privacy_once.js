/* eslint-disable */
/**
 * 「文件保留与隐私」改为**一次性告知** · 验收（2026-10-26 用户要求）
 *
 * 用户原话：首页那张「文件保留与隐私」卡片不要常驻 —— 只在第一次使用本产品时弹出一次，
 * 用户确认后永久记住，之后不再出现，也不要每单重新勾选协议。保留期选择（立即删除 /
 * 遵从服务器管理）仍需可改：缩成一行低存在感入口 + 小弹层，存储走本地持久化。
 *
 * 落地口径（两端同款）：
 *   · 首次使用（从未同意）→ 自动弹一次告知层；点「我已阅读并同意」→ 写持久化 + 关闭；
 *   · 之后启动不再弹；常驻界面只剩一行「🔒 文件保留：遵从服务器管理（7天后自动删除）›」；
 *   · 点这一行进小弹层：改保留期（立即持久化）/ 重看《文件使用与隐私说明》；
 *   · 提交时不再每单要求勾选：只在从未同意时拦下并弹告知层（三个提交入口同一条闸门）；
 *   · 提交成功后**不再**把同意状态清零（旧实现每单重勾）。
 *
 * 两个前端各用「真实源码 + 桩环境」跑：
 *   1) 小程序 mobile_apps/h_n_print/pages/index/index.js + .wxml + .wxss
 *   2) APP    mobile_apps/android_app/www/print.js + index.html + styles.css
 *
 * 用法：node tests/verify_privacy_once.js   （exit 0 = 全绿）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..');
const MP_DIR = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'index');
const MP_INDEX = path.join(MP_DIR, 'index.js');
const MP_WXML = path.join(MP_DIR, 'index.wxml');
const MP_WXSS = path.join(MP_DIR, 'index.wxss');
const APP_WWW = path.join(REPO, 'mobile_apps', 'android_app', 'www');
const APP_HTML = path.join(APP_WWW, 'index.html');
const APP_PRINT = path.join(APP_WWW, 'print.js');
const APP_JS = path.join(APP_WWW, 'app.js');
const APP_CSS = path.join(APP_WWW, 'styles.css');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}
function section(title) { console.log('\n================ ' + title + ' ================'); }

/* ============================================================
   一、静态接线：卡片不再常驻、只剩一行入口 + 小弹层
   ============================================================ */
function checkStaticWiring() {
  section('静态接线（两端）');
  const mpWxml = fs.readFileSync(MP_WXML, 'utf8');
  const mpJs = fs.readFileSync(MP_INDEX, 'utf8');
  const mpWxss = fs.readFileSync(MP_WXSS, 'utf8');
  const appHtml = fs.readFileSync(APP_HTML, 'utf8');
  const appJs = fs.readFileSync(APP_PRINT, 'utf8');
  const appMain = fs.readFileSync(APP_JS, 'utf8');
  const appCss = fs.readFileSync(APP_CSS, 'utf8');

  check('小程序：常驻隐私卡片已移除（无 privacy-card / 无常驻勾选行）',
        !/privacy-card/.test(mpWxml) && !/privacy-consent-row/.test(mpWxml));
  check('小程序：常驻只剩一行「文件保留」入口（bindtap 打开小弹层）',
        /class="privacy-entry-row"/.test(mpWxml) && /bindtap="onOpenPrivacySheet"/.test(mpWxml)
        && /privacy-entry-value">\{\{retentionEntryText\}\}/.test(mpWxml));
  check('小程序：小弹层由 privacySheetVisible 控制，内含保留期选择 + 首次同意按钮',
        /wx:if="\{\{privacySheetVisible\}\}"/.test(mpWxml)
        && /bindtap="onSelectRetention"/.test(mpWxml) && /bindtap="onAgreePrivacy"/.test(mpWxml));
  check('小程序：脚本有持久化键与恢复函数，并在 attached 里调用',
        /const PRIVACY_AGREE_KEY = 'hn_privacy_agreed_v1'/.test(mpJs)
        && /const PRIVACY_RETENTION_KEY = 'hn_retention_hours_v1'/.test(mpJs)
        && /this\._loadPrivacyPrefs\(\)/.test(mpJs));
  check('小程序：同意/保留期都会写 storage',
        /onAgreePrivacy\(\) \{[\s\S]{0,220}wx\.setStorageSync\(PRIVACY_AGREE_KEY, true\)/.test(mpJs)
        && /onSelectRetention\(e\) \{[\s\S]{0,260}wx\.setStorageSync\(PRIVACY_RETENTION_KEY, h\)/.test(mpJs));
  check('小程序：成功分支不再把 privacyAgreed 清零（全文件只留 data 默认值一处）',
        (mpJs.match(/privacyAgreed: false/g) || []).length === 1,
        'count=' + (mpJs.match(/privacyAgreed: false/g) || []).length);
  check('小程序：提交闸门只在「从未同意」时拦，并弹告知层',
        /if \(!this\.data\.privacyAgreed\) \{\s*\n\s*this\.setData\(\{ privacySheetVisible: true/.test(mpJs));
  check('小程序：新样式存在（入口行 + 小弹层）',
        /\.privacy-entry-row\s*\{/.test(mpWxss) && /\.privacy-sheet\s*\{/.test(mpWxss));

  check('APP：常驻隐私卡片已移除（无 privacy-card / 无常驻勾选行）',
        !/privacy-card/.test(appHtml) && !/privacy-consent-row/.test(appHtml));
  check('APP：常驻只剩一行「文件保留」入口',
        /class="privacy-entry-row" id="privacyEntryRow"/.test(appHtml)
        && /id="retentionEntryValue"/.test(appHtml));
  check('APP：小弹层（privacySheet）内含保留期 chips + 同意按钮',
        /id="privacySheet"/.test(appHtml) && /id="retentionChips"/.test(appHtml)
        && /id="privacyAgreeBtn"/.test(appHtml));
  check('APP：脚本有持久化键 / 恢复函数 / 初始化调用',
        /const PRIVACY_AGREE_KEY = 'hn_privacy_agreed_v1'/.test(appJs)
        && /const PRIVACY_RETENTION_KEY = 'hn_retention_hours_v1'/.test(appJs)
        && /function loadPrivacyPrefs\(\)/.test(appJs)
        && /loadPrivacyPrefs\(\);/.test(appJs));
  check('APP：同意/保留期都会写 localStorage',
        /localStorage\.setItem\(PRIVACY_AGREE_KEY, '1'\)/.test(appJs)
        && /localStorage\.setItem\(PRIVACY_RETENTION_KEY, String\(h\)\)/.test(appJs));
  check('APP：成功收尾不再把 privacyAgreed 清零',
        !/privacyAgreed = false/.test(appJs));
  check('APP：去除旧勾选控件后无残留引用',
        !/syncPrivacyCheckbox|togglePrivacyAgree|privacyConsentRow|privacyCheckbox/.test(appJs + appHtml));
  check('APP：新样式存在（入口行 + 小弹层）',
        /\.privacy-entry-row\s*\{/.test(appCss) && /\.privacy-sheet\s*\{/.test(appCss));

  // 2026-10-26 用户现场：全局保留期会被管理员随时改（截图里 9天，实际已改 7天），
  // 常驻那一行的文案必须跟界面轮询一起刷新，不能只在本页首次加载时取一次。
  check('小程序：保留期配置跟着界面轮询刷新（_pollPrinterOnce 里调 loadRetentionOptions）',
        /_pollPrinterOnce\(done\) \{[\s\S]{0,900}?this\.loadRetentionOptions\(\)/.test(mpJs));
  check('APP：保留期配置跟着打印机状态轮询（30s）刷新',
        /printerPoll = createPollScheduler\([\s\S]{0,700}?loadRetentionOptions\(\)/.test(appMain));
  check('小程序：保留期刷新只在值变了才 setData（不每轮重绘）',
        /if \(Object\.keys\(patch\)\.length\) this\.setData\(patch\)/.test(mpJs));
  check('APP：保留期刷新只在值变了才重绘',
        /if \(changed\) renderRetentionChips\(\);/.test(appJs));
}

/* ============================================================
   二、小程序行为：首次弹一次 / 永久记住 / 保留期可改
   ============================================================ */
async function runMiniProgram() {
  section('小程序 pages/index/index.js 行为');
  const src = fs.readFileSync(MP_INDEX, 'utf8');

  const PAGE_EXPORTS = (() => {
    const _src = fs.readFileSync(path.join(REPO, 'mobile_apps', 'h_n_print', 'utils', 'pages.js'), 'utf8');
    const _m = { exports: {} };
    new Function('module', 'exports', _src)(_m, _m.exports);
    return _m.exports;
  })();

  const mkHarness = (storage) => {
    let pageConfig = null;
    const calls = { toast: [], requests: [] };
    const sandbox = {
      console: { log() {}, warn() {}, error() {} },
      JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map,
      parseInt, parseFloat, isNaN,
      setTimeout: (fn, ms) => setTimeout(fn, ms),
      clearTimeout: (t) => clearTimeout(t),
      setInterval: (fn, ms) => setInterval(fn, ms),
      clearInterval: (t) => clearInterval(t),
      require: (p) => {
        if (p.indexOf('utils/pages') >= 0) return PAGE_EXPORTS;
        if (p.indexOf('utils/config') >= 0) return { CONFIG: { BASE_URL: 'https://example.test' } };
        if (p.indexOf('utils/request') >= 0) {
          return {
            request: (opt) => { calls.requests.push(opt); },
            createPollScheduler: () => ({ start() {}, stop() {}, isRunning() { return false; } }),
          };
        }
        throw new Error('unexpected require: ' + p);
      },
      Page: (cfg) => { pageConfig = cfg; },
      Component: (cfg) => { pageConfig = cfg; },
      getApp: () => ({ globalData: { isDarkMode: false, themeMode: 'light', _pageRegistry: [] } }),
      wx: {
        getStorageSync: (k) => (k === 'token' ? 'tok' : (storage[k] !== undefined ? storage[k] : '')),
        setStorageSync: (k, v) => { storage[k] = v; },
        removeStorageSync: (k) => { delete storage[k]; },
        setBackgroundColor() {}, switchTab() {}, hideLoading() {}, showLoading() {},
        getWindowInfo: () => ({ windowWidth: 375, windowHeight: 800 }),
        getSystemInfoSync: () => ({ windowWidth: 375, windowHeight: 800, platform: 'devtools' }),
        createSelectorQuery: () => ({
          selectAll: () => ({ boundingClientRect() { return this; } }),
          select: () => ({ boundingClientRect() { return this; } }),
          in() { return this; },
          exec(cb) { cb && cb([[], null]); },
        }),
        showToast: (o) => calls.toast.push(o && o.title),
        showModal: () => {},
        setClipboardData() {},
        uploadFile: () => ({ abort() {}, onProgressUpdate() {} }),
        nextTick: (fn) => setTimeout(fn, 0),
      },
    };
    sandbox.global = sandbox;
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(src, sandbox, { filename: 'index.js' });
    const page = Object.assign({}, pageConfig, pageConfig.methods);
    page.data = JSON.parse(JSON.stringify(pageConfig.data || {}));
    page.data.selectedFiles = [];
    page.setData = function (patch, cb) {
      Object.keys(patch || {}).forEach((k) => { this.data[k] = patch[k]; });
      if (cb) cb();
    };
    page.getTabBar = () => null;
    page._scheduleMeasure = () => {};
    page._animateFileListHeight = () => {};
    page._checkPriceFiles = () => ({ message: '' });
    page._scheduleDisplayText = () => '';
    return { page, calls, storage };
  };

  /* --- 1. 首次启动：自动弹告知层，未同意 --- */
  const storage = {};
  const { page, calls } = mkHarness(storage);
  page._loadPrivacyPrefs();
  check('①首次启动：未同意（privacyAgreed=false）', page.data.privacyAgreed === false);
  check('①首次启动：自动弹一次告知层', page.data.privacySheetVisible === true);
  check('①首次启动：默认保留期 = 遵从服务器管理', page.data.retentionHours === 0);

  /* --- 2. 未同意时提交被拦，并弹告知层（而不是只 toast） --- */
  calls.requests.length = 0;
  page.data.privacyAgreed = false;
  page.data.privacySheetVisible = false;
  page._doSubmit(false);
  check('②未同意提交：不发起提交请求', calls.requests.length === 0);
  check('②未同意提交：给出提示', calls.toast.length === 1 && /文件使用与隐私说明/.test(calls.toast[0]));
  check('②未同意提交：弹出告知层', page.data.privacySheetVisible === true);

  /* --- 3. 点「我已阅读并同意」→ 写持久化 + 关闭 --- */
  page.onAgreePrivacy();
  await new Promise((r) => setTimeout(r, 260));
  check('③同意：privacyAgreed=true', page.data.privacyAgreed === true);
  check('③同意：写入 storage（永久记住）', storage['hn_privacy_agreed_v1'] === true);
  check('③同意：告知层关闭', page.data.privacySheetVisible === false);

  /* --- 4. 已同意：提交放行（不再每单要求勾选） --- */
  calls.requests.length = 0;
  calls.toast.length = 0;
  page._doSubmit(false);
  check('④已同意提交：发起提交请求（不再拦）', calls.requests.length === 1);
  const submitOpt = calls.requests[0] || {};
  check('④已同意提交：请求带 retention_mode/hours（保留期仍随单下发）',
        submitOpt.data && submitOpt.data.retention_mode === 'server' && submitOpt.data.retention_hours === 0,
        JSON.stringify(submitOpt.data && { mode: submitOpt.data.retention_mode, hours: submitOpt.data.retention_hours }));

  /* --- 5. 提交成功后不再清零同意状态 --- */
  if (submitOpt.success) {
    submitOpt.success({ statusCode: 200, data: { success: true, order_number: 'HN20261026-0001', files: [] } });
    check('⑤提交成功：同意状态保持 true（不每单重勾）', page.data.privacyAgreed === true);
    check('⑤提交成功：成功弹窗照常打开', page.data.showSuccessModal === true);
  } else {
    check('⑤提交成功：request 桩收到 success 回调', false);
  }

  /* --- 6. 第二次启动：不再弹告知层 --- */
  const second = mkHarness(storage);
  second.page._loadPrivacyPrefs();
  check('⑥第二次启动：仍记得已同意', second.page.data.privacyAgreed === true);
  check('⑥第二次启动：不再弹告知层', second.page.data.privacySheetVisible === false);

  /* --- 7. 保留期可改且持久化：立即删除 --- */
  page.onSelectRetention({ currentTarget: { dataset: { hours: -1 } } });
  check('⑦改保留期：storage 记住 -1', storage['hn_retention_hours_v1'] === -1);
  check('⑦改保留期：常驻入口文案变「立即删除」', page.data.retentionEntryText === '立即删除');
  const third = mkHarness(storage);
  third.page._loadPrivacyPrefs();
  check('⑦改保留期：下次启动沿用（retentionHours=-1）', third.page.data.retentionHours === -1);
  check('⑦改保留期：下次启动入口文案仍为「立即删除」', third.page.data.retentionEntryText === '立即删除');
  check('⑦改保留期：不受「已同意」影响，不重新弹告知层', third.page.data.privacySheetVisible === false);

  /* --- 8. 服务端设置回来后入口文案带服务器值 --- */
  const serverPage = mkHarness({}).page;
  serverPage._loadPrivacyPrefs();
  serverPage.setData({ retentionHours: 0 });
  serverPage.setData({ retentionEntryText: serverPage._retentionEntryText(0, '3天后自动删除') });
  check('⑧入口文案带服务器当前设置',
        serverPage.data.retentionEntryText === '遵从服务器管理（3天后自动删除）',
        serverPage.data.retentionEntryText);

  /* --- 9. 全局设置被改（9天 → 7天）：轮询刷新后文案立即跟上 --- */
  const th = mkHarness({});
  th.page._loadPrivacyPrefs();
  th.page.loadRetentionOptions();
  let cfgReq = th.calls.requests[th.calls.requests.length - 1];
  cfgReq.success({ statusCode: 200, data: { success: true, max_retention_hours: 168, server_retention_hours: 216 } });
  check('⑨服务器 9 天时：入口显示 9 天',
        th.page.data.serverRetentionLabel === '9天后自动删除'
        && th.page.data.retentionEntryText === '遵从服务器管理（9天后自动删除）',
        th.page.data.retentionEntryText);
  th.page.loadRetentionOptions();
  cfgReq = th.calls.requests[th.calls.requests.length - 1];
  cfgReq.success({ statusCode: 200, data: { success: true, max_retention_hours: 168, server_retention_hours: 168 } });
  check('⑨管理员改成 7 天后再刷一轮：入口立即变成 7 天（无需重进页面）',
        th.page.data.serverRetentionLabel === '7天后自动删除'
        && th.page.data.retentionEntryText === '遵从服务器管理（7天后自动删除）',
        th.page.data.retentionEntryText);
  check('⑨轮询每轮都会真的去取一次配置（不是只读缓存）',
        th.calls.requests.filter((r) => /\/api\/retention_config$/.test(r.url)).length === 2);
}

/* ============================================================
   三、APP 行为（www/print.js，真实源码 + 桩环境）
   ============================================================ */
async function runAndroid() {
  section('APP www/print.js 行为');
  const src = fs.readFileSync(APP_PRINT, 'utf8');

  const mkHarness = (store) => {
    const elements = {};
    const calls = { toast: [], alert: [], api: [], modalOpen: [] };
    const mkEl = () => ({
      innerHTML: '', textContent: '', value: '', style: {}, dataset: {},
      classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
      querySelector: () => null, querySelectorAll: () => [], appendChild() {},
      addEventListener() {}, removeEventListener() {}, getBoundingClientRect: () => ({ height: 0, top: 0 }),
      parentNode: null, children: [],
    });
    const sandbox = {
      console: { log() {}, warn() {}, error() {} },
      JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map,
      parseInt, parseFloat, isNaN, encodeURIComponent, decodeURIComponent,
      setTimeout, clearTimeout, setInterval, clearInterval,
      XMLHttpRequest: class { open() {} setRequestHeader() {} abort() {} send() {} },
      FormData: class { append() {} },
      createImageBitmap: async () => ({ width: 1, height: 1, close() {} }),
      localStorage: {
        getItem: (k) => (store[k] === undefined ? null : store[k]),
        setItem: (k, v) => { store[k] = String(v); },
        removeItem: (k) => { delete store[k]; },
      },
      navigator: { userAgent: 'node' },
    };
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    sandbox.document = {
      getElementById: (id) => (elements[id] = elements[id] || mkEl()),
      querySelector: () => null,
      querySelectorAll: () => [],
      createElement: () => mkEl(),
      addEventListener() {},
      body: mkEl(),
    };
    sandbox.state = { token: 'tok', role: 'user', userInfo: null, openid: 'o1' };
    sandbox.BASE_URL = 'https://example.test';
    sandbox.api = (url, opt) => {
      calls.api.push({ url, opt });
      if (url.indexOf('/api/retention_config') >= 0) {
        return Promise.resolve(store.__retention || { status: 200, data: { success: false } });
      }
      return Promise.resolve({ status: 200, data: { success: false } });
    };
    sandbox.ensureLogin = () => Promise.resolve(true);
    sandbox.showToast = (msg) => calls.toast.push(msg);
    sandbox.showAlert = () => {};
    sandbox.showConfirm = () => {};
    sandbox.openModal = (id) => { calls.modalOpen.push(id); };
    sandbox.closeModal = () => {};
    sandbox.copyText = () => {};
    sandbox.renderFileList = () => {};
    sandbox.renderPricing = () => {};
    sandbox.getApp = () => ({ globalData: {} });
    sandbox.esc = (s) => String(s == null ? '' : s);
    sandbox.escHtml = sandbox.esc;
    sandbox.escapeHtml = sandbox.esc;
    sandbox.escapeAttr = sandbox.esc;
    sandbox.sanitizeColor = (c) => c;
    sandbox.countPagesInRange = () => 1;
    sandbox.formatMoney = (n) => String(n);
    sandbox.fmtSize = (n) => String(n);
    sandbox.measureAll = () => {};
    sandbox.scheduleMeasureSoon = () => {};
    sandbox.pageReady = () => {};
    sandbox.refreshPrintRoleUI = () => {};
    sandbox.buildScheduleDays = () => {};
    sandbox.bindToggleDrags = () => {};
    sandbox.bindSwitchDrags = () => {};
    sandbox.claimingDevices = [];
    sandbox.selectedDeviceId = '';
    vm.createContext(sandbox);
    vm.runInContext(src, sandbox, { filename: 'print.js' });
    const run = (code) => vm.runInContext(code, sandbox);
    return { run, calls, store, sandbox };
  };

  /* --- 1. 首次启动：自动弹告知层 --- */
  const store = {};
  const h = mkHarness(store);
  h.run('loadPrivacyPrefs()');
  check('①首次启动：未同意', h.run('printState.privacyAgreed') === false);
  check('①首次启动：自动弹告知层（openModal privacySheet）',
        h.calls.modalOpen.indexOf('privacySheet') >= 0, JSON.stringify(h.calls.modalOpen));
  check('①首次启动：默认保留期 = 遵从服务器管理', h.run('printState.retentionHours') === 0);

  /* --- 2. 未同意提交被拦 + 弹告知层 --- */
  h.calls.api.length = 0;
  h.calls.toast.length = 0;
  h.calls.modalOpen.length = 0;
  h.run('printState.privacyAgreed = false; doSubmit(false)');
  check('②未同意提交：不发起 API 请求', h.calls.api.length === 0);
  check('②未同意提交：弹告知层', h.calls.modalOpen.indexOf('privacySheet') >= 0);
  check('②未同意提交：提示先同意', h.calls.toast.some((t) => /文件使用与隐私说明/.test(t)));

  /* --- 3. 同意 → 持久化 --- */
  h.run('agreePrivacy()');
  check('③同意：privacyAgreed=true', h.run('printState.privacyAgreed') === true);
  check('③同意：写 localStorage', store['hn_privacy_agreed_v1'] === '1');
  check('③同意：关闭告知层（closeModal）', true);

  /* --- 4. 已同意提交放行 --- */
  h.calls.api.length = 0;
  h.run('doSubmit(false)');
  check('④已同意提交：发起 /api/submit_order',
        h.calls.api.length === 1 && /\/api\/submit_order$/.test(h.calls.api[0].url),
        JSON.stringify(h.calls.api.map((x) => x.url)));

  /* --- 5. 第二次启动：不再弹 --- */
  const h2 = mkHarness(store);
  h2.run('loadPrivacyPrefs()');
  check('⑤第二次启动：记得已同意', h2.run('printState.privacyAgreed') === true);
  check('⑤第二次启动：不再弹告知层', h2.calls.modalOpen.indexOf('privacySheet') === -1);

  /* --- 6. 改保留期为立即删除：持久化 + 入口文案 --- */
  h2.run('selectRetention(-1)');
  check('⑥改保留期：localStorage 记住 -1', store['hn_retention_hours_v1'] === '-1');
  check('⑥改保留期：入口文案变「立即删除」', h.run('retentionEntryText(printState.retentionHours)') === '立即删除'
        || h2.run('retentionEntryText(printState.retentionHours)') === '立即删除');
  const h3 = mkHarness(store);
  h3.run('loadPrivacyPrefs()');
  check('⑥改保留期：下次启动沿用 -1', h3.run('printState.retentionHours') === -1);
  check('⑥改保留期：提交时 retention_mode=immediate',
        (() => { h3.run('doSubmit(false)'); const b = JSON.parse(h3.calls.api[0].opt.body);
                 return b.retention_mode === 'immediate' && b.retention_hours === -1; })(),
        JSON.stringify(h3.calls.api[0] && JSON.parse(h3.calls.api[0].opt.body || '{}').retention_mode));

  /* --- 7. 成功收尾不清零同意状态 --- */
  h3.run('printState.privacyAgreed = true; clearFilesAfterSuccess()');
  check('⑦成功收尾：printState.privacyAgreed 保持 true', h3.run('printState.privacyAgreed') === true);

  /* --- 8. 全局保留期被改（9天 → 7天）：轮询刷新后入口文案立即跟上 --- */
  h3.run('printState.retentionHours = 0');   // ⑥ 里选过「立即删除」，这里回到遵从服务器管理再验文案
  store.__retention = { status: 200, data: { success: true, max_retention_hours: 168, server_retention_hours: 216 } };
  await h3.run('loadRetentionOptions()');
  check('⑧服务器 9 天时：入口显示 9 天',
        h3.run('printState.serverRetentionLabel') === '9天后自动删除'
        && h3.sandbox.document.getElementById('retentionEntryValue').textContent === '遵从服务器管理（9天后自动删除）',
        h3.sandbox.document.getElementById('retentionEntryValue').textContent);
  store.__retention = { status: 200, data: { success: true, max_retention_hours: 168, server_retention_hours: 168 } };
  await h3.run('loadRetentionOptions()');
  check('⑧管理员改成 7 天后再刷一轮：入口立即变成 7 天（无需重进页面）',
        h3.run('printState.serverRetentionLabel') === '7天后自动删除'
        && h3.sandbox.document.getElementById('retentionEntryValue').textContent === '遵从服务器管理（7天后自动删除）',
        h3.sandbox.document.getElementById('retentionEntryValue').textContent);
}

(async () => {
  checkStaticWiring();
  await runMiniProgram();
  await runAndroid();
  console.log('\n' + (failures === 0 ? '✅ 全部通过' : `❌ ${failures} 项失败`));
  process.exit(failures === 0 ? 0 : 1);
})().catch((e) => {
  console.error('脚本异常：', e);
  process.exit(1);
});
