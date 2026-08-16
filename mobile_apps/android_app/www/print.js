/* HN Cloud Print — Android App 打印流程（与小程序 index 页视觉对齐） */

const SUPPORTED_EXTS = ['.pdf', '.doc', '.docx', '.txt', '.csv', '.md',
  '.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp', '.tiff', '.tif'];
const IMAGE_EXTS = ['.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp', '.tiff', '.tif'];
const UNSUPPORTED_EXTS = ['.xls', '.xlsx', '.ppt', '.pptx', '.dwg', '.dxf'];
const MAX_FILES = 20;
const MAX_FILE_MB = 50;
const MAX_POLL_ATTEMPTS = 60;
// 大图压缩阈值与压缩后最长边（对齐小程序"相册选择压缩图"体验）
const COMPRESS_IMAGE_THRESHOLD = 2 * 1024 * 1024; // 超过 2MB 的图片尝试压缩
const COMPRESS_MAX_SIDE = 1600;                    // 压缩后最长边上限（px）

const printState = {
  selectedFiles: [],
  pricingLoaded: false,
  deliveryEnabled: false,
  deliveryLocation: '1号楼北楼',
  deliveryLocations: ['1号楼北楼', '1号楼南楼', '图书馆', '教学楼E/F', '女生宿舍'],
  deliveryPercentages: { '1号楼北楼': 0, '1号楼南楼': 5, '图书馆': 15, '教学楼E/F': 20, '女生宿舍': 10 },
  deliveryPercent: 0,
  urgency: '低',
  urgencyOptions: ['低', '中', '高'],
  urgencyPrices: { '低': 0, '中': 0.08, '高': 0.15 },
  urgencyPrice: 0,
  coverPage: false,
  coverPagePrice: 0.10,
  autoPrintEnabled: false,
  autoPrintGlow: false,
  adminPrintEnabled: false,  // 管理员自行打印（仅管理员可见）
  scheduleMode: 'now',
  scheduleDayIndex: 0,
  scheduleTime: '',
  countdownMin: 5,
  countdownSec: 0,
  submitting: false,
  _lastOrderResult: null,
  _pollTimers: {},
  _uploadTimers: {},
  _fileListPx: 0,
  _wheelValues: { hour: 0, minute: 0, minuteValue: -1, cdMin: 5, cdSec: 0 },
};

/* ================= 初始化 ================= */

function initPrintPage() {
  setupFileInput();
  setupPrintButtons();
  loadPricing();
  refreshPrintRoleUI();
  buildScheduleDays();
  scheduleMeasureSoon();
  pageReady();
}

function onPrintTabShown() {
  refreshPrintRoleUI();
  scheduleMeasureSoon();
}

let _printEntrancePlayed = false;

// 页面就绪：入场动画已由各卡片 .enter-XX 类（animation 简写自带延迟 0.5~1.1s）驱动，
// 无需 JS 干预；此函数保留仅作启动标记
function pageReady() {
  if (_printEntrancePlayed) return;
  _printEntrancePlayed = true;
}

function scheduleMeasureSoon() {
  setTimeout(() => measureAll(), 80);
  setTimeout(() => measureAll(), 400);
}

// 管理员专属卡片显示：仅切换 display；入场动画由 .enter-XX 类的 animation 简写自带延迟驱动
// （无障碍打印 0.9s / 管理员自行打印 1.0s），显示即按各自延迟播放
function showAdminOnlySection(el) {
  if (!el) return;
  el.style.display = state.role === 'admin' ? '' : 'none';
}

function refreshPrintRoleUI() {
  const coverSw = document.getElementById('coverSwitch');
  const coverTag = document.getElementById('coverPriceTag');
  const coverReq = document.getElementById('coverRequiredTag');
  if (state.role === 'user') {
    printState.coverPage = true;
    coverSw.classList.add('switch-on');
    coverSw.classList.add('locked');
    coverTag.style.display = '';
    coverReq.style.display = '';
  } else {
    coverSw.classList.remove('locked');
    coverReq.style.display = 'none';
  }
  // 管理员专属卡片（无障碍打印 / 管理员自行打印）统一显示逻辑：
  // 隐藏→显示时强制重启动画（各自内联 animation-delay 0.9s / 1.0s），保证出现顺序确定
  showAdminOnlySection(document.getElementById('autoPrintSection'));
  showAdminOnlySection(document.getElementById('adminPrintSection'));
  updateCoverPrice();
  updateScheduleUI();
}

/* ================= 文件选择 ================= */

function setupFileInput() {
  document.getElementById('addFileBtn').addEventListener('click', () => {
    if (state.role === 'guest') {
      showToast('请先兑换许可密钥后再选择文件');
      return;
    }
    document.getElementById('fileInput').click();
  });
  document.getElementById('fileInput').addEventListener('change', async (e) => {
    let compressedCount = 0;
    for (const file of e.target.files) {
      if (printState.selectedFiles.length >= MAX_FILES) {
        showToast('最多 20 个文件');
        break;
      }
      const name = file.name || '';
      const size = file.size || 0;
      if (size > MAX_FILE_MB * 1024 * 1024) {
        showToast('文件超过 50MB 限制');
        continue;
      }
      const ext = name.slice(name.lastIndexOf('.')).toLowerCase();
      if (!ext || ext === '.') { showToast('不支持的文件格式'); continue; }
      const isImage = IMAGE_EXTS.includes(ext);
      const isUnsupported = UNSUPPORTED_EXTS.includes(ext);
      if (!SUPPORTED_EXTS.includes(ext) && !isUnsupported) {
        showToast('不支持 ' + ext + ' 格式');
        continue;
      }
      // 大图自动压缩（对齐小程序"相册选择压缩图"体验）：超过 2MB 的图片先尝试 canvas 压缩，
      // 压缩失败（无法解码等）回退原文件上传，不直接拦截。
      // 注意局部变量不能叫 uploadFile（会遮蔽全局上传函数）
      let fileToUpload = file;
      let uploadSize = size;
      if (isImage && size > COMPRESS_IMAGE_THRESHOLD) {
        const cf = await compressImageFile(file);
        if (cf) { fileToUpload = cf; uploadSize = cf.size; compressedCount++; }
      }
      const fi = {
        name, size: uploadSize, file: fileToUpload,
        sizeDisplay: (uploadSize / 1024).toFixed(1),
        fileId: null, uploading: true, progress: 0, failed: false,
        copies: 1,
        rangeLines: [{ value: '', error: '' }],
        pageRange: '',
        duplex: isImage ? 'off' : 'on',
        imageOrientation: 'auto',
        isImage, excelWarning: isUnsupported, unsupportedFormat: false,
        pageCount: 0, pageCountStatus: '', singlePage: false,
        entering: true, removing: false,
      };
      printState.selectedFiles.push(fi);
      renderFileList();
      updateFileBadge(true);
      uploadFile(printState.selectedFiles.length - 1);
    }
    if (compressedCount > 0) showToast(compressedCount + ' 张大图已自动压缩后上传');
    e.target.value = '';
  });
}

/* 大图压缩：canvas 缩放 + JPEG 渐进降质，目标 ≤ 2MB（对齐小程序压缩上传体验）。
   返回新 File（.jpg）或 null（解码/压缩失败，回退原文件） */
async function compressImageFile(file) {
  try {
    const bitmap = await createImageBitmap(file);
    let { width, height } = bitmap;
    const maxSide = Math.max(width, height);
    if (maxSide > COMPRESS_MAX_SIDE) {
      const ratio = COMPRESS_MAX_SIDE / maxSide;
      width = Math.max(1, Math.round(width * ratio));
      height = Math.max(1, Math.round(height * ratio));
    }
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(bitmap, 0, 0, width, height);
    if (bitmap.close) bitmap.close();
    let quality = 0.85;
    let blob = await new Promise(res => canvas.toBlob(res, 'image/jpeg', quality));
    while (blob && blob.size > COMPRESS_IMAGE_THRESHOLD && quality > 0.4) {
      quality -= 0.1;
      blob = await new Promise(res => canvas.toBlob(res, 'image/jpeg', quality));
    }
    if (blob && blob.size < file.size) {
      const newName = file.name.replace(/\.[^.]+$/, '') + '.jpg';
      return new File([blob], newName, { type: 'image/jpeg', lastModified: Date.now() });
    }
  } catch (e) { /* 解码失败（如 TIFF）→ 回退原文件 */ }
  return null;
}

/* ================= 文件列表渲染（小程序类名） ================= */

function fileCardStatusHTML(f) {
  if (f.uploading) {
    return `
      <view class="upload-row"><text class="status-label uploading">上传中…</text><text class="upload-pct">${f.progress}%</text></view>
      <view class="progress-track"><view class="progress-fill" style="width:${f.progress}%"></view></view>`;
  }
  if (f.failed) {
    return `
      <view class="file-retry-row">
        <text class="status-label failed">上传失败</text>
        <text class="file-retry-btn" data-action="retry">重试</text>
      </view>`;
  }
  if (f.excelWarning) {
    return `<text class="status-label warn">该文件类型不支持自动打印，请联系管理员</text>`;
  }
  if (f.fileId) {
    let bar = '';
    if (!f.isImage && !f.excelWarning) {
      if (f.pageCountStatus === 'analyzing') {
        bar = `<view class="page-analysis-bar"><view class="analysis-dot-pulse"></view><text class="analysis-text">正在分析页数，本地打印工具转换中…</text></view>`;
      } else if (f.pageCountStatus === 'offline') {
        bar = `<view class="page-analysis-bar offline"><text class="analysis-dot-warn">⚠</text><text class="analysis-text">本地打印工具离线，无法返回总页数</text></view>`;
      } else if (f.pageCountStatus === 'confirmed' && f.pageCount > 0) {
        bar = `<view class="page-analysis-bar confirmed"><text class="analysis-dot-done">✓</text><text class="analysis-text">共 ${f.pageCount} 页</text></view>`;
      }
    }
    return `<text class="status-label done">已上传</text>${bar}`;
  }
  return '';
}

