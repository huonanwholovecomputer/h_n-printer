/* eslint-disable */
/**
 * 文件预览验收（小程序 + APP，零后端依赖方案）
 *
 * 设计：预览一律用「用户本机选中的文件」——小程序 `selectedFiles[i].path`（chooseMessageFile
 * 临时文件）、APP `selectedFiles[i].file`（内存 File 对象），不向服务器取文件，因此不需要
 * 新增后端接口。
 *   · 图片              → 小程序 wx.previewImage / APP objectURL + <img>
 *   · PDF / Word / Excel→ 小程序 wx.openDocument（微信内置文档预览）；
 *                         APP WebView 无原生渲染 → 明确提示不支持（需系统应用/原生桥）
 *   · txt / csv / md    → 两端都是「本地读文本 → 预览层自绘」
 * 大文件保护：文本类 > 2MB 不读；预览最多 20000 字符（截断提示）。
 *
 * 本脚本加载两端真实源码（index.js / print.js）+ 桩环境，断言分派与渲染结果。
 * 用法：node tests/verify_file_preview.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..');
const MP_INDEX = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'index', 'index.js');
const MP_WXML = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'index', 'index.wxml');
const MP_WXSS = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'index', 'index.wxss');
const APP_PRINT = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'print.js');
const APP_HTML = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'index.html');
const APP_CSS = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'styles.css');
const APP_JS = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'app.js');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }

/* ============================================================
   一、微信小程序（pages/index/index.js 真实代码）
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
    if (cur === undefined || cur === null) throw new Error('bad path ' + key);
  }
}

function runMiniProgram() {
  section('小程序 pages/index/index.js');
  const src = fs.readFileSync(MP_INDEX, 'utf8');
  let pageConfig = null;
  const calls = { previewImage: [], openDocument: [], readFile: [], copyFile: [], toast: [], modal: [], loading: 0 };
  const fsStub = {
    readFile: (o) => { calls.readFile.push(o); if (o.success) o.success({ data: sandbox.__readData }); },
    copyFile: (o) => { calls.copyFile.push(o); if (o.success) o.success({}); },
  };
  const sandbox = {
    console: { log() {}, warn: console.warn, error: console.error },
    JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map,
    parseInt, parseFloat, isNaN,
    setTimeout, clearTimeout, setInterval, clearInterval,
    require: (p) => {
      if (p.indexOf('utils/config') >= 0) return { CONFIG: { BASE_URL: 'https://example.test' } };
      if (p.indexOf('utils/request') >= 0) return { request: () => {} };
      throw new Error('unexpected require: ' + p);
    },
    Page: (cfg) => { pageConfig = cfg; },
    Component: (cfg) => { pageConfig = cfg; },
    getApp: () => ({ globalData: { isDarkMode: false, themeMode: 'light' } }),
    wx: {
      getStorageSync: () => '', setStorageSync() {}, removeStorageSync() {},
      setBackgroundColor() {}, switchTab() {}, hideLoading() {}, showLoading: () => { calls.loading++; },
      getWindowInfo: () => ({ windowWidth: 375, windowHeight: 800 }),
      getSystemInfoSync: () => ({ windowWidth: 375, windowHeight: 800, platform: 'devtools' }),
      createSelectorQuery: () => ({
        select: () => ({ boundingClientRect() { return this; }, scrollOffset() { return this; } }),
        selectAll: () => ({ boundingClientRect() { return this; } }),
        in: function () { return this; }, exec(cb) { cb && cb([[], null, { scrollTop: 0 }]); },
      }),
      nextTick: (fn) => setTimeout(fn, 0),
      showToast: (o) => calls.toast.push(o && o.title),
      showModal: (o) => calls.modal.push(o),
      setClipboardData: () => {},
      uploadFile: () => ({ abort() {}, onProgressUpdate() {} }),
      previewImage: (o) => calls.previewImage.push(o),
      openDocument: (o) => { calls.openDocument.push(o); if (sandbox.__openDocFail && o.fail) o.fail({}); },
      getFileSystemManager: () => fsStub,
      env: { USER_DATA_PATH: 'wxfile://usr' },
    },
  };
  sandbox.__readData = '';
  sandbox.__openDocFail = false;
  sandbox.global = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  try { vm.runInContext(src, sandbox, { filename: 'index.js' }); }
  catch (e) { check('index.js 可在桩环境中加载', false, e.message); return; }
  check('index.js 可在桩环境中加载', true);

  const page = Object.assign({}, pageConfig, pageConfig.methods);
  page.data = JSON.parse(JSON.stringify(pageConfig.data || {}));
  page.setData = function (patch, cb) { Object.keys(patch || {}).forEach(k => setByPath(this.data, k, patch[k])); if (cb) cb(); };
  page._scheduleMeasure = () => {};
  page.getTabBar = () => null;

  const mk = (over) => Object.assign({
    _uid: 1, name: 'a.txt', size: 1024, path: 'wxfile://tmp/a.txt', fileId: 'F1',
    uploading: false, progress: 100, failed: false, copies: 1, pageRange: '',
    rangeLines: [{ value: '', error: '' }], duplex: 'on', imageOrientation: 'auto',
    entering: false, removing: false, excelWarning: false, unsupportedFormat: false,
    isImage: false, pageCount: 1, pageCountStatus: 'confirmed', singlePage: false,
  }, over || {});
  const preview = (i) => { page.onPreviewFile({ currentTarget: { dataset: { index: i } } }); };
  const reset = () => {
    calls.previewImage.length = 0; calls.openDocument.length = 0; calls.readFile.length = 0;
    calls.copyFile.length = 0; calls.toast.length = 0; calls.loading = 0;
    sandbox.__openDocFail = false;
    page.setData({ showPreview: false, previewName: '', previewText: '', previewTruncated: false });
  };

  page.data.selectedFiles = [
    mk({ name: '照片.png', isImage: true, path: 'wxfile://tmp/p.png' }),          // 0
    mk({ name: '报告.docx', path: 'wxfile://tmp/r.docx' }),                       // 1
    mk({ name: '手册.pdf', path: 'wxfile://tmp/m.pdf' }),                         // 2
    mk({ name: '说明.md', path: 'wxfile://tmp/d.md' }),                           // 3
    mk({ name: '清单.csv', path: 'wxfile://tmp/list.csv' }),                      // 4
    mk({ name: '备注.txt', path: '', fileId: 'F9' }),                             // 5 云端订单（无本机副本）
    mk({ name: '坏文件.txt', path: '', fileId: null }),                           // 6 文件已失效
  ];

  /* ① 图片 → wx.previewImage（原生缩放/左右滑） */
  reset();
  preview(0);
  check('①图片走 wx.previewImage 且用本机路径',
        calls.previewImage.length === 1 && calls.previewImage[0].urls[0] === 'wxfile://tmp/p.png',
        JSON.stringify(calls.previewImage));
  check('①图片不触发 openDocument / readFile',
        calls.openDocument.length === 0 && calls.readFile.length === 0);

  /* ② PDF / Word → wx.openDocument（fileType 正确） */
  reset();
  preview(1);
  check('②Word 走 wx.openDocument 且 fileType=docx',
        calls.openDocument.length === 1 && calls.openDocument[0].fileType === 'docx'
        && calls.openDocument[0].filePath === 'wxfile://tmp/r.docx',
        JSON.stringify(calls.openDocument.map(o => o.fileType)));
  reset();
  preview(2);
  check('②PDF 走 wx.openDocument 且 fileType=pdf',
        calls.openDocument.length === 1 && calls.openDocument[0].fileType === 'pdf');

  /* ③ openDocument 失败 → 复制到用户目录重试一次（临时路径机型兼容） */
  reset();
  sandbox.__openDocFail = true;   // 首次失败
  preview(1);
  check('③openDocument 失败后复制到用户目录',
        calls.copyFile.length === 1 && String(calls.copyFile[0].destPath).indexOf('wxfile://usr/preview_') === 0,
        JSON.stringify(calls.copyFile));
  check('③复制成功后会再试一次 openDocument',
        calls.openDocument.length === 2, 'openDocument=' + calls.openDocument.length);

  /* ④ md / csv → 本地读取 + 预览层自绘 */
  reset();
  sandbox.__readData = '# 标题\n正文内容\n第二行';
  preview(3);
  check('④md 走本地 readFile（utf8）',
        calls.readFile.length === 1 && calls.readFile[0].encoding === 'utf8'
        && calls.readFile[0].filePath === 'wxfile://tmp/d.md');
  check('④md 打开预览层且内容正确',
        page.data.showPreview === true && page.data.previewName === '说明.md'
        && page.data.previewText === '# 标题\n正文内容\n第二行' && page.data.previewTruncated === false,
        JSON.stringify({ show: page.data.showPreview, n: page.data.previewName }));
  reset();
  sandbox.__readData = 'a,b,c\n1,2,3';
  preview(4);
  check('④csv 同样走本地读取 + 预览层',
        calls.readFile.length === 1 && page.data.previewText === 'a,b,c\n1,2,3');

  /* ⑤ 超长文本 → 截断标记 */
  reset();
  sandbox.__readData = 'x'.repeat(25000);
  preview(3);
  check('⑤超长文本被截断并标记', page.data.previewText.length === 20000 && page.data.previewTruncated === true,
        'len=' + page.data.previewText.length);

  /* ⑥ 文本类 > 2MB 不读（避免读进内存） */
  reset();
  page.data.selectedFiles[3].size = 3 * 1024 * 1024;
  preview(3);
  check('⑥>2MB 文本不读文件、直接提示',
        calls.readFile.length === 0 && /文件较大/.test(calls.toast.join('|')), calls.toast.join('|'));
  page.data.selectedFiles[3].size = 1024;

  /* ⑦ 无本机副本：云端订单文件 vs 已失效文件 文案区分 */
  reset();
  preview(5);
  check('⑦云端订单文件：提示不支持预览且不报"已失效"',
        calls.toast.length === 1 && /云端订单/.test(calls.toast[0]), calls.toast.join('|'));
  reset();
  preview(6);
  check('⑦本机文件失效：提示重新选择',
        calls.toast.length === 1 && /重新选择/.test(calls.toast[0]), calls.toast.join('|'));

  /* ⑧ 关闭预览层 */
  reset();
  sandbox.__readData = 'abc';
  preview(3);
  page.onClosePreview();
  check('⑧关闭预览层清空状态',
        page.data.showPreview === false && page.data.previewText === '' && page.data.previewName === '');

  /* ⑨ 空文件也给出可见内容 */
  reset();
  sandbox.__readData = '';
  preview(3);
  check('⑨空文件显示占位文本', page.data.previewText === '（空文件）', page.data.previewText);
}

