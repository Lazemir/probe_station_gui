"""Jog command, projection, serial, and stop lifecycle for the joystick."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Optional, TYPE_CHECKING

import serial
from PySide6.QtCore import QTimer

if TYPE_CHECKING:
    from probe_station_gui.stage.controller import StageController


logger = logging.getLogger(__name__)

RelativeMotionProjector = Callable[
    [tuple[tuple[str, float], ...], object | None],
    object,
]


class JoystickJogRuntimeMixin:
    """Own command generation, serial dispatch, and active jog lifecycle."""

    def set_serial(self, serial_connection: Optional[serial.Serial]) -> None:
        """Assign the serial connection used for jogging commands."""

        if self.serial_connection and self.serial_connection.is_open:
            self.stop_jog()
        self._invalidate_jog_stop_resend()
        self.serial_connection = serial_connection
        if not serial_connection or not serial_connection.is_open:
            self._active_axes = None
            self._pending_jog_axes = None
            self._key_stack.clear()
            self._key_press_times.clear()
            self._clear_pending_key_activations()
            self._sync_physical_key_watchdog()
            logger.debug("Joystick serial detached")
        if serial_connection and serial_connection.is_open:
            self.status_label.setText(
                f"Connected to {serial_connection.port} @ {serial_connection.baudrate}"
            )
            logger.info(
                "Joystick connected to %s @ %s baud",
                serial_connection.port,
                serial_connection.baudrate,
            )
        else:
            self.status_label.setText("Disconnected")
            logger.info("Joystick disconnected from serial link")
            self._pending_homing_axes.clear()
            self._stop_homing_animation("ALL")
            for axis in self.HOMING_AXES:
                self._stop_homing_animation(axis)
        self._update_enabled_state()

    def _move_safety_check(self) -> bool:
        if self._motion_safety_disabled:
            if self.stage_controller is not None and self.stage_controller.is_busy():
                logger.debug("Jog blocked because stage controller is busy")
                return False
            return True
        if not self._axis_a_ready:
            logger.debug("Jog blocked because A axis is not homed/zero")
            return False
        if self.stage_controller is not None and self.stage_controller.is_busy():
            logger.debug("Jog blocked because stage controller is busy")
            return False
        return True

    def set_stage_controller(
        self, stage_controller: Optional["StageController"]
    ) -> None:
        self.stage_controller = stage_controller

    def set_relative_motion_projector(
        self,
        projector: RelativeMotionProjector | None,
    ) -> None:
        self._relative_motion_projector = projector
        self._active_jog_projection_lease = None

    def start_jog(self, axis: str, direction: int) -> None:
        logger.debug("TIMING start_jog_requested axis=%s direction=%s", axis, direction)
        if not self._move_safety_check():
            return
        if self._control_mode == self.MODE_STEP:
            self._manual_axis_step(axis, direction, mode="G91")
            return
        self.motion_axis_requested.emit(axis.upper())
        self._apply_axes(((axis, direction),))

    def _restart_active_jog_with_current_feedrate(self) -> None:
        axes = self._active_axes
        if not axes:
            return
        projection_lease = getattr(self, "_active_jog_projection_lease", None)
        self.stop_jog()
        self._active_jog_projection_lease = projection_lease
        self._apply_axes(axes)

    def stop_jog(self) -> None:
        had_active_axes = self._active_axes is not None
        self._active_jog_projection_lease = None
        logger.debug(
            "TIMING stop_jog_requested active_axes=%s key_stack=%s",
            self._active_axes,
            self._key_stack,
        )
        if not self.serial_connection or not self.serial_connection.is_open:
            self._active_axes = None
            self._pending_jog_axes = None
            self._clear_pending_key_activations()
            if had_active_axes:
                self.jog_stopped.emit()
            return
        if not had_active_axes:
            self._pending_jog_axes = None
            return
        self._active_axes = None
        self._pending_jog_axes = None
        stop_serial = self.serial_connection
        self.send_command(b"\x85")
        if (
            stop_serial is not None
            and self.serial_connection is stop_serial
            and getattr(stop_serial, "is_open", False)
        ):
            self._schedule_jog_stop_resend()
        if had_active_axes:
            self.jog_stopped.emit()
        logger.debug("Stop jog command issued")

    def cancel_jog_input(self) -> None:
        """Stop motion and forget held inputs before changing its coordinate basis."""

        self._pending_jog_axes = None
        self._clear_pending_key_activations()
        self._key_stack.clear()
        self._key_press_times.clear()
        self._sync_physical_key_watchdog()
        self.stop_jog()

    def _apply_axes(self, axes: tuple[tuple[str, int], ...]) -> None:
        if not self._move_safety_check():
            self.stop_jog()
            return
        axes_sorted = tuple(sorted(axes, key=lambda item: item[0]))
        if not axes_sorted:
            self.stop_jog()
            return
        if self._active_axes == axes_sorted:
            return
        if not self.serial_connection or not self.serial_connection.is_open:
            self._active_axes = None
            self._active_jog_projection_lease = None
            return
        feedrate = self._feedrate_for_axes(axes_sorted)
        if feedrate is None:
            self.stop_jog()
            return
        projection_lease = getattr(self, "_active_jog_projection_lease", None)
        if self._active_axes is not None:
            self.stop_jog()
        requested_distances = tuple(
            (axis, direction * self._distance_for_axis(axis))
            for axis, direction in axes_sorted
        )
        projector = getattr(self, "_relative_motion_projector", None)
        next_projection_lease: object | None = None
        if projector is None:
            commanded_distances = list(requested_distances)
        else:
            try:
                projection = projector(requested_distances, projection_lease)
                if not bool(projection.accepted):
                    reason = str(projection.reason or "Jog movement is unavailable.")
                    self._active_jog_projection_lease = None
                    self._show_warning(reason)
                    self.jog_command_changed.emit(tuple(), float(feedrate))
                    return
                commanded_distances = list(projection.raw_distances)
                next_projection_lease = projection.lease
            except Exception as error:  # pragma: no cover - UI safety guard
                self._active_jog_projection_lease = None
                self._show_warning(str(error))
                logger.exception("Failed to project jog command: %s", error)
                self.jog_command_changed.emit(tuple(), float(feedrate))
                return
        if self.stage_controller is not None:
            try:
                commanded_distances = list(
                    self.stage_controller.constrain_jog_distances(
                        tuple(commanded_distances)
                    )
                )
            except Exception as error:  # pragma: no cover - UI safety guard
                self._active_jog_projection_lease = None
                self._show_warning(str(error))
                logger.exception("Failed to constrain jog command: %s", error)
                self.jog_command_changed.emit(tuple(), float(feedrate))
                return
        if not commanded_distances:
            self._active_jog_projection_lease = None
            self.jog_command_changed.emit(tuple(), float(feedrate))
            return
        parts = [f"{axis}{distance:.3f}" for axis, distance in commanded_distances]
        command = f"$J=G91 G21 {' '.join(parts)} F{feedrate}\n"
        logger.debug(
            "TIMING jog_command_prepared axes=%s feedrate=%s command=%s",
            commanded_distances,
            feedrate,
            command.strip(),
        )
        if not self.send_command(command):
            self._active_jog_projection_lease = None
            self.jog_command_changed.emit(tuple(), float(feedrate))
            return
        self._invalidate_jog_stop_resend()
        self._active_jog_projection_lease = next_projection_lease
        if projector is None:
            commanded_axes = {axis for axis, _distance in commanded_distances}
            self._active_axes = tuple(
                (axis, direction)
                for axis, direction in axes_sorted
                if axis in commanded_axes
            )
        else:
            self._active_axes = axes_sorted
        self.jog_command_changed.emit(tuple(commanded_distances), float(feedrate))
        logger.debug("TIMING jog_command_sent command=%s", command.strip())

    def _distance_for_axis(self, axis: str) -> float:
        axis = axis.upper()
        if axis == "B":
            return self._rotary_jog_distance_deg
        if axis == "A":
            return self._manual_axis_distance_mm
        return self._linear_jog_distance_mm

    def _feedrate_for_axes(self, axes: tuple[tuple[str, int], ...]) -> Optional[float]:
        if not axes:
            return None
        target = self._feedrate_target_for_axis(axes[0][0])
        self._set_active_feedrate_target(target)
        return self._linear_feedrate_value

    def _manual_axis_step(
        self, axis: str, direction: int, *, mode: Optional[str] = None
    ) -> None:
        if not (self._axis_a_ready or self._motion_safety_disabled):
            return
        axis = axis.strip().upper()
        if axis not in self.MANUAL_JOG_AXES:
            return
        target = self._feedrate_target_for_axis(axis)
        self._set_active_feedrate_target(target)
        distance = float(direction) * self._manual_axis_distance_mm
        self.motion_axis_requested.emit(axis)
        self.manual_axis_move_requested.emit(
            axis,
            distance,
            mode or self.DEFAULT_MANUAL_AXIS_MODE,
            self._linear_feedrate_value,
        )

    def _on_step_distance_spin_changed(self) -> None:
        self._set_step_distance(
            float(self.step_distance_spin.value()), emit_changed=True
        )

    def _set_step_distance(self, value: float, *, emit_changed: bool) -> bool:
        try:
            bounded = float(value)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(bounded):
            return False
        bounded = min(1000.0, max(0.001, bounded))
        previous_value = getattr(self, "_manual_axis_distance_mm", bounded)
        changed = abs(bounded - previous_value) > 1e-9
        self._manual_axis_distance_mm = bounded
        spin = getattr(self, "step_distance_spin", None)
        if spin is not None and hasattr(spin, "setValue"):
            current_value: float | None = None
            if hasattr(spin, "value"):
                try:
                    current_value = float(spin.value())
                except (TypeError, ValueError):
                    current_value = None
            if current_value is None or abs(current_value - bounded) > 1e-9:
                if hasattr(spin, "blockSignals"):
                    spin.blockSignals(True)
                spin.setValue(bounded)
                if hasattr(spin, "blockSignals"):
                    spin.blockSignals(False)
        if emit_changed and changed:
            self._emit_manual_axis_settings_changed()
        return changed

    def _emit_manual_axis_settings_changed(self) -> None:
        if self._applying_jog_settings:
            return
        self._manual_axis_mode = self.DEFAULT_MANUAL_AXIS_MODE
        self.manual_axis_settings_changed.emit(
            "X",
            self._manual_axis_distance_mm,
            self._manual_axis_mode,
            self._feedrate_values.get(
                self._feedrate_key(self.FEED_TARGET_XY, self.MODE_STEP),
                self._manual_axis_feedrate_mm_min,
            ),
        )

    def _compute_active_axes(self) -> tuple[tuple[str, int], ...]:
        axis_directions: dict[str, list[int]] = {}
        for identifier in self._key_stack:
            mapping = self._mapping_from_identifier(identifier)
            if mapping is None:
                continue
            axis, direction = mapping
            axis_directions.setdefault(axis, []).append(direction)
        unique_axes: dict[str, int] = {}
        for axis, directions in axis_directions.items():
            active_direction = self._active_direction_for_axis(axis)
            if active_direction in directions:
                unique_axes[axis] = active_direction
            else:
                unique_axes[axis] = directions[-1]
        return tuple(unique_axes.items())

    def _schedule_active_jog_update(self) -> None:
        axes = self._compute_active_axes()
        if not axes:
            self._pending_jog_axes = None
            self._clear_pending_key_activations()
            if self._jog_state_sync_timer.isActive():
                self._jog_state_sync_timer.stop()
            logger.debug(
                "Scheduled immediate jog stop: axes=%s active_axes=%s",
                axes,
                self._active_axes,
            )
            self.stop_jog()
            return
        self._pending_jog_axes = axes
        interval = self._jog_sync_interval_for_axes(axes)
        if self._jog_state_sync_timer.isActive():
            self._jog_state_sync_timer.stop()
        self._jog_state_sync_timer.setInterval(interval)
        self._jog_state_sync_timer.start()
        logger.debug(
            "Scheduled jog state sync: axes=%s interval_ms=%s active_axes=%s",
            axes,
            interval,
            self._active_axes,
        )

    def _sync_active_jog_state(self) -> None:
        axes = self._pending_jog_axes
        if axes is None:
            axes = self._compute_active_axes()
        self._pending_jog_axes = None
        logger.debug("Active keys mapped to axes: %s", axes)
        if not axes:
            self.stop_jog()
            return
        self._apply_axes(axes)

    def _jog_sync_interval_for_axes(self, axes: tuple[tuple[str, int], ...]) -> int:
        if not axes:
            return 0
        linear_axis_count = sum(axis in self.LINEAR_AXES for axis, _direction in axes)
        active_axes = self._active_axes or ()
        if not active_axes:
            return (
                self.KEYBOARD_JOG_DIAGONAL_CHORD_WINDOW_MS
                if linear_axis_count == 1
                else 0
            )
        if any(
            self._axis_direction_changes(axis, direction) for axis, direction in axes
        ):
            return self.KEYBOARD_JOG_DIRECTION_CHANGE_CHORD_WINDOW_MS
        active_linear_axis_count = sum(
            axis in self.LINEAR_AXES for axis, _direction in active_axes
        )
        if linear_axis_count == 1 and active_linear_axis_count >= 2:
            return self.KEYBOARD_JOG_AXIS_DROP_CHORD_WINDOW_MS
        return self.KEYBOARD_JOG_SYNC_DEBOUNCE_MS

    def _active_direction_for_axis(self, axis: str) -> Optional[int]:
        for active_axis, active_direction in self._active_axes or ():
            if active_axis == axis:
                return active_direction
        return None

    def _axis_direction_changes(self, axis: str, direction: int) -> bool:
        active_direction = self._active_direction_for_axis(axis)
        return active_direction is not None and active_direction != direction

    def _send_reset(self) -> None:
        self.reset_requested.emit()
        if self.stage_controller is None:
            self.send_command(b"\x18")

    def send_command(self, command: str | bytes) -> bool:
        if not self.serial_connection or not self.serial_connection.is_open:
            logger.debug("Discarded command because serial is closed: %s", command)
            return False
        queued = self._queue_controller_command(command)
        if queued is not None:
            return queued
        try:
            data = command if isinstance(command, bytes) else command.encode("ascii")
            self._log_serial_write_timing(command, "begin")
            self.serial_connection.write(data)
            self.serial_connection.flush()
            self._log_serial_write_timing(command, "flushed")
            if isinstance(command, bytes):
                logger.debug("Command written to serial (bytes): %s", command.hex())
            else:
                logger.debug("Command written to serial: %s", command.strip())
            return True
        except serial.SerialException as error:  # pragma: no cover - best effort guard
            self._show_warning(f"Serial communication error: {error}")
            self.set_serial(None)
            logger.exception("Serial communication error: %s", error)
            return False

    def _queue_controller_command(self, command: str | bytes) -> bool | None:
        if self.stage_controller is None:
            return None
        try:
            return self.stage_controller.queue_outbound_command(
                command,
                source="joystick_reset_button",
            )
        except Exception as error:  # pragma: no cover - UI safety guard
            self._show_warning(str(error))
            logger.exception("Failed to queue controller command: %s", error)
            return False

    @staticmethod
    def _log_serial_write_timing(command: str | bytes, phase: str) -> None:
        if isinstance(command, str) and command.startswith("$J="):
            logger.debug(
                "TIMING jog_serial_write_%s command=%s",
                phase,
                command.strip(),
            )
        elif isinstance(command, bytes) and command == b"\x85":
            logger.debug("TIMING jog_stop_write_%s command=0x85", phase)

    def _invalidate_jog_stop_resend(self) -> None:
        self._jog_stop_resend_generation = (
            getattr(self, "_jog_stop_resend_generation", 0) + 1
        )

    def _schedule_jog_stop_resend(self) -> None:
        serial_connection = self.serial_connection
        if serial_connection is None:
            return
        self._invalidate_jog_stop_resend()
        generation = self._jog_stop_resend_generation

        def resend(expected_generation: int, expected_serial: serial.Serial) -> None:
            if expected_generation != self._jog_stop_resend_generation:
                return
            if self.serial_connection is not expected_serial:
                return
            if (
                self._key_stack
                or self._pending_jog_axes is not None
                or self._pending_key_activations
                or self._active_axes is not None
            ):
                return
            if not expected_serial.is_open:
                return
            self.send_command(b"\x85")
            logger.debug("Resent stop jog command")

        QTimer.singleShot(
            self.JOG_STOP_RESEND_DELAY_MS,
            lambda expected_generation=generation, expected_serial=serial_connection: (
                resend(expected_generation, expected_serial)
            ),
        )
