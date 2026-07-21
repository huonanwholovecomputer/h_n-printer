"""
order_tabs.py — Tab bar + file card components (stable, native-Qt version)
Replaces custom paintEvent with QLabel + QSS styling to avoid IndexError.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal, QPoint
from PySide6.QtGui import QFont, QColor, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QSizePolicy, QMenu, QApplication,
)

# ═══════════════════════ constants ═══════════════════════

TAB_MIN_WIDTH = 100
TAB_MAX_WIDTH = 220
TAB_HEIGHT = 36
TAB_SPACING = 4
ADD_BUTTON_WIDTH = 36

CARD_PADDING = 8
CARD_SPACING = 4

# ═══════════════════════ helpers ═══════════════════════

def _truncate_text(text: str, max_chars: int) -> str:
    """Simple char-based truncation — robust, no pixel math."""
    if not text:
        return ""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3] + "..."


def _tab_bg_style(is_active: bool, is_highlight: bool) -> str:
    """Return QSS background for a tab button."""
    bg = "#e3f2fd" if is_active else "#f5f5f5"
    border = "2px solid #1976d2" if is_active else "1px solid #ddd"
    if is_highlight:
        bg = "#bbdefb"
        border = "2px dashed #1976d2"
    return (
        f"background-color: {bg};"
        f"border: {border};"
        f"border-radius: 6px;"
        f"padding: 2px 8px;"
    )


# ═══════════════════════ _TabButton ═══════════════════════

class _TabButton(QPushButton):
    """Single tab — uses QLabel children instead of custom paintEvent.
    All attributes initialised immediately in __init__."""

    def __init__(self, order_key: str, parent=None):
        super().__init__(parent)

        # -- state (all set immediately) --
        self.order_key = order_key
        self._is_active = False
        self._source_icon = "📁"
        self._label = "new"
        self._file_count = 0
        self._subtotal_text = ""
        self._progress = -1          # -1 = hide, 0-100 = percent
        self._has_unread = False
        self._highlight = False

        # -- geometry --
        self.setCheckable(True)
        self.setFixedHeight(TAB_HEIGHT)
        self.setMinimumWidth(TAB_MIN_WIDTH)
        self.setMaximumWidth(TAB_MAX_WIDTH)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("orderTab")
        self.setAcceptDrops(True)

        # -- layout --
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(6, 1, 6, 1)
        self._layout.setSpacing(4)

        self._icon_label = QLabel("📁")
        self._icon_label.setFixedWidth(20)
        self._icon_label.setAlignment(Qt.AlignCenter)
        font_icon = QFont(self.font())
        font_icon.setPointSize(10)
        self._icon_label.setFont(font_icon)

        self._text_label = QLabel("new")
        self._text_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        font_text = QFont(self.font())
        font_text.setPointSize(8)
        self._text_label.setFont(font_text)

        self._info_label = QLabel("0f")
        self._info_label.setFixedWidth(55)
        self._info_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        font_info = QFont(self.font())
        font_info.setPointSize(7)
        self._info_label.setFont(font_info)

        self._layout.addWidget(self._icon_label)
        self._layout.addWidget(self._text_label, 1)
        self._layout.addWidget(self._info_label)

        self._apply_style()

    # -- public setters --

    def set_active(self, active: bool):
        self._is_active = active
        self.setChecked(active)
        self._apply_style()

    def set_tab_info(self, source_icon: str, label: str, file_count: int,
                     subtotal_text: str = "", progress: int = -1,
                     has_unread: bool = False):
        self._source_icon = source_icon
        self._label = label
        self._file_count = file_count
        self._subtotal_text = subtotal_text
        self._progress = progress
        self._has_unread = has_unread
        self._refresh_labels()

    def set_highlight(self, on: bool):
        self._highlight = on
        self._apply_style()

    # -- internal --

    def _apply_style(self):
        self.setStyleSheet(_tab_bg_style(self._is_active, self._highlight))

    def _refresh_labels(self):
        self._icon_label.setText(self._source_icon)
        self._text_label.setText(_truncate_text(self._label, 18))
        text_font = self._text_label.font()
        text_font.setBold(self._is_active)
        self._text_label.setFont(text_font)

        if self._progress >= 0:
            info = f"{self._progress}%"
        elif self._subtotal_text:
            info = self._subtotal_text
        else:
            info = f"{self._file_count}f"
            if self._has_unread:
                info = f"● {info}"

        color = "#f44336" if self._has_unread and self._progress < 0 else "#888"
        self._info_label.setText(info)
        self._info_label.setStyleSheet(f"color: {color};")

    # -- drag support --

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.set_highlight(True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.set_highlight(False)

    def dropEvent(self, event: QDropEvent):
        self.set_highlight(False)
        urls = event.mimeData().urls()
        if urls:
            files = [u.toLocalFile() for u in urls if os.path.isfile(u.toLocalFile())]
            if files:
                bar = self._find_tab_bar()
                if bar:
                    bar.filesDroppedOnTab.emit(files, self.order_key)
            event.acceptProposedAction()
        else:
            event.ignore()

    def _find_tab_bar(self):
        p = self.parent()
        while p and not isinstance(p, OrderTabBar):
            p = p.parent()
        return p


# ═══════════════════════ OrderTabBar ═══════════════════════

class OrderTabBar(QScrollArea):
    """Horizontal scrollable tab bar.

    Signals:
      orderActivated(str)
      filesDroppedOnTab(list, str)
      newOrderRequested()
    """
    orderActivated = Signal(str)
    filesDroppedOnTab = Signal(list, str)
    newOrderRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("orderTabBar")
        self.setFixedHeight(TAB_HEIGHT + 6)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)

        self._container = QWidget()
        self._container.setObjectName("orderTabContainer")
        self._layout = QHBoxLayout(self._container)
        self._layout.setContentsMargins(0, 2, 0, 0)
        self._layout.setSpacing(TAB_SPACING)
        self._layout.setAlignment(Qt.AlignLeft)
        self.setWidget(self._container)

        self._tabs: dict[str, _TabButton] = {}
        self._active_key: str = ""

        self._setup_add_button()

    def _setup_add_button(self):
        self._add_btn = QPushButton("+")
        self._add_btn.setObjectName("addOrderBtn")
        self._add_btn.setFixedSize(ADD_BUTTON_WIDTH, TAB_HEIGHT)
        self._add_btn.setCursor(Qt.PointingHandCursor)
        self._add_btn.clicked.connect(self.newOrderRequested.emit)
        self._add_btn.setToolTip("New local order")
        self._add_btn.setAcceptDrops(True)
        self._layout.addWidget(self._add_btn)

    # -- public API --

    def set_tabs(self, orders: dict, active_key: str):
        current = set(self._tabs.keys())
        new = set(orders.keys())
        for k in current - new:
            self._remove_tab(k)
        for k in orders:
            o = orders[k]
            if k not in self._tabs:
                self._add_tab(k, o)
            else:
                self._update_tab(k, o)
        # keep + last
        self._layout.removeWidget(self._add_btn)
        self._layout.addWidget(self._add_btn)
        if active_key and active_key in self._tabs:
            self.set_active(active_key)
        elif self._tabs:
            self.set_active(next(iter(self._tabs.keys())))

    def set_active(self, order_key: str):
        if order_key == self._active_key:
            return
        if self._active_key and self._active_key in self._tabs:
            self._tabs[self._active_key].set_active(False)
        self._active_key = order_key
        if order_key in self._tabs:
            self._tabs[order_key].set_active(True)
            t = self._tabs[order_key]
            t.set_tab_info(t._source_icon, t._label, t._file_count,
                           t._subtotal_text, t._progress, False)

    def update_tab(self, order_key: str, order, subtotal_text: str = "",
                   progress: int = -1, has_unread: bool = False):
        if order_key not in self._tabs:
            return
        icon = "☁️" if getattr(order, 'is_cloud', False) else "📁"
        label = order.order_number or getattr(order, 'summary', 'untitled')
        self._tabs[order_key].set_tab_info(
            icon, label, getattr(order, 'file_count', 0),
            subtotal_text, progress, has_unread,
        )

    def remove_tab(self, order_key: str):
        self._remove_tab(order_key)
        if order_key == self._active_key:
            self._active_key = ""
            if self._tabs:
                self.set_active(next(iter(self._tabs.keys())))

    def active_key(self) -> str:
        return self._active_key

    def tab_count(self) -> int:
        return len(self._tabs)

    # -- internal --

    def _add_tab(self, order_key: str, order):
        btn = _TabButton(order_key)
        btn.clicked.connect(lambda c=False, k=order_key: self._on_tab_clicked(k))
        idx = self._layout.count() - 1
        self._layout.insertWidget(max(0, idx), btn)
        self._tabs[order_key] = btn
        self._update_tab(order_key, order)

    def _update_tab(self, order_key: str, order):
        if order_key not in self._tabs:
            return
        icon = "☁️" if getattr(order, 'is_cloud', False) else "📁"
        label = order.order_number or getattr(order, 'summary', 'untitled')
        self._tabs[order_key].set_tab_info(icon, label, getattr(order, 'file_count', 0))

    def _remove_tab(self, order_key: str):
        btn = self._tabs.pop(order_key, None)
        if btn:
            self._layout.removeWidget(btn)
            btn.deleteLater()

    def _on_tab_clicked(self, order_key: str):
        self.set_active(order_key)
        self.orderActivated.emit(order_key)


# ═══════════════════════ FileCardWidget ═══════════════════════

class FileCardWidget(QFrame):
    """Single file info card — native QLabel-based."""

    fileContextMenuRequested = Signal(str, QPoint)

    def __init__(self, file_path: str, parent=None):
        super().__init__(parent)
        self.file_path = file_path
        self.setObjectName("fileCard")
        self.setFrameShape(QFrame.StyledPanel)
        self.setCursor(Qt.PointingHandCursor)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_ctx)
        self.setMinimumHeight(56)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        main = QHBoxLayout(self)
        main.setContentsMargins(CARD_PADDING + 2, CARD_PADDING - 1,
                                CARD_PADDING, CARD_PADDING - 1)
        main.setSpacing(6)

        # icon
        self._icon_label = QLabel("📄")
        self._icon_label.setFixedWidth(32)
        self._icon_label.setAlignment(Qt.AlignCenter)
        f = QFont(self.font()); f.setPointSize(14)
        self._icon_label.setFont(f)
        main.addWidget(self._icon_label)

        # info column
        info = QWidget()
        il = QVBoxLayout(info)
        il.setContentsMargins(0, 0, 0, 0); il.setSpacing(1)

        self._name_label = QLabel("")
        self._name_label.setObjectName("cardFileName")
        fn = QFont(self.font()); fn.setPointSize(9); fn.setBold(True)
        self._name_label.setFont(fn)
        il.addWidget(self._name_label)

        self._params_label = QLabel("")
        self._params_label.setObjectName("cardParams")
        fp_ = QFont(self.font()); fp_.setPointSize(8)
        self._params_label.setFont(fp_)
        il.addWidget(self._params_label)

        self._pages_label = QLabel("")
        self._pages_label.setObjectName("cardPages")
        fpp = QFont(self.font()); fpp.setPointSize(7)
        self._pages_label.setFont(fpp)
        il.addWidget(self._pages_label)

        main.addWidget(info, 1)

        # cost
        self._cost_label = QLabel("")
        self._cost_label.setObjectName("cardCost")
        self._cost_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._cost_label.setFixedWidth(100)
        fc = QFont(self.font()); fc.setPointSize(10); fc.setBold(True)
        self._cost_label.setFont(fc)
        main.addWidget(self._cost_label)

    def set_info(self, file_name: str, copies: int, duplex: str,
                 page_range: str, page_count: int, orientation: str,
                 engine: str, cost_text: str, cost_value: float,
                 excel_warning: bool = False, is_image: bool = False):
        self._name_label.setText(file_name)

        duplex_label = "双面" if duplex == "on" else "单面"
        range_label = page_range if page_range.strip() else "全部"
        if excel_warning:
            self._params_label.setText("⚠ Excel — contact admin")
        else:
            self._params_label.setText(f"{copies}份 · {duplex_label} · 范围:{range_label}")

        ori_map = {"portrait": "竖", "landscape": "横", "mixed": "混"}
        ori = ori_map.get(orientation, "") if orientation else ""
        pages = f"{page_count} 页" if page_count > 0 else "页数未知"
        if ori:
            pages += f" · {ori}"
        self._pages_label.setText(pages)

        self._cost_label.setText(cost_text)
        self._cost_label.setStyleSheet(
            "color: #D4380D;" if cost_value > 0 else "color: #888;")

        if excel_warning:
            self._icon_label.setText("⚠")
        elif is_image:
            self._icon_label.setText("🖼")
        else:
            ext = os.path.splitext(file_name)[1].lower()
            if ext == ".pdf":
                self._icon_label.setText("📕")
            elif ext in (".doc", ".docx"):
                self._icon_label.setText("📝")
            else:
                self._icon_label.setText("📄")

    def _on_ctx(self, pos: QPoint):
        self.fileContextMenuRequested.emit(self.file_path, self.mapToGlobal(pos))


# ═══════════════════════ FileCardList ═══════════════════════

class FileCardList(QScrollArea):
    """Scrollable file card list for the active order."""

    filesDropped = Signal(list)
    fileSelected = Signal(str)
    fileDoubleClicked = Signal(str)
    fileContextMenuRequested = Signal(str, QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("fileCardList")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setAcceptDrops(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._container = QWidget()
        self._container.setObjectName("cardListContainer")
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(CARD_SPACING)
        self._layout.setAlignment(Qt.AlignTop)
        self.setWidget(self._container)

        self._cards: dict[str, FileCardWidget] = {}
        self._selected_path: str = ""

        self._empty_label = QLabel(
            'Drop files here or click "Add Files" below')
        self._empty_label.setObjectName("cardListEmpty")
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setWordWrap(True)
        self._layout.addWidget(self._empty_label)

    # -- public --

    def set_files(self, entries: list, calc_cost_fn,
                  simplex_price: float, duplex_price: float):
        # 1. pop and delete stale cards
        for path in list(self._cards.keys()):
            card = self._cards.pop(path, None)
            if card:
                card.hide()
                card.deleteLater()

        # 2. clear layout
        while self._layout.count() > 0:
            item = self._layout.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.setParent(None)

        # 3. re-add empty label
        if self._empty_label is None:
            self._empty_label = QLabel(
                'Drop files here or click "Add Files" below')
            self._empty_label.setObjectName("cardListEmpty")
            self._empty_label.setAlignment(Qt.AlignCenter)
            self._empty_label.setWordWrap(True)

        self._layout.addWidget(self._empty_label)
        self._empty_label.setVisible(len(entries) == 0)

        # 4. build cards
        for entry in entries:
            self._add_card(entry, calc_cost_fn, simplex_price, duplex_price)

        # 5. stretch only when content exists
        if entries:
            self._layout.addStretch()

        # 6. force repaint
        self._container.update()
        self.update()

    def _add_card(self, entry, calc_cost_fn, simplex_price, duplex_price):
        ext = os.path.splitext(entry.file_name)[1].lower()
        is_excel = ext in (".xls", ".xlsx")
        image_exts = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tiff", ".tif"}
        is_image = ext in image_exts

        cost, _ = calc_cost_fn(entry.page_count, entry.copies, entry.duplex,
                               simplex_price, duplex_price, entry.page_range)
        cost_text = f"¥{cost:.2f}" if cost > 0 else "¥0.00"

        card = FileCardWidget(entry.file_path)
        card.set_info(entry.file_name, entry.copies, entry.duplex,
                      entry.page_range, entry.page_count, entry.orientation,
                      entry.engine, cost_text, cost, is_excel, is_image)
        card.fileContextMenuRequested.connect(
            lambda fp, pos: self._on_card_ctx(fp, pos))

        def mk_click(fp):
            def h(e):
                self._selected_path = fp
                self.fileSelected.emit(fp)
            return h
        card.mousePressEvent = mk_click(entry.file_path)

        def mk_dbl(fp):
            def h(e):
                self.fileDoubleClicked.emit(fp)
            return h
        card.mouseDoubleClickEvent = mk_dbl(entry.file_path)

        idx = self._layout.count()
        self._layout.insertWidget(max(0, idx), card)
        self._cards[entry.file_path] = card
        return card

    def selected_file_path(self) -> str:
        return self._selected_path

    # -- drag / drop --

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if urls:
            files = [u.toLocalFile() for u in urls if os.path.isfile(u.toLocalFile())]
            if files:
                self.filesDropped.emit(files)
            event.acceptProposedAction()
        else:
            event.ignore()

    def _on_card_ctx(self, fp, pos):
        self.fileContextMenuRequested.emit(fp, pos)
