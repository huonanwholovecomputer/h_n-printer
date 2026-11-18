# -*- coding: utf-8 -*-
"""钉住「字体缺失详情」与 PostScript 字体名匹配（2026-11-15 现场需求）。

现场来由：用户装了 TimesNewRomanPSMT.otf / ArialMT.ttf 等字体，工具照旧在行上、
tooltip 里、控制台里报「本机没装这些字体：TimesNewRomanPS-BoldMT、Arial-BoldMT、ArialMT」。
他要两件事：① 别误报（装了就得认）；② 提示里的字体名要能整段复制去搜索下载，
下载好的 .ttf 也要能一键装进系统。本脚本钉住的判据（每一条都能红）：

  ① PDF 的 PostScript 名（TimesNewRomanPSMT / ArialMT / TimesNewRomanPS-BoldMT /
     -ItalicMT / Helvetica-Bold / CourierNewPSMT）在只装了对应系统字族时**不算缺**；
  ② 真缺的字体（方正书宋_GBK / STSong-Light）照旧报缺 —— 别为了不误报放过真问题；
  ③ 字体文件安装：plan_install 认名字（times.ttf → Times New Roman）、子集阈值、
     非字体文件/不存在的路径要拒；install(dry_run=True) 绝不碰系统；
  ④ font_help_text：两种「缺」分开列、名字可复制、步骤里点名「复制字体名」
     「安装字体文件…」「已装好，重新检查」，并说明西文不替换、子集字体救不了别的文件；
  ⑤ FontHelpDialog 真建得起来（offscreen）：正文含名字、复制按钮真的写进剪贴板、
     没选文件时安装动作不炸；
  ⑥ 源码接线：_queue_font_checks 会 refresh_installed（装完字体加文件不再沿用旧快照）、
     右键菜单接了「🖋 字体缺失详情…」→ _open_font_help → _font_recheck_jobs。

不出纸、不改本机字体：install 一律 dry_run，剪贴板用完还原。
"""
from __future__ import annotations

import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

