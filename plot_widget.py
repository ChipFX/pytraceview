"""
pytraceview/plot_widget.py
TraceView — the main plotting container widget.

Manages split (one lane per trace) and overlay (all traces on one plot)
display modes, cursors, persistence, retrigger overlays, zoom helpers,
and screenshot export.

Theme contract:
  Construction:  TraceView(theme=plot_theme, ...)
  Live update:   trace_view.apply_theme(new_plot_theme)
  The host application is responsible for connecting its theme-change
  signal to apply_theme.  TraceView does not hold a reference to any
  theme manager.
"""

import numpy as np
import pyqtgraph as pg
from pyqtgraph import InfiniteLine
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QScrollArea, QMenu,
                              QSizePolicy)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QEvent, QRectF
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QAction, QPen
from typing import List, Dict, Optional, Tuple

from pytraceview.plot_theme import PlotTheme, DEFAULT_PLOT_THEME
from pytraceview.trace_model import TraceModel
from pytraceview.draw_mode import (
    DEFAULT_DENSITY_PEN_MAPPING,
    DEFAULT_DRAW_MODE,
)
from pytraceview.display_items import (
    EngineeringAxisItem,
    EngineeringTimeAxisItem,
    RangeBar,
)
from pytraceview.render_utils import (
    TraceStyleContext,
    _style_context_from_plot_theme,
    _effective_color,
    _interpolated_trace_value,
    DEFAULT_LIMITS_CONFIG,
)
from pytraceview.trace_lane import TraceLane, OverlayTraceVisual


# ── Screenshot branding helper ────────────────────────────────────────────────

