# -*- coding: utf-8 -*-
"""审计修复验收 · 🔴-7 配置被写坏即无法启动

事故形状：PrinterConfig.load() 的 except 只捕 (json.JSONDecodeError, OSError,
ValueError)，而：
 · 键存在但值为 null → data.get 返回 None（不取默认）→ float(None)/int(None) 抛
   **TypeError**；
 · 根节点被写成数组 → data.get 抛 **AttributeError**；
两者都不在捕获列表里，而 load() 是 MainWindow.__init__ 的第一条语句且外层无保护
⇒ 手改坏一个 `null` 就让程序**启动即崩**（根本进不了界面去改回来）。

修复后有两级防护：
 ① from_dict 逐字段归一（_num_or_default / _dict_or_default）—— 坏一个值只丢那个值；
 ② load 的 except 补 TypeError/AttributeError 兜底 —— 极端结构错也能启动。
"""
import io
import json
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import printer_config as pc                       # noqa: E402
from printer_config import PrinterConfig, PrintJob, TabSettings  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


TMP = tempfile.mkdtemp(prefix="hn_badcfg_")


def write_raw(name, text):
    p = os.path.join(TMP, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def try_load(raw_text, name):
    """写坏配置并 load，返回 (是否抛异常, 异常repr, 配置对象, 备份文件列表)。"""
    p = write_raw(name, raw_text)
    try:
        cfg = PrinterConfig.load(p)
        exc = ""
    except Exception as e:
        return True, repr(e), None, []
    corrupts = [n for n in os.listdir(TMP) if n.startswith(name + ".corrupt-")]
    return False, exc, cfg, corrupts


print("=== 1. 键存在但值为 null（float(None)/int(None) → TypeError）===")
raised, exc, cfg, corrupts = try_load(
    '{"printer_name": "HP LaserJet", "simplex_price": 0.5, "cover_page_price": null,'
    ' "duplex_price": null, "render_dpi": null, "last_order_number": null}',
    "null_values.json")
check("load() 不抛异常", not raised, exc)
if cfg:
    check("坏值归一到默认（cover_page_price=0.10）", cfg.cover_page_price == 0.10,
          f"{cfg.cover_page_price}")
    check("坏值归一到默认（duplex_price=0.3）", cfg.duplex_price == 0.3, f"{cfg.duplex_price}")
    check("坏值归一到默认（render_dpi=0=自动）", cfg.render_dpi == 0, f"{cfg.render_dpi}")
    check("坏值归一到默认（last_order_number=0）", cfg.last_order_number == 0,
          f"{cfg.last_order_number}")
    check("**其余配置完整保留**（同为 0.5 的 simplex_price 未被牵连）",
          cfg.simplex_price == 0.5, f"{cfg.simplex_price}")
    check("其余配置完整保留（printer_name）", cfg.printer_name == "HP LaserJet",
          repr(cfg.printer_name))
    check("未因单字段坏值而把整份配置判废（无 .corrupt 备份）", not corrupts,
          f"corrupts={corrupts}")

print("\n=== 2. 根节点是数组（data.get → AttributeError）===")
raised, exc, cfg, corrupts = try_load("[1, 2, 3]", "array_root.json")
check("load() 不抛异常（改前 AttributeError 穿出 ⇒ 启动崩）", not raised, exc)
check("返回默认配置", cfg is not None and cfg.printer_name == "" and cfg.render_dpi == 0,
      f"cfg={cfg!r}")
check("损坏文件改名保留为 .corrupt-*（不静默丢失）", len(corrupts) == 1, f"{corrupts}")

print("\n=== 3. 截断 JSON（JSONDecodeError，原有分支）===")
raised, exc, cfg, corrupts = try_load('{"printer_name": "HP", "tabs": {"1": [',
                                      "truncated.json")
check("load() 不抛异常", not raised, exc)
check("返回默认配置", cfg is not None and cfg.render_dpi == 0, f"cfg={cfg!r}")
check("截断文件改名保留", len(corrupts) == 1, f"{corrupts}")

print("\n=== 4. dict 字段被写成列表/字符串（.get/.items 会 AttributeError）===")
raised, exc, cfg, corrupts = try_load(
    '{"delivery_percentages": [1,2], "urgency_prices": "x", "printer_profiles": [1],'
    ' "delivery_locations": 42}',
    "wrong_dict_type.json")
check("load() 不抛异常", not raised, exc)
if cfg:
    check("delivery_percentages 回落默认（含 1号楼北楼）",
          isinstance(cfg.delivery_percentages, dict) and "1号楼北楼" in cfg.delivery_percentages,
          f"{cfg.delivery_percentages}")
    check("urgency_prices 回落默认（含 低/中/高）",
          isinstance(cfg.urgency_prices, dict) and set(cfg.urgency_prices) >= {"低", "中", "高"},
          f"{cfg.urgency_prices}")
    check("printer_profiles 回落空 dict", cfg.printer_profiles == {},
          f"{cfg.printer_profiles}")

print("\n=== 5. tabs 键被写成列表（tabs_data.items → AttributeError）===")
raised, exc, cfg, corrupts = try_load('{"tabs": [1,2,3]}', "tabs_list.json")
check("load() 不抛异常", not raised, exc)
check("tabs 回落为单个空标签页", cfg is not None and set(cfg.tabs) == {"1"}, f"{cfg and cfg.tabs}")

print("\n=== 6. PrintJob / TabSettings 内层字段坏值 ===")
p = PrintJob.from_dict({"file_path": "a.docx", "copies": None, "page_count": "abc",
                        "dpi": None, "task_id": None, "order_id": "7"})
check("PrintJob.copies=null → 1", p.copies == 1, f"{p.copies}")
check("PrintJob.page_count='abc' → 0", p.page_count == 0, f"{p.page_count}")
check("PrintJob.dpi=null → 0", p.dpi == 0, f"{p.dpi}")
check("PrintJob.task_id=null → 0", p.task_id == 0, f"{p.task_id}")
check("PrintJob.order_id='7' → 7（合法字符串仍接受）", p.order_id == 7, f"{p.order_id}")

t = TabSettings.from_dict({"cover_page_price": None, "is_admin_print": None,
                           "jobs": [{"file_path": "b.docx"}]})
check("TabSettings.cover_page_price=null → 0.10", t.cover_page_price == 0.10,
      f"{t.cover_page_price}")
check("TabSettings.is_admin_print=null → True（危险默认值回落，不靠 bool() 巧合）",
      t.is_admin_print is True, f"{t.is_admin_print}")

print("\n=== 7. 正常配置不受影响（零回归）===")
p = write_raw("normal.json", json.dumps({
    "printer_name": "HP", "render_dpi": 300, "simplex_price": 0.25, "duplex_price": 0.4,
    "cover_page_price": 0.05, "last_order_number": 12, "delivery_location": "图书馆",
    "tabs": {"1": [{"file_path": "x.docx", "copies": 3, "page_count": 9}]},
}, ensure_ascii=False))
cfg = PrinterConfig.load(p)
check("正常值原样读取（不被归一改写）",
      cfg.render_dpi == 300 and cfg.simplex_price == 0.25 and cfg.duplex_price == 0.4
      and cfg.cover_page_price == 0.05 and cfg.last_order_number == 12,
      f"dpi={cfg.render_dpi} sp={cfg.simplex_price} dp={cfg.duplex_price}"
      f" cpp={cfg.cover_page_price} lon={cfg.last_order_number}")
check("标签页任务原样读取", cfg.tabs["1"].jobs[0].copies == 3 and cfg.tabs["1"].jobs[0].page_count == 9,
      f"{cfg.tabs['1'].jobs[0]}")
check("delivery_location 原样读取", cfg.delivery_location == "图书馆", repr(cfg.delivery_location))

print("\n" + ("全部通过" if ok_all else "存在失败项"))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if ok_all else 1)
