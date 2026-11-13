/* utils/preview_viewer.js — 自绘全屏预览层（双击放大 / 双指捏合 / 放大后拖动 / 未放大时左右滑翻页）
 *
 * 为什么不再直接用 wx.previewImage：那是微信原生的预览层，手势由微信版本和机型决定 ——
 * 双指缩放一般都有，**双击放大并不保证**（用户 2026-11-13 报「移动端不支持双击放大」）。
 * APP 端的全屏层（www/me.js 的 #pfvMask）本来就是自己画的手势，这里补一份同口径的小程序版，
 * 两端手感一致：双击放大到 2.5 倍、再双击还原；两指以中点为锚缩放 1~5 倍；放大后单指拖动；
 * 未放大时左右滑翻页（45px 且横向占优）。
 *
 * 状态挂在页面 data.pv 上，模板见 pages/me/preview-viewer.wxml（me 页与 user-orders 页共用）。
 * 所有手势函数都接收 ctx（页面 this），因为它们要 setData。
 */
const MIN_SCALE = 1;
const MAX_SCALE = 5;
const DOUBLE_SCALE = 2.5;   // 双击放大到 2.5 倍
const TAP_MS = 320;         // 两次 tap 的时间窗
const TAP_PX = 30;          // 两次 tap 的位移容差（超过就算两次单击）
const SWIPE_PX = 45;        // 未放大时的翻页阈值
const SWIPE_RATIO = 1.2;    // 横向位移要明显大于纵向才算翻页
const ANIM_MS = 220;        // 双击放大/还原的过渡时长（与 WXSS 的 transition 一致）
const SLIDE_MS = 180;       // 翻页：淡出/滑出的时长（与 WXSS 里 .pv-slide 的 transition 一致）

/* 一次只有一个全屏层，手势中间态放模块级即可 */
const g = {
  mode: '',
  startDist: 0,
  startScale: 1,
  startMid: { x: 0, y: 0 },
  startC: { x: 0, y: 0 },
  startPt: null,
  lastX: 0,
  lastY: 0,
  moved: false,
  lastTapAt: 0,
  lastTapX: 0,
  lastTapY: 0,
  animTimer: null,
  seq: 0,          // 翻页动画序号：连点/快速滑动时只让最后一次动画落地
};

function initialData() {
  return {
    pv: {
      visible: false,
      url: '',
      urls: [],
      index: 0,
      count: 0,
      scale: 1,
      x: 0,
      y: 0,
      animating: false,
      failed: false,
      topPad: 0,        // 顶部两件套（✕ / 页码）的 px 间距，见 topPadPx()
      prevUrl: '',      // 翻页时正在滑出的旧图（双层交叉用，动画结束即清空）
      swapping: false,  // true ⇒ 两层都在，按 swapDir 播 keyframes
      swapDir: 1,       // 1 = 往后翻（新图从右进），-1 = 往前翻（新图从左进）
    },
  };
}

/* 顶部安全区（2026-11-13 用户真机截图：✕ 与页码被系统状态栏压住）。
   微信里 env(safe-area-inset-top) 在多数机型/devtools 上取到 0，而全屏层是 fixed 铺满屏幕，
   所以这里用 statusBarHeight 现算一版 px，模板用行内 style 盖过 WXSS 的兜底值。 */
function topPadPx() {
  let sb = 0;
  try {
    if (typeof wx.getWindowInfo === 'function') {
      sb = Number((wx.getWindowInfo() || {}).statusBarHeight) || 0;
    } else if (typeof wx.getSystemInfoSync === 'function') {
      sb = Number((wx.getSystemInfoSync() || {}).statusBarHeight) || 0;
    }
  } catch (e) {
    sb = 0;
  }
  if (!(sb > 0)) sb = 24;   // 拿不到就按常见状态栏高度兜底，宁可多让一点
  return Math.round(sb + 12);
}

function viewSize() {
  try {
    if (typeof wx.getWindowInfo === 'function') {
      const i = wx.getWindowInfo() || {};
      return { w: i.windowWidth || 375, h: i.windowHeight || 667 };
    }
    const i = wx.getSystemInfoSync() || {};
    return { w: i.windowWidth || 375, h: i.windowHeight || 667 };
  } catch (e) {
    return { w: 375, h: 667 };
  }
}

function dist(a, b) {
  const dx = (a.clientX || 0) - (b.clientX || 0);
  const dy = (a.clientY || 0) - (b.clientY || 0);
  return Math.sqrt(dx * dx + dy * dy) || 1;
}

function mid(a, b) {
  return { x: ((a.clientX || 0) + (b.clientX || 0)) / 2, y: ((a.clientY || 0) + (b.clientY || 0)) / 2 };
}

