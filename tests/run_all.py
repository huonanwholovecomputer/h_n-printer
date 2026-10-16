#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""run_all.py — HN 打印系统 验收套件聚合 runner

背景（2026-10 审查报告 根因 7）：全仓库零 CI、零聚合 runner，40 个验收脚本靠
"口口相传"执行 —— 这是"假保护能长期存活"的机制原因。本脚本把全套串起来，
让每一次改动都能"改前绿、改后绿"。

用法：
    python tests/run_all.py                # 跑全部"可在本机跑"的脚本（默认）
    python tests/run_all.py --all          # 连"必须换环境跑"的也一起跑（会大量失败，仅排查用）
    python tests/run_all.py --only duplex  # 只跑文件名含 duplex 的
    python tests/run_all.py -v             # 打印每个脚本的完整输出
    python tests/run_all.py --timeout 300  # 单脚本超时秒数（默认 150）
    python tests/run_all.py --strict       # 把「已知预期红」也按失败计

退出码：全部通过 = 0；有任何 FAIL/TIMEOUT = 1。
「已知预期红」（RED*）＝ 源码已改但产物待重建的过渡状态（见 EXPECTED_RED），
不计失败，但会打印解除条件；脚本一旦转绿也会提示把该条删掉，防止长期掩盖真回归。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

# 「必须换环境跑」的脚本：默认跳过并打印原因（不计入失败）。
# 判据来自脚本自身的 docstring（在服务器上跑 / 需后端依赖 / 需真实浏览器 / 需联网）。
ENV_REQUIRED: dict[str, str] = {
    "verify_deploy_smoke.py": "需在生产服务器上跑（线上冒烟）",
    "verify_http_heartbeat_channel.py": "需在服务器上跑（导入真实 app）",
    "verify_gthread_websocket.py": "需后端依赖环境（本机起独立小应用 5099）",
    "verify_apk_published.py": "需联网比对线上清单与 APK",
    "measure_card_heights.mjs": "需 Playwright / 真实浏览器",
    "verify_dark_rail_contrast.mjs": "需 Playwright / 真实浏览器",
    "verify_preview_render.mjs": "需 Playwright / 真实浏览器",
    "verify_trend_hourly_render.mjs": "需 Playwright / 真实浏览器（本机已装："
                                      "C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/"
                                      "dependencies/node/node_modules/playwright；--all 或直接 node 可跑）",
    "verify_order_table_columns.mjs": "需 Playwright / 真实浏览器（订单明细列宽只能靠真实布局量；"
                                      "同上，--all 或直接 node 可跑）",
}

# 非验收脚本（工具/选型/测量），不参与 PASS 计数
NOT_A_TEST = {"_pick_js_rounding.py"}

# 「已知预期红」：**不是**回归，而是"源码已改、产物待重建"的过渡状态。
# 判据必须是具体断言 + 明确的解除条件；脚本一旦转绿会提示删除本条，
# 以免长期掩盖真回归。`--strict` 下仍按失败计。
# 2026-10-16：APK 已重打为 1.1.32（versionCode 33）并通过 verify_apk_release.py ⇒
# 原先 verify_apk_release.py 那条过渡项**已删除**（留着会长期掩盖真回归）。
# 2026-10-16（D5 批次）：又改了 www/updater.js（更新锁三路解锁）+ app.js/print.js（请求超时）
# ⇒ 再次登记过渡项；**同日已重打 1.1.33（versionCode 34）并发布**，该项随即删除（守卫已转绿）。
# 2026-10（无障碍 × 双面拦截放开）：改了 www/print.js + www/app.js + www/styles.css（去掉移动端
# 「双面 × 无人值守」拦截）⇒ APK 内 web 资源与 www/ 暂时不一致。**2026-10-16 已重打 1.1.34
# （versionCode 35）并通过 verify_apk_release.py + verify_apk_published.py ⇒ 该项已删除**。
# 2026-10（攒批模式远程开关）：又改了 www/app.js + www/styles.css（设备列表的攒批状态徽标与
# 远程开关）⇒ 登记过渡项；**2026-10-16 已重打 1.1.35（versionCode 36），verify_apk_release
# 与 verify_apk_published 均转绿 ⇒ 该项已删除**。
EXPECTED_RED: dict[str, str] = {}

PY_EXTS = {".py"}
JS_EXTS = {".js", ".mjs"}


def discover() -> list[Path]:
    found: list[Path] = []
    for pattern in ("*.py", "*.js", "*.mjs"):
        found.extend(TESTS.glob(pattern))
        found.extend((TESTS / "audit_2026_12").glob(pattern))
    out = []
    for p in sorted(set(found)):
        name = p.name
        if name.startswith("_") or name == Path(__file__).name:
            continue
        if name in NOT_A_TEST:
            continue
        if p.parent.name == "__pycache__":
            continue
        out.append(p)
    return out


def find_node() -> str | None:
    node = shutil.which("node")
    if node:
        return node
    for cand in (
        r"C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-6\node.exe",
        r"C:\Program Files\nodejs\node.exe",
        "/usr/bin/node",
    ):
        if os.path.exists(cand):
            return cand
    return None


