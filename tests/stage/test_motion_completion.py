from probe_station_gui.stage.motion_completion import (
    MotionCompletionKind,
    MotionCompletionState,
)


def test_ordinary_claim_has_no_correlation() -> None:
    claim = MotionCompletionState().claim_untracked(True, "done")
    assert claim.kind is MotionCompletionKind.ORDINARY
    assert claim.correlation is None


def test_latest_armed_workflow_exclusively_owns_completion() -> None:
    state = MotionCompletionState()
    correlation = object()
    state.arm_click()
    state.arm_alignment(correlation)
    claim = state.claim_untracked(False, "opaque")
    assert claim.kind is MotionCompletionKind.ALIGNMENT
    assert claim.correlation is correlation
    assert state.claim_untracked(True, "late").kind is MotionCompletionKind.ORDINARY


def test_clear_discards_armed_workflow() -> None:
    state = MotionCompletionState()
    state.arm_click()
    state.clear()
    assert state.claim_untracked(True, "late").kind is MotionCompletionKind.ORDINARY


def test_discard_alignment_clears_only_alignment_purpose() -> None:
    state = MotionCompletionState()
    state.arm_click()
    assert state.discard_alignment() is False
    assert state.claim_untracked(True, "click").kind is MotionCompletionKind.CLICK
    state.arm_alignment(object())
    assert state.alignment_pending() is True
    assert state.discard_alignment() is True
    assert state.alignment_pending() is False
    assert state.claim_untracked(True, "late").kind is MotionCompletionKind.ORDINARY
