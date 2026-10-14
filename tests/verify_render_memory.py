"""verify_render_memory.py — 逐页渲染打印管线的内存/GDI 句柄安全验收探针。

**（2026-10-14 重写）** 本脚本原先"只复刻 pdf_printer.print_plan_pass 里逐页那几行"来测内存形状
—— 它测的**不是真循环**：把真循环改成"把每面渲染结果存进列表"（`_keep_img.append(rendered_im)`），
这个探针照样 exit 0（审查报告 D1-🔴12 已实测）。现在改为**驱动真实的 `print_plan_pass`**：

  · 用 `manual_duplex.plan_single()` 造**真实** PrintPlan，走 `print_plan_pass` → `print_pdf`
    → `_print_pdf_native` 的真逐页循环；
  · 只把 **win32 的"打印机 DC / 文档生命周期"换掉**（伪 `win32print`/`win32ui`），句柄指向一个
    **真实的内存 DC**（`gdi32.CreateCompatibleDC`）⇒ `ImageWin.Dib.draw` 走的仍是真正的
    StretchDIBits（内存→内存），**不出纸、不碰任何打印机队列**；
  · PyMuPDF 渲染、PIL 位图、Dib 分配**全是真的** —— 内存/GDI 形状才是被测对象的真形状。

判定（退出码 0 = 通过）：
  ① 真实管线逐面 RSS 漂移 < 3 个单页位图（允许分配器抖动，不允许累积）；
  ② GDI 句柄增量 ≤ 5（Dib 未释放会随页数涨，Windows 上限 10000 先耗尽）；
  ③ **内建反证**：把真实循环里的渲染结果改成"被循环外容器持有"（等价于审查员的变异），
     漂移判据**必须报红** —— 否则它就是"永远为绿的假守卫"。这一条是本脚本的存在理由。

用法：
    python tests/verify_render_memory.py                    # ①+②+③（默认 200 面 @300dpi）
    python tests/verify_render_memory.py --pages 600 --dpi 400
    python tests/verify_render_memory.py --no-self-check    # 跳过反证与第二趟，仅调参用
    python tests/verify_render_memory.py --hoard            # 只跑"持有即堆积"测量档（量化后果）

历史测量档（不参与默认判定，用于已决策的比较）：
    --blank-check / --prerotate-check / --rotate-mode {bitmap,render}

背景数字（RGB 3 字节/像素，A4 = 8.27×11.69 inch）：
    200dpi → 1654×2339   ≈ 11.6 MiB/页
    300dpi → 2480×3509   ≈ 24.9 MiB/页（官方"标清"）
    400dpi → 3306×4678   ≈ 44.2 MiB/页（官方默认）
    600dpi → 4962×7014   ≈ 99.6 MiB/页（官方"高清"，也是旧空白页的尺寸）
  实测单页流水线瞬时峰值 ≈ 5.7 个单页位图（pixmap + transpose 副本 + Dib 副本 + 分配器抖动）。
  旧空白占位页按打印机原生 DPI 的设备像素开整页 RGB 图 → 600dpi 下 99.6 MiB/张，比内容页贵；
  现已改为复用一个 8×8 纯白 DIB 拉伸到整页（成本与张数无关）。
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import gc
import io
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "local_print_tool"))

MB = 1024 * 1024


# ── Windows 计数器（ctypes，不依赖 pywin32 / psutil）──

class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform == "win32" else None
_u32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None


def _init_winapi():
    """必须显式声明 restype：GetCurrentProcess 返回伪句柄 (HANDLE)-1，
    ctypes 默认 restype=c_int 会把它截断成 32 位 → 句柄非法、所有计数器静默返回 0。"""
    if sys.platform != "win32":
        return
    _k32.GetCurrentProcess.restype = ctypes.c_void_p
    for dll, name in ((_k32, "K32GetProcessMemoryInfo"),):
        try:
            fn = getattr(dll, name)
        except AttributeError:
            continue
        fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), ctypes.c_ulong]
        fn.restype = ctypes.c_int
    _u32.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    _u32.GetGuiResources.restype = ctypes.c_ulong


def _mem() -> tuple[float, float]:
    """(当前工作集 MiB, 峰值工作集 MiB)"""
    if sys.platform != "win32":
        return 0.0, 0.0
    c = PROCESS_MEMORY_COUNTERS()
    c.cb = ctypes.sizeof(c)
    fn = getattr(_k32, "K32GetProcessMemoryInfo", None)
    if fn is None:
        import warnings
        warnings.warn("K32GetProcessMemoryInfo 不可用，内存计数为 0")
        return 0.0, 0.0
    if not fn(_k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
        return 0.0, 0.0
    return c.WorkingSetSize / MB, c.PeakWorkingSetSize / MB


def _gdi() -> int:
    """本进程当前 GDI 对象数（泄漏位图/DC 会持续上涨；Windows 默认上限 10000）"""
    if sys.platform != "win32":
        return -1
    return int(_u32.GetGuiResources(_k32.GetCurrentProcess(), 0))


# ── 造一份多页 PDF（A4，含文字，避免全空页影响渲染成本）──

def _make_pdf(path: str, pages: int) -> None:
    import fitz
    doc = fitz.open()
    for i in range(pages):
        p = doc.new_page(width=595, height=842)     # A4 pt
        p.insert_text((72, 100), f"page {i + 1} / {pages}", fontsize=14)
        # 铺一点图形，让渲染有真实工作量（纯文字页渲染太快、内存抖动看不出来）
        for k in range(12):
            p.draw_line(fitz.Point(60, 140 + k * 40), fitz.Point(535, 140 + k * 40))
    doc.save(path)
    doc.close()


# ============================================================
# 伪 DC 层：只替换 win32 的"设备/文档"接口，fitz 与 PIL 全用真的
# ============================================================

class _Rec:
    def __init__(self):
        self.startdoc = 0
        self.enddoc = 0
        self.abort = 0
        self.pages = 0
        self.printer = ""


class _FakeDC:
    """伪打印机 DC。

    句柄不是一个瞎编的整数，而是一个**真实的内存 DC**（`gdi32.CreateCompatibleDC`）：
    `ImageWin.Dib.draw(hdc, ...)` 于是走的仍是真正的 `StretchDIBits`（内存→内存），
    只把"目标表面"从打印机换成 1×1 默认位图 ⇒ GDI 直接裁剪，不出纸、不产生打印任务。
    """

    def __init__(self, rec: _Rec, hdc: int, native_dpi: int):
        self._rec, self._hdc, self._dpi = rec, hdc, native_dpi

    def CreatePrinterDC(self, name):
        self._rec.printer = name

    def GetHandleOutput(self):
        return self._hdc

    def GetDeviceCaps(self, which):
        # win32con：LOGPIXELSX=88 / LOGPIXELSY=90 / HORZRES=8 / VERTRES=10
        return {88: self._dpi, 90: self._dpi,
                8: int(8.27 * self._dpi), 10: int(11.69 * self._dpi)}[which]

    def StartDoc(self, title):
        self._rec.startdoc += 1

    def StartPage(self):
        self._rec.pages += 1

    def EndPage(self):
        pass

    def EndDoc(self):
        self._rec.enddoc += 1

    def AbortDoc(self):
        self._rec.abort += 1

    def DeleteDC(self):
        pass


@contextlib.contextmanager
def _fake_print_env(native_dpi: int):
    """注入伪 win32print / win32ui；yield (rec, hdc)。

    刻意**不提供** `win32print.OpenPrinter`：`_get_printer_devmode` 走它自己的 except 兜底
    （返回 None），本脚本走的是"手动双面/校准"路径（duplex='off' + pages_override），
    DEVMODE 拿不到是允许的（不打印机的单面路径），且这条分支本身由 verify_duplex_probe.py 覆盖。
    """
    saved = {k: sys.modules.get(k) for k in ("win32print", "win32ui")}
    rec = _Rec()
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi.DeleteDC.argtypes = [ctypes.c_void_p]
    hdc = gdi.CreateCompatibleDC(None)

    wpr = types.ModuleType("win32print")
    wpr.GetDefaultPrinter = lambda: "HN-MEMPROBE(无设备)"
    wui = types.ModuleType("win32ui")
    wui.CreateDC = lambda: _FakeDC(rec, hdc, native_dpi)
    sys.modules["win32print"] = wpr
    sys.modules["win32ui"] = wui
    try:
        yield rec, hdc
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        gdi.DeleteDC(hdc)


@contextlib.contextmanager
def _retaining_dib():
    """**变异**：让真实逐页循环里每一面的渲染结果都被循环外的容器持有。

    与审查员对 `pdf_printer.py` 做的改动（`_keep_img.append(rendered_im)`）**等价** ——
    `Dib(img)` 是循环里每一面必然要走的构造点，把它的入参（渲染结果）扣下来，
    就复现了"渲染好的页面堆积"。
    """
    from PIL import ImageWin
    orig = ImageWin.Dib
    held: list = []

    class _Leaky:
        def __init__(self, img, *a, **kw):
            held.append(img)                     # ← 泄漏点
            self._inner = orig(img, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    ImageWin.Dib = _Leaky
    try:
        yield held
    finally:
        ImageWin.Dib = orig
        held.clear()


# ============================================================
# 主流程：驱动**真实** print_plan_pass，逐面采样
# ============================================================

def real_pipeline_probe(pages: int, dpi: int, native_dpi: int, sample_every: int,
                        which: int = 1, retain: bool = False,
                        label: str = "") -> tuple[int, dict]:
    """跑真实 `print_plan_pass` 一趟，返回 (退出码, 结果字典)。

    retain=True 走变异档（见 `_retaining_dib`）：此时**期望**看到漂移，
    看不到漂移同样算失败 —— 说明判据不灵，它是假守卫。
    """
    import fitz
    from PIL import Image          # noqa: F401  （真实 renderer 会用到）
    import manual_duplex as md
    import pdf_printer as pp

    tag = label or (f"第{which}趟" + ("（变异：持有渲染结果）" if retain else ""))
    tmp = os.path.join(tempfile.gettempdir(), "hn_memprobe.pdf")
    _make_pdf(tmp, pages)
    print(f"\n=== {tag} ===\n探测文件: {tmp}  ({pages} 页, {os.path.getsize(tmp) / MB:.1f} MB)")
    print(f"参数: 渲染 {dpi} dpi, 打印机原生 {native_dpi} dpi, 逐面序列来自 print_plan_pass(which={which})")

    # 单页位图字节数：循环外单独渲染第 0 页量一次（不进入被测循环，不影响漂移判据）
    doc = fitz.open(tmp)
    _mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    _px = doc[0].get_pixmap(matrix=_mat, alpha=False)
    page_bytes = _px.height * _px.stride
    del _px
    doc.close()
    print(f"单页位图: ≈ {page_bytes / MB:.1f} MiB（阈值 = 3 个单页位图 = {3 * page_bytes / MB:.1f} MiB）")

    plan = md.plan_single(list(range(1, pages + 1)))
    seq = plan.pass_page_indices(which)
    blanks = sum(1 for p in seq if p is None)
    print(f"本次逐面序列: {len(seq)} 面（含 {blanks} 个空白占位页）")

    samples: list[tuple[int, float, int]] = []
    base_rss = base_peak = 0.0
    base_gdi = end_gdi = 0
    elapsed = 0.0
    ok_run = False
    run_msg = ""
    caught = ""
    rec = None

    with _fake_print_env(native_dpi) as (rec, _hdc):
        gc.collect()
        base_rss, base_peak = _mem()
        base_gdi = _gdi()
        print(f"基线: 工作集 {base_rss:.1f} MB / 峰值 {base_peak:.1f} MB / GDI {base_gdi}")

        total_sides = len(seq)

        def cb(cur, total):
            if cur % sample_every == 0 or cur == total:
                r, _ = _mem()
                samples.append((cur, r, _gdi()))

        ctx = _retaining_dib() if retain else contextlib.nullcontext()
        t0 = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            try:
                with ctx:
                    ok_run, run_msg = pp.print_plan_pass(
                        plan, tmp, printer_name="", which=which,
                        second_rotate_180=True, progress_callback=cb, dpi=dpi)
            finally:
                caught = buf.getvalue()
        elapsed = time.perf_counter() - t0
        end_rss, end_peak = _mem()
        end_gdi = _gdi()

    for cur, r, g in samples:
        print(f"  第 {cur:>5} 面  工作集 {r:>8.1f} MB  GDI {g}")

    n = max(1, len(samples) // 5)
    head = sum(s[1] for s in samples[:n]) / n if samples else 0.0
    tail = sum(s[1] for s in samples[-n:]) / n if samples else 0.0
    drift = tail - head
    gdi_delta = end_gdi - base_gdi
    pb = page_bytes / MB

    print(f"真实管线: ok={ok_run} msg={run_msg!r}  "
          f"（StartDoc {rec.startdoc if rec else '?'} / StartPage {rec.pages if rec else '?'} / "
          f"EndDoc {rec.enddoc if rec else '?'} / AbortDoc {rec.abort if rec else '?'}）")
    print(f"耗时 {elapsed:.1f}s（{len(seq) / max(elapsed, 1e-6):.1f} 面/秒）")
    print(f"首段工作集均值 {head:.1f} MB → 末段 {tail:.1f} MB  漂移 {drift:+.1f} MB "
          f"（= {drift / max(pb, 1e-6):+.2f} 个单页位图）")
    # 采样到的最大工作集：等价于"页内瞬时重叠"在采样分辨率下的上界（首帧已含渲染器预热）
    first_rss = samples[0][1]
    max_rss = max(s[1] for s in samples)
    print(f"采样最大工作集 {max_rss:.1f} MB（本档首帧 {first_rss:.1f} MB，"
          f"Δ{max_rss - first_rss:+.1f} MB = {(max_rss - first_rss) / max(pb, 1e-6):+.2f} 个单页位图）")
    print(f"进程峰值工作集 {end_peak:.1f} MB（自进程启动单调递增，**跨档不可比**；"
          f"首档可比：基线 {base_peak:.1f} MB → {end_peak:.1f} MB，"
          f"≈ {(end_peak - base_peak) / max(pb, 1e-6):.1f} 个单页位图）")
    print(f"GDI 句柄 {base_gdi} → {end_gdi}（Δ{gdi_delta:+d}）")

    res = {"ok_run": ok_run, "msg": run_msg, "drift": drift, "page_bytes": page_bytes,
           "gdi_delta": gdi_delta, "samples": len(samples), "elapsed": elapsed,
           "caught": caught, "rec": rec}

    rc = 0
    # ⓪ 前提：真实管线必须真的跑起来了（否则后面所有"没漂移"都是假绿）
    if not ok_run or "打印成功 (GDI" not in (run_msg or ""):
        print("✗ 真实 print_plan_pass 未成功返回 —— 探针前提不成立，判失败")
        print("  （最后 15 行真实输出：" + "\n   ".join(caught.strip().splitlines()[-15:]) + "）")
        res["rc"] = 1
        return 1, res
    if not samples:
        print("✗ 一帧采样都没有 —— progress_callback 没被调用，说明测的不是真循环")
        res["rc"] = 1
        return 1, res

    if retain:
        if drift > 3 * pb:
            print(f"✓ 反证成立：持有渲染结果后内存线性增长 {drift:+.0f} MiB "
                  f"（{pages} 面全堆积预计需 {pb * pages / 1024:.1f} GiB）")
        else:
            print(f"✗ 反证失败：变异后漂移只有 {drift:+.1f} MiB（阈值 {3 * pb:.1f}）"
                  f" —— 内存判据是**假守卫**，测不出「渲染结果被攒起来」")
            rc = 1
    else:
        if drift > 3 * pb:
            print(f"✗ 内存随页数增长（漂移 {drift:+.1f} MB 超过 3 个单页位图）→ 存在堆积/未释放")
            rc = 1
        else:
            print("✓ 内存不随页数增长：真实管线是 O(1) 的，长任务安全")

    if pages >= 100 and gdi_delta > 5:
        print(f"✗ GDI 句柄随页数累积（Δ{gdi_delta}）→ ImageWin.Dib 未释放，"
              f"Windows 上限 10000 个会先耗尽")
        rc = 1
    else:
        print(f"✓ GDI 句柄无累积（Δ{gdi_delta}）")

    res["rc"] = rc
    return rc, res


def blank_probe(native_dpi: int, count: int = 20) -> int:
    """空白占位页成本：**旧实现 vs 现实现**。

    旧（已修复）：`Image.new("RGB", (printable_w, printable_h))` —— printable_* 是打印机
                  **原生 DPI 的设备像素**，600 dpi 的 A4 ≈ 99.6 MiB/张，比内容页还贵。
    新（现行）：复用同一个 8×8 纯白 DIB 拉伸到整页 ⇒ 单张持有成本 ≈ 0，与张数无关。
    """
    from PIL import Image, ImageWin
    w = int(8.27 * native_dpi)
    h = int(11.69 * native_dpi)
    per = w * h * 3 / MB
    print(f"打印机原生 {native_dpi} dpi → 可打印区 {w}×{h} 设备像素")
    print(f"每张空白页若按设备像素开 RGB 图 ≈ {per:.1f} MiB\n")

    # ── 旧实现：每张都开一张整页大图（持有到最后，暴露真实量级）──
    gc.collect()
    b0, _ = _mem()
    g0 = _gdi()
    kept = []
    for _ in range(count):
        kept.append(ImageWin.Dib(Image.new("RGB", (w, h), "white")))
    a0, p0 = _mem()
    gd0 = _gdi()
    print(f"旧实现：{count} 张整页大图  工作集 {b0:.1f} → {a0:.1f} MiB "
          f"(Δ{a0 - b0:+.1f})  峰值 {p0:.1f} MiB  GDI Δ{gd0 - g0:+d}")
    del kept

    # ── 新实现：一个 8×8 DIB 反复使用（此处只测持有成本；draw 需要真 DC）──
    gc.collect()
    b1, _ = _mem()
    g1 = _gdi()
    small = ImageWin.Dib(Image.new("RGB", (8, 8), "white"))
    for _ in range(count):
        pass                      # 真实路径是 small.draw(hdc, (0, 0, w, h))，需打印机 DC
    a1, p1 = _mem()
    gd1 = _gdi()
    print(f"现实现：同一个 8×8 DIB 复用 {count} 次  工作集 {b1:.1f} → {a1:.1f} MiB "
          f"(Δ{a1 - b1:+.1f})  峰值 {p1:.1f} MiB  GDI Δ{gd1 - g1:+d}")

    ok = (a1 - b1) < 1.0 and (gd1 - g1) <= 2
    print()
    if ok:
        print(f"✓ 空白占位页成本与张数无关：{per:.1f} MiB/张 → ≈0 "
              f"（省下 {per:.1f} MiB × 每张空白）")
    else:
        print(f"✗ 现实现仍在按张分配内存（Δ{a1 - b1:+.1f} MiB）")
    return 0 if ok else 1


def _fix_console_encoding():
    """本仓库 Windows 环境控制台是 GBK：中文 print 会乱码、✓/✗ 直接抛
    UnicodeEncodeError。统一改 UTF-8 并容错，避免验收脚本"死在最后一行 print"。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def rotate_mode_probe(mode: str, pages: int, dpi: int) -> int:
    """**单方案、单进程**跑一遍，报进程级峰值工作集增量。

    为什么必须分进程：`PROCESS_MEMORY_COUNTERS.PeakWorkingSetSize` 是**进程生命周期单调**的，
    同一进程里先跑 A 再跑 B，B 的峰值会被 A 抬高的水位掩盖；而 `prerotate` 想省掉的正是
    **页内瞬时**重叠（原图 + 旋转副本 + Dib 副本同时存活），页与页之间采样根本看不到。
    """
    import fitz
    from PIL import Image, ImageWin

    _rot180 = getattr(getattr(Image, "Transpose", None), "ROTATE_180", None) \
        or getattr(Image, "ROTATE_180", None)
    tmp = os.path.join(tempfile.gettempdir(), "hn_rotmode.pdf")
    _make_pdf(tmp, max(1, pages))
    doc = fitz.open(tmp)
    gc.collect()
    base, peak_before = _mem()          # 峰值水位：建 doc 之后、进循环之前
    t = time.perf_counter()
    try:
        for i in range(len(doc)):
            mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
            if mode == "render":
                mat = mat.prerotate(180)      # 渲染期旋转
            px = doc[i].get_pixmap(matrix=mat, alpha=False)
            im = Image.frombuffer("RGB", (px.width, px.height), px.samples,
                                  "raw", "RGB", px.stride, 1)
            if mode == "bitmap" and _rot180 is not None:
                im = im.transpose(_rot180)    # 现状：位图旋转
            _dib = ImageWin.Dib(im)
    finally:
        doc.close()
    el = time.perf_counter() - t
    _, peak_after = _mem()
    label = {"bitmap": "位图旋转（现状）", "render": "渲染期 prerotate(180)"}[mode]
    print(f"{label}: {pages} 面 @{dpi}dpi  "
          f"进程峰值增量 {peak_after - peak_before:7.1f} MiB  耗时 {el:5.2f}s")
    try:
        os.remove(tmp)
    except OSError:
        pass
    return 0


