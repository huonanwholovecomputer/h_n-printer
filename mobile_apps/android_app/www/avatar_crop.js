(function (root, factory) {
  // 浏览器里挂到 window.AvatarCrop（index.html 先加载本文件）；Node（验收脚本）走 require。
  // 两份实现必须语义一致：tests/verify_avatar_crop.js 用同一组用例同时跑它们。
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.AvatarCrop = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict'

  var MAX_ZOOM = 4
  var OUTPUT_SIZE = 512

  function _num(v, d) {
    var n = Number(v)
    return isFinite(n) && n !== 0 ? n : d
  }

  function fitState(viewport, image) {
    var vp = _num(viewport, 1)
    var w = _num(image && image.width, 1)
    var h = _num(image && image.height, 1)
    var scale = vp / Math.min(w, h)
    return { scale: scale, x: (vp - w * scale) / 2, y: (vp - h * scale) / 2 }
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
    x = Math.min(0, Math.max(vp - dw, x))
    y = Math.min(0, Math.max(vp - dh, y))
    return { scale: scale, x: x, y: y }
  }

  function cropRect(state, viewport, image) {
    var vp = _num(viewport, 1)
    var s = clamp(state, vp, image)
    var w = _num(image && image.width, 1)
    var h = _num(image && image.height, 1)
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
    return clamp({
      scale: s.scale * f,
      x: ax - (ax - s.x) * f,
      y: ay - (ay - s.y) * f
    }, vp, image)
  }

  return {
    MAX_ZOOM: MAX_ZOOM,
    OUTPUT_SIZE: OUTPUT_SIZE,
    fitState: fitState,
    clamp: clamp,
    cropRect: cropRect,
    zoomAt: zoomAt
  }
})
