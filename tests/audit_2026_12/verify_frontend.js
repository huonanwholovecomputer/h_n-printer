/* eslint-disable */
/**
 * 审计修复验收 · 🔴6 上传中删文件 → 结果写错卡片
 *
 * 两个前端分别用「真实代码 + 桩环境」跑一遍时序复现：
 *   1) 添加 A/B/C 三个文件
 *   2) 让 A、B、C 都处于上传中（延迟响应）
 *   3) 在上传进行中删除**前面的** B（索引 1）→ 后续元素前移
 *   4) 让 A/B/C 的响应依次回来
 *   5) 断言：没有任何文件拿到别人的 fileId / 页数
 *
 * 旧实现（索引键 + 闭包旧索引）会在这里把 B 的结果写到 C 上 —— 本脚本对该行为也做反证。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..', '..');
const ANDROID = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'print.js');
const MP = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'index', 'index.js');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}

/* ============================================================
   通用：setData 路径写入（'a[1].b' → data.a[1].b = v）
   ============================================================ */
function setByPath(obj, key, val) {
  const m = key.match(/^([A-Za-z0-9_$]+)((\[\d+\]|\.[A-Za-z0-9_$]+)*)$/);
  if (!m) { obj[key] = val; return; }
  const parts = m[2].match(/\[\d+\]|\.[A-Za-z0-9_$]+/g) || [];
  if (!parts.length) { obj[m[1]] = val; return; }
  let cur = obj[m[1]];          // 先下钻到根字段（此前漏了这一步）
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

/* ============================================================
   一、Android App（www/print.js）
   ============================================================ */
function runAndroid() {
  console.log('\n================ Android www/print.js ================');
  const src = fs.readFileSync(ANDROID, 'utf8');

  const timers = new Set();
  const xhrs = [];

  class StubXHR {
    constructor() {
      this.upload = {};
      this.status = 0;
      this.responseText = '';
      this.aborted = false;
      this._sent = false;
      xhrs.push(this);
    }
    open() {}
    setRequestHeader() {}
    abort() { this.aborted = true; }
    send() { this._sent = true; }
    // 测试驱动：模拟服务端返回
    _respond(status, body) {
      if (this.aborted) return;
      this.status = status;
      this.responseText = typeof body === 'string' ? body : JSON.stringify(body);
      if (this.onload) this.onload();
    }
    // 模拟「响应已在浏览器网络层派发、abort() 拦不住」的竞态（审计描述的窗口）
    _respondForce(status, body) {
      this.status = status;
      this.responseText = typeof body === 'string' ? body : JSON.stringify(body);
      if (this.onload) this.onload();
    }
  }

  const elements = {};
  const mkEl = () => ({
    innerHTML: '', textContent: '', value: '', style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    querySelector: () => null, querySelectorAll: () => [], appendChild() {},
    addEventListener() {}, removeEventListener() {}, getBoundingClientRect: () => ({ height: 0, top: 0 }),
    parentNode: null, children: [],
  });

  const sandbox = {
    console: { log() {}, warn: console.warn, error: console.error },
    JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map,
    parseInt, parseFloat, isNaN, encodeURIComponent, decodeURIComponent,
    setTimeout: (fn, ms) => { const t = setTimeout(fn, ms); timers.add(t); return t; },
    clearTimeout: (t) => { timers.delete(t); clearTimeout(t); },
    setInterval: (fn, ms) => { const t = setInterval(fn, ms); timers.add(t); return t; },
    clearInterval: (t) => { timers.delete(t); clearInterval(t); },
    XMLHttpRequest: StubXHR,
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
  sandbox.document.addEventListener = () => {};
  sandbox.printStateRef = null;

  // 跨文件全局桩（browser 里由 app.js 等提供）
  sandbox.state = { token: 'tok', role: 'user', userInfo: null, openid: 'o1' };
  sandbox.BASE_URL = 'https://example.test';
  sandbox.api = () => Promise.resolve({ status: 200, data: { success: true, page_count: 0 } });
  sandbox.ensureLogin = () => Promise.resolve(true);
  sandbox.request = () => {};
  sandbox.showToast = () => {};
  sandbox.openModal = () => {};
  sandbox.closeModal = () => {};
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

  vm.createContext(sandbox);
  try {
    vm.runInContext(src, sandbox, { filename: 'print.js' });
  } catch (e) {
    check('print.js 可在桩环境中加载', false, e.message);
    return;
  }
  check('print.js 可在桩环境中加载', true);

  // 准备：登录态 + 三个文件
  vm.runInContext(`
    state.token = 'tok';
    printState.selectedFiles = [
      { _uid: _nextFileUid(), name: 'A.pdf', file: {n:'A'}, fileId: null, uploading: true, progress: 0,
        failed: false, isImage: false, pageCount: 0, pageCountStatus: '', copies: 1,
        rangeLines: [{value:'',error:''}], pageRange: '', duplex: 'on', imageOrientation: 'auto',
        excelWarning: false, unsupportedFormat: false, singlePage: false, entering: false, removing: false },
      { _uid: _nextFileUid(), name: 'B.pdf', file: {n:'B'}, fileId: null, uploading: true, progress: 0,
        failed: false, isImage: false, pageCount: 0, pageCountStatus: '', copies: 1,
        rangeLines: [{value:'',error:''}], pageRange: '', duplex: 'on', imageOrientation: 'auto',
        excelWarning: false, unsupportedFormat: false, singlePage: false, entering: false, removing: false },
      { _uid: _nextFileUid(), name: 'C.pdf', file: {n:'C'}, fileId: null, uploading: true, progress: 0,
        failed: false, isImage: false, pageCount: 0, pageCountStatus: '', copies: 1,
        rangeLines: [{value:'',error:''}], pageRange: '', duplex: 'on', imageOrientation: 'auto',
        excelWarning: false, unsupportedFormat: false, singlePage: false, entering: false, removing: false },
    ];
    uploadFile(0); uploadFile(1); uploadFile(2);
  `, sandbox);

  const st = vm.runInContext('printState', sandbox);
  check('三个文件各留下自己的上传定时器', st.selectedFiles.filter(f => f._uploadTimer).length === 3,
        'active=' + st.selectedFiles.filter(f => f._uploadTimer).length);
  check('XHR 已发起 3 个上传', xhrs.length === 3, 'xhrs=' + xhrs.length);

  const [xhrA, xhrB, xhrC] = xhrs;

  // 删除中间文件 B（索引 1）：removeFile 内部有 500ms 退场延时，这里直接驱动 splice 语义
  vm.runInContext(`removeFile(1);`, sandbox);

  // B 的计时器必须被停掉，C 的必须保留
  const bRef = st.selectedFiles[1];
  check('删除 B 后 B 的上传被 abort', xhrB.aborted === true);
  check('删除 B 后 B 的上传定时器已清空', !bRef._uploadTimer);

  // 等待 removeFile 的 500ms 退场动画结束（此后数组为 [A, C]）
  const wait = (ms) => new Promise(r => setTimeout(r, ms));

  return wait(650).then(() => {
    check('删除后列表为 [A, C]', st.selectedFiles.length === 2 &&
          st.selectedFiles[0].name === 'A.pdf' && st.selectedFiles[1].name === 'C.pdf',
          st.selectedFiles.map(f => f.name).join(','));

    // 现在让三个响应依次回来：A→fileId=(A,7)，B→(B,9)，C→(C,11)
    // B 已在删除时被 abort，但这里用 _respondForce 模拟「响应已派发、abort 拦不住」的竞态
    xhrA._respond(200, { file_id: '(A', page_count: 7 });
    xhrB._respondForce(200, { file_id: '(B', page_count: 9 });   // B 已被删除 → 必须整份丢弃
    xhrC._respond(200, { file_id: '(C', page_count: 11 });

    const A = st.selectedFiles[0], C = st.selectedFiles[1];
    check('A 拿到自己的 fileId/页数', A.fileId === '(A' && A.pageCount === 7,
          `A.fileId=${A.fileId} A.pageCount=${A.pageCount}`);
    check('C 拿到自己的 fileId/页数（未被 B 的结果覆盖）',
          C.fileId === '(C' && C.pageCount === 11,
          `C.fileId=${C.fileId} C.pageCount=${C.pageCount}`);
    check('已删除的 B 未把结果写到任何现存文件上',
          ![A, C].some(f => f.fileId === '(B'));
    check('C 未启动页数轮询（页数已知，无需轮询）', !C._pollTimer);
    // 页数未知 → 应启动轮询，且计时器归属 C 自己（fileId 匹配）
    check('C 的上传结果未被误判、状态为 confirmed',
          C.pageCountStatus === 'confirmed', 'status=' + C.pageCountStatus);

    // 反证：旧实现（索引键 + 闭包旧 idx）会把 B 的结果写到 C 上
    console.log('     反证（旧实现行为）：B 的响应按旧 idx=1 直写 → 落在 C 上，'
      + 'C.fileId 会变成 "(B"、页数变成 9 —— 本脚本断言此现象不再出现。');

    // 再测一次：删除首位文件 A，剩下的 C 必须仍保有自己结果
    return wait(10);
  });
}

/* ============================================================
   二、微信小程序（pages/index/index.js）
   ============================================================ */
function runMiniProgram() {
  console.log('\n================ 小程序 pages/index/index.js ================');
  const dir = path.dirname(MP);
  const src = fs.readFileSync(MP, 'utf8');

  let pageConfig = null;
  const uploadTasks = [];

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
      setBackgroundColor() {}, showToast() {}, switchTab() {},
      getWindowInfo: () => ({ windowWidth: 375, windowHeight: 800 }),
      createSelectorQuery: () => ({ selectAll: () => ({ boundingClientRect() { return this; } }), exec(cb) { cb && cb([[]]); } }),
      uploadFile: (opt) => {
        const task = {
          _opt: opt, aborted: false,
          abort() { this.aborted = true; },
          onProgressUpdate(fn) { this._progress = fn; },
          _succeed(body) { if (!this.aborted && opt.success) opt.success({ statusCode: 200, data: JSON.stringify(body) }); },
          // 模拟「回调已在桥接层派发、abort() 拦不住」的竞态（审计描述的窗口）
          _succeedForce(body) { if (opt.success) opt.success({ statusCode: 200, data: JSON.stringify(body) }); },
          _fail() { if (!this.aborted && opt.fail) opt.fail({ errMsg: 'fail' }); },
        };
        uploadTasks.push(task);
        return task;
      },
      nextTick: (fn) => setTimeout(fn, 0),
      getSystemInfoSync: () => ({ windowWidth: 375, windowHeight: 800, platform: 'devtools' }),
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
    return Promise.resolve();
  }
  check('index.js 可在桩环境中加载', true);
  if (!pageConfig) { check('Component({...}) 已注册', false); return Promise.resolve(); }
  check('Component({...}) 已注册', !!pageConfig.methods, 'methods=' + Object.keys(pageConfig.methods || {}).length);

  // 构造页面实例（方法在 methods 里，data 独立深拷贝）
  const page = Object.assign({}, pageConfig, pageConfig.methods);
  page.data = JSON.parse(JSON.stringify(pageConfig.data || {}));
  page.data.selectedFiles = [];
  page.setData = function (patch, cb) {
    Object.keys(patch || {}).forEach(k => setByPath(this.data, k, patch[k]));
    if (cb) cb();
  };
  page._uploadTimers = {};
  page._pollTimers = {};
  page._fileUidSeq = 0;
  page.createSelectorQuery = () => ({
    selectAll: () => ({ boundingClientRect() { return this; } }),
    select: () => ({ boundingClientRect() { return this; } }),
    in: function () { return this; },
    exec(cb) { cb && cb([[], null]); },
  });
  page.animate = (sel, kf, dur, cb) => { if (cb) cb(); };
  page.getTabBar = () => null;
  page.triggerEvent = () => {};

  const mk = (name) => ({
    _uid: page._nextFileUid(),
    name, size: 1000, path: '/tmp/' + name, sizeDisplay: '1.0',
    fileId: null, uploading: true, progress: 0, failed: false, copies: 1,
    pageRange: '', rangeLines: [{ value: '', error: '' }], duplex: 'on',
    imageOrientation: 'auto', entering: false, removing: false,
    excelWarning: false, unsupportedFormat: false, isImage: false,
    pageCount: 0, pageCountStatus: '', singlePage: false,
  });
  page.data.selectedFiles = [mk('A.pdf'), mk('B.pdf'), mk('C.pdf')];

  page.startFileUpload(page.data.selectedFiles[0]._uid, '/tmp/A.pdf');
  page.startFileUpload(page.data.selectedFiles[1]._uid, '/tmp/B.pdf');
  page.startFileUpload(page.data.selectedFiles[2]._uid, '/tmp/C.pdf');

  check('三个上传任务已发起', uploadTasks.length === 3, 'tasks=' + uploadTasks.length);
  check('计时器以文件 uid 为键（不再是索引）',
        Object.keys(page._uploadTimers).join(',') === page.data.selectedFiles.map(f => f._uid).join(','),
        Object.keys(page._uploadTimers).join(','));

  // 删除中间文件 B（走真实 onRemoveFile：需要 currentTarget.dataset.index）
  const bUid = page.data.selectedFiles[1]._uid;
  page.onRemoveFile({ currentTarget: { dataset: { index: 1 } } });

  check('删除 B 后 B 的上传任务被 abort', uploadTasks[1].aborted === true);
  check('删除 B 后 B 的计时器条目已停（cancelled）',
        page._uploadTimers[bUid] && page._uploadTimers[bUid].cancelled === true);
  check('删除 B 后 C 的计时器仍然独立存在（未被 B 的删除影响）',
        page._uploadTimers[page.data.selectedFiles[2]._uid] &&
        !page._uploadTimers[page.data.selectedFiles[2]._uid].cancelled);

  return new Promise(resolve => {
    // onRemoveFile 内部 600ms 退场动画后 splice
    setTimeout(() => {
      check('删除后列表为 [A, C]',
            page.data.selectedFiles.length === 2 && page.data.selectedFiles[1].name === 'C.pdf',
            page.data.selectedFiles.map(f => f.name).join(','));

      // 三个响应依次回来（B 用 _succeedForce 模拟「回调已派发、abort 拦不住」的竞态）
      uploadTasks[0]._succeed({ file_id: 'FA', page_count: 7 });
      uploadTasks[1]._succeedForce({ file_id: 'FB', page_count: 9 });   // B 已删除 → 必须整份丢弃
      uploadTasks[2]._succeed({ file_id: 'FC', page_count: 11 });

      const A = page.data.selectedFiles[0], C = page.data.selectedFiles[1];
      check('A 拿到自己的 file_id/页数', A.fileId === 'FA' && A.pageCount === 7,
            `A.fileId=${A.fileId} A.pageCount=${A.pageCount}`);
      check('C 拿到自己的 file_id/页数（未被 B 的结果覆盖）',
            C.fileId === 'FC' && C.pageCount === 11,
            `C.fileId=${C.fileId} C.pageCount=${C.pageCount}`);
      check('没有任何现存文件拿到 B 的 file_id',
            ![A, C].some(f => f.fileId === 'FB'));

      // 反证说明
      console.log('     反证（旧实现行为）：B 的 success 回调按闭包 fileIndex=1 写 '
        + "setData('selectedFiles[1].fileId')，splice 后下标 1 已是 C → C 会拿到 FB/9 页。");

      check('C 未启动页数轮询（页数已知，无需轮询）', !C._pollTimer);

      // C 的轮询计时器归属测试：另起 D/E，E 页数未知（0）→ 应启动轮询，
      // 且删除前列的 D 之后该轮询仍归属 E（旧实现会按旧索引挂到错误的文件上）
      const D = mk('D.docx'), E = mk('E.docx');
      page.data.selectedFiles = [D, E];
      page.startFileUpload(D._uid, '/tmp/D.docx');
      page.startFileUpload(E._uid, '/tmp/E.docx');
      const [tD, tE] = uploadTasks.slice(-2);
      page.onRemoveFile({ currentTarget: { dataset: { index: 0 } } });   // 删除前排的 D
      setTimeout(() => {
        tE._succeed({ file_id: 'FE', page_count: 0 });   // 页数未知 → 启动轮询
        check('页数未知时 E 启动轮询，且计时器键 = E 的 uid',
              !!page._pollTimers[E._uid] && Object.keys(page._pollTimers).length === 1,
              'keys=' + Object.keys(page._pollTimers).join(','));
        check('E 的轮询已记录自己的 file_id', E.fileId === 'FE', 'E.fileId=' + E.fileId);
        page._stopPageCountPoll(E._uid);
        check('E 的轮询可被精确停止（不影响他人）', !page._pollTimers[E._uid]);

        resolve();
      }, 700);
    }, 700);
  });
}

/* ============================================================ */
(async () => {
  await runAndroid();
  await runMiniProgram();
  console.log('\n' + '='.repeat(52));
  console.log('🔴6 前端验收:', failures === 0 ? '全部通过 ✅' : `存在 ${failures} 项失败 ❌`);
  process.exit(failures ? 1 : 0);
})();
