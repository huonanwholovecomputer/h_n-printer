# -*- coding: utf-8 -*-
"""验收 · 转换实例池（2026-11-19）：复用常驻 Word/WPS 实例，而不是每份文件冷启动一个

为什么要有这个池（现场实测数字）：老路径每转一份文件 `DispatchEx` 一个新 Office 进程、
转完 `Quit`；同一批 6 份同模板文档 9.08 s（1.37~1.66 s/份）。复用同一个私有实例循环
Open/Export/Close 只要 3.15 s（0.19~0.56 s/份）⇒ 瓶颈是进程冷启动，不是线程数。

本脚本钉住的语义（**全部桩引擎，绝不拉起真的 Office**）：
  A. 默认走池：N 份文件只 `DispatchEx` 一次，产物一份都不少；
  B. 池大小 = `HN_CONVERT_POOL_SIZE`（默认 2）：并发峰值不超过它，也不超过 1；
  C. 每一份文件在 `Documents.Open` 之前都**重新收紧**实例（常驻 ≠ 放松）；
  D. 单份转换失败不换实例（连续失败才重建）——坏的是一份文档，不是引擎；
  E. 连续两次失败 ⇒ 该实例 Quit 后按需重建；
  F. 空闲超过 TTL ⇒ 实例自动 Quit 退出（不长期占内存）；
  G. `shutdown_office_pools()` 收尾：常驻实例全部 Quit，线程退出；
  H. `HN_CONVERT_POOL=0` ⇒ 完全回到老路径（每文件新实例 + Quit，零回归）。

用法: python tests/verify_convert_pool.py     （exit 0 = 全绿）
"""
import io
import os
import sys
import tempfile
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_conv_pool_")
os.environ["APPDATA"] = _SANDBOX
os.environ.pop("HN_CONVERT_POOL", None)
os.environ.pop("HN_CONVERT_POOL_SIZE", None)
os.environ.pop("HN_CONVERT_POOL_IDLE", None)
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import converter as C  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title):
    print("\n=== " + title + " ===")


# ============================================================
# 桩引擎：假 Word/WPS COM（记录 Open/Export/Quit 与并发峰值）
# ============================================================

STATE = {
    "dispatch": 0,        # 创建了几个私有实例
    "quit": 0,            # 实例 Quit 了几次
    "export": 0,          # 成功导出几份
    "harden": 0,          # 每次 Open 前的收紧调用次数
    "active": 0,          # 当前正在导出的并发数
    "peak": 0,            # 并发峰值
    "fail_remaining": 0,  # 接下来几次导出要抛错（模拟坏文档）
    "sleep": 0.0,         # 每次导出的耗时
}
STATE_LOCK = threading.Lock()


class _FakeDoc:
    def __init__(self, app, src):
        self._app = app
        self.src = src

    def ExportAsFixedFormat(self, dst, ExportFormat=17):
        with STATE_LOCK:
            STATE["active"] += 1
            STATE["peak"] = max(STATE["peak"], STATE["active"])
            fail = STATE["fail_remaining"] > 0
            if fail:
                STATE["fail_remaining"] -= 1
            delay = STATE["sleep"]
        try:
            if delay:
                time.sleep(delay)
            if fail:
                raise RuntimeError("fake export failure")
            with open(dst, "wb") as f:
                f.write(b"%PDF-1.4 fake pool output")
            with STATE_LOCK:
                STATE["export"] += 1
        finally:
            with STATE_LOCK:
                STATE["active"] -= 1

    def Close(self, SaveChanges=0):
        try:
            self._app.open_docs.remove(self)
        except ValueError:
            pass


class _FakeDocs:
    def __init__(self, app):
        self._app = app

    @property
    def Count(self):
        return len(self._app.open_docs)

    def __call__(self, i):
        return self._app.open_docs[int(i) - 1]

    def Open(self, path, ReadOnly=True):
        # 与真 COM 一样：打开前实例必须已被收紧（harden 由调用方在 Open 之前调用）
        if not self._app.hardened:
            raise RuntimeError("Documents.Open 之前没有收紧实例（安全判据被绕过）")
        doc = _FakeDoc(self._app, path)
        self._app.open_docs.append(doc)
        return doc


class _FakeApp:
    def __init__(self):
        self.open_docs = []
        self.hardened = False
        self.alive = True
        self.Documents = _FakeDocs(self)
        self.Visible = True
        self.DisplayAlerts = -1
        self.AutomationSecurity = 0
        self.Options = type("_Opts", (), {"UpdateLinksAtOpen": True})()

    @property
    def Name(self):
        if not self.alive:
            raise RuntimeError("fake app gone")
        return "FakeOffice"

    def Quit(self):
        self.alive = False
        with STATE_LOCK:
            STATE["quit"] += 1

    def __setattr__(self, key, value):
        # 记录"实例被收紧过"：四件套里任意一项写成功即可满足桩的 Open 前置条件
        object.__setattr__(self, key, value)
        if key in ("Visible", "DisplayAlerts", "AutomationSecurity"):
            object.__setattr__(self, "hardened", True)