function fileControlsHTML(f) {
  if (f.excelWarning || f.unsupportedFormat) return '';
  let html = `
    <view class="control-row">
      <text class="control-label">份数</text>
      <view class="stepper">
        <view class="stepper-btn ${f.copies <= 1 ? 'stepper-disabled' : ''}" data-action="copies-minus" data-hover="stepper-hover">−</view>
        <input class="stepper-input" type="number" min="1" max="99" value="${f.copies}" disabled>
        <view class="stepper-btn" data-action="copies-plus" data-hover="stepper-hover">+</view>
      </view>
    </view>`;
  if (f.isImage) {
    html += `
      <view class="control-row">
        <text class="control-label">方向</text>
        <view class="img-ori-toggle">
          <view class="img-ori-slider ${f.imageOrientation === 'landscape' ? 'slider-landscape' : ''} ${f.imageOrientation === 'portrait' ? 'slider-portrait' : ''}"></view>
          <view class="img-ori-opt ${!f.imageOrientation || f.imageOrientation === 'auto' ? 'opt-active' : ''}" data-action="ori" data-value="auto">自动</view>
          <view class="img-ori-opt ${f.imageOrientation === 'landscape' ? 'opt-active' : ''}" data-action="ori" data-value="landscape">横向</view>
          <view class="img-ori-opt ${f.imageOrientation === 'portrait' ? 'opt-active' : ''}" data-action="ori" data-value="portrait">竖向</view>
        </view>
      </view>`;
  } else if (f.pageCount !== 1) {
    const known = f.pageCountStatus === 'confirmed' && f.pageCount > 0;
    // 对齐小程序：输入区常驻渲染（页数确认后带 state-collapsed 收起过渡），摘要仅在确认后显示
    const linesMaxH = ((f.rangeLines.length) * 100 + 20) * (100 / 750);
    html += `
      <view class="control-row range-control-multi">
        <text class="control-label">范围</text>
        <view class="range-lines-wrap">
          <view class="range-unknown-warn ${known ? 'range-warn-collapsed' : ''}">⚠ 文档页数未知，若页数范围错误，该任务会直接记为"被打回"</view>
          <view class="range-inputs-state ${known ? 'state-collapsed' : ''}" style="max-height:${linesMaxH.toFixed(3)}cqw">
            ${f.rangeLines.map((line, li) => `
              <view class="range-line-row ${line.entering ? 'range-line-entering' : ''}">
                <input class="range-line-input ${line.error ? 'range-input-err' : ''}" data-action="range-line" data-line="${li}"
                  value="${escHtml(line.value)}" placeholder="请输入第${li + 1}个页面范围(如1-5或7)">
                ${line.error ? `<text class="range-err-msg range-line-err">${escHtml(line.error)}</text>` : ''}
              </view>`).join('')}
          </view>
          ${known ? `
          <view class="range-summary-trigger" data-action="open-range">
            <text class="range-summary-text ${f.pageRange ? '' : 'range-summary-all'}">${escHtml(f.pageRange ? '已选 ' + f.pageRange : '全部页')}</text>
            <text class="range-summary-arrow">›</text>
          </view>` : ''}
        </view>
      </view>`;
    // 对齐小程序：模式行始终渲染，单页时收起淡出（保留过渡，卡片高度平滑变化）
    html += `
      <view class="control-row mode-row ${f.singlePage ? 'mode-row-collapsed' : ''}">
        <text class="control-label">模式</text>
        <view class="duplex-toggle">
          <view class="duplex-slider ${f.duplex === 'on' ? 'slider-right' : ''}"></view>
          <view class="duplex-opt ${f.duplex === 'off' ? 'opt-active' : ''}" data-action="duplex" data-value="off">单面</view>
          <view class="duplex-opt ${f.duplex === 'on' ? 'opt-active' : ''}" data-action="duplex" data-value="on">双面</view>
        </view>
      </view>`;
  }
  return html;
}

function renderFileList() {
  const container = document.getElementById('fileList');
  container.innerHTML = printState.selectedFiles.map((f, i) => `
    <view class="file-card ${f.entering ? 'card-entering' : ''} ${f.removing ? 'card-removing' : ''}">
      <view class="file-card-top">
        <view class="file-name-area">
          <text class="file-name">${esc(f.name)}</text>
          <text class="file-size">${f.sizeDisplay} KB</text>
        </view>
        <view class="file-remove" data-action="remove">✕</view>
      </view>
      <view class="file-status-area">${fileCardStatusHTML(f)}</view>
      ${!f.excelWarning ? `<view class="file-controls">${fileControlsHTML(f)}</view>` : ''}
    </view>`).join('');
  // 入场动画结束后清除 entering
  if (printState.selectedFiles.some(f => f.entering)) {
    setTimeout(() => {
      let changed = false;
      printState.selectedFiles.forEach(f => { if (f.entering) { f.entering = false; changed = true; } });
      if (changed) renderFileList();
    }, 800);
  }
  recalcFileListHeight();
  bindToggleDrags();
}

// 模式/方向滑块：按住拖动（拖动中跟手，松手吸附到最近档位；纯点击仍走原切换）
function bindToggleDrags() {
  document.querySelectorAll('#fileList .duplex-toggle, #fileList .img-ori-toggle').forEach(toggle => {
    if (toggle._dragBound) return;
    toggle._dragBound = true;
    const slider = toggle.querySelector('.duplex-slider, .img-ori-slider');
    if (!slider) return;
    const opts = [...toggle.querySelectorAll('[data-value]')];
    if (opts.length < 2) return;
    let pointerId = null, startX = 0, startY = 0, dragging = false, moved = false, startPx = 0, segPx = 0;
    const syncValue = (idx) => {
      const val = opts[idx].dataset.value;
      const card = toggle.closest('.file-card');
      const cardIdx = [...document.querySelectorAll('#fileList .file-card')].indexOf(card);
      if (cardIdx < 0) return;
      if (toggle.classList.contains('duplex-toggle')) setDuplex(cardIdx, val);
      else setOrientation(cardIdx, val);
    };
    toggle.addEventListener('pointerdown', (e) => {
      if (e.button !== undefined && e.button !== 0) return;
      pointerId = e.pointerId;
      try { toggle.setPointerCapture(e.pointerId); } catch (err) { /* 兼容 */ }
      startX = e.clientX; startY = e.clientY;
      dragging = true; moved = false;
      const r = toggle.getBoundingClientRect();
      segPx = r.width / opts.length;
      const cur = opts.findIndex(o => o.classList.contains('opt-active'));
      startPx = Math.max(0, cur) * segPx;
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
      segPx = r.width / opts.length;
      let offsetPx = Math.max(0, Math.min(r.width - segPx, startPx + dx));
      // 平滑磁力：进入磁吸区（±40% 段宽）后向档位中心施加拉力，
      // 越接近拉力越大（线性衰减），不瞬移；松手时才完全吸附
      const nearest = Math.round(offsetPx / segPx) * segPx;
      const zone = segPx * 0.40;
      const dist = nearest - offsetPx;
      if (Math.abs(dist) < zone) {
        const t = Math.abs(dist) / zone; // 0=档位中心，1=磁吸区边缘
        offsetPx += dist * (1 - t);
      }
      slider.style.transition = 'none';
      slider.style.transform = 'translateX(' + offsetPx + 'px)';
    });
    const endDrag = (e) => {
      if (!dragging) { pointerId = null; return; }
      dragging = false; pointerId = null;
      gestureBus.horizontal = false;
      if (!moved) return; // 纯点击：保留原 data-action 点击切换
      const r = toggle.getBoundingClientRect();
      segPx = r.width / opts.length;
      const offsetPx = parseFloat((/translateX\((-?[\d.]+)px\)/.exec(slider.style.transform || '') || [])[1] || '0');
      const idx = Math.max(0, Math.min(opts.length - 1, Math.round(offsetPx / segPx)));
      slider.style.transition = 'transform 0.3s cubic-bezier(0.34,1.56,0.64,1)';
      slider.style.transform = 'translateX(' + (idx * segPx) + 'px)';
      syncValue(idx);
      toggle._dragHandled = Date.now();
      // 过渡结束后清除内联 transform，交给类接管（位置一致无跳动）
      setTimeout(() => { if (slider.style.transform) slider.style.transform = ''; }, 320);
    };
    toggle.addEventListener('pointerup', endDrag);
    toggle.addEventListener('pointercancel', endDrag);
  });
}

// iOS 开关：按住拖动（thumb 跟手，松手过半吸附；纯点击仍走原 toggle）
function bindSwitchDrags() {
  const defs = [
    { id: 'coverSwitch', getOn: () => printState.coverPage || state.role === 'user', toggle: toggleCoverPage },
    { id: 'deliverySwitch', getOn: () => printState.deliveryEnabled, toggle: toggleDelivery },
    { id: 'autoPrintSwitch', getOn: () => printState.autoPrintEnabled, toggle: toggleAutoPrint },
    { id: 'adminPrintSwitch', getOn: () => printState.adminPrintEnabled, toggle: toggleAdminPrint },
  ];
  defs.forEach(def => {
    const el = document.getElementById(def.id);
    if (!el) return;
    const thumb = el.querySelector('.switch-thumb');
    if (!thumb) return;
    let pointerId = null, startX = 0, startY = 0, dragging = false, moved = false, startPx = 0, maxPx = 0;
    el.addEventListener('pointerdown', (e) => {
      if (e.button !== undefined && e.button !== 0) return;
      pointerId = e.pointerId;
      try { el.setPointerCapture(e.pointerId); } catch (err) { /* 兼容 */ }
      startX = e.clientX; startY = e.clientY;
      dragging = true; moved = false;
      // 拇指行程对齐小程序 rpx 常量（lg 开关 47rpx / 普通 35rpx，CSS --sw-px 同源），
      // 避免实测宽度与 CSS 行程不一致导致拖满后 thumb 差几 rpx 不到位
      const frame = document.querySelector('.app-frame');
      const basis = (frame && frame.clientWidth) || 375;
      maxPx = Math.max(1, Math.round((def.id === 'autoPrintSwitch' || def.id === 'adminPrintSwitch' ? 47 : 35) * basis / 750));
      startPx = def.getOn() ? maxPx : 0;
      thumb.style.setProperty('--sw-px', startPx + 'px');
      el.classList.add('sw-dragging');
    });
    el.addEventListener('pointermove', (e) => {
      if (!dragging || e.pointerId !== pointerId) return;
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;
      if (!moved) {
        if (Math.abs(dx) < 6 && Math.abs(dy) < 6) return;
        if (Math.abs(dx) <= Math.abs(dy)) { // 纵向交给页面滚动
          dragging = false;
          el.classList.remove('sw-dragging');
          thumb.style.removeProperty('--sw-px');
          return;
        }
        moved = true;
        gestureBus.horizontal = true;
      }
      const px = Math.max(0, Math.min(maxPx, startPx + dx));
      thumb.style.setProperty('--sw-px', px + 'px');
    });
    const endDrag = (e) => {
      if (!dragging) { pointerId = null; return; }
      dragging = false; pointerId = null;
      gestureBus.horizontal = false;
      el.classList.remove('sw-dragging');
      if (!moved) { thumb.style.removeProperty('--sw-px'); return; } // 纯点击
      const px = parseFloat(thumb.style.getPropertyValue('--sw-px')) || startPx;
      const on = px > maxPx / 2;
      thumb.style.removeProperty('--sw-px');
      el._dragHandled = Date.now();
      if (on !== def.getOn()) def.toggle();
    };
    el.addEventListener('pointerup', endDrag);
    el.addEventListener('pointercancel', endDrag);
  });
}

// 文件列表显式高度（上限 85vh，内部滚动）——对齐小程序 scroll-view。
// 高度用 350ms/500ms easeOutCubic 补间，而非瞬间跳变；
// 动画中的卡片克隆测量"最终高度"，避免容器高度跟着展开/收起中间值跳动。
let _fileListTween = null;

function measureCardHeight(card) {
  if (!card.classList.contains('card-entering') && !card.classList.contains('card-removing')) {
    return card.offsetHeight;
  }
  const parent = card.parentElement;
  const clone = card.cloneNode(true);
  clone.classList.remove('card-entering', 'card-removing');
  clone.style.maxHeight = 'none';
  clone.style.animation = 'none';
  clone.style.position = 'absolute';
  clone.style.left = '-9999px';
  clone.style.top = '0';
  clone.style.visibility = 'hidden';
  clone.style.width = card.getBoundingClientRect().width + 'px';
  parent.appendChild(clone);
  const h = clone.offsetHeight;
  parent.removeChild(clone);
  return h;
}

