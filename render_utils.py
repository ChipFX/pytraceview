"""
pytraceview/render_utils.py
Signal-processing helpers and internal rendering context used by
TraceLane, OverlayTraceVisual, and TraceView.

Public API surface:
  DEFAULT_LIMITS_CONFIG   — default viewport-limit config dict
  TraceStyleContext        — frozen dataclass bundling theme + draw settings
  downsample_for_display  — min/max decimation for display
  sinc_interpolate_to_n   — bandlimited upsampling via FFT zero-padding
  cubic_interpolate_to_n  — cubic spline upsampling via scipy

Internal helpers (used inside pytraceview only):
  _style_context_from_plot_theme
  _effective_color
  _trace_primary_segment_points  — returns one Segment's display arrays
  _windowed_render_points        — clips (t, y) to the visible x range
  _interpolated_trace_value      — linear interp at a cursor position
  _resolve_display_limit
  _upsample_for_display
"""

import math
import numpy as np
from dataclasses import dataclass
from typing import Optional, Tuple

from pytraceview.draw_mode import (
    DEFAULT_DENSITY_PEN_MAPPING,
    DEFAULT_DRAW_MODE,
    RenderViewport,
    create_density_estimator,
    resolve_pen_width,
)
from pytraceview.trace_model import TraceModel, Segment
from pytraceview.plot_theme import PlotTheme, DEFAULT_PLOT_THEME


MAX_DISPLAY_POINTS = 50_000   # kept for any external references; internal logic uses _limits_config

# Default viewport-limits configuration.  Passed to TraceView at construction
# and propagated to TraceLane / OverlayTraceVisual.
DEFAULT_LIMITS_CONFIG: dict = {
    "mode":          "window",  # "window" or "preset"
    "scale_min_px":  2,         # window mode — floor: pts = max(preset_min, scale_min * width)
    "scale_max_px":  12,        # window mode — ceiling: pts = scale_max * width
    "preset_min":    2048,      # absolute floor in both modes
    "preset_max":    50_000,    # preset mode limit
}


def _resolve_display_limit(limits_config: dict, width_px: float) -> int:
    """Return the max-points cap to pass to downsample_for_display.

    window mode : max_pts = max(preset_min, scale_max_px × width_px)
    preset mode : max_pts = preset_max
    width_px < 1: widget not yet shown — fall back to preset_max.
    """
    preset_max = int(limits_config.get("preset_max", 50_000))
    preset_min = int(limits_config.get("preset_min", 2_048))
    if width_px < 1:
        return preset_max
    if limits_config.get("mode", "window") != "window":
        return preset_max
    scale_max = int(limits_config.get("scale_max_px", 12))
    scale_min = int(limits_config.get("scale_min_px", 2))
    lo = max(preset_min, int(width_px * scale_min))
    hi = max(lo, int(width_px * scale_max))
    return hi


