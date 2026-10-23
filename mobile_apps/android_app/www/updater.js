/* HN Cloud Print — App 在线更新（打开下载页，**不在应用内静默安装**）
 *
 * S1-02 更新链加固（2026-10-23，docs/安全加固修复计划_2026-10-23.md §S1-02 改动 5）
 *
 * 架构：清单 JSON = {BASE_URL}/updates/app_update.json（服务器 nginx /updates/ 静态目录）
 *      安装包 APK = 腾讯云 COS（MB/s 级分发，不占服务器带宽）
 *
 * 本次收紧的五条（都已落在这里，缺一条都等于没装）：
 *  ① 地址白名单：清单与 APK 都只允许 https + 白名单 host（own 域名 / COS 桶）。
 *  ② 禁止 302：清单请求用 fetch(..., {redirect:'error'}) —— 任何跳转（尤其跨 host）
 *     都直接失败；APK 地址在打开前再校验一遍 scheme/host。
 *  ③ 哈希必填：优先 sha256（64 位十六进制），回退 md5（32 位）；缺失/格式非法 = 拒绝更新
 *     （旧实现把清单里的 md5 原样透传，`''` 等于零校验）。
 *  ④ versionCode 单调递增：必须是正整数且 > 本机，否则不更新（防降级/防乱写）。
 *  ⑤ **关闭应用内静默安装**：不再调用原生 AppUpdater.downloadAndInstall(...)，改为打开
 *     下载页，由系统浏览器 + 系统安装器完成（用户看得到来源、看得到安装确认）。
 *     理由：应用内安装只有 md5 一条校验，而 md5 与 url 都由同一份清单给出 —— 能改清单
 *     就能算出匹配的 md5，所以"应用内静默安装"等于"改清单即可给门店手机装任意 APK"。
 *     即便本轮保留 md5 兼容，也不再让 WebView 直接触发安装；把最终确认交回系统。
 *
 * 原生侧（MainActivity 注入 window.AppUpdater）：
 *   - getVersionCode() / getVersionName()
 *   - openUrl(url) —— 用系统浏览器打开（旧版 APK 没有该方法时回退 window.open）
 *   - downloadAndInstall(...) 仍存在于旧版原生壳里，但**本文件不再调用它**
 * 原生进度回调：window.hnUpdateProgress(status, msg)（保留兼容，不再驱动安装）
 * 浏览器开发预览（无原生桥）时静默跳过。
 *
 * D5 第 12 条：更新锁必须**一定会被释放**。
 *   旧实现 `_updateBusy = true` 只在 `status === 'error'` 复位；而用户点「立即更新」后系统
 *   安装器弹出、用户在系统界面点「取消」时**没有任何回调**回到这里 ⇒ 锁永久持有，
 *   `checkAppUpdate()` 第一行就 return，表现为「检查更新」永久无响应（连提示都没有）。
 *   现改为三路解锁：① error 回调；② 从系统安装器/浏览器返回（visibilitychange / resume）；
 *   ③ 兜底超时。忙时手动点击也会给出「正在更新中」的反馈，而不是静默吞掉。
 */

const UPDATE_MANIFEST_URL = BASE_URL + '/updates/app_update.json';
const UPDATE_BUSY_TIMEOUT_MS = 10 * 60 * 1000;   // 兜底解锁：系统界面不回调时的最后一道保险

// S1-02 ①：更新链只认这两个 host（与本地工具 updater.ALLOWED_DOWNLOAD_HOSTS 同口径）
const UPDATE_ALLOWED_HOSTS = [
  'hn-space.cn',
  'hn-printer-updates-1324480550.cos.ap-guangzhou.myqcloud.com',
];
const UPDATE_ALLOWED_SCHEMES = ['https:'];

let _updateBusy = false;
let _updateBusyTimer = null;

