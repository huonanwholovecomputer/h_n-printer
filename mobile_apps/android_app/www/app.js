/* HN Cloud Print — Android App Core
 * 状态 / 请求封装(401 自动重登) / 主题(自动·浅色·深色) / 视图栈
 * 橡皮筋滚动引擎（移植小程序自定义滚动物理）/ 手势总线 / 页面转场
 */

const DEFAULT_BASE_URL = 'https://hn-space.cn';
/* S2-09（DS-22/MB-06）：API 基址是**编译期常量**，不再允许 localStorage 覆盖。
 *
 * 旧写法 `localStorage.getItem('hn_base_url') || DEFAULT_BASE_URL` 把"把 API 基址改到任意
 * 域名"变成产品能力：WebView 里任何一次脚本执行（XSS / 未来引入的第三方 SDK）都能持久化
 * 改写基址，之后所有请求（含 Authorization 头）都发往攻击者。它唯一的使用者是静态测试桩，
 * 所以改成**显式的测试钩子** `window.__HN_TEST_BASE_URL__`（由测试在 addInitScript 里设置，
 * 生产包与页面自身永不设置）。改动同时要同步 tests/*.mjs 与 android_app/tools/*.mjs。 */
const TEST_BASE_URL = (typeof window !== 'undefined' && window.__HN_TEST_BASE_URL__) || '';
const BASE_URL = TEST_BASE_URL || DEFAULT_BASE_URL;

const state = {
  token: localStorage.getItem('hn_token') || '',
  openid: localStorage.getItem('hn_openid') || '',
  // 设备账号（dev_ 前缀）：绑定微信账号后 token 变成微信 openid，devOpenid 保留用于解绑回退
  devOpenid: localStorage.getItem('hn_dev_openid') || '',
  role: localStorage.getItem('hn_role') || 'guest',
  isSuperAdmin: localStorage.getItem('hn_is_super') === '1',
  nickname: localStorage.getItem('hn_nickname') || '',
  avatarUrl: localStorage.getItem('hn_avatar') || '',
  licenseInfo: null,
  tempUntil: '',
  hasTempAccess: false,
  themeMode: localStorage.getItem('hn_theme_mode') || 'auto',
};

let _refreshing = null;

/* ================= 请求封装 ================= */

// D5（最后一条）：全 APP 原先**没有任何请求超时**。后端与本地工具都有超时（本地工具
// CONNECT_REQUEST_TIMEOUT=20s），而这里是唯一缺口：`await api(...)` 遇到"TCP 连上但收不到数据"
// （弱网、校园网抖动、代理异常）会**永久挂起** —— 界面停在加载态，既不报错也不重试。
// 用 AbortController 设默认上限；需要长耗时的调用可传 opts.timeoutMs 覆盖（传 0 表示不限）。
// S1-09：默认上限收敛到 **15s**（计划要求 10–15s 区间；原先 20s 偏长，弱网下用户要干等）。
const API_TIMEOUT_MS = 15000;
// 注：文件上传走 XMLHttpRequest（在 print.js），它的超时常量由 print.js 自己定义
// （`UPLOAD_TIMEOUT_MS`）—— 两个文件各自的超时上限都在使用它的文件里，不跨文件依赖词法作用域。

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
  const timeoutMs = (opts.timeoutMs === undefined) ? API_TIMEOUT_MS : opts.timeoutMs;
  const fetchOpts = Object.assign({}, opts, { headers });
  delete fetchOpts.timeoutMs;              // 自定义字段不传给 fetch
  let timer = null;
  let controller = null;
  if (!opts.signal && timeoutMs > 0 && typeof AbortController !== 'undefined') {
    controller = new AbortController();
    fetchOpts.signal = controller.signal;
    timer = setTimeout(() => controller.abort(), timeoutMs);
  }
  let res;
  try {
    res = await fetch(BASE_URL + path, fetchOpts);
  } catch (e) {
    // AbortController 触发的取消 = 超时（调用方自己传 signal 时不算我们的超时）
    const aborted = !!(e && (e.name === 'AbortError' || e.code === 20));
    const err = new Error(aborted ? '请求超时' : '网络错误');
    err.network = true;
    err.timeout = aborted;
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
  }
  if (res.status === 401 && !opts._retried) {
    const ok = await refreshToken();
    if (ok) return api(path, Object.assign({}, opts, { _retried: true }));
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* 非 JSON */ }
  return { status: res.status, data };
}

async function refreshToken() {
  if (_refreshing) return _refreshing;
  _refreshing = (async () => {
    try {
      // S1-09：这条是**裸 fetch**（不走 api()），必须自己挂 AbortController 超时，
      // 否则弱网/校园网抖动时会永久挂起（401 刷新卡住 ⇒ 所有重试都排队等它）。
      const ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
      const t = ctrl ? setTimeout(() => ctrl.abort(), API_TIMEOUT_MS) : null;
      let r;
      try {
        r = await fetch(BASE_URL + '/api/device_login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ device_id: getDeviceId() }),
          ...(ctrl ? { signal: ctrl.signal } : {}),
        });
      } finally {
        if (t) clearTimeout(t);
      }
      const data = await r.json();
      if (data.success && data.token) {
        setAuth(data.token, data.openid || '');
        return true;
      }
      return false;
    } catch (e) {
      return false;
    } finally {
      _refreshing = null;
    }
  })();
  return _refreshing;
}

function setAuth(token, openid) {
  state.token = token;
  state.openid = openid || '';
  if (state.openid && state.openid.indexOf('dev_') === 0 && !state.devOpenid) {
    state.devOpenid = state.openid;
    localStorage.setItem('hn_dev_openid', state.openid);
  }
  localStorage.setItem('hn_token', token);
  localStorage.setItem('hn_openid', state.openid);
}

/* ============================================================
 * S1-09：轮询自调度器（setInterval → setTimeout 链 + 失败指数退避）
 * ============================================================
 * 旧写法 `setInterval(fn, 30s)` 不管上一轮是否回来：弱网下 api() 要 20s 才超时，
 * 连续失败时仍以固定频率重试（把弱网里本就不通的请求一遍遍打出去，还叠着跑）。
 * 现在：**上一次任务落定后**才排下一次；失败 1.5× 指数退避（上限 5 分钟），
 * 成功立刻恢复基线间隔。taskFn(done) / 或返回 Promise（resolve=成功 / reject=失败）。
 * me.js 与本文件共享同一个 window 作用域，直接复用这一个实现。
 */
const POLL_BACKOFF_FACTOR = 1.5;
const POLL_BACKOFF_MAX_MS = 300000;

function createPollScheduler(taskFn, baseDelayMs, options) {
  const opts = options || {};
  const base = Math.max(1, Number(baseDelayMs) || 30000);
  const maxDelay = Number(opts.maxDelayMs) || POLL_BACKOFF_MAX_MS;
  const factor = Number(opts.factor) || POLL_BACKOFF_FACTOR;
  // immediate=false：start() 后先等一个基线间隔再跑（调用方已自己先拉过一次时用）
  const immediate = opts.immediate !== false;
  let timer = null;
  let delay = base;
  let stopped = true;
  let running = false;

  function schedule(ok) {
    if (stopped) return;
    if (ok === false) delay = Math.min(Math.round(delay * factor), maxDelay);
    else if (ok === true) delay = base;
    timer = setTimeout(tick, delay);
  }

  function tick() {
    timer = null;
    if (stopped || running) return;
    running = true;
    let settled = false;
    const done = (ok) => {
      if (settled) return;
      settled = true;
      running = false;
      schedule(ok);
    };
    try {
      const ret = taskFn(done);
      if (ret && typeof ret.then === 'function') {
        ret.then(() => done(true), () => done(false));
      }
    } catch (e) {
      done(false);
    }
  }

  return {
    start() {
      if (!stopped) return;
      stopped = false;
      delay = base;
      if (immediate) tick();
      else timer = setTimeout(tick, delay);
    },
    stop() {
      stopped = true;
      running = false;
      if (timer) { clearTimeout(timer); timer = null; }
    },
    isRunning() { return !stopped; },
    currentDelay() { return delay; },
  };
}

// 是否已绑定微信账号：当前 token 是微信 openid，且与本地设备账号不同
function isBound() {
  return !!(state.devOpenid && state.openid && state.openid !== state.devOpenid);
}

async function ensureLogin() {
  if (state.token) return true;
  const ok = await refreshToken();
  if (ok) await refreshSession();
  return ok;
}

function getDeviceId() {
  let id = localStorage.getItem('hn_device_id');
  if (!id) {
    id = 'dev_' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
    localStorage.setItem('hn_device_id', id);
  }
  return id;
}

/* ================= 会话 ================= */

async function refreshSession() {
  if (!state.token) return;
  const [me, profile] = await Promise.all([
    api('/api/me').catch(() => null),
    api('/api/profile').catch(() => null),
  ]);
  if (me && me.data && me.data.success) {
    const d = me.data;
    state.role = d.role || 'guest';
    state.isSuperAdmin = !!d.is_super_admin;
    state.tempUntil = d.temp_until || '';
    state.hasTempAccess = !!d.has_temp_access;
    state.licenseInfo = d.license_info || null;
    localStorage.setItem('hn_role', state.role);
    localStorage.setItem('hn_is_super', state.isSuperAdmin ? '1' : '0');
    syncThemeFromServer(d.theme_mode);
  }
  if (profile && profile.data && profile.data.success) {
    state.nickname = profile.data.nickname || '';
    state.avatarUrl = profile.data.avatar_url || '';
    localStorage.setItem('hn_nickname', state.nickname);
    if (state.avatarUrl) localStorage.setItem('hn_avatar', state.avatarUrl);
  }
  updateSessionUI();
}

function updateSessionUI() {
  if (typeof refreshMeUI === 'function') refreshMeUI();
  if (typeof refreshPrintRoleUI === 'function') refreshPrintRoleUI();
}

/* ================= 主题 ================= */

function effectiveDark(mode) {
  if (mode === 'dark') return true;
  if (mode === 'light') return false;
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
}

function applyNativeSafeArea() {
  const b = window.AndroidBars;
  if (!b) return;
  document.documentElement.style.setProperty('--safe-top', (b.getStatusBarHeight() || 0) + 'px');
  document.documentElement.style.setProperty('--safe-bottom', (b.getNavigationBarHeight() || 0) + 'px');
  // 安全区注入后导航栏高度变化，主题按钮需重新定位（否则被导航栏盖住）
  if (typeof positionThemeToggle === 'function') positionThemeToggle();
}

function syncNativeStatusBar(dark) {
  const b = window.AndroidBars;
  if (b && b.setDark) b.setDark(dark);
}

/* 主题切换动画：优先用 View Transitions（浏览器把切换前后各截一张整页快照，
   在合成线程做交叉淡入，动画成本与 DOM 元素数量无关）；不支持时回退逐元素 CSS 过渡。 */
let _themeFirstApply = true;
let _themeSchemeT = null;
const _mqReduce = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
function prefersReducedMotion() { return !!(_mqReduce && _mqReduce.matches); }
function setColorScheme(dark) { document.documentElement.style.colorScheme = dark ? 'dark' : 'light'; }

function applyTheme(mode, opts) {
  opts = opts || {};
  state.themeMode = mode;
  localStorage.setItem('hn_theme_mode', mode);
  const dark = effectiveDark(mode);
  syncNativeStatusBar(dark);
  const toggle = document.getElementById('themeToggle');
  const iconText = { auto: '🌓', dark: '🌙', light: '☀️' }[mode] || '🌓';
  if (toggle) {
    // 图标包在 .theme-toggle-icon 内层 span（承载 emoji 竖直居中的位移补偿），
    // 若 span 不存在（极端情况下 DOM 被改写）则回退直接写按钮文本
    const icon = toggle.querySelector('.theme-toggle-icon');
    if (icon) icon.textContent = iconText;
    else toggle.textContent = iconText;
  }
  const root = document.documentElement;
  function commitContent() {
    // 深色类同时挂到 body（变量级联）、背景层与所有 modal-mask
    document.body.classList.toggle('theme-dark', dark);
    document.body.classList.toggle('dark', dark);
    const bg = document.getElementById('themeBgLayer');
    if (bg) bg.classList.toggle('bg-dark', dark);
    document.querySelectorAll('.modal-mask').forEach(m => m.classList.toggle('theme-dark', dark));
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', dark ? '#1C1C1E' : '#F2F2F7');
  }

  if (_themeFirstApply || prefersReducedMotion()) {
    // 首帧不动画（避免加载闪变）；用户偏好减少动效时直接切换。
    commitContent();
    setColorScheme(dark);
  } else if (document.startViewTransition) {
    // 快照期间禁用逐元素过渡：否则新快照会截到过渡起始值（颜色还没变），交叉淡入看不到变化。
    root.classList.add('theme-vt');
    const vt = document.startViewTransition(function () {
      commitContent();
      setColorScheme(dark);
    });
    const done = function () { root.classList.remove('theme-vt'); };
    if (vt && vt.finished && vt.finished.finally) vt.finished.finally(done);
    else setTimeout(done, 1000);
  } else {
    // 降级：先冻结当前 color-scheme。Chromium 在 color-scheme 变化时会重启后代 color 过渡
    // （动画 currentTime 反复归零），使文字颜色被拖到约 900ms、而背景 ~480ms 就到位。
    root.style.colorScheme = getComputedStyle(root).colorScheme;
    commitContent();
    clearTimeout(_themeSchemeT);
    _themeSchemeT = setTimeout(function () { setColorScheme(dark); }, 520);
  }
  _themeFirstApply = false;

  if (!opts.skipServer) syncThemeToServer();
  updateSessionUI();
}