def _fake_dispatch():
    with STATE_LOCK:
        STATE["dispatch"] += 1
    return _FakeApp()


def _reset_state(**kw):
    with STATE_LOCK:
        for k in ("dispatch", "quit", "export", "harden", "peak"):
            STATE[k] = 0
        STATE["active"] = 0
        STATE["fail_remaining"] = 0
        STATE["sleep"] = 0.0
        STATE.update(kw)


# 桩掉真 COM 的两个入口；harden 包一层只为计数（仍执行真实收紧逻辑）
_orig_word_dispatch = C._safe_word_dispatch
_orig_wps_dispatch = C._safe_wps_dispatch
_orig_harden = C._harden_office_com_app
_orig_idle_ttl = C._convert_pool_idle_ttl


def _counting_harden(app, label, quiet=False):
    with STATE_LOCK:
        STATE["harden"] += 1
    return _orig_harden(app, label, quiet)


C._safe_word_dispatch = _fake_dispatch
C._safe_wps_dispatch = _fake_dispatch
C._harden_office_com_app = _counting_harden

WORK = tempfile.mkdtemp(prefix="hn_pool_work_")
OUT = tempfile.mkdtemp(prefix="hn_pool_out_")
SRCS = []


def make_src(i):
    p = os.path.join(WORK, f"doc{i:02d}.docx")
    with open(p, "wb") as f:
        f.write(b"PK\x03\x04 fake docx %d" % i)
    SRCS.append(p)
    return p


def convert(i, engine="word"):
    src = make_src(i)
    dst = os.path.join(OUT, f"doc{i:02d}.pdf")
    (C._convert_via_wps_com if engine == "wps" else C._convert_via_word_com)(src, dst)
    return dst


