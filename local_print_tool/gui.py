"""
gui.py — PySide6 主界面 MainWindow + 打印工作线程 PrintWorker
HN 本地打印工具 — 支持浅色/深色双主题
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
import requests as http_requests
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from PySide6.QtCore import (
    QThread,
    Signal,
    Qt,
    QTimer,
    QObject,
    QEvent,
    QPropertyAnimation,
    QEasingCurve,
    QUrl,
)
from PySide6.QtGui import (
    QAction,
    QFont,
    QColor,
    QIcon,
    QPainter,
    QShortcut,
    QKeySequence,
    QDesktopServices,
)
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QGroupBox,
    QLabel,
    QLineEdit,
    QComboBox,
    QDoubleSpinBox,
    QSpinBox,
    QSizePolicy,
    QPushButton,
    QProgressBar,
    QTextEdit,
    QDialog,
    QFileDialog,
    QMessageBox,
    QMenuBar,
    QMenu,
    QAbstractItemView,
    QScrollArea,
    QAbstractScrollArea,
    QStatusBar,
    QStyleFactory,
    QStyle,
    QStyleOptionButton,
    QCheckBox,
)

from printer_config import PrinterConfig, PrintJob, TabSettings, calc_cost, _count_pages_in_range, DEFAULT_OWNER_NAME, PLACEHOLDER_OWNER_NAMES
from converter import get_converter, UniversalConverter
from pdf_printer import print_pdf, list_system_printers, get_pdf_info, get_docx_orientation, get_image_info, estimate_print_sides
from theme_manager import ThemeManager, MODE_SYSTEM, MODE_LIGHT, MODE_DARK, MODE_LABELS
from cloud_client import CloudClient, CloudTask, pdf_cache_key
from stats_server import StatsServer, load_local_data_file, clear_local_data_files
from offline_sync import OfflineSync

logger = logging.getLogger(__name__)


# ============================================================
# 辅助工具
# ============================================================

def _disable_combo_wheel(combo: QComboBox) -> None:
    """禁止 QComboBox 响应鼠标滚轮事件，改为转发给父 QScrollArea 实现滚动。"""
    class _WheelBlocker(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.Wheel:
                # 查找最近的 QScrollArea 并转发滚轮事件
                w = obj.parent()
                while w is not None:
                    if isinstance(w, QScrollArea):
                        QApplication.sendEvent(w.viewport(), event)
                        return True
                    w = w.parent()
                return True  # 找不到 ScrollArea 则吞掉
            return super().eventFilter(obj, event)

    combo.installEventFilter(_WheelBlocker(combo))


class _OwnerComboRefreshFilter(QObject):
    """归属下拉弹出时触发一次回调（用于从云端同步收支清算成员名单，保证选项一致）。
    监听弹出视图 viewport 的 Show 事件 —— 无论点击下拉箭头还是编辑框都会先 Show。"""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Show and self._callback:
            try:
                self._callback()
            except Exception:
                pass
        return super().eventFilter(obj, event)


def _soft_wrap_text(text: str) -> str:
    """在长 token（路径/文件名）中插入零宽空格(U+200B)，让 QLabel wordWrap 可换行。
    路径与云端临时文件名常为无空格长串，wordWrap 默认断不开会撑宽整个布局。"""
    if not text:
        return text
    for sep in ("\\", "/", "_", "-"):
        text = text.replace(sep, sep + "​")
    return text


class ThemedCheckBox(QCheckBox):
    """主题适配复选框：方框由 QSS 绘制，勾选态的对勾由 paintEvent 直接用字符绘制。

    免图片资源：勾选后叠加绘制 "✓"（U+2713）字符，颜色随主题（深色主题深色勾 / 浅色主题白色勾），
    字体渲染与界面文字一致，任意 DPI 都清晰。
    """

    def __init__(self, text: str = "", parent: QWidget | None = None, theme_manager: ThemeManager | None = None):
        super().__init__(text, parent)
        self._theme_manager = theme_manager

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.isChecked():
            return
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        rect = self.style().subElementRect(QStyle.SE_CheckBoxIndicator, opt, self)
        if rect.isEmpty():
            return
        dark = self._theme_manager is not None and self._theme_manager.effective_theme == MODE_DARK
        if not self.isEnabled():
            color = QColor("#6c7086") if dark else QColor("#9ca0b0")
        else:
            color = QColor("#1e1e2e") if dark else QColor("#ffffff")
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            f = QFont(self.font())
            f.setPixelSize(max(8, int(rect.height() * 0.72)))
            f.setBold(True)
            painter.setFont(f)
            painter.setPen(color)
            painter.drawText(rect, Qt.AlignCenter, "✓")
        finally:
            painter.end()


def _format_engine_label(ext: str) -> str:
    """返回某文件格式实际使用的 PDF 转换引擎名（参数面板灰色显示用）。"""
    if ext in (".doc", ".docx"):
        return "Word"
    if ext == ".md":
        return "markdown 库"
    if ext in (".html", ".htm"):
        return "wkhtmltopdf"
    if ext in (".txt", ".csv"):
        return "reportlab"
    if ext in (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tiff", ".tif"):
        return "图片渲染"
    if ext in (".xls", ".xlsx"):
        return "LibreOffice"
    if ext == ".pdf":
        return "直接打印"
    return "—"


def _truncate_filename(filename: str, max_width: int = 52) -> str:
    """截断文件名到指定显示宽度（中文=1.5，ASCII=1）。

    格式: base.ext，超出部分用...替代。
    例: "PixPin_2026-06-30_20-24-28.jpg" → "PixPin_2026-06-30....jpg"
    """
    def _display_width(s: str) -> float:
        w = 0.0
        for ch in s:
            if ord(ch) > 0x2000:
                w += 1.5
            else:
                w += 1.0
        return w

    base, ext = os.path.splitext(filename)
    suffix = ext  # 含点，如 ".pdf"
    suffix_w = _display_width(suffix)

    full_w = _display_width(base) + suffix_w
    if full_w <= max_width:
        return f"{base}{suffix}"

    # 需要截断：预留 "..." (3) 和 suffix 的空间
    available = max_width - 3 - suffix_w
    if available <= 0:
        return f"...{suffix}"

    # 逐字符截断 base
    truncated = ""
    w = 0.0
    for ch in base:
        cw = 1.5 if ord(ch) > 0x2000 else 1.0
        if w + cw > available:
            break
        truncated += ch
        w += cw

    return f"{truncated}...{suffix}"


def _get_persistent_client_id() -> str:
    """生成持久化客户端 ID：优先读工具目录下 .client_id 文件，不存在则生成并写入。

    同机多实例共用 hostname 会导致云端 client_id 冲突（任务可能被错误派发/回滚），
    因此追加一个随机后缀并持久化，重启后保持不变。
    """
    import socket as _socket
    import uuid
    hostname = _socket.gethostname()
    id_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".client_id")
    suffix = ""
    try:
        if os.path.isfile(id_file):
            with open(id_file, "r", encoding="utf-8") as f:
                suffix = f.read().strip()
        if not suffix:
            suffix = uuid.uuid4().hex[:10]
            with open(id_file, "w", encoding="utf-8") as f:
                f.write(suffix)
    except Exception:
        suffix = ""
    if suffix:
        return f"{hostname}-{suffix}"
    return hostname


def _enable_smooth_scroll(view: QAbstractScrollArea) -> None:
    """为可滚动区域启用平滑滚动：拦截滚轮事件并用动画过渡。"""
    class _SmoothFilter(QObject):
        def __init__(self, area):
            super().__init__(area)
            self._area = area
            self._anim: QPropertyAnimation | None = None

        def eventFilter(self, obj, event):
            if event.type() == QEvent.Wheel:
                vbar = self._area.verticalScrollBar()
                delta = event.angleDelta().y()
                if delta == 0:
                    return False

                # 每次滚轮滚动 3 倍单步步长
                step = vbar.singleStep() or 15
                current = vbar.value()
                target = current - (delta // 120) * step * 3
                target = max(vbar.minimum(), min(vbar.maximum(), target))

                # 停止上次动画
                if self._anim and self._anim.state() == QPropertyAnimation.Running:
                    self._anim.stop()

                self._anim = QPropertyAnimation(vbar, b"value", self)
                self._anim.setDuration(180)
                self._anim.setStartValue(current)
                self._anim.setEndValue(target)
                self._anim.setEasingCurve(QEasingCurve.OutCubic)
                self._anim.start()
                return True  # 已处理，阻止默认滚动
            return super().eventFilter(obj, event)

    view.viewport().installEventFilter(_SmoothFilter(view))


# ============================================================
# 自定义计数器控件
# ============================================================

class CounterWidget(QWidget):
    """自定义计数器：[−] 按钮 + 数字标签 + [+] 按钮，替代默认 QSpinBox。"""

    valueChanged = Signal(int)

    def __init__(self, min_val: int = 1, max_val: int = 99, parent: QWidget | None = None):
        super().__init__(parent)
        self._min = min_val
        self._max = max_val
        self._value = min_val

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._btn_minus = QPushButton("−")  # minus sign (−)
        self._btn_minus.setObjectName("counterMinus")
        self._btn_minus.setFixedSize(32, 32)
        self._btn_minus.clicked.connect(self._decrease)

        self._label = QLabel(str(self._value))
        self._label.setObjectName("counterLabel")
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setFixedHeight(32)

        self._btn_plus = QPushButton("+")
        self._btn_plus.setObjectName("counterPlus")
        self._btn_plus.setFixedSize(32, 32)
        self._btn_plus.clicked.connect(self._increase)

        layout.addWidget(self._btn_minus)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._btn_plus)

        self._update_state()

    # -- 公开 API（与 QSpinBox 兼容）--

    def value(self) -> int:
        return self._value

    def setValue(self, v: int) -> None:
        if self._min <= v <= self._max:
            self._value = v
            self._label.setText(str(v))
            self._update_state()

    def setRange(self, min_val: int, max_val: int) -> None:
        self._min = min_val
        self._max = max_val
        clamped = max(min_val, min(max_val, self._value))
        if clamped != self._value:
            self._value = clamped
            self._label.setText(str(self._value))
            self.valueChanged.emit(self._value)
        self._update_state()

    # -- 内部逻辑 --

    def _increase(self) -> None:
        if self._value < self._max:
            self._value += 1
            self._label.setText(str(self._value))
            self._update_state()
            self.valueChanged.emit(self._value)

    def _decrease(self) -> None:
        if self._value > self._min:
            self._value -= 1
            self._label.setText(str(self._value))
            self._update_state()
            self.valueChanged.emit(self._value)

    def _update_state(self) -> None:
        self._btn_minus.setEnabled(self._value > self._min)
        self._btn_plus.setEnabled(self._value < self._max)


# ============================================================
# 动态页码范围输入组件
# ============================================================

class RangeListWidget(QWidget):
    """
    多行页码范围输入：每行一个范围，自动增减行，检测重叠和超限。
    始终保留一个空行供用户输入。
    """

    rangesChanged = Signal()  # 有效范围变更时发出

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        self._inputs: list[QLineEdit] = []
        self._total_pages: int = 0
        self._rebuilding = False
        self._valid = True  # 空输入视为有效

        self._add_row()

    # -- 公开 API --

    def set_total_pages(self, n: int) -> None:
        self._total_pages = n
        self._check_all()

    def set_ranges(self, text: str) -> None:
        """从逗号分隔的字符串恢复多行。"""
        self._rebuilding = True
        self._remove_all()
        parts = []
        if text and text.strip():
            parts = [p.strip() for p in
                     text.replace("、", ",").replace("，", ",").replace("；", ",").replace(" ", "").split(",")
                     if p.strip()]
        for p in parts:
            inp = self._add_row()
            inp.setText(p)
        # 确保末尾有空行
        if not self._inputs or self._inputs[-1].text().strip():
            self._add_row()
        self._rebuilding = False
        self._check_all()

    def get_ranges(self) -> str:
        """获取合并后的范围字符串。"""
        parts = []
        for inp in self._inputs:
            t = inp.text().strip()
            if t:
                parts.append(t)
        return ",".join(parts)

    def is_valid(self) -> bool:
        """当前输入是否全部有效（无格式错误、无重叠、无超限）。"""
        return self._valid

    def clear(self) -> None:
        self.set_ranges("")

    # -- 内部逻辑 --

    def _add_row(self) -> QLineEdit:
        inp = QLineEdit()
        inp.setPlaceholderText("如: 1-5 或 7")
        inp.textChanged.connect(lambda t, i=inp: self._on_text(i, t))
        inp.editingFinished.connect(lambda i=inp: self._on_focus_lost(i))
        self._inputs.append(inp)
        self._layout.addWidget(inp)
        return inp

    def _remove_all(self) -> None:
        for inp in self._inputs:
            inp.blockSignals(True)
            self._layout.removeWidget(inp)
            inp.deleteLater()
        self._inputs.clear()

    def _sort_inputs(self) -> None:
        """按页码范围起始值升序排列非空输入行，保持末尾空行。"""
        if self._rebuilding:
            return
        # 收集非空行，附带排序键
        entries: list[tuple[int, str]] = []
        for inp in self._inputs:
            t = inp.text().strip()
            if not t:
                continue
            pages = self._parse_range(t)
            key = min(pages) if pages else 10 ** 9  # 解析失败排末尾
            entries.append((key, t))
        if len(entries) <= 1:
            return
        # 判断是否需要排序
        sorted_entries = sorted(entries, key=lambda x: x[0])
        if entries == sorted_entries:
            return
        # 重建：排序后内容 + 一个空行
        self._rebuilding = True
        for inp in self._inputs:
            inp.blockSignals(True)
            self._layout.removeWidget(inp)
            inp.deleteLater()
        self._inputs.clear()
        for _, text in sorted_entries:
            new_inp = self._add_row()
            new_inp.setText(text)
        self._add_row()  # 底部空行
        self._rebuilding = False

    def _rebuild_lines(self) -> None:
        """重建行列表：移除多余空行，排序后保证末尾一个空行。"""
        self._rebuilding = True
        # 收集非空行，附带排序键
        entries: list[tuple[int, str]] = []
        for inp in self._inputs:
            t = inp.text().strip()
            if not t:
                continue
            pages = self._parse_range(t)
            key = min(pages) if pages else 10 ** 9
            entries.append((key, t))
        # 排序
        entries.sort(key=lambda x: x[0])
        # 移除所有旧行
        for inp in self._inputs:
            inp.blockSignals(True)
            self._layout.removeWidget(inp)
            inp.deleteLater()
        self._inputs.clear()
        # 添加排序后的非空行 + 一个空行
        for _, text in entries:
            new_inp = self._add_row()
            new_inp.setText(text)
        self._add_row()  # 底部空行
        self._rebuilding = False

    def _on_text(self, sender: QLineEdit, text: str) -> None:
        if self._rebuilding:
            return
        # 最后一行有内容 → 追加新空行
        if text.strip() and sender is self._inputs[-1]:
            self._rebuilding = True
            self._add_row()
            self._rebuilding = False
        self._check_all()
        if self._valid:
            self.rangesChanged.emit()

    def _on_focus_lost(self, sender: QLineEdit) -> None:
        if self._rebuilding:
            return
        # 统计非空行数
        filled = sum(1 for inp in self._inputs if inp.text().strip())
        empty_count = len(self._inputs) - filled
        # 多于 1 个空行时重建
        if empty_count > 1:
            self._rebuild_lines()
        # 没有空行时追加
        elif empty_count == 0:
            self._rebuilding = True
            self._add_row()
            self._rebuilding = False
        self._check_all()
        # 验证通过后自动排序，然后通知表格更新
        if self._valid:
            self._sort_inputs()
            self.rangesChanged.emit()

    def _check_all(self) -> None:
        """检测格式、重叠、超限。"""
        # 清除所有错误样式
        for inp in self._inputs:
            inp.setProperty("invalid", False)
            inp.style().unpolish(inp)
            inp.style().polish(inp)

        parsed: list[tuple[QLineEdit, set[int]]] = []
        has_error = False

        for inp in self._inputs:
            t = inp.text().strip()
            if not t:
                continue
            pages = self._parse_range(t)
            if pages is None:
                self._mark_invalid(inp)
                has_error = True
            elif self._total_pages > 0 and max(pages) > self._total_pages:
                self._mark_invalid(inp)
                has_error = True
            else:
                parsed.append((inp, pages))

        # 检查重叠
        for i in range(len(parsed)):
            for j in range(i + 1, len(parsed)):
                if parsed[i][1] & parsed[j][1]:
                    self._mark_invalid(parsed[i][0])
                    self._mark_invalid(parsed[j][0])
                    has_error = True

        self._valid = not has_error

    @staticmethod
    def _mark_invalid(inp: QLineEdit) -> None:
        """标记输入框为错误状态（通过 QSS 动态属性驱动样式）。"""
        inp.setProperty("invalid", True)
        inp.style().unpolish(inp)
        inp.style().polish(inp)

    @staticmethod
    def _parse_range(text: str) -> set[int] | None:
        """解析单个范围字符串（如 "1-5"、"7"），start > end 视为格式错误。"""
        text = text.strip()
        if not text:
            return None
        try:
            if "-" in text:
                a, b = text.split("-", 1)
                start, end = int(a), int(b)
                if 1 <= start < end:
                    return set(range(start, end + 1))
                return None
            else:
                v = int(text)
                return {v} if v >= 1 else None
        except ValueError:
            return None


# ============================================================
# 打印工作线程
# ============================================================

class PrintWorker(QThread):
    """
    后台打印工作线程。
    顺序处理任务列表中的每个文件：转换 → 打印 → 清理。
    通过信号与主界面通信。
    """

    # 信号定义
    progress = Signal(int, int, str)          # (current, total, status_text)
    log_message = Signal(str)                  # 日志消息
    job_finished = Signal(int, bool, str)      # (job_index, success, message)
    all_finished = Signal(int, int)            # (success_count, fail_count)
    error_occurred = Signal(str)               # 全局错误
    # 图片转换后缓存信号：worker 只写 PDF 文件，主线程补索引
    # (source_md5, cached_path, name, ext, page_count, image_orientation)
    pdf_cached = Signal(str, str, str, str, int, str)

    def __init__(
        self,
        jobs: list[PrintJob],
        printer_name: str,
        duplex_mode: str,
        keep_temp_pdf: bool,
        render_dpi: int = 400,
        cover_page: bool = False,
        cover_page_config: dict | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._jobs = jobs
        self._printer_name = printer_name
        self._duplex_mode = duplex_mode
        self._keep_temp_pdf = keep_temp_pdf
        self._render_dpi = render_dpi
        self._cover_page = cover_page
        self._cover_page_config = cover_page_config or {}
        self._converter: Optional[UniversalConverter] = None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        self.log_message.emit("[取消] 用户取消了打印任务")

    def run(self):
        """线程主函数：统一 PDF 打印流程（GDI 优先），精确到每面的进度条。"""
        try:
            self._converter = get_converter()
            task_count = len(self._jobs)
            success_count = 0
            fail_count = 0

            # 预计算总面数（用于精确进度条）
            job_sides: list[int] = []
            for job in self._jobs:
                sides = estimate_print_sides(
                    max(1, job.page_count), max(1, job.copies), job.duplex, job.page_range,
                )
                job_sides.append(sides)
            total_sides = sum(job_sides)
            if total_sides <= 0:
                total_sides = 1
        except Exception as e:
            # 初始化/预估失败 → 发射失败信号，保证 all_finished 最终发出、UI 不卡死
            self.log_message.emit(f"✗ 打印初始化失败: {e}")
            self.error_occurred.emit(f"打印初始化失败: {e}")
            self.all_finished.emit(0, len(self._jobs))
            return

        self.progress.emit(0, total_sides, f"共 {task_count} 个任务, 预估 {total_sides} 面")
        self.log_message.emit(f"共 {task_count} 个任务待处理")

        # ── 打印首页 (Cover Page) ──
        cover_pdf_path: Optional[str] = None
        if self._cover_page:
            self.log_message.emit("📋 正在生成打印首页...")
            try:
                from printer_config import generate_cover_page_pdf

                cover_pdf_path = os.path.join(
                    tempfile.gettempdir(),
                    f"hn_cover_{os.getpid()}_{int(time.time())}.pdf"
                )
                cfg_dict = self._cover_page_config
                order_number = cfg_dict.get("order_number", "")
                created_at = cfg_dict.get("created_at", "")

                # 构建临时的 PrinterConfig 用于生成首页
                config = PrinterConfig()
                config.simplex_price = cfg_dict.get("simplex_price", 0.2)
                config.duplex_price = cfg_dict.get("duplex_price", 0.3)
                config.delivery_enabled = cfg_dict.get("delivery_enabled", False)
                config.delivery_location = cfg_dict.get("delivery_location", "")
                config.delivery_percentages = cfg_dict.get("delivery_percentages", {})
                config.urgency = cfg_dict.get("urgency", "低")
                config.urgency_prices = cfg_dict.get("urgency_prices", {})
                config.cover_page = True
                config.cover_page_price = cfg_dict.get("cover_page_price", 0.15)
                config.pickup_address = cfg_dict.get("pickup_address", "")

                ok = generate_cover_page_pdf(
                    cover_pdf_path, config, self._jobs,
                    order_number=order_number,
                    created_at=created_at,
                )
                if ok and os.path.isfile(cover_pdf_path):
                    # 打印首页（单面、1份、无页码范围）
                    self.log_message.emit("📋 正在打印首页...")
                    cover_ok, cover_msg = print_pdf(
                        pdf_path=cover_pdf_path,
                        printer_name=self._printer_name,
                        copies=1,
                        duplex="off",
                        duplex_mode="long-edge",
                        page_range="",
                        orientation="portrait",
                        progress_callback=None,
                        dpi=self._render_dpi,
                    )
                    if cover_ok:
                        self.log_message.emit(f"  ✓ 首页: {cover_msg}")
                    else:
                        self.log_message.emit(f"  ✗ 首页打印失败: {cover_msg}")
                else:
                    self.log_message.emit("  ✗ 首页生成失败")
            except Exception as e:
                self.log_message.emit(f"  ✗ 首页生成异常: {e}")
            finally:
                # 清理临时首页 PDF
                if cover_pdf_path and os.path.isfile(cover_pdf_path):
                    try:
                        os.remove(cover_pdf_path)
                    except OSError:
                        pass

        offset_sides = 0
        for idx, job in enumerate(self._jobs):
            if self._cancelled:
                # 取消：剩余任务按失败处理，保证 job_finished/all_finished 完整发射、UI 不卡死
                for rest_idx in range(idx, len(self._jobs)):
                    self.job_finished.emit(rest_idx, False, "已取消")
                    fail_count += 1
                self.log_message.emit(f"[取消] 已中止剩余 {len(self._jobs) - idx} 个任务")
                break

            file_name = os.path.basename(job.file_path)
            self.progress.emit(offset_sides, total_sides, f"正在处理: {file_name}")
            self.log_message.emit(f"[{idx + 1}/{task_count}] {file_name}")

            ext = os.path.splitext(job.file_path)[1].lower()
            copies = max(1, job.copies)
            orient_info = f", 方向:{job.orientation}" if job.orientation else ""
            temp_pdf: Optional[str] = None
            ok = False  # 本任务是否打印成功（except 路径保持 False）

            # 此任务的预估面数
            this_job_sides = job_sides[idx] if idx < len(job_sides) else 1
            actual_sides = 0  # 本任务实际打印面数（失败/取消时按实际回退进度，避免进度条跳变）

            def _make_progress_callback(base_offset: int, total_all: int):
                def _on_side(page_seq: int, _task_total: int):
                    nonlocal actual_sides
                    if self._cancelled:
                        return  # 已取消：不再推进进度
                    actual_sides = max(actual_sides, page_seq)
                    # page_seq 是此任务内部的当前面号（从1开始）
                    self.progress.emit(base_offset + page_seq, total_all,
                                       f"正在打印: {file_name} ({page_seq}/{this_job_sides}面)")
                return _on_side

            try:
                if not os.path.isfile(job.file_path):
                    raise FileNotFoundError(f"文件不存在: {job.file_path}")

                # 1. 确定打印用的 PDF 路径
                if ext == ".pdf":
                    print_path = job.file_path
                    self.log_message.emit(f"  → 已是 PDF，跳过转换")
                elif job.cached_pdf and os.path.isfile(job.cached_pdf):
                    print_path = job.cached_pdf
                    self.log_message.emit(f"  → 使用缓存的 PDF")
                else:
                    self.log_message.emit(f"  → 正在转换为 PDF...")
                    temp_pdf = self._converter.convert(
                        job.file_path, image_orientation=getattr(job, 'image_orientation', 'auto'))
                    print_path = temp_pdf
                    self.log_message.emit(f"  → 转换完成: {os.path.basename(temp_pdf)}")

                # 1.5 图片转换后写入方向缓存（方案A：同图同方向免重复下载/转换）。
                # worker 只写 PDF 文件，索引由主线程补（meta 为空时 GUI 缓存命中不采纳）。
                if temp_pdf and os.path.isfile(temp_pdf) and job.source_md5:
                    _ext_i = os.path.splitext(job.file_path)[1].lower()
                    if _ext_i in (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tiff", ".tif"):
                        try:
                            _key = pdf_cache_key(job.source_md5, getattr(job, 'image_orientation', 'auto'))
                            _cdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdf_cache")
                            os.makedirs(_cdir, exist_ok=True)
                            _dest = os.path.join(_cdir, _key + ".pdf")
                            if not os.path.exists(_dest):
                                shutil.copy2(temp_pdf, _dest)
                            _pg = get_pdf_info(temp_pdf).get("page_count", 0)
                            self.pdf_cached.emit(
                                job.source_md5, _dest,
                                job.display_name or os.path.basename(job.file_path),
                                _ext_i, _pg, getattr(job, 'image_orientation', 'auto'))
                        except Exception:
                            pass

                # 2. 打印 PDF（传入进度回调）
                self.log_message.emit(
                    f"  → 正在打印 (份数:{copies}, 双面:{job.duplex}{orient_info})..."
                )
                dm = job.duplex_mode or self._duplex_mode
                effective_dpi = job.dpi if job.dpi > 0 else self._render_dpi
                # 打印方向以实际 PDF 页面为准：图片转换/方向设置后 job.orientation 可能过时
                # （EXIF 竖拍照片原始像素是横的；image_orientation 显式设置后 job.orientation 仍是旧值）
                _pdf_ori = ""
                try:
                    if print_path and os.path.isfile(print_path):
                        _pdf_ori = get_pdf_info(print_path).get("orientation", "")
                except Exception:
                    _pdf_ori = ""
                eff_ori = _pdf_ori if _pdf_ori in ("portrait", "landscape") else (job.orientation or "")
                ok, msg = print_pdf(
                    pdf_path=print_path,
                    printer_name=self._printer_name,
                    copies=copies,
                    duplex=job.duplex,
                    duplex_mode=dm,
                    page_range=job.page_range,
                    orientation=eff_ori,
                    progress_callback=_make_progress_callback(offset_sides, total_sides),
                    dpi=effective_dpi,
                )
                if ok:
                    self.log_message.emit(f"  ✓ {msg}")
                    success_count += 1
                else:
                    self.log_message.emit(f"  ✗ 打印失败: {msg}")
                    fail_count += 1

                self.job_finished.emit(idx, ok, msg)

            except Exception as e:
                self.log_message.emit(f"  ✗ 错误: {e}")
                fail_count += 1
                self.job_finished.emit(idx, False, str(e))

            finally:
                # 3. 清理临时 PDF（缓存的 PDF 不删，下次打印复用）
                if temp_pdf and os.path.isfile(temp_pdf):
                    if self._keep_temp_pdf:
                        original_base = os.path.splitext(os.path.basename(job.file_path))[0]
                        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
                        dest_name = f"[转换]{original_base}.pdf"
                        dest_path = os.path.join(desktop, dest_name)
                        try:
                            shutil.copy2(temp_pdf, dest_path)
                            self.log_message.emit(f"  → 转换副本已保存到桌面: {dest_name}")
                        except OSError as e:
                            self.log_message.emit(f"  → 保存转换副本到桌面失败: {e}")
                    try:
                        os.remove(temp_pdf)
                    except OSError as e:
                        self.log_message.emit(f"  → 清理临时 PDF 失败: {e}")

            if ok:
                offset_sides += this_job_sides
            else:
                # 失败/取消：按实际打印面数回退，避免进度条凭空跳变
                offset_sides += actual_sides

        self.progress.emit(total_sides, total_sides, "全部完成")
        self.all_finished.emit(success_count, fail_count)
        self.log_message.emit(
            f"========== 打印完毕：成功 {success_count}，失败 {fail_count} =========="
        )


class ConvertWorker(QThread):
    """
    后台线程：将 Word 文件转为 PDF。
    不阻塞 UI，转换完成后通过信号返回结果。
    """
    # (row, file_path, cached_pdf, page_count, orientation)
    # file_path 用于回调按文件匹配行，避免多文件订单行号错位
    finished = Signal(int, str, str, int, str)

    def __init__(self, row: int, file_path: str, engine: str, source_md5: str = ""):
        super().__init__()
        self._row = row
        self._file_path = file_path
        self._engine = engine
        self._source_md5 = source_md5
        self._cancelled = False

    def cancel(self):
        """协作式取消：置标志，run() 各阶段检查后优雅退出（替代 terminate 强杀）。"""
        self._cancelled = True

    def run(self):
        from converter import _convert_via_word_com, _convert_via_wps_com, get_converter
        from pdf_printer import get_pdf_info

        # PDF 缓存目录（与 cloud_client.py 共用）
        cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdf_cache")
        os.makedirs(cache_dir, exist_ok=True)
        temp_pdf: str | None = None
        try:
            ext = os.path.splitext(self._file_path)[1].lower()
            if self._cancelled:
                self.finished.emit(self._row, self._file_path, "", 0, "")
                return
            if ext in (".doc", ".docx") and self._engine != "libreoffice":
                # 直接以 MD5 命名存入 pdf_cache（若无 MD5 则用临时文件）
                if self._source_md5:
                    temp_pdf = os.path.join(cache_dir, f"{self._source_md5}.pdf")
                else:
                    import tempfile as _tf
                    fd, temp_pdf = _tf.mkstemp(suffix=".pdf", prefix="_conv_")
                    os.close(fd)
                if self._cancelled:
                    self.finished.emit(self._row, self._file_path, "", 0, "")
                    return
                if self._engine == "wps":
                    _convert_via_wps_com(self._file_path, temp_pdf)
                else:
                    _convert_via_word_com(self._file_path, temp_pdf)
            else:
                converter = get_converter()
                temp_pdf = converter.convert(self._file_path)
        except Exception:
            # Word/WPS 引擎失败，降级到 LibreOffice
            if ext in (".doc", ".docx") and self._engine != "libreoffice":
                try:
                    if temp_pdf and os.path.isfile(temp_pdf):
                        os.remove(temp_pdf)
                except OSError:
                    pass
                try:
                    converter = get_converter()
                    temp_pdf = converter.convert(self._file_path)
                except Exception:
                    self.finished.emit(self._row, self._file_path, "", 0, "")
                    return
            else:
                self.finished.emit(self._row, self._file_path, "", 0, "")
                return
        if self._cancelled:
            self.finished.emit(self._row, self._file_path, "", 0, "")
            return

        info = get_pdf_info(temp_pdf)
        self.finished.emit(self._row, self._file_path, temp_pdf, info["page_count"], info["orientation"])


def _cleanup_temp(path: str | None) -> None:
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


# ============================================================
# 地点管理对话框
# ============================================================

class LocationManagerDialog(QDialog):
    """管理派送地点及百分比。"""

    def __init__(self, locations: dict[str, float], parent=None):
        super().__init__(parent)
        self.setWindowTitle("管理派送地点")
        self.setMinimumSize(400, 300)
        self.resize(450, 350)
        self._locations = dict(locations)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        # 表格：地点名称 | 百分比
        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(["地点名称", "百分比"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self._table.setColumnWidth(1, 80)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self._table)

        # 添加按钮 — 置于表格下方（参考文件列表样式）
        btn_add = QPushButton("📂 添加地点")
        btn_add.clicked.connect(self._on_add)
        layout.addWidget(btn_add)

        # 确定 / 取消
        bottom = QHBoxLayout()
        bottom.addStretch()
        btn_ok = QPushButton("确定")
        btn_ok.clicked.connect(self._on_ok)
        bottom.addWidget(btn_ok)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        bottom.addWidget(btn_cancel)
        layout.addLayout(bottom)

        self._populate_table()

    def _populate_table(self):
        self._table.setRowCount(0)
        for name, pct in self._locations.items():
            self._add_row(name, pct)

    def _add_row(self, name: str = "", pct: float = 0.0):
        row = self._table.rowCount()
        self._table.insertRow(row)

        name_item = QTableWidgetItem(name)
        self._table.setItem(row, 0, name_item)

        pct_spin = QDoubleSpinBox()
        pct_spin.setButtonSymbols(QDoubleSpinBox.NoButtons)
        pct_spin.setRange(0.0, 100.0)
        pct_spin.setDecimals(0)
        pct_spin.setSingleStep(1)
        pct_spin.setSuffix("%")
        pct_spin.setValue(pct)
        pct_spin.setMaximumWidth(70)
        self._table.setCellWidget(row, 1, pct_spin)

    def _on_add(self):
        self._add_row("新地点", 0.0)
        row = self._table.rowCount() - 1
        self._table.selectRow(row)
        self._table.editItem(self._table.item(row, 0))
        self._table.scrollToBottom()

    def _on_context_menu(self, pos):
        """右键菜单：删除选中地点。"""
        item = self._table.itemAt(pos)
        if item is None:
            return
        row = item.row()
        self._table.selectRow(row)
        name = self._table.item(row, 0).text()

        menu = QMenu(self)
        del_action = menu.addAction(f"删除「{name}」")
        action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if action == del_action:
            reply = QMessageBox.question(self, "确认删除", f"确定要删除地点「{name}」吗？")
            if reply == QMessageBox.Yes:
                self._table.removeRow(row)

    def _on_ok(self):
        new_locations: dict[str, float] = {}
        for row in range(self._table.rowCount()):
            name = self._table.item(row, 0).text().strip()
            if not name:
                continue
            pct_widget = self._table.cellWidget(row, 1)
            pct = pct_widget.value() if pct_widget else 0.0
            new_locations[name] = pct
        if not new_locations:
            QMessageBox.warning(self, "提示", "至少需要保留一个地点。")
            return
        self._locations = new_locations
        self.accept()

    def get_locations(self) -> dict[str, float]:
        return self._locations


# ============================================================
# DropTableWidget — 支持拖放文件
# ============================================================

class DropTableWidget(QTableWidget):
    """支持从资源管理器拖放文件到表格中。"""

    filesDropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            files = []
            for url in urls:
                path = url.toLocalFile()
                if path and os.path.isfile(path):
                    files.append(path)
            if files:
                self.filesDropped.emit(files)
            event.acceptProposedAction()
        else:
            event.ignore()


# ============================================================
# CloudTaskListWindow — 云端任务列表窗口（v7：替代单任务弹窗）
# ============================================================

class CloudTaskListWindow(QDialog):
    """统一的云端任务列表窗口，按订单分组管理待确认的云端打印任务。
    非模态，支持批量操作、取消响应和自动关闭。"""

    # 信号：emit 订单中所有任务的列表
    order_accepted = Signal(list)   # list[CloudTask] — 用户确认添加某订单
    order_rejected = Signal(list)   # list[CloudTask] — 用户打回某订单

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("☁ 云端任务列表")
        self.setMinimumWidth(600)
        self.setMinimumHeight(380)
        self.setModal(False)
        self.setAttribute(Qt.WA_DeleteOnClose, False)

        # order_id → {"order_number": str, "tasks": [CloudTask], "status": str, "canceled_at": float|None}
        self._pending_orders: dict[int, dict] = {}
        self._auto_close_seconds = 300
        self._auto_close_timer = QTimer(self)
        self._auto_close_timer.setSingleShot(True)
        self._auto_close_timer.timeout.connect(self._on_auto_close)

        self._cancel_remove_timer = QTimer(self)
        self._cancel_remove_timer.setInterval(1000)
        self._cancel_remove_timer.timeout.connect(self._check_canceled_expiry)

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 10, 12, 10)

        # ── 自动关闭设置行 ──
        auto_row = QHBoxLayout()
        auto_row.addWidget(QLabel("自动关闭：待确认任务清空后"))
        self._auto_minutes = QSpinBox()
        self._auto_minutes.setButtonSymbols(QSpinBox.NoButtons)  # 与主界面输入框一致：隐藏加减箭头
        self._auto_minutes.setRange(0, 60)
        self._auto_minutes.setValue(5)
        self._auto_minutes.setSuffix(" 分钟")
        self._auto_minutes.setFixedWidth(100)
        self._auto_minutes.valueChanged.connect(self._on_auto_close_changed)
        auto_row.addWidget(self._auto_minutes)
        self._auto_seconds = QSpinBox()
        self._auto_seconds.setButtonSymbols(QSpinBox.NoButtons)  # 与主界面输入框一致：隐藏加减箭头
        self._auto_seconds.setRange(0, 59)
        self._auto_seconds.setValue(0)
        self._auto_seconds.setSuffix(" 秒")
        self._auto_seconds.setFixedWidth(80)
        self._auto_seconds.valueChanged.connect(self._on_auto_close_changed)
        auto_row.addWidget(self._auto_seconds)
        auto_row.addWidget(QLabel("后自动关闭"))
        auto_row.addStretch()
        self._countdown_label = QLabel("")
        self._countdown_label.setObjectName("countdownLabel")
        self._countdown_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        auto_row.addWidget(self._countdown_label)
        layout.addLayout(auto_row)

        # 倒计时刷新定时器（每 1 秒更新右侧剩余时间）
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._update_countdown)

        # ── 表格：订单号 | 文件数 | 总页数 | 状态 | 操作 ──
        from PySide6.QtWidgets import QTableWidget as _QTW, QTableWidgetItem as _QTWI
        self._table = _QTW()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(["订单号", "文件数", "总页数", "状态", "操作"])
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(38)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self._table, 1)

        # ── 按钮行 ──
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._accept_all_btn = QPushButton("📥 全部添加到新标签页")
        self._accept_all_btn.clicked.connect(self._on_accept_all)
        btn_row.addWidget(self._accept_all_btn)

        self._reject_all_btn = QPushButton("↩ 全部打回")
        self._reject_all_btn.setObjectName("cloudRejectBtn")
        self._reject_all_btn.clicked.connect(self._on_reject_all)
        btn_row.addWidget(self._reject_all_btn)

        btn_row.addStretch()

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self._on_close)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)

    # ── 公共 API ──

    def add_task(self, task: CloudTask):
        """添加一个待确认的云端任务，按订单分组。"""
        oid = task.order_id or task.task_id  # 无 order_id 时按 task_id 自分组
        if oid not in self._pending_orders:
            self._pending_orders[oid] = {
                "order_number": task.order_number or f"#{oid}",
                "tasks": [],
                "status": "pending",
                "canceled_at": None,
            }
        entry = self._pending_orders[oid]
        # 去重
        if any(t.task_id == task.task_id for t in entry["tasks"]):
            return
        entry["tasks"].append(task)
        if entry["status"] != "canceled":
            entry["status"] = "pending"
        self._cancel_auto_close()
        self._rebuild_table()
        if not self.isVisible():
            self.show()
            self._cancel_remove_timer.start()

    def mark_canceled(self, order_id: int, task_ids: list[int]):
        """标记指定订单为已取消（响应 F6 取消推送）。"""
        if order_id in self._pending_orders:
            entry = self._pending_orders[order_id]
            if entry["status"] == "pending":
                entry["status"] = "canceled"
                entry["canceled_at"] = time.time()
        self._rebuild_table()
        self._check_auto_close()

    def _rebuild_table(self):
        """刷新表格：按订单分组显示。"""
        from PySide6.QtWidgets import QTableWidgetItem as _QTWI
        self._table.setRowCount(0)
        for oid, entry in self._pending_orders.items():
            tasks = entry["tasks"]
            status = entry["status"]
            file_count = len(tasks)
            total_pages = sum(getattr(t, 'page_count', 0) or 0 for t in tasks) * sum(max(1, t.copies) for t in tasks) if tasks else 0
            # 简化：总页数暂不计算（CloudTask无page_count），显示文件数即可
            total_cost = 0  # 后续可从任务详情获取

            row = self._table.rowCount()
            self._table.insertRow(row)

            # 订单号
            self._table.setItem(row, 0, _QTWI(entry["order_number"]))

            # 文件数
            self._table.setItem(row, 1, _QTWI(str(file_count)))

            # 总页数（暂无精确数据，显示 "—"）
            self._table.setItem(row, 2, _QTWI("—"))

            # 状态
            status_text = {"pending": "待确认", "canceled": "已取消", "accepted": "已添加", "rejected": "已打回"}
            status_item = _QTWI(status_text.get(status, status))
            if status == "canceled":
                status_item.setForeground(QColor("#999"))
            self._table.setItem(row, 3, status_item)

            # 操作按钮
            if status == "pending":
                btn_widget = QWidget()
                btn_layout = QHBoxLayout(btn_widget)
                btn_layout.setContentsMargins(0, 0, 0, 0)
                btn_layout.setSpacing(4)

                accept_btn = QPushButton("📥 添加")
                accept_btn.setFixedSize(70, 26)
                accept_btn.setStyleSheet("font-size:11px; padding:0;")
                accept_btn.clicked.connect(lambda checked=False, ts=tasks: self._on_accept_order(ts))
                btn_layout.addWidget(accept_btn)

                reject_btn = QPushButton("↩ 打回")
                reject_btn.setFixedSize(70, 26)
                reject_btn.setStyleSheet("font-size:11px; padding:0;")
                reject_btn.clicked.connect(lambda checked=False, ts=tasks: self._on_reject_order(ts))
                btn_layout.addWidget(reject_btn)

                self._table.setCellWidget(row, 4, btn_widget)
            elif status == "canceled":
                info_btn = QPushButton("✕ 已取消")
                info_btn.setFixedSize(80, 26)
                info_btn.setStyleSheet("font-size:11px; padding:0;")
                info_btn.setEnabled(False)
                self._table.setCellWidget(row, 4, info_btn)
            else:
                self._table.setItem(row, 4, _QTWI("—"))

        has_pending = any(e["status"] == "pending" for e in self._pending_orders.values())
        self._accept_all_btn.setEnabled(has_pending)
        self._reject_all_btn.setEnabled(has_pending)

    # ── 单订单操作 ──

    def _on_accept_order(self, tasks: list):
        """确认添加某订单的全部文件。"""
        oid = tasks[0].order_id or tasks[0].task_id
        if oid in self._pending_orders:
            self._pending_orders[oid]["status"] = "accepted"
        self.order_accepted.emit(tasks)
        self._rebuild_table()
        self._check_auto_close()

    def _on_reject_order(self, tasks: list):
        """打回某订单的全部文件。"""
        oid = tasks[0].order_id or tasks[0].task_id
        if oid in self._pending_orders:
            self._pending_orders[oid]["status"] = "rejected"
        self.order_rejected.emit(tasks)
        self._rebuild_table()
        self._check_auto_close()

    # ── 批量操作 ──

    def _on_accept_all(self):
        pending_orders = [(oid, e) for oid, e in self._pending_orders.items() if e["status"] == "pending"]
        if not pending_orders:
            return
        reply = QMessageBox.question(
            self, "全部添加", f"将为 {len(pending_orders)} 个订单各创建一个新标签页，确定吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return
        for oid, entry in pending_orders:
            entry["status"] = "accepted"
            self.order_accepted.emit(entry["tasks"])
        self._rebuild_table()
        self._check_auto_close()

    def _on_reject_all(self):
        pending_orders = [(oid, e) for oid, e in self._pending_orders.items() if e["status"] == "pending"]
        if not pending_orders:
            return
        reply = QMessageBox.question(
            self, "全部打回", f"将打回 {len(pending_orders)} 个待确认订单，确定吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return
        for oid, entry in pending_orders:
            entry["status"] = "rejected"
            self.order_rejected.emit(entry["tasks"])
        self._rebuild_table()
        self._check_auto_close()

    # ── 自动关闭 ──

    def _on_auto_close_changed(self):
        self._auto_close_seconds = self._auto_minutes.value() * 60 + self._auto_seconds.value()

    def _update_countdown(self):
        """每 1 秒刷新右侧倒计时文本（读取自动关闭定时器剩余时间）。"""
        remaining_ms = self._auto_close_timer.remainingTime()
        if remaining_ms < 0:
            self._countdown_timer.stop()
            self._countdown_label.setText("")
            return
        total = (remaining_ms + 999) // 1000  # 向上取整，避免闪现 00:00
        if total <= 0:
            self._countdown_timer.stop()
            self._countdown_label.setText("")
            return
        mm, ss = divmod(total, 60)
        self._countdown_label.setText(f"剩余 {mm:02d}:{ss:02d}")

    def _start_auto_close(self):
        """启动自动关闭倒计时（含右侧剩余时间显示）。"""
        self._auto_close_timer.start(self._auto_close_seconds * 1000)
        self._countdown_timer.start()
        self._update_countdown()

    def _check_auto_close(self):
        has_pending = any(e["status"] == "pending" for e in self._pending_orders.values())
        all_done = not has_pending and len(self._pending_orders) > 0
        if all_done:
            if not self._auto_close_timer.isActive():
                self._start_auto_close()
        else:
            self._cancel_auto_close()

    def _cancel_auto_close(self):
        # 只要还有待确认订单就无条件停掉倒计时并清空文字，
        # 否则手动关闭窗口后再来新订单时旧倒计时会残留。
        self._auto_close_timer.stop()
        self._countdown_timer.stop()
        self._countdown_label.setText("")
        self._pending_orders = {
            oid: e for oid, e in self._pending_orders.items()
            if e["status"] == "pending"
        }
        if not self._pending_orders:
            self._start_auto_close()

    def _on_auto_close(self):
        self._countdown_timer.stop()
        self._countdown_label.setText("")
        if not any(e["status"] == "pending" for e in self._pending_orders.values()):
            self._cancel_remove_timer.stop()
            self.hide()

    def _check_canceled_expiry(self):
        now = time.time()
        changed = False
        for oid, entry in list(self._pending_orders.items()):
            if entry["status"] == "canceled" and entry["canceled_at"]:
                if now - entry["canceled_at"] > 5:
                    del self._pending_orders[oid]
                    changed = True
        if changed:
            self._rebuild_table()
            self._check_auto_close()

    # ── 关闭 ──

    def _on_close(self):
        pending_orders = [(oid, e) for oid, e in self._pending_orders.items() if e["status"] == "pending"]
        if pending_orders:
            reply = QMessageBox.question(
                self, "关闭窗口",
                f"还有 {len(pending_orders)} 个未确认的订单，关闭将全部打回。\n确定关闭吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            for oid, entry in pending_orders:
                entry["status"] = "rejected"
                self.order_rejected.emit(entry["tasks"])
        self._cancel_remove_timer.stop()
        self._auto_close_timer.stop()
        self._countdown_timer.stop()
        self._countdown_label.setText("")
        self.hide()

    def closeEvent(self, event):
        self._on_close()
        event.ignore()


# ============================================================
# 主窗口
# ============================================================

class MainWindow(QMainWindow):
    """HN 本地打印工具 — 主窗口"""

    # 表格列索引常量
    COL_FILE, COL_COPIES, COL_DUPLEX, COL_RANGE, COL_PAGES, COL_ORIENT, COL_ENGINE, COL_COST = range(8)

    # 图片扩展名（含 tiff/tif：小程序允许上传，本地工具需同样识别为图片 → 双面/页码范围无意义）
    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tiff", ".tif"}

    # 连线后同步的网络操作统一放到后台线程，避免阻塞 UI；结果/日志经信号回主线程
    _cloudConnSyncLog = Signal(str)   # 后台同步网络 → 主线程 _log 的安全通道
    _ownerComboRefreshed = Signal()   # 成员名单同步完成 → 主线程刷新归属下拉
    _tabDisplayRefresh = Signal()     # 后台同步中换号完成后 → 主线程刷新标签页显示

    def __init__(self, config_path: str = "print_config.json", theme_manager: ThemeManager | None = None):
        super().__init__()
        self._config_path = config_path
        self._config = PrinterConfig.load(config_path)
        self._worker: Optional[PrintWorker] = None
        self._pending_jobs: list[PrintJob] = []
        self._theme_manager = theme_manager
        self._last_dir = self._config.last_dir
        self._copy_total_btn: Optional[QPushButton] = None
        self._copy_total_timer: Optional[QTimer] = None
        self._all_printed = False  # 是否已完成全部打印

        # ── 文件日志 ──
        self._log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(self._log_dir, exist_ok=True)
        self._file_logger = logging.getLogger("hn_local_tool")
        self._file_logger.setLevel(logging.DEBUG)
        if not self._file_logger.handlers:
            fh = logging.FileHandler(
                os.path.join(self._log_dir, "local_tool.log"),
                encoding="utf-8",
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            self._file_logger.addHandler(fh)
        self._file_logger.info("HN 本地打印工具启动")

        # ── 启动清理：上次会话遗留的"已完成订单"标签页 ──
        # 全部 job 均已打印成功（sent=True）的标签页在正常退出时已由 closeEvent 自动清理；
        # 此处兜底覆盖进程崩溃/强杀场景，避免已完成订单越积越多。
        tabs = self._config.tabs
        if tabs:
            kept_tabs = {}
            removed_count = 0
            for key, tab in tabs.items():
                jobs = tab.jobs or []
                if jobs and all(getattr(j, 'sent', False) for j in jobs):
                    removed_count += 1
                    continue  # 已完成订单 → 启动即清理
                kept_tabs[key] = tab
            if removed_count:
                self._config.tabs = kept_tabs if kept_tabs else {"1": TabSettings()}
                if self._config.active_tab not in self._config.tabs:
                    self._config.active_tab = next(iter(self._config.tabs))
                self._config.save(config_path)
                self._file_logger.info("启动清理：已移除 %d 个已完成订单标签页", removed_count)

        # ── 标签页系统 ──
        # 确保 tabs 中至少有一个标签
        if not self._config.tabs:
            self._config.tabs = {"1": TabSettings()}
        self._current_tab = self._config.active_tab or "1"
        if self._current_tab not in self._config.tabs:
            self._current_tab = next(iter(self._config.tabs.keys()))
        # 撤回备份（每个标签独立）
        self._cleared_jobs_backup: dict[str, list[PrintJob]] = {}
        # 已处理的云端任务 ID（防重复弹窗；跨重启持久化，防重启后重复打印）
        self._processed_cloud_tasks: set[int] = self._load_processed_tasks()
        # 无障碍自动打印：打印机忙时暂存的重试队列（打印完成后自动补打）
        self._auto_print_retry: list[dict] = []
        # 打印机消失/改名提示只弹一次（避免每次刷新都弹窗）
        self._printer_missing_warned: bool = False

        # ── 云端客户端 ──
        self._cloud_client: CloudClient | None = None
        self._stats_server: StatsServer | None = None  # 收支清算统计 HTTP 服务器
        self._cloud_tasks: dict[int, CloudTask] = {}  # task_id → CloudTask
        self._cloud_task_window: CloudTaskListWindow | None = None
        self._offline_sync: OfflineSync | None = None  # 初始化在 _init_cloud_client 之后
        # 无障碍打印：按订单收集自动任务，防抖后批量创建标签页并自动开始打印
        self._auto_print_queue: dict[int, list[CloudTask]] = {}  # order_id → tasks
        self._auto_print_timers: dict[int, QTimer] = {}          # order_id → debounce timer
        # 无障碍打印预约单（指定时间/倒计时）：到点才自动打印的状态机
        # order_id → {
        #   "target_ts": int,          # 预约起点 epoch（来自任务 scheduled_ts）
        #   "ready": {},               # task_id → CloudTask（已下载完成）
        #   "pending": {},             # task_id → CloudTask（已到达，下载中）
        #   "timer": QTimer | None,    # 3s 防抖（到达/就绪静默后评估）
        #   "print_timer": QTimer | None,  # 到点打印倒计时
        #   "frozen": bool,            # 到点文件未就绪已冻结
        #   "delayed_sent": bool,      # 已上报 download_delayed
        #   "printed": bool,           # 已开始打印（防重）
        #   "tab_key": str | None,     # 已创建的标签页
        # }
        self._scheduled_orders: dict[int, dict] = {}
        self._cloud_connected: bool = False  # 云端连接状态（预约冻结自恢复判断用）
        # 成员名单刷新防抖：下拉反复 Show（用户快速点开/关闭）不并发叠加请求
        self._owner_refresh_last_ts: float = 0.0
        self._owner_refresh_inflight: bool = False
        self._scheduled_check_timer = QTimer(self)
        self._scheduled_check_timer.setInterval(1000)
        self._scheduled_check_timer.timeout.connect(self._check_scheduled_timeouts)
        self._scheduled_check_timer.start()

        self.setWindowTitle("HN 本地打印工具")
        # 设置窗口图标
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "HN_printer.png")
        if os.path.isfile(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.setMinimumSize(900, 650)
        self.resize(1100, 720)

        self._setup_ui()
        self._load_config_to_ui()
        self._init_cloud_client()

        # ── 启动日志：让日志区启动即有内容 ──
        # （打包版默认不内置服务器地址/令牌，未配云端时也明确提示用户，避免误以为日志功能异常）
        if not self._config.cloud_enabled:
            self._log("ℹ 云端功能未启用：以本地模式运行，打印订单一律记入本地数据库。如需云端，请在「文件 → 云端连接设置」勾选「启用云端功能」。")
        elif self._config.cloud_token:
            self._log(f"☁ 云端已配置 {self._config.cloud_api_url}，正在连接...")
        else:
            self._log("ℹ 云端已启用但未配置令牌。请通过「文件 → 云端连接设置」填写服务器地址/令牌。")
        self._log("✅ HN 本地打印工具已启动")

    # ---- 标签页 key 安全排序 ----

    @staticmethod
    def _sorted_tab_keys(tabs: dict) -> list[str]:
        """安全排序标签页 key，非数字 key 按字符串自然顺序排末尾。"""
        numeric = []
        non_numeric = []
        for k in tabs.keys():
            try:
                numeric.append((int(k), k))
            except (ValueError, TypeError):
                non_numeric.append(k)
        numeric.sort(key=lambda x: x[0])
        return [k for _, k in numeric] + sorted(non_numeric)

    @staticmethod
    def _safe_int_key(key: str, default: int = 0) -> int:
        """安全地将标签页 key 转为 int，无法转换时返回 default。"""
        try:
            return int(key)
        except (ValueError, TypeError):
            return default

    # ---- 云端连接 ----

    def _init_cloud_client(self):
        """初始化云打印客户端（根据配置决定是否自动连接）。

        本地订单库（OfflineSync / SQLite）始终初始化——即便未启用云端，本地打印订单一律写入
        本地订单库（供「本地订单统计」读取）。CloudClient 仅在启用云端功能时才创建。
        """
        # 初始化本地订单库（离线/本地模式订单都写这里，供本地订单统计）
        self._offline_sync = OfflineSync()

        if not self._config.cloud_enabled:
            logger.info("云端功能未启用：以本地订单模式运行（CloudClient 不创建）")
            self._cloud_client = None
            self._cloud_task_window = None
            return

        # 持久化唯一 ID：同机多实例共用 hostname 会冲突，追加随机后缀并落盘
        client_id = _get_persistent_client_id()

        self._cloud_client = CloudClient(
            api_url=self._config.cloud_api_url,
            ws_url=self._config.cloud_ws_url,
            token=self._config.cloud_token,
            client_id=client_id,
            parent=self,
        )

        # 连接信号
        self._cloud_client.task_received.connect(self._on_cloud_task_received)
        self._cloud_client.task_updated.connect(self._on_cloud_task_updated)
        self._cloud_client.connection_changed.connect(self._on_cloud_connection_changed)
        self._cloud_client.status_message.connect(self._on_cloud_status_message)
        self._cloud_client.auth_failed.connect(self._on_cloud_auth_failed)
        self._cloud_client.order_canceled.connect(self._on_cloud_order_canceled)
        self._cloud_client.start_print.connect(self._on_cloud_start_print)

        # 连线后后台同步的信号 → 回主线程（日志写入 / 归属下拉刷新 / 标签页显示刷新）
        self._cloudConnSyncLog.connect(self._log)
        self._ownerComboRefreshed.connect(self._on_owner_combo_refreshed)
        self._tabDisplayRefresh.connect(self._on_refresh_tab_display_safe)

        # 初始化云端任务列表窗口（非模态，复用）
        self._cloud_task_window = CloudTaskListWindow(self)
        self._cloud_task_window.order_accepted.connect(self._on_cloud_order_accepted)
        self._cloud_task_window.order_rejected.connect(self._on_cloud_order_rejected)

        # 如果配置了 token 且启用了云端，自动连接
        if self._config.cloud_enabled and self._config.cloud_token:
            self._cloud_client.start()
            self._update_cloud_status()

        # 初始化离线同步引擎，启动后台定时同步（仅云端模式下自动把本地库订单定时上报；
        # 本地模式不启动——否则残留的旧云端 token 会把本地订单纯本地订单静默上传到旧云端）
        if self._config.cloud_enabled and self._config.cloud_token:
            self._offline_sync.start_background_sync(
                server_url=self._config.cloud_api_url,
                token=self._config.cloud_token,
                interval=60,
            )
        else:
            logger.info("云端功能未启用或未配置令牌：不启动后台订单同步")

    def _on_open_finance(self, tab: str = ""):
        """打开收支清算页面：云端模式与本地模式共用同一个 settlement.html，
        由 stats_server 注入 finance_mode 决定数据/配置的存储来源。
        本地模式（cloud_enabled=False）：配置/数据存本地 print_data.json。
        云端模式：存云端 finance/config；加载时若本地+云端都有真实数据则弹「合并」同步询问。
        tab 可选：'settings' 深链到「设置」标签（无成员引导用）。"""
        finance_mode = "cloud" if self._config.cloud_enabled else "local"
        # 如果已启动则直接打开，否则先启动
        if self._stats_server is None:
            self._stats_server = StatsServer(
                api_url=self._config.cloud_api_url,
                token=self._config.cloud_token,
                finance_mode=finance_mode,
            )
            self._stats_server.start_in_thread()
        else:
            # 已启动但配置可能已变更（如云端设置对话框保存后），同步一次代理目标与模式
            self._stats_server.update_config(self._config.cloud_api_url, self._config.cloud_token)
            if self._config.cloud_enabled:
                self._stats_server.finance_mode = "cloud"
            else:
                self._stats_server.finance_mode = "local"

        # 附加启动令牌：settlement.html 要求每次启动的随机 token 才能访问；mode 显式传存储模式
        url = (f"{self._stats_server.url}/settlement.html"
               f"?token={self._stats_server.launch_token}&mode={finance_mode}")
        if isinstance(tab, str) and tab in ("settings",):
            url += "&tab=settings"
        mode_txt = "云端收支清算" if finance_mode == "cloud" else "本地收支清算"
        logger.info(f"打开{mode_txt}页面: {self._stats_server.url}")
        QDesktopServices.openUrl(QUrl(url))

    def _on_cloud_settings(self):
        """打开云端连接设置对话框。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("☁ 云端连接设置")
        dlg.setMinimumWidth(460)
        dlg.setModal(True)

        layout = QVBoxLayout(dlg)
        layout.setSpacing(10)
        layout.setContentsMargins(20, 16, 20, 16)

        # 说明文字
        title = QLabel("<b>☁ 云打印服务器连接设置</b>")
        layout.addWidget(title)
        layout.addWidget(QLabel("连接到你部署的后端服务器，接收小程序/APP 提交的打印任务。"))

        layout.addSpacing(8)

        # 云端总开关：关闭时禁用所有云端功能，程序以纯本地订单模式运行
        cloud_switch = ThemedCheckBox("启用云端功能", theme_manager=self._theme_manager)
        cloud_switch.setToolTip("关闭后将以纯本地模式运行（本地订单库记录打印，收支清算显示本地订单统计）。切换后需重启应用生效。")
        cloud_switch.setChecked(bool(self._config.cloud_enabled))
        layout.addWidget(cloud_switch)

        layout.addSpacing(8)

        # API 地址
        layout.addWidget(QLabel("API 地址:"))
        api_input = QLineEdit(self._config.cloud_api_url)
        api_input.setPlaceholderText("https://your-server.com")
        layout.addWidget(api_input)

        # WebSocket 地址
        layout.addWidget(QLabel("WebSocket 地址:"))
        ws_input = QLineEdit(self._config.cloud_ws_url)
        ws_input.setPlaceholderText("wss://your-server.com")
        layout.addWidget(ws_input)

        # Token
        layout.addWidget(QLabel("认证 Token:"))
        token_input = QLineEdit(self._config.cloud_token)
        token_input.setPlaceholderText("打印机认证 token")
        token_input.setEchoMode(QLineEdit.Password)
        layout.addWidget(token_input)

        # 连接状态
        layout.addSpacing(4)
        status_text = "🟢 已连接" if (self._cloud_client and self._cloud_client.is_connected()) else "🔴 未连接"
        status_label = QLabel(status_text)
        layout.addWidget(status_label)

        # 按钮行
        layout.addSpacing(8)
        btn_row = QHBoxLayout()

        if self._cloud_client and self._cloud_client.is_connected():
            disconnect_btn = QPushButton("断开连接")
            disconnect_btn.setObjectName("cloudDisconnected")
            disconnect_btn.clicked.connect(lambda: self._cloud_client.stop())
            disconnect_btn.clicked.connect(lambda: status_label.setText("🔴 未连接"))
            btn_row.addWidget(disconnect_btn)

        btn_row.addStretch()

        save_btn = QPushButton("保存")
        save_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(save_btn)

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_btn)

        layout.addLayout(btn_row)

        if dlg.exec() == QDialog.Accepted:
            # 保存配置到内存（云端开关以对话中的勾选为准）
            old_enabled = bool(self._config.cloud_enabled)
            self._config.cloud_api_url = api_input.text().strip()
            self._config.cloud_ws_url = ws_input.text().strip()
            self._config.cloud_token = token_input.text().strip()
            self._config.cloud_enabled = bool(cloud_switch.isChecked())

            # 立刻写入磁盘，防止程序崩溃丢失
            try:
                self._config.save(self._config_path)
            except Exception as e:
                logger.warning(f"保存云端配置失败: {e}")

            # 开关状态发生变化 → 云端 UI 与机制差异巨大，需重启应用重新装配界面
            if old_enabled != self._config.cloud_enabled:
                if not self._config.cloud_enabled:
                    # 关闭云端：确认 → 冲刷未同步订单（此时云端仍连接，可换号/上报）→ 清空缓存数据
                    if not self._confirm_cloud_off_and_wipe():
                        # 用户取消关闭：恢复旧开关状态（配置已写盘，这里改回内存+写盘）
                        self._config.cloud_enabled = True
                        try:
                            self._config.save(self._config_path)
                        except Exception:
                            pass
                        self._log("❌ 已取消关闭云端")
                        return
                self._cloud_client and self._cloud_client.stop()
                self._restart_for_cloud_switch(self._config.cloud_enabled)
                return

            # 开关未变化：仅更新连接（用户可能只是改了地址/token）
            # 更新 CloudClient 并连接/停止
            if self._config.cloud_enabled:
                if self._cloud_client:
                    self._cloud_client.stop()
                    self._cloud_client.api_url = self._config.cloud_api_url
                    self._cloud_client.ws_url = self._config.cloud_ws_url
                    self._cloud_client.token = self._config.cloud_token
                    self._cloud_client.start()
                self._update_cloud_status()
            else:
                if self._cloud_client:
                    self._cloud_client.stop()
                self._update_cloud_status()
            # 同步 stats_server（收支清算页代理）：若已启动，配置可能仍旧占位地址，
            # 不同步会导致代理请求打到不存在的占位域名 → SSLError（重启后才恢复）。
            if self._stats_server:
                self._stats_server.update_config(self._config.cloud_api_url, self._config.cloud_token)
            self._log("☁ 云端配置已保存并写入磁盘" + ("，正在连接..." if self._config.cloud_enabled else "，云端已关闭"))

    def _restart_for_cloud_switch(self, enabled: bool):
        """云端总开关变化后：提示用户并重启应用，重新装配本地/云端 UI。"""
        from PySide6.QtWidgets import QMessageBox
        state_txt = "启用云端" if enabled else "关闭云端（切为本地模式）"
        reply = QMessageBox.question(
            self,
            "需要重启生效",
            f"{state_txt}需要重启应用才能生效。\n\n"
            "重启会关闭当前窗口，本轮所有未打印的任务不会被自动保存（请先确认无需打印）。\n\n"
            "是否立即重启？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply == QMessageBox.Yes:
            self._restart_app()
        else:
            # 不重启：配置已保存，下次启动生效
            self._log(f"🔁 云端开关已保存，下次启动时生效（{state_txt}）")

    def _confirm_cloud_off_and_wipe(self) -> bool:
        """关闭云端前的确认与清理。返回 True 表示继续关闭（云端缓存数据已清空）。"""
        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self,
            "关闭云端功能",
            "关闭云端功能后，本机将清空云端缓存数据（成员名单、收支数据、已同步订单），转为纯本地模式。\n\n"
            "机器配置（打印机、价格、派送地点）会保留；未同步订单会先尝试上传云端。\n\n"
            "是否继续关闭？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return False
        self._flush_pending_orders_before_cloud_off()
        self._wipe_cloud_cached_data()
        return True

    def _flush_pending_orders_before_cloud_off(self):
        """关闭云端前尽力冲刷未同步订单（不中断流程，仅记录日志）。
        此时云端仍连接：先处理待换号单（-L / 遗留 LOCAL-*），再上报其余待同步单。"""
        if not self._offline_sync:
            return
        # ① 待换号单 → 换正式号并上报（云端仍连接时才能换号）
        if self._cloud_client and self._cloud_client.is_connected():
            try:
                self._sync_local_orders_to_cloud()
            except Exception as e:
                self._log(f"⚠️ 关闭云端前换号同步失败: {e}")
        # ② 其余待同步单 → 直接上报
        pending = self._offline_sync.pending_count()
        if pending > 0:
            if self._cloud_client and self._cloud_client.api_url and self._cloud_client.token:
                try:
                    n = self._offline_sync.sync_all_pending_orders(
                        server_url=self._cloud_client.api_url,
                        token=self._cloud_client.token,
                    )
                    if n > 0:
                        self._log(f"📋 关闭云端前已上传 {n} 条未同步订单")
                except Exception as e:
                    self._log(f"⚠️ 关闭云端前同步失败: {e}")
        remain = self._offline_sync.count_unsynced()
        if remain > 0:
            self._log(f"⚠️ 仍有 {remain} 条未同步订单，将随云端缓存数据一并清空（云端不可达或上传失败）")

    def _wipe_cloud_cached_data(self):
        """关闭云端时清空本机云端缓存数据：
        成员名单、收支数据文件、openid 绑定、本地订单库、标签页归属与订单号。
        机器配置（打印机/价格/派送地点）与文件队列保留。"""
        logger.info("开始清空云端缓存数据（关闭云端功能）")
        # 1. 成员名单（归属下拉回到「(无成员)」）
        self._config.admin_names = []
        # 2. 标签页归属与订单号（保留文件队列与打印设置；订单号下次打印重新分配）
        for tab in self._config.tabs.values():
            tab.owner_name = ""
            for job in tab.jobs:
                job.order_number = ""
        try:
            self._config.save(self._config_path)
        except Exception as e:
            logger.warning(f"保存配置失败: {e}")
        # 3. 本地订单库（已尽力冲刷；残留记录随清空移除）
        if self._offline_sync:
            self._offline_sync.clear_all()
        # 4. 收支数据文件 + openid 绑定文件
        try:
            clear_local_data_files()
        except Exception as e:
            logger.warning(f"删除收支数据文件失败: {e}")
        self._log("🧹 已清空本机云端缓存数据（成员名单/收支数据/本地订单库），下次启动以纯本地模式运行")

    def _restart_app(self):
        """重启当前应用（自动重新装配 UI）。"""
        import sys as _sys
        try:
            self._save_config()
            if self._stats_server:
                self._stats_server.stop()
            if self._cloud_client:
                self._cloud_client.stop()
            python = _sys.executable
            cmd = [python, os.path.abspath(_sys.argv[0])]
            _sys.argv and cmd.extend(_sys.argv[1:])
            # 用 subprocess 异步启动新实例，当前实例随后退出
            import subprocess
            subprocess.Popen(cmd, cwd=os.getcwd())
        except Exception as e:
            logger.warning(f"自动重启失败: {e}")
        QApplication.quit()

    def _toggle_cloud_connection(self):
        """状态栏按钮：切换云端连接。"""
        if not self._cloud_client:
            # 还没初始化 → 打开设置
            self._on_cloud_settings()
            return
        if self._cloud_client.is_connected():
            self._cloud_client.stop()
        else:
            if not self._config.cloud_token:
                # 没配置 token → 打开设置对话框
                self._on_cloud_settings()
                return
            self._cloud_client.start()
        self._update_cloud_status()

    def _update_cloud_status(self):
        """更新状态栏的云端状态指示器。"""
        connected = self._cloud_client and self._cloud_client.is_connected()
        if hasattr(self, "_cloud_status_indicator") and self._cloud_status_indicator:
            # 云端功能启用状态下才存在指示器/按钮（本地模式不创建）
            if connected:
                self._cloud_status_indicator.setText("☁ 已连接")
                self._cloud_status_indicator.setObjectName("cloudStatusOn")
                self._cloud_status_btn.setText("断开云端")
            else:
                self._cloud_status_indicator.setText("☁ 未连接")
                self._cloud_status_indicator.setObjectName("cloudStatusOff")
                self._cloud_status_btn.setText("连接云端")
            self._cloud_status_indicator.style().unpolish(self._cloud_status_indicator)
            self._cloud_status_indicator.style().polish(self._cloud_status_indicator)
        # 收支清算入口：云端未连接时置灰（数据存云端，离线不可用）；
        # 本地模式下（未启用云端）始终可用（读本地订单库）。
        if hasattr(self, "_finance_action"):
            if self._config.cloud_enabled:
                self._finance_action.setEnabled(connected)
            else:
                self._finance_action.setEnabled(True)

    # ---- UI 构建 ----

    def _setup_ui(self):
        """构建完整界面。"""
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 8, 12, 12)
        root.setSpacing(0)

        # -- 菜单栏 --
        self._setup_menu()

        # -- 顶部：打印机信息 + 配置 --
        top_container = QWidget()
        top_layout = QVBoxLayout(top_container)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(8)
        self._setup_top_bar(top_layout)

        # -- 中部：文件列表 + 编辑面板（QSplitter） --
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._setup_file_table())
        splitter.addWidget(self._setup_edit_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([700, 200])
        splitter.setCollapsible(1, False)
        top_layout.addWidget(splitter, 1)

        # -- 进度条 --
        self._progress_bar = QProgressBar()
        self._progress_bar.setValue(0)
        top_layout.addWidget(self._progress_bar)

        # -- 按钮栏 --
        top_layout.addLayout(self._setup_button_bar())

        # -- 日志区域（可拖动顶边调整高度） --
        self._log_text = QTextEdit()
        self._log_text.setObjectName("logTextEdit")
        self._log_text.setReadOnly(True)
        self._log_text.setAcceptRichText(True)
        self._log_text.setMinimumHeight(24)
        _enable_smooth_scroll(self._log_text)

        v_splitter = QSplitter(Qt.Vertical)
        v_splitter.addWidget(top_container)
        v_splitter.addWidget(self._log_text)
        v_splitter.setStretchFactor(0, 1)
        v_splitter.setStretchFactor(1, 0)
        v_splitter.setSizes([650, 50])
        root.addWidget(v_splitter, 1)

        # -- 状态栏 --
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_label = QLabel("就绪")
        self._status_bar.addWidget(self._status_label)

        self._status_bar.addPermanentWidget(QLabel(" "))

        # 云端状态指示器/连接按钮：仅在启用云端功能时显示（本地模式不出现）
        self._cloud_status_indicator = None
        self._cloud_status_btn = None
        if self._config.cloud_enabled:
            self._cloud_status_indicator = QLabel("☁ 未连接")
            self._cloud_status_indicator.setObjectName("cloudStatusOff")
            self._status_bar.addPermanentWidget(self._cloud_status_indicator)

            self._cloud_status_btn = QPushButton("连接云端")
            self._cloud_status_btn.setFixedWidth(80)
            self._cloud_status_btn.clicked.connect(self._toggle_cloud_connection)
            self._status_bar.addPermanentWidget(self._cloud_status_btn)

    def _setup_menu(self):
        """设置菜单栏。"""
        mb = self.menuBar()

        # 文件菜单
        file_menu = mb.addMenu("文件(&F)")

        open_action = QAction("打开(&O)", self)
        open_action.triggered.connect(self._on_add_files)
        file_menu.addAction(open_action)

        file_menu.addSeparator()

        cloud_action = QAction("云端(&C)", self)
        cloud_action.triggered.connect(self._on_cloud_settings)
        file_menu.addAction(cloud_action)

        locations_action = QAction("地点(&L)", self)
        locations_action.triggered.connect(self._on_manage_locations)
        file_menu.addAction(locations_action)

        file_menu.addSeparator()

        # 收支清算入口：启用云端 = 云端收支清算；关闭云端 = 收支统计（本地模式，读本地数据）
        if self._config.cloud_enabled:
            self._finance_action = QAction("📊 收支清算(&S)", self)
            self._finance_action.triggered.connect(self._on_open_finance)
            self._finance_action.setToolTip("收支清算数据存于云端，需连接云端后才能使用")
        else:
            self._finance_action = QAction("📊 收支统计(&S)", self)
            self._finance_action.triggered.connect(self._on_open_finance)
            self._finance_action.setToolTip("本地模式：收支统计，配置与数据存于本地（未连接云端）")
        file_menu.addAction(self._finance_action)

        file_menu.addSeparator()

        exit_action = QAction("退出(&X)", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # 主题菜单
        if self._theme_manager is not None:
            theme_menu = mb.addMenu("主题(&T)")
            self._setup_theme_menu(theme_menu)

        # 帮助菜单
        help_menu = mb.addMenu("帮助(&H)")
        shortcuts_action = QAction("快捷键(&K)", self)
        shortcuts_action.triggered.connect(self._on_shortcuts)
        help_menu.addAction(shortcuts_action)
        help_menu.addSeparator()
        selfcheck_action = QAction("自检(&S)", self)
        selfcheck_action.triggered.connect(self._on_self_check)
        help_menu.addAction(selfcheck_action)
        log_action = QAction("日志(&L)", self)
        log_action.triggered.connect(self._on_show_log_manager)
        help_menu.addAction(log_action)
        help_menu.addSeparator()
        about_action = QAction("关于(&A)", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)

        # 全局快捷键（ApplicationShortcut 确保不被子控件拦截）
        self._shortcut_copy = QShortcut(QKeySequence("Ctrl+C"), self)
        self._shortcut_copy.setContext(Qt.ApplicationShortcut)
        self._shortcut_copy.activated.connect(self._on_shortcut_copy_total)
        self._shortcut_copyd = QShortcut(QKeySequence("Ctrl+Shift+C"), self)
        self._shortcut_copyd.setContext(Qt.ApplicationShortcut)
        self._shortcut_copyd.activated.connect(self._on_shortcut_copy_detail)
        # Delete 需防止在文本输入框中误触发
        self._shortcut_del = QShortcut(QKeySequence(QKeySequence.Delete), self)
        self._shortcut_del.setContext(Qt.ApplicationShortcut)
        self._shortcut_del.activated.connect(self._on_shortcut_delete)
        self._shortcut_ctrl_d = QShortcut(QKeySequence("Ctrl+D"), self)
        self._shortcut_ctrl_d.setContext(Qt.ApplicationShortcut)
        self._shortcut_ctrl_d.activated.connect(self._on_remove_selected)
        self._shortcut_paste = QShortcut(QKeySequence("Ctrl+V"), self)
        self._shortcut_paste.setContext(Qt.ApplicationShortcut)
        self._shortcut_paste.activated.connect(self._on_shortcut_paste)

    def _setup_theme_menu(self, menu):
        """构建主题切换子菜单（单选模式）。"""
        from PySide6.QtGui import QActionGroup

        group = QActionGroup(self)
        group.setExclusive(True)

        for mode in [MODE_SYSTEM, MODE_LIGHT, MODE_DARK]:
            action = QAction(MODE_LABELS[mode], self)
            action.setCheckable(True)
            action.setData(mode)
            if mode == self._theme_manager.mode:
                action.setChecked(True)
            action.triggered.connect(self._on_theme_changed)
            group.addAction(action)
            menu.addAction(action)

    def _setup_top_bar(self, root: QVBoxLayout):
        """顶部：打印机选择 + 保留 PDF 选项。"""
        layout = QHBoxLayout()

        layout.addWidget(QLabel("打印机:"))

        self._printer_combo = QComboBox()
        self._printer_combo.setMinimumWidth(200)
        _disable_combo_wheel(self._printer_combo)
        layout.addWidget(self._printer_combo, 1)

        btn_refresh = QPushButton("🔄")
        btn_refresh.setObjectName("btnRefreshPrinters")
        btn_refresh.setToolTip("刷新打印机列表")
        btn_refresh.setFixedSize(36, 36)
        btn_refresh.clicked.connect(self._on_refresh_printers)
        layout.addWidget(btn_refresh)

        layout.addWidget(QLabel("保存转换副本到桌面:"))

        self._keep_temp_check = QComboBox()
        self._keep_temp_check.addItems(["否", "是"])
        self._keep_temp_check.setCurrentIndex(0)
        self._keep_temp_check.currentIndexChanged.connect(self._on_keep_temp_changed)
        _disable_combo_wheel(self._keep_temp_check)
        layout.addWidget(self._keep_temp_check)

        layout.addWidget(QLabel("  单面:"))

        self._simplex_price_spin = QDoubleSpinBox()
        self._simplex_price_spin.setButtonSymbols(QDoubleSpinBox.NoButtons)
        self._simplex_price_spin.setRange(0.01, 99.99)
        self._simplex_price_spin.setDecimals(2)
        self._simplex_price_spin.setSingleStep(0.01)
        self._simplex_price_spin.setValue(self._config.simplex_price)
        self._simplex_price_spin.setFixedWidth(60)
        self._simplex_price_spin.valueChanged.connect(self._on_price_changed)
        layout.addWidget(self._simplex_price_spin)
        layout.addWidget(QLabel("元/张"))

        layout.addWidget(QLabel(" 双面:"))

        self._duplex_price_spin = QDoubleSpinBox()
        self._duplex_price_spin.setButtonSymbols(QDoubleSpinBox.NoButtons)
        self._duplex_price_spin.setRange(0.01, 99.99)
        self._duplex_price_spin.setDecimals(2)
        self._duplex_price_spin.setSingleStep(0.01)
        self._duplex_price_spin.setValue(self._config.duplex_price)
        self._duplex_price_spin.setFixedWidth(60)
        self._duplex_price_spin.valueChanged.connect(self._on_price_changed)
        layout.addWidget(self._duplex_price_spin)
        layout.addWidget(QLabel("元/张"))

        layout.addWidget(QLabel(" DPI:"))

        self._render_dpi_combo = QComboBox()
        self._render_dpi_combo.addItems(["高速(200)", "标清(300)", "清晰(400)", "高清(600)"])
        _disable_combo_wheel(self._render_dpi_combo)
        self._render_dpi_combo.setToolTip("全局默认渲染质量，DPI越高越清晰但越慢")
        self._render_dpi_combo.currentIndexChanged.connect(self._on_render_dpi_changed)
        layout.addWidget(self._render_dpi_combo)

        root.addLayout(layout)

        # ---- 第二行：标签页附加服务 ----
        row2 = QHBoxLayout()
        row2.setSpacing(6)

        # 标签页指示器
        self._tab_scope_label = QLabel(f"📑 标签页 {self._current_tab}")
        self._tab_scope_label.setObjectName("tabScopeLabel")
        row2.addWidget(self._tab_scope_label)
        row2.addSpacing(8)

        # —— 先创建所有控件 ——

        # 派送开关
        self._delivery_onoff_combo = QComboBox()
        self._delivery_onoff_combo.addItems(["否", "是"])
        _disable_combo_wheel(self._delivery_onoff_combo)
        self._delivery_onoff_combo.currentIndexChanged.connect(self._on_delivery_toggled)

        # 派送地点
        self._delivery_location_combo = QComboBox()
        self._delivery_location_combo.addItems(list(self._config.delivery_percentages.keys()))
        _disable_combo_wheel(self._delivery_location_combo)
        self._delivery_location_combo.currentIndexChanged.connect(self._on_delivery_location_changed)

        # 派送百分比
        self._delivery_percent_spin = QDoubleSpinBox()
        self._delivery_percent_spin.setButtonSymbols(QDoubleSpinBox.NoButtons)
        self._delivery_percent_spin.setRange(0.0, 100.0)
        self._delivery_percent_spin.setDecimals(0)
        self._delivery_percent_spin.setSingleStep(1)
        self._delivery_percent_spin.setSuffix("%")
        self._delivery_percent_spin.setFixedWidth(60)
        self._delivery_percent_spin.valueChanged.connect(self._on_delivery_percent_changed)

        # 优先程度
        self._urgency_combo = QComboBox()
        self._urgency_combo.addItems(list(self._config.urgency_prices.keys()))
        _disable_combo_wheel(self._urgency_combo)
        self._urgency_combo.currentIndexChanged.connect(self._on_urgency_changed)

        # 优先级价格
        self._urgency_price_spin = QDoubleSpinBox()
        self._urgency_price_spin.setButtonSymbols(QDoubleSpinBox.NoButtons)
        self._urgency_price_spin.setRange(0.0, 99.99)
        self._urgency_price_spin.setDecimals(2)
        self._urgency_price_spin.setSingleStep(0.01)
        self._urgency_price_spin.setFixedWidth(60)
        self._urgency_price_spin.valueChanged.connect(self._on_urgency_price_changed)

        # 首页开关
        self._cover_page_onoff_combo = QComboBox()
        self._cover_page_onoff_combo.addItems(["否", "是"])
        _disable_combo_wheel(self._cover_page_onoff_combo)
        self._cover_page_onoff_combo.currentIndexChanged.connect(self._on_cover_page_toggled)

        # 首页价格
        self._cover_page_price_spin = QDoubleSpinBox()
        self._cover_page_price_spin.setButtonSymbols(QDoubleSpinBox.NoButtons)
        self._cover_page_price_spin.setRange(0.0, 99.99)
        self._cover_page_price_spin.setDecimals(2)
        self._cover_page_price_spin.setSingleStep(0.01)
        self._cover_page_price_spin.setValue(self._config.cover_page_price)
        self._cover_page_price_spin.setFixedWidth(60)
        self._cover_page_price_spin.valueChanged.connect(self._on_price_changed)

        # —— 派送/地点居左 + 优先级居右 + 首页居右 ——

        # Group 1: 派送（居左，最小宽度）
        g1 = QHBoxLayout()
        g1.setSpacing(2)
        g1.addWidget(QLabel("派送:"))
        g1.addWidget(self._delivery_onoff_combo)
        row2.addLayout(g1)

        row2.addSpacing(12)

        # Group 2: 地点（居左，平分剩余空间）
        g2 = QHBoxLayout()
        g2.setSpacing(2)
        g2.addWidget(QLabel("地点:"))
        g2.addWidget(self._delivery_location_combo)
        g2.addWidget(self._delivery_percent_spin)
        g2.addStretch()
        row2.addLayout(g2, 1)

        # Group 3: 优先级（居右，平分剩余空间）
        g3 = QHBoxLayout()
        g3.setSpacing(2)
        g3.addStretch()
        g3.addWidget(QLabel("优先级:"))
        g3.addWidget(self._urgency_combo)
        g3.addWidget(self._urgency_price_spin)
        g3.addWidget(QLabel("元"))
        row2.addLayout(g3, 1)

        row2.addSpacing(12)

        # Group 4: 首页（居右，最小宽度）
        g4 = QHBoxLayout()
        g4.setSpacing(2)
        g4.addStretch()
        g4.addWidget(QLabel("首页:"))
        g4.addWidget(self._cover_page_onoff_combo)
        g4.addWidget(self._cover_page_price_spin)
        g4.addWidget(QLabel("元"))
        row2.addLayout(g4)

        # 初始状态：派送=否时禁用地点和百分比
        self._delivery_location_combo.setEnabled(False)
        self._delivery_percent_spin.setEnabled(False)

        root.addLayout(row2)

        # 统一输入框高度为下拉框高度（延迟到布局完成后测量实际高度）
        def _normalize_spin_heights():
            combo_h = self._printer_combo.height()
            if combo_h > 0:
                for sp in (self._simplex_price_spin, self._duplex_price_spin,
                           self._delivery_percent_spin, self._urgency_price_spin,
                           self._cover_page_price_spin):
                    sp.setFixedHeight(combo_h)
        QTimer.singleShot(0, _normalize_spin_heights)

    def _setup_file_table(self) -> QWidget:
        """左侧：标签页选择器 + 文件列表表格。"""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # ── 标签页选择器：[-] [N] [+] ──
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(0, 0, 0, 0)
        tab_row.setSpacing(2)

        self._tab_btn_minus = QPushButton("−")
        self._tab_btn_minus.setObjectName("tabBtnMinus")
        self._tab_btn_minus.setFixedSize(24, 28)
        self._tab_btn_minus.setToolTip("上一个标签页")
        self._tab_btn_minus.clicked.connect(lambda: self._switch_tab(-1))

        self._tab_label = QLabel(self._current_tab)
        self._tab_label.setObjectName("tabLabel")
        self._tab_label.setAlignment(Qt.AlignCenter)
        self._tab_label.setFixedSize(36, 28)
        self._tab_label.setCursor(Qt.PointingHandCursor)
        self._tab_label.setToolTip("点击管理标签页")
        font_tab = QFont(self.font()); font_tab.setPointSize(12); font_tab.setBold(True)
        self._tab_label.setFont(font_tab)
        # 点击标签数字 → 弹出标签页管理窗口
        self._tab_label.mousePressEvent = lambda e: self._show_tab_manager()

        self._tab_btn_plus = QPushButton("+")
        self._tab_btn_plus.setObjectName("tabBtnPlus")
        self._tab_btn_plus.setFixedSize(24, 28)
        self._tab_btn_plus.setToolTip("下一个标签页 / 新建标签页")
        self._tab_btn_plus.clicked.connect(lambda: self._switch_tab(1))

        self._tab_btn_cleanup_empty = QPushButton("🗑 空")
        self._tab_btn_cleanup_empty.setObjectName("tabBtnCleanupEmpty")
        self._tab_btn_cleanup_empty.setFixedHeight(28)
        self._tab_btn_cleanup_empty.setToolTip("删除所有空标签页（不含当前）")
        self._tab_btn_cleanup_empty.clicked.connect(self._on_cleanup_empty_tabs)

        tab_row.addWidget(self._tab_btn_minus)
        tab_row.addWidget(self._tab_label)
        tab_row.addWidget(self._tab_btn_plus)
        tab_row.addWidget(self._tab_btn_cleanup_empty)
        tab_row.addStretch()

        layout.addLayout(tab_row)

        # 刷新标签页显示状态
        self._refresh_tab_display()

        # ── 文件表格 (v3.1 风格) ──
        self._table = DropTableWidget()
        self._table.setColumnCount(8)
        self._table.setHorizontalHeaderLabels(["文件名", "份数", "单/双面", "页码范围", "页数", "方向", "引擎", "费用"])
        self._table.filesDropped.connect(self._on_files_dropped)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        _enable_smooth_scroll(self._table)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.cellDoubleClicked.connect(self._on_table_double_click)
        self._table.verticalHeader().setVisible(False)

        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(self.COL_FILE, QHeaderView.Stretch)
        hh.setSectionResizeMode(self.COL_COPIES, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(self.COL_DUPLEX, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(self.COL_RANGE, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(self.COL_PAGES, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(self.COL_ORIENT, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(self.COL_ENGINE, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(self.COL_COST, QHeaderView.ResizeToContents)

        # 选中行变化 → 右侧编辑面板同步
        self._table.selectionModel().selectionChanged.connect(self._on_table_selection)
        # 右键菜单
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_table_context_menu)

        layout.addWidget(self._table, 1)

        # ── 合计费用 ──
        total_row = QHBoxLayout()
        total_row.setContentsMargins(0, 0, 0, 0)
        # F9: 当前标签页订单号
        self._order_number_label = QLabel("📋 未分配订单号")
        self._order_number_label.setObjectName("orderNumberLabel")
        total_row.addWidget(self._order_number_label)
        # 订单归属（v24）：订单号右侧 —— 归属管理员下拉 + 管理员自行打印勾选
        self._owner_label = QLabel("归属")
        self._owner_label.setObjectName("ownerLabel")
        total_row.addWidget(self._owner_label)
        self._owner_combo = QComboBox()
        self._owner_combo.setObjectName("ownerCombo")
        # 只允许从下拉选择（名单 = 收支清算成员管理中的成员），不允许直接输入
        self._owner_combo.setEditable(False)
        self._owner_combo.setFixedWidth(110)
        self._owner_combo.setToolTip("这笔订单属于谁（从下拉选择，名单与收支清算成员管理一致）")
        self._owner_combo.currentTextChanged.connect(self._on_owner_changed)
        # 无成员时下拉只有「(无成员)」一项：用户点击该项（含再次点击当前项）→ 弹添加成员引导
        self._owner_combo.activated.connect(self._on_owner_activated)
        # 弹出下拉时同步收支清算成员名单（与「收支清算 → 成员管理」保持一致）
        self._owner_combo_refresh_filter = _OwnerComboRefreshFilter(self._refresh_owner_names_from_cloud)
        self._owner_combo.view().viewport().installEventFilter(self._owner_combo_refresh_filter)
        total_row.addWidget(self._owner_combo)
        self._admin_print_check = ThemedCheckBox("管理员自行打印", theme_manager=self._theme_manager)
        self._admin_print_check.setObjectName("adminPrintCheck")
        self._admin_print_check.setToolTip("勾选 = 这是管理员自己打印的订单，而不是顾客的订单")
        self._admin_print_check.toggled.connect(self._on_admin_print_toggled)
        total_row.addWidget(self._admin_print_check)
        total_row.addStretch()
        self._total_label = QLabel("合计: ¥0.00")
        self._total_label.setObjectName("totalCostLabel")
        self._total_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        total_row.addWidget(self._total_label)
        self._copy_total_btn = QPushButton("📋 复制")
        self._copy_total_btn.setFixedWidth(100)
        self._copy_total_btn.clicked.connect(self._on_copy_total)
        self._copy_total_timer = QTimer(self)
        self._copy_total_timer.setSingleShot(True)
        self._copy_total_timer.timeout.connect(self._reset_copy_button)
        total_row.addWidget(self._copy_total_btn)
        self._convert_workers: list[ConvertWorker] = []
        self._copy_detail_btn = QPushButton("📋 复制计费明细")
        self._copy_detail_btn.setVisible(False)
        self._copy_detail_btn.clicked.connect(self._on_copy_detail)
        total_row.addWidget(self._copy_detail_btn)
        self._detail_toggle_btn = QPushButton("⏷")
        self._detail_toggle_btn.setObjectName("detailToggleBtn")
        self._detail_toggle_btn.setCheckable(True)
        self._detail_toggle_btn.setToolTip("展开计费明细")
        self._detail_toggle_btn.toggled.connect(self._on_toggle_detail)
        total_row.addWidget(self._detail_toggle_btn)
        layout.addLayout(total_row)

        # ── 添加文件按钮 ──
        btn_add = QPushButton("📂 添加文件")
        btn_add.clicked.connect(self._on_add_files)
        layout.addWidget(btn_add)

        return container

    # ──────── 标签页切换 ────────

    def _switch_tab(self, delta: int):
        """切换标签页。delta: -1 上一页, +1 下一页（若已在最后一页则新建）。"""
        tab_keys = self._sorted_tab_keys(self._config.tabs)
        if not tab_keys:
            self._config.tabs = {"1": TabSettings()}
            tab_keys = ["1"]

        try:
            idx = tab_keys.index(self._current_tab)
        except ValueError:
            idx = 0

        # 切换标签页时取消撤回状态（备份属于旧标签页）
        self._cancel_undo_if_active()

        new_idx = idx + delta
        if new_idx < 0:
            return

        if new_idx >= len(tab_keys):
            # 在最后一页点 + → 新建标签
            last_num = self._safe_int_key(tab_keys[-1]) if tab_keys else 0
            new_key = str(last_num + 1)
            self._config.tabs[new_key] = TabSettings()
            self._current_tab = new_key
            self._config.active_tab = new_key
            self._save_config()
            self._rebuild_table()
            self._refresh_tab_display()
            self._sync_edit_enabled(False)
            self._log(f"📑 新建标签页 {new_key}")
            return

        self._current_tab = tab_keys[new_idx]
        self._config.active_tab = self._current_tab
        self._save_config()
        self._rebuild_table()
        self._refresh_tab_display()
        self._sync_edit_enabled(False)

        # F3: 向左切换后，删除序号更大的空标签页
        if delta < 0:
            self._cleanup_empty_tabs(after_key=self._current_tab)

    def _has_empty_tabs_except_current(self) -> bool:
        """检查是否存在非当前标签页的空标签页。"""
        for key, tab in self._config.tabs.items():
            if key != self._current_tab and len(tab.jobs) == 0:
                return True
        return False

    def _renumber_tabs(self):
        """删除标签页后重新从 1 开始编号。更新 _current_tab 指向同一标签的新 key。"""
        old_current = self._current_tab
        old_tabs = self._config.tabs
        sorted_keys = self._sorted_tab_keys(old_tabs)
        new_tabs = {}
        new_current = "1"
        new_idx = 1
        key_map = {}
        for old_key in sorted_keys:
            new_key = str(new_idx)
            key_map[old_key] = new_key
            new_tabs[new_key] = old_tabs[old_key]
            if old_key == old_current:
                new_current = new_key
            new_idx += 1
        self._config.tabs = new_tabs
        self._current_tab = new_current
        self._config.active_tab = new_current
        # 预约单状态机中的 tab_key 同步重映射（key 已重新编号）
        for st in self._scheduled_orders.values():
            old_key = st.get("tab_key")
            if old_key and old_key in key_map:
                st["tab_key"] = key_map[old_key]

    def _cleanup_empty_tabs(self, after_key: str | None = None):
        """删除空标签页。after_key 不为 None 时仅删除 key > after_key 的标签页。"""
        removed = []
        for key in self._sorted_tab_keys(self._config.tabs):
            if key == self._current_tab:
                continue
            if after_key is not None and self._safe_int_key(key) <= self._safe_int_key(after_key):
                continue
            tab = self._config.tabs.get(key)
            if tab and len(tab.jobs) == 0:
                removed.append(key)
        for key in removed:
            del self._config.tabs[key]
            self._log(f"🗑 已删除空标签页 {key}")
            # 预约单指向该标签页 → 联动清理状态机（防到点打印空标签页）
            self._cleanup_scheduled_orders_for_tab(key, reason="标签页已删除")
        if removed:
            self._renumber_tabs()
            self._save_config()
            self._refresh_tab_display()

    def _on_cleanup_empty_tabs(self):
        """F4: 点击"删除空标签页"按钮。"""
        if not self._has_empty_tabs_except_current():
            return
        self._cleanup_empty_tabs()

    def _refresh_tab_display(self):
        """刷新标签页数字显示和按钮状态。"""
        tab = self._config.tabs.get(self._current_tab)
        is_frozen = tab.frozen if tab else False

        # 锁定状态下标签显示为 🔒N
        if is_frozen:
            self._tab_label.setText(f"🔒{self._current_tab}")
            self._tab_label.setFixedWidth(52)  # 锁图标需要更宽
        else:
            self._tab_label.setText(self._current_tab)
            self._tab_label.setFixedWidth(36)

        if hasattr(self, '_tab_scope_label') and self._tab_scope_label:
            prefix = "🔒 " if is_frozen else "📑 "
            self._tab_scope_label.setText(f"{prefix}标签页 {self._current_tab}")

        tab_keys = self._sorted_tab_keys(self._config.tabs)
        try:
            idx = tab_keys.index(self._current_tab)
        except ValueError:
            idx = 0

        # 更新按钮启用状态
        self._tab_btn_minus.setEnabled(idx > 0)
        # F4: 存在空标签页（不含当前）时启用按钮
        if hasattr(self, '_tab_btn_cleanup_empty') and self._tab_btn_cleanup_empty:
            self._tab_btn_cleanup_empty.setEnabled(self._has_empty_tabs_except_current())

        # 冻结时禁用打印和清空按钮（_setup_file_table 阶段按钮尚未创建）
        if hasattr(self, '_btn_start'):
            self._btn_start.setEnabled(not is_frozen)
        if hasattr(self, '_btn_clear'):
            self._btn_clear.setEnabled(not is_frozen)

        # 同步当前标签页的附加服务设置到 UI 控件
        self._sync_tab_settings_to_ui(tab)

        if hasattr(self, '_order_number_label') and self._order_number_label:
            tb_jobs = tab.jobs if tab else []
            order_num = ""
            for j in tb_jobs:
                if j.order_number:
                    order_num = j.order_number
                    break
            self._order_number_label.setText(f"📋 {order_num}" if order_num else "📋 未分配订单号")

    def _get_current_jobs(self) -> list[PrintJob]:
        """返回当前标签页的任务列表。"""
        tab = self._config.tabs.get(self._current_tab)
        return tab.jobs if tab else []

    def _is_current_tab_frozen(self) -> bool:
        """检查当前标签页是否已固定（打印后锁定，不可编辑）。"""
        tab = self._config.tabs.get(self._current_tab)
        return tab.frozen if tab else False

    def _set_current_jobs(self, jobs: list[PrintJob]):
        """设置当前标签页的任务列表并保存配置。"""
        if self._current_tab in self._config.tabs:
            self._config.tabs[self._current_tab].jobs = jobs
        self._save_config()

    def _get_tab(self, key: str):
        """获取标签页设置对象。"""
        return self._config.tabs.get(key)

    def _save_config(self):
        """实时保存配置到 JSON 文件。"""
        try:
            self._sync_ui_to_config()
            self._config.save(self._config_path)
        except Exception as e:
            logger.warning(f"自动保存配置失败: {e}")

    def _cleanup_orphan_pdf_cache(self, abandoned_jobs: list):
        """放弃订单后清理不再被任何活跃任务引用的 PDF 缓存。
        遍历 abandoned_jobs 中每个 job 的 source_md5，
        若其他标签页中无任务引用同一 MD5，则从 pdf_cache 中删除。"""
        if not self._cloud_client or not abandoned_jobs:
            return
        abandoned_md5s = {j.source_md5 for j in abandoned_jobs if j.source_md5}
        if not abandoned_md5s:
            return
        # 收集所有仍在活跃标签页中引用的 MD5
        active_md5s = set()
        for tab_entry in self._config.tabs.values():
            for job in (tab_entry.jobs if tab_entry.jobs else []):
                if job.source_md5:
                    active_md5s.add(job.source_md5)
        for md5 in abandoned_md5s:
            if md5 not in active_md5s:
                self._cloud_client.remove_cached_pdf(md5)

    # ──────── 标签页管理窗口 ────────

    def _show_tab_manager(self):
        """弹出标签页管理窗口，可查看各标签页信息、新建和删除标签页。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("📑 标签页管理")
        dlg.setMinimumWidth(500)
        dlg.setMinimumHeight(350)
        dlg.setModal(True)

        layout = QVBoxLayout(dlg)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 14, 16, 14)

        # 说明
        layout.addWidget(QLabel("<b>所有标签页</b> · 点击数字可切换，选择后可删除"))

        # 表格
        from PySide6.QtWidgets import QTableWidget as _QTW, QTableWidgetItem as _QTWI
        table = _QTW()
        table.setColumnCount(6)
        table.setHorizontalHeaderLabels(["标签", "文件数", "总页数", "合计费用", "来源", "订单号"])
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        hh = table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeToContents)

        tab_keys = []

        def _rebuild_dialog_table():
            """重建对话框内的标签页表格。"""
            nonlocal tab_keys
            tab_keys = self._sorted_tab_keys(self._config.tabs)
            table.setRowCount(0)
            for key in tab_keys:
                tb = self._config.tabs.get(key)
                jobs = tb.jobs if tb else []
                file_count = len(jobs)
                total_pages = sum(j.page_count * j.copies for j in jobs)
                total_cost = sum(calc_cost(j.page_count, j.copies, j.duplex,
                                           self._config.simplex_price, self._config.duplex_price,
                                           j.page_range)[0] for j in jobs)
                has_cloud = any(j.task_id > 0 for j in jobs)
                source = "☁ 云端" if has_cloud else ("📂 本地" if file_count > 0 else "空")
                # 取第一个非空订单号（云端任务有，本地任务在复制时生成）
                order_num = ""
                for j in jobs:
                    if j.order_number:
                        order_num = j.order_number
                        break
                if file_count > 0 and not order_num:
                    order_num = "未分配"
                elif file_count == 0:
                    order_num = "--"

                row = table.rowCount()
                table.insertRow(row)
                marker = ""
                if key == self._current_tab:
                    marker += " ★"
                if tb and tb.frozen:
                    marker += " 🔒"
                table.setItem(row, 0, _QTWI(f"标签 {key}{marker}"))
                table.setItem(row, 1, _QTWI(str(file_count)))
                table.setItem(row, 2, _QTWI(str(total_pages)))
                table.setItem(row, 3, _QTWI(f"¥{total_cost:.2f}"))
                table.setItem(row, 4, _QTWI(source))
                table.setItem(row, 5, _QTWI(order_num))

            # 更新"删除所有已完成订单"按钮状态
            try:
                has_frozen = any(t.frozen for t in self._config.tabs.values())
                del_frozen_btn.setEnabled(has_frozen)
            except NameError:
                pass  # 首次调用时按钮尚未创建

        _rebuild_dialog_table()

        # 双击切换标签页，右键删除
        table.setContextMenuPolicy(Qt.CustomContextMenu)

        def _on_double_click(row, col):
            if 0 <= row < len(tab_keys):
                key = tab_keys[row]
                if key != self._current_tab:
                    self._cancel_undo_if_active()
                    self._current_tab = key
                    self._config.active_tab = key
                    self._save_config()
                    self._rebuild_table()
                    self._refresh_tab_display()
                    self._sync_edit_enabled(False)
                dlg.accept()  # 切换后关闭标签页管理器
        table.cellDoubleClicked.connect(_on_double_click)

        def _on_context_menu(pos):
            item = table.itemAt(pos)
            if item is None:
                return
            row = item.row()
            table.selectRow(row)
            menu = QMenu(dlg)
            del_action = menu.addAction(f"🗑 删除标签页 {tab_keys[row]}")
            del_action.triggered.connect(lambda: _delete_one_tab(tab_keys[row]))
            menu.exec(table.viewport().mapToGlobal(pos))
        table.customContextMenuRequested.connect(_on_context_menu)

        layout.addWidget(table, 1)

        # 按钮行
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        new_btn = QPushButton("＋ 新建标签页")
        new_btn.clicked.connect(lambda: (
            self._switch_tab(999),
            _rebuild_dialog_table(),
        ))
        btn_row.addWidget(new_btn)

        def _delete_one_tab(key):
            """删除单个标签页"""
            if len(tab_keys) <= 1:
                QMessageBox.warning(dlg, "无法删除", "至少保留一个标签页。")
                return
            tab_entry = self._config.tabs.get(key)
            jobs = tab_entry.jobs if tab_entry else []
            if jobs:
                reply = QMessageBox.question(
                    dlg, "确认删除",
                    f"标签页 {key} 中有 {len(jobs)} 个文件，删除后无法恢复。\n确定删除吗？",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return
            # 通知后端放弃该标签页中未打印的云端任务 + 本地预留订单（已完成的跳过）
            for job in jobs:
                if getattr(job, 'sent', False):
                    continue
                if job.order_id > 0 and self._cloud_client:
                    self._cloud_client.abandon_order_to_server(job.order_id)
                elif job.task_id > 0 and self._cloud_client:
                    self._cloud_client.abandon_order_to_server(job.task_id)
                elif job.order_number and "-L" not in job.order_number and self._cloud_client:
                    price, _ = calc_cost(job.page_count, job.copies, job.duplex, page_range=job.page_range or "")
                    self._cloud_client.abandon_reserved_order(job.order_number, price)
            del self._config.tabs[key]
            self._cleanup_orphan_pdf_cache(jobs)
            self._renumber_tabs()
            self._save_config()
            self._rebuild_table()
            self._refresh_tab_display()
            self._sync_edit_enabled(False)
            self._log(f"已删除标签页 {key}")
            _rebuild_dialog_table()

        def _delete_all_tabs():
            """删除全部标签页并重置为仅标签页 1"""
            total = sum(len(t.jobs) for t in self._config.tabs.values())
            if total > 0:
                reply = QMessageBox.question(
                    dlg, "确认清空全部",
                    f"将删除全部 {len(self._config.tabs)} 个标签页（共 {total} 个文件），"
                    "重置为单个空标签页。\n\n确定清空吗？",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return
            # 通知后端放弃所有未打印的云端任务 + 本地预留订单（已完成的跳过）
            all_abandoned = []
            for tab_entry in self._config.tabs.values():
                for job in (tab_entry.jobs if tab_entry else []):
                    if getattr(job, 'sent', False):
                        continue
                    all_abandoned.append(job)
                    if job.order_id > 0 and self._cloud_client:
                        self._cloud_client.abandon_order_to_server(job.order_id)
                    elif job.task_id > 0 and self._cloud_client:
                        self._cloud_client.abandon_order_to_server(job.task_id)
                    elif job.order_number and "-L" not in job.order_number and self._cloud_client:
                        price, _ = calc_cost(job.page_count, job.copies, job.duplex, page_range=job.page_range or "")
                        self._cloud_client.abandon_reserved_order(job.order_number, price)
            self._config.tabs = {"1": TabSettings()}
            self._current_tab = "1"
            self._config.active_tab = "1"
            self._cleanup_orphan_pdf_cache(all_abandoned)
            self._save_config()
            self._rebuild_table()
            self._refresh_tab_display()
            self._sync_edit_enabled(False)
            self._log("已清空全部标签页")
            _rebuild_dialog_table()

        def _cleanup_empty_in_dialog():
            """在标签页管理器中删除空标签页。"""
            removed = []
            for key in self._sorted_tab_keys(self._config.tabs):
                if key == self._current_tab:
                    continue
                tab_entry = self._config.tabs.get(key)
                if tab_entry and len(tab_entry.jobs) == 0:
                    removed.append(key)
            if not removed:
                return
            for key in removed:
                del self._config.tabs[key]
                self._log(f"🗑 已删除空标签页 {key}")
            self._renumber_tabs()
            self._save_config()
            self._rebuild_table()
            self._refresh_tab_display()
            self._sync_edit_enabled(False)
            _rebuild_dialog_table()

        def _delete_all_frozen_tabs():
            """删除所有已完成（已固定）的标签页并重新编号。"""
            frozen_keys = [k for k, t in self._config.tabs.items() if t.frozen]
            if not frozen_keys:
                QMessageBox.information(dlg, "提示", "没有已完成的订单。")
                return
            reply = QMessageBox.question(
                dlg, "确认删除",
                f"将删除 {len(frozen_keys)} 个已完成订单的标签页，删除后无法恢复。\n确定删除吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            for key in frozen_keys:
                del self._config.tabs[key]
                self._log(f"🗑 已删除已完成标签页 {key}")
            self._renumber_tabs()
            self._save_config()
            self._rebuild_table()
            self._refresh_tab_display()
            self._sync_edit_enabled(False)
            _rebuild_dialog_table()

        cleanup_empty_btn = QPushButton("🗑 删除空标签页")
        cleanup_empty_btn.clicked.connect(_cleanup_empty_in_dialog)
        btn_row.addWidget(cleanup_empty_btn)

        del_frozen_btn = QPushButton("🗑 删除所有已完成订单")
        del_frozen_btn.setObjectName("cloudRejectBtn")
        del_frozen_btn.clicked.connect(_delete_all_frozen_tabs)
        btn_row.addWidget(del_frozen_btn)

        del_all_btn = QPushButton("✕ 清空全部标签页")
        del_all_btn.setObjectName("cloudRejectBtn")
        del_all_btn.clicked.connect(_delete_all_tabs)
        btn_row.addWidget(del_all_btn)

        btn_row.addStretch()

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)
        dlg.exec()

    # ──────── 表格重建与行操作 ────────

    def _rebuild_table(self):
        """用当前标签页的 jobs 重建表格。"""
        self._table.setRowCount(0)
        for job in self._get_current_jobs():
            self._add_table_row(job)
        self._update_total_cost()

    def _add_table_row(self, job: PrintJob):
        """添加一行到表格。"""
        row = self._table.rowCount()
        self._table.insertRow(row)

        ext = os.path.splitext(job.file_path)[1].lower()
        image_exts = self.IMAGE_EXTS
        is_image = ext in image_exts

        display = job.display_name or os.path.basename(job.file_path)
        name_item = QTableWidgetItem(display)
        name_item.setData(Qt.UserRole, job.file_path)  # 存储完整路径
        name_item.setToolTip(job.file_path)
        self._table.setItem(row, self.COL_FILE, name_item)

        copies_item = QTableWidgetItem(str(job.copies))
        copies_item.setTextAlignment(Qt.AlignCenter)
        self._table.setItem(row, self.COL_COPIES, copies_item)

        # 图片：双面无意义
        if is_image:
            duplex_text = "—"
        elif job.duplex == "on":
            dm = "长边" if job.duplex_mode != "short-edge" else "短边"
            duplex_text = f"双面({dm})"
        else:
            duplex_text = "单面"
        duplex_item = QTableWidgetItem(duplex_text)
        duplex_item.setTextAlignment(Qt.AlignCenter)
        self._table.setItem(row, self.COL_DUPLEX, duplex_item)

        # 图片：页码范围无意义
        if is_image:
            range_text = "—"
        else:
            range_text = job.page_range or "全部"
        range_item = QTableWidgetItem(range_text)
        range_item.setTextAlignment(Qt.AlignCenter)
        self._table.setItem(row, self.COL_RANGE, range_item)

        # 页数
        pages_text = str(job.page_count) if job.page_count > 0 else "?"
        pages_item = QTableWidgetItem(pages_text)
        pages_item.setTextAlignment(Qt.AlignCenter)
        self._table.setItem(row, self.COL_PAGES, pages_item)

        # 方向
        ori_map = {"portrait": "竖", "landscape": "横", "mixed": "混"}
        ori_text = ori_map.get(job.orientation, "")
        ori_item = QTableWidgetItem(ori_text)
        ori_item.setTextAlignment(Qt.AlignCenter)
        self._table.setItem(row, self.COL_ORIENT, ori_item)

        # 打印引擎（仅 Word 文件显示；非 Word 文件显示 "—"）
        is_word_file = ext in (".doc", ".docx")
        eng_labels = {"word": "Word", "wps": "WPS", "libreoffice": "LibreOffice"}
        eng_text = eng_labels.get(job.engine, "Word") if is_word_file else "—"
        eng_item = QTableWidgetItem(eng_text)
        eng_item.setTextAlignment(Qt.AlignCenter)
        if not is_word_file:
            eng_item.setToolTip("仅 Word 文档(.doc/.docx)支持选择转换引擎")
        else:
            eng_item.setToolTip("Word: Microsoft Word | WPS: WPS Office | LibreOffice: 兜底")
        self._table.setItem(row, self.COL_ENGINE, eng_item)

        # 费用
        cost, formula = calc_cost(job.page_count, job.copies, job.duplex,
                                  self._config.simplex_price, self._config.duplex_price,
                                  job.page_range)
        if cost > 0:
            cost_text = f"{formula}=¥{cost:.2f}"
        elif job.page_count <= 0:
            cost_text = "?"
        else:
            cost_text = "¥0.00"
        cost_item = QTableWidgetItem(cost_text)
        cost_item.setTextAlignment(Qt.AlignCenter)
        cost_item.setData(Qt.UserRole, cost)
        cost_item.setToolTip(cost_text)
        self._table.setItem(row, self.COL_COST, cost_item)

    def _recalc_row_cost(self, row: int):
        """重新计算指定行的费用。"""
        name_item = self._table.item(row, self.COL_FILE)
        if not name_item:
            return
        jobs = self._get_current_jobs()
        if row >= len(jobs):
            return
        job = jobs[row]
        cost, formula = calc_cost(job.page_count, job.copies, job.duplex,
                                  self._config.simplex_price, self._config.duplex_price,
                                  job.page_range)
        if cost > 0:
            cost_text = f"{formula}=¥{cost:.2f}"
        elif job.page_count <= 0:
            cost_text = "?"
        else:
            cost_text = "¥0.00"
        cost_item = self._table.item(row, self.COL_COST)
        if cost_item:
            cost_item.setText(cost_text)
            cost_item.setData(Qt.UserRole, cost)
            cost_item.setToolTip(cost_text)
        self._update_total_cost()

    def _update_total_cost(self):
        """更新合计费用标签（含附加服务）。"""
        jobs = self._get_current_jobs()
        base_total = 0.0
        all_known = True
        for job in jobs:
            cost, _ = calc_cost(job.page_count, job.copies, job.duplex,
                                self._config.simplex_price, self._config.duplex_price,
                                job.page_range)
            base_total += cost
            if job.page_count <= 0:
                all_known = False
        tab = self._config.tabs.get(self._current_tab)
        extra = tab.calc_extra_total(base_total, self._config) if tab else 0.0
        total = base_total + extra
        prefix = "≈ " if not all_known else ""
        self._total_label.setText(f"合计: {prefix}¥{total:.2f}")
        # 标签页无文件时不支持复制价格/明细（复制冷却期内保持禁用）
        if not jobs:
            if self._copy_total_btn:
                self._copy_total_btn.setEnabled(False)
            if self._copy_detail_btn:
                self._copy_detail_btn.setEnabled(False)
        elif not (self._copy_total_timer and self._copy_total_timer.isActive()):
            if self._copy_total_btn:
                self._copy_total_btn.setEnabled(True)
            if self._copy_detail_btn:
                self._copy_detail_btn.setEnabled(True)

    def _on_table_double_click(self, row: int, col: int):
        """双击表格行 → 用默认程序打开文件。"""
        name_item = self._table.item(row, self.COL_FILE)
        if name_item:
            file_path = name_item.data(Qt.UserRole)
            if file_path and os.path.isfile(file_path):
                os.startfile(file_path)

    def _on_table_selection(self):
        """选中行变化 → 同步编辑面板。"""
        rows = set(idx.row() for idx in self._table.selectedIndexes())
        if not rows:
            self._sync_edit_enabled(False)
            return
        # 标签页已固定时不允许编辑
        if self._is_current_tab_frozen():
            self._sync_edit_enabled(False)
            return
        row = min(rows)
        jobs = self._get_current_jobs()
        if row >= len(jobs):
            self._sync_edit_enabled(False)
            return
        job = jobs[row]
        self._sync_edit_enabled(True)

        # 文件类型/页数判定（供下方同步与禁用逻辑共用）
        ext = os.path.splitext(job.file_path)[1].lower() if job.file_path else ""
        is_img = ext in self.IMAGE_EXTS
        is_word = ext in (".doc", ".docx")
        # 单页判定：整份 1 页 或 页码范围有效页数恰好 1 页 → 双面打印物理上即单面输出
        single_page = _count_pages_in_range(job.page_range or "", job.page_count or 0) == 1
        duplex_usable = (not is_img) and (not single_page)

        # 同步编辑控件
        self._edit_copies.blockSignals(True)
        self._edit_copies.setValue(job.copies)
        self._edit_copies.blockSignals(False)

        self._edit_duplex.blockSignals(True)
        self._edit_duplex.setCurrentIndex(0 if job.duplex == "on" else 1)
        self._edit_duplex.blockSignals(False)

        self._edit_duplex_mode.blockSignals(True)
        self._edit_duplex_mode.setCurrentIndex(0 if job.duplex_mode != "short-edge" else 1)
        self._edit_duplex_mode.blockSignals(False)

        self._edit_page_range.set_total_pages(job.page_count)
        self._edit_page_range.set_ranges(job.page_range)

        # PDF转换引擎：Word 显示可选引擎；其余格式显示实际转换引擎（灰色不可选）
        if is_word:
            eng_map = {"word": 0, "wps": 1, "libreoffice": 2}
            self._edit_engine.blockSignals(True)
            self._edit_engine.clear()
            self._edit_engine.addItems(["Word", "WPS", "LibreOffice"])
            self._edit_engine.setCurrentIndex(eng_map.get(job.engine, 0))
            self._edit_engine.blockSignals(False)
        else:
            self._edit_engine.blockSignals(True)
            self._edit_engine.clear()
            self._edit_engine.addItems([_format_engine_label(ext)])
            self._edit_engine.setCurrentIndex(0)
            self._edit_engine.blockSignals(False)

        dpi_map = {0: 0, 200: 1, 300: 2, 400: 3, 600: 4}
        self._edit_dpi.blockSignals(True)
        self._edit_dpi.setCurrentIndex(dpi_map.get(job.dpi, 0))
        self._edit_dpi.blockSignals(False)

        # 按文件类型/页数控制参数可用性：
        # 双面/双面模式 → 仅非图片且非单页可用（图片单页、无双面概念；单页文件无法双面）；
        # 页码范围 → 仅非图片可用；
        # PDF转换引擎 → 仅 Word(doc/docx) 可用（其余格式走各自转换路径或直接打印，引擎选择无意义）；
        # 图片方向 → 仅图片可用
        duplex_usable = (not is_img) and (not single_page)
        self._label_duplex.setEnabled(duplex_usable)
        self._edit_duplex.setEnabled(duplex_usable)
        self._label_duplex_mode.setEnabled(duplex_usable)
        self._edit_duplex_mode.setEnabled(duplex_usable)
        self._label_range.setEnabled(not is_img)
        self._edit_page_range.setEnabled(not is_img)
        self._label_engine.setEnabled(is_word)
        self._edit_engine.setEnabled(is_word)
        if not duplex_usable:
            self._edit_duplex.blockSignals(True)
            self._edit_duplex.setCurrentIndex(1)  # 单面
            self._edit_duplex.blockSignals(False)
        if is_img:
            # 图片页码无意义 → 清空范围输入
            self._edit_page_range.blockSignals(True)
            self._edit_page_range.set_ranges("")
            self._edit_page_range.blockSignals(False)

        # 图片方向（仅图片可用）：auto=自动 / landscape=横向 / portrait=竖向
        img_ori_map = {"auto": 0, "landscape": 1, "portrait": 2}
        self._edit_img_orientation.blockSignals(True)
        self._edit_img_orientation.setCurrentIndex(img_ori_map.get(getattr(job, 'image_orientation', 'auto'), 0))
        self._edit_img_orientation.blockSignals(False)
        self._edit_img_orientation.setEnabled(is_img)
        self._label_img_orientation.setEnabled(is_img)

        # 更新选中文件标签（云端任务显示原始文件名；路径软换行避免长临时路径撑宽面板）
        shown_name = job.display_name or os.path.basename(job.file_path)
        self._selected_file_label.setText(
            f"📄 {_soft_wrap_text(shown_name)}\n"
            f"路径: {_soft_wrap_text(job.file_path)}"
        )

    def _sync_edit_enabled(self, enabled: bool):
        """统一启用/禁用编辑面板所有控件。"""
        if not hasattr(self, '_edit_widgets') or not self._edit_widgets:
            return
        for w in self._edit_widgets:
            w.setEnabled(enabled)
        if not enabled:
            self._selected_file_label.setText("(未选中任务)")

    def _auto_apply_edit(self):
        """编辑面板参数变更 → 自动应用到当前选中行并保存。"""
        if self._is_current_tab_frozen():
            return
        rows = set(idx.row() for idx in self._table.selectedIndexes())
        if not rows:
            return
        row = min(rows)
        jobs = self._get_current_jobs()
        if row >= len(jobs):
            return

        job = jobs[row]

        # 读取编辑控件值
        job.copies = self._edit_copies.value()
        # 图片/单页文件：双面无意义，固定单面（避免被禁用的双面控件残留值误写）
        # 单页判定含范围选 1 页（多页文件手动选 1 页 → 双面物理上即单面输出）
        is_img = bool(job.file_path) and os.path.splitext(job.file_path)[1].lower() in self.IMAGE_EXTS
        single_page = _count_pages_in_range(job.page_range or "", job.page_count or 0) == 1
        if is_img or single_page:
            job.duplex = "off"
        else:
            job.duplex = "on" if self._edit_duplex.currentIndex() == 0 else "off"
            job.duplex_mode = "short-edge" if self._edit_duplex_mode.currentIndex() == 1 else "long-edge"

        # 图片方向（仅图片可用）：方向变化后清掉已转换的缓存 PDF，打印时按新方向重渲染
        if is_img:
            new_ori = {0: "auto", 1: "landscape", 2: "portrait"}.get(
                self._edit_img_orientation.currentIndex(), "auto")
            if new_ori != getattr(job, 'image_orientation', 'auto'):
                job.image_orientation = new_ori
                job.cached_pdf = ""   # 旧方向 PDF 失效

        # 页码范围（RangeListWidget 仅校验通过才发 rangesChanged；
        # 此处再校验一次，非法输入时不写入，保留原值，避免被其他控件联动覆盖）
        if self._edit_page_range.is_valid():
            ranges_str = ",".join(
                inp.text().strip() for inp in self._edit_page_range._inputs
                if inp.text().strip()
            )
            job.page_range = ranges_str

        # 引擎（仅 Word 文件有意义；其余格式引擎显示为转换方式，不写回 job.engine）
        is_word = os.path.splitext(job.file_path)[1].lower() in (".doc", ".docx") if job.file_path else False
        if is_word:
            eng_map = {0: "word", 1: "wps", 2: "libreoffice"}
            job.engine = eng_map.get(self._edit_engine.currentIndex(), "word")

        # DPI
        dpi_map = {0: 0, 1: 200, 2: 300, 3: 400, 4: 600}
        job.dpi = dpi_map.get(self._edit_dpi.currentIndex(), 0)

        # 更新表格行显示
        self._recalc_row_cost(row)
        # 实时保存配置
        self._set_current_jobs(jobs)

    def _on_table_context_menu(self, pos):
        """表格右键菜单。"""
        menu = QMenu(self)
        # 检查是否点击在有效行上
        item = self._table.itemAt(pos)
        if item is not None:
            row = item.row()
            # 选中该行
            self._table.selectRow(row)
            # 移除选中
            remove_action = menu.addAction("🗑 移除选中")
            remove_action.triggered.connect(self._on_remove_selected)
            menu.addSeparator()
            # 打开文件位置
            name_item = self._table.item(row, self.COL_FILE)
            if name_item:
                fp = name_item.data(Qt.UserRole)
                open_action = menu.addAction("📂 打开文件位置")
                open_action.triggered.connect(lambda checked=False, p=fp: (
                    os.startfile(os.path.dirname(p)) if p and os.path.isfile(p) else None
                ))
                menu.addSeparator()

        # 粘贴
        paste_action = menu.addAction("📋 粘贴")
        paste_action.setEnabled(self._can_paste_files())
        paste_action.triggered.connect(self._on_paste_files)

        menu.exec(self._table.viewport().mapToGlobal(pos))


    def _setup_edit_panel(self) -> QWidget:
        """右侧：选中任务的参数编辑面板。"""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 0, 0, 0)

        title = QLabel("⚙ 任务参数")
        title.setObjectName("sectionLabel")
        layout.addWidget(title)

        # ---- 可滚动区域：空间不足时上下滑动，不压缩内容 ----
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        _enable_smooth_scroll(scroll)

        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(8)

        gb = QGroupBox("编辑选中任务")
        gl = QVBoxLayout(gb)
        gl.setSpacing(10)

        # 份数
        label_copies = QLabel("份数:")
        gl.addWidget(label_copies)
        self._edit_copies = CounterWidget(1, 99)
        self._edit_copies.valueChanged.connect(self._auto_apply_edit)
        gl.addWidget(self._edit_copies)

        # 双面
        self._label_duplex = QLabel("单/双面:")
        gl.addWidget(self._label_duplex)
        self._edit_duplex = QComboBox()
        self._edit_duplex.addItems(["双面打印", "单面打印"])
        _disable_combo_wheel(self._edit_duplex)
        self._edit_duplex.currentIndexChanged.connect(self._auto_apply_edit)
        gl.addWidget(self._edit_duplex)

        # 双面模式（仅双面时可用）
        self._label_duplex_mode = QLabel("双面模式:")
        gl.addWidget(self._label_duplex_mode)
        self._edit_duplex_mode = QComboBox()
        self._edit_duplex_mode.addItems(["长边翻转", "短边翻转"])
        _disable_combo_wheel(self._edit_duplex_mode)
        self._edit_duplex_mode.currentIndexChanged.connect(self._auto_apply_edit)
        gl.addWidget(self._edit_duplex_mode)

        # 页码范围
        self._label_range = QLabel("页码范围:")
        gl.addWidget(self._label_range)
        self._edit_page_range = RangeListWidget()
        self._edit_page_range.rangesChanged.connect(self._auto_apply_edit)
        gl.addWidget(self._edit_page_range)

        # 打印引擎
        self._label_engine = QLabel("PDF转换引擎:")
        gl.addWidget(self._label_engine)
        self._edit_engine = QComboBox()
        self._edit_engine.addItems(["Word", "WPS", "LibreOffice"])
        _disable_combo_wheel(self._edit_engine)
        self._edit_engine.currentIndexChanged.connect(self._auto_apply_edit)
        gl.addWidget(self._edit_engine)
        # 灰度不可用引擎（延迟到窗口显示后，避免阻塞启动）
        QTimer.singleShot(0, self._refresh_engine_availability)

        # 渲染质量（逐文件）
        label_dpi = QLabel("DPI:")
        gl.addWidget(label_dpi)
        self._edit_dpi = QComboBox()
        self._edit_dpi.addItems(["跟随全局(默认)", "高速(200)", "标清(300)", "清晰(400)", "高清(600)"])
        _disable_combo_wheel(self._edit_dpi)
        self._edit_dpi.currentIndexChanged.connect(self._auto_apply_edit)
        gl.addWidget(self._edit_dpi)

        # 图片方向（仅图片文件可用）：auto=自动 / landscape=横向 / portrait=竖向
        self._label_img_orientation = QLabel("图片方向:")
        gl.addWidget(self._label_img_orientation)
        self._edit_img_orientation = QComboBox()
        self._edit_img_orientation.addItems(["自动方向", "横向", "竖向"])
        _disable_combo_wheel(self._edit_img_orientation)
        self._edit_img_orientation.currentIndexChanged.connect(self._auto_apply_edit)
        gl.addWidget(self._edit_img_orientation)

        gl.addStretch()

        # 统一管理：无选中任务时全部禁用
        self._edit_widgets = [
            label_copies, self._edit_copies,
            self._label_duplex, self._edit_duplex,
            self._label_duplex_mode, self._edit_duplex_mode,
            self._label_range, self._edit_page_range,
            self._label_engine, self._edit_engine,
            label_dpi, self._edit_dpi,
            self._label_img_orientation, self._edit_img_orientation,
        ]
        for w in self._edit_widgets:
            w.setEnabled(False)

        scroll_layout.addWidget(gb)

        # 当前选中文件信息
        self._selected_file_label = QLabel("(未选中任务)")
        self._selected_file_label.setObjectName("selectedFileLabel")
        self._selected_file_label.setWordWrap(True)
        # 水平尺寸策略 Ignored：不因内容（长路径）撑宽右侧面板，宽度由布局/视口决定
        self._selected_file_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        scroll_layout.addWidget(self._selected_file_label)

        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll, 1)

        return container

    def _setup_button_bar(self) -> QHBoxLayout:
        """底部按钮栏。"""
        layout = QHBoxLayout()

        self._btn_clear = QPushButton("✖ 清空列表")
        self._btn_clear.clicked.connect(self._on_clear_list)
        layout.addWidget(self._btn_clear)

        # 撤回定时器：清空后 5 秒内可撤回（每个标签页独立备份）
        self._clear_undo_timer = QTimer(self)
        self._clear_undo_timer.setSingleShot(True)
        self._clear_undo_timer.timeout.connect(self._on_undo_expired)

        layout.addStretch()

        self._btn_start = QPushButton("▶ 开始打印")
        self._btn_start.setObjectName("btnStartPrint")
        self._btn_start.clicked.connect(self._on_start_print)
        layout.addWidget(self._btn_start)

        return layout

    # ---- 数据 → UI ----

    def _load_config_to_ui(self):
        """将配置数据同步到 UI 控件。"""
        # 先加载打印机列表，再设置当前选中项
        self._refresh_printer_list()
        self._printer_combo.setCurrentText(self._config.printer_name)

        self._keep_temp_check.setCurrentIndex(1 if self._config.keep_temp_pdf else 0)

        # 全局渲染 DPI
        dpi_map = {200: 0, 300: 1, 400: 2, 600: 3}
        self._render_dpi_combo.setCurrentIndex(dpi_map.get(self._config.render_dpi, 2))

        # 附加服务
        self._delivery_onoff_combo.blockSignals(True)
        self._delivery_onoff_combo.setCurrentIndex(1 if self._config.delivery_enabled else 0)
        self._delivery_onoff_combo.blockSignals(False)

        # 刷新派送地点列表
        self._delivery_location_combo.blockSignals(True)
        self._delivery_location_combo.clear()
        self._delivery_location_combo.addItems(list(self._config.delivery_percentages.keys()))
        idx = self._delivery_location_combo.findText(self._config.delivery_location)
        if idx >= 0:
            self._delivery_location_combo.setCurrentIndex(idx)
        self._delivery_location_combo.blockSignals(False)

        self._delivery_percent_spin.blockSignals(True)
        self._delivery_percent_spin.setValue(
            self._config.delivery_percentages.get(self._config.delivery_location, 0.0))
        self._delivery_percent_spin.blockSignals(False)

        delivery_on = self._config.delivery_enabled
        self._delivery_location_combo.setEnabled(delivery_on)
        self._delivery_percent_spin.setEnabled(delivery_on)

        self._urgency_combo.blockSignals(True)
        self._urgency_combo.setCurrentText(self._config.urgency)
        self._urgency_combo.blockSignals(False)

        self._urgency_price_spin.blockSignals(True)
        urgency_price = 0.0 if self._config.urgency == "低" else self._config.urgency_prices.get(self._config.urgency, 0.0)
        self._urgency_price_spin.setValue(urgency_price)
        self._urgency_price_spin.setEnabled(self._config.urgency != "低")
        self._urgency_price_spin.blockSignals(False)

        self._cover_page_onoff_combo.blockSignals(True)
        self._cover_page_onoff_combo.setCurrentIndex(1 if self._config.cover_page else 0)
        self._cover_page_onoff_combo.blockSignals(False)

        self._cover_page_price_spin.blockSignals(True)
        self._cover_page_price_spin.setValue(self._config.cover_page_price)
        self._cover_page_price_spin.setEnabled(self._config.cover_page)
        self._cover_page_price_spin.blockSignals(False)

        self._rebuild_table()
        # 刷新订单号显示（初始加载时 _refresh_tab_display 中 hasattr 跳过了）
        if hasattr(self, '_order_number_label') and self._order_number_label:
            tb = self._config.tabs.get(self._current_tab)
            jobs = tb.jobs if tb else []
            order_num = ""
            for j in jobs:
                if j.order_number:
                    order_num = j.order_number
                    break
            self._order_number_label.setText(f"📋 {order_num}" if order_num else "📋 未分配订单号")

        # 初始加载后，用当前标签页的附加服务覆盖全局默认值
        tab = self._config.tabs.get(self._current_tab)
        if tab:
            self._sync_tab_settings_to_ui(tab)

        # v4.2：成员名单初始化 —— 本地模式立即从本地收支数据读取（云端模式等连接后由 _conn_sync_worker 刷新）
        if not self._config.cloud_enabled:
            self._refresh_owner_names_from_local()
            if hasattr(self, '_owner_combo') and self._owner_combo:
                self._update_owner_combo_items()

    def _refresh_printer_list(self):
        """刷新下拉列表中的系统打印机。"""
        current = self._printer_combo.currentText().strip()
        self._printer_combo.clear()
        # 空选项 = 系统默认打印机
        self._printer_combo.addItem("（系统默认打印机）", "")
        printers = list_system_printers()
        for name in printers:
            self._printer_combo.addItem(name, name)
        # 如果当前有选中的打印机，尝试恢复
        if current:
            idx = self._printer_combo.findText(current)
            if idx >= 0:
                self._printer_combo.setCurrentIndex(idx)
            elif not self._printer_missing_warned:
                # 原打印机已不存在/改名 → 静默回退默认，仅提示一次（避免每次刷新都弹窗）
                self._printer_missing_warned = True
                self._log(f"⚠ 打印机「{current}」已不存在或改名，已回退到系统默认打印机")
                QMessageBox.warning(
                    self, "打印机不可用",
                    f"打印机「{current}」已不存在或改名。\n已自动切换为系统默认打印机。",
                )

    def _on_refresh_printers(self):
        """刷新打印机列表按钮回调。"""
        self._refresh_printer_list()
        self._log("已刷新打印机列表")

    def _on_price_changed(self):
        """单价/附加服务变更 → 重算当前标签页所有费用并保存。"""
        self._config.simplex_price = self._simplex_price_spin.value()
        self._config.duplex_price = self._duplex_price_spin.value()
        # 首页价格写回当前标签页
        tab = self._config.tabs.get(self._current_tab)
        if tab and hasattr(self, '_cover_page_price_spin'):
            tab.cover_page_price = self._cover_page_price_spin.value()
        for row in range(self._table.rowCount()):
            self._recalc_row_cost(row)
        self._update_total_cost()
        self._save_config()

    def _on_keep_temp_changed(self):
        """保存转换副本设置变更 → 实时同步到 config。"""
        self._config.keep_temp_pdf = (self._keep_temp_check.currentIndex() == 1)

    def _on_render_dpi_changed(self):
        """全局渲染 DPI 变更。"""
        dpi_values = [200, 300, 400, 600]
        idx = self._render_dpi_combo.currentIndex()
        self._config.render_dpi = dpi_values[idx] if 0 <= idx < len(dpi_values) else 400

    # ---- 附加服务信号处理（每标签页独立）----

    def _sync_tab_settings_to_ui(self, tab):
        """将标签页的附加服务设置同步到 UI 控件。"""
        if not tab or not hasattr(self, '_delivery_onoff_combo'):
            return
        # 冻结时禁用所有附加服务控件
        if tab.frozen:
            self._delivery_onoff_combo.setEnabled(False)
            self._delivery_location_combo.setEnabled(False)
            self._delivery_percent_spin.setEnabled(False)
            self._urgency_combo.setEnabled(False)
            self._urgency_price_spin.setEnabled(False)
            self._cover_page_onoff_combo.setEnabled(False)
            self._cover_page_price_spin.setEnabled(False)
            if hasattr(self, '_owner_combo') and self._owner_combo:
                self._owner_combo.setEnabled(False)
            if hasattr(self, '_admin_print_check') and self._admin_print_check:
                self._admin_print_check.setEnabled(False)
            return
        self._delivery_onoff_combo.setEnabled(True)
        self._delivery_onoff_combo.blockSignals(True)
        self._delivery_onoff_combo.setCurrentIndex(1 if tab.delivery_enabled else 0)
        self._delivery_onoff_combo.blockSignals(False)
        self._delivery_location_combo.setEnabled(tab.delivery_enabled)
        self._delivery_percent_spin.setEnabled(tab.delivery_enabled)
        if tab.delivery_location:
            idx = self._delivery_location_combo.findText(tab.delivery_location)
            if idx >= 0:
                self._delivery_location_combo.setCurrentIndex(idx)
        if tab.delivery_enabled:
            pct = self._config.delivery_percentages.get(tab.delivery_location, 0.0)
            self._delivery_percent_spin.blockSignals(True)
            self._delivery_percent_spin.setValue(pct)
            self._delivery_percent_spin.blockSignals(False)
        self._urgency_combo.blockSignals(True)
        self._urgency_combo.setCurrentText(tab.urgency)
        self._urgency_combo.blockSignals(False)
        is_low = (tab.urgency == "低")
        self._urgency_price_spin.setEnabled(not is_low)
        self._urgency_price_spin.blockSignals(True)
        self._urgency_price_spin.setValue(0.0 if is_low else self._config.urgency_prices.get(tab.urgency, 0.0))
        self._urgency_price_spin.blockSignals(False)
        self._cover_page_onoff_combo.blockSignals(True)
        self._cover_page_onoff_combo.setCurrentIndex(1 if tab.cover_page else 0)
        self._cover_page_onoff_combo.blockSignals(False)
        self._cover_page_price_spin.setEnabled(tab.cover_page)
        self._cover_page_price_spin.blockSignals(True)
        self._cover_page_price_spin.setValue(tab.cover_page_price)
        self._cover_page_price_spin.blockSignals(False)
        # 订单归属（v24）：归属管理员下拉 + 管理员自行打印勾选
        if hasattr(self, '_owner_combo') and self._owner_combo:
            self._owner_combo.setEnabled(True)
            self._update_owner_combo_items()   # 内部同步 admin_print_check 启用态（无成员时禁用）
        if hasattr(self, '_admin_print_check') and self._admin_print_check:
            self._admin_print_check.blockSignals(True)
            self._admin_print_check.setChecked(bool(tab.is_admin_print))
            self._admin_print_check.blockSignals(False)

    def _member_names(self) -> list[str]:
        """真实成员名单（剔除旧版占位名 张三/李四/王五）。归属下拉与建单校验共用。"""
        return [n for n in (self._config.admin_names or []) if n and n not in PLACEHOLDER_OWNER_NAMES]

    def _has_members(self) -> bool:
        """是否有可用成员（无成员时禁止创建订单）。"""
        return bool(self._member_names())

    def _show_no_member_hint(self):
        """无成员提示：引导到「文件 → 收支统计 → 设置」添加成员。"""
        from PySide6.QtWidgets import QMessageBox
        msg = QMessageBox(self)
        msg.setWindowTitle("暂无成员")
        msg.setIcon(QMessageBox.Information)
        msg.setText("当前没有可用成员，无法创建订单。\n\n请先在「文件 → 收支统计 → 设置」中添加一个成员。")
        go_btn = msg.addButton("去添加成员", QMessageBox.AcceptRole)
        msg.addButton("知道了", QMessageBox.RejectRole)
        msg.exec()
        if msg.clickedButton() is go_btn:
            self._on_open_finance("settings")

    def _on_owner_activated(self, index: int):
        """归属下拉项被用户点击（含再次点击当前项）：无成员时弹添加成员引导。"""
        if self._owner_combo and self._owner_combo.itemText(index) == "(无成员)":
            self._show_no_member_hint()

    def _update_owner_combo_items(self):
        """按成员名单重建归属下拉，尽量保持当前选中。
        成员名单来源：云端模式 = 云端收支配置成员（_refresh_owner_names_from_cloud）；
        本地模式 = 本地 print_data.json 成员（_refresh_owner_names_from_local）。
        无成员时下拉仅显示「(无成员)」，且禁止创建订单（复制/开始打印会弹提示）。"""
        tab = self._config.tabs.get(self._current_tab)
        names = self._member_names()
        tab_owner = (tab.owner_name if tab else "") or ""
        if names:
            # 有成员：标签页归属为空/占位名/已不在名单 → 自动落到第一个真实成员（写回标签页，保证建单归属非空）
            if (not tab_owner) or tab_owner in PLACEHOLDER_OWNER_NAMES or tab_owner not in names:
                act_owner = names[0]
            else:
                act_owner = tab_owner
            combo_items = names
        else:
            act_owner = "(无成员)"
            combo_items = ["(无成员)"]
        if [self._owner_combo.itemText(i) for i in range(self._owner_combo.count())] != combo_items:
            self._owner_combo.blockSignals(True)
            self._owner_combo.clear()
            self._owner_combo.addItems(combo_items)
            self._owner_combo.blockSignals(False)
        if self._owner_combo.currentText() != act_owner:
            self._owner_combo.blockSignals(True)
            self._owner_combo.setCurrentText(act_owner)
            self._owner_combo.blockSignals(False)
        # 写回标签页归属（真实成员；无成员时置空）
        if tab:
            new_owner = act_owner if act_owner != "(无成员)" else ""
            if tab.owner_name != new_owner:
                tab.owner_name = new_owner
                self._save_config()
        # 无成员时「管理员自行打印」无意义，禁用
        if hasattr(self, '_admin_print_check') and self._admin_print_check:
            self._admin_print_check.setEnabled(bool(names))

    def _refresh_owner_names_from_cloud(self):
        """归属下拉弹出时同步成员名单（统一入口）：
        云端已连接 → 后台拉云端成员；否则（本地模式/云端未连）→ 后台读本地 print_data.json 成员。
        完成后经 _ownerComboRefreshed 信号回主线程刷新下拉，避免阻塞 UI。"""
        # 防抖：上下两次请求至少间隔 2s，避免下拉反复 Show 触发并发叠加
        now = time.monotonic()
        if now - self._owner_refresh_last_ts < 2.0:
            return
        self._owner_refresh_last_ts = now
        threading.Thread(
            target=self._owner_refresh_net_thread,
            daemon=True,
            name="owner-name-sync",
        ).start()

    def changeEvent(self, event):
        """窗口激活（用户从收支统计页切回）→ 重新同步成员名单。
        保证归属下拉严格跟随收支统计的成员管理：删掉成员后切回 GUI 即刷新为「(无成员)」。
        仅触发后台刷新（有防抖），不阻塞 UI。"""
        if event.type() == QEvent.WindowActivate:
            try:
                self._refresh_owner_names_from_cloud()
            except Exception:
                pass
        super().changeEvent(event)

    def _owner_refresh_net_thread(self):
        """后台线程：拉取成员名单（云端或本地）+ 合并 + 写盘 + 回主线程刷新下拉。"""
        try:
            cloud_ok = bool(
                self._cloud_client and self._cloud_client.is_connected()
                and self._cloud_client.api_url and self._cloud_client.token
            )
            if cloud_ok:
                self._refresh_owner_names_from_cloud_net()
            else:
                self._refresh_owner_names_from_local()
        except Exception as e:
            logger.debug(f"同步成员名单失败: {e}")
        finally:
            # 无论是否变化都刷新下拉（保持顺序/一致性）——必须回主线程
            self._ownerComboRefreshed.emit()

    def _refresh_owner_names_from_local(self):
        """本地模式：从本地 print_data.json 读成员名单，严格采用（清洗占位名）。
        收支统计成员管理是唯一权威来源：成员被删除后，归属下拉立即不再显示该名字
        （不保留"标签页仍引用的旧名字"，标签页归属会在 _update_owner_combo_items 中被重置）。
        无 UI 操作，可在后台线程执行；UI 刷新由调用方经 _ownerComboRefreshed 信号。"""
        try:
            data = load_local_data_file()
            names = []
            if data:
                for m in (data.get("config") or {}).get("members") or []:
                    n = str(m.get("name", "")).strip() if isinstance(m, dict) else ""
                    if n and n not in names and n not in PLACEHOLDER_OWNER_NAMES:
                        names.append(n)
            base = list(self._config.admin_names or [])
            merged = list(names)   # 严格 = 收支统计成员名单（不并入任何其他来源的名字）
            if merged != base:
                self._config.admin_names = merged
                self._save_config()
        except Exception as e:
            logger.debug(f"同步本地成员名单失败: {e}")

    def _refresh_owner_names_from_cloud_net(self):
        """云端成员名单拉取 + 写盘（无 UI 操作，可在后台线程执行）。
        收支统计成员管理是唯一权威来源，归属下拉严格按云端成员名单渲染：
        · 云端尚未保存收支配置（CEO 首次部署，data 为 null）→ 回退本地成员名单；
        · 云端配置已存在 → 严格采用云端成员（云端为空 = 无成员，本地/标签页旧名字不保留）。"""
        if not self._cloud_client or not self._cloud_client.is_connected():
            return
        if not self._cloud_client.api_url or not self._cloud_client.token:
            return
        try:
            resp = http_requests.get(
                f"{self._cloud_client.api_url}/api/finance/config",
                params={"token": self._cloud_client.token},
                timeout=4,
            )
            if not resp.ok:
                return
            data = resp.json()
            if not data.get("success"):
                return
            raw = data.get("data")
            if raw is None:
                # 云端尚未保存收支配置（CEO 首次部署、收支数据未迁移）→ 回退本地成员名单
                self._refresh_owner_names_from_local()
                return
            if isinstance(raw, dict):
                cfg = raw.get("config") or {}
            else:
                cfg = {}
            members = cfg.get("members") or []
            names = []
            for m in members:
                n = str(m.get("name", "")).strip() if isinstance(m, dict) else ""
                if n and n not in names and n not in PLACEHOLDER_OWNER_NAMES:
                    names.append(n)
            base = list(self._config.admin_names or [])
            merged = list(names)   # 严格 = 云端收支配置成员（云端为空 → 空名单，即「(无成员)」）
            if merged != base:
                self._config.admin_names = merged
                self._save_config()
        except Exception as e:
            logger.debug(f"同步云端成员名单失败: {e}")

    def _on_owner_changed(self, text: str):
        """归属成员变更 → 写入当前标签页（下拉只允许选择，选择即保存）。
        「(无成员)」仅展示，不写入标签页（写空归属）；点击引导见 _on_owner_activated。"""
        if self._is_current_tab_frozen():
            return
        if text == "(无成员)":
            return
        tab = self._config.tabs.get(self._current_tab)
        if tab:
            tab.owner_name = (text or "").strip()
        self._save_config()

    def _on_admin_print_toggled(self, checked: bool):
        """管理员自行打印勾选变更 → 写入当前标签页。"""
        if self._is_current_tab_frozen():
            return
        tab = self._config.tabs.get(self._current_tab)
        if tab:
            tab.is_admin_print = bool(checked)
        self._save_config()

    def _on_delivery_toggled(self):
        """派送开关变更 → 写入当前标签页。"""
        if self._is_current_tab_frozen():
            return
        enabled = (self._delivery_onoff_combo.currentIndex() == 1)
        tab = self._config.tabs.get(self._current_tab)
        if tab:
            tab.delivery_enabled = enabled
        self._delivery_location_combo.setEnabled(enabled)
        self._delivery_percent_spin.setEnabled(enabled)
        if enabled:
            loc = self._delivery_location_combo.currentText()
            pct = self._config.delivery_percentages.get(loc, 0.0)
            self._delivery_percent_spin.blockSignals(True)
            self._delivery_percent_spin.setValue(pct)
            self._delivery_percent_spin.blockSignals(False)
        self._save_config()
        self._on_price_changed()

    def _on_cover_page_toggled(self):
        """首页开关变更 → 写入当前标签页。"""
        if self._is_current_tab_frozen():
            return
        enabled = (self._cover_page_onoff_combo.currentIndex() == 1)
        tab = self._config.tabs.get(self._current_tab)
        if tab:
            tab.cover_page = enabled
        self._cover_page_price_spin.setEnabled(enabled)
        self._save_config()
        self._on_price_changed()

    def _on_delivery_location_changed(self):
        """派送地点变更 → 更新百分比 spinbox 并写入当前标签页。"""
        if self._is_current_tab_frozen():
            return
        loc = self._delivery_location_combo.currentText()
        tab = self._config.tabs.get(self._current_tab)
        if tab:
            tab.delivery_location = loc
        pct = self._config.delivery_percentages.get(loc, 0.0)
        self._delivery_percent_spin.blockSignals(True)
        self._delivery_percent_spin.setValue(pct)
        self._delivery_percent_spin.blockSignals(False)
        self._save_config()
        self._on_price_changed()

    def _on_delivery_percent_changed(self):
        """派送百分比编辑 → 回写全局百分比表（所有标签页共享）。"""
        loc = self._delivery_location_combo.currentText()
        self._config.delivery_percentages[loc] = self._delivery_percent_spin.value()
        self._save_config()
        self._on_price_changed()

    def _on_urgency_changed(self):
        """优先级变更 → 写入当前标签页。"低"时锁定为 0.00 并禁用。"""
        if self._is_current_tab_frozen():
            return
        level = self._urgency_combo.currentText()
        tab = self._config.tabs.get(self._current_tab)
        if tab:
            tab.urgency = level
        is_low = (level == "低")
        self._urgency_price_spin.setEnabled(not is_low)
        price = 0.0 if is_low else self._config.urgency_prices.get(level, 0.0)
        self._urgency_price_spin.blockSignals(True)
        self._urgency_price_spin.setValue(price)
        self._urgency_price_spin.blockSignals(False)
        self._save_config()
        self._on_price_changed()

    def _on_urgency_price_changed(self):
        """紧急价格编辑 → 回写全局紧急价格表。"""
        level = self._urgency_combo.currentText()
        self._config.urgency_prices[level] = self._urgency_price_spin.value()
        self._save_config()
        self._on_price_changed()

    def _sync_ui_to_config(self):
        """将 UI 控件数据同步回配置对象。"""
        printer_data = self._printer_combo.currentData()
        self._config.printer_name = printer_data if printer_data else ""

        self._config.keep_temp_pdf = (self._keep_temp_check.currentIndex() == 1)

        self._config.simplex_price = self._simplex_price_spin.value()
        self._config.duplex_price = self._duplex_price_spin.value()

        dpi_values = [200, 300, 400, 600]
        idx = self._render_dpi_combo.currentIndex()
        self._config.render_dpi = dpi_values[idx] if 0 <= idx < len(dpi_values) else 400

        self._config.last_dir = self._last_dir

        # 附加服务
        self._config.delivery_enabled = (self._delivery_onoff_combo.currentIndex() == 1)
        self._config.delivery_location = self._delivery_location_combo.currentText()
        self._config.urgency = self._urgency_combo.currentText()
        self._config.cover_page = (self._cover_page_onoff_combo.currentIndex() == 1)
        self._config.cover_page_price = self._cover_page_price_spin.value()

        # jobs 已通过表格实时维护并保存到 tabs 中

    def _find_job_row_by_file_path(self, file_path: str) -> int | None:
        """按 file_path 在当前标签页查找任务行号，找不到返回 None。"""
        jobs = self._get_current_jobs()
        for i, j in enumerate(jobs):
            if j.file_path == file_path:
                return i
        return None

    def _start_convert_worker(self, row: int, file_path: str, engine: str):
        """启动后台 PDF 转换线程。先检查 MD5 缓存，命中则跳过转换。

        MD5 一律从 file_path 计算（不再依赖 jobs[row].source_md5 ——
        多文件订单并发转换时行号可能错位，会读到别的文件的 MD5）。
        仅当任务上已有相同 file_path 的 source_md5 时复用，避免重复计算大文件。
        """
        # 计算源文件 MD5 并检查 PDF 缓存
        source_md5 = ""
        image_orientation = "auto"
        jobs = self._get_current_jobs()
        if row < len(jobs) and jobs[row].source_md5 and jobs[row].file_path == file_path:
            source_md5 = jobs[row].source_md5
        if row < len(jobs) and jobs[row].file_path == file_path:
            image_orientation = getattr(jobs[row], 'image_orientation', 'auto')
        if not source_md5 and os.path.isfile(file_path):
            try:
                if self._cloud_client:
                    source_md5 = self._cloud_client._compute_md5_file(file_path)
                else:
                    import hashlib
                    m = hashlib.md5()
                    with open(file_path, "rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            m.update(chunk)
                    source_md5 = m.hexdigest()
            except Exception:
                source_md5 = ""
        # 检查本地 PDF 缓存（图片按方向后缀分开）
        if source_md5:
            if self._cloud_client:
                cached_pdf, cached_meta = self._cloud_client._get_cached_pdf(source_md5, image_orientation)
            else:
                # 离线：直接检查 pdf_cache 目录
                cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdf_cache")
                cached_pdf = os.path.join(cache_dir, pdf_cache_key(source_md5, image_orientation) + ".pdf")
                cached_meta = {}
                if not os.path.isfile(cached_pdf):
                    cached_pdf = None
            if cached_pdf:
                from pdf_printer import get_pdf_info as _gpi
                info = _gpi(cached_pdf)
                page_count = info.get("page_count", cached_meta.get("page_count", 0))
                orientation = info.get("orientation", "")
                self._log(f"📦 缓存命中: {os.path.basename(file_path)} → {page_count} 页 (MD5={source_md5[:8]}...)")
                # 按 file_path 匹配行回写（不依赖调用方传入的 row，防多文件订单行错位写错行）
                match_row = self._find_job_row_by_file_path(file_path)
                if match_row is not None:
                    jobs = self._get_current_jobs()
                    jobs[match_row].source_md5 = source_md5
                    jobs[match_row].cached_pdf = cached_pdf
                    jobs[match_row].page_count = page_count
                    jobs[match_row].orientation = orientation
                    self._set_current_jobs(jobs)
                    self._table.item(match_row, self.COL_PAGES).setText(str(page_count))
                    ori_map = {"portrait": "竖", "landscape": "横", "mixed": "混"}
                    self._table.item(match_row, self.COL_ORIENT).setText(ori_map.get(orientation, ""))
                    self._recalc_row_cost(match_row)
                    self._update_total_cost()
                return  # 缓存命中，跳过转换
        # 缓存未命中 → 启动转换线程（同 file_path 的旧 worker 先取消，防重复写缓存）
        self._cancel_convert_worker_for_path(file_path)
        worker = ConvertWorker(row, file_path, engine, source_md5)
        worker.finished.connect(self._on_convert_finished)
        self._convert_workers.append(worker)
        worker.start()

    def _cancel_convert_worker_for_path(self, file_path: str):
        """取消并等待指定 file_path 的转换 worker（协作式取消，2s 超时后 terminate 兜底）。"""
        self._convert_workers = [w for w in self._convert_workers if w.isRunning()]
        for w in self._convert_workers:
            if getattr(w, '_file_path', '') == file_path:
                try:
                    w.finished.disconnect()
                except Exception:
                    pass
                if w.isRunning():
                    w.cancel()
                    if not w.wait(2000):
                        w.terminate()
                        w.wait(100)

    def _cancel_all_convert_workers(self):
        """终止所有正在进行的 PDF 转换线程（协作式取消，2s 超时后 terminate 兜底）。"""
        for w in self._convert_workers:
            if w.isRunning():
                try:
                    w.finished.disconnect()
                except Exception:
                    pass
                w.cancel()
                if not w.wait(2000):
                    w.terminate()
                    w.wait(100)
        self._convert_workers.clear()

    def _resolve_engine(self, job: PrintJob) -> str:
        """
        返回最终使用的引擎名。
        非 Word 文件固定使用 LibreOffice；
        Word 文件使用 job.engine（已在拖入时自动检测好，或用户手动选择）。
        """
        ext = os.path.splitext(job.file_path)[1].lower()
        if ext not in (".doc", ".docx"):
            return "libreoffice"
        return job.engine

    def _refresh_engine_availability(self):
        """查询引擎可用性，灰度不可用的下拉选项。"""
        from converter import get_available_engines
        available = get_available_engines()
        eng_to_idx = {"word": 0, "wps": 1, "libreoffice": 2}
        model = self._edit_engine.model()
        for eng, idx in eng_to_idx.items():
            if model and idx < model.rowCount():
                item = model.item(idx)
                if item:
                    item.setEnabled(available.get(eng, False))
                    if not available.get(eng, False):
                        item.setToolTip(f"{eng.upper()} 未安装，不可用")

    def _check_word_engine_available(self) -> bool:
        """检查是否至少有一个 Word 引擎可用（用于阻止无引擎打印）。"""
        from converter import get_available_engines
        available = get_available_engines()
        return available.get("word", False) or available.get("wps", False) or available.get("libreoffice", False)

    def _current_owner_params(self) -> dict:
        """当前标签页的归属参数（owner_name / is_admin_print），供预留/上报随请求传给后端。
        建单前已校验存在成员（_has_members），owner_name 恒为真实成员；空值兜底由后端处理。"""
        tab = self._config.tabs.get(self._current_tab)
        return {
            "owner_name": (tab.owner_name if tab else "") or "",
            "is_admin_print": "1" if (bool(tab.is_admin_print) if tab else True) else "0",
        }

    def _generate_local_order_number_l(self) -> str:
        """生成本地临时订单号 HN{date}-L{seq:04d}（离线/云端取号失败时使用，连线后自动换正式号）。"""
        from datetime import date
        today = date.today().strftime("%Y%m%d")
        self._config.last_order_number += 1
        return f"HN{today}-L{self._config.last_order_number:04d}"

    def _ensure_order_number(self) -> str:
        """确保当前标签页有订单号。连线时从后端获取，离线时生成本地临时号（-L 前缀）。"""
        jobs = self._get_current_jobs()
        if not jobs:
            return ""
        for j in jobs:
            if j.order_number:
                return j.order_number
        # 已固定的标签页不再申请新订单号
        if self._is_current_tab_frozen():
            return ""
        # 尝试从后端获取
        online = self._cloud_client and self._cloud_client.is_connected()
        if online and self._cloud_client.api_url and self._cloud_client.token:
            try:
                resp = http_requests.get(
                    f"{self._cloud_client.api_url}/api/next_order_number",
                    params={**{"token": self._cloud_client.token}, **self._current_owner_params()},
                    timeout=5,
                )
                if resp.ok:
                    data = resp.json()
                    if data.get("success"):
                        order_number = data.get("order_number", "")
                        if order_number:
                            for j in jobs: j.order_number = order_number
                            self._set_current_jobs(jobs)
                            self._refresh_tab_display()
                            self._log(f"📋 已分配订单号: {order_number}")
                            return order_number
            except Exception:
                pass
        # 离线：生成本地临时号（统一 -L 格式，连线后自动换正式号）
        order_number = self._generate_local_order_number_l()
        for j in jobs:
            j.order_number = order_number
        self._set_current_jobs(jobs)
        self._refresh_tab_display()
        self._log(f"📋 已分配本地订单号: {order_number}（连线后将自动同步）")
        return order_number

    def _sync_local_orders_to_cloud(self):
        """连线后：将所有本地临时订单号（-L / 遗留 LOCAL-*）同步为云端正式订单号并上报。
        覆盖两处来源：① 标签页 jobs（当前/历史队列）；② 本地离线库 offline_orders
        （含标签页已清理的孤儿单）。换号后在离线库同步改名并标记已同步，防止 OfflineSync
        以旧号重复上传；随后用新号走 /api/local_orders 上报（reserved → sent）。"""
        if not self._cloud_client or not self._cloud_client.is_connected():
            return
        if not (self._cloud_client.api_url and self._cloud_client.token):
            return
        candidates: dict[str, dict] = {}  # old_number → {"owner_name", "is_admin_print"}
        # ① 标签页 jobs
        for key, tab in self._config.tabs.items():
            for job in tab.jobs:
                num = job.order_number or ""
                if num and (("-L" in num) or num.startswith("LOCAL-")) and num not in candidates:
                    candidates[num] = {
                        "owner_name": (tab.owner_name or ""),
                        "is_admin_print": bool(tab.is_admin_print),
                    }
        # ② 本地离线库（含标签页已清理的孤儿单）
        if self._offline_sync:
            for row in (self._offline_sync.list_pending_orders(like="%-L%")
                        + self._offline_sync.list_pending_orders(like="LOCAL-%")):
                num = row[1] or ""
                if num and num not in candidates:
                    candidates[num] = {
                        "owner_name": (row[5] or ""),
                        "is_admin_print": bool(row[6]),
                    }
        if not candidates:
            return
        replacements = {}  # old_number → new_number
        for old_num, meta in candidates.items():
            try:
                resp = http_requests.get(
                    f"{self._cloud_client.api_url}/api/next_order_number",
                    params={
                        "token": self._cloud_client.token,
                        "owner_name": meta["owner_name"] or DEFAULT_OWNER_NAME,
                        "is_admin_print": "1" if meta["is_admin_print"] else "0",
                    },
                    timeout=5,
                )
                if resp.ok and resp.json().get("success"):
                    new_num = resp.json().get("order_number", "")
                    if new_num:
                        replacements[old_num] = new_num
            except Exception:
                pass
        if not replacements:
            return
        # 替换所有本地号为云端号（job 为可变对象，就地修改即可）
        for key, tab in self._config.tabs.items():
            for job in tab.jobs:
                if job.order_number in replacements:
                    job.order_number = replacements[job.order_number]
        self._save_config()
        # 离线库同步改名（保持待同步；孤儿单靠它找到新号）
        if self._offline_sync:
            for old_num, new_num in replacements.items():
                self._offline_sync.rename_order_number(old_num, new_num)
        # 后台线程内不直接操作 Qt 控件：换号刷新/日志经信号回主线程
        self._tabDisplayRefresh.emit()
        self._cloudConnSyncLog.emit(f"📋 已同步 {len(replacements)} 个本地订单号到云端: {' '.join(replacements.values())}")
        # 上报到后端（新号 → reserved 占位更新为 sent）
        for old_num, new_num in replacements.items():
            meta = candidates[old_num]
            try:
                order_jobs = []
                for tab2 in self._config.tabs.values():
                    for j in tab2.jobs:
                        if j.order_number == new_num:
                            order_jobs.append(j)
                payload = None
                if order_jobs:
                    payload = {
                        "order_number": new_num,
                        "total_price": sum(calc_cost(j.page_count, j.copies, j.duplex,
                            self._config.simplex_price, self._config.duplex_price,
                            j.page_range)[0] for j in order_jobs),
                        "files": [{
                            "file_name": j.display_name or os.path.basename(j.file_path),
                            "copies": j.copies,
                            "page_count": j.page_count,
                            "cost": calc_cost(j.page_count, j.copies, j.duplex,
                                self._config.simplex_price, self._config.duplex_price,
                                j.page_range)[0],
                            "duplex": j.duplex,
                            "page_range": j.page_range,
                        } for j in order_jobs],
                        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                elif self._offline_sync:
                    # 孤儿单：标签页已清理，从离线库行构造 payload
                    row = self._offline_sync.find_pending_by_number(new_num)
                    if row:
                        payload = self._build_files_payload_from_db_row(row)
                if payload:
                    payload["owner_name"] = meta["owner_name"] or DEFAULT_OWNER_NAME
                    payload["is_admin_print"] = meta["is_admin_print"]
                    resp = http_requests.post(
                        f"{self._cloud_client.api_url}/api/local_orders",
                        params={"token": self._cloud_client.token},
                        json=payload,
                        timeout=10,
                    )
                    if resp.ok and resp.json().get("success"):
                        if self._offline_sync:
                            self._offline_sync.mark_synced(new_num)
            except Exception:
                pass

    @staticmethod
    def _build_files_payload_from_db_row(row) -> dict:
        """把离线库行（id, order_number, files_json, total_price, created_at, owner_name, is_admin_print）
        构造成 /api/local_orders 的 payload（孤儿单换号上报用）。"""
        dbid, num, files_json, total_price, created_at, owner_name, is_admin_print = row
        files = []
        try:
            for f in json.loads(files_json) or []:
                if not isinstance(f, dict):
                    continue
                files.append({
                    "file_name": f.get("file_name", ""),
                    "copies": int(f.get("copies", 1) or 1),
                    "page_count": int(f.get("page_count", 0) or 0),
                    "cost": float(f.get("cost", 0) or 0),
                    "duplex": f.get("duplex", "on"),
                    "page_range": f.get("page_range", ""),
                })
        except Exception:
            files = []
        return {
            "order_number": num,
            "total_price": float(total_price or 0),
            "files": files,
            "created_at": created_at or "",
        }

    def _on_copy_total(self):
        """复制合计金额到剪贴板（含订单号）。无成员时禁止创建订单。"""
        if not self._has_members():
            self._show_no_member_hint()
            return
        if not self._get_current_jobs():
            return  # 标签页无文件时不复制价格（首页费等附加费不构成独立价格）
        order_number = self._ensure_order_number()
        text = self._total_label.text()
        # 去掉"合计: "前缀和"≈ "前缀，保留 ¥ 符号
        amount = text.replace("合计: ", "").replace("≈ ", "").strip()
        try:
            # 验证是否为有效金额
            float(amount.replace("¥", ""))
            copy_text = amount
            if order_number:
                copy_text = f"{order_number} — {amount}"
            clipboard = QApplication.clipboard()
            clipboard.setText(copy_text)
            if self._copy_total_btn:
                self._copy_total_btn.setText("✅ 已复制")
                self._copy_total_btn.setEnabled(False)
            if self._copy_total_timer:
                self._copy_total_timer.start(5000)
        except ValueError:
            pass  # 金额无效时不复制

    def _reset_copy_button(self):
        """恢复复制按钮为可点击状态（无文件时保持禁用）。"""
        if self._copy_total_timer and self._copy_total_timer.isActive():
            self._copy_total_timer.stop()
        has_jobs = bool(self._get_current_jobs())
        if self._copy_total_btn:
            self._copy_total_btn.setText("📋 复制")
            self._copy_total_btn.setEnabled(has_jobs)
        if self._copy_detail_btn:
            self._copy_detail_btn.setText("📋 复制计费明细")
            self._copy_detail_btn.setEnabled(has_jobs)

    def _on_toggle_detail(self, checked: bool):
        """展开/收起计费明细复制按钮。"""
        self._copy_detail_btn.setVisible(checked)
        self._detail_toggle_btn.setText("⏶" if checked else "⏷")
        self._detail_toggle_btn.setToolTip("隐藏计费明细" if checked else "展开计费明细")

    def _can_paste_files(self) -> bool:
        """检查剪贴板是否包含可粘贴的文件。"""
        allowed = {".pdf", ".doc", ".docx", ".txt", ".md",
                   ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
        # 1. 来自文件管理器的 URL 列表
        mime = QApplication.clipboard().mimeData()
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    path = url.toLocalFile()
                    ext = os.path.splitext(path)[1].lower()
                    if ext in allowed and os.path.isfile(path):
                        return True
        # 2. 纯文本路径
        text = QApplication.clipboard().text().strip()
        if text:
            for line in text.splitlines():
                line = line.strip().strip('"').strip("'")
                ext = os.path.splitext(line)[1].lower()
                if ext in allowed:
                    return True
        return False

    def _on_paste_files(self):
        """从剪贴板粘贴文件，支持文件管理器复制和纯文本路径。"""
        allowed = {".pdf", ".doc", ".docx", ".txt", ".md",
                   ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
        files: list[str] = []

        # 1. 优先处理文件管理器复制的 URL 列表
        mime = QApplication.clipboard().mimeData()
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    path = url.toLocalFile()
                    ext = os.path.splitext(path)[1].lower()
                    if ext in allowed and os.path.isfile(path):
                        files.append(os.path.normpath(path))

        # 2. 降级处理纯文本路径（每行一个）
        if not files:
            text = QApplication.clipboard().text().strip()
            for line in text.splitlines():
                line = line.strip().strip('"').strip("'")
                if not line:
                    continue
                ext = os.path.splitext(line)[1].lower()
                if ext in allowed and os.path.isfile(line):
                    files.append(os.path.normpath(line))

        if files:
            self._add_files_to_table(files)
        else:
            QMessageBox.information(self, "粘贴", "剪贴板中没有可识别的文件。\n\n"
                                    "请先在文件管理器中复制文件(Ctrl+C)，再来粘贴。\n"
                                    "支持格式: PDF, Word(.doc/.docx), "
                                    "文本(.txt/.md), 图片(.jpg/.png/.bmp等)")

    def _on_manage_locations(self):
        """打开地点管理对话框。"""
        dlg = LocationManagerDialog(self._config.delivery_percentages, self)
        if dlg.exec() == QDialog.Accepted:
            new_locs = dlg.get_locations()
            self._config.delivery_percentages = new_locs
            # 如果当前选中的地点已被删除，改为第一个
            if self._config.delivery_location not in new_locs:
                self._config.delivery_location = next(iter(new_locs))
            # 刷新地点下拉框
            self._delivery_location_combo.blockSignals(True)
            self._delivery_location_combo.clear()
            self._delivery_location_combo.addItems(list(new_locs.keys()))
            idx = self._delivery_location_combo.findText(self._config.delivery_location)
            if idx >= 0:
                self._delivery_location_combo.setCurrentIndex(idx)
            self._delivery_location_combo.blockSignals(False)
            # 更新百分比显示
            pct = new_locs.get(self._config.delivery_location, 0.0)
            self._delivery_percent_spin.blockSignals(True)
            self._delivery_percent_spin.setValue(pct)
            self._delivery_percent_spin.blockSignals(False)
            self._on_price_changed()

    def _on_add_files(self):
        """通过文件对话框添加文件。"""
        file_filter = (
            "所有支持格式 (*.pdf *.doc *.docx *.txt *.md"
            " *.jpg *.jpeg *.png *.bmp *.gif *.webp);;"
            "PDF (*.pdf);;"
            "Word 文档 (*.doc *.docx);;"
            "文本 (*.txt *.md *.html *.htm);;"
            "图片 (*.jpg *.jpeg *.png *.bmp *.gif *.webp);;"
            "所有文件 (*.*)"
        )
        files, _ = QFileDialog.getOpenFileNames(self, "添加文件", self._last_dir, file_filter)
        if not files:
            return
        self._last_dir = os.path.dirname(files[0])
        self._add_files_to_table(files)

    def _on_files_dropped(self, files: list[str]):
        """拖放文件到表格。"""
        self._add_files_to_table(files)

    def _add_files_to_table(self, files: list[str]):
        """添加文件到任务列表的核心逻辑。"""
        if self._is_current_tab_frozen():
            self._log("🔒 该标签页已固定，不可添加文件")
            return
        self._cancel_undo_if_active()
        if getattr(self, '_loading_files', False):
            self._log("⏳ 正在加载文件，请稍候（上一批文件尚未处理完成）")
            return
        self._loading_files = True
        try:
            self.__add_files_to_table_impl(files)
        finally:
            self._loading_files = False

    def _on_shortcut_copy_total(self):
        """Ctrl+C：仅在非文本编辑状态下复制总价格。"""
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit) or isinstance(focused, QTextEdit):
            return  # 让输入框正常处理 Ctrl+C
        self._on_copy_total()

    def _on_shortcut_copy_detail(self):
        """Ctrl+Shift+C：仅在非文本编辑状态下复制计费明细。"""
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit) or isinstance(focused, QTextEdit):
            return
        self._on_copy_detail()

    def _on_shortcut_delete(self):
        """Delete 快捷键：仅在非文本编辑状态下删除任务。"""
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit) or isinstance(focused, QTextEdit):
            return  # 让输入框正常处理 Delete 键
        self._on_remove_selected()

    def _on_shortcut_paste(self):
        """Ctrl+V 快捷键：非文本编辑时粘贴文件。"""
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit) or isinstance(focused, QTextEdit):
            return  # 让输入框正常处理粘贴
        if self._can_paste_files():
            self._on_paste_files()

    def _abandon_jobs(self, jobs):
        """通知后端放弃未打印的云端任务（已完成的跳过）。"""
        if not self._cloud_client:
            return
        for job in jobs:
            if getattr(job, 'sent', False):
                continue
            if job.order_id > 0:
                self._cloud_client.abandon_order_to_server(job.order_id)
            elif job.task_id > 0:
                self._cloud_client.abandon_order_to_server(job.task_id)

    def _on_undo_expired(self):
        """撤回超时 → 确认清空，通知后端放弃被清空的云端任务。"""
        jobs = self._cleared_jobs_backup.pop(self._current_tab, [])
        self._abandon_jobs(jobs)
        self._restore_clear_button()

    def _restore_clear_button(self):
        """恢复清空按钮正常样式。"""
        self._btn_clear.setText("✖ 清空列表")
        self._btn_clear.setStyleSheet("")
        self._btn_clear.setObjectName("")
        self._btn_clear.style().unpolish(self._btn_clear)
        self._btn_clear.style().polish(self._btn_clear)

    def _cancel_undo_if_active(self):
        """新增任务时取消撤回状态 → 确认清空，通知后端放弃被清空的云端任务。"""
        if self._clear_undo_timer.isActive():
            self._clear_undo_timer.stop()
        jobs = self._cleared_jobs_backup.pop(self._current_tab, [])
        self._abandon_jobs(jobs)
        self._restore_clear_button()

    def _on_progress(self, current: int, total: int, status: str):
        """更新进度条。"""
        self._progress_bar.setMaximum(total)
        self._progress_bar.setValue(current)
        self._status_label.setText(status)

    def _on_pdf_cached(self, md5: str, cached_path: str, name: str, ext: str, page_count: int, orientation: str):
        """打印转换后的图片 PDF 补写缓存索引（文件已由 worker 写入，这里只补 meta，供后续缓存命中）。"""
        if not self._cloud_client:
            return
        try:
            # cached_path 即目标缓存文件路径，_save_pdf_to_cache 内 samefile 命中 → 跳过复制、只写索引
            self._cloud_client._save_pdf_to_cache(md5, cached_path, name, ext, page_count, orientation)
        except Exception as e:
            self._log(f"  → 图片 PDF 缓存索引写入失败: {e}")

    def _on_job_finished(self, idx: int, success: bool, message: str):
        """单个任务完成回调：上报云端 + 标记完成（新 UI 无表格行操作）。"""
        flat_jobs = getattr(self, '_flat_jobs', [])
        if 0 <= idx < len(flat_jobs):
            job = flat_jobs[idx]
            task_id = getattr(job, 'task_id', 0)
            if success:
                # 打印成功 → 标记已完成并立即持久化，异常退出重载后仍保留，退出时不再放弃
                job.sent = True
                self._save_config()
            if task_id and self._cloud_client:
                if success:
                    # 归属标记（v24）：云端订单为顾客订单，由打印它的管理员盖章（订单号右侧下拉选择）
                    owner_name = ""
                    tab = self._config.tabs.get(self._current_tab)
                    if tab:
                        owner_name = (tab.owner_name or "").strip()
                    # 上报实际打印配置（本地可能修改过份数/双面/范围/页数），后端同步 order_files
                    # 单双面标记与实际一致：有效打印页数=1 时（整份 1 页 / 范围恰好选 1 页）物理上即单面打印
                    eff_pages = _count_pages_in_range(job.page_range or "", job.page_count or 0)
                    report_cfg = {
                        "copies": job.copies,
                        "duplex": job.duplex if eff_pages > 1 else "off",
                        "page_range": job.page_range or "",
                        "page_count": job.page_count or 0,
                    }
                    if owner_name:
                        report_cfg["owner_name"] = owner_name
                    # v24.1：归属标记随打印回报（管理员可在机位勾选/取消），后端同步 orders
                    report_cfg["is_admin_print"] = bool(tab.is_admin_print) if tab else False
                    self._cloud_client.report_success(task_id, report_cfg)
                else:
                    self._cloud_client.report_fail(task_id, message)

    def _start_print_worker(self, flat_jobs: list) -> bool:
        """用给定的任务列表启动打印 Worker。返回 True=已启动，False=忙/被取消。"""
        if not self._has_members():
            # 无成员禁止创建订单（订单必须归属某个成员）
            self._show_no_member_hint()
            return False
        if self._worker is not None and self._worker.isRunning():
            self._log("⚠️ 已有打印任务正在进行，不能重复启动")
            return False

        # 打印前校验页码范围：页数已知且范围非法 → 弹一次确认框（确认后按全部页打印）
        invalid_warned = False
        for j in flat_jobs:
            if j.page_count > 0 and j.page_range and j.page_range.strip():
                from printer_config import _parse_range_parts
                if not _parse_range_parts(j.page_range, j.page_count):
                    if not invalid_warned:
                        invalid_warned = True
                        reply = QMessageBox.question(
                            self, "页码范围无效",
                            f"文件「{j.display_name or os.path.basename(j.file_path)}」的页码范围无效。\n"
                            f"将按全部页打印，是否继续？",
                            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                        )
                        if reply != QMessageBox.Yes:
                            self._log("🛑 用户取消打印：页码范围无效")
                            return False
                    self._log(f"⚠ 文件「{j.display_name or os.path.basename(j.file_path)}」页码范围无效，将打印全部页")

        self._flat_jobs = flat_jobs
        self._sync_ui_to_config()

        # 立即冻结标签页 — 一旦开始打印，订单即已固定，不允许任何编辑
        tab = self._config.tabs.get(self._current_tab)
        if tab:
            tab.frozen = True
        self._save_config()
        self._refresh_tab_display()
        self._log(f"🔒 标签页 {self._current_tab} 已固定，打印完成后不可编辑")

        from datetime import datetime
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 构造文件数据（在线/离线共用）
        files_data = []
        total = 0.0
        for j in flat_jobs:
            cost = calc_cost(j.page_count, j.copies, j.duplex,
                self._config.simplex_price, self._config.duplex_price,
                j.page_range)[0]
            total += cost
            files_data.append({
                "file_name": j.display_name or os.path.basename(j.file_path),
                "copies": j.copies,
                "page_count": j.page_count,
                "cost": round(cost, 2),
                # 单双面标记与实际一致：有效打印页数=1（整份 1 页 / 范围恰好选 1 页）→ 单面
                "duplex": j.duplex if _count_pages_in_range(j.page_range or "", j.page_count or 0) > 1 else "off",
                "page_range": j.page_range,
            })
        # 附加服务：与界面合计（_update_total_cost）口径一致，从标签页读设置
        tab = self._config.tabs.get(self._current_tab)
        tab_extra = tab.calc_extra_total(total, self._config) if tab else 0.0
        total_price = round(total + tab_extra, 2)
        extra_fields = {
            "delivery_enabled": bool(tab.delivery_enabled) if tab else self._config.delivery_enabled,
            "delivery_location": (tab.delivery_location if tab else self._config.delivery_location),
            "urgency": (tab.urgency if tab else self._config.urgency),
            "cover_page": bool(tab.cover_page) if tab else self._config.cover_page,
            "cover_page_price": (tab.cover_page_price if tab else self._config.cover_page_price),
        }

        # 获取订单号并上传：在线直接上报后端，离线暂存本地数据库
        # 注意：如果标签页已有订单号（用户之前点击了"复制"），则复用该订单号，
        # 避免重复调用 /api/next_order_number 导致订单号浪费。
        online = self._cloud_client and self._cloud_client.is_connected()
        order_number = ""
        uploaded = False
        existing_order = ""
        for j in flat_jobs:
            if j.order_number:
                existing_order = j.order_number
                break

        if existing_order:
            # 复用已有的订单号（来自之前的"复制"操作）
            order_number = existing_order
            self._log(f"📋 复用已有订单号: {order_number}")

            # 在线时直接上传（离线时等待同步）
            if online and self._cloud_client.api_url and self._cloud_client.token:
                try:
                    resp = http_requests.post(
                        f"{self._cloud_client.api_url}/api/local_orders",
                        params={"token": self._cloud_client.token},
                        json={
                            "order_number": order_number,
                            "total_price": total_price,
                            "files": files_data,
                            "created_at": created_at,
                            # 订单归属（v24）：谁处理了这笔订单 + 是否管理员自行打印
                            "owner_name": (tab.owner_name if tab else DEFAULT_OWNER_NAME),
                            "is_admin_print": bool(tab.is_admin_print) if tab else True,
                            # 附加服务上报（与计费口径一致）
                            **extra_fields,
                        },
                        timeout=10,
                    )
                    if resp.ok and resp.json().get("success"):
                        self._log(f"📋 订单已上报云端: {order_number} ({len(files_data)} 个文件)")
                        uploaded = True
                    else:
                        self._log(f"⚠️ 订单上报云端失败: {resp.text[:200] if resp.text else '无响应'}")
                except Exception as e:
                    self._log(f"⚠️ 订单上报云端失败: {e}")
        elif online and self._cloud_client.api_url and self._cloud_client.token:
            # 在线：从后端获取全局唯一订单号（同时后端创建 reserved 占位记录）——
            # 预留即带当前标签页归属，避免上报失败时占位单无归属
            try:
                resp = http_requests.get(
                    f"{self._cloud_client.api_url}/api/next_order_number",
                    params={**{"token": self._cloud_client.token}, **self._current_owner_params()},
                    timeout=5,
                )
                if resp.ok:
                    data = resp.json()
                    if data.get("success"):
                        order_number = data.get("order_number", "")
            except Exception as e:
                self._log(f"⚠️ 获取云端订单号失败: {e}")

            if not order_number:
                # 获取订单号失败，回退到本地临时号（-L，连线后自动换正式号）
                order_number = self._generate_local_order_number_l()

            # 上传订单信息到后端（将 reserved 占位更新为 sent）
            try:
                resp = http_requests.post(
                    f"{self._cloud_client.api_url}/api/local_orders",
                    params={"token": self._cloud_client.token},
                    json={
                        "order_number": order_number,
                        "total_price": total_price,
                        "files": files_data,
                        "created_at": created_at,
                        # 订单归属（v24）：谁处理了这笔订单 + 是否管理员自行打印
                        "owner_name": (tab.owner_name if tab else DEFAULT_OWNER_NAME),
                        "is_admin_print": bool(tab.is_admin_print) if tab else True,
                        # 附加服务上报（与计费口径一致）
                        **extra_fields,
                    },
                    timeout=10,
                )
                if resp.ok and resp.json().get("success"):
                    self._log(f"📋 订单已上报云端: {order_number} ({len(files_data)} 个文件)")
                    uploaded = True
                else:
                    self._log(f"⚠️ 订单上报云端失败: {resp.text[:200] if resp.text else '无响应'}")
            except Exception as e:
                self._log(f"⚠️ 订单上报云端失败: {e}")
        else:
            # 离线：统一生成本地临时订单号（HN{date}-L{seq}），暂存到本地数据库，
            # 联网后由 _sync_local_orders_to_cloud 换正式号并上报
            order_number = self._generate_local_order_number_l()
            self._log(f"📋 离线模式：已生成本地订单号 {order_number}")
            if self._offline_sync:
                try:
                    self._offline_sync.save_order_offline(
                        order_number=order_number,
                        files_data=files_data,
                        total_price=total_price,
                        created_at=created_at,
                        owner_name=(tab.owner_name if tab else ""),
                        is_admin_print=bool(tab.is_admin_print) if tab else True,
                    )
                    self._log(f"📋 离线模式：任务已缓存，联网后自动上传 ({len(files_data)} 个文件)")
                except Exception as e:
                    self._log(f"⚠️ 离线缓存失败: {e}")

        # 将订单号写回 jobs（重要：确保封面页等后续流程能读到订单号）
        for j in flat_jobs:
            j.order_number = order_number

        try:
            self._config.save(self._config_path)
        except Exception as e:
            logger.warning(f"保存配置失败: {e}")

        # 刷新左下角订单号显示（用户可能未点击"复制"直接点击"开始打印"）
        self._refresh_tab_display()

        self._btn_start.setEnabled(False)
        self._progress_bar.setValue(0)

        # 封面页配置：标签页有独立附加服务设置则读标签页，否则读全局（与计费/上传口径一致）
        cover_page_enabled = bool(tab.cover_page) if tab else self._config.cover_page
        cover_page_config = {
            "simplex_price": self._config.simplex_price,
            "duplex_price": self._config.duplex_price,
            "delivery_enabled": bool(tab.delivery_enabled) if tab else self._config.delivery_enabled,
            "delivery_location": (tab.delivery_location if tab else self._config.delivery_location),
            "delivery_percentages": self._config.delivery_percentages,
            "urgency": (tab.urgency if tab else self._config.urgency),
            "urgency_prices": self._config.urgency_prices,
            "cover_page_price": (tab.cover_page_price if tab else self._config.cover_page_price),
            "pickup_address": self._config.pickup_address,
            "order_number": order_number,
            "created_at": created_at,
        }

        worker = PrintWorker(
            jobs=flat_jobs,
            printer_name=self._config.printer_name,
            duplex_mode=self._config.duplex_mode,
            keep_temp_pdf=self._config.keep_temp_pdf,
            render_dpi=self._config.render_dpi,
            cover_page=cover_page_enabled,
            cover_page_config=cover_page_config,
        )
        worker.progress.connect(self._on_progress)
        worker.log_message.connect(self._log)
        worker.job_finished.connect(self._on_job_finished)
        worker.all_finished.connect(self._on_all_finished)
        worker.pdf_cached.connect(self._on_pdf_cached)
        self._worker = worker
        worker.start()
        return True

    def _on_all_finished(self, success_count: int, fail_count: int):
        """全部任务完成。标签页已固定，不允许再次打印或编辑。"""
        self._worker = None
        self._all_printed = (fail_count == 0)

        total = success_count + fail_count
        status = "✅ 全部成功" if fail_count == 0 else f"⚠️ 成功 {success_count} / 失败 {fail_count}"
        self._status_label.setText(f"🔒 已完成：{status}（共 {total} 个任务）")

        if fail_count > 0:
            QMessageBox.warning(
                self, "打印完成（有错误）",
                f"全部 {total} 个任务处理完毕。\n成功: {success_count}\n失败: {fail_count}\n\n"
                f"⚠️ 该标签页已锁定，不可编辑。如需重试失败文件，请新建标签页。\n详情请查看日志。"
            )
        else:
            self._log(f"🔒 全部 {total} 个任务打印成功！标签页已锁定。")

        # 无障碍自动打印重试队列：打印机空闲后补打忙时丢弃的订单（只打未完成的，已打的不重打）
        if self._auto_print_retry:
            item = self._auto_print_retry.pop(0)
            tab_key = item.get("tab_key", "")
            order_id = item.get("order_id", 0)
            if tab_key in self._config.tabs and self._config.tabs[tab_key].jobs:
                self._current_tab = tab_key
                self._config.active_tab = tab_key
                self._rebuild_table()
                self._refresh_tab_display()
                pending = [j for j in self._config.tabs[tab_key].jobs if not getattr(j, 'sent', False)]
                if pending:
                    self._log(f"⚡ 打印机空闲，补打订单 #{order_id}（标签页 {tab_key}，{len(pending)} 个未打印文件）")
                    self._start_print_worker(pending)
                else:
                    self._log(f"⚠ 重试订单 #{order_id} 标签页 {tab_key} 已全部打印完成，跳过补打")
            else:
                self._log(f"⚠ 重试订单 #{order_id} 标签页 {tab_key} 已无文件，跳过补打")

    def _on_about(self):
        """关于对话框。"""
        QMessageBox.about(
            self, "关于 HN 本地打印工具",
            "<h3>HN 本地打印工具 v4.1.2</h3>"
            "<p>本地文件一键打印工具，支持多种文件格式。</p>"
            "<p>支持拖放添加、自动计费、浅色/深色主题切换。</p>"
            "<hr>"
            "<p>核心流程：文件 → PDF → Windows 原生 GDI 打印</p>"
            "<p>外部工具（可选）：LibreOffice | wkhtmltopdf | SumatraPDF</p>"
            "<p>技术：PySide6 + PyMuPDF + PyPDF2 + python-docx</p>"
            "<hr>"
            "<p><b>⚠ 仅用于学习用途</b></p>"
            "<p>GitHub: <a href='https://github.com/huonanwholovecomputer/h_n-printer'>"
            "github.com/huonanwholovecomputer/h_n-printer</a></p>"
        )

    def _on_self_check(self):
        """自检：检查外部工具和 COM 引擎状态。"""
        from converter import _find_libreoffice, _find_wkhtmltopdf, get_available_engines
        from converter import _warm_word_running, _warm_wps_running
        from pdf_printer import _find_sumatra_pdf

        def _status_icon(ok: bool) -> str:
            return "✅" if ok else "❌"

        def _status_text(ok: bool, detail: str = "") -> str:
            icon = _status_icon(ok)
            if ok:
                return f"{icon} <span style='color:#4a9;font-weight:bold'>可用</span> {detail}"
            else:
                return f"{icon} <span style='color:#c55;font-weight:bold'>不可用</span> {detail}"

        rows = []

        # ── 外部工具 ──
        lo = _find_libreoffice()
        rows.append(("LibreOffice<br><small>(Office→PDF)</small>",
                     _status_text(lo is not None, f"<small>{lo or ''}</small>")))

        wk = _find_wkhtmltopdf()
        rows.append(("wkhtmltopdf<br><small>(HTML/MD→PDF)</small>",
                     _status_text(wk is not None, f"<small>{wk or ''}</small>")))

        sumatra = _find_sumatra_pdf()
        rows.append(("SumatraPDF<br><small>(备用打印)</small>",
                     _status_text(sumatra is not None, f"<small>{sumatra or ''}</small>")))

        # ── COM 引擎 ──
        engines = get_available_engines()
        word_ok = engines.get("word", False)
        wps_ok = engines.get("wps", False)
        lo_ok = engines.get("libreoffice", False)

        ww = " (已预热)" if _warm_word_running else ""
        wps_w = " (已预热)" if _warm_wps_running else ""

        rows.append(("<hr><b>COM 引擎</b>", ""))
        rows.append(("Microsoft Word COM", _status_text(word_ok, ww)))
        rows.append(("WPS Office COM", _status_text(wps_ok, wps_w)))
        rows.append(("LibreOffice 无头模式", _status_text(lo_ok)))

        # ── 系统打印机 ──
        printers = list_system_printers()
        printer_count = len(printers)
        printer_ok = printer_count > 0
        printer_detail = f"共 {printer_count} 台"
        rows.append(("<hr><b>系统打印机</b>", ""))
        rows.append(("打印机", _status_text(printer_ok, printer_detail)))

        # 构建 HTML 表格
        html = "<style>td{padding:3px 8px;}</style>"
        html += "<table>"
        for label, status in rows:
            html += f"<tr><td>{label}</td><td>{status}</td></tr>"
        html += "</table>"

        QMessageBox.information(self, "自检", html)

    def _on_show_log_manager(self):
        """弹出日志管理窗口。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("📋 日志管理")
        dlg.setMinimumWidth(450)
        dlg.setModal(True)

        layout = QVBoxLayout(dlg)
        layout.setSpacing(10)
        layout.setContentsMargins(14, 12, 14, 12)

        layout.addWidget(QLabel("<b>日志管理</b>"))

        # 状态标签
        status_label = QLabel("就绪")
        status_label.setWordWrap(True)
        layout.addWidget(status_label)

        # 拉取按钮
        fetch_btn = QPushButton("📥 拉取前后端日志")
        fetch_btn.clicked.connect(lambda: self._fetch_remote_logs(status_label))
        layout.addWidget(fetch_btn)

        layout.addWidget(QLabel("<hr>"))

        # 本地日志操作
        local_layout = QHBoxLayout()
        open_local_btn = QPushButton("📂 打开本地日志目录")
        open_local_btn.clicked.connect(lambda: os.startfile(self._log_dir) if os.path.isdir(self._log_dir) else None)
        local_layout.addWidget(open_local_btn)

        clear_local_btn = QPushButton("🗑 清空本地日志")
        clear_local_btn.clicked.connect(lambda: self._clear_local_logs(status_label))
        local_layout.addWidget(clear_local_btn)
        layout.addLayout(local_layout)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn)

        dlg.exec()

    def _fetch_remote_logs(self, status_label: QLabel):
        """从后端拉取 server.log 和 frontend.log。"""
        if not self._cloud_client or not self._cloud_client.api_url or not self._cloud_client.token:
            status_label.setText("⚠ 云端未连接，无法拉取")
            return
        status_label.setText("⏳ 正在拉取...")
        QApplication.processEvents()

        results = []
        for log_type in ("server", "frontend"):
            try:
                resp = http_requests.get(
                    f"{self._cloud_client.api_url}/api/log/fetch",
                    params={"token": self._cloud_client.token, "type": log_type},
                    timeout=15,
                )
                if resp.ok:
                    data = resp.json()
                    content = data.get("content", "")
                    size = data.get("size", 0)
                    if size > 0 and content:
                        dest = os.path.join(self._log_dir, f"remote_{log_type}.log")
                        with open(dest, "w", encoding="utf-8") as f:
                            f.write(content)
                    results.append(f"{log_type}: {size} 字节")
                else:
                    results.append(f"{log_type}: 请求失败")
            except Exception as e:
                results.append(f"{log_type}: 异常({e})")

        status_label.setText("✓ 拉取完成: " + " | ".join(results))

    def _clear_local_logs(self, status_label: QLabel):
        """清空本地日志文件。"""
        removed = 0
        for fname in os.listdir(self._log_dir):
            if fname.endswith(".log"):
                try:
                    os.remove(os.path.join(self._log_dir, fname))
                    removed += 1
                except OSError:
                    pass
        status_label.setText(f"✓ 已清空 {removed} 个本地日志文件")
        self._log("📋 已清空本地日志")

    def _on_shortcuts(self):
        """快捷键说明对话框。"""
        QMessageBox.information(
            self, "快捷键",
            "<table cellspacing='8'>"
            "<tr><td><b>Ctrl+C</b></td><td>复制合计金额</td></tr>"
            "<tr><td><b>Ctrl+Shift+C</b></td><td>复制计费明细</td></tr>"
            "<tr><td><b>Delete</b> / <b>Ctrl+D</b></td><td>删除选中任务</td></tr>"
            "<tr><td><b>Ctrl+V</b></td><td>粘贴文件</td></tr>"
            "</table>"
        )

    def _on_theme_changed(self):
        """主题菜单项点击回调。"""
        action = self.sender()
        if action and self._theme_manager:
            mode = action.data()
            self._theme_manager.set_mode(mode)

    def _log(self, msg: str):
        """追加日志到界面文本框（自动滚动到底部）并写入文件。"""
        ts = datetime.now().strftime("%H:%M:%S")
        plain = f"[{ts}] {msg}"
        # 写入界面（QSS 支持 HTML 彩色渲染，含/不含 <span> 都只追加一次）
        self._log_text.append(plain)
        self._log_text.verticalScrollBar().setValue(
            self._log_text.verticalScrollBar().maximum()
        )
        # 写入文件（纯文本，去掉 HTML 标签）
        import re
        plain_msg = re.sub(r'<[^>]+>', '', msg)
        self._file_logger.info(plain_msg)

    def _log_info(self, tag: str, msg: str):
        """信息日志：[标签] ℹ 消息"""
        self._log(f'<span style="color:#888">[{tag}]</span> <span style="color:#ccc">ℹ {msg}</span>')
        self._file_logger.info(f"[{tag}] {msg}")

    def _log_ok(self, tag: str, msg: str):
        """成功日志：[标签] ✓ 消息"""
        self._log(f'<span style="color:#888">[{tag}]</span> <span style="color:#4caf50">✓ {msg}</span>')
        self._file_logger.info(f"[{tag}] ✓ {msg}")

    def _log_warn(self, tag: str, msg: str):
        """警告日志：[标签] ⚠ 消息"""
        self._log(f'<span style="color:#888">[{tag}]</span> <span style="color:#ff9800">⚠ {msg}</span>')
        self._file_logger.warning(f"[{tag}] {msg}")

    def _log_error(self, tag: str, msg: str):
        """错误日志：[标签] ✗ 消息"""
        self._log(f'<span style="color:#888">[{tag}]</span> <span style="color:#f44336">✗ {msg}</span>')
        self._file_logger.error(f"[{tag}] {msg}")

    # ---- 云端任务处理 ----

    def _processed_tasks_path(self) -> str:
        """已处理任务 ID 集合的持久化文件路径（跨重启防重复打印）。"""
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".processed_tasks.json")

    def _load_processed_tasks(self) -> set:
        """启动时从磁盘加载已处理任务 ID 集合（进程崩溃/重启后不再重复打印）。"""
        try:
            with open(self._processed_tasks_path(), "r", encoding="utf-8") as f:
                import json as _json
                data = _json.load(f)
            if isinstance(data, list):
                return set(int(x) for x in data if str(x).isdigit())
        except Exception:
            pass
        return set()

    def _mark_processed_task(self, task_id: int):
        """记录已处理任务 ID 并追加保存到磁盘（超过 10000 条截断保留最近）。"""
        self._processed_cloud_tasks.add(task_id)
        try:
            items = list(self._processed_cloud_tasks)
            if len(items) > 10000:
                items = sorted(items)[-10000:]
                self._processed_cloud_tasks = set(items)
            with open(self._processed_tasks_path(), "w", encoding="utf-8") as f:
                import json as _json
                _json.dump(items, f, ensure_ascii=False)
        except Exception:
            pass

    def _is_processed_task(self, task_id: int) -> bool:
        """判断任务是否已处理（防 SocketIO + HTTP 双通道重复）。"""
        return task_id in self._processed_cloud_tasks

    def _on_cloud_task_received(self, task: CloudTask):
        """收到新的云端打印任务 → 加入云端任务列表窗口（无障碍打印任务自动跳过窗口）。"""
        if task.task_id in self._processed_cloud_tasks:
            return  # 已处理过，跳过（防 SocketIO + HTTP 双通道重复）
        self._cloud_tasks[task.task_id] = task
        is_scheduled = task.auto_print and getattr(task, "schedule_mode", "now") != "now"
        self._log(f"☁ 收到云端任务 #{task.task_id}: {task.file_name}"
                  + (" ⚡无障碍" if task.auto_print else "")
                  + (" ⏰预约" if is_scheduled else ""))
        if is_scheduled:
            # 预约单：进入预约状态机（到点才自动打印）
            self._register_scheduled_task(task)
        elif task.status == "ready":
            if task.auto_print:
                self._enqueue_auto_print_task(task)
            elif self._cloud_task_window:
                self._cloud_task_window.add_task(task)

    def _on_cloud_task_updated(self, task: CloudTask):
        """云端任务状态更新 → 就绪时加入窗口（或自动处理无障碍打印），出错时通知服务器标记失败。"""
        if task.status == "ready" and task.task_id in self._processed_cloud_tasks:
            return  # 已处理过
        self._cloud_tasks[task.task_id] = task
        # 预约单状态更新走预约状态机
        if task.order_id in self._scheduled_orders:
            self._update_scheduled_task(task)
            return
        if task.status == "error":
            if task.task_id in self._processed_cloud_tasks:
                # 任务已被用户打回/接受，迟到的下载失败不再上报后端，
                # 否则 print_fail 会把已 reject 的订单覆盖成 failed。
                self._log(f"☁ 云端任务 #{task.task_id} 下载出错但已被处理，忽略上报: {task.error_message}")
                self._cloud_tasks.pop(task.task_id, None)
                return
            self._mark_processed_task(task.task_id)
            self._log(f"☁ 云端任务 #{task.task_id} 出错: {task.error_message}")
            if self._cloud_client:
                self._cloud_client.report_fail(task.task_id, f"下载失败: {task.error_message}")
                self._cloud_client.reject_task(task.task_id)
            self._cloud_tasks.pop(task.task_id, None)
            # 无障碍打印任务出错：从队列中移除
            if task.auto_print and task.order_id:
                self._auto_print_queue.pop(task.order_id, None)
                t = self._auto_print_timers.pop(task.order_id, None)
                if t:
                    t.stop()
        elif task.status == "ready":
            self._log(f"☁ 云端任务 #{task.task_id} 下载完成"
                      + (" ⚡无障碍" if task.auto_print else ""))
            if task.auto_print:
                self._enqueue_auto_print_task(task)
            elif self._cloud_task_window:
                self._cloud_task_window.add_task(task)

    # ──────── 无障碍打印（自动建标签页 + 自动打印）────────

    def _enqueue_auto_print_task(self, task: CloudTask):
        """将无障碍打印任务加入队列，按订单分组，防抖后批量处理。"""
        if not task.order_id:
            self._log(f"⚠ 无障碍任务 #{task.task_id} 缺少 order_id，跳过")
            return
        oid = task.order_id
        if oid not in self._auto_print_queue:
            self._auto_print_queue[oid] = []
        # 避免重复添加同一个 task_id
        if not any(t.task_id == task.task_id for t in self._auto_print_queue[oid]):
            self._auto_print_queue[oid].append(task)
            self._log(f"⚡ 无障碍任务 #{task.task_id} 已加入队列（订单 #{oid}，"
                      f"共 {len(self._auto_print_queue[oid])} 个文件）")

        # 停止旧定时器，重新启动 3 秒防抖（同一订单的后续文件会重置计时）
        old_timer = self._auto_print_timers.pop(oid, None)
        if old_timer:
            old_timer.stop()
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda o=oid: self._process_auto_print_order(o))
        timer.start(3000)
        self._auto_print_timers[oid] = timer

    def _process_auto_print_order(self, order_id: int):
        """防抖定时器触发：收集该订单全部就绪任务，创建标签页并自动开始打印。"""
        tasks = self._auto_print_queue.pop(order_id, [])
        self._auto_print_timers.pop(order_id, None)
        if not tasks:
            return

        # 过滤掉未就绪的任务
        ready_tasks = [t for t in tasks if t.status == "ready"]
        if not ready_tasks:
            self._log(f"⚡ 订单 #{order_id}: 无就绪文件，跳过自动打印")
            return

        self._log(f"⚡ 无障碍打印：订单 #{order_id} 共 {len(ready_tasks)} 个文件，自动创建标签页并开始打印")

        # 标记为已处理（防重复）
        for t in ready_tasks:
            self._mark_processed_task(t.task_id)

        # 自动创建标签页
        self._add_cloud_tasks_to_new_tab(ready_tasks)

        # 通知后端已接受
        if ready_tasks[0].order_id and self._cloud_client:
            self._cloud_client.accept_order_to_server(ready_tasks[0].order_id)

        # 自动开始打印（打印机忙时不静默丢弃 → 入重试队列，打印完成后自动补打）。
        # 只打印未完成（sent=False）的 job：追加到已有标签页时，已打印过的文件不重打
        jobs = [j for j in self._get_current_jobs() if not getattr(j, 'sent', False)]
        if jobs:
            self._log(f"⚡ 自动开始打印标签页 {self._current_tab}（{len(jobs)} 个文件）")
            if not self._start_print_worker(list(jobs)):
                self._auto_print_retry.append({
                    "order_id": order_id,
                    "tab_key": self._current_tab,
                    "task_ids": [t.task_id for t in ready_tasks],
                })
                self._log(f"⚡ 打印机正忙，订单 #{order_id} 已加入重试队列（打印完成后自动补打）")
        else:
            self._log(f"⚠ 无障碍打印：标签页 {self._current_tab} 无文件，跳过")

    # ──────── 无障碍打印预约单（指定时间/倒计时 → 到点自动打印，冻结等待）────────

    @staticmethod
    def _fmt_sched(ts: int) -> str:
        """epoch 秒 → "MM-DD HH:MM:SS" 本地时间（用于预约倒计时日志）。"""
        try:
            from datetime import datetime as _dt
            return _dt.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")
        except Exception:
            return str(ts)

    def _register_scheduled_task(self, task: CloudTask):
        """预约单任务到达：登记到状态机，按订单防抖后评估。"""
        oid = task.order_id
        if not oid:
            self._log(f"⚠ 预约任务 #{task.task_id} 缺少 order_id，跳过")
            return
        st = self._scheduled_orders.setdefault(oid, {
            "target_ts": 0, "ready": {}, "pending": {},
            "timer": None, "print_timer": None,
            "frozen": False, "delayed_sent": False, "printed": False,
            "printed_ts": 0, "tab_key": None, "retry_count": 0,
        })
        if st["printed"]:
            self._log(f"⏰ 预约单 #{oid} 已开始打印，迟到的文件 {task.file_name} 将不再自动打印")
        if task.task_id not in st["pending"] and task.task_id not in st["ready"]:
            st["pending"][task.task_id] = task
        # 注：目标时间取自任务 scheduled_ts（后端下发，本机 epoch 直接可比较）。
        # 本机时钟偏差场景依赖服务端 start_print 到点兜底（_on_cloud_start_print 会重设 target_ts），
        # 若后端后续提供服务器时间偏移接口，可在此换算 target_ts 后再使用。
        ts = getattr(task, "scheduled_ts", 0) or 0
        if ts and (not st["target_ts"] or ts < st["target_ts"]):
            st["target_ts"] = ts
        self._log(f"⏰ 预约单 #{oid}: 收到文件 {task.file_name}"
                  + (f"，目标 {self._fmt_sched(ts)}" if ts else ""))
        self._restart_scheduled_debounce(oid)

    def _update_scheduled_task(self, task: CloudTask):
        """预约单任务状态更新（下载完成/出错）。"""
        oid = task.order_id
        st = self._scheduled_orders.get(oid)
        if not st:
            return
        if task.status == "error":
            self._mark_processed_task(task.task_id)
            st["pending"].pop(task.task_id, None)
            st["ready"].pop(task.task_id, None)
            self._log(f"⏰ 预约单 #{oid} 文件下载失败: {task.error_message}")
            if self._cloud_client:
                self._cloud_client.report_fail(task.task_id, f"下载失败: {task.error_message}")
                self._cloud_client.reject_task(task.task_id)
            self._cloud_tasks.pop(task.task_id, None)
            self._cleanup_scheduled_order(oid, reason="文件下载失败")
            return
        if task.status == "ready":
            st["pending"].pop(task.task_id, None)
            st["ready"][task.task_id] = task
            total = len(st["pending"]) + len(st["ready"])
            self._log(f"⏰ 预约单 #{oid}: 文件 {task.file_name} 已就绪"
                      + (f"（{len(st['ready'])}/{total}）" if st["pending"] else ""))
            if not st["printed"]:
                self._restart_scheduled_debounce(oid)

    def _restart_scheduled_debounce(self, order_id: int):
        """重启预约单 3s 防抖（新任务到达/就绪都会重置，静默后评估）。"""
        st = self._scheduled_orders.get(order_id)
        if not st:
            return
        old = st["timer"]
        if old:
            old.stop()
        t = QTimer(self)
        t.setSingleShot(True)
        t.timeout.connect(lambda o=order_id: self._evaluate_scheduled_order(o))
        t.start(3000)
        st["timer"] = t

    def _evaluate_scheduled_order(self, order_id: int):
        """预约单评估：全部就绪才排入到点打印；到点未齐则冻结等待。"""
        st = self._scheduled_orders.get(order_id)
        if not st or st["printed"]:
            return
        now = int(time.time())
        ready_tasks = list(st["ready"].values())
        pending = st["pending"]

        if pending:
            # 仍有文件未下载完成 → 取消已排的打印，等待；到点则冻结
            if st["print_timer"]:
                st["print_timer"].stop()
                st["print_timer"] = None
            if st["target_ts"] and now >= st["target_ts"] and not st["frozen"] and not st["delayed_sent"]:
                self._freeze_scheduled_order(order_id, pending)
            return

        # 全部就绪
        if not ready_tasks:
            return
        if not st["tab_key"]:
            # 首次全部就绪 → 创建标签页（后续新增文件追加）。
            # 注意：预约单不走 accept_order（会误把父订单标成 accepted），
            # 后端状态由 file_ready → waiting、start_printing → printing 驱动。
            self._log(f"⏰ 预约单 #{order_id}: 全部 {len(ready_tasks)} 个文件就绪")
            for t in ready_tasks:
                self._mark_processed_task(t.task_id)
            self._add_cloud_tasks_to_new_tab(ready_tasks)
            st["tab_key"] = self._current_tab

        if st["frozen"]:
            # 冻结已解除（文件补齐）：在线等后端 start_print 重设目标；断网则 30s 自恢复
            if not self._cloud_connected and not st["print_timer"]:
                self._log(f"⏰ 预约单 #{order_id}: 断网自恢复，30s 后开始打印")
                self._start_scheduled_print_timer(order_id, now + 30)
            return

        # 未冻结 → 排入到点打印倒计时
        target = st["target_ts"] or now
        self._start_scheduled_print_timer(order_id, target)

    def _freeze_scheduled_order(self, order_id: int, pending: dict):
        """到点文件未就绪 → 本地冻结 + 上报后端（冻结等待，文件齐后继续）。"""
        st = self._scheduled_orders.get(order_id)
        if not st:
            return
        st["frozen"] = True
        st["delayed_sent"] = True
        self._log(f"⏰ 预约单 #{order_id}: 到点仍有 {len(pending)} 个文件未就绪，冻结等待")
        if self._cloud_client:
            self._cloud_client.report_download_delayed(order_id, list(pending.keys()))

    def _check_scheduled_timeouts(self):
        """每秒检查：
        - 预约单到点仍有文件未下载完 → 冻结上报（不必等下载完成事件）
        - 已打印完成的预约单保留 1 小时后清理（防迟到文件二次打印期间需保留状态）"""
        now = int(time.time())
        for order_id, st in list(self._scheduled_orders.items()):
            if st["printed"]:
                # 打印完成已超 1 小时 → 清理（迟到文件窗口已过）
                if st["printed_ts"] and now - st["printed_ts"] > 3600:
                    self._cleanup_scheduled_order(order_id)
                continue
            if not st["pending"]:
                continue
            if st["target_ts"] and now >= st["target_ts"] and not st["frozen"] and not st["delayed_sent"]:
                self._freeze_scheduled_order(order_id, st["pending"])

    def _start_scheduled_print_timer(self, order_id: int, target_ts: int):
        """在 target_ts（已过则立即）触发预约单打印。"""
        st = self._scheduled_orders.get(order_id)
        if not st or st["printed"]:
            return
        old = st["print_timer"]
        if old:
            old.stop()
        now = int(time.time())
        delay = max(0, target_ts - now)
        t = QTimer(self)
        t.setSingleShot(True)
        t.timeout.connect(lambda o=order_id: self._fire_scheduled_print(o))
        if delay > 0:
            t.start(delay * 1000)
            self._log(f"⏰ 预约单 #{order_id}: 已排入 {self._fmt_sched(target_ts)} 自动打印")
        else:
            t.start(0)
            self._log(f"⏰ 预约单 #{order_id}: 目标时间已过，立即打印")
        st["print_timer"] = t

    def _fire_scheduled_print(self, order_id: int):
        """预约单到点：确认文件全部就绪后开始打印。打印机正忙则 10s 后重试，保证打出来。"""
        st = self._scheduled_orders.get(order_id)
        if not st or st["printed"]:
            return
        if st["pending"]:
            # 到点仍有文件未就绪 → 冻结（正常情况下不会走到：就绪后才排入倒计时）
            if not st["delayed_sent"]:
                self._freeze_scheduled_order(order_id, st["pending"])
            return
        ready_tasks = list(st["ready"].values())
        if not ready_tasks:
            self._cleanup_scheduled_order(order_id, reason="无就绪文件")
            return

        # 确保标签页存在
        if not st["tab_key"]:
            for t in ready_tasks:
                self._mark_processed_task(t.task_id)
            self._add_cloud_tasks_to_new_tab(ready_tasks)
            st["tab_key"] = self._current_tab
        else:
            self._current_tab = st["tab_key"]

        # 打印机正忙 → 10s 后重试（最多 60 次 ≈ 10 分钟），保证预约单不丢
        if self._worker and self._worker.isRunning():
            retry = st.get("retry_count", 0) + 1
            st["retry_count"] = retry
            if retry > 60:
                # 重试超限：上报后端失败并清理状态机（不静默丢弃）
                self._log(f"⚠ 预约单 #{order_id}: 打印机持续忙碌，放弃自动打印，请手动在标签页打印")
                if self._cloud_client:
                    for t in ready_tasks:
                        self._cloud_client.report_fail(t.task_id, "打印机持续忙碌，预约打印重试超限")
                self._cleanup_scheduled_order(order_id, reason="打印机持续忙碌，重试超限")
                return
            self._log(f"⏰ 预约单 #{order_id}: 打印机正忙，10s 后重试（第 {retry} 次）")
            old = st["print_timer"]
            if old:
                old.stop()
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(lambda o=order_id: self._fire_scheduled_print(o))
            t.start(10000)
            st["print_timer"] = t
            return

        self._log(f"⚡ 无障碍预约打印：订单 #{order_id} 共 {len(ready_tasks)} 个文件，开始打印")
        jobs = self._get_current_jobs()
        if jobs:
            if not self._start_print_worker(list(jobs)):
                # 恰好此刻打印机变忙 → 不置 printed，10s 后重试（防丢失）
                self._log(f"⏰ 预约单 #{order_id}: 打印机正忙，10s 后重试")
                self._start_scheduled_print_timer(order_id, int(time.time()) + 10)
                return
            # 确认打印已启动后才置 printed（防假打印：忙时/空标签页不再标记成功）
            st["printed"] = True
            st["printed_ts"] = int(time.time())
            if self._cloud_client:
                self._cloud_client.report_start_printing(order_id, [t.task_id for t in ready_tasks])
        else:
            # 标签页无文件（可能被清空/删除）→ 不置 printed、不上报，重置为待重试
            self._log(f"⚠ 无障碍预约打印：标签页 {self._current_tab} 无文件，订单 #{order_id} 保持待重试")
            st["retry_count"] = 0
            self._start_scheduled_print_timer(order_id, int(time.time()) + 10)

    def _on_cloud_start_print(self, order_id: int, scheduled_ts: int, task_ids: list):
        """后端 start_print：到点兜底 / 冻结解除后重设目标。本地已自触发则幂等跳过。"""
        if not order_id:
            return
        st = self._scheduled_orders.get(order_id)
        if not st:
            return
        if st["printed"]:
            return
        if scheduled_ts:
            st["target_ts"] = scheduled_ts
        st["frozen"] = False
        if st["pending"]:
            # 仍有文件未就绪 → 继续等待（全部就绪后重新评估）
            self._restart_scheduled_debounce(order_id)
        else:
            self._evaluate_scheduled_order(order_id)

    def _cleanup_scheduled_order(self, order_id: int, reason: str = ""):
        """清理预约单状态机（失败/取消/超时未使用）。已打印的订单保留状态防迟到文件二次打印。"""
        st = self._scheduled_orders.pop(order_id, None)
        if not st:
            return
        for t in (st["timer"], st["print_timer"]):
            if t:
                t.stop()
        if reason:
            self._log(f"⏰ 预约单 #{order_id} 已结束: {reason}")

    def _cleanup_scheduled_orders_for_tab(self, tab_key: str, reason: str = "标签页已清空"):
        """清空/删除标签页时，联动清理指向该标签页的预约单状态机条目。"""
        for oid, st in list(self._scheduled_orders.items()):
            if st.get("tab_key") == tab_key:
                self._cleanup_scheduled_order(oid, reason=reason)

    def _on_cloud_order_canceled(self, order_id: int, task_ids: list):
        """云端订单被用户取消 → 通知任务列表窗口更新状态，若正在打印则立即取消。"""
        self._log(f"☁ 订单 #{order_id} 已被用户取消")
        if self._cloud_task_window:
            self._cloud_task_window.mark_canceled(order_id, task_ids)
        # 预约单被取消 → 清理预约状态机（停掉到点倒计时）
        if order_id in self._scheduled_orders:
            self._cleanup_scheduled_order(order_id, reason="已被用户取消")

        # 检查当前打印的任务是否包含被取消的 task_id → 立即终止打印
        cancel_current_print = False
        if self._worker and self._worker.isRunning():
            for job in self._worker._jobs:
                if job.task_id in task_ids or job.order_id == order_id:
                    cancel_current_print = True
                    break
        if cancel_current_print:
            self._log(f"☁ 订单 #{order_id} 正在打印，已自动取消")
            self._worker.cancel()

        # 如果已添加到标签页中，弹出提示（多任务合并为一次，避免弹窗堆叠）
        cancel_hits = []
        for key, tab in list(self._config.tabs.items()):
            for job in tab.jobs:
                if job.task_id in task_ids:
                    cancel_hits.append(f"标签页 {key}：「{job.display_name or os.path.basename(job.file_path)}」")
        if cancel_hits:
            shown = cancel_hits[:5]
            more = f"\n...等共 {len(cancel_hits)} 个任务" if len(cancel_hits) > 5 else ""
            QMessageBox.information(
                self, "任务已取消",
                "以下任务已被用户取消：\n" + "\n".join(shown) + more + "\n建议删除对应标签页。",
            )

    def _on_cloud_order_accepted(self, tasks: list):
        """用户从云端任务列表窗口确认添加订单中的全部任务到同一个新标签页。"""
        if not tasks:
            return
        if not self._has_members():
            # 云端订单同样需要归属成员（谁打印的），无成员时禁止接收
            self._show_no_member_hint()
            return
        for task in tasks:
            self._mark_processed_task(task.task_id)
        self._add_cloud_tasks_to_new_tab(tasks)
        # 通知后端：订单已接受
        if tasks[0].order_id and self._cloud_client:
            self._cloud_client.accept_order_to_server(tasks[0].order_id)

    def _on_cloud_order_rejected(self, tasks: list):
        """用户从云端任务列表窗口打回订单中的任务。"""
        for task in tasks:
            self._mark_processed_task(task.task_id)
        if tasks and tasks[0].order_id and self._cloud_client:
            self._cloud_client.reject_order_to_server(tasks[0].order_id)
        for task in tasks:
            self._cloud_tasks.pop(task.task_id, None)

    def _on_cloud_connection_changed(self, connected: bool):
        """云端连接状态改变。连线后的网络同步放到后台线程执行，避免阻塞 UI（主线程）。"""
        self._cloud_connected = connected
        self._update_cloud_status()
        if connected:
            threading.Thread(
                target=self._conn_sync_worker,
                daemon=True,
                name="cloud-conn-sync",
            ).start()

    def _conn_sync_worker(self):
        """后台线程：连线后同步（成员名单 / 离线订单换号 / 状态 / 离线上报）。
        这些均为同步网络请求，放后台避免 UI 卡死；日志与 UI 刷新经信号回主线程。
        成员名单拉取放在最前，尽早更新归属下拉（占位名可尽快被真实名单替换），
        不被耗时的离线订单换号拖在后面。"""
        try:
            # ① 成员名单：最先拉取+写配置（后台），UI 刷新经信号回主线程。
            #    放最前：让归属下拉尽快替换掉占位名（张三/李四），不等慢的订单换号。
            try:
                self._refresh_owner_names_from_cloud_net()
                self._ownerComboRefreshed.emit()   # 拉完立即刷新下拉
            except Exception as e:
                logger.debug(f"同步云端成员名单失败: {e}")
            # ② 逐个 -L 离线订单换号（逐单同步 requests，最耗时，放最后）
            try:
                self._sync_local_orders_to_cloud()
            except Exception as e:
                logger.warning(f"同步本地订单号失败: {e}")
            if self._cloud_client:
                try:
                    self._cloud_client.sync_pending_statuses()
                except Exception as e:
                    logger.warning(f"云端状态同步失败: {e}")
            # 离线订单上报（同步网络）
            if self._offline_sync and self._cloud_client:
                try:
                    count = self._offline_sync.sync_all_pending_orders(
                        server_url=self._cloud_client.api_url,
                        token=self._cloud_client.token,
                    )
                    if count > 0:
                        self._cloudConnSyncLog.emit(f"📋 离线订单已同步: {count} 个任务已上报云端")
                except Exception as e:
                    self._cloudConnSyncLog.emit(f"⚠️ 离线订单同步失败: {e}")
            # ③ 全部订单已上云 → 清空本地订单库副本（数据改为云端储存；重试耗尽的行保留防丢）
            if self._offline_sync:
                try:
                    if self._offline_sync.count_unsynced() == 0:
                        cleared = self._offline_sync.clear_all()
                        if cleared > 0:
                            self._cloudConnSyncLog.emit(f"📋 本地订单已全部上云，清空本地 {cleared} 条订单记录")
                except Exception as e:
                    logger.debug(f"清理本地订单库失败: {e}")
        finally:
            # 成员归属下拉刷新回主线程执行（幂等：已刷过一次也无妨）
            self._ownerComboRefreshed.emit()

    def _on_owner_combo_refreshed(self):
        """（主线程）后台成员名单同步完成后，刷新归属下拉。"""
        if hasattr(self, '_owner_combo') and self._owner_combo:
            try:
                self._update_owner_combo_items()
            except Exception:
                pass

    def _on_refresh_tab_display_safe(self):
        """（主线程）后台同步换号完成后，刷新标签页显示（必须回主线程操作控件）。"""
        try:
            self._refresh_tab_display()
        except Exception:
            pass

    def _on_cloud_status_message(self, msg: str):
        """云端日志消息 → 写入界面日志。"""
        self._log(msg)

    def _on_cloud_auth_failed(self, msg: str):
        """云端认证失败 → 日志 + 弹窗提示（自动重试中，超过上限后停止）。"""
        self._log(f"🚫 云端认证失败：{msg}")
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(
            self,
            "云端认证失败",
            f"{msg}\n\n工具将自动重试连接。若持续失败，请检查云端令牌（cloud_token）是否已更新。",
        )

    def _on_cloud_pull(self):
        """手动拉取云端排队任务。"""
        if self._cloud_client:
            self._cloud_client.pull_pending()
            self._log("☁ 已手动请求拉取云端排队任务")

    # ---- 关闭事件 ----

    def closeEvent(self, event):
        """关闭窗口时检查未完成任务，确认后保存并退出。"""
        # 关闭云端任务列表窗口
        if self._cloud_task_window and self._cloud_task_window.isVisible():
            self._cloud_task_window.close()

        # 打印进行中 → 询问用户：等待完成 / 取消打印 / 继续打印
        if self._worker is not None and self._worker.isRunning():
            reply = QMessageBox.question(
                self, "打印正在进行",
                "当前仍有打印任务正在进行。\n\n"
                "「是」= 等待打印完成后退出\n"
                "「否」= 取消打印并退出\n"
                "「取消」= 继续打印，不退出",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Cancel:
                event.ignore()
                return
            if reply == QMessageBox.No:
                # 取消打印并退出：置取消标志 + 等待线程结束（5s 超时后 terminate 兜底）
                self._log("🛑 用户选择取消打印并退出")
                self._worker.cancel()
                try:
                    self._worker.all_finished.disconnect(self._on_all_finished)
                except Exception:
                    pass
                if not self._worker.wait(5000):
                    self._worker.terminate()
                    self._worker.wait(100)
                self._worker = None
            else:
                # 等待打印完成后再退出（轮询 + 处理事件，界面保持响应）
                self._log("⏳ 正在等待打印完成，完成后自动退出...")
                while self._worker is not None and self._worker.isRunning():
                    QApplication.processEvents()
                    time.sleep(0.05)

        # 检查所有标签页是否有未完成的文件
        total_files = 0
        for key, tab in self._config.tabs.items():
            total_files += len(tab.jobs)

        if total_files > 0:
            # 全部订单是否均已打印完成（每个 job 均 sent=True）
            all_completed = all(
                getattr(job, 'sent', False)
                for tab in self._config.tabs.values()
                for job in tab.jobs
            )
            if all_completed:
                # 全部订单已完成 → 不阻挡关闭、无需确认，直接自动清理（含 PDF 缓存）
                self._log("🗑 所有订单均已打印完成，退出时自动清理已完成订单")
                cache_jobs = []
                for tab in self._config.tabs.values():
                    cache_jobs.extend(tab.jobs)
                # 已清空但撤回窗口未过的备份任务：即使主标签页全部完成，未打印的云端任务仍需放弃
                for backup_jobs in self._cleared_jobs_backup.values():
                    for job in backup_jobs:
                        if getattr(job, 'sent', False):
                            continue
                        if job.order_id > 0 and self._cloud_client:
                            self._cloud_client.abandon_order_to_server(job.order_id)
                        elif job.task_id > 0 and self._cloud_client:
                            self._cloud_client.abandon_order_to_server(job.task_id)
                self._config.tabs = {"1": TabSettings()}
                self._config.active_tab = "1"
                self._current_tab = "1"
                self._cleanup_orphan_pdf_cache(cache_jobs)
            else:
                reply = QMessageBox.question(
                    self, "存在未完成的任务",
                    f"当前共有 {total_files} 个文件分布在 {len(self._config.tabs)} 个标签页中尚未打印。\n\n"
                    "退出将清空全部标签页，确定退出吗？",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    event.ignore()
                    return
                # 用户确认退出 → 通知后端放弃未打印的云端任务 + 本地预留订单，然后清空
                # （已打印完成的 job 跳过，避免误放弃已完成订单）
                all_abandoned = []
                for key, tab in self._config.tabs.items():
                    for job in tab.jobs:
                        if getattr(job, 'sent', False):
                            continue  # 已打印完成，不放弃
                        all_abandoned.append(job)
                        if job.order_id > 0 and self._cloud_client:
                            self._cloud_client.abandon_order_to_server(job.order_id)
                        elif job.task_id > 0 and self._cloud_client:
                            self._cloud_client.abandon_order_to_server(job.task_id)
                        elif job.order_number and "-L" not in job.order_number and self._cloud_client:
                            # 本地预留订单（仅获取了订单号但未提交打印）→ 标记为放弃，并补记价格
                            price, _ = calc_cost(job.page_count, job.copies, job.duplex, page_range=job.page_range or "")
                            self._cloud_client.abandon_reserved_order(job.order_number, price)
                # 已清空但撤回窗口未过的备份任务同样放弃（用户确认退出=确认清空）
                for backup_jobs in self._cleared_jobs_backup.values():
                    for job in backup_jobs:
                        if getattr(job, 'sent', False):
                            continue
                        all_abandoned.append(job)
                        if job.order_id > 0 and self._cloud_client:
                            self._cloud_client.abandon_order_to_server(job.order_id)
                        elif job.task_id > 0 and self._cloud_client:
                            self._cloud_client.abandon_order_to_server(job.task_id)
                self._config.tabs = {"1": TabSettings()}
                self._config.active_tab = "1"
                self._current_tab = "1"
                self._cleanup_orphan_pdf_cache(all_abandoned)

        try:
            # 删除所有空标签页并重新编号（确保下次启动从 1 开始）
            self._cleanup_empty_tabs()
            self._renumber_tabs()
            self._sync_ui_to_config()
            self._config.save(self._config_path)
            self._log(f"配置已自动保存至: {self._config_path}")
        except Exception as e:
            logger.warning(f"自动保存配置失败: {e}")

        # 断开云端连接
        if self._cloud_client:
            self._cloud_client.stop()

        # 停止收支清算统计服务器
        if self._stats_server:
            self._stats_server.stop()

        super().closeEvent(event)


    # ──────── 复制详情 ────────

    def _on_copy_detail(self):
        """复制当前标签页的计费明细到剪贴板（含订单号）。无成员时禁止创建订单。"""
        if not self._has_members():
            self._show_no_member_hint()
            return
        jobs = self._get_current_jobs()
        if not jobs:
            return
        order_number = self._ensure_order_number()
        lines = []
        if order_number:
            lines.append(f"订单号: {order_number}")
            lines.append("-" * 14)
        lines.append("计费明细")
        lines.append("-" * 14)
        for i, job in enumerate(jobs, 1):
            cost, formula = calc_cost(job.page_count, job.copies, job.duplex,
                                       self._config.simplex_price, self._config.duplex_price,
                                       job.page_range)
            display = job.display_name or os.path.basename(job.file_path)
            lines.append(f"{i}. {display}")
            lines.append(f"   {job.copies}x {'双面' if job.duplex=='on' else '单面'} 范围:{job.page_range or '全部'}")
            if cost > 0:
                lines.append(f"   {formula} = ¥{cost:.2f}")
        total = sum(calc_cost(j.page_count, j.copies, j.duplex,
                              self._config.simplex_price, self._config.duplex_price,
                              j.page_range)[0] for j in jobs)
        # 与界面合计（_update_total_cost）口径一致：附加服务费（派送/加急/首页）
        tab = self._config.tabs.get(self._current_tab)
        extra = tab.calc_extra_total(total, self._config) if tab else 0.0
        if extra > 0:
            lines.append(f"附加服务费: ¥{extra:.2f}")
        lines.append(f"合计: ¥{total + extra:.2f}")
        QApplication.clipboard().setText("\n".join(lines))
        if self._copy_detail_btn:
            self._copy_detail_btn.setText("✅ 已复制")
            self._copy_detail_btn.setEnabled(False)
        if self._copy_total_timer:
            self._copy_total_timer.start(5000)

    # ──────── 转换完成回调 ────────

    def _on_convert_finished(self, row: int, file_path: str, cached_pdf: str, page_count: int, orientation: str):
        """后台 PDF 转换完成 → 按 file_path 匹配行并更新表格、缓存。

        多文件订单并发转换时行号可能错位，统一按 file_path 找行：
        row 处恰好匹配直接回写，否则扫描全部 job；找不到（文件已被删除）→ 丢弃结果只清理旧缓存。
        """
        # 按 file_path 匹配行
        jobs = self._get_current_jobs()
        target_row = None
        if row < len(jobs) and jobs[row].file_path == file_path:
            target_row = row
        else:
            for i, j in enumerate(jobs):
                if j.file_path == file_path:
                    target_row = i
                    break
        if target_row is None:
            # 文件已被删除 → 丢弃转换结果，仅清理产生的缓存文件
            if cached_pdf and os.path.isfile(cached_pdf):
                try:
                    os.remove(cached_pdf)
                except OSError:
                    pass
            return
        if not cached_pdf:
            if target_row < self._table.rowCount():
                self._table.item(target_row, self.COL_PAGES).setText("?")
            return

        jobs = self._get_current_jobs()
        old_pdf = jobs[target_row].cached_pdf
        if old_pdf and os.path.isfile(old_pdf) and old_pdf != cached_pdf:
            try:
                os.remove(old_pdf)
            except OSError:
                pass
        jobs[target_row].cached_pdf = cached_pdf
        jobs[target_row].page_count = page_count
        jobs[target_row].orientation = orientation
        self._set_current_jobs(jobs)

        if target_row < self._table.rowCount():
            self._table.item(target_row, self.COL_PAGES).setText(str(page_count))
            ori_map = {"portrait": "竖", "landscape": "横", "mixed": "混"}
            ori_text = ori_map.get(orientation, "")
            self._table.item(target_row, self.COL_ORIENT).setText(ori_text)
        self._recalc_row_cost(target_row)
        self._update_total_cost()

        # 转换完成后存入 MD5 缓存（供后续同文件复用，避免重复转换）
        if cached_pdf and os.path.isfile(cached_pdf) and target_row < len(jobs):
            job = jobs[target_row]
            # 如果没有 source_md5，从源文件计算
            if not job.source_md5 and job.file_path and os.path.isfile(job.file_path):
                try:
                    job.source_md5 = self._cloud_client._compute_md5_file(job.file_path) if self._cloud_client else ""
                except Exception:
                    pass
            # 存入 MD5 缓存索引
            if job.source_md5:
                if self._cloud_client:
                    try:
                        self._cloud_client._save_pdf_to_cache(
                            job.source_md5, cached_pdf,
                            job.display_name or os.path.basename(job.file_path),
                            os.path.splitext(job.file_path)[1].lower(),
                            page_count,
                            getattr(job, 'image_orientation', 'auto'),
                        )
                    except Exception as e:
                        self._log(f"  → MD5 缓存保存失败: {e}")
                else:
                    # 离线：直接更新 pdf_cache/index.json
                    try:
                        import json as _json
                        cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdf_cache")
                        idx_path = os.path.join(cache_dir, "index.json")
                        idx = {}
                        if os.path.exists(idx_path):
                            with open(idx_path, "r", encoding="utf-8") as _f:
                                idx = _json.load(_f)
                        idx[job.source_md5] = {
                            "original_name": job.display_name or os.path.basename(job.file_path),
                            "source_ext": os.path.splitext(job.file_path)[1].lower(),
                            "page_count": page_count,
                            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        }
                        with open(idx_path, "w", encoding="utf-8") as _f:
                            _json.dump(idx, _f, ensure_ascii=False, indent=2)
                    except Exception as e:
                        self._log(f"  → 离线缓存保存失败: {e}")
                self._set_current_jobs(jobs)  # 保存 source_md5 到配置

        # 转换完成后立刻保存副本到桌面
        if self._config.keep_temp_pdf and cached_pdf and os.path.isfile(cached_pdf):
            file_path = jobs[target_row].file_path if target_row < len(jobs) else ""
            original_base = os.path.splitext(os.path.basename(file_path))[0] if file_path else "document"
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            dest_name = f"[转换]{original_base}.pdf"
            dest_path = os.path.join(desktop, dest_name)
            try:
                shutil.copy2(cached_pdf, dest_path)
                self._log(f"  → 转换副本已保存到桌面: {dest_name}")
            except OSError as e:
                self._log(f"  → 保存转换副本到桌面失败: {e}")

    def _add_cloud_tasks_to_new_tab(self, tasks: list):
        """将同一订单的多个云端任务添加到同一个新标签页，全部加入后再刷新显示。"""
        if not tasks:
            return
        for task in tasks:
            self._add_cloud_task_to_new_tab(task, is_first=(task == tasks[0]))
        # 全部任务加入后再刷新表格/显示（而非只对第一个刷新）→ 确保所有文件可见
        self._save_config()
        self._rebuild_table()
        self._refresh_tab_display()
        self._sync_edit_enabled(False)

    def _find_tab_for_order(self, order_id) -> str | None:
        """查找已包含指定云端订单任务的标签页 key；无则返回 None。
        用于多文件订单：即使文件下载完成时间分散（超过防抖窗口），也追加到同一标签页。"""
        if not order_id:
            return None
        for key, tab in self._config.tabs.items():
            if any(getattr(j, 'order_id', 0) == order_id for j in tab.jobs):
                return key
        return None

    def _add_cloud_task_to_new_tab(self, task: CloudTask, is_first: bool = True):
        """将云端任务添加到标签页并切换过去。同一订单已有标签页 → 追加到该标签页（一个订单一个标签页）。"""
        # 同一订单已有标签页 → 追加到它，而不是新建（防下载完成时间分散导致拆成多个标签页）
        existing_key = self._find_tab_for_order(task.order_id)
        if existing_key is not None:
            self._current_tab = existing_key
            self._config.active_tab = existing_key
            is_first = False
        if is_first:
            tab_keys = self._sorted_tab_keys(self._config.tabs)
            last_num = self._safe_int_key(tab_keys[-1]) if tab_keys else 0
            new_key = str(last_num + 1)
            # 用前端订单的附加服务覆盖默认值
            tab_settings = TabSettings()
            tab_settings.delivery_enabled = task.delivery_enabled
            tab_settings.delivery_location = task.delivery_location
            tab_settings.urgency = task.urgency
            tab_settings.cover_page = task.cover_page
            tab_settings.cover_page_price = task.cover_page_price
            # v24.1：云端订单若由管理员在前端标记"管理员自行打印"→ 预勾选并沿用归属人；
            # 否则为顾客订单（不是管理员自行打印），归属人默认当前机位管理员
            tab_settings.is_admin_print = bool(getattr(task, 'is_admin_print', False))
            task_owner = (getattr(task, 'owner_name', '') or '').strip()
            if task_owner:
                tab_settings.owner_name = task_owner
            self._config.tabs[new_key] = tab_settings
            self._current_tab = new_key
            self._config.active_tab = new_key
        else:
            new_key = self._current_tab

        ext = os.path.splitext(task.local_path)[1].lower() if task.local_path else ""
        image_exts = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
        # 仅使用任务自带的 source_md5（后端 MD5 索引提供），不再主线程计算大文件 MD5（防 UI 冻结）；
        # 缺失时由 _start_convert_worker 在转换流程中计算
        source_md5 = getattr(task, 'source_md5', '') or ''

        page_count = 0; orientation = ""; cached_pdf = ""
        need_convert = False

        # 1. 检查 PDF 缓存（MD5 索引，图片按方向后缀分开）
        if source_md5 and self._cloud_client:
            cached_pdf, cached_meta = self._cloud_client._get_cached_pdf(
                source_md5, getattr(task, 'image_orientation', 'auto'))
            if cached_pdf and cached_meta:
                page_count = cached_meta.get("page_count", 0)
                orientation = ""  # 缓存里可能没有 orientation，从 PDF 读取
                if page_count > 0:
                    from pdf_printer import get_pdf_info as _gpi
                    _info = _gpi(cached_pdf)
                    page_count = _info.get("page_count", page_count)
                    orientation = _info.get("orientation", "")
                self._log(f"📦 缓存命中: {task.file_name} → {page_count} 页 (MD5={source_md5[:8]}...)")
                cached_pdf = cached_pdf  # 直接使用缓存的 PDF

        # 2. 未命中缓存 → 从本地文件获取信息
        if page_count <= 0 and task.local_path and os.path.isfile(task.local_path):
            if ext == ".pdf":
                info = get_pdf_info(task.local_path); page_count = info["page_count"]; orientation = info["orientation"]
            elif ext in image_exts:
                info = get_image_info(task.local_path); page_count = info["page_count"]; orientation = info["orientation"]
            elif ext == ".docx":
                orientation = get_docx_orientation(task.local_path)

        engine = "word"
        if ext in (".doc", ".docx") and task.local_path:
            from converter import _read_docx_last_editor, get_available_engines
            available = get_available_engines()
            editor = _read_docx_last_editor(task.local_path) if ext == ".docx" else None
            preferred = "wps" if editor == "wps" else "word"
            for eng in ([preferred] + [e for e in ["word","wps","libreoffice"] if e != preferred]):
                if available.get(eng, False): engine = eng; break

        is_image = ext in image_exts
        duplex_mode = "short-edge" if orientation == "landscape" else "long-edge"

        job = PrintJob(
            file_path=task.local_path or "",
            copies=task.copies if task.copies > 0 else 1,
            duplex="off" if is_image else (task.duplex or "on"),
            duplex_mode=duplex_mode,
            page_range=task.page_range or "",
            page_count=page_count,
            orientation=orientation,
            image_orientation=getattr(task, 'image_orientation', 'auto'),
            engine=engine,
            task_id=task.task_id,
            order_id=task.order_id or 0,
            source_md5=source_md5,
            display_name=task.file_name,  # 使用后端返回的原始文件名
            order_number=task.order_number,  # 云端订单号
            cached_pdf=cached_pdf,        # 使用缓存的 PDF（如有）
        )
        self._config.tabs[new_key].jobs.append(job)

        # Word 文件：缓存未命中时才启动转换（传真实行号，job 已 append；
        # 回调改为按 file_path 匹配，行号仅作快速路径提示）
        if ext in (".doc", ".docx") and task.local_path and not cached_pdf:
            row = len(self._config.tabs[new_key].jobs) - 1
            self._start_convert_worker(row, task.local_path, engine)

        self._log(f"☁ 云端任务 #{task.task_id} 已添加到标签页 {new_key}")
        if self._cloud_client:
            self._cloud_client.accept_task(task.task_id)

    # ──────── 清空 / 撤回 / 移除 / 打印 ────────

    def _on_clear_list(self):
        """清空当前标签页 / 撤回清空（按钮双功能）。"""
        # 如果已经在撤回模式，则执行撤回
        if self._clear_undo_timer.isActive():
            self._on_undo_clear()
            return

        if self._is_current_tab_frozen():
            self._log("🔒 该标签页已固定，不可清空")
            return

        jobs = self._get_current_jobs()
        if not jobs:
            return
        self._cancel_all_convert_workers()
        # 不立即通知后端放弃：等撤回窗口(5s)过期或确认后才放弃，
        # 避免用户在 5 秒内点"撤回"后订单已被后端标记为放弃
        self._cleared_jobs_backup[self._current_tab] = list(jobs)
        self._set_current_jobs([])
        # 预约单指向该标签页 → 联动清理状态机（防到点打印空标签页/假打印）
        self._cleanup_scheduled_orders_for_tab(self._current_tab, reason="标签页已清空")
        self._rebuild_table()
        self._update_total_cost()
        self._refresh_tab_display()
        self._sync_edit_enabled(False)
        self._btn_clear.setText("↩ 撤回清空")
        self._btn_clear.setObjectName("btnUndo")
        self._btn_clear.style().unpolish(self._btn_clear)
        self._btn_clear.style().polish(self._btn_clear)
        self._clear_undo_timer.start(5000)
        self._log(f"已清空标签页 {self._current_tab}（5秒内可撤回）")

    def _on_undo_clear(self):
        """撤回清空操作（按钮点击）。"""
        # 先取备份，再取消定时器（cancel 也会 pop，所以先取）
        backup = self._cleared_jobs_backup.pop(self._current_tab, None)
        self._cancel_undo_if_active()
        if backup:
            self._set_current_jobs(backup)
            self._rebuild_table()
            self._update_total_cost()
            self._refresh_tab_display()
            self._log(f"已撤回标签页 {self._current_tab} 的清空操作")
        else:
            self._log("没有可撤回的清空操作")

    def _on_remove_selected(self):
        """移除表格中选中的行。"""
        if self._is_current_tab_frozen():
            self._log("🔒 该标签页已固定，不可移除文件")
            return
        rows = sorted(set(idx.row() for idx in self._table.selectedIndexes()), reverse=True)
        if not rows:
            return
        jobs = self._get_current_jobs()
        # 通知后端放弃被移除的云端任务（已完成的跳过）
        for row in rows:
            if row < len(jobs):
                job = jobs[row]
                if getattr(job, 'sent', False):
                    continue
                if job.order_id > 0 and self._cloud_client:
                    self._cloud_client.abandon_order_to_server(job.order_id)
                elif job.task_id > 0 and self._cloud_client:
                    self._cloud_client.abandon_order_to_server(job.task_id)
        # 取消被移除文件的转换 worker（防完成后回调写回已删除行/残留缓存）
        for row in rows:
            if row < len(jobs):
                self._cancel_convert_worker_for_path(jobs[row].file_path)
        for row in rows:
            if row < len(jobs):
                self._cancel_undo_if_active()
                del jobs[row]
        self._set_current_jobs(jobs)
        self._rebuild_table()
        self._update_total_cost()
        self._refresh_tab_display()
        self._sync_edit_enabled(False)

    def _on_start_print(self):
        """开始打印当前标签页的所有文件。"""
        if self._is_current_tab_frozen():
            self._log("🔒 该标签页已固定，不能重复打印")
            return
        jobs = self._get_current_jobs()
        if not jobs:
            self._log("当前标签页没有文件可以打印")
            return
        self._start_print_worker(list(jobs))

    def _refresh_config_jobs_from_table(self):
        """从表格同步任务列表到配置（已通过实时保存自动处理）。"""
        pass

    # ──────── 添加文件核心实现 ────────

    def __add_files_to_table_impl(self, files, target_order_key=None):
        """添加文件到当前标签页的核心逻辑。"""
        # HTML/HTM 已移除；表格(xls/xlsx)在前端标记为不支持类型，不再本地打印
        allowed_types = {
            ".pdf", ".doc", ".docx",
            ".txt", ".md",
            ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp",
        }
        image_exts = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}

        self._cancel_undo_if_active()
        jobs = self._get_current_jobs()
        rows_before = len(jobs)
        existing_paths = {j.file_path for j in jobs}

        for f in files:
            # 跳过已存在的文件
            if f in existing_paths:
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext not in allowed_types:
                continue
            existing_paths.add(f)
            page_count = 0; orientation = ""
            if ext == ".pdf":
                info = get_pdf_info(f); page_count = info["page_count"]; orientation = info["orientation"]
            elif ext == ".docx":
                orientation = get_docx_orientation(f)
            elif ext in image_exts:
                info = get_image_info(f); page_count = info["page_count"]; orientation = info["orientation"]
            engine = "word"
            if ext in (".doc", ".docx"):
                from converter import _read_docx_last_editor, get_available_engines
                available = get_available_engines()
                editor = _read_docx_last_editor(f) if ext == ".docx" else None
                preferred = "wps" if editor == "wps" else "word"
                for eng in ([preferred] + [e for e in ["word","wps","libreoffice"] if e != preferred]):
                    if available.get(eng, False): engine = eng; break
            is_image = ext in image_exts
            duplex_mode = "short-edge" if orientation == "landscape" else "long-edge"

            job = PrintJob(
                file_path=f, copies=1,
                duplex="off" if is_image else "on",
                duplex_mode=duplex_mode,
                page_count=page_count, orientation=orientation, engine=engine,
            )
            jobs.append(job)

        if len(jobs) > rows_before:
            self._set_current_jobs(jobs)
            self._rebuild_table()
            self._refresh_tab_display()
            self._log(f"标签页 {self._current_tab}: 已添加 {len(jobs) - rows_before} 个文件")
            for i, job in enumerate(jobs):
                ext = os.path.splitext(job.file_path)[1].lower()
                if ext != ".pdf":
                    # 所有非 PDF 文件都在添加时预转换为 PDF 并缓存（图片/TXT/Word 等）
                    # 这样崩溃恢复后即使源文件不可用，缓存 PDF 仍可用于打印
                    self._start_convert_worker(i, job.file_path, job.engine)
