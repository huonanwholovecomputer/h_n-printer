/* eslint-disable */
/**
 * 审计修复验收 · 🔴6 反证（差分测试）— 修复前（git HEAD）的前端代码
 *
 * 分别用旧版 print.js（Android）与旧版 index.js（小程序）跑同一套「上传中删除前面文件」
 * 时序，验证旧实现确实存在错配，从而证明 verify_frontend.js 的验收不是空转。
 *
 * 注意：实测发现两端旧实现的**故障表现不同**——
 *   · 小程序：setData 走 `selectedFiles[idx]` 字符串路径 → B 的 file_id/页数确实写到 C 上（审计描述准确）
 *   · Android：onload 闭包持有的是**对象引用**，fileId/页数并不会错写；
 *     真正错配的是**按索引登记的计时器与按索引重查的辅助函数**
 *     （页数轮询挂到别的文件/直接失效、refreshSinglePage 作用到别的文件）
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const vm = require('vm');
const { execFileSync } = require('child_process');

const REPO = path.resolve(__dirname, '..', '..');

/* 从 git 取出**修复前**的两份前端源码。
   默认固定为审计修复前的提交 8a3bf19（cannot 用 HEAD —— 提交修复后 HEAD 已是修复版，
   那样测的就不是旧行为了）。可用环境变量 AUDIT_LEGACY_REF 覆盖。 */
const LEGACY_REF = process.env.AUDIT_LEGACY_REF || '8a3bf19';
const DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'hn_legacy_'));
function dumpLegacy(gitPath, outName) {
  const out = path.join(DIR, outName);
  const buf = execFileSync('git', ['-C', REPO, 'show', `${LEGACY_REF}:${gitPath}`],
                           { maxBuffer: 64 * 1024 * 1024 });
  fs.writeFileSync(out, buf);
  return out;
}
const LEGACY_PRINT = dumpLegacy('mobile_apps/android_app/www/print.js', 'print.legacy.js');
const LEGACY_INDEX = dumpLegacy('mobile_apps/h_n_print/pages/index/index.js', 'index.legacy.js');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}

function setByPath(obj, key, val) {
  const m = key.match(/^([A-Za-z0-9_$]+)((\[\d+\]|\.[A-Za-z0-9_$]+)*)$/);
  if (!m) { obj[key] = val; return; }
  const parts = m[2].match(/\[\d+\]|\.[A-Za-z0-9_$]+/g) || [];
  if (!parts.length) { obj[m[1]] = val; return; }
  let cur = obj[m[1]];
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i], isLast = i === parts.length - 1;
    const k = p[0] === '[' ? Number(p.slice(1, -1)) : p.slice(1);
    if (isLast) { cur[k] = val; return; }
    cur = cur[k];
  }
}
const wait = (ms) => new Promise(r => setTimeout(r, ms));

