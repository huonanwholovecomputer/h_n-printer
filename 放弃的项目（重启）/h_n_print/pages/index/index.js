// index.js
const { CONFIG } = require('../../utils/config')

Component({
  data: {
    motto: 'HN同学的打印机即将上线',
    // 多文件列表：每项 { name, size, path, fileId, uploading, progress, failed, copies }
    selectedFiles: [],
    duplex: 'on',
    printerActive: false,
    showSuccessModal: false,
    showAccessDeniedModal: false,
    userRole: '',
    submitting: false,
    logoScale: 1,
    logoPadding: 40,
    // 内部滚动位置（驱动 scroll-content 的 translateY）
    scrollTop: 0,
  },
  lifetimes: {
    attached() {
      this._initScrollEngine()
      this._uploadTimers = {}   // { index: intervalId } — 每个文件独立的进度条定时器
      this.doLogin()
    },
    detached() {
      this._destroyScrollEngine()
      this._stopAllUploadTimers()
    },
  },
  pageLifetimes: {
    show() {
      this.loadPrinterStatus()
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
    },
  },
  methods: {
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
          this._y = this._maxY
          this._applyY()
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
      wx.chooseMessageFile({
        count: 1,
        type: 'all',
        success: (res) => {
          const file = res.tempFiles[0]
          const name = file.name || ''
          const sizeKB = Number(file.size) || 0
          const fileIndex = this.data.selectedFiles.length

          // 检测是否为 Excel
          const ext = name.slice(name.lastIndexOf('.')).toLowerCase()
          const isExcel = ext === '.xls' || ext === '.xlsx'

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
            pageRange: '',
            duplex: 'on',
            entering: true,
            removing: false,
            excelWarning: isExcel,
          }
          this.setData({
            ['selectedFiles[' + fileIndex + ']']: newFile
          })
          // 入场动画 ~380ms 后清除 entering 标记，避免后续列表更新触发重播
          setTimeout(() => {
            this.setData({ ['selectedFiles[' + fileIndex + '].entering']: false })
          }, 400)
          this._scheduleMeasure(400)
          setTimeout(() => this._scheduleMeasure(500), 500)
          this.startFileUpload(fileIndex, file.path)
        },
        fail: (err) => {
          console.log('选择文件失败', err)
        }
      })
    },

    onRemoveFile(e) {
      const index = e.currentTarget.dataset.index
      // 先标记退场动画（opacity + transform transition ~220ms），等动画完成后再移除
      this.stopFileUploadTimer(index)
      this.setData({ ['selectedFiles[' + index + '].removing']: true })
      setTimeout(() => {
        const files = this.data.selectedFiles.slice()
        files.splice(index, 1)
        // 重映射定时器索引
        const newTimers = {}
        const oldKeys = Object.keys(this._uploadTimers).map(Number)
        oldKeys.forEach((k) => {
          if (k > index) {
            newTimers[k - 1] = this._uploadTimers[k]
          } else if (k < index) {
            newTimers[k] = this._uploadTimers[k]
          }
          // k === index 跳过（已清理）
        })
        this._uploadTimers = newTimers
        this.setData({ selectedFiles: files })
        this._scheduleMeasure()
      }, 350)  // fileCardExit 动画 300ms + 50ms buffer
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
          let errMsg = ''
          try {
            const data = JSON.parse(uploadRes.data)
            fileId = data.file_id || data.id
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

          console.log('文件上传成功，返回 ID:', fileId, 'index:', fileIndex)
          this._uploadTimers[fileIndex].realProgress = 100
          this.stopFileUploadTimer(fileIndex)
          this.setData({
            [key + '.uploading']: false,
            [key + '.progress']: 100,
            [key + '.fileId']: fileId,
            [key + '.failed']: false,
          })
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

    // ==================== 每文件份数操作 ====================

    onFileCopiesMinus(e) {
      const index = e.currentTarget.dataset.index
      if (this.data.selectedFiles[index].excelWarning) return
      const v = this.data.selectedFiles[index].copies
      if (v > 1) {
        this.setData({ ['selectedFiles[' + index + '].copies']: v - 1 })
      }
    },

    onFileCopiesPlus(e) {
      const index = e.currentTarget.dataset.index
      if (this.data.selectedFiles[index].excelWarning) return
      const v = this.data.selectedFiles[index].copies
      if (v < 99) {
        this.setData({ ['selectedFiles[' + index + '].copies']: v + 1 })
      }
    },

    onFileCopiesChange(e) {
      const index = e.currentTarget.dataset.index
      if (this.data.selectedFiles[index].excelWarning) return
      const v = parseInt(e.detail.value, 10)
      this.setData({
        ['selectedFiles[' + index + '].copies']: isNaN(v) || v < 1 ? 1 : v > 99 ? 99 : v
      })
    },

    onFilePageRangeInput(e) {
      const index = e.currentTarget.dataset.index
      if (this.data.selectedFiles[index].excelWarning) return
      this.setData({
        ['selectedFiles[' + index + '].pageRange']: e.detail.value || ''
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
      if (this.data.selectedFiles[index] && this.data.selectedFiles[index].excelWarning) return
      this.setData({ ['selectedFiles[' + index + '].duplex']: value })
    },

    onDuplexChange(e) {
      this.setData({ duplex: e.currentTarget.dataset.value })
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

      this.setData({ submitting: true })
      wx.showLoading({ title: '提交中...' })

      const filesPayload = selectedFiles.map(f => ({
        file_id: f.fileId,
        file: f.name,
        copies: Number(f.copies),
        page_range: f.pageRange || '',
        duplex: f.duplex || 'on',
      }))

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
        },
        success: (submitRes) => {
          wx.hideLoading()
          if (submitRes.statusCode === 401) {
            this.setData({ submitting: false })
            this.doLoginAndRetry(() => this.onSubmit())
            return
          }
          console.log('任务提交成功：', submitRes.data)
          this.setData({
            submitting: false,
            showSuccessModal: true,
          })
        },
        fail: (err) => {
          wx.hideLoading()
          console.error('任务提交失败：', err)
          this.setData({ submitting: false })
          wx.showToast({ title: '任务提交失败', icon: 'none', duration: 2000 })
        }
      })
    },

    onCloseModal() {
      this.setData({
        showSuccessModal: false,
        selectedFiles: [],
      })
      this._stopAllUploadTimers()
      this._scheduleMeasure()
    },

    onViewOrders() {
      this.setData({ showSuccessModal: false, selectedFiles: [] })
      this._stopAllUploadTimers()
      this._scheduleMeasure()
      wx.switchTab({ url: '/pages/me/me' })
    },

    noop() {},
  },
})
