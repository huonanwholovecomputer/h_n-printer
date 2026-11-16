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
    // 位移层由 data 兜底的落位（px）：视图层抹掉内联样式后的最后一道保险
    thumbX: 0,
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
      this._quiet = false
      this._refresh()
    },
    detached() {
      if (this._waveTimer) { clearTimeout(this._waveTimer); this._waveTimer = 0 }
    },
  },
  pageLifetimes: {
    show() { this._refresh() },
  },
  observers: {
    // 页面会直接对 tabBar setData（提交时切 hideBorder、onShow 时切 selected）：
    // 每次 setData 框架都会重新下发节点 style，把视图层写的位移整条抹掉，
    // 所以这两个字段一变就补画一次（只重画，不动位置）。
    hideBorder() { if (!this._quiet) this._repaint() },
    selected() { if (!this._quiet) this._repaint() },
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
      // 只有「刚切 tab 落到本页」才播整排 wave；从子页返回不播，免得每回一次抖一下
      let arriving = false
      try {
        const at = Number(wx.getStorageSync('_tabWaveAt') || 0)
        if (at && Date.now() - at < 1500) {
          arriving = true
          wx.removeStorageSync('_tabWaveAt')
        }
      } catch (e) {}
      this._rev += 1
      const patch = {
        selected: idx,
        dark,
        thumbX: idx * geo.step,
        cfg: { step: geo.step, maxIdx: geo.maxIdx, over: geo.over, sel: idx, rev: this._rev, snap: true },
      }
      if (arriving) patch.wave = true
      this._markQuiet()
      this.data.list.forEach((item, i) => {
        if (item.active !== (i === idx)) patch['list[' + i + '].active'] = (i === idx)
      })
      this.setData(patch)
      if (arriving) {
        // 收 wave 也是一次 setData：此时药丸已经停在选中格，抹掉再补画看不出差别
        if (this._waveTimer) clearTimeout(this._waveTimer)
        this._waveTimer = setTimeout(() => {
          this._waveTimer = 0
          this._rev += 1
          this._markQuiet()
          this.setData({
            wave: false,
            cfg: { step: geo.step, maxIdx: geo.maxIdx, over: geo.over, rev: this._rev, snap: false },
          })
        }, 700)
      }
    },

    // 自己 setData 期间不要触发 observers 的补画（否则一次切换会多打一轮）
    _markQuiet() {
      this._quiet = true
      const done = () => { this._quiet = false }
      if (wx.nextTick) wx.nextTick(done)
      else setTimeout(done, 0)
    },

    // 只把位置补画回来（不动 state.pill / state.sel）—— 用于 setData 抹掉内联样式之后
    _repaint() {
      const geo = this._geo || this._measure()
      this._rev += 1
      this.setData({
        thumbX: (this.data.selected || 0) * geo.step,
        cfg: { step: geo.step, maxIdx: geo.maxIdx, over: geo.over, rev: this._rev, snap: false },
      })
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
      // 关键：这一路**不做任何 setData**。逻辑层 setData 会让框架重新下发节点 style，
      // 把视图层正在做的位移整条抹掉 —— 表现就是「滑块滑到一侧后突然闪现到另一侧」。
      // 选中态 / 整排 wave / 药丸落位全交给切过去之后那一页的 _refresh：
      // 新页面的 tabBar 本来就是新建实例，不存在「旧实例要跟着改」这回事。
      try { wx.setStorageSync('_tabWaveAt', Date.now()) } catch (e) {}
      // 200ms > 页面退出动画 160ms：药丸滑到位、页面滑出去之后再换页
      setTimeout(() => { wx.switchTab({ url: '/' + target.pagePath }) }, 200)
    },
  },
})