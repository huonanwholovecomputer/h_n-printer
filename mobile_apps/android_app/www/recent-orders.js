/* ============================================================
   最近订单（管理员）—— 独立子视图（2026-11 需求 #3）
   ------------------------------------------------------------
   原来是「我」页里内嵌的最近 N 条列表（只有 N 条、翻不了页），现在搬到独立页：
   数据 GET /api/admin/recent_orders?page=&per_page=（后端 C5 契约，
   带 page 时返回 { success, orders, total, page, per_page, total_pages }；
   不带 page 是旧结构 —— 所以这里永远带 page，total 缺失时退化成单页）。

   交互：
     · 行内金额点一下 = 复制「订单号 — ¥金额」（与本地打印工具 Ctrl+C 同格式，me.js 的 buildPriceSimpleText）
     · 异常终态文字点一下 = 原因气泡（me.js 的 openStatusReasonPop：「复制」「关闭」）
     · 归属人点一下 = 脱敏/真名就地切换（不重渲染，避免滚动跳动）
     · 每页条数与「我的打印任务」同款下拉（10/20/50/100），页码顶/底各一份

   规则：本文件与小程序 pages/recent-orders/ 一一对应；金额一律取后端实收值，不重算。
   ============================================================ */

const RO_PER_PAGE_KEY = 'recentOrdersPerPage';
const RO_PER_PAGE_OPTIONS = [10, 20, 50, 100];

const roState = {
  orders: [],
  page: 1,
  perPage: 20,
  total: 0,
  totalPages: 0,
  loading: false,
  loaded: false,
  showPageSizePicker: false,
  noPermission: false,
};

function roPerPageInitial() {
  const saved = parseInt(localStorage.getItem(RO_PER_PAGE_KEY), 10);
  return RO_PER_PAGE_OPTIONS.indexOf(saved) >= 0 ? saved : 20;
}

/* 打开子视图：每次进入都从第 1 页开始（管理员的「最近订单」是流水视角，
   保留上次页码只会让人看到莫名其妙的中间页） */
function openRecentOrdersView() {
  if (typeof closeStatusReasonPop === 'function') closeStatusReasonPop();
  if (typeof closePricePop === 'function') closePricePop();
  roState.orders = [];
  roState.page = 1;
  roState.perPage = roPerPageInitial();
  roState.total = 0;
  roState.totalPages = 0;
  roState.loading = false;
  roState.loaded = false;
  roState.showPageSizePicker = false;
  roState.noPermission = false;
  const role = state.role;
  roState.noPermission = !(role === 'admin' || role === 'super_admin');
  const noPerm = document.getElementById('roNoPermission');
  const section = document.getElementById('roSection');
  if (noPerm) noPerm.style.display = roState.noPermission ? '' : 'none';
  if (section) section.style.display = roState.noPermission ? 'none' : '';
  document.getElementById('roList').innerHTML = '';
  document.getElementById('roPager').style.display = 'none';
  document.getElementById('roPagerBottom').style.display = 'none';
  document.getElementById('roSummary').style.display = 'none';
  document.getElementById('roEndHint').style.display = 'none';
  showView('view-recent-orders', '最近订单');
  if (!roState.noPermission) loadRecentOrdersView();
}

async function loadRecentOrdersView(silent) {
  const list = document.getElementById('roList');
  if (!silent) {
    roState.loading = true;
    list.innerHTML = '<view class="status-box"><text class="status-text">加载中...</text></view>';
  }
  try {
    const r = await api('/api/admin/recent_orders?page=' + roState.page + '&per_page=' + roState.perPage);
    if (r.status === 200 && r.data && r.data.success) {
      const rows = (r.data.orders || []).map(roDecorate);
      // 旧版后端不带 total：按本页条数退化，至少不会出现 NaN 页码或永远转圈
      const rawTotal = Number(r.data.total);
      const total = isFinite(rawTotal) && rawTotal >= 0 ? rawTotal : rows.length;
      const totalPages = Math.max(1, Math.ceil(total / roState.perPage));
      if (roState.page > totalPages) roState.page = totalPages;
      roState.orders = rows;
      roState.total = total;
      roState.totalPages = totalPages;
      roState.loaded = true;
      renderRecentOrdersView();
    } else if (!silent) {
      list.innerHTML = '<view class="status-box"><text class="status-text">'
        + escHtml((r.data && r.data.message) || '加载失败') + '</text></view>';
    }
  } catch (e) {
    if (!silent) list.innerHTML = '<view class="status-box"><text class="status-text">网络错误</text></view>';
  }
  roState.loading = false;
  // 行内要查订单对象（复制价格/原因气泡都靠 id 反查）
  meState.recentOrderRows = roState.orders;
  measureAll(150);
}

function roDecorate(o) {
  const total = Math.round((Number(o && o.total_price) || 0) * 100) / 100;
  return Object.assign({}, o, {
    // me.js 的 _priceOrderById 按 o.id 反查：后端给的是 order_id，这里补一份 id
    id: o.order_id,
    total_price: total,
    priceDisplay: total.toFixed(2),
    ownerText: o.owner_masked || o.owner || '—',
    timeShort: roShortTime(o.created_at),
    statText: (Number(o.file_count) || 0) + ' 文件 · ' + (Number(o.page_count) || 0) + ' 页',
  });
}

