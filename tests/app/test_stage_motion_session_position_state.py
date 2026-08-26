from __future__ import annotations

import dataclasses
import threading

import pytest
from PySide6.QtCore import QThread, Qt
from PySide6.QtWidgets import QApplication

from probe_station_gui.application.stage_motion_session import _StageMotionSession
from probe_station_gui.coordinates.model import PhysicalMachinePose
from probe_station_gui.stage.controller import StageController as QtStageController
from probe_station_gui.stage.types import (
    StageMotionResetReason,
    TrackedAbsoluteXYMoveFinished,
    TrackedAbsoluteXYMoveStarted,
    _Status,
)
from tests.app.test_stage_motion_session_planned_position import (
    _config,
    _motion_token,
    _observation,
    _observe,
    _request,
    _session,
    _tracked_finish,
    _tracked_start,
)


def test_valid_and_unhomed_position_presentations_publish_before_action_state() -> None:
    session, controller = _session()
    events: list[tuple[str, object]] = []
    session.presentation_changed.connect(
        lambda value: events.append(("presentation", value))
    )
    session.action_state_changed.connect(lambda value: events.append(("action", value)))

    _observe(session, controller, (1.0, 2.0, 3.0, 4.0, 5.0))

    assert [name for name, _value in events] == ["presentation", "action"]
    presentation = events[0][1]
    assert presentation.presented_position == (1.0, 2.0, 3.0, 4.0, 5.0)
    assert presentation.presented_stage_xy == (1.0, 2.0)
    assert presentation.unhomed_fallback is False

    controller.homed.clear()
    events.clear()
    _observe(session, controller, (7.0, 8.0, 3.0, 4.0, 5.0))

    assert [name for name, _value in events] == ["presentation", "action"]
    fallback = events[0][1]
    assert fallback.reported_position == (7.0, 8.0, 3.0, 4.0, 5.0)
    assert fallback.unhomed_fallback is True
    assert fallback.presented_stage_xy is None
    assert session.snapshot().presented_stage_xy == (7.0, 8.0)


@pytest.mark.parametrize("invalid_position", [None, ("bad", 2.0)])
def test_invalid_position_clears_stale_presentation(invalid_position: object) -> None:
    session, _controller = _session()
    presentations = []
    session.presentation_changed.connect(presentations.append)
    _observe(session, _controller, (1.0, 2.0, 3.0, 4.0, 5.0))

    _observe(session, _controller, invalid_position)

    snapshot = session.snapshot()
    assert snapshot.presented_position is None
    assert snapshot.presented_stage_xy is None
    assert snapshot.physical_machine_pose.to_dict() == {}
    unavailable = presentations[-1]
    assert unavailable.reported_position is invalid_position
    assert unavailable.presented_position is None
    assert unavailable.presented_stage_xy is None
    assert unavailable.raw_stage_xy is None


@pytest.mark.parametrize("state", ["pending", "active", "waiting"])
def test_explicit_connection_reset_observation_clears_owned_motion_state(
    state: str,
) -> None:
    session, controller = _session()
    presentations = []
    session.presentation_changed.connect(presentations.append)
    _observe(session, controller, (1.0, 2.0, 3.0, 4.0, 5.5))
    assert session.request_planned_xy_move(_request()) is True
    motion_token = _motion_token(controller)
    if state in {"active", "waiting"}:
        _tracked_start(session, controller, motion_token=motion_token)
    if state == "waiting":
        _tracked_finish(
            session,
            controller,
            success=True,
            message="Arrived",
            motion_token=motion_token,
        )

    session.on_stage_position_changed(
        dataclasses.replace(
            _observation(controller, None),
            reset_reason=StageMotionResetReason.CONNECTION_CHANGED,
        )
    )

    snapshot = session.snapshot()
    assert snapshot.presented_position is None
    assert snapshot.presented_stage_xy is None
    assert snapshot.physical_machine_pose.to_dict() == {}
    assert snapshot.last_reported_b_position is None
    assert snapshot.planned_pending_target_xy is None
    assert snapshot.planned_pending_source_label is None
    assert snapshot.planned_stage_xy is None
    assert snapshot.planned_prediction_active is False
    assert snapshot.planned_waiting_for_fresh_status is False
    assert snapshot.active_axes == frozenset()
    assert session._prediction_timer.isActive() is False
    assert presentations[-1].reported_position is None

    cleared = session.snapshot()
    session.on_tracked_absolute_xy_move_started(
        TrackedAbsoluteXYMoveStarted(
            motion_token=motion_token,
            origin_position=(1.0, 2.0, 3.0, 4.0, 5.5),
            target_stage_xy=(4.0, 6.0),
            feedrate_mm_min=300.0,
        )
    )
    session.on_tracked_absolute_xy_move_finished(
        TrackedAbsoluteXYMoveFinished(
            motion_token=motion_token,
            target_stage_xy=(4.0, 6.0),
            success=False,
            message="stale after reset",
            status_timestamp=10.0,
        )
    )
    assert session.snapshot() == cleared


