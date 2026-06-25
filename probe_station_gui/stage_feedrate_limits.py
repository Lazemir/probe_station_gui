"""Feedrate limit helpers for stage axis moves."""

from __future__ import annotations

import math
from collections.abc import Mapping


def clean_axis_max_feedrates(
    rates: Mapping[object, object] | None,
    *,
    axis_index: Mapping[str, int],
    min_feedrate: float,
) -> dict[str, float]:
    cleaned: dict[str, float] = {}
    for raw_axis, raw_rate in (rates or {}).items():
        axis = str(raw_axis).upper().strip()
        if axis not in axis_index:
            continue
        try:
            rate = float(raw_rate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(rate) and rate > 0.0:
            cleaned[axis] = max(float(min_feedrate), rate)
    return cleaned


def axis_max_feedrate(
    axis: str,
    feedrates: Mapping[str, float],
    *,
    default_feedrate: float,
    min_feedrate: float,
) -> float:
    axis_key = str(axis).upper().strip()
    value = feedrates.get(axis_key, default_feedrate)
    if not math.isfinite(value) or value <= 0.0:
        return default_feedrate
    return max(min_feedrate, float(value))


def max_feedrate_for_axes(
    axes: object,
    feedrates: Mapping[str, float],
    *,
    axis_index: Mapping[str, int],
    default_feedrate: float,
    min_feedrate: float,
) -> float:
    if isinstance(axes, str):
        raw_axes = [axes]
    else:
        try:
            raw_axes = list(axes)  # type: ignore[arg-type]
        except TypeError:
            raw_axes = []
    normalized = [
        str(axis).upper().strip()
        for axis in raw_axes
        if str(axis).upper().strip() in axis_index
    ]
    if not normalized:
        return default_feedrate
    return max(
        min_feedrate,
        min(
            axis_max_feedrate(
                axis,
                feedrates,
                default_feedrate=default_feedrate,
                min_feedrate=min_feedrate,
            )
            for axis in normalized
        ),
    )
