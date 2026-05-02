"""
pytraceview/draw_mode.py
Density-aware draw mode strategies and style resolution helpers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Tuple

import numpy as np


DRAW_MODE_SIMPLE = "Simple"
DRAW_MODE_FAST = "Fast"
DRAW_MODE_CLEAR = "Clear"
DRAW_MODE_ADVANCED = "Advanced"
DEFAULT_DRAW_MODE = DRAW_MODE_CLEAR

DRAW_MODE_TOOLTIPS = {
    DRAW_MODE_SIMPLE: (
        "Always uses a fixed thin line. Fastest rendering, no dynamic adjustments."
    ),
    DRAW_MODE_FAST: (
        "Adjusts line thickness based on samples per pixel. Lightweight and responsive."
    ),
    DRAW_MODE_CLEAR: (
        "Uses a perceptual density estimate for improved clarity during zooming."
    ),
    DRAW_MODE_ADVANCED: (
        "Uses the most accurate density estimation. Best visual quality but may impact performance."
    ),
}

DEFAULT_DENSITY_PEN_MAPPING = {
    "min_width": 0.8,
    "max_width": 2.4,
    "response_curve": 0.35,
}


@dataclass(frozen=True)
class RenderViewport:
    width_px: float
    height_px: float
    x_range: Tuple[float, float]
    y_range: Tuple[float, float]
    visible_samples: int


class DensityEstimator(ABC):
    max_segments = 400

    @abstractmethod
    def compute(self, trace, screen_points: np.ndarray, viewport: RenderViewport) -> float:
        raise NotImplementedError

    def _sample_points(self, screen_points: np.ndarray) -> np.ndarray:
        if len(screen_points) <= self.max_segments + 1:
            return screen_points
        idx = np.linspace(0, len(screen_points) - 1, self.max_segments + 1, dtype=int)
        return screen_points[idx]


class SimpleDensityEstimator(DensityEstimator):
    def compute(self, trace, screen_points: np.ndarray, viewport: RenderViewport) -> float:
        return 1e9


class FastDensityEstimator(DensityEstimator):
    def compute(self, trace, screen_points: np.ndarray, viewport: RenderViewport) -> float:
        width = max(1.0, float(viewport.width_px))
        return max(0.0, float(viewport.visible_samples) / width)


class ClearDensityEstimator(DensityEstimator):
    def compute(self, trace, screen_points: np.ndarray, viewport: RenderViewport) -> float:
        pts = self._sample_points(screen_points)
        if len(pts) < 2:
            return 0.0
        delta = np.diff(pts, axis=0)
        dx = np.abs(delta[:, 0])
        dy = np.abs(delta[:, 1])
        valid = dx > 1e-9
        if not np.any(valid):
            return 0.0
        return float(np.mean(dy[valid] / dx[valid]))


class AdvancedDensityEstimator(DensityEstimator):
    def compute(self, trace, screen_points: np.ndarray, viewport: RenderViewport) -> float:
        pts = self._sample_points(screen_points)
        if len(pts) < 2:
            return 0.0
        delta = np.diff(pts, axis=0)
        lengths = np.sqrt(delta[:, 0] ** 2 + delta[:, 1] ** 2)
        width = max(1.0, float(viewport.width_px))
        return float(np.sum(lengths) / width)


def create_density_estimator(draw_mode: str) -> DensityEstimator:
    mode = (draw_mode or DEFAULT_DRAW_MODE).title()
    if mode == DRAW_MODE_SIMPLE:
        return SimpleDensityEstimator()
    if mode == DRAW_MODE_FAST:
        return FastDensityEstimator()
    if mode == DRAW_MODE_ADVANCED:
        return AdvancedDensityEstimator()
    return ClearDensityEstimator()


def resolve_pen_width(density: float, settings: Dict[str, float]) -> float:
    min_width = float(settings.get("min_width", DEFAULT_DENSITY_PEN_MAPPING["min_width"]))
    max_width = float(settings.get("max_width", DEFAULT_DENSITY_PEN_MAPPING["max_width"]))
    alpha = float(settings.get("response_curve", DEFAULT_DENSITY_PEN_MAPPING["response_curve"]))
    width = max_width / (1.0 + max(0.0, alpha) * max(0.0, density))
    return max(min_width, min(max_width, width))
