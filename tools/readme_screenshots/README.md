# README 界面图生成器（模拟数据）

README 里的四张界面图**不是手工截的**，而是用真实界面 + 一批**虚构数据**离屏渲染出来的：
换界面/换文案后重跑脚本即可更新，不必再靠人肉截图（历史上那几张空列表、旧版式的图就是这么过时的）。
两个脚本都**不连云端、不读写真实配置**（用户数据目录被指到临时沙箱）。

| 产物 | 脚本 | 说明 |
|---|---|---|
| `docs/screenshots/local-print-tool.png` | `python tools/readme_screenshots/gen_gui_shot.py` | 真实 PySide6 主窗口。用默认 `windows` 平台 + `WA_DontShowOnScreen`（**不上屏**、不弹窗），这样中文与彩色 emoji 都能正常出字；任务/成员/价格全是模拟值 |
| `docs/screenshots/cloud-settlement.jpg` | `node tools/readme_screenshots/gen_web_shots.mjs --only settlement` | 真实 `finance/settlement.html`：本地起静态服务 + `/api/**` 打桩，切「云端数据」截图 |
| `docs/screenshots/mini-program-print.jpg` | `node tools/readme_screenshots/gen_web_shots.mjs --only print` | 真实 `mobile_apps/android_app/www/`（与小程序同源），灌 4 个"已选文件"后截图 |
| `docs/screenshots/mini-program-me.jpg` | `node tools/readme_screenshots/gen_web_shots.mjs --only me` | 同上，「我」页 |

不带 `--only` 就把三张 web 图一次生成完（`gen_web_shots.mjs`）。

## 约定 / 坑（改之前先看）

- **虚构数据集中在一处**：两个脚本里共用人名（张伟/李娜/王芳）、设备
  （`DESKTOP-PRINT01` / `R7000-AHP10`）、单号（`HN2026…`）与价格口径（单面 0.30、双面 0.40），
  四张图看起来才像同一家店。
- **不要写真实的人名/微信昵称/头像/服务器地址**：这几张图是公开仓库的内容。
- `sizeDisplay` 是**KB 数值**（模板自己拼 " KB"），别再传 `'1.8 MB'`。
- `/api/printer_status` 的 `active` 在**顶层**（不是 `data.active`）——app.js 的 `api()` 返回
  `{status, data: 整个 body}`，写错就永远显示「打印机离线」。
- 手机页两个 `scroll-content`（`scrollContentPrint` / `scrollContentMe`）**不能**用
  `A || B` 取：隐藏的那个高度是 0，会量出错误的截图高度。
- 进货图需要 Playwright（与仓库里其它 `.mjs` 验收脚本同一份，硬编码在
  `C:/Users/Administrator/.cache/codex-runtimes/…/node_modules/playwright`）与 Chrome。
- 结算页那张图里的「手动代收」是用**页面自己的对账函数**算出来再回填的（`_reconcileScope`），
  自己在外面临摹收益规则会差几十块，顶部就会挂出「对账差额」黄条。
