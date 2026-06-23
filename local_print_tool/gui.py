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
    QFileDialog,
    QMessageBox,
    QMenuBar,
    QMenu,
    QAbstractItemView,
    QScrollArea,
    QStatusBar,
    QStyleFactory,
)

from printer_config import PrinterConfig, PrintJob, calc_cost
from converter import get_converter, UniversalConverter
from pdf_printer import print_pdf, list_system_printers, get_pdf_info, get_docx_orientation, get_image_info
from theme_manager import ThemeManager, MODE_SYSTEM, MODE_LIGHT, MODE_DARK, MODE_LABELS

logger = logging.getLogger(__name__)


# ============================================================
# 辅助工具
# ============================================================

def _disable_combo_wheel(combo: QComboBox) -> None:
    """禁止 QComboBox 响应鼠标滚轮事件，避免滚动页面时误改选项。"""
    class _WheelBlocker(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.Wheel:
                return True  # 吞掉滚轮事件
            return super().eventFilter(obj, event)

    combo.installEventFilter(_WheelBlocker(combo))


def _enable_smooth_scroll(scroll_area: QScrollArea) -> None:
    """为 QScrollArea 启用平滑滚动：拦截滚轮事件并用动画过渡。"""
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

    scroll_area.viewport().installEventFilter(_SmoothFilter(scroll_area))


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
        parent=None,
    ):
        super().__init__(parent)
        self._jobs = jobs
        self._printer_name = printer_name
        self._duplex_mode = duplex_mode
        self._keep_temp_pdf = keep_temp_pdf
        self._converter: Optional[UniversalConverter] = None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        self.log_message.emit("[取消] 用户取消了打印任务")

    def run(self):
        """线程主函数。"""
        self._converter = get_converter()
        total = len(self._jobs)
        success_count = 0
        fail_count = 0

        self.progress.emit(0, total, "开始处理...")
        self.log_message.emit(f"共 {total} 个任务待处理")

        for idx, job in enumerate(self._jobs):
            if self._cancelled:
                self.log_message.emit(f"[跳过] 第 {idx + 1} 个任务（已取消）")
                break

            file_name = os.path.basename(job.file_path)
            self.progress.emit(idx, total, f"正在处理: {file_name}")
            self.log_message.emit(f"[{idx + 1}/{total}] {file_name}")

            temp_pdf: Optional[str] = None
            try:
                # 1. 检查文件是否存在
                if not os.path.isfile(job.file_path):
                    raise FileNotFoundError(f"文件不存在: {job.file_path}")

                # 2. 如果不是 PDF，转换为 PDF
                ext = os.path.splitext(job.file_path)[1].lower()
                if ext == ".pdf":
                    print_path = job.file_path
                    self.log_message.emit(f"  → 已是 PDF，跳过转换")
                else:
                    self.log_message.emit(f"  → 正在转换为 PDF...")
                    temp_pdf = self._converter.convert(job.file_path)
                    print_path = temp_pdf
                    self.log_message.emit(f"  → 转换完成: {os.path.basename(temp_pdf)}")

                # 3. 静默打印（份数由原生 API 一次性处理）
                copies = max(1, job.copies)
                orient_info = f", 方向:{job.orientation}" if job.orientation else ""
                self.log_message.emit(f"  → 正在打印 (份数:{copies}, 双面:{job.duplex}{orient_info})...")
                ok, msg = print_pdf(
                    pdf_path=print_path,
                    printer_name=self._printer_name,
                    copies=copies,
                    duplex=job.duplex,
                    duplex_mode=self._duplex_mode,
                    page_range=job.page_range,
                    orientation=job.orientation,
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
                # 4. 清理临时 PDF（如需要则先复制到桌面供人工核对）
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
                    # 无论是否保留副本，TEMP 中的临时文件始终清理
                    try:
                        os.remove(temp_pdf)
                    except OSError as e:
                        self.log_message.emit(f"  → 清理临时 PDF 失败: {e}")

        self.progress.emit(total, total, "全部完成")
        self.all_finished.emit(success_count, fail_count)
        self.log_message.emit(
            f"========== 打印完毕：成功 {success_count}，失败 {fail_count} =========="
        )


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
    COL_COST = 6

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

        self.setWindowTitle("HN 本地打印工具")
        # 设置窗口图标
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "HN_printer.png")
        if os.path.isfile(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.setMinimumSize(900, 650)
        self.resize(1100, 720)

        self._setup_ui()
        self._load_config_to_ui()

    # ---- UI 构建 ----

    def _setup_ui(self):
        """构建完整界面。"""
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 8, 12, 12)
        root.setSpacing(8)

        # -- 菜单栏 --
        self._setup_menu()

        # -- 顶部：打印机信息 + 配置 --
        self._setup_top_bar(root)

        # -- 中部：文件列表 + 编辑面板（QSplitter） --
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._setup_file_table())
        splitter.addWidget(self._setup_edit_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

        # -- 进度条 --
        self._progress_bar = QProgressBar()
        self._progress_bar.setValue(0)
        root.addWidget(self._progress_bar)

        # -- 按钮栏 --
        root.addLayout(self._setup_button_bar())

        # -- 日志区域 --
        self._log_text = QTextEdit()
        self._log_text.setObjectName("logTextEdit")
        self._log_text.setReadOnly(True)
        self._log_text.setMaximumHeight(180)
        root.addWidget(self._log_text)

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
        """顶部：打印机选择 + 双面模式 + 保留 PDF 选项。"""
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

        layout.addWidget(QLabel("双面模式:"))

        self._duplex_combo = QComboBox()
        self._duplex_combo.addItems(["long-edge (长边翻转)", "short-edge (短边翻转)"])
        self._duplex_combo.setCurrentIndex(0)
        _disable_combo_wheel(self._duplex_combo)
        layout.addWidget(self._duplex_combo)

        layout.addWidget(QLabel("保存转换副本到桌面:"))

        self._keep_temp_check = QComboBox()
        self._keep_temp_check.addItems(["否", "是"])
        self._keep_temp_check.setCurrentIndex(0)
        _disable_combo_wheel(self._keep_temp_check)
        layout.addWidget(self._keep_temp_check)

        layout.addWidget(QLabel("  单面:"))

        self._simplex_price_spin = QDoubleSpinBox()
        self._simplex_price_spin.setButtonSymbols(QDoubleSpinBox.NoButtons)
        self._simplex_price_spin.setRange(0.01, 99.99)
        self._simplex_price_spin.setDecimals(2)
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
        self._duplex_price_spin.setValue(self._config.duplex_price)
        self._duplex_price_spin.setFixedWidth(60)
        self._duplex_price_spin.valueChanged.connect(self._on_price_changed)
        layout.addWidget(self._duplex_price_spin)
        layout.addWidget(QLabel("元/张"))

        root.addLayout(layout)

    def _setup_file_table(self) -> QWidget:
        """左侧：文件列表表格。"""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        title = QLabel("📄 打印任务列表")
        title.setObjectName("sectionLabel")
        layout.addWidget(title)

        self._table = DropTableWidget()
        self._table.setColumnCount(7)
        self._table.setHorizontalHeaderLabels(["文件名", "份数", "单/双面", "页码范围", "页数", "方向", "费用"])
        self._table.filesDropped.connect(self._on_files_dropped)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
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

        # 页码范围
        label_range = QLabel("页码范围:")
        gl.addWidget(label_range)
        self._edit_page_range = RangeListWidget()
        self._edit_page_range.rangesChanged.connect(self._auto_apply_edit)
        gl.addWidget(self._edit_page_range)

        gl.addStretch()

        # 统一管理：无选中任务时全部禁用
        self._edit_widgets = [
            label_copies, self._edit_copies,
            label_duplex, self._edit_duplex,
            label_range, self._edit_page_range,
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

        btn_clear = QPushButton("✖ 清空列表")
        btn_clear.clicked.connect(self._on_clear_list)
        layout.addWidget(btn_clear)

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

        dm = self._config.duplex_mode
        if dm == "short-edge":
            self._duplex_combo.setCurrentIndex(1)
        else:
            self._duplex_combo.setCurrentIndex(0)

        self._keep_temp_check.setCurrentIndex(1 if self._config.keep_temp_pdf else 0)
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
        for row in range(self._table.rowCount()):
            self._recalc_row_cost(row)
        self._update_total_cost()

    def _sync_ui_to_config(self):
        """将 UI 控件数据同步回配置对象。"""
        printer_data = self._printer_combo.currentData()
        self._config.printer_name = printer_data if printer_data else ""

        idx = self._duplex_combo.currentIndex()
        if idx == 1:
            self._config.duplex_mode = "short-edge"
        else:
            self._config.duplex_mode = "long-edge"

        self._config.keep_temp_pdf = (self._keep_temp_check.currentIndex() == 1)

        self._config.simplex_price = self._simplex_price_spin.value()
        self._config.duplex_price = self._duplex_price_spin.value()

        self._config.last_dir = self._last_dir

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

        name_item = QTableWidgetItem(os.path.basename(job.file_path))
        name_item.setData(Qt.UserRole, job.file_path)  # 存储完整路径
        name_item.setToolTip(job.file_path)
        self._table.setItem(row, self.COL_FILE, name_item)

        copies_item = QTableWidgetItem(str(job.copies))
        copies_item.setTextAlignment(Qt.AlignCenter)
        self._table.setItem(row, self.COL_COPIES, copies_item)

        duplex_item = QTableWidgetItem("双面" if job.duplex == "on" else "单面")
        duplex_item.setTextAlignment(Qt.AlignCenter)
        self._table.setItem(row, self.COL_DUPLEX, duplex_item)

        range_item = QTableWidgetItem(job.page_range or "全部")
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

    def _update_total_cost(self):
        """重新计算并更新合计费用标签。"""
        total = 0.0
        all_known = True
        for row in range(self._table.rowCount()):
            cost_item = self._table.item(row, self.COL_COST)
            if cost_item:
                c = cost_item.data(Qt.UserRole)
                if isinstance(c, (int, float)) and c > 0:
                    total += c
                elif cost_item.text() == "?":
                    all_known = False
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
        lines = ["HN 本地打印工具 — 计费明细", "─" * 30]
        for row in range(self._table.rowCount()):
            file_item = self._table.item(row, self.COL_FILE)
            filename = file_item.text() if file_item else "?"
            copies = self._table.item(row, self.COL_COPIES).text()
            duplex = self._table.item(row, self.COL_DUPLEX).text()
            page_range = self._table.item(row, self.COL_RANGE).text()
            orient = self._table.item(row, self.COL_ORIENT).text()
            cost_item = self._table.item(row, self.COL_COST)
            cost_text = cost_item.text() if cost_item else "?"
            lines.append(
                f"{row + 1}. {filename}\n"
                f"   份数: {copies} | 单/双面: {duplex} | "
                f"页码范围: {page_range} | 方向: {orient} | 费用: {cost_text}"
            )
        lines.append("─" * 30)
        total_text = self._total_label.text()
        lines.append(total_text)
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
        if page_range == "全部":
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
            page_range = self._table.item(row, self.COL_RANGE).text()
            if page_range == "全部":
                page_range = ""
            pages_text = self._table.item(row, self.COL_PAGES).text()
            page_count = int(pages_text) if pages_text.isdigit() else 0
            ori_map = {"竖": "portrait", "横": "landscape", "混": "mixed"}
            ori_text = self._table.item(row, self.COL_ORIENT).text()
            orientation = ori_map.get(ori_text, "")
            jobs.append(PrintJob(file_path=file_path, copies=copies, duplex=duplex,
                                 page_range=page_range, page_count=page_count,
                                 orientation=orientation))
        self._config.jobs = jobs

    # ---- 事件处理 ----

    def _on_table_selection(self):
        """表格选中行变化 → 右侧编辑面板同步。"""
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
        # 设置总页数用于范围校验
        pages_text = self._table.item(row, self.COL_PAGES).text()
        total_pages = int(pages_text) if pages_text.isdigit() else 0
        self._edit_page_range.set_total_pages(total_pages)
        self._edit_page_range.set_ranges("" if page_range == "全部" else page_range)

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
        if row < 0:
            return  # 未点击在有效行上，不弹出菜单
        menu = QMenu(self)
        remove_action = menu.addAction("移除选中")
        action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if action == remove_action:
            self._on_remove_selected()

    def _auto_apply_edit(self):
        """编辑面板参数变更 → 即时写回选中行。"""
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        self._table.item(row, self.COL_COPIES).setText(str(self._edit_copies.value()))
        self._table.item(row, self.COL_DUPLEX).setText("双面" if self._edit_duplex.currentIndex() == 0 else "单面")
        page_range = self._edit_page_range.get_ranges()
        self._table.item(row, self.COL_RANGE).setText(page_range if page_range else "全部")

        self._recalc_row_cost(row)
        self._update_total_cost()

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
        converter = get_converter()
        converted = 0
        allowed_types = {
            ".pdf",
            ".doc", ".docx",
            ".txt", ".md", ".html", ".htm",
            ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp",
        }
        image_exts = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
        # 记录添加前的行数，避免重复转换已有文件
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
            elif ext in (".docx", ".doc"):
                orientation = get_docx_orientation(f)
            elif ext in image_exts:
                info = get_image_info(f)
                page_count = info["page_count"]
                orientation = info["orientation"]

            job = PrintJob(file_path=f, copies=1, duplex="on", page_range="",
                           page_count=page_count, orientation=orientation)
            self._config.jobs.append(job)
            self._add_table_row(job)

        self._log(f"已添加 {len(files)} 个文件")

        # 仅对新添加的非 PDF/非图片文件转为临时 PDF 以统计页数
        for row in range(start_row, self._table.rowCount()):
            file_path = self._table.item(row, self.COL_FILE).data(Qt.UserRole)
            ext = os.path.splitext(file_path)[1].lower()
            if ext == ".pdf" or ext in image_exts:
                continue

            self._table.item(row, self.COL_PAGES).setText("...")
            QApplication.processEvents()

            temp_pdf: str | None = None
            try:
                temp_pdf = converter.convert(file_path)
                info = get_pdf_info(temp_pdf)
                page_count = info["page_count"]
                orientation = info["orientation"]

                self._table.item(row, self.COL_PAGES).setText(str(page_count))
                ori_map = {"portrait": "竖", "landscape": "横", "mixed": "混"}
                self._table.item(row, self.COL_ORIENT).setText(ori_map.get(orientation, ""))
                self._recalc_row_cost(row)

                converted += 1
            except Exception as e:
                self._table.item(row, self.COL_PAGES).setText("?")
                logger.warning(f"预转换失败 ({os.path.basename(file_path)}): {e}")
            finally:
                if temp_pdf and os.path.isfile(temp_pdf):
                    try:
                        os.remove(temp_pdf)
                    except OSError as e:
                        logger.warning(f"无法删除临时 PDF ({temp_pdf}): {e}")

        self._update_total_cost()
        if converted > 0:
            self._log(f"已预转换 {converted} 个文件以统计页数")

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
        """清空任务列表。"""
        if self._config.jobs:
            ret = QMessageBox.question(self, "确认", "确定要清空所有任务吗？")
            if ret != QMessageBox.Yes:
                return
        self._table.setRowCount(0)
        self._config.jobs.clear()
        self._update_total_cost()
        self._log("已清空任务列表")

    def _on_start_print(self):
        """开始打印。"""
        self._refresh_config_jobs_from_table()
        self._sync_ui_to_config()

        if not self._config.jobs:
            QMessageBox.warning(self, "提示", "请先添加要打印的文件。")
            return

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
        """单个任务完成回调：更新表格行颜色。"""
        if idx < self._table.rowCount():
            fg = QColor("#6b9e6b") if success else QColor("#c96b6b")
            for col in range(7):
                item = self._table.item(idx, col)
                if item:
                    item.setForeground(fg)

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
            "<h3>HN 本地打印工具 v2.4</h3>"
            "<p>本地文件一键打印工具，支持多种文件格式。</p>"
            "<p>支持拖放添加、自动计费、浅色/深色主题切换。</p>"
            "<hr>"
            "<p>核心流程：文件 → PDF → Windows 原生 GDI 打印</p>"
            "<p>外部工具（可选）：LibreOffice | wkhtmltopdf | SumatraPDF</p>"
            "<p>技术：PySide6 + PyMuPDF + PyPDF2 + python-docx</p>"
        )

    def _on_shortcuts(self):
        """快捷键说明对话框。"""
        QMessageBox.information(
            self, "快捷键",
            "<table cellspacing='8'>"
            "<tr><td><b>Ctrl+C</b></td><td>复制合计金额</td></tr>"
            "<tr><td><b>Ctrl+Shift+C</b></td><td>复制计费明细</td></tr>"
            "<tr><td><b>Delete</b> / <b>Ctrl+D</b></td><td>删除选中任务</td></tr>"
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

    # ---- 关闭事件 ----

    def closeEvent(self, event):
        """关闭窗口时自动保存配置。"""
        try:
            self._refresh_config_jobs_from_table()
            self._sync_ui_to_config()
            self._config.save(self._config_path)
            self._log(f"配置已自动保存至: {self._config_path}")
        except Exception as e:
            logger.warning(f"自动保存配置失败: {e}")
        super().closeEvent(event)