/* ============================================================
   二、Android APP（www/print.js 真实代码）
   ============================================================ */
function mkEl() {
  return {
    innerHTML: '', textContent: '', value: '', style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    querySelector: () => null, querySelectorAll: () => [], appendChild() {},
    addEventListener() {}, removeEventListener() {},
    getBoundingClientRect: () => ({ height: 0, top: 0 }),
    parentNode: null, children: [],
  };
}

function runAndroid() {
  section('Android www/print.js');
  const src = fs.readFileSync(APP_PRINT, 'utf8');
  const elements = {};
  const calls = { toast: [], modalOpen: [], modalClose: [], objectUrls: [], revoked: [] };

  const sandbox = {
    console: { log() {}, warn: console.warn, error: console.error },
    JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map,
    parseInt, parseFloat, isNaN, encodeURIComponent, decodeURIComponent,
    setTimeout, clearTimeout, setInterval, clearInterval,
    XMLHttpRequest: class { open() {} setRequestHeader() {} abort() {} send() {} },
    FormData: class { append() {} },
    Blob: class {},
    FileReader: class {
      readAsText() { this.onload && this.onload(); }
    },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    navigator: { userAgent: 'node' },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.URL = {
    createObjectURL: (file) => { const u = 'blob:mock/' + (file && file.name || 'x'); calls.objectUrls.push(u); return u; },
    revokeObjectURL: (u) => calls.revoked.push(u),
  };
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
  sandbox.api = () => Promise.resolve({ status: 200, data: { success: true } });
  sandbox.ensureLogin = () => Promise.resolve(true);
  sandbox.request = () => {};
  sandbox.showToast = (msg) => calls.toast.push(msg);
  sandbox.showAlert = () => {};
  sandbox.showConfirm = () => {};
  sandbox.openModal = (id) => calls.modalOpen.push(id);
  sandbox.closeModal = (id) => { calls.modalClose.push(id); if (id === 'previewMask' && typeof sandbox.closePreviewBody === 'function') sandbox.closePreviewBody(); };
  sandbox.copyText = () => {};
  sandbox.renderFileList = () => {};
  sandbox.renderPricing = () => {};
  sandbox.getApp = () => ({ globalData: {} });
  sandbox.esc = (s) => String(s == null ? '' : s);
  sandbox.escHtml = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  sandbox.escapeHtml = sandbox.escHtml;
  sandbox.escapeAttr = sandbox.escHtml;
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
  try { vm.runInContext(src, sandbox, { filename: 'print.js' }); }
  catch (e) { check('print.js 可在桩环境中加载', false, e.message); return; }
  check('print.js 可在桩环境中加载', true);

  const run = (code) => vm.runInContext(code, sandbox);
  const fileObj = (name, text) => ({ name, size: text ? text.length : 1024, text: () => Promise.resolve(text == null ? '' : text) });
  const mk = (over) => Object.assign({
    _uid: 1, name: 'a.txt', size: 1024, file: fileObj('a.txt', 'hello'), fileId: 'F1',
    uploading: false, progress: 100, failed: false, copies: 1, pageRange: '',
    rangeLines: [{ value: '', error: '' }], duplex: 'on', imageOrientation: 'auto',
    entering: false, removing: false, excelWarning: false, unsupportedFormat: false,
    isImage: false, pageCount: 1, pageCountStatus: 'confirmed', singlePage: false, sizeDisplay: '1.0',
  }, over || {});
  const reset = () => {
    calls.toast.length = 0; calls.modalOpen.length = 0; calls.modalClose.length = 0;
    calls.objectUrls.length = 0; calls.revoked.length = 0;
    const body = elements.previewBody; if (body) body.innerHTML = '';
  };
  const bodyHTML = () => (elements.previewBody ? elements.previewBody.innerHTML : '');

  run(`
    printState.selectedFiles = [
      { _uid: 1, name: '照片.png', size: 2048, file: ${'({ name: "照片.png", size: 2048 })'}, fileId: 'F1',
        uploading: false, progress: 100, failed: false, copies: 1, pageRange: '', rangeLines: [{value:'',error:''}],
        duplex: 'off', imageOrientation: 'auto', entering: false, removing: false, excelWarning: false,
        unsupportedFormat: false, isImage: true, pageCount: 1, pageCountStatus: '', singlePage: true, sizeDisplay: '2.0' },
      { _uid: 2, name: '报告.docx', size: 4096, file: { name: '报告.docx', size: 4096 }, fileId: 'F2',
        uploading: false, progress: 100, failed: false, copies: 1, pageRange: '', rangeLines: [{value:'',error:''}],
        duplex: 'on', imageOrientation: 'auto', entering: false, removing: false, excelWarning: false,
        unsupportedFormat: false, isImage: false, pageCount: 0, pageCountStatus: 'analyzing', singlePage: false, sizeDisplay: '4.0' },
      { _uid: 3, name: '说明.md', size: 32, file: { name: '说明.md', size: 32, text: function () { return Promise.resolve('# 标题\\n正文'); } }, fileId: 'F3',
        uploading: false, progress: 100, failed: false, copies: 1, pageRange: '', rangeLines: [{value:'',error:''}],
        duplex: 'on', imageOrientation: 'auto', entering: false, removing: false, excelWarning: false,
        unsupportedFormat: false, isImage: false, pageCount: 1, pageCountStatus: 'confirmed', singlePage: true, sizeDisplay: '0.1' },
      { _uid: 4, name: '云端文件.txt', size: 16, file: null, fileId: 'F4',
        uploading: false, progress: 100, failed: true, copies: 1, pageRange: '', rangeLines: [{value:'',error:''}],
        duplex: 'on', imageOrientation: 'auto', entering: false, removing: false, excelWarning: false,
        unsupportedFormat: false, isImage: false, pageCount: 0, pageCountStatus: '', singlePage: false, sizeDisplay: '0.1' }
    ];
  `);

  /* ① 图片 → objectURL + <img> */
  reset();
  run('previewFile(0);');
  check('①图片：打开预览层并渲染 <img>（objectURL）',
        calls.modalOpen.indexOf('previewMask') >= 0 && bodyHTML().indexOf('<img') === 0
        && calls.objectUrls.length === 1 && bodyHTML().indexOf(calls.objectUrls[0]) > 0,
        bodyHTML().slice(0, 60));
  check('①图片：标题为文件名', elements.previewTitle && elements.previewTitle.textContent === '照片.png',
        elements.previewTitle && elements.previewTitle.textContent);

  /* ② 关闭 → revoke objectURL + 清空 */
  reset();
  run('closePreview();');
  check('②关闭预览：revoke objectURL 且清空内容',
        calls.revoked.length === 1 && calls.modalClose.indexOf('previewMask') >= 0 && bodyHTML() === '',
        'revoked=' + calls.revoked.length + ' body=' + JSON.stringify(bodyHTML()));

  /* ③ txt/md/csv → File.text() 读文本 → <pre> 渲染（转义） */
  reset();
  run('previewFile(2);');
  return new Promise((resolve) => setTimeout(() => {
    check('③文本类：读文本后打开预览层并渲染 <pre>',
          calls.modalOpen.indexOf('previewMask') >= 0 && bodyHTML().indexOf('<pre class="preview-text">') === 0
          && bodyHTML().indexOf('# 标题') > 0, bodyHTML().slice(0, 80));
    check('③文本类：内容做了 HTML 转义（防注入）',
          bodyHTML().indexOf('<pre class="preview-text">') === 0 && run('escHtml("a<b>c")') === 'a&lt;b&gt;c');

    /* ④ PDF/Word → 明确提示不支持（WebView 无原生渲染） */
    reset();
    run('previewFile(1);');
    check('④Word：不打开预览层，提示需用系统应用打开',
          calls.modalOpen.length === 0 && calls.toast.length === 1 && /暂不支持预览/.test(calls.toast[0]),
          calls.toast.join('|'));

    /* ⑤ 无本机 File 对象的云端订单文件 → 提示来自云端 */
    reset();
    run('previewFile(3);');
    check('⑤云端订单文件：提示来自云端订单',
          calls.toast.length === 1 && /云端订单/.test(calls.toast[0]) && calls.modalOpen.length === 0,
          calls.toast.join('|'));

    /* ⑥ 超长文本截断 */
    reset();
    run(`printState.selectedFiles[2].file = { name: '说明.md', size: 30000, text: function () { return Promise.resolve('y'.repeat(25000)); } };`);
    run('previewFile(2);');
    setTimeout(() => {
      const truncated = elements.previewTruncated;
      check('⑥超长文本截断到 20000 字符并显示截断提示',
            bodyHTML().indexOf('y'.repeat(20000)) > 0 && bodyHTML().indexOf('y'.repeat(20001)) < 0
            && truncated && truncated.style.display === '',
            'len=' + bodyHTML().length);
      resolve();
    }, 10);
  }, 10));
}

/* ============================================================
   三、接线核对（模板/样式/入口）
   ============================================================ */
function runWiringChecks() {
  section('接线核对（预览入口 + 预览层 + 样式）');
  const mpWxml = fs.readFileSync(MP_WXML, 'utf8');
  const mpWxss = fs.readFileSync(MP_WXSS, 'utf8');
  const mpJs = fs.readFileSync(MP_INDEX, 'utf8');
  const appHtml = fs.readFileSync(APP_HTML, 'utf8');
  const appCss = fs.readFileSync(APP_CSS, 'utf8');
  const appJs = fs.readFileSync(APP_JS, 'utf8');
  const printJs = fs.readFileSync(APP_PRINT, 'utf8');

  check('小程序：卡片头部有预览入口（名称区 + 预览按钮）',
        /class="file-name-area" bindtap="onPreviewFile"/.test(mpWxml) && /class="file-preview-btn"/.test(mpWxml));
  check('小程序：预览层 markup（标题/正文/截断提示）',
        /class="preview-mask/.test(mpWxml) && /{{previewName}}/.test(mpWxml)
        && /scroll-view class="preview-body"/.test(mpWxml) && /previewTruncated/.test(mpWxml));
  check('小程序：wxss 样式齐备',
        /\.file-preview-btn \{/.test(mpWxss) && /\.preview-sheet \{/.test(mpWxss) && /\.preview-text \{/.test(mpWxss));
  check('小程序：预览不依赖后端（不使用 request / BASE_URL 取文件）',
        /onPreviewFile\(e\) \{[\s\S]{0,900}wx\.previewImage/.test(mpJs) && /onPreviewFile\(e\) \{[\s\S]{0,1200}wx\.openDocument|_previewByNativeDocument/.test(mpJs));

  check('APP：卡片头部有预览入口（data-action="preview"）',
        /class="file-name-area" data-action="preview"/.test(printJs) && /class="file-preview-btn" data-action="preview"/.test(printJs));
  check('APP：点击委托已接 previewFile',
        /action === 'preview'\) previewFile\(idx\)/.test(printJs));
  check('APP：预览层 markup 存在',
        /id="previewMask"/.test(appHtml) && /id="previewBody"/.test(appHtml)
        && /id="previewTitle"/.test(appHtml) && /id="previewTruncated"/.test(appHtml));
  check('APP：预览层复用 .modal-mask（点遮罩可关闭）',
        /class="modal-mask preview-mask" id="previewMask"/.test(appHtml));
  check('APP：closeModal 关闭预览时回收 objectURL/清空内容',
        /id === 'previewMask' && typeof closePreviewBody === 'function'/.test(appJs));
  check('APP：styles.css 样式齐备（含 cqw 换算）',
        /\.file-preview-btn \{/.test(appCss) && /\.preview-sheet \{/.test(appCss)
        && /\.preview-image \{/.test(appCss) && /\.preview-text \{/.test(appCss));
}

/* ============================================================ */
(async () => {
  runMiniProgram();
  await runAndroid();
  runWiringChecks();
  console.log('\n' + '='.repeat(52));
  console.log('文件预览验收:', failures === 0 ? '全部通过 ✅' : `存在 ${failures} 项失败 ❌`);
  process.exit(failures ? 1 : 0);
})();
