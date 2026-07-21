// order-detail.js
const { CONFIG } = require('../../utils/config')

Component({
  data: {
    order: null,
    loading: true,
    statusMap: {
      queued: '排队中',
      printing: '打印中',
      accepted: '已添加',
      abandoned: '放弃打印',
      sent: '已完成',
      failed: '失败',
      rejected: '被打回',
      canceled: '已取消',
    },
  },
  lifetimes: {
    attached() {
      const pages = getCurrentPages()
      const options = pages[pages.length - 1].options || {}
      const orderId = options.order_id
      if (orderId) {
        this.loadOrderDetail(orderId)
      } else {
        this.setData({ loading: false })
      }
    },
  },
  methods: {
    loadOrderDetail(orderId) {
      const token = wx.getStorageSync('token')
      if (!token) {
        this.setData({ loading: false })
        return
      }

      wx.request({
        url: CONFIG.BASE_URL + '/api/order/' + orderId,
        method: 'GET',
        header: {
          'Authorization': 'Bearer ' + token
        },
        success: (res) => {
          if (res.statusCode === 200 && res.data.success) {
            const order = res.data.order
            // 预处理文件大小显示（WXML 不支持 .toFixed()）
            if (order.files) {
              order.files.forEach(f => {
                f.sizeDisplay = f.size ? (f.size / 1024).toFixed(1) + ' KB' : ''
              })
            }
            this.setData({
              order: order,
              loading: false,
            })
          } else {
            wx.showToast({ title: res.data.message || '加载失败', icon: 'none' })
            this.setData({ loading: false })
          }
        },
        fail: (err) => {
          console.error('获取任务详情失败:', err)
          wx.showToast({ title: '网络错误', icon: 'none' })
          this.setData({ loading: false })
        }
      })
    },

    onCancelOrder() {
      const orderId = this.data.order.id
      const token = wx.getStorageSync('token')

      wx.showModal({
        title: '确认取消',
        content: '确定要取消这个打印任务吗？',
        success: (modalRes) => {
          if (!modalRes.confirm) return

          wx.showLoading({ title: '取消中...' })
          wx.request({
            url: CONFIG.BASE_URL + '/api/cancel_order',
            method: 'POST',
            header: {
              'Authorization': 'Bearer ' + token,
              'content-type': 'application/json'
            },
            data: { order_id: String(orderId) },
            success: (res) => {
              wx.hideLoading()
              if (res.data.success) {
                wx.showToast({ title: '已取消' })
                // 刷新本地状态
                this.setData({
                  'order.status': 'canceled'
                })
                // 同步取消子任务状态
                if (this.data.order.files) {
                  const patch = {}
                  this.data.order.files.forEach((f, i) => {
                    patch['order.files[' + i + '].status'] = 'canceled'
                  })
                  this.setData(patch)
                }
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

    onReprint() {
      const order = this.data.order
      const files = order.files || []
      // 携带多文件信息到首页
      wx.setStorageSync('reprintInfo', {
        files: files.map(f => ({
          file_name: f.original_name || f.file_name,
          copies: f.copies,
          duplex: f.duplex || 'on',
        })),
        duplex: order.duplex || 'on',
      })
      wx.switchTab({
        url: '/pages/index/index'
      })
    },
  },
})