def test_position_publishes_partial_physical_pose_and_tracks_b() -> None:
    session, controller = _session()
    presentations = []
    session.presentation_changed.connect(presentations.append)

    _observe(session, controller, (1.0, 2.0, 3.0, 4.0, 5.5))

    assert controller.pose_axes == []
    assert presentations[-1].physical_machine_pose.to_dict() == {
        "X": 11.0,
        "Y": 22.0,
        "B": 55.0,
    }
    assert presentations[-1].b_position == pytest.approx(5.5)
    assert session.snapshot().last_reported_b_position == pytest.approx(5.5)


def test_mutation_is_confined_to_object_qt_thread() -> None:
    session, controller = _session()
    assert session.thread() == QThread.currentThread()
    assert session._prediction_timer.parent() is session
    errors: list[BaseException] = []

    def mutate_from_python_thread() -> None:
        try:
            session.request_planned_xy_move(_request())
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=mutate_from_python_thread)
    thread.start()
    thread.join()

    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert controller.move_requests == []


def test_cached_pose_refresh_updates_presentation_without_controller_io() -> None:
    session, controller = _session()
    presentations = []
    actions = []
    session.presentation_changed.connect(presentations.append)
    session.action_state_changed.connect(actions.append)

    session.refresh_physical_machine_pose()

    assert controller.pose_axes == [_config().axis_names]
    assert session.snapshot().physical_machine_pose.to_dict() == {
        "X": 11.0,
        "Y": 22.0,
        "B": 55.0,
    }
    assert len(presentations) == 1
    assert presentations[0].physical_machine_pose.to_dict() == {
        "X": 11.0,
        "Y": 22.0,
        "B": 55.0,
    }
    assert actions == []


def test_cached_pose_refresh_preserves_unhomed_position_unavailability() -> None:
    session, controller = _session()
    presentations = []
    session.presentation_changed.connect(presentations.append)
    controller.homed.clear()
    _observe(session, controller, (7.0, 8.0, 3.0, 4.0, 5.0))
    before = presentations[-1]
    controller.pose = PhysicalMachinePose.from_mapping({"X": 70.0})

    session.refresh_physical_machine_pose()

    refreshed = presentations[-1]
    assert refreshed == dataclasses.replace(
        before,
        physical_machine_pose=controller.pose,
    )
    assert refreshed.unhomed_fallback is True
    assert refreshed.presented_stage_xy is None


def test_cached_pose_refresh_preserves_contact_calibration_position() -> None:
    session, controller = _session()
    presentations = []
    session.presentation_changed.connect(presentations.append)
    _observe(session, controller, (1.0, 2.0, 3.0, 4.0, 5.0))
    before = presentations[-1]
    controller.pose = PhysicalMachinePose.from_mapping({"X": 12.0})

    session.refresh_physical_machine_pose()

    assert before.contact_calibration_position == (1.0, 2.0, 3.0)
    assert presentations[-1] == dataclasses.replace(
        before,
        physical_machine_pose=controller.pose,
    )


def test_cached_pose_refresh_preserves_active_motion_axes() -> None:
    session, controller = _session()
    presentations = []
    session.presentation_changed.connect(presentations.append)
    _observe(session, controller, (1.0, 2.0, 3.0, 4.0, 5.0))
    assert session.request_planned_xy_move(_request()) is True
    _tracked_start(session, controller)
    before = presentations[-1]
    controller.pose = PhysicalMachinePose.from_mapping({"X": 13.0})

    session.refresh_physical_machine_pose()

    assert before.active_axes == frozenset({"X", "Y"})
    assert presentations[-1] == dataclasses.replace(
        before,
        physical_machine_pose=controller.pose,
    )


