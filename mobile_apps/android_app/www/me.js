/* HN Cloud Print — Android App 个人中心 / 订单 / 管理员（与小程序 me 页视觉对齐） */

const DEFAULT_AVATAR = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='108' height='108'%3E%3Ccircle cx='54' cy='54' r='54' fill='%23E5E5EA'/%3E%3Ccircle cx='54' cy='44' r='18' fill='%23C7C7CC'/%3E%3Cellipse cx='54' cy='80' rx='32' ry='22' fill='%23C7C7CC'/%3E%3C/svg%3E";

const SECURITY_DEFS = [
  { key: 'user_quota_mb', label: '每用户存储配额', unit: 'MB', min: 0, max: 102400, hint: '超限拒绝上传，0=不限' },
  { key: 'disk_min_free_mb', label: '磁盘最低剩余', unit: 'MB', min: 0, max: 102400, hint: '低于则拒绝上传，0=不检查' },
  { key: 'queued_timeout_hours', label: '排队订单超时', unit: '小时', min: 0, max: 720, hint: '超时自动取消，0=不过期' },
  { key: 'upload_rate_limit', label: '上传频率上限', unit: '次/分', min: 1, max: 600, hint: '每用户每分钟' },
  { key: 'submit_order_rate_limit', label: '提交频率上限', unit: '次/分', min: 1, max: 600, hint: '每用户每分钟' },
  { key: 'device_login_rate_limit', label: '设备注册上限', unit: '次/时', min: 1, max: 600, hint: '每IP/每设备每小时' },
  { key: 'redeem_rate_limit', label: '密钥兑换上限', unit: '次/分', min: 1, max: 600, hint: '每用户每分钟' },
  { key: 'log_report_rate_limit', label: '日志上报上限', unit: '条/分', min: 1, max: 600, hint: '每IP每分钟' },
];

const _adminLoadTimes = { keys: 0, tempUsers: 0, storage: 0, security: 0, admins: 0 };
let _lastRoleForAdminLoad = null;

// 角色进入 admin 时重置管理员模块加载时间戳：
// 游客/普通用户直升管理员后，保证 5 个管理员模块立即重新拉取（而不是被 30s 防抖吞掉）。
function adminLoadsNeedReset(role) {
  if (role === 'admin' && _lastRoleForAdminLoad !== 'admin') {
    Object.keys(_adminLoadTimes).forEach(k => { _adminLoadTimes[k] = 0; });
  }
  _lastRoleForAdminLoad = role;
}

function loadIfStale(key, fn, ttl) {
  const now = Date.now();
  if (now - _adminLoadTimes[key] > (ttl || 30000)) {
    _adminLoadTimes[key] = now;
    const p = fn();
    // 拉取失败（返回 false 或抛错）时清空时间戳，下次刷新立即重试，
    // 避免“首次失败后被防抖抑制，界面停留在 -- 直到重启 APP”。
    if (p && typeof p.then === 'function') {
      p.then(ok => { if (ok === false) _adminLoadTimes[key] = 0; })
       .catch(() => { _adminLoadTimes[key] = 0; });
    }
  }
}

const meState = {
  orders: [],
  ordersPage: 1,
  ordersPerPage: 10,
  ordersTotal: 0,
  ordersTotalPages: 0,
  ordersLoadError: '',
  expandedOrders: {},
  showLicenseDetail: false,
  loadingOrders: false,
  _lastDataLoad: 0,
  _orderPollTimer: null,
  _keyPollTimer: null,
  _tempCountdownTimer: null,
  activeKeys: [],
  licenseMinutes: 1,
  keyType: 'temp',
  generating: false,
  tempUsers: [],
  admins: [],
  storageStats: null,
  retentionDays: 7,
  retentionHours: 0,
  savingRetention: false,
  deletingAllFiles: false,
  securityItems: [],
  securityConfig: null,
  savingSecurity: false,
  securityExpanded: false,
  authorizedUsers: [],
  bindDevices: [],
  bindDevicesLoading: false,
  userOrdersView: { openid: '', nickname: '', source: '', orders: [], page: 1, perPage: 10, total: 0, totalPages: 0, expanded: {} },
};

/* ================= 初始化 / Tab 刷新 ================= */

function initMePage() {
  setupMeButtons();
  refreshMeUI();
  loadOrders();
}

function loadMeTab() {
  refreshMeUI();
  const now = Date.now();
  if (!meState._lastDataLoad || (now - meState._lastDataLoad) > 60000) {
    meState._lastDataLoad = now;
    loadOrders();
    loadBindDevices();
    if (state.role === 'admin') {
      loadIfStale('keys', loadActiveKeys);
      loadIfStale('tempUsers', loadTempUsers);
      loadIfStale('storage', loadStorageStats);
      loadIfStale('security', loadSecurityConfig);
      if (state.isSuperAdmin) loadIfStale('admins', loadAdmins);
    }
  }
  startOrderPolling();
  startKeyPolling();
  measureAll(150);
}

function refreshMeUI() {
  adminLoadsNeedReset(state.role);
  const roleLabel = state.isSuperAdmin ? '超级管理员' : state.role === 'admin' ? '管理员' : state.role === 'user' ? '普通用户' : '访客';
  document.getElementById('roleLabel').textContent = roleLabel;
  const nickText = document.getElementById('nicknameText');
  if (nickText) nickText.textContent = state.nickname || '点击设置昵称';
  setAvatarImg(state.avatarUrl);
  const badge = document.getElementById('licenseBadge');
  const detail = document.getElementById('licenseDetail');
  const li = state.licenseInfo;
  if (li) {
    badge.style.display = '';
    const permanent = state.role === 'admin' || li.type === 'admin';
    badge.classList.toggle('license-badge-admin', permanent);
    badge.classList.toggle('license-badge-temp', !permanent && !li.expired);
    badge.classList.toggle('license-badge-expired', !permanent && li.expired);
    document.getElementById('licenseBadgeKey').textContent = li.key;
    document.getElementById('licenseBadgeValidity').textContent =
      permanent ? '永久' : (li.expired ? '已过期' : (formatTempRemain(li.expires_at) || '--'));
    document.getElementById('licenseDetailStatus').textContent =
      permanent ? '永久' : (li.expired ? '已过期' : '有效');
    document.getElementById('licenseDetailKey').textContent = li.key || '—';
    document.getElementById('licenseDetailType').textContent = li.type === 'admin' ? '管理员许可' : '临时许可';
    document.getElementById('licenseDetailCreator').textContent = li.creator_nickname || '—';
    document.getElementById('licenseDetailCreated').textContent = li.created_at || '—';
    document.getElementById('licenseDetailUsed').textContent = li.used_at || '—';
    document.getElementById('licenseDetailExpires').textContent = li.expires_at || '—';
    document.getElementById('licenseDetailValidity').textContent = li.validity_minutes ? li.validity_minutes + ' 分钟' : '—';
  } else {
    badge.style.display = 'none';
    detail.classList.remove('license-detail-expanded');
    detail.style.maxHeight = '';
    detail.style.opacity = '';
    detail.style.padding = '';
  }
  document.getElementById('guestSection').style.display = state.role === 'guest' ? '' : 'none';
  // 账号绑定入口副标题：与小程序同语义——显示已绑定设备数（管理界面在独立子视图）
  const bindEntry = document.getElementById('bindEntryDesc');
  if (bindEntry) {
    const bindCount = (meState.bindDevices || []).length;
    bindEntry.textContent = bindCount > 0 ? ('已绑定 ' + bindCount + ' 台设备') : '管理微信小程序与 APP 的账号绑定';
  }
  updateAdminCollapsed();
  // 普通用户：打印许可卡片（对齐小程序）
  const userLic = document.getElementById('userLicenseSection');
  if (userLic) userLic.style.display = state.role === 'user' ? '' : 'none';
  const userLicDetail = document.getElementById('userLicenseDetail');
  if (state.role === 'user' && li) {
    if (userLicDetail) userLicDetail.style.display = '';
    document.getElementById('userLicenseKey').textContent = li.key || '—';
    document.getElementById('userLicenseCreator').textContent = li.creator_nickname || '—';
  } else if (userLicDetail) {
    userLicDetail.style.display = 'none';
  }
  manageTempCountdown();
  updateRoleActions();
  if (state.role === 'admin') {
    loadIfStale('keys', loadActiveKeys);
    loadIfStale('tempUsers', loadTempUsers);
    loadIfStale('storage', loadStorageStats);
    loadIfStale('security', loadSecurityConfig);
    if (state.isSuperAdmin) loadIfStale('admins', loadAdmins);
  }
  measureAll(150);
}

function updateRoleActions() {
  const admin = state.role === 'admin';
  document.getElementById('btnAuthorizedUsers').style.display = admin ? '' : 'none';
  document.getElementById('btnLocalOrders').style.display = admin ? '' : 'none';
  document.getElementById('adminsBlock').style.display = state.isSuperAdmin ? '' : 'none';
  document.getElementById('tempUsersBlock').style.display = admin ? '' : 'none';
  document.getElementById('keyTypeRow').style.display = state.isSuperAdmin ? '' : 'none';
  updateKeyForm();
}

// 管理员区块展开/收起（对齐小程序 admin-collapsible 过渡）
// 存储/防滥用区块：数据到达后才展开（小程序 storageStats/securityConfig 到位才加 admin-expanded）
function updateAdminCollapsed() {
  const isAdmin = state.role === 'admin';
  const setBlock = (id, on) => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('admin-expanded', on);
  };
  setBlock('adminBlockKeys', isAdmin);
  setBlock('adminBlockStorage', isAdmin && !!meState.storageStats);
  setBlock('adminBlockSecurity', isAdmin && !!meState.securityConfig);
  setBlock('adminBlockAuth', isAdmin);
  setBlock('adminBlockLocal', isAdmin || !!state.isSuperAdmin);
  measureAll(150);
}

// 许可密钥详情展开/收起（对齐小程序：license-detail-expanded 类 + CSS 过渡，纯类切换无延迟）
function toggleLicenseDetail(detailId, open) {
  const detail = document.getElementById(detailId);
  if (!detail) return;
  detail.classList.toggle('license-detail-expanded', open);
  measureAll(150);
  // 0.32s 过渡结束后再次测量（收起后内容变短，滚动边界收缩）
  setTimeout(() => measureAll(320), 340);
}

// 临时授权剩余时间（对齐小程序 tempCountdownText："剩余 X 分 XX 秒"）
function formatTempRemain(str) {
  if (!str) return '';
  const t = new Date(String(str).replace(/-/g, '/')).getTime();
  if (isNaN(t)) return str;
  const remain = t - Date.now();
  if (remain <= 0) return '已过期';
  const m = Math.floor(remain / 60000);
  const s = Math.floor((remain % 60000) / 1000);
  return '剩余 ' + m + ' 分 ' + (s < 10 ? '0' + s : s) + ' 秒';
}

function updateTempCountdownTexts() {
  const li = state.licenseInfo;
  if (!li || state.role !== 'user' || li.type === 'admin') return;
  const txt = formatTempRemain(li.expires_at);
  const badgeVal = document.getElementById('licenseBadgeValidity');
  if (badgeVal && li.type !== 'admin') badgeVal.textContent = li.expired ? '已过期' : (txt || '--');
  const el = document.getElementById('userLicenseCountdown');
  if (el) el.textContent = txt;
}

function manageTempCountdown() {
  stopTempCountdown();
  const li = state.licenseInfo;
  if (state.role !== 'user' || !li || li.type === 'admin' || li.expired) return;
  updateTempCountdownTexts();
  meState._tempCountdownTimer = setInterval(updateTempCountdownTexts, 1000);
}

function stopTempCountdown() {
  if (meState._tempCountdownTimer) {
    clearInterval(meState._tempCountdownTimer);
    meState._tempCountdownTimer = null;
  }
}

/* ================= 控件 ================= */

