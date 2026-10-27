"""
converter.py — 通用文件转换器 UniversalConverter
将所有可打印文件格式统一转换为 PDF，支持:
  - TXT / CSV  → reportlab
  - 图片 (JPG/PNG/BMP/GIF/WEBP/TIFF) → reportlab
  - Markdown    → markdown → HTML → pdfkit (wkhtmltopdf)
  - Office 文档 → LibreOffice 无头模式
"""

from __future__ import annotations

import base64
import logging
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Callable
from urllib.parse import unquote

logger = logging.getLogger(__name__)


def _com_uninitialize_quietly() -> None:
    """反初始化当前线程的 COM（吞掉异常，绝不影响主流程）。

    P1（审计 2026-12）：pywin32 的 `pythoncom.CoInitialize()` **返回 None**，
    不是 S_OK(0)。因此历史写法 `if com_init == 0: CoUninitialize()` 的条件恒为 False，
    所有正常路径永不反初始化（当前靠避免重复初始化不崩，属巧合）。
    统一改为无条件配对，与 `start_word_warmup` 的 finally 写法一致。
    """
    try:
        import pythoncom
        pythoncom.CoUninitialize()
    except Exception as e:  # 重复反初始化 / 未初始化 → 忽略
        logger.debug(f"CoUninitialize 忽略异常: {e}")