function animateFileListHeight(scroll, target, duration) {
  if (_fileListTween) { clearTimeout(_fileListTween); _fileListTween = null; }
  const start = scroll.offsetHeight || 0;
  const diff = target - start;
  if (Math.abs(diff) < 1) {
    scroll.style.height = target + 'px';
    printState._fileListPx = target;
    measureAll(150);
    return;
  }
  const startTime = Date.now();
  const tick = () => {
    const t = Math.min(1, (Date.now() - startTime) / duration);
    const eased = 1 - Math.pow(1 - t, 3); // easeOutCubic：先快后慢（对齐小程序）
    scroll.style.height = Math.round(start + diff * eased) + 'px';
    if (t < 1) {
      _fileListTween = setTimeout(tick, 24);
    } else {
      _fileListTween = null;
      printState._fileListPx = target;
      measureAll(150);
    }
  };
  tick();
}

function recalcFileListHeight() {
  const scroll = document.getElementById('fileListScroll');
  if (!scroll) return;
  const list = document.getElementById('fileList');
  let h = 0;
  let removing = false;
  list.querySelectorAll('.file-card').forEach(c => {
    if (c.classList.contains('card-removing')) { removing = true; return; } // 移除中的卡片不计入
    h += measureCardHeight(c) + 8;
  });
  const cap = window.innerHeight * 0.85;
  const target = Math.max(0, Math.min(cap, h));
  animateFileListHeight(scroll, target, removing ? 500 : 350);
}

// 文件数徽章动画（进入/弹跳/退场）
let _badgeTimer = null;
let _badgeCountTimer = null;
let _btnPulseTimer = null;

function scheduleBadgeClear(ms) {
  if (_badgeTimer) clearTimeout(_badgeTimer);
  _badgeTimer = setTimeout(() => {
    const badge = document.getElementById('fileCount');
    if (badge) badge.classList.remove('entering', 'bouncing');
  }, ms);
}

// 对齐小程序徽章时序：计数与动画统一延迟 0.25s（与卡片入场同步）；
// 末文件删除先播退场动画（650ms，撑到卡片移除）再隐藏；
// 首文件入场 450ms / 弹跳 400ms 清除
function updateFileBadge(isAdd) {
  const badge = document.getElementById('fileCount');
  if (!badge) return;
  const count = printState.selectedFiles.filter(f => !f.removing).length;
  if (count === 0) {
    // 末文件删除/清空：先播退场动画（对齐 mini _triggerBadgeExit）
    badge.classList.remove('entering', 'bouncing');
    badge.classList.add('exiting');
    badge.textContent = 1;
    badge.style.display = '';
    triggerBtnPulse();
    if (_badgeTimer) clearTimeout(_badgeTimer);
    _badgeTimer = setTimeout(() => {
      badge.classList.remove('exiting');
      badge.style.display = 'none';
    }, 650);
    return;
  }
  if (_badgeCountTimer) clearTimeout(_badgeCountTimer);
  _badgeCountTimer = setTimeout(() => {
    badge.style.display = '';
    badge.textContent = count;
    badge.classList.remove('exiting');
    const entering = isAdd && count === 1; // 仅首次添加播放入场，其余（含删除后）弹跳
    badge.classList.add(entering ? 'entering' : 'bouncing');
    scheduleBadgeClear(entering ? 450 : 400);
  }, 250);
  if (count === 1 && isAdd) triggerBtnPulse();
}

function triggerBtnPulse() {
  const btn = document.getElementById('addFileBtn');
  if (!btn) return;
  const text = printState.selectedFiles.length ? '添加文件' : '选择打印文件';
  document.getElementById('addFileBtnText').textContent = text;
  clearTimeout(_btnPulseTimer);
  btn.classList.add('pulsing');
  _btnPulseTimer = setTimeout(() => btn.classList.remove('pulsing'), 450);
}

/* ================= 文件操作 ================= */

function removeFile(idx) {
  const f = printState.selectedFiles[idx];
  if (!f) return;
  stopUploadTimer(idx);
  stopPageCountPoll(idx);
  if (f.fileId && !(f.pageCount > 0)) {
    api('/api/cancel_page_analysis', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_ids: [f.fileId] }),
    }).catch(() => {});
  }
  f.removing = true;
  updateFileBadge(false); // 对齐小程序：删除时立即更新徽章（卡片退场动画并行）
  renderFileList();
  setTimeout(() => {
    printState.selectedFiles.splice(idx, 1);
    delete printState._uploadTimers[idx];
    delete printState._pollTimers[idx];
    renderFileList();
  }, 500);
}

function setCopies(idx, val) {
  const f = printState.selectedFiles[idx];
  if (!f || f.excelWarning || f.unsupportedFormat) return;
  const v = parseInt(val, 10);
  if (isNaN(v)) return;
  f.copies = Math.max(1, Math.min(99, v));
  // 原地更新份数（不整表重绘）：整表重绘会销毁被按下的按钮，导致短按缩放动画播放不完整
  const card = document.querySelectorAll('#fileList .file-card')[idx];
  if (card) {
    const input = card.querySelector('.stepper-input');
    if (input) input.value = f.copies;
    const minus = card.querySelector('[data-action="copies-minus"]');
    if (minus) minus.classList.toggle('stepper-disabled', f.copies <= 1);
  }
}

function setDuplex(idx, val) {
  const f = printState.selectedFiles[idx];
  if (!f || f.excelWarning || f.unsupportedFormat) return;
  if (f.pageCount === 1 || f.singlePage) return;
  if (f.duplex === val) return;
  f.duplex = val;
  // 原地更新滑块与选项（整表重绘会重建元素，0.3s 滑动过渡无法播放——对齐小程序 setData 保留元素）
  const card = document.querySelectorAll('#fileList .file-card')[idx];
  if (card) {
    const slider = card.querySelector('.duplex-slider');
    if (slider) slider.classList.toggle('slider-right', val === 'on');
    card.querySelectorAll('.duplex-opt[data-action="duplex"]').forEach(opt => {
      opt.classList.toggle('opt-active', opt.dataset.value === val);
    });
  }
}

function setOrientation(idx, val) {
  const f = printState.selectedFiles[idx];
  if (!f || !f.isImage) return;
  if (f.imageOrientation === val) return;
  f.imageOrientation = val;
  // 原地更新三档滑块与选项（对齐小程序 onFileImageOrientation，保留滑动过渡）
  const card = document.querySelectorAll('#fileList .file-card')[idx];
  if (card) {
    const slider = card.querySelector('.img-ori-slider');
    if (slider) {
      slider.classList.toggle('slider-landscape', val === 'landscape');
      slider.classList.toggle('slider-portrait', val === 'portrait');
    }
    card.querySelectorAll('.img-ori-opt[data-action="ori"]').forEach(opt => {
      opt.classList.toggle('opt-active', opt.dataset.value === val);
    });
  }
}

function retryUpload(idx) {
  const f = printState.selectedFiles[idx];
  if (!f || f.uploading) return;
  if (!f.file) {
    showToast('文件路径已失效，请重新选择');
    return;
  }
  f.uploading = true;
  f.progress = 0;
  f.fileId = null;
  f.failed = false;
  f.pageCount = 0;
  f.pageCountStatus = '';
  renderFileList();
  uploadFile(idx);
}

/* ================= 上传 ================= */

function uploadFile(idx) {
  const f = printState.selectedFiles[idx];
  if (!f || !f.file) return;
  if (!state.token) {
    ensureLogin().then(ok => { if (ok) uploadFile(idx); });
    return;
  }
  stopUploadTimer(idx);
  const entry = { realProgress: 0, xhr: null, timer: null };
  printState._uploadTimers[idx] = entry;
  entry.timer = setInterval(() => {
    const cur = printState._uploadTimers[idx];
    if (!cur || !cur.realProgress) return;
    const file = printState.selectedFiles[idx];
    if (!file) return;
    if (file.progress >= cur.realProgress) return;
    const next = file.progress + Math.max(1, (cur.realProgress - file.progress) * 0.5);
    file.progress = Math.round(Math.min(next, cur.realProgress));
    renderFileList();
  }, 500);
  const xhr = new XMLHttpRequest();
  entry.xhr = xhr;
  xhr.open('POST', BASE_URL + '/api/upload');
  xhr.setRequestHeader('Authorization', 'Bearer ' + state.token);
  const fd = new FormData();
  fd.append('file', f.file);
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) entry.realProgress = Math.round(e.loaded / e.total * 100);
  };
  xhr.onload = () => {
    // 先保存响应，再停计时器（stopUploadTimer 会 abort xhr，abort 后 status/responseText 被清空）
    const status = xhr.status;
    const responseText = xhr.responseText;
    stopUploadTimer(idx);
    if (status === 401) {
      f.uploading = false;
      renderFileList();
      ensureLogin().then(ok => {
        if (ok) {
          f.uploading = true;
          f.progress = 0;
          renderFileList();
          uploadFile(idx);
        } else showToast('登录失败，请检查网络连接');
      });
      return;
    }
    let data = null;
    let errMsg = '';
    try { data = JSON.parse(responseText); }
    catch (e) {
      if (status === 413) errMsg = '文件过大，请压缩后再试';
      else if (status >= 500) errMsg = '服务器错误，请稍后重试';
      else errMsg = '上传失败';
    }
    const fileId = data && (data.file_id || data.id);
    if (!fileId) {
      f.uploading = false;
      f.failed = true;
      renderFileList();
      showToast(errMsg || (data && data.message) || '上传失败', 2500);
      return;
    }
    let pageCount = data.page_count || 0;
    if (f.isImage) pageCount = 1;
    f.uploading = false;
    f.progress = 100;
    f.fileId = fileId;
    f.failed = false;
    f.pageCount = pageCount;
    f.pageCountStatus = (!f.isImage) ? (pageCount > 0 ? 'confirmed' : 'analyzing') : '';
    if (!f.isImage) refreshSinglePage(idx);
    if (!f.isImage && pageCount <= 0) startPageCountPoll(idx, fileId);
    renderFileList();
  };
  xhr.onerror = () => {
    stopUploadTimer(idx);
    f.uploading = false;
    f.failed = true;
    renderFileList();
    showToast('文件上传失败');
  };
  xhr.send(fd);
}

function stopUploadTimer(idx) {
  const entry = printState._uploadTimers[idx];
  if (!entry) return;
  if (entry.timer) { clearInterval(entry.timer); entry.timer = null; }
  if (entry.xhr) { try { entry.xhr.abort(); } catch (e) { /* ok */ } entry.xhr = null; }
}

/* ================= 页码分析轮询 ================= */

