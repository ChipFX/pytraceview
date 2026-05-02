"""
pytraceview/plot_theme.py
PlotTheme: colour contract between a host application and the plotting engine.

The host creates a PlotTheme (from its own theme system, a config file, or
direct construction) and passes it to TraceView at construction time.
Call TraceView.apply_theme(new_plot_theme) to update colours live.

All fields carry dark-mode defaults so TraceView() with no arguments
already looks reasonable.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class PlotTheme:
    """Colour and palette contract for pytraceview plot widgets.

    Only the keys actually consumed by the plotting engine live here.
    Application-wide stylesheet colours (menus, dialogs, etc.) are the
    host app's responsibility and are not part of this contract.
    """
    background:   str  = "#050508"
    grid:         str  = "#1a2a1a"
    text:         str  = "#e0e0e0"
    cursor_a:     str  = "#ffcc00"
    cursor_b:     str  = "#00ccff"
    accent:       str  = "#1e88e5"
    force_labels: bool = False
    # Used for per-theme style overrides (e.g. the "rs_green" phosphor theme).
    # Set to the theme's file-stem identifier; empty string = no overrides.
    theme_id:     str  = ""
    trace_colors: List[str] = field(default_factory=lambda: [
        "#F0C040", "#40C0F0", "#F04080", "#40F080", "#F08040",
        "#A040F0", "#40F0F0", "#F0F040", "#F04040", "#4080F0",
    ])

    def trace_color(self, index: int) -> str:
        if not self.trace_colors:
            return "#ffffff"
        return self.trace_colors[index % len(self.trace_colors)]


DEFAULT_PLOT_THEME = PlotTheme()