// 统一入口：置位/复位更新锁，并管理兜底超时
function _setUpdateBusy(busy, timeoutMs) {
  _updateBusy = !!busy;
  if (_updateBusyTimer) {
    clearTimeout(_updateBusyTimer);
    _updateBusyTimer = null;
  }
  if (_updateBusy && timeoutMs) {
    _updateBusyTimer = setTimeout(() => {
      _updateBusy = false;
      _updateBusyTimer = null;
    }, timeoutMs);
  }
}

function _isUpdateBusy() {
  return _updateBusy;
}

function isNativeApp() {
  return typeof window !== 'undefined' && typeof window.AppUpdater !== 'undefined';
}

// S1-02 ①/②：更新链地址白名单（scheme + host）。不合法直接返回 null。
function _safeUpdateUrl(raw) {
  var s = String(raw || '').trim();
  if (!s) return null;
  var u;
  try {
    u = new URL(s, BASE_URL);
  } catch (e) {
    return null;
  }
  if (UPDATE_ALLOWED_SCHEMES.indexOf(u.protocol) < 0) return null;
  if (UPDATE_ALLOWED_HOSTS.indexOf(String(u.hostname || '').toLowerCase()) < 0) return null;
  if (u.username || u.password) return null;
  if (u.port && u.port !== '443') return null;
  return u.toString();
}

// S1-02 ③：哈希字段必填，sha256 优先、md5 兼容。缺失或格式非法 → null（调用方拒绝更新）。
function _pickExpectedHash(manifest) {
  var sha = String((manifest && manifest.sha256) || '').trim();
  if (sha) {
    // sha256 存在但格式非法：拒绝，**不**静默回落 md5（那看起来像有人在让我们忽略新字段）
    return /^[0-9a-fA-F]{64}$/.test(sha) ? { algo: 'sha256', hex: sha.toLowerCase() } : null;
  }
  var md5 = String((manifest && manifest.md5) || '').trim();
  if (md5) {
    return /^[0-9a-fA-F]{32}$/.test(md5) ? { algo: 'md5', hex: md5.toLowerCase() } : null;
  }
  return null;
}

// 打开下载页（系统浏览器 / 系统下载管理器），**不**在应用内下载安装。
// 返回 true = 已交给系统；false = 没有可用的打开方式（调用方给出提示）。
function _openUpdateDownloadPage(url) {
  try {
    if (isNativeApp() && typeof window.AppUpdater.openUrl === 'function') {
      window.AppUpdater.openUrl(url);
      return true;
    }
    if (typeof window !== 'undefined' && typeof window.open === 'function') {
      window.open(url, '_blank');
      return true;
    }
    if (typeof window !== 'undefined' && window.location) {
      window.location.href = url;
      return true;
    }
  } catch (e) {
    /* 打不开就返回 false，由调用方提示 */
  }
  return false;
}

// 从系统安装器/浏览器返回 → 解锁。
// 真装成功时应用会被新版本替换重启，根本走不到这里；能走到的就是"用户取消了"。
if (typeof document !== 'undefined' && document.addEventListener) {
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) _setUpdateBusy(false);
  });
}
if (typeof window !== 'undefined' && window.addEventListener) {
  window.addEventListener('resume', function () { _setUpdateBusy(false); });
  window.addEventListener('focus', function () { _setUpdateBusy(false); });
}

// 原生下载/安装进度回调（MainActivity 通过 evaluateJavascript 调用）
// S1-02 ⑤ 起本文件不再发起应用内安装；该回调仅保留给旧原生壳，避免 evaluateJavascript 报错。
window.hnUpdateProgress = function (status, msg) {
  if (status === 'error') {
    showToast(msg || '更新失败', 3000);
    _setUpdateBusy(false);
  } else if (status === 'installing') {
    showToast(msg || '正在安装…', 4000);
    _setUpdateBusy(true, UPDATE_BUSY_TIMEOUT_MS);
  } else {
    showToast(msg || '正在下载…', 3000);
  }
};

