"""手动双面（无自动双面器）适配工具：能力判定 / 两趟页序验算 / 驱动弹窗可接管性嗅探。

为什么需要：本项目的双面设置全部走 DEVMODE（`pdf_printer._get_printer_devmode` 写
`dmDuplex` + `DM_DUPLEX`）或 SumatraPDF 的 `duplexlong`，两者都**假设驱动会真的双面出纸**。
HP LaserJet 1018/1020/1022 这类机器没有双面器，驱动只在「双面打印说明」弹窗里指挥用户
手工翻纸；这个假设不成立时失败是静默的：不支持的驱动会在 `DocumentProperties` 合并过程中
把 `dmDuplex` 改回 1(SIMPLEX)，程序照样报「打印成功」，客户付了双面钱、店里出双倍纸。

三种用法（都在**接了那台打印机的机器**上跑）：

    python tools/check_printer_duplex.py                      # ① 能力判定（全部打印机）
    python tools/check_printer_duplex.py "HP LaserJet 1020"   #    只看这一台
    python tools/check_printer_duplex.py --batch 4d,1s,5d     # ② 两趟页序验算（不碰打印机）
    python tools/check_printer_duplex.py --plan 5 --copies 2
    python tools/check_printer_duplex.py --sniff              # ③ 嗅探驱动的手动翻纸弹窗
    python tools/check_printer_duplex.py --sniff --press      #    并尝试点它的「继续」

`--sniff` 回答的是工程问题「这个弹窗能不能被程序接管」：窗口属于哪个进程、类名、控件
ID/文本，以及 `BM_CLICK` / `WM_COMMAND` / 聚焦+回车 三种点法哪种真的把它关掉了。

Exit 0 = 命令跑完（这是诊断工具，不是通过/失败型测试；判定结果打在 stdout）。
"""
from __future__ import annotations

import sys
from pathlib import Path

DC_DUPLEX = 7  # wingdi.h: DC_DUPLEX
DMDUP_SIMPLEX = 1
DMDUP_VERTICAL = 2  # 长边翻转
DMDUP_HORIZONTAL = 3  # 短边翻转

# 明确只有手动双面的机型（HP 官方手册只有 "Print on Both Sides (Manually)" 一节）
MANUAL_ONLY_HINTS = ("1018", "1020", "1022", "1022n", "1022nw")

# 驱动「双面打印说明」弹窗的识别关键词（中/英）
DUPLEX_DIALOG_WORDS = ("双面打印说明", "双面", "both sides", "duplex", "instructions", "奇数页")
DUPLEX_BUTTON_WORDS = ("继续", "continue", "确定", "ok")

# 页序引擎（唯一真相，纯逻辑 + 独立验收 tests/verify_manual_duplex.py）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import manual_duplex as md  # noqa: E402
import pdf_printer as pp  # noqa: E402  （模块级只 import 标准库；fitz/PIL 在函数内惰性导入）


# ============================================================
# 一、能力探测
# ============================================================

def probe_printer(win32print, win32con, name: str) -> dict:
    """探测单台打印机：端口、DC_DUPLEX、DEVMODE 双面字段、驱动接受度。"""
    info = {"name": name, "port": "", "driver": "", "virtual": False,
            "dc_duplex": None, "dm_duplex": None, "dm_fields": None,
            "accepted": None, "error": ""}
    handle = None
    try:
        handle = win32print.OpenPrinter(name)
        info["port"] = win32print.GetPrinter(handle, 2).get("pPortName", "")
        info["driver"] = win32print.GetPrinter(handle, 2).get("pDriverName", "") or ""
        info["virtual"] = pp.is_virtual_printer(name, info["driver"], info["port"])

        cap = win32print.DeviceCapabilities(name, info["port"], DC_DUPLEX)
        # pywin32 多数版本对 DC_DUPLEX 返回 int；个别能力位返回元组，统一取首元素
        if isinstance(cap, (tuple, list)):
            cap = cap[0] if cap else None
        info["dc_duplex"] = cap

        dm = win32print.GetPrinter(handle, 2)["pDevMode"]
        info["dm_duplex"] = getattr(dm, "Duplex", None)
        info["dm_fields"] = getattr(dm, "Fields", None)

        # 请求长边双面，交给驱动校验（合并到缓冲区，**不改打印机默认设置**，已实测核对）
        dm.Duplex = DMDUP_VERTICAL
        dm.Fields |= win32con.DM_DUPLEX
        win32print.DocumentProperties(
            0, handle, name, dm, dm,
            win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER,
        )
        info["accepted"] = (getattr(dm, "Duplex", None) == DMDUP_VERTICAL)
    except Exception as e:  # 打印机名不存在、驱动异常等
        info["error"] = str(e)
    finally:
        if handle is not None:
            try:
                win32print.ClosePrinter(handle)
            except Exception:
                pass
    return info


