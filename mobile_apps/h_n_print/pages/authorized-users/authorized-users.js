// pages/authorized-users/authorized-users.js
// F10: 历史授权用户列表（管理员/超级管理员可见）
// 展示所有被本管理员授权过的用户（管理员+临时用户，含已移除/已过期），
// 卡片含密钥类型与当前状态，可展开查看该用户的全部密钥记录（多次授权）。
const { CONFIG } = require('../../utils/config')

Component({
  data: {
    pageSlide: 'page-init',
    pageExit: '',
    isDarkMode: wx.getStorageSync('isDarkMode') || false,
    users: [],
    loading: true,
    loadError: '',
    expandedUsers: {},
    statusMap: {
      permanent: '永久管理员',
      active: '临时授权中',
      expired: '已过期',
      removed: '已移除',
    },
    keyStatusMap: {
      unused: '未使用',
      used: '已使用',
      revoked: '已作废',
      finished: '已完成',
      archived: '归档',
    },
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
    },
    hide() {
      const forward = wx.getStorageSync('_navForward')
      this.setData({ pageExit: forward ? 'page-exit-left' : 'page-exit-right' })
    },
  },

  lifetimes: {
    attached() {
      const app = getApp()
      this.setData({ isDarkMode: app.globalData.isDarkMode })
      this.loadUsers()
    },
  },

  methods: {
    _navigateWithAnimation(url) {
      wx.setStorageSync('_navForward', '1')
      this.setData({ pageExit: 'page-exit-left' })
      setTimeout(() => { wx.navigateTo({ url }) }, 280)
    },

    loadUsers() {
      const token = wx.getStorageSync('token')
      if (!token) {
        wx.showToast({ title: '请先登录', icon: 'none' })
        return
      }
      this.setData({ loading: true, loadError: '' })
      wx.request({
        url: CONFIG.BASE_URL + '/api/authorized_users',
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          if (res.data && res.data.success) {
            const users = (res.data.users || []).map(u => ({
              ...u,
              avatarChar: (u.nickname || '?')[0],
              // 头像 URL 加时间戳防缓存，确保每次打开显示最新头像
              avatarUrl: u.avatar_url ? u.avatar_url + '?t=' + Date.now() : '',
              licenseTypeLabel: u.license_type === 'admin' ? '管理员许可'
                : u.license_type === 'temp' ? '临时许可'
                : u.license_type === 'both' ? '管理员+临时' : '无密钥记录',
              statusLabel: this.data.statusMap[u.status] || u.status,
            }))
            this.setData({ users, loadError: '' })
          } else {
            // 页面内状态提示（对齐 Android App）：不清空已加载列表
            this.setData({ loadError: res.data.message || '加载失败' })
          }
        },
        fail: () => {
          this.setData({ loadError: '网络错误' })
        },
        complete: () => {
          this.setData({ loading: false })
        },
      })
    },

    onUserTap(e) {
      const openid = e.currentTarget.dataset.openid
      const nickname = e.currentTarget.dataset.nickname || ''
      this._navigateWithAnimation(
        `/pages/user-orders/user-orders?openid=${openid}&nickname=${encodeURIComponent(nickname)}`
      )
    },

    // 展开/收起该用户的全部密钥记录
    onToggleRecords(e) {
      const openid = e.currentTarget.dataset.openid
      const expanded = { ...this.data.expandedUsers }
      if (expanded[openid]) {
        delete expanded[openid]
      } else {
        expanded[openid] = true
      }
      this.setData({ expandedUsers: expanded })
    },
  },
})
