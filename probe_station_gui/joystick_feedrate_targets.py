"""Pure feedrate target helpers for the joystick controls."""

from __future__ import annotations

import math
from collections.abc import Mapping


def feedrate_key(
    target: str,
    mode: str,
    *,
    target_labels: Mapping[str, str],
    default_target: str,
    valid_modes: set[str],
    default_mode: str,
) -> str:
    target_key = str(target).strip().lower()
    if target_key not in target_labels:
        target_key = default_target
    mode_key = str(mode).strip().lower()
    if mode_key not in valid_modes:
        mode_key = default_mode
    return f"{mode_key}:{target_key}"


def feedrate_target_for_axis(
    axis: str,
    *,
    xy_target: str,
    focus_target: str,
    needles_target: str,
    turntable_target: str,
) -> str:
    axis_name = str(axis).upper().strip()
    if axis_name == "Z":
        return focus_target
    if axis_name == "A":
        return needles_target
    if axis_name == "B":
        return turntable_target
    return xy_target


def clean_axis_feedrate_limits(
    limits: Mapping[object, object],
    *,
    valid_axes: tuple[str, ...],
) -> dict[str, float]:
    cleaned: dict[str, float] = {}
    for axis, value in limits.items():
        axis_name = str(axis).strip().upper()
        if axis_name not in valid_axes:
            continue
        try:
            feedrate = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(feedrate) and feedrate > 0.0:
            cleaned[axis_name] = feedrate
    return cleaned
