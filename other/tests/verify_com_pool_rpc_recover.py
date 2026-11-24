"""verify_com_pool_rpc_recover.py — COM 池「实例失联 ⇒ 立即重建并重投一次」的验收（P1-1）。

背景（`日志体检_2026-11-24.md` §2.1）：
  7 天里 389 条 `Windows fatal exception: code 0x800706ba/be/10108`，全部落在
  `converter._convert_one → Documents.Open/ExportAsFixedFormat`；业务日志只留 2 条。
  旧恢复策略：失败后**要等 `_failures >= 2`** 才 `_reset_app()` ⇒ 第一份文件注定跑在
  死实例上白失败一次，能不能成功全靠"下一次调度碰巧换实例"。

新策略：`is_rpc_lost(e)` 命中 ⇒ 立刻 `_reset_app()` + 把**同一件**活重投一次
（`_RPC_RETRY_LIMIT = 1`），且不置 `job.done`（调用方等的还是同一个事件）。
坏文档（`RuntimeError: docx 直转产物无效`）不匹配 `is_rpc_lost` ⇒ 一次都不重投。

三段断言，全部**真跑** `_ComPoolWorker.run()`（Office 用假引擎替代，不启动任何 COM 服务）：
  A 一次失联后重投成功：Open 调 2 次、最终无 error、旧实例被 Quit
  B 坏文档：Open 只调 1 次、error 原样带回、不重投
  C 重投上限：连续失联 2 次后放弃（不会无限重投），并留下 com_errors 记录

变异自证：把 `_RPC_RETRY_LIMIT` 改成 0（等价于退回旧行为）⇒ A 段必须红。
"""
from __future__ import annotations

import logging
import sys
import tempfile
import threading
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


class _ComError(Exception):
    """模拟 pywintypes.com_error：args[0] = 负 HRESULT。"""


class _FakeDoc:
    def __init__(self):
        self.closed = 0

    def ExportAsFixedFormat(self, *a, **k):
        return None

    def Close(self, **k):
        self.closed += 1


class _FakeDocs:
    def __init__(self, opener):
        self._opener = opener
        self.Count = 0

    def Open(self, *a, **k):
        return self._opener()


class _FakeApp:
    """足够宽的假 Word 实例：属性随便赋值，Open 交给 opener 决定成败。"""

    def __init__(self, opener, tag: str):
        self.Name = "FakeWord"
        self.tag = tag
        self.quit_calls = 0
        self.Documents = _FakeDocs(opener)

    def Quit(self):
        self.quit_calls += 1

    def __getattr__(self, item):
        return 0          # Options / Version / AutomationSecurity ... 一律给个 int


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _run_case(converter, opener_factory, src: str, n_jobs: int = 1):
    """起一个 worker、投一件活、等 done，然后停掉 worker。返回 (job, apps, opens)。"""
    apps: list[_FakeApp] = []
    opens = {"n": 0}

    def make_app():
        app = _FakeApp(opener_factory(opens, apps), tag=f"app{len(apps)}")
        apps.append(app)
        return app

    original = converter._safe_word_dispatch
    converter._safe_word_dispatch = make_app
    worker = converter._ComPoolWorker("word", "Word", 0)
    worker.start()
    try:
        job = converter._PoolJob(src, src + ".pdf")
        worker.submit(job)
        job.done.wait(20.0)
    finally:
        worker.submit(converter._STOP)
        worker.join(10.0)
        converter._safe_word_dispatch = original
    return job, apps, opens["n"]


