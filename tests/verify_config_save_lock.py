# -*- coding: utf-8 -*-
"""验收 · 🟡-D5 第 14 条：配置文件落盘必须只有一个入口，且在锁内

缺陷（审查报告 D5）：`gui.py` 里有 **10 处** 直接写 `self._config.save(self._config_path)`，
全部**绕过** `_save_config()` 里的 `_config_save_lock`。而价格同步、成员名单同步、本地订单
上传等**后台线程**会同时调用 `_save_config()` —— 两个线程同时写同一个 JSON 文件，
可能落盘出**半截文件**（截断式写入：写到一半被另一个写者覆盖）。

修复后本脚本钉住的不变量（用 AST，不受注释/字符串干扰）：
  ① 整个模块里 `self._config.save(...)` 只出现 **1 次**；
  ② 那一次必须在 `_save_config` 函数体内；
  ③ 而且必须落在 `with self._config_save_lock:` 块里。

外加**变异自证**：故意造出"别处又直调一次"和"把锁去掉"两份源码，判定必须报违规 ——
否则这类"单入口"判据很容易写成永远为绿。

用法: python tests/verify_config_save_lock.py     （exit 0 = 全绿）
"""
import ast
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUI = os.path.join(REPO, "local_print_tool", "gui.py")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def _is_config_save_call(node) -> bool:
    """`self._config.save(...)` 形态。"""
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "save"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "_config"
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "self")


def audit(source: str) -> list:
    """返回违规清单（空 = 合规）。抽成函数以便变异自证。"""
    problems = []
    tree = ast.parse(source)

    save_calls = [n for n in ast.walk(tree) if _is_config_save_call(n)]
    if len(save_calls) != 1:
        problems.append(f"self._config.save(…) 出现 {len(save_calls)} 次（应恰好 1 次，即锁内那次）")

    save_fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "_save_config")
                   if isinstance(tree, ast.Module) else None, None)
    if save_fn is None:
        # 类方法：在 ClassDef 里找
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, ast.FunctionDef) and sub.name == "_save_config":
                        save_fn = sub
    if save_fn is None:
        problems.append("找不到 MainWindow._save_config（单入口不复存在）")
        return problems

    fn_calls = [n for n in ast.walk(save_fn) if _is_config_save_call(n)]
    if len(fn_calls) != 1:
        problems.append(f"_save_config 内落盘调用 {len(fn_calls)} 次（应恰好 1 次）")

    # 落盘必须在 `with self._config_save_lock:` 块内
    locked_ok = False
    for node in ast.walk(save_fn):
        if isinstance(node, ast.With):
            items_src = [ast.unparse(it.context_expr) for it in node.items]
            if any("_config_save_lock" in s for s in items_src):
                if any(_is_config_save_call(sub) for sub in ast.walk(node)):
                    locked_ok = True
    if not locked_ok:
        problems.append("落盘调用不在 `with self._config_save_lock:` 块内（并发写会写坏文件）")

    # 反向：单入口之外的别处不许再有直调（len(save_calls)==1 已覆盖，这里给更明确的诊断）
    outside = [n for n in save_calls if not any(n is sub for sub in fn_calls)]
    if outside:
        problems.append(f"有 {len(outside)} 处直调绕过 _save_config（行号 "
                        f"{[getattr(n, 'lineno', '?') for n in outside]}）")
    return problems


src = open(GUI, encoding="utf-8").read()

print("=== A. 现行源码 ===")
_problems = audit(src)
check("★ `self._config.save(…)` 全模块只出现 1 次且落在 `_save_config` 的锁内",
      not _problems, "；".join(_problems))

_tree = ast.parse(src)
_n_save = sum(1 for n in ast.walk(_tree) if _is_config_save_call(n))
check("AST 计数确认：直调数为 1", _n_save == 1, f"{_n_save}")
check("_config_save_lock 在 _save_config 里被使用（锁没被误删）",
      "with self._config_save_lock:" in src)
check("后台调用点仍显式传 sync_ui=False（不读 QWidget，避免跨线程碰控件）",
      src.count("_save_config(sync_ui=False)") >= 10,
      f"实际 {src.count('_save_config(sync_ui=False)')} 处")

print("\n=== B. 变异自证（判据必须报违规，否则是假守卫）===")
_mut1 = src.replace("            with self._config_save_lock:",
                    "            if True:  # 变异：去掉锁", 1)
check("B1 去掉 `with self._config_save_lock:` → 必报违规",
      _mut1 != src and bool(audit(_mut1)))

_mut2 = src + "\n\ndef _evil(self):\n    self._config.save(self._config_path)\n"
check("B2 别处再加一处直调 → 必报违规", bool(audit(_mut2)))

_mut3 = src.replace("_save_config(sync_ui=False)", "_save_config()")
check("B3 后台调用点改回不传 sync_ui（会去读控件）→ 判据仍应通过（该条只钉直调与锁）",
      not audit(_mut3))

_mut4 = src.replace("    def _save_config(self, sync_ui: bool | None = None):",
                    "    def _save_config_disabled(self, sync_ui: bool | None = None):", 1)
check("B4 把 _save_config 改名（入口消失）→ 必报违规", _mut4 != src and bool(audit(_mut4)))

print("\n" + "=" * 60)
print("配置落盘单入口验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
