// utils/request.js
// 统一请求封装：wx.request 包装 + 401 自动重登录重放（最多重试 2 次，500ms/1500ms 指数退避）
// 用法与 wx.request 完全一致（url/method/data/header/success/fail/complete/timeout），
// 区别仅在于：
//   ① 任何请求返回 401 时自动 wx.login 换 code → 调 /api/login 刷新 token → 重放原请求；
//   ② **默认超时 12 秒**（S1-09）：wx.request 不传 timeout 时微信默认 60s —— 弱网/校园网
//      抖动下界面会停在"加载中"一分钟，既不报错也不能重试。10–15s 区间取 12s，
//      调用方仍可用 options.timeout 显式覆盖（上传/长任务给更长）。
const { CONFIG } = require('./config')

const DEFAULT_TIMEOUT_MS = 12000

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
          timeout: DEFAULT_TIMEOUT_MS,
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
      // S1-09：**每个请求都有超时** —— 未显式传就用 12s 默认，绝不落到微信的 60s 默认
      timeout: options.timeout || DEFAULT_TIMEOUT_MS,
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

// ============================================================
// S1-09：轮询自调度器（setInterval → setTimeout 链 + 指数退避）
// ============================================================
//
// 旧写法 `setInterval(fn, 15000)` 的问题是**不关心上一轮是否回来**：弱网下一次请求要
// 10s+ 才超时，15s 的间隔只是"大约"，实际会叠着跑；连续失败时更是一直以固定频率重试，
// 把弱网里本该省下的请求全打出去（微信同域名并发有限，最后全排队、界面像卡死）。
//
// 现在：**上一次任务落定后**才排下一次（`setTimeout` 自调度），
//   · taskFn(done)：done(true) 成功 / done(false) 失败 / 也可返回 Promise（resolve=成功）；
//   · 失败按 1.5× 指数退避，上限 5 分钟；成功立刻恢复到基线间隔。
const POLL_BACKOFF_FACTOR = 1.5
const POLL_BACKOFF_MAX_MS = 300000

function createPollScheduler(taskFn, baseDelayMs, options) {
  const opts = options || {}
  const base = Math.max(1, Number(baseDelayMs) || 15000)
  const maxDelay = Number(opts.maxDelayMs) || POLL_BACKOFF_MAX_MS
  const factor = Number(opts.factor) || POLL_BACKOFF_FACTOR
  // immediate=false：start() 后先等一个基线间隔再跑（调用方自己已经同步拉过一次时用，
  // 避免"刚加载完立刻又发一轮"的重复请求）
  const immediate = opts.immediate !== false
  let timer = null
  let delay = base
  let stopped = true
  let running = false

  function schedule(ok) {
    if (stopped) return
    if (ok === false) delay = Math.min(Math.round(delay * factor), maxDelay)
    else if (ok === true) delay = base
    timer = setTimeout(tick, delay)
  }

  function tick() {
    timer = null
    if (stopped || running) return
    running = true
    let settled = false
    const done = (ok) => {
      if (settled) return
      settled = true
      running = false
      schedule(ok)
    }
    try {
      const ret = taskFn(done)
      if (ret && typeof ret.then === 'function') {
        ret.then(() => done(true), () => done(false))
      }
    } catch (e) {
      done(false)
    }
  }

  return {
    start() {
      if (!stopped) return
      stopped = false
      delay = base
      if (immediate) tick()
      else timer = setTimeout(tick, delay)
    },
    stop() {
      stopped = true
      running = false
      if (timer) { clearTimeout(timer); timer = null }
    },
    isRunning() { return !stopped },
    currentDelay() { return delay },
  }
}

module.exports = { request, createPollScheduler, POLL_BACKOFF_FACTOR, POLL_BACKOFF_MAX_MS }
