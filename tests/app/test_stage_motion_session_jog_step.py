from __future__ import annotations

import dataclasses
import gc
from types import SimpleNamespace
import weakref

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from probe_station_gui.application import stage_motion_session as motion_session
from probe_station_gui.application import stage_motion_types as motion_types
from probe_station_gui.coordinates.model import PhysicalMachinePose
from probe_station_gui.stage import exact_step
from probe_station_gui.stage.coordinate_targets import (
    CoordinateMoveRequest,
    CoordinateTargetConfig,
)
from probe_station_gui.stage.manual_jog_prediction import ManualJogPredictionConfig
from probe_station_gui.stage.types import (
    StageMotionResetReason,
    StagePositionObservation,
    TrackedAbsoluteXYMoveStarted,
)


AXES = ("X", "Y", "Z", "A", "B", "C")


class _Controller:
    DEFAULT_FEEDRATE = 600.0

    def __init__(self) -> None:
        self.position: tuple[float, ...] | None = (1.0, 2.0, 3.0, 0.0, 0.0, 0.0)
        self.state = "Idle"
        self.status_timestamp: float | None = 1.0
        self.requests: list[tuple[dict[str, float], float]] = []
        self.events: list[str] = []
        self.feed_override_resets = 0
        self.planned_requests: list[tuple[float, float, object | None]] = []

    def request_move_to_xy(
        self,
        x_mm: float,
        y_mm: float,
        *,
        motion_token: object | None = None,
    ) -> bool:
        self.planned_requests.append((float(x_mm), float(y_mm), motion_token))
        return True

    def latest_stage_position(self) -> tuple[float, ...] | None:
        return self.position

    def latest_stage_state(self) -> str:
        return self.state

    def last_status_timestamp(self) -> float | None:
        return self.status_timestamp

    def is_busy(self) -> bool:
        return self.state.lower() in {"run", "jog"}

    def axis_max_feedrates(self) -> dict[str, float]:
        return {axis: 600.0 for axis in AXES}

    def axis_display_limits(self, _axis: str) -> tuple[float, float]:
        return (-100.0, 100.0)

    def axis_machine_display_limits(self, _axis: str) -> tuple[float, float]:
        return (-100.0, 100.0)

    def request_absolute_axis_targets_move(
        self, targets: dict[str, float], *, feedrate: float
    ) -> bool:
        self.requests.append((dict(targets), float(feedrate)))
        return True

    def queue_feed_override_reset(self) -> None:
        self.feed_override_resets += 1

    def request_status_refresh(self) -> None:
        self.events.append("status-refresh")


@pytest.fixture(scope="module", autouse=True)
def _qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _config() -> motion_types.StageMotionConfig:
    values = {
        "axis_names": AXES,
        "prediction_interval_ms": 50,
        "planned_move_duration_padding_s": 0.12,
        "planned_start_tolerance_mm": 1e-4,
        "min_feedrate_mm_min": 1.0,
        "b_position_change_tolerance_deg": 1e-3,
        "manual_jog": ManualJogPredictionConfig(
            axis_names=AXES,
            ignore_idle_after_command_s=0.25,
            reconcile_smooth_threshold_mm=0.35,
            reconcile_smooth_alpha=0.35,
            status_settle_hold_s=0.8,
            default_stop_tail_s=0.11,
            stop_tail_min_s=0.02,
            stop_tail_max_s=0.25,
            stop_tail_learn_alpha=0.25,
        ),
        "coordinate_target": CoordinateTargetConfig(
            axis_names=AXES,
            min_feedrate_mm_min=1.0,
            duration_padding_s=0.0,
            min_idle_accept_s=0.0,
            target_tolerance_mm=0.01,
        ),
        "settle_status_poll_delays_ms": (180, 420),
    }
    optional = {
        "exact_step_accumulation_ms": 80,
        "terminal_resume_after_jog_ms": 180,
    }
    fields = motion_types.StageMotionConfig.__dataclass_fields__
    values.update({name: value for name, value in optional.items() if name in fields})
    return motion_types.StageMotionConfig(**values)


