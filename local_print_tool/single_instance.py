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

# ===== S1-10（DS-08 余项）：命名对象从 Local\ 改成 Global\ + 用户 SID =====
# 为什么：`Local\` 前缀的对象**只在当前登录会话内可见**。同一台机器同时存在两个会话
# （控制台 + 远程桌面，或快速用户切换后残留的会话）时，两个实例互相看不见 ⇒ 同时运行、
# 互相抢 print_config.json / 攒批清单（duplex_stack）/ pdf_cache —— 这正是要修的洞。
#
# 为什么还要带用户 SID：`Global\` 跨会话、跨用户可见。数据目录 `%APPDATA%` 本来就是
# 按用户隔离的，A 用户跑着不该挡住 B 用户，所以名字里带"当前用户 SID"，语义是
# **同一用户的所有会话共用一个锁**。
#
# 为什么保留 Local\ 回退：创建 `Global\` 对象需要 SeCreateGlobalPrivilege（默认只给
# 管理员/服务账号）。普通用户门店机上创建会失败 —— 那种情况必须退回 `Local\`，
# 否则单实例保护会被静默关掉（原来的 `if not h: return True` 就是"失败即放行"）。
_MUTEX_NAMES = []
_EVENT_NAMES = []


def _user_sid_suffix() -> str:
    """当前进程用户 SID 的字符串形式；取不到时退回 用户名@域 的短哈希。"""
    try:
        import hashlib
        import os

        advapi32 = ctypes.windll.advapi32
        TOKEN_QUERY, TokenUser = 0x0008, 1
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.HANDLE)]
        advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 ctypes.c_void_p, wintypes.DWORD,
                                                 ctypes.POINTER(wintypes.DWORD)]
        advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                                    ctypes.POINTER(wintypes.LPWSTR)]
        htoken = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), TOKEN_QUERY,
                                         ctypes.byref(htoken)):
            raise OSError("OpenProcessToken failed")
        try:
            need = wintypes.DWORD(0)
            advapi32.GetTokenInformation(htoken, TokenUser, None, 0, ctypes.byref(need))
            buf = ctypes.create_string_buffer(max(int(need.value), 64))
            if not advapi32.GetTokenInformation(htoken, TokenUser, buf, need,
                                                ctypes.byref(need)):
                raise OSError("GetTokenInformation failed")
            sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            str_sid = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(str_sid)):
                raise OSError("ConvertSidToStringSidW failed")
            sid = str_sid.value or ""
            _kernel32.LocalFree(str_sid) if hasattr(_kernel32, "LocalFree") else None
            if sid:
                return sid.replace("-", "")[-16:]
        finally:
            _kernel32.CloseHandle(htoken)
    except Exception:
        pass
    # 回退：用户名@域 的短哈希（仍然按用户隔离，只是理论碰撞面大一点）
    import hashlib
    import os
    ident = "%s@%s" % (os.environ.get("USERNAME", ""), os.environ.get("USERDOMAIN", ""))
    return "u" + hashlib.sha256(ident.encode("utf-8", "replace")).hexdigest()[:12]


_SID_SUFFIX = _user_sid_suffix()
for _ns in ("Global", "Local"):
    _MUTEX_NAMES.append("%s\\HN_Print_Tool_SingleInstance_Mutex_%s" % (_ns, _SID_SUFFIX))
    _EVENT_NAMES.append("%s\\HN_Print_Tool_SingleInstance_Event_%s" % (_ns, _SID_SUFFIX))

# 兼容旧名（下面 acquire 里会先按新名判定；旧名只用于"升级瞬间"的过渡：
# 旧版本进程持的是 Local\HN_Print_Tool_SingleInstance_Mutex，不检查它就可能在升级后
# 出现"新旧两个实例同时跑"，所以一并尝试打开）
_LEGACY_MUTEX_NAME = "Local\\HN_Print_Tool_SingleInstance_Mutex"
_LEGACY_EVENT_NAME = "Local\\HN_Print_Tool_SingleInstance_Event"

# 当前实际生效的命名（acquire 成功后写入；供测试与日志用）
_MUTEX_NAME = ""
_EVENT_NAME = ""

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


def acquire(retries: int = 3, retry_delay: float = 0.15) -> bool:
    """获取单实例锁。

    retries/retry_delay：检测到已有实例时的短重试（默认 3×0.15s≈0.45s）。
    重试仅用于防极端竞态（自动重启的看门狗已保证旧进程 PID 消失后才启动新实例，
    互斥体随进程退出由内核自动释放，正常情况首次 acquire 即成功）；
    若旧实例正常运行，0.45s 内即判定"已在运行"→ 通知既有实例置前并退出，
    避免用户双击第二个实例时长时间无响应。

    Returns:
        True  = 本实例是唯一实例（互斥体已持有，继续启动）
        False = 已有实例在运行（已通知其将窗口置于前台），调用方应立即退出
    """
    global _mutex_handle, _front_event, _MUTEX_NAME, _EVENT_NAME
    # 先试 Global\（跨会话），失败再退 Local\（普通用户没有 SeCreateGlobalPrivilege）
    for ns_idx, mutex_name in enumerate(_MUTEX_NAMES):
        event_name = _EVENT_NAMES[ns_idx]
        for attempt in range(retries + 1):
            h = _kernel32.CreateMutexW(None, False, mutex_name)
            if not h:
                # 该命名空间不可用（权限/系统异常）→ 换下一个命名空间；
                # 全部不可用才放行（避免把用户锁在门外）
                break
            if _kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                _kernel32.CloseHandle(h)
                # 升级过渡：旧版本进程可能仍持有不带 SID 后缀的 Local\ 锁
                legacy = _kernel32.CreateMutexW(None, False, _LEGACY_MUTEX_NAME)
                if legacy and _kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                    _kernel32.CloseHandle(legacy)
                    if attempt < retries:
                        time.sleep(retry_delay)
                        continue
                    _notify_front()
                    return False
                if legacy:
                    _kernel32.CloseHandle(legacy)
                if attempt < retries:
                    time.sleep(retry_delay)
                    continue
                _notify_front()
                return False
            # 第一个实例：持有互斥体句柄，保持锁
            _mutex_handle = h
            _MUTEX_NAME, _EVENT_NAME = mutex_name, event_name
            # 创建置前通知事件（自动复位）；若已存在（上次实例残留）则复用，无碍
            _front_event = _kernel32.CreateEventW(None, False, False, event_name)
            return True
    # 所有命名空间都创建失败（权限/系统异常）→ 放行，避免把用户锁在门外
    return True


def _notify_front() -> None:
    """通知既有实例把窗口置于前台。

    事件可能恰逢首个实例刚创建互斥体、尚未创建事件（极短窗口），短时重试兜底。
    """
    _event_names = ([_EVENT_NAME] if _EVENT_NAME else []) + _EVENT_NAMES + [_LEGACY_EVENT_NAME]
    for _ in range(10):
        evt = None
        for _en in _event_names:
            evt = _kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, _en)
            if evt:
                break
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
