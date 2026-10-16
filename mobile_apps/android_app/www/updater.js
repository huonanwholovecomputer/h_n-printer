/* HN Cloud Print — App 在线更新（整包 APK 覆盖安装，COS 加速分发）
 * 架构与本地打印工具自更新同构：
 *   - 清单 JSON：{BASE_URL}/updates/app_update.json（服务器 nginx /updates/ 静态目录，几 KB）
 *   - 安装包 APK：腾讯云 COS（国内节点 MB/s 级下载，不占服务器带宽）
 * 原生侧（MainActivity 注入 window.AppUpdater）：
 *   - getVersionCode() / getVersionName()
 *   - downloadAndInstall(url, fileName, md5) — 后台线程下载 → MD5 校验 → 系统安装器
 * 原生进度回调：window.hnUpdateProgress(status, msg)
 * 浏览器开发预览（无原生桥）时静默跳过。
 *
 * D5 第 12 条：更新锁必须**一定会被释放**。
 *   旧实现 `_updateBusy = true` 只在 `status === 'error'` 复位；而用户点「立即更新」后系统安装器
 *   弹出、用户在系统界面点「取消」时**没有任何回调**回到这里 ⇒ 锁永久持有，
 *   `checkAppUpdate()` 第一行就 return，表现为「检查更新」永久无响应（连提示都没有）。
 *   现改为三路解锁：① error 回调；② 从系统安装器返回/应用恢复前台（visibilitychange / resume）；
 *   ③ 兜底超时。忙时手动点击也会给出「正在更新中」的反馈，而不是静默吞掉。
 */

const UPDATE_MANIFEST_URL = BASE_URL + '/updates/app_update.json';
const UPDATE_BUSY_TIMEOUT_MS = 10 * 60 * 1000;   // 兜底解锁：系统安装器不回调时的最后一道保险

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

// 从系统安装器返回 / 应用恢复前台 → 解锁。
// 真装成功时应用会被新版本替换重启，根本走不到这里；能走到的就是"用户取消了安装"。
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
window.hnUpdateProgress = function (status, msg) {
  if (status === 'error') {
    showToast(msg || '更新失败', 3000);
    _setUpdateBusy(false);
  } else if (status === 'installing') {
    showToast('下载完成，正在安装…', 4000);
    // 进入安装阶段仍持锁，但带上兜底超时（用户取消安装时靠它解锁）
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
    const res = await fetch(UPDATE_MANIFEST_URL + '?t=' + Date.now(), { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    manifest = await res.json();
  } catch (e) {
    if (manual) showToast('检查更新失败：网络错误');
    return;
  }
  const current = window.AppUpdater.getVersionCode();
  const remote = parseInt(manifest.versionCode || 0, 10);
  if (!remote || remote <= current) {
    if (manual) showToast('已是最新版本');
    return;
  }
  const notes = manifest.notes ? '\n\n' + manifest.notes : '';
  showConfirm('发现新版本 v' + manifest.version,
    '当前版本 v' + window.AppUpdater.getVersionName() + '，是否下载并安装更新？' + notes,
    '立即更新', '#FF3B30', () => {
      _setUpdateBusy(true, UPDATE_BUSY_TIMEOUT_MS);
      const fileName = 'hn-cloud-print_v' + manifest.version + '.apk';
      window.AppUpdater.downloadAndInstall(manifest.url, fileName, manifest.md5 || '');
    });
}

// 启动后延迟静默检查（对齐本地打印工具 4 秒自动检查）
setTimeout(() => { try { checkAppUpdate(false); } catch (e) { /* 静默 */ } }, 5000);
