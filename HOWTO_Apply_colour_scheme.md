# pytraceview — Applying a Colour Scheme

This document covers everything a host application needs to do to make
pytraceview widgets (TraceView, ChannelPanel) fully respect its colour theme.
It also documents hard-won workarounds for Qt 6.7+ behaviour on Windows 11.

NOTICE:
This HOWTO was written by Claude Code after a very intensive wall-hitting and 
assisting session. It had scanned the application afterwards and attempted to 
write this instruction for anyone, including itself in the future, to avoid 
having to run into all this mess again and again. There may be slight errors, 
but by and large it seems in line with the human co-author's memory, although 
full-review has not happened yet. (This is a hobby project for a 60hr+/week 
engineer!)

---

## 1. Qt Style: set Fusion once at startup

**Do this before anything else — before the first `setStyleSheet()` call.**

```python
from PyQt6.QtWidgets import QApplication, QStyleFactory

app = QApplication.instance()
fusion = QStyleFactory.create("Fusion")
if fusion:
    app.setStyle(fusion)
```

### Why

Qt 6.7+ on Windows 11 defaults to the native "windows11" Fluent/WinUI style
(QTBUG-134497).  This style renders many widget states through the native OS
compositor, **ignoring Qt stylesheet rules** for:

- `QPushButton:hover { background-color: ... }` — hover states
- `QToolTip { background-color: ... }` — tooltip background
- Other interactive pseudo-states (`:checked`, `:focus`, etc.)

Fusion is Qt's own fully-drawn style.  With Fusion active, **every stylesheet
rule works as documented**.

### Critical: call setStyle() only once

`QApplication.setStyle()` resets the application font as a side-effect.  If
you call it on every theme change, the font scale/size set by your app will be
lost each time.  Guard it:

```python
if not getattr(app, '_fusion_applied', False):
    fusion = QStyleFactory.create("Fusion")
    if fusion:
        app.setStyle(fusion)
    app._fusion_applied = True
```

---

## 2. Application stylesheet — include a QToolTip rule

Add a `QToolTip { }` block to whatever stylesheet you apply to
`QApplication.instance()`:

```python
app.setStyleSheet(f"""
    /* ... all your other rules ... */
    QToolTip {{
        color: {text};
        background-color: {bg};
        border: 1px solid {border};
    }}
""")
```

Use the theme's `text` colour for the tooltip text and `bg` for the background.
This covers most widgets in the application.

### Re-apply the stylesheet on every theme change

Every call to `app.setStyleSheet()` replaces the previous one entirely.
Rebuild the full stylesheet from your active theme data each time the user
switches themes.

### QToolTip.setPalette() is unreliable on Windows 11

You may see advice to call `QToolTip.setPalette(pal)`.  On Windows 11 with
the native style, `QApplication.setPalette()` silently resets the
QToolTip palette immediately after.  Even with Fusion style active, this call
is not reliably respected.  Prefer the stylesheet rule above; it takes
precedence over the palette for tooltip rendering.

---

## 3. Widgets with transparent stylesheets need _TooltipFilter

Any widget that has `background: transparent` or `background-color: transparent`
in its own stylesheet will bleed that transparency into the tooltip popup window.
The result: the OS/palette ToolTipBase colour (often dark) shows as a border or
fill around the themed HTML content.

The fix is an event filter that intercepts `QEvent.Type.ToolTip` and calls
`QToolTip.showText()` with an HTML table tooltip, passing a **stylesheet-free**
widget as the third argument.  Qt uses that widget's style context to render the
tooltip window — no stylesheet means no transparent-background bleed.

```python
class _TooltipFilter(QObject):
    def __init__(self, bg_fn, text_fn, parent=None, widget_fn=None):
        super().__init__(parent)
        self._bg_fn     = bg_fn      # callable -> hex colour string
        self._text_fn   = text_fn    # callable -> hex colour string
        self._widget_fn = widget_fn  # callable -> QWidget with no stylesheet

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.ToolTip:
            tip = obj.toolTip()
            if not tip:
                return False
            bg, fg = self._bg_fn(), self._text_fn()
            html = (f'<table cellspacing="0" cellpadding="3"'
                    f' style="background-color:{bg}; margin:0px;">'
                    f'<tr><td style="color:{fg};">{tip}</td></tr></table>')
            show_w = self._widget_fn() if self._widget_fn else obj
            QToolTip.showText(event.globalPos(), html, show_w)
            return True
        return False
```

Key rules:
- `bg_fn` and `text_fn` are callables, not values, so they reflect the
  current theme even if the filter was created at startup.
