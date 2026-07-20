"""Universal calibrated-coordinate mapping for stage control."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Optional

from probe_station_gui.settings.axis_calibration_config import AxisCalibrationSettings
from probe_station_gui.stage.axis_calibration import (
    CalibrationCoordinateUnavailable,
    StageAxisCalibrationMapper,
)
from probe_station_gui.stage.axis_mapping import (
    AxisCalibrationCurve,
    CalibrationOutOfDomain,
    curve_from_settings,
)
from probe_station_gui.stage.errors import StageControllerError
from probe_station_gui.stage.needle_targets import normalise_needle_lowering_target
from probe_station_gui.stage.types import _Status


class StageControllerAxisCoordinatesMixin:
    """Controller-facing operations built on the pure six-axis mapper."""

    def apply_axis_calibrations(
        self,
        calibrations: Mapping[str, AxisCalibrationSettings],
    ) -> None:
        """Replace all runtime curves with validated settings snapshots."""

        curves: dict[str, AxisCalibrationCurve] = {}
        for raw_axis, settings in calibrations.items():
            axis = str(raw_axis).strip().upper()
            if axis not in self.AXIS_INDEX:
                continue
            curve = curve_from_settings(settings)
            if curve is not None:
                curves[axis] = curve
        self._axis_calibrations = curves

    def axis_a_gcode_coordinate_for_lowering(self, lowering_mm: float) -> float:
        """Map physical A lowering to a raw machine A coordinate."""

        try:
            return self._axis_calibration_mapper().physical_to_controller(
                "A",
                -float(lowering_mm),
            )
        except CalibrationOutOfDomain as error:
            raise self._calibration_target_error("A") from error

    def axis_a_lowering_for_gcode_coordinate(self, a_coordinate_mm: float) -> float:
        """Map a raw machine A coordinate to positive physical lowering."""

        try:
            physical = self._axis_calibration_mapper().controller_to_physical(
                "A",
                float(a_coordinate_mm),
            )
        except CalibrationOutOfDomain as error:
            raise self._calibration_target_error("A") from error
        return -physical

    def axis_a_configured_coordinate_for_lowering(self, lowering_mm: float) -> float:
        return self._axis_a_configured_target_for_lowering(lowering_mm)

    def axis_a_lowering_for_configured_coordinate(self, a_coordinate_mm: float) -> float:
        return self._axis_a_lowering_for_configured_coordinate(a_coordinate_mm)

    def _normalise_needle_lowering_target(
        self,
        position_mm: Optional[float],
    ) -> Optional[float]:
        return normalise_needle_lowering_target(
            position_mm,
            lowering_for_gcode_coordinate=self.axis_a_lowering_for_gcode_coordinate,
        )

    def calibrated_axis_display_value(self, axis: str, raw_value: float) -> float:
        """Map a configured raw coordinate to its physical display coordinate."""

        normalized = str(axis).strip().upper()
        try:
            return self._axis_calibration_mapper().configured_controller_to_physical(
                normalized,
                float(raw_value),
            )
        except (CalibrationOutOfDomain, CalibrationCoordinateUnavailable) as error:
            raise self._calibration_target_error(normalized) from error

    def calibrated_axis_raw_value(self, axis: str, display_value: float) -> float:
        """Map a physical display coordinate to the configured raw basis."""

        normalized = str(axis).strip().upper()
        try:
            return self._axis_calibration_mapper().physical_to_configured_controller(
                normalized,
                float(display_value),
            )
        except (CalibrationOutOfDomain, CalibrationCoordinateUnavailable) as error:
            raise self._calibration_target_error(normalized) from error

    def calibrated_axis_raw_target_value(
        self,
        axis: str,
        display_value: float,
    ) -> float | None:
        """Return an inverse-mapped target, or ``None`` when unavailable."""

        try:
            return self.calibrated_axis_raw_value(axis, display_value)
        except StageControllerError:
            return None

    def validate_calibrated_axis_raw_target(self, axis: str, raw_value: float) -> None:
        """Reject configured raw targets outside an enabled measured domain."""

        normalized = str(axis).strip().upper()
        try:
            self._axis_calibration_mapper().configured_controller_to_physical(
                normalized,
                float(raw_value),
            )
        except (CalibrationOutOfDomain, CalibrationCoordinateUnavailable) as error:
            raise self._calibration_target_error(normalized) from error

    def axis_raw_limits_for_configured_mode(
        self,
        axis: str,
        status: _Status | None = None,
    ) -> tuple[float, float] | None:
        """Intersect software limits with the enabled calibration domain."""

        normalized = str(axis).strip().upper()
        mapper = self._axis_calibration_mapper()
        software_limits = mapper.axis_limits_for_configured_mode(
            normalized,
            self._axis_limits.get(normalized),
            status,
        )
        try:
            calibration_limits = mapper.controller_domain_for_configured_mode(
                normalized,
                status,
            )
        except (CalibrationOutOfDomain, CalibrationCoordinateUnavailable):
            return None
        if software_limits is None:
            return calibration_limits
        if calibration_limits is None:
            return software_limits
        lower = max(software_limits[0], calibration_limits[0])
        upper = min(software_limits[1], calibration_limits[1])
        return None if lower > upper else (lower, upper)

    def calibrated_axis_display_limits(
        self,
        axis: str,
        status: _Status | None = None,
    ) -> tuple[float, float] | None:
        """Return usable limits in the physical display coordinate."""

        normalized = str(axis).strip().upper()
        raw_limits = self.axis_raw_limits_for_configured_mode(normalized, status)
        if raw_limits is None:
            return None
        mapper = self._axis_calibration_mapper()
        try:
            return (
                mapper.configured_controller_to_physical(normalized, raw_limits[0], status),
                mapper.configured_controller_to_physical(normalized, raw_limits[1], status),
            )
        except (CalibrationOutOfDomain, CalibrationCoordinateUnavailable):
            return None

    def _axis_calibration_mapper(self) -> StageAxisCalibrationMapper:
        return StageAxisCalibrationMapper(
            calibrations=self._axis_calibrations,
            position_reporting_mode=self._position_reporting_mode,
            active_work_coordinate_system=self._active_work_coordinate_system,
            controller_coordinate_offsets=self._controller_coordinate_offsets,
            axis_index=self.AXIS_INDEX,
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
        machine_coordinate = float(a_coordinate_mm) + mapper.axis_work_offset_for_configured_mode(
            "A",
            status,
        )
        try:
            physical = mapper.controller_to_physical("A", machine_coordinate)
        except (CalibrationOutOfDomain, CalibrationCoordinateUnavailable) as error:
            raise self._calibration_target_error("A") from error
        return -physical

    def _axis_a_configured_coordinate_for_lowering(
        self,
        lowering_mm: float,
        status: _Status | None = None,
    ) -> float:
        return self._axis_a_configured_target_for_lowering(lowering_mm, status)

    def _axis_a_configured_target_for_lowering(
        self,
        lowering_mm: float,
        status: _Status | None = None,
    ) -> float:
        mapper = self._axis_calibration_mapper()
        try:
            machine_coordinate = mapper.physical_to_controller(
                "A",
                -float(lowering_mm),
            )
        except (CalibrationOutOfDomain, CalibrationCoordinateUnavailable) as error:
            raise self._calibration_target_error("A") from error
        return machine_coordinate - mapper.axis_work_offset_for_configured_mode(
            "A",
            status,
        )

    def _axis_a_gcode_coordinate_for_lowering(self, lowering_mm: float) -> float:
        return self.axis_a_gcode_coordinate_for_lowering(lowering_mm)

    def _axis_a_gcode_coordinate_for_lowering_step(
        self,
        current_a: float,
        requested_step_mm: float,
    ) -> float:
        current_lowering = self._axis_a_lowering_for_configured_coordinate(current_a)
        return self._axis_a_configured_target_for_lowering(
            current_lowering - float(requested_step_mm)
        )

    def _position_for_configured_mode(
        self,
        status: _Status | None,
    ) -> tuple[float, ...] | None:
        return self._axis_calibration_mapper().position_for_configured_mode(status)

    def _axis_value_for_configured_mode(
        self,
        status: _Status | None,
        axis: str,
    ) -> float | None:
        return self._axis_calibration_mapper().axis_value_for_configured_mode(status, axis)

    def _axis_limits_for_configured_mode(
        self,
        axis: str,
        status: _Status | None,
    ) -> tuple[float, float] | None:
        return self._axis_calibration_mapper().axis_limits_for_configured_mode(
            axis,
            self._axis_limits.get(axis),
            status,
        )

    @staticmethod
    def _calibration_target_error(axis: str) -> StageControllerError:
        return StageControllerError(
            f"{axis} target cannot be represented by the calibrated axis mapping."
        )