function startPageCountPoll(idx, fileId) {
  stopPageCountPoll(idx);
  if (!fileId) return;
  let attempts = 0;
  const poll = async () => {
    try {
      const r = await api('/api/file_page/' + fileId);
      const f = printState.selectedFiles[idx];
      if (!f || f.fileId !== fileId) { stopPageCountPoll(idx); return; }
      if (r.status === 200 && r.data && r.data.success) {
        const pc = r.data.page_count || 0;
        const verified = !!r.data.verified;
        if (pc > 0 && verified) {
          f.pageCount = pc;
          f.pageCountStatus = 'confirmed';
          if (pc === 1) {
            f.duplex = 'off';
            f.rangeLines = [{ value: '', error: '' }];
            f.pageRange = '';
          }
          // smooth：输入行/警告原地收起（过渡动画），摘要淡入，延时重绘
          normalizeAndValidateRangeLines(idx, true);
          stopPageCountPoll(idx);
          return;
        }
        f.pageCountStatus = r.data.printer_online ? 'analyzing' : 'offline';
        attempts++;
        if (attempts >= MAX_POLL_ATTEMPTS) stopPageCountPoll(idx);
        renderFileList();
      }
    } catch (e) { /* 网络错误继续轮询 */ }
  };
  poll();
  printState._pollTimers[idx] = setInterval(poll, 5000);
}

function stopPageCountPoll(idx) {
  if (printState._pollTimers[idx]) {
    clearInterval(printState._pollTimers[idx]);
    delete printState._pollTimers[idx];
  }
}

/* ================= 页码范围 ================= */

function parseSingleRange(text) {
  text = (text || '').trim();
  if (!text) return null;
  if (text.indexOf('-') !== -1) {
    const parts = text.split('-');
    if (parts.length !== 2) return null;
    const start = parseInt(parts[0], 10);
    const end = parseInt(parts[1], 10);
    if (isNaN(start) || isNaN(end)) return null;
    if (start >= 1 && start < end) {
      const pages = new Set();
      for (let p = start; p <= end; p++) pages.add(p);
      return pages;
    }
    return null;
  }
  const v = parseInt(text, 10);
  if (isNaN(v) || v < 1) return null;
  return new Set([v]);
}

function computeSinglePage(f) {
  if (!f) return false;
  if (f.isImage) return false;
  if (f.pageCount === 1) return true;
  const pages = new Set();
  for (const line of (f.rangeLines || [])) {
    const v = (line.value || '').trim();
    if (!v) continue;
    const parsed = parseSingleRange(v);
    if (parsed) parsed.forEach(p => pages.add(p));
  }
  return pages.size === 1;
}

function refreshSinglePage(idx) {
  const f = printState.selectedFiles[idx];
  if (!f) return;
  f.singlePage = computeSinglePage(f);
  renderFileList();
}

// 页数确认后：输入区/警告原地加收起类（CSS 过渡播放），摘要立即插入，延时整表重绘刷新文本
function ensureRangeSummary(card, f) {
  const wrap = card.querySelector('.range-lines-wrap');
  if (!wrap || card.querySelector('.range-summary-trigger')) return;
  const div = document.createElement('view');
  div.className = 'range-summary-trigger';
  div.dataset.action = 'open-range';
  div.innerHTML = `<text class="range-summary-text ${f.pageRange ? '' : 'range-summary-all'}">${escHtml(f.pageRange ? '已选 ' + f.pageRange : '全部页')}</text><text class="range-summary-arrow">›</text>`;
  wrap.appendChild(div);
}

function normalizeAndValidateRangeLines(idx, smooth) {
  const f = printState.selectedFiles[idx];
  if (!f || f.isImage || f.excelWarning || f.unsupportedFormat) return;
  const lines = f.rangeLines || [{ value: '', error: '' }];
  const maxPages = f.pageCount || 0;
  const entries = [];
  for (const line of lines) {
    const v = (line.value || '').trim();
    if (!v) continue;
    const pages = parseSingleRange(v);
    if (pages) entries.push({ value: v, pages, error: '' });
    else entries.push({ value: v, pages: null, error: '格式错误（应为 1-5 或 7）' });
  }
  if (maxPages > 0) {
    for (const e of entries) {
      if (e.pages && Math.max(...e.pages) > maxPages) { e.error = '超出总页数 ' + maxPages; e.pages = null; }
    }
  }
  for (let i = 0; i < entries.length; i++) {
    if (!entries[i].pages) continue;
    for (let j = i + 1; j < entries.length; j++) {
      if (!entries[j].pages) continue;
      if ([...entries[i].pages].some(p => entries[j].pages.has(p))) {
        entries[i].error = '重叠: ' + entries[j].value;
        entries[j].error = '重叠: ' + entries[i].value;
        entries[i].pages = null;
        entries[j].pages = null;
      }
    }
  }
  entries.sort((a, b) => {
    if (!a.pages && !b.pages) return 0;
    if (!a.pages) return 1;
    if (!b.pages) return -1;
    return Math.min(...a.pages) - Math.min(...b.pages);
  });
  const prevSingle = f.singlePage;
  f.rangeLines = entries.map(e => ({ value: e.value, error: e.error })).concat([{ value: '', error: '' }]);
  f.pageRange = entries.filter(e => e.pages).map(e => e.value).join(',');
  f.singlePage = computeSinglePage(f);
  const known = f.pageCountStatus === 'confirmed' && f.pageCount > 0;
  const singleChanged = prevSingle !== f.singlePage;
  const card = document.querySelectorAll('#fileList .file-card')[idx];
  const inputsEl = card ? card.querySelector('.range-inputs-state') : null;
  const needsCollapseAnim = known && inputsEl && !inputsEl.classList.contains('state-collapsed');
  if (smooth && card && (needsCollapseAnim || (singleChanged && !known))) {
    // 原地切换类名，CSS 过渡正常播放（整表重绘会重建元素导致突变）
    const modeRow = card.querySelector('.mode-row');
    if (modeRow) modeRow.classList.toggle('mode-row-collapsed', f.singlePage);
    if (needsCollapseAnim) {
      const warn = card.querySelector('.range-unknown-warn');
      if (warn) warn.classList.add('range-warn-collapsed');
      inputsEl.classList.add('state-collapsed');
      ensureRangeSummary(card, f);
    }
    // 等收起过渡结束后整表重绘刷新文本/行序/高度
    setTimeout(() => renderFileList(), 320);
  } else {
    renderFileList();
  }
}

/* ================= 页数网格选择 ================= */

const rangePicker = { fileIndex: -1, total: 0, pages: [], selAll: false, selOdd: false, selEven: false };

function isExactOddSet(selected, total) {
  for (let n = 1; n <= total; n++) if (selected.has(n) !== (n % 2 === 1)) return false;
  return true;
}
function isExactEvenSet(selected, total) {
  for (let n = 1; n <= total; n++) if (selected.has(n) !== (n % 2 === 0)) return false;
  return true;
}

function openRangePicker(idx) {
  const f = printState.selectedFiles[idx];
  if (!f || !(f.pageCount > 0)) return;
  const total = f.pageCount;
  const selected = new Set();
  for (const line of (f.rangeLines || [])) {
    const v = (line.value || '').trim();
    if (!v) continue;
    const parsed = parseSingleRange(v);
    if (parsed) parsed.forEach(p => selected.add(p));
  }
  rangePicker.fileIndex = idx;
  rangePicker.total = total;
  rangePicker.pages = [];
  for (let n = 1; n <= total; n++) rangePicker.pages.push({ n, sel: selected.has(n) });
  rangePicker.selAll = selected.size === total;
  rangePicker.selOdd = isExactOddSet(selected, total);
  rangePicker.selEven = isExactEvenSet(selected, total);
  renderRangePicker();
  openModal('rangePickerModal');
}

function renderRangePicker() {
  document.getElementById('rangePickerTitle').textContent = '共 ' + rangePicker.total + ' 页';
  document.getElementById('rangePickerGrid').innerHTML = rangePicker.pages.map(p =>
    `<view class="range-picker-cell ${p.sel ? 'selected' : ''}" data-page="${p.n}">${p.n}</view>`).join('');
  document.getElementById('rangePickerAll').classList.toggle('active', rangePicker.selAll);
  document.getElementById('rangePickerOdd').classList.toggle('active', rangePicker.selOdd);
  document.getElementById('rangePickerEven').classList.toggle('active', rangePicker.selEven);
}

function toggleRangeCell(n) {
  const p = rangePicker.pages.find(x => x.n === n);
  if (!p) return;
  p.sel = !p.sel;
  const selected = new Set(rangePicker.pages.filter(x => x.sel).map(x => x.n));
  rangePicker.selAll = selected.size === rangePicker.total;
  rangePicker.selOdd = isExactOddSet(selected, rangePicker.total);
  rangePicker.selEven = isExactEvenSet(selected, rangePicker.total);
  renderRangePicker();
}

function rangePickerSelectBy(selFn) {
  rangePicker.pages = rangePicker.pages.map(p => ({ n: p.n, sel: selFn(p.n) }));
  const selected = new Set(rangePicker.pages.filter(p => p.sel).map(p => p.n));
  rangePicker.selAll = selected.size === rangePicker.total;
  rangePicker.selOdd = isExactOddSet(selected, rangePicker.total);
  rangePicker.selEven = isExactEvenSet(selected, rangePicker.total);
  renderRangePicker();
}

function confirmRangePicker() {
  const idx = rangePicker.fileIndex;
  const f = printState.selectedFiles[idx];
  if (idx < 0 || !f) { closeModal('rangePickerModal'); return; }
  const selected = rangePicker.pages.filter(p => p.sel).map(p => p.n).sort((a, b) => a - b);
  f.rangeLines = selected.map(n => ({ value: String(n), error: '' })).concat([{ value: '', error: '' }]);
  f.pageRange = selected.join(',');
  f.singlePage = computeSinglePage(f);
  closeModal('rangePickerModal');
  // 原地更新卡片摘要（不整表重绘）：重绘会重建全部元素，触发 statusFadeIn 入场动画，
  // 导致"√已上传"/页数状态文字闪烁
  const card = document.querySelectorAll('#fileList .file-card')[idx];
  if (card) {
    const summary = card.querySelector('.range-summary-text');
    if (summary) {
      summary.textContent = f.pageRange ? '已选 ' + f.pageRange : '全部页';
      summary.classList.toggle('range-summary-all', !f.pageRange);
    }
    const modeRow = card.querySelector('.mode-row');
    if (modeRow) modeRow.classList.toggle('mode-row-collapsed', !!f.singlePage);
  }
  measureAll(150);
  // 卡片高度可能变化（模式行收起），过渡结束后再次测量（对齐小程序 _recalcFileListHeight）
  setTimeout(() => measureAll(320), 340);
}

/* ================= 定价 / 附加服务 ================= */

async function loadPricing() {
  try {
    const r = await api('/api/pricing');
    if (r.status === 200 && r.data && r.data.success) {
      const p = r.data.pricing;
      if (p.delivery_locations) printState.deliveryLocations = p.delivery_locations;
      if (p.delivery_percentages) printState.deliveryPercentages = p.delivery_percentages;
      if (p.urgency_levels) printState.urgencyOptions = p.urgency_levels;
      if (p.urgency_prices) printState.urgencyPrices = p.urgency_prices;
      if (p.cover_page_price != null) printState.coverPagePrice = p.cover_page_price;
      printState.pricingLoaded = true;
      const pct = printState.deliveryPercentages[printState.deliveryLocation];
      if (pct != null) printState.deliveryPercent = pct;
      renderExtParams();
    }
  } catch (e) { /* 默认值 */ }
}

