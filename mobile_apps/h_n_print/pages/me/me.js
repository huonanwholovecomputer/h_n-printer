// me.js
const { CONFIG } = require('../../utils/config')
const { request } = require('../../utils/request')

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
    nickname: '',
    avatarUrl: '',
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
    // 分页
    ordersCurrentPage: 1,
    ordersPerPage: 10,
    ordersTotal: 0,
    ordersTotalPages: 0,
    // 页码数组（预计算进 data，避免 WXML 内调用方法 + wx:key="*this" 在部分基础库下渲染失败）
    ordersPageNumbers: [],
    pageOptions: [10, 20, 50, 100],
    showPageSizePicker: false,
    pageSizeDropdownLeft: 0,   // 分页下拉 fixed 定位（viewport 坐标）
    pageSizeDropdownTop: 0,
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
    scrollConfig: { minY: 0, maxY: 0, scrollerH: 0, contentH: 0, listOverflow: false, closePicker: true },
    scrollCmd: null,        // WXS 程序化滚动命令（回顶 / 滚到订单区）
    // 任务卡展开状态: { [orderId]: true }
    expandedOrders: {},
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
          animationClass = 'page-enter-left'
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
      if (!this._lastDataLoad || (_now - this._lastDataLoad) > 60000) {
        this._lastDataLoad = _now
        this.loadUserRole()
        this.loadOrders()
        this.loadProfile()
        this.loadBindDeviceCount()
        const cachedRole = wx.getStorageSync('userRole')
        if (cachedRole === 'admin') {
          this.loadTempUsers()
          this.loadActiveKey()
          this.loadStorageStats()
          this.loadSecurityConfig()
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
      return true
    },

    // 带退出动画的子页面导航（防连点锁）
    _navigateWithAnimation(url) {
      if (this._navigating) return
      this._navigating = true
      this.setData({ pageExit: 'page-exit-left' })
      wx.setStorageSync('_navForward', '1')
      wx.setStorageSync('_meReturnFromSub', '1')
      setTimeout(() => {
        wx.navigateTo({
          url,
          complete: () => { this._navigating = false }
        })
      }, 280)
    },

    // 订单状态轮询（静默增量更新，不触发全量渲染）；15s 避免高频请求触发 Nginx 限流
    _startOrderPolling() {
      this._stopOrderPolling()
      this._orderPollTimer = setInterval(() => {
        this._pollOrdersSilent()
      }, 15000)
    },
    _pollOrdersSilent() {
      const token = wx.getStorageSync('token')
      if (!token) return
      request({
        url: CONFIG.BASE_URL + '/api/orders',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        data: { page: this.data.ordersCurrentPage, per_page: this.data.ordersPerPage },
        success: (res) => {
          if (res.statusCode !== 200 || !res.data || !res.data.success) return
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
              // 同步更新子文件状态
              if (n.files && o.files) {
                for (let j = 0; j < Math.min(n.files.length, o.files.length); j++) {
                  if (n.files[j].status !== o.files[j].status) {
                    updates['orders[' + i + '].files[' + j + '].status'] = n.files[j].status
                  }
                }
              }
            }
          }
          if (changed) {
            // 结构变化（增删订单），做全量刷新但保留展开状态
            newOrders.forEach(order => {
              order.totalPriceDisplay = (order.total_price || 0).toFixed(2)
            })
            this.setData({ orders: newOrders, expandedOrders: this.data.expandedOrders })
          } else if (Object.keys(updates).length > 0) {
            this.setData(updates)
          }
        },
        fail: () => {}
      })
    },
    _stopOrderPolling() {
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
      this.setData({
        scrollConfig: {
          minY: 0,
          maxY: Math.round(this._maxY || 0),
          scrollerH: this._scrollerH || 0,
          contentH: this._contentH || 0,
          listOverflow: false,
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
        this._scrollerH = vp
        this._contentH = ch
        this._maxY = Math.max(0, ch - vp + this._bottomPad + this._tabOverlayPx)
        this._pushScrollConfig()
      })
    },

    noop() {},

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
          that.setData({ avatarUrl })
          that.uploadAvatar(avatarUrl)
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
                that.setData({ avatarUrl: downloadRes.tempFilePath })
                that.uploadAvatar(downloadRes.tempFilePath)
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
      const type = e.currentTarget.dataset.type
      if (type === 'admin' || type === 'temp') {
        this.setData({ keyType: type })
      }
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
    loadActiveKey(isInitial) {
      const token = wx.getStorageSync('token')
      if (!token) return
      request({
        url: CONFIG.BASE_URL + '/api/license/active',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          if (res.statusCode !== 200 || !res.data || !res.data.success) return
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
        },
        fail: () => {},
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
              text += ' | ' + f.copies + '份 × ' + f.page_count + '页'
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
          const nickname = encodeURIComponent(admin.nickname || '')
          this._navigateWithAnimation(
            `/pages/user-orders/user-orders?openid=${openid}&nickname=${nickname}`
          )
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

    // F12: 跳转本地打印任务列表（通过 source=local 过滤）
    onGoLocalOrders() {
      this._navigateWithAnimation('/pages/user-orders/user-orders?source=local')
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

    // 管理员许可密钥轮询：抽屉打开时每 5 秒刷新状态
    _startKeyPolling() {
      this._stopKeyPolling()
      this._keyPollTimer = setInterval(() => {
        this.loadActiveKey(false)
        // 同步刷新管理员列表（头像等）
        if (this.data.isSuperAdmin) {
          this._pollAdminsSilent()
        }
      }, 15000)
    },

    _stopKeyPolling() {
      if (this._keyPollTimer) {
        clearInterval(this._keyPollTimer)
        this._keyPollTimer = null
      }
    },

    // 管理员列表静默轮询（仅更新变化字段，不触发全量渲染）
    _pollAdminsSilent() {
      const token = wx.getStorageSync('token')
      if (!token) return
      request({
        url: CONFIG.BASE_URL + '/api/admin/admins',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        data: { page: 1, page_size: 50 },
        success: (res) => {
          if (!res.data || !res.data.success) return
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
        },
        fail: () => {}
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
            const firstLoad = this.data.orders.length === 0 && newOrders.length > 0
            this.setData({
              orders: newOrders,
              loading: false,
              loadError: '',
              ordersTotal: total,
              ordersTotalPages: Math.ceil(total / this.data.ordersPerPage),
              expandedOrders: {},
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
      this.setData({ expandedOrders: expanded })
      // 详情展开/收起有 250ms 动画，测量需等动画完成
      this._scheduleMeasure()
      setTimeout(() => this._scheduleMeasure(300), 300)
    },

    onDetailCancelOrder(e) {
      const orderId = e.currentTarget.dataset.id
      const token = wx.getStorageSync('token')
      if (!token) return
      this._showConfirm('确认取消', '确定要取消这个打印任务吗？', '取消订单', '#FF9500', () => {
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
              this.loadOrders()
            } else {
              wx.showToast({ title: res.data.message, icon: 'none' })
            }
          },
          fail: () => {
            wx.hideLoading()
            wx.showToast({ title: '网络错误', icon: 'none' })
          }
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
