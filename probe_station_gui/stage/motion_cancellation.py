"""Explicit decisions for cancelling transient Stage motion."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StageMotionCancelDecision:
    coordinate_priority: bool = False
    reported_active_motion: bool = False
    cancel_reported_motion: bool = False
    cancel_active_task: bool = False

    @property
    def cancelable(self) -> bool:
        return bool(
            self.coordinate_priority
            or self.cancel_reported_motion
            or self.cancel_active_task
        )

    def action_cancelable(
        self,
        *,
        pending_edits: bool,
        planned_pending: bool,
        planned_active: bool,
    ) -> bool:
        return bool(
            pending_edits or planned_pending or planned_active or self.cancelable
        )


class StageMotionCancellationState:
    """Interpret cached controller activity for the Stage cancellation workflow."""

    def __init__(self, *, active_state_stale_s: float) -> None:
        self._active_state_stale_s = float(active_state_stale_s)

    def decide(
        self,
        *,
        coordinate_active: bool,
        controller_state: object,
        status_timestamp: float | None,
        controller_busy: bool,
        monotonic_s: float,
    ) -> StageMotionCancelDecision:
        reported_active_motion = self.reported_active_motion(
            controller_state,
            status_timestamp=status_timestamp,
            monotonic_s=monotonic_s,
        )
        if coordinate_active:
            return StageMotionCancelDecision(
                coordinate_priority=True,
                reported_active_motion=reported_active_motion,
            )
        if reported_active_motion:
            return StageMotionCancelDecision(
                reported_active_motion=True,
                cancel_reported_motion=True,
            )
        return StageMotionCancelDecision(cancel_active_task=bool(controller_busy))

    def reported_active_motion(
        self,
        controller_state: object,
        *,
        status_timestamp: float | None,
        monotonic_s: float,
    ) -> bool:
        if str(controller_state or "").strip().lower() not in {"run", "jog"}:
            return False
        if status_timestamp is None:
            return True
        try:
            age_s = max(0.0, float(monotonic_s) - float(status_timestamp))
        except (TypeError, ValueError, OverflowError):
            return True
        return age_s <= self._active_state_stale_s
