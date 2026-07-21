// pages/authorized-users/authorized-users.js
// F10: 历史授权用户列表（管理员/超级管理员可见）
const CONFIG = require('../../utils/config')

Page({
  data: {
    users: [],
    loading: true,
  },

  onLoad() {
    this.loadUsers()
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
          this.setData({ users: res.data.users || [] })
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
    wx.navigateTo({ url: `/pages/me/me?viewUser=${openid}` })
  },
})
