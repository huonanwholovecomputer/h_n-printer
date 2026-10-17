# -*- coding: utf-8 -*-
"""对抗日验收 · task-4：把「作者写的测试」换成**我自己构造的反例**

不跑作者的脚本，只用我自己造的场景打靶（作者脚本另跑，见 task-4 报告）：

A 组 · 打印机档案独立落盘（task-2 语义）——全部在临时沙箱里做，绝不碰真实 %APPDATA%
  A1  新代码 save → 同目录 printer_profiles.json（权威）+ print_config.json 镜像都在
  A2  **旧版整体重写** print_config.json（真按旧版 to_dict 落盘，含 tabs/prices，无
      printer_profiles 键）→ 重新 load ⇒ 档案仍在（反清空核心）
  A3  紧接着一次 save ⇒ 镜像被补回（print_config.json 不再长期缺字段）
  A4  手工删掉权威文件 ⇒ load 从镜像恢复，并迁移回权威文件
  A5  权威文件是**坏 JSON** ⇒ load 不抛、档案从镜像恢复
  A6  权威文件是**空文件** ⇒ 同上
  A7  权威文件是 `{"version":1}`（没有 profiles 键）⇒ 档案不得丢（这是最常见的"写一半"）
  A8  权威文件是 `{"profiles":"garbage"}` ⇒ 档案不得丢、不抛
  A9  权威文件路径被**目录**占住 ⇒ load/save 不抛、档案从镜像恢复
  A10 权威文件 0 字节 + 镜像也没有档案 ⇒ 不抛、不凭空留下垃圾
  A11 并集保存：内存 X + 磁盘 Y ⇒ 两份都在；内存为空保存 ⇒ 磁盘 Y 不被清掉
  A12 只有权威文件、print_config.json 不存在 ⇒ 档案照样读回（能力不丢）
  A13 沙箱隔离：所有读写落在临时目录内；真实 %APPDATA% 既没有权威文件、配置 md5 不变
  A14 文件名口径：paths.PRINTER_PROFILES_FILENAME == printer_config.PRINTER_PROFILES_FILENAME

B 组 · GUI 双面能力闭环（task-5 语义）——离屏真 MainWindow
  B1  未盖章（auto_duplex=None）+ 非虚拟队列 ⇒ 提示一次；立刻再调 ⇒ 不再提示
  B2  **切到另一个未盖章队列**（不手工清 _duplex_warned）⇒ 重新提示、且提示里带新队列名
  B3  再切回原队列 ⇒ 仍会提示（"切回来才再说"）
  B4  盖章 auto_duplex=True ⇒ 不再提示；盖章"不支持自动双面" ⇒ 不再提示
  B5  虚拟打印机（Microsoft Print to PDF）⇒ 从不提示
  B6  盖章"不支持自动双面"⇒ 镜像 + 权威文件都落盘；_uses_manual_duplex_job(多页)=True、
      _stack_applicable=True、双面下拉文案变「双面（手动翻纸）」
  B7  重新从磁盘 load ⇒ 档案仍在（落盘真的生效）
  B8  盖章"支持自动双面"⇒ 控制器关闭、攒批不适用、文案回「双面打印」
  B9  盖回"未确认"⇒ 能力清空（回到驱动双面旧行为），但手动双面参数仍留在档案里

C 组 · 已知残留（**只记录，不判红**）
  C1  page_count=0 且原件本身就是 PDF + 手动档案 ⇒ 仍走两趟（驱动全程单面，但白配一张
      空白背面）。这是 lead 裁定的范围外残留（被 tests/verify_manual_duplex_gui.py ①b 钉住）。
      本组只断言"驱动级一个 duplex='on' 都没有"（安全性），把"多走一趟纸"记为观察。

用法（GREEN = 冻结修订；亦可指向被改坏的副本做反向验证）：
    PYTHONPATH= PYTHONUTF8=1 python tests/verify_task4_adversarial.py
    HN_PRINT_CODE_ROOT=<copied-local_print_tool> ... python tests/verify_task4_adversarial.py
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import traceback

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE_ROOT = os.path.abspath(os.environ.get("HN_PRINT_CODE_ROOT")
                            or os.path.join(REPO, "local_print_tool"))
SANDBOX = tempfile.mkdtemp(prefix="hn_t4_adv_")
os.environ["APPDATA"] = SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, CODE_ROOT)
os.chdir(SANDBOX)

from PySide6.QtWidgets import QApplication                                  # noqa: E402
app = QApplication.instance() or QApplication([])

import converter as _converter                                              # noqa: E402
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}

import gui                                                                  # noqa: E402
import paths                                                                # noqa: E402
import pdf_printer                                                          # noqa: E402
import printer_config                                                       # noqa: E402
from printer_config import PrinterConfig, PrinterProfile, PrintJob           # noqa: E402

gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.Yes)
gui.QMessageBox.warning = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)
gui.QMessageBox.information = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)

FAILS: list[str] = []


def check(cid: str, desc: str, cond: bool, detail: str = "") -> bool:
    ok = bool(cond)
    if not ok:
        FAILS.append(cid)
    print(f"  [{'PASS' if ok else 'FAIL'}] {cid:<4} {desc}" + (f"\n         —— {detail}" if detail else ""))
    return ok


def observe(msg: str) -> None:
    print(f"  (观察) {msg}")


def section(t: str) -> None:
    print("\n" + "═" * 78)
    print(t)
    print("═" * 78)


def md5(p: str) -> str:
    try:
        with open(p, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError:
        return "(缺失)"


def wjson(p: str, obj) -> None:
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def rmf(p: str) -> None:
    """删除文件但容忍不存在（反向验证跑在"被改坏"的代码上时也要能跑到汇总）。"""
    try:
        os.remove(p)
    except OSError:
        pass


def mirror_keys(cfg_path: str) -> list:
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return sorted(json.load(f).keys())
    except Exception:
        return []


QUEUE = "Fake Queue A"
QUEUE_B = "Fake Queue B"
PROF_FIELDS = dict(auto_duplex=False, capability_source="manual", manual_duplex=True,
                   first="odd", reverse_second=True, second_rotate_180=True,
                   verified_at="2026-10-14 21:00:00", verified_by="实机校准")
REAL_APPDATA = os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Roaming", "HN打印工具")


class Case:
    """每个场景一个独立子目录（配置路径决定权威文件位置 ⇒ 天然隔离）。"""

    def __init__(self, name: str):
        self.dir = os.path.join(SANDBOX, name)
        os.makedirs(self.dir, exist_ok=True)
        self.cfg = os.path.join(self.dir, "print_config.json")
        self.auth = os.path.join(self.dir, printer_config.PRINTER_PROFILES_FILENAME)

    def seed(self, with_profile=True, extra=None):
        cfg = PrinterConfig(printer_name=QUEUE)
        if with_profile:
            cfg.update_profile(QUEUE, **PROF_FIELDS)
        for k, v in (extra or {}).items():
            setattr(cfg, k, v)
        cfg.save(self.cfg)
        return cfg

    def old_client_rewrite(self, drop=("printer_profiles",)):
        """忠实模拟旧版客户端：它按自己的 to_dict() 整体重写（没有那些新键）。"""
        with open(self.cfg, encoding="utf-8") as f:
            data = json.load(f)
        for k in drop:
            data.pop(k, None)
        wjson(self.cfg, data)


# ───────────────────────── A 组：档案独立落盘 ─────────────────────────

def section_a() -> None:
    section("A 组 · 打印机档案独立落盘（反清空 / 异常路径 / 沙箱隔离）")

    # A1
    c = Case("a1")
    c.seed()
    keys = mirror_keys(c.cfg)
    check("A1", "save 后权威文件 + 镜像都在（镜像仍带 printer_profiles 供旧版/后端读）",
          os.path.isfile(c.auth) and "printer_profiles" in keys,
          f"auth存在={os.path.isfile(c.auth)} 镜像键含printer_profiles={'printer_profiles' in keys}")
    try:
        with open(c.auth, encoding="utf-8") as f:
            st = json.load(f)
    except Exception as e:
        st = {}
        observe(f"A1b 权威文件读不到：{type(e).__name__}: {e}")
    check("A1b", "权威文件结构 = {version:1, profiles:{队列:档案}}",
          isinstance(st.get("profiles"), dict) and QUEUE in (st.get("profiles") or {}),
          f"结构={sorted(st.keys())} 队列={list((st.get('profiles') or {}).keys())}")

    # A2 / A3
    c = Case("a2")
    c.seed()
    c.old_client_rewrite()
    check("A2′", "前提：旧版落盘确实把镜像里的 printer_profiles 抹掉了（复现现场形状）",
          "printer_profiles" not in mirror_keys(c.cfg), f"镜像键={mirror_keys(c.cfg)}")
    cfg2 = PrinterConfig.load(c.cfg)
    check("A2", "**旧版整体重写后重新 load：档案仍在**（反清空核心）",
          QUEUE in cfg2.printer_profiles
          and cfg2.get_profile(QUEUE).auto_duplex is False
          and cfg2.get_profile(QUEUE).manual_duplex is True
          and cfg2.get_profile(QUEUE).verified_at == "2026-10-14 21:00:00",
          f"档案={list(cfg2.printer_profiles.keys())} "
          f"summary={cfg2.get_profile(QUEUE).summary() if QUEUE in cfg2.printer_profiles else '—'}")
    cfg2.save(c.cfg)
    check("A3", "紧接着 save ⇒ 镜像补回（不会长期缺字段，旧版/后端仍能读到）",
          "printer_profiles" in mirror_keys(c.cfg), f"镜像键={mirror_keys(c.cfg)}")

    # A4 删权威文件
    c = Case("a4")
    c.seed()
    rmf(c.auth)
    cfg4 = PrinterConfig.load(c.cfg)
    check("A4", "手工删掉权威文件 ⇒ 从镜像恢复，并迁移回权威文件（幂等补齐）",
          QUEUE in cfg4.printer_profiles and os.path.isfile(c.auth),
          f"档案={list(cfg4.printer_profiles.keys())} auth重建={os.path.isfile(c.auth)}")

    # A5 坏 JSON
    c = Case("a5")
    c.seed()
    with open(c.auth, "w", encoding="utf-8") as f:
        f.write("{ this is not json")
    try:
        cfg5 = PrinterConfig.load(c.cfg)
        ok5 = QUEUE in cfg5.printer_profiles
    except Exception as e:
        cfg5, ok5 = None, False
        observe(f"A5 load 抛异常：{type(e).__name__}: {e}")
    check("A5", "权威文件坏 JSON ⇒ load 不抛、档案从镜像恢复、并自愈重写", ok5,
          f"档案={list(cfg5.printer_profiles.keys()) if cfg5 else '—'}")

    # A6 空文件
    c = Case("a6")
    c.seed()
    open(c.auth, "w").close()
    try:
        cfg6 = PrinterConfig.load(c.cfg)
        ok6 = QUEUE in cfg6.printer_profiles
    except Exception as e:
        cfg6, ok6 = None, False
        observe(f"A6 load 抛异常：{type(e).__name__}: {e}")
    check("A6", "权威文件 0 字节 ⇒ load 不抛、档案从镜像恢复", ok6,
          f"档案={list(cfg6.printer_profiles.keys()) if cfg6 else '—'}")

    # A7 只有 version 键（写一半的形状）
    c = Case("a7")
    c.seed()
    wjson(c.auth, {"version": 1})
    try:
        cfg7 = PrinterConfig.load(c.cfg)
        ok7 = QUEUE in cfg7.printer_profiles
    except Exception as e:
        cfg7, ok7 = None, False
        observe(f"A7 load 抛异常：{type(e).__name__}: {e}")
    check("A7", "权威文件只剩 {\"version\":1} ⇒ 档案不得丢（从镜像兜底）", ok7,
          f"档案={list(cfg7.printer_profiles.keys()) if cfg7 else '—'}")
    _, healthy7 = printer_config._read_profiles_store(c.auth)
    observe(f"A7b 该文件被 _read_profiles_store 判为 healthy={healthy7}"
            f"（docstring 说「结构不对 → False」，若为 True 则不会触发迁移自愈）")

    # A8 profiles 不是 dict
    c = Case("a8")
    c.seed()
    wjson(c.auth, {"version": 1, "profiles": "garbage"})
    try:
        cfg8 = PrinterConfig.load(c.cfg)
        ok8 = QUEUE in cfg8.printer_profiles
    except Exception as e:
        cfg8, ok8 = None, False
        observe(f"A8 load 抛异常：{type(e).__name__}: {e}")
    check("A8", "权威文件 {\"profiles\":\"garbage\"} ⇒ 不抛、档案从镜像恢复", ok8,
          f"档案={list(cfg8.printer_profiles.keys()) if cfg8 else '—'}")

    # A9 权威文件路径被目录占住
    c = Case("a9")
    c.seed()
    rmf(c.auth)
    os.makedirs(c.auth, exist_ok=True)     # 目录占位
    try:
        cfg9 = PrinterConfig.load(c.cfg)
        ok9 = QUEUE in cfg9.printer_profiles
        cfg9.save(c.cfg)                   # save 也不能因为"写不进去"而整体失败
        ok9s = os.path.isfile(c.cfg)
    except Exception as e:
        cfg9, ok9, ok9s = None, False, False
        observe(f"A9 抛异常：{type(e).__name__}: {e}")
    check("A9", "权威文件路径被目录占住 ⇒ load/save 都不抛，配置照常落盘", ok9 and ok9s,
          f"档案={list(cfg9.printer_profiles.keys()) if cfg9 else '—'} save后配置在={ok9s}")

    # A10 无档案时不留垃圾
    c = Case("a10")
    cfg = PrinterConfig(printer_name=QUEUE)
    cfg.save(c.cfg)          # 内存无档案 + 文件不存在 → 不该凭空造空权威文件
    check("A10", "内存无档案且权威文件不存在 ⇒ 不凭空造空文件（也不抛）",
          not os.path.exists(c.auth), f"auth存在={os.path.exists(c.auth)}")

    # A11 并集保存
    c = Case("a11")
    c.seed(with_profile=True)
    # 磁盘再放一个别的队列（模拟另一台机器/CLI 校准写入）
    try:
        with open(c.auth, encoding="utf-8") as f:
            st = json.load(f)
    except Exception as e:
        st = {"version": 1, "profiles": {}}
        observe(f"A11 权威文件读不到（被改坏时正常）：{type(e).__name__}")
    st.setdefault("profiles", {})[QUEUE_B] = PrinterProfile(
        auto_duplex=True, capability_source="manual").to_dict()
    wjson(c.auth, st)
    # 内存里只有 A（过期内存）→ save 必须保留磁盘上的 B
    stale = PrinterConfig(printer_name=QUEUE)
    stale.printer_profiles = {QUEUE: PrinterProfile(**PROF_FIELDS)}
    stale.save(c.cfg)
    try:
        with open(c.auth, encoding="utf-8") as f:
            after = json.load(f).get("profiles") or {}
    except Exception:
        after = {}
    check("A11", "并集保存：内存只知 A 时，磁盘上的 B 不被带走（内存同键优先）",
          QUEUE in after and QUEUE_B in after, f"并存队列={sorted(after.keys())}")
    # 再验：内存为空 save 也不清盘
    empty_cfg = PrinterConfig(printer_name=QUEUE)
    empty_cfg.printer_profiles = {}
    empty_cfg.save(c.cfg)
    try:
        with open(c.auth, encoding="utf-8") as f:
            after2 = json.load(f).get("profiles") or {}
    except Exception:
        after2 = {}
    check("A11b", "内存档案为空时 save ⇒ 磁盘档案照旧（内存空 ≠ 删档案）",
          QUEUE in after2 and QUEUE_B in after2, f"并存队列={sorted(after2.keys())}")

    # A12 只有权威文件、没有 print_config.json
    c = Case("a12")
    c.seed()
    os.remove(c.cfg)
    cfg12 = PrinterConfig.load(c.cfg)
    check("A12", "配置文件整体丢失、只有权威文件 ⇒ 能力照样读回（不丢）",
          QUEUE in cfg12.printer_profiles, f"档案={list(cfg12.printer_profiles.keys())}")

    # A13 沙箱隔离（硬断言）+ 真实 %APPDATA% 状态（观察：2026-10-17 lead 按用户指示
    #     主动写入真实 printer_profiles.json，属**已知排除项**，不能再断言"不存在"）
    outside = []
    for root, _dirs, files in os.walk(SANDBOX):
        for fn in files:
            p = os.path.join(root, fn)
            if os.path.abspath(p).startswith(os.path.abspath(SANDBOX)):
                continue
            outside.append(p)
    check("A13", "本脚本所有落地文件都在沙箱内（没有写到沙箱外）",
          not outside, f"越界文件={outside[:3] or '无'}")
    real_auth = os.path.join(REAL_APPDATA, printer_config.PRINTER_PROFILES_FILENAME)
    if os.path.exists(real_auth):
        try:
            with open(real_auth, encoding="utf-8") as f:
                _real_store = json.load(f)
            _wellformed = isinstance((_real_store or {}).get("profiles"), dict)
        except Exception:
            _real_store, _wellformed = None, False
        observe(f"真实 %APPDATA% 存在权威文件（lead 2026-10-17 主动写入，已知排除项）："
                f"md5={md5(real_auth)} 结构合法={_wellformed} "
                f"队列={sorted(((_real_store or {}).get('profiles') or {}).keys())}")
    else:
        observe("真实 %APPDATA% 没有权威文件")
    observe(f"真实 %APPDATA% 配置 md5={md5(os.path.join(REAL_APPDATA, 'print_config.json'))}")

    # ── R4（delta 复核新增）：坏结构 ⇒ 是否真的触发迁移/自愈（实测，不看文档）──
    # A15 坏结构 + 镜像有档案 ⇒ load 当场把权威文件重写成合法结构（不需要等下一次 save）
    c = Case("a15")
    c.seed()
    wjson(c.auth, {"version": 1, "profiles": "garbage"})
    try:
        cfg15 = PrinterConfig.load(c.cfg)
        ok15 = QUEUE in cfg15.printer_profiles
    except Exception as e:
        cfg15, ok15 = None, False
        observe(f"A15 load 抛异常：{type(e).__name__}: {e}")
    try:
        with open(c.auth, encoding="utf-8") as f:
            healed = json.load(f)
    except Exception:
        healed = None
    check("A15", "坏结构(profiles≠dict) + 镜像有档案 ⇒ **load 当场自愈**权威文件（无需 save）",
          ok15 and isinstance((healed or {}).get("profiles"), dict)
          and QUEUE in ((healed or {}).get("profiles") or {}),
          f"档案={list(cfg15.printer_profiles.keys()) if cfg15 else '—'} "
          f"自愈后权威文件={json.dumps(healed, ensure_ascii=False)[:140]}")

    # A16 坏结构 + 镜像也清空（没有任何数据源）⇒ 不崩、档案仍为空；**记录**是否重写
    c = Case("a16")
    c.seed()
    c.old_client_rewrite()                       # 镜像里不再有 printer_profiles
    wjson(c.auth, {"version": 1, "profiles": "garbage"})
    before16 = open(c.auth, encoding="utf-8").read()
    try:
        cfg16 = PrinterConfig.load(c.cfg)
        crashed16 = False
    except Exception as e:
        cfg16, crashed16 = None, True
        observe(f"A16 load 抛异常：{type(e).__name__}: {e}")
    after16 = open(c.auth, encoding="utf-8").read()
    check("A16", "坏结构 + 镜像也没档案 ⇒ 不崩、不凭空变出档案（空就是空）",
          not crashed16 and not (cfg16.printer_profiles if cfg16 else {}),
          f"档案={list(cfg16.printer_profiles.keys()) if cfg16 else '—'}")
    observe(f"A16b 权威文件被重写？{'是' if after16 != before16 else '否'}"
            f"（两手都空 ⇒ 没有数据可迁移；「坏文件必须自愈」这条判据在此场景**不成立**，见报告 R4b）")

    # A17 无 profiles 外壳的扁平旧格式：仍要能读、且视为健康（load 不重写它）
    c = Case("a17")
    c.seed()
    wjson(c.auth, {QUEUE: PrinterProfile(**PROF_FIELDS).to_dict()})
    before17 = open(c.auth, encoding="utf-8").read()
    try:
        cfg17 = PrinterConfig.load(c.cfg)
        ok17 = QUEUE in cfg17.printer_profiles
    except Exception as e:
        cfg17, ok17 = None, False
        observe(f"A17 load 抛异常：{type(e).__name__}: {e}")
    after17 = open(c.auth, encoding="utf-8").read()
    check("A17", "扁平旧格式 {队列名: 档案} ⇒ 档案读得到、视为健康（不触发迁移重写）",
          ok17 and before17 == after17,
          f"档案={list(cfg17.printer_profiles.keys()) if cfg17 else '—'} 文件未被重写={before17 == after17}")

    # A18 只有 {"version":1}（外壳在、内容丢光）⇒ 观察 healthy（新代码按"扁平格式"兜住）
    c = Case("a18")
    c.seed()
    wjson(c.auth, {"version": 1})
    try:
        cfg18 = PrinterConfig.load(c.cfg)
        ok18 = QUEUE in cfg18.printer_profiles
    except Exception as e:
        cfg18, ok18 = None, False
        observe(f"A18 load 抛异常：{type(e).__name__}: {e}")
    check("A18", "权威文件只剩 {\"version\":1} ⇒ 档案不丢（镜像兜底），不抛",
          ok18, f"档案={list(cfg18.printer_profiles.keys()) if cfg18 else '—'}")

    # A19 判据表：把 _read_profiles_store 对 8 种文件形态的返回值钉死（这是"实测判据"）
    rows = [
        ("文件不存在", None, False, False),
        ("非法 JSON", "{ not json", False, False),
        ("profiles=字符串", json.dumps({"version": 1, "profiles": "garbage"}), False, False),
        ("profiles=列表", json.dumps({"version": 1, "profiles": []}), False, False),
        ("根不是 dict", json.dumps([1, 2, 3]), False, False),
        ("扁平旧格式", json.dumps({QUEUE: PrinterProfile(**PROF_FIELDS).to_dict()}), True, True),
        ("完整结构", json.dumps({"version": 1,
                                "profiles": {QUEUE: PrinterProfile(**PROF_FIELDS).to_dict()}}),
         True, True),
        ("只有 version 键", json.dumps({"version": 1}), False, True),
    ]
    c = Case("a19")
    bad_rows = []
    print("      文件形态                      读出档案?  healthy  期望(档案/healthy)")
    for name, content, exp_prof, exp_healthy in rows:
        pth = os.path.join(c.dir, "probe.json")
        rmf(pth)
        if content is not None:
            with open(pth, "w", encoding="utf-8") as f:
                f.write(content)
        with contextlib.redirect_stdout(io.StringIO()):
            prof, healthy = printer_config._read_profiles_store(pth)
        got_prof = QUEUE in prof
        mark = "OK" if (got_prof == exp_prof and healthy is exp_healthy) else "**不符**"
        print(f"      {name:<28} {str(got_prof):<9} {str(healthy):<8} {exp_prof}/{exp_healthy} {mark}")
        if mark != "OK":
            bad_rows.append(f"{name}: 实得 档案={got_prof}/healthy={healthy}，期望 {exp_prof}/{exp_healthy}")
    check("A19", "档案文件形态 → (读出档案, healthy) 判据表全部符合（含 R4 修复项）",
          not bad_rows, "；".join(bad_rows))

    # A14 文件名口径
    check("A14", "paths 与 printer_config 的档案文件名一致（防口径漂移）",
          paths.PRINTER_PROFILES_FILENAME == printer_config.PRINTER_PROFILES_FILENAME,
          f"{paths.PRINTER_PROFILES_FILENAME!r} vs {printer_config.PRINTER_PROFILES_FILENAME!r}")
    check("A14b", "paths.printer_profiles_path() 落在用户数据根下且文件名固定",
          paths.printer_profiles_path() == os.path.join(paths.get_app_data_dir(),
                                                       paths.PRINTER_PROFILES_FILENAME),
          paths.printer_profiles_path())


# ───────────────────────── B 组：GUI 双面能力闭环 ─────────────────────────

def section_b() -> None:
    section("B 组 · GUI 双面能力闭环（提示时机 / 切队列 / 盖章落盘 / 联动）")
    cfg_path = os.path.join(SANDBOX, "b", "print_config.json")
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    seed = PrinterConfig(printer_name=QUEUE)
    seed.save(cfg_path)

    win = gui.MainWindow(config_path=cfg_path)
    logs: list[str] = []
    _orig = win._log
    win._log = lambda m: (logs.append(m), _orig(m))[1]

    def warns() -> list:
        return [m for m in logs if "未确认" in m]

    def select(name: str):
        """真路径切队列：下拉切 + config 同步（**不动 _duplex_warned**，让它保留上一个队列）。"""
        combo = win._printer_combo
        for i in range(combo.count()):
            if combo.itemData(i) == name:
                combo.setCurrentIndex(i)
                break
        else:
            combo.addItem(name, name)
            combo.setCurrentIndex(combo.count() - 1)
        win._config.printer_name = name
        return win._config.printer_name

    # B1 未盖章 ⇒ 提示一次
    win._config.printer_name = QUEUE
    win._duplex_warned = ""
    logs.clear()
    win._warn_if_duplex_capability_unknown()
    first = warns()
    check("B1", "未盖章 + 非虚拟队列 ⇒ 提示一次（含后果与盖章入口）",
          len(first) == 1 and "双面打印说明" in first[0] and "打印机双面能力" in first[0],
          f"{len(first)} 条：{first[0][:80] if first else '—'}…")
    win._warn_if_duplex_capability_unknown()
    check("B1b", "同一队列同一状态立刻再调 ⇒ 不再提示（不刷日志）",
          len(warns()) == 1, f"累计 {len(warns())} 条")

    # B2 切到另一个未盖章队列 ⇒ 重新提示（不手工清 flag）
    logs.clear()
    select(QUEUE_B)
    win._warn_if_duplex_capability_unknown()
    b2 = warns()
    check("B2", "切到另一个未盖章队列 ⇒ 重新提示，且提示里带**新队列名**",
          len(b2) == 1 and QUEUE_B in b2[0], f"{len(b2)} 条：{b2[0][:70] if b2 else '—'}…")
    win._warn_if_duplex_capability_unknown()
    check("B2b", "新队列同一状态再调 ⇒ 仍只一次",
          len(warns()) == 1, f"累计 {len(warns())} 条")

    # B3 切回原队列 ⇒ 仍会提示（"切回来才再说"）
    logs.clear()
    select(QUEUE)
    win._warn_if_duplex_capability_unknown()
    check("B3", "切回原队列 ⇒ 仍会提示（flag 记的是上一个队列，不是「已提示过整个会话」）",
          len(warns()) == 1 and QUEUE in warns()[0], f"{warns()[0][:70] if warns() else '—'}…")

    # B5 虚拟打印机不提示
    logs.clear()
    select("Microsoft Print to PDF")
    win._warn_if_duplex_capability_unknown()
    check("B5", "虚拟打印机（Print to PDF）⇒ 从不提示（双面对它无意义，不刷日志）",
          not warns(), f"{warns()[:1] or '无'}")

    # B6 盖章「不支持自动双面」
    select(QUEUE)
    vals_manual = {"auto_duplex": False, "capability_source": "manual", "manual_duplex": True,
                   "first": "odd", "reverse_second": True, "second_rotate_180": True,
                   "verified_at": "2026-10-17 18:00:00", "verified_by": "验收",
                   "note": "人工盖章：不支持自动双面"}
    logs.clear()
    ok_apply = win._apply_duplex_capability(QUEUE, vals_manual)
    mirror_has = "printer_profiles" in mirror_keys(cfg_path)
    auth_path = os.path.join(os.path.dirname(cfg_path),
                             printer_config.PRINTER_PROFILES_FILENAME)
    try:
        with open(auth_path, encoding="utf-8") as f:
            auth_has = QUEUE in (json.load(f).get("profiles") or {})
    except Exception as e:
        auth_has = False
        observe(f"B6 权威文件读不到：{type(e).__name__}: {e}")
    job2 = PrintJob(file_path=os.path.join(SANDBOX, "two.pdf"), copies=1, duplex="on", page_count=2)
    job1 = PrintJob(file_path=os.path.join(SANDBOX, "one.pdf"), copies=1, duplex="on", page_count=1)
    check("B6", "盖章「不支持自动双面」⇒ 镜像 + 权威文件都落盘（_save_config 走 printer_config）",
          ok_apply and mirror_has and auth_has,
          f"apply={ok_apply} 镜像={mirror_has} 权威={auth_has}")
    check("B6b", "盖章后立刻联动：多页⇒自制控制器 True / 1 页⇒False / 攒批适用 / 文案改「双面（手动翻纸）」",
          win._uses_manual_duplex_job(job2) is True
          and win._uses_manual_duplex_job(job1) is False
          and win._stack_applicable() is True
          and (not hasattr(win, "_edit_duplex")
               or win._edit_duplex.itemText(0) == "双面（手动翻纸）"),
          f"job2={win._uses_manual_duplex_job(job2)} job1={win._uses_manual_duplex_job(job1)} "
          f"stack={win._stack_applicable()}")
    logs.clear()
    win._warn_if_duplex_capability_unknown()
    check("B4", "已盖章（不支持自动双面）⇒ 不再提示未确认",
          not warns(), f"{warns()[:1] or '无'}")

    # B7 重新 load：落盘真的生效
    reloaded = PrinterConfig.load(cfg_path)
    prof = reloaded.get_profile(QUEUE)
    check("B7", "重新从磁盘 load ⇒ 档案仍在（人工结论 + 参数 + 校准人都在）",
          prof.auto_duplex is False and prof.manual_duplex is True
          and prof.capability_source == "manual" and prof.verified_by == "验收",
          prof.summary())

    # B8 盖章「支持自动双面」
    vals_auto = {"auto_duplex": True, "capability_source": "manual", "manual_duplex": False,
                 "verified_at": "2026-10-17 18:01:00", "verified_by": "验收",
                 "note": "人工盖章：支持自动双面"}
    win._apply_duplex_capability(QUEUE, vals_auto)
    check("B8", "盖章「支持自动双面」⇒ 控制器关闭、攒批不适用、文案回「双面打印」",
          win._uses_manual_duplex_job(job2) is False and win._stack_applicable() is False
          and (not hasattr(win, "_edit_duplex")
               or win._edit_duplex.itemText(0) == "双面打印"),
          f"job2={win._uses_manual_duplex_job(job2)} stack={win._stack_applicable()}")

    # B9 回到「未确认」
    vals_none = {"auto_duplex": None, "capability_source": "", "manual_duplex": False,
                 "verified_at": "", "verified_by": "", "note": "人工改回「未确认」"}
    win._apply_duplex_capability(QUEUE, vals_none)
    prof_none = PrinterConfig.load(cfg_path).get_profile(QUEUE)
    logs.clear()
    win._warn_if_duplex_capability_unknown()
    check("B9", "盖回「未确认」⇒ 能力清空、提示恢复（并保留手动双面参数不丢）",
          prof_none.auto_duplex is None and prof_none.manual_duplex is False
          and prof_none.second_rotate_180 is True and bool(warns()),
          f"auto={prof_none.auto_duplex} manual={prof_none.manual_duplex} "
          f"rotate180={prof_none.second_rotate_180} 提示={len(warns())}")

    win.close()


# ───────────────────────── C 组：已知残留（观察）─────────────────────────

def section_c() -> None:
    section("C 组 · 已知残留（只记录，不判红）：page_count=0 且原件是 PDF")

    # 造一个真 1 页 PDF
    def one_page_pdf(path: str) -> str:
        import fitz
        doc = fitz.open()
        doc.new_page()
        doc.save(path)
        doc.close()
        return path

    pdf1 = one_page_pdf(os.path.join(SANDBOX, "resid_1p.pdf"))

    calls = {"native": [], "plan": []}

    def native(*a, **k):
        calls["native"].append({"duplex": (a[2] if len(a) > 2 else k.get("duplex")),
                                "pages_override": (a[9] if len(a) > 9 else k.get("pages_override"))})
        return True, "STUB"

    real_plan = pdf_printer.print_plan_pass

    def plan_pass(plan, pdf_path, printer_name="", which=1, **kw):
        calls["plan"].append(int(which))
        return real_plan(plan, pdf_path, printer_name, which, **kw)

    def _make_merged(path, pages=1):
        import fitz
        doc = fitz.open()
        for _ in range(pages):
            doc.new_page()
        doc.save(path)
        doc.close()

    def _merge(paths, out):
        _make_merged(out, 1)
        return True, "STUB"

    orig = (pdf_printer._print_pdf_native, gui.print_plan_pass, pdf_printer.merge_pdfs,
            pdf_printer.get_printer_duplex_support, gui.wait_for_output)
    pdf_printer._print_pdf_native = native
    gui.print_plan_pass = plan_pass
    pdf_printer.merge_pdfs = _merge
    pdf_printer.get_printer_duplex_support = lambda *a, **k: {
        "printer": QUEUE, "auto_duplex": True, "confidence": "medium", "virtual": False,
        "detail": "stub"}
    gui.wait_for_output = lambda *a, **k: {"queue_seconds": 0.0, "margin_seconds": 0.0}
    try:
        w = gui.PrintWorker(jobs=[PrintJob(file_path=pdf1, copies=1, duplex="on", page_count=0)],
                            printer_name=QUEUE, duplex_mode="long-edge", keep_temp_pdf=False,
                            render_dpi=0, manual_duplex_profile=PrinterProfile(**PROF_FIELDS))
        w.flip_required.connect(lambda t, s: w.confirm_flip(True))
        with contextlib.redirect_stdout(io.StringIO()):
            w.run()
        check("C1", "残留场景下驱动级仍然**一个 duplex='on' 都没有**（安全性不破）",
              "on" not in [c["duplex"] for c in calls["native"]],
              f"native={calls['native']}")
        observe(f"残留：趟次={calls['plan']}（两趟 = 白配一张空白背面、多翻一次纸）；"
                f"native={calls['native']}")
        observe("说明：这一步被 tests/verify_manual_duplex_gui.py ①b 钉住"
                "（1 页 PDF page_count=0 必须参与合批）⇒ lead 裁定范围外，非本次回归")
    finally:
        (pdf_printer._print_pdf_native, gui.print_plan_pass, pdf_printer.merge_pdfs,
         pdf_printer.get_printer_duplex_support, gui.wait_for_output) = orig


def main() -> int:
    print(f"CODE_ROOT = {CODE_ROOT}")
    for n in ("gui.py", "printer_config.py", "paths.py", "pdf_printer.py"):
        print(f"  {n:<20} md5={md5(os.path.join(CODE_ROOT, n))}")
    section_a()
    section_b()
    section_c()
    section("汇总")
    print(f"  {'❌ 有失败: ' + ', '.join(FAILS) if FAILS else '✅ 全部通过'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