function clampOffset(v, scale, size) {
  const lim = Math.max(0, ((scale - 1) * size) / 2);
  return Math.min(lim, Math.max(-lim, v || 0));
}

function clampPos(x, y, scale) {
  const vs = viewSize();
  return { x: clampOffset(x, scale, vs.w), y: clampOffset(y, scale, vs.h) };
}

/* 视觉层：transform: translate3d(x,y,0) scale(s)，origin 在元素中心
   ⇒ 元素中心 = (w/2 + x, h/2 + y)，内容点 q 落在 (中心 + s*q) */
function setTransform(ctx, scale, x, y, animating) {
  const p = clampPos(x, y, scale);
  ctx.setData({
    'pv.scale': scale,
    'pv.x': p.x,
    'pv.y': p.y,
    'pv.animating': !!animating,
  });
}

function clearAnimTimer() {
  if (g.animTimer) {
    clearTimeout(g.animTimer);
    g.animTimer = null;
  }
}

function afterAnim(ctx) {
  clearAnimTimer();
  g.animTimer = setTimeout(function () {
    g.animTimer = null;
    try {
      ctx.setData({ 'pv.animating': false });
    } catch (e) { /* 页面已卸载 */ }
  }, ANIM_MS + 20);
}

function open(ctx, urls, index) {
  const list = (urls || []).filter(Boolean);
  if (!list.length) return;
  const idx = Math.max(0, Math.min(list.length - 1, Number(index) || 0));
  clearAnimTimer();
  g.mode = '';
  g.moved = false;
  g.lastTapAt = 0;
  g.seq += 1;
  ctx.setData({
    pv: {
      visible: true,
      url: list[idx],
      urls: list,
      index: idx,
      count: list.length,
      scale: 1,
      x: 0,
      y: 0,
      animating: false,
      failed: false,
      topPad: topPadPx(),
      prevUrl: '',
      swapping: false,
      swapDir: 1,
    },
  });
}

function close(ctx) {
  clearAnimTimer();
  g.mode = '';
  g.moved = false;
  g.lastTapAt = 0;
  g.seq += 1;
  ctx.setData({ 'pv.visible': false, 'pv.animating': false, 'pv.swapping': false, 'pv.prevUrl': '' });
}

/* 翻页：旧图留在 .pv-slide-old 上滑出淡出，新图从另一侧滑入淡入（双层交叉，200ms）。
   2026-11-13 用户反馈「左右切换是直接切换的，太生硬」。
   index/url 是同步切换的（验收脚本与页码都按同步语义断言），动画只存在于 DOM 层：
   旧图那一层的 keyframes 结束后（SLIDE_MS）才把 pv.swapping 置回 false。 */
function page(ctx, delta) {
  const pv = ctx.data.pv || {};
  const list = pv.urls || [];
  if (list.length < 2) return;
  const cur = Number(pv.index) || 0;
  const next = Math.max(0, Math.min(list.length - 1, cur + delta));
  if (next === cur) return;
  const dir = next > cur ? 1 : -1;
  clearAnimTimer();
  const seq = (g.seq += 1);
  g.mode = '';
  g.lastTapAt = 0;
  g.startPt = null;
  ctx.setData({
    'pv.index': next,
    'pv.url': list[next],
    'pv.prevUrl': list[cur],
    'pv.swapping': true,
    'pv.swapDir': dir,
    'pv.failed': false,
    'pv.scale': 1,
    'pv.x': 0,
    'pv.y': 0,
    'pv.animating': false,
  });
  g.animTimer = setTimeout(function () {
    if (seq !== g.seq) return;
    g.animTimer = null;
    try {
      ctx.setData({ 'pv.swapping': false, 'pv.prevUrl': '' });
    } catch (e) { /* 页面已卸载 */ }
  }, SLIDE_MS + 20);
}

/* 双击：1 倍 ⇄ 2.5 倍，以点按位置为锚（用 translate+scale 换算，见 setTransform 的注释） */
function zoomByDoubleTap(ctx, px, py) {
  const pv = ctx.data.pv || {};
  const cur = pv.scale || 1;
  const vs = viewSize();
  if (cur > 1.01) {
    setTransform(ctx, 1, 0, 0, true);
    afterAnim(ctx);
    return;
  }
  const k = DOUBLE_SCALE;
  const x = (1 - k) * ((Number(px) || 0) - vs.w / 2);
  const y = (1 - k) * ((Number(py) || 0) - vs.h / 2);
  setTransform(ctx, k, x, y, true);
  afterAnim(ctx);
}

