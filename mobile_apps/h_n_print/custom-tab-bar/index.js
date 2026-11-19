// custom-tab-bar/index.js
// 原生自定义 tabBar。
//
// 第 18 轮（2026-11-19，用户口径：「小程序端就不要药丸动画了，直接切换就行，手动滑动也删掉」）：
// 这个组件里**没有任何动画，也没有任何手势**。
//   · 药丸是静态指示块：位置 = 本页自己那一格 × 格宽，由 data 直接算出来，没有 transition；
//   · 视图层不再有 utils/tabbar.wxs —— 跟手拖动 / 85ms 滞后 / 橡皮筋 / 松手吸附 /
//     整条栏船式起伏 / 整排 wave / 落位动效，全部删除；
//   · 选中态（图标透明度、文字颜色、图标容器底色）也没有 200ms 过渡，切页那一帧一次换完。
//
// 为什么这样反而最稳：每个 tab 页各持一份 tabBar 实例，被隐藏的 webview 不再出帧 ——
// 屏上第一帧永远是它上次离场时的最后一帧。只要「本页 data 永远等于本页那一格」，
// 离场最后一帧 = 到场第一帧，两边天然对齐：
// 不需要交接点、不需要事后纠正、也没有「标志位没被清掉就永久错位」的死锁。
// （第 6~17 轮那些方案与踩过的坑记在 mobile_apps/docs/移动端_小程序.md 的 Tab 栏各轮小节里。）
//
// 本次启动内的「已落定格」：同一次启动里多份 tabBar 实例（每个 tab 页各一份）共享。
// 冷启动从 -1 开始，**绝不**回读 storage 里的 _tabCur —— 那里是上一条命留下的值，
// 页面栈还空的那一瞬读到它，药丸就会落到上一次的格（现场：在打印页、药丸在「我」）。
let sessionCur = -1

