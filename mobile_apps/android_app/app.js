/* HN Cloud Print — Android App Core
 * 状态 / 请求封装(401 自动重登) / 主题(自动·浅色·深色) / 视图栈
 * 橡皮筋滚动引擎（移植小程序自定义滚动物理）/ 手势总线 / 页面转场
 */

const DEFAULT_BASE_URL = 'https://hn-space.cn';
const BASE_URL = localStorage.getItem('hn_base_url') || DEFAULT_BASE_URL;

const state = {
  token: localStorage.getItem('hn_token') || '',
  openid: localStorage.getItem('hn_openid') || '',
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
  localStorage.setItem('hn_token', token);
  localStorage.setItem('hn_openid', state.openid);
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

function applyTheme(mode, opts) {
  opts = opts || {};
  state.themeMode = mode;
  localStorage.setItem('hn_theme_mode', mode);
  const dark = effectiveDark(mode);
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

/* ================= 橡皮筋滚动引擎（移植小程序 index.js/me.js 物理） ================= */

class ScrollEngine {
  constructor(scrollerEl, contentEl, opts) {
    this.el = scrollerEl;
    this.content = contentEl;
    this.opts = opts || {};
    this.y = 0;
    this.minY = 0;
    this.maxY = 0;
    this.scrollerH = 0;
    this.contentH = 0;
    this.dampMax = 130;
    this.fric = 0.006;
    this.snapSpd = 0.32;
    this.trackId = null;
    this.lastY = 0;
    this.lastT = 0;
    this.moved = false;
    this.vel = 0;
    this.inDecel = false;
    this.handoff = false;
    this.tick = null;
    this.startX = 0;
    this.startY = 0;
    this.directionLocked = false;
    this.horizontalGesture = false;
    this._wheelTimer = null;
    this._measureTimer = null;
    this._destroyed = false;
    this.el.classList.add('engine-active');
    this._bind();
    this.measure();
    this._debouncedMeasure = () => { if (this._measureTimer) clearTimeout(this._measureTimer); this._measureTimer = setTimeout(() => { this._measureTimer = null; this.measure(); }, 120); };
    window.addEventListener('resize', this._debouncedMeasure);
  }

  destroy() {
    this._destroyed = true;
    this.cancel();
    window.removeEventListener('resize', this._debouncedMeasure);
    this.el.classList.remove('engine-active');
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
    const tabBar = document.getElementById('tabBar');
    const tabOverlay = tabBar ? tabBar.getBoundingClientRect().height + 16 : 60;
    this.maxY = Math.max(0, ch - vp + (this.opts.bottomPad || 20) + tabOverlay);
    if (this.y > this.maxY) this.snapBack();
  }

  onTouchStart(e) {
    // 记录手势起点是否在内层可滚动列表（文件列表）内：
    // 内层能滚时交给原生，到边界/内层不可滚时由引擎接管外层
    this._nestedScroll = (e.target && e.target.closest && e.target.closest('.file-list-scroll')) || null;
    const touches = e.touches || [];
    if (touches.length > 0) {
      this.startX = touches[0].clientX;
      this.startY = touches[0].clientY;
      this.directionLocked = false;
      this.horizontalGesture = false;
    }
    this.cancel();
    this.inDecel = false;
    this.handoff = false;
    this._touchStartY0 = this.y;
    const p = touches[0];
    if (!p) return;
    this.trackId = p.identifier;
    this.lastY = p.clientY;
    this.lastT = Date.now();
    this.vel = 0;
    this.moved = false;
  }

  onTouchMove(e) {
    if (this.trackId === null) return;
    const touches = e.touches || [];
    if (touches.length === 0) return;
    // 方向锁定：横向手势（左滑删除）让出垂直滚动
    const dx = touches[0].clientX - this.startX;
    const dy = touches[0].clientY - this.startY;
    if (!this.directionLocked) {
      if (Math.abs(dx) > 5 || Math.abs(dy) > 5) {
        this.directionLocked = true;
        if (Math.abs(dx) > Math.abs(dy)) { this.horizontalGesture = true; gestureBus.horizontal = true; return; }
      } else {
        return;
      }
    }
    if (this.horizontalGesture || gestureBus.horizontal) return;
    const cur = touches[0];
    const now = Date.now();
    const ddy = cur.clientY - this.lastY;
    // 嵌套滚动：内层列表可继续滚时让给原生，到边界后引擎接管外层
    if (this._nestedScroll) {
      const el = this._nestedScroll;
      const maxTop = el.scrollHeight - el.clientHeight;
      const canScrollDown = ddy < 0 && el.scrollTop < maxTop - 0.5;
      const canScrollUp = ddy > 0 && el.scrollTop > 0.5;
      if (canScrollDown || canScrollUp) {
        this.lastY = cur.clientY;
        this.lastT = now;
        return;
      }
      this._nestedScroll = null; // 内层到边界 → 引擎接管
    }
    const dt = Math.max(1, now - this.lastT);
    if (Math.abs(ddy) > 0.5) this.moved = true;
    this.y -= ddy;
    const inst = -ddy / dt;
    this.vel = this.vel * 0.6 + inst * 0.4;
    this.lastY = cur.clientY;
    this.lastT = now;
    e.preventDefault();
    this.applyY();
  }

  onTouchEnd() {
    this._nestedScroll = null;
    gestureBus.horizontal = false;
    this.horizontalGesture = false;
    this.directionLocked = false;
    this.trackId = null;
    this.handoff = false;
    // 点击（几乎无位移/未真正滚动）不启动滚动物理：
    // 模拟触摸下轻微抖动会进入减速帧并触发 onScroll，把刚展开的 UI（如下拉）立刻收起
    const scrolled = this.moved && Math.abs(this.y - this._touchStartY0) > 2;
    if (!scrolled) { this.vel = 0; return; }
    this.startPhysics();
  }

  onWheel(e) {
    const nested = e.target && e.target.closest && e.target.closest('.file-list-scroll');
    if (nested) {
      const maxTop = nested.scrollHeight - nested.clientHeight;
      const atBottom = nested.scrollTop >= maxTop - 0.5;
      const atTop = nested.scrollTop <= 0.5;
      if ((e.deltaY > 0 && !atBottom) || (e.deltaY < 0 && !atTop)) return; // 内层可滚，交给原生
      // 内层到边界 → 引擎接管外层
    }
    if (gestureBus.horizontal) return;
    e.preventDefault();
    this.cancel();
    this.inDecel = false;
    this.y += e.deltaY;
    this.applyY();
    // 停止滚动后平滑回弹
    if (this._wheelTimer) clearTimeout(this._wheelTimer);
    this._wheelTimer = setTimeout(() => this.startPhysics(), 140);
  }

  dampShift(d) {
    const max = this.dampMax;
    const sign = d >= 0 ? 1 : -1;
    return sign * max * (1 - Math.exp(-Math.abs(d) / (max * 1.6)));
  }

  renderY() {
    const y = this.y;
    if (y < this.minY) return this.minY - this.dampShift(this.minY - y);
    if (y > this.maxY) return this.maxY + this.dampShift(y - this.maxY);
    return y;
  }

  applyY() {
    const real = Math.max(0, Math.min(this.y, this.maxY));
    const ratio = this.maxY > 0 ? Math.min(real / 400, 1) : 0;
    this.content.style.transform = 'translateY(' + (-this.renderY()) + 'px)';
    if (typeof this.opts.onScroll === 'function') this.opts.onScroll(real, ratio);
  }

  startPhysics() {
    this.cancel();
    if (this.y < this.minY || this.y > this.maxY) { this.vel = 0; this.snapBack(); return; }
    if (Math.abs(this.vel) < 0.05) { this.snapBack(); return; }
    this.inDecel = true;
    this.lastT = Date.now();
    const tick = () => {
      if (!this.inDecel || this._destroyed) return;
      const now = Date.now();
      const dt = Math.max(1, now - this.lastT);
      this.lastT = now;
      this.vel *= Math.exp(-this.fric * dt);
      this.y += this.vel * dt;
      if (this.y < this.minY || this.y > this.maxY) {
        this.y = Math.max(this.minY, Math.min(this.maxY, this.y));
        this.vel = 0;
        this.inDecel = false;
        this.snapBack();
        return;
      }
      if (Math.abs(this.vel) < 0.02) { this.inDecel = false; this.applyY(); return; }
      this.applyY();
      this.tick = setTimeout(tick, 16);
    };
    this.tick = setTimeout(tick, 16);
  }

  snapBack() {
    this.cancel();
    const tick = () => {
      if (this._destroyed) return;
      let target = this.y;
      if (this.y < this.minY) target = this.minY;
      else if (this.y > this.maxY) target = this.maxY;
      else { this.y = target; this.applyY(); this.tick = null; return; }
      this.y += (target - this.y) * this.snapSpd;
      if (Math.abs(this.y - target) < 0.3) { this.y = target; this.applyY(); this.tick = null; return; }
      this.applyY();
      this.tick = setTimeout(tick, 16);
    };
    this.tick = setTimeout(tick, 16);
  }

  cancel() {
    if (this.tick) { clearTimeout(this.tick); this.tick = null; }
  }

  scrollTo(targetY, duration) {
    this.cancel();
    this.inDecel = false;
    const startY = this.y;
    const diff = targetY - startY;
    if (Math.abs(diff) < 1) { this.y = targetY; this.applyY(); return; }
    const startT = Date.now();
    const tick = () => {
      const p = Math.min(1, (Date.now() - startT) / (duration || 280));
      const eased = 1 - Math.pow(1 - p, 3);
      this.y = startY + diff * eased;
      this.applyY();
      if (p < 1) this.tick = setTimeout(tick, 16);
      else this.tick = null;
    };
    this.tick = setTimeout(tick, 16);
  }

  scheduleMeasure(delay) {
    if (this._measureTimer) clearTimeout(this._measureTimer);
    this._measureTimer = setTimeout(() => { this._measureTimer = null; this.measure(); }, delay || 120);
  }
}

const scrollEngines = {};

function initScrollEngines() {
  const printScroller = document.getElementById('scroller-print');
  const printContent = document.getElementById('scrollContentPrint');
  if (printScroller && printContent && !scrollEngines.print) {
    scrollEngines.print = new ScrollEngine(printScroller, printContent, {
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
    // 初始化头部基线样式（对齐小程序首帧即 40rpx）：首次交互触发 scroll 事件前就位，
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
    scrollEngines.me = new ScrollEngine(meScroller, meContent, {
      onScroll(real) {
        const btn = document.getElementById('scrollTopBtn');
        if (btn) btn.classList.toggle('scroll-top-visible', real > 200);
        // 滚动即收起分页下拉（对齐小程序：避免 fixed 下拉错位）
        if (typeof closePageSizePicker === 'function') closePageSizePicker();
      },
    });
  }
}

function measureAll(delay) {
  Object.keys(scrollEngines).forEach(k => {
    const engine = scrollEngines[k];
    // 页面隐藏（display:none）时不测量：隐藏态高度为 0 会重算 maxY 并把滚动位置 clamp 掉
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
  // me 页离场动画（对齐小程序 page-exit-left），240ms 后切换子视图
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
  }, 240);
}

function hideView() {
  if (_viewTransition) return;
  _viewTransition = true;
  if (typeof closePageSizePicker === 'function') closePageSizePicker();
  document.getElementById('page-print').style.display = 'none';
  // 当前子视图离场动画（对齐小程序 page-exit-right），240ms 后显示 me 页
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
    setTimeout(finish, 240);
  } else {
    finish();
  }
}

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
  }, 180);
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
  const m = Math.floor(remain / 60000);
  const s = Math.floor((remain % 60000) / 1000);
  return m + '分' + (s < 10 ? '0' : '') + s + '秒';
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
    '.duplex-opt', '.img-ori-opt', '.key-type-option', '.license-badge', '.key-delete',
    '.admin-delete-btn', '.stepper-btn', '.avatar-btn',
  ].join(',');
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
  positionThemeToggle();
  updateThemeToggleVisibility();
  window.addEventListener('resize', positionThemeToggle);
  applyTheme(state.themeMode, { skipServer: true });
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
