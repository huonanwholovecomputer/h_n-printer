// pages/recent-orders/recent-orders.js
// #3：管理员的「最近订单」独立页（原来内嵌在「我」页里，只有最近 N 条、看不了全部）。
//
// 数据：GET /api/admin/recent_orders?page=&per_page=（后端 C5 契约）
//   带 page 时返回 { success, orders, total, page, per_page, total_pages }；
//   不带 page 时保持旧结构（只有 orders/limit）—— 所以本页**永远带 page**，
//   并在 total 缺失时退化成「只有一页」，绝不因为后端没升级就白屏。
//
// 交互：#9 异常状态文字可点 → 原因气泡（复制/关闭）；金额可点 → 复制价格（与本地工具同格式）。
// 滚动：与 user-orders 完全同一套 WXS 引擎（视图层直驱，0 setData），不自造第二套。
const { CONFIG } = require('../../utils/config')
const { request } = require('../../utils/request')

/* ===== 异常状态原因（#9，与「我」页同判据）===== */
const REASON_STATUSES = ['failed', 'timeout', 'canceled', 'rejected', 'abandoned']
const REASON_FALLBACK_TEXT = '该订单未提供具体原因'

const PER_PAGE_OPTIONS = [10, 20, 50, 100]
const PER_PAGE_KEY = 'recentOrdersPerPage'   // 用户选过的每页条数（跨页面记忆）

// 「最近 N 条」上限：超管在「我」页配置（/api/admin/recent_orders_limit），所有管理员可读。
// 本页只展示最近 N 条（分页也只在这 N 条里翻）；读不到配置时按 10 兜底，与「我」页一致。
const DEFAULT_RECENT_LIMIT = 10
const RECENT_LIMIT_MAX = 100

// 订单时间戳 → YYYY-MM-DD HH:MM:SS（manual_status_at 展示用；认不出格式就原样返回）
function _fmtStatusTime(v) {
  const s = String(v == null ? '' : v).trim()
  if (!s) return ''
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/)
  if (m) return m[1] + '-' + m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5] + ':' + m[6]
  return s
}

// 列表行时间：只留「MM-DD HH:MM」，列表里不需要年份与秒
function _shortTime(v) {
  const s = String(v == null ? '' : v).trim()
  if (!s) return ''
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/)
  if (m) return m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5]
  return s
}

function _money(n) {
  return (Math.round((Number(n) || 0) * 100) / 100).toFixed(2)
}

function _initialPerPage() {
  const saved = parseInt(wx.getStorageSync(PER_PAGE_KEY), 10)
  return PER_PAGE_OPTIONS.indexOf(saved) >= 0 ? saved : 20
}

