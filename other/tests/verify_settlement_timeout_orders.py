# -*- coding: utf-8 -*-
"""验收 · 收支清算的取数清单必须含 timeout（2026-11-12 现场 HN20261112-0023）

被验对象：
  · `local_print_tool/finance/settlement.html` 的 `selectedStatuses()` 与两处取数 URL
  · `mobile_apps/printer-backend/app.py` 的 `admin_statistics_revenue()` 默认白名单

现场：订单 HN20261112-0023（云端 status='timeout'，21:25:05 开始打印后设备一直没回报完成）
在手机端「我 → 订单」里有、在本地工具标签页里有，偏偏在 **收支清算 → 订单明细** 里凭空消失。

根因链（实测，见 docs/收支清算与结算页.md）：
  ① 页面把状态清单拼进 URL：`...&status=sent,printing,queued,failed,...`；
  ② 后端 `admin_statistics_revenue()` 只要收到 status= 就**只按这份清单**过滤
     （没收到才用后端自己的默认白名单，那个白名单是含 timeout 的）；
  ③ 而页面的 `selectedStatuses()` 硬编码清单**漏了 timeout** ⇒ 超时单永远取不回来
     ⇒ 明细 / KPI / 趋势 / 状态筛选全都看不到它（金额不计收益的口径是对的，但「看不见单」是错的）。

判据（四组）：
  A. 取数清单 —— `selectedStatuses()` 含 timeout，且**包含后端的全部默认状态**
     （后端以后再加状态而页面没跟上，这条会当场红 —— 这正是本次事故的形态）。
  B. 取数接线 —— `fetchScopeData` / `fetchCloudCompare` / `ensureUsage` 确实把这份清单拼进 URL。
  C. 展示与金额口径 —— timeout 归「异常组」（恒等式不塌）、在 `_EXCLUDE_STATUS` 里（不虚增收益）、
     有「超时」标签与静态兜底筛选项。
  D. 变异自证 —— 把页面清单里的 timeout 删掉、把后端默认白名单加一个页面没有的状态，
     对应判据必须当场转红（在内存里改字符串，不动仓库文件）。

用法：python other/tests/verify_settlement_timeout_orders.py
exit 0 = 全部通过。
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PAGE = os.path.join(REPO, "local_print_tool", "finance", "settlement.html")
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend", "app.py")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(t):
    print("\n=== " + t + " ===")


def func_body(src, name):
    """抠出 `function name(...) { ... }` 的函数体（按花括号配平，字符串里的括号也带着走）。"""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        return None
    i = src.find("{", m.end())
    if i < 0:
        return None
    depth, j, quote = 0, i, ""
    while j < len(src):
        c = src[j]
        if quote:
            if c == "\\":
                j += 2
                continue
            if c == quote:
                quote = ""
        elif c in "'\"`":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[i + 1:j]
        j += 1
    return None


def page_statuses(src):
    """从 selectedStatuses() 的 return [...] 里取出状态名列表。"""
    body = func_body(src, "selectedStatuses")
    if body is None:
        return None
    m = re.search(r"return\s*\[(.*?)\]\s*;", body, re.S)
    if not m:
        return None
    return [x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()]


def backend_default_statuses(src):
    """后端 admin_statistics_revenue() 的默认白名单（else 分支那份带引号的字面量）。"""
    m = re.search(r'allowed_statuses\s*=\s*\[("(?:sent|printing)[^\]]*)\]', src, re.S)
    if not m:
        return None
    return re.findall(r'"([a-z_]+)"', m.group(1))


SRC = open(PAGE, encoding="utf-8").read()
APP = open(BACKEND, encoding="utf-8").read()
PAGE_ST = page_statuses(SRC) or []
BE_ST = backend_default_statuses(APP) or []

# ══════════════════════════════════════════════════════════════
# A. 取数清单
# ══════════════════════════════════════════════════════════════
section("A. 取数清单（selectedStatuses 必须覆盖后端默认状态）")

check("能解析出页面的状态清单（selectedStatuses 的 return 不是空数组）",
      len(PAGE_ST) >= 8, f"{PAGE_ST}")
check("能解析出后端默认白名单", len(BE_ST) >= 5, f"{BE_ST}")
check("★ selectedStatuses() 含 timeout（2026-11-12 超时单消失的直接原因）",
      "timeout" in PAGE_ST, f"{PAGE_ST}")
missing = [s for s in BE_ST if s not in PAGE_ST]
check("★ 页面清单是后端默认白名单的超集（后端加状态、页面没跟上 ⇒ 这里红）",
      not missing, f"页面缺：{missing}；页面={PAGE_ST}；后端默认={BE_ST}")

# ══════════════════════════════════════════════════════════════
# B. 取数接线（清单真的被拼进 URL）
# ══════════════════════════════════════════════════════════════
section("B. 取数接线（清单真的进了请求 URL）")

for fn in ("fetchScopeData", "fetchCloudCompare"):
    body = func_body(SRC, fn) or ""
    check(f"{fn} 用 selectedStatuses() 拼 status=",
          "var statuses = selectedStatuses();" in body
          and "'&status=' + encodeURIComponent(statuses.join(','))" in body,
          f"函数体 {len(body)} 字符")

usage = func_body(SRC, "ensureUsage") or ""
check("ensureUsage（废纸预估口径）同样用 selectedStatuses().join(',')",
      "encodeURIComponent(selectedStatuses().join(','))" in usage,
      f"函数体 {len(usage)} 字符")

# ══════════════════════════════════════════════════════════════
# C. 展示与金额口径（取回来之后各分支都能正确对待 timeout）
# ══════════════════════════════════════════════════════════════
section("C. 展示与金额口径")

m_ab = re.search(r"var\s+_ABNORMAL_STATUS\s*=\s*\{(.*?)\};", SRC, re.S)
check("timeout 在「异常组」_ABNORMAL_STATUS 里（总数 = 完成+进行中+异常+取消 的恒等式不塌）",
      bool(m_ab) and "timeout: 1" in m_ab.group(1), (m_ab.group(1).strip() if m_ab else "没找到"))
m_ex = re.search(r"var\s+_EXCLUDE_STATUS\s*=\s*\{(.*?)\};", SRC, re.S)
check("timeout 在 _EXCLUDE_STATUS 里（没出纸就不算钱，与后端 revenue 口径一致）",
      bool(m_ex) and "timeout: 1" in m_ex.group(1), (m_ex.group(1).strip() if m_ex else "没找到"))
check("STATUS_LABELS 里有「超时」",
      bool(re.search(r"timeout:\s*'超时'", SRC)))
check("状态筛选下拉有 timeout 的静态兜底项（当前区间没有超时单时入口仍在）",
      "if (!statusCount.timeout)" in SRC and ">超时 (0)</option>" in SRC)

# ══════════════════════════════════════════════════════════════
# D. 变异自证（内存里改字符串，判据必须转红）
# ══════════════════════════════════════════════════════════════
section("D. 变异自证")

body = func_body(SRC, "selectedStatuses") or ""
mut_body = body.replace("'timeout',", "", 1)
mut_src = SRC.replace(body, mut_body, 1) if body else SRC
mut_st = page_statuses(mut_src) or []
check("★ 变异①：把清单里的 'timeout' 删掉 ⇒ A 的 timeout 判据转红",
      "timeout" not in mut_st and "timeout" in PAGE_ST,
      f"变异后={mut_st}")

mut_app = APP.replace('["sent", "printing", "queued", "failed", "timeout", "offline_unknown"]',
                      '["sent", "printing", "queued", "failed", "timeout", "offline_unknown", "refunded"]', 1)
mut_be = backend_default_statuses(mut_app) or []
check("★ 变异②：后端默认白名单多一个状态 ⇒ 超集判据转红",
      "refunded" in mut_be and [s for s in mut_be if s not in PAGE_ST] == ["refunded"],
      f"变异后后端={mut_be}")

print("\n" + ("全部通过 ✅（收支清算取数清单含 timeout / 超时单不再凭空消失）"
              if ok_all else "存在失败项 ❌"))
sys.exit(0 if ok_all else 1)
