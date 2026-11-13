/* eslint-disable */
/**
 * 「我的文件」点开看详情（小程序 + APP）· 验收
 *
 * 用户要求（2026-11-12 原话）：「在移动端的云端文件管理那里，需要支持点击文件列表中的
 * 文件然后在卡片内展示该文件的详细信息，比如时间、大小、1024x1024 略缩图等信息。」
 *
 * 背景（为什么现在才有）：后端 2026-11-05 那批（commit 9a7f24f）只落了后端 ——
 *   GET /api/file_preview/<id>（懒生成、≤1024×1024 JPEG、?token= 鉴权）+
 *   /api/my_files 每项补 order_id / order_number / has_preview / preview_url，
 *   当次提交里明确写着「前端（小程序/APP 详情页）下一轮接」。前端一直没接 ⇒ 用户点了没反应。
 *
 * 本脚本钉住的是「接上了」的四件事：
 *   ① 点行展开/收起（同一时间只展开一行；点「删除」不触发展开；点详情内部不收起）；
 *   ② 详情内容：上传时间 / 大小 / 页数 / 所属订单 / 保留期 / 状态；
 *   ③ 1024 预览图：has_preview 且 preview_url 才拼 <image src>/<img src>，必须带
 *     ?token=（<image>/<img> 带不了 Authorization 头）；失败/无能力时静默退回类型图标；
 *   ④ 两端样式盘得住：行改 block、头部 flex 下移、预览图固定高度。
 *
 * 运行：node other/tests/verify_myfile_detail.js
 */
const fs = require('fs');
const path = require('path');

const REPO = path.resolve(__dirname, '..', '..');
const MP = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'me');
const APP = path.join(REPO, 'mobile_apps', 'android_app', 'www');
const BACKEND = path.join(REPO, 'mobile_apps', 'printer-backend', 'app.py');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (detail ? '  -- ' + detail : ''));
}

const read = (p) => fs.readFileSync(p, 'utf8');
const MP_JS = read(path.join(MP, 'me.js'));
const MP_WXML = read(path.join(MP, 'me.wxml'));
/* 2026-11-13 晚：详情卡的样式抽到 me/myfile-detail.wxss（两个页面共用），
   断言一律对「两份拼接」做，别再只看 me.wxss。 */
const MP_WXSS = read(path.join(MP, 'me.wxss')) + '\n' + read(path.join(MP, 'myfile-detail.wxss'));
const APP_ME_JS = read(path.join(APP, 'me.js'));
const APP_APP_JS = read(path.join(APP, 'app.js'));
const APP_INDEX = read(path.join(APP, 'index.html'));
const APP_CSS = read(path.join(APP, 'styles.css'));
const APP_PY = read(BACKEND);

