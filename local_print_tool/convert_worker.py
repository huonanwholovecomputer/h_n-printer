"""convert_worker.py — 隔离转换 worker（S2-03 客户端半）

由 `office_sandbox.py` 以子进程方式拉起，在 Windows Job Object 里完成一次转换：

    python convert_worker.py <spec.json>          # 源码模式
    HN打印工具.exe --convert-worker <spec.json>   # 打包模式（main.py 早期分发）

协议（防竞态）
--------------
1. 进程启动后**先阻塞在 stdin 读一行**；父进程把本进程加入 Job Object 之后写 `GO`；
2. 收到 `GO` 才导入 converter 并执行转换（这样"拉 Office 子进程"一定发生在入 Job 之后）；
3. 结果写 stdout 最后一行 `RESULT:{"ok":..., "output":..., "error":...}`；成功时退出码 0。

为什么结果走 stdout 而不是退出码：COM 转换失败面很宽（无引擎/弹窗/空产物/超时），
需要把原因原样带回 GUI 日志；退出码只保留"成败"。

本文件**不导入 Qt / 不做任何 UI**；被 main.py 静态 import 以保证 PyInstaller 会打包它。
"""

from __future__ import annotations

import json
import os
import sys


def _emit(payload: dict) -> None:
    """把结论写到 stdout 最后一行（UTF-8；父进程按行解析）。"""
    try:
        sys.stdout.write("RESULT:" + json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        _emit({"ok": False, "error": "缺少 spec 参数"})
        return 2

    spec_path = argv[0]
    # 等父进程确认已进入 Job Object
    try:
        line = sys.stdin.readline().strip()
    except Exception:  # noqa: BLE001
        line = ""
    if line != "GO":
        _emit({"ok": False, "error": "未收到父进程 GO（拒绝在沙箱之外执行转换）"})
        return 3

    try:
        with open(spec_path, "r", encoding="utf-8") as f:
            spec = json.load(f)
        src = str(spec.get("src") or "")
        dst = str(spec.get("dst") or "")
        if not src or not dst:
            _emit({"ok": False, "error": "spec 缺少 src/dst"})
            return 2

        # worker 的日志走 stderr，父进程会把它转进主进程日志（2026-10-25：原来 INFO 级
        # 因为没有 handler 而被丢弃 ⇒ 开沙箱后现场看不到"引擎选择""COM 安全设置"）。
        import logging  # noqa: PLC0415
        logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                            format="%(levelname)s %(name)s: %(message)s")

        # 延迟到收到 GO 之后再导入（保证导入/依赖初始化也在 Job 内完成）
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import converter  # noqa: PLC0415
        logging.getLogger("convert_worker").info(
            "worker 开始转换: %s → %s（sandbox_child=%s）",
            os.path.basename(src), os.path.basename(dst),
            os.environ.get("HN_OFFICE_SANDBOX_CHILD", ""))

        conv = converter.UniversalConverter()
        out = conv.convert(
            src, dst,
            image_orientation=str(spec.get("image_orientation") or "auto"),
            from_cloud=bool(spec.get("from_cloud")),
        )
        ok = bool(out) and os.path.isfile(out) and os.path.getsize(out) > 0
        _emit({
            "ok": ok,
            "output": out or "",
            "error": "" if ok else "转换产物缺失或为空",
        })
        return 0 if ok else 4
    except Exception as e:  # noqa: BLE001 —— 任何异常都原样带回，父进程据此判失败
        _emit({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return 4


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
