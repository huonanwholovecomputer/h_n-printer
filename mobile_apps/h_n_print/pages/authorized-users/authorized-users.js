// pages/authorized-users/authorized-users.js
// F10: 历史授权用户列表（管理员/超级管理员可见）
const { CONFIG } = require('../../utils/config')

Component({
  data: {
    users: [],
    loading: true,
  },

  lifetimes: {
    attached() {
      this.loadUsers()
    },
  },

  methods: {
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
      wx.navigateTo({
        url: `/pages/user-orders/user-orders?openid=${openid}&nickname=${encodeURIComponent(nickname)}`
      })
    },
  },
})