function renderExtParams() {
  document.getElementById('urgencyPicker').innerHTML = printState.urgencyOptions.map(u =>
    `<view class="picker-option ${printState.urgency === u ? 'picker-selected' : ''}" data-urg="${escHtml(u)}">
       <text>${escHtml(u)}</text><text class="picker-pct">¥${Number(printState.urgencyPrices[u] || 0).toFixed(2)}</text>
     </view>`).join('');
  document.getElementById('urgencyValue').textContent = printState.urgency;
  document.getElementById('deliveryPicker').innerHTML = printState.deliveryLocations.map(loc =>
    `<view class="picker-option ${printState.deliveryLocation === loc ? 'picker-selected' : ''}" data-loc="${escHtml(loc)}">
       <text>${escHtml(loc)}</text><text class="picker-pct">${printState.deliveryPercentages[loc] || 0}%</text>
     </view>`).join('');
  document.getElementById('deliveryLocationValue').textContent = printState.deliveryLocation;
  document.getElementById('deliverySwitch').classList.toggle('switch-on', printState.deliveryEnabled);
  // 派送地点列表的展开由 .delivery-collapse.delivery-open 控制（max-height 过渡），
  // 只改 style.display 会被 max-height:0 继续折叠 —— 必须同步切换类名。
  const deliveryOptions = document.getElementById('deliveryOptions');
  if (deliveryOptions) {
    deliveryOptions.classList.toggle('delivery-open', printState.deliveryEnabled);
    deliveryOptions.style.display = '';
  }
  updateCoverPrice();
  measureAll(150);
}

let _coverPriceTimer = null;
function updateCoverPrice(animate) {
  const tag = document.getElementById('coverPriceTag');
  const sw = document.getElementById('coverSwitch');
  const on = printState.coverPage || state.role === 'user';
  sw.classList.toggle('switch-on', on);
  tag.textContent = '¥' + Number(printState.coverPagePrice).toFixed(2);
  clearTimeout(_coverPriceTimer);
  if (animate === 'on') {
    // 打开：价格标签淡入（对齐小程序 coverPriceIn 0.3s spring）
    tag.style.display = '';
    tag.classList.remove('exiting');
    tag.classList.add('entering');
    _coverPriceTimer = setTimeout(() => tag.classList.remove('entering'), 350);
  } else if (animate === 'off') {
    // 关闭：价格标签淡出（对齐小程序 coverPriceOut 0.25s），动画结束后隐藏
    tag.classList.remove('entering');
    tag.classList.add('exiting');
    _coverPriceTimer = setTimeout(() => {
      tag.classList.remove('exiting');
      tag.style.display = 'none';
    }, 300);
  } else {
    // 初始/参数渲染：无动画直接定位
    tag.classList.remove('entering', 'exiting');
    tag.style.display = on ? '' : 'none';
  }
}

function toggleCoverPage() {
  if (state.role === 'user') { showToast('普通用户必须打印首页'); return; }
  printState.coverPage = !printState.coverPage;
  updateCoverPrice(printState.coverPage ? 'on' : 'off');
}

function toggleUrgencyPicker() {
  document.getElementById('urgencyPicker').classList.toggle('picker-expanded');
  // 展开/收起有 350ms 过渡，动画完成后重新测量滚动内容高度（对齐小程序 _scheduleMeasure）
  measureAll(150);
  setTimeout(() => measureAll(400), 400);
}

function selectUrgency(urg) {
  printState.urgency = urg;
  printState.urgencyPrice = printState.urgencyPrices[urg] || 0;
  renderExtParams();
  document.getElementById('urgencyPicker').classList.remove('picker-expanded');
  measureAll(150);
  setTimeout(() => measureAll(400), 400);
}

function toggleDelivery() {
  printState.deliveryEnabled = !printState.deliveryEnabled;
  printState.deliveryPercent = printState.deliveryEnabled
    ? (printState.deliveryPercentages[printState.deliveryLocation] || 0) : 0;
  renderExtParams();
}

function toggleDeliveryPicker() {
  document.getElementById('deliveryPicker').classList.toggle('picker-expanded');
  // 展开/收起有 350ms 过渡，动画完成后重新测量滚动内容高度（对齐小程序 _scheduleMeasure）
  measureAll(150);
  setTimeout(() => measureAll(400), 400);
}

function selectDeliveryLocation(loc) {
  printState.deliveryLocation = loc;
  printState.deliveryPercent = printState.deliveryPercentages[loc] || 0;
  renderExtParams();
  document.getElementById('deliveryPicker').classList.remove('picker-expanded');
  measureAll(150);
  setTimeout(() => measureAll(400), 400);
}

/* ================= 无障碍打印 / 预约 ================= */

function toggleAutoPrint() {
  printState.autoPrintEnabled = !printState.autoPrintEnabled;
  updateScheduleUI();
  const burst = document.getElementById('lightningBurst');
  if (!burst) return;
  if (printState.autoPrintEnabled) {
    // 爆发瞬间：闪白 + 抖动 + 环形冲击波（striking 类挂容器上）
    burst.classList.remove('reset', 'active');
    burst.classList.add('striking');
    setTimeout(() => drawLightningBolts(), 30);
    setTimeout(() => {
      burst.classList.remove('striking');
      fadeOutGlow();
    }, 350);
  } else {
    // 关闭：清除全部光效
    burst.classList.remove('striking', 'active', 'reset');
    stopBreathingGlow();
    const icon = document.getElementById('autoPrintIcon');
    if (icon) icon.style.textShadow = '';
    clearBoltCanvas();
  }
}

// 管理员自行打印：自己的订单不计入收益，归属提交者（后端仅管理员角色生效）
function toggleAdminPrint() {
  printState.adminPrintEnabled = !printState.adminPrintEnabled;
  const sw = document.getElementById('adminPrintSwitch');
  if (sw) sw.classList.toggle('switch-on', printState.adminPrintEnabled);
}

let _scheduleOptionsTimer = null;
let _scheduleLeaveTimer = null;

// 模式切换：两阶段过渡（收起旧选项 260ms → 换内容 → 展开新选项），对齐小程序
function setScheduleMode(mode) {
  if (mode === printState.scheduleMode) return;
  const at = document.getElementById('scheduleAtOptions');
  const cd = document.getElementById('scheduleCountdownOptions');
  const hadOptions = printState.scheduleMode !== 'now';
  const willHaveOptions = mode !== 'now';
  if (_scheduleOptionsTimer) clearTimeout(_scheduleOptionsTimer);

  const expand = (el) => {
    el.classList.add('collapsed');
    el.style.display = '';
    void el.offsetWidth;
    el.classList.remove('collapsed');
  };
  const finish = () => {
    printState.scheduleMode = mode;
    updateScheduleValues();
    document.querySelectorAll('[data-schedule-mode]').forEach(el => {
      el.classList.toggle('active', el.dataset.scheduleMode === mode);
    });
    measureAll(150);
  };

  if (hadOptions && willHaveOptions) {
    const current = printState.scheduleMode === 'at' ? at : cd;
    current.classList.add('collapsed');
    _scheduleOptionsTimer = setTimeout(() => {
      current.style.display = 'none';
      finish();
      expand(mode === 'at' ? at : cd);
    }, 260);
  } else if (hadOptions && !willHaveOptions) {
    const current = printState.scheduleMode === 'at' ? at : cd;
    current.classList.add('collapsed');
    _scheduleOptionsTimer = setTimeout(() => {
      current.style.display = 'none';
      finish();
    }, 260);
  } else {
    finish();
    expand(mode === 'at' ? at : cd);
  }
}

function buildScheduleDays() {
  const weekNames = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
  const labels = ['今天', '明天', '后天'];
  return labels.map((label, i) => {
    const d = new Date();
    d.setDate(d.getDate() + i);
    return label + '(' + weekNames[d.getDay()] + ')';
  });
}

function updateScheduleValues() {
  const days = buildScheduleDays();
  document.getElementById('scheduleDayValue').textContent = days[printState.scheduleDayIndex];
  document.getElementById('scheduleTimeValue').textContent = printState.scheduleTime || '选择时间';
  document.getElementById('scheduleCountdownMinValue').textContent = String(printState.countdownMin).padStart(2, '0');
  document.getElementById('scheduleCountdownSecValue').textContent = String(printState.countdownSec).padStart(2, '0');
  document.getElementById('dayPickerOptions').innerHTML = days.map((d, i) =>
    `<view class="sheet-option ${i === printState.scheduleDayIndex ? 'sheet-option-selected' : ''}" data-day="${i}">
       <text>${escHtml(d)}</text>${i === printState.scheduleDayIndex ? '<text class="sheet-option-check">✓</text>' : ''}
     </view>`).join('');
}

// 面板展开/收起动画：max-height + opacity + translateY 过渡（对齐小程序 collapsed/expanded）
function updateScheduleUI() {
  const sw = document.getElementById('autoPrintSwitch');
  sw.classList.toggle('switch-on', printState.autoPrintEnabled);
  const panel = document.getElementById('schedulePanel');
  if (printState.autoPrintEnabled) {
    if (_scheduleLeaveTimer) { clearTimeout(_scheduleLeaveTimer); _scheduleLeaveTimer = null; }
    if (!printState._scheduleExpanded) {
      printState._scheduleExpanded = true;
      // 先以 collapsed 渲染，再切 expanded 触发 CSS 过渡
      panel.classList.add('collapsed');
      panel.style.display = '';
      void panel.offsetWidth;
      panel.classList.remove('collapsed');
      // 面板展开后选项行直接就位（当前模式）
      const at = document.getElementById('scheduleAtOptions');
      const cd = document.getElementById('scheduleCountdownOptions');
      at.classList.remove('collapsed');
      cd.classList.remove('collapsed');
      if (printState.scheduleMode === 'at') { at.style.display = ''; cd.style.display = 'none'; }
      else if (printState.scheduleMode === 'countdown') { cd.style.display = ''; at.style.display = 'none'; }
      else { at.style.display = 'none'; cd.style.display = 'none'; }
    }
  } else if (printState._scheduleExpanded) {
    printState._scheduleExpanded = false;
    panel.classList.add('collapsed');
    if (_scheduleLeaveTimer) clearTimeout(_scheduleLeaveTimer);
    _scheduleLeaveTimer = setTimeout(() => { panel.style.display = 'none'; }, 320);
  }
  document.querySelectorAll('[data-schedule-mode]').forEach(el => {
    el.classList.toggle('active', el.dataset.scheduleMode === printState.scheduleMode);
  });
  updateScheduleValues();
  measureAll(150);
}

function openScheduleDayPicker() { openModal('scheduleDayModal'); }

function openScheduleTimePicker() {
  const now = new Date();
  if (printState.scheduleDayIndex === 0 && now.getHours() === 23 && now.getMinutes() >= 59) {
    showToast('今天已无可用时间，请选择明天');
    return;
  }
  openModal('scheduleTimeModal');
  buildTimeWheels();
}

