#!/usr/bin/env python3
"""
HN 云打印 — 打印机客户端（Windows GDI 直打）

用法:
    python printer_client.py

依赖:
    pip install -r requirements.txt
"""

import json
import os
import re
import signal
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime

import requests as http_requests
import socketio as socketio_lib

# ---------- 加载配置 ----------

try:
    import config

    CLOUD_API_URL = config.CLOUD_API_URL
    WEBSOCKET_URL = config.WEBSOCKET_URL
    TOKEN = config.TOKEN
    PRINTER_NAME = config.PRINTER_NAME
    DUPLEX_MODE = getattr(config, "DUPLEX_MODE", "long-edge")
except ImportError:
    print("[FAIL] 未找到 config.py，请在 printer_client/ 目录下创建 config.py")
    sys.exit(1)
except AttributeError as e:
    print(f"[FAIL] config.py 缺少配置项: {e}")
    sys.exit(1)

# ---------- 路径初始化 ----------

# 切换到脚本所在目录，确保 PyMuPDF 等依赖能找到 static/ 等资源
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ---------- 常量 ----------

HEARTBEAT_INTERVAL = 30
RECONNECT_BASE_DELAY = 2
RECONNECT_MAX_DELAY = 120
DOWNLOAD_DIR = tempfile.gettempdir()
HOSTNAME = socket.gethostname()

# DEVMODE dmDuplex 常量
DMDUP_SIMPLEX = 1
DMDUP_VERTICAL = 2
DMDUP_HORIZONTAL = 3

DUPLEX_MAP = {
    "simplex": DMDUP_SIMPLEX,
    "long-edge": DMDUP_VERTICAL,
    "short-edge": DMDUP_HORIZONTAL,
}

# ---------- 全局状态 ----------

sio: socketio_lib.Client | None = None
stop_event = threading.Event()
reconnect_count = 0
heartbeat_timer: threading.Timer | None = None
print_lock = threading.Lock()  # 防止并发打印


# ---------- 日志 ----------

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ---------- 打印页码范围解析 ----------

def parse_page_range(page_range: str | None) -> list[int] | None:
    """
    解析页码范围字符串，返回去重排序后的页码列表。
    支持格式:
      - 空 / None → None（打印全部）
      - "1-5" → [1, 2, 3, 4, 5]
      - "1,2,4" → [1, 2, 4]
      - "1-5,7,9" → [1, 2, 3, 4, 5, 7, 9]
      - 中文逗号/顿号: "1-5、7、9" 同样支持
    返回 None 表示无需筛选（打印全部）。
    """
    if not page_range or not page_range.strip():
        return None

    raw = page_range.strip()
    raw = raw.replace("、", ",").replace("，", ",").replace(" ", "").replace("；", ",")

    pages: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                start_s, end_s = part.split("-", 1)
                start = int(start_s)
                end = int(end_s)
                if start < 1:
                    start = 1
                for p in range(start, end + 1):
                    pages.add(p)
            except ValueError:
                continue
        else:
            try:
                p = int(part)
                if p > 0:
                    pages.add(p)
            except ValueError:
                continue

    if not pages:
        return None
    return sorted(pages)


# ---------- 文件类型检测 ----------

