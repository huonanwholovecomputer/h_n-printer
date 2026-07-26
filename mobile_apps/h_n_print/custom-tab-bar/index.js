// custom-tab-bar/index.js
// 原生自定义 tabBar 组件（app.json 中 "tabBar.custom": true 时由框架注入）
Component({
  data: {
    hideBorder: false,
    selected: 0,
    isDarkMode: wx.getStorageSync('isDarkMode') || false,
    list: [
      {
        text: "打印",
        icon: "/images/tab/print.png",
        pagePath: "pages/index/index",
        active: true,
      },
      {
        text: "我",
        icon: "/images/tab/me.png",
        pagePath: "pages/me/me",
        active: false,
      },
    ],
  },
  lifetimes: {
    attached() {
      this._syncSelected()
      this._syncTheme()
    },
  },
  pageLifetimes: {
    show() {
      this._syncSelected()
      this._syncTheme()
    },
  },
  methods: {
    _syncTheme() {
      const app = getApp()
      const dark = typeof app.globalData.isDarkMode === 'boolean'
        ? app.globalData.isDarkMode
        : (wx.getStorageSync('isDarkMode') || false)
      if (dark !== this.data.isDarkMode) {
        this.setData({ isDarkMode: dark })
      }
    },

    // 根据当前页面路由批量更新 list[].active 与 selected
    _syncSelected() {
      const pages = getCurrentPages()
      const cur = pages[pages.length - 1]
      if (!cur || !cur.route) return
      const route = cur.route
      const idx = this.data.list.findIndex((item) => item.pagePath === route)
      if (idx < 0) return
      const patch = {}
      let needPatch = false
      if (this.data.selected !== idx) {
        patch.selected = idx
        needPatch = true
      }
      this.data.list.forEach((item, i) => {
        const want = i === idx
        if (item.active !== want) {
          patch[`list[${i}].active`] = want
          needPatch = true
        }
      })
      if (needPatch) {
        this.setData(patch)
      }
    },

    switchTab(e) {
      const index = Number(e.currentTarget.dataset.index)
      const target = this.data.list[index]
      if (!target) return
      if (index === this.data.selected) return

      const prevSelected = this.data.selected
      const direction = prevSelected === 0 ? 'left' : 'right'

      // 询问当前页面是否允许切换（弹窗打开时先关弹窗，不切换）
      const pages = getCurrentPages()
      const curPage = pages[pages.length - 1]
      if (curPage && typeof curPage.animateExit === 'function') {
        const shouldSwitch = curPage.animateExit(direction)
        if (!shouldSwitch) return  // 弹窗被关闭，取消 tab 切换
      }

      wx.setStorageSync('_tabFrom', prevSelected)
      wx.setStorageSync('_tabTo', index)
      const patch = { selected: index }
      this.data.list.forEach((item, i) => {
        if (item.active !== (i === index)) {
          patch[`list[${i}].active`] = (i === index)
        }
      })
      this.setData(patch)

      setTimeout(() => {
        const app = getApp()
        const bg = (app.globalData.isDarkMode) ? '#1C1C1E' : '#F2F2F7'
        wx.setBackgroundColor({ backgroundColor: bg, backgroundColorTop: bg, backgroundColorBottom: bg })
        wx.switchTab({ url: "/" + target.pagePath })
      }, 240)
    },
  },
})