- `widget_fn` should return a widget that has **no `setStyleSheet()` call**
  on it.  A plain QWidget container works.  The ChannelPanel widget itself
  is a safe choice for anything inside the channel panel.
- If a widget has no tooltip text (`toolTip()` returns `""`), return `False`
  so the event propagates naturally up the widget tree.
- Parent the filter object to a widget that will outlive it (e.g. `parent=self`
  inside the widget that owns the tooltip).

### When to use _TooltipFilter

Install it on any widget that:
- Has `background: transparent` in its stylesheet **AND**
- Has tooltip text you want to show in themed colours

Do **not** set a tooltip on the transparent widget itself; instead clear the
tooltip and let the event propagate to a non-transparent parent that has the
filter installed.  Qt propagates `ToolTip` events up the widget tree when the
receiving widget has no tooltip text.

---

## 4. ChannelPanel.set_palette(pv)

Call this after every theme change.  `pv` is a flat dict of colour strings.

### Required keys

| Key | Used for |
|-----|----------|
| `bg` | Tooltip background; group header QToolTip rule; accent tint calculation |
| `bg_panel` | "CHANNELS" header bar background |
| `text` | "CHANNELS" header text; group header tooltip text; button font scale |
| `accent` | Group header item background; left/right rail stripes; selection tint |
| `border` | QToolTip border in the QListWidget stylesheet |

### Optional keys (TraceLab subclass)

| Key | Used for |
|-----|----------|
| `interp_sinc_color` | "All Sinc" button text colour |
| `interp_cub_color` | "All Cub" button text colour |

### Call order matters

```python
# Correct order when handling a theme change:
app.setStyleSheet(build_full_stylesheet(new_theme))   # 1. app stylesheet first
channel_panel.set_palette(new_theme.plotview_dict())  # 2. then panel palette
trace_view.apply_theme(new_theme.to_plot_theme())     # 3. then plot theme
for trace in all_traces:
    trace.sync_theme_color(new_theme)                 # 4. sync hidden traces too
channel_panel.refresh_all()                           # 5. repaint rows
```

Step 4 is easy to forget: `apply_theme()` only syncs traces that have active
lanes in the view.  Traces that are hidden or not yet plotted need explicit
`sync_theme_color()`.

---

## 5. TraceView.apply_theme(PlotTheme)

```python
from pytraceview.plot_theme import PlotTheme

theme = PlotTheme(
    background   = "#050508",   # plot area background
    grid         = "#1a2a1a",   # grid lines
    text         = "#e0e0e0",   # axis labels, legend text
    cursor_a     = "#ffcc00",   # primary cursor colour
    cursor_b     = "#00ccff",   # secondary cursor colour
    accent       = "#1e88e5",   # highlights, selection
    force_labels = False,       # if True, always show lane labels
    theme_id     = "dark",      # arbitrary string identifier
    trace_colors = ["#F0C040", "#40C0F0", ...],
)
view.apply_theme(theme)
```

---

## 6. trace_colors — flexible length

`trace_colors` is a list of hex colour strings.  It can be any length from
1 upward.  `TraceModel.theme_color_index` is mapped to a colour using modulo:

```python
color = trace_colors[theme_color_index % len(trace_colors)]
```

- **1 colour**: every trace gets the same colour.
- **10 colours**: the default TraceLab palette; cycles every 10 traces.
- **100+ colours**: works fine; just slower to build a theme file.

There is no maximum.  The list wraps automatically, so a 3-colour palette on a
20-trace session cycles through the 3 colours repeatedly.

### In a theme JSON file

```json
{
  "plotview": { ... },
  "trace_colors": [
    "#F0C040",
    "#40C0F0",
    "#F04080"
  ]
}
```

An empty list falls back to `"#ffffff"` (white) in `ThemeData.trace_color()`.

---

## 7. Minimal theme-change handler skeleton

```python
def apply_theme(self, theme_data):
    app = QApplication.instance()

    # Fusion style — once only (resets font if called repeatedly)
    if not getattr(app, '_fusion_applied', False):
        if f := QStyleFactory.create("Fusion"):
            app.setStyle(f)
        app._fusion_applied = True

    # Full application stylesheet (must include QToolTip rule)
    app.setStyleSheet(theme_data.get_stylesheet())

    pv = theme_data.plotview_palette()          # dict of colour strings
    self._channel_panel.set_palette(pv)
    self._plot.apply_theme(theme_data.to_plot_theme())

    for trace in self._traces:
        trace.sync_theme_color(theme_data)

    self._channel_panel.refresh_all()
    self._plot.refresh_all()
```