function toggleTheme() {
  const order = ['auto', 'dark', 'light'];
  const next = order[(order.indexOf(state.themeMode) + 1) % 3];
  applyTheme(next);
}

function syncThemeToServer() {
  if (!state.token) return;
  api('/api/me/theme', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ theme_mode: state.themeMode }),
  }).catch(() => {});
}

function syncThemeFromServer(serverTheme) {
  if (serverTheme && ['auto', 'dark', 'light'].includes(serverTheme) && serverTheme !== state.themeMode) {
    applyTheme(serverTheme, { skipServer: true });
  }
}

/* ================= 手势总线（左滑手势让出垂直滚动） ================= */

const gestureBus = { horizontal: false };

/* ================= 可调曲线 JS 惯性引擎（原型：真机试手感，参数见 FLING） ================= */

const scrollEngines = {};

// 惯性曲线参数 —— 真机调手感时改这里
const FLING = {
  power: 0.9,          // 摩擦幂次 p：dv/dt = -k·|v|^p。越接近 1，低速段拖尾越长（指数衰减长尾）
  friction: 0.0018,    // 摩擦系数 k（px/ms 量纲，数值越大停得越快）
  lowSpeedThreshold: 0.12, // 低于该速度进入低速段（px/ms）
  lowSpeedFactor: 0.6,     // 低速段摩擦乘数（越小低速溜得越久，总距离基本不变）
  releaseWindowMs: 50, // 抬手初速的最近窗口 ms（越短越能抓住快速短甩的峰值）
  maxVelocity: 8,      // 单次甩动速度上限 px/ms（模拟平台 fling 速度上限，Android 原生约 8~15）
  boost: true,         // 连续同向甩动速度叠加（模拟 Chromium Fling Booster）
  boostMinSpeed: 0.35, // 触发叠加的最低速度 px/ms
  boostWindow: 300,    // 可叠加的时间窗口 ms
  stopVelocity: 0.01,  // 低于此速度停止（px/ms，越小低速段溜得越久）
  rubberMax: 80,       // 惯性撞边的最大形变 px（弹簧最大压缩量）
  edgeSpringOmega: 0.016, // 弹簧自然角频率 rad/ms：越大弹簧越硬、压缩越浅
  edgeSpringDamping: 0.5, // 压入段阻尼比：减震器吸收能量，速度平滑降到 0
  edgeRecoverMsPerPx: 13, // 恢复阶段时长 ms/px：越大回弹越慢（S 曲线：加速→近似恒速→减速→停）
  edgeRecoverAccel: 0.25, // 恢复段加速占比（0~1）：越小加速越短、减速越长
  snapSpd: 0.4,        // 越界回弹速度（越大越快）
  dampMax: 130,        // 拖拽越界阻尼上限 px
  settleOmega: 0.012,  // 内容变短收尾：跟随弹簧角频率 rad/ms
  settleDamping: 0.9,  // 内容变短收尾：阻尼比（接近临界，快速到位无过冲）
};

class FlingEngine {
  constructor(scrollerEl, contentEl, opts) {
    this.el = scrollerEl;
    this.content = contentEl;
    this.opts = opts || {};
    this.y = 0;
    this.minY = 0;
    this.maxY = 0;
    this.scrollerH = 0;
    this.contentH = 0;
    this.vel = 0;
    this.inDecel = false;
    this.tick = null;
    this._trackId = null;
    this._lastY = 0;
    this._lastT = 0;
    this._moved = false;
    this._velSamples = [];
    this._startX = 0;
    this._startY = 0;
    this._directionLocked = false;
    this._horizontalGesture = false;
    this._nestedScroll = null;
    this._nestedEl = null;   // 本手势起于哪个内层滚动容器（外层到边界后交还内层用）
    this._wheelTimer = null;
    this._measureTimer = null;
    this._prevFlingVel = 0;
    this._lastFlingT = 0;
    this._spring = null; // 边界弹簧状态 { target, dir, x, vx, xMax, compressing, t0, durR }
    this._destroyed = false;

    // 接管滚动：外层裁剪 + 内容用 transform 驱动。
    // touch-action 用 pan-y（不是 none）：内层 .file-list-scroll 等真滚动容器需要浏览器
    // 原生纵向滚动才能滚（touch-action 取"元素+祖先"交集，祖先 none 会让内层完全滚不动，
    // 手势卡死在内层）；引擎在自己接管的每一帧 preventDefault 取消原生滚动。
    this.el.classList.add('js-scroll');
    this.el.style.overflow = 'hidden';
    this.el.style.touchAction = 'pan-y';

    // 内层滚动行程的手势内估计（见 onTouchStart；NaN/undefined 一律按 0 处理）
    this._nestMax = 0;
    this._nestEst = 0;
    this._nestDir = 0;

    this._debouncedMeasure = () => {
      if (this._measureTimer) clearTimeout(this._measureTimer);
      this._measureTimer = setTimeout(() => { this._measureTimer = null; this.measure(); }, 100);
    };
    this._bind();
    this.measure();
    window.addEventListener('resize', this._debouncedMeasure);
  }

  destroy() {
    this._destroyed = true;
    this.cancel();
    window.removeEventListener('resize', this._debouncedMeasure);
    this.el.classList.remove('js-scroll');
    this.el.style.overflow = '';
    this.el.style.touchAction = '';
    this.el.removeEventListener('touchstart', this._hTs);
    this.el.removeEventListener('touchmove', this._hTm);
    this.el.removeEventListener('touchmove', this._hTmPassive);
    this.el.removeEventListener('touchend', this._hTe);
    this.el.removeEventListener('touchcancel', this._hTe);
    this.el.removeEventListener('wheel', this._hW);
  }

  _bind() {
    this._hTs = (e) => this.onTouchStart(e);
    this._hTm = (e) => this.onTouchMove(e);
    this._hTmPassive = (e) => this.onTouchMovePassive(e);
    this._hTe = (e) => this.onTouchEnd(e);
    this._hW = (e) => this.onWheel(e);
    this._passiveNested = false;
    this.el.addEventListener('touchstart', this._hTs, { passive: true });
    this.el.addEventListener('touchmove', this._hTm, { passive: false });
    this.el.addEventListener('touchend', this._hTe, { passive: true });
    this.el.addEventListener('touchcancel', this._hTe, { passive: true });
    this.el.addEventListener('wheel', this._hW, { passive: false });
  }

  /**
   * 内层原生滚动期间把 touchmove 切成 passive。
   * 原因：祖先上只要挂着**非 passive** 的 touchmove，浏览器就必须为每个 touchmove 等 JS，
   * 内层滚动只能走主线程（合成器快路径被禁掉）—— 本应用 DOM 重（blur/渐变/阴影），
   * 于是「手指滑到文件列表区域特别卡」。起手在内层滚动容器时换 passive，浏览器即可
   * 用合成器线程滚内层（丝滑）；引擎在被动回调里仍能按 scrollTop 判边界、驱动外层，
   * 只是不能 preventDefault —— 而边界处内层本来就滚不动，本就无需 cancel。
   */
  _usePassiveNested(enable) {
    if (!!enable === !!this._passiveNested) return;
    this._passiveNested = !!enable;
    if (enable) {
      this.el.removeEventListener('touchmove', this._hTm);
      this.el.addEventListener('touchmove', this._hTmPassive, { passive: true });
    } else {
      this.el.removeEventListener('touchmove', this._hTmPassive);
      this.el.addEventListener('touchmove', this._hTm, { passive: false });
    }
  }

  measure() {
    const vp = this.el.getBoundingClientRect().height || 0;
    const ch = this.content.getBoundingClientRect().height || this.content.scrollHeight || 0;
    this.scrollerH = vp;
    this.contentH = ch;
    // 底部留白对齐小程序：(12+110)rpx 按屏宽换算 + 底部安全区
    const rpx = (this.el.clientWidth || 375) / 750;
    let tabOverlay = Math.round(122 * rpx);
    try {
      const sb = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--safe-bottom'));
      if (isFinite(sb)) tabOverlay += Math.round(sb);
    } catch (e) { /* 忽略，安全区取 0 */ }
    this.maxY = Math.max(0, ch - vp + (this.opts.bottomPad || 20) + tabOverlay);
    // 内容变短（收起卡片等）：单段 S 曲线平滑收到新底部；
    // 触摸/惯性中不动（松手由弹簧收尾）；内容重新变长时中止进行中的收尾弹簧
    if (this.y > this.maxY && this._trackId === null && !this.inDecel) {
      this._settleShrink();
    } else if (this._spring && this._spring.settle) {
      this.cancel();
    }
  }

  scheduleMeasure(delay) {
    if (this._measureTimer) clearTimeout(this._measureTimer);
    this._measureTimer = setTimeout(() => { this._measureTimer = null; this.measure(); }, delay || 100);
  }

  cancel() {
    if (this.tick) { cancelAnimationFrame(this.tick); this.tick = null; }
    this._spring = null;
  }

  // ---- 触摸拖动 ----

  onTouchStart(e) {
    // 内层可滚动列表（文件列表 / 滚轮选择器 / 订单详情里的文件列表）：能滚时交给原生（同小程序 WXS 的口径）
    const nestedEl = (e.target && e.target.closest && e.target.closest('.file-list-scroll, .wheel-viewport, .detail-files-scroll')) || null;
    this._nestedScroll = nestedEl;
    // 本手势起于内层：外层滚到边界后反向拖拽时交还内层（双向接力）
    this._nestedEl = nestedEl;
    // 起手在内层且内层真能滚 → 本手势改用 passive touchmove（内层走合成器线程，不卡）
    this._usePassiveNested(!!nestedEl && (nestedEl.scrollHeight - nestedEl.clientHeight) > 1);
    // 内层行程的**手势内累计估计**（2026-11-09）：touchmove 里读到的 el.scrollTop 是上一帧的值
    // （WebView 的滚动由合成器线程异步应用），直接拿它算「还剩多少行程」会少算 ⇒ 贴边那一帧
    // 内层还在滚、外层也按"没行程了"接管 ⇒ 内外层同时位移（现场：「滑列表整页跟着动」）。
    // 所以起手记一份估计值，之后按本手势方向的消耗量累加；反向拖动时才回读真实 scrollTop 校准。
    this._nestMax = nestedEl ? Math.max(0, nestedEl.scrollHeight - nestedEl.clientHeight) : 0;
    this._nestEst = nestedEl ? Math.max(0, Math.min(this._nestMax, nestedEl.scrollTop || 0)) : 0;
    this._nestDir = 0;
    const touches = e.touches || [];
    if (touches.length > 0) {
      this._startX = touches[0].clientX;
      this._startY = touches[0].clientY;
      this._directionLocked = false;
      this._horizontalGesture = false;
    }
    this.cancel();
    this.inDecel = false;
    this._touchStartY0 = this.y;
    const p = touches[0];
    if (!p) return;
    this._trackId = p.identifier;
    this._lastY = p.clientY;
    this._lastT = Date.now();
    this.vel = 0;
    this._velSamples = [{ t: this._lastT, y: p.clientY }];
    this._moved = false;
  }

  onTouchMove(e) {
    this._handleTouchMove(e, true);
  }

  /* 内层原生滚动用的被动版本：同样判边界、同样能驱动外层，只是不能 preventDefault
     （边界处内层已滚不动，本就不需要 cancel；若强行调用还会被浏览器告警） */
  onTouchMovePassive(e) {
    this._handleTouchMove(e, false);
  }

