"""Homing and startup synchronization workflow for the stage controller."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from probe_station_gui.stage.errors import StageControllerError


logger = logging.getLogger(__name__)


class StageControllerHomingStartupMixin:
    """Run FluidNC startup sync and homing tasks for the stage controller."""

    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    def request_startup_sync(
        self, *, auto_home_a: bool = True, clear_unverified_state: bool = False
    ) -> None:
        """Load controller state after connect and optionally home A."""

        self._start_background_task(
            target=self._run_startup_sync,
            args=(bool(auto_home_a),),
            busy_message="Stage is busy. Skipping startup sync.",
            before_create=(
                self._clear_unverified_controller_state_locked
                if clear_unverified_state
                else None
            ),
        )

    def request_home_axis(self, axis: str) -> bool:
        """Home a specific axis via a background task."""

        axis = axis.upper().strip()
        if not axis:
            return False
        return self._start_background_task(
            target=self._run_home,
            args=(f"$H{axis}", axis),
            busy_message="Stage is busy. Ignoring home request.",
            before_start=lambda: self.homing_action_started.emit(axis),
        )

    def request_home_all(self) -> bool:
        """Home all axes via a background task."""

        return self._start_background_task(
            target=self._run_home,
            args=("$H", "ALL"),
            busy_message="Stage is busy. Ignoring home request.",
            before_start=lambda: self.homing_action_started.emit("ALL"),
        )

    def _run_home(self, command: str, axis_key: str) -> None:
        self.movement_started.emit()
        try:
            with self._serial_session():
                self._perform_home_command(command)
            self.invalidate_coordinate_confidence(
                "Homing resets coordinate confidence.",
                axes=(self.AXIS_INDEX if axis_key == "ALL" else (axis_key,)),
            )
            self.movement_finished.emit(True, "Homing complete.")
            self.homing_action_finished.emit(True, "Homing complete.", axis_key)
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
            self.homing_action_finished.emit(False, str(exc), axis_key)

    def _run_startup_sync(self, auto_home_a: bool) -> None:
        success = False
        try:
            serial_connection = self._require_open_serial()

            self.status_message.emit("Loading controller startup state...")
            with self._serial_session(serial_connection):
                axis_feedrates = dict(self._axis_max_feedrates)
                if axis_feedrates:
                    logger.info(
                        "Using cached controller axis max feedrates for unchanged controller session."
                    )
                else:
                    try:
                        axis_feedrates = self._query_axis_max_feedrates_locked()
                    except StageControllerError as exc:
                        axis_feedrates = {}
                        logger.warning(
                            "Unable to read axis max feedrates from controller: %s",
                            exc,
                        )
                if axis_feedrates:
                    self.apply_axis_max_feedrates(axis_feedrates)
                    self.axis_max_feedrates_changed.emit(dict(axis_feedrates))
                self._ensure_axis_limits(required_axes=self.CONTROLLER_LIMIT_AXES)
                self._refresh_coordinate_system_state(apply_preference=True)
                status = self._query_status(serial_connection)
            if status is None:
                raise StageControllerError("Unable to read startup controller status.")

            self.status_message.emit(
                "Axis limits loaded from controller. B uses app soft limit ±45 deg."
            )

            self._emit_coordinate_system_status(status)
            effective_homed = status.homed_axes
            if effective_homed is None and self._homed_axes:
                effective_homed = set(self._homed_axes)
            if effective_homed:
                ordered = ", ".join(sorted(effective_homed))
                self.status_message.emit(f"Homed axes: {ordered}.")
            else:
                self.status_message.emit("Controller did not report any homed axes.")

            self._controller_state_stale = False
            self._refresh_axis_a_ready_from_state()

            if auto_home_a and (effective_homed is None or "A" not in effective_homed):
                self.movement_started.emit()
                self.homing_action_started.emit("A")
                try:
                    self.status_message.emit(
                        "A axis not homed. Homing needles on startup."
                    )
                    with self._serial_session(serial_connection):
                        self._perform_home_command("$HA")
                    self.movement_finished.emit(True, "Startup A homing complete.")
                    self.homing_action_finished.emit(
                        True, "Startup A homing complete.", "A"
                    )
                except StageControllerError as exc:
                    self.movement_finished.emit(False, str(exc))
                    self.homing_action_finished.emit(False, str(exc), "A")
                    raise
            with self._serial_session(serial_connection):
                self._ensure_controller_session_marker()
            if self._last_stage_position is not None:
                self._publish_cached_stage_position(tuple(self._last_stage_position))
            success = True
        except StageControllerError as exc:
            self.status_message.emit(str(exc))
        finally:
            with self._state_lock:
                if success:
                    self._controller_reboot_recovery_pending = False
                    self._controller_reboot_ready_notified = False

    def _perform_home_command(self, command: str) -> None:
        """Execute a homing command using the current serial session."""

        self.status_message.emit(f"Homing: {command}")
        self._write_current_command_and_wait(command, timeout=30.0)
        self._wait_for_idle(timeout=30.0)
        if command.upper() in ("$H", "$HA"):
            axes = set(self._homed_axes)
            if command.upper() == "$H":
                axes.update({"X", "Y", "Z", "A"})
            else:
                axes.add("A")
            self._update_homing_status(axes)
            self._update_limit_axes(self._limit_axes.difference(axes))
            self._controller_state_stale = False
            self._set_needles_state(True, known=True, zone="raise")
            if self._controller_session_marker is None:
                self._ensure_controller_session_marker()
