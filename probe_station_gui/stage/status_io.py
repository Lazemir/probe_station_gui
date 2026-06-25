"""FluidNC command acknowledgement and status-frame I/O for stage control."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Optional

import serial

from probe_station_gui.stage.errors import SERIAL_IO_EXCEPTIONS, StageControllerError
from probe_station_gui.stage.fluidnc_protocol import (
    parse_float_tuple,
    parse_fluidnc_status_line,
)
from probe_station_gui.stage.types import _Status


logger = logging.getLogger(__name__)

_SERIAL_IO_EXCEPTIONS = SERIAL_IO_EXCEPTIONS


class StageControllerStatusIOMixin:
    """Internal command acknowledgement and status-query methods."""

    def _write_command(
        self,
        serial_connection: serial.Serial,
        command: str,
        *,
        check_cancelled: bool = True,
    ) -> None:
        if check_cancelled:
            self._check_cancelled()
        self._discard_pending_status_input(
            serial_connection,
            reason=f"before command {command.strip()}",
        )
        data = (command.strip() + "\n").encode("ascii")
        try:
            logger.debug("SERIAL TRACE stage_write command=%s", command.strip())
            serial_connection.write(data)
            serial_connection.flush()
            logger.debug("SERIAL TRACE stage_write_flushed command=%s", command.strip())
        except _SERIAL_IO_EXCEPTIONS as exc:  # pragma: no cover - hardware interaction
            raise StageControllerError(f"Serial write failed: {exc}") from exc

    def _wait_for_ok(
        self,
        serial_connection: serial.Serial,
        timeout: float = 5.0,
        *,
        check_cancelled: bool = True,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check_cancelled:
                self._check_cancelled()
            try:
                raw = serial_connection.readline()
            except _SERIAL_IO_EXCEPTIONS as exc:  # pragma: no cover - hardware interaction
                raise StageControllerError(f"Serial read failed: {exc}") from exc
            line = raw.decode("ascii", errors="ignore").strip()
            if not line:
                continue
            logger.debug("SERIAL TRACE stage_readline wait_for_ok line=%r", line)
            self._raise_if_controller_reboot_line(line, "wait_for_ok")
            self._handle_limit_line(line)
            homed_msg = self.HOMED_MSG_PATTERN.match(line)
            if homed_msg:
                axes = set(homed_msg.group("axes").upper())
                if self._homed_axes:
                    axes = set(self._homed_axes).union(axes)
                self._update_homing_status(axes)
                continue
            if line.lower() == "ok":
                return
            if line.lower().startswith("alarm"):
                raise StageControllerError(f"Controller alarm: {line}")
            if line.startswith("[MSG:ERR:"):
                raise StageControllerError(f"Controller reported: {line}")
            if line.lower().startswith("error"):
                raise StageControllerError(f"Controller reported: {line}")
        raise StageControllerError("Timeout waiting for controller acknowledgement.")

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
        serial_connection: serial.Serial,
        timeout: float = 1.5,
        *,
        check_cancelled: bool = True,
    ) -> Optional[_Status]:
        desired_mask = self._desired_status_report_mask_for_mode(
            self._position_reporting_mode
        )
        self._ensure_status_report_mask(
            serial_connection,
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
        self._last_stage_state = status.state
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
        serial_connection: serial.Serial,
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
        serial_connection: serial.Serial,
        *,
        reason: str = "status query",
    ) -> None:
        try:
            waiting = int(getattr(serial_connection, "in_waiting", 0) or 0)
        except (TypeError, ValueError, AttributeError, _SERIAL_IO_EXCEPTIONS):
            waiting = 0
        if waiting <= 0:
            return
        try:
            if hasattr(serial_connection, "read"):
                data = serial_connection.read(waiting)
                logger.debug(
                    "SERIAL TRACE discard_pending_input reason=%s bytes=%r",
                    reason,
                    data[:200],
                )
                self._handle_pending_serial_data_side_effects(data, reason)
                return
            serial_connection.reset_input_buffer()
            logger.debug(
                "SERIAL TRACE discard_pending_input reason=%s bytes=%d",
                reason,
                waiting,
            )
        except (AttributeError, _SERIAL_IO_EXCEPTIONS):
            logger.debug("Serial input buffer reset failed after stale input.")

    def _read_status_frame(
        self,
        serial_connection: serial.Serial,
        *,
        timeout: float,
        check_cancelled: bool = True,
    ) -> Optional[_Status]:
        try:
            self._discard_pending_status_input(
                serial_connection,
                reason="before status query",
            )
            logger.debug("SERIAL TRACE stage_query_status write=?")
            serial_connection.write(b"?\n")
            serial_connection.flush()
            logger.debug("SERIAL TRACE stage_query_status flushed=?")
        except _SERIAL_IO_EXCEPTIONS as exc:  # pragma: no cover - hardware interaction
            raise StageControllerError(f"Serial query failed: {exc}") from exc
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check_cancelled:
                self._check_cancelled()
            try:
                raw = serial_connection.readline()
            except _SERIAL_IO_EXCEPTIONS as exc:  # pragma: no cover - hardware interaction
                raise StageControllerError(f"Serial read failed: {exc}") from exc
            line = raw.decode("ascii", errors="ignore").strip()
            if not line:
                continue
            logger.debug("SERIAL TRACE stage_readline query_status line=%r", line)
            self._raise_if_controller_reboot_line(line, "status query")
            self._handle_limit_line(line)
            if line.lower().startswith("alarm"):
                raise StageControllerError(f"Controller alarm: {line}")
            homed_msg = self.HOMED_MSG_PATTERN.match(line)
            if homed_msg:
                axes = set(homed_msg.group("axes").upper())
                if self._homed_axes:
                    axes = set(self._homed_axes).union(axes)
                self._update_homing_status(axes)
                continue
            self._handle_coordinate_state_line(line)
            status = self._parse_status_line(line)
            if status is None:
                continue
            homed_match = self.HOMED_PATTERN.search(line)
            if homed_match:
                status.homed_axes = set(homed_match.group(1).upper())
                self._update_homing_status(status.homed_axes)
            return status
        return None

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
