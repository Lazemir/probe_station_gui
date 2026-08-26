"""Shared helpers for time-based motion position estimates."""

from __future__ import annotations

import math
from typing import Sequence


_MIN_PLANNED_DISTANCE_MM = 1e-6
_MIN_PLANNED_SPEED_MM_S = 1e-6
_MIN_PREDICTION_DURATION_S = 0.05
_SECONDS_PER_MINUTE = 60.0


def coerce_finite_xy(value: object) -> tuple[float, float] | None:
    try:
        xy = (float(value[0]), float(value[1]))  # type: ignore[index]
    except (IndexError, TypeError, ValueError, OverflowError):
        return None
    return xy if all(math.isfinite(axis_value) for axis_value in xy) else None


def planned_xy_matches(
    candidate: object,
    expected: tuple[float, float],
    *,
    tolerance_mm: float,
) -> bool:
    target = coerce_finite_xy(candidate)
    if target is None:
        return False
    return math.hypot(target[0] - expected[0], target[1] - expected[1]) <= float(
        tolerance_mm
    )


def matching_planned_xy_start(
    target_x_mm: object,
    target_y_mm: object,
    feedrate_mm_min: object,
    *,
    pending_target: tuple[float, float],
    tolerance_mm: float,
) -> tuple[tuple[float, float], float] | None:
    target = coerce_finite_xy((target_x_mm, target_y_mm))
    try:
        feedrate = float(feedrate_mm_min)
    except (TypeError, ValueError, OverflowError):
        return None
    if target is None or not math.isfinite(feedrate):
        return None
    if not planned_xy_matches(
        target,
        pending_target,
        tolerance_mm=tolerance_mm,
    ):
        return None
    return target, feedrate


def planned_xy_timing(
    origin: tuple[float, float],
    target: tuple[float, float],
    feedrate_mm_min: float,
    *,
    min_feedrate_mm_min: float,
    duration_padding_s: float,
    started_at: float,
) -> tuple[float, float] | None:
    distance_mm = math.hypot(target[0] - origin[0], target[1] - origin[1])
    if distance_mm <= _MIN_PLANNED_DISTANCE_MM:
        return None
    feedrate = max(float(min_feedrate_mm_min), float(feedrate_mm_min))
    speed_mm_per_s = feedrate / _SECONDS_PER_MINUTE
    if not math.isfinite(speed_mm_per_s) or speed_mm_per_s <= _MIN_PLANNED_SPEED_MM_S:
        return None
    duration_s = distance_mm / speed_mm_per_s + float(duration_padding_s)
    start = float(started_at)
    return start, start + max(duration_s, _MIN_PREDICTION_DURATION_S)


def motion_progress(started_at: float, ends_at: float, now: float) -> float:
    """Return clamped linear progress for a planned motion segment."""

    duration = max(float(ends_at) - float(started_at), 1e-6)
    return min(1.0, max(0.0, (float(now) - float(started_at)) / duration))


def held_planned_xy(
    planned_xy: tuple[float, float] | None,
    *,
    prediction_active: bool,
    waiting_for_fresh_status: bool,
    status_timestamp: float | None,
    stop_status_timestamp: float | None,
) -> tuple[float, float] | None:
    """Keep a planned coordinate until motion ends and a newer report arrives."""

    if planned_xy is None:
        return None
    if prediction_active:
        return planned_xy
    if not waiting_for_fresh_status:
        return None
    fresh = bool(
        status_timestamp is not None
        and stop_status_timestamp is not None
        and status_timestamp > stop_status_timestamp
    )
    return None if fresh else planned_xy


def interpolate_position(
    origin: Sequence[float],
    target: Sequence[float],
    started_at: float,
    ends_at: float,
    now: float,
) -> tuple[float, ...]:
    """Interpolate an N-axis position using the app's linear motion model."""

    progress = motion_progress(started_at, ends_at, now)
    count = min(len(origin), len(target))
    values = [
        float(origin[index] + (target[index] - origin[index]) * progress)
        for index in range(count)
    ]
    if len(target) > count:
        values.extend(float(value) for value in target[count:])
    return tuple(values)
