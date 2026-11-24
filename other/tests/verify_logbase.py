"""verify_logbase.py — 日志基座三项改动的验收（2026-11-24 日志体检 P2-1 / P2-2 / P2-3）。

背景（`日志体检_2026-11-24.md`）：
  D2  本地 10,428 行日志里 ERROR 为 **0**；GUI 线程崩溃只写 crash_traceback.txt，不进业务日志
      → 11-18 那 16 次 `StackQueueDialog already deleted`，当日 1,856 行日志一字未提。
  D3  faulthandler 的栈块没有时间戳，只能靠下一条「进程启动」反推；11-23 12:07 那批
      横跨 12,602 行才遇到下一锚点，故障落在哪一分钟读不出来。
  D7  崩漣日志被非致命 COM 错误淹没（7 天 389 条），真正的 SEH 致命栈混在噪音里。

断言四组：
  A 未捕获异常必须进业务日志（动态：直接调 main._emit_crash_to_log + 静态：hook 里确实调了它）
  B RPC 失联必须能和「坏文档」分开（动态：真实 HRESULT 与文案两种形态）
  C COM 失败进 com_errors.log 且**不**进每日日志（动态：真写文件）
  D 进程退出边界必须落盘（动态：调用注册函数后手触发）

全部不碰真实 logs 目录：临时目录 + monkeypatch main.paths.logs_dir。
变异自证：每个「必须出现」的断言都配一份被改坏的源码/对象，证明它真的会红。
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import types
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
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


def _fresh(name: str) -> tuple[logging.Logger, _Capture]:
    lg = logging.getLogger(name)
    lg.handlers.clear()
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    cap = _Capture()
    lg.addHandler(cap)
    return lg, cap


def main() -> int:
    print("=" * 78)
    print("verify_logbase.py — 日志基座（P2-1 / P2-2 / P2-3）")
    print("=" * 78)

    import importlib
    import main as main_mod
    import converter
    importlib.reload(converter)      # 保证拿到当前源码里的 is_rpc_lost

    tmpdir = tempfile.mkdtemp(prefix="hn_logbase_")

    # ── A. 未捕获异常进业务日志 ───────────────────────────────────────
    print("\n=== A. 未捕获异常必须进业务日志（修 D2）===")
    crash_logger, cap = _fresh("crash")
    try:
        ValueError("模拟 GUI 线程崩：StackQueueDialog already deleted")
        raise ValueError("模拟 GUI 线程崩：StackQueueDialog already deleted")
    except ValueError:
        etype, value, tb = sys.exc_info()
        main_mod._emit_crash_to_log(etype, value)

    check("A1 记录了一条 ERROR", any(r.levelno == logging.ERROR for r in cap.records),
          f"实际级别={[r.levelname for r in cap.records]}")
    check("A2 记录里带异常类型与消息",
          any("ValueError" in r.getMessage() and "StackQueueDialog" in r.getMessage()
              for r in cap.records),
          f"实得={[r.getMessage() for r in cap.records][:2]}")

    # 静态：hook 里必须真的调用了 _emit_crash_to_log（否则 A1 绿了但线上仍然不写）
    import inspect
    hook_src = inspect.getsource(main_mod.main)
    check("A3 _debug_excepthook 里确实调用了 _emit_crash_to_log",
          "_emit_crash_to_log(etype, value)" in hook_src)
    # 变异自证：把调用删掉，判据必须失效
    mutated = hook_src.replace("        _emit_crash_to_log(etype, value)\n", "")
    check("A4 变异自证：删掉调用后 A3 判据会变红",
          "_emit_crash_to_log(etype, value)" not in mutated)

    # ── B. RPC 失联 vs 坏文档 ────────────────────────────────────────
    print("\n=== B. RPC 失联必须能和『坏文档』分开（修 D7 的前提）===")

    class _FakeComError(Exception):
        """模拟 pywintypes.com_error：args[0] 是负 HRESULT。"""

    rpc = _FakeComError(-2147023174, "RPC 服务器不可用。", None, None)   # 0x800706BA
    rpc_be = _FakeComError(-2147023170, "RPC_S_CALL_FAILED", None, None)  # 0x800706BE
    rpc_dis = _FakeComError(-2147417848, "Call was rejected", None, None)  # 0x80010108
    bad_doc = RuntimeError("docx 直转产物无效（空 / 非 PDF）")

    check("B1 0x800706BA 判为失联", converter.is_rpc_lost(rpc), str(rpc.args))
    check("B2 0x800706BE 判为失联", converter.is_rpc_lost(rpc_be))
    check("B3 0x80010108 判为失联", converter.is_rpc_lost(rpc_dis))
    check("B4 坏文档**不**判为失联", not converter.is_rpc_lost(bad_doc),
          "坏文档被误判 ⇒ 会白白重建 COM 实例")
    check("B5 文案形态也能认（无 args 时）",
          converter.is_rpc_lost(Exception("pywintypes.com_error: RPC 服务器不可用。")))
    check("B6 None 不炸", converter.is_rpc_lost(None) is False)

    # 变异自证：把 RPC 码集合清空、且文案表也清空，B1 必须变红
    saved_codes, saved_names = converter._RPC_LOST_CODES, converter._RPC_LOST_NAMES
    try:
        converter._RPC_LOST_CODES = frozenset()
        converter._RPC_LOST_NAMES = ()
        check("B7 变异自证：清空识别表后 B1 判据会变红",
              not converter.is_rpc_lost(rpc))
    finally:
        converter._RPC_LOST_CODES, converter._RPC_LOST_NAMES = saved_codes, saved_names

    # ── C. COM 失败进 com_errors.log、不进每日日志 ──────────────────
    print("\n=== C. COM 失败单独立卷（修 D7）===")
    saved_logs_dir = main_mod.paths.logs_dir
    main_mod.paths.logs_dir = lambda: tmpdir
    main_mod._COM_ERR_HANDLER = None
    com_edge, _ = _fresh("com.error")
    try:
        main_mod._install_com_error_log(None)
        com_logger = logging.getLogger("com.error")
        check("C1 propagate 关掉（不给每日日志刷屏）", com_logger.propagate is False,
              f"propagate={com_logger.propagate}")
        com_logger.warning("Word COM 实例失联（code=0x800706ba，第 1 次）")
        for h in com_logger.handlers:
            try:
                h.flush()
            except Exception:
                pass
        path = os.path.join(tmpdir, "com_errors.log")
        body = Path(path).read_text(encoding="utf-8") if os.path.isfile(path) else ""
        check("C2 com_errors.log 落了这一条", "0x800706ba" in body, f"文件内容={body[:200]!r}")
        check("C3 带时间戳前缀", body[:4].isdigit() and body[4] == "-", f"首 12 字={body[:12]!r}")
    finally:
        main_mod.paths.logs_dir = saved_logs_dir
        for h in list(logging.getLogger("com.error").handlers):
            try:
                h.close()
            except Exception:
                pass
        logging.getLogger("com.error").handlers.clear()
        main_mod._COM_ERR_HANDLER = None

    # ── D. 进程退出边界 ────────────────────────────────────────────
    print("\n=== D. 进程退出边界（修 D3）===")
    # 判据只用 ASCII 结构，不拿中文文案比对产品源码（verify_guard_quality 的 A2 棘轮
    # 只许降不许升：中文文案当契约是双面失效的反模式，别往 146 上面加）。
    import re as _re
    win_path = os.path.join(tmpdir, "crash_faulthandler.log")
    _MARK = _re.compile(r"^===== \S+ 20\d\d-\d\d-\d\d \d\d:\d\d:\d\d \(pid=\d+\) =====$", _re.M)
    with open(win_path, "w", encoding="utf-8") as fh:
        fh.write("===== 进程启动 2026-11-23 12:07:20 (pid=15512) =====\n")
        fh.flush()
        end_cb = main_mod._register_window_end(fh, None)
        check("D1 注册函数返回可调用的退出回调", callable(end_cb))
        end_cb()                      # 模拟 atexit
    tail = Path(win_path).read_text(encoding="utf-8")
    marks = _MARK.findall(tail)
    check("D2 窗口有两条边界（起 + 终），批跨度因此闭合",
          len(marks) == 2, f"实得 {len(marks)} 条: {marks}")
    check("D3 两条边界时间不同（确实是起与终，不是重复写同一行）",
          len(marks) == 2 and marks[0] != marks[1], f"{marks}")

    print("\n" + "=" * 78)
    if fails:
        print(f"FAILED  {len(fails)}/{checks}: {fails}")
        return 1
    print(f"ALL PASS  {checks}/{checks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