  _handleTouchMove(e, canPrevent) {
    if (gestureBus.horizontal) return; // 左滑卡片 / 滑块让出
    if (this._trackId === null) return;
    const touches = e.touches || [];
    if (touches.length === 0) return;
    // 方向锁定：横向手势让出垂直滚动
    const dx = touches[0].clientX - this._startX;
    const dy = touches[0].clientY - this._startY;
    if (!this._directionLocked) {
      if (Math.abs(dx) > 5 || Math.abs(dy) > 5) {
        this._directionLocked = true;
        if (Math.abs(dx) > Math.abs(dy)) { this._horizontalGesture = true; gestureBus.horizontal = true; return; }
      } else {
        return;
      }
    }
    if (this._horizontalGesture) return;
    const cur = touches[0];
    const now = Date.now();
    let ddy = cur.clientY - this._lastY;
    // 嵌套滚动接力（与小程序 WXS scroll.wxs 同口径）：
    // ddy < 0 手指上滑（外层 y 增大 / 内层滚到底）；ddy > 0 手指下滑（外层 y 减小 / 内层滚到顶）。
    // 内层还能往手势方向滚 → 让原生滚；贴边 → 引擎接管外层；外层到边界反向 → 再交还内层。
    // 门槛用 ddy !== 0（不再是 >=0.5）：累计估计要靠每一帧的位移推进，慢速拖动时单帧常小于
    // 0.5px，若把这些帧跳过，估计值会明显落后于真实 scrollTop ⇒ 贴边时多出一段"内层不动、
    // 外层也不动"的死区。ddy === 0 的重复事件直接跳过（不推进、不判定）。
    if (ddy !== 0) {
      if (this._nestedScroll) {
        const el = this._nestedScroll;
        const maxTop = Math.max(0, el.scrollHeight - el.clientHeight);
        // 手势内累计估计（见 onTouchStart 里的注释）：只有方向反转时才回读真实值校准
        const dir = ddy < 0 ? 1 : -1;
        if (this._nestDir && dir !== this._nestDir) {
          this._nestEst = el.scrollTop;
        }
        this._nestDir = dir;
        const est = Math.max(0, Math.min(maxTop, this._nestEst));
        // room：内层在**本手势方向**上还剩多少行程（上滑剩到底的、下滑剩到顶的）
        const room = ddy < 0 ? maxTop - est : est;
        if (room >= Math.abs(ddy)) {
          this._nestEst = Math.max(0, Math.min(maxTop, est - ddy));
          this._lastY = cur.clientY;
          this._lastT = now;
          return;   // 这一帧内层自己吃得下 → 整帧交原生
        }
        // 内层这一帧只能吃掉 room，剩下的由外层补：本帧位移仍是 1:1 跟手。
        // （旧写法在 room < |ddy| 的这一帧整帧 return ⇒ 丢掉 |ddy|-room 的位移；下一帧才接管，
        //   手感就是"到边界那一下卡一下、要再划一次"——用户反馈的正是这个。）
        // 本帧内层仍会被原生滚到边界（passive 下拦不住，也不必拦：room 已按实计），
        // 外层只补余量 ⇒ 两者相加正好 = 本帧真实位移，不叠加、不跳。
        // Math.max(0, room)：内层若已被橡皮筋拉过边界（scrollTop 越界），余量按 0 计，
        // 否则会把越界量当成"负余量"加回去，变成一次跳变。
        ddy = ddy < 0 ? ddy + Math.max(0, room) : ddy - Math.max(0, room);
        this._nestEst = ddy < 0 ? maxTop : 0;
        this._nestedScroll = null; // 内层到边界 → 引擎接管
      } else if (this._nestedEl) {
        const el = this._nestedEl;
        const outerBlocked = ddy > 0
          ? this.y <= this.minY + 0.5
          : (this.maxY > 0 && this.y >= this.maxY - 0.5);
        const maxTop = Math.max(0, el.scrollHeight - el.clientHeight);
        // 交还判据同样用手势内估计：外层接管期间真实 scrollTop 可能被惯性带着漂移
        const est = Math.max(0, Math.min(maxTop, this._nestEst));
        const innerCanReturn = ddy > 0 ? est > 0.5 : est < maxTop - 0.5;
        if (outerBlocked && innerCanReturn) {
          this._nestedScroll = el; // 交还内层（本帧不 preventDefault → 原生继续滚）
          this._nestEst = est;
          this._nestDir = 0;
          this._lastY = cur.clientY;
          this._lastT = now;
          return;
        }
      }
    }
    if (canPrevent) e.preventDefault();   // passive 版本不能调（浏览器会忽略并告警）
    const dt = Math.max(1, now - this._lastT);
    if (Math.abs(ddy) > 0.5) this._moved = true;
    this.y -= ddy;
    const inst = -ddy / dt;
    this.vel = this.vel * 0.6 + inst * 0.4;
    this._lastY = cur.clientY;
    this._lastT = now;
    this._velSamples.push({ t: now, y: cur.clientY });
    while (this._velSamples.length > 1 && now - this._velSamples[0].t > 120) {
      this._velSamples.shift();
    }
    this.applyY();
  }

  onTouchEnd(e) {
    this._horizontalGesture = false;
    this._directionLocked = false;
    gestureBus.horizontal = false;
    const touches = e.touches || [];
    if (touches.length > 0) { this._trackId = null; return; } // 还有手指按住，等全部抬起再启动惯性
    this._trackId = null;
    this._nestedScroll = null;
    this._nestedEl = null;
    this._nestDir = 0;               // 手势内累计估计只在本手势内有效
    this._usePassiveNested(false);   // 手势结束恢复非 passive 监听（外层惯性仍需 preventDefault）
    this.startPhysics();
  }

  // ---- 惯性（可调曲线） ----

  // 抬手初速估计：最近窗口平均 + 末尾瞬时速度，取绝对值最大者。
  // 对齐 Android VelocityTracker：突出手指抬起瞬间的速度，避免长窗平均把快速短甩抹平。
  _estimateReleaseVel() {
    const samples = this._velSamples;
    if (!samples || samples.length < 2) return;
    const last = samples[samples.length - 1];
    let best = 0;
    // 最近 releaseWindowMs 窗口平均
    let start = 0;
    for (let i = samples.length - 1; i >= 0; i--) {
      if (last.t - samples[i].t > FLING.releaseWindowMs) break;
      start = i;
    }
    const spanWin = last.t - samples[start].t;
    if (spanWin >= 8) {
      best = -(last.y - samples[start].y) / spanWin;
    }
    // 最后两个采样点瞬时速度
    const prev = samples[samples.length - 2];
    const spanInst = last.t - prev.t;
    if (spanInst >= 8) {
      const inst = -(last.y - prev.y) / spanInst;
      if (Math.abs(inst) > Math.abs(best)) best = inst;
    }
    if (Math.abs(best) > Math.abs(this.vel)) this.vel = best;
    this._velSamples = [];
  }

  startPhysics() {
    this.cancel();
    this._estimateReleaseVel();
    // 单次速度上限
    const maxV = FLING.maxVelocity;
    this.vel = Math.max(-maxV, Math.min(maxV, this.vel));
    // 连续同向甩动：速度叠加（模拟 Fling Booster）
    if (FLING.boost && this._prevFlingVel !== 0 && Math.sign(this._prevFlingVel) === Math.sign(this.vel)
        && Math.abs(this._prevFlingVel) >= FLING.boostMinSpeed && Math.abs(this.vel) >= FLING.boostMinSpeed
        && Date.now() - this._lastFlingT <= FLING.boostWindow) {
      this.vel += this._prevFlingVel;
      this.vel = Math.max(-maxV, Math.min(maxV, this.vel));
    }
    this._prevFlingVel = this.vel;
    this._lastFlingT = Date.now();

    // 拖拽/滚轮越界松手 → 以当前可见形变启动弹簧恢复（质量-弹簧-阻尼）
    if (this.y < this.minY) {
      const visual = this.dampShift(this.minY - this.y); // 以可见形变为初始压缩量，避免松手跳变
      this._startSpring(this.minY, -1, Math.abs(this.vel), visual);
      return;
    }
    if (this.y > this.maxY) {
      const visual = this.dampShift(this.y - this.maxY);
      this._startSpring(this.maxY, 1, Math.abs(this.vel), visual);
      return;
    }
    if (Math.abs(this.vel) < FLING.stopVelocity) {
      this.snapBack();
      return;
    }
    this.inDecel = true;
    this._lastT = Date.now();
    const tick = () => {
      if (!this.inDecel || this._destroyed) return;
      const now = Date.now();
      const dt = Math.max(1, now - this._lastT);
      this._lastT = now;
      // 幂律摩擦：v' = -k·|v|^p；进入低速段后摩擦再减弱，形成 1/x 式拖尾
      const speed = Math.abs(this.vel);
      let k = FLING.friction;
      if (speed < FLING.lowSpeedThreshold) k *= FLING.lowSpeedFactor;
      const decel = k * Math.pow(speed, FLING.power);
      this.vel -= Math.sign(this.vel) * decel * dt;
      this.y += this.vel * dt;
      // 惯性撞边界：剩余速度压入弹簧（质量-弹簧-阻尼），压缩后缓慢恢复
      if (this.y < this.minY) {
        this._startSpring(this.minY, -1, Math.abs(this.vel));
        return;
      }
      if (this.y > this.maxY) {
        this._startSpring(this.maxY, 1, Math.abs(this.vel));
        return;
      }
      if (Math.abs(this.vel) < FLING.stopVelocity) {
        this.inDecel = false;
        this.applyY();
        return;
      }
      this.applyY();
      this.tick = requestAnimationFrame(tick);
    };
    this.tick = requestAnimationFrame(tick);
  }

  // 边界弹簧：两段式。
  // ①压入（惯性撞击）：真实弹簧积分 x'' = -ω²x - 2ζωx'，初速 = 到达边界的速度，
  //   弹簧施加阻力连续减速到停（速度不跳变，不会“猛地缩进去”）；
  // ②恢复（撞击与拖拽松手共用）：不对称 S 曲线——短加速、长减速，平滑停止。
  _startSpring(target, dir, v0, x0) {
    this.cancel();
    this.inDecel = false;
    this.vel = 0;
    // 压缩深度上限 ≈ v0/ω，把初速限制在 rubberMax 对应的能量内
    const vCap = FLING.rubberMax * FLING.edgeSpringOmega;
    const initV = Math.max(0, Math.min(vCap, v0 || 0));
    const givenX = Math.max(0, Math.min(FLING.rubberMax, x0 || 0));
    if (givenX > 0) {
      // 拖拽松手：直接从当前形变按 S 曲线恢复（无压入段）
      this._spring = {
        target, dir, x: givenX, xMax: givenX, vx: 0, compressing: false,
        t0: Date.now(),
        durR: Math.min(640, Math.max(180, givenX * FLING.edgeRecoverMsPerPx)),
      };
    } else if (initV < 0.02) {
      this.y = target;
      this.applyY();
      return;
    } else {
      // 惯性撞击：物理弹簧压入，速度连续
      this._spring = {
        target, dir, x: 0, xMax: 0, vx: initV, compressing: true, t0: Date.now(), durR: 0,
      };
    }
    this.y = target + dir * this._spring.x; // 拖拽模式从当前形变起步，无跳变
    this._lastT = Date.now();
    this.tick = requestAnimationFrame(() => this._springTick());
  }

  _springTick() {
    if (!this._spring || this._spring.settle || this._destroyed) return;
    const s = this._spring;
    const now = Date.now();
    if (s.compressing) {
      const dt = Math.min(32, Math.max(1, now - s.t0));
      s.t0 = now;
      const w = FLING.edgeSpringOmega;
      const z = FLING.edgeSpringDamping;
      // 弹簧阻力：x'' = -ω²x - 2ζωx'，初速 = 到达边界的速度
      const a = -w * w * s.x - 2 * z * w * s.vx;
      s.vx += a * dt;
      s.x += s.vx * dt;
      if (s.x >= FLING.rubberMax) { s.x = FLING.rubberMax; s.vx = 0; }
      // 压到最深（速度反向）→ 进入 S 曲线恢复
      if (s.vx <= 0) {
        s.xMax = s.x;
        s.compressing = false;
        s.t0 = now;
        s.durR = Math.min(640, Math.max(180, s.xMax * FLING.edgeRecoverMsPerPx));
      }
    } else {
      // 目标实时钳制到当前边界：内容变短（收尾弹簧）期间 maxY 持续缩小，
      // 弹簧目标跟着走 → 单段平滑移动，不会停在旧目标上
      s.target = Math.max(this.minY, Math.min(s.target, this.maxY));
      const p = Math.min(1, (now - s.t0) / s.durR);
      // 不对称 S 曲线：前 edgeRecoverAccel 快速起速，后段长时间平滑减速到停
      const accel = FLING.edgeRecoverAccel;
      let eased;
      if (p < accel) {
        const u = p / accel;
        eased = accel * u * u * u; // 短加速（easeInCubic 接续）
      } else {
        const u = (p - accel) / (1 - accel);
        eased = accel + (1 - accel) * (1 - Math.pow(1 - u, 3)); // 长减速（easeOutCubic）
      }
      s.x = s.xMax * (1 - eased);
    }
    this.y = s.target + s.dir * s.x;
    this._applyRender(this.y); // 越界形变 1:1 渲染，不被拖拽阻尼压缩
    if (!s.compressing && (now - s.t0) >= s.durR) {
      this.y = Math.min(this.y, this.maxY); // 防御：动画期间 maxY 可能已更新
      this._spring = null;
      this.applyY();
      return;
    }
    this.tick = requestAnimationFrame(() => this._springTick());
  }

  // 内容变短（收起卡片等）：阻尼弹簧实时跟随 maxY 平滑收到新底部。
  // 弹簧对位置/速度积分（无离散跳变）→ 无论何时收起（静止/滚动中/弹回中）
  // 都不会闪现，也不会两段式移动；中间重测只更新 maxY，弹簧自行跟随。
  _settleShrink() {
    if (this._spring && this._spring.settle) return; // 已在收尾跟随
    this.cancel();
    this.inDecel = false;
    this.vel = 0;
    this._spring = { settle: true, vel: 0, lastT: Date.now() };
    this.tick = requestAnimationFrame(() => this._settleTick());
  }

  _settleTick() {
    if (!this._spring || !this._spring.settle || this._destroyed) return;
    const s = this._spring;
    const now = Date.now();
    const dt = Math.min(32, Math.max(1, now - s.lastT));
    s.lastT = now;
    const target = this.maxY; // 目标实时读取（内容持续变短 → 弹簧持续跟随）
    const w = FLING.settleOmega;
    const z = FLING.settleDamping;
    const a = -w * w * (this.y - target) - 2 * z * w * s.vel;
    s.vel += a * dt;
    this.y += s.vel * dt;
    // 渲染原始坐标（对齐小程序 settleTick / 弹回弹簧）：收尾期间 y 始终 > maxY，
    // 若走 applyY() 的 renderY 阻尼压缩，首帧会相对上一帧原始渲染瞬间跳变 → 闪现
    this._applyRender(this.y);
    if (Math.abs(this.y - target) < 0.5 && Math.abs(s.vel) < 0.02) {
      this.y = target;
      this._spring = null;
      this._applyRender(this.y);
      return;
    }
    this.tick = requestAnimationFrame(() => this._settleTick());
  }

  snapBack() {
    this.cancel();
    const tick = () => {
      if (this._destroyed) return;
      const target = this.y < this.minY ? this.minY : (this.y > this.maxY ? this.maxY : this.y);
      if (target === this.y) { this.applyY(); this.tick = null; return; }
      this.y += (target - this.y) * FLING.snapSpd;
      if (Math.abs(this.y - target) < 0.3) { this.y = target; this.applyY(); this.tick = null; return; }
      this.applyY();
      this.tick = requestAnimationFrame(tick);
    };
    this.tick = requestAnimationFrame(tick);
  }

  // ---- 渲染 ----

