# pytraceview — The Segment Data Model

NOTICE:
This HOWTO was written by Claude Code after a very intensive wall-hitting and 
assisting session. It had scanned the application afterwards and attempted to 
write this instruction for anyone, including itself in the future, to avoid 
having to run into all this mess again and again. There may be slight errors, 
but by and large it seems in line with the human co-author's memory, although 
full-review has not happened yet. (This is a hobby project for a 60hr+/week 
engineer!)

---

## Core principle: no flat concatenated arrays, ever

Every `TraceModel` owns a list of `Segment` objects.  Each `Segment` owns its
own `data` and `time` arrays.  There are no flat cross-segment concatenations
anywhere in pytraceview.  Code that needs a single contiguous array must
explicitly request it and must understand it is operating on one segment only.

This design correctly handles oscilloscope multi-trigger captures, VNA sweep
histories, multi-session DAQ recordings, and live ring buffers — all share the
same model with no special cases.

---

## Segment

```python
from pytraceview.trace_model import Segment
import numpy as np

seg = Segment(
    data        = np.array([...]),   # raw samples, shape (N,)
    time        = np.array([...]),   # time axis, same shape as data
    t0_absolute = time.time(),       # Unix epoch float of first sample
    t0_relative = 0.0,               # seconds since segments[0].t0_absolute
    sample_rate = 1e6,               # samples per second
    label       = "Trigger 1",       # optional annotation
)
```

- `data` and `time` are always pre-computed by the caller.  There is no lazy
  generation from `sample_rate`.
- `t0_absolute` is a Unix epoch float, not a datetime string.  Strings belong
  in import/export and display labels only.
- `filtered_data` is `None` by default; set by `TraceModel.set_filter()`.

---

## TraceModel

```python
from pytraceview.trace_model import TraceModel

# Single-segment (most common)
trace = TraceModel(name="Ch1", segments=[seg])

# Multi-segment
trace = TraceModel(name="Ch1", segments=[seg1, seg2, seg3])
```

### Accessing data

```python
seg   = trace.primary()               # the active segment (or segments[0])
y     = trace.segment_processed(seg)  # scaled data (filter applied if active)
t     = seg.time + trace.time_offset  # display time axis
```

Never access `trace.segments[0].data` directly in display code.  Always go
through `segment_processed()` so scaling and filtering are applied.

### primary_segment

```python
trace.primary_segment = 2    # show segment index 2 as the main curve
trace.primary_segment = None # no primary; treat all segments equally
```

The `primary()` convenience method returns `segments[primary_segment]` if set,
else `segments[0]`.

### non_primary_viewmode

Controls how non-primary segments are rendered when a primary is selected:

| Value | Effect |
|-------|--------|
| `""` or `"dimmed"` | drawn at reduced opacity (default) |
| `"dashed"` | drawn with a dashed pen |
| `"hide"` | not drawn |

---

## Time handling

### time_offset — never mutate time arrays

Shifting the time axis is done by adding `trace.time_offset` to `seg.time` at
render time.  **Never modify `seg.time` directly.**

```python
# Set t=0 at cursor position
trace.time_offset -= cursor_t

# Restore original t=0
trace.time_offset = 0.0
```

### Wall-clock anchor

The wall-clock time at display t=0 is:

```python
t0_wall = seg.t0_absolute - trace.time_offset
```

This is derived dynamically; there is no separate stored field.

---

## Filters

Filters run independently on each segment so IIR state never bleeds across
acquisition boundaries:

```python
from scipy import signal as sp_signal

sos = sp_signal.butter(4, fc / (0.5 * sps), btype='low', output='sos')
results = []
for seg in trace.segments:
    if len(seg.data) < 4:
        results.append(None)    # segment too short for this filter order
        continue
    results.append(sp_signal.sosfiltfilt(sos, seg.data))

trace.set_filter(results, description="LP Butter 4 1kHz")
```

```python
trace.clear_filter()
trace.has_filter      # bool
trace.filter_description  # str
```

`segment_processed()` returns `filtered_data` when available, `data` otherwise.
Scaling is always applied after filtering.

---

## Scaling

```python
from pytraceview.trace_model import ScalingConfig

trace.scaling = ScalingConfig(
    enabled    = True,
    input_min  = 0.0,
    input_max  = 4095.0,
    output_min = -1.25,
    output_max = 1.25,
    unit       = "V",
)
```

Or in gain/offset mode:

```python
trace.scaling = ScalingConfig(
    enabled        = True,
    use_gain_offset = True,
    gain           = 0.001,   # raw → physical
    offset         = 0.0,
    unit           = "A",
)
```

Scaling is applied by `segment_processed()`.  Raw data in segments is never
modified.

---

## Colour

```python
# Use theme palette colour (default)
trace.use_theme_color = True
trace.theme_color_index = 3      # index into trace_colors list (wraps)

# Sync to current theme
trace.sync_theme_color(theme_data)   # sets trace.color from theme palette

# Override with user-picked colour
trace.set_user_color("#ff4488")      # sets use_theme_color = False

# Restore theme colour
trace.reset_color_to_theme(index=3)  # sets use_theme_color = True
```

Always call `sync_theme_color()` for every trace — including hidden ones and
those not currently in a plot lane — after a theme change.  `apply_theme()` on
TraceView only syncs traces it knows about directly.

---

## Building traces from imported data

```python
import numpy as np
from pytraceview.trace_model import TraceModel, Segment

# Single capture from a CSV column
t = np.linspace(0, 1e-3, 10000)
y = np.sin(2 * np.pi * 1000 * t)

seg = Segment(
    data        = y,
    time        = t,
    t0_absolute = 1_700_000_000.0,   # Unix epoch at capture time
    sample_rate = 1 / (t[1] - t[0]),
)
trace = TraceModel(name="sine_1kHz", segments=[seg])
```

For multi-segment data (e.g. multi-trigger oscilloscope file):

```python
segments = []
t0_base = capture_epoch
for i, (y_block, t_block, epoch) in enumerate(trigger_blocks):
    seg = Segment(
        data        = y_block,
        time        = t_block,
        t0_absolute = epoch,
        t0_relative = epoch - t0_base,
        sample_rate = 1 / (t_block[1] - t_block[0]),
        label       = f"Trigger {i+1}",
    )
    segments.append(seg)

trace = TraceModel(name="Ch1", segments=segments)
trace.primary_segment = 0   # show first trigger prominently
```

---

## Convenience properties

```python
trace.sample_rate   # sample_rate of primary() segment
trace.dt            # 1 / sample_rate
trace.duration      # total duration across all segments (seconds)
trace.n_samples     # total sample count across all segments
```

`windowed_data(t_start, t_end)` returns `(time, scaled_data)` for the primary
segment masked to the given range, with `time_offset` applied.  Intended for
FFT and analysis that must operate on one contiguous window.
