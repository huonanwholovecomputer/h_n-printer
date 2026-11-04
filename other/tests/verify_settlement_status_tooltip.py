# -*- coding: utf-8 -*-
"""验收 · 结算页订单明细「异常状态 Tooltip + 标记人」（2026-11 批次，#9）

只做源码结构断言（不启服务、不跑浏览器）；行为细节由同批
tests/verify_settlement_order_ux.js 用 Node+vm 真跑页面函数覆盖。

钉住的口径：
  1. tooltip 三支优先级：manual_status_by（谁在何时标记为什么）→ status_reason
     （文件行退回 reject_reason）→ 通用文案；三段都不为空。
  2. 时间戳统一 YYYY-MM-DD HH:MM:SS（ISO 的 T 转空格、截到秒）。
  3. 订单行与文件行的状态标签都挂 title，且都走同一个 _statusTip。
  4. timeout=超时 进标签/色板/异常分组/筛选下拉（琥珀色，深色主题可读）。
  5. ORDER_FIXABLE_STATUSES 仍是 5 个终态，timeout 不进人工订正白名单。
  6. 标记人：成员名单名字、localStorage 记忆、空名单禁用、body 带 by。
  7. 文案无 markdown 星号、无 alert。

用法：python tests/verify_settlement_status_tooltip.py
exit 0 = 全部通过。
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HTML = os.path.join(REPO, "local_print_tool", "finance", "settlement.html")
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def body(fn_name):
    """取一个函数的源码片段（从 function 名开始，按大括号配平）。"""
    m = re.search(r"function\s+" + re.escape(fn_name) + r"\s*\(", src)
    if not m:
        return ""
    i = src.find("{", m.end())
    if i < 0:
        return ""
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    return ""


with open(HTML, encoding="utf-8") as f:
    src = f.read()

print("== 1. tooltip 文案与优先级 ==")
tip = body("_statusTip")
check("_statusTip 存在且非空", bool(tip.strip()))
check("① 人工订正优先：先取 manual_status_by，命中即 return",
      re.search(r"var by = String\(o\.manual_status_by \|\| ''\)\.trim\(\);\s*\n\s*if \(by\) return", tip) is not None)
check("① 文案逐字：{标记人}在{时间}被标记为{状态中文名}",
      "by + '在' + (_fmtStamp(o.manual_status_at) || '未知时间') + '被标记为' + label" in tip)
check("② 无人标记时用 status_reason（文件行退回 reject_reason）",
      "String(o.status_reason || o.reject_reason || '').trim()" in tip)
check("② 有原因时以「状态中文名：原因」返回", "return label + '：' + reason;" in tip)
check("③ 都没有时给通用文案，绝不返回空字符串", "return '当前状态：' + label;" in tip)
check("tooltip 文案里没有 markdown 星号", "*" not in tip)
check("tooltip 不用 alert", "alert(" not in tip)

print("== 2. 时间戳格式 ==")
fmt = body("_fmtStamp")
now = body("_nowStamp")
pick = body("_pickManualAt")
check("_fmtStamp 把 ISO 的 T 换成空格", "s.replace('T', ' ')" in fmt)
check("_fmtStamp 截到 19 位（YYYY-MM-DD HH:MM:SS）", "substring(0, 19)" in fmt)
check("_nowStamp 本地时间补零拼成 YYYY-MM-DD HH:MM:SS",
      all(k in now for k in ["getFullYear()", "getMonth() + 1", "getDate()", "getHours()", "getMinutes()", "getSeconds()"]))
check("_pickManualAt 兼容顶层与 data 嵌套的回传字段",
      "res.manual_status_at" in pick and "res.data && res.data.manual_status_at" in pick)

print("== 3. 标签挂 title（订单行 + 文件行） ==")
check("订单行状态标签 title 走 _statusTip(o, o.status)",
      'title="' + "' + esc(_statusTip(o, o.status)) + '" in src)
check("文件行状态标签 title 走 _statusTip(f, f.status)",
      'title="' + "' + esc(_statusTip(f, f.status)) + '" in src)
check("title 经过 esc() 转义（防引号截断/注入）",
      re.search(r"esc\(_statusTip\([^)]*\)\)", src) is not None)

print("== 4. timeout=超时（标签 / 色板 / 异常分组 / 筛选 / 深色可读） ==")
check("STATUS_LABELS 补 timeout: '超时'", re.search(r"timeout:\s*'超时'", src) is not None)
check("STATUS_CLASSES 补 timeout: 'st-timeout'", "timeout: 'st-timeout'" in src)
check("CHART_COLORS.status 补 timeout 琥珀色 #F59E0B", "timeout: '#F59E0B'" in src)
check("_ABNORMAL_STATUS 把 timeout 计入异常", re.search(r"_ABNORMAL_STATUS = \{[^}]*timeout: 1", src) is not None)
check("_statusGroups 的 abnormal 求和含 timeout", re.search(r"abnormal = [^;]*timeout", src) is not None)
check(".tag.st-timeout 浅色主题琥珀底", re.search(r"\.tag\.st-timeout \{[^}]*#FFF8E1", src) is not None)
check(".tag.st-timeout 深色主题可读（半透明琥珀底 + 亮字）",
      re.search(r'\[data-theme="dark"\] \.tag\.st-timeout \{[^}]*#FBBF24', src) is not None)
check(".status-dot.timeout 圆点琥珀色", re.search(r"\.status-dot\.timeout \{[^}]*#F59E0B", src) is not None)
check("筛选下拉静态补 timeout 项（动态列表没有它时入口仍在）",
      re.search(r"if \(!statusCount\.timeout\)[^\n]*value=\"timeout\"", src) is not None)

print("== 5. 人工订正白名单不变 ==")
m_list = re.search(r"ORDER_FIXABLE_STATUSES = \[([^\]]*)\]", src)
fe = set(re.findall(r"'([a-z_]+)'", m_list.group(1))) if m_list else set()
check("ORDER_FIXABLE_STATUSES 仍是 5 个终态", fe == {"sent", "failed", "abandoned", "canceled", "rejected"}, str(sorted(fe)))
check("timeout 不在人工订正白名单里", "timeout" not in fe)
check("applyOrderStatus 仍先过白名单校验", "ORDER_FIXABLE_STATUSES.indexOf(status) < 0" in src)

print("== 6. 标记人（成员名单 / 记忆 / 空名单 / body） ==")
sel = body("_manualBySelectHTML")
by_fn = body("_manualStatusBy")
check("标记人取收支结算的成员名单（getMembers().name），不用云端昵称",
      "getMembers().map" in body("_memberNames") and ".name" in body("_memberNames"))
check("localStorage 记住上次选择，失效则退回第一个成员",
      "finance.manualStatusBy.v1" in src and "names.indexOf(saved) >= 0 ? saved : names[0]" in by_fn)
check("成员名单为空时下拉禁用并显示「未设置标记人」",
      "未设置标记人" in sel and "disabled" in sel)
check("订正 body 带 by（后端据此写 manual_status_by / manual_status_at）",
      "body: JSON.stringify({ order_id: String(oid), status: status, by: by })" in src)
check("成功后本地同步 manual_status_by / manual_status_at（tooltip 立刻生效）",
      re.search(r"o\.manual_status_by = by;\s*\n\s*o\.manual_status_at = _pickManualAt\(res\) \|\| _nowStamp\(\);", src) is not None)
check("标记人下拉在订单明细工具条里（与状态筛选同一排）",
      re.search(r"<span class=\"st-by\">标记人' \+ _manualBySelectHTML\(\)", src) is not None)

print("== 7. 其余硬规矩 ==")
check("整页没有 alert(", "alert(" not in src)
check("不出现 markdown 星号包裹的 tooltip 文案", re.search(r"title=\"\*\*", src) is None)

print()
print("=" * 52)
print("结算页状态 Tooltip / timeout / 标记人：全部通过 ✅" if ok_all
      else "结算页状态 Tooltip 验收：存在失败项 ❌")
sys.exit(0 if ok_all else 1)