def _session() -> tuple[motion_session._StageMotionSession, _Controller]:
    controller = _Controller()
    return motion_session._StageMotionSession(controller, _config()), controller


def _observe(
    session: motion_session._StageMotionSession,
    controller: _Controller,
    position: tuple[float, ...],
    *,
    state: str,
    timestamp: float,
    last_jog: float | None = None,
) -> None:
    controller.position = position
    controller.state = state
    controller.status_timestamp = timestamp
    session.on_stage_position_changed(
        StagePositionObservation(
            position=position,
            physical_machine_pose=PhysicalMachinePose.from_mapping(
                dict(zip(AXES, position))
            ),
            motion_coordinate_snapshot=None,
            stage_state=state,
            homed_axes=frozenset(AXES),
            status_timestamp=timestamp,
            last_jog_write_timestamp=last_jog,
        )
    )


def _coordinate_request(
    targets: tuple[tuple[str, float, float], ...],
    *,
    seed: tuple[float, ...] = (1.0, 2.0, 3.0, 0.0, 0.0, 0.0),
    basis: object | None = None,
) -> CoordinateMoveRequest:
    return CoordinateMoveRequest(
        targets=targets,
        seed_position=seed,
        feedrate_mm_min=120.0,
        source_label="Step",
        display_basis=basis,
    )


def _queue_exact(
    session: motion_session._StageMotionSession,
    targets: tuple[tuple[str, float, float], ...],
    *,
    lease: object | None = None,
    basis: object | None = None,
    allow_pose_rebase: bool = False,
) -> object:
    request_type = getattr(exact_step, "ExactStepRequest", None)
    outcome_type = getattr(exact_step, "ExactStepOutcome", None)
    assert request_type is not None, "ExactStepRequest is absent"
    assert outcome_type is not None, "ExactStepOutcome is absent"
    request = request_type(
        move_request=_coordinate_request(targets, basis=basis),
        motion_lease=lease,
        allow_pose_rebase=allow_pose_rebase,
    )
    outcome = session.queue_exact_step(request)
    assert isinstance(outcome, outcome_type)
    assert outcome.accepted is True
    return outcome


def _finish_coordinate(
    session: motion_session._StageMotionSession,
    controller: _Controller,
    position: tuple[float, ...],
) -> None:
    _observe(session, controller, position, state="Run", timestamp=2.0)
    _observe(session, controller, position, state="Idle", timestamp=3.0)


def test_jog_step_contracts_are_frozen_and_keep_existing_times() -> None:
    for name in ("ExactStepRequest", "ExactStepOutcome"):
        value_type = getattr(exact_step, name, None)
        assert value_type is not None, name
        assert dataclasses.is_dataclass(value_type)
        assert value_type.__dataclass_params__.frozen is True
    fields = motion_types.StageMotionConfig.__dataclass_fields__
    assert fields["exact_step_accumulation_ms"].default == 80
    assert fields["terminal_resume_after_jog_ms"].default == 180


def test_manual_seed_precedence_is_coordinate_latest_then_presented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(motion_session.time, "monotonic", lambda: 10.0)
    session, controller = _session()
    seed = (8.0, 9.0, 3.0, 0.0, 0.0, 0.0)
    assert session.start_coordinate_move(
        _coordinate_request((("X", 8.0, 8.0),), seed=seed)
    )
    session.on_manual_jog_command((("X", 1.0),), 60.0)
    assert session.snapshot().presented_position == seed
    assert controller.requests == [({"X": 8.0}, 120.0)]

    session, controller = _session()
    controller.position = (4.0, 5.0, 6.0, 0.0, 0.0, 0.0)
    session.on_manual_jog_command((("Y", -1.0),), 60.0)
    assert session.snapshot().presented_position == controller.position

    session, controller = _session()
    presented = (11.0, 12.0, 6.0, 0.0, 0.0, 0.0)
    _observe(session, controller, presented, state="Idle", timestamp=2.0)
    controller.position = None
    session.on_manual_jog_command((("X", 1.0),), 60.0)
    assert session.snapshot().presented_position == presented


