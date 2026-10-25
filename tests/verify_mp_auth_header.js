/* eslint-disable */
/**
 * 小程序 utils/request.js —— 401「静默重登录 → 重放」真的带上了新 token 吗？
 *
 * 线上形状（2026-10-25 微信开发者工具实测）：
 *   `GET /api/my_files` 401 → request.js 触发 wx.login + /api/login 成功 → 重放 → **又 401**。
 *   `loadMyFiles` / `onDeleteMyFile` 两处调用**根本没传 header**，封装本身也不注入；
 *   更糟的是 401 分支里 `const header = Object.assign({}, options.header)` 构造出来的新头
 *   **从未被使用**（attempt() 里发的是 `options.header`）—— 所以"续登重放"整条路径从来
 *   没把刷新后的 token 发出去：漏带的永远漏带，带旧 token 的永远发旧 token。
 *
 * 本脚本钉住（用真实 request.js + 桩 wx）：
 *  ① 调用方**完全没传 header**（如 /api/my_files）→ 重放必须带 `Bearer <新 token>`；
 *  ② 调用方传了**过期 token** → 重放必须是**新 token**，不是旧 token；
 *  ③ 登录失败时不重放、不吞错，按 statusCode 401 交给 fail；
 *  ④ 200 时不登录、不重放（零回归）；
 *  ⑤ 静态兜底：me.js 两处（my_files / 删除文件）必须走 request()，且封装里确有 Bearer 注入。
 *
 * 用法：node tests/verify_mp_auth_header.js      （exit 0 = 全绿）
 * 反向验证：把 attempt() 的 header 注入删掉、或把重放改回 options.header → ①②③ 立刻转红。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..');
const MP = path.join(REPO, 'mobile_apps', 'h_n_print');
const SRC = path.join(MP, 'utils', 'request.js');
const ME_JS = path.join(MP, 'pages', 'me', 'me.js');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? `  -- ${detail}` : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }
// 续登是 Promise.then 里的微任务 ⇒ 断言前必须让微任务跑完
const tick = () => new Promise((r) => setImmediate(r));

/**
 * 载入真实 request.js。`respond(ctx)` 决定每次 wx.request 的结果：
 * ctx = { url, header, seq } → { statusCode, data } | { failMsg }
 */
function loadWrapper(respond, opts) {
  const options = opts || {};
  const storage = Object.assign({}, options.storage || {});
  const calls = [];
  const logins = [];
  const sandbox = {
    console,
    setTimeout: (fn) => { fn(); return 0; },   // 重放间隔（500/1500ms）就地执行，测试不等待
    clearTimeout: () => {},
    Promise, Date,
    module: { exports: {} },
    getApp: () => ({ globalData: {} }),
    require: (p) => (p === './config' ? { CONFIG: { BASE_URL: 'https://stub.test' } } : {}),
    wx: {
      getStorageSync: (k) => (k in storage ? storage[k] : ''),
      setStorageSync: (k, v) => { storage[k] = v; },
      login: (o) => { logins.push(o); o.success({ code: 'CODE-' + logins.length }); },
      request: (o) => {
        calls.push({ url: o.url, header: o.header, seq: calls.length });
        const res = respond({ url: o.url, header: o.header, seq: calls.length - 1 });
        if (res && res.failMsg) {
          if (o.fail) o.fail({ errMsg: res.failMsg });
          if (o.complete) o.complete({ errMsg: res.failMsg });
          return;
        }
        if (o.success) o.success(res);
        if (o.complete) o.complete(res);
      },
    },
  };
  sandbox.exports = sandbox.module.exports;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SRC, 'utf8'), sandbox, { filename: 'utils/request.js' });
  if (typeof sandbox.module.exports.request !== 'function') {
    throw new Error('request.js 未导出 request()');
  }
  return { api: sandbox.module.exports, calls, logins, storage };
}

const authOf = (call) => ((call && call.header) || {})['Authorization'];

