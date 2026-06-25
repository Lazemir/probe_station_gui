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


def _positive_axis_limit(axis_limits: Mapping[str, float], axis: str) -> float | None:
    value = axis_limits.get(axis)
    if value is None:
        return None
    try:
        feedrate = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(feedrate) or feedrate <= 0:
        return None
    return feedrate


def _axis_limited_target_max(
    target: str,
    *,
    axis_limits: Mapping[str, float],
    xy_target: str,
    focus_target: str,
    needles_target: str,
    turntable_target: str,
) -> float | None:
    target_axes = {
        xy_target: ("X", "Y"),
        focus_target: ("Z",),
        needles_target: ("A",),
        turntable_target: ("B",),
    }
    axes = target_axes.get(target)
    if axes is None:
        return None
    limits = [
        limit
        for axis in axes
        if (limit := _positive_axis_limit(axis_limits, axis)) is not None
    ]
    if not limits:
        return None
    return max(limits)


def _common_target_max(
    target: str,
    *,
    common_feedrate_max: float | None,
    min_linear_feedrate: float,
    common_target: str,
) -> float | None:
    if target != common_target or common_feedrate_max is None:
        return None
    try:
        value = float(common_feedrate_max)
    except (TypeError, ValueError):
        return min_linear_feedrate
    if math.isfinite(value) and value > 0:
        return value
    return None


def _fallback_feedrate_max(
    *,
    linear_feedrate_value: float,
    linear_default: float,
    linear_presets: list[float],
    min_linear_feedrate: float,
    max_linear_feedrate: float,
) -> float:
    fallback_values = [
        min_linear_feedrate,
        float(linear_feedrate_value),
        float(linear_default),
        *(float(value) for value in linear_presets),
    ]
    return min(
        max_linear_feedrate,
        max(value for value in fallback_values if math.isfinite(value)),
    )


def feedrate_max_for_target(
    target: str,
    *,
    axis_limits: Mapping[str, float],
    common_feedrate_max: float | None,
    linear_feedrate_value: float,
    linear_default: float,
    linear_presets: list[float],
    min_linear_feedrate: float,
    max_linear_feedrate: float,
    xy_target: str,
    focus_target: str,
    needles_target: str,
    turntable_target: str,
    common_target: str,
) -> float:
    target_max = _axis_limited_target_max(
        target,
        axis_limits=axis_limits,
        xy_target=xy_target,
        focus_target=focus_target,
        needles_target=needles_target,
        turntable_target=turntable_target,
    )
    if target_max is not None:
        return target_max

    target_max = _common_target_max(
        target,
        common_feedrate_max=common_feedrate_max,
        min_linear_feedrate=min_linear_feedrate,
        common_target=common_target,
    )
    if target_max is not None:
        return target_max

    return _fallback_feedrate_max(
        linear_feedrate_value=linear_feedrate_value,
        linear_default=linear_default,
        linear_presets=linear_presets,
        min_linear_feedrate=min_linear_feedrate,
        max_linear_feedrate=max_linear_feedrate,
    )


def feedrate_limit_known_for_target(
    target: str,
    *,
    axis_limits: Mapping[str, float],
    common_feedrate_max: float | None,
    xy_target: str,
    focus_target: str,
    needles_target: str,
    turntable_target: str,
    common_target: str,
) -> bool:
    if target == xy_target:
        return any(axis in axis_limits for axis in ("X", "Y"))
    if target == focus_target:
        return "Z" in axis_limits
    if target == needles_target:
        return "A" in axis_limits
    if target == turntable_target:
        return "B" in axis_limits
    if target == common_target:
        return common_feedrate_max is not None
    return False


def bounded_feedrate_setting(
    target: str,
    value: float,
    *,
    limit_known: bool,
    target_max: float,
    min_linear_feedrate: float,
) -> float:
    bounded = max(min_linear_feedrate, float(value))
    if limit_known:
        bounded = min(target_max, bounded)
    return bounded


def linear_feedrate_min_max(
    *,
    target_max: float,
    bounds: tuple[float, float] | None,
    min_linear_feedrate: float,
) -> tuple[float, float]:
    target_max = max(min_linear_feedrate, float(target_max))
    if bounds is None:
        return (min_linear_feedrate, target_max)
    min_value, max_value = bounds
    minimum = max(min_linear_feedrate, float(min_value))
    maximum = min(target_max, max(min_linear_feedrate, float(max_value)))
    return (minimum, max(minimum, maximum))


def slider_value_from_feedrate(
    value: float,
    *,
    min_value: float,
    max_value: float,
    scale: int,
) -> int:
    bounded = min(max_value, max(min_value, float(value)))
    return int(round(bounded * scale))


def feedrate_from_slider_value(slider_value: int, *, scale: int) -> float:
    return float(slider_value) / float(scale)