def _engine_probe_disabled() -> bool:
    """`HN_NO_ENGINE_PROBE=1` ⇒ 引擎探测直接判"不可用"（不碰 exe、更不碰 COM）。

    给测试/CI 的硬闸门：run_all.py 会对每个脚本注入该变量，保证**任何**测试构造
    MainWindow / 调 get_available_engines() 都不会启动或附着 Office。
    """
    return (os.environ.get("HN_NO_ENGINE_PROBE") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _dispatch_ex(prog_id: str):
    """用 `DispatchEx` 创建**私有** Office COM 实例（本项目唯一允许的启动方式）。

    为什么不能 Dispatch（2026-10-24 现场事故）：Word/WPS 是单实例 COM 服务器，
    机器上已有用户打开的实例时 `Dispatch` 会**附着**到它；随后 `Quit()` / `Visible=False`
    直接作用在用户的文档上 —— 一次"探测 Word 装没装"就能把用户正在编辑的 Word 关掉。
    `DispatchEx` 强制新进程，生命周期完全归本程序，用户实例不受影响。
    """
    import win32com.client
    return win32com.client.DispatchEx(prog_id)


# ============================================================
# 多引擎转换缓存
# ============================================================

# 引擎可用性缓存 (None=未检测, True=可用, False=不可用)
_engine_cache: dict[str, bool | None] = {
    "word": None,
    "wps": None,
}

# WPS COM ProgID 缓存 (检测到可用时记录)
_wps_progid: str | None = None

# Word COM 后台预热
_warm_word_thread: threading.Thread | None = None
_warm_word_running = False


# ============================================================
# Office COM 自动化安全（S1-04）
# ============================================================
#
# 威胁模型：Office 文档可以带 VBA 宏（.docm/.doc 甚至改后缀的 .docx）。用 COM 打开时
# 默认会**执行**宏（Document_Open / AutoOpen），或者在「禁用宏」提示背后仍可能跑
# 连接/字段更新（DDE、远程模板、外部链接）—— 打印机是长期在线的、以管理员身份跑的
# 终端，云端来的文档由顾客控制内容，绝不能获得这条执行路径。
#
# 这条链上有两个独立的控制点，缺一不可：
#   ① 打开前收紧 COM 实例：Visible=False（不弹主窗）、DisplayAlerts=0（吞掉所有模态框，
#      包括「是否启用宏」——否则自动化会挂在没人点的对话框上）、
#      AutomationSecurity=3（msoAutomationSecurityForceDisable，**禁用所有宏**）、
#      Options.UpdateLinksAtOpen=False（不更新外部链接/远程模板/DDE 字段）；
#   ② **云端来源的 Office 文档不交给 Word/WPS**：直接走 LibreOffice 无头转换
#      （它不执行 VBA，见 `_convert_office_to_pdf(from_cloud=True)`）。
# 只做 ① 是不够的：任何 Office COM 版本差异/注册表策略都可能让宏重新可用。

# MsoAutomationSecurity 枚举：3 = msoAutomationSecurityForceDisable（禁用所有宏，不提示）
MSO_AUTOMATION_SECURITY_FORCE_DISABLE = 3

# 只有这两种扩展名会走 Word/WPS COM（xls/ppt 一直走 LibreOffice）
COM_OFFICE_EXTS = (".doc", ".docx")

# ── S2-03：Office 转换沙箱（默认关闭；默认值必须是 False）──
# 开启后，**本地** .doc/.docx 的 Word/WPS COM 转换改到 Job Object 隔离 worker 里跑
# （office_sandbox.py + convert_worker.py）。默认关闭的原因：它改的是"每一单 Word"的
# 主链路，必须先在真机（Word/WPS/LibreOffice 三引擎）逐项验证再打开。打开方式：
#   ① 客户端配置 conversion_sandbox=true（GUI 启动时调 set_office_sandbox）；
#   ② 环境变量 HN_OFFICE_SANDBOX=1（现场调试/验收用，优先级最高）。
_office_sandbox_enabled = False


def set_office_sandbox(enabled: bool) -> None:
    """设置进程级沙箱开关（GUI 启动时按配置调用）。"""
    global _office_sandbox_enabled
    _office_sandbox_enabled = bool(enabled)


def office_sandbox_enabled() -> bool:
    """生效值：环境变量可强制开关（1/0），否则用配置项。"""
    env = (os.environ.get("HN_OFFICE_SANDBOX") or "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    return bool(_office_sandbox_enabled)


def should_sandbox(file_path: str, from_cloud: bool = False) -> bool:
    """该文件本次转换是否走沙箱：只有**本地**的 .doc/.docx（COM 引擎）才需要。

    · 云端文件默认在 S1-04 已强制 LibreOffice（无宏执行路径），不必再套 COM 沙箱；
      ⚠ 2026-10-26 隐式规则的唯一例外（管理员自打 + 管理员上传）会在**主进程**直接跑 COM，
      **不经过 Job Object 沙箱**（S2-03 的沙箱本来就只覆盖本地
      .doc/.docx；真机验证也证明它对 Word/WPS 基本无效）。要连它一起套，得把
      `allow_cloud_com` 传进来并重做 S2-03 的口径 —— 本批刻意没做；
    · xls/xlsx/ppt/pptx 本来就只走 LibreOffice 子进程（独立进程、无宏），不套；
    · 开关关闭 ⇒ 恒 False（对存量行为零影响）。

    ⚠ **worker 内部恒 False**（2026-10-25 真机验证后修）：沙箱 worker 是父进程用
    `subprocess` 起的，会继承 `HN_OFFICE_SANDBOX=1`；若不做闸门，worker 里的转换又会判定
    "需要沙箱" ⇒ 再起一个 worker ⇒ 无限套娃（实测 11.5 s 内 79 个并发 `convert_worker`，
    最终超时失败）。所以 worker 一律带 `HN_OFFICE_SANDBOX_CHILD=1`，这里见到它就是
    "我已经在沙箱里了，直接转"。
    """
    if (os.environ.get("HN_OFFICE_SANDBOX_CHILD") or "").strip().lower() in (
            "1", "true", "yes", "on"):
        return False
    return (not from_cloud) and ext_of(file_path) in COM_OFFICE_EXTS and office_sandbox_enabled()


def _harden_office_com_app(app, label: str) -> dict[str, bool]:
    """在 `Documents.Open` **之前**把 Word / WPS 的 COM 实例收紧到「不执行宏、不弹窗、不更新链接」。

    返回 `{设置项: 是否真的生效}`，并把它写进日志 —— 属性在某种 Office 版本上不存在时
    不谎报成功（`try/except` 逐项记录）。调用方只需调用，不需要判断返回值。

    WPS 的实际行为（如实记录，别在文档里夸大成"和 Word 完全一样"）：
      · `Visible` / `DisplayAlerts` —— WPS 的 COM 对象模型是 Word 的克隆，这两个一直可用；
      · `AutomationSecurity`（=3 禁用所有宏）—— 新版本 WPS 提供该属性，老版本可能没有；
        取不到时**这一项在 WPS 侧不生效**，宏拦截退回到 WPS 自身的宏安全级别设置
        （界面「信任中心」/ 注册表项，本程序不越权去改用户的安全策略）；
      · `Options.UpdateLinksAtOpen` —— 同上，取不到时只是不主动关链接更新。
    因此**顾客上传的云端文档根本不会走到这里**（强制 LibreOffice）；这里服务的是用户自己
    从本地拖进来的文件，以及「管理员自打 + 管理员上传」那一条隐式放行路径（2026-10-26）——
    出问题时都能追溯到具体文件，而宏仍被上面两条挡住（Word 侧全生效，WPS 侧尽力而为 +
    新版本生效）。
    """
    applied: dict[str, bool] = {}

    try:
        app.Visible = False
        applied["Visible=False"] = True
    except Exception as e:
        applied["Visible=False"] = False
        logger.debug(f"{label}: Visible=False 未生效 ({e})")

    try:
        app.DisplayAlerts = 0          # wdAlertsNone：不弹任何模态框
        applied["DisplayAlerts=0"] = True
    except Exception as e:
        applied["DisplayAlerts=0"] = False
        logger.debug(f"{label}: DisplayAlerts=0 未生效 ({e})")

    # msoAutomationSecurityForceDisable：**禁用所有宏**（不提示、不执行）
    try:
        app.AutomationSecurity = MSO_AUTOMATION_SECURITY_FORCE_DISABLE
        applied["AutomationSecurity=3"] = True
    except Exception as e:
        applied["AutomationSecurity=3"] = False
        logger.warning(f"{label}: AutomationSecurity=3 未生效（该版本 COM 无此属性）: {e}")

    # 打开时不更新外部链接（远程模板 / DDE / 外链字段）—— 否则打开动作本身就会出网
    try:
        app.Options.UpdateLinksAtOpen = False
        applied["Options.UpdateLinksAtOpen=False"] = True
    except Exception as e:
        applied["Options.UpdateLinksAtOpen=False"] = False
        logger.debug(f"{label}: Options.UpdateLinksAtOpen=False 未生效 ({e})")

    if all(applied.values()):
        logger.info(f"{label} COM 安全设置已生效: {', '.join(applied)}")
    else:
        logger.info(f"{label} COM 安全设置（部分属性该版本不支持）: "
                    + ", ".join(f"{k}={'✓' if v else '✗'}" for k, v in applied.items()))
    return applied


def start_word_warmup() -> None:
    """
    后台线程预热 Word COM — 启动 WINWORD.EXE 并保持存活。
    后续所有 COM Dispatch 不再冷启动，显著加速页数统计和打印。

    ⚠ 默认**不启用**（需 `HN_WARM_OFFICE=1`）：预热会在后台常驻一个 Office 进程，
    对"只是开一下工具"的用户是纯资源消耗；而且隔离沙箱/测试环境里更不该自动拉起
    Office。预热用 `DispatchEx` 私有实例，不碰用户已打开的 Word。
    """
    global _warm_word_thread, _warm_word_running
    if _warm_word_running:
        return
    if _engine_probe_disabled() or (os.environ.get("HN_WARM_OFFICE") or "").strip() != "1":
        logger.info("Office 预热未启用（默认关闭；需要时设置 HN_WARM_OFFICE=1）")
        return

    _warm_word_running = True

    def _warm_worker() -> None:
        global _warm_word_running
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            word = _dispatch_ex("Word.Application")
            # S1-04：预热实例也会被后续转换复用 ⇒ 现在就把宏/弹窗/链接收紧
            _harden_office_com_app(word, "Word(预热)")
            logger.info("Word COM 已预热（后台常驻）")

            # 保持存活，等待外部信号
            while _warm_word_running:
                time.sleep(1)

            try:
                word.Quit()
            except Exception:
                pass
            logger.info("Word COM 预热进程已退出")
        except Exception as e:
            logger.warning(f"Word COM 预热失败（系统可能未安装 Word）: {e}")
            _warm_word_running = False
        finally:
            pythoncom.CoUninitialize()

    _warm_word_thread = threading.Thread(
        target=_warm_worker, daemon=True, name="word-warmup"
    )
    _warm_word_thread.start()


def stop_word_warmup() -> None:
    """停止 Word COM 预热实例。"""
    global _warm_word_running
    _warm_word_running = False


# WPS COM 后台预热
_warm_wps_thread: threading.Thread | None = None
_warm_wps_running = False


def start_wps_warmup() -> None:
    """后台线程预热 WPS COM — 启动 wps.exe 并保持存活（默认不启用，同 Word 预热）。"""
    global _warm_wps_thread, _warm_wps_running
    if _warm_wps_running:
        return
    if _engine_probe_disabled() or (os.environ.get("HN_WARM_OFFICE") or "").strip() != "1":
        logger.info("WPS 预热未启用（默认关闭；需要时设置 HN_WARM_OFFICE=1）")
        return

    _warm_wps_running = True

    def _warm_worker() -> None:
        global _warm_wps_running
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            # 先检测 WPS ProgID
            for pid in ["WPS.Application", "KWPS.Application"]:
                try:
                    wps = _dispatch_ex(pid)
                    global _wps_progid
                    _wps_progid = pid
                    break
                except Exception:
                    continue
            else:
                raise RuntimeError("WPS COM ProgID 未注册")

            # S1-04：WPS 侧的宏/弹窗/链接收紧（属性缺失时记录实际生效项，不谎报）
            _harden_office_com_app(wps, "WPS(预热)")
            logger.info(f"WPS COM 已预热（后台常驻, ProgID={_wps_progid}）")

            while _warm_wps_running:
                time.sleep(1)

            try:
                wps.Quit()
            except Exception:
                pass
            logger.info("WPS COM 预热进程已退出")
        except Exception as e:
            logger.warning(f"WPS COM 预热失败（系统可能未安装 WPS）: {e}")
            _warm_wps_running = False
        finally:
            pythoncom.CoUninitialize()

    _warm_wps_thread = threading.Thread(
        target=_warm_worker, daemon=True, name="wps-warmup"
    )
    _warm_wps_thread.start()


def stop_wps_warmup() -> None:
    """停止 WPS COM 预热实例。"""
    global _warm_wps_running
    _warm_wps_running = False


def _safe_word_dispatch():
    """获取 Word **私有** COM 实例（DispatchEx；绝不 attach 用户正在用的 Word）。"""
    return _dispatch_ex("Word.Application")


def _safe_wps_dispatch():
    """获取 WPS **私有** COM 实例：依次尝试缓存/两个 ProgID，全部失败才抛错。

    与 Word 同理用 DispatchEx；ProgID 不再在探测阶段确认（探测不碰 COM）。
    """
    global _wps_progid
    candidates: list[str] = []
    if _wps_progid:
        candidates.append(_wps_progid)
    for pid in ("WPS.Application", "KWPS.Application"):
        if pid not in candidates:
            candidates.append(pid)
    last_err = None
    for pid in candidates:
        try:
            app = _dispatch_ex(pid)
            _wps_progid = pid
            return app
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"WPS COM 启动失败（DispatchEx 私有实例）: {last_err}")


def get_available_engines() -> dict[str, bool]:
    """
    检测本机可用的 Word 打印引擎。

    有预热时直接认定 Word 可用（避免主线程冷启动 Dispatch）；
    无预热时才通过 _detect_word() 检测。
    """
    return {
        "word": _warm_word_running or _detect_word(),
        "wps": _warm_wps_running or _detect_wps(),
        "libreoffice": _find_libreoffice() is not None,
    }


def ext_of(file_path: str) -> str:
    """取扩展名（小写，含点）。**点开头的名字特判**：

    `os.path.splitext(".pdf")` 认为"这是隐藏文件、没有扩展名"（返回 ('.pdf', '')）——
    可上游真把文件落成 `.pdf` 时，那就是个 PDF。老写法会让类型判定得到空字符串 →
    报「不支持的文件类型:  (文件: ....pdf)」（线上 2026-10 中文名云端订单事故的最后一环：
    响应头只剩 `.pdf`，即使落到了盘上也照样打不出来）。"""
    base = os.path.basename(str(file_path or ""))
    ext = os.path.splitext(base)[1].lower()
    if not ext and base.startswith(".") and len(base) > 1 and "." not in base[1:]:
        ext = base.lower()
    return ext


def _copy_to_temp(file_path: str) -> str:
    """将文件复制到临时目录，返回临时路径。用于 COM 安全操作。"""
    ext = os.path.splitext(file_path)[1]
    fd, temp_path = tempfile.mkstemp(suffix=ext, prefix="_word_")
    os.close(fd)
    shutil.copy2(file_path, temp_path)
    logger.debug(f"已复制到临时文件: {os.path.basename(temp_path)}")
    return temp_path


# CSV 单元格中的控制字符（\x00-\x08、\x0b、\x0c、\x0e-\x1f），reportlab Paragraph 会因此抛错
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# 文本文件的 BOM 表（**长 BOM 必须排在前**：UTF-32LE 的 BOM 以 UTF-16LE 的 BOM
#   「\xff\xfe」为前缀，先判短的会让 UTF-32 被半路截胡、读出 \x00 打头的乱码）。
_TEXT_BOMS = (
    (b"\xff\xfe\x00\x00", "utf-32"),   # UTF-32 LE
    (b"\x00\x00\xfe\xff", "utf-32"),   # UTF-32 BE
    (b"\xff\xfe", "utf-16"),           # UTF-16 LE
    (b"\xfe\xff", "utf-16"),           # UTF-16 BE
)


def _read_text_auto(file_path: str) -> str:
    """自动探测编码读取文本文件：BOM（UTF-16/32）→ utf-8-sig → gbk → latin-1(替换)。

    解决 GBK/UTF-16/带 BOM 文本乱码问题；latin-1 兜底永不失败，无法解码的字节替换。
    无 BOM 的 UTF-16 另按「原始字节里的 NUL 分布」识别（见 ②）。
    """
    # ① 先嗅探原始字节决定编码，**必须在编码链之前**：UTF-16/32 的文件若走 utf-8/gbk，
    #    只会解出带 NUL 的乱码（带 BOM 的会落进 latin-1 兜底并打个警告，**无 BOM 的
    #    连警告都没有** —— utf-8 解码器认为那些字节全都合法）。按长→短匹配 BOM，
    #    UTF-32 才不会被 UTF-16 截胡。
    try:
        with open(file_path, "rb") as f:
            raw = f.read(8192)
    except OSError:
        raw = b""
    enc = None
    for bom, _e in _TEXT_BOMS:
        if raw.startswith(bom):
            enc = _e
            break
    # ② 无 BOM 的 UTF-16：看整段字节里 NUL 的分布（UTF-16 的 ASCII 字符 = 字符 + \x00）。
    #    小端把 \x00 放在**奇数位**、大端放在偶数位；用切片 + bytes.count（C 层，够快）。
    #    ⚠ 已知未覆盖：**纯中文且无 BOM** 的 UTF-16（整段没有一个 \x00，此处判不出来，
    #    会退到 latin-1 乱码）—— 现实里这类文件几乎都带 BOM（记事本 / Excel 导出均带）。
    if enc is None and b"\x00" in raw:
        odd = raw[1::2].count(0)
        even = raw[0::2].count(0)
        pairs = len(raw) // 2
        if pairs and (odd * 2 > pairs or even * 2 > pairs):
            enc = "utf-16-le" if odd >= even else "utf-16-be"
    # ③ 按选定的编码用**文本模式**读（保留通用换行转换，与历史行为一致）
    if enc:
        try:
            with open(file_path, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            pass
    # 先 UTF-8（utf-8-sig 顺带剥离 BOM）
    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            return f.read()
    except UnicodeDecodeError:
        pass
    # GBK（常见中文编码）
    try:
        with open(file_path, "r", encoding="gbk") as f:
            return f.read()
    except UnicodeDecodeError:
        pass
    # 兜底：latin-1 可解码任意字节序列
    logger.warning(f"文本编码探测失败（非 UTF-8/GBK），按 latin-1 读取并替换无法解码字符: {file_path}")
    with open(file_path, "r", encoding="latin-1", errors="replace") as f:
        return f.read()


# ============================================================
# 工具函数
# ============================================================

def _find_executable(name: str, search_paths: list[str]) -> str | None:
    """在指定路径列表中查找可执行文件，返回首个存在的路径。"""
    for p in search_paths:
        if os.path.isfile(p):
            return p
    # 尝试 PATH 中查找
    which = shutil.which(name)
    return which


def _find_libreoffice() -> str | None:
    """跨平台查找 LibreOffice 可执行文件路径。"""
    system = platform.system()
    if system == "Windows":
        candidates = [
            os.path.expandvars(r"%ProgramFiles%\LibreOffice\program\soffice.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\LibreOffice\program\soffice.exe"),
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]
    elif system == "Darwin":
        candidates = [
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        ]
    else:  # Linux
        candidates = [
            "/usr/bin/soffice",
            "/usr/lib/libreoffice/program/soffice",
            "/usr/lib64/libreoffice/program/soffice",
        ]
    return _find_executable("soffice", candidates)


def _find_wkhtmltopdf() -> str | None:
    """查找 wkhtmltopdf 可执行文件。"""
    system = platform.system()
    if system == "Windows":
        candidates = [
            os.path.expandvars(r"%ProgramFiles%\wkhtmltopdf\bin\wkhtmltopdf.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\wkhtmltopdf\bin\wkhtmltopdf.exe"),
            r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe",
        ]
    elif system == "Darwin":
        candidates = [
            "/usr/local/bin/wkhtmltopdf",
            "/opt/homebrew/bin/wkhtmltopdf",
        ]
    else:
        candidates = [
            "/usr/bin/wkhtmltopdf",
            "/usr/local/bin/wkhtmltopdf",
        ]
    return _find_executable("wkhtmltopdf", candidates)


# ============================================================
# Office 引擎检测 (Word / WPS)
# ============================================================

def _detect_word() -> bool:
    """
    检测 Microsoft Word 是否可用：**只看 exe 是否存在，绝不碰 COM**。

    2026-10-24 现场事故：旧实现用 `Dispatch("Word.Application")` + `Quit()` 做"确认"，
    而 Word 是单实例 COM 服务器 ⇒ 用户正开着 Word 时会被附身，Quit 直接把用户的文档关掉
    （测试只是构造主窗口、界面上什么都看不到）。可用性探测不允许有任何用户可见副作用。
    真正转换时才用 `_dispatch_ex()` 建私有实例，失败由降级链兜底。
    """
    global _engine_cache
    if _engine_cache["word"] is not None:
        return _engine_cache["word"]

    if _engine_probe_disabled():
        logger.info("HN_NO_ENGINE_PROBE=1：跳过 Office 引擎探测（测试/CI 强制）")
        _engine_cache["word"] = False
        return False

    # Step 1: 快速文件检测
    exe_found = False
    word_candidates = [
        os.path.expandvars(r"%ProgramFiles%\Microsoft Office\root\Office16\WINWORD.EXE"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft Office\root\Office16\WINWORD.EXE"),
        r"C:\Program Files\Microsoft Office\Office16\WINWORD.EXE",
        r"C:\Program Files (x86)\Microsoft Office\Office16\WINWORD.EXE",
        # Office 365 / Click-to-Run 路径
        os.path.expandvars(r"%ProgramFiles%\Microsoft Office\root\Office15\WINWORD.EXE"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft Office\root\Office15\WINWORD.EXE"),
    ]
    for p in word_candidates:
        if os.path.isfile(p):
            exe_found = True
            break
    if not exe_found and shutil.which("WINWORD.EXE"):
        exe_found = True

    if not exe_found:
        logger.info("未检测到 Microsoft Word (WINWORD.EXE)")
        _engine_cache["word"] = False
        return False

    # exe 在 ⇒ 认为可用（COM 是否真的能起，交给转换时的 DispatchEx + 降级链判定）
    _engine_cache["word"] = True
    logger.info("✓ 检测到 Microsoft Word (WINWORD.EXE，未启动 COM)")
    return True


def _detect_wps() -> bool:
    """
    检测 WPS Office 是否可用：**只看 wps.exe 是否存在，绝不碰 COM**（理由同 _detect_word）。

    ProgID（WPS.Application / KWPS.Application）不再在探测阶段确认；转换时由
    `_safe_wps_dispatch()` 用 DispatchEx 依次尝试。
    """
    global _engine_cache, _wps_progid
    if _engine_cache["wps"] is not None:
        return _engine_cache["wps"]

    if _engine_probe_disabled():
        logger.info("HN_NO_ENGINE_PROBE=1：跳过 Office 引擎探测（测试/CI 强制）")
        _engine_cache["wps"] = False
        return False

    # Step 1: 搜索 wps.exe
    exe_found = False
    wps_candidates = [
        os.path.expandvars(r"%LocalAppData%\Kingsoft\WPS Office\wps.exe"),
        os.path.expandvars(r"%ProgramFiles%\WPS Office\wps.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\WPS Office\wps.exe"),
        r"C:\Program Files\WPS Office\wps.exe",
        r"C:\Program Files (x86)\WPS Office\wps.exe",
    ]

    for p in wps_candidates:
        if os.path.isfile(p):
            exe_found = True
            break

    # 在 WPS 目录树中搜索（WPS 路径常带版本号）
    if not exe_found:
        search_bases = []
        for base in [
            os.path.expandvars(r"%ProgramFiles%\WPS Office"),
            os.path.expandvars(r"%ProgramFiles(x86)%\WPS Office"),
            os.path.expandvars(r"%LocalAppData%\Kingsoft\WPS Office"),
        ]:
            if os.path.isdir(base):
                search_bases.append(base)
        for base in search_bases:
            try:
                for root, dirs, files in os.walk(base):
                    if "wps.exe" in files:
                        exe_found = True
                        logger.info(f"搜索到 WPS: {os.path.join(root, 'wps.exe')}")
                        break
            except (PermissionError, OSError):
                continue
            if exe_found:
                break

    if not exe_found and shutil.which("wps.exe"):
        exe_found = True

    if not exe_found:
        logger.info("未检测到 WPS Office (wps.exe)")
        _engine_cache["wps"] = False
        return False

    # exe 在 ⇒ 认为可用；ProgID 留默认值，真正转换时由 _safe_wps_dispatch() 逐个试
    _wps_progid = _wps_progid or "WPS.Application"
    _engine_cache["wps"] = True
    logger.info(f"✓ 检测到 WPS Office (wps.exe，未启动 COM；默认 ProgID={_wps_progid})")
    return True


# ============================================================
# Office COM 转换器 (Word / WPS)
# ============================================================

def _convert_via_word_com(file_path: str, output_pdf: str) -> None:
    """
    使用 Microsoft Word COM 将 .doc/.docx 文档转为 PDF。
    布局保真度最高 — 使用 Word 自身渲染引擎。
    操作临时副本，不干扰原文件。
    """
    import pythoncom
    import win32com.client

    abs_output = os.path.abspath(output_pdf)
    temp_input = _copy_to_temp(file_path)

    logger.info(f"Word COM: 开始转换 {os.path.basename(file_path)}")

    # COM 线程初始化（finally 中无条件配对 _com_uninitialize_quietly）
    pythoncom.CoInitialize()  # P1: 返回值恒为 None，finally 里无条件配对反初始化

    word = None
    doc = None
    try:
        word = _safe_word_dispatch()
        # S1-04：必须在 Documents.Open 之前收紧（宏在 Open 期间就会跑）
        _harden_office_com_app(word, "Word")

        # wdOpenFormat = 自动检测, ReadOnly=True 避免修改原文件
        doc = word.Documents.Open(os.path.abspath(temp_input), ReadOnly=True)

        # ExportFormat=17 = wdExportFormatPDF
        doc.ExportAsFixedFormat(abs_output, ExportFormat=17)

        # wdDoNotSaveChanges = 0
        doc.Close(SaveChanges=0)
        doc = None

        if not _warm_word_running:
            word.Quit()
        word = None

        logger.info(f"Word COM → PDF 完成: {output_pdf}")
    except Exception as e:
        logger.error(f"Word COM 转换失败: {e}")
        raise RuntimeError(f"Microsoft Word 转换失败: {e}") from e
    finally:
        # 确保 COM 资源释放
        if doc is not None:
            try:
                doc.Close(SaveChanges=0)
            except Exception:
                pass
        if word is not None:
            try:
                if not _warm_word_running:
                    word.Quit()
            except Exception:
                pass
        # P1: 无条件配对反初始化（CoInitialize 返回 None，比较 0 的旧写法恒不成立）
        _com_uninitialize_quietly()
        if os.path.isfile(temp_input):
            try:
                os.remove(temp_input)
            except OSError:
                pass


def _convert_via_wps_com(file_path: str, output_pdf: str) -> None:
    """
    使用 WPS Office COM 将 .doc/.docx 文档转为 PDF。
    对中文文档排版兼容性好。
    """
    global _wps_progid
    import pythoncom
    import win32com.client

    abs_output = os.path.abspath(output_pdf)

    # 复用检测阶段缓存的 ProgID
    prog_id = _wps_progid

    logger.info(f"WPS COM: 开始转换 {os.path.basename(file_path)} (ProgID={prog_id})")

    # P2-4: 与 Word 路径对齐 — 打开前先复制到临时副本，避免文件锁，finally 清理
    temp_input = _copy_to_temp(file_path)

    pythoncom.CoInitialize()  # P1: 返回值恒为 None，finally 里无条件配对反初始化

    wps = None
    doc = None
    try:
        wps = _safe_wps_dispatch()
        # S1-04：WPS 打开前同样收紧（宏/弹窗/外链）；属性缺失时日志记录实际生效项
        _harden_office_com_app(wps, "WPS")

        doc = wps.Documents.Open(os.path.abspath(temp_input), ReadOnly=True)

        # WPS 兼容 Word ExportFormat 常量 (17 = PDF)
        doc.ExportAsFixedFormat(abs_output, ExportFormat=17)

        doc.Close(SaveChanges=0)
        doc = None

        if not _warm_wps_running:  # P1-6: 不杀预热线程维持的 WPS 实例
            wps.Quit()
        wps = None

        logger.info(f"WPS COM → PDF 完成: {output_pdf}")
    except Exception as e:
        logger.error(f"WPS COM 转换失败: {e}")
        raise RuntimeError(f"WPS Office 转换失败: {e}") from e
    finally:
        if doc is not None:
            try:
                doc.Close(SaveChanges=0)
            except Exception:
                pass
        if wps is not None:
            try:
                if not _warm_wps_running:
                    wps.Quit()
            except Exception:
                pass
        # P1: 无条件配对反初始化（CoInitialize 返回 None，`== 0` 恒 False）
        _com_uninitialize_quietly()
        if os.path.isfile(temp_input):
            try:
                os.remove(temp_input)
            except OSError:
                pass


# ============================================================
# .docx 元数据检测
# ============================================================

def _read_docx_last_editor(file_path: str) -> str | None:
    """
    读取 .docx 文档最后编辑者。
    优先从 ZIP 内部 docProps/app.xml 的 <Application> 标签获取（WPS 保存时通常含 "WPS"）；
    未命中时再查 docProps/core.xml 的 lastModifiedBy/creator（部分 WPS 兼容保存仅在此留痕）。

    Returns:
        "wps"  — 最后被 WPS 编辑
        "word" — 最后被 Microsoft Word 编辑
        None   — 无法判断（标签缺失、非 .docx 文件、读取失败）
    """
    ext = ext_of(file_path)
    if ext != ".docx":
        return None

    try:
        import zipfile
        import xml.etree.ElementTree as ET

        def _judge(text: str | None) -> str | None:
            if not text:
                return None
            t = text.upper()
            if "WPS" in t:
                return "wps"
            if "MICROSOFT" in t or "WORD" in t:
                return "word"
            return None

        with zipfile.ZipFile(file_path, 'r') as z:
            # 1) docProps/app.xml 的 <Application>（主要来源）
            if 'docProps/app.xml' in z.namelist():
                with z.open('docProps/app.xml') as f:
                    ns = '{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}'
                    tree = ET.parse(f)
                    app = tree.find(f'{ns}Application')
                    r = _judge(app.text if app is not None else None)
                    if r:
                        return r
            # 2) docProps/core.xml 的 lastModifiedBy / creator（WPS 兼容保存兜底）
            if 'docProps/core.xml' in z.namelist():
                with z.open('docProps/core.xml') as f:
                    ns_cp = '{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}'
                    ns_dc = '{http://purl.org/dc/elements/1.1/}'
                    tree = ET.parse(f)
                    for tag in (f'{ns_cp}lastModifiedBy', f'{ns_dc}creator'):
                        el = tree.find(tag)
                        r = _judge(el.text if el is not None else None)
                        if r:
                            return r
        return None
    except Exception:
        return None


# ============================================================
# COM 快速页数获取 (Word / WPS ComputeStatistics + 批量)
# ============================================================

def _get_com_page_counts_batch(
    file_paths: list[str],
    prefer_engine: str = "word",
) -> dict[str, int]:
    """
    一次 COM 会话批量获取多个 Word 文件的页数。
    比逐个调用 _get_word_page_count() 快 N 倍（只启动一次 COM 进程）。

    Args:
        file_paths: Word 文件路径列表
        prefer_engine: 首选引擎 "word" | "wps"

    Returns:
        {file_path: page_count}，失败的条目不会出现在结果中
    """
    if not file_paths:
        return {}

    results: dict[str, int] = {}

    def _batch_via_engine(prog_id: str, label: str) -> list[str]:
        """用指定引擎批量处理，返回未成功的文件列表。"""
        import pythoncom
        import win32com.client

        remaining: list[str] = []
        pythoncom.CoInitialize()  # P1: 返回值恒为 None，finally 里无条件配对反初始化

        app = None
        try:
            if prog_id == "Word.Application":
                app = _safe_word_dispatch()
            else:
                app = _safe_wps_dispatch()
            # S1-04：批量数页同样要打开文档 ⇒ 打开前收紧（宏/弹窗/外链更新）
            _harden_office_com_app(app, label)

            for fp in file_paths:
                doc = None
                temp_fp: str | None = None
                try:
                    # 复制到临时文件，避免文件锁冲突
                    temp_fp = _copy_to_temp(fp)
                    doc = app.Documents.Open(os.path.abspath(temp_fp), ReadOnly=True)
                    results[fp] = int(doc.ComputeStatistics(2))  # 2 = wdStatisticPages
                    doc.Close(SaveChanges=0)
                    doc = None
                    logger.info(f"{label} 页数: {os.path.basename(fp)} → {results[fp]} 页")
                except Exception as e:
                    logger.warning(f"{label} 页数获取失败 ({os.path.basename(fp)}): {e}")
                    remaining.append(fp)
                    if doc is not None:
                        try:
                            doc.Close(SaveChanges=0)
                        except Exception:
                            pass
                finally:
                    if temp_fp and os.path.isfile(temp_fp):
                        try:
                            os.remove(temp_fp)
                        except OSError:
                            pass

            # P1-6: 不要杀预热线程维持的进程 — 按实际引擎（label）分别对应各自预热变量
            warm_running = _warm_word_running if label == "Word" else _warm_wps_running
            if not warm_running:
                try:
                    app.Quit()
                except Exception:
                    pass
            app = None
        except Exception as e:
            logger.warning(f"{label} 批量页数初始化失败: {e}")
            remaining = list(file_paths)
        finally:
            if app is not None:
                warm_running = _warm_word_running if label == "Word" else _warm_wps_running
                if not warm_running:
                    try:
                        app.Quit()
                    except Exception:
                        pass
            # P1: 无条件配对反初始化（CoInitialize 返回 None，`== 0` 恒 False）
            _com_uninitialize_quietly()

        return remaining

    # 首选引擎
    first_choice = prefer_engine
    if first_choice == "word":
        remaining = _batch_via_engine("Word.Application", "Word")
        if remaining:
            _batch_via_engine(_wps_progid or "WPS.Application", "WPS")
    elif first_choice == "wps":
        remaining = _batch_via_engine(_wps_progid or "WPS.Application", "WPS")
        if remaining:
            _batch_via_engine("Word.Application", "Word")

    return results


def _get_word_page_count(file_path: str) -> int:
    """
    使用 Microsoft Word COM ComputeStatistics 快速获取页数。
    不生成 PDF，秒级响应。操作临时副本，不干扰原文件。
    """
    import pythoncom
    import win32com.client

    temp_fp = _copy_to_temp(file_path)
    pythoncom.CoInitialize()  # P1: 返回值恒为 None，finally 里无条件配对反初始化
    word = None
    doc = None
    try:
        word = _safe_word_dispatch()
        # S1-04：打开前收紧（宏/弹窗/外链）
        _harden_office_com_app(word, "Word")

        doc = word.Documents.Open(os.path.abspath(temp_fp), ReadOnly=True)
        # 2 = wdStatisticPages
        page_count = doc.ComputeStatistics(2)
        doc.Close(SaveChanges=0)
        doc = None
        if not _warm_word_running:
            word.Quit()
        word = None

        logger.info(f"Word COM 页数: {os.path.basename(file_path)} → {page_count} 页")
        return int(page_count)
    finally:
        if doc is not None:
            try:
                doc.Close(SaveChanges=0)
            except Exception:
                pass
        if word is not None:
            try:
                if not _warm_word_running:
                    word.Quit()
            except Exception:
                pass
        # P1: 无条件配对反初始化（CoInitialize 返回 None，`== 0` 恒 False）
        _com_uninitialize_quietly()
        if os.path.isfile(temp_fp):
            try:
                os.remove(temp_fp)
            except OSError:
                pass


def _get_wps_page_count(file_path: str) -> int:
    """
    使用 WPS Office COM ComputeStatistics 快速获取页数。
    """
    global _wps_progid
    import pythoncom
    import win32com.client

    abs_input = os.path.abspath(file_path)
    prog_id = _wps_progid

    pythoncom.CoInitialize()  # P1: 返回值恒为 None，finally 里无条件配对反初始化
    wps = None
    doc = None
    try:
        wps = _safe_wps_dispatch()
        # S1-04：WPS 打开前同样收紧（宏/弹窗/外链）
        _harden_office_com_app(wps, "WPS")

        doc = wps.Documents.Open(abs_input, ReadOnly=True)
        # 2 = wdStatisticPages (WPS 兼容此常量)
        page_count = doc.ComputeStatistics(2)
        doc.Close(SaveChanges=0)
        doc = None
        if not _warm_wps_running:  # P1-6: 不杀预热线程维持的 WPS 实例
            wps.Quit()
        wps = None

        logger.info(f"WPS COM 页数: {os.path.basename(file_path)} → {page_count} 页")
        return int(page_count)
    finally:
        if doc is not None:
            try:
                doc.Close(SaveChanges=0)
            except Exception:
                pass
        if wps is not None:
            try:
                if not _warm_wps_running:
                    wps.Quit()
            except Exception:
                pass
        # P1: 无条件配对反初始化（CoInitialize 返回 None，`== 0` 恒 False）
        _com_uninitialize_quietly()


# ============================================================
# 各格式转换器
# ============================================================

def _convert_txt_to_pdf(file_path: str, output_pdf: str) -> None:
    """TXT 文本 → PDF（reportlab，支持中文）。"""
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    # 注册中文字体
    _register_chinese_font()

    # 读取文本内容（自动探测编码：utf-8-sig → gbk → latin-1）
    text = _read_text_auto(file_path)

    # P0-2: 空文本 → 写一页占位，避免生成 0 页 PDF 报成功漏打
    if not text or not text.strip():
        logger.warning(f"TXT 文件为空，生成占位页: {file_path}")
        text = "（空白文件）"

    c = canvas.Canvas(output_pdf, pagesize=A4)
    width, height = A4

    margin_left = 20 * mm
    margin_right = 20 * mm
    margin_top = 15 * mm
    margin_bottom = 15 * mm
    usable_width = width - margin_left - margin_right
    usable_height = height - margin_top - margin_bottom

    font_name = _get_chinese_font_name()
    font_size = 11
    line_height = font_size + 4

    lines = text.split("\n")
    y = height - margin_top

    for line in lines:
        if not line.strip():
            y -= line_height
        else:
            # 中文自动换行
            wrapped = _wrap_text_line(line, font_name, font_size, usable_width, c)
            for wline in wrapped:
                if y < margin_bottom:
                    c.showPage()
                    c.setFont(font_name, font_size)
                    y = height - margin_top
                c.setFont(font_name, font_size)
                c.drawString(margin_left, y, _sanitize_non_ascii(wline))
                y -= line_height

        if y < margin_bottom:
            c.showPage()
            c.setFont(font_name, font_size)
            y = height - margin_top

    c.save()
    logger.info(f"TXT → PDF 完成: {output_pdf}")


def _convert_csv_to_pdf(file_path: str, output_pdf: str) -> None:
    """CSV 表格 → PDF（reportlab 绘制表格）。"""
    import csv
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import PageBreak

    _register_chinese_font()
    font_name = _get_chinese_font_name()

    # 读取 CSV（自动探测编码：utf-8-sig 顺带解决 BOM 进首格问题）
    reader = csv.reader(_read_text_auto(file_path).splitlines())
    rows = list(reader)

    if not rows:
        # 空 CSV，写个占位
        from reportlab.pdfgen import canvas as cvs
        c = cvs.Canvas(output_pdf, pagesize=A4)
        c.drawString(100, 500, "(空 CSV 文件)")
        c.save()
        return

    # 使用 landscape 方向（宽表友好）
    doc = SimpleDocTemplate(output_pdf, pagesize=landscape(A4),
                            leftMargin=10*mm, rightMargin=10*mm,
                            topMargin=10*mm, bottomMargin=10*mm)

    styles = getSampleStyleSheet()
    style_normal = styles["Normal"]
    style_normal.fontName = font_name
    style_normal.fontSize = 9

    # 将每格包装为 Paragraph
    def cell(text: str) -> Paragraph:
        # P2-11: 清洗控制字符，避免 reportlab Paragraph 抛错
        clean = _CTRL_RE.sub(" ", str(text))
        # P2-6: 无中文字体回退时替换非 ASCII，避免 UnicodeEncodeError
        clean = _sanitize_non_ascii(clean)
        return Paragraph(clean.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), style_normal)

    table_data = [[cell(val) for val in row] for row in rows]

    max_rows_per_page = 40  # 横向每页大约行数
    elements = []

    for i in range(0, len(table_data), max_rows_per_page):
        chunk = table_data[i:i + max_rows_per_page]
        tbl = Table(chunk, repeatRows=1 if i == 0 else 0)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#45475a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#888888")),
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]))
        elements.append(tbl)
        if i + max_rows_per_page < len(table_data):
            elements.append(PageBreak())

    doc.build(elements)
    logger.info(f"CSV → PDF 完成: {output_pdf}")


