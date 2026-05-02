"""
pytraceview/trace_model.py
Data model for a single signal trace/channel.

Scaling pipeline (applied in order):
  raw_data  -> gain -> offset -> display as processed_data

Filters are non-destructive: raw_data is never touched by filters.
filter_data holds the filtered result; processed_data returns
filter_data if a filter is active, otherwise the gain+offset result.
"""

import numpy as np
from dataclasses import dataclass, field
from typing import Optional


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
class TraceModel:
    name: str
    raw_data: np.ndarray
    time_data: Optional[np.ndarray] = None
    sample_rate: float = 1.0
    dt: float = 1.0

    color: str = "#F0C040"
    visible: bool = True
    label: str = ""
    unit: str = "V"
    theme_color_index: int = 0
    use_theme_color: bool = True

    scaling: ScalingConfig = field(default_factory=ScalingConfig)

    # Display state
    y_offset: float = 0.0
    y_scale: float = 1.0
    display_row: int = 0

    # Instrument metadata (from CSV headers or binary import)
    coupling: str = ""
    impedance: str = ""
    bwlimit: str = ""

    # ── Source provenance ─────────────────────────────────────────────
    # Set at import time; readable by trace-manipulation plugins via the
    # traces list that gets passed to plugin.run().

    # Filename the trace was loaded from (basename only)
    source_file: str = ""

    # Column name exactly as it appeared in the source CSV header row,
    # before any display-name override from a parser plugin or the user.
    original_col_name: str = ""

    # Parser-assigned group name (e.g. "Measurements", "Alarms", "Temperature").
    # Empty string if no grouping was provided.
    col_group: str = ""

    # ── Wall-clock time anchor ────────────────────────────────────────
    # ISO 8601 string for the real-world moment that corresponds to t=0 on
    # this trace's time axis.  Empty string if unknown.
    #
    # Examples:
    #   Scope capture  → trigger time, e.g. "2002-03-23T02:21:36"
    #   Data logger    → timestamp of the first imported sample
    #
    # When the user performs "Set t=0 here" (cursor or sample index), the
    # pipeline shifts time_data and updates t0_wall_clock by the same delta
    # so the real-world calendar time remains consistent.
    #
    # The cursor UI uses:  t0_wall_clock_as_datetime + timedelta(seconds=cursor_t)
    # to show "Thursday 12 April 2026  13:44:22.460"
    t0_wall_clock: str = ""

    # Describes how the time axis was sourced; informational for plugins / UI.
    #   "seconds_relative"      — float seconds from t=0, kept as-is
    #   "unix_epoch"            — was Unix epoch; converted to seconds_relative
    #   "datetime:<strptime>"   — was datetime strings; converted to seconds_relative
    source_time_format: str = "seconds_relative"

    # ── Segment metadata ──────────────────────────────────────────────
    # Populated by importers that support multi-segment captures (e.g. LeCroy).
    # Each tuple: (start_index, end_index, t0_absolute, t0_relative)
    #   start_index  : int   — inclusive 0-based index into time_data / raw_data
    #   end_index    : int   — exclusive end index (Python slice convention)
    #   t0_absolute  : float — Unix timestamp of this segment's trigger
    #   t0_relative  : float — seconds since segment-1 trigger (0.0 for seg 1)
    # None means non-segmented or unknown — all code that doesn't know about
    # segments should treat None as "no special handling required".
    segments: Optional[list] = None        # list[tuple[int,int,float,float]] | None
    primary_segment: Optional[int] = None  # 0-based index into segments; None = all equal

    # How non-primary segments are rendered.  Empty string = default behaviour
    # (treated as "regular" until the GUI segment controls are implemented).
    # Valid values: "show_only_primary", "dimmed", "dashed", "regular"
    non_primary_viewmode: str = ""

    # Per-trace labels: list of (time_position, label_text) tuples
    # Each label is drawn as a text annotation anchored to that time point.
    trace_labels: list = field(default_factory=list)

    # Set by retrigger pipeline when the averaged/interpolated curve
    # extends outside the original capture's time bounds.
    retrigger_extrapolating: bool = False

    # Periodicity estimate — computed on load by a periodicity analyser.
    # 0.0 means unknown (estimation disabled, failed, or not yet run).
    # period_confidence is 0.0–1.0; values below ~0.3 should be treated
    # with scepticism.  Both fields are read-only to plugins.
    period_estimate:    float = 0.0
    period_confidence:  float = 0.0
    period_estimation_attempted: bool = False

    # ── Original time-zero anchor ─────────────────────────────────────
    # Set once at import time to time_axis[0] and never changed.
    # Used by the "Restore original t=0" button to undo any t=0 shifts.
    # None means the trace was created before this field existed (safe to skip).
    original_time_zero: Optional[float] = None

    # Non-destructive filter result (None = no filter active)
    _filter_data: Optional[np.ndarray] = field(default=None, repr=False)
    _filter_desc: str = field(default="", repr=False)  # e.g. "LP 1kHz"

    # Cache
    _processed_data: Optional[np.ndarray] = field(default=None, repr=False)
    _computed_time: Optional[np.ndarray] = field(default=None, repr=False)

    def __post_init__(self):
        if not self.label:
            self.label = self.name
        self._invalidate_cache()

    def _invalidate_cache(self):
        self._processed_data = None
        self._computed_time = None

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

    @property
    def processed_data(self) -> np.ndarray:
        """Returns filtered data if a filter is active, else scaled raw data."""
        if self._processed_data is None:
            if self._filter_data is not None:
                self._processed_data = self._filter_data
            else:
                self._processed_data = self.scaling.apply(self.raw_data)
        return self._processed_data

    @property
    def time_axis(self) -> np.ndarray:
        if self._computed_time is None:
            if self.time_data is not None:
                self._computed_time = self.time_data
            else:
                n = len(self.raw_data)
                t0_off = getattr(self, '_t0_sample_offset', 0)
                self._computed_time = (np.arange(n) - t0_off) * self.dt
        return self._computed_time

    def set_sample_rate(self, sps: float):
        self.sample_rate = sps
        self.dt = 1.0 / sps if sps != 0 else 1.0
        self._computed_time = None

    def set_dt(self, dt: float):
        self.dt = dt
        self.sample_rate = 1.0 / dt if dt != 0 else 1.0
        self._computed_time = None

    def update_scaling(self, scaling: ScalingConfig):
        self.scaling = scaling
        self._invalidate_cache()

    def set_filter(self, filtered_data: Optional[np.ndarray], description: str = ""):
        """Apply a non-destructive filter. Pass None to clear."""
        self._filter_data = filtered_data
        self._filter_desc = description
        self._processed_data = None  # invalidate display cache only

    def clear_filter(self):
        self.set_filter(None)

    @property
    def has_filter(self) -> bool:
        return self._filter_data is not None

    @property
    def filter_description(self) -> str:
        return self._filter_desc

    @property
    def duration(self) -> float:
        t = self.time_axis
        return (t[-1] - t[0]) if len(t) > 1 else 0.0

    @property
    def n_samples(self) -> int:
        return len(self.raw_data)

    def windowed_data(self, t_start: float, t_end: float):
        t = self.time_axis
        mask = (t >= t_start) & (t <= t_end)
        return t[mask], self.processed_data[mask]
