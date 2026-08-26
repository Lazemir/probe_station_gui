"""Application ownership for untracked Stage motion completion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from probe_station_gui.application.stage_motion_types import (
    AlignmentRotationCompletion,
)
from probe_station_gui.stage.motion_completion import (
    MotionCompletionKind,
    MotionCompletionState,
)
from probe_station_gui.stage.coordinate_targets import (
    CoordinateMovementCompletionDecision,
    CoordinateMoveDisposition,
)


class _RotationController(Protocol):
    def request_rotate_b(self, delta_deg: float) -> bool: ...


class _CoordinateCompletionState(Protocol):
    def claim_movement_finished(
        self,
        success: bool,
        message: str,
    ) -> CoordinateMovementCompletionDecision: ...


@dataclass(frozen=True)
class StageMotionCompletionEffects:
    click_success: bool | None = None
    alignment: AlignmentRotationCompletion | None = None
    status_message: str | None = None
    schedule_settle: bool = False
    emit_action_state: bool = False
    coordinate_disposition: CoordinateMoveDisposition | None = None
    coordinate_success: bool = False


class StageMotionCompletion:
    def __init__(
        self,
        controller: _RotationController,
        coordinate_state: _CoordinateCompletionState,
    ) -> None:
        self._controller = controller
        self._coordinate_state = coordinate_state
        self._state = MotionCompletionState()

    def arm_click(self) -> None:
        self._state.arm_click()

    def request_alignment_rotation(
        self,
        delta_deg: float,
        correlation: object,
    ) -> bool:
        accepted = bool(self._controller.request_rotate_b(float(delta_deg)))
        if accepted:
            self._state.arm_alignment(correlation)
        return accepted

    def discard_alignment_rotation(self) -> bool:
        return self._state.discard_alignment()

    def alignment_rotation_pending(self) -> bool:
        return self._state.alignment_pending()

    def clear(self) -> None:
        self._state.clear()

    def resolve(
        self,
        success: bool,
        message: str,
    ) -> StageMotionCompletionEffects:
        decision = self._coordinate_state.claim_movement_finished(success, message)
        if decision.claimed:
            return self._resolve_coordinate(decision)
        return self._resolve_untracked(success, message)

    @staticmethod
    def _resolve_coordinate(
        decision: CoordinateMovementCompletionDecision,
    ) -> StageMotionCompletionEffects:
        if decision.expected_reissue_cancel:
            return StageMotionCompletionEffects(
                schedule_settle=True,
                emit_action_state=True,
            )
        return StageMotionCompletionEffects(
            coordinate_disposition=decision.disposition,
            coordinate_success=decision.success,
            status_message=decision.message or None,
            schedule_settle=decision.success,
        )

    def _resolve_untracked(
        self,
        success: bool,
        message: str,
    ) -> StageMotionCompletionEffects:
        claim = self._state.claim_untracked(success, message)
        if claim.kind is MotionCompletionKind.CLICK:
            return StageMotionCompletionEffects(
                click_success=claim.success,
                status_message=claim.message or None,
                schedule_settle=claim.success,
                emit_action_state=True,
            )
        if claim.kind is MotionCompletionKind.ALIGNMENT:
            return StageMotionCompletionEffects(
                alignment=AlignmentRotationCompletion(
                    correlation=claim.correlation,
                    success=claim.success,
                    message=claim.message,
                )
            )
        return StageMotionCompletionEffects(
            status_message=claim.message or None,
            schedule_settle=claim.success,
            emit_action_state=True,
        )
