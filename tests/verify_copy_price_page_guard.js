/* eslint-disable */
/**
 * 「复制价格」页数未计算完成拦截 · 验收
 *
 * 背景（线上 BUG）：接管设备离线 + 上传 Word（doc/docx）时页数拿不到，后端提交订单只能
 * 按 1 页兜底算价（app.py submit_order 的 `or 1`），前端 `_calcCost` / `calcCost` 用这个
 * 假页数算出来的金额**比实收少**，却照样复制出去。md/txt/csv 同理（上传时后端默认 1 页）。
 * 设备在线但 PDF 转换/页数回报还没回来就提交，也一样是错价。
 *
 * 修法：复制价格 / 复制详细价格前校验每个文件页数是否可信——
 *   · page_count > 0 且（PDF/图片后端直接数页 → 可信；doc/docx/md/txt/csv 必须
 *     page_count_verified=1 才算可信）；
 *   · 不可信 → 不写剪贴板，改弹提示「需打印的文件中包含X类型，且页数未完成计算，价格计算无效」；
 *   · 页数在提交后才回报 → 同步进提交快照（_lastOrderResult.files），拦下自动解除、提示消失。
 *
 * 两个前端各用「真实源码 + 桩环境」跑：
 *   1) 小程序 mobile_apps/h_n_print/pages/index/index.js
 *   2) APP    mobile_apps/android_app/www/print.js
 *
 * 用法：node tests/verify_copy_price_page_guard.js   （exit 0 = 全绿）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..');
const MP_INDEX = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'index', 'index.js');
const ANDROID_PRINT = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'print.js');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}
function section(title) { console.log('\n================ ' + title + ' ================'); }

/* ============================================================
   一、微信小程序（pages/index/index.js）
   ============================================================ */
