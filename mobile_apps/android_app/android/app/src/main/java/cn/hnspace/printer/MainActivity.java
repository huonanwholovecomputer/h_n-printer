package cn.hnspace.printer;

import android.graphics.Color;
import android.os.Bundle;
import android.view.Window;
import android.webkit.JavascriptInterface;

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
