/* HN Cloud Print — Android App Logic */
/* Replaces WeChat wx.* APIs with standard Web APIs */

const BASE_URL = 'https://hn-space.cn';
let token = localStorage.getItem('hn_token') || '';
let userId = localStorage.getItem('hn_openid') || '';
let userRole = localStorage.getItem('hn_role') || 'guest';
let selectedFiles = [];
let orders = [];
let ordersPage = 1;
let ordersHasMore = false;

/* ========== Init ========== */
document.addEventListener('DOMContentLoaded', () => {
  setupTabBar();
  setupFileInput();
  setupButtons();
  setupNickname();
  checkPrinterStatus();
  if (token) { loadProfile(); loadOrders(); }
  updateRoleUI();
  setInterval(checkPrinterStatus, 30000);
});

/* ========== Tab Navigation ========== */
function setupTabBar() {
  document.querySelectorAll('.tab-item').forEach(item => {
    item.addEventListener('click', () => {
      const tab = item.dataset.tab;
      document.querySelectorAll('.tab-item').forEach(t => { t.classList.remove('active'); t.querySelector('.tab-text').classList.remove('active'); });
      item.classList.add('active'); item.querySelector('.tab-text').classList.add('active');
      document.getElementById('page-print').style.display = tab === 'print' ? '' : 'none';
      document.getElementById('page-me').style.display = tab === 'me' ? '' : 'none';
      document.getElementById('navTitle').textContent = tab === 'print' ? '提交打印' : '我';
      if (tab === 'me') { loadProfile(); loadOrders(); }
    });
  });
}

function switchToMe() {
  const meTab = document.querySelector('.tab-item[data-tab="me"]');
  if (meTab) meTab.click();
}

/* ========== File Input ========== */
function setupFileInput() {
  document.getElementById('addFileBtn').addEventListener('click', () => document.getElementById('fileInput').click());
  document.getElementById('fileInput').addEventListener('change', (e) => {
    for (const file of e.target.files) {
      const ext = '.' + file.name.split('.').pop().toLowerCase();
      const allowed = ['.pdf','.doc','.docx','.txt','.md','.html','.htm','.jpg','.jpeg','.png','.bmp','.gif','.webp'];
      if (!allowed.includes(ext)) continue;
      const isExcel = ext === '.xls' || ext === '.xlsx';
      const fi = {
        name: file.name, size: file.size, file: file,
        sizeDisplay: (file.size / 1024).toFixed(1),
        fileId: null, uploading: true, progress: 0, failed: false,
        copies: 1, pageRange: '', duplex: 'on', excelWarning: isExcel,
      };
      selectedFiles.push(fi);
      renderFileList();
      uploadFile(fi, selectedFiles.length - 1);
    }
    e.target.value = '';
  });
}

function removeFile(idx) {
  selectedFiles.splice(idx, 1);
  renderFileList();
}

function renderFileList() {
  const container = document.getElementById('fileList');
  const badge = document.getElementById('fileCount');
  badge.style.display = selectedFiles.length ? '' : 'none';
  badge.textContent = selectedFiles.length;
  container.innerHTML = selectedFiles.map((f, i) => `
    <div class="file-card">
      <div class="file-card-top">
        <div class="file-name-area">
          <span class="file-name">${esc(f.name)}</span>
          <span class="file-size">${f.sizeDisplay} KB</span>
        </div>
        <button class="file-remove" onclick="removeFile(${i})">✕</button>
      </div>
      <div class="file-status-area">
        ${f.uploading ? `<div class="upload-row"><span class="status-label uploading">上传中…</span><span class="upload-pct">${f.progress}%</span></div><div class="progress-track"><div class="progress-fill" style="width:${f.progress}%"></div></div>` :
          f.failed ? `<span class="status-label failed">上传失败</span>` :
          f.excelWarning ? `<span class="status-label warn">Excel 不支持自动打印</span>` :
          f.fileId ? `<span class="status-label done">已上传</span>` : ''}
      </div>
      ${f.fileId && !f.excelWarning ? `
      <div class="file-controls">
        <div class="control-row">
          <span class="control-label">份数</span>
          <div class="stepper">
            <button class="stepper-btn ${f.copies <= 1 ? 'disable' : ''}" onclick="changeCopies(${i},-1)">−</button>
            <input class="stepper-input" type="number" value="${f.copies}" onchange="setCopies(${i},this.value)">
            <button class="stepper-btn" onclick="changeCopies(${i},1)">+</button>
          </div>
        </div>
        <div class="control-row">
          <span class="control-label">范围</span>
          <input class="control-input" placeholder="全部（如 1-5,7,9）" value="${escHtml(f.pageRange)}" onchange="setPageRange(${i},this.value)">
        </div>
        <div class="control-row">
          <span class="control-label">模式</span>
          <div class="duplex-toggle">
            <div class="duplex-slider ${f.duplex === 'on' ? 'right' : ''}"></div>
            <div class="duplex-opt ${f.duplex === 'off' ? 'active' : ''}" onclick="setDuplex(${i},'off')">单面</div>
            <div class="duplex-opt ${f.duplex === 'on' ? 'active' : ''}" onclick="setDuplex(${i},'on')">双面</div>
          </div>
        </div>
      </div>` : ''}
    </div>
  `).join('');
}

