"""GUI-thread owner for transient Stage motion interpretation."""

from __future__ import annotations

import time
from dataclasses import replace

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from probe_station_gui.application.stage_motion_settle_polls import (
    _StageMotionSettleStatusPolls,
)
from probe_station_gui.application import stage_motion_types as motion_types
from probe_station_gui.coordinates.model import PhysicalMachinePose
from probe_station_gui.stage import coordinate_targets as coordinate_types
from probe_station_gui.stage.controller import StageController
from probe_station_gui.stage.coordinate_targets import (
    CoordinateTargetMoveState,
    plan_coordinate_target_start,
    stage_axis_target_limit_error,
)
from probe_station_gui.stage.exact_step import ExactStepAccumulator
from probe_station_gui.stage.manual_jog_prediction import ManualJogPredictionState
from probe_station_gui.stage.motion_prediction import (
    axis_value,
    coerce_finite_position,
    coerce_finite_xy,
    held_planned_xy,
    matching_planned_xy_start,
    motion_progress,
    planned_xy_matches,
    planned_xy_timing,
    position_with_stage_xy,
)
from probe_station_gui.stage import types as stage_types


class _StageMotionSession(QObject):
    presentation_changed = Signal(object)
    action_state_changed = Signal(object)
    click_move_finished = Signal(bool)
    coordinate_move_finished = Signal(object)
    continue_homing_requested = Signal()
    unclaimed_movement_finished = Signal(object)
    status_requested = Signal(str, int)

    def __init__(
        self,
        controller: StageController,
        config: motion_types.StageMotionConfig,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._config = config
        self._manual_jog_prediction = ManualJogPredictionState(config.manual_jog)
        self._coordinate_targets = CoordinateTargetMoveState(config.coordinate_target)
        self._exact_step_accumulator = ExactStepAccumulator(config.axis_names)
        self._prediction_timer = QTimer(self)
        self._prediction_timer.setInterval(int(config.prediction_interval_ms))
        self._prediction_timer.timeout.connect(self.tick)
        self._settle_status_polls = _StageMotionSettleStatusPolls(
            self,
            delays_ms=config.settle_status_poll_delays_ms,
            request_status_refresh=controller.request_status_refresh,
        )

        self._presented_position: tuple[float, ...] | None = None
        self._presented_stage_xy: tuple[float, float] | None = None
        self._physical_machine_pose = PhysicalMachinePose.from_mapping({})
        self._active_axes: frozenset[str] = frozenset()
        self._last_reported_b_position: float | None = None
        self._pending_planned_target_xy: tuple[float, float] | None = None
        self._pending_planned_source_label: str | None = None
        self._planned_origin_xy: tuple[float, float] | None = None
        self._planned_stage_xy: tuple[float, float] | None = None
        self._planned_target_xy: tuple[float, float] | None = None
        self._planned_started_at: float | None = None
        self._planned_ends_at: float | None = None
        self._planned_waiting_for_fresh_status = False
        self._planned_stop_status_timestamp: float | None = None
        self._next_planned_motion_token = 1
        self._planned_motion_token: object | None = None
        self._accepted_planned_target_xy: tuple[float, float] | None = None
        self._last_observation = stage_types.StagePositionObservation.empty()
        self._last_presentation = self._presentation(
            reported_position=None,
            raw_stage_xy=None,
            contact_calibration_position=None,
            unhomed_fallback=False,
            clear_motion_axes=False,
        )

    def snapshot(self) -> motion_types.StageMotionSnapshot:
        return motion_types.StageMotionSnapshot(
            presented_position=self._presented_position,
            presented_stage_xy=self._presented_stage_xy,
            physical_machine_pose=self._physical_machine_pose,
            active_axes=self._active_axes,
            cancelable=False,
            coordinate_active=self._coordinate_targets.has_active_move(),
            coordinate_display_basis=self._coordinate_targets.display_basis,
            coordinate_display_targets=tuple(
                (axis, float(self._coordinate_targets.display_targets[axis]))
                for axis in self._config.axis_names
                if axis in self._coordinate_targets.display_targets
            ),
            coordinate_stage_position=self._coordinate_targets.stage_position,
            coordinate_programmed_feedrate=self._coordinate_targets.programmed_feedrate,
            coordinate_effective_feedrate=self._coordinate_targets.effective_feedrate,
            coordinate_common_feedrate=self._coordinate_targets.common_feedrate,
            pending_edit_axes=frozenset(self._coordinate_targets.pending_edits),
            manual_prediction_active=self._manual_jog_prediction.prediction_active(),
            planned_pending_target_xy=self._pending_planned_target_xy,
            planned_pending_source_label=self._pending_planned_source_label,
            planned_stage_xy=self._planned_stage_xy,
            planned_prediction_active=self._planned_started_at is not None,
            planned_waiting_for_fresh_status=(self._planned_waiting_for_fresh_status),
            last_reported_b_position=self._last_reported_b_position,
        )

    def pending_coordinate_edits(self) -> coordinate_types.CoordinatePendingEdits:
        return self._coordinate_targets.pending_snapshot()

    def upsert_pending_coordinate_edit(
        self,
        axis: str,
        raw_target: float,
        display_target: float,
        *,
        motion_lease: object | None,
    ) -> None:
        self._require_object_thread()
        self._coordinate_targets.upsert_pending_edit(
            axis,
            raw_target,
            display_target,
            motion_lease=motion_lease,
        )
        self._emit_action_state()

    def pop_pending_coordinate_edit(
        self,
        axis: str,
    ) -> tuple[float, float] | None:
        self._require_object_thread()
        removed = self._coordinate_targets.pop_pending_edit(axis)
        self._emit_action_state()
        return removed

    def clear_pending_coordinate_edits(self) -> bool:
        self._require_object_thread()
        had_edits = self._coordinate_targets.clear_pending_edits()
        self._emit_action_state()
        return had_edits

    def consume_pending_coordinate_edits(
        self,
    ) -> coordinate_types.CoordinatePendingEdits:
        self._require_object_thread()
        pending = self._coordinate_targets.consume_pending_edits()
        self._emit_action_state()
        return pending

    def discard_coordinate_tracking_for_manual_jog(self) -> bool:
        self._require_object_thread()
        if not self._coordinate_targets.has_active_move():
            return False
        self._clear_coordinate_tracking(reset_override=False)
        self._emit_action_state()
        return True

    def cancel_coordinate_move(
        self,
        reason: str = "Coordinate move cancel requested.",
    ) -> bool:
        self._require_object_thread()
        if not self._coordinate_targets.has_active_move():
            return False
        self._controller.cancel_active_motion(str(reason))
        self._clear_coordinate_tracking(reset_override=True)
        self._coordinate_targets.clear_pending_edits()
        self._emit_action_state()
        return True

    def start_coordinate_move(
        self, request: coordinate_types.CoordinateMoveRequest
    ) -> bool:
        self._require_object_thread()
        if not isinstance(request, coordinate_types.CoordinateMoveRequest):
            raise TypeError("request must be a CoordinateMoveRequest")
        if self._coordinate_targets.has_active_move():
            return False
        targets = {
            str(axis).strip().upper(): (float(raw), float(display))
            for axis, raw, display in request.targets
        }
        physical_limits = {
            str(axis).strip().upper(): float(target)
            for axis, target in request.physical_limit_targets
        }

        def limit_error(axis: str, display_target: float) -> str | None:
            if axis in physical_limits:
                target = physical_limits[axis]
                accessor = self._controller.axis_machine_display_limits
            else:
                target = display_target
                accessor = self._controller.axis_display_limits
            return stage_axis_target_limit_error(
                axis,
                target,
                homed_axes=set(self._last_observation.homed_axes),
                axis_display_limits=accessor,
            )

        decision = plan_coordinate_target_start(
            self._coordinate_targets.config,
            targets=targets,
            feedrate_mm_min=request.feedrate_mm_min,
            source_label=request.source_label,
            seed_position=request.seed_position,
            latest_stage_position=self._controller.latest_stage_position(),
            axis_target_limit_error=limit_error,
            axis_max_feedrates=self._controller.axis_max_feedrates(),
            monotonic_s=time.monotonic(),
        )
        if not decision.accepted:
            if decision.status is not None:
                self.status_requested.emit(
                    decision.status.message,
                    decision.status.timeout_ms,
                )
            return False
        plan = decision.plan
        if plan is None:
            return False
        self._coordinate_targets.apply_start_plan(
            plan,
            display_basis=request.display_basis,
        )
        self._active_axes = frozenset(plan.active_axes)
        accepted = bool(
            self._controller.request_absolute_axis_targets_move(
                plan.raw_targets,
                feedrate=plan.feedrate_mm_min,
            )
        )
        if not accepted:
            self._clear_coordinate_tracking(reset_override=True)
            return False
        self._coordinate_targets.arm_purpose()
        self._presented_position = plan.publish_position
        self._presented_stage_xy = coerce_finite_xy(plan.publish_position)
        self.status_requested.emit(plan.status.message, plan.status.timeout_ms)
        self._emit_presentation(
            reported_position=plan.publish_position,
            raw_stage_xy=self._presented_stage_xy,
            contact_calibration_position=None,
            unhomed_fallback=False,
            clear_motion_axes=False,
        )
        self._emit_action_state()
        if not self._prediction_timer.isActive():
            self._prediction_timer.start()
        return True

    def set_coordinate_feedrate(self, feedrate_mm_min: float) -> None:
        self._require_object_thread()
        if not self._coordinate_targets.has_active_move():
            return
        now = time.monotonic()
        decision = self._coordinate_targets.plan_feedrate_reissue(
            controller_busy=self._controller.is_busy(),
            latest_stage_state=self._controller.latest_stage_state(),
            requested_feedrate_mm_min=feedrate_mm_min,
            monotonic_s=now,
        )
        if decision.clear_stale_tracking:
            self._clear_coordinate_tracking(reset_override=False)
            self._emit_action_state()
            return
        self._advance_coordinate_prediction(now)
        if not self._coordinate_targets.has_active_move():
            return
        decision = self._coordinate_targets.plan_feedrate_reissue(
            controller_busy=self._controller.is_busy(),
            latest_stage_state=self._controller.latest_stage_state(),
            requested_feedrate_mm_min=feedrate_mm_min,
            monotonic_s=now,
        )
        reissue = decision.request
        if reissue is None:
            return
        self._coordinate_targets.reissue_cancel_pending = (
            reissue.set_reissue_cancel_pending
        )
        try:
            accepted = bool(
                self._controller.queue_absolute_axis_targets_jog(
                    reissue.raw_targets,
                    feedrate=reissue.requested_feedrate_mm_min,
                    replace_active=True,
                )
            )
        except Exception as error:  # pragma: no cover - GUI safety guard
            accepted = False
            self.status_requested.emit(str(error), 3000)
        if not accepted:
            self._coordinate_targets.reissue_cancel_pending = False
            self.status_requested.emit(
                "Unable to update coordinate move feedrate.",
                3000,
            )
            self._settle_status_polls.schedule()
            return
        self._coordinate_targets.apply_feedrate_reissue_success(reissue)
        self.status_requested.emit(
            (
                "Active coordinate move feedrate: "
                f"F{reissue.requested_feedrate_mm_min:.1f}."
            ),
            1500,
        )
        self._emit_action_state()

    @Slot(bool, str)
    def on_movement_finished(self, success: bool, message: str) -> None:
        self._require_object_thread()
        decision = self._coordinate_targets.claim_movement_finished(success, message)
        if not decision.claimed:
            self.unclaimed_movement_finished.emit(
                stage_types.UnclaimedMovementCompletion(
                    success=decision.success,
                    message=decision.message,
                )
            )
            return
        if decision.expected_reissue_cancel:
            self._settle_status_polls.schedule()
            self._emit_action_state()
            return
        if decision.disposition is None:
            if decision.message:
                self.status_requested.emit(decision.message, 5000)
            if decision.success:
                self._settle_status_polls.schedule()
            return
        self._complete_coordinate_move(
            success=decision.success,
            disposition=decision.disposition,
            message=decision.message,
        )
        if decision.message:
            self.status_requested.emit(decision.message, 5000)
        if decision.success:
            self._settle_status_polls.schedule()

    def request_planned_xy_move(
        self,
        request: motion_types.PlannedXYMoveRequest,
    ) -> bool:
        self._require_object_thread()
        if self._planned_request_in_flight():
            return False
        target = coerce_finite_xy(request.target_stage_xy)
        if target is None:
            return False
        source_label = str(request.source_label).strip() or "absolute XY move"
        motion_token = self._next_planned_motion_token
        self._next_planned_motion_token += 1
        self._pending_planned_target_xy = target
        self._pending_planned_source_label = source_label
        accepted = bool(
            self._controller.request_move_to_xy(
                target[0],
                target[1],
                motion_token=motion_token,
            )
        )
        if not accepted:
            self._clear_planned_prediction(clear_wait_state=True)
            return False
        self._accepted_planned_target_xy = target
        self._planned_motion_token = motion_token
        self._emit_action_state()
        return True

    def _planned_request_in_flight(self) -> bool:
        return bool(
            self._pending_planned_target_xy is not None
            or self._planned_started_at is not None
            or self._planned_waiting_for_fresh_status
            or self._planned_motion_token is not None
        )

    def cancel_planned_xy_move(self) -> bool:
        """Clear only planned-XY interpretation; callers retain motion policy."""

        self._require_object_thread()
        if not self._planned_request_in_flight():
            return False
        self._clear_planned_prediction(clear_wait_state=True)
        self._emit_presentation(
            reported_position=self._presented_position,
            raw_stage_xy=self._presented_stage_xy,
            contact_calibration_position=None,
            unhomed_fallback=False,
            clear_motion_axes=False,
        )
        self._emit_action_state()
        return True

    def refresh_physical_machine_pose(self) -> None:
        """Refresh the owned pose from the controller's synchronized cache."""

        self._require_object_thread()
        self._physical_machine_pose = self._controller.latest_physical_machine_pose(
            self._config.axis_names
        )
        self._last_presentation = replace(
            self._last_presentation,
            physical_machine_pose=self._physical_machine_pose,
        )
        self.presentation_changed.emit(self._last_presentation)

    def reset(self, reason: stage_types.StageMotionResetReason) -> None:
        """Reset state owned by the current planned/position slice."""

        self._require_object_thread()
        if not isinstance(reason, stage_types.StageMotionResetReason):
            raise TypeError("reason must be a StageMotionResetReason")
        self._settle_status_polls.clear()
        self._clear_coordinate_tracking(reset_override=False)
        self._coordinate_targets.clear_pending_edits()
        self._clear_planned_prediction(clear_wait_state=True)
        self._presented_position = None
        self._presented_stage_xy = None
        self._physical_machine_pose = PhysicalMachinePose.from_mapping({})
        self._last_reported_b_position = None
        self._last_observation = stage_types.StagePositionObservation.empty()
        self._emit_presentation(
            reported_position=None,
            raw_stage_xy=None,
            contact_calibration_position=None,
            unhomed_fallback=False,
            clear_motion_axes=True,
        )
        self._emit_action_state()

    @Slot(object)
    def on_tracked_absolute_xy_move_started(
        self,
        started: stage_types.TrackedAbsoluteXYMoveStarted,
    ) -> None:
        self._require_object_thread()
        if not isinstance(started, stage_types.TrackedAbsoluteXYMoveStarted):
            raise TypeError("started must be a TrackedAbsoluteXYMoveStarted")
        if not self._owns_planned_motion(started.motion_token):
            return
        pending_target = self._pending_planned_target_xy
        if pending_target is None:
            return
        start = matching_planned_xy_start(
            started.target_stage_xy[0],
            started.target_stage_xy[1],
            started.feedrate_mm_min,
            pending_target=pending_target,
            tolerance_mm=self._config.planned_start_tolerance_mm,
        )
        if start is None:
            self._clear_planned_prediction(clear_wait_state=True)
            self._emit_action_state()
            return
        target, feedrate = start
        source_label = self._pending_planned_source_label or "absolute XY move"
        self._pending_planned_target_xy = None
        self._pending_planned_source_label = None
        self._start_planned_prediction(
            target,
            origin_position=started.origin_position,
            source_label=source_label,
            feedrate_mm_min=feedrate,
        )

    @Slot(object)
    def on_stage_position_changed(
        self,
        observation: stage_types.StagePositionObservation,
    ) -> None:
        self._require_object_thread()
        if not isinstance(observation, stage_types.StagePositionObservation):
            raise TypeError("observation must be a StagePositionObservation")
        if observation.reset_reason is not None:
            self.reset(observation.reset_reason)
            return
        previous_observation = self._last_observation
        self._last_observation = observation
        material_change = not previous_observation.has_same_motion_facts_as(observation)
        position = observation.position
        current_position = coerce_finite_position(position)
        if current_position is None or len(current_position) < 2:
            presentation_was_available = self._presented_position is not None
            self._physical_machine_pose = PhysicalMachinePose.from_mapping({})
            self._last_observation = replace(
                observation,
                physical_machine_pose=self._physical_machine_pose,
            )
            self._presented_position = None
            self._presented_stage_xy = None
            self._emit_presentation(
                reported_position=position,
                raw_stage_xy=None,
                contact_calibration_position=None,
                unhomed_fallback=False,
                clear_motion_axes=False,
                material_change=material_change or presentation_was_available,
            )
            self._emit_action_state()
            return

        self._physical_machine_pose = observation.physical_machine_pose
        raw_stage_xy = (current_position[0], current_position[1])
        latest_state = str(observation.stage_state or "").lower()
        coordinate_active = self._coordinate_targets.has_active_move()
        if coordinate_active and latest_state in {"run", "jog"}:
            self._coordinate_targets.seen_active_state = True
        b_position = axis_value(current_position, self._config.axis_names, "B")
        if b_position is not None:
            self._last_reported_b_position = b_position
        xy_homed = {"X", "Y"}.issubset(observation.homed_axes)
        xyz_homed = {"X", "Y", "Z"}.issubset(observation.homed_axes)
        unhomed_fallback = bool(
            not xy_homed and not self._manual_jog_prediction.prediction_available()
        )
        was_waiting_for_fresh_status = self._planned_waiting_for_fresh_status
        if unhomed_fallback:
            self._planned_stage_xy = None
            self._planned_waiting_for_fresh_status = False
            self._planned_stop_status_timestamp = None
            self._presented_stage_xy = raw_stage_xy
        else:
            self._presented_stage_xy = self._resolved_reported_stage_xy(
                raw_stage_xy,
                status_timestamp=observation.status_timestamp,
            )
        coordinate_position = self._coordinate_targets.stage_position
        if coordinate_active and coordinate_position is not None:
            self._presented_position = coordinate_position
            coordinate_xy = coerce_finite_xy(coordinate_position)
            if coordinate_xy is not None:
                self._presented_stage_xy = coordinate_xy
        else:
            self._presented_position = position_with_stage_xy(
                current_position,
                self._presented_stage_xy,
            )
        clear_motion_axes = latest_state == "idle"
        if clear_motion_axes:
            self._active_axes = frozenset()
        contact_position = (
            (current_position[0], current_position[1], current_position[2])
            if xyz_homed and len(current_position) >= 3
            else None
        )
        self._emit_presentation(
            reported_position=current_position,
            raw_stage_xy=raw_stage_xy,
            contact_calibration_position=contact_position,
            unhomed_fallback=unhomed_fallback,
            clear_motion_axes=clear_motion_axes,
            material_change=(
                material_change
                or (
                    was_waiting_for_fresh_status
                    and not self._planned_waiting_for_fresh_status
                )
            ),
        )
        if latest_state == "idle" and self._coordinate_targets.seen_active_state:
            decision = self._coordinate_targets.finish_if_idle_decision(
                latest_stage_state=latest_state,
                position=current_position,
                monotonic_s=time.monotonic(),
            )
            if decision.finish:
                if decision.stage_position is not None:
                    self._coordinate_targets.stage_position = decision.stage_position
                self._complete_coordinate_move(
                    success=True,
                    disposition=coordinate_types.CoordinateMoveDisposition.COMPLETED,
                    message="",
                    continue_homing=True,
                )
                return
        self._emit_action_state()

    @Slot(object)
    def on_tracked_absolute_xy_move_finished(
        self,
        finished: stage_types.TrackedAbsoluteXYMoveFinished,
    ) -> None:
        self._require_object_thread()
        if not isinstance(finished, stage_types.TrackedAbsoluteXYMoveFinished):
            raise TypeError("finished must be a TrackedAbsoluteXYMoveFinished")
        if not self._owns_planned_motion(finished.motion_token):
            return
        accepted_target = self._accepted_planned_target_xy
        if accepted_target is None or not planned_xy_matches(
            finished.target_stage_xy,
            accepted_target,
            tolerance_mm=self._config.planned_start_tolerance_mm,
        ):
            return
        if not finished.success:
            self._clear_planned_prediction(clear_wait_state=True)
            self._emit_action_state()
            return
        completion_target = accepted_target
        if completion_target is not None:
            self._planned_stage_xy = completion_target
            self._presented_stage_xy = completion_target
            self._presented_position = position_with_stage_xy(
                self._presented_position or self._last_observation.position,
                completion_target,
            )
        self._pending_planned_target_xy = None
        self._pending_planned_source_label = None
        self._planned_origin_xy = None
        self._planned_target_xy = None
        self._planned_started_at = None
        self._planned_ends_at = None
        self._planned_waiting_for_fresh_status = self._planned_stage_xy is not None
        self._planned_stop_status_timestamp = finished.status_timestamp
        self._planned_motion_token = None
        self._accepted_planned_target_xy = None
        self._prediction_timer.stop()
        self._active_axes = frozenset()
        self._emit_action_state()

    @Slot()
    def tick(self) -> None:
        self._require_object_thread()
        if self._coordinate_targets.started_at is not None:
            self._advance_coordinate_prediction(time.monotonic())
            return
        if (
            self._planned_origin_xy is None
            or self._planned_target_xy is None
            or self._planned_started_at is None
            or self._planned_ends_at is None
        ):
            self._prediction_timer.stop()
            return
        progress = motion_progress(
            self._planned_started_at,
            self._planned_ends_at,
            time.monotonic(),
        )
        origin_x, origin_y = self._planned_origin_xy
        target_x, target_y = self._planned_target_xy
        self._planned_stage_xy = (
            float(origin_x + (target_x - origin_x) * progress),
            float(origin_y + (target_y - origin_y) * progress),
        )
        self._presented_stage_xy = self._planned_stage_xy
        self._presented_position = position_with_stage_xy(
            self._presented_position or self._last_observation.position,
            self._planned_stage_xy,
        )
        self._emit_presentation(
            reported_position=self._presented_position,
            raw_stage_xy=self._presented_stage_xy,
            contact_calibration_position=None,
            unhomed_fallback=False,
            clear_motion_axes=False,
        )
        if progress >= 1.0:
            self._planned_waiting_for_fresh_status = True
            self._planned_stop_status_timestamp = None
            self._planned_origin_xy = None
            self._planned_target_xy = None
            self._planned_started_at = None
            self._planned_ends_at = None
            self._prediction_timer.stop()

    def _advance_coordinate_prediction(self, monotonic_s: float) -> None:
        decision = self._coordinate_targets.advance_prediction(
            monotonic_s=float(monotonic_s),
        )
        if decision.clear_tracking:
            self._clear_coordinate_tracking(reset_override=True)
            self._emit_action_state()
            return
        if decision.publish_position is None:
            return
        self._presented_position = decision.publish_position
        coordinate_xy = coerce_finite_xy(decision.publish_position)
        if coordinate_xy is not None:
            self._presented_stage_xy = coordinate_xy
        self._emit_presentation(
            reported_position=decision.publish_position,
            raw_stage_xy=coordinate_xy,
            contact_calibration_position=None,
            unhomed_fallback=False,
            clear_motion_axes=False,
        )

    def _clear_coordinate_tracking(self, *, reset_override: bool) -> None:
        self._coordinate_targets.clear_tracking()
        self._after_coordinate_tracking_cleared(reset_override=reset_override)

    def _after_coordinate_tracking_cleared(self, *, reset_override: bool) -> None:
        self._active_axes = frozenset()
        if reset_override:
            self._controller.queue_feed_override_reset()
        if self._planned_started_at is None:
            self._prediction_timer.stop()

    def _complete_coordinate_move(
        self,
        *,
        success: bool,
        disposition: coordinate_types.CoordinateMoveDisposition,
        message: str,
        continue_homing: bool = False,
    ) -> None:
        completion = self._coordinate_targets.complete(
            success=success,
            disposition=disposition,
            message=message,
        )
        self._after_coordinate_tracking_cleared(reset_override=True)
        self._emit_action_state()
        self.coordinate_move_finished.emit(completion)
        if continue_homing:
            self.continue_homing_requested.emit()

    def _start_planned_prediction(
        self,
        target: tuple[float, float],
        *,
        origin_position: tuple[float, ...],
        source_label: str,
        feedrate_mm_min: float,
    ) -> None:
        _ = source_label
        origin_position = coerce_finite_position(origin_position)
        origin = coerce_finite_xy(origin_position)
        if origin_position is None or origin is None:
            self._clear_planned_prediction(clear_wait_state=True)
            self._emit_action_state()
            return
        timing = planned_xy_timing(
            origin,
            target,
            feedrate_mm_min,
            min_feedrate_mm_min=self._config.min_feedrate_mm_min,
            duration_padding_s=self._config.planned_move_duration_padding_s,
            started_at=time.monotonic(),
        )
        if timing is None:
            self._clear_planned_prediction(clear_wait_state=True)
            self._emit_action_state()
            return
        started_at, ends_at = timing
        self._manual_jog_prediction.clear_waiting_status()
        self._planned_origin_xy = origin
        self._planned_stage_xy = origin
        self._planned_target_xy = target
        self._planned_started_at = started_at
        self._planned_ends_at = ends_at
        self._planned_waiting_for_fresh_status = False
        self._planned_stop_status_timestamp = None
        self._presented_stage_xy = origin
        self._presented_position = position_with_stage_xy(
            origin_position,
            origin,
        )
        self._active_axes = frozenset({"X", "Y"})
        self._emit_presentation(
            reported_position=self._presented_position,
            raw_stage_xy=origin,
            contact_calibration_position=None,
            unhomed_fallback=False,
            clear_motion_axes=False,
        )
        self._emit_action_state()
        if not self._prediction_timer.isActive():
            self._prediction_timer.start()

    def _resolved_reported_stage_xy(
        self,
        raw_stage_xy: tuple[float, float],
        *,
        status_timestamp: float | None,
    ) -> tuple[float, float]:
        held_stage_xy = held_planned_xy(
            self._planned_stage_xy,
            prediction_active=self._planned_started_at is not None,
            waiting_for_fresh_status=self._planned_waiting_for_fresh_status,
            status_timestamp=status_timestamp,
            stop_status_timestamp=self._planned_stop_status_timestamp,
        )
        if held_stage_xy is not None:
            return held_stage_xy
        self._planned_waiting_for_fresh_status = False
        self._planned_stop_status_timestamp = None
        self._planned_stage_xy = raw_stage_xy
        return raw_stage_xy

    def _clear_planned_prediction(self, *, clear_wait_state: bool) -> None:
        self._pending_planned_target_xy = None
        self._pending_planned_source_label = None
        self._planned_origin_xy = None
        self._planned_target_xy = None
        self._planned_started_at = None
        self._planned_ends_at = None
        self._planned_motion_token = None
        self._accepted_planned_target_xy = None
        self._prediction_timer.stop()
        self._active_axes = frozenset()
        if clear_wait_state:
            self._planned_stage_xy = None
            self._planned_waiting_for_fresh_status = False
            self._planned_stop_status_timestamp = None

    def _owns_planned_motion(self, motion_token: object) -> bool:
        return bool(
            self._planned_motion_token is not None
            and motion_token == self._planned_motion_token
        )

    def _presentation(
        self,
        *,
        reported_position: object | None,
        raw_stage_xy: tuple[float, float] | None,
        contact_calibration_position: tuple[float, float, float] | None,
        unhomed_fallback: bool,
        clear_motion_axes: bool,
        material_change: bool = True,
    ) -> motion_types.StageMotionPresentation:
        return motion_types.StageMotionPresentation(
            reported_position=reported_position,
            presented_position=self._presented_position,
            raw_stage_xy=raw_stage_xy,
            presented_stage_xy=(None if unhomed_fallback else self._presented_stage_xy),
            physical_machine_pose=self._physical_machine_pose,
            contact_calibration_position=contact_calibration_position,
            b_position=self._last_reported_b_position,
            active_axes=self._active_axes,
            unhomed_fallback=unhomed_fallback,
            clear_motion_axes=clear_motion_axes,
            motion_coordinate_snapshot=(
                self._last_observation.motion_coordinate_snapshot
            ),
            stage_state=self._last_observation.stage_state,
            homed_axes=self._last_observation.homed_axes,
            status_timestamp=self._last_observation.status_timestamp,
            last_jog_write_timestamp=(self._last_observation.last_jog_write_timestamp),
            material_change=material_change,
        )

    def _emit_presentation(
        self,
        *,
        reported_position: object | None,
        raw_stage_xy: tuple[float, float] | None,
        contact_calibration_position: tuple[float, float, float] | None,
        unhomed_fallback: bool,
        clear_motion_axes: bool,
        material_change: bool = True,
    ) -> None:
        self._last_presentation = self._presentation(
            reported_position=reported_position,
            raw_stage_xy=raw_stage_xy,
            contact_calibration_position=contact_calibration_position,
            unhomed_fallback=unhomed_fallback,
            clear_motion_axes=clear_motion_axes,
            material_change=material_change,
        )
        self.presentation_changed.emit(self._last_presentation)

    def _emit_action_state(self) -> None:
        pending = self._pending_planned_target_xy is not None
        active = self._planned_started_at is not None
        coordinate_active = self._coordinate_targets.has_active_move()
        self.action_state_changed.emit(
            motion_types.StageMotionActionState(
                active_axes=self._active_axes,
                cancelable=bool(pending or active or coordinate_active),
                coordinate_active=coordinate_active,
                planned_pending=pending,
                planned_active=active,
            )
        )

    def _require_object_thread(self) -> None:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("Stage motion session mutation requires its Qt thread.")
