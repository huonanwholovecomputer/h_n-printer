// my-performance.js — 个人月度打印统计
const { CONFIG } = require('../../utils/config')

Component({
  data: {
    year: 2026,
    month: 6,
    stats: null,
    loading: true,
    yearList: [],
    monthList: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
    yearIndex: 0,
    monthIndex: 0,
  },
  lifetimes: {
    attached() {
      const now = new Date()
      const currentYear = now.getFullYear()
      const currentMonth = now.getMonth() + 1
      const yearList = []
      for (let y = currentYear; y >= currentYear - 5; y--) {
        yearList.push(y)
      }
      this.setData({
        year: currentYear,
        month: currentMonth,
        yearList: yearList,
        yearIndex: 0,
        monthIndex: currentMonth - 1,
      })
      this.loadMyStatistics()
    },
  },
  methods: {
    loadMyStatistics() {
      const token = wx.getStorageSync('token')
      if (!token) return

      this.setData({ loading: true })

      wx.request({
        url: `${CONFIG.BASE_URL}/api/statistics/my?year=${this.data.year}&month=${this.data.month}`,
        method: 'GET',
        header: { 'Authorization': 'Bearer ' + token },
        success: (res) => {
          if (res.statusCode === 200 && res.data.success) {
            this.setData({
              stats: res.data.stats,
              loading: false,
            })
          } else {
            this.setData({ loading: false })
          }
        },
        fail: () => {
          wx.showToast({ title: '网络错误', icon: 'none' })
          this.setData({ loading: false })
        }
      })
    },

    onYearChange(e) {
      const idx = parseInt(e.detail.value)
      this.setData({
        yearIndex: idx,
        year: this.data.yearList[idx],
      })
      this.loadMyStatistics()
    },

    onMonthChange(e) {
      const idx = parseInt(e.detail.value)
      this.setData({
        monthIndex: idx,
        month: this.data.monthList[idx],
      })
      this.loadMyStatistics()
    },
  },
})
