"""Universal axis-calibration settings snapshots."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from probe_station_gui.settings.value_parsing import coerce_bool


CALIBRATION_AXES = ("X", "Y", "Z", "A", "B", "C")
_ROTARY_AXES = frozenset({"B", "C"})


@dataclass
class AxisCalibrationSettings:
    """Saved immutable-by-convention snapshot of one measured axis curve."""

    enabled: bool = False
    calibration_file: str = ""
    controller_points: list[float] = field(default_factory=list)
    physical_points: list[float] = field(default_factory=list)

    def clone(self) -> "AxisCalibrationSettings":
        return AxisCalibrationSettings(
            enabled=self.enabled,
            calibration_file=self.calibration_file,
            controller_points=list(self.controller_points),
            physical_points=list(self.physical_points),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "calibration_file": self.calibration_file,
            "controller_points": list(self.controller_points),
            "physical_points": list(self.physical_points),
        }


def axis_unit(axis: str) -> str:
    """Return the physical coordinate unit for *axis*."""

    normalized = str(axis).upper()
    if normalized not in CALIBRATION_AXES:
        raise ValueError(f"Unsupported calibration axis: {axis!r}")
    return "deg" if normalized in _ROTARY_AXES else "mm"


def default_axis_calibrations() -> dict[str, AxisCalibrationSettings]:
    """Create independent empty settings for every supported axis."""

    return {axis: AxisCalibrationSettings() for axis in CALIBRATION_AXES}


def parse_axis_calibrations(raw: object) -> dict[str, AxisCalibrationSettings]:
    """Normalize a persisted six-axis calibration mapping."""

    source = raw if isinstance(raw, Mapping) else {}
    normalized_source = {
        str(axis).upper(): value for axis, value in source.items() if isinstance(axis, str)
    }
    parsed: dict[str, AxisCalibrationSettings] = {}
    for axis in CALIBRATION_AXES:
        parsed[axis] = _parse_axis_calibration(normalized_source.get(axis))
    return parsed


def is_valid_calibration_curve(
    controller_points: object,
    physical_points: object,
) -> bool:
    """Return whether two persisted sequences satisfy the runtime contract."""

    controller = _finite_float_list(controller_points)
    physical = _finite_float_list(physical_points)
    return bool(
        len(controller) >= 2
        and len(controller) == len(physical)
        and all(right > left for left, right in zip(controller, controller[1:]))
        and all(right > left for left, right in zip(physical, physical[1:]))
    )


def _parse_axis_calibration(raw: object) -> AxisCalibrationSettings:
    if not isinstance(raw, Mapping):
        return AxisCalibrationSettings()

    controller = _finite_float_list(raw.get("controller_points"))
    physical = _finite_float_list(raw.get("physical_points"))
    if not is_valid_calibration_curve(controller, physical):
        return AxisCalibrationSettings()

    file_value = raw.get("calibration_file", "")
    calibration_file = file_value.strip() if isinstance(file_value, str) else ""
    return AxisCalibrationSettings(
        enabled=coerce_bool(raw.get("enabled", False), default=False),
        calibration_file=calibration_file,
        controller_points=controller,
        physical_points=physical,
    )


def _finite_float_list(raw: object) -> list[float]:
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes, Mapping)):
        return []
    values: list[float] = []
    for item in raw:
        try:
            value = float(item)
        except (TypeError, ValueError):
            return []
        if not math.isfinite(value):
            return []
        values.append(value)
    return values

