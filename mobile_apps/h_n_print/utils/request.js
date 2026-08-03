// utils/request.js
// 统一请求封装：wx.request 包装 + 401 自动重登录重放（最多重试 2 次，500ms/1500ms 指数退避）
// 用法与 wx.request 完全一致（url/method/data/header/success/fail/complete/timeout），
// 区别仅在于：任何请求返回 401 时自动 wx.login 换 code → 调 /api/login 刷新 token → 重放原请求。
const { CONFIG } = require('./config')

// 并发去重：同一时刻只执行一次刷新登录，其余 401 等待同一个 Promise
let _refreshing = null

function _refreshToken() {
  if (_refreshing) return _refreshing
  _refreshing = new Promise((resolve) => {
    wx.login({
      success: (res) => {
        if (!res.code) {
          console.error('[request] wx.login 未返回 code')
          resolve(false)
          return
        }
        wx.request({
          url: CONFIG.BASE_URL + '/api/login',
          method: 'POST',
          header: { 'content-type': 'application/json' },
          data: { code: res.code },
          success: (loginRes) => {
            if (loginRes.statusCode === 200 && loginRes.data && loginRes.data.success) {
              const token = loginRes.data.token
              const openid = loginRes.data.openid
              wx.setStorageSync('token', token)
              wx.setStorageSync('openid', openid)
              try {
                const app = getApp()
                if (app && app.globalData) {
                  app.globalData.token = token
                  app.globalData.openid = openid
                }
              } catch (e) { /* 极端情况忽略 */ }
              resolve(true)
            } else {
              console.error('[request] 刷新登录失败:', loginRes.statusCode, loginRes.data)
              resolve(false)
            }
          },
          fail: (err) => {
            console.error('[request] 刷新登录请求失败:', err)
            resolve(false)
          },
        })
      },
      fail: () => resolve(false),
    })
  }).then((ok) => {
    _refreshing = null
    return ok
  })
  return _refreshing
}

/**
 * request(options) — 与 wx.request 参数一致（url/method/data/header/success/fail/complete/timeout）
 * 401 时：静默重登录 → 更新 Authorization → 重放原请求（最多 2 次，间隔 500ms/1500ms）
 * /api/login 自身不参与 401 重试（避免登录接口 401 时无限套娃）
 */
function request(options) {
  const MAX_RETRY = 2
  const RETRY_DELAY = [500, 1500]
  const isLoginUrl = String(options.url || '').indexOf('/api/login') !== -1

  const attempt = (retryCount) => {
    wx.request({
      url: options.url,
      method: options.method || 'GET',
      data: options.data,
      header: options.header,
      timeout: options.timeout,
      success: (res) => {
        if (!isLoginUrl && res.statusCode === 401 && retryCount < MAX_RETRY) {
          _refreshToken().then((ok) => {
            if (!ok) {
              // 重登录失败：按网络失败处理，把 401 响应交给 fail
              if (options.fail) options.fail({ errMsg: 'request:fail 重新登录失败', statusCode: 401 })
              if (options.complete) options.complete(res)
              return
            }
            const header = Object.assign({}, options.header)
            header['Authorization'] = 'Bearer ' + (wx.getStorageSync('token') || '')
            setTimeout(() => attempt(retryCount + 1), RETRY_DELAY[retryCount] || 1500)
          })
          return
        }
        if (options.success) options.success(res)
        if (options.complete) options.complete(res)
      },
      fail: (err) => {
        if (options.fail) options.fail(err)
        if (options.complete) options.complete(err)
      },
    })
  }

  attempt(0)
}

module.exports = { request }
