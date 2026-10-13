"""实机校准：给"不支持自动双面"的打印机验证手动双面页序是否正确。

打印侧适配改完之后，唯一没法在没打印机的机器上验证的就是**物理出纸顺序**：第 2 趟到底
该逆序还是正序、长边/短边要不要把整叠平面转 180°、空白占位页位置对不对。这个脚本把那次
实机验证压成几分钟：

    python tools/print_duplex_calibration.py --dry-run              # 不打印，只看方案
    python tools/print_duplex_calibration.py "HP LaserJet 1020"     # 真打 5 页，跑两趟

它会：
  1. 生成一份带页号 + 上下左右标记的校准 PDF（页号够大，隔着纸叠也能一眼看出配对）；
  2. 探测目标打印机是否支持自动双面（不会打纸，只查能力位 + DEVMODE 回读）；
  3. 用 manual_duplex 引擎算两趟页序，打完第 1 趟后**停下来让你按 HP 弹窗的原话翻纸**；
  4. 打完第 2 趟后打印核对清单：每张纸该是 1/2、3/4…；哪一项不对该改哪个开关。

页序算法不在这里：唯一真相是 local_print_tool/manual_duplex.py
（有独立验收 tests/verify_manual_duplex.py，54 种组合物理模拟通过）。
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import manual_duplex as md          # noqa: E402
import pdf_printer as pp            # noqa: E402


# ============================================================
# 校准 PDF
# ============================================================

def build_calibration_pdf(path: str, pages: int = 5) -> str:
    """生成校准页：每页大字页号 + 四周方向标记（用来判断配对与是否倒置）。

    中文字体按项目既有约定注册（微软雅黑 → SimSun → 回退 Helvetica 只能打英文）——
    reportlab 内置字体没有中文字形，不注册的话"第 N 页"这类字会整段丢失/变方块。
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    font_cjk = "Helvetica"          # 兜底：只保证英文可读
    for name, fname in (("CalCJK", "msyh.ttc"), ("CalCJK", "msyhbd.ttc"),
                        ("CalCJK", "simsun.ttc"), ("CalCJK", "simhei.ttf")):
        p = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", fname)
        if os.path.isfile(p):
            try:
                pdfmetrics.registerFont(TTFont(name, p, subfontIndex=0))
                font_cjk = name
                break
            except Exception:
                continue

    w, h = A4
    c = canvas.Canvas(path, pagesize=A4)
    for n in range(1, pages + 1):
        c.setLineWidth(1)
        c.rect(28, 28, w - 56, h - 56)                      # 外框
        c.setFont("Helvetica-Bold", 13)
        c.drawCentredString(w / 2, h - 50, "MANUAL DUPLEX CALIBRATION")
        c.setFont(font_cjk, 9)
        c.drawCentredString(w / 2, h - 66, "手动双面校准页 · tools/print_duplex_calibration.py")

        # 四周方向标记（倒置/错位一眼可见；中英都给，字体缺字也不至于失去判据）
        c.setFont(font_cjk, 16)
        c.drawCentredString(w / 2, h - 100, "TOP  ▲  上")
        c.drawCentredString(w / 2, 60, "▼  BOTTOM  下")
        c.saveState()
        c.translate(60, h / 2)
        c.rotate(90)
        c.drawCentredString(0, 0, "LEFT  <  左")      # 用 ASCII 箭头：YaHei 缺 ◀▶ 字形
        c.restoreState()
        c.saveState()
        c.translate(w - 60, h / 2)
        c.rotate(-90)
        c.drawCentredString(0, 0, "RIGHT  >  右")
        c.restoreState()

        # 大字页号（隔着纸叠也能看清配对）—— 纯 ASCII，用内置字体保证一定打得出来
        c.setFont("Helvetica-Bold", 150)
        c.drawCentredString(w / 2, h / 2 + 30, f"P{n}")
        c.setFont(font_cjk, 30)
        c.drawCentredString(w / 2, h / 2 - 60, f"第 {n} 页 / 共 {pages} 页")

        c.setFont(font_cjk, 9)
        c.drawCentredString(w / 2, h / 2 - 100,
                            "本页应与相邻页共处同一张纸的两面（P1/P2、P3/P4 …）")
        c.drawCentredString(w / 2, h / 2 - 112,
                            "若某一面文字倒置 → 按清单调整长边/短边放纸方向")
        c.setFont("Helvetica", 9)
        c.drawCentredString(40, 34, f"page {n} of {pages}")
        c.showPage()
    c.save()
    return path


# ============================================================
# 方案 / 核对清单
# ============================================================