def run_one(path: Path, node: str | None, timeout: int) -> tuple[str, str, float]:
    """返回 (status, output, seconds)。status ∈ OK/FAIL/TIMEOUT/SKIP/ERROR。"""
    ext = path.suffix.lower()
    if ext in JS_EXTS:
        if not node:
            return "SKIP", "未找到 node，无法运行 JS 验收脚本", 0.0
        cmd = [node, str(path)]
    elif ext in PY_EXTS:
        cmd = [sys.executable, str(path)]
    else:
        return "SKIP", f"未知扩展名 {ext}", 0.0

    env = os.environ.copy()
    # 统一 UTF-8：脚本里的 emoji/中文断言在 GBK 控制台会直接崩（见审查报告根因 4）
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return "TIMEOUT", out, time.time() - t0
    except Exception as e:  # noqa: BLE001
        return "ERROR", f"{type(e).__name__}: {e}", time.time() - t0

    out = (proc.stdout or "") + (proc.stderr or "")
    return ("OK" if proc.returncode == 0 else "FAIL"), out, time.time() - t0


def tail(text: str, lines: int = 25) -> str:
    ls = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(ls[-lines:])


def main() -> int:
    ap = argparse.ArgumentParser(description="HN 打印系统 验收套件聚合 runner")
    ap.add_argument("--all", action="store_true", help="连必须换环境跑的脚本也一起跑")
    ap.add_argument("--only", default="", help="只跑文件名含该子串的脚本")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每个脚本的完整输出")
    ap.add_argument("--timeout", type=int, default=150, help="单脚本超时秒数（默认 150）")
    ap.add_argument("--strict", action="store_true",
                    help="把「已知预期红」（EXPECTED_RED）也按失败计")
    args = ap.parse_args()

    node = find_node()
    scripts = discover()
    if args.only:
        scripts = [p for p in scripts if args.only in p.name]

    results: list[tuple[Path, str, str, float]] = []
    total = len(scripts)
    print(f"HN 打印系统 · 验收套件聚合 runner  （共发现 {total} 个脚本）")
    print(f"  python : {sys.executable}")
    print(f"  node   : {node or '（未找到，JS 脚本将跳过）'}")
    print(f"  root   : {ROOT}")
    print("-" * 78)

    for i, path in enumerate(scripts, 1):
        rel = path.relative_to(ROOT).as_posix()
        if not args.all and path.name in ENV_REQUIRED:
            reason = ENV_REQUIRED[path.name]
            print(f"[{i:2d}/{total}] SKIP  {rel}  （{reason}）")
            results.append((path, "SKIP", reason, 0.0))
            continue

        status, out, secs = run_one(path, node, args.timeout)
        if path.name in EXPECTED_RED:
            if status == "OK":
                print(f"[{i:2d}/{total}] PASS  {rel}  ({secs:.1f}s)")
                print(f"        ⚠ 该脚本已在 EXPECTED_RED 名单里但已转绿 —— "
                      f"请从 tests/run_all.py 的 EXPECTED_RED 中删除本条（原因已过期）")
                results.append((path, "OK", out, secs))
            elif status == "FAIL" and not args.strict:
                print(f"[{i:2d}/{total}] RED*  {rel}  ({secs:.1f}s)  [已知预期红，非回归]")
                results.append((path, "RED*", out, secs))
            else:
                mark = {"OK": "PASS", "FAIL": "FAIL", "TIMEOUT": "TIME",
                        "SKIP": "SKIP", "ERROR": "ERR "}[status]
                print(f"[{i:2d}/{total}] {mark}  {rel}  ({secs:.1f}s)")
                results.append((path, status, out, secs))
            if args.verbose and out.strip():
                print("        " + tail(out).replace("\n", "\n        "))
            continue

        mark = {"OK": "PASS", "FAIL": "FAIL", "TIMEOUT": "TIME", "SKIP": "SKIP", "ERROR": "ERR "}[status]
        print(f"[{i:2d}/{total}] {mark}  {rel}  ({secs:.1f}s)")
        if args.verbose or status in ("FAIL", "TIMEOUT", "ERROR"):
            if out.strip():
                print("        " + tail(out).replace("\n", "\n        "))
        results.append((path, status, out, secs))

    # ── 汇总 ──
    ok = [r for r in results if r[1] == "OK"]
    skip = [r for r in results if r[1] == "SKIP"]
    red = [r for r in results if r[1] == "RED*"]
    bad = [r for r in results if r[1] in ("FAIL", "TIMEOUT", "ERROR")]

    print("-" * 78)
    print(f"结果：PASS {len(ok)} / {total - len(skip) - len(red)}  "
          f"（SKIP {len(skip)}，已知预期红 {len(red)}）   用时 {sum(r[3] for r in results):.1f}s")
    if bad:
        print("\n未通过：")
        for path, status, _out, _s in bad:
            print(f"  [{status}] {path.relative_to(ROOT).as_posix()}")
    if red:
        print("\n已知预期红（源码已改、产物待重建 —— 不是回归，--strict 可强行按失败计）：")
        for path, _s, _out, _t in red:
            print(f"  {path.relative_to(ROOT).as_posix()}")
            print(f"      {EXPECTED_RED[path.name]}")
    if skip:
        print("\n已跳过（需换环境跑，--all 可强制包含）：")
        for path, _s, reason, _t in skip:
            print(f"  {path.relative_to(ROOT).as_posix()}  —— {reason}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
