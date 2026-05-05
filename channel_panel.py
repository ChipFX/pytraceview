"""
pytraceview/channel_panel.py
Full channel management panel: visibility, colour, groups, drag-to-reorder.

Portable companion to TraceView.  Manages ChannelRow widgets inside a
QListWidget with InternalMove drag so rows can be reordered within and
between groups.  Group headers, floor separators, and accent-tinted row
backgrounds give clear visual feedback about group membership.

Oscilloscope-specific controls (e.g. interpolation mode buttons) are
intentionally excluded.  Host applications that need them should subclass
ChannelPanel and override _setup_extra_button_rows().

Signals on ChannelPanel:
    visibility_changed(str, bool)           — trace_name, visible
    color_changed(str, str)                 — trace_name, new_hex_color
    trace_removed(str)                      — trace_name
    order_changed(list)                     — ordered list of trace names
    interp_changed(str, str)                — trace_name, mode  (from per-row actions)
    reset_color_requested(str)              — trace_name
    trace_renamed(str, str)                 — trace_name, new_label
    segment_changed(str)                    — trace_name
    group_renamed(str, str)                 — old_name, new_name
    unit_changed(str, str)                  — trace_name, new_unit
    trace_context_menu_requested(str, obj)  — trace_name, QPoint global
"""

import fnmatch

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QCheckBox, QScrollArea, QMenu, QColorDialog, QSizePolicy,
    QAbstractItemView, QListWidget, QListWidgetItem, QFrame,
    QDialog, QToolTip,
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize, QMimeData, QObject, QEvent, QPoint
from PyQt6.QtGui import QColor, QFont, QDrag, QPixmap, QPainter, QCursor
from typing import Dict, List, Set
from pytraceview.trace_model import TraceModel
from pytraceview.grouping_dialog import GroupingDialog


class _TooltipFilter(QObject):
    """Intercepts ToolTip events to set palette immediately before display.

    Qt inherits the widget's own background-color into QToolTip regardless of
    any QToolTip stylesheet rules.  Calling QToolTip.setPalette() + showText()
    inside the event handler is the only reliable way to override this.
    bg_fn and text_fn are callables so they always return the current value.
    """
    def __init__(self, bg_fn, text_fn, parent=None):
        super().__init__(parent)
        self._bg_fn   = bg_fn
        self._text_fn = text_fn

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.ToolTip:
            tip = obj.toolTip()
            if not tip:
                return False
            bg   = self._bg_fn()
            fg   = self._text_fn()
            # HTML table tooltip: Qt's rich-text renderer respects the table
            # cell's background-color, filling the content area regardless of
            # the palette or platform style.  This bypasses the QPalette /
            # QToolTip stylesheet route that Qt 6.7+ ignores on Windows 11.
            html = (f'<table cellspacing="0" cellpadding="3"'
                    f' style="background-color:{bg}; margin:0px;">'
                    f'<tr><td style="color:{fg};">{tip}</td></tr></table>')
            QToolTip.showText(event.globalPos(), html, obj)
            return True
        return False


class _LabelClickFilter(QObject):
    """Event filter installed on a QLabel so clicking it toggles a checkbox.
    Tracks press position; only toggles on release if the mouse didn't move
    (i.e. it was a click, not the start of a drag)."""

    def __init__(self, checkbox, parent=None):
        super().__init__(parent)
        self._chk = checkbox
        self._press_pos: QPoint | None = None

    def eventFilter(self, obj, event):
        t = event.type()
        if t == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.pos()
            return False   # let press propagate for drag initiation
        if t == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            if self._press_pos is not None:
                delta = event.pos() - self._press_pos
                if abs(delta.x()) < 6 and abs(delta.y()) < 6:
                    self._chk.toggle()
            self._press_pos = None
            return False
        return False


