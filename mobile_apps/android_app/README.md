# HN 云打印 — Android APP

基于 WebView + Capacitor 的 Android 打印客户端，功能与微信小程序对齐（2026-08 移植）。

## 技术栈

- 纯 HTML/CSS/JS（无需框架）
- 视觉层直接移植小程序 WXSS（`tools/wxss2web.mjs` 把 rpx 转 cqw 按容器宽度等比缩放），
  手机端与小程序 750rpx 设计稿像素级一致；桌面预览按 430px 手机框缩放
- 交互层移植小程序自研：橡皮筋滚动引擎、页面转场、左滑手势、玻璃拟态弹窗、滚轮选择器、闪电 canvas
- 支持自动/浅色/深色三态主题（与小程序同款渐变背景交叉淡入）
- Capacitor 打包为原生 APK

> 小程序 WXSS 更新后，重新运行 `node tools/wxss2web.mjs` 即可再生成 styles.css。

## 功能清单

**打印流程**
- 文件选择/上传（XHR 进度条、失败重试、50MB 上限、最多 20 个）
- 每文件参数：份数、页码范围（多行输入 + 语法/超限/重叠校验 + 页数已知时网格选择）、单双面、图片方向（自动/横向/竖向）
- 页码分析：轮询本地打印工具分析结果（分析中/离线/已确认三态）
- 附加服务：打印首页（普通用户必选）、优先级（低/中/高）、派送（地点 + 百分比）
- 无障碍打印预约（仅管理员）：立即开始 / 指定时间 / 倒计时
- 提交后复制价格 / 复制详细计费明细
- 不支持格式（Excel/PPT/CAD）检测与自动跳过

**个人中心**
- 昵称 / 头像（本地相册选择）
- 许可密钥：访客兑换（含剪贴板粘贴）、临时授权倒计时、密钥详情
- 我的打印任务：完整状态机（queued/printing/rejected/reserved/scheduled/downloading/waiting 等 13 种）、分页（每页 10/20/50/100）、展开详情（文件/附加服务/许可密钥/价格）、取消任务、10s 轮询

**管理员**
- 生成/作废/复制许可密钥（临时/管理员两类，1-10 分钟），密钥倒计时，管理员密钥确认，订单结算复制
- 服务器存储统计、文件保留时间设置、删除全部缓存
- 防滥用（DDoS 防护）阈值设置（8 项）
- 历史授权用户（含多次授权记录）、查看指定用户订单、本地打印任务
- 超级管理员：管理员列表管理、已临时授权用户移除

## 构建

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
直接用浏览器打开 `index.html` 即可预览（需后端可访问）。

## API 对接

- 后端地址默认 `https://hn-space.cn`（见 `app.js` 的 `DEFAULT_BASE_URL`）；本地调试可用 `localStorage.setItem('hn_base_url', 'https://你的地址')` 覆盖
- 使用 `/api/device_login` 端点进行设备认证（无需微信），401 自动重登
- 其他 API 端点与微信小程序完全共用

## 与小程序差异

| 功能 | 小程序 | Android APP |
|------|--------|-------------|
| 登录 | wx.login → code → token | device_id → token |
| 文件选择 | wx.chooseMessageFile | HTML file input |
| 昵称 | 微信昵称自动填充 | 手动输入 |
| 头像 | 微信头像 / 相册 | 相册选择 |
| 预约时间选择 | picker-view 滚轮 | 下拉选择 |
| 动画 | 自定义滚动引擎/闪电动画 | CSS 过渡（简版） |
| 网络请求 | wx.request | fetch() / XHR |