def test_zero_manual_command_stops_prediction_clears_axes_and_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(motion_session.time, "monotonic", lambda: 20.0)
    session, controller = _session()
    session.on_manual_jog_command((("X", 1.0),), 60.0)
    assert session.snapshot().active_axes == frozenset({"X"})

    session.on_manual_jog_command((("X", 0.0),), 60.0)

    assert session.snapshot().manual_prediction_active is False
    assert session.snapshot().active_axes == frozenset()
    QTest.qWait(450)
    assert controller.events == ["status-refresh", "status-refresh"]


def test_manual_clears_coordinate_but_preserves_unrelated_pending_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(motion_session.time, "monotonic", lambda: 30.0)
    session, controller = _session()
    assert session.start_coordinate_move(
        _coordinate_request(
            (("X", 5.0, 5.0),),
            seed=(2.0, 3.0, 4.0, 0.0, 0.0, 0.0),
        )
    )
    session.upsert_pending_coordinate_edit("Y", 7.0, 7.0, motion_lease=None)

    session.on_manual_jog_command((("Z", 2.0),), 60.0)

    snapshot = session.snapshot()
    assert snapshot.coordinate_active is False
    assert snapshot.pending_edit_axes == frozenset({"Y"})
    assert snapshot.active_axes == frozenset({"Z"})
    assert controller.feed_override_resets == 0


@pytest.mark.parametrize("planned_state", ["pending", "active"])
def test_handled_manual_jog_clears_pending_or_active_planned_workflow(
    planned_state: str,
) -> None:
    session, controller = _session()
    request = motion_types.PlannedXYMoveRequest(
        target_stage_xy=(4.0, 6.0),
        source_label="Click move",
    )
    assert session.request_planned_xy_move(request)
    motion_token = controller.planned_requests[-1][2]
    if planned_state == "active":
        session.on_tracked_absolute_xy_move_started(
            TrackedAbsoluteXYMoveStarted(
                motion_token=motion_token,
                origin_position=controller.position,
                target_stage_xy=request.target_stage_xy,
                feedrate_mm_min=600.0,
            )
        )
        assert session.snapshot().planned_prediction_active is True
    else:
        assert session.snapshot().planned_pending_target_xy == (4.0, 6.0)

    session.on_manual_jog_command((("X", 1.0),), 60.0)

    snapshot = session.snapshot()
    assert snapshot.planned_pending_target_xy is None
    assert snapshot.planned_prediction_active is False
    assert snapshot.planned_waiting_for_fresh_status is False


def test_stop_tail_continues_prediction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [40.0]
    monkeypatch.setattr(motion_session.time, "monotonic", lambda: now[0])
    session, controller = _session()
    controller.position = (1.0, 2.0, 3.0, 0.0, 0.0, 0.0)
    session.on_manual_jog_command((("X", 1.0),), 60.0)
    now[0] = 40.1
    session.on_manual_jog_stopped()
    now[0] = 40.16
    session.tick()
    assert session.snapshot().presented_position == pytest.approx(
        (1.16, 2.0, 3.0, 0.0, 0.0, 0.0)
    )
    assert session.snapshot().manual_prediction_active is True


def test_fresh_idle_is_ignored_then_stale_idle_reconciles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [50.0]
    monkeypatch.setattr(motion_session.time, "monotonic", lambda: now[0])
    session, controller = _session()
    controller.position = (1.0, 2.0, 3.0, 0.0, 0.0, 0.0)
    session.on_manual_jog_command((("X", 1.0),), 60.0)
    now[0] = 50.1
    session.tick()
    predicted = session.snapshot().presented_position
    _observe(
        session,
        controller,
        (0.0, 0.0, 3.0, 0.0, 0.0, 0.0),
        state="Idle",
        timestamp=2.0,
        last_jog=50.05,
    )
    assert session.snapshot().presented_position == predicted

    now[0] = 50.4
    _observe(
        session,
        controller,
        (0.0, 0.0, 3.0, 0.0, 0.0, 0.0),
        state="Idle",
        timestamp=3.0,
        last_jog=50.05,
    )
    assert session.snapshot().presented_position[:2] == (0.0, 0.0)