class ChannelRow(QWidget):
    """One row: color swatch + checkbox + label."""
    visibility_changed        = pyqtSignal(str, bool)
    color_changed             = pyqtSignal(str, str)
    remove_requested          = pyqtSignal(str)
    interp_changed            = pyqtSignal(str, str)
    reset_color               = pyqtSignal(str)         # name
    renamed                   = pyqtSignal(str, str)    # (trace_name, new_label)
    segment_changed           = pyqtSignal(str)         # trace_name — primary or viewmode changed
    unit_changed              = pyqtSignal(str, str)    # (trace_name, new_unit)
    context_menu_requested    = pyqtSignal(str, object) # (trace_name, QPoint global)

    def __init__(self, trace: TraceModel, parent=None):
        super().__init__(parent)
        self.trace = trace
        self.scroll_primaries: bool = False  # set by ChannelPanel
        self._panel_bg: str = "#0d0d0d"     # theme bg — used for tooltip bg
        self.setFixedHeight(32)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        # Let the QListWidgetItem's background brush show through the entire
        # row — including behind the drag handle and label — rather than only
        # leaking through gaps between child widgets.
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        # ── Group membership visuals ───────────────────────────────────────────
        # Left rail + right rail + background tint together create a "U-channel"
        # shape: the group header's bottom border is the lid, the two rails are
        # the sides, and the tint fills the interior.  All three elements are
        # transparent/neutral when the row is ungrouped.
        self._stripe_color = "#1e88e5"   # default accent; overwritten by set_palette

        self._group_stripe = QFrame()    # left rail
        self._group_stripe.setFixedWidth(4)
        self._group_stripe.setFrameShape(QFrame.Shape.NoFrame)
        self._group_stripe.setStyleSheet("background: transparent;")
        layout.addWidget(self._group_stripe)

        layout.addSpacing(3)   # gap between left rail and drag handle

        # Drag handle indicator
        grip = QLabel("⠿")
        grip.setStyleSheet("color: #555; font-size: 13px; background: transparent;")
        grip.setFixedWidth(12)
        layout.addWidget(grip)

        self.btn_color = QPushButton()
        self.btn_color.setFixedSize(18, 18)
        self._update_color_btn()
        self.btn_color.clicked.connect(self._pick_color)
        layout.addWidget(self.btn_color)

        self.chk_vis = QCheckBox()
        self.chk_vis.setChecked(trace.visible)
        self.chk_vis.setToolTip("Toggle visibility")
        self.chk_vis.stateChanged.connect(self._toggle_vis)
        layout.addWidget(self.chk_vis)

        self.lbl = QLabel(trace.label)
        self.lbl.setFont(QFont("Courier New", 9))
        self.lbl.setStyleSheet(self._lbl_css())
        self.lbl.setSizePolicy(QSizePolicy.Policy.Expanding,
                                QSizePolicy.Policy.Preferred)
        # No tooltip on the label itself — the ToolTip event propagates up to
        # the ChannelRow, whose filter handles it correctly.  Setting a tooltip
        # here would make Qt use the label's style context (background:
        # transparent) when rendering the popup, causing a dark-border bleed.
        self.lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        self._lbl_click_filter = _LabelClickFilter(self.chk_vis, self.lbl)
        self.lbl.installEventFilter(self._lbl_click_filter)
        # ChannelRow handles all tooltip events for the whole row via this
        # filter — covers label area, edges, and any child with no tooltip.
        self.setToolTip("Click to toggle visibility")
        self._row_tip_filter = _TooltipFilter(
            bg_fn=lambda: self._panel_bg,
            text_fn=lambda: self.trace.color,
            parent=self)
        self.installEventFilter(self._row_tip_filter)
        layout.addWidget(self.lbl)

        btn_del = QPushButton("✕")
        btn_del.setFixedSize(16, 16)
        btn_del.setToolTip("Remove trace")
        btn_del.setStyleSheet(
            "QPushButton { color: #884444; border: none; font-size: 9px; "
            "background: transparent; padding: 0; }"
            "QPushButton:hover { color: #ff6666; }")
        btn_del.clicked.connect(lambda: self.remove_requested.emit(self.trace.name))
        layout.addWidget(btn_del)

        layout.addSpacing(3)   # gap between delete button and right rail

        self._group_stripe_r = QFrame()  # right rail
        self._group_stripe_r.setFixedWidth(4)
        self._group_stripe_r.setFrameShape(QFrame.Shape.NoFrame)
        self._group_stripe_r.setStyleSheet("background: transparent;")
        layout.addWidget(self._group_stripe_r)

        self.set_grouped(bool(trace.col_group))

    def _update_color_btn(self):
        self.btn_color.setStyleSheet(
            f"background-color: {self.trace.color}; "
            f"border: 1px solid #666; border-radius: 2px;")

    def _pick_color(self):
        c = QColorDialog.getColor(QColor(self.trace.color), self)
        if c.isValid():
            self.trace.set_user_color(c.name())
            self._update_color_btn()
            self.lbl.setStyleSheet(self._lbl_css(self.trace.visible))
            self.color_changed.emit(self.trace.name, self.trace.color)

    def _toggle_vis(self, state):
        vis = bool(state)
        self.trace.visible = vis
        alpha = "1.0" if vis else "0.35"
        self.lbl.setStyleSheet(self._lbl_css(vis))
        self.visibility_changed.emit(self.trace.name, vis)

    def _lbl_css(self, visible: bool = True) -> str:
        alpha = "" if visible else " opacity: 0.35;"
        return f"color: {self.trace.color};{alpha} background: transparent;"

    def set_accent_color(self, color: str, panel_bg: str = "#0d0d0d"):
        """Update the accent colour used for the group stripe and tooltip bg."""
        self._stripe_color = color or "#1e88e5"
        self._panel_bg = panel_bg
        if bool(self.trace.col_group):
            self.set_grouped(True)   # repaint with new colour
        self.lbl.setStyleSheet(self._lbl_css(self.trace.visible))

    def set_grouped(self, grouped: bool):
        """Apply or remove the group membership visual: left rail, right rail,
        and background tint.

        Together they create a U-channel shape framing the channel row.  The
        group header's bottom border serves as the lid.  All three elements are
        transparent / neutral when the row is ungrouped so layout is stable.
        """
        rail_css = (f"background: {self._stripe_color};"
                    if grouped else "background: transparent;")
        self._group_stripe.setStyleSheet(rail_css)
        self._group_stripe_r.setStyleSheet(rail_css)
        # Row background tint is handled at the QListWidgetItem level by the
        # panel via _update_item_backgrounds() — item.setBackground() is the
        # only painting that Qt's list widget doesn't overpaint.

    def refresh(self):
        self.lbl.setText(self.trace.label)
        self.lbl.setStyleSheet(self._lbl_css(self.trace.visible))
        self._update_color_btn()
        self.set_grouped(bool(self.trace.col_group))

    def contextMenuEvent(self, event):
        self.context_menu_requested.emit(self.trace.name, event.globalPos())

    def _rename(self):
        from PyQt6.QtWidgets import QInputDialog
        new_label, ok = QInputDialog.getText(
            self, "Rename Trace", "New label:", text=self.trace.label)
        if ok and new_label.strip():
            new_label = new_label.strip()
            self.trace.label = new_label
            self.lbl.setText(new_label)
            self.renamed.emit(self.trace.name, new_label)

    def _change_unit(self):
        from PyQt6.QtWidgets import QInputDialog
        new_unit, ok = QInputDialog.getText(
            self, "Change Unit", "New unit:", text=self.trace.unit)
        if ok:
            new_unit = new_unit.strip()
            self.trace.unit = new_unit
            if self.trace.scaling:
                self.trace.scaling.unit = new_unit
            self.unit_changed.emit(self.trace.name, new_unit)

    def _set_interp(self, mode: str):
        self.trace._interp_mode_override = mode
        self.interp_changed.emit(self.trace.name, mode)

    def _set_primary_segment(self, idx):
        self.trace.primary_segment = idx
        self.segment_changed.emit(self.trace.name)

    def _set_viewmode(self, mode: str):
        self.trace.non_primary_viewmode = mode
        self.segment_changed.emit(self.trace.name)

    def wheelEvent(self, event):
        segs = getattr(self.trace, 'segments', None)
        cur = getattr(self.trace, 'primary_segment', None)
        if (self.scroll_primaries and segs and len(segs) >= 2
                and cur is not None and 0 <= cur < len(segs)):
            delta = event.angleDelta().y()
            step = 1 if delta < 0 else -1
            new_idx = max(0, min(len(segs) - 1, cur + step))
            if new_idx != cur:
                self.trace.primary_segment = new_idx
                self.segment_changed.emit(self.trace.name)
            event.accept()
        else:
            super().wheelEvent(event)


