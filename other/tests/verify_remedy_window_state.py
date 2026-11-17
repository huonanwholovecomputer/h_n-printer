# -*- coding: utf-8 -*-
"""验收脚本 · 半成品补救的状态与计时（2026-11-17 现场）

现场两个症状（同一轮补救里同时出现）：

  ① 补救窗的「第 1 趟用时」永远停在 00:00 不动 —— 补救只调 set_flippable()，
     而"起表"那两行（_t0.restart + _pass1_timer.start）只在 announce() 里，补救根本
     不走 announce() ⇒ 计时器从没启动。
  ② 补救结束后窗口不收（一直停在「③ 正在打印第 2 趟…」、按钮全灰），下一轮补救被
     「另一个补救补打还在跑」永久挡住 —— _on_remedy_done 只清线程、不把窗口放回 done；
     下一轮 flip_required 的 set_flippable() 又被态势守卫挡掉，界面永不出现，那轮 worker
     干等 600+20 秒才超时，期间所有补救都被忙判据拒绝。

判据（A 静态接线 + B 运行时真构造 + C 变异自证）：

  A. _start_remedy_worker 必须先调 begin_remedy、_on_remedy_done 必须调 finish_remedy；
     finish_remedy 里必须把 _mode 置回 "done"；begin_remedy 里必须置 "remedy"；
     closeEvent 的跳过判据必须含 "remedy" 而**不含** "remedy_pass2"。
  B. 真构造 ManualDuplexDialog（offscreen）走一遍：
     begin_remedy → 等纸态、按钮可点、计时器**没**起跑；
     _on_go → remedy_pass2、_pass1_timer 活动、计时文案出现「已用」；
     finish_remedy → done、写「用时」、立刻可再开下一轮（卡死回归判据）；
     关窗语义：remedy 态 = 取消（发 skip），remedy_pass2 态 = 只收窗口（不发 skip）。
  C. 变异自证：删掉 finish_remedy 调用 / begin_remedy 不复位状态 / finish_remedy 不回
     done，静态判据必须当场转红（否则这套判据是空转的）。

    python tests/verify_remedy_window_state.py     # exit 0 = 全绿
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

GUI = os.path.join(LOCAL_TOOL, "gui.py")
SRC = io.open(GUI, encoding="utf-8").read()

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return cond


def section(title):
    print("\n" + "=" * 78)
    print(title)


def method_body(src, name):
    """取某个方法（4 空格缩进的 def）的源码，到同缩进的下一处 def/class/decorator 为止。"""
    m = re.search(rf"\n    def {re.escape(name)}\(self[^)]*\)", src)
    if not m:
        return ""
    body = src[m.end():]
    end = re.search(r"\n    (?:def |class |@)", body)
    return body[:end.start()] if end else body


def static_fails(src):
    """返回静态判据的失败清单；传变异后的源码即可复用做自证。"""
    fails = []

    start = method_body(src, "_start_remedy_worker")
    if not start:
        fails.append("找不到 _start_remedy_worker")
    elif "begin_remedy(" not in start:
        fails.append("_start_remedy_worker 没先调 begin_remedy()"
                     "（窗口可能停在上一轮的 pass2/done 态，界面永不出现）")

    done = method_body(src, "_on_remedy_done")
    if not done:
        fails.append("找不到 _on_remedy_done")
    elif "finish_remedy(" not in done:
        fails.append("_on_remedy_done 没调 finish_remedy()"
                     "（窗口停在 pass2 态 ⇒ 下一个补救永远启动不了）")

    fin = method_body(src, "finish_remedy")
    if not fin:
        fails.append("找不到 finish_remedy")
    else:
        if 'self._mode = "done"' not in fin:
            fails.append('finish_remedy 没把 _mode 置回 "done"（下一轮补救会被态势守卫挡掉）')
        if "_pass1_timer.stop()" not in fin:
            fails.append("finish_remedy 没停 _pass1_timer（计时会继续往后跑）")

    beg = method_body(src, "begin_remedy")
    if not beg:
        fails.append("找不到 begin_remedy")
    else:
        if 'self._mode = "remedy"' not in beg:
            fails.append('begin_remedy 没把 _mode 置成 "remedy"（等纸态起不来）')
        if "setEnabled(True)" not in beg:
            fails.append("begin_remedy 没把「开始」按钮点亮（硬规矩 12：出现即可点）")

    go = method_body(src, "_on_go")
    if 'self._mode = "remedy_pass2"' not in go:
        fails.append('补救分支没有 _mode = "remedy_pass2"')
    if "_remedy_t0.start()" not in go:
        fails.append("补救分支没有 _remedy_t0.start()（补打计时不会起跑）")

    close = method_body(src, "closeEvent")
    m = re.search(r"if self\._mode in \(([^)]*)\):\s*\n\s*self\._on_skip\(\)", close)
    if not m:
        fails.append("closeEvent 里找不到「关窗 = 跳过」的判据")
    else:
        names = [x.strip().strip("'\"") for x in m.group(1).split(",")]
        if "remedy" not in names:
            fails.append("closeEvent 跳过判据里没有 remedy（等纸时关窗不取消，补救会干等超时）")
        if "remedy_pass2" in names:
            fails.append("closeEvent 跳过判据里混进了 remedy_pass2（正在印的补救会被收窗判掉）")
    return fails


section("A. 静态：补救链路必须接上，且能回到 done")
fails = static_fails(SRC)
for msg in fails:
    check(msg, False)
check("A 全部静态判据（begin/finish 接线、done 复位、closeEvent 判据）",
      not fails, "；".join(fails) if fails else "")

# ── B. 运行时（offscreen）─────────────────────────────────────────────────
section("B. 运行时：真走一遍补救的两个状态")

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])
import gui  # noqa: E402

dlg = gui.ManualDuplexDialog()
dlg.show()

_cont = []
_skip = []
dlg.continue_requested.connect(lambda: _cont.append(1))
dlg.skip_requested.connect(lambda: _skip.append(1))

# B1. 起手：等纸态
dlg.begin_remedy("第 1 趟已打过的面朝下放进纸盘 2，然后点开始。",
                 what="补打第 2 趟", sheets=4, estimate_sec=0.0, timeout_sec=600)
check('begin_remedy → _mode == "remedy"'
      , dlg._mode == "remedy", dlg._mode)
check("begin_remedy → 状态栏写的是「等纸就位」，不是「正在打印」",
      "开始补打第 2 趟" in dlg._status.text() or "把纸放好" in dlg._status.text(),
      dlg._status.text())
check("begin_remedy → 「开始」按钮当场可点（硬规矩 12）",
      dlg._btn_go.isEnabled() and "开始" in dlg._btn_go.text(),
      f"enabled={dlg._btn_go.isEnabled()} text={dlg._btn_go.text()!r}")
check("begin_remedy → 计时器没有起跑（要等点了开始才起表）",
      not dlg._pass1_timer.isActive() and not dlg._remedy_t0.isValid())
check("begin_remedy → 就位说明铺到了详情区",
      "纸盘 2" in dlg._detail.text(), dlg._detail.text()[:40])

# B2. worker 的正式说明覆盖进来（旧口径会误写成「第 1 趟的数据已全部送进队列」）
dlg.set_flippable("把这叠纸从出纸槽取出，原样放进纸盘 2：已打印的那一面朝下。", 600)
check("set_flippable 在补救态只更新说明、不动状态", dlg._mode == "remedy", dlg._mode)
check("set_flippable 的正式说明铺上了",
      "出纸槽" in dlg._detail.text(), dlg._detail.text()[:40])
check("set_flippable 没把「第 1 趟的数据已全部送进打印队列」抄过来",
      "第 1 趟的数据" not in dlg._detail.text(), dlg._detail.text()[:60])

# B3. 点「开始」= 真的送印，补打计时从 0 起跑
dlg._on_go()
check('点开始 → _mode == "remedy_pass2"', dlg._mode == "remedy_pass2", dlg._mode)
check("点开始 → 补打计时器真的在跑（这是 ① 的回归判据）",
      dlg._pass1_timer.isActive() and dlg._remedy_t0.isValid())
check("点开始 → 状态栏是「③ 正在补打第 2 趟」",
      "正在补打第 2 趟" in dlg._status.text(), dlg._status.text())
check("点开始 → continue_requested 发出一次（worker 才知道可以送纸）",
      len(_cont) == 1, f"{len(_cont)} 次")

_real_elapsed = dlg._remedy_elapsed
dlg._remedy_elapsed = lambda: 125.0
dlg._remedy_tick()
_t = dlg._pass1.text()
check("计时文案是「补打已用 …」且真的在动（不再是 00:00 的第 1 趟）",
      "已用" in _t and ("02:05" in _t or "125" in _t), _t)
check("计时文案里不再出现「第 1 趟」", "第 1 趟" not in _t, _t)
dlg._remedy_elapsed = _real_elapsed

# B4. 收尾：窗口必须回到 done，且把「上一轮补救」彻底放掉
dlg._remedy_elapsed = lambda: 125.0      # 假装真的送印了两分多钟
dlg.finish_remedy("✓ 补打完成：第 2 趟已出纸")
dlg._remedy_elapsed = _real_elapsed
check('finish_remedy → _mode == "done"（这是 ② 的回归判据）',
      dlg._mode == "done", dlg._mode)
check("finish_remedy → 计时器已停", not dlg._pass1_timer.isActive())
check("finish_remedy → 写了「用时」且是真实耗时（02:05）",
      "用时" in dlg._pass1.text() and "02:05" in dlg._pass1.text(), dlg._pass1.text())
check("finish_remedy → 跳过按钮变成「关闭」",
      dlg._btn_skip.text() == "关闭" and dlg._btn_skip.isEnabled(), dlg._btn_skip.text())
check("finish_remedy → 安排了自动收窗", dlg._close_timer.isActive())
dlg._close_timer.stop()       # 别让它 1.5 秒后把窗关掉，后面还要接着测

# B4b. 极端快的一轮（不足 1 秒）也要写「用时 00:00」，不能整行空着
dlg0 = gui.ManualDuplexDialog()
dlg0.show()
dlg0.begin_remedy("快轮", what="补打第 2 趟", timeout_sec=600)
dlg0._on_go()
dlg0.finish_remedy("✓ 快轮完成")
check("计时不足 1 秒时也写「用时 00:00」（不留空行）",
      "用时" in dlg0._pass1.text() and "00:00" in dlg0._pass1.text(), dlg0._pass1.text())
dlg0._close_timer.stop()
dlg0.hide()

# B5. 卡死回归：上一轮结束后，下一轮补救必须能起来（现场就是死在这里）
dlg.begin_remedy("本轮第 1 趟已打过的面朝下放回纸盘 2。", what="整单重打", sheets=3,
                 timeout_sec=600)
check("第二轮 begin_remedy 仍能进入 remedy 态（不再卡在上一轮）",
      dlg._mode == "remedy", dlg._mode)
dlg.set_flippable("整单重打：把纸原样放回纸盘 2。", 600)
check("第二轮 set_flippable 的说明能铺上（态势守卫不再吃掉它）",
      "整单重打" in dlg._detail.text(), dlg._detail.text()[:40])

# B6. 旧状态残留也要能自愈：强行塞一个 pass2（旧版 bug 留下的态）再 begin_remedy
dlg._mode = "pass2"
dlg.begin_remedy("第三轮：纸放回纸盘 2。", what="补打第 2 趟", timeout_sec=600)
check("从残留的 pass2 态调 begin_remedy 也能强制切回 remedy",
      dlg._mode == "remedy", dlg._mode)

# B7. 关窗语义：remedy = 取消；remedy_pass2 = 只收窗口
_skip.clear()
dlg._mode = "remedy"
dlg.close()
check("remedy 态关窗 = 取消本次补打（发 skip_requested，worker 立即收手）",
      len(_skip) == 1 and dlg._mode == "skipped", f"skip={len(_skip)} mode={dlg._mode}")

dlg2 = gui.ManualDuplexDialog()
dlg2.show()
_skip2 = []
dlg2.skip_requested.connect(lambda: _skip2.append(1))
dlg2.begin_remedy("第四轮：纸放回纸盘 2。", what="补打第 2 趟", timeout_sec=600)
dlg2._on_go()
check("前置：第二轮窗在 remedy_pass2 态", dlg2._mode == "remedy_pass2", dlg2._mode)
dlg2.close()
check("remedy_pass2 态关窗不发 skip（纸已经在印，收窗不能把补救判掉）",
      not _skip2 and dlg2._mode == "remedy_pass2", f"skip={len(_skip2)} mode={dlg2._mode}")

# ── C. 变异自证 ───────────────────────────────────────────────────────────
section("C. 变异自证：判据必须能红")

mut1 = SRC.replace(
    '            self._ensure_duplex_dialog().finish_remedy(("✓ " if ok else "✗ ") + str(message or ""))',
    '            self._ensure_duplex_dialog()', 1)
check("变异体 1 真的改到了源码（锚点存在）", mut1 != SRC)
f1 = static_fails(mut1)
check("变异 1（_on_remedy_done 不调 finish_remedy）→ 静态判据转红", bool(f1),
      "；".join(f1[:2]))

mut2 = SRC.replace('        self._remedy_t0.invalidate()\n        self._mode = "remedy"',
                   '        self._remedy_t0.invalidate()', 1)
check("变异体 2 真的改到了源码（锚点存在）", mut2 != SRC)
f2 = static_fails(mut2)
check('变异 2（begin_remedy 不置 "remedy"）→ 静态判据转红', bool(f2),
      "；".join(f2[:2]))

mut3 = SRC.replace('        self._remedy_t0.invalidate()\n        self._mode = "done"',
                   '        self._remedy_t0.invalidate()', 1)
check("变异体 3 真的改到了源码（锚点存在）", mut3 != SRC)
f3 = static_fails(mut3)
check('变异 3（finish_remedy 不回到 done）→ 静态判据转红', bool(f3),
      "；".join(f3[:2]))

section("结果")
print("半成品补救状态与计时验收: " + ("全部通过 ✅" if ok_all else "有失败 ❌"))
sys.exit(0 if ok_all else 1)