function setupMeButtons() {
  document.getElementById('avatarBtn').addEventListener('click', () => document.getElementById('avatarInput').click());
  document.getElementById('avatarInput').addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (file) uploadAvatar(file);
    e.target.value = '';
  });
  document.getElementById('nicknameRow').addEventListener('click', openNicknameModal);
  document.getElementById('nicknameModalCancel').addEventListener('click', () => closeModal('nicknameModal'));
  document.getElementById('nicknameModalSave').addEventListener('click', saveNicknameFromModal);
  document.getElementById('nicknameModalInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') saveNicknameFromModal();
  });
  document.getElementById('deviceRenameCancel').addEventListener('click', () => closeModal('deviceRenameModal'));
  document.getElementById('deviceRenameSave').addEventListener('click', saveDeviceRename);
  document.getElementById('deviceRenameInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); saveDeviceRename(); }
  });
  document.getElementById('redeemBtn').addEventListener('click', redeemKey);
  document.getElementById('redeemPasteBtn').addEventListener('click', pasteKey);
  document.getElementById('bindBtn').addEventListener('click', bindWechatAccount);
  document.getElementById('bindPasteBtn').addEventListener('click', pasteBindKey);
  document.getElementById('unbindBtn').addEventListener('click', unbindWechatAccount);
  document.getElementById('licenseMinutesMinus').addEventListener('click', () => setLicenseMinutes(meState.licenseMinutes - 1));
  document.getElementById('licenseMinutesPlus').addEventListener('click', () => setLicenseMinutes(meState.licenseMinutes + 1));
  const keyTypeClick = (type) => {
    const toggle = document.querySelector('.key-type-toggle');
    if (toggle && toggle._dragHandled && Date.now() - toggle._dragHandled < 500) { toggle._dragHandled = 0; return; }
    meState.keyType = type;
    updateKeyForm();
  };
  document.getElementById('keyTypeTemp').addEventListener('click', () => keyTypeClick('temp'));
  document.getElementById('keyTypeAdmin').addEventListener('click', () => keyTypeClick('admin'));
  bindKeyTypeDrag();
  document.getElementById('generateKeyBtn').addEventListener('click', generateKey);
  document.getElementById('retentionDaysMinus').addEventListener('click', () => setRetention('days', meState.retentionDays - 1));
  document.getElementById('retentionDaysPlus').addEventListener('click', () => setRetention('days', meState.retentionDays + 1));
  document.getElementById('retentionHoursMinus').addEventListener('click', () => setRetention('hours', meState.retentionHours - 1));
  document.getElementById('retentionHoursPlus').addEventListener('click', () => setRetention('hours', meState.retentionHours + 1));
  document.getElementById('saveRetentionBtn').addEventListener('click', saveRetention);
  document.getElementById('deleteAllFilesBtn').addEventListener('click', deleteAllFiles);
  document.getElementById('saveSecurityBtn').addEventListener('click', saveSecurity);
  document.getElementById('securitySummary').addEventListener('click', toggleSecurityExpanded);
  document.getElementById('securityItems').addEventListener('click', (e) => {
    const minus = e.target.closest('[data-sec-minus]');
    const plus = e.target.closest('[data-sec-plus]');
    if (minus) updateSecurityItem(parseInt(minus.dataset.secMinus, 10), -1);
    if (plus) updateSecurityItem(parseInt(plus.dataset.secPlus, 10), 1);
  });
  document.getElementById('securityItems').addEventListener('change', (e) => {
    const t = e.target;
    if (t.dataset.secInput != null) {
      const idx = parseInt(t.dataset.secInput, 10);
      const it = meState.securityItems[idx];
      if (!it) return;
      let v = parseInt(t.value, 10);
      if (isNaN(v)) v = it.min;
      v = Math.max(it.min, Math.min(it.max, v));
      it.value = v;
      renderSecurityConfig();
    }
  });
  document.getElementById('ordersPrev').addEventListener('click', () => changeOrdersPage(meState.ordersPage - 1));
  document.getElementById('ordersNext').addEventListener('click', () => changeOrdersPage(meState.ordersPage + 1));
  document.getElementById('ordersPrevBottom').addEventListener('click', () => changeOrdersPage(meState.ordersPage - 1));
  document.getElementById('ordersNextBottom').addEventListener('click', () => changeOrdersPage(meState.ordersPage + 1));
  document.getElementById('ordersPageNumbersBottom').addEventListener('click', (e) => {
    const num = e.target.closest('[data-page-num]');
    if (num && num.dataset.pageNum !== '...') changeOrdersPage(parseInt(num.dataset.pageNum, 10));
  });
  // 条/页下拉：document 级委托（下拉打开时被移到 body 末尾逃出变换层，触发器与选项分开处理）
  document.addEventListener('click', (e) => {
    // 子视图（本地打印任务/用户订单）每页条数下拉
    const uoOpt = e.target.closest('#uoPageSizeDropdown [data-size]');
    if (uoOpt) { selectUoPageSize(parseInt(uoOpt.dataset.size, 10)); return; }
    const uoSel = e.target.closest('#uoPageSizeSelector');
    if (uoSel) { toggleUoPageSizePicker(); return; }
    if (meState.userOrdersView && meState.userOrdersView.showPageSizePicker) closeUoPageSizePicker();
    // "我的打印任务"每页条数下拉（下拉已移出 #ordersPageSize，选项按 id 定位）
    const picker = document.getElementById('pageSizePicker');
    const isOpen = picker && picker.classList.contains('dropdown-show');
    const opt = e.target.closest('#pageSizePicker [data-size]');
    if (opt) { selectOrdersPageSize(parseInt(opt.dataset.size, 10)); return; }
    const sel = e.target.closest('#ordersPageSize');
    if (!sel) {
      // 点击页面其他区域：收起下拉
      if (isOpen) hidePageSizePicker();
      return;
    }
    togglePageSizePicker();
  });
  document.getElementById('orderList').addEventListener('click', (e) => {
    const cancel = e.target.closest('[data-cancel-order]');
    if (cancel) { e.stopPropagation(); cancelOrder(parseInt(cancel.dataset.cancelOrder, 10)); return; }
    const card = e.target.closest('[data-order-id]');
    if (card) toggleOrder(parseInt(card.dataset.orderId, 10));
  });
  document.getElementById('ordersPageNumbers').addEventListener('click', (e) => {
    const num = e.target.closest('[data-page-num]');
    if (num && num.dataset.pageNum !== '...') changeOrdersPage(parseInt(num.dataset.pageNum, 10));
  });
  document.getElementById('btnAuthorizedUsers').addEventListener('click', openAuthorizedUsers);
  document.getElementById('btnBindAccount').addEventListener('click', openBindView);
  const btnCheckUpdate = document.getElementById('btnCheckUpdate');
  if (btnCheckUpdate) btnCheckUpdate.addEventListener('click', () => checkAppUpdate(true));
  document.getElementById('btnLocalOrders').addEventListener('click', () => openUserOrdersView({ source: 'local', nickname: '本地打印任务' }));
  document.getElementById('adminList').addEventListener('click', (e) => {
    const rm = e.target.closest('[data-remove-admin]');
    if (rm) { e.stopPropagation(); removeAdmin(rm.dataset.removeAdmin); return; }
    // 对齐小程序：点击管理员卡片 → 打开该管理员的任务列表（顶部用户卡片 + 许可密钥徽章）
    const card = e.target.closest('[data-admin-openid]');
    if (card) {
      // 左滑后松手会补发 click，用时间戳避免误跳转（对齐小程序"纯点击才跳转"）
      if (card.dataset.swiped && Date.now() - Number(card.dataset.swiped) < 500) return;
      openUserOrdersView({ openid: card.dataset.adminOpenid, nickname: card.dataset.adminNickname });
    }
  });
  document.getElementById('tempUserList').addEventListener('click', (e) => {
    const rm = e.target.closest('[data-remove-tempuser]');
    if (rm) removeTempUser(rm.dataset.removeTempuser);
  });
  document.getElementById('activeKeys').addEventListener('click', (e) => {
    const copy = e.target.closest('[data-copy-key]');
    if (copy) copyKey(copy.dataset.copyKey);
    const revoke = e.target.closest('[data-revoke-key]');
    if (revoke) {
      // 触摸已由 pointerup 处理（点击可能被浏览器吞掉），此处只防重复
      if (revoke.dataset.tapHandled === '1') { delete revoke.dataset.tapHandled; return; }
      revokeKey(revoke.dataset.revokeKey);
    }
    const confirmKey = e.target.closest('[data-confirm-key]');
    if (confirmKey) confirmAdminKey(confirmKey.dataset.confirmKey);
    const settle = e.target.closest('[data-settle]');
    if (settle) settleOrder(settle.dataset.settle, settle.dataset.settleNickname);
  });
  // 触摸/笔直接触发作废：pointerup 一定触发，click 在触摸后可能被吞
  document.getElementById('activeKeys').addEventListener('pointerup', (e) => {
    if (e.pointerType === 'mouse') return;
    const del = e.target.closest('[data-revoke-key]');
    if (!del) return;
    del.dataset.tapHandled = '1';
    setTimeout(() => { delete del.dataset.tapHandled; }, 600);
    // 延迟到合成 click 派发之后再弹窗：若立即弹窗，遮罩会盖住点击点，
    // 随后的 click 落在遮罩上触发 closeModal，弹窗被秒关
    setTimeout(() => revokeKey(del.dataset.revokeKey), 30);
  });
  document.getElementById('authorizedUserList').addEventListener('click', (e) => {
    const orderToggle = e.target.closest('[data-key-order-toggle]');
    if (orderToggle) { toggleRecordOrders(orderToggle.dataset.userOpenid, orderToggle.dataset.keyOrderToggle); return; }
    const toggle = e.target.closest('[data-toggle-records]');
    if (toggle) { toggleAuthorizedUser(toggle.dataset.toggleRecords); return; }
    const card = e.target.closest('[data-user-openid]');
    if (card) openUserOrdersView({ openid: card.dataset.userOpenid, nickname: card.dataset.userNickname });
  });
  document.getElementById('userOrdersPrev').addEventListener('click', () => changeUserOrdersPage(meState.userOrdersView.page - 1));
  document.getElementById('userOrdersNext').addEventListener('click', () => changeUserOrdersPage(meState.userOrdersView.page + 1));
  document.getElementById('userOrdersPrevBottom').addEventListener('click', () => changeUserOrdersPage(meState.userOrdersView.page - 1));
  document.getElementById('userOrdersNextBottom').addEventListener('click', () => changeUserOrdersPage(meState.userOrdersView.page + 1));
  document.getElementById('userOrdersPageNumbers').addEventListener('click', (e) => {
    const num = e.target.closest('[data-page-num]');
    if (num && num.dataset.pageNum !== '...') changeUserOrdersPage(parseInt(num.dataset.pageNum, 10));
  });
  document.getElementById('userOrdersPageNumbersBottom').addEventListener('click', (e) => {
    const num = e.target.closest('[data-page-num]');
    if (num && num.dataset.pageNum !== '...') changeUserOrdersPage(parseInt(num.dataset.pageNum, 10));
  });
  // 子视图条/页下拉：触发器与选项均走 document 委托（见上方委托），此处不再重复绑定
  document.getElementById('userOrdersList').addEventListener('click', (e) => {
    const cancel = e.target.closest('[data-cancel-order]');
    if (cancel) { e.stopPropagation(); cancelOrder(parseInt(cancel.dataset.cancelOrder, 10)); return; }
    const card = e.target.closest('[data-order-id]');
    if (card) toggleUserOrdersOrder(parseInt(card.dataset.orderId, 10));
  });
  document.getElementById('scrollTopBtn').addEventListener('click', () => {
    if (scrollEngines.me) scrollEngines.me.scrollTo(0, 300);
  });
  // 许可密钥徽章：document 级委托（对齐小程序：点击展开/收起密钥详情）
  document.addEventListener('click', (e) => {
    const badge = e.target.closest('#licenseBadge');
    if (badge) {
      meState.showLicenseDetail = !meState.showLicenseDetail;
      toggleLicenseDetail('licenseDetail', meState.showLicenseDetail);
      return;
    }
    const uoBadge = e.target.closest('#uoLicenseBadge');
    if (uoBadge) {
      const v = meState.userOrdersView;
      v.showLicenseDetail = !v.showLicenseDetail;
      toggleLicenseDetail('uoLicenseDetail', v.showLicenseDetail);
    }
  });
}

/* ================= 头像 / 昵称 ================= */

// 默认头像（与 index.html 中内联 SVG data URI 一致，加载失败/无头像时兜底）
const DEFAULT_AVATAR_SRC = "data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 108 108'%3E%3Ccircle cx='54' cy='54' r='54' fill='%23E5E5EA'/%3E%3Ccircle cx='54' cy='44' r='18' fill='%23C7C7CC'/%3E%3Cellipse cx='54' cy='80' rx='32' ry='22' fill='%23C7C7CC'/%3E%3C/svg%3E";

// 统一头像设置入口：有头像显示真实图片（加时间戳防缓存），无头像回退默认图
function setAvatarImg(url) {
  const avatar = document.getElementById('avatarImg');
  if (!avatar) return;
  if (url) {
    avatar.onerror = function () { this.onerror = null; this.src = DEFAULT_AVATAR_SRC; };
    avatar.src = url + (url.indexOf('?') >= 0 ? '&t=' : '?t=') + Date.now();
  } else {
    avatar.onerror = null;
    avatar.src = DEFAULT_AVATAR_SRC;
  }
}

function openNicknameModal() {
  const input = document.getElementById('nicknameModalInput');
  input.value = state.nickname || '';
  openModal('nicknameModal');
  setTimeout(() => input.focus(), 200);
}

function saveNicknameFromModal() {
  const val = document.getElementById('nicknameModalInput').value.trim();
  if (!val) { showToast('请输入昵称'); return; }
  closeModal('nicknameModal');
  if (state.token) saveNickname(val);
  else {
    state.nickname = val;
    localStorage.setItem('hn_nickname', val);
    refreshMeUI();
  }
}

async function uploadAvatar(file) {
  if (!state.token) { showToast('请先登录'); return; }
  const fd = new FormData();
  fd.append('avatar', file);
  fd.append('nickname', state.nickname || '');
  try {
    const xhr = new XMLHttpRequest();
    const result = await new Promise((resolve) => {
      xhr.open('POST', BASE_URL + '/api/profile');
      xhr.setRequestHeader('Authorization', 'Bearer ' + state.token);
      xhr.onload = () => resolve({ status: xhr.status, data: (() => { try { return JSON.parse(xhr.responseText); } catch (e) { return null; } })() });
      xhr.onerror = () => resolve({ status: 0, data: null });
      xhr.send(fd);
    });
    if (result.data && result.data.success && result.data.avatar_url) {
      state.avatarUrl = result.data.avatar_url;
      localStorage.setItem('hn_avatar', state.avatarUrl);
      setAvatarImg(state.avatarUrl);
      showToast('头像已更新');
    } else showToast((result.data && result.data.message) || '上传失败');
  } catch (e) { showToast('网络错误'); }
}

function saveNickname(nickname) {
  state.nickname = nickname;
  localStorage.setItem('hn_nickname', nickname);
  // 立即刷新界面显示，避免等切页/会话刷新才更新
  refreshMeUI();
  api('/api/profile', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ nickname }),
  }).catch(() => {});
}

/* ================= 许可密钥（访客兑换） ================= */

function redeemKey() {
  const key = document.getElementById('redeemInput').value.trim().toUpperCase();
  if (key.length !== 8) { showToast('密钥为8位字符'); return; }
  const btn = document.getElementById('redeemBtn');
  btn.disabled = true;
  btn.textContent = '验证中…';
  api('/api/license/redeem', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key }),
  }).then(r => {
    btn.disabled = false;
    btn.textContent = '验证';
    if (r.data && r.data.success) {
      showToast('许可验证成功！');
      document.getElementById('redeemInput').value = '';
      refreshSession();
      loadOrders();
    } else showToast((r.data && r.data.message) || '密钥无效');
  }).catch(() => { btn.disabled = false; btn.textContent = '验证'; showToast('网络错误'); });
}

function pasteKey() {
  readClipboard().then(text => {
    const t = (text || '').trim();
    if (!t) { showToast('剪贴板为空'); return; }
    const m = t.match(/[A-Za-z0-9]{8}/);
    document.getElementById('redeemInput').value = m ? m[0].toUpperCase() : t.slice(0, 8).toUpperCase();
  }).catch(() => showToast('无法读取剪贴板'));
}