Component({
  data: {
    pageSlide: 'page-init',
    pageExit: '',
    isDarkMode: wx.getStorageSync('isDarkMode') || false,
    pageTitle: '最近订单',

    orders: [],
    loading: true,
    loadError: '',
    noPermission: false,

    // 分页（后端 C5：page 从 1 起；per_page 只认 10/20/50/100）
    currentPage: 1,
    perPage: 20,
    totalOrders: 0,
    // 后端全站总量（不受「最近 N 条」限制）：只在汇总行里提示「全站共 X 条」，用于解释为什么看不到更早的订单
    serverTotalOrders: 0,
    // 超管在「我」页配的「最近 N 条」上限；0 = 还没取到（首次进入先取配置再拉列表，避免先闪一次全站订单）
    recentLimit: 0,
    totalPages: 0,
    pageNumbers: [],
    pageOptions: PER_PAGE_OPTIONS,
    showPageSizePicker: false,

    statusMap: {
      queued: '排队中',
      printing: '待添加',
      accepted: '已添加',
      offline_unknown: '断线未知',
      sent: '已完成',
      failed: '失败',
      timeout: '超时',
      abandoned: '放弃打印',
      rejected: '被打回',
      canceled: '已取消',
      reserved: '已预留',
      scheduled: '已预约',
      downloading: '文件传输中',
      waiting: '等待打印',
    },

    // #9 原因气泡：挂在 page-root 外 + position:fixed（订单行不带 overflow 裁剪，
    // 但列表容器会随滚动移动，只有 fixed + 视口坐标才不会被 scroll-content 的 translateY 带偏）
    showStatusPop: false,
    statusPopOrderId: '',
    statusPopText: '',
    statusPopWidth: 0,
    statusPopLeft: 0,
    statusPopCaretLeft: 0,
    statusPopBottom: 0,
    statusPopTop: 0,
    statusPopBelow: false,

    // WXS 滚动引擎（与 user-orders 一致）
    scrollConfig: { minY: 0, maxY: 0, scrollerH: 0, contentH: 0, listOverflow: false },
    scrollCmd: null,
  },

  lifetimes: {
    attached() {
      const app = getApp()
      this.setData({
        isDarkMode: (app && app.globalData && app.globalData.isDarkMode) || this.data.isDarkMode,
        perPage: _initialPerPage(),
      })
      this._initScrollEngine()

      // 页面本身只对管理员开放（后端 /api/admin/recent_orders 也会校验）：
      // 非管理员进来直接给提示，不发无效请求 —— 避免 403 被当成「网络错误」误导用户。
      const role = wx.getStorageSync('userRole')
      if (role !== 'admin' && role !== 'super_admin') {
        this.setData({ noPermission: true, loading: false })
        return
      }
      this.loadLimitAndOrders()
    },
    detached() {
      this._destroyScrollEngine()
    },
  },

  pageLifetimes: {
    show() {
      const app = getApp()
      this.setData({
        pageSlide: 'page-fade-in',
        pageExit: '',
        isDarkMode: (app && app.globalData && app.globalData.isDarkMode) || this.data.isDarkMode,
      })
      // 从别的页回来只做静默刷新（保留当前页码），不重置到第 1 页
      if (this._hasLoaded && !this.data.noPermission) this.loadOrders(true)
      this._scheduleMeasure()
    },
  },

  methods: {
    // ==================== WXS 滚动引擎（与 user-orders 同一套） ====================
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

    onWxsScroll() {},

    // ==================== 数据 ====================

    // 先取「最近 N 条」上限，再拉列表：顺序不能反 —— 否则首屏会先按全站总量渲染一次，
    // 用户会看到「全部订单」闪一下（这正是 #1 的现象）。刷新按钮也走这里，让设置改动立即生效。
    loadLimitAndOrders() {
      if (this._limitLoading) return
      // 没 token 连设置都不问（与 loadOrders 的登录守卫一致，避免空 Authorization 打一趟 401）
      if (!wx.getStorageSync('token')) {
        this.setData({ loading: false, loadError: '登录已过期，请重新进入小程序' })
        return
      }
      this._limitLoading = true
      this.setData({ loading: true, loadError: '' })
      this._fetchRecentLimit((limit) => {
        this._limitLoading = false
        this.setData({ recentLimit: limit }, () => this.loadOrders())
      })
    },

    // GET /api/admin/recent_orders_limit：所有管理员可读（写权限才限超管）。
    // 读不到就按默认 10 兜底，绝不退回「全站所有订单」。
    _fetchRecentLimit(done) {
      request({
        url: CONFIG.BASE_URL + '/api/admin/recent_orders_limit',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + wx.getStorageSync('token') },
        success: (res) => {
          const d = (res && res.data) || {}
          const n = parseInt(d.limit, 10)
          if (res && res.statusCode === 200 && d.success && isFinite(n) && n >= 1) {
            done(Math.min(n, RECENT_LIMIT_MAX))
          } else {
            done(DEFAULT_RECENT_LIMIT)
          }
        },
        fail: () => done(DEFAULT_RECENT_LIMIT),
      })
    },

    // 当前生效的条数上限（data 里还没取到配置时按默认值，保证任何调用路径都有确定行为）
    _recentLimitCap() {
      const n = parseInt(this.data.recentLimit, 10)
      return isFinite(n) && n >= 1 ? Math.min(n, RECENT_LIMIT_MAX) : DEFAULT_RECENT_LIMIT
    },

    // silent=true：翻页/回页的静默刷新（不闪加载态、不清空已有列表）
    loadOrders(silent) {
      const token = wx.getStorageSync('token')
      if (!token) {
        this.setData({ loading: false, loadError: '登录已过期，请重新进入小程序' })
        return
      }
      if (this._loading) return
      this._loading = true
      this._hasLoaded = true
      if (!silent) this.setData({ loading: true, loadError: '' })

      const perPage = this.data.perPage
      const cap = this._recentLimitCap()
      // 在请求前就把页码夹进「上限内的页数」：N 比每页条数还小时只有一页，
      // 避免为第 2 页发一次必然返回空数组的请求。
      const capPages = Math.max(1, Math.ceil(cap / perPage))
      const reqPage = Math.min(this.data.currentPage, capPages)

      request({
        url: CONFIG.BASE_URL + '/api/admin/recent_orders',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        data: { page: reqPage, per_page: perPage },
        success: (res) => {
          this._loading = false
          if (res.statusCode === 200 && res.data && res.data.success) {
            const total = Number(res.data.total)
            // 旧版后端（不带 total）退化成单页：total 取本页条数，页数只有 1，
            // 「下一页」自然禁用 —— 至少不会出现 NaN 页码或永远转圈。
            const totalSafe = isFinite(total) && total >= 0 ? total : (res.data.orders || []).length
            // #1 真因：后端分页返回的是**全站** total，且 paging 模式不认 limit；
            // 所以「最近 N 条」必须由前端截断 —— 只保留最近 N 条，分页也按 N 算页数。
            const cappedTotal = Math.min(totalSafe, cap)
            const offset = (reqPage - 1) * perPage
            // N 落在本页中间时本页条数不足 perPage，多出来的（比 N 更早的）订单直接丢掉
            const rows = (res.data.orders || [])
              .slice(0, Math.max(0, cappedTotal - offset))
              .map((o) => this._decorate(o))
            const totalPages = Math.max(1, Math.ceil(cappedTotal / perPage))
            const page = Math.min(reqPage, totalPages)
            // 全站总量比 cap 还小（后端数据被清理过）：页码可能已越界，回第 1 页重拉一次
            if (page !== reqPage) {
              this.setData({ currentPage: page }, () => {
                this._syncPageNumbers()
                this.loadOrders(true)
              })
              return
            }
            this.setData({
              orders: rows,
              loading: false,
              loadError: '',
              currentPage: page,
              totalOrders: cappedTotal,
              serverTotalOrders: totalSafe,
              totalPages,
            }, () => {
              this._syncPageNumbers()
              this._scheduleMeasure(80)
            })
          } else {
            this.setData({
              loading: false,
              loadError: (res.data && res.data.message) ? res.data.message : '加载失败',
            })
            this._scheduleMeasure()
          }
        },
        fail: () => {
          this._loading = false
          this.setData({ loading: false, loadError: '网络错误' })
          this._scheduleMeasure()
        },
      })
    },

    // 行装饰（WXML 不能调方法/不能 toFixed，全部预计算进 data）
    _decorate(o) {
      const src = o || {}
      const total = Math.round((Number(src.total_price) || 0) * 100) / 100
      const files = Number(src.file_count) || 0
      const pages = Number(src.page_count) || 0
      const status = src.status || ''
      const order = {
        order_id: src.order_id,
        order_number: src.order_number || '',
        owner: src.owner || '',
        ownerText: src.owner_masked || src.owner || '—',
        created_at: src.created_at || '',
        timeShort: _shortTime(src.created_at),
        file_count: files,
        page_count: pages,
        statText: files + ' 文件 · ' + pages + ' 页',
        total_price: total,
        priceDisplay: total.toFixed(2),
        status,
        statusText: this.data.statusMap[status] || status || '—',
        statusReasonTappable: REASON_STATUSES.indexOf(status) >= 0,
        source: src.source || '',
        is_admin_print: !!src.is_admin_print,
        // #4：人工订正留痕 / 失败原因必须原样透传 —— 这里以前只挑展示字段，
        // _statusReasonText() 永远读不到 manual_status_by，只能落到兜底文案。
        status_reason: src.status_reason || '',
        manual_status_by: src.manual_status_by || '',
        manual_status_at: src.manual_status_at || '',
      }
      order.statusReasonText = this._statusReasonText(order)
      return order
    },

    // 原因文案：人工订正记录 > 后端 status_reason > 通用文案（气泡永远有内容可复制）
    _statusReasonText(o) {
      if (!o) return REASON_FALLBACK_TEXT
      const by = String(o.manual_status_by || '').trim()
      const label = (this.data.statusMap && this.data.statusMap[o.status]) || o.status || '异常状态'
      if (by) {
        // 与 settlement.html 的 title 口径一致：缺时间时写「未知时间」，不出现空串/破折号
        const at = _fmtStatusTime(o.manual_status_at) || '未知时间'
        return by + '在' + at + '被标记为' + label
      }
      const reason = String(o.status_reason || '').trim()
      return reason || REASON_FALLBACK_TEXT
    },

    _findOrder(orderId) {
      const key = String(orderId)
      const list = this.data.orders || []
      for (let i = 0; i < list.length; i++) {
        if (list[i] && String(list[i].order_id) === key) return list[i]
      }
      return null
    },

    onRefresh() {
      // 刷新时连「最近 N 条」配置一起重取：超管刚改过设置，这里不用退出页面就能生效
      this.setData({ currentPage: 1 }, () => {
        this._syncPageNumbers()
        this.loadLimitAndOrders()
      })
    },

    // 归属人：列表默认脱敏，点一下看完整昵称
    onOwnerTap(e) {
      const owner = e.currentTarget.dataset.owner || ''
      if (!owner) {
        wx.showToast({ title: '该订单未记录归属人', icon: 'none' })
        return
      }
      wx.showModal({ title: '归属人', content: owner, showCancel: false, confirmText: '知道了' })
    },

    // 金额：直接复制「订单号 — ¥金额」（与本地打印工具 Ctrl+C 同格式）。
    // 最近订单行没有逐文件明细，所以不弹价格气泡，也不提供「复制详细价格」。
    onPriceCopy(e) {
      const o = this._findOrder(e.currentTarget.dataset.id)
      if (!o) return
      const amount = '¥' + _money(o.total_price)
      const no = String(o.order_number || '').trim()
      const text = no ? (no + ' — ' + amount) : amount
      wx.setClipboardData({
        data: text,
        success: () => wx.showToast({ title: '已复制价格', icon: 'success' }),
        fail: () => wx.showToast({ title: '复制失败', icon: 'none' }),
      })
    },

    // ==================== #9 异常状态原因气泡 ====================
    onStatusReasonTap(e) {
      const id = e.currentTarget.dataset.id
      if (this.data.showStatusPop && String(this.data.statusPopOrderId) === String(id)) {
        this.setData({ showStatusPop: false })
        return
      }
      const o = this._findOrder(id)
      if (!o) return
      const text = this._statusReasonText(o)
      const win = (typeof wx.getWindowInfo === 'function' ? wx.getWindowInfo() : null) || {}
      const winW = win.windowWidth || 375
      const winH = win.windowHeight || 667
      const popW = Math.min(Math.round(winW * 0.86), winW - 24)
      const q = this.createSelectorQuery()
      q.select('#ro-status-tap-' + id).boundingClientRect()
      q.exec((res) => {
        const rect = res && res[0]
        if (!rect) return
        const left = Math.max(8, Math.min(rect.left + rect.width / 2 - popW / 2, winW - popW - 8))
        const caret = Math.max(18, Math.min(rect.left + rect.width / 2 - left, popW - 18))
        this.setData({
          showStatusPop: true,
          statusPopOrderId: id,
          statusPopText: text,
          statusPopWidth: popW,
          statusPopLeft: left,
          statusPopCaretLeft: caret,
          statusPopBottom: Math.max(8, winH - rect.top + 12),
          statusPopTop: rect.bottom + 12,
          statusPopBelow: false,
        }, () => {
          // 状态贴近屏幕顶部、上方放不下 → 翻到下方，箭头朝上
          const q2 = this.createSelectorQuery()
          q2.select('.ro-pop').boundingClientRect()
          q2.exec((r2) => {
            const b = r2 && r2[0]
            if (b && b.top < 8) this.setData({ statusPopBelow: true })
          })
        })
      })
    },

    onCloseStatusPop() {
      if (this.data.showStatusPop) this.setData({ showStatusPop: false })
    },

    // 气泡本体的 catchtap 需要真实存在的方法才能拦住冒泡（否则点正文会穿透到遮罩把气泡关掉）
    noop() {},

    onCopyStatusReason() {
      const text = this.data.statusPopText || ''
      this.setData({ showStatusPop: false })
      if (!text) return
      wx.setClipboardData({
        data: text,
        success: () => wx.showToast({ title: '已复制原因', icon: 'success' }),
        fail: () => wx.showToast({ title: '复制失败', icon: 'none' }),
      })
    },

    // ==================== 分页 ====================
    onPageChange(e) {
      const page = parseInt(e.currentTarget.dataset.page, 10)
      if (!isFinite(page) || page < 1 || page > this.data.totalPages || page === this.data.currentPage) return
      this.setData({ currentPage: page }, () => {
        this._syncPageNumbers()
        this.loadOrders()           // 翻页给加载态：内容整体换掉，静默刷新会让人以为没反应
        this._scrollToTop()
      })
    },

    onPrevPage() {
      if (this.data.currentPage <= 1) return
      this.setData({ currentPage: this.data.currentPage - 1 }, () => {
        this._syncPageNumbers()
        this.loadOrders()
        this._scrollToTop()
      })
    },

    onNextPage() {
      if (this.data.currentPage >= this.data.totalPages) return
      this.setData({ currentPage: this.data.currentPage + 1 }, () => {
        this._syncPageNumbers()
        this.loadOrders()
        this._scrollToTop()
      })
    },

    onTogglePageSizePicker() {
      this.setData({ showPageSizePicker: !this.data.showPageSizePicker })
    },

    onSelectPageSize(e) {
      const size = parseInt(e.currentTarget.dataset.size, 10)
      if (PER_PAGE_OPTIONS.indexOf(size) < 0) return
      if (size === this.data.perPage) {
        this.setData({ showPageSizePicker: false })
        return
      }
      wx.setStorageSync(PER_PAGE_KEY, size)
      this.setData({ perPage: size, currentPage: 1, showPageSizePicker: false }, () => {
        this._syncPageNumbers()
        this.loadOrders()
        this._scrollToTop()
      })
    },

    _scrollToTop() {
      this.setData({ scrollCmd: { mode: 'top', dur: 240 } })
    },

    // 页码数组：<=7 全列；否则 1 … 当前±1 … 末页（与 user-orders 同规则）
    getPageNumbers() {
      const total = this.data.totalPages
      const current = this.data.currentPage
      if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1)
      const pages = [1]
      if (current > 3) pages.push('...')
      const start = Math.max(2, current - 1)
      const end = Math.min(total - 1, current + 1)
      for (let i = start; i <= end; i++) pages.push(i)
      if (current < total - 2) pages.push('...')
      pages.push(total)
      return pages
    },

    _syncPageNumbers() {
      const pages = this.getPageNumbers()
      const prev = this.data.pageNumbers || []
      if (pages.length !== prev.length || pages.some((p, i) => p !== prev[i])) {
        this.setData({ pageNumbers: pages })
      }
    },
  },
})
