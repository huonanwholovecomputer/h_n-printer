# -*- coding: utf-8 -*-
"""独立验收 · 2026-10-17「单页 duplex=on」事故：修复前 RED 复现 + 修复后 GREEN 验收

事故（现场日志 %APPDATA%\\HN打印工具\\logs\\local_tool.log 17:48:43）：
    printer_name = "HP LaserJet 1020"（无自动双面器、驱动虚报 DC_DUPLEX=1）
    print_config.json 的 printer_profiles = **空**
    job = {page_count: 1, duplex: "on"} → 日志 `→ 正在打印 (份数:1, 双面:on ...)`
    → `✓ 打印成功 (GDI, 1 面)` —— 单次 GDI 作业把 duplex=on 交给了驱动
    → HP 驱动弹「双面打印说明」手动双面向导，多送一张空白纸走一趟。

本脚本**不复述作者说法**：桩掉底层 GDI 入口（`pdf_printer._print_pdf_native`）与
`gui.print_pdf`，用真实 `PrintWorker.run()` 观察**实际提交的参数**，并自己构造反例
（页数未知 / 页码范围只选 1 页 / 多页）。所有结论都来自桩观测，不来自读源码。

──────────────────────── 断言口径（修复后应有的行为）────────────────────────
硬断言（任一 FAIL ⇒ 退出码 1）：
  K1  空档案 ⇒ 该队列能力未确认（auto_duplex=None、manual_duplex=False）
  K2  空档案 ⇒ _uses_manual_duplex(1页 duplex=on) 为 False ⇒ 判定走驱动双面
  A1  1 页 + duplex="on" + 空档案 → **不得**有任何一次驱动级提交 duplex="on"
  A2  同一场景只提交 1 次（不该变成两趟/多次提交）
  B1  1 页 + duplex="on" + **手动双面档案** → 不得走两趟（不该白翻一次纸、白配一张空白占位）
  H1  3 页 PDF + page_range="2"（实际只打 1 页）+ duplex="on" → 不得提交 duplex="on"
  C1  3 页 + duplex="on" + 手动档案 → 两趟**仍然**发生（修单页不许把多页手动双面一起废掉）
  C2  同上 → 两趟全程单面（print_plan_pass 内部固定 duplex="off"）
  D1  3 页 + duplex="on" + 空档案 → 驱动级仍是 duplex="on"（多页仍走驱动双面）
  E1  1 页 + duplex="off" → 驱动级 duplex="off"（本来就单面的不受影响）
  G1  损坏 PDF（page_count=2，真实页数数不出来）+ duplex="on" + 空档案
      → 调用方实参仍是 duplex="on"：**页数读不出来 ≠ 1 页**，不许静默降级成单面
  G2  同上 + 手动档案 → 仍然尝试两趟（同上：未知页数保持旧行为）
  D1…D15  **最深一层兜底**（直调 pdf_printer.print_pdf，不经 gui）：1 页 ⇒ off；页码范围
      选多页 / 越界("999") / 非法("0","abc") / 倒序("3-1") / 智能拆分("23-4") ⇒ 仍 on；
      只选 1 页("2","1-1") ⇒ off；`pages_override`（手动双面/校准序列）**完全免疫**；
      1 页 ×2 份 ⇒ off；3 页 ×2 份 ⇒ on。另 D4′…D7′ 钉住"非空但全部无效 → 打印全部"
      的解析语义（否则那几条 'on' 只是碰巧）。

非判定观察（打印出来供判断，不参与退出码）：
  S  攒批路径：1 页 + duplex="on" + 手动档案 + stack_mode ⇒ stack_entry_planned 载荷
     （攒批里 1 页文件占 1 张纸、背面是空白占位，与是否标记 duplex 的物理结果相同）

──────────────────────── RED → GREEN ────────────────────────
**修复前会红在哪几条：A1、B1、H1**（正是事故本身与它同源的另两条路径）。其余为回归护栏。

跑法（RED 用修复前的代码快照，GREEN 用工作区）：
    cd /d/打印机项目
    PY="C:/Users/Administrator/AppData/Local/Programs/Python/Python311/python.exe"
    # GREEN：直接跑工作区
    PYTHONPATH= PYTHONUTF8=1 "$PY" tests/verify_incident_single_page_duplex.py
    # RED：把 HN_PRINT_CODE_ROOT 指向修复前的源码目录。两条都能复现（实测 A1/B1/H1 全红）：
    #   ① 已发布代码：git archive HEAD local_print_tool | tar -x -C <tmp>   → <tmp>/local_print_tool
    #   ② 当日修复前的工作区快照（含 native render 等未提交改动）
    HN_PRINT_CODE_ROOT=<pre-fix-dir> PYTHONPATH= PYTHONUTF8=1 "$PY" tests/verify_incident_single_page_duplex.py

⚠ 断言边界说明（避免误判「修复不完整」）：
  · A*/B*/C*/D*/E*/G*/H*/K* 的观测点都是**真实提交给底层 GDI 入口的 duplex**
    （`gui.print_pdf` 的实参另打印出来作对照）。所以无论修复落在调用方
    （PrintWorker 归一化）还是 print_pdf 内部，只要真实流程不再把 1 页按双面提交，本脚本就绿。
  · D 组是**直调** `pdf_printer.print_pdf(duplex="on")` 的硬断言（任务书要求的"最深一层兜底"）：
    这一层若被去掉（或 conditions 写宽了把越界范围误判成 1 页），D 组立刻转红。
  · 已知残留（本脚本不覆盖，见 test 头注释）：`page_count==0` **且原件本身是 PDF** 时，
    手动档案下仍会进两趟（驱动全程单面，但白走一张空白背面）；由既有断言
    tests/verify_manual_duplex_gui.py ①b 钉住，属 lead 裁定的范围外。

安全：APPDATA 指向临时沙箱（真实 %APPDATA% 只读当证据，绝不写）；离屏 Qt；
底层 GDI 入口被桩死 ⇒ 任何路径都不可能真出纸；不改任何源码。
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import traceback

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE_ROOT = os.path.abspath(os.environ.get("HN_PRINT_CODE_ROOT")
                            or os.path.join(REPO, "local_print_tool"))
SANDBOX = tempfile.mkdtemp(prefix="hn_dup_incident_")
os.environ["APPDATA"] = SANDBOX                    # paths.py 每次调用读 APPDATA
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, CODE_ROOT)
os.chdir(SANDBOX)

from PySide6.QtWidgets import QApplication                      # noqa: E402
app = QApplication.instance() or QApplication([])

import pdf_printer                                              # noqa: E402
import printer_config                                           # noqa: E402
import gui                                                      # noqa: E402
from printer_config import PrintJob, PrinterConfig, PrinterProfile  # noqa: E402

PN = "HP LaserJet 1020"
REAL_APPDATA_DIR = os.path.join(os.environ.get("USERPROFILE", ""),
                                "AppData", "Roaming", "HN打印工具")

CHECKS: list[tuple[str, str, bool, str]] = []
FAILS: list[str] = []


def check(cid: str, desc: str, cond: bool, detail: str = "") -> bool:
    ok = bool(cond)
    CHECKS.append((cid, desc, ok, detail))
    if not ok:
        FAILS.append(cid)
    print(f"  [{'PASS' if ok else 'FAIL'}] {cid:<3} {desc}"
          + (f"\n         —— {detail}" if detail else ""))
    return ok


def section(title: str) -> None:
    print("\n" + "═" * 78)
    print(title)
    print("═" * 78)


def info(msg: str) -> None:
    print(f"  · {msg}")


# ─────────────────────────── 0. 记录本次验证的代码快照 ───────────────────────────

def md5(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError:
        return "(缺失)"


section("0. 本次验证的代码基准（跑完请把这几行连同结论一起存档）")
print(f"  CODE_ROOT = {CODE_ROOT}")
for name in ("gui.py", "printer_config.py", "pdf_printer.py", "paths.py"):
    print(f"    {name:<20} md5={md5(os.path.join(CODE_ROOT, name))}")
print(f"  SANDBOX(APPDATA) = {SANDBOX}")
_single_page_marker = "单页硬闸门"
_gui_src = ""
try:
    with open(os.path.join(CODE_ROOT, "gui.py"), encoding="utf-8") as f:
        _gui_src = f.read()
except OSError:
    pass
print(f"  修复标记「{_single_page_marker}」在 gui.py 出现 {_gui_src.count(_single_page_marker)} 次"
      f"（0 = 修复前 / ≥1 = 已落地）")


# ─────────────────────────── 1. 现场证据（只读真实 %APPDATA%） ───────────────────────────

def section_1_real_evidence() -> dict:
    section("1. 现场证据（只读真实 %APPDATA%，不改一个字节）")
    real_cfg = os.path.join(REAL_APPDATA_DIR, "print_config.json")
    ev: dict = {}
    if not os.path.isfile(real_cfg):
        info(f"真实配置不存在（本机非事故机）：{real_cfg}")
        return ev
    ev["path"] = real_cfg
    ev["md5"] = md5(real_cfg)
    with open(real_cfg, encoding="utf-8") as f:
        raw = json.load(f)
    ev["printer_name"] = raw.get("printer_name", "")
    ev["profiles_keys"] = list((raw.get("printer_profiles") or {}).keys())
    jobs = []
    for tab in (raw.get("tabs") or {}).values():
        for j in (tab.get("jobs") or []):
            jobs.append({k: j.get(k) for k in
                         ("page_count", "duplex", "copies", "order_number", "sent")})
    ev["jobs"] = jobs
    print(f"  配置: {real_cfg}")
    print(f"        md5={ev['md5']}")
    print(f"        printer_name     = {ev['printer_name']!r}")
    print(f"        printer_profiles = {ev['profiles_keys']}  (2026-10-17 事故发生时空 = 能力未确认；"
          f"本脚本的场景一律用确定性空档案，不照抄这里)")
    for j in jobs:
        print(f"        job: page_count={j['page_count']} duplex={j['duplex']!r} "
              f"copies={j['copies']} order={j['order_number']!r} sent={j['sent']}")
    # 日志只看与本次事故同一时刻的那几行（只读）
    log = os.path.join(REAL_APPDATA_DIR, "logs", "local_tool.log")
    if os.path.isfile(log):
        try:
            with open(log, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            hit = [ln.rstrip() for ln in lines if "双面:on" in ln or "手动双面批次" in ln]
            ev["log_hits"] = hit[-6:]
            print(f"  日志（最后几条含 双面:on / 手动双面批次 的行）：")
            for ln in ev["log_hits"]:
                print(f"        {ln}")
        except OSError:
            pass
    return ev


# ─────────────────────────── 2. 最小 PDF 工厂（不依赖第三方库） ───────────────────────────

def pdf_bytes(n_pages: int) -> bytes:
    """手写最小 N 页 PDF（Type1 Helvetica，无需嵌入字体）。"""
    out = bytearray()
    page_ids = [3 + 2 * i for i in range(n_pages)]
    content_ids = [4 + 2 * i for i in range(n_pages)]
    font_id = 3 + 2 * n_pages
    objs: dict[int, bytes] = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objs[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode()
    for i, pid in enumerate(page_ids):
        objs[pid] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                     f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                     f"/Contents {content_ids[i]} 0 R >>").encode()
        stream = f"BT /F1 24 Tf 72 760 Td (Page {i + 1}) Tj ET".encode()
        objs[content_ids[i]] = (b"<< /Length " + str(len(stream)).encode()
                                + b" >>\nstream\n" + stream + b"\nendstream")
    objs[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    offs: dict[int, int] = {}
    for oid in sorted(objs):
        offs[oid] = len(out)
        out.extend(f"{oid} 0 obj\n".encode())
        out.extend(objs[oid])
        out.extend(b"\nendobj\n")
    xref = len(out)
    n = font_id + 1
    out.extend(f"xref\n0 {n}\n".encode())
    out.extend(b"0000000000 65535 f \n")
    for oid in range(1, n):
        out.extend(f"{offs[oid]:010d} 00000 n \n".encode())
    out.extend(f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(out)


def make_file(name: str, content: bytes) -> str:
    p = os.path.join(SANDBOX, name)
    with open(p, "wb") as f:
        f.write(content)
    return p


PDF1 = make_file("one_page.pdf", pdf_bytes(1))
PDF3 = make_file("three_page.pdf", pdf_bytes(3))
PDF10 = make_file("ten_page.pdf", pdf_bytes(10))
PDF_BROKEN = make_file("broken.pdf", b"%PDF-1.4\nthis is not a real pdf\n%%EOF\n")

# 沙箱配置：printer_name / duplex_mode / render_dpi 照抄真实配置（与事故机同形状），
# 但 **printer_profiles 一律写成确定性的空 dict** —— 不再照抄真实文件里的档案内容。
# 为什么：真实机器上档案会被恢复/被写（2026-10-17 现场就把档案写回来了，print_config.json
# 的镜像随之带上 `HP LaserJet 1020`），一旦照抄，"空档案 ⇒ 走驱动双面" 这组事故场景就会
# 假红（实测：run_all 里 K1/D1/G1 三条报红，其实是对真实文件内容的耦合，不是产品回归）。
# 真实文件的内容仍会在第 1 节**作为证据打印**（只读），只是不再参与构造场景。
REAL = {}
_real_cfg_path = os.path.join(REAL_APPDATA_DIR, "print_config.json")
if os.path.isfile(_real_cfg_path):
    try:
        with open(_real_cfg_path, encoding="utf-8") as f:
            REAL = json.load(f)
    except (OSError, ValueError):
        REAL = {}
SANDBOX_CFG = os.path.join(SANDBOX, "print_config.json")
with open(SANDBOX_CFG, "w", encoding="utf-8") as f:
    json.dump({
        "printer_name": REAL.get("printer_name") or PN,
        "duplex_mode": REAL.get("duplex_mode", "long-edge"),
        "render_dpi": REAL.get("render_dpi", 0),
        "printer_profiles": {},
    }, f, ensure_ascii=False, indent=2)

CFG = PrinterConfig.load(SANDBOX_CFG)
PROFILE_EMPTY = CFG.get_profile(CFG.printer_name or PN)
PROFILE_MANUAL = PrinterProfile(auto_duplex=False, capability_source="manual",
                                manual_duplex=True, first="odd", reverse_second=True,
                                second_rotate_180=True, verified_at="2026-10-14 21:00",
                                verified_by="实机校准")


# ─────────────────────────── 3. 桩：底层 GDI 入口 + gui.print_pdf ───────────────────────────

_REAL_PRINT_PDF = pdf_printer.print_pdf
_REAL_PRINT_PLAN_PASS = pdf_printer.print_plan_pass
_REAL_MERGE = pdf_printer.merge_pdfs
_REAL_DUPLEX_SUPPORT = pdf_printer.get_printer_duplex_support
_REAL_WAIT = gui.wait_for_output

# 与事故机一致的能力虚报（文档记载）：驱动报 auto_duplex=true/medium ⇒ 不触发 pdf_printer
# 里那条「明确不支持 ⇒ 拒打」的分支，双面请求会原样走到 GDI。
STUB_VERDICT = {"printer": PN, "auto_duplex": True, "confidence": "medium",
                "virtual": False, "detail": "桩：驱动虚报 DC_DUPLEX=1（与事故机一致）"}


class Rec:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.l1: list[dict] = []          # gui.print_pdf 的实参
        self.native: list[dict] = []      # 底层 GDI 入口收到的参数（真·提交给驱动）
        self.plans: list[dict] = []       # print_plan_pass 的趟次与页序
        self.merges: list[list[str]] = []
        self.logs: list[str] = []
        self.stack_planned: list = []
        self.stdout = ""


REC = Rec()


def install_stubs(rec: Rec) -> None:
    def l1_print_pdf(**kw):
        rec.l1.append(dict(kw))
        return _REAL_PRINT_PDF(**kw)

    def native(*a, **k):
        rec.native.append({
            "pdf": os.path.basename(str(a[0])) if a else k.get("pdf_path", ""),
            "printer": (a[1] if len(a) > 1 else k.get("printer_name")),
            "duplex": (a[2] if len(a) > 2 else k.get("duplex")),
            "pages_override": (a[9] if len(a) > 9 else k.get("pages_override")),
            "rotate_180": (a[10] if len(a) > 10 else k.get("rotate_180", False)),
        })
        return True, "STUB-GDI（未出纸：入口被桩死）"

    def plan_pass(plan, pdf_path, printer_name="", which=1, **kw):
        rec.plans.append({
            "which": int(which),
            "sheets": int(getattr(plan, "sheet_count", -1)),
            "pass1": list(plan.pass_pages(1)),
            "pass2": list(plan.pass_pages(2)),
        })
        return _REAL_PRINT_PLAN_PASS(plan, pdf_path, printer_name, which, **kw)

    def merge(paths, out_path):
        rec.merges.append(list(paths))
        try:
            with open(out_path, "wb") as f:
                f.write(pdf_bytes(1))
        except OSError:
            pass
        return True, "STUB-merge"

    gui.print_pdf = l1_print_pdf
    gui.print_plan_pass = plan_pass
    pdf_printer._print_pdf_native = native
    pdf_printer.merge_pdfs = merge
    pdf_printer.get_printer_duplex_support = lambda *a, **k: dict(STUB_VERDICT)
    gui.wait_for_output = lambda *a, **k: {"queue_seconds": 0.0, "margin_seconds": 0.0}


def uninstall_stubs() -> None:
    gui.print_pdf = _REAL_PRINT_PDF
    gui.print_plan_pass = _REAL_PRINT_PLAN_PASS
    pdf_printer._print_pdf_native = _ORIG_NATIVE
    pdf_printer.merge_pdfs = _REAL_MERGE
    pdf_printer.get_printer_duplex_support = _REAL_DUPLEX_SUPPORT
    gui.wait_for_output = _REAL_WAIT


_ORIG_NATIVE = pdf_printer._print_pdf_native


def run_worker(rec: Rec, job: PrintJob, profile, *, stack_mode=False,
               stack_out="", seq=0) -> tuple[object, object]:
    """真实 PrintWorker.run()（同步跑，不真起线程）；返回 (worker, 判定前 _uses_manual_duplex)。"""
    w = gui.PrintWorker(jobs=[job], printer_name=CFG.printer_name or PN,
                        duplex_mode=CFG.duplex_mode or "long-edge",
                        keep_temp_pdf=False, render_dpi=0,
                        manual_duplex_profile=profile,
                        stack_mode=stack_mode, stack_out_pdf=stack_out,
                        stack_seq=seq, stack_rotate=True)
    w.log_message.connect(lambda m: rec.logs.append(m))
    w.flip_required.connect(lambda text, t: w.confirm_flip(True))   # 翻纸窗自动回「继续」
    w.stack_entry_planned.connect(lambda d: rec.stack_planned.append(d))
    verdict = None
    if hasattr(w, "_uses_manual_duplex"):
        try:
            verdict = bool(w._uses_manual_duplex(job))
        except Exception as e:            # 签名变了也不该让脚本崩
            verdict = f"(调用异常: {type(e).__name__}: {e})"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        w.run()
    rec.stdout = buf.getvalue()
    return w, verdict


def fmt_pages(pages) -> str:
    return " ".join("空白" if p is None else str(p) for p in pages) or "(无)"


def native_duplex_values(rec: Rec) -> list:
    return [c.get("duplex") for c in rec.native]


def show_flow(tag: str, rec: Rec, verdict=None) -> None:
    print(f"    [{tag}] gui.print_pdf 实参 duplex = "
          f"{[c.get('duplex') for c in rec.l1]}")
    print(f"    [{tag}] 驱动级(GDI 入口) duplex = {native_duplex_values(rec)}"
          + (f"  pages_override={[c.get('pages_override') for c in rec.native]}"
             if rec.native else ""))
    if rec.plans:
        for p in rec.plans:
            print(f"    [{tag}] 第{p['which']}趟: pass1={fmt_pages(p['pass1'])} | "
                  f"pass2={fmt_pages(p['pass2'])} | {p['sheets']} 张纸")
    if verdict is not None:
        print(f"    [{tag}] run 前 _uses_manual_duplex(job) = {verdict}")
    for ln in rec.stdout.splitlines():
        if "双面" in ln and ln.startswith("[打印]"):
            print(f"    [{tag}] pdf_printer 自述: {ln}")


# ─────────────────────────── 4. 证据链 ②：空档案 ⇒ 走驱动双面 ───────────────────────────

def section_2_chain(ev: dict) -> None:
    section("2. 证据链 ②（config → 档案 → 判定）：printer_profiles 为空 ⇒ 走驱动双面")
    print(f"  沙箱配置（照抄真实配置的 printer_name / printer_profiles）: {SANDBOX_CFG}")
    print(f"    CFG.printer_name             = {CFG.printer_name!r}")
    print(f"    keys(CFG.printer_profiles)   = {list(CFG.printer_profiles.keys())}")
    print(f"    get_profile(printer_name)    → auto_duplex={PROFILE_EMPTY.auto_duplex!r} "
          f"manual_duplex={PROFILE_EMPTY.manual_duplex!r} "
          f"capability_source={PROFILE_EMPTY.capability_source!r} "
          f"is_verified={PROFILE_EMPTY.is_verified}")
    print(f"    对照：手动双面档案           → auto_duplex={PROFILE_MANUAL.auto_duplex!r} "
          f"manual_duplex={PROFILE_MANUAL.manual_duplex!r}")

    job = PrintJob(file_path=PDF1, copies=1, duplex="on", page_count=1)
    install_stubs(REC)
    try:
        REC.reset()
        _, verdict_empty = run_worker(REC, job, PROFILE_EMPTY)
    finally:
        uninstall_stubs()
    print(f"    空档案  _uses_manual_duplex(job) = {verdict_empty}")
    info("判定 False ⇒ 不进手动双面批次 ⇒ 交给驱动按 job.duplex 打（这就是事故路径）")
    check("K1", "空档案 ⇒ 该队列能力未确认（auto_duplex=None、manual_duplex=False）",
          PROFILE_EMPTY.auto_duplex is None and PROFILE_EMPTY.manual_duplex is False)
    check("K2", "空档案 ⇒ _uses_manual_duplex(1页 duplex=on) 为 False（走驱动双面）",
          verdict_empty is False, f"实际 = {verdict_empty!r}")
    if ev.get("profiles_keys") == [] and ev.get("printer_name"):
        info(f"与真实配置一致：{os.path.basename(ev['path'])} 的 printer_profiles 也是空")


# ─────────────────────────── 5. 桩观测：四条路径 ───────────────────────────

def section_3_paths() -> dict:
    section("3. 桩观测（真实 PrintWorker.run()）：1 页 / 范围选 1 页 / 多页 / 未知页数")
    out: dict = {}
    install_stubs(REC)
    try:
        # ── S1: 1 页 + duplex=on + 空档案（事故原样）──
        print("\n  S1  1 页 PDF + duplex='on' + **空档案**（事故原样）")
        REC.reset()
        _, v = run_worker(REC, PrintJob(file_path=PDF1, copies=1, duplex="on",
                                        page_count=1), PROFILE_EMPTY)
        show_flow("S1", REC, v)
        out["S1"] = REC
        check("A1", "1 页 + duplex=on + 空档案 ⇒ 驱动级不得提交 duplex='on'",
              "on" not in native_duplex_values(REC),
              f"实际驱动级 duplex={native_duplex_values(REC)}")
        check("A2", "同一场景只提交 1 次（不该变成两趟/多次提交）",
              len(REC.native) == 1, f"实际提交 {len(REC.native)} 次")

        # ── S2: 1 页 + duplex=on + 手动双面档案（空白页的第二种来源）──
        print("\n  S2  1 页 PDF + duplex='on' + **手动双面档案**（1 页凑不出两面）")
        REC.reset()
        _, v = run_worker(REC, PrintJob(file_path=PDF1, copies=1, duplex="on",
                                        page_count=1), PROFILE_MANUAL)
        show_flow("S2", REC, v)
        out["S2"] = REC
        check("B1", "1 页 + duplex=on + 手动档案 ⇒ 不得走两趟（不得白配一张空白占位纸）",
              len(REC.plans) == 0,
              f"实际走了 {len(REC.plans)} 趟；页序={REC.plans if REC.plans else '无'}")
        check("B2", "同场景若真走两趟，驱动级也必须是单面（两趟全程 simplex）",
              "on" not in native_duplex_values(REC),
              f"实际驱动级 duplex={native_duplex_values(REC)}")

        # ── S3: 3 页 + duplex=on + 手动档案（回归护栏：多页手动双面不能被废掉）──
        print("\n  S3  3 页 PDF + duplex='on' + 手动双面档案（回归护栏）")
        REC.reset()
        _, v = run_worker(REC, PrintJob(file_path=PDF3, copies=1, duplex="on",
                                        page_count=3), PROFILE_MANUAL)
        show_flow("S3", REC, v)
        out["S3"] = REC
        check("C1", "3 页 + 手动档案 ⇒ 两趟仍然发生（各打一趟）",
              [p["which"] for p in REC.plans] == [1, 2],
              f"实际趟次={[p['which'] for p in REC.plans]}")
        check("C2", "同场景两趟全程不请求驱动双面",
              "on" not in native_duplex_values(REC),
              f"实际驱动级 duplex={native_duplex_values(REC)}")

        # ── S4: 3 页 + duplex=on + 空档案（回归护栏：多页仍走驱动双面）──
        print("\n  S4  3 页 PDF + duplex='on' + 空档案（回归护栏）")
        REC.reset()
        _, v = run_worker(REC, PrintJob(file_path=PDF3, copies=1, duplex="on",
                                        page_count=3), PROFILE_EMPTY)
        show_flow("S4", REC, v)
        out["S4"] = REC
        check("D1", "3 页 + duplex=on + 空档案 ⇒ 驱动级仍是 duplex='on'（不许把多页也改单面）",
              native_duplex_values(REC) == ["on"],
              f"实际驱动级 duplex={native_duplex_values(REC)}")

        # ── S5: 1 页 + duplex=off（本来单面）──
        print("\n  S5  1 页 PDF + duplex='off'（本来就单面）")
        REC.reset()
        _, v = run_worker(REC, PrintJob(file_path=PDF1, copies=1, duplex="off",
                                        page_count=1), PROFILE_EMPTY)
        show_flow("S5", REC, v)
        out["S5"] = REC
        check("E1", "1 页 + duplex=off ⇒ 驱动级 duplex='off'（单面不受影响）",
              native_duplex_values(REC) == ["off"],
              f"实际驱动级 duplex={native_duplex_values(REC)}")

        # ── S6: 3 页 + page_range="2"（实际 1 页）+ duplex=on + 空档案 ──
        print("\n  S6  3 页 PDF + page_range='2'（实际只打 1 页）+ duplex='on' + 空档案")
        REC.reset()
        _, v = run_worker(REC, PrintJob(file_path=PDF3, copies=1, duplex="on",
                                        page_range="2", page_count=3), PROFILE_EMPTY)
        show_flow("S6", REC, v)
        out["S6"] = REC
        check("H1", "页码范围只选 1 页 ⇒ 驱动级不得提交 duplex='on'（与 1 页同源）",
              "on" not in native_duplex_values(REC),
              f"实际驱动级 duplex={native_duplex_values(REC)}")

        # ── S7: 损坏 PDF + page_count=2（真实页数数不出来）→ 不许当成 1 页 ──
        print("\n  S7  损坏 PDF（真实页数读不出）+ page_count=2 + duplex='on'（反例：不许误降级）")
        REC.reset()
        _, v = run_worker(REC, PrintJob(file_path=PDF_BROKEN, copies=1, duplex="on",
                                        page_count=2), PROFILE_EMPTY)
        show_flow("S7", REC, v)
        out["S7"] = REC
        check("G1", "页数读不出来 ⇒ 调用方实参仍是 duplex='on'（未知 ≠ 1 页）",
              [c.get("duplex") for c in REC.l1] == ["on"],
              f"实际 gui.print_pdf 实参 duplex={[c.get('duplex') for c in REC.l1]}")
        REC.reset()
        _, v = run_worker(REC, PrintJob(file_path=PDF_BROKEN, copies=1, duplex="on",
                                        page_count=2), PROFILE_MANUAL)
        show_flow("S7b", REC, v)
        out["S7b"] = REC
        check("G2", "页数读不出来 + 手动档案 ⇒ 仍尝试两趟（保持旧行为，不静默改单面）",
              len(REC.plans) >= 1,
              f"实际趟次={[p['which'] for p in REC.plans]}")
    finally:
        uninstall_stubs()
    return out


# ─────────────────── 6. 最深一层兜底：直调 pdf_printer.print_pdf 的硬断言（D 组）───────────────────

def section_3b_deepest_fallback() -> None:
    """`pdf_printer.print_pdf` 开头的「有效页数==1 且 duplex=='on' 且 pages_override is None ⇒ off」。

    这一层是给**绕过 PrintWorker 的调用方**（工具/脚本/将来新入口）兜底的，所以断言必须
    直接打在 `print_pdf` 上（不经 gui）。同时钉住它**不许过度触发**：
      · 多页（含 page_range 选多页）→ 仍 'on'；
      · page_range 非空但**全部无效/越界**（"999"/"0"/"abc"/"3-1"）→ 文档语义是"打印全部"，
        绝不能因为"解析出 1 页"就降单面（这是本层最容易写错的地方）；
      · `pages_override`（手动双面/校准逐面序列）→ 完全不碰，请求原样透传；
      · 多份 × 1 页 → 仍按单面（份数不会把 1 页变成 2 页）。
    """
    section("3b. 最深一层兜底（直调 pdf_printer.print_pdf，不经 gui）：不许过度触发")
    cases = [
        # (id, pdf, copies, page_range, pages_override, 期望驱动级 duplex, 说明)
        ("D1", PDF1, 1, "", None, "off", "1 页 → 单面（本层兜底的存在理由）"),
        ("D2", PDF3, 1, "1-2", None, "on", "页码范围选多页 → 仍双面"),
        ("D3", PDF3, 1, "2", None, "off", "页码范围恰好选出 1 页 → 单面"),
        ("D4", PDF10, 1, "999", None, "on", "**越界** page_range：语义=打印全部 ⇒ 不是 1 页"),
        ("D5", PDF10, 1, "0", None, "on", "**非法** page_range：语义=打印全部 ⇒ 不是 1 页"),
        ("D6", PDF10, 1, "abc", None, "on", "**不可解析** page_range：语义=打印全部"),
        ("D7", PDF10, 1, "3-1", None, "on", "倒序区间不可智能拆分 → 全部（不是 1 页）"),
        ("D8", PDF10, 1, "23-4", None, "on", "智能拆分 2,3,4 → 3 页（不是 1 页）"),
        ("D9", PDF10, 1, "1-1", None, "on", "**既有语义**：a==b 视作无效区间 → 打印全部 10 页（不是 1 页）"),
        ("D10", PDF1, 1, "", [0], "on", "**pages_override 免疫**：手动双面/校准序列不受影响"),
        ("D11", PDF3, 1, "", [0, None], "on", "**pages_override 免疫**（多面序列）"),
        ("D12", PDF1, 2, "", None, "off", "1 页 ×2 份 → 仍单面（份数≠页数）"),
        ("D13", PDF3, 2, "", None, "on", "3 页 ×2 份 → 仍双面"),
        ("D14", PDF10, 1, " 1 , 3 , 5 ", None, "on", "带空格/逗号选 3 页 → 仍双面"),
        ("D15", PDF10, 1, "1-2,7", None, "on", "组合范围 3 页 → 仍双面"),
    ]
    install_stubs(REC)
    try:
        for cid, pdf, copies, rng, po, want, why in cases:
            REC.reset()
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    ok, msg = pdf_printer.print_pdf(
                        pdf, CFG.printer_name or PN, copies=copies, duplex="on",
                        duplex_mode="long-edge", page_range=rng, orientation="portrait",
                        pages_override=po)
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {e}"
            got = native_duplex_values(REC)
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    resolved = len(pdf_printer._parse_page_range(
                        rng, pdf_printer.count_pdf_pages(pdf)))
            except Exception as e:
                resolved = f"(解析异常 {type(e).__name__})"
            label = f"{why}；page_range={rng!r}→解析出 {resolved} 页"
            if cid in ("D10", "D11"):
                label = f"{why}；pages_override={po}"
            check(cid, f"expected {want!r}: {label}",
                  got == [want],
                  f"实际驱动级 duplex={got} (print_pdf→{ok}/{msg!r})")
        # 无效/越界范围必须真的"打印全部"（否则 D4-D7 的 'on' 就是碰巧）
        for cid, rng, pages in (("D4", "999", 10), ("D5", "0", 10),
                                ("D6", "abc", 10), ("D7", "3-1", 10)):
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    n = len(pdf_printer._parse_page_range(rng, 10))
            except Exception:
                n = -1
            check(cid + "′", f"page_range={rng!r} 解析结果 = 全部 {pages} 页（不是 1 页 ⇒ 不该降单面）",
                  n == pages, f"实际解析出 {n} 页（文档语义：非空但全部无效 → 打印全部）")

        # 口径同步护栏：单页闸门依赖 _parse_page_range，若它与计费用的
        # printer_config._count_pages_in_range 漂移，就会出现"按单面打、按双面收费"这类错位
        drift = []
        for rng in ("", "2", "1", "1-1", "2-2", "999", "0", "abc", "3-1", "23-4",
                    "1-2", "1 , 3 , 5 ", "1,1", "5-2", "1-10", "11-20"):
            for total in (1, 2, 3, 10):
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        a = len(pdf_printer._parse_page_range(rng, total))
                        b = int(printer_config._count_pages_in_range(rng, total))
                except Exception as e:
                    drift.append(f"{rng!r}/{total}: 异常 {type(e).__name__}")
                    continue
                if a != b:
                    drift.append(f"{rng!r}/{total}: render={a} vs billing={b}")
        check("P1", "页码范围解析口径与计费口径一致（渲染/计费不漂移）",
              not drift, f"漂移项={drift[:5] or '无'}")
    finally:
        uninstall_stubs()


# ─────────────────────────── 7. 非判定观察：S 攒批路径 ───────────────────────────

def section_4_observations() -> None:
    section("4. 非判定观察（打印出来供判断，不参与退出码）")

    print("\n  S  攒批路径：1 页 + duplex='on' + 手动档案 + stack_mode=True")
    stack_out = os.path.join(SANDBOX, "stack_pass1.pdf")
    install_stubs(REC)
    try:
        REC.reset()
        _, v = run_worker(REC, PrintJob(file_path=PDF1, copies=1, duplex="on",
                                        page_count=1), PROFILE_MANUAL,
                          stack_mode=True, stack_out=stack_out, seq=1)
        print(f"    _uses_manual_duplex(job) = {v}")
        print(f"    驱动级 duplex = {native_duplex_values(REC)}")
        for d in REC.stack_planned:
            print(f"    stack_entry_planned 载荷 = {json.dumps(d, ensure_ascii=False)[:400]}")
        for ln in REC.logs:
            if "攒批" in ln or "单页" in ln or "单面" in ln:
                print(f"    日志: {ln}")
        info("攒批里 1 页文件占 1 张纸、背面是空白占位槽 —— 与它标 duplex 与否的物理结果相同，"
             "故只作观察、不作判定")
    except Exception as e:
        info(f"攒批探测异常（不算失败）：{type(e).__name__}: {e}")
        traceback.print_exc(limit=2)
    finally:
        uninstall_stubs()


# ─────────────────────────── 7. 汇总 ───────────────────────────

def main() -> int:
    ev = section_1_real_evidence()
    section_2_chain(ev)
    section_3_paths()
    section_3b_deepest_fallback()
    section_4_observations()

    section("5. 汇总")
    n_pass = sum(1 for _, _, ok, _ in CHECKS if ok)
    for cid, desc, ok, detail in CHECKS:
        print(f"  {'✓' if ok else '✗'} {cid:<4} {desc}")
    print(f"\n  {n_pass}/{len(CHECKS)} 条断言通过")
    if FAILS:
        print(f"  ❌ RED：以下断言未满足（修复前预期为红）: {', '.join(FAILS)}")
    else:
        print("  ✅ GREEN：全部断言满足 —— 1 页不再把 duplex='on' 提交给驱动"
              "（含绕过 PrintWorker 直调 print_pdf），也不再走两趟；"
              "多页 / 页数未知 / pages_override 的旧行为未被破坏")
    print(f"  （本次验证的代码基准见第 0 节 md5；沙箱 {SANDBOX} 未触碰真实 %APPDATA%）")
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
