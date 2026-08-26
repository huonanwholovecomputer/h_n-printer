"""
single_instance.py — 单实例锁（Windows）

确保程序同时只有一个实例在运行：
- 第一个实例创建命名互斥体（Mutex）持有锁；
- 第二个实例启动时 CreateMutexW 返回 ERROR_ALREADY_EXISTS → 说明已有实例在运行，
  通过命名事件（Event，自动复位）通知既有实例把窗口置于前台，然后自身退出。

用法：
    from single_instance import acquire, get_front_event, check_front_request
    if not acquire():
        return  # 已有实例，已通知其置前，本实例退出
    # 主窗口创建后：
    evt = get_front_event()
    QTimer 轮询 check_front_request(evt) → 收到则 showNormal/raise_/activateWindow
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

_kernel32 = ctypes.windll.kernel32

# 命名互斥体/事件：Local\ 前缀 = 当前登录会话内唯一（同机多用户互不影响）
_MUTEX_NAME = "Local\\HN_Print_Tool_SingleInstance_Mutex"
_EVENT_NAME = "Local\\HN_Print_Tool_SingleInstance_Event"

ERROR_ALREADY_EXISTS = 183
_EVENT_MODIFY_STATE = 0x0002          # SetEvent 所需权限
WAIT_OBJECT_0 = 0                     # WaitForSingleObject 信号态

_mutex_handle = None                  # 第一个实例持有（进程存活期间保持锁）
_front_event = None                   # 第一个实例的置前通知事件句柄

# ── 声明函数签名（避免 ctypes 默认整型截断句柄/指针）──
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.OpenEventW.restype = wintypes.HANDLE
_kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.SetEvent.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.GetLastError.restype = wintypes.DWORD


def acquire() -> bool:
    """获取单实例锁。

    Returns:
        True  = 本实例是唯一实例（互斥体已持有，继续启动）
        False = 已有实例在运行（已通知其将窗口置于前台），调用方应立即退出
    """
    global _mutex_handle, _front_event
    h = _kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if not h:
        # 创建失败（权限/系统异常）→ 放行，避免把用户锁在门外
        return True
    if _kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(h)
        _notify_front()
        return False
    # 第一个实例：持有互斥体句柄，保持锁
    _mutex_handle = h
    # 创建置前通知事件（自动复位）；若已存在（上次实例残留）则复用，无碍
    _front_event = _kernel32.CreateEventW(None, False, False, _EVENT_NAME)
    return True


def _notify_front() -> None:
    """通知既有实例把窗口置于前台。

    事件可能恰逢首个实例刚创建互斥体、尚未创建事件（极短窗口），短时重试兜底。
    """
    for _ in range(10):
        evt = _kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, _EVENT_NAME)
        if evt:
            try:
                _kernel32.SetEvent(evt)
            finally:
                _kernel32.CloseHandle(evt)
            return
        time.sleep(0.2)


def get_front_event():
    """返回第一个实例的置前通知事件句柄（供主窗口轮询），无则 None。"""
    return _front_event


def check_front_request(handle) -> bool:
    """非阻塞检查是否收到「置前」通知（自动复位，收到即消费）。"""
    if not handle:
        return False
    return _kernel32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0
