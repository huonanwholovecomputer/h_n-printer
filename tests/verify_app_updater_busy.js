/* eslint-disable */
/**
 * App 自更新「更新锁」验收（D5 第 12 条）
 *
 * 线上形状：用户在 APP 里点「立即更新」→ 系统安装器弹出 → 用户点「取消」返回 APP。
 * 旧实现 `_updateBusy = true` **只在 `status === 'error'` 复位**，取消安装没有任何回调回来
 * ⇒ 锁永久持有，`checkAppUpdate()` 第一行 `return`，表现为「检查更新」永久无响应
 * （连提示都没有，用户只会以为按钮坏了，只能重启 APP）。
 *
 * 修复后本脚本钉住：
 *  ① 确认更新后持锁（并发的第二次调用不得发起新的清单请求）；
 *  ② **从系统安装器返回（visibilitychange / resume / focus）必须解锁** —— 这是"取消安装"的
 *     真实信号（真装成功时应用被替换重启，走不到这里）；
 *  ③ 兜底超时也能解锁（系统安装器/厂商 ROM 不派发任何回调时的最后一道保险）；
 *  ④ error 回调仍解锁（零回归）；
 *  ⑤ 忙时手动点击给出「正在更新中」的提示，而不是静默吞掉；
 *  ⑥ 静态判据：确认/安装阶段都必须走 `_setUpdateBusy(...)`（不许再出现裸的 `_updateBusy = true`）。
 *
 * 用真实 www/updater.js + 桩环境（fetch / showToast / showConfirm / AppUpdater / document）。
 * 用法：node tests/verify_app_updater_busy.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..');
const UPDATER = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'updater.js');
const SRC = fs.readFileSync(UPDATER, 'utf8');   // **失败关闭**：读不到直接抛（不静默放行）

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? `  -- ${detail}` : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }

/** 造一个沙箱并加载真实 updater.js；返回值里带可观察的桩状态。 */
function makeEnv(manifest, currentCode) {
  const st = {
    fetchCalls: 0,
    toasts: [],
    confirmCb: null,
    installCalls: [],
    openCalls: [],
    listeners: {},
  };
  const documentStub = {
    hidden: false,
    addEventListener(type, fn) { (st.listeners['document:' + type] = st.listeners['document:' + type] || []).push(fn); },
  };
  const windowStub = {
    AppUpdater: {
      getVersionCode: () => currentCode,
      getVersionName: () => '1.0.0',
      // S1-02（2026-10-23）：updater.js 改为打开下载页，不再调 downloadAndInstall；
      // 这里同时留两个桩，用来断言「旧静默安装路径一次都没被调用」。
      downloadAndInstall: (...a) => st.installCalls.push(a),
      openUrl: (...a) => st.openCalls.push(a),
    },
    addEventListener(type, fn) { (st.listeners['window:' + type] = st.listeners['window:' + type] || []).push(fn); },
  };
  const sandbox = {
    window: windowStub,
    document: documentStub,
    BASE_URL: 'https://example.invalid',
    URL,                          // updater.js 用 new URL() 做 host 白名单校验（vm 上下文没有该全局）
    setTimeout: () => 0,          // 启动 5s 静默检查与兜底超时都由测试显式驱动，不真的计时
    clearTimeout: () => {},
    console,
    showToast: (msg, ms) => st.toasts.push(String(msg)),
    showConfirm: (title, body, ok, color, cb) => { st.confirmCb = cb; },
    fetch: async (url, opt) => {
      st.fetchCalls++;
      if (manifest === null) throw new Error('boom');
      return { ok: true, json: async () => manifest };
    },
  };
  sandbox.window.setTimeout = sandbox.setTimeout;
  sandbox.window.clearTimeout = sandbox.clearTimeout;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox, { filename: 'updater.js' });
  st.ctx = sandbox;
  return st;
}

function fire(st, key) {
  (st.listeners[key] || []).forEach((fn) => fn());
}