/* ---------- 工具：从真实源码里抠函数体（不用正则猜边界，按花括号配平） ---------- */
function extractFn(src, name) {
  // 形态 A：function name(...) { ... }（APP 端）
  let idx = src.indexOf('function ' + name + '(');
  if (idx >= 0) return sliceBraced(src, idx);
  // 形态 B：小程序 Page 对象里的简写方法 name(...) { ... }
  const re = new RegExp('(^|\\n)(\\s*)' + name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\(', 'g');
  const m = re.exec(src);
  if (!m) return '';
  const start = m.index + m[1].length + m[2].length;
  return 'function ' + sliceBraced(src, start);
}
function sliceBraced(src, idx) {
  let i = src.indexOf('{', idx);
  if (i < 0) return '';
  let depth = 0;
  for (let j = i; j < src.length; j++) {
    const c = src[j];
    if (c === '{') depth++;
    else if (c === '}') {
      depth--;
      if (depth === 0) return src.slice(idx, j + 1);
    }
  }
  return '';
}

/* 取某选择器的声明块（按整行/逗号分隔精确匹配，避免 .myfile-row 命中 .myfile-row-open） */
function decl(css, selector) {
  const lines = css.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const sels = lines[i].split(',').map((s) => s.trim().replace(/{$/, '').trim());
    if (!sels.includes(selector)) continue;
    let body = '';
    for (let j = i + 1; j < lines.length; j++) {
      body += lines[j] + '\n';
      if (lines[j].includes('}')) return body;
    }
    return body;
  }
  return null;
}

/* ==================== 1) 小程序：模板接线 ==================== */
console.log('\n--- 1) 小程序 me.wxml：行内展开 ---');

/* 把 myFiles 那条 wx:for 的整块抠出来（从 wx:for 起，到 </scroll-view> 前） */
const listStart = MP_WXML.indexOf('class="myfiles-list"');
const listEnd = MP_WXML.indexOf('</scroll-view>', listStart);
const rowBlock = (listStart >= 0 && listEnd > listStart) ? MP_WXML.slice(listStart, listEnd) : '';
/* 2026-11-13：详情卡抽成了 <template name="myfileDetailCard">（行内展开与订单文件详情弹窗
   共用一份，用户要求 4「复用代码逻辑」），所以详情字段/预览结构的断言要连同模板一起看。 */
const tplStart = MP_WXML.indexOf('<template name="myfileDetailCard">');
const tplEnd = tplStart >= 0 ? MP_WXML.indexOf('</template>', tplStart) : -1;
const tplBlock = (tplStart >= 0 && tplEnd > tplStart)
  ? MP_WXML.slice(tplStart, tplEnd + '</template>'.length) : '';
const detailBlock = rowBlock + '\n' + tplBlock;
check('详情卡抽成模板 myfileDetailCard（行内与弹窗共用）', !!tplBlock,
  tplBlock ? tplBlock.length + ' 字符' : '**没找到模板**');
check('模板被行内详情 + 订单文件弹窗两处引用',
  (MP_WXML.match(/<template is="myfileDetailCard"/g) || []).length === 2);

check('找到「我的文件」列表块', !!rowBlock && rowBlock.length > 200,
  rowBlock ? rowBlock.length + ' 字符' : '**没找到 .myfiles-list / scroll-view**');
check('行绑定 bindtap="onToggleMyFileDetail"',
  /bindtap="onToggleMyFileDetail"/.test(rowBlock));
check('行带 data-id（展开/收起按文件 id 认行）',
  /data-id="\{\{item\.file_id\}\}"/.test(rowBlock));
check('行 class 随展开态切换（myfile-row-open）',
  /myfile-row \{\{myFilesDetailId === item\.file_id \? 'myfile-row-open' : ''\}\}/.test(rowBlock));
check('「删除」仍是 catchtap（点删除不触发展开/收起）',
  /class="myfile-del"\s+catchtap="onDeleteMyFile"/.test(rowBlock));
check('详情块 wx:if 只对被点开的那一行渲染',
  /wx:if="\{\{myFilesDetailId === item\.file_id\}\}"\s+class="myfile-detail"/.test(rowBlock));
check('详情区自身 catchtap="noop"（点详情内部不收起）',
  /class="myfile-detail"\s+catchtap="noop"/.test(rowBlock));
check('详情紧跟在 .myfile-row 内部（不是另开一张卡）',
  /<view class="myfile-head">[\s\S]*<\/view>\s*<view wx:if="\{\{myFilesDetailId === item\.file_id\}\}"\s+class="myfile-detail"/.test(rowBlock));
check('<image> 用 shot.url + aspectFit + bindload/binderror + data-page',
  /<image[^>]*class="myfile-preview[^"]*"[^>]*src="\{\{shot\.url\}\}"/.test(tplBlock) &&
  /<image[^>]*mode="aspectFit"/.test(tplBlock) &&
  /bindload="onMyFilePreviewLoad"/.test(tplBlock) &&
  /binderror="onMyFilePreviewError"/.test(tplBlock) &&
  /data-page="\{\{shot\.page\}\}"/.test(tplBlock));
check('无预览能力时走 fallback 图标块（不是空框）',
  /class="myfile-preview-fallback"/.test(tplBlock) &&
  /f\.previewFailed/.test(tplBlock));

const detailKvs = ['上传时间', '文件大小', '所属订单', '保留期', '状态'];
for (const k of detailKvs) {
  check('详情含「' + k + '」字段', new RegExp('<text class="myfile-k">' + k + '</text>').test(detailBlock));
}
check('页数按需显示（pageCountText 不存在就不占一行）',
  /wx:if="\{\{f\.pageCountText\}\}"/.test(detailBlock));
/* 2026-11-13：箭头固定 ▾ + CSS 旋转（class 里多了展开态），不再是 ▴/▾ 换字形 */
check('有展开箭头（▾，展开时 .myfile-caret-open 转 180°）',
  /class="myfile-caret[ "{]/.test(rowBlock) && /▾/.test(rowBlock) &&
  /myfile-caret-open/.test(rowBlock));

/* ==================== 2) 小程序：装饰函数与交互行为 ==================== */
console.log('\n--- 2) 小程序 me.js：decorate / 展开 / 预览失败 ---');

/* 2026-11-13：展开/收起改成"量高度 + 过渡"，onToggleMyFileDetail 拆成了几个 helper，
   所以这里要一起抠出来并挂到 mpCtx（true 页面里它们是同一个 Page 对象上的方法）。 */
const mpFnNames = [
  '_fmtFileSize', '_myFileTypeIcon', '_decorateMyFile',
  '_myFileDetailStyle', '_clearMyFileDetailTimer', '_nextFrame',
  '_measureMyFileDetailCard', '_openMyFileDetail', '_closeMyFileDetail',
  'onToggleMyFileDetail', 'onMyFilePreviewError',
  // 2026-11-13 新增（多页 / 骨架 / 全屏 / 订单文件详情弹窗）
  '_myFilePreviewUrl', '_myFilePreviewShot', '_myFilePreviewShots',
  '_patchMyFile', '_patchFileDetail', '_prepareMyFilePreview',
  'onMyFilePreviewLoad', 'onMyFilePreviewChange', 'onOpenMyFilePreview',
  'onOpenOrderFileDetail', 'onCloseFileDetail',
  '_orderFileAt', '_localFileDetail',
  // 全屏预览层的延迟 require 入口（页面里 pvCore() 才去拉 utils/preview_viewer.js）
  'pvCore',
  // 2026-11-13 晚：全屏预览层改成自绘（用户要求双击放大），这 8 个是页面上的委托方法
  'onPvTouchStart', 'onPvTouchMove', 'onPvTouchEnd', 'onPvTouchCancel',
  'onPvClose', 'onPvPrev', 'onPvNext', 'onPvImageError',
];
const mpBody = mpFnNames.map((n) => extractFn(MP_JS, n)).filter(Boolean).join('\n\n');

check('抠到 ' + mpFnNames.length + ' 个函数（装饰 + 展开/收起动画链路）',
  mpBody.split('function ').length - 1 === mpFnNames.length,
  (mpBody.split('function ').length - 1) + '/' + mpFnNames.length + ' 个');

const MP_TOKEN = 'tok+9/1';
const previewCalls = [];
const wxStub = {
  getStorageSync: (k) => (k === 'token' ? MP_TOKEN : ''),
  // 全屏预览改成自绘层（utils/preview_viewer.js），不再调 wx.previewImage；
  // 这里只留 getWindowInfo，手势按 375×667 的窗口算锚点/边界。
  previewImage: (opt) => previewCalls.push(opt),
  getWindowInfo: () => ({ windowWidth: 375, windowHeight: 667 }),
};
const CONFIG_STUB = { BASE_URL: 'https://api.test' };
const reqCalls = [];
const requestStub = (opt) => { reqCalls.push(opt); if (requestStub.handler) requestStub.handler(opt); };
/* 全屏层实现（utils/preview_viewer.js）也要装进同一个作用域：页面里的 onPv* 都转发给它 */
const PV_JS = read(path.join(REPO, 'mobile_apps', 'h_n_print', 'utils', 'preview_viewer.js'));
const pvViewer = new Function('wx', 'module', PV_JS + '\nreturn module.exports;')(wxStub, { exports: {} });
/* 页面里用 pvCore() 延迟 require 预览层实现（不进首屏依赖），桩里照这个路径给回同一份模块 */
const pvRequire = (dep) => {
  if (String(dep).indexOf('preview_viewer') >= 0) return pvViewer;
  throw new Error('沙箱不该 require: ' + dep);
};
const mpApi = new Function('wx', 'CONFIG', 'PREVIEW_MAX_PAGES', 'request', 'require',
  mpBody + '\nreturn { ' + mpFnNames.map((n) => n + ': ' + n).join(', ') + ' };')(
  wxStub, CONFIG_STUB, 12, requestStub, pvRequire);

/* Page 实例：_decorateMyFile 里用 this._fmtFileSize / this._myFileTypeIcon，
   所以这两个必须先挂上去（真实页面里它们是同一个 Page 对象上的方法） */
const mpCtx = {
  data: { myFiles: [], myFilesDetailId: '', myFilesDetailStyle: '', pv: pvViewer.initialData().pv },
  // 全屏层用 'pv.scale' 这种点路径 setData，桩要跟着走
  setData(o, cb) {
    Object.keys(o).forEach((k) => {
      const parts = k.split('.');
      if (parts.length === 1) { this.data[k] = o[k]; return; }
      let cur = this.data;
      for (let i = 0; i < parts.length - 1; i++) {
        if (!cur[parts[i]]) cur[parts[i]] = {};
        cur = cur[parts[i]];
      }
      cur[parts[parts.length - 1]] = o[k];
    });
    if (typeof cb === 'function') cb();
  },
  onPvTouchStart: mpApi.onPvTouchStart,
  onPvTouchMove: mpApi.onPvTouchMove,
  onPvTouchEnd: mpApi.onPvTouchEnd,
  onPvTouchCancel: mpApi.onPvTouchCancel,
  onPvClose: mpApi.onPvClose,
  onPvPrev: mpApi.onPvPrev,
  onPvNext: mpApi.onPvNext,
  onPvImageError: mpApi.onPvImageError,
  // 单测没有真实节点：wxStub 没有 createSelectorQuery ⇒ 量高度走"量到 0"的护栏分支，
  // 展开/收起在测试里等价于瞬时状态切换（动画本身另有源码断言钉住）。
  _fmtFileSize: mpApi._fmtFileSize,
  _myFileTypeIcon: mpApi._myFileTypeIcon,
  // 订单文件详情弹窗里也用 _decorateMyFile 修饰（同一套字段）
  _decorateMyFile: mpApi._decorateMyFile,
  _orderFileAt: mpApi._orderFileAt,
  _localFileDetail: mpApi._localFileDetail,
  _myFileDetailStyle: mpApi._myFileDetailStyle,
  _clearMyFileDetailTimer: mpApi._clearMyFileDetailTimer,
  _nextFrame: mpApi._nextFrame,
  _measureMyFileDetailCard: mpApi._measureMyFileDetailCard,
  _openMyFileDetail: mpApi._openMyFileDetail,
  _closeMyFileDetail: mpApi._closeMyFileDetail,
  _myFilePreviewUrl: mpApi._myFilePreviewUrl,
  _myFilePreviewShot: mpApi._myFilePreviewShot,
  _myFilePreviewShots: mpApi._myFilePreviewShots,
  _patchMyFile: mpApi._patchMyFile,
  _patchFileDetail: mpApi._patchFileDetail,
  _prepareMyFilePreview: mpApi._prepareMyFilePreview,
};

const pdfFile = mpApi._decorateMyFile.call(mpCtx, {
  file_id: '7', original_name: '作业.pdf', size: 2048, page_count: 3,
  created_at: '2026-11-12 20:01:00', has_preview: true,
  preview_url: '/api/file_preview/7', order_number: 'HN20261112-0001',
  retention_hours: 24, auto_delete_at: '2026-11-13 20:01:00',
});
check('PDF：previewUrl 拼上 ?token=（并且 URL 编码 + /）',
  pdfFile.previewUrl === 'https://api.test/api/file_preview/7?token=' + encodeURIComponent(MP_TOKEN),
  pdfFile.previewUrl);
check('PDF：上传时间/页数/所属订单/大小 都解出来',
  pdfFile.createdAtText === '2026-11-12 20:01:00' && pdfFile.pageCountText === '3 页' &&
  pdfFile.orderText === 'HN20261112-0001' && pdfFile.sizeText === '2.0 KB',
  [pdfFile.createdAtText, pdfFile.pageCountText, pdfFile.orderText, pdfFile.sizeText].join(' | '));
check('PDF：类型图标 📕，previewFailed 初始 false',
  pdfFile.typeIcon === '📕' && pdfFile.previewFailed === false);

/* 2026-11-13 起 Office（doc/docx/xls/xlsx/ppt/pptx）后端也能出预览图了，
   所以"没有预览能力"的夹具换成 .txt（这类文件仍是 has_preview:false）。 */
const docxFile = mpApi._decorateMyFile.call(mpCtx, {
  file_id: '8', original_name: 'a.txt', size: 100, page_count: 0,
  created_at: '2026-11-01 10:00:00', has_preview: false, preview_url: '',
});
check('txt：没有预览图 ⇒ previewUrl 为空（不发请求）+ 图标 📄',
  docxFile.previewUrl === '' && docxFile.typeIcon === '📄');
check('txt：提示写明支持图片 / PDF / Word / Excel / PPT',
  docxFile.previewTip.indexOf('Word / Excel / PPT') >= 0 &&
  docxFile.previewTip.indexOf('图片') >= 0, docxFile.previewTip);
check('无页数 ⇒ pageCountText 空串（详情不占一行）', docxFile.pageCountText === '');
check('无订单 ⇒ 所属订单显示「未下单（仅上传）」', docxFile.orderText === '未下单（仅上传）');
check('无 created_at ⇒ 显示「未知」（不是 undefined）', mpApi._decorateMyFile.call(mpCtx, {
  file_id: '9', original_name: 'x.pdf', has_preview: true, preview_url: '/api/file_preview/9',
}).createdAtText === '未知');

const legacy = mpApi._decorateMyFile.call(mpCtx, {
  file_id: '10', original_name: 'x.pdf', has_preview: undefined,
  preview_url: '/api/file_preview/10',
});
check('老后端没 has_preview 字段时仍拼预览 URL（!== false 口径，不会一升级就丢预览）',
  legacy.previewUrl.indexOf('/api/file_preview/10') > 0, legacy.previewUrl);

/* 展开/收起：再点同一行收起，点别行切换 */
mpCtx.data.myFiles = [pdfFile, docxFile];
mpCtx.data.myFilesDetailId = '';
const ev = (id) => ({ currentTarget: { dataset: { id } } });
mpApi.onToggleMyFileDetail.call(mpCtx, ev('7'));
check('点行 → 展开（myFilesDetailId = 7）', mpCtx.data.myFilesDetailId === '7');
mpApi.onToggleMyFileDetail.call(mpCtx, ev('7'));
check('再点同一行 → 收起（空串）', mpCtx.data.myFilesDetailId === '');
mpApi.onToggleMyFileDetail.call(mpCtx, ev('7'));
mpApi.onToggleMyFileDetail.call(mpCtx, ev('8'));
check('点另一行 → 切换（只展开一个）', mpCtx.data.myFilesDetailId === '8');
mpApi.onToggleMyFileDetail.call(mpCtx, ev(''));
check('没有 data-id 的事件被忽略（不把状态弄成空字符串以外的怪值）',
  mpCtx.data.myFilesDetailId === '8');

/* 预览图失败：只把那一条退回图标，展开态与其它行不变 */
mpCtx.data.myFiles = [
  mpApi._decorateMyFile.call(mpCtx, { file_id: '7', original_name: 'a.pdf', has_preview: true, preview_url: '/api/file_preview/7' }),
  mpApi._decorateMyFile.call(mpCtx, { file_id: '8', original_name: 'b.pdf', has_preview: true, preview_url: '/api/file_preview/8' }),
];
mpCtx.data.myFilesDetailId = '7';
mpApi.onMyFilePreviewError.call(mpCtx, ev('7'));
check('预览失败 ⇒ 该行 previewFailed=true（退回图标，不弹错误）',
  mpCtx.data.myFiles[0].previewFailed === true);
check('预览失败不影响别的行', mpCtx.data.myFiles[1].previewFailed === false);
check('预览失败不收起详情', mpCtx.data.myFilesDetailId === '7');

/* ==================== 2.5) 多页预览 / 加载骨架 / 全屏 / 订单文件详情弹窗 ==================== */
console.log('\n--- 2.5) 多页预览 + 加载骨架 + 全屏 + 订单文件详情弹窗 ---');

check('预览区用 swiper（一页一个 swiper-item）+ 页码角标',
  /<swiper[^>]*class="myfile-preview-swiper"/.test(tplBlock) &&
  /<swiper-item wx:for="\{\{f\.previewShots\}\}"/.test(tplBlock) &&
  /myfile-preview-pager/.test(tplBlock));
check('点预览图触发全屏（bindtap=onOpenMyFilePreview + data-scope 区分宿主）',
  /bindtap="onOpenMyFilePreview"/.test(tplBlock) && /data-scope="\{\{scope\}\}"/.test(tplBlock));
check('加载中显示骨架（previewLoading + shimmer 条 + 「正在生成预览图」）',
  /f\.previewLoading/.test(tplBlock) && /myfile-preview-skeleton/.test(tplBlock) &&
  /myfile-skeleton-bar/.test(tplBlock) && /正在生成预览图/.test(tplBlock));
check('WXSS：骨架横扫动画 + 真图淡入 + 多页 420rpx 高度',
  /@keyframes myfileShimmer/.test(MP_WXSS) &&
  /background-size:\s*400%/.test(MP_WXSS) &&
  /\.myfile-preview-loaded\s*\{[^}]*opacity:\s*1/.test(MP_WXSS) &&
  /\.myfile-preview-swiper\s*\{[^}]*height:\s*420rpx/.test(MP_WXSS));
check('订单卡「文件列表」行可点开详情（bindtap + data-id=f.file_id + 单号/下标）',
  /class="detail-file-row"[\s\S]{0,240}?bindtap="onOpenOrderFileDetail"[\s\S]{0,240}?data-id="\{\{f\.file_id\}\}"/.test(MP_WXML));
check('文件详情弹窗独立存在（filedetail-sheet + /api/file_detail/）',
  /class="modal-sheet filedetail-sheet"/.test(MP_WXML) && /\/api\/file_detail\//.test(MP_JS));
check('弹窗关闭走 200ms 动画后再卸载（fileDetailClosing）',
  /fileDetailClosing: false/.test(MP_JS) && /onCloseFileDetail\(\)/.test(MP_JS));

const multi = mpApi._decorateMyFile.call(mpCtx, {
  file_id: '21', original_name: 'big.pdf', size: 1000, page_count: 9,
  has_preview: true, preview_url: '/api/file_preview/21', preview_pages: 9,
});
check('修饰时先只放第 1 页（URL 带 &page=1）',
  multi.previewShots.length === 1 && multi.previewShots[0].url.indexOf('&page=1') > 0,
  JSON.stringify(multi.previewShots));
check('has_preview 时 previewLoading 初始为 true（骨架先出）', multi.previewLoading === true);

mpCtx.data.myFiles = [multi];
const applied = [];
mpApi._prepareMyFilePreview.call(mpCtx, multi, (p) => applied.push(p));
const shotPatch = applied.length ? applied[applied.length - 1] : null;
const shots = shotPatch ? shotPatch.previewShots : [];
check('展开时按 preview_pages 切片，只有前 2 页 ready（不一次拉 9 张）',
  shots.length === 9 && shots[0].ready === true && shots[1].ready === true &&
  shots[2].ready === false, shots.map((s) => (s.ready ? 'R' : '-')).join(''));
check('切片上限 12 页（与后端 PREVIEW_MAX_PAGES 对齐）',
  mpApi._myFilePreviewShots.call(mpCtx, '21', 99, 0, []).length === 12);

const unknown = mpApi._decorateMyFile.call(mpCtx, {
  file_id: '22', original_name: 'x.docx', size: 1000, page_count: 0,
  has_preview: true, preview_url: '/api/file_preview/22',
});
check('页数未知 ⇒ preview_pages=0（展开时再问 /info）', unknown.previewPages === 0);
reqCalls.length = 0;
let patched = null;
mpApi._prepareMyFilePreview.call(mpCtx, unknown, (p) => { patched = p; });
check('页数未知 ⇒ 调 /api/file_preview/<id>/info?token=',
  reqCalls.length === 1 && reqCalls[0].url.indexOf('/api/file_preview/22/info?token=') > 0,
  reqCalls.length ? reqCalls[0].url : '**没发请求**');
reqCalls[0].success({ statusCode: 200, data: { success: true, pages: 4 } });
check('/info 返回 4 页 ⇒ 切片补到 4 页', !!patched && patched.previewShots.length === 4,
  patched ? String(patched.previewShots.length) : '**没有回调**');

// 页数核对：files.page_count 会少算（Office 转 PDF 后页数可能变多，线上 2 页的 docx 实际 3 页），
// 所以展开时**一律**向 /info 核对一次，但同一个文件只问一次。
reqCalls.length = 0;
mpApi._prepareMyFilePreview.call(mpCtx, unknown, () => {});
check('已核对过的文件不再重复问 /info（说了就记住）', reqCalls.length === 0, reqCalls.length);
check('源码：页数核对走 _previewInfoCache（每文件一次）',
  /this\._previewInfoCache\s*=\s*this\._previewInfoCache\s*\|\|\s*\{\}/.test(MP_JS) &&
  /this\._previewInfoCache\[fileId\]\s*=\s*n\s*\|\|\s*1/.test(MP_JS));

mpCtx.data.myFiles = [multi];
mpApi._prepareMyFilePreview.call(mpCtx, mpCtx.data.myFiles[0], (p) => {
  mpCtx.data.myFiles = mpCtx.data.myFiles.map((f) => Object.assign({}, f, p));
});
mpApi.onMyFilePreviewLoad.call(mpCtx, { currentTarget: { dataset: { id: '21', page: '1', scope: 'list' } } });
check('图片加载完 ⇒ previewLoading=false + 该页 loaded=true',
  mpCtx.data.myFiles[0].previewLoading === false && mpCtx.data.myFiles[0].previewShots[0].loaded === true);
mpApi.onMyFilePreviewChange.call(mpCtx, { currentTarget: { dataset: { id: '21', scope: 'list' } }, detail: { current: 5 } });
check('翻到第 6 页 ⇒ previewIndex=5 且相邻页 ready（预取）',
  mpCtx.data.myFiles[0].previewIndex === 5 &&
  mpCtx.data.myFiles[0].previewShots[4].ready && mpCtx.data.myFiles[0].previewShots[5].ready &&
  mpCtx.data.myFiles[0].previewShots[6].ready);
previewCalls.length = 0;
mpApi.onOpenMyFilePreview.call(mpCtx, { currentTarget: { dataset: { id: '21', scope: 'list', index: '5' } } });
check('点图 ⇒ 打开自绘全屏层（9 页、停在第 6 页，不再调 wx.previewImage）',
  mpCtx.data.pv.visible === true && mpCtx.data.pv.count === 9 && mpCtx.data.pv.index === 5 &&
  previewCalls.length === 0,
  'visible=' + mpCtx.data.pv.visible + ' count=' + mpCtx.data.pv.count +
  ' index=' + mpCtx.data.pv.index + ' previewImage=' + previewCalls.length);

/* ==================== 2.6) 全屏层自绘手势：双击放大 / 捏合 / 拖动 / 滑页 ==================== */
console.log('\n--- 2.6) 全屏预览层：双击放大（用户 2026-11-13 要求）/ 捏合 / 拖动 / 翻页 ---');
function tapAt(x, y) {
  const e = { touches: [], changedTouches: [{ clientX: x, clientY: y }] };
  mpApi.onPvTouchStart.call(mpCtx, { touches: [{ clientX: x, clientY: y }] });
  mpApi.onPvTouchEnd.call(mpCtx, e);
}
function pvReset() {
  mpCtx.data.pv.scale = 1; mpCtx.data.pv.x = 0; mpCtx.data.pv.y = 0;
}

/* 双击（320ms 内两次 tap）⇒ 2.5 倍，且以点按处为锚 */
pvReset();
tapAt(100, 200);
check('单击不放大（第一次 tap 只记时间）', Math.abs(mpCtx.data.pv.scale - 1) < 0.01,
  'scale=' + mpCtx.data.pv.scale);
tapAt(100, 200);
check('双击 ⇒ 放大到 2.5 倍', Math.abs(mpCtx.data.pv.scale - 2.5) < 0.01,
  'scale=' + mpCtx.data.pv.scale);
check('放大锚点=点按处（左上区域 ⇒ 内容向右下偏移，符合 translate+scale 换算）',
  Math.abs(mpCtx.data.pv.x - (1 - 2.5) * (100 - 187.5)) < 1 &&
  Math.abs(mpCtx.data.pv.y - (1 - 2.5) * (200 - 333.5)) < 1,
  'x=' + mpCtx.data.pv.x + ' y=' + mpCtx.data.pv.y);
tapAt(100, 200); tapAt(100, 200);
check('放大后再双击 ⇒ 还原 1 倍且回到中点', Math.abs(mpCtx.data.pv.scale - 1) < 0.01 &&
  mpCtx.data.pv.x === 0 && mpCtx.data.pv.y === 0,
  'scale=' + mpCtx.data.pv.scale + ' x=' + mpCtx.data.pv.x + ' y=' + mpCtx.data.pv.y);

/* 两次 tap 间隔太久 ⇒ 不算双击 */
pvReset();
tapAt(120, 240);
mpCtx.data.pv.scale = 1;
mpApi.onPvTouchEnd.call(mpCtx, { touches: [], changedTouches: [{ clientX: 300, clientY: 400 }] });
check('两次 tap 位置差很大 ⇒ 不当双击（第二次是滑页候选）', Math.abs(mpCtx.data.pv.scale - 1) < 0.01,
  'scale=' + mpCtx.data.pv.scale);

/* 两指捏合：100px → 200px 距离 ⇒ 2 倍 */
pvReset();
mpApi.onPvTouchStart.call(mpCtx, { touches: [{ clientX: 100, clientY: 300 }, { clientX: 200, clientY: 300 }] });
mpApi.onPvTouchMove.call(mpCtx, { touches: [{ clientX: 50, clientY: 300 }, { clientX: 250, clientY: 300 }] });
check('双指捏合 ⇒ 按两指间距比例放大（100→200px = 2 倍）', Math.abs(mpCtx.data.pv.scale - 2) < 0.01,
  'scale=' + mpCtx.data.pv.scale);
mpApi.onPvTouchMove.call(mpCtx, { touches: [{ clientX: 0, clientY: 300 }, { clientX: 800, clientY: 300 }] });
check('捏合上限 5 倍（不会无限放大）', Math.abs(mpCtx.data.pv.scale - 5) < 0.01, 'scale=' + mpCtx.data.pv.scale);
mpApi.onPvTouchEnd.call(mpCtx, { touches: [], changedTouches: [{ clientX: 400, clientY: 300 }] });

/* 放大后单指拖动 */
pvReset();
mpCtx.data.pv.scale = 2;
mpCtx.data.pv.x = 0; mpCtx.data.pv.y = 0;
mpApi.onPvTouchStart.call(mpCtx, { touches: [{ clientX: 100, clientY: 300 }] });
mpApi.onPvTouchMove.call(mpCtx, { touches: [{ clientX: 140, clientY: 300 }] });
check('放大后单指拖动 ⇒ 平移跟手（+40px）', Math.abs(mpCtx.data.pv.x - 40) < 1, 'x=' + mpCtx.data.pv.x);
mpApi.onPvTouchMove.call(mpCtx, { touches: [{ clientX: 900, clientY: 300 }] });
check('平移被夹在图片边界内（不超过 (scale-1)*窗口宽/2）', Math.abs(mpCtx.data.pv.x - 187.5) < 1,
  'x=' + mpCtx.data.pv.x);
mpApi.onPvTouchEnd.call(mpCtx, { touches: [], changedTouches: [{ clientX: 900, clientY: 300 }] });

/* 未放大时左滑 ⇒ 下一页 */
pvReset();
mpCtx.data.pv.index = 5;
const beforeIdx = mpCtx.data.pv.index;
mpApi.onPvTouchStart.call(mpCtx, { touches: [{ clientX: 300, clientY: 300 }] });
mpApi.onPvTouchMove.call(mpCtx, { touches: [{ clientX: 280, clientY: 302 }] });
mpApi.onPvTouchEnd.call(mpCtx, { touches: [], changedTouches: [{ clientX: 200, clientY: 302 }] });
check('未放大时左滑 100px ⇒ 翻到下一页（且回到 1 倍）',
  mpCtx.data.pv.index === beforeIdx + 1 && Math.abs(mpCtx.data.pv.scale - 1) < 0.01,
  'index=' + mpCtx.data.pv.index + ' scale=' + mpCtx.data.pv.scale);

/* 关掉全屏层 */
mpApi.onPvClose.call(mpCtx);
check('点 ✕ ⇒ 全屏层关闭（pv.visible=false）', mpCtx.data.pv.visible === false);

/* 模板/样式断言：与 APP 的 #pfvMask 同款提示 */
const PV_WXML = read(path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'me', 'preview-viewer.wxml'));
check('全屏层模板：image 按 pv.scale/x/y 变形 + 双击提示 + 翻页按钮',
  /transform: translate3d\(\{\{pv\.x\}\}px, \{\{pv\.y\}\}px, 0\) scale\(\{\{pv\.scale\}\}\)/.test(PV_WXML) &&
  /双击放大/.test(PV_WXML) && /onPvPrev/.test(PV_WXML) && /onPvNext/.test(PV_WXML));
check('两页都挂同一份模板（me 页 + 管理入口页）',
  /<import src="\.\/preview-viewer\.wxml" \/>/.test(MP_WXML) &&
  /<template is="previewViewer" data="\{\{pv: pv\}\}" \/>/.test(MP_WXML) &&
  /<import src="\.\.\/me\/preview-viewer\.wxml" \/>/.test(read(path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'user-orders', 'user-orders.wxml'))) &&
  /<template is="previewViewer" data="\{\{pv: pv\}\}" \/>/.test(read(path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'user-orders', 'user-orders.wxml'))));