def downsample_for_display(t, y, max_pts=MAX_DISPLAY_POINTS):
    n = len(t)
    if n <= max_pts:
        return t, y
    window = max(1, n // (max_pts // 2))
    n_windows = n // window
    n_use = n_windows * window
    # Reshape into (n_windows, window) blocks — all argmin/argmax in one numpy call
    t_w = t[:n_use].reshape(n_windows, window)
    y_w = y[:n_use].reshape(n_windows, window)
    imin = np.argmin(y_w, axis=1)
    imax = np.argmax(y_w, axis=1)
    row = np.arange(n_windows)
    t_min = t_w[row, imin];  y_min = y_w[row, imin]
    t_max = t_w[row, imax];  y_max = y_w[row, imax]
    # Interleave: emit min first when it comes before max in time, else swap
    swap = imin > imax
    t_out = np.empty(n_windows * 2)
    y_out = np.empty(n_windows * 2)
    t_out[0::2] = np.where(swap, t_max, t_min)
    y_out[0::2] = np.where(swap, y_max, y_min)
    t_out[1::2] = np.where(swap, t_min, t_max)
    y_out[1::2] = np.where(swap, y_min, y_max)
    return t_out, y_out


def sinc_interpolate_to_n(t: np.ndarray, y: np.ndarray,
                           target_n: int) -> tuple:
    """
    Bandlimited sinc interpolation via FFT zero-padding.
    Upsamples y to exactly target_n points spread evenly over [t[0], t[-1]].
    Only upsamples (target_n > len(y)); pass-through if not needed.
    """
    n = len(y)
    if n < 4 or target_n <= n:
        return t, y
    upsample = max(2, (target_n + n - 1) // n)
    n_new = n * upsample
    Y = np.fft.rfft(y)
    Y_pad = np.zeros(n_new // 2 + 1, dtype=complex)
    copy_len = min(len(Y), len(Y_pad))
    Y_pad[:copy_len] = Y[:copy_len] * upsample
    y_new = np.fft.irfft(Y_pad, n_new)
    t_new = np.linspace(t[0], t[-1], n_new, endpoint=False)
    return t_new, y_new


# Keep old name for backward compat (used in tests)
def sinc_interpolate(t, y, upsample=8):
    return sinc_interpolate_to_n(t, y, len(y) * upsample)


def cubic_interpolate_to_n(t: np.ndarray, y: np.ndarray,
                            target_n: int) -> tuple:
    """
    Cubic spline interpolation via scipy CubicSpline (not-a-knot boundary).
    Pass-through if target_n <= len(y) or len(y) < 4.
    Falls back to sinc if scipy fails.
    """
    n = len(y)
    if n < 4 or target_n <= n:
        return t, y
    try:
        from scipy.interpolate import CubicSpline
        cs = CubicSpline(t, y, bc_type='not-a-knot')
        t_new = np.linspace(t[0], t[-1], target_n)
        return t_new, cs(t_new)
    except Exception:
        return sinc_interpolate_to_n(t, y, target_n)


def _upsample_for_display(
        t: np.ndarray, y: np.ndarray,
        interp_mode: str, viewport_min_pts: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """Apply sinc/cubic upsampling to a short data segment if needed.

    Used for retrigger result curves so they receive the same display
    interpolation as the raw trace lanes.
    """
    if interp_mode not in ("sinc", "cubic") or len(t) < 4:
        return t, y
    if len(t) >= viewport_min_pts:
        return t, y
    if interp_mode == "cubic":
        return cubic_interpolate_to_n(t, y, viewport_min_pts)
    return sinc_interpolate_to_n(t, y, viewport_min_pts)


# ── Rendering context ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TraceStyleContext:
    theme: PlotTheme
    plot_colors: dict
    theme_name: str
    draw_mode: str
    density_pen_mapping: dict


def _style_context_from_plot_theme(plot_theme: PlotTheme,
                                   draw_mode: str = DEFAULT_DRAW_MODE,
                                   density_pen_mapping: Optional[dict] = None
                                   ) -> TraceStyleContext:
    return TraceStyleContext(
        theme=plot_theme,
        plot_colors={
            "background": plot_theme.background,
            "grid":       plot_theme.grid,
            "text":       plot_theme.text,
            "cursor_a":   plot_theme.cursor_a,
            "cursor_b":   plot_theme.cursor_b,
        },
        theme_name=plot_theme.theme_id,
        draw_mode=draw_mode,
        density_pen_mapping=dict(
            DEFAULT_DENSITY_PEN_MAPPING if density_pen_mapping is None
            else density_pen_mapping),
    )


def _effective_color(color: str, theme_name: str) -> str:
    """Override trace color for special themes."""
    if theme_name == "rs_green":
        return "#00ee44"
    return color


def _trace_primary_segment_points(
        trace: TraceModel,
        process_segments: bool,
) -> Tuple[np.ndarray, np.ndarray]:
    """Return (time, scaled_data) for the segment used as the main render curve.

    When process_segments=True and primary_segment is set, returns that
    specific segment.  Otherwise returns segments[0] — which is the whole
    trace for single-segment captures, or the fallback when segment
    differentiation is disabled.

    time_offset is applied here so all callers get display-ready time values.
    """
    if process_segments and trace.primary_segment is not None:
        seg = trace.segments[trace.primary_segment]
    else:
        seg = trace.segments[0]
    return seg.time + trace.time_offset, trace.segment_processed(seg)


def _windowed_render_points(
        t_points: np.ndarray,
        y_points: np.ndarray,
        x0: float,
        x1: float,
) -> Tuple[np.ndarray, np.ndarray]:
    """Clip to the current X window when enough samples are visible."""
    mask = (t_points >= x0) & (t_points <= x1)
    if int(mask.sum()) >= 2:
        return t_points[mask], y_points[mask]
    return t_points, y_points


def _interpolated_trace_value(
        t_points: np.ndarray,
        y_points: np.ndarray,
        t_pos: float,
) -> Optional[float]:
    """Return the interpolated trace value at t_pos within one segment's arrays."""
    if len(t_points) < 2:
        return None
    if t_pos < float(t_points[0]) or t_pos > float(t_points[-1]):
        return None
    idx = int(np.searchsorted(t_points, t_pos))
    idx = max(1, min(idx, len(t_points) - 1))
    t0, t1 = float(t_points[idx - 1]), float(t_points[idx])
    y0, y1 = float(y_points[idx - 1]), float(y_points[idx])
    value = y0 if t1 == t0 else y0 + (y1 - y0) * (t_pos - t0) / (t1 - t0)
    return None if math.isnan(value) else value