async function checkAppUpdate(manual) {
  if (_updateBusy) {
    // 不再静默吞掉：手动点「检查更新」时至少要给出反馈（否则像"按钮坏了"）
    if (manual) showToast('正在更新中，请稍候…', 3000);
    return;
  }
  if (!isNativeApp()) {
    if (manual) showToast('当前为浏览器预览，更新功能仅 APP 内可用');
    return;
  }
  let manifest;
  try {
    // S1-02 ②：redirect:'error' ⇒ 清单被 302 到任何地方都直接失败（跨 host 重定向无从发生）
    // S1-09：裸 fetch 必须自己挂 AbortController 超时（12s）—— 弱网下不设超时会永久挂起，
    // 「检查更新」按钮点了没有任何反应，也没有错误提示。
    const ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    const timer = ctrl ? setTimeout(() => ctrl.abort(), 12000) : null;
    let res;
    try {
      res = await fetch(UPDATE_MANIFEST_URL + '?t=' + Date.now(),
        Object.assign({ cache: 'no-store', redirect: 'error' },
          ctrl ? { signal: ctrl.signal } : {}));
    } finally {
      if (timer) clearTimeout(timer);
    }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    if (res.redirected) throw new Error('清单被重定向，已拒绝');
    manifest = await res.json();
  } catch (e) {
    if (manual) showToast('检查更新失败：网络错误');
    return;
  }
  if (!manifest || typeof manifest !== 'object') {
    if (manual) showToast('更新清单格式异常，已拒绝更新');
    return;
  }
  // S1-02 ④：versionCode 必须严格单调递增（正整数）。非整数/回退版本一律不更新。
  const current = window.AppUpdater.getVersionCode();
  const remote = Number(manifest.versionCode);
  if (!isFinite(remote) || remote <= 0 || Math.floor(remote) !== remote) {
    showToast('更新清单版本号非法，已拒绝更新', 3000);
    return;
  }
  if (remote <= current) {
    if (manual) showToast('已是最新版本');
    return;
  }
  // S1-02 ③：哈希必填（sha256 优先 / md5 兼容）
  const expected = _pickExpectedHash(manifest);
  if (!expected) {
    showToast('更新清单缺少有效哈希，已拒绝更新', 4000);
    return;
  }
  // S1-02 ①②：APK 地址同样要过 https + host 白名单
  const apkUrl = _safeUpdateUrl(manifest.url);
  if (!apkUrl) {
    showToast('更新包地址不在白名单内，已拒绝更新', 4000);
    return;
  }
  const notes = manifest.notes ? '\n\n' + manifest.notes : '';
  showConfirm('发现新版本 v' + manifest.version,
    '当前版本 v' + window.AppUpdater.getVersionName() + '，将打开下载页安装更新？' + notes,
    '去下载', '#FF3B30', () => {
      _setUpdateBusy(true, UPDATE_BUSY_TIMEOUT_MS);
      // S1-02 ⑤：交给系统下载/安装器 —— 应用内不再静默安装任何 APK。
      // （下载页里由系统展示来源与安装确认；hash 已在上方做过必填/格式校验。）
      const opened = _openUpdateDownloadPage(apkUrl);
      if (!opened) {
        _setUpdateBusy(false);
        showToast('无法打开下载页，请手动访问 ' + apkUrl, 5000);
      } else if (expected.algo === 'sha256') {
        // 下载页由系统浏览器/下载管理器完成，WebView 拿不到最终文件，无法在这里复算哈希。
        // 因此把期望值露出来：需要核对时（例如公司电脑代下）可对照前 16 位。
        showToast('已打开下载页；SHA-256 前16位 ' + expected.hex.slice(0, 16), 5000);
      } else {
        showToast('已打开下载页；MD5 前16位 ' + expected.hex.slice(0, 16), 5000);
      }
    });
}

// 启动后延迟静默检查（对齐本地打印工具 4 秒自动检查）
setTimeout(() => { try { checkAppUpdate(false); } catch (e) { /* 静默 */ } }, 5000);
