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
# ⑦ 第 1 趟耗时预估（两参数模型：a 启动开销 + b 每张纸）
# ============================================================
print("\n=== ⑦ 第 1 趟耗时预估 ===")
check("时长格式：MM:SS；满 1 小时才带小时",
      pc.format_duration(0) == "00:00" and pc.format_duration(65) == "01:05"
      and pc.format_duration(3599) == "59:59" and pc.format_duration(3600) == "1:00:00"
      and pc.format_duration(3725) == "1:02:05")
check("时长格式：负数/非法值不炸（返回 00:00）",
      pc.format_duration(-5) == "00:00" and pc.format_duration(None) == "00:00"
      and pc.format_duration("x") == "00:00")

check("无样本 → 用出厂默认 a/b（不至于显示「预估 0 秒」）",
      pc.fit_print_speed(None) == (pc.DEFAULT_STARTUP_SEC, pc.DEFAULT_PER_PAGE_SEC)
      and pc.fit_print_speed("junk") == (pc.DEFAULT_STARTUP_SEC, pc.DEFAULT_PER_PAGE_SEC))
check("★ 两点标定能还原 a/b（这正是「启动开销 + 斜率」两参数的意义）",
      [round(v, 1) for v in pc.fit_print_speed([[1, 11.5], [10, 43.0]])] == [8.0, 3.5])
check("★ 只有一种页数时：启动取默认、只识别斜率（而不是把固定开销摊进斜率）",
      [round(v, 1) for v in pc.fit_print_speed([[10, 43.0]])] == [8.0, 3.5])
check("异常样本被钳制（卡纸/操作员中途离开不会把预估带飞）",
      pc.fit_print_speed([[10, 99999.0]])[1] <= 60.0
      and pc.fit_print_speed([[1, 0.1]]) == (pc.DEFAULT_STARTUP_SEC, pc.DEFAULT_PER_PAGE_SEC))

# ── 2026-10-19：物理上不可能的样本（打印机只回报"开始打印"就返回）不得污染预估 ──
# 现场样本：换原生 1:1 渲染策略后，16 张纸的"第 1 趟实测"只有 2.1 秒、3 张 1.3 秒
# （同机历史真值是 3 张 ~20s、14 张 ~72s）。这类假数据会把斜率带成负数 ⇒ 16 张开出
# 10 秒以内的预估 ⇒「等到预估时刻再翻纸」的闸门直接失效。
check("★ 物理下限：首张 ≥2s、其后每张 ≥0.8s（假数据一律判不可信）",
      pc.speed_sample_plausible(16, 2.1) is False and pc.speed_sample_plausible(3, 1.3) is False
      and pc.speed_sample_plausible(1, 0.4) is False and pc.speed_sample_plausible(1, 0.5) is False
      and pc.speed_sample_plausible(1, 20.7) is True and pc.speed_sample_plausible(3, 20.9) is True
      and pc.speed_sample_plausible(16, 70.0) is True and pc.speed_sample_plausible(0, 30) is False
      and pc.speed_sample_plausible(16, 7200) is False)
check("★ 假样本不参与拟合：现场那套 [[1,20.7],[3,1.3],[16,2.1]] 退回默认模型"
      "（而不是算出「16 张 9.9 秒」）",
      pc.fit_print_speed([[1, 20.7], [3, 1.3], [16, 2.1]])
      == (pc.DEFAULT_STARTUP_SEC, pc.DEFAULT_PER_PAGE_SEC)
      and pc.fit_print_speed([[1, 20.7]]) == (pc.DEFAULT_STARTUP_SEC, pc.DEFAULT_PER_PAGE_SEC),
      f"{pc.fit_print_speed([[1, 20.7], [3, 1.3], [16, 2.1]])}")
