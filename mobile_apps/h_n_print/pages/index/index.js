// index.js
const { CONFIG } = require('../../utils/config')

Component({
  data: {
    // 多文件列表：每项 { name, size, path, fileId, uploading, progress, failed, copies }
    selectedFiles: [],
    duplex: 'on',
    printerActive: false,
    showSuccessModal: false,
    showAccessDeniedModal: false,
    showPageCountWarning: false,   // 页数未验证警告弹窗
    userRole: '',
    submitting: false,
    autoPrintEnabled: false,   // 无障碍打印开关（仅管理员可见）
    autoPrintGlow: false,      // ⚡ 闪电发光特效
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
    badgeEntering: false,   // 圆点入场动画（首文件）
    badgeBouncing: false,   // 圆点弹跳动画（已有文件时新增）
    badgeExiting: false,    // 圆点退场动画（末文件删除）
    badgeCount: 0,          // 延迟更新的计数，统一 0.25s 滞后于 selectedFiles.length
    btnPulse: false,        // 添加按钮脉冲动画（文字变化时）
    pageReady: false,         // 首次打开的入场动效
    pageExit: '',             // 退出动画: page-exit-left / page-exit-right
    pageSlide: 'page-init',   // 入场动画: page-fade-in / page-enter-left（初始隐藏防闪烁）
  },
  lifetimes: {
    attached() {
      // 首次启动清理残留的转场标记，防止热重载/缓存触发误动画
      wx.removeStorageSync('_tabFrom')
      wx.removeStorageSync('_tabTo')
      this._initScrollEngine()
      this._uploadTimers = {}   // { index: intervalId } — 每个文件独立的进度条定时器
      this._pollTimers = {}     // { index: intervalId } — 页数轮询定时器
      this.doLogin()
    },
    detached() {
      this._destroyScrollEngine()
      this._stopAllUploadTimers()
      this._stopAllPollTimers()
    },
  },
  pageLifetimes: {
    show() {
      // 系统对话框返回 → 跳过入场动画，直接恢复数据刷新
      if (this._returningFromDialog) {
        this._returningFromDialog = false
        this.loadPrinterStatus()
        this._startPrinterPolling()
        this._scheduleMeasure()
        setTimeout(() => this._scheduleMeasure(300), 300)
        return
      }
      // 两步入场动画：先强制重置为隐藏态，下一帧再设入场动画
      // 避免 pageSlide 与上次 show() 留下的类名相同导致 CSS 不重新触发动画（闪烁）
      const tabFrom = wx.getStorageSync('_tabFrom')
      const isFirstLaunch = (tabFrom == null || tabFrom === '')
      this.setData({ pageExit: '', pageSlide: 'page-init' })
      setTimeout(() => {
        let animationClass = ''
        if (isFirstLaunch) {
          animationClass = 'page-fade-in'
        } else if (tabFrom === 1) {
          animationClass = 'page-enter-left'
        } else {
          animationClass = 'page-fade-in'
        }
        this.setData({ pageSlide: animationClass })
      }, 30)  // 确保 CSS 检测到类名变更

      // 首次打开才有元素入场动画
      if (!this._entrancePlayed && isFirstLaunch) {
        this.setData({ pageReady: false })
      }

      this.loadPrinterStatus()
      // 启动打印机状态轮询（30秒）
      this._startPrinterPolling()
      // 首次加载定价配置（与本地打印工具保持同步）
      if (!this.data.pricingLoaded) {
        this.loadPricing()
      }
      // 每次切回页面时重新检查角色（可能在"我"页面兑换了许可）
      if (wx.getStorageSync('token')) {
        this.loadUserRole()
      }
      // 重印恢复：唯一消费点（来自"我"页或详情页写入的 reprintInfo）
      const reprintInfo = wx.getStorageSync('reprintInfo')
      if (reprintInfo) {
        wx.removeStorageSync('reprintInfo')
        this.setData({
          duplex: reprintInfo.duplex || 'on',
        })
      }
      // 同步 tabBar 选中态（标准 WeChat 模式：每个页面主动更新）
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
      // 重置入场动画类为隐藏态，确保下次 show 时框架首帧不可见，避免闪烁
      // pageExit 控制退出动画，pageSlide 控制入场/静止态，互不冲突
      this.setData({ pageSlide: 'page-init', pageExit: '' })
    },
  },
  methods: {
    // 由 tabBar 调用：退出动画 → 回调中切换页面
    animateExit(direction) {
      this.setData({ pageExit: direction === 'left' ? 'page-exit-left' : 'page-exit-right' })
    },

    _startPrinterPolling() {
      this._stopPrinterPolling()
      this._printerPollTimer = setInterval(() => {
        this.loadPrinterStatus()
      }, 5000)
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
          wx.request({
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
          wx.request({
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
      wx.request({
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
      this.setData({ showAccessDeniedModal: false })
      wx.switchTab({ url: '/pages/me/me' })
    },

    // ==================== 自定义橡皮筋滚动引擎 ====================

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

      // 底部额外滚动留白，防止提交按钮贴边或被 tabBar 遮挡
      this._bottomPad = 20

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
        this._contentH = ch
        this._maxY = Math.max(0, ch - vp + this._bottomPad)
        if (this._y > this._maxY) {
          // 不直接跳变，让 _snapBack() 从当前位置平滑回弹到新边界
          this._snapBack()
        }
      })
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

          // 检测文件格式：图片 / 不支持格式（Excel/PPT/压缩包等）
          const ext = name.slice(name.lastIndexOf('.')).toLowerCase()
          const imageExts = ['.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp', '.tiff', '.tif']
          const isImage = imageExts.includes(ext)
          const isExcel = ext === '.xls' || ext === '.xlsx'

          // 不支持的文件格式：直接拒绝，不上传
          const supportedExts = ['.pdf', '.doc', '.docx', '.txt', '.csv', '.md', '.html', '.htm',
            '.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp', '.tiff', '.tif']
          if (!supportedExts.includes(ext) && !isExcel) {
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
            duplex: 'on',
            entering: true,
            removing: false,
            excelWarning: isExcel,
            unsupportedFormat: false,   // 不支持格式已在选择时拦截，不会到达此处
            isImage: isImage,
            pageCount: 0,
            pageCountStatus: '',  // '' | 'analyzing' | 'confirmed' — 页数分析进度
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
          this._scheduleMeasure(400)
          setTimeout(() => this._scheduleMeasure(600), 600)
          setTimeout(() => this._scheduleMeasure(850), 850)  // 动画完成后刷新滚动边界
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
        this.setData({ selectedFiles: files, badgeCount: files.length })
        if (!isLastFile) {
          this._prevFileCount = files.length
        }
        this._scheduleMeasure()
      }, 600)  // cardRemove 动画 0.55s + 50ms buffer
    },

    // ==================== 文件上传（每个文件独立进度条）====================

    startFileUpload(fileIndex, filePath) {
      const token = wx.getStorageSync('token') || ''
      this.stopFileUploadTimer(fileIndex)

      const key = 'selectedFiles[' + fileIndex + ']'
      this._uploadTimers[fileIndex] = {
        realProgress: 0,
        timer: null,
      }

      // 每 0.5s 把显示进度向真实进度推进
      this._uploadTimers[fileIndex].timer = setInterval(() => {
        const real = this._uploadTimers[fileIndex].realProgress
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
          // 页数未知时启动轮询（等待本地打印工具分析回报）
          if (pageCount <= 0 && file && !file.isImage) {
            this._startPageCountPoll(fileIndex, fileId)
          }
          // DOM 从"上传中+进度条"切换为"已上传+份数+打印范围"，需更长延迟等渲染稳定
          this._scheduleMeasure(200)
          setTimeout(() => this._scheduleMeasure(450), 450)
        },
        fail: (err) => {
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
    },

    stopFileUploadTimer(fileIndex) {
      const entry = this._uploadTimers && this._uploadTimers[fileIndex]
      if (entry && entry.timer) {
        clearInterval(entry.timer)
        this._uploadTimers[fileIndex].timer = null
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

    // ==================== 页数轮询（等待本地打印工具分析）====================

    _MAX_POLL_ATTEMPTS: 60,   // 在线时 60 次 × 2s = 最多等 120 秒；离线时不计次，持续轮询

    _startPageCountPoll(fileIndex, fileId) {
      this._stopPageCountPoll(fileIndex)
      if (!fileId) return
      let attempts = 0

      const poll = () => {
        const token = wx.getStorageSync('token') || ''
        wx.request({
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
                  this.setData({
                    ['selectedFiles[' + fileIndex + '].pageCount']: pc,
                    ['selectedFiles[' + fileIndex + '].pageCountStatus']: 'confirmed',
                  })
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
                  // 打印机离线 → 显示黄色警告，不计入轮询次数（等待上线）
                  if (currentStatus !== 'offline') {
                    this.setData({
                      ['selectedFiles[' + fileIndex + '].pageCountStatus']: 'offline',
                    })
                  }
                  // 离线时重置计数器，打印机上线后重新计时
                  attempts = 0
                } else {
                  // 打印机在线 → 显示分析中，正常计数
                  if (currentStatus !== 'analyzing') {
                    this.setData({
                      ['selectedFiles[' + fileIndex + '].pageCountStatus']: 'analyzing',
                    })
                  }
                  attempts++
                  if (attempts >= this._MAX_POLL_ATTEMPTS) {
                    this._stopPageCountPoll(fileIndex)
                    console.log('页数轮询超时: fileIndex=' + fileIndex)
                  }
                }
              }
            }
          },
          fail: () => {
            // 网络错误 → 继续轮询
          }
        })
      }

      // 立即发第一次，之后每 2 秒一次
      poll()
      this._pollTimers[fileIndex] = setInterval(poll, 2000)
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

      // 如果在最后一行输入了内容，自动追加新空行
      const lines = this.data.selectedFiles[fileIndex].rangeLines
      if (lineIndex === lines.length - 1 && value.trim()) {
        const newLines = lines.concat([{value: '', error: ''}])
        this.setData({ ['selectedFiles[' + fileIndex + '].rangeLines']: newLines })
        this._scheduleMeasure(200)
      }
    },

    onRangeLineBlur(e) {
      const fileIndex = e.currentTarget.dataset.fileIndex
      const file = this.data.selectedFiles[fileIndex]
      if (!file || file.excelWarning || file.unsupportedFormat || file.isImage) return
      this._normalizeAndValidateRangeLines(fileIndex)
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
    },

    loadPricing() {
      wx.request({
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
      wx.request({
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
      if (this.data.selectedFiles[index] && (this.data.selectedFiles[index].excelWarning || this.data.selectedFiles[index].unsupportedFormat)) return
      this.setData({ ['selectedFiles[' + index + '].duplex']: value })
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
      this.setData({ coverPagePrice: v })
    },

    // ==================== 提交任务 ====================

    onAutoPrintToggle() {
      const turningOn = !this.data.autoPrintEnabled
      this.setData({ autoPrintEnabled: turningOn })
      if (turningOn) {
        // ⚡ 闪电发光 + Canvas 绘制折线电流
        this.setData({ autoPrintGlow: true })
        setTimeout(() => {
          this._drawLightningBolts()
          setTimeout(() => this.setData({ autoPrintGlow: false }), 800)
        }, 30)
      }
    },

    // Canvas 绘制闪电折线（递归中点位移算法 + 随机分支）
    _drawLightningBolts() {
      const query = wx.createSelectorQuery()
      query.select('#boltCanvas').fields({ node: true, size: true }).exec((res) => {
        if (!res || !res[0] || !res[0].node) return
        const canvas = res[0].node
        const dpr = wx.getSystemInfoSync().pixelRatio || 2
        // 显式设置 canvas 物理像素尺寸，确保坐标系与 CSS 布局对齐
        canvas.width = res[0].width || 120 * dpr
        canvas.height = res[0].height || 120 * dpr
        const ctx = canvas.getContext('2d')
        ctx.scale(dpr, dpr)
        // CSS 尺寸（逻辑像素）
        const w = canvas.width / dpr
        const h = canvas.height / dpr
        const cx = w / 2, cy = h / 2

        const draw = (alpha) => {
          ctx.clearRect(0, 0, w, h)
          ctx.globalAlpha = alpha
          const boltCount = 4
          for (let i = 0; i < boltCount; i++) {
            const angle = (i / boltCount) * Math.PI * 2 + (Math.random() - 0.5) * 0.4
            const len = 6 + Math.random() * 5
            const ex = cx + Math.cos(angle) * len
            const ey = cy + Math.sin(angle) * len
            this._paintBolt(ctx, cx, cy, ex, ey, 9, 0)
          }
          ctx.globalAlpha = 1
        }

        draw(1)
        // 渐隐
        let step = 0
        const steps = 10
        const timer = setInterval(() => {
          step++
          if (step > steps) { clearInterval(timer); return }
          draw(1 - step / steps)
        }, 70)
      })
    },

    // 递归中点位移：每次迭代在两点中点加入随机偏移，depth 越大线越细
    _paintBolt(ctx, x1, y1, x2, y2, displace, depth) {
      if (displace < 1.2 || depth > 6) {
        ctx.beginPath()
        ctx.moveTo(x1, y1)
        ctx.lineTo(x2, y2)
        const colors = ['#fff', '#ffe500', '#ffc000', '#ff9500', '#ff7000']
        ctx.strokeStyle = colors[Math.min(depth, colors.length - 1)]
        ctx.lineCap = 'round'
        ctx.lineWidth = Math.max(0.08, 1.0 - depth * 0.12)
        ctx.shadowColor = '#ffe500'
        ctx.shadowBlur = 3 - depth * 0.5
        ctx.stroke()
        return
      }
      const midX = (x1 + x2) / 2 + (Math.random() - 0.5) * displace * 0.8
      const midY = (y1 + y2) / 2 + (Math.random() - 0.5) * displace * 0.8
      this._paintBolt(ctx, x1, y1, midX, midY, displace * 0.5, depth + 1)
      this._paintBolt(ctx, midX, midY, x2, y2, displace * 0.5, depth + 1)
      // 随机分支
      if (Math.random() < 0.25 && depth < 3) {
        const bx = midX + (Math.random() - 0.5) * displace * 2
        const by = midY + (Math.random() - 0.5) * displace * 2
        ctx.save()
        ctx.shadowColor = '#ff9500'
        ctx.shadowBlur = 1
        this._paintBolt(ctx, midX, midY, bx, by, displace * 0.2, depth + 3)
        ctx.restore()
      }
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

      // 检查是否有可打印的文件（排除 Excel 等不支持格式）
      const printable = selectedFiles.filter(f => !f.excelWarning && !f.unsupportedFormat)
      if (printable.length === 0) {
        wx.showToast({ title: '所选文件格式不支持打印', icon: 'none', duration: 2000 })
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
        const lines = f.rangeLines || []
        const hasError = lines.some(line => line.error)
        if (hasError) {
          wx.showToast({ title: `"${f.name}" 页码范围有误`, icon: 'none', duration: 2000 })
          return
        }
      }

      this._doSubmit(false)
    },

    // 确认强制提交（忽略页数未验证警告）
    onConfirmForceSubmit() {
      this.setData({ showPageCountWarning: false })
      this._doSubmit(true)
    },

    // 取消强制提交，返回等待
    onCancelForceSubmit() {
      this.setData({ showPageCountWarning: false })
    },

    _doSubmit(skipPageValidation) {
      const { selectedFiles } = this.data

      this.setData({ submitting: true })
      wx.showLoading({ title: '提交中...' })

      const filesPayload = selectedFiles.map(f => {
        // 从多行输入合并出 page_range（确保 blur 前的输入也不丢失）
        const lines = (f.rangeLines || []).filter(l => (l.value || '').trim() && !l.error)
        const range = lines.map(l => l.value.trim()).join(',')
        return {
          file_id: f.fileId,
          file: f.name,
          copies: Number(f.copies),
          page_range: range || f.pageRange || '',
          duplex: f.duplex || 'on',
        }
      })

      wx.request({
        url: CONFIG.BASE_URL + '/api/submit_order',
        method: 'POST',
        header: {
          'Authorization': 'Bearer ' + (wx.getStorageSync('token') || ''),
          'content-type': 'application/json'
        },
        data: {
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
          })
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
          wx.showToast({ title: '任务提交失败', icon: 'none', duration: 2000 })
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

      let baseTotal = 0
      let allKnown = true
      files.forEach(f => {
        const r = this._calcCost(f.page_count || 0, f.copies || 1, f.duplex || 'on')
        baseTotal += r.cost
        if (!r.known) allKnown = false
      })

      let total = baseTotal
      if (this.data.deliveryEnabled) {
        total += baseTotal * (this.data.deliveryPercent / 100)
      }
      total += this.data.urgencyPrice
      if (this.data.coverPage) total += this.data.coverPagePrice

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
      if (this.data.deliveryEnabled) {
        const loc = this.data.deliveryLocation
        const pct = this.data.deliveryPercent
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
      const urgPrice = this.data.urgencyPrice
      if (urgPrice > 0) {
        lines.push(itemNum + '. 优先级：' + this.data.urgency + ' | ￥' + urgPrice.toFixed(2))
        allParts.push(urgPrice.toFixed(2))
      } else {
        lines.push(itemNum + '. 优先级：' + this.data.urgency + ' | ￥0')
      }

      // 首页
      if (this.data.coverPage) {
        itemNum++
        lines.push(itemNum + '. 打印首页信息 | ' + this.data.coverPagePrice.toFixed(2))
        allParts.push(this.data.coverPagePrice.toFixed(2))
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
      this.setData({
        showSuccessModal: false,
        selectedFiles: [],
      })
      // 恢复 tab 栏发丝线
      try {
        const tabBar = this.getTabBar && this.getTabBar()
        if (tabBar) tabBar.setData({ hideBorder: false })
      } catch (e) { /* 兼容低版本 */ }
      this._stopAllUploadTimers()
      this._stopAllPollTimers()
      this._scheduleMeasure()
    },

    noop() {},
  },
})