def test_queued_position_observations_keep_complete_generations_without_cache_rereads(
    monkeypatch,
) -> None:
    application = QApplication.instance() or QApplication([])
    controller = QtStageController()
    session = _StageMotionSession(controller, _config())
    presentations = []
    session.presentation_changed.connect(presentations.append)
    controller.stage_position_observed.connect(
        session.on_stage_position_changed,
        Qt.ConnectionType.QueuedConnection,
    )
    controller._position_reporting_mode = "machine"
    controller._homed_axes = {"X", "Y", "Z"}
    first_position = (1.0, 2.0, 3.0, 4.0, 5.0)
    second_position = (6.0, 7.0, 8.0, 9.0, 10.0)

    def status(
        state: str,
        position: tuple[float, ...],
        machine_x: float,
    ) -> _Status:
        machine_position = (machine_x, 12.0, 13.0, 14.0, 15.0)
        return _Status(
            state=state,
            position=machine_position,
            synchronized_machine_position=machine_position,
            display_position=position,
        )

    try:
        controller._last_status_timestamp = 10.0
        controller._last_jog_write_timestamp = 4.0
        controller._update_cached_positions(status("Run", first_position, 11.0))
        controller._last_status_timestamp = 20.0
        controller._last_jog_write_timestamp = 5.0
        controller._update_cached_positions(status("Idle", second_position, 21.0))

        def unexpected_cache_read(*_args, **_kwargs):
            pytest.fail(
                "queued Stage position handling reread mutable controller cache"
            )

        for name in (
            "latest_physical_machine_pose",
            "latest_motion_coordinate_snapshot",
            "latest_machine_coordinate_snapshot",
            "latest_stage_position",
            "latest_stage_state",
            "last_status_timestamp",
            "last_jog_write_timestamp",
            "axes_are_homed",
            "homed_axes",
        ):
            monkeypatch.setattr(controller, name, unexpected_cache_read)
        assert presentations == []

        application.processEvents()

        assert [value.reported_position for value in presentations] == [
            first_position,
            second_position,
        ]
        assert [
            value.physical_machine_pose.to_dict()["X"] for value in presentations
        ] == [
            11.0,
            21.0,
        ]
        assert [
            value.motion_coordinate_snapshot.raw_machine_position[0]
            for value in presentations
        ] == [11.0, 21.0]
        assert [value.stage_state for value in presentations] == ["Run", "Idle"]
        assert [value.homed_axes for value in presentations] == [
            frozenset({"X", "Y", "Z"}),
            frozenset({"X", "Y", "Z"}),
        ]
        assert [value.status_timestamp for value in presentations] == [10.0, 20.0]
        assert [value.last_jog_write_timestamp for value in presentations] == [4.0, 5.0]
    finally:
        controller.shutdown()


def test_identical_newer_controller_status_releases_planned_wait_without_idle_rebuild(
    monkeypatch,
) -> None:
    application = QApplication.instance() or QApplication([])
    controller = QtStageController()
    session = _StageMotionSession(controller, _config())
    presentations = []
    move_tokens = []
    session.presentation_changed.connect(presentations.append)
    controller.stage_position_observed.connect(
        session.on_stage_position_changed,
        Qt.ConnectionType.QueuedConnection,
    )
    monkeypatch.setattr(
        controller,
        "request_move_to_xy",
        lambda _x, _y, *, motion_token=None: move_tokens.append(motion_token) or True,
    )
    controller._position_reporting_mode = "machine"
    controller._homed_axes = {"X", "Y", "Z"}
    position = (1.0, 2.0, 3.0, 4.0, 5.0)
    status = _Status(
        state="Idle",
        position=position,
        synchronized_machine_position=position,
        display_position=position,
    )

    try:
        controller._last_status_timestamp = 10.0
        controller._update_cached_positions(status)
        application.processEvents()
        assert session.request_planned_xy_move(_request()) is True
        token = move_tokens[-1]
        session.on_tracked_absolute_xy_move_started(
            TrackedAbsoluteXYMoveStarted(
                motion_token=token,
                origin_position=position,
                target_stage_xy=(4.0, 6.0),
                feedrate_mm_min=600.0,
            )
        )
        session.on_tracked_absolute_xy_move_finished(
            TrackedAbsoluteXYMoveFinished(
                motion_token=token,
                target_stage_xy=(4.0, 6.0),
                success=True,
                message="Arrived",
                status_timestamp=10.0,
            )
        )
        assert session.snapshot().planned_waiting_for_fresh_status is True

        controller._last_status_timestamp = 11.0
        controller._update_cached_positions(status)
        application.processEvents()

        assert session.snapshot().planned_waiting_for_fresh_status is False
        assert presentations[-1].material_change is True
        assert session.request_planned_xy_move(_request((8.0, 9.0))) is True
        assert session.cancel_planned_xy_move() is True
        presentation_count = len(presentations)

        controller._last_status_timestamp = 12.0
        controller._update_cached_positions(status)
        application.processEvents()

        assert session._last_observation.status_timestamp == 12.0
        assert len(presentations) == presentation_count + 1
        assert presentations[-1].material_change is False
    finally:
        controller.shutdown()


