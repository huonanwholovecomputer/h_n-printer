// custom-tab-bar/index.js
// 原生自定义 tabBar — 固定配色（不随主题变化），避免重建时首帧闪色
// 2026-11-16：加玻璃滑块。几何与选中格由这里下发（cfg），拖动/滞后/吸附/船式起伏全部
// 在视图层（utils/tabbar.wxs）逐帧完成 —— 手势期间逻辑层一次 setData 都不参与。
// 这里只负责「要切页」这一刻：先让当前页播 160ms 退出动画，再 switchTab。
Component({
  data: {
    hideBorder: false,
    dark: false,
    selected: 0,
    wave: false,
    cfg: null,
    list: [
      { text: '打印', icon: '/images/tab/print.png', pagePath: 'pages/index/index', active: true },
      { text: '我', icon: '/images/tab/me.png', pagePath: 'pages/me/me', active: false },
    ],
  },
  lifetimes: {
    attached() {
      this._rev = 0
      this._handledTapAt = 0
      this._waveTimer = 0
      this._geo = null
      this._refresh()
    },
    detached() {
      if (this._waveTimer) { clearTimeout(this._waveTimer); this._waveTimer = 0 }
    },
  },
  pageLifetimes: {
    show() { this._refresh() },
  },
  methods: {
    // 几何：一格多少 px、药丸允许探出多少（= 左右内缩 / 格宽）。与 index.wxss 的
    // left: 9rpx / width: calc(50% - 18rpx) 必须一致，否则药丸落位会偏。
    _measure() {
      let winW = 375
      try {
        const info = wx.getWindowInfo ? wx.getWindowInfo() : wx.getSystemInfoSync()
        if (info && info.windowWidth) winW = info.windowWidth
      } catch (e) { /* 老基础库：用 375 兜底 */ }
      const pxPerRpx = winW / 750
      const barW = winW - 160 * pxPerRpx
      const slot = barW / 2
      const inset = 9 * pxPerRpx
      this._geo = { step: slot, over: inset / slot, maxIdx: this.data.list.length - 1 }
      return this._geo
    },

    // 页面出现/切回时对齐选中格（新页面的 tabBar 是新建实例，药丸直接落在这一格）
    _refresh() {
      const geo = this._measure()
      let idx = this.data.selected
      let route = ''
      try {
        const pages = getCurrentPages()
        const cur = pages[pages.length - 1]
        if (cur && cur.route) route = cur.route
      } catch (e) { /* 兼容低版本 */ }
      if (route) {
        const found = this.data.list.findIndex((item) => item.pagePath === route)
        if (found >= 0) idx = found
      }
      let dark = false
      try { dark = !!(getApp().globalData && getApp().globalData.isDarkMode) } catch (e) {}
      this._rev += 1
      const patch = {
        selected: idx,
        dark,
        cfg: { step: geo.step, maxIdx: geo.maxIdx, over: geo.over, sel: idx, rev: this._rev },
      }
      this.data.list.forEach((item, i) => {
        if (item.active !== (i === idx)) patch['list[' + i + '].active'] = (i === idx)
      })
      this.setData(patch)
    },

    // 兜底：视图层没接住这一下时（老基础库/JS 层事件）仍能切页。
    // 视图层的点按会先 callMethod('onTabTap')，600ms 内的 bindtap 一律忽略。
    switchTab(e) {
      if (Date.now() - (this._handledTapAt || 0) < 600) return
      const index = Number(e.currentTarget.dataset.index)
      this._handledTapAt = Date.now()
      this._goTab(index)
    },

    onTabTap(arg) {
      this._handledTapAt = Date.now()
      this._goTab(Number(arg && arg.index))
    },

    onTabDragTo(arg) {
      this._handledTapAt = Date.now()
      this._goTab(Number(arg && arg.to))
    },

    onTabDragBack() {
      // 拖回原格：不切页，但抑制随后可能补发的 bindtap
      this._handledTapAt = Date.now()
    },

    _goTab(index) {
      const target = this.data.list[index]
      if (!target || index === this.data.selected) return
      const app = getApp()
      const isDark = app.globalData.isDarkMode
      wx.setBackgroundColor({ backgroundColor: isDark ? '#1C1C1E' : '#F2F2F7', backgroundColorTop: isDark ? '#1C1C1E' : '#F2F2F7', backgroundColorBottom: isDark ? '#1C1C1E' : '#F2F2F7' })
      wx.setNavigationBarColor({ frontColor: isDark ? '#ffffff' : '#000000', backgroundColor: isDark ? '#1C1C1E' : '#F2F2F7' })
      const prev = this.data.selected
      const dir = prev === 0 ? 'left' : 'right'
      let cp = null
      try {
        const pages = getCurrentPages()
        cp = pages[pages.length - 1]
      } catch (e) {}
      if (cp && typeof cp.animateExit === 'function' && !cp.animateExit(dir)) return
      wx.setStorageSync('_tabFrom', prev)
      wx.setStorageSync('_tabTo', index)
      const patch = { selected: index, wave: true }
      this.data.list.forEach((item, i) => {
        if (item.active !== (i === index)) patch['list[' + i + '].active'] = (i === index)
      })
      this.setData(patch)
      if (this._waveTimer) clearTimeout(this._waveTimer)
      this._waveTimer = setTimeout(() => {
        this._waveTimer = 0
        this.setData({ wave: false })
      }, 700)
      // 200ms > 页面退出动画 160ms：药丸滑到位、页面滑出去之后再换页
      setTimeout(() => { wx.switchTab({ url: '/' + target.pagePath }) }, 200)
    },
  },
})