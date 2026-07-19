"""
gui.py — PySide6 主界面 MainWindow + 打印工作线程 PrintWorker
HN 本地打印工具 — 支持浅色/深色双主题
"""

from __future__ import annotations

import logging
import os
import shutil
import traceback
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
        parent=None,
    ):
        super().__init__(parent)
        self._jobs = jobs
        self._printer_name = printer_name
        self._duplex_mode = duplex_mode
        self._keep_temp_pdf = keep_temp_pdf
        self._render_dpi = render_dpi
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
# 主窗口
# ============================================================

class MainWindow(QMainWindow):
    """HN 本地打印工具 — 主窗口"""

    COL_FILE = 0
    COL_COPIES = 1
    COL_DUPLEX = 2
    COL_RANGE = 3
    COL_PAGES = 4
    COL_ORIENT = 5
    COL_ENGINE = 6
    COL_COST = 7

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

        # ── 云端客户端 ──
        self._cloud_client: CloudClient | None = None
        self._cloud_tasks: dict[int, CloudTask] = {}  # task_id → CloudTask
        self._cloud_task_widgets: dict[int, QWidget] = {}  # task_id → 面板控件
        self._cloud_origin_files: dict[str, int] = {}  # file_path → task_id 映射

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

        # 如果配置了 token 且启用了云端，自动连接
        if self._config.cloud_enabled and self._config.cloud_token:
            self._cloud_client.start()
            self._update_cloud_connect_button()

    def _toggle_cloud_connection(self):
        """切换云端连接状态。"""
        if not self._cloud_client:
            return
        if self._cloud_client.is_connected():
            self._cloud_client.stop()
            self._update_cloud_connect_button()
        else:
            # 从 UI 读取配置并更新
            self._config.cloud_api_url = self._cloud_api_input.text().strip()
            self._config.cloud_ws_url = self._cloud_ws_input.text().strip()
            self._config.cloud_token = self._cloud_token_input.text().strip()
            self._config.cloud_enabled = True
            self._cloud_client.api_url = self._config.cloud_api_url
            self._cloud_client.ws_url = self._config.cloud_ws_url
            self._cloud_client.token = self._config.cloud_token
            self._cloud_client.start()
            self._update_cloud_connect_button()

    def _update_cloud_connect_button(self):
        """更新云端连接按钮的外观。"""
        if self._cloud_client and self._cloud_client.is_connected():
            self._cloud_connect_btn.setText("☁ 断开")
            self._cloud_connect_btn.setObjectName("cloudConnected")
            self._cloud_status_label.setText("🟢 已连接")
            self._cloud_status_label.setObjectName("cloudStatusOn")
        else:
            self._cloud_connect_btn.setText("☁ 连接")
            self._cloud_connect_btn.setObjectName("cloudDisconnected")
            self._cloud_status_label.setText("🔴 未连接")
            self._cloud_status_label.setObjectName("cloudStatusOff")
        # 强制刷新样式
        self._cloud_connect_btn.style().unpolish(self._cloud_connect_btn)
        self._cloud_connect_btn.style().polish(self._cloud_connect_btn)
        self._cloud_status_label.style().unpolish(self._cloud_status_label)
        self._cloud_status_label.style().polish(self._cloud_status_label)

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
        self._log_text.setMinimumHeight(40)
        _enable_smooth_scroll(self._log_text)

        v_splitter = QSplitter(Qt.Vertical)
        v_splitter.addWidget(top_container)
        v_splitter.addWidget(self._log_text)
        v_splitter.setStretchFactor(0, 1)
        v_splitter.setStretchFactor(1, 0)
        v_splitter.setSizes([600, 120])
        root.addWidget(v_splitter, 1)

        # -- 状态栏 --
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_label = QLabel("就绪")
        self._status_bar.addWidget(self._status_label)

    def _setup_menu(self):
        """设置菜单栏。"""
        mb = self.menuBar()

        # 文件菜单
        file_menu = mb.addMenu("文件(&F)")

        open_action = QAction("打开(&O)", self)
        open_action.triggered.connect(self._on_add_files)
        file_menu.addAction(open_action)

        file_menu.addSeparator()

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

        # —— Row 3: 云端连接配置 ——
        row3 = QHBoxLayout()
        row3.setSpacing(6)

        row3.addWidget(QLabel("☁ 云端:"))

        # 云端开关
        self._cloud_status_label = QLabel("🔴 未连接")
        self._cloud_status_label.setObjectName("cloudStatusOff")
        row3.addWidget(self._cloud_status_label)

        row3.addWidget(QLabel("API:"))
        self._cloud_api_input = QLineEdit(self._config.cloud_api_url)
        self._cloud_api_input.setPlaceholderText("https://hn-space.cn")
        self._cloud_api_input.setFixedWidth(180)
        row3.addWidget(self._cloud_api_input)

        row3.addWidget(QLabel("WS:"))
        self._cloud_ws_input = QLineEdit(self._config.cloud_ws_url)
        self._cloud_ws_input.setPlaceholderText("wss://hn-space.cn")
        self._cloud_ws_input.setFixedWidth(180)
        row3.addWidget(self._cloud_ws_input)

        row3.addWidget(QLabel("Token:"))
        self._cloud_token_input = QLineEdit(self._config.cloud_token)
        self._cloud_token_input.setPlaceholderText("打印机认证token")
        self._cloud_token_input.setEchoMode(QLineEdit.Password)
        self._cloud_token_input.setFixedWidth(150)
        row3.addWidget(self._cloud_token_input)

        self._cloud_connect_btn = QPushButton("☁ 连接")
        self._cloud_connect_btn.setObjectName("cloudDisconnected")
        self._cloud_connect_btn.setFixedWidth(80)
        self._cloud_connect_btn.clicked.connect(self._toggle_cloud_connection)
        row3.addWidget(self._cloud_connect_btn)

        row3.addStretch()
        root.addLayout(row3)

        # 统一输入框高度为下拉框高度（延迟到布局完成后测量实际高度）
        def _normalize_spin_heights():
            combo_h = self._printer_combo.height()
            if combo_h > 0:
                for sp in (self._simplex_price_spin, self._duplex_price_spin,
                           self._delivery_percent_spin, self._urgency_price_spin,
                           self._cover_page_price_spin):
                    sp.setFixedHeight(combo_h)
                for le in (self._cloud_api_input, self._cloud_ws_input, self._cloud_token_input):
                    le.setFixedHeight(combo_h)
        QTimer.singleShot(0, _normalize_spin_heights)

    def _setup_file_table(self) -> QWidget:
        """左侧：云端任务面板 + 本地文件列表表格。"""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # ── 云端任务面板（可折叠） ──
        self._cloud_panel_header = QWidget()
        header_layout = QHBoxLayout(self._cloud_panel_header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(4)

        self._cloud_panel_toggle = QPushButton("☁ 云端待处理 (0)")
        self._cloud_panel_toggle.setObjectName("cloudPanelToggle")
        self._cloud_panel_toggle.setCheckable(True)
        self._cloud_panel_toggle.setChecked(True)
        self._cloud_panel_toggle.toggled.connect(self._on_toggle_cloud_panel)
        header_layout.addWidget(self._cloud_panel_toggle)

        self._cloud_badge = QLabel("")
        self._cloud_badge.setObjectName("cloudBadge")
        header_layout.addWidget(self._cloud_badge)
        header_layout.addStretch()

        self._cloud_refresh_btn = QPushButton("🔄 拉取")
        self._cloud_refresh_btn.setToolTip("手动拉取云端排队任务")
        self._cloud_refresh_btn.clicked.connect(self._on_cloud_pull)
        header_layout.addWidget(self._cloud_refresh_btn)

        layout.addWidget(self._cloud_panel_header)

        # 云端任务列表区域（可折叠）
        self._cloud_tasks_container = QWidget()
        self._cloud_tasks_layout = QVBoxLayout(self._cloud_tasks_container)
        self._cloud_tasks_layout.setContentsMargins(0, 4, 0, 0)
        self._cloud_tasks_layout.setSpacing(3)

        self._cloud_empty_label = QLabel("暂无云端任务，等待小程序提交...")
        self._cloud_empty_label.setObjectName("cloudEmptyLabel")
        self._cloud_empty_label.setAlignment(Qt.AlignCenter)
        self._cloud_tasks_layout.addWidget(self._cloud_empty_label)
        self._cloud_tasks_layout.addStretch()

        layout.addWidget(self._cloud_tasks_container)

        # ── 本地任务列表 ──
        title = QLabel("📄 打印任务列表")
        title.setObjectName("sectionLabel")
        layout.addWidget(title)

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

        layout.addWidget(self._table)

        # 合计费用
        total_row = QHBoxLayout()
        total_row.setContentsMargins(0, 0, 0, 0)
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
        # 复制计费明细按钮（默认隐藏，由 ⏷ 控制）
        self._copy_detail_btn = QPushButton("📋 复制计费明细")
        self._copy_detail_btn.setVisible(False)
        self._copy_detail_btn.clicked.connect(self._on_copy_detail)
        total_row.addWidget(self._copy_detail_btn)
        # 展开计费明细的切换按钮
        self._detail_toggle_btn = QPushButton("⏷")
        self._detail_toggle_btn.setObjectName("detailToggleBtn")
        self._detail_toggle_btn.setCheckable(True)
        self._detail_toggle_btn.setToolTip("展开计费明细")
        self._detail_toggle_btn.toggled.connect(self._on_toggle_detail)
        total_row.addWidget(self._detail_toggle_btn)
        layout.addLayout(total_row)

        # 添加文件按钮 — 置于表格下方
        btn_add = QPushButton("📂 添加文件")
        btn_add.clicked.connect(self._on_add_files)
        layout.addWidget(btn_add)

        return container

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

        # 撤回定时器：清空后 5 秒内可撤回
        self._clear_undo_timer = QTimer(self)
        self._clear_undo_timer.setSingleShot(True)
        self._clear_undo_timer.timeout.connect(self._on_undo_expired)
        self._cleared_jobs_backup: list[PrintJob] = []

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
        """单价变更 → 重算所有费用。"""
        self._config.simplex_price = self._simplex_price_spin.value()
        self._config.duplex_price = self._duplex_price_spin.value()
        self._config.cover_page = (self._cover_page_onoff_combo.currentIndex() == 1)
        self._config.cover_page_price = self._cover_page_price_spin.value()
        for row in range(self._table.rowCount()):
            self._recalc_row_cost(row)
        self._update_total_cost()

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

        # jobs 已通过表格实时维护，这里不需要再同步

    def _rebuild_table(self):
        """用 config.jobs 重建表格。"""
        self._table.setRowCount(0)
        for job in self._config.jobs:
            self._add_table_row(job)

    def _add_table_row(self, job: PrintJob):
        """添加一行到表格。"""
        row = self._table.rowCount()
        self._table.insertRow(row)

        ext = os.path.splitext(job.file_path)[1].lower()
        image_exts = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
        is_image = ext in image_exts

        name_item = QTableWidgetItem(os.path.basename(job.file_path))
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
        ext = os.path.splitext(job.file_path)[1].lower()
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

        self._update_total_cost()

    def _on_convert_finished(self, row: int, cached_pdf: str, page_count: int, orientation: str):
        """后台 PDF 转换完成 → 更新表格、缓存、按需保存副本到桌面。"""
        if row >= self._table.rowCount():
            return
        if not cached_pdf:
            self._table.item(row, self.COL_PAGES).setText("?")
            return
        # 清理该行旧的缓存 PDF
        if row < len(self._config.jobs):
            old_pdf = self._config.jobs[row].cached_pdf
            if old_pdf and os.path.isfile(old_pdf) and old_pdf != cached_pdf:
                try:
                    os.remove(old_pdf)
                except OSError:
                    pass
            self._config.jobs[row].cached_pdf = cached_pdf
            self._config.jobs[row].page_count = page_count
            self._config.jobs[row].orientation = orientation
        self._table.item(row, self.COL_PAGES).setText(str(page_count))
        ori_map = {"portrait": "竖", "landscape": "横", "mixed": "混"}
        self._table.item(row, self.COL_ORIENT).setText(ori_map.get(orientation, ""))
        self._recalc_row_cost(row)
        self._update_total_cost()

        # 转换完成后立刻保存副本到桌面（而非等到打印完成）
        if self._config.keep_temp_pdf and cached_pdf and os.path.isfile(cached_pdf):
            file_path = ""
            if row < len(self._config.jobs):
                file_path = self._config.jobs[row].file_path
            original_base = os.path.splitext(os.path.basename(file_path))[0] if file_path else "document"
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            dest_name = f"[转换]{original_base}.pdf"
            dest_path = os.path.join(desktop, dest_name)
            try:
                shutil.copy2(cached_pdf, dest_path)
                self._log(f"  → 转换副本已保存到桌面: {dest_name}")
            except OSError as e:
                self._log(f"  → 保存转换副本到桌面失败: {e}")

    def _start_convert_worker(self, row: int, file_path: str, engine: str):
        """启动后台 PDF 转换线程。"""
        # 取消该行之前的转换 worker
        self._convert_workers = [w for w in self._convert_workers if w.isRunning()]
        for w in self._convert_workers:
            if w._row == row:
                w.finished.disconnect()
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

    def _update_total_cost(self):
        """重新计算并更新合计费用标签。"""
        base_total = 0.0  # 纸张费用合计（用于计算派送百分比）
        all_known = True
        for row in range(self._table.rowCount()):
            cost_item = self._table.item(row, self.COL_COST)
            if cost_item:
                c = cost_item.data(Qt.UserRole)
                if isinstance(c, (int, float)) and c > 0:
                    base_total += c
                elif cost_item.text() == "?":
                    all_known = False

        total = base_total

        # 订单级附加费用
        if self._config.delivery_enabled:
            pct = self._config.delivery_percentages.get(self._config.delivery_location, 0.0)
            total += base_total * (pct / 100.0)
        total += self._config.urgency_prices.get(self._config.urgency, 0.0)
        if self._config.cover_page:
            total += self._config.cover_page_price

        if total > 0:
            prefix = "≈ " if not all_known else ""
            self._total_label.setText(f"合计: {prefix}¥{total:.2f}")
        else:
            self._total_label.setText("合计: ¥0.00")
        # 总价变化时立即恢复复制按钮
        self._reset_copy_button()

    def _on_copy_total(self):
        """复制合计金额到剪贴板。"""
        text = self._total_label.text()
        # 去掉"合计: "前缀和"≈ "前缀，保留 ¥ 符号
        amount = text.replace("合计: ", "").replace("≈ ", "").strip()
        try:
            # 验证是否为有效金额
            float(amount.replace("¥", ""))
            clipboard = QApplication.clipboard()
            clipboard.setText(amount)
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

    def _on_copy_detail(self):
        """复制计费明细到剪贴板。"""
        image_exts = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
        lines = ["计费明细", "─" * 14]
        all_parts: list[str] = []  # 所有费用项的值，用于合计公式
        base_total = 0.0  # 纸张费用合计（用于计算派送费）
        item_num = 0

        # ── 1. 逐文件打印明细 ──
        for row in range(self._table.rowCount()):
            item_num += 1
            file_item = self._table.item(row, self.COL_FILE)
            file_path = file_item.data(Qt.UserRole) if file_item else ""
            filename = file_item.text() if file_item else "?"
            copies = self._table.item(row, self.COL_COPIES).text()
            duplex = self._table.item(row, self.COL_DUPLEX).text()
            page_range = self._table.item(row, self.COL_RANGE).text()
            orient = self._table.item(row, self.COL_ORIENT).text()

            display_name = _truncate_filename(filename)
            ext_lower = os.path.splitext(file_path)[1].lower()
            is_image = ext_lower in image_exts

            if is_image:
                meta_parts = [f"{copies}份"]
                if orient:
                    meta_parts.append(orient)
                meta_line = " | ".join(meta_parts)
            else:
                meta_parts = [f"{copies}份", duplex]
                if page_range and page_range not in ("全部", "—"):
                    meta_parts.append(f"{page_range}页")
                else:
                    meta_parts.append("全部页")
                if orient:
                    meta_parts.append(orient)
                meta_line = " | ".join(meta_parts)

            # 获取纸张费用
            pages_text = self._table.item(row, self.COL_PAGES).text()
            page_count = int(pages_text) if pages_text.isdigit() else 0
            dup = "on" if "双" in duplex else "off"
            pr = page_range if page_range not in ("全部", "—") else ""
            paper_cost, paper_formula = calc_cost(page_count, int(copies), dup,
                                                  self._config.simplex_price,
                                                  self._config.duplex_price, pr)

            lines.append(f"{item_num}. {display_name}")
            lines.append(f"   {meta_line}")
            if paper_cost > 0:
                lines.append(f"   {paper_formula}=¥{paper_cost:.2f}")
                all_parts.append(f"{paper_cost:.2f}")
                base_total += paper_cost
            elif page_count <= 0:
                lines.append(f"   💰 ?")

        # ── 2. 派送 ──
        item_num += 1
        if self._config.delivery_enabled:
            loc = self._config.delivery_location
            pct = self._config.delivery_percentages.get(loc, 0.0)
            delivery_cost = base_total * (pct / 100.0)
            if pct > 0 and delivery_cost > 0:
                lines.append(f"{item_num}. 派送：是 | {loc} {pct:.1f}% | ￥{delivery_cost:.2f}")
                all_parts.append(f"{delivery_cost:.2f}")
            else:
                lines.append(f"{item_num}. 派送：是 | {loc}免费")
        else:
            lines.append(f"{item_num}. 派送：否")

        # ── 3. 优先级 ──
        item_num += 1
        urgency_price = self._config.urgency_prices.get(self._config.urgency, 0.0)
        if urgency_price > 0:
            lines.append(f"{item_num}. 优先级：{self._config.urgency} | ￥{urgency_price:.2f}")
            all_parts.append(f"{urgency_price:.2f}")
        else:
            lines.append(f"{item_num}. 优先级：{self._config.urgency} | ￥0")

        # ── 4. 打印首页信息 ──
        if self._config.cover_page:
            item_num += 1
            lines.append(f"{item_num}. 打印首页信息 | {self._config.cover_page_price:.2f}")
            all_parts.append(f"{self._config.cover_page_price:.2f}")

        # ── 合计 ──
        lines.append("─" * 14)
        total_sum = sum(float(p) for p in all_parts) if all_parts else 0.0
        formula = "+".join(all_parts) if all_parts else "0"
        lines.append(f"💰合计: {formula}=￥{total_sum:.2f}")

        # ── 自取提示 ──
        if not self._config.delivery_enabled:
            order_no, next_num = generate_order_number(self._config.last_order_number)
            self._config.last_order_number = next_num
            from datetime import date
            today = date.today()
            short_no = f"{today.month}-{today.day}-{next_num:04d}"
            lines.append("")
            lines.append(f"⚠️ 您选择了自取，请凭{short_no}号码去[{self._config.pickup_address}]取件，请不要敲门，你必须提前联系打印机管理者才能取件，为保证良好的宿舍秩序，请提前10分钟询问是否可取件，不要过早或过晚。")

        QApplication.clipboard().setText("\n".join(lines))
        # 按钮反馈
        if self._copy_detail_btn:
            self._copy_detail_btn.setText("✅ 已复制明细")
            self._copy_detail_btn.setEnabled(False)
        if self._copy_total_timer:
            self._copy_total_timer.start(5000)

    def _recalc_row_cost(self, row: int):
        """重新计算指定行的费用。"""
        pages_text = self._table.item(row, self.COL_PAGES).text()
        page_count = int(pages_text) if pages_text.isdigit() else 0
        copies = int(self._table.item(row, self.COL_COPIES).text())
        duplex_text = self._table.item(row, self.COL_DUPLEX).text()
        duplex = "on" if "双" in duplex_text else "off"
        page_range = self._table.item(row, self.COL_RANGE).text()
        if page_range in ("全部", "—"):
            page_range = ""

        cost, formula = calc_cost(page_count, copies, duplex,
                                  self._config.simplex_price, self._config.duplex_price,
                                  page_range)
        if cost > 0:
            cost_text = f"{formula}=¥{cost:.2f}"
        elif page_count <= 0:
            cost_text = "?"
        else:
            cost_text = "¥0.00"
        cost_item = self._table.item(row, self.COL_COST)
        if cost_item:
            cost_item.setText(cost_text)
            cost_item.setToolTip(cost_text)
            cost_item.setData(Qt.UserRole, cost)

    def _refresh_config_jobs_from_table(self):
        """从表格数据重建 config.jobs 列表。"""
        jobs: list[PrintJob] = []
        for row in range(self._table.rowCount()):
            file_path = self._table.item(row, self.COL_FILE).data(Qt.UserRole)
            copies = int(self._table.item(row, self.COL_COPIES).text())
            duplex_text = self._table.item(row, self.COL_DUPLEX).text()
            duplex = "on" if "双" in duplex_text else "off"
            # 从 "双面(长边)" / "双面(短边)" 中解析双面模式
            duplex_mode = ""
            if duplex == "on":
                duplex_mode = "short-edge" if "短边" in duplex_text else "long-edge"
            page_range = self._table.item(row, self.COL_RANGE).text()
            if page_range in ("全部", "—"):
                page_range = ""
            pages_text = self._table.item(row, self.COL_PAGES).text()
            page_count = int(pages_text) if pages_text.isdigit() else 0
            ori_map = {"竖": "portrait", "横": "landscape", "混": "mixed"}
            ori_text = self._table.item(row, self.COL_ORIENT).text()
            orientation = ori_map.get(ori_text, "")
            # 读取引擎（静态文本列）
            engine_text = self._table.item(row, self.COL_ENGINE).text() if self._table.item(row, self.COL_ENGINE) else ""
            eng_rev = {"Word": "word", "WPS": "wps", "LibreOffice": "libreoffice"}
            engine = eng_rev.get(engine_text, "word")
            # DPI 不在表格中，从 config.jobs 保留
            job_dpi = self._config.jobs[row].dpi if row < len(self._config.jobs) else 0
            # cached_pdf 也需要保留
            job_cached_pdf = self._config.jobs[row].cached_pdf if row < len(self._config.jobs) else ""
            jobs.append(PrintJob(file_path=file_path, copies=copies, duplex=duplex,
                                 duplex_mode=duplex_mode,
                                 page_range=page_range, page_count=page_count,
                                 orientation=orientation, engine=engine,
                                 dpi=job_dpi,
                                 cached_pdf=job_cached_pdf))
        self._config.jobs = jobs

    # ---- 事件处理 ----

    def _on_table_selection(self):
        """表格选中行变化 → 右侧编辑面板同步。"""
        if getattr(self, '_loading_files', False):
            return
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            self._selected_file_label.setText("(未选中任务)")
            self._edit_copies.setValue(1)
            self._edit_duplex.setCurrentIndex(0)
            self._edit_page_range.set_ranges("")
            for w in self._edit_widgets:
                w.setEnabled(False)
            return

        for w in self._edit_widgets:
            w.setEnabled(True)

        row = rows[0].row()
        file_path = self._table.item(row, self.COL_FILE).data(Qt.UserRole)
        copies = int(self._table.item(row, self.COL_COPIES).text())
        duplex_text = self._table.item(row, self.COL_DUPLEX).text()
        page_range = self._table.item(row, self.COL_RANGE).text()

        self._selected_file_label.setText(f"当前文件:\n{file_path}")
        # 阻断信号避免加载时误触发自动应用
        self._edit_copies.blockSignals(True)
        self._edit_copies.setValue(copies)
        self._edit_copies.blockSignals(False)
        self._edit_duplex.blockSignals(True)
        self._edit_duplex.setCurrentIndex(0 if "双" in duplex_text else 1)
        self._edit_duplex.blockSignals(False)
        # 双面模式
        is_duplex = "双" in duplex_text
        self._edit_duplex_mode.setEnabled(is_duplex)
        self._edit_duplex_mode.blockSignals(True)
        self._edit_duplex_mode.setCurrentIndex(0 if "长边" in duplex_text else 1)
        self._edit_duplex_mode.blockSignals(False)
        # 设置总页数用于范围校验
        pages_text = self._table.item(row, self.COL_PAGES).text()
        total_pages = int(pages_text) if pages_text.isdigit() else 0
        self._edit_page_range.set_total_pages(total_pages)
        self._edit_page_range.set_ranges("" if page_range in ("全部", "—") else page_range)
        # 同步引擎
        eng_text = self._table.item(row, self.COL_ENGINE).text() if self._table.item(row, self.COL_ENGINE) else "Word"
        eng_map = {"Word": 0, "WPS": 1, "LibreOffice": 2}
        self._edit_engine.blockSignals(True)
        self._edit_engine.setCurrentIndex(eng_map.get(eng_text, 0))
        self._edit_engine.blockSignals(False)
        # PDF / 图片文件不需要转换，引擎不可用
        ext = os.path.splitext(file_path)[1].lower()
        is_convertible = ext in (".doc", ".docx")
        self._label_engine.setEnabled(is_convertible)
        self._edit_engine.setEnabled(is_convertible)
        # 图片只有一页，单双面和页码范围无意义
        image_exts = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
        is_image = ext in image_exts
        self._edit_duplex.setEnabled(not is_image)
        self._edit_duplex_mode.setEnabled(not is_image and is_duplex)
        self._edit_page_range.setEnabled(not is_image)
        # 同步渲染 DPI
        job_dpi = 0
        if row < len(self._config.jobs):
            job_dpi = self._config.jobs[row].dpi
        dpi_map = {0: 0, 200: 1, 300: 2, 400: 3, 600: 4}
        self._edit_dpi.blockSignals(True)
        self._edit_dpi.setCurrentIndex(dpi_map.get(job_dpi, 0))
        self._edit_dpi.blockSignals(False)

    def _on_table_double_click(self, row: int, col: int):
        """双击表格行 → 用系统默认程序打开文件。"""
        file_path = self._table.item(row, self.COL_FILE).data(Qt.UserRole)
        if file_path and os.path.isfile(file_path):
            try:
                os.startfile(file_path)
                self._log(f"打开文件: {os.path.basename(file_path)}")
            except OSError as e:
                logger.warning(f"无法打开文件 ({file_path}): {e}")
                QMessageBox.warning(self, "无法打开文件",
                                    f"没有找到可以打开此类型文件的程序。\n\n{file_path}")

    def _on_table_context_menu(self, pos):
        """表格右键菜单。"""
        row = self._table.rowAt(pos.y())
        menu = QMenu(self)

        if row < 0:
            # 空白区域：粘贴文件
            paste_action = menu.addAction("📋 粘贴")
            paste_action.setEnabled(self._can_paste_files())
            action = menu.exec(self._table.viewport().mapToGlobal(pos))
            if action == paste_action:
                self._on_paste_files()
            return

        # 有效行：移除 + 粘贴
        remove_action = menu.addAction("移除选中")
        menu.addSeparator()
        paste_action = menu.addAction("📋 粘贴")
        paste_action.setEnabled(self._can_paste_files())
        action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if action == remove_action:
            self._on_remove_selected()
        elif action == paste_action:
            self._on_paste_files()

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

    def _auto_apply_edit(self):
        """编辑面板参数变更 → 即时写回选中行。"""
        # 文件加载期间不处理（避免 processEvents 递归触发）
        if getattr(self, '_loading_files', False):
            return
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()

        file_path = self._table.item(row, self.COL_FILE).data(Qt.UserRole)
        ext = os.path.splitext(file_path)[1].lower()
        image_exts = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
        is_image = ext in image_exts

        self._table.item(row, self.COL_COPIES).setText(str(self._edit_copies.value()))
        # 图片：双面和页码范围不适用
        if is_image:
            # 保持 "—" 不覆盖
            pass
        else:
            if self._edit_duplex.currentIndex() == 0:
                dm_text = "长边" if self._edit_duplex_mode.currentIndex() == 0 else "短边"
                self._table.item(row, self.COL_DUPLEX).setText(f"双面({dm_text})")
            else:
                self._table.item(row, self.COL_DUPLEX).setText("单面")
        # 双面模式仅双面时启用
        is_duplex = self._edit_duplex.currentIndex() == 0
        self._edit_duplex_mode.setEnabled(is_duplex and not is_image)
        page_range = self._edit_page_range.get_ranges()
        if is_image:
            self._table.item(row, self.COL_RANGE).setText("—")
        else:
            self._table.item(row, self.COL_RANGE).setText(page_range if page_range else "全部")
        # 引擎
        eng_map = {0: "word", 1: "wps", 2: "libreoffice"}
        eng_labels = {"word": "Word", "wps": "WPS", "libreoffice": "LibreOffice"}
        engine = eng_map.get(self._edit_engine.currentIndex(), "word")
        self._table.item(row, self.COL_ENGINE).setText(eng_labels.get(engine, "Word"))
        if row < len(self._config.jobs):
            old_engine = self._config.jobs[row].engine
            self._config.jobs[row].engine = engine
            self._config.jobs[row].copies = self._edit_copies.value()
            self._config.jobs[row].duplex = "on" if is_duplex else "off"
            dm_map = {0: "long-edge", 1: "short-edge"}
            self._config.jobs[row].duplex_mode = dm_map.get(self._edit_duplex_mode.currentIndex(), "long-edge")
            # 页码范围（"全部" 对应空字符串）
            pr = page_range if not is_image else ""
            self._config.jobs[row].page_range = pr
            # 渲染 DPI
            dpi_values = [0, 200, 300, 400, 600]
            idx = self._edit_dpi.currentIndex()
            self._config.jobs[row].dpi = dpi_values[idx] if 0 <= idx < len(dpi_values) else 0
            # 引擎变更时重新转换 PDF
            if engine != old_engine:
                job = self._config.jobs[row]
                ext = os.path.splitext(job.file_path)[1].lower()
                if ext in (".doc", ".docx"):
                    self._table.item(row, self.COL_PAGES).setText("...")
                    self._start_convert_worker(row, job.file_path, job.engine)

        self._recalc_row_cost(row)
        self._update_total_cost()

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

    def __add_files_to_table_impl(self, files: list[str]):
        """_add_files_to_table 的内部实现（由重入保护包装）。"""
        allowed_types = {
            ".pdf",
            ".doc", ".docx",
            ".txt", ".md", ".html", ".htm",
            ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp",
        }
        image_exts = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
        start_row = self._table.rowCount()

        blocked = 0
        blocked_names: list[str] = []

        for f in files:
            ext = os.path.splitext(f)[1].lower()

            if ext not in allowed_types:
                blocked += 1
                blocked_names.append(os.path.basename(f))
                continue

            page_count = 0
            orientation = ""

            if ext == ".pdf":
                info = get_pdf_info(f)
                page_count = info["page_count"]
                orientation = info["orientation"]
            elif ext == ".docx":
                orientation = get_docx_orientation(f)
            elif ext == ".doc":
                # .doc 不支持 python-docx，方向在转 PDF 后获取
                orientation = ""
            elif ext in image_exts:
                info = get_image_info(f)
                page_count = info["page_count"]
                orientation = info["orientation"]

            # 根据元数据自动选择引擎；不可用时按 Word→WPS→LibreOffice 降级
            engine = "word"
            if ext in (".doc", ".docx"):
                from converter import _read_docx_last_editor, get_available_engines
                available = get_available_engines()
                editor = _read_docx_last_editor(f) if ext == ".docx" else None
                preferred = "wps" if editor == "wps" else "word"
                fallback_order = ["word", "wps", "libreoffice"]
                if preferred in fallback_order:
                    fallback_order.remove(preferred)
                    fallback_order.insert(0, preferred)
                for eng in fallback_order:
                    if available.get(eng, False):
                        engine = eng
                        break

            # 图片强制单面，无页码/双面概念
            is_image = ext in image_exts
            duplex_mode = "short-edge" if orientation == "landscape" else "long-edge"
            job_duplex = "off" if is_image else "on"
            job = PrintJob(file_path=f, copies=1, duplex=job_duplex,
                           duplex_mode=duplex_mode, page_range="",
                           page_count=page_count, orientation=orientation, engine=engine)
            self._config.jobs.append(job)
            self._add_table_row(job)

        self._log(f"已添加 {len(files)} 个文件")

        # ── 转换为 PDF 并统计页数（Word 文件用指定引擎）──
        for row in range(start_row, self._table.rowCount()):
            file_path = self._table.item(row, self.COL_FILE).data(Qt.UserRole)
            ext = os.path.splitext(file_path)[1].lower()
            if ext == ".pdf":
                continue

            self._table.item(row, self.COL_PAGES).setText("...")

            # 后台线程转换 PDF
            job = self._config.jobs[row]
            self._start_convert_worker(row, job.file_path, job.engine)

        self._update_total_cost()

        if blocked > 0:
            names = "\n".join(f"  • {n}" for n in blocked_names[:5])
            more = f"\n  ... 等共 {blocked} 个文件" if blocked > 5 else ""
            QMessageBox.warning(
                self, "不支持的文件类型",
                f"以下 {blocked} 个文件格式不支持，"
                f"请手动转换为 PDF 后添加：\n\n{names}{more}"
            )

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

    def _on_remove_selected(self):
        """移除表格中选中的行。"""
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return
        # 从后往前删避免索引偏移
        for r in sorted(rows, key=lambda x: x.row(), reverse=True):
            self._table.removeRow(r.row())
        self._refresh_config_jobs_from_table()
        self._update_total_cost()
        self._log("已移除选中任务")

    def _on_clear_list(self):
        """清空任务列表（5秒内可撤回）。"""
        # 如果是撤回模式，执行撤回
        if self._clear_undo_timer.isActive():
            self._on_undo_clear()
            return

        if not self._config.jobs:
            return

        # 终止正在进行的转换，避免回调更新已不存在的行
        self._cancel_all_convert_workers()

        # 备份并清空
        self._cleared_jobs_backup = list(self._config.jobs)
        self._table.setRowCount(0)
        self._config.jobs.clear()
        self._update_total_cost()
        self._progress_bar.reset()
        self._log("已清空任务列表（5秒内可撤回）")

        # 按钮变为撤回样式（琥珀色醒目样式）
        self._btn_clear.setText("↩ 撤回")
        self._btn_clear.setStyleSheet(
            "QPushButton {"
            "  background-color: #e8960a;"
            "  color: #fff;"
            "  font-weight: bold;"
            "  border: 2px solid #c47e08;"
            "  border-radius: 4px;"
            "  padding: 4px 14px;"
            "}"
            "QPushButton:hover {"
            "  background-color: #f0a81e;"
            "}"
            "QPushButton:pressed {"
            "  background-color: #c47e08;"
            "}"
        )

        self._clear_undo_timer.start(5000)

    def _on_undo_clear(self):
        """撤回清空操作，恢复任务列表并重新转换。"""
        self._clear_undo_timer.stop()
        for row, job in enumerate(self._cleared_jobs_backup):
            self._config.jobs.append(job)
            self._add_table_row(job)
            # 需要转换的文件重新启动后台转换
            ext = os.path.splitext(job.file_path)[1].lower()
            if ext != ".pdf":
                self._table.item(row, self.COL_PAGES).setText("...")
                self._start_convert_worker(row, job.file_path, job.engine)
        self._cleared_jobs_backup.clear()
        self._update_total_cost()
        self._log("已撤回清空操作")
        self._restore_clear_button()

    def _on_undo_expired(self):
        """撤回超时，清除备份。"""
        self._cleared_jobs_backup.clear()
        self._restore_clear_button()

    def _restore_clear_button(self):
        """恢复清空按钮正常样式。"""
        self._btn_clear.setText("✖ 清空列表")
        self._btn_clear.setStyleSheet("")
        self._btn_clear.setObjectName("")
        self._btn_clear.style().unpolish(self._btn_clear)
        self._btn_clear.style().polish(self._btn_clear)

    def _cancel_undo_if_active(self):
        """新增任务时取消撤回状态。"""
        if self._clear_undo_timer.isActive():
            self._clear_undo_timer.stop()
            self._cleared_jobs_backup.clear()
            self._restore_clear_button()

    def _on_start_print(self):
        """开始打印。"""
        self._refresh_config_jobs_from_table()
        self._sync_ui_to_config()

        if not self._config.jobs:
            QMessageBox.warning(self, "提示", "请先添加要打印的文件。")
            return

        # 打印开始 — 所有行变绿
        fg = QColor("#6b9e6b")
        for row in range(self._table.rowCount()):
            for col in range(self._table.columnCount()):
                item = self._table.item(row, col)
                if item:
                    item.setForeground(fg)
        self._table.viewport().update()

        # 检查页码范围有效性
        if hasattr(self._edit_page_range, 'is_valid') and not self._edit_page_range.is_valid():
            QMessageBox.warning(self, "页码范围错误",
                                "当前存在无效的页码范围设置，请修正后再打印。\n"
                                "可能原因：格式错误、范围重叠、页码超出文件总页数。")
            return

        # 自动保存配置
        try:
            self._config.save(self._config_path)
        except Exception as e:
            logger.warning(f"保存配置失败: {e}")

        # 禁用按钮，防止重复点击
        self._btn_start.setEnabled(False)
        self._progress_bar.setValue(0)

        worker = PrintWorker(
            jobs=list(self._config.jobs),
            printer_name=self._config.printer_name,
            duplex_mode=self._config.duplex_mode,
            keep_temp_pdf=self._config.keep_temp_pdf,
            render_dpi=self._config.render_dpi,
        )
        worker.progress.connect(self._on_progress)
        worker.log_message.connect(self._log)
        worker.job_finished.connect(self._on_job_finished)
        worker.all_finished.connect(self._on_all_finished)
        self._worker = worker
        worker.start()

    def _on_progress(self, current: int, total: int, status: str):
        """更新进度条。"""
        self._progress_bar.setMaximum(total)
        self._progress_bar.setValue(current)
        self._status_label.setText(status)

    def _on_job_finished(self, idx: int, success: bool, message: str):
        """单个任务完成回调：恢复行颜色 + 上报云端。"""
        # 恢复该行的正常颜色
        if 0 <= idx < self._table.rowCount():
            for col in range(self._table.columnCount()):
                item = self._table.item(idx, col)
                if item:
                    item.setBackground(QColor(255, 255, 255, 0))

        # 如果是云端任务，上报结果
        if 0 <= idx < len(self._config.jobs):
            job = self._config.jobs[idx]
            file_path = job.file_path
            task_id = self._cloud_origin_files.get(file_path)
            if task_id is not None and self._cloud_client:
                if success:
                    self._cloud_client.report_success(task_id)
                else:
                    self._cloud_client.report_fail(task_id, message)
                # 清理映射
                del self._cloud_origin_files[file_path]

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
        """追加日志到界面文本框。"""
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_text.append(f"[{ts}] {msg}")
        # 自动滚动到底部
        self._log_text.verticalScrollBar().setValue(
            self._log_text.verticalScrollBar().maximum()
        )

    # ---- 云端任务处理 ----

    def _on_cloud_task_received(self, task: CloudTask):
        """收到新的云端打印任务。"""
        self._cloud_tasks[task.task_id] = task
        self._rebuild_cloud_tasks_panel()
        self._log(f"☁ 新任务 #{task.task_id}: {task.file_name}")

    def _on_cloud_task_updated(self, task: CloudTask):
        """云端任务状态更新（下载进度等）。"""
        self._cloud_tasks[task.task_id] = task
        if task.status == "error":
            self._log(f"☁ 任务 #{task.task_id} 出错: {task.error_message}")
        elif task.status == "ready":
            self._log(f"☁ 任务 #{task.task_id} 下载完成，可加入打印列表")
        self._rebuild_cloud_tasks_panel()

    def _on_cloud_connection_changed(self, connected: bool):
        """云端连接状态改变。"""
        self._update_cloud_connect_button()

    def _on_cloud_status_message(self, msg: str):
        """云端日志消息 → 写入界面日志。"""
        self._log(msg)

    def _on_cloud_pull(self):
        """手动拉取云端排队任务。"""
        if self._cloud_client:
            self._cloud_client.pull_pending()
            self._log("☁ 已手动请求拉取云端排队任务")

    def _on_toggle_cloud_panel(self, checked: bool):
        """折叠/展开云端任务面板。"""
        self._cloud_tasks_container.setVisible(checked)
        if checked:
            self._cloud_panel_toggle.setText(
                f"☁ 云端待处理 ({len(self._cloud_tasks)})"
            )
        else:
            self._cloud_panel_toggle.setText("☁ 云端待处理 ▸")

    def _rebuild_cloud_tasks_panel(self):
        """重建云端任务面板的卡片列表。"""
        # 清空现有卡片
        for w in self._cloud_task_widgets.values():
            w.deleteLater()
        self._cloud_task_widgets.clear()

        # 移除空状态标签
        self._cloud_empty_label.setVisible(False)

        # 重建每张任务卡片
        pending_tasks = [
            t for t in self._cloud_tasks.values()
            if t.status not in ("accepted", "rejected")
        ]

        if not pending_tasks:
            self._cloud_empty_label.setVisible(True)
            self._cloud_panel_toggle.setText("☁ 云端待处理 (0)")
            self._cloud_badge.setText("")
            return

        self._cloud_panel_toggle.setText(f"☁ 云端待处理 ({len(pending_tasks)})")

        for task in pending_tasks:
            card = self._build_cloud_task_card(task)
            # 在 stretch 之前插入
            self._cloud_tasks_layout.insertWidget(
                self._cloud_tasks_layout.count() - 1, card
            )
            self._cloud_task_widgets[task.task_id] = card

    def _build_cloud_task_card(self, task: CloudTask) -> QWidget:
        """为单个云端任务构建 UI 卡片。"""
        card = QWidget()
        card.setObjectName("cloudTaskCard")
        outer = QVBoxLayout(card)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(2)

        # 第一行：文件名 + 状态
        row1 = QHBoxLayout()
        row1.setSpacing(6)

        file_label = QLabel(f"📎 {task.file_name}")
        file_label.setObjectName("cloudTaskFileName")
        file_label.setWordWrap(False)
        file_label.setMinimumWidth(200)
        row1.addWidget(file_label, 1)

        status_text = {
            "pending": "⏳ 等待下载",
            "downloading": f"⬇ {task.download_progress}%",
            "ready": "✅ 就绪",
            "error": f"❌ {task.error_message[:20]}",
        }.get(task.status, task.status)
        status_label = QLabel(status_text)
        status_label.setObjectName(f"cloudTaskStatus_{task.status}")
        row1.addWidget(status_label)

        outer.addLayout(row1)

        # 第二行：参数 + 操作按钮
        row2 = QHBoxLayout()
        row2.setSpacing(6)

        params = []
        params.append(f"📋 {task.copies}份")
        params.append(f"{'🔄 双面' if task.duplex == 'on' else '📄 单面'}")
        if task.page_range:
            params.append(f"🔢 {task.page_range}")
        if task.created_at:
            params.append(f"🕐 {task.created_at}")

        params_label = QLabel(" · ".join(params))
        params_label.setObjectName("cloudTaskParams")
        row2.addWidget(params_label, 1)

        # 操作按钮
        if task.status == "ready":
            btn_accept = QPushButton("📥 加入打印列表")
            btn_accept.setObjectName("cloudAcceptBtn")
            btn_accept.setFixedWidth(130)
            btn_accept.clicked.connect(
                lambda checked=False, tid=task.task_id: self._on_cloud_accept(tid)
            )
            row2.addWidget(btn_accept)

        elif task.status == "error":
            btn_retry = QPushButton("🔄 重试")
            btn_retry.setObjectName("cloudRetryBtn")
            btn_retry.setFixedWidth(70)
            btn_retry.clicked.connect(
                lambda checked=False, tid=task.task_id: self._on_cloud_retry(tid)
            )
            row2.addWidget(btn_retry)

        btn_reject = QPushButton("✕")
        btn_reject.setObjectName("cloudRejectBtn")
        btn_reject.setFixedSize(28, 28)
        btn_reject.setToolTip("移除此任务")
        btn_reject.clicked.connect(
            lambda checked=False, tid=task.task_id: self._on_cloud_reject(tid)
        )
        row2.addWidget(btn_reject)

        outer.addLayout(row2)
        return card

    def _on_cloud_accept(self, task_id: int):
        """接受云端任务：下载完成后加入本地打印列表。"""
        task = self._cloud_tasks.get(task_id)
        if not task or task.status != "ready":
            return
        if not task.local_path or not os.path.exists(task.local_path):
            self._log(f"☁ 任务 #{task_id} 文件不存在，请重试")
            return

        # 记录云来源映射（用于打印后上报）
        self._cloud_origin_files[task.local_path] = task_id

        # 加入本地打印列表（会按默认参数创建 PrintJob）
        self._add_files_to_table([task.local_path])

        # 将云端参数应用到刚刚加入的任务（最后一行）
        if self._table.rowCount() > 0:
            last_row = self._table.rowCount() - 1
            job = self._config.jobs[last_row]

            # 应用云端的份数、双面、页码范围
            if task.copies > 0:
                job.copies = task.copies
            job.duplex = task.duplex if task.duplex in ("on", "off") else "on"
            if task.page_range:
                job.page_range = task.page_range

            # 刷新表格行显示：份数、双面、页码范围
            copies_item = self._table.item(last_row, self.COL_COPIES)
            if copies_item:
                copies_item.setText(str(job.copies))
            duplex_item = self._table.item(last_row, self.COL_DUPLEX)
            if duplex_item:
                dm_label = job.duplex_mode or ""
                if job.duplex == "on":
                    display = "双面(长边)" if dm_label in ("long-edge", "") else f"双面({dm_label})"
                else:
                    display = "单面"
                duplex_item.setText(display)
            range_item = self._table.item(last_row, self.COL_RANGE)
            if range_item:
                range_item.setText(job.page_range if job.page_range.strip() else "全部")
            self._recalc_row_cost(last_row)

        # 从云端列表移除
        task = self._cloud_client.accept_and_add_to_local(task_id)
        if task:
            self._cloud_tasks.pop(task_id, None)
        self._rebuild_cloud_tasks_panel()

        self._log(f"☁ 任务 #{task_id} 已加入本地打印列表")

    def _on_cloud_reject(self, task_id: int):
        """拒绝/移除云端任务。"""
        if self._cloud_client:
            self._cloud_client.reject_task(task_id)
        self._cloud_tasks.pop(task_id, None)
        self._rebuild_cloud_tasks_panel()
        self._log(f"☁ 任务 #{task_id} 已移除")

    def _on_cloud_retry(self, task_id: int):
        """重试失败的云端任务下载。"""
        if self._cloud_client:
            self._cloud_client.accept_task(task_id)
            self._log(f"☁ 重试下载任务 #{task_id}")

    # ---- 关闭事件 ----

    def closeEvent(self, event):
        """关闭窗口时自动保存配置并断开云端。"""
        try:
            # 保存 cloud 配置
            self._config.cloud_api_url = self._cloud_api_input.text().strip()
            self._config.cloud_ws_url = self._cloud_ws_input.text().strip()
            self._config.cloud_token = self._cloud_token_input.text().strip()

            self._refresh_config_jobs_from_table()
            self._sync_ui_to_config()
            self._config.save(self._config_path)
            self._log(f"配置已自动保存至: {self._config_path}")
        except Exception as e:
            logger.warning(f"自动保存配置失败: {e}")

        # 断开云端连接
        if self._cloud_client:
            self._cloud_client.stop()

        super().closeEvent(event)