def test_running_status_smooths_large_prediction_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [60.0]
    monkeypatch.setattr(motion_session.time, "monotonic", lambda: now[0])
    session, controller = _session()
    session.on_manual_jog_command((("X", 1.0),), 60.0)
    now[0] = 60.1
    session.tick()
    _observe(
        session,
        controller,
        (5.0, 2.0, 3.0, 0.0, 0.0, 0.0),
        state="Run",
        timestamp=2.0,
        last_jog=60.05,
    )
    assert session.snapshot().presented_position[0] == pytest.approx(2.465)


def test_fresh_idle_learns_tail_for_next_jog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [70.0]
    monkeypatch.setattr(motion_session.time, "monotonic", lambda: now[0])
    session, controller = _session()
    controller.position = (0.0, 0.0, 3.0, 0.0, 0.0, 0.0)
    session.on_manual_jog_command((("X", 1.0),), 60.0)
    session.on_manual_jog_stopped()
    now[0] = 70.2
    session.tick()
    _observe(
        session,
        controller,
        (0.16, 0.0, 3.0, 0.0, 0.0, 0.0),
        state="Idle",
        timestamp=2.0,
        last_jog=70.0,
    )

    now[0] = 80.0
    controller.position = (0.16, 0.0, 3.0, 0.0, 0.0, 0.0)
    session.on_manual_jog_command((("X", 1.0),), 60.0)
    session.on_manual_jog_stopped()
    now[0] = 80.12
    session.tick()
    assert session.snapshot().manual_prediction_active is True


def test_terminal_resume_is_scheduled_before_settle_polls_and_both_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(motion_session.time, "monotonic", lambda: 90.0)
    session, controller = _session()
    signal = getattr(session, "terminal_live_poll_paused_changed", None)
    assert signal is not None, "typed terminal poll signal is absent"
    signal.connect(
        lambda paused: controller.events.append(
            "terminal-pause" if paused else "terminal-resume"
        )
    )
    settle_schedule = session._settle_status_polls.schedule

    def schedule_settle_polls() -> None:
        assert session._terminal_resume_timer.isActive()
        settle_schedule()

    monkeypatch.setattr(
        session._settle_status_polls,
        "schedule",
        schedule_settle_polls,
    )

    session.on_manual_jog_command((("X", 1.0),), 60.0)
    session.on_manual_jog_stopped()

    assert controller.events == ["terminal-pause"]
    for _attempt in range(100):
        if {"status-refresh", "terminal-resume"}.issubset(controller.events):
            break
        QTest.qWait(20)
    assert controller.events[0] == "terminal-pause"
    assert controller.events[1:].count("status-refresh") >= 1
    assert controller.events[1:].count("terminal-resume") == 1


def test_pending_terminal_resume_timer_does_not_retain_destroyed_session() -> None:
    session, _controller = _session()
    session.on_manual_jog_command((("X", 1.0),), 60.0)
    session.on_manual_jog_stopped()
    assert session._terminal_resume_timer.isActive()
    session_ref = weakref.ref(session)

    del session
    gc.collect()

    assert session_ref() is None


def test_exact_steps_accumulate_for_one_fixed_window() -> None:
    session, controller = _session()
    _queue_exact(session, (("X", 1.001, 1.001),))
    initial_remaining = session._exact_step_timer.remainingTime()
    assert session._exact_step_timer.interval() == 80
    QTest.qWait(20)
    _queue_exact(session, (("X", 1.002, 1.002),))
    remaining_after_second_press = session._exact_step_timer.remainingTime()
    assert 0 < remaining_after_second_press < initial_remaining
    assert session.dispatch_exact_steps() is False
    assert controller.requests == []
    QTest.qWait(100)
    assert controller.requests == [({"X": 1.002}, 120.0)]