def _convert_image_to_pdf(file_path: str, output_pdf: str, orientation: str = "auto") -> None:
    """图片 → PDF（reportlab，居中适应 A4）。多页图片（TIFF/GIF）每帧一页。

    orientation: 'auto'（宽图横版 / 高图竖版，最大化打印面积）| 'landscape'（恒横版）| 'portrait'（恒竖版）"""
    from PIL import Image as PILImage
    from PIL import ImageSequence, ImageOps
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm

    img = PILImage.open(file_path)

    # 单帧图片最多渲染 5,000 万像素（防止超大图片 OOM；比 PIL 默认保护更早拒绝，故无需禁用 PIL 保护）
    MAX_IMAGE_PIXELS = 50_000_000

    c = None
    tmp_img_path = None
    page_num = 0
    try:
        for frame in ImageSequence.Iterator(img):
            # P3-?: 应用 EXIF 方向（手机竖拍照片原始像素是横的，靠 EXIF 旋转显示）。
            # 不转置会横着渲染，且宽高判定/方向检测/缩放全部基于错误的原始尺寸
            frame = ImageOps.exif_transpose(frame)
            # P1-7: 超大图片 OOM 保护 — 渲染前手动检查像素量，超限拒绝并抛明确错误
            fw, fh = frame.size
            if fw * fh > MAX_IMAGE_PIXELS:
                raise RuntimeError(
                    f"图片分辨率过大（{fw}×{fh} = {fw * fh:,} 像素，上限 {MAX_IMAGE_PIXELS:,} 像素），无法转换为 PDF: {file_path}"
                )

            # P2-12: 透明图片（RGBA/LA/带透明色板的 P）先合成到白底，避免打印黑底
            if frame.mode in ("RGBA", "LA") or (frame.mode == "P" and "transparency" in frame.info):
                rgba = frame.convert("RGBA")
                background = PILImage.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                frame = background
            elif frame.mode not in ("RGB", "L"):
                frame = frame.convert("RGB")

            iw, ih = frame.size
            # 页面方向：auto=按宽高自动（宽图横版/高图竖版），landscape/portrait=固定方向
            if orientation == "landscape":
                page_w, page_h = landscape(A4)
            elif orientation == "portrait":
                page_w, page_h = A4
            else:
                if iw > ih:
                    page_w, page_h = landscape(A4)
                else:
                    page_w, page_h = A4

            if c is None:
                c = canvas.Canvas(output_pdf, pagesize=(page_w, page_h))
            else:
                c.setPageSize((page_w, page_h))

            margin = 10 * mm
            max_w = page_w - 2 * margin
            max_h = page_h - 2 * margin

            scale = min(max_w / iw, max_h / ih, 1.0)
            draw_w = iw * scale
            draw_h = ih * scale

            x = (page_w - draw_w) / 2
            y = (page_h - draw_h) / 2

            # 保存临时图片用于嵌入（逐帧复用同一临时文件）
            tmp_img_path = output_pdf + ".tmp.png"
            frame.save(tmp_img_path)
            c.drawImage(tmp_img_path, x, y, width=draw_w, height=draw_h)
            c.showPage()  # 每帧一页
            page_num += 1
    finally:
        img.close()
        if tmp_img_path and os.path.exists(tmp_img_path):
            try:
                os.remove(tmp_img_path)
            except OSError:
                pass

    if c is None or page_num == 0:
        # 无法读取任何帧 → 兜底生成一页占位，避免 0 页 PDF 报成功漏打
        logger.warning(f"图片无法读取任何帧，生成占位页: {file_path}")
        c = canvas.Canvas(output_pdf, pagesize=A4)
        c.drawString(100, 500, "(空白图片)")
        c.save()
    else:
        c.save()
    logger.info(f"图片 → PDF 完成: {output_pdf} ({page_num} 页)")


