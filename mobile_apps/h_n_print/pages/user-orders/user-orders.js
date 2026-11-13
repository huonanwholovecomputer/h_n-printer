// user-orders.js — 查看指定用户/来源的订单列表
// 用于: 管理管理员点击、历史授权用户点击、本地打印任务
const { CONFIG } = require('../../utils/config')
const { request } = require('../../utils/request')
// 页码范围口径的**唯一实现**（勿在本文件再写一份）——见 utils/pages.js 顶部注释
const { countPagesInRange } = require('../../utils/pages')
/* 「复制价格 / 复制详细价格」文案与算式的**唯一实现**：与「我」页共用 utils/price_copy.js
   （管理入口进来的这个子视图以前没有复制入口 —— 2026-11-09 用户报的现场问题）。
   勿在本文件拼第二份：守卫脚本 other/tests/verify_price_copy_popconfirm.js。 */
const priceCopy = require('../../utils/price_copy')
/* 文件详情卡（预览图 + 参数）的字段装配：「我」页与这里共用一份，模板也只有一份
   （pages/me/me.wxml 的 myfileDetailCard）。2026-11-13 晚用户要求管理入口同样可展开看详情。 */
const fileDetailCard = require('../../utils/file_detail_card')
// 单价（/api/pricing）带进程内缓存：本页与「我」页各自一份，口径相同
const _getPriceRules = priceCopy.createPriceRulesGetter(request, CONFIG)

/* 双面摊纸的兜底张数（契约 §2「单面页每页一张 + 其余两页一张」）：实现在 utils/order_stats，
   但它内部仍只用 utils/pages 的 pageRangeSet/chargeUnits（同一份口径，见该文件顶部说明）。
   这里**延迟 require**：页面加载路径（复制价格/气泡定位等）用不到它，不必进首屏依赖。 */
function _fallbackSheets(f, eff) {
  const { fallbackSheets } = require('../../utils/order_stats')
  return fallbackSheets(f, eff)
}

/* 文件张数/有效页数预处理（后端已返回时直接复用，缺失时前端兜底计算）：
   - effectivePages: 每份有效打印页数（范围过滤后）
   - totalSheets:    文件合计张数（含份数；单面每页 1 张、双面每 2 页 1 张，奇数页最后一张仍占 1 张）
   - pagesLine:      展示用「份 × 页」文本（页数取所选范围有效页数） */
function _enrichFileStat(f) {
  const pc = Number(f.page_count) || 0
  const copies = Number(f.copies) || 1
  const eff = typeof f.effective_pages === 'number' ? f.effective_pages : countPagesInRange(f.page_range, pc)
  // 张数：优先后端 sheets；缺失时同口径兜底（单面每页 1 张，双面按摊纸算）
  const perSheets = typeof f.sheets === 'number' ? f.sheets : _fallbackSheets(f, eff)
  f.effectivePages = eff
  f.totalSheets = typeof f.total_sheets === 'number' ? f.total_sheets : (perSheets * copies)
  f.pagesLine = copies + ' 份 × ' + eff + ' 页'
  return f
}

/* 订单级「合计页数 / 合计份数 / 合计张数」的展示文案（2026-11-07 用户要求）。
   历史口径：Excel 订单一律写「不适用」——那是页数分析还不覆盖表格的年代（后端只给兜底 1 页，
   显示出来是假数）。现在表格也会经本地工具回报真实页数（order_files.page_count →
   后端 effective_pages / total_pages / total_sheets），再一律「不适用」就是把已知信息藏起来。
   改为数据驱动：后端给了数就显示；确实未知（page_count=0 ⇒ total_* 也是 0）才写「不适用」，
   绝不拿兜底的 1 页充数。Excel 与其它格式走同一条规则，不再按扩展名分叉。 */
function _orderTotalsText(o) {
  const pages = Number(o.total_pages) || 0
  const copies = Number(o.total_copies) || Number(o.copies) || 0
  const sheets = Number(o.total_sheets) || Number(o.totalSheetsDisplay) || 0
  return {
    pages: pages > 0 ? pages + ' 页' : '不适用',
    copies: copies > 0 ? copies + ' 份' : '不适用',
    sheets: sheets > 0 ? sheets + ' 张' : '不适用',
  }
}