function esc(s) { return s.replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function changeCopies(idx, delta) {
  const v = selectedFiles[idx].copies + delta;
  if (v >= 1 && v <= 99) { selectedFiles[idx].copies = v; renderFileList(); }
}
function setCopies(idx, val) {
  const v = parseInt(val) || 1;
  selectedFiles[idx].copies = Math.max(1, Math.min(99, v));
  renderFileList();
}
function setPageRange(idx, val) { selectedFiles[idx].pageRange = val; }
function setDuplex(idx, val) { selectedFiles[idx].duplex = val; renderFileList(); }

/* ========== File Upload (fetch with XHR for progress) ========== */
function uploadFile(fi, idx) {
  if (!token) { ensureLogin(() => uploadFile(fi, idx)); return; }
  const xhr = new XMLHttpRequest();
  xhr.open('POST', BASE_URL + '/api/upload');
  xhr.setRequestHeader('Authorization', 'Bearer ' + token);
  const fd = new FormData(); fd.append('file', fi.file);
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) { fi.progress = Math.round(e.loaded / e.total * 100); renderFileList(); }
  };
  xhr.onload = () => {
    if (xhr.status === 401) { fi.uploading = false; renderFileList(); ensureLogin(() => { fi.uploading = true; uploadFile(fi, idx); }); return; }
    try {
      const data = JSON.parse(xhr.responseText);
      if (data.file_id || data.id) {
        fi.fileId = data.file_id || data.id; fi.uploading = false; fi.progress = 100;
      } else {
        fi.failed = true; fi.uploading = false; alert(data.message || '上传失败');
      }
    } catch(e) { fi.failed = true; fi.uploading = false; }
    renderFileList();
  };
  xhr.onerror = () => { fi.failed = true; fi.uploading = false; renderFileList(); };
  xhr.send(fd);
}

/* ========== Submit Order ========== */
function submitOrder() {
  if (userRole !== 'user' && userRole !== 'admin') {
    document.getElementById('denyModal').style.display = 'flex'; return;
  }
  if (!selectedFiles.length) { alert('请选择文件'); return; }
  if (selectedFiles.some(f => f.uploading)) { alert('文件上传中'); return; }
  if (selectedFiles.some(f => f.failed || !f.fileId)) { alert('有文件未上传成功'); return; }

  const btn = document.getElementById('submitBtn');
  btn.disabled = true; btn.textContent = '提交中…';

  fetch(BASE_URL + '/api/submit_order', {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      duplex: 'on',
      files: selectedFiles.map(f => ({
        file_id: f.fileId, file: f.name,
        copies: f.copies, page_range: f.pageRange, duplex: f.duplex,
      })),
    }),
  }).then(r => r.json()).then(data => {
    btn.disabled = false; btn.textContent = '提交打印任务';
    if (data.success) {
      document.getElementById('successModal').style.display = 'flex';
      selectedFiles = []; renderFileList();
    } else {
      alert(data.message || '提交失败');
    }
  }).catch(() => { btn.disabled = false; btn.textContent = '提交打印任务'; alert('网络错误'); });
}

function closeSuccess() { document.getElementById('successModal').style.display = 'none'; }
function viewOrders() { closeSuccess(); switchToMe(); }
function closeDeny() { document.getElementById('denyModal').style.display = 'none'; }