# ============================================================
# S0-09（2026-10-23）：Markdown 渲染后 HTML 的安全闸门 + 本地图片内联
# ============================================================
# 背景（DS-01，已动态复现）：wkhtmltopdf 带「允许本地文件访问」开关时，顾客文档里的
# <img src="file:///…/print_config.json"> 或 XHR 能把本机任意文件读进 PDF / 外发。
# D-2 已拍板**永久拒绝 HTML 文件**，`.html/.htm` 的转换入口已从扩展名表删除；只剩
# Markdown 内部仍走 HTML 管线，所以这里做两道闸：
#   ① 转换前扫描危险语法 → 命中**直接拒绝**（不净化、不"尽力而为"）；
#   ② 去掉「允许本地文件访问」开关后本地相对路径图片会被**静默丢弃**（附录 A-7），
#      故先把输入文件同目录（含子目录）里的图片内联成 data: URI。内联做不到（不存在/
#      越界/超限/格式不支持）就**明确失败**，绝不静默少印图片。

# 与后端 S0-05 的 Markdown 扫描同一份判据；顺序无所谓，命中即拒。
_UNSAFE_HTML_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("file: 协议（可读取本机任意文件）", re.compile(r"file:", re.IGNORECASE)),
    ("UNC 网络路径（\\\\server\\share）", re.compile(r"\\\\")),
    ("<script> 脚本", re.compile(r"<\s*script", re.IGNORECASE)),
    ("<iframe> 内嵌框架", re.compile(r"<\s*iframe", re.IGNORECASE)),
    ("<object> 外部对象", re.compile(r"<\s*object", re.IGNORECASE)),
    ("<embed> 外部嵌入", re.compile(r"<\s*embed", re.IGNORECASE)),
    ("内联事件处理器（on…=）", re.compile(r"\bon\w+\s*=", re.IGNORECASE)),
    ("data:text/html 内联文档", re.compile(r"data\s*:\s*text/html", re.IGNORECASE)),
)

