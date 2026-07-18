"""A/Z calibration and configured-coordinate mapping for stage control."""

from __future__ import annotations

import math
from typing import Optional

from probe_station_gui.stage.axis_calibration import StageAxisCalibrationMapper
from probe_station_gui.stage.axis_mapping import evaluate_polynomial
from probe_station_gui.settings.axis_calibration_npz import (
    LINEAR_INTERPOLATION_MODEL,
)
from probe_station_gui.stage.needle_targets import normalise_needle_lowering_target
from probe_station_gui.stage.types import _Status


def _linear_interpolation_values(calibration: object) -> dict[str, object] | None:
    try:
        gcode_points = tuple(
            float(value) for value in getattr(calibration, "interpolation_gcode_mm")
        )
        display_points = tuple(
            float(value) for value in getattr(calibration, "interpolation_display_mm")
        )
        steps_per_mm = float(getattr(calibration, "steps_per_mm"))
    except (AttributeError, OverflowError, TypeError, ValueError):
        return None
    if (
        not math.isfinite(steps_per_mm)
        or steps_per_mm <= 0
        or len(gcode_points) < 2
        or len(gcode_points) != len(display_points)
        or not all(math.isfinite(value) for value in (*gcode_points, *display_points))
        or any(right <= left for left, right in zip(gcode_points, gcode_points[1:]))
        or any(right <= left for left, right in zip(display_points, display_points[1:]))
    ):
        return None
    return {
        "model": LINEAR_INTERPOLATION_MODEL,
        "steps_per_mm": steps_per_mm,
        "min": gcode_points[0],
        "max": gcode_points[-1],
        "gcode_points": gcode_points,
        "display_points": display_points,
    }


