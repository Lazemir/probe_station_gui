from __future__ import annotations

import dataclasses

import pytest

from probe_station_gui.stage.motion_cancellation import (
    StageMotionCancelDecision,
    StageMotionCancellationState,
)


def _state() -> StageMotionCancellationState:
    return StageMotionCancellationState(active_state_stale_s=2.0)


def test_cancel_decision_is_an_immutable_workflow_contract() -> None:
    assert dataclasses.is_dataclass(StageMotionCancelDecision)
    assert StageMotionCancelDecision.__dataclass_params__.frozen


def test_coordinate_has_priority_over_reported_motion_and_busy_task() -> None:
    decision = _state().decide(
        coordinate_active=True,
        controller_state="Run",
        status_timestamp=9.5,
        controller_busy=True,
        monotonic_s=10.0,
    )

    assert decision == StageMotionCancelDecision(
        coordinate_priority=True,
        reported_active_motion=True,
    )
    assert decision.cancelable is True


def test_fresh_reported_motion_has_priority_over_busy_task() -> None:
    decision = _state().decide(
        coordinate_active=False,
        controller_state="Jog",
        status_timestamp=9.5,
        controller_busy=True,
        monotonic_s=10.0,
    )

    assert decision == StageMotionCancelDecision(
        reported_active_motion=True,
        cancel_reported_motion=True,
    )


def test_busy_task_is_selected_without_reported_motion() -> None:
    decision = _state().decide(
        coordinate_active=False,
        controller_state="Idle",
        status_timestamp=9.5,
        controller_busy=True,
        monotonic_s=10.0,
    )

    assert decision == StageMotionCancelDecision(cancel_active_task=True)


@pytest.mark.parametrize("controller_state", ["Run", "Jog"])
def test_stale_reported_motion_is_not_cancelable(controller_state: str) -> None:
    decision = _state().decide(
        coordinate_active=False,
        controller_state=controller_state,
        status_timestamp=7.0,
        controller_busy=False,
        monotonic_s=10.0,
    )

    assert decision == StageMotionCancelDecision()
    assert decision.cancelable is False


@pytest.mark.parametrize(
    ("pending_edits", "planned_pending", "planned_active", "expected"),
    [
        (False, False, False, False),
        (True, False, False, True),
        (False, True, False, True),
        (False, False, True, True),
    ],
)
def test_action_cancelability_combines_workflow_availability(
    pending_edits: bool,
    planned_pending: bool,
    planned_active: bool,
    expected: bool,
) -> None:
    decision = StageMotionCancelDecision()

    assert (
        decision.action_cancelable(
            pending_edits=pending_edits,
            planned_pending=planned_pending,
            planned_active=planned_active,
        )
        is expected
    )