# 本地图片内联上限（附录 A-7）：单文件 ≤10MB、单份文档合计 ≤50MB
_MAX_INLINE_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_INLINE_IMAGE_TOTAL_BYTES = 50 * 1024 * 1024
# 只内联位图。SVG 可内嵌脚本（且属 D-2 的永久拒绝类型）⇒ 一律不内联，直接明确失败。
_INLINE_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

_IMG_SRC_RE = re.compile(
    r"""(<img\b[^>]*?\bsrc\s*=\s*)(?:"([^"]*)"|'([^']*)')""",
    re.IGNORECASE,
)


def _scan_unsafe_html(html: str) -> None:
    """Markdown 渲染后的 HTML 危险语法扫描（命中即拒绝转换，不做净化）。

    命中抛现有的转换失败异常（`RuntimeError`，与 wkhtmltopdf 失败同一族 —— 本模块没有
    也不新增 `ConversionError` 类），文案以「文档包含不安全内容」开头，GUI 会把它原样
    显示在「✗ 错误: …」里，操作员据此请顾客另存 PDF/DOCX 或转人工。
    """
    if not html:
        return
    for label, pattern in _UNSAFE_HTML_PATTERNS:
        m = pattern.search(html)
        if not m:
            continue
        pos = m.start()
        snippet = html[max(0, pos - 20):pos + 60].replace("\n", " ").replace("\r", " ")
        raise RuntimeError(
            f"文档包含不安全内容（{label}），已拒绝转换。"
            f"命中片段: …{snippet}…。"
            f"本工具不允许打印会读取本机文件或执行脚本的文档；"
            f"请让顾客另存为 PDF/DOCX 后重试，或联系管理员人工核对。"
        )


