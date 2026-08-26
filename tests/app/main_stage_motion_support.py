from __future__ import annotations

import types
import time

from probe_station_gui.application.stage_motion_types import StageMotionCancelOutcome
from probe_station_gui.coordinates.model import PhysicalMachinePose
from probe_station_gui.stage.coordinate_targets import (
    CoordinatePendingEdits,
    CoordinateTargetCommonFeedratePlan,
)
from probe_station_gui.stage.exact_step import ExactStepOutcome


class _FakeStageMotion:
    def __init__(
        self,
        controller: object | None = None,
        *,
        axis_names: tuple[str, ...] = ("X", "Y", "Z", "A", "B", "C"),
    ) -> None:
        self.controller = controller
        self.axis_names = axis_names
        self.physical_machine_pose = PhysicalMachinePose.from_mapping({})
        self.cancel_planned_calls = 0
        self.coordinate_active = False
        self.coordinate_display_basis = None
        self.coordinate_display_targets: tuple[tuple[str, float], ...] = ()
        self.coordinate_stage_position: tuple[float, ...] | None = None
        self.coordinate_programmed_feedrate: float | None = None
        self.coordinate_effective_feedrate: float | None = None
        self.coordinate_common_feedrate = CoordinateTargetCommonFeedratePlan(
            clear_common_target=True
        )
        self.active_axes: frozenset[str] = frozenset()
        self.pending: dict[str, tuple[float, float]] = {}
        self.pending_motion_lease: object | None = None
        self.start_requests: list[object] = []
        self.start_result = True
        self.feedrate_updates: list[float] = []
        self.cancel_coordinate_calls = 0
        self.exact_step_display_targets: tuple[tuple[str, float], ...] = ()
        self.exact_step_motion_lease: object | None = None
        self.exact_step_pose_rebase_allowed = False
        self.exact_requests: list[object] = []
        self.exact_clear_reasons: list[object] = []

    def snapshot(self) -> object:
        reported_active_motion = self._reported_active_motion()
        cancelable = (
            self.coordinate_active
            or reported_active_motion
            or bool(self.controller is not None and self.controller.is_busy())
        )
        return types.SimpleNamespace(
            physical_machine_pose=self.physical_machine_pose,
            presented_position=None,
            presented_stage_xy=None,
            active_axes=self.active_axes,
            reported_active_motion=reported_active_motion,
            cancelable=cancelable,
            coordinate_active=self.coordinate_active,
            coordinate_display_basis=self.coordinate_display_basis,
            coordinate_display_targets=self.coordinate_display_targets,
            coordinate_stage_position=self.coordinate_stage_position,
            coordinate_programmed_feedrate=self.coordinate_programmed_feedrate,
            coordinate_effective_feedrate=self.coordinate_effective_feedrate,
            coordinate_common_feedrate=self.coordinate_common_feedrate,
            pending_edit_axes=frozenset(self.pending),
            exact_step_display_targets=self.exact_step_display_targets,
            exact_step_motion_lease=self.exact_step_motion_lease,
            exact_step_pose_rebase_allowed=self.exact_step_pose_rebase_allowed,
            planned_stage_xy=None,
            planned_pending_target_xy=None,
            planned_prediction_active=False,
            planned_waiting_for_fresh_status=False,
        )

    def _reported_active_motion(self) -> bool:
        if self.controller is None:
            return False
        state = (self.controller.latest_stage_state() or "").strip().lower()
        if state not in {"run", "jog"}:
            return False
        timestamp = self.controller.last_status_timestamp()
        return timestamp is None or time.monotonic() - float(timestamp) <= 2.0

    def queue_exact_step(self, request: object) -> ExactStepOutcome:
        pending_targets = tuple(request.pending_targets or request.move_request.targets)
        if not pending_targets:
            return ExactStepOutcome(False, (), self.exact_step_motion_lease, False)
        if self.exact_step_motion_lease is None or request.allow_pose_rebase:
            self.exact_step_motion_lease = request.motion_lease
            self.exact_step_pose_rebase_allowed = bool(request.allow_pose_rebase)
        self.exact_step_display_targets = tuple(
            (str(axis).upper(), float(display))
            for axis, _raw, display in pending_targets
        )
        for axis, raw, display in pending_targets:
            self.upsert_pending_coordinate_edit(
                axis,
                raw,
                display,
                motion_lease=self.exact_step_motion_lease,
            )
        self.exact_requests.append(request)
        return ExactStepOutcome(
            True,
            pending_targets,
            self.exact_step_motion_lease,
            len(self.exact_requests) == 1,
        )

    def clear_exact_steps(self, reason: object) -> None:
        self.exact_clear_reasons.append(reason)
        for axis, _display in self.exact_step_display_targets:
            self.pop_pending_coordinate_edit(axis)
        self.exact_step_display_targets = ()
        self.exact_step_motion_lease = None
        self.exact_step_pose_rebase_allowed = False

    def pending_coordinate_edits(self) -> CoordinatePendingEdits:
        return CoordinatePendingEdits(
            targets=tuple(
                (axis, *self.pending[axis])
                for axis in self.axis_names
                if axis in self.pending
            ),
            motion_lease=self.pending_motion_lease,
        )

    def upsert_pending_coordinate_edit(
        self,
        axis: str,
        raw_target: float,
        display_target: float,
        *,
        motion_lease: object | None,
    ) -> None:
        self.pending[str(axis).upper()] = (float(raw_target), float(display_target))
        self.pending_motion_lease = motion_lease

    def pop_pending_coordinate_edit(self, axis: str) -> tuple[float, float] | None:
        removed = self.pending.pop(str(axis).upper(), None)
        if not self.pending:
            self.pending_motion_lease = None
        return removed

    def clear_pending_coordinate_edits(self) -> bool:
        had_pending = bool(self.pending)
        self.pending.clear()
        self.pending_motion_lease = None
        return had_pending

    def consume_pending_coordinate_edits(self) -> CoordinatePendingEdits:
        pending = self.pending_coordinate_edits()
        self.clear_pending_coordinate_edits()
        return pending

    def start_coordinate_move(self, request: object) -> bool:
        self.start_requests.append(request)
        if self.coordinate_active or not self.start_result:
            return False
        targets = tuple(
            (str(axis).upper(), float(display))
            for axis, _raw, display in request.targets
        )
        raw_targets = {
            str(axis).upper(): float(raw) for axis, raw, _display in request.targets
        }
        for axis in raw_targets:
            self.pending.pop(axis, None)
        if not self.pending:
            self.pending_motion_lease = None
        if self.controller is not None:
            accepted = self.controller.request_absolute_axis_targets_move(
                raw_targets,
                feedrate=float(request.feedrate_mm_min),
            )
            if not accepted:
                return False
        self.coordinate_active = True
        self.active_axes = frozenset(axis for axis, _display in targets)
        self.coordinate_display_targets = targets
        self.coordinate_display_basis = request.display_basis
        self.coordinate_stage_position = request.seed_position
        self.coordinate_programmed_feedrate = float(request.feedrate_mm_min)
        self.coordinate_effective_feedrate = float(request.feedrate_mm_min)
        return True

    def set_coordinate_feedrate(self, feedrate_mm_min: float) -> None:
        self.feedrate_updates.append(float(feedrate_mm_min))

    def discard_coordinate_tracking_for_manual_jog(self) -> bool:
        was_active = self.coordinate_active
        self.coordinate_active = False
        self.active_axes = frozenset()
        return was_active

    def cancel_coordinate_move(self) -> bool:
        if not self.coordinate_active:
            return False
        self.cancel_coordinate_calls += 1
        if self.controller is not None:
            self.controller.cancel_active_motion("Coordinate move cancel requested.")
        self.coordinate_active = False
        self.active_axes = frozenset()
        self.clear_pending_coordinate_edits()
        return True

    def cancel_stage_motion(self) -> StageMotionCancelOutcome:
        coordinate_priority = self.coordinate_active
        cancelled = False
        if coordinate_priority:
            cancelled = self.cancel_coordinate_move()
        elif self.controller is not None:
            state = (self.controller.latest_stage_state() or "").strip().lower()
            timestamp = self.controller.last_status_timestamp()
            fresh_motion = state in {"run", "jog"} and (
                timestamp is None or time.monotonic() - float(timestamp) <= 2.0
            )
            if fresh_motion:
                self.controller.cancel_active_motion("Motion cancel requested.")
                cancelled = True
            elif self.controller.is_busy():
                self.controller.cancel_active_task("Operation cancel requested.")
                cancelled = True
            if cancelled:
                self.active_axes = frozenset()
                self.cancel_planned_xy_move()
        pending_edits_cleared = self.clear_pending_coordinate_edits()
        return StageMotionCancelOutcome(
            stage_motion_cancelled=cancelled,
            coordinate_priority=coordinate_priority,
            pending_edits_cleared=pending_edits_cleared,
        )

    def cancel_planned_xy_move(self) -> bool:
        self.cancel_planned_calls += 1
        return True
