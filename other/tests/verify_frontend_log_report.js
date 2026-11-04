/* eslint-disable */
/**
 * 前端日志上报验收（POST /api/log/report）
 *
 * 线上形状：后端 `logs/frontend.log` 长期 0 字节 —— 小程序与 Android App 从来没把前端异常
 * 上报过，用户报"白屏 / 点了没反应"时服务端没有任何线索。
 *
 * 本脚本用**纯源码 + 桩环境**（不联网、不发真实请求）钉住两端 logger 的契约：
 *  ① 只上报 error / warning / critical 白名单级别（非白名单一律归一成 error）；
 *  ② 单条 message ≤4000 字符，超出自己截断并带「…（已截断）」；
 *  ③ 无 token 时零请求（不能靠服务端 403 兜底，那是纯噪音）；
 *  ④ 客户端限速生效：连发 10 条只出 ≤1 条（两次发送间隔 ≥10s）；
 *  ⑤ 内存队列上限 20，超出丢最旧并计数，不无限增长；
 *  ⑥ `?token=abc` 上报前被抹成 `?token=***`；
 *  ⑦ 堆栈只取第一个 at 帧，整条消息压成一行；
 *  ⑧ logger 自身异常被吞掉，绝不炸调用方（含不可序列化对象）。
 * 另外钉住两端接入面（app.js / index.html）与"报表消息组成"格式。
 *
 * 用法：node tests/verify_frontend_log_report.js   （退出码 0 = 全绿）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..', '..');
const MP_DIR = path.join(REPO, 'mobile_apps', 'h_n_print');
const APP_WWW = path.join(REPO, 'mobile_apps', 'android_app', 'www');
const MP_LOGGER = path.join(MP_DIR, 'utils', 'logger.js');
const APP_LOGGER = path.join(APP_WWW, 'logger.js');

const LEVEL_WHITELIST = ['warning', 'error', 'critical'];
const MAX_MESSAGE_LEN = 4000;

let checks = 0;
let failures = 0;
function check(name, cond, detail) {
  checks += 1;
  if (!cond) failures += 1;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (detail ? '  -- ' + detail : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }

function reportBodies(calls) {
  return calls.map(function (c) {
    try { return JSON.parse(c.opts.body); } catch (e) { return null; }
  });
}

/* ============================================================
 * 一、小程序端 logger：CommonJS + 桩 wx
 * ============================================================ */
function makeMpEnv(opts) {
  opts = opts || {};
  const requests = [];
  const timers = [];
  const printed = [];
  let token = 'token-hn-123';
  if (Object.prototype.hasOwnProperty.call(opts, 'token')) token = opts.token;

  const wx = {
    getStorageSync(key) { return key === 'token' ? token : ''; },
    getAccountInfoSync() { return { miniProgram: { version: opts.version || '4.6.0' } }; },
    request(o) {
      requests.push({ url: o.url, method: o.method, header: o.header, data: o.data, timeout: o.timeout });
      if (typeof o.success === 'function') o.success({ statusCode: 200, data: { success: true } });
      if (typeof o.complete === 'function') o.complete({});
    },
  };
  const consoleStub = {
    log() {},
    warn() {},
    error() { printed.push(Array.prototype.slice.call(arguments).map(String).join(' ')); },
  };
  const sandbox = {
    module: { exports: {} },
    require(dep) {
      if (dep === './config') return { CONFIG: { BASE_URL: 'https://log-test.invalid' } };
      throw new Error('logger.js 不该 require: ' + dep);
    },
    wx: wx,
    getCurrentPages() { return [{ route: opts.route || 'pages/print/print' }]; },
    console: consoleStub,
    // 假定时器：只记录不执行，避免验收脚本被 3s 轮询拖住/产生并发发送
    setTimeout(fn, ms) { timers.push({ fn: fn, ms: ms }); return timers.length; },
    clearTimeout() {},
    Date: Date,
    Error: Error,          // 让跨 realm 的 `instanceof Error` 对桩里的 Error 成立
    JSON: JSON,
    Promise: Promise,
  };
  sandbox.exports = sandbox.module.exports;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(MP_LOGGER, 'utf8'), sandbox, { filename: 'utils/logger.js' });
  return {
    logger: sandbox.module.exports,
    wx: wx,
    requests: requests,
    timers: timers,
    printed: printed,
    console: consoleStub,
  };
}