// MM-DD HH:MM（列表里不需要年份与秒）
function roShortTime(v) {
  const s = String(v == null ? '' : v).trim();
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  return m ? m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5] : s;
}

function roRowHTML(o) {
  const stLabel = ORDER_STATUS_MAP[o.status] || o.status || '';
  const statusCls = orderStatusClass(o.status);
  const tappable = typeof isReasonStatus === 'function' && isReasonStatus(o.status);
  const statusHtml = !stLabel ? '' : (tappable
    ? `<text class="order-status ${statusCls} status-reason-tap" data-ro-status="${escHtml(String(o.order_id))}">${escHtml(stLabel)} ⓘ</text>`
    : `<text class="order-status ${statusCls}">${escHtml(stLabel)}</text>`);
  return `<view class="ro-row">
      <view class="ro-row-line1">
        <text class="ro-order-no">${escHtml(o.order_number || '—')}</text>
        ${statusHtml}
        ${o.is_admin_print ? '<text class="ro-self-badge">管理员自打</text>' : ''}
      </view>
      <view class="ro-row-line2">
        <text class="ro-owner" data-ro-owner="${escHtml(String(o.order_id))}" data-owner-masked="${escHtml(o.owner_masked || o.owner || '—')}" data-owner-full="${escHtml(o.owner || o.owner_masked || '—')}">👤 ${escHtml(o.ownerText)}</text>
        <text class="ro-time">${escHtml(o.timeShort)}</text>
      </view>
      <view class="ro-row-line3">
        <text class="ro-stat">${escHtml(o.statText)}</text>
        <text class="ro-price-tap" data-ro-price="${escHtml(String(o.order_id))}">¥${o.priceDisplay}</text>
      </view>
    </view>`;
}

function renderRecentOrdersView() {
  const list = document.getElementById('roList');
  if (!roState.orders.length) {
    list.innerHTML = `
      <view class="empty-state">
        <view class="empty-illustration"><text class="empty-icon">🗂</text></view>
        <text class="empty-title">还没有订单</text>
        <text class="empty-desc">所有管理员提交过的订单会出现在这里。</text>
      </view>`;
  } else {
    list.innerHTML = roState.orders.map(roRowHTML).join('');
  }

  // 汇总
  const summary = document.getElementById('roSummary');
  const summaryText = document.getElementById('roSummaryText');
  if (summary && summaryText) {
    if (roState.total > 0) {
      summary.style.display = '';
      summaryText.textContent = '共 ' + roState.total + ' 条 · 第 ' + roState.page + '/' + roState.totalPages + ' 页';
    } else {
      summary.style.display = 'none';
    }
  }

  // 页码（顶/底）
  const nums = roPageNumbersHTML();
  document.getElementById('roPageNumbers').innerHTML = nums;
  document.getElementById('roPageNumbersBottom').innerHTML = nums;
  ['roPrev', 'roPrevBottom'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('page-btn-disabled', roState.page <= 1);
  });
  ['roNext', 'roNextBottom'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('page-btn-disabled', roState.page >= roState.totalPages);
  });
  document.getElementById('roPager').style.display = roState.totalPages > 0 ? '' : 'none';
  document.getElementById('roPagerBottom').style.display = roState.totalPages > 1 ? '' : 'none';
  document.getElementById('roEndHint').style.display =
    (roState.totalPages > 0 && roState.page >= roState.totalPages) ? '' : 'none';
  syncRoPageSizePicker();
  measureAll(150);
}

function roPageNumbersHTML() {
  const total = roState.totalPages;
  const current = roState.page;
  if (total <= 1) return '<view class="page-num page-num-active" data-ro-page="1">1</view>';
  const pages = [];
  if (total <= 7) {
    for (let i = 1; i <= total; i++) pages.push(i);
  } else {
    pages.push(1);
    if (current > 3) pages.push('...');
    for (let i = Math.max(2, current - 1); i <= Math.min(total - 1, current + 1); i++) pages.push(i);
    if (current < total - 2) pages.push('...');
    pages.push(total);
  }
  return pages.map(p => (p === '...'
    ? '<view class="page-ellipsis">…</view>'
    : `<view class="page-num ${p === current ? 'page-num-active' : ''}" data-ro-page="${p}">${p}</view>`)).join('');
}

function syncRoPageSizePicker() {
  const text = document.getElementById('roPageSizeText');
  if (text) text.textContent = roState.perPage + '条/页';
  const dd = document.getElementById('roPageSizeDropdown');
  if (dd) dd.classList.toggle('dropdown-show', roState.showPageSizePicker);
  const arrow = document.getElementById('roPageSizeArrow');
  if (arrow) arrow.classList.toggle('arrow-up', roState.showPageSizePicker);
  document.querySelectorAll('#roPageSizeDropdown .page-size-option').forEach(opt => {
    opt.classList.toggle('option-active', parseInt(opt.dataset.size, 10) === roState.perPage);
  });
}

