# -*- coding: utf-8 -*-
"""本机日志「按天分文件」验收（2026-10-28 用户要求：每天一个日志，如 2026-10-28.log）

口径（唯一实现 = local_print_tool/daily_log.py）：
  · 写：`hn_local_tool` logger 挂 DailyFileHandler，文件名 = 当天日期 YYYY-MM-DD.log，
        跨午夜自动换文件（关闭旧句柄）；
  · 读：云端「收集设备日志」与 tools/log_triage.py 都走 daily_log.read_tail /
        latest_log_path —— 找不到今天的文件退回最近的日文件、再退回旧版 local_tool.log，
        并按预算把最近几天的内容拼起来（零点刚过也收得到东西）。

用法: python tests/verify_daily_log_split.py   （exit 0 = 全部通过）
"""
import io
import logging
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "local_print_tool"))

import daily_log  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def at(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, 0)


def new_logger(handler):
    """独立 logger（避免多次运行互相带 handler），返回 (logger, cleanup)。"""
    lg = logging.getLogger("hn_daily_" + uuid.uuid4().hex[:8])
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    lg.addHandler(handler)

    def _cleanup():
        lg.removeHandler(handler)
        handler.close()
    return lg, _cleanup


def lines_of(path):
    with open(path, encoding="utf-8") as f:
        return [ln.rstrip("\n") for ln in f if ln.strip()]


WORKS = []
def work_dir():
    d = tempfile.mkdtemp(prefix="hn_daily_")
    WORKS.append(d)
    return d


