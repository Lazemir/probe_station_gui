"""Pure selection of a central design structure for focus registration."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable


Bounds = tuple[float, float, float, float]


@dataclass(frozen=True)
class FocusCandidate:
    center: tuple[float, float]
    bounds: Bounds
    distance_from_design_center: float


def select_central_focus_candidate(
    *,
    design_bounds: Bounds,
    structure_bounds: Iterable[Bounds],
    fov_size: tuple[float, float],
) -> FocusCandidate | None:
    """Choose the nearest deterministic structure that fits in the current FOV."""

    design = _required_bounds(design_bounds, "Design bounds")
    fov_width, fov_height = _required_size(fov_size, "Field of view")
    design_center = (
        (design[0] + design[2]) * 0.5,
        (design[1] + design[3]) * 0.5,
    )
    candidates: list[tuple[float, float, Bounds, FocusCandidate]] = []
    for raw_bounds in structure_bounds:
        bounds = _optional_bounds(raw_bounds)
        if bounds is None:
            continue
        width = bounds[2] - bounds[0]
        height = bounds[3] - bounds[1]
        if width > fov_width or height > fov_height:
            continue
        center = (
            (bounds[0] + bounds[2]) * 0.5,
            (bounds[1] + bounds[3]) * 0.5,
        )
        distance = math.hypot(
            center[0] - design_center[0],
            center[1] - design_center[1],
        )
        area = width * height
        candidate = FocusCandidate(center, bounds, distance)
        candidates.append((distance, area, bounds, candidate))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[:3])[3]


def _required_bounds(value: object, label: str) -> Bounds:
    bounds = _optional_bounds(value)
    if bounds is None:
        raise ValueError(f"{label} must be a finite non-empty rectangle.")
    return bounds


def _optional_bounds(value: object) -> Bounds | None:
    try:
        left, bottom, right, top = (float(item) for item in value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    bounds = (left, bottom, right, top)
    if not all(math.isfinite(item) for item in bounds):
        return None
    if right <= left or top <= bottom:
        return None
    return bounds


def _required_size(value: object, label: str) -> tuple[float, float]:
    try:
        width, height = (abs(float(item)) for item in value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain two finite positive values.") from exc
    if not all(math.isfinite(item) and item > 0.0 for item in (width, height)):
        raise ValueError(f"{label} must contain two finite positive values.")
    return (width, height)


__all__ = ["FocusCandidate", "select_central_focus_candidate"]
