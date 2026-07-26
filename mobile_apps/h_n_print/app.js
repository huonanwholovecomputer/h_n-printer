// app.js
const { CONFIG } = require('./utils/config')

App({
  onLaunch() {
    const logs = wx.getStorageSync('logs') || []
    logs.unshift(Date.now())
    wx.setStorageSync('logs', logs)
    this.globalData._firstLaunch = true

    // 初始化主题：'auto' | 'light' | 'dark'
    const themeMode = wx.getStorageSync('themeMode') || 'auto'
    this.globalData.themeMode = themeMode

    // 获取当前系统主题（需 darkmode: true）
    try {
      const sysInfo = wx.getSystemInfoSync()
      this.globalData._systemTheme = sysInfo.theme || 'light'
    } catch (e) {
      this.globalData._systemTheme = 'light'
    }

    // 计算实际生效的主题
    this._resolveEffectiveTheme()
    this._syncNativeBackground()

    // 监听系统主题变化
    if (typeof wx.onThemeChange === 'function') {
      wx.onThemeChange((res) => {
        this.globalData._systemTheme = res.theme || 'light'
        // 仅在自动模式下跟随系统
        if (this.globalData.themeMode === 'auto') {
          this._resolveEffectiveTheme()
          this._syncThemeToAllPages()
          this._syncNativeBackground()
        }
      })
    }
  },

  onShow() {
    if (!this.globalData._firstLaunch) {
      this.globalData._resumedFromBackground = true
    }
    this.globalData._firstLaunch = false
  },

  // 根据 themeMode + _systemTheme 计算 isDarkMode
  _resolveEffectiveTheme() {
    let isDark
    if (this.globalData.themeMode === 'auto') {
      isDark = this.globalData._systemTheme === 'dark'
    } else {
      isDark = this.globalData.themeMode === 'dark'
    }
    this.globalData.isDarkMode = isDark
    wx.setStorageSync('isDarkMode', isDark)
  },

  // 同步主题到所有活跃页面
  _syncThemeToAllPages() {
    const pages = getCurrentPages()
    pages.forEach(page => {
      if (page.setData) {
        page.setData({
          isDarkMode: this.globalData.isDarkMode,
          themeMode: this.globalData.themeMode,
          themeSwitching: true,
        })
      }
    })
    // 同步自定义 tabBar（组件独立于页面栈，需单独更新）
    if (pages.length > 0) {
      const tabBar = pages[pages.length - 1].getTabBar?.()
      if (tabBar) {
        tabBar.setData({ isDarkMode: this.globalData.isDarkMode })
      }
    }
    // 500ms 后清除脉冲标记，让动画完成
    setTimeout(() => {
      pages.forEach(page => {
        if (page.setData) {
          page.setData({ themeSwitching: false })
        }
      })
    }, 500)
  },

  // 供页面调用：切换主题模式，返回新的 { themeMode, isDarkMode }
  toggleTheme() {
    // auto → dark → light → auto
    const order = ['auto', 'dark', 'light']
    const cur = this.globalData.themeMode
    const idx = order.indexOf(cur)
    const next = order[(idx + 1) % 3]
    this._applyThemeMode(next)
    // 同步到后端（静默，不阻塞 UI）
    this._saveThemeToServer(next)
    return { themeMode: next, isDarkMode: this.globalData.isDarkMode }
  },

  // 从服务端同步主题（/api/me 返回 theme_mode 后调用）
  // serverTheme: 'auto' | 'light' | 'dark' | undefined
  syncThemeFromServer(serverTheme) {
    if (!serverTheme || !['auto', 'light', 'dark'].includes(serverTheme)) return
    // 服务端主题与当前不一致时覆盖（用户在另一设备修改了主题）
    if (serverTheme !== this.globalData.themeMode) {
      this._applyThemeMode(serverTheme)
    }
  },

  // 内部：应用主题模式
  _applyThemeMode(mode) {
    this.globalData.themeMode = mode
    wx.setStorageSync('themeMode', mode)
    this._resolveEffectiveTheme()
    this._syncThemeToAllPages()
    this._syncNativeBackground()
  },

  // 同步原生窗口背景色，避免深色模式下页面切换时露出白色窗口背景
  _syncNativeBackground() {
    const bg = this.globalData.isDarkMode ? '#1C1C1E' : '#F2F2F7'
    wx.setBackgroundColor({
      backgroundColor: bg,
      backgroundColorTop: bg,
      backgroundColorBottom: bg,
    })
  },

  // 保存主题到后端
  _saveThemeToServer(themeMode) {
    const token = wx.getStorageSync('token')
    if (!token) return
    wx.request({
      url: CONFIG.BASE_URL + '/api/me/theme',
      method: 'PUT',
      header: {
        'Authorization': 'Bearer ' + token,
        'content-type': 'application/json',
      },
      data: { theme_mode: themeMode },
      success: () => {},
      fail: () => {},
    })
  },

  globalData: {
    userInfo: null,
    token: null,
    openid: null,
    _resumedFromBackground: false,
    _firstLaunch: true,
    themeMode: 'auto',
    isDarkMode: false,
    _systemTheme: 'light',
  }
})
