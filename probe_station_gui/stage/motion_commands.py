"""Structured motion command workflow for the stage controller."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Callable, Optional

import numpy as np
import serial

from probe_station_gui.stage.errors import StageControllerError
from probe_station_gui.stage.jog_commands import format_gcode_value
from probe_station_gui.stage.motion_command_planning import (
    absolute_axis_g1_command,
    absolute_axis_target_limit_error,
    clamped_motion_feedrate,
    ordered_absolute_axis_targets,
)
from probe_station_gui.stage.motion_timing import (
    absolute_move_distance_for_timeout,
    idle_timeout_for_distance,
    move_distance_for_timeout,
)
from probe_station_gui.stage.types import MoveVector, _Status


class StageControllerMotionCommandsMixin:
    """Translate high-level motion intents into FluidNC move commands."""

    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    def request_rotate_b(self, delta_deg: float) -> None:
        """Rotate the B axis by a relative angle in the background."""

        self._start_background_task(
            target=self._run_rotate_b,
            args=(float(delta_deg),),
            busy_message="Stage is busy. Ignoring B rotation request.",
        )

    def request_manual_axis_move(
        self,
        axis: str,
        distance_mm: float,
        mode: str,
        feedrate: float | None = None,
        allow_unhomed: bool = False,
    ) -> bool:
        """Move an arbitrary axis from the manual jog controls.

        G91 is accepted as a UI-relative input mode, but it is resolved to an
        absolute G90 target before anything is sent to the controller.
        """

        axis = axis.upper().strip()
        if axis not in self.AXIS_INDEX:
            self.status_message.emit(f"Unsupported axis: {axis}")
            return False
        mode = mode.upper().strip()
        if mode not in {"G90", "G91"}:
            self.status_message.emit(f"Unsupported manual move mode: {mode}")
            return False
        try:
            effective_feedrate = (
                None if feedrate is None else max(self.MIN_FEEDRATE, float(feedrate))
            )
        except (TypeError, ValueError):
            self.status_message.emit(f"Unsupported manual feedrate: {feedrate}")
            return False
        return self._start_background_task(
            target=self._run_manual_axis_move,
            args=(
                axis,
                float(distance_mm),
                mode,
                effective_feedrate,
                bool(allow_unhomed),
            ),
            busy_message="Stage is busy. Ignoring manual axis move.",
        )

    def request_absolute_axis_move(
        self,
        axis: str,
        target_mm: float,
        feedrate: float | None = None,
        allow_unhomed: bool = True,
    ) -> bool:
        """Move one axis to an absolute coordinate in the configured report mode."""

        return self.request_absolute_axis_targets_move(
            {axis: target_mm},
            feedrate=feedrate,
            allow_unhomed=allow_unhomed,
        )

    def request_absolute_axis_targets_move(
        self,
        targets: dict[str, float],
        *,
        feedrate: float | None = None,
        allow_unhomed: bool = True,
    ) -> bool:
        """Move multiple axes to absolute coordinates in one controller command."""

        normalized: dict[str, float] = {}
        for raw_axis, raw_value in targets.items():
            axis = str(raw_axis).upper().strip()
            if axis not in self.AXIS_INDEX:
                self.status_message.emit(f"Unsupported axis: {axis}")
                return False
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                self.status_message.emit(f"Unsupported target for {axis}: {raw_value}")
                return False
            if not math.isfinite(value):
                self.status_message.emit(f"Unsupported target for {axis}: {raw_value}")
                return False
            normalized[axis] = value
        if not normalized:
            self.status_message.emit("No coordinate targets provided.")
            return False
        try:
            effective_feedrate = (
                None if feedrate is None else max(self.MIN_FEEDRATE, float(feedrate))
            )
        except (TypeError, ValueError):
            self.status_message.emit(f"Unsupported manual feedrate: {feedrate}")
            return False
        return self._start_background_task(
            target=self._run_absolute_axis_targets_move,
            args=(dict(normalized), effective_feedrate, bool(allow_unhomed)),
            busy_message="Stage is busy. Ignoring coordinate move.",
        )

    def request_oscillation(
        self, mode: str, amplitude_mm: float, feedrate: float, turns_per_sweep: float = 3.0
    ) -> None:
        """Start one of the repeated motion patterns."""

        mode_key = mode.upper().strip()
        self._start_background_task(
            target=self._run_oscillation,
            args=(
                mode_key,
                float(amplitude_mm),
                float(feedrate),
                float(turns_per_sweep),
            ),
            busy_message="Stage is busy. Ignoring oscillation request.",
        )

    def request_stop_oscillation(self) -> None:
        """Stop the active oscillation task if one is running."""

        if not self._oscillation_active:
            return
        self._cancel_event.set()
        self.status_message.emit("Oscillation stop requested.")

    def run_external_move_to_xy(
        self,
        target_x_mm: float,
        target_y_mm: float,
        *,
        feedrate: float | None = None,
    ) -> str:
        """Run a blocking X/Y move inside an external controller reservation."""

        self.movement_started.emit()
        try:
            self._check_cancelled()
            with self._serial_session():
                self._move_safety_check()
                message = self._move_to_xy_locked(
                    float(target_x_mm),
                    float(target_y_mm),
                    feedrate=feedrate,
                )
            self.movement_finished.emit(True, message)
            return message
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
            raise

    def run_external_absolute_axis_targets_move(
        self,
        targets: dict[str, float],
        *,
        feedrate: float | None = None,
        allow_unhomed: bool = False,
    ) -> str:
        """Run a blocking absolute coordinate move inside an external reservation."""

        normalized_targets: dict[str, float] = {}
        for raw_axis, raw_value in targets.items():
            axis = str(raw_axis).upper().strip()
            if axis not in self.AXIS_INDEX:
                raise StageControllerError(f"Unsupported axis: {raw_axis}")
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise StageControllerError(
                    f"Unsupported target for {axis}: {raw_value}"
                ) from exc
            if not math.isfinite(value):
                raise StageControllerError(
                    f"Unsupported target for {axis}: {raw_value}"
                )
            normalized_targets[axis] = value
        ordered_targets = {
            axis: normalized_targets[axis]
            for axis in self.AXIS_INDEX
            if axis in normalized_targets
        }
        if not ordered_targets:
            raise StageControllerError("No coordinate targets provided.")

        self.movement_started.emit()
        try:
            self._check_cancelled()
            feedrate_text = (
                self.DEFAULT_FEEDRATE
                if feedrate is None
                else max(self.MIN_FEEDRATE, float(feedrate))
            )
            target_text = " ".join(
                f"{axis}{value:+.3f}" for axis, value in ordered_targets.items()
            )
            self.status_message.emit(
                "Coordinate move (G90): "
                f"{target_text} F{self._format_gcode_value(feedrate_text)}."
            )
            with self._serial_session() as serial_connection:
                self._execute_precision_axis_targets_locked(
                    ordered_targets,
                    feedrate=feedrate,
                    allow_unhomed=allow_unhomed,
                )
                self._query_status(serial_connection)
            message = f"Coordinate move complete (G90 {target_text})."
            self.movement_finished.emit(True, message)
            return message
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
            raise

    def zero_b_axis(self) -> None:
        """Set the current B coordinate as the application zero reference."""

        with self._task_lock:
            active_thread = getattr(self, "_active_thread", None)
            if active_thread and active_thread.is_alive():
                raise StageControllerError(
                    "Stage is busy. Wait for the current operation to finish."
                )
            with self._serial_session():
                status = self._query_current_status_with_required_coordinates(
                    axes=("B",),
                )
        if status is None or self._axis_value_for_configured_mode(status, "B") is None:
            raise StageControllerError("Unable to read B axis position.")
        self._set_b_axis_zero_reference(status)

    def _run_rotate_b(self, delta_deg: float) -> None:
        self.movement_started.emit()
        try:
            self._check_cancelled()
            with self._serial_session():
                if abs(delta_deg) < 1e-3:
                    self.movement_finished.emit(True, "Chip is already aligned.")
                    return
                status = self._query_current_status_with_required_coordinates(
                    axes=("B",),
                )
                current_b = self._axis_value_for_configured_mode(status, "B")
                if current_b is None:
                    raise StageControllerError("Unable to read B axis position.")
                self.status_message.emit(f"Chip alignment: rotating B by {delta_deg:+.3f} deg.")
                self._execute_precision_axis_targets_locked(
                    {"B": float(current_b) + float(delta_deg)},
                    feedrate=None,
                    allow_unhomed=True,
                    before_first_segment=self.b_rotation_started.emit,
                )
            self.movement_finished.emit(
                True,
                f"Chip alignment rotation complete (B {delta_deg:+.3f} deg).",
            )
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
        finally:
            with self._task_lock:
                setattr(self, "_active_thread", None)

    def _run_manual_axis_move(
        self,
        axis: str,
        distance_mm: float,
        mode: str,
        feedrate: float | None,
        allow_unhomed: bool = False,
    ) -> None:
        self.movement_started.emit()
        try:
            self._check_cancelled()
            with self._serial_session():
                if mode == "G91" and abs(distance_mm) < 1e-6:
                    self.movement_finished.emit(True, "Manual axis move skipped.")
                    return
                feedrate_text = (
                    self.DEFAULT_FEEDRATE if feedrate is None else max(self.MIN_FEEDRATE, float(feedrate))
                )
                target_value = self._manual_axis_absolute_target(
                    axis,
                    distance_mm,
                    mode,
                )
                mode_label = "relative" if mode == "G91" else "absolute"
                self.status_message.emit(
                    f"Manual axis move ({mode_label}->G90): "
                    f"{axis}{target_value:+.3f} "
                    f"F{self._format_gcode_value(feedrate_text)}."
                )
                self._execute_precision_axis_targets_locked(
                    {axis: target_value},
                    feedrate=feedrate,
                    allow_unhomed=allow_unhomed or mode == "G91",
                    wait_for_completion=False,
                )
            self.movement_finished.emit(
                True,
                "Manual axis move accepted "
                f"({mode_label}->G90 {axis}{target_value:+.3f}).",
            )
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
        finally:
            with self._task_lock:
                setattr(self, "_active_thread", None)

    def _run_absolute_axis_targets_move(
        self,
        targets: dict[str, float],
        feedrate: float | None,
        allow_unhomed: bool = True,
    ) -> None:
        self.movement_started.emit()
        try:
            self._check_cancelled()
            with self._serial_session():
                feedrate_text = (
                    self.DEFAULT_FEEDRATE if feedrate is None else max(self.MIN_FEEDRATE, float(feedrate))
                )
                ordered_targets = {
                    axis: float(targets[axis])
                    for axis in self.AXIS_INDEX
                    if axis in targets
                }
                target_text = " ".join(
                    f"{axis}{value:+.3f}" for axis, value in ordered_targets.items()
                )
                self.status_message.emit(
                    "Coordinate move (G90): "
                    f"{target_text} F{self._format_gcode_value(feedrate_text)}."
                )
                self._execute_precision_axis_targets_locked(
                    ordered_targets,
                    feedrate=feedrate,
                    allow_unhomed=allow_unhomed,
                )
            self.movement_finished.emit(
                True,
                f"Coordinate move complete (G90 {target_text}).",
            )
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
        finally:
            with self._task_lock:
                setattr(self, "_active_thread", None)

    def _manual_axis_absolute_target(
        self,
        axis: str,
        value_mm: float,
        mode: str,
    ) -> float:
        axis = axis.upper().strip()
        mode = mode.upper().strip()
        if mode == "G90":
            return float(value_mm)
        if mode != "G91":
            raise StageControllerError(f"Unsupported manual move mode: {mode}")

        current_value = self._current_axis_value_for_manual_move(
            axis,
        )
        if current_value is None:
            raise StageControllerError(
                f"Unable to read {axis} position for relative manual move."
            )
        return float(current_value) + float(value_mm)

    def _current_axis_value_for_manual_move(
        self,
        axis: str,
    ) -> float | None:
        status = self._query_current_status_with_required_coordinates(axes=(axis,))
        current_value = self._axis_value_for_configured_mode(status, axis)
        if current_value is not None:
            return current_value
        index = self.AXIS_INDEX.get(axis.upper().strip())
        if (
            index is not None
            and self._last_stage_position is not None
            and index < len(self._last_stage_position)
        ):
            return float(self._last_stage_position[index])
        return None

    def _run_oscillation(
        self, mode: str, amplitude_mm: float, feedrate: float, turns_per_sweep: float
    ) -> None:
        serial_connection: serial.Serial | None = None
        try:
            serial_connection = self._require_open_serial()
            if mode not in {"X", "Y", "SPIRAL"}:
                raise StageControllerError(
                    f"Oscillation mode {mode} is not supported."
                )
            if not (
                self.OSCILLATION_MIN_AMPLITUDE_MM
                <= amplitude_mm
                <= self.OSCILLATION_MAX_AMPLITUDE_MM
            ):
                raise StageControllerError(
                    f"Oscillation amplitude must be between "
                    f"{self.OSCILLATION_MIN_AMPLITUDE_MM:.3f} and "
                    f"{self.OSCILLATION_MAX_AMPLITUDE_MM:.3f} mm."
                )
            if not (self.OSCILLATION_MIN_FEEDRATE <= feedrate <= self.OSCILLATION_MAX_FEEDRATE):
                raise StageControllerError(
                    f"Oscillation feedrate must be between "
                    f"{self.OSCILLATION_MIN_FEEDRATE:.1f} and "
                    f"{self.OSCILLATION_MAX_FEEDRATE:.1f} mm/min."
                )
            if mode == "SPIRAL" and not (
                self.SPIRAL_MIN_TURNS_PER_SWEEP
                <= turns_per_sweep
                <= self.SPIRAL_MAX_TURNS_PER_SWEEP
            ):
                raise StageControllerError(
                    f"Spiral turns per sweep must be between "
                    f"{self.SPIRAL_MIN_TURNS_PER_SWEEP:.2f} and "
                    f"{self.SPIRAL_MAX_TURNS_PER_SWEEP:.2f}."
                )
            with self._serial_session():
                self._oscillation_active = True
                self.oscillation_state_changed.emit(True, mode)
                self.status_message.emit(
                    f"Oscillation started in {mode}: amplitude={amplitude_mm:.3f} mm, "
                    f"feedrate={feedrate:.1f} mm/min."
                )
                self._write_current_command_and_wait("G21")
                self._write_current_command_and_wait("G91")
                if mode == "SPIRAL":
                    self._run_spiral_pattern(
                        amplitude_mm=amplitude_mm,
                        feedrate=feedrate,
                        turns_per_sweep=turns_per_sweep,
                    )
                else:
                    self._run_linear_pattern(
                        axis=mode,
                        amplitude_mm=amplitude_mm,
                        feedrate=feedrate,
                    )
                self._write_current_command_and_wait("G90")
                self.status_message.emit("Oscillation stopped.")
        except StageControllerError as exc:
            try:
                if serial_connection is not None and serial_connection.is_open:
                    with self._serial_session_lock:
                        self._cancel_event.clear()
                        self._write_command(serial_connection, "G90")
                        self._wait_for_ok(serial_connection)
            except StageControllerError:
                pass
            with self._task_lock:
                has_queued_needles_action = bool(self._queued_needles_actions)
            if not (
                str(exc) == "Operation cancelled." and has_queued_needles_action
            ):
                self.status_message.emit(str(exc))
        finally:
            self._oscillation_active = False
            self.oscillation_state_changed.emit(False, mode)
            with self._task_lock:
                setattr(self, "_active_thread", None)
            self._start_next_queued_needles_action()

    def _send_relative_move(
        self,
        move: MoveVector,
        *,
        allow_relative: bool = False,
        ignore_needle_safety: bool = False,
        feedrate: Optional[float] = None,
        wait_for_completion: bool = True,
        motion_started_callback: Optional[Callable[[MoveVector, float], None]] = None,
        as_jog: bool = False,
    ) -> None:
        if move.is_zero():
            return
        if not ignore_needle_safety:
            self._move_safety_check()
        if not self._motion_safety_disabled:
            self._ensure_axis_limits(
                required_axes=tuple(
                    axis for axis, delta in move.items() if abs(delta) >= 1e-6
                ),
            )
            self._check_relative_move_limits(move, allow_relative=allow_relative)
        move_parts: list[str] = [
            f"{axis}{self._format_gcode_value(value, decimals=6)}"
            for axis, value in move.items()
            if abs(value) >= 1e-6
        ]
        if not move_parts:
            return
        effective_feedrate = (
            self.DEFAULT_FEEDRATE if feedrate is None else max(self.MIN_FEEDRATE, float(feedrate))
        )
        move_distance = self._move_distance_for_timeout(move)
        command = (
            "G1 "
            + " ".join(move_parts)
            + f" F{self._format_gcode_value(effective_feedrate)}"
        )
        self._reset_feed_override()
        if as_jog:
            command = (
                "$J=G91 G21 "
                + " ".join(move_parts)
                + f" F{self._format_gcode_value(effective_feedrate)}"
            )
            self._write_current_command_and_wait(command)
            if motion_started_callback is not None:
                motion_started_callback(move, effective_feedrate)
            if wait_for_completion:
                self._wait_for_idle(
                    timeout=self._idle_timeout_for_distance(
                        move_distance, effective_feedrate
                    ),
                )
            return
        self._write_current_command_and_wait("G21")
        self._write_current_command_and_wait("G91")
        self._write_current_command_and_wait(command)
        if motion_started_callback is not None:
            motion_started_callback(move, effective_feedrate)
        self._write_current_command_and_wait("G90")
        if wait_for_completion:
            self._wait_for_idle(
                timeout=self._idle_timeout_for_distance(
                    move_distance, effective_feedrate
                ),
            )

    def _send_absolute_axis_targets_move(
        self,
        targets: dict[str, float],
        *,
        ignore_needle_safety: bool = False,
        feedrate: Optional[float] = None,
        wait_for_completion: bool = True,
        allow_unhomed: bool = False,
        as_jog: bool = False,
        motion_started_callback: Callable[[], None] | None = None,
    ) -> None:
        ordered_targets = ordered_absolute_axis_targets(
            targets,
            axis_order=self.AXIS_INDEX,
        )
        if not ordered_targets:
            return
        serial_connection = self._current_serial()
        if not ignore_needle_safety:
            self._move_safety_check()
        current_values: dict[str, float] = {}
        if not self._motion_safety_disabled:
            self._ensure_axis_limits(required_axes=tuple(ordered_targets))
            status = self._query_status_with_required_coordinates(
                serial_connection,
                axes=tuple(ordered_targets),
            )
            if status is None:
                raise StageControllerError("Unable to read position for absolute move.")
            self._require_homed_axes(
                status,
                set(ordered_targets),
                allow_relative=allow_unhomed,
            )
            for axis, value in ordered_targets.items():
                current_value = self._axis_value_for_configured_mode(status, axis)
                if current_value is not None:
                    current_values[axis] = float(current_value)
                limits = self._axis_limits_for_configured_mode(axis, status)
                if limits and self._axis_software_limit_ready(status, axis):
                    error = absolute_axis_target_limit_error(axis, value, limits)
                    if error is not None:
                        raise StageControllerError(error)
        effective_feedrate = clamped_motion_feedrate(
            feedrate,
            default_feedrate=self.DEFAULT_FEEDRATE,
            min_feedrate=self.MIN_FEEDRATE,
        )
        if as_jog:
            self._write_current_command_and_wait(
                self._absolute_axis_targets_jog_command(
                    ordered_targets,
                    effective_feedrate,
                ),
            )
            if motion_started_callback is not None:
                motion_started_callback()
            if wait_for_completion:
                move_distance = self._absolute_move_distance_for_timeout(
                    ordered_targets,
                    current_values,
                )
                self._wait_for_idle_at_targets(
                    ordered_targets,
                    timeout=self._idle_timeout_for_distance(
                        move_distance, effective_feedrate
                    ),
                )
            return
        self._write_current_command_and_wait("G21")
        self._write_current_command_and_wait("G90")
        self._reset_feed_override()
        self._write_current_command_and_wait(
            absolute_axis_g1_command(ordered_targets, effective_feedrate),
        )
        if motion_started_callback is not None:
            motion_started_callback()
        if wait_for_completion:
            move_distance = self._absolute_move_distance_for_timeout(
                ordered_targets,
                current_values,
            )
            self._wait_for_idle(
                timeout=self._idle_timeout_for_distance(
                    move_distance, effective_feedrate
                ),
            )

    def _validate_absolute_axis_targets_move(
        self,
        targets: dict[str, float],
        *,
        allow_unhomed: bool,
        status: _Status | None = None,
    ) -> None:
        """Validate a target map without sending controller commands."""

        ordered_targets = ordered_absolute_axis_targets(
            targets,
            axis_order=self.AXIS_INDEX,
        )
        if not ordered_targets or self._motion_safety_disabled:
            return
        self._ensure_axis_limits(required_axes=tuple(ordered_targets))
        if status is None:
            status = self._query_current_status_with_required_coordinates(
                axes=tuple(ordered_targets),
            )
        if status is None:
            raise StageControllerError("Unable to read position for absolute move.")
        self._require_homed_axes(
            status,
            set(ordered_targets),
            allow_relative=allow_unhomed,
        )
        for axis, value in ordered_targets.items():
            limits = self._axis_limits_for_configured_mode(axis, status)
            if limits and self._axis_software_limit_ready(status, axis):
                error = absolute_axis_target_limit_error(axis, value, limits)
                if error is not None:
                    raise StageControllerError(error)

    def _send_absolute_axis_move(
        self,
        axis: str,
        value: float,
        *,
        ignore_needle_safety: bool = False,
        feedrate: Optional[float] = None,
        wait_for_completion: bool = True,
        allow_unhomed: bool = False,
        as_jog: bool = False,
    ) -> None:
        axis = axis.upper().strip()
        self._send_absolute_axis_targets_move(
            {axis: float(value)},
            ignore_needle_safety=ignore_needle_safety,
            feedrate=feedrate,
            wait_for_completion=wait_for_completion,
            allow_unhomed=allow_unhomed,
            as_jog=as_jog,
        )

    @staticmethod
    def _absolute_move_distance_for_timeout(
        targets: dict[str, float],
        current_values: dict[str, float],
    ) -> float:
        return absolute_move_distance_for_timeout(targets, current_values)

    @staticmethod
    def _move_distance_for_timeout(move: MoveVector) -> float:
        return move_distance_for_timeout(move)

    @staticmethod
    def _format_gcode_value(value: float, decimals: int = 3) -> str:
        return format_gcode_value(value, decimals)

    def _idle_timeout_for_distance(self, distance: float, feedrate: float) -> float:
        """Return an idle wait timeout long enough for slow manual G1 moves."""

        return idle_timeout_for_distance(
            distance,
            feedrate,
            min_feedrate=self.MIN_FEEDRATE,
            margin_s=self.MOVE_IDLE_TIMEOUT_MARGIN_S,
            min_timeout_s=self.MOVE_IDLE_TIMEOUT_MIN_S,
            max_timeout_s=self.MOVE_IDLE_TIMEOUT_MAX_S,
        )

    def _write_relative_g1_unchecked(
        self,
        move: MoveVector,
        *,
        feedrate: Optional[float] = None,
    ) -> None:
        """Send a single relative G1 move assuming the controller is already in G91."""

        if move.is_zero():
            return
        move_parts: list[str] = [
            f"{axis}{self._format_gcode_value(value, decimals=6)}"
            for axis, value in move.items()
            if abs(value) >= 1e-6
        ]
        if not move_parts:
            return
        effective_feedrate = (
            self.DEFAULT_FEEDRATE if feedrate is None else max(self.MIN_FEEDRATE, float(feedrate))
        )
        self._write_current_command_and_wait(
            "G1 "
            + " ".join(move_parts)
            + f" F{self._format_gcode_value(effective_feedrate)}",
        )

    def _check_relative_move_limits(
        self,
        move: MoveVector,
        *,
        allow_relative: bool = False,
    ) -> None:
        if not self._axis_limits and abs(move.b) < 1e-6:
            return
        moved_limited_axes = tuple(
            axis
            for axis, delta in move.items()
            if abs(delta) >= 1e-6
            and (axis == "B" or axis in self._axis_limits)
        )
        status = self._query_current_status_with_required_coordinates(
            axes=moved_limited_axes,
        )
        positions = self._position_for_configured_mode(status)
        if status is None or not positions:
            return
        self._ensure_b_axis_zero_reference(status)
        for axis, delta in move.items():
            if abs(delta) < 1e-6:
                continue
            idx = self.AXIS_INDEX.get(axis)
            if idx is None or idx >= len(positions):
                continue
            if axis == "B":
                current_b = self._relative_b_position(status)
                limit = self.B_AXIS_SOFT_LIMIT_DEG
                target_b = current_b + delta
                if target_b < -limit or target_b > limit:
                    raise StageControllerError(
                        f"B move {delta:+.3f} exceeds software limit ({-limit:.3f}, {limit:.3f}) relative to B zero."
                    )
                continue
            limits = self._axis_limits_for_configured_mode(axis, status)
            if not limits:
                continue
            if not self._axis_software_limit_ready(status, axis):
                continue
            self._require_homed_axes(
                status, {axis}, allow_relative=allow_relative
            )
            min_value, max_value = limits
            target = positions[idx] + delta
            if target < min_value or target > max_value:
                raise StageControllerError(
                    f"{axis} move {delta:+.3f} exceeds limits ({min_value:.3f}, {max_value:.3f})."
                )

    def _require_position_for_absolute_motion(
        self, status: _Status, *, required_axes: int
    ) -> tuple[float, ...]:
        position = self._position_for_configured_mode(status)
        if position is None or len(position) < required_axes:
            raise StageControllerError(
                "Controller did not report a complete position for absolute motion."
            )
        return tuple(float(value) for value in position[:required_axes])

    def _ensure_b_axis_zero_reference(self, status: _Status) -> None:
        if self._b_axis_zero_position is not None:
            return
        if self._axis_value_for_configured_mode(status, "B") is None:
            return
        self._set_b_axis_zero_reference(status, emit_status=False)

    def _set_b_axis_zero_reference(
        self, status: _Status, *, emit_status: bool = True
    ) -> None:
        b_position = self._axis_value_for_configured_mode(status, "B")
        if b_position is None:
            raise StageControllerError("B axis position unavailable.")
        self._b_axis_zero_position = b_position
        if emit_status:
            self.status_message.emit(
                f"B zero reference set to current position ({self._b_axis_zero_position:.3f})."
            )

    def _relative_b_position(self, status: _Status) -> float:
        b_position = self._axis_value_for_configured_mode(status, "B")
        if b_position is None:
            raise StageControllerError("B axis position unavailable.")
        self._ensure_b_axis_zero_reference(status)
        zero = self._b_axis_zero_position
        if zero is None:
            raise StageControllerError("B zero reference is not initialized.")
        return b_position - zero

    def _move_vector_for_axis(self, axis: str, delta: float) -> MoveVector:
        """Create a single-axis move vector."""

        if axis == "X":
            return MoveVector(x=delta)
        if axis == "Y":
            return MoveVector(y=delta)
        if axis == "Z":
            return MoveVector(z=delta)
        if axis == "A":
            return MoveVector(a=delta)
        if axis == "B":
            return MoveVector(b=delta)
        if axis == "C":
            return MoveVector(c=delta)
        raise StageControllerError(f"Unsupported axis: {axis}")

    def _run_linear_pattern(
        self,
        *,
        axis: str,
        amplitude_mm: float,
        feedrate: float,
    ) -> None:
        """Run endless edge-to-edge motion along a single axis."""

        current_offset = 0.0
        target_offset = amplitude_mm
        direction = 1.0
        segment_length = max(
            0.001, amplitude_mm / float(self.LINEAR_SEGMENTS_PER_SWEEP)
        )
        while not self._cancel_event.is_set():
            self._check_cancelled()
            self._apply_pending_oscillation_needles_actions()
            remaining = target_offset - current_offset
            if abs(remaining) < 1e-6:
                direction *= -1.0
                target_offset = amplitude_mm * direction
                continue
            step = float(np.sign(remaining)) * min(abs(remaining), segment_length)
            self._write_relative_g1_unchecked(
                self._move_vector_for_axis(axis, step),
                feedrate=feedrate,
            )
            current_offset += step

    def _run_spiral_pattern(
        self,
        *,
        amplitude_mm: float,
        feedrate: float,
        turns_per_sweep: float,
    ) -> None:
        """Run a smooth forward-winding spiral around the current point."""

        phase = 0.0
        phase_step = (2.0 * np.pi) / float(self.SPIRAL_SEGMENTS_PER_TURN)
        start_angle = np.pi / 2.0
        last_x = 0.0
        last_y = 0.0
        while not self._cancel_event.is_set():
            self._check_cancelled()
            self._apply_pending_oscillation_needles_actions()
            phase += phase_step
            radius = amplitude_mm * 0.5 * (1.0 - float(np.cos(phase)))
            angle = start_angle + (2.0 * turns_per_sweep * phase)
            next_x = radius * float(np.cos(angle))
            next_y = radius * float(np.sin(angle))
            self._write_relative_g1_unchecked(
                MoveVector(x=next_x - last_x, y=next_y - last_y),
                feedrate=feedrate,
            )
            last_x = next_x
            last_y = next_y
