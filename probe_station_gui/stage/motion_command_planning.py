"""Pure planning helpers for stage motion command construction."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

from probe_station_gui.stage.errors import StageControllerError
from probe_station_gui.stage.jog_commands import format_gcode_value


def ordered_absolute_axis_targets(
    targets: Mapping[str, float],
    *,
    axis_order: Iterable[str],
) -> dict[str, float]:
    """Return finite targets in controller axis order."""

    ordered_targets: dict[str, float] = {}
    ordered_axes = tuple(axis_order)
    for axis in ordered_axes:
        if axis not in targets:
            continue
        value = float(targets[axis])
        if not math.isfinite(value):
            raise StageControllerError(f"Unsupported target for {axis}: {value}")
        ordered_targets[axis] = value

    supported_axes = {str(axis).upper().strip() for axis in ordered_axes}
    unsupported = [
        str(axis)
        for axis in targets
        if str(axis).upper().strip() not in supported_axes
    ]
    if unsupported:
        raise StageControllerError(f"Unsupported axis: {', '.join(unsupported)}")
    return ordered_targets


def clamped_motion_feedrate(
    feedrate: float | None,
    *,
    default_feedrate: float,
    min_feedrate: float,
) -> float:
    """Return the controller feedrate after applying default/minimum rules."""

    if feedrate is None:
        return float(default_feedrate)
    return max(float(min_feedrate), float(feedrate))


def absolute_axis_g1_command(targets: Mapping[str, float], feedrate: float) -> str:
    """Build the G1 command for an ordered absolute target mapping."""

    move_parts = (
        f"{axis}{float(value):.4f}"
        for axis, value in targets.items()
    )
    return "G1 " + " ".join(move_parts) + f" F{format_gcode_value(feedrate)}"


def absolute_axis_target_limit_error(
    axis: str,
    target: float,
    limits: tuple[float, float],
) -> str | None:
    """Return the current controller error text when a target exceeds limits."""

    min_value, max_value = limits
    if min_value <= target <= max_value:
        return None
    return (
        f"{axis} target {target:+.3f} exceeds limits "
        f"({min_value:.3f}, {max_value:.3f})."
    )
