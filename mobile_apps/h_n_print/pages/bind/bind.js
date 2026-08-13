// pages/bind/bind.js — 账号绑定管理（小程序生成个人认证密钥 → APP 端绑定微信账号）
// 低频管理功能收进独立页面：生成密钥 / 已绑定设备列表 / 解除绑定。
const { CONFIG } = require('../../utils/config')
const { request } = require('../../utils/request')

Component({
  data: {
    pageSlide: 'page-init',
    pageExit: '',
    isDarkMode: wx.getStorageSync('isDarkMode') || false,
    // 绑定密钥
    bindKey: '',
    bindKeyExpiresAt: '',
    bindKeyCountdownText: '',
    generatingBindKey: false,
    // 已绑定设备
    devices: [],
    loading: true,
    // WXS 引擎：滚动边界 + 程序化滚动命令
    scrollConfig: { minY: 0, maxY: 0, scrollerH: 0, contentH: 0, listOverflow: false },
    scrollCmd: null,
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
      this.loadDevices()
      if (this.data.bindKey) this._startBindCountdown()
      this._scheduleMeasure()
    },
    hide() {
      const forward = wx.getStorageSync('_navForward')
      this.setData({ pageExit: forward ? 'page-exit-left' : 'page-exit-right' })
      this._stopBindCountdown()
    },
  },

  lifetimes: {
    attached() {
      const app = getApp()
      this.setData({ isDarkMode: app.globalData.isDarkMode })
      this._initScrollEngine()
      this.loadDevices()
    },
    detached() {
      this._destroyScrollEngine()
    },
  },

  methods: {
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
          listOverflow: false,
        },
      })
    },

    // WXS 分桶回调（每 100px 一次），本页暂不消费
    onWxsScroll() {},

    // 解析后端时间字符串 "YYYY-MM-DD HH:MM:SS" → 时间戳（按服务器本地时区）
    _parseServerTime(str) {
      if (!str) return 0
      const parts = str.replace(/-/g, '/').split(' ')
      if (parts.length !== 2) return 0
      return new Date(parts[0] + ' ' + parts[1]).getTime()
    },

    onGenerateBindKey() {
      const token = wx.getStorageSync('token')
      if (!token) return
      if (this.data.generatingBindKey) return
      this.setData({ generatingBindKey: true })
      request({
        url: CONFIG.BASE_URL + '/api/bind/create',
        method: 'POST',
        header: { 'Authorization': 'Bearer ' + token, 'content-type': 'application/json' },
        data: { validity_minutes: 10 },
        success: (res) => {
          this.setData({ generatingBindKey: false })
          if (res.data && res.data.success) {
            wx.showToast({ title: '绑定密钥已生成', icon: 'success' })
            this.setData({
              bindKey: res.data.key,
              bindKeyExpiresAt: res.data.expires_at || '',
              bindKeyCountdownText: '',
            })
            this._startBindCountdown()
            this._scheduleMeasure()
          } else {
            wx.showToast({ title: (res.data && res.data.message) || '生成失败', icon: 'none' })
          }
        },
        fail: () => {
          this.setData({ generatingBindKey: false })
          wx.showToast({ title: '网络错误', icon: 'none' })
        }
      })
    },

    _startBindCountdown() {
      this._stopBindCountdown()
      const tick = () => {
        const target = this._parseServerTime(this.data.bindKeyExpiresAt)
        if (!target) { this._stopBindCountdown(); return }
        const remain = target - Date.now()
        if (remain <= 0) {
          this.setData({ bindKeyCountdownText: '已过期' })
          this._stopBindCountdown()
          return
        }
        const totalSec = Math.ceil(remain / 1000)
        const m = Math.floor(totalSec / 60)
        const s = totalSec % 60
        this.setData({ bindKeyCountdownText: m + ':' + (s < 10 ? '0' + s : s) })
      }
      tick()
      this._bindCountdownTimer = setInterval(tick, 1000)
    },

    _stopBindCountdown() {
      if (this._bindCountdownTimer) {
        clearInterval(this._bindCountdownTimer)
        this._bindCountdownTimer = null
      }
    },

    onCopyBindKey() {
      const key = this.data.bindKey
      if (!key) return
      wx.setClipboardData({
        data: '这是HN云打印的账号绑定密钥，请在APP端「绑定微信账号」中填写，10分钟内有效:\n密钥: ' + key,
        success: () => wx.showToast({ title: '已复制', icon: 'success' })
      })
    },

    loadDevices() {
      const token = wx.getStorageSync('token')
      if (!token) return
      request({
        url: CONFIG.BASE_URL + '/api/bind/devices',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          if (!res.data || !res.data.success) return
          this.setData({ devices: res.data.devices || [], loading: false })
          this._scheduleMeasure(100)
        },
        fail: () => {
          this.setData({ loading: false })
          this._scheduleMeasure()
        }
      })
    },

    onUnbindDevice(e) {
      const devOpenid = e.currentTarget.dataset.devOpenid
      if (!devOpenid) return
      const token = wx.getStorageSync('token')
      if (!token) return
      wx.showModal({
        title: '解除绑定',
        content: '解除后该 APP 将回到独立的设备账号：已产生的订单仍保留在微信账号下；解除期间的新订单记入设备账号，重新绑定后自动迁移。',
        confirmText: '解除',
        confirmColor: '#E53935',
        success: (res) => {
          if (!res.confirm) return
          request({
            url: CONFIG.BASE_URL + '/api/bind/revoke',
            method: 'POST',
            header: { 'Authorization': 'Bearer ' + token, 'content-type': 'application/json' },
            data: { dev_openid: devOpenid },
            success: (r) => {
              if (r.data && r.data.success) {
                wx.showToast({ title: '已解除绑定', icon: 'success' })
                this.loadDevices()
              } else {
                wx.showToast({ title: (r.data && r.data.message) || '解除失败', icon: 'none' })
              }
            },
            fail: () => wx.showToast({ title: '网络错误', icon: 'none' })
          })
        }
      })
    },

    onRenameDevice(e) {
      const devOpenid = e.currentTarget.dataset.devOpenid
      const curName = e.currentTarget.dataset.nickname || ''
      if (!devOpenid) return
      const token = wx.getStorageSync('token')
      if (!token) return
      wx.showModal({
        title: '重命名设备',
        editable: true,
        placeholderText: '输入设备名称（如：我的手机、办公室平板）',
        content: curName,
        success: (res) => {
          if (!res.confirm) return
          const nickname = (res.content || '').trim()
          if (!nickname) {
            wx.showToast({ title: '名称不能为空', icon: 'none' })
            return
          }
          request({
            url: CONFIG.BASE_URL + '/api/bind/rename',
            method: 'POST',
            header: { 'Authorization': 'Bearer ' + token, 'content-type': 'application/json' },
            data: { dev_openid, nickname },
            success: (r) => {
              if (r.data && r.data.success) {
                wx.showToast({ title: '已重命名', icon: 'success' })
                this.loadDevices()
              } else {
                wx.showToast({ title: (r.data && r.data.message) || '重命名失败', icon: 'none' })
              }
            },
            fail: () => wx.showToast({ title: '网络错误', icon: 'none' })
          })
        }
      })
    },
  },
})
