"""GUI-thread owner for transient Stage motion interpretation."""

from __future__ import annotations

import time

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from probe_station_gui.application.stage_motion_settle_polls import (
    _StageMotionSettleStatusPolls,
)
from probe_station_gui.application.stage_motion_cancellation import (
    StageMotionCancellation,
)
from probe_station_gui.application.stage_motion_completion import (
    StageMotionCompletion,
)
from probe_station_gui.application.stage_motion_read_model import StageMotionReadModel
from probe_station_gui.application.stage_position_state import StagePositionState
from probe_station_gui.application import stage_motion_types as motion_types
from probe_station_gui.stage import coordinate_targets as coordinate_types
from probe_station_gui.stage.controller import StageController
from probe_station_gui.stage.coordinate_targets import (
    CoordinateTargetMoveState,
    plan_coordinate_target_start,
    stage_axis_target_limit_error,
)
from probe_station_gui.stage import exact_step as exact_types
from probe_station_gui.stage.manual_jog_prediction import ManualJogPredictionState
from probe_station_gui.stage.planned_xy import PlannedXYMotionState
from probe_station_gui.stage.motion_prediction import (
    coerce_finite_xy,
    position_with_stage_xy,
)
from probe_station_gui.stage import types as stage_types


class _StageMotionSession(QObject):
    presentation_changed = Signal(object)
    action_state_changed = Signal(object)
    alignment_rotation_finished = Signal(object)
    click_move_finished = Signal(bool)
    coordinate_move_finished = Signal(object)
    continue_homing_requested = Signal()
    status_requested = Signal(str, int)
    terminal_live_poll_paused_changed = Signal(bool)

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
        self._planned_xy = PlannedXYMotionState()
        self._position = StagePositionState(config.axis_names)
        self._exact_steps = exact_types.ExactStepWorkflowState(config.axis_names)
        self._cancellation = StageMotionCancellation(
            controller, active_state_stale_s=config.controller_active_state_stale_s
        )
        self._read_model = StageMotionReadModel(
            axis_names=config.axis_names,
            cancellation=self._cancellation,
            coordinate_targets=self._coordinate_targets,
            exact_steps=self._exact_steps,
            manual_jog_prediction=self._manual_jog_prediction,
            planned_xy=self._planned_xy,
            position=self._position,
        )
        self._prediction_timer = QTimer(self)
        self._prediction_timer.setInterval(int(config.prediction_interval_ms))
        self._prediction_timer.timeout.connect(self.tick)
        self._exact_step_timer = QTimer(self)
        self._exact_step_timer.setSingleShot(True)
        self._exact_step_timer.setInterval(int(config.exact_step_accumulation_ms))
        self._exact_step_timer.timeout.connect(self._on_exact_step_window_elapsed)
        self._terminal_resume_timer = QTimer(self)
        self._terminal_resume_timer.setSingleShot(True)
        self._terminal_resume_timer.setInterval(
            int(config.terminal_resume_after_jog_ms)
        )
        self._terminal_resume_timer.timeout.connect(self._resume_terminal_live_poll)
        self._settle_status_polls = _StageMotionSettleStatusPolls(
            self,
            delays_ms=config.settle_status_poll_delays_ms,
            request_status_refresh=controller.request_status_refresh,
        )

        self._terminal_live_poll_paused = False
        self._completion = StageMotionCompletion(controller, self._coordinate_targets)
        self._position.last_presentation = self._position.record_presentation(
            reported_position=None,
            raw_stage_xy=None,
            contact_calibration_position=None,
            unhomed_fallback=False,
            clear_motion_axes=False,
        )

    def snapshot(self) -> motion_types.StageMotionSnapshot:
        return self._read_model.snapshot(monotonic_s=time.monotonic())

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

    def on_manual_jog_command(
        self,
        commanded_distances: tuple[tuple[str, float], ...],
        feedrate_mm_min: float,
    ) -> None:
        self._require_object_thread()
        if not isinstance(commanded_distances, tuple):
            return
        self.clear_exact_steps(exact_types.ExactStepClearReason.MANUAL_JOG_STARTED)
        self.cancel_planned_xy_move()
        self._set_terminal_live_poll_paused(True)
        coordinate_active = self._coordinate_targets.has_active_move()
        latest_position = self._controller.latest_stage_position()
        seed_position = (
            latest_position
            if latest_position is not None
            else self._position.presented_position
        )
        result = self._manual_jog_prediction.handle_command(
            commanded_distances,
            feedrate=float(feedrate_mm_min),
            now=time.monotonic(),
            coordinate_move_active=coordinate_active,
            coordinate_move_stage_position=(
                self._coordinate_targets.stage_position if coordinate_active else None
            ),
            latest_stage_position=seed_position,
            current_design_stage_xy=self._position.presented_stage_xy,
        )
        if not result.handled:
            return
        if result.zero_distance:
            self._position.active_axes = frozenset()
            self._stop_prediction_timer_if_idle()
            self._emit_presentation(
                reported_position=self._position.presented_position,
                raw_stage_xy=self._position.presented_stage_xy,
                contact_calibration_position=None,
                unhomed_fallback=False,
                clear_motion_axes=True,
            )
            self._settle_status_polls.schedule()
            self._emit_action_state()
            return
        if result.clear_coordinate_move_tracking:
            self._clear_coordinate_tracking(reset_override=False)
        self._position.active_axes = result.motion_axes
        if result.publish_position is not None:
            self._position.presented_position = result.publish_position
            predicted_xy = coerce_finite_xy(result.publish_position)
            if predicted_xy is not None:
                self._position.presented_stage_xy = predicted_xy
            self._emit_presentation(
                reported_position=result.publish_position,
                raw_stage_xy=predicted_xy,
                contact_calibration_position=None,
                unhomed_fallback=False,
                clear_motion_axes=False,
            )
        self._emit_action_state()
        if result.start_timer and not self._prediction_timer.isActive():
            self._prediction_timer.start()

    def on_manual_jog_stopped(self) -> None:
        self._require_object_thread()
        result = self._manual_jog_prediction.handle_stop(
            now=time.monotonic(),
            last_status_timestamp=self._controller.last_status_timestamp(),
        )
        if result.start_timer and not self._prediction_timer.isActive():
            self._prediction_timer.start()
        if result.stop_timer:
            self._stop_prediction_timer_if_idle()
        if result.resume_live_poll:
            self._terminal_resume_timer.start()
        if result.schedule_status_refreshes:
            self._settle_status_polls.schedule()
        self._emit_action_state()

    def queue_exact_step(
        self,
        request: exact_types.ExactStepRequest,
    ) -> exact_types.ExactStepOutcome:
        self._require_object_thread()
        decision = self._exact_steps.queue(
            request,
            window_active=self._exact_step_timer.isActive(),
        )
        for axis in decision.removed_axes:
            self._coordinate_targets.pop_pending_edit(axis)
        for axis, raw_target, display_target in decision.outcome.pending_targets:
            self._coordinate_targets.upsert_pending_edit(
                axis,
                raw_target,
                display_target,
                motion_lease=decision.outcome.motion_lease,
            )
        if decision.outcome.window_started:
            self._exact_step_timer.start()
        self._emit_action_state()
        return decision.outcome

    def dispatch_exact_steps(self) -> bool:
        self._require_object_thread()
        decision = self._exact_steps.begin_dispatch(
            coordinate_active=self._coordinate_targets.has_active_move(),
            controller_busy=self._controller.is_busy(),
        )
        if decision.clear_workflow:
            self._clear_exact_step_state()
            return False
        if decision.move_request is None:
            return False
        accepted = self.start_coordinate_move(decision.move_request)
        if not accepted:
            self._clear_exact_step_state()
            return False
        self._exact_steps.mark_move_started(decision.move_request)
        return True

    def clear_exact_steps(
        self,
        reason: exact_types.ExactStepClearReason,
    ) -> None:
        self._require_object_thread()
        if not isinstance(reason, exact_types.ExactStepClearReason):
            raise TypeError("reason must be an ExactStepClearReason")
        self._clear_exact_step_state()
        self._emit_action_state()

    @Slot()
    def _on_exact_step_window_elapsed(self) -> None:
        self._exact_steps.mark_window_elapsed()
        self.dispatch_exact_steps()

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

    def cancel_stage_motion(self) -> motion_types.StageMotionCancelOutcome:
        """Cancel only motion workflows owned by this Stage session."""

        self._require_object_thread()
        had_pending_edits = bool(self._coordinate_targets.pending_edits)
        self.clear_exact_steps(exact_types.ExactStepClearReason.CANCEL_REQUESTED)
        decision = self._cancellation.decide_current(
            coordinate_active=self._coordinate_targets.has_active_move(),
            monotonic_s=time.monotonic(),
        )
        if decision.coordinate_priority:
            self._controller.cancel_active_motion("Coordinate move cancel requested.")
            self._clear_coordinate_tracking(reset_override=True)
            pending_edits_cleared = bool(
                self._coordinate_targets.clear_pending_edits() or had_pending_edits
            )
            self._settle_status_polls.schedule()
            self._emit_action_state()
            return motion_types.StageMotionCancelOutcome(
                stage_motion_cancelled=True,
                coordinate_priority=True,
                pending_edits_cleared=pending_edits_cleared,
            )

        cancelled = False
        if decision.cancel_reported_motion:
            self._controller.cancel_active_motion("Motion cancel requested.")
            cancelled = True
        elif decision.cancel_active_task:
            self._controller.cancel_active_task("Operation cancel requested.")
            cancelled = True
        if cancelled:
            self._position.active_axes = frozenset()
            self.cancel_planned_xy_move()
            self._settle_status_polls.schedule()
        pending_edits_cleared = bool(
            self._coordinate_targets.clear_pending_edits() or had_pending_edits
        )
        self._emit_action_state()
        return motion_types.StageMotionCancelOutcome(
            stage_motion_cancelled=cancelled,
            coordinate_priority=False,
            pending_edits_cleared=pending_edits_cleared,
        )

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
                homed_axes=set(self._position.last_observation.homed_axes),
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
        self._position.active_axes = frozenset(plan.active_axes)
        accepted = bool(
            self._controller.request_absolute_axis_targets_move(
                plan.raw_targets,
                feedrate=plan.feedrate_mm_min,
            )
        )
        if not accepted:
            self._clear_coordinate_tracking(reset_override=True)
            return False
        self._completion.clear()
        self._coordinate_targets.arm_purpose()
        self._position.presented_position = plan.publish_position
        self._position.presented_stage_xy = coerce_finite_xy(plan.publish_position)
        self.status_requested.emit(plan.status.message, plan.status.timeout_ms)
        self._emit_presentation(
            reported_position=plan.publish_position,
            raw_stage_xy=self._position.presented_stage_xy,
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
        effects = self._completion.resolve(success, message)
        if effects.click_success is not None:
            self.click_move_finished.emit(effects.click_success)
        if effects.alignment is not None:
            self.alignment_rotation_finished.emit(effects.alignment)
        if effects.coordinate_disposition is not None:
            self._complete_coordinate_move(
                success=effects.coordinate_success,
                disposition=effects.coordinate_disposition,
                message=effects.status_message or "",
            )
        if effects.status_message:
            self.status_requested.emit(effects.status_message, 5000)
        if effects.schedule_settle:
            self._settle_status_polls.schedule()
        if effects.emit_action_state:
            self._emit_action_state()

    def request_planned_xy_move(
        self,
        request: motion_types.PlannedXYMoveRequest,
    ) -> bool:
        self._require_object_thread()
        decision = self._planned_xy.prepare_request(
            request.target_stage_xy,
            request.source_label,
        )
        if decision is None:
            return False
        accepted = bool(
            self._controller.request_move_to_xy(
                decision.target_stage_xy[0],
                decision.target_stage_xy[1],
                motion_token=decision.motion_token,
            )
        )
        if not accepted:
            self._clear_planned_prediction(clear_wait_state=True)
            return False
        self._planned_xy.accept_request(decision)
        self._emit_action_state()
        return True

    def _planned_request_in_flight(self) -> bool:
        return self._planned_xy.request_in_flight()

    def cancel_planned_xy_move(self) -> bool:
        """Clear only planned-XY interpretation; callers retain motion policy."""

        self._require_object_thread()
        if not self._planned_request_in_flight():
            return False
        self._clear_planned_prediction(clear_wait_state=True)
        self._emit_presentation(
            reported_position=self._position.presented_position,
            raw_stage_xy=self._position.presented_stage_xy,
            contact_calibration_position=None,
            unhomed_fallback=False,
            clear_motion_axes=False,
        )
        self._emit_action_state()
        return True

    def refresh_physical_machine_pose(self) -> None:
        """Refresh the owned pose from the controller's synchronized cache."""

        self._require_object_thread()
        presentation = self._position.refresh_pose(
            self._controller.latest_physical_machine_pose(self._config.axis_names)
        )
        self.presentation_changed.emit(presentation)

    def reset(self, reason: stage_types.StageMotionResetReason) -> None:
        """Reset state owned by the current planned/position slice."""

        self._require_object_thread()
        if not isinstance(reason, stage_types.StageMotionResetReason):
            raise TypeError("reason must be a StageMotionResetReason")
        self._settle_status_polls.clear()
        self._terminal_resume_timer.stop()
        self._set_terminal_live_poll_paused(False)
        self._manual_jog_prediction.reset_tracking()
        self._completion.clear()
        self._clear_exact_step_state()
        self._clear_coordinate_tracking(reset_override=False)
        self._coordinate_targets.clear_pending_edits()
        self._clear_planned_prediction(clear_wait_state=True)
        self.presentation_changed.emit(self._position.reset())
        self._emit_action_state()

    @Slot(float, float, float)
    def on_click_move_started(
        self,
        _x_mm: float,
        _y_mm: float,
        _feedrate_mm_min: float,
    ) -> None:
        self._require_object_thread()
        self._completion.arm_click()

    def request_alignment_rotation(
        self,
        delta_deg: float,
        correlation: object,
    ) -> bool:
        self._require_object_thread()
        return self._completion.request_alignment_rotation(delta_deg, correlation)

    def discard_alignment_rotation(self) -> bool:
        self._require_object_thread()
        return self._completion.discard_alignment_rotation()

    def alignment_rotation_pending(self) -> bool:
        return self._completion.alignment_rotation_pending()

    @Slot(object)
    def on_tracked_absolute_xy_move_started(
        self,
        started: stage_types.TrackedAbsoluteXYMoveStarted,
    ) -> None:
        self._require_object_thread()
        if not isinstance(started, stage_types.TrackedAbsoluteXYMoveStarted):
            raise TypeError("started must be a TrackedAbsoluteXYMoveStarted")
        decision = self._planned_xy.start(
            started,
            tolerance_mm=self._config.planned_start_tolerance_mm,
            min_feedrate_mm_min=self._config.min_feedrate_mm_min,
            duration_padding_s=self._config.planned_move_duration_padding_s,
            monotonic_s=time.monotonic(),
        )
        if not decision.handled:
            return
        if decision.clear_workflow:
            self._clear_planned_prediction(clear_wait_state=True)
            self._emit_action_state()
            return
        self._manual_jog_prediction.clear_waiting_status()
        self._position.presented_stage_xy = decision.stage_xy
        self._position.presented_position = decision.publish_position
        self._position.active_axes = frozenset({"X", "Y"})
        self._emit_presentation(
            reported_position=decision.publish_position,
            raw_stage_xy=decision.stage_xy,
            contact_calibration_position=None,
            unhomed_fallback=False,
            clear_motion_axes=False,
        )
        self._emit_action_state()
        if decision.start_timer and not self._prediction_timer.isActive():
            self._prediction_timer.start()

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
        decision = self._position.observe(
            observation,
            coordinate_targets=self._coordinate_targets,
            manual_prediction=self._manual_jog_prediction,
            planned_xy=self._planned_xy,
            monotonic_s=time.monotonic(),
        )
        self.presentation_changed.emit(decision.presentation)
        if decision.deferred or decision.current_position is None:
            self._emit_action_state()
            return
        if decision.coordinate_idle_candidate:
            completion = self._coordinate_targets.finish_if_idle_decision(
                latest_stage_state=decision.latest_state,
                position=decision.current_position,
                monotonic_s=time.monotonic(),
            )
            if completion.finish:
                if completion.stage_position is not None:
                    self._coordinate_targets.stage_position = completion.stage_position
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
        decision = self._planned_xy.finish(
            finished,
            tolerance_mm=self._config.planned_start_tolerance_mm,
        )
        if not decision.handled:
            return
        if decision.clear_workflow:
            self._clear_planned_prediction(clear_wait_state=True)
            self._emit_action_state()
            return
        if decision.stage_xy is not None:
            self._position.presented_stage_xy = decision.stage_xy
            self._position.presented_position = position_with_stage_xy(
                self._position.presented_position
                or self._position.last_observation.position,
                decision.stage_xy,
            )
        if decision.stop_timer:
            self._prediction_timer.stop()
        self._position.active_axes = frozenset()
        self._emit_action_state()

    @Slot()
    def tick(self) -> None:
        self._require_object_thread()
        if self._coordinate_targets.started_at is not None:
            self._advance_coordinate_prediction(time.monotonic())
            return
        now = time.monotonic()
        if self._manual_jog_prediction.prediction_active(now):
            result = self._manual_jog_prediction.advance(
                now=now,
                coordinate_move_stage_position=None,
                latest_stage_position=self._controller.latest_stage_position(),
                current_design_stage_xy=self._position.presented_stage_xy,
            )
            if result.publish_position is not None:
                self._position.presented_position = result.publish_position
                predicted_xy = coerce_finite_xy(result.publish_position)
                if predicted_xy is not None:
                    self._position.presented_stage_xy = predicted_xy
                self._emit_presentation(
                    reported_position=result.publish_position,
                    raw_stage_xy=predicted_xy,
                    contact_calibration_position=None,
                    unhomed_fallback=False,
                    clear_motion_axes=False,
                )
            if result.stop_timer:
                self._stop_prediction_timer_if_idle()
            return
        decision = self._planned_xy.advance(now)
        if decision.publish_stage_xy is None:
            if decision.stop_timer:
                self._prediction_timer.stop()
            return
        self._position.presented_stage_xy = decision.publish_stage_xy
        self._position.presented_position = position_with_stage_xy(
            self._position.presented_position
            or self._position.last_observation.position,
            decision.publish_stage_xy,
        )
        self._emit_presentation(
            reported_position=self._position.presented_position,
            raw_stage_xy=self._position.presented_stage_xy,
            contact_calibration_position=None,
            unhomed_fallback=False,
            clear_motion_axes=False,
        )
        if decision.stop_timer:
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
        self._position.presented_position = decision.publish_position
        coordinate_xy = coerce_finite_xy(decision.publish_position)
        if coordinate_xy is not None:
            self._position.presented_stage_xy = coordinate_xy
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
        self._position.active_axes = frozenset()
        if reset_override:
            self._controller.queue_feed_override_reset()
        if self._planned_xy.started_at is None:
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
        self._handle_exact_step_completion(completion)
        self.coordinate_move_finished.emit(completion)
        if continue_homing:
            self.continue_homing_requested.emit()

    def _resolved_reported_stage_xy(
        self,
        raw_stage_xy: tuple[float, float],
        *,
        status_timestamp: float | None,
    ) -> tuple[float, float]:
        return self._planned_xy.resolve_reported_stage_xy(
            raw_stage_xy,
            status_timestamp=status_timestamp,
        )

    def _clear_planned_prediction(self, *, clear_wait_state: bool) -> None:
        self._planned_xy.clear(clear_wait_state=clear_wait_state)
        self._prediction_timer.stop()
        self._position.active_axes = frozenset()

    def _clear_exact_step_state(self) -> None:
        self._exact_step_timer.stop()
        for axis in self._exact_steps.clear():
            self._coordinate_targets.pop_pending_edit(axis)

    def _handle_exact_step_completion(
        self,
        completion: coordinate_types.CoordinateMoveCompletion,
    ) -> None:
        decision = self._exact_steps.complete(completion)
        for axis in decision.removed_axes:
            self._coordinate_targets.pop_pending_edit(axis)
        if decision.clear_workflow:
            self._clear_exact_step_state()
            self._emit_action_state()
            return
        if decision.dispatch_followup and not self._exact_step_timer.isActive():
            self.dispatch_exact_steps()

    def _set_terminal_live_poll_paused(self, paused: bool) -> None:
        normalized = bool(paused)
        if normalized == self._terminal_live_poll_paused:
            return
        self._terminal_live_poll_paused = normalized
        self.terminal_live_poll_paused_changed.emit(normalized)

    @Slot()
    def _resume_terminal_live_poll(self) -> None:
        self._set_terminal_live_poll_paused(False)

    def _stop_prediction_timer_if_idle(self) -> None:
        if self._coordinate_targets.started_at is not None:
            return
        if self._manual_jog_prediction.prediction_active():
            return
        if self._planned_xy.started_at is not None:
            return
        self._prediction_timer.stop()

    def _owns_planned_motion(self, motion_token: object) -> bool:
        return self._planned_xy.owns_motion(motion_token)

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
        presentation = self._position.record_presentation(
            reported_position=reported_position,
            raw_stage_xy=raw_stage_xy,
            contact_calibration_position=contact_calibration_position,
            unhomed_fallback=unhomed_fallback,
            clear_motion_axes=clear_motion_axes,
            material_change=material_change,
        )
        self.presentation_changed.emit(presentation)

    def _emit_action_state(self) -> None:
        self.action_state_changed.emit(
            self._cancellation.action_state(
                active_axes=self._position.active_axes,
                coordinate_active=self._coordinate_targets.has_active_move(),
                pending_edits=bool(self._coordinate_targets.pending_edits),
                planned_pending=self._planned_xy.pending_target_xy is not None,
                planned_active=self._planned_xy.started_at is not None,
                monotonic_s=time.monotonic(),
            )
        )

    def _require_object_thread(self) -> None:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("Stage motion session mutation requires its Qt thread.")
