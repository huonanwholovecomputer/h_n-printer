// me.js
const { CONFIG } = require('../../utils/config')
const { request, createPollScheduler } = require('../../utils/request')
// 页码范围口径的**唯一实现**（勿在本文件再写一份）——见 utils/pages.js 顶部注释
const { countPagesInRange } = require('../../utils/pages')
// 「复制价格 / 复制详细价格」文案口径的**唯一实现**（与 user-orders 子视图共用）——
// 见 utils/price_copy.js 顶部注释；守卫脚本 other/tests/verify_price_copy_popconfirm.js。
const priceCopy = require('../../utils/price_copy')
// 单价（/api/pricing）带进程内缓存：两个入口各自持一份即可，口径相同
const _getPriceRules = priceCopy.createPriceRulesGetter(request, CONFIG)

/* 双面摊纸的兜底张数（契约 §2「单面页每页一张 + 其余两页一张」）：实现在 utils/order_stats，
   但它内部仍只用 utils/pages 的 pageRangeSet/chargeUnits（同一份口径，见该文件顶部说明）。
   这里**延迟 require**：页面加载路径（复制价格/气泡定位等）用不到它，不必进首屏依赖。 */
function _fallbackSheets(f, eff) {
  const { fallbackSheets } = require('../../utils/order_stats')
  return fallbackSheets(f, eff)
}
// 头像裁剪的纯几何（fit/clamp/cropRect/zoomAt）：与 APP 的 www/avatar_crop.js 同一语义
const avatarCrop = require('../../utils/avatar_crop')
/* 全屏预览层（双击放大 / 双指捏合 / 滑页）延迟 require：只在点开大图时才用得到，
   不该进页面首屏依赖；也让只抠本文件函数的验收脚本不必为它造桩。 */
function pvCore() {
  return require('../../utils/preview_viewer')
}
// 「我的文件」预览图最多展示前多少页（与后端 app.py 的 PREVIEW_MAX_PAGES 对齐：
// 后端预热也只生成这么多页，再往后的页没有缓存，点开会现生成、明显慢）
const PREVIEW_MAX_PAGES = 12

function _initIsDark() {
  try {
    const a = getApp()
    if (a && typeof a.globalData.isDarkMode === 'boolean') return a.globalData.isDarkMode
  } catch (e) {}
  return wx.getStorageSync('isDarkMode') || false
}
function _initThemeMode() {
  try {
    const a = getApp()
    if (a && a.globalData.themeMode) return a.globalData.themeMode
  } catch (e) {}
  return wx.getStorageSync('themeMode') || 'auto'
}

/* 文件张数/有效页数预处理（后端已返回 effective_pages/sheets/total_sheets 时直接复用；
   字段缺失（旧后端）时前端兜底计算）：
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
    // 合计张数（后端 total_sheets 优先，缺失时按文件张数求和兜底）
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

/* ===== 异常状态原因（#9）===== */
// 这些是「终态且应能解释原因」的状态：状态文字可点，弹出 Popconfirm 显示原因/订正记录。
// sent（已完成）不算异常；queued/printing 等中间态也不弹（它们没有原因可讲）。
const REASON_STATUSES = ['failed', 'timeout', 'canceled', 'rejected', 'abandoned']
// 后端只保证「有一条异常状态时报 status_reason」；历史单/正常取消可能都没有原因，给通用文案兜底
const REASON_FALLBACK_TEXT = '该订单未提供具体原因'

/* 订单时间戳 → YYYY-MM-DD HH:MM:SS。
   后端 manual_status_at 形如 "2026-11-03 14:12:50"（也可能带 T/Z 的 ISO），
   前端只做展示、不参与比较：认不出格式就原样返回，宁可难看也不吞掉信息。 */
function _fmtStatusTime(v) {
  const s = String(v == null ? '' : v).trim()
  if (!s) return ''
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/)
  if (m) return m[1] + '-' + m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5] + ':' + m[6]
  return s
}

/* ===== 价格复制文案（#5：与本地打印工具 gui.py 的 Ctrl+C / Ctrl+Shift+C 逐字对齐）=====
   单行：`{订单号} — ¥{金额}`（em dash 两侧空格；无订单号只 ¥{金额}）
   详细：订单号 / 14 短横线 / 计费明细 / 14 短横线 / 每文件三行 / 附加服务费 / 14 短横线 / 💰合计
   金额一律取「实收」（orders.total_price、order_files[].total_price）——设备回报页数后
   后端已重算回写，前端不重算金额，只按 calc_cost 的口径**复原算式**给人看。 */
/* 「复制价格 / 复制详细价格」的文案与算式已移到 utils/price_copy.js（与 user-orders 共用），
   本文件不再保留第二份：PRICE_DASH / _money / _chargeUnitsOf / _calcCostFormula 均已删除。
   页面这边只负责气泡定位、深层剪贴板调用与按钮状态。 */