  dampShift(d) {
    const max = FLING.dampMax;
    const sign = d >= 0 ? 1 : -1;
    return sign * max * (1 - Math.exp(-Math.abs(d) / (max * 1.6)));
  }

  renderY() {
    const y = this.y;
    if (y < this.minY) return this.minY - this.dampShift(this.minY - y);
    if (y > this.maxY) return this.maxY + this.dampShift(y - this.maxY);
    return y;
  }

  _applyRender(rawY) {
    const real = Math.max(0, Math.min(rawY, this.maxY));
    const ratio = this.maxY > 0 ? Math.min(real / 400, 1) : 0;
    // translate3d 走 GPU 合成层；will-change 由 .js-scroll .scroll-content 提供
    this.content.style.transform = 'translate3d(0,' + (-rawY) + 'px,0)';
    if (typeof this.opts.onScroll === 'function') this.opts.onScroll(real, ratio);
  }

  applyY() {
    this._applyRender(this.renderY());
  }

  // ---- 桌面滚轮：1:1 直滚 + 边界回弹 ----

  onWheel(e) {
    const nested = e.target && e.target.closest ? e.target.closest('.file-list-scroll, .wheel-viewport, .detail-files-scroll') : null;
    if (nested) {
      const maxTop = nested.scrollHeight - nested.clientHeight;
      const atBottom = nested.scrollTop >= maxTop - 0.5;
      const atTop = nested.scrollTop <= 0.5;
      if ((e.deltaY > 0 && !atBottom) || (e.deltaY < 0 && !atTop)) return;
    }
    if (e.ctrlKey) return; // 保留 Ctrl+滚轮缩放
    e.preventDefault();
    const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY; // Firefox 行模式归一化
    this.cancel();
    this.inDecel = false;
    this.y += dy;
    this.applyY();
    // 停止滚动后平滑回弹（越界时）
    if (this._wheelTimer) clearTimeout(this._wheelTimer);
    this._wheelTimer = setTimeout(() => this.startPhysics(), 140);
  }

  scrollTo(targetY, duration) {
    this.cancel();
    this.inDecel = false;
    const target = Math.max(this.minY, Math.min(targetY, this.maxY));
    if (duration === 0) { this.y = target; this.applyY(); return; } // 0 = 直接定位
    const startY = this.y;
    const diff = target - startY;
    if (Math.abs(diff) < 1) { this.y = target; this.applyY(); return; }
    const startT = Date.now();
    const tick = () => {
      const p = Math.min(1, (Date.now() - startT) / (duration || 280));
      const eased = 1 - Math.pow(1 - p, 3);
      this.y = startY + diff * eased;
      this.applyY();
      if (p < 1) this.tick = requestAnimationFrame(tick);
      else this.tick = null;
    };
    this.tick = requestAnimationFrame(tick);
  }
}

function initScrollEngines() {
  const printScroller = document.getElementById('scroller-print');
  const printContent = document.getElementById('scrollContentPrint');
  if (printScroller && printContent && !scrollEngines.print) {
    scrollEngines.print = new FlingEngine(printScroller, printContent, {
      onScroll(real, ratio) {
        const logoWrap = document.getElementById('logoWrap');
        const headerArea = document.getElementById('headerArea');
        if (logoWrap) logoWrap.style.transform = 'scale(' + (1 - ratio * 0.7).toFixed(3) + ')';
        if (headerArea) {
          // 对齐小程序 logoPadding（40→8 rpx），rpx 按 750 设计稿换算为 cqw
          const padRpx = Math.round(40 - ratio * 32);
          const padCqw = (padRpx * 100 / 750).toFixed(4);
          headerArea.style.paddingTop = padCqw + 'cqw';
          headerArea.style.paddingBottom = padCqw + 'cqw';
        }
      },
    });
    // 初始化头部基线样式（对齐小程序首帧即 40rpx）：首次滚动前就位，
    // 避免 inline padding 从 CSS 默认值跳到 40rpx，导致头部下方内容整体下移
    const headerArea = document.getElementById('headerArea');
    if (headerArea) {
      const initCqw = (40 * 100 / 750).toFixed(4);
      headerArea.style.paddingTop = initCqw + 'cqw';
      headerArea.style.paddingBottom = initCqw + 'cqw';
    }
    const logoWrap = document.getElementById('logoWrap');
    if (logoWrap) logoWrap.style.transform = 'scale(1)';
  }
  const meScroller = document.getElementById('scroller-me');
  const meContent = document.getElementById('scrollContentMe');
  if (meScroller && meContent && !scrollEngines.me) {
    scrollEngines.me = new FlingEngine(meScroller, meContent, {
      onScroll(real) {
        const btn = document.getElementById('scrollTopBtn');
        if (btn) btn.classList.toggle('scroll-top-visible', real > 200);
        // 滚动即收起分页下拉（对齐小程序：避免 fixed 下拉错位）
        if (typeof closePageSizePicker === 'function') closePageSizePicker();
      },
    });
  }
  // 子视图（历史授权用户 / 账号绑定 / 我的任务）统一走同一套滚动引擎
  const overlayScrollers = [
    ['authorized', 'scroller-authorized', 'scrollContentAuthorized'],
    ['bind', 'scroller-bind', 'scrollContentBind'],
    ['userOrders', 'scroller-user-orders', 'scrollContentUserOrders'],
    ['recentOrders', 'scroller-recent-orders', 'scrollContentRecentOrders'],
  ];
  overlayScrollers.forEach(([key, scId, ctId]) => {
    const sc = document.getElementById(scId);
    const ct = document.getElementById(ctId);
    if (sc && ct && !scrollEngines[key]) {
      const opts = {};
      if (key === 'recentOrders') {
        // 滚动即收起每页条数下拉（fixed 下拉的定位是按下拉打开那一刻算的，滚动后必然错位）
        opts.onScroll = () => { if (typeof closeRoPageSizePicker === 'function') closeRoPageSizePicker(); };
      }
      if (key === 'userOrders') {
        // 滚动即收起每页条数下拉（对齐"我的打印任务"的 closePageSizePicker，避免 fixed 下拉错位）
        opts.onScroll = () => { if (typeof closeUoPageSizePicker === 'function') closeUoPageSizePicker(); };
      }
      scrollEngines[key] = new FlingEngine(sc, ct, opts);
    }
  });
}

function measureAll(delay) {
  Object.keys(scrollEngines).forEach(k => {
    const engine = scrollEngines[k];
    // 页面隐藏（display:none）时不测量：隐藏态高度为 0 会把滚动位置 clamp 掉
    if (engine.el && engine.el.offsetParent === null) return;
    engine.scheduleMeasure(delay);
  });
}

/* ================= Tab / 视图栈 / 页面转场 ================= */

// 交叉转场用：导航栏实测高度（入场页绝对定位时从导航栏下方开始，避免盖住导航栏）
function navBarHeight() {
  const el = document.getElementById('navBar');
  return (el && el.offsetHeight) || 48;
}

let currentTab = 'print';
let _tabSwitching = false;
let _tabPending = '';        // 正在切去的 tab（转场还没落定时；currentTab 要等落定才更新）
// 页面转场（2026-11-16 第七轮，用户口径「动画播到一半反向切 tab，动画要立刻反方向播」）：
// 两页位置由一个进度 _pageQ 驱动，rAF 逐帧写 transform —— 目标一变就从当前位置调头，
// 不再有「等这一趟播完再播反向」的排队（旧的 _tabQueuedTab 已删除）。
let _pageQ = 0;              // 0 = 打印页在前，1 = 我页在前
let _pageRaf = 0;            // 正在跑的 rAF 句柄（0 = 没在播）
let _viewTransition = false;

/* ── 玻璃滑块（APP 2026-11-16）─────────────────────────────────────────
   #tabThumb 只做位移；#tabPill 是一块硬质玻璃 —— 不缩放、不拉伸（2026-11-16 第五轮，
   用户口径「滑块不需要有弹性，要硬质玻璃的感觉」）。
   两层必须是两个元素：合到一个元素上时位移与缩放两条插值互相拉扯，滑块会被
   拉成畸形（demo 的硬结论）。拖动见下面「拖动 / 按下反馈」：手势只绑 #tabBar，
   页面里的横滑手势挂在滚动内容上，两者不抢；转场进行中也能起手（2026-11-16 第三轮）。 */
let _tabWaveTimer = 0;

// 格子序号：0 = 打印，1 = 我（找不到就退回 0，别让一个笔误把滑块丢到栏外）
function _tabIndex(tab) {
  const items = [...document.querySelectorAll('.tab-item')];
  const i = items.findIndex(t => t.dataset.tab === tab);
  return i < 0 ? 0 : i;
}

function currentTabThumbIndex() {
  const thumb = document.getElementById('tabThumb');
  return thumb ? (parseInt(thumb.getAttribute('data-i'), 10) || 0) : 0;
}

// 位移层：只改 --tab-i，过渡交给 CSS；跨格时让形变层拉一下
function slideTabThumb(index) {
  const thumb = document.getElementById('tabThumb');
  if (!thumb) return;
  const from = currentTabThumbIndex();
  thumb.setAttribute('data-i', String(index));
  thumb.style.setProperty('--tab-i', String(index));
}

// 整排起伏：逐格错开 26ms，点任意 tab 都放一次
function _waveTabBar() {
  const bar = document.getElementById('tabBar');
  if (!bar) return;
  const items = [...bar.querySelectorAll('.tab-item')];
  items.forEach((t, k) => { t.style.animationDelay = (k * 26) + 'ms'; });
  bar.classList.remove('wave');
  void bar.offsetWidth;
  bar.classList.add('wave');
  clearTimeout(_tabWaveTimer);
  _tabWaveTimer = setTimeout(() => {
    bar.classList.remove('wave');
    items.forEach(t => { t.style.animationDelay = ''; });
  }, 640);
}

// 图标弹跳：滑块先到位、图标再落（跨格延迟 170ms，同格点按立即弹）
function _bumpTabIcon(item, delay) {
  const wrap = item && item.querySelector('.tab-icon-wrap');
  if (!wrap) return;
  setTimeout(() => {
    wrap.classList.remove('bump');
    void wrap.offsetWidth;
    wrap.classList.add('bump');
    setTimeout(() => wrap.classList.remove('bump'), 580);
  }, delay || 0);
}

// 首帧定位：不做过渡直接落位（否则启动时药丸会从左往右滑一遍）
function initTabThumb() {
  const thumb = document.getElementById('tabThumb');
  if (!thumb) return;
  const i = _tabIndex(currentTab);
  thumb.style.transition = 'none';
  thumb.setAttribute('data-i', String(i));
  thumb.style.setProperty('--tab-i', String(i));
  void thumb.offsetWidth;
  thumb.style.transition = '';
}

/* ── 拖动 / 按下反馈（APP 2026-11-16 第二轮）──────────────────────────────
   只在 #tabBar 自己的命中面上起手：页面内容是它的兄弟节点，拖栏不会碰到页面里
   那套横滑手势（gestureBus 挂在滚动内容上）。
   · 位移层跟手：.dragging 里 transition:none，直接写分数 --tab-i（CSS 按
     calc((100% + 2.4cqw) * var(--tab-i)) 落位，分数就是「格与格之间」）
   · 形变层沿行程 scaleX，origin 落在来向一侧 —— 才像被手指拽着走
   · 整排 tab 按 TAB_ROW_FOLLOW 跟随，越界按 TAB_RUBBER 阻尼（弹性偏移）
   · 松手：过半格 → switchTab（复用 320ms 归位 + goo + wave + bump）
           未过半格 → 写回原格，320ms 自己弹回去
   · 点按：按下压 0.92，松手船式回弹；同格点击才放 rebound，跨格交给 wave，
           两套 transform 动画不同时挂在一个格子上（会互相顶掉） */
const TAB_RUBBER = 0.35;       // 越界阻尼：拖出栏体后的行程只剩 35%
const TAB_BAR_FOLLOW = 0.12;   // 整条栏跟随比例（拖 60px 约走 7px）
const TAB_BAR_OVER = 0.35;     // 越界那段整条栏跟得更明显（橡皮筋要有「拉得住」的手感）
const TAB_BAR_MAX = 10;        // 整条栏最大位移（2026-11-16 用户口径：10px 就够了）
const TAB_PILL_OVER = 0.028;   // 药丸越界最多再走 0.028 格（≈4px，正好把栏体左右那 1.2cqw 内缩走完）
                               // —— 再多药丸就探出玻璃栏的圆角轮廓了（截图实测）
let _tabDrag = null;
let _tabSuppressClickUntil = 0;

// 船式起伏（2026-11-16 第六轮，用户原话「整个 tab 受到点击或滑动时，都会有微微缩小和放大的
// 弹簧效果，参考个人网站荣誉证书的点击缩小放大动画」）：按住时整条栏下沉，松手弹回并过冲。
// 这一层只管**整条栏**；格子自己的 .press / .rebound（图标）保持原样，两层叠起来才像一块
// 被按在桌面上的硬玻璃。scale 走 CSS 独立属性，和 --tab-bar-dx 的 transform 互不覆盖。
let _tabBoatTimer = 0;
function _tabBoatDown(bar) {
  if (!bar) return;
  clearTimeout(_tabBoatTimer);
  bar.classList.remove('boat-up');
  bar.classList.add('boat');
}
function _tabBoatUp(bar) {
  if (!bar || !bar.classList.contains('boat')) return;
  bar.classList.remove('boat');
  bar.classList.add('boat-up');
  clearTimeout(_tabBoatTimer);
  _tabBoatTimer = setTimeout(() => bar.classList.remove('boat-up'), 620);
}

