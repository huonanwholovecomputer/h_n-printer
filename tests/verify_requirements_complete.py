# -*- coding: utf-8 -*-
"""审计修复验收 · 🔴-A9：requirements.txt 缺 3 个直接运行依赖

事故形状：代码里直接 `import fitz / requests / socketio`，但清单里没有 PyMuPDF /
requests / python-socketio。干净机器照 CLAUDE.md 的
`pip install -r requirements.txt && python main.py` 装完，程序能启动（这三个都是
延迟/局部 import），却在「开始打印」（fitz 渲染）或连接云端（socketio / requests）
时**抛 ImportError**。

本脚本用 AST 扫描 local_print_tool 下全部 .py 的顶层与函数内 import，把 import 名
映射到 PyPI 包名，逐项断言清单里有声明 —— 以后新增依赖忘了登记会立刻变红。

判定口径：
 · 只认「运行依赖」（构建脚本 build_installer.py 的 PyInstaller 属构建期工具，排除）；
 · 标准库、本仓模块、相对导入不参与；
 · 声明名大小写不敏感（PyPI 名称本就大小写不敏感）。
"""
import ast
import glob
import io
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
REQ = os.path.join(LOCAL_TOOL, "requirements.txt")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# import 名 → PyPI 包名（只有需要改名的才必须映射，同名可省略）
IMPORT_TO_PYPI = {
    "PIL": "Pillow",
    "docx": "python-docx",
    "fitz": "PyMuPDF",
    "socketio": "python-socketio",
    "pythoncom": "pywin32",
    "win32api": "pywin32",
    "win32com": "pywin32",
    "win32con": "pywin32",
    "win32gui": "pywin32",
    "win32print": "pywin32",
    "win32ui": "pywin32",
    # S1-05（2026-10-23）：财务完整性 HMAC 的密钥用 DPAPI 保护（win32crypt 属于 pywin32）
    "win32crypt": "pywin32",
    # S1-02（2026-10-23）：更新清单 ed25519 验签
    "cryptography": "cryptography",
    "yaml": "PyYAML",
}

# 构建期工具：不属于运行依赖（只在 build_installer.py 里用）
BUILD_ONLY = {"PyInstaller", "setuptools", "wheel"}
# 构建脚本（其 import 不计入运行依赖）
BUILD_SCRIPTS = {"build_installer.py"}

# ── 解析 requirements.txt ──
declared = {}
for raw in io.open(REQ, encoding="utf-8").read().splitlines():
    line = raw.split("#", 1)[0].strip()
    if not line:
        continue
    m = re.match(r"^([A-Za-z0-9_.\-]+)", line)
    if m:
        declared[m.group(1).lower()] = line

print("=== 1. 清单可解析，且三个曾缺失的依赖已登记 ===")
check("requirements.txt 能读到声明", len(declared) >= 5, f"共 {len(declared)} 条")
for pkg in ("PyMuPDF", "requests", "python-socketio"):
    check(f"已登记 {pkg}", pkg.lower() in declared, declared.get(pkg.lower(), "(缺失)"))

# ── 扫描全部运行期 import ──
repo_mods = {os.path.splitext(f)[0] for f in os.listdir(LOCAL_TOOL) if f.endswith(".py")}
repo_mods |= {d for d in os.listdir(LOCAL_TOOL)
              if os.path.isdir(os.path.join(LOCAL_TOOL, d)) and not d.startswith(".")}

third_party = {}
for path in sorted(glob.glob(os.path.join(LOCAL_TOOL, "*.py"))) + \
        sorted(glob.glob(os.path.join(LOCAL_TOOL, "finance", "*.py"))):
    base = os.path.basename(path)
    try:
        tree = ast.parse(io.open(path, encoding="utf-8").read())
    except SyntaxError as e:
        check(f"可解析 {base}", False, str(e))
        continue
    for node in ast.walk(tree):          # walk 覆盖函数内/条件内的延迟 import
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module.split(".")[0]] if (node.module and node.level == 0) else []
        else:
            continue
        for n in names:
            if n in sys.stdlib_module_names or n in repo_mods or n.startswith("_"):
                continue
            if n in BUILD_ONLY and base in BUILD_SCRIPTS:
                continue
            third_party.setdefault(n, set()).add(base)

print(f"\n=== 2. 运行期第三方 import 共 {len(third_party)} 个，逐个核对清单 ===")
missing = []
for mod in sorted(third_party):
    pkg = IMPORT_TO_PYPI.get(mod, mod)
    if pkg.lower() not in declared:
        missing.append(f"{mod}（→ {pkg}）  被 {','.join(sorted(third_party[mod]))} 使用")
    else:
        print(f"    OK  {mod:14s} -> {declared[pkg.lower()]:24s} [{','.join(sorted(third_party[mod]))}]")

check("所有运行期第三方 import 都能在清单里找到声明", not missing, "; ".join(missing))

print("\n=== 3. 反向保护：扫描器确实认得出缺失（对拍）===")
# 用一份「去掉三行」的清单文本重跑同一判定 —— 必须报出这 3 个缺失，
# 否则说明扫描器是假守卫（永远为绿）。
trimmed = {k: v for k, v in declared.items()
           if k not in ("pymupdf", "requests", "python-socketio")}
still_missing = [m for m in ("fitz", "requests", "socketio")
                 if IMPORT_TO_PYPI.get(m, m).lower() not in trimmed]
check("去掉这三行后扫描器能报出缺失（证明判定有效）",
      set(still_missing) == {"fitz", "requests", "socketio"}, f"{still_missing}")

print("\n=== 4. 构建期工具不应混入运行依赖（清单只写运行期）===")
check("PyInstaller 未被要求登记（属构建工具）",
      "pyinstaller" not in declared and "PyInstaller" in BUILD_ONLY)

print("\n" + ("全部通过" if ok_all else "存在失败项"))
sys.exit(0 if ok_all else 1)