def verdict(info: dict) -> str:
    """把探针结果翻译成一句可执行的结论。"""
    if info["error"]:
        return f"无法探测（{info['error']}）"
    name = info["name"]
    hinted = any(h in name for h in MANUAL_ONLY_HINTS)
    dc, acc = info["dc_duplex"], info["accepted"]

    # 虚拟打印机优先判：它不出纸，双面**没有物理意义**（不是"驱动做不到"），
    # 程序对它放行双面请求并按单面输出（见 pdf_printer.is_virtual_printer）。
    if info.get("virtual"):
        return (f"虚拟打印机（{'/'.join(x for x in (info.get('driver'), info.get('port')) if x)}）"
                f"—— 不出纸，双面为空操作：程序按请求放行、按单面输出，不会拦双面打印")

    if dc == 0:
        tail = "（该机型只有『Print on Both Sides (Manually)』，须走本程序的手动双面两趟）" \
            if hinted else "须走手动双面两趟或改单面计费"
        return f"无自动双面 → DC_DUPLEX=0{tail}"
    if dc == 1 and acc:
        tail = "但 1018/1020/1022 系可能把『手动双面』也算进能力位 —— 必须实机自检确认" \
            if hinted else "驱动保持 Duplex=2，可走自动双面"
        return f"声明支持自动双面 → DC_DUPLEX=1 且驱动接受{tail}"
    if dc == 1 and not acc:
        return "DC_DUPLEX=1 但驱动把 Duplex 改回 1 —— 实际不可用，按无自动双面处理"
    return f"DC_DUPLEX={dc}，驱动接受={acc}（结果不明，按实机自检为准）"


def run_probe(argv: list[str]) -> int:
    try:
        import win32con
        import win32print
    except ImportError:
        print("✗ 缺少 pywin32：pip install pywin32")
        return 1

    want = argv[0] if argv else ""
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    allp = win32print.EnumPrinters(flags, None, 2)
    if want:
        names = [i["pPrinterName"] for i in allp if want.lower() in i["pPrinterName"].lower()]
        if not names:
            print(f"✗ 找不到名称包含「{want}」的打印机，本机有：")
            for i in allp:
                print(f"    {i['pPrinterName']}")
            return 1
    else:
        names = [i["pPrinterName"] for i in allp]

    print(f"共 {len(names)} 台打印机\n" + "=" * 78)
    for n in names:
        info = probe_printer(win32print, win32con, n)
        print(f"{n}\n  驱动            : {info['driver'] or '(未知)'}")
        print(f"  端口            : {info['port']}")
        print(f"  DC_DUPLEX       : {info['dc_duplex']}   (1=声明支持双面, 0=不支持)")
        print(f"  DEVMODE.Duplex  : {info['dm_duplex']}  (1=SIMPLEX 2=长边 3=短边)")
        print(f"  请求双面后驱动  : {'接受' if info['accepted'] else '拒绝/改回 SIMPLEX'}")
        print(f"  结论            : {verdict(info)}")
        print("-" * 78)
    return 0


# ============================================================
# 二、两趟页序（引擎在 manual_duplex.py，这里只做 CLI 渲染）
# ============================================================
#
# 物理依据（HP LaserJet 1020 用户手册 + 驱动「双面打印说明」弹窗原文）：
#
#   · 装纸：「顶边朝里、要打印的一面朝上」；出纸盒是 face down（手册规格表
#     "Top (face down) output bin"）。
#   · 弹窗指令：第 1 趟「不改变方向，从出纸槽中取出纸叠」→ 第 2 趟「保持相同方向，
#     将纸叠插入纸盘 2，使已打印面朝下」——**整叠不翻面、不重新排序**。
#   · 进纸盒从**最上面**一张取纸 ⇒ 第 2 趟的进纸顺序 = 第 1 趟印刷顺序的**逆序**
#     ⇒ 第 2 趟必须整体逆序打印（Word/Adobe 的手动双面同样如此）。
#   · 弹窗「不要丢弃空白页面」⇒ 驱动自己在第 1 趟（偶数页那趟）插空白占位页占住纸张槽位。
#     **每张纸在两趟里各占一个槽位，没内容就补空白页。**
#
# 页序算法**不在本文件实现**：唯一真相是 local_print_tool/manual_duplex.py。改页序只改那里，
# 本文件与 tests/verify_manual_duplex.py 都会跟着变。