check('WXSS：全屏层铺满 + 过渡只在 pv.animating 时生效（拖动跟手）',
  /\.pv-mask\s*\{/.test(MP_WXSS) && /position:\s*fixed/.test(MP_WXSS) && /\.pv-img\s*\{/.test(MP_WXSS) &&
  /\.pv-close\s*\{/.test(MP_WXSS) && /\.pv-nav\s*\{/.test(MP_WXSS));
{
  // 2026-11-13 用户现场：「这个按钮的位置是不是有点太靠右上角了呢？」——原来 close 贴
  // right:28rpx + top:20rpx，正好压在微信右上角的胶囊按钮上 ⇒ 钉住它待在左上角。
  const closeBlock = ((MP_WXSS.match(/\.pv-close\s*\{[^}]*\}/) || [''])[0] || '')
    .replace(/\/\*[\s\S]*?\*\//g, '');   // 去掉注释再看声明，免得注释里的旧值把断言带偏
  check('全屏关闭按钮在左上角、不压微信胶囊（right 已删、left + safe-area）',
    /left:\s*24rpx/.test(closeBlock) && /safe-area-inset-top/.test(closeBlock) &&
    !/right:/.test(closeBlock), closeBlock.replace(/\s+/g, ' '));
}

mpCtx.data.myFiles = [multi];
mpCtx.data.myFiles = mpCtx.data.myFiles.map((f) => Object.assign({}, f, {
  previewShots: mpApi._myFilePreviewShots.call(mpCtx, '21', 9, 0, f.previewShots),
}));
mpApi.onMyFilePreviewError.call(mpCtx, { currentTarget: { dataset: { id: '21', page: '1', scope: 'list' } } });
check('多页时单页失败 ⇒ 仍留着预览块（previewFailed 保持 false）',
  mpCtx.data.myFiles[0].previewFailed === false &&
  mpCtx.data.myFiles[0].previewShots[0].failed === true);

reqCalls.length = 0;
mpCtx.data.fileDetail = null;
mpApi.onOpenOrderFileDetail.call(mpCtx, { currentTarget: { dataset: { id: '7' } } });
check('点订单文件行 ⇒ 打开弹窗并拉 /api/file_detail/7',
  mpCtx.data.fileDetailVisible === true && mpCtx.data.fileDetailLoading === true &&
  reqCalls.length === 1 && reqCalls[0].url === 'https://api.test/api/file_detail/7',
  reqCalls.length ? reqCalls[0].url : '**没发请求**');
reqCalls[0].success({
  statusCode: 200,
  data: {
    success: true,
    file: {
      file_id: '7', original_name: '作业.pdf', size: 2048, page_count: 3,
      created_at: '2026-11-12 20:01:00', has_preview: true,
      preview_url: '/api/file_preview/7', preview_pages: 3, order_number: 'HN20261112-0001',
    },
  },
});
check('弹窗拿到数据 ⇒ 与列表同款修饰（sizeText/orderText/previewShots）',
  mpCtx.data.fileDetailLoading === false && !!mpCtx.data.fileDetail &&
  mpCtx.data.fileDetail.sizeText === '2.0 KB' &&
  mpCtx.data.fileDetail.previewShots.length === 3 &&
  mpCtx.data.fileDetail.orderText === 'HN20261112-0001');
mpApi.onCloseFileDetail.call(mpCtx, {});
check('关闭弹窗 ⇒ 先播 200ms 收起动画（fileDetailClosing）',
  mpCtx.data.fileDetailClosing === true);

check('删除成功的分支里会把已删文件的展开态清掉',
  /myFilesDetailId: this\.data\.myFilesDetailId === fileId \? '' : this\.data\.myFilesDetailId/.test(MP_JS));
check('每次打开「我的文件」都从全部收起开始',
  // 2026-11-13 晚：多了 myFilesDetailStyle（展开动画的行内高度），断言按字段集合而不是整串
  /showMyFiles: true, myFilesClosing: false, myFilesDetailId: '', myFilesDetailStyle: ''/.test(MP_JS));

/* ==================== 3) APP：装饰 / 渲染 / 点击 ==================== */
console.log('\n--- 3) APP www/me.js：decorate / render / click ---');

const escHtmlSrc = extractFn(APP_APP_JS, 'escHtml');
const appFnNames = [
  'fmtMyFileSize', 'myFileTypeIcon', 'decorateMyFile', 'myfileKv',
  'myfileRowEl', 'closeMyFileDetailAnimated', 'toggleMyFileDetail',
  'renderMyFiles', 'myFilePreviewError', 'setupMyFiles',
];
const appBody = [
  escHtmlSrc,
  'let myfileAnimTimer = null;',
  ...appFnNames.map((n) => extractFn(APP_ME_JS, n)),
].filter(Boolean).join('\n\n');

check('抠到 ' + appFnNames.length + ' 个函数（含 escHtml）',
  appBody.split('function ').length - 1 === appFnNames.length + 1,
  (appBody.split('function ').length - 1) + '/' + (appFnNames.length + 1) + ' 个');

const APP_TOKEN = 'tok+9/1';
const appState = { token: APP_TOKEN, deleteCalls: [] };
const meState = { myFiles: [], myFilesDetailId: '', myFilesLoading: false, myFilesTotal: 0, myFilesPage: 1 };
const els = {};
const documentStub = {
  getElementById(id) {
    if (!els[id]) els[id] = {
      id, style: {}, textContent: '', innerHTML: '', _handlers: {},
      addEventListener(ev, fn) { this._handlers[ev] = fn; },
    };
    return els[id];
  },
};
const deleteStub = (id) => appState.deleteCalls.push(id);

const appApi = new Function('document', 'state', 'BASE_URL', 'meState', 'deleteMyFile', 'showToast',
  appBody +
  '\nreturn { ' + appFnNames.map((n) => n + ': ' + n).join(', ') + ', escHtml: escHtml };')(
  documentStub, appState, 'https://api.test', meState, deleteStub, () => {});

const appPdf = appApi.decorateMyFile({
  file_id: '7', original_name: '作业.pdf', size: 2048, page_count: 3,
  created_at: '2026-11-12 20:01:00', has_preview: true,
  preview_url: '/api/file_preview/7', order_number: 'HN20261112-0001',
  retention_hours: 24, auto_delete_at: '2026-11-13 20:01:00',
});
check('APP：previewUrl 用 state.token 拼 ?token=',
  appPdf.previewUrl === 'https://api.test/api/file_preview/7?token=' + encodeURIComponent(APP_TOKEN),
  appPdf.previewUrl);
check('APP：时间/页数/订单同口径',
  appPdf.createdAtText === '2026-11-12 20:01:00' && appPdf.pageCountText === '3 页' &&
  appPdf.orderText === 'HN20261112-0001');
check('APP：txt 无预览 ⇒ 空 URL + 📄（Office 现在是可预览类型）',
  appApi.decorateMyFile({ file_id: '8', original_name: 'a.txt', has_preview: false }).previewUrl === '' &&
  appApi.decorateMyFile({ file_id: '8', original_name: 'a.txt', has_preview: false }).typeIcon === '📄');

/* 渲染：收起态 */
meState.myFiles = [appPdf];
meState.myFilesDetailId = '';
appApi.renderMyFiles();
const htmlClosed = els['myFilesList'].innerHTML;
check('APP 渲染（收起）：有 myfile-head / myfile-caret ▾，没有 myfile-detail',
  htmlClosed.indexOf('myfile-head') >= 0 && htmlClosed.indexOf('▾') >= 0 &&
  htmlClosed.indexOf('myfile-detail') < 0, htmlClosed.slice(0, 80));
check('APP 渲染（收起）：行带 data-file-id（点击 handler 靠它认行）',
  /class="myfile-row" data-file-id="7"/.test(htmlClosed), htmlClosed.slice(0, 120));

/* 渲染：展开态 */
meState.myFilesDetailId = '7';
appApi.renderMyFiles();
const htmlOpen = els['myFilesList'].innerHTML;
check('APP 渲染（展开）：myfile-detail + 上传时间/大小/所属订单/保留期/状态',
  ['myfile-detail', '上传时间', '文件大小', '所属订单', '保留期', '状态'].every((s) => htmlOpen.indexOf(s) >= 0));
check('APP 渲染（展开）：<img class="myfile-preview" src=...token=...>',
  /<img class="myfile-preview" src="https:\/\/api\.test\/api\/file_preview\/7\?token=[^"]+"/.test(htmlOpen));
check('APP 渲染（展开）：onerror 回退钩子带 file_id',
  htmlOpen.indexOf("onerror=\"myFilePreviewError('7')\"") >= 0);
check('APP 渲染（展开）：箭头加 myfile-caret-open（CSS 转 180°）+ 行加 myfile-row-open',
  htmlOpen.indexOf('myfile-caret-open') >= 0 && htmlOpen.indexOf('myfile-row-open') >= 0);

/* 渲染：无预览能力的类型走 fallback */
meState.myFiles = [appApi.decorateMyFile({ file_id: '8', original_name: 'a.txt', has_preview: false })];
meState.myFilesDetailId = '8';
appApi.renderMyFiles();
const htmlNoPrev = els['myFilesList'].innerHTML;
check('APP 渲染（无预览）：fallback 图标 + 提示，且没有 <img>',
  htmlNoPrev.indexOf('myfile-preview-fallback') >= 0 &&
  htmlNoPrev.indexOf('Word / Excel / PPT') >= 0 && htmlNoPrev.indexOf('<img') < 0);

/* 渲染：预览加载失败后同一行退回 fallback */
meState.myFiles = [appApi.decorateMyFile({
  file_id: '7', original_name: 'a.pdf', has_preview: true, preview_url: '/api/file_preview/7',
})];
meState.myFilesDetailId = '7';
appApi.myFilePreviewError('7');
check('APP：预览失败 ⇒ 该行退回 fallback（且不弹错误）',
  els['myFilesList'].innerHTML.indexOf('myfile-preview-fallback') >= 0);

/* 点击分派：行 → 展开；详情内 → 不收起；删除胶囊 → 只删不展开 */
appApi.setupMyFiles();
const listEl = els['myFilesList'];
check('setupMyFiles 装上了 click 监听', typeof listEl._handlers.click === 'function');
/* 假 element：closest 按选择器返回自己 */
function fakeTarget(classes) {
  const self = {
    classes,
    closest(sel) {
      const want = sel.replace('.', '');
      return classes.indexOf(want) >= 0 ? self : null;
    },
    dataset: { fileId: '7', fileName: '作业.pdf' },
  };
  return self;
}
appState.deleteCalls = [];
meState.myFilesDetailId = '';
listEl._handlers.click({ target: fakeTarget(['myfile-row']) });
check('点行（不碰详情/删除）⇒ 展开该行', meState.myFilesDetailId === '7');
listEl._handlers.click({ target: fakeTarget(['myfile-detail']) });
check('点详情内部 ⇒ 不收起（对应小程序 catchtap="noop"）', meState.myFilesDetailId === '7');
listEl._handlers.click({ target: fakeTarget(['myfile-del']) });
check('点「删除」⇒ 只走删除，不改展开态',
  appState.deleteCalls.length === 1 && appState.deleteCalls[0] === '7' && meState.myFilesDetailId === '7');
check('删除成功的分支会清掉该文件的展开态',
  /if \(meState\.myFilesDetailId === fileId\) meState\.myFilesDetailId = ''/.test(APP_ME_JS));
check('每次打开「我的文件」都从全部收起开始',
  // 2026-11-13 晚：openMyFiles 开头多了 setupMyFilePreviewUI()，先断言「重置在开头 200 字符内且早于 openModal」
  /function openMyFiles\(\) \{[\s\S]{0,200}?meState\.myFilesDetailId = '';[\s\S]{0,120}?openModal\('myFilesModal'\)/.test(APP_ME_JS));

/* ==================== 4) 样式：两端都要「行读得下详情」 ==================== */
console.log('\n--- 4) 样式（小程序 wxss / APP css）---');
for (const [where, css, rowOpenPad, headFlex] of [
  ['小程序 me.wxss', MP_WXSS, /padding:\s*18rpx 0/, /display:\s*flex/],
  ['APP styles.css', APP_CSS, /padding:\s*2\.16cqw 0/, /display:\s*flex/],
]) {
  const row = decl(css, '.myfile-row');
  const head = decl(css, '.myfile-head');
  check(where + ' .myfile-row 改成 block（详情才能占下一行）',
    !!row && /display:\s*block/.test(row) && rowOpenPad.test(row), row ? row.replace(/\s+/g, ' ').trim() : '**缺失**');
  check(where + ' .myfile-head 接管 flex', !!head && headFlex.test(head) && /align-items:\s*center/.test(head));
  const preview = decl(css, '.myfile-preview');
  check(where + ' .myfile-preview 固定高度 + 不撑开卡片',
    !!preview && /height:\s*\d/.test(preview) && /width:\s*100%/.test(preview),
    preview ? preview.replace(/\s+/g, ' ').trim() : '**缺失**');
  /* 2026-11-13：内边距/圆角挪到内层 .myfile-detail-card —— 外层只做高度裁剪，
     否则收起动画结束时 padding 会把卡片留成一条灰边。 */
  check(where + ' .myfile-detail-card 有内边距/圆角（详情是一张内嵌小卡）',
    /padding:/.test(decl(css, '.myfile-detail-card') || '') &&
    /border-radius:/.test(decl(css, '.myfile-detail-card') || ''));
  check(where + ' .myfile-detail 只做高度裁剪（overflow:hidden + height 过渡）',
    /overflow:\s*hidden/.test(decl(css, '.myfile-detail') || '') &&
    /transition:[^;]*height\s+240ms/.test(decl(css, '.myfile-detail') || ''),
    (decl(css, '.myfile-detail') || '**缺失**').replace(/\s+/g, ' ').trim());
  check(where + ' 展开时箭头旋转（.myfile-caret-open rotate 180deg）',
    /transition:[^;]*transform/.test(decl(css, '.myfile-caret') || '') &&
    /rotate\(180deg\)/.test(decl(css, '.myfile-caret-open') || ''));
  check(where + ' 行底色随展开渐显（.myfile-row 有 background-color 过渡）',
    /transition:[^;]*background-color/.test(row || ''));
}
check('APP .myfile-preview 还写了 object-fit: contain（img 不走 aspectFit）',
  /object-fit:\s*contain/.test(decl(APP_CSS, '.myfile-preview') || ''));

/* ==================== 4.5) 展开/收起动画（2026-11-13 用户要求 1） ==================== */
console.log('\n--- 4.5) 展开/收起动画 ---');
check('小程序：展开先落 height:0 那一帧，再量真实高度',
  /_openMyFileDetail\(fileId\)[\s\S]*?_myFileDetailStyle\(0, 0\)[\s\S]*?_measureMyFileDetailCard[\s\S]*?_myFileDetailStyle\(h, 1\)/.test(MP_JS));
check('小程序：动画收尾把 height 改回 auto（预览图撑高不留白）',
  /_myFileDetailStyle\(null, 1\)/.test(MP_JS));
check('小程序：收起先钉 px 再归零，过渡结束才清 myFilesDetailId',
  /_closeMyFileDetail\(\)[\s\S]*?_myFileDetailStyle\(h, 1\)[\s\S]*?_myFileDetailStyle\(0, 0\)[\s\S]*?myFilesDetailId: ''/.test(MP_JS));
check('小程序：量不到高度时同步收起（单测/老基础库不至于关不掉）',
  /if \(!h\) \{[\s\S]*?myFilesDetailId: ''/.test(MP_JS));
check('APP：展开 0 → 实测高度 → 260ms 后 height:auto',
  /toggleMyFileDetail\(fileId\)[\s\S]*?style\.height = '0px'[\s\S]*?style\.height = h \+ 'px'[\s\S]*?style\.height = 'auto'/.test(APP_ME_JS));
check('APP：收起动画走完才重渲染（节点不是瞬间消失）',
  /closeMyFileDetailAnimated\(done\)[\s\S]*?style\.height = '0px'[\s\S]*?setTimeout\(finish, 260\)/.test(APP_ME_JS));
check('APP：没有真实 DOM 查询能力时退回同步切换（假 DOM 单测不炸）',
  /typeof list\.querySelectorAll !== 'function'/.test(APP_ME_JS));

/* ==================== 5) 后端契约：1024 + token 鉴权仍在 ==================== */
console.log('\n--- 5) 后端契约（别让前端拼的 URL 落空）---');
check('后端 PREVIEW_MAX_PX = 1024（用户要求的 1024×1024）',
  /PREVIEW_MAX_PX = 1024/.test(APP_PY));
check('后端有 GET /api/file_preview/<file_id>',
  /@app\.route\("\/api\/file_preview\/<file_id>"/.test(APP_PY));
check('预览端点接受 ?token=（<image>/<img> 带不了请求头）',
  /request\.args\.get\("token"\)/.test(APP_PY) && /def _openid_for_media/.test(APP_PY));
check('/api/my_files 每项回 has_preview + preview_pages + preview_url',
  /def _my_file_item\(/.test(APP_PY) &&
  /"has_preview": has_preview/.test(APP_PY) &&
  /has_preview = _has_preview_for\(/.test(APP_PY) &&
  /"preview_pages":/.test(APP_PY) &&
  /"preview_url":/.test(APP_PY));
check('/api/my_files 每项回 order_number（详情里的「所属订单」）',
  /"order_number":/.test(APP_PY));
check('没有预览能力时后端回 415（前端据此静默退回图标）',
  /_has_preview_for/.test(APP_PY) && /415/.test(APP_PY));

/* ==================== 汇总 ==================== */
console.log('\n' + (failures === 0 ? '全部通过 ✅（我的文件：点行看详情 + 1024 预览图，两端）'
  : ('失败 ' + failures + ' 条 ❌')));
process.exit(failures === 0 ? 0 : 1);
