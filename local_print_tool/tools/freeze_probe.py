"""GUI 卡死取证探针 —— 只在「真的卡住」的那几秒里抓 py-spy 栈。

用法（在 D:\\打印机项目\\local_print_tool 下）：

    py tools\\freeze_probe.py              # 附加到已经在跑的本工具窗口
    py tools\\freeze_probe.py --launch     # 探针自己用 py main.py 拉起来（推荐，环境与手点一致）
    py tools\\freeze_probe.py --seconds 120

它做什么：
  · 每 0.1s 对主窗口发一次 WM_NULL（SendMessageTimeout，300ms 超时）—— 这就是 Windows
    判定「(未响应)」的同一套机制；连续超时 ≈ 主线程没在泵消息。
  · 卡满 2.4s 就自动 `py-spy dump --pid`（再隔 1.5s 补一次，两次同帧即锁定阻塞点），
    连所有线程的 Python 栈一起写文件，**不注入、不暂停进程**，抓完自动收工。
  · 结果写 %TEMP%\\hn_freeze_<pid>_<时间>.txt，控制台同时回显。

为什么需要它：日志只能看到「停在哪一行打印」，看不到停在哪一帧；gdb 式断点会把卡死错过。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime

WM_NULL = 0x0000
SMTO_ABORTIFHUNG = 0x0002
SMTO_BLOCK = 0x0001
POLL_SEC = 0.1
PROBE_TIMEOUT_MS = 300
HANG_AFTER_TIMEOUTS = 8          # 连续 8 次超时 ≈ 2.4s 没泵消息
DUMP_AGAIN_SEC = 1.5
TITLE_HINTS = ("HN 本地打印工具", "本地打印工具", "HN 打印工具")

user32 = ctypes.windll.user32
EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def _window_title_text(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _pid_of(hwnd: int) -> int:
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def find_app_windows() -> list[tuple[int, int, str]]:
    """返回 [(hwnd, pid, title)]：标题像本工具的**可见顶层**窗口。"""
    found: list[tuple[int, int, str]] = []

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_title_text(hwnd)
        if title and any(h in title for h in TITLE_HINTS):
            found.append((hwnd, _pid_of(hwnd), title))
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return found


def ping(hwnd: int) -> bool:
    """True=主线程在泵消息（响应），False=超时（未响应）。"""
    res = wt.DWORD(0)
    ok = user32.SendMessageTimeoutW(
        wt.HWND(hwnd), WM_NULL, 0, 0,
        SMTO_ABORTIFHUNG | SMTO_BLOCK, PROBE_TIMEOUT_MS, ctypes.byref(res),
    )
    return bool(ok)


def pyspy_exe() -> str | None:
    exe = shutil.which("py-spy")
    if exe:
        return exe
    for cand in (
        os.path.join(sys.prefix, "Scripts", "py-spy.exe"),
        os.path.join(os.path.dirname(sys.executable), "Scripts", "py-spy.exe"),
        r"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\Scripts\py-spy.EXE",
    ):
        if os.path.isfile(cand):
            return cand
    return None


def _dump(spy: str, pid: int, tag: str, sink) -> str:
    try:
        out = subprocess.run(
            [spy, "dump", "--pid", str(pid)],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
        body = (out.stdout or "") + (("\n[stderr]\n" + out.stderr) if out.stderr else "")
    except subprocess.TimeoutExpired:
        body = "[py-spy dump 超时 30s]"
    except Exception as e:                                    # noqa: BLE001
        body = f"[py-spy dump 失败] {type(e).__name__}: {e}"
    block = f"\n{'=' * 78}\n### {tag}  {datetime.now().strftime('%H:%M:%S.%f')[:-3]}\n{'=' * 78}\n{body}"
    sink.write(block)
    sink.flush()
    return block


def watch(pid: int, hwnd: int, seconds: float, sink) -> None:
    spy = pyspy_exe()
    if not spy:
        print("✗ 找不到 py-spy，先装：py -m pip install py-spy", flush=True)
        return
    print(f"· py-spy = {spy}", flush=True)
    print(f"· 目标窗口 pid={pid} hwnd=0x{hwnd:X} —— 现在去点「开始打印」，卡住后什么都不用做",
          flush=True)

    deadline = time.time() + seconds if seconds > 0 else 0.0
    busy = 0
    episode_start = 0.0
    dumped_in_episode = False
    episodes: list[tuple[str, float]] = []
    n = 0

    while True:
        if deadline and time.time() > deadline:
            print(f"\n· 到时限（{seconds:.0f}s），收工", flush=True)
            break
        n += 1
        alive = user32.IsWindow(wt.HWND(hwnd))
        if not alive:
            print("\n· 目标窗口已关闭（进程退出？），收工", flush=True)
            break

        responsive = ping(hwnd)
        now = time.time()
        if responsive:
            if busy:
                dur = now - episode_start
                episodes.append((datetime.fromtimestamp(episode_start).strftime('%H:%M:%S.%f')[:-3], dur))
                print(f"\n  ↳ 恢复响应（这一次卡了 {dur:.1f}s）", flush=True)
            busy = 0
            episode_start = 0.0
            dumped_in_episode = False
            if n % 20 == 0:
                print(".", end="", flush=True)
            time.sleep(POLL_SEC)
            continue

        if busy == 0:
            episode_start = now
        busy += 1
        print("!", end="", flush=True)
        if busy >= HANG_AFTER_TIMEOUTS and not dumped_in_episode:
            dumped_in_episode = True
            hung_for = now - episode_start
            print(f"\n⚠ 检测到未响应 {hung_for:.1f}s —— 抓栈（主线程 + 全部线程）…", flush=True)
            t = threading.Thread(
                target=_dump_twice, args=(spy, pid, episode_start), daemon=True,
            )
            t.start()
        time.sleep(POLL_SEC)

    print("\n──────── 卡死时间线 ────────", flush=True)
    if not episodes:
        print("（本次没有观察到 ≥2.4s 的未响应）", flush=True)
    for start, dur in episodes:
        flag = "  ← 已抓栈" if dur >= HANG_AFTER_TIMEOUTS * PROBE_TIMEOUT_MS / 1000 else ""
        print(f"  {start}  卡 {dur:5.1f}s{flag}", flush=True)
    print(f"\n完整栈写在：{sink.name}", flush=True)


def _dump_twice(spy: str, pid: int, episode_start: float) -> None:
    sink_name = _SINK_PATH
    with open(sink_name, "a", encoding="utf-8") as sink:
        stamp = datetime.fromtimestamp(episode_start).strftime('%H:%M:%S')
        first = _dump(spy, pid, f"卡死中第 1 次抓栈（卡始于 {stamp}）", sink)
        time.sleep(DUMP_AGAIN_SEC)
        second = _dump(spy, pid, "卡死中第 2 次抓栈（确认同一帧）", sink)
        print(f"\n[探针] 两次抓栈完成 → {sink_name}", flush=True)
        print(_short(first), flush=True)
        if _short(first) == _short(second):
            print("[探针] 两次完全一致 ⇒ 这段时间确实阻塞在同一处", flush=True)


def _short(dump_text: str, limit: int = 1200) -> str:
    """只回显 MainThread 那一段，便于人眼看。"""
    lines = dump_text.splitlines()
    out: list[str] = []
    keep = False
    for ln in lines:
        if "Thread" in ln and "(" in ln:
            keep = "MainThread" in ln or "Main " in ln
        if keep:
            out.append(ln)
    return "\n".join(out)[:limit] or dump_text[:limit]


_SINK_PATH = ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0, help="直接指定进程 pid（默认按窗口标题找）")
    ap.add_argument("--launch", action="store_true", help="探针自己拉起 py main.py")
    ap.add_argument("--seconds", type=float, default=0.0, help="跑多久自动收工（0=直到 Ctrl+C）")
    args = ap.parse_args()

    global _SINK_PATH

    if args.launch:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        print(f"· 启动 {os.path.join(here, 'main.py')}", flush=True)
        subprocess.Popen([sys.executable, "main.py"], cwd=here)
        time.sleep(6)

    hwnd, pid = 0, args.pid
    for _ in range(50):
        wins = find_app_windows()
        if pid:
            wins = [w for w in wins if w[1] == pid] or wins
        if wins:
            hwnd, pid, title = wins[0]
            break
        time.sleep(0.4)
    else:
        print("✗ 找不到「HN 本地打印工具」窗口 —— 先启动主程序，或加 --launch", flush=True)
        return 2

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _SINK_PATH = os.path.join(tempfile.gettempdir(), f"hn_freeze_{pid}_{ts}.txt")
    with open(_SINK_PATH, "w", encoding="utf-8") as sink:
        sink.write(f"# HN 本地打印工具 GUI 卡死取证\n# 窗口={title!r} pid={pid} hwnd=0x{hwnd:X}\n"
                   f"# 开始={datetime.now():%Y-%m-%d %H:%M:%S}\n")

    print(f"· 窗口：{title}（pid={pid}）", flush=True)
    print(f"· 落盘：{_SINK_PATH}", flush=True)
    try:
        with open(_SINK_PATH, "a", encoding="utf-8") as sink:
            watch(pid, hwnd, args.seconds, sink)
    except KeyboardInterrupt:
        print(f"\n· 手动收工。栈在：{_SINK_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