// S1-02（2026-10-23）：清单 fixture 换成**合法**取值 —— 更新链已要求「https + host 白名单」
// 与「哈希必填（sha256 优先 / md5 兼容）」，`url:'https://cos/1.2.0.apk'` + `md5:'abc'`
// 在新代码里会被正确拒绝（那样本脚本的 ①~④ 就测不到更新锁了）。
const MANIFEST = {
  version: '1.2.0',
  versionCode: 99,
  url: 'https://hn-space.cn/updates/h_n-printer_android_v1.2.0.apk',
  md5: '0123456789abcdef0123456789abcdef',
  notes: 'x',
};

(async () => {
  section('① 确认更新后持锁，并发调用不重复请求清单');
  {
    const st = makeEnv(MANIFEST, 1);
    await st.ctx.checkAppUpdate(true);
    check('首次检查发出 1 次清单请求', st.fetchCalls === 1, `fetch=${st.fetchCalls}`);
    check('发现新版本时弹出确认框', typeof st.confirmCb === 'function');
    st.confirmCb();                          // 用户点「去下载」
    // S1-02（2026-10-23）：应用内静默安装已关闭 ⇒ 不再调用原生 downloadAndInstall，
    // 改为打开下载页（系统浏览器/系统安装器完成安装与确认）。
    check('确认后**不再**调用原生下载安装（应用内静默安装已关闭）',
      st.installCalls.length === 0, JSON.stringify(st.installCalls));
    check('确认后改为打开下载页', st.openCalls.length === 1, JSON.stringify(st.openCalls));
    check('打开的是清单里那一个已过白名单的 APK 地址',
      st.openCalls.length === 1 && String(st.openCalls[0][0]) === MANIFEST.url,
      JSON.stringify(st.openCalls));
    check('★ 持锁：第二次检查不再发请求', (await st.ctx.checkAppUpdate(true), st.fetchCalls === 1),
      `fetch=${st.fetchCalls}`);
    check('★ 忙时手动点击有反馈（旧实现静默吞掉，像按钮坏了）',
      st.toasts.some((t) => t.includes('正在更新中')), JSON.stringify(st.toasts));

    section('② 从系统安装器返回（取消安装）必须解锁 ← 本条修复点');
    fire(st, 'document:visibilitychange');
    check('★ visibilitychange 解锁后，检查更新重新发起清单请求',
      (await st.ctx.checkAppUpdate(true), st.fetchCalls === 2), `fetch=${st.fetchCalls}`);
    check('重新检查仍会弹确认框（功能恢复）', typeof st.confirmCb === 'function');
  }

  section('②b resume / focus 同样解锁（不同 ROM 派发不同事件）');
  for (const evt of ['window:resume', 'window:focus']) {
    const st = makeEnv(MANIFEST, 1);
    await st.ctx.checkAppUpdate(true);
    st.confirmCb();
    const before = st.fetchCalls;
    fire(st, evt);
    await st.ctx.checkAppUpdate(true);
    check(`★ ${evt.replace('window:', '')} 解锁后可重新检查`, st.fetchCalls > before,
      `fetch ${before} → ${st.fetchCalls}`);
  }

  section('③ 兜底超时也能解锁（系统安装器不回调时）');
  {
    // 用显式 timeout 驱动：_setUpdateBusy(true, ms) 内部用 sandbox.setTimeout；
    // 这里直接验证"带超时的置位"确实注册了超时，并且超时回调能解锁。
    const st = makeEnv(MANIFEST, 1);
    // 反向验证时源码退回改前版本、**没有** _setUpdateBusy —— 直接调用会 TypeError 崩在中间
    // （"崩在中间的红"不算红，项目老教训），所以先判存在性、失败也落在断言上。
    const setBusy = st.ctx._setUpdateBusy;
    check('★ 存在统一的更新锁入口 _setUpdateBusy（改前没有）', typeof setBusy === 'function');
    if (typeof setBusy !== 'function') {
      check('兜底超时相关断言（入口缺失 → 记失败）', false, '缺少 _setUpdateBusy');
    } else {
      let captured = null;
      st.ctx.setTimeout = (fn, ms) => { captured = { fn, ms }; return 1; };
      st.ctx.window.setTimeout = st.ctx.setTimeout;
      setBusy(true, 1234);
      check('带超时置位时注册了兜底超时', !!captured && captured.ms === 1234,
        JSON.stringify(captured && captured.ms));
      check('持锁状态下检查更新被拦住', (await st.ctx.checkAppUpdate(true), st.fetchCalls === 0),
        `fetch=${st.fetchCalls}`);
      captured.fn();                           // 模拟超时触发
      await st.ctx.checkAppUpdate(true);
      check('★ 超时后自动解锁并可重新检查', st.fetchCalls === 1, `fetch=${st.fetchCalls}`);
    }
    check('兜底超时常量存在（10 分钟级）',
      /UPDATE_BUSY_TIMEOUT_MS\s*=\s*10\s*\*\s*60\s*\*\s*1000/.test(SRC));
  }

  section('④ error 回调仍解锁（零回归）');
  {
    const st = makeEnv(MANIFEST, 1);
    await st.ctx.checkAppUpdate(true);
    st.confirmCb();
    st.ctx.window.hnUpdateProgress('error', '下载失败');
    await st.ctx.checkAppUpdate(true);
    check('error 解锁后可重新检查', st.fetchCalls === 2, `fetch=${st.fetchCalls}`);
    check('error 有 toast 提示', st.toasts.some((t) => t.includes('下载失败')), JSON.stringify(st.toasts));
  }

  section('⑤ 其它回归：已是最新 / 无原生桥 / 清单请求失败');
  {
    const st = makeEnv({ ...MANIFEST, versionCode: 1 }, 1);
    await st.ctx.checkAppUpdate(true);
    check('版本不高于本机 → 提示已是最新且不弹确认框',
      st.toasts.some((t) => t.includes('已是最新版本')) && st.confirmCb === null,
      JSON.stringify(st.toasts));
  }
  {
    const st = makeEnv(MANIFEST, 1);
    delete st.ctx.window.AppUpdater;
    await st.ctx.checkAppUpdate(true);
    check('浏览器预览（无原生桥）→ 提示仅 APP 内可用，且不请求清单',
      st.fetchCalls === 0 && st.toasts.length >= 1,
      `fetch=${st.fetchCalls} ${JSON.stringify(st.toasts)}`);
  }
  {
    const st = makeEnv(null, 1);
    await st.ctx.checkAppUpdate(true);
    check('清单请求失败 → 提示网络错误且不抛异常',
      st.toasts.some((t) => t.includes('网络错误')), JSON.stringify(st.toasts));
  }

  section('⑥ 静态判据：不许再出现裸的 _updateBusy = true');
  {
    const noComment = SRC.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^[ \t]*\/\/.*$/gm, '');
    check('确认路径走 _setUpdateBusy(true, …)', /_setUpdateBusy\(true,\s*UPDATE_BUSY_TIMEOUT_MS\)/.test(noComment));
    check('★ 源码里已无裸 `_updateBusy = true`（注释里引用旧写法不算）',
      !/_updateBusy\s*=\s*true/.test(noComment));
    // S1-02（2026-10-23）：应用内静默安装必须彻底退场（只允许出现在注释里）
    check('★ 源码不再调用 AppUpdater.downloadAndInstall（应用内静默安装已关闭）',
      !/\.downloadAndInstall\s*\(/.test(noComment));
    check('★ 静态：打开下载页（openUrl / window.open）',
      /_openUpdateDownloadPage/.test(noComment) && /openUrl|window\.open/.test(noComment));
  }

  console.log('\n' + (failures ? `存在失败项 ❌ (${failures})` : '全部通过 ✅'));
  process.exit(failures ? 1 : 0);
})();