/* ================= 微信账号绑定（个人认证密钥） ================= */

function bindWechatAccount() {
  const key = document.getElementById('bindInput').value.trim().toUpperCase();
  if (key.length !== 8) { showToast('密钥为8位字符'); return; }
  const btn = document.getElementById('bindBtn');
  btn.disabled = true;
  btn.textContent = '绑定中…';
  api('/api/bind/redeem', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key }),
  }).then(r => {
    btn.disabled = false;
    btn.textContent = '绑定';
    if (r.data && r.data.success) {
      // 记录设备账号（升级/重装场景兜底：后端 redeem 返回 dev_openid）
      if (r.data.dev_openid) {
        state.devOpenid = r.data.dev_openid;
        localStorage.setItem('hn_dev_openid', r.data.dev_openid);
      }
      setAuth(r.data.token, r.data.openid);
      showToast('绑定成功');
      document.getElementById('bindInput').value = '';
      refreshSession();
      loadOrders();
      renderBindView();
      loadBindDevices();
    } else showToast((r.data && r.data.message) || '绑定失败');
  }).catch(() => { btn.disabled = false; btn.textContent = '绑定'; showToast('网络错误'); });
}

function pasteBindKey() {
  readClipboard().then(text => {
    const t = (text || '').trim();
    if (!t) { showToast('剪贴板为空'); return; }
    const m = t.match(/[A-Za-z0-9]{8}/);
    document.getElementById('bindInput').value = m ? m[0].toUpperCase() : t.slice(0, 8).toUpperCase();
  }).catch(() => showToast('无法读取剪贴板'));
}

// 打开账号绑定子视图（管理功能独立窗口，不在「我」页常驻展示）
function openBindView() {
  showView('view-bind', '账号绑定');
  renderBindView();
  loadBindDevices();
}

function renderBindView() {
  const bound = isBound();
  const unboundCard = document.getElementById('bindUnboundCard');
  const boundCard = document.getElementById('bindBoundCard');
  if (unboundCard) unboundCard.style.display = bound ? 'none' : '';
  if (boundCard) boundCard.style.display = bound ? '' : 'none';
  const bindNick = document.getElementById('bindBoundNickname');
  if (bindNick) bindNick.textContent = state.nickname || '微信账号';
  const badge = document.getElementById('bindDeviceCountBadge');
  const count = (meState.bindDevices || []).length;
  if (badge) { badge.style.display = count > 0 ? '' : 'none'; badge.textContent = count; }
  // 同步「我」页入口副标题（与小程序同语义：已绑定 N 台设备）
  const bindEntry = document.getElementById('bindEntryDesc');
  if (bindEntry) {
    bindEntry.textContent = count > 0 ? ('已绑定 ' + count + ' 台设备') : '管理微信小程序与 APP 的账号绑定';
  }
  renderBindDeviceList();
}

function renderBindDeviceList() {
  const list = document.getElementById('bindDeviceList');
  if (!list) return;
  const devices = meState.bindDevices || [];
  if (meState.bindDevicesLoading && !devices.length) {
    list.innerHTML = '<view class="status-box"><text class="status-text">加载中...</text></view>';
    return;
  }
  if (!devices.length) {
    list.innerHTML = '<view class="empty-state"><view class="empty-illustration"><text class="empty-icon">📱</text></view><text class="empty-title">暂无绑定设备</text><text class="empty-desc">在微信小程序生成绑定密钥并填写后，设备会出现在这里</text></view>';
    return;
  }
  list.innerHTML = devices.map(d => `
    <view class="bind-device-item">
      <view class="bind-device-info">
        <text class="bind-device-name">${esc(d.nickname || '手机设备')}</text>
        <text class="bind-device-time">绑定于 ${esc(d.used_at || '—')}</text>
      </view>
      <view class="bind-device-actions">
        <button class="bind-device-btn bind-device-rename" data-rename-device="${escHtml(d.dev_openid)}" data-rename-nickname="${escHtml(d.nickname || '')}">重命名</button>
        <button class="bind-device-btn bind-device-unbind" data-unbind-device="${escHtml(d.dev_openid)}">解除</button>
      </view>
    </view>`).join('');
  list.querySelectorAll('[data-unbind-device]').forEach(btn => {
    btn.addEventListener('click', () => unbindDevice(btn.dataset.unbindDevice));
  });
  list.querySelectorAll('[data-rename-device]').forEach(btn => {
    btn.addEventListener('click', () => openDeviceRename(btn.dataset.renameDevice, btn.dataset.renameNickname));
  });
}

async function loadBindDevices() {
  if (!state.token) return;
  meState.bindDevicesLoading = true;
  renderBindDeviceList();
  try {
    const r = await api('/api/bind/devices');
    if (r.data && r.data.success) {
      meState.bindDevices = r.data.devices || [];
    }
  } catch (e) { /* 静默 */ }
  meState.bindDevicesLoading = false;
  renderBindView();
}

let _deviceRenameOpenid = '';
function openDeviceRename(devOpenid, curName) {
  _deviceRenameOpenid = devOpenid || '';
  const input = document.getElementById('deviceRenameInput');
  if (input) input.value = curName || '';
  openModal('deviceRenameModal');
}

function saveDeviceRename() {
  const nickname = document.getElementById('deviceRenameInput').value.trim();
  if (!nickname) { showToast('名称不能为空'); return; }
  const devOpenid = _deviceRenameOpenid;
  if (!devOpenid) { closeModal('deviceRenameModal'); return; }
  closeModal('deviceRenameModal');
  api('/api/bind/rename', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dev_openid: devOpenid, nickname }),
  }).then(r => {
    if (r.data && r.data.success) {
      showToast('已重命名');
      loadBindDevices();
    } else showToast((r.data && r.data.message) || '重命名失败');
  }).catch(() => showToast('网络错误'));
}

function unbindDevice(devOpenid) {
  if (!devOpenid) return;
  showConfirm('解除绑定', '解除后该 APP 将回到独立的设备账号：已产生的订单仍保留在微信账号下；解除期间的新订单记入设备账号，重新绑定后自动迁移。', '解除', '#FF3B30', () => {
    api('/api/bind/revoke', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dev_openid: devOpenid }),
    }).then(r => {
      if (r.data && r.data.success) {
        showToast('已解除绑定');
        // 解绑的是本机：切换到返回的设备账号 token；解绑其他设备则保持当前身份
        if (devOpenid === state.devOpenid && r.data.token && r.data.openid) {
          setAuth(r.data.token, r.data.openid);
          refreshSession();
          loadOrders();
        }
        loadBindDevices();
      } else showToast((r.data && r.data.message) || '解除失败');
    }).catch(() => showToast('网络错误'));
  });
}

function unbindWechatAccount() {
  if (!state.devOpenid) { showToast('缺少设备标识，请在小程序中解除绑定'); return; }
  unbindDevice(state.devOpenid);
}

/* ================= 订单 ================= */

// 订单数据加工（价格/文件大小/isExcel），加载与轮询共用，保证"已送达"等判断不丢字段
function normalizeOrder(o) {
  o.totalPriceDisplay = Number(o.total_price || 0).toFixed(2);
  if (o.files) {
    o.files.forEach(f => {
      f.sizeDisplay = f.size ? (f.size / 1024).toFixed(1) + ' KB' : '';
      const name = (f.original_name || f.file_name || '').toLowerCase();
      f.isExcel = name.endsWith('.xls') || name.endsWith('.xlsx');
    });
    o.isExcel = o.files.length > 0 && o.files.every(f => f.isExcel);
  }
  return o;
}

async function loadOrders() {
  if (!state.token) return;
  meState.loadingOrders = true;
  renderOrdersLoading();
  try {
    const r = await api('/api/orders?page=' + meState.ordersPage + '&per_page=' + meState.ordersPerPage);
    meState.loadingOrders = false;
    if (r.status === 200 && r.data && r.data.success) {
      meState.ordersLoadError = '';
      const orders = (r.data.orders || []).map(normalizeOrder);
      meState.orders = orders;
      meState.ordersTotal = r.data.total || 0;
      meState.ordersTotalPages = Math.ceil(meState.ordersTotal / meState.ordersPerPage);
      meState.expandedOrders = {};
      renderOrders();
    } else {
      // 对齐小程序：优先展示后端返回的 message
      meState.ordersLoadError = (r.data && r.data.message) || '加载失败';
      renderOrders();
    }
  } catch (e) {
    meState.loadingOrders = false;
    meState.ordersLoadError = '网络错误';
    renderOrders();
  }
}

function renderOrdersLoading() {
  const list = document.getElementById('orderList');
  if (meState.orders.length === 0 && meState.loadingOrders) {
    list.innerHTML = '<view class="status-box"><text class="status-text">加载中...</text></view>';
  }
}

function orderStatusClass(status) {
  return 'status-' + (status || 'queued');
}

function orderCardHTML(o, expanded, deliveredLabel, allowCancel) {
  const status = ORDER_STATUS_MAP[o.status] || o.status;
  let statusText = status;
  let statusCls = orderStatusClass(o.status);
  // 对齐小程序 me 页：Excel 订单 sent → 已送达（仅"我的打印任务"列表）
  const isDelivered = deliveredLabel && o.isExcel && o.status === 'sent';
  if (isDelivered) {
    statusText = '已送达';
    statusCls = 'status-delivered';
  }
  if (expanded === undefined) expanded = !!meState.expandedOrders[o.id];
  // 取消按钮：默认可取消（本人订单视图）；user-orders 视图仅本人订单显示。
  // 含预约单到点前（scheduled/downloading/waiting），与后端 cancel_order 允许的状态一致
  const canCancel = allowCancel !== false
    && (o.status === 'queued' || o.status === 'printing' || o.status === 'waiting'
        || o.status === 'downloading' || o.status === 'scheduled');
  // 详情区常驻渲染、用 detail-expanded 类切换（对齐小程序：展开/收起均有过渡动画）
  let fileRows = '';
  (o.files || []).forEach(f => {
    fileRows += `
      <view class="detail-file-row">
        <view class="detail-file-info">
          <text class="detail-file-name">${esc(f.original_name || f.file_name || '')}</text>
          <view class="detail-file-tags">
            ${f.file_type ? `<text class="file-type-tag">${esc(f.file_type)}</text>` : ''}
            ${f.sizeDisplay ? `<text class="file-size-tag">${esc(f.sizeDisplay)}</text>` : ''}
          </view>
        </view>
        <view class="detail-file-right">
          <text class="detail-file-copies">${f.copies} 份 × ${f.page_count} 页</text>
          <text class="detail-file-range">${f.duplex === 'on' ? '双面' : '单面'}</text>
          ${f.page_range ? `<text class="detail-file-range">范围: ${esc(f.page_range)}</text>` : ''}
          ${(f.status === 'rejected' || f.status === 'failed') && f.reject_reason ? `<text class="file-reject-reason">${esc(f.reject_reason)}</text>` : ''}
        </view>
      </view>`;
  });
  let licenseRows = '';
  if (o.license_info) {
    licenseRows = `
      <view class="detail-section">
        <view class="detail-section-title">临时许可密钥</view>
        <view class="detail-row"><text class="detail-label">密钥</text><text class="detail-value license-key-value">${esc(o.license_info.key)}</text></view>
        <view class="detail-row"><text class="detail-label">使用时间</text><text class="detail-value">${esc(o.license_info.used_at || '—')}</text></view>
        <view class="detail-row"><text class="detail-label">到期时间</text><text class="detail-value">${esc(o.license_info.expires_at || '—')}</text></view>
      </view>`;
  }
  let extRows = '';
  if (o.urgency !== '低' || o.cover_page > 0 || o.delivery_enabled) {
    extRows = '<view class="detail-section"><view class="detail-section-title">附加服务</view>';
    if (o.urgency && o.urgency !== '低') {
      extRows += `<view class="detail-row"><text class="detail-label">加急</text><text class="detail-value">${o.urgency === '高' ? '🚀 高' : '⚡ 中'}${Number(o.urgency_price) > 0 ? ' + ¥' + Number(o.urgency_price).toFixed(2) : ''}</text></view>`;
    }
    if (o.cover_page > 0) {
      extRows += `<view class="detail-row"><text class="detail-label">首页</text><text class="detail-value">${o.cover_page} 页${Number(o.cover_page_price) > 0 ? ' · ¥' + Number(o.cover_page_price).toFixed(2) + '/页' : ''}</text></view>`;
    }
    if (o.delivery_enabled) {
      extRows += `<view class="detail-row"><text class="detail-label">派送</text><text class="detail-value">${esc(o.delivery_location || '未指定地点')}${Number(o.delivery_percentage) > 0 ? ' · 加收 ' + Number(o.delivery_percentage) + '%' : ''}</text></view>`;
    }
    extRows += '</view>';
  }
  // 备注（与附加服务同级展示；空备注不显示；灰色背景与文件列表一致）
  let remarkRows = '';
  if (o.remark) {
    remarkRows = `<view class="detail-section"><view class="detail-section-title">备注</view>
      <view class="remark-block"><text class="remark-text">${esc(o.remark)}</text></view></view>`;
  }
  const detail = `
    <view class="order-card-detail ${expanded ? 'detail-expanded' : ''}">
      <view class="detail-divider"></view>
      <view class="detail-section">
        <view class="detail-section-title">打印信息</view>
        <view class="detail-row"><text class="detail-label">接单设备</text><text class="detail-value">${o.received_label ? esc(o.received_label) : '—'}</text></view>
        <view class="detail-row"><text class="detail-label">提交时间</text><text class="detail-value">${esc(o.created_at || '')}</text></view>
        <view class="detail-row"><text class="detail-label">开始打印</text><text class="detail-value">${o.print_started_at ? esc(o.print_started_at) : '—'}</text></view>
        <view class="detail-row"><text class="detail-label">合计页数</text><text class="detail-value">${o.isExcel ? '不适用' : ((o.total_pages || (o.page_count * o.copies)) + ' 页')}</text></view>
        <view class="detail-row"><text class="detail-label">合计份数</text><text class="detail-value">${o.isExcel ? '不适用' : ((o.total_copies || o.copies) + ' 份')}</text></view>
      </view>
      ${licenseRows}
      ${(o.files && o.files.length) ? `<view class="detail-section"><view class="detail-section-title">文件列表 (${o.files.length})</view>${fileRows}</view>` : ''}
      ${extRows}
      ${remarkRows}
      <view class="detail-section">
        <view class="detail-row detail-price-row"><text class="detail-label">合计价格</text><text class="detail-price-value status-${o.status || 'queued'}${(o.is_admin_print && o.status === 'sent') ? ' is-self-sent' : ''}">¥${o.totalPriceDisplay || '0.00'}</text></view>
      </view>
      <view class="detail-actions">
        ${canCancel ? `<button class="btn-cancel-sm" data-cancel-order="${o.id}">取消任务</button>` : ''}
      </view>
    </view>`;
  return `
    <view class="order-card ${expanded ? 'order-expanded' : ''}" data-order-id="${o.id}">
      <view class="order-card-header">
        <view class="order-card-main">
          <text class="order-filename">${esc(o.order_number || ('#' + o.id))}</text>
          ${o.source === 'local'
            ? '<text class="order-source-tag tag-local">🖨 本地</text>'
            : '<text class="order-source-tag tag-cloud">☁ 云端</text>'}
          <text class="order-status ${statusCls}">${esc(statusText)}</text>
          ${o.is_admin_print ? '<text class="order-self-print-badge">👤自打</text>' : ''}
        </view>
        <text class="order-number-label">${esc(o.file_summary || o.file || '')}</text>
        <view class="order-card-meta">
          <text class="order-card-stat">📄 ${o.isExcel ? '不适用' : ((o.total_pages || (o.page_count * o.copies)) + ' 页')}</text>
          <text class="order-card-stat">📋 ${o.isExcel ? '不适用' : ((o.total_copies || o.copies) + ' 份')}</text>
        </view>
        <view class="order-card-footer">
          <text class="order-created">${esc(o.created_at || '')}</text>
          <text class="order-arrow ${expanded ? 'arrow-up' : ''}">›</text>
        </view>
      </view>
      ${detail}
    </view>`;
}

