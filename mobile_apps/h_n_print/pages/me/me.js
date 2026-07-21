// me.js
const { CONFIG } = require('../../utils/config')

Component({
  data: {
    nickname: '',
    avatarUrl: '',
    isAdmin: false,
    isSuperAdmin: false,
    userRole: '',
    orders: [],
    loading: true,
    ordersPage: 1,
    ordersTotal: 0,
    ordersHasMore: false,
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
    },
    // 管理员：许可密钥 & 用户列表
    licenseMinutes: 1,
    generatedKey: '',
    generating: false,
    licensedUsers: [],
    // 许可密钥抽屉状态机：idle | opening | open
    keyAnimState: 'idle',
    keyExpired: false,
    countdownText: '',
    expiresAt: '',         // 后端返回的到期时间字符串 "YYYY-MM-DD HH:MM:SS"
    keySwipeX: 0,          // 左滑位移（px）
    keySwipeTransition: true,  // 是否启用 CSS 过渡（拖动时关闭，吸附时开启）
    // 许可密钥状态: unused | used_waiting | used_done
    keyStatus: 'unused',
    keyType: 'temp',
    // 兑换用户信息（used_by）
    usedByNickname: '',
    usedByAvatarUrl: '',
    usedByOpenid: '',
    // 关联订单（used_done 时）
    relatedOrderId: null,
    relatedOrderStatus: '',
    // 访客：兑换密钥
    redeemKey: '',
    redeeming: false,
    // 临时授权倒计时
    tempUntil: '',
    tempCountdownText: '',

    // 打印机在线状态
    printerOnline: false,
    printerCount: 0,
    // 管理员：许可密钥轮询定时器（内部状态，非响应式）
    _keyPollTimer: null,
    // 内部滚动位置（驱动 scroll-content 的 translateY）
    scrollTop: 0,
    // 任务卡展开状态: { [orderId]: true }
    expandedOrders: {},
    // 管理员：服务器存储统计
    storageStats: null,
    retentionDays: 7,
    retentionHours: 0,
    savingRetention: false,
    deletingAllFiles: false,
    // 超级管理员：管理员列表
    admins: [],
    adminsLoading: false,
    adminSwipeX: {},      // { openid: px }
    adminSwipeTransition: {},  // { openid: bool }
  },
  lifetimes: {
    attached() {
      this._initScrollEngine()
      this.loadProfile()
      this.loadUserRole()
      this.loadOrders()
    },
    detached() {
      this._destroyScrollEngine()
    },
  },
  pageLifetimes: {
    show() {
      this.loadUserRole()
      this.loadOrders()
      // 每次切回页面时刷新头像（可能在其他端更新了）
      this.loadProfile()
      // 管理员：刷新许可用户列表 + 恢复当前有效密钥（实现关闭再打开恢复显示）
      const cachedRole = wx.getStorageSync('userRole')
      if (cachedRole === 'admin') {
        this.loadLicensedUsers()
        this.loadActiveKey()
        this.loadStorageStats()
        // 超级管理员加载管理员列表
        if (this.data.isSuperAdmin) {
          this.loadAdmins()
        }
      }
      // 同步 tabBar 选中态（标准 WeChat 模式：每个页面主动更新）
      try {
        const tabBar = this.getTabBar && this.getTabBar()
        if (tabBar) {
          tabBar.setData({ selected: 1, 'list[0].active': false, 'list[1].active': true })
        }
      } catch (e) { /* 兼容低版本 */ }
      this._scheduleMeasure()
      // 页面切换后 DOM 可能尚未稳定，追加一次延迟测量
      setTimeout(() => this._scheduleMeasure(300), 300)
      // 开启订单状态定时轮询（每8秒）
      this._startOrderPolling()
    },
    hide() {
      this._stopOrderPolling()
    },
  },
  methods: {
    // 订单状态轮询（含打印机在线状态）
    _startOrderPolling() {
      this._stopOrderPolling()
      this._checkPrinterStatus()  // 立即检查一次
      this._orderPollTimer = setInterval(() => {
        this.loadOrders(1, false)
        this._checkPrinterStatus()
      }, 8000)
    },
    _stopOrderPolling() {
      if (this._orderPollTimer) {
        clearInterval(this._orderPollTimer)
        this._orderPollTimer = null
      }
    },
    _checkPrinterStatus() {
      const token = wx.getStorageSync('token')
      if (!token) return
      wx.request({
        url: CONFIG.BASE_URL + '/api/printer_status',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          if (res.data && res.data.success) {
            this.setData({ printerOnline: res.data.online, printerCount: res.data.count })
          }
        },
        fail: () => {}
      })
    },
    // ==================== 自定义橡皮筋滚动引擎 ====================
    // 与首页 index.js 同构，去掉 Logo 联动；新增 _scheduleMeasure
    // 以便在动态内容（任务/角色/许可用户）加载后刷新滚动上下界。

    _initScrollEngine() {
      this._y = 0
      this._minY = 0
      this._maxY = 0
      this._scrollerH = 0
      this._contentH = 0

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

      // 底部额外滚动留白，防止内容贴边或被 tabBar 遮挡
      this._bottomPad = 20

      // 许可密钥倒计时 / 左滑 运行时状态
      this._keyCountdownTimer = null
      this._keySwipeStartX = 0
      this._keySwipeStartY = 0
      this._keySwipeLastX = 0
      this._keySwipeHorizontal = false   // 本次触摸是否已锁定为水平
      this._swipeHorizontal = false      // 通知滚动引擎让出控制（卡片左滑中）
      this._deleteWidthPx = 70           // 删除按钮宽度（140rpx ≈ 70px @750 设计稿）

      // 初次测量（多次延迟以应对 swiper 布局稳定）
      setTimeout(() => this._measure(), 60)
      setTimeout(() => this._measure(), 400)
      setTimeout(() => this._measure(), 800)
    },

    _destroyScrollEngine() {
      this._cancelSchedule()
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

    _schedule(fn) {
      return setTimeout(fn, 16)
    },
    _cancelSchedule() {
      if (this._tick) {
        clearTimeout(this._tick)
        this._tick = null
      }
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
        // 内容变短导致当前超出新上界 → 平滑回弹归位（不跳变）
        if (this._y > this._maxY) {
          this._snapBack()
        } else {
          this._applyY()
        }
      })
    },

    _applyY() {
      this.setData({ scrollTop: this._renderY() })
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
      // 许可密钥卡片正在左滑 → 滚动引擎让出控制，避免上下抖动
      if (this._swipeHorizontal) return
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

    // ==================== 用户资料 ====================

    loadProfile() {
      const token = wx.getStorageSync('token')
      if (!token) {
        console.warn('[loadProfile] token 不存在，跳过')
        return
      }

      wx.request({
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

    onNicknameInput(e) {
      this.setData({ nickname: e.detail.value })
    },

    onNicknameSave(e) {
      const nickname = e.detail.value || ''
      if (!nickname) return

      const token = wx.getStorageSync('token')
      if (!token) return

      wx.request({
        url: CONFIG.BASE_URL + '/api/profile',
        method: 'POST',
        header: {
          'Authorization': 'Bearer ' + token,
          'content-type': 'application/json'
        },
        data: { nickname: nickname },
        success: (res) => {
          if (res.statusCode === 200 && res.data.success) {
            wx.setStorageSync('nickname', nickname)
          } else {
            console.error('[onNicknameSave] 服务器返回异常:', res.statusCode, res.data)
          }
        },
        fail: (err) => {
          console.error('[onNicknameSave] 网络请求失败:', err)
        }
      })
    },

    // ==================== 用户角色 ====================

    loadUserRole() {
      const token = wx.getStorageSync('token')
      if (!token) {
        console.warn('[loadUserRole] token 不存在，跳过')
        return
      }

      wx.request({
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
            })
            wx.setStorageSync('userRole', role)
            // 角色切换会改变 wx:if 区块，内容高度变化显著 → 刷新滚动边界
            this._scheduleMeasure()
            // 管理员加载许可用户列表 + 存储统计
            if (role === 'admin') {
              this.loadLicensedUsers()
              this.loadStorageStats()
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

    // ==================== 管理员：许可密钥 ====================

    onLicenseMinutesMinus() {
      const v = this.data.licenseMinutes
      if (v > 1) { this.setData({ licenseMinutes: v - 1 }) }
    },

    onLicenseMinutesPlus() {
      const v = this.data.licenseMinutes
      if (v < 10) { this.setData({ licenseMinutes: v + 1 }) }
    },

    onLicenseMinutesChange(e) {
      const v = parseInt(e.detail.value, 10)
      this.setData({
        licenseMinutes: isNaN(v) || v < 1 ? 1 : v > 10 ? 10 : v
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
      // 动画中拦截（防止抽屉展开的 340ms 内连点）
      if (this.data.keyAnimState === 'opening') return

      this.setData({ generating: true })
      wx.request({
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
            // 后端已自动作废旧密钥；前端用新数据覆盖
            const wasOpen = this.data.keyAnimState === 'open'
            this.setData({
              generatedKey: res.data.key,
              expiresAt: res.data.expires_at,
              keyExpired: false,
              keySwipeX: 0,
              keyStatus: 'unused',
              keyType: res.data.type || 'temp',
              usedByNickname: '',
              usedByAvatarUrl: '',
              usedByOpenid: '',
              relatedOrderId: null,
              relatedOrderStatus: '',
            })
            this._startCountdown()
            if (wasOpen) {
              // open 态再次生成：抽屉保持打开，仅内容刷新
              this._scheduleMeasure()
            } else {
              // 首次生成：从卡片下方抽出抽屉
              this._openKeyDrawer()
            }
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

    // 抽屉展开：opening → open（340ms 动画）
    _openKeyDrawer() {
      this.setData({ keyAnimState: 'opening' })
      setTimeout(() => {
        this.setData({ keyAnimState: 'open' })
        this._scheduleMeasure()
        this._startKeyPolling()
      }, 340)
    },

    // 抽屉收回：open → idle（320ms 动画），并清空密钥数据
    _closeKeyDrawer() {
      this._stopKeyPolling()
      this.setData({ keyAnimState: 'idle', keySwipeX: 0 })
      setTimeout(() => {
        this.setData({
          generatedKey: '', expiresAt: '', countdownText: '', keyExpired: false,
          keyStatus: 'unused', keyType: 'temp',
          usedByNickname: '', usedByAvatarUrl: '', usedByOpenid: '',
          relatedOrderId: null, relatedOrderStatus: '',
        })
        this._scheduleMeasure()
      }, 320)
      this._stopCountdown()
    },

    // 倒计时：以后端 expires_at 为唯一真相
    _startCountdown() {
      this._stopCountdown()
      const target = this._parseServerTime(this.data.expiresAt)
      if (!target) return

      const tick = () => {
        const remain = target - Date.now()
        if (remain > 0) {
          const totalSec = Math.ceil(remain / 1000)
          const m = Math.floor(totalSec / 60)
          const s = totalSec % 60
          this.setData({
            countdownText: m + ':' + (s < 10 ? '0' + s : s),
            keyExpired: false,
          })
        } else {
          // 过期
          this.setData({ countdownText: '已过期', keyExpired: true })
          this._stopCountdown()
        }
      }
      tick()  // 立即执行一次
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

    // 从后端恢复当前有效密钥（attached / pageLifetimes.show 调用）
    loadActiveKey() {
      const token = wx.getStorageSync('token')
      if (!token) return
      wx.request({
        url: CONFIG.BASE_URL + '/api/license/active',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          if (res.statusCode === 200 && res.data && res.data.success && res.data.active) {
            // 有未过期密钥 → 恢复抽屉显示
            const wasOpen = this.data.keyAnimState === 'open'
            this.setData({
              generatedKey: res.data.key,
              expiresAt: res.data.expires_at,
              keyExpired: false,
              keySwipeX: 0,
              keyStatus: res.data.status || 'unused',
              keyType: res.data.type || 'temp',
              usedByNickname: res.data.used_by_nickname || '',
              usedByAvatarUrl: res.data.used_by_avatar_url || '',
              usedByOpenid: res.data.used_by || '',
              relatedOrderId: res.data.order_id || null,
              relatedOrderStatus: res.data.order_status || '',
            })
            this._startCountdown()
            if (!wasOpen) this._openKeyDrawer()
          } else {
            // 无有效密钥 → 确保抽屉关闭
            if (this.data.keyAnimState !== 'idle') {
              this._closeKeyDrawer()
            }
          }
        }
      })
    },

    // ==================== 许可密钥左滑删除 ====================

    onKeyTouchStart(e) {
      const t = e.touches[0]
      if (!t) return
      this._keySwipeStartX = t.clientX
      this._keySwipeStartY = t.clientY
      this._keySwipeLastX = t.clientX
      this._keySwipeHorizontal = false
      this._swipeHorizontal = false
      // 左滑开始时关闭 CSS 过渡，让位移跟手
      this.setData({ keySwipeTransition: false })
    },

    onKeyTouchMove(e) {
      const t = e.touches[0]
      if (!t) return
      const dx = t.clientX - this._keySwipeStartX
      const dy = t.clientY - this._keySwipeStartY

      // 方向锁定：首次显著位移时判定
      if (!this._keySwipeHorizontal) {
        if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return  // 尚未定向
        if (Math.abs(dx) > Math.abs(dy)) {
          // 水平主导 → 锁定左滑，通知滚动引擎让出
          this._keySwipeHorizontal = true
          this._swipeHorizontal = true
        } else {
          // 垂直主导 → 不拦截，让滚动引擎处理；本手势退出
          this._keySwipeHorizontal = false
          this._swipeHorizontal = false
          return
        }
      }

      // 水平锁定后：仅允许向左滑（dx<0），向右归位不超过 0
      let x = dx
      if (x > 0) x = 0
      // 到达删除按钮宽度后硬限位（不再继续左滑）
      if (x < -this._deleteWidthPx) x = -this._deleteWidthPx
      this._keySwipeLastX = x
      this.setData({ keySwipeX: x })
    },

    onKeyTouchEnd(e) {
      if (!this._keySwipeHorizontal) {
        // 未进入水平滑动：复位让滚动引擎接管（已自行处理）
        this._swipeHorizontal = false
        return
      }
      // 启用过渡，吸附到目标位置
      this.setData({ keySwipeTransition: true })
      // 超过删除按钮一半 → 吸附露出删除；否则回弹归位
      const target = this._keySwipeLastX < -this._deleteWidthPx / 2
        ? -this._deleteWidthPx
        : 0
      this.setData({ keySwipeX: target })
      // 释放滚动引擎控制
      this._swipeHorizontal = false
      this._keySwipeHorizontal = false
    },

    // 点击删除按钮：先右滑归位，再上滑收回抽屉
    onRevokeKey() {
      const token = wx.getStorageSync('token')
      if (!token) return

      wx.showModal({
        title: '作废密钥',
        content: '确定作废当前许可密钥？作废后他人将无法使用。',
        confirmText: '作废',
        confirmColor: '#ff4d4f',
        success: (modal) => {
          if (!modal.confirm) return
          wx.request({
            url: CONFIG.BASE_URL + '/api/license/revoke',
            method: 'POST',
            header: { 'Authorization': 'Bearer ' + token, 'content-type': 'application/json' },
            success: (res) => {
              if (res.data && res.data.success) {
                wx.showToast({ title: '已作废', icon: 'success' })
                // 第一阶段：卡片右滑归位（250ms）
                this.setData({ keySwipeTransition: true, keySwipeX: 0 })
                // 第二阶段：抽屉上滑收回（与右滑重叠，体现"归位同时抽回"）
                setTimeout(() => {
                  this._closeKeyDrawer()
                }, 120)
              } else {
                wx.showToast({ title: res.data.message || '作废失败', icon: 'none' })
              }
            },
            fail: () => {
              wx.showToast({ title: '网络错误', icon: 'none' })
            }
          })
        }
      })
    },

    onCopyKey() {
      // 用实际剩余时间（而非 licenseMinutes）生成文案
      const remain = this.data.countdownText || (this.data.licenseMinutes + ':00')
      const text = '这是HN同学的打印机的使用许可密钥，剩余有效时间' + remain + '，请在有效期内填写到小程序的指定位置:\n密钥: ' + this.data.generatedKey
      wx.setClipboardData({
        data: text,
        success: () => {
          wx.showToast({ title: '已复制到剪贴板', icon: 'success' })
        }
      })
    },

    // 结束打印任务：查询订单价格详情并复制
    onEndPrintTask() {
      const orderId = this.data.relatedOrderId
      if (!orderId) {
        wx.showToast({ title: '未找到关联订单', icon: 'none' })
        return
      }
      const token = wx.getStorageSync('token')
      if (!token) return

      wx.showLoading({ title: '获取订单详情...' })
      wx.request({
        url: CONFIG.BASE_URL + '/api/order_price/' + orderId,
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          wx.hideLoading()
          if (res.data && res.data.success) {
            const d = res.data
            const files = d.files || []
            const username = this.data.usedByNickname || '用户'
            let text = '【打印任务结算】\n用户: ' + username
            if (d.is_free) {
              text += '\n总价: 免费'
            } else {
              files.forEach((f, i) => {
                const unitPrice = (typeof f.per_copy_price === 'number') ? f.per_copy_price : 0
                const fileTotal = (typeof f.total_price === 'number') ? f.total_price : 0
                text += '\n文件' + (i + 1) + ': ' + f.file_name
                text += ' | ' + f.copies + '份 × ' + f.page_count + '页'
                text += ' | 单价: ¥' + unitPrice.toFixed(2)
                text += ' | 小计: ¥' + fileTotal.toFixed(2)
              })
              text += '\n总价: ¥' + (typeof d.total_price === 'number' ? d.total_price : 0).toFixed(2)
            }
            wx.setClipboardData({
              data: text,
              success: () => {
                wx.showToast({ title: '已复制结算详情', icon: 'success' })
              }
            })
          } else {
            wx.showToast({ title: (res.data && res.data.message) || '获取失败', icon: 'none' })
          }
        },
        fail: () => {
          wx.hideLoading()
          wx.showToast({ title: '网络错误', icon: 'none' })
        }
      })
    },

    // 确认管理员密钥生效（关闭抽屉）
    onConfirmAdminKey() {
      const token = wx.getStorageSync('token')
      if (!token) return
      wx.request({
        url: CONFIG.BASE_URL + '/api/license/finish',
        method: 'POST',
        header: { 'Authorization': 'Bearer ' + token, 'content-type': 'application/json' },
        data: { key: this.data.generatedKey },
        success: (res) => {
          if (res.data && res.data.success) {
            wx.showToast({ title: '已确认', icon: 'success' })
            this._closeKeyDrawer()
          } else {
            wx.showToast({ title: (res.data && res.data.message) || '操作失败', icon: 'none' })
          }
        },
        fail: () => {
          wx.showToast({ title: '网络错误', icon: 'none' })
        }
      })
    },

    // ==================== 超级管理员：管理员列表 ====================

    loadAdmins() {
      const token = wx.getStorageSync('token')
      if (!token) return
      this.setData({ adminsLoading: true })
      wx.request({
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
        horizontal: false,
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
        if (Math.abs(dx) > Math.abs(dy)) { sd.horizontal = true }
        else { return }
      }
      let x = dx
      if (x > 0) x = 0
      const maxX = -140  // rpx ≈ 70px
      if (x < maxX) x = maxX
      sd.lastX = x
      const swipeX = { ...this.data.adminSwipeX }
      swipeX[openid] = x
      this.setData({ adminSwipeX: swipeX })
    },

    onAdminTouchEnd(e) {
      const openid = e.currentTarget.dataset.openid
      const sd = this._adminSwipeData && this._adminSwipeData[openid]
      if (!sd || !sd.horizontal) return
      const trans = { ...this.data.adminSwipeTransition }
      trans[openid] = true
      const target = sd.lastX < -35 ? -140 : 0
      const swipeX = { ...this.data.adminSwipeX }
      swipeX[openid] = target
      this.setData({ adminSwipeX: swipeX, adminSwipeTransition: trans })
    },

    onRemoveAdmin(e) {
      const openid = e.currentTarget.dataset.openid
      const token = wx.getStorageSync('token')
      if (!token) return
      wx.showModal({
        title: '移除管理员',
        content: '确定要移除该管理员吗？',
        confirmText: '移除',
        confirmColor: '#ff4d4f',
        success: (modal) => {
          if (!modal.confirm) return
          wx.request({
            url: CONFIG.BASE_URL + '/api/admin/remove_admin',
            method: 'POST',
            header: { 'Authorization': 'Bearer ' + token, 'content-type': 'application/json' },
            data: { openid: openid },
            success: (res) => {
              if (res.data && res.data.success) {
                wx.showToast({ title: '已移除', icon: 'success' })
                this.loadAdmins()
              } else {
                wx.showToast({ title: res.data.message || '移除失败', icon: 'none' })
              }
            },
            fail: () => {
              wx.showToast({ title: '网络错误', icon: 'none' })
            }
          })
        }
      })
    },

    // F10: 跳转历史授权用户页面
    onGoAuthorizedUsers() {
      wx.navigateTo({ url: '/pages/authorized-users/authorized-users' })
    },

    // F12: 跳转本地打印任务列表（通过 source=local 过滤）
    onGoLocalOrders() {
      // 复用订单列表页面但加 source=local 参数
      wx.showToast({ title: '本地打印任务功能开发中', icon: 'none' })
    },

    loadStorageStats() {
      const token = wx.getStorageSync('token')
      if (!token) return

      wx.request({
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
      wx.request({
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
      wx.showModal({
        title: '⚠️ 确认删除',
        content: '将删除服务器及本地打印工具的全部缓存文件（不包括用户头像），此操作不可撤销。确定继续？',
        confirmText: '确认删除',
        confirmColor: '#FF3B30',
        success: (modal) => {
          if (!modal.confirm) return
          const token = wx.getStorageSync('token')
          if (!token) return

          this.setData({ deletingAllFiles: true })
          wx.request({
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
      })
    },

    // 管理员许可密钥轮询：抽屉打开时每 5 秒刷新状态
    _startKeyPolling() {
      this._stopKeyPolling()
      this._pollKeyTick = () => {
        if (this.data.keyAnimState !== 'open') {
          this._stopKeyPolling()
          return
        }
        this.loadActiveKey()
      }
      this._keyPollTimer = setInterval(this._pollKeyTick, 5000)
    },

    _stopKeyPolling() {
      if (this._keyPollTimer) {
        clearInterval(this._keyPollTimer)
        this._keyPollTimer = null
      }
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

    loadLicensedUsers() {
      const token = wx.getStorageSync('token')
      if (!token) {
        console.log('[loadLicensedUsers] token 不存在，跳过')
        return
      }

      console.log('[loadLicensedUsers] 正在请求用户列表...')
      wx.request({
        url: CONFIG.BASE_URL + '/api/admin/users',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          console.log('[loadLicensedUsers] 响应:', res.statusCode, JSON.stringify(res.data))
          if (res.data && res.data.success) {
            this.setData({ licensedUsers: res.data.users || [] })
            console.log('[loadLicensedUsers] 加载到 ' + (res.data.users || []).length + ' 个用户')
            this._scheduleMeasure()
          } else {
            console.error('[loadLicensedUsers] 接口返回失败:', res.data)
          }
        },
        fail: (err) => {
          console.error('[loadLicensedUsers] 请求失败:', err)
        }
      })
    },

    // ==================== 访客：兑换许可密钥 ====================

    onRedeemKeyInput(e) {
      this.setData({ redeemKey: e.detail.value })
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
      wx.request({
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

    loadOrders(page = 1, append = false) {
      const token = wx.getStorageSync('token')
      if (!token) {
        this.setData({ loading: false })
        return
      }

      // 初次加载显示 loading，追加加载不显示
      if (!append) {
        this.setData({ loading: true, ordersPage: 1 })
      }

      wx.request({
        url: CONFIG.BASE_URL + '/api/orders',
        method: 'GET',
        header: {
          'Authorization': 'Bearer ' + token
        },
        data: { page: page, per_page: 20 },
        success: (res) => {
          if (res.statusCode === 200 && res.data.success) {
            // 预处理文件大小显示（WXML 不支持 .toFixed()）
            const newOrders = (res.data.orders || [])
            newOrders.forEach(order => {
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
            const allOrders = append
              ? [...this.data.orders, ...newOrders]
              : newOrders

            this.setData({
              orders: allOrders,
              loading: false,
              ordersPage: page,
              ordersTotal: total,
              ordersHasMore: allOrders.length < total,
              expandedOrders: append ? this.data.expandedOrders : {},
            })
          } else {
            this.setData({ loading: false })
          }
          this._scheduleMeasure()
        },
        fail: (err) => {
          console.error('[loadOrders] 网络请求失败:', err)
          this.setData({ loading: false })
          this._scheduleMeasure()
        }
      })
    },

    onLoadMoreOrders() {
      const nextPage = this.data.ordersPage + 1
      this.loadOrders(nextPage, true)
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
        }
      })
    },

    // ==================== 导航 ====================

    onGoMyPerformance() {
      wx.navigateTo({
        url: '/pages/my-performance/my-performance'
      })
    },
  },
})
