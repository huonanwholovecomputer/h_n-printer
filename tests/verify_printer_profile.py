# -*- coding: utf-8 -*-
"""打印机档案（能力 + 手动双面实测参数）验收

背景：双面能力与纸路径是**具体机型**的属性，而同一台电脑可能挂多台打印机。2026-10
HP LaserJet 1020 实机实测得到四条结论（本脚本逐条钉住，防止有人把默认值改回去）：

  ① 它的驱动 `DC_DUPLEX=1` 且接受 `Duplex=2` —— **自动探测会虚报支持**（那份"双面能力"
     其实来自驱动的手动双面功能）。所以档案里必须有 `capability_source=manual` 这一层，
     且**人工结论不会被后续自动探测覆盖**（与后端 devices.json 同一套规则：manual > probe）；
  ② "整叠不改变方向搬回" ⇒ 第 2 趟必须**逆序**（reverse_second=True）；
  ③ 纸路径把第 2 趟的内容整体转了 180° ⇒ 必须 `second_rotate_180=True`，否则第 2 面倒置；
  ④ `first="odd"` ⇒ 成品叠**最上面是第 1 页**（even 为镜像，配对同样正确只是观感反过来）。

档案按**打印队列名**索引，存在 print_config.json；本脚本全程用临时目录，不碰真实配置。

用法: python tests/verify_printer_profile.py   （exit 0 = 全部通过）
"""
import io
import json
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

import printer_config as pc  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


TMP = tempfile.mkdtemp(prefix="hn_profile_")
CFG = os.path.join(TMP, "print_config.json")
QUEUE = "HP LaserJet 1020"          # 实机那台
OTHER = "HP LaserJet MFP M232-M237"  # 有双面器那台

# ============================================================
# ① 默认档案：未登记的队列 = 能力未确认
# ============================================================
print("\n=== ① 默认档案 ===")
cfg = pc.PrinterConfig.load(CFG)
prof = cfg.get_profile(QUEUE)
check("未登记队列的默认档案：auto_duplex=None（未确认）", prof.auto_duplex is None, f"{prof.auto_duplex}")
check("默认 first=odd、reverse_second=True、second_rotate_180=True（实机实测默认值）",
      prof.first == "odd" and prof.reverse_second is True and prof.second_rotate_180 is True,
      f"{prof.first}/{prof.reverse_second}/{prof.second_rotate_180}")
check("默认未校准（is_verified=False）", prof.is_verified is False)
check("未确认 ≠ 需要手动双面（needs_manual_duplex 只在确认不支持时为 True）",
      prof.needs_manual_duplex is False)
check("摘要含「能力未确认」「未校准」", "未确认" in prof.summary() and "未校准" in prof.summary(),
      prof.summary())

# ============================================================
# ② 实机那台的档案（校准工具的 --save 写入内容）
# ============================================================
print("\n=== ② 实机档案（HP LaserJet 1020）===")
cfg.printer_name = QUEUE
cfg.update_profile(QUEUE,
                   auto_duplex=False, capability_source="manual", manual_duplex=True,
                   first="odd", reverse_second=True, second_rotate_180=True,
                   verified_at="2026-10-12 22:14:49", verified_by="丙成员",
                   note="实机校准 5 页")
cfg.update_profile(OTHER, auto_duplex=True, capability_source="probe")
cfg.save(CFG)

cfg2 = pc.PrinterConfig.load(CFG)
p1020 = cfg2.get_profile(QUEUE)
p232 = cfg2.get_profile(OTHER)
check("★ 1020：auto_duplex=False / 人工确认 / 需手动双面",
      p1020.auto_duplex is False and p1020.capability_source == "manual"
      and p1020.needs_manual_duplex is True,
      f"{p1020.auto_duplex}/{p1020.capability_source}")
check("★ 1020：odd + 逆序 + 180°补偿（实机四条结论）",
      p1020.first == "odd" and p1020.reverse_second is True and p1020.second_rotate_180 is True,
      f"{p1020.first}/{p1020.reverse_second}/{p1020.second_rotate_180}")
check("1020：记录校准时间与校准者",
      p1020.verified_at == "2026-10-12 22:14:49" and p1020.verified_by == "丙成员" and p1020.is_verified)
check("摘要含能力/参数/校准信息",
      "不支持自动双面" in p1020.summary() and "180°补偿" in p1020.summary()
      and "2026-10-12" in p1020.summary(), p1020.summary())
check("232（有双面器）：auto_duplex=True 且 supports 手动双面未开启",
      p232.auto_duplex is True and p232.needs_manual_duplex is False, f"{p232.auto_duplex}")
check("两台机器互不串档（按队列名索引）",
      cfg2.printer_profiles[QUEUE].auto_duplex is False
      and cfg2.printer_profiles[OTHER].auto_duplex is True)

# ============================================================
# ③ 人工结论不被自动探测覆盖（与后端同一规则）
# ============================================================
print("\n=== ③ 人工 > 探测 ===")
check("★ 已人工确认的档案：apply_probe 拒绝覆盖并返回 False",
      p1020.apply_probe(True) is False and p1020.auto_duplex is False
      and p1020.capability_source == "manual", f"{p1020.auto_duplex}/{p1020.capability_source}")
check("只有探测来源的档案：apply_probe 可更新",
      p232.apply_probe(False) is True and p232.auto_duplex is False
      and p232.capability_source == "probe", f"{p232.auto_duplex}/{p232.capability_source}")