/* ============================================================
 * 二、Android App logger：IIFE + 桩 window/fetch
 * ============================================================ */
function makeAppEnv(opts) {
  opts = opts || {};
  const calls = [];
  const listeners = {};
  const timers = [];
  const stored = {};
  if (opts.token) stored.hn_token = opts.token;

  const documentStub = {
    querySelectorAll() { return opts.views || []; },
    getElementById() { return null; },
    addEventListener() {},
  };
  const localStorageStub = {
    getItem(k) { return Object.prototype.hasOwnProperty.call(stored, k) ? stored[k] : null; },
    setItem(k, v) { stored[k] = String(v); },
    removeItem(k) { delete stored[k]; },
  };
  const win = {
    fetch(url, o) {
      calls.push({ url: String(url), opts: o || {} });
      if (typeof opts.fetchImpl === 'function') return opts.fetchImpl(url, o);
      return Promise.resolve({ status: 200, json: async () => ({ success: true }) });
    },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    document: documentStub,
    localStorage: localStorageStub,
    location: { href: 'http://localhost/index.html' },
    navigator: { userAgent: 'node-verify' },
    AppUpdater: { getVersionName() { return '1.1.50'; } },
    setTimeout(fn, ms) { timers.push({ fn: fn, ms: ms }); return timers.length; },
    clearTimeout() {},
    AbortController: class { constructor() { this.signal = { aborted: false }; } abort() {} },
    Promise: Promise,
    Date: Date,
    Error: Error,
    JSON: JSON,
    console: console,
  };
  win.window = win;
  win.globalThis = win;

  const sandbox = win;
  sandbox.document = documentStub;
  sandbox.localStorage = localStorageStub;
  sandbox.BASE_URL = 'https://log-test.invalid';
  sandbox.currentTab = opts.currentTab || 'print';
  if (opts.stateToken) sandbox.state = { token: opts.stateToken };

  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(APP_LOGGER, 'utf8'), sandbox, { filename: 'logger.js' });
  return {
    win: win,
    logger: win.HNLogger,
    calls: calls,
    listeners: listeners,
    timers: timers,
    stored: stored,
  };
}