async function main() {
  /* ---------------- ① 调用方完全没传 header ---------------- */
  section('① 没传 header（/api/my_files 的真实形状）→ 重放必须带上新 token');
  {
    const ctx = loadWrapper(({ url, seq }) => {
      if (url.indexOf('/api/login') !== -1) {
        return { statusCode: 200, data: { success: true, token: 'NEW', openid: 'o1' } };
      }
      return seq === 0
        ? { statusCode: 401, data: { success: false, message: '未登录，请先授权' } }
        : { statusCode: 200, data: { success: true, files: [] } };
    }, { storage: { token: 'OLD' } });

    let gotStatus = null, failCalled = null;
    ctx.api.request({
      url: 'https://stub.test/api/my_files?page=1&per_page=20',
      method: 'GET',
      success: (res) => { gotStatus = res.statusCode; },
      fail: (e) => { failCalled = e; },
    });
    await tick();

    const replayed = ctx.calls.filter((c) => c.url.indexOf('/api/my_files') !== -1);
    check('发生了 401 续登（wx.login 1 次 / 原请求共 2 次）',
      ctx.logins.length === 1 && replayed.length === 2,
      `logins=${ctx.logins.length} 原请求=${replayed.length}`);
    check('★ 重放请求带了 Authorization', !!authOf(replayed[1]),
      JSON.stringify(replayed[1] && replayed[1].header));
    check('★ 重放带的是**新** token（Bearer NEW）', authOf(replayed[1]) === 'Bearer NEW',
      String(authOf(replayed[1])));
    check('重放成功后回调拿到 200、且没有走 fail', gotStatus === 200 && !failCalled,
      `status=${gotStatus} fail=${failCalled && failCalled.errMsg}`);
  }

  /* ---------------- ② 调用方传了过期 token ---------------- */
  section('② 调用方自己带了**过期** token → 重放必须是新 token');
  {
    const ctx = loadWrapper(({ url, seq }) => {
      if (url.indexOf('/api/login') !== -1) {
        return { statusCode: 200, data: { success: true, token: 'FRESH', openid: 'o2' } };
      }
      return seq === 0
        ? { statusCode: 401, data: { success: false, message: '登录已过期，请重新登录' } }
        : { statusCode: 200, data: { success: true, role: 'user' } };
    }, { storage: { token: 'STALE' } });

    let ok = false;
    ctx.api.request({
      url: 'https://stub.test/api/profile',
      method: 'GET',
      header: { Authorization: 'Bearer STALE' },
      success: (res) => { ok = res.statusCode === 200; },
    });
    await tick();

    const replayed = ctx.calls.filter((c) => c.url.indexOf('/api/profile') !== -1);
    check('发生了续登并重放一次', ctx.logins.length === 1 && replayed.length === 2,
      `logins=${ctx.logins.length} 原请求=${replayed.length}`);
    check('★ 重放用的是新 token（不是原来的 STALE）', authOf(replayed[1]) === 'Bearer FRESH',
      String(authOf(replayed[1])));
    check('调用方传的原始 header 没被就地篡改（首跳仍是 STALE）',
      authOf(replayed[0]) === 'Bearer STALE', String(authOf(replayed[0])));
    check('最终回调成功', ok);
  }

  /* ---------------- ③ 登录失败 ---------------- */
  section('③ 续登失败 → 不重放、把 401 交给 fail');
  {
    const ctx = loadWrapper(({ url }) => {
      if (url.indexOf('/api/login') !== -1) return { statusCode: 429, data: '<html>429</html>' };
      return { statusCode: 401, data: { success: false, message: '未登录' } };
    });

    let failCalled = null, successCalled = 0;
    ctx.api.request({
      url: 'https://stub.test/api/orders',
      method: 'GET',
      success: () => { successCalled++; },
      fail: (e) => { failCalled = e; },
    });
    await tick();

    const attempts = ctx.calls.filter((c) => c.url.indexOf('/api/orders') !== -1);
    check('只发了 1 次原请求（没有拿旧头硬重放）', attempts.length === 1, `实际 ${attempts.length} 次`);
    check('走了 fail 且带 statusCode=401', !!failCalled && failCalled.statusCode === 401,
      JSON.stringify(failCalled));
    check('没有误当成功', successCalled === 0, `success ${successCalled} 次`);
  }

  /* ---------------- ④ 200 零回归 ---------------- */
  section('④ 200 正常响应 → 不登录、不重放');
  {
    const ctx = loadWrapper(() => ({ statusCode: 200, data: { success: true } }),
      { storage: { token: 'T' } });
    let status = null;
    ctx.api.request({
      url: 'https://stub.test/api/pricing',
      method: 'GET',
      success: (res) => { status = res.statusCode; },
    });
    await tick();
    check('只发 1 次、没有多余登录', ctx.calls.length === 1 && ctx.logins.length === 0,
      `calls=${ctx.calls.length} logins=${ctx.logins.length}`);
    check('拿到 200', status === 200);
  }

  /* ---------------- ⑤ 静态兜底 ---------------- */
  section('⑤ 静态：me.js 两处调用走 request()，封装里确有 Bearer 注入');
  {
    const me = fs.readFileSync(ME_JS, 'utf8');
    function blockOf(marker) {
      const i = me.indexOf(marker);
      if (i < 0) return null;
      const start = me.lastIndexOf('request({', i);
      if (start < 0) return null;
      let depth = 0, j = start + 'request('.length;
      for (; j < me.length; j++) {
        if (me[j] === '{') depth++;
        else if (me[j] === '}') { depth--; if (depth === 0) break; }
      }
      return me.slice(start, j + 1);
    }
    const myFiles = blockOf("'/api/my_files?page='");
    const del = blockOf("'/api/file/' + encodeURIComponent");
    check('找到 my_files 调用点', !!myFiles);
    check('找到删除文件调用点', !!del);
    check('两处都走 request()（不是 wx.request）',
      !!myFiles && !!del && myFiles.indexOf('wx.request(') === -1 && del.indexOf('wx.request(') === -1);

    const wrapper = fs.readFileSync(SRC, 'utf8');
    // 判据：发送前（attempt 内）注入 Bearer，而不是 401 分支里那个用不上的局部变量
    const inject = /header\s*\[\s*['"]Authorization['"]\s*\]\s*=\s*['"]Bearer\s*['"]\s*\+/
      .test(wrapper);
    const sendsComputedHeader = /wx\.request\(\{[\s\S]{0,600}?header:\s*(?:buildHeader\(\)|header\b)/
      .test(wrapper);
    check('★ 封装在发送前注入 Authorization（新调用漏写也不会 401）', inject);
    check('★ attempt() 发的是算好的 header（不是 options.header 原样）', sendsComputedHeader);
  }

  console.log('\n' + '='.repeat(60));
  console.log('小程序 401 续登重放:', failures === 0 ? '全部通过 ✅' : `${failures} 项失败 ❌`);
  process.exit(failures === 0 ? 0 : 1);
}

main();
