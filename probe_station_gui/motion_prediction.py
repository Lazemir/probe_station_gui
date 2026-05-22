"""Shared helpers for time-based motion position estimates."""

from __future__ import annotations

from typing import Sequence


def motion_progress(started_at: float, ends_at: float, now: float) -> float:
    """Return clamped linear progress for a planned motion segment."""

    duration = max(float(ends_at) - float(started_at), 1e-6)
    return min(1.0, max(0.0, (float(now) - float(started_at)) / duration))


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
