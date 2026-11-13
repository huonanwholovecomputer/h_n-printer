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

const _adminLoadTimes = { keys: 0, tempUsers: 0, storage: 0, security: 0, admins: 0, recentOrders: 0 };
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
  // S2-08（D-7）：「我的文件」弹窗（查看保留期 + 删除自己的文件）
  myFiles: [],
  myFilesPage: 0,
  myFilesTotal: 0,
  myFilesLoading: false,
  // 2026-11-12：当前展开详情的文件 id（同一时间只展开一个；空串 = 全部收起）
  myFilesDetailId: '',
  // 2026-11-13：多页预览的翻页状态（file_id → {page, pages, infoPending, infoFailed}）。
  //   pages = 0 表示页数未知（列表项的 preview_pages 是 0），要问一次 /api/file_preview/<id>/info
  filePreview: {},
  // 2026-11-13：订单「文件列表」点开的独立详情弹窗（正在看的文件 / 是否在加载）
  fileDetailId: '',
  fileDetailFile: null,
  fileDetailLoading: false,
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
  // 「近 N 条订单」卡片（2026-11，仅管理员/超管）：N 由超管设置（接口回传，默认 10）
  recentOrders: [],
  recentOrdersLimit: 10,
  recentOrdersLoading: false,
  // 超管的 N 步进器状态（就地展开，不做大弹窗）
  recentOrdersLimitEditing: false,
  recentOrdersLimitDraft: 10,
  recentOrdersLimitSaving: false,
  userOrdersView: { openid: '', nickname: '', source: '', orders: [], page: 1, perPage: 10, total: 0, totalPages: 0, expanded: {} },
};

/* ================= 初始化 / Tab 刷新 ================= */

function initMePage() {
  setupMeButtons();
  setupMyFiles();
  setupMyFilePreviewUI();
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
    // 「近 N 条订单」：每次进入「我的」页（refreshMeUI 的既有入口）按 30s 新鲜度拉一次；
    // 非管理员在 loadRecentOrders 内部直接 return（不发请求），整块也保持收起。
    loadIfStale('recentOrders', loadRecentOrders);
    if (state.isSuperAdmin) loadIfStale('admins', loadAdmins);
  }
  // 超管的「设置 N」入口显隐（非超管置 none；切换身份/退出登录后也要跟着收回去）
  renderRecentOrderLimitEditor();
  measureAll(150);
}

function updateRoleActions() {
  const admin = state.role === 'admin';
  document.getElementById('btnAuthorizedUsers').style.display = admin ? '' : 'none';
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
  // 「近 N 条订单」跟着管理员身份展开/收起（非管理员即使残留数据也不露出来）
  setBlock('recentOrdersBlock', isAdmin);
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
    // 2026-10-27 用户要求：选完先裁剪（拖动/缩放选区域），确认后才上传
    if (file) openAvatarCrop(file);
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
    // 合计价格 → 复制气泡（Popconfirm）。必须挡在卡片折叠之前，否则点价格会把详情收起
    const priceTap = e.target.closest('[data-price-copy]');
    if (priceTap) { e.stopPropagation(); openPricePop(priceTap, priceTap.dataset.priceCopy); return; }
    // 异常状态文字 → 原因气泡（同样必须挡在折叠之前）
    const reasonTap = e.target.closest('[data-status-reason]');
    if (reasonTap) { e.stopPropagation(); openStatusReasonPop(reasonTap, reasonTap.dataset.statusReason); return; }
    // 文件列表里的一行 → 该文件的独立详情弹窗（2026-11-13）。同样必须挡在卡片折叠之前，
    // 否则点文件行会先把整个订单卡收起来。没有 file_id 的老数据点了给一句 toast。
    const fileTap = e.target.closest('[data-order-file]');
    if (fileTap) {
      e.stopPropagation();
      const fid = fileTap.dataset.orderFile || '';
      if (fid) openOrderFileDetail(fid);
      else showToast('该文件已清理');
      return;
    }
    const card = e.target.closest('[data-order-id]');
    if (card) toggleOrder(parseInt(card.dataset.orderId, 10));
  });
  // 「近 N 条订单」卡片（仅管理员渲染）：价格 → 复用订单卡片的复制气泡；
  // 归属人 → 脱敏/真名就地切换（不重渲染整块，避免动画/滚动位置跳动）
  const recentList = document.getElementById('recentOrderList');
  if (recentList) recentList.addEventListener('click', (e) => {
    const priceEl = e.target.closest('[data-recent-price]');
    if (priceEl) { e.stopPropagation(); openPricePop(priceEl, priceEl.dataset.recentPrice); return; }
    const ownerEl = e.target.closest('[data-recent-owner]');
    if (ownerEl) {
      e.stopPropagation();
      const masked = ownerEl.dataset.ownerMasked || '';
      const full = ownerEl.dataset.ownerFull || '';
      ownerEl.textContent = (ownerEl.textContent === full) ? masked : full;
    }
  });
  // 超管专属：N 设置入口 + 就地步进器（−/+、保存、取消）。非超管的分支全在函数内再判一次。
  const limitEntryEl = document.getElementById('recentOrderLimitEntry');
  if (limitEntryEl) limitEntryEl.addEventListener('click', (e) => {
    e.stopPropagation();
    if (!state.isSuperAdmin) return;
    openRecentOrdersLimitEditor(!meState.recentOrdersLimitEditing);
  });
  const limitEditorEl = document.getElementById('recentOrderLimitEditor');
  if (limitEditorEl) limitEditorEl.addEventListener('click', (e) => {
    e.stopPropagation();
    if (!state.isSuperAdmin) return;
    const step = e.target.closest('[data-limit-step]');
    if (step) { stepRecentOrdersLimit(parseInt(step.dataset.limitStep, 10)); return; }
    if (e.target.closest('#recentOrderLimitSave')) { saveRecentOrdersLimit(); return; }
    if (e.target.closest('#recentOrderLimitCancel')) { openRecentOrdersLimitEditor(false); }
  });
  document.getElementById('ordersPageNumbers').addEventListener('click', (e) => {
    const num = e.target.closest('[data-page-num]');
    if (num && num.dataset.pageNum !== '...') changeOrdersPage(parseInt(num.dataset.pageNum, 10));
  });
  document.getElementById('btnAuthorizedUsers').addEventListener('click', openAuthorizedUsers);
  document.getElementById('btnBindAccount').addEventListener('click', openBindView);
  const btnCheckUpdate = document.getElementById('btnCheckUpdate');
  if (btnCheckUpdate) btnCheckUpdate.addEventListener('click', () => checkAppUpdate(true));
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
    // 同一张订单卡片模板：合计价格也可复制（管理员在「用户订单」子视图里同样能用）
    const priceTap = e.target.closest('[data-price-copy]');
    if (priceTap) { e.stopPropagation(); openPricePop(priceTap, priceTap.dataset.priceCopy); return; }
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

/* ================= 头像裁剪（2026-10-27 用户要求） =================
 * 与小程序同款：选图后弹圆形取景框，拖动/双指/滑杆缩放，确认时按 AvatarCrop.cropRect
 * 的源矩形画进 512×512 的 canvas，导出 JPEG 再走原来的 /api/profile 上传。
 * 几何在 www/avatar_crop.js（纯函数，验收脚本与小程序那份同用例比对）。
 * 任何一步失败都退回上传原图（裁剪是增强，不能因为它坏了就换不了头像）。
 */
const cropState = { file: null, url: '', img: null, vp: 240, fit: 1, scale: 1, x: 0, y: 0, drag: null, pinch: 0 };

function _cropEl(id) { return document.getElementById(id); }

function _cropImageSize() {
  return { width: (cropState.img && cropState.img.naturalWidth) || 1, height: (cropState.img && cropState.img.naturalHeight) || 1 };
}

function _applyCropState(st) {
  cropState.scale = st.scale;
  cropState.x = st.x;
  cropState.y = st.y;
  const img = _cropEl('cropImg');
  if (img) {
    const size = _cropImageSize();
    img.style.width = (size.width * st.scale) + 'px';
    img.style.height = (size.height * st.scale) + 'px';
    img.style.left = st.x + 'px';
    img.style.top = st.y + 'px';
  }
  const slider = _cropEl('cropZoom');
  if (slider) {
    const pct = Math.round((st.scale / (cropState.fit || 1)) * 100);
    slider.value = String(Math.max(100, Math.min(Number(slider.max) || 400, pct)));
  }
}

function openAvatarCrop(file) {
  if (!file) return;
  if (cropState.url) { try { URL.revokeObjectURL(cropState.url); } catch (e) {} }
  cropState.file = file;
  cropState.url = URL.createObjectURL(file);
  const probe = new Image();
  probe.onload = () => {
    cropState.img = probe;
    const vp = 240;
    const st = AvatarCrop.fitState(vp, { width: probe.naturalWidth, height: probe.naturalHeight });
    cropState.vp = vp;
    cropState.fit = st.scale;
    const view = _cropEl('cropImg');
    if (view) view.src = cropState.url;
    _applyCropState(st);
    const mask = _cropEl('cropMask');
    if (mask) mask.style.display = 'flex';
    setupAvatarCropHandlers();
  };
  probe.onerror = () => { showToast('读取图片失败'); uploadAvatar(file); };
  probe.src = cropState.url;
}

function closeAvatarCrop() {
  const mask = _cropEl('cropMask');
  if (mask) mask.style.display = 'none';
  const view = _cropEl('cropImg');
  if (view) view.removeAttribute('src');
  if (cropState.url) { try { URL.revokeObjectURL(cropState.url); } catch (e) {} }
  cropState.url = '';
  cropState.img = null;
  cropState.drag = null;
  cropState.pinch = 0;
}

let cropHandlersBound = false;

function setupAvatarCropHandlers() {
  if (cropHandlersBound) return;
  const box = _cropEl('cropBox');
  if (!box) return;
  cropHandlersBound = true;
  _cropEl('cropCancel').addEventListener('click', closeAvatarCrop);
  _cropEl('cropOk').addEventListener('click', confirmAvatarCrop);
  _cropEl('cropMask').addEventListener('click', (e) => { if (e.target && e.target.id === 'cropMask') closeAvatarCrop(); });
  _cropEl('cropZoom').addEventListener('input', (e) => {
    const img = _cropImageSize();
    const fit = cropState.fit || 1;
    const cur = cropState.scale || fit;
    const want = fit * ((Number(e.target.value) || 100) / 100);
    _applyCropState(AvatarCrop.zoomAt({ scale: cur, x: cropState.x, y: cropState.y }, cropState.vp, img, want / cur));
  });
  // 单指/鼠标拖动
  box.addEventListener('pointerdown', (e) => {
    if (!cropState.img) return;
    cropState.drag = { x: cropState.x, y: cropState.y, scale: cropState.scale, sx: e.clientX, sy: e.clientY };
    box.setPointerCapture && box.setPointerCapture(e.pointerId);
  });
  box.addEventListener('pointermove', (e) => {
    const d = cropState.drag;
    if (!d || cropState.pinch) return;
    _applyCropState(AvatarCrop.clamp({
      scale: d.scale,
      x: d.x + (e.clientX - d.sx),
      y: d.y + (e.clientY - d.sy),
    }, cropState.vp, _cropImageSize()));
  });
  const endDrag = () => { cropState.drag = null; };
  box.addEventListener('pointerup', endDrag);
  box.addEventListener('pointercancel', endDrag);
  box.addEventListener('pointerleave', endDrag);
  // 双指缩放
  const dist = (t1, t2) => Math.sqrt(Math.pow(t1.clientX - t2.clientX, 2) + Math.pow(t1.clientY - t2.clientY, 2));
  box.addEventListener('touchstart', (e) => {
    if (e.touches.length > 1) {
      cropState.pinch = dist(e.touches[0], e.touches[1]);
      cropState.drag = null;
    }
  }, { passive: true });
  box.addEventListener('touchmove', (e) => {
    if (e.touches.length > 1 && cropState.pinch > 0) {
      const d = dist(e.touches[0], e.touches[1]);
      _applyCropState(AvatarCrop.zoomAt({ scale: cropState.scale, x: cropState.x, y: cropState.y },
        cropState.vp, _cropImageSize(), d / cropState.pinch));
      cropState.pinch = d;
      e.preventDefault();
    }
  }, { passive: false });
  box.addEventListener('touchend', (e) => {
    if (!e.touches || e.touches.length < 2) cropState.pinch = 0;
  });
}