# ── S2-03 外部引用策略：转换过程一律不访问外网 ──
# Markdown 里可以出现指向远程的 `<img src=http…>`、CSS `url(http…)`/`@import`、
# `<link rel=stylesheet href=http…>`、`<base href=…>` 与 `<meta http-equiv=refresh>`
# （后两者能把相对地址/整页跳到攻击者站点）。S0-09 已经禁了 JS 与本地文件访问，
# 但 wkhtmltopdf 仍会去下载这些远程资源 —— 顾客文档可以让店铺机向任意地址发请求
# （内网探测/打点），也会把转换拖到 90 s 超时。策略：**不抓取**，直接剥离并记日志。
_REMOTE_URL_RE = re.compile(r"^\s*(?:https?:)?//", re.IGNORECASE)
_FETCH_ATTR_RE = re.compile(
    r"""(\s(?:src|srcset|poster)\s*=\s*)(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_LINK_TAG_RE = re.compile(r"<\s*link\b[^>]*>", re.IGNORECASE)
_BASE_TAG_RE = re.compile(r"<\s*base\b[^>]*>", re.IGNORECASE)
_META_REFRESH_RE = re.compile(
    r"""<\s*meta\b[^>]*http-equiv\s*=\s*["']?\s*refresh["']?[^>]*>""", re.IGNORECASE)
_CSS_IMPORT_RE = re.compile(r"@import\b[^;]*;", re.IGNORECASE)
_CSS_URL_RE = re.compile(r"""url\(\s*(["']?)([^)"']*)\1\s*\)""", re.IGNORECASE)


def _attr_value(tag: str, name: str) -> str:
    m = re.search(r"""\b%s\s*=\s*(?:"([^"]*)"|'([^']*)')""" % re.escape(name), tag,
                  re.IGNORECASE)
    if not m:
        return ""
    return m.group(1) if m.group(1) is not None else (m.group(2) or "")


def _strip_remote_refs(html: str) -> tuple[str, int]:
    """剥离所有会发起网络请求的外部引用，返回 (净化后 HTML, 移除处数)。

    只处理"会被 wkhtmltopdf 主动抓取"的引用；`<a href>` 只是链接（无 JS、不会自动打开），
    保留不动（不影响打印内容）。本地相对路径/data: URI 原样保留。
    """
    if not html:
        return html, 0
    removed = 0

    html, n = _BASE_TAG_RE.subn("", html)          # <base> 会改变相对地址的解析基准
    removed += n

    def _meta(m: re.Match) -> str:
        nonlocal removed
        removed += 1
        return ""

    html = _META_REFRESH_RE.sub(_meta, html)       # meta refresh 可整页跳转到远程

    def _link(m: re.Match) -> str:
        nonlocal removed
        tag = m.group(0)
        if _REMOTE_URL_RE.search(_attr_value(tag, "href")):
            removed += 1
            return ""
        return tag

    html = _LINK_TAG_RE.sub(_link, html)           # 远程样式表/预加载

    def _fetch_attr(m: re.Match) -> str:
        nonlocal removed
        val = m.group(2) if m.group(2) is not None else (m.group(3) or "")
        parts = [p.strip() for p in val.split(",")] if "srcset" in m.group(1).lower() else [val.strip()]
        if any(_REMOTE_URL_RE.match(p) for p in parts if p):
            removed += 1
            return ""                              # 整个属性移除：没有 src 就不会发请求
        return m.group(0)

    html = _FETCH_ATTR_RE.sub(_fetch_attr, html)   # img/source/video 等

    def _css_url(m: re.Match) -> str:
        nonlocal removed
        val = (m.group(2) or "").strip()
        if _REMOTE_URL_RE.match(val):
            removed += 1
            return 'url("data:,")'                 # data: 不会出网
        return m.group(0)

    html = _CSS_URL_RE.sub(_css_url, html)

    def _css_import(m: re.Match) -> str:
        nonlocal removed
        if _REMOTE_URL_RE.search(m.group(0)):
            removed += 1
            return ""
        return m.group(0)

    html = _CSS_IMPORT_RE.sub(_css_import, html)
    return html, removed


def _inline_local_images(html: str, base_dir: str) -> str:
    """把 `<img src>` 指向的本地文件内联成 data: URI（仅限 base_dir 及其子目录）。

    为什么必须做（附录 A-7）：wkhtmltopdf 0.12 去掉「允许本地文件访问」开关后，
    本地相对路径图片会被**静默丢弃**（不报错、PDF 里就是没有图）—— 顾客文档会少印
    图片，而操作员看不出任何异常。所以转换前把图片自己读进来内联。

    做不到的情况（图片不存在 / 超出输入文件目录 / 单文件 >10MB / 合计 >50MB / 格式
    不支持）一律抛 RuntimeError 明确失败 —— **绝不**允许静默少印。
    """
    base = os.path.abspath(base_dir or ".")
    total = 0

    def _repl(m: re.Match) -> str:
        nonlocal total
        prefix = m.group(1)
        src = m.group(2) if m.group(2) is not None else (m.group(3) or "")
        quote = '"' if m.group(2) is not None else "'"
        raw = src.strip()
        low = raw.lower()
        # 已经是内联数据 / 远程 URL / 协议相对地址：原样保留（远程图仍交给 wkhtmltopdf
        # 按 90s 整体超时去取；取不到属网络问题，不属于"本地文件访问"的范畴）
        if (not raw or low.startswith("data:") or low.startswith("http://")
                or low.startswith("https://") or raw.startswith("//")):
            return m.group(0)
        if low.startswith("file:"):
            # 正常走不到（_scan_unsafe_html 已先拒绝），留着兜底防将来漏审
            raise RuntimeError(
                f"文档包含不安全内容（file: 协议图片 {raw}），已拒绝转换。"
                f"请让顾客把图片嵌入文档后另存为 PDF/DOCX 再打印。")

        rel = raw.split("#", 1)[0].split("?", 1)[0]
        try:
            rel = unquote(rel)
        except Exception:  # noqa: BLE001 — 解码失败就按原样处理
            pass
        target = os.path.abspath(rel if os.path.isabs(rel) else os.path.join(base, rel))
        try:
            inside = os.path.commonpath([base, target]) == base
        except ValueError:
            inside = False
        if not inside:
            raise RuntimeError(
                f"文档引用了输入文件目录之外的本地图片，已中止转换（避免读取本机其它文件）: {raw}。"
                f"请把图片放到文档同目录（或其子目录）后重试，或让顾客另存为 PDF/DOCX。")
        if not os.path.isfile(target):
            raise RuntimeError(
                f"文档引用的本地图片不存在，已中止转换（避免静默少印图片）: {raw}。"
                f"请补齐图片后重试，或让顾客另存为 PDF/DOCX。")
        ext = os.path.splitext(target)[1].lower()
        mime = _INLINE_IMAGE_MIME.get(ext)
        if not mime:
            raise RuntimeError(
                f"文档引用的本地图片格式不支持内联（{ext or '无扩展名'}），已中止转换: {raw}。"
                f"请换成 PNG/JPG 等位图后重试。")
        try:
            size = os.path.getsize(target)
        except OSError as e:
            raise RuntimeError(f"读取本地图片信息失败，已中止转换: {raw} ({e})") from None
        if size > _MAX_INLINE_IMAGE_BYTES:
            raise RuntimeError(
                f"文档引用的本地图片过大（{size / 1024 / 1024:.1f} MB > 10 MB 上限），"
                f"已中止转换（避免静默少印图片）: {raw}。请压缩图片后重试。")
        if total + size > _MAX_INLINE_IMAGE_TOTAL_BYTES:
            raise RuntimeError(
                f"文档引用的本地图片合计超过 50 MB 上限，已中止转换（避免静默少印图片）。"
                f"请压缩/精简图片后重试。")
        try:
            with open(target, "rb") as f:
                data = f.read()
        except OSError as e:
            raise RuntimeError(f"本地图片读取失败，已中止转换: {raw} ({e})") from None
        total += len(data)
        return f"{prefix}{quote}data:{mime};base64,{base64.b64encode(data).decode('ascii')}{quote}"

    return _IMG_SRC_RE.sub(_repl, html)


def _convert_html_to_pdf(html_path_or_content: str, output_pdf: str, is_content: bool = False) -> None:
    """HTML 内容 → PDF（wkhtmltopdf 直接 subprocess 调用 + 90s 超时）。

    ⚠ 自 S0-09 / D-2 起**不再有 HTML 文件打印入口**（`.html/.htm` 已从
    `UniversalConverter.CONVERSION_MAP` 删除），本函数只供 Markdown 内部调用。

    说明：pdfkit 不暴露 timeout 参数（其底层就是调用 wkhtmltopdf 可执行文件），
    故改为直接 subprocess 调用，可加超时与安全选项。S0-09：命令行不含「允许本地
    文件访问」（local-file-access）开关，且带 `--disable-javascript`（顾客文档里的
    JS 一律不执行）；JS 关闭后限制脚本执行时长的两个参数失去意义，已一并移除
    （仍保留 90 s 进程超时）。
    """
    wk_path = _find_wkhtmltopdf()
    if not wk_path:
        raise RuntimeError(
            "未找到 wkhtmltopdf。请安装后加入 PATH。\n"
            "下载地址: https://wkhtmltopdf.org/downloads.html"
        )

    # 内容模式：先写入临时 HTML 文件再转换
    temp_html: str | None = None
    try:
        if is_content:
            # S2-03 外部引用策略：先剥离远程图片/CSS/跳转（转换过程不访问外网）
            html_text, n_remote = _strip_remote_refs(html_path_or_content)
            if n_remote:
                logger.warning(
                    f"已移除 {n_remote} 处外部引用（远程图片/CSS/刷新跳转）："
                    f"转换过程不访问外网，相关图片不会打印")
            fd, temp_html = tempfile.mkstemp(suffix=".html", prefix="_md_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(html_text)
            input_path = temp_html
        else:
            input_path = html_path_or_content

        cmd = [
            wk_path,
            "--page-size", "A4",
            "--encoding", "UTF-8",
            # S0-09 / DS-01：**不许**读本机文件（原先「允许本地文件访问」的参数已删除）。
            # 本地图片由 _inline_local_images 提前内联成 data: URI（附录 A-7）。
            # JS 一律不执行：顾客文档里的脚本无法读文件、也无法外发（XHR/fetch/Image）。
            "--disable-javascript",
            "--no-outline",
            "--margin-top", "10mm",
            "--margin-bottom", "10mm",
            "--margin-left", "10mm",
            "--margin-right", "10mm",
            input_path,
            os.path.abspath(output_pdf),
        ]
        logger.info(f"执行 wkhtmltopdf: {' '.join(cmd[:5])}... (超时 90s)")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"wkhtmltopdf 转换失败 (rc={result.returncode}): {result.stderr.strip()[:300]}"
            )
    except subprocess.TimeoutExpired:
        # 超时 → 抛明确错误，不再挂死（JS 已禁用，通常是文档过大或远程图片下载过慢）
        raise RuntimeError("wkhtmltopdf 转换超时（90 秒），已中止（文档过大或远程图片下载过慢）") from None
    except FileNotFoundError:
        raise RuntimeError(f"wkhtmltopdf 可执行文件未找到: {wk_path}") from None
    finally:
        if temp_html and os.path.isfile(temp_html):
            try:
                os.remove(temp_html)
            except OSError:
                pass

    logger.info(f"HTML → PDF 完成: {output_pdf}")


def _convert_markdown_to_pdf(file_path: str, output_pdf: str) -> None:
    """Markdown → HTML → PDF（S0-09：转换前做危险语法扫描 + 本地图片内联）。"""
    import markdown

    md_text = _read_text_auto(file_path)  # 与 TXT/CSV 一致的编码探测链（utf-8-sig → gbk → latin-1）

    # Markdown → HTML（包含基本样式）
    md_body = markdown.markdown(md_text, extensions=["tables", "fenced_code", "codehilite"])

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: "Microsoft YaHei", "Segoe UI", sans-serif; font-size: 12pt; line-height: 1.8; max-width: 780px; margin: 0 auto; padding: 20px; color: #222; }}
  pre {{ background: #f4f4f4; padding: 12px; border-radius: 4px; overflow-x: auto; }}
  code {{ background: #f4f4f4; padding: 2px 6px; border-radius: 3px; font-family: "Consolas", "Courier New", monospace; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; }}
  th {{ background: #eee; }}
  img {{ max-width: 100%; }}
</style>
</head>
<body>
{md_body}
</body>
</html>"""

    _scan_unsafe_html(html)  # 先拒危险语法（file:/脚本/事件处理器…），再谈图片
    # 本地相对路径图片内联成 data: URI —— 必须做在转换**之前**（附录 A-7）
    html = _inline_local_images(html, os.path.dirname(os.path.abspath(file_path)))

    _convert_html_to_pdf(html, output_pdf, is_content=True)
    logger.info(f"Markdown → PDF 完成: {output_pdf}")


def _pdf_product_ok(path: str) -> bool:
    """转换产物是否**真的**是一份能用的 PDF：存在、非空、且以 `%PDF` 开头。

    为什么需要（D5 第 4 条）：Word/WPS 的 COM 导出会**静默成功** —— `ExportAsFixedFormat`
    正常返回、不抛异常，但产物可能是 0 字节、半截文件，或压根不是 PDF（写成了别的格式）。
    降级链原先只看"函数没抛异常"就 `return`，而真正的产物校验在**链外**
    （`UniversalConverter.convert_to_pdf` 末尾）⇒ 这类情况 LibreOffice 兜底永远轮不到，
    用户拿到一个打不开的文件（甚至被当成"转换成功"去打印）。

    判定口径与链外**保持同一份实现**（链内外各写一套迟早漂移）：四种转换引擎的产物都是
    真 PDF，多要一个 `%PDF` 头不会误伤。"""
    try:
        if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
            return False
        with open(path, "rb") as f:
            return f.read(5).startswith(b"%PDF")
    except OSError:
        return False


def _discard_invalid_pdf(path: str) -> None:
    """丢弃被判定为无效的产物（COM 静默成功留下的 0 字节/非 PDF 残次品）。

    必须在降级到下一引擎**之前**删掉：否则半截文件留在原位，下一引擎可能"看到已存在"
    而不覆盖，或最终被当成转换结果交出去。失败只记日志，不影响降级流程。"""
    try:
        if path and os.path.isfile(path):
            os.remove(path)
            logger.warning(f"已丢弃无效的转换产物: {path}")
    except OSError as e:
        logger.warning(f"丢弃无效产物失败（忽略）: {path} ({e})")


def _strip_docx_external_refs(src: str) -> tuple[str, int]:
    """把 .docx 副本里**指向外部**的引用剥掉，返回 `(可交给引擎的路径, 剥离条数)`。

    为什么必须做（2026-10-25 真机验证实测，见 docs/S2-03沙箱三引擎验证_2026-10-25.md）：
    Office 打开文档时会去抓 `TargetMode="External"` 的链接图片（诱捕服务实测 Word 抓 2 次、
    WPS 1 次、LibreOffice 1 次，**开沙箱照样抓**）—— 这既是隐私泄露（谁在什么时候打印了什么
    会打到外部服务器），也是"文档可访问内网"的入口。`AutomationSecurity`/`UpdateLinksAtOpen`
    只挡 `INCLUDEPICTURE` 一类域，挡不住 blip 的 `r:link`。

    做法（不重新序列化 XML，避免破坏 `mc:Ignorable` 的命名空间声明）：
      1. 解析 `word/_rels/*.rels`，删掉 `TargetMode="External"` 的 `<Relationship>`，记下 Id；
      2. 在对应部件里删掉**只**引用这些 Id 的属性（`r:link` / `r:embed` / `r:id`）；
      3. 把 `w:instrText` 里以 INCLUDEPICTURE / LINK 开头的域指令清空（域结果保留占位）。
    没有可剥的东西时**原样返回原路径**（不产生临时文件）。

    只支持 .docx（OOXML = zip）。`.doc` 是复合二进制，改不了内容 —— 调用方记日志说明限制。
    """
    import zipfile  # noqa: PLC0415

    rel_external = re.compile(rb'<Relationship\b[^>]*TargetMode\s*=\s*"External"[^>]*/>', re.I)
    rel_id = re.compile(rb'\bId\s*=\s*"([^"]+)"')
    instr = re.compile(rb'<w:instrText\b[^>]*>.*?</w:instrText>', re.S | re.I)

    def _instr_inner(block: bytes) -> bytes:
        m = re.match(rb'<w:instrText\b[^>]*>(.*?)</w:instrText>', block, re.S | re.I)
        return m.group(1) if m else b""

    stripped = 0
    changed: dict[str, bytes] = {}
    external_ids: dict[str, set[bytes]] = {}

    with zipfile.ZipFile(src) as zin:
        names = zin.namelist()
        for name in names:
            if not (name.endswith(".rels") and "/_rels/" in name):
                continue
            raw = zin.read(name)
            hit_ids: set[bytes] = set()
            for m in rel_external.finditer(raw):
                mid = rel_id.search(m.group(0))
                if mid:
                    hit_ids.add(mid.group(1))
            if not hit_ids:
                continue
            body = rel_external.sub(b"", raw)
            changed[name] = body
            stripped += len(hit_ids)
            base_dir, rels_name = name.rsplit("/_rels/", 1)
            part = (base_dir + "/" if base_dir else "") + rels_name[:-len(".rels")]
            external_ids.setdefault(part, set()).update(hit_ids)

        # 部件里删属性 + 清域指令
        targets = set(external_ids)
        targets.update(n for n in names if n.startswith("word/") and n.endswith(".xml"))
        for name in sorted(targets):
            if name not in names:
                continue
            raw = changed.get(name) or zin.read(name)
            out = raw
            for rid in external_ids.get(name, ()):
                pat = re.compile(rb'\s[\w]+:(?:link|embed|id)\s*=\s*"' + re.escape(rid) + rb'"')
                out = pat.sub(b"", out)

            instr_hits = 0

            def _clear(m):
                nonlocal instr_hits
                inner = re.sub(rb"<[^>]+>", b"", _instr_inner(m.group(0))).strip()
                if inner.upper().startswith((b"INCLUDEPICTURE", b"LINK ")):
                    instr_hits += 1
                    head = m.group(0).split(b">", 1)[0]
                    return head + b"></w:instrText>"
                return m.group(0)

            cleaned = instr.sub(_clear, out)
            stripped += instr_hits
            if cleaned != raw:
                changed[name] = cleaned

    if not changed:
        return src, 0

    import tempfile  # noqa: PLC0415
    fd, out_path = tempfile.mkstemp(suffix=".docx", prefix="_hn_noref_")
    os.close(fd)
    try:
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out_path, "w") as zout:
            for item in zin.infolist():
                data = changed.get(item.filename)
                if data is None:
                    data = zin.read(item.filename)
                zi = zipfile.ZipInfo(item.filename, date_time=item.date_time)
                zi.compress_type = item.compress_type
                zi.external_attr = item.external_attr
                zout.writestr(zi, data)
    except Exception:
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise
    return out_path, stripped


def _prepare_office_src(file_path: str) -> tuple[str, str]:
    """转换前把「要交给引擎的路径」和「需要事后清理的临时副本」算出来。

    返回 `(src, temp_to_clean)`；没有净化动作时 `temp_to_clean == ""`（原样用原文件）。
    """
    ext = ext_of(file_path)
    if ext == ".docx":
        try:
            src, n = _strip_docx_external_refs(file_path)
        except Exception as e:  # noqa: BLE001 —— 净化是加固，失败不该拦住打印
            logger.warning(f"剥离 docx 外部引用失败，按原文件转换（风险已记录）: {e}")
            return file_path, ""
        if src != file_path:
            logger.info(f"已剥离 {n} 处 Office 外部引用（转换过程不访问外网）"
                        f" ({os.path.basename(file_path)})")
            return src, src
        return file_path, ""
    if ext == ".doc":
        logger.warning("老格式 .doc 是复合二进制，无法剥离文档内的外部引用；"
                       "如需彻底隔离，请把文档另存为 .docx 再打印")
    return file_path, ""


def _sanitize_office_refs(func):
    """入口装饰器：先净化（docx 外部引用），再交给引擎链；用完清理临时副本。

    放在装饰器里而不是改函数体：`tests/verify_office_automation_security.py` 会静态取
    `_convert_office_to_pdf` 的函数体核对「云端分支必须走 LibreOffice」，函数体保持不动最安全。
    """
    import functools  # noqa: PLC0415

    @functools.wraps(func)
    def wrapper(file_path: str, output_pdf: str, *args, **kwargs):
        src, temp = _prepare_office_src(file_path)
        try:
            return func(src, output_pdf, *args, **kwargs)
        finally:
            if temp:
                try:
                    os.remove(temp)
                except OSError:
                    pass

    return wrapper


@_sanitize_office_refs
def _convert_office_to_pdf(file_path: str, output_pdf: str,
                           from_cloud: bool = False,
                           allow_cloud_com: bool = False) -> None:
    """
    Office 文档 → PDF（多引擎智能降级）。

    Word 文档 (.doc/.docx):
      .docx 按最后编辑者决定首选引擎（WPS 编辑 → WPS 优先，Word/未知 → Word 优先）：
      WPS 的特殊布局（公式/文本框/域等）用 Word 引擎渲染会错位，必须用 WPS 自身引擎；
      首选引擎不可用时依次降级。
      1. 首选引擎（Word 或 WPS COM）
      2. 另一 COM 引擎
      3. LibreOffice 无头模式 → 兜底
    .doc（老格式，无 last-editor 元数据）: Word → WPS → LibreOffice

    其他 Office 格式 (.xls/.xlsx/.ppt/.pptx):
      → LibreOffice 无头模式

    Args:
        from_cloud: **来源标记（S1-04）**。True = 文件来自云端（顾客上传，内容不可信）
            ⇒ 跳过 Word/WPS COM，**强制 LibreOffice**（无 VBA 执行路径）；
            False（默认）= 用户本地文件 ⇒ 保持原三引擎降级链，行为零变化。
            ⚠ **默认值必须是 False**：把本地文件也赶去 LibreOffice 会让「门店装没装
            LibreOffice」从"只影响兜底"变成"直接影响首选渲染"，那是回归。
        allow_cloud_com: **「管理员自打 + 管理员上传」隐式规则的放行位**（2026-10-26 晚）。
            只有该任务确为 `is_admin_print`（订单类型「自打」）**且**文件由管理员上传
            （`uploader_is_admin`）时才由调用方置 True —— 判定本体在
            `printer_config.cloud_word_com_allowed()`，本函数只消费结论。
            为 True 时云端 .doc/.docx 走与本地文件相同的 COM 降级链（仍带
            AutomationSecurity=3 / DisplayAlerts=0 / UpdateLinksAtOpen=False 硬化）。
            ⚠ **默认值必须是 False**：默认仍强制 LibreOffice，不给顾客文件任何 COM 机会。
    """
    ext = ext_of(file_path)

    # ── 云端来源的 Office 文档：默认绝不交给 Word/WPS（S1-04）──
    # LibreOffice 无头转换不执行 VBA（也不跑 AutoOpen/Document_Open 宏），是这条链上
    # 唯一"内容不可信也敢打开"的引擎。装没装 LibreOffice 在这里变成硬前提，见下方报错。
    # ⚠ 唯一例外（2026-10-26 隐式规则）：该任务是「管理员自打」**且**文件由管理员上传时，
    #   调用方会传 allow_cloud_com=True —— 那时走下面的 COM 降级链（仍带宏/弹窗/外链硬化）。
    #   顾客上传的内容永远拿不到这个放行位。
    if from_cloud and ext in COM_OFFICE_EXTS and not allow_cloud_com:
        if not _find_libreoffice():
            raise RuntimeError(
                "云端来源的 Office 文档（.doc/.docx）强制用 LibreOffice 转换（不执行宏），"
                "但本机未安装 / 未找到 LibreOffice。\n"
                "请安装 LibreOffice 后再打印云端 Word 文档。\n"
                "下载地址: https://www.libreoffice.org/download/"
            )
        logger.info(
            f"引擎选择: LibreOffice（云端来源强制，跳过 Word/WPS COM 以杜绝宏执行）"
            f" ({os.path.basename(file_path)})")
        _convert_office_via_libreoffice(file_path, output_pdf)
        return

    # 云端来源 + 隐式放行（管理员自打 + 管理员上传）：显式记一条 WARNING，事后能从日志分辨
    # 「这单为什么没走 LibreOffice」，也让安全口径（谁在什么情况下放开的）可追溯。
    if from_cloud and ext in COM_OFFICE_EXTS and allow_cloud_com:
        logger.warning(
            "引擎策略：云端 Word 文档按「管理员自打 + 管理员上传」隐式规则允许 Word/WPS COM"
            f"（COM 已硬化：禁用宏/弹窗/外链更新）: {os.path.basename(file_path)}")

    # Word 文档：多引擎降级
    if ext in COM_OFFICE_EXTS:
        # docx 优先用最后编辑它的引擎渲染（WPS 特殊布局须用 WPS，Word 渲染会错位）
        preferred = "word"
        if ext == ".docx":
            last_editor = _read_docx_last_editor(file_path)
            if last_editor == "wps":
                preferred = "wps"

        engines_order = [preferred] + [e for e in ("word", "wps") if e != preferred]
        for eng in engines_order:
            if eng == "word" and _detect_word():
                try:
                    logger.info(f"引擎选择: Microsoft Word COM ({os.path.basename(file_path)})")
                    _convert_via_word_com(file_path, output_pdf)
                    if _pdf_product_ok(output_pdf):
                        return
                    # COM「静默成功」：没抛异常但产物是空的/不是 PDF ⇒ 丢掉残次品继续降级
                    logger.warning(
                        "Microsoft Word 报告成功但产物无效（空 / 非 PDF），降级到下一引擎")
                    _discard_invalid_pdf(output_pdf)
                except Exception as e:
                    logger.warning(f"Microsoft Word 转换失败，降级到下一引擎: {e}")
            elif eng == "wps" and _detect_wps():
                try:
                    logger.info(f"引擎选择: WPS Office COM ({os.path.basename(file_path)})")
                    _convert_via_wps_com(file_path, output_pdf)
                    if _pdf_product_ok(output_pdf):
                        return
                    logger.warning(
                        "WPS Office 报告成功但产物无效（空 / 非 PDF），降级到下一引擎")
                    _discard_invalid_pdf(output_pdf)
                except Exception as e:
                    logger.warning(f"WPS Office 转换失败，降级到下一引擎: {e}")

        # ── 引擎 3: LibreOffice ──
        logger.info(f"引擎选择: LibreOffice (降级) ({os.path.basename(file_path)})")

    # 其他 Office 格式 / Word 文档最终降级
    _convert_office_via_libreoffice(file_path, output_pdf)


def _convert_office_via_libreoffice(file_path: str, output_pdf: str) -> None:
    """
    Office 文档 → PDF（LibreOffice 无头模式）。
    支持 .doc / .docx / .xls / .xlsx / .ppt / .pptx
    """
    libreoffice = _find_libreoffice()
    if not libreoffice:
        raise RuntimeError(
            "未找到 LibreOffice。请安装 LibreOffice 来转换 Office 文档。\n"
            "下载地址: https://www.libreoffice.org/download/"
        )

    abs_input = os.path.abspath(file_path)

    # P1-5: 每次调用使用独立输出子目录（唯一），避免并发转换同名 basename 互相覆盖
    work_dir = tempfile.mkdtemp(prefix="hn_lo_")

    # P2-5: 超时随文件大小放大（60s 起步，5s/MB，上限 600s）
    file_size_mb = os.path.getsize(abs_input) / (1024 * 1024)
    lo_timeout = min(max(60, int(file_size_mb * 5)), 600)

    cmd = [
        libreoffice,
        "--headless",
        "--convert-to", "pdf",
        "--outdir", work_dir,
        abs_input,
    ]

    logger.info(f"执行 LibreOffice 转换: {' '.join(cmd)} (超时 {lo_timeout}s)")

    try:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=lo_timeout,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"LibreOffice 转换超时（{lo_timeout} 秒）")
        except FileNotFoundError:
            raise RuntimeError(f"LibreOffice 可执行文件未找到: {libreoffice}")

        if result.returncode != 0:
            raise RuntimeError(f"LibreOffice 转换失败 (rc={result.returncode}): {result.stderr}")

        # LibreOffice 输出的 PDF 文件名 = 原始文件名（改扩展名 .pdf）
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        generated_pdf = os.path.join(work_dir, base_name + ".pdf")

        if not os.path.isfile(generated_pdf):
            raise RuntimeError(f"LibreOffice 未生成预期的 PDF: {generated_pdf}")

        # 如果输出路径与生成路径不同，移动/重命名
        if os.path.abspath(generated_pdf) != os.path.abspath(output_pdf):
            if os.path.exists(output_pdf):
                os.remove(output_pdf)
            shutil.move(generated_pdf, output_pdf)

        logger.info(f"LibreOffice → PDF 完成: {output_pdf}")
    finally:
        # P1-5: 无论成功失败都清理本次独立输出目录
        shutil.rmtree(work_dir, ignore_errors=True)


# ============================================================
# 字体工具
# ============================================================

_chinese_font_registered = False
_chinese_font_name = "Helvetica"


def _register_chinese_font() -> None:
    """注册系统中可用的中文字体到 reportlab。"""
    global _chinese_font_registered, _chinese_font_name
    if _chinese_font_registered:
        return

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    system = platform.system()

    # 字体候选列表：(字体名, 路径列表)
    candidates: list[tuple[str, list[str]]] = []
    if system == "Windows":
        candidates = [
            ("Microsoft YaHei", [
                os.path.expandvars(r"%SystemRoot%\Fonts\msyh.ttc"),
                os.path.expandvars(r"%SystemRoot%\Fonts\msyh.ttf"),
            ]),
            ("SimSun", [
                os.path.expandvars(r"%SystemRoot%\Fonts\simsun.ttc"),
                os.path.expandvars(r"%SystemRoot%\Fonts\simsun.ttf"),
            ]),
            ("SimHei", [
                os.path.expandvars(r"%SystemRoot%\Fonts\simhei.ttf"),
            ]),
        ]
    elif system == "Darwin":
        candidates = [
            ("PingFang SC", [
                "/System/Library/Fonts/PingFang.ttc",
                "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            ]),
        ]
    else:
        candidates = [
            ("Noto Sans CJK SC", [
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
            ]),
            ("WenQuanYi Micro Hei", [
                "/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc",
            ]),
            ("Droid Sans Fallback", [
                "/usr/share/fonts/droid/DroidSansFallbackFull.ttf",
            ]),
        ]

    for font_name, paths in candidates:
        for path in paths:
            if os.path.isfile(path):
                try:
                    pdfmetrics.registerFont(TTFont(font_name, path))
                    _chinese_font_name = font_name
                    _chinese_font_registered = True
                    logger.info(f"已注册中文字体: {font_name} ({path})")
                    return
                except Exception as e:
                    logger.warning(f"注册字体失败 {font_name} ({path}): {e}")
                    continue

    # 回退
    logger.warning("未找到任何可用中文字体，将使用 Helvetica（中文可能无法正常显示）")
    _chinese_font_registered = True


def _get_chinese_font_name() -> str:
    return _chinese_font_name


def _sanitize_non_ascii(text: str) -> str:
    """无中文字体（Helvetica 回退）时，将非 ASCII 字符替换为 '□'。

    避免 reportlab drawString/Paragraph 对 Helvetica(WinAnsi) 写中文时抛 UnicodeEncodeError；
    已注册中文字体时原样返回。
    """
    if _chinese_font_name != "Helvetica":
        return text
    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        return "".join(ch if ord(ch) < 128 else "□" for ch in text)


def _wrap_text_line(text: str, font_name: str, font_size: int, max_width: float,
                    canvas_obj) -> list[str]:
    """将单行文本按宽度自动换行，返回分行列表。

    先按最长 120 字符切块处理：每次 stringWidth 测宽不超过 120 字符，
    避免超长单词（如 URL）逐字符累加测宽导致 O(n²) 卡死；
    块内仍按原逻辑逐字符断行，`current` 跨块延续，正常断词不受影响。
    """
    result: list[str] = []
    current = ""
    MAX_CHUNK = 120  # 单次测宽的最大字符数（超长词硬截断上限）

    for chunk_start in range(0, len(text), MAX_CHUNK):
        chunk = text[chunk_start:chunk_start + MAX_CHUNK]
        # 快速路径：无未完成行且整块宽度不超限 → 直接成行（避免逐字符测宽）
        if not current and canvas_obj.stringWidth(chunk, font_name, font_size) <= max_width:
            result.append(chunk)
            continue
        for ch in chunk:
            test = current + ch
            w = canvas_obj.stringWidth(test, font_name, font_size)
            if w > max_width and current:
                result.append(current)
                current = ch
            else:
                current = test
        # 安全兜底：即使 stringWidth 异常返回 0，current 也不会无限增长
        if len(current) >= MAX_CHUNK:
            result.append(current)
            current = ""

    if current:
        result.append(current)
    return result if result else [""]


# ============================================================
# UniversalConverter
# ============================================================

class UniversalConverter:
    """
    通用文件转 PDF 转换器。
    根据文件扩展名自动选择转换策略。
    """

    # 扩展名 → 转换函数 映射
    CONVERSION_MAP: dict[str, Callable[[str, str], None]] = {}

    def __init__(self) -> None:
        self._init_conversion_map()

    def _init_conversion_map(self) -> None:
        """初始化扩展名→转换函数映射。"""
        cm = self.CONVERSION_MAP
        # TXT / 文本类
        cm[".txt"] = _convert_txt_to_pdf
        cm[".csv"] = _convert_csv_to_pdf
        # 图片类（TIFF 多帧每帧一页）
        for ext in (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"):
            cm[ext] = _convert_image_to_pdf
        # Office 文档
        for ext in (".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"):
            cm[ext] = _convert_office_to_pdf
        # Markdown（HTML 支持按 D-2 **永久删除**：`.html/.htm` 不再注册；
        # `_convert_html_to_pdf` 保留，仅供 Markdown 内部调用）
        cm[".md"] = _convert_markdown_to_pdf

    def convert(self, file_path: str, output_pdf: str | None = None,
                image_orientation: str = "auto", from_cloud: bool = False,
                allow_cloud_com: bool = False) -> str:
        """
        将文件转换为 PDF。

        Args:
            file_path: 源文件路径（含扩展名）
            output_pdf: 输出 PDF 路径；若为 None 则自动生成临时文件
            image_orientation: 图片方向（'auto' | 'landscape' | 'portrait'），
                仅图片类生效；其余文件类型忽略。
            from_cloud: **文件来源（S1-04）**。True = 云端下载来的文件（顾客控制内容）
                ⇒ Office 文档强制走 LibreOffice（无宏执行），绝不交给 Word/WPS COM。
                **默认 False = 本地文件语义**，三引擎降级链与之前完全一致。
                云端调用点：`cloud_client.py` 页数分析、`gui.ConvertWorker`（云端任务
                预转换）、`gui.PrintWorker`（job.task_id > 0 的兜底转换）。
            allow_cloud_com: 仅当「本机策略允许 + 任务确为管理员自打单」时置 True
                （见 `_convert_office_to_pdf`）；默认 False = 云端 Word 仍强制 LibreOffice。
                页数分析路径**永远不传**（上传时还不知道订单归属，按最严口径走 LO）。

        Returns:
            生成的 PDF 文件路径

        Raises:
            ValueError: 不支持的文件类型
            FileNotFoundError: 源文件不存在
            RuntimeError: 转换失败
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        ext = ext_of(file_path)

        # PDF 无需转换
        if ext == ".pdf":
            if output_pdf and os.path.abspath(file_path) != os.path.abspath(output_pdf):
                shutil.copy2(file_path, output_pdf)
                return output_pdf
            return file_path

        convert_func = self.CONVERSION_MAP.get(ext)
        if convert_func is None:
            raise ValueError(f"不支持的文件类型: {ext}  (文件: {file_path})")

        created_temp = False
        if output_pdf is None:
            fd, output_pdf = tempfile.mkstemp(suffix=".pdf", prefix="_conv_")
            os.close(fd)
            created_temp = True

        try:
            logger.info(f"开始转换: {os.path.basename(file_path)} ({ext}) → PDF"
                        + ("（云端来源：Office 强制 LibreOffice）"
                           if from_cloud and not allow_cloud_com else "")
                        + ("（云端来源：管理员自打单放行 COM）"
                           if from_cloud and allow_cloud_com else ""))
            # 图片方向可配置：把 image_orientation 传给图片渲染函数（其余类型忽略）
            if convert_func is _convert_image_to_pdf:
                convert_func(file_path, output_pdf, image_orientation)
            elif convert_func is _convert_office_to_pdf:
                # S2-03：本地 Office 文件优先在 Job Object 隔离 worker 里转换（默认关闭）。
                # 注意「失败闭环」：沙箱**跑起来后**失败/超时绝不回退进程内转换（恶意文档
                # 正是靠"卡死 → 回退"把风险带回主进程）；只有沙箱本身不可用才回退。
                if should_sandbox(file_path, from_cloud=from_cloud):
                    import office_sandbox  # noqa: PLC0415
                    _res = office_sandbox.run_office_conversion(
                        file_path, output_pdf, image_orientation=image_orientation)
                    if _res.status == "ok":
                        logger.info(f"引擎选择: 隔离沙箱 worker（Job Object）"
                                    f" ({os.path.basename(file_path)})")
                        return output_pdf
                    if _res.status == "unavailable":
                        logger.warning(f"转换沙箱不可用，回退进程内转换: {_res.error}")
                        convert_func(file_path, output_pdf, from_cloud=from_cloud,
                                     allow_cloud_com=allow_cloud_com)
                    else:
                        raise RuntimeError(
                            f"隔离转换失败（{_res.status}）：{_res.error}。"
                            f"若该文档在其它机器能正常转换，请把日志里的 office_sandbox 行交给管理员。")
                else:
                    # S1-04：来源标记只影响 Office 转换引擎的选择（云端 → LibreOffice）
                    # 2026-10-26 方案 B：管理员自打单可按本机策略放行 COM（allow_cloud_com）
                    convert_func(file_path, output_pdf, from_cloud=from_cloud,
                                 allow_cloud_com=allow_cloud_com)
            else:
                convert_func(file_path, output_pdf)

            # 口径与降级链内部**同一份实现**（_pdf_product_ok）：存在 + 非空 + %PDF 头
            if not _pdf_product_ok(output_pdf):
                raise RuntimeError(f"转换为 PDF 后产物无效（空 / 不存在 / 非 PDF）: {output_pdf}")

            return output_pdf
        except Exception:
            if created_temp and output_pdf and os.path.isfile(output_pdf):
                try:
                    os.remove(output_pdf)
                    logger.debug(f"已清理失败的临时 PDF: {output_pdf}")
                except OSError:
                    pass
            raise


# 模块级单例
_converter_instance: UniversalConverter | None = None


def get_converter() -> UniversalConverter:
    """获取 UniversalConverter 单例。"""
    global _converter_instance
    if _converter_instance is None:
        _converter_instance = UniversalConverter()
    return _converter_instance
