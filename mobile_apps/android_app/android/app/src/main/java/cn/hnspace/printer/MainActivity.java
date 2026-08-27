package cn.hnspace.printer;

import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.view.View;
import android.view.Window;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import androidx.core.content.FileProvider;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.core.view.WindowInsetsControllerCompat;

import com.getcapacitor.BridgeActivity;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;

public class MainActivity extends BridgeActivity {

    private int statusBarHeight = 0;
    private int navigationBarHeight = 0;

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        enableEdgeToEdge();
        // 关闭 WebView 原生越界效果（边缘发光 / 整页拉伸），边界橡皮筋由前端 JS 统一实现
        getBridge().getWebView().setOverScrollMode(View.OVER_SCROLL_NEVER);
        // App 在线更新桥：前端 window.AppUpdater.downloadAndInstall(url, fileName, md5)
        getBridge().getWebView().addJavascriptInterface(new AppUpdaterBridge(), "AppUpdater");
    }

    // Android 物理返回键：先询问前端是否在子界面/弹窗中。
    // 前端 window.hnHandleBack() 返回 true = 已消费（正在返回"我"页/关弹窗），
    // 返回 false = 不在子界面，退出 App。
    @Override
    public void onBackPressed() {
        WebView webView = getBridge().getWebView();
        if (webView != null) {
            webView.evaluateJavascript(
                "window.hnHandleBack ? window.hnHandleBack() : 'false'",
                value -> {
                    boolean handled = "true".equals(value);
                    if (!handled) {
                        runOnUiThread(MainActivity.this::finish);
                    }
                });
        } else {
            super.onBackPressed();
        }
    }

    private void enableEdgeToEdge() {
        Window window = getWindow();

        WindowCompat.setDecorFitsSystemWindows(window, false);
        window.setStatusBarColor(Color.TRANSPARENT);
        window.setNavigationBarColor(Color.TRANSPARENT);

        ViewCompat.setOnApplyWindowInsetsListener(window.getDecorView(), (v, insets) -> {
            float density = getResources().getDisplayMetrics().density;
            statusBarHeight = Math.round(insets.getInsets(WindowInsetsCompat.Type.statusBars()).top / density);
            navigationBarHeight = Math.round(insets.getInsets(WindowInsetsCompat.Type.navigationBars()).bottom / density);
            return WindowInsetsCompat.CONSUMED;
        });
        ViewCompat.requestApplyInsets(window.getDecorView());

        getBridge().getWebView().addJavascriptInterface(new AndroidBarsBridge(), "AndroidBars");
    }

    private void setStatusBarAppearance(boolean dark) {
        WindowInsetsControllerCompat controller =
            WindowCompat.getInsetsController(getWindow(), getWindow().getDecorView());
        if (controller != null) {
            controller.setAppearanceLightStatusBars(!dark);
        }
    }

    private final class AndroidBarsBridge {
        @JavascriptInterface
        public int getStatusBarHeight() {
            return statusBarHeight;
        }

        @JavascriptInterface
        public int getNavigationBarHeight() {
            return navigationBarHeight;
        }

        @JavascriptInterface
        public void setDark(final boolean dark) {
            runOnUiThread(() -> setStatusBarAppearance(dark));
        }
    }

    /* ================= App 在线更新桥（整包 APK 覆盖安装） =================
     * 架构与本地打印工具自更新同构：清单 JSON 放服务器（/updates/app_update.json），
     * APK 放腾讯云 COS（国内节点 MB/s 级下载，不占服务器带宽）。
     * 前端流程：GET 清单 → 比较 versionCode → showConfirm → downloadAndInstall →
     * 后台线程下载到 cacheDir → MD5 校验 → FileProvider → ACTION_VIEW 系统安装器。
     * 进度/结果通过 window.hnUpdateProgress(status, msg) 回调前端。
     */
    @SuppressWarnings("deprecation")
    private final class AppUpdaterBridge {

        @JavascriptInterface
        public int getVersionCode() {
            try {
                android.content.pm.PackageInfo info =
                    getPackageManager().getPackageInfo(getPackageName(), 0);
                // getLongVersionCode 为 API 28+；低版本回退到弃用的 versionCode 字段
                return (int) (android.os.Build.VERSION.SDK_INT >= 28
                    ? info.getLongVersionCode() : info.versionCode);
            } catch (Exception e) {
                return 0;
            }
        }

        @JavascriptInterface
        public String getVersionName() {
            try {
                return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
            } catch (Exception e) {
                return "";
            }
        }

        @JavascriptInterface
        public void downloadAndInstall(final String url, final String fileName, final String md5) {
            new Thread(() -> {
                File apk = null;
                try {
                    updateProgress("downloading", "正在下载新版本…");
                    HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
                    conn.setConnectTimeout(15000);
                    conn.setReadTimeout(60000);
                    conn.setInstanceFollowRedirects(true); // COS/CDN 可能 302 到分片节点
                    conn.connect();
                    int code = conn.getResponseCode();
                    if (code / 100 != 2) {
                        updateProgress("error", "下载失败（HTTP " + code + "）");
                        return;
                    }
                    long total = conn.getContentLengthLong();
                    apk = new File(getCacheDir(), fileName);
                    try (InputStream in = conn.getInputStream();
                         FileOutputStream out = new FileOutputStream(apk)) {
                        byte[] buf = new byte[8192];
                        long done = 0;
                        int n;
                        while ((n = in.read(buf)) != -1) {
                            out.write(buf, 0, n);
                            done += n;
                            if (total > 0) {
                                final int pct = (int) (done * 100 / total);
                                updateProgress("downloading", "正在下载新版本… " + pct + "%");
                            }
                        }
                    } finally {
                        conn.disconnect();
                    }
                    // MD5 校验，防止下载损坏/被篡改
                    if (md5 != null && !md5.isEmpty()) {
                        String real = md5Of(apk);
                        if (!md5.equalsIgnoreCase(real)) {
                            updateProgress("error", "安装包校验失败，请重试");
                            apk.delete();
                            return;
                        }
                    }
                    updateProgress("installing", "下载完成，正在安装…");
                    installApk(apk);
                } catch (Exception e) {
                    updateProgress("error", "更新失败：" + e.getMessage());
                    if (apk != null) apk.delete();
                }
            }).start();
        }
    }

    private void updateProgress(final String status, final String msg) {
        runOnUiThread(() -> {
            WebView webView = getBridge().getWebView();
            if (webView == null) return;
            String js = "window.hnUpdateProgress && window.hnUpdateProgress("
                + JSONObject.quote(status) + "," + JSONObject.quote(msg) + ")";
            webView.evaluateJavascript(js, null);
        });
    }

    private String md5Of(File f) throws Exception {
        MessageDigest md = MessageDigest.getInstance("MD5");
        try (InputStream in = new FileInputStream(f)) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) != -1) md.update(buf, 0, n);
        }
        StringBuilder sb = new StringBuilder();
        for (byte b : md.digest()) sb.append(String.format("%02x", b));
        return sb.toString();
    }

    private void installApk(File apk) {
        runOnUiThread(() -> {
            try {
                Uri uri = FileProvider.getUriForFile(this, getPackageName() + ".fileprovider", apk);
                Intent intent = new Intent(Intent.ACTION_VIEW);
                intent.setDataAndType(uri, "application/vnd.android.package-archive");
                intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(intent);
            } catch (Exception e) {
                updateProgress("error", "无法打开安装器：" + e.getMessage());
            }
        });
    }
}
