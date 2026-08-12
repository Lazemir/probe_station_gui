"""Universal calibrated-coordinate mapping for stage control."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
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
from probe_station_gui.stage.calibration_positions import a_position_failure_status
from probe_station_gui.stage.errors import StageControllerError
from probe_station_gui.stage.machine_coordinates import (
    MachineCoordinateSnapshotUnavailable,
)
from probe_station_gui.stage.needle_targets import normalise_needle_lowering_target
from probe_station_gui.stage.types import _Status


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NeedleHeightSaveResult:
    success: bool
    lowering_mm: float | None = None
    error: str = ""


class StageControllerAxisCoordinatesMixin:
    """Controller-facing operations built on the pure six-axis mapper."""

    def apply_coordinate_system_configuration(
        self,
        *,
        position_mode: str,
        startup_mode: str,
        preferred_system: str,
    ) -> None:
        """Apply coordinate-system preferences loaded from persistent settings."""

        reporting_mode = position_mode.strip().lower()
        if reporting_mode not in {"work", "machine"}:
            reporting_mode = "work"
        mode = startup_mode.strip().lower()
        if mode not in {"controller", "fixed"}:
            mode = "controller"
        system = preferred_system.strip().upper()
        if system not in self.WORK_COORDINATE_SYSTEMS:
            system = self.DEFAULT_WORK_COORDINATE_SYSTEM
        reporting_mode_changed = reporting_mode != self._position_reporting_mode
        self._position_reporting_mode = reporting_mode
        self._coordinate_startup_mode = mode
        self._preferred_work_coordinate_system = system
        if reporting_mode_changed:
            self._last_machine_coordinate_snapshot = None
            self._last_motion_coordinate_snapshot = None

    def active_coordinate_system(self) -> str | None:
        """Return the currently active work coordinate system, if known."""

        return self._active_work_coordinate_system

    def coordinate_display_name(self) -> str:
        """Return the label that matches the coordinates exposed to the GUI."""

        if self._position_reporting_mode == "machine":
            return "Machine"
        active = self._active_work_coordinate_system
        if active:
            return active
        return "Work"

    def axis_display_limits(self, axis: str) -> tuple[float, float] | None:
        """Return software limits in the same coordinate basis as the GUI."""

        return self.calibrated_axis_display_limits(axis.upper().strip(), None)

    def set_current_axis_work_coordinate(
        self,
        axis: str,
        value: float = 0.0,
    ) -> None:
        """Shift the active work offset so the current axis position reads value."""

        axis_key = axis.upper().strip()
        if axis_key not in self.AXIS_INDEX:
            raise StageControllerError(f"Unsupported axis: {axis}")
        try:
            target_value = float(value)
        except (TypeError, ValueError) as exc:
            raise StageControllerError(f"Unsupported {axis_key} coordinate: {value}") from exc
        if not math.isfinite(target_value):
            raise StageControllerError(f"Unsupported {axis_key} coordinate: {value}")
        lease = self._operation_lifecycle.try_reserve_idle(
            f"set {axis_key} work coordinate"
        )
        if lease is None:
            raise StageControllerError(
                "Stage is busy. Wait for the current operation to finish."
            )
        with lease:
            with self._state_lock:
                with self._serial_session():
                    self._set_current_axis_work_coordinate_locked(
                        axis_key,
                        target_value,
                    )

    def _set_current_axis_work_coordinate_locked(
        self,
        axis: str,
        value: float,
    ) -> None:
        serial_connection = self._current_serial()
        status = self._query_status_with_required_coordinates(
            serial_connection,
            axes=(axis,),
        )
        if status is None:
            raise StageControllerError("Unable to read controller status.")
        if status.state.lower() in {"jog", "run"}:
            raise StageControllerError(
                "Wait for the stage to stop before setting a work coordinate."
            )
        coordinate_system = (
            status.coordinate_system
            or self._active_work_coordinate_system
            or self._preferred_work_coordinate_system
        )
        coordinate_system = coordinate_system.strip().upper()
        p_value = self.WORK_COORDINATE_SYSTEM_P_VALUES.get(coordinate_system)
        if p_value is None:
            raise StageControllerError(
                f"Unsupported work coordinate system: {coordinate_system}."
            )
        self._require_homed_axes(status, {axis})
        command = (
            f"G10 L20 P{p_value} {axis}"
            f"{self._format_gcode_value(value, decimals=6)}"
        )
        self._write_current_command_and_wait(command)
        self._active_work_coordinate_system = coordinate_system
        if self._position_reporting_mode != "machine":
            try:
                self._controller_coordinate_offsets = self._query_work_coordinate_offsets()
            except Exception as exc:
                logger.warning("Unable to refresh work coordinate offsets: %s", exc)
        try:
            refreshed = self._query_status_with_required_coordinates(
                serial_connection,
                axes=(axis,),
            )
            if axis == "A" and refreshed is not None:
                self._update_needles_from_status(refreshed)
        except Exception as exc:
            logger.warning(
                "Unable to refresh stage status after setting %s work coordinate: %s",
                axis,
                exc,
            )
        self.status_message.emit(
            f"{axis} work coordinate set to {self._format_gcode_value(value)} "
            f"in {coordinate_system}."
        )

    def request_needle_height_save(self, request_id: object) -> bool:
        with self._shutdown_gate:
            if self._shutdown_started.is_set():
                return False
            return self._start_background_task(
                target=self._run_needle_height_save_request,
                args=(request_id,),
                busy_message="Wait for the stage to stop before saving needle contact.",
            )

    def _run_needle_height_save_request(self, request_id: object) -> None:
        try:
            with self._state_lock:
                with self._serial_session():
                    a_position = self._read_current_a_position()
                    if a_position is None:
                        reason = (
                            self.last_a_position_read_failure()
                            or "unknown reason"
                        )
                        result = NeedleHeightSaveResult(
                            False,
                            error=a_position_failure_status(reason),
                        )
                    else:
                        lowering_mm = self._axis_a_lowering_for_configured_coordinate(
                            a_position
                        )
                        try:
                            self._set_current_axis_work_coordinate_locked("A", 0.0)
                        except StageControllerError as exc:
                            result = NeedleHeightSaveResult(
                                False,
                                error=f"Unable to set A0 at needle contact: {exc}",
                            )
                        else:
                            result = NeedleHeightSaveResult(
                                True,
                                lowering_mm=lowering_mm,
                            )
        except Exception as exc:
            result = NeedleHeightSaveResult(
                False,
                error=f"Unable to save needle contact: {exc}",
            )
        with self._shutdown_gate:
            if not self._shutdown_started.is_set():
                self.needle_height_save_finished.emit(request_id, result)

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
        cached = self._last_machine_coordinate_snapshot
        if cached is not None:
            try:
                self._last_machine_coordinate_snapshot = cached.remap(
                    self._axis_calibration_mapper()
                )
            except MachineCoordinateSnapshotUnavailable:
                self._last_machine_coordinate_snapshot = None
        live_cached = self._last_motion_coordinate_snapshot
        if live_cached is not None:
            try:
                self._last_motion_coordinate_snapshot = live_cached.remap(
                    self._axis_calibration_mapper()
                )
            except MachineCoordinateSnapshotUnavailable:
                self._last_motion_coordinate_snapshot = None

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

    def axis_machine_display_limits(
        self,
        axis: str,
    ) -> tuple[float, float] | None:
        """Return software/calibration limits in physical Machine coordinates."""

        normalized = str(axis).strip().upper()
        mapper = self._axis_calibration_mapper()
        software_limits = self._axis_limits.get(normalized)
        calibration_limits = mapper.controller_domain(normalized)
        if software_limits is None:
            raw_limits = calibration_limits
        elif calibration_limits is None:
            raw_limits = software_limits
        else:
            lower = max(float(software_limits[0]), float(calibration_limits[0]))
            upper = min(float(software_limits[1]), float(calibration_limits[1]))
            raw_limits = None if lower > upper else (lower, upper)
        if raw_limits is None:
            return None
        try:
            physical = (
                mapper.controller_to_physical(normalized, float(raw_limits[0])),
                mapper.controller_to_physical(normalized, float(raw_limits[1])),
            )
        except CalibrationOutOfDomain:
            return None
        return min(physical), max(physical)

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