def detect_file_type(file_path: str) -> str:
    """返回文件类型: 'pdf' | 'image' | 'office' | 'other'"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        return "pdf"
    if ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".tif", ".webp"):
        return "image"
    if ext in (".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"):
        return "office"
    return "other"


# ---------- Office → PDF 转换（COM） ----------

# Office SaveAs PDF 格式常量
_WORD_PDF = 17       # wdFormatPDF
_EXCEL_PDF = 0       # xlTypePDF
_PPT_PDF = 32        # ppSaveAsPDF

_OFFICE_PROGIDS = {
    ".docx": "Word.Application",
    ".doc": "Word.Application",
    ".xlsx": "Excel.Application",
    ".xls": "Excel.Application",
    ".pptx": "PowerPoint.Application",
    ".ppt": "PowerPoint.Application",
}

_OFFICE_PDF_FORMATS = {
    "Word.Application": 17,          # wdFormatPDF
    "Excel.Application": 0,          # xlTypePDF
    "PowerPoint.Application": 32,    # ppSaveAsPDF
}


def convert_office_to_pdf(file_path: str) -> str | None:
    """
    通过 Windows COM 将 Office 文档转为 PDF。
    成功返回临时 PDF 路径，失败返回 None。
    """
    try:
        import pythoncom
        import win32com.client
    except ImportError as e:
        log(f"  [CONVERT] 缺少 pywin32 模块，无法转换 Office 文档: {e}")
        log(f"  [CONVERT] 请运行: pip install pywin32")
        return None

    ext = os.path.splitext(file_path)[1].lower()
    progid = _OFFICE_PROGIDS.get(ext)
    if not progid:
        log(f"  [CONVERT] 不支持的文件类型: {ext}")
        return None

    pdf_format = _OFFICE_PDF_FORMATS.get(progid, 17)
    temp_pdf = os.path.join(tempfile.gettempdir(), f"_conv_{os.getpid()}_{int(time.time())}.pdf")
    app = None

    try:
        pythoncom.CoInitialize()
        log(f"  [CONVERT] 启动 {progid} 转换 {os.path.basename(file_path)} → PDF")

        app = win32com.client.Dispatch(progid)
        try:
            app.Visible = False
        except Exception:
            pass  # PowerPoint 等应用可能不允许隐藏窗口
        try:
            app.DisplayAlerts = False
        except Exception:
            pass

        if progid == "Word.Application":
            doc = app.Documents.Open(os.path.abspath(file_path), ReadOnly=True)
            doc.SaveAs(os.path.abspath(temp_pdf), FileFormat=pdf_format)
            doc.Close()
        elif progid == "Excel.Application":
            wb = app.Workbooks.Open(os.path.abspath(file_path), ReadOnly=True)
            wb.ExportAsFixedFormat(pdf_format, os.path.abspath(temp_pdf))
            wb.Close()
        elif progid == "PowerPoint.Application":
            pres = app.Presentations.Open(os.path.abspath(file_path), ReadOnly=True)
            pres.SaveAs(os.path.abspath(temp_pdf), pdf_format)
            pres.Close()

        log(f"  [CONVERT] 转换完成: {temp_pdf} ({os.path.getsize(temp_pdf)} bytes)")
        return temp_pdf

    except Exception as e:
        log(f"  [CONVERT] Office 转换失败: {e}")
        if temp_pdf and os.path.exists(temp_pdf):
            try:
                os.remove(temp_pdf)
            except OSError:
                pass
        return None
    finally:
        if app:
            try:
                app.Quit()
            except Exception:
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


# ---------- PDF / 图片 → 位图渲染 ----------

def render_pages(file_path: str, file_type: str, page_range: str | None = None) -> tuple[list, str]:
    """
    将文件渲染为 PIL Image 列表。
    - PDF: PyMuPDF 逐页渲染
    - 图片: Pillow 直接加载
    返回 (images, error)，成功时 error 为空字符串。
    """
    pages = parse_page_range(page_range)

    if file_type == "pdf":
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(file_path)
            total = doc.page_count
            if pages:
                valid = [p for p in pages if p <= total]
                if not valid:
                    doc.close()
                    return [], f"页码范围 {page_range} 超出文件总页数 {total}"
            else:
                valid = list(range(1, total + 1))

            images = []
            for pnum in valid:
                page = doc[pnum - 1]  # PyMuPDF 0-indexed
                # 300 DPI 渲染为 RGB pixmap
                mat = fitz.Matrix(300 / 72, 300 / 72)
                pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
                from PIL import Image as PILImage
                img = PILImage.frombytes("RGB", [pix.width, pix.height], pix.samples)
                images.append(img)

            doc.close()
            log(f"  [RENDER] PDF 渲染 {len(images)} 页 (共 {total} 页)")
            return images, ""

        except Exception as e:
            return [], f"PDF 渲染失败: {e}"

    elif file_type == "image":
        try:
            from PIL import Image as PILImage
            img = PILImage.open(file_path)
            # 统一转 RGB（处理 RGBA / CMYK / indexed 等模式）
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            if pages:
                log(f"  [RENDER] 图片文件忽略页码范围")
            log(f"  [RENDER] 图片加载: {img.size[0]}x{img.size[1]}")
            return [img], ""
        except Exception as e:
            return [], f"图片加载失败: {e}"

    return [], f"不支持的文件类型: {file_type}"


# ---------- GDI 打印 ----------

def gdi_print_pages(images, copies: int, duplex: str, printer_name: str, job_name: str) -> tuple[bool, str]:
    """
    通过 Windows GDI 直接打印 PIL Image 列表。
    份数和双面由 DEVMODE 控制，打印机原生处理。
    images: PIL Image 列表
    copies: 份数
    duplex: 'on' | 'off'（映射到 DUPLEX_MODE 配置的双面方式）
    """
    if not images:
        return False, "无页面可打印"

    try:
        import win32print
        import win32ui
        import win32con
        from PIL import ImageWin
    except ImportError as e:
        return False, f"缺少依赖: {e} (请运行 pip install pywin32 Pillow)"

    hprinter = None
    hdc = None

    try:
        # 1. 打开打印机，获取可修改的 DEVMODE
        hprinter = win32print.OpenPrinter(printer_name)

        devmode = win32print.GetPrinter(hprinter, 2)["pDevMode"]

        # 份数
        if copies > 1:
            devmode.Copies = copies
            devmode.Fields = devmode.Fields | win32con.DM_COPIES
        # 双面
        if duplex == "on":
            duplex_val = DUPLEX_MAP.get(DUPLEX_MODE, DMDUP_VERTICAL)
            devmode.Duplex = duplex_val
        else:
            devmode.Duplex = DMDUP_SIMPLEX
        devmode.Fields = devmode.Fields | win32con.DM_DUPLEX

        # 2. 将修改后的 DEVMODE 写回打印机
        win32print.DocumentProperties(0, hprinter, printer_name, devmode, devmode,
                                      win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER)

        # 3. 创建打印机 DC（自动应用上述修改）
        hdc = win32ui.CreateDC()
        hdc.CreatePrinterDC(printer_name)
        hdc.StartDoc(job_name)

        printer_width = hdc.GetDeviceCaps(win32con.HORZRES)
        printer_height = hdc.GetDeviceCaps(win32con.VERTRES)
        log(f"  [GDI] 打印机页面: {printer_width}x{printer_height} px, "
            f"份数: {copies}, 双面: {duplex}, 页数: {len(images)}")

        # 3. 逐页绘制
        for idx, pil_image in enumerate(images):
            hdc.StartPage()

            iw, ih = pil_image.size
            # 等比缩放填满打印区域
            scale = min(printer_width / iw, printer_height / ih)
            tw = int(iw * scale)
            th = int(ih * scale)
            # 居中偏移
            ox = max(0, (printer_width - tw) // 2)
            oy = max(0, (printer_height - th) // 2)

            dib = ImageWin.Dib(pil_image)
            dib.draw(hdc.GetHandleOutput(), (ox, oy, ox + tw, oy + th))

            hdc.EndPage()
            log(f"  [GDI] 第 {idx + 1}/{len(images)} 页已渲染")

        hdc.EndDoc()
        log(f"  [GDI] 打印作业完成")

        return True, ""

    except Exception as e:
        return False, f"GDI 打印失败: {e}"
    finally:
        if hdc:
            try:
                hdc.DeleteDC()
            except Exception:
                pass
        if hprinter:
            try:
                win32print.ClosePrinter(hprinter)
            except Exception:
                pass


# ---------- 统一打印入口 ----------

def print_file(file_path: str, copies: int = 1, duplex: str = "on",
               page_range: str | None = None) -> tuple[bool, str]:
    """
    统一打印入口：检测文件类型 → Office 转 PDF → 渲染位图 → GDI 直打。
    """
    if not os.path.exists(file_path):
        return False, f"文件不存在: {file_path}"

    fname = os.path.basename(file_path)
    file_type = detect_file_type(file_path)
    page_info = f", 范围: {page_range}" if page_range else ""

    log(f"  [PRINT] 文件: {fname}, 类型: {file_type}, 份数: {copies}, "
        f"双面: {duplex}{page_info}")

    # ---- Office 文档先转 PDF ----
    print_path = file_path
    temp_pdf = None
    if file_type == "office":
        temp_pdf = convert_office_to_pdf(file_path)
        if not temp_pdf:
            return False, "Office 文档转 PDF 失败"
        # 测试阶段：在桌面留一份转换后的 PDF
        try:
            import shutil
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            pdf_copy = os.path.join(desktop, f"[PDF]_" + os.path.splitext(fname)[0] + ".pdf")
            shutil.copy2(temp_pdf, pdf_copy)
            log(f"  [TEST] PDF 副本已保存到桌面: {os.path.basename(pdf_copy)}")
        except Exception as e:
            log(f"  [TEST] 保存 PDF 副本失败: {e}")
        print_path = temp_pdf
        file_type = "pdf"  # 后续按 PDF 处理
        # 页码范围在渲染阶段应用（对转换后的 PDF）
    elif file_type == "other":
        # 非 Office/PDF/图片 → 尝试转为 PDF（部分文件可通过 Office 打开）
        # 降级：无法处理
        return False, f"不支持的文件类型: {os.path.splitext(file_path)[1]}"

    # ---- 渲染为位图 ----
    try:
        images, error = render_pages(print_path, file_type, page_range)
    finally:
        if temp_pdf and os.path.exists(temp_pdf):
            try:
                os.remove(temp_pdf)
            except OSError:
                pass

    if error:
        return False, error

    # ---- GDI 打印 ----
    job_name = f"HN_Print - {fname}"
    return gdi_print_pages(images, copies, duplex, PRINTER_NAME, job_name)


# ---------- 下载文件 ----------

def download_file(url: str, task_id: int) -> str | None:
    """从后端下载文件到临时目录，保留原始文件名（含扩展名）"""
    try:
        log(f"  [DOWNLOAD] {url}")

        resp = http_requests.get(url, timeout=60, stream=True)
        resp.raise_for_status()

        # 从 Content-Disposition 响应头提取原始文件名（含扩展名）
        original_name = None
        cd = resp.headers.get("Content-Disposition", "")
        if "filename=" in cd:
            m = re.search(r'filename[*]?=(?:UTF-8\'\')?(?:"([^"]+)"|([^;]+))', cd, re.I)
            if m:
                original_name = m.group(1) or m.group(2)

        if not original_name:
            original_name = f"print_task_{task_id}.dat"

        dest = os.path.join(DOWNLOAD_DIR, f"{task_id}_{original_name}")
        log(f"  [DOWNLOAD] → {dest}")

        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        log(f"  [OK] 下载完成: {os.path.getsize(dest)} bytes")
        return dest
    except Exception as e:
        log(f"  [FAIL] 下载失败: {e}")
        return None


# ---------- 处理打印任务 ----------

def handle_print_task(task: dict):
    """处理单个打印任务：下载 → 打印 → 上报结果"""
    task_id = task.get("task_id")
    file_url = task.get("file_url", task.get("download_url", ""))
    options = task.get("options", {})
    copies = int(options.get("copies", 1))
    duplex = options.get("duplex", "on")
    page_range = options.get("page_range", "") or ""

    log(f"[TASK] 处理任务 #{task_id}: copies={copies}, duplex={duplex}"
        + (f", page_range={page_range}" if page_range else ""))

    if not file_url:
        log("  [FAIL] 缺少 file_url")
        sio.emit("print_fail", {"task_id": task_id, "error": "缺少 file_url"})
        return

    with print_lock:
        local_path = download_file(file_url, task_id)
        if not local_path:
            sio.emit("print_fail", {"task_id": task_id, "error": "文件下载失败"})
            return

        # ---- Excel 表格特殊处理：不支持自动打印，转存到桌面 ----
        ext = os.path.splitext(local_path)[1].lower()
        if ext in (".xls", ".xlsx"):
            # 优先用后端传来的原始文件名，否则从下载文件名剥离 task_id 前缀
            original_name = task.get("file_name", "")
            if not original_name:
                basename = os.path.basename(local_path)
                prefix = f"{task_id}_"
                original_name = basename[len(prefix):] if basename.startswith(prefix) else basename
            log(f"  [EXCEL] Excel 表格不支持自动打印，请联系管理员进行手动打印")
            try:
                import shutil
                desktop = os.path.join(os.path.expanduser("~"), "Desktop")
                manual_dir = os.path.join(desktop, "需手动处理的打印任务")
                os.makedirs(manual_dir, exist_ok=True)
                dest = os.path.join(manual_dir, original_name)
                shutil.copy2(local_path, dest)
                log(f"  [EXCEL] 文件已转存到: {dest}")
            except Exception as e:
                log(f"  [EXCEL] 转存失败: {e}")
            finally:
                try:
                    os.remove(local_path)
                except OSError:
                    pass
            sio.emit("print_success", {"task_id": task_id})
            log(f"  [OK] 任务 #{task_id} 已转存到桌面")
            return

        try:
            success, error = print_file(local_path, copies, duplex, page_range)
        except Exception as e:
            success, error = False, str(e)
            log(f"  [ERROR] 打印异常: {e}")
        finally:
            try:
                os.remove(local_path)
            except OSError:
                pass

    if success:
        sio.emit("print_success", {"task_id": task_id})
        log(f"  [OK] 任务 #{task_id} 完成")
    else:
        sio.emit("print_fail", {"task_id": task_id, "error": error})
        log(f"  [FAIL] 任务 #{task_id}: {error}")


# ---------- 拉取排队任务 ----------

def pull_and_process_queued_orders():
    """启动时拉取所有排队任务，按顺序处理"""
    log("[QUEUE] 正在拉取排队任务...")
    try:
        resp = http_requests.get(
            f"{CLOUD_API_URL}/api/pull_queued_orders",
            params={"token": TOKEN},
            timeout=10,
        )
        # 先检查 HTTP 状态码，再尝试解析 JSON
        if not resp.ok:
            log(f"[QUEUE] HTTP {resp.status_code}, body={resp.text[:300]}")
            return
        try:
            data = resp.json()
        except Exception:
            log(f"[QUEUE] JSON 解析失败, HTTP {resp.status_code}, body={resp.text[:300]}")
            return
        if not data.get("success"):
            log(f"[QUEUE] 拉取失败: {data.get('message', '未知错误')}")
            return

        orders = data.get("orders", [])
        if not orders:
            log("[QUEUE] 无排队任务")
            return

        log(f"[QUEUE] 拉取到 {len(orders)} 个排队任务，开始处理...")
        for order in orders:
            if stop_event.is_set():
                break
            handle_print_task(order)
        log("[QUEUE] 排队任务处理完毕")
    except Exception as e:
        log(f"[QUEUE] 拉取排队任务异常: {e}")


# ---------- 心跳 ----------

def schedule_heartbeat():
    global heartbeat_timer
    if stop_event.is_set():
        return
    heartbeat_timer = threading.Timer(HEARTBEAT_INTERVAL, send_heartbeat)
    heartbeat_timer.daemon = True
    heartbeat_timer.start()


def send_heartbeat():
    if sio and sio.connected:
        sio.emit("ping")
        log("ping")
    schedule_heartbeat()


# ---------- 连接管理 ----------

def connect_sio():
    global sio, reconnect_count
    if sio:
        try:
            sio.disconnect()
        except Exception:
            pass

    sio = socketio_lib.Client(
        reconnection=False,
        logger=False,
        engineio_logger=False,
    )

    @sio.on("connect")
    def _on_connect():
        global reconnect_count
        reconnect_count = 0
        log("[LINK] 已连接到云端")
        schedule_heartbeat()

        # 连接后立即拉取并处理排队任务（后台线程，不阻塞心跳）
        t = threading.Thread(target=pull_and_process_queued_orders, daemon=True)
        t.start()

    @sio.on("disconnect")
    def _on_disconnect():
        global heartbeat_timer
        log("[LINK] 已断开连接")
        if heartbeat_timer:
            heartbeat_timer.cancel()
            heartbeat_timer = None

        if not stop_event.is_set():
            delay = min(RECONNECT_BASE_DELAY * (2 ** reconnect_count), RECONNECT_MAX_DELAY)
            log(f"[WAIT] {delay}s 后重连...")
            time.sleep(delay)
            reconnect_count += 1
            connect_sio()

    @sio.on("print_task")
    def _on_print_task(data):
        # 收到实时推送任务，在独立线程中处理
        t = threading.Thread(target=handle_print_task, args=(data,), daemon=True)
        t.start()

    @sio.on("pong")
    def _on_pong():
        pass

    @sio.on("auth_fail")
    def _on_auth_fail(data):
        log(f"[FAIL] 认证失败: {data.get('message', '未知原因')}")
        stop_event.set()
        sio.disconnect()

    connect_url = f"{WEBSOCKET_URL}?token={TOKEN}&client_id={HOSTNAME}"
    log(f"[NET] 正在连接 {WEBSOCKET_URL} ...")
    try:
        sio.connect(connect_url, wait_timeout=10)
    except Exception as e:
        log(f"[WARN] 连接失败: {e}")
        if not stop_event.is_set():
            delay = min(RECONNECT_BASE_DELAY * (2 ** reconnect_count), RECONNECT_MAX_DELAY)
            log(f"[WAIT] {delay}s 后重连...")
            time.sleep(delay)
            reconnect_count += 1
            connect_sio()


# ---------- 信号处理 ----------

def shutdown(signum=None, frame=None):
    log("[STOP] 正在关闭客户端...")
    stop_event.set()

    global heartbeat_timer
    if heartbeat_timer:
        heartbeat_timer.cancel()

    if sio:
        sio.disconnect()

    log("[BYE] 已退出")


# ---------- 入口 ----------

if __name__ == "__main__":
    print("=" * 50)
    print("  [PRINTER]  HN 云打印 — 打印机客户端 (GDI 直打)")
    print("=" * 50)
    print(f"  云端 API: {CLOUD_API_URL}")
    print(f"  WebSocket: {WEBSOCKET_URL}")
    print(f"  打印机: {PRINTER_NAME}")
    print(f"  双面模式: {DUPLEX_MODE}")
    print(f"  下载目录: {DOWNLOAD_DIR}")
    print(f"  客户端 ID: {HOSTNAME}")
    print("=" * 50)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    connect_sio()

    try:
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