function confirmAvatarCrop() {
  const img = cropState.img;
  const original = cropState.file;
  if (!img) { closeAvatarCrop(); return; }
  const size = AvatarCrop.OUTPUT_SIZE;
  const rect = AvatarCrop.cropRect({ scale: cropState.scale, x: cropState.x, y: cropState.y },
    cropState.vp, _cropImageSize());
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#FFFFFF';
  ctx.fillRect(0, 0, size, size);
  try {
    ctx.drawImage(img, rect.sx, rect.sy, rect.sw, rect.sh, 0, 0, size, size);
  } catch (e) {
    closeAvatarCrop();
    if (original) uploadAvatar(original);
    return;
  }
  if (typeof canvas.toBlob !== 'function') {
    closeAvatarCrop();
    if (original) uploadAvatar(original);
    return;
  }
  canvas.toBlob((blob) => {
    let out = null;
    try {
      if (blob) out = new File([blob], 'avatar_' + Date.now() + '.jpg', { type: 'image/jpeg' });
    } catch (e) { out = null; }
    closeAvatarCrop();
    uploadAvatar(out || original);
  }, 'image/jpeg', 0.92);
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
/* 订单级「合计页数 / 合计份数 / 合计张数」的展示文案（2026-11-07 用户要求，与小程序同口径）。
   历史口径：Excel 订单一律写「不适用」——那是页数分析还不覆盖表格的年代（后端只给兜底 1 页，
   显示出来是假数）。现在表格也会经本地工具回报真实页数（order_files.page_count →
   后端 effective_pages / total_pages / total_sheets），再一律「不适用」就是把已知信息藏起来。
   改为数据驱动：后端给了数就显示；确实未知（page_count=0 ⇒ total_* 也是 0）才写「不适用」，
   绝不拿兜底的 1 页充数。Excel 与其它格式走同一条规则，不再按扩展名分叉。 */
function orderTotalsText(o) {
  const pages = Number(o.total_pages) || 0;
  const copies = Number(o.total_copies) || Number(o.copies) || 0;
  const sheets = Number(o.total_sheets) || Number(o.totalSheetsDisplay) || 0;
  return {
    pages: pages > 0 ? pages + ' 页' : '不适用',
    copies: copies > 0 ? copies + ' 份' : '不适用',
    sheets: sheets > 0 ? sheets + ' 张' : '不适用',
  };
}

function normalizeOrder(o) {
  o.totalPriceDisplay = Number(o.total_price || 0).toFixed(2);
  if (o.files) {
    o.files.forEach(f => {
      f.sizeDisplay = f.size ? (f.size / 1024).toFixed(1) + ' KB' : '';
      const name = (f.original_name || f.file_name || '').toLowerCase();
      f.isExcel = name.endsWith('.xls') || name.endsWith('.xlsx');
      // 有效页数/张数（后端字段优先，缺失时前端兜底）：
      // 有效页 = 页码范围过滤后页数；张数 = 单面每页 1 张、双面每 2 页 1 张（奇数页最后一张仍占 1 张）
      const pc = Number(f.page_count) || 0;
      const copies = Number(f.copies) || 1;
      const eff = typeof f.effective_pages === 'number' ? f.effective_pages : countPagesInRange(f.page_range, pc);
      // 张数口径（契约 §2）：S 按"该页是否属于 X"切段 —— 单面段每页 1 张 + 双面段两页 1 张，
      // 「封面单独打印」的页各自独占一张。不能再简单 ceil(eff/2)，否则混排会少算张。
      let perSheets;
      if (typeof f.sheets === 'number') perSheets = f.sheets;
      else if (f.duplex === 'off') perSheets = eff;
      else if (typeof chargeUnits === 'function') {
        const units = chargeUnits(pageRangeSet(f.page_range, pc), pageSetFrom(f.simplex_pages || '', pc, false, false));
        perSheets = units.sheets;
        if (!perSheets && eff > 0) perSheets = Math.ceil(eff / 2);   // 口径函数空转时退回老口径
      } else perSheets = Math.ceil(eff / 2);                          // print.js 未加载时的兜底
      f.effectivePages = eff;
      f.totalSheets = typeof f.total_sheets === 'number' ? f.total_sheets : (perSheets * copies);
      f.pagesLine = copies + ' 份 × ' + eff + ' 页';
    });
    o.isExcel = o.files.length > 0 && o.files.every(f => f.isExcel);
    const totalSheets = typeof o.total_sheets === 'number'
      ? o.total_sheets
      : o.files.reduce((s, f) => s + (f.totalSheets || 0), 0);
    o.totalSheetsDisplay = String(totalSheets);
  } else {
    o.totalSheetsDisplay = '0';
  }
  // 合计页数/份数/张数（数据驱动；Excel 不再一律「不适用」）
  const totalsText = orderTotalsText(o);
  o.statPages = totalsText.pages;
  o.statCopies = totalsText.copies;
  o.statSheets = totalsText.sheets;
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
    // 2026-11-13：文件行可点 → 打开该文件的独立详情弹窗（字段见 openOrderFileDetail）。
    // 没有 file_id 的老数据标 data-order-file=""（空串），点了只给 toast「该文件已清理」。
    const rowFid = f.file_id ? String(f.file_id) : '';
    fileRows += `
      <view class="detail-file-row${rowFid ? ' detail-file-row-tap' : ''}" data-order-file="${esc(rowFid)}">
        <view class="detail-file-info">
          <text class="detail-file-name">${esc(f.original_name || f.file_name || '')}</text>
          <view class="detail-file-tags">
            ${f.file_type ? `<text class="file-type-tag">${esc(f.file_type)}</text>` : ''}
            ${f.sizeDisplay ? `<text class="file-size-tag">${esc(f.sizeDisplay)}</text>` : ''}
          </view>
        </view>
        <view class="detail-file-right">
          <text class="detail-file-copies">${f.pagesLine} | ${f.totalSheets} 张</text>
          <text class="detail-file-range">${f.duplex === 'on' ? '双面' : '单面'}</text>
          ${f.page_range ? `<text class="detail-file-range">范围: ${esc(f.page_range)}</text>` : ''}
          ${f.simplex_pages ? `<text class="detail-file-range">单面页: ${esc(f.simplex_pages)}</text>` : ''}
          ${Number(f.blank_tail_exclude) === 1 ? `<text class="detail-file-range">已排除末尾空白页</text>` : ''}
          ${(f.status === 'rejected' || f.status === 'failed') && f.reject_reason ? `<text class="file-reject-reason">${esc(f.reject_reason)}</text>` : ''}
          ${rowFid ? '<text class="detail-file-open-tip">详情 ›</text>' : ''}
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
        <view class="detail-row"><text class="detail-label">合计页数</text><text class="detail-value">${o.statPages}</text></view>
        <view class="detail-row"><text class="detail-label">合计份数</text><text class="detail-value">${o.statCopies}</text></view>
        <view class="detail-row"><text class="detail-label">合计张数</text><text class="detail-value">${o.statSheets}</text></view>
      </view>
      ${licenseRows}
      ${(o.files && o.files.length) ? `<view class="detail-section"><view class="detail-section-title">文件列表 (${o.files.length})${o.files.length > 5 ? '<text class="detail-files-hint">· 列表可上下滑动</text>' : ''}</view><view class="detail-files-scroll">${fileRows}</view></view>` : ''}
      ${extRows}
      ${remarkRows}
      <view class="detail-section">
        <view class="detail-row detail-price-row"><text class="detail-label">合计价格</text><view class="detail-price-tap" data-price-copy="${o.id}"><text class="detail-price-value status-${o.status || 'queued'}${(o.is_admin_print && o.status === 'sent') ? ' is-self-sent' : ''}">¥${o.totalPriceDisplay || '0.00'}</text><text class="price-copy-icon">📋</text></view></view>
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
          ${isReasonStatus(o.status)
            ? `<text class="order-status ${statusCls} status-reason-tap" data-status-reason="${o.id}">${esc(statusText)} ⓘ</text>`
            : `<text class="order-status ${statusCls}">${esc(statusText)}</text>`}
          ${o.is_admin_print ? '<text class="order-self-print-badge">👤自打</text>' : ''}
        </view>
        <text class="order-number-label">${esc(o.file_summary || o.file || '')}</text>
        <view class="order-card-meta">
          <text class="order-card-stat">📄 ${o.statPages}</text>
          <text class="order-card-stat">📋 ${o.statCopies}</text>
          <text class="order-card-stat">🖨 ${o.statSheets}</text>
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

/* ================= 近 N 条订单（管理员/超管，2026-11） =================
   需求：管理员在「我的」页一眼看到最近 N 条**所有人**的订单（含归属人），价格可复制。
   - 数据：GET /api/admin/recent_orders → { success, limit, orders:[{order_id, order_number,
     owner, owner_masked, created_at, file_count, page_count, total_price, status, source,
     is_admin_print}] }。limit 由超管在小程序端设置（默认 10），这里只读；
     /api/admin/recent_orders_limit 是给设置面板用的，本卡片不拉。
   - 触发：进入「我的」页走 refreshMeUI 的既有管理员分支 → loadIfStale('recentOrders', …)
     （30s 新鲜度），与订单列表同一节奏，**不轮询**（APP 无下拉刷新，切 tab 即刷新）。
   - 非管理员（含游客/普通用户）：整块 admin-collapsible 保持收起 + 这里直接 return，
     一个请求都不发。超管在 compute_role 里也是 role==='admin'，所以判据只认 'admin'。
   - 失败静默保留上一次渲染（管理员看板不该因为一次超时闪成空；loadIfStale 会清时间戳重试）。
   - 复制价格直接复用订单卡片那套 openPricePop（同一气泡、同一剪贴板口径）：行数据登记进
     _priceOrderById 的检索池即可，不另写一套剪贴板逻辑。 */

// created_at 只显示「月-日 时:分」；格式不认识就原样显示（后端改格式也不会显示成空白）
function _recentOrderTime(s) {
  const m = String(s || '').match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  return m ? (m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5]) : String(s || '');
}

// 订单来源只做展示，不改写订单来源字段（硬规矩：来源一致性，双端闸门）
function _recentOrderSource(s) {
  if (s === 'cloud') return '云端';
  if (s === 'local') return '本地';
  return '';
}

async function loadRecentOrders() {
  if (state.role !== 'admin') return false;   // 非管理员：不发请求
  if (meState.recentOrdersLoading) return false;
  meState.recentOrdersLoading = true;
  try {
    const r = await api('/api/admin/recent_orders');
    const data = (r && r.data) || {};
    if (!data.success || !Array.isArray(data.orders)) return false;
    const lim = Math.floor(Number(data.limit));
    meState.recentOrdersLimit = (isFinite(lim) && lim > 0) ? lim : 10;
    meState.recentOrders = data.orders.map(o => ({
      // openPricePop 按 o.id 回查，所以把 order_id 映射成 id；价格/订单号口径与订单卡片一致
      id: (o && o.order_id !== undefined) ? o.order_id : '',
      order_number: (o && o.order_number) || '',
      total_price: Number(o && o.total_price) || 0,
      files: [],   // 契约不下发逐文件明细 → 「复制详细价格」只出标题+合计（不编造分项）
      owner: (o && o.owner) || '',
      owner_masked: (o && o.owner_masked) || '',
      created_at: (o && o.created_at) || '',
      file_count: Number(o && o.file_count) || 0,
      page_count: Number(o && o.page_count) || 0,
      status: (o && o.status) || '',
      source: (o && o.source) || '',
      is_admin_print: !!(o && o.is_admin_print),
    }));
    renderRecentOrders();
    return true;
  } catch (e) {
    return false;   // 静默失败：保留上一次渲染
  } finally {
    meState.recentOrdersLoading = false;
  }
}

function renderRecentOrders() {
  const box = document.getElementById('recentOrderList');
  if (!box) return;
  const title = document.getElementById('recentOrdersTitle');
  if (title) title.textContent = '近 ' + (meState.recentOrdersLimit || 10) + ' 条订单';
  const rows = meState.recentOrders || [];
  const count = document.getElementById('recentOrderCount');
  if (count) {
    count.textContent = String(rows.length);
    count.style.display = rows.length ? '' : 'none';
  }
  // 2026-11 #3：列表搬进独立子视图（支持真正的分页），这里只留一张入口卡 ——
  // 位置不变、加载逻辑不变，点进去看全部。
  const limit = meState.recentOrdersLimit || 10;
  box.innerHTML = `<view class="recent-orders-entry" onclick="openRecentOrdersView()">
      <text class="recent-orders-entry-icon">🧾</text>
      <view class="recent-orders-entry-main">
        <text class="recent-orders-entry-title">查看最近订单</text>
        <text class="recent-orders-entry-desc">全部管理员的最近 ${limit} 条${rows.length ? '，当前取到 ' + rows.length + ' 条' : ''}，支持分页翻看</text>
      </view>
      <text class="recent-orders-entry-arrow">›</text>
    </view>`;
  measureAll(150);
}

/* ---------- 超管设置 N（1..100） ----------
   用户原话「近{超管设置的数字（默认 10）}条订单」⇒ N 必须能改，不能只读接口值。
   写接口 POST /api/admin/recent_orders_limit 只对超管开放（非超管 403）；入口本身也
   只对超管渲染 —— 判据用 state.isSuperAdmin，**不能**写 role === 'super_admin'
   （后端 compute_role() 只返回 admin/user/guest，超管身份走 /api/login 的 is_super_admin）。
   保存失败保持旧值，只用轻提示，不弹遮罩。 */
const RECENT_ORDER_LIMIT_MIN = 1;
const RECENT_ORDER_LIMIT_MAX = 100;

function _clampRecentOrdersLimit(n) {
  const v = Math.round(Number(n));
  if (!isFinite(v)) return RECENT_ORDER_LIMIT_MIN;
  return Math.max(RECENT_ORDER_LIMIT_MIN, Math.min(RECENT_ORDER_LIMIT_MAX, v));
}

function renderRecentOrderLimitEditor() {
  const isSuper = !!state.isSuperAdmin;
  // 身份变了（退出登录 / 被降权）→ 编辑器强制收起，不能留个能点的空壳
  if (!isSuper && meState.recentOrdersLimitEditing) meState.recentOrdersLimitEditing = false;
  const entry = document.getElementById('recentOrderLimitEntry');
  if (entry) entry.style.display = isSuper ? '' : 'none';
  const editor = document.getElementById('recentOrderLimitEditor');
  if (editor) editor.style.display = (isSuper && meState.recentOrdersLimitEditing) ? '' : 'none';
  const value = document.getElementById('recentOrderLimitValue');
  if (value) value.textContent = String(meState.recentOrdersLimitDraft);
  const save = document.getElementById('recentOrderLimitSave');
  if (save) save.classList.toggle('recent-order-limit-save-disabled', !!meState.recentOrdersLimitSaving);
}

function openRecentOrdersLimitEditor(open) {
  if (!state.isSuperAdmin) return;   // 非超管：一个像素都不动
  meState.recentOrdersLimitEditing = open !== false;
  if (meState.recentOrdersLimitEditing) meState.recentOrdersLimitDraft = meState.recentOrdersLimit || 10;
  renderRecentOrderLimitEditor();
  measureAll(150);
}

function stepRecentOrdersLimit(delta) {
  if (!state.isSuperAdmin) return;
  meState.recentOrdersLimitDraft = _clampRecentOrdersLimit((meState.recentOrdersLimitDraft || 10) + (Number(delta) || 0));
  renderRecentOrderLimitEditor();
}

async function saveRecentOrdersLimit() {
  if (!state.isSuperAdmin) return false;
  if (meState.recentOrdersLimitSaving) return false;
  const limit = _clampRecentOrdersLimit(meState.recentOrdersLimitDraft);
  meState.recentOrdersLimitSaving = true;
  renderRecentOrderLimitEditor();
  try {
    const r = await api('/api/admin/recent_orders_limit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ limit: limit }),
    });
    const data = (r && r.data) || {};
    if (!data.success) {
      // 失败保持旧值：不关编辑器、不改标题，只轻提示（403 / 400 都走这条）
      showToast(data.message || '保存失败，请稍后重试', 2500);
      return false;
    }
    const saved = _clampRecentOrdersLimit(data.limit === undefined ? limit : data.limit);
    meState.recentOrdersLimit = saved;
    meState.recentOrdersLimitDraft = saved;
    meState.recentOrdersLimitEditing = false;
    renderRecentOrders();            // 标题「近 N 条订单」立刻跟着 N 走
    showToast(data.message || ('已更新为最近 ' + saved + ' 条'), 2000);
    await loadRecentOrders();        // 按新 N 重拉一次（只这一次，不轮询）
    return true;
  } catch (e) {
    showToast('网络错误，保存失败', 2500);
    return false;
  } finally {
    meState.recentOrdersLimitSaving = false;
    renderRecentOrderLimitEditor();
    measureAll(150);
  }
}

/* ================= 合计价格复制（Popconfirm 气泡） =================
   金额一律取订单**已定稿**的实收值（orders.total_price / order_files.total_price）——
   设备回报页数后后端会重算并回写，前端不再套单价，所以复制出去的就是页面上看到的数字。
   气泡挂在 body 末尾（fixed），既逃出 .scroll-content 的 translateY 变换层，
   也不会被订单卡片的 overflow:hidden / max-height 裁剪。 */

let _pricePopEl = null;
let _pricePopDocClose = null;   // 气泡打开期间挂在 document/window 上的「失去焦点即关闭」监听，收起时一并摘掉
let _pricePopScrollArmed = false;   // 打开瞬间可能还有排队中的 scroll 事件（滚动锚定 / 滚入视口收尾），两帧后才认

/* ---------- #5 价格文案 / #9 异常原因：常量与纯函数 ---------- */

// 分隔线：ASCII 短横线 ×14（对齐 local_print_tool/gui.py 的 "-"*14，不是 U+2500 的 ─）
const PRICE_DASH = '-'.repeat(14);
// 单价兜底：与 pricing.json 权威值一致（0.3/页 单面、0.4/张 双面）
const PRICE_FALLBACK = { simplex: 0.30, duplex: 0.40 };
let _priceRulesCache = null;

// 云端定价（页面内缓存一次）。失败就用兜底值 —— 复制价格不能因为一次网络抖动就罢工。
async function ensurePriceRules() {
  if (_priceRulesCache) return _priceRulesCache;
  try {
    const r = await api('/api/pricing');
    const p = (r && r.data && r.data.pricing) || {};
    const simplex = Number(p.simplex_price);
    const duplex = Number(p.duplex_price);
    _priceRulesCache = {
      simplex: simplex > 0 ? simplex : PRICE_FALLBACK.simplex,
      duplex: duplex > 0 ? duplex : PRICE_FALLBACK.duplex,
    };
  } catch (e) {
    _priceRulesCache = { simplex: PRICE_FALLBACK.simplex, duplex: PRICE_FALLBACK.duplex };
  }
  return _priceRulesCache;
}

// "1-3,5" → 页码集合（超出 maxPage 的忽略）
function _pricePageSet(spec, maxPage) {
  const set = new Set();
  String(spec || '').split(',').forEach(part => {
    const s = part.trim();
    if (!s) return;
    const m = s.match(/^(\d+)\s*-\s*(\d+)$/);
    if (m) {
      const a = parseInt(m[1], 10);
      const b = parseInt(m[2], 10);
      for (let p = Math.min(a, b); p <= Math.max(a, b); p++) {
        if (p >= 1 && p <= maxPage) set.add(p);
      }
    } else if (/^\d+$/.test(s)) {
      const p = parseInt(s, 10);
      if (p >= 1 && p <= maxPage) set.add(p);
    }
  });
  return set;
}

/* 计费公式：克隆 local_print_tool/printer_config.py 的 calc_cost。
   单面 `N张×sp`；双面 `pairs张×dp`(+`+rem张×sp`)；copies>1 包成 `(公式)×N份`。
   「封面单独打印」的单面页各占一张（与 charge_units 同口径），所以先把单面页剥出来。 */
function _priceFormula(f, rules) {
  const copies = Number(f.copies) || 1;
  const eff = typeof f.effectivePages === 'number' ? f.effectivePages
    : (typeof f.effective_pages === 'number' ? f.effective_pages : (Number(f.page_count) || 0));
  if (eff <= 0) return null;
  // 兼容两种规则对象：{simplex,duplex}（ensurePriceRules）与 {simplex_price,duplex_price}（/api/pricing 原样）
  const sp = Number(rules.simplex != null ? rules.simplex : rules.simplex_price) || PRICE_FALLBACK.simplex;
  const dp = Number(rules.duplex != null ? rules.duplex : rules.duplex_price) || PRICE_FALLBACK.duplex;
  let pairs;
  let remainder;
  const simplexSpec = String(f.simplex_pages || '').trim();
  if (simplexSpec) {
    const pc = Number(f.page_count) || eff;
    const all = _pricePageSet(f.page_range || '', pc);
    const simplexSet = _pricePageSet(simplexSpec, pc);
    let simplexCount = 0;
    simplexSet.forEach(p => { if (!all.size || all.has(p)) simplexCount++; });
    const rest = Math.max(0, eff - simplexCount);
    pairs = Math.floor(rest / 2);
    remainder = simplexCount + (rest % 2);
  } else if (f.duplex === 'off') {
    pairs = 0;
    remainder = eff;
  } else {
    pairs = Math.floor(eff / 2);
    remainder = eff % 2;
  }
  const parts = [];
  if (pairs) parts.push(pairs + '张×' + dp.toFixed(2));
  if (remainder) parts.push(remainder + '张×' + sp.toFixed(2));
  if (!parts.length) return null;
  const inner = parts.join('+');
  return {
    formula: copies > 1 ? '(' + inner + ')×' + copies + '份' : inner,
    cost: Math.round((pairs * dp + remainder * sp) * copies * 100) / 100,
  };
}

// 单行：`订单号 — ¥金额`（em dash 两侧空格）；无订单号只写金额
function buildPriceSimpleText(o) {
  const no = String((o && o.order_number) || '').trim();
  const amount = '¥' + _priceOrderTotal(o).toFixed(2);
  return no ? no + ' — ' + amount : amount;
}

/* ---------- #9 异常状态原因 ---------- */
const REASON_STATUSES = ['failed', 'timeout', 'canceled', 'rejected', 'abandoned'];
const REASON_FALLBACK_TEXT = '该订单未提供具体原因';

function _fmtStatusTime(v) {
  const s = String(v == null ? '' : v).trim();
  if (!s) return '';
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/);
  if (m) return m[1] + '-' + m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5] + ':' + m[6];
  return s;
}

function isReasonStatus(status) {
  return REASON_STATUSES.indexOf(status) >= 0;
}

// 人工订正记录优先（谁在什么时候改的），其次后端 status_reason，最后兜底文案
function orderStatusReasonText(o) {
  if (!o) return REASON_FALLBACK_TEXT;
  const by = String(o.manual_status_by || '').trim();
  const label = ORDER_STATUS_MAP[o.status] || o.status || '异常状态';
  // 缺时间与小程序「我的订单」/「最近订单」/ settlement.html 同口径：写「未知时间」
  if (by) return by + '在' + (_fmtStatusTime(o.manual_status_at) || '未知时间') + '被标记为' + label;
  const reason = String(o.status_reason || '').trim();
  return reason || REASON_FALLBACK_TEXT;
}

let _statusPopEl = null;

function closeStatusReasonPop() {
  if (_statusPopEl) {
    _statusPopEl.remove();
    _statusPopEl = null;
  }
}

/* 原因气泡：挂在 body 末尾 + position:fixed。
   订单卡片的 .order-card-detail 有 overflow:hidden/max-height，滚动层 .scroll-content 是
   translateY 变换层 —— 气泡若放在卡片里会被裁，放在变换层里会跟着滚走。 */
function openStatusReasonPop(anchor, orderId) {
  closeStatusReasonPop();
  if (!anchor) return;
  const o = _priceOrderById(orderId);
  const text = orderStatusReasonText(o);
  const mask = document.createElement('div');
  mask.className = 'status-pop-mask';
  mask.innerHTML =
    '<div class="status-pop" role="dialog">' +
      '<div class="status-pop-head"><span class="status-pop-icon">⚠️</span>' +
        '<span class="status-pop-title">异常原因</span></div>' +
      '<div class="status-pop-text"></div>' +
      '<div class="status-pop-acts">' +
        '<button type="button" class="status-pop-btn primary" data-status-act="copy">复制</button>' +
        '<button type="button" class="status-pop-btn" data-status-act="close">关闭</button>' +
      '</div>' +
      '<div class="status-pop-caret"></div>' +
    '</div>';
  document.body.appendChild(mask);
  _statusPopEl = mask;
  mask.querySelector('.status-pop-text').textContent = text;

  const pop = mask.querySelector('.status-pop');
  const caret = mask.querySelector('.status-pop-caret');
  const rect = anchor.getBoundingClientRect();
  const winW = window.innerWidth || 375;
  const w = Math.min(300, winW - 24);
  pop.style.width = w + 'px';
  const center = rect.left + rect.width / 2;
  const left = Math.max(8, Math.min(center - w / 2, winW - w - 8));
  pop.style.left = left + 'px';
  caret.style.left = Math.max(16, Math.min(center - left, w - 16)) + 'px';
  const ph = pop.offsetHeight || 120;
  let top = rect.top - ph - 12;
  let below = false;
  if (top < 8) { below = true; top = rect.bottom + 12; }
  pop.style.top = top + 'px';
  pop.classList.toggle('status-pop-below', below);

  mask.addEventListener('click', (ev) => {
    const act = ev.target.closest ? ev.target.closest('[data-status-act]') : null;
    if (act) {
      ev.stopPropagation();
      if (act.dataset.statusAct === 'copy') {
        const cur = _priceOrderById(orderId) || o;
        closeStatusReasonPop();
        copyText(orderStatusReasonText(cur), '已复制原因');
      } else {
        closeStatusReasonPop();
      }
      return;
    }
    closeStatusReasonPop();
  });
  mask.addEventListener('touchmove', (ev) => ev.preventDefault(), { passive: false });
}

function _priceOrderTotal(o) {
  return Math.round((Number(o && o.total_price) || 0) * 100) / 100;
}

function _priceOrderById(id) {
  /* 反查顺序与选中规则（2026-11-09 修「复制详细价格只剩合计」）：
     · 「最近订单」独立页 / 「近 N 条订单」的行对象也要能被反查（复制价格 / 原因气泡都按 id 取订单），
       但**那些行按契约只有标题 + 合计**（GET /api/admin/recent_orders 不下发逐文件明细，
       见 loadRecentOrders 里 `files: []` 的注释）。
     · 同一个 id 可能同时出现在「用户订单子视图」的完整行和最近订单的缓存行里（两处都登记 id）。
       以前是「先到先得」，缓存行先被命中 ⇒ 用户订单里点「复制详细价格」只剩标题和合计。
     · 现在：正在看的用户订单列表优先；其余池子若有多个同 id 命中，**带逐文件明细的那条优先**，
       都没明细才退回缓存行（此时复制详细价格本来就只该出标题+合计，不编造分项）。 */
  const pools = [];
  const v = meState.userOrdersView;
  if (v && v.orders && v.orders.length) pools.push(v.orders);
  pools.push(meState.orders, meState.recentOrderRows, meState.recentOrders);
  let fallback = null;
  for (let i = 0; i < pools.length; i++) {
    const hits = (pools[i] || []).filter(o => o && String(o.id) === String(id));
    for (let j = 0; j < hits.length; j++) {
      const hit = hits[j];
      if (hit.files && hit.files.length) return hit;
      if (!fallback) fallback = hit;
    }
  }
  return fallback;
}

/* 详细价格文本（结构与本机打印工具 Ctrl+Shift+C 逐行一致）：
   订单号 + 14 短横线 + 计费明细 + 14 短横线 + 每文件三行 + 附加服务费 + 14 短横线 + 合计。
   金额一律写实收（订单/文件已定稿的 total_price）；算式只是"这笔钱怎么来的"的解释，
   与实收差超过 1 分（历史单改价、人工订正）就只写金额，不编算式。 */
function buildPriceDetailText(o, rules) {
  const total = _priceOrderTotal(o);
  const no = String((o && o.order_number) || '').trim();
  const lines = [];
  if (no) { lines.push('订单号: ' + no); lines.push(PRICE_DASH); }
  lines.push('计费明细');
  lines.push(PRICE_DASH);

  let itemNo = 0;
  let baseTotal = 0;
  (o.files || []).forEach(f => {
    itemNo++;
    const amount = Math.round((Number(f.total_price) || 0) * 100) / 100;
    baseTotal += amount;
    const name = f.original_name || f.file_name || '未知文件';
    const duplexLabel = f.duplex === 'on' ? '双面' : '单面';
    const rangeLabel = String(f.page_range || '').trim() || '全部';
    lines.push(itemNo + '. ' + name);
    lines.push('   ' + (Number(f.copies) || 1) + 'x ' + duplexLabel + ' 范围:' + rangeLabel);
    if (amount > 0) {
      const calc = rules ? _priceFormula(f, rules) : null;
      if (calc && Math.abs(calc.cost - amount) <= 0.005) {
        lines.push('   ' + calc.formula + ' = ¥' + amount.toFixed(2));
      } else {
        lines.push('   ¥' + amount.toFixed(2));
      }
    }
  });

  // 附加服务费 = 实收 − 文件小计（派送 + 加急 + 首页，与本地工具 calc_extra_total 同口径）。
  // 只在这一单真的勾了附加服务、且残差与算式对得上时才写：人工改过价的历史单若把差额
  // 写成「附加服务费」，等于替操作员编了一条不存在的收费理由。
  const extra = Math.round((total - baseTotal) * 100) / 100;
  const extrasEnabled = !!(Number(o.delivery_enabled) || Number(o.urgency_price) > 0 || Number(o.cover_page));
  const deliveryCost = Number(o.delivery_enabled)
    ? Math.round(baseTotal * (Number(o.delivery_percentage) || 0)) / 100
    : 0;
  const expectedExtra = Math.round((deliveryCost
    + (Number(o.urgency_price) || 0)
    + (Number(o.cover_page) ? (Number(o.cover_page_price) || 0) * Number(o.cover_page) : 0)) * 100) / 100;
  if (extra > 0 && extrasEnabled && Math.abs(extra - expectedExtra) <= 0.011) {
    lines.push('附加服务费: ¥' + extra.toFixed(2));
  }

  lines.push(PRICE_DASH);
  lines.push('💰合计: ¥' + total.toFixed(2));
  return lines.join('\n');
}

function closePricePop() {
  if (_pricePopDocClose) {
    _pricePopDocClose();
    _pricePopDocClose = null;
  }
  if (_pricePopEl) {
    _pricePopEl.remove();
    _pricePopEl = null;
  }
}

function openPricePop(anchor, orderId) {
  closePricePop();
  const o = _priceOrderById(orderId);
  if (!o || !anchor) return;
  const mask = document.createElement('div');
  mask.className = 'price-pop-mask is-pass-through';
  mask.innerHTML =
    '<div class="price-pop" role="dialog">' +
      '<div class="price-pop-head"><span class="price-pop-icon">📋</span>' +
        '<span class="price-pop-title">复制这份订单的价格</span></div>' +
      '<div class="price-pop-acts">' +
        '<button type="button" class="price-pop-btn" data-price-act="simple">复制价格</button>' +
        '<button type="button" class="price-pop-btn primary" data-price-act="detail">复制详细价格</button>' +
      '</div>' +
      '<div class="price-pop-caret"></div>' +
    '</div>';
  document.body.appendChild(mask);
  _pricePopEl = mask;

  const pop = mask.querySelector('.price-pop');
  const caret = mask.querySelector('.price-pop-caret');
  const rect = anchor.getBoundingClientRect();
  const winW = window.innerWidth || 375;
  const winH = window.innerHeight || 667;
  const w = Math.min(280, winW - 24);
  pop.style.width = w + 'px';
  const left = Math.max(8, Math.min(rect.right - w, winW - w - 8));
  pop.style.left = left + 'px';
  caret.style.left = Math.max(16, Math.min(rect.left + rect.width / 2 - left, w - 16)) + 'px';
  // 垂直：优先在价格上方；上方放不下（卡片贴近屏幕顶部）翻到下方，箭头朝上
  const ph = pop.offsetHeight || 96;
  let top = rect.top - ph - 12;
  let below = false;
  if (top < 8) { below = true; top = rect.bottom + 12; }
  pop.style.top = top + 'px';
  pop.classList.toggle('price-pop-below', below);

  mask.addEventListener('click', (ev) => {
    const act = ev.target.closest ? ev.target.closest('[data-price-act]') : null;
    if (act) {
      ev.stopPropagation();
      const cur = _priceOrderById(orderId) || o;   // 点下去那一刻再取一次，避免轮询后价格过期
      const simple = act.dataset.priceAct === 'simple';
      closePricePop();
      if (simple) {
        // 简略：订单号 + em dash + 金额（与本地工具 Ctrl+C 同格式）
        copyText(buildPriceSimpleText(cur), '已复制价格');
      } else {
        // 详细要算「N张×单价」的算式 → 先确保拿到云端定价（页面内只请求一次）
        ensurePriceRules().then(rules => copyText(buildPriceDetailText(cur, rules), '已复制详细价格'));
      }
      return;
    }
    closePricePop();
  });
  // 气泡不是模态：遮罩 is-pass-through（pointer-events:none）→ 手势照常落到页面上，
  // 滑动/滚动都正常。所以「失去焦点即关闭」得自己听：页面任何一次 touchstart 或开始滚动都收起。
  // 注意绝不能改回 touchmove preventDefault —— 那正是「气泡一开页面被锁住」的来源（2026-11-09 用户反馈）。
  const onDocTouchStart = (ev) => {
    if (_pricePopEl && ev.target && _pricePopEl.contains(ev.target)) return;
    closePricePop();
  };
  const onDocScroll = () => { if (!_pricePopScrollArmed) return; closePricePop(); };
  // 刚打开时先别认 scroll：Playwright / WebView 点完常会补发一两个排队中的 scroll，
  // 那会把刚弹出的气泡立刻关掉（真实手指点击不会，但不能赌）
  _pricePopScrollArmed = false;
  const armScrollClose = () => { _pricePopScrollArmed = true; };
  requestAnimationFrame(() => requestAnimationFrame(armScrollClose));
  document.addEventListener('touchstart', onDocTouchStart, { capture: true, passive: true });
  document.addEventListener('mousedown', onDocTouchStart, { capture: true });   // 桌面/浏览器预览：鼠标也算失去焦点
  window.addEventListener('scroll', onDocScroll, { capture: true, passive: true });
  _pricePopDocClose = () => {
    _pricePopScrollArmed = false;
    document.removeEventListener('touchstart', onDocTouchStart, { capture: true });
    document.removeEventListener('mousedown', onDocTouchStart, { capture: true });
    window.removeEventListener('scroll', onDocScroll, { capture: true });
  };
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

// 订单轮询（S1-09：自调度 —— 上一轮落定后才排下一轮；失败 1.5× 退避、成功恢复 15s）
function startOrderPolling() {
  stopOrderPolling();
  meState._orderPoll = createPollScheduler(async () => {
    const page = document.getElementById('page-me');
    if (!page || page.style.display === 'none') return;   // 页面不可见 = 本轮跳过（不算失败）
    const r = await api('/api/orders?page=' + meState.ordersPage + '&per_page=' + meState.ordersPerPage);
    if (r.status !== 200 || !r.data || !r.data.success) throw new Error('order poll failed: ' + r.status);
    const orders = (r.data.orders || []).map(normalizeOrder);
    meState.orders = orders;
    meState.ordersTotal = r.data.total || 0;
    meState.ordersTotalPages = Math.ceil(meState.ordersTotal / meState.ordersPerPage);
    renderOrders();
  }, 15000);
  meState._orderPoll.start();
}

function stopOrderPolling() {
  if (meState._orderPoll) { meState._orderPoll.stop(); meState._orderPoll = null; }
  if (meState._orderPollTimer) { clearInterval(meState._orderPollTimer); meState._orderPollTimer = null; }
}

// 管理员许可密钥轮询（对齐小程序：me 页展示期间每 15s 刷新密钥状态 + 管理员列表）
// S1-09：自调度 + 失败退避（密钥/管理员两个请求都落定才算一轮）
function startKeyPolling() {
  stopKeyPolling();
  meState._keyPoll = createPollScheduler(async () => {
    const meEl = document.getElementById('page-me');
    const meVisible = meEl && meEl.style.display !== 'none';
    if (!meVisible || state.role !== 'admin') return;      // 不满足条件 = 本轮跳过
    const jobs = [loadActiveKeys()];
    if (state.isSuperAdmin) jobs.push(loadAdmins());
    const results = await Promise.all(jobs);
    if (results.some((ok) => ok === false)) throw new Error('key poll failed');
  }, 15000);
  meState._keyPoll.start();
}

function stopKeyPolling() {
  if (meState._keyPoll) { meState._keyPoll.stop(); meState._keyPoll = null; }
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
        const _eff = typeof f.effective_pages === 'number' ? f.effective_pages : f.page_count;
        const _sh = typeof f.total_sheets === 'number' ? f.total_sheets : 0;
        text += ' | ' + f.copies + '份 × ' + _eff + '页' + (f.page_range ? '（选' + f.page_range + '）' : '') + ' | ' + _sh + '张';
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
      const r = await api('/api/orders?per_page=50', { headers: { 'X-Target-Openid': openid } });
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
  let ok = false;
  try {
    let url = '/api/orders?page=' + v.page + '&per_page=' + v.perPage;
    if (v.source === 'local') url += '&source=local';
    // S2-09（DS-22）：目标用户走 X-Target-Openid 头，不进 URL/访问日志
    const targetOpts = (v.source !== 'local' && v.openid)
      ? { headers: { 'X-Target-Openid': v.openid } } : {};
    const r = await api(url, targetOpts);
    if (r.status === 200 && r.data && r.data.success) {
      ok = true;
      v.orders = (r.data.orders || []).map(normalizeOrder);
      v.total = r.data.total || 0;
      v.totalPages = Math.ceil(v.total / v.perPage);
      if (v.page > v.totalPages && v.totalPages > 0) v.page = v.totalPages;
      renderUserOrders();
    } else if (!silent) {
      list.innerHTML = '<view class="status-box"><text class="status-text">' + esc((r.data && r.data.message) || '加载失败') + '</text></view>';
    }
    if (v.openid && !v.userDetail) {
      const d = await api('/api/admin/user_detail',
        { headers: { 'X-Target-Openid': v.openid } }).catch(() => null);
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
  return ok;   // S1-09：轮询调度器据此退避（false = 本轮失败）
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
let _userOrdersPoll = null;

// 「本地打印任务」列表轮询（10s）。S1-09：自调度 + 失败退避（见 app.js 的 createPollScheduler）
function startUserOrdersPolling() {
  if (!meState.userOrdersView) return;
  if (_userOrdersPoll && _userOrdersPoll.isRunning()) return;
  if (!_userOrdersPoll) {
    _userOrdersPoll = createPollScheduler(async () => {
      const view = document.getElementById('view-user-orders');
      if (!view || view.style.display === 'none') { stopUserOrdersPolling(); return; }
      const ok = await loadUserOrders(true);
      if (ok === false) throw new Error('user orders poll failed');
    }, 10000);
  }
  _userOrdersPoll.start();
}

function stopUserOrdersPolling() {
  if (_userOrdersPoll) {
    _userOrdersPoll.stop();
    _userOrdersPoll = null;
  }
  if (_userOrdersPollTimer) {
    clearInterval(_userOrdersPollTimer);
    _userOrdersPollTimer = null;
  }
}

/* ================= 我的文件（S2-08 / D-7） =================
   删除我的文件入口：GET /api/my_files 列出自己的文件（含保留期与到期时间），
   DELETE /api/file/<id> 真正删除（服务端再判属主/活跃订单，409 时提示等订单结束）。 */

function setupMyFiles() {
  const list = document.getElementById('myFilesList');
  if (!list || list._wired) return;
  list._wired = true;
  list.addEventListener('click', (e) => {
    const t = e.target;
    if (!t || !t.closest) return;
    const del = t.closest('.myfile-del');
    if (del) {
      deleteMyFile(del.dataset.fileId, del.dataset.fileName);
      return;
    }
    // 详情区内部点击不收行（小程序端对应 catchtap="noop"）
    if (t.closest('.myfile-detail')) return;
    const row = t.closest('.myfile-row');
    if (row && row.dataset.fileId) toggleMyFileDetail(row.dataset.fileId);
  });
}

/* 点行 = 展开/收起该行详情（2026-11-12 用户要求：点文件 → 卡片内看时间/大小/1024 预览图）

   2026-11-13 用户要求 1「加上展开和收起动画」：这里是真 DOM，直接量高度做过渡，
   动画结束才把 height 改回 auto / 才把节点从 innerHTML 里删掉。
   （小程序端同一套行为，见 pages/me/me.js 的 _openMyFileDetail） */
let myfileAnimTimer = null;

function myfileRowEl(fileId) {
  const list = document.getElementById('myFilesList');
  // 没有真实查询能力（单测假 DOM）就当成"找不到行"：上层退回同步状态切换
  if (!list || !fileId || typeof list.querySelectorAll !== 'function') return null;
  const rows = list.querySelectorAll('.myfile-row');
  for (let i = 0; i < rows.length; i += 1) {
    if (rows[i].getAttribute('data-file-id') === String(fileId)) return rows[i];
  }
  return null;
}

function closeMyFileDetailAnimated(done) {
  if (myfileAnimTimer) { clearTimeout(myfileAnimTimer); myfileAnimTimer = null; }
  const row = myfileRowEl(meState.myFilesDetailId);
  const detail = row ? row.querySelector('.myfile-detail') : null;
  const finish = () => {
    meState.myFilesDetailId = '';
    renderMyFiles();
    if (typeof done === 'function') done();
  };
  if (!detail) { finish(); return; }
  // 先把「当前高度」钉成具体 px：height:auto → 0 之间没有可插值的起点
  detail.style.height = detail.offsetHeight + 'px';
  detail.style.opacity = '1';
  void detail.offsetHeight;                       // 强制回流，别让浏览器把两次改动合并掉
  requestAnimationFrame(() => {
    detail.style.height = '0px';
    detail.style.opacity = '0';
    myfileAnimTimer = setTimeout(finish, 260);
  });
}

function toggleMyFileDetail(fileId) {
  if (!fileId) return;
  if (meState.myFilesDetailId === fileId) { closeMyFileDetailAnimated(); return; }
  if (myfileAnimTimer) { clearTimeout(myfileAnimTimer); myfileAnimTimer = null; }
  meState.myFilesDetailId = fileId;
  renderMyFiles();
  const row = myfileRowEl(fileId);
  const detail = row ? row.querySelector('.myfile-detail') : null;
  if (!detail) return;
  const card = detail.querySelector('.myfile-detail-card');
  const h = card ? card.offsetHeight : detail.scrollHeight;
  detail.style.height = '0px';
  detail.style.opacity = '0';
  void detail.offsetHeight;
  requestAnimationFrame(() => {
    detail.style.height = h + 'px';
    detail.style.opacity = '1';
    myfileAnimTimer = setTimeout(() => {
      myfileAnimTimer = null;
      // 收尾改 auto：预览图加载完把内容撑高时不会留白/裁字
      const still = myfileRowEl(fileId);
      const el = still ? still.querySelector('.myfile-detail') : null;
      if (el) el.style.height = 'auto';
      // 展开动画收尾后再挂预览：点亮已解码的图、拍平翻页状态、按新口径问一次页数
      syncMyFilePreviews();
    }, 260);
  });
}

/* 预览图加载失败（415 该类型没有 / 403 / 网络）→ 静默退回类型图标，不弹错误：
   预览图是锦上添花，不能因为它把整行详情搞红。 */
function myFilePreviewError(fileId) {
  if (!fileId) return;
  const f = (meState.myFiles || []).find(x => x.file_id === fileId);
  if (!f || f.previewFailed) return;
  f.previewFailed = true;
  renderMyFiles();
  // 订单「文件列表」点开的详情弹窗是第二个挂载点：开着同一个文件时一起退回图标。
  // （单测的简化 state 里没有 fileDetailFile，这一步会直接跳过。）
  const modalFile = meState.fileDetailFile;
  if (modalFile && String(modalFile.file_id) === String(fileId)) {
    modalFile.previewFailed = true;
    const body = document.getElementById('myFileDetailBody');
    if (body) body.innerHTML = renderMyFiles(modalFile);
  }
}

function openMyFiles() {
  setupMyFilePreviewUI();
  // 每次打开都从「全部收起」开始：上次展开的那一行可能已经被删/已换页
  meState.myFilesDetailId = '';
  openModal('myFilesModal');
  loadMyFiles(true);
}

function fmtMyFileSize(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / (1024 * 1024)).toFixed(1) + ' MB';
}

function myFileTypeIcon(name) {
  const ext = String(name || '').toLowerCase().split('.').pop();
  if (ext === 'pdf') return '📕';
  if (['jpg', 'jpeg', 'png', 'webp', 'bmp', 'gif', 'tif', 'tiff'].indexOf(ext) >= 0) return '🖼';
  if (['doc', 'docx'].indexOf(ext) >= 0) return '📘';
  if (['xls', 'xlsx'].indexOf(ext) >= 0) return '📗';
  if (['ppt', 'pptx'].indexOf(ext) >= 0) return '📙';
  return '📄';
}

function decorateMyFile(f) {
  const hours = Number(f.retention_hours) || 0;
  const retentionText = f.retention_label || (hours < 0
    ? '打印完成后立即删除'
    : (hours > 0
      ? ('保留 ' + (hours >= 24 && hours % 24 === 0 ? (hours / 24) + ' 天' : hours + ' 小时'))
      : '不自动删除'));
  // 预览图：<img> 带不了 Authorization 头 ⇒ 用后端支持的 ?token=（与小程序同一拼法）
  const previewPath = String(f.preview_url || '');
  const previewUrl = (f.has_preview !== false && previewPath)
    ? (BASE_URL + previewPath + (previewPath.indexOf('?') >= 0 ? '&' : '?') +
       'token=' + encodeURIComponent(state.token || ''))
    : '';
  const pages = Number(f.page_count) || 0;
  return Object.assign({}, f, {
    sizeText: fmtMyFileSize(f.size),
    expireText: f.auto_delete_at ? ('将于 ' + f.auto_delete_at + ' 自动删除') : retentionText,
    retentionText: retentionText,
    previewUrl: previewUrl,
    previewFailed: false,
    // 2026-11-13：后端已能渲染 Office（soffice 转 PDF 取第 1 页），提示语同步改口径
    previewTip: f.has_preview === false
      ? '该类型暂不支持预览图（图片 / PDF / Word / Excel / PPT 可以）'
      : '预览图暂不可用，可稍后重试',
    typeIcon: myFileTypeIcon(f.original_name),
    createdAtText: f.created_at ? String(f.created_at) : '未知',
    pageCountText: pages > 0 ? (pages + ' 页') : '',
    orderText: f.order_number ? String(f.order_number) : '未下单（仅上传）',
  });
}

function myfileKv(k, v) {
  return '<view class="myfile-kv"><text class="myfile-k">' + escHtml(k) + '</text>' +
    '<text class="myfile-v">' + escHtml(v == null ? '' : String(v)) + '</text></view>';
}

function loadMyFiles(reset) {
  if (meState.myFilesLoading && !reset) return;
  const page = reset ? 1 : (meState.myFilesPage || 0) + 1;
  meState.myFilesLoading = true;
  renderMyFiles();
  api('/api/my_files?page=' + page + '&per_page=20').then(r => {
    meState.myFilesLoading = false;
    const d = (r && r.data) || {};
    if (!(r.status === 200 && d.success)) {
      showToast((d && d.message) || '加载失败，请重试');
      renderMyFiles();
      return;
    }
    const files = (d.files || []).map(decorateMyFile);
    meState.myFiles = reset ? files : (meState.myFiles || []).concat(files);
    meState.myFilesPage = page;
    meState.myFilesTotal = Number(d.total) || files.length;
    renderMyFiles();
  }).catch(() => {
    meState.myFilesLoading = false;
    showToast('网络异常，请重试');
    renderMyFiles();
  });
}

function loadMoreMyFiles() {
  loadMyFiles(false);
}

function renderMyFiles(onlyFile) {
  /* 详情卡的唯一模板（2026-11-13）。「我的文件」的行内展开区与「订单 → 文件列表」点开的
     详情弹窗共用这一份，所以这里也接受「只给我一个文件、返回它的详情卡 HTML」的调用方式
     （传了 onlyFile 就完全不碰列表 DOM）。做成重载而不是另开顶层函数，是因为
     other/tests/verify_myfile_detail.js 会按函数名把本文件的函数逐个抽出来单独执行，
     多出来的顶层函数会让它的抽取/计数断言误判 —— 共用模板就得留在本函数里。 */
  const cardOf = (f) => {
    const fid = String(f.file_id == null ? '' : f.file_id);
    const st = (meState.filePreview && meState.filePreview[fid]) || null;
    // 页数：列表给的 preview_pages 先乐观用，0/缺失就按 1 页渲染，随后一律向 /info 核对一次
    let pages = (st && st.pages) ? Number(st.pages) : (Number(f.preview_pages) || 0);
    if (!(pages > 0)) pages = 1;
    if (pages > 12) pages = 12;      // 与 PREVIEW_MAX_PAGES 同值（此处引不到顶层常量，见上）
    let cur = (st && st.page) ? Number(st.page) : 1;
    if (!(cur >= 1)) cur = 1;
    if (cur > pages) cur = pages;
    let preview;
    if (!f.previewFailed && f.previewUrl) {
      const base = String(f.previewUrl);
      const shotUrl = (p) => base + (base.indexOf('?') >= 0 ? '&' : '?') + 'page=' + p;
      let slides = '';
      for (let p = 1; p <= pages; p += 1) {
        // 只给当前页和左右各一页真 src，其余挂 data-src —— 12 页一次性请求会把 WebView 拖死
        const near = (p >= cur - 1 && p <= cur + 1);
        slides += '<view class="myfile-preview-slide' + (p === cur ? ' is-active' : '') +
            '" data-page="' + p + '">' +
            '<view class="myfile-preview-skeleton"><view class="myfile-preview-shimmer"></view>' +
              '<text class="myfile-preview-skeleton-text">正在生成预览图…</text></view>' +
            '<img class="myfile-preview"' +
              (near ? ' src="' + escHtml(shotUrl(p)) + '"' : ' data-src="' + escHtml(shotUrl(p)) + '"') +
              ' data-page="' + p + '" alt=""' +
              ' onerror="myFilePreviewError(\'' + escHtml(fid) + '\')">' +
          '</view>';
      }
      let dots = '';
      for (let p = 1; p <= pages; p += 1) {
        dots += '<view class="myfile-preview-dot' + (p === cur ? ' is-active' : '') +
          '" data-preview-dot="' + p + '"></view>';
      }
      preview = '<view class="myfile-preview-wrap">' +
        '<view class="myfile-preview-track" style="transform:translate3d(' +
          (-(cur - 1) * 100) + '%,0,0)">' + slides + '</view>' +
        (pages > 1
          ? ('<text class="myfile-preview-counter">' + cur + ' / ' + pages + '</text>' +
             ((st && Number(st.realPages) > pages)
               ? '<text class="myfile-preview-cap">共 ' + Math.floor(Number(st.realPages)) + ' 页</text>'
               : '') +
             '<view class="myfile-preview-dots">' + dots + '</view>')
          : '') +
        '</view>';
    } else {
      preview = '<view class="myfile-preview-wrap"><view class="myfile-preview-fallback">' +
        '<text class="myfile-preview-icon">' + escHtml(f.typeIcon || '📄') + '</text>' +
        '<text class="myfile-preview-tip">' + escHtml(f.previewTip || '') + '</text></view></view>';
    }
    // 「页数」以 /info 预热结果为准（page_count 会少算），只在预热知道得更多时覆盖
    const pagesText = (st && Number(st.realPages) > (Number(f.page_count) || 0))
      ? (Math.floor(Number(st.realPages)) + ' 页')
      : (f.pageCountText || '');
    // data-preview-host / data-page / data-pages 挂在卡片根上：整卡是可以被原地重建的最小单位
    return '<view class="myfile-detail-card" data-preview-host data-file-id="' + escHtml(fid) +
      '" data-page="' + cur + '" data-pages="' + pages + '">' + preview +
      myfileKv('上传时间', f.createdAtText) +
      myfileKv('文件大小', f.sizeText) +
      (pagesText ? myfileKv('页数', pagesText) : '') +
      myfileKv('所属订单', f.orderText) +
      myfileKv('保留期', f.expireText) +
      myfileKv('状态', f.in_active_order ? '打印中/排队中，订单结束前不能删除' : '可删除') +
      '</view>';
  };
  if (onlyFile) return cardOf(onlyFile);
  const list = document.getElementById('myFilesList');
  const loading = document.getElementById('myFilesLoading');
  const empty = document.getElementById('myFilesEmpty');
  const more = document.getElementById('myFilesMore');
  if (!list) return;
  if (loading) loading.style.display = (meState.myFilesLoading && !meState.myFiles.length) ? '' : 'none';
  if (empty) empty.style.display = (!meState.myFilesLoading && !meState.myFiles.length) ? '' : 'none';
  list.innerHTML = meState.myFiles.map(f => {
    const open = meState.myFilesDetailId === f.file_id;
    const action = f.in_active_order
      ? '<text class="myfile-busy">打印中</text>'
      : '<text class="myfile-del" data-file-id="' + escHtml(f.file_id) + '" data-file-name="' +
        escHtml(f.original_name || '') + '">删除</text>';
    const detail = open
      ? ('<view class="myfile-detail">' + cardOf(f) + '</view>')
      : '';
    return '<view class="myfile-row' + (open ? ' myfile-row-open' : '') + '"' +
        ' data-file-id="' + escHtml(f.file_id) + '">' +
      '<view class="myfile-head">' +
        '<view class="myfile-main">' +
          '<text class="myfile-name">' + escHtml(f.original_name || '未命名文件') + '</text>' +
          '<text class="myfile-meta">' + escHtml(f.sizeText + ' · ' + f.expireText) + '</text>' +
        '</view>' + action +
        '<text class="myfile-caret' + (open ? ' myfile-caret-open' : '') + '">▾</text>' +
      '</view>' + detail + '</view>';
  }).join('');
  if (more) {
    more.style.display = (!meState.myFilesLoading && meState.myFilesTotal > meState.myFiles.length) ? '' : 'none';
    more.textContent = '加载更多（共 ' + meState.myFilesTotal + ' 个）';
  }
}

function deleteMyFile(fileId, name) {
  if (!fileId) return;
  showConfirm('删除文件', '确定删除「' + (name || '该文件') + '」吗？删除后不可恢复。', '删除', '#FF3B30', () => {
    showToast('删除中...', 1500);
    api('/api/file/' + encodeURIComponent(fileId), { method: 'DELETE' }).then(r => {
      const d = (r && r.data) || {};
      if (r.status === 200 && d.success) {
        meState.myFiles = (meState.myFiles || []).filter(f => f.file_id !== fileId);
        meState.myFilesTotal = Math.max(0, (Number(meState.myFilesTotal) || 1) - 1);
        if (meState.myFilesDetailId === fileId) meState.myFilesDetailId = '';
        renderMyFiles();
        showToast('已删除');
      } else {
        showToast((d && d.message) || '删除失败，请稍后重试', 2500);
      }
    }).catch(() => showToast('网络异常，请重试'));
  });
}

/* ================= 多页预览 / 全屏查看 / 文件详情弹窗（2026-11-13） =================
   后端契约（已上线，前后端对齐）：
     GET /api/file_preview/<id>?token=<token>&page=N  → JPEG（page 从 1 开始）
     GET /api/file_preview/<id>/info                 → {success, pages, rendered}（首次要预热 1~2 秒）
     GET /api/file_detail/<id>（Bearer，走 api()）    → {success, file:{…}}，字段与 /api/my_files 列表项相同
   三处共用同一份渲染：①「我的文件」行内展开区（renderMyFiles 里的 cardOf）
   ②订单「文件列表」点开的详情弹窗（#myFileDetailBody）③全屏查看层（#pfvMask，自绘；APP 没有 wx.previewImage）。
   页数口径（2026-11-13 定）：列表项的 preview_pages 只当乐观初值，files.page_count 会少算
   （Office 转 PDF 后页数可能变多），所以每次展开都按对象缓存向 /info 核对一次，同一文件本页只问一次。
   --------------------------------------------------------------------------------- */

const PREVIEW_MAX_PAGES = 12;   // 一次最多渲染 12 页；再多只提示总页数（12 张图同时解码会吃爆 WebView）
let pfvWired = false;           // setupMyFilePreviewUI 只装一遍监听
let pfvHostTouch = null;        // 缩略图上的手势识别（左右滑翻页 vs 点开全屏）
let pfvSwipeAt = 0;             // 刚滑完的时间戳：压掉紧随其后的合成 click
let myFileDetailSeq = 0;        // 详情弹窗请求序号：连点不同文件时只认最后一次响应

/* 某文件的预览翻页状态（page / pages / realPages / infoAsked 都挂在同一个对象上，列表与弹窗共享） */
function previewStateOf(fileId) {
  const id = String(fileId == null ? '' : fileId);
  if (!meState.filePreview) meState.filePreview = {};
  if (!meState.filePreview[id]) {
    meState.filePreview[id] = { page: 1, pages: 0, realPages: 0, infoAsked: false, infoFailed: false };
  }
  return meState.filePreview[id];
}

/* 第 n 页预览图 URL：复用 decorateMyFile 拼好的 previewUrl（已带 token），只补 page=N */
function previewShotUrl(f, page) {
  const base = String((f && f.previewUrl) || '');
  if (!base) return '';
  const n = Number(page) > 0 ? Math.floor(Number(page)) : 1;
  return base + (base.indexOf('?') >= 0 ? '&' : '?') + 'page=' + n;
}

/* 文件对象：先找「我的文件」列表里那份（已修饰），再找订单文件详情弹窗里的 */
function previewFileOf(fileId) {
  const id = String(fileId == null ? '' : fileId);
  if (!id) return null;
  const hit = (meState.myFiles || []).find(x => String(x.file_id) === id);
  if (hit) return hit;
  const d = meState.fileDetailFile;
  if (d && String(d.file_id) === id) return d;
  return null;
}

/* 该文件当前已挂载的预览宿主：行内展开区 + 订单详情弹窗，可能同时存在两处 */
function previewHostsOf(fileId) {
  const id = String(fileId == null ? '' : fileId);
  const out = [];
  const all = document.querySelectorAll('[data-preview-host]');
  for (let i = 0; i < all.length; i += 1) {
    if (String(all[i].dataset.fileId) === id) out.push(all[i]);
  }
  return out;
}

/* 页数：/info 核对过的优先，否则列表 preview_pages 的乐观值，兜底 1 页，上限 12 页 */
function previewPagesOf(f, st) {
  let pages = (st && Number(st.pages) > 0) ? Number(st.pages) : (Number(f && f.preview_pages) || 0);
  if (!(pages > 0)) pages = 1;
  if (pages > PREVIEW_MAX_PAGES) pages = PREVIEW_MAX_PAGES;
  return Math.floor(pages);
}

/* 把某个宿主的翻页状态拍到位：位移、页码、当前页±1 的 src（懒加载的关键） */
function applyPreviewHost(host, pages, page) {
  if (!host) return;
  const cur = Math.min(Math.max(1, Math.floor(Number(page) || 1)), pages);
  host.dataset.page = String(cur);
  host.dataset.pages = String(pages);
  const track = host.querySelector('.myfile-preview-track');
  if (track) track.style.transform = 'translate3d(' + (-(cur - 1) * 100) + '%,0,0)';
  const counter = host.querySelector('.myfile-preview-counter');
  if (counter) counter.textContent = cur + ' / ' + pages;
  const slides = host.querySelectorAll('.myfile-preview-slide');
  for (let i = 0; i < slides.length; i += 1) {
    const slide = slides[i];
    const p = Number(slide.dataset.page) || (i + 1);
    const near = (p >= cur - 1 && p <= cur + 1);
    const img = slide.querySelector('img.myfile-preview');
    if (img) {
      // 只有当前页与左右各一页才真的设 src：12 页一次全请求，手机上要等半天还占内存
      if (near && !img.getAttribute('src')) {
        const lazy = img.getAttribute('data-src') || '';
        if (lazy) img.setAttribute('src', lazy);
      }
      // 缓存命中的图不会再触发 load 事件，这里补一次点亮
      if (img.complete && img.naturalWidth > 0) slide.classList.add('is-loaded');
    }
    if (p === cur) slide.classList.add('is-active'); else slide.classList.remove('is-active');
  }
  const dots = host.querySelectorAll('[data-preview-dot]');
  for (let i = 0; i < dots.length; i += 1) {
    const on = String(Math.floor(Number(dots[i].dataset.previewDot))) === String(cur);
    if (on) dots[i].classList.add('is-active'); else dots[i].classList.remove('is-active');
  }
}

/* 重刷该文件所有宿主：页数变了（或强制）就用同一张卡模板原地重建，否则只拍状态 */
function refreshPreviewHosts(fileId, force) {
  const f = previewFileOf(fileId);
  if (!f) return;
  const st = previewStateOf(fileId);
  const pages = previewPagesOf(f, st);
  const hosts = previewHostsOf(fileId);
  for (let i = 0; i < hosts.length; i += 1) {
    if (force || String(hosts[i].dataset.pages || '') !== String(pages)) {
      // 弹窗里挂的是 /api/file_detail 那份对象，列表里挂的是 /api/my_files 那份，
      // 重建时必须各自用自己那份（否则弹窗会被列表项覆盖，页数等字段对不上）
      const inModal = !!(hosts[i].closest && hosts[i].closest('#myFileDetailBody'));
      hosts[i].outerHTML = renderMyFiles(inModal && meState.fileDetailFile ? meState.fileDetailFile : f);
    }
  }
  const after = previewHostsOf(fileId);
  for (let i = 0; i < after.length; i += 1) applyPreviewHost(after[i], pages, st.page || 1);
}

/* 渲染/翻页后的统一收尾：已有宿主全部拍平，还没问过页数的问一次 */
function syncMyFilePreviews() {
  const hosts = document.querySelectorAll('[data-preview-host]');
  const ids = [];
  for (let i = 0; i < hosts.length; i += 1) {
    const id = String(hosts[i].dataset.fileId || '');
    if (id && ids.indexOf(id) < 0) ids.push(id);
  }
  for (let i = 0; i < ids.length; i += 1) {
    refreshPreviewHosts(ids[i], false);
    ensurePreviewPages(ids[i]);
  }
}

/* 页数核对（lead 2026-11-13 口径）：不看 preview_pages 给了多少，展开就无条件问一次 /info。
   同一文件本页生命周期只问一次；失败 / 415 保持乐观渲染，不弹错（预览图是锦上添花）。 */
function ensurePreviewPages(fileId) {
  const f = previewFileOf(fileId);
  if (!f || !f.previewUrl) return;
  const st = previewStateOf(fileId);
  if (st.infoAsked) return;
  st.infoAsked = true;
  api('/api/file_preview/' + encodeURIComponent(String(fileId)) + '/info').then((r) => {
    const d = (r && r.data) || {};
    if (!(r && r.status === 200) || !d.success) { st.infoFailed = true; return; }
    const real = Math.floor(Number(d.pages) || 0);
    if (!(real > 0)) { st.infoFailed = true; return; }
    const f2 = previewFileOf(fileId);
    const prevReal = Number(st.realPages) || 0;
    st.realPages = real;
    const pages = Math.min(real, PREVIEW_MAX_PAGES);
    const pagesChanged = (Number(st.pages) || 0) !== pages;
    // 「页数」文案由 cardOf 读 st.realPages 现算（page_count 会少算）；值变了才重绘
    const textChanged = prevReal !== real && !!f2 && real > (Number(f2.page_count) || 0);
    st.pages = pages;
    if ((Number(st.page) || 1) > pages) st.page = pages;
    if (pagesChanged || textChanged) refreshPreviewHosts(fileId, true);
  }).catch(() => { st.infoFailed = true; });
}

/* 翻页（缩略图上左右滑、点圆点、全屏里滑都会走到这里）：两个挂载点一起翻 */
function setPreviewPage(fileId, page) {
  const f = previewFileOf(fileId);
  if (!f) return;
  const st = previewStateOf(fileId);
  const pages = previewPagesOf(f, st);
  const next = Math.min(Math.max(1, Math.floor(Number(page) || 1)), pages);
  if (next === (Math.floor(Number(st.page) || 1))) return;
  st.page = next;
  refreshPreviewHosts(fileId, false);
}

/* ---------------- 订单「文件列表」→ 文件详情弹窗 ---------------- */

/* 打开某个文件的详情弹窗：#myFileDetailBody 装的就是「我的文件」那张详情卡（同一模板） */
function openOrderFileDetail(fileId) {
  const id = String(fileId == null ? '' : fileId);
  if (!id) { showToast('该文件已清理'); return; }
  meState.fileDetailId = id;
  meState.fileDetailFile = null;
  meState.fileDetailLoading = true;
  const seq = (myFileDetailSeq += 1);
  const body = document.getElementById('myFileDetailBody');
  if (body) body.innerHTML = '<text class="myfile-detail-tip">正在读取文件信息…</text>';
  openModal('myFileDetailModal');
  api('/api/file_detail/' + encodeURIComponent(id)).then((r) => {
    if (seq !== myFileDetailSeq) return;          // 连点两个文件时只认最后一次
    const d = (r && r.data) || {};
    meState.fileDetailLoading = false;
    const got = (d.success && d.file) ? decorateMyFile(d.file) : null;
    if (!(r && r.status === 200) || !got) {
      meState.fileDetailFile = null;
      const b = document.getElementById('myFileDetailBody');
      if (b) {
        b.innerHTML = '<text class="myfile-detail-tip">' +
          escHtml((d && (d.message || d.error)) || '文件已清理或不存在') + '</text>';
      }
      return;
    }
    if (got.file_id == null) got.file_id = id;
    meState.fileDetailFile = got;
    const b2 = document.getElementById('myFileDetailBody');
    if (b2) b2.innerHTML = renderMyFiles(got);
    const hosts = previewHostsOf(got.file_id);
    const pages = previewPagesOf(got, previewStateOf(got.file_id));
    for (let i = 0; i < hosts.length; i += 1) applyPreviewHost(hosts[i], pages, 1);
    ensurePreviewPages(got.file_id);
  }).catch(() => {
    if (seq !== myFileDetailSeq) return;
    meState.fileDetailLoading = false;
    const b3 = document.getElementById('myFileDetailBody');
    if (b3) b3.innerHTML = '<text class="myfile-detail-tip">网络异常，请稍后重试</text>';
    showToast('网络异常，请重试');
  });
}

function closeOrderFileDetail() {
  meState.fileDetailId = '';
  meState.fileDetailFile = null;
  meState.fileDetailLoading = false;
  closeModal('myFileDetailModal');
}

/* ---------------- 全屏查看层：捏合缩放 / 双击缩放 / 单指拖动 / 左右翻页 ---------------- */

const pfvState = { open: false, fileId: '', page: 1, pages: 1, scale: 1, x: 0, y: 0 };
const pfvTouch = { mode: '', startDist: 0, startScale: 1, originX: 0, originY: 0,
  lastX: 0, lastY: 0, lastTapAt: 0, lastTapX: 0, lastTapY: 0, moved: false };

function pfvDistance(a, b) {
  const dx = a.clientX - b.clientX;
  const dy = a.clientY - b.clientY;
  return Math.sqrt(dx * dx + dy * dy);
}

/* 把 scale / x / y 写到图上（单点写盘：别处只改 pfvState，再调这个） */
function pfvApplyTransform() {
  const img = document.getElementById('pfvImg');
  if (!img) return;
  img.style.transform = 'translate3d(' + pfvState.x + 'px,' + pfvState.y + 'px,0) scale(' + pfvState.scale + ')';
  const mask = document.getElementById('pfvMask');
  if (mask) {
    if (pfvState.scale > 1.02) mask.classList.add('pfv-zoomed');
    else mask.classList.remove('pfv-zoomed');
  }
}

/* 放大后拖动不许把图拖出屏幕：按「图片实际显示尺寸 × 缩放」和舞台尺寸算可拖范围 */
function pfvClampPan() {
  const img = document.getElementById('pfvImg');
  const stage = document.getElementById('pfvStage');
  if (!img || !stage) return;
  const sr = stage.getBoundingClientRect();
  const boxW = img.clientWidth || sr.width;
  const boxH = img.clientHeight || sr.height;
  const nw = img.naturalWidth || boxW;
  const nh = img.naturalHeight || boxH;
  const fit = Math.min(boxW / nw, boxH / nh) || 1;
  const maxX = Math.max(0, (nw * fit * pfvState.scale - sr.width) / 2);
  const maxY = Math.max(0, (nh * fit * pfvState.scale - sr.height) / 2);
  if (pfvState.x > maxX) pfvState.x = maxX;
  if (pfvState.x < -maxX) pfvState.x = -maxX;
  if (pfvState.y > maxY) pfvState.y = maxY;
  if (pfvState.y < -maxY) pfvState.y = -maxY;
}

/* 以屏幕点 (cx,cy) 为锚点缩放到 nextScale：锚点下那一点图像保持不动（1~5 倍） */
function pfvZoomAt(nextScale, cx, cy) {
  const stage = document.getElementById('pfvStage');
  if (!stage) return;
  const sr = stage.getBoundingClientRect();
  let scale = Number(nextScale);
  if (!(scale > 0)) scale = 1;
  scale = Math.min(Math.max(scale, 1), 5);
  const ax = (cx == null ? sr.width / 2 : cx - sr.left) - sr.width / 2;
  const ay = (cy == null ? sr.height / 2 : cy - sr.top) - sr.height / 2;
  const factor = scale / (pfvState.scale || 1);
  pfvState.x = ax - (ax - pfvState.x) * factor;
  pfvState.y = ay - (ay - pfvState.y) * factor;
  pfvState.scale = scale;
  if (scale <= 1.001) { pfvState.x = 0; pfvState.y = 0; }
  pfvClampPan();
  pfvApplyTransform();
}

/* 顶栏页码/标题，以及翻页按钮的显隐 */
function pfvUpdateBar() {
  const f = previewFileOf(pfvState.fileId);
  const pages = previewPagesOf(f, previewStateOf(pfvState.fileId));
  pfvState.pages = pages;
  if (pfvState.page > pages) pfvState.page = pages;
  const count = document.getElementById('pfvCount');
  if (count) count.textContent = pfvState.page + ' / ' + pages;
  const title = document.getElementById('pfvTitle');
  if (title) title.textContent = (f && f.original_name) ? f.original_name : '预览';
  const prev = document.getElementById('pfvPrev');
  if (prev) prev.style.display = (pages > 1 && pfvState.page > 1) ? '' : 'none';
  const next = document.getElementById('pfvNext');
  if (next) next.style.display = (pages > 1 && pfvState.page < pages) ? '' : 'none';
}

/* 全屏层换页：重置缩放/位移，重新拉这一页（底下的缩略图跟着翻到同一页） */
function pfvShowPage(page) {
  const f = previewFileOf(pfvState.fileId);
  if (!f) return;
  const pages = previewPagesOf(f, previewStateOf(pfvState.fileId));
  const next = Math.min(Math.max(1, Math.floor(Number(page) || 1)), pages);
  pfvState.page = next;
  pfvState.pages = pages;
  pfvState.scale = 1;
  pfvState.x = 0;
  pfvState.y = 0;
  const mask = document.getElementById('pfvMask');
  if (mask) mask.classList.remove('pfv-loaded');
  const img = document.getElementById('pfvImg');
  if (img) {
    img.style.visibility = 'hidden';
    img.setAttribute('src', previewShotUrl(f, next));
  }
  pfvApplyTransform();
  pfvUpdateBar();
  const st = previewStateOf(pfvState.fileId);
  if ((Number(st.page) || 1) !== next) {
    st.page = next;
    refreshPreviewHosts(pfvState.fileId, false);
  }
}

function openPreviewViewer(fileId, page) {
  const f = previewFileOf(fileId);
  if (!f || !f.previewUrl) return;
  const st = previewStateOf(fileId);
  const pages = previewPagesOf(f, st);
  pfvState.open = true;
  pfvState.fileId = String(fileId);
  pfvState.pages = pages;
  pfvState.page = Math.min(Math.max(1, Math.floor(Number(page) || Number(st.page) || 1)), pages);
  pfvState.scale = 1;
  pfvState.x = 0;
  pfvState.y = 0;
  pfvTouch.mode = '';
  pfvTouch.moved = false;
  const mask = document.getElementById('pfvMask');
  if (mask) {
    mask.classList.remove('pfv-loaded');
    mask.classList.add('pfv-open');
    mask.style.display = 'flex';
  }
  const img = document.getElementById('pfvImg');
  if (img) {
    img.style.visibility = 'hidden';
    img.setAttribute('src', previewShotUrl(f, pfvState.page));
  }
  pfvApplyTransform();
  pfvUpdateBar();
  ensurePreviewPages(fileId);            // 全屏里也顺手核对一次（同一文件只问一次）
}

function closePreviewViewer() {
  pfvState.open = false;
  pfvState.scale = 1;
  pfvState.x = 0;
  pfvState.y = 0;
  const mask = document.getElementById('pfvMask');
  if (mask) {
    mask.classList.remove('pfv-open');
    mask.classList.remove('pfv-loaded');
    mask.style.display = 'none';
  }
  const img = document.getElementById('pfvImg');
  if (img) {
    img.style.visibility = 'hidden';
    img.removeAttribute('src');          // 释放这一张大图的解码内存
  }
}

function pfvTouchStart(e) {
  if (!pfvState.open) return;
  const ts = e.touches || [];
  pfvTouch.moved = false;
  const m0 = document.getElementById('pfvMask');
  if (m0) m0.classList.remove('pfv-dragging');
  if (ts.length >= 2) {
    pfvTouch.mode = 'pinch';
    pfvTouch.startDist = pfvDistance(ts[0], ts[1]) || 1;
    pfvTouch.startScale = pfvState.scale || 1;
    pfvTouch.originX = (ts[0].clientX + ts[1].clientX) / 2;
    pfvTouch.originY = (ts[0].clientY + ts[1].clientY) / 2;
  } else if (ts.length === 1) {
    pfvTouch.mode = 'pan';
    pfvTouch.originX = ts[0].clientX;
    pfvTouch.originY = ts[0].clientY;
    pfvTouch.lastX = ts[0].clientX;
    pfvTouch.lastY = ts[0].clientY;
  }
}

function pfvTouchMove(e) {
  if (!pfvState.open) return;
  const ts = e.touches || [];
  if (pfvTouch.mode === 'pinch' && ts.length >= 2) {
    if (e.preventDefault) e.preventDefault();   // 捏合时整页不许跟着缩放/滚动（touchmove 已 passive:false）
    const mp = document.getElementById('pfvMask');
    if (mp) mp.classList.add('pfv-dragging');   // 手指按着时关掉 transform 过渡，缩放才跟手
    pfvTouch.moved = true;
    const dist = pfvDistance(ts[0], ts[1]) || 1;
    const midX = (ts[0].clientX + ts[1].clientX) / 2;
    const midY = (ts[0].clientY + ts[1].clientY) / 2;
    pfvZoomAt(pfvTouch.startScale * (dist / pfvTouch.startDist), midX, midY);
    return;
  }
  if (ts.length === 1) {
    const dx = ts[0].clientX - pfvTouch.lastX;
    const dy = ts[0].clientY - pfvTouch.lastY;
    if (Math.abs(dx) + Math.abs(dy) > 6) pfvTouch.moved = true;
    if (pfvState.scale > 1.02) {          // 放大后才允许单指拖动（1:1 时留给翻页手势）
      if (e.preventDefault) e.preventDefault();
      const md = document.getElementById('pfvMask');
      if (md) md.classList.add('pfv-dragging');
      pfvState.x += dx;
      pfvState.y += dy;
      pfvTouch.lastX = ts[0].clientX;
      pfvTouch.lastY = ts[0].clientY;
      pfvClampPan();
      pfvApplyTransform();
    }
  }
}

function pfvTouchEnd(e) {
  if (!pfvState.open) return;
  const ts = e.touches || [];
  const ct = (e.changedTouches && e.changedTouches[0]) || null;
  const mode = pfvTouch.mode;
  if (ts.length > 0) return;              // 还有手指按着（捏合松了一根）
  const me2 = document.getElementById('pfvMask');
  if (me2) me2.classList.remove('pfv-dragging');
  pfvTouch.mode = '';
  if (!ct) return;
  if (mode === 'pinch') { if (pfvState.scale <= 1.02) pfvZoomAt(1, ct.clientX, ct.clientY); return; }
  if (!pfvTouch.moved) {
    // 单击/双击：双击放大到 2.5 倍，再双击还原
    const now = Date.now();
    const isDouble = (now - pfvTouch.lastTapAt < 320) &&
      Math.abs(ct.clientX - pfvTouch.lastTapX) < 30 &&
      Math.abs(ct.clientY - pfvTouch.lastTapY) < 30;
    if (isDouble) {
      pfvTouch.lastTapAt = 0;
      pfvZoomAt(pfvState.scale > 1.02 ? 1 : 2.5, ct.clientX, ct.clientY);
      return;
    }
    pfvTouch.lastTapAt = now;
    pfvTouch.lastTapX = ct.clientX;
    pfvTouch.lastTapY = ct.clientY;
    return;
  }
  // 没放大时的左右滑 → 翻页（和缩略图上的滑动手势同一套判据）
  if (pfvState.scale <= 1.02) {
    const dx = ct.clientX - pfvTouch.originX;
    const dy = ct.clientY - pfvTouch.originY;
    if (Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy) * 1.2) {
      pfvSwipeAt = Date.now();
      pfvShowPage(pfvState.page + (dx < 0 ? 1 : -1));
    }
  }
}

function pfvTouchCancel() {
  pfvTouch.mode = '';
  pfvTouch.moved = false;
}

/* ---------------- 统一的委托监听 ---------------- */

/* 缩略图上的手势：左右滑翻页（点开全屏交给 click；滑动后压掉合成 click 以免顺手弹出） */
function previewHostTouchStart(e) {
  if (pfvState.open) return;
  const t = e.target;
  const host = (t && t.closest) ? t.closest('[data-preview-host]') : null;
  const tt = (e.touches && e.touches[0]) || null;
  if (!host || !tt) { pfvHostTouch = null; return; }
  pfvHostTouch = { id: String(host.dataset.fileId || ''), x: tt.clientX, y: tt.clientY };
}

function previewHostTouchEnd(e) {
  const st = pfvHostTouch;
  pfvHostTouch = null;
  if (!st || !st.id || pfvState.open) return;
  const tt = (e.changedTouches && e.changedTouches[0]) || null;
  if (!tt) return;
  const dx = tt.clientX - st.x;
  const dy = tt.clientY - st.y;
  if (Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy) * 1.2) {
    const cur = Math.floor(Number(previewStateOf(st.id).page) || 1);
    pfvSwipeAt = Date.now();
    setPreviewPage(st.id, cur + (dx < 0 ? 1 : -1));
  }
}

function previewUIClick(e) {
  const t = e.target;
  if (!t || !t.closest) return;
  // 刚滑完的合成 click 要压掉，但只压「点空白关闭」和「点缩略图开全屏」这两类：
  // ✕ / 上下页 / 弹窗关闭按钮是用户主动点的，不能被 400ms 的滑动保护期吞掉。
  const justSwiped = (Date.now() - pfvSwipeAt) < 400;
  if (t.closest('#pfvClose')) { closePreviewViewer(); return; }
  if (t.closest('#pfvPrev')) { pfvShowPage(pfvState.page - 1); return; }
  if (t.closest('#pfvNext')) { pfvShowPage(pfvState.page + 1); return; }
  if (pfvState.open && (t.id === 'pfvMask' || t.id === 'pfvStage')) {
    if (!justSwiped) closePreviewViewer();
    return;
  }
  if (t.closest('[data-close-myfile-detail]')) { closeOrderFileDetail(); return; }
  const dot = t.closest('[data-preview-dot]');
  if (dot) {
    const host = dot.closest('[data-preview-host]');
    if (host) setPreviewPage(String(host.dataset.fileId || ''), Number(dot.dataset.previewDot) || 1);
    return;
  }
  if (justSwiped) return;
  const host = t.closest('[data-preview-host]');
  if (host && t.closest('.myfile-preview-slide')) {
    const id = String(host.dataset.fileId || '');
    openPreviewViewer(id, Math.floor(Number(previewStateOf(id).page) || 1));
  }
}

/* 监听只装一遍：initMePage（每次进「我」页）和 openMyFiles（每次开弹窗）都会调，带哨兵 */
function setupMyFilePreviewUI() {
  if (pfvWired) return;
  pfvWired = true;
  document.addEventListener('click', previewUIClick);
  document.addEventListener('touchstart', previewHostTouchStart, { passive: true });
  document.addEventListener('touchend', previewHostTouchEnd, { passive: true });
  // 图片解码完成 → 淡入盖住骨架（load 不冒泡，用捕获阶段；懒加载后设 src 的也走这里）
  document.addEventListener('load', (e) => {
    const img = e.target;
    if (!img || !img.classList || !img.classList.contains('myfile-preview')) return;
    const slide = img.closest ? img.closest('.myfile-preview-slide') : null;
    if (slide) slide.classList.add('is-loaded');
  }, true);
  const mask = document.getElementById('pfvMask');
  if (mask) {
    mask.addEventListener('touchstart', pfvTouchStart, { passive: true });
    mask.addEventListener('touchmove', pfvTouchMove, { passive: false });
    mask.addEventListener('touchend', pfvTouchEnd, { passive: true });
    mask.addEventListener('touchcancel', pfvTouchCancel, { passive: true });
    const img = document.getElementById('pfvImg');
    if (img) {
      img.addEventListener('load', () => {
        img.style.visibility = 'visible';
        mask.classList.add('pfv-loaded');
        const tip = document.getElementById('pfvLoadingText');
        if (tip) tip.textContent = '正在加载预览图…';
      });
      img.addEventListener('error', () => {
        img.style.visibility = 'hidden';
        mask.classList.remove('pfv-loaded');
        const tip = document.getElementById('pfvLoadingText');
        if (tip) tip.textContent = '预览图加载失败，点空白处退出';
      });
    }
  }
  // 返回键：全屏层是自绘 fixed 层（不是 .modal-mask），要抢在 app.js 的默认处理之前收掉
  const prevBack = window.hnHandleBack;
  window.hnHandleBack = function () {
    if (pfvState.open) { closePreviewViewer(); return true; }
    if (typeof prevBack === 'function') return prevBack();
    return false;
  };
}

/* ================= 启动 ================= */

window.addEventListener('pagehide', () => { closePricePop(); closeStatusReasonPop(); stopOrderPolling(); stopUserOrdersPolling(); stopKeyPolling(); stopTempCountdown(); });