check("★ 只剩「1 张」一种页数时不分斜率（1 张的耗时里 a 与 b 纠缠，旧公式会读成 12.7 s/张 ⇒ "
      "16 张 211 秒），退回默认模型",
      pc.fit_print_speed([[1, 20.7], [1, 21.0]])
      == (pc.DEFAULT_STARTUP_SEC, pc.DEFAULT_PER_PAGE_SEC)
      and pc.fit_print_speed([[1, 30.0]]) == (pc.DEFAULT_STARTUP_SEC, pc.DEFAULT_PER_PAGE_SEC))
check("真样本仍照旧拟合（没有被物理下限误伤）",
      [round(v, 1) for v in pc.fit_print_speed([[1, 11.5], [10, 43.0]])] == [8.0, 3.5]
      and [round(v, 1) for v in pc.fit_print_speed([[10, 43.0]])] == [8.0, 3.5]
      and pc.fit_print_speed([[2, 16.2], [10, 54.5], [14, 72.3]])[1] > 3.0,
      f"{[round(v, 2) for v in pc.fit_print_speed([[2, 16.2], [10, 54.5], [14, 72.3]])]}")

p = pc.PrinterProfile(auto_duplex=False, manual_duplex=True, capability_source="manual")
est10 = p.estimate_first_pass(10)
check("没有样本时也能给预估（默认 8s + 3.5s×10 = 43s）", 42.0 <= est10 <= 44.0, f"{est10:.1f}")
check("★ 空白占位页也算（sheets 是槽位数）",
      p.estimate_first_pass(5) < p.estimate_first_pass(6) < p.estimate_first_pass(7))
check("预估依据能显示给用户（基于最近 N 次 / 出厂默认）",
      "尚无实测样本" in p.speed_summary())
p.add_speed_sample(10, 43.0)
p.add_speed_sample(1, 11.5)
check("样本入库后 speed_summary 标注来源",
      "基于最近 2 次实测" in p.speed_summary() and len(p.speed_samples) == 2)
check("★ 记样本会丢弃明显异常值（≤0.5s / >1h / 张数<1）",
      p.add_speed_sample(3, 0.2) is False and p.add_speed_sample(3, 7200) is False
      and p.add_speed_sample(0, 12) is False and len(p.speed_samples) == 2)
check("★ 物理上不可能的样本（打印机提前回报）也拒收：16 张 2.1s 进不去档案",
      p.add_speed_sample(16, 2.1) is False and p.add_speed_sample(3, 1.3) is False
      and len(p.speed_samples) == 2, f"{p.speed_samples}")
_p9 = pc.PrinterProfile(auto_duplex=False, manual_duplex=True,
                        speed_samples=[[1, 20.7], [3, 1.3], [16, 2.1]])
check("★ 档案里已有假样本 → 预估退回默认模型（现场 16 张纸预估 64s ≈ 实机 70~80s，"
      "而不是被假数据带塌的 9.9s），且不谎报「基于最近 3 次实测」",
      63.0 <= _p9.estimate_first_pass(16) <= 65.0
      and "基于最近 1 次实测" in _p9.speed_summary()
      and "已忽略 2 条不可信样本" in _p9.speed_summary(),
      f"{_p9.estimate_first_pass(16):.1f} | {_p9.speed_summary()}")
_q = pc.PrinterProfile()
for _i in range(30):
    _q.add_speed_sample(5, 20.0 + _i)
check("样本条数有上限（只留最近 N 条，反映当前状态）",
      len(_q.speed_samples) == pc.SPEED_SAMPLES_MAX and _q.speed_samples[-1][1] == 49.0,
      f"{len(_q.speed_samples)} 条，最后一条 {_q.speed_samples[-1]}")
check("★ 档案序列化往返保留样本（重启后预估不丢）",
      pc.PrinterProfile.from_dict(p.to_dict()).speed_samples == p.speed_samples)
