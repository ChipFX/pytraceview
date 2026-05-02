"""
pytraceview/display_items.py
Peripheral display components for the plotting engine:
  _eng_format             — SI-prefix number formatter
  _filter_dense_labels    — Y-axis label overlap filter
  EngineeringTimeAxisItem — X-axis with SI time prefixes and smart HH:MM:SS mode
  EngineeringAxisItem     — Y-axis with SI unit prefixes (mV, µA, kHz …)
  RangeBar                — compact X/Y range input bar shown below the plot
"""

import math
import pyqtgraph as pg
from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QLabel, QLineEdit,
                              QPushButton)
from PyQt6.QtCore import Qt, pyqtSignal, QRectF, QPointF
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPen


# ── SI-prefix formatter ───────────────────────────────────────────────────────

def _eng_format(value: float, unit: str, spacing: float = None) -> str:
    """
    Format a float with engineering-style SI prefix and unit.

    When ``spacing`` is provided (tick interval in the same units as value),
    the number of decimal places is computed so that adjacent ticks cannot
    produce identical labels.  Without spacing a short-but-readable heuristic
    is used (not safe for closely-spaced ticks).

    Examples:  0.001 V  ->  '1 mV'
               0.000099 V -> '99 µV'
               1500 Hz    -> '1.5 kHz'
               0.1 V      -> '100 mV'
    """
    if value == 0:
        return f"0 {unit}"
    abs_v = abs(value)
    prefixes = [
        (1e12, 'T'), (1e9, 'G'), (1e6, 'M'), (1e3, 'k'),
        (1,    ''),  (1e-3, 'm'), (1e-6, 'µ'), (1e-9, 'n'), (1e-12, 'p'),
    ]
    for scale, prefix in prefixes:
        if abs_v >= scale * 0.9999:
            scaled = value / scale
            if spacing is not None and spacing > 0:
                # Enough decimal places so that spacing/scale differences are
                # never rounded away.  E.g. spacing=0.05, scale=1 → dp=2.
                scaled_sp = abs(spacing / scale)
                dp = max(0, -int(math.floor(math.log10(scaled_sp)))) if scaled_sp < 1 else 0
                dp = min(dp, 9)   # guard against pathological inputs
                s = f"{scaled:.{dp}f}"
            else:
                # Heuristic for status-bar / non-tick uses
                if abs(scaled) >= 100:
                    s = f"{scaled:.0f}"
                elif abs(scaled) >= 10:
                    s = f"{scaled:.1f}".rstrip('0').rstrip('.')
                else:
                    s = f"{scaled:.2f}".rstrip('0').rstrip('.')
            return f"{s} {prefix}{unit}"
    # Fallback for very small values
    return f"{value:.3e} {unit}"


# ── Y-axis label density filter ───────────────────────────────────────────────

def _filter_dense_labels(textSpecs: list) -> list:
    """Drop Y-axis tick labels that overlap.

    pyqtgraph returns textSpecs as [(QRectF, flags, text), ...].
    The rects are computed from actual font metrics, so this is font-size-
    independent.  Gridlines (in tickSpecs) are never affected.

    A new label is accepted only if its top edge is at or below the previous
    label's bottom edge (strict no-overlap).
    """
    if len(textSpecs) <= 1:
        return textSpecs
    # Sort top-to-bottom by rect vertical centre
    by_y = sorted(textSpecs, key=lambda s: s[0].center().y())
    kept = [by_y[0]]
    for spec in by_y[1:]:
        if spec[0].top() >= kept[-1][0].bottom():
            kept.append(spec)
    return kept


# ── X-axis: time with SI or HH:MM:SS ─────────────────────────────────────────