function _tabStep(bar) {
  const items = [...bar.querySelectorAll('.tab-item')];
  if (items.length < 2) return 0;
  const r0 = items[0].getBoundingClientRect();
  const r1 = items[1].getBoundingClientRect();
  return r1.left - r0.left;   // 一格步长 = 栏宽 / 格数（栏宽变化自动跟着走）
}

// 滑块此刻的「视觉」格序号：转场途中它正被 CSS 过渡带着走，data-i 已经指向终点，
// 拿 data-i 当锚点会看到药丸瞬间跳过去。读 transform 里的 tx 才是手指底下的真实位置。
function _tabVisualIndex(thumb, step, max) {
  if (!thumb || !step) return 0;
  const m = getComputedStyle(thumb).transform;
  let tx = 0;
  if (m && m !== 'none') {
    const nums = m.slice(m.indexOf('(') + 1).replace(')', '').split(',');
    tx = parseFloat(nums[4]) || 0;
  }
  const i = tx / step;
  return Math.max(0, Math.min(max, isNaN(i) ? 0 : i));
}

function _tabDragStart(e) {
  const bar = document.getElementById('tabBar');
  if (!bar || _tabDrag) return;
  if (e.pointerType === 'mouse' && e.button !== 0) return;
  const step = _tabStep(bar);
  if (!step) return;
  bar.classList.remove('settling');   // 上一次松手的回弹还没收完就接下一次拖
  _tabBoatDown(bar);                  // 按下去整条栏先沉一下（随后才可能是拖动）
  // 关键：转场（switchTab 的 480ms 平铺推拉、或滑块的 320ms 归位）途中也允许起手 ——
  // 直接把药丸冻在当前视觉位置，接着跟手；页面转场该怎么走还怎么走，松手时再按位置决定
  const thumb = document.getElementById('tabThumb');
  const maxSlot = bar.querySelectorAll('.tab-item').length - 1;
  const i0 = _tabVisualIndex(thumb, step, maxSlot);
  if (thumb) {
    thumb.style.transition = 'none';
    thumb.style.setProperty('--tab-i', String(i0));
  }
  const item = e.target && e.target.closest ? e.target.closest('.tab-item') : null;
  if (item) {
    item.classList.remove('rebound');
    item.classList.add('press');
  }
  _tabDrag = {
    id: e.pointerId, x0: e.clientX, y0: e.clientY, i0: i0,
    step: step, max: maxSlot, item: item, active: false,
  };
}

function _tabDragMove(e) {
  const d = _tabDrag;
  if (!d || e.pointerId !== d.id) return;
  const bar = document.getElementById('tabBar');
  if (!bar) return;
  const dx = e.clientX - d.x0;
  const dy = e.clientY - d.y0;
  if (!d.active) {
    if (Math.abs(dx) < 4 && Math.abs(dy) < 4) return;
    // 竖向意图让位：栏体上本来没有滚动，但方向锁定要和页面手势同一口径
    if (Math.abs(dy) > Math.abs(dx)) { _tabDragEnd(e, true); return; }
    d.active = true;
    bar.classList.add('dragging');
    if (d.item) d.item.classList.remove('press');
    // 拖动真正开始后才抓指针：点在格子上时就抓会让 click 落到栏体上，普通点按就失效了
    try { bar.setPointerCapture(d.id); } catch (err) { /* 老 WebView 不支持就算了 */ }
  }
  const raw = d.i0 + dx / d.step;
  let damped = raw;
  if (raw < 0) damped = raw * TAB_RUBBER;
  else if (raw > d.max) damped = d.max + (raw - d.max) * TAB_RUBBER;
  // 整条栏位移：普通行程按 12%，越界那段按 35%，整体夹在 ±TAB_BAR_MAX（用户口径 10px 足够）。
  // 方向和手指一致；不拿 raw 的越界余量反推，否则往左拖会把栏往右推。
  const travelPx = (damped - d.i0) * d.step;
  const inRangePx = (Math.max(0, Math.min(d.max, damped)) - d.i0) * d.step;
  const overPx = travelPx - inRangePx;
  const barDx = Math.max(-TAB_BAR_MAX, Math.min(TAB_BAR_MAX,
    inRangePx * TAB_BAR_FOLLOW + overPx * TAB_BAR_OVER));
  bar.style.setProperty('--tab-bar-dx', barDx.toFixed(2) + 'px');
  // 位移层：栏自己挪了 barDx，药丸内部行程要扣掉这一段，屏幕上才仍然停在手指底下；
  // 越界时把药丸挡在玻璃栏内（栏体左右各留 1.2cqw 内缩，正好够走这一小段），
  // 剩下的「拉不住」全部由整条栏的位移表达 —— 药丸探出圆角轮廓像画坏了
  const fracRaw = (travelPx - barDx) / d.step + d.i0;
  const frac = Math.max(-TAB_PILL_OVER, Math.min(d.max + TAB_PILL_OVER, fracRaw));
  const thumb = document.getElementById('tabThumb');
  thumb.style.setProperty('--tab-i', String(frac));
}

function _tabDragEnd(ev, silent) {
  const d = _tabDrag;
  if (!d) return;
  _tabDrag = null;
  const bar = document.getElementById('tabBar');
  if (!bar) return;
  _tabBoatUp(bar);                    // 松手：整条栏弹簧回弹（点按与拖动同一口径）
  try { bar.releasePointerCapture(d.id); } catch (err) { /* 没抓住过就算了 */ }
  if (d.item) d.item.classList.remove('press');
  const thumb = document.getElementById('tabThumb');
  const items = [...bar.querySelectorAll('.tab-item')];
  // 页面此刻认定的格：转场途中认 _tabPending（currentTab 要等落定才更新）
  const destIdx = _tabIndex(_tabPending || currentTab);
  const restoreThumb = () => { if (thumb) thumb.style.transition = ''; };
  if (!d.active) {
    // 纯点按：药丸回到页面认定的那一格（转场途中冻过，这里放回去接着过渡）
    if (thumb) thumb.style.setProperty('--tab-i', String(destIdx));
    restoreThumb();
    if (!silent && d.item && d.item.dataset.tab === currentTab) {
      d.item.classList.remove('rebound');
      void d.item.offsetWidth;
      d.item.classList.add('rebound');
      setTimeout(() => d.item.classList.remove('rebound'), 460);
    }
    return;
  }
  const frac = parseFloat(thumb.style.getPropertyValue('--tab-i'));
  const target = Math.max(0, Math.min(d.max, Math.round(isNaN(frac) ? d.i0 : frac)));
  bar.classList.remove('dragging');
  bar.classList.add('settling');
  setTimeout(() => bar.classList.remove('settling'), 360);
  bar.style.removeProperty('--tab-bar-dx');
  restoreThumb();   // 药丸重新拿回 CSS 过渡：从松手位置滑向目标格
  const targetTab = items[target] ? items[target].dataset.tab : '';
  if (!targetTab || target === destIdx) {
    thumb.style.setProperty('--tab-i', String(destIdx));   // 没过半格 / 本来就在这格：滑回这一格
    return;
  }
  _tabSuppressClickUntil = Date.now() + 400;
  // 页面正在播上一趟转场也照样立刻接管：switchTab 会从当前进度就地反向，不再排到落定之后
  thumb.style.setProperty('--tab-i', String(target));
  switchTab(targetTab);
}

function initTabDrag() {
  const bar = document.getElementById('tabBar');
  if (!bar || bar.dataset.dragBound) return;
  bar.dataset.dragBound = '1';
  bar.addEventListener('pointerdown', _tabDragStart);
  bar.addEventListener('pointermove', _tabDragMove);
  bar.addEventListener('pointerup', _tabDragEnd);
  bar.addEventListener('pointercancel', (e) => _tabDragEnd(e, true));   // 被系统打断：直接收摊，别弹回弹
}

// tab 切换：平铺推拉转场 —— 两页并排铺满内容区，旧页抽出、新页从对面推入，互不重叠。
// 2026-11-16 第七轮（用户口径「动画播到一半反向切 tab，动画应当立刻反方向播，而不是等这一趟
// 播完再播反向」）：转场不再是一条 CSS 关键帧动画 + setTimeout，而是由连续进度 _pageQ
// 驱动的 rAF 逐帧位移 —— 任何时刻再点名，都从当前位置直接调头（见 _pageSlideTo）。
function _pageEase(t) {
  // cubic-bezier(0.16, 1, 0.3, 1)：与原 CSS 动画同一条强 Ease-Out（先快后慢），保持手感一致
  if (t <= 0) return 0;
  if (t >= 1) return 1;
  const x1 = 0.16, y1 = 1, x2 = 0.3, y2 = 1;
  const cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx;
  const cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
  const sx = (u) => ((ax * u + bx) * u + cx) * u;
  const sy = (u) => ((ay * u + by) * u + cy) * u;
  const dx = (u) => (3 * ax * u + 2 * bx) * u + cx;
  let u = t;
  for (let k = 0; k < 6; k++) {
    const err = sx(u) - t;
    if (Math.abs(err) < 1e-5) break;
    const d = dx(u);
    if (Math.abs(d) < 1e-5) break;
    u -= err / d;
  }
  u = Math.max(0, Math.min(1, u));
  return sy(u);
}

// 两页的位置只由 _pageQ 决定：q=0 打印页贴左、我页在右外侧；q=1 我页贴左、打印页在左外侧。
// 只写 transform/opacity，不碰 transition —— 逐帧位移必须是即时的。
function _pagePaint(q) {
  const printPage = document.getElementById('page-print');
  const mePage = document.getElementById('page-me');
  if (!printPage || !mePage) return;
  printPage.style.transform = 'translate3d(' + (-q * 100).toFixed(3) + '%,0,0)';
  mePage.style.transform = 'translate3d(' + ((1 - q) * 100).toFixed(3) + '%,0,0)';
  printPage.style.opacity = (1 - q).toFixed(3);
  mePage.style.opacity = q.toFixed(3);
}

function _pageSettle(tab) {
  _pageRaf = 0;
  _pageQ = tab === 'me' ? 1 : 0;
  const printPage = document.getElementById('page-print');
  const mePage = document.getElementById('page-me');
  [printPage, mePage].forEach(p => {
    if (!p) return;
    p.classList.remove('page-push');
    p.style.transition = '';
    p.style.transform = '';
    p.style.opacity = '';
    p.style.top = '';
  });
  if (printPage) printPage.style.display = tab === 'me' ? 'none' : '';
  if (mePage) mePage.style.display = tab === 'me' ? '' : 'none';
  hideViews();
  currentTab = tab;
  _tabPending = '';
  _tabSwitching = false;
  updateThemeToggleVisibility();
  if (tab === 'me' && typeof loadMeTab === 'function') loadMeTab();
  if (tab === 'print' && typeof onPrintTabShown === 'function') onPrintTabShown();
  measureAll();
}

// 从当前 _pageQ 滑向 targetQ。途中再调一次 = 取消旧 rAF、按当前位置重新起跑，
// 时长按剩余距离算（观感与一整趟同速）—— 这就是「反向立刻播」的全部实现。
function _pageSlideTo(targetQ, tab) {
  const printPage = document.getElementById('page-print');
  const mePage = document.getElementById('page-me');
  if (!printPage || !mePage) return;
  const navH = navBarHeight() + 'px';
  [printPage, mePage].forEach(p => {
    p.style.display = '';
    p.classList.remove('page-exit-left', 'page-exit-right', 'page-fade-in', 'page-enter-right', 'page-enter-left');
    p.classList.add('page-push');
    p.style.top = navH;
    p.style.transition = 'none';
  });
  if (_pageRaf) { cancelAnimationFrame(_pageRaf); _pageRaf = 0; }
  const from = _pageQ;
  const dist = Math.abs(targetQ - from);
  if (dist < 0.002) { _pageSettle(tab); return; }
  const dur = Math.max(140, 480 * dist);
  const t0 = performance.now();
  const step = (now) => {
    const p = Math.min(1, (now - t0) / dur);
    _pageQ = from + (targetQ - from) * _pageEase(p);
    _pagePaint(_pageQ);
    if (p < 1) { _pageRaf = requestAnimationFrame(step); return; }
    _pageSettle(tab);
  };
  _pageRaf = requestAnimationFrame(step);
}

function switchTab(tab) {
  // 已经在去这一格的路上：重复点名不重启动画
  if (_tabSwitching && tab === _tabPending) return;
  // 就停在这一页、也没在播转场：什么都不用做
  if (!_tabSwitching && tab === currentTab) return;
  _tabSwitching = true;
  _tabPending = tab;

  // 切换前关闭打开的弹窗（对齐小程序 animateExit 中的处理）
  document.querySelectorAll('.modal-mask').forEach(m => {
    if (m.style.display !== 'none' && m.id) closeModal(m.id);
  });
  // 每页条数下拉在 page-root 外 fixed，切页时一并收起
  if (typeof closePageSizePicker === 'function') closePageSizePicker();
  // 「合计价格复制」气泡同理（tab 栏 z-index 高于气泡遮罩，不关会跟到另一页）
  if (typeof closePricePop === 'function') closePricePop();
  if (typeof closeStatusReasonPop === 'function') closeStatusReasonPop();
  if (typeof closeRoPageSizePickerIfOpen === 'function') closeRoPageSizePickerIfOpen();

  // tab 栏立即更新选中态：玻璃滑块位移 + 整排起伏 + 图标弹跳
  //（旧的 .tab-hover 缩放已由 .tab-icon-wrap.bump 取代，别再加回来 —— 两套缩放会打架）
  const _prevThumb = currentTabThumbIndex();
  const _nextThumb = _tabIndex(tab);
  slideTabThumb(_nextThumb);
  document.querySelectorAll('.tab-item').forEach(t => {
    const on = t.dataset.tab === tab;
    t.classList.toggle('active', on);
    const iconWrap = t.querySelector('.tab-icon-wrap');
    if (iconWrap) iconWrap.classList.toggle('icon-active', on);
    const txt = t.querySelector('.tab-text');
    if (txt) txt.classList.toggle('text-active', on);
  });
  _bumpTabIcon(document.querySelector('.tab-item[data-tab="' + tab + '"]'),
               _prevThumb !== _nextThumb ? 170 : 0);
  _waveTabBar();

  // 标题立即切换（交叉过渡期间导航栏属于全局 UI，不随页面动画延迟）
  document.getElementById('navTitle').textContent = tab === 'print' ? '提交打印' : '我';
  document.getElementById('navBack').style.display = 'none';
  document.getElementById('tabBar').style.display = '';

  // 主题按钮随 tab 切换同步播放滑入/滑出动画（与页面转场同时开始，按目标页判定）
  updateThemeToggleVisibility(tab);

  // 页面本身：从当前位置滑向 tab 对应的位置（转场途中再点名 = 就地反向，不排队）
  _pageSlideTo(tab === 'me' ? 1 : 0, tab);
}