function touchStart(ctx, e) {
  const pv = ctx.data.pv || {};
  if (!pv.visible) return;
  const ts = e.touches || [];
  g.moved = false;
  g.startPt = null;
  if (ts.length >= 2) {
    g.mode = 'pinch';
    g.startDist = dist(ts[0], ts[1]);
    g.startScale = pv.scale || 1;
    g.startMid = mid(ts[0], ts[1]);
    const vs = viewSize();
    g.startC = { x: vs.w / 2 + (pv.x || 0), y: vs.h / 2 + (pv.y || 0) };
    return;
  }
  if (ts.length === 1) {
    g.mode = 'pan';
    g.startPt = { clientX: ts[0].clientX, clientY: ts[0].clientY };
    g.lastX = ts[0].clientX;
    g.lastY = ts[0].clientY;
  }
}

function touchMove(ctx, e) {
  const pv = ctx.data.pv || {};
  if (!pv.visible) return;
  const ts = e.touches || [];
  if (g.mode === 'pinch' && ts.length >= 2) {
    g.moved = true;
    const s0 = g.startScale || 1;
    const ratio = dist(ts[0], ts[1]) / (g.startDist || 1);
    const s = Math.min(MAX_SCALE, Math.max(MIN_SCALE, s0 * ratio));
    const k = s / s0;
    const m = mid(ts[0], ts[1]);
    const vs = viewSize();
    const x = m.x - k * (g.startMid.x - g.startC.x) - vs.w / 2;
    const y = m.y - k * (g.startMid.y - g.startC.y) - vs.h / 2;
    clearAnimTimer();
    ctx.setData({
      'pv.scale': s,
      'pv.x': clampOffset(x, s, vs.w),
      'pv.y': clampOffset(y, s, vs.h),
      'pv.animating': false,
    });
    return;
  }
  if (g.mode === 'pan' && ts.length === 1) {
    const dx = ts[0].clientX - g.lastX;
    const dy = ts[0].clientY - g.lastY;
    if (Math.abs(dx) + Math.abs(dy) > 6) g.moved = true;
    if ((pv.scale || 1) > 1.01) {
      g.lastX = ts[0].clientX;
      g.lastY = ts[0].clientY;
      const vs = viewSize();
      ctx.setData({
        'pv.x': clampOffset((pv.x || 0) + dx, pv.scale, vs.w),
        'pv.y': clampOffset((pv.y || 0) + dy, pv.scale, vs.h),
        'pv.animating': false,
      });
    }
  }
}

function touchEnd(ctx, e) {
  const pv = ctx.data.pv || {};
  if (!pv.visible) return;
  const rest = e.touches || [];
  if (rest.length > 0) return;          // 捏合时先松了一根手指，等最后一根
  const ct = (e.changedTouches && e.changedTouches[0]) || null;
  const mode = g.mode;
  g.mode = '';
  if (!ct) return;
  if (mode === 'pinch') {
    if ((pv.scale || 1) <= 1.02) {
      setTransform(ctx, 1, 0, 0, true);
      afterAnim(ctx);
    }
    return;
  }
  if (!g.moved) {
    const now = Date.now();
    const isDouble = (now - g.lastTapAt < TAP_MS)
      && Math.abs((ct.clientX || 0) - g.lastTapX) < TAP_PX
      && Math.abs((ct.clientY || 0) - g.lastTapY) < TAP_PX;
    if (isDouble) {
      g.lastTapAt = 0;
      zoomByDoubleTap(ctx, ct.clientX, ct.clientY);
      return;
    }
    g.lastTapAt = now;
    g.lastTapX = ct.clientX || 0;
    g.lastTapY = ct.clientY || 0;
    return;
  }
  if ((pv.scale || 1) <= 1.02 && g.startPt) {
    const dx = (ct.clientX || 0) - g.startPt.clientX;
    const dy = (ct.clientY || 0) - g.startPt.clientY;
    if (Math.abs(dx) > SWIPE_PX && Math.abs(dx) > Math.abs(dy) * SWIPE_RATIO) {
      page(ctx, dx < 0 ? 1 : -1);
    }
  }
}

function touchCancel() {
  g.mode = '';
  g.moved = false;
  g.startPt = null;
}

module.exports = {
  MIN_SCALE: MIN_SCALE,
  MAX_SCALE: MAX_SCALE,
  DOUBLE_SCALE: DOUBLE_SCALE,
  initialData: initialData,
  open: open,
  close: close,
  page: page,
  touchStart: touchStart,
  touchMove: touchMove,
  touchEnd: touchEnd,
  touchCancel: touchCancel,
  topPadPx: topPadPx,
};