function renderOrders() {
  const list = document.getElementById('orderList');
  const empty = document.getElementById('ordersEmpty');
  const badge = document.getElementById('orderCountBadge');
  if (meState.loadingOrders && meState.orders.length === 0) list.innerHTML = '<view class="status-box"><text class="status-text">加载中...</text></view>';
  else if (meState.ordersLoadError && meState.orders.length === 0) { list.innerHTML = '<view class="status-box"><text class="status-text">' + esc(meState.ordersLoadError) + '</text></view>'; empty.style.display = 'none'; }
  else if (meState.orders.length === 0) { list.innerHTML = ''; empty.style.display = ''; }
  else { empty.style.display = 'none'; list.innerHTML = meState.orders.map(o => orderCardHTML(o, !!meState.expandedOrders[o.id], true)).join(''); }
  badge.style.display = meState.orders.length ? '' : 'none';
  badge.textContent = meState.ordersTotal;
  const pager = document.getElementById('ordersPager');
  // 选项为静态 HTML（与子视图一致），仅同步激活高亮
  document.querySelectorAll('#pageSizePicker .page-size-option').forEach(opt => {
    opt.classList.toggle('option-active', parseInt(opt.dataset.size, 10) === meState.ordersPerPage);
  });
  document.getElementById('ordersPageSizeText').textContent = meState.ordersPerPage + '条/页';
  document.getElementById('ordersPageNumbers').innerHTML = buildPageNumbers();
  document.getElementById('ordersPrev').classList.toggle('page-btn-disabled', meState.ordersPage <= 1);
  document.getElementById('ordersNext').classList.toggle('page-btn-disabled', meState.ordersPage >= meState.ordersTotalPages);
  pager.style.display = meState.ordersTotalPages > 0 ? '' : 'none';
  // 底部分页 + 结束提示（对齐小程序 me 页）
  const pagerBottom = document.getElementById('ordersPagerBottom');
  if (pagerBottom) {
    pagerBottom.style.display = meState.ordersTotalPages > 1 ? '' : 'none';
    document.getElementById('ordersPageNumbersBottom').innerHTML = buildPageNumbers();
    document.getElementById('ordersPrevBottom').classList.toggle('page-btn-disabled', meState.ordersPage <= 1);
    document.getElementById('ordersNextBottom').classList.toggle('page-btn-disabled', meState.ordersPage >= meState.ordersTotalPages);
  }
  const endHint = document.getElementById('ordersEndHint');
  if (endHint) {
    endHint.style.display = (meState.ordersTotalPages > 0 && meState.ordersPage >= meState.ordersTotalPages && meState.orders.length > 0) ? '' : 'none';
  }
  measureAll(150);
}

function buildPageNumbers() {
  const total = meState.ordersTotalPages;
  const current = meState.ordersPage;
  // 对齐小程序：仅 1 页时也显示当前页数字（高亮），避免分页栏只有 ‹ › 箭头
  if (total <= 1) {
    return '<view class="page-num page-num-active" data-page-num="1">1</view>';
  }
  const pages = [];
  if (total <= 7) { for (let i = 1; i <= total; i++) pages.push(i); }
  else {
    pages.push(1);
    if (current > 3) pages.push('...');
    for (let i = Math.max(2, current - 1); i <= Math.min(total - 1, current + 1); i++) pages.push(i);
    if (current < total - 2) pages.push('...');
    pages.push(total);
  }
  return pages.map(p => {
    if (p === '...') return '<view class="page-ellipsis">…</view>';
    return `<view class="page-num ${p === current ? 'page-num-active' : ''}" data-page-num="${p}">${p}</view>`;
  }).join('');
}

function toggleOrder(id) {
  if (meState.expandedOrders[id]) delete meState.expandedOrders[id];
  else meState.expandedOrders[id] = true;
  // 原地切换类名让 CSS 过渡播放（整表重绘会重建元素导致展开/收起突变）
  const card = document.querySelector('#orderList [data-order-id="' + id + '"]');
  if (card) {
    const expanded = !!meState.expandedOrders[id];
    card.classList.toggle('order-expanded', expanded);
    const detail = card.querySelector('.order-card-detail');
    if (detail) detail.classList.toggle('detail-expanded', expanded);
    const arrow = card.querySelector('.order-arrow');
    if (arrow) arrow.classList.toggle('arrow-up', expanded);
  }
  measureAll(150);
  // 详情展开/收起有 0.3s 动画，收起后内容变短需在动画结束后再次测量（对齐小程序 onOrderTap 双测量），
  // 否则界面停留在旧滚动位置下方留白
  setTimeout(() => measureAll(320), 340);
}

function changeOrdersPage(page) {
  if (page < 1 || page > meState.ordersTotalPages || page === meState.ordersPage) return;
  meState.ordersPage = page;
  loadOrders();
}

let _pageSizePickerOpenedAt = 0;

// 打开下拉：移到 body 末尾（逃出 scroll-content 的 translateY 变换层，
// fixed 才真正相对视口、backdrop-filter 才能正常采样），按触发器视口坐标定位
function openDropdownFixed(dropdown, triggerEl) {
  if (!dropdown || !triggerEl) return;
  if (dropdown.parentElement !== document.body) document.body.appendChild(dropdown);
  const r = triggerEl.getBoundingClientRect();
  dropdown.style.position = 'fixed';
  dropdown.style.left = Math.max(8, Math.round(r.left)) + 'px';
  dropdown.style.top = Math.round(r.bottom + 6) + 'px';
  dropdown.style.zIndex = '2000';
  dropdown.classList.add('dropdown-show');
}

function togglePageSizePicker() {
  try {
    const picker = document.getElementById('pageSizePicker');
    if (!picker) { showToast('分页下拉元素缺失'); return; }
    const open = !picker.classList.contains('dropdown-show');
    if (open) {
      _pageSizePickerOpenedAt = Date.now();
      openDropdownFixed(picker, document.getElementById('ordersPageSize'));
    } else {
      hidePageSizePicker();
      return;
    }
    const arrow = document.getElementById('ordersPageSizeArrow');
    if (arrow) arrow.classList.add('arrow-up');
  } catch (err) {
    showToast('分页下拉出错: ' + (err && err.message ? err.message : err));
  }
}

// 子视图（本地打印任务/用户订单）每页条数下拉：展开/收起/选择
function toggleUoPageSizePicker() {
  const v = meState.userOrdersView;
  if (!v) return;
  if (v.showPageSizePicker) { closeUoPageSizePicker(); return; }
  v.showPageSizePicker = true;
  openDropdownFixed(document.getElementById('uoPageSizeDropdown'), document.getElementById('uoPageSizeSelector'));
  syncUserOrdersPageSizePicker();
}

function closeUoPageSizePicker() {
  const v = meState.userOrdersView;
  if (!v || !v.showPageSizePicker) return;
  v.showPageSizePicker = false;
  syncUserOrdersPageSizePicker();
}

function selectUoPageSize(size) {
  const v = meState.userOrdersView;
  if (!v) return;
  if (size === v.perPage) { closeUoPageSizePicker(); return; }
  v.perPage = size;
  v.page = 1;
  v.showPageSizePicker = false;
  syncUserOrdersPageSizePicker();
  loadUserOrders().then(() => scrollUserOrdersToTop());
}

// 统一收起：移除类并清空内联显隐（内联 opacity 优先级高于 CSS，不清会关不掉）
function hidePageSizePicker() {
  const picker = document.getElementById('pageSizePicker');
  if (picker) picker.classList.remove('dropdown-show');
  const arrow = document.getElementById('ordersPageSizeArrow');
  if (arrow) arrow.classList.remove('arrow-up');
}

function selectOrdersPageSize(size) {
  meState.ordersPerPage = size;
  meState.ordersPage = 1;
  hidePageSizePicker();
  loadOrders().then(() => scrollToOrdersSection());
}

// 滚动开始即收起分页下拉（对齐小程序 onScrollerTouchStart，避免 fixed 下拉错位）
function closePageSizePicker() {
  const picker = document.getElementById('pageSizePicker');
  if (!picker || !picker.classList.contains('dropdown-show')) return;
  // 展开后短暂时间内不因滚动/回弹收起，防止模拟触摸的物理帧干扰（点击已不启动物理，此为双保险）
  if (Date.now() - _pageSizePickerOpenedAt < 300) return;
  hidePageSizePicker();
}

// 选择条数后滚动回"我的打印任务"区域顶部（对齐小程序 _scrollToOrdersSection）
function scrollToOrdersSection() {
  const engine = scrollEngines.me;
  if (!engine) return;
  const section = document.querySelector('#page-me .orders-section');
  if (!section || !engine.el) return;
  engine.measure(); // 用最新内容高度更新 maxY
  // 内容变短导致当前位置超出新边界：直接无动画归位
  if (engine.y > engine.maxY) {
    engine.cancel();
    engine.y = engine.maxY;
    engine.applyY();
  }
  // 内容不足一屏（maxY <= 0）：不滚动，保持原位
  if (engine.maxY <= 0) return;
  // 对齐小程序 WXS：区块坐标 = 区块可视位置 - 容器(scroller)可视位置 + 当前滚动量。
  // 注意必须用未被 transform 的 scroller 矩形，用被 translateY 的 scroll-content 会多出一个 +y，
  // 导致目标位置随当前滚动位置漂移（点击任意条数选项都会继续向下滚出大量距离）
  const offset = section.getBoundingClientRect().top - engine.el.getBoundingClientRect().top + engine.y;
  const target = Math.min(Math.max(0, offset - 20), engine.maxY);
  // 目标与当前位置接近时不滚动，避免无意义的跳动
  if (Math.abs(target - engine.y) < 1) return;
  engine.scrollTo(target, 280);
}

// 取消订单请求进行中（防重复提交：确认框关闭后 loading toast 期间连点会并发两个取消请求）
let _cancellingOrder = false;
function cancelOrder(id) {
  showConfirm('确认取消', '确定要取消这个打印任务吗？', '取消订单', '#FF9500', () => {
    if (_cancellingOrder) return;
    _cancellingOrder = true;
    showToast('取消中...', 10000);
    api('/api/cancel_order', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ order_id: String(id) }),
    }).then(r => {
      if (r.data && r.data.success) {
        showToast('已取消');
        loadOrders();
        const uo = document.getElementById('view-user-orders');
        if (uo && uo.style.display !== 'none') loadUserOrders();
      } else showToast((r.data && r.data.message) || '取消失败');
    }).catch(() => showToast('网络错误'))
      .finally(() => { _cancellingOrder = false; });
  });
}

function startOrderPolling() {
  stopOrderPolling();
  meState._orderPollTimer = setInterval(() => {
    if (document.getElementById('page-me').style.display !== 'none') {
      api('/api/orders?page=' + meState.ordersPage + '&per_page=' + meState.ordersPerPage).then(r => {
        if (r.status === 200 && r.data && r.data.success) {
          const orders = (r.data.orders || []).map(normalizeOrder);
          meState.orders = orders;
          meState.ordersTotal = r.data.total || 0;
          meState.ordersTotalPages = Math.ceil(meState.ordersTotal / meState.ordersPerPage);
          renderOrders();
        }
      }).catch(() => {});
    }
  }, 15000);
}

function stopOrderPolling() {
  if (meState._orderPollTimer) { clearInterval(meState._orderPollTimer); meState._orderPollTimer = null; }
}

// 管理员许可密钥轮询（对齐小程序：me 页展示期间每 15s 刷新密钥状态 + 管理员列表）
function startKeyPolling() {
  stopKeyPolling();
  meState._keyPollTimer = setInterval(() => {
    const meVisible = document.getElementById('page-me') && document.getElementById('page-me').style.display !== 'none';
    if (!meVisible || state.role !== 'admin') return;
    loadActiveKeys();
    if (state.isSuperAdmin) loadAdmins();
  }, 15000);
}

function stopKeyPolling() {
  if (meState._keyPollTimer) { clearInterval(meState._keyPollTimer); meState._keyPollTimer = null; }
}

/* ================= 管理员：密钥 ================= */

