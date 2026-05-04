"""
pytraceview/trace_model.py
Data model for a single signal trace/channel.

Every trace owns one or more Segment objects.  A Segment holds its own
raw data array, time axis, wall-clock anchor, and optional filter result.
There are no flat inter-segment concatenations anywhere in this module.

Scaling pipeline per segment:
  Segment.data  →  ScalingConfig.apply()  →  scaled output
                    (or Segment.filtered_data if a filter is active)

Callers that need a single array use trace.primary() to get the active
segment, then trace.segment_processed(seg) for the scaled result.
"""

import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ScalingConfig:
    """ADC-to-physical-unit scaling: output = (raw * gain) + offset."""
    enabled: bool = False
    # Linear map mode: input_min..input_max -> output_min..output_max
    input_min: float = 0.0
    input_max: float = 4095.0
    output_min: float = -1.25
    output_max: float = 1.25
    unit: str = "V"
    # Direct gain/offset mode (takes precedence when use_gain_offset=True)
    use_gain_offset: bool = False
    gain: float = 1.0
    offset: float = 0.0

    def apply(self, data: np.ndarray) -> np.ndarray:
        if not self.enabled:
            return data
        if self.use_gain_offset:
            return data * self.gain + self.offset
        in_range = self.input_max - self.input_min
        out_range = self.output_max - self.output_min
        if in_range == 0:
            return data
        return (data - self.input_min) / in_range * out_range + self.output_min

    @property
    def display_unit(self):
        return self.unit


@dataclass
class Segment:
    """One contiguous capture block within a trace.

    A trace always has at least one Segment.  Multi-trigger captures
    (oscilloscope, LeCroy), sweep histories (VNA), and live DAQ scans
    each map naturally to one Segment per acquisition event.

    Time and data are always pre-computed — no lazy generation from
    sample_rate.  The caller (importer, live source) builds both arrays
    explicitly before constructing the Segment.

    filtered_data is set per-segment by set_filter() on the owning
    TraceModel.  The filter runs independently on each segment so that
    IIR state never bleeds across acquisition boundaries.
    """
    data:          np.ndarray               # raw samples
    time:          np.ndarray               # pre-computed time axis, same length as data
    t0_absolute:   float = 0.0             # Unix epoch of first sample in this segment
    t0_relative:   float = 0.0             # seconds since segments[0].t0_absolute
    sample_rate:   float = 1.0             # samples per second
    label:         str   = ""              # optional annotation (e.g. "Trigger 3")
    filtered_data: Optional[np.ndarray] = field(default=None, repr=False)


