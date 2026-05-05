"""
pytraceview/trace_lane.py
Per-trace rendering primitives:
  TraceLane        — split-mode: one pg.PlotWidget per visible trace
  OverlayTraceVisual — overlay-mode: curve + persistence/retrigger logic
                       without its own widget (shares the overlay PlotItem)
"""

import math
import numpy as np
import pyqtgraph as pg
from pyqtgraph import InfiniteLine
from PyQt6.QtWidgets import QColorDialog, QInputDialog
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from typing import Dict, List, Optional, Tuple

from pytraceview.trace_model import TraceModel
from pytraceview.plot_theme import PlotTheme
from pytraceview.draw_mode import RenderViewport, create_density_estimator, resolve_pen_width
from pytraceview.render_utils import (
    TraceStyleContext,
    _style_context_from_plot_theme,
    _effective_color,
    _trace_primary_segment_points,
    _trace_value_at_position,
    _windowed_render_points,
    _interpolated_trace_value,
    _resolve_display_limit,
    DEFAULT_LIMITS_CONFIG,
    downsample_for_display,
    sinc_interpolate_to_n,
    cubic_interpolate_to_n,
    _upsample_for_display,
)
from pytraceview.display_items import EngineeringAxisItem, EngineeringTimeAxisItem


# ── Split-mode lane ───────────────────────────────────────────────────────────

