# -*- coding: utf-8 -*-
"""L1-4 守卫：本地外部打印与本地报表必须认「封面单独打印（simplex_pages）」

口径（硬规矩 7：张数只有一个真相）：
  - 双面张数 = Σ 双面段 floor(len/2)、单面张数 = Σ 双面段 (len%2) + 单面页个数
    —— 唯一实现是 manual_duplex.charge_units（后端 app.py:_charge_units 与它同值）。
  - 本地外部打印（print_watch.amount_for → printer_config.calc_cost）和本地报表
    （stats_server 本地订单接口）都必须把 simplex_pages 传进这套实现；
    自己再写一份 (eff+1)//2 就必然漏掉「封面单独打印」的单面张。

用法：python other/tests/verify_simplex_local.py [pages_js 路径]（路径参数只给变异自证用）
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL)

failures = []


def check(name, cond, detail=""):
    if not cond:
        failures.append(name)
    print(("[PASS] " if cond else "[FAIL] ") + name + (("  -- " + str(detail)) if detail and not cond else ""))


# ══════════════════════════════════════════════════════════════════
# A) 静态：字段 / 传参与共用实现
# ══════════════════════════════════════════════════════════════════
print("=== A) 静态检查 ===")
pw_src = open(os.path.join(LOCAL, "print_watch.py"), encoding="utf-8").read()
st_src = open(os.path.join(LOCAL, "stats_server.py"), encoding="utf-8").read()

m_cls = re.search(r"class ExternalPrint:\n(.*?)\n\n", pw_src, re.S)
check("A1 print_watch.ExternalPrint 有 simplex_pages 字段（带默认值）",
      bool(m_cls) and re.search(r"^\s+simplex_pages\s*:\s*str\s*=", m_cls.group(1), re.M) is not None)

m_amt = re.search(r"def amount_for\(.*?\n(?=\ndef |\n\n# )", pw_src, re.S)
check("A2 amount_for 把 simplex_pages 传进 calc_cost",
      bool(m_amt) and re.search(r"simplex_pages\s*=", m_amt.group(0)) is not None,
      "amount_for 里没有 simplex_pages= —— 外部打印会按全双面算")

check("A3 stats_server 引入 manual_duplex.charge_units（不再自己算张数）",
      re.search(r"from manual_duplex import charge_units|manual_duplex\.charge_units", st_src) is not None)
# 只查**代码**：注释/文档串里写旧算法（"原来这里是 (eff + 1) // 2"）不算违规
_st_code = re.sub(r'"""[\s\S]*?"""', "", st_src)
_st_code = "\n".join(ln for ln in _st_code.splitlines() if not ln.lstrip().startswith("#"))
check("A3b stats_server 里没有第二份 (eff+1)//2 张数算法",
      re.search(r"\(\s*eff\s*\+\s*1\s*\)\s*//\s*2", _st_code) is None)
check("A3c stats_server 用共用张数函数产出 sheets",
      re.search(r"\"sheets\"\s*:\s*_file_sheets|sheets\s*=\s*_file_sheets", st_src) is not None)
check("A4 stats_server 的本地文件明细下发 simplex_pages（页面复算要摊纸）",
      re.search(r"\"simplex_pages\"\s*:", st_src) is not None)

# ══════════════════════════════════════════════════════════════════
# B) 行为：print_watch 金额
# ══════════════════════════════════════════════════════════════════
print("\n=== B) 外部打印金额（单面 0.30 / 双面 0.40）===")
import print_watch as pw  # noqa: E402


def amt(**kw):
    try:
        return pw.amount_for(pw.ExternalPrint(**kw), 0.3, 0.4)
    except TypeError as exc:
        return "TypeError: %s" % exc


check("B1 4 页双面 + 封面单独打印 1 页 → 1 双面张 + 2 单面张 = 1.00",
      amt(pages=4, duplex="on", simplex_pages="1") == 1.00,
      amt(pages=4, duplex="on", simplex_pages="1"))
check("B1b 同一单没有 simplex 时仍是 2 双面张 = 0.80",
      amt(pages=4, duplex="on") == 0.80, amt(pages=4, duplex="on"))
check("B1c 3 页双面 + 封面单独打印 1 页 → 0.40 + 0.30 = 0.70",
      amt(pages=3, duplex="on", simplex_pages="1") == 0.70,
      amt(pages=3, duplex="on", simplex_pages="1"))
check("B1d 单面打印不受 simplex 影响（4 页 → 4 张 = 1.20）",
      amt(pages=4, duplex="off", simplex_pages="1") == 1.20,
      amt(pages=4, duplex="off", simplex_pages="1"))

try:
    d = pw.ExternalPrint(job_id=7, pages=4, duplex="on", simplex_pages="1,3").to_dict()
except TypeError as exc:
    d = {}
    print("  （构造带 simplex_pages 的 ExternalPrint 失败：%s）" % exc)
check("B2 to_dict/from_dict 保留 simplex_pages", d.get("simplex_pages") == "1,3", d.get("simplex_pages"))
old = pw.ExternalPrint.from_dict({"job_id": 8, "pages": 4, "duplex": "on"})
check("B3 旧账本行没有 simplex_pages → 默认空串（老数据按全双面，不会崩）",
      old is not None and getattr(old, "simplex_pages", None) == "",
      getattr(old, "simplex_pages", "<无此字段>") if old else None)

# ══════════════════════════════════════════════════════════════════
# C) 行为：stats_server 本地报表张数
# ══════════════════════════════════════════════════════════════════
print("\n=== C) 本地报表张数 ===")
import stats_server as st  # noqa: E402
import printer_config as local_cfg  # noqa: E402
import manual_duplex  # noqa: E402

_fs = getattr(st, "_file_sheets", None)
check("C0 stats_server 暴露共用张数函数 _file_sheets(page_count, duplex, page_range, simplex_pages)",
      callable(_fs))
if callable(_fs):
    check("C1 4 页双面 + simplex=1 → (4 页, 3 张)（旧 (eff+1)//2 = 2 张）",
          _fs(4, "on", "", "1") == (4, 3), _fs(4, "on", "", "1"))
    check("C2 4 页双面无 simplex → (4, 2)", _fs(4, "on", "", "") == (4, 2), _fs(4, "on", "", ""))
    check("C3 范围 1,3,5 + simplex=1 → (3, 2)：段边界只认「是否单面页」，不看页号连号",
          _fs(5, "on", "1,3,5", "1") == (3, 2), _fs(5, "on", "1,3,5", "1"))
    check("C4 单面打印忽略 simplex：9 页取 2-4 → (3, 3)", _fs(9, "off", "2-4", "1") == (3, 3),
          _fs(9, "off", "2-4", "1"))

    # 与唯一实现逐组对拍（任何一端漂移都会红）
    bad = []
    for pc in (1, 2, 3, 4, 5, 9, 10):
        for dup in ("on", "off"):
            for pr in ("", "1-3", "2", "1,3,5"):
                for sx in ("", "1", "1,3", "2,4"):
                    eff = local_cfg._count_pages_in_range(pr, pc)
                    if dup == "on":
                        want = manual_duplex.charge_units(
                            local_cfg.selected_pages(pr, pc),
                            local_cfg.simplex_page_set(pr, pc, sx))
                        want = (eff, want[0] + want[1])
                    else:
                        want = (eff, eff)
                    got = _fs(pc, dup, pr, sx)
                    if got != want:
                        bad.append((pc, dup, pr, sx, got, want))
    check("C5 与 manual_duplex.charge_units 逐组一致（%d 组）" % (7 * 2 * 4 * 4), not bad,
          "不一致 %d 组，例：%r" % (len(bad), bad[0] if bad else None))

    # 金额与张数同口径：统计出来 3 张，按同一套单价就该是 1.00
    eff, sheets = _fs(4, "on", "", "1")
    check("C6 张数口径与 calc_cost 金额同源（4 页双面 + simplex=1 → 3 张 → 1.00）",
          sheets == 3 and local_cfg.calc_cost(4, 1, "on", 0.3, 0.4, "", "1")[0] == 1.00)

print()
if failures:
    print("本地 simplex 口径验收：FAIL（%d 项）" % len(failures))
    for f in failures:
        print("  ✗ " + f)
    sys.exit(1)
print("本地 simplex 口径验收：全部通过 ✅")
