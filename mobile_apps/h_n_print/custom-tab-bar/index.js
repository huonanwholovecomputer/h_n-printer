// custom-tab-bar/index.js
// 原生自定义 tabBar 组件（app.json 中 "tabBar.custom": true 时由框架注入）
// 高亮通过每个 item 的 active 布尔值控制，避免 WXML 中 selected === index 的类型问题。
// pageLifetimes.show / attached 时根据当前路由自动同步，也支持页面通过 getTabBar().setData() 直接更新。
Component({
  data: {
    selected: 0,
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
    },
  },
  pageLifetimes: {
    show() {
      this._syncSelected()
    },
  },
  methods: {
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
      // 先更新高亮，再切换页面，保证点击瞬间就有视觉反馈
      const patch = { selected: index }
      this.data.list.forEach((item, i) => {
        if (item.active !== (i === index)) {
          patch[`list[${i}].active`] = (i === index)
        }
      })
      this.setData(patch)
      wx.switchTab({ url: "/" + target.pagePath })
    },
  },
})