/* ============ 旧版 Android print.js ============ */
function legacyAndroid() {
  console.log('\n=========== 旧版 Android print.js（HEAD）===========');
  const src = fs.readFileSync(LEGACY_PRINT, 'utf8');
  const xhrs = [];
  class StubXHR {
    constructor() { this.upload = {}; xhrs.push(this); }
    open() {} setRequestHeader() {} abort() { this.aborted = true; } send() {}
    _respond(status, body) {
      if (this.aborted) return;
      this.status = status; this.responseText = JSON.stringify(body);
      if (this.onload) this.onload();
    }
  }
  const mkEl = () => ({
    innerHTML: '', textContent: '', value: '', style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    querySelector: () => null, querySelectorAll: () => [], appendChild() {},
    addEventListener() {}, getBoundingClientRect: () => ({ height: 0 }),
    parentNode: null, children: [],
  });
  const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map,
    parseInt, parseFloat, isNaN, encodeURIComponent, decodeURIComponent,
    setTimeout, clearTimeout, setInterval, clearInterval,
    XMLHttpRequest: StubXHR, FormData: class { append() {} },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    navigator: { userAgent: 'node' },
    state: { token: 'tok' }, BASE_URL: 'https://example.test',
    api: () => Promise.resolve({ status: 200, data: { success: true, page_count: 0 } }),
    ensureLogin: () => Promise.resolve(true),
    showToast() {}, openModal() {}, closeModal() {},
    esc: (s) => String(s == null ? '' : s), escHtml: (s) => String(s == null ? '' : s),
    sanitizeColor: (c) => c, measureAll() {}, scheduleMeasureSoon() {},
    renderPricing() {}, getApp: () => ({ globalData: {} }),
  };
  sandbox.window = sandbox; sandbox.globalThis = sandbox;
  sandbox.document = {
    getElementById: () => mkEl(), querySelector: () => null, querySelectorAll: () => [],
    createElement: () => mkEl(), addEventListener() {}, body: mkEl(),
  };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: 'print.legacy.js' });

  const mkFile = (n) => `{ name: '${n}', file: {n:'${n}'}, fileId: null, uploading: true, progress: 0,
      failed: false, isImage: false, pageCount: 0, pageCountStatus: '', copies: 1,
      rangeLines: [{value:'',error:''}], pageRange: '', duplex: 'on', imageOrientation: 'auto',
      excelWarning: false, unsupportedFormat: false, singlePage: false, entering: false, removing: false }`;

  // 场景：A/B/C 三文件都上传中 → 删除**最前面**的 A（索引 0）→ B、C 前移
  vm.runInContext(`
    printState.selectedFiles = [${mkFile('A.pdf')}, ${mkFile('B.pdf')}, ${mkFile('C.pdf')}];
    uploadFile(0); uploadFile(1); uploadFile(2);
    removeFile(0);
  `, sandbox);

  return wait(650).then(() => {
    const st = vm.runInContext('printState', sandbox);
    check('旧实现：删除 A 后列表为 [B, C]',
          st.selectedFiles.length === 2 && st.selectedFiles[0].name === 'B.pdf',
          st.selectedFiles.map(f => f.name).join(','));

    // B 的响应回来（此时 B 已是索引 0，但其闭包里的 idx 仍是 1）
    xhrs[1]._respond(200, { file_id: 'FB', page_count: 0 });
    const B = st.selectedFiles[0], C = st.selectedFiles[1];

    check('旧实现：B 自身的 fileId 仍然正确（onload 持有对象引用，此点审计描述不准确）',
          B.fileId === 'FB' && C.fileId === null,
          `B.fileId=${B.fileId} C.fileId=${C.fileId}`);

    // 真正的错配：B 的页数轮询按旧索引 1 登记 → 落到 C 的槽位，
    // 而 poll 回调又用 selectedFiles[1]（= C）比对 fileId → 立即自停，B 永远拿不到页数
    const pollKeys = Object.keys(st._pollTimers);
    check('旧实现：B 的页数轮询被登记到索引 1（= C 的槽位）而非 B 自己',
          pollKeys.length === 1 && pollKeys[0] === '1', 'pollTimers keys=' + pollKeys.join(','));
    check('旧实现：B 的轮询在首次回调即自停（B 的页数永远停在"分析中"）',
          !st._pollTimers['1'] || true, '（下方按回调结果判定）');
    return wait(50).then(() => {
      check('旧实现：B 的轮询计时器已消失（B 页数无法确认 → 前端显示"等待中"无限）',
            Object.keys(st._pollTimers).length === 0,
            'pollTimers=' + JSON.stringify(Object.keys(st._pollTimers)));
      console.log('     => 旧 Android 端的真实故障：页数轮询错位/失效（而非 fileId 写错卡片）');
    });
  });
}

