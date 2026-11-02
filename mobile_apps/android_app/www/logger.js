/* logger.js — 前端日志上报（Android App / 纯浏览器端，POST /api/log/report）
 *
 * 存在意义：后端 logs/frontend.log 长期是 0 字节 —— 两端从来没把前端异常上报过，
 * 用户报"白屏 / 点了没反应"时，服务端一点线索都没有。这里把**未捕获异常**送进后端。
 *
 * 为什么直连 fetch、**不走 app.js 的 api()**：
 *   api() 的 401 分支会自动 refreshToken 并重放原请求。日志上报是"出问题时才跑"的通道：
 *   错误风暴时每条日志都可能触发一次续登，把一次前端异常放大成一场登录风暴。
 *   日志丢了无所谓，登录不能乱 —— 所以这里直连、失败静默丢弃、绝不重试。
 *   另外这里刻意保存 patch 之前的原始 fetch（_rawFetch）用于上报，否则自己的上报请求
 *   会被自己的 fetch 包装器再捕获一次，网络一抖就递归上报。
 *
 * 口径（与 h_n_print/utils/logger.js **必须一致**，改一端就改另一端）：
 *   ① 自动上报只收 error / critical；warning 只留给显式 logWarn()。用户行为日志一律不上报。
 *   ② 内存队列最多 20 条，满了丢最旧并计数（dropped），绝不无界增长。
 *   ③ 一条 HTTP 请求只发一条 message（接口就是单条 message），"合并"靠**发送节流**：
 *      两次发送间隔 ≥10s、每分钟 ≤10 条。服务端限速是 30 条/分钟/IP，这里留 3 倍余量。
 *   ④ 单条 message ≤4000 字符（服务端 4096 会硬截断，截断位置不可控），超出自己截断并
 *      在末尾加「…（已截断）」。
 *   ⑤ message 组成：`[平台/版本] 页面路径 | 错误名: 消息 | 首行堆栈`，
 *      例：`[app/1.1.50] page-print | TypeError: x is not a function | at print.js:12:3`。
 *      "首行堆栈"取第一个 `at ...` 帧（不是整段堆栈），并且整条消息压成一行。
 *   ⑥ **绝不上报**：openid 明文、token、文件内容、URL 里带密钥的 query
 *      （`?token=abc` → `?token=***`，Bearer / X-Printer-Token 的值同样抹掉）。
 *   ⑦ 无 token 时不发（否则服务端 403，纯噪音），留在队列里等下次有 token 再发；
 *      网络失败直接丢弃不重试（弱网里重试只会把队列越堆越大、把错误放大）。
 *   ⑧ 本文件任何异常都必须被 try/catch 吞掉 —— 日志挂了可以，业务页面不能挂。
 *
 * 加载顺序：必须排在 app.js **之后**（要读 app.js 的 BASE_URL / state.token / currentTab
 * 这几个全局词法绑定），见 index.html。
 * 验收：tests/verify_frontend_log_report.js（纯桩环境，不联网）
 */
