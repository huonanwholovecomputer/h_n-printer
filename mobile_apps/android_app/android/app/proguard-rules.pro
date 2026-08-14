# Add project specific ProGuard rules here.
# You can control the set of applied configuration files using the
# proguardFiles setting in build.gradle.
#
# For more details, see
#   http://developer.android.com/guide/developing/tools/proguard.html

# ==== 必须保留：WebView JS 桥接（window.AndroidBars）====
# 开启 minify 混淆后，若 MainActivity 内部的 AndroidBarsBridge 被重命名/移除，
# WebView 里 JS 调用 window.AndroidBars.getStatusBarHeight() 会失败（状态栏沉浸失效）。
# 整体 keep（类名 + 成员），保证桥接稳定且便于混淆后排查。
-keep class cn.hnspace.printer.MainActivity$AndroidBarsBridge { *; }
# 通用规则：保留所有被 @JavascriptInterface 标注的方法
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}

# Capacitor：保留 Bridge/插件核心（Capacitor 6 库自带 consumer 规则，此处兜底）
-keep class com.getcapacitor.** { *; }
