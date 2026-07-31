// custom-tab-bar/index.js
// 原生自定义 tabBar — 固定配色（不随主题变化），避免重建时首帧闪色
Component({
  data: {
    hideBorder: false,
    selected: 0,
    list: [
      { text: "打印", icon: "/images/tab/print.png", pagePath: "pages/index/index", active: true },
      { text: "我", icon: "/images/tab/me.png", pagePath: "pages/me/me", active: false },
    ],
  },
  lifetimes: {
    attached() { this._syncSelected() },
  },
  pageLifetimes: {
    show() { this._syncSelected() },
  },
  methods: {
    _syncSelected() {
      const pages = getCurrentPages()
      const cur = pages[pages.length - 1]
      if (!cur || !cur.route) return
      const route = cur.route
      const idx = this.data.list.findIndex((item) => item.pagePath === route)
      if (idx < 0) return
      const patch = {}
      let needPatch = false
      if (this.data.selected !== idx) { patch.selected = idx; needPatch = true }
      this.data.list.forEach((item, i) => {
        if (item.active !== (i === idx)) { patch[`list[${i}].active`] = (i === idx); needPatch = true }
      })
      if (needPatch) this.setData(patch)
    },
    switchTab(e) {
      const index = Number(e.currentTarget.dataset.index)
      const target = this.data.list[index]
      if (!target || index === this.data.selected) return
      const app = getApp()
      const isDark = app.globalData.isDarkMode
      wx.setBackgroundColor({ backgroundColor: isDark ? '#1C1C1E' : '#F2F2F7', backgroundColorTop: isDark ? '#1C1C1E' : '#F2F2F7', backgroundColorBottom: isDark ? '#1C1C1E' : '#F2F2F7' })
      wx.setNavigationBarColor({ frontColor: isDark ? '#ffffff' : '#000000', backgroundColor: isDark ? '#1C1C1E' : '#F2F2F7' })
      const prev = this.data.selected, dir = prev === 0 ? 'left' : 'right'
      const pages = getCurrentPages(), cp = pages[pages.length - 1]
      if (cp && typeof cp.animateExit === 'function' && !cp.animateExit(dir)) return
      wx.setStorageSync('_tabFrom', prev); wx.setStorageSync('_tabTo', index)
      const patch = { selected: index }
      this.data.list.forEach((item, i) => { if (item.active !== (i === index)) patch[`list[${i}].active`] = (i === index) })
      this.setData(patch)
      setTimeout(() => { wx.switchTab({ url: "/" + target.pagePath }) }, 240)
    },
  },
})
