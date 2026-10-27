/* eslint-disable */
/**
 * APP「检查更新」的错误分类 + 退避重试 · 验收（2026-10-27）
 *
 * 现场：APP 1.1.44 点「检查更新」永远弹「检查更新失败：网络错误」，用户以为手机没网、
 * 以为要自己去下安装包。真实根因在服务端 —— nginx 把 /updates/ 与 /api/ 放同一个限流桶
 * （hn_api 60r/m），App 自身轮询就 ~2.9 r/s ⇒ 桶长期满 ⇒ 更新清单一律 **HTTP 429**。
 * 页面把非 ok 一律写成「网络错误」，把"服务端限流"伪装成"手机网络坏了"。
 *
 * 本脚本用**真实源码 + 桩环境**钉住三件事：
 *   ① 429 与 5xx 要说「服务器繁忙 / 服务器错误」，不能再说「网络错误」；
 *   ② 429 / 5xx 退避 1.5s **重试一次**（第二次成功就照常走更新流程）；
 *   ③ 真正的网络异常（fetch reject）/ 4xx 仍按原样报错，且**不**重试（不放大服务端压力）。
 * 末尾变异自证：换成 git HEAD 的旧 updater.js，① 必须转红（证明判据不是恒真）。
 *
 * 用法：node tests/verify_app_update_errors.js   （exit 0 = 全绿）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { execFileSync } = require('child_process');

const REPO = path.resolve(__dirname, '..');
const UPDATER = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'updater.js');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }

/**
 * 在 vm 里跑一份 updater.js（真实源码），返回本次运行的探针。
 * @param {string} src            updater.js 源码
 * @param {Array}  script         fetch 的脚本序列：[{status:200, body:{...}} | {throw:'net'}]
 * @param {number} versionCode    本机 versionCode
 */
function runUpdater(src, script, versionCode = 45) {
  const calls = [];
  const toasts = [];
  const confirms = [];
  const opened = [];
  let i = 0;

  const fakeFetch = (url) => {
    calls.push(url);
    const step = script[Math.min(i, script.length - 1)];
    i++;
    if (step.throw) return Promise.reject(new TypeError('网络异常'));
    const status = step.status;
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      redirected: false,
      json: () => Promise.resolve(step.body || {}),
    });
  };

  const sandbox = {
    BASE_URL: 'https://hn-space.cn',
    console,
    URL,
    AbortController,
    Promise,
    Number,
    String,
    Math,
    Date,
    // 定时器：重试用的 1.5s 立刻跑；updater.js 末尾「启动后 5s 静默检查」直接不跑（避免串进用例）
    setTimeout: (fn, ms) => (ms >= 4000 ? 0 : setImmediate(fn)),
    clearTimeout: () => {},
    fetch: fakeFetch,
    showToast: (msg, ms) => { toasts.push(msg); return ms; },
    showConfirm: (title, msg, btn, color, cb) => { confirms.push({ title, msg, btn }); if (cb) cb(); },
    window: {
      AppUpdater: {
        getVersionCode: () => versionCode,
        getVersionName: () => '1.1.44',
        openUrl: (u) => { opened.push(u); },
      },
      addEventListener: () => {},
      open: (u) => { opened.push(u); },
    },
    document: { addEventListener: () => {}, hidden: false },
  };
  sandbox.window.window = sandbox.window;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: 'updater.js' });
  return { sandbox, calls, toasts, confirms, opened };
}

const MANIFEST_NEW = {
  version: '1.1.48',
  versionCode: 49,
  url: 'https://hn-space.cn/updates/h_n_printer_android_v1.1.48.apk',
  md5: 'befe7d0928855fd122756743633d26bf',
  notes: 'x',
};

