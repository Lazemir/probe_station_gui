"""Coordinate conversion helpers for calibrated stage axes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from probe_station_gui.stage_axis_mapping import (
    axis_a_calibrated_coordinate_for_gcode_coordinate,
    axis_a_commanded_lowering_for_calibrated_coordinate,
    axis_a_gcode_coordinate_for_calibrated_coordinate,
    axis_a_gcode_coordinate_for_lowering,
    axis_a_lowering_for_gcode_coordinate,
    axis_a_model_calibrated_coordinate_for_commanded,
    axis_a_model_lowering_for_commanded,
    axis_a_model_parameters,
    axis_z_coefficients,
    axis_z_display_for_gcode_coordinate,
    axis_z_gcode_coordinate_for_display,
)


@dataclass(frozen=True)
class StageAxisCalibrationMapper:
    """Map calibrated user coordinates to controller coordinates."""

    axis_a_calibration: object | None
    axis_z_calibration: object | None
    position_reporting_mode: str
    active_work_coordinate_system: str | None
    controller_coordinate_offsets: Mapping[str, Sequence[float]]
    axis_index: Mapping[str, int]

    def axis_a_model_parameters(self) -> tuple[float, float, float, float] | None:
        return axis_a_model_parameters(self.axis_a_calibration)

    def axis_a_model_calibrated_coordinate_for_commanded(
        self,
        commanded_lowering_mm: float,
    ) -> float:
        return axis_a_model_calibrated_coordinate_for_commanded(
            self.axis_a_calibration,
            commanded_lowering_mm,
        )

    def axis_a_model_lowering_for_commanded(
        self,
        commanded_lowering_mm: float,
    ) -> float:
        return axis_a_model_lowering_for_commanded(
            self.axis_a_calibration,
            commanded_lowering_mm,
        )

    def axis_a_calibrated_coordinate_for_gcode_coordinate(
        self,
        a_coordinate_mm: float,
    ) -> float:
        return axis_a_calibrated_coordinate_for_gcode_coordinate(
            self.axis_a_calibration,
            a_coordinate_mm,
        )

    def axis_a_lowering_for_gcode_coordinate(self, a_coordinate_mm: float) -> float:
        return axis_a_lowering_for_gcode_coordinate(
            self.axis_a_calibration,
            a_coordinate_mm,
        )

    def axis_work_offset_for_configured_mode(
        self,
        axis: str,
        status: object | None = None,
    ) -> float:
        if self.position_reporting_mode == "machine":
            return 0.0
        idx = self.axis_index.get(axis.upper().strip())
        if idx is None:
            return 0.0
        work_offset = None if status is None else getattr(status, "work_offset", None)
        coordinate_system = (
            None if status is None else getattr(status, "coordinate_system", None)
        ) or self.active_work_coordinate_system
        if work_offset is None and coordinate_system:
            work_offset = self.controller_coordinate_offsets.get(coordinate_system)
        if work_offset is None or idx >= len(work_offset):
            return 0.0
        return float(work_offset[idx])

    def position_for_configured_mode(
        self,
        status: object | None,
    ) -> tuple[float, ...] | None:
        if status is None:
            return None
        position = (
            getattr(status, "position", None)
            if self.position_reporting_mode == "machine"
            else getattr(status, "work_position", None)
        )
        if position is None:
            return None
        return tuple(float(value) for value in position)

    def axis_value_for_configured_mode(
        self,
        status: object | None,
        axis: str,
    ) -> float | None:
        position = self.position_for_configured_mode(status)
        if position is None:
            return None
        idx = self.axis_index.get(axis.upper())
        if idx is None or idx >= len(position):
            return None
        return float(position[idx])

    def axis_limits_for_configured_mode(
        self,
        axis: str,
        limits: tuple[float, float] | None,
        status: object | None,
    ) -> tuple[float, float] | None:
        if not limits:
            return None
        if self.position_reporting_mode == "machine":
            return limits
        idx = self.axis_index.get(axis.upper())
        if idx is None:
            return limits
        work_offset = None if status is None else getattr(status, "work_offset", None)
        if work_offset is None and self.active_work_coordinate_system:
            work_offset = self.controller_coordinate_offsets.get(
                self.active_work_coordinate_system
            )
        if work_offset is None or idx >= len(work_offset):
            return None
        min_value, max_value = limits
        offset = float(work_offset[idx])
        return (float(min_value) - offset, float(max_value) - offset)

    def axis_a_lowering_for_configured_coordinate(
        self,
        a_coordinate_mm: float,
        status: object | None = None,
    ) -> float:
        machine_coordinate = (
            float(a_coordinate_mm)
            + self.axis_work_offset_for_configured_mode("A", status)
        )
        return self.axis_a_lowering_for_gcode_coordinate(machine_coordinate)

    def axis_a_configured_coordinate_for_lowering(
        self,
        lowering_mm: float,
        status: object | None = None,
    ) -> float:
        machine_coordinate = self.axis_a_gcode_coordinate_for_lowering(lowering_mm)
        return machine_coordinate - self.axis_work_offset_for_configured_mode(
            "A",
            status,
        )

    def axis_a_gcode_coordinate_for_calibrated_coordinate(
        self,
        calibrated_coordinate_mm: float,
    ) -> float:
        return axis_a_gcode_coordinate_for_calibrated_coordinate(
            self.axis_a_calibration,
            calibrated_coordinate_mm,
        )

    def axis_a_gcode_coordinate_for_lowering(self, lowering_mm: float) -> float:
        return axis_a_gcode_coordinate_for_lowering(
            self.axis_a_calibration,
            lowering_mm,
        )

    def axis_a_commanded_lowering_for_calibrated_coordinate(
        self,
        calibrated_coordinate_mm: float,
    ) -> float:
        return axis_a_commanded_lowering_for_calibrated_coordinate(
            self.axis_a_calibration,
            calibrated_coordinate_mm,
        )

    def axis_a_gcode_coordinate_for_lowering_step(
        self,
        current_a: float,
        requested_step_mm: float,
    ) -> float:
        current_physical = self.axis_a_lowering_for_configured_coordinate(current_a)
        target_physical = current_physical - float(requested_step_mm)
        return self.axis_a_configured_coordinate_for_lowering(target_physical)

    def axis_z_coefficients(self) -> tuple[float, ...] | None:
        return axis_z_coefficients(self.axis_z_calibration)

    def axis_z_display_for_gcode_coordinate(self, z_coordinate_mm: float) -> float:
        return axis_z_display_for_gcode_coordinate(
            self.axis_z_calibration,
            z_coordinate_mm,
        )

    def axis_z_gcode_coordinate_for_display(self, display_mm: float) -> float:
        return axis_z_gcode_coordinate_for_display(
            self.axis_z_calibration,
            display_mm,
        )
