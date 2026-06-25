"""Parsing helpers for persisted StageController state."""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping


def parse_cached_controller_session_marker(data: Mapping[str, object]) -> int | None:
    raw_marker = data.get("controller_session_marker")
    try:
        marker = int(raw_marker)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return marker if marker > 0 else None


def parse_cached_axis_limits(
    raw_limits: object,
    *,
    axis_names: Collection[str],
) -> dict[str, tuple[float, float]]:
    limits: dict[str, tuple[float, float]] = {}
    if not isinstance(raw_limits, dict):
        return limits
    normalized_axes = {str(axis).strip().upper() for axis in axis_names}
    for raw_axis, raw_values in raw_limits.items():
        axis = str(raw_axis).strip().upper()
        if axis not in normalized_axes or axis == "B":
            continue
        if not isinstance(raw_values, (list, tuple)) or len(raw_values) < 2:
            continue
        try:
            min_value = float(raw_values[0])
            max_value = float(raw_values[1])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(min_value) or not math.isfinite(max_value):
            continue
        if max_value < min_value:
            continue
        limits[axis] = (min_value, max_value)
    return limits


def parse_cached_axis_max_feedrates(
    raw_feedrates: object,
    *,
    axis_names: Collection[str],
    min_feedrate: float,
) -> dict[str, float]:
    feedrates: dict[str, float] = {}
    if not isinstance(raw_feedrates, dict):
        return feedrates
    normalized_axes = {str(axis).strip().upper() for axis in axis_names}
    for raw_axis, raw_rate in raw_feedrates.items():
        axis = str(raw_axis).strip().upper()
        if axis not in normalized_axes:
            continue
        try:
            rate = float(raw_rate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(rate) and rate > 0.0:
            feedrates[axis] = max(float(min_feedrate), rate)
    return feedrates


def parse_cached_coordinate_offsets(
    raw_offsets: object,
    *,
    coordinate_systems: Collection[str],
) -> dict[str, tuple[float, ...]]:
    offsets: dict[str, tuple[float, ...]] = {}
    if not isinstance(raw_offsets, dict):
        return offsets
    normalized_systems = {str(system).strip().upper() for system in coordinate_systems}
    for raw_system, raw_values in raw_offsets.items():
        system = str(raw_system).strip().upper()
        if system not in normalized_systems:
            continue
        if not isinstance(raw_values, (list, tuple)):
            continue
        try:
            values = tuple(float(value) for value in raw_values)
        except (TypeError, ValueError):
            continue
        if len(values) < 3 or not all(math.isfinite(value) for value in values):
            continue
        offsets[system] = values
    return offsets