/* ========== Login (device-based registration) ========== */
function ensureLogin(callback) {
  const deviceId = getDeviceId();
  fetch(BASE_URL + '/api/device_login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ device_id: deviceId }),
  }).then(r => r.json()).then(data => {
    if (data.success && data.token) {
      token = data.token; userId = data.openid || '';
      localStorage.setItem('hn_token', token);
      localStorage.setItem('hn_openid', userId);
      loadProfile();
      if (callback) callback();
    } else {
      alert('登录失败，请检查网络连接');
    }
  }).catch(() => alert('网络错误，请重试'));
}

function getDeviceId() {
  let id = localStorage.getItem('hn_device_id');
  if (!id) { id = 'dev_' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36); localStorage.setItem('hn_device_id', id); }
  return id;
}

/* ========== Profile ========== */
function loadProfile() {
  if (!token) return;
  fetch(BASE_URL + '/api/profile', { headers: { 'Authorization': 'Bearer ' + token } })
    .then(r => r.json()).then(data => {
      if (data.success) {
        document.getElementById('nicknameInput').value = data.nickname || '';
        if (data.avatar_url) document.getElementById('avatarImg').src = data.avatar_url;
        userRole = data.role || 'guest';
        localStorage.setItem('hn_role', userRole);
        document.getElementById('roleLabel').textContent =
          data.is_super_admin ? '超级管理员' : userRole === 'admin' ? '管理员' : userRole === 'user' ? '普通用户' : '访客';
        if (data.temp_until) {
          document.getElementById('tempCountdown').textContent = '剩余时间: ' + formatRemain(data.temp_until);
        }
        updateRoleUI();
      }
    });
}

function setupNickname() {
  document.getElementById('nicknameInput').addEventListener('blur', function() {
    const val = this.value.trim();
    if (!val || !token) return;
    fetch(BASE_URL + '/api/profile', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
      body: JSON.stringify({ nickname: val }),
    });
  });
}

/* ========== Redeem Key ========== */
function redeemKey() {
  const key = document.getElementById('redeemInput').value.trim().toUpperCase();
  if (key.length !== 8) { alert('密钥为8位字符'); return; }
  const btn = document.getElementById('redeemBtn');
  btn.disabled = true; btn.textContent = '验证中…';
  fetch(BASE_URL + '/api/license/redeem', {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
    body: JSON.stringify({ key }),
  }).then(r => r.json()).then(data => {
    btn.disabled = false; btn.textContent = '验证';
    if (data.success) { alert('许可验证成功！'); loadProfile(); loadOrders(); }
    else { alert(data.message || '密钥无效'); }
  }).catch(() => { btn.disabled = false; btn.textContent = '验证'; alert('网络错误'); });
}

/* ========== Orders ========== */
function loadOrders(page = 1, append = false) {
  if (!token) { document.getElementById('ordersLoading').style.display = 'none'; return; }
  if (!append) { document.getElementById('ordersLoading').style.display = ''; ordersPage = 1; }
  fetch(BASE_URL + '/api/orders?page=' + page + '&per_page=20', {
    headers: { 'Authorization': 'Bearer ' + token },
  }).then(r => r.json()).then(data => {
    document.getElementById('ordersLoading').style.display = 'none';
    if (data.success) {
      const newOrders = (data.orders || []).map(o => {
        if (o.files) o.files.forEach(f => {
          f.sizeDisplay = f.size ? (f.size / 1024).toFixed(1) + ' KB' : '';
        });
        return o;
      });
      orders = append ? [...orders, ...newOrders] : newOrders;
      ordersPage = page;
      ordersHasMore = orders.length < (data.total || 0);
      renderOrders();
    }
  });
}

