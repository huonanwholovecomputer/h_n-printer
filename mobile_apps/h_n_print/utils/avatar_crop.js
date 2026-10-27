'use strict'

// 头像裁剪的**纯几何**（小程序与 APP 共用同一份语义：tests/verify_avatar_crop.js 用同一组
// 用例跑两份实现，任何一边改跑偏都会当场比对出来）。
//
// 坐标约定：
//   · viewport = 裁剪框（正方形）边长，px；
//   · state = { scale, x, y }：scale 是"图片显示像素 / 原图像素"的倍率，x/y 是图片左上角
//     相对**裁剪框左上角**的偏移（都是 px，右/下为负才会露出白边 —— clamp 保证不会）；
//   · image = { width, height }：原图像素尺寸。

var MAX_ZOOM = 4          // 在"刚好盖满裁剪框"之上最多再放大 4 倍
var OUTPUT_SIZE = 512     // 导出的正方形边长（服务端还会再压到 200px）

function _num(v, d) {
  var n = Number(v)
  return isFinite(n) && n !== 0 ? n : d
}

function fitState(viewport, image) {
  var vp = _num(viewport, 1)
  var w = _num(image && image.width, 1)
  var h = _num(image && image.height, 1)
  // 盖满裁剪框的最小倍率 = 裁剪框边长 / 短边
  var scale = vp / Math.min(w, h)
  return {
    scale: scale,
    x: (vp - w * scale) / 2,
    y: (vp - h * scale) / 2
  }
}

function clamp(state, viewport, image) {
  var vp = _num(viewport, 1)
  var w = _num(image && image.width, 1)
  var h = _num(image && image.height, 1)
  var fit = vp / Math.min(w, h)
  var scale = Number(state && state.scale)
  if (!isFinite(scale) || scale <= 0) scale = fit
  scale = Math.min(Math.max(scale, fit), fit * MAX_ZOOM)
  var dw = w * scale
  var dh = h * scale
  var x = Number(state && state.x)
  var y = Number(state && state.y)
  if (!isFinite(x)) x = 0
  if (!isFinite(y)) y = 0
  // 图片必须始终盖满裁剪框：左/上不超 0，右/下不露白
  x = Math.min(0, Math.max(vp - dw, x))
  y = Math.min(0, Math.max(vp - dh, y))
  return { scale: scale, x: x, y: y }
}

function cropRect(state, viewport, image) {
  var vp = _num(viewport, 1)
  var s = clamp(state, vp, image)
  var w = _num(image && image.width, 1)
  var h = _num(image && image.height, 1)
  // 屏幕上的裁剪框 → 原图像素（drawImage 的源矩形）
  var sx = Math.min(Math.max(0, -s.x / s.scale), Math.max(0, w - 1))
  var sy = Math.min(Math.max(0, -s.y / s.scale), Math.max(0, h - 1))
  var sw = Math.min(vp / s.scale, w - sx)
  var sh = Math.min(vp / s.scale, h - sy)
  return { sx: sx, sy: sy, sw: sw, sh: sh, scale: s.scale }
}

function zoomAt(state, viewport, image, factor, anchorX, anchorY) {
  var vp = _num(viewport, 1)
  var s = clamp(state, vp, image)
  var f = Number(factor)
  if (!isFinite(f) || f <= 0) f = 1
  var ax = isFinite(Number(anchorX)) ? Number(anchorX) : vp / 2
  var ay = isFinite(Number(anchorY)) ? Number(anchorY) : vp / 2
  // 让锚点下面那个"图片像素"缩放前后停在原地（否则双指缩放会整体漂移）
  return clamp({
    scale: s.scale * f,
    x: ax - (ax - s.x) * f,
    y: ay - (ay - s.y) * f
  }, vp, image)
}

module.exports = {
  MAX_ZOOM: MAX_ZOOM,
  OUTPUT_SIZE: OUTPUT_SIZE,
  fitState: fitState,
  clamp: clamp,
  cropRect: cropRect,
  zoomAt: zoomAt
}
