"""Read-model projection for the Stage motion session."""

from __future__ import annotations

from probe_station_gui.application.stage_motion_cancellation import (
    StageMotionCancellation,
)
from probe_station_gui.application.stage_motion_types import StageMotionSnapshot
from probe_station_gui.application.stage_position_state import StagePositionState
from probe_station_gui.stage.coordinate_targets import CoordinateTargetMoveState
from probe_station_gui.stage.exact_step import ExactStepWorkflowState
from probe_station_gui.stage.manual_jog_prediction import ManualJogPredictionState
from probe_station_gui.stage.planned_xy import PlannedXYMotionState


class StageMotionReadModel:
    def __init__(
        self,
        *,
        axis_names: tuple[str, ...],
        cancellation: StageMotionCancellation,
        coordinate_targets: CoordinateTargetMoveState,
        exact_steps: ExactStepWorkflowState,
        manual_jog_prediction: ManualJogPredictionState,
        planned_xy: PlannedXYMotionState,
        position: StagePositionState,
    ) -> None:
        self._axis_names = axis_names
        self._cancellation = cancellation
        self._coordinates = coordinate_targets
        self._exact_steps = exact_steps
        self._manual_jog_prediction = manual_jog_prediction
        self._planned_xy = planned_xy
        self._position = position

    def snapshot(self, *, monotonic_s: float) -> StageMotionSnapshot:
        cancellation = self._cancellation.decide_current(
            coordinate_active=self._coordinates.has_active_move(),
            monotonic_s=monotonic_s,
        )
        return StageMotionSnapshot(
            presented_position=self._position.presented_position,
            presented_stage_xy=self._position.presented_stage_xy,
            physical_machine_pose=self._position.physical_machine_pose,
            active_axes=self._position.active_axes,
            reported_active_motion=cancellation.reported_active_motion,
            cancelable=cancellation.cancelable,
            coordinate_active=cancellation.coordinate_priority,
            coordinate_display_basis=self._coordinates.display_basis,
            coordinate_display_targets=tuple(
                (axis, float(self._coordinates.display_targets[axis]))
                for axis in self._axis_names
                if axis in self._coordinates.display_targets
            ),
            coordinate_stage_position=self._coordinates.stage_position,
            coordinate_programmed_feedrate=(self._coordinates.programmed_feedrate),
            coordinate_effective_feedrate=self._coordinates.effective_feedrate,
            coordinate_common_feedrate=self._coordinates.common_feedrate,
            pending_edit_axes=frozenset(self._coordinates.pending_edits),
            manual_prediction_active=(self._manual_jog_prediction.prediction_active()),
            manual_prediction_available=(
                self._manual_jog_prediction.prediction_available()
            ),
            planned_pending_target_xy=self._planned_xy.pending_target_xy,
            planned_pending_source_label=self._planned_xy.pending_source_label,
            planned_stage_xy=self._planned_xy.stage_xy,
            planned_prediction_active=self._planned_xy.started_at is not None,
            planned_waiting_for_fresh_status=(
                self._planned_xy.waiting_for_fresh_status
            ),
            last_reported_b_position=self._position.last_reported_b_position,
            exact_step_display_targets=tuple(
                (axis, float(self._exact_steps.accumulator.targets[axis]))
                for axis in self._axis_names
                if axis in self._exact_steps.accumulator.targets
            ),
            exact_step_motion_lease=self._exact_steps.motion_lease,
            exact_step_pose_rebase_allowed=self._exact_steps.pose_rebase_allowed,
        )
