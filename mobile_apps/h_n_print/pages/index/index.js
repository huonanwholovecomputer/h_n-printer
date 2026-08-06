// index.js
const { CONFIG } = require('../../utils/config')
const { request } = require('../../utils/request')

// 文件卡片每类型高度（rpx，由 _probeCardHeights() 实测后硬编码；默认值为按 CSS 估算的占位）：
//   image = 仅份数控件；word = 份数+范围+双面+页数分析条；
//   word-single = 单页 Word/PDF 仅份数（范围/模式两行隐藏后收缩）；
//   excel = 无控件仅状态行
// 配合「上传时即显示完整卡片」策略，卡片高度按类型恒定，故列表高度可同步求和计算、无需异步实测。
// 注意：单页文档的 word-single 高度是动态的——pageCount 异步确认=1 后由 _recalcFileListHeight() 重算列表高度。
const FILE_CARD_HEIGHT_RPX = {
  image: 312.8,           // 实测(2026-08-05): 仅份数+方向
  'word-grid': 451.1,     // 估算(待探测): 页数已知→份数+范围(网格摘要按钮)+模式+页数条
  'word-grid-single': 385.1, // 估算(待探测): 页数已知且单选→份数+范围(摘要按钮)，无模式
  'word-text': 477.1,     // 估算(待探测): 页数未知→份数+范围(警告行+多行输入)+模式+页数条
  'word-text-single': 411.1, // 估算(待探测): 页数未知且单选→份数+范围(警告行+输入)，无模式
  'word-single': 311.0,   // 实测(2026-08-06): 整份1页→仅份数+页数条
  excel: 151.2,           // 实测(2026-08-05): 仅状态行
}
// .file-card 底部间距 margin-bottom: 12rpx
const FILE_CARD_GAP_RPX = 12
// 文本模式(页数未知)下，每多一个范围行在基础高度上额外增加的高度(rpx)：输入框56 + 行间距4
const RANGE_LINE_EXTRA_RPX = 60

function _fileCardTypeKey(file) {
  if (file.excelWarning) return 'excel'
  if (file.isImage) return 'image'
  // 整份 1 页：范围/模式两行均隐藏，卡片收缩为"仅份数"（word-single）
  if (file.pageCount === 1) return 'word-single'
  const single = !!file.singlePage   // 有效范围恰好选中 1 页 → 模式行隐藏
  const known = file.pageCountStatus === 'confirmed' && file.pageCount > 0
  if (known) return single ? 'word-grid-single' : 'word-grid'
  return single ? 'word-text-single' : 'word-text'
}

// 文件卡片实际高度（rpx）：基础类型高度 + 文本模式每多一个范围行的增量。
// 仅文本模式(页数未知)按行数累加；网格模式/整份1页/图片/Excel 的 rangeLines 不渲染为独立行，不计增量。
function _fileCardHeightRpx(file) {
  const base = FILE_CARD_HEIGHT_RPX[_fileCardTypeKey(file)] || 0
  const textMode = !file.excelWarning && !file.isImage && file.pageCount !== 1 &&
    !(file.pageCountStatus === 'confirmed' && file.pageCount > 0)
  if (!textMode) return base
  const extra = (file.rangeLines || []).length - 1
  return extra > 0 ? base + extra * RANGE_LINE_EXTRA_RPX : base
}

// 时间滚轮（picker-view）：item 高 88rpx、可视区 440rpx 由 WXSS 定义
// （picker-view 的贴合单元 = column 内 item 高度，无需 JS 参与几何计算）

// 初始主题值：直接从 app.globalData 读取（同步、无延迟），绕过 storage 的异步写窗口
// 避免首次创建组件时首帧使用过期值触发 CSS transition 闪烁
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

