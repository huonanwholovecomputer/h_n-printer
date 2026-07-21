"""
gui.py — PySide6 主界面 MainWindow + 打印工作线程 PrintWorker
HN 本地打印工具 — 支持浅色/深色双主题
"""

from __future__ import annotations

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
)
from PySide6.QtGui import QAction, QFont, QColor, QIcon, QShortcut, QKeySequence
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
)

from printer_config import PrinterConfig, PrintJob, calc_cost, generate_order_number
from converter import get_converter, UniversalConverter
from pdf_printer import print_pdf, list_system_printers, get_pdf_info, get_docx_orientation, get_image_info, estimate_print_sides
from theme_manager import ThemeManager, MODE_SYSTEM, MODE_LIGHT, MODE_DARK, MODE_LABELS
from cloud_client import CloudClient, CloudTask

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
                self.log_message.emit(f"[跳过] 第 {idx + 1} 个任务（已取消）")
                break

            file_name = os.path.basename(job.file_path)
            self.progress.emit(offset_sides, total_sides, f"正在处理: {file_name}")
            self.log_message.emit(f"[{idx + 1}/{task_count}] {file_name}")

            ext = os.path.splitext(job.file_path)[1].lower()
            copies = max(1, job.copies)
            orient_info = f", 方向:{job.orientation}" if job.orientation else ""
            temp_pdf: Optional[str] = None

            # 此任务的预估面数
            this_job_sides = job_sides[idx] if idx < len(job_sides) else 1

            def _make_progress_callback(base_offset: int, total_all: int):
                def _on_side(page_seq: int, _task_total: int):
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
                    temp_pdf = self._converter.convert(job.file_path)
                    print_path = temp_pdf
                    self.log_message.emit(f"  → 转换完成: {os.path.basename(temp_pdf)}")

                # 2. 打印 PDF（传入进度回调）
                self.log_message.emit(
                    f"  → 正在打印 (份数:{copies}, 双面:{job.duplex}{orient_info})..."
                )
                dm = job.duplex_mode or self._duplex_mode
                effective_dpi = job.dpi if job.dpi > 0 else self._render_dpi
                ok, msg = print_pdf(
                    pdf_path=print_path,
                    printer_name=self._printer_name,
                    copies=copies,
                    duplex=job.duplex,
                    duplex_mode=dm,
                    page_range=job.page_range,
                    orientation=job.orientation,
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

            offset_sides += this_job_sides

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
    finished = Signal(int, str, int, str)  # (row, cached_pdf, page_count, orientation)

    def __init__(self, row: int, file_path: str, engine: str):
        super().__init__()
        self._row = row
        self._file_path = file_path
        self._engine = engine

    def run(self):
        import tempfile
        from converter import _convert_via_word_com, _convert_via_wps_com, get_converter
        from pdf_printer import get_pdf_info

        temp_pdf: str | None = None
        try:
            ext = os.path.splitext(self._file_path)[1].lower()
            if ext in (".doc", ".docx") and self._engine != "libreoffice":
                fd, temp_pdf = tempfile.mkstemp(suffix=".pdf", prefix="_conv_")
                os.close(fd)
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
                    self.finished.emit(self._row, "", 0, "")
                    return
            else:
                self.finished.emit(self._row, "", 0, "")
                return

        info = get_pdf_info(temp_pdf)
        self.finished.emit(self._row, temp_pdf, info["page_count"], info["orientation"])


def _cleanup_temp(path: str | None) -> None:
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


# ============================================================
# 支持拖放添加文件的表格
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
    """统一的云端任务列表窗口，管理所有待确认的云端打印任务。
    非模态，支持批量操作、取消响应和自动关闭。"""

    task_accepted = Signal(object)   # CloudTask — 用户确认添加
    task_rejected = Signal(object)   # CloudTask — 用户打回
    all_accepted = Signal()          # 全部添加
    all_rejected = Signal()          # 全部打回

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("☁ 云端任务列表")
        self.setMinimumWidth(600)
        self.setMinimumHeight(380)
        self.setModal(False)
        self.setAttribute(Qt.WA_DeleteOnClose, False)

        self._pending_tasks: dict[int, dict] = {}  # task_id → {"task": CloudTask, "status": str, "canceled_at": float}
        self._auto_close_seconds = 300  # 默认 5 分钟
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
        auto_row.addWidget(QLabel("自动关闭：待确认任务为 0 后"))
        self._auto_minutes = QSpinBox()
        self._auto_minutes.setRange(0, 60)
        self._auto_minutes.setValue(5)
        self._auto_minutes.setSuffix(" 分钟")
        self._auto_minutes.setFixedWidth(100)
        self._auto_minutes.valueChanged.connect(self._on_auto_close_changed)
        auto_row.addWidget(self._auto_minutes)
        self._auto_seconds = QSpinBox()
        self._auto_seconds.setRange(0, 59)
        self._auto_seconds.setValue(0)
        self._auto_seconds.setSuffix(" 秒")
        self._auto_seconds.setFixedWidth(80)
        self._auto_seconds.valueChanged.connect(self._on_auto_close_changed)
        auto_row.addWidget(self._auto_seconds)
        auto_row.addStretch()
        auto_row.addWidget(QLabel("后自动关闭"))
        layout.addLayout(auto_row)

        # ── 表格 ──
        from PySide6.QtWidgets import QTableWidget as _QTW, QTableWidgetItem as _QTWI
        self._table = _QTW()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(["订单号", "文件名", "份数", "状态", "操作"])
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
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
        """添加一个待确认的云端任务。若窗口未显示则自动弹出。"""
        if task.task_id in self._pending_tasks:
            return  # 去重
        self._pending_tasks[task.task_id] = {
            "task": task,
            "status": "pending",
            "canceled_at": None,
        }
        self._cancel_auto_close()
        self._rebuild_table()
        if not self.isVisible():
            self.show()
            self._cancel_remove_timer.start()

    def mark_canceled(self, order_id: int, task_ids: list[int]):
        """标记指定订单的任务为已取消（响应 F6 取消推送）。"""
        now = time.time()
        for tid in task_ids:
            if tid in self._pending_tasks:
                entry = self._pending_tasks[tid]
                if entry["status"] == "pending":
                    entry["status"] = "canceled"
                    entry["canceled_at"] = now
        self._rebuild_table()
        self._check_auto_close()

    def _rebuild_table(self):
        """刷新表格内容。"""
        self._table.setRowCount(0)
        for tid, entry in self._pending_tasks.items():
            task = entry["task"]
            status = entry["status"]
            row = self._table.rowCount()
            self._table.insertRow(row)

            # 订单号
            from PySide6.QtWidgets import QTableWidgetItem as _QTWI
            order_text = task.order_number or f"#{task.order_id}"
            self._table.setItem(row, 0, _QTWI(order_text))

            # 文件名
            self._table.setItem(row, 1, _QTWI(task.file_name))

            # 份数
            self._table.setItem(row, 2, _QTWI(str(task.copies)))

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
                btn_layout.setContentsMargins(2, 1, 2, 1)
                btn_layout.setSpacing(4)

                accept_btn = QPushButton("📥 添加")
                accept_btn.setFixedWidth(70)
                accept_btn.clicked.connect(lambda checked=False, t=task: self._on_accept_one(t))
                btn_layout.addWidget(accept_btn)

                reject_btn = QPushButton("↩ 打回")
                reject_btn.setFixedWidth(70)
                reject_btn.clicked.connect(lambda checked=False, t=task: self._on_reject_one(t))
                btn_layout.addWidget(reject_btn)

                self._table.setCellWidget(row, 4, btn_widget)
            elif status == "canceled":
                info_btn = QPushButton("✕ 已取消")
                info_btn.setFixedWidth(80)
                info_btn.setEnabled(False)
                self._table.setCellWidget(row, 4, info_btn)
            else:
                self._table.setItem(row, 4, _QTWI("—"))

        # 更新按钮状态
        has_pending = any(e["status"] == "pending" for e in self._pending_tasks.values())
        self._accept_all_btn.setEnabled(has_pending)
        self._reject_all_btn.setEnabled(has_pending)

    # ── 单任务操作 ──

    def _on_accept_one(self, task: CloudTask):
        if task.task_id in self._pending_tasks:
            self._pending_tasks[task.task_id]["status"] = "accepted"
        self.task_accepted.emit(task)
        self._rebuild_table()
        self._check_auto_close()

    def _on_reject_one(self, task: CloudTask):
        if task.task_id in self._pending_tasks:
            self._pending_tasks[task.task_id]["status"] = "rejected"
        self.task_rejected.emit(task)
        self._rebuild_table()
        self._check_auto_close()

    # ── 批量操作 ──

    def _on_accept_all(self):
        pending = [e["task"] for e in self._pending_tasks.values() if e["status"] == "pending"]
        if not pending:
            return
        reply = QMessageBox.question(
            self, "全部添加", f"将为 {len(pending)} 个任务各创建一个新标签页，确定吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return
        for task in pending:
            self._pending_tasks[task.task_id]["status"] = "accepted"
            self.task_accepted.emit(task)
        self._rebuild_table()
        self._check_auto_close()

    def _on_reject_all(self):
        pending = [e["task"] for e in self._pending_tasks.values() if e["status"] == "pending"]
        if not pending:
            return
        reply = QMessageBox.question(
            self, "全部打回", f"将打回 {len(pending)} 个待确认任务，确定吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return
        for task in pending:
            self._pending_tasks[task.task_id]["status"] = "rejected"
            self.task_rejected.emit(task)
        self._rebuild_table()
        self._check_auto_close()

    # ── 自动关闭 ──

    def _on_auto_close_changed(self):
        self._auto_close_seconds = self._auto_minutes.value() * 60 + self._auto_seconds.value()

    def _check_auto_close(self):
        """检查是否所有任务已处理完毕，启动自动关闭计时器。"""
        has_pending = any(e["status"] == "pending" for e in self._pending_tasks.values())
        all_done = not has_pending and len(self._pending_tasks) > 0

        if all_done:
            if not self._auto_close_timer.isActive():
                self._auto_close_timer.start(self._auto_close_seconds * 1000)
        else:
            self._cancel_auto_close()

    def _cancel_auto_close(self):
        if self._auto_close_timer.isActive():
            self._auto_close_timer.stop()
        # 清理已处理的条目
        self._pending_tasks = {
            tid: e for tid, e in self._pending_tasks.items()
            if e["status"] == "pending"
        }
        if not self._pending_tasks:
            self._auto_close_timer.start(self._auto_close_seconds * 1000)

    def _on_auto_close(self):
        """自动关闭：无待确认任务且计时器到期。"""
        pending = [e for e in self._pending_tasks.values() if e["status"] == "pending"]
        if not pending:
            self._cancel_remove_timer.stop()
            self.hide()

    def _check_canceled_expiry(self):
        """定期检查已取消任务是否超过 5 秒，超时移除。"""
        now = time.time()
        changed = False
        for tid, entry in list(self._pending_tasks.items()):
            if entry["status"] == "canceled" and entry["canceled_at"]:
                if now - entry["canceled_at"] > 5:
                    del self._pending_tasks[tid]
                    changed = True
        if changed:
            self._rebuild_table()
            self._check_auto_close()

    # ── 关闭 ──

    def _on_close(self):
        """关闭窗口：检查是否有未确认任务，提示全部打回。"""
        pending = [e["task"] for e in self._pending_tasks.values() if e["status"] == "pending"]
        if pending:
            reply = QMessageBox.question(
                self, "关闭窗口",
                f"还有 {len(pending)} 个未确认的任务，关闭将全部打回。\n确定关闭吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            for task in pending:
                self._pending_tasks[task.task_id]["status"] = "rejected"
                self.task_rejected.emit(task)
        self._cancel_remove_timer.stop()
        self._auto_close_timer.stop()
        self.hide()

    def closeEvent(self, event):
        self._on_close()
        event.ignore()  # 不真正关闭，只是隐藏


# ============================================================
# 主窗口
# ============================================================

class MainWindow(QMainWindow):
    """HN 本地打印工具 — 主窗口"""

    # 表格列索引常量
    COL_FILE, COL_COPIES, COL_DUPLEX, COL_RANGE, COL_PAGES, COL_ORIENT, COL_ENGINE, COL_COST = range(8)

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

        # ── 标签页系统 ──
        # 确保 tabs 中至少有一个标签
        if not self._config.tabs:
            self._config.tabs = {"1": []}
        self._current_tab = self._config.active_tab or "1"
        if self._current_tab not in self._config.tabs:
            self._current_tab = next(iter(self._config.tabs.keys()))
        # 撤回备份（每个标签独立）
        self._cleared_jobs_backup: dict[str, list[PrintJob]] = {}
        # 已处理的云端任务 ID（防重复弹窗）
        self._processed_cloud_tasks: set[int] = set()

        # ── 云端客户端 ──
        self._cloud_client: CloudClient | None = None
        self._cloud_tasks: dict[int, CloudTask] = {}  # task_id → CloudTask
        self._cloud_task_window: CloudTaskListWindow | None = None

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

    # ---- 云端连接 ----

    def _init_cloud_client(self):
        """初始化云打印客户端（根据配置决定是否自动连接）。"""
        import socket
        client_id = socket.gethostname()

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
        self._cloud_client.order_canceled.connect(self._on_cloud_order_canceled)

        # 初始化云端任务列表窗口（非模态，复用）
        self._cloud_task_window = CloudTaskListWindow(self)
        self._cloud_task_window.task_accepted.connect(self._on_cloud_task_accepted)
        self._cloud_task_window.task_rejected.connect(self._on_cloud_task_rejected)

        # 如果配置了 token 且启用了云端，自动连接
        if self._config.cloud_enabled and self._config.cloud_token:
            self._cloud_client.start()
            self._update_cloud_status()

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

        # API 地址
        layout.addWidget(QLabel("API 地址:"))
        api_input = QLineEdit(self._config.cloud_api_url)
        api_input.setPlaceholderText("https://hn-space.cn")
        layout.addWidget(api_input)

        # WebSocket 地址
        layout.addWidget(QLabel("WebSocket 地址:"))
        ws_input = QLineEdit(self._config.cloud_ws_url)
        ws_input.setPlaceholderText("wss://hn-space.cn")
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
            # 保存配置到内存
            self._config.cloud_api_url = api_input.text().strip()
            self._config.cloud_ws_url = ws_input.text().strip()
            self._config.cloud_token = token_input.text().strip()
            self._config.cloud_enabled = True

            # 立刻写入磁盘，防止程序崩溃丢失
            try:
                self._config.save(self._config_path)
            except Exception as e:
                logger.warning(f"保存云端配置失败: {e}")

            # 更新 CloudClient 并连接
            if self._cloud_client:
                self._cloud_client.stop()
                self._cloud_client.api_url = self._config.cloud_api_url
                self._cloud_client.ws_url = self._config.cloud_ws_url
                self._cloud_client.token = self._config.cloud_token
                self._cloud_client.start()
                self._update_cloud_status()
            self._log("☁ 云端配置已保存并写入磁盘，正在连接...")

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
        if connected:
            self._cloud_status_indicator.setText("☁ 已连接")
            self._cloud_status_indicator.setObjectName("cloudStatusOn")
            self._cloud_status_btn.setText("断开云端")
        else:
            self._cloud_status_indicator.setText("☁ 未连接")
            self._cloud_status_indicator.setObjectName("cloudStatusOff")
            self._cloud_status_btn.setText("连接云端")
        # 刷新样式
        self._cloud_status_indicator.style().unpolish(self._cloud_status_indicator)
        self._cloud_status_indicator.style().polish(self._cloud_status_indicator)

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
        splitter.setStretchFactor(1, 1)
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

        self._status_bar.addPermanentWidget(QLabel("  "))

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

    def _log(self, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._log_text.append(f"[{timestamp}] {msg}")
        sb = self._log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_theme_changed(self):
        action = self.sender()
        if action:
            mode = action.data()
            self._theme_manager.set_mode(mode)
            self._theme_manager.apply_to_app(QApplication.instance(), mode)

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

        # ---- 第二行：附加服务全局配置 ----
        row2 = QHBoxLayout()
        row2.setSpacing(6)

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
        self._cover_page_onoff_combo.currentIndexChanged.connect(self._on_price_changed)

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

        # 标签页信息标签
        self._tab_info_label = QLabel("")
        self._tab_info_label.setObjectName("tabInfoLabel")
        tab_row.addWidget(self._tab_info_label)

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
        self._order_number_label = QLabel("")
        self._order_number_label.setObjectName("orderNumberLabel")
        total_row.addWidget(self._order_number_label)
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
        tab_keys = sorted(self._config.tabs.keys(), key=lambda x: int(x))
        if not tab_keys:
            self._config.tabs = {"1": []}
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
            new_key = str(int(tab_keys[-1]) + 1)
            self._config.tabs[new_key] = []
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
        for key, jobs in self._config.tabs.items():
            if key != self._current_tab and len(jobs) == 0:
                return True
        return False

    def _renumber_tabs(self):
        """删除标签页后重新从 1 开始编号。更新 _current_tab 指向同一标签的新 key。"""
        old_current = self._current_tab
        old_tabs = self._config.tabs
        # 按当前 key 的数值排序，保留 current 对应的 jobs
        sorted_keys = sorted(old_tabs.keys(), key=lambda x: int(x))
        current_jobs = old_tabs.get(old_current, [])
        # 重新编号
        new_tabs = {}
        new_current = "1"
        new_idx = 1
        for old_key in sorted_keys:
            new_key = str(new_idx)
            new_tabs[new_key] = old_tabs[old_key]
            if old_key == old_current:
                new_current = new_key
            new_idx += 1
        self._config.tabs = new_tabs
        self._current_tab = new_current
        self._config.active_tab = new_current

    def _cleanup_empty_tabs(self, after_key: str | None = None):
        """删除空标签页。after_key 不为 None 时仅删除 key > after_key 的标签页。"""
        removed = []
        for key in sorted(self._config.tabs.keys(), key=lambda x: int(x)):
            if key == self._current_tab:
                continue
            if after_key is not None and int(key) <= int(after_key):
                continue
            if len(self._config.tabs.get(key, [])) == 0:
                removed.append(key)
        for key in removed:
            del self._config.tabs[key]
            self._log(f"🗑 已删除空标签页 {key}")
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
        self._tab_label.setText(self._current_tab)

        tab_keys = sorted(self._config.tabs.keys(), key=lambda x: int(x))
        try:
            idx = tab_keys.index(self._current_tab)
        except ValueError:
            idx = 0

        # 更新按钮启用状态
        self._tab_btn_minus.setEnabled(idx > 0)
        # F4: 存在空标签页（不含当前）时启用按钮
        if hasattr(self, '_tab_btn_cleanup_empty') and self._tab_btn_cleanup_empty:
            self._tab_btn_cleanup_empty.setEnabled(self._has_empty_tabs_except_current())

        # 更新信息标签
        jobs = self._config.tabs.get(self._current_tab, [])
        total = sum(calc_cost(j.page_count, j.copies, j.duplex,
                              self._config.simplex_price, self._config.duplex_price,
                              j.page_range)[0] for j in jobs)
        self._tab_info_label.setText(f"共 {len(jobs)} 个文件 · 合计 ¥{total:.2f}")

        # F9: 更新订单号显示
        order_num = ""
        for j in jobs:
            if j.order_number:
                order_num = j.order_number
                break
        if hasattr(self, '_order_number_label') and self._order_number_label:
            if order_num:
                self._order_number_label.setText(f"📋 {order_num}")
            else:
                self._order_number_label.setText("📋 未分配订单号")

    def _get_current_jobs(self) -> list[PrintJob]:
        """返回当前标签页的任务列表。"""
        return self._config.tabs.get(self._current_tab, [])

    def _set_current_jobs(self, jobs: list[PrintJob]):
        """设置当前标签页的任务列表并保存配置。"""
        self._config.tabs[self._current_tab] = jobs
        self._save_config()

    def _save_config(self):
        """实时保存配置到 JSON 文件。"""
        try:
            self._sync_ui_to_config()
            self._config.save(self._config_path)
        except Exception as e:
            logger.warning(f"自动保存配置失败: {e}")

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
            tab_keys = sorted(self._config.tabs.keys(), key=lambda x: int(x))
            table.setRowCount(0)
            for key in tab_keys:
                jobs = self._config.tabs.get(key, [])
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
                marker = " ★" if key == self._current_tab else ""
                table.setItem(row, 0, _QTWI(f"标签 {key}{marker}"))
                table.setItem(row, 1, _QTWI(str(file_count)))
                table.setItem(row, 2, _QTWI(str(total_pages)))
                table.setItem(row, 3, _QTWI(f"¥{total_cost:.2f}"))
                table.setItem(row, 4, _QTWI(source))
                table.setItem(row, 5, _QTWI(order_num))

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
            jobs = self._config.tabs.get(key, [])
            if jobs:
                reply = QMessageBox.question(
                    dlg, "确认删除",
                    f"标签页 {key} 中有 {len(jobs)} 个文件，删除后无法恢复。\n确定删除吗？",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return
            del self._config.tabs[key]
            self._renumber_tabs()
            self._save_config()
            self._rebuild_table()
            self._refresh_tab_display()
            self._sync_edit_enabled(False)
            self._log(f"已删除标签页 {key}")
            _rebuild_dialog_table()

        def _delete_all_tabs():
            """删除全部标签页并重置为仅标签页 1"""
            total = sum(len(j) for j in self._config.tabs.values())
            if total > 0:
                reply = QMessageBox.question(
                    dlg, "确认清空全部",
                    f"将删除全部 {len(self._config.tabs)} 个标签页（共 {total} 个文件），"
                    "重置为单个空标签页。\n\n确定清空吗？",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return
            self._config.tabs = {"1": []}
            self._current_tab = "1"
            self._config.active_tab = "1"
            self._save_config()
            self._rebuild_table()
            self._refresh_tab_display()
            self._sync_edit_enabled(False)
            self._log("已清空全部标签页")
            _rebuild_dialog_table()

        def _cleanup_empty_in_dialog():
            """在标签页管理器中删除空标签页。"""
            removed = []
            for key in sorted(self._config.tabs.keys(), key=lambda x: int(x)):
                if key == self._current_tab:
                    continue
                if len(self._config.tabs.get(key, [])) == 0:
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

        cleanup_empty_btn = QPushButton("🗑 删除空标签页")
        cleanup_empty_btn.clicked.connect(_cleanup_empty_in_dialog)
        btn_row.addWidget(cleanup_empty_btn)

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
        image_exts = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
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
        """更新合计费用标签。"""
        total = 0.0
        all_known = True
        for job in self._get_current_jobs():
            cost, _ = calc_cost(job.page_count, job.copies, job.duplex,
                                self._config.simplex_price, self._config.duplex_price,
                                job.page_range)
            total += cost
            if job.page_count <= 0:
                all_known = False
        prefix = "≈ " if not all_known else ""
        self._total_label.setText(f"合计: {prefix}¥{total:.2f}")

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
        row = min(rows)
        jobs = self._get_current_jobs()
        if row >= len(jobs):
            self._sync_edit_enabled(False)
            return
        job = jobs[row]
        self._sync_edit_enabled(True)

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

        eng_map = {"word": 0, "wps": 1, "libreoffice": 2}
        self._edit_engine.blockSignals(True)
        self._edit_engine.setCurrentIndex(eng_map.get(job.engine, 0))
        self._edit_engine.blockSignals(False)

        dpi_map = {0: 0, 200: 1, 300: 2, 400: 3, 600: 4}
        self._edit_dpi.blockSignals(True)
        self._edit_dpi.setCurrentIndex(dpi_map.get(job.dpi, 0))
        self._edit_dpi.blockSignals(False)

        # 更新选中文件标签
        self._selected_file_label.setText(
            f"📄 {os.path.basename(job.file_path)}\n"
            f"路径: {job.file_path}"
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
        job.duplex = "on" if self._edit_duplex.currentIndex() == 0 else "off"
        job.duplex_mode = "short-edge" if self._edit_duplex_mode.currentIndex() == 1 else "long-edge"

        # 页码范围
        ranges_str = ",".join(
            inp.text().strip() for inp in self._edit_page_range._inputs
            if inp.text().strip()
        )
        job.page_range = ranges_str

        # 引擎
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
        label_duplex = QLabel("单/双面:")
        gl.addWidget(label_duplex)
        self._edit_duplex = QComboBox()
        self._edit_duplex.addItems(["双面打印", "单面打印"])
        _disable_combo_wheel(self._edit_duplex)
        self._edit_duplex.currentIndexChanged.connect(self._auto_apply_edit)
        gl.addWidget(self._edit_duplex)

        # 双面模式（仅双面时可用）
        label_duplex_mode = QLabel("双面模式:")
        gl.addWidget(label_duplex_mode)
        self._edit_duplex_mode = QComboBox()
        self._edit_duplex_mode.addItems(["长边翻转", "短边翻转"])
        _disable_combo_wheel(self._edit_duplex_mode)
        self._edit_duplex_mode.currentIndexChanged.connect(self._auto_apply_edit)
        gl.addWidget(self._edit_duplex_mode)

        # 页码范围
        label_range = QLabel("页码范围:")
        gl.addWidget(label_range)
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

        gl.addStretch()

        # 统一管理：无选中任务时全部禁用
        self._edit_widgets = [
            label_copies, self._edit_copies,
            label_duplex, self._edit_duplex,
            label_duplex_mode, self._edit_duplex_mode,
            label_range, self._edit_page_range,
            self._label_engine, self._edit_engine,
            label_dpi, self._edit_dpi,
        ]
        for w in self._edit_widgets:
            w.setEnabled(False)

        scroll_layout.addWidget(gb)

        # 当前选中文件信息
        self._selected_file_label = QLabel("(未选中任务)")
        self._selected_file_label.setObjectName("selectedFileLabel")
        self._selected_file_label.setWordWrap(True)
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
        self._urgency_price_spin.setValue(
            self._config.urgency_prices.get(self._config.urgency, 0.0))
        self._urgency_price_spin.blockSignals(False)

        self._cover_page_onoff_combo.blockSignals(True)
        self._cover_page_onoff_combo.setCurrentIndex(1 if self._config.cover_page else 0)
        self._cover_page_onoff_combo.blockSignals(False)

        self._cover_page_price_spin.blockSignals(True)
        self._cover_page_price_spin.setValue(self._config.cover_page_price)
        self._cover_page_price_spin.blockSignals(False)

        self._rebuild_table()

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

    def _on_refresh_printers(self):
        """刷新打印机列表按钮回调。"""
        self._refresh_printer_list()
        self._log("已刷新打印机列表")

    def _on_price_changed(self):
        """单价变更 → 重算当前标签页所有费用并保存。"""
        self._config.simplex_price = self._simplex_price_spin.value()
        self._config.duplex_price = self._duplex_price_spin.value()
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

    # ---- 附加服务信号处理 ----

    def _on_delivery_toggled(self):
        """派送开关变更。"""
        enabled = (self._delivery_onoff_combo.currentIndex() == 1)
        self._config.delivery_enabled = enabled
        self._delivery_location_combo.setEnabled(enabled)
        self._delivery_percent_spin.setEnabled(enabled)
        if enabled:
            loc = self._delivery_location_combo.currentText()
            pct = self._config.delivery_percentages.get(loc, 0.0)
            self._delivery_percent_spin.blockSignals(True)
            self._delivery_percent_spin.setValue(pct)
            self._delivery_percent_spin.blockSignals(False)
        self._on_price_changed()

    def _on_delivery_location_changed(self):
        """派送地点变更 → 更新百分比 spinbox。"""
        loc = self._delivery_location_combo.currentText()
        self._config.delivery_location = loc
        pct = self._config.delivery_percentages.get(loc, 0.0)
        self._delivery_percent_spin.blockSignals(True)
        self._delivery_percent_spin.setValue(pct)
        self._delivery_percent_spin.blockSignals(False)
        self._on_price_changed()

    def _on_delivery_percent_changed(self):
        """派送百分比编辑 → 回写地点百分比表。"""
        loc = self._delivery_location_combo.currentText()
        self._config.delivery_percentages[loc] = self._delivery_percent_spin.value()
        self._on_price_changed()

    def _on_urgency_changed(self):
        """优先级变更 → 更新价格 spinbox。"""
        level = self._urgency_combo.currentText()
        self._config.urgency = level
        price = self._config.urgency_prices.get(level, 0.0)
        self._urgency_price_spin.blockSignals(True)
        self._urgency_price_spin.setValue(price)
        self._urgency_price_spin.blockSignals(False)
        self._on_price_changed()

    def _on_urgency_price_changed(self):
        """紧急价格编辑 → 回写紧急价格表。"""
        level = self._urgency_combo.currentText()
        self._config.urgency_prices[level] = self._urgency_price_spin.value()
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

    def _start_convert_worker(self, row: int, file_path: str, engine: str):
        """启动后台 PDF 转换线程。"""
        self._convert_workers = [w for w in self._convert_workers if w.isRunning()]
        for w in self._convert_workers:
            if getattr(w, '_file_path', '') == file_path:
                try:
                    w.finished.disconnect()
                except Exception:
                    pass
                w.wait(100)
        worker = ConvertWorker(row, file_path, engine)
        worker.finished.connect(self._on_convert_finished)
        self._convert_workers.append(worker)
        worker.start()

    def _cancel_all_convert_workers(self):
        """终止所有正在进行的 PDF 转换线程。"""
        for w in self._convert_workers:
            if w.isRunning():
                try:
                    w.finished.disconnect()
                except Exception:
                    pass
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

    def _ensure_order_number(self) -> str:
        """确保当前标签页有订单号，若无则生成并分配。返回订单号字符串。"""
        jobs = self._get_current_jobs()
        if not jobs:
            return ""
        # 检查是否已有订单号
        for j in jobs:
            if j.order_number:
                return j.order_number
        # 生成新订单号
        from printer_config import generate_order_number
        order_number, next_num = generate_order_number(self._config.last_order_number)
        self._config.last_order_number = next_num
        # 分配给当前标签页所有任务
        for j in jobs:
            j.order_number = order_number
        self._set_current_jobs(jobs)
        self._refresh_tab_display()
        self._log(f"📋 已分配订单号: {order_number}")
        return order_number

    def _on_copy_total(self):
        """复制合计金额到剪贴板（含订单号）。"""
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
        """恢复复制按钮为可点击状态。"""
        if self._copy_total_timer and self._copy_total_timer.isActive():
            self._copy_total_timer.stop()
        if self._copy_total_btn:
            self._copy_total_btn.setText("📋 复制")
            self._copy_total_btn.setEnabled(True)
        if self._copy_detail_btn:
            self._copy_detail_btn.setText("📋 复制计费明细")
            self._copy_detail_btn.setEnabled(True)

    def _on_toggle_detail(self, checked: bool):
        """展开/收起计费明细复制按钮。"""
        self._copy_detail_btn.setVisible(checked)
        self._detail_toggle_btn.setText("⏶" if checked else "⏷")
        self._detail_toggle_btn.setToolTip("隐藏计费明细" if checked else "展开计费明细")

    def _can_paste_files(self) -> bool:
        """检查剪贴板是否包含可粘贴的文件。"""
        allowed = {".pdf", ".doc", ".docx", ".txt", ".md", ".html", ".htm",
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
        allowed = {".pdf", ".doc", ".docx", ".txt", ".md", ".html", ".htm",
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
                                    "文本(.txt/.md/.html), 图片(.jpg/.png/.bmp等)")

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
            "所有支持格式 (*.pdf *.doc *.docx *.txt *.md *.html *.htm"
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
        self._cancel_undo_if_active()
        if getattr(self, '_loading_files', False):
            logger.warning("上一批文件仍在处理中，忽略本次添加请求")
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

    def _on_undo_expired(self):
        """撤回超时，清除当前标签页的备份。"""
        self._cleared_jobs_backup.pop(self._current_tab, None)
        self._restore_clear_button()

    def _restore_clear_button(self):
        """恢复清空按钮正常样式。"""
        self._btn_clear.setText("✖ 清空列表")
        self._btn_clear.setStyleSheet("")
        self._btn_clear.setObjectName("")
        self._btn_clear.style().unpolish(self._btn_clear)
        self._btn_clear.style().polish(self._btn_clear)

    def _cancel_undo_if_active(self):
        """新增任务时取消撤回状态，丢弃备份。"""
        if self._clear_undo_timer.isActive():
            self._clear_undo_timer.stop()
        self._cleared_jobs_backup.pop(self._current_tab, None)
        self._restore_clear_button()

    def _on_progress(self, current: int, total: int, status: str):
        """更新进度条。"""
        self._progress_bar.setMaximum(total)
        self._progress_bar.setValue(current)
        self._status_label.setText(status)

    def _on_job_finished(self, idx: int, success: bool, message: str):
        """单个任务完成回调：上报云端（新 UI 无表格行操作）。"""
        flat_jobs = getattr(self, '_flat_jobs', [])
        if 0 <= idx < len(flat_jobs):
            job = flat_jobs[idx]
            task_id = getattr(job, 'task_id', 0)
            if task_id and self._cloud_client:
                if success:
                    self._cloud_client.report_success(task_id)
                else:
                    self._cloud_client.report_fail(task_id, message)

    def _start_print_worker(self, flat_jobs: list):
        """用给定的任务列表启动打印 Worker。"""
        self._flat_jobs = flat_jobs
        self._sync_ui_to_config()

        from datetime import datetime
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        order_number, next_num = generate_order_number(self._config.last_order_number)
        self._config.last_order_number = next_num
        try:
            self._config.save(self._config_path)
        except Exception as e:
            logger.warning(f"保存配置失败: {e}")

        self._btn_start.setEnabled(False)
        self._progress_bar.setValue(0)

        cover_page_config = {
            "simplex_price": self._config.simplex_price,
            "duplex_price": self._config.duplex_price,
            "delivery_enabled": self._config.delivery_enabled,
            "delivery_location": self._config.delivery_location,
            "delivery_percentages": self._config.delivery_percentages,
            "urgency": self._config.urgency,
            "urgency_prices": self._config.urgency_prices,
            "cover_page_price": self._config.cover_page_price,
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
            cover_page=self._config.cover_page,
            cover_page_config=cover_page_config,
        )
        worker.progress.connect(self._on_progress)
        worker.log_message.connect(self._log)
        worker.job_finished.connect(self._on_job_finished)
        worker.all_finished.connect(self._on_all_finished)
        self._worker = worker
        worker.start()

    def _on_all_finished(self, success_count: int, fail_count: int):
        """全部任务完成。"""
        self._btn_start.setEnabled(True)
        self._worker = None

        total = success_count + fail_count
        self._status_label.setText(f"完成：成功 {success_count} / {total}")

        if fail_count > 0:
            QMessageBox.warning(
                self, "打印完成（有错误）",
                f"全部 {total} 个任务处理完毕。\n成功: {success_count}\n失败: {fail_count}\n\n详情请查看日志。"
            )
        else:
            self._log(f"✓ 全部 {total} 个任务打印成功！")

    def _on_about(self):
        """关于对话框。"""
        QMessageBox.about(
            self, "关于 HN 本地打印工具",
            "<h3>HN 本地打印工具 v3.1</h3>"
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
        # 写入界面（支持 HTML）
        if "<span" in msg:
            self._log_text.append(plain.replace(msg, msg))  # HTML 原样
        self._log_text.append(f"[{ts}] {msg}")
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

    def _on_cloud_task_received(self, task: CloudTask):
        """收到新的云端打印任务 → 加入云端任务列表窗口。"""
        if task.task_id in self._processed_cloud_tasks:
            return  # 已处理过，跳过（防 SocketIO + HTTP 双通道重复）
        self._cloud_tasks[task.task_id] = task
        self._log(f"☁ 收到云端任务 #{task.task_id}: {task.file_name}")
        if task.status == "ready" and self._cloud_task_window:
            self._cloud_task_window.add_task(task)

    def _on_cloud_task_updated(self, task: CloudTask):
        """云端任务状态更新 → 就绪时加入窗口，出错时通知服务器标记失败。"""
        if task.status == "ready" and task.task_id in self._processed_cloud_tasks:
            return  # 已处理过
        self._cloud_tasks[task.task_id] = task
        if task.status == "error":
            self._processed_cloud_tasks.add(task.task_id)
            self._log(f"☁ 云端任务 #{task.task_id} 出错: {task.error_message}")
            if self._cloud_client:
                self._cloud_client.report_fail(task.task_id, f"下载失败: {task.error_message}")
                self._cloud_client.reject_task(task.task_id)
            self._cloud_tasks.pop(task.task_id, None)
        elif task.status == "ready":
            self._log(f"☁ 云端任务 #{task.task_id} 下载完成")
            if self._cloud_task_window:
                self._cloud_task_window.add_task(task)

    def _on_cloud_order_canceled(self, order_id: int, task_ids: list):
        """云端订单被用户取消 → 通知任务列表窗口更新状态。"""
        self._log(f"☁ 订单 #{order_id} 已被用户取消")
        if self._cloud_task_window:
            self._cloud_task_window.mark_canceled(order_id, task_ids)
        # 如果已添加到标签页中，弹出提示
        for key, jobs in list(self._config.tabs.items()):
            for job in jobs:
                if job.task_id in task_ids:
                    QMessageBox.information(
                        self, "任务已取消",
                        f"标签页 {key} 中的任务「{job.display_name or os.path.basename(job.file_path)}」已被用户取消。\n"
                        f"建议删除该标签页。",
                    )
                    break

    def _on_cloud_task_accepted(self, task: CloudTask):
        """用户从云端任务列表窗口确认添加单个任务。"""
        self._processed_cloud_tasks.add(task.task_id)
        self._add_cloud_task_to_new_tab(task)

    def _on_cloud_task_rejected(self, task: CloudTask):
        """用户从云端任务列表窗口打回单个任务。"""
        self._processed_cloud_tasks.add(task.task_id)
        if task.order_id and self._cloud_client:
            self._cloud_client.reject_order_to_server(task.order_id)
        self._cloud_tasks.pop(task.task_id, None)

    def _on_cloud_connection_changed(self, connected: bool):
        """云端连接状态改变。"""
        self._update_cloud_status()

    def _on_cloud_status_message(self, msg: str):
        """云端日志消息 → 写入界面日志。"""
        self._log(msg)

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

        # 检查所有标签页是否有未完成的文件
        total_files = 0
        for key, jobs in self._config.tabs.items():
            total_files += len(jobs)

        if total_files > 0:
            reply = QMessageBox.question(
                self, "存在未完成的任务",
                f"当前共有 {total_files} 个文件分布在 {len(self._config.tabs)} 个标签页中尚未打印。\n\n"
                "退出将清空全部标签页，确定退出吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            # 用户确认退出 → 清空全部标签页
            self._config.tabs = {"1": []}
            self._config.active_tab = "1"
            self._current_tab = "1"

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

        super().closeEvent(event)


    # ──────── 复制详情 ────────

    def _on_copy_detail(self):
        """复制当前标签页的计费明细到剪贴板（含订单号）。"""
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
        lines.append(f"合计: ¥{total:.2f}")
        QApplication.clipboard().setText("\n".join(lines))

    # ──────── 转换完成回调 ────────

    def _on_convert_finished(self, row: int, cached_pdf: str, page_count: int, orientation: str):
        """后台 PDF 转换完成 → 更新表格、缓存。"""
        if row >= self._table.rowCount():
            return
        if not cached_pdf:
            if row < self._table.rowCount():
                self._table.item(row, self.COL_PAGES).setText("?")
            return

        jobs = self._get_current_jobs()
        if row < len(jobs):
            old_pdf = jobs[row].cached_pdf
            if old_pdf and os.path.isfile(old_pdf) and old_pdf != cached_pdf:
                try:
                    os.remove(old_pdf)
                except OSError:
                    pass
            jobs[row].cached_pdf = cached_pdf
            jobs[row].page_count = page_count
            jobs[row].orientation = orientation
            self._set_current_jobs(jobs)

        if row < self._table.rowCount():
            self._table.item(row, self.COL_PAGES).setText(str(page_count))
            ori_map = {"portrait": "竖", "landscape": "横", "mixed": "混"}
            ori_text = ori_map.get(orientation, "")
            self._table.item(row, self.COL_ORIENT).setText(ori_text)
        self._recalc_row_cost(row)
        self._update_total_cost()

        # 转换完成后存入 MD5 缓存（供后续同文件复用，避免重复转换）
        if cached_pdf and os.path.isfile(cached_pdf) and row < len(jobs):
            job = jobs[row]
            # 如果没有 source_md5，从源文件计算
            if not job.source_md5 and job.file_path and os.path.isfile(job.file_path):
                try:
                    job.source_md5 = self._cloud_client._compute_md5_file(job.file_path) if self._cloud_client else ""
                except Exception:
                    pass
            # 存入缓存
            if job.source_md5 and self._cloud_client:
                try:
                    self._cloud_client._save_pdf_to_cache(
                        job.source_md5, cached_pdf,
                        job.display_name or os.path.basename(job.file_path),
                        os.path.splitext(job.file_path)[1].lower(),
                        page_count,
                    )
                    self._set_current_jobs(jobs)  # 保存 source_md5 到配置
                except Exception as e:
                    self._log(f"  → MD5 缓存保存失败: {e}")

        # 转换完成后立刻保存副本到桌面
        if self._config.keep_temp_pdf and cached_pdf and os.path.isfile(cached_pdf):
            file_path = jobs[row].file_path if row < len(jobs) else ""
            original_base = os.path.splitext(os.path.basename(file_path))[0] if file_path else "document"
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            dest_name = f"[转换]{original_base}.pdf"
            dest_path = os.path.join(desktop, dest_name)
            try:
                shutil.copy2(cached_pdf, dest_path)
                self._log(f"  → 转换副本已保存到桌面: {dest_name}")
            except OSError as e:
                self._log(f"  → 保存转换副本到桌面失败: {e}")

    def _add_cloud_task_to_new_tab(self, task: CloudTask):
        """将云端任务添加到新的标签页并切换过去。会检查 PDF 缓存避免重复转换。"""
        tab_keys = sorted(self._config.tabs.keys(), key=lambda x: int(x))
        new_key = str(int(tab_keys[-1]) + 1) if tab_keys else "1"
        self._config.tabs[new_key] = []

        ext = os.path.splitext(task.local_path)[1].lower() if task.local_path else ""
        image_exts = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
        source_md5 = getattr(task, 'source_md5', '') or ''

        # 计算 MD5（如果 task 里没有）
        if not source_md5 and task.local_path and os.path.isfile(task.local_path):
            try:
                source_md5 = self._cloud_client._compute_md5_file(task.local_path) if self._cloud_client else ""
            except Exception:
                source_md5 = ""

        page_count = 0; orientation = ""; cached_pdf = ""
        need_convert = False

        # 1. 检查 PDF 缓存（MD5 索引）
        if source_md5 and self._cloud_client:
            cached_pdf, cached_meta = self._cloud_client._get_cached_pdf(source_md5)
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
            engine=engine,
            task_id=task.task_id,
            source_md5=source_md5,
            display_name=task.file_name,  # 使用后端返回的原始文件名
            order_number=task.order_number,  # 云端订单号
            cached_pdf=cached_pdf,        # 使用缓存的 PDF（如有）
        )
        self._config.tabs[new_key].append(job)

        self._current_tab = new_key
        self._config.active_tab = new_key
        self._save_config()
        self._rebuild_table()
        self._refresh_tab_display()
        self._sync_edit_enabled(False)

        # Word 文件：缓存未命中时才启动转换
        if ext in (".doc", ".docx") and task.local_path and not cached_pdf:
            self._start_convert_worker(0, task.local_path, engine)

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

        jobs = self._get_current_jobs()
        if not jobs:
            return
        self._cancel_all_convert_workers()
        self._cleared_jobs_backup[self._current_tab] = list(jobs)
        self._set_current_jobs([])
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
        rows = sorted(set(idx.row() for idx in self._table.selectedIndexes()), reverse=True)
        if not rows:
            return
        jobs = self._get_current_jobs()
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
        allowed_types = {
            ".pdf", ".doc", ".docx", ".xls", ".xlsx",
            ".txt", ".md", ".html", ".htm",
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
                if ext in (".doc", ".docx"):
                    self._start_convert_worker(i, job.file_path, job.engine)
