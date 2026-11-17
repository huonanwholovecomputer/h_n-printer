# -*- coding: utf-8 -*-
"""验收 · 判据自己的可信度（2026-11-17 审计 🔴12-🔴17）

被验对象：other/tests/ 下所有脚本的**判据写法**，不是业务代码。

为什么要这条守卫：审计实测存量 62 条判据读 stdout/日志文案、104 条判据读产品源码文本再比对
中文文案（46 个脚本）。这类判据双面失效 ——
  · 假红：业务改一句提示文案就红，人开始习惯性「这条老红」；
  · 假绿：真正行为变了它抓不到。
约定见 other/tests/判据写法约定.md。

本脚本做三件事：
  A. 计数（棘轮）：两类坏判据的条数不得**增加**（基线 BASELINE = 2026-11-17 实测值）。
     识别方式不靠正则猜文案，而是轻量数据流：先找「从产品源码读出文本」与「从日志/stdout
     取文本」的变量，再看 check(...) 的条件里有没有拿中文串和这些变量比。
  B. 伪跳过：脚本里打印「跳过/SKIP」却 exit 0（run_all 会记 PASS）—— 必须打 [HN-SKIP] 前缀。
  C. 关键域守卫存在且真跑行为（订单来源 / 结算口径 / 设备裁决）。

用法：python other/tests/verify_guard_quality.py [--report]
      默认退出码即判定；--report 只打印计数、不判定（改基线时用）。
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TESTS = os.path.join(REPO, "other", "tests")
SELF = os.path.abspath(__file__)

# 2026-11-17 审计基线（只许降、不许升；每把一条换成行为判据就把基线减一）
BASELINE = {"log_text": 47, "source_text": 146}

CJK = re.compile(r"[\u4e00-\u9fff]")

PRODUCT_FILES = [
    ("local_print_tool", "gui.py"), ("local_print_tool", "pdf_printer.py"),
    ("local_print_tool", "manual_duplex.py"), ("local_print_tool", "duplex_stack.py"),
    ("local_print_tool", "cloud_client.py"), ("local_print_tool", "printer_config.py"),
    ("local_print_tool", "print_watch.py"), ("local_print_tool", "stats_server.py"),
    ("local_print_tool", "duplex_remedy.py"), ("local_print_tool", "converter.py"),
    ("local_print_tool", "finance", "settlement.html"),
    ("mobile_apps", "printer-backend", "app.py"),
]

_bad = []
_n = [0]


def check(title, cond, detail=""):
    _n[0] += 1
    if cond:
        print("  PASS  " + title)
    else:
        print("  FAIL  " + title + (("  [" + str(detail) + "]") if detail else ""))
        _bad.append(title)


def read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _skip_str(s, i):
    q = s[i]
    i += 1
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == q:
            return i + 1
        i += 1
    return i


def iter_check_args(text):
    out = []
    for m in re.finditer(r"\bcheck\s*\(", text):
        i = m.end()
        depth = 1
        start = i
        while i < len(text) and depth:
            c = text[i]
            if c in "\"'":
                i = _skip_str(text, i)
                continue
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append(text[start:i])
    return out


def drop_first_string(arg):
    m = re.search(r"\"(?:[^\"\\\\]|\\\\.)*\"|'(?:[^'\\\\]|\\\\.)*'", arg)
    if not m:
        return arg
    return arg[:m.start()] + arg[m.end():]


def cjk_literals(expr):
    lits = []
    for m in re.finditer(r"\"((?:[^\"\\\\]|\\\\.)*)\"|'((?:[^'\\\\]|\\\\.)*)'", expr):
        s = m.group(1) if m.group(1) is not None else m.group(2)
        if s and CJK.search(s):
            lits.append(s)
    return lits


def assigned_vars(text, rhs_pat):
    out = set()
    for m in re.finditer(r"^\s*([A-Za-z_]\w*)\s*=\s*(.+)$", text, re.M):
        if re.search(rhs_pat, m.group(2)):
            out.add(m.group(1))
    return out


def mentions(expr, names):
    for nm in names:
        if re.search(r"(?<![\w.])" + re.escape(nm) + r"(?![\w])", expr):
            return nm
    return None


def main():
    report_only = "--report" in sys.argv

    BLOB = "\n".join(read(os.path.join(REPO, *rel)) for rel in PRODUCT_FILES)
    check("A0 能读到产品源码（否则源码文案分类失效）", len(BLOB) > 500000, "blob=" + str(len(BLOB)))

    files = []
    for name in sorted(os.listdir(TESTS)):
        if not name.endswith((".py", ".mjs", ".js")):
            continue
        if name.startswith("_") or os.path.abspath(os.path.join(TESTS, name)) == SELF:
            continue
        files.append(name)

    log_hits, src_hits = [], []
    for name in files:
        text = read(os.path.join(TESTS, name))
        # 读产品源码文本的变量（src/gui/PAGE/... = open(...).read() / read_text() / .read()）
        src_vars = assigned_vars(text, r"open\s*\(|read_text\s*\(|\.read\s*\(\s*\)|Path\([^)]*\)\.read")
        # 读日志/子进程输出的变量
        log_vars = assigned_vars(text, r"\.text\s*\(\s*\)|toPlainText|\.stdout|\.stderr|LOGS|\.lines")
        log_vars |= {"LOGS", "OUT", "STDOUT"} & set(re.findall(r"^\s*([A-Z_]\w*)\s*=", text, re.M))
        for arg in iter_check_args(text):
            expr = drop_first_string(arg)
            if not cjk_literals(expr):
                continue
            if mentions(expr, log_vars):
                log_hits.append(name)
            elif mentions(expr, src_vars):
                # 只算「中文文案确实出现在产品源码里」的那些 —— 这才算拿文案当契约
                if any(l in BLOB for l in cjk_literals(expr)):
                    src_hits.append(name)

    print("")
    print("── A 计数（棘轮）──")
    print("  拿中文文案比对日志/输出   : %d 条（基线 %d）" % (len(log_hits), BASELINE["log_text"]))
    print("  拿中文文案比对产品源码   : %d 条（基线 %d）" % (len(src_hits), BASELINE["source_text"]))
    print("  涉及脚本：%d 个" % len(set(log_hits) | set(src_hits)))
    if report_only:
        print("  （--report：不判定）")
        print("")
        print("结果：报告模式，未判定")
        return 0

    check("A1 比对日志/输出文案的判据没有增加（只许降）",
          len(log_hits) <= BASELINE["log_text"],
          "%d > %d；新增出现在：%s" % (len(log_hits), BASELINE["log_text"],
                                       ", ".join(sorted(set(log_hits)))[:300]))
    check("A2 比对产品源码中文文案的判据没有增加（只许降）",
          len(src_hits) <= BASELINE["source_text"],
          "%d > %d" % (len(src_hits), BASELINE["source_text"]))
    if len(log_hits) < BASELINE["log_text"] or len(src_hits) < BASELINE["source_text"]:
        print("  NOTE  存量已下降：可把 BASELINE 下调为 %d / %d" % (len(log_hits), len(src_hits)))

    print("")
    print("── B 伪跳过：打印跳过却 exit 0（run_all 会记 PASS）──")
    fake = []
    for name in files:
        if not name.endswith(".py"):
            continue
        text = read(os.path.join(TESTS, name))
        for m in re.finditer(r"print\s*\(\s*[\"']([^\"']*(?:跳过|SKIP)[^\"']*)[\"']", text):
            if "[HN-SKIP]" in m.group(1):
                continue
            tail = text[m.start():m.start() + 900]
            if re.search(r"(sys\.exit\(0\)|return 0)", tail):
                fake.append(name + ": " + m.group(1)[:40])
    check("B1 没有「静默跳过当通过」的脚本", not fake, "; ".join(fake)[:400])

    print("")
    print("── C 关键域守卫存在且真跑行为 ──")
    domains = {
        "订单来源不得被本地改写": "verify_order_source_local_guard.py",
        "结算口径唯一 _count_pages_in_range": "verify_count_range_pages_single_source.py",
        "设备是订单最终裁决者（迟到 print_success 覆盖终态）": "verify_cancel_terminal_orders.py",
    }
    for label, fname in domains.items():
        p = os.path.join(TESTS, fname)
        ok = os.path.isfile(p)
        check("C 存在守卫：" + label + "（" + fname + "）", ok)
        if ok:
            t = read(p)
            check("C  " + fname + " 真跑行为（HTTP/DB/DOM/子进程）",
                  any(k in t for k in ("test_client", "subprocess.", "sqlite3", "execute(",
                                       "page.", "connect(")))

    print("")
    if _bad:
        print("结果：%d 条 FAIL / %d 条判据" % (len(_bad), _n[0]))
        return 1
    print("结果：全部通过（%d 条判据）" % _n[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
