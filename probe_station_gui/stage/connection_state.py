"""Connection, controller-session, and reboot state for stage control."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Optional

import serial

from probe_station_gui.stage.errors import StageControllerError
from probe_station_gui.stage.fluidnc_session import (
    FluidNCSession,
    FluidNCSessionCallbacks,
)
from probe_station_gui.stage.fluidnc_protocol import line_indicates_controller_reboot


logger = logging.getLogger(__name__)


class StageControllerConnectionMixin:
    """Internal connection and cached-controller-state methods for StageController."""

    def _require_open_serial(self) -> serial.Serial:
        serial_connection = self._serial
        if serial_connection is None or not serial_connection.is_open:
            raise StageControllerError("Serial connection is not available.")
        return serial_connection

    @contextmanager
    def _serial_session(
        self, serial_connection: Optional[serial.Serial] = None
    ) -> Iterator[serial.Serial]:
        if serial_connection is None:
            serial_connection = self._require_open_serial()
        elif not serial_connection.is_open:
            raise StageControllerError("Serial connection is not available.")
        with self._serial_session_lock:
            stack = getattr(self._serial_session_state, "serial_stack", None)
            if stack is None:
                stack = []
                self._serial_session_state.serial_stack = stack
            stack.append(serial_connection)
            try:
                yield serial_connection
            finally:
                stack.pop()

    def _current_serial(self) -> serial.Serial:
        stack = getattr(self._serial_session_state, "serial_stack", ())
        if stack:
            serial_connection = stack[-1]
            if serial_connection is None or not serial_connection.is_open:
                raise StageControllerError("Serial connection is not available.")
            return serial_connection
        return self._require_open_serial()

    def _fluidnc_session_callbacks(self) -> FluidNCSessionCallbacks:
        return FluidNCSessionCallbacks(
            check_cancelled=self._check_cancelled,
            raise_if_controller_reboot_line=self._raise_if_controller_reboot_line,
            handle_limit_line=self._handle_limit_line,
            handle_homing_message_line=self._handle_homing_message_line,
            handle_coordinate_state_line=self._handle_coordinate_state_line,
            parse_status_line=self._parse_status_line,
            extract_status_homed_axes=self._status_homed_axes_from_line,
            update_homing_status=self._update_homing_status,
            handle_pending_serial_data_side_effects=(
                self._handle_pending_serial_data_side_effects
            ),
        )

    def _fluidnc_session_for(
        self, serial_connection: serial.Serial
    ) -> FluidNCSession:
        return FluidNCSession(
            serial_connection=serial_connection,
            callbacks=self._fluidnc_session_callbacks(),
        )

    @contextmanager
    def _fluidnc_session(self) -> Iterator[FluidNCSession]:
        with self._serial_session() as serial_connection:
            yield self._fluidnc_session_for(serial_connection)

    def _current_fluidnc_session(self) -> FluidNCSession:
        return self._fluidnc_session_for(self._current_serial())

    def set_serial(self, serial_connection: Optional[serial.Serial]) -> None:
        """Assign or clear the serial connection used for stage control."""

        with self._task_lock:
            self._serial = serial_connection
            self._queued_jog_generation += 1
            with self._feed_override_lock:
                self._active_feed_override_percent = None
            self._controller_reboot_recovery_pending = False
            self._controller_reboot_ready_notified = False
            self._clear_pending_async_writes()
            self._clear_unverified_controller_state_locked()
            if serial_connection is None or not serial_connection.is_open:
                self._pixels_to_mm = None
                self._axis_limits.clear()
                self._b_axis_zero_position = None
                self._refresh_axis_a_ready_from_state()
            else:
                self._axis_limits.clear()
                self._b_axis_zero_position = None
                self._controller_state_stale = True
                if bool(
                    getattr(serial_connection, "probe_station_reboot_detected", False)
                ):
                    logger.warning(
                        "Controller reboot banner detected on serial connect."
                    )
                self._refresh_axis_a_ready_from_state()

    def export_cached_controller_state(self) -> dict[str, object] | None:
        """Return controller state suitable for persistence across app restarts."""

        if (
            self._last_stage_position is None
            and not self._homed_axes
            and not self._needles_known
        ):
            return None
        return {
            "last_stage_position": (
                list(self._last_stage_position)
                if self._last_stage_position is not None
                else None
            ),
            "last_machine_position": (
                list(self._last_machine_position)
                if self._last_machine_position is not None
                else None
            ),
            "last_stage_state": self._last_stage_state,
            "active_work_coordinate_system": self._active_work_coordinate_system,
            "controller_coordinate_offsets": {
                system: list(values)
                for system, values in self._controller_coordinate_offsets.items()
            },
            "homed_axes": sorted(self._homed_axes),
            "needles_up": bool(self._needles_up),
            "needles_known": bool(self._needles_known),
            "needles_zone": self._needles_zone,
            "controller_session_marker": self._controller_session_marker,
            "axis_limits": {
                axis: [float(values[0]), float(values[1])]
                for axis, values in self._axis_limits.items()
            },
            "axis_max_feedrates": {
                axis: float(rate)
                for axis, rate in self._axis_max_feedrates.items()
            },
        }

    def import_cached_controller_state(self, data: dict[str, object]) -> None:
        """Restore homing state when the volatile controller marker matches.

        FluidNC does not report the full homed-axis set in regular status
        frames.  When the marker proves this is the same controller session,
        we can reuse cached homing flags, but positions still come only from
        the next live status query.
        """

        homed_raw = data.get("homed_axes")
        homed_axes: set[str] = set()
        if isinstance(homed_raw, (list, tuple)):
            for value in homed_raw:
                if isinstance(value, str) and value.strip():
                    homed_axes.add(value.strip().upper())
        needles_up = bool(data.get("needles_up", False))
        needles_known = bool(data.get("needles_known", False))
        needles_zone_raw = data.get("needles_zone")
        needles_zone = (
            needles_zone_raw.strip().lower()
            if isinstance(needles_zone_raw, str)
            else None
        )
        if needles_zone not in {"raise", "lift", "lower"}:
            needles_zone = None
        if needles_zone is not None:
            needles_up = needles_zone == "raise"
        marker = self._parse_cached_controller_session_marker(data)
        axis_limits = self._parse_cached_axis_limits(data.get("axis_limits"))
        axis_max_feedrates = self._parse_cached_axis_max_feedrates(
            data.get("axis_max_feedrates")
        )
        coordinate_system = data.get("active_work_coordinate_system")
        coordinate_offsets = self._parse_cached_coordinate_offsets(
            data.get("controller_coordinate_offsets")
        )

        self._controller_state_stale = True
        self._controller_session_marker = marker
        if isinstance(coordinate_system, str):
            normalized_system = coordinate_system.strip().upper()
            if normalized_system in self.WORK_COORDINATE_SYSTEMS:
                self._active_work_coordinate_system = normalized_system
        if coordinate_offsets:
            self._controller_coordinate_offsets = coordinate_offsets
        if axis_limits:
            self._axis_limits = axis_limits
        if axis_max_feedrates:
            self._axis_max_feedrates = axis_max_feedrates
        self._update_homing_status(homed_axes)
        self._set_needles_state(needles_up, known=needles_known, zone=needles_zone)
        self._refresh_axis_a_ready_from_state()
        logger.info(
            "Restored cached controller state pending live status: homed_axes=%s needles_up=%s needles_known=%s needles_zone=%s marker=%s axis_limits=%s axis_max_feedrates=%s coordinate_system=%s",
            sorted(homed_axes),
            needles_up,
            needles_known,
            needles_zone,
            marker,
            sorted(axis_limits),
            sorted(axis_max_feedrates),
            self._active_work_coordinate_system,
        )

    def cached_controller_session_is_current(self, data: dict[str, object]) -> bool:
        """Return True when the controller still has the cached volatile marker."""

        expected_marker = self._parse_cached_controller_session_marker(data)
        if expected_marker is None:
            logger.info("Cached controller state has no session marker; ignoring it.")
            return False
        serial_connection = self._serial
        if serial_connection is None or not serial_connection.is_open:
            return False
        attempts = max(1, int(self.CONTROLLER_SESSION_MARKER_READ_ATTEMPTS))
        current_marker = None
        used_attempts = 0
        try:
            with self._serial_session_lock:
                for attempt in range(1, attempts + 1):
                    used_attempts = attempt
                    current_marker = self._read_controller_session_marker()
                    if current_marker is not None:
                        break
                    if attempt < attempts:
                        logger.info(
                            "Controller session marker read returned no marker on attempt %s/%s; retrying.",
                            attempt,
                            attempts,
                        )
        except StageControllerError as exc:
            logger.info("Unable to verify cached controller session marker: %s", exc)
            return False
        matches = current_marker == expected_marker
        logger.info(
            "Controller session marker check: expected=%s current=%s matches=%s attempts=%s",
            expected_marker,
            current_marker,
            matches,
            used_attempts,
        )
        return matches

    def clear_cached_controller_state(self) -> None:
        """Forget locally cached controller state."""

        self._clear_unverified_controller_state_locked()

    def _clear_unverified_controller_state_locked(self) -> None:
        """Clear volatile state that must be verified from the live controller."""

        self._last_stage_position = None
        self._last_machine_position = None
        self._last_stage_state = None
        self._last_status_timestamp = None
        self._last_jog_write_timestamp = None
        self._jog_motion_active = False
        self._active_needles_action = None
        self._active_needles_programmed_feedrate = None
        with self._feed_override_lock:
            self._active_feed_override_percent = None
        self._current_status_report_mask = None
        self._controller_session_marker = None
        self._active_work_coordinate_system = None
        self._controller_coordinate_offsets.clear()
        self._axis_limits.clear()
        self._axis_max_feedrates.clear()
        self._controller_state_stale = True
        self._update_homing_status(set())
        self._update_limit_axes(set())
        self._set_needles_state(False, known=False)
        self.stage_position_changed.emit(None)

    def _handle_controller_reboot_detected(self, line: str, source: str) -> None:
        already_pending = self._controller_reboot_recovery_pending
        self._controller_reboot_recovery_pending = True
        self._controller_reboot_ready_notified = False
        self._queued_jog_generation += 1
        self._clear_pending_async_writes()
        self._clear_unverified_controller_state_locked()
        if already_pending:
            logger.debug(
                "Additional controller reboot/reset line from %s: %r",
                source,
                line,
            )
            return
        logger.warning(
            "Controller reboot/reset detected from %s serial output: %r",
            source,
            line,
        )
        self.controller_reboot_detected.emit()

    def _raise_if_controller_reboot_line(self, line: str, source: str) -> None:
        if not line_indicates_controller_reboot(line):
            return
        self._handle_controller_reboot_detected(line, source)
        raise StageControllerError(
            "Controller reboot detected. Cleared homing state."
        )

    def _handle_pending_serial_data_side_effects(
        self, data: bytes, source: str
    ) -> None:
        text = data.decode("ascii", errors="ignore")
        for line in text.splitlines():
            line = line.strip()
            self._handle_limit_line(line)
            if line_indicates_controller_reboot(line):
                self._handle_controller_reboot_detected(line, source)
                return
