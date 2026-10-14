# -*- coding: utf-8 -*-
"""审计修复验收 · 🔴-5 首页 PDF 文本未转义（收钱没交付且静默）

事故形状：generate_cover_page_pdf 把用户可控文本直接交给 reportlab Paragraph，
而 Paragraph 按富文本标记解析：
 · `A&B报价.docx` 印成 `A&B;报价.docx`（`&` 被当实体起始）；
 · `规格<A>B&C.docx` 里的 `<A>` 被当标记 ⇒ 抛 ValueError；
 · 而唯一的 try 只包住 `doc.build()`，构建 story 期间的异常直接穿出函数
   ⇒ 首页（收费项）没打印，界面只看到异常/无首页。
修复后：所有用户可控文本过 _esc()，且整段 story 构建纳入 try。

用 PyMuPDF 抽取生成 PDF 的文本，断言字符**按字面渲染**（而非标记泄漏）。
"""
import io
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
from printer_config import PrinterConfig, PrintJob  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


TMP = tempfile.mkdtemp(prefix="hn_cover_")


def extract_text(path):
    import fitz
    doc = fitz.open(path)
    try:
        return "\n".join(p.get_text() for p in doc)
    finally:
        doc.close()


def make_cfg(delivery_loc="Bldg&A<3>FL", urgency="<URG>", pickup="Pick&Up<2>",
             delivery_enabled=True):
    cfg = PrinterConfig()
    cfg.delivery_enabled = delivery_enabled
    cfg.delivery_location = delivery_loc
    cfg.delivery_percentages = {delivery_loc: 10.0}
    cfg.urgency = urgency
    cfg.urgency_prices = {urgency: 2.0}
    cfg.pickup_address = pickup
    return cfg


print("=== 1. _esc 单元行为 ===")
check("& < > 全部转义", pc._esc("A&B<C>") == "A&amp;B&lt;C&gt;", pc._esc("A&B<C>"))
check("& 先行替换（不产生 &amp;amp; 之外的错误嵌套）",
      pc._esc("&amp;") == "&amp;amp;", pc._esc("&amp;"))
check("None → 空串（不抛异常）", pc._esc(None) == "", repr(pc._esc(None)))
check("非字符串按原类型转成文本", pc._esc(123) == "123", repr(pc._esc(123)))

print("\n=== 2. 派送场景：文件名/页码范围/备注/地址/优先级/订单号 含特殊字符 ===")
fname_spec = "spec<A>B&C.docx"
range_spec = "1-5<x>"
remark_spec = "note<&>here"
order_no_spec = "HN<2026>&001"
job = PrintJob(file_path=os.path.join(TMP, fname_spec), copies=2, duplex="on",
               page_count=3, page_range=range_spec, remark=remark_spec)
out1 = os.path.join(TMP, "cover1.pdf")
try:
    ok1 = pc.generate_cover_page_pdf(out1, make_cfg(), [job],
                                     order_number=order_no_spec,
                                     created_at="2026-10-14 10:00:00")
    err1 = ""
except Exception as e:
    ok1, err1 = False, repr(e)
check("含 < > & 的输入不再抛异常且返回 True", ok1 is True, err1)
if ok1:
    t1 = extract_text(out1)
    check("文件名按字面渲染 <A>B&C", "spec<A>B&C.docx" in t1,
          "" if "spec<A>B&C.docx" in t1 else repr(t1[:400]))
    check("页码范围按字面渲染 1-5<x>", range_spec in t1)
    check("备注按字面渲染 note<&>here", remark_spec in t1)
    check("订单号按字面渲染 HN<2026>&001", order_no_spec in t1)
    check("派送地址按字面渲染 Bldg&A<3>FL", "Bldg&A<3>FL" in t1)
    check("优先级按字面渲染 <URG>", "<URG>" in t1)
    check("无转义实体泄漏（&amp; / &lt; / &gt; 均不应出现在 PDF 文本中）",
          not any(x in t1 for x in ("&amp;", "&lt;", "&gt;")),
          "; ".join(x for x in ("&amp;", "&lt;", "&gt;") if x in t1))

print("\n=== 3. 自取场景：自取地址含特殊字符 ===")
out2 = os.path.join(TMP, "cover2.pdf")
job2 = PrintJob(file_path=os.path.join(TMP, "plain.docx"), copies=1, page_count=2)
ok2 = pc.generate_cover_page_pdf(out2, make_cfg(delivery_enabled=False), [job2])
check("自取场景生成成功", ok2 is True)
if ok2:
    t2 = extract_text(out2)
    check("自取地址按字面渲染 Pick&Up<2>", "Pick&Up<2>" in t2,
          "" if "Pick&Up<2>" in t2 else repr(t2[:400]))

print("\n=== 4. 构建期异常被外层 try 接住（改前直接穿出函数）===")
out3 = os.path.join(TMP, "cover3.pdf")
bad_job = PrintJob(file_path=os.path.join(TMP, "x.docx"), page_count=1)
bad_job.remark = None            # 触发 .strip() 的 AttributeError（构建期，非 build 期）
try:
    ok3 = pc.generate_cover_page_pdf(out3, make_cfg(), [bad_job])
    raised = ""
except Exception as e:
    ok3, raised = None, repr(e)
check("构建期异常不穿出函数（返回 False，便于上层记录/重试）", ok3 is False,
      raised or f"ok3={ok3}")
check("失败时不产生半成品 PDF", not os.path.exists(out3),
      f"exists={os.path.exists(out3)}")

print("\n" + ("全部通过" if ok_all else "存在失败项"))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if ok_all else 1)