function toggleRoPageSizePicker() {
  if (roState.showPageSizePicker) { closeRoPageSizePicker(); return; }
  roState.showPageSizePicker = true;
  const dd = document.getElementById('roPageSizeDropdown');
  const trigger = document.getElementById('roPageSizeSelector');
  // 下拉移到 body 末尾（逃出 .scroll-content 的 translateY 变换层，fixed 才真相对视口）
  if (typeof openDropdownFixed === 'function') openDropdownFixed(dd, trigger);
  else if (dd) dd.classList.add('dropdown-show');
  syncRoPageSizePicker();
}

function closeRoPageSizePicker() {
  if (!roState.showPageSizePicker) return;
  roState.showPageSizePicker = false;
  syncRoPageSizePicker();
}

function selectRoPageSize(size) {
  if (RO_PER_PAGE_OPTIONS.indexOf(size) < 0) return;
  roState.showPageSizePicker = false;
  if (size === roState.perPage) { syncRoPageSizePicker(); return; }
  roState.perPage = size;
  roState.page = 1;
  try { localStorage.setItem(RO_PER_PAGE_KEY, String(size)); } catch (e) { /* 隐私模式忽略 */ }
  syncRoPageSizePicker();
  loadRecentOrdersView();
  roScrollToTop();
}

function changeRoPage(page) {
  if (page < 1 || page > roState.totalPages || page === roState.page) return;
  roState.page = page;
  loadRecentOrdersView();
  roScrollToTop();
}

function roScrollToTop() {
  const engine = scrollEngines && scrollEngines.recentOrders;
  if (!engine) return;
  engine.measure();
  if (engine.maxY <= 0) return;
  if (engine.y <= 2) return;
  engine.scrollTo(0, 240);
}

function roOwnerTap(el) {
  const full = el.dataset.ownerFull || '';
  const masked = el.dataset.ownerMasked || '';
  el.textContent = (el.textContent.replace(/^👤\s*/, '') === full) ? '👤 ' + masked : '👤 ' + full;
}

function roPriceTap(orderId) {
  const rows = roState.orders || [];
  const o = rows.filter(x => String(x.order_id) === String(orderId))[0];
  if (!o) return;
  // 与本地工具 Ctrl+C 同格式：`订单号 — ¥金额`（无订单号只写金额）
  const text = typeof buildPriceSimpleText === 'function'
    ? buildPriceSimpleText(o)
    : '¥' + o.priceDisplay;
  copyText(text, '已复制价格');
}

/* 事件绑定：只绑一次（子视图是常驻 DOM，不随打开关闭重建） */
let _roBound = false;
function initRecentOrdersView() {
  if (_roBound) return;
  _roBound = true;
  const bind = (id, fn) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('click', fn);
  };
  bind('roPrev', () => changeRoPage(roState.page - 1));
  bind('roNext', () => changeRoPage(roState.page + 1));
  bind('roPrevBottom', () => changeRoPage(roState.page - 1));
  bind('roNextBottom', () => changeRoPage(roState.page + 1));
  bind('roRefresh', () => loadRecentOrdersView());
  bind('roPageSizeSelector', (e) => { e.stopPropagation(); toggleRoPageSizePicker(); });
  ['roPageNumbers', 'roPageNumbersBottom'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('click', (e) => {
      const num = e.target.closest('[data-ro-page]');
      if (num) changeRoPage(parseInt(num.dataset.roPage, 10));
    });
  });
  const dd = document.getElementById('roPageSizeDropdown');
  if (dd) dd.addEventListener('click', (e) => {
    const opt = e.target.closest('[data-size]');
    if (!opt) return;
    e.stopPropagation();
    selectRoPageSize(parseInt(opt.dataset.size, 10));
  });
  const list = document.getElementById('roList');
  if (list) list.addEventListener('click', (e) => {
    const priceEl = e.target.closest('[data-ro-price]');
    if (priceEl) { e.stopPropagation(); roPriceTap(priceEl.dataset.roPrice); return; }
    const statusEl = e.target.closest('[data-ro-status]');
    if (statusEl) { e.stopPropagation(); openStatusReasonPop(statusEl, statusEl.dataset.roStatus); return; }
    const ownerEl = e.target.closest('[data-ro-owner]');
    if (ownerEl) { e.stopPropagation(); roOwnerTap(ownerEl); }
  });
  // 点空白处收起条数下拉
  document.addEventListener('click', (e) => {
    if (!roState.showPageSizePicker) return;
    if (e.target.closest('#roPageSizeSelector')) return;
    if (e.target.closest('#roPageSizeDropdown')) return;
    closeRoPageSizePicker();
  });
}

// 退出子视图 / 切 tab 时收起下拉（与 closeUoPageSizePicker 同口径）
function closeRoPageSizePickerIfOpen() {
  if (roState.showPageSizePicker) closeRoPageSizePicker();
}