def show_plan(plan: md.PrintPlan, pages: int, copies: int, first: str, forward: bool,
              second_rotate: int = 180) -> None:
    print("=" * 78)
    print(f"校准文档: {pages} 页 × {copies} 份 → 共 {plan.sheet_count} 张纸")
    print(f"约定: 第 1 趟打{'偶数面' if first != md.FIRST_ODD else '奇数面'}；"
          f"第 2 趟{'正序（--forward）' if forward else '逆序（整叠不翻面）'}；"
          f"第 2 趟内容旋转 {second_rotate}°")
    print("-" * 78)
    print("第 1 趟: " + " ".join("〔空白〕" if p is None else str(p) for p in plan.pass_pages(1)))
    print("第 2 趟: " + " ".join("〔空白〕" if p is None else str(p) for p in plan.pass_pages(2)))
    print("每张纸的两面应为: " + "，".join(plan.pair_lines()))
    print("=" * 78)


def show_checklist(pages: int, copies: int) -> None:
    print("\n" + "=" * 78)
    print("★ 拿起成品，逐张核对（5 分钟）：")
    print("=" * 78)
    print(f"1) 拿出一张纸看两面 —— 应恰好是 P1/P2 这一对（1 与 2 在同一张纸上）。")
    print(f"   若配对错开一页（如 P1 的背面是 P4）→ 空白占位页位置不对，回看第 1/2 趟序列。")
    print(f"2) 检查纸张顺序 —— 从最上面一张往下应是 P1、P3、P5…（odd 约定 / 默认；")
    print(f"   每张纸的正面朝上，符合「拿起一叠从第 1 页开始翻」的直觉）。")
    print(f"   若最上面变成 P2、P4… → 加 --first even（镜像约定，配对同样正确，只是观感反过来）；")
    print(f"   若整叠的页序整体倒过来（P1/P2 跑到最后一张）→ 加 --forward 重跑。")
    print(f"3) 若某一张纸的两面内容是【同一对但位置相反】(P2 在上、P1 在下) → 加 --forward 重跑。")
    print(f"4) 方向 —— 两张面的文字都应该是【正立】的（长边/书本式）：")
    print(f"   · 第 2 趟那一面整体倒置 → 用 --second-rotate 180（默认已是 180，HP 1020 实测值）")
    print(f"   · 若 180 反而倒置 → 用 --second-rotate 0，并让操作员把整叠平面内转 180° 再放")
    print(f"     （页序不受影响；这一项随打印机型号的纸路径而变，实测为准）")
    print(f"5) 奇数页文档（如 5 页）会有一张纸只有一面有内容（另一面是空白占位）——")
    print(f"   这是**故意的**，对应 HP 驱动弹窗那句「不要丢弃空白页面」，别把它抽走。")
    if copies > 1:
        print(f"6) {copies} 份的边界：第 1 份的最后一张与第 2 份的第一张不应混在一张纸上。")
    print("=" * 78)
    print("结论落到两处：① 本工具的 --first / --forward；② 服务端该设备的能力标注")
    print("（收支清算「授权」页 → 自动双面 → 不支持（需人工翻纸））。")


def show_flip_instructions() -> None:
    """人工翻纸的指令 —— 原文来自 HP 驱动「双面打印说明」弹窗，逐字对齐。"""
    print("\n" + "=" * 78)
    print("请按下面三步操作（原文来自 HP 驱动「双面打印说明」弹窗）：")
    print("  1. 不改变方向，从出纸槽中取出纸叠。不要丢弃空白页面。")
    print("  2. 保持相同方向，将纸叠插入纸盘 2，使已打印面朝下。")
    print("     （长边/书本式默认：原样放回，别翻面、别重排；")
    print("       短边/便签式：整叠在平面内转 180° 再放）")
    print("  3. 继续打第 2 趟。")
    print("=" * 78)


def probe(printer: str) -> dict:
    v = pp.get_printer_duplex_support(printer, refresh=True)
    print(f"能力探测: {v['printer']}")
    print(f"  驱动={v['driver'] or '?'}  端口={v['port'] or '?'}")
    print(f"  DC_DUPLEX={v['dc_duplex']}  驱动接受双面={v['dm_accepted']}")
    print(f"  判定: auto_duplex={v['auto_duplex']} 置信度={v['confidence']} — {v['detail']}")
    if v["auto_duplex"] is True and v["confidence"] != "high":
        print("  ⚠ 只是「驱动声明支持」：HP 1018/1020/1022 这类只有手动双面的机型也会这么报，")
        print("    所以下面的手动双面校准仍然要做。")
    return v