class StageControllerAxisCoordinatesMixin:
    """Internal A/Z calibration and coordinate-mapping methods."""

    def apply_axis_a_calibration(self, calibration: object | None) -> None:
        """Apply the compact nonlinear A-axis calibration model."""

        if calibration is None or not bool(getattr(calibration, "configured", False)):
            self._axis_a_calibration = None
            return
        model = str(getattr(calibration, "model", "")).strip()
        if model == LINEAR_INTERPOLATION_MODEL:
            self._axis_a_calibration = _linear_interpolation_values(calibration)
            return
        if model != "cosine_displacement":
            self._axis_a_calibration = None
            return
        try:
            offset = float(getattr(calibration, "offset_mm"))
            if offset > 0.0:
                offset = -offset
            amplitude = float(getattr(calibration, "amplitude_mm"))
            if amplitude > 0.0:
                amplitude = -amplitude
            values: dict[str, float | str] = {
                "model": model,
                "steps_per_mm": float(getattr(calibration, "steps_per_mm")),
                "min": float(getattr(calibration, "commanded_lowering_min_mm")),
                "max": float(getattr(calibration, "commanded_lowering_max_mm")),
                "offset": offset,
                "amplitude": amplitude,
                "angular_frequency": float(
                    getattr(calibration, "angular_frequency_rad_per_mm")
                ),
                "phase": float(getattr(calibration, "phase_rad")),
            }
        except (TypeError, ValueError):
            self._axis_a_calibration = None
            return
        if (
            float(values["steps_per_mm"]) <= 0
            or float(values["max"]) <= float(values["min"])
            or abs(float(values["amplitude"])) <= 1e-12
            or float(values["angular_frequency"]) <= 0
        ):
            self._axis_a_calibration = None
            return
        self._axis_a_calibration = values

    def apply_axis_z_calibration(self, calibration: object | None) -> None:
        """Apply the compact nonlinear Z-axis calibration model."""

        if calibration is None or not bool(getattr(calibration, "configured", False)):
            self._axis_z_calibration = None
            return
        model = str(getattr(calibration, "model", "")).strip()
        if model == LINEAR_INTERPOLATION_MODEL:
            self._axis_z_calibration = _linear_interpolation_values(calibration)
            return
        if model != "quintic_polynomial":
            self._axis_z_calibration = None
            return
        try:
            coefficients = tuple(
                float(value) for value in getattr(calibration, "coefficients_mm")
            )
            values: dict[str, float | str | tuple[float, ...]] = {
                "model": model,
                "steps_per_mm": float(getattr(calibration, "steps_per_mm")),
                "min": float(getattr(calibration, "gcode_min_mm")),
                "max": float(getattr(calibration, "gcode_max_mm")),
                "coefficients": coefficients,
            }
        except (TypeError, ValueError):
            self._axis_z_calibration = None
            return
        if (
            float(values["steps_per_mm"]) <= 0
            or float(values["max"]) <= float(values["min"])
            or len(coefficients) != 6
            or not all(math.isfinite(value) for value in coefficients)
        ):
            self._axis_z_calibration = None
            return
        self._axis_z_calibration = values

    def axis_a_gcode_coordinate_for_lowering(self, lowering_mm: float) -> float:
        """Map a physical A-axis lowering in millimeters to an absolute G-code A coordinate."""

        return self._axis_a_gcode_coordinate_for_lowering(lowering_mm)

    def axis_a_lowering_for_gcode_coordinate(self, a_coordinate_mm: float) -> float:
        """Map an absolute G-code A coordinate to physical calibrated lowering."""

        return self._axis_a_lowering_for_gcode_coordinate(a_coordinate_mm)

    def axis_a_configured_coordinate_for_lowering(
        self,
        lowering_mm: float,
    ) -> float:
        """Map physical A lowering to the current GUI/controller coordinate basis."""

        return self._axis_a_configured_coordinate_for_lowering(lowering_mm)

    def axis_a_lowering_for_configured_coordinate(
        self,
        a_coordinate_mm: float,
    ) -> float:
        """Map the current GUI/controller A coordinate to physical calibrated lowering."""

        return self._axis_a_lowering_for_configured_coordinate(a_coordinate_mm)

    def _normalise_needle_lowering_target(
        self,
        position_mm: Optional[float],
    ) -> Optional[float]:
        return normalise_needle_lowering_target(
            position_mm,
            lowering_for_gcode_coordinate=self._axis_a_lowering_for_gcode_coordinate,
        )

    def calibrated_axis_display_value(
        self,
        axis: str,
        raw_value: float,
    ) -> float:
        """Map a controller coordinate to the calibrated user-facing coordinate."""

        axis = axis.upper().strip()
        if axis == "A":
            return self._axis_a_calibrated_coordinate_for_gcode_coordinate(raw_value)
        if axis == "Z":
            return self._axis_z_display_for_gcode_coordinate(raw_value)
        return float(raw_value)

    def calibrated_axis_raw_value(
        self,
        axis: str,
        display_value: float,
    ) -> float:
        """Map a calibrated user-facing coordinate to a controller coordinate."""

        axis = axis.upper().strip()
        if axis == "A":
            return self._axis_a_gcode_coordinate_for_calibrated_coordinate(display_value)
        if axis == "Z":
            return self._axis_z_gcode_coordinate_for_display(display_value)
        return float(display_value)

    def _axis_calibration_mapper(self) -> StageAxisCalibrationMapper:
        return StageAxisCalibrationMapper(
            axis_a_calibration=self._axis_a_calibration,
            axis_z_calibration=self._axis_z_calibration,
            position_reporting_mode=self._position_reporting_mode,
            active_work_coordinate_system=self._active_work_coordinate_system,
            controller_coordinate_offsets=self._controller_coordinate_offsets,
            axis_index=self.AXIS_INDEX,
        )

    def _axis_a_model_parameters(self) -> tuple[float, float, float, float] | None:
        return self._axis_calibration_mapper().axis_a_model_parameters()

    def _axis_a_model_calibrated_coordinate_for_commanded(
        self,
        commanded_lowering_mm: float,
    ) -> float:
        mapper = self._axis_calibration_mapper()
        return mapper.axis_a_model_calibrated_coordinate_for_commanded(
            commanded_lowering_mm,
        )

    def _axis_a_model_lowering_for_commanded(
        self,
        commanded_lowering_mm: float,
    ) -> float:
        return self._axis_calibration_mapper().axis_a_model_lowering_for_commanded(
            commanded_lowering_mm,
        )

    def _axis_a_calibrated_coordinate_for_gcode_coordinate(
        self, a_coordinate_mm: float
    ) -> float:
        mapper = self._axis_calibration_mapper()
        return mapper.axis_a_calibrated_coordinate_for_gcode_coordinate(
            a_coordinate_mm,
        )

    def _axis_a_lowering_for_gcode_coordinate(self, a_coordinate_mm: float) -> float:
        return self._axis_calibration_mapper().axis_a_lowering_for_gcode_coordinate(
            a_coordinate_mm,
        )

    def _axis_work_offset_for_configured_mode(
        self,
        axis: str,
        status: _Status | None = None,
    ) -> float:
        return self._axis_calibration_mapper().axis_work_offset_for_configured_mode(
            axis,
            status,
        )

    def _axis_a_lowering_for_configured_coordinate(
        self,
        a_coordinate_mm: float,
        status: _Status | None = None,
    ) -> float:
        mapper = self._axis_calibration_mapper()
        return mapper.axis_a_lowering_for_configured_coordinate(
            a_coordinate_mm,
            status,
        )

    def _axis_a_configured_coordinate_for_lowering(
        self,
        lowering_mm: float,
        status: _Status | None = None,
    ) -> float:
        return self._axis_calibration_mapper().axis_a_configured_coordinate_for_lowering(
            lowering_mm,
            status,
        )

    def _axis_a_gcode_coordinate_for_calibrated_coordinate(
        self,
        calibrated_coordinate_mm: float,
    ) -> float:
        mapper = self._axis_calibration_mapper()
        return mapper.axis_a_gcode_coordinate_for_calibrated_coordinate(
            calibrated_coordinate_mm,
        )

    def _axis_a_gcode_coordinate_for_lowering(self, lowering_mm: float) -> float:
        return self._axis_calibration_mapper().axis_a_gcode_coordinate_for_lowering(
            lowering_mm,
        )

    def _axis_a_commanded_lowering_for_calibrated_coordinate(
        self,
        calibrated_coordinate_mm: float,
    ) -> float:
        mapper = self._axis_calibration_mapper()
        return mapper.axis_a_commanded_lowering_for_calibrated_coordinate(
            calibrated_coordinate_mm,
        )

    def _axis_a_gcode_coordinate_for_lowering_step(
        self,
        current_a: float,
        requested_step_mm: float,
    ) -> float:
        return self._axis_calibration_mapper().axis_a_gcode_coordinate_for_lowering_step(
            current_a,
            requested_step_mm,
        )

    @staticmethod
    def _evaluate_polynomial(coefficients: tuple[float, ...], x_value: float) -> float:
        return evaluate_polynomial(coefficients, x_value)

    def _axis_z_coefficients(self) -> tuple[float, ...] | None:
        return self._axis_calibration_mapper().axis_z_coefficients()

    def _axis_z_display_for_gcode_coordinate(self, z_coordinate_mm: float) -> float:
        return self._axis_calibration_mapper().axis_z_display_for_gcode_coordinate(
            z_coordinate_mm,
        )

    def _axis_z_gcode_coordinate_for_display(self, display_mm: float) -> float:
        return self._axis_calibration_mapper().axis_z_gcode_coordinate_for_display(
            display_mm,
        )

    def _position_for_configured_mode(self, status: _Status | None) -> tuple[float, ...] | None:
        return self._axis_calibration_mapper().position_for_configured_mode(status)

    def _axis_value_for_configured_mode(
        self, status: _Status | None, axis: str
    ) -> float | None:
        return self._axis_calibration_mapper().axis_value_for_configured_mode(
            status,
            axis,
        )

    def _axis_limits_for_configured_mode(
        self, axis: str, status: _Status | None
    ) -> tuple[float, float] | None:
        limits = self._axis_limits.get(axis)
        return self._axis_calibration_mapper().axis_limits_for_configured_mode(
            axis,
            limits,
            status,
        )