def prerotate_probe(pages: int, dpi: int) -> int:
    """对照：**位图旋转**（现状 transpose(ROTATE_180)） vs **渲染期旋转**（Matrix.prerotate(180)）。

    这两个方案要回答三件事，缺一不可：
      ① 内存：prerotate 省掉一次整页拷贝 —— 到底省多少（采样到的最大工作集）
      ② 像素：两者出图是否**逐像素等价** —— 不等价则这个优化根本不成立
      ③ 耗时：渲染期旋转是否引入额外代价（旋转后光栅化的工作量并不一定相同）

    ② 是决策项：只有逐像素等价，才谈得上"省内存的免费优化"。
    """
    import fitz
    from PIL import Image, ImageWin

    _rot180 = getattr(getattr(Image, "Transpose", None), "ROTATE_180", None) \
        or getattr(Image, "ROTATE_180", None)
    tmp = os.path.join(tempfile.gettempdir(), "hn_prerotate.pdf")
    _make_pdf(tmp, max(1, pages))
    doc = fitz.open(tmp)

    # ── ② 像素等价性（先用第 1 页、低 DPI 快速判定；这一步决定后面还测不测）──
    page = doc[0]
    mat_bitmap = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    p1 = page.get_pixmap(matrix=mat_bitmap, alpha=False)
    img_bitmap = Image.frombuffer("RGB", (p1.width, p1.height), p1.samples,
                                  "raw", "RGB", p1.stride, 1).transpose(_rot180)
    b1 = img_bitmap.tobytes()

    mat_render = fitz.Matrix(dpi / 72.0, dpi / 72.0).prerotate(180)
    p2 = page.get_pixmap(matrix=mat_render, alpha=False)
    img_render = Image.frombuffer("RGB", (p2.width, p2.height), p2.samples,
                                  "raw", "RGB", p2.stride, 1)
    b2 = img_render.tobytes()

    same_size = (p1.width, p1.height) == (p2.width, p2.height)
    print(f"尺寸：位图旋转 {p1.width}×{p1.height} / 渲染期旋转 {p2.width}×{p2.height}  "
          f"→ {'一致' if same_size else '★不一致'}")
    if not same_size or len(b1) != len(b2):
        print("✗ 尺寸不一致：渲染期旋转改变了输出尺寸，不能当作等价替换")
        doc.close()
        return 1
    identical = (b1 == b2)
    if identical:
        print("✓ 像素：两种方式**逐字节完全一致**")
    else:
        diffs = [abs(x - y) for x, y in zip(b1, b2) if x != y]
        n_diff = len(diffs)
        mean_d = sum(diffs) / max(1, n_diff)
        max_d = max(diffs) if diffs else 0
        # 按行统计差像素分布：抗锯齿差异会沿全部内容边缘稀疏铺开；
        # 错位/方向错则是局部大片或整体平移。
        row = p1.width * 3
        rows_with_diff = 0
        for r in range(0, len(b1), row):
            if b1[r:r + row] != b2[r:r + row]:
                rows_with_diff += 1
        print(f"✗ 像素：{n_diff:,} 字节不同（占 {n_diff / len(b1) * 100:.2f}%）"
              f"，涉及 {rows_with_diff}/{p1.height} 行")
        print(f"  差值幅度：平均 {mean_d:.2f} / 最大 {max_d}（满量程 255）")
        # 判据用**空间分布**而不是单一幅度阈值：旋转相位不同只会让"边缘像素的覆盖率"变，
        # 影响范围限于内容边缘（很小的像素占比）；方向错/错位则会波及大片区域。
        px_total = p1.width * p1.height
        px_diff = n_diff / 3
        frac = px_diff / max(1, px_total)
        if frac < 0.02 and rows_with_diff < p1.height * 0.2:
            print(f"  → 差异仅落在抗锯齿边缘（{frac * 100:.2f}% 像素 / {rows_with_diff} 行）："
                  f"是**光栅化相位**变了，不是方向错或错位，视觉上不可辨")
        else:
            print(f"  → ★差异波及 {frac * 100:.2f}% 像素、{rows_with_diff} 行："
                  f"属结构性差异（方向错/错位），绝不可替换")

    # ── ①③ 分别测每个方案：采样最大工作集 + 耗时 ──
    def _run(use_prerotate: bool) -> tuple[float, float]:
        gc.collect()
        base, _ = _mem()
        peak = base
        t = time.perf_counter()
        d = fitz.open(tmp)
        try:
            for i in range(len(d)):
                mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
                if use_prerotate:
                    mat = mat.prerotate(180)
                px = d[i].get_pixmap(matrix=mat, alpha=False)
                im = Image.frombuffer("RGB", (px.width, px.height), px.samples,
                                      "raw", "RGB", px.stride, 1)
                if not use_prerotate and _rot180 is not None:
                    im = im.transpose(_rot180)
                _dib = ImageWin.Dib(im)
                cur, _ = _mem()
                peak = max(peak, cur)
        finally:
            d.close()
        return peak - base, time.perf_counter() - t

    n = len(doc)
    print(f"\n各跑 {n} 面 @ {dpi} dpi（同一进程内先后执行，各自采样最大工作集）：")
    peak_a, t_a = _run(False)
    print(f"  位图旋转（现状）  ：采样最大工作集增量 {peak_a:7.1f} MiB   耗时 {t_a:5.1f}s")
    peak_b, t_b = _run(True)
    print(f"  渲染期旋转        ：采样最大工作集增量 {peak_b:7.1f} MiB   耗时 {t_b:5.1f}s")
    print(f"  → 内存 {'省 ' + format(peak_a - peak_b, '.1f') + ' MiB' if peak_b < peak_a else '★没省（%+.1f MiB）' % (peak_b - peak_a)}"
          f"，耗时 {'快 ' + format(t_a - t_b, '.1f') + 's' if t_b < t_a else '慢 ' + format(t_b - t_a, '.1f') + 's'}")

    if not identical:
        print(f"\n⚠ 像素不等价（光栅化相位差异）→ 不是等价替换。")
        print(f"  收益实测：进程峰值 {'无差异' if abs(peak_a - peak_b) < 2 else format(peak_a - peak_b, '.1f') + ' MiB'}"
              f"（分进程实测 141.9 vs 142.0 MiB @300dpi/200 面 —— 页内峰值由 MuPDF 渲染内部"
              f"缓冲区主导，transpose 只占其中 1/5.7，被更大的水位掩盖）；")
        print(f"  每页省一次 {dpi} dpi 整幅拷贝的 CPU（上表耗时差），"
              f"而打印每面要 1~4 秒 ⇒ 渲染耗时被打印机物理速度掩盖。")
        print("  结论：**不采纳**。改一条已在实机验证过、且第二面方向只有打出来才知道的路径，"
              "换取实际为零的收益，不划算。")
    else:
        print(f"\n结论：像素逐字节等价，可省 {peak_a - peak_b:.1f} MiB 峰值"
              f"（若 ≤ 5 MiB 则不值得为它改动已实机验证过的打印路径）。")
    doc.close()
    try:
        os.remove(tmp)
    except OSError:
        pass
    return 0