function setLicenseMinutes(v) {
  meState.licenseMinutes = Math.max(1, Math.min(10, v));
  updateKeyForm();
}

function updateKeyForm() {
  const valEl = document.getElementById('licenseMinutesValue');
  if (valEl) valEl.value = meState.licenseMinutes;
  document.getElementById('licenseMinutesMinus').classList.toggle('stepper-disabled', meState.licenseMinutes <= 1);
  document.getElementById('licenseMinutesPlus').classList.toggle('stepper-disabled', meState.licenseMinutes >= 10);
  document.getElementById('keyTypeTemp').classList.toggle('opt-active', meState.keyType === 'temp');
  document.getElementById('keyTypeAdmin').classList.toggle('opt-active', meState.keyType === 'admin');
  const slider = document.getElementById('keyTypeSlider');
  if (slider) slider.classList.toggle('slider-right', meState.keyType === 'admin');
}

// 密钥类型分段滑块：按住拖动（指示条跟手，松手吸附；纯点击仍走原切换）
function bindKeyTypeDrag() {
  const toggle = document.querySelector('.key-type-toggle');
  if (!toggle || toggle._dragBound) return;
  toggle._dragBound = true;
  const slider = document.getElementById('keyTypeSlider');
  if (!slider) return;
  let pointerId = null, startX = 0, startY = 0, dragging = false, moved = false, startPx = 0, segPx = 0;
  toggle.addEventListener('pointerdown', (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    pointerId = e.pointerId;
    try { toggle.setPointerCapture(e.pointerId); } catch (err) { /* 兼容 */ }
    startX = e.clientX; startY = e.clientY;
    dragging = true; moved = false;
    const r = toggle.getBoundingClientRect();
    segPx = r.width / 2;
    startPx = (meState.keyType === 'admin' ? 1 : 0) * segPx;
  });
  toggle.addEventListener('pointermove', (e) => {
    if (!dragging || e.pointerId !== pointerId) return;
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    if (!moved) {
      if (Math.abs(dx) < 6 && Math.abs(dy) < 6) return;
      if (Math.abs(dx) <= Math.abs(dy)) { dragging = false; return; } // 纵向交给页面滚动
      moved = true;
      gestureBus.horizontal = true;
    }
    const r = toggle.getBoundingClientRect();
    segPx = r.width / 2;
    const offsetPx = Math.max(0, Math.min(r.width - segPx, startPx + dx));
    slider.style.transition = 'none';
    slider.style.transform = 'translateX(' + offsetPx + 'px)';
  });
  const endDrag = (e) => {
    if (!dragging) { pointerId = null; return; }
    dragging = false; pointerId = null;
    gestureBus.horizontal = false;
    if (!moved) return; // 纯点击
    const r = toggle.getBoundingClientRect();
    segPx = r.width / 2;
    const offsetPx = parseFloat((/translateX\((-?[\d.]+)px\)/.exec(slider.style.transform || '') || [])[1] || '0');
    const idx = Math.max(0, Math.min(1, Math.round(offsetPx / segPx)));
    slider.style.transition = 'transform 0.3s cubic-bezier(0.34,1.56,0.64,1)';
    slider.style.transform = 'translateX(' + (idx * segPx) + 'px)';
    toggle._dragHandled = Date.now();
    meState.keyType = idx === 1 ? 'admin' : 'temp';
    updateKeyForm();
    setTimeout(() => { if (slider.style.transform) slider.style.transform = ''; }, 320);
  };
  toggle.addEventListener('pointerup', endDrag);
  toggle.addEventListener('pointercancel', endDrag);
}

function generateKey() {
  if (meState.generating) return;
  meState.generating = true;
  const btn = document.getElementById('generateKeyBtn');
  btn.disabled = true;
  btn.textContent = '生成中…';
  api('/api/license/create', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ validity_minutes: meState.licenseMinutes, type: meState.keyType }),
  }).then(r => {
    meState.generating = false;
    btn.disabled = false;
    btn.textContent = '生成许可密钥';
    if (r.data && r.data.success) { showToast('密钥已生成'); loadActiveKeys(); }
    else showToast((r.data && r.data.message) || '生成失败');
  }).catch(() => {
    meState.generating = false;
    btn.disabled = false;
    btn.textContent = '生成许可密钥';
    showToast('网络错误');
  });
}

let _keysLoadedOnce = false;

async function loadActiveKeys() {
  if (!state.token || state.role !== 'admin') return;
  try {
    const r = await api('/api/license/active');
    if (r.data && r.data.success) {
      const prev = meState.activeKeys;
      const prevByKey = new Map(prev.map(k => [k.key, k]));
      const newKeys = r.data.keys || [];
      const newSet = new Set(newKeys.map(k => k.key));
      // 合并：保留本地动画状态；非首次加载出现的新 key → 播放入场动画
      // （用显式 _keysLoadedOnce 而非"列表是否为空"，保证第一把生成的密钥也能播放入场）
      const merged = newKeys.map(k => {
        const old = prevByKey.get(k.key);
        return {
          ...k,
          _entering: old ? !!old._entering : _keysLoadedOnce,
          _exiting: old ? !!old._exiting : false,
        };
      });
      // 服务端已移除的 key → 保留并标记离场，动画结束后再移除
      const removed = prev.filter(o => !newSet.has(o.key) && !o._exiting);
      for (const rk of removed) merged.push({ ...rk, _exiting: true });
      meState.activeKeys = merged;
      refreshKeyCountdowns();
      renderActiveKeys();
      startKeyCountdown();
      if (removed.length) {
        setTimeout(() => {
          meState.activeKeys = meState.activeKeys.filter(k => !k._exiting);
          renderActiveKeys();
          if (!meState.activeKeys.length) stopKeyCountdown();
        }, 350);
      }
      if (merged.some(k => k._entering)) {
        setTimeout(() => {
          meState.activeKeys.forEach(k => { k._entering = false; });
          document.querySelectorAll('#activeKeys .key-card-wrap.key-entering').forEach(el => el.classList.remove('key-entering'));
        }, 800);
      }
      _keysLoadedOnce = true;
      return true;
    }
  } catch (e) { /* 静默 */ }
  return false;
}

let _keyCountdownTimer = null;
function startKeyCountdown() {
  stopKeyCountdown();
  if (!meState.activeKeys.length) return;
  _keyCountdownTimer = setInterval(() => {
    refreshKeyCountdowns();
    updateKeyCountdownTexts();
  }, 1000);
}
function stopKeyCountdown() {
  if (_keyCountdownTimer) { clearInterval(_keyCountdownTimer); _keyCountdownTimer = null; }
}
function refreshKeyCountdowns() {
  const now = Date.now();
  meState.activeKeys.forEach(k => {
    const exp = new Date(String(k.expires_at || '').replace(/-/g, '/')).getTime();
    k._expired = !isNaN(exp) && exp < now;
    // 对齐小程序：右侧倒计时区对已使用密钥统一显示"已使用"（等待任务中仅出现在按钮区）
    if (k.status !== 'unused') k._countdownText = '已使用';
    else k._countdownText = k._expired ? '已过期' : formatRemain(k.expires_at);
  });
}

// 倒计时只在原地更新文本（不整表重绘，避免清掉左滑绑定/展开状态）
function updateKeyCountdownTexts() {
  document.querySelectorAll('#activeKeys [data-key-swipe]').forEach(card => {
    const k = meState.activeKeys.find(x => x.key === card.dataset.keySwipe);
    if (!k) return;
    const cd = card.querySelector('.key-countdown');
    if (cd) {
      cd.textContent = k._countdownText;
      cd.classList.toggle('countdown-expired', !!k._expired);
    }
  });
}

function renderActiveKeys() {
  const wrap = document.getElementById('activeKeys');
  const count = document.getElementById('activeKeyCount');
  count.style.display = meState.activeKeys.length ? '' : 'none';
  count.textContent = meState.activeKeys.length;
  if (!meState.activeKeys.length) {
    wrap.innerHTML = '';
    return;
  }
  wrap.innerHTML = meState.activeKeys.map(k => {
    const canCopy = k.status === 'unused' && !k._expired;
    // 已使用密钥也可左滑删除（后端归档，授权记录保留在历史授权用户中）
    const revokeLabel = k.status === 'unused' ? '作废' : '删除';
    const confirmBtn = k.status === 'used_waiting' && k.type === 'admin'
      ? `<button class="copy-btn btn-confirm-admin" data-confirm-key="${escHtml(k.key)}">确认并关闭</button>` : '';
    const waitingBtn = k.status === 'used_waiting' && k.type !== 'admin'
      ? `<button class="copy-btn btn-waiting" disabled>等待任务中</button>` : '';
    const settleBtn = k.order_id
      ? `<button class="copy-btn" data-settle="${k.order_id}" data-settle-nickname="${escHtml(k.used_by_nickname || '用户')}">结算</button>` : '';
    const deleteBtn = `<view class="key-delete" data-revoke-key="${escHtml(k.key)}" style="opacity:0"><text class="delete-icon">🗑</text><text>${revokeLabel}</text></view>`;
    return `
      <view class="key-card-wrap ${k._entering ? 'key-entering' : ''} ${k._exiting ? 'key-exiting' : ''} ${k._expired && k.status === 'unused' ? 'key-drawer-expired' : ''}">
        ${deleteBtn}
        <view class="key-card" data-key-swipe="${escHtml(k.key)}">
          <view class="key-row">
            <view class="key-left">
              <text class="key-label">${k.type === 'admin' ? '管理员密钥' : '许可密钥'}</text>
              <text class="key-value">${esc(k.key)}</text>
            </view>
            <view class="key-right">
              <text class="key-countdown ${k._expired ? 'countdown-expired' : ''}">${esc(k._countdownText)}</text>
            </view>
          </view>
          ${k.used_by ? `<view class="key-user-info">
            ${k.used_by_avatar_url ? `<img class="key-user-avatar" src="${escHtml(k.used_by_avatar_url)}">` : ''}
            <text class="key-user-name">${esc(k.used_by_nickname || '用户')}</text>
            ${settleBtn}
          </view>` : ''}
          <view class="key-actions">
            ${canCopy ? `<button class="copy-btn" data-copy-key="${escHtml(k.key)}">复制</button>` : ''}
            ${k.status === 'unused' && k._expired ? '<button class="copy-btn copy-btn-disabled">已过期</button>' : ''}
            ${confirmBtn}
            ${waitingBtn}
          </view>
        </view>
      </view>`;
  }).join('');
  // 列表重建后旧绑定的元素已脱离文档，重新绑定到新元素
  _swipeBindings = {};
  // 绑定左滑作废
  bindKeySwipes();
}

let _swipeBindings = {};
function bindKeySwipes() {
  document.querySelectorAll('[data-key-swipe]').forEach(card => {
    const key = card.dataset.keySwipe;
    const del = card.parentElement.querySelector('.key-delete');
    if (!del) return;
    if (_swipeBindings[key]) return;
    // 密钥卡：延迟淡入（35% 起显）+ 右滑归位快速淡出（对齐小程序 onKeyTouchMove/End）
    _swipeBindings[key] = makeSwipeable(card, del, () => revokeKey(key), { fadeMode: 'delayed', quickFade: true });
  });
}

// 通用左滑手势：露出右侧按钮，超过半程吸附展开（Pointer Events，鼠标/触摸通用）
// opts：{ deadZone, rubberOver, fadeMode: 'linear'|'delayed', quickFade } 用于逐卡对齐小程序参数
function makeSwipeable(card, deleteEl, onDelete, opts) {
  opts = opts || {};
  let startX = 0, startY = 0, lastRaw = 0, horizontal = false, startCardX = 0, pointerId = null;
  // 以删除按钮实际宽度为准（CSS 16.803cqw ≈ 80px，不能写死）
  // 延迟到首次触摸时测量：卡片可能在「我」页未显示时绑定，display:none 下 offsetWidth=0
  let DELETE_W = deleteEl ? deleteEl.offsetWidth : 140;
  const deadZone = opts.deadZone != null ? opts.deadZone : 8;
  const rubberOver = opts.rubberOver != null ? opts.rubberOver : 40;
  const rubber = (raw, max, min, over) => {
    if (raw > max) return max + over * (1 - Math.exp(-(raw - max) / (over * 1.6)));
    if (raw < min) return min - over * (1 - Math.exp(-(min - raw) / (over * 1.6)));
    return raw;
  };
  const getX = () => {
    const m = /translateX\((-?[\d.]+)px\)/.exec(card.style.transform || '');
    return m ? parseFloat(m[1]) : 0;
  };
  const opacityOf = (raw) => {
    if (raw >= 0) return 0;
    const p = Math.abs(raw) / DELETE_W;
    if (opts.fadeMode === 'delayed') return Math.min(1, Math.max(0, (p - 0.35) / 0.65));
    return Math.min(1, p / 0.6);
  };
  const setQuickFade = (on) => {
    if (!opts.quickFade || !deleteEl) return;
    const wrap = deleteEl.parentElement;
    if (wrap) wrap.classList.toggle('quick-fade', on);
  };
  const apply = (x, opacity, transition) => {
    card.style.transform = 'translateX(' + x + 'px)';
    card.style.transition = transition ? 'transform 0.25s cubic-bezier(0.4,0,0.2,1)' : 'none';
    if (deleteEl) deleteEl.style.opacity = opacity;
  };
  const onDown = (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    if (!DELETE_W) DELETE_W = deleteEl ? (deleteEl.offsetWidth || 140) : 140;
    pointerId = e.pointerId;
    startX = e.clientX; startY = e.clientY;
    // 卡片可能已处于展开态：以当前位移为起点，右滑才能平滑归位
    startCardX = getX();
    lastRaw = startCardX;
    horizontal = false;
  };
  const onMove = (e) => {
    if (e.pointerId !== pointerId) return;
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    if (!horizontal) {
      if (Math.abs(dx) < deadZone && Math.abs(dy) < deadZone) return;
      if (Math.abs(dx) > Math.abs(dy)) {
        horizontal = true;
        gestureBus.horizontal = true;
      }
      else return;
    }
    const raw = startCardX + dx;
    lastRaw = raw;
    const visual = rubber(raw, 0, -DELETE_W, rubberOver);
    const opacity = opacityOf(raw);
    setQuickFade(raw >= 0);
    apply(visual, opacity, false);
  };
  const onUp = () => {
    if (pointerId == null) return;
    pointerId = null;
    gestureBus.horizontal = false;
    if (!horizontal) return;
    // 记录滑动时间戳：左滑后松手补发的 click 不再触发卡片点击行为
    card.dataset.swiped = String(Date.now());
    // 吸附判定用卡片绝对位移（展开态右滑也能正确计算），对齐小程序 onKeyTouchEnd
    const raw = lastRaw;
    const target = raw > 0 ? 0 : (raw < -DELETE_W ? -DELETE_W : (raw < -DELETE_W / 2 ? -DELETE_W : 0));
    setQuickFade(target === 0);
    apply(target, target === 0 ? 0 : 1, true);
    horizontal = false;
  };
  card.addEventListener('pointerdown', onDown);
  card.addEventListener('pointermove', onMove);
  card.addEventListener('pointerup', onUp);
  card.addEventListener('pointercancel', onUp);
  return { destroy() {
    card.removeEventListener('pointerdown', onDown);
    card.removeEventListener('pointermove', onMove);
    card.removeEventListener('pointerup', onUp);
    card.removeEventListener('pointercancel', onUp);
  }};
}

