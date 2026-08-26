from __future__ import annotations

import dataclasses
import types

import pytest
from PySide6.QtWidgets import QApplication

from probe_station_gui.application import stage_motion_session as motion_session
from probe_station_gui.application import stage_motion_types as motion_types
from probe_station_gui.coordinates.model import PhysicalMachinePose
from probe_station_gui.stage import coordinate_targets
from probe_station_gui.stage.coordinate_targets import CoordinateTargetConfig
from probe_station_gui.stage import types as stage_types
from probe_station_gui.stage.manual_jog_prediction import ManualJogPredictionConfig
from probe_station_gui.stage.types import StagePositionObservation


AXES = ("X", "Y", "Z", "A", "B", "C")


class _Controller:
    DEFAULT_FEEDRATE = 600.0

    def __init__(self) -> None:
        self.accept_start = True
        self.accept_reissue = True
        self.busy = False
        self.state = "Idle"
        self.position = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
        self.max_feedrates = {"X": 500.0, "Y": 600.0, "Z": 80.0}
        self.requests: list[tuple[str, object]] = []
        self.feed_override_resets = 0
        self.events: list[str] = []
        self.status_refreshes = 0
        self.display_limit_reads: list[str] = []
        self.machine_limit_reads: list[str] = []

    def latest_stage_position(self) -> tuple[float, ...]:
        return self.position

    def latest_stage_state(self) -> str:
        return self.state

    def last_status_timestamp(self) -> float | None:
        return None

    def is_busy(self) -> bool:
        return self.busy

    def axis_max_feedrates(self) -> dict[str, float]:
        return dict(self.max_feedrates)

    def axis_display_limits(self, axis: str) -> tuple[float, float] | None:
        self.display_limit_reads.append(str(axis))
        return (0.0, 20.0)

    def axis_machine_display_limits(self, axis: str) -> tuple[float, float] | None:
        self.machine_limit_reads.append(str(axis))
        return (-2.0, 8.0)

    def request_absolute_axis_targets_move(
        self, targets: dict[str, float], *, feedrate: float
    ) -> bool:
        self.events.append("request")
        self.requests.append(("start", (dict(targets), float(feedrate))))
        return self.accept_start

    def queue_absolute_axis_targets_jog(
        self,
        targets: dict[str, float],
        *,
        feedrate: float,
        replace_active: bool,
    ) -> bool:
        self.requests.append(
            ("reissue", (dict(targets), float(feedrate), bool(replace_active)))
        )
        return self.accept_reissue

    def queue_feed_override_reset(self) -> None:
        self.feed_override_resets += 1

    def request_status_refresh(self) -> None:
        self.status_refreshes += 1