Component({
  data: {
    hideBorder: false,
    dark: false,
    selected: 0,
    // 药丸位移（px）：本页格 × 一格宽。落位后在整个页面生命周期里都不再变化。
    thumbX: 0,
    // 首帧显隐门（第 4 轮）：新实例 first paint 时 data 还是默认的 0，先不显形；
    // 「落位 + thumbReady」放进同一次 setData，于是显形即正确。
    thumbReady: false,
    list: [
      { text: '打印', icon: '/images/tab/print.png', pagePath: 'pages/index/index', active: true },
      { text: '我', icon: '/images/tab/me.png', pagePath: 'pages/me/me', active: false },
    ],
  },
  lifetimes: {
    attached() {
      this._geo = null
      this._settleTimer = 0
      this._refresh()
    },
    detached() {
      if (this._settleTimer) { clearTimeout(this._settleTimer); this._settleTimer = 0 }
    },
  },
  pageLifetimes: {
    // 回到屏上：清闸门 + 按真实 route 落一次位（位置本来就对，这一次 setData 通常只是把
    // selected / active 与真实页面校一遍）。
    show() {
      this._pageHidden = false
      this._refresh()
    },
    // 页面**真的被换走**（switchTab 落地）才会触发 —— 这是「切页到底落没落地」最可靠的信号。
    // _reconcile 靠它区分「switchTab 没生效」和「只是页面栈还没换成新页」：后者绝不能清 _tabWant
    // （第 9 轮现场：switchTab 落地慢了一拍，450ms 的核对把目标格当失败清掉了）。
    hide() {
      this._pageHidden = true
    },
  },
  observers: {
    // 页面会直接对本组件 setData（提交时切 hideBorder、onShow 时切 selected）：
    // 每次 setData 框架都会重新下发节点样式，所以这两个字段一变就按 data 补画一次。
    hideBorder() { this._repaint() },
    // 页面声明的选中格与药丸位置不一致就补画。这里不看任何「静默期」标志：
    // 现场那次事故正是「页面 onShow 的 setData 恰好落在静默期里」，补画被吞、药丸永久停在错格。
    // 已经对上就什么都不做（_refresh 自己下发的那一次不会多打一轮 setData）。
    selected() { this.syncPill() },
  },
  methods: {
    // 几何：一格多少 px。与 index.wxss 的 left: 9rpx / width: calc(50% - 18rpx) 必须一致，
    // 否则药丸落位会偏。
    _measure() {
      let winW = 375
      try {
        const info = wx.getWindowInfo ? wx.getWindowInfo() : wx.getSystemInfoSync()
        if (info && info.windowWidth) winW = info.windowWidth
      } catch (e) { /* 老基础库：用 375 兜底 */ }
      const pxPerRpx = winW / 750
      const barW = winW - 160 * pxPerRpx
      this._geo = { step: barW / 2 }
      return this._geo
    },

    // 本页应该落哪一格。三层（第 6 轮口径，第 18 轮保留）：
    // ① route 命中哪一格就是哪一格；
    // ② route 拿不到时用本次启动里已落定过的格（模块内存 sessionCur）——**不要**回退到
    //    data.selected：新实例的默认值是 0，那就是「药丸画到左格」；
    // ③ _goTab 在 1.5s 内记下的目标格优先于上面两层（它代表用户的最新意图）。
    _ownIdx() {
      let idx = -1
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
      if (idx < 0 && sessionCur >= 0 && sessionCur <= this.data.list.length - 1) idx = sessionCur
      if (idx < 0) idx = this.data.selected
      // 切 tab 落地这一刻页面栈可能还没换成新页：route 仍是上一页，而且它会「命中」旧页那一格
      // （不是未命中），所以这条兜底不能只写在 found < 0 的分支里。
      try {
        const want = Number(wx.getStorageSync('_tabWant'))
        const at = Number(wx.getStorageSync('_tabWantAt') || 0)
        if (want >= 0 && want <= this.data.list.length - 1 && at && Date.now() - at < 1500) idx = want
      } catch (e) {}
      return idx
    },

    // 页面出现/切回时对齐选中格（新页面的 tabBar 是新建实例，药丸直接落在这一格）
    _refresh() {
      const geo = this._measure()
      const idx = this._ownIdx()
      sessionCur = idx
      let dark = false
      try { dark = !!(getApp().globalData && getApp().globalData.isDarkMode) } catch (e) {}
      const patch = {
        selected: idx,
        dark,
        thumbX: idx * geo.step,
        thumbReady: true,
      }
      this.data.list.forEach((item, i) => {
        if (item.active !== (i === idx)) patch['list[' + i + '].active'] = (i === idx)
      })
      this.setData(patch)
      try { wx.setStorageSync('_tabCur', idx) } catch (e) {}   // 仅诊断镜像，不作落位依据
    },

    // 页面直接对本组件 setData 之后，按逻辑层的 selected 重新落位（位置永远由 selected 决定）
    _repaint() {
      const geo = this._geo || this._measure()
      const sel = this.data.selected || 0
      sessionCur = sel
      this.setData({ thumbX: sel * geo.step, thumbReady: true })
      try { wx.setStorageSync('_tabCur', sel) } catch (e) {}   // 仅诊断镜像，不作落位依据
    },

    // 页面 onShow 声明完选中格之后主动对一次（公开方法，两个 tab 页都会调）。
    // 为什么不能只靠 selected 观察者：页面随后 setData({selected: 同一个值}) 等于什么都没发生，
    // 观察者压根不开火 —— 药丸就停在那儿了。
    syncPill() {
      const geo = this._geo || this._measure()
      const sel = this.data.selected || 0
      const want = sel * geo.step
      if (Math.abs((Number(this.data.thumbX) || 0) - want) < 0.5) {
        // 位置与 selected 对得上，但两者有可能**一起**被旧 route 带偏（现场：切到「我」之后整条栏
        // 按打印页重画，selected=0、thumbX=0，屏上「打印高亮 + 药丸在左」，而页面明明是「我」）。
        // 这种「一致地错」靠位置比对永远发现不了 —— 必须拿真实页面校一次。
        if (this._disagreesWithRoute(sel)) this._refresh()
        return
      }
      this._repaint()
    },

    // 事先声明的格与「当前真实页面」是否冲突（_goTab 刚记下的目标格优先，切页途中不算冲突）
    _disagreesWithRoute(sel) {
      // 本页不在屏上时不算冲突：当前 route 指向的是**别人那一页**，拿它来校这一份实例，
      // 只会把它拽到别人的格上。
      if (this._pageHidden) return false
      let route = ''
      try {
        const pages = getCurrentPages()
        const cur = pages[pages.length - 1]
        if (cur && cur.route) route = cur.route
      } catch (e) { return false }
      if (!route) return false
      const found = this.data.list.findIndex((item) => item.pagePath === route)
      if (found < 0 || found === sel) return false
      try {
        const want = Number(wx.getStorageSync('_tabWant'))
        const at = Number(wx.getStorageSync('_tabWantAt') || 0)
        if (want === sel && at && Date.now() - at < 1500) return false
      } catch (e) {}
      return true
    },

    // 切页到底落没落地：switchTab 可能压根没生效（转场中重复调用 / 页面正被隐藏 / 低端机超时）。
    // 那时页面栈还停在原格，而它 onShow 的 setData({selected: 上一个值}) 因为「值没变」不会触发补画
    // —— 屏上就是「打印高亮 + 药丸在『我』」而且一直不恢复。450ms 后按真实 route 核一次。
    // pagePath 传的是**本实例所属那一页**（原页）：route 还是它 ⇒ switchTab 没落地。
    _reconcile(pagePath) {
      let route = ''
      try {
        const pages = getCurrentPages()
        const cur = pages[pages.length - 1]
        if (cur && cur.route) route = cur.route
      } catch (e) { return }
      if (!route || route !== pagePath) return
      // 页面已经被换走（pageLifetimes.hide 已触发）⇒ 切页真的落地了，只是 getCurrentPages()
      // 还没换成新页。这时候清 _tabWant 就是在拆自己刚铺的路：新页面的那份实例拿不到目标格，
      // 只能按旧 route 落回上一格 —— 现场「切到我又跳回打印、且永久停住」。
      if (this._pageHidden) return
      try {
        wx.removeStorageSync('_tabWant')
        wx.removeStorageSync('_tabWantAt')
      } catch (e) {}
      this._refresh()
    },

    // 点按切页（视图层 bindtap）。第 18 轮起视图层没有任何手势回调，这里是唯一入口。
    switchTab(e) {
      const index = Number(e.currentTarget.dataset.index)
      this._goTab(index)
    },

    _goTab(index) {
      const target = this.data.list[index]
      if (!target || index === this.data.selected) return
      const prev = this.data.selected
      // 每次切页重新计：本页此后是否真的被换走，只看这一次的 pageLifetimes.hide
      this._pageHidden = false
      // 目标格必须先落盘：animateExit 在「有弹窗」时会自己接管切页（关弹窗 → switchTab），
      // 不会回到下面这段 —— 那条路上的新页面 tabBar 拿不到 _tabWant，只能按旧 route 落 0，
      // 一帧后再被页面 onShow 的 setData(selected) 拽回来，看上去就是药丸先闪回上一格。
      // _tabFrom / _tabTo 是页面侧入场动画用的（pages/index/index.js、pages/me/me.js 都会读）。
      try {
        wx.setStorageSync('_tabFrom', prev)
        wx.setStorageSync('_tabTo', index)
        wx.setStorageSync('_tabWant', index)
        wx.setStorageSync('_tabWantAt', Date.now())
        // 目标格同时也是「本次启动的当前格」：切页途中若有一份新实例被创建（如被隐藏的打印页），
        // 它的 _refresh 拿不到 route，就会读这个值 —— 落在目标格。
        sessionCur = index
        wx.setStorageSync('_tabCur', index)   // 仅诊断镜像，不作落位依据
      } catch (e) {}
      const app = getApp()
      const isDark = app.globalData.isDarkMode
      wx.setBackgroundColor({ backgroundColor: isDark ? '#1C1C1E' : '#F2F2F7', backgroundColorTop: isDark ? '#1C1C1E' : '#F2F2F7', backgroundColorBottom: isDark ? '#1C1C1E' : '#F2F2F7' })
      wx.setNavigationBarColor({ frontColor: isDark ? '#ffffff' : '#000000', backgroundColor: isDark ? '#1C1C1E' : '#F2F2F7' })
      // 这里**一次 setData 都不发**（第 13 轮起的口径，第 18 轮沿用）：
      // 药丸停在本页格不动，选中态也留给切过去之后那一页的 _refresh ——
      // 因为每份实例的离场最后一帧就是下次到场的第一帧，本页只要一开始挪药丸，
      // 下一帧在别人那一页上就得再挪回来（第 6~17 轮的同一个坑）。
      // 点按的即时反馈由 hover 态 + 本页 160ms 退出动画承担。
      const fromPath = (this.data.list[prev] && this.data.list[prev].pagePath) || ''
      if (this._settleTimer) clearTimeout(this._settleTimer)
      // 这条核对必须两边都排：有弹窗时页面自己接管切页（下面 return），那条路同样需要兜底。
      this._settleTimer = setTimeout(() => {
        this._settleTimer = 0
        this._reconcile(fromPath)
      }, 450)
      const dir = prev === 0 ? 'left' : 'right'
      let cp = null
      try {
        const pages = getCurrentPages()
        cp = pages[pages.length - 1]
      } catch (e) {}
      if (cp && typeof cp.animateExit === 'function' && !cp.animateExit(dir)) return
      // 200ms > 页面退出动画 160ms：页面滑出去之后再换页
      setTimeout(() => {
        wx.switchTab({ url: '/' + target.pagePath })
      }, 200)
    },
  },
})