class EngineeringTimeAxisItem(pg.AxisItem):
    """X-axis with SI time prefixes (ns/µs/ms/s/ks) or smart MM:SS / HH:MM:SS display."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._smart       = False
        self._smart_max_s = 300.0   # seconds above which → MM:SS
        self._smart_max_m = 120.0   # minutes above which → HH:MM:SS
        self._smart_max_h = 24.0    # hours   above which → DD:HH:MM:SS
        self._div_cfg: dict = {}
        self._real_time        = False
        self._t0_wall_clock_dt = None  # datetime | None
        self._rt_accent_color: str = "#1e88e5"
        # Per-render-cycle anchor state (reset in generateDrawSpecs)
        self._rt_anchor_label: str   = ""
        self._rt_anchor_t:     float = 0.0
        self._rt_max_spacing:  float = 0.0

    def set_div_settings(self, cfg: dict):
        self._div_cfg = cfg or {}
        self.picture = None
        self.update()

    def tickSpacing(self, minVal, maxVal, size):
        ticks = super().tickSpacing(minVal, maxVal, size)
        if not ticks or maxVal <= minVal or size <= 0:
            return ticks
        major = ticks[0][0]
        if major <= 0:
            return ticks
        px_per_major = size * major / (maxVal - minVal)
        result = [(major, ticks[0][1])]
        if px_per_major >= self._div_cfg.get("div_tenths_px", 60):
            result.append((major / 10.0, 0))
        elif px_per_major >= self._div_cfg.get("div_fifths_px", 30):
            result.append((major / 5.0, 0))
        elif px_per_major >= self._div_cfg.get("div_halves_px", 15):
            result.append((major / 2.0, 0))
        self._last_tick_result = result   # cache for status-bar readback
        return result

    def tickValues(self, minVal, maxVal, size):
        levels = super().tickValues(minVal, maxVal, size)
        self._tick_level_counts = [len(lvl[1]) for lvl in levels]
        return levels

    def generateDrawSpecs(self, p):
        # Reset per-render anchor so tickStrings picks up the major-level call
        self._rt_anchor_label = ""
        self._rt_max_spacing  = 0.0
        result = super().generateDrawSpecs(p)
        if result is None:
            return result   # axis not yet sized
        axisSpec, tickSpecs, textSpecs = result
        self._fix_subdiv_alpha(tickSpecs)
        if self._real_time and self._t0_wall_clock_dt is not None and self._rt_anchor_label:
            self._inject_rt_anchor(tickSpecs, textSpecs, p)
        return axisSpec, tickSpecs, textSpecs

    def _fix_subdiv_alpha(self, tickSpecs):
        """Re-apply a density-independent alpha to level-1 (sub-div) tick specs.
        pyqtgraph's built-in formula multiplies by 0.05*length/N which renders
        fine sub-divisions nearly invisible.  We override to a fixed fraction."""
        if self.grid is False or not tickSpecs:
            return
        counts = getattr(self, '_tick_level_counts', [])
        if len(counts) < 2:
            return   # only major level, nothing to boost
        n_major = counts[0]
        if len(tickSpecs) <= n_major:
            return   # all specs are major
        sub_alpha = max(20, int(self.grid * 0.55))
        for idx in range(n_major, len(tickSpecs)):
            pen, p1, p2 = tickSpecs[idx]
            pen = QPen(pen)
            c = pen.color()
            c.setAlpha(sub_alpha)
            pen.setColor(c)
            tickSpecs[idx] = (pen, p1, p2)

    def set_smart_scale(self, settings: dict):
        ss = settings or {}
        self._smart       = bool(ss.get("enabled", False))
        self._smart_max_s = float(ss.get("max_seconds", 300))
        self._smart_max_m = float(ss.get("max_minutes", 120))
        self._smart_max_h = float(ss.get("max_hours",   24))
        self.picture = None   # invalidate cached axis rendering
        self.update()

    def set_real_time(self, settings: dict):
        """Enable/disable real-time mode.  settings keys:
            enabled (bool), t0_wall_clock (ISO-8601 str or ""), accent_color (hex str)
        """
        from datetime import datetime
        rt = settings or {}
        self._real_time = bool(rt.get("enabled", False))
        t0_str = rt.get("t0_wall_clock", "") or ""
        if self._real_time and t0_str:
            try:
                self._t0_wall_clock_dt = datetime.fromisoformat(t0_str)
            except ValueError:
                self._t0_wall_clock_dt = None
        else:
            self._t0_wall_clock_dt = None
        if rt.get("accent_color"):
            self._rt_accent_color = rt["accent_color"]
        self.picture = None
        self.update()

    def set_accent_color(self, color: str):
        self._rt_accent_color = color or "#1e88e5"
        self.picture = None
        self.update()

    def tickStrings(self, values, scale, spacing):
        if not values:
            return []
        if self._real_time and self._t0_wall_clock_dt is not None:
            return self._fmt_real_time_strings(values, spacing)
        if not self._smart:
            return self._eng_strings(values)

        max_abs = max(abs(float(v)) for v in values)
        if max_abs < self._smart_max_s:
            return self._eng_strings(values)   # still in seconds range — keep SI

        show_ms   = spacing < 1.0
        max_m_thr = self._smart_max_m * 60.0
        max_h_thr = self._smart_max_h * 3600.0

        # Shared-prefix optimisation: when all visible ticks share the same
        # whole-minute (or whole-hour) value, show only the seconds portion
        # after the first tick to reduce label clutter.
        use_prefix = False
        if len(values) > 1 and max_abs >= max_m_thr and spacing < 60:
            prefix_mins = [int(abs(float(v))) // 60 for v in values]
            use_prefix = (len(set(prefix_mins)) == 1)

        return [self._fmt_smart(float(v), max_abs, max_m_thr, max_h_thr,
                                show_ms, use_prefix and i > 0)
                for i, v in enumerate(values)]

    @staticmethod
    def _fmt_smart(t: float, max_abs: float,
                   max_m_thr: float, max_h_thr: float,
                   show_ms: bool, prefix_only: bool = False) -> str:
        sign = "−" if t < 0 else ""   # proper minus sign
        a    = abs(t)
        ms_str = f".{int(round((a % 1.0) * 1000)):03d}" if show_ms else ""
        secs   = int(a) % 60
        mins   = int(a) // 60 % 60
        hours  = int(a) // 3600 % 24
        days   = int(a) // 86400

        if max_abs < max_m_thr:
            total_mins = int(a) // 60
            if prefix_only:
                return f":{secs:02d}{ms_str}"
            return f"{sign}{total_mins}:{secs:02d}{ms_str}"
        elif max_abs < max_h_thr:
            if prefix_only:
                return f":{secs:02d}{ms_str}"
            return f"{sign}{hours}:{mins:02d}:{secs:02d}{ms_str}"
        else:
            if prefix_only:
                return f":{secs:02d}"
            return f"{sign}{days}d {hours:02d}:{mins:02d}:{secs:02d}"

    @staticmethod
    def _eng_strings(values) -> list:
        out = []
        for v in values:
            t = float(v)
            a = abs(t)
            if   a == 0:   out.append("0 s")
            elif a < 1e-9: out.append(f"{t*1e12:.4g} ps")
            elif a < 1e-6: out.append(f"{t*1e9:.4g} ns")
            elif a < 1e-3: out.append(f"{t*1e6:.4g} µs")
            elif a < 1.0:  out.append(f"{t*1e3:.4g} ms")
            elif a < 1e3:  out.append(f"{t:.4g} s")
            else:           out.append(f"{t/1e3:.4g} ks")
        return out

    def _fmt_real_time_strings(self, values, spacing) -> list:
        """Format tick labels in real-time mode.

        The anchor is the EXACT viewport left edge (self.range[0]), not a grid
        boundary.  This means:
          • The anchor label at the left edge always shows the true wall-clock
            time at that pixel, and ticks up/down continuously as you pan.
          • Every visible tick shows its delta from the left edge, so adjacent
            ticks always differ by exactly one major div's worth of time.

        The anchor text is NOT returned as a tick label — it is injected by
        generateDrawSpecs at the left edge so it can never be clipped.

        Only the major-level tickStrings call (largest spacing) establishes the
        anchor; minor-level calls reuse the same anchor_t for their deltas.
        """
        from datetime import timedelta

        if spacing > self._rt_max_spacing:
            # Major-level call — lock in the anchor at the exact left edge
            self._rt_max_spacing = spacing
            try:
                t_anchor = float(self.range[0])
            except Exception:
                t_anchor = float(values[0])
            anchor_dt = self._t0_wall_clock_dt + timedelta(seconds=t_anchor)
            self._rt_anchor_label = self._fmt_rt_anchor(anchor_dt, spacing)
            self._rt_anchor_t = t_anchor

        t_anchor = self._rt_anchor_t
        return [self._fmt_rt_delta(float(v) - t_anchor, spacing) for v in values]

    def _inject_rt_anchor(self, tickSpecs, textSpecs, p):
        """Inject the anchor label and accent line at pixel x=0 (left viewport edge).

        1. Draws a thin accent-coloured vertical line at x=0 spanning the full
           axis + plot height, giving a visual anchor for the timestamp.
        2. Measures the rendered width of the anchor text and removes any tick
           labels whose left edge would overlap it.
        3. Appends the anchor label as a left-aligned, always-in-bounds textSpec.
        """
        from PyQt6.QtCore import QRectF, QPointF, Qt
        from PyQt6.QtGui import QPen, QColor
        label = self._rt_anchor_label
        if not label:
            return

        bounds = self.boundingRect()

        # ── 1. Accent line ────────────────────────────────────────────────────
        # Extend from the top of the linked view (into the plot area) through
        # the full axis height so the line visually connects label to data.
        lv = self.linkedView()
        if lv is not None and self.grid is not False:
            tb = lv.mapRectToItem(self, lv.boundingRect())
            line_top = tb.top()
        else:
            line_top = bounds.top()
        accent_pen = QPen(QColor(self._rt_accent_color), 1.5)
        tickSpecs.append((accent_pen, QPointF(0, line_top), QPointF(0, bounds.bottom())))

        # ── 2. Borrow text-row geometry from existing tick labels ─────────────
        if textSpecs:
            sample = textSpecs[0][0]
            y, h = sample.y(), sample.height()
        else:
            h = 12.0
            y = bounds.bottom() - h - 2

        # ── 3. Measure anchor label width and suppress overlapping tick labels ─
        measure = QRectF(0, 0, 2000, 100)
        anchor_w = p.boundingRect(measure, Qt.AlignmentFlag.AlignLeft, label).width()
        # A tick label is centred at its tick x-position.
        # It overlaps the anchor if  (tick_x - label_w/2)  < anchor_w.
        filtered = []
        for spec in textSpecs:
            rect, flags, text = spec
            tick_cx = rect.center().x()
            lbl_w   = rect.width()
            if tick_cx - lbl_w / 2 < anchor_w:
                continue   # would overlap — suppress
            filtered.append(spec)
        textSpecs[:] = filtered

        # ── 4. Inject anchor label ─────────────────────────────────────────────
        rect  = QRectF(bounds.left() + 1, y, bounds.width() - 2, h)
        flags = (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter |
                 Qt.TextFlag.TextDontClip)
        textSpecs.append((rect, flags, label))

    @staticmethod
    def _fmt_rt_anchor(dt, spacing: float) -> str:
        """Absolute datetime label for the anchor (first visible major tick).
        Precision tracks the tick spacing: ms when spacing < 1 s, else tenths.
        """
        if spacing < 1e-3:
            # microsecond spacing or finer — ms precision is plenty (per spec)
            ms = dt.microsecond // 1000
            return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{ms:03d}"
        else:
            tenths = dt.microsecond // 100000
            return dt.strftime("%Y-%m-%d %H:%M:%S.") + str(tenths)

    @staticmethod
    def _fmt_rt_delta(delta_s: float, spacing: float) -> str:
        """Relative '+delta' label for every tick after the anchor."""
        a = abs(delta_s)
        sign = "+" if delta_s >= 0 else "−"
        if spacing >= 3600:
            h  = int(a) // 3600
            m  = int(a) // 60 % 60
            s  = int(a) % 60
            return f"{sign}{h}:{m:02d}:{s:02d}"
        elif spacing >= 60:
            total_m = int(a) // 60
            s       = int(a) % 60
            frac    = round((a % 1) * 10)
            return f"{sign}{total_m}:{s:02d}.{frac}"
        elif spacing >= 1:
            frac = round((a % 1) * 10)
            return f"{sign}{int(a)}.{frac}"
        elif spacing >= 1e-3:
            return f"{sign}{a * 1e3:.4g}ms"
        elif spacing >= 1e-6:
            return f"{sign}{a * 1e6:.4g}µs"
        elif spacing >= 1e-9:
            return f"{sign}{a * 1e9:.4g}ns"
        else:
            return f"{sign}{a:.4g}s"


# ── Y-axis: physical units with SI prefix ─────────────────────────────────────

class EngineeringAxisItem(pg.AxisItem):
    """
    Y-axis that labels ticks as  '1 mV', '-500 µV', '1.5 V' etc.
    Set unit via .set_unit(str). Empty/None unit falls back to plain numbers.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._unit = ""
        self._div_cfg: dict = {}
        self._ch_name: str = ""
        self._last_tick_values: list = []   # [(spacing, [val, ...]), ...] from last tickValues call

    def set_ch_name(self, name: str):
        self._ch_name = name or ""

    def set_unit(self, unit: str):
        self._unit = unit or ""
        self.picture = None   # invalidate cached axis rendering
        self.update()         # schedule repaint so tickStrings() runs with new unit

    def set_div_settings(self, cfg: dict):
        self._div_cfg = cfg or {}
        self.picture = None
        self.update()

    def tickSpacing(self, minVal, maxVal, size):
        ticks = super().tickSpacing(minVal, maxVal, size)
        if not ticks or maxVal <= minVal or size <= 0:
            return ticks
        major = ticks[0][0]
        if major <= 0:
            return ticks
        # size is in logical pixels; user thresholds are in physical pixels.
        # Multiply by DPR so the comparison is apples-to-apples.
        from PyQt6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        dpr = screen.devicePixelRatio() if screen else 1.0
        px_per_major = size * major / (maxVal - minVal) * dpr
        result = [(major, ticks[0][1])]
        if px_per_major >= self._div_cfg.get("div_tenths_px", 60):
            result.append((major / 10.0, 0))
        elif px_per_major >= self._div_cfg.get("div_fifths_px", 30):
            result.append((major / 5.0, 0))
        elif px_per_major >= self._div_cfg.get("div_halves_px", 15):
            result.append((major / 2.0, 0))
        self._last_tick_result = result   # cache for status-bar readback
        return result

    def tickValues(self, minVal, maxVal, size):
        levels = super().tickValues(minVal, maxVal, size)
        self._tick_level_counts = [len(lvl[1]) for lvl in levels]
        self._last_tick_values = [(sp, list(vals)) for sp, vals in levels]
        return levels

    def generateDrawSpecs(self, p):
        axisSpec, tickSpecs, textSpecs = super().generateDrawSpecs(p)
        textSpecs = _filter_dense_labels(textSpecs)
        if not textSpecs and tickSpecs:
            try:
                saved = self._salvage_one_label(tickSpecs)
                if saved:
                    textSpecs = [saved]
            except Exception:
                pass
        self._fix_subdiv_alpha(tickSpecs)
        return axisSpec, tickSpecs, textSpecs

    def _salvage_one_label(self, tickSpecs):
        """When pyqtgraph clips all tick labels off-screen (tight zoom, ticks near
        lane edges), synthesise one label for the major-tick closest to the
        view centre, clamped so its rect stays fully within the axis bounds.

        Reads item-local y-coordinates directly from tickSpecs (which pyqtgraph
        already computed correctly) to avoid any coordinate-mapping issues."""
        if not self._last_tick_values:
            return None
        spacing, vals = self._last_tick_values[0]
        if not vals:
            return None
        n_major = (getattr(self, '_tick_level_counts', None) or [0])[0]
        n_major = min(n_major or len(vals), len(tickSpecs), len(vals))
        if n_major == 0:
            return None
        # Pick the major tick value closest to the view centre
        view = self.linkedView()
        if view is None:
            return None
        vmin, vmax = view.viewRange()[1]
        v_centre = (vmin + vmax) / 2.0
        best_idx = min(range(len(vals)), key=lambda i: abs(vals[i] - v_centre))
        best_val = vals[best_idx]
        # Clamp index to available major tickSpecs
        tick_idx = min(best_idx, n_major - 1)
        _, pt1, pt2 = tickSpecs[tick_idx]
        tick_y = (pt1.y() + pt2.y()) / 2.0   # item-local y from pyqtgraph
        # Format the label
        if self._unit and self._unit != "raw":
            label = _eng_format(float(best_val), self._unit, spacing)
        else:
            strs = super().tickStrings([best_val], 1.0, spacing)
            label = strs[0] if strs else str(best_val)
        # Build rect within axis bounds.
        # boundingRect() expands to include the plot view when grid is enabled,
        # so we use geometry() for the x/width (just the axis strip itself).
        geom = self.geometry()
        axis_w = geom.width()
        axis_h = geom.height()
        tick_font = getattr(self, 'tickFont', None)
        fm = QFontMetrics(tick_font if isinstance(tick_font, QFont) else QFont())
        lh = fm.height()
        top = tick_y - lh / 2.0
        top = max(0.0, min(top, axis_h - lh))
        rect = QRectF(0, top, axis_w, lh)
        flags = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight
        return (rect, int(flags), label)

    def _fix_subdiv_alpha(self, tickSpecs):
        """Re-apply a density-independent alpha to level-1 (sub-div) tick specs.
        pyqtgraph's built-in formula multiplies by 0.05*length/N which renders
        fine sub-divisions nearly invisible.  We override to a fixed fraction."""
        if self.grid is False or not tickSpecs:
            return
        counts = getattr(self, '_tick_level_counts', [])
        if len(counts) < 2:
            return
        n_major = counts[0]
        if len(tickSpecs) <= n_major:
            return
        # Sub-divs should be just barely less prominent than major lines.
        # Major lines end up at alpha≈self.grid; use 0.92 so they're distinguishable.
        sub_alpha = max(40, int(self.grid * 0.92))
        for idx in range(n_major, len(tickSpecs)):
            pen, p1, p2 = tickSpecs[idx]
            pen = QPen(pen)
            c = pen.color()
            c.setAlpha(sub_alpha)
            pen.setColor(c)
            tickSpecs[idx] = (pen, p1, p2)

    def tickStrings(self, values, scale, spacing):
        if not self._unit or self._unit in ("raw", ""):
            return super().tickStrings(values, scale, spacing)
        # Pass spacing so _eng_format uses enough decimal places that
        # adjacent ticks never produce identical labels.
        return [_eng_format(float(v), self._unit, spacing) for v in values]