def _composite_branding(img: "QImage", svg_path: str) -> "QImage":
    """
    Render an SVG logo into the bottom-left corner of img.
    The logo is constrained to a max of 120px wide / 60px tall (before scale
    is applied at caller side) with 6px padding from the corner.
    Returns the composited QImage (may be a copy).
    """
    try:
        from PyQt6.QtSvg import QSvgRenderer
        from PyQt6.QtGui import QPainter, QImage
        from PyQt6.QtCore import QRectF, Qt

        renderer = QSvgRenderer(svg_path)
        if not renderer.isValid():
            return img

        # Target logo size: up to 160×80 px on the output image
        w_img, h_img = img.width(), img.height()
        logo_w = min(160, w_img // 6)
        logo_h = min(80,  h_img // 8)
        # Maintain SVG aspect ratio
        vp = renderer.viewBox()
        if vp.width() > 0 and vp.height() > 0:
            aspect = vp.width() / vp.height()
            if logo_w / aspect > logo_h:
                logo_w = int(logo_h * aspect)
            else:
                logo_h = int(logo_w / aspect)

        pad = 8
        x = pad
        y = h_img - logo_h - pad

        # Convert to ARGB for compositing if needed
        if img.format() != QImage.Format.Format_ARGB32_Premultiplied:
            img = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)

        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        renderer.render(painter, QRectF(x, y, logo_w, logo_h))
        painter.end()
    except Exception:
        pass  # branding failure must never crash a screenshot
    return img


# ── Main plot container ───────────────────────────────────────────────────────

class TraceView(QWidget):
    cursor_values_changed        = pyqtSignal(dict)
    sinc_active_changed          = pyqtSignal(bool)  # emitted when sinc kicks in/out
    view_changed                 = pyqtSignal()       # emitted (throttled) on pan/zoom
    trace_context_menu_requested = pyqtSignal(str, object)  # (trace_name, QPoint global)

    def __init__(self, theme: PlotTheme = None,
                 y_lock_auto: bool = True,
                 interp_mode: str = "linear",
                 viewport_min_pts: int = 1024,
                 draw_mode: str = DEFAULT_DRAW_MODE,
                 density_pen_mapping: Optional[dict] = None,
                 lane_label_size: int = 8,
                 show_lane_labels: bool = True,
                 allow_theme_force_labels: bool = False,
                 lane_label_spacing: float = 0.3,
                 limits_config: Optional[dict] = None,
                 parent=None):
        super().__init__(parent)
        self._shutting_down = False
        self._plot_theme: PlotTheme = theme if theme is not None else DEFAULT_PLOT_THEME
        self._draw_mode = draw_mode or DEFAULT_DRAW_MODE
        self._density_pen_mapping = dict(
            DEFAULT_DENSITY_PEN_MAPPING if density_pen_mapping is None
            else density_pen_mapping)
        self._style_context = _style_context_from_plot_theme(
            self._plot_theme, self._draw_mode, self._density_pen_mapping)
        self.y_lock_auto = y_lock_auto
        self.interp_mode = interp_mode
        self.viewport_min_pts = viewport_min_pts
        self._limits_config: dict = dict(limits_config) if limits_config else dict(DEFAULT_LIMITS_CONFIG)
        self._lane_label_size: int = lane_label_size
        self._show_lane_labels: bool = show_lane_labels
        self._allow_theme_force_labels: bool = allow_theme_force_labels
        self._lane_label_spacing: float = lane_label_spacing
        self._overlay_legend_items: list = []
        self._last_sinc_active = False
        self.traces: List[TraceModel] = []
        self._lanes: Dict[str, TraceLane] = {}
        self._mode = "split"
        self._cursors = {0: None, 1: None}
        self._cursor_colors = {
            0: self._style_context.plot_colors["cursor_a"],
            1: self._style_context.plot_colors["cursor_b"],
        }
        self._overlay_visuals: Dict[str, OverlayTraceVisual] = {}
        self._overlay_z_order: List[str] = []  # for bring-to-front
        # Scroll / min-height settings (updated via set_scroll_settings / set_min_lane_height)
        self._scroll_settings: dict = {}
        self._min_lane_height: int = 80
        self._y_axis_label_w: int = 60   # updated by set_y_axis_label_width
        # Persistence / retrigger state — reapplied after every rebuild
        self._persist_state: Dict[str, tuple] = {}       # name->(layers, t_ref)
        self._retrigger_curve_state: Dict[str, tuple] = {}  # name->(t_abs, data)
        # Segment rendering state — applied to new lanes on rebuild
        self._seg_process: bool = True
        self._seg_dim_opacity: float = 0.30
        self._seg_dash_pattern: Optional[list] = None
        # Smart scale settings — applied to new lanes on rebuild
        self._smart_scale_settings: dict = {}
        # Real-time axis settings — applied to new lanes on rebuild
        self._real_time_settings: dict = {}
        # Div sub-division settings — applied to new lanes on rebuild
        self._div_settings: dict = {}

        layout = QVBoxLayout(self)
        layout.setSpacing(1)
        layout.setContentsMargins(0, 0, 0, 0)

        # Split-lane scroll area
        self._lanes_container = QWidget()
        self._lanes_layout = QVBoxLayout(self._lanes_container)
        self._lanes_layout.setSpacing(1)
        self._lanes_layout.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._lanes_container)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self._scroll)

        # Overlay widget
        self._overlay_widget = pg.PlotWidget(
            background=self._style_context.plot_colors["background"])
        self._overlay_widget.hide()
        layout.addWidget(self._overlay_widget)
        self._setup_overlay()
        # Intercept wheel events on overlay viewport for modifier-key scroll
        self._overlay_widget.viewport().installEventFilter(self)

        # Range bar
        self._range_bar = RangeBar()
        self._range_bar.range_changed.connect(self._on_range_bar_changed)
        layout.addWidget(self._range_bar)

        # Timer to update range bar AND status bar (throttled to 100ms)
        self._range_timer = QTimer(self)
        self._range_timer.setSingleShot(True)
        self._range_timer.setInterval(100)
        self._range_timer.timeout.connect(self._update_range_bar)
        self._range_timer.timeout.connect(self.view_changed)

        # Debounce timer for visibility-driven rebuilds (0 ms = next event tick).
        # Coalesces rapid back-to-back set_trace_visible calls (e.g. "None" with
        # 32 channels) into a single _rebuild, preventing O(n²) widget churn.
        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(0)
        self._rebuild_timer.timeout.connect(self._rebuild)

    def begin_shutdown(self) -> None:
        """Stop timers/signals so late callbacks cannot hit torn-down widgets."""
        if self._shutting_down:
            return
        self._shutting_down = True
        try:
            self._range_timer.stop()
            self._rebuild_timer.stop()
        except Exception:
            pass
        # Disconnect pyqtgraph overlay sigRangeChanged — pyqtgraph emits this
        # internally during its own C++ teardown.  The connected lambdas call
        # self._range_timer.start() which crashes if the timer's C++ object is
        # already gone (destroyed earlier in the same parent-child sweep).
        # This is the primary cause of segfaults when closing with no data loaded.
        try:
            pi = self._overlay_widget.getPlotItem()
            pi.sigRangeChanged.disconnect()
        except Exception:
            pass
        # Disconnect per-lane view_range_changed → _range_timer.start() for the
        # same reason.
        for lane in list(self._lanes.values()):
            try:
                lane.view_range_changed.disconnect()
            except Exception:
                pass
        try:
            self._overlay_widget.viewport().removeEventFilter(self)
        except Exception:
            pass
        for lane in list(self._lanes.values()):
            try:
                lane.viewport().removeEventFilter(self)
            except Exception:
                pass

    def _setup_overlay(self):
        # Replace default axes with engineering-unit axes
        pi = self._overlay_widget.getPlotItem()
        self._ov_y_axis = EngineeringAxisItem(orientation="left")
        self._ov_x_axis = EngineeringTimeAxisItem(orientation="bottom")
        pi.setAxisItems({"left": self._ov_y_axis, "bottom": self._ov_x_axis})

        pi.showGrid(x=True, y=True, alpha=0.3)
        pi.setMenuEnabled(False)
        for ax_name in ("left", "bottom"):
            ax = pi.getAxis(ax_name)
            pen = pg.mkPen(color=self._style_context.plot_colors["text"], width=1)
            ax.setPen(pen)
            ax.setTextPen(pen)
        pi.sigRangeChanged.connect(lambda: self._range_timer.start())
        pi.sigRangeChanged.connect(self._on_overlay_range_changed)
        pi.sigRangeChanged.connect(self._reposition_overlay_legend)
        self._overlay_widget.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._overlay_widget.customContextMenuRequested.connect(
            self._overlay_context_menu)
        if self.y_lock_auto:
            pi.setMouseEnabled(x=True, y=False)

    def _apply_plot_theme_internal(self, plot_theme: PlotTheme):
        """Update internal style state from a new PlotTheme without touching lanes/visuals."""
        self._plot_theme = plot_theme
        self._style_context = _style_context_from_plot_theme(
            plot_theme, self._draw_mode, self._density_pen_mapping)
        plot_colors = self._style_context.plot_colors
        self._overlay_widget.setBackground(plot_colors["background"])
        pi = self._overlay_widget.getPlotItem()
        for ax_name in ("left", "bottom"):
            ax = pi.getAxis(ax_name)
            pen = pg.mkPen(color=plot_colors["text"], width=1)
            ax.setPen(pen)
            ax.setTextPen(pen)
        self._cursor_colors = {
            0: plot_colors["cursor_a"],
            1: plot_colors["cursor_b"],
        }

    def _refresh_cursor_styles(self):
        for cid, x_pos in self._cursors.items():
            if x_pos is not None:
                self._place_cursors(cid, x_pos)

    def apply_theme(self, plot_theme: PlotTheme):
        """Update the active theme and repaint all plot elements.

        Call this from your host application's theme-change handler:
            theme_manager.themeChanged.connect(
                lambda td: trace_view.apply_theme(td.to_plot_theme()))
        """
        if self._shutting_down:
            return
        self._apply_plot_theme_internal(plot_theme)
        rt_enabled = bool(self._real_time_settings.get("enabled"))
        for lane in self._lanes.values():
            lane.apply_theme(plot_theme)
            if rt_enabled:
                lane._x_axis.set_accent_color(plot_theme.accent)
        for visual in self._overlay_visuals.values():
            visual.apply_theme(plot_theme)
        if rt_enabled:
            self._ov_x_axis.set_accent_color(plot_theme.accent)
        self._rebuild_overlay_legend()
        self._refresh_cursor_styles()
        self._overlay_widget.update()
        self.update()
        self.repaint()

    def set_draw_mode(self, draw_mode: str):
        self._draw_mode = draw_mode or DEFAULT_DRAW_MODE
        self._style_context = _style_context_from_plot_theme(
            self._plot_theme, self._draw_mode, self._density_pen_mapping)
        for lane in self._lanes.values():
            lane.apply_style(self._style_context)
            lane.refresh_curve()
        for trace_name, visual in self._overlay_visuals.items():
            visual.apply_style(self._style_context)
            visual.interp_mode = getattr(
                next((t for t in self.traces if t.name == trace_name), None),
                '_interp_mode_override',
                self.interp_mode)
            visual.refresh_curve(self.get_current_view_range())
        self.update()
        self.repaint()

    def set_density_pen_mapping(self, density_pen_mapping: dict):
        self._density_pen_mapping = dict(density_pen_mapping or DEFAULT_DENSITY_PEN_MAPPING)
        self.set_draw_mode(self._draw_mode)

    def _on_overlay_range_changed(self):
        """Re-render overlay curves on every pan/zoom so viewport windowing stays correct."""
        if self._shutting_down:
            return
        self._refresh_overlay_visuals()

    def set_mode(self, mode: str):
        # Snapshot the current x window before switching so the new mode
        # inherits it — split and overlay use independent PlotItems.
        _x0, _x1 = self.get_current_view_range()
        self._mode = mode
        if mode == "overlay":
            self._scroll.hide()
            self._overlay_widget.show()
            self._rebuild_overlay(inherit_x=(_x0, _x1))
        else:
            self._overlay_widget.hide()
            self._scroll.show()
            self._rebuild_split(inherit_x=(_x0, _x1))

    def get_cursor_placement_x(self, cursor_id: int) -> float:
        x0, x1 = self.get_current_view_range()
        if self._mode == "overlay":
            fraction = 0.5 if cursor_id == 0 else 0.75
            return x0 + (x1 - x0) * fraction
        mid = (x0 + x1) / 2.0
        if cursor_id == 1 and self._cursors.get(0) is not None:
            return self._cursors[0] + (x1 - x0) * 0.1
        return mid

    def set_y_lock_auto(self, locked: bool):
        self.y_lock_auto = locked
        for lane in self._lanes.values():
            lane.set_y_lock_auto(locked)
        pi = self._overlay_widget.getPlotItem()
        pi.setMouseEnabled(x=True, y=not locked)
        if locked:
            pi.enableAutoRange(axis="y")

    def set_interp_mode(self, mode: str):
        """Switch global interpolation mode. Clears per-trace overrides.
        Updates existing lanes directly to avoid triggering auto-range."""
        self.interp_mode = mode
        for trace in self.traces:
            if hasattr(trace, '_interp_mode_override'):
                del trace._interp_mode_override
        # Update lanes in-place — avoids the enableAutoRange() that _rebuild() triggers
        vr = self.get_current_view_range()
        for lane in self._lanes.values():
            lane.interp_mode = mode
            lane.refresh_curve()
        for visual in self._overlay_visuals.values():
            visual.interp_mode = mode
            visual.refresh_curve(vr)
        self._sync_sinc_state()

    def add_trace(self, trace: TraceModel):
        # Allow overwrite if name already exists
        if trace.name in [t.name for t in self.traces]:
            for i, t in enumerate(self.traces):
                if t.name == trace.name:
                    self.traces[i] = trace
                    break
        else:
            self.traces.append(trace)
        self._rebuild()

    def batch_add_traces(self, new_traces: list):
        """Add or replace multiple traces with a single plot rebuild.
        Caller is responsible for all per-trace bookkeeping (colors, channel
        panel, etc.) before calling this — this method only updates self.traces
        and triggers one _rebuild() instead of one per trace."""
        for trace in new_traces:
            existing = [t.name for t in self.traces]
            if trace.name in existing:
                idx = existing.index(trace.name)
                self.traces[idx] = trace
            else:
                self.traces.append(trace)
        self._rebuild()

    def reorder_traces(self, name_order: list):
        """Reorder traces list to match channel panel order."""
        name_idx = {n: i for i, n in enumerate(name_order)}
        self.traces.sort(key=lambda t: name_idx.get(t.name, 999))
        self._rebuild()

    def clear_all(self):
        self.traces.clear()
        self._persist_state.clear()
        self._retrigger_curve_state.clear()
        self._rebuild()

    def remove_trace(self, trace_name: str):
        self.traces = [t for t in self.traces if t.name != trace_name]
        self._persist_state.pop(trace_name, None)
        self._retrigger_curve_state.pop(trace_name, None)
        self._rebuild()

    def set_trace_visible(self, trace_name: str, visible: bool):
        for t in self.traces:
            if t.name == trace_name:
                t.visible = visible
        # Defer the rebuild so rapid back-to-back calls (e.g. "None" button
        # on 32 channels) only trigger one rebuild instead of n² widget churn.
        self._rebuild_timer.start()

    def set_interp_mode_for_trace(self, trace_name: str, mode: str):
        """Set per-trace interpolation override."""
        for t in self.traces:
            if t.name == trace_name:
                t._interp_mode_override = mode
        lane = self._lanes.get(trace_name)
        if lane:
            lane.interp_mode = mode
            lane.refresh_curve()
        visual = self._overlay_visuals.get(trace_name)
        if visual:
            visual.interp_mode = mode
            visual.refresh_curve(self.get_current_view_range())

    def get_sinc_active(self) -> bool:
        """True if the active view currently uses viewport interpolation."""
        if self._mode == "overlay":
            return any(getattr(v, '_interpolated_view', False)
                       for v in self._overlay_visuals.values())
        return any(getattr(l, '_sinc_active', False)
                   for l in self._lanes.values())

    def _sync_sinc_state(self) -> None:
        sinc_now = self.get_sinc_active()
        if sinc_now != self._last_sinc_active:
            self._last_sinc_active = sinc_now
            self.sinc_active_changed.emit(sinc_now)

    def refresh_all(self):
        if self._shutting_down:
            return
        if self._mode == "split":
            for lane in self._lanes.values():
                lane.refresh_curve()
                if self.y_lock_auto:
                    lane.getPlotItem().enableAutoRange(axis="y")
        else:
            self._refresh_overlay_visuals()
        self._sync_sinc_state()

    def set_scroll_settings(self, settings: dict):
        """Update modifier-key scroll behaviour from Advanced UI settings."""
        self._scroll_settings = dict(settings)

    def set_min_lane_height(self, height: int):
        """Change minimum trace height; applies to all current and future lanes."""
        self._min_lane_height = max(40, int(height))
        for lane in self._lanes.values():
            lane.setMinimumHeight(self._min_lane_height)

    def set_smart_scale(self, settings: dict):
        """Propagate smart_scale settings to overlay axis and all split-mode lanes."""
        self._smart_scale_settings = dict(settings)
        self._ov_x_axis.set_smart_scale(settings)
        for lane in self._lanes.values():
            lane._x_axis.set_smart_scale(settings)
            lane.refresh_curve()

    def set_real_time(self, settings: dict):
        """Propagate real-time axis settings to overlay axis and all split-mode lanes."""
        self._real_time_settings = dict(settings)
        self._ov_x_axis.set_real_time(settings)
        for lane in self._lanes.values():
            lane._x_axis.set_real_time(settings)

    def set_div_settings(self, cfg: dict):
        """Propagate div sub-division settings to all axis items (X and Y, overlay and lanes)."""
        self._div_settings = dict(cfg)
        self._ov_x_axis.set_div_settings(cfg)
        self._ov_y_axis.set_div_settings(cfg)
        for lane in self._lanes.values():
            lane._x_axis.set_div_settings(cfg)
            lane._y_axis.set_div_settings(cfg)

    def set_process_segments(self, enabled: bool):
        """Enable/disable segment-aware rendering on all lanes and overlay visuals."""
        self._seg_process = enabled
        for lane in self._lanes.values():
            lane._process_segments = enabled
            lane.refresh_curve()
        vr = self.get_current_view_range()
        for visual in self._overlay_visuals.values():
            visual._process_segments = enabled
            visual.refresh_curve(vr)

    def set_segment_dim_opacity(self, opacity_pct: int):
        """Set dim opacity (10–90%) for non-primary segments and refresh."""
        opacity = max(0.1, min(0.9, opacity_pct / 100.0))
        self._seg_dim_opacity = opacity
        for lane in self._lanes.values():
            lane._segment_dim_opacity = opacity
            lane.refresh_curve()
        vr = self.get_current_view_range()
        for visual in self._overlay_visuals.values():
            visual._segment_dim_opacity = opacity
            visual.refresh_curve(vr)

    def set_segment_dash_pattern(self, dash_size: int, gap_size: int):
        """Set custom dash pattern for dashed non-primary segments and refresh."""
        pattern = [float(dash_size), float(gap_size)]
        self._seg_dash_pattern = pattern
        for lane in self._lanes.values():
            lane._segment_dash_pattern = pattern
            lane.refresh_curve()
        vr = self.get_current_view_range()
        for visual in self._overlay_visuals.values():
            visual._segment_dash_pattern = pattern
            visual.refresh_curve(vr)

    def eventFilter(self, obj, event):
        """Intercept wheel events on TraceLane / overlay viewports.
        Decides whether to zoom (pass to pyqtgraph) or scroll the lane list."""
        if event.type() != QEvent.Type.Wheel:
            return False

        s = self._scroll_settings
        zoom_on  = s.get("scroll_zoom_enabled", True)
        list_on  = s.get("scroll_list_enabled", True)
        default  = s.get("scroll_default", "zoom")
        mod_keys = s.get("scroll_modifier_keys", ["ctrl", "alt", "shift"])

        mods = event.modifiers()
        modifier_held = (
            ("ctrl"  in mod_keys and bool(mods & Qt.KeyboardModifier.ControlModifier)) or
            ("alt"   in mod_keys and bool(mods & Qt.KeyboardModifier.AltModifier))    or
            ("shift" in mod_keys and bool(mods & Qt.KeyboardModifier.ShiftModifier))
        )

        # Determine requested action
        if default == "zoom":
            action = "scroll_list" if modifier_held else "zoom"
        else:
            action = "zoom" if modifier_held else "scroll_list"

        if action == "scroll_list" and list_on and self._mode == "split":
            sb    = self._scroll.verticalScrollBar()
            delta = event.angleDelta().y()
            sb.setValue(sb.value() - delta * 3 // 8)
            return True   # consumed — pyqtgraph won't zoom

        if action == "zoom" and not zoom_on:
            return True   # zoom disabled — consume silently

        return False      # let pyqtgraph handle

    def set_limits_config(self, cfg: dict) -> None:
        """Hot-update the display-limit config and refresh all curves."""
        self._limits_config = dict(cfg)
        for lane in self._lanes.values():
            lane._limits_config = dict(cfg)
        for visual in self._overlay_visuals.values():
            visual._limits_config = dict(cfg)
        self.refresh_all()

    def _set_lanes_suppress(self, suppress: bool) -> None:
        """Block or unblock per-lane view-change redraws (used during batch zoom)."""
        for lane in self._lanes.values():
            lane._suppress_view_redraws = suppress

    def _rebuild(self):
        if self._shutting_down:
            return
        if self._mode == "split":
            self._rebuild_split()
        else:
            self._rebuild_overlay()

    def _defer_lane_refresh(self, lane: TraceLane) -> None:
        """Queue a lane refresh on the next tick, owned by the lane itself."""
        if self._shutting_down:
            return
        timers = getattr(lane, "_deferred_refresh_timers", None)
        if timers is None:
            timers = []
            lane._deferred_refresh_timers = timers
        timer = QTimer(lane)
        timer.setSingleShot(True)
        timer.setInterval(0)

        def _fire():
            try:
                timers.remove(timer)
            except ValueError:
                pass
            if self._shutting_down:
                return
            try:
                lane.refresh_curve()
            except RuntimeError:
                pass

        timer.timeout.connect(_fire)
        timers.append(timer)
        timer.start()

    def _rebuild_split(self, inherit_x=None):
        for lane in self._lanes.values():
            lane.hide()          # must hide BEFORE setParent(None) or it flashes as a top-level window
            lane.setParent(None)
            lane.deleteLater()
        self._lanes.clear()

        visible = [t for t in self.traces if t.visible]
        if not visible:
            return

        first_lane = None
        for trace in visible:
            lane = TraceLane(trace, self._style_context,
                              self.y_lock_auto,
                              getattr(trace, '_interp_mode_override',
                                      self.interp_mode),
                              lane_label_size=self._lane_label_size,
                              show_lane_labels=self._show_lane_labels,
                              allow_theme_force_labels=self._allow_theme_force_labels,
                              limits_config=self._limits_config)
            lane.viewport_min_pts = self.viewport_min_pts
            lane._process_segments = self._seg_process
            lane._segment_dim_opacity = self._seg_dim_opacity
            lane._segment_dash_pattern = self._seg_dash_pattern
            if self._smart_scale_settings:
                lane._x_axis.set_smart_scale(self._smart_scale_settings)
            if self._real_time_settings:
                lane._x_axis.set_real_time(self._real_time_settings)
            if self._div_settings:
                lane._x_axis.set_div_settings(self._div_settings)
                lane._y_axis.set_div_settings(self._div_settings)
            lane.setMinimumHeight(self._min_lane_height)
            lane.viewport().installEventFilter(self)
            if first_lane is None:
                first_lane = lane
            else:
                lane.setXLink(first_lane)
            lane.cursor_moved.connect(self._on_cursor_moved)
            lane.context_menu_requested.connect(self.trace_context_menu_requested)
            lane.view_range_changed.connect(
                lambda _: self._range_timer.start())
            self._lanes[trace.name] = lane
            self._lanes_layout.addWidget(lane)
            # Apply current Y-axis label width (overrides TraceLane's hardcoded 60)
            lane.getPlotItem().getAxis("left").setWidth(self._y_axis_label_w)
            # Defer refresh to next event loop tick — widget now has proper
            # geometry and view range set via layout, avoiding zombie curves
            # that would result from calling refresh_curve() before addWidget.
            self._defer_lane_refresh(lane)

        # Apply inherited x range to the x-link master; all linked lanes follow.
        # setXRange is synchronous so it takes effect before the deferred
        # refresh_curve() calls run on the next event loop iteration.
        if inherit_x is not None and first_lane is not None:
            x0, x1 = inherit_x
            if x1 > x0:
                first_lane.getPlotItem().setXRange(x0, x1, padding=0)

        for cid, t_pos in self._cursors.items():
            if t_pos is not None:
                self._place_cursors(cid, t_pos)

        for name, (layers, t_ref) in self._persist_state.items():
            lane = self._lanes.get(name)
            if lane:
                lane.set_persistence_layers(layers, t_ref)
        for name, (t_abs, data) in self._retrigger_curve_state.items():
            lane = self._lanes.get(name)
            if lane:
                lane.set_retrigger_curve(t_abs, data)

        self._range_timer.start()

    def set_y_axis_label_width(self, width: int):
        """Set the Y-axis label area width for all lanes and the overlay.
        Stored so that lanes created after this call (on data load) also pick it up."""
        self._y_axis_label_w = width
        for lane in self._lanes.values():
            lane.getPlotItem().getAxis("left").setWidth(width)
        if self._overlay_widget is not None:
            self._overlay_widget.getPlotItem().getAxis("left").setWidth(width)

    def apply_lane_label_settings(self, size: int, show: bool, allow_force: bool,
                                   spacing: float = None):
        """Update lane label settings and propagate to all existing lanes/legend."""
        self._lane_label_size = size
        self._show_lane_labels = show
        self._allow_theme_force_labels = allow_force
        if spacing is not None:
            self._lane_label_spacing = spacing
        for lane in self._lanes.values():
            lane.set_lane_label_settings(size, show, allow_force)
        self._rebuild_overlay_legend()

    # ── Overlay legend ────────────────────────────────────────────────

    def _overlay_label_visible(self) -> bool:
        """True when the overlay legend should be shown."""
        if self._show_lane_labels:
            return True
        if self._allow_theme_force_labels:
            return self._plot_theme.force_labels
        return False

    def _clear_overlay_legend(self):
        """Remove all legend TextItems from the overlay PlotItem."""
        pi = self._overlay_widget.getPlotItem()
        for item in self._overlay_legend_items:
            try:
                pi.removeItem(item)
            except Exception:
                pass
        self._overlay_legend_items.clear()

    def _rebuild_overlay_legend(self):
        """Create fresh legend TextItems for all visible traces (top-right)."""
        self._clear_overlay_legend()
        if not self._overlay_z_order or not self._overlay_label_visible():
            return
        pi = self._overlay_widget.getPlotItem()
        bg_color = QColor(
            self._style_context.plot_colors.get("background", "#0d0d0d"))
        bg_color.setAlpha(210)
        font = QFont()
        font.setPointSize(self._lane_label_size)
        font.setBold(True)
        for name in self._overlay_z_order:
            trace = next((t for t in self.traces if t.name == name), None)
            if trace is None:
                continue
            color = trace.sync_theme_color(self._plot_theme)
            color = _effective_color(color, self._style_context.theme_name)
            item = pg.TextItem(
                text=trace.label,
                color=color,
                fill=pg.mkBrush(bg_color),
                anchor=(1.0, 0.0),  # top-right corner of box at setPos point
            )
            item.setFont(font)
            pi.addItem(item, ignoreBounds=True)
            self._overlay_legend_items.append(item)
        self._reposition_overlay_legend()

    def _normalized_overlay_z_order(self, visible_names: List[str]) -> List[str]:
        """Preserve existing overlay stacking for visible traces and append new ones."""
        ordered = [name for name in self._overlay_z_order if name in visible_names]
        for name in visible_names:
            if name not in ordered:
                ordered.append(name)
        return ordered

    def _apply_overlay_z_order(self) -> None:
        """Apply the current overlay z-order to all visible overlay curves."""
        for i, name in enumerate(self._overlay_z_order):
            visual = self._overlay_visuals.get(name)
            if visual and visual.curve is not None:
                visual.curve.setZValue(float(i))

    def _reposition_overlay_legend(self):
        """Restack legend items in the top-right corner using screen-pixel spacing."""
        if not self._overlay_legend_items:
            return
        try:
            pi = self._overlay_widget.getPlotItem()
            vr = pi.viewRange()
            x_max = vr[0][1]
            y_max = vr[1][1]
            y_min = vr[1][0]
            h_px = max(1.0, float(pi.vb.height()))
            y_span = max(1e-12, y_max - y_min)
            # Measure actual line height from font metrics
            font = QFont()
            font.setPointSize(self._lane_label_size)
            font.setBold(True)
            font_h_px = float(QFontMetrics(font).height())
            # line slot = text height + gap (spacing_factor × text height)
            line_slot_px = font_h_px * (1.0 + self._lane_label_spacing)
            line_slot_data = line_slot_px * y_span / h_px
            for i, item in enumerate(self._overlay_legend_items):
                item.setPos(x_max, y_max - i * line_slot_data)
        except Exception:
            pass

    def _update_overlay_legend_items(self):
        """Refresh text/colors of existing items; rebuilds if count changed."""
        if len(self._overlay_legend_items) != len(self._overlay_z_order):
            self._rebuild_overlay_legend()
            return
        if not self._overlay_label_visible():
            for item in self._overlay_legend_items:
                item.setVisible(False)
            return
        bg_color = QColor(
            self._style_context.plot_colors.get("background", "#0d0d0d"))
        bg_color.setAlpha(210)
        font = QFont()
        font.setPointSize(self._lane_label_size)
        font.setBold(True)
        for item, name in zip(self._overlay_legend_items, self._overlay_z_order):
            trace = next((t for t in self.traces if t.name == name), None)
            if trace is None:
                continue
            color = trace.sync_theme_color(self._plot_theme)
            color = _effective_color(color, self._style_context.theme_name)
            item.setColor(color)
            item.fill = pg.mkBrush(bg_color)
            item.setText(trace.label)
            item.setFont(font)
            item.setVisible(True)
        self._reposition_overlay_legend()

    def _refresh_overlay_visuals(self):
        visible = [t for t in self.traces if t.visible]
        visible_names = [t.name for t in visible]
        if set(visible_names) != set(self._overlay_visuals.keys()):
            self._rebuild_overlay()
            return

        view_range = self.get_current_view_range()
        self._overlay_z_order = self._normalized_overlay_z_order(visible_names)
        unit = next((t.unit for t in visible
                     if t.unit and t.unit != 'raw'), '')
        self._ov_y_axis.set_unit(unit)

        for trace in visible:
            visual = self._overlay_visuals.get(trace.name)
            if visual is None:
                self._rebuild_overlay()
                return
            visual.trace = trace
            visual.interp_mode = getattr(trace, '_interp_mode_override',
                                         self.interp_mode)
            visual.refresh_curve(view_range)

        self._apply_overlay_z_order()
        if self.y_lock_auto:
            self._overlay_widget.getPlotItem().enableAutoRange(axis="y")
        self._update_overlay_legend_items()
        self._range_timer.start()

    def _rebuild_overlay(self, inherit_x=None):
        pi = self._overlay_widget.getPlotItem()
        saved_view = pi.viewRange()
        had_items = bool(self._overlay_visuals)
        for visual in self._overlay_visuals.values():
            visual.remove()
        self._overlay_visuals.clear()

        visible = [t for t in self.traces if t.visible]
        self._overlay_z_order = self._normalized_overlay_z_order(
            [t.name for t in visible])

        # Decide the x range to use for initial curve renders.
        # inherit_x (from a mode switch) takes priority; fall back to the
        # overlay's own saved range when just rebuilding within overlay mode.
        if inherit_x is not None and inherit_x[1] > inherit_x[0]:
            x0, x1 = inherit_x
        elif had_items:
            x0, x1 = saved_view[0][0], saved_view[0][1]
        else:
            vr = pi.viewRange()
            x0, x1 = vr[0]

        # Update Y-axis unit from first visible trace with a real unit
        unit = next((t.unit for t in visible
                     if t.unit and t.unit != 'raw'), '')
        self._ov_y_axis.set_unit(unit)

        for trace in visible:
            mode = getattr(trace, '_interp_mode_override',
                           self.interp_mode)
            visual = OverlayTraceVisual(
                pi, trace, self._style_context, mode, self.viewport_min_pts,
                limits_config=self._limits_config)
            visual._process_segments    = self._seg_process
            visual._segment_dim_opacity = self._seg_dim_opacity
            visual._segment_dash_pattern = self._seg_dash_pattern
            visual.refresh_curve((x0, x1))
            self._overlay_visuals[trace.name] = visual

        self._apply_overlay_z_order()

        if inherit_x is not None and inherit_x[1] > inherit_x[0]:
            pi.setXRange(inherit_x[0], inherit_x[1], padding=0)
            if self.y_lock_auto:
                pi.enableAutoRange(axis="y")
        elif had_items:
            pi.setXRange(saved_view[0][0], saved_view[0][1], padding=0)
            if self.y_lock_auto:
                pi.enableAutoRange(axis="y")
            else:
                pi.setYRange(saved_view[1][0], saved_view[1][1], padding=0)
        elif self.y_lock_auto:
            pi.enableAutoRange(axis="y")

        # Re-place cursors
        for cid, t_pos in self._cursors.items():
            if t_pos is not None:
                self._place_cursors(cid, t_pos)

        for name, (layers, t_ref) in self._persist_state.items():
            visual = self._overlay_visuals.get(name)
            if visual:
                visual.set_persistence_layers(layers, t_ref)
        for name, (t_abs, data) in self._retrigger_curve_state.items():
            visual = self._overlay_visuals.get(name)
            if visual:
                visual.set_retrigger_curve(t_abs, data)

        self._rebuild_overlay_legend()
        self._range_timer.start()
        self._sync_sinc_state()

    # ── Persistence / retrigger public API ────────────────────────────────────

    def set_persistence_layers(self, trace_name: str,
                               layers: list, t_ref: float = 0.0):
        """Store and display persistence ghost curves for one trace."""
        self._persist_state[trace_name] = (layers, t_ref)
        if self._mode == "split":
            lane = self._lanes.get(trace_name)
            if lane:
                lane.set_persistence_layers(layers, t_ref)
        else:
            visual = self._overlay_visuals.get(trace_name)
            if visual:
                visual.set_persistence_layers(layers, t_ref)

    def clear_persistence_layers(self, trace_name: str = None):
        """Remove persistence ghost curves for one trace, or all if None."""
        if trace_name is None:
            self._persist_state.clear()
            for lane in self._lanes.values():
                lane.clear_persistence_layers()
            for visual in self._overlay_visuals.values():
                visual.clear_persistence_layers()
        else:
            self._persist_state.pop(trace_name, None)
            lane = self._lanes.get(trace_name)
            if lane:
                lane.clear_persistence_layers()
            visual = self._overlay_visuals.get(trace_name)
            if visual:
                visual.clear_persistence_layers()

    def set_retrigger_curve(self, trace_name: str,
                            time_abs: np.ndarray, data: np.ndarray,
                            original_display: str = "dimmed",
                            dimmed_opacity: float = 0.5,
                            dash_pattern: Optional[list] = None):
        """Store and display an averaged / interpolated result curve."""
        self._retrigger_curve_state[trace_name] = (time_abs, data)
        kw = dict(original_display=original_display,
                  dimmed_opacity=dimmed_opacity,
                  dash_pattern=dash_pattern)
        if self._mode == "split":
            lane = self._lanes.get(trace_name)
            if lane:
                lane.set_retrigger_curve(time_abs, data, **kw)
        else:
            visual = self._overlay_visuals.get(trace_name)
            if visual:
                visual.set_retrigger_curve(time_abs, data, **kw)

    def clear_retrigger_curve(self, trace_name: str = None):
        """Remove retrigger result curve(s)."""
        if trace_name is None:
            self._retrigger_curve_state.clear()
            for lane in self._lanes.values():
                lane.clear_retrigger_curve()
            for visual in self._overlay_visuals.values():
                visual.clear_retrigger_curve()
        else:
            self._retrigger_curve_state.pop(trace_name, None)
            lane = self._lanes.get(trace_name)
            if lane:
                lane.clear_retrigger_curve()
            visual = self._overlay_visuals.get(trace_name)
            if visual:
                visual.clear_retrigger_curve()

    def bring_trace_to_front(self, trace_name: str):
        """Move a trace curve to the top of the overlay z-order."""
        if self._mode != "overlay":
            return
        if trace_name not in self._overlay_z_order:
            return
        # Move to end of z-order list (highest z = drawn on top)
        self._overlay_z_order.remove(trace_name)
        self._overlay_z_order.append(trace_name)
        self._apply_overlay_z_order()
        # Rebuild legend so the front trace appears at the top of the label stack
        self._rebuild_overlay_legend()

    def _overlay_context_menu(self, pos):
        # Hit-test: if the click falls on a legend label, open the per-trace menu
        scene_pos = self._overlay_widget.mapToScene(pos)
        for i, item in enumerate(self._overlay_legend_items):
            if item.sceneBoundingRect().contains(scene_pos):
                if i < len(self._overlay_z_order):
                    self.trace_context_menu_requested.emit(
                        self._overlay_z_order[i],
                        self._overlay_widget.mapToGlobal(pos))
                return

        # Background right-click: Bring-to-Front submenu + auto scale
        menu = QMenu(self._overlay_widget)
        if self._overlay_z_order:
            front_menu = menu.addMenu("Bring to Front")
            for name in self._overlay_z_order:
                trace = next((t for t in self.traces if t.name == name), None)
                if trace:
                    act = QAction(f"● {trace.label}", self._overlay_widget)
                    act.setData(name)
                    act.triggered.connect(
                        lambda checked, n=name: self.bring_trace_to_front(n))
                    front_menu.addAction(act)

        menu.addSeparator()
        act_y = QAction("Auto Scale Y", self._overlay_widget)
        act_y.triggered.connect(
            lambda: self._overlay_widget.getPlotItem().enableAutoRange(axis="y"))
        menu.addAction(act_y)
        act_xy = QAction("Auto Scale X+Y", self._overlay_widget)
        act_xy.triggered.connect(
            lambda: self._overlay_widget.getPlotItem().enableAutoRange())
        menu.addAction(act_xy)
        menu.exec(self._overlay_widget.mapToGlobal(pos))

    def auto_scale_trace(self, trace_name: str, axis: str = "both"):
        """Auto-scale the viewport for a trace; handles both split and overlay modes."""
        if self._mode == "split":
            lane = self._lanes.get(trace_name)
            if lane:
                if axis == "y":
                    lane.getPlotItem().enableAutoRange(axis="y")
                else:
                    lane.getPlotItem().enableAutoRange()
        else:
            pi = self._overlay_widget.getPlotItem()
            if axis == "y":
                pi.enableAutoRange(axis="y")
            else:
                pi.enableAutoRange()

    # ── Cursors ───────────────────────────────────────────────────────

    def set_cursor(self, cursor_id: int, x_pos: Optional[float]):
        self._cursors[cursor_id] = x_pos
        if x_pos is not None:
            self._place_cursors(cursor_id, x_pos)
        else:
            self._remove_cursor_visuals(cursor_id)
        self._emit_cursor_values()

    def clear_cursors(self):
        """Remove both cursors from the plot and reset their positions."""
        for cid in (0, 1):
            self._cursors[cid] = None
            self._remove_cursor_visuals(cid)
        self._emit_cursor_values()

    def _remove_cursor_visuals(self, cursor_id: int) -> None:
        """Remove one cursor's graphics from both split and overlay views."""
        for lane in self._lanes.values():
            if cursor_id in lane._cursors:
                try:
                    lane.removeItem(lane._cursors[cursor_id])
                except Exception:
                    pass
                lane._cursors.pop(cursor_id, None)
        attr = f"_overlay_cursor_{cursor_id}"
        if hasattr(self, attr):
            try:
                self._overlay_widget.getPlotItem().removeItem(getattr(self, attr))
            except Exception:
                pass
            delattr(self, attr)

    def _place_cursors(self, cursor_id, x_pos):
        color = self._cursor_colors[cursor_id]
        label = "A" if cursor_id == 0 else "B"
        if self._mode == "split":
            for lane in self._lanes.values():
                lane.add_cursor(cursor_id, x_pos, color, label)
        else:
            pi = self._overlay_widget.getPlotItem()
            attr = f"_overlay_cursor_{cursor_id}"
            if hasattr(self, attr):
                pi.removeItem(getattr(self, attr))
            pen = pg.mkPen(color=color, width=1.5,
                            style=Qt.PenStyle.DashLine)
            line = InfiniteLine(pos=x_pos, angle=90, pen=pen, movable=True,
                                 label=label,
                                 labelOpts={"color": color, "position": 0.95})
            line.sigPositionChanged.connect(
                lambda l, cid=cursor_id: self._on_cursor_moved(l.value(), cid))
            pi.addItem(line)
            setattr(self, attr, line)

    def _on_cursor_moved(self, x_pos, cursor_id):
        self._cursors[cursor_id] = x_pos
        if self._mode == "split":
            for lane in self._lanes.values():
                lane.update_cursor(cursor_id, x_pos)
        self._emit_cursor_values()

    def _emit_cursor_values(self):
        result = {}
        for cid, t_pos in self._cursors.items():
            if t_pos is None:
                continue
            vals = {"time": t_pos}
            for trace in self.traces:
                if not trace.visible:
                    continue
                lane = self._lanes.get(trace.name)
                if lane:
                    v = lane.get_value_at(t_pos)
                    if v is not None:
                        vals[trace.name] = v
                else:
                    v = _interpolated_trace_value(
                        trace.time_axis,
                        trace.processed_data,
                        t_pos,
                    )
                    if v is not None:
                        vals[trace.name] = v
            result[cid] = vals
        self.cursor_values_changed.emit(result)

    # ── View range ────────────────────────────────────────────────────

    def get_current_view_range(self) -> Tuple[float, float]:
        if self._mode == "overlay":
            vr = self._overlay_widget.getPlotItem().viewRange()
            return vr[0][0], vr[0][1]
        if self._lanes:
            first = next(iter(self._lanes.values()))
            vr = first.getPlotItem().viewRange()
            return vr[0][0], vr[0][1]
        return 0.0, 1.0

    def get_current_y_range(self) -> Tuple[float, float]:
        if self._mode == "overlay":
            vr = self._overlay_widget.getPlotItem().viewRange()
            return vr[1][0], vr[1][1]
        if self._lanes:
            first = next(iter(self._lanes.values()))
            vr = first.getPlotItem().viewRange()
            return vr[1][0], vr[1][1]
        return -1.0, 1.0

    def _update_range_bar(self):
        if self._shutting_down:
            return
        x0, x1 = self.get_current_view_range()
        y0, y1 = self.get_current_y_range()
        self._range_bar.update_display(x0, x1, y0, y1)

    def _on_range_bar_changed(self, x0, x1, y0, y1):
        self.zoom_x_range(x0, x1)
        self.zoom_y_range(y0, y1)

    def zoom_full(self):
        self.zoom_fit_x()
        self.zoom_fit_y()

    def zoom_fit_x(self):
        if self._mode == "split":
            for lane in self._lanes.values():
                lane.getPlotItem().enableAutoRange(axis="x")
        else:
            self._overlay_widget.getPlotItem().enableAutoRange(axis="x")

    def zoom_fit_y(self):
        if self._mode == "split":
            for lane in self._lanes.values():
                lane.getPlotItem().enableAutoRange(axis="y")
        else:
            self._overlay_widget.getPlotItem().enableAutoRange(axis="y")

    def zoom_x_range(self, x_start, x_end):
        if self._mode == "split":
            for lane in self._lanes.values():
                lane.getPlotItem().setXRange(x_start, x_end, padding=0)
        else:
            self._overlay_widget.getPlotItem().setXRange(
                x_start, x_end, padding=0)

    def zoom_y_range(self, y_start, y_end):
        if self._mode == "split":
            for lane in self._lanes.values():
                lane.getPlotItem().setYRange(y_start, y_end, padding=0)
        else:
            self._overlay_widget.getPlotItem().setYRange(
                y_start, y_end, padding=0)

    def zoom_in(self, factor=0.5):
        x0, x1 = self.get_current_view_range()
        mid = (x0 + x1) / 2
        half = (x1 - x0) * factor / 2
        self.zoom_x_range(mid - half, mid + half)

    def zoom_out(self, factor=2.0):
        self.zoom_in(factor)

    def pan_x(self, fraction):
        x0, x1 = self.get_current_view_range()
        dx = (x1 - x0) * fraction
        self.zoom_x_range(x0 + dx, x1 + dx)

    def take_screenshot(self, filepath, scale=2, branding_path=""):
        """
        Grab plot + status bar together (no range-edit bar in screenshot).
        The scope status bar is expected to be a sibling widget managed
        by the parent; we grab only the plot scroll/overlay area.
        """
        from PyQt6.QtCore import Qt as _Qt
        from PyQt6.QtGui import QPainter, QImage

        # Only grab the plot area (scroll or overlay), not the range bar
        if self._mode == "split":
            grab_widget = self._scroll
        else:
            grab_widget = self._overlay_widget
        pixmap = grab_widget.grab()

        if scale > 1:
            img = pixmap.toImage()
            img = img.scaled(
                img.width() * scale, img.height() * scale,
                _Qt.AspectRatioMode.KeepAspectRatio,
                _Qt.TransformationMode.SmoothTransformation)
        else:
            img = pixmap.toImage()

        # Composite branding SVG into bottom-left corner
        if branding_path:
            img = _composite_branding(img, branding_path)

        img.save(filepath)
        return True
