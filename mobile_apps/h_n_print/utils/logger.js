// utils/logger.js — 前端日志上报（小程序端，POST /api/log/report）
//
// 存在意义：后端 logs/frontend.log 长期是 0 字节 —— 两端从来没把前端异常上报过，
// 线上「用户说点了没反应 / 页面白屏」只能靠猜。这里把**未捕获异常**送进后端，日志才有内容。
//
// 为什么裸 wx.request、**不走 utils/request.js**：
//   request.js 的 401 分支会自动 wx.login 换 code→/api/login→重放原请求。日志上报是
//   "出问题时才跑"的通道：错误风暴时每条日志都触发一次续登，会把一次前端异常放大成
//   一场登录风暴（wx.login 有频率限制，_refreshing 还会让正常业务请求排队等它）。
//   日志丢了无所谓，登录不能乱 —— 所以这里直连、失败静默丢弃、绝不重试。
//
// 口径（与 android_app/www/logger.js **必须一致**，改一端就改另一端）：
//   ① 自动上报只收 error / critical；warning 只留给显式 logWarn()。用户行为日志一律不上报。
//   ② 内存队列最多 20 条，满了丢最旧并计数（dropped），绝不无界增长。
//   ③ 一条 HTTP 请求只发一条 message（接口就是单条 message），"合并"靠**发送节流**：
//      两次发送间隔 ≥10s、每分钟 ≤10 条。服务端限速是 30 条/分钟/IP，这里留 3 倍余量。
//   ④ 单条 message ≤4000 字符（服务端 4096 会硬截断，截断位置不可控），超出自己截断并
//      在末尾加「…（已截断）」。
//   ⑤ message 组成：`[平台/版本] 页面路径 | 错误名: 消息 | 首行堆栈`，
//      例：`[mp/4.6.0] pages/print/print | TypeError: x is not a function | at print.js:12:3`。
//      "首行堆栈"取第一个 `at ...` 帧（不是整段堆栈），并且整条消息压成一行。
//   ⑥ **绝不上报**：openid 明文、token、文件内容、URL 里带密钥的 query
//      （`?token=abc` → `?token=***`，Bearer / X-Printer-Token 的值同样抹掉）。
//   ⑦ 无 token 时不发（否则服务端 403，纯噪音），留在队列里等下次有 token 再发；
//      网络失败直接丢弃不重试（弱网里重试只会把队列越堆越大、把错误放大）。
//   ⑧ 本文件任何异常都必须被 try/catch 吞掉 —— 日志挂了可以，业务页面不能挂。
//
// 验收：tests/verify_frontend_log_report.js（纯桩环境，不联网）
const { CONFIG } = require('./config')

const PLATFORM = 'mp'
const REPORT_PATH = '/api/log/report'
const MAX_QUEUE = 20
const MAX_MESSAGE_LEN = 4000
const TRUNC_SUFFIX = '…（已截断）'
const FLUSH_INTERVAL_MS = 3000       // 每 3s 尝试一次 flush（spec 要求 3~5s）
const MIN_SEND_INTERVAL_MS = 10000   // 两次发送至少隔 10s
const MAX_SEND_PER_MINUTE = 10       // 每分钟最多 10 条（服务端 30/分钟/IP 的 1/3）
const REQUEST_TIMEOUT_MS = 8000
const INFLIGHT_TIMEOUT_MS = 15000    // wx.request 万一不回调 complete，别把队列永久卡死
const LEVELS = ['warning', 'error', 'critical']

let _queue = []          // [{ level, message }]
let _dropped = 0         // 因队列满被丢弃的条数
let _sent = 0            // 已发出的请求数（无论成败）
let _lastSentAt = 0
let _sendTimes = []      // 最近一分钟的发送时间戳（客户端限速）
let _inflightAt = 0      // 上一条还在路上的时间（0 = 空闲）
let _installed = false
let _timer = null
let _version = null

/* ---------- 基础工具（全部吞异常） ---------- */

function _safeString(v) {
  try {
    if (v === null || v === undefined) return ''
    if (typeof v === 'string') return v
    if (typeof v === 'number' || typeof v === 'boolean') return String(v)
    if (v instanceof Error) return v.message || v.name || 'Error'
    return JSON.stringify(v)
  } catch (e) {
    try { return String(v) } catch (e2) { return '[不可序列化对象]' }
  }
}

