from __future__ import annotations

import dataclasses

import pytest
from PySide6.QtWidgets import QApplication

from probe_station_gui.application import stage_motion_session as motion_session
from probe_station_gui.application import stage_motion_types as motion_types
from probe_station_gui.coordinates.model import PhysicalMachinePose
from probe_station_gui.stage import exact_step
from probe_station_gui.stage.types import (
    StageMotionResetReason,
    StagePositionObservation,
    TrackedAbsoluteXYMoveStarted,
)
from tests.app.test_stage_motion_session_jog_step import (
    _Controller,
    _config,
    _coordinate_request,
)


class _CancelController(_Controller):
    def __init__(self) -> None:
        super().__init__()
        self.busy = False
        self.cancelled_motions: list[str] = []
        self.cancelled_tasks: list[str] = []

    def is_busy(self) -> bool:
        return self.busy

    def cancel_active_motion(self, reason: str) -> None:
        self.cancelled_motions.append(str(reason))

    def cancel_active_task(self, reason: str) -> None:
        self.cancelled_tasks.append(str(reason))


@pytest.fixture(scope="module", autouse=True)
def _qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _session() -> tuple[motion_session._StageMotionSession, _CancelController]:
    controller = _CancelController()
    return motion_session._StageMotionSession(controller, _config()), controller


def _start_planned(
    session: motion_session._StageMotionSession,
    controller: _CancelController,
) -> None:
    request = motion_types.PlannedXYMoveRequest((4.0, 6.0), "Click move")
    assert session.request_planned_xy_move(request)
    session.on_tracked_absolute_xy_move_started(
        TrackedAbsoluteXYMoveStarted(
            motion_token=controller.planned_requests[-1][2],
            origin_position=controller.position,
            target_stage_xy=request.target_stage_xy,
            feedrate_mm_min=600.0,
        )
    )
    assert session.snapshot().planned_prediction_active


def _observe_controller_state(
    session: motion_session._StageMotionSession,
    controller: _CancelController,
) -> None:
    session.on_stage_position_changed(
        StagePositionObservation(
            position=controller.position,
            physical_machine_pose=PhysicalMachinePose.from_mapping({}),
            motion_coordinate_snapshot=None,
            stage_state=controller.state,
            homed_axes=frozenset(),
            status_timestamp=controller.status_timestamp,
            last_jog_write_timestamp=None,
        )
    )


def test_cancel_outcome_is_a_frozen_typed_contract() -> None:
    assert dataclasses.is_dataclass(motion_types.StageMotionCancelOutcome)
    assert motion_types.StageMotionCancelOutcome.__dataclass_params__.frozen


def test_coordinate_cancel_has_priority_and_clears_all_coordinate_state() -> None:
    session, controller = _session()
    assert session.start_coordinate_move(_coordinate_request((("X", 5.0, 5.0),)))
    session.upsert_pending_coordinate_edit("Y", 7.0, 7.0, motion_lease=object())
    controller.busy = True

    outcome = session.cancel_stage_motion()

    assert outcome.stage_motion_cancelled is True
    assert outcome.coordinate_priority is True
    assert outcome.pending_edits_cleared is True
    assert controller.cancelled_motions == ["Coordinate move cancel requested."]
    assert controller.cancelled_tasks == []
    assert controller.feed_override_resets == 1
    snapshot = session.snapshot()
    assert snapshot.coordinate_active is False
    assert snapshot.pending_edit_axes == frozenset()


def test_fresh_reported_motion_cancels_motion_and_clears_planned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, controller = _session()
    _start_planned(session, controller)
    monkeypatch.setattr(motion_session.time, "monotonic", lambda: 100.0)
    controller.state = "Run"
    controller.status_timestamp = 99.5
    controller.busy = True
    _observe_controller_state(session, controller)

    assert session.snapshot().reported_active_motion is True

    outcome = session.cancel_stage_motion()

    assert outcome.stage_motion_cancelled is True
    assert outcome.coordinate_priority is False
    assert controller.cancelled_motions == ["Motion cancel requested."]
    assert controller.cancelled_tasks == []
    assert session.snapshot().planned_prediction_active is False


def test_busy_controller_without_reported_motion_cancels_active_task() -> None:
    session, controller = _session()
    controller.state = "Idle"
    controller.busy = True

    assert session.snapshot().cancelable is True
    outcome = session.cancel_stage_motion()

    assert outcome.stage_motion_cancelled is True
    assert outcome.coordinate_priority is False
    assert controller.cancelled_motions == []
    assert controller.cancelled_tasks == ["Operation cancel requested."]


def test_snapshot_uses_fresh_synchronized_controller_activity_without_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, controller = _session()
    monkeypatch.setattr(motion_session.time, "monotonic", lambda: 100.0)
    controller.state = "Run"
    controller.status_timestamp = 99.5

    snapshot = session.snapshot()

    assert snapshot.reported_active_motion is True
    assert snapshot.cancelable is True


def test_snapshot_preserves_reported_motion_during_coordinate_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, controller = _session()
    assert session.start_coordinate_move(_coordinate_request((("X", 5.0, 5.0),)))
    monkeypatch.setattr(motion_session.time, "monotonic", lambda: 100.0)
    controller.state = "Run"
    controller.status_timestamp = 99.5

    snapshot = session.snapshot()

    assert snapshot.coordinate_active is True
    assert snapshot.reported_active_motion is True


