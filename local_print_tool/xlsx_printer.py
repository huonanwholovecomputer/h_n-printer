# -*- coding: utf-8 -*-
"""
xlsx_printer.py — 表格文件（.xls / .xlsx）→ PDF：**让 Excel/WPS 自己排版**

用户的工作流不是"在工具里再排版一次"，而是先在 Excel 里设好「边距 0.5cm / 页眉页脚 0 /
水平居中 / 调整所有列到一页」再按 Ctrl+P。所以本工具**必须完全复现 Excel 自己的打印结果**，
唯一可靠路径就是 Excel/WPS COM 的 `ExportAsFixedFormat`：缩放/页边距/打印区域/分页全由
Excel 自己算，代码**不得**自行改写页边距、缩放比例、打印区域（一改就与用户看到的预览不一致）。

引擎优先级：Excel → WPS(表格) → LibreOffice 兜底。兜底会**明写日志**「版式可能与 Excel
不一致」—— LibreOffice 不认 Excel 的全部排版语义（分页/窄边距/缩放常有偏差），它只是
"装不上 Excel 时别让用户白跑一趟"的最后一档。

导出粒度 = **活动工作表**（Excel 打印对话框的默认范围「打印活动工作表」）：
`Workbook.ExportAsFixedFormat` 会导出**全部**工作表，实测一个两工作表的工作簿导出 17 页，
而用户手里的参照 PDF 只有 5 页（正是活动工作表那一张）⇒ 一律导 `wb.ActiveSheet`。
日志里写清导出的 sheet 名与工作簿里一共几个 sheet，让人能核对"为什么只打了这几页"。

缓存身份：同一个 xlsx 用 Excel 与用 LibreOffice 导出的版式不同，**不能混用缓存**。
key 形态 `{md5}[_方向]_x{engine}`（`_xexcel` / `_xwps` / `_xlo`），读侧按引擎优先级探测
**当前仍可用的**引擎产物（Excel 卸载后就跳过 `_xexcel` 旧的，重新用 WPS 转）。

安全（S1-04 同一口径）：
  · 本地文件：Excel/WPS COM，实例用 `DispatchEx` 私有进程 + 打开前硬化
    （Visible=False / DisplayAlerts=False / AutomationSecurity=3 禁宏 / EnableEvents=False /
    AskToUpdateLinks=False），工作簿 ReadOnly=True 打开、Close(SaveChanges=False)，
    **绝不修改用户源文件**；
  · 云端顾客文件：不给 COM 机会（宏/外链），直接 LibreOffice —— 与 Word/PDF 同一条线；
  · 只退出**我们自己启动**的实例（`owned=True` 才 Quit）。默认恒 DispatchEx 私有实例；
    走 `GetActiveObject` 复用用户已开的 Excel 只在显式打开 `HN_XLSX_REUSE_EXCEL=1` 时发生，
    那时 `owned=False`，**永不 Quit**（绝不关用户正在编辑的 Excel）。

只有在 Excel/WPS 都装不上、又没装 LibreOffice 时才失败；错误信息写清缺什么。
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 本模块负责的扩展名（.xls = BIFF 老格式，同样只能交给 Excel/WPS/LO）
XLSX_EXTS = (".xls", ".xlsx")

# 引擎顺序 = 与 Excel 自己打印的一致性排序。LibreOffice 只是兜底。
ENGINE_ORDER = ("excel", "wps", "libreoffice")

ENGINE_LABELS = {
    "excel": "Microsoft Excel",
    "wps": "WPS 表格",
    "libreoffice": "LibreOffice",
}

# PDF 缓存 key 里的引擎身份后缀（不同引擎版式不同，绝不能共用一份缓存）
CACHE_TAGS = {"excel": "excel", "wps": "wps", "libreoffice": "lo"}

# 各引擎的 COM ProgID（Excel 表格 / WPS 表格；WPS 的 ET.Application 老版本，本机没有）
_PROGIDS = {"excel": "Excel.Application", "wps": "KET.Application"}

# 复用用户已开实例的开关（默认关闭：见模块 docstring 的 2026-10-24 事故）
_REUSE_ENV = "HN_XLSX_REUSE_EXCEL"

# Quit() 之后等进程真正退出多久（Excel 的关闭是异步的；实测正常 <1s，等不到就强制收尾）
_EXIT_WAIT_S = 3.0

# 引擎对应的进程映像名（强制收尾前的保险：PID 会被复用，只认这些名字）
_APP_IMAGES = {
    "excel": ("excel.exe",),
    "wps": ("wps.exe", "et.exe", "ket.exe"),
    "libreoffice": ("soffice.exe", "soffice.bin"),
}

# 引擎可用性探测缓存（只查注册表/文件系统，**不启动 COM**）
_probe_cache: dict[str, bool | None] = {"excel": None, "wps": None, "libreoffice": None}
_probe_lock = threading.Lock()

# xlCalculationManual 之类不碰：公式必须照 Excel 打开时的口径算好再导出


class XlsxExportError(RuntimeError):
    """表格导出失败（密码保护 / 文件损坏 / 引擎全不可用）。信息面向使用者。"""


@dataclass(frozen=True)
class ExportResult:
    """一次表格导出的结果。"""
    engine: str                 # excel | wps | libreoffice
    pdf_path: str
    sheet: str = ""             # 实际导出的工作表名（活动工作表）
    sheet_count: int = 0        # 工作簿里一共有几个工作表（>1 时日志会提示只打了活动表）
    fallback: bool = False      # 是否走了兜底引擎（LibreOffice）
    hardened: dict = field(default_factory=dict)   # 逐项硬化结果（没有 COM 时为空）
    elapsed: float = 0.0

    @property
    def engine_label(self) -> str:
        return engine_label(self.engine)

    @property
    def cache_tag(self) -> str:
        return CACHE_TAGS.get(self.engine, "")


# ============================================================
# 小工具
# ============================================================

def _engine_probe_disabled() -> bool:
    """`HN_NO_ENGINE_PROBE=1` ⇒ 探测一律"不可用"（与 converter 同一道硬闸门）。

    测试/CI 用它保证任何脚本都不会启动或附着 Office。converter 里已有同名实现，
    这里不 import 它（避免 import 环），行为保持逐字一致。
    """
    return (os.environ.get("HN_NO_ENGINE_PROBE") or "").strip().lower() in (
        "1", "true", "yes", "on")


def ext_of(path_or_ext: str) -> str:
    """取扩展名（小写含点）；也接受裸扩展名（".xlsx"）。"""
    s = str(path_or_ext or "")
    if s.startswith(".") and s.count(".") == 1:
        return s.lower()
    return os.path.splitext(s)[1].lower()


def is_xlsx(path_or_ext: str) -> bool:
    """是不是本模块负责的表格文件。"""
    return ext_of(path_or_ext) in XLSX_EXTS


def engine_label(engine: str) -> str:
    return ENGINE_LABELS.get(str(engine or ""), str(engine or ""))


def progid_available(prog_id: str, use_cache: bool = True) -> bool:
    """只查注册表判断 COM ProgID 是否注册（不启动 COM，毫秒级）。"""
    global _probe_cache
    key = str(prog_id or "")
    if not key:
        return False
    if _engine_probe_disabled():
        return False
    with _probe_lock:
        if use_cache and key in _probe_cache and _probe_cache[key] is not None:
            return bool(_probe_cache[key])
    ok = False
    try:
        import winreg  # noqa: PLC0415
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, key + r"\CLSID") as k:
            clsid, _ = winreg.QueryValueEx(k, "")
        if clsid:
            # CLSID 存在还不够：真正的服务器注册在 CLSID\{...}\LocalServer32
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                                    r"CLSID\%s\LocalServer32" % clsid):
                    pass
                ok = True
            except OSError:
                ok = True   # 至少 ProgID→CLSID 是通的（部分版本用 InprocServer32）
    except OSError:
        ok = False
    except Exception:  # noqa: BLE001 —— 非 Windows / 权限问题一律当不可用
        ok = False
    with _probe_lock:
        _probe_cache[key] = ok
    return ok


def engine_available(engine: str) -> bool:
    """某引擎当前是否可用（Excel/WPS 查注册表；LibreOffice 查可执行文件）。"""
    eng = str(engine or "")
    if eng in _PROGIDS:
        return progid_available(_PROGIDS[eng])
    if eng == "libreoffice":
        with _probe_lock:
            if _probe_cache.get("libreoffice") is not None:
                return bool(_probe_cache["libreoffice"])
        ok = False
        if not _engine_probe_disabled():
            try:
                import converter  # noqa: PLC0415 —— 延迟 import，避免与 converter 形成环
                ok = converter._find_libreoffice() is not None
            except Exception:  # noqa: BLE001
                ok = False
        with _probe_lock:
            _probe_cache["libreoffice"] = ok
        return ok
    return False


def live_engines() -> list[str]:
    """当前可用引擎，按优先级排序（用于缓存探测与降级链）。"""
    return [e for e in ENGINE_ORDER if engine_available(e)]


def reset_probe_cache() -> None:
    """清空引擎探测缓存（测试/装完 Office 后重新探测用）。"""
    with _probe_lock:
        for k in _probe_cache:
            _probe_cache[k] = None


# ============================================================
# PDF 缓存 key（含引擎身份）
# ============================================================

def cache_key(source_md5: str, image_orientation: str = "", engine: str = "") -> str:
    """表格文件的 PDF 缓存 key：`{md5}[_方向]_x{引擎}`。

    方向后缀规则与 `cloud_client.pdf_cache_key` **同一真相**（延迟 import，取不到时按
    同一条规则本地实现）；引擎后缀用表格专用的 `_x…`，不与 docx 的 `_e…` 混用，
    也就不会被 `_purge_stale_engine_entries`（只清非 docx 的 `_eword/_ewps`）误删。
    """
    md5 = str(source_md5 or "")
    if not md5:
        return ""
    try:
        from cloud_client import pdf_cache_key  # noqa: PLC0415
        base = pdf_cache_key(md5, image_orientation)
    except Exception:  # noqa: BLE001
        base = md5
        if image_orientation in ("landscape", "portrait"):
            base = f"{base}_{image_orientation}"
    tag = CACHE_TAGS.get(str(engine or ""), "")
    return f"{base}_x{tag}" if tag else base


def find_cached_pdf(source_md5: str, image_orientation: str = "",
                    cache_dir: str = "") -> str | None:
    """按引擎优先级找一份**当前仍可用引擎**产出的缓存 PDF；没有则 None。

    只探测可用引擎：Excel 卸载后 `_xexcel` 的旧产物不能再用（版式与现有引擎不一致），
    直接跳过并让上层重新转换。文件头必须是 %PDF（半截/损坏的缓存视为未命中）。
    """
    if not source_md5:
        return None
    cdir = cache_dir
    if not cdir:
        try:
            import paths as _paths  # noqa: PLC0415
            cdir = _paths.pdf_cache_dir()
        except Exception:  # noqa: BLE001
            return None
    for eng in live_engines():
        path = os.path.join(cdir, cache_key(source_md5, image_orientation, eng) + ".pdf")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as f:
                if not f.read(5).startswith(b"%PDF"):
                    logger.warning(f"表格缓存损坏（非 PDF 头），忽略: {path}")
                    continue
        except OSError:
            continue
        return path
    return None


def upsert_cache_index(cache_dir: str, key: str, meta: dict) -> None:
    """把一条索引写进 `pdf_cache/index.json`（原子写）。

    为什么必须写：`retention.sweep_pdf_cache` 会把"磁盘上有、索引里没有"的 .pdf 当孤儿删掉。
    按引擎身份命名的缓存文件如果不在索引里，下次启动就被清空 ⇒ 每单都重转（Excel 一单 2 秒）。
    """
    if not cache_dir or not key:
        return
    idx_path = os.path.join(cache_dir, "index.json")
    idx: dict = {}
    if os.path.isfile(idx_path):
        try:
            import json as _json  # noqa: PLC0415
            with open(idx_path, "r", encoding="utf-8") as f:
                loaded = _json.load(f)
            if isinstance(loaded, dict):
                idx = loaded
        except Exception as e:  # noqa: BLE001
            logger.warning(f"读取 pdf_cache 索引失败（按空索引继续）: {e}")
            idx = {}
    idx[key] = dict(meta or {})
    try:
        import json as _json  # noqa: PLC0415
        os.makedirs(cache_dir, exist_ok=True)
        tmp = idx_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(idx, f, ensure_ascii=False, indent=2)
        os.replace(tmp, idx_path)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"写入 pdf_cache 索引失败: {e}")


# ============================================================
# 文件预检（密码保护 / 损坏 / 受保护视图）
# ============================================================

def _strip_motw(path: str) -> bool:
    """删掉临时副本上的「来自 Internet」标记（Zone.Identifier ADS）。

    云端/下载来的表格带 MOTW ⇒ Excel 会把它放进**受保护视图**打开，COM 侧看不到文档
    （`Workbooks.Open` 拿不到可导出的 Workbook，或导出空文件）。我们操作的永远是自己的
    临时副本，剥掉标记不改变任何内容，只是不让 Excel 把我们的副本关进沙箱。
    """
    if os.name != "nt":
        return False
    try:
        ok = bool(ctypes.windll.kernel32.DeleteFileW(f"{path}:Zone.Identifier"))
        if ok:
            logger.info("已清除临时副本的「来自 Internet」标记（避免受保护视图）")
        return ok
    except Exception as e:  # noqa: BLE001
        logger.debug(f"清除 Zone.Identifier 失败（可忽略）: {e}")
        return False


def _read_head(path: str, size: int = 65536) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(size)
    except OSError:
        return b""


def validate_xlsx(file_path: str) -> None:
    """转换前预检：**明确**的失败信息，而不是让 Excel 弹一个看不见的对话框挂死。

    覆盖三种真实情况：
      · 密码保护（Excel「用密码进行加密」）→ OOXML 被包成 OLE 复合文档，内含
        `EncryptedPackage` 流；COM 打开会索要密码，而 DisplayAlerts=False 下没人能输密码；
      · 文件损坏 / 根本不是表格 → xlsx 的 zip 结构或 `xl/workbook.xml` 不成立；
      · 0 字节空文件（下载半途、U 盘拔出）。
    """
    name = os.path.basename(file_path)
    try:
        size = os.path.getsize(file_path)
    except OSError as e:
        raise XlsxExportError(f"表格文件读不到：{name}（{e}）") from e
    if size == 0:
        raise XlsxExportError(f"表格文件是空的（0 字节），无法打印：{name}")

    head = _read_head(file_path)
    if b"EncryptedPackage" in head or "EncryptedPackage".encode("utf-16-le") in head:
        raise XlsxExportError(
            f"表格文件已加密（密码保护），无法打印：{name}\n"
            "请用 Excel 打开 → 文件 → 信息 → 保护工作簿 → 用密码进行加密 → 清空密码并保存后重试。")

    if head[:2] == b"PK":   # OOXML（xlsx）
        try:
            with zipfile.ZipFile(file_path) as zf:
                names = set(zf.namelist())
        except zipfile.BadZipFile as e:
            raise XlsxExportError(
                f"表格文件已损坏（zip 结构无法解析），请用 Excel 另存为新文件后重试：{name}（{e}）") from e
        except OSError as e:
            raise XlsxExportError(f"表格文件读不到：{name}（{e}）") from e
        if "xl/workbook.xml" not in names:
            raise XlsxExportError(
                f"这不是有效的 xlsx 工作簿（缺 xl/workbook.xml），可能是改了后缀的文件：{name}")
        return

    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":   # OLE 复合文档
        # .xls 老格式正常就该是 OLE；.xlsx 却是 OLE ⇒ 多半是"改了后缀的 xls"或加密包
        # （加密包上面已按 EncryptedPackage 拦掉）。交给 Excel 自己判断即可。
        return

    raise XlsxExportError(
        f"这不是有效的表格文件（既不是 xlsx 的 zip 结构，也不是 xls 的 OLE 结构）：{name}")


# ============================================================
# COM：实例 / 硬化 / 导出
# ============================================================

def _attach_existing(prog_id: str):
    """附着用户**已经打开**的实例（只在 HN_XLSX_REUSE_EXCEL=1 时用）。失败返回 None。"""
    try:
        import win32com.client  # noqa: PLC0415
        return win32com.client.GetActiveObject(prog_id)
    except Exception as e:  # noqa: BLE001
        logger.info(f"没有可复用的已开实例（{prog_id}）: {e}")
        return None


def _reuse_existing_enabled() -> bool:
    return (os.environ.get(_REUSE_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def _pid_of_app(app) -> int:
    """拿 Excel/WPS 实例自己的进程号（`app.Hwnd` 在 Visible=False 时仍然有效）。

    拿不到就返回 0 —— 收尾逻辑会退化成"只 Quit，不确认"，绝不猜一个 PID 去 kill。
    """
    try:
        import win32process  # noqa: PLC0415
        hwnd = int(app.Hwnd)
        if not hwnd:
            return 0
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        return int(pid)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"取 COM 实例 PID 失败（不影响导出）: {e}")
        return 0


def _pid_image_name(pid: int) -> str:
    """进程映像文件名（小写，如 `excel.exe`）；取不到返回 ""。

    强制收尾前的最后一道保险：PID 会被系统复用，只有"还活着 + 进程名仍是 Excel/WPS"
    才能认定那是我们启动的那个实例。
    """
    if pid <= 0:
        return ""
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(len(buf))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return ""


def _pid_alive(pid: int) -> bool:
    """进程是否还在（不依赖 psutil；进程不存在/无权访问都按"已退出"处理）。"""
    if pid <= 0:
        return False
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return False


def _terminate_pid(pid: int) -> bool:
    """强制结束**我们确认是自己启动的**那个进程（只在 Quit 后仍存活时用）。"""
    if pid <= 0:
        return False
    _PROCESS_TERMINATE = 0x0001
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_PROCESS_TERMINATE, False, int(pid))
        if not handle:
            return False
        try:
            return bool(kernel32.TerminateProcess(handle, 1))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return False


def _wait_pid_exit(pid: int, timeout: float = _EXIT_WAIT_S) -> bool:
    """等进程退出：Excel 的 Quit 是异步的（实测要 1~3s），不做这步就会误判"残留"。"""
    if pid <= 0:
        return True
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    return not _pid_alive(pid)


def _dispatch_app(engine: str) -> tuple[object, bool]:
    """取一个 COM Application 实例，返回 `(app, owned)`。

    `owned=True` 才允许 Quit。默认恒用 converter 的 `_dispatch_ex`（DispatchEx **私有**
    进程）：2026-10-24 的事故就是 `Dispatch` 附着了用户正在编辑的 Word、随后 Quit 把它关掉。
    复用已开实例必须显式开 `HN_XLSX_REUSE_EXCEL=1`，那时 owned=False ⇒ 我们**永不 Quit**，
    也绝不 Close 用户的其它工作簿。
    """
    prog_id = _PROGIDS[engine]
    if _reuse_existing_enabled():
        app = _attach_existing(prog_id)
        if app is not None:
            logger.info(f"表格引擎: 复用用户已打开的 {prog_id}（owned=False，绝不 Quit）")
            return app, False
    import converter  # noqa: PLC0415
    return converter._dispatch_ex(prog_id), True


def _harden_excel(app, label: str) -> dict[str, bool]:
    """打开工作簿**之前**收紧实例：不弹窗、不执行宏、不更新外链、不重算导致卡顿。

    逐项 try + 日志（属性在某个 Excel/WPS 版本上不存在时不谎报成功）。取值口径与
    `converter._harden_office_com_app` 一致，另加 Excel 专有的 EnableEvents / Interactive /
    AskToUpdateLinks：
      · EnableEvents=False —— 工作簿里的 Workbook_Open 类事件不再回调（VBA 已在
        AutomationSecurity=3 下被禁用，这里再堵一层 COM 事件）；
      · Interactive=False —— 不允许任何用户交互（模态框直接失败而不是挂住）；
      · AskToUpdateLinks=False —— 打开时不问"是否更新链接"（外链不出网）。
    """
    applied: dict[str, bool] = {}
    settings = (
        ("Visible=False", "Visible", False),
        ("DisplayAlerts=False", "DisplayAlerts", False),
        ("EnableEvents=False", "EnableEvents", False),
        ("Interactive=False", "Interactive", False),
        ("AskToUpdateLinks=False", "AskToUpdateLinks", False),
        ("AutomationSecurity=3", "AutomationSecurity", 3),   # 3 = 禁用所有宏，不提示
    )
    for name, attr, value in settings:
        try:
            setattr(app, attr, value)
            applied[name] = True
        except Exception as e:  # noqa: BLE001
            applied[name] = False
            logger.warning(f"{label}: {name} 未生效（该版本 COM 无此属性）: {e}")
    if all(applied.values()):
        logger.info(f"{label} COM 安全设置已生效: {', '.join(applied)}")
    else:
        logger.info(f"{label} COM 安全设置（部分属性该版本不支持）: "
                    + ", ".join(f"{k}={'✓' if v else '✗'}" for k, v in applied.items()))
    return applied


def _open_workbook(app, abs_src: str):
    """以只读方式打开工作簿（绝不修改源文件；不让 Excel 索要密码/弹只读建议）。"""
    wb = None
    try:
        wb = app.Workbooks.Open(
            abs_src,
            UpdateLinks=0,               # 0 = 不更新任何外部链接（打开动作本身不出网）
            ReadOnly=True,               # 只读：源文件一个字节都不会被改
            IgnoreReadOnlyRecommended=True,
            Notify=False,                # 已被占用时不要"等对方释放"的通知（会挂住）
            Password="",                 # 空密码：加密文件立刻失败，而不是弹密码框
        )
    except TypeError:
        # 某些 WPS 版本的 Workbooks.Open 不接受这些关键字 ⇒ 退回最小参数集，
        # 只保留「不更新链接 + 只读」这两条硬要求。
        wb = app.Workbooks.Open(abs_src, 0, True)
    return wb


def _export_worksheet_pdf(ws, abs_out: str) -> None:
    """把**一个工作表**导出成 PDF（Excel 打印对话框默认范围「打印活动工作表」）。

    `Excel.Worksheet.ExportAsFixedFormat(Type, Filename, Quality, IncludeDocProperties,
    IgnorePrintAreas, From, To, OpenAfterPublish, FixedFormatExtClassPtr)`：
      · Type=0        = xlTypePDF
      · Quality=0     = 标准质量（不降分辨率）
      · IgnorePrintAreas=False —— **尊重用户在 Excel 里设的打印区域**（这是"复现 Excel
        打印结果"的一部分，代码不得覆盖）
      · OpenAfterPublish=False —— 不要弹 PDF 阅读器
      · From/To 不传 —— 不擅自把"全部页"改成某个区间，页范围由用户的页面设置决定
    """
    try:
        ws.ExportAsFixedFormat(
            Type=0, Filename=abs_out, Quality=0,
            IncludeDocProperties=True, IgnorePrintAreas=False, OpenAfterPublish=False)
    except TypeError:
        # 老 WPS 不认关键字参数：位置参数顺序同上（From/To 省略）
        ws.ExportAsFixedFormat(0, abs_out, 0, True, False)


def _export_with_com(engine: str, abs_src: str, abs_out: str) -> ExportResult:
    """用 Excel / WPS 的 COM 导出活动工作表。失败抛异常（由调用方决定是否降级）。"""
    import pythoncom  # noqa: PLC0415

    label = engine_label(engine)
    t0 = time.time()
    pythoncom.CoInitialize()   # P1 口径：与 converter 一致，finally 无条件配对反初始化
    app = None
    owned = False
    pid = 0
    wb = None
    applied: dict[str, bool] = {}
    try:
        app, owned = _dispatch_app(engine)
        pid = _pid_of_app(app)
        applied = _harden_excel(app, label)

        if os.path.exists(abs_out):
            try:
                os.remove(abs_out)
            except OSError:
                pass

        wb = _open_workbook(app, abs_src)
        try:
            sheet_count = int(wb.Sheets.Count)
        except Exception:  # noqa: BLE001
            sheet_count = 0
        ws = wb.ActiveSheet
        sheet_name = ""
        try:
            sheet_name = str(ws.Name)
        except Exception:  # noqa: BLE001
            sheet_name = ""

        _export_worksheet_pdf(ws, abs_out)

        if not os.path.isfile(abs_out) or os.path.getsize(abs_out) <= 0:
            raise XlsxExportError(f"{label} 报告成功但没有生成 PDF: {os.path.basename(abs_src)}")
        if sheet_count > 1:
            logger.warning(
                f"{label} 导出的是活动工作表「{sheet_name}」（该工作簿共 {sheet_count} 个工作表）。"
                f"Excel 默认打印范围就是活动工作表；若用户要打其它表，请在 Excel 里切到那张表另存。"
                f" ({os.path.basename(abs_src)})")
        logger.info(
            f"{label} → PDF 完成: sheet=「{sheet_name}」({sheet_count} 个工作表)，"
            f"耗时 {time.time() - t0:.2f}s")
        return ExportResult(engine=engine, pdf_path=abs_out, sheet=sheet_name,
                            sheet_count=sheet_count,
                            fallback=(engine == "libreoffice"),
                            hardened=applied, elapsed=time.time() - t0)
    finally:
        if wb is not None:
            try:
                wb.Close(SaveChanges=False)   # 绝不把任何改动写回用户文件
            except Exception as e:  # noqa: BLE001
                logger.debug(f"{label}: 关闭工作簿失败（忽略）: {e}")
        if app is not None and owned:
            # 只退我们自己启动的实例；复用的用户实例 owned=False ⇒ 绝不 Quit
            try:
                app.Quit()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"{label}: Quit 失败（忽略）: {e}")
            # Quit 是异步的；也见过 Quit 抛错/被 Excel 忽略而留下僵尸进程的情形
            # （10:28 与 10:31 两次验收各漏 1 个 EXCEL.EXE /automation -Embedding）。
            # 这里只针对**我们自己刚启动的那个 PID** 做确认与收尾，绝不碰用户已开的 Excel。
            if not _wait_pid_exit(pid):
                _img = _pid_image_name(pid)
                if _img in _APP_IMAGES.get(engine, ()):
                    logger.warning(
                        f"{label} 实例退出超时（PID {pid} / {_img}），强制结束我们自己启动的实例"
                        f"（Quit 未被 Excel 执行）")
                    if not _terminate_pid(pid) or not _wait_pid_exit(pid, timeout=3.0):
                        logger.warning(f"{label} 实例仍存活（PID {pid}），请手工检查任务管理器")
                else:
                    logger.warning(
                        f"{label} 实例退出超时（PID {pid} / {_img or '进程名未知'}），"
                        f"进程名对不上，不做强制结束，请手工检查任务管理器")
        elif app is not None and not owned:
            logger.info(f"表格引擎: 复用的用户实例不做 Quit（owned=False）")
        try:
            import converter  # noqa: PLC0415
            converter._com_uninitialize_quietly()
        except Exception:  # noqa: BLE001
            pass


def _export_with_libreoffice(abs_src: str, abs_out: str) -> ExportResult:
    """LibreOffice 兜底：**版式可能与 Excel 不一致**（日志里明写，别让人以为是同一结果）。"""
    logger.warning(
        "LibreOffice 兜底，版式可能与 Excel 不一致（缩放/页边距/分页的解释不同；"
        "并且 LibreOffice 导出的是全部工作表，Excel 默认只打活动工作表）。"
        f" ({os.path.basename(abs_src)})")
    import converter  # noqa: PLC0415
    converter._convert_office_via_libreoffice(abs_src, abs_out)
    if not os.path.isfile(abs_out) or os.path.getsize(abs_out) <= 0:
        raise XlsxExportError(f"LibreOffice 没有生成 PDF: {os.path.basename(abs_src)}")
    return ExportResult(engine="libreoffice", pdf_path=abs_out, fallback=True)


# ============================================================
# 对外唯一入口
# ============================================================

def export_to_pdf(file_path: str, output_pdf: str | None = None, *,
                  from_cloud: bool = False, allow_cloud_com: bool = False,
                  prefer_engine: str = "") -> ExportResult:
    """表格文件 → PDF（Excel 自己排版）。

    Args:
        file_path: 源文件（.xls/.xlsx）。**只读**打开，绝不修改它。
        output_pdf: 输出路径；None ⇒ 临时文件（调用方负责删除）。
        from_cloud: 文件来自云端（顾客上传）。True 且未放行 ⇒ **只用 LibreOffice**
            （不给 COM 任何宏/外链执行机会，与 Word/PDF 同一条 S1-04 口径）。
        allow_cloud_com: 「管理员上传」放行位（口径见 printer_config.cloud_word_com_allowed）。
        prefer_engine: 指定首选引擎（"excel"/"wps"/"libreoffice"），其余仍按优先级降级。

    Returns:
        ExportResult（engine / pdf_path / sheet / sheet_count / fallback / hardened / elapsed）

    Raises:
        XlsxExportError: 预检失败（加密/损坏/空文件）或所有引擎都失败。
    """
    if not os.path.isfile(file_path):
        raise XlsxExportError(f"文件不存在: {file_path}")
    if not is_xlsx(file_path):
        raise XlsxExportError(f"不是表格文件（xls/xlsx）: {file_path}")

    created_temp = False
    out = output_pdf
    if not out:
        fd, out = tempfile.mkstemp(suffix=".pdf", prefix="_xlsx_")
        os.close(fd)
        created_temp = True
    out = os.path.abspath(out)

    validate_xlsx(file_path)

    # 工作副本：COM 打开期间不加锁用户文件；同时剥掉 MOTW（受保护视图）
    temp_input = ""
    try:
        ext = os.path.splitext(file_path)[1]
        fd, temp_input = tempfile.mkstemp(suffix=ext, prefix="_xlsx_src_")
        os.close(fd)
        shutil.copy2(file_path, temp_input)
        _strip_motw(temp_input)
        src = temp_input

        cloud_locked = bool(from_cloud) and not allow_cloud_com
        if cloud_locked:
            order = ["libreoffice"]
            logger.info("引擎选择: LibreOffice（云端来源强制，跳过 Excel/WPS COM 以杜绝宏执行）"
                        f" ({os.path.basename(file_path)})")
        else:
            order = list(ENGINE_ORDER)
            if str(prefer_engine or "") in order:
                order = [prefer_engine] + [e for e in order if e != prefer_engine]

        errors: list[str] = []
        for eng in order:
            if not engine_available(eng):
                logger.info(f"表格引擎 {engine_label(eng)} 不可用，跳过")
                continue
            try:
                if eng == "libreoffice":
                    return _export_with_libreoffice(src, out)
                logger.info(f"引擎选择: {engine_label(eng)}（原生导出版式，与 Excel 打印一致）"
                            f" ({os.path.basename(file_path)})")
                res = _export_with_com(eng, src, out)
                _verify_product(out)
                return res
            except Exception as e:  # noqa: BLE001 —— 逐级降级，最后一档才报错
                errors.append(f"{engine_label(eng)}: {e}")
                logger.warning(f"{engine_label(eng)} 导出失败，降级到下一引擎: {e}")
                if os.path.isfile(out):
                    try:
                        os.remove(out)
                    except OSError:
                        pass

        if not errors:
            raise XlsxExportError(
                "没有可用的表格转换引擎：Microsoft Excel / WPS / LibreOffice 都没找到。\n"
                "请安装 Microsoft Excel 或 WPS Office（推荐，版式与用户预览一致），"
                "或安装 LibreOffice 作为兜底。")
        raise XlsxExportError("表格转 PDF 失败（已依次尝试 Excel / WPS / LibreOffice）：\n"
                              + "\n".join(f"  · {e}" for e in errors))
    finally:
        if temp_input and os.path.isfile(temp_input):
            try:
                os.remove(temp_input)
            except OSError:
                pass
        if created_temp and out and os.path.isfile(out):
            # 失败路径：调用方拿不到 result，删掉半成品
            try:
                if not _looks_like_pdf(out):
                    os.remove(out)
            except OSError:
                pass


def _looks_like_pdf(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(5).startswith(b"%PDF")
    except OSError:
        return False


def _verify_product(path: str) -> None:
    """产物校验：存在 + 非空 + %PDF 头（与 converter._pdf_product_ok 同一口径）。"""
    if not _looks_like_pdf(path):
        raise XlsxExportError(f"导出的 PDF 无效（空 / 不存在 / 非 PDF）: {path}")


def export_and_cache_tag(file_path: str, output_pdf: str | None = None, *,
                         from_cloud: bool = False, allow_cloud_com: bool = False,
                         prefer_engine: str = "") -> tuple[str, ExportResult]:
    """`export_to_pdf` 的便捷形式：返回 `(pdf_path, result)`（省得调用方取 .pdf_path）。"""
    res = export_to_pdf(file_path, output_pdf, from_cloud=from_cloud,
                        allow_cloud_com=allow_cloud_com, prefer_engine=prefer_engine)
    return res.pdf_path, res