check("旧档案没有 speed_samples 字段 / 被手工改坏 → 一律回空，不影响其它字段",
      pc.PrinterProfile.from_dict({"auto_duplex": False, "manual_duplex": True}).speed_samples == []
      and pc.PrinterProfile.from_dict({"speed_samples": [["x"], None, [3, 12.0]]}).speed_samples
      == [[3, 12.0]])

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
check("★ 校准工具有 --speed（两点测速，走 pdf_printer.measure_print_speed_two_point）",
      '"--speed"' in tool and "measure_print_speed_two_point(" in tool
      and "add_speed_sample(" in tool and "speed_samples=prof.speed_samples" in tool)

# ============================================================
# ⑦ 2026-10-20 用户要求：耗时模型人工覆盖 + 翻纸放行比例
# ============================================================
print(chr(10) + "=== ⑦ 人工覆盖模型 / 翻纸放行策略 ===")
_base = pc.PrinterProfile()
check("① 默认：两个分量都自动（0），放行比例 50%、翻页准备 0 秒",
      _base.speed_manual_a == 0.0 and _base.speed_manual_b == 0.0
      and abs(_base.gate_ratio - 0.5) < 1e-9 and _base.flip_ready_sec == 0.0
      and _base.speed_is_manual is False)
check("② 默认闸门 = 预估 × 50%（放行时刻）",
      abs(_base.gate_unlock_seconds(100.0) - 50.0) < 1e-6
      and _base.gate_unlock_seconds(0.0) == 0.0)
_man = pc.PrinterProfile(speed_manual_a=13.0, speed_manual_b=6.9,
                      gate_ratio=0.75, flip_ready_sec=20.0)
check("③ 手动覆盖进入 speed_fit（启动/每张纸以人工为准）",
      _man.speed_fit() == (13.0, 6.9) and _man.speed_is_manual is True
      and _man.estimate_first_pass(18) == 13.0 + 6.9 * 18)
check("④ 闸门 = 预估 × 比例 + 翻页准备时间（100 × 75% + 20 = 95 秒）",
      abs(_man.gate_unlock_seconds(100.0) - 95.0) < 1e-6)
check("⑤ 只填一个分量时另一个仍用自动值",
      pc.PrinterProfile(speed_manual_a=20.0).speed_fit()[1]
      == pc.PrinterProfile().speed_fit()[1])
_rt = pc.PrinterProfile.from_dict(_man.to_dict())
check("⑥ 四个新字段能落盘并读回（重启不丢）",
      _rt.speed_manual_a == 13.0 and _rt.speed_manual_b == 6.9
      and abs(_rt.gate_ratio - 0.75) < 1e-9 and _rt.flip_ready_sec == 20.0)
_bad = pc.PrinterProfile.from_dict({"speed_manual_a": "abc", "speed_manual_b": None,
                                 "gate_ratio": 5, "flip_ready_sec": -3})
check("⑦ 坏值/越界值兜底（垃圾字符串 → 0；比例夹到 1；负数夹到 0）",
      _bad.speed_manual_a == 0.0 and _bad.speed_manual_b == 0.0
      and _bad.gate_ratio == 1.0 and _bad.flip_ready_sec == 0.0,
      f"{_bad.speed_manual_a}/{_bad.speed_manual_b}/{_bad.gate_ratio}/{_bad.flip_ready_sec}")
check("⑧ 旧配置（没有这四个键）照样能读，默认值生效",
      pc.PrinterProfile.from_dict({"auto_duplex": False}).gate_ratio == 0.5)
_src = open(os.path.join(LOCAL_TOOL, "printer_config.py"), encoding="utf-8").read()
_fields_src = _src.split("_FIELDS = ", 1)[1].split(")", 1)[0]
check("⑨ 静态：四个新字段都在 _FIELDS 里（否则 to_dict 会静默丢掉）",
      '"speed_manual_a"' in _fields_src and '"gate_ratio"' in _fields_src
      and '"flip_ready_sec"' in _fields_src)

print("\n" + "=" * 56)
print("打印机档案验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if ok_all else 1)