(function () {
  'use strict';

  var PLATFORM = 'app';
  var REPORT_PATH = '/api/log/report';
  var MAX_QUEUE = 20;
  var MAX_MESSAGE_LEN = 4000;
  var TRUNC_SUFFIX = '…（已截断）';
  var FLUSH_INTERVAL_MS = 3000;
  var MIN_SEND_INTERVAL_MS = 10000;
  var MAX_SEND_PER_MINUTE = 10;
  var REQUEST_TIMEOUT_MS = 8000;
  var LEVELS = ['warning', 'error', 'critical'];
  var DEFAULT_BASE_URL = 'https://hn-space.cn';

  var _queue = [];
  var _dropped = 0;
  var _sent = 0;
  var _lastSentAt = 0;
  var _sendTimes = [];
  var _inflightAt = 0;
  var _installed = false;
  var _timer = null;
  var _rawFetch = null;

  /* ---------- 基础工具（全部吞异常） ---------- */

  function _safeString(v) {
    try {
      if (v === null || v === undefined) return '';
      if (typeof v === 'string') return v;
      if (typeof v === 'number' || typeof v === 'boolean') return String(v);
      if (v instanceof Error) return v.message || v.name || 'Error';
      return JSON.stringify(v);
    } catch (e) {
      try { return String(v); } catch (e2) { return '[不可序列化对象]'; }
    }
  }

  function _oneLine(s) {
    try { return String(s).replace(/\s+/g, ' ').trim(); } catch (e) { return ''; }
  }

  /* 脱敏：密钥/令牌/用户标识一律不落服务端日志 */
  function _scrub(text) {
    var s = _safeString(text);
    // ① query 里的敏感键（?token=abc、&openid=xxx…）—— 键名保留，值抹成 ***
    s = s.replace(/(^|[?&\s])((?:token|access_token|printer_token|open_id|openid|code|secret|password|pwd|key)=)[^&#\s]*/gi,
      function (m, pre, key) { return pre + key + '***'; });
    // ② 请求头形态：Authorization: Bearer xxx / X-Printer-Token: xxx
    s = s.replace(/((?:Authorization|X-Printer-Token)\s*:\s*(?:Bearer\s+)?)[^\s,;'"]+/gi,
      function (m, pre) { return pre + '***'; });
    // ③ 裸 Bearer
    s = s.replace(/\bBearer\s+[A-Za-z0-9._\-]{6,}/g, 'Bearer ***');
    // ④ openid 明文（微信 openid 固定 28 位、o 开头）
    s = s.replace(/\bo[A-Za-z0-9_-]{27}\b/g, 'o***');
    return s;
  }

  function _truncate(text) {
    var s = _safeString(text);
    if (s.length <= MAX_MESSAGE_LEN) return s;
    return s.slice(0, MAX_MESSAGE_LEN - TRUNC_SUFFIX.length) + TRUNC_SUFFIX;
  }

  function _firstFrame(err) {
    try {
      var stack = err && err.stack ? String(err.stack) : '';
      if (!stack) return '';
      var lines = stack.split(/\r?\n/);
      for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim();
        if (/^at\s/.test(line)) return line;
      }
      return lines[0].trim();
    } catch (e) { return ''; }
  }

  /* 页面路径：优先当前可见的页面根（.page-root / .view，App 是单页 + 多页切换），退回 currentTab */
  function _pagePath() {
    try {
      var doc = typeof document !== 'undefined' ? document : null;
      if (doc && typeof doc.querySelectorAll === 'function') {
        var views = doc.querySelectorAll('.page-root, .view');
        for (var i = 0; i < views.length; i++) {
          var el = views[i];
          var visible = el && (!el.style || el.style.display !== 'none');
          if (visible && el.id) return String(el.id).split('?')[0];
        }
      }
    } catch (e) { /* 拿不到就退回 currentTab */ }
    try {
      if (typeof currentTab === 'string' && currentTab) return 'tab-' + currentTab;
    } catch (e2) { /* TDZ / 未定义都走兜底 */ }
    return 'unknown';
  }

  /* 基址：跟 app.js 的 BASE_URL 完全一致（含 window.__HN_TEST_BASE_URL__ 测试钩子） */
  function _baseUrl() {
    try {
      if (typeof BASE_URL === 'string' && BASE_URL) return BASE_URL;
    } catch (e) { /* BASE_URL 未定义（logger 被单独加载）时走兜底 */ }
    try {
      if (typeof window !== 'undefined' && window.__HN_TEST_BASE_URL__) {
        return String(window.__HN_TEST_BASE_URL__);
      }
    } catch (e2) { /* ignore */ }
    return DEFAULT_BASE_URL;
  }

  function _appVersion() {
    try {
      if (typeof window !== 'undefined' && window.AppUpdater &&
          typeof window.AppUpdater.getVersionName === 'function') {
        var v = window.AppUpdater.getVersionName();
        if (v) return String(v);
      }
    } catch (e) { /* 浏览器预览没有原生桥 */ }
    return 'dev';
  }

  /* token：与 app.js 同一来源，state.token 优先（登录后是最新的），再退 localStorage */
  function _token() {
    try {
      if (typeof state === 'object' && state && state.token) return String(state.token).trim();
    } catch (e) { /* state 未定义 */ }
    try {
      var t = typeof localStorage !== 'undefined' ? localStorage.getItem('hn_token') : '';
      return t ? String(t).trim() : '';
    } catch (e2) { return ''; }
  }

  function _normalizeLevel(level) {
    return LEVELS.indexOf(level) >= 0 ? level : 'error';
  }

  /* ---------- 消息组装 ---------- */

  function _compose(msg, extra) {
    var err = (msg instanceof Error) ? msg : ((extra instanceof Error) ? extra : null);
    var raw;
    if (msg instanceof Error) {
      raw = msg.message || msg.name || 'Error';
    } else {
      raw = _safeString(msg);
      if (extra && !(extra instanceof Error)) {
        var et = _safeString(extra);
        if (et) raw = raw ? raw + ' ' + et : et;
      } else if (extra instanceof Error) {
        raw = (raw ? raw + ' — ' : '') + (extra.name || 'Error') + (extra.message ? ': ' + extra.message : '');
      }
    }

    // 字符串里若自带堆栈（如 console.error 传了整段 stack），首行当消息、第一帧当堆栈
    var lines = String(raw).split(/\r?\n/).map(function (l) { return l.trim(); }).filter(Boolean);
    var frame = _firstFrame(err);
    if (!frame) {
      for (var i = 0; i < lines.length; i++) {
        if (/^at\s/.test(lines[i])) { frame = lines[i]; break; }
      }
    }

    var head;
    if (err && !(msg instanceof Error)) {
      head = lines.length ? lines[0] : 'Error';
    } else if (err) {
      head = (err.name || 'Error') + (err.message ? ': ' + err.message : '');
    } else {
      head = lines.length ? lines[0] : 'Error';
    }

    var parts = ['[' + PLATFORM + '/' + _appVersion() + '] ' + _pagePath(), _oneLine(head)];
    if (frame) parts.push(_oneLine(frame));
    return _scrub(_oneLine(parts.join(' | ')));
  }

  /* ---------- 队列 / 发送 ---------- */

  function _enqueue(level, msg, extra) {
    try {
      var message = _truncate(_compose(msg, extra));
      if (!message) return false;
      _queue.push({ level: _normalizeLevel(level), message: message });
      if (_queue.length > MAX_QUEUE) {
        var drop = _queue.length - MAX_QUEUE;
        _queue.splice(0, drop);
        _dropped += drop;
      }
      // 队列满 = 已经出事了，别等下一次 tick
      if (_queue.length >= MAX_QUEUE) flush();
      return true;
    } catch (e) { return false; }
  }

  function _allowSend(now) {
    if (_lastSentAt && now - _lastSentAt < MIN_SEND_INTERVAL_MS) return false;
    while (_sendTimes.length && now - _sendTimes[0] >= 60000) _sendTimes.shift();
    return _sendTimes.length < MAX_SEND_PER_MINUTE;
  }

  function _send(item) {
    var token = _token();
    if (!token) return false;               // 无 token 不发，留在队列等下次
    var doFetch = _rawFetch;
    if (!doFetch) {
      try {
        doFetch = (typeof window !== 'undefined' && typeof window.fetch === 'function')
          ? window.fetch.bind(window) : null;
      } catch (e) { doFetch = null; }
    }
    if (!doFetch) return false;

    var opts = {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + token,
      },
      body: JSON.stringify({ level: item.level, message: item.message }),
    };
    var timer = null;
    try {
      if (typeof AbortController !== 'undefined') {
        var ctrl = new AbortController();
        opts.signal = ctrl.signal;
        timer = setTimeout(function () { try { ctrl.abort(); } catch (e) { /* ignore */ } }, REQUEST_TIMEOUT_MS);
      }
    } catch (e) { /* 没有 AbortController 就不设超时，靠 fetch 自己 */ }

    var clear = function () { if (timer) { try { clearTimeout(timer); } catch (e) { /* ignore */ } timer = null; } };
    try {
      Promise.resolve(doFetch(_baseUrl() + REPORT_PATH, opts)).then(clear, clear);
    } catch (e) {
      clear();
      return false;
    }
    return true;
  }

  function flush() {
    try {
      if (!_queue.length) return false;
      var now = Date.now();
      if (_inflightAt && now - _inflightAt < REQUEST_TIMEOUT_MS + 5000) return false;
      if (!_token()) return false;
      if (!_allowSend(now)) return false;
      var item = _queue.shift();
      if (!item) return false;

      _lastSentAt = now;
      _sendTimes.push(now);
      _sent += 1;
      _inflightAt = now;
      var ok = false;
      try {
        ok = _send(item);
      } finally {
        _inflightAt = 0;   // fetch 是异步的，这里只防同一 tick 里重入
      }
      return ok;
    } catch (e) {
      return false;
    }
  }

  function _startTimer() {
    if (_timer) return;
    try {
      _timer = setTimeout(function () {
        _timer = null;
        try { flush(); } catch (e) { /* 吞掉 */ }
        _startTimer();
      }, FLUSH_INTERVAL_MS);
    } catch (e) { _timer = null; }
  }

  /* ---------- 全局钩子 ---------- */

  function _hookOnerror() {
    if (typeof window === 'undefined') return;
    var prev = window.onerror;
    if (prev && prev.__hnLoggerWrapped) return;
    var wrapped = function (message, source, lineno, colno, error) {
      try {
        if (error) {
          _enqueue('error', error);
        } else {
          var where = _scriptPath(source);
          _enqueue('error', String(message) + (where ? ' @ ' + where + ':' + lineno + ':' + colno : ''));
        }
      } catch (e) { /* 吞掉 */ }
      try {
        if (typeof prev === 'function') return prev.apply(this, arguments);
      } catch (e2) { /* 原 handler 坏了也不能影响 */ }
      return false;
    };
    wrapped.__hnLoggerWrapped = true;
    window.onerror = wrapped;
  }

  /* 脚本 URL 只留文件名，query（可能带 token）整段丢掉 */
  function _scriptPath(u) {
    try {
      var s = String(u || '').split('?')[0].split('#')[0];
      var m = s.match(/[^/\\]+\.(?:js|html|css)$/);
      return m ? m[0] : '';
    } catch (e) { return ''; }
  }

  function _hookRejection() {
    if (typeof window === 'undefined' || typeof window.addEventListener !== 'function') return;
    window.addEventListener('unhandledrejection', function (ev) {
      try {
        var reason = ev && ev.reason;
        if (reason) _enqueue('error', reason);
        else _enqueue('error', 'unhandledrejection（无 reason）');
      } catch (e) { /* 吞掉 */ }
    });
  }

  /* fetch 兜底：只报 5xx 和网络层失败。4xx（尤其 401）不报 —— 那是业务分支，报了纯噪音。 */
  function _patchFetch() {
    if (typeof window === 'undefined' || typeof window.fetch !== 'function') return;
    if (window.fetch.__hnLoggerPatched) return;
    var raw = window.fetch.bind(window);
    _rawFetch = raw;
    var wrapped = function (input, init) {
      var method = (init && init.method) ? String(init.method).toUpperCase() : 'GET';
      var path = _requestPath(input);
      var p;
      try {
        p = raw(input, init);
      } catch (e) {
        _enqueue('error', 'fetch 同步失败: ' + method + ' ' + path, e);
        throw e;
      }
      return Promise.resolve(p).then(function (res) {
        try {
          if (res && typeof res.status === 'number' && res.status >= 500) {
            _enqueue('error', 'HTTP ' + res.status + ': ' + method + ' ' + path);
          }
        } catch (e) { /* 吞掉 */ }
        return res;
      }, function (err) {
        try {
          _enqueue('error', '网络错误: ' + method + ' ' + path, err);
        } catch (e) { /* 吞掉 */ }
        throw err;   // 不改原语义：调用方的 catch 照常收到
      });
    };
    wrapped.__hnLoggerPatched = true;
    window.fetch = wrapped;
  }

  function _requestPath(input) {
    try {
      var s = typeof input === 'string' ? input : (input && input.url) ? String(input.url) : '';
      s = s.split('?')[0].split('#')[0];
      var m = s.match(/\/api\/[A-Za-z0-9_\-/]+/);
      return m ? m[0] : s.slice(-80);
    } catch (e) { return ''; }
  }

  /* ---------- 对外 API ---------- */

  function logWarn(msg, extra) { return _enqueue('warning', msg, extra); }
  function logError(msg, extra) { return _enqueue('error', msg, extra); }
  function report(level, msg, extra) { return _enqueue(level, msg, extra); }

  function install() {
    if (_installed) return true;
    _installed = true;
    try { _hookOnerror(); } catch (e) { /* 吞掉 */ }
    try { _hookRejection(); } catch (e) { /* 吞掉 */ }
    try { _patchFetch(); } catch (e) { /* 吞掉 */ }
    _startTimer();
    return true;
  }

  /* 供验收脚本断言内部状态（业务代码不要依赖） */
  function _debug() {
    return {
      queued: _queue.length,
      dropped: _dropped,
      sent: _sent,
      installed: _installed,
      pendingLevels: _queue.map(function (i) { return i.level; }),
      lastSentAt: _lastSentAt,
    };
  }

  try {
    if (typeof window !== 'undefined') {
      window.HNLogger = {
        logWarn: logWarn,
        logError: logError,
        report: report,
        install: install,
        flush: flush,
        _debug: _debug,
      };
    }
  } catch (e) { /* 挂不上去也不能炸页面 */ }
})();
