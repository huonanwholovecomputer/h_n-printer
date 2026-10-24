"""office_sandbox.py — Office 转换的隔离执行器（S2-03 客户端半）

问题背景
--------
Word/WPS 的 COM 转换会在本进程里拉起 Office，并打开顾客/用户文档。S1-04 已经把
「宏/外部链接/可见窗口」关掉，但还有两类风险留在本进程边界之内：

1. **进程失控**：文档触发 Office 卡死/弹模态框/吃满内存时，卡住的是整个打印工具
   （GUI 线程里等 COM 返回，界面冻结、队列停摆），只能人工杀进程；
2. **子进程无主**：COM 拉起的 `winword.exe` / `wps.exe` 不在任何生命周期约束下，
   工具退出后仍可能残留（下次转换命中已损坏的 Office 实例）。

做法
----
把**本地 Office 文档**的转换改到独立 worker 进程里跑（`convert_worker.py`），
父进程用 Windows **Job Object** 把它和它拉起的所有 Office 子进程绑在一起：

- `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`：父进程退出/崩溃 ⇒ 整棵进程树一起回收；
- `JOB_OBJECT_LIMIT_JOB_MEMORY`（默认 2048 MB，可用 `HN_OFFICE_SANDBOX_MEM_MB` 调）：
  文档吃内存到上限就让 Office 失败，而不是拖垮整台机器；
- 超时（默认 180 s，可用 `HN_OFFICE_SANDBOX_TIMEOUT` 调）到点直接 `TerminateJobObject`
  —— 这是**失败闭环**：超时/失败不静默回退进程内转换（恶意文档正是靠"卡死→回退"
  把风险带回主进程），只有"沙箱本身不可用"才回退（等于今天的行为）。

启动协议（防竞态）
----------------
worker 启动后先阻塞在 stdin 上等父进程一行 `GO`；父进程先 `AssignProcessToJobObject`
再写 `GO`。这样不会出现"worker 已经开始拉 Office、但还没进 Job"的窗口。

worker 结果通过 stdout 最后一行 `RESULT:{json}` 返回；转换产物必须非空且以 `%PDF` 开头
（与 converter._pdf_product_ok 同口径，避免 COM"静默成功"的空文件被当成功）。

平台
----
仅 Windows 有 Job Object；其它平台 `available()` 返回 False，调用方回退进程内转换。
刻意**没有**实现 CreateRestrictedToken 的低权/低完整性令牌：Office COM 需要用户配置
与桌面环境，低完整性下 Word/WPS 基本起不来（会变成"每单都失败"），Job Object 才是
这里能真正落地的约束（计划原文为"低权进程/Job Object"，取后者）。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── 默认参数（均可用环境变量覆盖，便于现场调试）──
DEFAULT_TIMEOUT_SEC = 180.0
DEFAULT_MEMORY_MB = 2048

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_CREATE_NO_WINDOW = 0x08000000


def _timeout_sec() -> float:
    try:
        return max(10.0, float(os.environ.get("HN_OFFICE_SANDBOX_TIMEOUT", DEFAULT_TIMEOUT_SEC)))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SEC


def _memory_mb() -> int:
    try:
        return max(256, int(os.environ.get("HN_OFFICE_SANDBOX_MEM_MB", DEFAULT_MEMORY_MB)))
    except (TypeError, ValueError):
        return DEFAULT_MEMORY_MB


def available() -> bool:
    """当前环境能否使用 Job Object 沙箱（仅 Windows）。"""
    return sys.platform == "win32" and hasattr(ctypes, "windll")


@dataclass
class SandboxResult:
    """一次隔离转换的结论。status: ok | failed | timeout | unavailable"""
    status: str
    output: str = ""
    error: str = ""
    returncode: int | None = None


# ── Win32 Job Object 封装（ctypes；只在 Windows 模块加载时使用）──

if sys.platform == "win32":  # pragma: no cover - Windows 专属分支
    from ctypes import wintypes as _wt

    _SIZE_T = ctypes.c_size_t
    _ULONG_PTR = ctypes.c_size_t
    _DWORD = ctypes.c_ulong
    _LARGE_INTEGER = ctypes.c_int64
    _ULONGLONG = ctypes.c_ulonglong

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", _LARGE_INTEGER),
            ("PerJobUserTimeLimit", _LARGE_INTEGER),
            ("LimitFlags", _DWORD),
            ("MinimumWorkingSetSize", _SIZE_T),
            ("MaximumWorkingSetSize", _SIZE_T),
            ("ActiveProcessLimit", _DWORD),
            ("Affinity", _ULONG_PTR),
            ("PriorityClass", _DWORD),
            ("SchedulingClass", _DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", _ULONGLONG),
            ("WriteOperationCount", _ULONGLONG),
            ("OtherOperationCount", _ULONGLONG),
            ("ReadTransferCount", _ULONGLONG),
            ("WriteTransferCount", _ULONGLONG),
            ("OtherTransferCount", _ULONGLONG),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", _SIZE_T),
            ("JobMemoryLimit", _SIZE_T),
            ("PeakProcessMemoryUsed", _SIZE_T),
            ("PeakJobMemoryUsed", _SIZE_T),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [_wt.LPVOID, _wt.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = _wt.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        _wt.HANDLE, ctypes.c_int, _wt.LPVOID, _DWORD]
    _kernel32.SetInformationJobObject.restype = _wt.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [_wt.HANDLE, _wt.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = _wt.BOOL
    _kernel32.TerminateJobObject.argtypes = [_wt.HANDLE, _wt.UINT]
    _kernel32.TerminateJobObject.restype = _wt.BOOL
    _kernel32.CloseHandle.argtypes = [_wt.HANDLE]
    _kernel32.CloseHandle.restype = _wt.BOOL
else:  # pragma: no cover
    _kernel32 = None


def _create_job(memory_mb: int):
    """创建 Job Object 并设置「关闭即杀 + 内存上限」。失败抛 OSError。"""
    if not available():
        raise OSError("当前平台不支持 Job Object 沙箱")
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(f"CreateJobObject 失败: {ctypes.get_last_error()}")
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (
        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_JOB_MEMORY)
    info.JobMemoryLimit = int(memory_mb) * 1024 * 1024
    ok = _kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        err = ctypes.get_last_error()
        _kernel32.CloseHandle(job)
        raise OSError(f"SetInformationJobObject 失败: {err}")
    return job


def _close_job(job) -> None:
    try:
        _kernel32.CloseHandle(job)
    except Exception:  # noqa: BLE001
        pass


def _worker_command(spec_path: str) -> list[str]:
    """worker 启动命令：源码模式跑 convert_worker.py；打包模式走 exe 的 --convert-worker。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--convert-worker", spec_path]
    here = os.path.dirname(os.path.abspath(__file__))
    return [sys.executable, os.path.join(here, "convert_worker.py"), spec_path]