Component({
  data: {
    pv: { visible: false, url: '', urls: [], index: 0, count: 0, scale: 1, x: 0, y: 0, animating: false, failed: false },   // 全屏预览层状态（见 utils/preview_viewer.js）
    nickname: '',
    avatarUrl: '',
    // 头像裁剪（2026-10-27 用户要求）：取景几何全部走 utils/avatar_crop.js 的纯函数；
    // 这里只放渲染需要的值（手势过程中的 _cropTouch 挂在实例上，不进 data）。
    cropVisible: false,
    cropSrc: '',
    cropImgW: 0,
    cropImgH: 0,
    cropViewport: 240,
    cropFit: 1,
    cropScale: 1,
    cropX: 0,
    cropY: 0,
    cropZoomPct: 100,
    cropMaxZoomPct: 400,
    isAdmin: false,
    isSuperAdmin: false,
    userRole: '',
    // 许可密钥详情（临时授权用户显示）
    licenseInfo: null,
    // 许可密钥详情展开（点击卡片右侧徽章）
    showLicenseDetail: false,
    orders: [],
    loading: true,
    loadError: '',
    // 取消订单请求进行中（防重复提交：loading 无 mask 时快速连点会并发发两个取消请求）
    isCanceling: false,
    // 分页
    ordersCurrentPage: 1,
    ordersPerPage: 10,
    ordersTotal: 0,
    ordersTotalPages: 0,
    // 页码数组（预计算进 data，避免 WXML 内调用方法 + wx:key="*this" 在部分基础库下渲染失败）
    ordersPageNumbers: [],
    pageOptions: [10, 20, 50, 100],
    showPageSizePicker: false,
    // S2-08（D-7）：「我的文件」弹窗（查看保留期 + 删除自己的文件）
    showMyFiles: false,
    myFilesClosing: false,
    myFiles: [],
    myFilesLoading: false,
    myFilesPage: 0,
    myFilesTotal: 0,
    // 2026-11-12：当前展开详情的文件 id（同一时间只展开一个；空串 = 全部收起）
    myFilesDetailId: '',
    // 展开/收起动画用：JS 量出的高度写进这个行内 style（如 'height:auto;opacity:1;'）
    myFilesDetailStyle: '',
    // 2026-11-13 用户要求 4：订单卡片「文件列表」点开的文件详情弹窗（独立弹窗，同源模板）
    fileDetailVisible: false,
    fileDetailClosing: false,
    fileDetailLoading: false,
    fileDetail: null,
    pageSizeDropdownLeft: 0,   // 分页下拉 fixed 定位（viewport 坐标）
    pageSizeDropdownTop: 0,
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
    // 管理员：「近 N 条订单」卡片（GET /api/admin/recent_orders）。
    // N 由超管设置（默认 10），页面只显示后端返回的 limit；非管理员不渲染、不发请求；
    // 失败静默保留上次数据（不弹错误遮罩）。
    recentOrders: [],
    recentOrdersLimit: 10,
    recentOrdersLoading: false,
    pricePopRecent: false,   // 气泡锚在「近 N 条订单」行：只有「复制价格」（无逐文件明细）
    // 异常状态原因气泡（#9）：与价格气泡同一套定位思路 —— 挂在 page-root 外 + position:fixed，
    // 否则会被订单卡片 .order-card-detail 的 overflow:hidden / max-height 裁掉，
    // 也会被 .scroll-content 的 translateY 变换层带偏。
    showStatusPop: false,
    statusPopOrderId: '',
    statusPopText: '',
    statusPopWidth: 0,
    statusPopLeft: 0,
    statusPopCaretLeft: 0,
    statusPopBottom: 0,
    statusPopTop: 0,
    statusPopBelow: false,
    // 超管专属：N 编辑器（POST /api/admin/recent_orders_limit，只有 super_admin 能写）。
    // 入口只对 isSuperAdmin 渲染 —— 后端 compute_role() 把超管折叠成 admin，
    // 判 userRole === 'super_admin' 永远不命中。
    recentLimitEditor: false,
    recentLimitDraft: '',
    recentLimitSaving: false,
    // 管理员：许可密钥 & 用户列表
    licenseMinutes: 1,
    minusDisabled: true,   // 独立字段避免 setData licenseMinutes 时模板重渲染打断 hover
    plusDisabled: false,
    generating: false,
    // 多密钥支持：所有活跃密钥以数组存储
    activeKeys: [],
    // 已临时授权的普通用户（卡片 + 左滑移除）
    tempUsers: [],
    tempUsersLoading: false,
    tempUserSwipeX: {},            // { openid: px }
    tempUserSwipeTransition: {},   // { openid: bool }
    tempUserDeleteOpacity: {},     // { openid: 0~1 }
    redeemKey: '',
    redeeming: false,
    // 账号绑定入口（管理功能在独立页面 pages/bind/bind，这里只显示已绑定设备数）
    bindDeviceCount: 0,
    // 临时授权倒计时
    tempUntil: '',
    tempCountdownText: '',

    // 密钥类型选择（表单用）
    keyType: 'temp',
    // 多密钥倒计时定时器
    _keyCountdownTimer: null,
    // 管理员：许可密钥轮询定时器（内部状态，非响应式）
    _keyPollTimer: null,
    showScrollTop: false,   // 回顶按钮显隐（WXS 分桶回调驱动）
    scrollConfig: {
      minY: 0, maxY: 0, scrollerH: 0, contentH: 0,
      // 内层滚动框（订单详情里的文件列表）：由 utils/scroll.wxs 直驱 translateY，
      // 到边当帧把余量交给外层页面、外层到顶再交还（双向接力）。选择器只在首次下发。
      nestedSel: '.detail-files-scroll', nestedTrackSel: '.detail-files-track',
      closePicker: true,
    },
    scrollCmd: null,        // WXS 程序化滚动命令（回顶 / 滚到订单区）
    // 任务卡展开状态: { [orderId]: true }
    expandedOrders: {},
    // 合计价格复制 Popconfirm（气泡在 page-root 外 fixed 定位，坐标为视口 px）：
    //   showPricePop / pricePopOrderId —— 气泡显隐与所属订单
    //   pricePopLeft / pricePopWidth / pricePopCaretLeft —— 水平位置（左右都按 px 夹在视口内）
    //   pricePopBottom（气泡在价格上方时的 bottom 距离）/ pricePopTop（空间不足翻到下方时的 top）
    //   pricePopBelow —— 上方放不下时翻到价格下方，箭头改为朝上
    showPricePop: false,
    pricePopOrderId: '',
    pricePopLeft: 0,
    pricePopWidth: 0,
    pricePopCaretLeft: 0,
    pricePopBottom: 0,
    pricePopTop: 0,
    pricePopBelow: false,
    // 管理员：服务器存储统计
    storageStats: null,
    retentionDays: 7,
    retentionHours: 0,
    savingRetention: false,
    deletingAllFiles: false,
    // 管理员：防滥用（DDoS 防护）阈值
    securityConfig: null,
    securityItems: [],
    savingSecurity: false,
    securityExpanded: false,
    // 超级管理员：管理员列表
    admins: [],
    adminsLoading: false,
    adminSwipeX: {},      // { openid: px }
    adminSwipeTransition: {},  // { openid: bool }
    adminDeleteOpacity: {},  // { openid: 0~1 }
    pageExit: '',             // 退出动画: page-exit-left / page-exit-right
    pageSlide: 'page-init',   // 入场动画: page-enter-right（初始隐藏防闪烁）
    isDarkMode: _initIsDark(),
    themeMode: _initThemeMode(),
    // 通用确认弹窗（跟随 app 主题，替代 wx.showModal）
    showConfirmModal: false,
    showNicknamePicker: false,     // 昵称选择弹窗
    showNicknameInput: false,      // 自定义昵称输入弹窗
    customNickname: '',            // 自定义昵称输入缓冲
    focusWechatNickname: false,    // 聚焦隐藏的 type="nickname" 输入框
    modalClosing: false,           // 弹窗关闭动画进行中
    confirmModalTitle: '',
    confirmModalContent: '',
    // 主题切换按钮位置（兼容所有设备，避免 env(safe-area-inset-top) 在某些设备返回 0）
    navBarBtnTop: 72,  // 默认 20+44+8，attached 中根据实际 statusBarHeight 校正
    confirmModalConfirmText: '',
    confirmModalConfirmColor: '#ff4d4f',
    _confirmCallback: null,
    _revokeTargetIdx: null,
  },
  lifetimes: {
    attached() {
      // 注册到全局页面实例池，供主题切换时同步缓存页
      try { const r = getApp().globalData._pageRegistry; if (r && !r.includes(this)) r.push(this) } catch(e) {}
      // 首帧前直接覆写数据对象，绕过 setData 异步延迟
      this.data.isDarkMode = getApp().globalData.isDarkMode
      // 从 app.globalData 同步主题：先设原生背景色防止闪烁
      const app = getApp()
      const bg = app.globalData.isDarkMode ? '#1C1C1E' : '#F2F2F7'
      wx.setBackgroundColor({ backgroundColor: bg, backgroundColorTop: bg, backgroundColorBottom: bg })
      // 计算主题切换按钮位置：放在导航栏下方（导航栏 = statusBar + 内容区域）
      // iOS 导航栏内容高 44px，Android/devtools 高 48px（与 navigation-bar 组件逻辑一致）
      const winInfo = wx.getWindowInfo()
      const devInfo = wx.getDeviceInfo()
      const sbh = winInfo.statusBarHeight || 20
      const isAndroid = devInfo.platform === 'android' || devInfo.platform === 'devtools'
      const navContentH = isAndroid ? 48 : 44
      const btnTop = sbh + navContentH + 8  // +8px 间距
      this.data.navBarBtnTop = btnTop
      this.setData({
        isDarkMode: app.globalData.isDarkMode,
        themeMode: app.globalData.themeMode,
        navBarBtnTop: btnTop,
      })
      this._initScrollEngine()
      this._measureKeyTypeWidth()   // 预缓存密钥类型滑块宽度，首次拖动即可跟手
      setTimeout(() => this._measureKeyTypeWidth(), 300)
      this.loadProfile()
      this.loadUserRole()
      this.loadOrders()
    },
    detached() {
      try { const r = getApp().globalData._pageRegistry; if (r) { const i = r.indexOf(this); if (i >= 0) r.splice(i, 1) } } catch(e) {}
      this._destroyScrollEngine()
    },
  },
  pageLifetimes: {
    show() {
      // 【必须在任何操作前】覆写数据对象，因为首帧渲染可能早于 show() 生命周期
      this.data.isDarkMode = getApp().globalData.isDarkMode
      // 防止 tab 切换时闪白/闪黑：先同步原生背景色
      const app = getApp()
      const bg = app.globalData.isDarkMode ? '#1C1C1E' : '#F2F2F7'
      wx.setBackgroundColor({ backgroundColor: bg, backgroundColorTop: bg, backgroundColorBottom: bg })
      // 回到本页先补测一次滚动边界（切 tab 期间订单/密钥/存储统计可能已经变了高度）
      this._scheduleMeasure(60)
      // 切回本页时收起「合计价格复制」气泡（切 tab 期间它不该跟着回来）
      if (this.data.showPricePop) this.setData({ showPricePop: false })
      const resumedFromBg = app.globalData._resumedFromBackground
      if (resumedFromBg) {
        app.globalData._resumedFromBackground = false
        this.setData({
          pageExit: '', pageSlide: 'page-fade-in',
          isDarkMode: app.globalData.isDarkMode,
          themeMode: app.globalData.themeMode,
        })
        this.loadUserRole()
        this.loadOrders()
        this.loadProfile()
        const cachedRole = wx.getStorageSync('userRole')
        if (cachedRole === 'admin') {
          this.loadTempUsers()
          this.loadActiveKey(false)
          this.loadStorageStats()
          this.loadSecurityConfig()
          this.loadRecentOrders()   // 「近 N 条订单」：进入页面拉一次（15s 内去重）
          if (this.data.isSuperAdmin) {
            this.loadAdmins()
          }
        }
        try {
          const tabBar = this.getTabBar && this.getTabBar()
          if (tabBar) {
            tabBar.setData({ selected: 1, 'list[0].active': false, 'list[1].active': true })
          }
        } catch (e) {}
        this._scheduleMeasure()
        setTimeout(() => this._scheduleMeasure(300), 300)
        this._startOrderPolling()
        return
      }
      // 两步入场动画：① 强制隐藏 + 无条件同步主题，② 稍后播入场
      // page-init 确保 isDarkMode 在首帧渲染前已提交，避免使用缓存页面的过期主题值
      this.setData({
        pageExit: '',
        pageSlide: 'page-init',
        isDarkMode: app.globalData.isDarkMode,
        themeMode: app.globalData.themeMode,
      })
      setTimeout(() => {
        const tabFrom = wx.getStorageSync('_tabFrom')
        const returnFromSub = wx.getStorageSync('_meReturnFromSub')
        wx.removeStorageSync('_meReturnFromSub')
        if (returnFromSub) this.loadBindDeviceCount()  // 从绑定管理页返回时刷新入口副标题
        const isFirstLaunch = (tabFrom == null || tabFrom === '')
        let animationClass = ''
        if (returnFromSub) {
          // 系统 navigateBack 动画已负责位移（与子页面退出交叉），这里只做纯淡入，避免双重位移
          animationClass = 'page-fade-in'
        } else if (isFirstLaunch) {
          animationClass = 'page-fade-in'
        } else if (tabFrom === 0) {
          animationClass = 'page-enter-right'
        } else {
          animationClass = 'page-fade-in'
        }
        this.setData({ pageSlide: animationClass })
      }, 80)  // >2帧，让原生组件（page-meta/navigation-bar）有足够时间完成桥接更新

      // 数据新鲜度守卫：60 秒内切回本页不重复全量拉取（切 tab 反复拉取会触发 Nginx hn_api 限流）。
      // 入口动画、tab 同步、轮询照常；个别操作（提交/兑换/改配置）后对应处理器会主动刷新。
      const _now = Date.now()
      // 个人资料（头像/昵称）每次 show 都刷新：APP 端绑定微信账号后可能在 APP 里修改头像/昵称，
      // 小程序需及时同步（/api/profile 为轻量单查，不受全量守卫限制）
      this.loadProfile()
      if (!this._lastDataLoad || (_now - this._lastDataLoad) > 60000) {
        this._lastDataLoad = _now
        this.loadUserRole()
        this.loadOrders()
        this.loadBindDeviceCount()
        const cachedRole = wx.getStorageSync('userRole')
        if (cachedRole === 'admin') {
          this.loadTempUsers()
          this.loadActiveKey()
          this.loadStorageStats()
          this.loadSecurityConfig()
          this.loadRecentOrders()   // 「近 N 条订单」：进入页面拉一次（15s 内去重）
          if (this.data.isSuperAdmin) {
            this.loadAdmins()
          }
        }
      }
      try {
        const tabBar = this.getTabBar && this.getTabBar()
        if (tabBar) {
          tabBar.setData({ selected: 1, 'list[0].active': false, 'list[1].active': true })
        }
      } catch (e) {}
      this._scheduleMeasure()
      setTimeout(() => this._scheduleMeasure(300), 300)
      this._startOrderPolling()
      // 恢复密钥轮询：hide() 已 _stopKeyPolling()，而 60s 数据守卫可能跳过 loadUserRole（轮询只在其中启动），
      // 不在这里恢复会导致切 tab 回来后密钥状态（已使用/已删除）不再与另一端同步
      if (wx.getStorageSync('userRole') === 'admin') this._startKeyPolling()
      // 主题按钮淡入（从右向左滑入，与页面入场同步；类交替变化触发动画重播）
      this.setData({ themeBtnAnim: 'theme-show' })
    },
    hide() {
      this._stopOrderPolling()
      this._stopKeyPolling()        // 管理员密钥轮询随页面隐藏停止
      this._stopCountdown()         // 密钥倒计时随页面隐藏停止
      this._stopTempCountdown()     // 临时授权倒计时随页面隐藏停止
      // 重置入场动画类为隐藏态，确保下次 show 时框架首帧不可见，避免闪烁
      // pageExit 控制退出动画，pageSlide 控制入场/静止态，互不冲突
      this.setData({ pageSlide: 'page-init', pageExit: '' })
    },
  },
  methods: {
    // 由 tabBar 调用：退出动画 → 回调中切换页面
    // 弹窗打开时：播放关闭动画 → 关闭弹窗 + 切换 tab（一气呵成）
    animateExit(direction) {
      if (this.data.showConfirmModal) {
        this.setData({ modalClosing: true })
        wx.setStorageSync('_tabFrom', 1)
        wx.setStorageSync('_tabTo', 0)
        const app = getApp()
        const bg = app.globalData.isDarkMode ? '#1C1C1E' : '#F2F2F7'
        wx.setBackgroundColor({ backgroundColor: bg, backgroundColorTop: bg, backgroundColorBottom: bg })
        setTimeout(() => {
          this.setData({ showConfirmModal: false, modalClosing: false, _confirmCallback: null })
          wx.switchTab({ url: '/pages/index/index' })
        }, 200)
        return false
      }
      this.setData({ pageExit: direction === 'left' ? 'page-exit-left' : 'page-exit-right' })
      // 主题按钮向右滑出淡出（与页面退出动画同步）
      this.setData({ themeBtnAnim: 'theme-hide' })
      return true
    },

    // 带系统 push 动画的子页面导航（防连点锁）：
    // 不再播自定义退出动画，直接 navigateTo，系统动画天然让新页滑入与旧页离开重叠（交叉过渡）
    _navigateWithAnimation(url) {
      if (this._navigating) return
      this._navigating = true
      wx.setStorageSync('_navForward', '1')
      wx.setStorageSync('_meReturnFromSub', '1')
      wx.navigateTo({
        url,
        complete: () => { this._navigating = false }
      })
    },

    // 订单状态轮询（静默增量更新，不触发全量渲染）；基线 15s，避免高频请求触发 Nginx 限流。
    // S1-09：**自调度** —— 上一次 `_pollOrdersSilent` 的请求落定后才排下一次（setTimeout 链），
    // 失败按 1.5× 指数退避（上限 5 分钟），成功即恢复 15s。旧写法 setInterval(15s) 在弱网
    // （请求 12s 超时）时会叠着跑，越积越多。
    _startOrderPolling() {
      this._stopOrderPolling()
      // 桩环境/老版本 utils/request 没导出调度器时安静跳过（真机永远有；见 utils/request.js）
      if (typeof createPollScheduler !== 'function') return
      if (!this._orderPoll) {
        this._orderPoll = createPollScheduler((done) => this._pollOrdersSilent(done), 15000)
      }
      this._orderPoll.start()
    },
    _pollOrdersSilent(done) {
      const token = wx.getStorageSync('token')
      if (!token) {
        if (done) done(true)   // 未登录 = 不发请求，不算失败（不退避）
        return
      }
      request({
        url: CONFIG.BASE_URL + '/api/orders',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        data: { page: this.data.ordersCurrentPage, per_page: this.data.ordersPerPage },
        success: (res) => {
          if (res.statusCode !== 200 || !res.data || !res.data.success) {
            if (done) done(false)
            return
          }
          const newOrders = res.data.orders || []
          const oldOrders = this.data.orders || []
          // 逐条对比，仅更新变化的 status 字段
          const updates = {}
          let changed = false
          const maxLen = Math.max(newOrders.length, oldOrders.length)
          for (let i = 0; i < maxLen; i++) {
            const n = newOrders[i], o = oldOrders[i]
            if (!n || !o) { changed = true; break }  // 数量变了，全量刷新
            if (n.id !== o.id) { changed = true; break }
            if (n.status !== o.status) {
              updates['orders[' + i + '].status'] = n.status
              // 状态变了：#9 的可点标记与原因文本一起跟着变（否则点了还是旧文案）
              updates['orders[' + i + '].statusReasonTappable'] = REASON_STATUSES.indexOf(n.status) >= 0
              updates['orders[' + i + '].statusReasonText'] = this._statusReasonText(n)
            }
            // 子文件状态同步（D5 第 6 条）：
            //  ① **按 file_id 配对**，绝不按下标 —— 子文件集合变化（增删/顺序变化）时按下标
            //     会把 B 行的状态写到 A 行（行内状态串台）；集合本身变了就全量刷新。
            //  ② 不再依赖父订单状态是否变化 —— 聚合口径（failed > printing > queued > sent）
            //     下"某个文件变了、父状态没变"是可能的（如 已sent + queued ⇒ 父仍 queued），
            //     旧写法会让那一行的状态一直不更新。
            if (n.files && o.files) {
              const newIds = n.files
                .map(f => (f && f.file_id != null) ? String(f.file_id) : '').join(',')
              const oldIds = o.files
                .map(f => (f && f.file_id != null) ? String(f.file_id) : '').join(',')
              if (newIds !== oldIds) {
                changed = true        // 子文件集合变了 → 全量刷新，别让旧行留着
                break
              }
              const oldById = {}
              o.files.forEach(f => { if (f && f.file_id != null) oldById[f.file_id] = f })
              n.files.forEach((nf, j) => {
                const prev = (nf && nf.file_id != null) ? oldById[nf.file_id] : null
                if (prev && nf.status !== prev.status) {
                  updates['orders[' + i + '].files[' + j + '].status'] = nf.status
                }
              })
            }
            // 开始打印时间随轮询增量更新：订单开始打印后无需切换 tab 即显示
            if ((n.print_started_at || '') !== (o.print_started_at || '')) {
              updates['orders[' + i + '].print_started_at'] = n.print_started_at || ''
            }
            // 接单设备随轮询增量更新（订单被设备接收后无需切换 tab 即显示）
            if ((n.received_label || '') !== (o.received_label || '')) {
              updates['orders[' + i + '].received_label'] = n.received_label || ''
            }
          }
          if (changed) {
            // 结构变化（增删订单），做全量刷新但保留展开状态
            newOrders.forEach(order => {
              _normalizeOrderRow(order)
              order.statusReasonTappable = REASON_STATUSES.indexOf(order.status) >= 0
              order.statusReasonText = this._statusReasonText(order)
            })
            this.setData({ orders: newOrders, expandedOrders: this.data.expandedOrders, showPricePop: false })
          } else if (Object.keys(updates).length > 0) {
            this.setData(updates)
          }
          if (done) done(true)   // S1-09：本轮成功 → 轮询恢复 15s 基线
        },
        fail: () => {
          if (done) done(false)  // S1-09：本轮失败 → 退避（1.5× / 上限 5 分钟）
        }
      })
    },
    _stopOrderPolling() {
      if (this._orderPoll) {
        this._orderPoll.stop()
      }
      if (this._orderPollTimer) {
        clearInterval(this._orderPollTimer)
        this._orderPollTimer = null
      }
    },
    // ==================== WXS 滚动引擎（视图层直驱，0 setData） ====================
    // 与首页共用 utils/scroll.wxs；本页只负责测量边界 + 低频推送 scrollConfig，
    // 以及回顶/滚到订单区的 scrollCmd 指令。

    _initScrollEngine() {
      this._scrollerH = 0
      this._contentH = 0
      this._maxY = 0

      this._measureTimer = null  // 去抖测量句柄

      // 底部额外滚动留白（内容与 tabBar 顶边之间的小间隙）
      this._bottomPad = 20
      // 悬浮 tabBar 遮挡高度：bottom 12rpx + 高度 110rpx + 底部安全区（与 index 页一致）
      const _wi = wx.getWindowInfo()
      const _safeBottom = _wi && _wi.safeArea ? Math.max(0, _wi.windowHeight - _wi.safeArea.bottom) : 0
      this._tabOverlayPx = Math.round((12 + 110) * ((_wi.windowWidth || 375) / 750)) + _safeBottom

      // 许可密钥倒计时 / 左滑 运行时状态
      this._keyCountdownTimer = null
      this._keySwipeStartX = 0
      this._keySwipeStartY = 0
      this._keySwipeLastX = 0
      this._keySwipeStartCardX = 0  // 触摸开始时卡片的初始偏移
      this._keySwipeHorizontal = false   // 本次触摸是否已锁定为水平
      this._swipeHorizontal = false      // 卡片左滑中（WXS 引擎靠自身方向锁定让位，此标记保留给卡片自身）
      // 删除按钮宽度：140rpx → px（按实际屏幕宽度换算，取代硬编码）
      const { windowWidth } = wx.getWindowInfo()
      const rpxRatio = (windowWidth || 375) / 750
      this._deleteWidthPx = Math.round(140 * rpxRatio)      // 密钥作废按钮
      this._adminDeleteWidthPx = Math.round(140 * rpxRatio)  // 管理员移除按钮

      // 初次测量（多次延迟以应对 swiper 布局稳定）
      this._scheduleMeasure()
      setTimeout(() => this._scheduleMeasure(), 400)
      setTimeout(() => this._scheduleMeasure(), 800)
    },

    _destroyScrollEngine() {
      if (this._scrollAnimTimer) {
        clearTimeout(this._scrollAnimTimer)
        this._scrollAnimTimer = null
      }
      if (this._measureTimer) {
        clearTimeout(this._measureTimer)
        this._measureTimer = null
      }
      if (this._keyCountdownTimer) {
        clearInterval(this._keyCountdownTimer)
        this._keyCountdownTimer = null
      }
      this._stopTempCountdown()
      this._stopKeyPolling()
    },

    // 去抖测量：动态内容变化时合并多次刷新请求
    // delay 可选，默认 100ms
    _scheduleMeasure(delay) {
      if (this._measureTimer) clearTimeout(this._measureTimer)
      this._measureTimer = setTimeout(() => {
        this._measureTimer = null
        this._measure()
      }, delay || 100)
    },

    // 推送滚动边界给 WXS 引擎（change:prop 观察器，低频同步，不逐帧走 setData）
    _pushScrollConfig() {
      // rev 自增：其余字段相同时 change:prop 不触发，而失同步的内联 transform
      // 要靠 onConfig 里的重绘收敛（与首页同口径）。
      this._scrollRev = (this._scrollRev || 0) + 1
      this.setData({
        scrollConfig: {
          rev: this._scrollRev,
          minY: 0,
          maxY: Math.round(this._maxY || 0),
          scrollerH: this._scrollerH || 0,
          contentH: this._contentH || 0,
          // 底部留白（px）：WXS 用它 + 真实内容高/视口高自己重算 maxY（与首页同口径）
          // （|| 0：_pushScrollConfig 若早于 _initScrollEngine 被调用，别把 NaN 推下去）
          padPx: (this._bottomPad || 0) + (this._tabOverlayPx || 0),
          // 内层滚动框选择器（每次也带上：onConfig 只在首帧见过它一次，丢了就退化成不可滚）
          nestedSel: '.detail-files-scroll',
          nestedTrackSel: '.detail-files-track',
          closePicker: true,   // 触摸开始时通知逻辑层收起分页下拉
        },
      })
    },

    // WXS 分桶回调（每 100px 一次）：控制回顶按钮显隐
    onWxsScroll({ y }) {
      const show = y > 200
      if (show !== this.data.showScrollTop) this.setData({ showScrollTop: show })
    },

    // WXS 触摸开始回调：收起 fixed 定位的分页下拉，避免滚动时错位
    onWxsTouchStart() {
      if (this.data.showPageSizePicker) this.setData({ showPageSizePicker: false })
    },

    _measure() {
      const q = this.createSelectorQuery()
      q.select('.scroller').boundingClientRect()
      q.select('.scroll-content').boundingClientRect()
      q.exec((res) => {
        if (!res || !res[0] || !res[1]) return
        const vp = res[0].height || 0
        const ch = res[1].height || 0
        // 隐藏页/首帧会量到 0：照收会把 maxY 写成一整屏的空白（与首页同一道闸门）
        if (!(vp > 100) || !(ch > 0)) return
        this._scrollerH = vp
        this._contentH = ch
        this._maxY = Math.max(0, ch - vp + this._bottomPad + this._tabOverlayPx)
        this._pushScrollConfig()
      })
    },

    noop() {},

    /* ================= 头像裁剪（2026-10-27 用户要求） =================
     * 选图/取微信头像之后**先裁剪**再上传：拖动选位置、双指或滑杆缩放，圆形取景框，
     * 确认时用 canvas 2d 按 cropRect 的源矩形导出 512×512 的 JPEG，再走原来的
     * /api/profile 上传（服务端本来就会再压到 200px）。
     * 几何全部在 utils/avatar_crop.js（纯函数、有验收）；这里只做手势与画布。
     * 任何一步失败都**退回上传原图**（宁可头像不方，也不让用户卡在弹层里）。
     */
    openAvatarCrop(src) {
      const that = this
      if (!src) return
      wx.getImageInfo({
        src,
        success(info) {
          const vp = 240        // 裁剪框边长（px），与 me.wxss 的 .crop-box 一致
          const w = info.width || info.orientation === 'right' ? info.height : info.width
          const h = info.orientation === 'right' ? info.width : info.height
          const st = avatarCrop.fitState(vp, { width: w, height: h })
          that.setData({
            cropVisible: true,
            cropSrc: src,
            cropImgW: w,
            cropImgH: h,
            cropViewport: vp,
            cropFit: st.scale,
            cropMaxZoomPct: Math.round(avatarCrop.MAX_ZOOM * 100),
            cropZoomPct: 100,
            cropScale: st.scale,
            cropX: st.x,
            cropY: st.y
          })
        },
        fail(err) {
          console.error('[openAvatarCrop] 读取图片信息失败，退回直接上传:', err)
          that.uploadAvatar(src)
        }
      })
    },

    closeAvatarCrop() {
      this._cropTouch = null
      this.setData({ cropVisible: false, cropSrc: '' })
    },

    onCropCancel() {
      this.closeAvatarCrop()
    },

    _cropImage() {
      return { width: this.data.cropImgW, height: this.data.cropImgH }
    },

    _applyCropState(st) {
      const fit = this.data.cropFit || 1
      const pct = Math.round((st.scale / fit) * 100)
      this.setData({
        cropScale: st.scale,
        cropX: st.x,
        cropY: st.y,
        cropZoomPct: Math.max(100, Math.min(this.data.cropMaxZoomPct || 400, pct))
      })
    },

    onCropTouchStart(e) {
      const ts = e.touches || []
      if (!ts.length) return
      this._cropTouch = {
        x: this.data.cropX,
        y: this.data.cropY,
        scale: this.data.cropScale,
        clientX: ts[0].clientX,
        clientY: ts[0].clientY,
        distance: ts.length > 1
          ? Math.sqrt(Math.pow(ts[0].clientX - ts[1].clientX, 2) + Math.pow(ts[0].clientY - ts[1].clientY, 2))
          : 0
      }
    },

    onCropTouchMove(e) {
      const base = this._cropTouch
      const ts = e.touches || []
      if (!base || !ts.length) return
      const img = this._cropImage()
      const vp = this.data.cropViewport
      if (ts.length > 1 && base.distance > 0) {
        const d = Math.sqrt(Math.pow(ts[0].clientX - ts[1].clientX, 2) + Math.pow(ts[0].clientY - ts[1].clientY, 2))
        this._applyCropState(avatarCrop.zoomAt(
          { scale: base.scale, x: base.x, y: base.y }, vp, img, d / base.distance))
        return
      }
      this._applyCropState(avatarCrop.clamp({
        scale: base.scale,
        x: base.x + (ts[0].clientX - base.clientX),
        y: base.y + (ts[0].clientY - base.clientY)
      }, vp, img))
    },

    onCropTouchEnd() {
      this._cropTouch = null
    },

    onCropZoom(e) {
      const img = this._cropImage()
      const vp = this.data.cropViewport
      const fit = this.data.cropFit || 1
      const cur = this.data.cropScale || fit
      const want = fit * ((Number(e.detail && e.detail.value) || 100) / 100)
      const factor = want > 0 ? want / cur : 1
      this._applyCropState(avatarCrop.zoomAt(
        { scale: cur, x: this.data.cropX, y: this.data.cropY }, vp, img, factor))
    },

    onCropConfirm() {
      const that = this
      const src = this.data.cropSrc
      const img = this._cropImage()
      const vp = this.data.cropViewport
      const rect = avatarCrop.cropRect(
        { scale: this.data.cropScale, x: this.data.cropX, y: this.data.cropY }, vp, img)
      // 兜底一律退回原图上传：裁剪是增强，不能因为它失败就让人换不了头像
      const done = (path) => {
        that.closeAvatarCrop()
        const p = path || src
        if (p) that.uploadAvatar(p)
      }
      try {
        wx.createSelectorQuery().in(this).select('#cropCanvas')
          .fields({ node: true, size: true })
          .exec((res) => {
            const canvas = res && res[0] && res[0].node
            if (!canvas) { done(src); return }
            const size = avatarCrop.OUTPUT_SIZE
            canvas.width = size
            canvas.height = size
            const ctx = canvas.getContext('2d')
            const image = canvas.createImage()
            image.onload = () => {
              ctx.fillStyle = '#FFFFFF'
              ctx.fillRect(0, 0, size, size)
              ctx.drawImage(image, rect.sx, rect.sy, rect.sw, rect.sh, 0, 0, size, size)
              wx.canvasToTempFilePath({
                canvas,
                x: 0, y: 0, width: size, height: size,
                destWidth: size, destHeight: size,
                fileType: 'jpg', quality: 0.92,
                success(out) { done(out.tempFilePath) },
                fail(err) {
                  console.error('[onCropConfirm] 导出裁剪图失败，退回原图:', err)
                  done(src)
                }
              })
            }
            image.onerror = (err) => {
              console.error('[onCropConfirm] 载入图片失败，退回原图:', err)
              done(src)
            }
            image.src = src
          })
      } catch (err) {
        console.error('[onCropConfirm] 裁剪异常，退回原图:', err)
        done(src)
      }
    },

    onScrollToTop() {
      // 交给 WXS 引擎做 easeOutCubic 平滑滚动（视图层 rAF）
      this.setData({ scrollCmd: { mode: 'to', y: 0, dur: 300 } })
    },

    // ==================== 用户资料 ====================

    loadProfile() {
      // 先从缓存加载，避免每次切换从头下载大图
      const cachedAvatar = wx.getStorageSync('avatarUrl')
      if (cachedAvatar) {
        this.setData({ avatarUrl: cachedAvatar })
      }

      const token = wx.getStorageSync('token')
      if (!token) {
        console.warn('[loadProfile] token 不存在，跳过')
        return
      }

      request({
        url: CONFIG.BASE_URL + '/api/profile',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          if (res.statusCode === 200 && res.data.success) {
            const nickname = res.data.nickname || ''
            const avatarUrl = res.data.avatar_url || ''
            this.setData({ nickname, avatarUrl })
            // 缓存到本地，头像加载失败时可用
            if (nickname) wx.setStorageSync('nickname', nickname)
            if (avatarUrl) wx.setStorageSync('avatarUrl', avatarUrl)
            this._scheduleMeasure()
          } else if (res.statusCode === 401) {
            console.warn('[loadProfile] token 已过期')
          } else {
            console.error('[loadProfile] 服务器返回异常:', res.statusCode, res.data)
          }
        },
        fail: (err) => {
          console.error('[loadProfile] 网络请求失败:', err)
        }
      })
    },

    onChooseAvatar() {
      const that = this
      wx.showActionSheet({
        itemList: ['从相册选择', '使用微信头像'],
        success(res) {
          if (res.tapIndex === 0) {
            that.chooseFromAlbum()
          } else if (res.tapIndex === 1) {
            that.chooseWechatAvatar()
          }
        },
        fail(err) {
          console.error('[onChooseAvatar] 操作取消:', err)
        }
      })
    },

    // 从相册选择头像
    chooseFromAlbum() {
      const that = this
      wx.chooseImage({
        count: 1,
        sizeType: ['compressed'],
        sourceType: ['album', 'camera'],
        success(res) {
          const avatarUrl = res.tempFilePaths[0]
          if (!avatarUrl) return
          // 2026-10-27 用户要求：选完先裁剪（拖动/缩放选区域），确认后才上传。
          // 不再先把未裁剪的原图塞进 avatarUrl —— 否则上传成功前会闪一张方图变圆图。
          that.openAvatarCrop(avatarUrl)
        },
        fail(err) {
          console.error('[chooseFromAlbum] 选择图片失败:', err)
        }
      })
    },

    // 使用微信头像
    chooseWechatAvatar() {
      const that = this
      wx.getUserProfile({
        desc: '用于设置个人头像',
        success(res) {
          const wechatAvatarUrl = res.userInfo.avatarUrl
          if (!wechatAvatarUrl) {
            wx.showToast({ title: '获取微信头像失败', icon: 'none' })
            return
          }
          // get_user_profile 返回的头像 URL 可能不带 /0 后缀，补上以获取高清图
          const hdUrl = wechatAvatarUrl.replace(/\/\d+$/, '/0')
          wx.showLoading({ title: '下载头像...' })
          wx.downloadFile({
            url: hdUrl,
            success(downloadRes) {
              wx.hideLoading()
              if (downloadRes.statusCode === 200) {
                that.openAvatarCrop(downloadRes.tempFilePath)
              } else {
                wx.showToast({ title: '下载头像失败', icon: 'none' })
              }
            },
            fail(err) {
              wx.hideLoading()
              console.error('[chooseWechatAvatar] 下载失败:', err)
              wx.showToast({ title: '下载头像失败', icon: 'none' })
            }
          })
        },
        fail(err) {
          console.error('[chooseWechatAvatar] 获取微信头像失败:', err)
          wx.showToast({ title: '获取微信头像授权失败', icon: 'none' })
        }
      })
    },

    // 上传头像到后端（共用）
    uploadAvatar(filePath) {
      const that = this
      const token = wx.getStorageSync('token')
      if (!token) {
        wx.showToast({ title: '请先登录', icon: 'none' })
        return
      }

      wx.showLoading({ title: '上传中...' })
      wx.uploadFile({
        url: CONFIG.BASE_URL + '/api/profile',
        filePath: filePath,
        name: 'avatar',
        header: { 'Authorization': 'Bearer ' + token },
        formData: {
          nickname: that.data.nickname || ''
        },
        success(uploadRes) {
          wx.hideLoading()
          if (uploadRes.statusCode === 401) {
            wx.showToast({ title: '登录已过期', icon: 'none' })
            return
          }
          try {
            const data = JSON.parse(uploadRes.data)
            if (data.success && data.avatar_url) {
              that.setData({ avatarUrl: data.avatar_url })
              wx.setStorageSync('avatarUrl', data.avatar_url)
              wx.showToast({ title: '头像已更新', icon: 'success', duration: 1500 })
            } else {
              console.error('[uploadAvatar] 上传失败:', data)
              wx.showToast({ title: data.message || '上传失败', icon: 'none' })
            }
          } catch (e) {
            console.error('[uploadAvatar] 解析响应失败:', e, uploadRes.data)
            wx.showToast({ title: '上传失败', icon: 'none' })
          }
        },
        fail(err) {
          wx.hideLoading()
          console.error('[uploadAvatar] 网络请求失败:', err)
          wx.showToast({ title: '网络错误', icon: 'none' })
        }
      })
    },

    // ==================== 昵称设置（表情弹窗，避免与主题切换按钮重叠） ====================

    onTapNickname() {
      if (this.data.modalClosing) return
      this.setData({ showNicknamePicker: true })
    },
    onCancelNicknamePicker() {
      if (this.data.modalClosing) return
      this.setData({ modalClosing: true })
      setTimeout(() => {
        this.setData({ showNicknamePicker: false, modalClosing: false })
        // 等弹窗关闭动画播完再聚焦，否则键盘被遮住
        if (this._pendingNicknameAction === 'wechat') {
          this.setData({ focusWechatNickname: true })
          this._pendingNicknameAction = null
        } else if (this._pendingNicknameAction === 'custom') {
          this.setData({ customNickname: this.data.nickname || '', showNicknameInput: true })
          this._pendingNicknameAction = null
        }
      }, 200)
    },

    // 使用微信昵称：触发隐藏的 type="nickname" 原生输入
    onChooseWechatNickname() {
      if (this.data.modalClosing) return
      this._pendingNicknameAction = 'wechat'
      this.setData({ modalClosing: true })
      setTimeout(() => {
        this.setData({ showNicknamePicker: false, modalClosing: false, focusWechatNickname: true })
        this._pendingNicknameAction = null
      }, 200)
    },

    // 使用自定义昵称
    onChooseCustomNickname() {
      if (this.data.modalClosing) return
      this._pendingNicknameAction = 'custom'
      this.setData({ modalClosing: true })
      setTimeout(() => {
        this.setData({ showNicknamePicker: false, modalClosing: false, customNickname: this.data.nickname || '', showNicknameInput: true })
        this._pendingNicknameAction = null
      }, 200)
    },
    onCancelCustomNickname() {
      if (this.data.modalClosing) return
      this.setData({ modalClosing: true })
      setTimeout(() => {
        this.setData({ showNicknameInput: false, modalClosing: false })
      }, 200)
    },
    onCustomNicknameInput(e) {
      this.setData({ customNickname: e.detail.value })
    },
    onSaveCustomNickname() {
      const name = (this.data.customNickname || '').trim()
      if (!name) return
      if (this.data.modalClosing) return
      this.setData({ modalClosing: true })
      setTimeout(() => {
        this.setData({ showNicknameInput: false, modalClosing: false })
        this._saveNickname(name)
      }, 200)
    },

    // 隐藏的 type="nickname" 输入：微信自动填充后保存
    onHiddenNicknameInput(e) {
      const val = (e.detail.value || '').trim()
      if (val) {
        this.setData({ focusWechatNickname: false })
        this._saveNickname(val)
      }
    },
    onHiddenNicknameBlur(e) {
      this.setData({ focusWechatNickname: false })
      const val = (e.detail.value || '').trim()
      if (val) this._saveNickname(val)
    },

    // 保存昵称到后端
    _saveNickname(nickname) {
      this.setData({ nickname })
      const token = wx.getStorageSync('token')
      if (!token) return
      request({
        url: CONFIG.BASE_URL + '/api/profile',
        method: 'POST',
        header: { 'Authorization': 'Bearer ' + token, 'content-type': 'application/json' },
        data: { nickname },
        success: (res) => {
          if (res.statusCode === 200 && res.data.success) {
            wx.setStorageSync('nickname', nickname)
          }
        },
        fail: () => {}
      })
    },

    // ==================== 用户角色 ====================

    loadUserRole() {
      const token = wx.getStorageSync('token')
      if (!token) {
        console.warn('[loadUserRole] token 不存在，跳过')
        return
      }

      request({
        url: CONFIG.BASE_URL + '/api/me',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          if (res.statusCode === 200 && res.data && res.data.success) {
            const role = res.data.role || 'guest'
            const isSuper = res.data.is_super_admin || false
            const tempUntil = res.data.temp_until || ''
            const hasTempAccess = res.data.has_temp_access || false
            this.setData({
              isAdmin: role === 'admin',
              isSuperAdmin: isSuper,
              userRole: role,
              tempUntil: tempUntil,
              licenseInfo: res.data.license_info || null,
            })
            wx.setStorageSync('userRole', role)
            // 从服务端同步主题偏好（跨设备一致）
            const app = getApp()
            app.syncThemeFromServer(res.data.theme_mode)
            // 角色切换会改变 wx:if 区块，内容高度变化显著 → 刷新滚动边界
            this._scheduleMeasure()
            // 管理员加载存储统计 + 开启轮询（loadTempUsers 已在 show 中调用）
            if (role === 'admin') {
              this.loadStorageStats()
              this.loadSecurityConfig()
              this.loadRecentOrders()   // 角色刚确认为管理员（首次/刚被授权）→ 补拉一次
              this._startKeyPolling()
              if (isSuper) {
                this.loadAdmins()
              }
            } else {
              this._stopKeyPolling()
              // 临时授权用户：启动倒计时
              if (hasTempAccess && tempUntil) {
                this._startTempCountdown()
              } else {
                this._stopTempCountdown()
              }
            }
          } else {
            console.error('[loadUserRole] 服务器返回异常:', res.statusCode, res.data)
          }
        },
        fail: (err) => {
          console.error('[loadUserRole] 网络请求失败:', err)
        }
      })
    },

    // 点击卡片右侧许可密钥徽章：向下展开/收起详情
    onToggleLicenseDetail() {
      this.setData({ showLicenseDetail: !this.data.showLicenseDetail })
      // 展开/收起改变内容高度 → 刷新自定义滚动引擎边界（动画结束后再测一次）
      this._scheduleMeasure()
      setTimeout(() => this._scheduleMeasure(300), 320)
    },

    // ==================== 管理员：许可密钥 ====================

    onLicenseMinutesMinus() {
      const v = this.data.licenseMinutes
      if (v > 1) {
        const next = v - 1
        // 即时更新（与存储保留/防滥用步进器一致）；licenseMinutes 为平铺字段，
        // setData 不会重建按钮节点，hover 动画不受影响，无需延迟
        this.setData({ licenseMinutes: next, minusDisabled: next <= 1, plusDisabled: next >= 10 })
      }
    },

    onLicenseMinutesPlus() {
      const v = this.data.licenseMinutes
      if (v < 10) {
        const next = v + 1
        this.setData({ licenseMinutes: next, minusDisabled: next <= 1, plusDisabled: next >= 10 })
      }
    },

    onLicenseMinutesChange(e) {
      const v = parseInt(e.detail.value, 10)
      const next = isNaN(v) || v < 1 ? 1 : v > 10 ? 10 : v
      this.setData({
        licenseMinutes: next,
        minusDisabled: next <= 1,
        plusDisabled: next >= 10
      })
    },

    onSelectKeyType(e) {
      this._setKeyType(e.currentTarget.dataset.type)
    },

    _setKeyType(type) {
      if (type === 'admin' || type === 'temp') {
        this.setData({ keyType: type })
      }
    },

    // ==================== 密钥类型滑块拖动（与 index 页分段滑块一致） ====================
    // 预测量 key-type-toggle 宽度并缓存：createSelectorQuery 异步，
    // 首次触摸才测量会导致第一次拖动无跟手（宽度未就绪直接 return）。
    _measureKeyTypeWidth() {
      if (!this._segW) this._segW = {}
      const q = this.createSelectorQuery()
      q.selectAll('.key-type-toggle').boundingClientRect()
      q.exec((res) => {
        if (res && res[0] && res[0].length) this._segW['keytype'] = res[0].map(r => r.width || 0)
      })
    },

    onSegTouchStart(e) {
      const seg = e.currentTarget.dataset.seg
      const idx = e.currentTarget.dataset.index
      const t = e.touches[0]
      if (!t) return
      this._seg = { seg, idx, key: seg + '-' + idx, startX: t.clientX, startY: t.clientY, locked: false, lastP: undefined }
      if (!this._segW) this._segW = {}
      if (!this._segW[seg]) {
        this._measureKeyTypeWidth()
      }
    },

    onSegTouchMove(e) {
      const s = this._seg
      if (!s) return
      const t = e.touches[0]
      if (!t) return
      const dx = t.clientX - s.startX
      const dy = t.clientY - s.startY
      if (!s.locked) {
        if (Math.abs(dx) < 6 && Math.abs(dy) < 6) return
        if (Math.abs(dy) > Math.abs(dx)) { s.locked = true; return }
        s.locked = true
      }
      const widths = this._segW && this._segW[s.seg]
      const w = widths && widths[s.idx]
      if (!w) return
      const segPx = w / 2
      const cur = this.data.keyType === 'admin' ? 1 : 0
      const p = Math.max(0, Math.min(1, cur + dx / segPx))
      s.lastP = p
      this.setData({ ['segDrag.' + s.key]: Math.round(p * 100) })
    },

    onSegTouchEnd() {
      const s = this._seg
      if (!s) return
      this._seg = null
      if (!s.locked || s.lastP === undefined) return
      const target = Math.round(s.lastP)
      this._setKeyType(target >= 1 ? 'admin' : 'temp')
      this.setData({ ['segDrag.' + s.key]: -1 })
    },

    onGenerateKey() {
      const token = wx.getStorageSync('token')
      if (!token) return
      if (this.data.generating) return

      this.setData({ generating: true })
      request({
        url: CONFIG.BASE_URL + '/api/license/create',
        method: 'POST',
        header: {
          'Authorization': 'Bearer ' + token,
          'content-type': 'application/json'
        },
        data: { validity_minutes: this.data.licenseMinutes, type: this.data.keyType },
        success: (res) => {
          this.setData({ generating: false })
          if (res.data.success) {
            wx.showToast({ title: '密钥已生成', icon: 'success' })
            // 刷新密钥列表
            this.loadActiveKey(false)
          } else {
            wx.showToast({ title: res.data.message || '生成失败', icon: 'none' })
          }
        },
        fail: () => {
          this.setData({ generating: false })
          wx.showToast({ title: '网络错误', icon: 'none' })
        }
      })
    },

    // 倒计时：对所有活跃密钥同时计时
    _startCountdown() {
      this._stopCountdown()
      const tick = () => {
        const keys = this.data.activeKeys
        if (!keys.length) { this._stopCountdown(); return }
        const updates = {}
        let hasActive = false
        keys.forEach((k, i) => {
          // 已被使用的密钥不显示倒计时
          if (k.status !== 'unused') {
            if (k.countdownText !== '已使用') {
              updates['activeKeys[' + i + '].countdownText'] = '已使用'
              updates['activeKeys[' + i + '].expired'] = false
            }
            return
          }
          // admin 类型也显示倒计时（24小时自然过期）
          const target = this._parseServerTime(k.expires_at)
          if (!target) return
          const remain = target - Date.now()
          if (remain > 0) {
            hasActive = true
            const totalSec = Math.ceil(remain / 1000)
            const m = Math.floor(totalSec / 60)
            const s = totalSec % 60
            const text = m + ':' + (s < 10 ? '0' + s : s)
            updates['activeKeys[' + i + '].countdownText'] = text
            updates['activeKeys[' + i + '].expired'] = false
          } else {
            updates['activeKeys[' + i + '].countdownText'] = '已过期'
            updates['activeKeys[' + i + '].expired'] = true
          }
        })
        if (Object.keys(updates).length > 0) this.setData(updates)
        if (!hasActive && !keys.some(k => k.status === 'unused' && !k.expired)) {
          // 所有未使用密钥都已过期且没有被使用的密钥，停止计时器
        }
      }
      tick()
      this._keyCountdownTimer = setInterval(tick, 1000)
    },

    _stopCountdown() {
      if (this._keyCountdownTimer) {
        clearInterval(this._keyCountdownTimer)
        this._keyCountdownTimer = null
      }
    },

    // 解析后端时间字符串 "YYYY-MM-DD HH:MM:SS" → 时间戳（按服务器本地时区）
    _parseServerTime(str) {
      if (!str) return 0
      // 后端用 datetime.now() 生成，无时区后缀；前端按本地时区解析
      const parts = str.replace(/-/g, '/').split(' ')
      if (parts.length !== 2) return 0
      return new Date(parts[0] + ' ' + parts[1]).getTime()
    },

    // 从后端恢复所有活跃密钥（attached / pageLifetimes.show / 轮询 调用）
    // isInitial=true 时新卡片附加入场动画
    loadActiveKey(isInitial, done) {
      const token = wx.getStorageSync('token')
      if (!token) {
        if (done) done(true)
        return
      }
      request({
        url: CONFIG.BASE_URL + '/api/license/active',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          if (res.statusCode !== 200 || !res.data || !res.data.success) {
            if (done) done(false)
            return
          }
          const newKeys = res.data.keys || []
          const oldKeys = this.data.activeKeys || []
          const oldKeySet = oldKeys.map(o => o.key).sort().join(',')
          const newKeySet = newKeys.map(o => o.key).sort().join(',')
          const isFresh = oldKeySet !== newKeySet || !oldKeys.length
          const newKeyLookup = new Set(newKeys.map(o => o.key))
          // 合并：保留本地状态（swipeX/countdownText 等），更新服务端数据
          const merged = newKeys.map((nk, i) => {
            const old = oldKeys.find(o => o.key === nk.key)
            return {
              ...nk,
              expired: old ? old.expired : false,
              countdownText: old ? old.countdownText : '',
              swipeX: old ? old.swipeX : 0,
              swipeTransition: old ? old.swipeTransition : undefined,
              deleteOpacity: old ? old.deleteOpacity : 0,
              deleteQuickFade: old ? !!old.deleteQuickFade : false,
              exiting: old ? !!old.exiting : false,  // 保留离场标记避免被 poll 冲掉
              entering: !old && !isInitial,  // 非首次加载时的新 key → 播放入场动画
            }
          })
          // 已过期/被删的 key：保留并标记 exiting，动画结束后移除
          const removedKeys = oldKeys.filter(o => !newKeyLookup.has(o.key) && !o.exiting)
          for (const rk of removedKeys) {
            merged.push({ ...rk, exiting: true })
          }
          // 始终更新（状态可能变化），只对新增的卡片播放入场动画
          this.setData({ activeKeys: merged })
          // 离场动画完成后真正移除
          if (removedKeys.length > 0) {
            setTimeout(() => {
              const keys = this.data.activeKeys.filter(k => !k.exiting)
              this.setData({ activeKeys: keys })
              if (!keys.length) this._stopCountdown()
            }, 350)
          }
          // 动画完成后清除 entering 标记
          const hasEntering = merged.some(k => k.entering)
          if (hasEntering) {
            setTimeout(() => {
              const keys = this.data.activeKeys
              const updates = {}
              let changed = false
              keys.forEach((k, i) => { if (k.entering) { updates['activeKeys[' + i + '].entering'] = false; changed = true } })
              if (changed) this.setData(updates)
            }, 800)
          }
          if (merged.length > 0) {
            this._startCountdown()
          } else {
            this._stopCountdown()
          }
          if (done) done(true)
        },
        fail: () => {
          if (done) done(false)
        },
      })
    },

    // ==================== 许可密钥左滑删除 ====================

    onKeyTouchStart(e) {
      const idx = e.currentTarget.dataset.idx
      const t = e.touches[0]
      if (!t || idx == null) return
      const k = this.data.activeKeys[idx]
      if (!k) return
      this._keySwipeIdx = idx
      this._keySwipeStartX = t.clientX
      this._keySwipeStartY = t.clientY
      this._keySwipeLastX = t.clientX
      this._keySwipeStartCardX = this.data.activeKeys[idx].swipeX
      this._keySwipeHorizontal = false
      this._swipeHorizontal = false
      this.setData({ ['activeKeys[' + idx + '].swipeTransition']: false })
    },

    onKeyTouchMove(e) {
      if (this._keySwipeIdx == null) return
      const t = e.touches[0]
      if (!t) return
      const dx = t.clientX - this._keySwipeStartX
      const dy = t.clientY - this._keySwipeStartY

      if (!this._keySwipeHorizontal) {
        if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return
        if (Math.abs(dx) > Math.abs(dy)) {
          this._keySwipeHorizontal = true
          this._swipeHorizontal = true
        } else {
          this._keySwipeHorizontal = false
          this._swipeHorizontal = false
          return
        }
      }

      const idx = this._keySwipeIdx
      const rawX = this._keySwipeStartCardX + dx
      const maxX = -this._deleteWidthPx
      const visualX = this._rubberBand(rawX, maxX, 0, 40)
      this._keySwipeLastX = rawX
      // 延迟淡入：滑动超过 35% 才开始显示删除按钮，减少透明重叠
      const progress = Math.abs(rawX) / this._deleteWidthPx
      const opacity = rawX < 0 ? Math.min(1, Math.max(0, (progress - 0.35) / 0.65)) : 0
      this.setData({
        ['activeKeys[' + idx + '].swipeX']: visualX,
        ['activeKeys[' + idx + '].deleteOpacity']: opacity,
        ['activeKeys[' + idx + '].deleteQuickFade']: rawX >= 0,
      })
    },

    onKeyTouchEnd(e) {
      if (!this._keySwipeHorizontal) {
        this._swipeHorizontal = false
        this._keySwipeIdx = null
        return
      }
      const idx = this._keySwipeIdx
      const rawX = this._keySwipeLastX
      const maxX = -this._deleteWidthPx
      const target = rawX > 0 ? 0 : (rawX < maxX ? maxX : (rawX < maxX / 2 ? maxX : 0))
      // 右滑归位时加速淡出，避免透明重叠
      const capturedIdx = idx
      this.setData({
        ['activeKeys[' + idx + '].swipeTransition']: true,
        ['activeKeys[' + idx + '].swipeX']: target,
        ['activeKeys[' + idx + '].deleteOpacity']: target === 0 ? 0 : 1,
        ['activeKeys[' + idx + '].deleteQuickFade']: target === 0,
      })
      // 回弹动画结束后移除内联 transition，让 CSS 重新接管主题过渡
      setTimeout(() => {
        if (capturedIdx < this.data.activeKeys.length) {
          // 注意：不能用 undefined（微信 setData 警告），用 null（同为假值，wxml 判断不受影响）
          this.setData({ ['activeKeys[' + capturedIdx + '].swipeTransition']: null })
        }
      }, 300)
      this._swipeHorizontal = false
      this._keySwipeHorizontal = false
      this._keySwipeIdx = null
    },

    // 通用确认弹窗（替代 wx.showModal，跟随 app 主题）
    _showConfirm(title, content, confirmText, confirmColor, callback) {
      this.setData({
        showConfirmModal: true,
        confirmModalTitle: title,
        confirmModalContent: content,
        confirmModalConfirmText: confirmText,
        confirmModalConfirmColor: confirmColor || '#ff4d4f',
        _confirmCallback: callback,
      })
    },
    onCancelConfirm() {
      if (this.data.modalClosing) return
      this.setData({ modalClosing: true })
      setTimeout(() => {
        this.setData({ showConfirmModal: false, modalClosing: false, _confirmCallback: null })
      }, 200)
    },
    onConfirmModal() {
      if (this.data.modalClosing) return
      const cb = this.data._confirmCallback
      this.setData({ modalClosing: true })
      setTimeout(() => {
        this.setData({ showConfirmModal: false, modalClosing: false, _confirmCallback: null })
        if (typeof cb === 'function') cb()
      }, 200)
    },

    // ==================== 我的文件（S2-08 / D-7）====================
    // 删除我的文件入口：GET /api/my_files 列出自己的文件（含保留期与到期时间），
    // DELETE /api/file/<id> 真正删除（服务端再判属主/活跃订单，409 时提示等订单结束）。
    onOpenMyFiles() {
      // 每次打开都从「全部收起」开始：上次展开的那一行可能已经被删/已换页
      this.setData({ showMyFiles: true, myFilesClosing: false, myFilesDetailId: '', myFilesDetailStyle: '' })
      this.loadMyFiles(true)
    },

    onCloseMyFiles() {
      if (this.data.myFilesClosing) return
      this.setData({ myFilesClosing: true })
      setTimeout(() => this.setData({ showMyFiles: false, myFilesClosing: false }), 200)
    },

    _fmtFileSize(bytes) {
      const n = Number(bytes) || 0
      if (n < 1024) return n + ' B'
      if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
      return (n / (1024 * 1024)).toFixed(1) + ' MB'
    },

    // 2026-11-12（用户要求）：点一行 → 在该行卡片**内部**展开详情（上传时间 / 大小 / 页数 /
    // 所属订单 / 1024 预览图）。预览图是 <image src>，带不了 Authorization 头 ⇒ 用后端
    // 支持的 ?token= 拼在 URL 上（app.py「文件预览图」一节）；has_preview=false 的类型
    // （doc/docx/xls/ppt…）根本不发请求，直接显示类型图标 —— 后端没有 Office 渲染能力。
    _myFileTypeIcon(name) {
      const ext = String(name || '').toLowerCase().split('.').pop()
      if (ext === 'pdf') return '📕'
      if (['jpg', 'jpeg', 'png', 'webp', 'bmp', 'gif', 'tif', 'tiff'].indexOf(ext) >= 0) return '🖼'
      if (['doc', 'docx'].indexOf(ext) >= 0) return '📘'
      if (['xls', 'xlsx'].indexOf(ext) >= 0) return '📗'
      if (['ppt', 'pptx'].indexOf(ext) >= 0) return '📙'
      return '📄'
    },

    _decorateMyFile(f) {
      const hours = Number(f.retention_hours) || 0
      const retentionText = f.retention_label || (hours < 0
        ? '打印完成后立即删除'
        : (hours > 0 ? ('保留 ' + (hours >= 24 && hours % 24 === 0 ? (hours / 24) + ' 天' : hours + ' 小时')) : '不自动删除'))
      const expireText = f.auto_delete_at ? ('将于 ' + f.auto_delete_at + ' 自动删除') : retentionText
      const previewPath = String(f.preview_url || '')
      const token = (wx.getStorageSync('token') || '')
      const previewUrl = (f.has_preview !== false && previewPath)
        ? (CONFIG.BASE_URL + previewPath + (previewPath.indexOf('?') >= 0 ? '&' : '?') +
           'token=' + encodeURIComponent(token))
        : ''
      const pages = Number(f.page_count) || 0
      // 2026-11-13 用户要求 1/2/3：多页预览 + 加载骨架 + 点开全屏。
      // preview_pages 是后端给的页数提示（图片恒 1，Office/PDF 取真实页数）；
      // 后端没分析出页数时为 0，展开时再问 /api/file_preview/<id>/info。
      const previewPages = Math.max(0, Number(f.preview_pages) || 0)
      return Object.assign({}, f, {
        sizeText: this._fmtFileSize(f.size),
        retentionText: retentionText,
        expireText: expireText,
        previewUrl: previewUrl,
        previewPages: previewPages,
        previewShots: previewUrl
          ? [this._myFilePreviewShot(this._myFilePreviewUrl(f.file_id, 1), 1, true)]
          : [],
        previewIndex: 0,
        previewLoading: !!previewUrl,
        previewFailed: false,
        // 2026-11-13：后端已能渲染 Office（soffice 转 PDF 取第 1 页），提示语同步改口径
        previewTip: f.has_preview === false
          ? '该类型暂不支持预览图（图片 / PDF / Word / Excel / PPT 可以）'
          : '预览图暂不可用，可稍后重试',
        typeIcon: this._myFileTypeIcon(f.original_name),
        createdAtText: f.created_at ? String(f.created_at) : '未知',
        pageCountText: pages > 0 ? (pages + ' 页') : '',
        orderText: f.order_number ? String(f.order_number) : '未下单（仅上传）',
      })
    },

    // 单页预览图地址：<BASE>/api/file_preview/<id>?token=..&page=N（<image> 带不了 Bearer 头）
    _myFilePreviewUrl(fileId, page) {
      const token = (wx.getStorageSync('token') || '')
      return CONFIG.BASE_URL + '/api/file_preview/' + encodeURIComponent(fileId) +
        '?token=' + encodeURIComponent(token) + '&page=' + (Number(page) || 1)
    },

    _myFilePreviewShot(url, page, ready) {
      return { page: Number(page) || 1, url: url, ready: !!ready, loaded: false, failed: false }
    },

    // 只让当前页 ±1 页真的发请求：一份 12 页的 PDF 不该一展开就拉 12 张图
    _myFilePreviewShots(fileId, total, index, keepLoaded) {
      const n = Math.max(1, Math.min(PREVIEW_MAX_PAGES, Number(total) || 1))
      const cur = Number(index) || 0
      const old = {}
      ;(keepLoaded || []).forEach((s) => { old[s.page] = s })
      const shots = []
      for (let p = 1; p <= n; p++) {
        const prev = old[p]
        shots.push({
          page: p,
          url: this._myFilePreviewUrl(fileId, p),
          ready: prev ? !!prev.ready : Math.abs(p - 1 - cur) <= 1,
          loaded: prev ? !!prev.loaded : false,
          failed: prev ? !!prev.failed : false,
        })
      }
      return shots
    },

    // 更新「我的文件」列表里的某个文件（详情弹窗走 _patchFileDetail）
    _patchMyFile(fileId, fn) {
      const list = (this.data.myFiles || []).map((f) => (
        f.file_id === fileId ? Object.assign({}, f, fn(f) || {}) : f
      ))
      this.setData({ myFiles: list })
    },

    _patchFileDetail(fn) {
      const cur = this.data.fileDetail
      if (!cur) return
      this.setData({ fileDetail: Object.assign({}, cur, fn(cur) || {}) })
    },

    // 展开详情时才准备预览：已知页数直接用，未知再问 /info（后端会顺带预热前几页）
    _prepareMyFilePreview(item, apply) {
      if (!item || item.has_preview === false || !item.previewUrl) return
      const fileId = item.file_id
      const pages = Math.max(0, Number(item.preview_pages) || 0)
      const total = pages > 0 ? Math.min(pages, PREVIEW_MAX_PAGES) : 1
      apply({
        previewShots: this._myFilePreviewShots(fileId, total, 0, item.previewShots),
        previewIndex: 0,
        previewLoading: true,
        previewFailed: false,
      })
      // 页数一律向 info 端点核对一次（本页生命周期内缓存）：
      // files.page_count 是页数分析的结果，Office 转成 PDF 后**可能比它多**
      // （线上样例：page_count=2 的 docx 实际 3 页），只信它会让多页少一页。
      // 已核对过的文件不再重复请求；info 顺带把前 12 页预热出来。
      this._previewInfoCache = this._previewInfoCache || {}
      if (this._previewInfoCache[fileId]) return
      request({
        url: CONFIG.BASE_URL + '/api/file_preview/' + encodeURIComponent(fileId) +
          '/info?token=' + encodeURIComponent(wx.getStorageSync('token') || ''),
        method: 'GET',
        success: (res) => {
          const d = (res && res.data) || {}
          if (res.statusCode !== 200 || !d.success) return
          const n = Math.min(PREVIEW_MAX_PAGES, Number(d.pages) || 0)
          this._previewInfoCache[fileId] = n || 1
          if (n <= 1) return
          const cur = Number(item.preview_pages) || 0
          if (cur === n) return
          apply({
            previewShots: this._myFilePreviewShots(fileId, n, 0, item.previewShots),
            previewPages: n,
          })
        },
      })
    },

    // 预览图加载完：淡入（CSS opacity 过渡），骨架撤掉
    onMyFilePreviewLoad(e) {
      const ds = (e && e.currentTarget && e.currentTarget.dataset) || {}
      const page = Number(ds.page) || 1
      const apply = (fn) => (ds.scope === 'detail'
        ? this._patchFileDetail(fn)
        : this._patchMyFile(ds.id || '', fn))
      apply((f) => ({
        previewLoading: false,
        previewShots: (f.previewShots || []).map((s) => (
          s.page === page ? Object.assign({}, s, { loaded: true, failed: false }) : s
        )),
      }))
    },

    // 翻页：更新页码，并把相邻页标成 ready（预取，不让用户看着空白等）
    onMyFilePreviewChange(e) {
      const ds = (e && e.currentTarget && e.currentTarget.dataset) || {}
      const index = Number(e && e.detail && e.detail.current) || 0
      const apply = (fn) => (ds.scope === 'detail'
        ? this._patchFileDetail(fn)
        : this._patchMyFile(ds.id || '', fn))
      apply((f) => ({
        previewIndex: index,
        previewShots: (f.previewShots || []).map((s, i) => (
          Math.abs(i - index) <= 1 && !s.ready ? Object.assign({}, s, { ready: true }) : s
        )),
      }))
    },

    // 点预览图 → 全屏。用自绘层（utils/preview_viewer.js）而不是 wx.previewImage：
    // 原生预览层的手势随微信版本/机型变化，双击放大不保证（用户 2026-11-13 要求双击放大）。
    // urls 里带上全部已切片的页，index 指定当前页。
    onOpenMyFilePreview(e) {
      const ds = (e && e.currentTarget && e.currentTarget.dataset) || {}
      const item = ds.scope === 'detail'
        ? this.data.fileDetail
        : (this.data.myFiles || []).find((f) => f.file_id === ds.id)
      if (!item) return
      const shots = (item.previewShots || []).filter((s) => s.url)
      if (!shots.length) return
      const urls = shots.map((s) => s.url)
      const idx = Math.max(0, Math.min(urls.length - 1, Number(ds.index) || 0))
      // 自绘全屏层（双击放大 / 双指捏合 / 左右滑翻页）——不再用 wx.previewImage：
      // 原生预览层的双击放大随微信版本/机型而变，用户 2026-11-13 明确要求双击放大。
      pvCore().open(this, urls, idx)
    },

    /* ---------------- 全屏预览层（模板 pages/me/preview-viewer.wxml） ---------------- */
    onPvTouchStart(e) { pvCore().touchStart(this, e) },
    onPvTouchMove(e) { pvCore().touchMove(this, e) },
    onPvTouchEnd(e) { pvCore().touchEnd(this, e) },
    onPvTouchCancel() { pvCore().touchCancel(this) },
    onPvClose() { pvCore().close(this) },
    onPvPrev() { pvCore().page(this, -1) },
    onPvNext() { pvCore().page(this, 1) },
    onPvImageError() { this.setData({ 'pv.failed': true }) },

    // 订单卡片「文件列表」→ 文件详情弹窗（GET /api/file_detail/<id>，与列表项同形状）
    /* 订单卡片里那一行文件对象（详情弹窗拿它兜底：没有云端文件时也要把打印参数摊开） */
    _orderFileAt(orderId, findex) {
      const orders = this.data.orders || []
      const order = orders.find(o => String(o.id) === String(orderId))
      if (!order || !order.files) return null
      const f = order.files[Number(findex)]
      return f || null
    },

    /* 本地打单 / 云端副本已被清理：没有预览图，但这一行的打印参数还在这张单上。
       2026-11-13 晚用户报「我的打印任务里点任意文件都提示该文件已清理」——
       根因是本地打单的 order_files.file_id 本来就是空的（生产库 2000+ 行），
       以前这里一律弹「已清理」，等于把已知信息也藏了。 */
    _localFileDetail(f, reason) {
      const file = f || {}
      const name = file.original_name || file.file_name || '未知文件'
      const copies = Number(file.copies) || 1
      const pages = Number(file.page_count) || 0
      const eff = Number(file.effective_pages) || pages
      const sheets = Number(file.total_sheets) || 0
      // order_files.status 的词表跟订单一样：sent 才是"已完成"（success 只在旧数据里出现）
      const statusMap = {
        pending: '待打印', queued: '排队中', printing: '打印中',
        sent: '已完成', success: '已完成', failed: '打印失败', rejected: '已拒绝',
        cancelled: '已取消', canceled: '已取消', timeout: '超时', abandoned: '放弃打印',
        downloading: '文件传输中', waiting: '等待打印', scheduled: '已预约', reserved: '已预留',
      }
      const rows = [
        { k: '份数 / 页数', v: file.pagesLine || (copies + ' 份 × ' + eff + ' 页') },
      ]
      if (sheets > 0) rows.push({ k: '合计张数', v: sheets + ' 张' })
      rows.push({ k: '单双面', v: file.duplex === 'on' ? '双面' : '单面' })
      if (file.page_range) rows.push({ k: '页码范围', v: file.page_range })
      if (file.simplex_pages) rows.push({ k: '单面页', v: file.simplex_pages })
      if (file.blank_tail_exclude) rows.push({ k: '末尾空白页', v: '已排除' })
      if (file.status) rows.push({ k: '打印状态', v: statusMap[file.status] || file.status })
      if (file.reject_reason) rows.push({ k: '退回原因', v: file.reject_reason })
      return {
        local_only: true,
        original_name: name,
        typeIcon: this._myFileTypeIcon(name),
        localNote: reason === 'cleaned'
          ? '云端副本已被清理（保留期到了或手动删除过），预览图取不回来了；这单的打印参数还在，一起放在下面。'
          : '这单是本地打单 —— 源文件只保存在店主的电脑上，云端没有副本，所以没有预览图。',
        localRows: rows,
      }
    },

    onOpenOrderFileDetail(e) {
      const ds = (e && e.currentTarget && e.currentTarget.dataset) || {}
      const fileId = ds.id || ''
      const rowFile = this._orderFileAt(ds.oid, ds.findex)
      const showLocal = (reason) => this.setData({
        fileDetailVisible: true,
        fileDetailClosing: false,
        fileDetailLoading: false,
        fileDetail: this._localFileDetail(rowFile, reason),
      })
      if (!fileId) {
        // 本地打单：没有云端文件，但打印参数还在这单上，别再说「已清理」
        if (rowFile) showLocal('local')
        else wx.showToast({ title: '这行没有可查看的文件', icon: 'none' })
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
        success: (res) => {
          const d = (res && res.data) || {}
          if (res.statusCode === 200 && d.success && d.file) {
            const item = this._decorateMyFile(d.file)
            this.setData({ fileDetail: item, fileDetailLoading: false })
            this._prepareMyFilePreview(item, (patch) => this._patchFileDetail(() => patch))
          } else if (rowFile) {
            // 云端副本被清理/无权看：退回「只有打印参数」的那一版，不再弹一句「已清理」就完事
            showLocal('cleaned')
          } else {
            this.setData({ fileDetailLoading: false })
            wx.showToast({ title: (d && d.message) || '文件不存在或已清理', icon: 'none' })
          }
        },
        fail: () => {
          // 网络断了也别把已知的参数藏起来：有这行数据就摊开参数面板（说明是网络问题）
          if (rowFile) { showLocal('cleaned'); return }
          this.setData({ fileDetailLoading: false })
          wx.showToast({ title: '网络异常，请重试', icon: 'none' })
        },
      })
    },

    onCloseFileDetail() {
      if (this.data.fileDetailClosing) return
      this.setData({ fileDetailClosing: true })
      setTimeout(() => this.setData({
        fileDetailVisible: false,
        fileDetailClosing: false,
        fileDetail: null,
      }), 200)
    },

    loadMyFiles(reset) {
      if (this.data.myFilesLoading && !reset) return
      const page = reset ? 1 : (this.data.myFilesPage || 0) + 1
      this.setData({ myFilesLoading: true })
      request({
        url: CONFIG.BASE_URL + '/api/my_files?page=' + page + '&per_page=20',
        method: 'GET',
        success: (res) => {
          const d = (res && res.data) || {}
          if (res.statusCode !== 200 || !d.success) {
            this.setData({ myFilesLoading: false })
            wx.showToast({ title: (d && d.message) || '加载失败，请重试', icon: 'none' })
            return
          }
          const files = (d.files || []).map(f => this._decorateMyFile(f))
          this.setData({
            myFiles: reset ? files : (this.data.myFiles || []).concat(files),
            myFilesPage: page,
            myFilesTotal: Number(d.total) || files.length,
            myFilesLoading: false,
          })
        },
        fail: () => {
          this.setData({ myFilesLoading: false })
          wx.showToast({ title: '网络异常，请重试', icon: 'none' })
        },
      })
    },

    onLoadMoreMyFiles() {
      this.loadMyFiles(false)
    },

    // 点行 = 展开/收起该行详情（同一时间只展开一个；再点同一行收起）
    //
    // 2026-11-13 用户要求 1「加上展开和收起动画」。小程序没有 FLIP，只能手量高度：
    //   展开：先渲染 height:0 那一帧 → 量 .myfile-detail-card 真实高度 → 过渡到该高度
    //         → 过渡结束改成 height:auto（预览图加载完把内容撑高时不会留白/裁字）；
    //   收起：先量当前 px（此时可能已是 auto）→ 下一帧归零 → 过渡结束才真正卸载节点。
    // 旧写法 wx:if 直接挂载/卸载 = 瞬间出现/消失，正是要补的那段。
    _myFileDetailStyle(height, opacity) {
      return (height === null ? 'height:auto;' : ('height:' + height + 'px;')) +
        'opacity:' + (opacity ? 1 : 0) + ';'
    },

    _clearMyFileDetailTimer() {
      if (this._myFileDetailTimer) {
        clearTimeout(this._myFileDetailTimer)
        this._myFileDetailTimer = null
      }
    },

    _nextFrame(cb) {
      if (typeof wx.nextTick === 'function') wx.nextTick(cb)
      else setTimeout(cb, 16)
    },

    _measureMyFileDetailCard(cb) {
      // 没有测量能力（单测桩 / 老基础库）就当量到 0：上层会退回"瞬时展开收起"，
      // 动画是视觉层，绝不能因为量不到高度把状态卡住。
      if (typeof wx.createSelectorQuery !== 'function') { cb(0); return }
      wx.createSelectorQuery()
        .select('.myfile-detail-card')
        .boundingClientRect((rect) => cb(rect && rect.height ? Math.ceil(rect.height) : 0))
        .exec()
    },

    _openMyFileDetail(fileId) {
      this._clearMyFileDetailTimer()
      this.setData({
        myFilesDetailId: fileId,
        myFilesDetailStyle: this._myFileDetailStyle(0, 0),
      })
      // 预览图要在卡片挂载后就开始准备（多页 + 骨架），否则动画结束后才看到一块灰
      const item = (this.data.myFiles || []).find((f) => f.file_id === fileId)
      if (item) {
        this._prepareMyFilePreview(item, (patch) => this._patchMyFile(fileId, () => patch))
      }
      // 必须等"高度 0"这一帧真的渲染出来再量，否则量到的还是 0
      this._nextFrame(() => {
        if (this.data.myFilesDetailId !== fileId) return
        this._measureMyFileDetailCard((h) => {
          if (this.data.myFilesDetailId !== fileId) return
          if (!h) {
            // 量不到（极端情况）：别把内容锁在 0 高度，直接给 auto
            this.setData({ myFilesDetailStyle: this._myFileDetailStyle(null, 1) })
            return
          }
          this.setData({ myFilesDetailStyle: this._myFileDetailStyle(h, 1) })
          this._myFileDetailTimer = setTimeout(() => {
            this._myFileDetailTimer = null
            if (this.data.myFilesDetailId === fileId) {
              this.setData({ myFilesDetailStyle: this._myFileDetailStyle(null, 1) })
            }
          }, 260)
        })
      })
    },

    _closeMyFileDetail() {
      this._clearMyFileDetailTimer()
      this._measureMyFileDetailCard((h) => {
        if (!h) {
          // 量不到高度（单测桩/极端情况）：直接收起，别让它卡在"关不掉"的状态
          this.setData({ myFilesDetailId: '', myFilesDetailStyle: '' })
          return
        }
        // 先把 height:auto 钉成具体 px，否则 auto → 0 没有可插值的起点
        this.setData({ myFilesDetailStyle: this._myFileDetailStyle(h, 1) })
        this._nextFrame(() => {
          this.setData({ myFilesDetailStyle: this._myFileDetailStyle(0, 0) })
          this._myFileDetailTimer = setTimeout(() => {
            this._myFileDetailTimer = null
            this.setData({ myFilesDetailId: '', myFilesDetailStyle: '' })
          }, 260)
        })
      })
    },

    onToggleMyFileDetail(e) {
      const fileId = (e && e.currentTarget && e.currentTarget.dataset)
        ? (e.currentTarget.dataset.id || '') : ''
      if (!fileId) return
      if (this.data.myFilesDetailId === fileId) {
        this._closeMyFileDetail()
        return
      }
      // 换了另一行：上一行的详情直接换掉（不做"先收再开"两段动画，操作更跟手）
      this._clearMyFileDetailTimer()
      this.setData({ myFilesDetailId: '', myFilesDetailStyle: '' }, () => {
        this._openMyFileDetail(fileId)
      })
    },

    // 预览图加载失败（415 / 403 / 网络）→ 静默退回类型图标，不弹错误框：
    // 预览图是锦上添花，不能因为它把整行详情搞红。
    onMyFilePreviewError(e) {
      const ds = (e && e.currentTarget && e.currentTarget.dataset) || {}
      const fileId = ds.id || ''
      if (!fileId) return
      const page = Number(ds.page) || 1
      const apply = (fn) => (ds.scope === 'detail'
        ? this._patchFileDetail(fn)
        : this._patchMyFile(fileId, fn))
      apply((f) => {
        const shots = (f.previewShots || []).map((s) => (
          s.page === page ? Object.assign({}, s, { failed: true, loaded: false }) : s
        ))
        // 多页时某一页坏掉不该把整块预览换成占位图，只有一页可用都没有才退回类型图标
        const anyOk = shots.some((s) => s.ready && !s.failed)
        return { previewShots: shots, previewLoading: false, previewFailed: !anyOk }
      })
    },

    onDeleteMyFile(e) {
      const fileId = e.currentTarget.dataset.id
      const name = e.currentTarget.dataset.name || '该文件'
      if (!fileId) return
      this._showConfirm('删除文件', '确定删除「' + name + '」吗？删除后不可恢复。', '删除', '#FF3B30', () => {
        wx.showLoading({ title: '删除中...' })
        request({
          url: CONFIG.BASE_URL + '/api/file/' + encodeURIComponent(fileId),
          method: 'DELETE',
          success: (res) => {
            wx.hideLoading()
            const d = (res && res.data) || {}
            if (res.statusCode === 200 && d.success) {
              const list = (this.data.myFiles || []).filter(f => f.file_id !== fileId)
              this.setData({
                myFiles: list,
                myFilesTotal: Math.max(0, (Number(this.data.myFilesTotal) || 1) - 1),
                myFilesDetailId: this.data.myFilesDetailId === fileId ? '' : this.data.myFilesDetailId,
              })
              wx.showToast({ title: '已删除', icon: 'success' })
            } else {
              wx.showToast({ title: (d && d.message) || '删除失败，请稍后重试', icon: 'none', duration: 2500 })
            }
          },
          fail: () => {
            wx.hideLoading()
            wx.showToast({ title: '网络异常，请重试', icon: 'none' })
          },
        })
      })
    },

    // 作废指定密钥
    onRevokeKey(e) {
      const idx = e.currentTarget.dataset.idx
      const k = this.data.activeKeys[idx]
      if (!k) return
      const token = wx.getStorageSync('token')
      if (!token) return
      const isUsed = k.status !== 'unused'
      const that = this
      this._showConfirm(
        isUsed ? '删除密钥' : '作废密钥',
        isUsed ? '删除此密钥不会影响已获得的授权。' : '确定作废此许可密钥？作废后他人将无法使用。',
        isUsed ? '删除' : '作废',
        '#ff4d4f',
        function() {
          const k2 = that.data.activeKeys[idx]
          if (!k2) return
          const isUsed2 = k2.status !== 'unused'
          // 第一步：右滑收起删除按钮（0.25s）
          that.setData({
            ['activeKeys[' + idx + '].swipeTransition']: true,
            ['activeKeys[' + idx + '].swipeX']: 0,
            ['activeKeys[' + idx + '].deleteOpacity']: 0,
            ['activeKeys[' + idx + '].deleteQuickFade']: false,
          })
          request({
            url: CONFIG.BASE_URL + '/api/license/revoke',
            method: 'POST',
            header: { 'Authorization': 'Bearer ' + token, 'content-type': 'application/json' },
            data: { key: k2.key },
            success: function(res) {
              if (res.data && res.data.success) {
                wx.showToast({ title: isUsed2 ? '已删除' : '已作废', icon: 'success' })
                // 第二步：右滑收起完成后播离场收起动画（keySlideOut 0.3s），再移除
                setTimeout(function() {
                  that.setData({ ['activeKeys[' + idx + '].exiting']: true })
                  setTimeout(function() {
                    var keys = that.data.activeKeys.filter(function(_, i) { return i !== idx })
                    that.setData({ activeKeys: keys })
                    if (!keys.length) that._stopCountdown()
                  }, 350)
                }, 250)
              } else {
                wx.showToast({ title: res.data.message || '操作失败', icon: 'none' })
                // 恢复：回到滑开状态（删除按钮可见，便于重试）
                that.setData({
                  ['activeKeys[' + idx + '].exiting']: false,
                  ['activeKeys[' + idx + '].swipeX']: -that._deleteWidthPx,
                  ['activeKeys[' + idx + '].deleteOpacity']: 1,
                })
              }
            },
            fail: function() {
              wx.showToast({ title: '网络错误', icon: 'none' })
              that.setData({
                ['activeKeys[' + idx + '].exiting']: false,
                ['activeKeys[' + idx + '].swipeX']: -that._deleteWidthPx,
                ['activeKeys[' + idx + '].deleteOpacity']: 1,
              })
            }
          })
        }
      )
    },

    // 复制密钥
    onCopyKey(e) {
      const idx = e.currentTarget.dataset.idx
      const k = this.data.activeKeys[idx]
      if (!k) return
      const remain = k.countdownText || ''
      const text = '这是HN同学的打印机的使用许可密钥，剩余有效时间' + remain + '，请在有效期内填写到小程序的指定位置:\n密钥: ' + k.key
      wx.setClipboardData({
        data: text,
        success: () => wx.showToast({ title: '已复制到剪贴板', icon: 'success' })
      })
    },

    // ==================== 账号绑定入口（管理功能在独立页面 pages/bind/bind） ====================

    onGoBindPage() {
      this._navigateWithAnimation('/pages/bind/bind')
    },

    // 拉取已绑定设备数，用于入口副标题展示
    loadBindDeviceCount() {
      const token = wx.getStorageSync('token')
      if (!token) return
      request({
        url: CONFIG.BASE_URL + '/api/bind/devices',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          if (!res.data || !res.data.success) return
          this.setData({ bindDeviceCount: (res.data.devices || []).length })
        },
        fail: () => {}
      })
    },

    // 结束打印任务
    onEndPrintTask(e) {
      const idx = e.currentTarget.dataset.idx
      const k = this.data.activeKeys[idx]
      if (!k || !k.order_id) {
        wx.showToast({ title: '未找到关联订单', icon: 'none' })
        return
      }
      const token = wx.getStorageSync('token')
      if (!token) return

      wx.showLoading({ title: '获取订单详情...' })
      request({
        url: CONFIG.BASE_URL + '/api/order_price/' + k.order_id,
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          wx.hideLoading()
          if (res.data && res.data.success) {
            const d = res.data
            const files = d.files || []
            const username = k.used_by_nickname || '用户'
            let text = '【打印任务结算】\n用户: ' + username
            files.forEach((f, i) => {
              const unitPrice = (typeof f.per_copy_price === 'number') ? f.per_copy_price : 0
              const fileTotal = (typeof f.total_price === 'number') ? f.total_price : 0
              text += '\n文件' + (i + 1) + ': ' + f.file_name
              const _eff = typeof f.effective_pages === 'number' ? f.effective_pages : f.page_count
              const _sh = typeof f.total_sheets === 'number' ? f.total_sheets : 0
              text += ' | ' + f.copies + '份 × ' + _eff + '页' + (f.page_range ? '（选' + f.page_range + '）' : '') + ' | ' + _sh + '张'
              text += ' | 单价: ¥' + unitPrice.toFixed(2)
              text += ' | 小计: ¥' + fileTotal.toFixed(2)
            })
            text += '\n总价: ¥' + (typeof d.total_price === 'number' ? d.total_price : 0).toFixed(2)
            wx.setClipboardData({
              data: text,
              success: () => wx.showToast({ title: '已复制结算详情', icon: 'success' })
            })
          } else {
            // 404 时后端提示"该订单不属于你"（管理员查他人订单）→ 改为通用提示
            const msg = res.statusCode === 404
              ? '无法获取该订单结算信息'
              : ((res.data && res.data.message) || '获取失败')
            wx.showToast({ title: msg, icon: 'none' })
          }
        },
        fail: () => { wx.hideLoading(); wx.showToast({ title: '网络错误', icon: 'none' }) }
      })
    },

    // 确认管理员密钥生效
    onConfirmAdminKey(e) {
      const idx = e.currentTarget.dataset.idx
      const k = this.data.activeKeys[idx]
      if (!k) return
      const token = wx.getStorageSync('token')
      if (!token) return
      request({
        url: CONFIG.BASE_URL + '/api/license/finish',
        method: 'POST',
        header: { 'Authorization': 'Bearer ' + token, 'content-type': 'application/json' },
        data: { key: k.key },
        success: (res) => {
          if (res.data && res.data.success) {
            wx.showToast({ title: '已确认', icon: 'success' })
            this.loadActiveKey(false)
          } else {
            wx.showToast({ title: (res.data && res.data.message) || '操作失败', icon: 'none' })
          }
        },
        fail: () => wx.showToast({ title: '网络错误', icon: 'none' })
      })
    },

    // ==================== 超级管理员：管理员列表 ====================

    loadAdmins() {
      const token = wx.getStorageSync('token')
      if (!token) return
      this.setData({ adminsLoading: true })
      request({
        url: CONFIG.BASE_URL + '/api/admin/admins',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        data: { page: 1, page_size: 50 },
        success: (res) => {
          this.setData({ adminsLoading: false })
          if (res.data && res.data.success) {
            this.setData({ admins: res.data.admins || [] })
            this._scheduleMeasure()
          }
        },
        fail: () => {
          this.setData({ adminsLoading: false })
        }
      })
    },

    // 管理员卡片左滑手势
    onAdminTouchStart(e) {
      const openid = e.currentTarget.dataset.openid
      const t = e.touches[0]
      if (!t) return
      this._adminSwipeData = this._adminSwipeData || {}
      this._adminSwipeData[openid] = {
        startX: t.clientX,
        startY: t.clientY,
        lastX: t.clientX,
        startCardX: this.data.adminSwipeX[openid] || 0,  // 卡片当前偏移，保证从已滑开位置继续拖拽
        horizontal: false,
        moved: false,
      }
      const trans = { ...this.data.adminSwipeTransition }
      trans[openid] = false
      this.setData({ adminSwipeTransition: trans })
    },

    onAdminTouchMove(e) {
      const openid = e.currentTarget.dataset.openid
      const sd = this._adminSwipeData && this._adminSwipeData[openid]
      if (!sd) return
      const t = e.touches[0]
      if (!t) return
      const dx = t.clientX - sd.startX
      const dy = t.clientY - sd.startY
      if (!sd.horizontal) {
        if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return
        sd.moved = true
        if (Math.abs(dx) > Math.abs(dy)) {
          sd.horizontal = true
          // 通知滚动引擎让出控制权（与密钥左滑共用 _swipeHorizontal 标记）
          this._swipeHorizontal = true
        } else {
          return
        }
      }
      sd.moved = true
      const rawX = sd.startCardX + dx       // 原始位置（允许越界）
      const maxX = -this._adminDeleteWidthPx
      // 橡皮筋阻尼：过滑 >0 或 <maxX 时产生抵抗感，松手自动回弹
      const visualX = this._rubberBand(rawX, maxX, 0, 55)
      sd.lastX = rawX                       // 保存原始值用于吸附判断
      sd.lastVisualX = visualX
      const swipeX = { ...this.data.adminSwipeX }
      swipeX[openid] = visualX
      const opacity = { ...this.data.adminDeleteOpacity }
      opacity[openid] = rawX < 0 ? Math.min(1, Math.abs(rawX) / (this._adminDeleteWidthPx * 0.6)) : 0
      this.setData({ adminSwipeX: swipeX, adminDeleteOpacity: opacity })
    },

    onAdminTouchEnd(e) {
      // 释放滚动引擎控制权
      this._swipeHorizontal = false
      const openid = e.currentTarget.dataset.openid
      const sd = this._adminSwipeData && this._adminSwipeData[openid]
      if (!sd) return
      if (!sd.moved) {
        // 没有任何移动 → 纯点击，跳转到该管理员的任务列表
        const admin = this.data.admins.find(a => a.openid === openid)
        if (admin) {
          // S2-09（DS-22）：目标 openid 不再拼进页面 URL（页面栈/截图/真机调试都会带出去），
          // 改由 app.globalData 交接；user-orders 页 attached() 读取后立即清空。
          const app = getApp()
          app.globalData.userOrdersTarget = { openid, nickname: admin.nickname || '' }
          this._navigateWithAnimation('/pages/user-orders/user-orders')
        }
        return
      }
      if (!sd.horizontal) return  // 垂直滑动滚屏，不处理
      const trans = { ...this.data.adminSwipeTransition }
      trans[openid] = true
      const maxX = -this._adminDeleteWidthPx
      const rawX = sd.lastX
      // 吸附：越界弹回边界，正常范围过半则吸附露出按钮
      const target = rawX > 0 ? 0 : (rawX < maxX ? maxX : (rawX < maxX / 2 ? maxX : 0))
      const opacity = { ...this.data.adminDeleteOpacity }
      opacity[openid] = target === 0 ? 0 : 1
      this.setData({ adminSwipeTransition: trans, adminDeleteOpacity: opacity })
      wx.nextTick(() => {
        const swipeX = { ...this.data.adminSwipeX }
        swipeX[openid] = target
        this.setData({ adminSwipeX: swipeX })
        // 回弹动画结束后移除内联 transition，让 CSS 重新接管主题过渡
        const capturedOpenid = openid
        setTimeout(() => {
          const cleanTrans = { ...this.data.adminSwipeTransition }
          delete cleanTrans[capturedOpenid]
          this.setData({ adminSwipeTransition: cleanTrans })
        }, 300)
      })
    },

    onRemoveAdmin(e) {
      const openid = e.currentTarget.dataset.openid
      const token = wx.getStorageSync('token')
      if (!token) return
      this._showConfirm('移除管理员', '确定要移除该管理员吗？', '移除', '#ff4d4f', () => {
          request({
            url: CONFIG.BASE_URL + '/api/admin/remove_admin',
            method: 'POST',
            header: { 'Authorization': 'Bearer ' + token, 'content-type': 'application/json' },
            data: { openid: openid },
            success: (res) => {
              if (res.data && res.data.success) {
                wx.showToast({ title: '已移除', icon: 'success' })
                // ① 先滑回收起（隐藏红色删除按钮）
                const swipeX = { ...this.data.adminSwipeX }
                swipeX[openid] = 0
                const trans = { ...this.data.adminSwipeTransition }
                trans[openid] = true
                this.setData({ adminSwipeX: swipeX, adminSwipeTransition: trans })
                // ② 收回后再向上淡出（与许可密钥卡片一致的删除动画）
                setTimeout(() => {
                  const admins = this.data.admins.map(a =>
                    a.openid === openid ? { ...a, _exiting: true } : a
                  )
                  this.setData({ admins })
                  // ③ 动画完成后真正移除
                  setTimeout(() => {
                    this.setData({ admins: this.data.admins.filter(a => a.openid !== openid) })
                    this._scheduleMeasure()
                  }, 350)
                }, 180)
              } else {
                wx.showToast({ title: res.data.message || '移除失败', icon: 'none' })
              }
            },
            fail: () => {
              wx.showToast({ title: '网络错误', icon: 'none' })
            }
          })
      })
    },

    // F10: 跳转历史授权用户页面
    onGoAuthorizedUsers() {
      this._navigateWithAnimation('/pages/authorized-users/authorized-users')
    },

    loadStorageStats() {
      const token = wx.getStorageSync('token')
      if (!token) return

      request({
        url: CONFIG.BASE_URL + '/api/admin/storage',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          if (res.statusCode === 200 && res.data && res.data.success) {
            this.setData({
              storageStats: res.data,
              retentionDays: res.data.retention_days ?? 7,
              retentionHours: res.data.retention_hours ?? 0,
            })
          }
        },
      })
    },

    // ---- 保留时间步进器 ----
    onRetentionDaysMinus() {
      const v = this.data.retentionDays
      if (v > 0) this.setData({ retentionDays: v - 1 })
    },
    onRetentionDaysPlus() {
      const v = this.data.retentionDays
      if (v < 365) this.setData({ retentionDays: v + 1 })
    },
    onRetentionDaysChange(e) {
      const v = parseInt(e.detail.value, 10)
      this.setData({ retentionDays: isNaN(v) || v < 0 ? 0 : v > 365 ? 365 : v })
    },
    onRetentionHoursMinus() {
      const v = this.data.retentionHours
      if (v > 0) this.setData({ retentionHours: v - 1 })
    },
    onRetentionHoursPlus() {
      const v = this.data.retentionHours
      if (v < 23) this.setData({ retentionHours: v + 1 })
    },
    onRetentionHoursChange(e) {
      const v = parseInt(e.detail.value, 10)
      this.setData({ retentionHours: isNaN(v) || v < 0 ? 0 : v > 23 ? 23 : v })
    },

    onSaveRetention() {
      if (this.data.savingRetention) return   // 防连点
      const token = wx.getStorageSync('token')
      if (!token) return

      const { retentionDays, retentionHours } = this.data
      // 0天0小时 = 永不过期，允许；否则至少保留1小时
      if (retentionDays === 0 && retentionHours === 0) {
        // 允许，表示永不过期
      } else if (retentionDays === 0 && retentionHours < 1) {
        wx.showToast({ title: '至少保留1小时', icon: 'none' })
        return
      }

      this.setData({ savingRetention: true })
      request({
        url: CONFIG.BASE_URL + '/api/admin/storage',
        method: 'POST',
        header: {
          'Authorization': 'Bearer ' + token,
          'content-type': 'application/json',
        },
        data: {
          retention_days: retentionDays,
          retention_hours: retentionHours,
        },
        success: (res) => {
          this.setData({ savingRetention: false })
          if (res.data && res.data.success) {
            wx.showToast({ title: '已同步到服务器和本地工具', icon: 'success' })
            // 刷新统计（保存后会立即清理，文件数可能变化）
            this.loadStorageStats()
          } else {
            wx.showToast({ title: res.data.message || '保存失败', icon: 'none' })
          }
        },
        fail: () => {
          this.setData({ savingRetention: false })
          wx.showToast({ title: '网络错误', icon: 'none' })
        },
      })
    },

    onDeleteAllFiles() {
      const token = wx.getStorageSync('token')
      if (!token) return
      this._showConfirm(
        '⚠️ 确认删除',
        '将删除服务器及本地打印工具的全部缓存文件（不包括用户头像），此操作不可撤销。确定继续？',
        '确认删除',
        '#FF3B30',
        () => {
          this.setData({ deletingAllFiles: true })
          request({
            url: CONFIG.BASE_URL + '/api/admin/storage',
            method: 'DELETE',
            header: { 'Authorization': 'Bearer ' + token },
            success: (res) => {
              this.setData({ deletingAllFiles: false })
              if (res.data && res.data.success) {
                wx.showToast({ title: res.data.message, icon: 'success' })
                this.loadStorageStats()
              } else {
                wx.showToast({ title: (res.data && res.data.message) || '删除失败', icon: 'none' })
              }
            },
            fail: () => {
              this.setData({ deletingAllFiles: false })
              wx.showToast({ title: '网络错误', icon: 'none' })
            }
          })
        }
      )
    },

    // ---- 防滥用（DDoS 防护）阈值设置 ----
    loadSecurityConfig() {
      const token = wx.getStorageSync('token')
      if (!token) return
      request({
        url: CONFIG.BASE_URL + '/api/admin/security',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          if (res.statusCode === 200 && res.data && res.data.success) {
            this.setData({ securityConfig: res.data })
            this._syncSecurityItems(res.data)
          }
        },
      })
    },

    _syncSecurityItems(cfg) {
      const defs = [
        { key: 'user_quota_mb', label: '每用户存储配额', unit: 'MB', min: 0, max: 102400, hint: '超限拒绝上传，0=不限' },
        { key: 'disk_min_free_mb', label: '磁盘最低剩余', unit: 'MB', min: 0, max: 102400, hint: '低于则拒绝上传，0=不检查' },
        { key: 'queued_timeout_hours', label: '排队订单超时', unit: '小时', min: 0, max: 720, hint: '超时自动取消，0=不过期' },
        { key: 'upload_rate_limit', label: '上传频率上限', unit: '次/分', min: 1, max: 600, hint: '每用户每分钟' },
        { key: 'submit_order_rate_limit', label: '提交频率上限', unit: '次/分', min: 1, max: 600, hint: '每用户每分钟' },
        { key: 'device_login_rate_limit', label: '设备注册上限', unit: '次/时', min: 1, max: 600, hint: '每IP/每设备每小时' },
        { key: 'redeem_rate_limit', label: '密钥兑换上限', unit: '次/分', min: 1, max: 600, hint: '每用户每分钟' },
        { key: 'log_report_rate_limit', label: '日志上报上限', unit: '条/分', min: 1, max: 600, hint: '每IP每分钟' },
      ]
      this.setData({
        securityItems: defs.map((d) => ({
          ...d,
          value: cfg[d.key] !== undefined && cfg[d.key] !== null ? Number(cfg[d.key]) : 0,
        })),
      })
    },

    _updateSecurityItem(key, fn) {
      // 路径式 setData（关键修复）：只更新目标项的 value。
      // 之前整体替换 securityItems 数组 → wx:for 整块重渲染 → 渲染层清掉
      // 正在按下按钮的 hover 状态，按压缩放动画被重渲染打断（短按看不到缩小）。
      // 与密钥分钟步进器（onLicenseMinutesMinus 平铺字段 setData）同理，
      // diff 精确到单项，按钮节点路径不变、不被重建，动画得以正常播放。
      const idx = this.data.securityItems.findIndex((it) => it.key === key)
      if (idx < 0) return
      const item = this.data.securityItems[idx]
      let v = fn(item.value)
      if (isNaN(v)) v = item.min
      v = Math.max(item.min, Math.min(item.max, v))
      this.setData({ [`securityItems[${idx}].value`]: v })
    },

    onSecurityMinus(e) {
      const key = e.currentTarget.dataset.key
      this._updateSecurityItem(key, (v) => Number(v) - 1)
    },
    onSecurityPlus(e) {
      const key = e.currentTarget.dataset.key
      this._updateSecurityItem(key, (v) => Number(v) + 1)
    },
    onSecurityInput(e) {
      const key = e.currentTarget.dataset.key
      this._updateSecurityItem(key, () => parseInt(e.detail.value, 10))
    },

    onSaveSecurity() {
      if (this.data.savingSecurity) return   // 防连点
      const token = wx.getStorageSync('token')
      if (!token) return
      const payload = {}
      for (const it of this.data.securityItems) {
        payload[it.key] = it.value
      }
      this.setData({ savingSecurity: true })
      request({
        url: CONFIG.BASE_URL + '/api/admin/security',
        method: 'POST',
        header: {
          'Authorization': 'Bearer ' + token,
          'content-type': 'application/json',
        },
        data: payload,
        success: (res) => {
          this.setData({ savingSecurity: false })
          if (res.data && res.data.success) {
            // 用纯文字 toast：success 图标模式会截断 8 个汉字，漏掉末尾"新"字
            wx.showToast({ title: '防滥用阈值已更新', icon: 'none', duration: 2000 })
            this.loadSecurityConfig()
          } else {
            wx.showToast({ title: res.data.message || '保存失败', icon: 'none' })
          }
        },
        fail: () => {
          this.setData({ savingSecurity: false })
          wx.showToast({ title: '网络错误', icon: 'none' })
        },
      })
    },

    // 展开/收起防滥用设置二级面板
    onToggleSecurity() {
      const expanded = !this.data.securityExpanded
      this.setData({ securityExpanded: expanded })
      // 等展开/收起动画（300ms）结束后再更新滚动边界，避免动画期间强制布局造成卡顿
      setTimeout(() => this._scheduleMeasure(80), 320)
    },

    // 管理员许可密钥轮询：抽屉打开时每 15s 刷新状态（S1-09：自调度 + 失败退避）
    _startKeyPolling() {
      this._stopKeyPolling()
      if (typeof createPollScheduler !== 'function') return   // 同 _startOrderPolling
      if (!this._keyPoll) {
        this._keyPoll = createPollScheduler((done) => this._pollKeysOnce(done), 15000)
      }
      this._keyPoll.start()
    },
    // 一轮 = 许可密钥（必发）+ 管理员列表（超管才发），两者都落定才算结束
    _pollKeysOnce(done) {
      let remaining = this.data.isSuperAdmin ? 2 : 1
      let failed = false
      const fin = (ok) => {
        if (ok === false) failed = true
        remaining -= 1
        if (remaining <= 0) done(!failed)
      }
      this.loadActiveKey(false, fin)
      // 同步刷新管理员列表（头像等）
      if (this.data.isSuperAdmin) {
        this._pollAdminsSilent(fin)
      }
    },

    _stopKeyPolling() {
      if (this._keyPoll) {
        this._keyPoll.stop()
      }
      if (this._keyPollTimer) {
        clearInterval(this._keyPollTimer)
        this._keyPollTimer = null
      }
    },

    // 管理员列表静默轮询（仅更新变化字段，不触发全量渲染）
    _pollAdminsSilent(done) {
      const token = wx.getStorageSync('token')
      if (!token) {
        if (done) done(true)
        return
      }
      request({
        url: CONFIG.BASE_URL + '/api/admin/admins',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        data: { page: 1, page_size: 50 },
        success: (res) => {
          if (!res.data || !res.data.success) {
            if (done) done(false)
            return
          }
          const newAdmins = res.data.admins || []
          const oldAdmins = this.data.admins || []
          // 数量变化 → 全量刷新
          if (newAdmins.length !== oldAdmins.length) {
            this.setData({ admins: newAdmins })
            this._scheduleMeasure()
            return
          }
          // 逐条对比，仅更新变化的字段（avatar/nickname）
          const updates = {}
          let changed = false
          for (let i = 0; i < newAdmins.length; i++) {
            const n = newAdmins[i], o = oldAdmins[i]
            if (!n || !o || n.openid !== o.openid) { changed = true; break }
            if (n.avatar_url !== o.avatar_url) {
              updates['admins[' + i + '].avatar_url'] = n.avatar_url
            }
            if (n.nickname !== o.nickname) {
              updates['admins[' + i + '].nickname'] = n.nickname
            }
          }
          if (changed) {
            this.setData({ admins: newAdmins })
          } else if (Object.keys(updates).length > 0) {
            this.setData(updates)
          }
          if (done) done(true)
        },
        fail: () => {
          if (done) done(false)
        }
      })
    },

    // 临时授权倒计时（访客兑换 temp 密钥后显示）
    _startTempCountdown() {
      this._stopTempCountdown()
      const target = this._parseServerTime(this.data.tempUntil)
      if (!target) {
        this.setData({ tempCountdownText: '' })
        return
      }
      const tick = () => {
        const remain = target - Date.now()
        if (remain > 0) {
          const totalSec = Math.ceil(remain / 1000)
          const m = Math.floor(totalSec / 60)
          const s = totalSec % 60
          this.setData({
            tempCountdownText: '剩余 ' + m + ' 分 ' + (s < 10 ? '0' + s : s) + ' 秒',
          })
        } else {
          this.setData({ tempCountdownText: '已过期' })
          this._stopTempCountdown()
          // 刷新角色（可能已降级为访客）
          this.loadUserRole()
        }
      }
      tick()
      this._tempCountdownTimer = setInterval(tick, 1000)
    },

    _stopTempCountdown() {
      if (this._tempCountdownTimer) {
        clearInterval(this._tempCountdownTimer)
        this._tempCountdownTimer = null
      }
    },

    // ==================== 已临时授权的普通用户 ====================

    loadTempUsers() {
      const token = wx.getStorageSync('token')
      if (!token) return
      this.setData({ tempUsersLoading: true })
      request({
        url: CONFIG.BASE_URL + '/api/admin/temp_users',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          this.setData({ tempUsersLoading: false })
          if (res.data && res.data.success) {
            const users = (res.data.users || []).map(u => ({
              ...u,
              avatarUrl: u.avatar_url ? u.avatar_url + '?t=' + Date.now() : '',
              avatarChar: (u.nickname || '?')[0],
            }))
            this.setData({ tempUsers: users })
            this._scheduleMeasure()
          }
        },
        fail: () => {
          this.setData({ tempUsersLoading: false })
        }
      })
    },

    // 临时用户卡片左滑手势（仅移除，无点击跳转）
    onTempUserTouchStart(e) {
      const openid = e.currentTarget.dataset.openid
      const t = e.touches[0]
      if (!t) return
      this._tempUserSwipeData = this._tempUserSwipeData || {}
      this._tempUserSwipeData[openid] = {
        startX: t.clientX,
        startY: t.clientY,
        lastX: t.clientX,
        startCardX: this.data.tempUserSwipeX[openid] || 0,
        horizontal: false,
        moved: false,
      }
      const trans = { ...this.data.tempUserSwipeTransition }
      trans[openid] = false
      this.setData({ tempUserSwipeTransition: trans })
    },

    onTempUserTouchMove(e) {
      const openid = e.currentTarget.dataset.openid
      const sd = this._tempUserSwipeData && this._tempUserSwipeData[openid]
      if (!sd) return
      const t = e.touches[0]
      if (!t) return
      const dx = t.clientX - sd.startX
      const dy = t.clientY - sd.startY
      if (!sd.horizontal) {
        if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return
        sd.moved = true
        if (Math.abs(dx) > Math.abs(dy)) {
          sd.horizontal = true
          // 通知滚动引擎让出控制权
          this._swipeHorizontal = true
        } else {
          return
        }
      }
      sd.moved = true
      const rawX = sd.startCardX + dx
      const maxX = -this._adminDeleteWidthPx
      const visualX = this._rubberBand(rawX, maxX, 0, 55)
      sd.lastX = rawX
      sd.lastVisualX = visualX
      const swipeX = { ...this.data.tempUserSwipeX }
      swipeX[openid] = visualX
      const opacity = { ...this.data.tempUserDeleteOpacity }
      opacity[openid] = rawX < 0 ? Math.min(1, Math.abs(rawX) / (this._adminDeleteWidthPx * 0.6)) : 0
      this.setData({ tempUserSwipeX: swipeX, tempUserDeleteOpacity: opacity })
    },

    onTempUserTouchEnd(e) {
      this._swipeHorizontal = false
      const openid = e.currentTarget.dataset.openid
      const sd = this._tempUserSwipeData && this._tempUserSwipeData[openid]
      if (!sd) return
      if (!sd.moved || !sd.horizontal) return  // 纯点击/垂直滚动：无跳转
      const trans = { ...this.data.tempUserSwipeTransition }
      trans[openid] = true
      const maxX = -this._adminDeleteWidthPx
      const rawX = sd.lastX
      const target = rawX > 0 ? 0 : (rawX < maxX ? maxX : (rawX < maxX / 2 ? maxX : 0))
      const opacity = { ...this.data.tempUserDeleteOpacity }
      opacity[openid] = target === 0 ? 0 : 1
      this.setData({ tempUserSwipeTransition: trans, tempUserDeleteOpacity: opacity })
      wx.nextTick(() => {
        const swipeX = { ...this.data.tempUserSwipeX }
        swipeX[openid] = target
        this.setData({ tempUserSwipeX: swipeX })
        const capturedOpenid = openid
        setTimeout(() => {
          const cleanTrans = { ...this.data.tempUserSwipeTransition }
          delete cleanTrans[capturedOpenid]
          this.setData({ tempUserSwipeTransition: cleanTrans })
        }, 300)
      })
    },

    onRemoveTempUser(e) {
      const openid = e.currentTarget.dataset.openid
      const token = wx.getStorageSync('token')
      if (!token) return
      this._showConfirm('移除用户', '确定要移除该临时授权用户吗？移除后其授权记录将保留在历史授权用户中。', '移除', '#ff4d4f', () => {
        request({
          url: CONFIG.BASE_URL + '/api/admin/remove_user',
          method: 'POST',
          header: { 'Authorization': 'Bearer ' + token, 'content-type': 'application/json' },
          data: { openid: openid },
          success: (res) => {
            if (res.data && res.data.success) {
              wx.showToast({ title: '已移除', icon: 'success' })
              // 滑回收起 → 淡出 → 移除
              const swipeX = { ...this.data.tempUserSwipeX }
              swipeX[openid] = 0
              const trans = { ...this.data.tempUserSwipeTransition }
              trans[openid] = true
              this.setData({ tempUserSwipeX: swipeX, tempUserSwipeTransition: trans })
              setTimeout(() => {
                const users = this.data.tempUsers.map(u =>
                  u.openid === openid ? { ...u, _exiting: true } : u
                )
                this.setData({ tempUsers: users })
                setTimeout(() => {
                  this.setData({ tempUsers: this.data.tempUsers.filter(u => u.openid !== openid) })
                  this._scheduleMeasure()
                }, 350)
              }, 180)
            } else {
              wx.showToast({ title: (res.data && res.data.message) || '移除失败', icon: 'none' })
            }
          },
          fail: () => {
            wx.showToast({ title: '网络错误', icon: 'none' })
          }
        })
      })
    },

    // ==================== 访客：兑换许可密钥 ====================

    onRedeemKeyInput(e) {
      this.setData({ redeemKey: e.detail.value })
    },

    // 从剪贴板自动提取密钥
    onPasteKey() {
      wx.getClipboardData({
        success: (res) => {
          const text = (res.data || '').trim()
          if (!text) {
            wx.showToast({ title: '剪贴板为空', icon: 'none', duration: 1500 })
            return
          }
          // 尝试匹配 "密钥: XXXXXXXX" 格式
          const match = text.match(/密钥[：:]\s*([A-Za-z0-9]{8})/i)
          if (match) {
            this.setData({ redeemKey: match[1].toUpperCase() })
            wx.showToast({ title: '已自动填入', icon: 'success', duration: 1200 })
          } else {
            // 回退：查找任意 8 位字母数字串
            const fallback = text.match(/\b([A-Za-z0-9]{8})\b/)
            if (fallback) {
              this.setData({ redeemKey: fallback[1].toUpperCase() })
              wx.showToast({ title: '已自动填入', icon: 'success', duration: 1200 })
            } else {
              wx.showToast({ title: '未识别到密钥', icon: 'none', duration: 1500 })
            }
          }
        },
        fail: () => {
          wx.showToast({ title: '无法读取剪贴板', icon: 'none', duration: 1500 })
        }
      })
    },

    onRedeemKey() {
      const key = (this.data.redeemKey || '').trim().toUpperCase()
      if (!key) {
        wx.showToast({ title: '请输入许可密钥', icon: 'none' })
        return
      }
      if (key.length !== 8) {
        wx.showToast({ title: '密钥为8位字符', icon: 'none' })
        return
      }

      const token = wx.getStorageSync('token')
      if (!token) return

      this.setData({ redeeming: true })
      request({
        url: CONFIG.BASE_URL + '/api/license/redeem',
        method: 'POST',
        header: {
          'Authorization': 'Bearer ' + token,
          'content-type': 'application/json'
        },
        data: { key: key },
        success: (res) => {
          this.setData({ redeeming: false })
          if (res.data.success) {
            wx.showToast({ title: '许可验证成功！', icon: 'success' })
            // 重新加载角色，刷新 UI（内部会触发 _scheduleMeasure 和倒计时）
            this.loadUserRole()
            this.setData({ redeemKey: '' })
            this._scheduleMeasure()
          } else {
            wx.showToast({ title: res.data.message || '密钥无效', icon: 'none', duration: 2000 })
          }
        },
        fail: () => {
          this.setData({ redeeming: false })
          wx.showToast({ title: '网络错误', icon: 'none' })
        }
      })
    },

    // ==================== 任务列表 ====================

    loadOrders(cb) {
      const token = wx.getStorageSync('token')
      if (!token) {
        this.setData({ loading: false })
        return
      }

      this.setData({ loadError: '' })
      // 仅在列表为空时显示加载状态（首次加载），切换页面/条数时静默刷新
      if (this.data.orders.length === 0) {
        this.setData({ loading: true })
      }

      request({
        url: CONFIG.BASE_URL + '/api/orders',
        method: 'GET',
        header: {
          'Authorization': 'Bearer ' + token
        },
        data: { page: this.data.ordersCurrentPage, per_page: this.data.ordersPerPage },
        success: (res) => {
          if (res.statusCode === 200 && res.data.success) {
            // 预处理文件大小显示（WXML 不支持 .toFixed()）
            const newOrders = (res.data.orders || [])
            newOrders.forEach(order => {
              _normalizeOrderRow(order)
              // #9：终态非 sent 的订单，状态文字可点 → 原因气泡。
              // 后端未升级时没有 status_reason/manual_status_* 字段 → 走通用文案，入口仍然可点。
              order.statusReasonTappable = REASON_STATUSES.indexOf(order.status) >= 0
              order.statusReasonText = this._statusReasonText(order)
            })

            const total = res.data.total || 0
            const firstLoad = this.data.orders.length === 0 && newOrders.length > 0
            this.setData({
              orders: newOrders,
              loading: false,
              loadError: '',
              ordersTotal: total,
              ordersTotalPages: Math.ceil(total / this.data.ordersPerPage),
              expandedOrders: {},
              showPricePop: false,
              ordersAnimated: firstLoad,
            }, () => {
              this._syncOrdersPageNumbers()
              this._scheduleMeasure()
              if (typeof cb === 'function') cb()
            })
            if (firstLoad) {
              setTimeout(() => this.setData({ ordersAnimated: false }), newOrders.length * 60 + 500)
            }
          } else {
            // 页面内状态提示（对齐 Android App）：不清空已加载列表
            this.setData({ loading: false, loadError: res.data && res.data.message ? res.data.message : '加载失败' })
            this._scheduleMeasure()
            if (typeof cb === 'function') cb()
          }
        },
        fail: (err) => {
          console.error('[loadOrders] 网络请求失败:', err)
          this.setData({ loading: false, loadError: '网络错误' })
          this._scheduleMeasure()
          if (typeof cb === 'function') cb()
        }
      })
    },

    // ==================== 分页控制 ====================

    onOrdersPageChange(e) {
      const page = parseInt(e.currentTarget.dataset.page, 10)
      if (page < 1 || page > this.data.ordersTotalPages || page === this.data.ordersCurrentPage) return
      this.setData({ ordersCurrentPage: page }, () => {
        this._syncOrdersPageNumbers()
        this.loadOrders(() => this._scrollToOrdersSection())
      })
    },

    onOrdersPrevPage() {
      if (this.data.ordersCurrentPage <= 1) return
      this.setData({ ordersCurrentPage: this.data.ordersCurrentPage - 1 }, () => {
        this._syncOrdersPageNumbers()
        this.loadOrders(() => this._scrollToOrdersSection())
      })
    },

    onOrdersNextPage() {
      if (this.data.ordersCurrentPage >= this.data.ordersTotalPages) return
      this.setData({ ordersCurrentPage: this.data.ordersCurrentPage + 1 }, () => {
        this._syncOrdersPageNumbers()
        this.loadOrders(() => this._scrollToOrdersSection())
      })
    },

    onToggleOrdersPageSize() {
      if (this.data.showPageSizePicker) {
        this.setData({ showPageSizePicker: false })
        return
      }
      // 计算选择器在视口中的位置，下拉以 fixed 坐标弹出（page-root 外，逃出变换层）
      const q = this.createSelectorQuery()
      q.select('.page-size-selector').boundingClientRect()
      q.exec((res) => {
        const rect = res && res[0]
        if (!rect) {
          this.setData({ showPageSizePicker: true })
          return
        }
        const winH = (wx.getWindowInfo ? wx.getWindowInfo().windowHeight : 0) || 600
        const dropH = 250   // 4 个选项的估算高度
        const left = rect.left
        const top = rect.top + rect.height + 3
        this.setData({
          pageSizeDropdownLeft: left,
          pageSizeDropdownTop: (top + dropH > winH) ? Math.max(8, rect.top - dropH - 3) : top,
          showPageSizePicker: true,
        })
      })
    },

    onSelectOrdersPageSize(e) {
      const size = parseInt(e.currentTarget.dataset.size, 10)
      if (isNaN(size)) return
      this.setData({
        ordersPerPage: size,
        ordersCurrentPage: 1,
        showPageSizePicker: false,
      }, () => {
        this._syncOrdersPageNumbers()
        this.loadOrders(() => this._scrollToOrdersSection())
      })
    },

    // 滚动到"我的打印任务"区域而非页面顶部。
    // 由 WXS 引擎在视图层测量区块位置并动画（内容坐标 = 区块可视位置 - 容器可视位置 + 当前滚动量）。
    _scrollToOrdersSection() {
      this.setData({ scrollCmd: { mode: 'section', section: '.orders-section', offset: -20, dur: 280 } })
    },

    // 橡皮筋阻尼：允许越界滑动，阻力渐增，松手回弹
    _rubberBand(rawVal, minVal, maxVal, maxOverscroll = 60) {
      if (rawVal > maxVal) {
        const excess = rawVal - maxVal
        return maxVal + maxOverscroll * (1 - Math.exp(-excess / (maxOverscroll * 1.6)))
      }
      if (rawVal < minVal) {
        const excess = minVal - rawVal
        return minVal - maxOverscroll * (1 - Math.exp(-excess / (maxOverscroll * 1.6)))
      }
      return rawVal
    },

    // 生成页码数组
    getOrdersPageNumbers() {
      const total = this.data.ordersTotalPages
      const current = this.data.ordersCurrentPage
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
    _syncOrdersPageNumbers() {
      const pages = this.getOrdersPageNumbers()
      const prev = this.data.ordersPageNumbers || []
      if (pages.length !== prev.length || pages.some((p, i) => p !== prev[i])) {
        this.setData({ ordersPageNumbers: pages })
      }
    },

    onOrderTap(e) {
      const orderId = e.currentTarget.dataset.id
      // 切换展开/收起
      const expanded = { ...this.data.expandedOrders }
      if (expanded[orderId]) {
        delete expanded[orderId]
      } else {
        expanded[orderId] = true
      }
      this.setData({ expandedOrders: expanded, showPricePop: false })
      // 详情展开/收起有 250ms 动画：过渡中途先跟一版，动画结束后再测最终值
      this._scheduleMeasure()
      setTimeout(() => this._scheduleMeasure(120), 120)
      setTimeout(() => this._scheduleMeasure(300), 300)
    },

    // ==================== 管理员：「近 N 条订单」 ====================
    // 云端对所有管理员公开最近 N 条订单（N 由超管设置，默认 10；以后端返回的 limit 为准）。
    // 非管理员不渲染、不发请求；进入页面拉一次（15s 内不重复拉，避免切 tab 反复请求触发限流）；
    // 失败静默保留上次数据（不弹错误遮罩），标题右侧「刷新」可手动重拉。

    _normalizeRecentOrders(orders, limit) {
      const list = Array.isArray(orders) ? orders : []
      const cap = Math.max(1, Math.min(100, Number(limit) || 10))
      return list.slice(0, cap).map((o) => {
        const src = o || {}
        const total = Math.round((Number(src.total_price) || 0) * 100) / 100
        const files = Number(src.file_count) || 0
        const pages = Number(src.page_count) || 0
        return {
          order_id: src.order_id,
          order_number: src.order_number || '',
          owner: src.owner || '',
          // 归属人默认脱敏，点一下看完整昵称（owner）
          ownerText: src.owner_masked || src.owner || '—',
          created_at: src.created_at || '',
          file_count: files,
          page_count: pages,
          statText: files + ' 文件 · ' + pages + ' 页',
          total_price: total,
          priceDisplay: total.toFixed(2),
          status: src.status || '',
          statusText: this.data.statusMap[src.status] || src.status || '—',
          source: src.source || '',
          is_admin_print: !!src.is_admin_print,
        }
      })
    },

    loadRecentOrders(force) {
      // 只有管理员能看到这张卡片：非管理员连请求都不发（后端也会 403）
      if (this.data.userRole !== 'admin' && wx.getStorageSync('userRole') !== 'admin') return
      const token = wx.getStorageSync('token')
      if (!token) return
      const now = Date.now()
      if (!force && this._recentOrdersAt && (now - this._recentOrdersAt) < 15000) return
      if (this._recentOrdersLoading) return
      this._recentOrdersLoading = true
      if (!this.data.recentOrdersLoading) this.setData({ recentOrdersLoading: true })
      request({
        url: CONFIG.BASE_URL + '/api/admin/recent_orders',
        method: 'GET',
        header: {
          'Authorization': 'Bearer ' + token
        },
        success: (res) => {
          const d = (res && res.data) || {}
          if (res && res.statusCode === 200 && d.success) {
            this._recentOrdersAt = Date.now()
            const limit = Math.max(1, Math.min(100, parseInt(d.limit, 10) || 10))
            this.setData({
              recentOrders: this._normalizeRecentOrders(d.orders, limit),
              recentOrdersLimit: limit,
              recentOrdersLoading: false,
            }, () => this._scheduleMeasure(60))
            return
          }
          // 静默失败：保留上次数据，不弹错误遮罩
          this.setData({ recentOrdersLoading: false })
        },
        fail: () => {
          this.setData({ recentOrdersLoading: false })
        },
        complete: () => {
          this._recentOrdersLoading = false
        },
      })
    },

    // #3：最近订单改成独立页（分页列表在新页面里拉；本页只保留条数与「⚙ 设置」入口）
    onOpenRecentOrders() {
      // #2：必须走带返回标记的导航 —— 否则回到本页时读到上次的 _tabFrom===0，
      // 会播「从右滑入」的 page-enter-right；这里和其它二级页一样只做 fade。
      this._navigateWithAnimation('/pages/recent-orders/recent-orders')
    },

    onRefreshRecentOrders() {
      if (this.data.recentOrdersLoading) return
      this.loadRecentOrders(true)
    },

    // 归属人：列表里显示脱敏（owner_masked），点一下看完整昵称
    onRecentOrderOwnerTap(e) {
      const ds = (e && e.currentTarget && e.currentTarget.dataset) || {}
      const full = ds.owner || ''
      if (!full) {
        wx.showToast({ title: '该订单未记录归属人', icon: 'none' })
        return
      }
      wx.showModal({ title: '订单归属人', content: full, showCancel: false, confirmText: '知道了' })
    },

    // ---- 超管专属：改「近 N 条订单」的 N ----
    // 后端 compute_role() 只返回 admin/user/guest，超管被折叠成 admin；
    // 超管身份由 /api/me 的 isSuperAdmin 标记区分（**不要**判 userRole === 'super_admin'）。
    // 写接口只允许超管：POST /api/admin/recent_orders_limit {"limit": n}（1..100）。
    _canEditRecentLimit() {
      return this.data.userRole === 'admin' && this.data.isSuperAdmin === true
    },

    onEditRecentOrdersLimit() {
      if (!this._canEditRecentLimit()) return
      const open = !this.data.recentLimitEditor
      this.setData({
        recentLimitEditor: open,
        recentLimitDraft: open ? String(this.data.recentOrdersLimit || 10) : '',
      })
    },

    onRecentOrdersLimitInput(e) {
      const raw = (e && e.detail && e.detail.value != null) ? String(e.detail.value) : ''
      // 只留数字：type="number" 在部分机型仍会带小数点/空格
      this.setData({ recentLimitDraft: raw.replace(/[^0-9]/g, '') })
    },

    onRecentOrdersLimitStep(e) {
      const ds = (e && e.currentTarget && e.currentTarget.dataset) || {}
      const delta = Number(ds.delta) === -1 ? -1 : 1
      const cur = parseInt(this.data.recentLimitDraft, 10)
      const base = isNaN(cur) ? (this.data.recentOrdersLimit || 10) : cur
      const next = Math.max(1, Math.min(100, base + delta))
      this.setData({ recentLimitDraft: String(next) })
    },

    onSaveRecentOrdersLimit() {
      if (!this._canEditRecentLimit()) return
      if (this.data.recentLimitSaving) return
      const n = parseInt(this.data.recentLimitDraft, 10)
      if (isNaN(n) || n < 1 || n > 100) {
        wx.showToast({ title: '请输入 1~100 的整数', icon: 'none' })
        return
      }
      const token = wx.getStorageSync('token')
      if (!token) return
      this.setData({ recentLimitSaving: true })
      request({
        url: CONFIG.BASE_URL + '/api/admin/recent_orders_limit',
        method: 'POST',
        header: {
          'Content-Type': 'application/json',
          'Authorization': 'Bearer ' + token
        },
        data: { limit: n },
        success: (res) => {
          const d = (res && res.data) || {}
          if (res && res.statusCode === 200 && d.success) {
            const limit = Math.max(1, Math.min(100, parseInt(d.limit, 10) || n))
            this.setData({
              recentOrdersLimit: limit,
              recentLimitEditor: false,
              recentLimitDraft: '',
              recentLimitSaving: false,
            }, () => this._scheduleMeasure(60))
            wx.showToast({ title: d.message || ('已更新为最近 ' + limit + ' 条'), icon: 'none' })
            // 标题立刻跟着变；列表按新 N 重拉（force 跳过 15s 去重）
            this.loadRecentOrders(true)
            return
          }
          // 失败保持旧值：只轻提示，不弹遮罩
          this.setData({ recentLimitSaving: false })
          wx.showToast({ title: '保存失败，请稍后重试', icon: 'none' })
        },
        fail: () => {
          this.setData({ recentLimitSaving: false })
          wx.showToast({ title: '网络错误，保存失败', icon: 'none' })
        },
      })
    },

    onCancelRecentOrdersLimit() {
      this.setData({ recentLimitEditor: false, recentLimitDraft: '' })
    },

    // ==================== 合计价格复制（Popconfirm 气泡） ====================
    // 金额一律取订单**已定稿**的实收值（orders.total_price / order_files.total_price）：
    // 设备回报页数后后端会用 _recalc_prices_for_file 重算并回写这两处，前端不再自己套单价，
    // 复制出去的就是页面上看到的数字（避免「复制价 ≠ 实收价」）。

    _findOrderById(id) {
      const key = String(id == null ? '' : id)
      // 「近 N 条订单」行：data-id 带 r- 前缀 —— 这些订单不一定落在当前分页的 orders 里
      if (key.indexOf('r-') === 0) {
        const oid = key.slice(2)
        const recent = this.data.recentOrders || []
        for (let i = 0; i < recent.length; i++) {
          if (recent[i] && String(recent[i].order_id) === oid) return recent[i]
        }
        return null
      }
      const list = this.data.orders || []
      for (let i = 0; i < list.length; i++) {
        const o = list[i]
        if (o && String(o.id) === String(id)) return o
      }
      return null
    },

    _orderTotal(o) {
      return Math.round((Number(o && o.total_price) || 0) * 100) / 100
    },

    _copyText(text, toastTitle) {
      wx.setClipboardData({
        data: text,
        success: () => wx.showToast({ title: toastTitle, icon: 'success' }),
        fail: () => wx.showToast({ title: '复制失败', icon: 'none' }),
      })
    },

    /* ==================== #9 异常状态原因气泡 ==================== */

    // 原因文本：优先「人工订正记录」（谁在什么时候把状态改成什么），其次后端给的原因，
    // 都没有才用通用文案 —— 气泡永远有内容可复制，不会弹出空白框。
    _statusReasonText(o) {
      if (!o) return REASON_FALLBACK_TEXT
      const by = String(o.manual_status_by || '').trim()
      const label = (this.data.statusMap && this.data.statusMap[o.status]) || o.status || '异常状态'
      if (by) {
        // 与「最近订单」页 / settlement.html 同一口径：缺时间写「未知时间」，不出现破折号
        const at = _fmtStatusTime(o.manual_status_at) || '未知时间'
        return by + '在' + at + '被标记为' + label
      }
      const reason = String(o.status_reason || '').trim()
      return reason || REASON_FALLBACK_TEXT
    },

    // 点击异常状态文字 → 在状态处弹原因气泡（再点一次收起）。
    // 与价格气泡同一套：page-root 外 + position:fixed（订单卡片 overflow:hidden 会裁、
    // .scroll-content 的 translateY 会让里面的 fixed 相对变换层定位）+ 二次探测翻到下方。
    onStatusReasonTap(e) {
      const id = e.currentTarget.dataset.id
      if (this.data.showStatusPop && String(this.data.statusPopOrderId) === String(id)) {
        this.setData({ showStatusPop: false })
        return
      }
      const o = this._findOrderById(id)
      if (!o) return
      const text = this._statusReasonText(o)
      const win = (typeof wx.getWindowInfo === 'function' ? wx.getWindowInfo() : null) || {}
      const winW = win.windowWidth || 375
      const winH = win.windowHeight || 667
      // 原因可能很长：宽度给到视口八成半，高度交给 CSS 换行/滚动
      const popW = Math.min(Math.round(winW * 0.86), winW - 24)
      const q = this.createSelectorQuery()
      q.select('#status-tap-' + id).boundingClientRect()
      q.exec((res) => {
        const rect = res && res[0]
        if (!rect) return
        // 水平：气泡中心对齐状态文字中心，再夹在视口内；箭头指向文字中心
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
          // 同一时刻只留一个气泡
          showPricePop: false,
        }, () => {
          const q2 = this.createSelectorQuery()
          q2.select('.status-pop').boundingClientRect()
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

    onCopyStatusReason() {
      const text = this.data.statusPopText || ''
      this.setData({ showStatusPop: false })
      if (!text) return
      this._copyText(text, '已复制原因')
    },

    /* ==================== #5 价格复制（文案口径在 utils/price_copy.js） ==================== */
    // 文案与算式**只有一份实现**：`utils/price_copy.js`（「我」页与 user-orders 子视图共用同一份）。
    // 页面这边只留：单价取值的进程内缓存、订单对象 → 文本、剪贴板调用。
    //   简略：`{订单号} — ¥{金额}`（= 本地工具 Ctrl+C）
    //   详细：订单号 / -×14 / 计费明细 / -×14 / 每文件 3 行 / [附加服务费] / -×14 / 💰合计
    // 金额一律取**实收**（total_price），算式只用来解释它。

    _withPriceRules(cb) {
      _getPriceRules(cb)
    },

    _priceLine(o) {
      return priceCopy.priceLine(o)
    },

    _priceDetailLines(o, rules) {
      return priceCopy.priceDetailLines(o, rules)
    },

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
          // 「近 N 条订单」行没有逐文件明细 → 气泡里只留「复制价格」
          pricePopRecent: String(id).indexOf('r-') === 0,
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
      const o = this._findOrderById(e.currentTarget.dataset.id)
      this.setData({ showPricePop: false })
      if (!o) return
      this._copyText(this._priceLine(o), '已复制价格')
    },

    // 复制详细价格（逐文件 + 附加服务费 + 合计，与本地打印工具 Ctrl+Shift+C 同格式）
    onCopyOrderDetailPrice(e) {
      const o = this._findOrderById(e.currentTarget.dataset.id)
      this.setData({ showPricePop: false })
      if (!o) return
      this._withPriceRules((rules) => {
        this._copyText(this._priceDetailLines(o, rules).join(String.fromCharCode(10)), '已复制详细价格')
      })
    },

    onDetailCancelOrder(e) {
      const orderId = e.currentTarget.dataset.id
      const token = wx.getStorageSync('token')
      if (!token || this.data.isCanceling) return
      this._showConfirm('确认取消', '确定要取消这个打印任务吗？', '取消订单', '#FF9500', () => {
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
              this.loadOrders()
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
      })
    },

    // ==================== 主题切换（自动/浅色/深色 三态循环）====================

    onToggleTheme() {
      const app = getApp()
      const result = app.toggleTheme()
      this.setData({
        isDarkMode: result.isDarkMode,
        themeMode: result.themeMode,
      })
    },

    // 获取当前模式对应的图标文字
    getThemeIcon() {
      const mode = this.data.themeMode
      if (mode === 'auto') return '🌓'
      if (mode === 'dark') return '🌙'
      return '☀️'
    },

  },
})