async function runMiniProgram() {
  section('小程序 pages/index/index.js');

  const src = fs.readFileSync(MP_INDEX, 'utf8');
  let pageConfig = null;
  const calls = { clipboard: [], modal: [], toast: [] };

  const sandbox = {
    console: { log() {}, warn: console.warn, error: console.error },
    JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map,
    parseInt, parseFloat, isNaN,
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (t) => clearTimeout(t),
    setInterval: (fn, ms) => setInterval(fn, ms),
    clearInterval: (t) => clearInterval(t),
    require: (p) => {
      if (p.indexOf('utils/config') >= 0) return { CONFIG: { BASE_URL: 'https://example.test' } };
      if (p.indexOf('utils/request') >= 0) {
        return { request: (opt) => { sandbox.__requests.push(opt); } };
      }
      throw new Error('unexpected require: ' + p);
    },
    Page: (cfg) => { pageConfig = cfg; },
    Component: (cfg) => { pageConfig = cfg; },
    getApp: () => ({ globalData: { isDarkMode: false, themeMode: 'light', _pageRegistry: [] } }),
    wx: {
      getStorageSync: (k) => (k === 'token' ? 'tok' : ''),
      setStorageSync() {}, removeStorageSync() {},
      setBackgroundColor() {}, switchTab() {}, hideLoading() {}, showLoading() {},
      getWindowInfo: () => ({ windowWidth: 375, windowHeight: 800 }),
      getSystemInfoSync: () => ({ windowWidth: 375, windowHeight: 800, platform: 'devtools' }),
      createSelectorQuery: () => ({
        selectAll: () => ({ boundingClientRect() { return this; } }),
        select: () => ({ boundingClientRect() { return this; } }),
        in: function () { return this; },
        exec(cb) { cb && cb([[], null]); },
      }),
      showToast: (o) => calls.toast.push(o && o.title),
      showModal: (o) => calls.modal.push({ title: o && o.title, content: o && o.content, showCancel: o && o.showCancel }),
      setClipboardData: (o) => { calls.clipboard.push(o && o.data); if (o && o.success) o.success({}); },
      uploadFile: () => ({ abort() {}, onProgressUpdate() {} }),
      nextTick: (fn) => setTimeout(fn, 0),
    },
  };
  sandbox.__requests = [];
  sandbox.global = sandbox;
  sandbox.globalThis = sandbox;

  vm.createContext(sandbox);
  try {
    vm.runInContext(src, sandbox, { filename: 'index.js' });
  } catch (e) {
    check('index.js 可在桩环境中加载', false, e.message);
    return;
  }
  check('index.js 可在桩环境中加载', true);
  if (!pageConfig || !pageConfig.methods) { check('Component({methods}) 已注册', false); return; }
  check('Component({methods}) 已注册', true);

  const page = Object.assign({}, pageConfig, pageConfig.methods);
  page.data = JSON.parse(JSON.stringify(pageConfig.data || {}));
  page.data.selectedFiles = [];
  page.data.simplexPrice = 0.3;     // 线上单价（/api/pricing 同步值）
  page.data.duplexPrice = 0.4;
  page.data.coverPagePrice = 0.1;
  page.setData = function (patch, cb) {
    Object.keys(patch || {}).forEach(k => setByPath(this.data, k, patch[k]));
    if (cb) cb();
  };
  page._uploadTimers = {};
  page._pollTimers = {};
  page._fileUidSeq = 0;
  page.getTabBar = () => null;
  page._scheduleMeasure = () => {};

  /* 提交结果构造：files 即后端 submit_order 返回的 sub_tasks */
  const subtask = (over) => Object.assign({
    file_id: 'F1', file_name: '报告.docx', copies: 1, page_count: 1,
    page_count_verified: false, page_range: '', duplex: 'on', total_price: 0,
  }, over || {});
  const setOrder = (files, opts) => {
    page._lastOrderResult = {
      success: true, order_number: 'HN20260101', files,
      data: { delivery_enabled: 0, urgency: '低', cover_page: 0 },
    };
    // 设备在线状态：claimingDevices/selectedDeviceId 与真实界面同源
    const o = opts || {};
    page.data.selectedDeviceId = o.deviceId || ''
    page.data.claimingDevices = o.devices || []
    page.data.showSuccessModal = true;
    page._refreshPriceInvalidHint();
  };
  const OFFLINE_DEVICE = { devices: [{ client_id: 'pc-1', online: false }], deviceId: 'pc-1' };
  const ONLINE_DEVICE = { devices: [{ client_id: 'pc-1', online: true }], deviceId: 'pc-1' };
  const last = (arr) => arr[arr.length - 1];
  const reset = () => { calls.clipboard.length = 0; calls.modal.length = 0; calls.toast.length = 0; };

  /* --- 1. Word 未验证 + 接管设备离线 → 拦截 --- */
  reset();
  setOrder([subtask({ file_name: '报告.docx', page_count: 1, page_count_verified: false })], OFFLINE_DEVICE);
  page.onCopyPrice();
  check('①离线+Word未验证：未写剪贴板', calls.clipboard.length === 0,
        'clipboard=' + JSON.stringify(calls.clipboard));
  check('①离线+Word未验证：弹出提示弹窗（单按钮）',
        calls.modal.length === 1 && calls.modal[0].showCancel === false,
        JSON.stringify(calls.modal));
  check('①提示含「包含Word类型」「页数未完成计算」「价格计算无效」',
        calls.modal.length === 1 && /包含Word类型/.test(calls.modal[0].content)
        && /页数未完成计算/.test(calls.modal[0].content) && /价格计算无效/.test(calls.modal[0].content),
        calls.modal.length ? calls.modal[0].content : '(无弹窗)');
  check('①提示点明接管设备离线', calls.modal.length === 1 && /接管设备当前离线/.test(calls.modal[0].content));
  check('①成功弹窗内先亮出价格无效提示',
        /包含Word类型/.test(page.data.priceInvalidHint || '') && /页数未完成计算/.test(page.data.priceInvalidHint || ''),
        page.data.priceInvalidHint);
  check('①详细价格同样被拦', (() => {
    reset();
    page.onCopyDetailPrice();
    return calls.clipboard.length === 0 && calls.modal.length === 1;
  })());

  /* --- 2. md 同理 → 拦截 --- */
  reset();
  setOrder([subtask({ file_name: 'README.md', page_count: 1, page_count_verified: false })], OFFLINE_DEVICE);
  page.onCopyPrice();
  check('②md 未验证：未写剪贴板', calls.clipboard.length === 0);
  check('②md 提示含「包含Markdown类型」',
        calls.modal.length === 1 && /包含Markdown类型/.test(calls.modal[0].content),
        calls.modal.length ? calls.modal[0].content : '(无弹窗)');

  /* --- 3. 设备在线、PDF 转换/页数未返回就提交 → 拦截 --- */
  reset();
  setOrder([subtask({ file_name: '报告.docx', page_count: 0, page_count_verified: false })], ONLINE_DEVICE);
  page.onCopyPrice();
  check('③在线+页数未返回：未写剪贴板', calls.clipboard.length === 0);
  check('③提示含Word且不误报离线',
        calls.modal.length === 1 && /包含Word类型/.test(calls.modal[0].content)
        && !/接管设备当前离线/.test(calls.modal[0].content),
        calls.modal.length ? calls.modal[0].content : '(无弹窗)');
  check('③提示给出等待指引', calls.modal.length === 1 && /请等本地打印工具完成转换/.test(calls.modal[0].content));

  /* --- 4. Word 页数已验证（离线也一样）→ 放行，且金额按真实页数 --- */
  reset();
  setOrder([subtask({ file_name: '报告.docx', page_count: 12, page_count_verified: true })], OFFLINE_DEVICE);
  page.onCopyPrice();
  check('④Word 页数已验证：写剪贴板且无弹窗',
        calls.clipboard.length === 1 && calls.modal.length === 0,
        'clipboard=' + JSON.stringify(calls.clipboard) + ' modal=' + JSON.stringify(calls.modal));
  check('④金额按 12 页双面计（¥2.40）', calls.clipboard.length === 1 && calls.clipboard[0].indexOf('¥2.40') >= 0,
        calls.clipboard[0]);
  check('④成功弹窗内价格提示已清空', page.data.priceInvalidHint === '', page.data.priceInvalidHint);

  /* --- 5. PDF 由后端直接数页 → 放行（page_count_verified 一直是 0 也算可信） --- */
  reset();
  setOrder([subtask({ file_name: 'a.pdf', page_count: 5, page_count_verified: false })], OFFLINE_DEVICE);
  page.onCopyPrice();
  check('⑤PDF 页数已知：放行', calls.clipboard.length === 1 && calls.modal.length === 0,
        'clipboard=' + JSON.stringify(calls.clipboard));
  check('⑤金额按 5 页双面计（¥1.10）', calls.clipboard.length === 1 && calls.clipboard[0].indexOf('¥1.10') >= 0,
        calls.clipboard[0]);

  /* --- 6. 图片（1 页）→ 放行 --- */
  reset();
  setOrder([subtask({ file_name: 'img.png', page_count: 1, page_count_verified: false, duplex: 'off' })], OFFLINE_DEVICE);
  page.onCopyPrice();
  check('⑥图片：放行（¥0.30）',
        calls.clipboard.length === 1 && calls.clipboard[0].indexOf('¥0.30') >= 0 && calls.modal.length === 0,
        calls.clipboard[0]);

  /* --- 7. 混合：一个 PDF 可信 + 一个 md 不可信 → 整体拦截（不能只算一半） --- */
  reset();
  setOrder([
    subtask({ file_id: 'F1', file_name: 'a.pdf', page_count: 5, page_count_verified: false }),
    subtask({ file_id: 'F2', file_name: 'notes.md', page_count: 1, page_count_verified: false }),
  ], OFFLINE_DEVICE);
  page.onCopyPrice();
  check('⑦混合订单（PDF+md）：整体拦截', calls.clipboard.length === 0 && calls.modal.length === 1);

  /* --- 8. 页数不在本地类型表、且压根没数出来 → 拦截并点名文件 --- */
  reset();
  setOrder([subtask({ file_name: 'x.pdf', page_count: 0, page_count_verified: false })], ONLINE_DEVICE);
  page.onCopyPrice();
  check('⑧页数为 0 的 PDF：拦截且提示点名文件',
        calls.clipboard.length === 0 && calls.modal.length === 1 && /x\.pdf/.test(calls.modal[0].content),
        calls.modal.length ? calls.modal[0].content : '(无弹窗)');

  /* --- 9. 提交后页数才回报（真实轮询路径）→ 拦下自动解除 --- */
  reset();
  const uid = page._nextFileUid();
  page.data.selectedFiles = [{
    _uid: uid, name: '报告.docx', fileId: 'F1', uploading: false, progress: 100, failed: false,
    copies: 1, pageRange: '', rangeLines: [{ value: '', error: '' }], duplex: 'on',
    imageOrientation: 'auto', excelWarning: false, unsupportedFormat: false, isImage: false,
    pageCount: 0, pageCountStatus: 'offline', singlePage: false, entering: false, removing: false,
  }];
  setOrder([subtask({ file_id: 'F1', file_name: '报告.docx', page_count: 0, page_count_verified: false })], OFFLINE_DEVICE);
  page.onCopyPrice();
  check('⑨页数未回报：拦下', calls.clipboard.length === 0 && calls.modal.length === 1);

  reset();
  page._startPageCountPoll(uid, 'F1');
  const pollReq = last(sandbox.__requests);
  check('⑨轮询已发起 /api/file_page/<file_id>',
        !!pollReq && String(pollReq.url).indexOf('/api/file_page/F1') >= 0,
        pollReq ? pollReq.url : '(无请求)');
  pollReq.success({ statusCode: 200, data: { success: true, page_count: 9, verified: true, printer_online: false } });
  page._stopPageCountPoll(uid);
  check('⑨页数回报写入提交快照（9 页 + verified）',
        page._lastOrderResult.files[0].page_count === 9 && page._lastOrderResult.files[0].page_count_verified === true,
        JSON.stringify(page._lastOrderResult.files[0]));
  check('⑨弹窗内价格提示随之清空', page.data.priceInvalidHint === '', page.data.priceInvalidHint);
  page.onCopyPrice();
  check('⑨页数回报后可复制，金额按 9 页双面（¥1.90）',
        calls.clipboard.length === 1 && calls.clipboard[0].indexOf('¥1.90') >= 0 && calls.modal.length === 0,
        calls.clipboard[0] || '(未复制)');
}

