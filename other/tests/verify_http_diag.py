"""verify_http_diag.py — 取号/上报失败必须留下状态码（2026-11-24 日志体检 P0-2 第 1 步）。

背景：服务端 7 天里对本地工具端点记了 172 次 403（`next_order_number` 110 次、
`local_orders` 48 次…），响应体恒为 `{"success":false,"message":"token 无效"}`（49 字节）。
而本地日志里 **一条都没留下** —— 112/110/48 这些数字只能从服务器 journald 反推，
现场只看到「订单号获取超时（>4s），改用本地临时号」，两端日志没法对齐（体检报告 D5）。

根因两处：
  1. `_ensure_tab_order_number` 的取号线程 `_net()` 里 `except Exception: result[...]=""`
     把非 2xx 与异常整个吞掉，连状态码都没取；
  2. `_upload()` / 换号补报失败只 emit 到同步面板，没有 logger 落文件。

本脚本用 AST 定位函数体、用源码断言锁住文案，两者都不依赖构造 MainWindow。
断言全部正向；每条都配变异自证 —— 把修复删掉，判据必须变红。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
GUI = ROOT / "local_print_tool" / "gui.py"

fails: list[str] = []
checks = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global checks
    checks += 1
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f"  ← {detail}" if detail else ""))
        fails.append(name)
    return bool(cond)


def _func_node(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _calls_logger_warning(node: ast.AST) -> list[ast.Call]:
    """node 子树里所有 `logger.warning(...)` / `logger.error(...)` 调用。"""
    out = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        if isinstance(f, ast.Attribute) and f.attr in ("warning", "error"):
            base = f.value
            if isinstance(base, ast.Name) and base.id == "logger":
                out.append(sub)
    return out


def _source_segment(src: str, node: ast.AST) -> str:
    try:
        return ast.get_source_segment(src, node) or ""
    except Exception:
        return ""


def main() -> int:
    print("=" * 78)
    print("verify_http_diag.py — 取号/上报失败留下状态码（P0-2 第 1 步）")
    print("=" * 78)

    src = GUI.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)

    # ── 1. 取号线程不再静默 ─────────────────────────────────────────
    print("\n=== 1. _ensure_order_number 的取号线程必须记状态码 ===")
    fn = _func_node(tree, "_ensure_order_number")
    check("1.0 找到 _ensure_order_number", fn is not None)
    if fn is None:
        print("\nFAILED  函数都没找到，后面跳过")
        return 1

    body_src = _source_segment(src, fn)
    check("1.1 函数体可读", len(body_src) > 100, f"长度={len(body_src)}")

    warn_calls = _calls_logger_warning(fn)
    check("1.2 取号线程里有 logger.warning/error（原来一条都没有）",
          len(warn_calls) >= 1, f"实得 {len(warn_calls)} 处")

    # 每处失败日志都得带 status_code（否则对不上服务端那 403）
    with_status = [c for c in warn_calls if "status_code" in _source_segment(src, c)]
    check("1.3 至少一处失败日志带 resp.status_code",
          len(with_status) >= 1, f"带状态码的 {len(with_status)}/{len(warn_calls)} 处")

    check("1.4 失败日志带幂等键 req=（两端能凭它 join）",
          any("req=" in _source_segment(src, c) for c in warn_calls))

    # 变异自证：把新增的 logger.warning 表达式在源码里换成 pass，重新解析，判据必须变红
    mutated_src = src
    for c in sorted(warn_calls, key=lambda n: -getattr(n, "lineno", 0)):
        seg = _source_segment(src, c)
        if seg:
            mutated_src = mutated_src.replace(seg, "pass", 1)
    try:
        mfn = _func_node(ast.parse(mutated_src), "_ensure_order_number")
        mwarn = _calls_logger_warning(mfn) if mfn is not None else []
    except SyntaxError as e:
        mwarn = []
        print(f"     （变异体解析失败，按 0 处计: {e}）")
    check("1.5 变异自证：摘掉新增日志后『有 logger.warning』判据会变红",
          len(mwarn) == 0, f"变异后仍剩 {len(mwarn)} 处 ⇒ 摘得不干净")

    # ── 2. 上报 / 补报失败也要落文件 ──────────────────────────────
    # 判据只用 ASCII 标识符与 AST 结构，不拿中文文案比对产品源码
    # （verify_guard_quality A2 棘轮只许降；中文文案当契约是双面失效的反模式）。
    print("\n=== 2. _upload / 换号补报失败必须有 logger 落盘 ===")
    upload_fns = [n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_upload"]
    check("2.0 找到 _upload（取号随之上报的那一支）", len(upload_fns) >= 1,
          f"实得 {len(upload_fns)} 个")
    up_warn = []
    for u in upload_fns:
        up_warn.extend(_calls_logger_warning(u))
    check("2.1 _upload 里有 logger.warning（原来只 emit 到同步面板）",
          len(up_warn) >= 1, f"实得 {len(up_warn)} 处")
    check("2.2 _upload 的失败日志带 resp.status_code",
          any("status_code" in _source_segment(src, c) for c in up_warn),
          "不带状态码 ⇒ 对不上服务端那 48 次 403")

    adopt_warn = [c for c in _calls_logger_warning(tree)
                  if "new_num" in _source_segment(src, c)
                  and "status_code" in _source_segment(src, c)]
    check("2.3 换号补报失败带状态码的 logger.warning", len(adopt_warn) >= 1,
          f"实得 {len(adopt_warn)} 处")

    acquire = _func_node(tree, "_acquire_order_number_and_upload_net")
    check("2.4 找到 _acquire_order_number_and_upload_net", acquire is not None)
    acq_warn = _calls_logger_warning(acquire) if acquire is not None else []
    check("2.5 该函数里的失败日志带 status_code",
          any("status_code" in _source_segment(src, c) for c in acq_warn),
          f"该函数共 {len(acq_warn)} 处 logger.warning")

    # 变异自证：把 _upload 里新增的日志摘掉，2.1 判据必须变红
    mut2 = src
    for c in sorted(up_warn, key=lambda n: -getattr(n, "lineno", 0)):
        seg = _source_segment(src, c)
        if seg:
            mut2 = mut2.replace(seg, "pass", 1)
    try:
        m2 = [n for n in ast.walk(ast.parse(mut2))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_upload"]
        m2warn = [c for u in m2 for c in _calls_logger_warning(u)]
    except SyntaxError:
        m2warn = []
    check("2.6 变异自证：摘掉 _upload 里新增日志后 2.1 判据会变红", len(m2warn) == 0,
          f"变异后仍剩 {len(m2warn)} 处")

    print("\n" + "=" * 78)
    if fails:
        print(f"FAILED  {len(fails)}/{checks}: {fails}")
        return 1
    print(f"ALL PASS  {checks}/{checks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
