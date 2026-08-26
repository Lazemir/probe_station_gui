"""Application adapter for Stage cancellation activity sources."""

from __future__ import annotations

from probe_station_gui.application.stage_motion_types import StageMotionActionState
from probe_station_gui.stage.controller import StageController
from probe_station_gui.stage.motion_cancellation import (
    StageMotionCancelDecision,
    StageMotionCancellationState,
)


class StageMotionCancellation:
    """Classify synchronized controller facts for Stage cancellation."""

    def __init__(
        self, controller: StageController, *, active_state_stale_s: float
    ) -> None:
        self._controller = controller
        self._state = StageMotionCancellationState(
            active_state_stale_s=active_state_stale_s
        )

    def decide_current(
        self,
        *,
        coordinate_active: bool,
        monotonic_s: float,
    ) -> StageMotionCancelDecision:
        return self._state.decide(
            coordinate_active=coordinate_active,
            controller_state=self._controller.latest_stage_state(),
            status_timestamp=self._controller.last_status_timestamp(),
            controller_busy=self._controller.is_busy(),
            monotonic_s=monotonic_s,
        )

    def action_state(
        self,
        *,
        active_axes: frozenset[str],
        coordinate_active: bool,
        pending_edits: bool,
        planned_pending: bool,
        planned_active: bool,
        monotonic_s: float,
    ) -> StageMotionActionState:
        decision = StageMotionCancelDecision()
        if not (pending_edits or planned_pending or planned_active):
            decision = self.decide_current(
                coordinate_active=coordinate_active,
                monotonic_s=monotonic_s,
            )
        return StageMotionActionState(
            active_axes=active_axes,
            cancelable=decision.action_cancelable(
                pending_edits=pending_edits,
                planned_pending=planned_pending,
                planned_active=planned_active,
            ),
            coordinate_active=coordinate_active,
            planned_pending=planned_pending,
            planned_active=planned_active,
        )