def _parse_items(spec: str) -> list[tuple[str, int, bool]]:
    """解析 "4d,1s,5d" / "4,1,5"（缺省 d=双面, s=单面）为 [(标签, 页数, 双面)]。"""
    items = []
    for i, part in enumerate(spec.split(",")):
        part = part.strip()
        if not part:
            continue
        duplex = not part.lower().endswith("s")
        num = part.rstrip("dDsS")
        if not num.isdigit():
            raise ValueError(f"看不懂的规格片段: {part}")
        items.append((f"F{i + 1}", int(num), duplex))
    return items


def manual_duplex_passes(pages: list[int], copies: int = 1,
                         reverse_backs: bool = True) -> tuple[list[int | None], list[int | None]]:
    """（兼容旧签名）单文件两趟页序 —— 内部走 manual_duplex 引擎。

    返回 (第 1 趟, 第 2 趟) 的 1-based 页码序列，None = 空白占位页。
    默认约定 = HP 的「偶数面先打」（模块文档里有物理依据与两种约定的差别）。
    """
    plan = md.plan_single(list(pages), copies=copies, reverse_second=reverse_backs)
    return plan.pass_pages(1), plan.pass_pages(2)


def _fmt_slots(slots, n_files: int) -> str:
    """把 PassSlot 列表渲染成一行（单文件省略标签，与引擎 describe() 同口径）。"""
    out = []
    for s in slots:
        if s.page is None:
            out.append("〔空白〕")
        elif n_files > 1:
            out.append(f"{s.label}:{s.page}")
        else:
            out.append(str(s.page))
    return " ".join(out)


def run_plan(argv: list[str], batch_spec: str, single: int) -> int:
    copies, first, reverse_second = 1, md.FIRST_ODD, True
    i = 0
    while i < len(argv):
        if argv[i] == "--copies" and i + 1 < len(argv):
            copies = max(1, int(argv[i + 1]))
            i += 2
            continue
        if argv[i] == "--first" and i + 1 < len(argv):
            first = argv[i + 1].lower()
            i += 2
            continue
        if argv[i] == "--forward":
            reverse_second = False
        i += 1

    if batch_spec:
        try:
            specs = _parse_items(batch_spec)
        except ValueError as e:
            print(f"✗ {e}")
            return 1
    elif single > 0:
        specs = [("", single, True)]
    else:
        print("用法: --plan 5 [--copies 2] [--first even|odd] [--forward]")
        print("      --batch 4d,1s,5d [--copies 2] （d=双面 s=单面，可跨文件合成一批）")
        return 1

    files = [md.FilePlan(label, list(range(1, n + 1)), duplex) for label, n, duplex in specs]
    plan = md.plan_passes(files, copies=copies, first=first, reverse_second=reverse_second)

    desc = "、".join(f"{label or '单文件'}={n}页({'双面' if d else '单面'})"
                     for label, n, d in specs)
    print(f"批次: {desc} × {copies} 份 → 共 {plan.sheet_count} 张纸")
    print("=" * 78)
    print("第 1 趟 —— 进纸盒放空白纸:")
    print("  " + _fmt_slots(plan.first_pass, len(specs)))
    print("\n※ 打印完不要翻面、不要重排、不要丢弃空白页；整叠原样放回进纸盒（已打印面朝下）。")
    print("\n第 2 趟 —— 补打另一面:")
    print("  " + _fmt_slots(plan.second_pass, len(specs)))
    print("=" * 78)
    print(f"空白占位页: 第 1 趟 {plan.blank_slots_first} 个，第 2 趟 {plan.blank_slots_second} 个")
    for note in plan.notes:
        print(note)
    print("每张纸的两面: " + "，".join(plan.pair_lines()))
    print("实机核对: 每张纸两面对应同一文件相邻页（1/2、3/4…），且成品叠最上面是第 1 页。"
          "第 2 趟整体反了 → 加 --forward；成品最上面变成第 2 页 → 加 --first even（镜像约定）。")
    return 0


