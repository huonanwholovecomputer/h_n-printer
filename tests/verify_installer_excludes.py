# -*- coding: utf-8 -*-
"""验收 · 🔴-D2 安装包 `Excludes` 必须覆盖**全部**用户数据项（且不得靠注释自证）

背景（审查报告 D1/D2）：`installer/HN打印工具.iss` 的 `Excludes` 只有 8 项，
`file_cache`（`staging.py` 的"工作副本"，装的是**顾客上传的原始文档**）、
`duplex_stack`（攒批账本：合并 PDF + 含订单号的 stack.json）、
仓库根 `print_data.json`、`.processed_tasks.json` 都不在名单里。

**当下真正拦住它们的是 `.spec` 的 `datas` 白名单**（实测：`datas` 是显式列表，
上面那些根本没被打进 `dist/`）—— 但 `.iss` 的注释却把它写成"必须排除…否则会把
开发机的服务器地址 + 打印机 token 分发给所有安装者"，把**当前无效的第二道防线
当成主防线**。一旦有人放宽 `datas`（例如 `datas=[('*','.')]`），
顾客文件 + `print_config.json`（含服务器地址与 printer token）会一起进安装包。

本脚本钉住两件事（两侧都做集合比对，不靠注释）：
  A. `.iss` 的 `Excludes` ⊇ `paths.py` 里 `get_app_data_dir()` 下的**全部**用户数据项
     （含 `migrate_legacy_data()` 的旧版文件名清单）—— 清单由 **AST 从 paths.py 推导**，
     不是硬编码，将来新增一个用户数据目录会自动被这条守卫抓住；
  B. `.spec` 的 `datas`（白名单）里**不得出现任何**用户数据项 —— `theme_settings.json`
     曾同时出现在两边（自相矛盾），本脚本把它钉死。

两条检查都**自带对拍**：把清单里的一项从输入里去掉 / 把用户数据塞回 datas，
检查器必须报错。没有对拍的话，它就是一个"永远为绿"的假守卫。

用法：python tests/verify_installer_excludes.py     （exit 0 = 全部通过）
"""
import ast
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATHS_PY = os.path.join(REPO, "local_print_tool", "paths.py")
ISS = os.path.join(REPO, "local_print_tool", "installer", "HN打印工具.iss")
SPECS = [os.path.join(REPO, "local_print_tool", "HN打印工具.spec"),
         os.path.join(REPO, "local_print_tool", "HN打印工具_debug.spec")]
SPEC = SPECS[0]

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ============================================================
# 清单来源：AST 推导 paths.py（唯一真相），不是硬编码
# ============================================================

