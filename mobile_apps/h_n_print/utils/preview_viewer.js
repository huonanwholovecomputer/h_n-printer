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
    },
  };
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
    },
  });
}

function close(ctx) {
  clearAnimTimer();
  g.mode = '';
  g.moved = false;
  g.lastTapAt = 0;
  ctx.setData({ 'pv.visible': false, 'pv.animating': false });
}

function page(ctx, delta) {
  const pv = ctx.data.pv || {};
  const list = pv.urls || [];
  if (list.length < 2) return;
  const next = Math.max(0, Math.min(list.length - 1, (pv.index || 0) + delta));
  if (next === pv.index) return;
  clearAnimTimer();
  g.mode = '';
  g.lastTapAt = 0;
  ctx.setData({
    'pv.index': next,
    'pv.url': list[next],
    'pv.failed': false,
    'pv.scale': 1,
    'pv.x': 0,
    'pv.y': 0,
    'pv.animating': false,
  });
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
};