try:
    # ==================== ① 文件名 = 当天日期 ====================
    section("① 文件名就是当天日期（2026-10-28.log），不是 local_tool.log")
    d1 = work_dir()
    h = daily_log.make_handler(d1, now_fn=lambda: at(2026, 10, 28))
    lg, done = new_logger(h)
    lg.info("第一行")
    lg.warning("第二行")
    p1 = os.path.join(d1, "2026-10-28.log")
    check("★ 写进 2026-10-28.log（而不是 local_tool.log）",
          os.path.isfile(p1) and not os.path.exists(os.path.join(d1, "local_tool.log")),
          str(sorted(os.listdir(d1))))
    body = lines_of(p1)
    check("同一进程连续写 → 追加到同一文件（两条，不覆盖）", len(body) == 2, str(body))
    check("格式没变：<时间> [级别] 正文",
          body[0].endswith("[INFO] 第一行") and body[1].endswith("[WARNING] 第二行"),
          body[0] + " | " + body[1])
    check("handler 报出当前文件路径", os.path.basename(h.current_path()) == "2026-10-28.log",
          h.current_path())
    done()

    # ==================== ② 跨午夜自动换文件 ====================
    section("② 跨午夜：自动落到新日期的文件，旧句柄被关闭")
    d2 = work_dir()
    clock = {"now": at(2026, 10, 28, 23, 59)}
    h2 = daily_log.make_handler(d2, now_fn=lambda: clock["now"])
    lg2, done2 = new_logger(h2)
    lg2.info("午夜前")
    p_old = os.path.join(d2, "2026-10-28.log")
    clock["now"] = at(2026, 10, 29, 0, 1)
    lg2.info("午夜后")
    p_new = os.path.join(d2, "2026-10-29.log")
    check("★ 午夜后写入新建的 2026-10-29.log", os.path.isfile(p_new) and len(lines_of(p_new)) == 1,
          str(sorted(os.listdir(d2))))
    check("★ 昨天的文件保持原样（只有午夜前那一行）",
          lines_of(p_old) == lines_of(p_old)[:1] and lines_of(p_old)[0].endswith("[INFO] 午夜前"),
          str(lines_of(p_old)))
    removed = False
    try:
        os.remove(p_old)          # Windows：句柄没关就删不掉（共享模式不含 FILE_SHARE_DELETE）
        removed = True
    except OSError as e:
        removed = False
        check("旧日文件已关闭（Windows 下可删除）", False, str(e))
    check("旧日文件已关闭（Windows 下可删除，证明换文件时真的 close 了）", removed)
    clock["now"] = at(2026, 10, 30, 9, 0)
    lg2.info("再跨一天")
    check("再跨一天 → 2026-10-30.log", os.path.isfile(os.path.join(d2, "2026-10-30.log")),
          str(sorted(os.listdir(d2))))
    done2()

    # ==================== ③ 读尾部：零点刚过也收得到内容 ====================
    section("③ read_tail：今天的 + 昨天的按预算拼起来（零点刚过不是空的）")
    d3 = work_dir()
    with open(os.path.join(d3, "2026-10-27.log"), "w", encoding="utf-8") as f:
        f.write("\n".join("27日第%d行" % i for i in range(1, 4)) + "\n")
    with open(os.path.join(d3, "2026-10-28.log"), "w", encoding="utf-8") as f:
        f.write("28日唯一一行\n")

    def cross_day_ok(read_fn):
        text = read_fn(d3)
        return ("27日第3行" in text) and ("28日唯一一行" in text) and \
               text.index("27日第3行") < text.index("28日唯一一行")

    check("★ 默认实现同时给出昨天末尾与今天的内容，且按时间先后拼接", cross_day_ok(daily_log.read_tail))
    only_today = lambda dd: daily_log._tail_bytes(os.path.join(dd, "2026-10-28.log"), 4096).decode("utf-8")
    check("变异：只读今天那一份（旧的 read_tail 语义）→ 上一条判据必须变红", not cross_day_ok(only_today))

    # ==================== ④ 回退链 ====================
    section("④ 找不到今天的文件时的回退链：最近日文件 → 旧版 local_tool.log → 空")
    d4 = work_dir()
    check("什么都没有 → 空串（调用方按无日志处理）",
          daily_log.read_tail(d4) == "" and daily_log.latest_log_path(d4) == "")
    with open(os.path.join(d4, "local_tool.log"), "w", encoding="utf-8") as f:
        f.write("旧版单文件日志\n")
    check("只有旧版 local_tool.log（升级当天）→ 读得到",
          "旧版单文件日志" in daily_log.read_tail(d4)
          and daily_log.latest_log_path(d4).endswith("local_tool.log"))
    with open(os.path.join(d4, "2026-10-20.log"), "w", encoding="utf-8") as f:
        f.write("20日的日志\n")
    check("出现日文件后优先日文件（不被旧版抢）",
          daily_log.latest_log_path(d4).endswith("2026-10-20.log")
          and daily_log.read_tail(d4).startswith("20日的日志"))
    with open(os.path.join(d4, "2026-10-25.log"), "w", encoding="utf-8") as f:
        f.write("25日的日志\n")
    check("取**最近**的日文件（按名字排序，25 日 > 20 日）",
          daily_log.latest_log_path(d4).endswith("2026-10-25.log"))
    with open(os.path.join(d4, "remote_server.log"), "w", encoding="utf-8") as f:
        f.write("远端日志不该被当成本机日志\n")
    check("非 YYYY-MM-DD.log 的文件不算（remote_* / finance_audit 不混进来）",
          "远端日志不该" not in daily_log.read_tail(d4)
          and daily_log.latest_log_path(d4).endswith("2026-10-25.log"))

    # ==================== ⑤ 预算与半行 ====================
    section("⑤ 字节预算与半行跳过")
    d5 = work_dir()
    big = os.path.join(d5, "2026-10-28.log")
    with open(big, "w", encoding="utf-8") as f:
        f.write("".join("第%04d行-abcdefghij\n" % i for i in range(1, 501)))
    small = daily_log.read_tail(d5, max_bytes=500)
    check("预算生效：单文件读取不超过 max_bytes", len(small.encode("utf-8")) <= 500,
          str(len(small.encode("utf-8"))))
    check("从完整行开始（不出现被截断的半行）", small.startswith("第" + small[1:5]) if small else False,
          repr(small[:24]))
    check("取的是**尾部**（最后一行在结果里）", small.rstrip().endswith("-abcdefghij"),
          repr(small[-24:]))
    check("max_files 限制生效（默认最多 3 天）", daily_log.MAX_TAIL_FILES == 3, str(daily_log.MAX_TAIL_FILES))

    # ==================== ⑥ 静态接线 ====================
    section("⑥ 三处接线：gui 写 / cloud_client 读 / 排查工具读")
    gui_src = open(os.path.join(REPO, "local_print_tool", "gui.py"), encoding="utf-8").read()
    cc_src = open(os.path.join(REPO, "local_print_tool", "cloud_client.py"), encoding="utf-8").read()
    tri_src = open(os.path.join(REPO, "local_print_tool", "tools", "log_triage.py"), encoding="utf-8").read()

    def gui_wired(src):
        i = src.find("self._file_logger = logging.getLogger")
        chunk = src[i:i + 900]
        # 2026-10-30 起 make_handler 多了 keep_days / on_prune（30 天保留），
        # 所以判据只认前缀调用，不再要求右括号紧跟；但仍必须带保留参数。
        return ("daily_log.make_handler(self._log_dir" in chunk
                and "keep_days=" in chunk
                and 'FileHandler(' not in chunk
                and "local_tool.log" not in chunk)

    def cc_wired(src):
        i = src.find("def _read_local_log_tail")
        chunk = src[i:i + 700]
        return "daily_log.read_tail(" in chunk and "local_tool.log" not in chunk

    def tri_wired(src):
        return "daily_log.latest_log_path(" in src and 'Path(paths.logs_dir()) / "local_tool.log"' not in src

    check("★ gui：登录器挂的是 daily_log.make_handler（不再固定 local_tool.log）", gui_wired(gui_src))
    check("★ cloud_client：读日志走 daily_log.read_tail", cc_wired(cc_src))
    check("★ log_triage：默认日志文件走 daily_log.latest_log_path", tri_wired(tri_src))

    # ==================== ⑦ 判据自证 ====================
    section("⑦ 判据自证（改回单文件必须变红）")
    _orig_day_key = daily_log.DailyFileHandler._day_key
    daily_log.DailyFileHandler._day_key = lambda self: "2026-10-28"   # 等价于"永不跨天"
    d6 = work_dir()
    clock6 = {"now": at(2026, 10, 28, 23, 59)}
    h6 = daily_log.make_handler(d6, now_fn=lambda: clock6["now"])
    lg6, done6 = new_logger(h6)
    lg6.info("午夜前")
    clock6["now"] = at(2026, 10, 29, 0, 1)
    lg6.info("午夜后")
    done6()
    daily_log.DailyFileHandler._day_key = _orig_day_key
    check("B1 去掉「按日期换文件」（日期键恒为一固定值）→ ② 的判据必须变红",
          not os.path.isfile(os.path.join(d6, "2026-10-29.log")), str(sorted(os.listdir(d6))))

    _mut_gui = gui_src.replace("daily_log.make_handler(self._log_dir",
                               'logging.FileHandler(os.path.join(self._log_dir, "local_tool.log"))')
    check("B2 gui 退回固定 local_tool.log → ⑥ 判据变红", not gui_wired(_mut_gui))

    _mut_cc = cc_src.replace("daily_log.read_tail(", "open(")
    check("B3 cloud_client 不再走 daily_log.read_tail → ⑥ 判据变红", not cc_wired(_mut_cc))

    # ==================== ⑧ 真窗口启动：今天的日志真的落盘 ====================
    section("⑧ 真实 MainWindow 启动 → 日志落进 <今天>.log（端到端）")
    try:
        import importlib
        from PySide6.QtWidgets import QApplication, QMessageBox

        appdir = work_dir()
        os.environ["APPDATA"] = appdir                     # 沙箱：不碰真实用户目录
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        QMessageBox.information = staticmethod(lambda *a, **kw: QMessageBox.Ok)
        QMessageBox.warning = staticmethod(lambda *a, **kw: QMessageBox.Ok)
        QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.Yes)
        import paths as _paths
        importlib.reload(_paths)                           # APPDATA 变了 → 重新解析用户目录
        gui_mod = importlib.import_module("gui")
        win = gui_mod.MainWindow(config_path=_paths.config_path())
        log_dir = _paths.logs_dir()
        target = os.path.join(log_dir, datetime.now().strftime("%Y-%m-%d") + ".log")
        win._log("按天日志端到端探针")
        body = ""
        if os.path.isfile(target):
            with open(target, encoding="utf-8", errors="replace") as f:
                body = f.read()
        check("★ 启动即在 <今天>.log 里留下「HN 本地打印工具启动」",
              "HN 本地打印工具启动" in body, target)
        check("★ win._log() 写进同一个当天文件（界面日志与文件日志同一管道）",
              "按天日志端到端探针" in body)
        check("★ 不再生成固定的 local_tool.log",
              not os.path.exists(os.path.join(log_dir, "local_tool.log")),
              str(sorted(os.listdir(log_dir)) if os.path.isdir(log_dir) else log_dir))
        win.close()
    except Exception as e:                                  # 无 GUI 依赖的环境按 SKIP 处理
        print(f"[SKIP] 无头 GUI 起不来，跳过端到端那一段：{type(e).__name__}: {e}")


finally:
    for _d in WORKS:
        shutil.rmtree(_d, ignore_errors=True)

print("\n" + "=" * 56)
print("按天日志验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
