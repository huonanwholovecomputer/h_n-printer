// utils/back_guard.js — 安卓物理返回键接管（**唯一实现**，勿在页面里再写一份）
//
// 要解决的问题（2026-11-20 用户报的现场问题）：安卓上按系统返回键，小程序**直接退出**，
// 而 App 是「先收弹窗 → 再退出」。要求两端一致：一层层退到最底层界面之后才关闭应用。
//
// 机制：小程序**没有**拦截物理返回键的 API（wx.onBackPress 不存在）。官方唯一可用的口子是
// <page-container>，官方文档原文：
//   「页面内存在该容器时，当用户进行返回操作，关闭该容器不关闭页面。返回操作包括三种情形，
//     右滑手势、安卓物理返回键和调用 navigateBack 接口。」
// 所以每页末尾挂一只**隐形守卫容器**（透明、pointer-events:none、z-index 压在最底下）：
//   · 有弹层在 → 守卫 show=true → 用户按返回 → 微信只关容器、不动页面 → beforeleave 里
//     我们收掉最上层弹层，然后把守卫重新挂上；
//   · 弹层全收完 → 守卫 show=false → 下一次返回才落回「关页面 / 退出小程序」，
//     与 App 的 MainActivity.finish() 同口径。
//
// 三条不能改的约束（守卫脚本 other/tests/verify_mp_back_guard.py 盯着）：
// 1) **递归闸必须在最前面**：代码里 setData({show:false}) 同样会触发 beforeleave
//    （2022-02-21 varzy.me 实测）。没有 backGuardHold 这道闸，一次返回会连关两层弹层。
// 2) 微信关掉容器后**不会自己再打开**：所以收完一层要假推一次 hold（true → false），
//    让 still-open 的守卫从 false→true 重新挂上；不重挂的话第二层就漏过去、直接退小程序。
// 3) show 必须是**页面 data 的表达式**（wxml 里 `{{!backGuardHold && (各弹层标志 || …)}}`），
//    不要额外维护一个「守卫是否该显示」的布尔值：弹层自己 setData 收尾时表达式自然跟着变，
//    多一个派生状态就又是一种会漂移的口径。

/** 取 data 上的标志值，支持 `pv.visible` 这种一层嵌套路径（预览层就是这么存的）。 */
function _flag(data, path) {
  if (!path) return undefined
  if (path.indexOf('.') < 0) return data[path]
  const parts = path.split('.')
  let cur = data
  for (let i = 0; i < parts.length; i++) {
    if (cur == null) return undefined
    cur = cur[parts[i]]
  }
  return cur
}

/**
 * 收掉栈里最上层那个弹层。
 * @param {Object} page 页面实例（Component 实例）
 * @param {Array}  layers `[{ flag, close, closing? }, …]`，**从上到下**排
 * @returns {boolean} 是否真的命中了一层（false = 当前没有任何弹层）
 */
function closeTopLayer(page, layers) {
  const list = layers || []
  for (let i = 0; i < list.length; i++) {
    const item = list[i]
    if (!item || !item.flag) continue
    if (!_flag(page.data, item.flag)) continue
    // 正在退场（200ms 动画里连按两次返回）：这次吞掉，不重复触发同一层的收尾逻辑
    if (item.closing && page.data[item.closing]) return true
    const fn = item.close ? page[item.close] : null
    if (typeof fn === 'function') fn.call(page)
    else if (typeof console !== 'undefined') console.warn('[back_guard] 缺少收层方法', item.close)
    return true
  }
  return false
}

/** 收完一层后把守卫重新挂上（微信不会自己重新打开容器）。 */
function remount(page) {
  page.setData({ backGuardHold: true }, function () {
    page.setData({ backGuardHold: false })
  })
}

/**
 * 守卫容器的 bind:beforeleave 处理器；页面里的 onBackGuardLeave 直接转调它。
 * @returns {boolean} 是否消费了这次返回（false = 没有弹层可收，交回微信/页面栈）
 */
function leave(page, layers) {
  // 约束 1：程序性关容器也会走这里，必须最先挡住
  if (page.data.backGuardHold) return false
  const closed = closeTopLayer(page, layers)
  // 没有弹层可收：不重挂，让容器正常关掉，下一次返回就交回页面栈/退出小程序
  if (closed) remount(page)
  return closed
}

module.exports = { leave, closeTopLayer }