@pytest.mark.parametrize("state", ["Run", "Jog"])
def test_stale_reported_motion_is_not_cancelable(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    session, controller = _session()
    monkeypatch.setattr(motion_session.time, "monotonic", lambda: 100.0)
    controller.state = state
    controller.status_timestamp = 90.0
    _observe_controller_state(session, controller)

    assert session.snapshot().reported_active_motion is False
    assert session.snapshot().cancelable is False
    outcome = session.cancel_stage_motion()

    assert outcome.stage_motion_cancelled is False
    assert controller.cancelled_motions == []
    assert controller.cancelled_tasks == []


@pytest.mark.parametrize("state", [None, ""])
def test_missing_reported_motion_state_is_not_cancelable(state: object) -> None:
    session, controller = _session()
    controller.state = state  # type: ignore[assignment]

    assert session.snapshot().cancelable is False
    assert session.cancel_stage_motion().stage_motion_cancelled is False


def test_reset_clears_every_owned_stage_motion_fact_and_timer() -> None:
    session, controller = _session()
    controller.position = (1.0, 2.0, 3.0, 0.0, 5.0, 0.0)
    session.on_stage_position_changed(
        StagePositionObservation(
            position=controller.position,
            physical_machine_pose=PhysicalMachinePose.from_mapping(
                {"X": 1.0, "Y": 2.0, "B": 5.0}
            ),
            motion_coordinate_snapshot=None,
            stage_state="Idle",
            homed_axes=frozenset({"X", "Y", "Z", "B"}),
            status_timestamp=1.0,
            last_jog_write_timestamp=None,
        )
    )
    session.on_manual_jog_command((("X", 1.0),), 60.0)
    session.on_manual_jog_stopped()
    _start_planned(session, controller)
    session.queue_exact_step(
        exact_step.ExactStepRequest(
            move_request=_coordinate_request((("X", 1.001, 1.001),)),
            motion_lease=object(),
        )
    )
    session.upsert_pending_coordinate_edit("Y", 7.0, 7.0, motion_lease=object())

    session.reset(StageMotionResetReason.CONNECTION_CHANGED)

    snapshot = session.snapshot()
    assert snapshot.presented_position is None
    assert snapshot.presented_stage_xy is None
    assert snapshot.physical_machine_pose.to_dict() == {}
    assert snapshot.last_reported_b_position is None
    assert snapshot.active_axes == frozenset()
    assert snapshot.coordinate_active is False
    assert snapshot.pending_edit_axes == frozenset()
    assert snapshot.manual_prediction_available is False
    assert snapshot.planned_pending_target_xy is None
    assert snapshot.planned_prediction_active is False
    assert snapshot.exact_step_display_targets == ()
    assert session._coordinate_targets.purpose_armed is False
    assert session._prediction_timer.isActive() is False
    assert session._exact_step_timer.isActive() is False
    assert session._terminal_resume_timer.isActive() is False


def test_action_state_combines_pending_edits_with_session_cancelability() -> None:
    session, controller = _session()
    emitted: list[motion_types.StageMotionActionState] = []
    session.action_state_changed.connect(emitted.append)

    session.upsert_pending_coordinate_edit("X", 3.0, 3.0, motion_lease=None)
    assert emitted[-1].cancelable is True

    session.clear_pending_coordinate_edits()
    assert emitted[-1].cancelable is False


def test_planned_pending_and_active_states_remain_cancelable() -> None:
    session, controller = _session()
    emitted: list[motion_types.StageMotionActionState] = []
    session.action_state_changed.connect(emitted.append)
    request = motion_types.PlannedXYMoveRequest((4.0, 6.0), "Click move")

    assert session.request_planned_xy_move(request)
    assert emitted[-1].planned_pending is True
    assert emitted[-1].cancelable is True

    session.on_tracked_absolute_xy_move_started(
        TrackedAbsoluteXYMoveStarted(
            motion_token=controller.planned_requests[-1][2],
            origin_position=controller.position,
            target_stage_xy=request.target_stage_xy,
            feedrate_mm_min=600.0,
        )
    )
    assert emitted[-1].planned_active is True
    assert emitted[-1].cancelable is True


def test_exact_step_pending_edits_are_reported_cleared_by_cancel() -> None:
    session, controller = _session()
    session.queue_exact_step(
        exact_step.ExactStepRequest(
            move_request=_coordinate_request((("X", 1.001, 1.001),)),
            motion_lease=object(),
        )
    )
    assert session.snapshot().pending_edit_axes == frozenset({"X"})

    outcome = session.cancel_stage_motion()

    assert outcome.pending_edits_cleared is True
    assert outcome.stage_motion_cancelled is False
    assert session.snapshot().pending_edit_axes == frozenset()
    assert session.snapshot().exact_step_display_targets == ()
    assert controller.requests == []


def test_snapshot_contains_no_unrelated_application_workflow_state() -> None:
    field_names = {
        field.name for field in dataclasses.fields(motion_types.StageMotionSnapshot)
    }
    forbidden = ("route", "scan", "surface", "click", "sample", "homing")

    assert not any(token in name for token in forbidden for name in field_names)
