// user-orders.js — 查看指定用户/来源的订单列表
// 用于: 管理管理员点击、历史授权用户点击、本地打印任务
const { CONFIG } = require('../../utils/config')

Component({
  properties: {
    openid:    { type: String, value: '' },
    nickname:  { type: String, value: '' },
    source:    { type: String, value: '' },
  },

  data: {
    pageSlide: 'page-init',
    pageExit: '',
    isDarkMode: wx.getStorageSync('isDarkMode') || false,
    // 页面标题和过滤参数
    pageTitle: '订单列表',
    viewOpenid: '',       // 查看指定用户的 openid（为空则只看 source）
    viewNickname: '',
    sourceFilter: '',     // 'local' 表示本地打印任务，'' 表示云端任务

    orders: [],
    loading: true,

    // 分页
    currentPage: 1,
    perPage: 10,
    totalOrders: 0,
    totalPages: 0,
    pageOptions: [10, 20, 50, 100],
    showPageSizePicker: false,

    // 展开状态
    expandedOrders: {},

    statusMap: {
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
    },
  },

  lifetimes: {
    attached() {
      const app = getApp()
      this.setData({ isDarkMode: app.globalData.isDarkMode })
      const openid = this.data.openid || ''
      const nickname = this.data.nickname ? decodeURIComponent(this.data.nickname) : ''
      const source = this.data.source || ''

      let title = '订单列表'
      if (source === 'local') {
        title = '本地打印任务'
      } else if (nickname) {
        title = nickname + ' 的任务'
      }

      this.setData({
        viewOpenid: openid,
        viewNickname: nickname,
        sourceFilter: source,
        pageTitle: title,
      })

      // 直接传参，不依赖 this.data 是否就绪（对齐 authorized-users 的模式）
      this.loadOrders(openid, source)
    },
  },
  pageLifetimes: {
    show() {
      const app = getApp()
      const forward = wx.getStorageSync('_navForward')
      wx.removeStorageSync('_navForward')
      this.setData({
        pageSlide: forward ? 'page-enter-right' : 'page-enter-left',
        pageExit: '',
        isDarkMode: app.globalData.isDarkMode,
      })
      if (this._hasLoaded) {
        this.loadOrders(this.data.viewOpenid, this.data.sourceFilter)
      }
      this._startPolling()
    },
    hide() {
      this._stopPolling()
      const forward = wx.getStorageSync('_navForward')
      this.setData({ pageExit: forward ? 'page-exit-left' : 'page-exit-right' })
    },
  },

  methods: {
    // 轮询：每 10 秒静默刷新，确保 reserved → abandoned 等后端状态变更可见
    _startPolling() {
      this._stopPolling()
      this._pollTimer = setInterval(() => {
        this._pollOrdersSilent()
      }, 10000)
    },
    _stopPolling() {
      if (this._pollTimer) {
        clearInterval(this._pollTimer)
        this._pollTimer = null
      }
    },
    _pollOrdersSilent() {
      const token = wx.getStorageSync('token')
      if (!token) return
      const data = {
        page: this.data.currentPage,
        per_page: this.data.perPage,
      }
      if (this.data.viewOpenid) data.openid = this.data.viewOpenid
      if (this.data.sourceFilter) data.source = this.data.sourceFilter
      wx.request({
        url: CONFIG.BASE_URL + '/api/orders',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        data,
        success: (res) => {
          if (res.statusCode !== 200 || !res.data || !res.data.success) return
          const newOrders = (res.data.orders || [])
          // 格式化价格
          newOrders.forEach(order => {
            order.totalPriceDisplay = (order.total_price || 0).toFixed(2)
          })
          const oldOrders = this.data.orders || []
          // 仅更新变化的 status 字段，避免整列表重渲染
          const updates = {}
          let changed = false
          for (let i = 0; i < Math.min(newOrders.length, oldOrders.length); i++) {
            if (newOrders[i].id !== oldOrders[i].id) { changed = true; break }
            if (newOrders[i].status !== oldOrders[i].status) {
              updates['orders[' + i + '].status'] = newOrders[i].status
            }
            // 同步子文件状态
            if (newOrders[i].files && oldOrders[i].files) {
              for (let j = 0; j < Math.min(newOrders[i].files.length, oldOrders[i].files.length); j++) {
                if (newOrders[i].files[j].status !== oldOrders[i].files[j].status) {
                  updates['orders[' + i + '].files[' + j + '].status'] = newOrders[i].files[j].status
                }
              }
            }
          }
          if (newOrders.length !== oldOrders.length) changed = true
          if (changed) {
            this.setData({ orders: newOrders, expandedOrders: this.data.expandedOrders })
          } else if (Object.keys(updates).length > 0) {
            this.setData(updates)
          }
        },
        fail: () => {}
      })
    },

    loadOrders(openid, source) {
      const token = wx.getStorageSync('token')
      if (!token) {
        this.setData({ loading: false })
        return
      }

      this.setData({ loading: true })
      this._hasLoaded = true

      const data = {
        page: this.data.currentPage,
        per_page: this.data.perPage,
      }
      if (openid) {
        data.openid = openid
      }
      if (source) {
        data.source = source
      }

      wx.request({
        url: CONFIG.BASE_URL + '/api/orders',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        data: data,
        success: (res) => {
          if (res.statusCode === 200 && res.data && res.data.success) {
            const newOrders = (res.data.orders || [])
            // 预处理文件大小显示
            newOrders.forEach(order => {
              order.totalPriceDisplay = (order.total_price || 0).toFixed(2)
              if (order.files) {
                order.files.forEach(f => {
                  f.sizeDisplay = f.size ? (f.size / 1024).toFixed(1) + ' KB' : ''
                  const name = (f.original_name || f.file_name || '').toLowerCase()
                  f.isExcel = name.endsWith('.xls') || name.endsWith('.xlsx')
                })
                order.isExcel = order.files.length > 0 && order.files.every(f => f.isExcel)
              }
            })

            const total = res.data.total || 0
            this.setData({
              orders: newOrders,
              loading: false,
              totalOrders: total,
              totalPages: Math.ceil(total / this.data.perPage),
              expandedOrders: {},
            })
          } else {
            this.setData({ loading: false })
          }
        },
        fail: () => {
          wx.showToast({ title: '网络错误', icon: 'none' })
          this.setData({ loading: false })
        },
      })
    },

    // ==================== 分页 ====================

    onPageChange(e) {
      const page = parseInt(e.currentTarget.dataset.page, 10)
      if (page < 1 || page > this.data.totalPages || page === this.data.currentPage) return
      this.setData({ currentPage: page }, () => {
        this.loadOrders(this.data.viewOpenid, this.data.sourceFilter)
        // 滚动回顶部
        wx.pageScrollTo({ scrollTop: 0, duration: 200 })
      })
    },

    onPrevPage() {
      if (this.data.currentPage <= 1) return
      this.setData({ currentPage: this.data.currentPage - 1 }, () => {
        this.loadOrders(this.data.viewOpenid, this.data.sourceFilter)
        wx.pageScrollTo({ scrollTop: 0, duration: 200 })
      })
    },

    onNextPage() {
      if (this.data.currentPage >= this.data.totalPages) return
      this.setData({ currentPage: this.data.currentPage + 1 }, () => {
        this.loadOrders(this.data.viewOpenid, this.data.sourceFilter)
        wx.pageScrollTo({ scrollTop: 0, duration: 200 })
      })
    },

    onTogglePageSizePicker() {
      this.setData({ showPageSizePicker: !this.data.showPageSizePicker })
    },

    onSelectPageSize(e) {
      const size = parseInt(e.currentTarget.dataset.size, 10)
      if (isNaN(size)) return
      this.setData({
        perPage: size,
        currentPage: 1,
        showPageSizePicker: false,
      }, () => {
        this.loadOrders(this.data.viewOpenid, this.data.sourceFilter)
      })
    },

    // ==================== 订单卡片 ====================

    onOrderTap(e) {
      const orderId = e.currentTarget.dataset.id
      const expanded = { ...this.data.expandedOrders }
      if (expanded[orderId]) {
        delete expanded[orderId]
      } else {
        expanded[orderId] = true
      }
      this.setData({ expandedOrders: expanded })
    },

    onDetailCancelOrder(e) {
      const orderId = e.currentTarget.dataset.id
      const token = wx.getStorageSync('token')
      if (!token) return

      wx.showModal({
        title: '确认取消',
        content: '确定要取消这个打印任务吗？',
        success: (modalRes) => {
          if (!modalRes.confirm) return
          wx.showLoading({ title: '取消中...' })
          wx.request({
            url: CONFIG.BASE_URL + '/api/cancel_order',
            method: 'POST',
            header: { 'Authorization': 'Bearer ' + token, 'content-type': 'application/json' },
            data: { order_id: String(orderId) },
            success: (res) => {
              wx.hideLoading()
              if (res.data.success) {
                wx.showToast({ title: '已取消' })
                this.loadOrders(this.data.viewOpenid, this.data.sourceFilter)
              } else {
                wx.showToast({ title: res.data.message, icon: 'none' })
              }
            },
            fail: () => {
              wx.hideLoading()
              wx.showToast({ title: '网络错误', icon: 'none' })
            },
          })
        },
      })
    },

    // ==================== 生成页码数组（用于渲染 < 1 2 3 ... >） ====================

    getPageNumbers() {
      const total = this.data.totalPages
      const current = this.data.currentPage
      if (total <= 7) {
        return Array.from({ length: total }, (_, i) => i + 1)
      }
      const pages = []
      pages.push(1)
      if (current > 3) pages.push('...')
      const start = Math.max(2, current - 1)
      const end = Math.min(total - 1, current + 1)
      for (let i = start; i <= end; i++) {
        pages.push(i)
      }
      if (current < total - 2) pages.push('...')
      pages.push(total)
      return pages
    },
  },
})
