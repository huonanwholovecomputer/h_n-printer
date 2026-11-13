// utils/file_detail_card.js — 「文件详情卡」的数据装配（2026-11-13 晚）
//
// 卡片模板只有一份：pages/me/me.wxml 里的 <template name="myfileDetailCard">
// （样式见 pages/me/myfile-detail.wxss）。模板只认下面这些字段：
//   云端文件：original_name / typeIcon / has_preview / previewFailed / previewLoading /
//             previewShots[{page,url,ready,loaded}] / previewIndex / previewTip /
//             createdAtText / sizeText / pageCountText / orderText / expireText / in_active_order
//   本地单（没有云端副本）：local_only / localNote / localRows[{k,v}] / original_name / typeIcon
//
// 「我」页自己有 _decorateMyFile / _localFileDetail（那两个函数被 other/tests 逐个抽出来
// 单独执行，不能挪走）；「管理 → 某人的任务」页（pages/user-orders）用这一份。
// 两边字段名必须一致，改模板先来改这里。

const PREVIEW_MAX_PAGES = 12

// order_files.status 的词表：sent 才是"已完成"（success 只在旧数据里出现）
const STATUS_TEXT = {
  pending: '待打印',
  queued: '排队中',
  printing: '打印中',
  sent: '已完成',
  success: '已完成',
  failed: '打印失败',
  rejected: '已拒绝',
  cancelled: '已取消',
  canceled: '已取消',
  timeout: '超时',
  abandoned: '放弃打印',
  downloading: '文件传输中',
  waiting: '等待打印',
  scheduled: '已预约',
  reserved: '已预留',
}

function fmtSize(bytes) {
  const n = Number(bytes) || 0
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / (1024 * 1024)).toFixed(1) + ' MB'
}

function typeIcon(name) {
  const ext = String(name || '').toLowerCase().split('.').pop()
  if (ext === 'pdf') return '📕'
  if (['jpg', 'jpeg', 'png', 'webp', 'bmp', 'gif', 'tif', 'tiff'].indexOf(ext) >= 0) return '🖼'
  if (['doc', 'docx'].indexOf(ext) >= 0) return '📘'
  if (['xls', 'xlsx'].indexOf(ext) >= 0) return '📗'
  if (['ppt', 'pptx'].indexOf(ext) >= 0) return '📙'
  return '📄'
}

// <image> 带不了 Authorization 头，只能把 token 拼进查询串（后端 _openid_for_media 认这个）
function previewUrl(baseUrl, fileId, page, token) {
  const p = Number(page) > 0 ? Math.floor(Number(page)) : 1
  return String(baseUrl) + '/api/file_preview/' + encodeURIComponent(String(fileId)) +
    '?token=' + encodeURIComponent(String(token || '')) + '&page=' + p
}

// 只让当前页 ±1 页真的发请求：一份 12 页的 PDF 不该一展开就拉 12 张图
function shots(baseUrl, token, fileId, total, index, keepLoaded) {
  const n = Math.max(1, Math.min(PREVIEW_MAX_PAGES, Number(total) || 1))
  let cur = Number(index) || 0
  if (cur < 0) cur = 0
  if (cur >= n) cur = n - 1
  const old = {}
  ;(keepLoaded || []).forEach((s) => { if (s && s.page) old[s.page] = s })
  const out = []
  for (let p = 1; p <= n; p++) {
    const prev = old[p]
    const near = Math.abs(p - 1 - cur) <= 1
    out.push({
      page: p,
      url: previewUrl(baseUrl, fileId, p, token),
      // 翻到第 N 页时，第 N 页及其左右各一页必须变 ready（否则 swiper 停在一张
      // 永远不发请求的骨架上）；已经 ready 的页保持 ready，loaded 也不重拉。
      ready: prev ? (!!prev.ready || near) : near,
      loaded: prev ? !!prev.loaded : false,
      failed: prev ? !!prev.failed : false,
    })
  }
  return { shots: out, index: cur }
}

function retentionTextOf(f) {
  const hours = Number(f.retention_hours) || 0
  if (f.retention_label) return f.retention_label
  if (hours < 0) return '打印完成后立即删除'
  if (hours > 0) {
    return '保留 ' + (hours >= 24 && hours % 24 === 0 ? (hours / 24) + ' 天' : hours + ' 小时')
  }
  return '不自动删除'
}