def save_profile(args, pages: int, copies: int) -> bool:
    """把实测参数写进 print_config.json 里该打印队列的档案。

    只登记"这台机器怎么做手动双面"这几项；`auto_duplex` 默认记 False（会跑手动双面校准的
    机器通常就是没有双面器的 —— HP LaserJet 1018/1020/1022 正是如此），要记成支持请加
    `--auto-duplex`。人工结论一旦落盘就不会再被自动探测覆盖（PrinterProfile.apply_probe 保证）。
    """
    from datetime import datetime
    from printer_config import update_user_printer_profile
    try:
        prof = update_user_printer_profile(
            args.printer,
            auto_duplex=bool(args.auto_duplex),
            capability_source="manual",
            manual_duplex=not args.auto_duplex,
            first=args.first,
            reverse_second=not args.forward,
            second_rotate_180=bool(args.second_rotate),
            verified_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            verified_by=args.verified_by,
            note=(f"实机校准 {pages} 页 × {copies} 份：配对/叠序/方向经人眼确认"
                  f"（first={args.first}，{'逆序' if not args.forward else '正序'}，"
                  f"旋转 {args.second_rotate}°）"),
        )
    except Exception as e:
        print(f"✗ 写入打印机档案失败: {e}")
        return False
    print("\n✅ 已写入打印机档案（print_config.json）:")
    print(f"   {args.printer} → {prof.summary()}")
    print("   （GUI 会按这份档案决定：双面请求走「程序自己分两趟 + 人工翻纸」）")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="手动双面实机校准（会真打纸）")
    ap.add_argument("printer", nargs="?", default="", help="打印队列名（真打时必填）")
    ap.add_argument("--pages", type=int, default=5, help="校准页数（默认 5，覆盖奇数页场景）")
    ap.add_argument("--copies", type=int, default=1, help="份数（默认 1）")
    ap.add_argument("--first", choices=[md.FIRST_EVEN, md.FIRST_ODD], default=None,
                    help="第 1 趟打哪一面（缺省：用该队列已校准档案，无档案则 odd"
                         "——成品叠最上面是第 1 页；even 为镜像）")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--forward", action="store_true", default=None, help="强制第 2 趟正序")
    grp.add_argument("--reverse", action="store_true", default=None, help="强制第 2 趟逆序（覆盖档案）")
    ap.add_argument("--second-rotate", type=int, choices=[0, 180], default=None,
                    help="第 2 趟内容旋转补偿（缺省：用该队列已校准档案，无档案则 180"
                         "——出纸 face-down + 整叠原样放回；实测 HP 1020 = 180）")
    ap.add_argument("--only-pass", type=int, choices=[1, 2], default=0,
                    help="只打某一趟（人工翻纸分两次跑，或远程指导现场；参数须与上一次一致）")
    ap.add_argument("--save", action="store_true",
                    help="本次校准结束后，把实测参数写入 print_config.json（按该打印队列名存档）")
    ap.add_argument("--save-only", action="store_true",
                    help="不打印，只把当前参数写入档案（配合 --only-pass 分两次校准后的收尾）")
    ap.add_argument("--verified-by", default="", help="校准者（成员姓名），随档案记录")
    ap.add_argument("--auto-duplex", action="store_true",
                    help="档案里记为「支持自动双面」（默认记为不支持/需人工翻纸 —— "
                         "会跑手动双面校准的机器通常就是没有双面器的）")
    ap.add_argument("--dry-run", action="store_true", help="只打印方案，不碰打印机")
    ap.add_argument("--keep-pdf", action="store_true", help="保留生成的校准 PDF 以便检查")
    ap.add_argument("--speed", action="store_true",
                    help="两点测速标定：打 1 张 + 10 张空白页（单面，不需要翻纸），"
                         "测出「启动开销 + 每张纸耗时」写进档案（翻纸窗的预计耗时用它）")
    ap.add_argument("--speed-pages", default="1,10",
                    help="--speed 的两点页数（默认 1,10）")
    args = ap.parse_args()

    # ── 只做测速标定（与双面页序校准互不干扰，可单独跑）──
    if args.speed:
        if not args.printer:
            print("✗ --speed 必须给出打印队列名（真打）。")
            return 2
        try:
            small, large = (int(x) for x in str(args.speed_pages).split(","))
        except ValueError:
            print("✗ --speed-pages 格式应为「小,大」，例如 1,10")
            return 2
        import pdf_printer as _pp
        from printer_config import load_user_printer_profile, update_user_printer_profile
        print(f"测速标定：{args.printer} —— 打 {small} 张 + {large} 张空白页（单面，共 {small + large} 张纸）")
        ok, msg, samples = _pp.measure_print_speed_two_point(
            args.printer, small=small, large=large, on_log=lambda m: print(m))
        if not ok:
            print(f"✗ {msg}")
            return 1
        prof = load_user_printer_profile(args.printer)
        for n, sec in samples:
            prof.add_speed_sample(n, sec)
        update_user_printer_profile(args.printer, speed_samples=prof.speed_samples)
        a, b = prof.speed_fit()
        print(f"✓ {msg}")
        print(f"  写入档案：启动 ≈{a:.1f}s + 每张 ≈{b:.1f}s（{prof.speed_summary()}）")
        for n in (1, 5, 10, 20):
            print(f"    预估：{n} 张 → {prof.estimate_first_pass(n):.0f}s")
        return 0

    # ── 参数来源：CLI 优先 > 该队列已校准档案 > 内置默认 ──
    # 让档案真正生效：同一台机器再校准/复测时不必每次重复敲参数。
    first, reverse_second, second_rotate = args.first, None, args.second_rotate
    if args.forward:
        reverse_second = False
    elif args.reverse:
        reverse_second = True
    src = "内置默认"
    if args.printer:
        try:
            from printer_config import load_user_printer_profile
            prof = load_user_printer_profile(args.printer)
            if prof.is_verified:
                src = f"已校准档案（{prof.summary()}）"
                first = first or prof.first
                second_rotate = second_rotate if second_rotate is not None \
                    else (180 if prof.second_rotate_180 else 0)
                if reverse_second is None:
                    reverse_second = prof.reverse_second
        except Exception as e:
            print(f"（读取打印机档案失败，用内置默认值继续: {e}）")
    first = first or md.FIRST_ODD
    if second_rotate is None:
        second_rotate = 180
    if reverse_second is None:
        reverse_second = True
    args.first, args.second_rotate = first, second_rotate
    args.forward = not reverse_second          # 兼容下游既有的"forward 表示正序"写法
    print(f"参数来源: {src} → first={first}, "
          f"{'逆序' if reverse_second else '正序'}, 旋转 {second_rotate}°")

    pages = max(1, args.pages)
    copies = max(1, args.copies)
    plan = md.plan_single(list(range(1, pages + 1)), copies=copies,
                          first=first, reverse_second=reverse_second)

    # ── 只写档案（分两次校准后的收尾，或补记参数）──
    if args.save_only:
        if not args.printer:
            print("✗ --save-only 必须给出打印队列名。")
            return 1
        show_plan(plan, pages, copies, args.first, args.forward, args.second_rotate)
        return 0 if save_profile(args, pages, copies) else 1

    if not args.dry_run and not args.printer:
        print("✗ 真打模式必须给出打印队列名（避免误打到系统默认打印机）。")
        print("  例如: python tools/print_duplex_calibration.py \"HP LaserJet 1020\"")
        print("  只想看方案: 加 --dry-run")
        return 1

    if args.dry_run:
        print("【--dry-run】不打印，只预演方案\n")
        show_plan(plan, pages, copies, args.first, args.forward, args.second_rotate)
        show_checklist(pages, copies)
        if args.save:
            print("\n（--dry-run 不写档案；确认无误后去掉 --dry-run 再跑）")
        return 0

    probe(args.printer)

    pdf_path = os.path.join(tempfile.gettempdir(), f"hn_duplex_calib_{os.getpid()}.pdf")
    build_calibration_pdf(pdf_path, pages)
    print(f"\n校准 PDF: {pdf_path}")

    try:
        show_plan(plan, pages, copies, args.first, args.forward, args.second_rotate)
        do_pass1 = args.only_pass in (0, 1)
        do_pass2 = args.only_pass in (0, 2)

        # ── 第 1 趟 ──
        if do_pass1:
            if args.only_pass == 0:
                print("\n▶ 第 1 趟：进纸盒放空白纸（{0} 张以上），回车开始...".format(plan.sheet_count))
                input()
            ok1, msg1 = pp.print_plan_pass(plan, pdf_path, args.printer, 1,
                                           second_rotate_180=bool(args.second_rotate))
            print(f"  第 1 趟: {'✓ ' if ok1 else '✗ '}{msg1}")
            if not ok1:
                print("✗ 第 1 趟失败，校准中止（先解决打印问题）")
                return 1
            if args.only_pass == 1:
                show_flip_instructions()
                print("\n（--only-pass 1 完成。翻好纸后，用**完全相同的参数**加 --only-pass 2 打第 2 趟）")
                if args.save:
                    print("（--save 会在第 2 趟跑完后才写档案）")
                return 0

        if do_pass2:
            if args.only_pass == 0:
                show_flip_instructions()
                input("\n▶ 翻好纸后回车，开始第 2 趟...")
            print("\n▶ 第 2 趟（补打另一面）...")
            ok2, msg2 = pp.print_plan_pass(plan, pdf_path, args.printer, 2,
                                           second_rotate_180=bool(args.second_rotate))
            print(f"  第 2 趟: {'✓ ' if ok2 else '✗ '}{msg2}")
            if not ok2:
                return 1

        show_checklist(pages, copies)
        if args.save:
            if not save_profile(args, pages, copies):
                return 1
        else:
            print("\n（参数未写入档案；人眼确认无误后加 --save 再跑，或用 --save-only 补记）")
        return 0
    finally:
        if not args.keep_pdf and os.path.isfile(pdf_path):
            try:
                os.remove(pdf_path)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