@dataclass
class TraceModel:
    """Data model for one signal channel.

    Segments are the authoritative data source.  There are no flat
    concatenated arrays on this class; callers address segments directly.

    Minimal construction (single capture):
        seg = Segment(data=y, time=t, t0_absolute=time.time(), sample_rate=1e3)
        trace = TraceModel(name="Ch1", segments=[seg])
    """

    name: str
    segments: List[Segment]    # always populated; minimum one element

    # ── Display / palette ─────────────────────────────────────────────
    color:             str  = "#F0C040"
    visible:           bool = True
    label:             str  = ""
    unit:              str  = "V"
    theme_color_index: int  = 0
    use_theme_color:   bool = True

    scaling: ScalingConfig = field(default_factory=ScalingConfig)

    # ── Display state ─────────────────────────────────────────────────
    y_offset:    float = 0.0
    y_scale:     float = 1.0
    display_row: int   = 0

    # ── Instrument metadata ───────────────────────────────────────────
    coupling:  str = ""
    impedance: str = ""
    bwlimit:   str = ""

    # ── Source provenance ─────────────────────────────────────────────
    source_file:       str = ""
    original_col_name: str = ""
    col_group:         str = ""

    # Informational: how the time axis was originally sourced.
    #   "seconds_relative" — float seconds from t=0
    #   "unix_epoch"       — was Unix epoch; converted at import time
    #   "datetime:<fmt>"   — was datetime strings; converted at import time
    source_time_format: str = "seconds_relative"

    # ── Time shift ────────────────────────────────────────────────────
    # Applied additively when reading segment times:  t_display = seg.time + time_offset
    # "Set t=0 here" → time_offset -= cursor_t
    # "Restore t=0"  → time_offset  = 0.0
    time_offset: float = 0.0

    # ── Segment display control ───────────────────────────────────────
    # primary_segment: index of the segment shown as the main curve, or
    # None meaning all segments are treated equally (no primary).
    primary_segment:     Optional[int] = None
    # How non-primary segments are rendered when primary_segment is set.
    # Values: "dimmed" | "dashed" | "hide" | "" (default = "dimmed")
    non_primary_viewmode: str = ""

    # ── Per-trace annotations ─────────────────────────────────────────
    # list of (time_position, label_text) drawn on the plot
    trace_labels: list = field(default_factory=list)

    # ── Retrigger flag ────────────────────────────────────────────────
    retrigger_extrapolating: bool = False

    # ── Periodicity estimate ──────────────────────────────────────────
    period_estimate:             float = 0.0
    period_confidence:           float = 0.0
    period_estimation_attempted: bool  = False

    # ── Filter description ────────────────────────────────────────────
    # The result lives in each Segment.filtered_data; this string
    # describes what filter was applied (e.g. "LP Butter 1 kHz").
    _filter_desc: str = field(default="", repr=False)

    # ── Internal cache ────────────────────────────────────────────────
    _processed_data_cache: Optional[np.ndarray] = field(default=None, repr=False)

    def __post_init__(self):
        if not self.label:
            self.label = self.name

    # ── Convenience accessors ─────────────────────────────────────────

    @property
    def sample_rate(self) -> float:
        """Sample rate of the primary segment.

        Convenience proxy so callers don't have to write
        trace.primary().sample_rate everywhere.  Avoids precision loss
        from repeated dt=1/sps → sps=1/dt round-trips.
        """
        return self.primary().sample_rate

    @property
    def dt(self) -> float:
        """Sample interval (1/sample_rate) of the primary segment."""
        sps = self.primary().sample_rate
        return 1.0 / sps if sps > 0 else 1.0

    # ── Primary segment accessor ──────────────────────────────────────

    def primary(self) -> Segment:
        """Return the active (or only) segment.

        Code that legitimately needs one contiguous array — FFT, trigger
        detection, periodicity — calls this explicitly rather than
        receiving a hidden flat concatenation.
        """
        idx = self.primary_segment if self.primary_segment is not None else 0
        return self.segments[idx]

    # ── Scaled data ───────────────────────────────────────────────────

    def segment_processed(self, seg: Segment) -> np.ndarray:
        """Return scaled data for one segment (filtered if active, else raw)."""
        src = seg.filtered_data if seg.filtered_data is not None else seg.data
        return self.scaling.apply(src)

    # ── Filter ────────────────────────────────────────────────────────

    def set_filter(self, filtered_per_segment: list, description: str = ""):
        """Store per-segment filter results.

        filtered_per_segment must be a list matching len(self.segments).
        Each element is an np.ndarray (same length as the segment's data)
        or None (segment not filtered, e.g. too short for the filter order).

        The filter is run independently on each segment so that IIR
        state never bleeds across acquisition boundaries.
        """
        for seg, fdata in zip(self.segments, filtered_per_segment):
            seg.filtered_data = fdata
        self._filter_desc = description

    def clear_filter(self):
        for seg in self.segments:
            seg.filtered_data = None
        self._filter_desc = ""

    @property
    def has_filter(self) -> bool:
        return any(s.filtered_data is not None for s in self.segments)

    @property
    def filter_description(self) -> str:
        return self._filter_desc

    # ── Windowed data (primary segment) ───────────────────────────────

    def windowed_data(self, t_start: float, t_end: float):
        """Return (time, scaled_data) for the primary segment, masked to [t_start, t_end].

        Used by FFT dialog and analysis plugins that operate on one
        contiguous time window.  Applies time_offset before masking.
        """
        seg  = self.primary()
        t    = seg.time + self.time_offset
        y    = self.segment_processed(seg)
        mask = (t >= t_start) & (t <= t_end)
        return t[mask], y[mask]

    # ── Convenience metrics ───────────────────────────────────────────

    @property
    def duration(self) -> float:
        """Total duration across all segments in seconds."""
        return sum(
            (float(s.time[-1]) - float(s.time[0]))
            for s in self.segments if len(s.time) > 1
        )

    @property
    def n_samples(self) -> int:
        """Total sample count across all segments."""
        return sum(len(s.data) for s in self.segments)

    # ── Scaling ───────────────────────────────────────────────────────

    def update_scaling(self, scaling: ScalingConfig):
        self.scaling = scaling

    # ── Color management ──────────────────────────────────────────────

    def set_user_color(self, color: str):
        self.color = color
        self.use_theme_color = False

    def reset_color_to_theme(self, index: Optional[int] = None):
        if index is not None:
            self.theme_color_index = index
        self.use_theme_color = True

    def sync_theme_color(self, theme) -> str:
        if self.use_theme_color:
            self.color = theme.trace_color(self.theme_color_index)
        return self.color
