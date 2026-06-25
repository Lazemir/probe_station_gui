"""Small FluidNC serial protocol helpers shared across UI and controller code."""

from __future__ import annotations

import re
from collections.abc import Mapping

from probe_station_gui.stage_types import _Status

CONTROLLER_REBOOT_TOKENS = (
    "[MSG:RST",
    "FAST_FLASH_BOOT",
    "ESP-ROM",
)
CONTROLLER_REBOOT_LINE_PREFIXES = ("RST:", "LOAD:", "ENTRY ")
CONTROLLER_STARTUP_BANNER_TOKENS = (
    "[VER:",
    "FLUIDNC",
    "GRBL",
)
FLUIDNC_AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2, "A": 3, "B": 4, "C": 5}
STATUS_PATTERN = re.compile(r"^<(?P<body>[^>]*)>")
STATUS_FIELD_PATTERN = re.compile(r"(?P<key>[A-Za-z]+):(?P<value>.+)")
AXIS_RANGE_PATTERN = re.compile(
    r"^\[MSG:INFO: Axis (?P<axis>[A-Za-z]) \((?P<min>-?\d+\.?\d*),(?P<max>-?\d+\.?\d*)\)\]"
)


def _normalized_line(line: str) -> str:
    return str(line or "").strip().upper()


def line_indicates_controller_reboot(line: str) -> bool:
    upper = _normalized_line(line)
    return upper.startswith(CONTROLLER_REBOOT_LINE_PREFIXES) or any(
        token in upper for token in CONTROLLER_REBOOT_TOKENS
    )


def line_indicates_controller_startup(line: str) -> bool:
    upper = _normalized_line(line)
    return line_indicates_controller_reboot(upper) or any(
        token in upper for token in CONTROLLER_STARTUP_BANNER_TOKENS
    )


def parse_fluidnc_status_line(
    line: str,
    *,
    position_reporting_mode: str,
    active_work_coordinate_system: str | None = None,
    controller_coordinate_offsets: Mapping[str, tuple[float, ...]] | None = None,
    axis_index: Mapping[str, int] | None = None,
) -> _Status | None:
    """Parse a FluidNC real-time status frame for the configured position mode."""

    match = STATUS_PATTERN.search(line)
    if not match:
        return None
    body = match.group("body")
    parts = body.split("|")
    if not parts:
        return None
    state = parts[0].strip()
    if not state:
        return None

    machine_position: tuple[float, ...] | None = None
    work_position: tuple[float, ...] | None = None
    work_offset: tuple[float, ...] | None = None
    pins: set[str] = set()
    axes = axis_index or FLUIDNC_AXIS_INDEX
    for part in parts[1:]:
        field_match = STATUS_FIELD_PATTERN.match(part)
        if not field_match:
            continue
        key = field_match.group("key")
        value = field_match.group("value")
        if key == "MPos" and position_reporting_mode == "machine":
            machine_position = parse_float_tuple(value)
        elif key == "WPos" and position_reporting_mode != "machine":
            work_position = parse_float_tuple(value)
        elif key == "WCO":
            work_offset = parse_float_tuple(value)
        elif key == "Pn":
            pins = {
                pin.upper()
                for pin in value.strip()
                if pin.strip() and pin.upper() in axes
            }

    if machine_position is not None and len(machine_position) < 3:
        return None
    if work_position is not None and len(work_position) < 3:
        return None
    if work_offset is not None and len(work_offset) < 3:
        return None
    if position_reporting_mode == "machine" and machine_position is None:
        return None
    if position_reporting_mode != "machine" and work_position is None:
        return None

    coordinate_system = active_work_coordinate_system
    if position_reporting_mode != "machine":
        offsets = controller_coordinate_offsets or {}
        if (
            work_offset is None
            and coordinate_system
            and coordinate_system in offsets
        ):
            work_offset = offsets.get(coordinate_system)
    else:
        coordinate_system = None

    return _Status(
        state=state,
        position=machine_position,
        display_position=(
            machine_position
            if position_reporting_mode == "machine"
            else work_position
        ),
        work_position=work_position,
        work_offset=work_offset,
        coordinate_system=coordinate_system,
        pins=pins or None,
    )


def parse_float_tuple(raw: str) -> tuple[float, ...] | None:
    try:
        values = tuple(float(part) for part in raw.split(","))
    except ValueError:
        return None
    return values if values else None


def parse_startup_axis_limits(lines: list[str]) -> dict[str, tuple[float, float]]:
    """Parse FluidNC startup axis range messages."""

    limits: dict[str, tuple[float, float]] = {}
    for line in lines:
        match = AXIS_RANGE_PATTERN.match(line)
        if not match:
            continue
        axis = match.group("axis").upper()
        try:
            min_value = float(match.group("min"))
            max_value = float(match.group("max"))
        except ValueError:
            continue
        limits[axis] = (min_value, max_value)
    return limits


__all__ = [
    "AXIS_RANGE_PATTERN",
    "CONTROLLER_REBOOT_LINE_PREFIXES",
    "CONTROLLER_REBOOT_TOKENS",
    "CONTROLLER_STARTUP_BANNER_TOKENS",
    "FLUIDNC_AXIS_INDEX",
    "STATUS_FIELD_PATTERN",
    "STATUS_PATTERN",
    "line_indicates_controller_reboot",
    "line_indicates_controller_startup",
    "parse_float_tuple",
    "parse_fluidnc_status_line",
    "parse_startup_axis_limits",
]
