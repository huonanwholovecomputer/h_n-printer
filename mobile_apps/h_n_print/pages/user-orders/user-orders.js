// user-orders.js — 查看指定用户/来源的订单列表
// 用于: 管理管理员点击、历史授权用户点击、本地打印任务
const { CONFIG } = require('../../utils/config')
const { request } = require('../../utils/request')

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
    loadError: '',

    // 分页
    currentPage: 1,
    perPage: 10,
    totalOrders: 0,
    totalPages: 0,
    // 页码数组（预计算进 data，避免 WXML 内调用方法 + wx:key="*this" 在部分基础库下渲染失败）
    pageNumbers: [],
    pageOptions: [10, 20, 50, 100],
    showPageSizePicker: false,

    // 展开状态
    expandedOrders: {},

    // 被查看用户的卡片信息（从管理入口跳转后显示）
    userDetail: null,
    showLicenseDetail: false,

    // 列表滚动控制（对齐“我”页面：切换页码/每页条数后自动滚动到列表顶部）
    listScrollTop: 0,
    scrollWithAnimation: false,

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
      scheduled: '已预约',
      downloading: '文件传输中',
      waiting: '等待打印',
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
      if (openid) {
        this.loadUserDetail(openid)
      }
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
      request({
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

      this.setData({ loading: true, loadError: '' })
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

      request({
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
              loadError: '',
              totalOrders: total,
              totalPages: Math.ceil(total / this.data.perPage),
              expandedOrders: {},
            }, () => {
              this._syncPageNumbers()
            })
          } else {
            // 页面内状态提示（对齐 Android App）：不清空已加载列表
            this.setData({ loading: false, loadError: res.data && res.data.message ? res.data.message : '加载失败' })
          }
        },
        fail: () => {
          this.setData({ loading: false, loadError: '网络错误' })
        },
      })
    },

    // ==================== 被查看用户卡片 ====================

    loadUserDetail(openid) {
      const token = wx.getStorageSync('token')
      if (!token || !openid) return
      request({
        url: CONFIG.BASE_URL + '/api/admin/user_detail',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        data: { openid },
        success: (res) => {
          if (res.statusCode === 200 && res.data && res.data.success) {
            const detail = res.data
            detail.avatarChar = (detail.nickname || '?')[0]
            // 头像 URL 加时间戳防缓存，确保显示最新头像
            if (detail.avatar_url) {
              detail.avatar_url = detail.avatar_url + '?t=' + Date.now()
            }
            this.setData({ userDetail: detail })
          }
        },
        fail: () => {}
      })
    },

    onToggleLicenseDetail() {
      this.setData({ showLicenseDetail: !this.data.showLicenseDetail })
    },

    // 滚动到任务列表顶部（与“我”页面一致：切换页码/每页条数后自动回到列表顶部）
    _scrollToOrdersSection() {
      const q = this.createSelectorQuery()
      q.select('.orders-section').boundingClientRect()
      q.select('.scrollarea').boundingClientRect()
      q.exec((res) => {
        if (!res || !res[0] || !res[1]) return
        const target = Math.max(0, res[0].top - res[1].top - 12)
        this.setData({ listScrollTop: target, scrollWithAnimation: true })
      })
    },

    // ==================== 分页 ====================

    onPageChange(e) {
      const page = parseInt(e.currentTarget.dataset.page, 10)
      if (page < 1 || page > this.data.totalPages || page === this.data.currentPage) return
      this.setData({ currentPage: page }, () => {
        this._syncPageNumbers()
        this.loadOrders(this.data.viewOpenid, this.data.sourceFilter)
        this._scrollToOrdersSection()
      })
    },

    onPrevPage() {
      if (this.data.currentPage <= 1) return
      this.setData({ currentPage: this.data.currentPage - 1 }, () => {
        this._syncPageNumbers()
        this.loadOrders(this.data.viewOpenid, this.data.sourceFilter)
        this._scrollToOrdersSection()
      })
    },

    onNextPage() {
      if (this.data.currentPage >= this.data.totalPages) return
      this.setData({ currentPage: this.data.currentPage + 1 }, () => {
        this._syncPageNumbers()
        this.loadOrders(this.data.viewOpenid, this.data.sourceFilter)
        this._scrollToOrdersSection()
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
        this._syncPageNumbers()
        this.loadOrders(this.data.viewOpenid, this.data.sourceFilter)
        this._scrollToOrdersSection()
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
          request({
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

    // 将页码数组同步到 data（供 WXML wx:for 使用，规避方法调用绑定在部分基础库下的渲染问题）
    _syncPageNumbers() {
      const pages = this.getPageNumbers()
      const prev = this.data.pageNumbers || []
      if (pages.length !== prev.length || pages.some((p, i) => p !== prev[i])) {
        this.setData({ pageNumbers: pages })
      }
    },
  },
})