function buildTimeWheels() {
  const now = new Date();
  const curHour = now.getHours();
  const curMin = now.getMinutes();
  const isToday = printState.scheduleDayIndex === 0;
  let hourStart = 0, minuteStart = 0;
  if (isToday) {
    minuteStart = curMin + 1;
    if (minuteStart > 59) { minuteStart = 0; hourStart = curHour + 1; }
    else hourStart = curHour;
  }
  const hours = [];
  for (let h = hourStart; h <= 23; h++) hours.push(String(h).padStart(2, '0'));
  const buildMinuteList = (mStart) => {
    const list = [];
    for (let m = mStart; m <= 59; m++) list.push(String(m).padStart(2, '0'));
    return list;
  };
  // 解析已有选择：按选中小时构建对应的分钟列表，得到 (hi, mi) 与分钟值
  const t = (printState.scheduleTime || '').match(/^(\d{1,2}):(\d{2})$/);
  let hi = 0, mi = 0, minuteValue = -1;
  if (t) {
    const selH = parseInt(t[1], 10);
    const selM = parseInt(t[2], 10);
    const hiCand = hours.indexOf(String(selH).padStart(2, '0'));
    if (hiCand >= 0) {
      hi = hiCand;
      minuteValue = selM;
      const mStart2 = (isToday && selH === curHour) ? minuteStart : 0;
      const list2 = buildMinuteList(mStart2);
      mi = list2.indexOf(String(selM).padStart(2, '0'));
      if (mi < 0) {
        // 保存的分钟在新列表不可用（如已过时间）→ 回退到该小时首个可用分钟
        mi = 0;
        minuteValue = parseInt(list2[0], 10);
      }
    }
  }
  printState._wheelValues.hour = hi;
  printState._wheelValues.minute = mi;
  printState._wheelValues.minuteValue = minuteValue;
  buildWheel('hourWheel', hours, hi, idx => {
    printState._wheelValues.hour = idx;
    refreshMinuteWheel(idx, hourStart, curHour, minuteStart);
  });
  refreshMinuteWheel(hi, hourStart, curHour, minuteStart);
}

function refreshMinuteWheel(hourIdx, hourStart, curHour, minuteStart) {
  const isToday = printState.scheduleDayIndex === 0;
  const selectedHour = hourStart + hourIdx;
  // 对齐小程序 _buildMinuteItems：今天且选中当前小时 → curMin+1..59；未来小时/明天 → 00-59
  const mStart = (isToday && selectedHour === curHour) ? minuteStart : 0;
  const minutes = [];
  for (let m = mStart; m <= 59; m++) minutes.push(String(m).padStart(2, '0'));
  // 换小时时保留分钟值：新列表仍包含该分钟则保持，否则回退到该小时首个可用分钟
  let mi = 0;
  const want = printState._wheelValues.minuteValue;
  if (want >= 0) {
    const idx = minutes.indexOf(String(want).padStart(2, '0'));
    if (idx >= 0) mi = idx;
  }
  printState._wheelValues.minute = mi;
  printState._wheelValues.minuteValue = parseInt(minutes[mi], 10);
  buildWheel('minuteWheel', minutes, mi, idx => {
    printState._wheelValues.minute = idx;
    printState._wheelValues.minuteValue = parseInt(minutes[idx], 10);
  });
}

// 通用滚轮：scroll-snap 贴合
// 索引判定用"item 中心到视口中心的距离最近"（与 scroll-snap-align:center 同一规则），
// 不依赖间距测量，避免 offsetHeight/offsetTop 舍入在长列表累积误差导致高亮错位
function buildWheel(wheelId, items, selectedIndex, onChange) {
  const col = document.getElementById(wheelId);
  col.innerHTML = `
    <view class="wheel-indicator"></view>
    <view class="wheel-viewport">
      <view class="wheel-items">
        ${items.map((it, i) => `<view class="wheel-item ${i === selectedIndex ? 'wheel-item-active' : ''}" data-i="${i}">${escHtml(it)}</view>`).join('')}
      </view>
    </view>`;
  const vp = col.querySelector('.wheel-viewport');
  const first = vp.querySelector('.wheel-item');
  if (!first) return;
  const nearestIndex = () => {
    const list = vp.querySelectorAll('.wheel-item');
    if (!list.length) return 0;
    const vpCenter = vp.scrollTop + vp.clientHeight / 2;
    let best = 0, bestD = Infinity;
    for (let i = 0; i < list.length; i++) {
      const c = list[i].offsetTop + list[i].offsetHeight / 2;
      const d = Math.abs(c - vpCenter);
      if (d < bestD) { bestD = d; best = i; }
    }
    return best;
  };
  const centerItem = (i) => {
    const list = vp.querySelectorAll('.wheel-item');
    const el = list[i] || list[0];
    if (!el) return false;
    const h = el.offsetHeight;
    if (!h) return false; // 布局未就绪（如弹窗未显示），下一帧重试
    vp.scrollTop = Math.max(0, el.offsetTop + h / 2 - vp.clientHeight / 2);
    return true;
  };
  const scrollTo = (i) => {
    if (!centerItem(i)) requestAnimationFrame(() => scrollTo(i));
  };
  scrollTo(selectedIndex);
  let deb = null;
  const update = () => {
    const idx = Math.max(0, Math.min(items.length - 1, nearestIndex()));
    vp.querySelectorAll('.wheel-item').forEach((el, i) => el.classList.toggle('wheel-item-active', i === idx));
    if (deb) clearTimeout(deb);
    deb = setTimeout(() => { if (typeof onChange === 'function') onChange(idx); }, 120);
  };
  vp.addEventListener('scroll', update, { passive: true });
}

function openScheduleCountdownPicker() {
  const cds = Array.from({ length: 60 }, (_, i) => String(i).padStart(2, '0'));
  openModal('scheduleCountdownModal');
  buildWheel('countdownMinuteWheel', cds, printState.countdownMin, idx => { printState._wheelValues.cdMin = idx; });
  buildWheel('countdownSecondWheel', cds, printState.countdownSec, idx => { printState._wheelValues.cdSec = idx; });
}

function confirmScheduleTime() {
  const hourIdx = readWheelIndex('hourWheel');
  const minuteIdx = readWheelIndex('minuteWheel');
  const hh = (document.querySelectorAll('#hourWheel .wheel-item')[hourIdx] || {}).textContent || '';
  const mm = (document.querySelectorAll('#minuteWheel .wheel-item')[minuteIdx] || {}).textContent || '';
  if (!hh || !mm) { showToast('请选择有效时间'); return; }
  printState.scheduleTime = hh + ':' + mm;
  closeModal('scheduleTimeModal');
  updateScheduleUI();
}

function confirmScheduleCountdown() {
  printState.countdownMin = readWheelIndex('countdownMinuteWheel');
  printState.countdownSec = readWheelIndex('countdownSecondWheel');
  if (printState.countdownMin === 0 && printState.countdownSec === 0) { showToast('请选择有效倒计时'); return; }
  closeModal('scheduleCountdownModal');
  updateScheduleUI();
}

// 读取滚轮当前选中索引：item 中心离视口中心最近的项（与 scroll-snap-align:center 同一规则）
function readWheelIndex(wheelId) {
  const col = document.getElementById(wheelId);
  const vp = col.querySelector('.wheel-viewport');
  const list = col.querySelectorAll('.wheel-item');
  if (!list.length) return 0;
  const vpCenter = vp.scrollTop + vp.clientHeight / 2;
  let best = 0, bestD = Infinity;
  for (let i = 0; i < list.length; i++) {
    const c = list[i].offsetTop + list[i].offsetHeight / 2;
    const d = Math.abs(c - vpCenter);
    if (d < bestD) { bestD = d; best = i; }
  }
  return best;
}

function validateSchedule() {
  if (!printState.autoPrintEnabled) return '';
  if (printState.scheduleMode === 'at') {
    const time = (printState.scheduleTime || '').trim();
    if (!time) return '请选择预约时间';
    const m = /^(\d{1,2}):(\d{2})$/.exec(time);
    if (!m) return '预约时间格式不正确';
    const hh = parseInt(m[1], 10), mm = parseInt(m[2], 10);
    if (hh > 23 || mm > 59) return '预约时间格式不正确';
    return '';
  }
  if (printState.scheduleMode === 'countdown') {
    if (printState.countdownMin > 59 || printState.countdownSec > 59) return '倒计时时长无效';
    if (printState.countdownMin === 0 && printState.countdownSec === 0) return '倒计时时长必须大于 0';
    return '';
  }
  return '';
}

function scheduleDisplayText() {
  if (!printState.autoPrintEnabled) return '';
  if (printState.scheduleMode === 'now') return '立即开始打印';
  if (printState.scheduleMode === 'at') {
    return buildScheduleDays()[printState.scheduleDayIndex] + ' ' + printState.scheduleTime + ' 开始打印';
  }
  return printState.countdownMin + ' 分 ' + printState.countdownSec + ' 秒后开始打印';
}

/* ================= 闪电 canvas + 光晕（移植小程序 index.js） ================= */

let _glowTimer = null;
let _breathTimer = null;

// 爆发后 JS 逐帧渐隐 text-shadow（30rpx/12rpx/6rpx → cqw）
function fadeOutGlow() {
  stopBreathingGlow();
  const icon = document.getElementById('autoPrintIcon');
  if (!icon) return;
  let step = 0;
  const MAX = 8;
  const tick = () => {
    if (step >= MAX) {
      icon.style.textShadow = '';
      if (printState.autoPrintEnabled) startBreathingGlow();
      return;
    }
    const t = step / MAX;
    const a = (1 - t) * (1 - t); // ease-out
    icon.style.textShadow =
      '0 0 ' + (4 * a).toFixed(3) + 'cqw rgba(255,255,255,' + a.toFixed(3) + '), ' +
      '0 0 ' + (1.6 * a).toFixed(3) + 'cqw rgba(255,229,0,' + a.toFixed(3) + '), ' +
      '0 0 ' + (0.8 * a).toFixed(3) + 'cqw rgba(255,149,0,' + a.toFixed(3) + ')';
    step++;
    _glowTimer = setTimeout(tick, 60);
  };
  tick();
}

// 呼吸光晕：ON 状态常驻（~3s 周期正弦波）
function startBreathingGlow() {
  stopBreathingGlow();
  const icon = document.getElementById('autoPrintIcon');
  if (!icon) return;
  let frame = 0;
  _breathTimer = setInterval(() => {
    frame++;
    const t = (frame % 30) / 30;
    const pulse = 0.5 + 0.5 * Math.sin(t * Math.PI * 2);
    const alpha = 0.12 + pulse * 0.58;
    const blur = (3 + pulse * 10) * 0.1333; // rpx→cqw
    icon.style.textShadow =
      '0 0 ' + (blur * 0.5).toFixed(3) + 'cqw rgba(255,255,255,' + (alpha * 0.35).toFixed(3) + '), ' +
      '0 0 ' + blur.toFixed(3) + 'cqw rgba(255,225,0,' + alpha.toFixed(3) + '), ' +
      '0 0 ' + (blur * 2).toFixed(3) + 'cqw rgba(255,180,0,' + (alpha * 0.5).toFixed(3) + ')';
  }, 100);
}

function stopBreathingGlow() {
  if (_breathTimer) { clearInterval(_breathTimer); _breathTimer = null; }
}

