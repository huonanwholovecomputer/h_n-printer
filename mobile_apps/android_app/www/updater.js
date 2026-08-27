/* HN Cloud Print — App 在线更新（整包 APK 覆盖安装，COS 加速分发）
 * 架构与本地打印工具自更新同构：
 *   - 清单 JSON：{BASE_URL}/updates/app_update.json（服务器 nginx /updates/ 静态目录，几 KB）
 *   - 安装包 APK：腾讯云 COS（国内节点 MB/s 级下载，不占服务器带宽）
 * 原生侧（MainActivity 注入 window.AppUpdater）：
 *   - getVersionCode() / getVersionName()
 *   - downloadAndInstall(url, fileName, md5) — 后台线程下载 → MD5 校验 → 系统安装器
 * 原生进度回调：window.hnUpdateProgress(status, msg)
 * 浏览器开发预览（无原生桥）时静默跳过。
 */

const UPDATE_MANIFEST_URL = BASE_URL + '/updates/app_update.json';

let _updateBusy = false;

function isNativeApp() {
  return typeof window !== 'undefined' && typeof window.AppUpdater !== 'undefined';
}

// 原生下载/安装进度回调（MainActivity 通过 evaluateJavascript 调用）
window.hnUpdateProgress = function (status, msg) {
  if (status === 'error') {
    showToast(msg || '更新失败', 3000);
    _updateBusy = false;
  } else if (status === 'installing') {
    showToast('下载完成，正在安装…', 4000);
  } else {
    showToast(msg || '正在下载…', 3000);
  }
};

async function checkAppUpdate(manual) {
  if (_updateBusy) return;
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
      _updateBusy = true;
      const fileName = 'hn-cloud-print_v' + manifest.version + '.apk';
      window.AppUpdater.downloadAndInstall(manifest.url, fileName, manifest.md5 || '');
    });
}

// 启动后延迟静默检查（对齐本地打印工具 4 秒自动检查）
setTimeout(() => { try { checkAppUpdate(false); } catch (e) { /* 静默 */ } }, 5000);
