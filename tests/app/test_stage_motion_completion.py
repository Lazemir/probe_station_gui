from __future__ import annotations

from probe_station_gui.application.stage_motion_completion import (
    StageMotionCompletion,
)
from probe_station_gui.stage.coordinate_targets import (
    CoordinateMovementCompletionDecision,
)


class _CoordinateState:
    def claim_movement_finished(
        self,
        success: bool,
        message: str,
    ) -> CoordinateMovementCompletionDecision:
        return CoordinateMovementCompletionDecision(False, success, message)


def _completion(controller: _Controller) -> StageMotionCompletion:
    return StageMotionCompletion(controller, _CoordinateState())


class _Controller:
    def __init__(self, *, accepted: bool) -> None:
        self.accepted = accepted
        self.rotation_requests: list[float] = []

    def request_rotate_b(self, delta_deg: float) -> bool:
        self.rotation_requests.append(delta_deg)
        return self.accepted


def test_rejected_alignment_does_not_replace_click_owner() -> None:
    controller = _Controller(accepted=False)
    completion = _completion(controller)
    completion.arm_click()

    assert completion.request_alignment_rotation(2.5, object()) is False

    effects = completion.resolve(True, "click")
    assert controller.rotation_requests == [2.5]
    assert effects.click_success is True
    assert effects.status_message == "click"
    assert effects.schedule_settle is True
    assert effects.emit_action_state is True
    assert effects.alignment is None


def test_accepted_alignment_exclusively_owns_next_completion() -> None:
    controller = _Controller(accepted=True)
    completion = _completion(controller)
    correlation = object()
    completion.arm_click()

    assert completion.request_alignment_rotation(3.0, correlation) is True

    effects = completion.resolve(False, "opaque")
    assert effects.click_success is None
    assert effects.status_message is None
    assert effects.schedule_settle is False
    assert effects.emit_action_state is False
    assert effects.alignment is not None
    assert effects.alignment.correlation is correlation
    assert effects.alignment.success is False


def test_discard_alignment_preserves_other_completion_owner() -> None:
    completion = _completion(_Controller(accepted=True))
    completion.arm_click()

    assert completion.discard_alignment_rotation() is False
    assert completion.resolve(True, "done").click_success is True


def test_clear_removes_armed_completion_owner() -> None:
    completion = _completion(_Controller(accepted=True))
    assert completion.request_alignment_rotation(1.0, object()) is True

    completion.clear()

    effects = completion.resolve(True, "late")
    assert effects.click_success is None
    assert effects.alignment is None
    assert effects.status_message == "late"
    assert effects.schedule_settle is True
    assert effects.emit_action_state is True
