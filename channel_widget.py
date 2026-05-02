"""
pytraceview/channel_widget.py
Simplified channel list widget — portable companion to TraceView.

Provides visibility toggle, colour swatch, label display, inline rename,
and drag-to-reorder for any app that embeds pytraceview.

Signals on ChannelListWidget:
  visibility_changed(str, bool)   — trace_name, visible
  color_changed(str, str)         — trace_name, new_hex_color
  trace_removed(str)              — trace_name
  trace_renamed(str, str)         — trace_name, new_label
  order_changed(list)             — ordered list of trace names
  context_menu_requested(str, object)  — trace_name, QPoint global

This widget intentionally does NOT include oscilloscope-specific controls
(interpolation mode, segments, grouping dialogs).  Host applications that
need those should subclass or compose as appropriate.
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea,
    QPushButton, QCheckBox, QLabel, QFrame, QColorDialog,
    QLineEdit, QSizePolicy, QApplication,
)
from PyQt6.QtCore import Qt, pyqtSignal, QMimeData, QPoint
from PyQt6.QtGui import QColor, QDrag, QPixmap, QPainter, QCursor

from pytraceview.trace_model import TraceModel


# ── Single channel row ────────────────────────────────────────────────────────

class ChannelRow(QWidget):
    """One row in the channel list: [drag] [color] [✓ label] [✕]"""

    visibility_changed    = pyqtSignal(str, bool)
    color_changed         = pyqtSignal(str, str)   # name, hex
    remove_requested      = pyqtSignal(str)
    renamed               = pyqtSignal(str, str)   # name, new_label
    context_menu_requested = pyqtSignal(str, object)

    _DRAG_MIME = "application/x-pytraceview-channel"

    def __init__(self, trace: TraceModel, parent=None):
        super().__init__(parent)
        self._name = trace.name
        self._dragging = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)

        # Drag handle
        self._drag_handle = QLabel("⠿")
        self._drag_handle.setFixedWidth(14)
        self._drag_handle.setCursor(Qt.CursorShape.SizeVerCursor)
        self._drag_handle.setToolTip("Drag to reorder")
        layout.addWidget(self._drag_handle)

        # Colour swatch (clickable)
        self._swatch = QPushButton()
        self._swatch.setFixedSize(16, 16)
        self._swatch.setFlat(True)
        self._swatch.setCursor(Qt.CursorShape.PointingHandCursor)
        self._swatch.setToolTip("Click to change colour")
        self._swatch.clicked.connect(self._pick_color)
        self._set_swatch_color(trace.color)
        layout.addWidget(self._swatch)

        # Visibility checkbox + label (double-click to rename)
        self._check = QCheckBox()
        self._check.setChecked(trace.visible)
        self._check.stateChanged.connect(
            lambda state: self.visibility_changed.emit(
                self._name, state == Qt.CheckState.Checked.value))
        layout.addWidget(self._check)

        self._label = QLabel(trace.label)
        self._label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._label.mouseDoubleClickEvent = lambda _e: self._start_rename()
        layout.addWidget(self._label)

        self._edit = QLineEdit()
        self._edit.setVisible(False)
        self._edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._edit.returnPressed.connect(self._finish_rename)
        self._edit.editingFinished.connect(self._finish_rename)
        layout.addWidget(self._edit)

        # Remove button
        self._rm_btn = QPushButton("✕")
        self._rm_btn.setFixedSize(18, 18)
        self._rm_btn.setFlat(True)
        self._rm_btn.setToolTip("Remove trace")
        self._rm_btn.clicked.connect(lambda: self.remove_requested.emit(self._name))
        layout.addWidget(self._rm_btn)

    # ── Public refresh ────────────────────────────────────────────────

    def refresh(self, trace: TraceModel):
        """Sync row appearance to the current trace state."""
        self._set_swatch_color(trace.color)
        self._check.blockSignals(True)
        self._check.setChecked(trace.visible)
        self._check.blockSignals(False)
        self._label.setText(trace.label)

    # ── Colour ────────────────────────────────────────────────────────

    def _set_swatch_color(self, hex_color: str):
        self._swatch.setStyleSheet(
            f"background-color: {hex_color}; border: 1px solid #555; border-radius: 2px;")

    def _pick_color(self):
        current = QColor(self._swatch.palette().button().color())
        color = QColorDialog.getColor(current, self, "Choose trace colour")
        if color.isValid():
            self._set_swatch_color(color.name())
            self.color_changed.emit(self._name, color.name())

    # ── Rename ────────────────────────────────────────────────────────

    def _start_rename(self):
        self._label.setVisible(False)
        self._edit.setText(self._label.text())
        self._edit.setVisible(True)
        self._edit.setFocus()
        self._edit.selectAll()

    def _finish_rename(self):
        new_label = self._edit.text().strip()
        self._edit.setVisible(False)
        self._label.setVisible(True)
        if new_label and new_label != self._label.text():
            self._label.setText(new_label)
            self.renamed.emit(self._name, new_label)

    # ── Context menu ──────────────────────────────────────────────────

    def contextMenuEvent(self, event):
        self.context_menu_requested.emit(self._name, event.globalPos())

    # ── Drag ─────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            handle_rect = self._drag_handle.geometry()
            if handle_rect.contains(event.pos()):
                self._drag_start_pos = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (event.buttons() & Qt.MouseButton.LeftButton
                and hasattr(self, '_drag_start_pos')):
            dist = (event.pos() - self._drag_start_pos).manhattanLength()
            if dist > QApplication.startDragDistance():
                self._start_drag()
        super().mouseMoveEvent(event)

    def _start_drag(self):
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(self._DRAG_MIME, self._name.encode())
        drag.setMimeData(mime)
        # Lightweight drag pixmap — just the row's current appearance
        pm = QPixmap(self.size())
        pm.fill(Qt.GlobalColor.transparent)
        self.render(pm)
        drag.setPixmap(pm)
        drag.setHotSpot(self._drag_handle.pos())
        drag.exec(Qt.DropAction.MoveAction)


# ── Channel list container ────────────────────────────────────────────────────

class ChannelListWidget(QWidget):
    """
    Scrollable list of ChannelRow widgets with drag-to-reorder support.

    Usage:
        panel = ChannelListWidget()
        panel.add_trace(trace_model)
        panel.visibility_changed.connect(plot_view.set_trace_visible)
        panel.color_changed.connect(lambda name, c: ...)
        panel.order_changed.connect(plot_view.reorder_traces)
    """

    visibility_changed    = pyqtSignal(str, bool)
    color_changed         = pyqtSignal(str, str)
    trace_removed         = pyqtSignal(str)
    trace_renamed         = pyqtSignal(str, str)
    order_changed         = pyqtSignal(list)
    context_menu_requested = pyqtSignal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: dict[str, ChannelRow] = {}
        self._order: list[str] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Top button bar: All / None
        btn_bar = QWidget()
        btn_layout = QHBoxLayout(btn_bar)
        btn_layout.setContentsMargins(4, 2, 4, 2)
        btn_layout.setSpacing(4)
        all_btn = QPushButton("All")
        all_btn.setFixedHeight(22)
        all_btn.clicked.connect(self._show_all)
        none_btn = QPushButton("None")
        none_btn.setFixedHeight(22)
        none_btn.clicked.connect(self._hide_all)
        btn_layout.addWidget(all_btn)
        btn_layout.addWidget(none_btn)
        btn_layout.addStretch()
        outer.addWidget(btn_bar)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        outer.addWidget(sep)

        # Scrollable row area
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._layout.addStretch()
        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll)

        # Drag-and-drop
        self._container.setAcceptDrops(True)
        self._container.dragEnterEvent = self._drag_enter
        self._container.dragMoveEvent  = self._drag_move
        self._container.dropEvent      = self._drop

    # ── Public API ────────────────────────────────────────────────────

    def add_trace(self, trace: TraceModel):
        if trace.name in self._rows:
            self._rows[trace.name].refresh(trace)
            return
        row = ChannelRow(trace)
        row.visibility_changed.connect(self.visibility_changed)
        row.color_changed.connect(self.color_changed)
        row.remove_requested.connect(self._on_remove)
        row.renamed.connect(self.trace_renamed)
        row.context_menu_requested.connect(self.context_menu_requested)
        self._rows[trace.name] = row
        self._order.append(trace.name)
        # Insert before the trailing stretch
        self._layout.insertWidget(self._layout.count() - 1, row)

    def remove_trace(self, name: str):
        row = self._rows.pop(name, None)
        if row:
            self._layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        if name in self._order:
            self._order.remove(name)

    def refresh_trace(self, trace: TraceModel):
        row = self._rows.get(trace.name)
        if row:
            row.refresh(trace)

    def get_ordered_names(self) -> list:
        return list(self._order)

    def set_trace_order(self, names: list):
        """Re-stack rows to match the given name order."""
        for name in names:
            row = self._rows.get(name)
            if row:
                self._layout.removeWidget(row)
                self._layout.insertWidget(self._layout.count() - 1, row)
        self._order = [n for n in names if n in self._rows]

    # ── Visibility helpers ─────────────────────────────────────────────

    def _show_all(self):
        for name, row in self._rows.items():
            row._check.setChecked(True)

    def _hide_all(self):
        for name, row in self._rows.items():
            row._check.setChecked(False)

    # ── Remove ────────────────────────────────────────────────────────

    def _on_remove(self, name: str):
        self.remove_trace(name)
        self.trace_removed.emit(name)

    # ── Drag-and-drop reorder ──────────────────────────────────────────

    def _drag_enter(self, event):
        if event.mimeData().hasFormat(ChannelRow._DRAG_MIME):
            event.acceptProposedAction()

    def _drag_move(self, event):
        if event.mimeData().hasFormat(ChannelRow._DRAG_MIME):
            event.acceptProposedAction()

    def _drop(self, event):
        if not event.mimeData().hasFormat(ChannelRow._DRAG_MIME):
            return
        src_name = event.mimeData().data(ChannelRow._DRAG_MIME).data().decode()
        src_row = self._rows.get(src_name)
        if not src_row:
            return

        # Find the target slot by y-position
        drop_y = event.position().y()
        target_idx = len(self._order)
        for i, name in enumerate(self._order):
            row = self._rows.get(name)
            if row and row.y() + row.height() / 2 > drop_y:
                target_idx = i
                break

        # Re-order internal list
        if src_name in self._order:
            self._order.remove(src_name)
        self._order.insert(target_idx, src_name)

        # Re-stack widgets to match
        self.set_trace_order(self._order)
        event.acceptProposedAction()
        self.order_changed.emit(list(self._order))