# ============================================================
# ④ 读写与容错
# ============================================================
print("\n=== ④ 读写与容错 ===")
cfg3 = pc.PrinterConfig.load(CFG)
cfg3.update_profile(QUEUE, note="改备注")          # 部分更新
cfg3.save(CFG)
again = pc.PrinterConfig.load(CFG).get_profile(QUEUE)
check("部分更新只改指定字段（其余保留）",
      again.note == "改备注" and again.first == "odd" and again.verified_at == "2026-10-12 22:14:49")
try:
    cfg3.update_profile(QUEUE, 拼错字段=1)
    check("未知字段名应报错", False)
except ValueError as e:
    check("未知字段名 → ValueError（宁可在调用点炸，不静默失效）", True, str(e)[:60])
try:
    cfg3.printer_name = ""
    cfg3.update_profile("")
    check("没有队列名应报错", False)
except ValueError:
    check("队列名与 config.printer_name 都为空 → ValueError", True)

check("老配置（无 printer_profiles 键）→ 空档案，不报错",
      pc.PrinterConfig.from_dict({"printer_name": "X"}).printer_profiles == {})
bad = pc.PrinterConfig.from_dict({"printer_profiles": {
    "Q1": {"auto_duplex": "yes", "first": "侧着", "reverse_second": None},
    "Q2": "不是字典",
}})
check("脏数据容错：auto_duplex 非布尔→None、first 非法→odd、非字典项被丢弃",
      bad.get_profile("Q1").auto_duplex is None and bad.get_profile("Q1").first == "odd"
      and "Q2" not in bad.printer_profiles, f"{bad.printer_profiles}")
# 危险默认值：reverse_second / second_rotate_180 默认是 True（逆序 + 180°补偿）。
# 配置被写坏成 null 时若用 bool(None) 会静默变成 False（正序 / 不旋转）→ 直接印错页/倒置页。
check("★ 不安全值回落安全默认：null → 逆序 + 180°补偿（不能因 bool(None) 变 False）",
      bad.get_profile("Q1").reverse_second is True
      and bad.get_profile("Q1").second_rotate_180 is True,
      f"{bad.get_profile('Q1').reverse_second}/{bad.get_profile('Q1').second_rotate_180}")
mixed = pc.PrinterConfig.from_dict({"printer_profiles": {"Q1": {
    "reverse_second": 0, "second_rotate_180": 1, "manual_duplex": "x"}}})
check("0/1 当作布尔接受、字符串仍回落默认",
      mixed.get_profile("Q1").reverse_second is False
      and mixed.get_profile("Q1").second_rotate_180 is True
      and mixed.get_profile("Q1").manual_duplex is False,
      f"{mixed.get_profile('Q1').reverse_second}/{mixed.get_profile('Q1').second_rotate_180}")

# ============================================================
# ⑤ 模块级便捷入口（工具/GUI 共用，直接落盘）
# ============================================================
print("\n=== ⑤ update_user_printer_profile / load_user_printer_profile ===")
CFG2 = os.path.join(TMP, "print_config2.json")
saved = pc.update_user_printer_profile(QUEUE, path=CFG2,
                                       auto_duplex=False, capability_source="manual",
                                       first="odd", verified_at="2026-10-12 23:00:00")
check("落盘后可回读（不传 path 时走 %APPDATA%，此处显式传临时路径）",
      saved.auto_duplex is False and os.path.isfile(CFG2)
      and pc.load_user_printer_profile(QUEUE, path=CFG2).verified_at == "2026-10-12 23:00:00")
check("落盘写入的是打印机档案键（JSON 结构正确）",
      json.load(open(CFG2, encoding="utf-8"))["printer_profiles"][QUEUE]["first"] == "odd")
check("写档案不影响配置文件里的其它字段（tabs/admin_names 等保留）",
      set(pc.PrinterConfig.load(CFG2).to_dict().keys()) >=
      {"tabs", "active_tab", "cloud_enabled", "admin_names", "printer_profiles"})

# ============================================================
# ⑥ 静态接线：校准工具会落盘，且参数与档案字段一一对应
# ============================================================
print("\n=== ⑥ 静态接线 ===")
tool = open(os.path.join(LOCAL_TOOL, "tools", "print_duplex_calibration.py"), encoding="utf-8").read()
check("校准工具有 --save / --save-only 两个入口",
      '"--save"' in tool and '"--save-only"' in tool and "def save_profile(" in tool)
check("校准工具调用 update_user_printer_profile 落盘",
      "update_user_printer_profile(" in tool)
check("落盘时写入实机四项（first / reverse_second / second_rotate_180 / auto_duplex）",
      'first=args.first' in tool and 'reverse_second=not args.forward' in tool
      and 'second_rotate_180=bool(args.second_rotate)' in tool
      and 'auto_duplex=bool(args.auto_duplex)' in tool)
check("落盘走「人工确认」来源（不会被自动探测改回去）",
      'capability_source="manual"' in tool)
check("第 1 趟单独跑时不落盘（校准未完成不盖章）",
      "（--save 会在第 2 趟跑完后才写档案）" in tool)
check("★ 重跑校准优先用已校准档案（CLI > 档案 > 内置默认）",
      "load_user_printer_profile(args.printer)" in tool and "参数来源: {src}" in tool
      and "if prof.is_verified:" in tool)
check("--forward / --reverse 互斥（避免同时给正序与逆序）",
      "add_mutually_exclusive_group()" in tool)

print("\n" + "=" * 56)
print("打印机档案验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if ok_all else 1)
