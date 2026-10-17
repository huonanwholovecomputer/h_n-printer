# -*- coding: utf-8 -*-
"""打印机档案**独立存储**验收（能力不可被配置重写静默清空）

现场事故（2026-10，已由 git 历史证实根因）
────────────────────────────────────────
本机 %APPDATA%\\HN打印工具\\print_config.json 里 `"printer_profiles": {}` 是空的，
而 2026-10-12..10-16 12:42 手动双面一直可用（日志有「手动双面完成」
「已记入速度样本（第 14 条）」）⇒ 档案确实存在过，被某次配置**整体落盘**静默清空了。

根因：`printer_profiles` 是 2026-10 才引入的字段。
  `git show d597512:local_print_tool/printer_config.py | grep -c printer_profiles` = 0
（d597512 = 本地工具 v4.5.15 / 移动端 1.1.25，2026-09-15）——**旧版客户端根本不知道
这个字段**，它的一次 `_save_config()` 就会把 print_config.json 整体写成不带该键的版本。
本机 4.5.15 一直装到 2026-10-16 16:48 才被自动更新换成 4.5.20；12:55/13:09/13:39 三次
退出落盘，17:09 起日志变成「当前打印机「HP LaserJet 1020」未确认为『不支持自动双面、
需人工翻纸』」⇒ 档案在 12:42–17:09 之间被旧版落盘带走。

修复（printer_config.py）：档案独立成同目录 printer_profiles.json（权威存储，原子写），
print_config.json 里仍镜像一份（旧版兼容），save 时把「内存 ∪ 磁盘已有档案
（内存同键优先）」写进独立文件 —— **内存里档案为空绝不等于删档案**。

本脚本用临时目录全程验证（绝不碰真实 %APPDATA%）：
  ① 迁移：旧配置带档案、无独立文件 → load 后内存有档案 + 独立文件生成（幂等，二次 load 不写盘）；
  ② 独立文件优先：两边同键不同值 → 独立文件赢；仅旧配置里有的键仍保留（兼容）；损坏可自愈；
  ③ **反清空（核心）**：模拟旧版把 print_config.json 整体覆盖成不含 printer_profiles 的版本 → 档案仍在；
  ④ **反过期覆盖**：A（加载时没有档案）在 B 写入后 save → 档案仍在（并集语义）；
  ⑤ 显式 path / 沙箱：所有读写落在传入目录内，绝不调用 paths 的真实 APPDATA 路径；
  ⑥ 零回归：tests/verify_printer_profile.py、tests/verify_bad_config_boot.py 仍全绿。

用法（仓库根目录）:
  PYTHONPATH= PYTHONUTF8=1 C:/Users/Administrator/AppData/Local/Programs/Python/Python311/python.exe \
      tests/verify_printer_profile_store.py      （exit 0 = 全部通过）
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

import paths as paths_mod        # noqa: E402
import printer_config as pc      # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


SANDBOX = tempfile.mkdtemp(prefix="hn_profstore_")
Q = "HP LaserJet 1020"                # 实机那台（档案存在过的机器）
Q2 = "HP LaserJet MFP M232-M237"      # 有双面器那台


def fresh_dir(name):
    d = os.path.join(SANDBOX, name)
    os.makedirs(d, exist_ok=True)
    return d


def cfg_in(name, filename="print_config.json"):
    return os.path.join(fresh_dir(name), filename)


def store_for(config_path):
    return os.path.join(os.path.dirname(config_path), pc.PRINTER_PROFILES_FILENAME)


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def prof(**kw):
    """一条"实机校准过"的档案（校准工具 --save 写出来的形状）。"""
    base = {
        "auto_duplex": False, "capability_source": "manual", "manual_duplex": True,
        "first": "odd", "reverse_second": True, "second_rotate_180": True,
        "verified_at": "2026-10-12 22:14:49", "verified_by": "丙成员",
        "note": "实机校准 5 页", "speed_samples": [[10, 43.0]],
        "stack_capacity": 90, "stack_warn_after_sec": 1800,
    }
    base.update(kw)
    return base


# 统计独立档案写盘次数（钉住"迁移只写一次"这种幂等性）
_writes = []
_orig_write = pc._write_profiles_store


def _counting_write(path, profiles):
    _writes.append(path)
    return _orig_write(path, profiles)


pc._write_profiles_store = _counting_write

# ============================================================
# ① 迁移：旧配置带档案、无独立文件 → 一次性写入独立文件（幂等）
# ============================================================
print("\n=== ① 旧配置 → 独立文件迁移（幂等）===")
CFG = cfg_in("migrate")
STORE = store_for(CFG)
# 旧版配置形状：档案住在 print_config.json 里；dpi_auto_migrated 置位是为了隔离
# render_dpi 的一次性迁移（否则 load 会额外 save 一次，干扰写盘次数断言）。
write_json(CFG, {
    "printer_name": Q, "duplex_mode": "long-edge", "render_dpi": 0,
    "dpi_auto_migrated": True,
    "tabs": {"1": {"jobs": []}},
    "printer_profiles": {Q: prof()},
})
check("前置：此时只有 print_config.json，独立文件不存在",
      os.path.isfile(CFG) and not os.path.exists(STORE))

_writes.clear()
cfg1 = pc.PrinterConfig.load(CFG)
p1 = cfg1.get_profile(Q)
check("★ 迁移：load 后档案在内存里（能力没丢）",
      p1.auto_duplex is False and p1.capability_source == "manual"
      and p1.verified_at == "2026-10-12 22:14:49",
      f"{p1.auto_duplex}/{p1.capability_source}/{p1.verified_at}")
check("★ 迁移：独立文件已生成，且路径由传入配置路径推导（同目录固定文件名）",
      os.path.isfile(STORE) and store_for(CFG) == STORE, STORE)
raw_store = read_json(STORE)
check("独立文件结构 = {version:1, profiles:{队列名: 档案}}",
      raw_store.get("version") == pc.PRINTER_PROFILES_VERSION == 1
      and raw_store["profiles"][Q]["first"] == "odd"
      and raw_store["profiles"][Q]["speed_samples"] == [[10, 43.0]],
      str(raw_store)[:120])
check("迁移这次 load 只写了一次独立文件", len(_writes) == 1, f"{len(_writes)} 次")

_writes.clear()
cfg1b = pc.PrinterConfig.load(CFG)
check("★ 迁移幂等：二次 load 不再写独立文件（独立文件已健康）",
      len(_writes) == 0 and cfg1b.get_profile(Q).auto_duplex is False,
      f"{len(_writes)} 次")
check("迁移不改变配置里其它字段（printer_name / tabs 原样）",
      cfg1b.printer_name == Q and set(cfg1b.tabs) == {"1"})

# ============================================================
# ② 独立文件优先 + 旧键兼容 + 损坏自愈
# ============================================================
print("\n=== ② 独立文件优先 / 旧键兼容 / 损坏自愈 ===")
CFG2 = cfg_in("priority")
STORE2 = store_for(CFG2)
write_json(CFG2, {
    "printer_name": Q, "dpi_auto_migrated": True,
    "tabs": {"1": {"jobs": []}},
    "printer_profiles": {
        Q: prof(note="旧配置镜像", verified_at="2026-10-01 00:00:00"),
        "ONLY-IN-LEGACY": prof(auto_duplex=True, capability_source="probe"),
    },
})
write_json(STORE2, {"version": 1, "profiles": {
    Q: prof(note="独立文件（权威）", verified_at="2026-10-12 22:14:49"),
    Q2: prof(auto_duplex=True, capability_source="probe", note="独立文件里的另一台"),
}})
cfg2 = pc.PrinterConfig.load(CFG2)
check("★ 同键冲突：独立文件赢（print_config.json 里的镜像只是兼容兜底）",
      cfg2.get_profile(Q).note == "独立文件（权威）"
      and cfg2.get_profile(Q).verified_at == "2026-10-12 22:14:49",
      cfg2.get_profile(Q).note)
check("独立文件独有的键照样在（Q2）",
      cfg2.get_profile(Q2).auto_duplex is True
      and cfg2.get_profile(Q2).note == "独立文件里的另一台")
check("仅旧配置里有的键仍被接受（升级兼容：不会因为独立文件缺席就丢档案）",
      cfg2.get_profile("ONLY-IN-LEGACY").auto_duplex is True)
check("合并读取零写盘（独立文件健康 → load 不写文件）", len(_writes) == 0,
      f"{len(_writes)} 次")

# 独立文件被写坏：不能崩、不能丢档案（退回旧配置镜像 + 自愈成合法结构）
CFG2b = cfg_in("heal")
STORE2b = store_for(CFG2b)
write_json(CFG2b, {
    "printer_name": Q, "dpi_auto_migrated": True, "tabs": {"1": {"jobs": []}},
    "printer_profiles": {Q: prof(note="镜像兜底")},
})
os.makedirs(os.path.dirname(STORE2b), exist_ok=True)
with open(STORE2b, "w", encoding="utf-8") as f:
    f.write('{"version":1,"profiles":{ 不是 json')
cfg2b = pc.PrinterConfig.load(CFG2b)
check("★ 独立文件损坏：load 不抛异常，且退回旧配置镜像（档案不丢）",
      cfg2b.get_profile(Q).note == "镜像兜底", cfg2b.get_profile(Q).note)
check("损坏文件被自愈成合法结构（下次读不再报错）",
      read_json(STORE2b)["profiles"][Q]["note"] == "镜像兜底")

# ============================================================
# ③ 反清空（核心）：旧版客户端整体重写 print_config.json
# ============================================================
print("\n=== ③ 反清空：旧版客户端整体重写配置（核心）===")
CFG3 = cfg_in("antiwipe")
STORE3 = store_for(CFG3)
pc.update_user_printer_profile(Q, path=CFG3, auto_duplex=False, capability_source="manual",
                               manual_duplex=True, first="odd",
                               verified_at="2026-10-12 22:14:49", verified_by="丙成员")
check("前置：档案已落盘（镜像 + 独立文件都在）",
      read_json(CFG3)["printer_profiles"][Q]["first"] == "odd"
      and read_json(STORE3)["profiles"][Q]["first"] == "odd")

# 模拟旧版客户端（v4.5.15，不认识 printer_profiles）：它把整个配置对象保存一遍。
# 这正是现场 12:55/13:09/13:39 三次退出落盘里发生的事 —— 键根本不在这份 dict 里。
old_client = pc.PrinterConfig.from_dict({
    "printer_name": Q, "duplex_mode": "long-edge", "render_dpi": 0,
    "dpi_auto_migrated": True, "tabs": {"1": {"jobs": []}},
})
check("★ 模拟前提：旧版客户端的配置对象里没有档案（printer_profiles == {}）",
      old_client.printer_profiles == {})
old_client.save(CFG3)
check("★ 旧版落盘确实把配置里的镜像清空了（复现现场形状：printer_profiles == {}）",
      read_json(CFG3).get("printer_profiles") == {},
      str(read_json(CFG3).get("printer_profiles")))
check("★ 独立文件没被删、档案还在（旧版整体落盘不再带走能力事实）",
      os.path.isfile(STORE3)
      and read_json(STORE3)["profiles"][Q]["verified_at"] == "2026-10-12 22:14:49")
reloaded_cfg = pc.PrinterConfig.load(CFG3)
reloaded = reloaded_cfg.get_profile(Q)
check("★ 重启后能力仍在：不支持自动双面 + 人工确认 + 校准时间",
      reloaded.auto_duplex is False and reloaded.capability_source == "manual"
      and reloaded.needs_manual_duplex is True
      and reloaded.verified_at == "2026-10-12 22:14:49",
      f"{reloaded.auto_duplex}/{reloaded.capability_source}/{reloaded.verified_at}")
reloaded_cfg.save(CFG3)               # GUI 下一次常规落盘
check("★ 下一次保存把镜像补回（print_config.json 不再长期缺字段）",
      read_json(CFG3)["printer_profiles"][Q]["first"] == "odd")

# ============================================================
# ④ 反过期覆盖：A（加载时没有档案）在 B 写入后保存
# ============================================================
print("\n=== ④ 反过期覆盖（并集语义：内存为空不清盘）===")
CFG4 = cfg_in("stale")
STORE4 = store_for(CFG4)
seed = pc.PrinterConfig()
seed.save(CFG4)                      # 先有一份正常配置（此时没有任何档案）
A = pc.PrinterConfig.load(CFG4)
check("前置：A 加载时没有档案", A.printer_profiles == {})
B_prof = pc.update_user_printer_profile(Q, path=CFG4, auto_duplex=False,
                                        capability_source="manual", manual_duplex=True,
                                        verified_at="2026-10-16 12:41:54")
check("B 用 update_user_printer_profile 写入并返回档案（独立文件已建）",
      B_prof.verified_at == "2026-10-16 12:41:54"
      and os.path.isfile(STORE4) and read_json(STORE4)["profiles"][Q]["verified_at"]
      == "2026-10-16 12:41:54")
A.last_dir = "D:/A_stale_memory"      # A 是过期内存：只知道自己那份旧配置
A.save(CFG4)
check("★ A 用过期内存保存 → 档案仍在（内存档案为空绝不删除独立文件）",
      read_json(STORE4)["profiles"][Q]["verified_at"] == "2026-10-16 12:41:54"
      and pc.PrinterConfig.load(CFG4).get_profile(Q).auto_duplex is False,
      str(read_json(STORE4)["profiles"][Q]["verified_at"]))
check("A 的配置改动本身照常落盘（存档守卫不影响正常保存）",
      read_json(CFG4)["last_dir"] == "D:/A_stale_memory")

# 同键冲突的既定语义：内存（调用方刚更新的那份）优先 —— 但**键本身绝不会丢**
CFG5 = cfg_in("stale_same_key")
STORE5 = store_for(CFG5)
pc.update_user_printer_profile(Q, path=CFG5, auto_duplex=False, capability_source="manual",
                               verified_at="2026-10-12 22:14:49", note="旧备注")
A5 = pc.PrinterConfig.load(CFG5)
pc.update_user_printer_profile(Q, path=CFG5, note="新备注")
A5.save(CFG5)
check("同键冲突取内存值（调用方刚更新的那份最新；键不会被丢）",
      read_json(STORE5)["profiles"][Q].get("note") == "旧备注"
      and Q in read_json(STORE5)["profiles"], str(read_json(STORE5)["profiles"][Q]["note"]))

# ============================================================
# ⑤ 显式 path / 沙箱：所有读写落在传入目录内，绝不碰真实 %APPDATA%
# ============================================================
print("\n=== ⑤ 显式 path / 沙箱隔离 ===")
check("★ 档案路径由传入 config 路径推导（同目录、固定文件名）",
      pc._profiles_path_for(CFG) == os.path.join(os.path.dirname(CFG),
                                                 "printer_profiles.json"))
check("★ 传相对/等价路径也推导到同一位置（不受 cwd 影响）",
      pc._profiles_path_for(os.path.join(SANDBOX, ".", "migrate", "print_config.json"))
      == pc._profiles_path_for(os.path.join(SANDBOX, "migrate", "print_config.json")))

# 断言 printer_config 在显式 path 下**完全不查**默认 APPDATA 路径
_orig_default = pc._default_config_path


def _boom_default():
    raise AssertionError("显式 path 时不应回落到真实 %APPDATA% 配置路径")


pc._default_config_path = _boom_default
try:
    probe_cfg = cfg_in("explicit")
    pc.PrinterConfig.load(probe_cfg)
    pc.PrinterConfig.load(probe_cfg).save(probe_cfg)
    pc.update_user_printer_profile("某队列", path=probe_cfg, auto_duplex=False,
                                   capability_source="manual")
    explicit_ok = True
    explicit_err = ""
except Exception as e:              # noqa: BLE001 — 这里就是要抓「漏进真实路径」
    explicit_ok = False
    explicit_err = repr(e)
finally:
    pc._default_config_path = _orig_default
check("★ 显式 path 全程不解析真实 %APPDATA% 配置路径", explicit_ok, explicit_err)

# 正式运行时的路径口径：paths.printer_profiles_path() 与 printer_config 推导一致。
# 把 APPDATA 临时改到沙箱再调用（path 函数会 makedirs，绝不能让它建在真实用户目录）。
_orig_appdata = os.environ.get("APPDATA")
os.environ["APPDATA"] = os.path.join(SANDBOX, "appdata_env")
try:
    real_paths_val = paths_mod.printer_profiles_path()
    derived = pc._profiles_path_for(paths_mod.config_path())
    expected_root = os.path.join(SANDBOX, "appdata_env", "HN打印工具")
finally:
    if _orig_appdata is None:
        os.environ.pop("APPDATA", None)
    else:
        os.environ["APPDATA"] = _orig_appdata
check("paths.printer_profiles_path() 落在用户数据根下、文件名固定",
      os.path.dirname(real_paths_val) == expected_root
      and os.path.basename(real_paths_val) == "printer_profiles.json", real_paths_val)
check("★ 两处文件名口径一致（paths 与 printer_config 不漂移）",
      os.path.basename(real_paths_val) == pc.PRINTER_PROFILES_FILENAME
      and getattr(paths_mod, "PRINTER_PROFILES_FILENAME", None)
      == pc.PRINTER_PROFILES_FILENAME)
check("★ 正式运行时：printer_config 由 config_path() 推导出的档案路径 == paths 的函数",
      derived == real_paths_val, f"{derived} != {real_paths_val}")

# 沙箱封口：本次所有落盘文件都在 SANDBOX 里，且没有遗留临时文件
outside, leftovers = [], []
for root, _dirs, files in os.walk(SANDBOX):
    for name in files:
        full = os.path.abspath(os.path.join(root, name))
        if not full.startswith(os.path.abspath(SANDBOX) + os.sep):
            outside.append(full)
        if name.endswith(".tmp"):
            leftovers.append(full)
check("★ 所有读写都落在临时沙箱内（绝不碰真实 %APPDATA%）", not outside, str(outside[:3]))
check("原子写不遗留 .tmp 文件", not leftovers, str(leftovers[:3]))
check("镜像与独立文件同时存在（旧版兼容 + 后端上报口径不破）",
      "printer_profiles" in read_json(CFG3) and os.path.isfile(STORE3))

# ============================================================
# ⑥ 零回归：既有两个验收脚本仍全绿
# ============================================================
print("\n=== ⑥ 零回归（verify_printer_profile / verify_bad_config_boot）===")


def run_existing(name):
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    env["PYTHONUTF8"] = "1"
    try:
        r = subprocess.run([sys.executable, os.path.join(REPO, "tests", name)],
                           cwd=REPO, env=env, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        return False, "超时 180s"
    out = (r.stdout or b"").decode("utf-8", "replace")
    tail = [ln for ln in out.strip().splitlines() if ln.strip()][-2:]
    return r.returncode == 0, f"exit={r.returncode} | " + " / ".join(tail)


for _script in ("verify_printer_profile.py", "verify_bad_config_boot.py"):
    _ok, _detail = run_existing(_script)
    check(f"★ 零回归：tests/{_script} 全绿", _ok, _detail)

print("\n" + "=" * 56)
print("打印机档案独立存储验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
pc._write_profiles_store = _orig_write
shutil.rmtree(SANDBOX, ignore_errors=True)
sys.exit(0 if ok_all else 1)
