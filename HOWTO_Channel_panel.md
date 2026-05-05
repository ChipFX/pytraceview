# pytraceview — Using and Extending ChannelPanel

NOTICE:
This HOWTO was written by Claude Code after a very intensive wall-hitting and 
assisting session. It had scanned the application afterwards and attempted to 
write this instruction for anyone, including itself in the future, to avoid 
having to run into all this mess again and again. There may be slight errors, 
but by and large it seems in line with the human co-author's memory, although 
full-review has not happened yet. (This is a hobby project for a 60hr+/week 
engineer!)

---

## Overview

`ChannelPanel` is a full channel management sidebar: drag-to-reorder, group
management, visibility toggle, colour picker, rename, segment scroll wheel, and
accent-coloured group visuals.  It is designed to sit beside a `TraceView`.

```python
from pytraceview import ChannelPanel, TraceView, TraceModel
```

---

## Basic usage

```python
panel = ChannelPanel()
view  = TraceView()

trace = TraceModel("Ch1", segments=[seg])
panel.add_trace(trace)
view.add_trace(trace)

panel.visibility_changed.connect(lambda name, vis: view.refresh_all())
panel.color_changed.connect(lambda name, col: view.refresh_all())
panel.order_changed.connect(view.reorder_traces)
panel.trace_removed.connect(view.remove_trace)
```

---

## Signals

| Signal | Arguments | Notes |
|--------|-----------|-------|
| `visibility_changed` | `str, bool` | trace name, visible |
| `color_changed` | `str, str` | trace name, new hex colour |
| `trace_removed` | `str` | trace name |
| `order_changed` | `list[str]` | ordered trace names |
| `interp_changed` | `str, str` | trace name, mode string |
| `reset_color_requested` | `str` | trace name |
| `trace_renamed` | `str, str` | old name, new label |
| `segment_changed` | `str` | trace name |
| `group_renamed` | `str, str` | old group name, new group name |
| `unit_changed` | `str, str` | trace name, new unit |
| `trace_context_menu_requested` | `str, object` | trace name, global QPoint |

---

## Public API

```python
panel.add_trace(trace)          # add or refresh a TraceModel row
panel.remove_trace(name)        # remove by name
panel.refresh_all()             # repaint all rows (call after trace colour change)
panel.get_ordered_names()       # list[str] in current visual order
panel.set_palette(pv)           # apply colour theme dict (see colour HOWTO)
panel.set_font_scale(scale)     # float, e.g. 1.0 or 1.3
panel.set_scroll_primaries(b)   # enable wheel-to-step-segment on rows
```

---

## Subclassing for app-specific controls

The base `ChannelPanel` intentionally excludes oscilloscope-specific controls.
Use the `_setup_extra_button_rows(layout)` hook to inject additional button
rows **above** the New Group / Group… controls:

```python
from pytraceview.channel_panel import ChannelPanel as _Base
from PyQt6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout
from PyQt6.QtCore import pyqtSignal

class MyChannelPanel(_Base):
    mode_changed = pyqtSignal(str)

    def _setup_extra_button_rows(self, layout: QVBoxLayout):
        row = QHBoxLayout()
        row.setContentsMargins(4, 0, 4, 4)
        self._btn_a = QPushButton("Mode A")
        self._btn_b = QPushButton("Mode B")
        self._btn_a.clicked.connect(lambda: self.mode_changed.emit("a"))
        self._btn_b.clicked.connect(lambda: self.mode_changed.emit("b"))
        row.addWidget(self._btn_a)
        row.addWidget(self._btn_b)
        layout.addLayout(row)

    def _apply_button_styles(self):
        super()._apply_button_styles()      # always call super first
        if not hasattr(self, '_btn_a'):
            return                           # guard: called before hook runs
        fs = max(8, int(round(11 * self._font_scale * 0.9)))
        self._btn_a.setStyleSheet(f"font-size: {fs}px;")
        self._btn_b.setStyleSheet(f"font-size: {fs}px;")

    def _update_minimum_width(self):
        # Override if your buttons are wider than New Group + Group…
        if not hasattr(self, '_btn_a'):
            super()._update_minimum_width()
            return
        for btn in (self._btn_a, self._btn_b):
            btn.ensurePolished()
        needed = self._btn_a.sizeHint().width() + self._btn_b.sizeHint().width() + 11
        self.setMinimumWidth(max(170, needed))
```

### Hook call-time guarantee

`_setup_extra_button_rows(layout)` is called once during `__init__`, after the
All/None row is in the layout and before the New Group/Group… row is added.
`_apply_button_styles()` is called from both `set_palette()` and `set_font_scale()`.
The `if not hasattr(self, '_btn_a'): return` guard is needed because
`_apply_button_styles()` is also called during the base `__init__` (before your
hook has run).

---

## Group visual system

The group membership visuals form a closed box around each group:

- **Header item background** — solid accent colour (via `item.setBackground()`)
- **Left and right rails** — 4 px accent-coloured QFrame strips
- **Channel row tint** — accent at ~13 % alpha (via `item.setBackground()`)
- **Floor separator** — 6 px accent-coloured bar after the last member
- **Selection** — grouped rows keep the tint; ungrouped rows show accent at
  ~35 % alpha when selected

All backgrounds use `item.setBackground(QBrush)` rather than widget-level
palette or stylesheet.  Qt's QListWidget overpaints widget-level palette
changes on embedded item widgets; item-level brushes are the only layer it
does not override.

---

## Tooltip styling inside the panel

See `HOWTO_Apply_colour_scheme.md` for the full explanation.  The short version:

- `ChannelRow` uses `_TooltipFilter` on the row widget itself.  The tooltip
  event propagates from child widgets (label, colour swatch) up to the row.
- Group header buttons use `_TooltipFilter` with `widget_fn` pointing at the
  `ChannelPanel` (no stylesheet), avoiding the dark-border bleed caused by
  `background: transparent` on the button stylesheet.
- All tooltip colours update automatically via `set_palette()`.

---

## Drag-to-reorder and group membership

Channels can be dragged within and between groups using Qt's built-in
`InternalMove` drag.  On drop, `rowsMoved` is used to identify **only the
moved item** and determine its new group from the item above it in the list:

- A floor separator above the drop position → ungrouped territory
- A group header above the drop position → that group

This means bystander channels are never accidentally re-assigned.  Empty groups
(header with no members) survive drags and theme rebuilds; they are only
removed when the last member is explicitly deleted (`remove_trace`).
