from __future__ import annotations

import dataclasses

from probe_station_gui.application import stage_motion_types
from probe_station_gui.stage.types import StageMotionResetReason
from tests.app.test_stage_motion_session_coordinate import _request, _session


def _typed_signal(session, name: str):
    assert hasattr(session, name), f"missing typed session signal: {name}"
    return getattr(session, name)


def test_alignment_completion_dto_is_frozen_and_typed() -> None:
    dto_type = getattr(stage_motion_types, "AlignmentRotationCompletion", None)
    assert dto_type is not None
    assert dataclasses.is_dataclass(dto_type)
    assert dto_type.__dataclass_params__.frozen is True
    completion = dto_type(correlation=object(), success=True, message="opaque")
    assert completion.success is True


def test_ordinary_completion_emits_status_settle_and_action(monkeypatch) -> None:
    session, _controller = _session()
    clicks: list[bool] = []
    alignments: list[object] = []
    statuses: list[tuple[str, int]] = []
    actions: list[object] = []
    settles: list[bool] = []
    _typed_signal(session, "click_move_finished").connect(clicks.append)
    _typed_signal(session, "alignment_rotation_finished").connect(alignments.append)
    session.status_requested.connect(
        lambda message, timeout: statuses.append((message, timeout))
    )
    session.action_state_changed.connect(actions.append)
    monkeypatch.setattr(
        session._settle_status_polls, "schedule", lambda: settles.append(True)
    )

    session.on_movement_finished(True, "Click and alignment words are irrelevant.")

    assert clicks == []
    assert alignments == []
    assert statuses == [("Click and alignment words are irrelevant.", 5000)]
    assert settles == [True]
    assert len(actions) == 1


def test_click_arms_only_on_start_and_emits_once() -> None:
    session, _controller = _session()
    clicks: list[bool] = []
    _typed_signal(session, "click_move_finished").connect(clicks.append)

    session.on_movement_finished(True, "ordinary")
    assert clicks == []
    assert hasattr(session, "on_click_move_started")
    session.on_click_move_started(1.0, 2.0, 300.0)
    session.on_movement_finished(False, "opaque")
    session.on_movement_finished(True, "late")

    assert clicks == [False]


def test_coordinate_start_supersedes_armed_click() -> None:
    session, _controller = _session()
    clicks: list[bool] = []
    _typed_signal(session, "click_move_finished").connect(clicks.append)
    assert hasattr(session, "on_click_move_started")
    session.on_click_move_started(1.0, 2.0, 300.0)
    assert session.start_coordinate_move(_request())

    session.on_movement_finished(True, "coordinate")
    assert clicks == []
    session.on_movement_finished(True, "click")
    assert clicks == []


def test_alignment_request_arms_only_when_controller_accepts() -> None:
    session, controller = _session()
    results: list[object] = []
    _typed_signal(session, "alignment_rotation_finished").connect(results.append)
    assert hasattr(session, "request_alignment_rotation")
    controller.request_rotate_b = lambda _delta: False
    rejected = object()
    assert session.request_alignment_rotation(2.0, rejected) is False
    session.on_movement_finished(True, "late")
    assert results == []

    controller.request_rotate_b = lambda _delta: True
    accepted = object()
    assert session.request_alignment_rotation(2.0, accepted) is True
    session.on_movement_finished(True, "opaque")
    session.on_movement_finished(False, "later")
    assert len(results) == 1
    assert results[0].correlation is accepted
    assert results[0].success is True


def test_reset_clears_alignment_correlation() -> None:
    session, controller = _session()
    results: list[object] = []
    _typed_signal(session, "alignment_rotation_finished").connect(results.append)
    assert hasattr(session, "request_alignment_rotation")
    controller.request_rotate_b = lambda _delta: True
    assert session.request_alignment_rotation(2.0, object()) is True

    session.reset(StageMotionResetReason.DISCONNECT)
    session.on_movement_finished(True, "late")

    assert results == []


def test_discard_alignment_rotation_preserves_click_purpose() -> None:
    session, _controller = _session()
    clicks: list[bool] = []
    session.click_move_finished.connect(clicks.append)
    session.on_click_move_started(1.0, 2.0, 300.0)

    assert session.discard_alignment_rotation() is False
    session.on_movement_finished(True, "click")

    assert clicks == [True]


def test_discard_alignment_rotation_clears_only_alignment_purpose() -> None:
    session, controller = _session()
    alignments: list[object] = []
    session.alignment_rotation_finished.connect(alignments.append)
    controller.request_rotate_b = lambda _delta: True
    assert session.request_alignment_rotation(2.0, object()) is True

    assert session.discard_alignment_rotation() is True
    assert session.discard_alignment_rotation() is False
    session.on_movement_finished(True, "late")

    assert alignments == []


def test_discard_alignment_rotation_preserves_coordinate_tracking() -> None:
    session, _controller = _session()
    assert session.start_coordinate_move(_request()) is True
    before = session.snapshot()

    assert session.discard_alignment_rotation() is False

    after = session.snapshot()
    assert before.coordinate_active is True
    assert after.coordinate_active is True