# ── Range input bar ───────────────────────────────────────────────────────────

class RangeBar(QWidget):
    """Compact X/Y range input bar shown below the plot area."""
    range_changed      = pyqtSignal(float, float, float, float)  # x0,x1,y0,y1
    t0_date_requested  = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)

        layout.addWidget(QLabel("X:"))
        self.x0 = QLineEdit(); self.x0.setFixedWidth(90)
        self.x1 = QLineEdit(); self.x1.setFixedWidth(90)
        layout.addWidget(self.x0)
        layout.addWidget(QLabel("→"))
        layout.addWidget(self.x1)

        layout.addWidget(QLabel("  Y:"))
        self.y0 = QLineEdit(); self.y0.setFixedWidth(80)
        self.y1 = QLineEdit(); self.y1.setFixedWidth(80)
        layout.addWidget(self.y0)
        layout.addWidget(QLabel("→"))
        layout.addWidget(self.y1)

        btn = QPushButton("Apply")
        btn.setMinimumWidth(44)
        btn.setMaximumWidth(88)
        btn.clicked.connect(self._apply)
        layout.addWidget(btn)
        layout.addStretch()

        self._t0_date_btn = QPushButton("Set t=0 date")
        self._t0_date_btn.setMinimumWidth(72)
        self._t0_date_btn.setMaximumWidth(144)
        self._t0_date_btn.setCheckable(True)
        self._t0_date_btn.clicked.connect(self.t0_date_requested)
        layout.addWidget(self._t0_date_btn)

    def set_date_indicator(self, has_date: bool, _accent_colour: str = ""):
        """Toggle the button's checked state to reflect whether a date is set.

        Uses QPushButton:checked from the app stylesheet so the accent colour
        is always up-to-date after theme changes — no manual colour needed.
        """
        self._t0_date_btn.setChecked(has_date)

    def update_display(self, x0, x1, y0, y1):
        def fmt(v):
            if abs(v) < 1e-3 or abs(v) >= 1e6:
                return f"{v:.4e}"
            return f"{v:.6g}"
        for edit, val in [(self.x0, x0), (self.x1, x1),
                           (self.y0, y0), (self.y1, y1)]:
            edit.blockSignals(True)
            edit.setText(fmt(val))
            edit.blockSignals(False)

    def _apply(self):
        try:
            x0 = float(self.x0.text())
            x1 = float(self.x1.text())
            y0 = float(self.y0.text())
            y1 = float(self.y1.text())
            if x0 < x1 and y0 < y1:
                self.range_changed.emit(x0, x1, y0, y1)
        except ValueError:
            pass
