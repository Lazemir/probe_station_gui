"""FluidNC command acknowledgement and status-frame I/O for stage control."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from typing import Optional

from probe_station_gui.stage.errors import StageControllerError
from probe_station_gui.stage.axis_mapping import CalibrationOutOfDomain
from probe_station_gui.stage.fluidnc_protocol import (
    parse_float_tuple,
    parse_fluidnc_status_line,
)
from probe_station_gui.stage.machine_coordinates import MachineCoordinateSnapshot
from probe_station_gui.stage.types import _Status


logger = logging.getLogger(__name__)


class StageControllerStatusIOMixin:
    """Internal command acknowledgement and status-query methods."""

    def _write_command(
        self,
        serial_connection,
        command: str,
        *,
        check_cancelled: bool = True,
    ) -> None:
        self._fluidnc_session_for(serial_connection).write_command(
            command,
            check_cancelled=check_cancelled,
        )

    def _wait_for_ok(
        self,
        serial_connection,
        timeout: float = 5.0,
        *,
        check_cancelled: bool = True,
    ) -> None:
        self._fluidnc_session_for(serial_connection).wait_for_ok(
            timeout=timeout,
            check_cancelled=check_cancelled,
        )

    def _write_current_command_and_wait(
        self,
        command: str,
        *,
        timeout: float = 5.0,
        check_cancelled: bool = True,
    ) -> None:
        serial_connection = self._current_serial()
        if check_cancelled:
            self._write_command(serial_connection, command)
        else:
            self._write_command(
                serial_connection,
                command,
                check_cancelled=check_cancelled,
            )
        if timeout == 5.0 and check_cancelled:
            self._wait_for_ok(serial_connection)
        elif check_cancelled:
            self._wait_for_ok(serial_connection, timeout=timeout)
        else:
            self._wait_for_ok(
                serial_connection,
                timeout=timeout,
                check_cancelled=check_cancelled,
            )

    def _wait_for_idle(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancelled()
            status = self._query_current_status()
            logger.debug(
                "SERIAL TRACE wait_for_idle status=%s position=%s",
                None if status is None else status.state,
                None if status is None else status.display_position,
            )
            if status and status.state.lower() == "idle":
                return
            if status and status.state.lower() == "alarm":
                raise StageControllerError("Controller entered ALARM state.")
            time.sleep(0.1)
        raise StageControllerError("Controller did not return to IDLE state in time.")

    def _wait_for_idle_at_targets(
        self,
        targets: dict[str, float],
        *,
        timeout: float,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancelled()
            status = self._query_current_status()
            logger.debug(
                "SERIAL TRACE wait_for_target_idle status=%s position=%s targets=%s",
                None if status is None else status.state,
                None if status is None else status.display_position,
                targets,
            )
            if status and status.state.lower() == "alarm":
                raise StageControllerError("Controller entered ALARM state.")
            if (
                status
                and status.state.lower() == "idle"
                and self._status_matches_axis_targets(status, targets)
            ):
                return
            time.sleep(0.1)
        raise StageControllerError("Controller did not reach the requested coordinate in time.")

    def _status_matches_axis_targets(
        self,
        status: _Status,
        targets: dict[str, float],
        *,
        tolerance: float | None = None,
    ) -> bool:
        allowed_error = (
            self.COORDINATE_TARGET_STATUS_TOLERANCE
            if tolerance is None
            else max(0.0, float(tolerance))
        )
        for axis, target in targets.items():
            value = self._axis_value_for_configured_mode(status, axis)
            if value is None:
                return False
            if abs(float(value) - float(target)) > allowed_error:
                return False
        return True

    def _query_status(
        self,
        serial_connection,
        timeout: float = 1.5,
        *,
        check_cancelled: bool = True,
    ) -> Optional[_Status]:
        desired_mask = self._desired_status_report_mask_for_mode(
            self._position_reporting_mode
        )
        self._ensure_status_report_mask(
            desired_mask,
            check_cancelled=check_cancelled,
        )
        status = self._read_status_frame(
            serial_connection,
            timeout=timeout,
            check_cancelled=check_cancelled,
        )
        if status is None:
            return None
        if self._position_reporting_mode == "work":
            status.display_position = status.work_position
        else:
            status.display_position = status.position
        self._last_status_timestamp = time.monotonic()
        self._controller_state_stale = False
        self._update_cached_positions(status)
        self._update_limit_axes_from_status(status)
        self._update_needles_from_status(status)
        self._ensure_b_axis_zero_reference(status)
        if (
            self._controller_reboot_recovery_pending
            and not self._controller_reboot_ready_notified
        ):
            self._controller_reboot_ready_notified = True
            self.controller_reboot_ready.emit()
        return status

    def _query_current_status(self) -> Optional[_Status]:
        return self._query_status(self._current_serial())

    def _query_current_status_with_required_coordinates(
        self,
        *,
        axes: Iterable[str] | None = None,
        min_axes: int | None = None,
        timeout: float | None = None,
        attempts: int | None = None,
    ) -> Optional[_Status]:
        kwargs: dict[str, object] = {}
        if axes is not None:
            kwargs["axes"] = axes
        if min_axes is not None:
            kwargs["min_axes"] = min_axes
        if timeout is not None:
            kwargs["timeout"] = timeout
        if attempts is not None:
            kwargs["attempts"] = attempts
        return self._query_status_with_required_coordinates(
            self._current_serial(),
            **kwargs,
        )

    def _query_status_with_required_coordinates(
        self,
        serial_connection,
        *,
        axes: Iterable[str] | None = None,
        min_axes: int | None = None,
        timeout: float | None = None,
        attempts: int | None = None,
    ) -> Optional[_Status]:
        """Read status repeatedly until it includes the required coordinates."""

        required_axis_count = self._required_coordinate_axis_count(
            axes,
            min_axes=min_axes,
        )
        read_attempts = max(
            1,
            int(
                self.COORDINATE_STATUS_READ_ATTEMPTS
                if attempts is None
                else attempts
            ),
        )
        for attempt in range(read_attempts):
            status = (
                self._query_status(serial_connection)
                if timeout is None
                else self._query_status(serial_connection, timeout=timeout)
            )
            position = self._position_for_configured_mode(status)
            if position is not None and len(position) >= required_axis_count:
                return status

            if attempt + 1 < read_attempts:
                logger.debug(
                    "Controller status did not include required coordinates; "
                    "retrying status query (attempt %d/%d, required_axes=%d, "
                    "position=%r, state=%r).",
                    attempt + 1,
                    read_attempts,
                    required_axis_count,
                    position,
                    None if status is None else getattr(status, "state", None),
                )
                if status is None:
                    self._discard_pending_status_input(serial_connection)
                time.sleep(self.COORDINATE_STATUS_RETRY_DELAY_S)
            else:
                logger.debug(
                    "Controller status did not include required coordinates "
                    "(attempt %d/%d, required_axes=%d, position=%r, state=%r).",
                    attempt + 1,
                    read_attempts,
                    required_axis_count,
                    position,
                    None if status is None else getattr(status, "state", None),
                )

        return None

    def _required_coordinate_axis_count(
        self,
        axes: Iterable[str] | None,
        *,
        min_axes: int | None = None,
    ) -> int:
        required = 0 if min_axes is None else max(0, int(min_axes))
        for axis in axes or ():
            axis_key = str(axis).upper().strip()
            try:
                axis_index = self.AXIS_INDEX[axis_key]
            except KeyError as exc:
                raise StageControllerError(f"Unsupported axis: {axis}") from exc
            required = max(required, axis_index + 1)
        return required

    def _discard_pending_status_input(
        self,
        serial_connection,
        *,
        reason: str = "status query",
    ) -> None:
        self._fluidnc_session_for(serial_connection).discard_pending_input(
            reason=reason
        )

    def _read_status_frame(
        self,
        serial_connection,
        *,
        timeout: float,
        check_cancelled: bool = True,
        parse_status_line=None,
    ) -> Optional[_Status]:
        return self._fluidnc_session_for(serial_connection).read_status_frame(
            timeout=timeout,
            check_cancelled=check_cancelled,
            parse_status_line=parse_status_line,
        )

    def _handle_homing_message_line(self, line: str) -> bool:
        homed_msg = self.HOMED_MSG_PATTERN.match(line)
        if homed_msg is None:
            return False
        axes = set(homed_msg.group("axes").upper())
        if self._homed_axes:
            axes = set(self._homed_axes).union(axes)
        self._update_homing_status(axes)
        return True

    def _status_homed_axes_from_line(self, line: str) -> set[str] | None:
        homed_match = self.HOMED_PATTERN.search(line)
        if homed_match is None:
            return None
        return set(homed_match.group(1).upper())

    def _parse_status_line(self, line: str) -> Optional[_Status]:
        return parse_fluidnc_status_line(
            line,
            position_reporting_mode=self._position_reporting_mode,
            active_work_coordinate_system=self._active_work_coordinate_system,
            controller_coordinate_offsets=self._controller_coordinate_offsets,
            axis_index=self.AXIS_INDEX,
        )

    @staticmethod
    def _parse_float_tuple(raw: str) -> tuple[float, ...] | None:
        return parse_float_tuple(raw)

    def _poll_status_once(self) -> None:
        serial_connection = self._serial
        if serial_connection is None or not serial_connection.is_open:
            return
        try:
            if not self._serial_session_lock.acquire(blocking=False):
                return
            try:
                with self._serial_session(serial_connection):
                    self._query_status(serial_connection, check_cancelled=False)
            finally:
                self._serial_session_lock.release()
        except StageControllerError:
            return

    def _run_status_refresh(self) -> None:
        lease = self._operation_lifecycle.try_reserve_idle("status refresh")
        try:
            if lease is not None:
                with lease:
                    self._poll_status_once()
        finally:
            with self._state_lock:
                self._status_refresh_thread = None

    def current_stage_position(self) -> tuple[float, ...]:
        """Return the latest controller position in the active GUI coordinate space."""

        lease = self._operation_lifecycle.try_reserve_idle("stage position query")
        if lease is None:
            raise StageControllerError("Stage is busy. Wait for the current operation to finish.")
        with lease:
            status = self._query_current_stage_position_status()
            return self._stage_position_from_status(status)

    def run_external_current_stage_position(self) -> tuple[float, ...]:
        """Read stage position from the worker that owns an external reservation."""

        operation = self._operation_lifecycle.snapshot()
        if not operation.owned_by(threading.current_thread()):
            raise StageControllerError(
                "Current thread does not own an external stage task."
            )
        status = self._query_current_stage_position_status()
        return self._stage_position_from_status(status)

    def run_external_current_physical_machine_coordinates(
        self,
        axes: Iterable[str],
    ) -> dict[str, float]:
        """Read synchronized physical Machine coordinates inside an external task."""

        operation = self._operation_lifecycle.snapshot()
        if not operation.owned_by(threading.current_thread()):
            raise StageControllerError(
                "Current thread does not own an external stage task."
            )
        with self._serial_session():
            return self._current_physical_machine_coordinates_locked(axes)

    def request_machine_coordinate_snapshot(
        self,
        request_id: object,
        *,
        axes: Iterable[str],
    ) -> bool:
        """Capture one calibrated, provenance-checked status off the GUI thread."""

        normalized_axes = tuple(str(axis).strip().upper() for axis in axes)
        return self._start_background_task(
            target=self._run_machine_coordinate_snapshot_request,
            args=(request_id, normalized_axes),
            busy_message="Stage is busy. Wait before capturing a reference.",
        )

    def _run_machine_coordinate_snapshot_request(
        self,
        request_id: object,
        axes: tuple[str, ...],
    ) -> None:
        try:
            status = self._query_current_stage_position_status()
            snapshot = MachineCoordinateSnapshot.from_status(
                status,
                self._axis_calibration_mapper(),
                self.AXIS_INDEX,
            )
            for axis in axes:
                snapshot.physical_machine_pose.require(axis)
        except Exception as exc:
            self.machine_coordinate_snapshot_finished.emit(
                request_id,
                False,
                None,
                str(exc),
            )
            return
        self.machine_coordinate_snapshot_finished.emit(
            request_id,
            True,
            snapshot,
            "",
        )

    def _current_physical_machine_coordinates_locked(
        self,
        axes: Iterable[str],
    ) -> dict[str, float]:
        normalized_axes = tuple(str(axis).strip().upper() for axis in axes)
        serial_connection = self._current_serial()
        restore_mask = self._desired_status_report_mask_for_mode(
            self._position_reporting_mode
        )
        machine_mask = self._desired_status_report_mask_for_mode("machine")
        try:
            self._ensure_status_report_mask(machine_mask)
            required_count = self._required_coordinate_axis_count(normalized_axes)
            status = None
            for attempt in range(max(1, int(self.COORDINATE_STATUS_READ_ATTEMPTS))):
                status = self._read_status_frame(
                    serial_connection,
                    timeout=1.5,
                    parse_status_line=self._parse_raw_machine_status_line,
                )
                if status is not None and status.position is not None and len(status.position) >= required_count:
                    break
                if attempt + 1 < int(self.COORDINATE_STATUS_READ_ATTEMPTS):
                    time.sleep(self.COORDINATE_STATUS_RETRY_DELAY_S)
            return self._physical_machine_coordinates_from_status(
                status,
                axes=normalized_axes,
            )
        finally:
            self._ensure_status_report_mask(restore_mask, check_cancelled=False)

    def _parse_raw_machine_status_line(self, line: str) -> _Status | None:
        return parse_fluidnc_status_line(
            line,
            position_reporting_mode="machine",
            active_work_coordinate_system=self._active_work_coordinate_system,
            controller_coordinate_offsets=self._controller_coordinate_offsets,
            axis_index=self.AXIS_INDEX,
        )

    def _physical_machine_coordinates_from_status(
        self,
        status: _Status | None,
        *,
        axes: Iterable[str],
    ) -> dict[str, float]:
        normalized_axes = tuple(str(axis).strip().upper() for axis in axes)
        if status is None or status.position is None:
            raise StageControllerError("Unable to read physical Machine coordinates.")
        if status.state.lower() in {"jog", "run"}:
            raise StageControllerError(
                "Wait for the stage to stop before capturing a reference."
            )
        mapper = self._axis_calibration_mapper()
        result: dict[str, float] = {}
        for axis in normalized_axes:
            index = self.AXIS_INDEX.get(axis)
            if index is None:
                raise StageControllerError(f"Unsupported axis: {axis}")
            if index >= len(status.position):
                raise StageControllerError(
                    f"Controller did not report complete Machine coordinates for {axis}."
                )
            raw_value = float(status.position[index])
            try:
                result[axis] = float(mapper.controller_to_physical(axis, raw_value))
            except CalibrationOutOfDomain as exc:
                raise StageControllerError(
                    f"Machine {axis}={raw_value:.6f} is outside the axis calibration domain."
                ) from exc
        return result

    def _query_current_stage_position_status(self) -> _Status | None:
        with self._serial_session():
            return self._query_synced_status_for_absolute_motion(min_axes=3)

    def _stage_position_from_status(
        self,
        status: _Status | None,
    ) -> tuple[float, ...]:
        if status is None or status.display_position is None:
            raise StageControllerError("Unable to read stage position.")
        if len(status.display_position) < 3:
            raise StageControllerError("Controller did not report complete X/Y/Z coordinates.")
        if status.state.lower() in {"jog", "run"}:
            raise StageControllerError("Wait for the stage to stop before capturing a marker.")
        self._ensure_b_axis_zero_reference(status)
        return tuple(float(value) for value in status.display_position)

    def request_status_refresh(self) -> None:
        """Poll controller position in a background thread when idle."""

        with self._state_lock:
            if not self._async_write_queue.empty():
                return
            if self._jog_motion_active:
                return
            serial_connection = self._serial
            if serial_connection is None or not serial_connection.is_open:
                return
            if (
                self._status_refresh_thread is not None
                and self._status_refresh_thread.is_alive()
            ):
                return
            thread = threading.Thread(target=self._run_status_refresh, daemon=True)
            self._status_refresh_thread = thread
            thread.start()

    def _query_synced_status_for_absolute_motion(
        self,
        *,
        refresh_coordinate_state: bool = True,
        axes: Iterable[str] | None = None,
        min_axes: int | None = None,
    ) -> Optional[_Status]:
        """Refresh coordinate-system state before absolute position reads and moves."""

        serial_connection = self._current_serial()
        with self._serial_session(serial_connection):
            if not refresh_coordinate_state:
                state_was_stale = self._controller_state_stale
                status = self._query_status_with_required_coordinates(
                    serial_connection,
                    axes=axes,
                    min_axes=min_axes,
                )
                if (
                    status is not None
                    and not state_was_stale
                ):
                    return status

            if self._position_reporting_mode != "machine" or self._controller_state_stale:
                self._refresh_coordinate_system_state(apply_preference=True)
            return self._query_status_with_required_coordinates(
                serial_connection,
                axes=axes,
                min_axes=min_axes,
            )