# ============================================================
# 三、嗅探驱动的手动翻纸弹窗，并试接管的可行路径
# ============================================================

def _proc_name(win32api, win32con, win32process, pid: int) -> str:
    """尽力取进程名；取不到时把错误原文带回来（取不到本身就是 UIPI/权限线索）。"""
    err = None
    handle = None
    try:
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid)
        return win32process.GetModuleFileNameEx(handle, 0)
    except Exception as e:
        err = e
    finally:
        if handle is not None:
            try:
                win32api.CloseHandle(handle)
            except Exception:
                pass
    return f"<取不到: {err}>"


def _children(win32gui, hwnd: int) -> list[dict]:
    out: list[dict] = []

    def cb(child, _):
        try:
            out.append({
                "hwnd": child,
                "id": win32gui.GetDlgCtrlID(child),
                "class": win32gui.GetClassName(child),
                "text": win32gui.GetWindowText(child),
                "visible": bool(win32gui.IsWindowVisible(child)),
            })
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, cb, None)
    except Exception:
        pass
    return out


def _looks_like_duplex_dialog(title: str, cls: str, kids: list[dict]) -> bool:
    t = title.lower()
    if any(w in t for w in DUPLEX_DIALOG_WORDS):
        return True
    # 标题不明显时，靠「有继续/Continue 按钮 + 说明文字里有双面/奇数页」判定
    if cls != "#32770":
        return False
    has_btn = any(k["class"] == "Button" and
                  any(w in k["text"].lower() for w in DUPLEX_BUTTON_WORDS) for k in kids)
    has_word = any(any(w in k["text"].lower() for w in DUPLEX_DIALOG_WORDS) for k in kids)
    return has_btn and has_word


def _try_press(win32api, win32con, win32gui, win32process, hwnd: int, kids: list[dict]) -> str:
    """依次尝试三种「按继续」的方式，返回哪种真的把对话框关掉了（没关掉也如实回报）。"""
    import time
    def is_button(k: dict) -> bool:
        return "button" in k["class"].lower()

    btn = next((k for k in kids if is_button(k) and k["visible"] and
                any(w in k["text"].lower() for w in DUPLEX_BUTTON_WORDS)), None)
    # 文本不含「继续/Continue/确定」时退而求其次：IDOK(1)，再不然唯一/第一个可见按钮
    if not btn:
        btn = next((k for k in kids if is_button(k) and k["visible"] and k["id"] == 1), None)
    if not btn:
        btn = next((k for k in kids if is_button(k) and k["visible"]), None)
    if not btn:
        return "找不到可点的按钮"

    attempts = []

    def gone(sec=1.5):
        end = time.time() + sec
        while time.time() < end:
            try:
                if not win32gui.IsWindow(hwnd):
                    return True
            except Exception:
                return True
            time.sleep(0.1)
        return False

    # ① 直接给按钮发 BM_CLICK（不需要窗口处于前台）
    try:
        win32gui.SendMessage(btn["hwnd"], win32con.BM_CLICK, 0, 0)
        attempts.append("BM_CLICK")
        if gone():
            return f"✓ BM_CLICK 成功（控件 id={btn['id']} text={btn['text']!r}）"
    except Exception as e:
        attempts.append(f"BM_CLICK 抛错 {e}")

    # ② 给对话框发 WM_COMMAND(BN_CLICKED)（等价于点该按钮，绕过前台限制）
    try:
        wparam = (win32con.BN_CLICKED << 16) | (btn["id"] & 0xFFFF)
        win32gui.PostMessage(hwnd, win32con.WM_COMMAND, wparam, btn["hwnd"])
        attempts.append("WM_COMMAND")
        if gone():
            return f"✓ WM_COMMAND(BN_CLICKED, id={btn['id']}) 成功"
    except Exception as e:
        attempts.append(f"WM_COMMAND 抛错 {e}")

    # ③ 抢前台 + 回车（缺陷：Enter 是对话框消息循环 IsDialogMessage 处理的，
    #    直接给窗口发 WM_KEYDOWN 无效，必须让它真的成为前台窗口）
    try:
        cur_tid = win32api.GetCurrentThreadId()
        fg = win32gui.GetForegroundWindow()
        fg_tid = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
        dlg_tid = win32process.GetWindowThreadProcessId(hwnd)[0]
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        for tid in (fg_tid, dlg_tid):
            if tid:
                win32process.AttachThreadInput(cur_tid, tid, True)
        ok_fg = bool(win32gui.SetForegroundWindow(hwnd))
        for tid in (fg_tid, dlg_tid):
            if tid:
                win32process.AttachThreadInput(cur_tid, tid, False)
        attempts.append(f"SetForegroundWindow={ok_fg}")
        if ok_fg:
            win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
            win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)
            if gone():
                return "✓ 抢前台+回车成功"
    except Exception as e:
        attempts.append(f"前台+回车抛错 {e}")

    return "✗ 三种点法都没关掉它（" + "; ".join(attempts) + "）"