function copyKey(key) {
  const k = meState.activeKeys.find(x => x.key === key);
  if (!k) return;
  const text = '这是HN同学的打印机的使用许可密钥，剩余有效时间' + (k._countdownText || '') + '，请在有效期内填写到小程序的指定位置:\n密钥: ' + k.key;
  copyText(text, '已复制到剪贴板');
}

function revokeKey(key) {
  const k = meState.activeKeys.find(x => x.key === key);
  const isUsed = !!(k && k.status !== 'unused');
  showConfirm(isUsed ? '删除密钥' : '作废密钥',
    isUsed ? '删除此密钥不会影响已获得的授权。' : '确定作废此许可密钥？作废后他人将无法使用。',
    isUsed ? '删除' : '作废', '#FF3B30', () => {
    // 第一步：右滑收起删除按钮（对齐小程序，先收起再请求）
    const card = document.querySelector('[data-key-swipe="' + key + '"]');
    const wrap = card ? card.parentElement : null;
    const del = card ? wrap.querySelector('.key-delete') : null;
    if (card) {
      card.style.transition = 'transform 0.25s cubic-bezier(0.4,0,0.2,1)';
      card.style.transform = 'translateX(0)';
      if (del) del.style.opacity = 0;
    }
    api('/api/license/revoke', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key }),
    }).then(r => {
      if (r.data && r.data.success) {
        showToast(isUsed ? '已删除' : '已作废');
        // 第二步：收起完成后播离场动画（keySlideOut 0.3s），再移除
        setTimeout(() => {
          const k = meState.activeKeys.find(x => x.key === key);
          if (k) k._exiting = true;
          if (wrap) wrap.classList.add('key-exiting');
          setTimeout(() => {
            meState.activeKeys = meState.activeKeys.filter(x => x.key !== key);
            renderActiveKeys();
            if (!meState.activeKeys.length) stopKeyCountdown();
          }, 350);
        }, 250);
      } else {
        showToast((r.data && r.data.message) || '操作失败');
        restoreKeySwipe(card, del);
      }
    }).catch(() => {
      showToast('网络错误');
      restoreKeySwipe(card, del);
    });
  });
}

// 作废失败：恢复卡片到滑开状态（删除按钮可见，便于重试）
function restoreKeySwipe(card, del) {
  if (!card) return;
  const w = del ? (del.offsetWidth || 80) : 80;
  card.style.transition = 'transform 0.25s cubic-bezier(0.4,0,0.2,1)';
  card.style.transform = 'translateX(' + (-w) + 'px)';
  if (del) del.style.opacity = 1;
}

function confirmAdminKey(key) {
  api('/api/license/finish', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key }),
  }).then(r => {
    if (r.data && r.data.success) { showToast('已确认'); loadActiveKeys(); }
    else showToast((r.data && r.data.message) || '操作失败');
  }).catch(() => showToast('网络错误'));
}

function settleOrder(orderId, nickname) {
  api('/api/order_price/' + orderId).then(r => {
    if (r.data && r.data.success) {
      const d = r.data;
      const files = d.files || [];
      let text = '【打印任务结算】\n用户: ' + (nickname || '用户');
      files.forEach((f, i) => {
        const unitPrice = typeof f.per_copy_price === 'number' ? f.per_copy_price : 0;
        const fileTotal = typeof f.total_price === 'number' ? f.total_price : 0;
        text += '\n文件' + (i + 1) + ': ' + (f.file_name || '');
        text += ' | ' + f.copies + '份 × ' + f.page_count + '页';
        text += ' | 单价: ¥' + unitPrice.toFixed(2);
        text += ' | 小计: ¥' + fileTotal.toFixed(2);
      });
      text += '\n总价: ¥' + (typeof d.total_price === 'number' ? d.total_price : 0).toFixed(2);
      copyText(text, '已复制结算详情');
    } else showToast((r.data && r.data.message) || '无法获取该订单结算信息');
  }).catch(() => showToast('网络错误'));
}

/* ================= 管理员：存储 / 防滥用 ================= */

async function loadStorageStats() {
  if (!state.token || state.role !== 'admin') return false;
  try {
    const r = await api('/api/admin/storage');
    if (r.status === 200 && r.data && r.data.success) {
      meState.storageStats = r.data;
      meState.retentionDays = r.data.retention_days != null ? r.data.retention_days : 7;
      meState.retentionHours = r.data.retention_hours != null ? r.data.retention_hours : 0;
      renderStorageStats();
      updateAdminCollapsed(); // 数据到达 → 存储区块展开（对齐小程序）
      _storageRetryCount = 0;
      return true;
    }
  } catch (e) { /* 静默 */ }
  // 拉取失败：短时自动重试（覆盖刚升级时的抖动/限流），最多 3 次
  scheduleStorageRetry();
  return false;
}

let _storageRetryCount = 0;
function scheduleStorageRetry() {
  if (!state.token || state.role !== 'admin') return;
  if (_storageRetryCount >= 3) { _storageRetryCount = 0; return; }
  _storageRetryCount++;
  const delay = [3000, 8000, 15000][_storageRetryCount - 1] || 15000;
  setTimeout(() => {
    if (!state.token || state.role !== 'admin') { _storageRetryCount = 0; return; }
    loadStorageStats();
  }, delay);
}

function renderStorageStats() {
  const s = meState.storageStats;
  if (!s) return;
  document.getElementById('storageFiles').textContent = (s.total_files || 0) + ' 个';
  document.getElementById('storageSize').textContent = s.total_size_display || '0 B';
  document.getElementById('retentionDaysValue').value = meState.retentionDays;
  document.getElementById('retentionHoursValue').value = meState.retentionHours;
  document.getElementById('retentionHint').style.display = (meState.retentionDays === 0 && meState.retentionHours === 0) ? '' : 'none';
  document.getElementById('retentionDaysMinus').classList.toggle('stepper-disabled', meState.retentionDays <= 0);
  document.getElementById('retentionHoursMinus').classList.toggle('stepper-disabled', meState.retentionHours <= 0);
  measureAll(150);
}

function setRetention(kind, v) {
  if (kind === 'days') meState.retentionDays = Math.max(0, Math.min(365, v));
  else meState.retentionHours = Math.max(0, Math.min(23, v));
  renderStorageStats();
}

function saveRetention() {
  if (meState.savingRetention) return;
  if (meState.retentionDays === 0 && meState.retentionHours === 0) { /* 永不过期，允许 */ }
  else if (meState.retentionDays === 0 && meState.retentionHours < 1) { showToast('至少保留1小时'); return; }
  meState.savingRetention = true;
  api('/api/admin/storage', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ retention_days: meState.retentionDays, retention_hours: meState.retentionHours }),
  }).then(r => {
    meState.savingRetention = false;
    if (r.data && r.data.success) { showToast('已同步到服务器和本地工具'); loadStorageStats(); }
    else showToast((r.data && r.data.message) || '保存失败');
  }).catch(() => { meState.savingRetention = false; showToast('网络错误'); });
}

function deleteAllFiles() {
  showConfirm('⚠️ 确认删除', '将删除服务器及本地打印工具的全部缓存文件（不包括用户头像），此操作不可撤销。确定继续？', '确认删除', '#FF3B30', () => {
    meState.deletingAllFiles = true;
    api('/api/admin/storage', { method: 'DELETE' }).then(r => {
      meState.deletingAllFiles = false;
      if (r.data && r.data.success) { showToast(r.data.message || '已删除'); loadStorageStats(); }
      else showToast((r.data && r.data.message) || '删除失败');
    }).catch(() => { meState.deletingAllFiles = false; showToast('网络错误'); });
  });
}

async function loadSecurityConfig() {
  if (!state.token || state.role !== 'admin') return false;
  try {
    const r = await api('/api/admin/security');
    if (r.status === 200 && r.data && r.data.success) {
      meState.securityConfig = r.data;
      meState.securityItems = SECURITY_DEFS.map(d => ({
        ...d,
        value: r.data[d.key] !== undefined && r.data[d.key] !== null ? Number(r.data[d.key]) : 0,
      }));
      renderSecurityConfig();
      updateAdminCollapsed(); // 数据到达 → 防滥用区块展开（对齐小程序）
      return true;
    }
  } catch (e) { /* 静默 */ }
  return false;
}

function renderSecurityConfig() {
  document.getElementById('securityItems').innerHTML = meState.securityItems.map((it, i) => `
    <view class="security-row">
      <view class="security-label-wrap">
        <text class="security-label">${esc(it.label)}</text>
        <text class="security-hint">${esc(it.hint)}</text>
      </view>
      <view class="security-stepper">
        <view class="retention-stepper-btn ${it.value <= it.min ? 'stepper-disabled' : ''}" data-sec-minus="${i}">−</view>
        <input class="retention-stepper-input security-input" type="number" value="${it.value}" data-sec-input="${i}">
        <view class="retention-stepper-btn ${it.value >= it.max ? 'stepper-disabled' : ''}" data-sec-plus="${i}">+</view>
        <text class="retention-unit">${esc(it.unit)}</text>
      </view>
    </view>`).join('');
}

function toggleSecurityExpanded() {
  meState.securityExpanded = !meState.securityExpanded;
  document.getElementById('securitySummary').classList.toggle('security-summary-active', meState.securityExpanded);
  document.getElementById('securityDetail').classList.toggle('security-detail-expanded', meState.securityExpanded);
  document.getElementById('securityArrow').classList.toggle('arrow-up', meState.securityExpanded);
  measureAll(320);
  // 0.3s 过渡结束后再次测量（收起后内容变短，界面自然上移对齐小程序）
  setTimeout(() => measureAll(320), 340);
}

function updateSecurityItem(idx, delta) {
  const it = meState.securityItems[idx];
  if (!it) return;
  it.value = Math.max(it.min, Math.min(it.max, it.value + delta));
  // 原地更新数值与禁用态（不整表重绘）：重绘会销毁被按下的按钮，短按缩放动画无法播放
  const rows = document.querySelectorAll('#securityItems .security-row');
  const row = rows[idx];
  if (row) {
    const input = row.querySelector('.security-input');
    if (input) input.value = it.value;
    const minus = row.querySelector('[data-sec-minus]');
    const plus = row.querySelector('[data-sec-plus]');
    if (minus) minus.classList.toggle('stepper-disabled', it.value <= it.min);
    if (plus) plus.classList.toggle('stepper-disabled', it.value >= it.max);
  }
}

function saveSecurity() {
  if (meState.savingSecurity) return;
  const payload = {};
  meState.securityItems.forEach(it => { payload[it.key] = it.value; });
  meState.savingSecurity = true;
  api('/api/admin/security', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then(r => {
    meState.savingSecurity = false;
    if (r.data && r.data.success) { showToast('防滥用阈值已更新'); loadSecurityConfig(); }
    else showToast((r.data && r.data.message) || '保存失败');
  }).catch(() => { meState.savingSecurity = false; showToast('网络错误'); });
}

/* ================= 管理员：临时用户 / 管理员列表 ================= */

async function loadTempUsers() {
  if (!state.token || state.role !== 'admin') return false;
  try {
    const r = await api('/api/admin/temp_users');
    if (r.data && r.data.success) {
      meState.tempUsers = r.data.users || [];
      renderTempUsers();
      return true;
    }
  } catch (e) { /* 静默 */ }
  return false;
}

function renderTempUsers() {
  const wrap = document.getElementById('tempUserList');
  const count = document.getElementById('tempUserCount');
  count.style.display = meState.tempUsers.length ? '' : 'none';
  count.textContent = meState.tempUsers.length;
  if (!meState.tempUsers.length) {
    wrap.innerHTML = '<view class="license-card"><text class="redeem-desc" style="text-align:center;margin:0;">暂无已临时授权的普通用户</text></view>';
    return;
  }
  wrap.innerHTML = meState.tempUsers.map(u => `
    <view class="admin-card-wrap">
      <view class="admin-delete" data-remove-tempuser="${escHtml(u.openid)}" style="opacity:0">
        <text class="delete-icon">🗑</text>
        <text>移除</text>
      </view>
      <view class="admin-card">
        <img class="admin-avatar" src="${u.avatar_url ? escHtml(u.avatar_url) : DEFAULT_AVATAR}">
        <view class="admin-info">
          <text class="admin-name">${esc(u.nickname || '用户')}</text>
          <view class="temp-user-sub">
            <text class="temp-user-key">${esc(u.license_key || '')}</text>
            <text class="temp-user-status ${u.status === 'active' ? 'active' : 'expired'}">${u.status === 'active' ? '授权中' : '已过期'}</text>
          </view>
        </view>
      </view>
    </view>`).join('');
  // 左滑移除（对齐小程序：滑动露出删除按钮）
  wrap.querySelectorAll('.admin-card-wrap').forEach(wrapEl => {
    const del = wrapEl.querySelector('.admin-delete');
    const card = wrapEl.querySelector('.admin-card');
    if (del && card) makeSwipeable(card, del, () => {}, { rubberOver: 55 });
  });
}

function removeTempUser(openid) {
  showConfirm('移除用户', '确定要移除该临时授权用户吗？移除后其授权记录将保留在历史授权用户中。', '移除', '#FF3B30', () => {
    api('/api/admin/remove_user', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ openid }),
    }).then(r => {
      if (r.data && r.data.success) { showToast('已移除'); animateRemoveTempUser(openid); }
      else showToast((r.data && r.data.message) || '移除失败');
    }).catch(() => showToast('网络错误'));
  });
}