/* ============================================================
   二、Android App（www/print.js）
   ============================================================ */
async function runAndroid() {
  section('Android www/print.js');

  const src = fs.readFileSync(ANDROID_PRINT, 'utf8');
  const elements = {};
  const mkEl = () => ({
    innerHTML: '', textContent: '', value: '', style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    querySelector: () => null, querySelectorAll: () => [], appendChild() {},
    addEventListener() {}, removeEventListener() {}, getBoundingClientRect: () => ({ height: 0, top: 0 }),
    parentNode: null, children: [],
  });
  const calls = { clipboard: [], alert: [], toast: [] };

  const sandbox = {
    console: { log() {}, warn: console.warn, error: console.error },
    JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map,
    parseInt, parseFloat, isNaN, encodeURIComponent, decodeURIComponent,
    setTimeout, clearTimeout, setInterval, clearInterval,
    XMLHttpRequest: class { open() {} setRequestHeader() {} abort() {} send() {} },
    FormData: class { append() {} },
    createImageBitmap: async () => ({ width: 1, height: 1, close() {} }),
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
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
  /* app.js 等提供跨文件全局（与浏览器一致） */
  sandbox.state = { token: 'tok', role: 'user', userInfo: null, openid: 'o1' };
  sandbox.BASE_URL = 'https://example.test';
  sandbox.api = () => Promise.resolve({ status: 200, data: sandbox.__filePageResponse || { success: true, page_count: 0 } });
  sandbox.ensureLogin = () => Promise.resolve(true);
  sandbox.request = () => {};
  sandbox.showToast = (msg) => calls.toast.push(msg);
  sandbox.showAlert = (title, content, ok) => calls.alert.push({ title, content, ok });
  sandbox.showConfirm = () => {};
  sandbox.openModal = () => {};
  sandbox.closeModal = () => {};
  sandbox.copyText = (text) => calls.clipboard.push(text);
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
  /* app.js 的接单设备全局 */
  sandbox.claimingDevices = [];
  sandbox.selectedDeviceId = '';
  sandbox.__filePageResponse = null;

  vm.createContext(sandbox);
  try {
    vm.runInContext(src, sandbox, { filename: 'print.js' });
  } catch (e) {
    check('print.js 可在桩环境中加载', false, e.message);
    return;
  }
  check('print.js 可在桩环境中加载', true);

  const run = (code) => vm.runInContext(code, sandbox);
  const last = (arr) => arr[arr.length - 1];
  const reset = () => { calls.clipboard.length = 0; calls.alert.length = 0; calls.toast.length = 0; };

  const setOrder = (files, opts) => {
    run(`
      printState.simplexPrice = 0.3;
      printState.duplexPrice = 0.4;
      printState.coverPagePrice = 0.1;
      printState._lastOrderResult = {
        success: true, order_number: 'HN20260101', files: ${JSON.stringify(files)},
        data: { delivery_enabled: 0, urgency: '低', cover_page: 0 },
      };
      selectedDeviceId = ${JSON.stringify((opts && opts.deviceId) || '')};
      claimingDevices = ${JSON.stringify((opts && opts.devices) || [])};
      printState.selectedFiles = [];
    `);
    run('refreshPriceInvalidHint();');
  };
  const hintEl = () => elements.successPriceHint || mkEl();
  const subtask = (over) => Object.assign({
    file_id: 'F1', file_name: '报告.docx', copies: 1, page_count: 1,
    page_count_verified: false, page_range: '', duplex: 'on', total_price: 0,
  }, over || {});
  const OFFLINE_DEVICE = { devices: [{ client_id: 'pc-1', online: false }], deviceId: 'pc-1' };
  const ONLINE_DEVICE = { devices: [{ client_id: 'pc-1', online: true }], deviceId: 'pc-1' };

  /* --- 1. Word 未验证 + 接管设备离线 → 拦截 --- */
  reset();
  setOrder([subtask({ file_name: '报告.docx', page_count: 1, page_count_verified: false })], OFFLINE_DEVICE);
  run('onCopyPrice();');
  check('①离线+Word未验证：未写剪贴板', calls.clipboard.length === 0, JSON.stringify(calls.clipboard));
  check('①单按钮提示弹窗（标题「价格暂不可复制」）',
        calls.alert.length === 1 && calls.alert[0].title === '价格暂不可复制',
        JSON.stringify(calls.alert));
  check('①提示含「包含Word类型」「页数未完成计算」「价格计算无效」',
        calls.alert.length === 1 && /包含Word类型/.test(calls.alert[0].content)
        && /页数未完成计算/.test(calls.alert[0].content) && /价格计算无效/.test(calls.alert[0].content),
        calls.alert.length ? calls.alert[0].content : '(无弹窗)');
  check('①提示点明接管设备离线', calls.alert.length === 1 && /接管设备当前离线/.test(calls.alert[0].content));
  check('①成功弹窗内先亮出价格无效提示',
        /包含Word类型/.test(hintEl().textContent || '') && hintEl().style.display !== 'none',
        hintEl().textContent);
  check('①详细价格同样被拦', (() => {
    reset();
    run('onCopyDetailPrice();');
    return calls.clipboard.length === 0 && calls.alert.length === 1;
  })());

  /* --- 2. md 同理 → 拦截 --- */
  reset();
  setOrder([subtask({ file_name: 'README.md', page_count: 1, page_count_verified: false })], OFFLINE_DEVICE);
  run('onCopyPrice();');
  check('②md 未验证：未写剪贴板', calls.clipboard.length === 0);
  check('②md 提示含「包含Markdown类型」',
        calls.alert.length === 1 && /包含Markdown类型/.test(calls.alert[0].content),
        calls.alert.length ? calls.alert[0].content : '(无弹窗)');

  /* --- 3. 设备在线、页数未返回就提交 → 拦截 --- */
  reset();
  setOrder([subtask({ file_name: '报告.docx', page_count: 0, page_count_verified: false })], ONLINE_DEVICE);
  run('onCopyPrice();');
  check('③在线+页数未返回：未写剪贴板', calls.clipboard.length === 0);
  check('③提示含Word且不误报离线',
        calls.alert.length === 1 && /包含Word类型/.test(calls.alert[0].content)
        && !/接管设备当前离线/.test(calls.alert[0].content),
        calls.alert.length ? calls.alert[0].content : '(无弹窗)');

  /* --- 4. Word 页数已验证 → 放行（金额按真实页数） --- */
  reset();
  setOrder([subtask({ file_name: '报告.docx', page_count: 12, page_count_verified: true })], OFFLINE_DEVICE);
  run('onCopyPrice();');
  check('④Word 页数已验证：写剪贴板且无弹窗',
        calls.clipboard.length === 1 && calls.alert.length === 0,
        'clipboard=' + JSON.stringify(calls.clipboard));
  check('④金额按 12 页双面计（¥2.40）', calls.clipboard.length === 1 && calls.clipboard[0].indexOf('¥2.40') >= 0,
        calls.clipboard[0]);
  check('④成功弹窗内价格提示已隐藏', (hintEl().textContent || '') === '' && hintEl().style.display === 'none');

  /* --- 5. PDF → 放行 --- */
  reset();
  setOrder([subtask({ file_name: 'a.pdf', page_count: 5, page_count_verified: false })], OFFLINE_DEVICE);
  run('onCopyPrice();');
  check('⑤PDF 页数已知：放行（¥1.10）',
        calls.clipboard.length === 1 && calls.clipboard[0].indexOf('¥1.10') >= 0 && calls.alert.length === 0,
        calls.clipboard[0]);

  /* --- 6. 图片 1 页 → 放行 --- */
  reset();
  setOrder([subtask({ file_name: 'img.png', page_count: 1, page_count_verified: false, duplex: 'off' })], OFFLINE_DEVICE);
  run('onCopyPrice();');
  check('⑥图片：放行（¥0.30）',
        calls.clipboard.length === 1 && calls.clipboard[0].indexOf('¥0.30') >= 0 && calls.alert.length === 0,
        calls.clipboard[0]);

  /* --- 7. 混合订单整体拦截 --- */
  reset();
  setOrder([
    subtask({ file_id: 'F1', file_name: 'a.pdf', page_count: 5, page_count_verified: false }),
    subtask({ file_id: 'F2', file_name: 'notes.md', page_count: 1, page_count_verified: false }),
  ], OFFLINE_DEVICE);
  run('onCopyPrice();');
  check('⑦混合订单（PDF+md）：整体拦截', calls.clipboard.length === 0 && calls.alert.length === 1);

  /* --- 8. 页数为 0 的非本地类型 → 拦截并点名文件 --- */
  reset();
  setOrder([subtask({ file_name: 'x.pdf', page_count: 0, page_count_verified: false })], ONLINE_DEVICE);
  run('onCopyPrice();');
  check('⑧页数为 0 的 PDF：拦截且提示点名文件',
        calls.clipboard.length === 0 && calls.alert.length === 1 && /x\.pdf/.test(calls.alert[0].content),
        calls.alert.length ? calls.alert[0].content : '(无弹窗)');

  /* --- 9. 提交后页数才回报（真实轮询路径）→ 拦下自动解除 --- */
  reset();
  setOrder([subtask({ file_id: 'F1', file_name: '报告.docx', page_count: 0, page_count_verified: false })], OFFLINE_DEVICE);
  run(`
    printState.selectedFiles = [{
      _uid: _nextFileUid(), name: '报告.docx', fileId: 'F1', uploading: false, progress: 100,
      failed: false, copies: 1, pageRange: '', rangeLines: [{ value: '', error: '' }], duplex: 'on',
      imageOrientation: 'auto', excelWarning: false, unsupportedFormat: false, isImage: false,
      pageCount: 0, pageCountStatus: 'offline', singlePage: false, entering: false, removing: false,
    }];
  `);
  run('onCopyPrice();');
  check('⑨页数未回报：拦下', calls.clipboard.length === 0 && calls.alert.length === 1);

  reset();
  sandbox.__filePageResponse = { success: true, page_count: 9, verified: true, printer_online: false };
  run('startPageCountPoll(printState.selectedFiles[0], "F1");');
  await new Promise(r => setTimeout(r, 30));   // 等 poll() 的 await api 落地
  run('stopPageCountPoll(printState.selectedFiles[0]);');
  const patched = run('printState._lastOrderResult.files[0]');
  check('⑨页数回报写入提交快照（9 页 + verified）',
        patched.page_count === 9 && patched.page_count_verified === true, JSON.stringify(patched));
  check('⑨弹窗内价格提示随之隐藏', (hintEl().textContent || '') === '' && hintEl().style.display === 'none');
  run('onCopyPrice();');
  check('⑨页数回报后可复制，金额按 9 页双面（¥1.90）',
        calls.clipboard.length === 1 && calls.clipboard[0].indexOf('¥1.90') >= 0 && calls.alert.length === 0,
        calls.clipboard[0] || '(未复制)');
}

/* ============================================================
   三、接线核对（模板/样式/全局函数确实接上了）
   ============================================================ */
function checkWiring() {
  section('接线核对（WXML/WXSS/index.html/styles.css/app.js）');

  const read = (rel) => fs.readFileSync(path.join(REPO, rel), 'utf8');
  const mpWxml = read('mobile_apps/h_n_print/pages/index/index.wxml');
  const mpWxss = read('mobile_apps/h_n_print/pages/index/index.wxss');
  const andHtml = read('mobile_apps/android_app/www/index.html');
  const andCss = read('mobile_apps/android_app/www/styles.css');
  const appJs = read('mobile_apps/android_app/www/app.js');

  check('小程序：成功弹窗渲染 priceInvalidHint',
        /wx:if="\{\{priceInvalidHint\}\}"/.test(mpWxml));
  check('小程序：wxss 定义 .price-invalid-hint', /\.price-invalid-hint\s*\{/.test(mpWxss));
  check('小程序：两个复制按钮仍绑定复制处理器',
        /bindtap="onCopyPrice"/.test(mpWxml) && /bindtap="onCopyDetailPrice"/.test(mpWxml));

  check('APP：成功弹窗有 successPriceHint 元素',
        /id="successPriceHint"/.test(andHtml) && /price-invalid-hint/.test(andHtml));
  check('APP：styles.css 定义 .price-invalid-hint', /\.price-invalid-hint\s*\{/.test(andCss));
  check('APP：app.js 提供 showAlert（单按钮提示）', /function showAlert\(/.test(appJs));
  check('APP：showConfirm 恢复被 showAlert 隐藏的取消按钮',
        /confirmCancel'\)\.style\.display = ''/.test(appJs));
  check('APP：index.html 仍有两个复制按钮',
        /id="copyPriceBtn"/.test(andHtml) && /id="copyDetailPriceBtn"/.test(andHtml));
}

/* ============================================================
   setData 路径写入（'a[1].b' → data.a[1].b = v）
   ============================================================ */
function setByPath(obj, key, val) {
  const m = key.match(/^([A-Za-z0-9_$]+)((\[\d+\]|\.[A-Za-z0-9_$]+)*)$/);
  if (!m) { obj[key] = val; return; }
  const parts = m[2].match(/\[\d+\]|\.[A-Za-z0-9_$]+/g) || [];
  if (!parts.length) { obj[m[1]] = val; return; }
  let cur = obj[m[1]];
  if (cur === undefined || cur === null) { obj[m[1]] = cur = {}; }
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    const isLast = i === parts.length - 1;
    const k = p[0] === '[' ? Number(p.slice(1, -1)) : p.slice(1);
    if (isLast) { cur[k] = val; return; }
    cur = cur[k];
    if (cur === undefined || cur === null) { throw new Error('bad path: ' + key); }
  }
}

/* ============================================================ */
(async () => {
  checkWiring();
  await runMiniProgram();
  await runAndroid();
  console.log('\n' + '='.repeat(52));
  console.log('复制价格页数拦截验收:', failures === 0 ? '全部通过 ✅' : `存在 ${failures} 项失败 ❌`);
  process.exit(failures ? 1 : 0);
})();