function clearBoltCanvas() {
  const canvas = document.getElementById('boltCanvas');
  if (canvas && canvas.getContext) {
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
  }
}

// 递归闪电折线（中点位移 + 多级分支）——移植小程序 _paintFork
function paintFork(ctx, x1, y1, x2, y2, displace, depth, thickness) {
  if (displace < 1.5 || depth > 7) {
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    const t = thickness * (1 - depth * 0.07);
    const alpha = Math.max(0.25, t);
    const colors = [
      'rgba(255,255,255,' + alpha.toFixed(3) + ')',
      'rgba(255,235,50,' + (alpha * 0.92).toFixed(3) + ')',
      'rgba(255,200,0,' + (alpha * 0.70).toFixed(3) + ')',
      'rgba(255,150,50,' + (alpha * 0.48).toFixed(3) + ')',
    ];
    ctx.strokeStyle = colors[Math.min(depth, colors.length - 1)];
    ctx.lineCap = 'round';
    ctx.lineWidth = Math.max(0.4, thickness * (2.0 - depth * 0.18));
    ctx.stroke();
    return;
  }
  const jitter = displace * 1.2;
  const midX = (x1 + x2) / 2 + (Math.random() - 0.5) * jitter;
  const midY = (y1 + y2) / 2 + (Math.random() - 0.5) * jitter;
  paintFork(ctx, x1, y1, midX, midY, displace * 0.55, depth + 1, thickness);
  paintFork(ctx, midX, midY, x2, y2, displace * 0.55, depth + 1, thickness);
  if (Math.random() < 0.35 && depth < 4 && depth > 0) {
    const bx = midX + (Math.random() - 0.5) * displace * 2.0;
    const by = midY + (Math.random() - 0.5) * displace * 2.0;
    paintFork(ctx, midX, midY, bx, by, displace * 0.3, depth + 2, thickness * 0.55);
  }
  if (Math.random() < 0.15 && depth < 3 && depth > 1) {
    const tx = x2 + (Math.random() - 0.5) * displace * 1.5;
    const ty = y2 + (Math.random() - 0.5) * displace * 1.5;
    paintFork(ctx, x2, y2, tx, ty, displace * 0.2, depth + 3, thickness * 0.4);
  }
}

// Canvas 绘制闪电（随机方向 + 多级分支 + 光晕层 + 6 帧淡出）
function drawLightningBolts() {
  const canvas = document.getElementById('boltCanvas');
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 2;
  const cssW = canvas.offsetWidth || 100;
  const cssH = canvas.offsetHeight || 100;
  canvas.width = cssW * dpr * 2;
  canvas.height = cssH * dpr * 2;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr * 2, dpr * 2);
  ctx.imageSmoothingEnabled = true;

  const w = cssW, h = cssH, cx = w / 2, cy = h / 2;
  const draw = (alpha) => {
    ctx.clearRect(0, 0, w, h);
    if (alpha > 0.04) {
      const grad = ctx.createRadialGradient(cx, cy, 1, cx, cy, w * 0.48);
      grad.addColorStop(0, 'rgba(255,235,50,' + (alpha * 0.40).toFixed(3) + ')');
      grad.addColorStop(0.4, 'rgba(255,210,0,' + (alpha * 0.18).toFixed(3) + ')');
      grad.addColorStop(1, 'rgba(255,180,0,0)');
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, w, h);
    }
    ctx.globalAlpha = alpha;
    const mainCount = 2 + Math.round(Math.random());
    for (let i = 0; i < mainCount; i++) {
      const angle = Math.random() * Math.PI * 2;
      const len = 13 + Math.random() * 10;
      paintFork(ctx, cx, cy, cx + Math.cos(angle) * len, cy + Math.sin(angle) * len, 10, 0, 1.0);
    }
    const subCount = 1 + Math.round(Math.random());
    for (let i = 0; i < subCount; i++) {
      const angle = Math.random() * Math.PI * 2;
      const len = 8 + Math.random() * 7;
      paintFork(ctx, cx, cy, cx + Math.cos(angle) * len, cy + Math.sin(angle) * len, 8, 0, 0.7);
    }
    ctx.globalAlpha = 1;
  };

  draw(1);
  let step = 0;
  const steps = 6;
  const timer = setInterval(() => {
    step++;
    if (step > steps) { clearInterval(timer); return; }
    draw(1 - step / steps);
  }, 80);
}

/* ================= 提交 ================= */

function onSubmit() {
  const files = printState.selectedFiles;
  if (state.role !== 'user' && state.role !== 'admin') { openModal('denyModal'); return; }
  if (!files.length) { showToast('请先选择打印文件'); return; }
  if (files.some(f => f.uploading)) { showToast('文件上传中，请稍候'); return; }
  const printable = files.filter(f => !f.excelWarning && !f.unsupportedFormat);
  const unsupportedCount = files.length - printable.length;
  if (printable.length === 0) {
    // 对齐小程序：全部不支持 → 弹窗"任务发起失败"
    openModal('failModal');
    return;
  }
  if (unsupportedCount > 0) {
    document.getElementById('unsupportedCount').textContent = unsupportedCount;
    openModal('unsupportedModal');
    return;
  }
  if (files.some(f => f.failed || !f.fileId)) { showToast('有文件未上传成功，请重新选择'); return; }
  for (const f of files) {
    if (!f.copies || f.copies < 1) { showToast('"' + f.name + '" 份数无效'); return; }
  }
  const unverified = files.filter(f => {
    if (f.isImage || f.excelWarning || f.unsupportedFormat) return false;
    if (!f.pageRange || !f.pageRange.trim()) return false;
    return (f.pageCount || 0) <= 0;
  });
  if (unverified.length > 0) { openModal('pageCountModal'); return; }
  for (const f of files) {
    if (f.pageCount === 1) continue;
    if ((f.rangeLines || []).some(line => line.error)) { showToast('"' + f.name + '" 页码范围有误'); return; }
  }
  const scheduleErr = validateSchedule();
  if (scheduleErr) { showToast(scheduleErr, 2500); return; }
  doSubmit(false);
}

function doSubmit(skipPageValidation) {
  if (printState.submitting) return;
  const printable = printState.selectedFiles.filter(f => !f.excelWarning && !f.unsupportedFormat);
  printState.submitting = true;
  const btn = document.getElementById('submitBtn');
  btn.disabled = true;
  btn.textContent = '提交中…';

  const filesPayload = printable.map(f => {
    const lines = (f.rangeLines || []).filter(l => (l.value || '').trim() && !l.error);
    const range = lines.map(l => l.value.trim()).join(',');
    return {
      file_id: f.fileId,
      file: f.name,
      copies: Number(f.copies),
      page_range: (f.pageCount === 1) ? '' : (range || f.pageRange || ''),
      duplex: (f.isImage || f.pageCount === 1 || f.singlePage) ? 'off' : (f.duplex || 'on'),
      image_orientation: f.isImage ? (f.imageOrientation || 'auto') : 'auto',
    };
  });

  api('/api/submit_order', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      client_request_id: Date.now().toString(36) + Math.random().toString(36).slice(2, 10),
      // 发起端标记：APP（后端写入 orders.source，统计页区分下单渠道）
      client: 'app',
      duplex: 'on',
      files: filesPayload,
      delivery_enabled: printState.deliveryEnabled ? 1 : 0,
      delivery_location: printState.deliveryLocation,
      delivery_percentage: printState.deliveryPercent,
      urgency: printState.urgency,
      urgency_price: printState.urgencyPrice,
      cover_page: (state.role === 'user' || printState.coverPage) ? 1 : 0,
      cover_page_price: printState.coverPagePrice,
      skip_page_validation: skipPageValidation ? 1 : 0,
      auto_print: printState.autoPrintEnabled ? 1 : 0,
      // v24.1：管理员自行打印（后端仅管理员角色生效；自己的订单不计入收益）
      is_admin_print: (state.role === 'admin' && printState.adminPrintEnabled) ? 1 : 0,
      schedule_mode: printState.autoPrintEnabled ? printState.scheduleMode : 'now',
      schedule_day: printState.autoPrintEnabled && printState.scheduleMode === 'at' ? printState.scheduleDayIndex : 0,
      schedule_time: printState.autoPrintEnabled && printState.scheduleMode === 'at' ? printState.scheduleTime : '',
      countdown_seconds: printState.autoPrintEnabled && printState.scheduleMode === 'countdown'
        ? (printState.countdownMin * 60 + printState.countdownSec) : 0,
    }),
  }).then(r => {
    printState.submitting = false;
    btn.disabled = false;
    btn.textContent = '提交打印任务';
    if (r.status === 200 && r.data && r.data.success) {
      printState._lastOrderResult = r.data;
      document.getElementById('successOrderNumber').textContent = r.data.order_number || '';
      const st = scheduleDisplayText();
      const stEl = document.getElementById('successScheduleText');
      const defEl = document.getElementById('successDefaultText');
      const apText = (state.role === 'admin' && printState.adminPrintEnabled) ? '已标记管理员自行打印，不计入收益' : '';
      const apEl = document.getElementById('successAdminPrintText');
      if (stEl) { stEl.textContent = st; stEl.style.display = st ? '' : 'none'; }
      if (apEl) { apEl.textContent = apText; apEl.style.display = apText ? '' : 'none'; }
      if (defEl) defEl.style.display = (st || apText) ? 'none' : '';
      openModal('successModal');
      // 对齐小程序：提交成功后先收起文件列表（保留数据），关闭弹窗时再清空
      const scroll = document.getElementById('fileListScroll');
      if (scroll) animateFileListHeight(scroll, 0, 300);
    } else {
      showToast((r.data && r.data.message) || '服务器错误，请稍后重试', 2500);
    }
  }).catch(() => {
    printState.submitting = false;
    btn.disabled = false;
    btn.textContent = '提交打印任务';
    showToast('提交结果未知，请到"我的订单"确认后重试', 2500);
  });
}

// 成功弹窗关闭时清空文件列表（对齐小程序 onCloseModal）
function clearFilesAfterSuccess() {
  printState.selectedFiles = [];
  Object.keys(printState._pollTimers).forEach(k => stopPageCountPoll(Number(k)));
  Object.keys(printState._uploadTimers).forEach(k => stopUploadTimer(Number(k)));
  const scroll = document.getElementById('fileListScroll');
  if (scroll) scroll.style.height = '';
  renderFileList();
  updateFileBadge(false);
}

/* ================= 价格计算 / 复制 ================= */

function calcCost(pageCount, copies, duplex) {
  const simplex = 0.2;
  const duplexP = 0.3;
  if (!pageCount || pageCount <= 0) return { cost: 0, formula: '?', known: false };
  if (duplex === 'on') {
    const pairs = Math.floor(pageCount / 2);
    const remainder = pageCount % 2;
    let cost, innerFormula;
    if (remainder === 0) { cost = pairs * duplexP; innerFormula = pairs + '张×' + duplexP.toFixed(2); }
    else if (pairs === 0) { cost = remainder * simplex; innerFormula = remainder + '张×' + simplex.toFixed(2); }
    else { cost = pairs * duplexP + remainder * simplex; innerFormula = pairs + '张×' + duplexP.toFixed(2) + '+' + remainder + '张×' + simplex.toFixed(2); }
    const formula = copies > 1 ? '(' + innerFormula + ')×' + copies + '份' : innerFormula;
    return { cost: Math.round(cost * copies * 100) / 100, formula, known: true };
  }
  const innerFormula = pageCount + '张×' + simplex.toFixed(2);
  const formula = copies > 1 ? '(' + innerFormula + ')×' + copies + '份' : innerFormula;
  return { cost: Math.round(pageCount * simplex * copies * 100) / 100, formula, known: true };
}

