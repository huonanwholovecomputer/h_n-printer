/* eslint-disable */
/**
 * APP 请求超时验收（D5 最后一条）
 *
 * 线上形状：全 APP **没有任何请求超时**（后端有、本地工具有，前端是唯一缺口）。
 * `await api(...)` 遇到 TCP 连上但收不到数据（弱网 / 校园网抖动 / 代理异常）会**永久挂起**：
 * 界面停在加载态，既不报错也不重试，用户只能杀进程。
 *
 * 修复后本脚本钉住：
 *  ① `api()` 默认带超时（AbortController）→ 超时抛 `err.timeout = true` 的「请求超时」；
 *  ② 单次调用可用 `opts.timeoutMs` 覆盖；传 0 表示不限（不注入 signal、不挂定时器）；
 *  ③ 调用方自带 `signal` 时不覆盖它（尊重调用方）；
 *  ④ 正常响应仍返回 `{status, data}`（回归）；401 仍触发重新登录后重放（回归）；
 *  ⑤ 上传（print.js 的 XHR）也必须有上限 + ontimeout 处理（改前完全没有 timeout）。
 *
 * 用真实 www/app.js + 桩环境（fetch / localStorage / document / AbortController）。
 * 用法：node tests/verify_app_request_timeout.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..');
const APP_JS = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'app.js');
const PRINT_JS = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'print.js');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? `  -- ${detail}` : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }

/** 载入真实 app.js；fetchImpl(fetchOpts, url) 由各用例提供（可返回永不 settle 的 promise）。 */
function loadApp(fetchImpl) {
  const stored = {};
  const sandbox = {
    console,
    setTimeout, clearTimeout,        // 真计时器：超时用例依赖它
    fetch: (url, opts) => fetchImpl(url, opts),
    localStorage: {
      getItem: (k) => (k in stored ? stored[k] : null),
      setItem: (k, v) => { stored[k] = String(v); },
      removeItem: (k) => { delete stored[k]; },
    },
    document: {
      addEventListener: () => {}, removeEventListener: () => {},
      documentElement: { style: {}, classList: { add: () => {}, remove: () => {} } },
      body: { classList: { add: () => {}, remove: () => {} } },
      querySelector: () => null, querySelectorAll: () => [],
      createElement: () => ({ style: {}, classList: { add: () => {}, remove: () => {} },
                              addEventListener: () => {}, appendChild: () => {} }),
      hidden: false, readyState: 'complete',
    },
    window: {
      addEventListener: () => {}, removeEventListener: () => {},
      innerWidth: 375, innerHeight: 800, devicePixelRatio: 2,
      matchMedia: () => ({ matches: false, addEventListener: () => {} }),
      location: { href: 'https://example.invalid/' },
    },
    navigator: { userAgent: 'node-test' },
    location: { href: 'https://example.invalid/' },
    requestAnimationFrame: (fn) => setTimeout(() => fn(Date.now()), 0),
    cancelAnimationFrame: (id) => clearTimeout(id),
    CustomEvent: class { constructor(t, o) { this.type = t; Object.assign(this, o || {}); } },
    AbortController,
    alert: () => {}, confirm: () => true,
  };
  sandbox.globalThis = sandbox;
  sandbox.window.document = sandbox.document;
  sandbox.window.localStorage = sandbox.localStorage;
  sandbox.window.fetch = sandbox.fetch;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(APP_JS, 'utf8'), sandbox, { filename: 'app.js' });
  if (typeof sandbox.api !== 'function') {
    throw new Error('app.js 未暴露 api()（夹具需同步更新）');
  }
  return sandbox;
}

