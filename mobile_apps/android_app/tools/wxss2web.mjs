/* wxss2web.mjs — 把小程序 WXSS 转成 Android App 可用的 Web CSS
 *
 * 用法:  node tools/wxss2web.mjs [输出路径]
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
const OUT = resolve(__dirname, '..', 'styles.css');

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
html, body { height: 100%; }
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
  bottom: 19cqw;
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

/* 滚轮选择器（picker-view 的 Web 替代：scroll-snap 贴合，item 88rpx、可视 440rpx） */
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
  scroll-snap-type: y mandatory;
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
  scroll-snap-align: center;
  transition: color 0.15s;
}
.wheel-item.wheel-item-active { color: var(--text-primary); font-weight: 700; }
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
  scrollbar-width: none;
  -ms-overflow-style: none;
}
.file-list-scroll::-webkit-scrollbar {
  display: none;
}
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