function _oneLine(s) {
  try { return String(s).replace(/\s+/g, ' ').trim() } catch (e) { return '' }
}

// 脱敏：密钥/令牌/用户标识一律不落服务端日志
function _scrub(text) {
  let s = _safeString(text)
  // ① query 里的敏感键（?token=abc、&openid=xxx…）—— 键名保留，值抹成 ***
  s = s.replace(/(^|[?&\s])((?:token|access_token|printer_token|open_id|openid|code|secret|password|pwd|key)=)[^&#\s]*/gi,
    function (m, pre, key) { return pre + key + '***' })
  // ② 请求头形态：Authorization: Bearer xxx / X-Printer-Token: xxx
  s = s.replace(/((?:Authorization|X-Printer-Token)\s*:\s*(?:Bearer\s+)?)[^\s,;'"]+/gi,
    function (m, pre) { return pre + '***' })
  // ③ 裸 Bearer
  s = s.replace(/\bBearer\s+[A-Za-z0-9._\-]{6,}/g, 'Bearer ***')
  // ④ openid 明文（微信 openid 固定 28 位、o 开头）
  s = s.replace(/\bo[A-Za-z0-9_-]{27}\b/g, 'o***')
  return s
}

function _truncate(text) {
  const s = _safeString(text)
  if (s.length <= MAX_MESSAGE_LEN) return s
  return s.slice(0, MAX_MESSAGE_LEN - TRUNC_SUFFIX.length) + TRUNC_SUFFIX
}

function _firstFrame(err) {
  try {
    const stack = err && err.stack ? String(err.stack) : ''
    if (!stack) return ''
    const lines = stack.split(/\r?\n/)
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim()
      if (/^at\s/.test(line)) return line
    }
    return lines[0].trim()
  } catch (e) { return '' }
}

function _pagePath() {
  try {
    const pages = typeof getCurrentPages === 'function' ? getCurrentPages() : null
    const page = pages && pages.length ? pages[pages.length - 1] : null
    if (page) {
      const route = page.route || page.__route__ || ''
      if (route) return String(route).split('?')[0]
    }
  } catch (e) { /* 页面栈拿不到就算了 */ }
  return 'unknown'
}

function _appVersion() {
  if (_version !== null) return _version
  let v = 'dev'
  try {
    if (typeof wx.getAccountInfoSync === 'function') {
      const info = wx.getAccountInfoSync()
      const ver = info && info.miniProgram && info.miniProgram.version
      if (ver) v = String(ver)
    }
  } catch (e) { v = 'dev' }
  _version = v
  return v
}

function _normalizeLevel(level) {
  return LEVELS.indexOf(level) >= 0 ? level : 'error'
}

/* ---------- 消息组装 ---------- */

function _compose(msg, extra) {
  const err = (msg instanceof Error) ? msg : ((extra instanceof Error) ? extra : null)
  let raw
  if (msg instanceof Error) {
    raw = msg.message || msg.name || 'Error'
  } else {
    raw = _safeString(msg)
    if (extra && !(extra instanceof Error)) {
      const et = _safeString(extra)
      if (et) raw = raw ? raw + ' ' + et : et
    } else if (extra instanceof Error) {
      const name = extra.name || 'Error'
      raw = (raw ? raw + ' — ' : '') + name + (extra.message ? ': ' + extra.message : '')
    }
  }

  // 小程序 App.onError 传的是整段字符串（首行消息 + 后续 at 帧）：首行当消息，第一帧当堆栈
  const lines = String(raw).split(/\r?\n/).map(function (l) { return l.trim() }).filter(Boolean)
  let frame = _firstFrame(err)
  if (!frame) {
    for (let i = 0; i < lines.length; i++) {
      if (/^at\s/.test(lines[i])) { frame = lines[i]; break }
    }
  }

  let head
  if (err && !(msg instanceof Error)) {
    head = lines.length ? lines[0] : 'Error'   // 附加的 Error 已在 raw 里拼过 name: message
  } else if (err) {
    const name = err.name || 'Error'
    head = name + (err.message ? ': ' + err.message : '')
  } else {
    head = lines.length ? lines[0] : 'Error'
  }

  const parts = ['[' + PLATFORM + '/' + _appVersion() + '] ' + _pagePath(), _oneLine(head)]
  if (frame) parts.push(_oneLine(frame))
  return _scrub(_oneLine(parts.join(' | ')))
}

/* ---------- 队列 / 发送 ---------- */

function _enqueue(level, msg, extra) {
  try {
    const message = _truncate(_compose(msg, extra))
    if (!message) return false
    _queue.push({ level: _normalizeLevel(level), message: message })
    if (_queue.length > MAX_QUEUE) {
      const drop = _queue.length - MAX_QUEUE
      _queue.splice(0, drop)
      _dropped += drop
    }
    // 队列满 = 已经出事了，别等下一次 tick
    if (_queue.length >= MAX_QUEUE) flush()
    return true
  } catch (e) { return false }
}

function _token() {
  try {
    const t = wx.getStorageSync('token')
    return typeof t === 'string' ? t.trim() : ''
  } catch (e) { return '' }
}

function _allowSend(now) {
  if (_lastSentAt && now - _lastSentAt < MIN_SEND_INTERVAL_MS) return false
  while (_sendTimes.length && now - _sendTimes[0] >= 60000) _sendTimes.shift()
  return _sendTimes.length < MAX_SEND_PER_MINUTE
}

function flush() {
  try {
    if (!_queue.length) return false
    const now = Date.now()
    // 上一条还没落定：不并发（并发只会把速率打乱，也帮不上忙）
    if (_inflightAt && now - _inflightAt < INFLIGHT_TIMEOUT_MS) return false
    const token = _token()
    if (!token) return false            // 无 token 不发，留在队列等下次
    if (!_allowSend(now)) return false
    const item = _queue.shift()
    if (!item) return false

    _lastSentAt = now
    _sendTimes.push(now)
    _sent += 1
    _inflightAt = now
    try {
      wx.request({
        url: CONFIG.BASE_URL + REPORT_PATH,
        method: 'POST',
        header: {
          'content-type': 'application/json',
          'Authorization': 'Bearer ' + token,
        },
        data: { level: item.level, message: item.message },
        timeout: REQUEST_TIMEOUT_MS,
        // 成功失败都静默：日志上报的失败不值得打扰用户，也不重试（防风暴）
        success: function () {},
        fail: function () {},
        complete: function () { _inflightAt = 0 },
      })
    } catch (e) {
      _inflightAt = 0
    }
    return true
  } catch (e) {
    return false
  }
}

function _startTimer() {
  if (_timer) return
  try {
    _timer = setTimeout(function () {
      _timer = null
      try { flush() } catch (e) { /* 吞掉 */ }
      _startTimer()
    }, FLUSH_INTERVAL_MS)
  } catch (e) { _timer = null }
}

/* ---------- 对外 API ---------- */

function logWarn(msg, extra) { return _enqueue('warning', msg, extra) }
function logError(msg, extra) { return _enqueue('error', msg, extra) }
// 显式指定级别（白名单外的值一律归一成 error，保证服务端只收到三种级别）
function report(level, msg, extra) { return _enqueue(level, msg, extra) }

function install() {
  if (_installed) return true
  _installed = true
  try { _wrapConsoleError() } catch (e) { /* 吞掉 */ }
  _startTimer()
  return true
}

// 接管 console.error：**保留原输出**，同时把 error 级日志送上去。
// 故意不接管 console.log —— 业务里 console.log 量太大，会瞬间灌满队列和限速额度。
function _wrapConsoleError() {
  if (typeof console === 'undefined' || typeof console.error !== 'function') return
  if (console.error.__hnLoggerWrapped) return
  const orig = console.error
  const wrapped = function () {
    try { orig.apply(console, arguments) } catch (e) { /* 原输出坏了也不影响上报 */ }
    try {
      const args = Array.prototype.slice.call(arguments)
      const first = args[0]
      if (first instanceof Error) {
        _enqueue('error', first, args.length > 1 ? args.slice(1) : undefined)
      } else {
        _enqueue('error', '[console.error] ' + args.map(function (a) { return _safeString(a) }).join(' '))
      }
    } catch (e) { /* 日志自身异常吞掉 */ }
  }
  wrapped.__hnLoggerWrapped = true
  console.error = wrapped
}

// 供验收脚本断言内部状态（业务代码不要依赖）
function _debug() {
  return {
    queued: _queue.length,
    dropped: _dropped,
    sent: _sent,
    installed: _installed,
    pendingLevels: _queue.map(function (i) { return i.level }),
    lastSentAt: _lastSentAt,
  }
}

module.exports = { logWarn, logError, report, install, flush, _debug }
