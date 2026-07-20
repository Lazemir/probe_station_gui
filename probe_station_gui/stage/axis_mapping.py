"""Pure piecewise-linear mapping for measured axis coordinates."""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass

from probe_station_gui.settings.axis_calibration_config import (
    AxisCalibrationSettings,
    is_valid_calibration_curve,
)


class CalibrationOutOfDomain(ValueError):
    """A coordinate cannot be represented by a measured calibration curve."""


@dataclass(frozen=True)
class AxisCalibrationCurve:
    """One strictly increasing controller-to-physical curve."""

    controller: tuple[float, ...]
    physical: tuple[float, ...]


def curve_from_settings(
    settings: AxisCalibrationSettings | None,
) -> AxisCalibrationCurve | None:
    """Create a runtime curve only for an enabled, valid snapshot."""

    if settings is None or not settings.enabled:
        return None
    if not is_valid_calibration_curve(
        settings.controller_points,
        settings.physical_points,
    ):
        return None
    return AxisCalibrationCurve(
        controller=tuple(settings.controller_points),
        physical=tuple(settings.physical_points),
    )


def controller_to_physical(curve: AxisCalibrationCurve, value: float) -> float:
    """Map a controller machine coordinate inside the measured domain."""

    return _interpolate(curve.controller, curve.physical, value, coordinate="controller")


def physical_to_controller(curve: AxisCalibrationCurve, value: float) -> float:
    """Map a physical machine coordinate inside the measured domain."""

    return _interpolate(curve.physical, curve.controller, value, coordinate="physical")


def _interpolate(
    x_points: tuple[float, ...],
    y_points: tuple[float, ...],
    raw_value: float,
    *,
    coordinate: str,
) -> float:
    value = float(raw_value)
    if not math.isfinite(value) or value < x_points[0] or value > x_points[-1]:
        raise CalibrationOutOfDomain(
            f"{coordinate.capitalize()} coordinate is outside the calibration range."
        )
    if value == x_points[0]:
        return y_points[0]
    if value == x_points[-1]:
        return y_points[-1]
    upper = bisect_right(x_points, value)
    lower = upper - 1
    fraction = (value - x_points[lower]) / (x_points[upper] - x_points[lower])
    return y_points[lower] + fraction * (y_points[upper] - y_points[lower])