/* 订单行预处理（列表加载与轮询「结构变化 → 全量刷新」共用）：价格文本 / 文件列统计 /
   isExcel 标记 / 合计页数·份数·张数文案。
   轮询那条分支必须也过这一遍：它在结构变化时整片替换 orders，派生字段（statPages 等）不重算
   就会全丢，卡片上三个数字变成空白。 */
function _normalizeOrderRow(order) {
  order.totalPriceDisplay = (order.total_price || 0).toFixed(2)
  if (order.files) {
    order.files.forEach(f => {
      f.sizeDisplay = f.size ? (f.size / 1024).toFixed(1) + ' KB' : ''
      const name = (f.original_name || f.file_name || '').toLowerCase()
      f.isExcel = name.endsWith('.xls') || name.endsWith('.xlsx')
      _enrichFileStat(f)
    })
    order.isExcel = order.files.length > 0 && order.files.every(f => f.isExcel)
    const totalSheets = typeof order.total_sheets === 'number'
      ? order.total_sheets
      : order.files.reduce((s, f) => s + (f.totalSheets || 0), 0)
    order.totalSheetsDisplay = String(totalSheets)
  } else {
    order.totalSheetsDisplay = '0'
  }
  // 合计页数/份数/张数（数据驱动；Excel 不再一律「不适用」）
  const totalsText = _orderTotalsText(order)
  order.statPages = totalsText.pages
  order.statCopies = totalsText.copies
  order.statSheets = totalsText.sheets
  return order
}

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
    selfOpenid: '',       // 当前登录用户 openid（取消按钮仅本人订单显示）

    orders: [],
    loading: true,
    loadError: '',
    // 取消订单请求进行中（防重复提交）
    isCanceling: false,

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

    // 合计价格复制气泡（与「我」页同一套：fixed 定位在 page-root 外，坐标由 onPriceTap 量出来）
    showPricePop: false,
    pricePopOrderId: '',
    pricePopWidth: 0,
    pricePopLeft: 0,
    pricePopCaretLeft: 0,
    pricePopBottom: 0,
    pricePopTop: 0,
    pricePopBelow: false,

    // 被查看用户的卡片信息（从管理入口跳转后显示）
    userDetail: null,
    showLicenseDetail: false,

    // WXS 引擎：滚动边界 + 程序化滚动命令（对齐“我”页面）
    scrollConfig: {
      minY: 0, maxY: 0, scrollerH: 0, contentH: 0,
      // 内层滚动框（订单详情里的文件列表）：由 utils/scroll.wxs 直驱，双向接力到外层页面
      nestedSel: '.detail-files-scroll', nestedTrackSel: '.detail-files-track',
    },
    scrollCmd: null,

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

    // 文件详情弹窗（2026-11-13 晚：从管理入口「打卡 → 某人的任务」点文件也能看缩略图与参数）
    fileDetailVisible: false,
    fileDetailClosing: false,
    fileDetailLoading: false,
    fileDetail: null,
  },

  lifetimes: {
    attached() {
      const app = getApp()
      this.setData({ isDarkMode: app.globalData.isDarkMode })
      this._initScrollEngine()
      // S2-09（DS-22）：目标用户改由 app.globalData 交接（页面 URL 不再带 openid ——
      // 页面栈/截图/真机调试都会把 openid 带出去）。从页面参数进来的老路径仍兼容。
      const target = (app.globalData && app.globalData.userOrdersTarget) || null
      if (target) app.globalData.userOrdersTarget = null
      const openid = this.data.openid || (target && target.openid) || ''
      const nickname = this.data.nickname
        ? decodeURIComponent(this.data.nickname)
        : ((target && target.nickname) || '')
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
        selfOpenid: wx.getStorageSync('openid') || (app.globalData && app.globalData.openid) || '',
      })

      // 直接传参，不依赖 this.data 是否就绪（对齐 authorized-users 的模式）
      this.loadOrders(openid, source)
      if (openid) {
        this.loadUserDetail(openid)
      }
    },
    detached() {
      this._destroyScrollEngine()
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
      if (this._hasLoaded) {
        this.loadOrders(this.data.viewOpenid, this.data.sourceFilter)
      }
      this._startPolling()
      this._scheduleMeasure()
    },
    hide() {
      this._stopPolling()
      // 返回由系统 navigateBack 动画负责交叉过渡，不再播自定义退出动画
    },
  },

  methods: {
    // 空handler：订单卡内的状态气泡（catchtouchmove）用它把手势终止在气泡层，
    // 不让事件冒泡到外层 .scroller 的 WXS 滚动引擎（气泡里是可滚的文本，不能带着页面一起滚）
    noop() {},

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
          nestedSel: '.detail-files-scroll',
          nestedTrackSel: '.detail-files-track',
        },
      })
    },

    // WXS 分桶回调（每 100px 一次），本页暂不消费
    onWxsScroll() {},

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
          // 轮询成功即自愈：清除上次的加载失败提示（对齐 APP 轮询自恢复）
          if (this.data.loadError) this.setData({ loadError: '' })
          const newOrders = (res.data.orders || [])
          // 格式化价格 + 派生合计字段
          newOrders.forEach(order => {
            _normalizeOrderRow(order)
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
            // 开始打印时间随轮询增量更新：订单开始打印后无需切换 tab 即显示
            if ((newOrders[i].print_started_at || '') !== (oldOrders[i].print_started_at || '')) {
              updates['orders[' + i + '].print_started_at'] = newOrders[i].print_started_at || ''
            }
            // 接单设备随轮询增量更新（订单被设备接收后无需切换 tab 即显示）
            if ((newOrders[i].received_label || '') !== (oldOrders[i].received_label || '')) {
              updates['orders[' + i + '].received_label'] = newOrders[i].received_label || ''
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
      if (source) {
        data.source = source
      }

      request({
        url: CONFIG.BASE_URL + '/api/orders',
        method: 'GET',
        // S2-09（DS-22）：目标用户走 X-Target-Openid 头，不进 URL/访问日志；
        // 服务端仍按目标 openid 做「管理员是否授权过该用户」的校验。
        header: openid
          ? { 'Authorization': 'Bearer ' + token, 'X-Target-Openid': openid }
          : { 'Authorization': 'Bearer ' + token },
        data: data,
        success: (res) => {
          if (res.statusCode === 200 && res.data && res.data.success) {
            const newOrders = (res.data.orders || [])
            // 预处理文件大小显示（WXML 不支持 .toFixed()）
            newOrders.forEach(order => {
              _normalizeOrderRow(order)
            })

            const total = res.data.total || 0
            this.setData({
              orders: newOrders,
              loading: false,
              loadError: '',
              // 换页/刷新后旧气泡指向的订单可能已不在这一页：先收起
              showPricePop: false,
              totalOrders: total,
              totalPages: Math.ceil(total / this.data.perPage),
            }, () => {
              this._syncPageNumbers()
              this._scheduleMeasure(100)
            })
          } else {
            // 页面内状态提示（对齐 Android App）：不清空已加载列表
            this.setData({ loading: false, loadError: res.data && res.data.message ? res.data.message : '加载失败' })
            this._scheduleMeasure()
          }
        },
        fail: () => {
          this.setData({ loading: false, loadError: '网络错误' })
          this._scheduleMeasure()
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
        // S2-09（DS-22）：目标用户走头，不进 URL
        header: { 'Authorization': 'Bearer ' + token, 'X-Target-Openid': openid },
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
    // 由 WXS 引擎在视图层测量区块位置并动画（内容坐标 = 区块可视位置 - 容器可视位置 + 当前滚动量）
    _scrollToOrdersSection() {
      this.setData({ scrollCmd: { mode: 'section', section: '.orders-section', offset: -12, dur: 280 } })
    },

    // ==================== 分页 ====================

    onPageChange(e) {
      const page = parseInt(e.currentTarget.dataset.page, 10)
      if (page < 1 || page > this.data.totalPages || page === this.data.currentPage) return
      this.setData({ currentPage: page, showPricePop: false }, () => {
        this._syncPageNumbers()
        this.loadOrders(this.data.viewOpenid, this.data.sourceFilter)
        this._scrollToOrdersSection()
      })
    },

    onPrevPage() {
      if (this.data.currentPage <= 1) return
      this.setData({ currentPage: this.data.currentPage - 1, showPricePop: false }, () => {
        this._syncPageNumbers()
        this.loadOrders(this.data.viewOpenid, this.data.sourceFilter)
        this._scrollToOrdersSection()
      })
    },

    onNextPage() {
      if (this.data.currentPage >= this.data.totalPages) return
      this.setData({ currentPage: this.data.currentPage + 1, showPricePop: false }, () => {
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
      if (size === this.data.perPage) {
        // 重复选择当前条数：只收起下拉，不重复请求（对齐 APP 防重）
        this.setData({ showPageSizePicker: false })
        return
      }
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

    // ==================== 合计价格复制（气泡 + 两种文案） ====================
    // 文案 / 算式 / 单价兜底全在 utils/price_copy.js；这里只做「量位置 → 弹气泡 → 写剪贴板」。

    // 点击「¥xx.xx」→ 在价格处弹出气泡（再点一次收起）。
    // 气泡在 page-root 外、position:fixed：既逃出 scroll-content 的 translateY 变换层，
    // 也不会被订单卡片的 overflow:hidden / max-height 裁剪。
    onPriceTap(e) {
      const id = e.currentTarget.dataset.id
      if (this.data.showPricePop && String(this.data.pricePopOrderId) === String(id)) {
        this.setData({ showPricePop: false })
        return
      }
      const win = (typeof wx.getWindowInfo === 'function' ? wx.getWindowInfo() : null) || {}
      const winW = win.windowWidth || 375
      const winH = win.windowHeight || 667
      const popW = Math.min(Math.round(winW * 0.74), winW - 24)
      const q = this.createSelectorQuery()
      q.select('#price-tap-' + id).boundingClientRect()
      q.exec((res) => {
        const rect = res && res[0]
        if (!rect) return
        // 水平：气泡右边对齐价格右边，再夹在视口内；箭头指向价格中心
        const left = Math.max(8, Math.min(rect.right - popW, winW - popW - 8))
        const caret = Math.max(18, Math.min(rect.left + rect.width / 2 - left, popW - 18))
        this.setData({
          showPricePop: true,
          pricePopOrderId: id,
          pricePopWidth: popW,
          pricePopLeft: left,
          pricePopCaretLeft: caret,
          // 垂直：气泡底边锚在价格上方 12px（高度未知也可用，向上自然长高）
          pricePopBottom: Math.max(8, winH - rect.top + 12),
          pricePopTop: rect.bottom + 12,
          pricePopBelow: false,
        }, () => {
          // 价格贴近屏幕顶部、上方放不下 → 翻到价格下方，箭头朝上
          const q2 = this.createSelectorQuery()
          q2.select('.price-pop').boundingClientRect()
          q2.exec((r2) => {
            const b = r2 && r2[0]
            if (b && b.top < 8) this.setData({ pricePopBelow: true })
          })
        })
      })
    },

    // 价格气泡是「非模态」的：遮罩 pointer-events:none（不挡滚动），失去焦点即关闭。
    // page-root 的 capture-bind:touchstart 落到这里 —— 点/滑页面任何地方都先收起气泡，
    // 手势本身照常交给 .scroller 的 WXS 滚动引擎（不能用 catchtouchmove，那会锁住页面）。
    onPricePopDismiss() {
      if (this.data.showPricePop) this.setData({ showPricePop: false })
    },

    onClosePricePop() {
      if (this.data.showPricePop) this.setData({ showPricePop: false })
    },

    // 复制价格（简略：`{订单号} — ¥{金额}`，与本地打印工具 Ctrl+C 同格式）
    onCopyOrderPrice(e) {
      const o = this._orderById(e.currentTarget.dataset.id)
      this.setData({ showPricePop: false })
      if (!o) return
      this._copyPriceText(priceCopy.priceLine(o), '已复制价格')
    },

    // 复制详细价格（逐文件 + 附加服务费 + 合计，与本地打印工具 Ctrl+Shift+C 同格式）
    onCopyOrderDetailPrice(e) {
      const o = this._orderById(e.currentTarget.dataset.id)
      this.setData({ showPricePop: false })
      if (!o) return
      _getPriceRules((rules) => {
        this._copyPriceText(
          priceCopy.priceDetailLines(o, rules).join(String.fromCharCode(10)),
          '已复制详细价格')
      })
    },

    _orderById(id) {
      const rows = this.data.orders || []
      for (let i = 0; i < rows.length; i++) {
        if (String(rows[i].id) === String(id)) return rows[i]
      }
      return null
    },

    _copyPriceText(text, toastTitle) {
      wx.setClipboardData({
        data: text,
        success: () => wx.showToast({ title: toastTitle, icon: 'success' }),
        fail: () => wx.showToast({ title: '复制失败', icon: 'none' }),
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
      this.setData({ expandedOrders: expanded, showPricePop: false })
      this._scheduleMeasure()
      // 详情展开/收起有 0.3s max-height 动画：过渡中途先跟一版（缩小白屏窗口），
      // 动画结束后再测最终值（引擎收到变小的 maxY 会平滑滚动到新边界，不跳变）
      setTimeout(() => this._scheduleMeasure(150), 150)
      setTimeout(() => this._scheduleMeasure(300), 320)
    },

    onDetailCancelOrder(e) {
      const orderId = e.currentTarget.dataset.id
      const token = wx.getStorageSync('token')
      if (!token || this.data.isCanceling) return

      wx.showModal({
        title: '确认取消',
        content: '确定要取消这个打印任务吗？',
        confirmText: '取消订单',
        confirmColor: '#FF9500',
        success: (modalRes) => {
          if (!modalRes.confirm) return
          this.setData({ isCanceling: true })
          wx.showLoading({ title: '取消中...', mask: true })
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
            complete: () => this.setData({ isCanceling: false }),
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


    // ==================== 文件详情弹窗（与「我」页同一份模板） ====================
    // 取本页 orders 里某单的第 findex 个文件行（本地打单没有云端 file_id，参数全在这行上）
    _uoFileAt(orderId, findex) {
      const orders = this.data.orders || []
      const idx = Number(findex)
      for (let i = 0; i < orders.length; i++) {
        if (String(orders[i].id) !== String(orderId)) continue
        const files = orders[i].files || []
        if (!isNaN(idx) && idx >= 0 && idx < files.length) return files[idx]
        break
      }
      return null
    },

    // 没有云端副本时：用这单的打印参数拼一张只读面板（不请求任何接口）
    _uoShowLocal(rowFile, reason) {
      this.setData({
        fileDetailVisible: true,
        fileDetailClosing: false,
        fileDetailLoading: false,
        fileDetail: fileDetailCard.localDetail(rowFile, reason),
      })
    },

    onOpenOrderFileDetail(e) {
      const ds = (e && e.currentTarget && e.currentTarget.dataset) || {}
      const fileId = ds.id ? String(ds.id) : ''
      const rowFile = this._uoFileAt(ds.oid, ds.findex)
      // 本地打单（order_files.file_id 为空）：云端没有这份文件，但参数有 —— 不再弹「该文件已清理」
      if (!fileId) {
        if (rowFile) { this._uoShowLocal(rowFile, 'local'); return }
        wx.showToast({ title: '这行没有可查看的文件', icon: 'none' })
        return
      }
      const token = wx.getStorageSync('token')
      if (!token) {
        wx.showToast({ title: '请先登录', icon: 'none' })
        return
      }
      this.setData({
        fileDetailVisible: true,
        fileDetailClosing: false,
        fileDetailLoading: true,
        fileDetail: null,
      })
      request({
        url: CONFIG.BASE_URL + '/api/file_detail/' + encodeURIComponent(fileId),
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          const data = res.data || {}
          if (res.statusCode === 200 && data.success && data.file) {
            const detail = fileDetailCard.decorate(data.file, { baseUrl: CONFIG.BASE_URL, token })
            this.setData({ fileDetail: detail, fileDetailLoading: false })
            this._prepareFilePreview(detail)
            return
          }
          // 403/404：文件已过保留期或被删 —— 这单的参数还查得到，就展示参数并说明原因
          if (rowFile) { this._uoShowLocal(rowFile, 'cleaned'); return }
          this.setData({ fileDetailLoading: false, fileDetailVisible: false })
          wx.showToast({ title: res.statusCode === 403 ? '无权查看该文件' : '该文件已被清理', icon: 'none' })
        },
        fail: () => {
          if (rowFile) { this._uoShowLocal(rowFile, 'cleaned'); return }
          this.setData({ fileDetailLoading: false, fileDetailVisible: false })
          wx.showToast({ title: '加载失败，请重试', icon: 'none' })
        },
      })
    },

    // 页数纠偏：列表预览图只给第 1 页，Office 转 PDF 后页数会变多（后端 meta 才知道真页数），
    // 所以每个文件问一次 /info（带 token 查参数），拿到 pages 后重建 shots
    _prepareFilePreview(item) {
      if (!item || item.local_only || !item.file_id) return
      const fid = String(item.file_id)
      if (item.has_preview === false) return
      const asked = this._fileDetailInfoAsked || (this._fileDetailInfoAsked = {})
      if (asked[fid]) return
      asked[fid] = true
      const token = wx.getStorageSync('token')
      if (!token) return
      request({
        url: CONFIG.BASE_URL + '/api/file_preview/' + encodeURIComponent(fid) +
          '/info?token=' + encodeURIComponent(token),
        method: 'GET',
        timeout: 20000, // 首张要现渲染（Word/Excel 1~2s），12s 默认超时留足余量
        success: (res) => {
          const data = res.data || {}
          if (res.statusCode !== 200 || !data.success) return
          const cur = this.data.fileDetail
          if (!cur || cur.local_only || String(cur.file_id) !== fid) return
          const pages = Math.max(1, Number(data.pages) || 1)
          const built = fileDetailCard.shots(CONFIG.BASE_URL, token, fid, pages,
            cur.previewIndex || 0, cur.previewShots)
          this.setData({
            'fileDetail.previewShots': built.shots,
            'fileDetail.previewIndex': built.index,
            'fileDetail.previewPages': built.shots.length,
            'fileDetail.previewTip': '',
          })
        },
      })
    },

    // 翻页：只给当前页 ±1 页真发请求（12 页上限）
    onMyFilePreviewChange(e) {
      const detail = this.data.fileDetail
      if (!detail || detail.local_only) return
      const index = Number(e && e.detail && e.detail.current) || 0
      const keep = detail.previewShots || []
      const built = fileDetailCard.shots(CONFIG.BASE_URL, wx.getStorageSync('token'),
        detail.file_id, keep.length || 1, index, keep)
      this.setData({
        'fileDetail.previewIndex': built.index,
        'fileDetail.previewShots': built.shots,
      })
    },

    onMyFilePreviewLoad(e) {
      const ds = (e && e.currentTarget && e.currentTarget.dataset) || {}
      const index = Number(ds.index)
      if (isNaN(index)) return
      const detail = this.data.fileDetail
      if (!detail || !detail.previewShots || !detail.previewShots[index]) return
      this.setData({
        ['fileDetail.previewShots[' + index + '].loaded']: true,
        'fileDetail.previewLoading': false,
      })
    },

    onMyFilePreviewError(e) {
      const ds = (e && e.currentTarget && e.currentTarget.dataset) || {}
      const index = Number(ds.index)
      if (isNaN(index)) return
      const detail = this.data.fileDetail
      if (!detail || !detail.previewShots || !detail.previewShots[index]) return
      const patch = {}
      patch['fileDetail.previewShots[' + index + '].failed'] = true
      patch['fileDetail.previewShots[' + index + '].loaded'] = false
      const anyOk = (detail.previewShots || []).some((s, i) => i !== index && s.loaded)
      if (!anyOk) patch['fileDetail.previewLoading'] = false
      this.setData(patch)
    },

    // 全屏：小程序直接交给 wx.previewImage（原生双指缩放 + 左右翻页）
    onOpenMyFilePreview(e) {
      const detail = this.data.fileDetail
      if (!detail || detail.local_only) return
      const ds = (e && e.currentTarget && e.currentTarget.dataset) || {}
      const shots = detail.previewShots || []
      const index = Number(ds.index)
      const base = isNaN(index) ? 0 : index
      const urls = shots.map(s => s.url)
      if (!urls.length) return
      wx.previewImage({ urls, current: urls[base] || urls[0] })
    },

    onCloseFileDetail() {
      if (this._uoFileDetailTimer) clearTimeout(this._uoFileDetailTimer)
      this.setData({ fileDetailClosing: true })
      this._uoFileDetailTimer = setTimeout(() => {
        this._uoFileDetailTimer = null
        this.setData({ fileDetailVisible: false, fileDetailClosing: false, fileDetail: null })
      }, 200)
    },
  },
})
