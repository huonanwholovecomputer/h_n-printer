# -*- coding: utf-8 -*-
"""「订单详情 → 文件列表」内部滚动的**静态验收**（微信小程序那一半）

## 为什么需要这个脚本

2026-11-04 现场截图：一个「国家助学金申请书」任务里有 10+ 个文件，订单卡片展开后
文件列表只显示到一半就断了，后面的文件、附加服务、合计价格**永远看不到**。

根因（结构性问题，不是偶发）：订单详情容器 `.order-card-detail` 是
`max-height + overflow: hidden`（展开动画靠 max-height 过渡），而文件列表是详情里
**唯一不受限生长**的部分 —— 文件一多就把容器顶穿，撑出去的部分被 overflow 裁掉。

修法两端一起（同源逻辑，两边都改才算改完）：
  · 小程序：文件列表进 `<scroll-view scroll-y class="detail-files-scroll">`，滚动框上限约 5 行；
            同时把 me 页 `.detail-expanded` 的 2000rpx 抬到 3000rpx（与 user-orders 页对齐）。
  · APP：`me.js` 的 fileRows 外面包一层 `<view class="detail-files-scroll">`，CSS 给
            `max-height` + `overflow-y: auto`。
  · 文件多（> 5）时在标题后面补一句「· 列表可上下滑动」，让用户知道里面还能滑。

## 判据

① 两端文件列表都被独立滚动框包住（顺序：标题 → 滚动框开 → wx:for 行 → 滚动框关 → 区块关）
② 滚动框有高度上限（小程序 max-height / APP max-height + overflow-y:auto），且**不是**固定 height
   （固定 height 会让 1~2 个文件的订单留一大块空白）
③ 详情容器的展开上限 ≥ 3000rpx（me 页原 2000rpx 是这次被裁的直接原因；user-orders 早已 3000）
④ 提示文案只在文件 > 5 时出现，且没有 markdown 星号（根 CLAUDE.md 硬规矩 8）
⑤ wxml 标签配平（<scroll-view> 开合一致、<view> 配平）—— 小程序没有编译期检查，
   配平错只会在真机上报 "unexpected end tag"
⑥ 变异自证：把滚动框拆掉（= 改回旧结构）后 ① ② 必须变红，否则本脚本测不到这次修复

用法: python other/tests/verify_order_files_scroll.py   （exit 0 = 全部通过）
"""
import io
import os
import re
import sys

try:                                    # Windows 控制台默认 GBK，会吞掉 ✅/❌
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MP = os.path.join(REPO, 'mobile_apps', 'h_n_print')
APP = os.path.join(REPO, 'mobile_apps', 'android_app', 'www')

PAGES = [
    (os.path.join(MP, 'pages', 'me', 'me.wxml'), os.path.join(MP, 'pages', 'me', 'me.wxss'), 'me'),
    (os.path.join(MP, 'pages', 'user-orders', 'user-orders.wxml'),
     os.path.join(MP, 'pages', 'user-orders', 'user-orders.wxss'), 'user-orders'),
]

fails = []


def check(name, cond, detail=''):
    print(('[PASS] ' if cond else '[FAIL] ') + name + (('  -- ' + str(detail)) if detail else ''))
    if not cond:
        fails.append(name)


def read(path):
    with io.open(path, 'r', encoding='utf-8') as f:
        return f.read()


def block_of(src, marker='<!-- 文件列表 -->'):
    """取「文件列表」注释到该 detail-section 结束的那一段（到下一个 detail-section 注释为止）。"""
    i = src.find(marker)
    if i < 0:
        return ''
    j = src.find('<!-- 附加服务 -->', i)
    return src[i:] if j < 0 else src[i:j]


def css_rule(css, selector):
    m = re.search(re.escape(selector) + r'\s*\{([^}]*)\}', css)
    return m.group(1) if m else ''