def run_office_conversion(src: str, dst: str, image_orientation: str = "auto",
                          timeout: float | None = None,
                          memory_mb: int | None = None) -> SandboxResult:
    """在 Job Object 沙箱里把 src 转成 dst。**不抛异常**，失败以 SandboxResult 返回。

    status:
      - ok          产物已落盘且非空（调用方仍会做 %PDF 头校验，双保险）
      - unavailable 沙箱不可用（非 Windows / Job 创建失败 / worker 起不来）
                    → 调用方回退进程内转换（等价改前行为，不算安全退化）
      - timeout     超时被 TerminateJobObject（失败闭环，不回退）
      - failed      worker 报错或产物无效（失败闭环，不回退）
    """
    if not available():
        return SandboxResult(status="unavailable", error="非 Windows 平台，无 Job Object")
    if not src or not os.path.isfile(src):
        return SandboxResult(status="failed", error=f"源文件不存在: {src}")

    timeout = _timeout_sec() if timeout is None else float(timeout)
    memory_mb = _memory_mb() if memory_mb is None else int(memory_mb)

    spec_fd, spec_path = tempfile.mkstemp(suffix=".json", prefix="_convspec_")
    try:
        with os.fdopen(spec_fd, "w", encoding="utf-8") as f:
            json.dump({
                "src": os.path.abspath(src),
                "dst": os.path.abspath(dst),
                "image_orientation": image_orientation or "auto",
                "from_cloud": False,
            }, f, ensure_ascii=False)
        return _run_worker(spec_path, timeout, memory_mb)
    finally:
        try:
            os.remove(spec_path)
        except OSError:
            pass