// 三段离场动画（对齐小程序 onRemoveTempUser）：滑回收起 → 上移淡出 → 移除重渲染
function animateRemoveTempUser(openid) {
  const del = document.querySelector('#tempUserList [data-remove-tempuser="' + CSS.escape(openid) + '"]');
  const wrapEl = del ? del.parentElement : null;
  const card = wrapEl ? wrapEl.querySelector('.admin-card') : null;
  if (!card) { loadTempUsers(); return; }
  card.style.transition = 'transform 0.25s cubic-bezier(0.4, 0, 0.2, 1)';
  card.style.transform = 'translateX(0px)';
  if (del) del.style.opacity = '0';
  setTimeout(() => {
    wrapEl.classList.add('admin-exiting');
    setTimeout(() => { loadTempUsers(); }, 350);
  }, 180);
}

async function loadAdmins() {
  if (!state.token || !state.isSuperAdmin) return false;
  try {
    const r = await api('/api/admin/admins?page=1&page_size=50');
    if (r.data && r.data.success) {
      meState.admins = r.data.admins || [];
      renderAdmins();
      return true;
    }
  } catch (e) { /* 静默 */ }
  return false;
}

function renderAdmins() {
  const wrap = document.getElementById('adminList');
  const count = document.getElementById('adminCount');
  if (count) { count.style.display = meState.admins.length ? '' : 'none'; count.textContent = meState.admins.length; }
  if (!meState.admins.length) {
    wrap.innerHTML = '<view class="license-card"><text class="redeem-desc" style="text-align:center;margin:0;">暂无其他管理员</text></view>';
    return;
  }
  wrap.innerHTML = meState.admins.map(a => `
    <view class="admin-card-wrap">
      <view class="admin-delete" data-remove-admin="${escHtml(a.openid)}" style="opacity:0">
        <text class="delete-icon">🗑</text>
        <text>移除</text>
      </view>
      <view class="admin-card" data-admin-openid="${escHtml(a.openid)}" data-admin-nickname="${escHtml(a.nickname || '')}">
        <img class="admin-avatar" src="${a.avatar_url ? escHtml(a.avatar_url) : DEFAULT_AVATAR}">
        <view class="admin-info">
          <text class="admin-name">${esc(a.nickname || '用户')}</text>
          ${a.is_super ? '<text class="admin-badge">超级管理员</text>' : ''}
        </view>
      </view>
    </view>`).join('');
  // 左滑移除（对齐小程序：滑动露出删除按钮；纯点击卡片跳转任务列表）
  wrap.querySelectorAll('.admin-card-wrap').forEach(wrapEl => {
    const del = wrapEl.querySelector('.admin-delete');
    const card = wrapEl.querySelector('.admin-card');
    if (del && card) makeSwipeable(card, del, () => {}, { rubberOver: 55 });
  });
}

function removeAdmin(openid) {
  showConfirm('移除管理员', '确定要移除该管理员吗？', '移除', '#FF3B30', () => {
    api('/api/admin/remove_admin', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ openid }),
    }).then(r => {
      if (r.data && r.data.success) { showToast('已移除'); animateRemoveAdmin(openid); }
      else showToast((r.data && r.data.message) || '移除失败');
    }).catch(() => showToast('网络错误'));
  });
}

// 三段离场动画（对齐小程序 onRemoveAdmin）：
// ① 0.25s 滑回收起删除按钮 → ② 180ms 后 adminSlideOut 上移淡出 0.3s → ③ 350ms 后真正移除并重渲染
function animateRemoveAdmin(openid) {
  const card = document.querySelector('#adminList [data-admin-openid="' + CSS.escape(openid) + '"]');
  if (!card) { loadAdmins(); return; }
  const wrapEl = card.parentElement;
  const del = wrapEl.querySelector('.admin-delete');
  card.style.transition = 'transform 0.25s cubic-bezier(0.4, 0, 0.2, 1)';
  card.style.transform = 'translateX(0px)';
  if (del) del.style.opacity = '0';
  setTimeout(() => {
    wrapEl.classList.add('admin-exiting');
    setTimeout(() => { loadAdmins(); }, 350);
  }, 180);
}

/* ================= 子视图：历史授权用户 ================= */

function openAuthorizedUsers() {
  showView('view-authorized', '历史授权用户');
  loadAuthorizedUsers();
}

async function loadAuthorizedUsers() {
  const list = document.getElementById('authorizedUserList');
  list.innerHTML = '<view class="status-box"><text class="status-text">加载中...</text></view>';
  try {
    const r = await api('/api/authorized_users');
    if (r.data && r.data.success) {
      meState.authorizedUsers = (r.data.users || []).map(u => {
        u._expanded = false;
        // 管理员密钥的关联订单展开状态（按密钥 key 索引）+ 用户订单缓存
        u._expandedKeyOrders = {};
        u._ordersLoaded = false;
        u._ordersLoading = false;
        u._ordersCache = null;
        return u;
      });
      renderAuthorizedUsers();
    } else {
      list.innerHTML = '<view class="status-box"><text class="status-text">' + esc((r.data && r.data.message) || '加载失败') + '</text></view>';
    }
  } catch (e) {
    list.innerHTML = '<view class="status-box"><text class="status-text">网络错误</text></view>';
  }
}

function renderAuthorizedUsers() {
  const list = document.getElementById('authorizedUserList');
  if (!meState.authorizedUsers.length) {
    list.innerHTML = `
      <view class="empty-state">
        <view class="empty-illustration"><text class="empty-icon">👥</text></view>
        <text class="empty-title">暂无授权用户</text>
        <text class="empty-desc">生成许可密钥分享给他人后，这里会显示授权记录</text>
      </view>`;
    return;
  }
  const statusMap = { active: '临时授权中', expired: '已过期', removed: '已移除' };
  const keyStatusMap = { unused: '未使用', used: '已使用', revoked: '已作废', finished: '已完成', archived: '归档' };
  let html = `<view class="list-summary"><text class="summary-text">共 ${meState.authorizedUsers.length} 位授权用户</text></view>`;
  html += '<view class="user-list">' + meState.authorizedUsers.map(u => {
    const avatarChar = (u.nickname || '?')[0];
    const avatarUrl = u.avatar_url ? escHtml(u.avatar_url) + '?t=' + Date.now() : '';
    // 独立许可标签：按密钥类型去重（管理员在前），不合并成“管理员+临时”
    const records = u.records || u.keys || [];
    const typeSet = [];
    if (records.some(k => (k.type || 'temp') === 'admin')) typeSet.push('admin');
    if (records.some(k => (k.type || 'temp') === 'temp')) typeSet.push('temp');
    if (!typeSet.length) {
      // 兜底：老数据无 records 时按 license_type 字段
      if (u.license_type === 'admin' || u.license_type === 'both') typeSet.push('admin');
      if (u.license_type === 'temp' || u.license_type === 'both') typeSet.push('temp');
    }
    const licenseTagHtml = typeSet.map(t => {
      const label = t === 'admin' ? '管理员许可' : '临时许可';
      const cls = t === 'admin' ? 'tag-admin' : 'tag-temp';
      return `<text class="key-type-tag ${cls}">${label}</text>`;
    }).join('');
    // 永久管理员不再单独显示状态标签（许可类型标签已表达“管理员许可”）
    const statusLabel = u.status === 'permanent' ? '' : (statusMap[u.status] || u.status);
    const recordRows = records.length ? records.map(k => {
      const isAdminKey = (k.type || 'temp') === 'admin';
      const keyOrdersOpen = !!(u._expandedKeyOrders && u._expandedKeyOrders[k.key]);
      // 管理员密钥：不直接显示“关联订单是哪个”，改为显示数量 + 点击展开订单列表（对齐小程序）
      const orderLine = isAdminKey ? `
        <view class="record-line record-order-toggle" data-key-order-toggle="${escHtml(k.key)}" data-user-openid="${escHtml(u.openid)}">
          <text class="record-label">关联订单</text>
          <text class="record-value record-order-count">${u.order_count || 0} 个</text>
          <text class="record-order-arrow ${keyOrdersOpen ? 'arrow-up' : ''}">›</text>
        </view>
        <view class="record-orders ${keyOrdersOpen ? 'record-orders-expanded' : ''}" data-key-orders="${escHtml(k.key)}">
          <view class="record-orders-inner">${keyOrdersOpen ? renderRecordOrderContent(u) : ''}</view>
        </view>` : `
        <view class="record-line"><text class="record-label">关联订单</text><text class="record-value">${k.order_id ? ('订单 #' + k.order_id) : '空订单（有效期内未提交任务）'}</text></view>`;
      return `
      <view class="record-row">
        <view class="record-head">
          <text class="record-key">${esc(k.key)}</text>
          <text class="record-type ${isAdminKey ? 'tag-admin' : 'tag-temp'}">${isAdminKey ? '管理员许可' : '临时许可'}</text>
          <text class="record-status">${esc(keyStatusMap[k.status] || k.status)}</text>
        </view>
        <view class="record-line"><text class="record-label">创建时间</text><text class="record-value">${esc(k.created_at || '—')}</text></view>
        <view class="record-line"><text class="record-label">使用时间</text><text class="record-value">${esc(k.used_at || '—')}</text></view>
        <view class="record-line"><text class="record-label">到期时间</text><text class="record-value">${esc(k.expires_at || '—')}</text></view>
        ${orderLine}
      </view>`;
    }).join('') : '<view class="records-empty">无密钥记录</view>';
    return `
      <view class="user-card-item">
        <view class="user-card-main" data-user-openid="${escHtml(u.openid)}" data-user-nickname="${escHtml(u.nickname || '')}">
          <view class="user-avatar-circle">
            ${avatarUrl ? `<img class="user-avatar-img" src="${avatarUrl}" alt="头像">` : `<text class="user-avatar-char">${esc(avatarChar)}</text>`}
          </view>
          <view class="user-card-center">
            <text class="user-card-name">${esc(u.nickname || '未知用户')}</text>
            <view class="user-card-tags">
              ${licenseTagHtml}
              ${statusLabel ? `<text class="status-tag status-${esc(u.status)}">${esc(statusLabel)}</text>` : ''}
            </view>
            <text class="user-card-id">ID: ${esc(u.openid_short || u.openid)}</text>
          </view>
          <view class="user-card-right">
            <view class="user-stat"><text class="user-stat-num">${u.order_count || 0}</text><text class="user-stat-label">个订单</text></view>
            ${u.last_order ? `<text class="user-card-last">最近: ${esc(u.last_order)}</text>` : ''}
            <text class="role-btn-arrow">›</text>
          </view>
        </view>
        <view class="records-toggle" data-toggle-records="${escHtml(u.openid)}">
          <text class="records-toggle-text">密钥记录 (${records.length})</text>
          <text class="records-toggle-arrow ${u._expanded ? 'arrow-up' : ''}">›</text>
        </view>
        <view class="records-panel ${u._expanded ? 'records-expanded' : ''}">
          <view class="detail-divider"></view>
          ${recordRows}
        </view>
      </view>`;
  }).join('') + '</view>';
  list.innerHTML = html;
}

function toggleAuthorizedUser(openid) {
  const u = meState.authorizedUsers.find(x => x.openid === openid);
  if (!u) return;
  u._expanded = !u._expanded;
  // 原地切换类名让 CSS max-height/opacity 过渡真实播放（对齐小程序 onToggleRecords，避免整表重建导致过渡失效）
  const wrap = document.querySelector('#authorizedUserList [data-user-openid="' + CSS.escape(openid) + '"]');
  const item = wrap ? wrap.parentElement : null;
  if (item) {
    const panel = item.querySelector('.records-panel');
    const arrow = item.querySelector('.records-toggle-arrow');
    if (panel) panel.classList.toggle('records-expanded', u._expanded);
    if (arrow) arrow.classList.toggle('arrow-up', u._expanded);
  }
  measureAll(150);
  // 0.32s 过渡完成后再次测量：收起后内容变短，界面自然上移对齐小程序
  setTimeout(() => measureAll(320), 340);
}

// 管理员密钥的“关联订单”展开内容（加载中 / 空 / 列表）
function renderRecordOrderContent(u) {
  if (u._ordersLoading) return '<text class="record-orders-status">加载中...</text>';
  if (!u._ordersLoaded) return '<text class="record-orders-status">加载中...</text>';
  const orders = u._ordersCache || [];
  if (!orders.length) return '<text class="record-orders-status">暂无关联订单</text>';
  return orders.map(o => `
    <view class="record-order-item">
      <text class="record-order-no">${esc(o.order_number || ('#' + o.id))}</text>
      <text class="record-order-time">${esc(o.created_at || '')}</text>
      <text class="record-order-status">${esc(ORDER_STATUS_MAP[o.status] || o.status || '')}</text>
      <text class="record-order-price">¥${Number(o.total_price || 0).toFixed(2)}</text>
    </view>`).join('');
}