def _fn_name(call):
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def inventory_from_paths(src_text):
    """从 paths.py 推导"用户数据项"清单（%APPDATA%\\HN打印工具 下的文件名/目录名）。

    来源两处：
      · 所有 `os.path.join(get_app_data_dir(), "<名字>", ...)` 的第一段；
      · `migrate_legacy_data()` 里那个旧版文件名元组（旧版数据散在程序目录，
        正是要被 Excludes 拦住的东西）。
    返回 (set, errors)。
    """
    errors = []
    try:
        tree = ast.parse(src_text)
    except SyntaxError as e:
        return set(), [f"paths.py 无法解析: {e}"]

    items = set()
    joins = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _fn_name(node) == "join" and len(node.args) >= 2):
            continue
        head, seg = node.args[0], node.args[1]
        if not (isinstance(head, ast.Call) and _fn_name(head) == "get_app_data_dir"):
            continue
        joins += 1
        if isinstance(seg, ast.Constant) and isinstance(seg.value, str):
            items.add(seg.value)
        else:
            errors.append(f"get_app_data_dir() 的第 2 段不是字符串常量（第 {node.lineno} 行）")

    legacy = set()
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "migrate_legacy_data"), None)
    if fn is None:
        errors.append("paths.py 里找不到 migrate_legacy_data()（旧版数据清单来源消失）")
    else:
        for node in ast.walk(fn):
            if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple):
                legacy |= {e.value for e in node.iter.elts
                           if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    if not joins:
        errors.append("paths.py 里一个 get_app_data_dir() 路径都没解析到 —— 推导失效")
    return items | legacy, errors


def excludes_from_iss(iss_text):
    """解析 [Files] 里 Source 行的 Excludes 列表 → 名字集合（按 basename 归一）。"""
    m = re.search(r"Excludes:\s*\"([^\"]*)\"", iss_text)
    if not m:
        return None
    return {os.path.basename(x.strip().replace("\\", "/"))
            for x in m.group(1).split(",") if x.strip()}


def find_uncovered(iss_text, inventory):
    """Excludes 未覆盖的用户数据项（返回 (excludes, missing)；解析失败 → (None, [原因])）。"""
    ex = excludes_from_iss(iss_text)
    if ex is None:
        return None, ["解析不到 Excludes 列表（Source 行的 Excludes: 段被删/改写）"]
    return ex, sorted(inventory - ex)


def find_shipped_userdata(spec_text, inventory):
    """`.spec` 的 datas 白名单里出现的用户数据项（不该有任何一项）。"""
    m = re.search(r"datas\s*=\s*\[(.*?)\]\s*,", spec_text, re.S)
    if m is None:
        return ["解析不到 datas 列表"]
    bad = []
    for raw in re.findall(r"\(\s*'([^']+)'\s*,", m.group(1)):
        base = os.path.basename(raw.replace("\\", "/"))
        if base in inventory:
            bad.append(f"datas 里打包了用户数据 {raw!r}（{base}）")
    return bad


# ============================================================
# A. 推导 + 集合比对
# ============================================================
paths_src = open(PATHS_PY, encoding="utf-8").read()
iss_src = open(ISS, encoding="utf-8").read()
spec_src = open(SPEC, encoding="utf-8").read()

INVENTORY, inv_errors = inventory_from_paths(paths_src)
print("=== A. Excludes 必须覆盖全部用户数据项（清单 AST 推导自 paths.py）===")
check("能从 paths.py 推导出用户数据清单", not inv_errors, "；".join(inv_errors))
print(f"     推导到的用户数据项（{len(INVENTORY)} 项）: {', '.join(sorted(INVENTORY))}")

# 这几个是现场问题的名字，必须在清单里（防止推导器半途失效后"集合为空 → 全绿"）
for must in ("print_config.json", "file_cache", "duplex_stack", "print_data.json",
             ".processed_tasks.json", "logs", "pdf_cache"):
    check(f"清单里含 `{must}`（推导器没漏项）", must in INVENTORY)

EXCLUDES, missing = find_uncovered(iss_src, INVENTORY)
check("能解析到 .iss 的 Excludes 列表", EXCLUDES is not None, "；".join(missing))
print(f"     .iss Excludes（{len(EXCLUDES or ())} 项）: {', '.join(sorted(EXCLUDES or ()))}")
check("★ .iss Excludes ⊇ 全部用户数据项（含 file_cache / duplex_stack）",
      not missing, "未覆盖: " + ", ".join(missing))
for key in ("file_cache", "duplex_stack", "print_data.json"):
    check(f"★ Excludes 含 `{key}`", EXCLUDES is not None and key in EXCLUDES)

check("Excludes 仍保留旧版 layout 的 finance\\print_data.json（覆盖历史形态）",
      EXCLUDES is not None and "print_data.json" in EXCLUDES
      and "finance\\print_data.json" in iss_src)
check("Excludes 仍排除 print_config.json（含服务器地址 + printer token）",
      EXCLUDES is not None and "print_config.json" in EXCLUDES)

# 注释必须说清"谁是主防线、谁是第二道"
check("★ .iss 注释说明 Excludes 是**第二道防线**（不再自称主防线）",
      "第二道防线" in iss_src)
check("★ .iss 注释点明主防线是 .spec 的 datas 白名单",
      re.search(r"白名单", iss_src) is not None)

# ============================================================
# B. .spec 的 datas 白名单里不得有用户数据（**两份 spec 都查**）
# ============================================================
print("\n=== B. .spec 的 datas 白名单不得打包用户数据（release + debug）===")
spec_bad = find_shipped_userdata(spec_src, INVENTORY)
for _p in SPECS[1:]:
    _t = open(_p, encoding="utf-8").read()
    check(f"能解析 {os.path.basename(_p)} 的 datas", find_shipped_userdata(_t, INVENTORY) != ["解析不到 datas 列表"])
    spec_bad += [f"{os.path.basename(_p)}: {x}" for x in find_shipped_userdata(_t, INVENTORY)]
check("★ 两份 spec 的 datas 里都没有任何用户数据项（theme_settings.json 自相矛盾已消除）",
      not spec_bad, "；".join(spec_bad))
check("datas 仍包含程序自带资源（styles_dark.qss / finance/settlement.html）",
      "styles_dark.qss" in spec_src and "'finance/settlement.html'" in spec_src)
check("spec 注释仍写明不打包 print_config.json 与 finance/print_data.json",
      "刻意不打包 print_config.json" in spec_src and "不打包 finance/print_data.json" in spec_src)

# ============================================================
# C. 对拍：两个检查器都必须真的会报错（否则是假守卫）
# ============================================================
print("\n=== C. 检查器对拍（喂进坏输入，必须报错）===")

_iss_no_fcache = re.sub(r"file_cache,", "", iss_src, count=1)
_, miss1 = find_uncovered(_iss_no_fcache, INVENTORY)
check("★ 对拍：Excludes 去掉 file_cache → 必须报未覆盖", "file_cache" in miss1, str(miss1))

_iss_no_excl = re.sub(r"Excludes:\s*\"[^\"]*\"", "", iss_src)
_ex, miss2 = find_uncovered(_iss_no_excl, INVENTORY)
check("★ 对拍：整段 Excludes 被删 → 必须报「解析不到」", _ex is None and bool(miss2), str(miss2))

# 模拟"将来有人新增一个用户数据目录，但忘了加进 Excludes"
_new_paths = paths_src.replace(
    'def logs_dir() -> str:',
    'def something_cache_dir() -> str:\n'
    '    d = os.path.join(get_app_data_dir(), "something_cache")\n'
    '    return d\n\n\n'
    'def logs_dir() -> str:', 1)
_inv2, _err2 = inventory_from_paths(_new_paths)
_, miss3 = find_uncovered(iss_src, _inv2)
check("★ 对拍：paths.py 新增用户数据目录 → 必须被推导出来并报未覆盖",
      "something_cache" in _inv2 and "something_cache" in miss3,
      f"清单含={('something_cache' in _inv2)} 未覆盖={miss3}")

_spec_bad = spec_src.replace(
    "('HN_printer.png', '.'),",
    "('HN_printer.png', '.'), ('theme_settings.json', '.'),", 1)
bad2 = find_shipped_userdata(_spec_bad, INVENTORY)
check("★ 对拍：把 theme_settings.json 塞回 datas → 必须报违规",
      any("theme_settings.json" in x for x in bad2), str(bad2))

_spec_bad2 = spec_src.replace(
    "('HN_printer.png', '.'),",
    "('HN_printer.png', '.'), ('finance/print_data.json', 'finance'),", 1)
bad3 = find_shipped_userdata(_spec_bad2, INVENTORY)
check("★ 对拍：把 privacy 数据 finance/print_data.json 塞回 datas → 必须报违规",
      any("print_data.json" in x for x in bad3), str(bad3))

check("★ 对拍：正常 .iss / .spec 不误报（两个检查器都返回空）",
      not find_uncovered(iss_src, INVENTORY)[1] and not find_shipped_userdata(spec_src, INVENTORY))

print("\n" + "=" * 56)
print("安装包排除清单验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
