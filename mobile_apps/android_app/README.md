# HN 云打印 — Android APP

基于 WebView + Capacitor 的 Android 打印客户端，与微信小程序功能一致。

## 技术栈

- 纯 HTML/CSS/JS（无需框架）
- Apple 风格 UI（与小程序共用设计令牌）
- Capacitor 打包为原生 APK

## 快速构建

### 1. 安装依赖
```bash
npm install -g @capacitor/cli @capacitor/core @capacitor/android
```

### 2. 初始化 Capacitor（首次）
```bash
cd android_app
npx cap init "HN 云打印" "cn.hnspace.printer" --web-dir=.
npx cap add android
```

### 3. 构建 APK
```bash
npx cap sync android
npx cap open android
# 在 Android Studio 中: Build > Build Bundle(s) / APK(s) > Build APK(s)
```

### 4. 直接测试（无需构建 APK）
直接用浏览器打开 `index.html` 即可预览（需启动后端服务）。

## API 对接

- 后端地址在 `app.js` 中配置：`const BASE_URL = 'https://hn-space.cn'`
- 使用 `/api/device_login` 端点进行设备认证（无需微信）
- 其他 API 端点与微信小程序完全共用

## 与小程序差异

| 功能 | 小程序 | Android APP |
|------|--------|-------------|
| 登录 | wx.login → code → token | device_id → token |
| 文件选择 | wx.chooseMessageFile | HTML file input |
| 文件上传 | wx.uploadFile | XHR + FormData |
| 网络请求 | wx.request | fetch() |
| UI 设计 | 微信原生组件 | Web CSS (相同设计令牌) |