// 管理员密钥：展开/收起关联订单列表（首次展开拉取该用户订单，缓存复用）
async function toggleRecordOrders(openid, key) {
  const u = meState.authorizedUsers.find(x => x.openid === openid);
  if (!u) return;
  if (!u._expandedKeyOrders) u._expandedKeyOrders = {};
  const open = !u._expandedKeyOrders[key];
  u._expandedKeyOrders[key] = open;
  // 原地切换类名让 CSS max-height/opacity 下拉动画播放（对齐密钥记录面板）
  const wrap = document.querySelector('#authorizedUserList [data-key-orders="' + CSS.escape(key) + '"]');
  const arrow = document.querySelector('#authorizedUserList [data-key-order-toggle="' + CSS.escape(key) + '"] .record-order-arrow');
  if (wrap) wrap.classList.toggle('record-orders-expanded', open);
  if (arrow) arrow.classList.toggle('arrow-up', open);
  if (open && !u._ordersLoaded && !u._ordersLoading) {
    u._ordersLoading = true;
    const inner = wrap ? wrap.querySelector('.record-orders-inner') : null;
    if (inner) inner.innerHTML = '<text class="record-orders-status">加载中...</text>';
    try {
      const r = await api('/api/orders?openid=' + encodeURIComponent(openid) + '&per_page=50');
      u._ordersLoaded = true;
      u._ordersLoading = false;
      u._ordersCache = (r.data && r.data.success) ? (r.data.orders || []) : [];
      if (wrap && u._expandedKeyOrders[key]) {
        const i2 = wrap.querySelector('.record-orders-inner');
        if (i2) i2.innerHTML = renderRecordOrderContent(u);
        // 异步渲染后才开始展开动画 → 动画结束后重测滚动边界（收起时避免底部空白）
        measureAll(150);
        setTimeout(() => measureAll(320), 340);
      }
    } catch (e) {
      u._ordersLoaded = true;
      u._ordersLoading = false;
      u._ordersCache = [];
      if (wrap && u._expandedKeyOrders[key]) {
        const i3 = wrap.querySelector('.record-orders-inner');
        if (i3) i3.innerHTML = '<text class="record-orders-status">网络错误</text>';
      }
    }
  }
  measureAll(150);
  setTimeout(() => measureAll(220), 220);
  setTimeout(() => measureAll(320), 340);
}

/* ================= 子视图：用户订单 / 本地任务 ================= */

function openUserOrdersView(opts) {
  opts = opts || {};
  stopUserOrdersPolling();
  meState.userOrdersView = {
    openid: opts.openid || '',
    nickname: opts.nickname || '',
    source: opts.source || '',
    orders: [],
    page: 1, perPage: 10, total: 0, totalPages: 0, expanded: {},
    userDetail: null, loading: true,
    showLicenseDetail: false, showPageSizePicker: false,
  };
  const title = opts.source === 'local' ? '本地打印任务' : (opts.nickname ? opts.nickname + ' 的任务' : '订单列表');
  // 页内标题已移除（对齐小程序：标题由导航栏承载）
  document.getElementById('userOrdersUserCard').style.display = 'none';
  document.getElementById('uoLicenseDetail').classList.remove('license-detail-expanded');
  document.getElementById('userOrdersPager').style.display = 'none';
  document.getElementById('userOrdersPagerBottom').style.display = 'none';
  document.getElementById('userOrdersSummary').style.display = 'none';
  document.getElementById('userOrdersEndHint').style.display = 'none';
  showView('view-user-orders', title);
  loadUserOrders();
}

async function loadUserOrders(silent) {
  const v = meState.userOrdersView;
  const list = document.getElementById('userOrdersList');
  if (!silent) {
    v.loading = true;
    list.innerHTML = '<view class="status-box"><text class="status-text">加载中...</text></view>';
  }
  try {
    let url = '/api/orders?page=' + v.page + '&per_page=' + v.perPage;
    if (v.source === 'local') url += '&source=local';
    else if (v.openid) url += '&openid=' + encodeURIComponent(v.openid);
    const r = await api(url);
    if (r.status === 200 && r.data && r.data.success) {
      v.orders = (r.data.orders || []).map(normalizeOrder);
      v.total = r.data.total || 0;
      v.totalPages = Math.ceil(v.total / v.perPage);
      if (v.page > v.totalPages && v.totalPages > 0) v.page = v.totalPages;
      renderUserOrders();
    } else if (!silent) {
      list.innerHTML = '<view class="status-box"><text class="status-text">' + esc((r.data && r.data.message) || '加载失败') + '</text></view>';
    }
    if (v.openid && !v.userDetail) {
      const d = await api('/api/admin/user_detail?openid=' + encodeURIComponent(v.openid)).catch(() => null);
      if (d && d.data && d.data.success) {
        v.userDetail = d.data;
        renderUserOrdersUserCard();
      }
    }
  } catch (e) {
    if (!silent) list.innerHTML = '<view class="status-box"><text class="status-text">网络错误</text></view>';
  }
  v.loading = false;
  startUserOrdersPolling();
  measureAll(150);
}

function renderUserOrders() {
  const v = meState.userOrdersView;
  const list = document.getElementById('userOrdersList');
  if (v.loading && !v.orders.length) {
    list.innerHTML = '<view class="status-box"><text class="status-text">加载中...</text></view>';
  } else if (!v.orders.length) {
    list.innerHTML = `
      <view class="empty-state">
        <view class="empty-illustration"><text class="empty-icon">📋</text></view>
        <text class="empty-title">暂无任务</text>
        <text class="empty-desc">${v.source === 'local' ? '还没有本地打印任务记录' : '该用户还没有发起过打印任务'}</text>
      </view>`;
  } else {
    list.innerHTML = v.orders.map(o => orderCardHTML(o, !!v.expanded[o.id], undefined, o.openid === state.openid)).join('');
  }
  // 每页条数下拉
  syncUserOrdersPageSizePicker();
  // 页码（顶/底）
  const nums = buildUserOrdersPageNumbers();
  document.getElementById('userOrdersPageNumbers').innerHTML = nums;
  document.getElementById('userOrdersPageNumbersBottom').innerHTML = nums;
  document.getElementById('userOrdersPrev').classList.toggle('page-btn-disabled', v.page <= 1);
  document.getElementById('userOrdersNext').classList.toggle('page-btn-disabled', v.page >= v.totalPages);
  document.getElementById('userOrdersPrevBottom').classList.toggle('page-btn-disabled', v.page <= 1);
  document.getElementById('userOrdersNextBottom').classList.toggle('page-btn-disabled', v.page >= v.totalPages);
  document.getElementById('userOrdersPager').style.display = v.totalPages > 0 ? '' : 'none';
  document.getElementById('userOrdersPagerBottom').style.display = v.totalPages > 1 ? '' : 'none';
  // 总览
  const summary = document.getElementById('userOrdersSummary');
  if (v.total > 0) {
    summary.style.display = '';
    document.getElementById('userOrdersSummaryText').textContent = `共 ${v.total} 个订单，当前第 ${v.page}/${v.totalPages} 页`;
  } else summary.style.display = 'none';
  // 最后一页提示
  document.getElementById('userOrdersEndHint').style.display = (v.totalPages > 0 && v.page >= v.totalPages) ? '' : 'none';
  measureAll(150);
}

function syncUserOrdersPageSizePicker() {
  const v = meState.userOrdersView;
  document.getElementById('uoPageSizeText').textContent = v.perPage + '条/页';
  document.getElementById('uoPageSizeDropdown').classList.toggle('dropdown-show', v.showPageSizePicker);
  document.getElementById('uoPageSizeArrow').classList.toggle('arrow-up', v.showPageSizePicker);
  document.querySelectorAll('#uoPageSizeDropdown .page-size-option').forEach(opt => {
    opt.classList.toggle('option-active', parseInt(opt.dataset.size, 10) === v.perPage);
  });
}

function renderUserOrdersUserCard() {
  const v = meState.userOrdersView;
  const d = v.userDetail;
  const card = document.getElementById('userOrdersUserCard');
  if (!d) { card.style.display = 'none'; return; }
  card.style.display = '';
  const img = document.getElementById('uoAvatarImg');
  const char = document.getElementById('uoAvatarChar');
  if (d.avatar_url) {
    img.src = escHtml(d.avatar_url) + '?t=' + Date.now();
    img.style.display = '';
    char.style.display = 'none';
  } else {
    img.style.display = 'none';
    char.style.display = '';
    char.textContent = esc((d.nickname || '?')[0]);
  }
  document.getElementById('uoNickname').textContent = d.nickname || '未知用户';
  document.getElementById('uoRole').textContent = d.is_super ? '超级管理员'
    : d.role === 'admin' ? '管理员' : d.role === 'user' ? '普通用户' : '访客';
  const li = d.license_info;
  const badge = document.getElementById('uoLicenseBadge');
  const detail = document.getElementById('uoLicenseDetail');
  if (li) {
    const permanent = d.role === 'admin' || li.type === 'admin';
    badge.style.display = '';
    badge.className = 'license-badge ' + (permanent ? 'license-badge-admin' : li.expired ? 'license-badge-expired' : 'license-badge-temp');
    document.getElementById('uoLicenseKey').textContent = li.key;
    document.getElementById('uoLicenseValidity').textContent = permanent ? '永久' : (li.expired ? '已过期' : '临时授权');
    document.getElementById('uoLicenseStatus').textContent = permanent ? '永久' : (li.expired ? '已过期' : '有效');
    document.getElementById('uoLicenseKeyFull').textContent = li.key || '—';
    document.getElementById('uoLicenseType').textContent = li.type === 'admin' ? '管理员许可' : '临时许可';
    document.getElementById('uoLicenseCreator').textContent = li.creator_nickname || '—';
    document.getElementById('uoLicenseCreated').textContent = li.created_at || '—';
    document.getElementById('uoLicenseUsed').textContent = li.used_at || '—';
    document.getElementById('uoLicenseExpires').textContent = li.expires_at || '—';
    document.getElementById('uoLicenseValidityDetail').textContent = li.validity_minutes ? li.validity_minutes + ' 分钟' : '—';
  } else {
    badge.style.display = 'none';
    detail.classList.remove('license-detail-expanded');
  }
}

function buildUserOrdersPageNumbers() {
  const total = meState.userOrdersView.totalPages;
  const current = meState.userOrdersView.page;
  // 对齐小程序：仅 1 页时也显示当前页数字（高亮），避免分页栏只有 ‹ › 箭头
  if (total <= 1) {
    return '<view class="page-num page-num-active" data-page-num="1">1</view>';
  }
  const pages = [];
  if (total <= 7) { for (let i = 1; i <= total; i++) pages.push(i); }
  else {
    pages.push(1);
    if (current > 3) pages.push('...');
    for (let i = Math.max(2, current - 1); i <= Math.min(total - 1, current + 1); i++) pages.push(i);
    if (current < total - 2) pages.push('...');
    pages.push(total);
  }
  return pages.map(p => {
    if (p === '...') return '<view class="page-ellipsis">…</view>';
    return `<view class="page-num ${p === current ? 'page-num-active' : ''}" data-page-num="${p}">${p}</view>`;
  }).join('');
}

function changeUserOrdersPage(page) {
  const v = meState.userOrdersView;
  if (page < 1 || page > v.totalPages || page === v.page) return;
  v.page = page;
  loadUserOrders();
  scrollUserOrdersToSection(); // 切页回订单区顶部（对齐小程序 _scrollToOrdersSection，280ms 动画）
}

// 滚动到订单区域顶部（对齐小程序 user-orders 页：offset -12 / dur 280 / easeOutCubic）
function scrollUserOrdersToSection() {
  const engine = scrollEngines.userOrders;
  if (!engine) return;
  const section = document.querySelector('#view-user-orders .orders-section');
  if (!section || !engine.el) return;
  engine.measure();
  if (engine.y > engine.maxY) {
    engine.cancel();
    engine.y = engine.maxY;
    engine.applyY();
  }
  if (engine.maxY <= 0) return;
  // 用未被 transform 的 scroller 矩形计算（对齐小程序 WXS 公式，避免 +y 漂移）
  const offset = section.getBoundingClientRect().top - engine.el.getBoundingClientRect().top + engine.y;
  const target = Math.min(Math.max(0, offset - 12), engine.maxY);
  if (Math.abs(target - engine.y) < 1) return;
  engine.scrollTo(target, 280);
}

function toggleUserOrdersOrder(id) {
  const v = meState.userOrdersView;
  if (v.expanded[id]) delete v.expanded[id];
  else v.expanded[id] = true;
  // 原地切换类名让 CSS 过渡播放（与"我的打印任务"一致）
  const card = document.querySelector('#userOrdersList [data-order-id="' + id + '"]');
  if (card) {
    const expanded = !!v.expanded[id];
    card.classList.toggle('order-expanded', expanded);
    const detail = card.querySelector('.order-card-detail');
    if (detail) detail.classList.toggle('detail-expanded', expanded);
    const arrow = card.querySelector('.order-arrow');
    if (arrow) arrow.classList.toggle('arrow-up', expanded);
  }
  measureAll(150);
  // 收起动画（0.3s）期间/结束后多次重测：中途先跟一版缩小白屏窗口，结束后测最终值
  setTimeout(() => measureAll(220), 220);
  setTimeout(() => measureAll(320), 340);
}

// 选择条数后滚动回订单列表顶部（对齐小程序 _scrollToOrdersSection）
function scrollUserOrdersToTop() {
  const engine = scrollEngines.userOrders;
  const section = document.querySelector('#view-user-orders .orders-section');
  if (!engine || !section || !engine.el) return;
  engine.measure(); // 用最新内容高度更新 maxY
  // 内容不足一屏：不滚动，保持原位
  if (engine.contentH <= engine.scrollerH) return;
  // 用未被 transform 的 scroller 矩形计算（对齐小程序 WXS 公式，避免 +y 漂移）
  const top = section.getBoundingClientRect().top - engine.el.getBoundingClientRect().top + engine.y;
  const target = Math.min(Math.max(0, top - 12), engine.maxY);
  if (Math.abs(target - engine.y) < 2) return;
  engine.scrollTo(target, 280);
}

let _userOrdersPollTimer = null;

function startUserOrdersPolling() {
  if (!meState.userOrdersView || _userOrdersPollTimer) return;
  _userOrdersPollTimer = setInterval(() => {
    const view = document.getElementById('view-user-orders');
    if (!view || view.style.display === 'none') { stopUserOrdersPolling(); return; }
    loadUserOrders(true);
  }, 10000);
}

function stopUserOrdersPolling() {
  if (_userOrdersPollTimer) {
    clearInterval(_userOrdersPollTimer);
    _userOrdersPollTimer = null;
  }
}

/* ================= 启动 ================= */

window.addEventListener('pagehide', () => { stopOrderPolling(); stopUserOrdersPolling(); stopKeyPolling(); stopTempCountdown(); });
