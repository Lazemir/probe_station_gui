"""Jog, feed override, and terminal serial queue methods for stage control."""

from __future__ import annotations

import logging
import math

from probe_station_gui.stage.errors import StageControllerError
from probe_station_gui.stage.feed_override import (
    clamp_feed_override_percent,
    feed_override_payload_for_percent_change,
    feed_override_percent_for_feedrates,
)
from probe_station_gui.stage.fluidnc_command_channel import classify_outbound_command
from probe_station_gui.stage.jog_commands import (
    absolute_axis_targets_jog_command,
    jog_command_feedrate,
    move_vector_from_axis_distances,
    move_vector_from_jog_command,
    relative_jog_command_to_absolute,
)
from probe_station_gui.stage.types import MoveVector, _QueuedSerialWrite


logger = logging.getLogger(__name__)


class StageControllerJogQueueMixin:
    """Internal jog parsing, feed override, and async queue entry points."""

    def queue_outbound_command(
        self,
        command: str | bytes,
        *,
        source: str = "unknown",
    ) -> bool | None:
        """Route a UI-originated outbound FluidNC command through controller-owned paths."""

        kind = classify_outbound_command(command)
        if kind == "jog_stop":
            self.queue_jog_stop()
            return True
        if kind == "soft_reset":
            self.queue_soft_reset(source=source)
            return True
        if kind == "jog_command":
            self.queue_jog_command(command)
            return True
        if kind == "manual_command":
            self.queue_manual_command(command)
            return True
        return None

    def queue_jog_command(self, command: str) -> None:
        """Queue the latest jog command for asynchronous serial delivery."""

        if self.is_busy():
            raise StageControllerError("Stage is busy. Wait for the current operation to finish.")
        stripped = command.strip()
        if not stripped:
            return
        move = self._move_vector_from_jog_command(stripped)
        if move is not None and not self._motion_safety_disabled:
            checked_move = self._check_cached_jog_move_limits(move)
            stripped = self._absolute_jog_command_for_relative_move(
                stripped,
                checked_move,
            )
        self._queued_jog_generation += 1
        self._jog_motion_active = True
        self.queue_feed_override_reset()
        self._async_write_queue.put(
            _QueuedSerialWrite(
                priority=self.SERIAL_PRIORITY_JOG_COMMAND,
                sequence=self._next_queued_write_sequence(),
                kind="jog_command",
                payload=(stripped + "\n").encode("ascii"),
                description=stripped,
                generation=self._queued_jog_generation,
                clear_epoch=self._async_write_clear_epoch,
            )
        )

    def queue_absolute_axis_targets_jog(
        self,
        targets: dict[str, float],
        *,
        feedrate: float,
        replace_active: bool = False,
    ) -> bool:
        """Stop and requeue an absolute jog target through the jog command path."""

        active_busy = self.is_busy()
        if active_busy and not replace_active:
            raise StageControllerError("Stage is busy. Wait for the current operation to finish.")
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
            effective_feedrate = max(self.MIN_FEEDRATE, float(feedrate))
        except (TypeError, ValueError):
            self.status_message.emit(f"Unsupported manual feedrate: {feedrate}")
            return False
        command = self._absolute_axis_targets_jog_command(
            normalized,
            effective_feedrate,
        )
        if not command:
            return False
        if active_busy:
            self._cancel_event.set()
        self.queue_jog_stop()
        self._queued_jog_generation += 1
        self._jog_motion_active = False
        self.queue_feed_override_reset()
        self._async_write_queue.put(
            _QueuedSerialWrite(
                priority=self.SERIAL_PRIORITY_JOG_COMMAND,
                sequence=self._next_queued_write_sequence(),
                kind="jog_command",
                payload=(command + "\n").encode("ascii"),
                description=command,
                generation=self._queued_jog_generation,
                clear_epoch=self._async_write_clear_epoch,
            )
        )
        return True

    @classmethod
    def feed_override_percent_for_feedrates(
        cls, programmed_feedrate: float, target_feedrate: float
    ) -> int:
        return feed_override_percent_for_feedrates(
            programmed_feedrate,
            target_feedrate,
            min_feedrate=cls.MIN_FEEDRATE,
            min_percent=cls.FEED_OVERRIDE_MIN_PERCENT,
            max_percent=cls.FEED_OVERRIDE_MAX_PERCENT,
        )

    @classmethod
    def _feed_override_payload_for_percent_change(
        cls,
        current_percent: int,
        target_percent: int,
        *,
        reset_first: bool = False,
    ) -> tuple[bytes, int]:
        return feed_override_payload_for_percent_change(
            current_percent,
            target_percent,
            reset_first=reset_first,
            min_percent=cls.FEED_OVERRIDE_MIN_PERCENT,
            max_percent=cls.FEED_OVERRIDE_MAX_PERCENT,
            reset_payload=cls.FEED_OVERRIDE_RESET,
            plus_10_payload=cls.FEED_OVERRIDE_PLUS_10,
            minus_10_payload=cls.FEED_OVERRIDE_MINUS_10,
            plus_1_payload=cls.FEED_OVERRIDE_PLUS_1,
            minus_1_payload=cls.FEED_OVERRIDE_MINUS_1,
        )

    def queue_feed_override_for_feedrate(
        self, programmed_feedrate: float, target_feedrate: float
    ) -> int | None:
        """Queue realtime feed override bytes for an already-running G1 move."""

        try:
            target_percent = self.feed_override_percent_for_feedrates(
                programmed_feedrate, target_feedrate
            )
        except (TypeError, ValueError, ZeroDivisionError):
            return None
        return self._queue_feed_override_percent(target_percent)

    def queue_active_needles_feedrate(self, target_feedrate: float) -> int | None:
        """Apply a realtime feed override to an active needle G1 move."""

        with self._task_lock:
            programmed_feedrate = self._active_needles_programmed_feedrate
            action = self._active_needles_action
        if action not in {"raise", "lower", "adjust"} or programmed_feedrate is None:
            return None
        return self.queue_feed_override_for_feedrate(
            programmed_feedrate,
            target_feedrate,
        )

    def queue_feed_override_reset(self) -> int | None:
        """Return feed override to 100% without blocking the UI."""

        with self._feed_override_lock:
            current = self._active_feed_override_percent
        if current in (None, 100):
            return current
        return self._queue_feed_override_percent(100)

    def _queue_feed_override_percent(self, target_percent: int) -> int | None:
        target = clamp_feed_override_percent(
            target_percent,
            min_percent=self.FEED_OVERRIDE_MIN_PERCENT,
            max_percent=self.FEED_OVERRIDE_MAX_PERCENT,
        )
        with self._feed_override_lock:
            current = self._active_feed_override_percent
            reset_first = current is None
            payload, applied = self._feed_override_payload_for_percent_change(
                100 if current is None else current,
                target,
                reset_first=reset_first,
            )
            if not payload:
                self._active_feed_override_percent = applied
                return applied
            self._active_feed_override_percent = applied
        self._queued_feed_override_generation += 1
        self._async_write_queue.put(
            _QueuedSerialWrite(
                priority=self.SERIAL_PRIORITY_FEED_OVERRIDE,
                sequence=self._next_queued_write_sequence(),
                kind="feed_override",
                payload=payload,
                description=f"feed override {applied}%",
                generation=self._queued_feed_override_generation,
                clear_epoch=self._async_write_clear_epoch,
            )
        )
        return applied

    def constrain_jog_distances(
        self,
        commanded_distances: object,
    ) -> tuple[tuple[str, float], ...]:
        """Clip UI jog distances so homed axes do not cross software limits."""

        if not isinstance(commanded_distances, (tuple, list)):
            return tuple()
        normalized: list[tuple[str, float]] = []
        for item in commanded_distances:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                continue
            axis = str(item[0]).strip().upper()
            if axis not in self.AXIS_INDEX:
                continue
            try:
                distance = float(item[1])
            except (TypeError, ValueError):
                continue
            if abs(distance) < 1e-6:
                continue
            normalized.append((axis, distance))
        if not normalized or self._motion_safety_disabled:
            return tuple(normalized)

        self._refresh_cached_jog_status_if_missing()
        move = self._move_vector_from_axis_distances(normalized)
        clipped = self._clip_relative_move_to_software_limits(move, emit_status=True)
        clipped_values = {axis: value for axis, value in clipped.items()}
        return tuple(
            (axis, clipped_values[axis])
            for axis, _distance in normalized
            if abs(clipped_values.get(axis, 0.0)) >= 1e-6
        )

    def _move_vector_from_jog_command(self, command: str) -> MoveVector | None:
        return move_vector_from_jog_command(command, axis_index=self.AXIS_INDEX)

    def _jog_command_feedrate(self, command: str) -> float | None:
        return jog_command_feedrate(command, min_feedrate=self.MIN_FEEDRATE)

    def _absolute_jog_command_for_relative_move(
        self,
        command: str,
        move: MoveVector,
    ) -> str:
        return relative_jog_command_to_absolute(
            command,
            move,
            position=self._last_stage_position,
            axis_index=self.AXIS_INDEX,
            min_feedrate=self.MIN_FEEDRATE,
            machine_position_mode=self._position_reporting_mode == "machine",
            axis_skip_reason=self._cached_jog_axis_skip_reason,
        )

    def _move_vector_from_axis_distances(
        self, distances: list[tuple[str, float]] | tuple[tuple[str, float], ...]
    ) -> MoveVector:
        return move_vector_from_axis_distances(
            distances,
            axis_index=self.AXIS_INDEX,
        )

    def _clip_relative_move_to_software_limits(
        self,
        move: MoveVector,
        *,
        emit_status: bool = False,
    ) -> MoveVector:
        if move.is_zero() or self._motion_safety_disabled:
            return move
        position = self._last_stage_position
        if position is None:
            return move
        values = {axis: delta for axis, delta in move.items()}
        for axis, delta in move.items():
            if abs(delta) < 1e-6:
                continue
            idx = self.AXIS_INDEX.get(axis)
            reason = self._cached_jog_axis_skip_reason(axis, position)
            if reason:
                if emit_status:
                    self.status_message.emit(
                        f"Jog soft limit unavailable on {axis}: {reason}."
                    )
                continue
            if axis == "B":
                clipped_delta = self._clip_b_relative_delta(delta)
                calibration_limits = self._axis_calibration_mapper().controller_domain_for_configured_mode(
                    axis,
                )
                if calibration_limits is not None:
                    current = float(position[idx])
                    target = current + clipped_delta
                    clipped_target = min(
                        max(target, calibration_limits[0]),
                        calibration_limits[1],
                    )
                    clipped_delta = clipped_target - current
                if emit_status and abs(clipped_delta - delta) >= 1e-6:
                    self.status_message.emit(
                        "Jog limited by software soft limit: "
                        f"B delta {delta:+.3f} clipped to {clipped_delta:+.3f}."
                    )
                values[axis] = clipped_delta
                continue
            limits = self.axis_raw_limits_for_configured_mode(axis, None)
            if limits is None:
                values[axis] = 0.0
                if emit_status:
                    self.status_message.emit(
                        f"Jog calibration range unavailable on {axis}."
                    )
                continue
            current = float(position[idx])
            min_value, max_value = limits
            target = current + float(delta)
            clipped_target = min(max(target, min_value), max_value)
            clipped_delta = clipped_target - current
            if abs(clipped_delta - delta) >= 1e-6 and emit_status:
                self.status_message.emit(
                    "Jog limited by software soft limit: "
                    f"{axis} target {target:+.3f} clipped to "
                    f"{clipped_target:+.3f} "
                    f"(limit {min_value:.3f}..{max_value:.3f})."
                )
            values[axis] = clipped_delta
        return MoveVector(
            x=values["X"],
            y=values["Y"],
            z=values["Z"],
            a=values["A"],
            b=values["B"],
            c=values["C"],
        )

    def _refresh_cached_jog_status_if_missing(self) -> None:
        if self._last_stage_position is not None:
            return
        serial_connection = self._serial
        if serial_connection is None or not serial_connection.is_open:
            return
        if not self._serial_session_lock.acquire(blocking=False):
            return
        try:
            with self._serial_session(serial_connection):
                self._query_status(serial_connection, timeout=0.5)
        except StageControllerError:
            return
        finally:
            self._serial_session_lock.release()

    def _cached_jog_axis_skip_reason(
        self,
        axis: str,
        position: tuple[float, ...],
    ) -> str | None:
        axis = axis.upper().strip()
        idx = self.AXIS_INDEX.get(axis)
        if idx is None:
            return "unsupported axis"
        if idx >= len(position):
            return "current position is unavailable"
        if axis == "B":
            if self._b_axis_zero_position is None:
                return "B zero reference is unavailable"
            return None
        if axis not in self._axis_limits:
            return "software limits are unavailable"
        limits = self._axis_limits_for_configured_mode(axis, None)
        if limits is None:
            return "software limits are unavailable in the current coordinate system"
        if not self._axis_software_limit_ready(None, axis):
            return "homing state is unavailable"
        return None

    def _clip_b_relative_delta(self, delta: float) -> float:
        if self._last_stage_position is None:
            return float(delta)
        idx = self.AXIS_INDEX.get("B")
        if idx is None or idx >= len(self._last_stage_position):
            return float(delta)
        if self._b_axis_zero_position is None:
            return float(delta)
        current_b = float(self._last_stage_position[idx]) - float(
            self._b_axis_zero_position
        )
        limit = self.B_AXIS_SOFT_LIMIT_DEG
        target_b = current_b + float(delta)
        clipped_target = min(max(target_b, -limit), limit)
        return clipped_target - current_b

    def _check_cached_jog_move_limits(self, move: MoveVector) -> MoveVector:
        clipped = self._clip_relative_move_to_software_limits(move)
        for axis, original_delta in move.items():
            clipped_delta = dict(clipped.items()).get(axis, 0.0)
            if abs(original_delta - clipped_delta) >= 1e-6:
                raise StageControllerError(
                    f"Jog exceeds software soft limit on {axis}."
                )
        return clipped

    def queue_jog_stop(self) -> None:
        """Queue a jog stop command without blocking the UI thread."""

        # Invalidate any queued-but-not-yet-written jog command so a late $J
        # cannot arrive after the stop and keep motion alive.
        self._queued_jog_generation += 1
        self._jog_motion_active = False
        job = _QueuedSerialWrite(
            priority=self.SERIAL_PRIORITY_JOG_STOP,
            sequence=self._next_queued_write_sequence(),
            kind="jog_stop",
            payload=b"\x85",
            description="0x85",
            generation=self._queued_jog_generation,
            clear_epoch=self._async_write_clear_epoch,
        )
        if self._try_write_jog_stop_immediately(job):
            return
        self._async_write_queue.put(job)

    def force_jog_stop(self, timeout: float = 0.5) -> bool:
        """Best-effort synchronous jog stop before closing the serial port."""

        self._queued_jog_generation += 1
        self._jog_motion_active = False
        self._clear_pending_async_writes()
        serial_connection = self._serial
        if serial_connection is None or not serial_connection.is_open:
            return False
        acquired = self._serial_session_lock.acquire(timeout=max(0.0, float(timeout)))
        if not acquired:
            logger.warning("Unable to acquire serial lock for emergency jog stop.")
            return False
        try:
            job = _QueuedSerialWrite(
                priority=self.SERIAL_PRIORITY_JOG_STOP,
                sequence=self._next_queued_write_sequence(),
                kind="jog_stop",
                payload=b"\x85",
                description="0x85 emergency",
                generation=self._queued_jog_generation,
                clear_epoch=self._async_write_clear_epoch,
            )
            self._write_async_job(serial_connection, job)
            logger.warning("Emergency jog stop written before serial shutdown.")
            return True
        except StageControllerError as exc:
            logger.warning("Emergency jog stop failed before serial shutdown: %s", exc)
            self.status_message.emit(str(exc))
            return False
        finally:
            self._serial_session_lock.release()

    def _try_write_jog_stop_immediately(self, job: _QueuedSerialWrite) -> bool:
        serial_connection = self._serial
        if serial_connection is None or not serial_connection.is_open:
            return True
        if not self._serial_session_lock.acquire(blocking=False):
            return False
        try:
            self._write_async_job(serial_connection, job)
            return True
        except StageControllerError as exc:
            self.status_message.emit(str(exc))
            return True
        finally:
            self._serial_session_lock.release()

    def queue_soft_reset(self, *, source: str = "unknown") -> None:
        """Queue a FluidNC soft reset without blocking the UI thread."""

        with self._task_lock:
            self._clear_unverified_controller_state_locked()
        self._async_write_queue.put(
            _QueuedSerialWrite(
                priority=self.SERIAL_PRIORITY_SOFT_RESET,
                sequence=self._next_queued_write_sequence(),
                kind="soft_reset",
                payload=b"\x18",
                description=f"CTRL-X source={source}",
                clear_epoch=self._async_write_clear_epoch,
            )
        )

    def queue_manual_command(self, command: str) -> None:
        """Queue a manual terminal command without blocking the UI thread."""

        if self.is_busy():
            raise StageControllerError("Cannot send while automated move is running.")
        payload = command if command.endswith("\n") else f"{command}\n"
        self._async_write_queue.put(
            _QueuedSerialWrite(
                priority=self.SERIAL_PRIORITY_TERMINAL,
                sequence=self._next_queued_write_sequence(),
                kind="terminal",
                payload=payload.encode("utf-8"),
                description=payload.rstrip(),
                clear_epoch=self._async_write_clear_epoch,
            )
        )

    def _absolute_axis_targets_jog_command(
        self,
        targets: dict[str, float],
        feedrate: float,
    ) -> str:
        return absolute_axis_targets_jog_command(
            targets,
            feedrate,
            axis_order=self.AXIS_INDEX,
            machine_position_mode=self._position_reporting_mode == "machine",
        )
