"""Helpers for parsing and building FluidNC jog commands."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence

from probe_station_gui.stage_types import MoveVector


JOG_AXIS_WORD_PATTERN = re.compile(
    r"(?<![A-Za-z])(?P<axis>[XYZABC])(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+))",
    re.IGNORECASE,
)
JOG_FEEDRATE_WORD_PATTERN = re.compile(
    r"(?<![A-Za-z])F(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+))",
    re.IGNORECASE,
)


def format_gcode_value(value: float, decimals: int = 3) -> str:
    text = f"{float(value):.{int(decimals)}f}"
    text = text.rstrip("0").rstrip(".")
    return text or "0"


def absolute_axis_targets_jog_command(
    targets: Mapping[str, float],
    feedrate: float,
    *,
    axis_order: Iterable[str],
    machine_position_mode: bool,
) -> str:
    ordered_targets = {
        axis: float(targets[axis])
        for axis in axis_order
        if axis in targets
    }
    if not ordered_targets:
        return ""
    move_parts = [
        f"{axis}{value:.4f}"
        for axis, value in ordered_targets.items()
    ]
    command_parts = ["$J=G90", "G21"]
    if machine_position_mode:
        command_parts.append("G53")
    command_parts.extend(move_parts)
    command_parts.append(f"F{format_gcode_value(feedrate)}")
    return " ".join(command_parts)


def move_vector_from_jog_command(
    command: str,
    *,
    axis_index: Mapping[str, int],
) -> MoveVector | None:
    stripped = command.strip()
    if not stripped.upper().startswith("$J="):
        return None
    distances: list[tuple[str, float]] = []
    for match in JOG_AXIS_WORD_PATTERN.finditer(stripped):
        axis = match.group("axis").upper()
        try:
            distance = float(match.group("value"))
        except (TypeError, ValueError):
            continue
        distances.append((axis, distance))
    if not distances:
        return None
    return move_vector_from_axis_distances(distances, axis_index=axis_index)


def jog_command_feedrate(command: str, *, min_feedrate: float) -> float | None:
    feedrate: float | None = None
    for match in JOG_FEEDRATE_WORD_PATTERN.finditer(command.strip()):
        try:
            value = float(match.group("value"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            feedrate = max(float(min_feedrate), value)
    return feedrate


def relative_jog_command_to_absolute(
    command: str,
    move: MoveVector,
    *,
    position: Sequence[float] | None,
    axis_index: Mapping[str, int],
    min_feedrate: float,
    machine_position_mode: bool,
    axis_skip_reason: Callable[[str, Sequence[float]], object],
) -> str:
    normalized = command.strip().upper().replace("$J=", " ")
    tokens = set(normalized.split())
    if "G91" not in tokens or "G90" in tokens:
        return command
    if position is None:
        return command
    targets: dict[str, float] = {}
    for axis, delta in move.items():
        if abs(delta) < 1e-6:
            continue
        index = axis_index.get(axis)
        if index is None or index >= len(position):
            return command
        if axis_skip_reason(axis, position):
            return command
        targets[axis] = float(position[index]) + float(delta)
    if not targets:
        return command
    feedrate = jog_command_feedrate(command, min_feedrate=min_feedrate)
    if feedrate is None:
        return command
    return absolute_axis_targets_jog_command(
        targets,
        feedrate,
        axis_order=axis_index,
        machine_position_mode=machine_position_mode,
    )


def move_vector_from_axis_distances(
    distances: Iterable[tuple[str, float]],
    *,
    axis_index: Mapping[str, int],
) -> MoveVector:
    values = {axis: 0.0 for axis in axis_index}
    for axis, distance in distances:
        normalized_axis = str(axis).strip().upper()
        if normalized_axis not in values:
            continue
        values[normalized_axis] += float(distance)
    return MoveVector(
        x=values.get("X", 0.0),
        y=values.get("Y", 0.0),
        z=values.get("Z", 0.0),
        a=values.get("A", 0.0),
        b=values.get("B", 0.0),
        c=values.get("C", 0.0),
    )