Component({
  data: {
    // 多文件列表：每项 { name, size, path, fileId, uploading, progress, failed, copies }
    selectedFiles: [],
    duplex: 'on',
    printerActive: false,
    showSuccessModal: false,
    showAccessDeniedModal: false,
    showPageCountWarning: false,   // 页数未验证警告弹窗
    showUnsupportedSkipModal: false,  // 存在不支持格式（Excel/PPT/CAD）→ 确认自动跳过的弹窗
    unsupportedSkipCount: 0,
    modalClosing: false,           // 弹窗关闭动画进行中
    userRole: '',
    submitting: false,
    autoPrintEnabled: false,   // 无障碍打印开关（仅管理员可见）
    autoPrintGlow: false,      // ⚡ 闪电发光特效
    glowPhase: '',             // 光晕阶段: ''（无）| 'striking'（爆发）| 'fading'（渐隐）| 'reset'（强制清除）
    glowStyle: '',             // 内联 text-shadow，JS 逐帧控制渐隐
    // 无障碍打印预约（仅管理员）：now=立即 / at=指定时间 / countdown=倒计时
    scheduleMode: 'now',
    scheduleDays: ['今天', '明天', '后天'],   // 动态生成：今天(周一)/明天(周二)/后天(周三)
    scheduleDayIndex: 0,
    scheduleVisible: false,    // 开始方式面板是否渲染（收起动画结束后卸载）
    scheduleAnim: 'collapsed', // 展开/收起过渡状态：collapsed | expanded
    scheduleOptionsVisible: false,    // 模式选项行（指定时间/倒计时）是否渲染（切换模式时收起动画结束后换行）
    scheduleOptionsAnim: 'collapsed', // 选项行展开/收起过渡状态：collapsed | expanded
    // 自定义玻璃选择弹窗（替代微信原生 picker：日期列表 + 时间双列滚轮）
    showScheduleDayPicker: false,     // 日期选择弹窗
    showScheduleTimePicker: false,    // 时间选择弹窗
    hourItems: Array.from({ length: 24 }, (_, i) => String(i).padStart(2, '0')),
    minuteItems: Array.from({ length: 60 }, (_, i) => String(i).padStart(2, '0')),
    hourWheelIndex: 8,                // 时滚轮选中索引（默认 08:00）
    minuteWheelIndex: 0,
    timeWheelValue: [8, 0],           // picker-view 选中索引数组（原生定位/贴合）
    curHour: 0,                       // 打开弹窗时的当前时刻（今天裁剪已过时间用）
    curMin: 0,
    hourStart: 0,                     // 今天：小时列起始小时（curMin=59 时为 curHour+1）
    minuteStart: 0,                   // 今天：当前小时的分钟列起始分钟（curMin+1）
    scheduleTime: '',
    countdownMin: 5,
    countdownSec: 0,
    // 倒计时选择弹窗（复用时间滚轮：分 + 秒，范围 00-59）
    showScheduleCountdownPicker: false,
    countdownMinItems: Array.from({ length: 60 }, (_, i) => String(i).padStart(2, '0')),
    countdownSecItems: Array.from({ length: 60 }, (_, i) => String(i).padStart(2, '0')),
    countdownMinWheelIndex: 5,
    countdownSecWheelIndex: 0,
    countdownWheelValue: [5, 0],
    logoScale: 1,
    logoPadding: 40,
    scrollTop: 0,
    // v5: 附加服务参数（与本地打印工具对齐）
    deliveryEnabled: false,
    deliveryLocation: '1号楼北楼',
    deliveryLocations: ['1号楼北楼', '1号楼南楼', '图书馆', '教学楼E/F', '女生宿舍'],
    deliveryPercentages: { '1号楼北楼': 0, '1号楼南楼': 5, '图书馆': 15, '教学楼E/F': 20, '女生宿舍': 10 },
    deliveryPercent: 0,
    urgency: '低',
    urgencyOptions: ['低', '中', '高'],
    urgencyPrices: { '低': 0, '中': 0.08, '高': 0.15 },
    urgencyPrice: 0,
    coverPage: false,
    coverPagePrice: 0.10,  // 与本地打印工具 print_config.json 保持一致
    coverPriceVisible: false,    // 首页价格标签是否可见（配合入场动画）
    coverPriceEntering: false,   // 首页价格入场动画
    coverPriceExiting: false,    // 首页价格退场动画
    pickupAddress: '1号楼202宿舍',
    showDeliveryPicker: false,
    showUrgencyPicker: false,
    lastOrderNumber: '',
    lastScheduleText: '',   // 无障碍打印预约成功提示文案（成功弹窗显示）
    badgeEntering: false,   // 圆点入场动画（首文件）
    badgeBouncing: false,   // 圆点弹跳动画（已有文件时新增）
    badgeExiting: false,    // 圆点退场动画（末文件删除）
    badgeCount: 0,          // 延迟更新的计数，统一 0.25s 滞后于 selectedFiles.length
    scrollPadHeight: 0,     // 滚动垫片高度，强制撑开 scroll-content 规避微信布局上限
    fileListHeight: 0,      // 文件列表显式高度（随内容增长至上限后锁定并内部滚动）
    btnPulse: false,        // 添加按钮脉冲动画（文字变化时）
    pageReady: false,         // 首次打开的入场动效
    pageExit: '',             // 退出动画: page-exit-left / page-exit-right
    pageSlide: 'page-init',   // 入场动画: page-fade-in / page-enter-left（初始隐藏防闪烁）
    isDarkMode: _initIsDark(),
    themeMode: _initThemeMode(),
    themeSwitching: false,       // 主题切换过渡中
    // 页数网格选择弹窗（页数已知时，范围控件点击弹出）
    showRangePicker: false,        // 弹窗可见
    rangePickerClosing: false,     // 弹窗关闭动画
    rangePickerFileIndex: -1,      // 正在编辑的文件下标
    rangePickerTotal: 0,           // 文档总页数
    rangePickerPages: [],          // [{n, sel}] 1..N 网格数据
    rangePickerSelAll: false,      // 一键"全部"选中态
    rangePickerSelOdd: false,      // 一键"单页(奇)"选中态
    rangePickerSelEven: false,     // 一键"双页(偶)"选中态
    entranceDelay: {           // 打印页元素入场延时（首次加载：基础 0.5s，从上到下逐个递增 0.1s）
      logo: '0.5s',
      statusBar: '0.6s',
      fileSection: '0.7s',
      extParams: '0.8s',
      autoPrint: '0.9s',
      submit: '1.0s',
    },
  },
  lifetimes: {
    attached() {
      // 注册到全局页面实例池，供主题切换时同步缓存页
      try { const r = getApp().globalData._pageRegistry; if (r && !r.includes(this)) r.push(this) } catch(e) {}
      // 首帧前直接覆写数据对象，绕过 setData 异步延迟
      this.data.isDarkMode = getApp().globalData.isDarkMode
      // 首次启动清理残留的转场标记，防止热重载/缓存触发误动画
      wx.removeStorageSync('_tabFrom')
      wx.removeStorageSync('_tabTo')
      // 从 app.globalData 同步主题，防止闪烁：先设原生背景色再设数据
      const app = getApp()
      const bg = app.globalData.isDarkMode ? '#1C1C1E' : '#F2F2F7'
      wx.setBackgroundColor({ backgroundColor: bg, backgroundColorTop: bg, backgroundColorBottom: bg })
      this.setData({
        isDarkMode: app.globalData.isDarkMode,
        themeMode: app.globalData.themeMode,
      })
      this._initScrollEngine()
      this._uploadTimers = {}   // { index: intervalId } — 每个文件独立的进度条定时器
      this._pollTimers = {}     // { index: intervalId } — 页数轮询定时器
      this._buildScheduleDays()
      this.doLogin()
    },
    detached() {
      try { const r = getApp().globalData._pageRegistry; if (r) { const i = r.indexOf(this); if (i >= 0) r.splice(i, 1) } } catch(e) {}
      this._cancelAllPendingPageAnalyses()   // 页面销毁（关闭小程序）→ 取消未确认文件的页数分析
      this._destroyScrollEngine()
      if (this._fileListTween) { clearTimeout(this._fileListTween); this._fileListTween = null }
      this._stopAllUploadTimers()
      this._stopAllPollTimers()
      this._stopBreathingGlow()
    },
  },
  pageLifetimes: {
    show() {
      // 跨天/切后台回来时刷新"今天(周X)"日期文案（scheduleDayIndex 保持有效）
      this._buildScheduleDays()
      this.data.isDarkMode = getApp().globalData.isDarkMode
      // 防止 tab 切换时闪白/闪黑：先同步原生背景色
      const app = getApp()
      const bg = app.globalData.isDarkMode ? '#1C1C1E' : '#F2F2F7'
      wx.setBackgroundColor({ backgroundColor: bg, backgroundColorTop: bg, backgroundColorBottom: bg })
      // 系统对话框返回 → 跳过入场动画，直接恢复数据刷新
      if (this._returningFromDialog) {
        this._returningFromDialog = false
        this.loadPrinterStatus()
        this._startPrinterPolling()
        this._restartPageCountPolls()
        if (this.data.autoPrintEnabled) this._startBreathingGlow()
        this._scheduleMeasure()
        setTimeout(() => this._scheduleMeasure(300), 300)
        return
      }
      // 从后台恢复 → 跳过入场动画，直接显示页面
      const resumedFromBg = app.globalData._resumedFromBackground
      if (resumedFromBg) {
        app.globalData._resumedFromBackground = false
        // 同步深色模式状态
        const darkMode = wx.getStorageSync('darkMode') || false
        this.setData({
          pageExit: '', pageSlide: 'page-fade-in',
          isDarkMode: app.globalData.isDarkMode,
          themeMode: app.globalData.themeMode,
        })
        this.loadPrinterStatus()
        this._startPrinterPolling()
        this._restartPageCountPolls()
        if (this.data.autoPrintEnabled) this._startBreathingGlow()
        this._scheduleMeasure()
        setTimeout(() => this._scheduleMeasure(300), 300)
        return
      }
      // 两步入场动画：① 强制隐藏 + 无条件同步主题，② 稍后播入场
      // page-init 确保 isDarkMode 在首帧渲染前已提交，避免使用缓存页面的过期主题值
      const app2 = getApp()
      const tabFrom = wx.getStorageSync('_tabFrom')
      const isFirstLaunch = (tabFrom == null || tabFrom === '')
      this.setData({
        pageExit: '',
        pageSlide: 'page-init',
        isDarkMode: app2.globalData.isDarkMode,
        themeMode: app2.globalData.themeMode,
      })
      setTimeout(() => {
        let animationClass = ''
        if (isFirstLaunch) {
          animationClass = 'page-fade-in page-fade-in-delayed'   // 首次加载：延迟 0.5s 向上淡入
        } else if (tabFrom === 1) {
          animationClass = 'page-enter-left'
        } else {
          animationClass = 'page-fade-in'
        }
        this.setData({ pageSlide: animationClass })
      }, 80)  // >2帧，让原生组件（page-meta/navigation-bar）有足够时间完成桥接更新

      // 首次打开才有元素入场动画
      if (!this._entrancePlayed && isFirstLaunch) {
        this.setData({ pageReady: false })
      }

      this.loadPrinterStatus()
      // 启动打印机状态轮询（30秒）
      this._startPrinterPolling()
      // 页数轮询 + 无障碍打印呼吸光晕：页面重新可见时恢复
      this._restartPageCountPolls()
      if (this.data.autoPrintEnabled) this._startBreathingGlow()
      // 首次加载定价配置（与本地打印工具保持同步）
      if (!this.data.pricingLoaded) {
        this.loadPricing()
      }
      // 每次切回页面时重新检查角色（可能在"我"页面兑换了许可）。
      // 守卫：从"我"页回来强制刷新（兑换可能发生），否则 60s 内不重复拉取
      if (wx.getStorageSync('token')) {
        const _now = Date.now()
        const cameFromMe = wx.getStorageSync('_tabFrom') === 1
        if (cameFromMe || !this._lastRoleLoad || (_now - this._lastRoleLoad) > 60000) {
          this._lastRoleLoad = _now
          this.loadUserRole()
        }
      }
      // 重印恢复：唯一消费点（来自"我"页或详情页写入的 reprintInfo）
      const reprintInfo = wx.getStorageSync('reprintInfo')
      if (reprintInfo) {
        wx.removeStorageSync('reprintInfo')
        this._restoreReprintFiles(reprintInfo)
      }
      // 同步 tabBar 选中态
      try {
        const tabBar = this.getTabBar && this.getTabBar()
        if (tabBar) {
          tabBar.setData({ selected: 0, 'list[0].active': true, 'list[1].active': false })
        }
      } catch (e) { /* 兼容低版本 */ }
      // 重新测量滚动引擎（因为 DOM 可能变化）
      this._scheduleMeasure()
      setTimeout(() => this._scheduleMeasure(300), 300)
      // 仅真实首次启动时触发入场动效（card-preload 保证卡片在动画触发前不可见）
      if (!this._entrancePlayed && isFirstLaunch) {
        if (this._readyTimer) clearTimeout(this._readyTimer)
        this._readyTimer = setTimeout(() => {
          this.setData({ pageReady: true })
          this._entrancePlayed = true
          this._readyTimer = null
        }, 250)
        // 入场动画（0.5s 延迟 + 卡片展开）全部结束后重新测量滚动边界
        setTimeout(() => this._scheduleMeasure(100), 1200)
      }
    },
    hide() {
      // 系统对话框（文件选择器等）触发 hide 时不重置页面，避免白屏闪烁
      if (this._choosingFile) {
        this._returningFromDialog = true
        return
      }
      if (this._readyTimer) { clearTimeout(this._readyTimer); this._readyTimer = null }
      this._stopPrinterPolling()
      this._stopAllPollTimers()      // 页数轮询随页面隐藏停止，show 时按需恢复
      this._cancelAllPendingPageAnalyses()   // 页面隐藏（切后台/关闭小程序）→ 取消未确认文件的页数分析，避免本地白下载
      this._stopBreathingGlow()      // 无障碍打印呼吸光晕定时器随页面隐藏停止
      // 重置入场动画类为隐藏态，确保下次 show 时框架首帧不可见，避免闪烁
      // pageExit 控制退出动画，pageSlide 控制入场/静止态，互不冲突
      this.setData({ pageSlide: 'page-init', pageExit: '' })
    },
  },
  methods: {
    // 由 tabBar 调用：退出动画 → 回调中切换页面
    // 弹窗打开时：播放关闭动画 → 关闭弹窗 + 切换 tab（一气呵成）
    animateExit(direction) {
      if (this.data.showPageCountWarning || this.data.showSuccessModal || this.data.showAccessDeniedModal) {
        this.setData({ modalClosing: true })
        wx.setStorageSync('_tabFrom', 0)
        wx.setStorageSync('_tabTo', 1)
        const app = getApp()
        const bg = app.globalData.isDarkMode ? '#1C1C1E' : '#F2F2F7'
        wx.setBackgroundColor({ backgroundColor: bg, backgroundColorTop: bg, backgroundColorBottom: bg })
        const wasSuccess = this.data.showSuccessModal
        setTimeout(() => {
          const patch = {
            showPageCountWarning: false,
            showSuccessModal: false,
            showAccessDeniedModal: false,
            modalClosing: false
          }
          if (wasSuccess) {
            patch.selectedFiles = []
            patch.badgeCount = 0
            patch.scrollPadHeight = 0
            this._contentEst = 0
          }
          this.setData(patch)
          if (wasSuccess) {
            try {
              const tabBar = this.getTabBar && this.getTabBar()
              if (tabBar) tabBar.setData({ hideBorder: false })
            } catch (e) { /* 兼容低版本 */ }
            this._stopAllUploadTimers()
            this._stopAllPollTimers()
            this._stopBreathingGlow()
          }
          wx.switchTab({ url: '/pages/me/me' })
        }, 200)
        return false
      }
      this.setData({ pageExit: direction === 'left' ? 'page-exit-left' : 'page-exit-right' })
      return true
    },

    _startPrinterPolling() {
      this._stopPrinterPolling()
      // 30s 轮询（设计意图，避免高频请求触发 Nginx hn_api 限流：60r/m + burst 150）
      this._printerPollTimer = setInterval(() => {
        this.loadPrinterStatus()
      }, 30000)
    },
    _stopPrinterPolling() {
      if (this._printerPollTimer) {
        clearInterval(this._printerPollTimer)
        this._printerPollTimer = null
      }
    },
    // ==================== 微信登录 ====================

    doLogin() {
      wx.login({
        success: (res) => {
          if (!res.code) {
            console.error('wx.login 未返回 code')
            return
          }
          request({
            url: CONFIG.BASE_URL + '/api/login',
            method: 'POST',
            header: { 'content-type': 'application/json' },
            data: { code: res.code },
            success: (loginRes) => {
              if (loginRes.statusCode === 200 && loginRes.data.success) {
                const token = loginRes.data.token
                const openid = loginRes.data.openid
                wx.setStorageSync('token', token)
                wx.setStorageSync('openid', openid)
                const app = getApp()
                app.globalData.token = token
                app.globalData.openid = openid
                console.log('登录成功, openid:', openid)
                this.loadUserRole()
              } else {
                console.error('[doLogin] 登录失败:', loginRes.statusCode, loginRes.data)
              }
            },
            fail: (err) => {
              console.error('[doLogin] 登录请求失败:', err)
            }
          })
        },
        fail: (err) => {
          console.error('wx.login 调用失败:', err)
        }
      })
    },

    doLoginAndRetry(retryCallback) {
      wx.login({
        success: (res) => {
          if (!res.code) {
            wx.showToast({ title: '重新登录失败', icon: 'none' })
            return
          }
          request({
            url: CONFIG.BASE_URL + '/api/login',
            method: 'POST',
            header: { 'content-type': 'application/json' },
            data: { code: res.code },
            success: (loginRes) => {
              if (loginRes.statusCode === 200 && loginRes.data.success) {
                wx.setStorageSync('token', loginRes.data.token)
                wx.setStorageSync('openid', loginRes.data.openid)
                retryCallback()
              } else {
                console.error('[doLoginAndRetry] 登录失败:', loginRes.statusCode, loginRes.data)
                wx.showToast({ title: '重新登录失败', icon: 'none' })
              }
            },
            fail: (err) => {
              console.error('[doLoginAndRetry] 网络请求失败:', err)
              wx.showToast({ title: '网络错误', icon: 'none' })
            }
          })
        }
      })
    },

    // ==================== 角色检查 ====================

    loadUserRole() {
      const token = wx.getStorageSync('token')
      if (!token) return
      request({
        url: CONFIG.BASE_URL + '/api/me',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          if (res.statusCode === 200 && res.data && res.data.success) {
            const role = res.data.role || 'guest'
            this.setData({
              userRole: role,
            })
            wx.setStorageSync('userRole', role)
            // 从服务端同步主题偏好（跨设备一致）
            const app = getApp()
            app.syncThemeFromServer(res.data.theme_mode)
          } else {
            console.error('[index.loadUserRole] 服务器返回异常:', res.statusCode, res.data)
          }
        },
        fail: (err) => {
          console.error('[index.loadUserRole] 网络请求失败:', err)
        }
      })
    },

    onAccessDeniedConfirm() {
      if (this.data.modalClosing) return
      this.setData({ modalClosing: true })
      setTimeout(() => {
        this.setData({ showAccessDeniedModal: false, modalClosing: false })
        wx.switchTab({ url: '/pages/me/me' })
      }, 200)
    },

    // ==================== 自定义橡皮筋滚动引擎 ====================

    _initScrollEngine() {
      this._y = 0
      this._minY = 0
      this._maxY = 0
      this._scrollerH = 0
      this._contentH = 0
      this._contentEst = 0   // 估算累积高度，兜底微信容器上限
      this._fileListPx = 0   // 文件列表累计占用高度（有界 scroll-view，达到上限后不再增长）

      this._trackId = null
      this._lastY = 0
      this._lastT = 0
      this._moved = false
      this._points = []

      this._tick = null
      this._vel = 0
      this._inDecel = false
      this._handoff = false

      this._dampMax = 130
      this._fric = 0.006
      this._snapSpd = 0.32

      this._measureTimer = null  // 去抖测量句柄

      // 底部额外滚动留白（提交按钮与 tabBar 顶边之间的小间隙）
      this._bottomPad = 20
      // 悬浮 tabBar 遮挡高度：bottom 12rpx + 高度 110rpx（见 custom-tab-bar/index.wxss）+ 底部安全区。
      // 滚动范围计算时显式加上它，保证滚到底时提交按钮不被 tabBar 遮住。
      const _wi = wx.getWindowInfo()
      const _safeBottom = _wi && _wi.safeArea ? Math.max(0, _wi.windowHeight - _wi.safeArea.bottom) : 0
      this._tabOverlayPx = Math.round((12 + 110) * ((_wi.windowWidth || 375) / 750)) + _safeBottom

      this._scheduleMeasure()
      setTimeout(() => this._scheduleMeasure(), 400)
      setTimeout(() => this._scheduleMeasure(), 800)
    },

    _destroyScrollEngine() {
      this._cancelSchedule()
      if (this._measureTimer) {
        clearTimeout(this._measureTimer)
        this._measureTimer = null
      }
    },

    // 去抖测量：内容变化后延迟刷新滚动边界
    // delay 可选，默认 100ms；动画后调用可传 400+ 确保 DOM 已稳定
    _scheduleMeasure(delay) {
      if (this._measureTimer) clearTimeout(this._measureTimer)
      this._measureTimer = setTimeout(() => {
        this._measureTimer = null
        this._measure()
      }, delay || 100)
    },

    // 圆点清除计时器（badge 状态已在上层 setData 中同帧设置，这里只负责定时清除）
    _scheduleBadgeClear(entering) {
      if (this._badgeTimer) clearTimeout(this._badgeTimer)
      this._prevFileCount = (this._prevFileCount || 0) + 1
      this._badgeTimer = setTimeout(() => {
        this.setData({ badgeEntering: false, badgeBouncing: false })
      }, entering ? 450 : 400)  // 入场:400ms动画+50ms / 弹跳:350ms动画+50ms (延迟已在外部)
    },

    // 圆点退场动画（末文件删除时先播动画再移除）
    _triggerBadgeExit() {
      if (this._badgeTimer) clearTimeout(this._badgeTimer)
      this._prevFileCount = 0
      this.setData({ badgeExiting: true, badgeEntering: false, badgeBouncing: false })
      this._triggerBtnPulse()  // 1→0: 按钮文字从「添加文件」→「选择打印文件」
      this._badgeTimer = setTimeout(() => {
        this.setData({ badgeExiting: false })
      }, 650)  // 必须撑到卡片 splice 之后（600ms），否则 forwards 填充失效导致闪现
    },

    // 添加按钮脉冲动画：文字切换时（选择打印文件 ↔ 添加文件）轻微按压回弹
    _triggerBtnPulse() {
      if (this._btnPulseTimer) clearTimeout(this._btnPulseTimer)
      this.setData({ btnPulse: true })
      this._btnPulseTimer = setTimeout(() => {
        this.setData({ btnPulse: false })
      }, 450)  // 动画 0.45s
    },

    _schedule(fn) {
      return setTimeout(fn, 16)
    },
    _cancelSchedule() {
      if (this._tick) {
        clearTimeout(this._tick)
        this._tick = null
      }
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
        // 估算同步为实测：避免 _contentEst 只涨不跌（否则 _contentH 恒取估算，滚动范围偏大）
        if (ch > 0) this._contentEst = ch
        this._contentH = Math.max(ch, this._contentEst)
        this._maxY = Math.max(0, this._contentH - vp + this._bottomPad + this._tabOverlayPx)
        if (this._y > this._maxY) {
          // 不直接跳变，让 _snapBack() 从当前位置平滑回弹到新边界
          this._snapBack()
        }
      })
    },

    // "打印文件"卡片高度补间动画：随文件添加/删除平滑展开/收起
    // （原生 enhanced scroll-view 的 height 样式不支持 CSS 过渡，用 JS 逐帧驱动 setData）
    _animateFileListHeight(targetHeight, duration) {
      if (this._fileListTween) {
        clearTimeout(this._fileListTween)
        this._fileListTween = null
      }
      const startHeight = this.data.fileListHeight || 0
      const diff = targetHeight - startHeight
      if (Math.abs(diff) < 1) {
        this.setData({ fileListHeight: targetHeight })
        return
      }
      const startTime = Date.now()
      const tick = () => {
        const elapsed = Date.now() - startTime
        const t = Math.min(1, elapsed / duration)
        const eased = 1 - Math.pow(1 - t, 3)   // easeOutCubic：先快后慢
        this.setData({ fileListHeight: Math.max(0, Math.round(startHeight + diff * eased)) })
        if (t < 1) {
          this._fileListTween = setTimeout(tick, 24)
        } else {
          this._fileListTween = null
        }
      }
      tick()
    },

    // 依据当前文件列表重算"打印文件"列表显式高度（动态卡片）。
    // 触发点：pageCount 确认/文本↔网格模式切换/singlePage 判定变化/范围行增删等。
    // 添加/删除/重印恢复路径已通过 _fileCardHeightRpx() 直接取到精确高度，无需额外调用。
    _recalcFileListHeight() {
      const { windowWidth, windowHeight } = wx.getWindowInfo()
      const rpxR = (windowWidth || 375) / 750
      const listCapPx = Math.round((windowHeight || 800) * 0.85)
      const files = this.data.selectedFiles || []
      let sumRpx = 0
      files.forEach((f, i) => {
        sumRpx += _fileCardHeightRpx(f) + (i > 0 ? FILE_CARD_GAP_RPX : 0)
      })
      const target = Math.min(listCapPx, Math.round(sumRpx * rpxR))
      const prev = this._fileListPx || 0
      const delta = target - prev
      this._fileListPx = target
      // 滚动边界估算同步增减（收缩为负 delta），避免等待 _measure 前滚动范围残留偏大
      if (delta !== 0 && this._contentEst) this._contentEst = Math.max(0, this._contentEst + delta)
      this._animateFileListHeight(target, 350)
      this._scheduleMeasure(400)
    },

    // 探测工具（一次性）：临时注入 图片/网格/网格单选/文本/文本单选/单页Word/Excel 七张「完整」卡片
    // （fileId 已存在 → 控件/页数条全展开），实测各自精确高度输出到控制台，
    // 用于替换上方 FILE_CARD_HEIGHT_RPX 的硬编码值。
    // 调用：开发者工具控制台执行 getCurrentPages()[0]._probeCardHeights()
    _probeCardHeights() {
      const savedFiles = this.data.selectedFiles
      const savedH = this.data.fileListHeight
      const mk = (over) => Object.assign({
        name: '', size: 0, path: '', sizeDisplay: '1.0', fileId: 0,
        uploading: false, progress: 100, failed: false, copies: 1, pageRange: '',
        rangeLines: [{ value: '', error: '' }], duplex: 'on', imageOrientation: 'auto',
        entering: false, removing: false,
        excelWarning: false, unsupportedFormat: false, isImage: false, pageCount: 1,
        pageCountStatus: 'confirmed', singlePage: false,
      }, over)
      const probes = [
        mk({ name: '探测-图片.png', fileId: 990001, isImage: true, duplex: 'off', imageOrientation: 'landscape', pageCount: 1 }),
        // 页数已知 → 网格模式
        mk({ name: '探测-网格.docx', fileId: 990002, isImage: false, pageCount: 10, pageCountStatus: 'confirmed', copies: 3 }),
        mk({ name: '探测-网格单选.docx', fileId: 990005, isImage: false, pageCount: 10, pageCountStatus: 'confirmed', pageRange: '5', rangeLines: [{ value: '5', error: '' }, { value: '', error: '' }], copies: 3, singlePage: true }),
        // 页数未知 → 文本模式（警告+多行输入；仅测 1 行基准高，额外行由 RANGE_LINE_EXTRA_RPX 运行时累加）
        mk({ name: '探测-文本.docx', fileId: 990006, isImage: false, pageCount: 0, pageCountStatus: 'analyzing', copies: 3 }),
        mk({ name: '探测-文本单选.docx', fileId: 990007, isImage: false, pageCount: 0, pageCountStatus: 'analyzing', pageRange: '5', rangeLines: [{ value: '5', error: '' }], copies: 3, singlePage: true }),
        // 整份 1 页
        mk({ name: '探测-单页文档.docx', fileId: 990004, isImage: false, pageCount: 1, pageCountStatus: 'confirmed', duplex: 'off', copies: 3, singlePage: true }),
        mk({ name: '探测-表格.xlsx', fileId: 990003, isImage: false, excelWarning: true, pageCount: 0, pageCountStatus: '' }),
      ]
      // 临时撑开列表高度，确保 7 张卡片完整渲染（0 高度容器可能不产出布局）
      this.setData({ selectedFiles: probes, badgeCount: probes.length, fileListHeight: 1600 }, () => {
        setTimeout(() => {
          const q = this.createSelectorQuery()
          q.selectAll('.file-card').boundingClientRect()
          q.exec((res) => {
            const cards = (res && res[0]) || []
            const { windowWidth } = wx.getWindowInfo()
            const rpxRatio = (windowWidth || 375) / 750
            const keys = ['image', 'word-grid', 'word-grid-single', 'word-text', 'word-text-single', 'word-single', 'excel']
            const gapPx = +(FILE_CARD_GAP_RPX * rpxRatio).toFixed(1)
            const heights = {}
            let sumPx = 0
            cards.forEach((c, i) => {
              if (!c || !keys[i]) return
              const px = Math.round(c.height)
              const rpx = +(c.height / rpxRatio).toFixed(1)
              heights[keys[i]] = { px, rpx }
              sumPx += px + (i > 0 ? gapPx : 0)
            })
            console.log('[卡片高度探测] 原始(px)与换算(rpx):', JSON.stringify(heights, null, 2))
            console.log('[卡片高度探测] 7卡合计含间距:', sumPx, 'px，卡片间距:', gapPx, 'px')
            console.log('[卡片高度探测] 硬编码建议: FILE_CARD_HEIGHT_RPX =', JSON.stringify({
              image: heights.image && heights.image.rpx,
              'word-grid': heights['word-grid'] && heights['word-grid'].rpx,
              'word-grid-single': heights['word-grid-single'] && heights['word-grid-single'].rpx,
              'word-text': heights['word-text'] && heights['word-text'].rpx,
              'word-text-single': heights['word-text-single'] && heights['word-text-single'].rpx,
              'word-single': heights['word-single'] && heights['word-single'].rpx,
              excel: heights.excel && heights.excel.rpx,
            }))
            // 恢复原列表
            this.setData({ selectedFiles: savedFiles, badgeCount: savedFiles.length, fileListHeight: savedH })
          })
        }, 120)  // 等 setData 渲染稳定后再测量
      })
    },

    // 添加文件时立刻计算列表高度并更新滚动边界，无需等待 cardExpand 动画完成。
    // 高度按每文件类型实测常量累加（同步计算、零异步测量），上限仍锁定 85vh。
    _bumpForNewFile() {
      const { windowWidth, windowHeight } = wx.getWindowInfo()
      const rpxRatio = (windowWidth || 375) / 750
      // 文件列表为有界原生 scroll-view（显式高度驱动，上限 ≈ 85vh / 3~4 个卡片）。
      // 达到上限后新增文件只内部滚动、不再撑高页面，故 contentEst 不再增长。
      const listCapPx = Math.round((windowHeight || 800) * 0.85)
      const files = this.data.selectedFiles
      let sumRpx = 0
      files.forEach((f, i) => {
        sumRpx += _fileCardHeightRpx(f) + (i > 0 ? FILE_CARD_GAP_RPX : 0)
      })
      if (!this._fileListPx) this._fileListPx = 0
      const prev = this._fileListPx
      this._fileListPx = Math.min(listCapPx, Math.round(sumRpx * rpxRatio))
      const delta = this._fileListPx - prev
      // 显式高度让 scroll-view 真正裁剪溢出卡片并内部滚动（仅 max-height 在微信中不可靠，会画到按钮上）。
      // 高度变化走补间动画，"打印文件"卡片随之平滑展开。
      this._animateFileListHeight(Math.max(0, this._fileListPx), 350)

      if (!this._scrollerH) {
        this._measure()
        this._scheduleMeasure(200)
        return
      }
      if (!this._contentEst) this._contentEst = this._contentH
      if (delta > 0) this._contentEst += delta
      this._maxY = Math.max(0, this._contentEst - this._scrollerH + this._bottomPad + this._tabOverlayPx)
      // 垫片置 0：容器自带底部 padding 且文件列表为有界滚动，不再触及微信高度上限。
      // 之前设成 _contentEst（整份估算高）会在容器外加高近一屏，造成底部大量空白。
      this.setData({ scrollPadHeight: 0 })
      if (this._y > this._maxY) this._snapBack()
    },

    _applyY() {
      const real = Math.max(0, Math.min(this._y, this._maxY))
      const ratio = this._maxY > 0 ? Math.min(real / 400, 1) : 0
      // transform: scale() 保持宽高比，每帧自然跟随滚动
      const logoScale = +(1.0 - ratio * 0.7).toFixed(3)  // 1.0 → 0.3
      const logoPadding = Math.round(40 - ratio * 32)
      const patch = { scrollTop: this._renderY() }
      if (logoScale !== this.data.logoScale) patch.logoScale = logoScale
      if (logoPadding !== this.data.logoPadding) patch.logoPadding = logoPadding
      this.setData(patch)
    },

    _dampShift(d) {
      const max = this._dampMax
      const sign = d >= 0 ? 1 : -1
      return sign * max * (1 - Math.exp(-Math.abs(d) / (max * 1.6)))
    },

    _renderY() {
      const y = this._y
      if (y < this._minY) {
        return this._minY - this._dampShift(this._minY - y)
      }
      if (y > this._maxY) {
        return this._maxY + this._dampShift(y - this._maxY)
      }
      return y
    },

    onScrollerTouchStart(e) {
      const touches = e.touches || []
      this._points = touches.map((t) => ({ id: t.identifier, y: t.clientY }))

      // 新增：方向锁定初始化
      if (touches.length > 0) {
        this._startX = touches[0].clientX
        this._startY = touches[0].clientY
        this._directionLocked = false
        this._horizontalGesture = false
      }

      this._cancelSchedule()
      this._inDecel = false
      this._handoff = false

      if (this._trackId === null) {
        const p = this._points[0]
        if (!p) return
        this._trackId = p.id
        this._lastY = p.y
        this._lastT = Date.now()
        this._vel = 0
        this._moved = false
      } else {
        const cur = this._points.find((p) => p.id === this._trackId)
        if (cur) {
          this._lastY = cur.y
          this._lastT = Date.now()
        }
      }
    },

    onScrollerTouchMove(e) {
      const touches = e.touches || []
      if (touches.length === 0) return
      this._points = touches.map((t) => ({ id: t.identifier, y: t.clientY }))

      // ---- 新增：方向锁定逻辑 ----
      const touchDx = touches[0].clientX - this._startX
      const touchDy = touches[0].clientY - this._startY

      if (!this._directionLocked) {
        if (Math.abs(touchDx) > 5 || Math.abs(touchDy) > 5) {
          if (Math.abs(touchDx) > Math.abs(touchDy)) {
            this._directionLocked = true
            this._horizontalGesture = true
            return
          } else {
            this._directionLocked = true
            this._horizontalGesture = false
          }
        } else {
          return
        }
      }

      if (this._horizontalGesture) {
        return
      }

      // ---- 原有垂直滚动逻辑 ----
      if (this._trackId === null || !this._points.find((p) => p.id === this._trackId)) {
        const p = this._points[0]
        if (!p) return
        this._trackId = p.id
        this._lastY = p.y
        this._lastT = Date.now()
        this._handoff = true
        return
      }

      const cur = this._points.find((p) => p.id === this._trackId)
      if (!cur) return

      const now = Date.now()
      const dy = cur.y - this._lastY
      const dt = Math.max(1, now - this._lastT)

      if (Math.abs(dy) > 0.5) this._moved = true

      this._y -= dy
      const inst = -dy / dt
      this._vel = this._vel * 0.6 + inst * 0.4

      this._lastY = cur.y
      this._lastT = now

      this._applyY()
    },

    onScrollerTouchEnd(e) {
      // 重置方向状态
      this._horizontalGesture = false
      this._directionLocked = false

      const touches = e.touches || []
      this._points = touches.map((t) => ({ id: t.identifier, y: t.clientY }))

      const stillHasMain = this._points.find((p) => p.id === this._trackId)
      if (stillHasMain) return

      if (this._points.length > 0) {
        this._trackId = null
        this._handoff = true
        return
      }

      this._trackId = null
      this._handoff = false
      this._startPhysics()
    },

    _startPhysics() {
      this._cancelSchedule()
      if (this._y < this._minY || this._y > this._maxY) {
        this._vel = 0
        this._snapBack()
        return
      }
      if (Math.abs(this._vel) < 0.05) {
        this._snapBack()
        return
      }
      this._inDecel = true
      this._lastT = Date.now()
      const tick = () => {
        if (!this._inDecel) return
        const now = Date.now()
        const dt = Math.max(1, now - this._lastT)
        this._lastT = now

        const decay = Math.exp(-this._fric * dt)
        this._vel *= decay
        this._y += this._vel * dt

        if (this._y < this._minY) {
          this._y = this._minY
          this._vel = 0
          this._inDecel = false
          this._snapBack()
          return
        }
        if (this._y > this._maxY) {
          this._y = this._maxY
          this._vel = 0
          this._inDecel = false
          this._snapBack()
          return
        }
        if (Math.abs(this._vel) < 0.02) {
          this._inDecel = false
          this._applyY()
          return
        }
        this._applyY()
        this._tick = this._schedule(tick)
      }
      this._tick = this._schedule(tick)
    },

    _snapBack() {
      this._cancelSchedule()
      const tick = () => {
        if (this._handoff) {
          this._tick = null
          return
        }
        const minY = this._minY
        const maxY = this._maxY
        let target = this._y
        if (this._y < minY) target = minY
        else if (this._y > maxY) target = maxY
        else {
          this._y = target
          this._applyY()
          this._tick = null
          return
        }
        this._y += (target - this._y) * this._snapSpd
        if (Math.abs(this._y - target) < 0.3) {
          this._y = target
          this._applyY()
          this._tick = null
          return
        }
        this._applyY()
        this._tick = this._schedule(tick)
      }
      this._tick = this._schedule(tick)
    },

    // ==================== 多文件操作 ====================

    onChooseFile() {
      // 访客拦截：需先兑换许可密钥再选择文件
      if (this.data.userRole === 'guest') {
        wx.showToast({ title: '请先兑换许可密钥后再选择文件', icon: 'none', duration: 2000 })
        return
      }
      // 文件数上限：最多 20 个
      if (this.data.selectedFiles.length >= 20) {
        wx.showToast({ title: '最多 20 个文件', icon: 'none', duration: 2000 })
        return
      }
      this._choosingFile = true
      wx.chooseMessageFile({
        count: 1,
        type: 'all',
        complete: () => { this._choosingFile = false },
        success: (res) => {
          const file = res.tempFiles[0]
          const name = file.name || ''
          const sizeKB = Number(file.size) || 0
          const fileIndex = this.data.selectedFiles.length

          // 50MB 上限预检（size 单位为字节）
          if ((Number(file.size) || 0) > 50 * 1024 * 1024) {
            wx.showToast({
              title: '文件超过 50MB 限制',
              icon: 'none',
              duration: 2000
            })
            return  // 拒绝此文件，不添加到列表
          }

          // 检测文件格式：图片 / 不支持格式（Excel/PPT/压缩包等）
          const ext = name.slice(name.lastIndexOf('.')).toLowerCase()
          const imageExts = ['.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp', '.tiff', '.tif']
          const isImage = imageExts.includes(ext)
          // 不支持自动打印的类型（可添加显示，但无法打印，需联系管理员）：Excel/PPT/CAD
          const unsupportedExts = ['.xls', '.xlsx', '.ppt', '.pptx', '.dwg', '.dxf']
          const isUnsupported = unsupportedExts.includes(ext)

          // 可打印的支持格式（html/htm 已移除）
          const supportedExts = ['.pdf', '.doc', '.docx', '.txt', '.csv', '.md',
            '.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp', '.tiff', '.tif']
          if (!supportedExts.includes(ext) && !isUnsupported) {
            wx.showToast({
              title: `不支持 ${ext} 格式`,
              icon: 'none',
              duration: 2000
            })
            return  // 拒绝此文件，不添加到列表
          }

          const newFile = {
            name: name,
            size: file.size,
            path: file.path,
            sizeDisplay: (sizeKB / 1024).toFixed(1),
            fileId: null,
            uploading: true,
            progress: 0,
            failed: false,
            copies: 1,
            pageRange: '',                        // 提交用，由 rangeLines 合并得出
            rangeLines: [{value: '', error: ''}],  // 多行输入，对齐本地工具 RangeListWidget
            duplex: isImage ? 'off' : 'on',  // 图片单页渲染，无双面概念 → 固定单面
            imageOrientation: 'auto',   // 图片打印方向: auto=自动 / landscape=横向 / portrait=竖向
            entering: true,
            removing: false,
            excelWarning: isUnsupported,   // 不支持自动打印的类型（Excel/PPT/CAD）
            unsupportedFormat: false,   // 未知格式已在选择时拦截，不会到达此处
            isImage: isImage,
            pageCount: 0,
            pageCountStatus: '',  // '' | 'analyzing' | 'confirmed' — 页数分析进度
            singlePage: false,    // 有效选择恰好 1 页 → 模式行隐藏、提交强制单面
          }
          // 圆点动画和计数统一延迟 0.25s，与卡片入场同步
          const isFirstFile = fileIndex === 0
          const newCount = fileIndex + 1
          this.setData({
            ['selectedFiles[' + fileIndex + ']']: newFile,
            badgeExiting: false
          })
          if (this._badgeCountTimer) clearTimeout(this._badgeCountTimer)
          this._badgeCountTimer = setTimeout(() => {
            this.setData({
              badgeCount: newCount,
              badgeEntering: isFirstFile,
              badgeBouncing: !isFirstFile
            })
            this._scheduleBadgeClear(isFirstFile)
          }, 250)
          if (isFirstFile) this._triggerBtnPulse()
          // 入场动画延迟 0.25s（等待微信文件选择器关闭）+ 动画 0.5s
          setTimeout(() => {
            this.setData({ ['selectedFiles[' + fileIndex + '].entering']: false })
          }, 800)  // 250ms delay + 500ms animation + 50ms buffer
          this._bumpForNewFile()                        // 立刻扩展滚动边界，不等动画
          this._scheduleMeasure(400)
          setTimeout(() => this._scheduleMeasure(850), 850)  // 动画完成后修正为精确值
          this.startFileUpload(fileIndex, file.path)
        },
        fail: (err) => {
          console.log('选择文件失败', err)
        }
      })
    },

    onRemoveFile(e) {
      const index = e.currentTarget.dataset.index
      this.stopFileUploadTimer(index)
      this._stopPageCountPoll(index)
      // 删除已上传但页数未确认的文件 → 取消本地页数分析（避免白下载）
      const removed = this.data.selectedFiles[index]
      if (removed && removed.fileId && !(removed.pageCount > 0)) {
        this._cancelPageAnalysis([removed.fileId])
      }
      const isLastFile = this.data.selectedFiles.length === 1
      const newCount = this.data.selectedFiles.length - 1

      // 最后一个文件 → 圆点先播退场动画
      if (isLastFile) {
        this._triggerBadgeExit()
      } else {
        // 非末文件：圆点弹跳 + 计数统一延迟 0.25s
        if (this._badgeCountTimer) clearTimeout(this._badgeCountTimer)
        this._badgeCountTimer = setTimeout(() => {
          this.setData({
            badgeCount: newCount,
            badgeBouncing: true,
            badgeExiting: false,
            badgeEntering: false
          })
          this._scheduleBadgeClear(false)
        }, 250)
      }

      // 触发 cardRemove 动画（0.55s 单段：淡出+收起），完成后移除
      this.setData({ ['selectedFiles[' + index + '].removing']: true })
      // 收起动画：随卡片折叠同步平滑缩短"打印文件"卡片
      // 目标 = min(上限, 其余卡片按类型实测高之和)：达到上限后删除文件时，
      // 只要剩余内容仍占满/超出上限，外部高度就不应下降（避免容器矮于内容、截断剩余卡片）
      const { windowWidth: rw, windowHeight: rh } = wx.getWindowInfo()
      const rpxR = (rw || 375) / 750
      const remCapPx = Math.round((rh || 800) * 0.85)
      let remSumRpx = 0
      let remCount = 0
      this.data.selectedFiles.forEach((f, i) => {
        if (i === index) return
        remSumRpx += _fileCardHeightRpx(f) + (remCount > 0 ? FILE_CARD_GAP_RPX : 0)
        remCount++
      })
      const remTarget = Math.min(remCapPx, Math.round(remSumRpx * rpxR))
      this._animateFileListHeight(remTarget, 500)
      setTimeout(() => {
        const files = this.data.selectedFiles.slice()
        files.splice(index, 1)
        const remapTimers = (timersObj) => {
          const newTimers = {}
          const oldKeys = Object.keys(timersObj).map(Number)
          oldKeys.forEach((k) => {
            if (k > index) newTimers[k - 1] = timersObj[k]
            else if (k < index) newTimers[k] = timersObj[k]
          })
          return newTimers
        }
        this._uploadTimers = remapTimers(this._uploadTimers || {})
        this._pollTimers = remapTimers(this._pollTimers || {})
        const { windowWidth: ww, windowHeight: wh } = wx.getWindowInfo()
        const rpxR = (ww || 375) / 750
        // 外部高度 = min(上限, 剩余卡片按类型实测高之和)：与 _bumpForNewFile 同规则，重算而非递减。
        // 达到上限后删除文件时，剩余内容仍占满上限则高度保持不变，仅在低于上限时收敛到内容高
        const listCapPx = Math.round((wh || 800) * 0.85)
        let sumRpx = 0
        files.forEach((f, i) => {
          sumRpx += _fileCardHeightRpx(f) + (i > 0 ? FILE_CARD_GAP_RPX : 0)
        })
        if (!this._fileListPx) this._fileListPx = 0
        const prev = this._fileListPx
        this._fileListPx = Math.min(listCapPx, Math.round(sumRpx * rpxR))
        const delta = this._fileListPx - prev
        this._contentEst = Math.max(0, this._contentEst + delta)
        this.setData({ selectedFiles: files, badgeCount: files.length, scrollPadHeight: 0 })
        // 高度随删除收敛到最新 _fileListPx（快速连删时以此为准做最终校正）
        this._animateFileListHeight(Math.max(0, this._fileListPx), 250)
        if (!isLastFile) {
          this._prevFileCount = files.length
        }
        // 高度收敛补间（250ms）结束后再测量，保证滚动边界精确
        this._scheduleMeasure(300)
      }, 600)  // cardRemove 动画 0.55s + 50ms buffer
    },

    // 重印恢复：把 storage 中的 files 重建为 selectedFiles（file_id 已存在的跳过上传，份数/页码/双面全部回填）
    _restoreReprintFiles(reprintInfo) {
      const imageExts = ['.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp', '.tiff', '.tif']
      const source = reprintInfo.files || []
      const files = source.map((f) => {
        const name = f.file_name || '未知文件'
        const ext = name.slice(name.lastIndexOf('.')).toLowerCase()
        const isImage = imageExts.indexOf(ext) !== -1
        const pageRange = f.page_range || ''
        const hasFileId = !!f.file_id
        const file = {
          name: name,
          size: Number(f.size) || 0,
          path: '',
          sizeDisplay: f.size ? (Number(f.size) / 1024).toFixed(1) : '',
          fileId: hasFileId ? f.file_id : null,
          uploading: false,
          progress: hasFileId ? 100 : 0,
          failed: !hasFileId,   // 无 file_id 无法补传，标记失败让用户移除
          copies: Math.min(Math.max(Number(f.copies) || 1, 1), 99),
          pageRange: pageRange,
          rangeLines: pageRange
            ? pageRange.split(',').filter(l => (l || '').trim()).map(v => ({ value: v.trim(), error: '' }))
            : [{ value: '', error: '' }],
          duplex: isImage ? 'off' : (f.duplex || 'on'),
          imageOrientation: isImage ? (f.image_orientation || 'auto') : 'auto',
          entering: true,
          removing: false,
          excelWarning: ['.xls', '.xlsx', '.ppt', '.pptx', '.dwg', '.dxf'].indexOf(ext) !== -1,  // 与添加路径一致
          unsupportedFormat: false,
          isImage: isImage,
          pageCount: Number(f.page_count) || 0,
          pageCountStatus: hasFileId ? (Number(f.page_count) > 0 ? 'confirmed' : 'analyzing') : '',
          singlePage: false,
        }
        file.singlePage = this._computeSinglePage(file)   // 有效选择恰好 1 页 → 模式行隐藏
        return file
      })
      this.setData({
        selectedFiles: files,
        badgeCount: files.length,
        duplex: reprintInfo.duplex || 'on',
        scrollPadHeight: 0,
      })
      // 恢复文件列表显式高度：按每文件类型实测高累加（与 _bumpForNewFile 同规则）
      const { windowWidth, windowHeight } = wx.getWindowInfo()
      const rpxRatio = (windowWidth || 375) / 750
      const listCapPx = Math.round((windowHeight || 800) * 0.85)
      let sumRpx = 0
      files.forEach((f, i) => {
        sumRpx += _fileCardHeightRpx(f) + (i > 0 ? FILE_CARD_GAP_RPX : 0)
      })
      this._fileListPx = Math.min(listCapPx, Math.round(sumRpx * rpxRatio))
      this.setData({ fileListHeight: Math.max(0, this._fileListPx) })
      // 页数未知的文件重新启动页数轮询
      files.forEach((f, i) => {
        if (f.fileId && !f.isImage && !(f.pageCount > 0)) {
          this._startPageCountPoll(i, f.fileId)
        }
      })
      this._scheduleMeasure(400)
      setTimeout(() => this._scheduleMeasure(850), 850)
    },

    // ==================== 文件上传（每个文件独立进度条）====================

    startFileUpload(fileIndex, filePath) {
      const token = wx.getStorageSync('token') || ''
      this.stopFileUploadTimer(fileIndex)

      const key = 'selectedFiles[' + fileIndex + ']'
      // 先登记 entry（timer/task 后续填充），再启动进度条定时器，避免声明前引用（TDZ）
      this._uploadTimers[fileIndex] = {
        realProgress: 0,
        timer: null,
        task: null,
      }

      // 每 0.5s 把显示进度向真实进度推进
      this._uploadTimers[fileIndex].timer = setInterval(() => {
        const entry = this._uploadTimers[fileIndex]
        if (!entry || !entry.realProgress) return  // 上传已被取消
        const real = entry.realProgress
        const files = this.data.selectedFiles
        if (!files[fileIndex]) return
        const shown = files[fileIndex].progress
        if (shown >= real) return
        const next = shown + Math.max(1, (real - shown) * 0.5)
        this.setData({
          [key + '.progress']: Math.round(Math.min(next, real))
        })
      }, 500)

      const task = wx.uploadFile({
        url: CONFIG.BASE_URL + '/api/upload',
        filePath: filePath,
        name: 'file',
        header: { 'Authorization': 'Bearer ' + token },
        success: (uploadRes) => {
          const entry = this._uploadTimers[fileIndex]
          if (!entry || entry.cancelled) return  // 上传已被取消
          if (uploadRes.statusCode === 401) {
            this.stopFileUploadTimer(fileIndex)
            this.setData({ [key + '.uploading']: false })
            this.doLoginAndRetry(() => {
              this.setData({
                [key + '.uploading']: true,
                [key + '.progress']: 0,
                [key + '.fileId']: null,
                [key + '.failed']: false,
              })
              this.startFileUpload(fileIndex, filePath)
            })
            return
          }

          let fileId = null
          let pageCount = 0
          let errMsg = ''
          try {
            const data = JSON.parse(uploadRes.data)
            fileId = data.file_id || data.id
            pageCount = data.page_count || 0
            if (!fileId) {
              errMsg = data.message || '上传失败'
            }
          } catch (e) {
            // 非 JSON 响应 — nginx 413 / 502 等
            const body = String(uploadRes.data || '')
            if (uploadRes.statusCode === 413 || body.includes('413') || body.includes('Entity Too Large')) {
              errMsg = '文件过大，请压缩后再试'
            } else if (uploadRes.statusCode >= 500) {
              errMsg = '服务器错误，请稍后重试'
            } else {
              console.error('上传返回解析失败:', e, body.slice(0, 200))
              errMsg = '上传失败'
            }
          }

          if (!fileId) {
            this.stopFileUploadTimer(fileIndex)
            this.setData({ [key + '.uploading']: false, [key + '.failed']: true })
            wx.showToast({ title: errMsg, icon: 'none', duration: 2500 })
            return
          }

          console.log('文件上传成功，返回 ID:', fileId, '页数:', pageCount, 'index:', fileIndex)
          this._uploadTimers[fileIndex].realProgress = 100
          this.stopFileUploadTimer(fileIndex)
          // 图片固定 1 页，覆盖后端返回值（确保一致性）
          const file = this.data.selectedFiles[fileIndex]
          if (file && file.isImage) pageCount = 1

          // 判断页数状态：PDF 直接确认，doc/docx 进入分析中
          let pageCountStatus = ''
          if (!file || !file.isImage) {
            pageCountStatus = pageCount > 0 ? 'confirmed' : 'analyzing'
          }
          this.setData({
            [key + '.uploading']: false,
            [key + '.progress']: 100,
            [key + '.fileId']: fileId,
            [key + '.failed']: false,
            [key + '.pageCount']: pageCount,
            [key + '.pageCountStatus']: pageCountStatus,
          })
          // 页数确认(PDF) → 文本↔网格模式可能切换、整份1页卡片收缩 → 同步 singlePage + 重算列表高度
          if (file && !file.isImage) {
            this._refreshSinglePage(fileIndex)
          }
          // 页数未知时启动轮询（等待本地打印工具分析回报）
          if (pageCount <= 0 && file && !file.isImage) {
            this._startPageCountPoll(fileIndex, fileId)
          }
          // DOM 从"上传中+进度条"切换为"已上传+份数+打印范围"，需更长延迟等渲染稳定
          this._scheduleMeasure(200)
          setTimeout(() => this._scheduleMeasure(450), 450)
        },
        fail: (err) => {
          const entry2 = this._uploadTimers[fileIndex]
          if (entry2 && entry2.cancelled) return  // 上传已被取消（abort 也会触发 fail）
          console.error('文件上传失败:', err)
          this.stopFileUploadTimer(fileIndex)
          this.setData({ [key + '.uploading']: false, [key + '.failed']: true })
          this._scheduleMeasure()
          wx.showToast({ title: '文件上传失败', icon: 'none', duration: 2000 })
        }
      })

      task.onProgressUpdate((res) => {
        if (typeof res.progress === 'number') {
          this._uploadTimers[fileIndex].realProgress = res.progress
        }
      })
      this._uploadTimers[fileIndex].task = task
    },

    // 上传失败卡片"重试"：重置状态后重新上传
    onRetryUpload(e) {
      const index = e.currentTarget.dataset.index
      const file = this.data.selectedFiles[index]
      if (!file || file.uploading) return
      if (!file.path) {
        wx.showToast({ title: '文件路径已失效，请重新选择', icon: 'none' })
        return
      }
      this.setData({
        ['selectedFiles[' + index + '].uploading']: true,
        ['selectedFiles[' + index + '].progress']: 0,
        ['selectedFiles[' + index + '].fileId']: null,
        ['selectedFiles[' + index + '].failed']: false,
      })
      this.startFileUpload(index, file.path)
    },

    stopFileUploadTimer(fileIndex) {
      const entry = this._uploadTimers && this._uploadTimers[fileIndex]
      if (!entry) return
      entry.cancelled = true
      if (entry.timer) {
        clearInterval(entry.timer)
        entry.timer = null
      }
      if (entry.task) {
        try { entry.task.abort() } catch (e) { /* ok */ }
        entry.task = null
      }
    },

    _stopAllUploadTimers() {
      if (!this._uploadTimers) return
      Object.keys(this._uploadTimers).forEach((k) => {
        this.stopFileUploadTimer(Number(k))
      })
    },

    _stopAllPollTimers() {
      if (!this._pollTimers) return
      Object.keys(this._pollTimers).forEach((k) => {
        this._stopPageCountPoll(Number(k))
      })
    },

    // 取消指定文件的页数分析（删除文件 / 页面隐藏 / 销毁时调用，尽力而为）
    _cancelPageAnalysis(fileIds) {
      const ids = (fileIds || []).filter(id => id)
      if (!ids.length) return
      const token = wx.getStorageSync('token') || ''
      request({
        url: CONFIG.BASE_URL + '/api/cancel_page_analysis',
        method: 'POST',
        header: { 'Authorization': 'Bearer ' + token, 'content-type': 'application/json' },
        data: { file_ids: ids },
        fail: () => { /* 取消是尽力而为，失败不影响主流程 */ }
      })
    },

    // 收集列表中所有"已上传但页数未确认"的文件并取消其分析（关闭小程序/删除文件时避免本地白下载）
    _cancelAllPendingPageAnalyses() {
      const ids = (this.data.selectedFiles || [])
        .filter(f => f && f.fileId && !(f.pageCount > 0))
        .map(f => f.fileId)
      if (ids.length) this._cancelPageAnalysis(ids)
    },

    // 页面重新可见时恢复页数轮询（仅对已上传且仍缺页数的文件）
    _restartPageCountPolls() {
      const files = this.data.selectedFiles || []
      files.forEach((f, i) => {
        if (f && f.fileId && !f.isImage && !f.excelWarning && !f.unsupportedFormat && !(f.pageCount > 0)) {
          this._startPageCountPoll(i, f.fileId)
        }
      })
    },

    // ==================== 页数轮询（等待本地打印工具分析）====================

    _MAX_POLL_ATTEMPTS: 60,   // 60 次 × 2s = 最多轮询 120 秒（在线/离线均计数，避免离线时无限轮询）

    _startPageCountPoll(fileIndex, fileId) {
      this._stopPageCountPoll(fileIndex)
      if (!fileId) return
      let attempts = 0

      const poll = () => {
        const token = wx.getStorageSync('token') || ''
        request({
          url: CONFIG.BASE_URL + '/api/file_page/' + fileId,
          method: 'GET',
          header: { 'Authorization': 'Bearer ' + token },
          success: (res) => {
            if (res.statusCode === 200 && res.data && res.data.success) {
              const pc = res.data.page_count || 0
              const verified = res.data.verified || false
              if (pc > 0 && verified) {
                // 页数已验证，更新文件数据并重新校验已有的页码范围
                const files = this.data.selectedFiles
                if (files[fileIndex] && files[fileIndex].fileId === fileId) {
                  const patch = {
                    ['selectedFiles[' + fileIndex + '].pageCount']: pc,
                    ['selectedFiles[' + fileIndex + '].pageCountStatus']: 'confirmed',
                  }
                  if (pc === 1) {
                    // 单页固定单面 + 范围行隐藏 → 清空历史范围输入，避免残留错误/旧页码
                    patch['selectedFiles[' + fileIndex + '].duplex'] = 'off'
                    patch['selectedFiles[' + fileIndex + '].rangeLines'] = [{ value: '', error: '' }]
                    patch['selectedFiles[' + fileIndex + '].pageRange'] = ''
                  }
                  this.setData(patch)
                  // _normalizeAndValidateRangeLines 末尾会 _refreshSinglePage → 单页判定/文本↔网格切换均重算列表高度
                  this._normalizeAndValidateRangeLines(fileIndex)
                }
                this._stopPageCountPoll(fileIndex)
                console.log('页数轮询成功: fileIndex=' + fileIndex + ', pages=' + pc)
                return
              }
              // 页数未验证 → 检查打印机是否在线
              const printerOnline = res.data.printer_online || false
              const files = this.data.selectedFiles
              if (files[fileIndex] && files[fileIndex].fileId === fileId) {
                const currentStatus = files[fileIndex].pageCountStatus
                if (!printerOnline) {
                  // 打印机离线 → 显示黄色警告；离线同样计数，避免无限轮询
                  if (currentStatus !== 'offline') {
                    this.setData({
                      ['selectedFiles[' + fileIndex + '].pageCountStatus']: 'offline',
                    })
                  }
                } else {
                  // 打印机在线 → 显示分析中
                  if (currentStatus !== 'analyzing') {
                    this.setData({
                      ['selectedFiles[' + fileIndex + '].pageCountStatus']: 'analyzing',
                    })
                  }
                }
                attempts++
                if (attempts >= this._MAX_POLL_ATTEMPTS) {
                  this._stopPageCountPoll(fileIndex)
                  console.log('页数轮询超时: fileIndex=' + fileIndex)
                }
              }
            }
          },
          fail: () => {
            // 网络错误 → 继续轮询
          }
        })
      }

      // 立即发第一次，之后每 5 秒一次（原 2s 过于频繁，多个未验证文件会触发 Nginx 限流）
      poll()
      this._pollTimers[fileIndex] = setInterval(poll, 5000)
    },

    _stopPageCountPoll(fileIndex) {
      if (this._pollTimers && this._pollTimers[fileIndex]) {
        clearInterval(this._pollTimers[fileIndex])
        delete this._pollTimers[fileIndex]
      }
    },

    // ==================== 每文件份数操作 ====================

    onFileCopiesMinus(e) {
      const index = e.currentTarget.dataset.index
      if (this.data.selectedFiles[index].excelWarning || this.data.selectedFiles[index].unsupportedFormat) return
      const v = this.data.selectedFiles[index].copies
      if (v > 1) {
        this.setData({ ['selectedFiles[' + index + '].copies']: v - 1 })
      }
    },

    onFileCopiesPlus(e) {
      const index = e.currentTarget.dataset.index
      if (this.data.selectedFiles[index].excelWarning || this.data.selectedFiles[index].unsupportedFormat) return
      const v = this.data.selectedFiles[index].copies
      if (v < 99) {
        this.setData({ ['selectedFiles[' + index + '].copies']: v + 1 })
      }
    },

    onFileCopiesChange(e) {
      const index = e.currentTarget.dataset.index
      if (this.data.selectedFiles[index].excelWarning || this.data.selectedFiles[index].unsupportedFormat) return
      const v = parseInt(e.detail.value, 10)
      this.setData({
        ['selectedFiles[' + index + '].copies']: isNaN(v) || v < 1 ? 1 : v > 99 ? 99 : v
      })
    },

    // ---- 页码范围 — 多行输入（对齐本地工具 RangeListWidget）----

    _parseSingleRange(text) {
      // 匹配 gui.py RangeListWidget._parse_range
      text = (text || '').trim()
      if (!text) return null
      if (text.indexOf('-') !== -1) {
        const parts = text.split('-')
        if (parts.length !== 2) return null
        const start = parseInt(parts[0], 10)
        const end = parseInt(parts[1], 10)
        if (isNaN(start) || isNaN(end)) return null
        if (start >= 1 && start < end) {
          const pages = new Set()
          for (let p = start; p <= end; p++) pages.add(p)
          return pages
        }
        return null
      } else {
        const v = parseInt(text, 10)
        if (isNaN(v) || v < 1) return null
        return new Set([v])
      }
    },

    onRangeLineInput(e) {
      const fileIndex = e.currentTarget.dataset.fileIndex
      const lineIndex = e.currentTarget.dataset.lineIndex
      const value = e.detail.value || ''
      const file = this.data.selectedFiles[fileIndex]
      if (!file || file.excelWarning || file.unsupportedFormat || file.isImage) return

      this.setData({
        ['selectedFiles[' + fileIndex + '].rangeLines[' + lineIndex + '].value']: value,
        ['selectedFiles[' + fileIndex + '].rangeLines[' + lineIndex + '].error']: '',
      })

      // 如果在最后一行输入了内容，自动追加新空行（带弹出动画 + 列表延伸）
      const lines = this.data.selectedFiles[fileIndex].rangeLines
      if (lineIndex === lines.length - 1 && value.trim()) {
        const newLines = lines.concat([{ value: '', error: '', entering: true }])
        this.setData({ ['selectedFiles[' + fileIndex + '].rangeLines']: newLines })
        // 新增范围行 → 卡片/列表高度延伸（每行增量已计入 _fileCardHeightRpx）
        this._recalcFileListHeight()
        this._scheduleMeasure(200)
        // 弹出动画播完清除 entering 标记，避免同一条目下次渲染重复动画
        setTimeout(() => {
          const f = this.data.selectedFiles[fileIndex]
          if (f && f.rangeLines && f.rangeLines.length === newLines.length) {
            this.setData({ ['selectedFiles[' + fileIndex + '].rangeLines[' + (newLines.length - 1) + '].entering']: false })
          }
        }, 350)
      }
    },

    onRangeLineBlur(e) {
      const fileIndex = e.currentTarget.dataset.fileIndex
      const file = this.data.selectedFiles[fileIndex]
      if (!file || file.excelWarning || file.unsupportedFormat || file.isImage) return
      this._normalizeAndValidateRangeLines(fileIndex)
    },

    // 有效选择是否恰好 1 页：整份 1 页，或有效范围解析出的页面集合只有 1 页。
    // true → 模式(单双面)行隐藏、提交强制单面。
    _computeSinglePage(file) {
      if (!file) return false
      if (file.isImage) return false   // 图片本无模式行，不参与
      if (file.pageCount === 1) return true
      const lines = file.rangeLines || []
      const pages = new Set()
      for (const line of lines) {
        const v = (line.value || '').trim()
        if (!v) continue
        const parsed = this._parseSingleRange(v)
        if (parsed) parsed.forEach(p => pages.add(p))
      }
      return pages.size === 1
    },

    // 同步 file.singlePage 并重算列表高度。
    // 无论单页判定是否变化都要重算：页数确认(文本↔网格模式切换)同样会改变卡片类型高度；
    // _recalcFileListHeight 目标不变时是 no-op，无副作用。
    _refreshSinglePage(fileIndex) {
      const file = this.data.selectedFiles[fileIndex]
      if (!file) return
      const singlePage = this._computeSinglePage(file)
      if (file.singlePage !== singlePage) {
        this.setData({ ['selectedFiles[' + fileIndex + '].singlePage']: singlePage })
      }
      this._recalcFileListHeight()
    },

    _normalizeAndValidateRangeLines(fileIndex) {
      const file = this.data.selectedFiles[fileIndex]
      if (!file) return
      const lines = file.rangeLines || [{value: '', error: ''}]
      const maxPages = file.pageCount || 0

      // 收集非空行，解析每行
      const entries = []
      for (const line of lines) {
        const v = (line.value || '').trim()
        if (!v) continue
        const pages = this._parseSingleRange(v)
        if (pages) {
          entries.push({ value: v, pages, error: '' })
        } else {
          entries.push({ value: v, pages: null, error: '格式错误（应为 1-5 或 7）' })
        }
      }

      // 超限检测
      if (maxPages > 0) {
        for (const e of entries) {
          if (e.pages && Math.max(...e.pages) > maxPages) {
            e.error = '超出总页数 ' + maxPages
            e.pages = null
          }
        }
      }

      // 重叠检测
      for (let i = 0; i < entries.length; i++) {
        if (!entries[i].pages) continue
        for (let j = i + 1; j < entries.length; j++) {
          if (!entries[j].pages) continue
          if ([...entries[i].pages].some(p => entries[j].pages.has(p))) {
            entries[i].error = '重叠: ' + entries[j].value
            entries[j].error = '重叠: ' + entries[i].value
            entries[i].pages = null
            entries[j].pages = null
          }
        }
      }

      // 按起始页排序
      entries.sort((a, b) => {
        if (!a.pages && !b.pages) return 0
        if (!a.pages) return 1
        if (!b.pages) return -1
        return Math.min(...a.pages) - Math.min(...b.pages)
      })

      // 重建 lines：排序后条目 + 一个底部空行
      const newLines = entries.map(e => ({ value: e.value, error: e.error }))
      newLines.push({ value: '', error: '' })

      // 合并有效范围
      const validParts = entries.filter(e => e.pages).map(e => e.value)
      const pageRange = validParts.join(',')

      this.setData({
        ['selectedFiles[' + fileIndex + '].rangeLines']: newLines,
        ['selectedFiles[' + fileIndex + '].pageRange']: pageRange,
      })
      this._refreshSinglePage(fileIndex)
    },

    // ==================== 页数网格选择弹窗（页数已知时替代文本输入） ====================

    _isExactOddSet(selected, total) {
      for (let n = 1; n <= total; n++) {
        if (selected.has(n) !== (n % 2 === 1)) return false
      }
      return true
    },
    _isExactEvenSet(selected, total) {
      for (let n = 1; n <= total; n++) {
        if (selected.has(n) !== (n % 2 === 0)) return false
      }
      return true
    },

    // 点击范围摘要按钮 → 弹出页数网格
    onOpenRangePicker(e) {
      const fileIndex = e.currentTarget.dataset.index
      const file = this.data.selectedFiles[fileIndex]
      if (!file || !(file.pageCount > 0)) return
      const total = file.pageCount
      // 当前选中：从 rangeLines 解析有效页面（空 → 全部未选 = 全部页）
      const selected = new Set()
      for (const line of (file.rangeLines || [])) {
        const v = (line.value || '').trim()
        if (!v) continue
        const parsed = this._parseSingleRange(v)
        if (parsed) parsed.forEach(p => selected.add(p))
      }
      const pages = []
      for (let n = 1; n <= total; n++) pages.push({ n, sel: selected.has(n) })
      this.setData({
        showRangePicker: true,
        rangePickerFileIndex: fileIndex,
        rangePickerTotal: total,
        rangePickerPages: pages,
        rangePickerSelAll: selected.size === total,
        rangePickerSelOdd: this._isExactOddSet(selected, total),
        rangePickerSelEven: this._isExactEvenSet(selected, total),
      })
    },

    // 点击数字：切换选中
    onRangePickerCell(e) {
      const n = Number(e.currentTarget.dataset.n)
      const pages = this.data.rangePickerPages.map(p => (p.n === n ? { n: p.n, sel: !p.sel } : p))
      const total = this.data.rangePickerTotal
      const selected = new Set(pages.filter(p => p.sel).map(p => p.n))
      this.setData({
        rangePickerPages: pages,
        rangePickerSelAll: selected.size === total,
        rangePickerSelOdd: this._isExactOddSet(selected, total),
        rangePickerSelEven: this._isExactEvenSet(selected, total),
      })
    },

    _rangePickerSelectBy(selFn) {
      const total = this.data.rangePickerTotal
      const pages = []
      for (let n = 1; n <= total; n++) pages.push({ n, sel: selFn(n) })
      const selected = new Set(pages.filter(p => p.sel).map(p => p.n))
      this.setData({
        rangePickerPages: pages,
        rangePickerSelAll: selected.size === total,
        rangePickerSelOdd: this._isExactOddSet(selected, total),
        rangePickerSelEven: this._isExactEvenSet(selected, total),
      })
    },

    onRangePickerAll() {
      this._rangePickerSelectBy(() => true)
    },
    // 单页 = 奇数页
    onRangePickerOdd() {
      this._rangePickerSelectBy(n => n % 2 === 1)
    },
    // 双页 = 偶数页
    onRangePickerEven() {
      this._rangePickerSelectBy(n => n % 2 === 0)
    },

    // 确定：把网格选中写回 rangeLines + pageRange，并重算 singlePage/列表高度
    onConfirmRangePicker() {
      const fileIndex = this.data.rangePickerFileIndex
      const file = this.data.selectedFiles[fileIndex]
      if (fileIndex < 0 || !file) {
        this.setData({ showRangePicker: false })
        return
      }
      const selected = this.data.rangePickerPages
        .filter(p => p.sel)
        .map(p => p.n)
        .sort((a, b) => a - b)
      const newLines = selected.map(n => ({ value: String(n), error: '' }))
      newLines.push({ value: '', error: '' })
      const pageRange = selected.join(',')
      this.setData({
        ['selectedFiles[' + fileIndex + '].rangeLines']: newLines,
        ['selectedFiles[' + fileIndex + '].pageRange']: pageRange,
        showRangePicker: false,
        rangePickerClosing: false,
      })
      // 单页判定变化（如只选 1 页）→ 模式行隐藏/显示 + 列表高度重算
      this._refreshSinglePage(fileIndex)
    },

    onCloseRangePicker() {
      this.setData({ rangePickerClosing: true })
      setTimeout(() => this.setData({ showRangePicker: false, rangePickerClosing: false }), 200)
    },

    loadPricing() {
      request({
        url: CONFIG.BASE_URL + '/api/pricing',
        method: 'GET',
        success: (res) => {
          if (res.statusCode === 200 && res.data && res.data.success) {
            const p = res.data.pricing
            this.setData({
              pricingLoaded: true,
              deliveryLocations: p.delivery_locations || this.data.deliveryLocations,
              deliveryPercentages: p.delivery_percentages || this.data.deliveryPercentages,
              urgencyOptions: p.urgency_levels || this.data.urgencyOptions,
              urgencyPrices: p.urgency_prices || this.data.urgencyPrices,
              coverPagePrice: p.cover_page_price != null ? p.cover_page_price : this.data.coverPagePrice,
              pickupAddress: p.pickup_address || this.data.pickupAddress,
            })
            // 刷新当前地点百分比显示
            const loc = this.data.deliveryLocation
            const updatedPct = (p.delivery_percentages || {})[loc]
            if (updatedPct != null) {
              this.setData({ deliveryPercent: updatedPct })
            }
          }
        },
        fail: () => {
          // 加载失败使用默认值，不阻塞
        }
      })
    },

    // ==================== 表单操作 ====================

    loadPrinterStatus() {
      request({
        url: CONFIG.BASE_URL + '/api/printer_status',
        method: 'GET',
        success: (res) => {
          if (res.data.success) {
            this.setData({ printerActive: res.data.active })
          }
        },
        fail: () => {
          this.setData({ printerActive: false })
        }
      })
    },

    onFileDuplexChange(e) {
      const index = e.currentTarget.dataset.index
      const value = e.currentTarget.dataset.value
      const f = this.data.selectedFiles[index]
      if (!f || f.excelWarning || f.unsupportedFormat) return
      if (f.pageCount === 1 || f.singlePage) return  // 整份1页/有效选择1页无法双面，固定单面
      this.setData({ ['selectedFiles[' + index + '].duplex']: value })
    },

    // 图片打印方向（仅图片）：auto=自动 / landscape=横向 / portrait=竖向
    onFileImageOrientation(e) {
      const index = e.currentTarget.dataset.index
      const value = e.currentTarget.dataset.value
      const f = this.data.selectedFiles[index]
      if (!f || !f.isImage) return
      this.setData({ ['selectedFiles[' + index + '].imageOrientation']: value })
    },

    onDuplexChange(e) {
      this.setData({ duplex: e.currentTarget.dataset.value })
    },

    // ==================== v5: 附加服务参数 ====================

    onToggleDelivery() {
      const next = !this.data.deliveryEnabled
      const loc = this.data.deliveryLocation
      this.setData({
        deliveryEnabled: next,
        deliveryPercent: next ? (this.data.deliveryPercentages[loc] || 0) : 0,
        showDeliveryPicker: false,  // 切换派送时关闭展开的地点列表
      })
      // 派送开关影响地点行 + 自取地址行，内容高度变化 → 刷新滚动边界
      this._scheduleMeasure()
      setTimeout(() => this._scheduleMeasure(400), 400)
    },

    onSelectDeliveryLocation(e) {
      const loc = e.currentTarget.dataset.loc
      const pct = this.data.deliveryPercentages[loc] || 0
      this.setData({
        deliveryLocation: loc,
        deliveryPercent: pct,
        showDeliveryPicker: false,
      })
      // 关闭地点选择器，内容高度变小 → 刷新滚动边界
      this._scheduleMeasure()
      setTimeout(() => this._scheduleMeasure(400), 400)
    },

    onToggleDeliveryPicker() {
      this.setData({ showDeliveryPicker: !this.data.showDeliveryPicker })
      // 展开/收起有 350ms 动画，动画完成后重新测量
      this._scheduleMeasure()
      setTimeout(() => this._scheduleMeasure(400), 400)
    },

    onSelectUrgency(e) {
      const urg = e.currentTarget.dataset.urg
      const price = this.data.urgencyPrices[urg] || 0
      this.setData({
        urgency: urg,
        urgencyPrice: price,
        showUrgencyPicker: false,
      })
      // 关闭优先级选择器，内容高度变小 → 刷新滚动边界
      this._scheduleMeasure()
      setTimeout(() => this._scheduleMeasure(400), 400)
    },

    onToggleUrgencyPicker() {
      this.setData({ showUrgencyPicker: !this.data.showUrgencyPicker })
      // 展开/收起有 350ms 动画，动画完成后重新测量
      this._scheduleMeasure()
      setTimeout(() => this._scheduleMeasure(400), 400)
    },

    onToggleCoverPage() {
      const turningOn = !this.data.coverPage
      this.setData({ coverPage: turningOn })
      if (turningOn) {
        // 打开：价格标签从右侧淡入
        this.setData({ coverPriceVisible: true, coverPriceEntering: true, coverPriceExiting: false })
        setTimeout(() => this.setData({ coverPriceEntering: false }), 350)
      } else {
        // 关闭：价格标签向左淡出，动画结束后移除
        this.setData({ coverPriceExiting: true, coverPriceEntering: false })
        setTimeout(() => {
          this.setData({ coverPriceVisible: false, coverPriceExiting: false })
        }, 300)
      }
      // 首页开关影响"首页费"行，内容高度变化 → 刷新滚动边界
      this._scheduleMeasure()
      setTimeout(() => this._scheduleMeasure(400), 400)
    },

    onPickupAddressInput(e) {
      this.setData({ pickupAddress: e.detail.value || '' })
    },

    onCoverPagePriceInput(e) {
      let v = parseFloat(e.detail.value)
      if (isNaN(v) || v < 0) v = 0
      if (v > 100) v = 100   // 首页费上限 100 元
      this.setData({ coverPagePrice: v })
    },

    // ==================== 提交任务 ====================

    onAutoPrintToggle() {
      const turningOn = !this.data.autoPrintEnabled
      this.setData({ autoPrintEnabled: turningOn })
      if (turningOn) {
        // 展开动画：先以 collapsed 初始态渲染，渲染完成后再切 expanded 触发 CSS 过渡
        if (this._scheduleLeaveTimer) { clearTimeout(this._scheduleLeaveTimer); this._scheduleLeaveTimer = null }
        // 选项行随面板整体展开（不单独播动画）：当前模式带选项则直接就位
        this.setData({
          scheduleVisible: true, scheduleAnim: 'collapsed',
          scheduleOptionsVisible: this.data.scheduleMode !== 'now',
          scheduleOptionsAnim: 'expanded',
        }, () => {
          this.setData({ scheduleAnim: 'expanded' })
        })
        // ⚡ 闪电发光 + Canvas 绘制折线电流
        // glowPhase 三态：'' | 'striking' | 'fading' | 'reset'（强制清除）
        this.setData({
          autoPrintGlow: true, glowPhase: 'striking',
          glowStyle: 'text-shadow: 0 0 30rpx #fff, 0 0 12rpx #ffe500, 0 0 6rpx #ff9500;',
        })
        setTimeout(() => {
          this._drawLightningBolts()
          // 350ms 后抖动/闪白结束 → JS 逐帧渐隐（iconShake 0.3s 已结束，shockRing 被隐式截断回到透明）
          setTimeout(() => {
            this.setData({ autoPrintGlow: false })
            this._fadeOutGlow()
          }, 350)
        }, 30)
      } else {
        // 收起动画：先切 collapsed 触发 CSS 过渡，动画结束后再卸载面板
        // （快速重新打开时清掉卸载定时器，避免面板被误卸载）
        this.setData({ scheduleAnim: 'collapsed', scheduleOptionsVisible: false, scheduleOptionsAnim: 'collapsed' })
        if (this._scheduleOptionsTimer) { clearTimeout(this._scheduleOptionsTimer); this._scheduleOptionsTimer = null }
        this._scheduleLeaveTimer = setTimeout(() => {
          this.setData({ scheduleVisible: false })
          this._scheduleLeaveTimer = null
        }, 320)
        // 关闭开关时立即清除光晕
        this._clearGlowCompletely()
        this._stopBreathingGlow()
      }
    },

    // 生成"今天(周一)/明天(周二)/后天(周三)"日期选项（仅保留 3 天）
    _buildScheduleDays() {
      const weekNames = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
      const labels = ['今天', '明天', '后天']
      const list = labels.map((label, i) => {
        const d = new Date()
        d.setDate(d.getDate() + i)
        return `${label}(${weekNames[d.getDay()]})`
      })
      this.setData({ scheduleDays: list })
    },

    // JS 逐帧渐隐 text-shadow（保持与初始 glowStyle 相同的颜色/层数结构，仅缩放 alpha+blur）
    _fadeOutGlow() {
      this.setData({ glowPhase: 'fading' })
      // 三组参数与初始 glowStyle 一一对应：
      //   rgba(255,255,255,?)  ← #fff     blur 30→0
      //   rgba(255,229,0,?)    ← #ffe500  blur 12→0
      //   rgba(255,149,0,?)    ← #ff9500  blur  6→0
      // 三路同步衰减，ease-out 曲线：α=(1-t)², blur=α×初始值
      const MAX = 8
      let step = 0
      const tick = () => {
        const t = step / MAX        // 0.0 → 1.0
        if (t >= 1) {
          this.setData({ glowPhase: 'reset', glowStyle: 'text-shadow: none !important;' })
          this._clearGlowCanvas()
          setTimeout(() => {
            this.setData({ glowPhase: '', glowStyle: '' })
            // 渐隐完毕且开关仍开 → 启动呼吸发光
            if (this.data.autoPrintEnabled) {
              this._startBreathingGlow()
            }
          }, 30)
          return
        }
        const a = (1 - t) * (1 - t)  // ease-out：先快后慢
        const w = Math.round(30 * a)
        const y = Math.round(12 * a)
        const o = Math.round(6 * a)
        this.setData({
          glowStyle: `text-shadow: 0 0 ${w}rpx rgba(255,255,255,${a.toFixed(3)}), 0 0 ${y}rpx rgba(255,229,0,${a.toFixed(3)}), 0 0 ${o}rpx rgba(255,149,0,${a.toFixed(3)});`,
        })
        step++
        setTimeout(tick, 60)
      }
      tick()
    },

    // 清除 Canvas 电流绘制残留
    _clearGlowCanvas() {
      const query = wx.createSelectorQuery()
      query.select('#boltCanvas').fields({ node: true, size: true }).exec((res) => {
        if (!res || !res[0] || !res[0].node) return
        const canvas = res[0].node
        const ctx = canvas.getContext('2d')
        ctx.clearRect(0, 0, canvas.width, canvas.height)
      })
    },

    // 完全清除所有光晕状态
    _clearGlowCompletely() {
      this._stopBreathingGlow()
      this.setData({ glowPhase: '', autoPrintGlow: false, glowStyle: '' })
      this._clearGlowCanvas()
    },

    // ── ON 状态呼吸发光 ──

    // 启动呼吸脉冲（ON 状态下常驻，~3s 周期正弦波）
    _startBreathingGlow() {
      this._stopBreathingGlow()
      let frame = 0
      this._breathTimer = setInterval(() => {
        frame++
        const t = (frame % 30) / 30            // 0.0 → 1.0，周期 30 帧 × 100ms = 3s
        const pulse = 0.5 + 0.5 * Math.sin(t * Math.PI * 2)  // 0.0 → 1.0 正弦
        const alpha = 0.12 + pulse * 0.58       // 0.12 → 0.70
        const blur = 3 + pulse * 10             // 3rpx → 13rpx
        this.setData({
          glowStyle: `text-shadow: 0 0 ${blur * 0.5}rpx rgba(255,255,255,${(alpha * 0.35).toFixed(3)}), 0 0 ${blur}rpx rgba(255,225,0,${alpha.toFixed(3)}), 0 0 ${blur * 2}rpx rgba(255,180,0,${(alpha * 0.5).toFixed(3)});`,
        })
      }, 100)
    },

    // 停止呼吸脉冲
    _stopBreathingGlow() {
      if (this._breathTimer) {
        clearInterval(this._breathTimer)
        this._breathTimer = null
      }
    },

    // Canvas 绘制闪电（随机方向+多级分支，告别均匀触手）
    _drawLightningBolts() {
      const query = wx.createSelectorQuery()
      query.select('#boltCanvas').fields({ node: true, size: true }).exec((res) => {
        if (!res || !res[0] || !res[0].node) return
        const canvas = res[0].node
        const dpr = wx.getWindowInfo().pixelRatio || 2
        const cssW = res[0].width || 100
        const cssH = res[0].height || 100
        canvas.width = cssW * dpr * 2
        canvas.height = cssH * dpr * 2
        const ctx = canvas.getContext('2d')
        ctx.scale(dpr * 2, dpr * 2)
        ctx.imageSmoothingEnabled = true

        const w = cssW, h = cssH, cx = w / 2, cy = h / 2

        const draw = (alpha) => {
          ctx.clearRect(0, 0, w, h)

          // 光晕层：径向渐变
          if (alpha > 0.04) {
            const grad = ctx.createRadialGradient(cx, cy, 1, cx, cy, w * 0.48)
            grad.addColorStop(0, `rgba(255,235,50,${(alpha * 0.40).toFixed(3)})`)
            grad.addColorStop(0.4, `rgba(255,210,0,${(alpha * 0.18).toFixed(3)})`)
            grad.addColorStop(1, 'rgba(255,180,0,0)')
            ctx.fillStyle = grad
            ctx.fillRect(0, 0, w, h)
          }

          ctx.globalAlpha = alpha

          // 2-3 条主干闪电，随机方向/长度
          const mainCount = 2 + Math.round(Math.random())
          for (let i = 0; i < mainCount; i++) {
            const angle = Math.random() * Math.PI * 2
            const len = 13 + Math.random() * 10
            const ex = cx + Math.cos(angle) * len
            const ey = cy + Math.sin(angle) * len
            this._paintFork(ctx, cx, cy, ex, ey, 10, 0, 1.0)
          }

          // 1-2 条次要闪电（更短、更细）
          const subCount = 1 + Math.round(Math.random())
          for (let i = 0; i < subCount; i++) {
            const angle = Math.random() * Math.PI * 2
            const len = 8 + Math.random() * 7
            const ex = cx + Math.cos(angle) * len
            const ey = cy + Math.sin(angle) * len
            this._paintFork(ctx, cx, cy, ex, ey, 8, 0, 0.7)
          }

          ctx.globalAlpha = 1
        }

        draw(1)
        let step = 0
        const steps = 6
        const timer = setInterval(() => {
          step++
          if (step > steps) { clearInterval(timer); return }
          draw(1 - step / steps)
        }, 80)
      })
    },

    // 递归闪电折线（中点位移 + 多级分支，模拟真实放电）
    // thickness 1.0=主干, <1.0=分支（继承上级粗细缩放）
    _paintFork(ctx, x1, y1, x2, y2, displace, depth, thickness) {
      if (displace < 1.5 || depth > 7) {
        ctx.beginPath()
        ctx.moveTo(x1, y1)
        ctx.lineTo(x2, y2)
        // 按深度和厚度调色：主干亮白，分支暖黄
        const t = thickness * (1 - depth * 0.07)
        const alpha = Math.max(0.25, t)
        const colors = [
          `rgba(255,255,255,${alpha.toFixed(3)})`,
          `rgba(255,235,50,${(alpha * 0.92).toFixed(3)})`,
          `rgba(255,200,0,${(alpha * 0.70).toFixed(3)})`,
          `rgba(255,150,50,${(alpha * 0.48).toFixed(3)})`,
        ]
        ctx.strokeStyle = colors[Math.min(depth, colors.length - 1)]
        ctx.lineCap = 'round'
        ctx.lineWidth = Math.max(0.4, thickness * (2.0 - depth * 0.18))
        ctx.stroke()
        return
      }
      // 中点随机偏移（偏移量×1.2 = 更锯齿、更凌厉）
      const jitter = displace * 1.2
      const midX = (x1 + x2) / 2 + (Math.random() - 0.5) * jitter
      const midY = (y1 + y2) / 2 + (Math.random() - 0.5) * jitter
      // 递归两段
      this._paintFork(ctx, x1, y1, midX, midY, displace * 0.55, depth + 1, thickness)
      this._paintFork(ctx, midX, midY, x2, y2, displace * 0.55, depth + 1, thickness)
      // 分叉分支（中级深度、较高概率）
      if (Math.random() < 0.35 && depth < 4 && depth > 0) {
        const bx = midX + (Math.random() - 0.5) * displace * 2.0
        const by = midY + (Math.random() - 0.5) * displace * 2.0
        this._paintFork(ctx, midX, midY, bx, by, displace * 0.3, depth + 2, thickness * 0.55)
      }
      // 尖端二次分叉（低级深度、较低概率）
      if (Math.random() < 0.15 && depth < 3 && depth > 1) {
        const tx = x2 + (Math.random() - 0.5) * displace * 1.5
        const ty = y2 + (Math.random() - 0.5) * displace * 1.5
        this._paintFork(ctx, x2, y2, tx, ty, displace * 0.2, depth + 3, thickness * 0.4)
      }
    },

    // ==================== 无障碍打印预约（开始方式） ====================

    onScheduleMode(e) {
      const mode = (e.currentTarget.dataset.mode || 'now')
      if (mode === this.data.scheduleMode) return  // 重复点击当前模式：无操作
      const hadOptions = this.data.scheduleMode !== 'now'
      const willHaveOptions = mode !== 'now'
      const refresh = () => {
        this._scheduleMeasure()
        setTimeout(() => this._scheduleMeasure(400), 400)
      }
      if (hadOptions && willHaveOptions) {
        // 收起旧选项行 → 切换模式 → 展开新选项行（两阶段，高度自然过渡）
        this.setData({ scheduleOptionsAnim: 'collapsed' })
        if (this._scheduleOptionsTimer) { clearTimeout(this._scheduleOptionsTimer); this._scheduleOptionsTimer = null }
        this._scheduleOptionsTimer = setTimeout(() => {
          this._scheduleOptionsTimer = null
          this.setData({ scheduleMode: mode, scheduleOptionsVisible: true, scheduleOptionsAnim: 'collapsed' }, () => {
            this.setData({ scheduleOptionsAnim: 'expanded' })
          })
          refresh()
        }, 260)
      } else if (hadOptions && !willHaveOptions) {
        // 收起旧选项行 → 卸载（切到"立即开始"）
        this.setData({ scheduleOptionsAnim: 'collapsed' })
        if (this._scheduleOptionsTimer) { clearTimeout(this._scheduleOptionsTimer); this._scheduleOptionsTimer = null }
        this._scheduleOptionsTimer = setTimeout(() => {
          this._scheduleOptionsTimer = null
          this.setData({ scheduleMode: mode, scheduleOptionsVisible: false, scheduleOptionsAnim: 'collapsed' })
          refresh()
        }, 260)
      } else {
        // 从"立即开始"切到带选项模式：直接展开
        this.setData({ scheduleMode: mode, scheduleOptionsVisible: true, scheduleOptionsAnim: 'collapsed' }, () => {
          this.setData({ scheduleOptionsAnim: 'expanded' })
        })
        refresh()
      }
    },

    // ── 自定义日期选择弹窗 ──

    onOpenScheduleDayPicker() {
      if (this.data.modalClosing) return
      this.setData({ showScheduleDayPicker: true })
    },

    onSelectScheduleDay(e) {
      if (this.data.modalClosing) return
      // 先播退出动画再关闭（与取消按钮一致的 sheetSpringOut）
      this.setData({
        scheduleDayIndex: Number(e.currentTarget.dataset.index),
        modalClosing: true,
      })
      setTimeout(() => {
        this.setData({ showScheduleDayPicker: false, modalClosing: false })
      }, 180)
    },

    onCloseScheduleDayPicker() {
      if (this.data.modalClosing) return
      this.setData({ modalClosing: true })
      setTimeout(() => {
        this.setData({ showScheduleDayPicker: false, modalClosing: false })
      }, 180)
    },

    // ── 自定义时间选择弹窗（官方 picker-view 双列滚轮） ──

    // 分钟列构建（数组元素即真实分钟字符串，索引仅用于 picker-view 定位）：
    // 非今天 → 00-59 全量；今天 → 选中小时为当前小时时仅 curMin+1..59，未来小时全量
    _buildMinuteItems(hourIndex, isToday, hourStart, minuteStart) {
      const full = Array.from({ length: 60 }, (_, i) => String(i).padStart(2, '0'))
      if (!isToday) return full
      if (hourStart + hourIndex > this.data.curHour) return full
      return Array.from({ length: 59 - this.data.curMin }, (_, i) => String(minuteStart + i).padStart(2, '0'))
    },

    onOpenScheduleTimePicker() {
      if (this.data.modalClosing) return
      const now = new Date()
      const curHour = now.getHours()
      const curMin = now.getMinutes()
      const isToday = this.data.scheduleDayIndex === 0
      // 今天：已过时间直接删除（不渲染）→ 分钟从 curMin+1 起；
      // curMin=59 时当前小时无可用分钟，小时列从 curHour+1 起
      let hourStart = 0
      let minuteStart = 0
      if (isToday) {
        minuteStart = curMin + 1
        if (minuteStart > 59) {
          minuteStart = 0
          hourStart = curHour + 1
          if (hourStart > 23) {
            wx.showToast({ title: '今天已无可用时间，请选择明天', icon: 'none', duration: 2500 })
            return
          }
        } else {
          hourStart = curHour
        }
      }
      const hourItems = Array.from({ length: 24 - hourStart }, (_, i) => String(hourStart + i).padStart(2, '0'))
      // 初始选中：已有选择且仍可用则保留，否则最早可用（小时列第 0 项）
      let hi = 0
      let mi = 0
      const t = (this.data.scheduleTime || '').match(/^(\d{1,2}):(\d{2})$/)
      if (t) {
        const selH = parseInt(t[1], 10)
        const selM = parseInt(t[2], 10)
        if (selH >= hourStart && selH <= 23 && selM <= 59) {
          hi = selH - hourStart
          const mStart = (isToday && selH === curHour) ? minuteStart : 0
          if (selM >= mStart) mi = selM - mStart
        }
      }
      const minuteItems = this._buildMinuteItems(hi, isToday, hourStart, minuteStart)
      if (mi >= minuteItems.length) mi = 0
      // value 由渲染层原生定位到选中项（系统 picker 同款引擎，精确居中）
      this.setData({
        showScheduleTimePicker: true,
        hourItems,
        minuteItems,
        hourWheelIndex: hi,
        minuteWheelIndex: mi,
        timeWheelValue: [hi, mi],
        curHour,
        curMin,
        hourStart,
        minuteStart,
      })
    },

    // picker-view 滚动变化：索引由组件原生给出（贴合后精确值）
    // 今天：小时变化 → 分钟列按新小时重建，分钟重置为该小时第一个可用分钟（级联）
    onTimeWheelChange(e) {
      const v = (e.detail && e.detail.value) || []
      if (v.length < 2) return
      const hi = v[0]
      const mi = v[1]
      if (this.data.scheduleDayIndex === 0 && hi !== this.data.hourWheelIndex) {
        const minuteItems = this._buildMinuteItems(hi, true, this.data.hourStart, this.data.minuteStart)
        this.setData({
          hourWheelIndex: hi,
          minuteWheelIndex: 0,
          minuteItems,
          timeWheelValue: [hi, 0],
        })
        return
      }
      this.setData({ hourWheelIndex: hi, minuteWheelIndex: mi })
    },

    onConfirmScheduleTime() {
      // 数组元素即真实时间字符串（已过时间已被裁剪，无需额外校验）
      const hh = this.data.hourItems[this.data.hourWheelIndex]
      const mm = this.data.minuteItems[this.data.minuteWheelIndex]
      if (!hh || !mm) {
        wx.showToast({ title: '请选择有效时间', icon: 'none', duration: 2000 })
        return
      }
      this.setData({ scheduleTime: `${hh}:${mm}` })
      this.onCloseScheduleTimePicker()
    },

    onCloseScheduleTimePicker() {
      if (this.data.modalClosing) return
      this.setData({ modalClosing: true })
      setTimeout(() => {
        this.setData({ showScheduleTimePicker: false, modalClosing: false })
      }, 180)
    },

    // ── 倒计时选择弹窗（分 + 秒，范围 00-59，复用"指定时间"同款滚轮引擎） ──

    onOpenScheduleCountdownPicker() {
      if (this.data.modalClosing) return
      const mi = Math.min(Math.max(parseInt(this.data.countdownMin, 10) || 0, 0), 59)
      const si = Math.min(Math.max(parseInt(this.data.countdownSec, 10) || 0, 0), 59)
      this.setData({
        showScheduleCountdownPicker: true,
        countdownMinWheelIndex: mi,
        countdownSecWheelIndex: si,
        countdownWheelValue: [mi, si],
      })
    },

    onCountdownWheelChange(e) {
      const v = (e.detail && e.detail.value) || []
      if (v.length < 2) return
      this.setData({
        countdownMinWheelIndex: v[0],
        countdownSecWheelIndex: v[1],
        countdownWheelValue: [v[0], v[1]],
      })
    },

    onConfirmScheduleCountdown() {
      const mi = this.data.countdownMinWheelIndex
      const si = this.data.countdownSecWheelIndex
      if (mi == null || si == null) {
        wx.showToast({ title: '请选择有效倒计时', icon: 'none', duration: 2000 })
        return
      }
      this.setData({ countdownMin: mi, countdownSec: si })
      this.onCloseScheduleCountdownPicker()
    },

    onCloseScheduleCountdownPicker() {
      if (this.data.modalClosing) return
      this.setData({ modalClosing: true })
      setTimeout(() => {
        this.setData({ showScheduleCountdownPicker: false, modalClosing: false })
      }, 180)
    },

    // 预约校验：返回错误文案（空串 = 通过）
    _validateSchedule() {
      if (!this.data.autoPrintEnabled) return ''
      const { scheduleMode } = this.data
      if (scheduleMode === 'at') {
        const time = (this.data.scheduleTime || '').trim()
        if (!time) return '请选择预约时间'
        const m = /^(\d{1,2}):(\d{2})$/.exec(time)
        if (!m) return '预约时间格式不正确'
        const hh = parseInt(m[1], 10)
        const mm = parseInt(m[2], 10)
        if (hh > 23 || mm > 59) return '预约时间格式不正确'
        // 客户端时钟不可信：不在此判断"预约时间已过"，由服务端校验，400 响应透传服务端 message
        return ''
      }
      if (scheduleMode === 'countdown') {
        const min = parseInt(this.data.countdownMin, 10) || 0
        const sec = parseInt(this.data.countdownSec, 10) || 0
        if (min > 59 || sec > 59) return '倒计时时长无效'
        if (min === 0 && sec === 0) return '倒计时时长必须大于 0'
        return ''
      }
      return ''
    },

    // 预约成功提示文案
    _scheduleDisplayText() {
      if (!this.data.autoPrintEnabled) return ''
      if (this.data.scheduleMode === 'now') return '立即开始打印'
      if (this.data.scheduleMode === 'at') {
        return `${this.data.scheduleDays[this.data.scheduleDayIndex]} ${this.data.scheduleTime} 开始打印`
      }
      const min = parseInt(this.data.countdownMin, 10) || 0
      const sec = parseInt(this.data.countdownSec, 10) || 0
      return `${min} 分 ${sec} 秒后开始打印`
    },

    onSubmit() {
      const { selectedFiles } = this.data

      // 访客拦截：角色未确定（''）或 guest 均拦截，避免登录竞态导致漏放
      if (this.data.userRole !== 'user' && this.data.userRole !== 'admin') {
        this.setData({ showAccessDeniedModal: true })
        return
      }

      if (!selectedFiles || selectedFiles.length === 0) {
        wx.showToast({ title: '请先选择打印文件', icon: 'none', duration: 2000 })
        return
      }

      // 检查是否有文件正在上传
      if (selectedFiles.some(f => f.uploading)) {
        wx.showToast({ title: '文件上传中，请稍候', icon: 'none', duration: 2000 })
        return
      }

      // 检查是否有可打印的文件（排除 Excel/PPT/CAD 等不支持格式）
      const printable = selectedFiles.filter(f => !f.excelWarning && !f.unsupportedFormat)
      const unsupportedCount = selectedFiles.length - printable.length
      if (printable.length === 0) {
        // 全部不支持 → 任务发起失败
        wx.showModal({
          title: '任务发起失败',
          content: '所选文件均为不支持的文件格式（如 Excel/PPT/CAD），无法打印。请移除后重新选择，或联系管理员。',
          showCancel: false,
          confirmText: '知道了',
        })
        return
      }
      if (unsupportedCount > 0) {
        // 部分不支持 → 提示将自动跳过
        this.setData({ showUnsupportedSkipModal: true, unsupportedSkipCount: unsupportedCount })
        return
      }

      // 检查是否有上传失败的文件
      if (selectedFiles.some(f => f.failed || !f.fileId)) {
        wx.showToast({ title: '有文件未上传成功，请重新选择', icon: 'none', duration: 2000 })
        return
      }

      // 检查所有份数有效
      for (let i = 0; i < selectedFiles.length; i++) {
        const f = selectedFiles[i]
        if (!f.copies || f.copies < 1) {
          wx.showToast({ title: `"${f.name}" 份数无效`, icon: 'none', duration: 2000 })
          return
        }
      }

      // 检查是否有设置了页码范围但页数未验证的文件
      const unverifiedFiles = selectedFiles.filter(f => {
        if (f.isImage || f.excelWarning || f.unsupportedFormat) return false
        if (!f.pageRange || !f.pageRange.trim()) return false  // 未设范围=打印全部，无需警告
        return (f.pageCount || 0) <= 0
      })
      if (unverifiedFiles.length > 0) {
        // 显示警告弹窗
        this.setData({ showPageCountWarning: true })
        return
      }

      // 检查所有页码范围语法错误（多行输入模式）
      for (let i = 0; i < selectedFiles.length; i++) {
        const f = selectedFiles[i]
        if (f.pageCount === 1) continue  // 单页文档：范围行已隐藏，忽略历史 rangeLines 错误
        const lines = f.rangeLines || []
        const hasError = lines.some(line => line.error)
        if (hasError) {
          wx.showToast({ title: `"${f.name}" 页码范围有误`, icon: 'none', duration: 2000 })
          return
        }
      }

      // 无障碍打印预约校验（指定时间已过 / 倒计时无效 → 拦截）
      const scheduleErr = this._validateSchedule()
      if (scheduleErr) {
        wx.showToast({ title: scheduleErr, icon: 'none', duration: 2500 })
        return
      }

      this._doSubmit(false)
    },

    // 确认强制提交（忽略页数未验证警告）
    onConfirmForceSubmit() {
      if (this.data.modalClosing) return   // 防连点（对齐 onAccessDeniedConfirm）
      this.setData({ modalClosing: true })
      setTimeout(() => {
        this.setData({ showPageCountWarning: false, modalClosing: false })
        this._doSubmit(true)
      }, 200)
    },

    // 取消强制提交，返回等待
    onCancelForceSubmit() {
      this.setData({ modalClosing: true })
      setTimeout(() => {
        this.setData({ showPageCountWarning: false, modalClosing: false })
      }, 200)
    },

    // 确认跳过不支持格式（Excel/PPT/CAD），提交其余可打印文件
    onConfirmSkipUnsupported() {
      if (this.data.modalClosing) return
      this.setData({ modalClosing: true })
      setTimeout(() => {
        this.setData({ showUnsupportedSkipModal: false, modalClosing: false })
        this._doSubmit(false)
      }, 200)
    },

    // 取消提交，返回修改选择
    onCancelSkipUnsupported() {
      this.setData({ modalClosing: true })
      setTimeout(() => {
        this.setData({ showUnsupportedSkipModal: false, modalClosing: false })
      }, 200)
    },

    _doSubmit(skipPageValidation) {
      if (this.data.submitting) return   // 防连点：提交进行中直接忽略
      const { selectedFiles } = this.data
      // 提交时跳过不支持格式（Excel/PPT/CAD），仅提交可打印文件
      const printableFiles = selectedFiles.filter(f => !f.excelWarning && !f.unsupportedFormat)

      this.setData({ submitting: true })
      wx.showLoading({ title: '提交中...' })

      const filesPayload = printableFiles.map(f => {
        // 从多行输入合并出 page_range（确保 blur 前的输入也不丢失）
        const lines = (f.rangeLines || []).filter(l => (l.value || '').trim() && !l.error)
        const range = lines.map(l => l.value.trim()).join(',')
        return {
          file_id: f.fileId,
          file: f.name,
          copies: Number(f.copies),
          // 单页文档范围行已隐藏 → 置空=打印全部（即第 1 页）
          page_range: (f.pageCount === 1) ? '' : (range || f.pageRange || ''),
          duplex: (f.isImage || f.pageCount === 1 || f.singlePage) ? 'off' : (f.duplex || 'on'),  // 图片/整份1页/有效选择1页固定单面
          image_orientation: f.isImage ? (f.imageOrientation || 'auto') : 'auto',
        }
      })

      request({
        url: CONFIG.BASE_URL + '/api/submit_order',
        method: 'POST',
        header: {
          'Authorization': 'Bearer ' + (wx.getStorageSync('token') || ''),
          'content-type': 'application/json'
        },
        data: {
          // 幂等键：后端按 client_request_id 对同一用户 10 分钟内去重（防提交失败重试造成重复订单）
          client_request_id: Date.now().toString(36) + Math.random().toString(36).slice(2, 10),
          duplex: this.data.duplex,
          files: filesPayload,
          // v5: 附加服务参数
          delivery_enabled: this.data.deliveryEnabled ? 1 : 0,
          delivery_location: this.data.deliveryLocation,
          delivery_percentage: this.data.deliveryPercent,
          urgency: this.data.urgency,
          urgency_price: this.data.urgencyPrice,
          cover_page: this.data.coverPage ? 1 : 0,
          cover_page_price: this.data.coverPagePrice,
          pickup_address: this.data.pickupAddress,
          skip_page_validation: skipPageValidation ? 1 : 0,
          auto_print: this.data.autoPrintEnabled ? 1 : 0,
          // 无障碍打印预约：now=立即 / at=指定时间（今天~后天 + HH:MM）/ countdown=倒计时（分+秒）
          schedule_mode: this.data.autoPrintEnabled ? this.data.scheduleMode : 'now',
          schedule_day: this.data.autoPrintEnabled && this.data.scheduleMode === 'at' ? this.data.scheduleDayIndex : 0,
          schedule_time: this.data.autoPrintEnabled && this.data.scheduleMode === 'at' ? this.data.scheduleTime : '',
          countdown_seconds: this.data.autoPrintEnabled && this.data.scheduleMode === 'countdown'
            ? (parseInt(this.data.countdownMin, 10) || 0) * 60 + (parseInt(this.data.countdownSec, 10) || 0)
            : 0,
        },
        success: (submitRes) => {
          wx.hideLoading()
          if (submitRes.statusCode === 401) {
            this.setData({ submitting: false })
            this.doLoginAndRetry(() => this.onSubmit())
            return
          }
          if (submitRes.statusCode !== 200 || !submitRes.data || !submitRes.data.success) {
            const msg = (submitRes.data && submitRes.data.message) || '服务器错误，请稍后重试'
            this.setData({ submitting: false })
            wx.showToast({ title: msg, icon: 'none', duration: 2500 })
            return
          }
          console.log('任务提交成功：', submitRes.data)
          this._lastOrderResult = submitRes.data
          this.setData({
            submitting: false,
            showSuccessModal: true,
            lastOrderNumber: submitRes.data.order_number || '',
            lastScheduleText: this._scheduleDisplayText(),
          })
          // 任务发起后自动收起"打印文件"列表（高度补间归零，返回初始状态）
          this._fileListPx = 0
          this._animateFileListHeight(0, 300)
          // 隐藏 tab 栏发丝线，让成功弹窗与 tab 栏融为一体
          try {
            const tabBar = this.getTabBar && this.getTabBar()
            if (tabBar) tabBar.setData({ hideBorder: true })
          } catch (e) { /* 兼容低版本 */ }
        },
        fail: (err) => {
          wx.hideLoading()
          console.error('任务提交失败：', err)
          this.setData({ submitting: false })
          // 网络失败时订单可能已创建成功（幂等键兜底），不引导立即重试
          wx.showToast({ title: '提交结果未知，请到"我的订单"确认后重试', icon: 'none', duration: 2500 })
        }
      })
    },

    // ---- 价格计算（复刻本地工具 calc_cost）----

    _calcCost(pageCount, copies, duplex) {
      const simplex = 0.2
      const duplexP = 0.3
      if (pageCount <= 0) return { cost: 0, formula: '?', known: false }

      if (duplex === 'on') {
        const pairs = Math.floor(pageCount / 2)
        const remainder = pageCount % 2
        let cost, innerFormula
        if (remainder === 0) {
          cost = pairs * duplexP
          innerFormula = pairs + '张×' + duplexP.toFixed(2)
        } else if (pairs === 0) {
          cost = remainder * simplex
          innerFormula = remainder + '张×' + simplex.toFixed(2)
        } else {
          cost = pairs * duplexP + remainder * simplex
          innerFormula = pairs + '张×' + duplexP.toFixed(2) + '+' + remainder + '张×' + simplex.toFixed(2)
        }
        const formula = copies > 1
          ? '(' + innerFormula + ')×' + copies + '份'
          : innerFormula
        return { cost: Math.round(cost * copies * 100) / 100, formula, known: true }
      } else {
        const innerFormula = pageCount + '张×' + simplex.toFixed(2)
        const formula = copies > 1
          ? '(' + innerFormula + ')×' + copies + '份'
          : innerFormula
        return { cost: Math.round(pageCount * simplex * copies * 100) / 100, formula, known: true }
      }
    },

    // ---- 复制价格（简略：仅金额，对齐本地工具 Ctrl+C）----

    onCopyPrice() {
      const d = this._lastOrderResult
      if (!d || !d.files) return
      const files = d.files
      // 附加服务参数优先取后端回显（d.data = 提交参数原样回显），避免界面状态在提交后变化导致复制价格失真
      const echo = d.data || {}
      const deliveryEnabled = echo.delivery_enabled != null ? !!Number(echo.delivery_enabled) : this.data.deliveryEnabled
      const deliveryPercent = echo.delivery_percentage != null ? Number(echo.delivery_percentage) : this.data.deliveryPercent
      const urgencyPrice = echo.urgency_price != null ? Number(echo.urgency_price) : this.data.urgencyPrice
      const coverPage = echo.cover_page != null ? !!Number(echo.cover_page) : this.data.coverPage
      const coverPagePrice = echo.cover_page_price != null ? Number(echo.cover_page_price) : this.data.coverPagePrice

      let baseTotal = 0
      let allKnown = true
      files.forEach(f => {
        const r = this._calcCost(f.page_count || 0, f.copies || 1, f.duplex || 'on')
        baseTotal += r.cost
        if (!r.known) allKnown = false
      })

      let total = baseTotal
      if (deliveryEnabled) {
        total += baseTotal * (deliveryPercent / 100)
      }
      total += urgencyPrice
      if (coverPage) total += coverPagePrice

      const orderNumber = d.order_number || ''
      const prefix = allKnown ? '' : '≈ '
      const amount = (orderNumber ? orderNumber + ' ' : '') + prefix + '¥' + total.toFixed(2)
      wx.setClipboardData({
        data: amount,
        success: () => wx.showToast({ title: '已复制价格', icon: 'success' })
      })
    },

    // ---- 复制详细价格（对齐本地工具 Ctrl+Shift+C）----

    onCopyDetailPrice() {
      const d = this._lastOrderResult
      if (!d || !d.files) return
      const files = d.files
      const orderNumber = d.order_number || ''
      // 附加服务参数优先取后端回显（d.data = 提交参数原样回显），避免界面状态在提交后变化导致复制价格失真
      const echo = d.data || {}
      const deliveryEnabled = echo.delivery_enabled != null ? !!Number(echo.delivery_enabled) : this.data.deliveryEnabled
      const deliveryLocation = echo.delivery_location != null ? echo.delivery_location : this.data.deliveryLocation
      const deliveryPercent = echo.delivery_percentage != null ? Number(echo.delivery_percentage) : this.data.deliveryPercent
      const urgency = echo.urgency != null ? echo.urgency : this.data.urgency
      const urgencyPrice = echo.urgency_price != null ? Number(echo.urgency_price) : this.data.urgencyPrice
      const coverPage = echo.cover_page != null ? !!Number(echo.cover_page) : this.data.coverPage
      const coverPagePrice = echo.cover_page_price != null ? Number(echo.cover_page_price) : this.data.coverPagePrice
      const lines = ['计费明细']
      if (orderNumber) lines.push(orderNumber)
      lines.push('─'.repeat(14))
      const allParts = []
      let baseTotal = 0
      let itemNum = 0

      files.forEach(f => {
        itemNum++
        const r = this._calcCost(f.page_count || 0, f.copies || 1, f.duplex || 'on')
        const name = f.file_name || '未知文件'
        const duplexLabel = f.duplex === 'on' ? '双面' : '单面'
        const rangeLabel = f.page_range ? f.page_range + '页' : '全部页'

        lines.push(itemNum + '. ' + name)
        lines.push('   ' + f.copies + '份 | ' + duplexLabel + ' | ' + rangeLabel)
        if (r.cost > 0) {
          lines.push('   ' + r.formula + '=¥' + r.cost.toFixed(2))
          allParts.push(r.cost.toFixed(2))
          baseTotal += r.cost
        } else {
          lines.push('   💰 ?')
        }
      })

      // 派送
      itemNum++
      if (deliveryEnabled) {
        const loc = deliveryLocation
        const pct = deliveryPercent
        const deliveryCost = baseTotal * (pct / 100)
        if (pct > 0 && deliveryCost > 0) {
          lines.push(itemNum + '. 派送：是 | ' + loc + ' ' + pct.toFixed(1) + '% | ￥' + deliveryCost.toFixed(2))
          allParts.push(deliveryCost.toFixed(2))
        } else {
          lines.push(itemNum + '. 派送：是 | ' + loc + '免费')
        }
      } else {
        lines.push(itemNum + '. 派送：否')
      }

      // 优先级
      itemNum++
      const urgPrice = urgencyPrice
      if (urgPrice > 0) {
        lines.push(itemNum + '. 优先级：' + urgency + ' | ￥' + urgPrice.toFixed(2))
        allParts.push(urgPrice.toFixed(2))
      } else {
        lines.push(itemNum + '. 优先级：' + urgency + ' | ￥0')
      }

      // 首页
      if (coverPage) {
        itemNum++
        lines.push(itemNum + '. 打印首页信息 | ' + coverPagePrice.toFixed(2))
        allParts.push(coverPagePrice.toFixed(2))
      }

      // 合计
      const totalSum = allParts.reduce((s, p) => s + parseFloat(p), 0)
      const formula = allParts.join('+') || '0'
      lines.push('─'.repeat(14))
      lines.push('💰合计: ' + formula + '=￥' + totalSum.toFixed(2))

      wx.setClipboardData({
        data: lines.join('\n'),
        success: () => wx.showToast({ title: '已复制详细价格', icon: 'success' })
      })
    },

    onCloseModal() {
      if (this.data.modalClosing) return
      this.setData({ modalClosing: true })
      setTimeout(() => {
        this.setData({
          showSuccessModal: false,
          modalClosing: false,
          selectedFiles: [],
          badgeCount: 0,
          scrollPadHeight: 0,
        })
        this._contentEst = 0
        // 恢复 tab 栏发丝线
        try {
          const tabBar = this.getTabBar && this.getTabBar()
          if (tabBar) tabBar.setData({ hideBorder: false })
        } catch (e) { /* 兼容低版本 */ }
        this._stopAllUploadTimers()
        this._stopAllPollTimers()
        this._stopBreathingGlow()
        this._scheduleMeasure()
      }, 200)
    },

    noop() {},
  },
})
