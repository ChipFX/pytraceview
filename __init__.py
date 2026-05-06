"""
pytraceview — portable signal plotting engine for PyQt6 applications.

Quick start:
    from pytraceview import TraceView, ChannelPanel, TraceModel, PlotTheme

    theme = PlotTheme()                # dark defaults
    view  = TraceView(theme=theme)
    panel = ChannelPanel()

    trace = TraceModel("Ch1", segments=[seg])
    view.add_trace(trace)
    panel.add_trace(trace)

    panel.visibility_changed.connect(view.set_trace_visible)
    panel.color_changed.connect(lambda name, c: ...)
    panel.order_changed.connect(view.reorder_traces)

Theme updates from a host theme system:
    theme_manager.themeChanged.connect(
        lambda td: view.apply_theme(td.to_plot_theme()))
"""

from pytraceview.plot_theme    import PlotTheme, DEFAULT_PLOT_THEME
from pytraceview.trace_model   import TraceModel, ScalingConfig
from pytraceview.draw_mode     import (
    DEFAULT_DRAW_MODE, DEFAULT_DENSITY_PEN_MAPPING,
    DRAW_MODE_SIMPLE, DRAW_MODE_FAST, DRAW_MODE_CLEAR, DRAW_MODE_ADVANCED,
    DRAW_MODE_TOOLTIPS,
)
from pytraceview.render_utils  import DEFAULT_LIMITS_CONFIG
from pytraceview.plot_widget   import TraceView
from pytraceview.channel_panel import ChannelPanel, ChannelRow
from pytraceview.grouping_dialog import GroupingDialog

__all__ = [
    "PlotTheme", "DEFAULT_PLOT_THEME",
    "TraceModel", "ScalingConfig",
    "DEFAULT_DRAW_MODE", "DEFAULT_DENSITY_PEN_MAPPING",
    "DRAW_MODE_SIMPLE", "DRAW_MODE_FAST", "DRAW_MODE_CLEAR", "DRAW_MODE_ADVANCED",
    "DRAW_MODE_TOOLTIPS",
    "DEFAULT_LIMITS_CONFIG",
    "TraceView",
    "ChannelPanel", "ChannelRow",
    "GroupingDialog",
]