function switchToMe() {
  const el = document.querySelector('.tab-item[data-tab="me"]');
  if (el) el.click();
}

function hideViews() {
  document.querySelectorAll('.view').forEach(v => { v.style.display = 'none'; });
}

function showView(id, title) {
  if (_viewTransition) return;
  _viewTransition = true;
  document.getElementById('page-print').style.display = 'none';
  if (typeof closePageSizePicker === 'function') closePageSizePicker();
  document.getElementById('tabBar').style.display = 'none';
  document.getElementById('navBack').style.display = '';
  document.getElementById('navTitle').textContent = title || '';
  // 平铺推拉转场：子视图与 me 页并排铺满内容区，me 页向左抽出、子视图从右推入，互不重叠
  hideViews();  // 先清掉可能残留的其他子视图
  const navH = navBarHeight() + 'px';
  const mePage = document.getElementById('page-me');
  const v = document.getElementById(id);
  if (v) {
    v.style.display = '';
    v.classList.remove('page-exit-left', 'page-exit-right', 'page-fade-in', 'page-enter-right', 'page-enter-left', 'page-push');
    v.style.top = navH;
    void v.offsetWidth;
    v.classList.add('page-push', 'page-enter-right');
  }
  mePage.classList.remove('page-exit-left', 'page-exit-right', 'page-fade-in', 'page-enter-right', 'page-enter-left', 'page-push');
  mePage.style.top = navH;
  void mePage.offsetWidth;
  mePage.classList.add('page-push', 'page-exit-left');
  setTimeout(() => {
    mePage.style.display = 'none';
    mePage.classList.remove('page-push', 'page-exit-left');
    mePage.style.top = '';
    if (v) { v.classList.remove('page-push', 'page-enter-right'); v.style.top = ''; }
    updateThemeToggleVisibility();
    measureAll();
    _viewTransition = false;
  }, 480);
}

function hideView() {
  if (_viewTransition) return;
  _viewTransition = true;
  if (typeof closePageSizePicker === 'function') closePageSizePicker();
  document.getElementById('page-print').style.display = 'none';
  // 平铺推拉转场：me 页与子视图并排铺满内容区，子视图向右抽出、me 页从左推入，互不重叠
  const currentView = [...document.querySelectorAll('.view')].find(v => v.style.display !== 'none');
  const mePage = document.getElementById('page-me');
  const finish = () => {
    if (currentView) currentView.style.display = 'none';
    if (currentView) { currentView.classList.remove('page-push', 'page-exit-right'); currentView.style.top = ''; }
    mePage.classList.remove('page-push', 'page-enter-left');
    mePage.style.top = '';
    document.getElementById('navTitle').textContent = '我';
    document.getElementById('navBack').style.display = 'none';
    document.getElementById('tabBar').style.display = '';
    currentTab = 'me';
    _tabSwitching = false;
    // 子视图返回「我」页是另一套 CSS 关键帧转场：这里把进度状态对齐到「我页在前」
    if (_pageRaf) { cancelAnimationFrame(_pageRaf); _pageRaf = 0; }
    _pageQ = 1;
    _tabPending = '';
    updateThemeToggleVisibility();
    if (typeof loadMeTab === 'function') loadMeTab();
    measureAll();
    _viewTransition = false;
  };
  if (currentView) {
    // 标题立即切换（导航栏属于全局 UI）
    document.getElementById('navTitle').textContent = '我';
    const navH = navBarHeight() + 'px';
    mePage.style.display = '';
    mePage.classList.remove('page-exit-left', 'page-exit-right', 'page-fade-in', 'page-enter-right', 'page-enter-left', 'page-push');
    mePage.style.top = navH;
    void mePage.offsetWidth;
    mePage.classList.add('page-push', 'page-enter-left');
    currentView.classList.remove('page-exit-left', 'page-exit-right', 'page-fade-in', 'page-enter-right', 'page-enter-left', 'page-push');
    currentView.style.top = navH;
    void currentView.offsetWidth;
    currentView.classList.add('page-push', 'page-exit-right');
    setTimeout(finish, 480);
  } else {
    // 防御分支：无可见子视图，直接恢复 me 页（无动画）
    document.getElementById('navTitle').textContent = '我';
    mePage.style.display = '';
    mePage.classList.remove('page-exit-left', 'page-exit-right', 'page-fade-in', 'page-enter-right', 'page-enter-left', 'page-push');
    mePage.style.top = '';
    finish();
  }
}

// Android 物理返回键（由 MainActivity.onBackPressed 经 evaluateJavascript 调用）：
// 有弹窗 → 关弹窗；有子视图 → 返回"我"页；否则返回 false 由原生退出 App
window.hnHandleBack = function () {
  const visibleModal = [...document.querySelectorAll('.modal-mask')].find(m => m.style.display !== 'none');
  if (visibleModal && visibleModal.id) {
    if (visibleModal.id === 'confirmModal') _confirmCallback = null;
    closeModal(visibleModal.id);
    return true;
  }
  if (typeof closePageSizePicker === 'function') closePageSizePicker();
  // 价格气泡 / 异常原因气泡是自绘的 fixed 层（不是 .modal-mask），返回键要先收掉它们
  if (document.querySelector('.status-pop-mask')) {
    if (typeof closeStatusReasonPop === 'function') closeStatusReasonPop();
    return true;
  }
  if (document.querySelector('.price-pop-mask')) {
    if (typeof closePricePop === 'function') closePricePop();
    return true;
  }
  const subVisible = [...document.querySelectorAll('.view')].some(v => v.style.display !== 'none');
  if (subVisible) {
    hideView();
    return true;
  }
  return false;
};

// 主题切换按钮仅"我"页使用（打印页/子视图隐藏，对齐小程序）。
// 显隐带过渡动画：显示从右向左滑入淡入（theme-show），隐藏向右滑出淡出（theme-hide），
// 动画结束后才真正 display:none（避免直接消失）。
// targetTab 参数：tab 切换转场开始时即按目标页判定（与页面动画同步播放），
// 不传则按当前页面 display 状态判定（子视图等场景）。
function updateThemeToggleVisibility(targetTab) {
  const toggle = document.getElementById('themeToggle');
  if (!toggle) return;
  let shouldShow;
  if (targetTab) {
    shouldShow = targetTab === 'me';
  } else {
    const printVisible = document.getElementById('page-print').style.display !== 'none';
    const subVisible = [...document.querySelectorAll('.view')].some(v => v.style.display !== 'none');
    shouldShow = !(printVisible || subVisible);
  }
  if (shouldShow) {
    if (!toggle._themeShown) {
      toggle._themeShown = true;
      toggle.classList.remove('theme-hide');
      toggle.style.display = '';
      void toggle.offsetWidth;   // 重启动画
      toggle.classList.add('theme-show');
      // 显示时按最新导航栏高度重新定位（安全区高度可能延迟到达，避免被导航栏盖住）
      if (typeof positionThemeToggle === 'function') positionThemeToggle();
    }
  } else {
    if (toggle._themeShown) {
      toggle._themeShown = false;
      toggle.classList.remove('theme-show');
      void toggle.offsetWidth;
      toggle.classList.add('theme-hide');
      const el = toggle;
      setTimeout(() => { if (!el._themeShown) el.style.display = 'none'; }, 300);
    }
  }
}

/* ================= Toast / Modal ================= */

let _toastTimer = null;
function showToast(msg, duration) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), duration || 2000);
}

function openModal(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove('modal-closing');
  el.style.display = 'flex';
}

function closeModal(id) {
  const el = document.getElementById(id);
  if (!el) return;
  // 成功弹窗关闭时清空文件列表（对齐小程序 onCloseModal：收起列表→关闭弹窗→清空数据）
  if (id === 'successModal' && typeof clearFilesAfterSuccess === 'function') clearFilesAfterSuccess();
  // 文件预览关闭时回收 objectURL / 清空内容（点遮罩与点 ✕ 都走这里）
  if (id === 'previewMask' && typeof closePreviewBody === 'function') closePreviewBody();
  el.classList.add('modal-closing');
  setTimeout(() => {
    el.style.display = 'none';
    el.classList.remove('modal-closing');
  }, 200);
}

let _confirmCallback = null;
function showConfirm(title, content, confirmText, confirmColor, cb) {
  document.getElementById('confirmTitle').textContent = title;
  document.getElementById('confirmContent').textContent = content;
  // showAlert 会隐藏取消按钮 → 普通确认弹窗恢复双按钮
  document.getElementById('confirmCancel').style.display = '';
  const btn = document.getElementById('confirmOk');
  btn.textContent = confirmText || '确定';
  btn.style.background = confirmColor || '#FF3B30';
  _confirmCallback = cb;
  openModal('confirmModal');
}

/* 单按钮提示弹窗（对齐小程序 wx.showModal({showCancel:false})）：
   复用 confirmModal，隐藏「取消」并让「知道了」占满整行。 */
function showAlert(title, content, okText) {
  document.getElementById('confirmTitle').textContent = title;
  document.getElementById('confirmContent').textContent = content;
  document.getElementById('confirmCancel').style.display = 'none';
  const btn = document.getElementById('confirmOk');
  btn.textContent = okText || '知道了';
  btn.style.background = '#007AFF';
  _confirmCallback = null;
  openModal('confirmModal');
}

function confirmOk() {
  closeModal('confirmModal');
  const cb = _confirmCallback;
  _confirmCallback = null;
  if (typeof cb === 'function') cb();
}

/* ================= 工具 ================= */

function esc(s) {
  return String(s == null ? '' : s).replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function formatRemain(str) {
  if (!str) return '';
  const t = new Date(String(str).replace(/-/g, '/')).getTime();
  if (isNaN(t)) return str;
  const remain = t - Date.now();
  if (remain <= 0) return '已过期';
  // 对齐小程序密钥倒计时格式 m:ss（Math.ceil 取整）
  const totalSec = Math.ceil(remain / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return m + ':' + (s < 10 ? '0' + s : s);
}

async function copyText(text, successMsg) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    if (successMsg) showToast(successMsg);
  } catch (e) {
    showToast('复制失败');
  }
}

/* 剪贴板读取（授权码/密钥自动获取用）。Capacitor WebView 下 navigator.clipboard.readText 常因
   非安全上下文 / clipboard-read 权限被拒，必须走多级降级：
   ① 官方 Capacitor 剪贴板插件（原生，最可靠，若已装 @capacitor/clipboard）
   ② navigator.clipboard.readText（现代浏览器 / 安全的 WebView）
   ③ 隐藏 contenteditable + execCommand('paste')（老 WebView，依赖按钮点击带来的用户手势）
   Promise<string>，取消/失败时 reject（调用方据此 toast「无法读取剪贴板」）。 */
async function readClipboard() {
  const cap = (typeof window !== 'undefined' && window.Capacitor && window.Capacitor.Plugins && window.Capacitor.Plugins.Clipboard);
  if (cap && typeof cap.read === 'function') {
    const r = await cap.read();
    return (r && r.value) ? String(r.value) : '';
  }
  if (navigator.clipboard && navigator.clipboard.readText) {
    if (window.isSecureContext !== false) return await navigator.clipboard.readText().then(t => String(t || ''));
  }
  // 兜底：execCommand('paste') 需在用户手势回调内执行（按钮点击即是）
  const el = document.createElement('div');
  el.setAttribute('contenteditable', 'true');
  el.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0;';
  document.body.appendChild(el);
  el.focus();
  try {
    const ok = document.execCommand('paste');
    const text = ok ? (el.textContent || '') : '';
    el.remove();
    if (!ok) throw new Error('paste denied');
    return text;
  } catch (e) {
    el.remove();
    throw e;
  }
}

const ORDER_STATUS_MAP = {
  queued: '排队中',
  printing: '待添加',
  accepted: '已添加',
  offline_unknown: '断线未知',
  sent: '已完成',
  failed: '失败',
  timeout: '超时',
  abandoned: '放弃打印',
  rejected: '被打回',
  canceled: '已取消',
  reserved: '已预留',
  scheduled: '已预约',
  downloading: '文件传输中',
  waiting: '等待打印',
};

/* ================= 打印机状态 ================= */