class TraceLane(pg.PlotWidget):
    cursor_moved             = pyqtSignal(float, int)
    view_range_changed       = pyqtSignal(object)   # passes self
    context_menu_requested   = pyqtSignal(str, object)  # (trace_name, QPoint global)

    def __init__(self, trace: TraceModel, style_context: TraceStyleContext,
                 y_lock_auto: bool = True,
                 interp_mode: str = "linear",
                 lane_label_size: int = 8,
                 show_lane_labels: bool = True,
                 allow_theme_force_labels: bool = False,
                 limits_config: Optional[dict] = None,
                 parent=None):
        self._y_axis = EngineeringAxisItem(orientation="left")
        self._x_axis = EngineeringTimeAxisItem(orientation="bottom")
        unit = getattr(trace, 'unit', '') or ''
        self._y_axis.set_unit(unit)
        self._y_axis.set_ch_name(getattr(trace, 'name', ''))
        super().__init__(parent=parent,
                         background=style_context.plot_colors["background"],
                         axisItems={"left": self._y_axis,
                                    "bottom": self._x_axis})
        self.trace = trace
        self._style_context = style_context
        self.y_lock_auto = y_lock_auto
        self.interp_mode = interp_mode   # "linear" or "sinc"
        self.viewport_min_pts = 1024      # minimum display points; set from settings
        self._lane_label_size: int = lane_label_size
        self._show_lane_labels: bool = show_lane_labels
        self._allow_theme_force_labels: bool = allow_theme_force_labels
        self._curve = None               # replaced by plot() call after _setup_plot()
        self._persist_curves: list = []   # ghost curves for persistence layers
        self._retrigger_curve = None      # averaged / interpolated override curve
        self._original_display_mode: Optional[str] = None  # "dimmed"|"dashed"|"hide"
        self._original_dimmed_opacity: float = 0.5
        self._original_dash_pattern: Optional[list] = None
        self._cursors: Dict[int, InfiniteLine] = {}
        self._labels: list = []          # TextItem labels anchored to time positions
        self._limits_config: dict = dict(limits_config) if limits_config else dict(DEFAULT_LIMITS_CONFIG)
        self._suppress_view_redraws = False   # set True by TraceView during batch zoom
        self._sinc_active = False         # True when sinc was actually used this draw
        self._segment_curves: list = []   # non-primary segment overlay curves
        self._process_segments: bool = True  # when False, render full trace ignoring segments
        self._segment_dim_opacity: float = 0.30  # 0–1, for "dimmed" non-primary segments
        self._segment_dash_pattern: Optional[list] = None  # Qt dash pattern for "dashed"
        self._render_t = np.array([])
        self._render_y = np.array([])
        self._visible_samples = 0
        self._density_estimator = create_density_estimator(
            self._style_context.draw_mode)
        self._last_style_key = None

        self._setup_plot()
        # Create the primary curve item once and reuse it across all redraws.
        # _add_trace_curve() updates it in-place via setData() — avoids
        # PlotCurveItem allocation/deallocation on every pan, zoom, or
        # interp-mode change.  Mirrors the OverlayVisual pattern.
        self._curve = self.plot([], [], pen=pg.mkPen(width=1.5), antialias=False)
        self._curve.setDownsampling(auto=True, method="peak")
        self._curve.setClipToView(True)
        self._add_trace_curve()

        # Re-render when view range changes (viewport-aware interp)
        self.getPlotItem().sigRangeChanged.connect(self._on_view_changed)
        self.getPlotItem().sigRangeChanged.connect(
            lambda: self.view_range_changed.emit(self))

        # Floating trace name label pinned to top-right of canvas
        self._setup_trace_label_item()

    def _on_view_changed(self):
        """Re-draw curve on every pan/zoom so viewport windowing + downsampling stay correct."""
        if self._suppress_view_redraws:
            return
        self._add_trace_curve()
        self._reposition_trace_label()

    def _setup_plot(self):
        pi = self.getPlotItem()
        pi.showGrid(x=True, y=True, alpha=0.3)
        pi.setMenuEnabled(False)
        pi.getAxis("left").setWidth(60)
        pi.getAxis("top").setStyle(showValues=False)
        pi.getAxis("right").setStyle(showValues=False)
        self.apply_style(self._style_context)
        self.setMouseTracking(True)
        if self.y_lock_auto:
            pi.setMouseEnabled(x=True, y=False)

    def _display_color(self) -> str:
        color = self.trace.sync_theme_color(self._style_context.theme)
        return _effective_color(color, self._style_context.theme_name)

    def apply_style(self, style_context: TraceStyleContext):
        self._style_context = style_context
        self._density_estimator = create_density_estimator(style_context.draw_mode)
        plot_colors = style_context.plot_colors
        pi = self.getPlotItem()
        self.setBackground(plot_colors["background"])
        pi.setLabel("left", "")                    # trace name lives in overlay, not axis
        try:
            pi.getAxis("left").showLabel(False)     # suppress label area and (x0.001) clutter
        except Exception:
            pass
        for ax_name in ("left", "bottom", "top", "right"):
            ax = pi.getAxis(ax_name)
            pen = pg.mkPen(color=plot_colors["text"], width=1)
            ax.setPen(pen)
            ax.setTextPen(pen)
        self._last_style_key = None
        self._apply_resolved_style()
        self._redraw_labels()
        self._update_trace_label_item()
        self.update()
        self.repaint()

    def apply_theme(self, plot_theme: PlotTheme):
        self.apply_style(_style_context_from_plot_theme(
            plot_theme,
            self._style_context.draw_mode,
            self._style_context.density_pen_mapping))

    # ── Floating trace label overlay ──────────────────────────────────

    def _label_visible(self) -> bool:
        """True when the overlay label should be shown."""
        if self._show_lane_labels:
            return True
        if self._allow_theme_force_labels:
            return self._style_context.theme.force_labels
        return False

    def _setup_trace_label_item(self):
        """Create a TextItem pinned to the top-right corner of the plot canvas."""
        disp_color = self._display_color()
        bg_color = QColor(
            self._style_context.plot_colors.get("background", "#0d0d0d"))
        bg_color.setAlpha(210)
        self._trace_label_item = pg.TextItem(
            text=self.trace.label,
            color=disp_color,
            fill=pg.mkBrush(bg_color),
            anchor=(1.0, 0.0),   # top-right corner of text box at setPos point
        )
        font = QFont()
        font.setPointSize(self._lane_label_size)
        font.setBold(True)
        self._trace_label_item.setFont(font)
        self.getPlotItem().addItem(self._trace_label_item, ignoreBounds=True)
        self._trace_label_item.setVisible(self._label_visible())
        self._reposition_trace_label()

    def _update_trace_label_item(self):
        """Update text, colour and visibility of the floating label; no-op until item exists."""
        if not hasattr(self, '_trace_label_item'):
            return
        disp_color = self._display_color()
        bg_color = QColor(
            self._style_context.plot_colors.get("background", "#0d0d0d"))
        bg_color.setAlpha(210)
        self._trace_label_item.fill = pg.mkBrush(bg_color)
        self._trace_label_item.setColor(disp_color)
        self._trace_label_item.setVisible(self._label_visible())
        self._trace_label_item.setText(self.trace.label)  # triggers repaint
        self._reposition_trace_label()

    def _reposition_trace_label(self):
        """Move the label to the top-right of the current viewport."""
        if not hasattr(self, '_trace_label_item'):
            return
        try:
            vr = self.getPlotItem().viewRange()
            self._trace_label_item.setPos(vr[0][1], vr[1][1])
        except Exception:
            pass

    def set_lane_label_settings(self, size: int, show: bool, allow_force: bool):
        """Update label size and visibility; safe to call at any time."""
        self._lane_label_size = size
        self._show_lane_labels = show
        self._allow_theme_force_labels = allow_force
        if not hasattr(self, '_trace_label_item'):
            return
        font = QFont()
        font.setPointSize(size)
        font.setBold(True)
        self._trace_label_item.setFont(font)
        self._trace_label_item.setVisible(self._label_visible())
        self._trace_label_item.setText(self.trace.label)  # trigger repaint

    def _current_viewport(self) -> RenderViewport:
        x_range, y_range = self.getPlotItem().viewRange()
        vb = self.getPlotItem().vb
        width_px = max(1.0, float(vb.width()))
        height_px = max(1.0, float(vb.height()))
        return RenderViewport(
            width_px=width_px,
            height_px=height_px,
            x_range=(float(x_range[0]), float(x_range[1])),
            y_range=(float(y_range[0]), float(y_range[1])),
            visible_samples=int(self._visible_samples),
        )

    def _update_visible_samples(self, viewport: Optional[RenderViewport] = None):
        viewport = viewport or self._current_viewport()
        x0, x1 = viewport.x_range
        t_points, _ = _trace_primary_segment_points(self.trace, self._process_segments)
        visible_mask = (t_points >= x0) & (t_points <= x1)
        self._visible_samples = int(visible_mask.sum()) or len(t_points)

    def _density_source_points(self, viewport: RenderViewport) -> Tuple[np.ndarray, np.ndarray]:
        x0, x1 = viewport.x_range
        if self.interp_mode in ("sinc", "cubic") and self._sinc_active and len(self._render_t):
            t_points = self._render_t
            y_points = self._render_y
        else:
            t_points, y_points = _trace_primary_segment_points(
                self.trace, self._process_segments)
            t_points, y_points = _windowed_render_points(t_points, y_points, x0, x1)

        max_points = self._density_estimator.max_segments + 1
        if len(t_points) > max_points:
            idx = np.linspace(0, len(t_points) - 1, max_points, dtype=int)
            t_points = t_points[idx]
            y_points = y_points[idx]
        return t_points, y_points

    def _screen_points(self, viewport: RenderViewport) -> np.ndarray:
        t_points, y_points = self._density_source_points(viewport)
        if len(t_points) == 0:
            return np.empty((0, 2), dtype=float)
        x0, x1 = viewport.x_range
        y0, y1 = viewport.y_range
        dx = max(1e-12, x1 - x0)
        dy = max(1e-12, y1 - y0)
        x_px = (t_points - x0) / dx * viewport.width_px
        y_px = (y_points - y0) / dy * viewport.height_px
        return np.column_stack((x_px, y_px))

    def _resolved_pen_width(self) -> float:
        viewport = self._current_viewport()
        self._update_visible_samples(viewport)
        viewport = RenderViewport(
            width_px=viewport.width_px,
            height_px=viewport.height_px,
            x_range=viewport.x_range,
            y_range=viewport.y_range,
            visible_samples=int(self._visible_samples),
        )
        style_key = (
            round(viewport.width_px, 2),
            round(viewport.height_px, 2),
            round(viewport.x_range[0], 9),
            round(viewport.x_range[1], 9),
            round(viewport.y_range[0], 9),
            round(viewport.y_range[1], 9),
            viewport.visible_samples,
            len(self._render_t),
            self._style_context.draw_mode,
            tuple(sorted(self._style_context.density_pen_mapping.items())),
        )
        if style_key == self._last_style_key and self._curve is not None:
            return float(self._curve.opts["pen"].widthF())
        density = self._density_estimator.compute(
            self.trace,
            self._screen_points(viewport),
            viewport,
        )
        self._last_style_key = style_key
        return resolve_pen_width(density, self._style_context.density_pen_mapping)

    def _apply_resolved_style(self):
        if self._curve is None:
            return
        color = self._display_color()
        width = self._resolved_pen_width()
        self._curve.setPen(pg.mkPen(color=color, width=width))
        self._curve.update()

    def update_render_style(self):
        self._apply_resolved_style()

    def _add_trace_curve(self):
        # Clean up non-primary segment curves from previous render
        for sc in self._segment_curves:
            try:
                self.removeItem(sc)
            except Exception:
                pass
        self._segment_curves = []

        t_full, y_full = _trace_primary_segment_points(
            self.trace, self._process_segments)
        self._sinc_active = False

        segs = self.trace.segments
        primary = self.trace.primary_segment
        viewmode = (self.trace.non_primary_viewmode or '').strip()
        process_segs = (self._process_segments
                        and len(segs) > 1
                        and primary is not None and 0 <= primary < len(segs))
        # Window to the currently visible x-range FIRST — for all interp modes.
        # Downsampling must operate on visible samples only; applying it to the
        # full dataset then clipping wastes resolution when zoomed in.
        vr = self.getPlotItem().viewRange()
        x0, x1 = vr[0]
        t_full, y_full = _windowed_render_points(t_full, y_full, x0, x1)
        n_vis = len(t_full)
        # n_vis < 2: widget created before the view range is set — keep full
        # data as fallback; the first real sigRangeChanged redraws correctly.

        if self.interp_mode in ("sinc", "cubic"):
            n_vis = len(t_full)
            if n_vis < self.viewport_min_pts and n_vis >= 4:
                if self.interp_mode == "cubic":
                    t_full, y_full = cubic_interpolate_to_n(
                        t_full, y_full, self.viewport_min_pts)
                else:
                    t_full, y_full = sinc_interpolate_to_n(
                        t_full, y_full, self.viewport_min_pts)
                self._sinc_active = True

        self._update_visible_samples()

        width_px = float(self.getPlotItem().vb.width())
        max_pts = _resolve_display_limit(self._limits_config, width_px)
        t, y = downsample_for_display(t_full, y_full, max_pts)
        self._render_t = t
        self._render_y = y
        self._last_style_key = None
        self._curve.setData(t, y)   # update in-place — no widget churn
        self._apply_resolved_style()
        self._reapply_original_style()   # restore dimmed/dashed/hidden if active
        if self._persist_curves:
            # Always keep main curve above all persistence ghost layers
            self._curve.setZValue(len(self._persist_curves) + 1)

        # Non-primary segment overlays — iterate Segment objects directly
        width_px = float(self.getPlotItem().vb.width())
        seg_max_pts = _resolve_display_limit(self._limits_config, width_px)
        if process_segs and viewmode != "hide":
            color = self._display_color()
            for i, seg in enumerate(segs):
                if i == primary:
                    continue
                t_seg = seg.time + self.trace.time_offset
                y_seg = self.trace.segment_processed(seg)
                t_seg, y_seg = _windowed_render_points(t_seg, y_seg, x0, x1)
                if len(t_seg) < 2:
                    continue
                t_ds, y_ds = downsample_for_display(t_seg, y_seg, seg_max_pts)
                if viewmode == "dimmed":
                    c = QColor(color)
                    c.setAlphaF(self._segment_dim_opacity)
                    seg_pen = pg.mkPen(color=c, width=1)
                elif viewmode == "dashed":
                    seg_pen = pg.mkPen(color=color, width=1)
                    if self._segment_dash_pattern:
                        seg_pen.setStyle(Qt.PenStyle.CustomDashLine)
                        seg_pen.setDashPattern(self._segment_dash_pattern)
                    else:
                        seg_pen.setStyle(Qt.PenStyle.DashLine)
                else:
                    seg_pen = pg.mkPen(color=color, width=1)
                sc = self.plot(t_ds, y_ds, pen=seg_pen, antialias=False)
                sc.setDownsampling(auto=True, method="peak")
                sc.setClipToView(True)
                self._segment_curves.append(sc)
        elif not process_segs and len(segs) > 1:
            # Segment display disabled — render remaining segments with normal style
            rendered_idx = primary if primary is not None else 0
            color = self._display_color()
            normal_pen = pg.mkPen(color=color, width=1.5)
            for i, seg in enumerate(segs):
                if i == rendered_idx:
                    continue
                t_seg = seg.time + self.trace.time_offset
                y_seg = self.trace.segment_processed(seg)
                t_seg, y_seg = _windowed_render_points(t_seg, y_seg, x0, x1)
                if len(t_seg) < 2:
                    continue
                t_ds, y_ds = downsample_for_display(t_seg, y_seg, seg_max_pts)
                sc = self.plot(t_ds, y_ds, pen=normal_pen, antialias=False)
                sc.setDownsampling(auto=True, method="peak")
                sc.setClipToView(True)
                self._segment_curves.append(sc)

        if self.y_lock_auto:
            self.getPlotItem().enableAutoRange(axis="y")
        self._redraw_labels()

    def _redraw_labels(self):
        """Draw per-trace text labels anchored to time positions."""
        for item in self._labels:
            self.removeItem(item)
        self._labels.clear()
        labels = getattr(self.trace, 'trace_labels', [])
        for t_pos, text in labels:
            y_pos = self.get_value_at(t_pos)
            if y_pos is None:
                continue
            color = self._display_color()
            item = pg.TextItem(text=text, color=color, anchor=(0.5, 1.0))
            item.setPos(t_pos, y_pos)
            self.addItem(item)
            self._labels.append(item)

    def refresh_curve(self):
        self._add_trace_curve()
        # Refresh unit on axis in case it changed (e.g. after filter applied)
        unit = getattr(self.trace, 'unit', '') or ''
        if hasattr(self, '_y_axis'):
            self._y_axis.set_unit(unit)

    def set_y_lock_auto(self, locked: bool):
        self.y_lock_auto = locked
        pi = self.getPlotItem()
        pi.setMouseEnabled(x=True, y=not locked)
        if locked:
            pi.enableAutoRange(axis="y")

    def add_cursor(self, cursor_id, x_pos, color, label=""):
        if cursor_id in self._cursors:
            self.removeItem(self._cursors[cursor_id])
        pen = pg.mkPen(color=color, width=1.5, style=Qt.PenStyle.DashLine)
        line = InfiniteLine(pos=x_pos, angle=90, pen=pen, movable=True,
                             label=label,
                             labelOpts={"color": color, "position": 0.95})
        line.sigPositionChanged.connect(
            lambda l, cid=cursor_id: self.cursor_moved.emit(l.value(), cid))
        self.addItem(line)
        self._cursors[cursor_id] = line

    def update_cursor(self, cursor_id, x_pos):
        if cursor_id in self._cursors:
            self._cursors[cursor_id].blockSignals(True)
            self._cursors[cursor_id].setValue(x_pos)
            self._cursors[cursor_id].blockSignals(False)

    def get_value_at(self, t_pos):
        return _trace_value_at_position(self.trace, t_pos)

    # ── Persistence / retrigger overlay ───────────────────────────────────────

    def set_persistence_layers(self, layers: list, t_ref: float = 0.0):
        """Overlay ghost traces for persistence mode."""
        self.clear_persistence_layers()
        color_hex = self._display_color()
        for layer in layers:
            t_plot = layer.time + t_ref
            d_plot = layer.data
            # Apply sinc/cubic interpolation to ghost layers when active
            if (self.interp_mode in ("sinc", "cubic")
                    and len(t_plot) >= 4
                    and len(t_plot) < self.viewport_min_pts):
                if self.interp_mode == "cubic":
                    t_plot, d_plot = cubic_interpolate_to_n(
                        t_plot, d_plot, self.viewport_min_pts)
                else:
                    t_plot, d_plot = sinc_interpolate_to_n(
                        t_plot, d_plot, self.viewport_min_pts)
            c = QColor(color_hex)
            c.setAlphaF(max(0.0, min(1.0, layer.opacity)))
            pen = pg.mkPen(color=c, width=max(0.5, 1.5 * layer.width_multiplier))
            curve = self.plot(t_plot, d_plot, pen=pen, antialias=False)
            curve.setZValue(layer.z_order)
            self._persist_curves.append(curve)
        if self._curve is not None:
            # Keep main curve above all ghost layers regardless of count
            self._curve.setZValue(len(self._persist_curves) + 1)

    def clear_persistence_layers(self):
        for c in self._persist_curves:
            try:
                self.removeItem(c)
            except Exception:
                pass
        self._persist_curves.clear()
        if self._curve is not None:
            self._curve.setZValue(0)

    def _reapply_original_style(self):
        """Apply dimmed/dashed/hidden styling to the raw trace curve when a
        result curve is active.  No-op when no result curve is set."""
        if self._curve is None or self._original_display_mode is None:
            return
        mode = self._original_display_mode
        color = self._display_color()
        width = float(self._curve.opts["pen"].widthF()) or 1.5
        if mode == "hide":
            self._curve.setVisible(False)
        elif mode == "dimmed":
            c = QColor(color)
            c.setAlphaF(self._original_dimmed_opacity)
            self._curve.setPen(pg.mkPen(color=c, width=width))
            self._curve.setVisible(True)
        elif mode == "dashed":
            pen = pg.mkPen(color=color, width=width)
            if self._original_dash_pattern:
                pen.setStyle(Qt.PenStyle.CustomDashLine)
                pen.setDashPattern(self._original_dash_pattern)
            else:
                pen.setStyle(Qt.PenStyle.DashLine)
            self._curve.setPen(pen)
            self._curve.setVisible(True)

    def set_retrigger_curve(self, time_abs: np.ndarray, data: np.ndarray,
                             original_display: str = "dimmed",
                             dimmed_opacity: float = 0.5,
                             dash_pattern: Optional[list] = None):
        """Show averaged/interpolated result as the solid hard line;
        style the raw trace according to original_display."""
        self.clear_retrigger_curve()
        self._original_display_mode = original_display
        self._original_dimmed_opacity = max(0.1, min(0.9, dimmed_opacity))
        self._original_dash_pattern = dash_pattern
        # Apply sinc/cubic upsampling if the mode is active
        t_plot, d_plot = _upsample_for_display(
            time_abs, data, self.interp_mode, self.viewport_min_pts)
        # Result curve — solid, full opacity, slightly wider
        color = self._display_color()
        pen = pg.mkPen(color=color, width=2.0)
        self._retrigger_curve = self.plot(t_plot, d_plot, pen=pen, antialias=False)
        self._retrigger_curve.setZValue(15)
        self._reapply_original_style()

    def clear_retrigger_curve(self):
        if self._retrigger_curve is not None:
            try:
                self.removeItem(self._retrigger_curve)
            except Exception:
                pass
            self._retrigger_curve = None
        # Restore raw trace to normal solid appearance
        self._original_display_mode = None
        if self._curve is not None:
            self._curve.setVisible(True)
            self._apply_resolved_style()

    def contextMenuEvent(self, event):
        self.context_menu_requested.emit(self.trace.name, event.globalPos())

    def _change_color(self):
        c = QColorDialog.getColor(QColor(self.trace.color), self)
        if c.isValid():
            self.trace.set_user_color(c.name())
            self.refresh_curve()
            self._update_trace_label_item()

    def _rename(self):
        text, ok = QInputDialog.getText(
            self, "Rename", "New label:", text=self.trace.label)
        if ok and text:
            self.trace.label = text
            self._update_trace_label_item()


