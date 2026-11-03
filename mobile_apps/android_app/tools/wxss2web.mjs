/* wxss2web.mjs — 把小程序 WXSS 转成 Android App 可用的 Web CSS
 *
 * 用法:  node tools/wxss2web.mjs --force
 *
 * ⚠ 破坏性脚本：它**永远写回 www/styles.css**（不接受输出路径参数，历史上 README 写错了）。
 *   而 www/styles.css 早已手工调过约 2000 行（几何、玻璃拟态、APP 专有卡片如「近 N 条订单」），
 *   直接重跑会把这些手工改动静默抹掉 —— 2026-11-02 就这么丢过一轮 188 行样式（靠 git 才救回来）。
 *   所以现在**必须显式传 --force** 才允许落盘；不加参数只打印说明并退出（不写文件）。
 *   真要用它覆盖，先 `git diff www/styles.css` 备份，跑完再逐段把手工改动贴回去。
 *
 * 转换规则:
 *   1. rpx → cqw（container query unit）: 1rpx = 100cqw/750，
 *      由 .app-frame 的 container-type: inline-size 提供缩放基准，
 *      手机上容器=屏幕宽度，与小程序 750rpx 设计稿完全一致。
 *   2. `page` 选择器 → `body`。
 *   3. 深色模式仍用 .theme-dark 类（Web 端挂到 <body> 和 modal-mask 上）。
 */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(__dirname, '..', '..', 'h_n_print');
const OUT = resolve(__dirname, '..', 'www', 'styles.css');

// 防误跑闸门：没有 --force 一律不落盘（见文件头说明）
if (!process.argv.includes('--force')) {
  console.error(
    '✗ wxss2web.mjs 会覆盖 www/styles.css（该文件已被手工大改，重跑会静默抹掉手工样式）。\n' +
    '  确认要重新生成请加 --force，并先备份: git diff www/styles.css > /tmp/styles.bak\n' +
    '  本次未写入任何文件。'
  );
  process.exit(1);
}


const FILES = [
  ['app.wxss', '/* ========== app.wxss — 设计系统 / 基础组件 ========== */'],
  ['components/navigation-bar/navigation-bar.wxss', '/* ========== navigation-bar 组件 ========== */'],
  ['custom-tab-bar/index.wxss', '/* ========== custom-tab-bar ========== */'],
  ['pages/index/index.wxss', '/* ========== index.wxss — 提交打印页 ========== */'],
  ['pages/me/me.wxss', '/* ========== me.wxss — 我页 ========== */'],
  ['pages/authorized-users/authorized-users.wxss', '/* ========== authorized-users.wxss — 历史授权用户 ========== */', '#view-authorized'],
  ['pages/user-orders/user-orders.wxss', '/* ========== user-orders.wxss — 我的任务 ========== */', '#view-user-orders'],
];

// rpx → cqw：N rpx = N * 100 / 750 cqw
function rpxToCqw(css) {
  return css.replace(/(-?\d+(?:\.\d+)?)rpx/g, (_m, n) => {
    const v = (parseFloat(n) * 100 / 750).toFixed(4);
    return v + 'cqw';
  });
}

// 转换单个文件
function convert(css) {
  let out = rpxToCqw(css);
  // `page` 根选择器 → `body`（仅匹配选择器位置的 page）
  out = out.replace(/(^|[,}\s])page(?=\s*[,{])/g, '$1body');
  return out;
}