def _run_worker(spec_path: str, timeout: float, memory_mb: int) -> SandboxResult:
    job = None
    proc = None
    try:
        try:
            job = _create_job(memory_mb)
        except OSError as e:
            logger.warning(f"转换沙箱不可用（Job 创建失败），回退进程内转换: {e}")
            return SandboxResult(status="unavailable", error=str(e))

        try:
            proc = subprocess.Popen(
                _worker_command(spec_path),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except OSError as e:
            logger.warning(f"转换沙箱不可用（worker 启动失败），回退进程内转换: {e}")
            return SandboxResult(status="unavailable", error=str(e))

        # 先入 Job 再放行（worker 在 stdin 上等 GO）—— 避免"已拉起 Office 却没进 Job"的窗口
        try:
            assigned = _kernel32.AssignProcessToJobObject(
                job, _wt.HANDLE(int(proc._handle)))
            if not assigned:
                err = ctypes.get_last_error()
                logger.warning(
                    f"无法把转换 worker 加入 Job Object（err={err}），回退进程内转换")
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                return SandboxResult(status="unavailable", error=f"AssignProcessToJobObject 失败: {err}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"转换 worker 加入 Job Object 异常，回退进程内转换: {e}")
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            return SandboxResult(status="unavailable", error=str(e))

        logger.info(f"转换沙箱: 启动隔离 worker（超时 {timeout:.0f}s，内存上限 {memory_mb} MB）")
        try:
            out, err = proc.communicate(input=b"GO\n", timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning(f"转换沙箱超时（{timeout:.0f}s），终止整个 Job（含 Office 子进程）")
            try:
                _kernel32.TerminateJobObject(job, 1)
            except Exception:  # noqa: BLE001
                pass
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:  # noqa: BLE001
                out, err = b"", b""
            return SandboxResult(status="timeout", error=f"隔离转换超时（{timeout:.0f}s）",
                                 returncode=proc.returncode)

        stdout = (out or b"").decode("utf-8", "replace")
        stderr = (err or b"").decode("utf-8", "replace")
        data = _parse_result(stdout)
        if data is None:
            tail = (stderr or stdout).strip()[-300:]
            return SandboxResult(status="failed",
                                 error=f"worker 未返回结果（rc={proc.returncode}）: {tail}",
                                 returncode=proc.returncode)
        if not data.get("ok"):
            return SandboxResult(status="failed",
                                 error=str(data.get("error") or "转换失败"),
                                 returncode=proc.returncode)
        output = str(data.get("output") or "")
        if not output or not os.path.isfile(output) or os.path.getsize(output) == 0:
            return SandboxResult(status="failed", error="worker 报告成功但产物缺失/为空",
                                 returncode=proc.returncode)
        return SandboxResult(status="ok", output=output, returncode=proc.returncode)
    finally:
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        if job is not None:
            # KILL_ON_JOB_CLOSE：关掉句柄即回收该 Job 下所有残留进程（含 Office）
            _close_job(job)


def _parse_result(stdout: str) -> dict | None:
    """从 worker stdout 取最后一行 RESULT:{json}。"""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("RESULT:"):
            try:
                data = json.loads(line[len("RESULT:"):])
            except (ValueError, json.JSONDecodeError):
                return None
            return data if isinstance(data, dict) else None
    return None
