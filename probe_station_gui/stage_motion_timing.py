"""Distance and timeout helpers for stage motion waits."""

from __future__ import annotations

import math
from collections.abc import Mapping

from probe_station_gui.stage_types import MoveVector


def absolute_move_distance_for_timeout(
    targets: Mapping[str, float],
    current_values: Mapping[str, float],
) -> float:
    if not current_values:
        return math.sqrt(sum(value * value for value in targets.values()))
    squared = 0.0
    for axis, value in targets.items():
        current = current_values.get(axis)
        delta = float(value) if current is None else float(value) - float(current)
        squared += delta * delta
    return math.sqrt(squared)


def move_distance_for_timeout(move: MoveVector) -> float:
    return math.sqrt(sum(value * value for _axis, value in move.items()))


def idle_timeout_for_distance(
    distance: float,
    feedrate: float,
    *,
    min_feedrate: float,
    margin_s: float,
    min_timeout_s: float,
    max_timeout_s: float,
) -> float:
    try:
        distance_value = abs(float(distance))
        feedrate_value = max(float(min_feedrate), float(feedrate))
    except (TypeError, ValueError):
        return float(min_timeout_s)
    travel_time_s = (distance_value / feedrate_value) * 60.0
    timeout = travel_time_s + float(margin_s)
    return min(
        float(max_timeout_s),
        max(float(min_timeout_s), timeout),
    )