// 把独立页面 WXSS 的作用域限定到对应子视图（如 #view-authorized），避免与 me/index 同名类互相覆盖。
// `.theme-dark ...` 深色规则保持全局：theme-dark 类挂在 <body> 上，是子视图的祖先。
function scopeCss(css, scope) {
  return css.replace(/([^{}]+)\{/g, (m, selBlock) => {
    if (selBlock.includes('/*')) return m;
    const scoped = selBlock
      .split(',')
      .map((s) => {
        s = s.trim();
        if (!s) return s;
        if (s.startsWith('.theme-dark')) return s;
        return `${scope} ${s}`;
      })
      .join(', ');
    return `${scoped} {`;
  });
}

const WEB_BASE = `/* 生成自 wxss2web.mjs — 请勿手改，改小程序样式后重新运行脚本 */

:root {
  --safe-top: env(safe-area-inset-top, 0px);
  --safe-bottom: env(safe-area-inset-bottom, 0px);
}

* { box-sizing: border-box; margin: 0; padding: 0; }
/* overscroll-behavior: none 必须保留 —— Android WebView 的整页下拉刷新 / 橡皮筋靠它压住；
   内层列表（.file-list-scroll / .wheel-viewport）的边界交接不走"放开滚动链"这条路，
   而是各自 contain + FlingEngine 贴边接管（见两处注释）。 */
html, body { height: 100%; overscroll-behavior: none; }
view { display: block; }

/* 手机框：桌面预览时约束为手机宽度；container-type 提供 cqw 缩放基准 */
body {
  background: #000;
  display: flex;
  justify-content: center;
  align-items: stretch;
}
.app-frame {
  container-type: inline-size;
  width: 100%;
  max-width: 430px;
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-primary, #F2F2F7);
  box-shadow: 0 0 40px rgba(0,0,0,0.35);
  position: relative;
}

/* 背景遮罩层改为 frame 内 absolute，避免桌面端铺满整个视口 */
.theme-bg-layer {
  position: absolute;
  width: 100%;
  height: 100%;
}

/* 桌面预览时把 fixed 弹窗约束到手机框内 */
@media (min-width: 431px) {
  .modal-mask {
    left: 50%;
    transform: translateX(-50%);
    width: 430px;
  }
}

button { cursor: pointer; }
button:disabled { cursor: default; }

/* 微信 button 默认样式清理 */
button::after { border: none; }
`;

const WEB_FOOT = `
/* ========== Web 适配补丁 ========== */

/* 触摸设备避免点击高亮 */
* { -webkit-tap-highlight-color: transparent; }

/* 小程序的 fixed 元素在桌面宽屏会错位 → 改为在 .app-frame 内 absolute 定位 */
.tab-bar { position: absolute; }
.theme-toggle-btn { position: absolute; }
.toast { position: absolute; }

/* scroller 允许原生滚动兜底（自定义引擎未接管时） */
.scroller { overflow-y: auto; }
.scroller.engine-active { overflow-y: hidden; }

/* 桌面端滚动条更细 */
.scroller::-webkit-scrollbar { width: 4px; }
.scroller::-webkit-scrollbar-thumb { background: rgba(0,0,0,0.15); border-radius: 2px; }

/* input 数字框去掉上下箭头 */
.stepper-input::-webkit-outer-spin-button,
.stepper-input::-webkit-inner-spin-button,
.security-input::-webkit-outer-spin-button,
.security-input::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }

/* ========== Web 独有组件 ========== */

/* Toast（小程序无此组件） */
.toast {
  position: absolute;
  left: 50%;
  /* 与 www/styles.css 的实际取值同口径（该文件后来手工调过，本模板已落后）：
     底距必须 >= tabBar 顶缘（1.44cqw + 13.2cqw + 安全区），否则安全区大的机型上被 tabBar 盖住 */
  bottom: calc(17.1cqw + var(--safe-bottom, env(safe-area-inset-bottom, 0px)));
  transform: translateX(-50%) translateY(16px);
  background: rgba(40, 40, 40, 0.92);
  color: #fff;
  font-size: 3.2cqw;
  padding: 2.8cqw 5cqw;
  border-radius: 2.4cqw;
  z-index: 2000;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.25s, transform 0.25s;
  max-width: 80%;
  text-align: center;
  box-shadow: 0 1.5cqw 6cqw rgba(0,0,0,0.3);
}
.toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

/* 滚轮选择器（picker-view 的 Web 替代：item 88rpx、可视 440rpx）
   ⚠ 这里**不用** scroll-snap：CSS 的 scroll-snap-type: y mandatory（本模板是 JS 模板字符串，
   注释里不能出现反引号）会让 Chromium 在每次松手时把惯性收敛到最近吸附点
   ⇒ 一次手势最多走一格，用户反馈的"滑动阻力感太大"就是它。
   现在改为「原生自由滚动 + 手指/惯性静止 140ms 后 JS 吸附一格」（print.js buildWheel），
   索引判定仍是"离视口中心最近的 item"，与 readWheelIndex() 同口径。 */
.time-wheel-row {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 2.6667cqw;
}
.wheel-col {
  position: relative;
  width: 26cqw;
  height: 58.6667cqw;
  overflow: hidden;
  border-radius: 2cqw;
}
.wheel-viewport {
  height: 100%;
  overflow-y: auto;
  /* 内层滚到底不把滚动链抛给外层（弹层里没有别的滚动容器），也不做 overscroll 回弹，
     否则回弹动画会和 JS 吸附抢 scrollTop */
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: none;
  -ms-overflow-style: none;
}
.wheel-viewport::-webkit-scrollbar { display: none; }
.wheel-items { padding: 23.4667cqw 0; }
.wheel-item {
  height: 11.7333cqw;
  line-height: 11.7333cqw;
  text-align: center;
  font-size: 4.8cqw;
  font-weight: 600;
  font-family: var(--font-mono);
  color: var(--text-tertiary);
  transition: color 0.15s;
}
/* ⚠ 前面给小程序 picker-view 写的 .wheel-item-active（在 .time-picker-view 那一块）会级联进来：
   font-size + 橙色 text-shadow。Web 滚轮里必须显式压回去，否则选中项比未选中项**更小**、
   且带一圈小程序橙光。字号保持与非选中项一致（只靠颜色/字重区分，避免字宽变化引起跳动）。 */
.wheel-item.wheel-item-active {
  color: var(--text-primary);
  font-weight: 700;
  font-size: 4.8cqw;
  text-shadow: none;
}
.wheel-indicator {
  position: absolute;
  top: 50%;
  left: 0;
  right: 0;
  height: 11.7333cqw;
  transform: translateY(-50%);
  pointer-events: none;
  background: rgba(0, 122, 255, 0.07);
  border-radius: 2cqw;
  z-index: 1;
}
.wheel-viewport::-webkit-scrollbar { display: none; }

/* 小程序未定义样式 / Web 自创组件的补充样式 */
/* 按钮继承设计系统字体（浏览器 button 默认 Arial，与小程序观感不一致）；
   同时去掉 UA 默认边框与原生外观（outset 边框在圆角按钮右下角会渲染出斜角黑边） */
button {
  font-family: inherit;
  display: block; /* 对齐微信小程序 button 默认：块级自动撑满容器（无宽度按钮不再收缩到内容宽） */
  border: none;
  appearance: none;
  -webkit-appearance: none;
}

/* 角色入口按钮：小程序用 view（块级自动撑满），Web 端 button 需显式撑满 */
.role-btn {
  width: 100%;
}
/* 防滥用设置与角色入口、两个入口按钮之间加大间距（原为 0 紧贴） */
.security-section {
  margin-bottom: 2.6667cqw;
}
.role-btn + .role-btn {
  margin-top: 2.6667cqw;
}
/* 打印机状态文字：me.wxss 的 .status-text(26rpx) 会全局覆盖打印页状态文字，
   作用域限定恢复小程序 index.wxss 的 22rpx */
#printerStatus .status-text {
  font-size: 2.9333cqw;
}
/* 份数 stepper 撑满剩余行宽（与方向/范围/模式控件一致，仅文件卡片控件行） */
.control-row .stepper {
  flex: 1;
}
/* 页码范围输入框撑满整行（微信 input 默认满宽，HTML input 需显式 100%） */
.range-line-input {
  width: 100%;
}
/* 深色模式：模式/方向滑轨加亮，提升与卡片背景的区分度 */
.theme-dark .duplex-toggle,
.theme-dark .img-ori-toggle {
  background-color: rgba(255, 255, 255, 0.18);
}
/* 模式/方向滑轨：纵向滚动交给页面，横向拖动交给滑块 */
.duplex-toggle,
.img-ori-toggle {
  touch-action: pan-y;
}

/* 开始方式卡片：标题更小；模式按钮与瓷块文字常规字重、居中 */
.schedule-title {
  font-size: 2.6667cqw;
}
.schedule-mode,
.schedule-mode.active {
  font-family: inherit;
  font-weight: 300;
  text-align: center;
}
.schedule-picker-value {
  display: block;
  width: 100%;
  font-family: inherit;
  font-weight: 300;
  text-align: center;
}

.nickname-input {
  font-size: 5.6cqw;
  font-weight: 600;
  background: transparent;
  width: 100%;
  padding: 0;
  border: none;
  outline: none;
  color: var(--text-primary);
}
.key-card {
  touch-action: pan-y;
}
/* 导航栏悬浮：内容从导航栏下方滚过（真实毛玻璃采样）；滚动内容加同高顶部留白 */
.weui-navigation-bar {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  z-index: 900;
}
.scroll-content {
  padding-top: calc(48px + env(safe-area-inset-top));
}
/* 内层文件列表：显式高度 + 内部滚动裁剪（对齐小程序 scroll-view），
   否则卡片溢出画到外层卡片/添加按钮上 */
.file-list-scroll {
  overflow-y: auto;
  /* 内层触底/到顶时不做 overscroll 回弹、也不把滚动链往外抛：外层 .scroller 是 FlingEngine
     自绘（overflow:hidden + transform），原生滚动链接到它不是真滚动容器 ⇒ 观感"卡在列表底部"。
     边界交接完全交给 app.js FlingEngine 的贴边判定（贴边那一帧起由外层 transform 吃手势）。
     ⚠ 不要改成 auto/none 去"放开"链条：外层不是真滚动容器，放开只会让浏览器先回弹再卡住。 */
  overscroll-behavior-y: contain;
  overscroll-behavior-x: contain;
  scrollbar-width: none;
  -ms-overflow-style: none;
}
.file-list-scroll::-webkit-scrollbar {
  display: none;
}
/* ── 以下为 APP 专有样式（Web 端需求，小程序端没有对应 WXSS）──
   写在这里是为了让"重跑生成器"不会静默抹掉它们；生成结果与本文件里的手改版本
   可能不完全同排版，但语义一致。 */
/* 离线设备下发的攒批指令还没落到设备上（服务端返回 pending） */
.device-state-stack-pending {
  color: #e67e22;
  font-weight: 600;
  margin-right: 0.96cqw;
}
.theme-dark .device-state-stack-pending {
  color: #FFA94D;
}
/* 「近 N 条订单」卡片（仅管理员可见，见 me.js loadRecentOrders） */
.recent-orders-card {
  background: var(--bg-card);
  border-radius: var(--radius-lg);
  padding: 1.2cqw 3.36cqw;
  text-align: left;
  box-shadow: var(--glass-shadow-sm), var(--glass-rim);
  border: 0.12cqw solid var(--separator);
}
.recent-order-row {
  padding: 2.16cqw 0;
  border-bottom: 0.12cqw solid var(--separator);
}
.recent-order-row:last-child { border-bottom: none; }
.recent-order-line {
  display: flex;
  align-items: center;
  gap: 1.2cqw;
  min-height: 3.6cqw;
}
.recent-order-line + .recent-order-line { margin-top: 0.72cqw; }
.recent-order-owner {
  font-size: 2.88cqw;
  font-weight: 600;
  color: var(--text-primary);
  max-width: 40%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  border-bottom: 0.12cqw dashed var(--gray3);
  -webkit-user-select: none;
  user-select: none;
}
.recent-order-time {
  font-size: 2.4cqw;
  color: var(--text-quaternary);
  font-family: var(--font-mono);
  flex-shrink: 0;
}
.recent-order-status {
  margin-left: auto;
  padding: 0.24cqw 1.44cqw;
  border-radius: 2.4cqw;
  font-size: 2.4cqw;
  font-weight: 600;
  flex-shrink: 0;
}
.recent-order-meta {
  flex: 1;
  min-width: 0;
  font-size: 2.4cqw;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.recent-order-price {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 0.48cqw;
  padding: 0.24cqw 0.96cqw;
  border-radius: 1.2cqw;
  font-family: var(--font-mono);
  font-size: 2.88cqw;
  font-weight: 600;
  color: var(--text-primary);
  flex-shrink: 0;
}
.recent-order-price:active { background: var(--gray6); }
.recent-order-empty {
  padding: 2.4cqw 0;
  text-align: center;
  font-size: 2.64cqw;
  color: var(--text-quaternary);
}
/* 超管设置 N 的就地步进器（仅超级管理员，isSuperAdmin） */
.recent-order-limit-entry {
  margin-left: auto;
  padding: 0.48cqw 1.44cqw;
  border-radius: 2.4cqw;
  background: rgba(0, 122, 255, 0.10);
  color: var(--blue);
  font-size: 2.4cqw;
  font-weight: 600;
  flex-shrink: 0;
  -webkit-user-select: none;
  user-select: none;
}
.recent-order-limit-entry:active { background: rgba(0, 122, 255, 0.18); }
.recent-order-limit-editor {
  display: flex;
  align-items: center;
  gap: 1.2cqw;
  margin-top: 1.2cqw;
  padding: 0.96cqw 1.2cqw;
  background: var(--gray6);
  border-radius: var(--radius-md);
}
.recent-order-limit-label {
  font-size: 2.4cqw;
  color: var(--text-tertiary);
  flex-shrink: 0;
}
.recent-order-limit-step {
  width: 5.52cqw;
  height: 5.52cqw;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: var(--bg-card);
  box-shadow: var(--glass-rim);
  font-size: 3.6cqw;
  font-weight: 600;
  color: var(--text-primary);
  flex-shrink: 0;
}
.recent-order-limit-step:active { background: var(--gray5); }
.recent-order-limit-value {
  min-width: 6.48cqw;
  text-align: center;
  font-family: var(--font-mono);
  font-size: 3.12cqw;
  font-weight: 600;
  color: var(--text-primary);
}
.recent-order-limit-save {
  margin-left: auto;
  padding: 0.48cqw 1.68cqw;
  border-radius: 2.4cqw;
  background: var(--blue);
  color: #fff;
  font-size: 2.4cqw;
  font-weight: 600;
  flex-shrink: 0;
}
.recent-order-limit-save-disabled { opacity: 0.5; pointer-events: none; }
.recent-order-limit-cancel {
  padding: 0.48cqw 1.68cqw;
  border-radius: 2.4cqw;
  border: 0.12cqw solid var(--separator);
  color: var(--text-tertiary);
  font-size: 2.4cqw;
  flex-shrink: 0;
}
/* ── APP 专有样式结束 ── */
/* 返回按钮垂直居中：__left 用 flex-start + wrapper 上下负边距会把箭头顶到导航顶部，
   改为 __left 垂直居中、去掉上下负边距（保留水平负边距的触摸区） */
.weui-navigation-bar__left {
  align-items: center;
}
.weui-navigation-bar__btn_goback_wrapper {
  margin: 0 -18px 0 -16px;
}
/* 顶部导航栏：与 Tab 栏同款毛玻璃材质（固定低透明底 + 模糊 + 内外描边，不随主题变化） */
.weui-navigation-bar__inner,
.theme-dark .weui-navigation-bar__inner {
  background: rgba(255, 255, 255, 0.12);
  backdrop-filter: saturate(180%) blur(14px);
  -webkit-backdrop-filter: saturate(180%) blur(14px);
  box-shadow:
    0 1.0667cqw 4.2667cqw rgba(0, 0, 0, 0.08),
    inset 0 0.1333cqw 0 rgba(255, 255, 255, 0.20),
    inset 0 -0.1333cqw 0 rgba(0, 0, 0, 0.04),
    0 0 0 0.0667cqw rgba(0, 0, 0, 0.04);
}
.detail-actions { display: flex; flex-direction: column; margin-top: 2.7cqw; }
.empty-illustration {
  width: 16cqw; height: 16cqw;
  background: rgba(0, 122, 255, 0.06);
  border-radius: 8cqw;
  display: flex; align-items: center; justify-content: center;
  font-size: 7.5cqw; margin-bottom: 3.7cqw;
}
.empty-title { font-size: 3.7cqw; font-weight: 600; margin-bottom: 1.1cqw; }
.empty-desc { font-size: 3.2cqw; color: var(--text-tertiary); }
.file-retry-btn {
  font-size: 2.7cqw; font-weight: 500;
  color: var(--orange);
  border: 0.1333cqw solid var(--orange);
  border-radius: 1.6cqw;
  padding: 0.8cqw 2.4cqw;
}
/* 内联 SVG 头像：CSS 只设了宽度，补宽高比（logo 用真实图片，按自身比例自适应） */
.avatar { aspect-ratio: 1 / 1; height: auto; }

/* ========== 手动修复区（重新运行脚本后必须保留的 APP 独有修复） ========== */

/* BUG3: 许可密钥卡片底部操作按钮对齐小程序渲染（微信 button 默认 184px 宽 + margin auto 居中，
   Web button 默认收缩内容宽且 flex-end 右对齐） */
.key-actions .copy-btn {
  width: 42.8000cqw;
  margin-left: auto;
  margin-right: auto;
  flex-shrink: 0;
}

/* BUG4: 历史授权用户/本地打印任务（#view-user-orders）金额去掉状态类底色，
   #前缀特异性(1,2,0)压过 #view-user-orders .status-*(1,1,0)，深色模式不再出现白色底纹 */
#view-user-orders .detail-price-value.status-queued,
#view-user-orders .detail-price-value.status-printing,
#view-user-orders .detail-price-value.status-accepted,
#view-user-orders .detail-price-value.status-offline_unknown,
#view-user-orders .detail-price-value.status-sent,
#view-user-orders .detail-price-value.status-failed,
#view-user-orders .detail-price-value.status-abandoned,
#view-user-orders .detail-price-value.status-reserved,
#view-user-orders .detail-price-value.status-rejected,
#view-user-orders .detail-price-value.status-canceled,
#view-user-orders .detail-price-value.status-scheduled,
#view-user-orders .detail-price-value.status-downloading,
#view-user-orders .detail-price-value.status-waiting,
#view-user-orders .detail-price-value.status-delivered {
  background-color: transparent;
}

/* BUG8: 倒计时滚轮单位标签（分/秒）改为 flex 流内排列（小程序用 left:40%/90% 显式定位，
   Web 端没有对应内联样式，absolute 静态定位会让两个单位重叠在容器中心） */
.wheel-unit {
  position: static;
  top: auto;
  transform: none;
  z-index: auto;
  flex-shrink: 0;
}

/* BUG9: 账号绑定「已绑定设备」使用全局卡片外观（底色 + 阴影 + 描边） */
.bind-device-list {
  background: var(--bg-card);
  box-shadow: var(--glass-shadow-sm), var(--glass-rim);
}
.bind-device-item {
  background-color: transparent;
}

/* BUG2: 头像 <img> 裁剪（对齐小程序 image mode="aspectFill"） */
.avatar { object-fit: cover; }

/* ⚠ 2026-11（用户需求 1·2·3·9）新增的 APP 专属样式**不在**上面这段里，而是直接写进了
   www/styles.css 对应规则之后。重跑本脚本（--force）会把它们抹掉，必须手工贴回：
     · .device-pending-badge（「⏳ 待应用」角标）
     · .device-order-sheet / .myfiles-sheet 加宽（79.2cqw + max-width:92cqw）
     · .myfiles-list / .myfile-row 整行宽 + border-box（修文字出界）
     · 「最近订单」整段：#view-recent-orders 汇总行 / 分页 / .ro-row 三行卡 / 空态
     · .status-reason-tap 虚线可点 + .status-pop-mask/.status-pop 异常原因气泡
     · .status-timeout 琥珀色（浅色 + 深色两套）
   查法：git diff www/styles.css | grep 2026-11 （重跑前先存这份 diff 备用）。
`;

let all = WEB_BASE;
for (const [rel, banner, scope] of FILES) {
  const p = resolve(SRC, rel);
  let css;
  try {
    css = readFileSync(p, 'utf8');
  } catch (e) {
    console.error('读取失败:', rel, e.message);
    process.exit(1);
  }
  let converted = convert(css);
  if (scope) converted = scopeCss(converted, scope);
  all += '\n' + banner + '\n' + converted;
}
all += WEB_FOOT;

mkdirSync(dirname(OUT), { recursive: true });
writeFileSync(OUT, all, 'utf8');
console.log('已生成:', OUT, `(${all.length} bytes)`);
