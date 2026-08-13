package cn.hnspace.printer;

import android.graphics.Color;
import android.os.Bundle;
import android.view.View;
import android.view.Window;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.core.view.WindowInsetsControllerCompat;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    private int statusBarHeight = 0;
    private int navigationBarHeight = 0;

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        enableEdgeToEdge();
        // 关闭 WebView 原生越界效果（边缘发光 / 整页拉伸），边界橡皮筋由前端 JS 统一实现
        getBridge().getWebView().setOverScrollMode(View.OVER_SCROLL_NEVER);
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
}