def main() -> int:
    _fix_console_encoding()
    _init_winapi()
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=200)
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--native-dpi", type=int, default=600,
                    help="打印机原生 DPI（决定空白页设备像素尺寸，激光机常见 600）")
    ap.add_argument("--sample-every", type=int, default=10)
    ap.add_argument("--no-self-check", action="store_true",
                    help="跳过第二趟与内建反证（只做单趟 O(1) 判定；调参用）")
    ap.add_argument("--blank-check", action="store_true", help="只测空白占位页成本")
    ap.add_argument("--prerotate-check", action="store_true",
                    help="对照：位图旋转(现状) vs 渲染期 prerotate(180)：内存/像素等价/耗时")
    ap.add_argument("--rotate-mode", choices=["bitmap", "render"], default=None,
                    help="单方案单进程跑（进程峰值才准），配合两次调用比较")
    ap.add_argument("--hoard", action="store_true",
                    help="只跑「持有渲染结果」测量档（真实管线 + 变异），量化堆积的后果")
    a = ap.parse_args()

    if sys.platform != "win32":
        print("非 Windows：GDI 路径不适用，跳过（本机无打印子系统）")
        return 0
    if a.blank_check:
        return blank_probe(a.native_dpi)
    if a.prerotate_check:
        return prerotate_probe(a.pages, a.dpi)
    if a.rotate_mode:
        return rotate_mode_probe(a.rotate_mode, a.pages, a.dpi)

    rcs: list[int] = []

    if a.hoard:
        # 测量档：故意持有渲染结果，量化后果（此档**期望**看到增长）
        rc, _ = real_pipeline_probe(a.pages, a.dpi, a.native_dpi, a.sample_every,
                                    which=1, retain=True, label="测量档：持有每面渲染结果")
        print(f"\n→ 结论：真实管线本身 O(1)；一旦自己持有渲染结果，"
              f"{a.pages} 面需 {_mem()[1] / 1024:.1f} GiB 量级，必崩")
        return rc

    print("=" * 68)
    print("第 1 趟（which=1）—— 真实 print_plan_pass 主判定")
    print("=" * 68)
    rc1, _ = real_pipeline_probe(a.pages, a.dpi, a.native_dpi, a.sample_every, which=1)
    rcs.append(rc1)

    if not a.no_self_check:
        # 第 2 趟：走 rotate_180 补偿分支（第 2 面的 180° 补偿只在这一处定义）；
        # 用**奇数**页数，顺带覆盖"空白占位页复用同一个 8×8 DIB"的真实循环路径。
        p2 = 61 if a.pages >= 61 else (a.pages | 1)
        print("\n" + "=" * 68)
        print(f"第 2 趟（which=2，rotate_180 补偿 + {p2} 面的奇数页空白占位页）")
        print("=" * 68)
        rc2, _ = real_pipeline_probe(p2, a.dpi, a.native_dpi, 10, which=2)
        rcs.append(rc2)

        # 内建反证：变异后漂移判据必须报红。
        # 页数不能太小：漂移判据取"末 20% 采样 − 首 20% 采样"，首段里已经有几面被持有，
        # 页数太少会让"首段基线"抬得太高而漏判（实测 12 页 only 2.67 个单页位图，差一点就假绿）。
        print("\n" + "=" * 68)
        print("内建反证（变异：让真实循环持有每面渲染结果 —— 判据必须报红）")
        print("=" * 68)
        rc3, res3 = real_pipeline_probe(60, 200, a.native_dpi, 3, which=1, retain=True,
                                        label="反证档：真实管线 + 持有渲染结果")
        rcs.append(rc3)

    print("\n" + "=" * 68)
    bad = [r for r in rcs if r != 0]
    if bad:
        print(f"逐页渲染内存/GDI 验收: 存在失败 ❌（{len(bad)}/{len(rcs)} 档）")
    else:
        print(f"逐页渲染内存/GDI 验收: 全部通过 ✅（{len(rcs)} 档："
              + "、".join(["主判定", "第2趟", "反证"][:len(rcs)]) + "）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