function orderEchoParams() {
  const d = printState._lastOrderResult;
  const echo = (d && d.data) || {};
  return {
    deliveryEnabled: echo.delivery_enabled != null ? !!Number(echo.delivery_enabled) : printState.deliveryEnabled,
    deliveryLocation: echo.delivery_location != null ? echo.delivery_location : printState.deliveryLocation,
    deliveryPercent: echo.delivery_percentage != null ? Number(echo.delivery_percentage) : printState.deliveryPercent,
    urgency: echo.urgency != null ? echo.urgency : printState.urgency,
    urgencyPrice: echo.urgency_price != null ? Number(echo.urgency_price) : printState.urgencyPrice,
    coverPage: echo.cover_page != null ? !!Number(echo.cover_page) : printState.coverPage,
    coverPagePrice: echo.cover_page_price != null ? Number(echo.cover_page_price) : printState.coverPagePrice,
  };
}

function onCopyPrice() {
  const d = printState._lastOrderResult;
  if (!d || !d.files) return;
  const p = orderEchoParams();
  let baseTotal = 0;
  let allKnown = true;
  d.files.forEach(f => {
    const r = calcCost(f.page_count || 0, f.copies || 1, f.duplex || 'on');
    baseTotal += r.cost;
    if (!r.known) allKnown = false;
  });
  let total = baseTotal;
  if (p.deliveryEnabled) total += baseTotal * (p.deliveryPercent / 100);
  total += p.urgencyPrice;
  if (p.coverPage) total += p.coverPagePrice;
  const prefix = allKnown ? '' : '≈ ';
  const amount = ((d.order_number || '') + ' ').trim() + prefix + '¥' + total.toFixed(2);
  copyText(amount, '已复制价格');
}

function onCopyDetailPrice() {
  const d = printState._lastOrderResult;
  if (!d || !d.files) return;
  const p = orderEchoParams();
  const lines = ['计费明细'];
  if (d.order_number) lines.push(d.order_number);
  lines.push('─'.repeat(14));
  const allParts = [];
  let baseTotal = 0;
  let itemNum = 0;
  d.files.forEach(f => {
    itemNum++;
    const r = calcCost(f.page_count || 0, f.copies || 1, f.duplex || 'on');
    const name = f.file_name || '未知文件';
    const duplexLabel = f.duplex === 'on' ? '双面' : '单面';
    const rangeLabel = f.page_range ? f.page_range + '页' : '全部页';
    lines.push(itemNum + '. ' + name);
    lines.push('   ' + f.copies + '份 | ' + duplexLabel + ' | ' + rangeLabel);
    if (r.cost > 0) { lines.push('   ' + r.formula + '=¥' + r.cost.toFixed(2)); allParts.push(r.cost.toFixed(2)); baseTotal += r.cost; }
    else lines.push('   💰 ?');
  });
  itemNum++;
  if (p.deliveryEnabled) {
    const deliveryCost = baseTotal * (p.deliveryPercent / 100);
    if (p.deliveryPercent > 0 && deliveryCost > 0) {
      lines.push(itemNum + '. 派送：是 | ' + p.deliveryLocation + ' ' + p.deliveryPercent.toFixed(1) + '% | ￥' + deliveryCost.toFixed(2));
      allParts.push(deliveryCost.toFixed(2));
    } else lines.push(itemNum + '. 派送：是 | ' + p.deliveryLocation + '免费');
  } else lines.push(itemNum + '. 派送：否');
  itemNum++;
  if (p.urgencyPrice > 0) {
    lines.push(itemNum + '. 优先级：' + p.urgency + ' | ￥' + p.urgencyPrice.toFixed(2));
    allParts.push(p.urgencyPrice.toFixed(2));
  } else lines.push(itemNum + '. 优先级：' + p.urgency + ' | ￥0');
  if (p.coverPage) {
    itemNum++;
    lines.push(itemNum + '. 打印首页信息 | ' + p.coverPagePrice.toFixed(2));
    allParts.push(p.coverPagePrice.toFixed(2));
  }
  const totalSum = allParts.reduce((s, x) => s + parseFloat(x), 0);
  lines.push('─'.repeat(14));
  lines.push('💰合计: ' + (allParts.join('+') || '0') + '=￥' + totalSum.toFixed(2));
  copyText(lines.join('\n'), '已复制详细价格');
}

/* ================= 事件绑定 ================= */

function setupPrintButtons() {
  const list = document.getElementById('fileList');
  list.addEventListener('click', (e) => {
    // 拖动结束后的 click 忽略（_dragHandled 由滑块拖动设置）
    const tg = e.target.closest('.duplex-toggle, .img-ori-toggle');
    if (tg && tg._dragHandled && Date.now() - tg._dragHandled < 500) { tg._dragHandled = 0; return; }
    const el = e.target.closest('[data-action]');
    if (!el) return;
    const card = el.closest('.file-card');
    const idx = card ? Array.prototype.indexOf.call(card.parentNode.children, card) : -1;
    if (idx < 0) return;
    const action = el.dataset.action;
    const f = printState.selectedFiles[idx];
    if (action === 'remove') removeFile(idx);
    else if (action === 'retry') retryUpload(idx);
    else if (action === 'copies-minus') setCopies(idx, f.copies - 1);
    else if (action === 'copies-plus') setCopies(idx, f.copies + 1);
    else if (action === 'duplex') setDuplex(idx, el.dataset.value);
    else if (action === 'ori') setOrientation(idx, el.dataset.value);
    else if (action === 'open-range') openRangePicker(idx);
  });
  list.addEventListener('input', (e) => {
    const t = e.target;
    if (t.dataset.action !== 'range-line') return;
    const card = t.closest('.file-card');
    const idx = card ? Array.prototype.indexOf.call(card.parentNode.children, card) : -1;
    const li = parseInt(t.dataset.line, 10);
    const f = printState.selectedFiles[idx];
    if (!f || f.isImage || f.excelWarning || f.unsupportedFormat) return;
    f.rangeLines[li].value = t.value;
    f.rangeLines[li].error = '';
    if (li === f.rangeLines.length - 1 && t.value.trim()) f.rangeLines.push({ value: '', error: '' });
    renderFileList();
    const inputs = document.querySelectorAll('[data-action="range-line"]');
    const target = inputs[Math.min(li, inputs.length - 1)];
    if (target) { target.focus(); target.setSelectionRange(target.value.length, target.value.length); }
  });
  list.addEventListener('blur', (e) => {
    const t = e.target;
    if (t.dataset.action === 'range-line') {
      const card = t.closest('.file-card');
      const idx = card ? Array.prototype.indexOf.call(card.parentNode.children, card) : -1;
      setTimeout(() => normalizeAndValidateRangeLines(idx, true), 0);
    }
  }, true);

  const switchClick = (el, fn) => {
    el.addEventListener('click', () => {
      // 拖动结束后的 click 忽略（bindSwitchDrags 设置 _dragHandled）
      if (el._dragHandled && Date.now() - el._dragHandled < 500) { el._dragHandled = 0; return; }
      fn();
    });
  };
  switchClick(document.getElementById('coverSwitch'), toggleCoverPage);
  document.getElementById('urgencyTrigger').addEventListener('click', toggleUrgencyPicker);
  switchClick(document.getElementById('deliverySwitch'), toggleDelivery);
  document.getElementById('deliveryTrigger').addEventListener('click', toggleDeliveryPicker);
  switchClick(document.getElementById('autoPrintSwitch'), toggleAutoPrint);
  switchClick(document.getElementById('adminPrintSwitch'), toggleAdminPrint);
  bindSwitchDrags();
  document.querySelectorAll('[data-schedule-mode]').forEach(el => {
    el.addEventListener('click', () => setScheduleMode(el.dataset.scheduleMode));
  });
  document.getElementById('scheduleDayTrigger').addEventListener('click', openScheduleDayPicker);
  document.getElementById('scheduleTimeTrigger').addEventListener('click', openScheduleTimePicker);
  document.getElementById('scheduleCountdownMinTrigger').addEventListener('click', openScheduleCountdownPicker);
  document.getElementById('scheduleCountdownSecTrigger').addEventListener('click', openScheduleCountdownPicker);
  document.getElementById('dayPickerOptions').addEventListener('click', (e) => {
    const opt = e.target.closest('[data-day]');
    if (opt) {
      printState.scheduleDayIndex = parseInt(opt.dataset.day, 10);
      closeModal('scheduleDayModal');
      updateScheduleUI();
    }
  });
  document.getElementById('scheduleTimeOk').addEventListener('click', confirmScheduleTime);
  document.getElementById('scheduleCountdownOk').addEventListener('click', confirmScheduleCountdown);
  document.getElementById('scheduleTimeCancel').addEventListener('click', () => closeModal('scheduleTimeModal'));
  document.getElementById('scheduleCountdownCancel').addEventListener('click', () => closeModal('scheduleCountdownModal'));

  document.getElementById('rangePickerGrid').addEventListener('click', (e) => {
    const cell = e.target.closest('[data-page]');
    if (cell) toggleRangeCell(parseInt(cell.dataset.page, 10));
  });
  document.getElementById('rangePickerAll').addEventListener('click', () => rangePickerSelectBy(() => true));
  document.getElementById('rangePickerOdd').addEventListener('click', () => rangePickerSelectBy(n => n % 2 === 1));
  document.getElementById('rangePickerEven').addEventListener('click', () => rangePickerSelectBy(n => n % 2 === 0));
  document.getElementById('rangePickerOk').addEventListener('click', confirmRangePicker);

  document.getElementById('submitBtn').addEventListener('click', onSubmit);
  document.getElementById('denyGoMe').addEventListener('click', () => { closeModal('denyModal'); switchToMe(); });
  document.getElementById('copyPriceBtn').addEventListener('click', onCopyPrice);
  document.getElementById('copyDetailPriceBtn').addEventListener('click', onCopyDetailPrice);
  document.getElementById('forceSubmitBtn').addEventListener('click', () => { closeModal('pageCountModal'); doSubmit(true); });
  document.getElementById('skipUnsupportedOk').addEventListener('click', () => { closeModal('unsupportedModal'); doSubmit(false); });
  document.getElementById('skipUnsupportedCancel').addEventListener('click', () => closeModal('unsupportedModal'));
  document.getElementById('urgencyPicker').addEventListener('click', (e) => {
    const opt = e.target.closest('[data-urg]');
    if (opt) selectUrgency(opt.dataset.urg);
  });
  document.getElementById('deliveryPicker').addEventListener('click', (e) => {
    const opt = e.target.closest('[data-loc]');
    if (opt) selectDeliveryLocation(opt.dataset.loc);
  });
}