class _ChannelGroupHeader(QWidget):
    """Group header bar.
    Left-click  → fold / unfold
    Double-click → toggle visibility of all channels in group
    Right-click  → context menu (Show All / Hide All / Rename / Change All Units)
    """
    rename_requested                   = pyqtSignal(str)        # group_name
    change_all_units_requested         = pyqtSignal(str, str)   # group_name, new_unit
    move_up_requested                  = pyqtSignal(str)        # group_name
    move_down_requested                = pyqtSignal(str)        # group_name
    delete_group_requested             = pyqtSignal(str)        # group_name
    delete_group_and_channels_requested = pyqtSignal(str)       # group_name

    _BTN = (
        "QPushButton {{ font-size: {fs}px; color: {fg}; border: none; "
        "background: transparent; padding: 0; }} "
        "QPushButton:hover {{ color: {hfg}; }}")

    def __init__(self, group_name: str, rows_ref: list,
                 on_toggle_collapse, parent=None):
        super().__init__(parent)
        self.group_name   = group_name
        self._rows_ref    = rows_ref           # shared list; populated after creation
        self._collapsed   = False
        self._on_toggle   = on_toggle_collapse
        self.setFixedHeight(30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Let the QListWidgetItem's background brush show through — Qt's stylesheet
        # cascade on QListWidget overrides widget-level palette painting, so the
        # header background is handled via item.setBackground() in the panel.
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

        hl = QHBoxLayout(self)
        hl.setContentsMargins(6, 2, 4, 2)
        hl.setSpacing(4)

        # Fold indicator — non-interactive, part of the click-to-fold area
        self._lbl_arrow = QLabel("▼")
        self._lbl_arrow.setFixedWidth(14)
        hl.addWidget(self._lbl_arrow)

        self._lbl_name = QLabel(group_name)
        self._lbl_name.setFont(QFont("Courier New", 9))
        hl.addWidget(self._lbl_name, 1)

        self._btn_all = QPushButton("✓")
        self._btn_all.setFixedSize(22, 22)
        self._btn_all.setToolTip("Enable all in group  (right-click for more)")
        self._btn_all.clicked.connect(
            lambda: [r.chk_vis.setChecked(True) for r in self._rows_ref])
        hl.addWidget(self._btn_all)

        self._btn_none = QPushButton("✕")
        self._btn_none.setFixedSize(22, 22)
        self._btn_none.setToolTip("Disable all in group  (right-click for more)")
        self._btn_none.clicked.connect(
            lambda: [r.chk_vis.setChecked(False) for r in self._rows_ref])
        hl.addWidget(self._btn_none)

        # Tooltip colours for this header — updated in set_accent_color.
        self._tip_bg   = "#0d0d0d"
        self._tip_text = "#e0e0e0"
        self._btn_all_tip_filter = _TooltipFilter(
            bg_fn=lambda: self._tip_bg, text_fn=lambda: self._tip_text,
            parent=self)
        self._btn_all.installEventFilter(self._btn_all_tip_filter)
        self._btn_none_tip_filter = _TooltipFilter(
            bg_fn=lambda: self._tip_bg, text_fn=lambda: self._tip_text,
            parent=self)
        self._btn_none.installEventFilter(self._btn_none_tip_filter)

        # Apply default accent (overwritten by panel's set_palette immediately)
        self.set_accent_color("#1e88e5")

    # ── Theme ─────────────────────────────────────────────────────────────────

    def set_accent_color(self, accent: str, bg: str = "#0d0d0d",
                         text: str = "#e0e0e0"):
        """Repaint the header using theme colours — no hardcoded values.

        Background = theme accent colour, painted at the QListWidgetItem level
        by the panel via item.setBackground() — that is the only layer Qt does
        not overpaint when a QListWidget has a stylesheet active.  This widget
        itself is transparent; text and border use the theme colours directly.

        Text/arrow = theme background colour (natural contrast on the accent).
        Bottom border = slightly darker accent (visual floor between header
        and the first member row below it).
        Tooltips = theme text on theme bg (QToolTip rule must be on the button
        itself, not the parent, to override the app palette reliably).
        """
        darker = QColor(accent).darker(150)
        # Widget is transparent — item-level brush provides the background.
        # QToolTip rule on a QWidget stylesheet cascades to child widget
        # tooltips; this is valid for QWidget subclasses (unlike QPushButton).
        self.setStyleSheet(
            f"background: transparent; border-bottom: 2px solid {darker.name()};"
            f" QToolTip {{ color: {text}; background-color: {bg};"
            f" border: 1px solid #808080; padding: 2px; }}")
        # Text = theme bg colour so it contrasts with the accent background.
        text_css = (f"color: {bg}; font-weight: bold; "
                    "background: transparent; border: none;")
        arrow_css = (f"color: {bg}; font-size: 11px; "
                     "background: transparent; border: none;")
        self._lbl_arrow.setStyleSheet(arrow_css)
        self._lbl_name.setStyleSheet(text_css)
        self._btn_all.setStyleSheet(
            self._BTN.format(fs=13, fg=bg, hfg=accent))
        self._btn_none.setStyleSheet(
            self._BTN.format(fs=13, fg=bg, hfg=accent))
        # Update stored tooltip colours so the _TooltipFilter lambdas pick
        # up the new theme values on the next hover.
        self._tip_bg   = bg
        self._tip_text = text

    # ── Click handling ────────────────────────────────────────────────────────

    def _toggle(self):
        self._collapsed = not self._collapsed
        self._lbl_arrow.setText("▶" if self._collapsed else "▼")
        self._on_toggle(self.group_name, self._collapsed)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(event.pos())
            if not isinstance(child, QPushButton):
                self._toggle()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(event.pos())
            if not isinstance(child, QPushButton):
                # Toggle: if any visible → hide all; if all hidden → show all
                any_vis = any(r.chk_vis.isChecked() for r in self._rows_ref)
                for r in self._rows_ref:
                    r.chk_vis.setChecked(not any_vis)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction("Show All").triggered.connect(
            lambda: [r.chk_vis.setChecked(True) for r in self._rows_ref])
        menu.addAction("Hide All").triggered.connect(
            lambda: [r.chk_vis.setChecked(False) for r in self._rows_ref])
        menu.addSeparator()
        menu.addAction("Move Group Up").triggered.connect(
            lambda: self.move_up_requested.emit(self.group_name))
        menu.addAction("Move Group Down").triggered.connect(
            lambda: self.move_down_requested.emit(self.group_name))
        menu.addSeparator()
        menu.addAction("Rename Group…").triggered.connect(
            lambda: self.rename_requested.emit(self.group_name))
        menu.addSeparator()
        menu.addAction("Change All Units…").triggered.connect(
            self._change_all_units)
        menu.addSeparator()
        menu.addAction("Delete Group").triggered.connect(
            lambda: self.delete_group_requested.emit(self.group_name))
        menu.addAction("Delete Group and Channels").triggered.connect(
            lambda: self.delete_group_and_channels_requested.emit(self.group_name))
        menu.exec(event.globalPos())

    def _change_all_units(self):
        from PyQt6.QtWidgets import QInputDialog
        current = next((r.trace.unit for r in self._rows_ref), "")
        new_unit, ok = QInputDialog.getText(
            self, "Change All Units",
            f"New unit for all {len(self._rows_ref)} channel(s) in '{self.group_name}':",
            text=current)
        if ok:
            new_unit = new_unit.strip()
            for r in self._rows_ref:
                r.trace.unit = new_unit
                if r.trace.scaling:
                    r.trace.scaling.unit = new_unit
            self.change_all_units_requested.emit(self.group_name, new_unit)


# ── Channel-panel sentinel for group-header list items ─────────────────────────
_GROUP_HEADER_ROLE = Qt.ItemDataRole.UserRole + 1   # item.data(this) == group name for headers
_GROUP_SEP_ROLE    = Qt.ItemDataRole.UserRole + 2   # thin floor bar after each group's last member


class ChannelPanel(QWidget):
    """
    Drag-to-reorder channel list with group management.
    Uses a QListWidget with InternalMove drag so rows can be reordered
    within and between groups.  Group headers appear as non-draggable
    separator rows with enable/disable-all and collapse/expand buttons.

    Subclass hook
    -------------
    Override _setup_extra_button_rows(layout) to inject additional button
    rows above the "New Group / Group…" controls.  Called once during
    __init__ with the panel's QVBoxLayout.  The base implementation is a
    no-op.  See TraceLab's core/channel_panel.py for an example.
    """

    visibility_changed          = pyqtSignal(str, bool)
    color_changed               = pyqtSignal(str, str)
    trace_removed               = pyqtSignal(str)
    order_changed               = pyqtSignal(list)
    interp_changed              = pyqtSignal(str, str)
    reset_color_requested       = pyqtSignal(str)     # trace name
    trace_renamed               = pyqtSignal(str, str)  # (trace_name, new_label)
    segment_changed             = pyqtSignal(str)       # trace_name
    group_renamed               = pyqtSignal(str, str)  # (old_name, new_name)
    unit_changed                = pyqtSignal(str, str)  # (trace_name, new_unit)
    trace_context_menu_requested = pyqtSignal(str, object)  # (trace_name, QPoint global)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(170)
        self.setMaximumWidth(405)
        self._pv: dict = {}          # plotview palette — set via set_palette()
        self._font_scale: float = 1.0
        self._rows: Dict[str, ChannelRow] = {}   # name -> ChannelRow
        self._trace_order: List[str] = []        # names in display order
        self._group_rows: Dict[str, List[str]] = {}     # group -> [trace names]
        self._group_items: Dict[str, QListWidgetItem] = {}  # group -> header item
        self._group_hdr_rows: Dict[str, List] = {}      # group -> [ChannelRow refs]
        self._scroll_primaries: bool = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._header = QLabel("CHANNELS")
        self._header.setStyleSheet(
            "background: #1a1a1a; color: #888; padding: 5px 8px; "
            "font-size: 10px; font-weight: bold; letter-spacing: 1px;")
        self._header.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._header)

        # QListWidget provides built-in drag reorder
        self._list = QListWidget()
        self._list.setDragDropMode(
            QAbstractItemView.DragDropMode.InternalMove)
        self._list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setSpacing(1)
        self._list.setStyleSheet(
            "QListWidget { background: transparent; border: none; }"
            "QListWidget::item { padding: 0px; }")
        self._list.model().rowsMoved.connect(self._on_rows_moved)
        # Selection highlight is applied via item.setBackground() so grouped
        # items can suppress it (keeping the accent tint instead).
        self._list.itemSelectionChanged.connect(self._update_item_backgrounds)
        layout.addWidget(self._list)

        ctrl = QHBoxLayout()
        ctrl.setContentsMargins(4, 4, 4, 4)
        ctrl.setSpacing(3)
        btn_all  = QPushButton("All")
        btn_none = QPushButton("None")
        btn_all.clicked.connect(lambda: self._set_all_visible(True))
        btn_none.clicked.connect(lambda: self._set_all_visible(False))
        ctrl.addWidget(btn_all)
        ctrl.addWidget(btn_none)
        layout.addLayout(ctrl)

        # Hook for subclasses to add rows (e.g. interpolation buttons) here,
        # above the group controls.
        self._setup_extra_button_rows(layout)

        ctrl3 = QHBoxLayout()
        ctrl3.setContentsMargins(4, 0, 4, 4)
        ctrl3.setSpacing(3)
        self._btn_new_group = QPushButton("New Group")
        self._btn_new_group.setToolTip("Create a new empty group (then drag channels into it)")
        self._btn_new_group.clicked.connect(self._open_new_group_dialog)
        ctrl3.addWidget(self._btn_new_group)
        self._btn_group = QPushButton("Group…")
        self._btn_group.setToolTip("Group channels by unit or name pattern")
        self._btn_group.clicked.connect(self._open_grouping_dialog)
        ctrl3.addWidget(self._btn_group)
        layout.addLayout(ctrl3)

        self.set_font_scale(1.0)  # apply default inline styles
        # Defer initial minimum-width check until the widget has been laid out
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, self._update_minimum_width)

    # ── Subclass hook ─────────────────────────────────────────────────────────

    def _setup_extra_button_rows(self, layout: QVBoxLayout):
        """Override to add extra button rows above the group controls.

        Called once during __init__ with the panel's QVBoxLayout.  At call
        time the list widget and the All/None row are already in the layout;
        the New Group/Group… row has not yet been added.
        """

    # ── Theme ─────────────────────────────────────────────────────────────────

    def _insert_group_header(self, group: str, at_row: int):
        """Insert a non-draggable group header item at the given list row."""
        if group not in self._group_hdr_rows:
            self._group_hdr_rows[group] = []
        hdr_widget = _ChannelGroupHeader(
            group, self._group_hdr_rows[group],
            on_toggle_collapse=self._on_group_collapse)
        hdr_widget.rename_requested.connect(self._on_group_rename)
        hdr_widget.change_all_units_requested.connect(self._on_group_change_units)
        hdr_widget.move_up_requested.connect(lambda g: self._move_group(g, -1))
        hdr_widget.move_down_requested.connect(lambda g: self._move_group(g, +1))
        hdr_widget.delete_group_requested.connect(self._delete_group)
        hdr_widget.delete_group_and_channels_requested.connect(
            self._delete_group_and_channels)
        hdr_widget.set_accent_color(
            self._pv.get("accent", "#1e88e5"),
            self._pv.get("bg",     "#0d0d0d"),
            self._pv.get("text",   "#e0e0e0"))
        item = QListWidgetItem()
        item.setData(_GROUP_HEADER_ROLE, group)       # marks it as a group header
        item.setSizeHint(QSize(0, 30))
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)      # no drag, no select
        self._list.insertItem(at_row, item)
        self._list.setItemWidget(item, hdr_widget)
        self._group_items[group] = item

    def _find_group_insert_pos(self, group: str) -> int:
        """Row index AFTER the last existing member of `group`, or end of list."""
        members = set(self._group_rows.get(group, []))
        last = -1
        for i in range(self._list.count()):
            it = self._list.item(i)
            if it and it.data(Qt.ItemDataRole.UserRole) in members:
                last = i
        return last + 1 if last >= 0 else self._list.count()

    def set_palette(self, pv: dict):
        """Apply the plotview palette to the header and button area."""
        self._pv = dict(pv)
        bg = pv.get("bg_panel", "#141414")
        fg = pv.get("text",     "#e0e0e0")
        self._header.setStyleSheet(
            f"background: {bg}; color: {fg}; padding: 5px 8px; "
            f"font-size: 10px; font-weight: bold; letter-spacing: 1px;")
        self._apply_button_styles()
        # Propagate accent colour to all existing rows and group headers.
        accent = pv.get("accent", "#1e88e5")
        bg     = pv.get("bg",     "#0d0d0d")
        for row in self._rows.values():
            row.set_accent_color(accent, bg)
        for grp_item in self._group_items.values():
            hdr = self._list.itemWidget(grp_item)
            if hdr and hasattr(hdr, "set_accent_color"):
                hdr.set_accent_color(accent, pv.get("bg",   "#0d0d0d"),
                                     pv.get("text", "#e0e0e0"))
        self._update_item_backgrounds()
        self._update_group_separators()
        # Push a QToolTip background rule into the list widget's stylesheet so
        # it cascades to all embedded item widgets (channel rows, header bars).
        # Text colour is intentionally absent — each child's own `color`
        # property provides it (giving channel-coloured text on trace labels).
        _bg  = pv.get("bg",     "#0d0d0d")
        _bdr = pv.get("border", "#2a2a2a")
        self._list.setStyleSheet(
            "QListWidget { background: transparent; border: none; }"
            "QListWidget::item { padding: 0px; }"
            f"QToolTip {{ background-color: {_bg};"
            f" border: 1px solid {_bdr}; padding: 2px; }}")

    def set_font_scale(self, scale: float):
        """Store scale and rebuild button styles."""
        self._font_scale = scale
        self._apply_button_styles()

    def _apply_button_styles(self):
        """Rebuild button inline styles from stored palette and scale.

        Subclasses that add extra buttons should call super() and then style
        their own widgets.
        """
        fs = max(8, int(round(11 * self._font_scale * 0.9)))
        self._btn_group.setStyleSheet(f"font-size: {fs}px;")
        self._btn_new_group.setStyleSheet(f"font-size: {fs}px;")
        self._update_minimum_width()

    def _update_minimum_width(self):
        """Expand the panel if the group control buttons don't fit.

        Subclasses with wider button rows should override this to check their
        own buttons instead.
        """
        for btn in (self._btn_new_group, self._btn_group):
            btn.ensurePolished()
        # ctrl3 layout: 4px left + 3px spacing + 4px right = 11px overhead
        needed = (self._btn_new_group.sizeHint().width()
                  + self._btn_group.sizeHint().width()
                  + 11)
        self.setMinimumWidth(max(170, needed))

    def add_trace(self, trace: TraceModel):
        if trace.name in self._rows:
            self._rows[trace.name].refresh()
            return
        row = ChannelRow(trace)
        row.set_accent_color(self._pv.get("accent", "#1e88e5"),
                             self._pv.get("bg",     "#0d0d0d"))
        row.scroll_primaries = self._scroll_primaries
        row.visibility_changed.connect(self.visibility_changed)
        row.color_changed.connect(self.color_changed)
        row.remove_requested.connect(self._on_remove)
        row.interp_changed.connect(self.interp_changed)
        row.reset_color.connect(self.reset_color_requested)
        row.renamed.connect(self.trace_renamed)
        row.segment_changed.connect(self.segment_changed)
        row.unit_changed.connect(self.unit_changed)
        row.context_menu_requested.connect(self.trace_context_menu_requested)

        group = getattr(trace, "col_group", "") or ""

        if group:
            if group not in self._group_rows:
                self._group_rows[group] = []
            if group not in self._group_items:
                # First trace in this group — insert header, then trace below it
                insert_at = self._list.count()
                self._insert_group_header(group, insert_at)
                insert_at += 1
            else:
                insert_at = self._find_group_insert_pos(group)
            self._group_rows[group].append(trace.name)
            if group in self._group_hdr_rows:
                self._group_hdr_rows[group].append(row)

            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, trace.name)
            item.setSizeHint(QSize(0, 32))
            self._list.insertItem(insert_at, item)
            self._list.setItemWidget(item, row)
        else:
            # Ungrouped: append at end
            item = QListWidgetItem(self._list)
            item.setData(Qt.ItemDataRole.UserRole, trace.name)
            item.setSizeHint(QSize(0, 32))
            self._list.setItemWidget(item, row)

        self._rows[trace.name] = row
        self._trace_order.append(trace.name)
        # Defer visual update — coalesces rapid batch-add calls into one pass
        from PyQt6.QtCore import QTimer as _QT
        _QT.singleShot(0, self._update_group_visuals)

    def remove_trace(self, trace_name: str):
        if trace_name not in self._rows:
            return
        # Remove from group tracking
        for grp, names in list(self._group_rows.items()):
            if trace_name in names:
                names.remove(trace_name)
                # Mutate the shared list in place — _ChannelGroupHeader holds
                # a reference to this same list object via _rows_ref, so
                # replacing it with a new list would leave the header buttons
                # iterating a stale copy still containing the deleted row.
                if grp in self._group_hdr_rows:
                    hdr_list = self._group_hdr_rows[grp]
                    for r in [r for r in hdr_list if r.trace.name == trace_name]:
                        hdr_list.remove(r)
                # If group is now empty, remove its header too
                if not names:
                    hdr_item = self._group_items.pop(grp, None)
                    if hdr_item:
                        row = self._list.row(hdr_item)
                        if row >= 0:
                            self._list.takeItem(row)
                    self._group_rows.pop(grp, None)
                    self._group_hdr_rows.pop(grp, None)
                break
        # Remove the channel row item
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == trace_name:
                self._list.takeItem(i)
                break
        self._rows.pop(trace_name, None)
        if trace_name in self._trace_order:
            self._trace_order.remove(trace_name)
        self._update_group_visuals()

    def _on_group_collapse(self, group: str, collapsed: bool):
        """Show/hide list items belonging to `group`."""
        members = set(self._group_rows.get(group, []))
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) in members:
                item.setHidden(collapsed)

    def refresh_all(self):
        for row in self._rows.values():
            row.refresh()

    def get_ordered_names(self) -> List[str]:
        """Return trace names in current display order (skips group headers)."""
        names = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item:
                name = item.data(Qt.ItemDataRole.UserRole)
                if name is not None:   # None means it's a group header
                    names.append(name)
        return names

    def _on_remove(self, name: str):
        self.remove_trace(name)
        self.trace_removed.emit(name)

    def _on_rows_moved(self, src_parent, src_start, src_end,
                       dst_parent, dst_row):
        # Determine the final position of the moved item.  rowsMoved fires
        # after Qt has already updated the model; dst_row is pre-removal.
        count    = src_end - src_start + 1
        final_pos = dst_row - count if src_start < dst_row else dst_row

        moved_item = self._list.item(final_pos)
        if moved_item is None:
            return
        name = moved_item.data(Qt.ItemDataRole.UserRole)
        if name is None:
            return   # shouldn't happen — only channel items are draggable
        row = self._rows.get(name)
        if row is None:
            return

        # Determine the new group by scanning backwards from the moved item.
        # A floor separator marks the end of a group's territory; hitting one
        # before a header means the drop landed in ungrouped space.
        new_group = ""
        for i in range(final_pos - 1, -1, -1):
            above = self._list.item(i)
            if above is None:
                continue
            if above.data(_GROUP_SEP_ROLE) is not None:
                new_group = ""   # landed below a group's floor — ungrouped
                break
            g = above.data(_GROUP_HEADER_ROLE)
            if g is not None:
                new_group = g
                break

        self._trace_order = self.get_ordered_names()
        old_group = row.trace.col_group or ""

        if old_group == new_group:
            self._update_group_visuals()
            self.order_changed.emit(self._trace_order)
            return

        # Apply membership change for this item only.  Only the dragged item
        # changes group; every other item keeps its existing col_group.
        row.trace.col_group = new_group or ""
        row.set_grouped(bool(new_group))

        # Remove from old group (mutate in place so header _rows_ref stays valid).
        # Empty groups are intentionally kept — the user may drag channels back in.
        if old_group:
            names_list = self._group_rows.get(old_group, [])
            if name in names_list:
                names_list.remove(name)
            hdr_list = self._group_hdr_rows.get(old_group)
            if hdr_list is not None:
                for r in [r for r in hdr_list if r.trace.name == name]:
                    hdr_list.remove(r)

        # Add to new group (mutate in place).
        if new_group:
            if new_group not in self._group_rows:
                self._group_rows[new_group] = []
                self._group_hdr_rows[new_group] = []
            names_list = self._group_rows[new_group]
            if name not in names_list:
                names_list.append(name)
            hdr_list = self._group_hdr_rows.get(new_group)
            if hdr_list is not None and row not in hdr_list:
                hdr_list.append(row)

        self._update_group_visuals()
        self.order_changed.emit(self._trace_order)

    def _delete_group(self, group_name: str):
        """Remove the group header; orphan its channels in place (ungrouped)."""
        # Clear col_group on every member so they float ungrouped,
        # and refresh the row so the indentation reverts immediately.
        for tname in list(self._group_rows.get(group_name, [])):
            if tname in self._rows:
                self._rows[tname].trace.col_group = ""
                self._rows[tname].refresh()

        # Remove the header item from the list widget
        hdr_item = self._group_items.pop(group_name, None)
        if hdr_item:
            row_idx = self._list.row(hdr_item)
            if row_idx >= 0:
                self._list.takeItem(row_idx)

        # Clean up tracking structures
        self._group_rows.pop(group_name, None)
        self._group_hdr_rows.pop(group_name, None)

        self._trace_order = self.get_ordered_names()
        self._update_group_visuals()
        self.order_changed.emit(self._trace_order)

    def _delete_group_and_channels(self, group_name: str):
        """Remove the group header and all channels that belong to it."""
        members = list(self._group_rows.get(group_name, []))
        for tname in members:
            self.remove_trace(tname)
            self.trace_removed.emit(tname)
        # remove_trace cleans up the group header when the last member leaves

    def _open_new_group_dialog(self):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "New Group", "Group name:")
        if not ok or not name.strip():
            return
        name = self._unique_group_name(name.strip())
        self._create_empty_group(name)

    def _create_empty_group(self, name: str):
        """Insert an empty group header at the top of the list.

        The user can then drag channels from elsewhere into it.
        The header is removed automatically if it's still empty when
        a drag completes (same cleanup as any other group).
        """
        if name in self._group_rows:
            return
        self._group_rows[name] = []
        self._group_hdr_rows[name] = []
        self._insert_group_header(name, 0)
        # Paint the new header item's background immediately — without this the
        # item has no brush set and the transparent widget makes text invisible.
        self._update_item_backgrounds()

    def _set_all_visible(self, visible: bool):
        for row in self._rows.values():
            row.chk_vis.setChecked(visible)

    def set_scroll_primaries(self, enabled: bool):
        """Enable/disable wheel-to-step-primary-segment on all rows."""
        self._scroll_primaries = enabled
        for row in self._rows.values():
            row.scroll_primaries = enabled

    # ── Grouping ──────────────────────────────────────────────────────────────

    def _full_rebuild(self):
        """Rebuild the list widget entirely from current trace.col_group state.

        Empty groups (headers with no current members) are preserved in the
        order they appeared before the rebuild, inserted at the top afterwards.
        """
        # Capture empty groups before clearing, in their current visual order
        empty_groups: List[str] = []
        seen_empty: set = set()
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item:
                g = item.data(_GROUP_HEADER_ROLE)
                if g is not None and not self._group_rows.get(g) and g not in seen_empty:
                    empty_groups.append(g)
                    seen_empty.add(g)

        ordered_traces = [self._rows[n].trace
                          for n in self._trace_order if n in self._rows]
        self._list.clear()
        self._rows.clear()
        self._trace_order.clear()
        self._group_rows.clear()
        self._group_items.clear()
        self._group_hdr_rows.clear()
        for trace in ordered_traces:
            self.add_trace(trace)

        # Re-insert empty group headers at the top in their original order
        for g in reversed(empty_groups):
            self._group_rows[g] = []
            self._group_hdr_rows[g] = []
            self._insert_group_header(g, 0)

        self._update_group_visuals()

    def _update_group_visuals(self):
        """Single call that refreshes both item backgrounds and floor separators."""
        self._update_item_backgrounds()
        self._update_group_separators()

    def _update_item_backgrounds(self):
        """Set QListWidgetItem background brush for every channel row and
        group header.

        Item-level background is painted by Qt before the item widget is
        drawn.  The widget's own background is transparent, so the item
        brush shows through — unlike widget-level palette/stylesheet
        which QListWidget overpaints.

        Group headers get a solid accent brush (full opacity).
        Grouped channel rows get a lightly tinted accent brush (~13% alpha).
        Ungrouped rows get a stronger tint when selected, transparent otherwise.
        """
        from PyQt6.QtGui import QBrush
        accent = self._pv.get("accent", "#1e88e5")

        # Group header: solid accent background
        hdr_brush = QBrush(QColor(accent))
        for grp_item in self._group_items.values():
            grp_item.setBackground(hdr_brush)

        # Channel rows: tinted accent for grouped, default for ungrouped.
        # Selection: grouped items keep the tint (drop the grey highlight);
        # ungrouped items show a stronger accent tint when selected — always
        # theme-derived so it works regardless of light/dark theme.
        c = QColor(accent)
        c.setAlpha(32)
        grouped_brush   = QBrush(c)
        c_sel = QColor(accent)
        c_sel.setAlpha(90)
        sel_brush       = QBrush(c_sel)
        ungrouped_brush = QBrush(Qt.GlobalColor.transparent)

        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is None:
                continue
            name = item.data(Qt.ItemDataRole.UserRole)
            if name is None:
                continue                     # group header or separator item
            row = self._rows.get(name)
            if row and row.trace.col_group:
                item.setBackground(grouped_brush)   # tint wins over selection
            elif item.isSelected():
                item.setBackground(sel_brush)
            else:
                item.setBackground(ungrouped_brush)

    def _update_group_separators(self):
        """Insert/refresh thick accent-coloured floor bars after each group's
        last member.  Together with the header (lid) and the row rails (sides)
        they form a closed box around each group — optically clear even to
        someone unfamiliar with the UI.

        Runs in one O(n) pass: remove all existing separators, scan for each
        group's last member row, then re-insert separators bottom-to-top so
        earlier indices stay valid.
        """
        from PyQt6.QtGui import QBrush

        # Remove all existing separator items
        i = 0
        while i < self._list.count():
            item = self._list.item(i)
            if item is not None and item.data(_GROUP_SEP_ROLE) is not None:
                self._list.takeItem(i)
            else:
                i += 1

        # Find the last list-row index belonging to each named group
        last_row: Dict[str, int] = {}
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is None:
                continue
            name = item.data(Qt.ItemDataRole.UserRole)
            if name is None:
                continue
            row = self._rows.get(name)
            if row and row.trace.col_group:
                last_row[row.trace.col_group] = i

        # Insert separators bottom-to-top so earlier row indices stay valid
        accent = self._pv.get("accent", "#1e88e5")
        for grp, last in sorted(last_row.items(), key=lambda x: x[1], reverse=True):
            sep = QListWidgetItem()
            sep.setData(_GROUP_SEP_ROLE, grp)
            sep.setSizeHint(QSize(0, 6))
            sep.setFlags(Qt.ItemFlag.ItemIsEnabled)   # not draggable/selectable
            c = QColor(accent)
            c.setAlpha(160)
            sep.setBackground(QBrush(c))
            self._list.insertItem(last + 1, sep)

    def _move_group(self, group_name: str, direction: int):
        """Swap group_name with the adjacent group above (direction=-1) or below (+1).

        Uses _trace_order reordering + _full_rebuild — safe, avoids the Qt
        C++ lifetime issues that direct takeItem/setItemWidget swap causes.
        Empty groups are preserved through the rebuild.
        """
        # Build named-group order from the current list (includes empty groups)
        group_order: List[str] = []
        seen: set = set()
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is None:
                continue
            g = item.data(_GROUP_HEADER_ROLE)
            if g is not None and g not in seen:
                group_order.append(g)
                seen.add(g)

        if group_name not in group_order:
            return
        idx = group_order.index(group_name)
        swap_idx = idx + direction
        if swap_idx < 0 or swap_idx >= len(group_order):
            return

        other_group = group_order[swap_idx]

        # Build _trace_order as an ordered list of "blocks" so that ungrouped
        # channels keep their exact positions relative to the groups around them.
        # A block is either ('group', name) or ('ungrouped', [trace_names]).
        # Only the two target group blocks are swapped; everything else stays put.
        by_group: Dict[str, List[str]] = {}
        blocks: List[tuple] = []
        ungrouped_run: List[str] = []
        last_group = ""

        for n in self._trace_order:
            row = self._rows.get(n)
            if not row:
                continue
            g = row.trace.col_group or ""
            if g:
                if ungrouped_run:
                    blocks.append(('ungrouped', list(ungrouped_run)))
                    ungrouped_run = []
                if g != last_group:
                    blocks.append(('group', g))
                    last_group = g
                by_group.setdefault(g, []).append(n)
            else:
                last_group = ""
                ungrouped_run.append(n)
        if ungrouped_run:
            blocks.append(('ungrouped', list(ungrouped_run)))

        # Swap only the two group blocks; ungrouped blocks stay in place.
        swap_positions = [i for i, b in enumerate(blocks)
                          if b[0] == 'group' and b[1] in (group_name, other_group)]
        if len(swap_positions) == 2:
            i, j = swap_positions
            blocks[i], blocks[j] = blocks[j], blocks[i]

        new_trace_order: List[str] = []
        for block in blocks:
            if block[0] == 'group':
                new_trace_order.extend(by_group.get(block[1], []))
            else:
                new_trace_order.extend(block[1])
        self._trace_order = new_trace_order

        self._full_rebuild()
        self.order_changed.emit(self._trace_order)

    def _open_grouping_dialog(self):
        existing = set(self._group_rows.keys())
        dlg = GroupingDialog(existing_group_names=existing, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        method, pattern, create_inside, custom_name = dlg.get_config()
        if method == 'unit':
            self._apply_group_by_unit(create_inside, custom_name)
        elif method == 'pattern':
            if not pattern:
                return
            self._apply_group_by_pattern(pattern, create_inside, custom_name)
        elif method == 'enabled':
            self._apply_group_enabled(custom_name)

    def _unique_group_name(self, base: str, also_exclude: set = None) -> str:
        """Return base if not already a group, else base_001 … base_999."""
        existing = set(self._group_rows.keys())
        if also_exclude:
            existing |= also_exclude
        if base not in existing:
            return base
        for i in range(1, 1000):
            candidate = f"{base}_{i:03d}"
            if candidate not in existing:
                return candidate
        return base

    def _on_group_rename(self, old_name: str):
        from PyQt6.QtWidgets import QInputDialog
        new_name, ok = QInputDialog.getText(
            self, "Rename Group", "New group name:", text=old_name)
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return
        new_name = new_name.strip()
        for tname in list(self._group_rows.get(old_name, [])):
            if tname in self._rows:
                self._rows[tname].trace.col_group = new_name
        self._full_rebuild()
        self.group_renamed.emit(old_name, new_name)

    def _on_group_change_units(self, group_name: str, new_unit: str):
        """Propagate unit_changed for all traces in the group."""
        for tname in self._group_rows.get(group_name, []):
            self.unit_changed.emit(tname, new_unit)

    def _apply_group_by_unit(self, create_inside: bool, custom_name: str = ""):
        ordered_traces = [self._rows[n].trace
                          for n in self._trace_order if n in self._rows]
        # Pre-compute target names to avoid calling unique_group_name per trace
        target_map: Dict[tuple, str] = {}   # (old_g, unit) → new col_group
        allocated: set = set()

        def _alloc(base: str) -> str:
            name = self._unique_group_name(base, allocated)
            allocated.add(name)
            return name

        if create_inside:
            group_units: Dict[str, Set[str]] = {}
            for trace in ordered_traces:
                g = trace.col_group or "__ungrouped__"
                unit = trace.unit.strip() or "Other"
                group_units.setdefault(g, set()).add(unit)
            for old_g, units in group_units.items():
                if len(units) <= 1:
                    continue  # homogeneous group → no split
                for unit in units:
                    suffix = f"{custom_name}_{unit}" if custom_name else unit
                    if old_g == "__ungrouped__":
                        base = suffix
                    else:
                        base = f"{old_g}_{suffix}"
                    target_map[(old_g, unit)] = _alloc(base)
            for trace in ordered_traces:
                key = (trace.col_group or "__ungrouped__",
                       trace.unit.strip() or "Other")
                if key in target_map:
                    trace.col_group = target_map[key]
        else:
            unit_target: Dict[str, str] = {}
            for trace in ordered_traces:
                unit = trace.unit.strip() or "Other"
                if unit not in unit_target:
                    base = f"{custom_name}_{unit}" if custom_name else unit
                    unit_target[unit] = _alloc(base)
            for trace in ordered_traces:
                trace.col_group = unit_target[trace.unit.strip() or "Other"]
        self._full_rebuild()

    def _apply_group_by_pattern(self, pattern: str, create_inside: bool,
                                custom_name: str = ""):
        pat_lower = pattern.lower()
        name_repr = pattern.replace('*', '(ALL)')
        allocated: set = set()

        def _alloc(base: str) -> str:
            name = self._unique_group_name(base, allocated)
            allocated.add(name)
            return name

        ordered_traces = [self._rows[n].trace
                          for n in self._trace_order if n in self._rows]

        def _matches(trace) -> bool:
            label = (trace.label or trace.name or "").lower()
            return fnmatch.fnmatch(label, pat_lower)

        if create_inside:
            group_has_nonmatch: Dict[str, bool] = {}
            for trace in ordered_traces:
                g = trace.col_group or "__ungrouped__"
                if not _matches(trace):
                    group_has_nonmatch[g] = True
            # Pre-compute one target name per source group that has mixed membership
            group_target: Dict[str, str] = {}
            for g, has_nm in group_has_nonmatch.items():
                suffix = custom_name or name_repr
                base = suffix if g == "__ungrouped__" else f"{g}_{suffix}"
                group_target[g] = _alloc(base)
            for trace in ordered_traces:
                if not _matches(trace):
                    continue
                old_g = trace.col_group or "__ungrouped__"
                if old_g in group_target:
                    trace.col_group = group_target[old_g]
        else:
            base = custom_name or f"Group_{name_repr}"
            group_name = _alloc(base)
            for trace in ordered_traces:
                if _matches(trace):
                    trace.col_group = group_name
        self._full_rebuild()

    def _apply_group_enabled(self, custom_name: str = ""):
        """Collect all currently-visible traces into one new group."""
        base = custom_name or "Enabled"
        group_name = self._unique_group_name(base)
        ordered_traces = [self._rows[n].trace
                          for n in self._trace_order if n in self._rows]
        for trace in ordered_traces:
            if trace.visible:
                trace.col_group = group_name
        self._full_rebuild()
