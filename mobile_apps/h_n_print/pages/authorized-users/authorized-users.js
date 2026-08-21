// pages/authorized-users/authorized-users.js
// F10: 历史授权用户列表（管理员/超级管理员可见）
// 展示所有被本管理员授权过的用户（管理员+临时用户，含已移除/已过期），
// 卡片含密钥类型与当前状态，可展开查看该用户的全部密钥记录（多次授权）。
const { CONFIG } = require('../../utils/config')

Component({
  data: {
    pageSlide: 'page-init',
    pageExit: '',
    isDarkMode: wx.getStorageSync('isDarkMode') || false,
    users: [],
    loading: true,
    loadError: '',
    expandedUsers: {},
    scrollConfig: { minY: 0, maxY: 0, scrollerH: 0, contentH: 0, listOverflow: false },
    scrollCmd: null,
    statusMap: {
      active: '临时授权中',
      expired: '已过期',
      removed: '已移除',
    },
    keyStatusMap: {
      unused: '未使用',
      used: '已使用',
      revoked: '已作废',
      finished: '已完成',
      archived: '归档',
    },
    // 管理员密钥的关联订单：{ [key]: { expanded, loading, loaded, orders } }
    keyOrders: {},
    // 订单状态文案（对齐 user-orders 页）
    orderStatusMap: {
      queued: '排队中',
      printing: '待添加',
      accepted: '已添加',
      offline_unknown: '断线未知',
      sent: '已完成',
      failed: '失败',
      abandoned: '放弃打印',
      rejected: '被打回',
      canceled: '已取消',
      reserved: '已预留',
      scheduled: '已预约',
      downloading: '文件传输中',
      waiting: '等待打印',
    },
  },

  pageLifetimes: {
    show() {
      const app = getApp()
      wx.removeStorageSync('_navForward')
      this.setData({
        pageSlide: 'page-fade-in',   // 进入位移由系统 push 动画负责，这里只做纯淡入，避免双重位移
        pageExit: '',
        isDarkMode: app.globalData.isDarkMode,
      })
      this._scheduleMeasure()
      // 每次返回本页都静默刷新（对齐 APP：从订单页返回可能已移除用户/密钥状态变化）
      if (this._hasLoaded) this.loadUsers()
    },
    hide() {
      // 返回由系统 navigateBack 动画负责交叉过渡，不再播自定义退出动画
    },
  },

  lifetimes: {
    attached() {
      const app = getApp()
      this.setData({ isDarkMode: app.globalData.isDarkMode })
      this._initScrollEngine()
      this.loadUsers()
    },
    detached() {
      this._destroyScrollEngine()
    },
  },

  methods: {
    // ==================== WXS 滚动引擎（视图层直驱，0 setData） ====================
    _initScrollEngine() {
      this._scrollerH = 0
      this._contentH = 0
      this._maxY = 0
      this._bottomPad = 20
      this._measureTimer = null
      this._scheduleMeasure()
      setTimeout(() => this._scheduleMeasure(), 400)
      setTimeout(() => this._scheduleMeasure(), 800)
    },

    _destroyScrollEngine() {
      if (this._measureTimer) {
        clearTimeout(this._measureTimer)
        this._measureTimer = null
      }
    },

    // 去抖测量：内容变化后延迟刷新滚动边界
    _scheduleMeasure(delay) {
      if (this._measureTimer) clearTimeout(this._measureTimer)
      this._measureTimer = setTimeout(() => {
        this._measureTimer = null
        this._measure()
      }, delay || 100)
    },

    _measure() {
      const q = this.createSelectorQuery()
      q.select('.scroller').boundingClientRect()
      q.select('.scroll-content').boundingClientRect()
      q.exec((res) => {
        if (!res || !res[0] || !res[1]) return
        const vp = res[0].height || 0
        const ch = res[1].height || 0
        this._scrollerH = vp
        this._contentH = ch
        this._maxY = Math.max(0, ch - vp + this._bottomPad)
        this._pushScrollConfig()
      })
    },

    // 推送滚动边界给 WXS 引擎（change:prop 观察器，低频同步，不逐帧走 setData）
    _pushScrollConfig() {
      this.setData({
        scrollConfig: {
          minY: 0,
          maxY: Math.round(this._maxY || 0),
          scrollerH: this._scrollerH || 0,
          contentH: this._contentH || 0,
          listOverflow: false,
        },
      })
    },

    // WXS 分桶回调（每 100px 一次），本页暂不消费
    onWxsScroll() {},

    _navigateWithAnimation(url) {
      wx.setStorageSync('_navForward', '1')
      // 系统 push 动画负责交叉过渡，不再播自定义退出动画
      wx.navigateTo({ url })
    },

    loadUsers() {
      const token = wx.getStorageSync('token')
      if (!token) {
        wx.showToast({ title: '请先登录', icon: 'none' })
        return
      }
      this._hasLoaded = true
      this.setData({ loading: true, loadError: '' })
      wx.request({
        url: CONFIG.BASE_URL + '/api/authorized_users',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          if (res.data && res.data.success) {
            const users = (res.data.users || []).map(u => {
              const records = u.records || []
              // 独立许可标签：按密钥类型去重（管理员在前），不合并成“管理员+临时”
              const licenseTags = []
              if (records.some(r => (r.type || 'temp') === 'admin')) {
                licenseTags.push({ label: '管理员许可', cls: 'tag-admin' })
              }
              if (records.some(r => (r.type || 'temp') === 'temp')) {
                licenseTags.push({ label: '临时许可', cls: 'tag-temp' })
              }
              if (!licenseTags.length) {
                // 兜底：老数据无 records 时按 license_type 字段
                if (u.license_type === 'admin' || u.license_type === 'both') {
                  licenseTags.push({ label: '管理员许可', cls: 'tag-admin' })
                }
                if (u.license_type === 'temp' || u.license_type === 'both') {
                  licenseTags.push({ label: '临时许可', cls: 'tag-temp' })
                }
              }
              return {
                ...u,
                avatarChar: (u.nickname || '?')[0],
                // 头像 URL 加时间戳防缓存，确保每次打开显示最新头像
                avatarUrl: u.avatar_url ? u.avatar_url + '?t=' + Date.now() : '',
                licenseTags,
                // 永久管理员不再单独显示状态标签（许可类型标签已表达“管理员许可”）
                statusLabel: u.status === 'permanent' ? '' : (this.data.statusMap[u.status] || u.status),
              }
            })
            this.setData({ users, loadError: '' })
            this._scheduleMeasure(100)
          } else {
            // 页面内状态提示（对齐 Android App）：不清空已加载列表
            this.setData({ loadError: res.data.message || '加载失败' })
          }
        },
        fail: () => {
          this.setData({ loadError: '网络错误' })
        },
        complete: () => {
          this.setData({ loading: false })
          this._scheduleMeasure()
        },
      })
    },

    onUserTap(e) {
      const openid = e.currentTarget.dataset.openid
      const nickname = e.currentTarget.dataset.nickname || ''
      this._navigateWithAnimation(
        `/pages/user-orders/user-orders?openid=${openid}&nickname=${encodeURIComponent(nickname)}`
      )
    },

    // 展开/收起该用户的全部密钥记录
    onToggleRecords(e) {
      const openid = e.currentTarget.dataset.openid
      const expanded = { ...this.data.expandedUsers }
      if (expanded[openid]) {
        delete expanded[openid]
      } else {
        expanded[openid] = true
      }
      this.setData({ expandedUsers: expanded })
      this._scheduleMeasure()
      // 密钥记录展开/收起有 0.32s 动画，收起后内容变短需再次测量（对齐 APP）
      setTimeout(() => this._scheduleMeasure(300), 300)
    },

    // 管理员密钥：展开/收起关联订单列表（首次展开拉取该用户订单，缓存复用）
    onToggleRecordOrders(e) {
      const key = e.currentTarget.dataset.key
      const openid = e.currentTarget.dataset.openid
      const ko = { ...this.data.keyOrders }
      const cur = ko[key] || { expanded: false, loading: false, loaded: false, orders: [] }
      const expanded = !cur.expanded
      ko[key] = { ...cur, expanded }
      this.setData({ keyOrders: ko })
      if (expanded && !cur.loaded && !cur.loading) {
        this._loadKeyOrders(key, openid)
      }
      this._scheduleMeasure()
      setTimeout(() => this._scheduleMeasure(300), 300)
    },

    _loadKeyOrders(key, openid) {
      const token = wx.getStorageSync('token')
      if (!token) return
      this.setData({ ['keyOrders.' + key + '.loading']: true })
      wx.request({
        url: CONFIG.BASE_URL + '/api/orders?openid=' + encodeURIComponent(openid) + '&per_page=50',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          const orders = (res.data && res.data.success) ? (res.data.orders || []) : []
          this.setData({
            ['keyOrders.' + key + '.loading']: false,
            ['keyOrders.' + key + '.loaded']: true,
            ['keyOrders.' + key + '.orders']: orders,
          })
          this._scheduleMeasure()
        },
        fail: () => {
          this.setData({
            ['keyOrders.' + key + '.loading']: false,
            ['keyOrders.' + key + '.loaded']: true,
            ['keyOrders.' + key + '.orders']: [],
          })
        },
      })
    },
  },
})