async function checkPrinterStatus() {
  try {
    const r = await api('/api/printer_status');
    const active = !!(r.data && r.data.active);
    const dot = document.querySelector('.status-dot');
    const text = document.getElementById('statusText');
    const bar = document.getElementById('printerStatus');
    if (dot) dot.className = 'status-dot';
    if (text) {
      text.textContent = active ? '打印机在线' : '打印机离线';
    }
    if (bar) {
      // 对齐小程序 printerActive ? 'status-active' : 'status-inactive'：
      // 圆点/文字颜色由这两个父类驱动，离线态必须显式加 status-inactive
      bar.classList.toggle('status-active', active);
      bar.classList.toggle('status-inactive', !active);
    }
    return true;   // S1-09：给轮询调度器一个成功信号
  } catch (e) {
    const dot = document.querySelector('.status-dot');
    const text = document.getElementById('statusText');
    const bar = document.getElementById('printerStatus');
    if (dot) dot.className = 'status-dot';
    if (text) text.textContent = '打印机离线';
    if (bar) {
      bar.classList.remove('status-active');
      bar.classList.add('status-inactive');
    }
    return false;  // S1-09：失败 → 轮询退避
  }
}

/* ================= 打印机状态轮询（S1-09：自调度） ================= */
let printerPoll = null;

function startPrinterPolling() {
  stopPrinterPolling();
  printerPoll = createPollScheduler(async () => {
    // 两个请求都落定后才算这一轮结束；任一失败 → reject → 调度器退避
    const results = await Promise.all([checkPrinterStatus(), loadClaimingDevices()]);
    // 2026-10-26（用户现场）：全局保留期会被管理员随时改（9天→7天），下单页常驻的
    // 「文件保留」那一行要跟着刷新；不参与本轮成败（该接口偶发失败不该让状态/设备轮询退避）。
    if (typeof loadRetentionOptions === 'function') loadRetentionOptions();
    if (results.some((ok) => ok === false)) throw new Error('printer poll failed');
  }, 30000, { immediate: false });   // initApp 已先拉过一次，别立刻重复
  printerPoll.start();
}

function stopPrinterPolling() {
  if (printerPoll) {
    printerPoll.stop();
    printerPoll = null;
  }
}

/* ================= 可接单设备（2026-12 多设备接单） ================= */

let claimingDevices = [];      // 可接单设备列表 {client_id, label, online, pin, pin_index, ...}
let selectedDeviceId = '';     // 当前选中的目标设备（提交订单时随 target_client_id 上报）
let stackTogglingId = '';      // 攒批模式远程切换中（client_id）：非空时开关置灰、拒绝重复点击
// 接单设备「自动选中 / 用户手动选中」状态位（2026-10 置顶）。只有用户真手点设备条
// （设备列表的事件委托）会置 true；默认口径选出来的设备一律保持 false。
let deviceChosenByUser = false;
// 刚保存过新的置顶顺序（一次性消费）：下一次 loadClaimingDevices 必须按服务端新顺序重置选中
let deviceOrderJustSaved = false;
// 「接单设备显示顺序」弹窗的**本地临时顺序**（置顶组在前）：{client_id,label,online,pin,pin_index}
let deviceOrderRows = [];
let deviceOrderSaving = false;

// 服务端已排好序，返回顺序**就是**展示顺序：置顶组在前（按 pin_index 升序，组内完全不看在线）
// → 未置顶组在线优先 → client_id。每台带 pin:boolean 与 pin_index:int(0 基)|null。
//
// 默认选中口径（2026-10 置顶，两条）：
//   ① 有置顶 ⇒ 选 pin_index === 0 的那台（**离线也选**：任务排进队列等它上线，
//      而不是偷偷换一台在线设备把顾客的单打到别人的纸上）；
//   ② 无置顶 ⇒ 沿用老口径「第一个在线设备」，无在线则第一个。
// 重置触发的三条条件（其余情况一律不抢用户的手动选择）：
//   A) 用户刚保存了新的置顶顺序（deviceOrderJustSaved）⇒ 按新顺序重选（一次性）；
//   B) 当前选中已失效（那台设备不再出现在可接单列表里）；
//   C) 用户从未手动点选过（deviceChosenByUser !== true）⇒ 跟随默认口径（置顶变了也跟着变）。
// 这三条任一命中才重置；且重置出来的选择仍是「自动选择」⇒ deviceChosenByUser 归 false。
async function loadClaimingDevices() {
  try {
    const r = await api('/api/claiming_devices');
    const devices = (r.data && r.data.success && r.data.devices) ? r.data.devices : [];
    claimingDevices = devices.map(normalizeDeviceStack);
    const pinFirst = devices.find(d => d.pin === true && d.pin_index === 0) ||
                     devices.find(d => d.pin === true) || null;
    const onlineOne = devices.find(d => d.online);
    const defaultId = pinFirst ? pinFirst.client_id
      : (onlineOne ? onlineOne.client_id : (devices.length ? devices[0].client_id : ''));
    const stillSelectable = !!selectedDeviceId && devices.some(d => d.client_id === selectedDeviceId);
    const justSaved = deviceOrderJustSaved === true;
    if (justSaved) deviceOrderJustSaved = false;
    if (justSaved || !stillSelectable || deviceChosenByUser !== true) {
      selectedDeviceId = defaultId;
      deviceChosenByUser = false;   // 重置出来的仍是自动选择
    }
    renderDeviceList();
    return true;   // S1-09：给轮询调度器一个成功信号
  } catch (e) {
    /* 静默失败，保留上次列表 */
    return false;  // S1-09：失败 → 轮询退避
  }
}

// 攒批三态的归一（2026-11 需求 2，与小程序 index.js 的 _normalizeDeviceStack 同一份口径）：
//   stack_view    = 期望值 true/false/null（null=从未上报 → 「需人工翻纸」）
//                   ← 后端 C4 的 stack_desired；缺字段（旧后端）时回退 stack_mode
//   stack_pending = 后端 C4 的 stack_pending（期望值已记下、设备还没对齐）
// 只影响渲染与按钮文案，不代表设备真实状态。
function normalizeDeviceStack(d) {
  if (!d || !d.client_id) return d;
  let view;
  if (d.stack_desired === true || d.stack_desired === false) view = d.stack_desired;
  else if (d.stack_mode === true || d.stack_mode === false) view = d.stack_mode;
  else view = null;
  return Object.assign({}, d, {
    stack_view: view,
    stack_pending: d.stack_pending === true,
  });
}

// 攒批开关的目标态：已开 → 点它就是关；未开/从未上报 → 点它是开。
// 用期望值（stack_view）而不是设备实际状态（stack_mode）：离线连续点两次时，
// 第二次必须基于第一次记下的期望值取反，否则会连发两个相同的 enable。
function stackDesiredOf(d) {
  if (!d) return null;
  if (d.stack_view === true || d.stack_view === false) return d.stack_view;
  // 没走 normalizeDeviceStack 的原样对象（服务端回包 / 旧缓存）也认 stack_desired
  if (d.stack_desired === true || d.stack_desired === false) return d.stack_desired;
  return (d.stack_mode === true) ? true : (d.stack_mode === false ? false : null);
}

function renderDeviceList() {
  const card = document.getElementById('deviceSelectCard');
  const list = document.getElementById('deviceList');
  if (!card || !list) return;
  // 「显示顺序」入口：仅 admin/super_admin（普通用户整块不渲染，不留空壳）
  const orderEntry = document.getElementById('deviceOrderEntry');
  if (orderEntry) orderEntry.style.display = isAdminRole() ? '' : 'none';
  if (!claimingDevices.length) { card.style.display = 'none'; return; }
  card.style.display = '';
  // 入场动画：设备卡片已加入 PRINT_ENTRANCE 调度（0.15s）。
  // 数据早于调度到达 → 不主动播放（_entrancePending 尚未置位），等 0.15s 调度统一触发，
  // 避免与 logo 同时出现；数据晚于调度 → _entrancePending 已置位，显示时立即补播。
  if (typeof playCardEntrance === 'function' && card._entrancePending) playCardEntrance(card);
  list.innerHTML = claimingDevices.map(d =>
    `<view class="device-item ${selectedDeviceId === d.client_id ? 'device-item-on' : ''}" data-id="${esc(d.client_id)}" data-hover="device-hover">
      <view class="device-dot ${d.online ? 'device-dot-on' : 'device-dot-off'}"></view>
      <text class="device-label">${esc(d.label || d.client_id)}</text>
      ${d.pin ? '<text class="device-pin-badge">📌 置顶</text>' : ''}
      ${stackBadgeHtml(d)}
      ${stackToggleBtnHtml(d)}
      <text class="device-state ${d.online ? 'device-state-on' : 'device-state-off'}">${d.online ? '在线' : '离线'}</text>
    </view>`
  ).join('');
}

// 攒批模式状态徽标（三态，2026-11 需求 2 改读**期望值** stack_view）：
// true=攒批中（醒目）/ false=攒批未开（弱化灰）/ null（该设备从未上报，状态未知）→「🖐 需人工翻纸」。
// ⚠ 攒批只对「需人工翻纸」的机型（auto_duplex === false）渲染 —— 自动双面的机器一次成型，
// 多任务合并翻纸纯属多余；能力未确认的也不给入口（2026-10 用户要求，三端同一条判据）。
// ⚠ 这三个胶囊**互斥**（2026-10-27 用户要求：设备条必须只占一行、高度一致）：旧写法「需人工翻纸」
//   与「攒批未开」同时出现，加上「开启攒批」按钮和「在线」，375px 一行放不下 ⇒ 换行 ⇒ 高度参差。
//   现在多了「⏳ 待应用」角标，靠 .device-item 的 flex-wrap:nowrap + .device-label 省略号保持单行。
//
// 2026-11（需求 2）：徽标显示**目标状态**（stack_desired / 归一后的 stack_view），旁边挂
// 「⏳ 待应用」直到设备 stack_state 回报对齐（服务端清 stack_pending）。这样离线改完能看出
// 「改成了什么、还没生效」，而不是只有一句「待应用」。
function stackBadgeHtml(d) {
  if (!d || d.auto_duplex !== false) return '';
  const desired = stackDesiredOf(d);
  let html;
  if (desired === true) html = '<text class="device-state device-state-stack">🖐 攒批中</text>';
  else if (desired === false) html = '<text class="device-state device-state-stack-off">🖐 攒批未开</text>';
  else html = '<text class="device-state device-state-manual">🖐 需人工翻纸</text>';
  if (d.stack_pending === true) html += '<text class="device-pending-badge">⏳ 待应用</text>';
  return html;
}

// 管理员/超管判定（攒批模式远程开关只对他们渲染）
function isAdminRole() {
  return state.role === 'admin' || state.role === 'super_admin';
}

// 攒批模式远程开关：只对**需人工翻纸**的机型渲染，且仅 admin/super_admin（普通用户看不到可点
// 控件，也不留空壳）。
// 2026-11（需求 2）：**允许随时切换**（在线 / 离线 / 设备忙都放行）—— 旧的
// `online !== false && stack_can_toggle === false` 置灰已删除；那套准入判定只在服务端，
// 服务端忙时也照样记账（stack_desired/stack_pending），设备空闲后自动下发。
// 只有本机请求在飞行中（stackTogglingId）才灰显，防重复点击。
function stackToggleBtnHtml(d) {
  if (!d || d.auto_duplex !== false) return '';
  if (!isAdminRole()) return '';
  const disabled = stackTogglingId === d.client_id;
  const label = stackDesiredOf(d) === true ? '关闭攒批' : '开启攒批';
  return `<view class="device-stack-btn${disabled ? ' device-stack-btn-disabled' : ''}" data-id="${esc(d.client_id)}">${label}</view>`;
}

// 攒批模式（多任务合并翻纸）远程开关：只对「需人工翻纸」的机型（auto_duplex === false）开放。
// 该接口一律 HTTP 200，按 body 的 success 判断（失败时 message 即拦截原因）。
// 2026-11（需求 2）：离线 / 设备忙都能改；后端把目标状态记在 stack_desired 并回 stack_pending=true，
// 设备 stack_state 回报后自动对齐（对齐成功才清 pending ⇒ 前端一直显示「⏳ 待应用」）。
// 离线反复切换以**最后一次**为准：enable 由期望值取反 + 请求中不并发。
async function toggleStackMode(clientId) {
  // 兜底：普通用户/游客即使状态异常也不放行
  if (!isAdminRole()) return;
  // 请求中禁用，防止重复点击
  if (stackTogglingId) return;
  const idx = claimingDevices.findIndex(d => d.client_id === clientId);
  if (idx < 0) return;
  const d = claimingDevices[idx];
  // 兜底：该机型不需要攒批（支持自动双面 / 能力未确认）—— 就算状态异常也不放行
  if (d.auto_duplex !== false) {
    showToast('该设备支持自动双面（或双面能力未确认），不需要攒批模式', 2500);
    return;
  }
  const enable = stackDesiredOf(d) !== true;
  stackTogglingId = clientId;
  renderDeviceList();
  try {
    const r = await api('/api/printer/stack_mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: clientId, enable: enable }),
    });
    const data = (r && r.data) || {};
    showToast(data.message || (data.success ? '操作成功' : '切换失败，请稍后重试'), data.success ? 2000 : 2500);
    if (data.success) {
      // 成功后就地更新 claimingDevices 缓存里那一台，再刷新列表（就地更新，不整表重新拉取）
      const i = claimingDevices.findIndex(x => x.client_id === clientId);
      if (i >= 0) {
        const pending = (data.stack_pending !== undefined)
          ? (data.stack_pending === true)
          : (data.pending === true);
        let view;
        if (data.stack_desired === true || data.stack_desired === false) view = data.stack_desired;
        else if (pending) view = enable === true;      // 期望值已记下、设备还没对齐
        else view = data.stack_mode === true;          // 已落到设备上
        claimingDevices[i].stack_pending = pending;
        claimingDevices[i].stack_view = view;
        claimingDevices[i].stack_mode = view;
        claimingDevices[i].stack_busy = !!data.stack_busy;
      }
    }
  } catch (e) {
    showToast('网络错误，切换失败', 2500);
  }
  stackTogglingId = '';
  renderDeviceList();
}