def test_controller_cache_clear_resets_owned_session_state(monkeypatch) -> None:
    application = QApplication.instance() or QApplication([])
    controller = QtStageController()
    session = _StageMotionSession(controller, _config())
    presentations = []
    move_tokens = []
    session.presentation_changed.connect(presentations.append)
    controller.stage_position_observed.connect(
        session.on_stage_position_changed,
        Qt.ConnectionType.QueuedConnection,
    )
    monkeypatch.setattr(
        controller,
        "request_move_to_xy",
        lambda _x, _y, *, motion_token=None: move_tokens.append(motion_token) or True,
    )
    position = (1.0, 2.0, 3.0, 4.0, 5.0)
    controller._position_reporting_mode = "machine"
    controller._homed_axes = {"X", "Y", "Z"}
    controller._last_status_timestamp = 10.0
    controller._update_cached_positions(
        _Status(
            state="Idle",
            position=position,
            synchronized_machine_position=position,
            display_position=position,
        )
    )
    application.processEvents()
    assert session.request_planned_xy_move(_request()) is True
    token = move_tokens[-1]
    session.on_tracked_absolute_xy_move_started(
        TrackedAbsoluteXYMoveStarted(
            motion_token=token,
            origin_position=position,
            target_stage_xy=(4.0, 6.0),
            feedrate_mm_min=600.0,
        )
    )
    session.on_tracked_absolute_xy_move_finished(
        TrackedAbsoluteXYMoveFinished(
            motion_token=token,
            target_stage_xy=(4.0, 6.0),
            success=True,
            message="Arrived",
            status_timestamp=10.0,
        )
    )

    try:
        controller.clear_cached_controller_state()
        application.processEvents()

        snapshot = session.snapshot()
        assert snapshot.presented_position is None
        assert snapshot.presented_stage_xy is None
        assert snapshot.physical_machine_pose.to_dict() == {}
        assert snapshot.planned_stage_xy is None
        assert snapshot.planned_waiting_for_fresh_status is False
        assert presentations[-1].reported_position is None
    finally:
        controller.shutdown()


def test_connection_reset_clears_all_owned_state_without_controller_io() -> None:
    session, controller = _session()
    _observe(session, controller, (1.0, 2.0, 3.0, 4.0, 5.5))
    assert session.request_planned_xy_move(_request()) is True
    _tracked_start(session, controller)
    assert session._prediction_timer.isActive() is True
    move_requests_before = list(controller.move_requests)

    session.reset(StageMotionResetReason.CONNECTION_CHANGED)

    snapshot = session.snapshot()
    assert snapshot.presented_position is None
    assert snapshot.presented_stage_xy is None
    assert snapshot.physical_machine_pose.to_dict() == {}
    assert snapshot.last_reported_b_position is None
    assert snapshot.active_axes == frozenset()
    assert snapshot.planned_pending_target_xy is None
    assert snapshot.planned_pending_source_label is None
    assert snapshot.planned_stage_xy is None
    assert snapshot.planned_prediction_active is False
    assert snapshot.planned_waiting_for_fresh_status is False
    assert session._prediction_timer.isActive() is False
    assert controller.move_requests == move_requests_before

    first_reset = session.snapshot()
    session.reset(StageMotionResetReason.CONNECTION_CHANGED)
    assert session.snapshot() == first_reset
    assert controller.move_requests == move_requests_before


@pytest.mark.parametrize("state", ["pending", "active", "waiting"])
def test_cancel_planned_xy_move_clears_only_owned_planned_state(state: str) -> None:
    session, controller = _session()
    assert session.request_planned_xy_move(_request()) is True
    if state in {"active", "waiting"}:
        _tracked_start(session, controller)
    if state == "waiting":
        _tracked_finish(session, controller, success=True, message="Arrived")

    cancelled = session.cancel_planned_xy_move()

    assert cancelled is True
    snapshot = session.snapshot()
    assert snapshot.planned_pending_target_xy is None
    assert snapshot.planned_pending_source_label is None
    assert snapshot.planned_stage_xy is None
    assert snapshot.planned_prediction_active is False
    assert snapshot.planned_waiting_for_fresh_status is False
    assert snapshot.active_axes == frozenset()
    assert session._prediction_timer.isActive() is False
    assert session.cancel_planned_xy_move() is False
