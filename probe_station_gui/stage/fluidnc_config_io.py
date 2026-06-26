"""FluidNC modal state, config, and startup-limit I/O for stage control."""

from __future__ import annotations

import logging
import time

import serial

from probe_station_gui.stage.controller_cache import (
    parse_cached_axis_limits,
    parse_cached_axis_max_feedrates,
    parse_cached_controller_session_marker,
    parse_cached_coordinate_offsets,
)
from probe_station_gui.stage.errors import StageControllerError
from probe_station_gui.stage.fluidnc_protocol import (
    parse_fluidnc_axis_max_feedrates,
    parse_startup_axis_limits,
)
from probe_station_gui.stage.types import _Status


logger = logging.getLogger(__name__)

class StageControllerFluidNCConfigIOMixin:
    """Internal FluidNC modal-state and config-query methods."""

    @staticmethod
    def _desired_status_report_mask_for_mode(position_mode: str) -> int:
        # FluidNC RtStatus::Position bit selects MPos; without it reports WPos.
        # Keep buffer reporting enabled in both modes.
        return 3 if position_mode.strip().lower() == "machine" else 2

    def _ensure_status_report_mask(
        self,
        serial_connection: serial.Serial,
        mask: int,
        *,
        check_cancelled: bool = True,
    ) -> None:
        if self._current_status_report_mask == mask:
            return
        self._write_command(
            serial_connection,
            f"$10={int(mask)}",
            check_cancelled=check_cancelled,
        )
        self._wait_for_ok(serial_connection, check_cancelled=check_cancelled)
        self._current_status_report_mask = int(mask)

    def _query_active_coordinate_system(self, timeout: float = 2.0) -> str | None:
        tokens = self._query_modal_state_tokens(timeout=timeout)
        for token in tokens:
            candidate = token.strip().upper()
            if candidate in self.WORK_COORDINATE_SYSTEMS:
                return candidate
        return None

    def _query_modal_state_tokens(self, timeout: float = 2.0) -> list[str]:
        session = self._current_fluidnc_session()
        saw_ack = False
        saw_non_modal_response = False
        tokens: list[str] | None = None
        for line in session.iter_command_response_lines(
            "$G",
            timeout=timeout,
            source="$G",
            handle_coordinate_state=True,
        ):
            lower = line.lower()
            if lower == "ok":
                if tokens is not None:
                    return tokens
                if saw_non_modal_response:
                    logger.debug(
                        "SERIAL TRACE $G returned non-modal response before ok; treating modal state as empty."
                    )
                    return []
                saw_ack = True
                continue
            modal_match = self.MODAL_STATE_PATTERN.match(line)
            if modal_match:
                tokens = modal_match.group("modal").split()
                if saw_ack:
                    return tokens
            else:
                saw_non_modal_response = True
        if tokens is not None:
            session.reset_input_buffer(reason="after truncated $G response")
            return tokens
        if saw_ack:
            session.reset_input_buffer(reason="after empty $G response")
            return []
        raise StageControllerError("Timeout waiting for controller response: $G.")

    def _handle_coordinate_state_line(self, line: str) -> None:
        modal_match = self.MODAL_STATE_PATTERN.match(line)
        if modal_match:
            for token in modal_match.group("modal").split():
                candidate = token.strip().upper()
                if candidate in self.WORK_COORDINATE_SYSTEMS:
                    self._active_work_coordinate_system = candidate
                    return
            return
        offset_match = self.COORDINATE_OFFSET_PATTERN.match(line)
        if not offset_match:
            return
        coords = self._parse_float_tuple(offset_match.group("coords"))
        if coords is None:
            return
        system = offset_match.group("system").upper()
        self._controller_coordinate_offsets[system] = coords

    def _read_controller_session_marker(self) -> int | None:
        for token in self._query_modal_state_tokens():
            token = token.strip().upper()
            if not token.startswith("T"):
                continue
            try:
                marker = int(float(token[1:]))
            except ValueError:
                continue
            return marker if marker > 0 else None
        return None

    def _ensure_controller_session_marker(self) -> None:
        marker = self._controller_session_marker
        if marker is None:
            marker = self._new_controller_session_marker()
        self._write_current_command_and_wait(f"T{marker}")
        self._controller_session_marker = marker
        logger.info("Controller volatile session marker set to T%s.", marker)

    @staticmethod
    def _parse_cached_controller_session_marker(
        data: dict[str, object]
    ) -> int | None:
        return parse_cached_controller_session_marker(data)

    @classmethod
    def _parse_cached_axis_limits(
        cls, raw_limits: object
    ) -> dict[str, tuple[float, float]]:
        return parse_cached_axis_limits(
            raw_limits,
            axis_names=cls.AXIS_INDEX,
        )

    @classmethod
    def _parse_cached_axis_max_feedrates(
        cls, raw_feedrates: object
    ) -> dict[str, float]:
        return parse_cached_axis_max_feedrates(
            raw_feedrates,
            axis_names=cls.AXIS_INDEX,
            min_feedrate=cls.MIN_FEEDRATE,
        )

    @classmethod
    def _parse_cached_coordinate_offsets(
        cls, raw_offsets: object
    ) -> dict[str, tuple[float, ...]]:
        return parse_cached_coordinate_offsets(
            raw_offsets,
            coordinate_systems=cls.WORK_COORDINATE_SYSTEMS,
        )

    @staticmethod
    def _new_controller_session_marker() -> int:
        return 100 + (time.monotonic_ns() % 900)

    def _query_work_coordinate_offsets(
        self, timeout: float = 2.5
    ) -> dict[str, tuple[float, ...]]:
        session = self._current_fluidnc_session()
        offsets: dict[str, tuple[float, ...]] = {}
        saw_final_ok = False
        for line in session.iter_command_response_lines(
            "$#",
            timeout=timeout,
            source="$#",
            handle_homing_messages=True,
            handle_coordinate_state=True,
        ):
            lower = line.lower()
            if lower == "ok":
                if offsets:
                    saw_final_ok = True
                    break
                continue
            match = self.COORDINATE_OFFSET_PATTERN.match(line)
            if not match:
                continue
            coords = self._parse_float_tuple(match.group("coords"))
            if coords is None:
                continue
            offsets[match.group("system").upper()] = coords
        if offsets and not saw_final_ok:
            session.reset_input_buffer(reason="after truncated $# response")
        return offsets

    def _query_axis_max_feedrates_locked(
        self,
        serial_connection,
        timeout: float = 20.0,
    ) -> dict[str, float]:
        session = self._fluidnc_session_for(serial_connection)
        lines: list[str] = []
        saw_final_ok = False
        for line in session.iter_command_response_lines(
            "$CD",
            timeout=timeout,
            source="$CD",
            handle_homing_messages=True,
            handle_coordinate_state=True,
        ):
            lower = line.lower()
            if lower == "ok":
                if lines:
                    saw_final_ok = True
                    break
                continue
            lines.append(line)
        if lines and not saw_final_ok:
            session.reset_input_buffer(reason="after truncated $CD response")
        rates = parse_fluidnc_axis_max_feedrates(lines)
        if not rates:
            raise StageControllerError(
                "Controller config dump did not include axis max feedrates."
            )
        return rates

    def _refresh_coordinate_system_state(
        self,
        serial_connection: serial.Serial,
        *,
        apply_preference: bool,
    ) -> None:
        desired_mask = self._desired_status_report_mask_for_mode(
            self._position_reporting_mode
        )
        self._ensure_status_report_mask(serial_connection, desired_mask)
        if (
            self._position_reporting_mode != "machine"
            and apply_preference
            and self._coordinate_startup_mode == "fixed"
        ):
            self._write_command(serial_connection, self._preferred_work_coordinate_system)
            self._wait_for_ok(serial_connection)
        detected_system = None if self._position_reporting_mode == "machine" else None
        try:
            if self._position_reporting_mode != "machine":
                detected_system = self._query_active_coordinate_system()
        except StageControllerError as exc:
            logger.warning("Unable to query active coordinate system: %s", exc)
        if (
            detected_system is None
            and self._position_reporting_mode != "machine"
            and self._coordinate_startup_mode == "fixed"
        ):
            detected_system = self._preferred_work_coordinate_system
        elif detected_system is None and self._position_reporting_mode != "machine":
            detected_system = self._preferred_work_coordinate_system
        self._active_work_coordinate_system = (
            None if self._position_reporting_mode == "machine" else detected_system
        )
        if self._position_reporting_mode != "machine":
            try:
                self._controller_coordinate_offsets = (
                    self._query_work_coordinate_offsets()
                )
            except StageControllerError as exc:
                logger.warning("Unable to load work coordinate offsets: %s", exc)

    def _emit_coordinate_system_status(self, status: _Status | None) -> None:
        if self._position_reporting_mode == "machine":
            self.status_message.emit("Coordinate system: machine coordinates.")
            return
        coordinate_system = self._active_work_coordinate_system
        if not coordinate_system:
            self.status_message.emit("Coordinate system: work coordinates.")
            return
        offset = None if status is None else status.work_offset
        if offset is not None and len(offset) >= 2:
            self.status_message.emit(
                f"Coordinate system: {coordinate_system} (X={offset[0]:.3f}, Y={offset[1]:.3f})."
            )
            return
        self.status_message.emit(f"Coordinate system: {coordinate_system}.")

    def _read_startup_limits(
        self, serial_connection, timeout: float = 3.5
    ) -> None:
        session = self._fluidnc_session_for(serial_connection)
        lines: list[str] = []
        for line in session.iter_command_response_lines(
            "$Startup/Show",
            timeout=timeout,
            source="$Startup/Show",
            reset_input_before_command=True,
            reset_input_reason="before $Startup/Show",
            handle_limit_lines=False,
        ):
            lines.append(line)
            if line.lower() == "ok":
                break
        limits = self._parse_startup_limits(lines)
        if limits:
            limits.pop("B", None)
            self._axis_limits.update(limits)

    def _ensure_axis_limits(
        self,
        serial_connection: serial.Serial,
        *,
        required_axes: tuple[str, ...] | list[str] | set[str] | None = None,
    ) -> None:
        normalized_required = [
            str(axis).strip().upper()
            for axis in (required_axes or ())
            if str(axis).strip().upper() in self.CONTROLLER_LIMIT_AXES
        ]
        missing_axes = [
            axis for axis in normalized_required if axis not in self._axis_limits
        ]
        if not self._axis_limits or missing_axes:
            logger.info(
                "Controller axis limits missing for %s; reading startup limits.",
                ", ".join(missing_axes) if missing_axes else "all axes",
            )
            self._read_startup_limits(serial_connection)
            missing_axes = [
                axis
                for axis in normalized_required
                if axis not in self._axis_limits
            ]
        if not self._axis_limits:
            raise StageControllerError("Axis limits unavailable from startup message.")
        if missing_axes:
            raise StageControllerError(
                "Axis limits unavailable for " + ", ".join(missing_axes) + "."
            )

    @staticmethod
    def _parse_startup_limits(lines: list[str]) -> dict[str, tuple[float, float]]:
        return parse_startup_axis_limits(lines)
