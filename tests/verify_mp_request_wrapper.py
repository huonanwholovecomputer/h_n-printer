# -*- coding: utf-8 -*-
"""验收 · 🟡-D5 第 7 条：小程序页面不得绕过 utils/request（否则 401 不续登）

缺陷（审查报告 D5）：`pages/authorized-users/authorized-users.js` 两处直接 `wx.request()`，
**绕过** `utils/request.js` 的「401 → 静默重登录 → 重放原请求」封装。token 7 天过期后，
这一页的请求永远 401：既不续登、也不引导重新登录，页面永远打不开（别的页都正常，
用户只会觉得"这一页坏了"）。

本脚本钉住的是一条例外名单为空的不变量（防以后再有人图省事直连）：
  ① 小程序前端（`mobile_apps/h_n_print/**`）里，`wx.request(` 只允许出现在
     `utils/request.js`（封装本体）中；
  ② 扫描面要有"存活下限"（扫不到文件即判失败，避免判定器失效后静默变绿）；
  ③ `authorized-users.js` 必须 `require('../../utils/request')` 并用 `request({` 发请求。

外加**变异自证**：往页面源码里注一处 `wx.request(` → 必判违规；把文件集合清空 → 必判失败。

用法: python tests/verify_mp_request_wrapper.py     （exit 0 = 全绿）
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MP = os.path.join(REPO, "mobile_apps", "h_n_print")
WRAPPER = os.path.join(MP, "utils", "request.js")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def collect_js(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("node_modules", "miniprogram_npm")]
        for fn in filenames:
            if fn.endswith(".js"):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def strip_comment_lines(text):
    """逐行把 `//` 之后置等长空格（判据不吃注释）。"""
    out = []
    for line in text.split("\n"):
        i = line.find("//")
        out.append(line if i < 0 else line[:i] + " " * (len(line) - i))
    return "\n".join(out)


def audit(files, contents):
    """返回违规清单（空 = 合规）。contents: {path: text}。"""
    problems = []
    if len(files) < 10:
        problems.append(f"扫描面异常：只扫到 {len(files)} 个 js（判定器可能已失效，判失败）")
    for p in files:
        if os.path.normcase(p) == os.path.normcase(WRAPPER):
            continue
        text = strip_comment_lines(contents.get(p, ""))
        hits = [i + 1 for i, ln in enumerate(text.split("\n")) if "wx.request(" in ln]
        if hits:
            problems.append(f"{os.path.relpath(p, REPO)} 直接 wx.request（行 {hits}）—— "
                            f"必须走 utils/request 才有 401 续登")
    return problems


files = collect_js(MP)
contents = {}
for p in files:
    try:
        contents[p] = open(p, encoding="utf-8").read()
    except OSError:
        contents[p] = ""

print("=== A. 全量扫描小程序前端 ===")
print(f"扫描 {len(files)} 个 js（封装本体：{os.path.relpath(WRAPPER, REPO)}）")
_problems = audit(files, contents)
check("★ 除 utils/request.js 外，没有任何页面直接 wx.request", not _problems,
      "；".join(_problems))
check("扫描面存活下限（≥10 个 js）", len(files) >= 10, f"{len(files)}")

print("=== B. authorized-users 页接线 ===")
_au = os.path.join(MP, "pages", "authorized-users", "authorized-users.js")
_au_src = contents.get(_au, "")
check("★ 该页 require 了 utils/request", "require('../../utils/request')" in _au_src)
check("★ 该页用 request({ 发请求（两处都已改）", _au_src.count("request({") == 2,
      f"实际 {_au_src.count('request({')} 处")
check("该页已无 wx.request", "wx.request(" not in strip_comment_lines(_au_src))
check("封装本体仍用 wx.request（没把底层也改坏）",
      "wx.request(" in contents.get(WRAPPER, ""))

print("\n=== C. 变异自证（判据必须报违规）===")
_mut = dict(contents)
_mut[_au] = contents[_au] + "\n// 变异\nfunction _evil() { wx.request({ url: 'x' }) }\n"
check("C1 页面里再注一处 wx.request → 必判违规（注释里的不算）",
      bool(audit(files, _mut)))
_mut2 = dict(contents)
_mut2[WRAPPER] = contents[WRAPPER] + "\n// 变异：底层加一处\nwx.request({ url: 'y' })\n"
check("C2 变异落在封装本体（豁免文件）→ 仍应合规", not audit(files, _mut2))
check("C3 扫描面清空 → 必判失败（防判定器失效后静默变绿）",
      bool(audit([], {})))
_comment_only = "\n// 说明：这里以前是 wx.request({...})，已改为 request({...})\n"
_mut3 = dict(contents)
_mut3[_au] = contents[_au] + _comment_only
check("C4 只在注释里提到 wx.request( → 不算违规（判据不吃注释）", not audit(files, _mut3))

print("\n" + "=" * 60)
print("小程序请求封装不变量:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