def run_sniff(argv: list[str]) -> int:
    import time
    try:
        import win32api
        import win32con
        import win32gui
        import win32process
    except ImportError:
        print("✗ 缺少 pywin32：pip install pywin32")
        return 1

    seconds, press = 30.0, "--press" in argv
    if "--seconds" in argv:
        try:
            seconds = float(argv[argv.index("--seconds") + 1])
        except Exception:
            pass

    print(f"嗅探 {seconds:.0f} 秒… 现在去打印一份**双面**的测试文件，"
          f"等驱动弹出「双面打印说明」框。")
    print("（本模式只看窗口，不动打印机；加 --press 才会尝试点「继续」）\n" + "=" * 78)

    seen: set[int] = set()
    end = time.time() + seconds
    found = 0
    while time.time() < end:
        tops: list[int] = []

        def cb(hwnd, _):
            tops.append(hwnd)
            return True

        try:
            win32gui.EnumWindows(cb, None)
        except Exception as e:
            print(f"EnumWindows 失败: {e}")
            return 1

        for hwnd in tops:
            if hwnd in seen:
                continue
            try:
                title = win32gui.GetWindowText(hwnd)
                cls = win32gui.GetClassName(hwnd)
            except Exception:
                continue
            kids = _children(win32gui, hwnd)
            if not _looks_like_duplex_dialog(title, cls, kids):
                continue

            seen.add(hwnd)
            found += 1
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            print(f"[命中 #{found}] hwnd={hwnd}  类={cls}  标题={title!r}")
            print(f"  进程        : pid={pid} {_proc_name(win32api, win32con, win32process, pid)}")
            try:
                print(f"  可见/前台   : "
                      f"visible={bool(win32gui.IsWindowVisible(hwnd))} "
                      f"foreground={win32gui.GetForegroundWindow() == hwnd}")
                print(f"  矩形        : {win32gui.GetWindowRect(hwnd)}")
            except Exception:
                pass
            print(f"  子控件 {len(kids)} 个:")
            for k in kids:
                if k["text"] or k["class"] == "Button":
                    print(f"    id={k['id']:<6} class={k['class']:<18} "
                          f"visible={int(k['visible'])} text={k['text']!r}")
            if press:
                print(f"  接管尝试    : "
                      f"{_try_press(win32api, win32con, win32gui, win32process, hwnd, kids)}")
            print("-" * 78)
        time.sleep(0.3)

    if not found:
        print("没嗅到符合特征的弹窗。可能是: ①弹窗还没出现（先打印再等）；"
              "②它属于更高完整性进程，枚举看不到；③标题/控件文本与预期不同，"
              "请把当时屏幕上的窗口标题告诉我，我加进关键词。")
    return 0


def main(argv: list[str]) -> int:
    if "--sniff" in argv:
        return run_sniff(argv)
    if "--plan" in argv or "--batch" in argv:
        i = argv.index("--batch") if "--batch" in argv else -1
        spec = argv[i + 1] if i >= 0 and i + 1 < len(argv) else ""
        n = 0
        if "--plan" in argv:
            j = argv.index("--plan")
            if j + 1 < len(argv) and argv[j + 1].isdigit():
                n = int(argv[j + 1])
        return run_plan(argv, spec, n)
    return run_probe([a for a in argv if not a.startswith("--")])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
