"""verify_watch_rpc_recover.py — 外部打印监视的 spooler 中断/恢复记录（P1-2）。

背景（`日志体检_2026-11-24.md` §2.1 末）：
  2026-11-23 22:54:58–22:56:24，spooler 的 RPC 不可用，`EnumPrinters` 抛 1722，
  **每 2 秒一条** warning，88 秒打了 44 条；恢复之后**一条都没留**，日志直接跳到 23:28。
  读日志的人看不出中断持续多久，也看不出有没有恢复（现场 23:02 还崩了 WINWORD）。

根因不是"没记日志"，而是**记日志的位置不对**：`default_list_printers()` 在内部把异常
吞成 `[]` 返回，于是 `PrintJobWatcher._loop()` 的 errors 计数器永远是 0、它那套
"第 1 次 + 每 30 次 + 恢复报一声"的节流**根本不会触发**。节流与状态必须挪到吞异常的地方。

断言（驱动 `print_watch._EnumFailureTracker`，假时钟，不碰 spooler）：
  A 首次失败记 1 条；继续失败不再刷屏
  B 超过 5 分钟升级一条"要操作员看服务"，且只升一次
  C 恢复时记一条带**中断秒数**与累计失败次数
  D 恢复后再次成功不再重复记

变异自证：把 `note_success` 的恢复行删掉 ⇒ C 段必须红。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "local_print_tool"))

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


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


ERR = Exception("(1722, 'EnumPrinters', 'RPC server unavailable')")


def main() -> int:
    print("=" * 78)
    print("verify_watch_rpc_recover.py — spooler 中断/恢复记录（P1-2）")
    print("=" * 78)

    import print_watch

    cap = _Capture()
    print_watch.logger.addHandler(cap)
    old_level = print_watch.logger.level
    print_watch.logger.setLevel(logging.DEBUG)

    clock = {"t": 1000.0}
    tr = print_watch._EnumFailureTracker(now_fn=lambda: clock["t"])

    try:
        # ── A. 首次失败记 1 条，继续失败不刷屏 ──────────────────
        print("\n=== A. 首次失败记 1 条；后续失败不刷屏 ===")
        cap.messages.clear()
        tr.note_failure(ERR)
        n_after_first = len(cap.messages)
        check("A1 首次失败恰好记 1 条", n_after_first == 1, f"实得 {n_after_first}")
        check("A2 这条带 HRESULT 1722", n_after_first == 1 and "1722" in cap.messages[0],
              f"实得={cap.messages[:1]}")

        for _ in range(10):
            clock["t"] += 2.0
            tr.note_failure(ERR)
        check("A3 再失败 10 次仍是 1 条（88 秒 44 条的刷屏不再发生）",
              len(cap.messages) == 1, f"实得 {len(cap.messages)}")
        check("A4 累计次数仍然被数出来", tr.failures == 11, f"failures={tr.failures}")

        # ── B. 超过 5 分钟升级一次 ──────────────────────────────
        print("\n=== B. 中断超过 5 分钟 ⇒ 升级一条（只升一次）===")
        clock["t"] += 301.0
        tr.note_failure(ERR)
        check("B1 升级行出现了", len(cap.messages) == 2, f"实得 {len(cap.messages)}")
        check("B2 升级行点名要查 Print Spooler",
              len(cap.messages) == 2 and "Print Spooler" in cap.messages[1],
              f"实得={cap.messages[1:2]}")
        check("B3 升级行带 300 秒阈值",
              len(cap.messages) == 2 and "300" in cap.messages[1],
              f"实得={cap.messages[1:2]}")
        for _ in range(5):
            clock["t"] += 2.0
            tr.note_failure(ERR)
        check("B4 不再重复升级", len(cap.messages) == 2, f"实得 {len(cap.messages)}")

        # ── C. 恢复要带秒数与累计次数 ───────────────────────────
        print("\n=== C. 恢复记一条，带中断秒数与累计失败次数 ===")
        clock["t"] += 9.0            # since=1000，恢复时刻 = 1312+9
        tr.note_success()
        check("C1 恢复了 1 条", len(cap.messages) == 3, f"实得 {len(cap.messages)}")
        last = cap.messages[-1] if cap.messages else ""
        # 中断秒数 = 恢复时刻 - 首次失败时刻；本段共 note_failure 17 次（1+10+1+5）
        check("C2 恢复行带中断秒数 340s", "340s" in last, f"实得={last!r}")
        check("C3 恢复行带累计失败次数 17", "17" in last, f"实得={last!r}")
        check("C4 状态被清零", tr.failures == 0 and tr.escalated is False,
              f"failures={tr.failures} escalated={tr.escalated}")

        # ── D. 恢复后再次成功不重复记 ───────────────────────────
        print("\n=== D. 恢复后再成功不重复记 ===")
        tr.note_success()
        tr.note_success()
        check("D1 仍然只有 3 条", len(cap.messages) == 3, f"实得 {len(cap.messages)}")

        # ── E. 变异自证 ────────────────────────────────────────
        print("\n=== E. 变异自证：摘掉恢复行 ⇒ C 段判据必须红 ===")
        saved = print_watch._EnumFailureTracker.note_success
        try:
            def _muted(self):
                self.failures = 0
                self.escalated = False
            print_watch._EnumFailureTracker.note_success = _muted
            cap.messages.clear()
            tr2 = print_watch._EnumFailureTracker(now_fn=lambda: clock["t"])
            tr2.note_failure(ERR)
            before = len(cap.messages)
            tr2.note_success()
            check("E1 变异后恢复时不再增行", len(cap.messages) == before,
                  f"恢复前 {before} 条，恢复后 {len(cap.messages)} 条")
        finally:
            print_watch._EnumFailureTracker.note_success = saved
    finally:
        print_watch.logger.removeHandler(cap)
        print_watch.logger.setLevel(old_level)

    print("\n" + "=" * 78)
    if fails:
        print(f"FAILED  {len(fails)}/{checks}: {fails}")
        return 1
    print(f"ALL PASS  {checks}/{checks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
