// pages/authorized-users/authorized-users.js
// F10: 历史授权用户列表（管理员/超级管理员可见）
const { CONFIG } = require('../../utils/config')

Component({
  data: {
    pageSlide: 'page-init',
    pageExit: '',
    users: [],
    loading: true,
  },

  pageLifetimes: {
    show() {
      const forward = wx.getStorageSync('_navForward')
      wx.removeStorageSync('_navForward')
      this.setData({ pageSlide: forward ? 'page-enter-right' : 'page-enter-left', pageExit: '' })
    },
    hide() {
      const forward = wx.getStorageSync('_navForward')
      this.setData({ pageExit: forward ? 'page-exit-left' : 'page-exit-right' })
    },
  },

  lifetimes: {
    attached() {
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
      this.setData({ loading: true })
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
            }))
            this.setData({ users })
          } else {
            wx.showToast({ title: res.data.message || '加载失败', icon: 'none' })
          }
        },
        fail: () => {
          wx.showToast({ title: '网络错误', icon: 'none' })
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
  },
})
