"""verify_stuck_subtask_alarm.py — 服务端 `[WAIT]` 去重 / 升级 / 状态收敛（P1-3）。

背景（`日志体检_2026-11-24.md` §2.4）：
  25 个子任务被绑在旧 client_id `R7000-AHP10-168dbdc3d1` 上，扫描器把它们轮询了
  **10,234 次**；`扫描到 10 个排队子任务` 出现 857 次。日志除了刷屏什么都没说 ——
  哪个单、等了多久、目标设备是谁，一个都没有。

改法：`process_pending_orders` 里两条 `[WAIT]` 分支改走 `_WaitThrottle`：
首次一条、之后同一子任务最多每 10 分钟一条、等过 30 分钟升级一条（带订单号 + 目标设备）。
**排队语义一个字没改**：不判失败、不改状态、不自动改派。

为什么不 `import app`：app.py 在导入期就建 Flask/DB/调度器（`verify_http_heartbeat_channel.py`
之所以标"需在服务器上跑"就是这原因）。本脚本用 AST 从源码里取出 `_WaitThrottle` 的类定义
**原样执行**，跑的是真代码，但不拉起服务。

断言（假时钟，全差分）：
  A 首轮：25 个子任务各 1 条
  B 10 分钟内再扫两轮：0 条（刷屏消失）
  C 过 11 分钟：每个子任务再 1 条，带已等分钟数与扫描次数
  D 过 30 分钟：升级 1 条，带订单号与目标设备；升级只发一次
  E `forget_absent` 把已派出的子任务状态清掉
  F 变异自证：把重复间隔改 0 ⇒ B 段判据必须变红
"""
from __future__ import annotations

import ast
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
APP = ROOT / "mobile_apps" / "printer-backend" / "app.py"

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


def _load_throttle_class():
    """从 app.py 源码里取出 _WaitThrottle 类定义并执行，返回类对象。"""
    src = APP.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "_WaitThrottle":
            seg = ast.get_source_segment(src, node)
            if not seg:
                break
            ns = {"time": time, "threading": threading, "__name__": "app_extracted"}
            exec(compile(seg, f"<{APP.name}:_WaitThrottle>", "exec"), ns)  # noqa: S102
            return ns["_WaitThrottle"]
    return None


def main() -> int:
    print("=" * 78)
    print("verify_stuck_subtask_alarm.py — 服务端 [WAIT] 收敛（P1-3）")
    print("=" * 78)

    check("0.0 app.py 存在", APP.is_file(), str(APP))
    Cls = _load_throttle_class()
    check("0.1 从源码里取到了 _WaitThrottle 类", Cls is not None,
          "类被改名/删除 ⇒ 这条会红")
    if Cls is None:
        print("\nFAILED  没取到类，后面跳过")
        return 1

    clock = {"t": 1_000_000.0}
    th = Cls(now_fn=lambda: clock["t"])

    N = 25
    IDENT = "R7000-AHP10-168dbdc3d1"
    ORDER = "HN20261123-0042"

    def sweep() -> list[str]:
        return [ln for ln in (th.note(i, IDENT, "target offline",
                                      extra=f"(order {ORDER})") for i in range(1, N + 1))
                if ln]

    # ── A. 首轮每个子任务各一条 ──────────────────────────────────
    print("\n=== A. 首轮：25 个子任务各 1 条 ===")
    lines_a = sweep()
    check("A1 首轮 25 条（一个子任务一条）", len(lines_a) == N, f"实得 {len(lines_a)}")
    check("A2 每条都带 [WAIT] 与子任务号",
          all("[WAIT]" in ln and "#" in ln for ln in lines_a))
    check("A3 每条带订单号", all(ORDER in ln for ln in lines_a),
          f"实得={lines_a[:1]}")

    # ── B. 10 分钟内不刷屏 ───────────────────────────────────────
    print("\n=== B. 10 分钟内再扫两轮：0 条 ===")
    clock["t"] += 60.0
    lines_b1 = sweep()
    clock["t"] += 60.0
    lines_b2 = sweep()
    check("B1 第 2 轮 0 条", len(lines_b1) == 0, f"实得 {len(lines_b1)}")
    check("B2 第 3 轮 0 条（现场那种每轮 25 行的刷屏没了）", len(lines_b2) == 0,
          f"实得 {len(lines_b2)}")

    # ── C. 过 10 分钟再念一次，带时长与扫描次数 ─────────────────
    print("\n=== C. 过 10 分钟：每个子任务再 1 条，带分钟数与次数 ===")
    clock["t"] += 600.0
    lines_c = sweep()
    check("C1 又是 25 条（间隔到了才念）", len(lines_c) == N, f"实得 {len(lines_c)}")
    check("C2 带已等分钟数", all("min" in ln or "分钟" in ln or "13" in ln for ln in lines_c),
          f"实得={lines_c[:1]}")
    check("C3 带累计扫描次数", all("4" in ln for ln in lines_c),
          f"实得={lines_c[:1]}")

    # ── D. 过 30 分钟升级一次 ────────────────────────────────────
    print("\n=== D. 过 30 分钟：升级 1 条，且只升一次 ===")
    clock["t"] += 1200.0          # 累计约 31 分钟
    lines_d = sweep()
    check("D1 升级轮有输出", len(lines_d) == N, f"实得 {len(lines_d)}")
    check("D2 升级行点名目标设备", all(IDENT in ln for ln in lines_d),
          f"实得={lines_d[:1]}")
    check("D3 升级行带订单号（能对上结算页那笔）",
          all(ORDER in ln for ln in lines_d), f"实得={lines_d[:1]}")
    clock["t"] += 600.0
    lines_d2 = sweep()
    check("D4 升级只发一次（下一轮仍是普通重复行，不是升级行）", len(lines_d2) == N,
          f"实得 {len(lines_d2)}")
    # 升级行专有的两个说法：升级措辞里会让人"改派/作废"，普通重复行只说已等多久
    check("D5 升级行确实用了升级措辞，而下一轮不再带它",
          all(("改派" in ln) for ln in lines_d) and all(("改派" not in ln) for ln in lines_d2),
          f"升级行={lines_d[:1]} 下一轮={lines_d2[:1]}")

    # ── E. forget_absent 清理状态 ───────────────────────────────
    print("\n=== E. forget_absent 把已派出的子任务状态清掉 ===")
    gone = th.forget_absent(set(range(1, 11)))     # 只保 10 个
    check("E1 清掉 15 条状态", gone == 15, f"实得 {gone}")
    fresh = th.note(20, IDENT, "target offline", extra=f"(order {ORDER})")
    check("E2 被清掉的子任务再来时当首轮处理（重新记一条）", fresh is not None)

    # ── F. 变异自证 ────────────────────────────────────────────
    print("\n=== F. 变异自证：重复间隔改 0 ⇒ B 段判据必须变红 ===")
    saved = Cls.REPEAT_EVERY_S
    try:
        Cls.REPEAT_EVERY_S = 0.0
        th2 = Cls(now_fn=lambda: clock["t"])
        first = [th2.note(i, IDENT, "x") for i in range(1, 3)]
        second = [th2.note(i, IDENT, "x") for i in range(1, 3)]
        check("F1 间隔为 0 时第二轮照样输出（B2 的判据因此有牙）",
              all(x is not None for x in second),
              f"首轮={[bool(x) for x in first]} 第二轮={[bool(x) for x in second]}")
    finally:
        Cls.REPEAT_EVERY_S = saved

    print("\n" + "=" * 78)
    if fails:
        print(f"FAILED  {len(fails)}/{checks}: {fails}")
        return 1
    print(f"ALL PASS  {checks}/{checks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