_SANDBOX = tempfile.mkdtemp(prefix="hn_font_help_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import font_check  # noqa: E402
import font_install  # noqa: E402
import printer_config  # noqa: E402

FAILED: list = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print("  ✅ " + name)
    else:
        FAILED.append(name)
        print("  ❌ " + name + (("  —— " + str(detail)[:400]) if detail else ""))


def pin(families) -> None:
    """把已装字体钉成给定集合（不碰真实 GDI 枚举，机器不同也照样判定）。"""
    font_check._installed = set(families)
    font_check._installed_norm_cache = None


def main() -> int:
    print("① PostScript 字体名 → 系统字族名（2026-11-15 现场：装了也报没装）")
    pin(["Times New Roman", "Arial", "Courier New", "宋体"])
    for name in ("TimesNewRomanPSMT", "TimesNewRomanPS-BoldMT",
                 "TimesNewRomanPS-ItalicMT", "TimesNewRomanPS-BoldItalicMT",
                 "ArialMT", "Arial-BoldMT", "Arial-ItalicMT",
                 "Helvetica-Bold", "CourierNewPSMT", "CourierNewPS-BoldMT"):
        check("装了同族字族时 %s 不算缺" % name,
              not font_check._missing_key(font_check._norm(name)),
              str(font_check._ps_variants(font_check._norm(name))))
    _v = font_check._ps_variants("timesnewromanps boldmt")
    check("根因那条：_ps_variants 把厂商后缀剥掉了（候选里出现 timesnewroman bold）",
          "timesnewroman bold" in _v, str(_v))

    print("② 真缺的字体照旧报缺（不能为了不误报放过真问题）")
    pin(["Times New Roman", "Arial", "宋体"])
    for name in ("方正书宋_GBK", "STSong-Light", "方正书宋_GBK Bold", "SimSun-ExtB"):
        check("真缺的 %s 仍然算缺" % name,
              font_check._missing_key(font_check._norm(name)),
              str(font_check._ps_variants(font_check._norm(name))))
    pin(["Times New Roman", "Arial"])
    check("只装了 Times/Arial 时，TimesNewRomanPS-BoldMT 不算缺",
          not font_check._missing_key(font_check._norm("TimesNewRomanPS-BoldMT")))
    check("同一台机器上 STSong-Light 仍算缺",
          font_check._missing_key(font_check._norm("STSong-Light")))

    print("③ 字体文件安装（dry_run，只算不装）")
    win_fonts = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "Fonts")
    times = os.path.join(win_fonts, "times.ttf")
    if os.path.isfile(times):
        plan = font_install.plan_install([times])
        check("times.ttf 的给的名字是 Times New Roman",
              plan and plan[0].get("ok") and plan[0].get("name") == "Times New Roman",
              str(plan))
        check("完整字体不算子集",
              plan and not plan[0].get("subset"), str(plan))
        check("目标目录落在当前用户字体目录",
              plan and "Fonts" in (plan[0].get("dest") or ""), str(plan))
    else:
        check("本机有 times.ttf（跳过名字解析用例）", False, times)
    _tmp_small = os.path.join(_SANDBOX, "fake-small.ttf")
    with open(_tmp_small, "wb") as f:
        f.write(b"\x00" * 2048)
    plan_small = font_install.plan_install([_tmp_small])
    check("几十 KB 的字体被子集阈值标出来",
          plan_small and plan_small[0].get("ok") and plan_small[0].get("subset"),
          str(plan_small))
    check("读不出 name 表时退回文件名",
          plan_small and plan_small[0].get("name") == "fake-small", str(plan_small))
    bad = font_install.plan_install([os.path.join(_SANDBOX, "nope.ttf"),
                                     os.path.join(_SANDBOX, "a.png")])
    check("不存在的路径被拒", bad and bad[0].get("ok") is False, str(bad))
    check("非字体扩展名被拒", len(bad) > 1 and bad[1].get("ok") is False, str(bad))
    rep = font_install.install([_tmp_small], dry_run=True)
    check("dry_run 报 installed 且标记 dry_run",
          rep.get("dry_run") is True and rep.get("installed"), str(rep))
    check("dry_run 没有把文件拷进真实字体目录",
          not os.path.exists(os.path.join(font_install._fonts_dir(),
                                          "fake-small.ttf")),
          font_install._fonts_dir())

    print("④ 详情正文（纯字符串）")
    import gui  # noqa: E402  （Qt 模块在 offscreen 下 import 是安全的）
    j1 = printer_config.PrintJob(file_path=r"C:\tmp\十八.pdf", copies=1,
                                 missing_fonts=["TimesNewRomanPS-BoldMT", "ArialMT"],
                                 nonembedded_fonts=["TimesNewRomanPSMT", "ArialMT",
                                                    "TimesNewRomanPS-ItalicMT"])
    j2 = printer_config.PrintJob(file_path=r"C:\tmp\没提示.pdf", copies=1)
    txt = gui.font_help_text([j1, j2])
    check("正文点名文件名", "十八.pdf" in txt, txt[:200])
    check("本机没装 / PDF 没嵌入 两段分开",
          "本机没装（装上就能救）" in txt and "PDF 生成时没嵌进字形" in txt, txt[:400])
    # 2026-11-18 现场：装好 SimSun 后清单里照旧列着它，用户以为「装了还是没装」。
    # 正文必须当场把两类说清，并且逐个名字标出本机到底装没装。
    check("正文抬头把两类分开讲（① 本机没装 ② 文件没带字形）",
          "这两类不要混" in txt and "装了就修好" in txt and "装不装都改不了" in txt, txt[:400])
    check("没嵌入的那类逐个标出本机装没装",
          "本机已装 ✓" in txt or "本机也没装" in txt, txt[:600])
    check("抬头统计的是「有提示的文件数」，不是传进来的文件数",
          "共 1 个文件有字体提示" in txt, txt[:40])
    check("两种缺的字体名都列了出来",
          all(n in txt for n in ("TimesNewRomanPS-BoldMT", "ArialMT",
                                 "TimesNewRomanPS-ItalicMT")), txt[:400])
    check("步骤里有「复制字体名 / 安装字体文件 / 已装好，重新检查」",
          "复制字体名" in txt and "安装字体文件" in txt and "已装好，重新检查" in txt, "")
    check("说明西文不替换（别指望装 Arial 改变打印）",
          "西文" in txt and "等价字形" in txt, txt[-400:])
    # 2026-11-18 现场：Times/Arial 全装齐了，详情窗却把「打印时用系统字形渲染」贴在西文名后面，
    # 用户以为「装了却没替换」。西文本来就不走系统字形（system_fonts 只装 CJK 回调）。
    _latin_line = [ln for ln in txt.split("\n") if "TimesNewRomanPSMT" in ln][0]
    check("西文已装时提示的是内置等价字形，不是系统字形",
          gui.FONT_NOTE_BUILTIN_GLYPH in _latin_line
          and gui.FONT_NOTE_SYSTEM_GLYPH not in _latin_line, _latin_line)
    _orig_inst4 = gui.font_check.is_installed
    gui.font_check.is_installed = lambda n: True
    try:
        _txt_all = gui.font_help_text([printer_config.PrintJob(
            file_path=r"C:\tmp\全装齐.pdf", copies=1,
            nonembedded_fonts=["ArialMT", "SimSun"])])
    finally:
        gui.font_check.is_installed = _orig_inst4
    check("本机全装齐时不再给下载安装指引（没有要补的字体）",
          gui.FONT_NOTE_BUILTIN_GLYPH in _txt_all and "ttf" not in _txt_all,
          _txt_all[:300])
    check("中文族名仍然按系统字形说（两类不能一刀切）",
          gui.FONT_NOTE_SYSTEM_GLYPH in _txt_all, _txt_all[:300])
    check("提醒子集字体 / 改名字体救不了别的文件",
          "子集" in txt and "冒充" in txt, txt[-400:])
    check("没有提示的文件不进正文", "没提示.pdf" not in txt, "")
    check("空列表也能出正文（不崩）",
          "没有字体提示" in gui.font_help_text([]), gui.font_help_text([])[:120])

    print("⑤ 详情窗（offscreen 真建控件）")
    from PySide6.QtWidgets import QApplication  # noqa: E402
    _app = QApplication.instance() or QApplication([])
    dlg = gui.FontHelpDialog(None, [j1])
    check("窗口正文就是 font_help_text", "TimesNewRomanPS-BoldMT" in dlg.view.toPlainText())
    check("三个动作按钮都在",
          all(hasattr(dlg, k) for k in ("btn_copy", "btn_install", "btn_recheck")))
    check("默认不在「重新检查」状态",
          dlg.recheck_requested is False and dlg.installed_any is False)
    _app.clipboard().setText("")
    dlg.btn_copy.click()
    _clip = _app.clipboard().text()
    check("复制字体名把全部名字按行写进剪贴板",
          set(_clip.split("\n")) >= {"TimesNewRomanPS-BoldMT", "ArialMT",
                                      "TimesNewRomanPSMT", "TimesNewRomanPS-ItalicMT"},
          _clip)
    check("复制后状态行报数量", "已复制" in dlg.status.text(), dlg.status.text())
    # 没选文件时点安装：必须原地返回，不能炸、不能改状态
    _orig = gui.QFileDialog.getOpenFileNames
    gui.QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: ([], ""))
    try:
        _before = dlg.status.text()
        dlg.btn_install.click()
        check("没选文件时安装动作什么都不做",
              dlg.status.text() == _before and dlg.installed_any is False,
              dlg.status.text())
    finally:
        gui.QFileDialog.getOpenFileNames = _orig
    dlg.btn_recheck.click()
    check("点「已装好，重新检查」置 recheck_requested 并关窗",
          dlg.recheck_requested is True, "recheck_requested=False")

    print("⑤b 装完字体当场重判（2026-11-18 现场：装好 SimSun 仍显示没装）")
    import tempfile  # noqa: E402
    _tmpd = tempfile.mkdtemp(prefix="hn_font_help_")
    _pdf = os.path.join(_tmpd, "sample.pdf")
    with open(_pdf, "wb") as _f:
        _f.write(b"%PDF-1.4\n%%EOF\n")
    _job = printer_config.PrintJob(file_path=_pdf, copies=1, nonembedded_fonts=["SimSun"])
    _dlg2 = gui.FontHelpDialog(None, [_job])
    _before_txt2 = _dlg2.view.toPlainText()
    _orig_check = gui.font_check.check_file
    _orig_inst = gui.font_check.is_installed
    # 假装 SimSun 已经装好、文件里仍然只有字体名没有字形
    gui.font_check.is_installed = lambda n: True
    gui.font_check.check_file = lambda p: {"ok": True, "fonts": ["SimSun"],
                                           "missing": [], "nonembedded": ["SimSun"]}
    try:
        _left = _dlg2._refresh_reports()
        _txt2 = _dlg2.view.toPlainText()
    finally:
        gui.font_check.check_file = _orig_check
        gui.font_check.is_installed = _orig_inst
    _has_mark = ("SimSun" in _txt2) and ("\u2713" in _txt2)
    check("重判后正文换成新结果（不再显示打开窗口时那份快照）",
          _has_mark and (_txt2 != _before_txt2), _txt2[:200])
    check("重判把「本机没装」清空（装了就是装了）",
          list(_job.missing_fonts) == [], str(_job.missing_fonts))
    check("重判后仍有「文件没带字形」提示 ⇒ 返回该文件给状态行",
          list(_left) == [_job], str(len(_left)))

    print("⑥ 源码接线（漏一处功能等于没做）")
    src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8", errors="replace").read()
    check("_queue_font_checks 会 refresh_installed（装完字体加文件不能沿用旧快照）",
          "font_check.refresh_installed(QFontDatabase.families())" in src)
    check("右键菜单有「🖋 字体缺失详情…」入口",
          "字体缺失详情" in src and "_open_font_help" in src)
    check("重新检查走 _font_recheck_jobs",
          "def _font_recheck_jobs" in src and "self._font_recheck_jobs(" in src)
    check("装完字体立刻在本窗口重判（不留装之前的快照）",
          "def _refresh_reports" in src and "self._refresh_reports()" in src)
    check("tooltip 指出了字体详情入口",
          "🖋 字体缺失详情…" in src)

    print()
    if FAILED:
        print("❌ 失败 %d 条 / 通过 %d 条" % (len(FAILED), PASSED))
        for n in FAILED:
            print("   - " + n)
        return 1
    print("字体缺失详情验收: 全部通过 ✅（通过 %d 条）" % PASSED)
    return 0


if __name__ == "__main__":
    sys.exit(main())