def test_same_basis_followup_rebases_and_coalesces_multiple_axes() -> None:
    session, controller = _session()
    basis = ("frame", "alpha")
    _queue_exact(session, (("X", 1.001, 1.001),), basis=basis)
    QTest.qWait(140)
    _queue_exact(
        session,
        (("X", 1.003, 1.003), ("Y", 1.998, 1.998)),
        basis=basis,
        allow_pose_rebase=True,
    )
    QTest.qWait(140)
    assert controller.requests == [({"X": 1.001}, 120.0)]

    _finish_coordinate(
        session,
        controller,
        (1.001, 2.0, 3.0, 0.0, 0.0, 0.0),
    )

    assert controller.requests == [
        ({"X": 1.001}, 120.0),
        ({"X": 1.003, "Y": 1.998}, 120.0),
    ]


def test_opposite_steps_returning_to_reached_target_send_no_followup() -> None:
    session, controller = _session()
    _queue_exact(session, (("X", 1.001, 1.001),))
    QTest.qWait(140)
    _queue_exact(session, (("X", 1.001, 1.001),))
    QTest.qWait(140)

    _finish_coordinate(
        session,
        controller,
        (1.001, 2.0, 3.0, 0.0, 0.0, 0.0),
    )

    assert controller.requests == [({"X": 1.001}, 120.0)]


def test_lease_is_frozen_until_same_basis_pose_rebase_is_allowed() -> None:
    session, _controller = _session()
    lease_one = SimpleNamespace(basis_fingerprint=("frame", "alpha"), pose="one")
    lease_two = SimpleNamespace(basis_fingerprint=("frame", "alpha"), pose="two")
    basis = ("frame", "alpha")
    _queue_exact(
        session,
        (("X", 1.001, 1.001),),
        lease=lease_one,
        basis=basis,
    )
    assert session.snapshot().exact_step_motion_lease is lease_one
    _queue_exact(
        session,
        (("X", 1.002, 1.002),),
        lease=lease_two,
        basis=basis,
    )
    assert session.snapshot().exact_step_motion_lease is lease_one
    _queue_exact(
        session,
        (("X", 1.003, 1.003),),
        lease=lease_two,
        basis=basis,
        allow_pose_rebase=True,
    )
    snapshot = session.snapshot()
    assert snapshot.exact_step_motion_lease is lease_two
    assert snapshot.exact_step_pose_rebase_allowed is True


@pytest.mark.parametrize(
    "reason_name",
    ["CONTROL_MODE_CHANGED", "HOMING_REQUESTED", "MANUAL_TERMINAL_COMMAND"],
)
def test_mode_homing_and_terminal_clear_deferred_exact_steps(
    reason_name: str,
) -> None:
    session, controller = _session()
    _queue_exact(session, (("X", 1.001, 1.001),))
    reason_type = getattr(exact_step, "ExactStepClearReason", None)
    assert reason_type is not None, "ExactStepClearReason is absent"

    session.clear_exact_steps(getattr(reason_type, reason_name))

    QTest.qWait(100)
    assert controller.requests == []
    assert session.snapshot().exact_step_display_targets == ()


def test_failed_move_clears_deferred_exact_followup() -> None:
    session, controller = _session()
    _queue_exact(session, (("X", 1.001, 1.001),))
    QTest.qWait(140)
    _queue_exact(session, (("X", 1.002, 1.002),))
    QTest.qWait(140)

    session.on_movement_finished(False, "Move failed.")

    QTest.qWait(100)
    assert controller.requests == [({"X": 1.001}, 120.0)]
    assert session.snapshot().exact_step_display_targets == ()


def test_disconnect_reset_clears_deferred_exact_steps() -> None:
    session, controller = _session()
    _queue_exact(session, (("X", 1.001, 1.001),))

    session.reset(StageMotionResetReason.CONNECTION_CHANGED)

    QTest.qWait(100)
    assert controller.requests == []
    assert session.snapshot().exact_step_display_targets == ()
