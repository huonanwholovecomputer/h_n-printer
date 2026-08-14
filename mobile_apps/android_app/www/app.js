/* HN Cloud Print — Android App Core
 * 状态 / 请求封装(401 自动重登) / 主题(自动·浅色·深色) / 视图栈
 * 橡皮筋滚动引擎（移植小程序自定义滚动物理）/ 手势总线 / 页面转场
 */

const DEFAULT_BASE_URL = 'https://hn-space.cn';
const BASE_URL = localStorage.getItem('hn_base_url') || DEFAULT_BASE_URL;

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

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
  let res;
  try {
    res = await fetch(BASE_URL + path, Object.assign({}, opts, { headers }));
  } catch (e) {
    const err = new Error('网络错误');
    err.network = true;
    throw err;
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
      const r = await fetch(BASE_URL + '/api/device_login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: getDeviceId() }),
      });
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
}

function syncNativeStatusBar(dark) {
  const b = window.AndroidBars;
  if (b && b.setDark) b.setDark(dark);
}

function applyTheme(mode, opts) {
  opts = opts || {};
  state.themeMode = mode;
  localStorage.setItem('hn_theme_mode', mode);
  const dark = effectiveDark(mode);
  syncNativeStatusBar(dark);
  // 深色类同时挂到 body（变量级联）、背景层与所有 modal-mask
  document.body.classList.toggle('theme-dark', dark);
  document.body.classList.toggle('dark', dark);
  const bg = document.getElementById('themeBgLayer');
  if (bg) bg.classList.toggle('bg-dark', dark);
  document.querySelectorAll('.modal-mask').forEach(m => m.classList.toggle('theme-dark', dark));
  document.documentElement.style.colorScheme = dark ? 'dark' : 'light';
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', dark ? '#1C1C1E' : '#F2F2F7');
  const toggle = document.getElementById('themeToggle');
  if (toggle) toggle.textContent = { auto: '🌓', dark: '🌙', light: '☀️' }[mode] || '🌓';
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
    this._wheelTimer = null;
    this._measureTimer = null;
    this._prevFlingVel = 0;
    this._lastFlingT = 0;
    this._spring = null; // 边界弹簧状态 { target, dir, x, vx, xMax, compressing, t0, durR }
    this._destroyed = false;

    // 接管滚动：外层裁剪 + 禁用浏览器手势，内容用 transform 驱动
    this.el.classList.add('js-scroll');
    this.el.style.overflow = 'hidden';
    this.el.style.touchAction = 'none';

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
    this.el.removeEventListener('touchend', this._hTe);
    this.el.removeEventListener('touchcancel', this._hTe);
    this.el.removeEventListener('wheel', this._hW);
  }

  _bind() {
    this._hTs = (e) => this.onTouchStart(e);
    this._hTm = (e) => this.onTouchMove(e);
    this._hTe = (e) => this.onTouchEnd(e);
    this._hW = (e) => this.onWheel(e);
    this.el.addEventListener('touchstart', this._hTs, { passive: true });
    this.el.addEventListener('touchmove', this._hTm, { passive: false });
    this.el.addEventListener('touchend', this._hTe, { passive: true });
    this.el.addEventListener('touchcancel', this._hTe, { passive: true });
    this.el.addEventListener('wheel', this._hW, { passive: false });
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
    if (this.y > this.maxY) this.snapBack();
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
    // 内层可滚动列表（文件列表 / 滚轮选择器）：能滚时交给原生
    this._nestedScroll = (e.target && e.target.closest && e.target.closest('.file-list-scroll, .wheel-viewport')) || null;
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
    const ddy = cur.clientY - this._lastY;
    // 嵌套滚动：内层列表可继续滚时让给原生，到边界后引擎接管外层
    if (this._nestedScroll) {
      const el = this._nestedScroll;
      const maxTop = el.scrollHeight - el.clientHeight;
      const canScrollDown = ddy < 0 && el.scrollTop < maxTop - 0.5;
      const canScrollUp = ddy > 0 && el.scrollTop > 0.5;
      if (canScrollDown || canScrollUp) {
        this._lastY = cur.clientY;
        this._lastT = now;
        return;
      }
      this._nestedScroll = null; // 内层到边界 → 引擎接管
    }
    e.preventDefault();
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
    const tick = () => {
      if (!this._spring || this._destroyed) return;
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
        this.y = s.target;
        this._spring = null;
        this.applyY();
        return;
      }
      this.tick = requestAnimationFrame(tick);
    };
    this.tick = requestAnimationFrame(tick);
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
    const nested = e.target && e.target.closest ? e.target.closest('.file-list-scroll, .wheel-viewport') : null;
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
  ];
  overlayScrollers.forEach(([key, scId, ctId]) => {
    const sc = document.getElementById(scId);
    const ct = document.getElementById(ctId);
    if (sc && ct && !scrollEngines[key]) {
      scrollEngines[key] = new FlingEngine(sc, ct, {});
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

function animatePageIn(el, cls) {
  if (!el) return;
  el.classList.remove('page-fade-in', 'page-enter-right', 'page-enter-left', 'page-exit-left', 'page-exit-right');
  void el.offsetWidth; // 强制 reflow 重启动画
  el.classList.add(cls || 'page-fade-in');
}

let currentTab = 'print';
let _tabSwitching = false;
let _viewTransition = false;

// 小程序同款 tab 切换：当前页横向滑出（240ms），新页从对面滑入
function switchTab(tab) {
  if (tab === currentTab || _tabSwitching) return;
  _tabSwitching = true;

  // 切换前关闭打开的弹窗（对齐小程序 animateExit 中的处理）
  document.querySelectorAll('.modal-mask').forEach(m => {
    if (m.style.display !== 'none' && m.id) closeModal(m.id);
  });
  // 每页条数下拉在 page-root 外 fixed，切页时一并收起
  if (typeof closePageSizePicker === 'function') closePageSizePicker();

  // tab 栏立即更新选中态 + 点击图标弹跳
  document.querySelectorAll('.tab-item').forEach(t => {
    const on = t.dataset.tab === tab;
    t.classList.toggle('active', on);
    const iconWrap = t.querySelector('.tab-icon-wrap');
    if (iconWrap) iconWrap.classList.toggle('icon-active', on);
    const txt = t.querySelector('.tab-text');
    if (txt) txt.classList.toggle('text-active', on);
    if (on) {
      t.classList.add('tab-hover');
      setTimeout(() => t.classList.remove('tab-hover'), 400);
    }
  });

  const printPage = document.getElementById('page-print');
  const mePage = document.getElementById('page-me');
  const exiting = tab === 'print' ? mePage : printPage;
  const entering = tab === 'print' ? printPage : mePage;
  // 离开打印页→左滑退出；离开我页→右滑退出（对齐 custom-tab-bar dir 逻辑）
  const exitCls = exiting === printPage ? 'page-exit-left' : 'page-exit-right';
  const enterCls = entering === mePage ? 'page-enter-right' : 'page-enter-left';

  exiting.classList.remove('page-exit-left', 'page-exit-right', 'page-fade-in', 'page-enter-right', 'page-enter-left');
  void exiting.offsetWidth;
  exiting.classList.add(exitCls);

  setTimeout(() => {
    exiting.style.display = 'none';
    entering.style.display = '';
    entering.classList.remove('page-exit-left', 'page-exit-right', 'page-fade-in', 'page-enter-right', 'page-enter-left');
    void entering.offsetWidth;
    entering.classList.add(enterCls);

    document.getElementById('navTitle').textContent = tab === 'print' ? '提交打印' : '我';
    document.getElementById('navBack').style.display = 'none';
    document.getElementById('tabBar').style.display = '';
    hideViews();
    currentTab = tab;
    _tabSwitching = false;
    updateThemeToggleVisibility();
    if (tab === 'me' && typeof loadMeTab === 'function') loadMeTab();
    if (tab === 'print' && typeof onPrintTabShown === 'function') onPrintTabShown();
    measureAll();
  }, 240);
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
  // me 页离场动画（对齐小程序 page-exit-left + 280ms 导航延迟），280ms 后切换子视图
  const mePage = document.getElementById('page-me');
  mePage.classList.remove('page-exit-left', 'page-exit-right', 'page-fade-in', 'page-enter-right', 'page-enter-left');
  void mePage.offsetWidth;
  mePage.classList.add('page-exit-left');
  setTimeout(() => {
    mePage.style.display = 'none';
    hideViews();
    const v = document.getElementById(id);
    if (v) { v.style.display = ''; animatePageIn(v, 'page-enter-right'); }
    updateThemeToggleVisibility();
    measureAll();
    _viewTransition = false;
  }, 280);
}

function hideView() {
  if (_viewTransition) return;
  _viewTransition = true;
  if (typeof closePageSizePicker === 'function') closePageSizePicker();
  document.getElementById('page-print').style.display = 'none';
  // 当前子视图离场动画（对齐小程序 page-exit-right），280ms 后显示 me 页
  const currentView = [...document.querySelectorAll('.view')].find(v => v.style.display !== 'none');
  const mePage = document.getElementById('page-me');
  const finish = () => {
    hideViews();
    mePage.style.display = '';
    animatePageIn(mePage, 'page-enter-left');
    document.getElementById('navTitle').textContent = '我';
    document.getElementById('navBack').style.display = 'none';
    document.getElementById('tabBar').style.display = '';
    currentTab = 'me';
    _tabSwitching = false;
    updateThemeToggleVisibility();
    if (typeof loadMeTab === 'function') loadMeTab();
    measureAll();
    _viewTransition = false;
  };
  if (currentView) {
    currentView.classList.remove('page-exit-left', 'page-exit-right', 'page-fade-in', 'page-enter-right', 'page-enter-left');
    void currentView.offsetWidth;
    currentView.classList.add('page-exit-right');
    setTimeout(finish, 280);
  } else {
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
  const subVisible = [...document.querySelectorAll('.view')].some(v => v.style.display !== 'none');
  if (subVisible) {
    hideView();
    return true;
  }
  return false;
};

// 主题切换按钮仅"我"页使用（打印页/子视图隐藏，对齐小程序）
function updateThemeToggleVisibility() {
  const toggle = document.getElementById('themeToggle');
  if (!toggle) return;
  const printVisible = document.getElementById('page-print').style.display !== 'none';
  const subVisible = [...document.querySelectorAll('.view')].some(v => v.style.display !== 'none');
  toggle.style.display = (printVisible || subVisible) ? 'none' : '';
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
  const btn = document.getElementById('confirmOk');
  btn.textContent = confirmText || '确定';
  btn.style.background = confirmColor || '#FF3B30';
  _confirmCallback = cb;
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

const ORDER_STATUS_MAP = {
  queued: '排队中',
  printing: '待添加',
  accepted: '已添加',
  offline_unknown: '断线未知',
  sent: '已完成',
  failed: '失败',
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
    item.addEventListener('click', () => switchTab(item.dataset.tab));
  });
}

function initApp() {
  setupTabs();
  setupNav();
  setupPressFeedback();
  // 首屏淡入：对齐小程序 page-fade-in-delayed（延迟 0.5s 向上淡入，仅首次加载；切 tab 用各自转场类）
  setTimeout(() => {
    const pp = document.getElementById('page-print');
    if (pp) pp.classList.add('page-fade-in', 'page-fade-in-delayed');
  }, 80);
  positionThemeToggle();
  updateThemeToggleVisibility();
  window.addEventListener('resize', positionThemeToggle);
  window.addEventListener('resize', applyNativeSafeArea);
  applyTheme(state.themeMode, { skipServer: true });
  applyNativeSafeArea();
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (state.themeMode === 'auto') applyTheme('auto', { skipServer: true });
    });
  }
  initScrollEngines();
  checkPrinterStatus();
  setInterval(checkPrinterStatus, 30000);
  ensureLogin().then(() => refreshSession()).finally(() => {
    if (typeof initPrintPage === 'function') initPrintPage();
    if (typeof initMePage === 'function') initMePage();
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