try:
    # ============================================================
    section("A. 默认走池：6 份文件只创建 1 个私有实例，产物齐全")
    _reset_state()
    C.shutdown_office_pools()
    outs = [convert(i) for i in range(6)]
    check("6 份文件全部产出 PDF", all(os.path.isfile(p) and os.path.getsize(p) > 0 for p in outs),
          f"{len([p for p in outs if os.path.isfile(p)])}/6")
    check("★ 只 DispatchEx 了 1 次（复用常驻实例，而不是每份冷启动）",
          STATE["dispatch"] == 1, f"dispatch={STATE['dispatch']}")
    check("转换期间没有提前 Quit 实例", STATE["quit"] == 0, f"quit={STATE['quit']}")
    check("★ 每份文件在 Open 前都重新收紧了一次（常驻 ≠ 放松）",
          STATE["harden"] == 6, f"harden={STATE['harden']}")
    check("导出次数 = 6", STATE["export"] == 6, f"export={STATE['export']}")

    # ============================================================
    section("B. 池大小：默认 2，并发峰值既不超过 2 也不退化成 1")
    C.shutdown_office_pools()
    _reset_state(sleep=0.2)
    errs = []

    def worker(i):
        try:
            convert(i)
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ths = [threading.Thread(target=worker, args=(100 + i,)) for i in range(6)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(30)
    check("6 份并发提交全部成功", not errs, str(errs[:2]))
    check("★ 并发峰值 = 2（= 默认池大小，不再一文件一实例）", STATE["peak"] == 2,
          f"peak={STATE['peak']}")
    check("只创建了 2 个实例", STATE["dispatch"] == 2, f"dispatch={STATE['dispatch']}")

    # ============================================================
    section("C. 池大小可配成 1：退化为单实例串行（并发峰值 1）")
    C.shutdown_office_pools()
    os.environ["HN_CONVERT_POOL_SIZE"] = "1"
    _reset_state(sleep=0.1)
    errs = []
    ths = [threading.Thread(target=worker, args=(200 + i,)) for i in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(30)
    check("单实例配置下 4 份并发提交全部成功", not errs, str(errs[:2]))
    check("★ 并发峰值为 1（HN_CONVERT_POOL_SIZE=1 生效）", STATE["peak"] == 1,
          f"peak={STATE['peak']}")
    check("只创建了 1 个实例", STATE["dispatch"] == 1, f"dispatch={STATE['dispatch']}")
    os.environ.pop("HN_CONVERT_POOL_SIZE", None)

    # ============================================================
    section("D. 单份失败不换实例；连续两次失败才重建")
    C.shutdown_office_pools()
    _reset_state(fail_remaining=1)
    try:
        convert(i=300)
        check("坏文档抛错（调用方据此降级 LibreOffice）", False, "没有抛错")
    except RuntimeError as e:
        check("坏文档抛错（调用方据此降级 LibreOffice）", "转换失败" in str(e) or "fake" in str(e),
              str(e)[:60])
    ok_after = convert(i=301)
    check("失败一次后**同一个实例**继续可用（dispatch 仍为 1）",
          STATE["dispatch"] == 1 and os.path.isfile(ok_after), f"dispatch={STATE['dispatch']}")
    check("实例没有被 Quit", STATE["quit"] == 0, f"quit={STATE['quit']}")

    _reset_state(fail_remaining=2)
    for i in (400, 401):
        try:
            convert(i)
        except RuntimeError:
            pass
    check("连续两次失败后实例被 Quit（不再复用一个可能已损坏的实例）",
          STATE["quit"] >= 1, f"quit={STATE['quit']}")
    disp_before = STATE["dispatch"]
    rebuilt = convert(i=402)
    check("★ 重建新实例后转换恢复正常",
          os.path.isfile(rebuilt) and STATE["dispatch"] == disp_before + 1,
          f"dispatch={STATE['dispatch']}（前 {disp_before}）")

    # ============================================================
    section("E. 空闲回收：TTL 到期实例自动 Quit 退出")
    C.shutdown_office_pools()
    _reset_state()
    C._convert_pool_idle_ttl = lambda: 0.3      # 测试里缩短到 0.3s（真值 90s 太慢）
    convert(i=500)
    time.sleep(1.2)
    check("★ 空闲超时后实例 Quit（不长期占内存/句柄）", STATE["quit"] >= 1,
          f"quit={STATE['quit']}")
    check("空闲线程已退出（线程结束即成垃圾，下次挑选时才从池里清掉）",
          all(not w.is_alive() for pool in list(getattr(C, "_pools", {}).values())
              for w in getattr(pool, "_workers", [])),
          "仍有存活线程")
    again = convert(i=501)
    check("空闲退出后下一次转换按需重建实例并成功",
          os.path.isfile(again) and STATE["dispatch"] >= 2, f"dispatch={STATE['dispatch']}")

    # ============================================================
    section("F. shutdown_office_pools()：常驻实例全部 Quit，线程退出")
    C.shutdown_office_pools()
    _reset_state()
    C._convert_pool_idle_ttl = _orig_idle_ttl
    convert(i=600)
    check("转换后实例存活", STATE["quit"] == 0)
    C.shutdown_office_pools()
    time.sleep(0.3)
    check("★ shutdown 后实例已 Quit", STATE["quit"] == 1, f"quit={STATE['quit']}")
    check("shutdown 后池已清空", not C._pools, f"pools={list(C._pools)}")

    # ============================================================
    section("G. HN_CONVERT_POOL=0：完全回到老路径（每文件新实例 + Quit）")
    C.shutdown_office_pools()
    os.environ["HN_CONVERT_POOL"] = "0"
    _reset_state()
    for i in range(3):
        convert(i=700 + i)
    check("★ 关闭池后每份文件各创建一个实例", STATE["dispatch"] == 3,
          f"dispatch={STATE['dispatch']}")
    check("★ 关闭池后每份文件转完都 Quit", STATE["quit"] == 3, f"quit={STATE['quit']}")
    check("关闭池后产物齐全", STATE["export"] == 3, f"export={STATE['export']}")
    os.environ.pop("HN_CONVERT_POOL", None)

    # ============================================================
    section("H. 开关默认值与环境变量解析")
    os.environ.pop("HN_CONVERT_POOL", None)
    check("默认启用实例池", C._convert_pool_enabled() is True)
    os.environ["HN_CONVERT_POOL"] = "0"
    check("HN_CONVERT_POOL=0 ⇒ 关闭", C._convert_pool_enabled() is False)
    os.environ["HN_CONVERT_POOL"] = "false"
    check("HN_CONVERT_POOL=false ⇒ 关闭", C._convert_pool_enabled() is False)
    os.environ.pop("HN_CONVERT_POOL", None)
    os.environ["HN_CONVERT_POOL_SIZE"] = "99"
    check("池大小被夹在 1~4（99 ⇒ 4）", C._convert_pool_size() == 4, str(C._convert_pool_size()))
    os.environ["HN_CONVERT_POOL_SIZE"] = "0"
    check("池大小被夹在 1~4（0 ⇒ 1）", C._convert_pool_size() == 1, str(C._convert_pool_size()))
    os.environ.pop("HN_CONVERT_POOL_SIZE", None)
    check("默认池大小 = 2", C._convert_pool_size() == 2, str(C._convert_pool_size()))

finally:
    C.shutdown_office_pools()
    C._safe_word_dispatch = _orig_word_dispatch
    C._safe_wps_dispatch = _orig_wps_dispatch
    C._harden_office_com_app = _orig_harden
    C._convert_pool_idle_ttl = _orig_idle_ttl

print("\n" + ("全部通过" if ok_all else "存在失败项"))
sys.exit(0 if ok_all else 1)
