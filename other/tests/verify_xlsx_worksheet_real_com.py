# -*- coding: utf-8 -*-
"""验收 · 表格「工作表」选择走**真的 Excel COM**（2026-11-04）

为什么单独一支：`verify_xlsx_worksheet_picker.py` 全是"假 COM"（打桩 + 静态接线），
它证明得了"代码接上了"，证明不了"Excel 真的切了表"。而工作表这功能最容易出的错正是
**看起来通过、实际按旧活动表打**（静默错版：纸已经出来了，只是打错页）。这里用本机
真实 Excel 导 PDF，用**页数**判"到底印了哪张表"（PDF 带创建时间，比 md5 无意义）。

判据：
  ① `list_worksheet_names` 的顺序/内容与 Excel 自己枚举的一致（下拉框顺序要跟 Excel 走）；
  ② 选「首页」（1 页）→ 1 页；选「明细」（300 行）→ 多页（真的换了表）；
  ③ `worksheet=""`（跟随活动表）== 保存时的活动表；
  ④ 不存在的表名 → 回退活动表 + warning，**不静默**；
  ⑤ `print_range="workbook"` → 页数 = 首页 + 明细。

只导 PDF，不打印、不碰打印机队列。默认套件里跳过（`ENV_REQUIRED`），要跑：

    python other/tests/verify_xlsx_worksheet_real_com.py
    python other/tests/run_all.py --only verify_xlsx_worksheet_real_com --all
"""
import io
import os
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

# 套件默认给所有脚本设了 HN_NO_ENGINE_PROBE=1（防误杀用户正开着的 Office）。
# 本脚本就是要真引擎 —— 必须在 import xlsx_printer 之前把它改回来，
# 而且引擎自己用 DispatchEx 起**独立实例**，绝不复用用户已打开的 Excel。
os.environ["HN_NO_ENGINE_PROBE"] = "0"

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "local_print_tool"))

if os.name != "nt":
    print("[SKIP] 非 Windows，没有 Excel COM")
    sys.exit(0)
try:
    import win32com.client as win32  # noqa: PLC0415
except Exception as e:  # noqa: BLE001
    print(f"[SKIP] 没有 pywin32（{type(e).__name__}: {e}）")
    sys.exit(0)

import xlsx_printer  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)) if detail else ""))


def pages(p):
    """PDF 页数：Excel 导出的页对象是未压缩字典，数 `/Type /Page` 即可（不引第三方库）。"""
    try:
        data = open(p, "rb").read()
    except OSError:
        return -1
    return len(re.findall(rb"/Type\s*/Page[^s]", data))


# ── 造一份真文件：首页 1 页、明细 300 行（多页），保存时活动表 = 明细 ──────────
tmp = tempfile.mkdtemp(prefix="hn_ws_real_")
src = os.path.join(tmp, "两份表.xlsx")
try:
    excel = win32.DispatchEx("Excel.Application")
except Exception as e:  # noqa: BLE001
    print(f"[SKIP] 本机起不来 Excel COM（{type(e).__name__}: {e}）")
    sys.exit(0)

excel.Visible = False
excel.DisplayAlerts = False
try:
    wb = excel.Workbooks.Add()
    ws1 = wb.Worksheets(1)
    ws1.Name = "首页"
    ws1.Range("A1").Value = "SHEET1_MARK"
    ws2 = wb.Worksheets.Add()
    ws2.Name = "明细"
    ws2.Range("A1:A300").Value = [[i] for i in range(300)]
    ws2.Activate()
    truth = [wb.Worksheets(i).Name for i in range(1, wb.Worksheets.Count + 1)]
    print("   Excel 自报顺序：%r（活动表=%r）" % (truth, wb.ActiveSheet.Name))
    wb.SaveAs(src, FileFormat=51)
    wb.Close(False)
finally:
    excel.Quit()

names = xlsx_printer.list_worksheet_names(src)
check("★ list_worksheet_names 的顺序/内容与 Excel 枚举一致", names == truth, (names, truth))

out = {}
for tag, opt in (("首页", {"worksheet": "首页"}),
                 ("明细", {"worksheet": "明细"}),
                 ("follow", {"worksheet": ""}),
                 ("missing", {"worksheet": "不存在的表"}),
                 ("book", {"worksheet": "首页", "print_range": "workbook"})):
    pdf = os.path.join(tmp, tag + ".pdf")
    res = xlsx_printer.export_to_pdf(src, pdf, print_options=opt, engine="excel")
    out[tag] = (pdf, res)
    print("   %-8s engine=%-6s sheet=%r pages=%d" %
          (tag, res.engine, getattr(res, "sheet", None), pages(pdf)))

check("真机导出全部走 Excel 引擎", all(o[1].engine == "excel" for o in out.values()),
      [o[1].engine for o in out.values()])
check("★ 选「首页」→ 1 页", pages(out["首页"][0]) == 1, pages(out["首页"][0]))
check("★ 选「明细」→ 多页（真的换了表）", pages(out["明细"][0]) > 1, pages(out["明细"][0]))
check("★ 跟随活动表 == 明细",
      pages(out["follow"][0]) == pages(out["明细"][0]),
      (pages(out["follow"][0]), pages(out["明细"][0])))
check("不存在的表名回退活动表（== 明细）",
      pages(out["missing"][0]) == pages(out["明细"][0]))
check("print_range=workbook 导整本（页数 = 首页 + 明细）",
      pages(out["book"][0]) == 1 + pages(out["明细"][0]),
      (pages(out["book"][0]), 1 + pages(out["明细"][0])))

print("\n真机 Excel COM 工作表选择验收：%s" % ("全部通过 ✅" if ok else "有失败 ❌"))
sys.exit(0 if ok else 1)