async function main() {
  const src = fs.readFileSync(UPDATER, 'utf8');

  section('① 429：文案必须点明「限流」，且退避重试一次');
  {
    const r = runUpdater(src, [{ status: 429 }]);
    await r.sandbox.checkAppUpdate(true);
    check('429 → 提示服务器繁忙/限流（不再说「网络错误」）',
      r.toasts.some((m) => /429/.test(m) && /繁忙|限流/.test(m) && !/网络错误/.test(m)),
      JSON.stringify(r.toasts));
    check('429 → 重试一次（fetch 共 2 次）', r.calls.length === 2, `calls=${r.calls.length}`);
    check('// 仍然打的是更新清单地址（没被改到别处）',
      r.calls.every((u) => u.indexOf('https://hn-space.cn/updates/app_update.json') === 0),
      r.calls[0]);
  }

  section('② 429 → 重试成功：照常走「发现新版本」流程');
  {
    const r = runUpdater(src, [{ status: 429 }, { status: 200, body: MANIFEST_NEW }]);
    await r.sandbox.checkAppUpdate(true);
    check('第 2 次成功 → 弹出更新确认框（v1.1.48）',
      r.confirms.length === 1 && /1\.1\.48/.test(r.confirms[0].title), JSON.stringify(r.confirms));
    check('确认框走的是白名单内的 APK 地址（打开下载页）',
      r.opened.length === 1 && r.opened[0].indexOf('https://hn-space.cn/updates/') === 0,
      r.opened[0]);
    check('没有多余的报错 toast', r.toasts.every((m) => !/失败/.test(m)), JSON.stringify(r.toasts));
  }

  section('③ 5xx：说「服务器错误」并重试一次');
  {
    const r = runUpdater(src, [{ status: 502 }]);
    await r.sandbox.checkAppUpdate(true);
    check('502 → 提示服务器错误（带状态码），不说「网络错误」',
      r.toasts.some((m) => /502/.test(m) && /服务器错误/.test(m)), JSON.stringify(r.toasts));
    check('502 → 重试一次（fetch 共 2 次）', r.calls.length === 2, `calls=${r.calls.length}`);
  }

  section('④ 真网络异常 / 4xx：原样报错，且**不**重试');
  {
    const r = runUpdater(src, [{ throw: 'net' }]);
    await r.sandbox.checkAppUpdate(true);
    check('fetch reject → 「网络错误」', r.toasts.some((m) => /网络错误/.test(m)), JSON.stringify(r.toasts));
    check('fetch reject → 不重试（calls === 1）', r.calls.length === 1, `calls=${r.calls.length}`);
  }
  {
    const r = runUpdater(src, [{ status: 404 }]);
    await r.sandbox.checkAppUpdate(true);
    check('404 → 提示 HTTP 404', r.toasts.some((m) => /HTTP 404/.test(m)), JSON.stringify(r.toasts));
    check('404 → 不重试（calls === 1）', r.calls.length === 1, `calls=${r.calls.length}`);
  }

  section('⑤ 正常路径零回归：已是最新版本 / 自动检查静默');
  {
    const r = runUpdater(src, [{ status: 200, body: Object.assign({}, MANIFEST_NEW, { versionCode: 45 }) }]);
    await r.sandbox.checkAppUpdate(true);
    check('versionCode 等于本机 → 「已是最新版本」',
      r.toasts.some((m) => /已是最新版本/.test(m)), JSON.stringify(r.toasts));
    check('不弹确认框', r.confirms.length === 0);
  }
  {
    const r = runUpdater(src, [{ status: 429 }]);
    await r.sandbox.checkAppUpdate(false);   // manual=false（启动静默检查）
    check('静默检查失败不打扰用户（无 toast）', r.toasts.length === 0, JSON.stringify(r.toasts));
  }

  section('⑥ 变异自证：换成 git HEAD 的旧 updater.js，①② 必须转红');
  let oldSrc = '';
  try {
    oldSrc = execFileSync('git', ['show', 'HEAD:mobile_apps/android_app/www/updater.js'],
      { cwd: REPO, encoding: 'utf8' });
  } catch (e) { /* 无 git / 无该文件 */ }
  if (!oldSrc) {
    check('可读取 git HEAD 的旧 updater.js', false, 'git show 失败');
  } else {
    const r = runUpdater(oldSrc, [{ status: 429 }]);
    await r.sandbox.checkAppUpdate(true);
    const oldSaysNetworkError = r.toasts.some((m) => /网络错误/.test(m));
    const oldNoRetry = r.calls.length === 1;
    check('旧版本对 429 只说「网络错误」（= 现场那次误判）', oldSaysNetworkError, JSON.stringify(r.toasts));
    check('旧版本不重试（calls === 1）', oldNoRetry, `calls=${r.calls.length}`);
    check('判据具有区分度（新代码不再满足旧行为）',
      !(oldSaysNetworkError === false && oldNoRetry === false));
  }

  console.log('\n' + (failures ? `存在失败项（${failures}）` : '全部通过'));
  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