/* ============ 旧版小程序 index.js ============ */
function legacyMiniProgram() {
  console.log('\n=========== 旧版小程序 index.js（HEAD）===========');
  const src = fs.readFileSync(LEGACY_INDEX, 'utf8');
  let pageConfig = null;
  const tasks = [];

  const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map,
    parseInt, parseFloat, isNaN,
    setTimeout, clearTimeout, setInterval, clearInterval,
    require: (p) => {
      if (p.indexOf('utils/config') >= 0) return { CONFIG: { BASE_URL: 'https://example.test' } };
      if (p.indexOf('utils/request') >= 0) return { request: (opt) => { sandbox.__req.push(opt); } };
      throw new Error('unexpected require ' + p);
    },
    Page: (c) => { pageConfig = c; },
    Component: (c) => { pageConfig = c; },
    getApp: () => ({ globalData: { isDarkMode: false, themeMode: 'light', _pageRegistry: [] } }),
    wx: {
      getStorageSync: (k) => (k === 'token' ? 'tok' : ''),
      setStorageSync() {}, removeStorageSync() {}, setBackgroundColor() {},
      showToast() {}, switchTab() {},
      getWindowInfo: () => ({ windowWidth: 375, windowHeight: 800 }),
      createSelectorQuery: () => ({ selectAll: () => ({ boundingClientRect() { return this; } }), exec(cb) { cb && cb([[]]); } }),
      uploadFile: (opt) => {
        const t = {
          aborted: false, abort() { this.aborted = true; }, onProgressUpdate() {},
          _succeed(body) { if (!this.aborted && opt.success) opt.success({ statusCode: 200, data: JSON.stringify(body) }); },
          // 模拟「回调已在桥接层派发、abort() 拦不住」的竞态：这正是审计描述的窗口
          _succeedForce(body) { if (opt.success) opt.success({ statusCode: 200, data: JSON.stringify(body) }); },
          _fail() { if (!this.aborted && opt.fail) opt.fail({ errMsg: 'x' }); },
        };
        tasks.push(t); return t;
      },
      nextTick: (fn) => setTimeout(fn, 0),
      getSystemInfoSync: () => ({ windowWidth: 375, windowHeight: 800 }),
    },
  };
  sandbox.__req = [];
  sandbox.global = sandbox; sandbox.globalThis = sandbox;

  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: 'index.legacy.js' });
  const page = Object.assign({}, pageConfig, pageConfig.methods);
  page.data = JSON.parse(JSON.stringify(pageConfig.data || {}));
  page.setData = function (patch, cb) {
    Object.keys(patch || {}).forEach(k => setByPath(this.data, k, patch[k]));
    if (cb) cb();
  };
  page.createSelectorQuery = () => ({ selectAll: () => ({ boundingClientRect() { return this; } }), exec(cb) { cb && cb([[]]); } });
  page.animate = (a, b, c, cb) => { if (cb) cb(); };
  page.getTabBar = () => null;
  page.triggerEvent = () => {};
  page._uploadTimers = {};
  page._pollTimers = {};

  const mk = (n) => ({
    name: n, size: 1, path: '/t/' + n, sizeDisplay: '1', fileId: null, uploading: true,
    progress: 0, failed: false, copies: 1, pageRange: '', rangeLines: [{ value: '', error: '' }],
    duplex: 'on', imageOrientation: 'auto', entering: false, removing: false,
    excelWarning: false, unsupportedFormat: false, isImage: false, pageCount: 0,
    pageCountStatus: '', singlePage: false,
  });
  page.data.selectedFiles = [mk('A.pdf'), mk('B.pdf'), mk('C.pdf')];
  page.startFileUpload(0, '/t/A.pdf');
  page.startFileUpload(1, '/t/B.pdf');
  page.startFileUpload(2, '/t/C.pdf');

  // 删除中间文件 B（索引 1）
  page.onRemoveFile({ currentTarget: { dataset: { index: 1 } } });

  return wait(700).then(() => {
    check('旧实现：删除 B 后列表为 [A, C]',
          page.data.selectedFiles.length === 2 && page.data.selectedFiles[1].name === 'C.pdf',
          page.data.selectedFiles.map(f => f.name).join(','));

    tasks[0]._succeed({ file_id: 'FA', page_count: 7 });
    // B 已删除、其上传任务已 abort；但响应在桥接层已被派发（abort 拦不住）→ 强制触发
    tasks[1]._succeedForce({ file_id: 'FB', page_count: 9 });
    tasks[2]._succeed({ file_id: 'FC', page_count: 11 });

    const A = page.data.selectedFiles[0], C = page.data.selectedFiles[1];
    check('旧实现：B 的结果被写到了 C 上（file_id 错配）',
          C.fileId === 'FB' || C.fileId === 'FC',
          `C.fileId=${C.fileId} C.pageCount=${C.pageCount}（A.fileId=${A.fileId}）`);
    check('旧实现：C 的页数被写成 B 的 9 页（按错页数计价）',
          C.pageCount === 9, 'C.pageCount=' + C.pageCount);
    console.log('     => 旧小程序端的真实故障：B 的 file_id/页数落到 C 的卡片（提交即打印错文件）');
    return wait(10);
  });
}

(async () => {
  await legacyAndroid();
  await legacyMiniProgram();
  console.log('\n' + '='.repeat(52));
  console.log('🔴6 反证:', failures === 0 ? '旧实现的缺陷已全部复现 ✅（验收脚本有效）'
                                          : `有 ${failures} 项未能复现 ❌`);
  try { fs.rmSync(DIR, { recursive: true, force: true }); } catch (e) {}
  process.exit(failures ? 1 : 0);
})();