# ── Overlay-mode visual (no widget of its own) ────────────────────────────────

class OverlayTraceVisual:
    def __init__(self, plot_item, trace: TraceModel,
                 style_context: TraceStyleContext,
                 interp_mode: str = "linear",
                 viewport_min_pts: int = 1024,
                 limits_config: Optional[dict] = None):
        self.plot_item = plot_item
        self.trace = trace
        self._style_context = style_context
        self.interp_mode = interp_mode
        self.viewport_min_pts = viewport_min_pts
        self._limits_config: dict = dict(limits_config) if limits_config else dict(DEFAULT_LIMITS_CONFIG)
        self._density_estimator = create_density_estimator(style_context.draw_mode)
        self._render_t = np.array([])
        self._render_y = np.array([])
        self._visible_samples = 0
        self._interpolated_view = False
        self._last_style_key = None
        self._persist_curves: list = []
        self._retrigger_curve = None
        self._original_display_mode: Optional[str] = None
        self._original_dimmed_opacity: float = 0.5
        self._original_dash_pattern: Optional[list] = None
        self._segment_curves: list = []
        self._process_segments: bool = True
        self._segment_dim_opacity: float = 0.30
        self._segment_dash_pattern: Optional[list] = None
        self.curve = self.plot_item.plot([], [], pen=pg.mkPen(width=1.5),
                                         name=trace.label, antialias=False)
        self.curve.setDownsampling(auto=True, method="peak")
        self.curve.setClipToView(True)
        self.apply_style(style_context)

    def _display_color(self) -> str:
        color = self.trace.sync_theme_color(self._style_context.theme)
        return _effective_color(color, self._style_context.theme_name)

    def apply_style(self, style_context: TraceStyleContext):
        self._style_context = style_context
        self._density_estimator = create_density_estimator(style_context.draw_mode)
        self._last_style_key = None
        self._apply_resolved_style()

    def apply_theme(self, plot_theme: PlotTheme):
        self.apply_style(_style_context_from_plot_theme(
            plot_theme,
            self._style_context.draw_mode,
            self._style_context.density_pen_mapping))

    def _current_viewport(self) -> RenderViewport:
        x_range, y_range = self.plot_item.viewRange()
        vb = self.plot_item.vb
        width_px = max(1.0, float(vb.width()))
        height_px = max(1.0, float(vb.height()))
        return RenderViewport(
            width_px=width_px,
            height_px=height_px,
            x_range=(float(x_range[0]), float(x_range[1])),
            y_range=(float(y_range[0]), float(y_range[1])),
            visible_samples=int(self._visible_samples),
        )

    def _update_visible_samples(self, viewport: Optional[RenderViewport] = None):
        viewport = viewport or self._current_viewport()
        x0, x1 = viewport.x_range
        t_points, _ = _trace_primary_segment_points(self.trace, self._process_segments)
        visible_mask = (t_points >= x0) & (t_points <= x1)
        self._visible_samples = int(visible_mask.sum()) or len(t_points)

    def _density_source_points(self, viewport: RenderViewport) -> Tuple[np.ndarray, np.ndarray]:
        x0, x1 = viewport.x_range
        if self.interp_mode in ("sinc", "cubic") and self._interpolated_view and len(self._render_t):
            t_points = self._render_t
            y_points = self._render_y
        else:
            t_points, y_points = _trace_primary_segment_points(
                self.trace, self._process_segments)
            t_points, y_points = _windowed_render_points(t_points, y_points, x0, x1)

        max_points = self._density_estimator.max_segments + 1
        if len(t_points) > max_points:
            idx = np.linspace(0, len(t_points) - 1, max_points, dtype=int)
            t_points = t_points[idx]
            y_points = y_points[idx]
        return t_points, y_points

    def _screen_points(self, viewport: RenderViewport) -> np.ndarray:
        t_points, y_points = self._density_source_points(viewport)
        if len(t_points) == 0:
            return np.empty((0, 2), dtype=float)
        x0, x1 = viewport.x_range
        y0, y1 = viewport.y_range
        dx = max(1e-12, x1 - x0)
        dy = max(1e-12, y1 - y0)
        x_px = (t_points - x0) / dx * viewport.width_px
        y_px = (y_points - y0) / dy * viewport.height_px
        return np.column_stack((x_px, y_px))

    def _resolved_pen_width(self) -> float:
        viewport = self._current_viewport()
        self._update_visible_samples(viewport)
        viewport = RenderViewport(
            width_px=viewport.width_px,
            height_px=viewport.height_px,
            x_range=viewport.x_range,
            y_range=viewport.y_range,
            visible_samples=int(self._visible_samples),
        )
        style_key = (
            round(viewport.width_px, 2),
            round(viewport.height_px, 2),
            round(viewport.x_range[0], 9),
            round(viewport.x_range[1], 9),
            round(viewport.y_range[0], 9),
            round(viewport.y_range[1], 9),
            viewport.visible_samples,
            len(self._render_t),
            self._style_context.draw_mode,
            tuple(sorted(self._style_context.density_pen_mapping.items())),
        )
        if style_key == self._last_style_key:
            return float(self.curve.opts["pen"].widthF())
        density = self._density_estimator.compute(
            self.trace,
            self._screen_points(viewport),
            viewport,
        )
        self._last_style_key = style_key
        return resolve_pen_width(density, self._style_context.density_pen_mapping)

    def _apply_resolved_style(self):
        width = self._resolved_pen_width()
        self.curve.setPen(pg.mkPen(color=self._display_color(), width=width))
        self.curve.update()

    def update_render_style(self):
        self._apply_resolved_style()
        self._reapply_original_style()

    def refresh_curve(self, view_range: Tuple[float, float]):
        # Clean up non-primary segment curves from previous render
        for sc in self._segment_curves:
            try:
                self.plot_item.removeItem(sc)
            except Exception:
                pass
        self._segment_curves = []

        t_full, y_full = _trace_primary_segment_points(
            self.trace, self._process_segments)
        x0, x1 = view_range

        segs = self.trace.segments
        primary = self.trace.primary_segment
        viewmode = (self.trace.non_primary_viewmode or '').strip()
        process_segs = (self._process_segments
                        and len(segs) > 1
                        and primary is not None and 0 <= primary < len(segs))
        self._interpolated_view = False
        self._update_visible_samples(RenderViewport(
            width_px=max(1.0, float(self.plot_item.vb.width())),
            height_px=max(1.0, float(self.plot_item.vb.height())),
            x_range=(x0, x1),
            y_range=tuple(self.plot_item.viewRange()[1]),
            visible_samples=self._visible_samples,
        ))

        # Window to visible range first — for all modes.
        t_full, y_full = _windowed_render_points(t_full, y_full, x0, x1)
        n_vis = len(t_full)
        # n_vis < 2: widget not yet laid out — keep full data as fallback

        if self.interp_mode in ("sinc", "cubic"):
            n_vis = len(t_full)
            if n_vis < self.viewport_min_pts and n_vis >= 4:
                if self.interp_mode == "cubic":
                    t_full, y_full = cubic_interpolate_to_n(
                        t_full, y_full, self.viewport_min_pts)
                else:
                    t_full, y_full = sinc_interpolate_to_n(
                        t_full, y_full, self.viewport_min_pts)
                self._interpolated_view = True

        width_px = max(1.0, float(self.plot_item.vb.width()))
        max_pts = _resolve_display_limit(self._limits_config, width_px)
        t_data, y_data = downsample_for_display(t_full, y_full, max_pts)
        self._render_t = t_data
        self._render_y = y_data
        self._last_style_key = None
        self.curve.setData(t_data, y_data)
        self.curve.opts["name"] = self.trace.label
        self._apply_resolved_style()
        self._reapply_original_style()
        if self._persist_curves:
            self.curve.setZValue(len(self._persist_curves) + 1)

        # Non-primary segment overlays — iterate Segment objects directly
        seg_max_pts = _resolve_display_limit(self._limits_config, width_px)
        if process_segs and viewmode != "hide":
            color = self._display_color()
            for i, seg in enumerate(segs):
                if i == primary:
                    continue
                t_seg = seg.time + self.trace.time_offset
                y_seg = self.trace.segment_processed(seg)
                t_seg, y_seg = _windowed_render_points(t_seg, y_seg, x0, x1)
                if len(t_seg) < 2:
                    continue
                t_ds, y_ds = downsample_for_display(t_seg, y_seg, seg_max_pts)
                if viewmode == "dimmed":
                    c = QColor(color)
                    c.setAlphaF(self._segment_dim_opacity)
                    seg_pen = pg.mkPen(color=c, width=1)
                elif viewmode == "dashed":
                    seg_pen = pg.mkPen(color=color, width=1)
                    if self._segment_dash_pattern:
                        seg_pen.setStyle(Qt.PenStyle.CustomDashLine)
                        seg_pen.setDashPattern(self._segment_dash_pattern)
                    else:
                        seg_pen.setStyle(Qt.PenStyle.DashLine)
                else:
                    seg_pen = pg.mkPen(color=color, width=1)
                sc = self.plot_item.plot(t_ds, y_ds, pen=seg_pen, antialias=False)
                sc.setDownsampling(auto=True, method="peak")
                sc.setClipToView(True)
                self._segment_curves.append(sc)
        elif not process_segs and len(segs) > 1:
            rendered_idx = primary if primary is not None else 0
            color = self._display_color()
            normal_pen = pg.mkPen(color=color, width=1.5)
            for i, seg in enumerate(segs):
                if i == rendered_idx:
                    continue
                t_seg = seg.time + self.trace.time_offset
                y_seg = self.trace.segment_processed(seg)
                t_seg, y_seg = _windowed_render_points(t_seg, y_seg, x0, x1)
                if len(t_seg) < 2:
                    continue
                t_ds, y_ds = downsample_for_display(t_seg, y_seg, seg_max_pts)
                sc = self.plot_item.plot(t_ds, y_ds, pen=normal_pen, antialias=False)
                sc.setDownsampling(auto=True, method="peak")
                sc.setClipToView(True)
                self._segment_curves.append(sc)

    # ── Persistence / retrigger overlay ───────────────────────────────────────

    def set_persistence_layers(self, layers: list, t_ref: float = 0.0):
        self.clear_persistence_layers()
        color_hex = self._display_color()
        for layer in layers:
            t_plot = layer.time + t_ref
            d_plot = layer.data
            # Apply sinc/cubic interpolation to ghost layers when active
            if (self.interp_mode in ("sinc", "cubic")
                    and len(t_plot) >= 4
                    and len(t_plot) < self.viewport_min_pts):
                if self.interp_mode == "cubic":
                    t_plot, d_plot = cubic_interpolate_to_n(
                        t_plot, d_plot, self.viewport_min_pts)
                else:
                    t_plot, d_plot = sinc_interpolate_to_n(
                        t_plot, d_plot, self.viewport_min_pts)
            c = QColor(color_hex)
            c.setAlphaF(max(0.0, min(1.0, layer.opacity)))
            pen = pg.mkPen(color=c, width=max(0.5, 1.5 * layer.width_multiplier))
            curve = self.plot_item.plot(t_plot, d_plot, pen=pen, antialias=False)
            curve.setZValue(layer.z_order)
            self._persist_curves.append(curve)
        self.curve.setZValue(len(self._persist_curves) + 1)

    def clear_persistence_layers(self):
        for c in self._persist_curves:
            try:
                self.plot_item.removeItem(c)
            except Exception:
                pass
        self._persist_curves.clear()
        self.curve.setZValue(0)

    def _reapply_original_style(self):
        if self._original_display_mode is None:
            return
        mode = self._original_display_mode
        color = self._display_color()
        width = float(self.curve.opts["pen"].widthF()) or 1.5
        if mode == "hide":
            self.curve.setVisible(False)
        elif mode == "dimmed":
            c = QColor(color)
            c.setAlphaF(self._original_dimmed_opacity)
            self.curve.setPen(pg.mkPen(color=c, width=width))
            self.curve.setVisible(True)
        elif mode == "dashed":
            pen = pg.mkPen(color=color, width=width)
            if self._original_dash_pattern:
                pen.setStyle(Qt.PenStyle.CustomDashLine)
                pen.setDashPattern(self._original_dash_pattern)
            else:
                pen.setStyle(Qt.PenStyle.DashLine)
            self.curve.setPen(pen)
            self.curve.setVisible(True)

    def set_retrigger_curve(self, time_abs: np.ndarray, data: np.ndarray,
                             original_display: str = "dimmed",
                             dimmed_opacity: float = 0.5,
                             dash_pattern: Optional[list] = None):
        self.clear_retrigger_curve()
        self._original_display_mode = original_display
        self._original_dimmed_opacity = max(0.1, min(0.9, dimmed_opacity))
        self._original_dash_pattern = dash_pattern
        t_plot, d_plot = _upsample_for_display(
            time_abs, data, self.interp_mode, self.viewport_min_pts)
        color = self._display_color()
        pen = pg.mkPen(color=color, width=2.0)
        self._retrigger_curve = self.plot_item.plot(
            t_plot, d_plot, pen=pen, antialias=False)
        self._retrigger_curve.setZValue(15)
        self._reapply_original_style()

    def clear_retrigger_curve(self):
        if self._retrigger_curve is not None:
            try:
                self.plot_item.removeItem(self._retrigger_curve)
            except Exception:
                pass
            self._retrigger_curve = None
        self._original_display_mode = None
        self.curve.setVisible(True)
        self._apply_resolved_style()

    def remove(self):
        self.clear_persistence_layers()
        self.clear_retrigger_curve()
        for sc in self._segment_curves:
            try:
                self.plot_item.removeItem(sc)
            except Exception:
                pass
        self._segment_curves = []
        self.plot_item.removeItem(self.curve)