// 设备相关点击（事件委托）
document.addEventListener('click', function (e) {
  if (!e.target.closest) return;
  // 「显示顺序」入口（仅 admin/super_admin 会渲染它，openDeviceOrderSheet 里再兜一次底）
  if (e.target.closest('.device-order-entry')) { openDeviceOrderSheet(); return; }
  // 置顶开关（弹窗内）：先于其它处理，它在 .device-order-row 内部
  const pinEl = e.target.closest('.device-order-pin');
  if (pinEl && pinEl.dataset && pinEl.dataset.id) { toggleDevicePin(pinEl.dataset.id); return; }
  // 置顶组内 ↑/↓
  const moveEl = e.target.closest('.device-order-move');
  if (moveEl && moveEl.dataset && moveEl.dataset.id && moveEl.dataset.dir) {
    moveDevicePin(moveEl.dataset.id, moveEl.dataset.dir); return;
  }
  // 攒批模式开关：先于设备选中处理（它在设备项内部，否则会被当成「选中该设备」）
  const btn = e.target.closest('.device-stack-btn');
  if (btn && btn.dataset && btn.dataset.id) { toggleStackMode(btn.dataset.id); return; }
  const item = e.target.closest('.device-item');
  if (item && item.dataset && item.dataset.id) {
    // 用户**真手点**的唯一入口 ⇒ 打开状态位：之后 30s 轮询/刷新都不许把它抢回去
    // （除非那台设备从列表里消失，或用户自己又保存了新的置顶顺序）。
    deviceChosenByUser = true;
    selectedDeviceId = item.dataset.id;
    renderDeviceList();
  }
});

/* ================= 接单设备显示顺序（置顶 / 置顶组内调优先级） =================
   链路：GET /api/claiming_devices 拿 pin/pin_index → 本地改临时顺序（deviceOrderRows）→
   POST /api/admin/device_pin_order {order:[置顶的 client_id 按序]} → 成功则重新拉列表
   （服务端已排好序）并按新的 pin_index===0 重选；失败回滚本地顺序 + 弹窗内可见红字 + toast。 */

// 服务端顺序 → 弹窗行：置顶组在前（按 pin_index 升序），未置顶组保持服务端顺序
function buildDeviceOrderRows(devices) {
  const pinned = [];
  const rest = [];
  (devices || []).forEach((d) => {
    if (!d || !d.client_id) return;
    const row = {
      client_id: d.client_id,
      label: d.label || d.device_name || d.client_id,
      online: d.online === true,
      pin: d.pin === true,
      pin_index: (typeof d.pin_index === 'number') ? d.pin_index : null,
    };
    (row.pin ? pinned : rest).push(row);
  });
  const key = (r) => (r.pin_index === null ? 1e9 : r.pin_index);
  pinned.sort((a, b) => key(a) - key(b));
  return reindexPins(pinned.concat(rest));
}

// 置顶行的 pin_index 重排成 0..n-1（未置顶恒为 null），本地顺序与展示序一致
function reindexPins(rows) {
  let p = 0;
  return rows.map(r => Object.assign({}, r, { pin_index: r.pin ? p++ : null }));
}

function openDeviceOrderSheet() {
  if (!isAdminRole()) return;   // 兜底：普通用户/游客即使状态异常也不放行
  deviceOrderRows = buildDeviceOrderRows(claimingDevices);
  deviceOrderSaving = false;
  const err = document.getElementById('deviceOrderError');
  if (err) { err.style.display = 'none'; err.textContent = ''; }
  const btn = document.getElementById('deviceOrderSaveBtn');
  if (btn) { btn.textContent = '完成'; btn.disabled = false; }
  renderDeviceOrderRows();
  openModal('deviceOrderSheet');
}

function renderDeviceOrderRows() {
  const body = document.getElementById('deviceOrderBody');
  if (!body) return;
  body.innerHTML = deviceOrderRows.map(r =>
    `<view class="device-order-row${r.pin ? ' device-order-row-pinned' : ''}" data-id="${esc(r.client_id)}">
      <view class="device-order-pin${r.pin ? ' device-order-pin-on' : ''}" data-id="${esc(r.client_id)}">${r.pin ? '📌 置顶' : '置顶'}</view>
      <text class="device-order-name">${esc(r.label)}</text>
      <text class="device-state device-order-state ${r.online ? 'device-state-on' : 'device-state-off'}">${r.online ? '在线' : '离线'}</text>
      <view class="device-order-move${r.pin ? '' : ' device-order-move-disabled'}" data-id="${esc(r.client_id)}" data-dir="up">↑</view>
      <view class="device-order-move${r.pin ? '' : ' device-order-move-disabled'}" data-id="${esc(r.client_id)}" data-dir="down">↓</view>
    </view>`
  ).join('');
}

// 置顶 / 取消置顶。新置顶的排到**置顶组末尾**（不抢已有的第一台，避免一次点击就改掉默认设备），
// 取消置顶的回到**未置顶组开头** —— 两种情况下"插入点"都是去掉它之后的置顶行数。
function toggleDevicePin(clientId) {
  const rows = deviceOrderRows.slice();
  const i = rows.findIndex(r => r.client_id === clientId);
  if (i < 0) return;
  const row = rows.splice(i, 1)[0];
  const insertAt = rows.filter(r => r.pin).length;
  rows.splice(insertAt, 0, Object.assign({}, row, { pin: !row.pin }));
  deviceOrderRows = reindexPins(rows);
  const err = document.getElementById('deviceOrderError');
  if (err) { err.style.display = 'none'; err.textContent = ''; }
  renderDeviceOrderRows();
}

// ↑ / ↓ 只在**置顶组内**调优先级（未置顶组顺序由服务端按「在线优先 → client_id」定）；
// 到顶/到底不动，也不跨组。
function moveDevicePin(clientId, dir) {
  const rows = deviceOrderRows.slice();
  const i = rows.findIndex(r => r.client_id === clientId);
  if (i < 0 || !rows[i].pin) return;
  const pinCount = rows.filter(r => r.pin).length;
  const j = i + (dir === 'up' ? -1 : 1);
  if (j < 0 || j >= pinCount) return;
  const tmp = rows[i]; rows[i] = rows[j]; rows[j] = tmp;
  deviceOrderRows = reindexPins(rows);
  renderDeviceOrderRows();
}

function failDeviceOrder(msg) {
  deviceOrderSaving = false;
  const btn = document.getElementById('deviceOrderSaveBtn');
  if (btn) { btn.textContent = '完成'; btn.disabled = false; }
  // 回滚本地临时顺序（按服务端真值重建），再把失败原因**可见地**摆出来（不静默、也不显示成功态）
  deviceOrderRows = buildDeviceOrderRows(claimingDevices);
  renderDeviceOrderRows();
  const err = document.getElementById('deviceOrderError');
  if (err) { err.textContent = msg; err.style.display = ''; }
  showToast(msg, 2500);
}

// 保存：order 只带**置顶**的 client_id（服务端自己去重保序、丢弃不可用 cid、截到 50 项）
async function saveDeviceOrder() {
  if (deviceOrderSaving) return;
  const order = deviceOrderRows.filter(r => r.pin).map(r => r.client_id);
  deviceOrderSaving = true;
  const btn = document.getElementById('deviceOrderSaveBtn');
  if (btn) { btn.textContent = '保存中…'; btn.disabled = true; }
  try {
    const r = await api('/api/admin/device_pin_order', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ order: order }),
    });
    const data = (r && r.data) || {};
    const code = (r && r.status) || 0;
    // 只有 HTTP 200 + success:true 才算成功（401/403/500 都是失败，403 文案「需要管理员权限」）
    if (code !== 200 || data.success !== true) {
      failDeviceOrder(data.message || ('保存失败（HTTP ' + code + '）'));
      return;
    }
    deviceOrderSaving = false;
    if (btn) { btn.textContent = '完成'; btn.disabled = false; }
    closeModal('deviceOrderSheet');
    // 保存成功 ⇒ 触发条件 A：重拉列表（服务端已按新顺序排好）并按 pin_index===0 重置选中
    deviceChosenByUser = false;
    deviceOrderJustSaved = true;
    await loadClaimingDevices();
    showToast('显示顺序已保存', 1500);
  } catch (e) {
    // 网络层异常（超时/断网/401 重登失败）同样回滚 + 明确提示
    failDeviceOrder('网络错误，显示顺序保存失败');
  }
}

/* ================= 初始化 ================= */

function setupNav() {
  document.getElementById('navBack').addEventListener('click', hideView);
  document.getElementById('themeToggle').addEventListener('click', toggleTheme);
  document.querySelectorAll('.modal-mask').forEach(m => {
    m.addEventListener('click', function (e) {
      if (e.target === this && !this.classList.contains('modal-locked')) closeModal(this.id);
    });
  });
  document.getElementById('confirmCancel').addEventListener('click', () => closeModal('confirmModal'));
  document.getElementById('confirmOk').addEventListener('click', confirmOk);
}

// 小程序 hover-class 的 Web 等价：按下加按压类、松开移除。
// 优先用元素 data-hover 指定的类（stepper-hover/generate-hover/...），
// 否则对常见交互元素统一加 tap-active（scale 0.97）。
function setupPressFeedback() {
  const DEFAULT_PRESS = [
    '.btn-primary', '.btn-secondary', '.btn-destructive', '.btn-small', '.add-file-btn',
    '.copy-btn', '.redeem-btn', '.redeem-paste-btn', '.retention-save-btn', '.delete-all-btn',
    '.btn-cancel-sm', '.page-btn', '.page-num', '.page-size-selector', '.page-size-option',
    '.sheet-option', '.schedule-mode', '.schedule-picker', '.picker-trigger', '.picker-option',
    '.range-picker-cell', '.range-picker-action', '.range-summary-trigger', '.file-retry-btn',
    '.duplex-opt', '.img-ori-opt', '.key-type-option', '.license-badge',
    '.admin-delete-btn', '.stepper-btn',
  ].join(',');
  // 注：.key-delete / .avatar-btn 不在默认列表——对齐小程序仅用 CSS :active 反馈
  //（key-delete scale 0.95、头像 opacity 0.7），避免与 JS tap-active(0.97) 冲突
  let pressedEl = null;
  const clear = () => {
    if (!pressedEl) return;
    const cls = pressedEl.dataset.hover;
    if (cls) pressedEl.classList.remove(cls);
    else pressedEl.classList.remove('tap-active');
    pressedEl = null;
  };
  const onDown = (e) => {
    if (!e.target || !e.target.closest) return;
    clear();
    const custom = e.target.closest('[data-hover]');
    if (custom) {
      pressedEl = custom;
      custom.classList.add(custom.dataset.hover);
      return;
    }
    const el = e.target.closest(DEFAULT_PRESS);
    if (el) {
      pressedEl = el;
      el.classList.add('tap-active');
    }
  };
  document.addEventListener('pointerdown', onDown, true);
  document.addEventListener('pointerup', clear, true);
  document.addEventListener('pointercancel', clear, true);
  window.addEventListener('blur', clear);
}

function setupTabs() {
  document.querySelectorAll('.tab-item').forEach(item => {
    item.addEventListener('click', () => {
      if (Date.now() < _tabSuppressClickUntil) return;   // 这一段是拖动收尾，别再补一次切换
      switchTab(item.dataset.tab);
    });
  });
}

function initApp() {
  // 前端日志上报：挂 window.onerror / unhandledrejection / fetch 失败兜底（见 www/logger.js）。
  // 必须放在最前面 —— 后面任何一步初始化报错都能被 onerror 接住；logger 自身异常一律吞掉。
  try {
    if (window.HNLogger && typeof window.HNLogger.install === 'function') window.HNLogger.install();
  } catch (e) { /* 日志挂了不能拖垮启动 */ }
  setupTabs();
  initTabThumb();
  initTabDrag();
  setupNav();
  setupPressFeedback();
  // 首屏入场由打印页各卡片的 .enter-XX 动画驱动（0.5s 起逐 0.1s 递增），
  // 不再叠加整页 page-fade-in-delayed，避免双重延迟导致出现顺序错乱
  // 必须先注入原生安全区（--safe-top）再定位主题按钮：导航栏高度 = 48px + safe-top，
  // 若先用 env() 兜底定位（safe-top=0），随后 --safe-top 注入使导航栏变高，会把按钮盖住。
  applyNativeSafeArea();
  positionThemeToggle();
  updateThemeToggleVisibility();
  window.addEventListener('resize', positionThemeToggle);
  window.addEventListener('resize', applyNativeSafeArea);
  applyTheme(state.themeMode, { skipServer: true });
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (state.themeMode === 'auto') applyTheme('auto', { skipServer: true });
    });
  }
  initScrollEngines();
  checkPrinterStatus();
  loadClaimingDevices();
  // S1-09：自调度轮询（上一轮两个请求都落定后才排下一轮；失败 1.5× 退避、成功恢复 30s）
  startPrinterPolling();
  ensureLogin().then(() => refreshSession()).finally(() => {
    if (typeof initPrintPage === 'function') initPrintPage();
    if (typeof initMePage === 'function') initMePage();
    if (typeof initRecentOrdersView === 'function') initRecentOrdersView();
  });
}

// 主题切换按钮放在导航栏下方（对齐小程序 navBarBtnTop = 状态栏 + 导航栏 + 8px）
function positionThemeToggle() {
  const toggle = document.getElementById('themeToggle');
  const navBar = document.getElementById('navBar');
  if (toggle && navBar) {
    const r = navBar.getBoundingClientRect();
    toggle.style.top = (r.bottom + 8) + 'px';
  }
}

document.addEventListener('DOMContentLoaded', initApp);