# ════════════════════ ① ② ⑤ 小程序结构 ════════════════════
for wxml_path, wxss_path, tag in PAGES:
    wxml = read(wxml_path)
    wxss = read(wxss_path)
    blk = block_of(wxml)

    check(f'[{tag}] 找到文件列表区块', bool(blk))
    i_title = blk.find('detail-section-title')
    m_open = re.search(r'<scroll-view[^>]*class="detail-files-scroll"[^>]*>', blk)
    i_open = m_open.start() if m_open else -1
    i_for = blk.find('wx:for="{{item.files}}"')
    i_close = blk.find('</scroll-view>')
    i_end = blk.rfind('</view>')
    check(f'[{tag}] ① 文件列表被 <scroll-view scroll-y> 包住（标题 → 开 → 行 → 关 顺序正确）',
          -1 < i_title < i_open < i_for < i_close < i_end,
          f'title={i_title} open={i_open} for={i_for} close={i_close} end={i_end}')

    rule = css_rule(wxss, '.detail-files-scroll')
    mh = re.search(r'max-height\s*:\s*(\d+(?:\.\d+)?)rpx', rule)
    check(f'[{tag}] ② .detail-files-scroll 有 max-height（约 5 行文件）',
          bool(mh) and 300 <= float(mh.group(1)) <= 700, rule.strip() or '(缺规则)')
    check(f'[{tag}] ② .detail-files-scroll 不写死 height（短列表不留白）',
          bool(rule) and not re.search(r'(?<!max-)height\s*:', rule), rule.strip())

    exp = css_rule(wxss, '.order-card-detail.detail-expanded')
    mh2 = re.search(r'max-height\s*:\s*(\d+)rpx', exp)
    check(f'[{tag}] ③ 详情展开上限 ≥ 3000rpx（文件列表已自带滚动框，容器只需容下静态内容）',
          bool(mh2) and int(mh2.group(1)) >= 3000, exp.strip() or '(缺规则)')

    # ⑥ 手势接线：外层 .scroller 挂着 WXS 滚动引擎，它的 touchmove 会 preventDefault
    #    （返回 false）→ 原生 scroll-view 再也滚不动。列表必须 catchtouchmove 把事件终止在这一层。
    check(f'[{tag}] ⑥ 列表 catchtouchmove="noop"（不让 WXS 引擎 preventDefault 吞掉原生滚动）',
          '<scroll-view scroll-y class="detail-files-scroll" catchtouchmove="noop">' in blk)
    page_js = read(os.path.join(MP, 'pages', tag, tag + '.js'))
    check(f'[{tag}] ⑥ noop handler 存在（catchtouchmove 指向它）',
          re.search(r'\n\s*noop\s*\(\s*\)\s*\{\s*\}', page_js) is not None)

    # ⑤ 标签配平
    opens = len(re.findall(r'<view\b', wxml))
    closes = len(re.findall(r'</view>', wxml))
    sc_opens = len(re.findall(r'<scroll-view\b', wxml))
    sc_closes = len(re.findall(r'</scroll-view>', wxml))
    check(f'[{tag}] ⑤ <view> 标签配平', opens == closes, f'open={opens} close={closes}')
    check(f'[{tag}] ⑤ <scroll-view> 标签配平', sc_opens == sc_closes,
          f'open={sc_opens} close={sc_closes}')

    # ④ 提示文案
    check(f'[{tag}] ④ 提示只在文件 > 5 时出现',
          'wx:if="{{item.files.length > 5}}" class="detail-files-hint"' in blk)
    check(f'[{tag}] ④ 提示文案无 markdown 星号（硬规矩 8）',
          '列表可上下滑动' in blk and '**' not in blk)
    check(f'[{tag}] ④ .detail-files-hint 样式存在', bool(css_rule(wxss, '.detail-files-hint')))

# ════════════════════ ① ② APP（同源 www/） ════════════════════
mejs = read(os.path.join(APP, 'me.js'))
appcss = read(os.path.join(APP, 'styles.css'))

check('[APP] ① fileRows 被 <view class="detail-files-scroll"> 包住',
      re.search(r'detail-section-title">文件列表 \(\$\{o\.files\.length\}\)[^`]*?'
                r'<view class="detail-files-scroll">\$\{fileRows\}</view>', mejs) is not None)
rule = css_rule(appcss, '.detail-files-scroll')
check('[APP] ② .detail-files-scroll 有 max-height + overflow-y: auto',
      bool(rule) and 'max-height' in rule and 'overflow-y: auto' in rule, rule.strip() or '(缺规则)')
check('[APP] ② .detail-files-scroll 不写死 height', bool(rule) and not re.search(r'(?<!max-)height\s*:', rule))
check('[APP] ④ 提示文案只在文件 > 5 时出现',
      re.search(r'o\.files\.length > 5 \? \'<text class="detail-files-hint">'
                r'· 列表可上下滑动</text>\'', mejs) is not None)
check('[APP] ④ .detail-files-hint 样式存在', bool(css_rule(appcss, '.detail-files-hint')))

appjs = read(os.path.join(APP, 'app.js'))
touch_sel = re.search(r"onTouchStart\(e\)[\s\S]{0,400}?closest\('([^']+)'\)", appjs)
wheel_sel = re.search(r"onWheel\(e\)[\s\S]{0,300}?closest\('([^']+)'\)", appjs)
check('[APP] ⑥ FlingEngine.onTouchStart 把 .detail-files-scroll 认作内层可滚容器'
      '（否则引擎 preventDefault，列表滚不动）',
      bool(touch_sel) and '.detail-files-scroll' in touch_sel.group(1),
      touch_sel.group(1) if touch_sel else '(没找到 closest 选择器)')
check('[APP] ⑥ FlingEngine.onWheel 同样认这个类（桌面滚轮不再把页面滚走）',
      bool(wheel_sel) and '.detail-files-scroll' in wheel_sel.group(1),
      wheel_sel.group(1) if wheel_sel else '(没找到 closest 选择器)')

# ════════════════════ ⑥ 变异自证 ════════════════════
def mutated_is_red(src):
    """把滚动框拆掉（= 改回旧结构），① 必须变红。"""
    broken = re.sub(r'<scroll-view[^>]*class="detail-files-scroll"[^>]*>', '', src)
    blk = block_of(broken)
    i_title = blk.find('detail-section-title')
    i_open = blk.find('class="detail-files-scroll"')
    i_close = blk.find('</scroll-view>')
    return not (-1 < i_title < i_open < i_close)


check('⑥ 变异自证：拆掉滚动框后 ① 判据确实变红',
      all(mutated_is_red(read(p[0])) for p in PAGES))

print()
if fails:
    print('❌ %d 项失败：%s' % (len(fails), ' / '.join(fails[:4])))
    sys.exit(1)
print('✅ 全部通过（小程序 me / user-orders + APP www 两端文件列表均为内嵌滚动）')
sys.exit(0)
