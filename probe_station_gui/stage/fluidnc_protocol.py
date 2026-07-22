"""Small FluidNC serial protocol helpers shared across UI and controller code."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from probe_station_gui.stage.types import _Status

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
FLUIDNC_AXIS_NAMES: tuple[str, ...] = ("X", "Y", "Z", "A", "B", "C")
MIN_STATUS_COORDINATES = 3
STATUS_PATTERN = re.compile(r"^<(?P<body>[^>]*)>")
STATUS_FIELD_PATTERN = re.compile(r"(?P<key>[A-Za-z]+):(?P<value>.+)")
AXIS_RANGE_PATTERN = re.compile(
    r"^\[MSG:INFO: Axis (?P<axis>[A-Za-z]) \((?P<min>-?\d+\.?\d*),(?P<max>-?\d+\.?\d*)\)\]"
)


@dataclass(frozen=True)
class _StatusFrame:
    state: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class _StatusFields:
    machine_position: tuple[float, ...] | None = None
    work_position: tuple[float, ...] | None = None
    work_offset: tuple[float, ...] | None = None
    pins: set[str] | None = None


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

    frame = _parse_status_frame(line)
    if frame is None:
        return None
    fields = _parse_status_fields(
        frame.fields,
        position_reporting_mode=position_reporting_mode,
        axis_index=axis_index or FLUIDNC_AXIS_INDEX,
    )
    if not _status_fields_are_complete(fields, position_reporting_mode):
        return None

    coordinate_system = _status_coordinate_system(
        position_reporting_mode,
        active_work_coordinate_system,
    )
    work_offset = _status_work_offset(
        fields,
        coordinate_system=coordinate_system,
        controller_coordinate_offsets=controller_coordinate_offsets,
    )

    return _Status(
        state=frame.state,
        position=fields.machine_position,
        synchronized_machine_position=_synchronized_machine_position(
            fields,
            position_reporting_mode=position_reporting_mode,
        ),
        display_position=(
            fields.machine_position
            if position_reporting_mode == "machine"
            else fields.work_position
        ),
        work_position=fields.work_position,
        work_offset=work_offset,
        coordinate_system=coordinate_system,
        pins=fields.pins,
    )


def _synchronized_machine_position(
    fields: _StatusFields,
    *,
    position_reporting_mode: str,
) -> tuple[float, ...] | None:
    if position_reporting_mode == "machine":
        return fields.machine_position
    work_position = fields.work_position
    same_frame_offset = fields.work_offset
    if work_position is None or same_frame_offset is None:
        return None
    if len(work_position) != len(same_frame_offset):
        return None
    return tuple(
        float(position + offset)
        for position, offset in zip(work_position, same_frame_offset)
    )


def _parse_status_frame(line: str) -> _StatusFrame | None:
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
    return _StatusFrame(state=state, fields=tuple(parts[1:]))


def _parse_status_fields(
    parts: tuple[str, ...],
    *,
    position_reporting_mode: str,
    axis_index: Mapping[str, int],
) -> _StatusFields:
    machine_position: tuple[float, ...] | None = None
    work_position: tuple[float, ...] | None = None
    work_offset: tuple[float, ...] | None = None
    pins: set[str] = set()
    for part in parts:
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
                if pin.strip() and pin.upper() in axis_index
            }

    return _StatusFields(
        machine_position=machine_position,
        work_position=work_position,
        work_offset=work_offset,
        pins=pins or None,
    )


def _status_fields_are_complete(
    fields: _StatusFields,
    position_reporting_mode: str,
) -> bool:
    if not _status_tuple_is_complete(fields.machine_position):
        return False
    if not _status_tuple_is_complete(fields.work_position):
        return False
    if not _status_tuple_is_complete(fields.work_offset):
        return False
    if position_reporting_mode == "machine":
        return fields.machine_position is not None
    return fields.work_position is not None


def _status_tuple_is_complete(values: tuple[float, ...] | None) -> bool:
    return values is None or len(values) >= MIN_STATUS_COORDINATES


def _status_coordinate_system(
    position_reporting_mode: str,
    active_work_coordinate_system: str | None,
) -> str | None:
    if position_reporting_mode == "machine":
        return None
    return active_work_coordinate_system


def _status_work_offset(
    fields: _StatusFields,
    *,
    coordinate_system: str | None,
    controller_coordinate_offsets: Mapping[str, tuple[float, ...]] | None,
) -> tuple[float, ...] | None:
    if fields.work_offset is not None:
        return fields.work_offset
    offsets = controller_coordinate_offsets or {}
    if coordinate_system and coordinate_system in offsets:
        return offsets.get(coordinate_system)
    return None


def parse_float_tuple(raw: str) -> tuple[float, ...] | None:
    try:
        values = tuple(float(part) for part in raw.split(","))
    except ValueError:
        return None
    return values if values else None


def parse_fluidnc_axis_max_feedrates(lines: Iterable[str]) -> dict[str, float]:
    """Extract per-axis max_rate_mm_per_min values from a FluidNC config dump."""

    axis_headers = {f"{axis}:" for axis in FLUIDNC_AXIS_NAMES}
    rates: dict[str, float] = {}
    current_axis: str | None = None
    for line in lines:
        stripped = str(line).strip()
        if not stripped or stripped.startswith("#"):
            continue
        upper = stripped.upper()
        if upper in axis_headers:
            current_axis = upper[0]
            continue
        if current_axis is None:
            continue
        if not stripped.lower().startswith("max_rate_mm_per_min:"):
            continue
        raw_value = stripped.split(":", 1)[1].strip()
        try:
            value = float(raw_value)
        except ValueError:
            continue
        if math.isfinite(value) and value > 0.0:
            rates[current_axis] = value
    return rates


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
    "FLUIDNC_AXIS_NAMES",
    "STATUS_FIELD_PATTERN",
    "STATUS_PATTERN",
    "line_indicates_controller_reboot",
    "line_indicates_controller_startup",
    "parse_float_tuple",
    "parse_fluidnc_axis_max_feedrates",
    "parse_fluidnc_status_line",
    "parse_startup_axis_limits",
]
