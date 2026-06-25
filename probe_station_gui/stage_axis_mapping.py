"""Pure coordinate mapping helpers for calibrated stage axes."""

from __future__ import annotations

import math
from collections.abc import Mapping


AxisCalibration = Mapping[str, object] | None


def axis_a_model_parameters(
    calibration: AxisCalibration,
) -> tuple[float, float, float, float] | None:
    if calibration is None:
        return None
    return (
        float(calibration["offset"]),
        float(calibration["amplitude"]),
        float(calibration["angular_frequency"]),
        float(calibration["phase"]),
    )


def axis_a_model_calibrated_coordinate_for_commanded(
    calibration: AxisCalibration,
    commanded_lowering_mm: float,
) -> float:
    parameters = axis_a_model_parameters(calibration)
    if parameters is None:
        return -float(commanded_lowering_mm)
    x_value = float(commanded_lowering_mm)
    offset, amplitude, angular_frequency, phase = parameters
    return offset + amplitude * (
        math.cos(phase) - math.cos(phase + angular_frequency * x_value)
    )


def axis_a_model_lowering_for_commanded(
    calibration: AxisCalibration,
    commanded_lowering_mm: float,
) -> float:
    return -axis_a_model_calibrated_coordinate_for_commanded(
        calibration,
        commanded_lowering_mm,
    )


def axis_a_calibrated_coordinate_for_gcode_coordinate(
    calibration: AxisCalibration,
    a_coordinate_mm: float,
) -> float:
    commanded_lowering = -float(a_coordinate_mm)
    if calibration is None:
        return float(a_coordinate_mm)
    origin = axis_a_model_calibrated_coordinate_for_commanded(
        calibration,
        float(calibration["min"]),
    )
    calibrated_value = (
        axis_a_model_calibrated_coordinate_for_commanded(
            calibration,
            commanded_lowering,
        )
        - origin
    )
    return 0.0 if abs(calibrated_value) <= 1e-12 else calibrated_value


def axis_a_lowering_for_gcode_coordinate(
    calibration: AxisCalibration,
    a_coordinate_mm: float,
) -> float:
    return -axis_a_calibrated_coordinate_for_gcode_coordinate(
        calibration,
        a_coordinate_mm,
    )


def axis_a_gcode_coordinate_for_calibrated_coordinate(
    calibration: AxisCalibration,
    calibrated_coordinate_mm: float,
) -> float:
    commanded_lowering = axis_a_commanded_lowering_for_calibrated_coordinate(
        calibration,
        calibrated_coordinate_mm,
    )
    return -commanded_lowering


def axis_a_gcode_coordinate_for_lowering(
    calibration: AxisCalibration,
    lowering_mm: float,
) -> float:
    return axis_a_gcode_coordinate_for_calibrated_coordinate(
        calibration,
        -float(lowering_mm),
    )


def axis_a_commanded_lowering_for_calibrated_coordinate(
    calibration: AxisCalibration,
    calibrated_coordinate_mm: float,
) -> float:
    if calibration is None:
        return -float(calibrated_coordinate_mm)
    low = float(calibration["min"])
    high = float(calibration["max"])
    origin_value = axis_a_model_calibrated_coordinate_for_commanded(calibration, low)
    target = origin_value + float(calibrated_coordinate_mm)
    low_value = axis_a_model_calibrated_coordinate_for_commanded(calibration, low)
    high_value = axis_a_model_calibrated_coordinate_for_commanded(calibration, high)
    if high_value < low_value:
        low, high = high, low
        low_value, high_value = high_value, low_value
    if target <= low_value:
        return low
    if target >= high_value:
        return high
    for _ in range(64):
        mid = (low + high) * 0.5
        value = axis_a_model_calibrated_coordinate_for_commanded(calibration, mid)
        if value < target:
            low = mid
        else:
            high = mid
    return (low + high) * 0.5


def evaluate_polynomial(coefficients: tuple[float, ...], x_value: float) -> float:
    result = 0.0
    for coefficient in coefficients:
        result = result * float(x_value) + float(coefficient)
    return result


def axis_z_coefficients(calibration: AxisCalibration) -> tuple[float, ...] | None:
    if calibration is None:
        return None
    coefficients = calibration["coefficients"]
    if not isinstance(coefficients, tuple):
        return None
    return coefficients


def axis_z_display_for_gcode_coordinate(
    calibration: AxisCalibration,
    z_coordinate_mm: float,
) -> float:
    coefficients = axis_z_coefficients(calibration)
    if coefficients is None:
        return float(z_coordinate_mm)
    return evaluate_polynomial(coefficients, float(z_coordinate_mm))


def axis_z_gcode_coordinate_for_display(
    calibration: AxisCalibration,
    display_mm: float,
) -> float:
    coefficients = axis_z_coefficients(calibration)
    if calibration is None or coefficients is None:
        return float(display_mm)
    low = float(calibration["min"])
    high = float(calibration["max"])
    low_value = evaluate_polynomial(coefficients, low)
    high_value = evaluate_polynomial(coefficients, high)
    target = float(display_mm)
    if high_value < low_value:
        low, high = high, low
        low_value, high_value = high_value, low_value
    if target <= low_value:
        return low
    if target >= high_value:
        return high
    for _ in range(64):
        mid = (low + high) * 0.5
        value = evaluate_polynomial(coefficients, mid)
        if value < target:
            low = mid
        else:
            high = mid
    return (low + high) * 0.5