def main() -> int:
    print("=" * 78)
    print("verify_com_pool_rpc_recover.py — COM 池实例失联自愈（P1-1）")
    print("=" * 78)

    import converter

    cap = _Capture()
    com_logger = logging.getLogger("com.error")
    com_logger.addHandler(cap)
    com_logger.setLevel(logging.DEBUG)

    tmp = tempfile.mkdtemp(prefix="hn_com_pool_")
    src = str(Path(tmp) / "sample.docx")
    Path(src).write_bytes(b"PK\x03\x04 fake docx")

    # ── A. 一次失联后重投成功 ──────────────────────────────────────
    print("\n=== A. 一次实例失联 ⇒ 立刻重建 + 重投同一件 ⇒ 成功 ===")

    def opener_rpc_once(opens, apps):
        def _open():
            opens["n"] += 1
            if opens["n"] == 1:
                raise _ComError(-2147023174, "RPC server unavailable", None, None)
            return _FakeDoc()
        return _open

    cap.messages.clear()
    job_a, apps_a, opens_a = _run_case(converter, opener_rpc_once, src)
    check("A1 最终无 error（重投成功）", job_a.error is None, f"error={job_a.error!r}")
    check("A2 Open 调了 2 次（失败 1 + 重投 1）", opens_a == 2, f"实得 {opens_a}")
    check("A3 job 只被重投 1 次", getattr(job_a, "retries", -1) == 1,
          f"retries={getattr(job_a, 'retries', None)}")
    check("A4 旧实例被重建（至少一个 app 被 Quit）",
          any(a.quit_calls >= 1 for a in apps_a),
          f"quit_calls={[a.quit_calls for a in apps_a]}")
    check("A5 com_errors 留下 0x800706ba",
          any("0x800706ba" in m for m in cap.messages),
          f"实得={cap.messages[:2]}")
    check("A6 重建后用的是**新**实例（不是同一个被复活的）", len(apps_a) >= 2,
          f"app 数={len(apps_a)}")

    # ── B. 坏文档不重投 ───────────────────────────────────────────
    print("\n=== B. 坏文档（非 RPC）⇒ 一次都不重投 ===")

    def opener_bad_doc(opens, apps):
        def _open():
            opens["n"] += 1
            raise RuntimeError("docx invalid output (empty / not PDF)")
        return _open

    cap.messages.clear()
    job_b, apps_b, opens_b = _run_case(converter, opener_bad_doc, src)
    check("B1 error 原样带回", isinstance(job_b.error, RuntimeError), f"error={job_b.error!r}")
    check("B2 Open 只调 1 次（没重投）", opens_b == 1, f"实得 {opens_b}")
    check("B3 retries 保持 0", getattr(job_b, "retries", -1) == 0)
    check("B4 没写 com_errors（不冒充实例失联）",
          not any("0x800706" in m for m in cap.messages), f"实得={cap.messages[:2]}")

    # ── C. 重投上限：连续失联不会无限重投 ────────────────────────
    print("\n=== C. 连续失联 ⇒ 到上限就放弃（不无限重投）===")

    def opener_always_rpc(opens, apps):
        def _open():
            opens["n"] += 1
            raise _ComError(-2147023170, "RPC call failed", None, None)
        return _open

    cap.messages.clear()
    job_c, apps_c, opens_c = _run_case(converter, opener_always_rpc, src)
    limit = converter._RPC_RETRY_LIMIT
    check("C1 最终仍报 error", job_c.error is not None, f"error={job_c.error!r}")
    check("C2 Open 次数 = 1 + 重投上限", opens_c == 1 + limit, f"实得 {opens_c}，上限 {limit}")
    check("C3 com_errors 记了 0x800706be",
          any("0x800706be" in m for m in cap.messages), f"实得={cap.messages[:3]}")

    # ── D. 变异自证 ───────────────────────────────────────────────
    print("\n=== D. 变异自证：把重投上限改成 0（退回旧行为）⇒ A 段判据必须红 ===")
    saved = converter._RPC_RETRY_LIMIT
    try:
        converter._RPC_RETRY_LIMIT = 0
        cap.messages.clear()
        job_d, apps_d, opens_d = _run_case(converter, opener_rpc_once, src)
        check("D1 变异后 A2（Open=2）不再成立", opens_d != 2, f"实得 {opens_d}")
        check("D2 变异后 A1（无 error）不再成立", job_d.error is not None,
              f"error={job_d.error!r}")
    finally:
        converter._RPC_RETRY_LIMIT = saved

    com_logger.removeHandler(cap)

    print("\n" + "=" * 78)
    if fails:
        print(f"FAILED  {len(fails)}/{checks}: {fails}")
        return 1
    print(f"ALL PASS  {checks}/{checks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