(async () => {
  section('① 默认超时：连上但收不到数据 → 必须抛「请求超时」而不是永久挂起');
  {
    const app = loadApp((url, opts) => new Promise((resolve, reject) => {
      // 永不 settle，只在被 abort 时 reject（模拟真实 fetch 的 AbortError）
      if (opts && opts.signal) {
        opts.signal.addEventListener('abort', () => {
          const e = new Error('aborted');
          e.name = 'AbortError';
          reject(e);
        });
      }
    }));
    const t0 = Date.now();
    // ⚠ 改前版本这里会**永久挂起**（没有超时）—— 直接用 await 会把整个脚本挂死，
    // 反向验证就跑不到汇总行了。用竞速 + 哨兵：拿到 '__HANG__' 就是"永久挂起"的直接证据。
    const raced = await Promise.race([
      app.api('/api/orders', { timeoutMs: 40 }).then(() => ({ ok: true })).catch((e) => e),
      new Promise((res) => setTimeout(() => res('__HANG__'), 2500)),
    ]);
    const err = (raced === '__HANG__') ? null : raced;
    const cost = Date.now() - t0;
    check('★ 超时后抛错（改前这里是永久挂起，只能靠竞速哨兵测出来）',
      raced !== '__HANG__' && !!err,
      raced === '__HANG__' ? '★ 永久挂起（>2.5s 未返回）= 改前缺陷现场' : String(err));
    check('★ 错误标记为超时且文案可辨识',
      !!err && err.timeout === true && err.message === '请求超时',
      err ? `${err.message} timeout=${err.timeout}` : '（未返回）');
    check('确实等到了超时时间才退出（不是立刻失败）', cost >= 30 && cost < 2000, `${cost}ms`);
  }

  section('② 单次覆盖：timeoutMs 覆盖默认值');
  {
    const seen = [];
    const app = loadApp((url, opts) => { seen.push(opts); return Promise.resolve({ status: 200, json: async () => ({ ok: 1 }) }); });
    const r = await app.api('/api/x');
    check('默认调用传入 signal（有超时保护）', !!seen[0] && !!seen[0].signal);
    check('默认响应仍返回 {status, data}（回归）',
      r && r.status === 200 && r.data && r.data.ok === 1, JSON.stringify(r));
    seen.length = 0;
    await app.api('/api/x', { timeoutMs: 0 });
    check('timeoutMs=0 → 不注入 signal（不限时，长任务用）',
      seen.length === 1 && !seen[0].signal, JSON.stringify(!!(seen[0] && seen[0].signal)));
    check('timeoutMs 不会透传给 fetch', seen.length === 1 && !('timeoutMs' in seen[0]));
  }

  section('③ 调用方自带 signal → 不覆盖');
  {
    const myCtrl = new AbortController();
    const seen = [];
    const app = loadApp((url, opts) => { seen.push(opts); return Promise.resolve({ status: 200, json: async () => ({}) }); });
    await app.api('/api/x', { signal: myCtrl.signal });
    check('透传调用方自己的 signal', seen[0] && seen[0].signal === myCtrl.signal);
  }

  section('④ 401 仍触发重新登录并重放（回归）');
  {
    let calls = 0;
    const app = loadApp((url, opts) => {
      calls++;
      if (String(url).includes('/api/device_login')) {
        return Promise.resolve({ status: 200, json: async () => ({ success: true, token: 't2', openid: 'o2' }) });
      }
      if (calls <= 2) return Promise.resolve({ status: 401, json: async () => ({ success: false }) });
      return Promise.resolve({ status: 200, json: async () => ({ success: true }) });
    });
    const r = await app.api('/api/me');
    check('401 后重放并拿到 200', r && r.status === 200, JSON.stringify(r));
    check('确实发生了重新登录请求', calls >= 2, `${calls} 次`);
  }

  section('⑤ 上传（print.js 的 XHR）也必须有限时与超时处理');
  {
    const src = fs.readFileSync(PRINT_JS, 'utf8');
    const code = src.replace(/\/\/[^\n]*/g, '');
    check('★ 设了 xhr.timeout = UPLOAD_TIMEOUT_MS', /xhr\.timeout\s*=\s*UPLOAD_TIMEOUT_MS/.test(code));
    check('★ 有 xhr.ontimeout 处理（标记失败 + 提示可重试）',
      /xhr\.ontimeout\s*=/.test(code) && /上传超时/.test(src));
    check('超时处理里停掉计时器并清理状态（不留野定时器）',
      /xhr\.ontimeout[\s\S]{0,220}stopUploadTimer\(f\)/.test(code)
      && /xhr\.ontimeout[\s\S]{0,260}f\.uploading\s*=\s*false/.test(code));
    const appSrc = fs.readFileSync(APP_JS, 'utf8');
    check('UPLOAD_TIMEOUT_MS 常量在 app.js 里定义（上传上限的唯一来源）',
      /const\s+UPLOAD_TIMEOUT_MS\s*=/.test(appSrc));
  }

  console.log('\n' + (failures ? `存在失败项 ❌ (${failures})` : '全部通过 ✅'));
  process.exit(failures ? 1 : 0);
})();