def _config() -> motion_types.StageMotionConfig:
    values = dict(
        axis_names=AXES,
        prediction_interval_ms=50,
        planned_move_duration_padding_s=0.12,
        planned_start_tolerance_mm=1e-4,
        min_feedrate_mm_min=1.0,
        b_position_change_tolerance_deg=1e-3,
        manual_jog=ManualJogPredictionConfig(
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
        coordinate_target=CoordinateTargetConfig(
            axis_names=AXES,
            min_feedrate_mm_min=1.0,
            duration_padding_s=0.0,
            min_idle_accept_s=0.0,
            target_tolerance_mm=0.01,
        ),
    )
    if (
        "settle_status_poll_delays_ms"
        in motion_types.StageMotionConfig.__dataclass_fields__
    ):
        values["settle_status_poll_delays_ms"] = (40, 120)
    return motion_types.StageMotionConfig(**values)


def _session() -> tuple[motion_session._StageMotionSession, _Controller]:
    QApplication.instance() or QApplication([])
    controller = _Controller()
    return motion_session._StageMotionSession(controller, _config()), controller


def _request(
    targets: tuple[tuple[str, float, float], ...] = (("X", 5.0, 15.0),),
    *,
    feedrate: float = 120.0,
    basis: object | None = ("design", "frame-a"),
    physical_limit_targets: tuple[tuple[str, float], ...] = (),
) -> object:
    request_type = getattr(coordinate_targets, "CoordinateMoveRequest", None)
    values = dict(
        targets=targets,
        feedrate_mm_min=feedrate,
        source_label="coordinate fields",
        seed_position=(1.0, 2.0, 3.0, 4.0, 5.0, 6.0),
        physical_limit_targets=physical_limit_targets,
        display_basis=basis,
    )
    if request_type is None:
        return types.SimpleNamespace(**values)
    return request_type(**values)


def _observe(
    session: motion_session._StageMotionSession,
    controller: _Controller,
    *,
    state: str,
    position: tuple[float, ...],
    timestamp: float,
) -> None:
    controller.state = state
    controller.position = position
    session.on_stage_position_changed(
        StagePositionObservation(
            position=position,
            physical_machine_pose=PhysicalMachinePose.from_mapping(
                {axis: value for axis, value in zip(AXES, position)}
            ),
            motion_coordinate_snapshot=None,
            stage_state=state,
            homed_axes=frozenset(AXES),
            status_timestamp=timestamp,
            last_jog_write_timestamp=None,
        )
    )


def test_coordinate_interface_values_are_frozen() -> None:
    for name in (
        "CoordinateMoveRequest",
        "CoordinateMoveCompletion",
        "CoordinateMoveDisposition",
        "CoordinatePendingEdits",
    ):
        value_type = getattr(coordinate_targets, name, None)
        assert value_type is not None, name
        if name != "CoordinateMoveDisposition":
            assert dataclasses.is_dataclass(value_type)
            assert value_type.__dataclass_params__.frozen is True
    assert dataclasses.is_dataclass(stage_types.UnclaimedMovementCompletion)
    assert stage_types.UnclaimedMovementCompletion.__dataclass_params__.frozen is True


def test_pending_edits_are_frozen_and_consumed_atomically() -> None:
    session, _controller = _session()
    lease = ("lease", 4)

    session.upsert_pending_coordinate_edit("X", 5.0, 15.0, motion_lease=lease)
    session.upsert_pending_coordinate_edit("Y", 6.0, 16.0, motion_lease=lease)

    pending = session.pending_coordinate_edits()
    assert pending.targets == (("X", 5.0, 15.0), ("Y", 6.0, 16.0))
    assert pending.motion_lease == lease
    assert session.snapshot().pending_edit_axes == frozenset({"X", "Y"})
    assert not isinstance(pending.targets, dict)
    consumed = session.consume_pending_coordinate_edits()
    assert consumed == pending
    assert session.pending_coordinate_edits().targets == ()
    assert session.snapshot().pending_edit_axes == frozenset()


def test_display_and_physical_limits_use_cached_controller_accessors() -> None:
    session, controller = _session()
    statuses: list[tuple[str, int]] = []
    session.status_requested.connect(
        lambda text, timeout: statuses.append((text, timeout))
    )
    _observe(
        session,
        controller,
        state="Idle",
        position=controller.position,
        timestamp=10.0,
    )

    display_rejected = session.start_coordinate_move(_request((("X", 5.0, 25.0),)))
    physical_request = _request(
        (("X", 5.0, 15.0),),
        physical_limit_targets=(("X", 9.0),),
    )
    physical_rejected = session.start_coordinate_move(physical_request)

    assert display_rejected is False
    assert physical_rejected is False
    assert controller.requests == []
    assert controller.display_limit_reads == ["X"]
    assert controller.machine_limit_reads == ["X"]
    assert statuses == [
        ("X target +25.000 exceeds software limits (0.000..20.000).", 4000),
        ("X target +9.000 exceeds software limits (-2.000..8.000).", 4000),
    ]


def test_rejected_start_never_arms_coordinate_completion_purpose() -> None:
    session, controller = _session()
    controller.accept_start = False
    completions = []
    session.coordinate_move_finished.connect(completions.append)

    assert session.start_coordinate_move(_request()) is False
    before = session.snapshot()
    session.on_movement_finished(False, "Late rejected completion.")

    assert session.snapshot() == before
    assert completions == []


def test_start_rejection_rolls_back_and_uses_only_controller_safety_request() -> None:
    session, controller = _session()
    controller.accept_start = False
    session.upsert_pending_coordinate_edit(
        "X", 5.0, 15.0, motion_lease=("design", "frame-a")
    )
    statuses: list[tuple[str, int]] = []
    session.status_requested.connect(
        lambda text, timeout: statuses.append((text, timeout))
    )

    accepted = session.start_coordinate_move(_request())

    assert accepted is False
    assert controller.requests == [("start", ({"X": 5.0}, 120.0))]
    assert session.snapshot().coordinate_active is False
    assert session.snapshot().active_axes == frozenset()
    assert session.snapshot().coordinate_common_feedrate.clear_common_target is True
    assert session.pending_coordinate_edits().targets == ()
    assert controller.feed_override_resets == 1
    assert statuses == []


def test_validation_rejection_preserves_owned_pending_edits() -> None:
    session, controller = _session()
    session.upsert_pending_coordinate_edit(
        "X", 5.0, 25.0, motion_lease=("design", "frame-a")
    )
    _observe(
        session,
        controller,
        state="Idle",
        position=controller.position,
        timestamp=10.0,
    )

    accepted = session.start_coordinate_move(_request((("X", 5.0, 25.0),)))

    assert accepted is False
    assert session.pending_coordinate_edits().targets == (("X", 5.0, 25.0),)


def test_accepted_start_publishes_origin_before_return_and_records_feedrates() -> None:
    session, controller = _session()
    presentations = []
    actions = []
    session.presentation_changed.connect(presentations.append)
    session.action_state_changed.connect(actions.append)

    accepted = session.start_coordinate_move(_request())

    assert accepted is True
    snapshot = session.snapshot()
    assert snapshot.presented_position == controller.position
    assert snapshot.coordinate_programmed_feedrate == 120.0
    assert snapshot.coordinate_effective_feedrate == 120.0
    assert snapshot.coordinate_display_targets == (("X", 15.0),)
    assert presentations[-1].presented_position == controller.position
    assert actions[-1].coordinate_active is True


def test_accepted_start_orders_request_then_status_then_presentation() -> None:
    session, controller = _session()
    session.status_requested.connect(
        lambda _text, _timeout: controller.events.append("status")
    )
    session.presentation_changed.connect(
        lambda _presentation: controller.events.append("presentation")
    )

    assert session.start_coordinate_move(_request()) is True

    assert controller.events == ["request", "status", "presentation"]


def test_mixed_axis_start_exposes_common_feedrate_bound() -> None:
    session, _controller = _session()

    assert session.start_coordinate_move(_request((("X", 5.0, 15.0), ("Z", 4.0, 14.0))))

    common = session.snapshot().coordinate_common_feedrate
    assert common.clear_common_target is False
    assert common.feedrate_mm_min == 120.0
    assert common.max_feedrate_mm_min == 500.0


def test_idle_before_active_observation_does_not_finish_coordinate_move() -> None:
    session, controller = _session()
    completions = []
    session.coordinate_move_finished.connect(completions.append)
    assert session.start_coordinate_move(_request())

    _observe(
        session,
        controller,
        state="Idle",
        position=(5.0, 2.0, 3.0, 4.0, 5.0, 6.0),
        timestamp=11.0,
    )

    assert session.snapshot().coordinate_active is True
    assert completions == []


def test_active_then_idle_at_target_emits_typed_completion_and_homing_continuation() -> (
    None
):
    session, controller = _session()
    completions = []
    continuations: list[bool] = []
    session.coordinate_move_finished.connect(completions.append)
    session.continue_homing_requested.connect(lambda: continuations.append(True))
    basis = ("design", "frame-a")
    assert session.start_coordinate_move(_request(basis=basis))
    _observe(
        session,
        controller,
        state="Run",
        position=(2.0, 2.0, 3.0, 4.0, 5.0, 6.0),
        timestamp=11.0,
    )

    _observe(
        session,
        controller,
        state="Idle",
        position=(5.0, 2.0, 3.0, 4.0, 5.0, 6.0),
        timestamp=12.0,
    )

    assert session.snapshot().coordinate_active is False
    assert controller.feed_override_resets == 1
    assert len(completions) == 1
    completion = completions[0]
    assert completion.success is True
    assert completion.disposition.name == "COMPLETED"
    assert completion.display_targets == (("X", 15.0),)
    assert completion.display_basis == basis
    assert continuations == [True]


@pytest.mark.parametrize(
    ("success", "message", "disposition"),
    [
        (False, "Limit reached.", "FAILED"),
        (True, "Move skipped.", "SKIPPED"),
        (True, "Stage is already there.", "ALREADY"),
        (True, "Target unchanged.", "UNCHANGED"),
    ],
)
def test_terminal_dispositions_clear_coordinate_tracking(
    success: bool,
    message: str,
    disposition: str,
) -> None:
    session, _controller = _session()
    completions = []
    continuations: list[bool] = []
    session.coordinate_move_finished.connect(completions.append)
    session.continue_homing_requested.connect(lambda: continuations.append(True))
    assert session.start_coordinate_move(_request())

    session.on_movement_finished(success, message)

    assert session.snapshot().coordinate_active is False
    assert completions[-1].success is success
    assert completions[-1].disposition.name == disposition
    assert completions[-1].message == message
    assert continuations == []


def test_unrelated_completion_without_coordinate_purpose_is_inert() -> None:
    session, _controller = _session()
    before = session.snapshot()
    completions = []
    session.coordinate_move_finished.connect(completions.append)

    session.on_movement_finished(False, "Other task failed.")

    assert session.snapshot() == before
    assert completions == []


def test_claimed_success_disarms_purpose_but_tracking_stays_until_idle(
    monkeypatch,
) -> None:
    session, controller = _session()
    statuses: list[tuple[str, int]] = []
    completions = []
    session.status_requested.connect(
        lambda text, timeout: statuses.append((text, timeout))
    )
    session.coordinate_move_finished.connect(completions.append)
    scheduled_polls: list[bool] = []
    monkeypatch.setattr(
        session._settle_status_polls,
        "schedule",
        lambda: scheduled_polls.append(True),
    )
    assert session.start_coordinate_move(_request())
    statuses.clear()

    session.on_movement_finished(True, "Move complete.")

    assert session.snapshot().coordinate_active is True
    assert statuses == [("Move complete.", 5000)]
    assert completions == []
    assert scheduled_polls == [True]

    session.on_movement_finished(False, "Other task failed.")

    assert session.snapshot().coordinate_active is True
    assert completions == []


def test_feedrate_reissue_success_updates_programmed_and_effective_feedrates() -> None:
    session, controller = _session()
    controller.busy = True
    controller.state = "Run"
    assert session.start_coordinate_move(_request())

    session.set_coordinate_feedrate(180.0)

    assert controller.requests[-1] == (
        "reissue",
        ({"X": 5.0}, 180.0, True),
    )
    snapshot = session.snapshot()
    assert snapshot.coordinate_programmed_feedrate == 180.0
    assert snapshot.coordinate_effective_feedrate == 180.0


def test_expected_reissue_cancellation_retains_tracking_and_suppresses_failure() -> (
    None
):
    session, controller = _session()
    controller.busy = True
    controller.state = "Run"
    completions = []
    statuses: list[tuple[str, int]] = []
    session.coordinate_move_finished.connect(completions.append)
    session.status_requested.connect(
        lambda text, timeout: statuses.append((text, timeout))
    )
    assert session.start_coordinate_move(_request())
    session.set_coordinate_feedrate(180.0)
    statuses.clear()

    session.on_movement_finished(False, "Operation cancelled.")

    session.on_movement_finished(True, "Move complete.")

    assert session.snapshot().coordinate_active is True
    assert session.snapshot().coordinate_programmed_feedrate == 180.0
    assert completions == []
    assert statuses == [("Move complete.", 5000)]


def test_rejected_feedrate_reissue_retains_original_tracking() -> None:
    session, controller = _session()
    controller.busy = True
    controller.state = "Run"
    controller.accept_reissue = False
    assert session.start_coordinate_move(_request())

    session.set_coordinate_feedrate(180.0)

    snapshot = session.snapshot()
    assert snapshot.coordinate_active is True
    assert snapshot.coordinate_programmed_feedrate == 120.0
    assert snapshot.coordinate_effective_feedrate == 120.0


def test_stale_idle_feedrate_change_clears_without_prediction_or_reissue() -> None:
    session, controller = _session()
    assert session.start_coordinate_move(_request())
    controller.state = "Idle"
    controller.busy = False
    presentations = []
    session.presentation_changed.connect(presentations.append)

    session.set_coordinate_feedrate(180.0)

    assert session.snapshot().coordinate_active is False
    assert presentations == []
    assert [kind for kind, _payload in controller.requests] == ["start"]


@pytest.mark.parametrize("expected_cancel", [False, True])
def test_feedrate_reissue_failure_schedules_settle_polls(
    monkeypatch,
    expected_cancel: bool,
) -> None:
    session, controller = _session()
    assert session.start_coordinate_move(_request())
    controller.busy = True
    controller.state = "Run"
    scheduled_polls: list[bool] = []
    monkeypatch.setattr(
        session._settle_status_polls,
        "schedule",
        lambda: scheduled_polls.append(True),
    )
    if not expected_cancel:
        controller.accept_reissue = False

    session.set_coordinate_feedrate(180.0)
    if expected_cancel:
        session.on_movement_finished(False, "Operation cancelled.")

    assert scheduled_polls == [True]