// /api/file_detail/<id> 的 file 对象（与 /api/my_files 列表项同形状）→ 模板字段
function decorate(file, opt) {
  const f = file || {}
  const options = opt || {}
  const baseUrl = options.baseUrl || ''
  const token = options.token || ''
  const hours = Number(f.retention_hours) || 0
  const retentionText = retentionTextOf(f)
  const expireText = f.auto_delete_at ? ('将于 ' + f.auto_delete_at + ' 自动删除') : retentionText
  const previewPath = String(f.preview_url || '')
  // 后端在 has_preview=false 或没有 preview_url 时不给图；否则拼 ?token=&page=1
  const hasShot = f.has_preview !== false && !!previewPath
  const slug = String(f.file_id == null ? '' : f.file_id)
  const first = hasShot && slug ? previewUrl(baseUrl, slug, 1, token) : ''
  const pages = Number(f.page_count) || 0
  const hint = Math.max(0, Number(f.preview_pages) || 0)
  return Object.assign({}, f, {
    sizeText: fmtSize(f.size),
    retentionText: retentionText,
    expireText: expireText,
    previewUrl: first,
    previewPages: hint > 0 ? Math.min(hint, PREVIEW_MAX_PAGES) : (first ? 1 : 0),
    previewShots: first ? [{ page: 1, url: first, ready: true, loaded: false, failed: false }] : [],
    previewIndex: 0,
    previewLoading: !!first,
    previewFailed: false,
    previewTip: f.has_preview === false
      ? '该类型暂不支持预览图（图片 / PDF / Word / Excel / PPT 可以）'
      : '预览图暂不可用，可稍后重试',
    typeIcon: typeIcon(f.original_name),
    createdAtText: f.created_at ? String(f.created_at) : '未知',
    pageCountText: pages > 0 ? (pages + ' 页') : '',
    orderText: f.order_number ? String(f.order_number) : '未下单（仅上传）',
    hours: hours,
  })
}

// 本地打单（order_files.file_id 为空）/ 云端副本已被清理：没有图，但打印参数还在这单上
function localDetail(rowFile, reason) {
  const f = rowFile || {}
  const name = f.original_name || f.file_name || '未知文件'
  const copies = Number(f.copies) || 1
  const pages = Number(f.page_count) || 0
  const eff = Number(f.effective_pages) || pages
  const sheets = Number(f.total_sheets) || 0
  const rows = [{ k: '份数 / 页数', v: f.pagesLine || (copies + ' 份 × ' + eff + ' 页') }]
  if (sheets > 0) rows.push({ k: '合计张数', v: sheets + ' 张' })
  rows.push({ k: '单双面', v: f.duplex === 'on' ? '双面' : '单面' })
  if (f.page_range) rows.push({ k: '页码范围', v: f.page_range })
  if (f.simplex_pages) rows.push({ k: '单面页', v: f.simplex_pages })
  if (f.blank_tail_exclude) rows.push({ k: '末尾空白页', v: '已排除' })
  if (f.status) rows.push({ k: '打印状态', v: STATUS_TEXT[f.status] || f.status })
  if (f.reject_reason) rows.push({ k: '退回原因', v: f.reject_reason })
  if (f.total_price != null && Number(f.total_price) > 0) {
    rows.push({ k: '这一行金额', v: '¥' + Number(f.total_price).toFixed(2) })
  }
  return {
    local_only: true,
    original_name: name,
    typeIcon: typeIcon(name),
    localNote: reason === 'cleaned'
      ? '云端副本已被清理（保留期到了或手动删除过），预览图取不回来了；这单的打印参数还在，一起放在下面。'
      : '这单是本地打单 —— 源文件只保存在店主的电脑上，云端没有副本，所以没有预览图。',
    localRows: rows,
  }
}

module.exports = {
  PREVIEW_MAX_PAGES: PREVIEW_MAX_PAGES,
  fmtSize: fmtSize,
  typeIcon: typeIcon,
  previewUrl: previewUrl,
  shots: shots,
  decorate: decorate,
  localDetail: localDetail,
}