async function main() {
  /* ======================= 小程序端 ======================= */
  section('小程序端 ① 级别白名单');
  {
    const env = makeMpEnv();
    env.logger.install();
    check('install() 挂了发送定时器（每 3s 一次 flush）', env.timers.length === 1 && env.timers[0].ms === 3000,
      JSON.stringify(env.timers.map(function (t) { return t.ms; })));
    env.logger.report('info', '自定义级别不该被上报成 info');
    env.logger.logWarn('显式告警');
    env.logger.logError('普通错误');
    const dbg = env.logger._debug();
    check('三条入队', dbg.queued === 3, JSON.stringify(dbg));
    check('队列里的级别都在白名单内',
      dbg.pendingLevels.every(function (l) { return LEVEL_WHITELIST.indexOf(l) >= 0; }),
      JSON.stringify(dbg.pendingLevels));
    check('非白名单级别被归一成 error', dbg.pendingLevels[0] === 'error', dbg.pendingLevels[0]);

    check('flush 发出一条', env.logger.flush() === true && env.requests.length === 1);
    const body = env.requests[0].data;
    check('请求体只有 level + message 两个字段',
      body && Object.keys(body).sort().join(',') === 'level,message', JSON.stringify(body));
    check('发出后的级别仍是白名单', LEVEL_WHITELIST.indexOf(body.level) >= 0, body.level);
    check('message 以 [mp/版本] 页面路径 开头',
      body.message.indexOf('[mp/4.6.0] pages/print/print |') === 0, body.message);
  }

  section('小程序端 ② message 截断 ≤4000 且带「已截断」');
  {
    const env = makeMpEnv();
    env.logger.logError('x'.repeat(6000));
    env.logger.flush();
    const body = env.requests[0].data;
    check('message 长度 ≤4000', body.message.length <= MAX_MESSAGE_LEN,
      String(body.message.length));
    check('截断标记「…（已截断）」在末尾', /…（已截断）$/.test(body.message),
      body.message.slice(-12));
  }

  section('小程序端 ③ 无 token 时零请求');
  {
    const env = makeMpEnv({ token: '' });
    env.logger.logError('错误 A');
    env.logger.logError('错误 B');
    env.logger.logError('错误 C');
    let sent = 0;
    for (let i = 0; i < 6; i++) if (env.logger.flush()) sent += 1;
    check('flush 一次都没发', sent === 0 && env.requests.length === 0, String(env.requests.length));
    check('条目留在队列里等 token', env.logger._debug().queued === 3, JSON.stringify(env.logger._debug()));
  }

  section('小程序端 ④ 客户端限速（间隔 ≥10s，连发 10 条只出 1 条）');
  {
    const env = makeMpEnv();
    for (let i = 0; i < 10; i++) env.logger.logError('连续错误 #' + i);
    let sent = 0;
    for (let i = 0; i < 10; i++) if (env.logger.flush()) sent += 1;
    check('10 次 flush 只发出 1 条', sent === 1 && env.requests.length === 1,
      'sent=' + sent + ' requests=' + env.requests.length);
    check('剩余 9 条仍在队列（等下一个 10s 窗口）', env.logger._debug().queued === 9,
      JSON.stringify(env.logger._debug()));
  }

  section('小程序端 ⑤ 内存队列上限 20，不无限增长');
  {
    const env = makeMpEnv({ token: '' });   // 无 token：只入队，不会因为发送而缩短队列
    for (let i = 0; i < 100; i++) env.logger.logError('灌队列 #' + i);
    const dbg = env.logger._debug();
    check('队列长度正好卡在 20', dbg.queued === 20, String(dbg.queued));
    check('丢弃计数 = 100 - 20', dbg.dropped === 80, String(dbg.dropped));
    check('零请求（无 token）', env.requests.length === 0);
  }

  section('小程序端 ⑥ 脱敏：?token=abc → ?token=***');
  {
    const env = makeMpEnv();
    env.logger.logError('GET /api/me?token=abc&x=1 失败');
    env.logger.flush();
    const msg = env.requests[0].data.message;
    check('敏感 query 值被抹成 ***', msg.indexOf('?token=***') >= 0, msg);
    check('原始 token 值不再出现', msg.indexOf('abc') < 0, msg);
  }
  {
    const env = makeMpEnv();
    env.logger.logError('Authorization: Bearer sk-live-123456789 被拒');
    env.logger.flush();
    const msg = env.requests[0].data.message;
    check('Bearer 令牌被抹掉', msg.indexOf('sk-live-123456789') < 0 && msg.indexOf('Bearer ***') >= 0, msg);
  }
  {
    const env = makeMpEnv();
    env.logger.logError('用户 oAbCdEfGhIjKlMnOpQrStUvWxYz0 不存在');
    env.logger.flush();
    const msg = env.requests[0].data.message;
    check('openid 明文被抹掉', msg.indexOf('oAbCdEfGhIjKlMnOpQrStUvWxYz0') < 0, msg);
  }

  section('小程序端 ⑦ 堆栈只取首个 at 帧，消息压成一行');
  {
    const env = makeMpEnv();
    const err = new Error('x is not a function');
    err.name = 'TypeError';
    err.stack = 'TypeError: x is not a function\n    at printOne (pages/print/print.js:12:3)\n    at pages/print/print.js:99:9';
    env.logger.logError(err);
    env.logger.flush();
    const msg = env.requests[0].data.message;
    check('带错误名与消息', msg.indexOf('TypeError: x is not a function') >= 0, msg);
    check('只保留首个 at 帧', msg.indexOf('print.js:12:3') >= 0 && msg.indexOf('99:9') < 0, msg);
    check('整条消息没有换行', !/[\r\n]/.test(msg));
  }

  section('小程序端 ⑧ logger 自身异常不炸调用方');
  {
    const env = makeMpEnv();
    const bad = {
      toJSON() { throw new Error('toJSON 炸了'); },
      toString() { throw new Error('toString 炸了'); },
    };
    let threw = null;
    try {
      env.logger.logError('坏对象', bad);
      env.logger.logError(bad);
      env.logger.flush();
    } catch (e) { threw = e; }
    check('没有异常冒到调用方', threw === null, threw && threw.message);
    check('后续仍可正常工作（计数是数字）', typeof env.logger._debug().queued === 'number');
  }

  section('小程序端 console.error 接管：转发 error、保留原输出、不转发 console.log');
  {
    const env = makeMpEnv();
    env.logger.install();
    const installed = env.logger._debug().installed;
    check('install() 幂等且标记已安装', installed === true && env.logger.install() === true);
    env.console.error('业务报错 A');
    check('console.error 被转发入队', env.logger._debug().queued === 1,
      JSON.stringify(env.logger._debug()));
    env.console.log('普通调试日志');
    check('console.log 不转发（否则会灌满队列）', env.logger._debug().queued === 1);
    check('原 console.error 输出仍保留', env.printed.some(function (s) { return s.indexOf('业务报错 A') >= 0; }),
      JSON.stringify(env.printed));
    env.console.error(new Error('对象形态的错误'));
    check('console.error(Error) 也能入队', env.logger._debug().queued === 2);
    check('只装了一个 3s 定时器（install 不重复起）', env.timers.length === 1, String(env.timers.length));
  }

  section('小程序端 发送契约：直连 wx.request / POST / Bearer / 8s 超时');
  {
    const env = makeMpEnv();
    env.logger.logError('契约检查');
    env.logger.flush();
    const req = env.requests[0];
    check('直连 CONFIG.BASE_URL + /api/log/report',
      req.url === 'https://log-test.invalid/api/log/report', req.url);
    check('method = POST', req.method === 'POST', String(req.method));
    check('带 Authorization: Bearer <token>',
      req.header && req.header.Authorization === 'Bearer token-hn-123', JSON.stringify(req.header));
    check('带 8s 超时（不会挂死）', req.timeout === 8000, String(req.timeout));
    check('一次请求只带一条 message（字符串）', typeof req.data.message === 'string');
  }

  /* ======================= Android App 端 ======================= */
  section('APP 端 安装钩子：window.onerror / unhandledrejection / fetch');
  {
    const env = makeAppEnv();
    check('logger 挂到 window.HNLogger', !!env.logger && typeof env.logger.install === 'function');
    check('install 前没有监听器', (env.listeners.unhandledrejection || []).length === 0);
    env.logger.install();
    check('装了 unhandledrejection 监听', (env.listeners.unhandledrejection || []).length === 1,
      JSON.stringify(Object.keys(env.listeners)));
    check('包装了 window.onerror', typeof env.win.onerror === 'function');
    check('包装了 window.fetch', env.win.fetch.__hnLoggerPatched === true);
    check('install 幂等（不重复包 onerror）', env.logger.install() === true && env.win.fetch.__hnLoggerPatched === true);
  }

  section('APP 端 ① 级别白名单 + 消息格式');
  {
    const env = makeAppEnv({ token: 'app-token', views: [{ id: 'page-print', style: {} }] });
    env.logger.report('debug', '非白名单');
    env.logger.flush();
    const body = env.logger && reportBodies(env.calls)[0];
    check('发出一条且级别归一成 error', body && body.level === 'error', JSON.stringify(body));
    check('message 以 [app/版本] 页面路径 开头',
      body && body.message.indexOf('[app/1.1.50] page-print |') === 0, body && body.message);
  }

  section('APP 端 ② message 截断 ≤4000 且带「已截断」');
  {
    const env = makeAppEnv({ token: 'app-token' });
    env.logger.logError('y'.repeat(9000));
    env.logger.flush();
    const body = reportBodies(env.calls)[0];
    check('message 长度 ≤4000', body.message.length <= MAX_MESSAGE_LEN, String(body.message.length));
    check('截断标记在末尾', /…（已截断）$/.test(body.message), body.message.slice(-12));
  }

  section('APP 端 ③ 无 token 时零请求');
  {
    const env = makeAppEnv({ token: '', stateToken: '' });
    env.logger.logError('错误 A');
    let sent = 0;
    for (let i = 0; i < 5; i++) if (env.logger.flush()) sent += 1;
    check('flush 不发送', sent === 0 && env.calls.length === 0, String(env.calls.length));
    check('条目留在队列', env.logger._debug().queued === 1);
  }

  section('APP 端 ④ 客户端限速（连发 10 条只出 1 条）');
  {
    const env = makeAppEnv({ token: 'app-token' });
    for (let i = 0; i < 10; i++) env.logger.logError('连续错误 #' + i);
    let sent = 0;
    for (let i = 0; i < 10; i++) if (env.logger.flush()) sent += 1;
    check('10 次 flush 只发出 1 条', sent === 1 && env.calls.length === 1,
      'sent=' + sent + ' calls=' + env.calls.length);
    check('剩余 9 条仍在队列', env.logger._debug().queued === 9);
  }

  section('APP 端 ⑤ 内存队列上限 20，不无限增长');
  {
    const env = makeAppEnv({ token: '' });
    for (let i = 0; i < 64; i++) env.logger.logError('灌队列 #' + i);
    const dbg = env.logger._debug();
    check('队列长度正好 20', dbg.queued === 20, String(dbg.queued));
    check('丢弃计数 = 64 - 20', dbg.dropped === 44, String(dbg.dropped));
  }

  section('APP 端 ⑥ 脱敏：?token=abc → ?token=***');
  {
    const env = makeAppEnv({ token: 'app-token' });
    env.logger.logError('请求 https://hn-space.cn/api/me?token=abc&x=1 失败');
    env.logger.flush();
    const msg = reportBodies(env.calls).pop().message;
    check('敏感 query 值被抹成 ***', msg.indexOf('?token=***') >= 0, msg);
    check('原始 token 值不再出现', msg.indexOf('abc') < 0, msg);
  }

  section('APP 端 ⑦ 堆栈只取首个 at 帧，消息压成一行');
  {
    const env = makeAppEnv({ token: 'app-token' });
    const err = new Error('x is not a function');
    err.name = 'TypeError';
    err.stack = 'TypeError: x is not a function\n    at doPrint (print.js:12:3)\n    at print.js:99:9';
    env.logger.logError(err);
    env.logger.flush();
    const msg = reportBodies(env.calls)[0].message;
    check('带错误名与消息', msg.indexOf('TypeError: x is not a function') >= 0, msg);
    check('只保留首个 at 帧', msg.indexOf('print.js:12:3') >= 0 && msg.indexOf('99:9') < 0, msg);
    check('整条消息没有换行', !/[\r\n]/.test(msg));
  }

  section('APP 端 ⑧ logger 自身异常不炸调用方');
  {
    const env = makeAppEnv();
    const bad = { toJSON() { throw new Error('toJSON 炸了'); }, toString() { throw new Error('toString 炸了'); } };
    let threw = null;
    try {
      env.logger.logError('坏对象', bad);
      env.logger.logError(bad);
      env.logger.flush();
    } catch (e) { threw = e; }
    check('没有异常冒到调用方', threw === null, threw && threw.message);
  }

  section('APP 端 window.onerror 钩子：脚本 URL 里的 token 不外泄');
  {
    const env = makeAppEnv({ token: 'app-token' });
    env.logger.install();
    env.win.onerror('Uncaught ReferenceError: x is not defined',
      'https://log-test.invalid/print.js?token=abc123', 12, 3, null);
    check('onerror 入队 1 条', env.logger._debug().queued === 1);
    env.logger.flush();
    const msg = reportBodies(env.calls)[0].message;
    check('消息里有脚本名与行列号', msg.indexOf('print.js:12:3') >= 0, msg);
    check('脚本 URL 的 token 值被丢掉', msg.indexOf('abc123') < 0, msg);
  }

  section('APP 端 unhandledrejection 钩子');
  {
    const env = makeAppEnv({ token: 'app-token' });
    env.logger.install();
    const fn = (env.listeners.unhandledrejection || [])[0];
    fn({ reason: new TypeError('promise 挂了') });
    check('未处理拒绝入队', env.logger._debug().queued === 1);
    env.logger.flush();
    const msg = reportBodies(env.calls)[0].message;
    check('拒绝原因进消息', msg.indexOf('TypeError: promise 挂了') >= 0 ||
      msg.indexOf('promise 挂了') >= 0, msg);
  }

  section('APP 端 fetch 兜底：网络失败上报，且不改调用方语义');
  {
    const env = makeAppEnv({ token: 'app-token', fetchImpl: () => Promise.reject(new TypeError('Failed to fetch')) });
    env.logger.install();
    let caught = null;
    await env.win.fetch('https://log-test.invalid/api/me?token=zzz').catch(function (e) { caught = e; });
    check('调用方仍收到 reject（语义不变）', caught instanceof Error, caught && caught.message);
    check('网络失败被兜底入队', env.logger._debug().queued === 1);
    env.logger.flush();
    const sent = env.calls.filter(function (c) { return c.url.indexOf('/api/log/report') >= 0; });
    check('上报请求只走原始 fetch 一次（不被自己的包装器再捕获）', sent.length === 1, String(sent.length));
    const msg = reportBodies(sent)[0].message;
    check('消息含「网络错误」+ 方法/路径', msg.indexOf('网络错误') >= 0 && msg.indexOf('/api/me') >= 0, msg);
    check('路径里的 token 值不外泄', msg.indexOf('zzz') < 0, msg);
    check('上报完队列清空（没有自我递归）', env.logger._debug().queued === 0,
      JSON.stringify(env.logger._debug()));
  }

  section('APP 端 fetch 兜底：5xx 上报、401/4xx 不报（避免登录噪音）');
  {
    const env5 = makeAppEnv({ fetchImpl: () => Promise.resolve({ status: 503 }) });
    env5.logger.install();
    await env5.win.fetch('/api/print');
    check('5xx 入队', env5.logger._debug().queued === 1, JSON.stringify(env5.logger._debug()));

    const env4 = makeAppEnv({ fetchImpl: () => Promise.resolve({ status: 401 }) });
    env4.logger.install();
    await env4.win.fetch('/api/me');
    check('401 不入队（登录态问题由 api() 自己处理）', env4.logger._debug().queued === 0);
  }

  section('APP 端 token / 基址来源与 app.js 一致');
  {
    const env = makeAppEnv({ token: 'ls-token', stateToken: 'state-token' });
    env.logger.logError('token 优先级');
    env.logger.flush();
    const c = env.calls[0];
    check('state.token 优先于 localStorage', c.opts.headers.Authorization === 'Bearer state-token',
      c.opts.headers.Authorization);
    check('基址用 app.js 的 BASE_URL（含测试钩子）',
      c.url === 'https://log-test.invalid/api/log/report', c.url);
    check('Content-Type 是 application/json', c.opts.headers['Content-Type'] === 'application/json',
      JSON.stringify(c.opts.headers));
  }

  /* ======================= 接入面（静态） ======================= */
  section('接入面：两个 app.js / index.html 真的装了 logger');
  {
    const mpApp = fs.readFileSync(path.join(MP_DIR, 'app.js'), 'utf8');
    check('小程序 app.js require 了 utils/logger',
      /require\(['"]\.\/utils\/logger['"]\)/.test(mpApp));
    check('小程序 app.js onLaunch 调 logger.install()', /logger\.install\(\)/.test(mpApp));
    check('小程序 app.js 定义了 onError', /onError\s*\(/.test(mpApp));
    check('小程序 app.js 定义了 onUnhandledRejection', /onUnhandledRejection\s*\(/.test(mpApp));
    check('小程序 logger 没有走统一请求封装（防 401 续登风暴）',
      !/require\(['"]\.\/request['"]\)/.test(fs.readFileSync(MP_LOGGER, 'utf8')));

    const appJs = fs.readFileSync(path.join(APP_WWW, 'app.js'), 'utf8');
    check('App app.js 的 initApp 调用了 HNLogger.install()',
      /HNLogger[\s\S]{0,40}install\(\)/.test(appJs));
    const appLoggerSrc = fs.readFileSync(APP_LOGGER, 'utf8');
    check('App logger 直连 fetch、不走 app.js 的 api()（防 401 续登风暴）',
      /doFetch|window\.fetch/.test(appLoggerSrc) && !/(^|[^.\w])api\(\s*['"`]/.test(appLoggerSrc));

    const html = fs.readFileSync(path.join(APP_WWW, 'index.html'), 'utf8');
    const iApp = html.indexOf('<script src="app.js"></script>');
    const iLog = html.indexOf('<script src="logger.js"></script>');
    const iUpd = html.indexOf('<script src="updater.js"></script>');
    check('index.html 在 app.js 之后、updater.js 之前加载 logger.js',
      iApp >= 0 && iLog > iApp && iUpd > iLog, [iApp, iLog, iUpd].join(','));
  }

  console.log('\n共 ' + checks + ' 条断言，失败 ' + failures + ' 条');
  console.log(failures ? '存在失败项 ❌' : '全部通过 ✅');
  process.exit(failures ? 1 : 0);
}

main().catch(function (e) {
  console.error('[FATAL] 验收脚本自身异常: ' + (e && e.stack ? e.stack : e));
  process.exit(1);
});
