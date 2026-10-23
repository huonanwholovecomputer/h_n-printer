"""
gui.py — PySide6 主界面 MainWindow + 打印工作线程 PrintWorker
HN 本地打印工具 — 支持浅色/深色双主题
"""

from __future__ import annotations

import json
import logging
import math
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
    QEventLoop,
    QElapsedTimer,
    QPropertyAnimation,
    QEasingCurve,
    QUrl,
    QItemSelectionModel,
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
    QFrame,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
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
    QPlainTextEdit,
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
    QFormLayout,
)

from printer_config import PrinterConfig, PrintJob, TabSettings, calc_cost, _count_pages_in_range, DEFAULT_OWNER_NAME, PLACEHOLDER_OWNER_NAMES, FALLBACK_OWNER_NAME, PrinterProfile, format_duration
from converter import get_converter, UniversalConverter
from pdf_printer import (print_pdf, list_system_printers, get_pdf_info, get_docx_orientation,
                         get_image_info, estimate_print_sides, estimate_manual_duplex_sides,
                         print_plan_pass, wait_for_output, count_pdf_pages)
import manual_duplex  # 手动双面两趟页序引擎（无自动双面器的机型；页序唯一真相）
import duplex_stack  # 攒批模式（多任务合并翻纸）的纸叠账本（纯逻辑，见 duplex_stack.py）
from theme_manager import ThemeManager, MODE_SYSTEM, MODE_LIGHT, MODE_DARK, MODE_LABELS
from cloud_client import CloudClient, CloudTask, cache_engine_for, pdf_cache_key, order_status_label_cn
from stats_server import StatsServer, load_local_data_file, clear_local_data_files
from offline_sync import OfflineSync
import staging  # 添加文件的「工作副本」暂存（压缩包拖拽兼容，详见 staging.py）
import paths    # 用户数据目录（攒批批次目录 duplex_stack_dir 在这里）

logger = logging.getLogger(__name__)

# 应用版本号（与 HN打印工具.spec 引用的 version_info.txt 保持一致，升级时两处同步递增）
APP_VERSION = "4.5.33"

# 冻结预约单的本地自恢复等待秒数（🔴-B3）：后端只在 file_ready 那一瞬清 schedule_frozen，
# 最后一次 file_ready 早于冻结置位时两侧会**永久冻结** ⇒ 到点后仍冻结就由本机自行解冻打印。
# 提为模块级常量便于验收脚本把它临时调成 0（否则测试要真等 30 秒）。
SCHEDULED_SELF_HEAL_S = 30

# 云端取号（/api/next_order_number + /api/local_orders 上报）在主线程等待的上限（秒）。
# S1-09：旧值是 **30 秒** —— "连着但慢"（TCP 半开、代理抖动、云端 502 后重试）时，
# 点「开始打印」会冻结 30 秒（期间靠 processEvents 保活，界面基本不可用）。
# 现在 4 秒（3–5 s 区间）就放弃等待、**直接用本地 -L 临时号开打**（连线后由换号流程补报），
# 取号慢不该拦住出纸。⚠ 别调回 30：这是现场"卡 30 秒不打印"的直接原因。
ORDER_NUMBER_WAIT_S = 4

# 无障碍自动打印的防抖窗口（毫秒）。同一订单的文件是**逐个下载完成**推送的，间隔可达数秒；
# 原来的 3000ms 会被"击穿"——每批先到的文件立即被当成整单处理并把队列 pop 清空
# （🔴-B4-2 现场实测：订单 #535 的 11 个文件分 7 批打印、同一订单号被上报 4 次）。
# 这是客户端能做的缓解；**根治**需后端在 push/accept 响应里下发订单级 file_count，
# 由客户端按"已到 / 总数"判定整单到齐（见 docs/修复计划_2026-10.md 批次 E）。
AUTO_PRINT_DEBOUNCE_MS = 8000

# ---- 标签页选择器几何口径（2026-10）----
# 需求：**无论有没有状态图标，−/＋ 按钮的位置都不许动**。
# 旧实现的 🔒/⏸ 是数字标签里的**文字前缀**，且随状态把标签宽度在 36/44/52 之间来回改：
# 标签一宽，右边的 ＋ 就被推着挪 8~16px（用户要按的位置会跑）。
# 新机制：数字标签只显示数字、宽度恒定；状态由**独立徽标**承担，徽标槽位恒定占位
# （无状态时只清空文字，既不 setVisible(False)——隐藏控件会被布局跳过同样挪位，
# 也不改宽度）。⇒ 加减按钮的几何与状态完全无关。
TAB_SWITCH_BTN = 32     # − / ＋ 按钮：圆角正方形（QSS 的 min/max 同值，改动需同步两处）
TAB_NUM_WIDTH = 36      # 数字标签：固定宽度，只放数字
TAB_BADGE_WIDTH = 24    # 状态徽标（🔒 已固定 / ⏸ 待人工翻纸 / 空）固定宽度
# 附加服务行左侧的「[徽标]标签页 N」同样按固定宽度处理（旧写法把 ⏸ 的说明拼进这一个标签，
# 文字一长就把右边的 派送/地点/优先级/首页 整排推走 —— 与加减按钮同一类隐患）。
# 78px：实测 12px 粗体下「标签页 12」= 72px（余量 ≥4px 由验收脚本按真实字体度量把关）。
TAB_SCOPE_WIDTH = 78

# 标签页状态的说明文案（唯一真相）：徽标/标签的 tooltip 用它，
# **不再拼进可见文字**（可见文字一长就会推走同行控件）。
TAB_FROZEN_HINT = "该标签页已固定（打印完成），不可再编辑。\n如需重打，请新建标签页。"
TAB_FLIP_HINT = ("本标签页有双面文件，而当前打印机不支持自动双面：\n"
                 "预约打印到点不会自动开始，需要人工点「开始打印」并在中途翻纸。\n"
                 "（无障碍打印不在此列：顾客要求的就是无人值守，会直接开始第 1 趟。）")


# ============================================================
# 辅助工具
# ============================================================

# 优先级档位业务顺序（低→中→高）：主界面下拉与价格同步统一按此排序，
# 不依赖接口/配置文件的键顺序（后端 jsonify 会按 Unicode 排序，如 中→低→高）。
URGENCY_ORDER = ("低", "中", "高")


def _sort_urgency_keys(keys) -> list:
    """把优先级档位键按 低→中→高 排序；未知档位按原顺序追加在后。"""
    keys = list(keys)
    known = [k for k in URGENCY_ORDER if k in keys]
    rest = [k for k in keys if k not in URGENCY_ORDER]
    return known + rest


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
            color = QColor("#6e6e6e") if dark else QColor("#9ca0b0")
        else:
            color = QColor("#ffffff")
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


# ── PDF 转换引擎下拉（编辑面板）─────────────────────────────────────────────
# 前 3 项是真实引擎（index ↔ 引擎名一一对应）；第 4 项只在「选区里的 Word 文件引擎不一致」时
# 出现，选中它 = **不写回**（各自保留原设置）。只读标签（「直接打印/图片渲染/…」）是另一回事，
# 它只有一个 item，且下拉整体禁用。
ENGINE_BY_INDEX = {0: "word", 1: "wps", 2: "libreoffice"}   # 索引 → 引擎名
ENGINE_INDEX_OF = {v: k for k, v in ENGINE_BY_INDEX.items()}  # 引擎名 → 索引（别把两者搞反）
ENGINE_CHOICE_LABELS = ["Word", "WPS", "LibreOffice"]
ENGINE_MIXED_LABEL = "混合（各自保留）"
ENGINE_MIXED_TIP = "选中的 Word 文档引擎不一致，当前各自保留原设置；选一个具体引擎可统一改掉"

# S0-09 / D-2（2026-10-23）：HTML 打印支持**永久删除**（转换器已不注册 .html/.htm，
# 且本机转换器禁止本地文件访问、禁用 JS）。拒绝文案只维护这一份，拖入/粘贴/对话框共用。
HTML_UNSUPPORTED_MSG = "本工具已不支持 HTML 文件，请另存为 PDF/DOCX 后打印"


def _format_engine_label(ext: str) -> str:
    """返回某文件格式实际使用的 PDF 转换引擎名（参数面板灰色显示用）。"""
    if ext in (".doc", ".docx"):
        return "Word"
    if ext == ".md":
        return "markdown 库"
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
    """生成持久化客户端 ID：优先用户级目录（%APPDATA%\\HN打印工具），
    升级/覆盖安装应用目录时不丢失 .client_id，避免同一台机器每次升级生成新 ID
    （否则收支清算「授权」页会堆积同机旧设备，如 DESKTOP-xxx-aaaa 与 DESKTOP-xxx-bbbb 并存）。
    兼容旧版：应用目录已存在的 .client_id 优先复用并迁移到用户级目录（升级后 ID 保持不变）。

    同机多实例共用 hostname 会导致云端 client_id 冲突（任务可能被错误派发/回滚），
    因此追加一个随机后缀并持久化，重启后保持不变。
    """
    import socket as _socket
    import uuid
    hostname = _socket.gethostname()
    app_dir = os.path.dirname(os.path.abspath(__file__))
    user_dir = os.path.join(os.environ.get("APPDATA", ""), "HN打印工具") if os.environ.get("APPDATA") else ""
    user_file = os.path.join(user_dir, ".client_id") if user_dir else ""
    app_file = os.path.join(app_dir, ".client_id")
    suffix = ""
    # 读取：用户级优先，其次旧版应用目录（读到即迁移）
    for candidate in (user_file, app_file):
        if not candidate:
            continue
        try:
            if os.path.isfile(candidate):
                with open(candidate, "r", encoding="utf-8") as f:
                    s = f.read().strip()
                if s:
                    suffix = s
                    break
        except OSError:
            continue
    if not suffix:
        suffix = uuid.uuid4().hex[:10]
    # 写入：用户级优先（升级不丢）；失败退回应用目录
    written = False
    if user_dir:
        try:
            os.makedirs(user_dir, exist_ok=True)
            with open(user_file, "w", encoding="utf-8") as f:
                f.write(suffix)
            written = True
        except OSError:
            pass
    if not written:
        try:
            with open(app_file, "w", encoding="utf-8") as f:
                f.write(suffix)
        except OSError:
            pass
    return f"{hostname}-{suffix}" if suffix else hostname


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


def _fit_message_box_buttons(box: QMessageBox | None) -> None:
    """把 QMessageBox 的自定义按钮按**文字实际宽度**撑开（2026-10-23 用户截图反馈）。

    现象：「关闭攒批列表」那个三选框底部按钮只剩中间几个字（"留（纸叠与订单都留着）"、
    "弃整叠（1 单全部按失败收尾）"），首尾被裁掉 → 最容易选错的三个选项恰好读不全。

    成因：Qt 的 QMessageBox 把整行按钮压进对话框宽度里，**不换行、也不为按钮撑宽对话框**
    ——文字比可用宽度长时按比例把每个按钮压窄（实测 sizeHint 190px 的文字被压成 114px，
    而 minimumSizeHint 仍是 190px，即布局被 forced）。所以这里显式把每个按钮的最小宽度
    设成它自己的 sizeHint ⇒ 对话框随按钮一起变宽（实测 400 → 589px）。

    只对**手写文字**的按钮有意义（标准 Yes/No 很短，设了也不会变宽）；调用点在
    `box.exec()` 之前（`addButton` 之后）。
    """
    if box is None:
        return
    try:
        for b in box.buttons():
            need = int(b.sizeHint().width())
            if need > int(b.minimumWidth()):
                b.setMinimumWidth(need)
    except Exception as e:      # 纯版式辅助：失败也不能影响对话框照常弹出
        logger.debug(f"调整对话框按钮宽度失败（忽略）: {e}")


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

# 页码范围一行输入的**跨度上限**（仅在总页数未知时用于兜底）。
# 为什么需要：`_parse_range` 原先用 `set(range(start, end + 1))` 且**完全无上限**，
# 手输 "1-999999999" 会直接申请 30+ GB（表 + int 对象）→ MemoryError / 界面卡死
# （2026-10 审查 🔴-1）。跨度是"页码"不是"循环次数"——1 万页以上的一行输入在真实
# 文档里不存在，超过即按上限截断（只影响集合内容，不影响"格式是否合法"的判定）。
_RANGE_SPAN_CAP = 10000


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
            pages = self._parse_range(t, self._total_pages)
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
            pages = self._parse_range(t, self._total_pages)
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
            pages = self._parse_range(t, self._total_pages)
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
    def _parse_range(text: str, total_pages: int = 0) -> set[int] | None:
        """解析单个范围字符串（如 "1-5"、"7"），start > end 视为格式错误。

        P0（2026-10 审查 🔴-1）：迭代前先收窄区间再建集合。
          · total_pages > 0：收窄到 [1, total_pages]；越界时额外塞一个哨兵
            `total_pages + 1`，让调用方 `max(pages) > total_pages` 的「超限」判据
            与改前**完全一致**（合法输入得到的集合与改前逐元素相同）。
          · total_pages == 0（页数未知）：按 `_RANGE_SPAN_CAP` 截断，只防内存炸弹。
        """
        text = text.strip()
        if not text:
            return None
        try:
            if "-" in text:
                a, b = text.split("-", 1)
                start, end = int(a), int(b)
                if 1 <= start < end:
                    if total_pages > 0:
                        pages = set(range(start, min(end, total_pages) + 1))
                        if end > total_pages:
                            pages.add(total_pages + 1)   # 越界哨兵
                        return pages
                    return set(range(start, min(end, start + _RANGE_SPAN_CAP - 1) + 1))
                return None
            else:
                v = int(text)
                return {v} if v >= 1 else None
        except ValueError:
            return None


class ManualDuplexDialog(QDialog):
    """手动双面**常驻**提示窗（对齐 HP 官方工具：打印一开始就在，而不是打完才弹）。

    HP 驱动的手动双面向导从打印开始就常驻在屏幕上，操作员不必等弹窗、也不会被突然弹出的
    窗口打断；我们照做，用状态机切换同一个窗口：
      ① 批次开始 → 窗口出现：「正在打印第 1 趟」（继续按钮禁用）
      ② 第 1 趟出纸 → 「可翻纸」：按钮启用 + 倒计时 + 出纸张数提示
      ③ 点了继续 → 「正在打印第 2 趟」（按钮禁用，防重复点）
      ④ 批次结束 → 自动收起
    **关窗 = 跳过**（绝不因为关窗就擅自把第 2 趟打了）。
    """

    continue_requested = Signal()   # 操作员确认已翻纸 → 继续第 2 趟
    skip_requested = Signal()       # 操作员跳过本任务（或超时/关窗）
    # ⚠ 本窗**只管单任务**手动双面。攒批（多任务合并翻纸）不走这个窗口：第 1 趟不弹窗、
    #   不计时，第 2 趟由「攒批列表」的「▶ 开始打印第 2 趟」+ 落机对账对话框发起
    #   （2026-10-20 用户要求：攒批第一趟弹这个窗、还从第一趟就开始计时，完全是复用旧窗导致的误伤）。

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("手动双面打印")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setMinimumWidth(600)
        self._mode = "idle"
        self._left = 0

        lay = QVBoxLayout(self)
        self._title = QLabel("手动双面打印（本机无自动双面器）")
        _f = self._title.font()
        _f.setPointSize(_f.pointSize() + 1)
        _f.setBold(True)
        self._title.setFont(_f)
        lay.addWidget(self._title)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        _sf = self._status.font()
        _sf.setBold(True)
        self._status.setFont(_sf)
        lay.addWidget(self._status)

        # 第 1 趟计时器（2026-10）：从窗口出现开始正计时，和"预计耗时"比：
        # 绿=在预估内（含 30 秒宽容带）／黄=超出 30 秒以上／红=超出 1 分钟以上。
        # 实时变色本身就是预警 —— 打一半就变红通常意味着缺纸/卡纸/打印机离线。
        self._pass1 = QLabel("")
        _pf = self._pass1.font()
        _pf.setBold(True)
        _pf.setPointSize(_pf.pointSize() + 2)
        self._pass1.setFont(_pf)
        self._pass1.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self._pass1)

        self._pass1_hint = QLabel("")
        self._pass1_hint.setStyleSheet("font-size: 11px;")
        self._pass1_hint.setWordWrap(True)
        lay.addWidget(self._pass1_hint)

        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        self._detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self._detail)

        self._countdown = QLabel("")
        self._countdown.setStyleSheet("color: #e67e22;")
        lay.addWidget(self._countdown)

        row = QHBoxLayout()
        row.addStretch(1)
        self._btn_go = QPushButton("▶ 继续打第 2 趟")
        self._btn_go.setEnabled(False)
        self._btn_go.clicked.connect(self._on_go)
        self._btn_skip = QPushButton("⏭ 跳过本任务")
        self._btn_skip.clicked.connect(self._on_skip)
        row.addWidget(self._btn_go)
        row.addWidget(self._btn_skip)
        lay.addLayout(row)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        # 第 1 趟计时器（与上面那个"翻纸确认倒计时"是两个独立计时器，别混）
        self._t0 = QElapsedTimer()
        self._pass1_sheets = 0
        self._pass1_est = 0.0
        self._pass1_final = 0.0
        self._pass1_done = False       # 用显式标志，别拿 0.0 当"还没冻结"（0 秒也是合法结果）
        self._pass1_timer = QTimer(self)
        self._pass1_timer.timeout.connect(self._pass1_tick)

        # 翻纸放行闸门（2026-10-19）：第 1 趟**回报**了、但按预估还没打完时，按钮先不放行。
        self._speed_hint = ""          # 预估依据那句话（set_speed_hint 设置，闸门文案要保留它）
        self._flip_gate_at = 0.0       # 闸门时刻（第 1 趟第几秒，0 = 无闸门）
        self._flip_text = ""           # 翻纸说明（闸门期间先存着，到点再铺到详情里）
        self._flip_timeout = 0         # 翻纸确认超时（同上）
        # 放行策略（来自打印队列档案：「帮助 → 打印速度…」可调）
        self._gate_ratio = self.FLIP_GATE_RATIO
        self._gate_extra = 0.0

    # ── 第 1 趟计时/预估 ──

    PASS1_GREEN = "#27ae60"      # 在预估内（含宽容带）
    PASS1_YELLOW = "#e67e22"     # 超出预估 30 秒以上
    PASS1_RED = "#e74c3c"        # 超出预估 1 分钟以上
    PASS1_GRACE_SEC = 30         # 宽容带：冷机首张出纸波动可达 10~20 秒，30 秒内不报警

    # ── 「▶ 继续打第 2 趟」的放行闸门（2026-10-19 现场；2026-10-20 改成比例放行）──
    # 主机型打印机（LaserJet 1020）**只回报「数据送进端口」，不回报「纸打完」**：换原生
    # 1:1 渲染策略后，第 1 趟的"完成回报"从 60~70 秒塌到 1~2 秒（实测 16 张纸 2.1 秒），
    # 纸还在出，窗口就说"第 1 趟已打完"了。打印机行为干涉不了，但**按钮不能信它**。
    #
    # 2026-10-20 用户反馈：旧口径「预估 − 5 秒」不合适 —— **纸越多，预估误差越大**，
    # 固定 5 秒在大批量上要么太保守（白等）、要么偏早。改为**按比例放行**：
    #     放行时刻 = 第 1 趟预估 × gate_ratio（默认 50%） + flip_ready_sec（人工翻页准备时间）
    # 两个参数都进「帮助 → 打印速度…」由操作员调（`PrinterProfile.gate_ratio/flip_ready_sec`）。
    FLIP_GATE_RATIO = 0.5         # 默认：预估完成 50% 后允许点「继续打第 2 趟」
    FLIP_GATE_MIN_SEC = 1.0       # 算出来 < 1 秒的闸门没有意义，直接放行

    @classmethod
    def flip_gate_at(cls, estimate: float, ratio: float | None = None,
                     extra: float = 0.0) -> float:
        """「▶ 继续打第 2 趟」最早可在第 1 趟第几秒启用（0 = 不设闸门）。

        闸门时刻 = 预估 × ratio + extra（extra = 人工翻页准备时间，默认 0）：
        ratio 默认 50%（用户口径「预估时间完成 50% 之后允许点击」），可在
        「帮助 → 打印速度…」里改。预估为空/0 或算出 < 1 秒 ⇒ 不设闸门。"""
        est = float(estimate or 0.0)
        if est <= 0:
            return 0.0
        try:
            r = float(cls.FLIP_GATE_RATIO if ratio is None else ratio)
        except (TypeError, ValueError):
            r = cls.FLIP_GATE_RATIO
        r = min(max(r, 0.0), 1.0)
        try:
            x = max(0.0, float(extra or 0.0))
        except (TypeError, ValueError):
            x = 0.0
        gate = est * r + x
        return gate if gate >= cls.FLIP_GATE_MIN_SEC else 0.0

    @staticmethod
    def pass1_color(elapsed: float, estimate: float) -> str:
        """计时器颜色：绿=在预估内(+30s 宽容带) / 黄=超出 30 秒以上 / 红=超出 1 分钟以上。"""
        if estimate <= 0:
            return ManualDuplexDialog.PASS1_GREEN
        delta = float(elapsed) - float(estimate)
        if delta <= ManualDuplexDialog.PASS1_GRACE_SEC:
            return ManualDuplexDialog.PASS1_GREEN
        if delta <= 60:
            return ManualDuplexDialog.PASS1_YELLOW
        return ManualDuplexDialog.PASS1_RED

    def _elapsed_sec(self) -> float:
        """第 1 趟的**实时**已用秒数（独立于"冻结值"：提前回报时计时必须继续走）。

        单独抽成一个方法是为了让验收脚本能把它换成任意时刻（不然要真等几十秒）。"""
        return self._t0.elapsed() / 1000.0

    def is_early_signal(self, seconds: float) -> bool:
        """这笔"第 1 趟实测"是不是**早于闸门**的提前回报（打印机只回报"开始打印"）？

        是的话：① 不拿它冻结界面计时（真实用时还在走）；② **不写进速度样本** ——
        16 张 2 秒这种数据一旦入库，两参数拟合立刻反向，预估塌到 10 秒以内，
        上面这道闸门自己就失效了（现场 2026-10-18~19 就是这么塌的）。"""
        gate = self.flip_gate_at(getattr(self, "_pass1_est", 0.0),
                                 getattr(self, "_gate_ratio", None),
                                 getattr(self, "_gate_extra", 0.0))
        if gate <= 0:
            return False
        try:
            return float(seconds) < gate
        except (TypeError, ValueError):
            return False

    def _pass1_text(self, elapsed: float, *, final: bool = False) -> str:
        est = getattr(self, "_pass1_est", 0.0)
        head = f"⏱ 第 1 趟{'用时' if final else '已用'} {format_duration(elapsed)}"
        if est > 0:
            head += f" / 预计 {format_duration(est)}"
        if est > 0 and elapsed > est:
            over = int(elapsed - est)
            head += f"（超出 {over} 秒）"
        return head

    def _pass1_tick(self):
        """每 0.5 秒刷新；第 1 趟已冻结（_pass1_done）就停在那里，不再继续走。"""
        elapsed = self._pass1_final if self._pass1_done else self._elapsed_sec()
        self._pass1.setText(self._pass1_text(elapsed, final=self._pass1_done))
        self._pass1.setStyleSheet(f"color: {self.pass1_color(elapsed, self._pass1_est)};")

    def freeze_pass1(self, seconds: float | None = None):
        """冻结第 1 趟计时。传 seconds 时用**程序实测的权威值**（worker 报的），
        不传就用界面自己的时钟兜底（信号没到的异常路径，例如第 1 趟失败/取消）。

        为什么要以实测为准：界面时钟会受主线程卡顿、窗口动画、我们自己的静置余量影响；
        程序实测（第 1 趟开打 → 纸出完）才是"这台打印机真正花的时间"，两者不一致时
        以实测为准，并把差值记下来（主线程卡顿/时序异常的证据）。"""
        if self._pass1_done:
            return
        self._pass1_done = True
        self._pass1_timer.stop()
        local = self._elapsed_sec()
        try:
            authoritative = float(seconds) if seconds is not None else None
        except (TypeError, ValueError):
            authoritative = None
        if authoritative is not None and authoritative >= 0:
            if abs(local - authoritative) > 5.0:
                self._pass1_hint.setText(
                    f"{self._pass1_hint.text()}\n（界面计时与程序实测差 {abs(local - authoritative):.0f} 秒，"
                    f"以程序实测 {format_duration(authoritative)} 为准）".strip())
            self._pass1_final = authoritative
        else:
            self._pass1_final = local
        self._pass1_tick()

    def _pass1_freeze(self):
        """内部用：界面时钟兜底冻结（保留最终用时，供操作员核对）；重复调用无副作用。"""
        self.freeze_pass1(None)

    # ── 状态切换（全部由 worker 信号 / 本窗按钮驱动）──

    def announce(self, text: str, sheets: int = 0, estimate_sec: float = 0.0,
                 gate_ratio: float | None = None, gate_extra: float = 0.0):
        """批次开始：窗口先立起来（常驻），此时还不能翻纸；第 1 趟计时从这一刻起。

        gate_ratio / gate_extra：本机档案的放行策略（预估百分比 + 人工翻页准备秒数）。"""
        self._timer.stop()
        self._mode = "pass1"
        if gate_ratio is not None:
            try:
                self._gate_ratio = min(max(float(gate_ratio), 0.0), 1.0)
            except (TypeError, ValueError):
                self._gate_ratio = self.FLIP_GATE_RATIO
        try:
            self._gate_extra = max(0.0, float(gate_extra or 0.0))
        except (TypeError, ValueError):
            self._gate_extra = 0.0
        self._status.setText("① 正在打印第 1 趟…（程序自己分两趟，中途只需要翻一次纸）")
        self._detail.setText(text)
        self._countdown.setText("")
        self._btn_go.setText("▶ 继续打第 2 趟（等第 1 趟打完）")
        self._btn_go.setEnabled(False)
        self._btn_skip.setText("⏭ 跳过本任务")
        self._btn_skip.setEnabled(True)
        # 第 1 趟计时/预估：操作员看到的"窗口出现"就是计时起点
        self._pass1_sheets = int(sheets or 0)
        self._pass1_est = float(estimate_sec or 0.0)
        self._pass1_final = 0.0
        self._pass1_done = False
        self._flip_gate_at = 0.0
        self._flip_text = text
        self._flip_timeout = 0
        self._left = 0                  # 翻纸确认倒计时：本批还没开始（闸门到点才起算）
        self._t0.restart()
        self._pass1_tick()
        self._pass1_timer.start(500)

    def set_speed_hint(self, text: str):
        """预估依据（"启动 ≈8s + 每张 ≈3.5s · 基于最近 N 次实测"）——鼠标悬停/小字显示。

        原文留一份（`_speed_hint`）：闸门期间的提示要在这句后面**追加**一句"为什么还不能翻"，
        到点后再还原，别把预估依据冲掉。"""
        self._speed_hint = text or ""
        self._pass1_hint.setText(self._speed_hint)
        self._pass1.setToolTip(self._speed_hint)

    def set_flippable(self, text: str, timeout_sec: int):
        """第 1 趟**回报**出纸。按时预估还没到点（打印机提前回报）就先不放开按钮。

        "回报"不等于"打完"：主机型打印机只回报数据送进端口。放行时刻见 `flip_gate_at`。"""
        self._flip_text = text
        self._flip_timeout = max(1, int(timeout_sec))
        gate = self.flip_gate_at(getattr(self, "_pass1_est", 0.0),
                                 getattr(self, "_gate_ratio", None),
                                 getattr(self, "_gate_extra", 0.0))
        # 闸门绝不能把操作员拖到 worker 的「等翻纸」超时之后（超时按"半成品失败"收尾、
        # 纸白打）：极端大批次（预估 >10 分钟 ≈ 175 张以上）才可能撞到，但撞上就是真失败。
        # worker 的等待是 FLIP_TIMEOUT_SEC + FLIP_GRACE_SEC（600 + 30），这里是它的保守下限。
        if gate > 0 and self._flip_timeout > 0:
            gate = min(gate, float(self._flip_timeout))
        # 手上要是已经有一笔**可信**的实测（不早于闸门）⇒ 第 1 趟确实打完了，直接放行
        measured_done = bool(gate > 0 and self._pass1_done and self._pass1_final >= gate)
        if gate > 0 and not measured_done and self._elapsed_sec() + 0.5 < gate:
            self._flip_gate_at = gate
            self._left = 0              # 闸门期间**不**扣翻纸确认倒计时（到点才起算）
            self._mode = "flip_gate"
            self._paint_flip_gate()
            self._timer.start(500)      # 闸门期间半秒一刷（自己数着，不看打印机的回报）
            return
        self._open_flip_gate()

    def _paint_flip_gate(self):
        """闸门未开时的文案：说清"为什么还不能翻"和"还要等多久"。"""
        left = max(0.0, self._flip_gate_at - self._elapsed_sec())
        est = float(getattr(self, "_pass1_est", 0.0) or 0.0)
        self._status.setText(
            "② 第 1 趟还在出纸：打印机只回报「开始打印」、不回报「打完」—— "
            "按预估等到点再翻纸")
        self._detail.setText(self._flip_text)
        self._btn_go.setText(f"▶ 继续打第 2 趟（还要等 {math.ceil(left)} 秒）")
        self._btn_go.setEnabled(False)
        self._btn_skip.setText("⏭ 跳过本任务")
        self._btn_skip.setEnabled(True)
        self._countdown.setStyleSheet("color: #e67e22;")
        _ratio = int(round(float(getattr(self, "_gate_ratio", self.FLIP_GATE_RATIO)) * 100))
        _extra = float(getattr(self, "_gate_extra", 0.0) or 0.0)
        self._countdown.setText(
            f"⏳ 预估第 1 趟共 {format_duration(est)}，按「预估 {_ratio}%"
            + (f" + 翻页准备 {_extra:.0f} 秒" if _extra > 0 else "")
            + f"」放行，还有 {math.ceil(left)} 秒")
        self._countdown.setToolTip("放行比例与人工翻页时间可在「帮助 → 打印速度…」里调")
        note = ("（打印机的「完成」回报早于实机出纸，计时与按钮都以预估为准；"
                "这笔回报不会被写进速度样本）")
        self._pass1_hint.setText(f"{self._speed_hint}\n{note}".strip() if self._speed_hint
                                 else note)

    def _open_flip_gate(self):
        """闸门到点（或本来就无需闸门）：允许翻纸 + 冻结计时 + 开始翻纸确认倒计时。

        冻结用的是**界面实时时钟**：此时那笔提前回报（1~2 秒）没资格代表第 1 趟用时
        （真实用时还在走，见 `is_early_signal`）。"""
        self._flip_gate_at = 0.0
        self._pass1_freeze()
        self._mode = "flip"
        self._status.setText("② 第 1 趟已打完 —— 请翻纸，然后点「继续打第 2 趟」")
        self._detail.setText(self._flip_text)
        self._btn_go.setText("▶ 继续打第 2 趟")
        self._btn_go.setEnabled(True)
        self._left = max(1, int(self._flip_timeout or 600))
        self._tick()
        self._timer.start(1000)
        if self._speed_hint:
            self._pass1_hint.setText(self._speed_hint)

    def finish(self, text: str = ""):
        """批次结束：收起窗口（稍候自动关，避免一直盖在屏幕上）。"""
        self._timer.stop()
        if self._pass1_timer.isActive() and not self._pass1_done:
            self._pass1_freeze()      # 异常路径（第 1 趟失败/取消）也要留下最终用时
        self._pass1_timer.stop()
        self._mode = "done"
        self._status.setText(text or "手动双面批次已结束")
        self._countdown.setText("")
        self._flip_gate_at = 0.0
        self._btn_go.setEnabled(False)
        self._btn_skip.setText("关闭")
        self._btn_skip.setEnabled(True)
        QTimer.singleShot(1500, self.close)

    def _tick(self):
        if self._mode == "flip_gate":
            # 等闸门到点：**不**扣翻纸确认倒计时（那 10 分钟从"真的能翻纸"那一刻才算），
            # 也不看打印机的回报（它只回报开始打印）。
            if self._elapsed_sec() >= self._flip_gate_at:
                self._open_flip_gate()
            else:
                self._paint_flip_gate()
            return
        if self._mode != "flip":
            return
        self._left -= 1
        if self._left <= 0:
            self._timer.stop()
            self._countdown.setText("⏳ 长时间无人确认 → 按「跳过本任务」处理")
            self._on_skip()
            return
        self._countdown.setText(
            f"⏳ 还有 {self._left // 60} 分 {self._left % 60} 秒（超时按跳过处理）")

    def _on_go(self):
        if self._mode != "flip":
            return
        self._timer.stop()
        self._mode = "pass2"
        self._status.setText("③ 正在打印第 2 趟…（请勿再动纸叠）")
        self._countdown.setText("")
        self._btn_go.setText("第 2 趟打印中…")
        self._btn_go.setEnabled(False)
        self._btn_skip.setEnabled(False)
        self.continue_requested.emit()

    def _on_skip(self):
        if self._mode == "skipped":
            return
        if self._mode == "done":      # 结束态下这个按钮就是"关闭"
            self.close()
            return
        # 注意：**第 1 趟还没打完时点跳过也要上报**（预置答案）—— worker 走到"等翻纸"那一步时
        # 会立刻收到 False 而不必等到超时（事件在批次开始时已清空，见 _print_manual_batch）。
        self._timer.stop()
        self._mode = "skipped"
        self._status.setText("⏭ 已跳过：第 1 趟打出的纸是半成品，请人工处理")
        self._btn_go.setEnabled(False)
        self._btn_skip.setEnabled(False)
        self.skip_requested.emit()

    def closeEvent(self, ev):
        # 还没确认就关窗 → 视为跳过（不能让关窗把第 2 趟放过去）；闸门期间同理
        if self._mode in ("flip", "flip_gate"):
            self._on_skip()
        super().closeEvent(ev)


# ── 攒批队列：账本可视化 + 「我已取走」对账入口（2026-10）──────────────────────
#
# 为什么需要它：账本原先只记「第 1 趟该打什么」，从不记「纸还在不在」。用户打完一趟把纸
# 拿走（例如一个 3 页**单面**文件，第 2 趟三个槽位全是空白占位），程序仍按老计划去数进纸盒
# → 日志「没有待打的第二趟的纸叠」；更糟的是之后又混进含双面内容的单，整个纸叠第 2 趟错位
# 串页。本窗把「攒了哪些单 / 哪几页 / 第 2 趟要打什么 / 哪些纸已被取走」常驻展示，
# 撤走/放回一律由账本裁决（界面不自己判断），并把清单立即落盘。
#
# 物理事实（文案必须遵守）：出纸槽 face-down、后出的纸在**上面**。整叠不翻面搬回进纸盒后，
# 进纸盒从最上面取纸 ⇒ 第 2 趟取纸顺序 = 第 1 趟印刷顺序的逆序
# ⇒「进纸盒里最上面那几张」= 程序计划里**最后几个槽位**。若操作员抽走的不是程序登记的那些
# 纸，第 2 趟必然错位 —— 所以只有「**按本单的 first 约定算下来、第 2 趟全为空白占位**」的项
# 允许撤走（单面文件的纸 / 双面末张的空白槽），且开打前必须对数。


def _stack_present_sheet_count(stack) -> int:
    """当前在叠张数（= 第 2 趟要送的纸数）。优先用账本的 present_sheet_count。

    兜底分支只为「账本还是旧版（没有 present_sheet_count）」时别让窗口/闸门崩掉：
    按每单的 plan_sheet_count() 累加，再退回 sheet_count。
    """
    try:
        v = getattr(stack, "present_sheet_count", None)
        if isinstance(v, int) and v >= 0:
            return v
    except Exception:
        pass
    total = 0
    for e in (getattr(stack, "entries", None) or []):
        fn = getattr(e, "plan_sheet_count", None)
        if callable(fn):
            try:
                total += max(0, int(fn()))
                continue
            except Exception:
                pass
        total += max(0, int(getattr(e, "sheet_count", 0) or 0))
    return total


def _stack_original_sheet_count(stack) -> int:
    """第 1 趟实际打出的**原始**张数（含之后被取走的）—— 对账口径的 T。

    优先用账本的 `original_sheets_count`（整叠求和的唯一真相；注意**是复数**，
    单数 `original_sheet_count` 是 StackEntry 上的字段，刻意不同名）。
    """
    try:
        v = getattr(stack, "original_sheets_count", None)
        if isinstance(v, int) and v >= 0:
            return v
    except Exception:
        pass
    total = 0
    for e in (getattr(stack, "entries", None) or []):
        fn = getattr(e, "original_sheet_value", None)
        if callable(fn):
            try:
                total += max(0, int(fn()))
                continue
            except Exception:
                pass
        total += max(0, int(getattr(e, "sheet_count", 0) or 0))
    return total


def _stack_entry_sheet_count(entry) -> int:
    """单**当前在叠**张数（撤走项不计）。"""
    fn = getattr(entry, "plan_sheet_count", None)
    if callable(fn):
        try:
            return max(0, int(fn()))
        except Exception:
            pass
    return max(0, int(getattr(entry, "sheet_count", 0) or 0))


def _stack_entry_ui_lines(entry) -> list:
    """一单的文件计划行（优先用账本的 entry.ui_lines()；旧账本时给一份只读兜底）。"""
    fn = getattr(entry, "ui_lines", None)
    if callable(fn):
        try:
            lines = fn()
            if isinstance(lines, list):
                return [dict(x) for x in lines if isinstance(x, dict)]
        except Exception as e:
            logger.debug(f"攒批队列：ui_lines 读取失败（第 {getattr(entry, 'seq', '?')} 单）: {e}")
    out = []
    for i, fp in enumerate(getattr(entry, "file_plans", None) or []):
        if not isinstance(fp, dict):
            continue
        pages = list(fp.get("pages") or [])
        dup = bool(fp.get("duplex", True))
        out.append({
            "index": i,
            "label": str(fp.get("label", "") or f"第 {i + 1} 个文件"),
            "pages": pages,
            "sheets": duplex_stack.sheets_for(len(pages), dup),
            "second": "第 2 趟内容未知（账本版本较旧）",
            # 旧账本算不出"第 2 趟是否全空白" ⇒ 一律不许撤走（fail-closed：宁可不让点，
            # 也不能让人撤走一张第 2 趟还要内容的纸，那必然整叠错页）。
            "blank_second": False,
            "taken": False,
        })
    return out


def _fmt_page_seq(seq) -> str:
    """页序 → 人话：「1 3 5」/「2 4 [空]」。

    空白占位页**固定写 `[空]`**（2026-10-20 用户要求）：它对应程序真的会走的一张空白纸
    （手动双面里每张纸两趟各占一个槽位），既不跳过、也不省纸，写清楚才好数纸。
    """
    try:
        items = list(seq or [])
    except TypeError:
        return "—"
    if not items:
        return "—"
    return " ".join("[空]" if p is None else str(p) for p in items)


def _file_pass_text(pages, duplex: bool = True, first: str = "odd") -> tuple[str, str]:
    """一个文件计划的两趟页号文本 → (第 1 趟, 第 2 趟)。

    页序**只走 manual_duplex**（唯一真相）：这里不自己配对，否则攒批列表显示的页号会和
    真正打印的页号各说一套 —— 那比不显示更糟（现场会照清单去数纸）。
    """
    try:
        pages = [int(p) for p in (pages or [])]
    except (TypeError, ValueError):
        pages = []
    if not pages:
        return "—", "—"
    try:
        plan = manual_duplex.plan_passes(
            [manual_duplex.FilePlan("", pages, bool(duplex))],
            copies=1, first=(first or manual_duplex.FIRST_ODD))
        return _fmt_page_seq(plan.pass_pages(1)), _fmt_page_seq(plan.pass_pages(2))
    except Exception as e:
        logger.debug(f"攒批列表：页序计算失败（{pages!r}）: {type(e).__name__}: {e}")
        return "—", "—"


class StackQueueDialog(QDialog):
    """🖐 攒批列表：攒批模式开启后**常驻**的左右两栏清单（2026-10-20 用户要求）。

    左栏「已攒」：已经打过第 1 趟、正等第 2 趟的文件 —— 每个文件一行，给出**具体页号**：
        第 1 趟打哪些页、第 2 趟打哪些页；该面没有内容就写 `[空]`（空白占位页）。
    右栏「等待打印」：因容量/装订方向/纸叠还没打完而**尚未入叠**的文件 —— 同样给页号，
        并写明排队原因。

    账（哪些纸已被取走）仍由 `duplex_stack` 裁决：本窗只是显示 + 「我已取走/放回」入口。
    **第 2 趟也从这里发起**（2026-10-20 用户要求）：点「▶ 开始打印第 2 趟」→ 主窗口弹数纸
    对话框（「进纸盒里应有 M 张，点是继续/点否取消」）→ 打整叠；「⏭ 放弃整叠」同样在这里。
    攒批不再复用「手动双面」那个窗（它只管单任务）。
    """

    toggle_requested = Signal(int, int, bool)    # (seq, plan_index, taken)
    start_second_requested = Signal()            # ▶ 开始打印第 2 趟（主窗口先弹数纸确认）
    abandon_requested = Signal()                 # ⏭ 放弃整叠（半成品按失败收尾）
    refresh_requested = Signal()                 # 重新读一遍纸叠/排队（主窗口回填）
    # 关闭（「关闭」按钮 / 窗口 ×）：本窗**不自己隐藏**，交给主窗口决定 ——
    # 左栏还有没打第 2 趟的纸时，主窗口会先弹确认（保留 / 放弃整叠 / 取消）。
    close_requested = Signal()

    LEFT_COLS = ["文件", "第 1 趟", "第 2 趟", "张数", "状态"]
    RIGHT_COLS = ["文件", "第 1 趟", "第 2 趟", "张数 · 页数"]
    # 列宽只给「非文件」列：文件列一律 Stretch（文件名长短差别大，固定宽会把名字挤成省略号）
    LEFT_WIDTHS = [0, 108, 108, 52, 126]
    RIGHT_WIDTHS = [0, 92, 92, 124]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🖐 攒批列表")
        self.setMinimumSize(1180, 520)
        self._stack = None
        self._busy = False
        self._sel = None

        lay = QVBoxLayout(self)
        lay.setSpacing(6)

        self._summary = QLabel("")
        _sf = self._summary.font()
        _sf.setBold(True)
        _sf.setPointSize(_sf.pointSize() + 1)
        self._summary.setFont(_sf)
        self._summary.setWordWrap(True)
        lay.addWidget(self._summary)

        self._hint = QLabel(
            "左栏＝已经攒进出纸槽的文件（第 1 趟已打，等第 2 趟）；右栏＝<b>本机已经受理、"
            "但还没能开打</b>的文件（出纸槽被当前纸叠独占 / 容量或装订方向不允许）——"
            "不是云端排队列表，云端还没下发给本机的订单不会出现在这里；纸叠打完第 2 趟后"
            "它们会自动放行。页号按「第 1 趟 / 第 2 趟」分列，<b>[空]</b> = 那一面没有内容，"
            "程序会送一张空白纸（不能跳过：漏一张，后面整叠错位）。")
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("font-size: 11px;")
        lay.addWidget(self._hint)

        self._note = QLabel("")
        self._note.setWordWrap(True)
        self._note.setStyleSheet("color: #e67e22;")
        lay.addWidget(self._note)

        split = QSplitter(Qt.Horizontal)
        self._left_group = QGroupBox("🖐 已攒（第 1 趟已打，等第 2 趟）")
        _lg = QVBoxLayout(self._left_group)
        self._tree_left = self._make_tree(self.LEFT_COLS, self.LEFT_WIDTHS)
        _lg.addWidget(self._tree_left)
        self._right_group = QGroupBox("⏳ 等待打印（还没入叠）")
        _rg = QVBoxLayout(self._right_group)
        self._tree_right = self._make_tree(self.RIGHT_COLS, self.RIGHT_WIDTHS)
        _rg.addWidget(self._tree_right)
        split.addWidget(self._left_group)
        split.addWidget(self._right_group)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([660, 500])
        lay.addWidget(split, 1)

        row = QHBoxLayout()
        self._btn_take = QPushButton("✓ 我已取走")
        self._btn_take.setEnabled(False)
        self._btn_take.clicked.connect(lambda *_: self._on_take_clicked())
        row.addWidget(self._btn_take)
        row.addStretch(1)
        self._btn_start = QPushButton("▶ 开始打印第 2 趟")
        self._btn_start.clicked.connect(lambda *_: self.start_second_requested.emit())
        self._btn_abandon = QPushButton("⏭ 放弃整叠")
        self._btn_abandon.setEnabled(False)
        self._btn_abandon.clicked.connect(lambda *_: self.abandon_requested.emit())
        self._btn_refresh = QPushButton("🔄 刷新")
        self._btn_refresh.clicked.connect(lambda *_: self.refresh_requested.emit())
        self._btn_close = QPushButton("关闭")
        self._btn_close.clicked.connect(lambda *_: self.close_requested.emit())
        row.addWidget(self._btn_start)
        row.addWidget(self._btn_abandon)
        row.addWidget(self._btn_refresh)
        row.addWidget(self._btn_close)
        lay.addLayout(row)

        self._tree_left.itemSelectionChanged.connect(self._on_left_selection)

    def _make_tree(self, cols, widths) -> "QTreeWidget":
        t = QTreeWidget()
        t.setHeaderLabels(cols)
        t.setRootIsDecorated(True)
        t.setAlternatingRowColors(True)
        t.setUniformRowHeights(False)
        t.setSelectionMode(QAbstractItemView.SingleSelection)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setWordWrap(False)                 # 页号一行放得下；太长靠 tooltip + 横向滚动
        hdr = t.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, len(widths)):
            hdr.setSectionResizeMode(i, QHeaderView.Interactive)
            t.setColumnWidth(i, widths[i])
        return t

    # ── 渲染（可反复调用：常驻窗随纸叠/排队实时刷新）──

    def render(self, stack, busy: bool = False, note: str = "",
               wait_requests=None, printing_text: str = ""):
        self._stack = stack
        self._busy = bool(busy)
        self._sel = None
        entries = list(getattr(stack, "entries", None) or []) if stack is not None else []
        present = _stack_present_sheet_count(stack) if entries else 0
        original = _stack_original_sheet_count(stack) if entries else 0
        taken = max(0, original - present)

        if entries:
            self._summary.setText(
                f"🖐 攒批列表：本叠 {len(entries)} 单 / 已攒 {present} 张纸"
                f"（第 1 趟共 {original} 张 − 已取走 {taken} 张）"
                + (f" · {printing_text}" if printing_text else ""))
        else:
            self._summary.setText("🖐 攒批列表：还没有已攒的纸叠"
                                  + (f" · {printing_text}" if printing_text else ""))

        hints = []
        if busy:
            hints.append("⏳ 打印机正在打（第 1 趟 / 整叠第 2 趟）—— 打完再改计划，现在先别动纸。")
        if getattr(stack, "tainted", False):
            hints.append(f"⚠ 本叠存疑：{getattr(stack, 'taint_reason', '') or '页序未经核对'}")
        if entries and present <= 0:
            hints.append("✅ 第 2 趟无内容：这一叠全是单面成品；点「开始第 2 趟」不出纸，按成品收尾。")
        if note:
            hints.append(note)
        self._note.setText("\n".join(hints))

        self._fill_left(entries)
        self._fill_right(list(wait_requests or []))
        n_req = len(wait_requests or [])
        n_files = sum(len(r.get("jobs") or []) for r in (wait_requests or []))
        self._left_group.setTitle(
            f"🖐 已攒（第 1 趟已打，等第 2 趟）—— {len(entries)} 单 / {present} 张")
        self._right_group.setTitle(
            f"⏳ 等待打印（还没入叠）—— {n_req} 个请求 / {n_files} 个文件")
        if entries and present <= 0:
            self._btn_start.setText("✅ 按成品收尾（第 2 趟无内容，不出纸）")
        else:
            self._btn_start.setText(
                f"▶ 开始打印第 2 趟（{len(entries)} 单 / {present} 张）"
                if entries else "▶ 开始打印第 2 趟")
        self._btn_start.setEnabled(not busy and bool(entries))
        self._btn_abandon.setEnabled(not busy and bool(entries))
        self._update_take_button()

    def _fill_left(self, entries: list):
        self._tree_left.clear()
        if not entries:
            it = QTreeWidgetItem(["（还没有攒进任何文件）", "", "", "", ""])
            it.setForeground(0, QColor("#7f8c8d"))
            self._tree_left.addTopLevelItem(it)
            return
        for entry in entries:
            try:
                self._add_entry(entry)
            except Exception as e:      # 一条坏数据不该让整窗空白
                logger.debug(f"攒批列表：第 {getattr(entry, 'seq', '?')} 单渲染失败: {e}")
                bad = QTreeWidgetItem([f"⚠ 第 {getattr(entry, 'seq', '?')} 单显示失败："
                                       f"{type(e).__name__}: {e}", "", "", "", ""])
                self._tree_left.addTopLevelItem(bad)
        self._tree_left.expandAll()

    def _add_entry(self, entry):
        seq = int(getattr(entry, "seq", 0) or 0)
        title = str(getattr(entry, "title", "") or f"第 {seq} 单")
        page_count = int(getattr(entry, "page_count", 0) or 0)
        sheets = _stack_entry_sheet_count(entry)
        lines = _stack_entry_ui_lines(entry)
        st = str(getattr(entry, "state", "") or "")
        marks = []
        if st == duplex_stack.STATE_FAILED:
            marks.append("第 1 趟失败 → 纸数存疑")
        elif st == duplex_stack.STATE_PRINTING:
            marks.append("正在打第 1 趟")
        if lines and all(bool(ln.get("taken")) for ln in lines):
            marks.append("已全部取走")

        top = QTreeWidgetItem([
            f"#{seq} {title} · 本单 {page_count} 页 / 在叠 {sheets} 张", "", "",
            f"{sheets} 张", "；".join(marks)])
        _f = top.font(0)
        _f.setBold(True)
        top.setFont(0, _f)
        if marks and (st == duplex_stack.STATE_FAILED or st == duplex_stack.STATE_PRINTING):
            top.setForeground(4, QColor("#e67e22"))
        if not lines:
            top.addChild(QTreeWidgetItem(["（本单没有可识别的文件计划）", "", "", "", ""]))
        for ln in lines:
            self._add_plan_row(top, seq, entry, ln)
        self._tree_left.addTopLevelItem(top)
        top.setFirstColumnSpanned(True)   # 必须在插入之后设，否则 Qt 不生效

    def _add_plan_row(self, top, seq: int, entry, ln: dict):
        idx = int(ln.get("index", 0) or 0)
        label = str(ln.get("label", "") or f"第 {idx + 1} 个文件")
        pages = list(ln.get("pages") or [])
        duplex = bool(ln.get("duplex", True))
        taken = bool(ln.get("taken"))
        blank = bool(ln.get("blank_second"))
        sheets = int(ln.get("sheets", 0) or 0)
        p1, p2 = _file_pass_text(pages, duplex, str(getattr(entry, "first", "odd") or "odd"))
        status = ("✔ 已取走" if taken
                  else ("🔓 可整份取走" if blank else "🔒 第 2 趟还要用"))
        child = QTreeWidgetItem([f"📄 {label}", p1, p2, f"{sheets} 张", status])
        child.setData(0, Qt.UserRole, {"seq": int(seq), "index": idx,
                                       "taken": taken, "blank": blank})
        child.setToolTip(4, ("这份第 2 趟没有内容，可以整份取走"
                             if blank and not taken else
                             "已登记取走（不再计入第 2 趟的送纸数）" if taken else
                             "第 2 趟还要用这张纸，不能取走"))
        if pages:
            child.setToolTip(1, f"第 1 趟：{p1}")
            child.setToolTip(2, f"第 2 趟：{p2}")
        if taken:
            _f = child.font(0)
            _f.setStrikeOut(True)
            # 这是**左栏**的行，列数取 LEFT_COLS（旧写 `self.COLS` 根本不存在 ⇒
            # 一标记「已取走」整单就渲染失败、整行消失；见 verify_stack_list_virtual.py）
            for c in range(len(self.LEFT_COLS)):
                child.setFont(c, _f)
                child.setForeground(c, QColor("#7f8c8d"))
        elif blank:
            child.setForeground(4, QColor("#8e44ad"))
        top.addChild(child)

    def _fill_right(self, wait_requests: list):
        self._tree_right.clear()
        if not wait_requests:
            it = QTreeWidgetItem(["（没有在等的文件）", "", "", ""])
            it.setForeground(0, QColor("#7f8c8d"))
            self._tree_right.addTopLevelItem(it)
            return
        for i, req in enumerate(wait_requests, 1):
            reason = str(req.get("reason") or "等当前纸叠打完第 2 趟")
            top = QTreeWidgetItem([f"{i}. ⏳ {reason}", "", "", ""])
            _f = top.font(0)
            _f.setBold(True)
            top.setFont(0, _f)
            jobs = [j for j in (req.get("jobs") or []) if j is not None]
            if not jobs:
                top.addChild(QTreeWidgetItem(["（请求为空）", "", "", ""]))
            for job in jobs:
                self._add_job_row(top, job)
            self._tree_right.addTopLevelItem(top)
            top.setFirstColumnSpanned(True)   # 必须在插入之后设，否则 Qt 不生效
        self._tree_right.expandAll()

    def _add_job_row(self, top, job):
        name = str(getattr(job, "display_name", "") or os.path.basename(
            str(getattr(job, "file_path", "") or "")) or "（未命名文件）")
        try:
            pages_n = int(getattr(job, "page_count", 0) or 0)
        except (TypeError, ValueError):
            pages_n = 0
        try:
            copies = max(1, int(getattr(job, "copies", 1) or 1))
        except (TypeError, ValueError):
            copies = 1
        duplex = (str(getattr(job, "duplex", "off") or "off") == "on")
        if pages_n <= 0:
            p1 = p2 = "页数未知"
            info = "? 张 · 页数未知（等分析）"
        else:
            p1, p2 = _file_pass_text(list(range(1, pages_n + 1)), duplex, "odd")
            info = (f"{duplex_stack.sheets_for(pages_n, duplex, 1) * copies} 张 · "
                    f"{pages_n} 页" + (f"×{copies} 份" if copies > 1 else "")
                    + ("" if duplex else " · 单面"))
        child = QTreeWidgetItem([f"📄 {name}", p1, p2, info])
        child.setToolTip(0, name)
        child.setToolTip(1, f"第 1 趟：{p1}")
        child.setToolTip(2, f"第 2 趟：{p2}")
        top.addChild(child)

    # ── 「我已取走 / 放回」：只对「第 2 趟无内容」的计划开放 ──

    def _on_left_selection(self):
        self._sel = None
        items = self._tree_left.selectedItems()
        if items:
            data = items[0].data(0, Qt.UserRole)
            if isinstance(data, dict):
                self._sel = data
        self._update_take_button()

    def _update_take_button(self):
        d = self._sel or {}
        self._btn_take.setEnabled(bool(d) and bool(d.get("blank")) and not self._busy)
        self._btn_take.setText("↩ 放回" if d.get("taken") else "✓ 我已取走")
        if not d:
            self._btn_take.setToolTip("先在左栏选中一个文件")
        elif not d.get("blank"):
            self._btn_take.setToolTip("这份第 2 趟还要用这张纸，不能取走")
        else:
            self._btn_take.setToolTip(
                "取走后这张纸不再计入第 2 趟的送纸数（清单立即落盘，可撤销）")

    def _on_take_clicked(self):
        d = self._sel
        if not d:
            return
        self.toggle_requested.emit(int(d["seq"]), int(d["index"]), not bool(d["taken"]))

    # ── 关闭：**不自己隐藏**，交给主窗口裁决 ──
    # 为什么不让窗口自己 hide：左栏还有没打第 2 趟的纸时，关掉窗口/关掉攒批模式会让
    # 一整叠半成品与若干订单**离开视线**（旧写法只 hide，连一句提示都没有；操作员很容易
    # 以为"关掉就等于这叠不要了"，或者反过来以为"关掉订单就完成了"）。
    # 主窗口先弹三选确认（保留 / 放弃整叠=按失败收尾 / 取消），再决定 hide()。

    def closeEvent(self, ev):
        """点窗口 × / Esc：只发信号，是否真的隐藏由主窗口决定（它会调 hide()）。"""
        self.close_requested.emit()
        ev.ignore()


# ══════════════════════════════════════════════════════════════════════════════
# 打印机双面能力（人工盖章）—— 2026-10-17 现场事故的闭环入口
# ══════════════════════════════════════════════════════════════════════════════
# 为什么需要用户能自己盖章：能力"未确认"时程序按**旧行为**把双面请求交给驱动 —— 而
# HP LaserJet 1018/1020/1022 这类**没有自动双面器**的机型，驱动会虚报支持自动双面
# （DC_DUPLEX=1 且接受 Duplex=2，实测本机 USB005 队列 confidence=medium 报 true），
# 于是双面请求走的是**驱动程序自带的手动双面向导**（「双面打印说明」：偶数页先打 →
# 提示整叠搬回 → 再打奇数页）。现场后果：单页文件被驱动多吐一张空白页（2026-10-17
# 17:48，订单 HN20261017-0002），而且全程没走程序自己那套两趟控制器（manual_duplex）。
#
# 此前唯一能建档案的入口是命令行（tools/print_duplex_calibration.py），而档案又被旧版
# 客户端的整体落盘清空过一次（根因见 printer_config.py 顶部）⇒ 用户"明明校准过"却
# 发现双面又回到官方工具。所以补这个图形入口：
#   · 人工结论 capability_source="manual"，**不被后续自动探测覆盖**（apply_probe 保证）；
#   · 与打印机档案同一份数据（printer_config 层读写，GUI 不另开口子）。
CAP_ASK = "未确认（先按驱动双面提交）"
CAP_MANUAL = "不支持自动双面 —— 由程序分两趟 + 人工翻纸"
CAP_AUTO = "支持自动双面（驱动一次成型）"
_CAP_ITEMS = [CAP_ASK, CAP_MANUAL, CAP_AUTO]

_FIRST_ITEMS = ["奇数页先打（成品叠最上面是第 1 页）", "偶数页先打（镜像观感）"]


class PrinterDuplexCapabilityDialog(QDialog):
    """帮助 → 打印机双面能力…：给**当前打印队列**盖一个三态能力章 + 手动双面参数。

    参数默认值是 HP LaserJet 1020 的实机校准结论（first=odd / 逆序 / 180° 补偿），
    换机型照抄会错 —— 对话框里写明了这一点，并要求填"校准人"留痕。
    """

    def __init__(self, printer_name: str, profile: PrinterProfile | None = None,
                 verified_by: str = "", parent=None):
        super().__init__(parent)
        self._printer_name = printer_name or "(系统默认打印机)"
        prof = profile or PrinterProfile()
        self.setWindowTitle("打印机双面能力")
        self.setMinimumWidth(620)

        lay = QVBoxLayout(self)

        head = QLabel(f"当前打印队列：<b>{self._printer_name}</b>")
        lay.addWidget(head)

        info = QLabel(
            "「未确认」时程序把双面请求<b>交给驱动</b>：若本机没有自动双面器"
            "（HP LaserJet 1018 / 1020 / 1022 属这类，<b>驱动会虚报支持</b>），"
            "实际走的是驱动自带的「双面打印说明」向导 —— 偶数页先打、提示翻纸、再打奇数页，"
            "且<b>可能先吐一张空白页</b>（单页文件尤其明显）。<br>"
            "选「不支持自动双面」后改用程序自己的两趟控制器：全程单面、只翻一次纸、"
            "第 2 趟自动做 180° 补偿，并支持「🖐 攒批模式」。<br>"
            "⚠ 三项手动参数是 <b>HP LaserJet 1020 的实机校准值</b>，换别的机型要重做校准"
            "（<code>tools/print_duplex_calibration.py \"队列名\" --save</code>），别照抄。")
        info.setWordWrap(True)
        lay.addWidget(info)

        cap_row = QHBoxLayout()
        cap_row.addWidget(QLabel("双面能力："))
        self._cap = QComboBox()
        self._cap.addItems(_CAP_ITEMS)
        self._cap.setCurrentIndex({None: 0, False: 1, True: 2}.get(
            prof.auto_duplex if isinstance(prof.auto_duplex, bool) else None, 0))
        self._cap.currentIndexChanged.connect(lambda _i: self._sync_enabled())
        cap_row.addWidget(self._cap, 1)
        lay.addLayout(cap_row)

        self._grp = QGroupBox("手动双面参数（仅「不支持自动双面」时生效）")
        gl = QVBoxLayout(self._grp)

        first_row = QHBoxLayout()
        first_row.addWidget(QLabel("第 1 趟："))
        self._first = QComboBox()
        self._first.addItems(_FIRST_ITEMS)
        self._first.setCurrentIndex(1 if str(prof.first).lower() == "even" else 0)
        first_row.addWidget(self._first, 1)
        gl.addLayout(first_row)

        second_row = QHBoxLayout()
        second_row.addWidget(QLabel("第 2 趟顺序："))
        self._reverse = QComboBox()
        self._reverse.addItems(["逆序（整叠原样搬回纸盘）", "正序（第 2 趟自己重排）"])
        self._reverse.setCurrentIndex(0 if prof.reverse_second else 1)
        second_row.addWidget(self._reverse, 1)
        gl.addLayout(second_row)

        self._rotate = ThemedCheckBox("第 2 趟内容旋转 180°（补偿纸路径翻转，实测 HP 1020 需要）")
        self._rotate.setChecked(bool(prof.second_rotate_180))
        gl.addWidget(self._rotate)

        by_row = QHBoxLayout()
        by_row.addWidget(QLabel("校准人："))
        self._by = QLineEdit(prof.verified_by or verified_by or "")
        self._by.setPlaceholderText("谁做的实机校准（留痕用）")
        by_row.addWidget(self._by, 1)
        gl.addLayout(by_row)

        lay.addWidget(self._grp)

        self._cur = QLabel("")
        self._cur.setStyleSheet("font-size: 11px;")
        self._cur.setWordWrap(True)
        lay.addWidget(self._cur)
        self._show_current(prof)

        btns = QHBoxLayout()
        btns.addStretch(1)
        ok = QPushButton("保存")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        lay.addLayout(btns)

        self._sync_enabled()

    def _sync_enabled(self):
        manual = self._cap.currentIndex() == 1
        self._grp.setEnabled(manual)
        self._by.setEnabled(self._cap.currentIndex() != 0)

    def _show_current(self, prof: PrinterProfile):
        self._cur.setText(f"当前档案：{prof.summary()}")

    def values(self) -> dict:
        """对话框 → update_profile 的字段（未确认 = 清掉人工结论，回到驱动双面旧行为）。"""
        idx = self._cap.currentIndex()
        if idx == 0:
            return {"auto_duplex": None, "capability_source": "", "manual_duplex": False,
                    "verified_at": "", "verified_by": "", "note": "人工改回「未确认」"}
        manual = (idx == 1)
        by = self._by.text().strip()
        return {
            "auto_duplex": not manual,
            "capability_source": "manual",
            "manual_duplex": manual,
            "first": "even" if self._first.currentIndex() == 1 else "odd",
            "reverse_second": self._reverse.currentIndex() == 0,
            "second_rotate_180": bool(self._rotate.isChecked()),
            "verified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "verified_by": by,
            "note": ("人工盖章：不支持自动双面，双面由程序分两趟 + 人工翻纸"
                     if manual else "人工盖章：支持自动双面（驱动一次成型）"),
        }


class PrintSpeedDialog(QDialog):
    """帮助 → 打印速度：**一个窗口装下所有与"第 1 趟耗时/翻纸放行"有关的设置**。

    2026-10-20 用户要求：
      ① 放行口径从「预估 − 5 秒」改成「预估 × 比例」（纸越多误差越大，固定秒数不合适）；
      ② 「启动/预热时间」机器量不出来、「人工翻页需要的时间」更是纯人工经验 ⇒ 都要能手动调；
      ③ 这些设置 + 原来的两点测速**放进同一个窗口**（入口改名「打印速度…」）。

    窗口内容：
      · 第 1 趟耗时模型：启动/预热（秒）、每张纸（秒）—— 0 = 该分量用自动学习值；
      · 翻纸放行：放行比例（%）、人工翻页准备时间（秒）
        放行时刻 = 第 1 趟预估 × 比例 + 翻页准备时间
      · 底部：打印测速（1 张 + 10 张空白页，写进速度样本）/ 恢复自动学习 / 保存 / 取消
    """

    calibrate_requested = Signal()

    def __init__(self, parent=None, profile=None, queue_name: str = ""):
        super().__init__(parent)
        self.setWindowTitle("打印速度")
        self.setMinimumWidth(520)
        prof = profile or PrinterProfile()
        lay = QVBoxLayout(self)

        head = QLabel(
            f"打印队列：<b>{queue_name or '(系统默认打印机)'}</b><br>"
            f"当前模型：{prof.speed_summary()}")
        head.setWordWrap(True)
        lay.addWidget(head)

        # ── 第 1 趟耗时模型 ──
        g1 = QGroupBox("第 1 趟耗时模型（0 = 该分量仍用自动学习值）")
        f1 = QFormLayout(g1)
        self._a = QDoubleSpinBox()
        self._a.setRange(0.0, 600.0)
        self._a.setDecimals(1)
        self._a.setSingleStep(0.5)
        self._a.setSuffix(" 秒")
        self._a.setValue(float(prof.speed_manual_a or 0.0))
        self._a.setToolTip("打印机从收到任务到第一张纸出来（含唤醒/预热）。"
                           "驱动量不准时（只回报「开始打印」）就手填，比如这台机器约 13 秒。")
        f1.addRow("启动 / 预热：", self._a)
        self._b = QDoubleSpinBox()
        self._b.setRange(0.0, 600.0)
        self._b.setDecimals(2)
        self._b.setSingleStep(0.1)
        self._b.setSuffix(" 秒 / 张")
        self._b.setValue(float(prof.speed_manual_b or 0.0))
        self._b.setToolTip("第一张之后的每张纸走纸 + 成像耗时。"
                           "留 0 用实测拟合值；只有一种页数的样本时会退回默认斜率。")
        f1.addRow("每张纸：", self._b)
        lay.addWidget(g1)

        # ── 翻纸放行 ──
        g2 = QGroupBox("翻纸放行（什么时候允许点「▶ 继续打第 2 趟」）")
        f2 = QFormLayout(g2)
        self._ratio = QSpinBox()
        self._ratio.setRange(0, 100)
        self._ratio.setSingleStep(5)
        self._ratio.setSuffix(" %")
        self._ratio.setValue(int(round(float(prof.gate_ratio if prof.gate_ratio is not None
                                             else 0.5) * 100)))
        self._ratio.setToolTip("第 1 趟预估完成到这个比例就放开按钮（默认 50%）。"
                               "纸越多、预估误差越大，固定提前 5 秒不再合适。")
        f2.addRow("放行比例：", self._ratio)
        self._ready = QDoubleSpinBox()
        self._ready.setRange(0.0, 3600.0)
        self._ready.setDecimals(0)
        self._ready.setSingleStep(5.0)
        self._ready.setSuffix(" 秒")
        self._ready.setValue(float(prof.flip_ready_sec or 0.0))
        self._ready.setToolTip("第 1 趟打完后，留给操作员走过去把纸叠搬回进纸盒的时间。"
                               "这段是人工经验，机器量不出来，所以允许手填。")
        f2.addRow("人工翻页准备时间：", self._ready)
        self._formula = QLabel("")
        self._formula.setWordWrap(True)
        self._formula.setStyleSheet("font-size: 11px;")
        f2.addRow("", self._formula)
        self._ratio.valueChanged.connect(self._sync_formula)
        self._ready.valueChanged.connect(self._sync_formula)
        self._sync_formula()
        lay.addWidget(g2)

        note = QLabel("提示：这台打印机的「完成」回报早于实机出纸（只回报「开始打印」），"
                      "所以按钮按上面这套预估放行；放行前仍会显示还要等几秒。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #7f8c8d; font-size: 11px;")
        lay.addWidget(note)

        row = QHBoxLayout()
        self._btn_cal = QPushButton("▶ 打印测速（1 张 + 10 张空白页）")
        self._btn_cal.setToolTip("打 11 张纸（单面、不翻纸）自动计时，把样本写进本机档案")
        self._btn_cal.clicked.connect(lambda *_: self.calibrate_requested.emit())
        row.addWidget(self._btn_cal)
        self._btn_reset = QPushButton("恢复自动学习")
        self._btn_reset.setToolTip("清空手动填的启动/每张纸，回到自动学习的拟合值")
        self._btn_reset.clicked.connect(self._on_reset)
        row.addWidget(self._btn_reset)
        row.addStretch(1)
        ok = QPushButton("保存")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        row.addWidget(ok)
        row.addWidget(cancel)
        lay.addLayout(row)

    def _sync_formula(self):
        self._formula.setText(
            f"放行时刻 = 第 1 趟预估 × {self._ratio.value()}%"
            + (f" + {self._ready.value():.0f} 秒（人工翻页准备）"
               if self._ready.value() > 0 else "")
            + "；算出来不足 1 秒时直接放行。")

    def _on_reset(self):
        self._a.setValue(0.0)
        self._b.setValue(0.0)

    def values(self) -> dict:
        return {
            "speed_manual_a": round(float(self._a.value()), 1),
            "speed_manual_b": round(float(self._b.value()), 2),
            "gate_ratio": round(float(self._ratio.value()) / 100.0, 2),
            "flip_ready_sec": round(float(self._ready.value()), 0),
        }


class SpeedCalibrationWorker(QThread):
    """打印速度两点标定：打 1 张 + 10 张**空白页**（单面，不需要翻纸）→ 交给档案拟合。

    为什么走独立线程：两趟加起来常有一两分钟，主线程不能卡（否则界面假死、翻纸窗也不刷新）。
    空白页只费纸不费碳粉，所以标定成本就是 11 张纸。"""

    log = Signal(str)
    done = Signal(bool, str, list)      # (ok, msg, samples=[(张数, 秒数), ...])

    def __init__(self, printer_name: str, small: int = 1, large: int = 10, parent=None):
        super().__init__(parent)
        self._printer = printer_name
        self._small = small
        self._large = large

    def run(self):
        try:
            from pdf_printer import measure_print_speed_two_point
            ok, msg, samples = measure_print_speed_two_point(
                self._printer, small=self._small, large=self._large,
                on_log=lambda m: self.log.emit(m))
            self.done.emit(bool(ok), msg, samples or [])
        except Exception as e:
            self.done.emit(False, f"测速异常：{type(e).__name__}: {e}", [])


# ============================================================
# 打印工作线程
# ============================================================

class _DeferManualDuplex(Exception):
    """内部控制流：本任务要并入手动双面批次，**不在这里打印/收尾**。

    为什么用异常：任务准备（转换/缓存）的代码在 try 块里，而 `continue` 会连带跑 finally
    的临时文件清理（批次合并还需要那份 PDF）。用这个哨兵跳出 try 最省事、也最不容易改坏
    原有的转换/缓存/出错分支。"""


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
    # 手动双面：第 1 趟打完 → 请求主线程弹「请翻纸」提示（(说明文字, 超时秒)）。
    # 主线程用 worker.confirm_flip(True/False) 回执；worker 在此期间阻塞等待。
    flip_required = Signal(str, int)
    # 手动双面常驻窗：批次开始就发一次（主线程立刻显示窗口，而不是打完才弹一堆东西出来），
    # 批次结束（成功/失败/跳过）再发一次让主线程收尾关闭。
    duplex_announce = Signal(str)
    duplex_finished = Signal(str)
    # 第 1 趟的「张数 + 预计秒数」（2026-10）：常驻窗据此显示计时器并按超时变色。
    duplex_plan = Signal(int, float)
    # 第 1 趟**实测**（张数, 秒数）→ 主线程写进该打印队列的档案，自动学习出纸速度
    # （T(n) ≈ a + b·n 的 a/b），下次预估更准。零额外用纸。
    duplex_speed_sample = Signal(int, float)
    # ── 攒批模式（多任务合并翻纸，2026-10）──
    # 本单第 1 趟**已排好页序、即将开打**时发射（payload=dict）：
    # 主线程据此把这一单落进纸叠清单（**打印前**就落盘，崩溃后才知道纸槽里有什么）。
    stack_entry_planned = Signal(object)
    # 本单第 1 趟结束：(seq, ok, message, 第 1 趟槽位数)
    stack_entry_done = Signal(int, bool, str, int)

    def __init__(
        self,
        jobs: list[PrintJob],
        printer_name: str,
        duplex_mode: str,
        keep_temp_pdf: bool,
        render_dpi: int = 0,
        cover_page: bool = False,
        cover_page_config: dict | None = None,
        manual_duplex_profile: PrinterProfile | None = None,
        stack_mode: bool = False,
        stack_out_pdf: str = "",
        stack_seq: int = 0,
        stack_rotate: bool = True,
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
        # 手动双面（无自动双面器的机型）：档案来自 print_config.json 的 printer_profiles，
        # 人工确认过 auto_duplex=False 且 manual_duplex=True 才启用；否则一切照旧（驱动双面）。
        self._manual_profile = manual_duplex_profile
        # 攒批模式：本单**只打第 1 趟**，合并后的 PDF 写到 stack_out_pdf（批次目录里），
        # 第 2 趟由主线程在整叠上一次性打（gui.DuplexFlushWorker）。见 duplex_stack.py。
        self._stack_mode = bool(stack_mode)
        self._stack_out_pdf = stack_out_pdf or ""
        self._stack_seq = int(stack_seq or 0)
        self._stack_rotate = bool(stack_rotate)
        # 翻纸回执：worker 发信号 → 主线程弹窗 → confirm_flip() 置事件
        self._flip_event = threading.Event()
        self._flip_continue = False
        # 出纸槽张数台账（本轮打印累计）：手动双面**批次之前**的直打作业（单面文件、
        # 首页）会先落在托盘里、压在本批下面。翻纸文案必须把它算进去，否则操作员照
        # "应有 N 张"永远数不够（判据失效），或者照"取出纸叠"把**整叠**搬进纸盘 2 ——
        # 多出来的纸就留在纸盘里、被后续作业当白纸用掉（文件静默丢失，零日志零报错）。
        # 现场：2026-10-15 订单 HN20261015-0008（照片先直打 + 申请书手动双面）。
        self._tray_sheets = 0

    # 翻纸提示的超时（秒）：主线程的弹窗自带倒计时，这里只多留一点余量兜底，
    # 保证 GUI 万一没响应时 worker 也不会永久挂住。
    FLIP_TIMEOUT_SEC = 600
    FLIP_GRACE_SEC = 30

    def cancel(self):
        self._cancelled = True
        self.log_message.emit("[取消] 用户取消了打印任务")
        # 正在等翻纸回执时也要立刻放行，否则取消后还卡在等事件上
        self._flip_event.set()

    def confirm_flip(self, cont: bool) -> None:
        """主线程回执：是否继续打第 2 趟（线程安全；可被主线程任意时刻调用）。"""
        self._flip_continue = bool(cont)
        self._flip_event.set()

    def _effective_pages(self, job: PrintJob, print_path: str = "") -> int:
        """本任务**实际会打印**的有效页数（单页硬闸门的判据）；0 = 确实未知。

        为什么优先数真实 PDF：job.page_count 可能过时/未知（旧配置、云端订单还没做页数
        分析时是 0），而"要不要请求双面"必须按**真的要打几页**决定。
        只在 `page_count <= 1` 时才去解析 PDF —— 页数 ≥2 时按已知页数套页码范围就够了
        （范围解析不依赖 PDF 内容），免得每次打印都白解析一遍 PDF。
        """
        n = int(getattr(job, "page_count", 0) or 0)
        if n <= 1:
            for _p in (print_path, getattr(job, "cached_pdf", ""),
                       getattr(job, "file_path", "")):
                try:
                    if _p and os.path.isfile(_p) and str(_p).lower().endswith(".pdf"):
                        _c = int(count_pdf_pages(_p) or 0)
                        if _c > 0:
                            n = _c
                            break
                except Exception:
                    continue
        if n <= 0:
            return 0
        try:
            return int(_count_pages_in_range(getattr(job, "page_range", "") or "", n))
        except Exception:
            return n

    def _effective_duplex(self, job: PrintJob, n_pages: int = 0) -> str:
        """本次实际该请求的双面模式：有效页数 == 1 ⇒ "off"（单页硬闸门，2026-10-17 事故）。

        现场：单页 docx 的 job.duplex="on" → 驱动弹 HP「双面打印说明」向导，打出一张
        空白页 + 正式页。1 页纸物理上不可能双面（配对需要 2 页），请求双面只会让驱动
        多走一张空白纸。放在**打印线程**里做硬闸门，才盖得住云端订单 / 旧配置 / 手工
        改坏的 job.duplex（主线程 `_on_convert_finished` 的归一化只在**转换完成那一刻**
        写一次：云端任务与本地文件都走那里，但旧配置里早就存在的 job、或没经过转换的
        PDF 直打，都不经过它）。
        页数未知(0) 不当作 1 页：保持请求原样（宁可交给驱动，也不能把多页文件误降单面）。
        """
        eff = int(n_pages or 0) or self._effective_pages(job)
        if eff == 1:
            return "off"
        return "on" if (getattr(job, "duplex", "off") or "off") == "on" else "off"

    def _driver_pairs_pages(self) -> bool:
        """本次双面请求会不会由**打印机硬件**把两页配到一张纸的两面。

        只影响一件事：合并作业里要不要在文件/副本之间补空白页（见 `_print_merged_group`）。
        · 档案明确 auto_duplex=False（无自动双面器）→ 驱动不会真双面（双面作业已走程序
          自己的两趟，或走驱动自带的手动向导弹窗），配对由那边负责；这里**不补**，
          补了只会白出空白纸；
        · 能力未确认 → 按"会双面"处理（与 `_sheets_of_job` 的出纸槽口径一致：宁可按会
          双面估算，也不要漏算配对）；
        · 虚拟打印机（Print to PDF / XPS / OneNote…）→ 不出纸、双面是空操作，页面根本
          不会两两配对，不补（否则导出的 PDF 里会多出空白页）。
        """
        p = self._manual_profile
        if p is not None and p.auto_duplex is False:
            return False
        try:
            from pdf_printer import is_virtual_printer
            if is_virtual_printer(self._printer_name):
                return False
        except Exception:
            pass
        return True

    def _uses_manual_duplex(self, job: PrintJob) -> bool:
        """该任务是否要走"程序自己分两趟 + 人工翻纸"。

        单页硬闸门（2026-10-17 现场事故）：有效页数 == 1 ⇒ **不是**手动双面 —— 1 页要靠
        一张空白占位纸才能凑成"一张纸的两面"，等于白走一趟纸、白翻一次纸；直接单面打
        一张更快也不会错。页数未知(0) 不当成 1 页（保持旧行为）。
        判据只用 job.page_count（**不解析 PDF**）：run() 里在 print_path 就绪后由
        `_print_one_job` 按真实 PDF 兜底，这里不该为判页数去读盘。
        （2026-10-18 曾试过在这里"页数未知时现数 PDF"：`verify_incident_single_page_duplex`
        的 G1/G2 是**故意**把"页数读不出来 + 手动档案 ⇒ 仍尝试两趟"钉死的——数不出来时
        宁可交给 `_print_one_job` 兜底，也不在这里替用户改判，已改回。）
        """
        p = self._manual_profile
        if int(_count_pages_in_range(getattr(job, "page_range", "") or "",
                                     int(getattr(job, "page_count", 0) or 0))) == 1:
            return False
        return bool(job.duplex == "on" and p is not None
                    and p.auto_duplex is False and p.manual_duplex)

    def _estimate_first_pass(self, sheets: int) -> float:
        """第 1 趟预估秒数（档案里实测样本 → a + b·n；没样本就是保守默认值）。

        sheets 用**槽位数**（含空白占位页）：空白页也要走纸，不能不算。"""
        p = self._manual_profile
        if p is None:
            return 0.0
        try:
            return float(p.estimate_first_pass(sheets))
        except Exception:
            return 0.0

    def _ask_operator_to_flip(self, plan, tray_foreign: int = 0) -> bool:
        """请操作员翻纸：发信号 → 等主线程回执。超时/取消/跳过都返回 False。

        tray_foreign：本轮打印在**本批之前**已落进出纸槽、还压在本批下面的张数
        （直打的单面文件 / 首页 / 上一批）。>0 时文案必须把"槽里总数"与"只翻最上面
        N 张"说清楚 —— 否则操作员照"从出纸槽中取出纸叠"整叠搬进纸盘 2，多出来的纸
        就永远出不来了（被后续作业当白纸用掉，且全程无任何日志）。
        """
        if self._cancelled:
            return False
        first_pages = " ".join("空白" if p is None else str(p) for p in plan.pass_pages(1))
        second_pages = " ".join("空白" if p is None else str(p) for p in plan.pass_pages(2))
        n = int(plan.sheet_count)
        foreign = max(0, int(tray_foreign or 0))
        if foreign <= 0:
            # 出纸槽里只有本批的纸 —— 保持原文案（最常见的情形，一个字都不改）
            head = (f"第 1 趟已完成：出纸槽里应有 {n} 张纸"
                    f"（本趟打的是 {first_pages}）—— 数够再点继续。\n\n")
            step1 = "  1. 不改变方向，从出纸槽中取出纸叠。不要丢弃空白页面。\n"
        else:
            head = (f"第 1 趟已完成：出纸槽里共应有 {n + foreign} 张纸 —— 最上面 {n} 张是"
                    f"本批的（本趟打的是 {first_pages}），下面 {foreign} 张是本次一起打出的"
                    f"其它文件，不属于本批、不要一起搬走。\n\n")
            step1 = (f"  1. 不改变方向，从出纸槽中只取出最上面 {n} 张"
                     f"（下面那 {foreign} 张留在出纸槽里）。不要丢弃空白页面。\n")
        text = (
            head
            + "请按下面三步操作（原文来自 HP 驱动「双面打印说明」弹窗）：\n"
            + step1
            + "  2. 保持相同方向，将纸叠插入纸盘 2，使已打印面朝下。\n"
            + "     （不要翻面、不要重排）\n"
            + f"  3. 点「继续打第 2 趟」——随后打印 {second_pages}。\n\n"
            + "⚠ 翻纸期间不要让别的打印任务插进来（其它程序的手动打印、本工具别的任务都算）：\n"
            + "   第 2 趟要接着这叠纸打，中间插单就会串页。"
        )
        self._flip_continue = False
        self.log_message.emit(f"⏸ 等待人工翻纸（最多 {self.FLIP_TIMEOUT_SEC // 60} 分钟）…")
        self.flip_required.emit(text, self.FLIP_TIMEOUT_SEC)
        # 事件在批次开始时已清空（_print_manual_batch）；这里不能再 clear ——
        # 否则操作员在第 1 趟打印期间就点了「跳过」的预置答案会被抹掉，白等 10 分钟。
        if not self._flip_event.wait(self.FLIP_TIMEOUT_SEC + self.FLIP_GRACE_SEC):
            self.log_message.emit("⚠ 翻纸提示长时间无人确认，按「跳过本任务」处理")
            return False
        if self._cancelled:
            return False
        return bool(self._flip_continue)

    def _job_copy_pages(self, job: PrintJob, print_path: str) -> list[list[int]]:
        """本任务**每一份**要打的页码（1-based，已应用页码范围）→ [[p…], [p…]]。

        返回 [] 表示**这份 PDF 读不出来**（损坏 / 加密 / PyMuPDF 打不开）——调用方据此
        **退回单文件直打**（`_print_one_job` 会按真实 PDF 自己兜底），绝不能用
        `job.page_count` 兜底去"取页"：那会合出一份空文档，把 duplex 静默降成单面
        （`verify_incident_single_page_duplex` 的 G1 正是钉这个：未知 ≠ 1 页）。
        """
        try:
            n = int(count_pdf_pages(print_path) or 0)
        except Exception:
            n = 0
        if n <= 0:
            return []
        try:
            import pdf_printer as _pp
            sel = [i + 1 for i in _pp._parse_page_range(getattr(job, "page_range", "") or "", n)]
        except Exception:
            sel = []
        if not sel:
            sel = list(range(1, n + 1))
        return [list(sel) for _ in range(max(1, int(getattr(job, "copies", 1) or 1)))]

    @staticmethod
    def _merge_selected(items: list[tuple[str, list[int]]], out_path: str,
                        blank_after: Optional[list[bool]] = None) -> tuple[bool, str]:
        """按 [(pdf路径, 要取的 1-based 页码…)] 的顺序合并成一份 PDF（每份各占一段）。

        ⚠ 必须按**顺序**逐份 insert：`fitz.Select` 的页码顺序不被保留，靠它表达"两份"
        或"副本重复"都不可靠。这正是"一个作业打完整个订单"的落点：合成一份文档后
        只调一次 `print_pdf` ⇒ 只有一个打印作业，打印机连续成像、中间没有作业边界。

        blank_after：与 items 等长的布尔表，True = 这一段的**末尾补一张空白页**（尺寸取该段
        最后一页）。自动双面时页数为奇数的文件/副本必须补：驱动按"前一页/后一页 = 一张纸
        的两面"连续配对，不补的话下一段第 1 页会印在**这张纸的背面**（顾客拿到的文件背面
        印着下一个文件 = 串页，2026-10-21 现场）。空白页直接写进合并件，GDI / Sumatra /
        循环三条打印路径都会把它当成一页打出去。
        """
        try:
            import fitz
        except Exception as e:
            return False, f"合并 PDF 需要 PyMuPDF: {e}"
        pads = list(blank_after or [])
        pad_n = 0
        try:
            doc = fitz.open()
            try:
                for seg_i, (path, pages) in enumerate(items or []):
                    src = fitz.open(path)
                    try:
                        total = len(src)
                        # 空白页要盖住**这一张纸的背面** ⇒ 尺寸跟该段最后一页一致
                        last_rect = src[0].rect if total else fitz.Rect(0, 0, 595, 842)
                        for p in pages or []:
                            if 1 <= int(p) <= total:
                                last_rect = src[int(p) - 1].rect
                                doc.insert_pdf(src, from_page=int(p) - 1, to_page=int(p) - 1)
                    finally:
                        src.close()
                    if seg_i < len(pads) and pads[seg_i]:
                        doc.new_page(width=last_rect.width, height=last_rect.height)
                        pad_n += 1
                doc.save(out_path)
            finally:
                doc.close()
        except Exception as e:
            try:
                if os.path.isfile(out_path):
                    os.remove(out_path)
            except OSError:
                pass
            return False, f"合并 PDF 失败: {type(e).__name__}: {e}"
        extra = f"（含 {pad_n} 张文件间空白页）" if pad_n else ""
        return True, (f"已合并 {len(items or [])} 份 PDF{extra} → "
                      f"{os.path.basename(out_path)}")

    def _print_merged_group(self, group: list[dict],
                            base_offset: int, total_sides: int) -> tuple[dict, int]:
        """把**连续同参数**的若干文件合并成**一个打印作业**打出去。

        返回 `({idx: (ok, msg)}, 额外面数)` —— 第一个是**逐文件**的结果（不是整组
        一个结果）。调用方按它逐条 `job_finished`。为什么不返回整组结果：组里可能有
        "读不出页数"的文件被排除在合并之外（见下），它们必须被单独打印、单独报成败；
        整组一个结果会让它们**被静默丢掉却报成功**（云端把那张单标 sent、顾客永远拿不到
        纸 —— 独立审查用 `[坏件, 2页, 3页]` 复现过）。
        第二个数是**比 `estimate_print_sides` 多出来的面数** = 文件与文件之间补的空白页
        （`estimate_print_sides` 只算了同一文件的多份之间的分隔页）。调用方要把它
        算进进度分母 —— 空白页也是真实送进打印机的一面。

        group 里每项：{idx, job, path, temp_pdf, copies, dpi, ori, duplex, sides, key}。
        日志与 progress 都按"这份文件第几面"报，界面上看不出与"每文件一个作业"的区别 ——
        区别只在打印机侧：**只有一个 StartDoc**，中间不再停下来重建设备上下文。

        **读不出页数的文件不参与合并、退回各自单打**（`_job_copy_pages` 返回 []）：
        用 `job.page_count` 兜底去"取页"会合出空文档、把 duplex 静默降成单面。

        **自动双面时必须补文件间空白页**（2026-10-21 现场「串页」）：驱动把一份文档的
        连续页两两配到一张纸的两面，页数为奇数的文件最后一张纸背面会接着印**下一个文件**
        的第 1 页 ⇒ 顾客拿到的文件背面是别人的内容、也没法按文件分开。所以每个"页数为奇数
        且后面还有内容"的文件/副本段尾补一张空白页（见 `_merge_selected`）。只在双面真由
        硬件配对时补（`_driver_pairs_pages`）；单面作业、无自动双面器的机型、虚拟打印机都不补。

        失败粒度：合并后的作业失败 = 参与合并的那几个文件一起失败（各自 report 各自的
        task_id）；被排除在外、单独打印的那几个各自报各自的成败。
        """
        merged: Optional[str] = None
        try:
            mergeable: list[dict] = []
            solo: list[dict] = []
            names: list[str] = []
            for rec in group:
                job, path = rec["job"], rec["path"]
                names.append(job.display_name or os.path.basename(job.file_path) or
                             f"#{rec['idx'] + 1}")
                if not path or not os.path.isfile(path):
                    return ({r["idx"]: (False, f"文件不存在: {getattr(r['job'], 'file_path', '')}")
                             for r in group}, 0)
                per = self._job_copy_pages(job, path)
                if not per:
                    # 读不出页数（损坏/加密）→ 不合并这一份，交回 `_print_one_job` 按真实
                    # PDF 兜底（它才是"1 页硬闸门"与大页数口径的裁决点）
                    solo.append(rec)
                    continue
                mergeable.append({**rec, "copies_pages": per})

            if len(mergeable) < 2:
                # 只有一份可合并（或都不可合并）→ 合并没有收益，直接逐份单打（保持旧行为）
                return self._print_solo_group(group, base_offset, total_sides), 0

            # ── 合并段：每个文件的**每一份**各占一段（份数已在 `_job_copy_pages` 里重复）──
            segs: list[tuple[dict, list[int]]] = [
                (rec, pages) for rec in mergeable for pages in rec["copies_pages"]]
            # 空白页只补在"这一段后面还有内容"的位置：最后一段的背面本来就是空白，不用补。
            # 判据用**这一段实际要打几页**（页码范围/份数都已体现），不是 job.page_count。
            eff_duplex = str(mergeable[0].get("duplex") or "off")
            pad = bool(eff_duplex == "on" and self._driver_pairs_pages())
            blank_after = [
                bool(pad and len(pages) % 2 == 1 and i < len(segs) - 1)
                for i, (_rec, pages) in enumerate(segs)
            ]
            pads = sum(1 for b in blank_after if b)
            # 进度口径：`estimate_print_sides` **已经**把"同一个文件的多份之间"那张空白页
            # 算进分母了（奇数页 + 双面 + 多份），但**文件与文件之间**那一张没算。
            # 所以额外面数只数"文件分界"这一种：本组里页数为奇数、且后面还有文件的作业，
            # 每个多 1 面。直接拿 pads 加会重复计（多份 + 多文件时进度条反而打不满）。
            extra_sides = 0
            if pad:
                extra_sides = sum(
                    1 for rec in mergeable[:-1]
                    if rec["copies_pages"] and len(rec["copies_pages"][0]) % 2 == 1)
            paths: list[tuple[str, list[int]]] = [
                (rec["path"], pages) for rec, pages in segs]
            task_ref: list = []
            for i, (rec, pages) in enumerate(segs):
                task_ref.extend([(rec["idx"], rec["job"])] * len(pages))
                if blank_after[i]:
                    task_ref.append(None)     # 文件间空白页：进度文案要认得它（不是文件）
            merged = os.path.join(
                tempfile.gettempdir(),
                f"hn_merged_{os.getpid()}_{int(time.time() * 1000)}.pdf")
            ok_m, msg_m = self._merge_selected(paths, merged, blank_after)
            if not ok_m:
                return {r["idx"]: (False, msg_m) for r in group}, 0
            merged_ids = {rec["idx"] for rec in mergeable}
            head = "、".join(rec["job"].display_name or os.path.basename(rec["job"].file_path)
                            for rec in mergeable[:4])
            if len(mergeable) > 4:
                head += f" 等 {len(mergeable)} 个文件"
            self.log_message.emit(
                f"  → 合并打印 {len(mergeable)} 个文件为一个打印作业（少中断）：{head}")
            if pads:
                self.log_message.emit(
                    f"  ⚙ 自动双面：在 {pads} 处文件/副本分界补了空白页"
                    f"（页数为奇数的段后面补一张，否则下一份从这张纸背面接着印 = 串页）")
            n_merged = int(count_pdf_pages(merged) or 0)
            if n_merged and n_merged != len(task_ref):
                self.log_message.emit(
                    f"  ⚠ 合并页数不符：实际 {n_merged} 页 / 各任务合计 {len(task_ref)} 面"
                    f"—— 已按实际页数打印，请核对页码范围与份数")
            ok, msg = print_pdf(
                pdf_path=merged,
                printer_name=self._printer_name,
                copies=1,                      # 各文件的份数已经合并进 merged 了
                # 双面用**逐文件算好的生效值**（1 页文件 ⇒ off，不会被合并件页数带偏），
                # 组内所有文件这个值必然相同（它就是分组键的一项）
                duplex=eff_duplex,
                duplex_mode=str(mergeable[0]["key"][1] or self._duplex_mode),
                page_range="",                 # 已按各文件页码范围抽好页
                # 方向只影响日志/DEVMODE 文案（逐页按自己尺寸渲染），取**真正参与合并**的第一项
                orientation=str(mergeable[0].get("ori") or ""),
                progress_callback=self._group_progress_callback(
                    task_ref, base_offset, total_sides + extra_sides),
                dpi=int(group[0]["dpi"] or 0),
            )
            if ok:
                self.log_message.emit(f"  ✓ {msg}")
            else:
                self.log_message.emit(f"  ✗ 打印失败: {msg}")
            out = {i: (ok, msg) for i in merged_ids}
            if solo:
                # 读不出页数的那些**单独打、单独报**（绝不能因为"没并进合并件"就当它成功了）。
                # ⚠ 必须**单独包一层 try**：合并件此刻已经把纸打出来了，若这里抛异常又被外层
                #   `except` 接住，会把**已经成功的 merged_ids 一起改写成失败** ⇒ 云端判失败
                #   重推 ⇒ 重复打印（独立审查用"桩抛异常"复现过）。异常只波及 solo 那几项。
                self.log_message.emit(
                    f"  ⚠ 其中 {len(solo)} 个文件读不出页数，不参与合并、按原样单独打印")
                try:
                    out.update(self._print_solo_group(solo, base_offset, total_sides))
                except Exception as e:
                    for rec in solo:
                        out[rec["idx"]] = (
                            False, f"单独打印异常：{type(e).__name__}: {e}")
            # 空白页只在这一组合并作业真的送出去之后才算"打过的面"；
            # 返回的是**额外面数**（文件分界补的那几张），多份之间的分隔页已在各任务
            # 自己的 `estimate_print_sides` 里，不能再算一遍。
            return out, (extra_sides if ok else 0)
        except Exception as e:
            return ({r["idx"]: (False, f"合并打印异常：{type(e).__name__}: {e}")
                     for r in group}, 0)
        finally:
            if merged:
                try:
                    if os.path.isfile(merged):
                        os.remove(merged)
                except OSError:
                    pass
            # 合并用的临时件由各文件自己的转换件兜着 —— 这里只清**本组**的转换件
            # （缓存件 _cleanup_temp_pdf 自己会跳过）
            for rec in group:
                self._cleanup_temp_pdf(rec["job"], rec.get("temp_pdf"))

    def _print_solo_group(self, group: list[dict],
                          base_offset: int, total_sides: int) -> dict:
        """逐份单打（合并无收益、或某份读不出页数时的**退路**）→ {idx: (ok, msg)}。

        **逐文件**返回：一个文件失败不该让同组其它文件连坐（它们各自有自己的 task_id 与
        云端回报），所以这里不返回"整组成败"。
        """
        out: dict = {}
        off = base_offset
        for rec in group:
            job, path = rec["job"], rec["path"]
            ok, msg = self._print_one_job(
                path, job, int(rec["copies"]), str(rec.get("ori") or ""),
                int(rec["dpi"] or 0),
                self._solo_progress_callback(rec, off, total_sides),
                task_ref=None)
            out[rec["idx"]] = (ok, msg)
            if ok:
                off += int(rec.get("sides") or 0)
        return out

    def _solo_progress_callback(self, rec: dict, base_offset: int, total_all: int):
        """单份打印的进度回调（页面在此份内部编号）。"""
        name = rec["job"].display_name or os.path.basename(rec["job"].file_path or "")

        def _on_side(page_seq: int, _task_total: int = 0):
            if self._cancelled:
                return
            self.progress.emit(base_offset + int(page_seq or 0), total_all,
                               f"正在打印: {name} ({page_seq}面)")
        return _on_side

    def _group_progress_callback(self, task_ref: list,
                                 base_offset: int, total_all: int):
        """合并作业的进度回调：把"第 k 面"映射回**是哪个文件的第几面**。

        task_ref 里的 `None` 是自动双面补的**文件间空白页**（没有归属文件）—— 文案要
        说清楚它是什么，不能拿前一份文件名凑数，否则日志里看起来像"这个文件多打了一面"。
        """
        def _on_side(page_seq: int, _task_total: int = 0):
            if self._cancelled:
                return
            idx = ""
            if 0 < int(page_seq or 0) <= len(task_ref):
                ref = task_ref[int(page_seq) - 1]
                if ref is None:
                    idx = " （文件间空白页）"
                else:
                    _i, _j = ref
                    idx = f" ({_j.display_name or os.path.basename(_j.file_path or '')})"
            self.progress.emit(base_offset + int(page_seq or 0), total_all,
                               f"正在打印: {page_seq}/{len(task_ref)}面{idx}")
        return _on_side

    def _print_one_job(self, print_path: str, job: PrintJob, copies: int, orientation: str,
                       dpi: int, job_progress, duplex: str | None = None,
                       duplex_mode: str | None = None, page_range: str | None = None,
                       task_ref: list | None = None) -> tuple[bool, str]:
        """打一个任务（**一次提交**）：支持自动双面的机器走驱动双面，其余走单面。

        手动双面的任务不会走到这里：它们在 run() 里被"并入手动双面批次"，由
        `_print_manual_batch` 一次性打完（**整批只翻一次纸**）。

        `duplex/duplex_mode/page_range` 显式给出时**覆盖** job 上的值（合并作业用，见下）。
        `task_ref`：合并作业时给出"第 k 面属于哪个任务"（`_print_merged_group` 传）。
        仅用于**校验**合并后的页数确实等于各任务页数之和 —— 对不上说明页码/份数口径出错，
        宁可只记日志（页面已按顺序排好，打印本身仍然正确），也不静默忽略。
        """
        # 单页硬闸门（2026-10-17 现场事故）：按**真实 PDF** 数出的有效页数 == 1 ⇒ 传 "off"。
        # 这一道盖的是主线程归一化管不到的情形（云端订单 / 旧配置 / 手工改坏）；
        # 1 页请求双面会让驱动弹手动双面向导、多送一张空白纸走一趟。
        _eff_pages = self._effective_pages(job, print_path)
        _duplex = self._effective_duplex(job, _eff_pages)
        if _duplex != (getattr(job, "duplex", "off") or "off"):
            self.log_message.emit(
                f"  ⚠ 有效页数 {_eff_pages} 页 → 本次按单面打印"
                f"（1 页双面物理上不可能，请求双面会让驱动多走一张空白纸）")
        if task_ref:
            try:
                _real = int(count_pdf_pages(print_path) or 0)
                if _real != len(task_ref):
                    self.log_message.emit(
                        f"  ⚠ 合并页数不符：实际 {_real} 页 / 各任务合计 {len(task_ref)} 面"
                        f"—— 已按实际页数打印，请核对页码范围与份数")
            except Exception:
                pass
        # ⚠ 合并路径**必须**显式给出 duplex/duplex_mode/page_range：
        #   · 合并件已经按各文件的页码范围抽好页（再传 page_range 会二次截断，还可能让
        #     "1 页硬闸门"按第一份的页码范围误判）；
        #   · 各文件的份数已经体现在合并件里 ⇒ copies 恒为 1。
        if duplex is None:
            duplex = _duplex
        if duplex_mode is None:
            duplex_mode = getattr(job, "duplex_mode", "") or self._duplex_mode
        if page_range is None:
            page_range = getattr(job, "page_range", "") or ""
        return print_pdf(
            pdf_path=print_path,
            printer_name=self._printer_name,
            copies=copies,
            duplex=duplex,
            duplex_mode=duplex_mode,
            page_range=page_range,
            orientation=orientation,
            progress_callback=job_progress,
            dpi=dpi,
        )

    def _rotate_for_job(self, job: PrintJob) -> bool:
        """本任务第 2 趟要不要旋转 180°：纸路径实测值 × 装订方向（长边补偿/短边不补偿）。"""
        p = self._manual_profile
        return manual_duplex.rotate_second_for(
            p.second_rotate_180 if p else True,
            job.duplex_mode or self._duplex_mode,
        )

    def _sheets_of_job(self, job: PrintJob, copies: int, print_path: str = "") -> int:
        """本任务会往出纸槽里放几张纸（台账用：翻纸文案要说清"槽里共几张"）。

        单面 n 页 = n 张；双面 n 页 = ceil(n/2) 张 —— 但**只有驱动真的替我们双面时**
        才算半张：本机档案明确 auto_duplex=False 时驱动不会双面，每页还是各占一张纸，
        照 ceil 算会**少算**。能力未知（没有档案）时按"会双面"算 —— 宁可少报
        （操作员数到的只多不少），也不要多报（多报会让"数够再翻"永远数不够）。

        页数未知（云端任务还没分析出来）时**现数一遍实际打印的那个 PDF**，与 run()
        预估进度用的口径一致 —— 否则会按 1 页估，台账少算、文案又把人带偏。
        """
        n_pages = max(1, int(job.page_count or 0))
        if job.page_count <= 0:
            for _p in (print_path, job.file_path, job.cached_pdf):
                try:
                    if _p and os.path.isfile(_p) and str(_p).lower().endswith(".pdf"):
                        n_pages = max(1, count_pdf_pages(_p))
                        break
                except Exception:
                    pass
        try:
            n_sel = _count_pages_in_range(job.page_range or "", n_pages) or n_pages
        except Exception:
            n_sel = n_pages
        p = self._manual_profile
        driver_duplex = bool(p is None or p.auto_duplex)
        return duplex_stack.sheets_for(
            n_sel, (job.duplex or "off") == "on" and driver_duplex,
            max(1, int(copies or 1)))

    def _cleanup_temp_pdf(self, job: PrintJob, temp_pdf: Optional[str]) -> None:
        """清理转换出的临时 PDF（keep_temp_pdf 时先复制一份到桌面）。缓存的 PDF 不删。"""
        if not temp_pdf or not os.path.isfile(temp_pdf):
            return
        if self._keep_temp_pdf:
            original_base = os.path.splitext(os.path.basename(job.file_path))[0]
            dest_path = os.path.join(os.path.expanduser("~"), "Desktop", f"[转换]{original_base}.pdf")
            try:
                shutil.copy2(temp_pdf, dest_path)
                self.log_message.emit(f"  → 转换副本已保存到桌面: {os.path.basename(dest_path)}")
            except OSError as e:
                self.log_message.emit(f"  → 保存转换副本到桌面失败: {e}")
        try:
            os.remove(temp_pdf)
        except OSError as e:
            self.log_message.emit(f"  → 清理临时 PDF 失败: {e}")

    @staticmethod
    def _fmt_pages(seq) -> str:
        return " ".join("〔空白〕" if x is None else str(x) for x in seq)

    def _duplex_announce_text(self, items, plan, rotate: bool, tray_foreign: int = 0) -> str:
        names = "、".join((j.display_name or os.path.basename(j.file_path)) for _i, j, _p, _t in items)
        foreign = max(0, int(tray_foreign or 0))
        warn = ""
        if foreign > 0:
            # 常驻窗在第 1 趟**打印期间**就显示 → 操作员能提前知道槽里还有别的纸
            warn = ("\n⚠ 出纸槽里已经压着本次一起打出的 "
                    f"{foreign} 张纸（单面文件/首页，不属于本批）：翻纸时只取本批的 "
                    f"{plan.sheet_count} 张（在最上面），别把下面的一起搬进纸盘 2。")
        return (
            f"本批 {len(items)} 个文件需手动双面：{names}\n"
            f"共 {plan.sheet_count} 张纸 —— 程序自己分两趟打（全程单面），中途只需翻一次纸。\n\n"
            f"第 1 趟（现在打）：{self._fmt_pages(plan.pass_pages(1))}\n"
            f"第 2 趟（翻纸后打）：{self._fmt_pages(plan.pass_pages(2))}\n"
            f"（第 2 趟内容{'旋转 180° 补偿' if rotate else '不旋转'}）"
            + warn
        )

    def _print_manual_batch(self, items, rotate: bool, base_offset: int,
                            total_sides: int) -> tuple[bool, str, int]:
        """把若干个"需手动双面"的文件合并成**一批**：一次翻纸、两趟打完。

        items: [(idx, job, print_path, temp_pdf)]（同批的 rotate 必须一致，调用方已分好组）
        返回 (ok, msg, 物理面数)；物理面数用于推进进度条。

        ⚠ 本批**只包含需手动双面的文件**。本轮里"已单面直打"的文件（1 页文件被单页硬闸门
        排除在批次外、或 duplex=off 的单面文件）**不并进来** —— 它们先打完、压在出纸槽下面，
        第 2 趟从堆顶取纸只取本批那几张（翻纸提示会明确说"只取最上面 N 张"），
        它们就留在出纸槽里等操作员取走。
        **不要**试图把它们并进页序：2026-10-18 试过"追加空白占位页 + 直打单元排最后"，
        结果是占位页落在**堆顶**、真直打纸在**堆底**，第 2 趟的送纸窗口（槽位数 = 第 1 趟
        打出的张数）**永远取不到那张纸** ⇒ 直打纸照旧留着、还白打一张空白。独立审查用真
        纸堆物理模型复现了这一点（`PrintPlan` 两趟长度绑死，物理上不可能表达"已打好的纸"）。
        """
        import pdf_printer as _pp
        p = self._manual_profile
        # 本批的翻纸回执从头清空：这样"第 1 趟还在打时操作员就点了跳过"的预置答案能生效
        self._flip_continue = False
        self._flip_event.clear()
        merged = os.path.join(tempfile.gettempdir(),
                              f"hn_duplex_batch_{os.getpid()}_{int(time.time() * 1000)}.pdf")
        try:
            ok, mmsg = _pp.merge_pdfs([it[2] for it in items], merged)
            if not ok:
                return False, f"手动双面批次：{mmsg}", 0

            # 合并后的页码是**同一份文档**的连续页码：每个文件按自己的页码范围取页，再整体偏移
            file_plans, off = [], 0
            for _idx, job, path, _t in items:
                n = count_pdf_pages(path)
                if n <= 0:
                    n = max(1, job.page_count)
                sel = [i + 1 for i in _pp._parse_page_range(job.page_range, n)]
                if not sel:
                    sel = list(range(1, n + 1))
                label = job.display_name or os.path.basename(job.file_path) or f"#{_idx + 1}"
                for c in range(max(1, job.copies)):
                    file_plans.append(manual_duplex.FilePlan(
                        f"{label}（第{c + 1}份）" if job.copies > 1 else label,
                        [off + x for x in sel], True))
                off += n

            plan = manual_duplex.plan_passes(file_plans, copies=1, first=p.first,
                                             reverse_second=p.reverse_second)
            n1 = len(plan.first_pass)
            # 本轮**本批之前**已经落在出纸槽里、还压在本批下面的张数（直打的单面文件 /
            # 首页 / 上一批）。翻纸文案与"取出哪几张"都要靠它 —— 见 _ask_operator_to_flip。
            tray_foreign = max(0, int(getattr(self, "_tray_sheets", 0) or 0))
            self.log_message.emit(
                f"  → 手动双面批次：{len(items)} 个文件 → {plan.sheet_count} 张纸，"
                f"一次翻纸打两趟（{p.first}/{'逆序' if p.reverse_second else '正序'}/"
                f"{'180°补偿' if rotate else '不旋转'}）")
            self.log_message.emit(f"     第 1 趟: {self._fmt_pages(plan.pass_pages(1))}")
            self.log_message.emit(f"     第 2 趟: {self._fmt_pages(plan.pass_pages(2))}")
            self.duplex_plan.emit(plan.sheet_count, self._estimate_first_pass(plan.sheet_count))
            self.duplex_announce.emit(
                self._duplex_announce_text(items, plan, rotate, tray_foreign))

            def _cb(base: int, slots):
                def _on_side(cur: int, _total: int):
                    lbl, _pg = slots[cur - 1] if 0 < cur <= len(slots) else ("", None)
                    self.progress.emit(base + cur, total_sides,
                                       f"正在打印: {lbl} ({cur}/{len(slots)}面)")
                return _on_side

            map1 = [(s.label, s.page) for s in plan.first_pass]
            map2 = [(s.label, s.page) for s in plan.second_pass]

            _t_pass1 = time.perf_counter()
            ok1, msg1 = print_plan_pass(plan, merged, self._printer_name, 1,
                                        second_rotate_180=rotate,
                                        progress_callback=_cb(base_offset, map1))
            if not ok1:
                return False, f"手动双面第 1 趟失败: {msg1}", n1
            # 第 1 趟的纸已经压进出纸槽了：台账往前推 —— 下一批（长短边混排拆出的第 2 批）
            # 翻纸时就该把这一批的纸算成"槽里已有的"
            self._tray_sheets = tray_foreign + plan.sheet_count
            if self._cancelled:
                return False, "已取消", n1

            w = wait_for_output(self._printer_name, pages=n1)
            if tray_foreign > 0:
                self.log_message.emit(
                    f"  → 第 1 趟出纸（队列 {w['queue_seconds']}s + 余量 {w['margin_seconds']}s）："
                    f"出纸槽里共应有 {plan.sheet_count + tray_foreign} 张纸 —— 最上面 "
                    f"{plan.sheet_count} 张是本批的，下面 {tray_foreign} 张不要动")
            else:
                self.log_message.emit(
                    f"  → 第 1 趟出纸（队列 {w['queue_seconds']}s + 余量 {w['margin_seconds']}s）："
                    f"出纸槽里应有 {plan.sheet_count} 张纸，请数够再翻")
            # 第 1 趟实测 = **权威时刻**：起点在第 1 趟开打前、终点在"纸出完"（下一行才开始等人翻纸），
            # 所以操作员按不按「继续」都不影响它，不会越等越长。扣掉我们自己加的静置余量
            # （那段是保守等待，不是打印机花的时间）。异常样本由 add_speed_sample 内部丢弃。
            _elapsed1 = time.perf_counter() - _t_pass1 - float(w.get("margin_seconds", 0.0) or 0.0)
            if _elapsed1 > 0:
                _est1 = self._estimate_first_pass(n1)
                self.log_message.emit(
                    f"  ⏱ 第 1 趟实测 {_elapsed1:.1f}s（{n1} 张，预估 {_est1:.1f}s）"
                    f"（是否写进速度样本由主线程按物理可信度判定，见紧跟着的一条）")
                if w.get("drained", True):
                    # 一个信号两个用途：① 主线程写档案自动学习；② 常驻窗按这个权威值冻结计时
                    # （界面自己那个时钟只做兜底：信号没到（异常路径）时它照样能冻结）
                    self.duplex_speed_sample.emit(int(n1), float(_elapsed1))
                else:
                    # 队列 120s 内没排空 = 这段"实测"里混着我们的出纸等待超时，不是打印机耗时；
                    # 写进档案会把预估拉长到十几秒/张（打印机离线/卡纸时尤其明显）。
                    self.log_message.emit(
                        "  ⚠ 出纸等待超时（队列没排空）→ 本次实测不写进速度样本")

            if not self._ask_operator_to_flip(plan, tray_foreign):
                if self._cancelled:
                    return False, f"已取消（第 1 趟已打出 {plan.sheet_count} 张纸，纸叠是半成品）", n1
                return False, (f"手动双面未完成：第 1 趟已打出（{plan.sheet_count} 张纸、"
                               f"{self._fmt_pages(plan.pass_pages(1))}），第 2 趟未打印 —— "
                               f"纸叠是半成品，请人工处理"), n1

            ok2, msg2 = print_plan_pass(plan, merged, self._printer_name, 2,
                                        second_rotate_180=rotate,
                                        progress_callback=_cb(base_offset + n1, map2))
            if not ok2:
                return False, f"手动双面第 2 趟失败: {msg2}（第 1 趟已打出）", plan.total_sides
            return True, (f"手动双面完成（{len(items)} 个文件 / {plan.sheet_count} 张纸 / 2 趟，"
                          f"只翻一次纸）"), plan.total_sides
        finally:
            self.duplex_finished.emit("手动双面批次结束")
            try:
                if os.path.isfile(merged):
                    os.remove(merged)
            except OSError:
                pass
            for _idx, job, _path, temp_pdf in items:
                self._cleanup_temp_pdf(job, temp_pdf)

    def _stack_entry_meta(self, items) -> list[dict]:
        """本单各任务的快照（回报云端 + 把 job 找回来都用它，崩溃后清单里也留一份）。"""
        meta = []
        for idx, job, _path, _t in items:
            meta.append({
                "idx": int(idx),
                "task_id": int(getattr(job, "task_id", 0) or 0),
                "order_id": int(getattr(job, "order_id", 0) or 0),
                "order_number": getattr(job, "order_number", "") or "",
                "display_name": (job.display_name or os.path.basename(job.file_path or "")),
                "file_path": job.file_path or "",
                "copies": max(1, int(getattr(job, "copies", 1) or 1)),
                "duplex": (getattr(job, "duplex", "off") or "off"),
                "page_range": getattr(job, "page_range", "") or "",
                "page_count": int(getattr(job, "page_count", 0) or 0),
            })
        return meta

    def _print_stack_pass1(self, items, cover_pdf: str, base_offset: int,
                           total_sides: int) -> tuple[bool, str, int]:
        """攒批模式：把本单（**整单**，含单面文件与封面页）打成第 1 趟，纸留在出纸槽里。

        与 `_print_manual_batch` 的差别就一条：**不等翻纸、不打第 2 趟**。
        第 1 趟出纸后本单就算"堆进纸叠"了，打印机立刻空出来收下一单
        （多任务合并的本质：多个任务的第 1 趟连续打，人工最后统一确认一次）。
        第 2 趟由主线程在整个纸叠上一次性打（DuplexFlushWorker）。

        为什么单面文件与封面页也在本单里：攒批期间出纸槽被这一叠**独占**，中间插进任何
        别的输出（哪怕是一张封面页）都会让整叠第 2 趟的逆序错位串页。它们在页序里各自
        占独立一张纸（FilePlan(duplex=False)），第 2 趟对应槽位是空白占位页（只走纸不打墨）。
        """
        import pdf_printer as _pp
        p = self._manual_profile
        if p is None:
            return False, "攒批模式需要打印机档案（手动双面参数）", 0
        if not self._stack_out_pdf:
            return False, "攒批模式缺少本单输出路径", 0

        paths: list[str] = []
        plans: list[dict] = []
        off = 0
        if cover_pdf and os.path.isfile(cover_pdf):
            # 封面页 = 独立单面纸：第 1 趟打它本身，第 2 趟给一个空白槽（消耗这张纸的背面）
            paths.append(cover_pdf)
            plans.append({"label": "封面页", "pages": [1], "duplex": False})
            off = 1
        try:
            for idx, job, path, _t in items:
                n = count_pdf_pages(path)
                if n <= 0:
                    n = max(1, int(job.page_count or 1))
                sel = [i + 1 for i in _pp._parse_page_range(job.page_range, n)]
                if not sel:
                    sel = list(range(1, n + 1))
                label = job.display_name or os.path.basename(job.file_path) or f"#{idx + 1}"
                # 单页硬闸门（2026-10-17 现场事故）：有效页数 == 1 的文件不按双面配对
                # （1 页凑不出"一张纸的两面"），按单面入叠 —— 它在纸叠里仍占 1 张纸、
                # 第 2 趟对应槽位是空白占位（只走纸不打墨），张数与旧写法完全相同，
                # 只是 file_plans/日志不再谎称它是双面任务。
                dup = ((getattr(job, "duplex", "off") or "off") == "on" and len(sel) > 1)
                paths.append(path)
                for c in range(max(1, int(job.copies or 1))):
                    plans.append({
                        "label": f"{label}（第{c + 1}份）" if int(job.copies or 1) > 1 else label,
                        "pages": [off + x for x in sel],
                        "duplex": dup,
                    })
                off += n

            ok, mmsg = _pp.merge_pdfs(paths, self._stack_out_pdf)
            if not ok:
                return False, f"攒批本单合并失败：{mmsg}", 0

            file_plans = [manual_duplex.FilePlan(fp["label"], list(fp["pages"]), bool(fp["duplex"]))
                          for fp in plans]
            plan = manual_duplex.plan_passes(file_plans, copies=1, first=p.first,
                                            reverse_second=p.reverse_second)
            n1 = len(plan.first_pass)
            self.log_message.emit(
                f"  → 攒批第 {self._stack_seq} 单：{len(items)} 个文件"
                + ("＋封面页" if cover_pdf else "")
                + f" → {plan.sheet_count} 张纸（本趟打第 1 面；第 2 趟等整叠一起打）")
            self.log_message.emit(f"     本单第 1 趟: {self._fmt_pages(plan.pass_pages(1))}")
            self.duplex_plan.emit(plan.sheet_count, self._estimate_first_pass(plan.sheet_count))
            self.duplex_announce.emit(
                f"🖐 攒批模式：正在打第 {self._stack_seq} 单的第 1 趟。\n"
                f"本单 {len(items)} 个文件"
                + ("＋封面页" if cover_pdf else "")
                + f"，{plan.sheet_count} 张纸。打完这一单程序不会停下来等翻纸 ——"
                  f"会直接开始下一单的第 1 趟，直到人工点「开始第 2 趟」。\n"
                  f"本单第 1 趟页序：{self._fmt_pages(plan.pass_pages(1))}\n"
                  f"⚠ 攒批期间出纸槽被这一叠独占：请勿在打印机上手动打印别的东西。")

            # **打印前**先把这一单报给主线程落进纸叠清单并写盘 —— 这样进程被杀/崩溃重启后，
            # 才知道出纸槽里那叠纸是由哪几单拼成的、第 2 趟该怎么打。
            meta = self._stack_entry_meta(items)
            self.stack_entry_planned.emit({
                "seq": self._stack_seq,
                "pdf": os.path.basename(self._stack_out_pdf),
                "file_plans": plans,
                "page_count": int(off),
                "sheet_count": int(plan.sheet_count),
                "pass1_pages": list(plan.pass_pages(1)),
                "pass2_pages": list(plan.pass_pages(2)),
                # 本单打印时用的 first 约定（账本拿去判"这一份第 2 趟有没有内容"）
                "first": p.first,
                "rotate": bool(self._stack_rotate),
                "items": meta,
                "state": duplex_stack.STATE_PRINTING,
            })

            def _cb(base: int, slots):
                def _on_side(cur: int, _total: int):
                    lbl, _pg = slots[cur - 1] if 0 < cur <= len(slots) else ("", None)
                    self.progress.emit(base + cur, total_sides,
                                       f"正在打印(攒批): {lbl} ({cur}/{len(slots)}面)")
                return _on_side

            map1 = [(s.label, s.page) for s in plan.first_pass]
            _t_pass1 = time.perf_counter()
            ok1, msg1 = print_plan_pass(plan, self._stack_out_pdf, self._printer_name, 1,
                                        second_rotate_180=self._stack_rotate,
                                        progress_callback=_cb(base_offset, map1))
            if self._cancelled:
                self.stack_entry_done.emit(self._stack_seq, False, "已取消", n1)
                return False, "已取消", n1
            if not ok1:
                self.stack_entry_done.emit(self._stack_seq, False,
                                           f"攒批第 1 趟失败: {msg1}", n1)
                return False, f"攒批第 1 趟失败: {msg1}", n1

            w = wait_for_output(self._printer_name, pages=n1)
            self.log_message.emit(
                f"  → 攒批第 {self._stack_seq} 单第 1 趟出纸（{n1} 张，排队 {w['queue_seconds']}s "
                f"+ 余量 {w['margin_seconds']}s）—— 纸留在出纸槽里，别动")
            if not w.get("drained"):
                # 队列 120s 内没排空（打印机离线/卡纸/队列卡住）：纸到底出了几张，
                # 程序**看不见**。照旧按"第 1 趟已出纸"记账（与旧口径一致，不擅自判失败），
                # 但必须把不确定性说出来 —— 否则"6 张纸已堆入出纸槽"会被读成事实。
                self.log_message.emit(
                    f"  ⚠ 打印队列 {w['queue_seconds']}s 内没有排空（打印机离线/卡纸/"
                    f"队列卡住？）—— 本单纸数只能人工核对；点「开始第 2 趟」前务必数清"
                    f"进纸盒里的纸")
            _elapsed1 = time.perf_counter() - _t_pass1 - float(w.get("margin_seconds", 0.0) or 0.0)
            if _elapsed1 > 0 and w.get("drained", True):
                # 队列没排空时这段"实测"混着 120s 出纸等待超时，不是打印机耗时 → 不写样本
                self.duplex_speed_sample.emit(int(n1), float(_elapsed1))
            self.stack_entry_done.emit(self._stack_seq, True,
                                       f"第 1 趟完成（{plan.sheet_count} 张纸已堆入出纸槽）", n1)
            return True, f"攒批第 {self._stack_seq} 单第 1 趟完成（{plan.sheet_count} 张）", n1
        except Exception as e:
            # 任何异常都必须报一次 entry_done：主线程靠它解除"出纸槽被独占"的占位，
            # 否则 _stack_pending_ctx 永远挂着 → 之后所有打印请求都排队（看起来像死锁）。
            msg = f"攒批第 1 趟异常：{type(e).__name__}: {e}"
            self.log_message.emit(f"  ✗ {msg}")
            self.stack_entry_done.emit(self._stack_seq, False, msg, 0)
            return False, msg, 0
        finally:
            # 源 PDF 已合并进批次目录内的本单 PDF → 临时转换件可以清了
            # （缓存件 _cleanup_temp_pdf 自己会跳过；keep_temp_pdf 时它先存桌面副本）。
            for _idx, job, _path, temp_pdf in items:
                self._cleanup_temp_pdf(job, temp_pdf)
            if cover_pdf and os.path.isfile(cover_pdf):
                try:
                    os.remove(cover_pdf)
                except OSError:
                    pass

    def run(self):
        """线程主函数：统一 PDF 打印流程（GDI 优先），精确到每面的进度条。"""
        # 本轮打印的出纸槽台账归零（批次之前的直打作业会往上累加，见 _sheets_of_job）
        self._tray_sheets = 0
        try:
            self._converter = get_converter()
            task_count = len(self._jobs)
            success_count = 0
            fail_count = 0

            # 预计算总面数（用于精确进度条）
            job_sides: list[int] = []
            for job in self._jobs:
                # page_count 未知（云端任务/未分析）时，PDF 就**现数**一遍：
                # 否则手动双面的面数会按 1 页估，进度分母偏小 → 进度条冲过 100%。
                n_pages = max(1, job.page_count)
                if job.page_count <= 0 and (job.file_path or "").lower().endswith(".pdf"):
                    try:
                        n_pages = max(1, count_pdf_pages(job.file_path))
                    except Exception:
                        n_pages = 1
                if self._stack_mode:
                    # 攒批模式下**整单**都走两趟（单面文件的第 2 趟是空白占位纸）：
                    # 物理面数 = 纸张数 × 2，且单面文件按「每页一张纸」而不是两页一张纸，
                    # 用 sheets_for 才算得准，否则进度条会冲过 100%。
                    _sel = _count_pages_in_range(job.page_range or "", n_pages) or n_pages
                    sides = duplex_stack.sheets_for(
                        _sel, (job.duplex or "off") == "on", max(1, job.copies)) * 2
                elif self._uses_manual_duplex(job):
                    # 手动双面不插副本分隔页，面数与 estimate_print_sides 不同（会超 100%）
                    sides = estimate_manual_duplex_sides(
                        n_pages, max(1, job.copies), job.page_range)
                else:
                    # 单双面口径必须与真正打印时一致（`_effective_duplex`）：1 页文件按单面算，
                    # 否则"1 页 + 多份 + 双面"会把 `estimate_print_sides` 里的副本分隔页也算
                    # 进去（实测 1 页 ×2 份 估 3 面、实际只打 2 面），进度条永远到不了 100%。
                    sides = estimate_print_sides(
                        n_pages, max(1, job.copies), self._effective_duplex(job), job.page_range,
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
        # 攒批模式：首页**不在这里单独打** —— 出纸槽被这一叠独占，单独打一张首页会插进
        # 纸叠中间，让整叠第 2 趟的逆序错位、串页。改为把它并进本单的第 1 趟页序
        # （占独立一张纸、背面留空），由 _print_stack_pass1 负责合并与清理。
        cover_pdf_path: Optional[str] = None
        cover_consumed = False   # True = 首页已交给攒批单（不走这里的删除）
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
                config.cover_page_price = cfg_dict.get("cover_page_price", 0.10)
                config.pickup_address = cfg_dict.get("pickup_address", "")

                ok = generate_cover_page_pdf(
                    cover_pdf_path, config, self._jobs,
                    order_number=order_number,
                    created_at=created_at,
                )
                if ok and os.path.isfile(cover_pdf_path):
                    if self._stack_mode:
                        cover_consumed = True
                        self.log_message.emit(
                            "📋 首页并入攒批第 1 趟（不单独打——单独打会插进纸叠中间导致串页）")
                    else:
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
                            # 首页也是单独一张纸、压在随后那个手动双面批次下面
                            self._tray_sheets += 1
                        else:
                            self.log_message.emit(f"  ✗ 首页打印失败: {cover_msg}")
                else:
                    self.log_message.emit("  ✗ 首页生成失败")
            except Exception as e:
                self.log_message.emit(f"  ✗ 首页生成异常: {e}")
            finally:
                # 清理临时首页 PDF（攒批模式除外：它还要被合并进本单）
                if cover_pdf_path and os.path.isfile(cover_pdf_path) and not cover_consumed:
                    try:
                        os.remove(cover_pdf_path)
                    except OSError:
                        pass

        offset_sides = 0
        # ── 两阶段打印（2026-10-18）──
        # 阶段 1：逐文件把 PDF 准备好（转换 / 缓存命中），并分流：
        #   · 需手动双面的文件 → deferred（阶段 2 并成"一次翻纸"的一批，两趟）
        #   · 其余 → pending（阶段 2 把**连续同参数**的文件合并成**一个打印作业**）
        # 阶段 2 打印为什么重要（用户现场反馈）：旧写法**一个文件 = 一个打印作业**
        # （每次 print_pdf 都 CreateDC → StartDoc → 逐页渲染 → EndDoc → DeleteDC），
        # 7 个文件就是 7 次作业边界；主机型打印机（LaserJet 1020）在两个作业之间要重做
        # 设备上下文与 DEVMODE 下发、spooler 还要收尾并回传状态 ⇒ 每个文件之间"停一下"。
        # 合并后同一段连续同参数文件共用一个作业，打印机连续成像、中间不留作业边界。
        # ⚠ 一个作业只能有一套 DEVMODE（双面/双面模式/份数/DPI）⇒ 只有这些**全部相同**
        #    的连续文件才并；不同参数自动断成新作业（同订单里混单双面 = 2 个作业，而不是 7 个）。
        deferred: list[tuple[int, PrintJob, str, Optional[str]]] = []
        pending: list[dict] = []     # 阶段 2 待合并打印的项（按 _jobs 顺序）
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
            temp_pdf: Optional[str] = None
            defer = self._stack_mode or self._uses_manual_duplex(job)   # 需手动双面 → 并批，稍后统一两趟

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
                    # S1-04：云端任务（task_id>0，文件是顾客上传的）⇒ 强制 LibreOffice，
                    # 绝不在打印路径上给 Word/WPS 执行宏的机会；本地任务保持原三引擎降级链。
                    _from_cloud = int(getattr(job, "task_id", 0) or 0) > 0
                    temp_pdf = self._converter.convert(
                        job.file_path, image_orientation=getattr(job, 'image_orientation', 'auto'),
                        from_cloud=_from_cloud)
                    print_path = temp_pdf
                    self.log_message.emit(f"  → 转换完成: {os.path.basename(temp_pdf)}")

                # 1.5 图片转换后写入方向缓存（方案A：同图同方向免重复下载/转换）。
                # worker 只写 PDF 文件，索引由主线程补（meta 为空时 GUI 缓存命中不采纳）。
                if temp_pdf and os.path.isfile(temp_pdf) and job.source_md5:
                    _ext_i = os.path.splitext(job.file_path)[1].lower()
                    if _ext_i in (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tiff", ".tif"):
                        try:
                            _key = pdf_cache_key(job.source_md5, getattr(job, 'image_orientation', 'auto'))
                            import paths as _paths
                            _cdir = _paths.pdf_cache_dir()
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

                # 2. 分流（见上方两阶段说明）
                # 单页硬闸门·**转换件补判**（2026-10-17 现场事故）：job.page_count 未知(0) 时
                # 上面的 defer 按"未知不当成 1 页"判成了"要两趟"；但转换出来的 PDF 此刻就在
                # 手上、页数可数 —— 数出来确实只有 1 页的就地纠正回"单面直打"，免得给它配一张
                # 空白占位纸、白翻一次纸（正是事故的浪费形态）。
                # ⚠ 只对"原件不是 PDF"（= print_path 是转换件）生效：原件本身就是 PDF 的情形由
                #   `_uses_manual_duplex`（按 job.page_count）与 `_print_one_job`（按真实 PDF）裁决
                #   ——既有验收把「1 页 PDF 参与手动双面合批」钉成了既有行为，不能在这里推翻。
                if (defer and not self._stack_mode
                        and int(getattr(job, "page_count", 0) or 0) <= 0
                        and not str(job.file_path or "").lower().endswith(".pdf")
                        and self._effective_pages(job, print_path) == 1):
                    self.log_message.emit(
                        "  ⚠ 转换后实际只打 1 页 → 按单面直打"
                        "（不入手动双面批次：省一张空白占位纸、省一次翻纸）")
                    defer = False
                if defer:
                    # 手动双面：不在这里打 —— 并入批次，等本批所有文件都准备好后**一次翻纸**打完
                    deferred.append((idx, job, print_path, temp_pdf))
                    self.log_message.emit(
                        f"  ⏳ 已并入手动双面批次（本批第 {len(deferred)} 个文件）")
                    continue
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
                # 合并分组键：一个作业只能有一套 DEVMODE（**实际生效的**双面 / 模式 / 份数 /
                # DPI），**外加页码范围**。
                # ⚠ 双面必须用 `_effective_duplex`（= 真实 PDF 页数判过的值），**不能**用
                #   `job.duplex` 原始请求：1 页文件请求双面时生效值是 "off"（单页硬闸门），
                #   用原始请求会让两个 1 页文件合成一个 `duplex=on` 的作业 ——
                #   手动双面机型被驱动硬拒（整组失败）、自动双面机型两页挤一张纸。
                #   这是 2026-10-17 那场事故的同一状态，别再绕过闸门。
                #   （合并提交时就传这个生效值，不再按合并件页数重算：N 个单页文件合起来
                #    当然 >1 页，但每一份仍是单面。）
                # ⚠ 页码范围必须进键：否则"只打第 2 页"的文件会被并进"打全部"的那一段，
                #   结果把它整份打出来（多打纸、还多收费）。不同页码范围各自成段即可。
                # ⚠ 方向**不进键**：渲染是逐页按自己尺寸做的，同一作业里混竖横是安全的；
                #   它只影响日志文案，按第一项带过去即可。
                _eff_duplex = self._effective_duplex(job, self._effective_pages(job, print_path))
                pending.append({
                    "idx": idx, "job": job, "path": print_path, "temp_pdf": temp_pdf,
                    "copies": copies, "dpi": effective_dpi, "ori": eff_ori,
                    "duplex": _eff_duplex,
                    "sides": job_sides[idx] if idx < len(job_sides) else 1,
                    "key": (_eff_duplex, str(job.duplex_mode or ""),
                            copies, effective_dpi, str(getattr(job, "page_range", "") or "")),
                })
            except Exception as e:
                self.log_message.emit(f"  ✗ 错误: {e}")
                fail_count += 1
                self.job_finished.emit(idx, False, str(e))
                # 准备阶段就失败（文件丢失 / 转换失败）：临时件此刻无人再引用，就地清掉
                if temp_pdf:
                    self._cleanup_temp_pdf(job, temp_pdf)

        # ── 阶段 2a：把**连续同参数**的直打文件合并成一个打印作业 ──
        if pending:
            groups: list[list[dict]] = []
            for rec in pending:
                if (groups and rec["idx"] == groups[-1][-1]["idx"] + 1
                        and rec["key"] == groups[-1][-1]["key"]):
                    groups[-1].append(rec)
                else:
                    groups.append([rec])
            for grp in groups:
                # 逐文件结果（不是整组一个）：组里可能有"读不出页数"被排除在合并之外的文件，
                # 它们必须被单独报成败 —— 整组一个结果会让它们被静默丢掉却报成功。
                res, pad_sides = self._print_merged_group(grp, offset_sides, total_sides)
                if pad_sides:
                    # 自动双面在**文件分界**补的空白页也是真实送进打印机的一面 ⇒ 进度分母
                    # 同步加（多份之间的分隔页已由各任务 `estimate_print_sides` 计入，别重复加）。
                    # 不加的话状态栏会出现 "12/11 面" 这种读数（进度条被 Qt 夹住，但数字不对），
                    # 而且后面几组的偏移会整体偏小。
                    total_sides += int(pad_sides)
                for rec in grp:
                    ok_g, msg_g = res.get(rec["idx"], (False, "打印结果缺失（内部错误）"))
                    if ok_g:
                        success_count += 1
                        # 这份纸已经落在出纸槽里了 → 记进台账：后面的手动双面批次翻纸时要靠它
                        # 算清"槽里到底有几张"（算少了操作员就会把不属于本批的纸一起搬走）
                        if rec["path"] and os.path.isfile(rec["path"]):
                            self._tray_sheets += self._sheets_of_job(
                                rec["job"], rec["copies"], rec["path"])
                    else:
                        fail_count += 1
                    self.job_finished.emit(rec["idx"], ok_g, msg_g)
                # 进度分母按"这一组实际打了多少面"推进（成功那几份的面数之和）。
                # ⚠ 这里**不夹上限**：夹了就再也回不去，预估值累计略超时进度会提前顶到 100%；
                #   `emit` 的那两处各自 min() 一次即可（纯观感）。
                offset_sides += sum(
                    int(r.get("sides") or 0) for r in grp
                    if res.get(r["idx"], (False, ""))[0]) + int(pad_sides or 0)

        # ── 攒批模式：本单只打第 1 趟，纸堆进出纸槽；第 2 趟等整叠一起打 ──
        if deferred and self._stack_mode:
            if self._cancelled:
                self.duplex_finished.emit("攒批本单已取消")
                for _idx, job, _p, temp_pdf in deferred:
                    self.job_finished.emit(_idx, False, "已取消")
                    fail_count += 1
                    self._cleanup_temp_pdf(job, temp_pdf)
                if cover_consumed and cover_pdf_path and os.path.isfile(cover_pdf_path):
                    try:
                        os.remove(cover_pdf_path)
                    except OSError:
                        pass
            else:
                ok_s, msg_s, sides_s = self._print_stack_pass1(
                    deferred, cover_pdf_path if cover_consumed else "",
                    offset_sides, total_sides)
                # 本单的任务**不在这里**报完成：第 1 趟只是半成品，sent/report_success 要等
                # 第 2 趟打完（由主线程在整叠打完时统一收尾 —— 见 _finish_stack_entries）。
                offset_sides += sides_s
                self.log_message.emit(f"  {'✓' if ok_s else '✗'} {msg_s}")
            self.progress.emit(offset_sides, total_sides, "本单第 1 趟完成")
            self.all_finished.emit(0, 0)
            self.log_message.emit(
                "========== 攒批本单第 1 趟结束（等待整叠第 2 趟）=========="
            )
            return

        # ── 手动双面批次：把本次打印里所有"需手动双面"的文件合并成**一次翻纸**打完 ──
        if deferred:
            if self._cancelled:
                for _idx, job, _p, temp_pdf in deferred:
                    self.job_finished.emit(_idx, False, "已取消")
                    fail_count += 1
                    self._cleanup_temp_pdf(job, temp_pdf)
            else:
                # 已发过 job_finished 的任务号：异常兜底时只补发**没报过的**，避免重复回报
                # （重复回报会让主线程把同一个任务结算两次）。
                reported: set[int] = set()
                try:
                    # 按装订方向分组：同批必须用同一个旋转补偿（长边补偿 180°、短边不补偿）。
                    # 一般整批同向 → 一批一次翻纸；长短边混排才拆成两批（翻两次，并记日志）。
                    groups: dict[bool, list] = {}
                    for it in deferred:
                        groups.setdefault(self._rotate_for_job(it[1]), []).append(it)
                    if len(groups) > 1:
                        self.log_message.emit(
                            f"⚠ 本批存在两种装订方向，将分 {len(groups)} 次翻纸")
                    for rotate, items in groups.items():
                        self.log_message.emit(
                            f"▶ 手动双面批次：{len(items)} 个文件，"
                            f"{'长边（第 2 趟内容旋转 180°）' if rotate else '短边（第 2 趟内容不旋转）'}"
                            f" —— 整批只翻一次纸")
                        # ⚠ 本批**只含**手动双面的文件：本轮已单面直打的纸先打完、压在出纸槽
                        # 下面，第 2 趟从堆顶取纸只取本批那几张（翻纸文案说"只取最上面 N 张"）。
                        # 不要把它们并进页序 —— 2026-10-18 试过，占位页落在堆顶、真直打纸
                        # 在堆底，第 2 趟永远取不到那张纸（记在 _print_manual_batch 的说明里）。
                        ok_b, msg_b, sides_b = self._print_manual_batch(
                            items, rotate, offset_sides, total_sides)
                        for _idx, _job, _p, _t in items:
                            self.job_finished.emit(_idx, ok_b, msg_b)
                            reported.add(_idx)
                            if ok_b:
                                success_count += 1
                            else:
                                fail_count += 1
                        self.log_message.emit(f"  {'✓' if ok_b else '✗'} {msg_b}")
                        offset_sides += sides_b
                except Exception as e:
                    # ── **总兜底**：异常绝不能逃出 run() ──
                    # 逃出去之后 job_finished / all_finished 一个都不发，主线程收不到收尾
                    # 信号 ⇒ 标签页永久停在"打印中"（用户只能重启工具），而日志里只剩
                    # traceback、没有可读原因。宁可整批判失败，也要把信号发全。
                    emsg = f"手动双面批次异常：{type(e).__name__}: {e}"
                    self.log_message.emit(f"  ✗ {emsg}")
                    for _idx, _job, _p, temp_pdf in deferred:
                        if _idx in reported:
                            continue
                        reported.add(_idx)
                        fail_count += 1
                        self.job_finished.emit(_idx, False, emsg)
                        # 兜底清理（批次自己的 finally 已经清过；这里防"还没进批次就炸"）
                        self._cleanup_temp_pdf(_job, temp_pdf)

        self.progress.emit(total_sides, total_sides, "全部完成")
        self.all_finished.emit(success_count, fail_count)
        self.log_message.emit(
            f"========== 打印完毕：成功 {success_count}，失败 {fail_count} =========="
        )


class DuplexFlushWorker(QThread):
    """攒批模式：把**整个纸叠**的第 2 趟一次性打完（一轮只翻一次纸的落地处）。

    输入是主线程拼好的整叠（`duplex_stack.DuplexStack`：各单合并 PDF 的加入顺序 +
    各单的文件计划）。本类只做三件事：把各单 PDF 合成一份整叠文档 → 用
    `manual_duplex.plan_passes` 重建整叠页序 → `pdf_printer.print_plan_pass(which=2)`
    打第 2 趟。

    ⚠ 页序与 180° 补偿都**不在这里重新定义**：一律走 manual_duplex（页序唯一真相）与
    print_plan_pass（第 2 趟旋转的唯一落点）。这两处一旦各写一遍就必然打错页/打颠倒。
    """

    progress = Signal(int, int, str)
    log_message = Signal(str)
    done = Signal(bool, str, int)     # (ok, message, 第 2 趟打印面数)

    def __init__(self, stack, batch_dir: str, printer_name: str,
                 dpi: int = 0, parent=None):
        super().__init__(parent)
        self._stack = stack
        self._batch_dir = batch_dir
        self._printer_name = printer_name
        self._dpi = int(dpi or 0)

    def run(self):
        import pdf_printer as _pp
        try:
            pdfs = self._stack.entry_pdfs(self._batch_dir)
            if not pdfs:
                self.done.emit(False, "纸叠里没有可打的单", 0)
                return
            missing = [p for p in pdfs if not os.path.isfile(p)]
            if missing:
                self.done.emit(
                    False, f"整叠源文件缺失 {len(missing)} 个（{os.path.basename(missing[0])} 等），"
                           f"无法打第 2 趟 —— 纸还在出纸槽里，请人工处理", 0)
                return
            # 页序**先**算（纯逻辑）：撤走项已由账本从 build_plan() 里剔除（只吃 active_plans）。
            # ⚠ 合并 PDF 仍然要合并**全部**各单：页序里的绝对页码是按 entry.page_count 累计的
            #   偏移，少合并一份文档会让后面所有页码整体前移 ⇒ 第 2 趟打到别的页上。
            plan = self._stack.build_plan()
            slots = plan.pass_slots(2)
            total = len(slots)
            if total <= 0:
                # 防御性：正常路径已在主窗口拦住（在叠张数 == 0 → 直接按成品收尾）。
                # 这里绝不去跑打印循环（白抽纸），也不谎称打成功（半成品当成品 = 白收钱）。
                self.done.emit(False, "第 2 趟无内容（没有要送的纸，未打印）", 0)
                return
            merged = os.path.join(self._batch_dir, "merged_pass2.pdf")
            present = _stack_present_sheet_count(self._stack)
            self.log_message.emit(
                f"▶ 攒批第 2 趟：{self._stack.count} 单 / {present} 张纸，"
                f"{'长边（内容旋转 180° 补偿）' if self._stack.rotate else '短边（不旋转）'}")
            ok, msg = _pp.merge_pdfs(pdfs, merged)
            if not ok:
                self.done.emit(False, f"整叠合并失败：{msg}", 0)
                return

            def _on_side(cur: int, _total: int):
                lbl = slots[cur - 1].label if 0 < cur <= total else ""
                self.progress.emit(cur, total, f"正在打第 2 趟(整叠): {lbl} ({cur}/{total}面)")

            ok2, msg2 = print_plan_pass(
                plan, merged, self._printer_name, 2,
                second_rotate_180=bool(self._stack.rotate),
                progress_callback=_on_side, dpi=self._dpi)
            if not ok2:
                self.done.emit(False, f"整叠第 2 趟失败：{msg2}", total)
                return
            self.done.emit(True, f"整叠第 2 趟完成（{self._stack.count} 单 / "
                                 f"{present} 张纸 / {total} 面）", total)
        except Exception as e:
            self.done.emit(False, f"整叠第 2 趟异常：{type(e).__name__}: {e}", 0)


class ConvertWorker(QThread):
    """
    后台线程：将 Word 文件转为 PDF。
    不阻塞 UI，转换完成后通过信号返回结果。
    """
    # (row, file_path, cached_pdf, page_count, orientation)
    # file_path 用于回调按文件匹配行，避免多文件订单行号错位
    finished = Signal(int, str, str, int, str)

    def __init__(self, row: int, file_path: str, engine: str, source_md5: str = "",
                 from_cloud: bool = False):
        super().__init__()
        self._row = row
        self._file_path = file_path
        self._engine = engine
        self._source_md5 = source_md5
        # S1-04：文件来自云端（顾客上传，内容不可信）⇒ 转换绝不走 Word/WPS COM（宏执行路径），
        # 强制 LibreOffice。默认 False = 用户本地文件，保持原三引擎降级链（零回归）。
        self._from_cloud = bool(from_cloud)
        self._cancelled = False

    def cancel(self):
        """协作式取消：置标志，run() 各阶段检查后优雅退出（替代 terminate 强杀）。"""
        self._cancelled = True

    def run(self):
        from converter import _convert_via_word_com, _convert_via_wps_com, get_converter
        from pdf_printer import get_pdf_info
        import paths as _paths

        # PDF 缓存目录（与 cloud_client.py 共用 %APPDATA%\HN打印工具\pdf_cache）
        cache_dir = _paths.pdf_cache_dir()
        os.makedirs(cache_dir, exist_ok=True)
        temp_pdf: str | None = None
        try:
            ext = os.path.splitext(self._file_path)[1].lower()
            if self._cancelled:
                self.finished.emit(self._row, self._file_path, "", 0, "")
                return
            if (ext in (".doc", ".docx") and self._engine != "libreoffice"
                    and not self._from_cloud):
                # ⚠ 云端来源必须在上面这条闸门处就被挡住（S1-04）：走 else 分支交给
                #   converter.convert(from_cloud=True) ⇒ LibreOffice，绝不给 Word/WPS 机会。
                # 直接以 MD5 命名存入 pdf_cache（docx 缓存 key 含引擎后缀，防止 WPS 文档
                # 被 Word 引擎转换的错误 PDF 被复用；若无 MD5 则用临时文件）
                if self._source_md5:
                    # 引擎后缀判定统一走 cache_engine_for（与读侧/索引侧同一口径）
                    _eng_key = cache_engine_for(self._file_path, self._engine)
                    temp_pdf = os.path.join(
                        cache_dir, pdf_cache_key(self._source_md5, "", _eng_key) + ".pdf")
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
                temp_pdf = converter.convert(self._file_path, from_cloud=self._from_cloud)
        except Exception:
            # Word/WPS 引擎失败，降级到 LibreOffice
            if (ext in (".doc", ".docx") and self._engine != "libreoffice"
                    and not self._from_cloud):
                try:
                    if temp_pdf and os.path.isfile(temp_pdf):
                        os.remove(temp_pdf)
                except OSError:
                    pass
                try:
                    converter = get_converter()
                    temp_pdf = converter.convert(self._file_path, from_cloud=self._from_cloud)
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
        self.setMinimumWidth(820)
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

        # ── 表格：订单号 | 文件数 | 总页数 | 成员 | 来源 | 备注 | 状态 | 操作 ──
        from PySide6.QtWidgets import QTableWidget as _QTW, QTableWidgetItem as _QTWI
        self._table = _QTW()
        self._table.setColumnCount(8)
        self._table.setHorizontalHeaderLabels(
            ["订单号", "文件数", "总页数", "成员", "来源", "备注", "状态", "操作"])
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
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.Stretch)   # 备注列自适应撑开
        hh.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(7, QHeaderView.ResizeToContents)
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

            # 成员（顾客订单 = 下单用户绑定的成员名；管理员自行打印 = 归属人）
            first_task = tasks[0] if tasks else None
            member = ""
            if first_task is not None:
                member = (getattr(first_task, 'bound_owner_name', '') or ''
                          or getattr(first_task, 'owner_name', '') or '')
            self._table.setItem(row, 3, _QTWI(member or "—"))

            # 来源（wechat/app/local）
            src = (getattr(first_task, 'source', '') or '') if first_task is not None else ''
            src_text = {"wechat": "小程序", "app": "APP", "local": "本地"}.get(src, src or "—")
            self._table.setItem(row, 4, _QTWI(src_text))

            # 备注（截断显示，悬停显示全文）
            remark = (getattr(first_task, 'remark', '') or '').strip() if first_task is not None else ""
            if remark:
                remark_item = _QTWI(remark if len(remark) <= 16 else remark[:16] + "…")
                remark_item.setToolTip(remark)
            else:
                remark_item = _QTWI("—")
            self._table.setItem(row, 5, remark_item)

            # 状态
            status_text = {"pending": "待确认", "canceled": "已取消", "accepted": "已添加", "rejected": "已打回"}
            status_item = _QTWI(status_text.get(status, status))
            if status == "canceled":
                status_item.setForeground(QColor("#999"))
            self._table.setItem(row, 6, status_item)

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

                self._table.setCellWidget(row, 7, btn_widget)
            elif status == "canceled":
                info_btn = QPushButton("✕ 已取消")
                info_btn.setFixedSize(80, 26)
                info_btn.setStyleSheet("font-size:11px; padding:0;")
                info_btn.setEnabled(False)
                self._table.setCellWidget(row, 7, info_btn)
            else:
                self._table.setItem(row, 7, _QTWI("—"))

        has_pending = any(e["status"] == "pending" for e in self._pending_orders.values())
        self._accept_all_btn.setEnabled(has_pending)
        self._reject_all_btn.setEnabled(has_pending)

    # ── 单订单操作 ──

    def _on_accept_order(self, tasks: list):
        """确认添加某订单的全部文件 → 直接从列表移除（用户已接管，无需保留「已添加」行）。"""
        oid = tasks[0].order_id or tasks[0].task_id
        self._pending_orders.pop(oid, None)
        self.order_accepted.emit(tasks)
        self._rebuild_table()
        self._check_auto_close()

    def _on_reject_order(self, tasks: list):
        """打回某订单的全部文件 → 直接从列表移除。"""
        oid = tasks[0].order_id or tasks[0].task_id
        self._pending_orders.pop(oid, None)
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
            self._pending_orders.pop(oid, None)
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
            self._pending_orders.pop(oid, None)
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
# S1-09：离线未同步订单窗口
# ============================================================

class OfflineOrdersDialog(QDialog):
    """「离线未同步订单」窗口：列出本地离线库里 `synced=0` 的订单，并提供手动重试。

    为什么需要它（S1-09 现场问题）：离线期间打的单只写在本地 `offline_orders` 表里，
    云端**没有**这张单 —— 结算页自然看不到、金额也不会入账。旧实现只有一条日志、且
    重试 5 次后该行会被**永久跳过**，等于订单静默消失。现在：
      · 状态列明确写「⏳ 离线未同步 / ⚠ 云端拒收」，**绝不**显示成「已完成」；
      · 永不放弃重试（指数退避），也可在这里点「重试上传」立刻再来一次；
      · 表格下方给出"还差几张单没上云"的总结与最近失败原因。
    """

    retry_requested = Signal(str)   # 订单号（空串 = 全部）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("📴 离线未同步订单")
        self.setMinimumSize(720, 420)
        self._rows: list[dict] = []

        root = QVBoxLayout(self)
        self._hint = QLabel(
            "下列订单**只存在本机**，云端还没有它们（结算页也就看不到）。"
            "联网后会自动补报（失败按 1.5× 指数退避，永不放弃）；也可以现在就手动重试。")
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["订单号", "创建时间", "金额", "状态", "最近失败原因"])
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.Stretch)
        root.addWidget(self._table, 1)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)

        self._summary = QLabel("")
        root.addWidget(self._summary)

        btns = QHBoxLayout()
        self._retry_selected_btn = QPushButton("🔄 重试选中")
        self._retry_selected_btn.clicked.connect(self._on_retry_selected)
        self._retry_all_btn = QPushButton("🔄 全部重试")
        self._retry_all_btn.clicked.connect(lambda: self.retry_requested.emit(""))
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.reload)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        btns.addWidget(self._retry_selected_btn)
        btns.addWidget(self._retry_all_btn)
        btns.addStretch(1)
        btns.addWidget(refresh_btn)
        btns.addWidget(close_btn)
        root.addLayout(btns)

    # ── 数据 ──

    @staticmethod
    def status_text(row: dict) -> str:
        """状态列文案（**不许**把未同步单写成「已完成」）。"""
        st = str(row.get("status") or "")
        retries = int(row.get("retry_count") or 0)
        if st == "rejected":
            return "⚠ 云端拒收（需处理）"
        if retries > 0:
            return f"⏳ 离线未同步（已重试 {retries} 次）"
        return "⏳ 离线未同步"

    def set_rows(self, rows: list[dict]):
        """填充表格（rows 来自 OfflineSync.list_unsynced_orders）。"""
        self._rows = list(rows or [])
        self._table.setRowCount(len(self._rows))
        for i, r in enumerate(self._rows):
            vals = [
                str(r.get("order_number") or ""),
                str(r.get("created_at") or ""),
                f"¥{float(r.get('total_price') or 0):.2f}",
                self.status_text(r),
                str(r.get("last_error") or ""),
            ]
            for col, v in enumerate(vals):
                self._table.setItem(i, col, QTableWidgetItem(v))
        n = len(self._rows)
        if n == 0:
            self._summary.setText("✅ 没有离线未同步订单（本地库里的订单都已上报云端）")
        else:
            rejected = sum(1 for r in self._rows if str(r.get("status") or "") == "rejected")
            extra = f"，其中 {rejected} 条被云端拒收（需人工处理）" if rejected else ""
            self._summary.setText(f"共 {n} 条离线未同步订单{extra}（云端尚未入账）")
        self._retry_all_btn.setEnabled(n > 0)
        self._retry_selected_btn.setEnabled(False)

    def reload(self):
        """从本地库重新读一遍（窗口打开期间/手动点刷新时用）。"""
        sync = getattr(self, "_offline_sync", None)
        if sync is None:
            self.set_rows([])
            return
        try:
            self.set_rows(sync.list_unsynced_orders())
        except Exception as e:
            logger.warning(f"读取离线未同步订单失败: {e}")
            self.set_rows([])

    def set_offline_sync(self, sync):
        self._offline_sync = sync

    # ── 交互 ──

    def _on_selection_changed(self):
        row = self._table.currentRow()
        self._retry_selected_btn.setEnabled(0 <= row < len(self._rows))

    def _on_retry_selected(self):
        row = self._table.currentRow()
        if row < 0 or row >= len(self._rows):
            return
        num = str(self._rows[row].get("order_number") or "")
        if num:
            self.retry_requested.emit(num)


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
    _pricingSynced = Signal()         # 打印价格同步完成（可能来自后台线程）→ 主线程刷新 UI
    _updateCheckDone = Signal(object) # 自更新后台线程 → 主线程结果 (kind, manifest, manual)
    _updateProgress = Signal(int, int) # 更新下载进度（已下载, 总大小）→ 主线程进度弹窗

    def __init__(self, config_path: str = "print_config.json", theme_manager: ThemeManager | None = None):
        super().__init__()
        self._config_path = config_path
        self._config = PrinterConfig.load(config_path)
        # P1（审计 2026-12）：配置落盘锁。后台线程（价格同步/成员名单同步/本地订单上传）
        # 与主线程会并发写同一份 print_config.json，save() 的 tmp+os.replace 只保证
        # 单次写不撕裂，不保证并发调用不互相覆盖，故在此串行化。
        self._config_save_lock = threading.Lock()
        self._worker: Optional[PrintWorker] = None
        self._pending_jobs: list[PrintJob] = []
        self._theme_manager = theme_manager
        self._last_dir = self._config.last_dir
        self._copy_total_btn: Optional[QPushButton] = None
        self._copy_total_timer: Optional[QTimer] = None
        self._all_printed = False  # 是否已完成全部打印

        # ── 文件日志 ──
        # 日志目录统一在 %APPDATA%\HN打印工具\logs（安装覆盖不丢日志）
        import paths as _paths
        self._log_dir = _paths.logs_dir()
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

        # ── 云端直连自检 ──
        # 本机若开着 Clash/mihomo 之类代理（HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 或系统代理），
        # 打印工具的云端流量会默认走代理：代理节点/规则异常时表现为「TCP 连上但收不到数据」，
        # 长连接还会在代理重载配置时被掐掉（长期挂后台尤其明显）。此处把云端主机登记进
        # 本进程 NO_PROXY 直连，并留一行日志便于事后判断断连是不是代理造成的。
        try:
            import net_direct
            net_direct.ensure_direct(self._config.cloud_api_url, self._config.cloud_ws_url)
            info = net_direct.describe(self._config.cloud_api_url)
            if info:
                self._file_logger.info("☁ 云端直连: " + info)
        except Exception as e:
            self._file_logger.warning(f"云端直连自检失败（不影响启动）: {e}")

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
                self._config.tabs = kept_tabs if kept_tabs else {"1": self._make_new_tab()}
                if self._config.active_tab not in self._config.tabs:
                    self._config.active_tab = next(iter(self._config.tabs))
                self._save_config(sync_ui=False)
                self._file_logger.info("启动清理：已移除 %d 个已完成订单标签页", removed_count)

        # ── 启动迁移：旧版遗留在 %TEMP% 的云端任务文件 → 暂存目录 ──
        # 必须早于 CloudClient.start()（它会清掉 %TEMP% 下 24h 以上的 hn_cloud_*，
        # 那正是"列表里还有任务、文件却没了"的旧隐患）
        try:
            self._adopt_legacy_cloud_files()
        except Exception as e:
            self._file_logger.warning("接管旧版云端临时文件失败: %s", e)

        # ── 标签页系统 ──
        # 确保 tabs 中至少有一个标签
        if not self._config.tabs:
            self._config.tabs = {"1": self._make_new_tab()}
        self._current_tab = self._config.active_tab or "1"
        if self._current_tab not in self._config.tabs:
            self._current_tab = next(iter(self._config.tabs.keys()))
        # 撤回备份（每个标签独立）
        self._cleared_jobs_backup: dict[str, list[PrintJob]] = {}
        # 已处理的云端任务 ID（防重复弹窗；跨重启持久化，防重启后重复打印）
        self._processed_cloud_tasks: set[int] = self._load_processed_tasks()
        # 无障碍自动打印：打印机忙时暂存的重试队列（打印完成后自动补打）
        self._auto_print_retry: list[dict] = []
        # 已进入无障碍队列的 task_id（🔴-B4-1 幂等守卫）：`_processed_cloud_tasks` 只在
        # **真正处理后**才写，"下载完成 → 防抖到期"之间存在数秒窗口，期间任何重复
        # task_updated(ready)（SocketIO 重推 / HTTP 兜底 / 状态队列重放）都会再次入队并
        # **反复重置防抖**（订单可能永远等不到处理）。处理完成时从这里移除。
        self._enqueued_cloud_tasks: set[int] = set()
        # ── 攒批模式（多任务合并翻纸，2026-10）──
        # 一叠正在累积的手动双面第 1 趟纸张（见 duplex_stack.py）。开关落在
        # config.duplex_stack_mode；本对象只在内存里，清单另存 %APPDATA%\...\duplex_stack\<batch>\。
        self._stack = duplex_stack.DuplexStack(
            capacity=self._printer_profile().stack_capacity,
            warn_after_sec=self._printer_profile().stack_warn_after_sec,
            printer_name=self._config.printer_name)
        # 攒批期间被挡住的打印请求（出纸槽被这一叠独占，插进来就会串页）：
        # [{"jobs": [...], "tab_key": str, "reason": str}]
        self._stack_wait: list[dict] = []
        # 已受理、等**上一单第 1 趟**打完就接着打的攒批单（2026-10-21）：
        # [{"jobs": [...], "tab_key": str}]。与 `_stack_wait` 分开：那个要等整叠第 2 趟
        # 打完才放行，而这个只要打印机从第 1 趟空出来就立刻接上（攒批"连续堆第 1 趟"的本意）。
        self._stack_entry_wait: list[dict] = []
        # 操作员**主动**收起过攒批列表吗（2026-10-21）：收起后不许被 _sync_stack_list_visible
        # 反复弹回来（否则"关掉"这个动作根本无效）；点底部 🖐 / 切开关 / 有新单一叠时清零。
        self._stack_list_user_closed: bool = False
        self._stack_flush_worker: DuplexFlushWorker | None = None
        # 第 2 趟开始那一刻的整叠快照（_on_stack_flush 冻结 → _on_stack_flush_done 按它收尾）：
        # 防止第 2 趟期间混进新单后被一起当成品 report_success（🔴-3 的第二道防线）
        self._stack_flush_snapshot: list = []
        self._stack_pending_ctx: dict = {}   # 正在打第 1 趟的那一单的上下文（见 _start_stack_entry）
        # 打印中保活：作业进行中每 60 秒刷新后端 locked_at（见 _tick_keepalive）
        self._keepalive_timer = QTimer(self)
        self._keepalive_timer.setInterval(60_000)
        self._keepalive_timer.timeout.connect(self._tick_keepalive)
        self._keepalive_timer.start()

        # 攒批模式状态变化 → 上报云端（移动端实时显示「攒批中 / 攒批未开」+ 能否远程切换）；
        # 2 秒比对一次，**变化才发**（不刷屏、不打扰服务端）。2026-10
        self._last_stack_sig = None
        self._stack_state_timer = QTimer(self)
        self._stack_state_timer.setInterval(2_000)
        self._stack_state_timer.timeout.connect(self._stack_state_tick)
        self._stack_state_timer.start()
        self._keepalive_last: tuple = ()     # 上次实际发送的任务集合（变了才立刻补发一次）
        # 无障碍后台打印：当前打印批次所属的标签页 key（后台创建时不切换前台；
        # 打印启动/回报/冻结据此定位标签页，空 = 前台标签页）
        self._printing_tab_key: str = ""
        # 打印机消失/改名提示只弹一次（避免每次刷新都弹窗）
        self._printer_missing_warned: bool = False

        # ── 云端客户端 ──
        self._cloud_client: CloudClient | None = None
        self._stats_server: StatsServer | None = None  # 收支清算统计 HTTP 服务器
        self._cloud_tasks: dict[int, CloudTask] = {}  # task_id → CloudTask
        self._cloud_task_window: CloudTaskListWindow | None = None
        self._offline_sync: OfflineSync | None = None  # 初始化在 _init_cloud_client 之后
        self._offline_dialog = None                    # 「离线未同步订单」窗口（懒创建）
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
        # 本会话被用户取消的订单 ID（P2-9）：push 与 cancel 并发时 socket 事件可能乱序，
        # 迟到的 print_task 不得重新注册预约状态机/自动打印队列，防止打印已取消订单
        self._cloud_canceled_orders: set[int] = set()
        self._cloud_connected: bool = False  # 云端连接状态（预约冻结自恢复判断用）
        # 接单状态缓存（后端 printer_state 推送）：is_active/active_owner_name/active_device_name
        self._cloud_printer_state_cache: dict = {}
        # 成员名单刷新防抖：下拉反复 Show（用户快速点开/关闭）不并发叠加请求
        self._owner_refresh_last_ts: float = 0.0
        self._owner_refresh_inflight: bool = False
        self._scheduled_check_timer = QTimer(self)
        self._scheduled_check_timer.setInterval(1000)
        self._scheduled_check_timer.timeout.connect(self._check_scheduled_timeouts)
        self._scheduled_check_timer.start()
        # 单实例置前通知：新实例启动时经命名事件通知本实例，把窗口置于前台（500ms 轮询）
        from single_instance import get_front_event, check_front_request
        self._front_event = get_front_event()
        if self._front_event:
            self._front_timer = QTimer(self)
            self._front_timer.timeout.connect(self._on_bring_to_front_request)
            self._front_timer.start(500)
        # 价格周期校验：借每秒定时器节流（每 300s 一次），与云端 pricing.json / 本地配置比对，
        # 网络请求在后台线程执行（_sync_pricing(from_thread=True)），不卡界面
        self._pricing_check_counter: int = 0
        self._pricing_changed_flag: bool = False  # _sync_pricing 发现价格变化 → 主线程打日志
        # 打印启动守卫：取号/上报期间禁止再次触发开始打印（防止 processEvents 等待窗口内的重入）
        self._print_starting: bool = False
        # 窗口关闭守卫：打印启动的 processEvents 等待期间窗口可能被关闭，置位后中止后续流程
        self._closing: bool = False

        self.setWindowTitle(f"HN 本地打印工具 v{APP_VERSION}")
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

        # ── 启动清理：上次会话遗留的暂存副本（工作副本） ──
        # 正常退出时已按引用清空；此处兜底覆盖崩溃/强杀遗留（副本较新时留 10 分钟宽限，
        # 避免误删刚复制好、还挂在崩溃前配置里的副本）
        try:
            _n, _bytes = staging.cache_size()
            if _n:
                self._file_logger.info("暂存副本目录：%d 个文件 / %.1f MB", _n, _bytes / 1048576)
            self._sweep_staged_files("启动清理", min_age_seconds=600)
        except Exception as e:
            logger.warning(f"启动清理暂存副本失败: {e}")

        # ── 自更新：启动 4 秒后后台检查更新（不阻塞 UI，发现新版本才弹窗）──
        #    S1-02 改动 4（2026-10-23）：启动检查**只提示**——不自动下载、不生成 update.cmd、
        #    不静默安装。真正的下载/安装只能由用户在「帮助 → 检查更新」后明确点「立即更新」触发
        #    （见 _on_update_check_result 的 manual 分支与 updater.prepare_update(user_confirmed=True)）。
        self._update_checking: bool = False
        self._updateCheckDone.connect(self._on_update_check_result)
        QTimer.singleShot(4000, lambda: self._start_update_check(manual=False))

        # ── 攒批续打：上次退出/崩溃时出纸槽里还留着一叠没打第 2 趟的纸 ──
        # 放到启动末尾（UI/日志就绪）再问，避免弹窗盖住启动日志。
        QTimer.singleShot(1200, self._check_pending_stack_on_startup)
        # 攒批列表**常驻**（2026-10-20 用户要求）：模式开着就自己出现；不抢焦点
        QTimer.singleShot(1500, self._sync_stack_list_visible)

        # ── 双面能力未确认就明说（2026-10-17 事故防呆：不盖章 ⇒ 会走驱动程序自带的
        #    手动双面向导，单页文件可能先吐一张空白页）。同一队列同一状态只提示一次。
        #    _duplex_warned 可能已被构造期 _refresh_printer_list 的切换回调用过，别清空
        #    （否则同一队列会连打两条一模一样的提示）。
        if not hasattr(self, "_duplex_warned"):
            self._duplex_warned = ""
        self._warn_if_duplex_capability_unknown()

    # ---- 攒批：启动续打 ----

    def _check_pending_stack_on_startup(self):
        """启动时扫描批次目录：有"欠第 2 趟"的遗留清单就询问是否续打。"""
        try:
            root = paths.duplex_stack_dir()
            found = duplex_stack.find_pending_batch(root)
        except Exception as e:
            logger.warning(f"扫描攒批批次目录失败: {e}")
            return
        if not found:
            return
        bdir, st = found
        if not st.entries:
            duplex_stack.discard_batch(bdir)
            return
        # 对账口径：M = 第 2 趟要送（= 进纸盒里应有），T = 第 1 趟原始张数，R = 已取走。
        # ⚠ `load_manifest` **会**返回 M==0（全部取走）的清单 —— 那一叠纸已经是成品，
        #   但"谁被取走"只记在清单里，所以必须让操作员看见并确认，不能静默丢弃/当成待续打。
        present = _stack_present_sheet_count(st)
        original = _stack_original_sheet_count(st)
        taken = max(0, original - present)
        # 成品单（第 1 趟正常收尾且计划全被取走）与"异常单"要分开：M==0 时只收成品，
        # 第 1 趟失败/状态未知的单**绝不能**当成品报成功（那是把半成品收钱）。
        finished = [e for e in st.entries if self._entry_is_finished_product(e)]
        remaining = [e for e in st.entries if e not in finished]
        if present <= 0:
            self._log(f"🖐 发现一叠已全部取走的攒批纸叠：{st.count} 单 / 第 1 趟共 {original} 张"
                      f"（其中成品单 {len(finished)} 个）—— 第 2 趟无内容")
            if not finished:
                # 全部不是成品（第 1 趟都没正常收尾）：纸数不明，**必须让操作员明确三选**
                # （旧写法只弹一个"知道了"，随后就自动按失败回报 + 删清单 —— 文案说"人工核对
                #  后决定"、行为却是程序替用户决定了，而且保留选项根本没有）。
                box = QMessageBox(self)
                box.setWindowTitle("上次的攒批只剩异常单")
                box.setIcon(QMessageBox.Warning)
                box.setText(
                    f"磁盘上还有一叠攒批清单（{st.count} 单，第 1 趟共 {original} 张），"
                    f"但每一单的第 1 趟都没有正常收尾，且纸已被标记取走。\n\n"
                    f"无法确认顾客是否真的拿到了纸，请人工核对后再选。\n\n"
                    f"清单：{duplex_stack.manifest_path(bdir)}")
                b_prod = box.addButton("按成品收尾（标记完成、上报成功）", QMessageBox.AcceptRole)
                b_fail = box.addButton("按失败收尾（放弃这几单）", QMessageBox.DestructiveRole)
                b_keep = box.addButton("先保留、稍后处理（不清清单）", QMessageBox.RejectRole)
                box.setDefaultButton(b_keep)
                # 同样是三选长文案：按钮被压扁就剩中间几个字（见 _fit_message_box_buttons）
                _fit_message_box_buttons(box)
                box.exec()
                clicked = box.clickedButton()
                if clicked is b_prod:
                    self._log(f"✅ 人工确认：按成品收尾（{st.count} 单 / 第 1 趟共 {original} 张）")
                    self._finish_stack_entries_offline(
                        st, ok=True,
                        message="第 1 趟即成品（人工核对后确认，纸已被取走）")
                    duplex_stack.mark_keep(bdir, False)
                    duplex_stack.discard_batch(bdir)
                    return
                if clicked is b_fail:
                    self._log(f"⏭ 人工确认：按失败收尾（{st.count} 单）")
                    self._finish_stack_entries_offline(
                        st, ok=False,
                        message="攒批遗留：第 1 趟未正常收尾且纸已被取走，人工判失败")
                    duplex_stack.mark_keep(bdir, False)
                    duplex_stack.discard_batch(bdir)
                    return
                duplex_stack.mark_keep(bdir, True)
                self._log("⏸ 保留这叠（已打「人工保留」标记，后续启动不会再自动清理它）："
                          f"{duplex_stack.manifest_path(bdir)}")
                return
            reply = QMessageBox.question(
                self, "上次的攒批已全部取走",
                f"这一叠纸的第 1 趟共打出 {original} 张，现在全部被标记为【已取走】"
                + (f"（另有 {len(remaining)} 单状态异常）" if remaining else "")
                + f"；{st.count} 个打印任务的第 1 趟：\n\n"
                + "\n".join(st.describe_lines()) + "\n\n"
                f"第 2 趟没有任何内容要打 —— 这些纸在第 1 趟就是成品。\n\n"
                f"「是」= 按成品收尾（这几单标记完成、上报成功、清单删除、不出纸）\n"
                f"「否」= 人工核对后先不动清单（保留下来，稍后再处理）",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                duplex_stack.mark_keep(bdir, True)
                self._log("⏸ 保留这叠已全部取走的清单（已打「人工保留」标记，"
                          "后续启动不会再自动清理它）—— 人工核对后可在攒批队列窗里收尾："
                          f"{duplex_stack.manifest_path(bdir)}")
                return
            self._log(f"✅ 按成品收尾：{len(finished)} 单（第 1 趟即成品）/ {original} 张")
            self._finish_stack_entries_offline(
                st, ok=True, message="第 1 趟即成品（该单的纸已被操作员取走，无需第 2 趟）")
            duplex_stack.mark_keep(bdir, False)
            duplex_stack.discard_batch(bdir)
            return
        self._log(f"🖐 发现未完成的攒批纸叠：{st.count} 单 / 进纸盒应有 {present} 张"
                  f"（第 1 趟共 {original} 张"
                  + (f"，已取走 {taken} 张" if taken else "") + "）"
                  f"（{st.created_at or '时间未知'}）")
        uncertain = any(e.state != duplex_stack.STATE_DONE for e in st.entries) or st.tainted
        detail = "\n".join(st.describe_lines())
        warn = ("\n\n⚠ 清单里有单的第 1 趟没有正常收尾（或进程是被强杀的），"
                "出纸槽里的纸到底有几张无法确定 —— 若页数对不上，"
                "打第 2 趟会整叠错位，请人工核对后选择「否」放弃。" if uncertain else "")
        reply = QMessageBox.question(
            self, "上次的攒批还没打第 2 趟",
            f"进纸盒里应有 {present} 张纸（第 1 趟共 {original} 张"
            + (f" − 已取走 {taken} 张" if taken else "")
            + f"；{st.count} 个打印任务的第 1 趟）：\n\n"
            f"{detail}{warn}\n\n"
            f"「是」= 续打第 2 趟（请先数够这 {present} 张，确认纸盘 2 里放着的就是这一整叠）\n"
            f"「否」= 放弃这一叠（按失败收尾，清单会被删除，纸请人工处理）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            self._log(f"⏭ 放弃上次遗留的攒批纸叠（{st.count} 单 / 在叠 {present} 张）")
            self._finish_stack_entries_offline(st)
            duplex_stack.discard_batch(bdir)
            return
        # 续打：恢复整叠并直接排入第 2 趟（顺手清掉其它遗留批次目录 —— 但"人工保留"的不动）
        duplex_stack.mark_keep(bdir, False)      # 已在处理这一叠了，标记可以撤了
        duplex_stack.keep_only_batch(root, bdir)
        self._stack = st
        # batch_id 必须与磁盘上那个目录名一致：_stack_batch_dir() 靠它拼路径，
        # 不一致就会去一个不存在的目录里找各单 PDF（第 2 趟直接失败）。
        self._stack.batch_id = os.path.basename(bdir)
        self._stack.printer_name = self._config.printer_name
        self._sync_stack_button()
        # 攒批不再复用「手动双面」窗：恢复后把攒批列表叫出来，第 2 趟从列表按钮发起
        dlg = self._ensure_stack_queue_dialog()
        self._refresh_stack_queue()
        self._show_stack_queue_dialog(dlg)
        self._log("🖐 已恢复上次的攒批纸叠，点「▶ 开始打印第 2 趟」继续（清单："
                  f"{duplex_stack.manifest_path(bdir)}）")

    def _finish_stack_entries_offline(self, st, ok: bool = False, message: str = ""):
        """收尾一个**没有活的 job 对象**的遗留纸叠（只有清单快照）。

        逐条按 task_id 回报后端：不报的话订单会一直停在 printing，
        直到后端 3 分钟/5 分钟超时机制判失败 —— 那期间顾客看到的是"正在打印"。

        `ok=False`（默认，= 放弃半成品）：按失败回报，理由沿用旧文案。
        `ok=True`（= 第 1 趟即成品，纸已被操作员取走）：按**成功**回报。
        ⚠ 两个关键闸门（少一个就会把半成品当成品收钱）：
          · `ok=True` 时只认 `_entry_is_finished_product()` 的单 —— 第 1 趟失败/状态未知的
            单一律仍按失败回报；
          · `page_count <= 0` 的一律仍按失败回报（页数未知 = 这单根本没打成、"第 1 趟即成品"
            这个说法就不成立）。
        """
        if not self._cloud_client:
            return
        for entry in st.entries:
            # ⚠ 每单算一次"这单能不能算成品"；下面每个任务按**只读**方式用它，
            #   绝不在任务循环里改写它（改写会串到同一单的下一个任务上）。
            entry_product = bool(ok) and self._entry_is_finished_product(entry)
            for t in (entry.tasks or []):
                tid = int(t.get("task_id") or 0)
                if not tid:
                    continue
                # 页数未知 = 这单根本没打成，"第 1 趟即成品"的说法不成立 → 该任务按失败回报
                task_ok = entry_product and int(t.get("page_count") or 0) > 0
                try:
                    if task_ok:
                        self._cloud_client.report_success(tid, {
                            "copies": int(t.get("copies") or 1),
                            "duplex": t.get("duplex") or "off",
                            "page_range": t.get("page_range") or "",
                            "page_count": int(t.get("page_count") or 0),
                            "is_admin_print": bool(t.get("is_admin_print", False)),
                            **({"owner_name": t.get("owner_name")} if t.get("owner_name") else {}),
                        })
                    else:
                        self._cloud_client.report_fail(
                            tid, message or "攒批纸叠被放弃（只打了第 1 趟，纸是半成品）")
                except Exception as e:
                    logger.warning(f"遗留攒批任务 #{tid} 收尾上报异常: {e}")

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

    def _next_tab_key(self) -> str:
        """下一个可用标签页 key = 现有最大数字 key + 1（新建标签页的唯一取号入口）。

        「在最后一页点 ＋」新建、以及「添加到新标签页」（把文件复制到新标签页）都走这里，
        避免两处各自算号算出同一个 key 把已有标签页顶掉。"""
        tab_keys = self._sorted_tab_keys(self._config.tabs)
        return str((self._safe_int_key(tab_keys[-1]) if tab_keys else 0) + 1)

    # ---- 云端连接 ----

    def _init_cloud_client(self):
        """初始化云打印客户端（根据配置决定是否自动连接）。

        本地订单库（OfflineSync / SQLite）始终初始化——即便未启用云端，本地打印订单一律写入
        本地订单库（供「本地订单统计」读取）。CloudClient 仅在启用云端功能时才创建。
        """
        # 初始化本地订单库（离线/本地模式订单都写这里，供本地订单统计）
        self._offline_sync = OfflineSync()
        # S1-09：启动即刷一次「📴 离线未同步」角标（上次运行遗留的未同步单要立刻看得见）
        self._refresh_offline_badge()

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
        # 接单状态（2026-11）：后端推送 → 本机是否接管/接管者是谁；后台线程 → 主线程 UI
        self._cloud_client.printer_state.connect(self._on_cloud_printer_state)
        # 移动端远程切换「🖐 攒批模式」（2026-10）：云端下发 → 主线程校验忙闲后再执行
        self._cloud_client.stack_mode_requested.connect(self._on_remote_stack_mode)
        # 接单配置：本机是否启用接单（仅接管者可接收订单；连上后自动向后端续接单）
        self._cloud_client.take_orders = bool(self._config.cloud_take_orders)

        # 连线后后台同步的信号 → 回主线程（日志写入 / 归属下拉刷新 / 标签页显示刷新）
        self._cloudConnSyncLog.connect(self._log)
        self._ownerComboRefreshed.connect(self._on_owner_combo_refreshed)
        self._tabDisplayRefresh.connect(self._on_refresh_tab_display_safe)
        self._pricingSynced.connect(self._on_pricing_synced)

        # 初始化云端任务列表窗口（非模态，复用）
        self._cloud_task_window = CloudTaskListWindow(self)
        self._cloud_task_window.order_accepted.connect(self._on_cloud_order_accepted)
        self._cloud_task_window.order_rejected.connect(self._on_cloud_order_rejected)

        # 如果配置了 token 且启用了云端，自动连接
        if self._config.cloud_enabled and self._config.cloud_token:
            self._cloud_client.start()
            self._update_cloud_status()

        # 启动后同步打印价格：云端模式读 /api/pricing，本地模式读收支统计本地配置。
        # 价格统一在「收支统计 → 设置」维护，主界面不再提供价格编辑入口。
        # 启动后后台同步打印价格（避免主线程 8s 网络超时冻结界面，阻塞云端信号处理）
        QTimer.singleShot(800, lambda: threading.Thread(
            target=self._sync_pricing, kwargs={"from_thread": True}, daemon=True).start())

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
        # 2026-11：本机设备信息注入 stats_server，供收支清算「授权」页展示本设备/绑定所有者
        import socket as _socket
        device_name = _socket.gethostname()[:64]
        client_id = self._cloud_client.client_id if self._cloud_client else _get_persistent_client_id()
        take_orders = bool(self._config.cloud_take_orders)
        # 如果已启动则直接打开，否则先启动
        if self._stats_server is None:
            self._stats_server = StatsServer(
                api_url=self._config.cloud_api_url,
                token=self._config.cloud_token,
                finance_mode=finance_mode,
                device_name=device_name,
                client_id=client_id,
                take_orders=take_orders,
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
        if isinstance(tab, str) and tab in ("settings", "auth"):
            url += f"&tab={tab}"
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

        layout.addSpacing(4)

        # 接单开关（2026-12）：多设备可同时启用接单，勾选后本机进入「可接单设备」列表，
        # 移动端提交订单时选择向哪台设备发送
        take_orders_switch = ThemedCheckBox("启用接单（接收云端订单）", theme_manager=self._theme_manager)
        take_orders_switch.setToolTip(
            "勾选后本机进入「可接单设备」列表，移动端提交订单时可选择向本机发送打印任务。\n"
            "多台设备可同时启用接单；未指定目标设备的订单由任一在线接单设备接收。")
        take_orders_switch.setChecked(bool(self._config.cloud_take_orders))
        layout.addWidget(take_orders_switch)
        # 当前接单状态提示（来自后端 printer_state 推送缓存；后台刷新见下方线程）
        claim_hint = QLabel("")
        claim_hint.setWordWrap(True)
        claim_hint.setStyleSheet("color: #8a8f98; font-size: 12px;")
        layout.addWidget(claim_hint)

        layout.addSpacing(4)

        # API 地址
        layout.addWidget(QLabel("API 地址："))
        api_input = QLineEdit(self._config.cloud_api_url)
        api_input.setPlaceholderText("https://your-server.com")
        layout.addWidget(api_input)

        # WebSocket 地址
        layout.addWidget(QLabel("WebSocket 地址："))
        ws_input = QLineEdit(self._config.cloud_ws_url)
        ws_input.setPlaceholderText("wss://your-server.com")
        layout.addWidget(ws_input)

        # Token
        layout.addWidget(QLabel("认证 Token："))
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

        # 打开时填充当前接单状态提示（缓存 + 后台刷新一次，不阻塞对话框）
        def _fill_claim_hint():
            st = getattr(self, "_cloud_printer_state_cache", {}) or {}
            if st.get("is_active"):
                claim_hint.setText("✅ 本机已启用接单（可接收云端订单）")
            else:
                cnt = st.get("claiming_count", 0)
                if cnt > 0:
                    claim_hint.setText(f"当前有 {cnt} 台设备启用接单；本机未启用")
                else:
                    claim_hint.setText("当前无设备启用接单")

        _fill_claim_hint()

        if self._cloud_client and self._cloud_client.is_connected():
            def _refresh_claim_hint():
                try:
                    data = self._cloud_client.get_printer_devices()
                    if not data:
                        return
                    claiming_count = sum(1 for d in data.get("devices", []) if d.get("is_active"))
                    self._cloud_printer_state_cache = {
                        "claiming_count": claiming_count,
                        "is_active": bool(self._cloud_client.take_orders),
                    }
                    QTimer.singleShot(0, _fill_claim_hint)
                except Exception:
                    pass
            threading.Thread(target=_refresh_claim_hint, daemon=True).start()

        if dlg.exec() == QDialog.Accepted:
            # 保存配置到内存（云端开关以对话中的勾选为准）
            old_enabled = bool(self._config.cloud_enabled)
            self._config.cloud_api_url = api_input.text().strip()
            self._config.cloud_ws_url = ws_input.text().strip()
            self._config.cloud_token = token_input.text().strip()
            self._config.cloud_enabled = bool(cloud_switch.isChecked())

            # 立刻写入磁盘，防止程序崩溃丢失
            try:
                self._save_config(sync_ui=False)
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
                            self._save_config(sync_ui=False)
                        except Exception:
                            pass
                        self._log("❌ 已取消「关闭云端」：云端保持开启，配置已回滚，未做任何改动")
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

            # D5 修复：后台离线同步线程的目标也要跟着换 —— 旧写法把 server_url/token 焊进
            # 闭包、且「已在运行」直接 return ⇒ 用户在这里改完地址/token 后，本次运行内后台
            # 定时同步仍往**旧地址**上报。云端关闭时停掉线程，否则残留的旧 token 会把本地订单
            # 静默上传到旧云端（与 _init_cloud_client 里「本地模式不启动后台同步」同源）。
            if self._offline_sync:
                if self._config.cloud_enabled and self._config.cloud_token:
                    self._offline_sync.start_background_sync(
                        server_url=self._config.cloud_api_url,
                        token=self._config.cloud_token,
                        interval=60,
                    )
                else:
                    self._offline_sync.stop_background_sync()

            # 接单开关同步后端（多设备可同时启用，2026-12）：勾选 → 加入可接单设备列表
            new_take = bool(take_orders_switch.isChecked()) and bool(cloud_switch.isChecked())
            if self._config.cloud_enabled and self._config.cloud_token and self._cloud_client:
                if new_take and not self._config.cloud_take_orders:
                    ok, msg, _extra = self._cloud_client.claim_printer()
                    if not ok:
                        from PySide6.QtWidgets import QMessageBox
                        QMessageBox.warning(self, "无法启用接单", msg)
                        new_take = False
                elif not new_take and self._config.cloud_take_orders:
                    self._cloud_client.release_printer()
            if self._config.cloud_take_orders != new_take:
                self._config.cloud_take_orders = new_take
                try:
                    self._save_config(sync_ui=False)
                except Exception as e:
                    logger.warning(f"保存接单配置失败: {e}")
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
            # 不重启：配置已保存，下次启动生效。⚠ 必须说清"现在"是什么状态 —— 调用方在调
            # 本函数之前已经 `_cloud_client.stop()`，所以重启前本进程**不会**再连云端。
            # 旧文案只写「已保存，下次启动时生效（启用云端）」，读起来像"现在已经在连"。
            self._log(f"🔁 云端开关已保存：下次启动时「{state_txt}」生效。"
                      f"本次不重启 —— 重启前本进程不会连接云端（云端订单不会进来）")

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

    # ── S1-09：离线未同步订单（状态栏角标 / 明细窗 / 手动重试）──

    def _refresh_offline_badge(self):
        """刷新状态栏「📴 离线未同步 N」角标（**本地 SQLite 计数，不走网络**）。"""
        btn = getattr(self, "_offline_badge", None)
        if btn is None:
            return
        sync = getattr(self, "_offline_sync", None)
        if sync is None:
            btn.setVisible(False)
            return
        try:
            summary = sync.status_summary()
        except Exception as e:
            logger.debug(f"刷新离线角标失败: {e}")
            btn.setVisible(False)
            return
        n = int(summary.get("unsynced", 0) or 0)
        rejected = int(summary.get("rejected", 0) or 0)
        if n <= 0:
            btn.setVisible(False)
            btn.setText("")
            return
        text = f"📴 离线未同步 {n}"
        if rejected:
            text += f"（{rejected} 条拒收）"
        btn.setText(text)
        btn.setVisible(True)

    def _open_offline_orders_dialog(self):
        """打开「离线未同步订单」窗口（行来自本地离线库 synced=0）。"""
        dlg = getattr(self, "_offline_dialog", None)
        if dlg is None:
            dlg = OfflineOrdersDialog(self)
            dlg.retry_requested.connect(self._retry_offline_orders)
            self._offline_dialog = dlg
        dlg.set_offline_sync(getattr(self, "_offline_sync", None))
        dlg.reload()
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _reload_offline_dialog(self):
        dlg = getattr(self, "_offline_dialog", None)
        if dlg is not None and dlg.isVisible():
            dlg.reload()

    def _retry_offline_orders(self, order_number: str = ""):
        """手动重试离线未同步订单：清掉退避/拒收标记 → 立即同步一轮（网络在后台线程）。

        ⚠ 只做"立刻再试一次"，不改任何上报口径：成功后该行 synced=1，云端照常入账；
        失败则保持「⏳ 离线未同步」并继续按指数退避自动重试（永不放弃）。
        """
        sync = getattr(self, "_offline_sync", None)
        client = getattr(self, "_cloud_client", None)
        if sync is None:
            return
        api_url = getattr(client, "api_url", "") if client else ""
        token = getattr(client, "token", "") if client else ""
        if not api_url or not token:
            self._log("⚠️ 云端未配置/未连接：离线订单暂时无法重试（恢复连接后会自动补报）")
            return
        try:
            n = sync.retry_now(order_number)
        except Exception as e:
            self._log(f"⚠️ 重置离线订单重试状态失败: {e}")
            return
        scope = order_number or "全部"
        self._log(f"🔄 手动重试离线未同步订单（{scope}）：重置 {n} 条，正在上传…")

        def _net():
            try:
                ok = sync.sync_all_pending_orders(server_url=api_url, token=token)
                self._cloudConnSyncLog.emit(f"📋 离线订单手动重试：本次上传成功 {ok} 条")
            except Exception as e:
                self._cloudConnSyncLog.emit(f"⚠️ 离线订单手动重试失败: {e}")

        threading.Thread(target=_net, daemon=True).start()
        # 主线程稍后刷新角标/明细（网络结果可能还没回来，角标会在 60s 定时器里再对齐）
        QTimer.singleShot(2500, self._after_offline_retry)

    def _after_offline_retry(self):
        self._refresh_offline_badge()
        self._reload_offline_dialog()

    def _wipe_cloud_cached_data(self):
        """关闭云端时清空本机云端缓存数据：
        成员名单、收支数据文件、openid 绑定、本地订单库、标签页归属与订单号。
        机器配置（打印机/价格/派送地点）与文件队列保留。"""
        logger.info("开始清空云端缓存数据（关闭云端功能）")
        # 1. 成员名单（归属下拉回到「(无成员)」）
        self._config.admin_names = []
        # 1.5 本设备所有者缓存清空（转本地模式后不再使用云端绑定）
        self._config.cloud_owner_name = ""
        # 2. 标签页归属与订单号（保留文件队列与打印设置；订单号下次打印重新分配）
        for tab in self._config.tabs.values():
            tab.owner_name = ""
            for job in tab.jobs:
                job.order_number = ""
        try:
            self._save_config(sync_ui=False)
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
        """重启当前应用（自动重新装配 UI）。

        用看门狗 cmd：等本进程完全退出（释放单实例互斥体）后再启动新实例，
        避免原 Popen+quit 的时序竞争——新实例启动时旧进程尚未退出，
        会被 single_instance.acquire 误判为"已在运行"而退出，
        表现为"云端开关确认后只关闭、不自动启动"。"""
        import sys as _sys
        import tempfile
        try:
            self._save_config()
            if self._stats_server:
                self._stats_server.stop()
            if self._cloud_client:
                self._cloud_client.stop()
            pid = os.getpid()
            python = _sys.executable
            args = [_sys.argv[0]] + list(_sys.argv[1:])
            launch = '"{}"'.format(python) + "".join(
                ' "{}"'.format(str(a).replace('"', '\\"')) for a in args)
            cmd_path = os.path.join(tempfile.gettempdir(), "hn_restart.cmd")
            lines = [
                "@echo off",
                "set n=0",
                ":wait",
                'tasklist /FI "PID eq {pid}" | findstr "{pid}" >nul'.format(pid=pid),
                "if errorlevel 1 goto start",
                "set /a n+=1",
                "if %n% gtr 120 goto start",
                "ping -n 2 127.0.0.1 >nul",
                "goto wait",
                ":start",
                'start "" {launch}'.format(launch=launch),
                "ping -n 2 127.0.0.1 >nul",
                'del "%~f0"',
            ]
            with open(cmd_path, "w", encoding="gbk", errors="replace") as f:
                f.write("\r\n".join(lines) + "\r\n")
            # 最小化窗口运行看门狗（SW_SHOWMINNOACTIVE=7），当前实例随后退出
            import ctypes
            ctypes.windll.shell32.ShellExecuteW(None, "open", cmd_path, None, None, 7)
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
                if self._cloud_client and self._cloud_client.take_orders:
                    # 接单中（2026-12）：本机已启用接单，可接收云端订单
                    self._cloud_status_indicator.setText("☁ 接单中")
                else:
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

        # S1-09：离线未同步订单角标（有未同步单才出现）。旧行为是"未同步单只在日志里
        # 一句 📋 离线模式：任务已缓存"，出纸后没人知道这张单其实还没上云 —— 结算页上
        # 也就没有它。这里给一个常驻读数 + 点开手动重试的入口。
        self._offline_badge = QPushButton("")
        self._offline_badge.setObjectName("offlineBadge")
        self._offline_badge.setToolTip("本机还有订单**没有上报到云端**（离线未同步）。\n"
                                      "点开可查看明细并手动重试上传。")
        self._offline_badge.setVisible(False)
        self._offline_badge.clicked.connect(self._open_offline_orders_dialog)
        self._status_bar.addPermanentWidget(self._offline_badge)
        self._offline_badge_timer = QTimer(self)
        self._offline_badge_timer.setInterval(60000)   # 本地 SQLite 计数，不走网络
        self._offline_badge_timer.timeout.connect(self._refresh_offline_badge)
        self._offline_badge_timer.start()

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

        # S1-09：离线未同步订单（本地库里 synced=0 的单，云端还没有）——手动重试入口
        offline_action = QAction("📴 离线未同步订单(&O)…", self)
        offline_action.setToolTip("查看本机还没上报到云端的订单，并可手动重试上传")
        offline_action.triggered.connect(self._open_offline_orders_dialog)
        file_menu.addAction(offline_action)

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
        # 2026-10-20 用户要求：把「启动/预热」「每张纸」「翻纸放行比例」「人工翻页准备时间」
        # 和原来的两点测速**全部塞进同一个窗口**，入口改名「打印速度…」。
        speed_action = QAction("打印速度…", self)
        speed_action.setToolTip("第 1 趟耗时模型（启动/预热、每张纸）+ 翻纸放行策略"
                                "（按预估百分比放行、人工翻页准备时间）+ 一键测速")
        speed_action.triggered.connect(self._on_calibrate_speed)
        help_menu.addAction(speed_action)
        duplex_cap_action = QAction("打印机双面能力…", self)
        duplex_cap_action.setToolTip("给当前打印队列盖「支持/不支持自动双面」的章："
                                     "不支持则改用程序自己的两趟控制器（人工翻纸），"
                                     "不再走驱动程序自带的「双面打印说明」向导")
        duplex_cap_action.triggered.connect(self._on_printer_duplex_capability)
        help_menu.addAction(duplex_cap_action)
        help_menu.addSeparator()
        self._update_action = QAction("检查更新(&U)", self)
        self._update_action.triggered.connect(lambda: self._start_update_check(manual=True))
        help_menu.addAction(self._update_action)
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

    # ---- 自更新（检查 / 下载 / 触发静默安装）----

    def _set_update_menu_state(self, busy: bool, label: str = ""):
        """更新相关菜单项状态：检查/下载期间禁用，防重复触发。"""
        action = getattr(self, '_update_action', None)
        if action is None:
            return
        action.setEnabled(not busy)
        action.setText(label if busy else "检查更新(&U)")

    def _start_update_check(self, manual: bool = False):
        """启动检查更新（后台线程，结果经 _updateCheckDone 信号回主线程）。"""
        if self._update_checking:
            return
        self._update_checking = True
        self._set_update_menu_state(True, "检查更新中...")
        if manual:
            self._log("☁ 正在检查更新...")
        threading.Thread(target=self._update_check_worker, args=(manual,), daemon=True).start()

    def _update_check_worker(self, manual: bool):
        try:
            from updater import (fetch_update_info, compare_versions, VersionFormatError,
                                 last_reject_reason)
            manifest = fetch_update_info()
            if not manifest:
                # 区分「网络/服务器问题」与「清单被安全校验拒绝」（host 白名单/哈希必填/验签）：
                # 后者必须让操作员看见原因，不能混成"检查更新失败，请检查网络"。
                reject = last_reject_reason()
                if reject:
                    self._updateCheckDone.emit(("rejected", reject, manual))
                elif manual:
                    self._updateCheckDone.emit(("latest", None, manual))
                else:
                    self._updateCheckDone.emit(("none", None, manual))
                return
            try:
                newer = compare_versions(APP_VERSION, manifest.get("version", ""))
            except VersionFormatError as e:
                # 版本号含非数字段（如 "4.5.13a"）：显式报错，绝不静默当作"无更新"
                self._updateCheckDone.emit(("manifest_error", str(e), manual))
                return
            if newer:
                self._updateCheckDone.emit(("available", manifest, manual))
            elif manual:
                self._updateCheckDone.emit(("latest", None, manual))
            else:
                self._updateCheckDone.emit(("none", None, manual))
        except Exception:
            self._updateCheckDone.emit(("error", None, manual))

    def _on_update_check_result(self, result):
        kind, manifest, manual = result
        self._update_checking = False
        if kind in ("ready", "download_fail"):
            self._on_update_ready(result)
            return
        # 检查结束（无更新/有更新待确认）→ 恢复菜单项
        self._set_update_menu_state(False)
        if kind == "available":
            version = manifest.get("version", "")
            notes = (manifest.get("notes") or "").strip()
            msg = f"发现新版本 v{version}（当前 v{APP_VERSION}）"
            if notes:
                msg += f"\n\n更新说明：\n{notes}"
            if manual:
                # S1-02 改动 4（2026-10-23）：只有**这一次明确点击**才允许进入下载 → 生成
                # update.cmd（/VERYSILENT 静默安装）。默认按钮是「稍后」：回车/直接关窗
                # 都等于不更新，绝不默认「立即更新」。
                msg += "\n\n是否立即下载并更新？（更新过程需退出程序，配置数据不会丢失）"
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Question)
                box.setWindowTitle("发现新版本")
                box.setText(msg)
                btn_now = box.addButton("立即下载并更新", QMessageBox.AcceptRole)
                btn_later = box.addButton("稍后", QMessageBox.RejectRole)
                box.setDefaultButton(btn_later)
                box.exec()
                if box.clickedButton() is btn_now:
                    self._perform_update(manifest)
            else:
                # 启动检查（manual=False）：**只提示**，不下载、不写 update.cmd、不安装。
                # 用户此刻多半正要开工，自动装会打断打印（旧行为：启动 4 秒弹「是否立即更新」，
                # 默认按钮还是「是」）。这里只给一个「稍后」，把决定权交回菜单。
                msg += ("\n\n为避免打断正在进行的打印，启动时不会自动更新。\n"
                        "需要升级时请点「帮助 → 检查更新」手动下载安装。")
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Information)
                box.setWindowTitle("发现新版本")
                box.setText(msg)
                btn_later = box.addButton("稍后", QMessageBox.RejectRole)
                box.setDefaultButton(btn_later)
                box.exec()
        elif kind == "rejected":
            # 清单没通过安全校验（host 白名单 / 哈希必填 / ed25519 验签）→ 绝不降级更新。
            detail = manifest if isinstance(manifest, str) else str(manifest)
            self._log(f"⚠ 更新清单未通过安全校验，已拒绝本次更新：{detail}")
            if manual:
                QMessageBox.warning(
                    self, "检查更新",
                    f"更新清单未通过安全校验，已拒绝本次更新：\n{detail}\n\n"
                    "（可能是清单被篡改，或发布流程未按新规签名；请联系发布者核对。）")
        elif kind == "latest" and manual:
            QMessageBox.information(self, "检查更新", f"当前已是最新版本 v{APP_VERSION}")
        elif kind == "manifest_error":
            # 版本号含非数字段 → 显式报错。旧实现会把非数字段归零，导致此类版本
            # 永远判定为"不更新"，发布者无从察觉（静默失效）。
            detail = manifest if isinstance(manifest, str) else str(manifest)
            self._log(f"⚠ 更新清单版本号格式非法，已跳过本次更新：{detail}")
            if manual:
                QMessageBox.warning(
                    self, "检查更新",
                    f"更新清单版本号格式非法，已拒绝本次更新：\n{detail}\n\n"
                    "版本号只允许纯数字点分段（如 4.5.14），请联系发布者修正。")
        elif kind == "error" and manual:
            QMessageBox.warning(self, "检查更新", "检查更新失败，请检查网络连接后重试")

    def _perform_update(self, manifest: dict):
        """用户明确点击「立即更新」后才走到这里：检查打印状态 → 下载进度窗口 → 后台下载
        → 生成 update.cmd（/VERYSILENT）→ 退出主程序。

        S1-02 改动 4（2026-10-23）：本方法是**唯一**能让更新进入"下载 + 静默安装"的入口，
        只由 _on_update_check_result 的 manual 分支在用户点「立即下载并更新」后调用；
        启动自动检查不会、也不能调用它。"""
        if self._worker and self._worker.isRunning():
            reply = QMessageBox.question(
                self, "正在打印",
                "当前有打印任务正在进行。\n更新需要退出程序，建议先完成打印。\n\n仍要立即更新吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
        self._log(f"☁ 开始下载更新安装包 v{manifest.get('version', '')}...")
        self._set_update_menu_state(True, "更新下载中...")
        # 下载进度弹窗（可取消）
        from PySide6.QtWidgets import QProgressDialog
        dlg = QProgressDialog("正在连接服务器...", "取消", 0, 100, self)
        dlg.setWindowTitle("正在下载更新")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setMinimumWidth(360)
        dlg.show()
        self._update_dlg = dlg
        self._update_cancelled = False
        self._updateProgress.connect(self._on_update_progress)
        dlg.canceled.connect(self._on_update_download_cancel)
        threading.Thread(target=self._update_download_worker, args=(manifest,), daemon=True).start()

    def _on_update_progress(self, downloaded: int, total: int):
        """主线程：更新下载进度弹窗。"""
        dlg = getattr(self, '_update_dlg', None)
        if not dlg:
            return
        if total > 0:
            dlg.setMaximum(total)
            dlg.setValue(downloaded)
            dlg.setLabelText(
                f"正在下载更新… {downloaded / 1048576:.1f} / {total / 1048576:.1f} MB")
        else:
            dlg.setMaximum(0)  # 未知总大小 → 忙碌态

    def _on_update_download_cancel(self):
        """用户取消下载 → 通知后台线程中止（回调返回 False 抛 UpdateCancelled）。"""
        self._update_cancelled = True
        self._log("☁ 已取消更新下载")

    def _update_download_worker(self, manifest: dict):
        """后台：下载安装包（带进度回调）→ MD5 校验 → 写 update.cmd → 最小化运行。"""
        try:
            from updater import prepare_update, write_update_cmd, _run_cmd_minimized
            import sys as _sys

            def _progress(downloaded: int, total: int):
                # 回调在下载线程：经信号回主线程更新弹窗；返回 False 表示用户取消
                self._updateProgress.emit(int(downloaded), int(total))
                return not getattr(self, '_update_cancelled', False)

            # S1-02（2026-10-23）：user_confirmed=True —— 本函数只由 _perform_update（用户
            # 明确点击「立即下载并更新」）启动；updater 会再次校验清单（host 白名单 + 哈希必填
            # + ed25519 验签），验签失败在这里就会返回 None，一步安装包都不会下载。
            result = prepare_update(manifest, APP_VERSION, progress_cb=_progress,
                                    user_confirmed=True)
            if not result:
                self._updateCheckDone.emit(("download_fail", None, False))
                return
            setup_path, exe_path = result
            cmd_path = os.path.join(os.path.dirname(setup_path), "update.cmd")
            # 启动新程序的完整命令 + 显式安装目录：
            # - 打包版：/DIR 指向当前程序目录（保证更新永远装到当前安装位置，
            #   无论发布者是否修改默认安装路径，已装用户都能原地升级）；启动即当前 exe
            # - 源码版：不装安装包到当前目录，保持安装包默认位置；启动用 cd /d + 相对文件名
            if getattr(_sys, "frozen", False):
                install_dir = os.path.dirname(exe_path)
                start_cmd = f'start "" "{exe_path}"'
            else:
                install_dir = ""
                script_dir = os.path.dirname(os.path.abspath(_sys.argv[0]))
                argv0 = os.path.basename(_sys.argv[0]) or "main.py"
                start_cmd = (
                    f'cd /d "{script_dir}" & start "" "{exe_path}" "{argv0}"')
            if not write_update_cmd(setup_path, exe_path, cmd_path,
                                    start_cmd=start_cmd, install_dir=install_dir,
                                    allow_silent_install=True):
                self._updateCheckDone.emit(("download_fail", None, False))
                return
            if not _run_cmd_minimized(cmd_path):
                self._updateCheckDone.emit(("download_fail", None, False))
                return
            self._updateCheckDone.emit(("ready", manifest, False))
        except Exception as e:
            logger.error(f"更新准备失败: {e}")
            self._updateCheckDone.emit(("download_fail", None, False))

    def _on_update_ready(self, result):
        kind, manifest, _manual = result
        # 恢复更新菜单项（下载/安装流程结束）
        self._set_update_menu_state(False)
        # 关闭下载进度弹窗
        dlg = getattr(self, '_update_dlg', None)
        if dlg is not None:
            dlg.close()
            self._update_dlg = None
        if kind == "ready":
            self._log(f"☁ 更新安装包已就绪 v{manifest.get('version', '')}，即将退出并自动安装...")
            self.close()  # update.cmd 轮询到进程退出后执行静默安装并启动新版本
        elif kind == "download_fail":
            if getattr(self, '_update_cancelled', False):
                QMessageBox.information(self, "更新已取消", "更新下载已取消。")
            else:
                QMessageBox.warning(self, "更新失败", "下载或准备更新安装包失败，请稍后重试。")

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

        layout.addWidget(QLabel("打印机："))

        self._printer_combo = QComboBox()
        self._printer_combo.setMinimumWidth(200)
        _disable_combo_wheel(self._printer_combo)
        # 换选打印机 → 攒批入口的显隐/生效性随能力立刻变（2026-10：自动双面的机器不显示攒批）
        self._printer_combo.currentIndexChanged.connect(self._on_printer_selection_changed)
        layout.addWidget(self._printer_combo, 1)

        btn_refresh = QPushButton("🔄")
        btn_refresh.setObjectName("btnRefreshPrinters")
        btn_refresh.setToolTip("刷新打印机列表")
        btn_refresh.setFixedSize(36, 36)
        btn_refresh.clicked.connect(self._on_refresh_printers)
        layout.addWidget(btn_refresh)

        layout.addWidget(QLabel("保存转换副本到桌面："))

        self._keep_temp_check = QComboBox()
        self._keep_temp_check.addItems(["否", "是"])
        self._keep_temp_check.setCurrentIndex(0)
        self._keep_temp_check.currentIndexChanged.connect(self._on_keep_temp_changed)
        _disable_combo_wheel(self._keep_temp_check)
        layout.addWidget(self._keep_temp_check)

        layout.addWidget(QLabel("  单面："))

        self._simplex_price_label = QLabel(f"{self._config.simplex_price:.2f} 元/张")
        self._simplex_price_label.setObjectName("readonlyPrice")
        layout.addWidget(self._simplex_price_label)

        layout.addWidget(QLabel("  双面："))

        self._duplex_price_label = QLabel(f"{self._config.duplex_price:.2f} 元/张")
        self._duplex_price_label.setObjectName("readonlyPrice")
        layout.addWidget(self._duplex_price_label)

        layout.addWidget(QLabel(" DPI："))

        self._render_dpi_combo = QComboBox()
        self._render_dpi_combo.addItems(["自动(设备原生)", "高速(200)", "标清(300)", "清晰(400)", "高清(600)"])
        _disable_combo_wheel(self._render_dpi_combo)
        self._render_dpi_combo.setToolTip(
            "自动(设备原生)：按打印机最终像素 1:1 渲染"
            "（最清晰、推荐，且不再吃 100MB/页内存）；"
            "其它档位是渲染分辨率上限，低于设备分辨率时会被放大一次（换速度）")
        self._render_dpi_combo.currentIndexChanged.connect(self._on_render_dpi_changed)
        layout.addWidget(self._render_dpi_combo)

        root.addLayout(layout)

        # ---- 第二行：标签页附加服务 ----
        row2 = QHBoxLayout()
        row2.setSpacing(6)

        # 标签页指示器：[状态徽标][标签页 N]
        # 口径与标签页选择器同源（见文件顶部 TAB_* 常量）：
        #   · 徽标是**固定宽度槽位**（📑 正常 / 🔒 已固定 / ⏸ 待人工翻纸）；
        #   · 文字标签**固定宽度**且只含「标签页 N」—— 数字位数变化（1→12）也不撑宽。
        # 旧写法把状态和说明都塞进这一个标签（`🔒 标签页 1（含双面：需人工翻纸）`）：
        # 文字一长，右边的 派送/地点/优先级/首页 整排被推走（与加减按钮同一类隐患）。
        # ⏸ 的完整说明改由 tooltip 承担（挂起那一刻的日志里也有原文，见 _hold_for_manual_duplex）。
        self._tab_scope_badge = QLabel("📑")
        self._tab_scope_badge.setObjectName("tabScopeBadge")
        self._tab_scope_badge.setAlignment(Qt.AlignCenter)
        self._tab_scope_badge.setFixedWidth(TAB_BADGE_WIDTH)

        self._tab_scope_label = QLabel(f"标签页 {self._current_tab}")
        self._tab_scope_label.setObjectName("tabScopeLabel")
        self._tab_scope_label.setFixedWidth(TAB_SCOPE_WIDTH)

        scope_row = QHBoxLayout()
        scope_row.setContentsMargins(0, 0, 0, 0)
        scope_row.setSpacing(2)
        scope_row.addWidget(self._tab_scope_badge)
        scope_row.addWidget(self._tab_scope_label)
        row2.addLayout(scope_row)
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

        # 派送百分比（只读，价格统一在「收支统计 → 设置」维护）
        self._delivery_percent_label = QLabel("0%")
        self._delivery_percent_label.setObjectName("readonlyPrice")
        self._delivery_percent_label.setFixedWidth(44)

        # 优先程度（档位按业务顺序 低→中→高）
        self._urgency_combo = QComboBox()
        self._urgency_combo.addItems(_sort_urgency_keys(self._config.urgency_prices.keys()))
        _disable_combo_wheel(self._urgency_combo)
        self._urgency_combo.currentIndexChanged.connect(self._on_urgency_changed)

        # 优先级价格（只读）
        self._urgency_price_label = QLabel("0.00 元")
        self._urgency_price_label.setObjectName("readonlyPrice")

        # 首页开关
        self._cover_page_onoff_combo = QComboBox()
        self._cover_page_onoff_combo.addItems(["否", "是"])
        _disable_combo_wheel(self._cover_page_onoff_combo)
        self._cover_page_onoff_combo.currentIndexChanged.connect(self._on_cover_page_toggled)

        # 首页价格（只读）
        self._cover_page_price_label = QLabel(f"{self._config.cover_page_price:.2f} 元")
        self._cover_page_price_label.setObjectName("readonlyPrice")

        # —— 派送/地点居左 + 优先级居右 + 首页居右 ——

        # Group 1: 派送（居左，最小宽度）
        g1 = QHBoxLayout()
        g1.setSpacing(2)
        g1.addWidget(QLabel("派送："))
        g1.addWidget(self._delivery_onoff_combo)
        row2.addLayout(g1)

        row2.addSpacing(12)

        # Group 2: 地点（居左，平分剩余空间）
        g2 = QHBoxLayout()
        g2.setSpacing(2)
        g2.addWidget(QLabel("地点："))
        g2.addWidget(self._delivery_location_combo)
        g2.addSpacing(8)   # 数字与下拉框隔开一个字符
        g2.addWidget(self._delivery_percent_label)
        g2.addStretch()
        row2.addLayout(g2, 1)

        # Group 3: 优先级（居右，平分剩余空间）
        g3 = QHBoxLayout()
        g3.setSpacing(2)
        g3.addStretch()
        g3.addWidget(QLabel("优先级："))
        g3.addWidget(self._urgency_combo)
        g3.addSpacing(8)   # 数字与下拉框隔开一个字符
        g3.addWidget(self._urgency_price_label)
        row2.addLayout(g3, 1)

        row2.addSpacing(12)

        # Group 4: 首页（居右，最小宽度）
        g4 = QHBoxLayout()
        g4.setSpacing(2)
        g4.addStretch()
        g4.addWidget(QLabel("首页："))
        g4.addWidget(self._cover_page_onoff_combo)
        g4.addSpacing(8)   # 数字与下拉框隔开一个字符
        g4.addWidget(self._cover_page_price_label)
        row2.addLayout(g4)

        # 初始状态：派送=否时禁用地点
        self._delivery_location_combo.setEnabled(False)

        root.addLayout(row2)

        # 统一输入框高度为下拉框高度（延迟到布局完成后测量实际高度）
        def _normalize_spin_heights():
            combo_h = self._printer_combo.height()
            if combo_h > 0:
                for sp in (self._render_dpi_combo, self._delivery_onoff_combo,
                           self._urgency_combo, self._cover_page_onoff_combo):
                    sp.setFixedHeight(combo_h)
        QTimer.singleShot(0, _normalize_spin_heights)

    def _setup_file_table(self) -> QWidget:
        """左侧：标签页选择器 + 文件列表表格。"""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # ── 标签页选择器：[-] [N] [+] [状态徽标] ──
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(0, 0, 0, 0)
        tab_row.setSpacing(2)

        # 尺寸口径见文件顶部 TAB_* 常量：按钮/数字/徽标三者宽度恒定，
        # 徽标槽位**永远**是布局里的一员 ⇒ 状态出现/消失都不挪动 −/＋。
        self._tab_btn_minus = QPushButton("−")
        self._tab_btn_minus.setObjectName("tabBtnMinus")
        self._tab_btn_minus.setFixedSize(TAB_SWITCH_BTN, TAB_SWITCH_BTN)
        self._tab_btn_minus.setToolTip("上一个标签页")
        self._tab_btn_minus.clicked.connect(lambda: self._switch_tab(-1))

        self._tab_label = QLabel(self._current_tab)
        self._tab_label.setObjectName("tabLabel")
        self._tab_label.setAlignment(Qt.AlignCenter)
        self._tab_label.setFixedSize(TAB_NUM_WIDTH, TAB_SWITCH_BTN)
        self._tab_label.setCursor(Qt.PointingHandCursor)
        self._tab_label.setToolTip("点击管理标签页")
        font_tab = QFont(self.font()); font_tab.setPointSize(12); font_tab.setBold(True)
        self._tab_label.setFont(font_tab)
        # 点击标签数字 → 弹出标签页管理窗口
        self._tab_label.mousePressEvent = lambda e: self._show_tab_manager()

        self._tab_btn_plus = QPushButton("+")
        self._tab_btn_plus.setObjectName("tabBtnPlus")
        self._tab_btn_plus.setFixedSize(TAB_SWITCH_BTN, TAB_SWITCH_BTN)
        self._tab_btn_plus.setToolTip("下一个标签页 / 新建标签页")
        self._tab_btn_plus.clicked.connect(lambda: self._switch_tab(1))

        # 状态徽标：🔒 标签页已固定（打印完成）/ ⏸ 含双面、需人工翻纸 / 空 = 正常。
        # 放在 ＋ 按钮**之后**：加减数字是一组（−1＋），状态是这一组的旁注；
        # 徽标只占固定槽位、无状态时留空 —— 它的位置在 −/＋ 之后，无论显示什么都不会
        # 改变 −/＋ 的坐标（这也是它不放进数字标签里的原因）。
        self._tab_state_badge = QLabel("")
        self._tab_state_badge.setObjectName("tabStateBadge")
        self._tab_state_badge.setAlignment(Qt.AlignCenter)
        self._tab_state_badge.setFixedSize(TAB_BADGE_WIDTH, TAB_SWITCH_BTN)
        font_badge = QFont(self.font()); font_badge.setPointSize(12)
        self._tab_state_badge.setFont(font_badge)
        self._tab_state_badge.setToolTip("")

        self._tab_btn_cleanup_empty = QPushButton("🗑 空")
        self._tab_btn_cleanup_empty.setObjectName("tabBtnCleanupEmpty")
        self._tab_btn_cleanup_empty.setFixedHeight(TAB_SWITCH_BTN)
        self._tab_btn_cleanup_empty.setToolTip("删除所有空标签页（不含当前）")
        self._tab_btn_cleanup_empty.clicked.connect(self._on_cleanup_empty_tabs)

        tab_row.addWidget(self._tab_btn_minus)
        tab_row.addWidget(self._tab_label)
        tab_row.addWidget(self._tab_btn_plus)
        tab_row.addWidget(self._tab_state_badge)
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
        # 多选（2026-10）：Ctrl/Shift + 左键、以及鼠标拖框（rubber band）都能选多行；
        # 右键菜单的批量操作（移除选中 / 添加到新标签页）对**整片选区**生效。
        # Qt 原生行选择 + 拖框在 ExtendedSelection 下即可用，无需自绘选择框。
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # 关闭「拖出整行」的内部拖动：dragEnabled=True 时从行内按下拖动会被 Qt 判成
        # 内部拖动（拖出行/光标变禁止）而**不是**拖框选择 —— 手势冲突，拖框会失效。
        # 资源管理器拖进来的文件投放不受影响（DropTableWidget 自己处理 dragEnter/drop）。
        self._table.setDragEnabled(False)
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
        self._total_label = QLabel("合计：¥0.00")
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
            self._config.tabs = {"1": self._make_new_tab()}
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
            new_key = self._next_tab_key()
            self._config.tabs[new_key] = self._make_new_tab()
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
        # P1（审计 2026-12）：自动打印重试队列的 tab_key 也必须同步重映射。
        # 原实现漏了这一处 —— 队列里残留旧 key，标签页删除/重编号后旧 key 可能
        # 恰好对上**另一个**标签页，补打时会把别的订单的文件打出来（计费错误）。
        self._remap_auto_print_retry(key_map)

    def _remap_auto_print_retry(self, key_map: dict) -> None:
        """标签页重编号后同步重映射 `_auto_print_retry` 队列中的 tab_key。

        映射不到的（标签页已被删除）置空，补打时按 order_id 反查标签页兜底，
        **绝不保留旧 key 去撞别的标签页**。
        """
        for item in self._auto_print_retry:
            old_key = item.get("tab_key")
            if not old_key:
                continue
            item["tab_key"] = key_map.get(old_key, "")

    def _drop_auto_print_retry_for_tab(self, tab_key: str) -> None:
        """标签页被删除/清空 → 丢弃指向它的补打队列项（防按旧 key 打到别的订单）。"""
        if not tab_key:
            return
        before = len(self._auto_print_retry)
        self._auto_print_retry[:] = [
            it for it in self._auto_print_retry if it.get("tab_key") != tab_key
        ]
        dropped = before - len(self._auto_print_retry)
        if dropped:
            self._log(f"🗑 已丢弃 {dropped} 条指向已删除标签页 {tab_key} 的补打队列项")

    def _find_tab_key_by_order_id(self, order_id: int) -> str:
        """按订单号反查当前标签页 key（标签页重编号/删除后定位补打目标用）。"""
        if not order_id:
            return ""
        for key, tab in self._config.tabs.items():
            for job in (tab.jobs or []):
                if getattr(job, "order_id", 0) == order_id:
                    return key
        return ""

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
            self._drop_auto_print_retry_for_tab(key)
        if removed:
            self._renumber_tabs()
            self._save_config()
            self._refresh_tab_display()

    def _abandon_tab_jobs(self, tab_entry) -> list:
        """通知后端放弃该标签页中**未打印**的云端任务/本地预留订单，返回其全部 jobs。

        归属/自打标记随放弃请求一起上报：否则后端只能按缺省值补记，会把「接单打印」
        的订单误盖章成「自打」（类型列与收益分类都按此字段）。金额传**整单合计**
        （不是本文件的价）：后端只认第一次命中的那笔，逐文件上报会让订单额变成
        第一个文件的价。
        """
        jobs = list(getattr(tab_entry, "jobs", None) or [])
        if not jobs or not self._cloud_client:
            return jobs
        for job in jobs:
            if getattr(job, 'sent', False):
                continue
            if job.order_id > 0:
                self._cloud_client.abandon_order_to_server(job.order_id)
            elif job.task_id > 0:
                self._cloud_client.abandon_order_to_server(job.task_id)
            elif job.order_number and "-L" not in job.order_number:
                self._cloud_client.abandon_reserved_order(
                    job.order_number, self._tab_total_price(tab_entry),
                    owner_name=(tab_entry.owner_name or ""),
                    is_admin_print=bool(tab_entry.is_admin_print))
        return jobs

    def _remove_tabs(self, keys, reason: str = "删除标签页") -> list:
        """删除一批标签页（**一次**重编号，避免逐个删除导致 key 漂移），
        并做全部联动清理：后端放弃通知 / 预约单状态机 / 补打队列 / 孤儿 PDF 缓存。
        返回**实际删除**的 key 列表（按输入顺序）。

        🔴-B2：原「标签页管理」对话框里的三个删除入口只做了
        `del self._config.tabs[key]` + `_renumber_tabs()`，漏了 `_scheduled_orders`
        与 `_auto_print_retry` 的联动清理。被删标签页的 key 不在重编号的 key_map 里
        ⇒ 预约单 `st["tab_key"]` 保留旧值，而重编号后该数字 key 已被**另一个**标签页
        接管 ⇒ 到点会把那个订单的文件打出来，并把归属/计费挂到别人的标签页快照上
        （白费纸 + 账目串单）。

        联动清理必须在 `_renumber_tabs()` **之前**（否则旧 key 已被新标签页占用）。
        UI 刷新由调用方负责。
        """
        removed_keys = []
        all_jobs = []
        for key in list(keys):
            tab_entry = self._config.tabs.get(key)
            if tab_entry is None:
                continue
            all_jobs.extend(self._abandon_tab_jobs(tab_entry))
            del self._config.tabs[key]
            removed_keys.append(key)
        if not removed_keys:
            return []
        for key in removed_keys:
            self._cleanup_scheduled_orders_for_tab(key, reason="标签页已删除")
            self._drop_auto_print_retry_for_tab(key)
        self._cleanup_orphan_pdf_cache(all_jobs)
        self._renumber_tabs()
        self._save_config()
        self._log(f"🗑 已删除标签页 {'、'.join(removed_keys)}（{reason}）")
        return removed_keys

    def _clear_all_tabs(self, reason: str = "清空全部标签页") -> None:
        """清空全部标签页并重置为单个空标签页（含预约单/补打队列联动清理）。

        同 `_remove_tabs`：原实现只重置了 `_config.tabs`，`_scheduled_orders` /
        `_auto_print_retry` 里残留的旧 key 会被重置后的「1」号标签页接管 ⇒ 到点
        自动打印时打到**另一个**订单。必须先清掉全部旧 key 的联动项再重置。
        """
        self._remove_tabs(list(self._config.tabs.keys()), reason=reason)
        self._config.tabs = {"1": self._make_new_tab()}
        self._current_tab = "1"
        self._config.active_tab = "1"
        self._save_config()

    def _scheduled_tab_still_owns(self, order_id: int, tab_key: str) -> bool:
        """预约单的 `tab_key` 是否仍指向**该订单自己的**标签页。

        🔴-B2 兜底：标签页重编号后，数字 key 可能被另一个标签页接管
        （`st["tab_key"]` 残留旧值），此时按 tab_key 直接取 jobs 会把**别的订单**
        的文件打印出来。消费前必须校验归属。
        """
        if not tab_key:
            return False
        tab = self._config.tabs.get(tab_key)
        if tab is None:
            return False
        for job in (tab.jobs or []):
            if getattr(job, "order_id", 0) == order_id:
                return True
        return False

    def _on_cleanup_empty_tabs(self):
        """F4: 点击"删除空标签页"按钮。"""
        if not self._has_empty_tabs_except_current():
            return
        self._cleanup_empty_tabs()

    def _update_duplex_label(self):
        """按当前打印机档案标注双面选项：不支持自动双面时写明「手动翻纸」。

        只在**人工确认过**（档案 auto_duplex=False + manual_duplex=True）时才改文案 —— 能力
        未确认的机器保持原样，避免凭空吓唬用户（未确认时走驱动双面，与旧版行为一致）。

        提示行还要看**当前选区能不能双面**（`_duplex_usable`，由 _on_table_selection 记下）：
        选中图片/单页文件时双面控件整体灰掉、物理上也不会双面，此时提示跟着收起 ——
        否则面板会一边灰着「单面」一边写着「分两趟打印」。

        同理，面板上**已经选着「单面打印」**时也收起（2026-10-23 用户截图反馈：单面打印下面
        还挂着「分两趟打印 · 中途需翻纸」，与旁边灰掉的「双面模式」一样自相矛盾）。这句话讲的
        是「双面要分两趟」，只有真选了双面才有意义；选了双面它立刻出现，不需要常驻。
        """
        if not hasattr(self, '_edit_duplex'):
            return
        try:
            prof = self._printer_profile()
        except Exception:
            return
        manual = (prof.auto_duplex is False and prof.manual_duplex)
        want = "双面（手动翻纸）" if manual else "双面打印"
        if self._edit_duplex.itemText(0) != want:
            self._edit_duplex.setItemText(0, want)
        if hasattr(self, '_edit_duplex_hint'):
            show = bool(manual and getattr(self, '_duplex_usable', True)
                        and self._edit_duplex.currentIndex() == 0)   # 索引 0 = 双面
            if show:
                # 文案必须**极短**（默认窗口下编辑面板视口只有 ~178px，见 task-7 复核：
                # 之前那句 27 字在里面恒为 3 行，是面板里视觉最重的一块）；正上方下拉已经
                # 写着「双面（手动翻纸）」，这里只补它没说的两件事：分两趟、中途要翻纸。
                # 完整三步说明放 tooltip（不占版面，鼠标停上去就有）。
                self._edit_duplex_hint.setText("分两趟打印 · 中途需翻纸")
                self._edit_duplex.setToolTip(
                    "本机无自动双面器（能力已人工确认）：双面由程序自己分两趟打印（全程单面）。\n"
                    "第 1 趟出纸后弹窗提示你把纸叠原样搬回纸盘、点「继续打第 2 趟」；\n"
                    "第 2 趟内容会自动做 180° 补偿，成品叠最上面是第 1 页。")
            else:
                self._edit_duplex_hint.setText("")
                self._edit_duplex.setToolTip("")
            self._edit_duplex_hint.setVisible(show)

    def _set_tab_state_badge(self, is_frozen: bool, needs_flip: bool) -> None:
        """（主线程）刷新标签页状态徽标（两处）：

          · 标签页选择器徽标 `_tab_state_badge`：空 = 正常 / 🔒 / ⏸
          · 附加服务行徽标 `_tab_scope_badge`：📑 正常 / 🔒 / ⏸（它是这一行的图标位，
            正常态也占位显示 📑，与旧外观一致）

        **只改文字与提示，绝不改宽度、不 setVisible(False)** —— 两个徽标都是固定槽位，
        状态切换不能改变 −/＋ 按钮或同行下拉框的位置（原因见文件顶部 TAB_* 常量）。
        构造阶段控件可能尚未创建 → 各自静默跳过。
        """
        if is_frozen:
            icon, tip = "🔒", TAB_FROZEN_HINT
        elif needs_flip:
            icon, tip = "⏸", TAB_FLIP_HINT
        else:
            icon, tip = "", ""

        badge = getattr(self, "_tab_state_badge", None)
        if badge is not None:
            badge.setText(icon)
            badge.setToolTip(tip)

        scope_badge = getattr(self, "_tab_scope_badge", None)
        if scope_badge is not None:
            scope_badge.setText(icon or "📑")   # 正常态用 📑 占位，槽位不留空
            scope_badge.setToolTip(tip)

    def _refresh_tab_display(self):
        """刷新标签页数字显示和按钮状态。"""
        tab = self._config.tabs.get(self._current_tab)
        is_frozen = tab.frozen if tab else False
        needs_flip = bool(getattr(tab, "manual_duplex_pending", False)) if tab else False

        # 数字标签**只放数字**，宽度在 _setup_file_table 里定死；状态走独立徽标。
        # 旧写法 `🔒{n}` + setFixedWidth(52/44/36) 会让 ＋ 按钮随状态左右挪，已废弃。
        self._tab_label.setText(self._current_tab)
        self._set_tab_state_badge(is_frozen, needs_flip)

        if hasattr(self, '_tab_scope_label') and self._tab_scope_label:
            # 可见文字**只有「标签页 N」**（宽度固定）：状态走左侧徽标，
            # ⏸/🔒 的说明走 tooltip —— 不再拼进文字里推走同行的下拉框。
            self._tab_scope_label.setText(f"标签页 {self._current_tab}")
            self._tab_scope_label.setToolTip(
                TAB_FLIP_HINT if (needs_flip and not is_frozen) else "")

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

        # 备注框：随标签页刷新加载订单备注（首个非空）；冻结时锁定编辑
        self._load_remark_editor(tab)

        # 打印机能力标注（双面是否=手动翻纸）随标签页/打印机变化同步
        self._update_duplex_label()

    def _get_current_jobs(self) -> list[PrintJob]:
        """返回当前标签页的任务列表。"""
        tab = self._config.tabs.get(self._current_tab)
        return tab.jobs if tab else []

    def _tab_jobs(self, key: str = "") -> list[PrintJob]:
        """返回指定标签页的任务列表（key 空 = 当前前台标签页）。
        2026-12：无障碍后台打印时，转换/打印回调按目标标签页定位，不再依赖前台切换。"""
        tab = self._config.tabs.get(key or self._current_tab)
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

    def _is_gui_thread(self) -> bool:
        """当前是否运行在 GUI 主线程（QObject.thread() 即该对象的归属线程）。"""
        try:
            return QThread.currentThread() is self.thread()
        except Exception:
            return True  # 判定失败时按主线程处理（保持旧行为，不丢 UI 同步）

    def _save_config(self, sync_ui: bool | None = None):
        """实时保存配置到 JSON 文件。

        sync_ui：是否先把 UI 控件状态同步回 config。默认 None = 自动判定，
        **仅当在 GUI 主线程调用时**才读控件。

        P1（审计 2026-12）：价格同步线程（_sync_pricing）、成员名单同步、
        本地订单上传等**后台线程**此前也直接调用本方法，而 _sync_ui_to_config()
        会读 `QComboBox.currentData()` 等 QWidget —— 从非 GUI 线程调用 QWidget
        方法属未定义行为（间歇性崩溃源），且与主线程并发写配置文件存在竞态。
        后台线程请传 sync_ui=False（纯 config 落盘），仅主线程可读控件。

        D5 第 14 条：本方法（含 `_config_save_lock`）是**唯一的落盘入口** ——
        GUI 各处不要再直接写 `self._config.save(...)`：那会绕开锁，与后台线程
        （价格同步/成员同步/本地订单上传）并发写同一个 JSON，可能写出半截文件
        （历史上真有 10 处直调；已全部收敛到这里，sync_ui=False 表示纯落盘、不读控件）。
        """
        try:
            if sync_ui is None:
                sync_ui = self._is_gui_thread()
            if sync_ui:
                self._sync_ui_to_config()
            with self._config_save_lock:
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
            # 删除 + 全部联动清理（后端放弃通知 / 预约单 / 补打队列 / 孤儿缓存）
            if not self._remove_tabs([key], reason="删除标签页"):
                return
            self._rebuild_table()
            self._refresh_tab_display()
            self._sync_edit_enabled(False)
            self._sweep_staged_files("删除标签页")
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
            # 清空全部 + 全部联动清理（后端放弃通知 / 预约单 / 补打队列 / 孤儿缓存）
            self._clear_all_tabs("清空全部标签页")
            self._rebuild_table()
            self._refresh_tab_display()
            self._sync_edit_enabled(False)
            self._sweep_staged_files("清空全部标签页")
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
            self._remove_tabs(removed, reason="删除空标签页")
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
            self._remove_tabs(frozen_keys, reason="删除已完成订单")
            self._rebuild_table()
            self._refresh_tab_display()
            self._sync_edit_enabled(False)
            self._sweep_staged_files("删除已完成订单")
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
        self._fill_table_row(row, job)

    def _fill_table_row(self, row: int, job: PrintJob):
        """按 job 的当前值重写第 row 行的全部单元格（新建行与「参数改完就地重画」共用）。

        ⚠ 编辑面板改参数后**必须**走这里。原先只调 `_recalc_row_cost()`（只重画「费用」列），
        于是「份数」那一格停在旧值 —— 面板写着 2 份、表格里还写着 1 份，操作员会以为自己
        没点到（2026-10 现场截图报的 bug：2 个文件都被改成 2 份，费用列已按 ×2 算，份数列仍是 1）。
        """
        ext = os.path.splitext(job.file_path)[1].lower()
        image_exts = self.IMAGE_EXTS
        is_image = ext in image_exts

        display = job.display_name or os.path.basename(job.file_path)
        name_item = QTableWidgetItem(display)
        name_item.setData(Qt.UserRole, job.file_path)  # 存储完整路径
        _tip = job.file_path
        _src = getattr(job, 'source_path', '')
        if _src and _src != job.file_path:
            # 列表任务用的是暂存副本（原件删掉/移动也不影响打印）→ 提示里给原件路径
            _tip = f"{_src}\n（打印使用暂存副本，原件删除/移动不影响打印）"
        name_item.setToolTip(_tip)
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

    def _job_user_path(self, row: int, default: str = "") -> str:
        """返回该行任务对应的「用户可见路径」：优先原件（source_path），否则列表使用的路径。
        列表任务用的是暂存副本，双击打开/打开文件位置都应指向用户自己那份文件。"""
        try:
            jobs = self._get_current_jobs()
            if 0 <= row < len(jobs):
                src = getattr(jobs[row], 'source_path', '') or ''
                if src and os.path.isfile(src):
                    return src
                return jobs[row].file_path or default
        except Exception:
            pass
        return default

    def _on_table_double_click(self, row: int, col: int):
        """双击表格行 → 用默认程序打开文件（优先原件，而非暂存副本）。"""
        name_item = self._table.item(row, self.COL_FILE)
        if name_item:
            file_path = self._job_user_path(row, name_item.data(Qt.UserRole) or "")
            if file_path and os.path.isfile(file_path):
                os.startfile(file_path)

    def _job_is_image(self, job) -> bool:
        """该任务是不是图片（图片单页、没有页码与双面的概念）。"""
        ext = os.path.splitext(getattr(job, 'file_path', '') or '')[1].lower()
        return ext in self.IMAGE_EXTS

    def _job_is_word(self, job) -> bool:
        """该任务是不是 Word 文档（.doc/.docx —— 只有它们能选 PDF 转换引擎）。"""
        ext = os.path.splitext(getattr(job, 'file_path', '') or '')[1].lower()
        return ext in (".doc", ".docx")

    def _selection_jobs(self, jobs, rows) -> list:
        """选区内的任务对象（按行号升序，越界忽略）。"""
        return [jobs[r] for r in rows if 0 <= r < len(jobs)]

    def _job_is_single_page(self, job) -> bool:
        """该任务实际只打 1 页（整份 1 页，或页码范围恰好解析出 1 页）。

        页数未知（page_count=0 且没填范围）不算单页 —— 保持「未知就允许双面」的旧行为，
        交由打印侧按真实页数处理。"""
        return _count_pages_in_range(getattr(job, 'page_range', '') or "",
                                     getattr(job, 'page_count', 0) or 0) == 1

    def _selection_traits(self, jobs, rows) -> tuple:
        """整片选区的共用特征 → (是否多选, 是否含图片, 是否含单页文件)。

        编辑面板是「一片选区共用」的，所以可用性必须按**全部**选中行判定（任一行不适用即禁用）。
        只按选区第一行判会出两种错位（2026-10 现场报的 bug）：① 选区里有单页文件，
        「单/双面」「双面模式」却亮着 —— 单页文件双面无意义；② 多选时「页码范围」亮着 ——
        同一个范围对页数不同的文件根本没有共同含义（还会被批量写进每一个文件）。
        """
        sel = self._selection_jobs(jobs, rows)
        return (len(sel) > 1,
                any(self._job_is_image(j) for j in sel),
                any(self._job_is_single_page(j) for j in sel))

    def _sync_duplex_mode_availability(self, duplex_usable: bool | None = None) -> bool:
        """「双面模式」（长/短边翻转）的可用性：**只有真的选了双面**才可用（2026-10-23 用户反馈）。

        单面打印时纸只有一面，翻转方式没有任何物理意义 —— 旧写法只跟着"选区里有没有图片/单页
        文件"走，于是面板会出现「单面打印 + 长边翻转亮着」这种自相矛盾的状态（用户拿截图报的
        就是这一处）。这里把标签与下拉**一起灰掉**（与图片/单页文件同一个表现：灰着、值不改、
        也不写回），并且**每次「单/双面」切换后都要重算**（见 _auto_apply_edit）。

        返回是否可用，调用方据此决定要不要把下拉的值写回 job（灰着的不写）。
        """
        if not hasattr(self, '_edit_duplex_mode'):
            return False
        if duplex_usable is None:
            duplex_usable = bool(getattr(self, '_duplex_usable', True))
        usable = bool(duplex_usable) and self._edit_duplex.currentIndex() == 0
        label = getattr(self, '_label_duplex_mode', None)
        if label is not None:
            label.setEnabled(usable)
        self._edit_duplex_mode.setEnabled(usable)
        return usable

    def _on_table_selection(self):
        """选中行变化 → 同步编辑面板（多选时面板显示选区第一行，改动作用于全部选中行）。"""
        rows = set(idx.row() for idx in self._table.selectedIndexes())
        # 多选提示：让操作员一眼看出「这一改会作用到几个文件」
        grp = getattr(self, '_edit_group', None)
        if grp is not None:
            grp.setTitle(f"编辑选中任务（{len(rows)} 个文件）" if len(rows) > 1
                         else "编辑选中任务")
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
        # 整片选区的共用可用性（多选/含图片/含单页 → 见 _selection_traits 说明）
        multi, any_img, any_single = self._selection_traits(jobs, sorted(rows))
        duplex_usable = (not any_img) and (not any_single)
        range_usable = (not multi) and (not any_img)
        # 记下"这份选区到底能不能双面"：双面能力提示（"分两趟打印 · 中途需翻纸"）是讲**打印机**的，
        # 但选中的是图片/单页文件时双面控件整个灰掉、物理上也不会双面 ⇒ 提示跟着收起来，
        # 免得面板一边灰着"单面"一边还写着"分两趟打印"（见 _update_duplex_label）。
        self._duplex_usable = duplex_usable

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

        # 转换引擎：选区里**只要有 Word 文件**就显示三选一（取值 = 选区里第一个 Word 文件的引擎，
        # 改动只作用于 .doc/.docx）；Word 文件之间引擎不一致时**追加**一项「混合（各自保留）」
        # 并选中它（显示真相，且此项不写回 = 保持各自设置）；一个 Word 文件都没有 → 只读标签显示
        # 第一行的实际转换方式（灰）。
        # 为什么不按「第一行」取：第一行可能是图片/PDF（它们根本没有引擎可选），照第一行取会把
        # 面板降成只读标签（选区里的 .docx 明明能选引擎）；更要命的是此时下拉只有那一个标签项，
        # 索引 0 会被当 "word" 写回，把 WPS 排版的文档静默改成 Word（见 _auto_apply_edit 的闸门）。
        sel_jobs = self._selection_jobs(jobs, sorted(rows))
        word_jobs = [j for j in sel_jobs if self._job_is_word(j)]
        if word_jobs:
            engines = {(getattr(j, 'engine', '') or 'word') for j in word_jobs}
            mixed = len(engines) > 1
            items = list(ENGINE_CHOICE_LABELS) + ([ENGINE_MIXED_LABEL] if mixed else [])
            self._edit_engine.blockSignals(True)
            self._edit_engine.clear()
            self._edit_engine.addItems(items)
            self._edit_engine.setCurrentIndex(
                len(items) - 1 if mixed else ENGINE_INDEX_OF.get(word_jobs[0].engine, 0))
            self._edit_engine.blockSignals(False)
            self._edit_engine.setToolTip(ENGINE_MIXED_TIP if mixed else "")
            # 重建 addItems 会把「引擎没装 → 灰掉」的状态冲掉 ⇒ 重建后补一次（可用性结果在
            # converter 里带缓存，重复调用很便宜）
            self._refresh_engine_availability()
        else:
            self._edit_engine.blockSignals(True)
            self._edit_engine.clear()
            self._edit_engine.addItems([_format_engine_label(ext)])
            self._edit_engine.setCurrentIndex(0)
            self._edit_engine.blockSignals(False)
            self._edit_engine.setToolTip("")

        dpi_map = {0: 0, 200: 1, 300: 2, 400: 3, 600: 4}
        self._edit_dpi.blockSignals(True)
        self._edit_dpi.setCurrentIndex(dpi_map.get(job.dpi, 0))
        self._edit_dpi.blockSignals(False)

        # 按文件类型/页数控制参数可用性（判定口径与 _selection_traits 一致 —— 看**整片选区**）：
        # 双面 → 选区里任一文件是图片或单页 → 整体禁用（单页文件双面无意义）；
        # 双面模式 → 上面那条 **且** 面板当前真的选着「双面打印」（单面没有翻转方式可言，
        #            2026-10-23 用户截图反馈；见 _sync_duplex_mode_availability）；
        # 页码范围 → 多选时整体禁用（范围对页数不同的文件没有共同含义）；单张图片也无页码；
        # PDF转换引擎 → 选区里有 Word(doc/docx) 才可用（其余格式走各自转换路径或直接打印）；
        # 图片方向 → 仅图片可用
        self._label_duplex.setEnabled(duplex_usable)
        self._edit_duplex.setEnabled(duplex_usable)
        self._sync_duplex_mode_availability(duplex_usable)
        self._label_range.setEnabled(range_usable)
        self._edit_page_range.setEnabled(range_usable)
        self._label_engine.setEnabled(bool(word_jobs))
        self._edit_engine.setEnabled(bool(word_jobs))
        if not duplex_usable:
            self._edit_duplex.blockSignals(True)
            self._edit_duplex.setCurrentIndex(1)  # 单面（仅显示；控件灰着，不会被写回）
            self._edit_duplex.blockSignals(False)
        if any_img:
            # 图片页码无意义 → 清空范围输入
            self._edit_page_range.blockSignals(True)
            self._edit_page_range.set_ranges("")
            self._edit_page_range.blockSignals(False)

        # 双面能力提示（"分两趟打印 · 中途需翻纸"）跟着选区的可用性收起/恢复 —— 见
        # _update_duplex_label：选中的是图片/单页文件时双面控件整体灰掉，提示不该还挂着。
        self._update_duplex_label()

        # 图片方向（仅图片可用）：auto=自动 / landscape=横向 / portrait=竖向
        img_ori_map = {"auto": 0, "landscape": 1, "portrait": 2}
        self._edit_img_orientation.blockSignals(True)
        self._edit_img_orientation.setCurrentIndex(img_ori_map.get(getattr(job, 'image_orientation', 'auto'), 0))
        self._edit_img_orientation.blockSignals(False)
        self._edit_img_orientation.setEnabled(is_img)
        self._label_img_orientation.setEnabled(is_img)

        # 更新选中文件标签（云端任务显示原始文件名；路径软换行避免长临时路径撑宽面板）
        # 本地任务用暂存副本打印 → 路径显示用户原件，便于核对
        shown_name = job.display_name or os.path.basename(job.file_path)
        shown_path = getattr(job, 'source_path', '') or job.file_path
        self._selected_file_label.setText(
            f"📄 {_soft_wrap_text(shown_name)}\n"
            f"路径: {_soft_wrap_text(shown_path)}"
        )

    def _sync_edit_enabled(self, enabled: bool):
        """统一启用/禁用编辑面板所有控件。"""
        if not hasattr(self, '_edit_widgets') or not self._edit_widgets:
            return
        for w in self._edit_widgets:
            w.setEnabled(enabled)
        if not enabled:
            self._selected_file_label.setText("(未选中任务)")

    def _on_remark_edited(self):
        """备注编辑 → 写入当前标签页（订单级）并保存（≤100 字）。
        不依赖选中任务、不依赖是否有文件；冻结（打印后固定）标签页锁定。
        同时同步到该标签页全部 job.remark（封面页/上报按 job 读取的兼容层）。"""
        if self._is_current_tab_frozen():
            return
        text = self._edit_remark.toPlainText()[:100]
        if self._edit_remark.toPlainText() != text:
            self._edit_remark.blockSignals(True)
            self._edit_remark.setPlainText(text)
            self._edit_remark.blockSignals(False)
        tab = self._config.tabs.get(self._current_tab)
        if not tab:
            return
        if tab.remark != text:
            tab.remark = text
        # 同步到 job（封面/上报读取 job.remark 的兼容层）
        changed = any(getattr(j, 'remark', '') != text for j in tab.jobs)
        if changed:
            for j in tab.jobs:
                j.remark = text
        if tab.remark != text or changed:
            self._save_config()

    def _load_remark_editor(self, tab=None):
        """加载当前标签页备注到编辑框（标签页级字段）；冻结或云端订单时锁定编辑。
        标签页切换 / 云端任务到达 / 打印完成冻结时调用。
        备注区高度由垂直分隔条（v_splitter）拖拽控制，不在此处做固定高度自适应。"""
        if not hasattr(self, '_edit_remark'):
            return
        tab = tab if tab is not None else self._config.tabs.get(self._current_tab)
        is_frozen = tab.frozen if tab else False
        # 云端订单的备注来自下单方：任务添加到标签页后即锁定只读，不允许本地修改
        is_cloud = self._is_cloud_order_tab(tab)
        locked = is_frozen or is_cloud
        self._label_remark.setEnabled(not locked)
        self._label_remark.setText(
            "订单备注（选填，最多 100 字；云端订单自动同步）" if not is_cloud
            else "云端订单备注（来自下单方，只读）")
        self._edit_remark.setEnabled(not locked)
        remark = tab.remark if tab else ""
        if self._edit_remark.toPlainText() != remark:
            self._edit_remark.blockSignals(True)
            self._edit_remark.setPlainText(remark)
            self._edit_remark.blockSignals(False)

    def _auto_apply_edit(self):
        """编辑面板参数变更 → 自动应用到选中的行并保存。

        多选（Ctrl/Shift/拖框）时**整片选区一起应用**：面板显示的是选区第一行的值，
        改一次作用到全部选中行 —— 否则会「看着 3 个文件被选中却只改了 1 个」（多选是
        本版新开的入口，这里必须同一口径，不然就是个静默的半成品操作）。

        但**灰着的控件一律不写回**：双面（选区含图片/单页文件）与页码范围（多选）会被
        `_on_table_selection` 禁用，此时面板往往是被迫显示第一行的值 —— 照写下去等于
        改了用户根本没点过的参数（见下方 duplex_editable / range_editable）。
        """
        if self._is_current_tab_frozen():
            return
        rows = sorted(set(idx.row() for idx in self._table.selectedIndexes()))
        if not rows:
            return
        jobs = self._get_current_jobs()

        # 控件值只读一次（下面所有选中行共用同一组值）
        copies = self._edit_copies.value()
        duplex_on = self._edit_duplex.currentIndex() == 0
        duplex_mode = "short-edge" if self._edit_duplex_mode.currentIndex() == 1 else "long-edge"
        img_ori = {0: "auto", 1: "landscape", 2: "portrait"}.get(
            self._edit_img_orientation.currentIndex(), "auto")
        ranges_valid = self._edit_page_range.is_valid()
        ranges_str = ",".join(
            inp.text().strip() for inp in self._edit_page_range._inputs
            if inp.text().strip()) if ranges_valid else ""
        engine = ENGINE_BY_INDEX.get(self._edit_engine.currentIndex())   # None = 混合项/只读标签
        dpi_map = {0: 0, 1: 200, 2: 300, 3: 400, 4: 600}
        dpi = dpi_map.get(self._edit_dpi.currentIndex(), 0)

        applied = []
        # 控件可用性按**整片选区**判定（与 _on_table_selection / _selection_traits 同一口径）：
        # · 选区里有图片/单页文件 → 双面控件是灰的 ⇒ **绝不能**按面板值写 duplex：面板此时被迫
        #   显示「单面」，照写会把同批的多页文件静默改成单面（改了用户没点过的参数）。
        # · 多选 → 页码范围整体禁用 ⇒ 不写 page_range（同一个范围对页数不同的文件没有共同含义）。
        multi, any_img, any_single = self._selection_traits(jobs, rows)
        duplex_editable = not (any_img or any_single)
        # 「双面模式」还要看**面板当前选的是不是双面**：单面打印 ⇒ 灰掉（没有翻转方式可言），
        # 且用户刚在「单/双面」上切换过值 ⇒ 必须在这里立刻重算，否则会停在
        # 「单面打印 + 长边翻转亮着」的矛盾状态（2026-10-23 用户截图）。返回值 = 下拉可用，
        # 灰着时它的值**不写回**（与图片/单页文件同一口径：灰着的不碰）。
        mode_editable = self._sync_duplex_mode_availability(duplex_editable)
        # 提示行（分两趟打印 · 中途需翻纸）同理随「单/双面」收起/恢复
        self._update_duplex_label()
        range_editable = not multi
        # 引擎：只有下拉**真的停在某个具体引擎**（Word/WPS/LibreOffice）且选区里有 Word 文件时
        # 才写回。「混合（各自保留）」项与只读标签（索引 0 的「图片渲染/直接打印」）都不写 ——
        # 后者若照索引 0 映射成 "word"，改个「份数」就把 WPS 文档静默改成 Word 渲染（串版）。
        engine_editable = (engine is not None
                           and any(self._job_is_word(j)
                                   for j in self._selection_jobs(jobs, rows)))
        for row in rows:
            if row >= len(jobs):
                continue
            job = jobs[row]
            applied.append(row)

            job.copies = copies
            # 图片/单页文件：双面无意义，固定单面（避免被禁用的双面控件残留值误写）
            # 单页判定含范围选 1 页（多页文件手动选 1 页 → 双面物理上即单面输出）
            is_img = bool(job.file_path) and os.path.splitext(job.file_path)[1].lower() in self.IMAGE_EXTS
            single_page = _count_pages_in_range(job.page_range or "", job.page_count or 0) == 1
            if is_img or single_page:
                job.duplex = "off"
            elif duplex_editable:
                job.duplex = "on" if duplex_on else "off"
                # 双面模式下拉**灰着**（当前选的是单面打印）⇒ 不写回：翻转方式与单面无关，
                # 各文件原来的值原样保留（下次改回双面时还是它自己那一档）。
                if mode_editable:
                    job.duplex_mode = duplex_mode
            # else：双面控件灰着（选区含图片/单页）→ 这一行的双面设置原样保留

            # 图片方向（仅图片可用）：方向变化后清掉已转换的缓存 PDF，打印时按新方向重渲染
            if is_img:
                if img_ori != getattr(job, 'image_orientation', 'auto'):
                    job.image_orientation = img_ori
                    job.cached_pdf = ""   # 旧方向 PDF 失效

            # 页码范围（RangeListWidget 仅校验通过才发 rangesChanged；
            # 此处再校验一次，非法输入时不写入，保留原值，避免被其他控件联动覆盖）
            if ranges_valid and range_editable:
                job.page_range = ranges_str

            # 引擎（仅 Word 文件有意义；其余格式引擎显示为转换方式，不写回 job.engine）
            is_word = os.path.splitext(job.file_path)[1].lower() in (".doc", ".docx") if job.file_path else False
            if is_word and engine_editable:
                job.engine = engine

            # DPI
            job.dpi = dpi

        # 更新表格行显示：**整行重画**（份数/单双面/页码范围/引擎/费用）。
        # 只调 _recalc_row_cost() 会留下「面板 2 份、表格 1 份」的错位（2026-10 现场报的 bug）；
        # 也不能整表 _rebuild_table()：那会清掉选区，多选连点几下「+」就断在第二次。
        for row in applied:
            self._fill_table_row(row, jobs[row])
        self._update_total_cost()
        # 实时保存配置
        self._set_current_jobs(jobs)

    def _selected_rows(self) -> list[int]:
        """表格当前选中的行号（升序、去重）。多选时含全部选中行。"""
        return sorted({idx.row() for idx in self._table.selectedIndexes()})

    def _select_only_row(self, row: int) -> None:
        """把选区替换成「只选中这一行」。

        为什么不用 `self._table.selectRow()`：它走视图的选择命令，**右键时用户还按着
        Ctrl/Shift**（多选完没松手）的那一瞬，ClearAndSelect 会被当成「追加」⇒ 选区里混进
        旧的行，菜单就对着不是用户点的选区弹出来。直连 selectionModel 永远是「替换」，
        与按键状态无关（实测：按住修饰键时 selectRow 会把旧选区留下）。
        """
        model = self._table.model()
        sm = self._table.selectionModel()
        if model is None or sm is None:
            return
        sm.select(model.index(row, 0),
                  QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)

    def _on_table_context_menu(self, pos):
        """表格右键菜单（支持多选：Ctrl/Shift/拖框选中的多行一起操作）。"""
        self._build_table_context_menu(pos).exec(self._table.viewport().mapToGlobal(pos))

    def _build_table_context_menu(self, pos) -> QMenu:
        """构造表格右键菜单（**不弹出**，由调用方决定何时 exec）。

        拆出来是为了可验收：验收脚本要能直接检查菜单项文案与启用态，而 exec() 是模态阻塞的。
        """
        menu = QMenu(self)
        # 标签页已固定（订单锁定）：除只读/复制类操作外一律禁用（删除标签页走标签页管理器）
        frozen = self._is_current_tab_frozen()
        # 检查是否点击在有效行上
        item = self._table.itemAt(pos)
        clicked_row = item.row() if item is not None else -1
        # 多选保留：右键落在**已选中**的行上时保持整片选区；落在未选中行上才只选该行
        # （旧写法无条件 selectRow ⇒ 右键一下就把多选压成一行，批量操作永远只剩一个文件）
        if clicked_row >= 0 and clicked_row not in self._selected_rows():
            self._select_only_row(clicked_row)
        rows = self._selected_rows()
        if rows:
            n = len(rows)
            # 移除选中（标签页已固定时禁用）
            remove_action = menu.addAction(
                f"🗑 移除选中（{n} 个文件）" if n > 1 else "🗑 移除选中")
            remove_action.setEnabled(not frozen)
            remove_action.triggered.connect(self._on_remove_selected)
            # 添加到新标签页（复制，原订单不动）：冻结标签页唯一的"把这几个文件再打一单"出口。
            # 菜单项**始终显示**，只在标签页已锁定（🔒 打印完成）时可用 —— 未锁定标签页里
            # 文件本来就能直接改/删，不需要复制出去（灰掉让用户知道这个功能存在、条件是什么）。
            move_action = menu.addAction(
                f"➕ 添加到新标签页（{n} 个文件）" if n > 1 else "➕ 添加到新标签页")
            move_action.setEnabled(frozen)
            move_action.setToolTip(
                "把选中的文件复制一份到新标签页重新处理\n"
                "（原标签页与已上报订单原样保留，不受影响）"
                if frozen else
                "灰色：仅对已锁定的标签页（🔒 打印完成）可用。\n"
                "当前标签页未锁定，文件可直接编辑/移除，无需复制到新标签页。")
            move_action.triggered.connect(
                lambda checked=False, rs=list(rows): self._add_rows_to_new_tab(rs))
            menu.addSeparator()
            # 打开文件位置（只读，始终可用）
            anchor_row = clicked_row if clicked_row >= 0 else rows[0]
            name_item = self._table.item(anchor_row, self.COL_FILE)
            if name_item:
                fp = self._job_user_path(anchor_row, name_item.data(Qt.UserRole) or "")
                open_action = menu.addAction("📂 打开文件位置")
                open_action.triggered.connect(lambda checked=False, p=fp: (
                    os.startfile(os.path.dirname(p)) if p and os.path.isfile(p) else None
                ))
                menu.addSeparator()

        # 粘贴（标签页已固定时禁用）
        paste_action = menu.addAction("📋 粘贴")
        paste_action.setEnabled(self._can_paste_files() and not frozen)
        paste_action.triggered.connect(self._on_paste_files)

        return menu

    def _add_rows_to_new_tab(self, rows) -> str | None:
        """把选中行**复制**到新标签页并切换过去（原标签页原样不动）。

        为什么需要（2026-10 现场「订单打印出错」的补救路径）：标签页打印完成后即冻结
        （🔒），文件既不能改也不能移除 —— 但现场经常要「把其中某个文件单独再打一单」。
        冻结标签页是已完成订单的账目快照（已回报后端、参与收支清算），只能读不能改，
        所以这里做**复制**：新标签页拿到一份彼此独立的 job。

        · 参数照抄：份数 / 单双面 / 双面翻转方向 / 页码范围 / 引擎 / DPI / 图片方向 / 页数 /
          引擎缓存 PDF（有缓存则新标签页可秒打，不必重新转换）；
        · 订单身份清零：task_id / order_id / order_number / sent / remark 全部重置
          → 新标签页是一张**全新的本地订单**，打印时重新取号、重新计费；
        · 文件不复制：两个标签页共用同一份暂存副本，引用计数（_staged_reference_paths）
          保证只要还有一个标签页引用它，副本就不会被清扫掉。

        返回新标签页 key；没有可用文件（全被跳过）时返回 None。
        """
        jobs = self._get_current_jobs()
        rows = sorted({r for r in rows if isinstance(r, int) and 0 <= r < len(jobs)})
        if not rows:
            return None
        src_key = self._current_tab
        new_jobs: list[PrintJob] = []
        skipped: list[str] = []
        for row in rows:
            job = jobs[row]
            # 文件兜底：暂存副本被外部删掉时按原件重建（与打印前兜底同一手段）
            file_path = job.file_path or ""
            if not (file_path and os.path.isfile(file_path)):
                src = getattr(job, 'source_path', '') or ""
                rebuilt = staging.stage_file(src) if src and os.path.isfile(src) else ""
                if rebuilt and os.path.isfile(rebuilt):
                    file_path = rebuilt
                else:
                    skipped.append(job.display_name or os.path.basename(job.file_path or "?"))
                    continue
            cached = getattr(job, 'cached_pdf', '') or ""
            new_jobs.append(PrintJob(
                file_path=file_path,
                copies=job.copies,
                duplex=job.duplex,
                duplex_mode=job.duplex_mode,
                page_range=job.page_range,
                page_count=job.page_count,
                orientation=job.orientation,
                engine=job.engine,
                cached_pdf=cached if (cached and os.path.isfile(cached)) else "",
                dpi=getattr(job, 'dpi', 0),
                image_orientation=getattr(job, 'image_orientation', 'auto'),
                source_md5=getattr(job, 'source_md5', ''),
                source_path=getattr(job, 'source_path', ''),
                display_name=job.display_name,
                # ── 订单身份清零：新标签页 = 新订单（绝不继承旧单的号/云端 ID/完成标记）──
                task_id=0,
                order_id=0,
                order_number="",
                remark="",
                sent=False,
            ))
        if not new_jobs:
            self._log(f"⚠ 选中的 {len(rows)} 个文件都已丢失（暂存副本与原件都不在），未创建新标签页")
            return None

        # 页数未知的 PDF（后端对云端 Word 返回 0 的常见情况）→ 复制时补一次，别让新标签页
        # 顶着「页数 ?」进打印（计费/预览都按 0 页算）。非 PDF 交给下面的预转换线程补。
        for job in new_jobs:
            if job.page_count > 0 or not job.file_path:
                continue
            if os.path.splitext(job.file_path)[1].lower() != ".pdf":
                continue
            try:
                info = get_pdf_info(job.file_path)
                job.page_count = info.get("page_count", 0)
                job.orientation = info.get("orientation", "") or job.orientation
            except Exception as e:
                logger.debug(f"复制到新标签页时读取 PDF 页数失败: {e}")

        new_key = self._next_tab_key()
        new_tab = self._make_new_tab()
        new_tab.jobs = new_jobs
        self._config.tabs[new_key] = new_tab

        # 换标签页 = 撤回备份属于旧标签页（与 _switch_tab 同一口径）
        self._cancel_undo_if_active()
        self._current_tab = new_key
        self._config.active_tab = new_key
        self._save_config()
        self._rebuild_table()
        self._refresh_tab_display()
        self._sync_edit_enabled(False)

        # 非 PDF 文件补一次预转换：命中 PDF 缓存则只是把缓存路径/页数接回来
        # （cached_pdf 不写进配置，重启后为空；这一步保证新标签页也能走缓存打印）
        for i, job in enumerate(new_jobs):
            ext = os.path.splitext(job.file_path)[1].lower()
            if ext != ".pdf" and not job.cached_pdf:
                self._start_convert_worker(i, job.file_path, job.engine, tab_key=new_key)

        msg = (f"➕ 已把 {len(new_jobs)} 个文件复制到新标签页 {new_key}"
               f"（原标签页 {src_key} 不变，新标签页打印时重新计费）")
        if skipped:
            msg += f"；{len(skipped)} 个文件已丢失被跳过：{'、'.join(skipped[:3])}"
        self._log(msg)
        return new_key


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
        self._edit_group = gb   # 多选时标题显示文件数（见 _on_table_selection）
        gl = QVBoxLayout(gb)
        gl.setSpacing(10)

        # 份数
        label_copies = QLabel("份数：")
        gl.addWidget(label_copies)
        self._edit_copies = CounterWidget(1, 99)
        self._edit_copies.valueChanged.connect(self._auto_apply_edit)
        gl.addWidget(self._edit_copies)

        # 双面
        self._label_duplex = QLabel("单/双面：")
        gl.addWidget(self._label_duplex)
        self._edit_duplex = QComboBox()
        self._edit_duplex.addItems(["双面打印", "单面打印"])
        _disable_combo_wheel(self._edit_duplex)
        self._edit_duplex.currentIndexChanged.connect(self._auto_apply_edit)
        gl.addWidget(self._edit_duplex)
        # 打印机不支持自动双面时的说明（文案随打印机档案变，见 _update_duplex_label）
        # ⚠ 必须是**可换行、不参与最小宽度**的窄提示：早期写法把一整句中文塞进不换行的
        #   QLabel，它的 sizeHint（整行文字宽度）会直接把编辑面板的最小宽度撑得比可视区
        #   还宽 ⇒ 窄面板下整行被裁掉、只剩半句（2026-10-17 用户现场截图）。
        #   修法与同一面板的 selectedFileLabel / 备注框**同策略**：wordWrap + 水平
        #   QSizePolicy.Ignored + minimumWidth(0)（宽度完全由面板给，多出来的折行），
        #   颜色与字号交给主题 QSS（QLabel#duplexHint，深浅两套）。缩进 6px 对齐字段。
        self._edit_duplex_hint = QLabel("")
        self._edit_duplex_hint.setObjectName("duplexHint")
        self._edit_duplex_hint.setWordWrap(True)
        self._edit_duplex_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._edit_duplex_hint.setMinimumWidth(0)
        self._edit_duplex_hint.setIndent(6)
        self._edit_duplex_hint.setVisible(False)
        gl.addWidget(self._edit_duplex_hint)

        # 双面模式（仅双面时可用）
        self._label_duplex_mode = QLabel("双面模式：")
        gl.addWidget(self._label_duplex_mode)
        self._edit_duplex_mode = QComboBox()
        self._edit_duplex_mode.addItems(["长边翻转", "短边翻转"])
        _disable_combo_wheel(self._edit_duplex_mode)
        self._edit_duplex_mode.currentIndexChanged.connect(self._auto_apply_edit)
        gl.addWidget(self._edit_duplex_mode)

        # 页码范围
        self._label_range = QLabel("页码范围：")
        gl.addWidget(self._label_range)
        self._edit_page_range = RangeListWidget()
        self._edit_page_range.rangesChanged.connect(self._auto_apply_edit)
        gl.addWidget(self._edit_page_range)

        # 打印引擎
        self._label_engine = QLabel("PDF转换引擎：")
        gl.addWidget(self._label_engine)
        self._edit_engine = QComboBox()
        self._edit_engine.addItems(["Word", "WPS", "LibreOffice"])
        _disable_combo_wheel(self._edit_engine)
        self._edit_engine.currentIndexChanged.connect(self._auto_apply_edit)
        gl.addWidget(self._edit_engine)
        # 灰度不可用引擎（延迟到窗口显示后，避免阻塞启动）
        QTimer.singleShot(0, self._refresh_engine_availability)

        # 渲染质量（逐文件）
        label_dpi = QLabel("DPI：")
        gl.addWidget(label_dpi)
        self._edit_dpi = QComboBox()
        self._edit_dpi.addItems(["跟随全局(默认)", "高速(200)", "标清(300)", "清晰(400)", "高清(600)"])
        _disable_combo_wheel(self._edit_dpi)
        self._edit_dpi.currentIndexChanged.connect(self._auto_apply_edit)
        gl.addWidget(self._edit_dpi)

        # 图片方向（仅图片文件可用）：auto=自动 / landscape=横向 / portrait=竖向
        self._label_img_orientation = QLabel("图片方向：")
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

        # ── 备注：右侧面板下半部分常驻区域，与上方任务参数用垂直分隔条隔开（高度可拖拽调节）──
        remark_gb = QGroupBox("备注")
        remark_gl = QVBoxLayout(remark_gb)
        remark_gl.setContentsMargins(8, 8, 8, 8)
        self._label_remark = QLabel("订单备注（选填，最多 100 字；云端订单自动同步）")
        self._label_remark.setObjectName("sectionHint")
        remark_gl.addWidget(self._label_remark)
        self._edit_remark = QPlainTextEdit()
        self._edit_remark.setPlaceholderText("给该订单添加备注（最多 100 字）")
        # 宽度修复：QPlainTextEdit 默认 sizeHint 有较大的最小宽度，会把右侧面板强制撑宽；
        # 设最小宽度 0 + 水平 Ignored（宽度完全由面板布局决定，与 selected_file_label 同策略）
        self._edit_remark.setMinimumWidth(0)
        self._edit_remark.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self._edit_remark.textChanged.connect(self._on_remark_edited)
        remark_gl.addWidget(self._edit_remark, 1)

        # 垂直分隔条：上方任务参数（可滚动） + 下方备注（常驻），高度由用户拖拽调节
        v_splitter = QSplitter(Qt.Vertical)
        v_splitter.addWidget(scroll)
        v_splitter.addWidget(remark_gb)
        v_splitter.setStretchFactor(0, 1)
        v_splitter.setStretchFactor(1, 0)
        v_splitter.setCollapsible(1, False)  # 备注区不可折叠（常驻）
        v_splitter.setSizes([580, 60])       # 初始：参数区占满，备注区约两行（≈60px），可拖拽调高
        layout.addWidget(v_splitter, 1)

        # 最小宽度限制：右侧面板受内部控件 minimumSizeHint 累积影响，会把水平 splitter
        # 撑得比期望宽（左侧文件表格被压缩）。显式把容器最小宽度压到其当前最小尺寸提示的
        # 一半（保证内容可用前提下，面板更窄、表格获得更多空间）。
        _hint_w = container.minimumSizeHint().width()
        container.setMinimumWidth(max(100, _hint_w // 2))

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

        # ── 攒批模式（多任务合并翻纸，2026-10）──
        # 只对「无自动双面器、需人工翻纸」的机型有意义（HP LaserJet 1020 等）：
        # 开启后需手动双面的打印请求不再各自翻一次纸，第 1 趟连续打进同一叠，
        # 人工点一次「开始第 2 趟」把整叠打完。攒批期间其余打印请求一律排队。
        # ⚠ **这项只对需人工翻纸的机型显示**（_sync_stack_button 按 _stack_applicable 显隐）：
        #    自动双面的机器一次成型，合并翻纸纯属多余，能力未确认的也不给入口（2026-10 用户要求）。
        # 复选框一律用 ThemedCheckBox：QSS 只画方框底色，勾选态的「✓」由
        # ThemedCheckBox.paintEvent 自己绘制。用原生 QCheckBox 会只剩一块**实心方块**
        # （没有对号），与「管理员自行打印/启用云端功能/启用接单」的观感不一致。
        self._chk_stack_mode = ThemedCheckBox("🖐 攒批模式", theme_manager=self._theme_manager)
        self._chk_stack_mode.setToolTip(
            "多任务合并翻纸：把连续多个打印任务的第 1 趟先堆进出纸槽，"
            "最后统一翻一次纸打完整个纸叠。\n"
            "只对「无自动双面器、需人工翻纸」的打印机显示 —— 自动双面的机器一次成型、"
            "不需要攒批，这一项会隐藏。\n"
            "攒批期间出纸槽被这一叠独占，其它打印请求会排队等这一叠打完。")
        self._chk_stack_mode.toggled.connect(self._on_stack_mode_toggled)
        layout.addWidget(self._chk_stack_mode)

        # 攒批入口：**只在出纸槽里真有纸叠时出现**（2026-10-21 用户要求）——
        # 列表窗本身随模式常驻（_sync_stack_list_visible），空纸叠时再挂一个「🖐 攒批」
        # 只会和左边「🖐 攒批模式」复选框并排被读成"第二个开关"。有纸叠时文字带读数
        # （`N张/M单（已取走 R）`），点它 = 把被收起的列表重新叫出来。
        self._btn_stack = QPushButton("🖐 攒批列表")
        self._btn_stack.setToolTip(
            "打开「攒批列表」：看已攒（等第 2 趟）与还在排队等着打印的文件，\n"
            "第 2 趟也从那个窗口发起。只有出纸槽里有纸叠时这一项才出现。")
        self._btn_stack.clicked.connect(self._on_stack_button)
        self._btn_stack.setVisible(False)
        layout.addWidget(self._btn_stack)

        # 视觉分隔：左边是「设置类」控件（攒批开关 / 纸叠入口），右边是页面唯一的主操作
        # 「开始打印」。两者紧挨着容易被读成同一组，故插一道短竖线做分组。
        # 颜色与粗细由 QSS（styles_dark/light.qss 的 QFrame#vSepBottomBar）给，
        # 不用 QFrame 自带的 VLine 形状 —— 它按调色板画线，深色主题下几乎看不见。
        # 纯装饰：不参与业务、无信号、不随攒批状态显隐（始终分隔左右两组）。
        layout.addSpacing(12)
        self._btnbar_sep = QFrame()
        self._btnbar_sep.setObjectName("vSepBottomBar")
        self._btnbar_sep.setFrameShape(QFrame.NoFrame)
        self._btnbar_sep.setFixedSize(1, 22)
        self._btnbar_sep.setAttribute(Qt.WA_StyledBackground, True)
        layout.addWidget(self._btnbar_sep)
        layout.addSpacing(12)

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

        # 攒批模式开关（配置 → UI；加载期间屏蔽信号，避免把保存又触发一遍）
        self._chk_stack_mode.blockSignals(True)
        self._chk_stack_mode.setChecked(bool(self._config.duplex_stack_mode))
        self._chk_stack_mode.blockSignals(False)
        self._sync_stack_button()

        # 全局渲染 DPI
        dpi_map = {0: 0, 200: 1, 300: 2, 400: 3, 600: 4}
        self._render_dpi_combo.setCurrentIndex(dpi_map.get(self._config.render_dpi, 0))

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

        delivery_on = self._config.delivery_enabled
        self._delivery_location_combo.setEnabled(delivery_on)
        self._delivery_percent_label.setText(
            f"{self._config.delivery_percentages.get(self._config.delivery_location, 0.0):.0f}%")

        self._urgency_combo.blockSignals(True)
        self._urgency_combo.setCurrentText(self._config.urgency)
        self._urgency_combo.blockSignals(False)

        urgency_price = 0.0 if self._config.urgency == "低" else self._config.urgency_prices.get(self._config.urgency, 0.0)
        self._urgency_price_label.setText(f"{urgency_price:.2f} 元")

        self._cover_page_onoff_combo.blockSignals(True)
        self._cover_page_onoff_combo.setCurrentIndex(1 if self._config.cover_page else 0)
        self._cover_page_onoff_combo.blockSignals(False)

        self._cover_page_price_label.setText(f"{self._config.cover_page_price:.2f} 元")

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
        """刷新下拉列表中的系统打印机。

        重建期间**屏蔽信号**：clear()/addItem() 时"当前项"只是中间态，会让攒批入口闪一下；
        更要紧的是顶栏的 currentIndexChanged 现在会同步 config.printer_name，而
        _load_config_to_ui 紧接着还要拿 config 里存的名字 setCurrentText —— 中间态回写会把
        用户存的打印机名清成空串（随后静默回退系统默认打印机）。
        """
        current = self._printer_combo.currentText().strip()
        self._printer_combo.blockSignals(True)
        try:
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
        finally:
            self._printer_combo.blockSignals(False)
        # 列表重建/回退后重新评估攒批入口（换到自动双面的队列就该收起来）
        self._sync_stack_button()

    def _on_printer_selection_changed(self, _index: int = 0):
        """顶栏换选打印机 → 攒批入口的显隐与生效性立刻跟着变（2026-10 用户要求）。

        攒批只对「不支持自动双面、双面需人工翻纸」的机型开放 ⇒ 换成自动双面的队列要马上收起来，
        不能等下一次刷新。这里只把选择同步进内存（落盘仍走 _save_config 的唯一入口，
        与 _sync_ui_to_config 从下拉框读 printer_name 是同一口径）。
        """
        try:
            data = self._printer_combo.currentData()
            self._config.printer_name = data if data else ""
        except Exception as e:
            logger.debug(f"切换打印机时同步选择失败: {e}")
        self._sync_stack_button()
        # 换了队列 ⇒ 双面能力要按**新队列**重判并提示（换成未盖章的机型时，
        # 双面请求会悄悄回到驱动自带的手动双面向导 —— 见 _warn_if_duplex_capability_unknown）
        self._warn_if_duplex_capability_unknown()

    def _on_refresh_printers(self):
        """刷新打印机列表按钮回调。"""
        self._refresh_printer_list()
        self._log("已刷新打印机列表")

    def _on_price_changed(self):
        """附加服务开关/选择变更 → 刷新只读价格显示并重算费用（价格本身不可在主界面编辑）。"""
        self._update_price_labels()
        for row in range(self._table.rowCount()):
            self._recalc_row_cost(row)
        self._update_total_cost()
        self._save_config()

    def _update_price_labels(self):
        """刷新主界面只读价格标签。价格由 _sync_pricing 从云端 /api/pricing 或本地配置同步，
        主界面不再提供价格编辑入口（统一在「收支统计 → 设置」维护）。"""
        if hasattr(self, '_simplex_price_label') and self._simplex_price_label:
            self._simplex_price_label.setText(f"{self._config.simplex_price:.2f} 元/张")
        if hasattr(self, '_duplex_price_label') and self._duplex_price_label:
            self._duplex_price_label.setText(f"{self._config.duplex_price:.2f} 元/张")
        if hasattr(self, '_delivery_percent_label') and self._delivery_percent_label:
            loc = self._delivery_location_combo.currentText() if hasattr(self, '_delivery_location_combo') else self._config.delivery_location
            self._delivery_percent_label.setText(f"{self._config.delivery_percentages.get(loc, 0.0):.0f}%")
        if hasattr(self, '_urgency_price_label') and self._urgency_price_label:
            level = self._urgency_combo.currentText() if hasattr(self, '_urgency_combo') else self._config.urgency
            price = 0.0 if level == "低" else self._config.urgency_prices.get(level, 0.0)
            self._urgency_price_label.setText(f"{price:.2f} 元")
        if hasattr(self, '_cover_page_price_label') and self._cover_page_price_label:
            self._cover_page_price_label.setText(f"{self._config.cover_page_price:.2f} 元")

    def _sync_pricing(self, from_thread: bool = False):
        """从价格权威源同步打印价格到 self._config（含本地标签页的首页价）。

        - 云端模式（cloud_enabled）：读取服务器 pricing.json（/api/pricing 无需鉴权），
          小程序/APP 计费显示、后端计价、本地打印首页共用同一份价格。
        - 本地模式：读取收支统计本地配置 print_data.json 的 config.pricing
          （在「收支统计 → 设置」中维护）。

        同步失败/字段缺失时保留现有 print_config.json 缓存值，不打断启动。
        from_thread=True 表示在后台线程调用（如连线同步），仅更新配置并保存，
        UI 刷新经 _pricingSynced 信号回主线程。"""
        try:
            new_pricing: dict | None = None
            if self._config.cloud_enabled and self._cloud_client and self._cloud_client.api_url:
                try:
                    resp = http_requests.get(f"{self._cloud_client.api_url}/api/pricing", timeout=8)
                    data = resp.json() if resp.ok else {}
                    if data.get("success") and isinstance(data.get("pricing"), dict):
                        new_pricing = data["pricing"]
                except Exception:
                    pass
            else:
                # 本地模式：读收支统计本地配置（价格在「收支统计 → 设置」中维护）
                local = load_local_data_file()
                if local and isinstance(local.get("config"), dict):
                    p = local["config"].get("pricing")
                    if isinstance(p, dict):
                        new_pricing = p
            if not new_pricing:
                return

            changed = False
            sp = new_pricing.get("simplex_price")
            if isinstance(sp, (int, float)) and sp > 0:
                if abs(self._config.simplex_price - float(sp)) > 1e-9:
                    changed = True
                self._config.simplex_price = float(sp)
            dp = new_pricing.get("duplex_price")
            if isinstance(dp, (int, float)) and dp > 0:
                if abs(self._config.duplex_price - float(dp)) > 1e-9:
                    changed = True
                self._config.duplex_price = float(dp)
            cp = new_pricing.get("cover_page_price")
            if isinstance(cp, (int, float)):
                if abs(self._config.cover_page_price - float(cp)) > 1e-9:
                    changed = True
                self._config.cover_page_price = float(cp)
            pct_map = new_pricing.get("delivery_percentages")
            if isinstance(pct_map, dict) and pct_map:
                clean_pct = {str(k): float(v) for k, v in pct_map.items()}
                if self._config.delivery_percentages != clean_pct:
                    changed = True
                self._config.delivery_percentages = clean_pct
            up_map = new_pricing.get("urgency_prices")
            if isinstance(up_map, dict) and up_map:
                clean_up = {str(k): float(v) for k, v in up_map.items()}
                # 按业务顺序重排（低→中→高），保证主界面优先级下拉顺序稳定
                clean_up = {k: clean_up[k] for k in _sort_urgency_keys(list(clean_up.keys()))}
                if self._config.urgency_prices != clean_up:
                    changed = True
                self._config.urgency_prices = clean_up
            # 本地标签页（无云端订单）的首页价跟随全局价格；云端订单标签页保留任务下发值
            for tab in self._config.tabs.values():
                if not any(getattr(j, 'order_id', 0) for j in tab.jobs):
                    if abs((tab.cover_page_price or 0) - self._config.cover_page_price) > 1e-9:
                        changed = True
                        tab.cover_page_price = self._config.cover_page_price
            if changed:
                # from_thread=True 时本方法跑在价格同步后台线程 → 显式禁止读 UI 控件
                self._save_config(sync_ui=not from_thread)
                # 标记价格变化，由主线程 _on_pricing_synced 打日志（此处可能在后台线程执行，不能直接 _log）
                self._pricing_changed_flag = True
            if from_thread:
                self._pricingSynced.emit()
            else:
                self._on_pricing_synced()
        except Exception as e:
            logger.debug(f"同步打印价格失败: {e}")

    def _on_pricing_synced(self):
        """（主线程）价格同步完成后刷新只读价格标签、地点/优先级下拉与标签页显示。"""
        try:
            if hasattr(self, '_delivery_location_combo') and self._delivery_location_combo:
                self._delivery_location_combo.blockSignals(True)
                self._delivery_location_combo.clear()
                self._delivery_location_combo.addItems(list(self._config.delivery_percentages.keys()))
                idx = self._delivery_location_combo.findText(self._config.delivery_location)
                if idx >= 0:
                    self._delivery_location_combo.setCurrentIndex(idx)
                self._delivery_location_combo.blockSignals(False)
            if hasattr(self, '_urgency_combo') and self._urgency_combo:
                # 优先级下拉按 低→中→高 重建，尽量保持当前选中
                new_items = _sort_urgency_keys(list(self._config.urgency_prices.keys()))
                cur = self._urgency_combo.currentText()
                if [self._urgency_combo.itemText(i) for i in range(self._urgency_combo.count())] != new_items:
                    self._urgency_combo.blockSignals(True)
                    self._urgency_combo.clear()
                    self._urgency_combo.addItems(new_items)
                    self._urgency_combo.setCurrentIndex(new_items.index(cur) if cur in new_items else 0)
                    self._urgency_combo.blockSignals(False)
            self._update_price_labels()
            self._refresh_tab_display()
            if getattr(self, '_pricing_changed_flag', False):
                self._pricing_changed_flag = False
                source = "云端" if self._config.cloud_enabled else "本地"
                self._log(f"🔄 价格已与{source}配置同步: "
                          f"单面 {self._config.simplex_price:.2f} | 双面 {self._config.duplex_price:.2f} | "
                          f"首页费 {self._config.cover_page_price:.2f}")
        except Exception as e:
            logger.debug(f"刷新价格显示失败: {e}")

    def _on_keep_temp_changed(self):
        """保存转换副本设置变更 → 实时同步到 config。"""
        self._config.keep_temp_pdf = (self._keep_temp_check.currentIndex() == 1)

    def _on_render_dpi_changed(self):
        """全局渲染 DPI 变更。"""
        dpi_values = [0, 200, 300, 400, 600]
        idx = self._render_dpi_combo.currentIndex()
        self._config.render_dpi = dpi_values[idx] if 0 <= idx < len(dpi_values) else 0

    # ---- 附加服务信号处理（每标签页独立）----

    def _is_cloud_order_tab(self, tab=None) -> bool:
        """当前/指定标签页是否为云端订单标签页（含 order_id>0 的云端任务）。
        云端订单的属性（归属/自打/附加服务）来自下单方，本地不允许调整（2026-12）。"""
        if tab is None:
            tab = self._config.tabs.get(self._current_tab)
        if not tab:
            return False
        return any(getattr(j, 'order_id', 0) > 0 for j in (tab.jobs or []))

    def _is_tab_editable(self, tab=None) -> bool:
        """标签页的订单属性（归属/自打/附加服务）是否可修改：未冻结 且 非云端订单。"""
        if tab is None:
            tab = self._config.tabs.get(self._current_tab)
        if not tab:
            return True
        return not tab.frozen and not self._is_cloud_order_tab(tab)

    def _sync_tab_settings_to_ui(self, tab):
        """将标签页的附加服务设置同步到 UI 控件。
        冻结 或 云端订单标签页 → 归属/自打/附加服务全部锁定（云端订单属性来自下单方，
        本地不可调整；普通用户订单的「管理员自行打印」因此固定为灰色）。"""
        if not tab or not hasattr(self, '_delivery_onoff_combo'):
            return
        cloud_locked = self._is_cloud_order_tab(tab)
        if tab.frozen or cloud_locked:
            self._delivery_onoff_combo.setEnabled(False)
            self._delivery_location_combo.setEnabled(False)
            self._urgency_combo.setEnabled(False)
            self._cover_page_onoff_combo.setEnabled(False)
            if hasattr(self, '_owner_combo') and self._owner_combo:
                self._owner_combo.setEnabled(False)
            if hasattr(self, '_admin_print_check') and self._admin_print_check:
                self._admin_print_check.setEnabled(False)
            if cloud_locked:
                # 云端订单（未冻结）：展示订单下发值（只读），而非残留上一个标签页的值
                self._apply_tab_settings_values(tab, editable=False)
            return
        self._apply_tab_settings_values(tab, editable=True)

    def _apply_tab_settings_values(self, tab, editable: bool = True):
        """同步标签页附加服务/归属/自打的值到控件（云端订单只读展示也走这里）。"""
        self._delivery_onoff_combo.blockSignals(True)
        self._delivery_onoff_combo.setCurrentIndex(1 if tab.delivery_enabled else 0)
        self._delivery_onoff_combo.blockSignals(False)
        if editable:
            self._delivery_onoff_combo.setEnabled(True)
        self._delivery_location_combo.setEnabled(bool(tab.delivery_enabled) and editable)
        if tab.delivery_location:
            idx = self._delivery_location_combo.findText(tab.delivery_location)
            if idx >= 0:
                self._delivery_location_combo.setCurrentIndex(idx)
        pct = self._config.delivery_percentages.get(tab.delivery_location, 0.0)
        self._delivery_percent_label.setText(f"{pct:.0f}%")
        self._urgency_combo.blockSignals(True)
        self._urgency_combo.setCurrentText(tab.urgency)
        self._urgency_combo.blockSignals(False)
        if editable:
            self._urgency_combo.setEnabled(True)
        is_low = (tab.urgency == "低")
        self._urgency_price_label.setText(
            f"{0.0 if is_low else self._config.urgency_prices.get(tab.urgency, 0.0):.2f} 元")
        self._cover_page_onoff_combo.blockSignals(True)
        self._cover_page_onoff_combo.setCurrentIndex(1 if tab.cover_page else 0)
        self._cover_page_onoff_combo.blockSignals(False)
        if editable:
            self._cover_page_onoff_combo.setEnabled(True)
        self._cover_page_price_label.setText(f"{tab.cover_page_price:.2f} 元")
        # 订单归属（v24）：归属管理员下拉 + 管理员自行打印勾选
        if hasattr(self, '_owner_combo') and self._owner_combo:
            if editable:
                self._owner_combo.setEnabled(True)
            self._update_owner_combo_items()   # 内部同步 admin_print_check 启用态（无成员时禁用）
            if not editable:
                # _update_owner_combo_items 可能按成员名单重新启用 → 云端订单恢复锁定
                self._owner_combo.setEnabled(False)
        if hasattr(self, '_admin_print_check') and self._admin_print_check:
            self._admin_print_check.blockSignals(True)
            self._admin_print_check.setChecked(bool(tab.is_admin_print))
            self._admin_print_check.blockSignals(False)
            if not editable:
                self._admin_print_check.setEnabled(False)

    def _member_names(self) -> list[str]:
        """真实成员名单（剔除旧版占位名 张三/李四/王五）。归属下拉与建单校验共用。"""
        return [n for n in (self._config.admin_names or []) if n and n not in PLACEHOLDER_OWNER_NAMES]

    # ---- 本设备所有者（2026-12：收支清算「授权」页绑定的成员姓名）----

    def _get_device_owner_name(self) -> str:
        """本设备所有者姓名：优先实时缓存（后端 printer_state 推送），回退本地配置缓存。
        未绑定/离线/本地模式返回空串（新建标签页时由 _update_owner_combo_items 兜底到第一个成员）。"""
        st = getattr(self, "_cloud_printer_state_cache", None) or {}
        name = (st.get("owner_name") or "").strip()
        if not name:
            client = getattr(self, "_cloud_client", None)
            if client is not None:
                name = ((getattr(client, "last_printer_state", None) or {}).get("owner_name") or "").strip()
        if not name:
            name = (getattr(self._config, "cloud_owner_name", "") or "").strip()
        return name

    def _make_new_tab(self) -> "TabSettings":
        """新建标签页的默认设置（2026-12）：
        · 订单归属者默认 = 本设备所有者（授权页绑定；未绑定时为空，由归属下拉兜底到第一个成员）；
        · 默认关闭「管理员自行打印」（is_admin_print=False）→ 新订单按顾客订单计费。"""
        tab = TabSettings()
        tab.owner_name = self._get_device_owner_name()
        tab.is_admin_print = False
        return tab

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
        # v5.25：未绑定顾客订单的固定归属「普通用户」——即使不在成员名单也作为合法候选，
        # 保证标签页归属不被「落到第一个成员」的兜底覆盖
        candidates = list(names)
        if tab_owner == FALLBACK_OWNER_NAME and FALLBACK_OWNER_NAME not in candidates:
            candidates.append(FALLBACK_OWNER_NAME)
        # v5.26：云端订单标签页的归属来自下单方（绑定成员/普通用户）→ 即使暂不在成员名单
        # （名单尚未同步）也保留，不被「落到第一个成员」覆盖
        if tab and self._is_cloud_order_tab(tab) and tab_owner and tab_owner not in candidates:
            candidates.append(tab_owner)
        if candidates:
            # 有候选：标签页归属为空/占位名/已不在候选 → 自动落到第一个真实成员（写回标签页，保证建单归属非空）
            if (not tab_owner) or tab_owner in PLACEHOLDER_OWNER_NAMES or tab_owner not in candidates:
                act_owner = candidates[0]
            else:
                act_owner = tab_owner
            combo_items = candidates
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
                self._save_config(sync_ui=False)   # 后台线程（owner-name-sync）→ 不读 UI 控件
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
                self._save_config(sync_ui=False)   # 后台线程（owner-name-sync）→ 不读 UI 控件
        except Exception as e:
            logger.debug(f"同步云端成员名单失败: {e}")

    def _on_owner_changed(self, text: str):
        """归属成员变更 → 写入当前标签页（下拉只允许选择，选择即保存）。
        「(无成员)」仅展示，不写入标签页（写空归属）；点击引导见 _on_owner_activated。"""
        if not self._is_tab_editable():
            return
        if text == "(无成员)":
            return
        tab = self._config.tabs.get(self._current_tab)
        if tab:
            tab.owner_name = (text or "").strip()
        self._save_config()

    def _on_admin_print_toggled(self, checked: bool):
        """管理员自行打印勾选变更 → 写入当前标签页。"""
        if not self._is_tab_editable():
            return
        tab = self._config.tabs.get(self._current_tab)
        if tab:
            tab.is_admin_print = bool(checked)
        self._save_config()

    def _on_delivery_toggled(self):
        """派送开关变更 → 写入当前标签页。"""
        if not self._is_tab_editable():
            return
        enabled = (self._delivery_onoff_combo.currentIndex() == 1)
        tab = self._config.tabs.get(self._current_tab)
        if tab:
            tab.delivery_enabled = enabled
        self._delivery_location_combo.setEnabled(enabled)
        self._save_config()
        self._on_price_changed()

    def _on_cover_page_toggled(self):
        """首页开关变更 → 写入当前标签页。"""
        if not self._is_tab_editable():
            return
        enabled = (self._cover_page_onoff_combo.currentIndex() == 1)
        tab = self._config.tabs.get(self._current_tab)
        if tab:
            tab.cover_page = enabled
        self._save_config()
        self._on_price_changed()

    def _on_delivery_location_changed(self):
        """派送地点变更 → 更新只读百分比显示并写入当前标签页。"""
        if not self._is_tab_editable():
            return
        loc = self._delivery_location_combo.currentText()
        tab = self._config.tabs.get(self._current_tab)
        if tab:
            tab.delivery_location = loc
        pct = self._config.delivery_percentages.get(loc, 0.0)
        self._delivery_percent_label.setText(f"{pct:.0f}%")
        self._save_config()
        self._on_price_changed()

    def _on_urgency_changed(self):
        """优先级变更 → 写入当前标签页。"低"时价格显示 0.00。"""
        if not self._is_tab_editable():
            return
        level = self._urgency_combo.currentText()
        tab = self._config.tabs.get(self._current_tab)
        if tab:
            tab.urgency = level
        is_low = (level == "低")
        price = 0.0 if is_low else self._config.urgency_prices.get(level, 0.0)
        self._urgency_price_label.setText(f"{price:.2f} 元")
        self._save_config()
        self._on_price_changed()

    def _sync_ui_to_config(self):
        """将 UI 控件数据同步回配置对象。"""
        printer_data = self._printer_combo.currentData()
        self._config.printer_name = printer_data if printer_data else ""

        self._config.keep_temp_pdf = (self._keep_temp_check.currentIndex() == 1)

        # 价格（单双面/派送百分比/加急/首页费）统一在「收支统计 → 设置」维护，
        # 由 _sync_pricing 同步到 self._config，主界面不提供价格编辑入口。
        dpi_values = [0, 200, 300, 400, 600]
        idx = self._render_dpi_combo.currentIndex()
        self._config.render_dpi = dpi_values[idx] if 0 <= idx < len(dpi_values) else 0

        self._config.last_dir = self._last_dir

        # 附加服务（订单属性选择，价格本身不在此编辑）
        self._config.delivery_enabled = (self._delivery_onoff_combo.currentIndex() == 1)
        self._config.delivery_location = self._delivery_location_combo.currentText()
        self._config.urgency = self._urgency_combo.currentText()
        self._config.cover_page = (self._cover_page_onoff_combo.currentIndex() == 1)

        # jobs 已通过表格实时维护并保存到 tabs 中

    def _find_job_row_by_file_path(self, file_path: str) -> int | None:
        """按 file_path 在当前标签页查找任务行号，找不到返回 None。"""
        jobs = self._get_current_jobs()
        for i, j in enumerate(jobs):
            if j.file_path == file_path:
                return i
        return None

    def _start_convert_worker(self, row: int, file_path: str, engine: str, tab_key: str = "",
                              from_cloud: bool = False):
        """启动后台 PDF 转换线程。先检查 MD5 缓存，命中则跳过转换。

        MD5 一律从 file_path 计算（不再依赖 jobs[row].source_md5 ——
        多文件订单并发转换时行号可能错位，会读到别的文件的 MD5）。
        仅当任务上已有相同 file_path 的 source_md5 时复用，避免重复计算大文件。
        tab_key（2026-12）：目标标签页（无障碍后台创建时定位 job；空 = 当前前台标签页）。
        from_cloud（S1-04）：云端任务 ⇒ Office 文档强制 LibreOffice，不走 Word/WPS COM。"""
        # 计算源文件 MD5 并检查 PDF 缓存
        source_md5 = ""
        image_orientation = "auto"
        jobs = self._tab_jobs(tab_key)
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
        # 检查本地 PDF 缓存（图片按方向后缀、docx 按转换引擎分开）
        if source_md5:
            # 引擎后缀判定统一走 cache_engine_for（写/读/打印三侧同一口径）
            _cache_engine = cache_engine_for(file_path, engine)
            if self._cloud_client:
                cached_pdf, cached_meta = self._cloud_client._get_cached_pdf(
                    source_md5, image_orientation, _cache_engine)
            else:
                # 离线：直接检查 pdf_cache 目录
                import paths as _paths
                cache_dir = _paths.pdf_cache_dir()
                cached_pdf = os.path.join(
                    cache_dir, pdf_cache_key(source_md5, image_orientation, _cache_engine) + ".pdf")
                cached_meta = {}
                if not os.path.isfile(cached_pdf):
                    cached_pdf = None
            if cached_pdf:
                from pdf_printer import get_pdf_info as _gpi
                info = _gpi(cached_pdf)
                page_count = info.get("page_count", cached_meta.get("page_count", 0))
                orientation = info.get("orientation", "")
                self._log(f"📦 缓存命中: {os.path.basename(file_path)} → {page_count} 页 (MD5={source_md5[:8]}...)")
                # 按 file_path 在目标标签页匹配行回写（不依赖调用方传入的 row，防多文件订单行错位写错行）
                jobs = self._tab_jobs(tab_key)
                match_row = None
                for i, j in enumerate(jobs):
                    if j.file_path == file_path:
                        match_row = i
                        break
                if match_row is not None:
                    jobs[match_row].source_md5 = source_md5
                    jobs[match_row].cached_pdf = cached_pdf
                    jobs[match_row].page_count = page_count
                    jobs[match_row].orientation = orientation
                    self._save_config()
                    # 仅当目标标签页是当前前台标签页时更新表格（后台标签页表格不可见）
                    if not tab_key or tab_key == self._current_tab:
                        self._table.item(match_row, self.COL_PAGES).setText(str(page_count))
                        ori_map = {"portrait": "竖", "landscape": "横", "mixed": "混"}
                        self._table.item(match_row, self.COL_ORIENT).setText(ori_map.get(orientation, ""))
                        self._recalc_row_cost(match_row)
                        self._update_total_cost()
                return
        # 缓存未命中 → 启动转换线程（同 file_path 的旧 worker 先取消，防重复写缓存）
        self._cancel_convert_worker_for_path(file_path)
        worker = ConvertWorker(row, file_path, engine, source_md5, from_cloud=from_cloud)
        worker.finished.connect(
            lambda r, fp, pdf, pc, ori, tk=tab_key: self._on_convert_finished(r, fp, pdf, pc, ori, tk))
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
        """查询引擎可用性，灰度不可用的下拉选项。

        下拉有两种内容，别搞混（2026-10 收拾）：
        ① 引擎选择（「Word / WPS / LibreOffice」+ 可能的「混合（各自保留）」）—— 按可用性灰度前 3 项；
        ② **单项只读标签**（「直接打印 / 图片渲染 / reportlab / …」，选中的文件都不是 Word）——
           它不是引擎选项，**不能**拿 Word 的可用性去灰它（旧写法按索引 0 灰 ⇒ Word 没装时
           连「直接打印」这种只读标签都被灰掉）。
        该方法在启动与每次重建下拉后调用（重建会把灰度状态冲掉，所以在 _on_table_selection 里补调）。
        """
        from converter import get_available_engines
        model = self._edit_engine.model()
        if model is None:
            return
        n = model.rowCount()
        if n <= 1:
            item = model.item(0)
            if item is not None:
                item.setEnabled(True)
                item.setToolTip("")
            return
        available = get_available_engines()
        for idx, eng in ENGINE_BY_INDEX.items():     # 注意方向：字典是「索引 → 引擎名」
            if idx >= n:
                continue
            item = model.item(idx)
            if item is None:
                continue
            ok = available.get(eng, False)
            item.setEnabled(ok)
            item.setToolTip("" if ok else f"{eng.upper()} 未安装，不可用")
        # 「混合（各自保留）」不是引擎，永远可选（它本来就只想显示真相）
        mixed_item = model.item(len(ENGINE_CHOICE_LABELS))
        if mixed_item is not None:
            mixed_item.setEnabled(True)
            mixed_item.setToolTip(ENGINE_MIXED_TIP)

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
        # 尝试从后端获取（网络放后台线程 + processEvents 保活事件循环，
        # 避免同步 GET 冻结主线程导致云端推送/下载完成等信号延迟处理）
        online = self._cloud_client and self._cloud_client.is_connected()
        if online and self._cloud_client.api_url and self._cloud_client.token:
            tab_key = self._current_tab
            result: dict = {}

            def _net():
                try:
                    resp = http_requests.get(
                        f"{self._cloud_client.api_url}/api/next_order_number",
                        params={**{"token": self._cloud_client.token}, **self._current_owner_params()},
                        timeout=5,
                    )
                    if resp.ok:
                        data = resp.json()
                        result["order_number"] = data.get("order_number", "") if data.get("success") else ""
                    else:
                        result["order_number"] = ""
                except Exception:
                    result["order_number"] = ""

            threading.Thread(target=_net, daemon=True).start()
            # S1-09：取号等待 ≤ 5s（原 6s），超时直接回落本地 -L 临时号
            deadline = time.time() + ORDER_NUMBER_WAIT_S
            while "order_number" not in result and time.time() < deadline:
                QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
                time.sleep(0.02)
            order_number = result.get("order_number", "")
            if self._closing:
                return ""  # 等待期间窗口被关闭，中止
            if order_number:
                # 等待期间用户可能已切换标签页：仅当仍在原标签页时才写回
                if self._current_tab == tab_key:
                    for j in jobs: j.order_number = order_number
                    self._set_current_jobs(jobs)
                    self._refresh_tab_display()
                    self._log(f"📋 已分配订单号: {order_number}")
                return order_number
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
        # 本地订单号换云端号（本方法在后台线程执行）→ 不读 UI 控件
        self._save_config(sync_ui=False)
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
                    # 备注取所属标签页（订单级）
                    _remark = next((t2.remark for t2 in self._config.tabs.values()
                                    if t2.remark and any(j.order_number == new_num for j in t2.jobs)), '')
                    payload = {
                        "order_number": new_num,
                        # v4.6：按本地是否已打印决定上报状态——仅复制价格未打印 → reserved（非空占位）；
                        # 已打印完成 → sent（覆盖）。避免"离线复制价格未打印"被重连误标为已完成。
                        "status": "sent" if any(getattr(j, 'sent', False) for j in order_jobs) else "reserved",
                        "remark": _remark,
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
        """把离线库行（id, order_number, files_json, total_price, created_at, owner_name, is_admin_print, remark）
        构造成 /api/local_orders 的 payload（孤儿单换号上报用）。"""
        dbid, num, files_json, total_price, created_at, owner_name, is_admin_print, remark = row
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
            # 离线库行均为打印启动时缓存（已完成打印）→ 上报 sent；备注一并携带
            "status": "sent",
            "remark": str(remark or "")[:100],
            "total_price": float(total_price or 0),
            "files": files,
            "created_at": created_at or "",
        }

    def _tab_total_price(self, tab) -> float:
        """标签页全部任务的合计金额（含附加服务费），按本机配置单价计算。

        放弃预留单时补记的必须是**整单金额**：早先是「每个文件各上报一次」，
        后端只认第一次命中的那笔 → 订单额被写成第一个文件的价
        （实测 HN20261011-0001 订单额 ¥6.30、文件合计 ¥7.40）。
        单价必须显式传：calc_cost 的默认单价是历史遗留值 0.2/0.3。
        """
        if not tab:
            return 0.0
        total = 0.0
        for j in (getattr(tab, "jobs", None) or []):
            cost, _ = calc_cost(j.page_count, j.copies, j.duplex,
                                self._config.simplex_price, self._config.duplex_price,
                                j.page_range or "")
            total += cost
        try:
            total += tab.calc_extra_total(total, self._config) or 0.0
        except Exception:
            pass
        return round(total, 2)

    def _upload_reserved_order(self, order_number: str):
        """后台线程：以 reserved 状态上报当前标签页完整订单信息（文件明细 + 备注 + 价格 + 归属）。

        v4.6：复制价格后即把订单同步到后端，"已预留"不再是空占位；后续添加/修改文件
        再次复制价格会覆盖（后端按 order_number 删旧插新），保证后端信息与本地一致。"""
        jobs = self._get_current_jobs()
        tab = self._config.tabs.get(self._current_tab)
        client = self._cloud_client
        if not jobs or not tab or not client:
            return
        # 云端订单（任何 job 带 task_id）→ 它的来源/明细由云端订单自己维护，
        # 不能借 /api/local_orders 把 source 改写成 'local'（2026-10-18 现场：小程序订单
        # 打印后变「本地」）。后端也有同一道闸门（热部署即生效），这里是客户端侧的第一道。
        if any(int(getattr(j, "task_id", 0) or 0) > 0 for j in jobs):
            return
        files_data = []
        total = 0.0
        for j in jobs:
            cost = calc_cost(j.page_count, j.copies, j.duplex,
                self._config.simplex_price, self._config.duplex_price,
                j.page_range)[0]
            total += cost
            files_data.append({
                "file_name": j.display_name or os.path.basename(j.file_path),
                "copies": j.copies,
                "page_count": j.page_count,
                "cost": round(cost, 2),
                "duplex": j.duplex if _count_pages_in_range(j.page_range or "", j.page_count or 0) > 1 else "off",
                "page_range": j.page_range,
            })
        tab_extra = tab.calc_extra_total(total, self._config) if tab else 0.0
        total_price = round(total + tab_extra, 2)
        remark = (tab.remark or '')
        payload = {
            "order_number": order_number,
            "status": "reserved",
            "total_price": total_price,
            "files": files_data,
            "owner_name": (tab.owner_name or ""),
            "is_admin_print": bool(tab.is_admin_print),
            "remark": remark,
            # 接收设备（多设备接单）：本机 client_id，后端据此记录订单「接单设备」
            "client_id": client.client_id,
            "delivery_enabled": bool(tab.delivery_enabled),
            "delivery_location": tab.delivery_location or "",
            "urgency": tab.urgency or "低",
            "cover_page": bool(tab.cover_page),
            "cover_page_price": (tab.cover_page_price if tab else self._config.cover_page_price),
        }
        api_url = client.api_url or ""
        token = client.token or ""

        def _net():
            try:
                resp = http_requests.post(
                    f"{api_url}/api/local_orders",
                    params={"token": token},
                    json=payload, timeout=10,
                )
                if resp.ok and resp.json().get("success"):
                    self._cloudConnSyncLog.emit(
                        f"📋 预留订单已同步云端: {order_number} ({len(files_data)} 个文件)")
                else:
                    self._cloudConnSyncLog.emit(
                        f"⚠️ 预留订单同步失败: {resp.text[:120] if resp.text else '无响应'}")
            except Exception as e:
                self._cloudConnSyncLog.emit(f"⚠️ 预留订单同步失败: {e}")

        threading.Thread(target=_net, daemon=True).start()

    def _on_copy_total(self):
        """复制合计金额到剪贴板（含订单号）。无成员时禁止创建订单。"""
        if not self._has_members():
            self._show_no_member_hint()
            return
        jobs = self._get_current_jobs()
        if not jobs:
            return  # 标签页无文件时不复制价格（首页费等附加费不构成独立价格）
        order_number = self._ensure_order_number()
        # v4.6：预留完整上报——复制价格后即把文件明细 + 备注 + 价格同步到后端（reserved 非空占位）。
        # 后续添加/修改文件再次复制价格会覆盖；离线时仅本地 -L 号，重连后由换号流程补报。
        if order_number and self._cloud_client and self._cloud_client.is_connected():
            self._upload_reserved_order(order_number)
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
        # mimeData() 可能是 None（剪贴板无属主/远程会话/offscreen 平台）——不能直接解引用，
        # 否则右键菜单一弹就崩（本方法在 _build_table_context_menu 里无条件被调用）
        mime = QApplication.clipboard().mimeData()
        if mime is not None and mime.hasUrls():
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

    def _clipboard_paths(self) -> list[str]:
        """剪贴板里的**全部**本地文件路径（文件管理器 URL + 纯文本路径，不按扩展名过滤）。

        S0-09：粘贴 `.html/.htm` 时要给明确拒绝文案，所以先把候选路径全部收上来，
        再由调用方区分「可添加的格式」与「已被永久删除的 HTML」。
        """
        paths: list[str] = []
        mime = QApplication.clipboard().mimeData()
        if mime is not None and mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    path = url.toLocalFile()
                    if os.path.isfile(path):
                        paths.append(os.path.normpath(path))
        if not paths:
            text = QApplication.clipboard().text().strip()
            for line in text.splitlines():
                line = line.strip().strip('"').strip("'")
                if line and os.path.isfile(line):
                    paths.append(os.path.normpath(line))
        return paths

    def _warn_html_unsupported(self, paths: list[str]):
        """S0-09 / D-2：往列表里加 `.html/.htm` 时给明确文案（不静默丢弃）。"""
        if not paths:
            return
        names = "、".join(os.path.basename(p) for p in paths[:5])
        if len(paths) > 5:
            names += f" 等 {len(paths)} 个文件"
        msg = f"{HTML_UNSUPPORTED_MSG}\n\n已跳过: {names}"
        self._log("⛔ " + msg.replace("\n", " "))
        QMessageBox.warning(self, "不支持 HTML 文件", msg)

    def _on_paste_files(self):
        """从剪贴板粘贴文件，支持文件管理器复制和纯文本路径。"""
        allowed = {".pdf", ".doc", ".docx", ".txt", ".md",
                   ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
        files: list[str] = []
        # S0-09：HTML 已永久不支持 —— 单独收集，给明确提示而不是默默忽略
        html_paths = [
            p for p in self._clipboard_paths()
            if os.path.splitext(p)[1].lower() in (".html", ".htm")
        ]

        # 1. 优先处理文件管理器复制的 URL 列表（mimeData 可能为 None，同 _can_paste_files）
        mime = QApplication.clipboard().mimeData()
        if mime is not None and mime.hasUrls():
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
        if html_paths:
            self._warn_html_unsupported(html_paths)
        elif not files:
            QMessageBox.information(self, "粘贴", "剪贴板中没有可识别的文件。\n\n"
                                    "请先在文件管理器中复制文件(Ctrl+C)，再来粘贴。\n"
                                    "支持格式: PDF, Word(.doc/.docx), "
                                    "文本(.txt/.md), 图片(.jpg/.png/.bmp等)")

    def _on_add_files(self):
        """通过文件对话框添加文件。"""
        file_filter = (
            "所有支持格式 (*.pdf *.doc *.docx *.txt *.md"
            " *.jpg *.jpeg *.png *.bmp *.gif *.webp);;"
            "PDF (*.pdf);;"
            "Word 文档 (*.doc *.docx);;"
            "文本 (*.txt *.md);;"
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
            return
        # S0-09 / D-2：只在剪贴板里是 HTML 时也要给明确文案（否则 Ctrl+V 毫无反应）
        html_paths = [
            p for p in self._clipboard_paths()
            if os.path.splitext(p)[1].lower() in (".html", ".htm")
        ]
        if html_paths:
            self._warn_html_unsupported(html_paths)

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

    # ──────── 暂存副本（工作副本）引用与清理 ────────
    # 添加文件时每个原件都会复制一份到 %APPDATA%\HN打印工具\file_cache（staging.py），
    # 云端任务的下载文件也落在这里（cloud_<task_id>\），只要还有任务引用副本就不能删；
    # 任务被移除/清空/打印完成后由下面两个方法回收。

    def _cloud_kept_paths(self) -> list[str]:
        """尚未变成列表任务、但仍需要文件的云端任务路径（下载中/待确认/预约待打印）。

        云端文件下载完成到用户点「接受」之间不挂在任何 job 上，如果这段时间触发清理
        （用户移除任务/清空列表/退出）就会把刚下好的文件删掉 → 接受后打印报文件不存在。
        这里把这些「半路任务」的文件路径一并算作引用。"""
        paths: list[str] = []
        for task in (getattr(self, '_cloud_tasks', None) or {}).values():
            p = getattr(task, 'local_path', '') or ''
            if p:
                paths.append(p)
        client = getattr(self, '_cloud_client', None)
        if client is not None:
            try:
                for task in client.get_pending_tasks():
                    p = getattr(task, 'local_path', '') or ''
                    if p:
                        paths.append(p)
            except Exception:
                pass
        # 预约单状态机：ready/pending 里的任务可能已从 _cloud_tasks 摘除，文件仍要用
        for entry in (getattr(self, '_scheduled_orders', None) or {}).values():
            if not isinstance(entry, dict):
                continue
            for key in ("ready", "pending"):
                for task in (entry.get(key) or {}).values():
                    p = getattr(task, 'local_path', '') or ''
                    if p:
                        paths.append(p)
        return paths

    def _staged_reference_paths(self) -> set[str]:
        """收集仍被引用的暂存副本路径：全部标签页任务 + 撤回备份 + 打印批次 + 未落地的云端任务。"""
        refs: list[str] = []
        for tab in self._config.tabs.values():
            for job in (tab.jobs or []):
                if getattr(job, 'file_path', ''):
                    refs.append(job.file_path)
        for jobs in self._cleared_jobs_backup.values():
            for job in (jobs or []):
                if getattr(job, 'file_path', ''):
                    refs.append(job.file_path)
        for job in (getattr(self, '_flat_jobs', None) or []):
            if getattr(job, 'file_path', ''):
                refs.append(job.file_path)
        worker = getattr(self, '_worker', None)
        for job in (getattr(worker, '_jobs', None) or []):
            if getattr(job, 'file_path', ''):
                refs.append(job.file_path)
        refs.extend(self._cloud_kept_paths())
        out = set()
        for p in refs:
            try:
                out.add(os.path.normcase(os.path.abspath(p)))
            except (OSError, ValueError):
                continue
        return out

    def _adopt_legacy_cloud_files(self) -> int:
        """把旧版本遗留在 %TEMP%\\hn_cloud_* 的云端任务文件收进暂存目录。

        旧版下载到系统临时目录：临时目录被清理、或旧版「启动清理 24h 以上 hn_cloud_*」
        都会删掉仍在列表里的任务文件 → 打印报文件不存在。升级后首次启动把这些文件复制
        一份到暂存目录并改指副本（必须早于 CloudClient.start() 的旧临时文件清理）。"""
        adopted = 0
        try:
            temp_root = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
        except Exception:
            return 0
        for tab in self._config.tabs.values():
            for job in (tab.jobs or []):
                fp = getattr(job, 'file_path', '') or ''
                if not fp or staging.is_staged(fp):
                    continue
                try:
                    norm = os.path.normcase(os.path.abspath(fp))
                except (OSError, ValueError):
                    continue
                if not norm.startswith(temp_root + os.sep):
                    continue
                if not os.path.basename(fp).startswith("hn_cloud_"):
                    continue
                if not os.path.isfile(fp):
                    continue
                new_path = staging.stage_file(fp)
                if new_path and new_path != fp and os.path.isfile(new_path):
                    job.file_path = new_path
                    adopted += 1
        if adopted:
            try:
                self._save_config(sync_ui=False)
            except Exception as e:
                logger.warning(f"接管旧版云端临时文件后保存配置失败: {e}")
            self._file_logger.info("已接管 %d 个旧版云端临时文件到暂存目录", adopted)
        return adopted

    def _sweep_staged_files(self, reason: str = "", min_age_seconds: float = 0.0) -> int:
        """清理不再被任何任务引用的暂存副本，返回删除数量。"""
        try:
            removed = staging.sweep(self._staged_reference_paths(),
                                    min_age_seconds=min_age_seconds)
        except Exception as e:
            logger.warning(f"清理暂存副本失败: {e}")
            return 0
        if removed:
            self._log(f"🧹 已清理 {removed} 个暂存文件副本" + (f"（{reason}）" if reason else ""))
        return removed

    def _on_undo_expired(self):
        """撤回超时 → 确认清空，通知后端放弃被清空的云端任务。"""
        jobs = self._cleared_jobs_backup.pop(self._current_tab, [])
        self._abandon_jobs(jobs)
        self._restore_clear_button()
        if jobs:
            # 撤回窗口已过 = 清空确认 → 这些任务的暂存副本可以回收
            self._sweep_staged_files("清空已确认")

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
        if jobs:
            # 撤回已取消 = 清空确认 → 回收这些任务的暂存副本
            self._sweep_staged_files("清空已确认")

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
            self._report_job_result(flat_jobs[idx], success, message)

    def _report_job_result(self, job, success: bool, message: str,
                           tab_key: str = "", owner_name: str = "",
                           is_admin_print: bool | None = None):
        """一个任务的收尾：标记完成 + 上报云端（成功/失败）。

        攒批模式要复用：第 2 趟打完时整叠的每个任务一起收尾，那时它们早已不在
        `self._flat_jobs` 里（那是另一次 worker 的列表），也不该按"当前前台标签页"
        猜归属 —— 所以归属/管理员标记由调用方显式传入（标签页可能已被删/改名）。
        """
        task_id = getattr(job, 'task_id', 0)
        if success:
            # 打印成功 → 标记已完成并立即持久化，异常退出重载后仍保留，退出时不再放弃
            job.sent = True
            self._save_config()
        if not task_id:
            # 本地单（来源=本地，没有云端 task_id）：它的 `sent` 是**打印启动那一刻**由
            # /api/local_orders 写下的（幂等：重复上报直接跳过）⇒ 失败必须单独回落，否则结算页
            # 把没打出来的单记「已完成」并计入收益（2026-10-23 现场：缺纸中断 + 攒批放弃整叠）。
            if not success:
                self._report_local_order_failed(job, message)
            return
        if not self._cloud_client:
            return
        if success:
            # 归属标记（v24）：云端订单为顾客订单，由打印它的管理员盖章（订单号右侧下拉选择）。
            # 2026-12：后台打印（无障碍）时 _printing_tab_key 指向实际标签页，不用前台标签页
            if not owner_name and is_admin_print is None:
                tab = self._config.tabs.get(
                    tab_key or getattr(self, "_printing_tab_key", "") or self._current_tab)
                if tab:
                    owner_name = (tab.owner_name or "").strip()
                    is_admin_print = bool(tab.is_admin_print)
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
            report_cfg["is_admin_print"] = bool(is_admin_print)
            self._cloud_client.report_success(task_id, report_cfg)
        else:
            self._cloud_client.report_fail(task_id, message)

    def _report_local_order_failed(self, job, message: str = ""):
        """本地单打印失败 → 把云端那笔「已完成」回落成「失败」（后台线程，不挡 GUI）。

        为什么必须有这条路（2026-10-23 现场，HN20261023-0013）：
          本地单的 `status='sent'` 是**打印启动那一刻**由 `/api/local_orders` 写下的
          （单据化 + 幂等：重复上报直接跳过），这条链路**没有失败分支** —— 缺纸中断、操作员
          在 Windows 里取消作业、攒批「放弃整叠」，订单在结算页仍是「已完成」，金额还进了收益。
          云端单不受影响：它们由 print_success / print_failed 按 task_id 回报。

        走向新接口 `/api/local_order_status`（只允许 failed，且只认本地单）：后端把父单与
        全部子任务一起改成 failed，人工订正过的终态（已取消/放弃打印/被打回）不覆盖。

        离线/取号失败时的 `-L` 临时号跳过（云端没有这笔单，换正式号后由同步流程补报；
        离线期间的失败目前**不会**补报——离线库没有状态列，这一点已知并记录在文档里）。
        """
        order_number = str(getattr(job, "order_number", "") or "").strip()
        client = self._cloud_client
        if not order_number or client is None:
            return
        upper = order_number.upper()
        if "-L" in upper or upper.startswith("LOCAL-"):
            return
        api_url = (client.api_url or "").rstrip("/")
        token = client.token or ""
        if not api_url or not token:
            return
        payload = {
            "order_number": order_number,
            "status": "failed",
            "reason": str(message or "")[:200],
            "client_id": getattr(client, "client_id", "") or "",
        }

        def _net():
            try:
                resp = http_requests.post(
                    f"{api_url}/api/local_order_status",
                    params={"token": token}, json=payload, timeout=10)
                try:
                    body = resp.json()
                except Exception:
                    body = {}
                if resp.ok and body.get("success"):
                    changed = body.get("changed", False)
                    self._cloudConnSyncLog.emit(
                        f"📋 本地订单 {order_number} → 失败"
                        f"（{str(message or '')[:40]}）"
                        + ("" if changed else "：云端已是终态/无需改动"))
                else:
                    self._cloudConnSyncLog.emit(
                        f"⚠️ 本地订单 {order_number} 失败状态回落未成功: "
                        f"{(body.get('message') if isinstance(body, dict) else '') or resp.text[:120]}")
            except Exception as e:
                self._cloudConnSyncLog.emit(
                    f"⚠️ 本地订单 {order_number} 失败状态回落异常: {e}")

        threading.Thread(target=_net, daemon=True).start()

    # ──────── 手动双面（无自动双面器的机型：程序自己分两趟 + 人工翻纸）────────

    def _printer_profile(self) -> PrinterProfile:
        """当前打印队列的档案（print_config.json 的 printer_profiles；没登记返回默认/未确认）。"""
        return self._config.get_profile(self._config.printer_name)

    # ── 双面能力：未确认就明说 + 给用户一个自己盖章的入口（2026-10-17 事故防呆）──

    def _warn_if_duplex_capability_unknown(self, force: bool = False):
        """当前队列的双面能力没盖章时，在日志里明确写出后果（同一队列同一状态只说一次）。

        为什么必须说出来：能力"未确认"时程序按**旧行为**把双面请求交给驱动，而
        HP LaserJet 1018/1020/1022 这类无自动双面器的机型**驱动会虚报支持**
        （实测本机「HP LaserJet 1020」USB005 队列报 DC_DUPLEX=1 / 接受 Duplex=2），
        实际弹出的是**驱动程序自带**的手动双面向导（「双面打印说明」：偶数页先打 →
        提示整叠搬回 → 再打奇数页）——2026-10-17 现场就是这样：单页文件先吐一张空白页
        再打正式页，而且**完全没走**程序自己的两趟控制器。日志里不说，用户永远不知道
        该去盖这个章。

        虚拟打印机（Print to PDF / XPS / OneNote / Adobe PDF…）不出纸，双面对它是空操作，
        不提示（否则天天刷日志，真问题会被淹掉）。切队列时会重判一次。
        """
        try:
            queue = (self._config.printer_name or "").strip()
            if not queue:
                return
            from pdf_printer import is_virtual_printer
            if is_virtual_printer(queue):
                return
            prof = self._printer_profile()
            if prof.auto_duplex is not None:
                self._duplex_warned = ""       # 盖章后复位：以后再回到未确认还能再提示
                return
            if not force and getattr(self, "_duplex_warned", "") == queue:
                return                          # 同一队列同一状态只说一次（切回来才再说）
            self._duplex_warned = queue
            self._log(f"⚠ 打印机「{queue}」的双面能力未确认：含双面文件的作业按驱动双面提交。"
                      f"若本机没有自动双面器（HP LaserJet 1018/1020/1022 这类驱动会虚报支持），"
                      f"实际走的是驱动程序自带的「双面打印说明」向导（偶数页先打 → 提示翻纸 → "
                      f"再打奇数页），单页文件还可能先吐一张空白页。"
                      f"请用「帮助 → 打印机双面能力…」盖章，改用程序自己的两趟控制器"
                      f"（全程单面、只翻一次纸、第 2 趟自动 180° 补偿）。")
        except Exception as e:      # 启动早期/半初始化的验收环境都要能安全调用
            logger.debug(f"双面能力提示失败（忽略）: {e}")

    def _on_printer_duplex_capability(self):
        """帮助 → 打印机双面能力…：给当前队列盖三态章 + 手动双面参数。

        人工结论（capability_source="manual"）**优先于自动探测且不被探测覆盖** ——
        这正是老机型（驱动虚报支持）唯一可靠的判据来源。此前只能敲命令行建档案，
        而档案又被旧版客户端的整体落盘清空过一次（根因见 printer_config.py 顶部），
        所以补上这个图形入口。
        """
        queue = (self._config.printer_name or "").strip()
        if not queue:
            QMessageBox.information(self, "打印机双面能力",
                                    "请先在顶栏选择一台打印机，再设置它的双面能力。")
            return
        dlg = PrinterDuplexCapabilityDialog(
            queue, self._printer_profile(),
            verified_by=(self._config.cloud_owner_name or ""), parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        if self._apply_duplex_capability(queue, dlg.values()):
            summary = self._config.get_profile(queue).summary()
            self._log(f"🖨 打印机双面能力已更新：{queue} → {summary}")
            QMessageBox.information(
                self, "打印机双面能力",
                f"「{queue}」的能力档案已保存：\n\n{summary}\n\n"
                f"人工结论不会被自动探测覆盖；双面作业按它决定走程序自己的两趟控制器"
                f"（人工翻纸）还是交给驱动一次成型。")

    def _apply_duplex_capability(self, queue: str, values: dict) -> bool:
        """把对话框结果写进该队列档案并落盘（读写都在 printer_config 层，GUI 不另开口子）。"""
        try:
            self._config.update_profile(queue, **values)
        except Exception as e:
            self._log(f"⚠ 打印能力档案写入失败：{type(e).__name__}: {e}")
            QMessageBox.warning(self, "打印机双面能力", f"档案写入失败：{e}")
            return False
        self._save_config()
        # 能力变了 ⇒ 攒批入口显隐、「双面（手动翻纸）」文案、无人值守挂起判据都要立刻跟变
        self._duplex_warned = ""
        try:
            self._set_stack_checkbox(self._stack_mode_on())
            self._sync_stack_button()
            self._update_duplex_label()
        except Exception as e:
            logger.debug(f"能力变更后的 UI 刷新失败（忽略）: {e}")
        return True

    def _manual_flip_required_for(self, jobs) -> bool:
        """这批任务是否"必须人工翻纸"：本机不支持自动双面 **且** 含双面文件。

        用于拦住无人值守路径（无障碍自动打印 / 预约到点）——那种场景没人翻纸，
        自动开打只会打出半成品（只印了第 1 趟）或静默单面。"""
        prof = self._printer_profile()
        if not (prof.auto_duplex is False and prof.manual_duplex):
            return False
        return any((getattr(j, "duplex", "off") or "off") == "on" for j in jobs)

    def _hold_for_manual_duplex(self, jobs, tab_key: str, reason: str) -> bool:
        """无人值守路径（**目前只剩「预约到点」**）遇到"需人工翻纸"的订单 → 挂起。

        返回 True = 已挂起（调用方必须直接 return，不要再去启动打印）。
        为什么不静默改单面：顾客按双面付了钱，改单面是少收费 + 多费纸；
        为什么不入重试队列：重试也没有人翻纸，只会每 10s 空转。改为在标签页亮 ⏸ 标记，
        订单保持未打印（不回报失败），等人工点「开始打印」。

        ⚠ **无障碍打印已不走这里**（2026-10-20 用户要求）：顾客勾了无障碍就是要「直接开始」，
        所以 `_process_auto_print_order` 会直接 accept + 打第 1 趟，翻纸窗照旧弹出等第 2 趟确认。
        别把这条再塞回无障碍路径。"""
        if not self._manual_flip_required_for(jobs):
            return False
        tab = self._config.tabs.get(tab_key)
        if tab is not None:
            tab.manual_duplex_pending = True
            self._save_config()
        self._log(f"⏸ {reason}：含双面文件，而当前打印机「{self._config.printer_name}」"
                  f"不支持自动双面（需人工翻纸）—— 本机已挂起（不回报失败），"
                  f"请人工点「开始打印」（第 1 趟打完后会弹窗提示翻纸）")
        self._refresh_tab_display()
        return True

    # ── 手动双面常驻翻纸窗（对齐 HP 官方：打印一开始就在，不是打完才弹）──

    def _ensure_duplex_dialog(self) -> ManualDuplexDialog:
        dlg = getattr(self, "_duplex_dialog", None)
        if dlg is None:
            dlg = ManualDuplexDialog(self)
            dlg.continue_requested.connect(self._on_duplex_continue)
            dlg.skip_requested.connect(self._on_duplex_skip)
            self._duplex_dialog = dlg
        return dlg

    def _show_duplex_dialog(self, dlg: ManualDuplexDialog):
        """把常驻窗立到最前：置顶 + 抢焦点 + 任务栏闪烁（否则操作员注意不到打印机在等着）。"""
        try:
            if dlg.isMinimized():
                dlg.showNormal()
            else:
                dlg.show()
            dlg.raise_()
            dlg.activateWindow()
            QApplication.alert(self, 0)          # 任务栏闪烁，叫回操作员
        except Exception:
            pass

    def _on_duplex_plan(self, sheets: int, estimate_sec: float):
        """批次开始播报「第 1 趟几张纸 / 预计多久」——常驻窗据此计时并按超时变色。"""
        self._duplex_plan_info = (int(sheets or 0), float(estimate_sec or 0.0))

    def _on_duplex_announce(self, text: str):
        """批次开始 → 常驻窗立刻出现（此后状态都在同一个窗口里切换，不再"等弹窗"）。"""
        dlg = self._ensure_duplex_dialog()
        sheets, est = getattr(self, "_duplex_plan_info", (0, 0.0))
        try:
            prof = self._printer_profile()
            dlg.announce(text, sheets=sheets, estimate_sec=est,
                         gate_ratio=prof.gate_ratio, gate_extra=prof.flip_ready_sec)
            dlg.set_speed_hint(prof.speed_summary())
        except Exception:
            dlg.announce(text, sheets=sheets, estimate_sec=est)
        self._show_duplex_dialog(dlg)

    def _on_duplex_speed_sample(self, sheets: int, seconds: float):
        """第 1 趟**实测**（程序侧权威值，与操作员按不按按钮无关）→ 两用：

        ① 常驻窗按它冻结计时（界面时钟只做兜底）；
        ② 写进本机打印队列档案自动学习（真实作业就是最好的样本，零额外用纸；
           异常样本由 add_speed_sample 内部丢弃）。

        ⚠ 这笔"实测"可能是**打印机提前回报的假数据**（主机型打印机只回报"数据送进端口"，
        不回报"纸打完"）：早于翻纸闸门（预估 × 放行比例 + 翻页准备时间；算出 <1 秒则不设闸门）的一律不采信 ——
        既不冻结计时（真实用时还在走，见 ManualDuplexDialog.is_early_signal），也不写进
        速度样本（写进去会把预估带塌到几秒，连带闸门自己一起失效：2026-10-18~19 现场）。"""
        dlg = getattr(self, "_duplex_dialog", None)
        try:
            early = bool(dlg is not None and dlg.is_early_signal(seconds))
        except Exception:
            early = False
        if early:
            self._log(f"  ⚠ 第 1 趟回报 {float(seconds):.1f}s（{int(sheets)} 张）早于预估闸门"
                      f"（预估 {format_duration(dlg._pass1_est)}）—— 打印机只回报「开始打印」，"
                      f"这笔实测不可信：不冻结计时、不写速度样本")
            return
        # ① 先用权威值冻结窗口计时（信号按发射顺序先于此处的 flip_required，所以窗口最终显示的就是它）
        try:
            if dlg is not None and not dlg._pass1_done:
                dlg.freeze_pass1(seconds)
        except Exception:
            pass
        # ② 学习
        try:
            prof = self._printer_profile()
            if not prof.add_speed_sample(sheets, seconds):
                self._log(f"  ⚠ 速度样本被丢弃（物理上不可能：{int(sheets)} 张 "
                          f"{float(seconds):.1f}s；打印机提前回报时就是这样）")
                return
            self._config.update_profile(self._config.printer_name,
                                        speed_samples=prof.speed_samples)
            self._save_config()
            a, b = prof.speed_fit()
            self._log(f"  📈 已记入速度样本（第 {len(prof.speed_samples)} 条）："
                      f"启动 ≈{a:.0f}s + 每张 ≈{b:.1f}s")
        except Exception as e:
            self._log(f"⚠ 速度样本写入失败（不影响打印）：{type(e).__name__}: {e}")

    def _speed_settings_block_reason(self) -> str:
        """打开「打印速度」窗 / 开始测速前的统一闸门（返回 "" = 可以）。

        两件事必须拦：① 打印机正忙（正在打）；② 出纸槽里还有没打第 2 趟的攒批纸叠 ——
        测速要打 11 张纸，插进那一叠会让整叠第 2 趟的逆序配对整体错位（铁律①）。
        """
        if self._worker is not None and self._worker.isRunning():
            return "打印机正忙"
        if self._stack_open_for_business():
            return "攒批未完成"
        return ""

    def _on_calibrate_speed(self):
        """帮助 → **打印速度…**：一个窗口里装下所有"第 1 趟耗时 / 翻纸放行"设置。

        2026-10-20 用户要求（原入口是"点一下就打 11 张纸的测速"）：
          ① 启动/预热时间、每张纸耗时 —— 机器量不准的分量允许人工填；
          ② 翻纸放行改成「预估 × 比例（默认 50%）+ 人工翻页准备时间」；
          ③ 原来那个两点测速收进同一个窗口，当它的一个按钮。
        方法名保持不变：`tests/verify_stack_gate_exclusive.py` ④ 直接调它验"纸叠未完成必须被拒"。
        """
        reason = self._speed_settings_block_reason()
        if reason == "打印机正忙":
            QMessageBox.information(self, "打印机正忙",
                                    "当前有打印任务在进行，请等它结束后再调整速度设置。")
            return
        if reason == "攒批未完成":
            # 🔴-3 旁路之三：测速会打出 11 张纸，插进未完成的攒批纸叠会让整叠乱序。
            # 窗口里的「打印测速」按钮走同一道闸门（`_start_speed_calibration`）。
            QMessageBox.information(
                self, "攒批未完成",
                "出纸槽里还有未完成的手动双面纸叠（第 2 趟还没打）。" + chr(10) + chr(10) +
                "测速会打出 11 张纸，插进这一叠会让整叠的逆序配对报废。" + chr(10) +
                "请先处理完纸叠（打完第 2 趟 / 放弃整叠）再测速。")
            self._log("🕐 测速被拒：出纸槽里有未完成的手动双面纸叠")
            return
        prof = self._printer_profile()
        dlg = PrintSpeedDialog(self, prof,
                               self._config.printer_name or "(系统默认打印机)")
        dlg.calibrate_requested.connect(self._start_speed_calibration)
        if dlg.exec() != QDialog.Accepted:
            return
        vals = dlg.values()
        try:
            self._config.update_profile(self._config.printer_name, **vals)
            self._save_config()
        except Exception as e:
            QMessageBox.warning(self, "打印速度",
                                f"保存失败：{type(e).__name__}: {e}")
            return
        a, b = self._printer_profile().speed_fit()
        ratio = int(round(vals["gate_ratio"] * 100))
        extra = vals["flip_ready_sec"]
        self._log(f"🕐 打印速度设置已保存：启动 {vals['speed_manual_a']:.0f}s"
                  f"（{'手动' if vals['speed_manual_a'] else '自动'}）+ 每张 "
                  f"{vals['speed_manual_b']:.1f}s"
                  f"（{'手动' if vals['speed_manual_b'] else '自动'}）"
                  f" → 当前模型 启动 ≈{a:.0f}s + 每张 ≈{b:.1f}s；"
                  f"翻纸放行 = 预估 {ratio}%"
                  + (f" + {extra:.0f}s" if extra else "")
                  + "（下一批打印生效）")

    def _start_speed_calibration(self):
        """「打印速度」窗里的「▶ 打印测速」：打 1 张 + 10 张空白页自动计时，写进本机档案。

        预估平时靠"每次真实打印自动采样"越用越准，但刚装好时没有样本；两点标定把这件事提前
        （11 张纸、单面、不需要人翻纸，远程也能做）。"""
        reason = self._speed_settings_block_reason()
        if reason == "打印机正忙":
            QMessageBox.information(self, "现在不能测速", "打印机正忙，等它结束后再测速。")
            return
        if reason == "攒批未完成":
            QMessageBox.information(
                self, "现在不能测速",
                "出纸槽里还有未完成的手动双面纸叠（第 2 趟还没打）——" + chr(10) +
                "测速会打出 11 张纸，插进这一叠会让整叠的逆序配对报废。" + chr(10) +
                "请先处理完纸叠再测速。")
            return
        cur = getattr(self, "_speed_cal_worker", None)
        if cur is not None and cur.isRunning():
            QMessageBox.information(self, "测速进行中", "上一次测速还没结束，请稍候。")
            return
        queue = self._config.printer_name or "(系统默认打印机)"
        reply = QMessageBox.question(
            self, "打印测速",
            f"将对「{queue}」打印 <b>1 张 + 10 张空白页</b>（单面，共 11 张纸；"
            f"空白页不费碳粉，也不需要翻纸）。" + chr(10) + chr(10) +
            f"用来测出「启动开销 + 每张纸耗时」，之后翻纸窗就能显示第 1 趟的预计耗时。" + chr(10) +
            f"请确认纸盒有纸，然后点「开始」。",
            QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Ok)
        if reply != QMessageBox.Ok:
            return
        w = SpeedCalibrationWorker(self._config.printer_name)
        w.log.connect(self._log)
        w.done.connect(self._on_speed_calibration_done)
        self._speed_cal_worker = w
        self._log(f"🕐 开始测速：{queue} —— 打 1 张 + 10 张空白页（单面）")
        w.start()

    def _on_speed_calibration_done(self, ok: bool, msg: str, samples: list):
        try:
            if ok and samples:
                prof = self._printer_profile()
                recorded = 0
                for item in samples:
                    try:
                        if prof.add_speed_sample(int(item[0]), float(item[1])):
                            recorded += 1
                    except (TypeError, ValueError, IndexError):
                        continue
                if recorded == 0:
                    # 别谎报成功：这台机器只回报"开始打印"，测出来的秒数小于物理下限，
                    # 一条也进不了档案（否则「已写进档案」是假话，用户以为标定过了）。
                    self._log(f"⚠ 测速数值被判为不可信（{msg}）—— 打印机的出纸回报早于实机"
                              f"出纸，未写入档案，仍用默认/既有模型")
                    QMessageBox.warning(
                        self, "测速结果不可信",
                        f"{msg}\n\n但这台打印机只回报「开始打印」就返回（主机型打印机"
                        f"不回报「纸打完」），量出来的秒数低于物理下限，一条都没写进档案。\n\n"
                        f"第 1 趟预估继续用默认/既有模型（翻纸按钮按它等到点才放行），没有副作用。")
                    return
                self._config.update_profile(self._config.printer_name,
                                            speed_samples=prof.speed_samples)
                self._save_config()
                a, b = prof.speed_fit()
                self._log(f"🕐 测速完成：启动 ≈{a:.0f}s + 每张 ≈{b:.1f}s（{prof.speed_summary()}）")
                QMessageBox.information(
                    self, "测速完成",
                    f"{msg}\n\n得到：启动 ≈{a:.0f} 秒 + 每张 ≈{b:.1f} 秒\n"
                    f"已写进「{self._config.printer_name}」的档案，翻纸窗按它预估第 1 趟耗时。")
            else:
                self._log(f"⚠ 测速失败：{msg}")
                QMessageBox.warning(self, "测速失败", msg or "未取得有效数据，请重试。")
        finally:
            self._speed_cal_worker = None

    def _on_duplex_finished(self, text: str):
        dlg = getattr(self, "_duplex_dialog", None)
        if dlg is not None:
            dlg.finish(text)

    def _on_flip_required(self, text: str, timeout_sec: int):
        """Worker 打完第 1 趟 → 常驻窗切到"可翻纸"（启用按钮 + 倒计时）。"""
        dlg = self._ensure_duplex_dialog()
        dlg.set_flippable(text, timeout_sec)
        self._show_duplex_dialog(dlg)

    def _on_duplex_continue(self):
        """单任务翻纸窗的「▶」：把"已翻纸"回执给 worker。

        ⚠ 这个窗**只管单任务**。攒批不走它：第 2 趟由「攒批列表」的
        「▶ 开始打印第 2 趟」→ `_on_stack_flush`（数纸对话框）发起。
        """
        if self._worker is not None:
            self._worker.confirm_flip(True)
        self._log("▶ 已确认翻纸，继续打第 2 趟")

    def _on_duplex_skip(self):
        """单任务：跳过本任务（攒批的「放弃整叠」在攒批列表上，不走这里）。"""
        if self._worker is not None:
            self._worker.confirm_flip(False)
        self._log("⏭ 已选择跳过：第 1 趟的纸是半成品，请人工处理")

    # ──────── 攒批模式（多任务合并翻纸，2026-10）────────
    #
    # 目标场景：无自动双面器的机型（HP LaserJet 1020）+ **大量小订单**。
    # 旧行为是"一个打印任务翻一次纸"，每个订单都要人跑到机器前；
    # 攒批模式把连续多个订单的第 1 趟连续打进同一叠（出纸槽），人工**一次性**确认后
    # 统一打第 2 趟 —— 一轮只翻一次纸。台账在 duplex_stack.py（纯逻辑）。
    #
    # 三条铁律（都在下面代码里落地，改这里前先读一遍）：
    # ① **出纸槽独占**：攒批期间除"攒批单的第 1 趟"以外的任何输出都会插进纸叠中间，
    #    让第 2 趟的逆序整体错位串页 ⇒ 其余打印请求一律排队（_park_print_request）。
    # ② **第 1 趟不算完成**：第 1 趟只是半成品，绝不置 job.sent / 绝不上报 print_success，
    #    否则后端与本地都会把半成品当成品（正确性 + 结算都错）。收尾统一在 _finish_stack_entries。
    # ③ **超时不自动跳过**：第 2 趟必须人把整叠搬回进纸盒才可能打；超时只变红预警。
    # ④ **每一张纸都要走第 2 趟**（2026-10-18 现场）：进纸盒里剩下的纸，**即使那一面是空白，
    #    也必须喂一遍**。绝不能"跳过空白槽位"来省纸 —— 跳过的那张纸会**留在进纸口**，
    #    下一次打印的第一张就是它（内容打到别人的纸上，且顾客的纸少了）。
    #    想少喂纸只有一条合法路径：操作员**真的把纸拿走**，并在「攒批队列」窗里登记
    #    「已取走」（`stack.toggle_taken`）—— 登记之后那张纸不再属于这一叠。

    def _stack_applicable(self) -> bool:
        """攒批模式对**当前打印机**是否适用：只对「不支持自动双面、双面需人工翻纸」的机型开放。

        自动双面的机器一次成型，多任务合并翻纸纯属多余 ⇒ 这类机型**不显示攒批入口**、也不接受
        开启（2026-10 用户要求）。判据 = 档案 auto_duplex is False（等价于 PrinterProfile.
        needs_manual_duplex；这里按属性直取，兼容验收脚本里那种只有 auto_duplex/manual_duplex
        两个属性的轻量桩）。与移动端设备列表同口径 —— 那边也只给 auto_duplex === false 的设备
        渲染攒批徽标与远程开关，服务端 _stack_needs_manual_flip 再拦一次。
        """
        try:
            return getattr(self._printer_profile(), "auto_duplex", None) is False
        except Exception as e:      # 定时器/半初始化的验收环境都要能安全调用
            logger.debug(f"攒批适用性判定失败（按不适用处理）: {e}")
            return False

    def _stack_inapplicable_reason(self) -> str:
        """攒批**为什么**不适用（给日志 / 弹窗 / 移动端回报的同一句口径）。

        「不适用」有两种完全不同的原因，旧写法糊成一句「支持自动双面（或双面能力未确认）」，
        读日志的人无从判断"该去盖章"还是"本来就无需求"。这里按档案三态如实分说：
          · auto_duplex is True  → 机器确实支持自动双面，攒批对它是多余的；
          · 其余（None / 读不到）→ 能力未确认，攒批只对已确认需人工翻纸的机型开放。
        """
        queue = (self._config.printer_name or "（系统默认打印机）")
        try:
            ad = getattr(self._printer_profile(), "auto_duplex", None)
        except Exception:
            ad = None
        if ad is True:
            return f"「{queue}」已确认支持自动双面（双面一次成型，不需要攒批）"
        return (f"「{queue}」的双面能力未确认（攒批只对「已确认不支持自动双面、"
                f"双面需人工翻纸」的机型开放；请先用「帮助 → 打印机双面能力…」盖章）")

    def _stack_mode_on(self) -> bool:
        """攒批模式**当前是否真的生效**：配置开着 + 当前打印机确实需要攒批。

        配置里的 duplex_stack_mode 只是用户的偏好（换回需人工翻纸的老机型时自动恢复），
        自动双面的机器上它既不显示也不生效（2026-10 用户要求）。
        """
        return bool(getattr(self._config, "duplex_stack_mode", False)) and self._stack_applicable()

    def _set_stack_checkbox(self, checked: bool):
        """同步/回滚攒批复选框的显示态（屏蔽信号，避免再触发一次切换）。"""
        chk = getattr(self, "_chk_stack_mode", None)
        if chk is None:
            return
        chk.blockSignals(True)
        chk.setChecked(bool(checked))
        chk.blockSignals(False)

    # ── 攒批模式：忙闲判定 + 云端上报 + 远程控制（2026-10）────────────────────────
    # 移动端可以看/改本机的「🖐 攒批模式」，硬前提是**打印机空闲**。
    # 忙 = 正在打印（普通批次 / 攒批第 1 趟 / 整叠第 2 趟）或 有活等在那儿
    # （出纸槽里还有没打第 2 趟的纸叠 / 攒批排队 / 自动打印补打队列）。
    # 本地勾选框与移动端下发走**同一道闸门**（_stack_busy_reason），两侧都拦、提示同样口径。

    def _stack_busy_reason(self, skip_entries: bool = False) -> str:
        """攒批模式能否改动的前置：返回 "" = 空闲可改；非空 = 忙的原因（给用户看）。

        全部 getattr 兜底 + 整体 try：本方法会被 2 秒定时器调用，而 GUI 验收脚本里存在
        「MainWindow 只初始化了一半」的场景 —— 不能因为缺一个属性就把异常抛进 Qt 事件循环。

        `skip_entries=True`：忽略"出纸槽里还有一叠等第 2 趟"这一条。只给**关闭**攒批模式
        用（那时会先弹三选确认：保留纸叠照样可以关模式）；其余调用方保持默认口径。
        """
        try:
            worker = getattr(self, "_worker", None)
            if worker is not None and worker.isRunning():
                return "正在打印"
            flush = getattr(self, "_stack_flush_worker", None)
            if flush is not None and flush.isRunning():
                return "正在打印整叠的第 2 趟"
            stack = getattr(self, "_stack", None)
            if (not skip_entries) and stack is not None and getattr(stack, "entries", None):
                return (f"出纸槽里有 {stack.total_sheets} 张纸还没打第 2 趟"
                        f"（{stack.count} 单，等人工翻纸）")
            wait = getattr(self, "_stack_wait", None) or []
            if wait:
                return f"有 {len(wait)} 个打印请求在排队等这一叠打完"
            entry_wait = getattr(self, "_stack_entry_wait", None) or []
            if entry_wait:
                return (f"有 {len(entry_wait)} 个攒批单在排队等上一单的第 1 趟打完"
                        f"（随后会自动接着打）")
            retry = getattr(self, "_auto_print_retry", None) or []
            if retry:
                return f"有 {len(retry)} 个订单在补打队列里等打印机空出来"
        except Exception as e:      # 定时器路径绝不能抛；异常时按"空闲"放行（设备端还会再自查）
            logger.debug(f"攒批忙闲判定失败（按空闲处理）: {e}")
        return ""

    def _report_stack_state(self, note: str = ""):
        """把本机攒批模式 + 忙闲报给云端（移动端据此显示状态、判断能否远程切换）。"""
        cc = getattr(self, "_cloud_client", None)
        if cc is None or not hasattr(cc, "report_stack_state"):
            return
        try:
            cc.report_stack_state(self._stack_mode_on(), bool(self._stack_busy_reason()), note)
        except Exception as e:
            logger.debug(f"上报攒批状态失败: {e}")

    def _stack_state_tick(self):
        """定时（2s）比对攒批模式 + 忙闲，**变化才上报** —— 移动端状态随之实时刷新。"""
        try:
            sig = (self._stack_mode_on(), self._stack_busy_reason())
            if sig == getattr(self, "_last_stack_sig", None):
                return
            self._last_stack_sig = sig
            self._report_stack_state()
        except Exception as e:      # 定时器路径绝不能抛
            logger.debug(f"攒批状态心跳失败: {e}")

    def _on_remote_stack_mode(self, enable: bool, by: str = ""):
        """（主线程）移动端下发的攒批模式切换。执行前**再自查一次**适用性与忙闲。

        适用性与服务端 `_stack_needs_manual_flip` 同口径：只对「不支持自动双面、需人工翻纸」的
        机型放行 —— 服务端镜像可能过期（用户刚把打印机换成自动双面的那台），设备端必须自己再拦一次。
        """
        want = bool(enable)
        if want and not self._stack_applicable():
            why = self._stack_inapplicable_reason()
            self._log(f"📱 移动端请求开启「🖐 攒批模式」被拒：{why}")
            self._report_stack_state(f"未执行：{why}")
            return
        busy = self._stack_busy_reason()
        if busy:
            self._log(f"📱 移动端请求{'开启' if want else '关闭'}「🖐 攒批模式」被拒："
                      f"打印机正忙（{busy}）")
            self._report_stack_state(f"打印机正忙（{busy}），未执行")
            return
        if want == self._stack_mode_on():
            self._log("📱 移动端请求的攒批模式与当前一致，无需改动")
            self._report_stack_state(f"状态未变（已是{'开启' if want else '关闭'}）")
            return
        who = f"（操作者：{by}）" if by else ""
        self._log(f"📱 移动端{'开启' if want else '关闭'}「🖐 攒批模式」{who}")
        self._apply_stack_mode(want, source="移动端")

    def _uses_manual_duplex_job(self, job) -> bool:
        """单个任务是否要"程序自己分两趟 + 人工翻纸"（与 PrintWorker._uses_manual_duplex 同口径）。

        单页硬闸门（2026-10-17 现场事故）：有效页数 == 1 ⇒ False —— 1 页文件双面没有
        意义（配对需要 2 页），进了手动双面批次/攒批单只会白配一张空白占位纸。它照旧
        按单面直打（攒批模式下若同单里还有真正的双面文件，仍随整单入叠，见
        `_print_stack_pass1`）。页数未知(0) 不当成 1 页（旧行为不变）。"""
        p = self._printer_profile()
        if self._job_is_single_page(job):
            return False
        return bool((getattr(job, "duplex", "off") or "off") == "on"
                    and p.auto_duplex is False and p.manual_duplex)

    def _stack_has_work(self) -> bool:
        """是否有纸叠正在累积 / 正在打第 2 趟（该状态下出纸槽被独占）。"""
        return bool(self._stack.entries) or self._stack_flush_worker is not None

    def _stack_busy(self) -> bool:
        return bool((self._worker is not None and self._worker.isRunning())
                    or (self._stack_flush_worker is not None
                        and self._stack_flush_worker.isRunning()))

    def _sync_stack_button(self):
        """刷新攒批控件的显隐/显示态（换打印机、开关切换、纸叠变化都要调）。

        · 复选框：**只对「不支持自动双面、需人工翻纸」的机型显示** —— 自动双面的机器不需要攒批
          （隐藏时显示态归零；配置里的偏好留着，换回需翻纸的机型自动恢复）；
        · 底部入口：**只在有纸叠（等第 2 趟的纸）时出现**，文字带 `N张/M单（已取走 R）`；
          空纸叠一律隐藏（2026-10-21 用户要求）—— 列表窗随模式常驻，再挂一个光秃秃的
          「🖐 攒批」只会被读成"第二个开关"，点了也只是把已经开着的窗抬到最前；
        · 攒批列表：模式开启时**常驻**（2026-10-20 用户要求，见 `_sync_stack_list_visible`）。
        """
        applicable = self._stack_applicable()
        chk = getattr(self, "_chk_stack_mode", None)
        if chk is not None:
            chk.setVisible(applicable)
            self._set_stack_checkbox(self._stack_mode_on())
        # 攒批列表跟着刷（开关切换 / 纸叠变化 / 取走与放回 / 排队变化都要立刻对上）——
        # 存在才刷；该不该出现由 _sync_stack_list_visible 决定（不抢焦点）。
        self._sync_stack_list_visible()
        btn = getattr(self, "_btn_stack", None)
        if btn is None:
            return
        stack = getattr(self, "_stack", None)
        entries = bool(getattr(stack, "entries", None)) if stack is not None else False
        if entries:
            present = _stack_present_sheet_count(stack)
            taken = max(0, _stack_original_sheet_count(stack) - present)
            btn.setText(f"🖐 攒批 {present}张/{stack.count}单"
                        + (f"（已取走 {taken}）" if taken else ""))
            btn.setVisible(True)
        else:
            # 空纸叠不占位：列表窗是常驻的，这个入口只在真有纸压进出纸槽时才需要
            btn.setText("🖐 攒批列表")
            btn.setVisible(False)

    def _on_stack_mode_toggled(self, checked: bool):
        """本地勾选框：先过**适用性**与**忙闲**两道闸门，再真正切换（2026-10）。

        适用性 = 当前打印机确实需要攒批（不支持自动双面）；不适用时复选框本就不显示，
        真被点到也只提示、绝不回写（与移动端下发、服务端 _stack_needs_manual_flip 同口径）。

        **关闭时若纸叠还没打第 2 趟**（2026-10-21 用户要求）：不再像旧版那样直接回滚 +
        一句"打印机正忙"，而是弹三选确认 ——「保留纸叠并关掉模式」/「放弃整叠（按失败收尾）」
        /「取消」。旧行为把"关模式"整件事堵死，用户根本没法选择；而一句忙提示也说不清
        "关掉会不会把订单弄失败"（事实是：保留就不失败，放弃才失败）。
        正在打印 / 有排队 / 有补打队列时仍然硬拦（那是真的忙，不是"有一叠在等翻纸"）。
        """
        want = bool(checked)
        if want and not self._stack_applicable():
            self._set_stack_checkbox(False)
            # 两种"不适用"的原因完全不同 —— 统一走 `_stack_inapplicable_reason()`
            # （与移动端被拒时的日志/回报同一句口径），不再把两种原因糊成一句。
            why = self._stack_inapplicable_reason()
            self._log(f"⚠ 攒批模式不适用：{why}")
            msg = (why + "\n\n攒批（多任务合并翻纸）只对「无自动双面器、双面需人工翻纸」"
                          "的机型有意义。")
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, "攒批模式", msg)
            return
        if want == self._stack_mode_on():
            return
        has_entries = bool(getattr(getattr(self, "_stack", None), "entries", None))
        if (not want) and has_entries:
            other = self._stack_busy_reason(skip_entries=True)
            if other:
                self._set_stack_checkbox(self._stack_mode_on())
                self._log(f"⚠ 打印机正忙（{other}）—— 空闲后才能关闭「🖐 攒批模式」")
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.information(self, "攒批模式",
                                        f"打印机正忙（{other}）。\n\n空闲后才能关闭攒批模式。")
                return
            choice = self._confirm_stack_close("关闭攒批模式")
            if choice == "":
                self._set_stack_checkbox(self._stack_mode_on())
                self._log("🖐 已取消关闭攒批模式 —— 纸叠原样保留，可以继续打第 2 趟")
                return
            # 先关模式再决定纸叠：关掉后新请求不会再入叠；不这样的话，下面"放弃整叠"
            # 顺手放行排队请求时，那单会跟着进一叠新模式已经关掉的纸叠。
            self._apply_stack_mode(False, source="本地")
            if choice == "abandon":
                self._on_stack_abandon(interactive=False)
            else:
                self._log(f"🖐 攒批模式已关闭，但纸叠保留（{self._stack.count} 单 / "
                          f"{self._stack_present_sheets()} 张纸仍未打第 2 趟）——"
                          f"列表里随时可以继续")
            return
        busy = self._stack_busy_reason()
        if busy:
            # 回滚勾选（不触发信号）并提示原因 —— 与移动端下发同口径的拦截
            self._set_stack_checkbox(self._stack_mode_on())
            verb = "开启" if want else "关闭"
            self._log(f"⚠ 打印机正忙（{busy}）—— 空闲后才能{verb}「🖐 攒批模式」")
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, "攒批模式",
                                    f"打印机正忙（{busy}）。\n\n空闲后才能{verb}攒批模式。")
            return
        self._apply_stack_mode(want, source="本地")

    def _apply_stack_mode(self, enable: bool, source: str = "本地"):
        """真正切换攒批模式（本地勾选框与移动端指令共用）：落盘 + 同步 UI + 上报云端。

        调用方已保证 `_stack_applicable()`（当前机型确实需要攒批）—— 本方法只管落盘与显示。
        """
        enable = bool(enable)
        self._config.duplex_stack_mode = enable
        self._save_config()
        self._set_stack_checkbox(enable)
        # 开关是**明示**操作：允许列表重新出现（操作员之前的"收起"到此为止）
        self._stack_list_user_closed = False
        if enable:
            self._log("🖐 攒批模式已开启：需手动双面的打印请求不再各自翻纸，"
                      "第 1 趟连续堆进出纸槽，人工一次性确认后打整叠第 2 趟")
            prof = self._printer_profile()
            if not getattr(prof, "manual_duplex", False):
                self._log(f"⚠ 打印机「{self._config.printer_name}」已确认不支持自动双面，"
                          f"但还没有手动双面档案（两趟页序）——攒批对它暂不生效"
                          f"（普通打印照旧；请先用实机校准建好该队列的手动双面参数）")
            self._log(f"   出纸槽容量阈值 {self._stack.capacity} 张，"
                      f"第 2 趟等待 {self._stack.warn_after_sec // 60} 分钟后变红预警（不自动跳过）")
        else:
            self._log("🖐 攒批模式已关闭（普通打印照旧）")
            if self._stack.entries:
                self._log(f"⚠ 但出纸槽里还有 {self._stack.total_sheets} 张纸的攒批未打第 2 趟 ——"
                          f"在打完/放弃这一叠之前，其它打印请求仍会排队")
        self._sync_stack_button()
        self._refresh_stack_dialog()
        # 立刻上报 + 刷新变化基线（不等下一次 tick）
        self._last_stack_sig = (self._stack_mode_on(), self._stack_busy_reason())
        self._report_stack_state(
            f"攒批模式已{'开启' if enable else '关闭'}"
            + ("（来自移动端）" if source == "移动端" else ""))

    def _on_stack_button(self):
        """点底部「🖐 攒批 N张/M单」：叫出**攒批列表**（常驻窗；模式开启时它本就在）。

        列表左侧＝已攒的文件（第 1 趟已打、等第 2 趟），右侧＝还在排队的文件 —— 见
        `StackQueueDialog`。**第 2 趟也从这个列表发起**（底部「▶ 开始打印第 2 趟」→
        数纸确认 → 打整叠；「⏭ 放弃整叠」同处），攒批不再复用「手动双面」窗。

        入口**只在有纸叠时出现**（2026-10-21 用户要求，`_sync_stack_button`），所以从界面
        点进来的这一刻一定有纸；它此刻的实质作用就两件：① 把操作员**主动收起**过的列表
        重新叫出来（`_stack_list_user_closed` 清零，否则不再自动弹回）；② 在日志里报一句
        「几单 / 第 2 趟要送多少张」。下面的空纸叠分支是防御（理论上点不到）。
        """
        if self._stack_busy():
            # 第 1 趟/整叠第 2 趟正在打：**日志要说清正在打哪一趟**（现场据此判断
            # "还要等多久 / 现在能不能动纸"），列表照样打开（按钮由 render 置灰）。
            _flushing = (self._stack_flush_worker is not None
                         and self._stack_flush_worker.isRunning())
            self._log("🖐 打印机正在打"
                      + ("整叠第 2 趟" if _flushing else "某一单的第 1 趟")
                      + "，打完再决定下一步（列表里可看进度，按钮已置灰）")
        dlg = self._ensure_stack_queue_dialog()
        self._stack_list_user_closed = False     # 明示点入口 ⇒ 让它出现
        self._refresh_stack_queue()
        self._show_stack_queue_dialog(dlg)
        if not self._stack.entries:
            # 防御分支：入口在空纸叠时已隐藏（2026-10-21），正常点不到这里 —— 真被程序内部
            # 调到也照旧把列表打开（右栏「等待打印」还有内容可看）+ 一条轻提示，
            # 绝不静默什么都不做。
            note = ("🖐 攒批列表：当前没有已攒的纸叠（攒批模式：已开启）"
                    if self._stack_mode_on() else
                    "🖐 攒批列表：当前没有已攒的纸叠（攒批模式未开启）")
            self._log(note)
            bar = getattr(self, "_status_bar", None)
            if bar is not None:
                # showMessage 只**临时覆盖**左侧普通部件，不动 `_status_label` 的进度文案
                bar.showMessage(note, 4000)
            return
        self._log(f"🖐 攒批列表：{self._stack.count} 单 / 第 2 趟要送 "
                  f"{self._stack_present_sheets()} 张纸"
                  + (f"（第 1 趟共 {self._stack_original_sheets()} 张，"
                     f"已取走 {self._stack_taken_sheets()} 张）"
                     if self._stack_taken_sheets() else ""))

    def _stack_detail_text(self) -> str:
        present = self._stack_present_sheets()
        original = self._stack_original_sheets()
        taken = max(0, original - present)
        lines = []
        if present <= 0:
            lines.append("✅ 第 2 趟无内容：这一叠全部是单面成品（第 1 趟已打完），"
                         "点「开始第 2 趟」不会出纸 —— 直接按成品收尾。")
        else:
            lines.append(f"出纸槽里应有 {present} 张纸"
                         f"（{self._stack.count} 个打印任务的第 1 趟"
                         + (f"；第 1 趟共 {original} 张，已取走 {taken} 张" if taken else "")
                         + "）：")
        lines.extend(self._stack.describe_lines())
        lines.append("")
        lines.append("翻纸三步（原文来自 HP 驱动「双面打印说明」弹窗）：")
        lines.append("  1. 不改变方向，从出纸槽中取出整叠纸（不要丢弃空白页面）。")
        lines.append("  2. 保持相同方向，把整叠插入纸盘 2，使已打印面朝下（不翻面、不重排）。")
        lines.append("  3. 点「开始第 2 趟」——程序会先要你核对张数，再按各单的逆序打完整叠。")
        lines.append("⚠ 进纸盒里的纸必须与上面登记的张数一致：少一张/多一张都会让整叠错位"
                     "（后面每一张的背面都会串到别的页上）。")
        if self._stack.tainted:
            lines.append("")
            lines.append(f"⚠ 本叠存疑：{self._stack.taint_reason}")
        if self._stack_wait:
            lines.append("")
            lines.append(f"⏸ 另有 {len(self._stack_wait)} 个打印请求在排队等这一叠打完：")
            for req in self._stack_wait[:5]:
                lines.append(f"  · {req.get('reason', '')}")
        return "\n".join(lines)

    def _new_stack_batch(self) -> str:
        """开一批新纸叠目录（清单 + 各单合并 PDF），并清理上一批的残留。"""
        root = paths.duplex_stack_dir()
        batch_id = self._stack.new_batch()
        bdir = duplex_stack.batch_dir(root, batch_id)
        os.makedirs(bdir, exist_ok=True)
        self._stack.printer_name = self._config.printer_name
        prof = self._printer_profile()
        self._stack.first = prof.first or manual_duplex.FIRST_ODD
        self._stack.reverse_second = bool(prof.reverse_second)
        # 容量/预警阈值也按当前档案刷新：一叠一个新批次，改档案后新批次即生效
        self._stack.capacity = duplex_stack.clamp_capacity(prof.stack_capacity)
        self._stack.warn_after_sec = duplex_stack.clamp_warn_after(prof.stack_warn_after_sec)
        duplex_stack.save_manifest(bdir, self._stack)
        return bdir

    def _stack_batch_dir(self) -> str:
        if not self._stack.batch_id:
            return ""
        return duplex_stack.batch_dir(paths.duplex_stack_dir(), self._stack.batch_id)

    def _park_print_request(self, flat_jobs: list, tab_key: str | None, reason: str) -> bool:
        """把一次打印请求排队，等当前纸叠打完再放行（出纸槽独占，见铁律①）。"""
        if not flat_jobs:
            return True
        self._stack_wait.append({
            "jobs": list(flat_jobs),
            "tab_key": tab_key or "",
            "reason": reason,
        })
        names = "、".join((j.display_name or os.path.basename(j.file_path or ""))
                          for j in flat_jobs[:3])
        more = f" 等 {len(flat_jobs)} 个文件" if len(flat_jobs) > 3 else ""
        self._log(f"⏸ 已排队（等当前纸叠打完第 2 趟）：{reason} —— {names}{more}")
        self._sync_stack_button()
        self._refresh_stack_dialog()
        return True

    def _drain_stack_wait_queue(self):
        """纸叠打完（或放弃）后，把排队的请求依次放行。

        只放到"纸叠重新开始累积"或队列空为止：一旦又收了一单进叠，
        出纸槽再次被独占，后面的继续排队（容量/方向也在这里再次判定）。
        """
        guard = 0
        while self._stack_wait and not self._stack_busy() and guard < 200:
            guard += 1
            req = self._stack_wait[0]
            if not self._can_start_request_now(req.get("jobs") or []):
                break
            self._stack_wait.pop(0)
            jobs = [j for j in (req.get("jobs") or []) if not getattr(j, "sent", False)]
            if not jobs:
                continue
            self._log(f"▶ 纸叠已处理完，放行排队请求（{len(jobs)} 个文件）")
            if not self._start_print_worker(list(jobs), tab_key=req.get("tab_key") or None):
                # 启动失败（如打印机忙/取号中）→ 放回队首，下次再试
                self._stack_wait.insert(0, req)
                break

    def _can_start_request_now(self, jobs: list) -> bool:
        """这批请求现在能不能开打（不实际启动，只看纸叠准入）。

        空叠永远能开；非空叠只有"需手动双面且装订方向一致且容量够"的单能接着堆，
        其余一律继续排队。
        """
        if not jobs:
            return True
        needs_flip = any(self._uses_manual_duplex_job(j) for j in jobs)
        if not needs_flip:
            return self._can_start_request_now_bypass(jobs)
        return self._admit_stack_entry(jobs)[0]

    def _can_start_request_now_bypass(self, jobs: list) -> bool:
        """不需手动双面的请求（单面/自动双面）：纸叠非空就不能放行。

        空叠时它们照旧直接打（正常路径）；一旦纸叠开始累积，出纸槽被这一叠独占，
        它们的纸会插进纸叠中间把第 2 趟的逆序整体顶错（铁律①）。
        """
        return not self._stack.entries

    def _request_rotate(self, jobs: list) -> tuple[bool, bool, str]:
        """本请求的第 2 趟旋转补偿 → (是否可用, 旋转值, 不可用原因)。

        ⚠ 别把"旋转值"当成功标志用：`rotate=False`（短边不补偿）是**合法值**，
        拿它做 `if not ok` 判断会把短边单误判成"不可用"并带着空原因去排队。
        同一请求里混排长短边 → 不可用（一叠只能有一种补偿）。
        """
        duplex_jobs = [j for j in jobs if self._uses_manual_duplex_job(j)]
        if not duplex_jobs:
            return True, True, ""
        vals = {self._rotate_for_job(j) for j in duplex_jobs}
        if len(vals) > 1:
            return False, False, "同一单里混排了长边/短边装订，攒批时只能有一种旋转补偿"
        return True, bool(next(iter(vals))), ""

    def _rotate_for_job(self, job) -> bool:
        """本任务第 2 趟要不要旋转 180°：纸路径实测值 × 装订方向（与 worker 同口径）。"""
        p = self._printer_profile()
        return manual_duplex.rotate_second_for(
            p.second_rotate_180 if p else True,
            getattr(job, "duplex_mode", "") or self._config.duplex_mode)

    def _admit_stack_entry(self, jobs: list) -> tuple[bool, str, int, bool]:
        """判断这批请求能不能作为新的一单收进当前纸叠。

        返回 (能否, 原因, 估算张数, 是否有页数未知的文件)。
        """
        ok_rot, rotate, rot_reason = self._request_rotate(jobs)
        if not ok_rot:
            return False, rot_reason, 0, False
        sheets, unknown = duplex_stack.estimate_jobs_sheets(jobs, _count_pages_in_range)
        adm = self._stack.admit(sheets, rotate, unknown)
        if adm.ok:
            return True, "", sheets, unknown
        return False, adm.message(self._stack), sheets, unknown

    def _start_stack_entry(self, flat_jobs: list, tab_key: str) -> bool:
        """把这一批任务作为**新的一单**打进纸叠（只打第 1 趟）。

        打印机还在打上一单的第 1 趟时，这一单**排队**而不是被拒（2026-10-21 现场 bug）：
        攒批模式的本意就是"多个任务的第 1 趟连续堆进同一叠"，操作员在上一单还没出完纸时
        点第二单是完全正常的动作。旧写法在这里被 `_start_print_worker` 的忙检查硬拒
        （「已有打印任务正在进行」），而且**在忙检查之前**就覆盖/清空了
        `_stack_pending_ctx` —— 上一单打完时主窗口会把它误判成"普通打印批次"，报出
        「全部 0 个任务已完成打印并回报成功」。现在：忙 → 入 `_stack_entry_wait`，
        上一单第 1 趟一结束由 `_drain_stack_entry_queue` 自动接着打。
        """
        ok_rot, rotate, rot_reason = self._request_rotate(flat_jobs)
        if not ok_rot:
            return self._park_print_request(flat_jobs, tab_key, rot_reason)
        if self._stack_flush_worker is not None and self._stack_flush_worker.isRunning():
            # 整叠第 2 趟在飞：出纸槽与进纸盒都被整叠独占（`_stack_gate` 一般已拦，
            # 这里兜底）→ 等整叠打完再放行，不能插进去。
            return self._park_print_request(
                flat_jobs, tab_key, "整叠第 2 趟正在打——打完整叠再排")
        busy_worker = self._worker is not None and self._worker.isRunning()
        if busy_worker or self._print_starting:
            return self._queue_stack_entry(
                flat_jobs, tab_key,
                "上一单的第 1 趟还在打" if busy_worker else "正在启动上一单的打印")
        return self._begin_stack_entry(flat_jobs, tab_key, rotate)

    def _queue_stack_entry(self, flat_jobs: list, tab_key: str, reason: str) -> bool:
        """打印机正忙时的"新的一单"：先受理排队，第 1 趟空出来立刻接着打（不丢请求）。"""
        if not flat_jobs:
            return True
        if not self._has_members():
            self._show_no_member_hint()
            return False
        key = tab_key or ""
        for req in self._stack_entry_wait:
            if (req.get("tab_key") or "") == key:
                # 同一标签页重复点：只更新文件清单，不排第二份（否则同一单打两遍）
                req["jobs"] = list(flat_jobs)
                self._log("⏸ 这一单已在排队，等上一单第 1 趟打完自动接着打"
                          "（已按当前文件清单更新，不用重复点）")
                self._refresh_stack_dialog()
                return True
        self._stack_entry_wait.append({
            "jobs": list(flat_jobs),
            "tab_key": key,
            "reason": f"{reason}，打完自动接着打",
        })
        names = "、".join((j.display_name or os.path.basename(j.file_path or ""))
                          for j in flat_jobs[:3])
        more = f" 等 {len(flat_jobs)} 个文件" if len(flat_jobs) > 3 else ""
        self._log(f"⏸ 攒批排队：{reason} —— 本单等它打完自动接着打（不用重复点）："
                  f"{names}{more}")
        self._sync_stack_button()
        self._refresh_stack_dialog()
        return True

    def _drain_stack_entry_queue(self):
        """上一单第 1 趟结束、打印机空出来的瞬间：把排队等着的新单接着打。

        只放到"又开始打"或队列空为止（`_stack_busy()` 变 True 即停）。容量/装订方向
        在这里**重新判一次**：排队期间纸叠可能又长高了，放不下的转进 `_stack_wait`
        （等整叠第 2 趟打完再放行），绝不硬塞进叠里。
        """
        guard = 0
        while self._stack_entry_wait and not self._stack_busy() and guard < 50:
            guard += 1
            req = self._stack_entry_wait.pop(0)
            jobs = [j for j in (req.get("jobs") or []) if not getattr(j, "sent", False)]
            # 排队期间操作员可能把文件从标签页里移除了（这一单还没冻结）—— 移除即"别打它"，
            # 不能在标签页里看不见了还照旧印出来（打印前是唯一能收敛的时机）。
            tab = self._config.tabs.get(req.get("tab_key") or "")
            if tab is not None:
                alive = {id(j) for j in (getattr(tab, "jobs", None) or [])}
                kept = [j for j in jobs if id(j) in alive]
                if len(kept) != len(jobs):
                    self._log(f"⏸ 排队的攒批单里有 {len(jobs) - len(kept)} 个文件"
                              f"已从标签页移除，不再打印")
                jobs = kept
            if not jobs:
                continue
            ok, reason, _sheets, _unknown = self._admit_stack_entry(jobs)
            if not ok:
                self._park_print_request(jobs, req.get("tab_key") or None, reason)
                continue
            self._log(f"▶ 上一单第 1 趟结束，接着打排队的攒批单（{len(jobs)} 个文件）")
            if not self._begin_stack_entry(jobs, req.get("tab_key") or "", self._request_rotate(jobs)[1]):
                self._log("⚠ 排队的攒批单启动失败，已移出队列"
                          "（文件还在对应标签页，可重新点「开始打印」）")
                break

    def _begin_stack_entry(self, flat_jobs: list, tab_key: str, rotate: bool) -> bool:
        """真正启动一单第 1 趟（`_stack_pending_ctx` 只在这里写，失败就地回滚）。"""
        batch_dir = self._stack_batch_dir() or self._new_stack_batch()
        seq = self._stack.next_seq()
        out_pdf = os.path.join(batch_dir, f"entry_{seq:04d}.pdf")

        # 归属/管理员标记在开打前定格（标签页之后可能被删/改名，第 2 趟收尾时要原样回报）
        tab = self._config.tabs.get(tab_key or self._current_tab)
        self._stack_pending_ctx = {
            "tab_key": tab_key or self._current_tab or "",
            "owner_name": (tab.owner_name or "").strip() if tab else "",
            "is_admin_print": bool(tab.is_admin_print) if tab else False,
            "order_id": next((int(getattr(j, "order_id", 0) or 0) for j in flat_jobs
                              if getattr(j, "order_id", 0)), 0),
            "order_number": next((getattr(j, "order_number", "") for j in flat_jobs
                                  if getattr(j, "order_number", "")), ""),
        }
        self._log(f"🖐 攒批：本单作为第 {seq} 单开打第 1 趟"
                  f"（第 1 趟{'旋转 180° 补偿' if rotate else '不旋转'}；"
                  f"本叠已有 {self._stack.total_sheets}/{self._stack.capacity} 张）")

        if not self._start_print_worker(flat_jobs, tab_key=tab_key,
                                        stack_entry=(seq, out_pdf, rotate)):
            self._stack_pending_ctx = {}
            return False
        return True

    def _on_stack_entry_planned(self, payload: dict):
        """worker 排好本单页序、即将开打 → 落进纸叠清单并写盘（**打印前**落盘）。"""
        payload = dict(payload or {})
        seq = int(payload.get("seq") or 0)
        ctx = getattr(self, "_stack_pending_ctx", {}) or {}
        items = payload.get("items") or []
        title = ctx.get("order_number") or ""
        if not title:
            names = [it.get("display_name") or "" for it in items[:2]]
            title = "、".join([n for n in names if n]) or f"{len(items)} 个文件"
        if len(items) > 2:
            title += f" 等 {len(items)} 个文件"
        entry = duplex_stack.StackEntry(
            seq=seq,
            title=title,
            tab_key=ctx.get("tab_key", ""),
            order_id=int(ctx.get("order_id") or 0),
            order_number=ctx.get("order_number", ""),
            page_count=int(payload.get("page_count") or 0),
            sheet_count=int(payload.get("sheet_count") or 0),
            state=payload.get("state") or duplex_stack.STATE_PRINTING,
            rotate=bool(payload.get("rotate", True)),
            created_at=duplex_stack.now_text(),
            pdf=payload.get("pdf", ""),
            first=str(payload.get("first") or duplex_stack.manual_duplex.FIRST_ODD),
            file_plans=list(payload.get("file_plans") or []),
            pass1_pages=list(payload.get("pass1_pages") or []),
            pass2_pages=list(payload.get("pass2_pages") or []),
            tasks=[{
                "task_id": int(it.get("task_id") or 0),
                "order_id": int(it.get("order_id") or 0),
                "owner_name": ctx.get("owner_name", ""),
                "is_admin_print": bool(ctx.get("is_admin_print", False)),
                "copies": int(it.get("copies") or 1),
                "duplex": it.get("duplex") or "off",
                "page_range": it.get("page_range") or "",
                "page_count": int(it.get("page_count") or 0),
                "display_name": it.get("display_name") or "",
            } for it in items],
            job_keys=[(it.get("task_id") or 0, it.get("file_path") or "") for it in items],
        )
        # 第 1 趟实际打出的**原始**张数（含之后被操作员取走的）：入叠这一刻定格。
        # 之后 toggle_taken 会把 sheet_count 收缩成"当前在叠张数"，T/R 对账口径就靠它
        # （账本的 original_sheet_value() 缺省回落 sheet_count，所以要在这里写实）。
        try:
            entry.original_sheet_count = int(payload.get("sheet_count") or 0)
        except (AttributeError, TypeError):
            pass
        self._stack.add(entry)
        duplex_stack.save_manifest(self._stack_batch_dir(), self._stack)
        self._log(f"🖐 攒批第 {seq} 单已入叠：{title} —— {entry.sheet_count} 张纸"
                  f"（整叠 {self._stack.total_sheets}/{self._stack.capacity} 张 / "
                  f"{self._stack.count} 单）")
        # 有新单入叠 ⇒ 纸叠状态实质变了，让列表重新露面（之前"主动收起"到此为止）
        self._stack_list_user_closed = False
        self._sync_stack_button()
        # 立刻给后端发一次保活：本单可能已经被 accept，3 分钟反馈窗口现在就要开始顺延
        self._tick_keepalive(force=True)

    def _on_stack_entry_done(self, seq: int, ok: bool, message: str, sheets: int):
        """本单第 1 趟结束（成功/失败）。失败时整叠页序存疑，只标记、不自动放弃。"""
        entry = self._stack.find(seq)
        if entry is not None:
            entry.state = duplex_stack.STATE_DONE if ok else duplex_stack.STATE_FAILED
            if not ok:
                entry.note = message
                self._stack.taint(f"第 {seq} 单（{entry.title}）第 1 趟未正常完成：{message}")
            duplex_stack.save_manifest(self._stack_batch_dir(), self._stack)
        self._log(f"  {'✓' if ok else '✗'} 攒批第 {seq} 单第 1 趟：{message}")
        if not ok:
            self._log("⚠ 出纸槽里的纸状态未知（可能只出了一部分）——"
                      "请人工核对后再决定「开始第 2 趟」还是「放弃整叠」")
        self._sync_stack_button()
        self._refresh_stack_dialog()

    def _refresh_stack_dialog(self):
        """按当前纸叠状态刷新**攒批列表**（切第 1 趟/第 2 趟结束后都走这里）。"""
        self._refresh_stack_queue()

    def _on_stack_announce(self, text: str):
        """接住 worker 的「本单第 1 趟开打」播报 —— **只刷列表 + 记日志，不弹窗、不计时**。

        2026-10-20 用户要求：攒批第 1 趟不该再借「手动双面」那个窗（它一出现就在跑第 1 趟
        计时/预估），也不该弹出来打断操作。第 1 趟的进度在主窗口进度条与攒批列表里看；
        第 2 趟由列表的「▶ 开始打印第 2 趟」+ 数纸对话框发起。
        """
        try:
            self._log(f"🖐 攒批第 1 趟开打：{text}")
        except Exception:
            pass
        self._refresh_stack_queue()

    # ── 攒批队列窗（账本可视化 / 撤走-放回 / 落机对账）────────────────────────────

    def _stack_present_sheets(self) -> int:
        """当前在叠张数（= 第 2 趟要送的纸数）。"""
        return _stack_present_sheet_count(getattr(self, "_stack", None))

    def _stack_original_sheets(self) -> int:
        """第 1 趟实际打出的原始张数（含之后被取走的）。"""
        return _stack_original_sheet_count(getattr(self, "_stack", None))

    def _stack_taken_sheets(self) -> int:
        """已被操作员取走、不再计入第 2 趟的张数（T − M）。"""
        return max(0, self._stack_original_sheets() - self._stack_present_sheets())

    def _stack_active_count(self) -> int:
        """还有在叠文件计划的单数（整单被取走的不算）。"""
        fn = getattr(self._stack, "active_entries", None)
        if callable(fn):
            try:
                return len(fn())
            except Exception:
                pass
        return self._stack.count

    def _stack_second_pass_slots(self) -> int:
        """第 2 趟实际要送的纸数（撤走项已被账本剔除）。算不出来时 -1（当"未知"）。"""
        try:
            return len(self._stack.build_plan().pass_slots(2))
        except Exception as e:
            logger.debug(f"攒批第 2 趟槽位计算失败（按未知处理）: {e}")
            return -1

    def _ensure_stack_queue_dialog(self) -> StackQueueDialog:
        dlg = getattr(self, "_stack_queue_dialog", None)
        if dlg is None:
            dlg = StackQueueDialog(self)
            dlg.toggle_requested.connect(self._on_stack_taken_toggle)
            dlg.start_second_requested.connect(self._on_stack_flush)
            # 用 lambda 转发：_on_stack_abandon 多了一个 interactive 参数（默认 True），
            # 零参信号直接连到带默认值的方法上虽然多数情况能跑，但别依赖 PySide 的签名推断。
            dlg.abandon_requested.connect(lambda *_: self._on_stack_abandon())
            dlg.refresh_requested.connect(self._refresh_stack_queue)
            dlg.close_requested.connect(self._on_stack_list_close_requested)
            self._stack_queue_dialog = dlg
        return dlg

    def _show_stack_queue_dialog(self, dlg: StackQueueDialog, activate: bool = True):
        """把攒批列表立到最前（非模态：不挡主界面，操作员可以一边看一边搬纸）。

        activate=False：只让它出现，不抢焦点/不抬到最前（攒批模式开启时**常驻**自动出现
        用这个 —— 不能因为开个开关就把正在编辑的表格抢走焦点）。
        """
        try:
            if dlg.isMinimized():
                dlg.showNormal()
            else:
                dlg.show()
            if activate:
                dlg.raise_()
                dlg.activateWindow()
        except Exception:
            pass

    def _stack_now_printing_text(self) -> str:
        """列表标题里那句「现在在打什么」（攒批列表常驻，操作员一眼要看到进度）。"""
        try:
            flush = getattr(self, "_stack_flush_worker", None)
            if flush is not None and flush.isRunning():
                return "正在打整叠第 2 趟"
            w = getattr(self, "_worker", None)
            if w is not None and w.isRunning() and getattr(w, "_stack_mode", False):
                n = len(getattr(self, "_stack_entry_wait", None) or [])
                return ("正在打某单的第 1 趟"
                        + (f"（后面还有 {n} 单等着接着打）" if n else ""))
            if getattr(self, "_stack_entry_wait", None):
                return f"{len(self._stack_entry_wait)} 个请求等上一单第 1 趟打完接着打"
            if getattr(self, "_stack_wait", None):
                return f"{len(self._stack_wait)} 个请求在排队等这一叠打完"
        except Exception as e:      # 定时器/半初始化环境都要能安全调用
            logger.debug(f"攒批列表进度文案失败: {e}")
        return ""

    def _refresh_stack_queue(self):
        """刷新常驻攒批列表（存在才刷；**不**抢焦点、不强制弹出）。"""
        dlg = getattr(self, "_stack_queue_dialog", None)
        if dlg is None:
            return
        try:
            # 右栏「等待打印」两来源：① 等整叠第 2 趟打完才放行的 `_stack_wait`；
            # ② 已接单、等上一单第 1 趟一结束就自动接着打的 `_stack_entry_wait`。
            wait_requests = (list(getattr(self, "_stack_entry_wait", []) or [])
                             + list(getattr(self, "_stack_wait", []) or []))
            dlg.render(self._stack, busy=self._stack_busy(),
                       wait_requests=wait_requests,
                       printing_text=self._stack_now_printing_text())
        except Exception as e:      # 定时器/半初始化环境都要能安全调用
            logger.debug(f"攒批列表刷新失败: {type(e).__name__}: {e}")

    def _sync_stack_list_visible(self):
        """攒批模式开启 ⇒ 攒批列表**常驻**（2026-10-20 用户要求）；关闭 ⇒ 收起。

        两条额外口径（2026-10-21 用户提问后补）：
          · **只要还有没打第 2 趟的纸叠，窗口就必须在场** —— 即使攒批模式刚被关掉
            （纸还在出纸槽里、订单还是半成品，让人看不见只会被彻底忘掉）；
          · 操作员**主动**关掉的窗口不在这里强行弹回来（`_stack_list_user_closed`）——
            旧写法每次 `_sync_stack_button()` 都会把它重新 show 出来，"关闭"形同虚设；
            点底部 🖐、切开关、有新单入叠时清标记（那些场景确实该让它出现）。
        """
        dlg = getattr(self, "_stack_queue_dialog", None)
        mode_on = self._stack_mode_on()
        has_entries = bool(getattr(getattr(self, "_stack", None), "entries", None))
        if not mode_on and not has_entries:
            if dlg is not None and dlg.isVisible():
                dlg.hide()
            return
        if not has_entries and not self._stack_applicable():
            return
        dlg = self._ensure_stack_queue_dialog()
        self._refresh_stack_queue()
        if getattr(self, "_stack_list_user_closed", False):
            return                    # 操作员主动收起的：不抢着弹回来
        if not dlg.isVisible():
            self._show_stack_queue_dialog(dlg, activate=False)

    def _on_stack_taken_toggle(self, seq: int, plan_index: int, taken: bool):
        """队列窗的「✓ 我已取走 / ↩ 放回」：**账本裁决** → 落盘清单 → 刷新按钮与窗口。

        为什么不由界面自己判断：撤走一张第 2 趟还要内容的纸，进纸盒就少一张，整叠第 2 趟
        的逆序配对整体前移 ⇒ 每一张的背面都串到别的页上。这个闸门只能由账本按文件计划算
        （`toggle_taken` 会拒绝并给出原因），界面照单执行 + 如实显示原因。
        """
        stack = getattr(self, "_stack", None)
        if stack is None or not getattr(stack, "entries", None):
            return
        verb = "取走" if taken else "放回"
        if self._stack_busy():
            reason = "打印机正在打（第 1 趟 / 整叠第 2 趟）—— 打完再改计划"
            self._log(f"⚠ 不能{verb}：{reason}")
            QMessageBox.warning(self, "攒批队列", f"不能{verb}：{reason}")
            self._refresh_stack_queue()
            return
        fn = getattr(stack, "toggle_taken", None)
        if not callable(fn):
            reason = "当前账本版本不支持撤走（duplex_stack.toggle_taken 缺失）"
            self._log(f"⚠ 不能{verb}：{reason}")
            QMessageBox.warning(self, "攒批队列", f"不能{verb}：{reason}")
            self._refresh_stack_queue()
            return
        try:
            ok, reason = fn(int(seq), int(plan_index), bool(taken))
        except Exception as e:
            ok, reason = False, f"{type(e).__name__}: {e}"
        if not ok:
            self._log(f"⚠ 第 {seq} 单不能{verb}：{reason}")
            QMessageBox.warning(self, "攒批队列",
                                f"第 {seq} 单不能{verb}：\n\n{reason}")
            self._refresh_stack_queue()
            return
        # **立即落盘**：撤走/放回是"纸还在不在"的唯一真相，崩溃续打时必须带上
        bdir = self._stack_batch_dir()
        if bdir:
            duplex_stack.save_manifest(bdir, self._stack)
        self._log(f"🖐 {verb}：第 {seq} 单的第 {int(plan_index) + 1} 个文件 —— "
                  f"第 2 趟要送 {self._stack_present_sheets()} 张纸"
                  f"（第 1 趟共 {self._stack_original_sheets()} 张，"
                  f"已取走 {self._stack_taken_sheets()} 张）")
        self._sync_stack_button()        # 内含队列窗刷新
        self._refresh_stack_queue()

    def _stack_reconcile_gate(self) -> bool:
        """落机对账闸门：开打第 2 趟**之前**让人数够进纸盒里的纸。

        物理事实：出纸槽 face-down、后出的纸在上面；整叠不翻面搬回进纸盒后，进纸盒从
        **最上面**取纸 ⇒ 第 2 趟取纸顺序 = 第 1 趟印刷顺序的逆序。**少一张/多一张，后面
        每一张的背面都会串到别的页上**（整叠错位）—— 所以张数必须由人确认，不能默认相信
        "纸还在那儿"，更不能拿老计划直接开打。

        返回 False = 用户取消（不打第 2 趟，纸叠原样保留、任务继续挂着，不会误报成功）。
        """
        present = self._stack_present_sheets()
        original = self._stack_original_sheets()
        taken = max(0, original - present)
        active = self._stack_active_count()
        text = (f"<b>请数清楚进纸盒里现在有多少张纸：应是 {present} 张</b>"
                f"（第 1 趟共 {original} 张 − 已取走 {taken} 张）。\n\n")
        if taken:
            text += (f"已登记取走 {taken} 张：这些纸不要放进进纸盒"
                     f"（它们已是成品，不在第 2 趟的送纸计划里）。\n\n")
        text += ("⚠ 少一张/多一张都会让整叠错位：整叠的第 2 趟按逆序配对，进纸盒里张数不对，"
                 "后面每一张的背面都会串到别的页上。\n\n")
        text += (f"本叠 {self._stack.count} 单"
                 + (f"（其中 {self._stack.count - active} 单已全部取走）"
                    if active < self._stack.count else "")
                 + "：\n" + "\n".join(self._stack.describe_lines())
                 + "\n\n点「是」= 继续打印整叠的第 2 趟；点「否」= 取消（不出纸，纸叠原样保留）。")
        reply = QMessageBox.question(self, "开始打印第 2 趟：先数清楚纸数", text,
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            self._log(f"🛑 第 2 趟已取消（未确认进纸盒里的 {present} 张纸）—— 纸叠原样保留，"
                      f"任务继续挂着（不会误报完成）")
            return False
        self._log(f"✓ 已核对进纸盒：{present} 张（第 1 趟共 {original} 张 − 已取走 {taken} 张）")
        return True

    def _finish_empty_stack_second_pass(self):
        """第 2 趟**无内容**（整叠的纸都被取走 / 全是单面成品）→ 不启动打印循环，直接收尾。

        ⚠ 只有"第 2 趟无内容"才能走这里：还有内容的单绝不能跳过第 2 趟（那等于把半成品
        当成品收钱）。因此调用方已用 present_sheet_count==0 / build_plan() 槽位==0 双判。

        逐单判成品（第 1 趟失败的绝不当成品）**不在本方法里做**，而是统一在
        `_on_stack_flush_done` 里判 —— 那里是整叠唯一的收尾落点，口径只此一处。
        """
        entries = list(self._stack.entries)
        self._log(f"🖐 第 2 趟无内容：{len(entries)} 单没有背面要打 / 纸已被取走"
                  f" —— 不打印、不抽纸，直接收尾")
        self._stack_flush_snapshot = entries
        self._on_stack_flush_done(True, "第 2 趟无内容（没有背面要打，不出纸）", 0)

    def _on_stack_flush(self):
        """人工确认已把整叠搬回进纸盒 → 打整个纸叠的第 2 趟。"""
        if self._stack_flush_worker is not None and self._stack_flush_worker.isRunning():
            return                      # 已经在打：窗口归 worker 管，别动它
        if self._stack_busy():
            self._log("⚠ 打印机正在忙（还有第 1 趟在打），等它结束再打第 2 趟")
            self._refresh_stack_queue()   # 列表按钮回到可点状态（别停在假的"打印中"）
            return
        if not self._stack.entries:
            self._log("🖐 纸叠是空的（没有待打第 2 趟的单），第 2 趟不启动")
            self._refresh_stack_queue()
            return
        present = self._stack_present_sheets()
        slots = self._stack_second_pass_slots()
        if present <= 0 or slots == 0:
            # 第 2 趟无内容（整叠全是单面成品 / 含内容的项已被裁掉）：**根本不启动打印循环**
            # —— 否则只会白抽一遍纸，而且 done 回调会把成品当半成品。
            self._finish_empty_stack_second_pass()
            return
        # 落机对账闸门：启动 worker **之前**必须确认进纸盒里就是这 M 张（见方法注释）
        if not self._stack_reconcile_gate():
            self._refresh_stack_queue()   # 点了「否」= 这次不打，纸叠原样保留、按钮照旧可用
            return
        bdir = self._stack_batch_dir()
        self._log(f"▶ 开始打整叠第 2 趟：{self._stack.count} 单 / "
                  f"{present} 张纸（第 1 趟共 {self._stack_original_sheets()} 张，"
                  f"已取走 {self._stack_taken_sheets()} 张；"
                  f"{'长边旋转 180° 补偿' if self._stack.rotate else '短边不旋转'}）")
        # 冻结本次要收尾的整叠：第 2 趟期间即使有别的 entry 混进 _stack.entries，
        # 也不会被 _on_stack_flush_done 当成成品一起收尾（半成品当成品 = 白收钱 + 账目错）
        self._stack_flush_snapshot = list(self._stack.entries)
        w = DuplexFlushWorker(self._stack, bdir, self._config.printer_name,
                              dpi=self._config.render_dpi)
        w.progress.connect(self._on_progress)
        w.log_message.connect(self._log)
        w.done.connect(self._on_stack_flush_done)
        self._stack_flush_worker = w
        self._progress_bar.setValue(0)
        w.start()
        self._refresh_stack_queue()       # 列表立刻切到「正在打整叠第 2 趟」+ 按钮禁用

    def _finish_stack_progress(self, text: str = ""):
        """整叠处理完毕（放弃 / 第 2 趟失败 / 无第 2 趟内容）→ 进度条打到 100%。

        攒批单的进度分母是「纸张数 × 2」（两趟都要打），只打完第 1 趟自然停在 50%；但这一叠
        既然不再处理了，半格进度会让人以为还在进行中（2026-10-23 用户截图：放弃整叠之后
        进度条停在 50%）。这里把它收成满格，状态栏写明结局（`text` 为空时只动进度条）。
        """
        bar = getattr(self, "_progress_bar", None)
        if bar is not None:
            bar.setMaximum(100)
            bar.setValue(100)
        label = getattr(self, "_status_label", None)
        if text and label is not None:
            label.setText(text)

    def _on_stack_flush_done(self, ok: bool, msg: str, sides: int):
        """整叠第 2 趟结束（成功/失败）→ 统一收尾所有单的任务。

        **这里是整叠唯一的收尾落点**（第 2 趟成功/失败、放弃整叠、"第 2 趟无内容"都汇到
        这里）——收尾口径只此一处，少一条分支就少一类"两份口径"的 bug。
        因此"逐单判成品"也放在这里（见下），不要在别处再开一条 `_finish_stack_entries` 调用。
        """
        try:
            self._stack_flush_worker = None
            # 用第 2 趟开始时的快照收尾（而非当下的 _stack.entries）：第 2 趟期间混进来的
            # 新单只打了第 1 趟，绝不能按成品 report_success（🔴-3 的第二道防线）
            entries = list(self._stack_flush_snapshot or self._stack.entries)
            self._stack_flush_snapshot = []
            if ok:
                self._log(f"✅ {msg}")
            else:
                self._log(f"✗ {msg}")
                self._log("⚠ 整叠第 2 趟未正常完成：出纸槽里的纸可能是半成品，请人工核对")
            final_msg = (msg if ok else
                         f"攒批第 2 趟失败：{msg} —— 整叠是半成品，请人工处理")
            empty_pass2 = (self._stack_second_pass_slots() == 0)
            if ok and empty_pass2:
                self._finish_stack_entries_split(entries, final_msg)
            else:
                self._finish_stack_entries(entries, ok, final_msg)
            bdir = self._stack_batch_dir()
            self._stack.clear()
            duplex_stack.discard_batch(bdir)
            self._sync_stack_button()
            # 这一叠到此为止（成功/失败都不再有第 2 趟进度）→ 进度条收满格
            self._finish_stack_progress(
                "" if ok else f"⏹ 攒批未完成：{str(msg)[:70]}")
            if not ok:
                QMessageBox.warning(
                    self, "攒批第 2 趟失败",
                    f"{msg}\n\n出纸槽/纸盘里的纸可能是半成品，请人工核对后再处理。\n"
                    f"相关打印任务已按失败上报（未标记完成）。")
        finally:
            self._drain_stack_wait_queue()

    def _on_stack_abandon(self, interactive: bool = True):
        """放弃整叠：第 1 趟的纸已是半成品，撤销不了；按失败收尾并清掉清单。

        `interactive=False`：调用方**已经问过**了（关闭列表 / 关攒批模式的三选确认里
        选了「放弃整叠」），别再弹第二个 Yes/No 框 —— 双重确认会让人以为第一次没生效。
        """
        if not self._stack.entries:
            self._refresh_stack_queue()
            return
        n, sheets = self._stack.count, self._stack_present_sheets()
        if interactive:
            reply = QMessageBox.question(
                self, "放弃整叠",
                f"当前纸叠有 {n} 个打印任务 / {sheets} 张纸（只打了第 1 趟）。\n\n"
                f"放弃后这些任务不会标记完成、也不会打第 2 趟，"
                f"出纸槽里的纸需要人工处理（可自行丢回纸盘再用或作废）。\n\n确定放弃吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                self._log("🖐 已取消「放弃整叠」—— 纸叠原样保留，可以继续打第 2 趟")
                self._refresh_stack_queue()
                return
        entries = list(self._stack.entries)
        bdir = self._stack_batch_dir()
        self._log(f"⏭ 放弃整叠：{n} 单 / {sheets} 张纸（半成品）")
        self._finish_stack_entries(entries, False,
                                   "攒批被人工放弃（只打了第 1 趟，纸是半成品）")
        # 放弃 = 这一叠处理完了：进度条不能停在「第 1 趟打完的 50%」（用户截图反馈），
        # 状态栏同时写明结局，免得被读成"还在打印"
        self._finish_stack_progress(f"⏹ 已放弃整叠：{n} 单 / {sheets} 张纸按失败收尾")
        self._stack.clear()
        self._stack_flush_snapshot = []
        duplex_stack.discard_batch(bdir)
        self._sync_stack_button()
        # 排队等第 1 趟的攒批单：整叠已被放弃 ⇒ 一并取消（不是打了一半的纸，
        # 文件仍在各自标签页里，操作员可重新点「开始打印」）。
        if self._stack_entry_wait:
            n_q = len(self._stack_entry_wait)
            self._stack_entry_wait.clear()
            self._log(f"🖐 放弃整叠：另有 {n_q} 个排队等第 1 趟的攒批单已取消"
                      f"（文件还在对应标签页，可重新点「开始打印」）")
        self._refresh_stack_queue()
        self._drain_stack_wait_queue()

    def _confirm_stack_close(self, purpose: str = "关闭攒批列表") -> str:
        """纸叠还没打第 2 趟时，"关闭列表 / 关掉攒批模式"必须先让人明确三选。

        返回 `"keep"`（保留纸叠，只收起窗口或关模式）/ `"abandon"`（放弃整叠：这些单
        全部按失败收尾，不可撤销）/ `""`（取消，什么都不做）。

        为什么不能只给「是/否」（2026-10-21 用户提问）：两种意图都要有出口 ——
        ① 我只是想把这个窗收起来，纸和订单都还在（绝不能让订单失败）；
        ② 我不打算打第 2 趟了，这叠纸作废（这时订单必须按失败收尾，否则永远挂在 printing）。
        旧实现两条路都不通：关窗口只是 hide（没有任何提示，订单既不失败也看不到），
        关模式则被"打印机正忙"直接堵死。
        """
        n = self._stack.count
        sheets = self._stack_present_sheets()
        original = self._stack_original_sheets()
        taken = max(0, original - sheets)
        box = QMessageBox(self)
        box.setWindowTitle(purpose)
        box.setIcon(QMessageBox.Warning)
        box.setText(
            f"这一叠还有 {n} 单 / {sheets} 张纸只打了第 1 趟，还欠整叠的第 2 趟"
            f"（第 1 趟共 {original} 张"
            + (f"，已取走 {taken} 张" if taken else "") + "）。\n\n"
            f"「保留」= 只收起窗口{ '/关掉攒批模式' if '模式' in purpose else '' }："
            f"纸叠清单和这 {n} 个订单都留着，程序会继续给云端保活（不会判失败）；"
            f"之后点底部「🖐 攒批 …」还能打开列表、打第 2 趟。\n\n"
            f"「放弃整叠」= 这 {n} 单全部按失败收尾（只有第 1 趟的纸是半成品，需要人工"
            f"处理），云端会收到打印失败、顾客可能重下单 —— 不可撤销。\n\n"
            f"（直接关程序也不会判失败：清单保留，下次启动会问你是否续打第 2 趟。）")
        b_keep = box.addButton("保留（纸叠与订单都留着）", QMessageBox.AcceptRole)
        b_abandon = box.addButton(f"放弃整叠（{n} 单全部按失败收尾）",
                                  QMessageBox.DestructiveRole)
        b_cancel = box.addButton("取消（保持原样）", QMessageBox.RejectRole)
        box.setDefaultButton(b_keep)
        # 三个选项的文案都长（且「放弃」不可撤销）→ 必须完整显示，见 _fit_message_box_buttons
        _fit_message_box_buttons(box)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_abandon:
            self._log(f"🖐 {purpose}：人工选择「放弃整叠」（{n} 单 → 按失败收尾）")
            return "abandon"
        if clicked is b_keep:
            self._log(f"🖐 {purpose}：人工选择「保留」—— 纸叠与 {n} 个订单都留着，"
                      f"第 2 趟随时可打")
            return "keep"
        self._log(f"🖐 {purpose}：已取消（{n} 单 / {sheets} 张纸仍在等第 2 趟）")
        return ""

    def _on_stack_list_close_requested(self):
        """攒批列表的「关闭」/窗口 ×：有没打第 2 趟的纸时先确认，再决定隐藏/放弃。"""
        dlg = getattr(self, "_stack_queue_dialog", None)
        if dlg is None:
            return
        if not getattr(self._stack, "entries", None):
            self._stack_list_user_closed = True
            dlg.hide()
            return
        choice = self._confirm_stack_close("关闭攒批列表")
        if choice == "":
            return
        self._stack_list_user_closed = True
        dlg.hide()
        if choice == "abandon":
            self._on_stack_abandon(interactive=False)
            return
        # 保留：给一条不挡操作的提示（状态栏），别让"关掉窗"被读成"这叠不要了"
        note = (f"🖐 攒批列表已收起：{self._stack.count} 单 / "
                f"{self._stack_present_sheets()} 张纸仍在等第 2 趟 —— 点底部「🖐 攒批」可再打开")
        self._log(note)
        bar = getattr(self, "_status_bar", None)
        if bar is not None:
            bar.showMessage(note, 6000)

    def _entry_is_finished_product(self, entry) -> bool:
        """整单文件计划全部被取走 ⇒ 这一单"第 1 趟即成品"（第 2 趟无内容）。

        只在第 1 趟**正常收尾**（STATE_DONE）时成立：第 1 趟失败/状态未知的单连纸数都不确定，
        不能因为有人点了「取走」就当成品（那等于把半成品报成功、白收钱）。
        """
        if str(getattr(entry, "state", "") or "") != duplex_stack.STATE_DONE:
            return False
        fn = getattr(entry, "active_plans", None)
        if callable(fn):
            try:
                return not (fn() or [])
            except Exception:
                return False
        return False

    def _finish_stack_entries_split(self, entries: list, final_msg: str):
        """「第 2 趟无内容」时的逐单收尾：成品按成功、其余按失败。

        为什么要拆（独立审查用真实状态复现过两条路径的差异）：整叠只剩"第 1 趟失败"的单、
        操作员又点了「我已取走」时，旧写法无条件 `ok=True` ⇒ 把**半成品报成功**
        （白收钱、云端不再重推）。启动续打那条路径一直是逐单判的，两条路径口径必须一致。

        ⚠ 只在这一态下调用（`_on_stack_flush_done` 里用"第 2 趟槽位数 == 0"把关）：
        正常打完的整叠里，单的 state 可能是普通态，逐单拆会把它们全判成"非成品"而误报失败。
        """
        product = [e for e in entries if self._entry_is_finished_product(e)]
        rest = [e for e in entries if e not in product]
        if product and rest:
            self._log(f"🖐 其中 {len(product)} 单是成品（纸已被取走）、"
                      f"{len(rest)} 单第 1 趟未正常收尾 —— 分别按成品/失败收尾")
        if product:
            self._finish_stack_entries(
                product, True, "第 1 趟即成品（该单的纸已被操作员取走，无需第 2 趟）")
        if rest:
            self._finish_stack_entries(
                rest, False, "第 2 趟无内容，但该单第 1 趟未正常收尾（纸数存疑）")

    def _finish_stack_entries(self, entries: list, ok: bool, message: str):
        """整叠收尾：按各单登记的任务快照统一"标记完成 + 回报云端"。

        ⚠ 第 1 趟结束时**绝不**调用这里（铁律②）：只打到第 1 趟的纸是半成品，
        置 sent / 报 print_success 会把半成品当成品（顾客付了双面的钱、结算也会错）。
        """
        done_tasks: set[int] = set()
        for entry in entries or []:
            # 「纸还在不在」的收尾口径：整单纸已被操作员取走 = 这一单已是成品。
            # 同叠**别的**单第 2 趟失败/被放弃，不该把成品的单连坐成失败 ——
            # 报失败会让云端重推（重复打印），而顾客手上那几张纸早就打好了。
            product_only = self._entry_is_finished_product(entry)
            entry_ok = bool(ok or product_only)
            entry_msg = (message if entry_ok == ok else
                         "第 1 趟即成品（该单的纸已被操作员取走，无需第 2 趟）")
            for t in (entry.tasks or []):
                tid = int(t.get("task_id") or 0)
                if tid and tid in done_tasks:
                    continue
                if tid:
                    done_tasks.add(tid)
                job = self._find_job_for_stack_task(entry, t)
                if job is not None:
                    self._report_job_result(job, entry_ok, entry_msg, tab_key=entry.tab_key,
                                            owner_name=t.get("owner_name", ""),
                                            is_admin_print=bool(t.get("is_admin_print", False)))
                elif tid and self._cloud_client:
                    # 本地已找不到这个 job（标签页被删/重编号）→ 只回报云端，
                    # 不能让订单永远停在 printing（后端 3 分钟后会判失败）
                    if entry_ok:
                        self._cloud_client.report_success(tid, {
                            "copies": int(t.get("copies") or 1),
                            "duplex": t.get("duplex") or "off",
                            "page_range": t.get("page_range") or "",
                            "page_count": int(t.get("page_count") or 0),
                            "is_admin_print": bool(t.get("is_admin_print", False)),
                            **({"owner_name": t.get("owner_name")} if t.get("owner_name") else {}),
                        })
                    else:
                        self._cloud_client.report_fail(tid, entry_msg)
        self._tick_keepalive(force=True)

    def _find_job_for_stack_task(self, entry, task: dict):
        """按 task_id / 文件路径把纸叠清单里的任务找回成活的 PrintJob（找不到返回 None）。"""
        tid = int(task.get("task_id") or 0)
        if tid:
            for tab in self._config.tabs.values():
                for job in (tab.jobs or []):
                    if int(getattr(job, "task_id", 0) or 0) == tid:
                        return job
        keys = {k[1] for k in (entry.job_keys or []) if len(k) > 1 and k[1]}
        if keys:
            tab = self._config.tabs.get(entry.tab_key)
            for job in (getattr(tab, "jobs", None) or []):
                if job.file_path in keys:
                    return job
        return None

    # ── 打印中保活（后端 3 分钟反馈超时 / 5 分钟孤儿回退的解药）──

    def _collect_keepalive_tasks(self) -> set[int]:
        """所有"正在打、即将打、已堆进纸叠"的云端子任务 id。

        每 60 秒重算一次（而不是维护增量集合）：少一处状态同步就少一类"忘了清导致
        永远保活"的 bug —— 保活一旦漏停，后端 3 分钟超时兜底就形同虚设。
        """
        ids: set[int] = set()

        def _grab(jobs):
            for j in jobs or []:
                try:
                    tid = int(getattr(j, "task_id", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if tid:
                    ids.add(tid)

        worker = getattr(self, "_worker", None)
        _grab(getattr(worker, "_jobs", None))
        _grab(getattr(self, "_flat_jobs", None))
        for entry in self._stack.entries:
            for t in (entry.tasks or []):
                try:
                    tid = int(t.get("task_id") or 0)
                except (TypeError, ValueError):
                    continue
                if tid:
                    ids.add(tid)
        for req in self._stack_wait:
            _grab(req.get("jobs"))
        for req in getattr(self, "_stack_entry_wait", None) or []:
            _grab(req.get("jobs"))
        for item in self._auto_print_retry:
            for tid in (item.get("task_ids") or []):
                try:
                    tid = int(tid)
                except (TypeError, ValueError):
                    continue
                if tid:
                    ids.add(tid)
        return ids

    def _tick_keepalive(self, force: bool = False):
        """作业进行中每 60 秒刷新后端 locked_at（见 cloud_client.report_keepalive）。

        为什么需要：`check_printing_timeout` 对已 accept 的子任务按 locked_at 起算 3 分钟
        判失败、`recover_orphaned_printing_tasks` 把超 5 分钟的 printing 回退 queued 重推
        （→ 重复打印）；而手动双面/攒批一次作业远超 3 分钟。
        集合没变化时**不重复发**（避免空转刷后端），force=True 时无论如何发一次。
        """
        try:
            ids = self._collect_keepalive_tasks()
        except Exception:
            return
        if not ids:
            self._keepalive_last = ()
            return
        key = tuple(sorted(ids))
        if not force and key == tuple(self._keepalive_last):
            return
        client = getattr(self, "_cloud_client", None)
        if client is None:
            return
        try:
            if client.report_keepalive(list(ids)):
                self._keepalive_last = key
        except Exception as e:
            logger.debug("保活发送失败: %s", e)

    def _stack_open_for_business(self) -> bool:
        """出纸槽是否已被一叠纸独占（含"正在打某一单的第 1 趟"这段窗口）。"""
        return self._stack_has_work() or bool(getattr(self, "_stack_pending_ctx", None))

    def _stack_gate(self, flat_jobs: list, tab_key: str | None) -> str:
        """攒批模式的入口分流：返回 "normal"（照旧打印）/ "parked"（已排队）/ "entry"（作为新单入叠）。

        ① **整叠第 2 趟已开始**（flush worker 存在）→ 出纸槽与进纸盒都被整叠独占，
           任何请求（含手动双面）一律排队 —— 插进去会让逆序配对报废，且
           `_on_stack_flush_done` 的 entries 快照会把这单连在一起当成品收尾（铁律①②）；
        ② 攒批开启 + 本请求含需手动双面的文件 → 试图入叠（容量/装订方向由账本判定）；
        ③ 其余请求：**只要纸叠非空**就排队 —— 出纸槽被独占，任何别的输出插进去都会
           让整叠第 2 趟的逆序错位串页（铁律①）。
        """
        jobs = list(flat_jobs or [])
        if not jobs:
            return "normal"
        # ① 第 2 趟已开始（在飞 / 待收尾）：整叠已被搬回进纸盒或正在翻面打印。
        #    ⚠ 不能把整个 `_stack_open_for_business()` 判断提到这里 —— 它包含"纸叠里
        #    已堆着第 1 趟的纸"这个**多单攒批的正常中间态**，提前会让攒批永远只能收 1 单。
        if self._stack_flush_worker is not None:
            # 说清是"正在打"还是"刚打完在收尾"（旧文案写成「正在打（或待收尾）」，
            # 把代码里**能分辨**的两种状态糊在一起，现场没法判断该等还是该动纸）。
            _flushing = self._stack_flush_worker.isRunning()
            self._park_print_request(
                jobs, tab_key,
                "整叠第 2 趟" + ("正在打" if _flushing else "刚打完、正在收尾")
                + "——出纸槽与进纸盒都被这一叠独占")
            return "parked"
        if self._stack_mode_on() and any(self._uses_manual_duplex_job(j) for j in jobs):
            ok, reason, _sheets, _unknown = self._admit_stack_entry(jobs)
            if ok:
                return "entry"
            self._park_print_request(jobs, tab_key, reason)
            return "parked"
        if self._stack_open_for_business():
            self._park_print_request(
                jobs, tab_key,
                "出纸槽里有未完成的手动双面纸叠（第 2 趟还没打）——插进去会串页")
            return "parked"
        return "normal"

    def _start_print_worker(self, flat_jobs: list, tab_key: str | None = None,
                            stack_entry: tuple | None = None) -> bool:
        """用给定的任务列表启动打印 Worker。返回 True=已启动，False=忙/被取消。
        tab_key：打印批次所属标签页（2026-12 无障碍后台打印用，默认当前前台标签页）。
        stack_entry：(seq, 本单输出 PDF, rotate) —— 攒批模式的"第 1 趟单"，见 _start_stack_entry。"""
        if not self._has_members():
            # 无成员禁止创建订单（订单必须归属某个成员）
            self._show_no_member_hint()
            return False

        # ── 攒批模式下的入口分流（必须在"忙"判断之前：排队不算"启动失败"）──
        if stack_entry is None:
            gate = self._stack_gate(flat_jobs, tab_key)
            if gate == "parked":
                return True          # 已受理（排队），调用方不要再当"忙"去重试
            if gate == "entry":
                return self._start_stack_entry(flat_jobs, tab_key or self._current_tab)

        if self._worker is not None and self._worker.isRunning():
            self._log("⚠️ 已有打印任务正在进行，不能重复启动")
            return False
        if self._stack_flush_worker is not None and self._stack_flush_worker.isRunning():
            # 攒批第 2 趟打的是**整叠第 1 趟的输出**，与新单共用同一台打印机；
            # 旧实现只看 self._worker（第 2 趟用的是 _stack_flush_worker）⇒ 新单会与
            # 整叠第 2 趟并发塞进打印机，页序全乱（🔴-3 旁路之二）。
            self._log("⚠️ 整叠第 2 趟正在打印，不能启动新的打印任务")
            return False
        if self._print_starting:
            self._log("⏳ 正在获取订单号/上报订单，请稍候…")
            return False

        # 打印前兜底：暂存副本若被外部清理（磁盘清理/杀软/手工删 %APPDATA%），
        # 从用户原件（source_path）重建副本；两者都没了才交给 Worker 报「文件不存在」
        for j in flat_jobs:
            _fp = getattr(j, 'file_path', '') or ''
            if not _fp or os.path.isfile(_fp):
                continue
            _src = getattr(j, 'source_path', '') or ''
            if _src and os.path.isfile(_src):
                _new = staging.stage_file(_src)
                if _new and os.path.isfile(_new):
                    j.file_path = _new
                    self._log(f"♻ 暂存副本缺失，已从原文件重建: {os.path.basename(_src)}")
                    continue
            self._log(f"⚠ 文件不可用（原件与暂存副本均缺失）: "
                      f"{j.display_name or os.path.basename(_fp) or _fp}")

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
        # 打印批次所属标签页：默认前台；无障碍后台打印时显式指定（冻结/附加服务/回报据此定位）
        target_key = tab_key or self._current_tab
        self._printing_tab_key = target_key

        # 立即冻结标签页 — 一旦开始打印，订单即已固定，不允许任何编辑
        tab = self._config.tabs.get(target_key)
        if tab:
            tab.frozen = True
        self._save_config()
        self._refresh_tab_display()
        self._log(f"🔒 标签页 {target_key} 已固定，打印完成后不可编辑")
        # 立即禁用「开始打印」按钮：取号/上报的等待窗口内用户也能看到打印已开始
        self._btn_start.setEnabled(False)

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
        # 附加服务：与界面合计（_update_total_cost）口径一致，从标签页读设置（后台打印读目标标签页）
        tab = self._config.tabs.get(target_key)
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
        # 网络操作放后台线程 + processEvents 保活事件循环，期间云端推送/下载完成信号不被冻结。
        online = self._cloud_client and self._cloud_client.is_connected()
        order_number, uploaded = self._acquire_order_number_and_upload(
            flat_jobs, files_data, total_price, extra_fields, tab, created_at, online,
        )
        if self._closing:
            # 取号/上报等待期间窗口被关闭（closeEvent 已清理）→ 中止本次打印启动
            self._log("⚠️ 窗口已关闭，已取消本次打印启动")
            return False

        # 将订单号写回 jobs（重要：确保封面页等后续流程能读到订单号）
        for j in flat_jobs:
            j.order_number = order_number

        try:
            self._save_config(sync_ui=False)
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

        _st_seq, _st_pdf, _st_rot = (stack_entry or (0, "", True))
        worker = PrintWorker(
            jobs=flat_jobs,
            printer_name=self._config.printer_name,
            duplex_mode=self._config.duplex_mode,
            keep_temp_pdf=self._config.keep_temp_pdf,
            render_dpi=self._config.render_dpi,
            cover_page=cover_page_enabled,
            cover_page_config=cover_page_config,
            manual_duplex_profile=self._printer_profile(),
            stack_mode=bool(stack_entry),
            stack_out_pdf=_st_pdf,
            stack_seq=int(_st_seq or 0),
            stack_rotate=bool(_st_rot),
        )
        worker.progress.connect(self._on_progress)
        worker.log_message.connect(self._log)
        worker.job_finished.connect(self._on_job_finished)
        worker.all_finished.connect(self._on_all_finished)
        worker.pdf_cached.connect(self._on_pdf_cached)
        worker.flip_required.connect(self._on_flip_required)
        worker.duplex_announce.connect(
            self._on_stack_announce if stack_entry else self._on_duplex_announce)
        worker.duplex_finished.connect(self._on_duplex_finished)
        worker.duplex_plan.connect(self._on_duplex_plan)
        worker.duplex_speed_sample.connect(self._on_duplex_speed_sample)
        if stack_entry:
            worker.stack_entry_planned.connect(self._on_stack_entry_planned)
            worker.stack_entry_done.connect(self._on_stack_entry_done)
        self._worker = worker
        worker.start()

        # 攒批单已开打：出纸槽从此被这一叠独占（_stack_gate 据此把别的请求排队）
        if stack_entry:
            self._tick_keepalive(force=True)

        # 人工已开始打印 → 清掉"需人工翻纸"挂起标记（⏸ 标记由自动打印挂起时置位）
        if tab is not None and getattr(tab, "manual_duplex_pending", False):
            tab.manual_duplex_pending = False
            self._save_config()
            self._refresh_tab_display()

        # 上报"开始打印"信号：打印批次实际启动 → 后端用服务器时钟记录 print_started_at（幂等，仅首次）。
        # 覆盖手动 / 无障碍自动 / 预约三类云端订单（task_id≠0）。
        # 只发信号不带时间戳——本机时钟可能不准，跨设备相减会得出错误的等待时长；
        # 时间由后端统一写入，等待时长由后端 calc_wait_seconds() 下发 wait_seconds。
        cloud_tasks: dict[int, list[int]] = {}
        for j in flat_jobs:
            tid = getattr(j, 'task_id', 0)
            oid = getattr(j, 'order_id', 0)
            if tid and oid:
                cloud_tasks.setdefault(oid, []).append(tid)
        if cloud_tasks and self._cloud_client:
            for oid, tids in cloud_tasks.items():
                self._cloud_client.report_start_printing(oid, tids)
        return True

    def _acquire_order_number_and_upload(
        self, flat_jobs, files_data, total_price, extra_fields, tab, created_at, online,
    ) -> tuple[str, bool]:
        """获取订单号并上报云端（打印启动前）。

        网络请求（GET 取号 / POST 上报）放后台线程执行，主线程用
        QApplication.processEvents 保活事件循环（沿用 closeEvent 的等待模式），
        期间界面不冻结、云端推送/下载完成等信号正常处理。
        返回 (order_number, uploaded)。
        """
        existing_order = ""
        for j in flat_jobs:
            if j.order_number:
                existing_order = j.order_number
                break
        # 这一批是不是**云端订单**（带 task_id = 来自小程序/APP）：是的话它的来源/明细归云端
        # 订单自身维护，**不能**再走 /api/local_orders（那会把 source 改写成 'local'）。
        is_cloud_order = any(int(getattr(j, "task_id", 0) or 0) > 0 for j in flat_jobs)

        # 离线/未配置云端：无网络，本地临时号 + 本地库缓存（快速，主线程直接执行）
        if not online or not self._cloud_client or not (self._cloud_client.api_url and self._cloud_client.token):
            order_number = existing_order or self._generate_local_order_number_l()
            if not existing_order:
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
                            remark=(tab.remark if tab else ''),
                        )
                        self._log(f"📴 离线未同步：任务只在本地（离线模式），联网后自动上传 "
                                  f"({len(files_data)} 个文件)")
                        # S1-09：这张单只在本地库里 —— 状态栏角标立刻显示「📴 离线未同步」，
                        # 不要再让它看起来像"已完成"（结算页上没有它）
                        self._refresh_offline_badge()
                    except Exception as e:
                        self._log(f"⚠️ 离线缓存失败: {e}")
            return order_number, False

        self._print_starting = True
        try:
            result: dict = {}

            def _net():
                try:
                    result.update(self._acquire_order_number_and_upload_net(
                        existing_order, files_data, total_price, extra_fields, tab,
                        created_at, is_cloud_order=is_cloud_order))
                except Exception as e:
                    result["order_number"] = ""
                    result["uploaded"] = False
                    self._cloudConnSyncLog.emit(f"⚠️ 订单号获取/上报异常: {e}")

            threading.Thread(target=_net, daemon=True).start()
            # S1-09：取号等待 30s → ORDER_NUMBER_WAIT_S（4s）。超时就用本地临时号开打，
            # 出纸不因为"云端取号慢"被拦住；连线后由换号流程把 -L 换成正式号并补报。
            deadline = time.time() + ORDER_NUMBER_WAIT_S
            while "order_number" not in result and time.time() < deadline:
                QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
                time.sleep(0.02)
            if self._closing:
                # 等待期间窗口被关闭（closeEvent 已清理标签页）→ 中止，不再启动打印
                return "", False
            if "order_number" not in result:
                # 超时兜底：本地临时号，连线后自动换正式号
                self._log(f"⚠️ 订单号获取超时（>{ORDER_NUMBER_WAIT_S}s），改用本地临时号")
                result["order_number"] = self._generate_local_order_number_l()
            return result.get("order_number") or "", bool(result.get("uploaded", False))
        finally:
            self._print_starting = False

    def _acquire_order_number_and_upload_net(
        self, existing_order, files_data, total_price, extra_fields, tab, created_at,
        is_cloud_order: bool = False,
    ) -> dict:
        """后台线程：云端取号 + 订单上报（网络部分）。

        不直接操作 GUI：日志经 _cloudConnSyncLog 信号回主线程。
        仅在线路径使用（离线路径由 _acquire_order_number_and_upload 主线程处理）。
        `is_cloud_order`：本批任务是云端订单（带 task_id）→ **不**再走 /api/local_orders，
        否则后端会把那行订单的 source 改写成 'local'（现场：小程序订单变「本地」）。
        """
        client = self._cloud_client
        api_url = client.api_url if client else ""
        token = client.token if client else ""
        order_number = ""
        uploaded = False

        def _upload(num: str) -> bool:
            try:
                resp = http_requests.post(
                    f"{api_url}/api/local_orders",
                    params={"token": token},
                    json={
                        "order_number": num,
                        "status": "sent",
                        "total_price": total_price,
                        "files": files_data,
                        "created_at": created_at,
                        # 接收设备（多设备接单）：本机 client_id，后端据此记录订单「接单设备」
                        "client_id": client.client_id,
                        # 订单归属（v24）：谁处理了这笔订单 + 是否管理员自行打印
                        "owner_name": (tab.owner_name if tab else DEFAULT_OWNER_NAME),
                        "is_admin_print": bool(tab.is_admin_print) if tab else True,
                        # 订单备注（标签页级，覆盖后端）
                        "remark": (tab.remark if tab else ''),
                        # 附加服务上报（与计费口径一致）
                        **extra_fields,
                    },
                    timeout=10,
                )
                if resp.ok and resp.json().get("success"):
                    self._cloudConnSyncLog.emit(f"📋 订单已上报云端: {num} ({len(files_data)} 个文件)")
                    return True
                self._cloudConnSyncLog.emit(f"⚠️ 订单上报云端失败: {resp.text[:200] if resp.text else '无响应'}")
            except Exception as e:
                self._cloudConnSyncLog.emit(f"⚠️ 订单上报云端失败: {e}")
            return False

        if existing_order:
            # 复用已有的订单号（来自之前的"复制"操作），在线时直接上传。
            # 🔴 但**云端订单**绝不能再上报到 /api/local_orders：那会把后端那行小程序/APP
            #   订单的 source 改写成 'local'（2026-10-18 现场：小程序订单打印后在结算页
            #   显示「本地」）。云端单的完成/失败由 print_success / print_failed（按 task_id）
            #   回报，这里跳过即可。
            #   `existing_order` 由调用方按"第一个带 order_number 的 job"选出；本层拿不到
            #   jobs 列表，故由调用方把"是不是云端单"判好传进来（`is_cloud_order`）。
            if is_cloud_order:
                self._cloudConnSyncLog.emit(
                    f"ℹ 云端订单 {existing_order}：本机不再重复上报本地订单"
                    f"（来源由云端订单自身维护，避免被改写成「本地」）")
                return {"order_number": existing_order, "uploaded": False}
            self._cloudConnSyncLog.emit(f"📋 复用已有订单号: {existing_order}")
            return {"order_number": existing_order, "uploaded": _upload(existing_order)}

        # 在线：从后端获取全局唯一订单号（同时后端创建 reserved 占位记录）——
        # 预留即带当前标签页归属，避免上报失败时占位单无归属
        try:
            resp = http_requests.get(
                f"{api_url}/api/next_order_number",
                params={**{"token": token}, **self._current_owner_params()},
                timeout=5,
            )
            if resp.ok:
                data = resp.json()
                if data.get("success"):
                    order_number = data.get("order_number", "")
        except Exception as e:
            logger.warning(f"获取云端订单号失败: {e}")

        if not order_number:
            # 获取订单号失败，回退到本地临时号（-L，连线后自动换正式号）
            order_number = self._generate_local_order_number_l()

        # 上传订单信息到后端（将 reserved 占位更新为 sent）
        return {"order_number": order_number, "uploaded": _upload(order_number)}

    def _on_all_finished(self, success_count: int, fail_count: int):
        """全部任务完成。标签页已固定，不允许再次打印或编辑。"""
        _finished_worker = self._worker
        self._worker = None
        # 判据不能只看 `_stack_pending_ctx`：旧写法在"攒批单第 1 趟还在打时点了第二单"
        # 的情况下 ctx 会被覆盖/清空（那次启动被忙检查拒绝），于是本方法把第 1 趟的结束
        # 当成普通批次，报出「全部 0 个任务已完成打印并回报成功」。worker 自己知道它是不是
        # 攒批单（`_stack_mode`），作为兜底判据（2026-10-21）。
        was_stack_entry = (
            bool(getattr(self, "_stack_pending_ctx", None))
            or bool(getattr(_finished_worker, "_stack_mode", False)))
        self._all_printed = (fail_count == 0)
        # 打印批次结束：清除后台打印上下文（下次打印回到前台标签页语义）
        self._printing_tab_key = ""

        if was_stack_entry:
            # ── 攒批单的第 1 趟结束（不是整批完成！）──
            # 本单的任务**不能**在这里收尾：纸只是打了一半，第 2 趟还没打。
            # 真正的收尾在 _finish_stack_entries（整叠第 2 趟打完/放弃时）。
            self._stack_pending_ctx = {}
            self._all_printed = False
            self._status_label.setText(
                f"🖐 攒批中：已堆 {self._stack.count} 单 / {self._stack.total_sheets} 张纸"
                f"（第 2 趟待确认）")
            if self._stack.entries:
                self._log(f"🖐 攒批第 1 趟结束：整叠 {self._stack.count} 单 / "
                          f"{self._stack.total_sheets}/{self._stack.capacity} 张纸")
                self._refresh_stack_dialog()
            self._sync_stack_button()
            self._tick_keepalive(force=True)
            # 打印机空闲了 → 若有被容量/方向挡住的请求排队，提示操作员先打第 2 趟
            if self._stack_wait:
                blocked = len(self._stack_wait)
                self._log(f"⏸ 还有 {blocked} 个打印请求在等这一叠打完第 2 趟"
                          f"（出纸槽 {self._stack.total_sheets}/{self._stack.capacity} 张）")
                if self._stack.total_sheets >= self._stack.capacity:
                    self._log("⚠ 出纸槽已达容量上限：请点「▶ 开始第 2 趟」腾空出纸槽")
            # 打印机刚从第 1 趟空出来 → 把排队等着"接着打第 1 趟"的新单顺次放行
            self._drain_stack_entry_queue()
            return

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
            self._log(f"🔒 全部 {total} 个任务已完成打印并回报成功！标签页已锁定。")

        # 无障碍自动打印重试队列：打印机空闲后补打忙时丢弃的订单（只打未完成的，已打的不重打）
        # 2026-12：补打保持后台（不切换前台标签页），与无障碍订单「后台创建标签页」一致
        if self._auto_print_retry:
            item = self._auto_print_retry[0]     # 先看队首：确认能启动才出队（防启动失败静默丢单）
            tab_key = item.get("tab_key", "")
            order_id = item.get("order_id", 0)
            tab = self._config.tabs.get(tab_key) if tab_key else None
            if tab is None and order_id:
                # P1（审计 2026-12）：tab_key 可能因标签页重编号/删除而失效 ——
                # 按订单号反查真正的标签页兜底，绝不按旧 key 猜（否则会打错订单）。
                found = self._find_tab_key_by_order_id(order_id)
                if found:
                    tab_key = found
                    tab = self._config.tabs[found]
                    self._log(f"⚡ 补打订单 #{order_id}: 标签页已重编号，按订单号定位到标签页 {tab_key}")
            if tab is not None and tab.jobs:
                pending = [j for j in tab.jobs if not getattr(j, 'sent', False)]
                if pending:
                    self._log(f"⚡ 打印机空闲，补打订单 #{order_id}（标签页 {tab_key}，{len(pending)} 个未打印文件）")
                    if self._start_print_worker(pending, tab_key=tab_key):
                        self._auto_print_retry.pop(0)
                    else:
                        # 🔴-B4-3：旧写法"先 pop 再启动"，启动失败（刚好又忙 / 正在取号）
                        # 就**静默丢单**，订单永远不打也不上报。改为留在队首，下次补打再试。
                        self._log(f"⚠ 补打订单 #{order_id} 本次未能启动（打印机忙/取号中），"
                                  f"保留在重试队列等待下次")
                else:
                    self._auto_print_retry.pop(0)
                    self._log(f"⚠ 重试订单 #{order_id} 标签页 {tab_key} 已全部打印完成，跳过补打")
            else:
                self._auto_print_retry.pop(0)
                self._log(f"⚠ 重试订单 #{order_id} 标签页 {tab_key or '(已删除)'} 已无文件，跳过补打")

        # 打印机空闲了 → 先接着打排队等第 1 趟的攒批单（连续堆纸），
        # 再放行攒批期间被挡住的其它排队请求（纸叠已打完时才会真的开打）
        self._drain_stack_entry_queue()
        self._drain_stack_wait_queue()

    def _on_about(self):
        """关于对话框。"""
        QMessageBox.about(
            self, "关于 HN 本地打印工具",
            f"<h3>HN 本地打印工具 v{APP_VERSION}</h3>"
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
        rows.append(("wkhtmltopdf<br><small>(Markdown→PDF；HTML 已不支持)</small>",
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

        # 收集在线设备日志按钮
        collect_btn = QPushButton("📥 收集所有在线设备日志")
        collect_btn.setToolTip("向后端请求：向所有在线打印设备下发收集指令，各设备回报本机 local_tool.log 尾部日志（每台最多 200KB）")
        collect_btn.clicked.connect(lambda: self._collect_device_logs(status_label))
        layout.addWidget(collect_btn)

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
        """从后端拉取 server.log 和 frontend.log（网络放后台线程，界面保持响应）。"""
        if not self._cloud_client or not self._cloud_client.api_url or not self._cloud_client.token:
            status_label.setText("⚠ 云端未连接，无法拉取")
            return
        status_label.setText("⏳ 正在拉取...")
        result: dict = {}

        def _net():
            try:
                results = []
                api_url = self._cloud_client.api_url
                token = self._cloud_client.token
                for log_type in ("server", "frontend"):
                    try:
                        resp = http_requests.get(
                            f"{api_url}/api/log/fetch",
                            params={"token": token, "type": log_type},
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
                result["text"] = "✓ 拉取完成: " + " | ".join(results)
            except Exception as e:
                result["text"] = f"✗ 拉取异常: {e}"

        threading.Thread(target=_net, daemon=True).start()
        deadline = time.time() + 35
        while "text" not in result and time.time() < deadline:
            QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
            time.sleep(0.02)
        try:
            status_label.setText(result.get("text", "✗ 拉取超时"))
        except RuntimeError:
            pass  # 等待期间对话框已关闭，标签已销毁

    def _collect_device_logs(self, status_label: QLabel):
        """收集所有在线打印设备的日志。

        经后端 /api/log/collect_all（printer token）：后端向所有在线客户端推送
        request_log，各设备回报本机日志尾部，后端聚合后返回。网络放后台线程，
        期间 processEvents 保活事件循环（不阻塞云端信号）。"""
        if not self._cloud_client or not self._cloud_client.api_url or not self._cloud_client.token:
            status_label.setText("⚠ 云端未连接，无法收集")
            return
        status_label.setText("⏳ 正在收集在线设备日志（最多等待 12 秒）...")
        result: dict = {}

        def _net():
            try:
                resp = http_requests.post(
                    f"{self._cloud_client.api_url}/api/log/collect_all",
                    params={"token": self._cloud_client.token},
                    json={"timeout": 10},
                    timeout=25,
                )
                result["resp"] = resp
            except Exception as e:
                result["error"] = str(e)

        threading.Thread(target=_net, daemon=True).start()
        deadline = time.time() + 30
        while ("resp" not in result and "error" not in result) and time.time() < deadline:
            QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
            time.sleep(0.02)
        if "error" in result:
            status_label.setText(f"✗ 收集失败: {result['error']}")
            return
        resp = result.get("resp")
        try:
            data = resp.json() if resp is not None else {}
        except Exception:
            data = {}
        if not (data and data.get("success")):
            msg = (data.get("message") if isinstance(data, dict) else "") or (
                f"HTTP {resp.status_code}" if resp is not None else "无响应")
            status_label.setText(f"✗ 收集失败: {msg}")
            return
        logs = data.get("logs") or []
        self._show_collected_logs(logs)
        ok_count = sum(1 for l in logs if l.get("content"))
        status_label.setText(f"✓ 已收集 {ok_count}/{len(logs)} 台在线设备的日志")

    def _show_collected_logs(self, logs: list):
        """弹窗展示收集到的各在线设备日志（每台设备一段：设备名 + 状态 + 内容）。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("📋 在线设备日志")
        dlg.resize(780, 580)
        layout = QVBoxLayout(dlg)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setLineWrapMode(QTextEdit.NoWrap)
        layout.addWidget(text)
        btn_row = QHBoxLayout()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dlg.accept)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        if not logs:
            # logs 为空 = 服务端一台在线设备都没有（设备超时未回报时它仍会带空 content 列出来，
            # 见 _show_collected_logs 下面的逐台渲染）—— 旧的「（或全部设备超时未回报）」
            # 把不可能发生的那种情况也写上了，属误导。
            text.setPlainText("云端当前没有在线设备 —— 没有可收集的日志。")
        else:
            parts = []
            for l in logs:
                name = l.get("device_name") or l.get("client_id") or "未知设备"
                cid = l.get("client_id", "")
                status = "🟢 在线" if l.get("online") else "⚫ 离线"
                header = f"========== {name} [{status}] {cid} =========="
                content = l.get("content") or ""
                if not content:
                    content = l.get("error") or "（无日志内容）"
                parts.append(header + "\n" + content)
            text.setPlainText("\n\n".join(parts))
        dlg.exec()

    def _clear_local_logs(self, status_label: QLabel):
        """清空本地日志文件。

        local_tool.log 正被 FileHandler 占用：Windows 下 Python 默认共享模式不含
        FILE_SHARE_DELETE，直接 os.remove 会抛 PermissionError（此前异常被静默吞掉，
        表现为「清空无效」）。改为截断为 0 字节——被占用句柄同样允许截断，
        后续日志 append 写入自动落在新文件末尾。"""
        cleared = 0
        failed = 0
        for fname in os.listdir(self._log_dir):
            if not fname.endswith(".log"):
                continue
            fpath = os.path.join(self._log_dir, fname)
            try:
                with open(fpath, "w", encoding="utf-8"):
                    pass
                cleared += 1
            except OSError:
                failed += 1
        msg = f"✓ 已清空 {cleared} 个本地日志文件"
        if failed:
            msg += f"（{failed} 个失败）"
        status_label.setText(msg)
        self._log("📋 已清空本地日志")

    def _on_shortcuts(self):
        """快捷键说明对话框。"""
        QMessageBox.information(
            self, "快捷键",
            "<table cellspacing='8'>"
            "<tr><td><b>Ctrl+C</b></td><td>复制合计金额</td></tr>"
            "<tr><td><b>Ctrl+Shift+C</b></td><td>复制计费明细</td></tr>"
            "<tr><td><b>Delete</b> / <b>Ctrl+D</b></td><td>删除选中文件</td></tr>"
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
        """已处理任务 ID 集合的持久化文件路径（跨重启防重复打印）。

        🔴-6：必须落在用户数据目录（%APPDATA%\\HN打印工具），不能放程序目录 ——
        装到 C:\\Program Files 后程序目录不可写，写失败会让"已处理"集合无法持久化：
        重启后同一批云端任务会被**重复打印**（顾客白付一次钱），且失败被静默吞掉。
        旧版写在程序目录的同名文件由 paths.migrate_legacy_data 一次性复制过来
        （幂等：目标已存在则跳过；源文件保留兜底）。
        """
        return os.path.join(paths.get_app_data_dir(), ".processed_tasks.json")

    def _load_processed_tasks(self) -> set:
        """启动时从磁盘加载已处理任务 ID 集合（进程崩溃/重启后不再重复打印）。"""
        try:
            with open(self._processed_tasks_path(), "r", encoding="utf-8") as f:
                import json as _json
                data = _json.load(f)
            if isinstance(data, list):
                return set(int(x) for x in data if str(x).isdigit())
        except FileNotFoundError:
            pass                      # 首次运行没有该文件是正常情况，不刷日志
        except Exception as e:
            # 不静默：读失败 ⇒ 以空集合启动 ⇒ 可能重复打印历史任务，必须留痕
            logger.warning(f"已处理任务集合读取失败（按空集合启动，可能重复打印已处理任务）: {e}")
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
        except Exception as e:
            # 🔴-6：不能静默 —— 写失败 ⇒ 集合无法持久化 ⇒ 重启后这批任务被重复打印
            logger.error(f"已处理任务集合保存失败（重启后可能重复打印该批任务）: {e}")

    def _is_processed_task(self, task_id: int) -> bool:
        """判断任务是否已处理（防 SocketIO + HTTP 双通道重复）。"""
        return task_id in self._processed_cloud_tasks

    def _on_cloud_task_received(self, task: CloudTask):
        """收到新的云端打印任务 → 加入云端任务列表窗口（无障碍打印任务自动跳过窗口）。"""
        if task.task_id in self._processed_cloud_tasks:
            return  # 已处理过，跳过（防 SocketIO + HTTP 双通道重复）
        if task.order_id and task.order_id in self._cloud_canceled_orders:
            self._log(f"☁ 忽略已取消订单 #{task.order_id} 的迟到任务 #{task.task_id}（{task.file_name}）")
            return  # 订单已被用户取消（push 与 cancel 并发/乱序），不再注册/打印
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
        if task.order_id and task.order_id in self._cloud_canceled_orders:
            self._cloud_tasks.pop(task.task_id, None)
            return  # 订单已被取消：迟到的下载完成/出错不再入列或上报
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
            # 无障碍打印任务出错：从队列中移除（并解除这些任务的"已入队"守卫，
            # 否则同一 task_id 重新推送 ready 时会被幂等守卫误拦、再也打不出来）
            if task.auto_print and task.order_id:
                for _t in self._auto_print_queue.pop(task.order_id, []):
                    self._enqueued_cloud_tasks.discard(_t.task_id)
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
        # 🔴-B4-1 幂等守卫（必须在**重置防抖之前**）：重复的 ready 事件不得再入队，
        # 更不得反复重置防抖。`_processed_cloud_tasks` 覆盖"已处理完"，
        # `_enqueued_cloud_tasks` 覆盖"已入队但还没处理"这段窗口（数秒）。
        if (task.task_id in self._processed_cloud_tasks
                or task.task_id in self._enqueued_cloud_tasks):
            return
        oid = task.order_id
        self._enqueued_cloud_tasks.add(task.task_id)
        if oid not in self._auto_print_queue:
            self._auto_print_queue[oid] = []
        self._auto_print_queue[oid].append(task)
        self._log(f"⚡ 无障碍任务 #{task.task_id} 已加入队列（订单 #{oid}，"
                  f"共 {len(self._auto_print_queue[oid])} 个文件）")

        # 同一订单的后续文件只**延长**同一只定时器（旧写法每次 new QTimer(self) 后
        # 只 stop 旧实例、不回收 ⇒ 既泄漏又让"重置防抖"语义含糊）。
        timer = self._auto_print_timers.get(oid)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda o=oid: self._process_auto_print_order(o))
            self._auto_print_timers[oid] = timer
        timer.start(AUTO_PRINT_DEBOUNCE_MS)

    def _process_auto_print_order(self, order_id: int):
        """防抖定时器触发：收集该订单全部就绪任务，创建标签页并自动开始打印。"""
        tasks = self._auto_print_queue.pop(order_id, [])
        self._auto_print_timers.pop(order_id, None)
        # 出队即解除"已入队"守卫，随后由 _processed_cloud_tasks 接管（见 _enqueue_auto_print_task）
        for t in tasks:
            self._enqueued_cloud_tasks.discard(t.task_id)
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

        # 自动创建标签页（2026-12：后台创建；当前标签页为空或已锁定时会把前台切过去，
        # 见 `_cloud_new_tab_should_focus`。返回目标标签页 key）
        tab_key = self._add_cloud_tasks_to_new_tab(ready_tasks, switch_tab=False)
        if not tab_key:
            self._log(f"⚠ 无障碍打印：订单 #{order_id} 未能创建标签页，跳过")
            return

        # **无障碍打印 = 顾客明确要求「不用人管、自动打」⇒ 直接开打，不再挂起等人点**
        # （2026-10-20 用户要求：旧实现遇到「本机需人工翻纸」的订单会挂起，要操作员先点
        # 「开始打印」才肯打第 1 趟；顾客勾了无障碍却还要人去点一下，等于无障碍失效）。
        # 2026-10-18 当初挂起的理由是「作业会超过后端 3 分钟反馈窗口 ⇒ 顾客先看到打印失败」——
        # 这条现在由 `print_keepalive` 兜住了：作业进行中每 60 秒刷新 locked_at
        # （见「打印中保活」，覆盖在飞/纸叠/排队任务），所以直接开打不会误判失败。
        # 仍保留的只有**第 2 趟的人工翻纸确认**（翻纸窗会弹出来等操作员）——那是物理限制，
        # 不是「要不要开始」。预约打印（到点自动打）不在此列：那条仍按 `_fire_scheduled_print`
        # 挂起等人（它可能在没人值守的时段到点，语义与「顾客此刻要打印」不同）。
        jobs = [j for j in self._tab_jobs(tab_key) if not getattr(j, 'sent', False)]
        if jobs and self._manual_flip_required_for(jobs):
            self._log(f"⚡ 无障碍打印订单 #{order_id}：含双面文件且本机需人工翻纸 —— "
                      f"按无障碍要求直接开始第 1 趟（第 2 趟仍会弹窗等翻纸确认）")

        # 通知后端已接受（后端权威校验订单是否仍有效；已取消 → 409 拒绝）
        if ready_tasks[0].order_id and self._cloud_client:
            result = self._cloud_client.accept_order_to_server(ready_tasks[0].order_id)
            if result == "canceled":
                # 订单已被用户取消（cancel 通知尚未到达本机，处于通知窗口期）：
                # 撤回该订单已加入标签页的任务，跳过自动打印
                oid = ready_tasks[0].order_id
                self._log(f"⚡ 订单 #{oid} 已被取消，撤回标签页 {tab_key}，跳过自动打印")
                tab = self._config.tabs.get(tab_key)
                if tab is not None:
                    keep = [j for j in tab.jobs if getattr(j, 'order_id', 0) != oid]
                    if len(keep) != len(tab.jobs):
                        tab.jobs = keep
                        self._save_config()
                        self._rebuild_table()
                        self._refresh_tab_display()
                        self._sync_edit_enabled(False)
                return

        # 自动开始打印（打印机忙时不静默丢弃 → 入重试队列，打印完成后自动补打）。
        # 只打印未完成（sent=False）的 job：追加到已有标签页时，已打印过的文件不重打
        if jobs:
            self._log(f"⚡ 自动开始打印标签页 {tab_key}（{len(jobs)} 个文件）")
            if not self._start_print_worker(list(jobs), tab_key=tab_key):
                # 🔴-B4-3：`jobs` 是"标签页里所有未 sent 的文件"（含上一批已进重试队列的）。
                # 旧写法无条件 append ⇒ 同一订单会出现多条条目、task_ids 互相重叠 ⇒
                # 补打时同一 task_id 被打两次（现场：#1062 上报"打印成功"两次）。
                # 改为按 order_id 合并：同一订单在重试队列里只保留一条，task_ids 取并集。
                tids = [t.task_id for t in ready_tasks]
                for it in self._auto_print_retry:
                    if it.get("order_id") == order_id:
                        for tid in tids:
                            if tid not in it["task_ids"]:
                                it["task_ids"].append(tid)
                        it["tab_key"] = tab_key
                        self._log(f"⚡ 打印机正忙，订单 #{order_id} 已在重试队列中，"
                                  f"本批文件已并入（共 {len(it['task_ids'])} 个文件）")
                        break
                else:
                    self._auto_print_retry.append({
                        "order_id": order_id,
                        "tab_key": tab_key,
                        "task_ids": tids,
                    })
                    self._log(f"⚡ 打印机正忙，订单 #{order_id} 已加入重试队列（打印完成后自动补打）")
        else:
            # jobs 是"该标签页里还没 sent 的文件"，为空 ⇒ 文件都在、只是都已经打完并回报过。
            # 旧文案写「无文件，跳过」，与"标签页里其实有文件"的事实不符。
            self._log(f"⚠ 无障碍打印：标签页 {tab_key} 已无待打印文件"
                      f"（该单文件都已打印完成），跳过")

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
        if oid in self._cloud_canceled_orders:
            self._log(f"⏰ 预约单 #{oid} 已被用户取消，忽略迟到文件 {task.file_name}")
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
        # 🔴-B2 兜底：tab_key 指向的标签页已被删除、或被重编号后的**另一个**标签页接管
        # → 丢弃旧 key（否则会把别的订单的文件当本单打印），按 ready 重建标签页。
        if st["tab_key"] and not self._scheduled_tab_still_owns(order_id, st["tab_key"]):
            self._log(f"⚠ 预约单 #{order_id}: 标签页 {st['tab_key']} 已失效，重新建立标签页")
            st["tab_key"] = None
        if not st["tab_key"]:
            # 首次全部就绪 → 创建标签页（后续新增文件追加；2026-12 后台创建 —— 当前标签页
            # 为空或已锁定时会切到新标签页，见 `_cloud_new_tab_should_focus`）。
            # 注意：预约单不走 accept_order（会误把父订单标成 accepted），
            # 后端状态由 file_ready → waiting、start_printing → printing 驱动。
            self._log(f"⏰ 预约单 #{order_id}: 全部 {len(ready_tasks)} 个文件就绪")
            for t in ready_tasks:
                self._mark_processed_task(t.task_id)
            st["tab_key"] = self._add_cloud_tasks_to_new_tab(ready_tasks, switch_tab=False)

        if st["frozen"]:
            # 文件已全部就绪但仍在冻结：正常路径等后端 start_print 重设目标并解冻。
            # 🔴-B3：后端只在 file_ready 那一瞬清 schedule_frozen（且要求"同一刻全部就绪"），
            # 最后一次 file_ready 早于冻结置位时两侧会**永久冻结** —— 订单文件已齐，
            # 却每秒被跳过、永不打印。因此无论在线/离线都要排本地自恢复定时器：
            # 到点仍未被后端解冻 → 按已齐文件自行解冻打印（并留日志，不静默）。
            if not st["print_timer"]:
                if self._cloud_connected:
                    self._log(f"⏰ 预约单 #{order_id}: 文件已齐但仍冻结，"
                              f"{SCHEDULED_SELF_HEAL_S}s 后本地自恢复打印")
                else:
                    self._log(f"⏰ 预约单 #{order_id}: 断网自恢复，"
                              f"{SCHEDULED_SELF_HEAL_S}s 后开始打印")
                self._start_scheduled_print_timer(
                    order_id, now + SCHEDULED_SELF_HEAL_S)
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

    def _on_bring_to_front_request(self):
        """单实例：收到新实例启动的通知 → 恢复窗口并置于前台。"""
        from single_instance import check_front_request
        if not check_front_request(self._front_event):
            return
        # 恢复最小化状态 + 置顶激活（Windows 前台锁由本进程自身线程触发，可正常抢前台）
        if self.isMinimized():
            self.showNormal()
        self.setWindowState((self.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        self.raise_()
        self.activateWindow()
        self._log("🪟 检测到重复启动，已将本窗口置于前台")

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
                # 🔴-B3 兜底：冻结且文件已齐 —— 原来这里直接 continue，一旦错过那一次
                # debounce 评估（`_evaluate_scheduled_order` 的 frozen 分支）就**永久冻死**。
                # 每秒检查时补排本地自恢复定时器（`not st["print_timer"]` 保证只排一次）。
                if st["frozen"] and st["ready"] and not st["print_timer"]:
                    self._log(f"⏰ 预约单 #{order_id}: 冻结中且文件已齐，"
                              f"{SCHEDULED_SELF_HEAL_S}s 后本地自恢复打印")
                    self._start_scheduled_print_timer(
                        order_id, now + SCHEDULED_SELF_HEAL_S)
                continue
            if st["target_ts"] and now >= st["target_ts"] and not st["frozen"] and not st["delayed_sent"]:
                self._freeze_scheduled_order(order_id, st["pending"])

        # 价格周期校验（节流 300s）：云端/本地配置可能被改，定时拉取保证单价始终一致
        self._pricing_check_counter += 1
        if self._pricing_check_counter >= 300:
            self._pricing_check_counter = 0
            try:
                threading.Thread(
                    target=self._sync_pricing, kwargs={"from_thread": True}, daemon=True
                ).start()
            except Exception:
                pass

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

        # 🔴-B3：本地自恢复触发（后端始终未发 start_print 解冻）→ 自行解冻并打印。
        # 走到这里 pending 必为空（上面已判），文件齐全，打印是完整的。
        if st["frozen"]:
            st["frozen"] = False
            self._log(f"⏰ 预约单 #{order_id}: 后端未解冻，本地自恢复打印（文件已全部就绪）")

        # 确保标签页存在（2026-12：后台创建/定位；当前标签页为空或已锁定时切过去，
        # 见 `_cloud_new_tab_should_focus`）
        # 🔴-B2 兜底：同 _evaluate_scheduled_order —— tab_key 失效即丢弃重建，
        # 绝不按残留的旧 key 去取 jobs（那会把别的订单的文件打印出来）。
        if st["tab_key"] and not self._scheduled_tab_still_owns(order_id, st["tab_key"]):
            self._log(f"⚠ 预约单 #{order_id}: 标签页 {st['tab_key']} 已失效，重新建立标签页")
            st["tab_key"] = None
        if not st["tab_key"]:
            for t in ready_tasks:
                self._mark_processed_task(t.task_id)
            st["tab_key"] = self._add_cloud_tasks_to_new_tab(ready_tasks, switch_tab=False)

        # 打印机正忙 → 10s 后重试（最多 60 次 ≈ 10 分钟），保证预约单不丢
        if self._worker and self._worker.isRunning():
            retry = st.get("retry_count", 0) + 1
            st["retry_count"] = retry
            if retry > 60:
                # 重试超限：上报后端失败并清理状态机（不静默丢弃）
                self._log(f"⚠ 预约单 #{order_id}: 打印机持续忙碌，放弃自动打印，请手动在标签页打印")
                self._report_scheduled_failure(
                    order_id, ready_tasks, "打印机持续忙碌，预约打印重试超限")
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
        jobs = [j for j in self._tab_jobs(st["tab_key"]) if not getattr(j, 'sent', False)]
        if jobs and self._hold_for_manual_duplex(jobs, st["tab_key"],
                                                 f"预约打印订单 #{order_id}"):
            # 不支持自动双面 + 含双面文件 → 挂起等人工（重试没意义：需要人翻纸）。
            # 不置 printed、不上报失败：订单保持待打印，等人工点「开始打印」。
            self._log(f"⏰ 预约单 #{order_id}: 本机已挂起等人工翻纸"
                      f"（不自动重试、不回报失败；需人工点「开始打印」）")
            self._cleanup_scheduled_order(order_id, reason="需人工翻纸，转为人工打印")
            return
        if jobs:
            if not self._start_print_worker(list(jobs), tab_key=st["tab_key"]):
                # 恰好此刻打印机变忙 → 不置 printed，10s 后重试（防丢失）
                self._log(f"⏰ 预约单 #{order_id}: 打印机正忙，10s 后重试")
                self._start_scheduled_print_timer(order_id, int(time.time()) + 10)
                return
            # 确认打印已启动后才置 printed（防假打印：忙时/空标签页不再标记成功）
            st["printed"] = True
            st["printed_ts"] = int(time.time())
        else:
            # 标签页无文件（可能被清空/删除，或已被手动打完）→ 不置 printed、不上报。
            # P1（审计 2026-12）：原实现把 retry_count 重置为 0 —— 直接绕过 60 次上限，
            # 每 10 秒空转**永不停**，订单既不打印也不上报，永久卡死在状态机里。
            # 改为与「打印机忙」共用同一重试计数与上限，超限上报失败并结束状态机。
            retry = st.get("retry_count", 0) + 1
            st["retry_count"] = retry
            if retry > 60:
                self._log(f"⚠ 预约单 #{order_id}: 标签页 {st['tab_key']} 持续无待打印文件，"
                          f"放弃自动打印并上报失败（已重试 {retry - 1} 次）")
                self._report_scheduled_failure(
                    order_id, ready_tasks, "预约打印：标签页持续无待打印文件")
                self._cleanup_scheduled_order(order_id, reason="标签页持续无文件，重试超限")
                return
            self._log(f"⚠ 无障碍预约打印：标签页 {st['tab_key']} 无文件，"
                      f"订单 #{order_id} 保持待重试（第 {retry} 次）")
            self._start_scheduled_print_timer(order_id, int(time.time()) + 10)

    def _report_scheduled_failure(self, order_id: int, tasks: list, reason: str):
        """预约单放弃自动打印时统一上报后端失败（绝不静默丢弃）。"""
        if not self._cloud_client:
            return
        for t in tasks or []:
            try:
                self._cloud_client.report_fail(t.task_id, reason)
            except Exception as e:
                logger.warning(f"预约单 #{order_id} 失败上报异常 (task={getattr(t, 'task_id', '?')}): {e}")

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

    def _purge_canceled_print_state(self, order_id: int, task_ids) -> dict:
        """撤单时把本机**还会把这一单打出来**的状态全部清掉（D5 第 12 条）。

        为什么必须"真清"而不只是弹窗提示：订单被顾客取消后，本机有四个入口还记得它 ——
        ① 标签页里的 job（手动双面挂起的单子正等着人工点「开始打印」）；② `_auto_print_retry`
        补打队列（打印机忙时暂存、之后自动补打）；③ `_stack_wait` 攒批排队（纸叠打完自动放行）；
        ④ 预约单状态机（到点自触发）。旧实现只弹一句"建议删除对应标签页" —— 只要操作员在
        挂起状态点一次「开始打印」，或队列/预约自动放行，就会把**已经取消的订单**打出来：
        白费纸，顾客那边还是"已取消"，收不到钱。

        清理粒度：只剔除属于该订单/这些 task_id 的项，**不整页丢弃** —— 同一个标签页里可能
        还混着别的订单（补打队列/纸叠排队同理，所以队列项按 task_ids 过滤而不是按 tab_key）。
        返回各处的清理计数，供调用方给出"已清理什么"的事实文案。
        """
        tids = {t for t in (task_ids or []) if t}
        removed = {"jobs": 0, "tabs": 0, "retry": 0, "wait": 0, "entry": 0,
                   "scheduled": 0}

        def _task_hit(job) -> bool:
            """这条 job 是否属于被取消的订单（按 task_id 优先，其次 order_id）。"""
            jt = getattr(job, "task_id", 0) or 0
            if jt and jt in tids:
                return True
            return bool(order_id) and (getattr(job, "order_id", 0) or 0) == order_id

        # ① 标签页任务：只删未完成（未 sent）的；已打出去的留着（删不删都不会再出纸）
        empty_tabs = []
        for key, tab in list(self._config.tabs.items()):
            keep = [j for j in tab.jobs if getattr(j, "sent", False) or not _task_hit(j)]
            if len(keep) != len(tab.jobs):
                removed["jobs"] += len(tab.jobs) - len(keep)
                tab.jobs = keep
                if not any(not getattr(j, "sent", False) for j in tab.jobs):
                    # 该页已无待打印内容 ⇒ 撤掉"挂起等人翻纸"的 ⏸（否则标记永远挂着）
                    tab.manual_duplex_pending = False
                if not tab.jobs:
                    empty_tabs.append(key)
        if empty_tabs:
            removed["tabs"] = len(empty_tabs)
            self._remove_tabs(empty_tabs, reason="云端订单已取消")

        # ② 补打队列（按订单/任务过滤；整页丢会把同页别的订单一起废掉）
        before = len(self._auto_print_retry)
        self._auto_print_retry[:] = [
            it for it in self._auto_print_retry
            if not ((order_id and it.get("order_id") == order_id)
                    or (tids & set(it.get("task_ids") or ())))
        ]
        removed["retry"] = before - len(self._auto_print_retry)

        # ③ 纸叠排队（等纸叠打完再放行的请求；同一个请求里可能混着别的订单的文件）
        new_wait = []
        for req in self._stack_wait:
            jobs = list(req.get("jobs") or [])
            keep = [j for j in jobs if not _task_hit(j)]
            removed["wait"] += len(jobs) - len(keep)
            if keep:
                req["jobs"] = keep
                new_wait.append(req)
        self._stack_wait[:] = new_wait

        # ③b 排队等第 1 趟的攒批单（2026-10-21）：同样要按订单剔，否则上一单打完
        # 它会被自动接着打出来 —— 已取消的订单会白费纸、顾客那边还是「已取消」。
        new_entry_wait = []
        for req in self._stack_entry_wait:
            jobs = list(req.get("jobs") or [])
            keep = [j for j in jobs if not _task_hit(j)]
            removed["entry"] += len(jobs) - len(keep)
            if keep:
                req["jobs"] = keep
                new_entry_wait.append(req)
        self._stack_entry_wait[:] = new_entry_wait

        # ④ 预约单状态机（到点会自触发；调用方可能已清过，这里兜底）
        if order_id and self._scheduled_orders.pop(order_id, None) is not None:
            removed["scheduled"] = 1

        if any(removed.values()):
            self._save_config()
            self._rebuild_table()
            self._refresh_tab_display()
            self._sync_edit_enabled(False)
            if removed["wait"] or removed["entry"]:
                self._sync_stack_button()
            if removed["entry"]:
                self._refresh_stack_queue()
            # 撤单后这些任务不再被引用 → 暂存副本按引用回收（避免越堆越多）
            self._sweep_staged_files("云端订单已取消")
            self._log(f"🧹 已清理订单 #{order_id} 的待打印状态：标签页任务 {removed['jobs']} 个"
                      f" / 空标签页 {removed['tabs']} 个 / 补打队列 {removed['retry']} 条"
                      f" / 纸叠排队 {removed['wait']} 个"
                      f" / 待打攒批单 {removed['entry']} 个"
                      f" / 预约单 {removed['scheduled']} 个")
        return removed

    def _on_cloud_order_canceled(self, order_id: int, task_ids: list, fix_status: str = ""):
        """云端订单**不可再打印** → 通知任务列表窗口更新状态，若正在打印则立即取消。

        `fix_status` 非空 = 管理员在结算页**人工订正**为终态（canceled/abandoned/rejected，
        后端 reason="manual_fix"）—— 与「顾客主动取消」走同一套清理（订单确实不该出纸），
        只有文案按来源区分，别把人工订正说成「被用户取消」。默认值 "" 保证既有两参调用不破。
        """
        fix_label = order_status_label_cn(fix_status) if fix_status else ""
        reason = (f"已被人工订正为「{fix_label}」" if fix_label else "已被用户取消")
        self._log(f"☁ 订单 #{order_id} {reason}")
        self._cloud_canceled_orders.add(order_id)  # 拒收该订单迟到的 print_task/状态更新（防乱序复活）
        if self._cloud_task_window:
            self._cloud_task_window.mark_canceled(order_id, task_ids)
        # 预约单被取消 → 清理预约状态机（停掉到点倒计时）
        if order_id in self._scheduled_orders:
            self._cleanup_scheduled_order(order_id, reason=reason)

        # 检查当前打印的任务是否包含被取消的 task_id → 立即终止打印
        cancel_current_print = False
        if self._worker and self._worker.isRunning():
            for job in self._worker._jobs:
                if job.task_id in task_ids or job.order_id == order_id:
                    cancel_current_print = True
                    break
        if cancel_current_print:
            # 「已自动取消」没说清**取消的是什么**（订单？打印作业？云端状态？）——
            # 这里只中止本机这次打印作业；订单本身不可再打印的原因见上一行日志。
            self._log(f"☁ 订单 #{order_id} 正在本机打印 → 已中止本机打印作业"
                      f"（订单{reason}）")
            self._worker.cancel()

        # D5 第 12 条：**真的清掉**还会把它打出来的状态（标签页任务 / 补打队列 / 纸叠排队 /
        # 预约单），而不是只弹一句"建议删除对应标签页" —— 手动双面挂起的订单要靠人工点
        # 「开始打印」，补打队列与纸叠排队也会自动放行，留着任务就会把已取消订单打出来。
        purged = self._purge_canceled_print_state(order_id, task_ids)
        if any(purged.values()):
            detail = []
            if purged["jobs"]:
                detail.append(f"标签页任务 {purged['jobs']} 个")
            if purged["tabs"]:
                detail.append(f"空标签页 {purged['tabs']} 个")
            if purged["retry"]:
                detail.append(f"补打队列 {purged['retry']} 条")
            if purged["wait"]:
                detail.append(f"纸叠排队 {purged['wait']} 个文件")
            if purged.get("entry"):
                detail.append(f"待打攒批单 {purged['entry']} 个文件")
            if purged["scheduled"]:
                detail.append(f"预约单 {purged['scheduled']} 个")
            QMessageBox.information(
                self, ("任务已订正" if fix_label else "任务已取消"),
                f"订单 #{order_id} {reason}，本机待打印的该订单任务已自动清理："
                + "、".join(detail) + "\n（不会再被打出来）",
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
        tab_key = self._add_cloud_tasks_to_new_tab(tasks)
        # 通知后端：订单已接受（后端权威校验订单是否仍有效；已取消 → 409 拒绝）
        if tasks[0].order_id and self._cloud_client:
            result = self._cloud_client.accept_order_to_server(tasks[0].order_id)
            if result == "canceled" and tab_key:
                # 订单已被用户取消（cancel 通知尚未到达本机，添加发生在通知窗口期）：
                # 撤回该订单已加入标签页的任务，防止打印已取消订单
                oid = tasks[0].order_id
                self._log(f"☁ 订单 #{oid} 已被取消，撤回标签页 {tab_key} 中该订单的任务")
                tab = self._config.tabs.get(tab_key)
                if tab is not None:
                    keep = [j for j in tab.jobs if getattr(j, 'order_id', 0) != oid]
                    if len(keep) != len(tab.jobs):
                        tab.jobs = keep
                        self._save_config()
                        self._rebuild_table()
                        self._refresh_tab_display()
                        self._sync_edit_enabled(False)
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(
                    self, "订单已取消",
                    f"订单 #{oid} 已被用户取消，无法添加，已撤回相关任务。")

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
            # 攒批模式状态（2026-10）：一连上就先报一次，移动端立刻能看到「攒批中/未开」
            self._report_stack_state()
            threading.Thread(
                target=self._conn_sync_worker,
                daemon=True,
                name="cloud-conn-sync",
            ).start()

    def _on_cloud_printer_state(self, state: dict):
        """（主线程）后端接单状态推送（PySide6 自动排队到主线程）。
        - 2026-12 多设备接单：不再有「唯一接管者被占用」，claim 总是成功；
          此分支保留兼容旧后端（take_orders_rejected）与正常状态缓存；
        - 正常状态更新缓存，并把本机所有者（owner_name）持久化到配置——
          新建标签页时作为默认订单归属者（2026-12）。"""
        if not isinstance(state, dict):
            return
        self._cloud_printer_state_cache = state
        # 本机所有者同步（授权页绑定后后端广播，本机立即生效，无需重启工具）
        owner = (state.get("owner_name") or "").strip()
        if owner and owner != (getattr(self._config, "cloud_owner_name", "") or "").strip():
            self._config.cloud_owner_name = owner
            try:
                self._save_config(sync_ui=False)
            except Exception:
                pass
        if state.get("take_orders_rejected"):
            msg = state.get("message", "打印机已被其他设备接管")
            self._log(f"☁ 接单续期失败：{msg}")
            self._config.cloud_take_orders = False
            try:
                self._save_config(sync_ui=False)
            except Exception:
                pass
            if self._cloud_client:
                self._cloud_client.take_orders = False
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "无法启用接单", f"{msg}\n\n本机「启用接单」已自动关闭。")

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
            # 价格同步（云端模式以服务器 pricing.json 为权威，连线后拉最新）
            try:
                self._sync_pricing(from_thread=True)
            except Exception:
                pass
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
        self._closing = True  # 打印启动等待（processEvents）期间检测到关窗 → 中止启动流程
        # 关闭云端任务列表窗口
        if self._cloud_task_window and self._cloud_task_window.isVisible():
            self._cloud_task_window.close()

        # ── 攒批：出纸槽里还有整叠的第 2 趟没打 ──
        # 退出**不清掉**批次清单：纸还在出纸槽里、任务都是半成品，留着清单下次启动才能续打。
        # 同时要把这一叠的任务从"退出即放弃"的名单里摘出来（它们不是"没打"，
        # 而是"打了一半"——放弃会让后端把订单标成 abandoned，第 2 趟就再也报不成功了）。
        if self._stack.entries or (self._stack_flush_worker is not None
                                   and self._stack_flush_worker.isRunning()):
            self._log("🖐 退出前检测到未完成的攒批纸叠")
            reply = QMessageBox.question(
                self, "攒批还没打完第 2 趟",
                f"出纸槽里有 {self._stack.count} 个打印任务 / {self._stack.total_sheets} 张纸"
                f"（只打了第 1 趟，还欠整叠的第 2 趟）。\n\n"
                f"「是」= 保留清单并退出 —— 下次启动会问你是否续打第 2 趟\n"
                f"「否」= 返回程序先把第 2 趟打完\n\n"
                f"⚠ 退出时请不要动出纸槽里的纸，否则续打会对不上页。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                self._closing = False
                return
            if self._stack_flush_worker is not None and self._stack_flush_worker.isRunning():
                self._log("⏳ 等待整叠第 2 趟打印线程结束…")
                self._stack_flush_worker.wait(8000)

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
                self._closing = False  # 用户选择不退出，恢复打印启动流程
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
                self._config.tabs = {"1": self._make_new_tab()}
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
                    self._closing = False  # 用户选择不退出，恢复打印启动流程
                    return
                # 用户确认退出 → 通知后端放弃未打印的云端任务 + 本地预留订单，然后清空
                # （已打印完成的 job 跳过，避免误放弃已完成订单）
                # 攒批纸叠里的任务更要跳过：它们不是"没打"，而是"打了一半"——
                # 放弃会让后端把订单标成 abandoned，续打第 2 趟时就再也报不成功了。
                _stack_task_ids = {int(t.get("task_id") or 0)
                                   for e in self._stack.entries for t in (e.tasks or [])}
                _stack_task_ids.discard(0)
                all_abandoned = []
                for key, tab in self._config.tabs.items():
                    for job in tab.jobs:
                        if getattr(job, 'sent', False):
                            continue  # 已打印完成，不放弃
                        if int(getattr(job, 'task_id', 0) or 0) in _stack_task_ids:
                            continue  # 攒批纸叠里的一单：半成品，保留订单等续打第 2 趟
                        all_abandoned.append(job)
                        if job.order_id > 0 and self._cloud_client:
                            self._cloud_client.abandon_order_to_server(job.order_id)
                        elif job.task_id > 0 and self._cloud_client:
                            self._cloud_client.abandon_order_to_server(job.task_id)
                        elif job.order_number and "-L" not in job.order_number and self._cloud_client:
                            # 本地预留订单（仅获取了订单号但未提交打印）→ 标记为放弃，并补记价格
                            # 金额传整单合计（不是本文件的价）+ 归属/自打标记
                            self._cloud_client.abandon_reserved_order(
                                job.order_number, self._tab_total_price(tab),
                                owner_name=(tab.owner_name if tab else ""),
                                is_admin_print=(bool(tab.is_admin_print) if tab else None))
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
                self._config.tabs = {"1": self._make_new_tab()}
                self._config.active_tab = "1"
                self._current_tab = "1"
                self._cleanup_orphan_pdf_cache(all_abandoned)

        try:
            # 删除所有空标签页并重新编号（确保下次启动从 1 开始）
            self._cleanup_empty_tabs()
            self._renumber_tabs()
            self._sync_ui_to_config()
            self._save_config(sync_ui=False)
            self._log(f"配置已自动保存至: {self._config_path}")
        except Exception as e:
            logger.warning(f"自动保存配置失败: {e}")

        # 退出时清理暂存副本（工作副本）：此时标签页已清空/重建，配置里不再引用任何副本；
        # 仍在打印的批次由 _staged_reference_paths 兜住（打印线程已在上方停止/等待）
        try:
            # 先结束还在跑的转换线程，避免它们继续读即将被清理的暂存副本
            self._cancel_all_convert_workers()
            self._sweep_staged_files("退出清理")
        except Exception as e:
            logger.warning(f"退出清理暂存副本失败: {e}")

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
        # v4.6 同口径（与「复制」`_on_copy_total` 一致）：复制计费明细也算"确认下单"，
        # 同样把文件明细 + 备注 + 价格 + 附加服务同步到后端（reserved 非空占位）。
        # 缺这一步会出现：只点「复制计费明细」不点「复制」时，云端订单还是旧数据/空占位
        # （钉住：tests/verify_copy_reserved_sync.py）。
        if order_number and self._cloud_client and self._cloud_client.is_connected():
            self._upload_reserved_order(order_number)
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
        # 合计行：上方一条与抬头同款的分隔线 + 💰 前缀（2026-10-21 用户要求；与小程序/APP 的
        # 「💰合计」同口径）。钉住：tests/verify_copy_reserved_sync.py ③b
        lines.append("-" * 14)
        lines.append(f"💰合计: ¥{total + extra:.2f}")
        QApplication.clipboard().setText("\n".join(lines))
        if self._copy_detail_btn:
            self._copy_detail_btn.setText("✅ 已复制")
            self._copy_detail_btn.setEnabled(False)
        if self._copy_total_timer:
            self._copy_total_timer.start(5000)

    # ──────── 转换完成回调 ────────

    def _on_convert_finished(self, row: int, file_path: str, cached_pdf: str, page_count: int,
                             orientation: str, tab_key: str = ""):
        """后台 PDF 转换完成 → 按 file_path 匹配行并更新标签页数据、表格。

        多文件订单并发转换时行号可能错位，统一按 file_path 找行：
        row 处恰好匹配直接回写，否则扫描全部 job；找不到（文件已被删除）→ 丢弃结果只清理旧缓存。
        tab_key（2026-12）：目标标签页（无障碍后台创建时定位；空 = 当前前台标签页）。"""
        # 按 file_path 匹配行
        jobs = self._tab_jobs(tab_key)
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
            if (not tab_key or tab_key == self._current_tab) and target_row < self._table.rowCount():
                self._table.item(target_row, self.COL_PAGES).setText("?")
            return

        jobs = self._tab_jobs(tab_key)
        old_pdf = jobs[target_row].cached_pdf
        if old_pdf and os.path.isfile(old_pdf) and old_pdf != cached_pdf:
            try:
                os.remove(old_pdf)
            except OSError:
                pass
        jobs[target_row].cached_pdf = cached_pdf
        jobs[target_row].page_count = page_count
        jobs[target_row].orientation = orientation

        # ── 单页归一化：有效页数 == 1 的本地文件一律改单面（2026-10-17 现场事故）──
        # 现场：单页 docx 的 job.duplex="on"（print_config.json 里 page_count=1）→ 日志
        # 「正在打印 (份数:1, 双面:on)」→ 驱动弹 HP「双面打印说明」向导，打出一张空白页 +
        # 正式页。1 页纸物理上不可能双面（配对需要 2 页），请求双面只会多走一张空白纸。
        # 页数**未知(0) 不是 1 页**：`_count_pages_in_range("", 0) == 0`，绝不能归一化
        # （否则云端/还没分析出页数的文件会被误判成单面而丢掉双面）。
        # ⚠ 打印侧另有一道**硬闸门**（`PrintWorker._effective_duplex`，按真实 PDF 数页），
        #    覆盖云端订单 / 旧配置 / 手工改坏；这一道只负责让界面与配置先和事实一致。
        _single_page_norm = False
        _job = jobs[target_row]
        _is_img = (bool(_job.file_path)
                   and os.path.splitext(_job.file_path)[1].lower() in self.IMAGE_EXTS)
        if (not _is_img and _job.duplex == "on"
                and int(_count_pages_in_range(_job.page_range or "", _job.page_count or 0)) == 1):
            _job.duplex = "off"
            _single_page_norm = True
            self._log(f"  → {_job.display_name or os.path.basename(_job.file_path)} "
                      f"实际只打 1 页 → 自动改为单面"
                      f"（1 页双面物理上不可能，请求双面会让驱动多送一张空白纸）")

        self._save_config()

        # 仅当目标标签页是当前前台标签页时才更新表格（后台标签页表格不可见）
        if not tab_key or tab_key == self._current_tab:
            self._set_current_jobs(jobs)
            if target_row < self._table.rowCount():
                self._table.item(target_row, self.COL_PAGES).setText(str(page_count))
                ori_map = {"portrait": "竖", "landscape": "横", "mixed": "混"}
                ori_text = ori_map.get(orientation, "")
                self._table.item(target_row, self.COL_ORIENT).setText(ori_text)
                if _single_page_norm:
                    # 归一化改了 job.duplex → **整行重画**（双面列必须立刻显示「单面」；
                    # 只改 COL_PAGES 会留下"表格写着双面、实际按单面打"的假象）。
                    self._fill_table_row(target_row, jobs[target_row])
            self._recalc_row_cost(target_row)
            self._update_total_cost()
            if _single_page_norm and target_row in self._selected_rows():
                # 编辑面板此刻正显示这一行 → 同步（与 `_selection_traits` 同源：1 页文件的
                # 「单/双面」控件本就该是灰的并显示单面）。它只读 job 重画面板、不写回，
                # 不会碰用户刚改的其它输入。
                self._on_table_selection()

        # 转换完成后存入 MD5 缓存（供后续同文件复用，避免重复转换）
        if cached_pdf and os.path.isfile(cached_pdf) and target_row < len(jobs):
            job = jobs[target_row]
            # 如果没有 source_md5，从源文件计算
            if not job.source_md5 and job.file_path and os.path.isfile(job.file_path):
                try:
                    job.source_md5 = self._cloud_client._compute_md5_file(job.file_path) if self._cloud_client else ""
                except Exception:
                    pass
            # 存入 MD5 缓存索引（docx 按转换引擎分开缓存，避免 WPS 文档被 Word 渲染的错误 PDF 被复用）
            if job.source_md5:
                # 写侧判定必须与读侧同一口径（原先只看 engine：图片/PDF/TXT 的 job.engine
                # 默认就是 "word" ⇒ 写成 _eword 后缀，而读侧只按 .docx 加后缀 ⇒ 永不命中）
                _cache_engine = cache_engine_for(job.file_path, getattr(job, 'engine', ''))
                if self._cloud_client:
                    try:
                        self._cloud_client._save_pdf_to_cache(
                            job.source_md5, cached_pdf,
                            job.display_name or os.path.basename(job.file_path),
                            os.path.splitext(job.file_path)[1].lower(),
                            page_count,
                            getattr(job, 'image_orientation', 'auto'),
                            _cache_engine,
                        )
                    except Exception as e:
                        self._log(f"  → MD5 缓存保存失败: {e}")
                else:
                    # 离线：直接更新 pdf_cache/index.json
                    try:
                        import json as _json
                        import paths as _paths
                        cache_dir = _paths.pdf_cache_dir()
                        idx_path = os.path.join(cache_dir, "index.json")
                        idx = {}
                        if os.path.exists(idx_path):
                            with open(idx_path, "r", encoding="utf-8") as _f:
                                idx = _json.load(_f)
                        idx[pdf_cache_key(job.source_md5, getattr(job, 'image_orientation', 'auto'), _cache_engine)] = {
                            "original_name": job.display_name or os.path.basename(job.file_path),
                            "source_ext": os.path.splitext(job.file_path)[1].lower(),
                            "page_count": page_count,
                            "engine": _cache_engine,
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

    def _cloud_new_tab_should_focus(self) -> bool:
        """云端新订单落地建标签页时，是否要把前台切到那个标签页（2026-10 用户要求）。

        判据只有两条 —— **当前标签页为空** 或 **已锁定（frozen）**：
          · 空标签页：没有任何正在准备的任务，切过去零打扰；
          · 已锁定：订单已开始打印、标签页已固定不可编辑，前台停在那里也没事可做
            （现场：打完一单标签页锁着，无障碍订单在后台悄悄建了标签页，操作员看不到）。
        当前标签页有未打印任务且未锁定时 **不切**（保持原行为：不抢正在编辑的前台）。
        """
        tab = self._config.tabs.get(self._current_tab)
        if tab is None:
            return True                     # 标签页已被删除/找不到 → 没有需要保护的前台
        if getattr(tab, "frozen", False):
            return True
        return len(getattr(tab, "jobs", None) or []) == 0

    def _focus_cloud_tab(self, key: str) -> None:
        """把前台切到云端订单标签页（与 `_switch_tab` 同口径）。

        切之前先结算旧标签页的「撤回清空」窗口（`_cancel_undo_if_active`）：撤回备份与
        5 秒定时器都按 `_current_tab` 索引，若切页时不清，备份会留在旧 key 上、
        定时器到期也弹不回来（被清空的云端任务永远不向后端放弃）。
        """
        if not key or key == self._current_tab:
            return
        self._cancel_undo_if_active()
        self._current_tab = key
        self._config.active_tab = key

    def _add_cloud_tasks_to_new_tab(self, tasks: list, switch_tab: bool = True) -> str | None:
        """将同一订单的多个云端任务添加到同一个新标签页，全部加入后再刷新显示。
        switch_tab=False（无障碍/预约自动打印）：默认在后台创建/追加、不切换当前标签页；
        但当前标签页为空或已锁定（`_cloud_new_tab_should_focus`）时仍会切过去。
        返回目标标签页 key（供后台打印定位 jobs；tasks 为空返回 None）。"""
        if not tasks:
            return None
        # 后台落地（switch_tab=False）也切前台的条件：当前标签页为空 / 已锁定。
        # 在这里统一决定，同一订单的多个文件走同一口径（不会中途换前台）。
        if not switch_tab and self._cloud_new_tab_should_focus():
            switch_tab = True
        target_key = None
        for task in tasks:
            target_key = self._add_cloud_task_to_new_tab(task, is_first=(task == tasks[0]),
                                                         switch_tab=switch_tab)
        # 全部任务加入后再刷新表格/显示（而非只对第一个刷新）→ 确保所有文件可见
        self._save_config()
        self._rebuild_table()
        self._refresh_tab_display()
        self._sync_edit_enabled(False)
        return target_key

    def _find_tab_for_order(self, order_id) -> str | None:
        """查找已包含指定云端订单任务的标签页 key；无则返回 None。
        用于多文件订单：即使文件下载完成时间分散（超过防抖窗口），也追加到同一标签页。"""
        if not order_id:
            return None
        for key, tab in self._config.tabs.items():
            if any(getattr(j, 'order_id', 0) == order_id for j in tab.jobs):
                return key
        return None

    def _add_cloud_task_to_new_tab(self, task: CloudTask, is_first: bool = True,
                                   switch_tab: bool = True) -> str:
        """将云端任务添加到标签页并（默认）切换过去。同一订单已有标签页 → 追加到该标签页。
        switch_tab=False（无障碍/预约自动打印）：默认后台创建/追加、不切换当前标签页，
        除非当前标签页为空或已锁定（见 `_cloud_new_tab_should_focus`）。
        返回目标标签页 key。"""
        # 同一订单已有标签页 → 追加到它，而不是新建（防下载完成时间分散导致拆成多个标签页）
        existing_key = self._find_tab_for_order(task.order_id)
        if existing_key is not None:
            if switch_tab:
                self._focus_cloud_tab(existing_key)
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
            # v4.6：云端订单备注 → 标签页级备注（编辑面板显示，可修改后覆盖上报）
            tab_settings.remark = getattr(task, 'remark', '') or ''
            # v24.1：云端订单若由管理员在前端标记"管理员自行打印"→ 预勾选并沿用归属人；
            # 否则为顾客订单（不是管理员自行打印），归属人默认当前机位管理员。
            # v5.24：顾客订单标签页归属 = 下单用户绑定的成员名（bound_owner_name，后端反查
            # 收支清算成员绑定），不再回退到「第一个成员」；管理员自打订单沿用 task.owner_name。
            # v5.25：未绑定成员的普通用户订单 → 归属固定显示「普通用户」（非真实成员）。
            tab_settings.is_admin_print = bool(getattr(task, 'is_admin_print', False))
            task_owner = (getattr(task, 'bound_owner_name', '')
                          or getattr(task, 'owner_name', '') or '').strip()
            if task_owner:
                tab_settings.owner_name = task_owner
            elif not tab_settings.is_admin_print:
                tab_settings.owner_name = FALLBACK_OWNER_NAME
            self._config.tabs[new_key] = tab_settings
            if switch_tab:
                self._focus_cloud_tab(new_key)
        else:
            # 追加到已存在的订单标签页（switch_tab=False 时 _current_tab 未切换，须显式用 existing_key）
            new_key = existing_key if existing_key is not None else self._current_tab

        ext = os.path.splitext(task.local_path)[1].lower() if task.local_path else ""
        image_exts = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
        # 仅使用任务自带的 source_md5（后端 MD5 索引提供），不再主线程计算大文件 MD5（防 UI 冻结）；
        # 缺失时由 _start_convert_worker 在转换流程中计算
        source_md5 = getattr(task, 'source_md5', '') or ''

        page_count = 0; orientation = ""; cached_pdf = ""
        need_convert = False

        # 0. 先确定 docx 转换引擎（缓存 key 需按引擎隔离，必须在查缓存之前计算；
        #    WPS 编辑的文档必须用 WPS 引擎渲染，避免 WPS 特殊布局被 Word 渲染出错）
        engine = "word"
        cache_engine = ""
        if ext in (".doc", ".docx") and task.local_path:
            from converter import _read_docx_last_editor, get_available_engines
            available = get_available_engines()
            editor = _read_docx_last_editor(task.local_path) if ext == ".docx" else None
            preferred = "wps" if editor == "wps" else "word"
            for eng in ([preferred] + [e for e in ["word","wps","libreoffice"] if e != preferred]):
                if available.get(eng, False): engine = eng; break
            # 引擎后缀判定统一走 cache_engine_for（仅 .docx + word/wps 才加后缀）
            cache_engine = cache_engine_for(task.file_name, engine)

        # 1. 检查 PDF 缓存（MD5 索引；图片按方向、docx 按转换引擎分开缓存）
        if source_md5 and self._cloud_client:
            cached_pdf, cached_meta = self._cloud_client._get_cached_pdf(
                source_md5, getattr(task, 'image_orientation', 'auto'), cache_engine)
            if cached_pdf and cached_meta:
                page_count = cached_meta.get("page_count", 0)
                orientation = ""  # 缓存里可能没有 orientation，从 PDF 读取
                if page_count > 0:
                    from pdf_printer import get_pdf_info as _gpi
                    _info = _gpi(cached_pdf)
                    page_count = _info.get("page_count", page_count)
                    orientation = _info.get("orientation", "")
                self._log(f"📦 缓存命中: {task.file_name} → {page_count} 页 (MD5={source_md5[:8]}...)"
                          + (f", 引擎={cache_engine}" if cache_engine else ""))
                cached_pdf = cached_pdf  # 直接使用缓存的 PDF

        # 2. 未命中缓存 → 从本地文件获取信息
        if page_count <= 0 and task.local_path and os.path.isfile(task.local_path):
            if ext == ".pdf":
                info = get_pdf_info(task.local_path); page_count = info["page_count"]; orientation = info["orientation"]
            elif ext in image_exts:
                info = get_image_info(task.local_path); page_count = info["page_count"]; orientation = info["orientation"]
            elif ext == ".docx":
                orientation = get_docx_orientation(task.local_path)

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
            remark=getattr(task, 'remark', '') or '',  # 云端订单备注
            cached_pdf=cached_pdf,        # 使用缓存的 PDF（如有）
        )
        self._config.tabs[new_key].jobs.append(job)

        # Word 文件：缓存未命中时才启动转换（传真实行号，job 已 append；
        # 回调改为按 file_path 匹配，行号仅作快速路径提示；tab_key 定位目标标签页）
        # from_cloud=True（S1-04）：云端任务的文件是顾客上传的 ⇒ 强制 LibreOffice，不碰 COM
        if ext in (".doc", ".docx") and task.local_path and not cached_pdf:
            row = len(self._config.tabs[new_key].jobs) - 1
            self._start_convert_worker(row, task.local_path, engine, tab_key=new_key,
                                       from_cloud=True)

        self._log(f"☁ 云端任务 #{task.task_id} 已添加到标签页 {new_key}")
        if self._cloud_client:
            self._cloud_client.accept_task(task.task_id)
        return new_key

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
        # 被移除任务的暂存副本已无人引用 → 立即回收（仍被其他标签页/打印批次引用的会保留）
        self._sweep_staged_files("移除任务")

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
        """添加文件到当前标签页的核心逻辑。

        2026-12 兼容性修复（压缩包拖拽）：每个文件先复制一份「工作副本」到本地暂存目录
        （staging.stage_file），列表任务只用副本路径。从压缩包直接拖出来的文件位于解压
        软件的临时目录（%TEMP%\\Rar$DIa0.123\\...），解压软件退出/临时目录被清理后原文件
        就没了，原先要等到打印时才报「文件不存在」。原件路径记录在 job.source_path
        （界面显示与双击打开仍用原件）；副本的回收见 _sweep_staged_files()。
        """
        # HTML/HTM 已移除；表格(xls/xlsx)在前端标记为不支持类型，不再本地打印
        allowed_types = {
            ".pdf", ".doc", ".docx",
            ".txt", ".md",
            ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp",
        }
        image_exts = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
        html_rejected: list[str] = []

        self._cancel_undo_if_active()
        jobs = self._get_current_jobs()
        rows_before = len(jobs)
        existing_paths = {j.file_path for j in jobs if j.file_path}
        # 原件路径（含副本已在列表里的情况）：同一文件重复添加仍按原逻辑跳过
        existing_sources = {
            os.path.normcase(os.path.abspath(j.source_path))
            for j in jobs if getattr(j, "source_path", "")
        }
        staged_count = 0

        for f in files:
            if not f:
                continue
            src = os.path.abspath(f)
            if not os.path.isfile(src):
                self._log(f"⚠ 已跳过不存在的文件: {f}")
                continue
            src_key = os.path.normcase(src)
            if src in existing_paths or src_key in existing_sources:
                continue  # 已在列表中
            ext = os.path.splitext(src)[1].lower()
            if ext in (".html", ".htm"):
                # S0-09 / D-2：HTML 打印支持**永久删除**。不静默 continue，收上来统一提示
                html_rejected.append(src)
                continue
            if ext not in allowed_types:
                continue
            # 关键：复制工作副本（原件之后被删/临时目录被系统清理都不影响打印）
            work_path = staging.stage_file(src)
            if work_path != src:
                staged_count += 1
            if work_path in existing_paths:
                continue
            existing_paths.add(work_path)
            existing_sources.add(src_key)

            page_count = 0; orientation = ""
            if ext == ".pdf":
                info = get_pdf_info(work_path); page_count = info["page_count"]; orientation = info["orientation"]
            elif ext == ".docx":
                orientation = get_docx_orientation(work_path)
            elif ext in image_exts:
                info = get_image_info(work_path); page_count = info["page_count"]; orientation = info["orientation"]
            engine = "word"
            if ext in (".doc", ".docx"):
                from converter import _read_docx_last_editor, get_available_engines
                available = get_available_engines()
                editor = _read_docx_last_editor(work_path) if ext == ".docx" else None
                preferred = "wps" if editor == "wps" else "word"
                for eng in ([preferred] + [e for e in ["word","wps","libreoffice"] if e != preferred]):
                    if available.get(eng, False): engine = eng; break
            is_image = ext in image_exts
            duplex_mode = "short-edge" if orientation == "landscape" else "long-edge"

            job = PrintJob(
                file_path=work_path, copies=1,
                source_path=src,  # 用户原件路径（显示/打开文件用；副本丢失时按此重建）
                duplex="off" if is_image else "on",
                duplex_mode=duplex_mode,
                page_count=page_count, orientation=orientation, engine=engine,
            )
            jobs.append(job)

        if html_rejected:
            # 拖入/粘贴/对话框选择 `.html/.htm`：明确告知不支持并给出替代路径
            self._warn_html_unsupported(html_rejected)

        if len(jobs) > rows_before:
            self._set_current_jobs(jobs)
            self._rebuild_table()
            self._refresh_tab_display()
            _msg = f"标签页 {self._current_tab}: 已添加 {len(jobs) - rows_before} 个文件"
            if staged_count:
                _msg += f"（{staged_count} 个已复制到暂存副本，原件删除/移动不影响打印）"
            self._log(_msg)
            # 只为**本次新增**的行启动预转换（rows_before 之前的行上次已处理过）。
            # 原先遍历整个 jobs ⇒ 添加第 N 个文件时把前 N-1 个全部重新渲染一遍（O(n²)，
            # 现场：7 个图片渲染 18+ 次、首个文件 50 秒内被渲染 4 次）。
            for i in range(rows_before, len(jobs)):
                job = jobs[i]
                ext = os.path.splitext(job.file_path)[1].lower()
                if ext != ".pdf":
                    # 所有非 PDF 文件都在添加时预转换为 PDF 并缓存（图片/TXT/Word 等）
                    # 这样崩溃恢复后即使源文件不可用，缓存 PDF 仍可用于打印
                    self._start_convert_worker(i, job.file_path, job.engine)
