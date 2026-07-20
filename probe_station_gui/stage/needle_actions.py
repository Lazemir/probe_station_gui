"""Needle action orchestration for stage control."""

from __future__ import annotations

import math
import threading

from probe_station_gui.settings.precision_approach import (
    precision_profile_is_effective,
)
from probe_station_gui.stage.errors import StageControllerError
from probe_station_gui.stage.feedrate_limits import axis_max_feedrate
from probe_station_gui.stage.needle_motion_profile import (
    build_needle_motion_profile_segments,
)
from probe_station_gui.stage.needle_state import needle_contact_boundary_lowering
from probe_station_gui.stage.needle_targets import (
    needle_programmed_feedrate,
    needle_target_lowering_for_action,
)
from probe_station_gui.stage.types import MoveVector, _Status


class StageControllerNeedleActionsMixin:
    """Internal needle action, profile, and queue methods."""

    def _begin_needles_feedrate_control(
        self,
        action: str,
        feedrate: float | None,
    ) -> float:
        programmed_feedrate = self._needle_programmed_feedrate(feedrate)
        with self._task_lock:
            self._active_needles_action = action
            self._active_needles_programmed_feedrate = programmed_feedrate
        return programmed_feedrate

    def _end_needles_feedrate_control(self) -> None:
        with self._task_lock:
            had_active_control = self._active_needles_action is not None
            self._active_needles_action = None
            self._active_needles_programmed_feedrate = None
        if had_active_control:
            self.queue_feed_override_reset()

    def _needle_programmed_feedrate(self, feedrate: float | None) -> float:
        return needle_programmed_feedrate(
            feedrate,
            default_feedrate=self.DEFAULT_FEEDRATE,
            min_feedrate=self.MIN_FEEDRATE,
            error_factory=StageControllerError,
        )

    def _axis_max_feedrate(self, axis: str) -> float:
        return axis_max_feedrate(
            axis,
            self._axis_max_feedrates,
            default_feedrate=self.DEFAULT_FEEDRATE,
            min_feedrate=self.MIN_FEEDRATE,
        )

    def _query_status_for_needle_axis_motion(self) -> _Status | None:
        status = self._query_synced_status_for_absolute_motion(
            refresh_coordinate_state=False,
            axes=("A",),
        )
        if self._needle_axis_status_has_work_offset(status):
            return status
        return self._query_synced_status_for_absolute_motion(axes=("A",))

    def _needle_axis_status_has_work_offset(self, status: _Status | None) -> bool:
        if self._position_reporting_mode == "machine":
            return True
        if status is None:
            return False
        axis_index = self.AXIS_INDEX.get("A")
        if axis_index is None:
            return False
        work_offset = getattr(status, "work_offset", None)
        if work_offset is not None and axis_index < len(work_offset):
            return True
        coordinate_system = (
            getattr(status, "coordinate_system", None)
            or self._active_work_coordinate_system
        )
        if not coordinate_system:
            return False
        cached_offset = self._controller_coordinate_offsets.get(
            str(coordinate_system).strip().upper()
        )
        return cached_offset is not None and axis_index < len(cached_offset)

    def _needle_target_lowering_for_action(self, action: str) -> float:
        return needle_target_lowering_for_action(
            action,
            down_lowering_mm=self._needle_down_lowering_mm,
            boundary_lowering=self._needle_contact_boundary_lowering(),
            error_factory=StageControllerError,
        )

    def _needle_contact_boundary_lowering(self) -> float | None:
        return needle_contact_boundary_lowering(
            down_lowering_mm=self._needle_down_lowering_mm,
            contact_zone_mm=self._needle_contact_zone_mm,
        )

    def _needle_motion_profile_segments(
        self,
        action: str,
        current_a: float,
        feedrate: float | None,
        status: _Status | None = None,
        *,
        target_lowering: float | None = None,
    ) -> list[tuple[float, float, bool]]:
        """Return physical lowering targets, feedrates, and slow-zone markers."""

        if target_lowering is None:
            target_lowering = self._needle_target_lowering_for_action(action)
        else:
            target_lowering = max(0.0, float(target_lowering))
        target_a = self._axis_a_configured_target_for_lowering(
            target_lowering,
            status,
        )
        if abs(target_a - float(current_a)) < 1e-6:
            return []

        current_lowering = self._axis_a_lowering_for_configured_coordinate(
            current_a,
            status,
        )
        return build_needle_motion_profile_segments(
            current_lowering=current_lowering,
            target_lowering=target_lowering,
            boundary_lowering=self._needle_contact_boundary_lowering(),
            fast_feedrate=self._axis_max_feedrate("A"),
            slow_feedrate=self._needle_programmed_feedrate(feedrate),
            min_feedrate=self.MIN_FEEDRATE,
        )

    def _send_needle_motion_profile_locked(
        self,
        *,
        action: str,
        current_a: float,
        target_lowering: float,
        feedrate: float | None,
        status: _Status | None,
    ) -> bool:
        target_a = self._axis_a_configured_target_for_lowering(
            target_lowering,
            status,
        )
        segments = self._needle_motion_profile_segments(
            action,
            current_a,
            feedrate,
            status,
            target_lowering=target_lowering,
        )
        if not segments:
            self._update_needles_from_a_position(target_a)
            return False
        for segment_index, (segment_lowering, segment_feedrate, slow_zone) in enumerate(segments):
            self._check_cancelled()
            segment_target_a = self._axis_a_configured_target_for_lowering(
                segment_lowering,
                status,
            )
            if abs(segment_target_a - current_a) < 1e-6:
                continue
            use_precision_approach = (
                segment_index == len(segments) - 1
                and precision_profile_is_effective(
                    self._precision_approach_settings.profiles["A"]
                )
            )
            if slow_zone:
                programmed_feedrate = self._begin_needles_feedrate_control(
                    action,
                    segment_feedrate,
                )
                try:
                    if use_precision_approach:
                        self._execute_precision_axis_targets_locked(
                            {"A": segment_target_a},
                            feedrate=programmed_feedrate,
                            allow_unhomed=False,
                            ignore_needle_safety=True,
                        )
                    else:
                        self._send_absolute_axis_move(
                            "A",
                            segment_target_a,
                            ignore_needle_safety=True,
                            feedrate=programmed_feedrate,
                            as_jog=True,
                        )
                finally:
                    self._end_needles_feedrate_control()
            else:
                if use_precision_approach:
                    self._execute_precision_axis_targets_locked(
                        {"A": segment_target_a},
                        feedrate=segment_feedrate,
                        allow_unhomed=False,
                        ignore_needle_safety=True,
                    )
                else:
                    self._send_absolute_axis_move(
                        "A",
                        segment_target_a,
                        ignore_needle_safety=True,
                        feedrate=segment_feedrate,
                        as_jog=True,
                    )
            current_a = segment_target_a
        self._update_needles_from_a_position(target_a)
        return True

    def _run_needles_action(
        self,
        action: str,
        feedrate: float | None = None,
    ) -> None:
        self.needles_action_started.emit(action)
        try:
            message = self._perform_needles_action(action, feedrate)
            self.needles_action_finished.emit(True, message, action)
        except StageControllerError as exc:
            self.needles_action_finished.emit(False, str(exc), action)
        finally:
            with self._task_lock:
                self._active_thread = None
            self._start_next_queued_needles_action()

    def _perform_needles_action(
        self,
        action: str,
        feedrate: float | None = None,
    ) -> str:
        action = str(action).strip().lower()
        with self._serial_session():
            if action in {"raise", "lift", "lower"}:
                status = self._query_status_for_needle_axis_motion()
                current_a = self._axis_value_for_configured_mode(status, "A")
                if status is None or current_a is None:
                    raise StageControllerError("Unable to read A position for needles.")
                self._require_homed_axes(status, {"A"})
                target_lowering = self._needle_target_lowering_for_action(action)
                target_a = self._axis_a_configured_target_for_lowering(
                    target_lowering,
                    status,
                )
                moved = self._send_needle_motion_profile_locked(
                    action=action,
                    current_a=current_a,
                    target_lowering=target_lowering,
                    feedrate=feedrate,
                    status=status,
                )
                if not moved:
                    update_a = current_a if action == "lift" else target_a
                    self._update_needles_from_a_position(update_a)
                    if action == "raise":
                        return "Needles already raised."
                    if action == "lift":
                        return "Needles already lifted."
                    return "Needles already lowered."
                if action == "raise":
                    return "Needles raised."
                if action == "lift":
                    return "Needles lifted."
                return "Needles lowered."
            raise StageControllerError(f"Unknown needle action: {action}.")

    def _perform_needles_lower_to_depth_below_down(
        self,
        depth_mm: float,
        feedrate: float | None = None,
    ) -> str:
        with self._serial_session():
            if not math.isfinite(depth_mm):
                raise StageControllerError("Needle search depth must be finite.")
            depth_mm = max(0.0, float(depth_mm))
            status = self._query_status_for_needle_axis_motion()
            current_a = self._axis_value_for_configured_mode(status, "A")
            if status is None or current_a is None:
                raise StageControllerError("Unable to read A position for needles.")
            self._require_homed_axes(status, {"A"})
            down_lowering = self._needle_target_lowering_for_action("lower")
            target_lowering = down_lowering + depth_mm
            moved = self._send_needle_motion_profile_locked(
                action="lower",
                current_a=current_a,
                target_lowering=target_lowering,
                feedrate=feedrate,
                status=status,
            )
            if not moved:
                return "Needles already lowered."
        if depth_mm <= 1e-9:
            return "Needles lowered."
        return f"Needles lowered to {depth_mm:.4f} mm below saved down."

    def _run_needles_adjust(
        self,
        step_mm: float,
        feedrate: float | None = None,
    ) -> None:
        action = "adjust"
        try:
            message = self._perform_needles_adjust(step_mm, feedrate)
            self.needles_action_finished.emit(
                True,
                message,
                action,
            )
        except StageControllerError as exc:
            self.needles_action_finished.emit(False, str(exc), action)
        finally:
            with self._task_lock:
                self._active_thread = None
            self._start_next_queued_needles_action()

    def _perform_needles_adjust(
        self,
        step_mm: float,
        feedrate: float | None = None,
    ) -> str:
        with self._serial_session():
            if abs(step_mm) < 1e-6:
                return "Needle position unchanged."
            status = self._query_status_for_needle_axis_motion()
            if (
                status is None
                or self._axis_value_for_configured_mode(status, "A") is None
            ):
                raise StageControllerError("Unable to read A position for needles.")
            self._require_homed_axes(status, {"A"})
            current_a = self._axis_value_for_configured_mode(status, "A")
            if current_a is None:
                raise StageControllerError("Unable to read A position for needles.")
            target_a = self._axis_a_gcode_coordinate_for_lowering_step(
                current_a,
                step_mm,
            )
            if abs(target_a - current_a) < 1e-6:
                return "Needle position unchanged."
            programmed_feedrate = self._begin_needles_feedrate_control(
                "adjust",
                feedrate,
            )
            try:
                self._send_absolute_axis_move(
                    "A",
                    target_a,
                    ignore_needle_safety=True,
                    feedrate=programmed_feedrate,
                    as_jog=True,
                )
            finally:
                self._end_needles_feedrate_control()
            current_a = self._read_current_a_position()
            if current_a is None:
                raise StageControllerError("Unable to confirm A position after move.")
            self._update_needles_from_a_position(current_a)
        direction = "lowered" if step_mm < 0 else "raised"
        return f"Needles {direction} by {abs(step_mm):.3f} mm."

    def _apply_pending_oscillation_needles_actions(self) -> None:
        """Apply queued A-axis actions inline while oscillation continues."""

        pending: list[tuple[str, float | None, float | None]] = []
        with self._task_lock:
            while self._oscillation_needles_actions:
                pending.append(self._oscillation_needles_actions.popleft())
        for action, step_mm, feedrate in pending:
            self._execute_oscillation_needles_action(
                action,
                step_mm,
                feedrate,
            )

    def _execute_oscillation_needles_action(
        self,
        action: str,
        step_mm: float | None,
        feedrate: float | None,
    ) -> None:
        """Execute an A-axis move inline in G91 during oscillation."""

        try:
            current_a = self._latest_known_a_position()
            if current_a is None:
                raise StageControllerError(
                    "A axis position is unknown; cannot adjust needles during oscillation."
                )
            if action in {"raise", "lift", "lower"}:
                target_lowering = self._needle_target_lowering_for_action(action)
                target_a = self._axis_a_configured_target_for_lowering(
                    target_lowering
                )
                segments = self._needle_motion_profile_segments(
                    action,
                    current_a,
                    feedrate,
                )
                if not segments:
                    update_a = current_a if action == "lift" else target_a
                    self._update_needles_from_a_position(update_a)
                    self.needles_action_finished.emit(
                        True,
                        (
                            "Needles already raised."
                            if action == "raise"
                            else (
                                "Needles already lifted."
                                if action == "lift"
                                else "Needles already lowered."
                            )
                        ),
                        action,
                    )
                    return
                new_a = current_a
                for segment_lowering, segment_feedrate, slow_zone in segments:
                    segment_target_a = self._axis_a_configured_target_for_lowering(
                        segment_lowering
                    )
                    relative_a_move = segment_target_a - new_a
                    if abs(relative_a_move) < 1e-6:
                        continue
                    if slow_zone:
                        programmed_feedrate = self._begin_needles_feedrate_control(
                            action,
                            segment_feedrate,
                        )
                        try:
                            self._write_relative_g1_unchecked(
                                MoveVector(a=relative_a_move),
                                feedrate=programmed_feedrate,
                            )
                        finally:
                            self._end_needles_feedrate_control()
                    else:
                        self._write_relative_g1_unchecked(
                            MoveVector(a=relative_a_move),
                            feedrate=segment_feedrate,
                        )
                    new_a = segment_target_a
                self._update_cached_axis_position("A", new_a)
                self._update_needles_from_a_position(new_a)
                if action == "lift":
                    message = "Needles lifted."
                else:
                    message = (
                        "Needles raised." if action == "raise" else "Needles lowered."
                    )
                self.needles_action_finished.emit(
                    True,
                    message,
                    action,
                )
                return
            if action == "adjust":
                requested_step = 0.0 if step_mm is None else float(step_mm)
                target_a = self._axis_a_gcode_coordinate_for_lowering_step(
                    current_a,
                    requested_step,
                )
                relative_a_move = target_a - current_a
                if abs(relative_a_move) < 1e-6:
                    self.needles_action_finished.emit(
                        True,
                        "Needle position unchanged.",
                        action,
                    )
                    return
            else:
                raise StageControllerError(f"Unknown needle action: {action}.")

            self._write_relative_g1_unchecked(
                MoveVector(a=relative_a_move),
                feedrate=self._needle_programmed_feedrate(feedrate),
            )
            new_a = target_a
            self._update_cached_axis_position("A", new_a)
            self._update_needles_from_a_position(new_a)
            if action == "adjust":
                requested_step = 0.0 if step_mm is None else float(step_mm)
                direction = "lowered" if requested_step < 0 else "raised"
                message = f"Needles {direction} by {abs(requested_step):.3f} mm."
            elif action == "raise":
                message = "Needles raised."
            else:
                message = "Needles lowered."
            self.needles_action_finished.emit(True, message, action)
        except StageControllerError as exc:
            self.needles_action_finished.emit(False, str(exc), action)

    def _queue_oscillation_needles_action_locked(
        self,
        action: str,
        step_mm: float | None = None,
        *,
        feedrate: float | None = None,
    ) -> None:
        """Queue an A-axis move to be injected into the running oscillation."""

        self._oscillation_needles_actions.append((action, step_mm, feedrate))
        self.needles_action_started.emit(action)
        if action == "raise":
            label = "Needle raise"
        elif action == "lift":
            label = "Needle lift"
        elif action == "lower":
            label = "Needle lower"
        else:
            direction = "lower" if (step_mm or 0.0) < 0 else "raise"
            label = f"Needle {direction} step"
        self.status_message.emit(f"{label} queued during oscillation.")

    def _start_needles_action_locked(
        self,
        action: str,
        step_mm: float | None = None,
        *,
        feedrate: float | None = None,
    ) -> None:
        """Start a needle action while the caller owns the task lock."""

        self._cancel_event.clear()
        if action == "adjust":
            if step_mm is None:
                step_mm = 0.0
            thread = threading.Thread(
                target=self._run_needles_adjust,
                args=(float(step_mm), feedrate),
                daemon=True,
            )
            self._active_thread = thread
            self.needles_action_started.emit("adjust")
            thread.start()
            return
        thread = threading.Thread(
            target=self._run_needles_action,
            args=(action, feedrate),
            daemon=True,
        )
        self._active_thread = thread
        thread.start()

    def _start_next_queued_needles_action(self) -> None:
        """Run the next queued needle action after oscillation yields the controller."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                return
            if not self._queued_needles_actions:
                return
            action, step_mm, feedrate = self._queued_needles_actions.popleft()
            self._start_needles_action_locked(
                action,
                step_mm,
                feedrate=feedrate,
            )

    def _latest_known_a_position(self) -> float | None:
        """Return the best available cached A-axis coordinate."""

        if self._position_reporting_mode != "machine":
            if self._last_stage_position is not None and len(self._last_stage_position) > 3:
                return float(self._last_stage_position[3])
            return None
        if self._last_machine_position is not None and len(self._last_machine_position) > 3:
            return float(self._last_machine_position[3])
        return None

    def _update_cached_axis_position(self, axis: str, value: float) -> None:
        """Update cached stage coordinates after an inline single-axis move."""

        index = self.AXIS_INDEX.get(axis.upper())
        if index is None:
            return
        if (
            self._position_reporting_mode == "machine"
            and self._last_machine_position is not None
            and len(self._last_machine_position) > index
        ):
            machine = list(self._last_machine_position)
            machine[index] = float(value)
            self._last_machine_position = tuple(machine)
        if self._last_stage_position is not None and len(self._last_stage_position) > index:
            stage = list(self._last_stage_position)
            stage[index] = float(value)
            coords = tuple(stage)
            self._last_stage_position = coords
            self.stage_position_changed.emit(coords)