function renderOrders() {
  document.getElementById('ordersEmpty').style.display = orders.length ? 'none' : '';
  document.getElementById('orderCountBadge').style.display = orders.length ? '' : 'none';
  document.getElementById('orderCountBadge').textContent = orders.length;
  document.getElementById('loadMoreWrap').style.display = ordersHasMore ? '' : 'none';

  const statusMap = { queued:'排队中', printing:'打印中', sent:'已完成', failed:'失败', canceled:'已取消' };
  document.getElementById('orderList').innerHTML = orders.map(o => `
    <div class="order-card" id="order-${o.id}" onclick="toggleOrder(${o.id})">
      <div class="order-main">
        <span class="order-filename">${esc(o.file_summary || o.file || '')}</span>
        <span class="order-badge badge-${o.status}">${statusMap[o.status] || o.status}</span>
      </div>
      <div class="order-meta">
        <span>📄 ${o.total_pages || (o.page_count * o.copies) || '?'} 页</span>
        <span>📋 ${o.total_copies || o.copies || 1} 份 · ${o.duplex === 'on' ? '双面' : '单面'}</span>
      </div>
      <div class="order-footer">
        <span class="order-time">${o.created_at || ''}</span>
      </div>
      <div class="order-detail" id="detail-${o.id}" style="display:none">
        <div class="detail-section">
          <div class="detail-section-title">文件列表 (${(o.files||[]).length})</div>
          ${(o.files || []).map(f => `
            <div class="detail-file-row">
              <span class="detail-file-name">${esc(f.original_name || f.file_name || '')}</span>
              <div class="detail-file-right">
                <div>${f.copies} 份 × ${f.page_count} 页</div>
                ${f.page_range ? `<div style="font-size:10px;color:var(--orange)">范围:${f.page_range}</div>` : ''}
              </div>
            </div>
          `).join('')}
        </div>
        ${o.status === 'queued' ? `<button class="btn-cancel-sm" onclick="event.stopPropagation();cancelOrder(${o.id})">取消任务</button>` : ''}
      </div>
    </div>
  `).join('');
}

function toggleOrder(id) {
  const detail = document.getElementById('detail-' + id);
  const card = document.getElementById('order-' + id);
  if (detail.style.display === 'none') {
    detail.style.display = ''; card.classList.add('expanded');
  } else {
    detail.style.display = 'none'; card.classList.remove('expanded');
  }
}

function cancelOrder(id) {
  if (!confirm('确定取消？')) return;
  fetch(BASE_URL + '/api/cancel_order', {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
    body: JSON.stringify({ order_id: String(id) }),
  }).then(r => r.json()).then(data => {
    if (data.success) { alert('已取消'); loadOrders(); }
    else alert(data.message || '取消失败');
  });
}

function loadMoreOrders() { loadOrders(ordersPage + 1, true); }

/* ========== Printer Status ========== */
function checkPrinterStatus() {
  fetch(BASE_URL + '/api/printer_status')
    .then(r => r.json()).then(data => {
      const dot = document.querySelector('.status-dot');
      const text = document.getElementById('statusText');
      if (data.active) {
        dot.className = 'status-dot online';
        text.textContent = '打印机在线';
        text.style.color = 'var(--green)';
      } else {
        dot.className = 'status-dot offline';
        text.textContent = '打印机离线';
        text.style.color = 'var(--gray)';
      }
    }).catch(() => {
      document.querySelector('.status-dot').className = 'status-dot offline';
      document.getElementById('statusText').textContent = '打印机离线';
    });
}

/* ========== Role UI ========== */
function updateRoleUI() {
  document.getElementById('guestSection').style.display = userRole === 'guest' ? '' : 'none';
  document.getElementById('userSection').style.display = (userRole === 'user' || userRole === 'admin') ? '' : 'none';
}

/* ========== Helpers ========== */
function formatRemain(str) {
  if (!str) return '';
  const parts = str.replace(/-/g,'/').split(' ');
  if (parts.length !== 2) return str;
  const remain = new Date(parts[0] + ' ' + parts[1]).getTime() - Date.now();
  if (remain <= 0) return '已过期';
  const m = Math.floor(remain / 60000);
  const s = Math.floor((remain % 60000) / 1000);
  return m + '分' + (s < 10 ? '0' : '') + s + '秒';
}

/* ========== Button Setup ========== */
function setupButtons() {
  document.getElementById('submitBtn').addEventListener('click', submitOrder);
  document.getElementById('modalContinue').addEventListener('click', closeSuccess);
  document.getElementById('modalViewOrders').addEventListener('click', viewOrders);
  document.getElementById('denyGoMe').addEventListener('click', () => { closeDeny(); switchToMe(); });
  document.getElementById('redeemBtn').addEventListener('click', redeemKey);
  document.getElementById('loadMoreBtn').addEventListener('click', loadMoreOrders);
  // Close modals on backdrop click
  document.querySelectorAll('.modal-mask').forEach(m => m.addEventListener('click', function(e) {
    if (e.target === this) { this.style.display = 'none'; }
  }));
}
