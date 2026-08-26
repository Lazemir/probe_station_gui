from __future__ import annotations

import ast
import dataclasses
import importlib
from pathlib import Path

import pytest
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

from probe_station_gui.application.stage_motion_types import (
    PlannedXYMoveRequest,
    StageMotionConfig,
)
from probe_station_gui.application.stage_motion_session import (
    _StageMotionSession,
)
from probe_station_gui.coordinates.model import PhysicalMachinePose
from probe_station_gui.stage.coordinate_targets import CoordinateTargetConfig
from probe_station_gui.stage.manual_jog_prediction import ManualJogPredictionConfig
from probe_station_gui.stage.types import (
    StageMotionResetReason,
    StagePositionObservation,
    TrackedAbsoluteXYMoveFinished,
    TrackedAbsoluteXYMoveStarted,
)


SESSION_MODULE = "probe_station_gui.application.stage_motion_session"
SESSION_PATH = (
    Path(__file__).resolve().parents[2]
    / "probe_station_gui"
    / "application"
    / "stage_motion_session.py"
)


def test_canonical_stage_motion_session_types_exist_and_are_frozen() -> None:
    session_module = importlib.import_module(SESSION_MODULE)
    types_module = importlib.import_module(
        "probe_station_gui.application.stage_motion_types"
    )

    assert issubclass(session_module._StageMotionSession, QObject)
    for name in (
        "StageMotionConfig",
        "StageMotionSnapshot",
        "PlannedXYMoveRequest",
        "StageMotionPresentation",
        "StageMotionActionState",
    ):
        value_type = getattr(types_module, name)
        assert dataclasses.is_dataclass(value_type)
        assert value_type.__dataclass_params__.frozen is True
        assert not hasattr(session_module, name)
    assert StageMotionResetReason.CONNECTION_CHANGED.name == "CONNECTION_CHANGED"


def test_stage_motion_session_starts_as_a_leaf_module() -> None:
    assert SESSION_PATH.is_file(), "canonical stage motion session module is absent"
    source = SESSION_PATH.read_text(encoding="utf-8")
    forbidden_imports = (
        "import main",
        "probe_station_gui.camera",
        "probe_station_gui.dialogs",
        "probe_station_gui.route",
        "probe_station_gui.views",
        "probe_station_gui.application.camera_pipeline",
        "probe_station_gui.application.registration_focus",
        "probe_station_gui.application.stage_design_position",
    )
    assert not any(value in source for value in forbidden_imports)
    assert "__getattr__" not in source
    assert "owner=self" not in source


def test_reset_reason_has_one_stage_owner_and_no_reverse_application_import() -> None:
    session_module = importlib.import_module(SESSION_MODULE)
    assert not hasattr(session_module, "StageMotionResetReason")

    root = Path(__file__).resolve().parents[2]
    owners: list[str] = []
    for path in (root / "probe_station_gui").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.ClassDef) and node.name == "StageMotionResetReason"
            for node in ast.walk(tree)
        ):
            owners.append(path.relative_to(root).as_posix())
    assert owners == ["probe_station_gui/stage/types.py"]

    connection_path = (
        root / "probe_station_gui" / "views" / "main_window_connection_flow.py"
    )
    connection_tree = ast.parse(
        connection_path.read_text(encoding="utf-8"),
        filename=str(connection_path),
    )
    imported_modules = {
        node.module
        for node in ast.walk(connection_tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "probe_station_gui.stage.types" in imported_modules
    assert not any(
        module.startswith("probe_station_gui.application")
        for module in imported_modules
    )


class _Controller:
    DEFAULT_FEEDRATE = 600.0

    def __init__(self) -> None:
        self.accepted = True
        self.state = "idle"
        self.position: tuple[float, ...] | None = (1.0, 2.0, 3.0, 4.0, 5.0)
        self.status_timestamp: float | None = 10.0
        self.homed = {"X", "Y", "Z"}
        self.pose = PhysicalMachinePose.from_mapping({"X": 11.0, "Y": 22.0, "B": 55.0})
        self.move_requests: list[tuple[float, float, object | None]] = []
        self.pose_axes: list[tuple[str, ...]] = []

    def request_move_to_xy(
        self,
        x_mm: float,
        y_mm: float,
        *,
        motion_token: object | None = None,
    ) -> bool:
        self.move_requests.append((float(x_mm), float(y_mm), motion_token))
        return self.accepted

    def latest_stage_position(self) -> tuple[float, ...] | None:
        return self.position

    def latest_stage_state(self) -> str:
        return self.state

    def last_status_timestamp(self) -> float | None:
        return self.status_timestamp

    def is_busy(self) -> bool:
        return False

    def latest_physical_machine_pose(
        self,
        axes: tuple[str, ...],
    ) -> PhysicalMachinePose:
        self.pose_axes.append(tuple(axes))
        return self.pose

    def axes_are_homed(self, axes: set[str]) -> bool:
        return axes.issubset(self.homed)

    def request_status_refresh(self) -> None:
        raise AssertionError("cached session refresh must not request controller I/O")


def _config() -> StageMotionConfig:
    axes = ("X", "Y", "Z", "A", "B", "C")
    return StageMotionConfig(
        axis_names=axes,
        prediction_interval_ms=50,
        planned_move_duration_padding_s=0.12,
        planned_start_tolerance_mm=1e-4,
        min_feedrate_mm_min=1.0,
        b_position_change_tolerance_deg=1e-3,
        manual_jog=ManualJogPredictionConfig(
            axis_names=axes,
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
            axis_names=axes,
            min_feedrate_mm_min=1.0,
            duration_padding_s=0.12,
            min_idle_accept_s=0.15,
            target_tolerance_mm=7.5e-4,
        ),
    )


def _session() -> tuple[_StageMotionSession, _Controller]:
    QApplication.instance() or QApplication([])
    controller = _Controller()
    return _StageMotionSession(controller, _config()), controller


def _observation(
    controller: _Controller,
    position: object,
) -> StagePositionObservation:
    return StagePositionObservation(
        position=position,
        physical_machine_pose=controller.pose,
        motion_coordinate_snapshot=None,
        stage_state=controller.state,
        homed_axes=frozenset(controller.homed),
        status_timestamp=controller.status_timestamp,
        last_jog_write_timestamp=None,
    )


def _observe(
    session: _StageMotionSession,
    controller: _Controller,
    position: object,
) -> None:
    session.on_stage_position_changed(_observation(controller, position))


def _request(
    target: tuple[float, float] = (4.0, 6.0),
    *,
    source: str = "design window",
) -> PlannedXYMoveRequest:
    return PlannedXYMoveRequest(target_stage_xy=target, source_label=source)


def _motion_token(controller: _Controller) -> object:
    token = controller.move_requests[-1][2]
    assert token is not None
    return token


def _tracked_start(
    session: _StageMotionSession,
    controller: _Controller,
    *,
    target: tuple[float, float] = (4.0, 6.0),
    feedrate_mm_min: float = 300.0,
    motion_token: object | None = None,
) -> None:
    session.on_tracked_absolute_xy_move_started(
        TrackedAbsoluteXYMoveStarted(
            motion_token=(
                _motion_token(controller) if motion_token is None else motion_token
            ),
            origin_position=tuple(float(value) for value in controller.position),
            target_stage_xy=target,
            feedrate_mm_min=feedrate_mm_min,
        )
    )


def _tracked_finish(
    session: _StageMotionSession,
    controller: _Controller,
    *,
    success: bool,
    message: str,
    target: tuple[float, float] = (4.0, 6.0),
    motion_token: object | None = None,
    status_timestamp: float | None = None,
) -> None:
    session.on_tracked_absolute_xy_move_finished(
        TrackedAbsoluteXYMoveFinished(
            motion_token=(
                _motion_token(controller) if motion_token is None else motion_token
            ),
            target_stage_xy=target,
            success=success,
            message=message,
            status_timestamp=(
                controller.status_timestamp
                if status_timestamp is None
                else status_timestamp
            ),
        )
    )


def test_idle_snapshot_contains_only_immutable_motion_facts() -> None:
    session, _controller = _session()

    snapshot = session.snapshot()

    assert snapshot.presented_position is None
    assert snapshot.presented_stage_xy is None
    assert snapshot.physical_machine_pose.to_dict() == {}
    assert snapshot.active_axes == frozenset()
    assert snapshot.coordinate_active is False
    assert snapshot.manual_prediction_active is False
    assert snapshot.planned_pending_target_xy is None
    assert snapshot.planned_prediction_active is False


def test_rejected_planned_request_does_not_arm_pending_state() -> None:
    session, controller = _session()
    controller.accepted = False
    before = session.snapshot()

    accepted = session.request_planned_xy_move(_request())

    assert accepted is False
    assert [request[:2] for request in controller.move_requests] == [(4.0, 6.0)]
    rejected_token = _motion_token(controller)
    _tracked_start(session, controller, motion_token=rejected_token)
    _tracked_finish(
        session,
        controller,
        success=False,
        message="late rejected failure",
        motion_token=rejected_token,
    )
    snapshot = session.snapshot()
    assert snapshot.planned_pending_target_xy is None
    assert snapshot.planned_pending_source_label is None
    assert snapshot.planned_prediction_active is False
    assert snapshot == before
    assert session._prediction_timer.isActive() is False


def test_accepted_planned_request_arms_only_target_and_source() -> None:
    session, controller = _session()

    accepted = session.request_planned_xy_move(_request())

    assert accepted is True
    assert [request[:2] for request in controller.move_requests] == [(4.0, 6.0)]
    snapshot = session.snapshot()
    assert snapshot.planned_pending_target_xy == (4.0, 6.0)
    assert snapshot.planned_pending_source_label == "design window"
    assert snapshot.planned_stage_xy is None
    assert snapshot.planned_prediction_active is False


@pytest.mark.parametrize("state", ["pending", "active", "waiting"])
def test_second_planned_request_is_transactionally_rejected(state: str) -> None:
    session, controller = _session()
    assert session.request_planned_xy_move(_request()) is True
    if state in {"active", "waiting"}:
        _tracked_start(session, controller)
    if state == "waiting":
        _tracked_finish(session, controller, success=True, message="Arrived")
    before = session.snapshot()
    timer_active = session._prediction_timer.isActive()

    accepted = session.request_planned_xy_move(
        _request((8.0, 9.0), source="second design target")
    )

    assert accepted is False
    assert len(controller.move_requests) == 1
    assert session.snapshot() == before
    assert session._prediction_timer.isActive() is timer_active


def test_matching_actual_start_begins_interpolation() -> None:
    session, controller = _session()
    presentations = []
    actions = []
    session.presentation_changed.connect(presentations.append)
    session.action_state_changed.connect(actions.append)
    assert session.request_planned_xy_move(_request()) is True

    _tracked_start(session, controller)

    snapshot = session.snapshot()
    assert snapshot.planned_pending_target_xy is None
    assert snapshot.planned_prediction_active is True
    assert snapshot.planned_stage_xy == (1.0, 2.0)
    assert snapshot.active_axes == frozenset({"X", "Y"})
    assert presentations[-1].active_axes == frozenset({"X", "Y"})
    assert actions[-1].active_axes == frozenset({"X", "Y"})


def test_tracked_start_and_tick_survive_invalid_observation_without_cache_rereads(
    monkeypatch,
) -> None:
    session, controller = _session()
    cache_reads: list[str] = []

    def unexpected_cache_read(name: str):
        def read() -> object:
            cache_reads.append(name)
            raise AssertionError(f"unexpected mutable cache read: {name}")

        return read

    controller.latest_stage_position = unexpected_cache_read("position")
    controller.last_status_timestamp = unexpected_cache_read("status timestamp")
    assert session.request_planned_xy_move(_request()) is True
    _tracked_start(session, controller)
    session.on_stage_position_changed(_observation(controller, None))
    monkeypatch.setattr(
        "probe_station_gui.application.stage_motion_session.time.monotonic",
        lambda: 1.0e12,
    )

    session.tick()

    controller.last_status_timestamp = lambda: controller.status_timestamp
    snapshot = session.snapshot()
    assert cache_reads == []
    assert snapshot.presented_stage_xy == (4.0, 6.0)
    assert snapshot.planned_waiting_for_fresh_status is True


def test_mismatched_actual_start_clears_pending_arm() -> None:
    session, controller = _session()
    assert session.request_planned_xy_move(_request()) is True

    _tracked_start(session, controller, target=(4.001, 6.0))

    snapshot = session.snapshot()
    assert snapshot.planned_pending_target_xy is None
    assert snapshot.planned_pending_source_label is None
    assert snapshot.planned_prediction_active is False


def test_success_holds_target_until_status_timestamp_is_newer() -> None:
    session, controller = _session()
    assert session.request_planned_xy_move(_request()) is True
    _tracked_start(session, controller)

    _tracked_finish(session, controller, success=True, message="Arrived")
    _observe(session, controller, (3.5, 5.5, 3.0, 4.0, 5.0))

    waiting = session.snapshot()
    assert waiting.planned_waiting_for_fresh_status is True
    assert waiting.presented_stage_xy == (4.0, 6.0)

    controller.status_timestamp = 10.1
    _observe(session, controller, (3.5, 5.5, 3.0, 4.0, 5.0))

    fresh = session.snapshot()
    assert fresh.planned_waiting_for_fresh_status is False
    assert fresh.presented_stage_xy == (3.5, 5.5)


def test_success_after_prediction_reaches_target_still_waits_for_newer_status(
    monkeypatch,
) -> None:
    session, controller = _session()
    assert session.request_planned_xy_move(_request()) is True
    _tracked_start(session, controller)
    monkeypatch.setattr(
        "probe_station_gui.application.stage_motion_session.time.monotonic",
        lambda: 1.0e12,
    )

    session.tick()
    _tracked_finish(session, controller, success=True, message="Arrived")
    _observe(session, controller, (3.5, 5.5, 3.0, 4.0, 5.0))

    waiting = session.snapshot()
    assert waiting.planned_waiting_for_fresh_status is True
    assert waiting.presented_stage_xy == (4.0, 6.0)

    controller.status_timestamp = 10.1
    _observe(session, controller, (3.5, 5.5, 3.0, 4.0, 5.0))

    fresh = session.snapshot()
    assert fresh.planned_waiting_for_fresh_status is False
    assert fresh.presented_stage_xy == (3.5, 5.5)


def test_failed_owned_completion_clears_planned_prediction() -> None:
    session, controller = _session()
    assert session.request_planned_xy_move(_request()) is True
    _tracked_start(session, controller)

    _tracked_finish(session, controller, success=False, message="Move failed")

    snapshot = session.snapshot()
    assert snapshot.planned_stage_xy is None
    assert snapshot.planned_prediction_active is False
    assert snapshot.planned_waiting_for_fresh_status is False
    assert snapshot.active_axes == frozenset()


def test_unrelated_completion_does_not_mutate_unowned_session_state() -> None:
    session, controller = _session()
    before = session.snapshot()

    _tracked_finish(
        session,
        controller,
        success=False,
        message="Other task failed",
        motion_token=object(),
    )

    assert session.snapshot() == before


def test_mismatched_token_cannot_mutate_pending_planned_request() -> None:
    session, controller = _session()
    assert session.request_planned_xy_move(_request()) is True
    before = session.snapshot()

    _tracked_start(session, controller, motion_token=object())
    _tracked_finish(
        session,
        controller,
        success=False,
        message="stale failure",
        motion_token=object(),
    )

    assert session.snapshot() == before


def test_old_token_cannot_clear_new_planned_request() -> None:
    session, controller = _session()
    assert session.request_planned_xy_move(_request()) is True
    old_token = _motion_token(controller)
    assert session.cancel_planned_xy_move() is True
    assert session.request_planned_xy_move(_request((8.0, 9.0))) is True
    new_token = _motion_token(controller)
    before = session.snapshot()

    assert new_token != old_token
    _tracked_start(
        session,
        controller,
        target=(4.0, 6.0),
        motion_token=old_token,
    )
    _tracked_finish(
        session,
        controller,
        success=False,
        message="old failure",
        motion_token=old_token,
    )

    assert session.snapshot() == before


@pytest.mark.parametrize("state", ["pending", "active", "waiting"])
def test_same_token_wrong_finish_target_is_inert_across_planned_lifecycle(
    monkeypatch,
    state: str,
) -> None:
    session, controller = _session()
    actions = []
    session.action_state_changed.connect(actions.append)
    assert session.request_planned_xy_move(_request()) is True
    token = _motion_token(controller)
    if state in {"active", "waiting"}:
        _tracked_start(session, controller, motion_token=token)
    if state == "waiting":
        monkeypatch.setattr(
            "probe_station_gui.application.stage_motion_session.time.monotonic",
            lambda: 1.0e12,
        )
        session.tick()
    before = session.snapshot()
    timer_active = session._prediction_timer.isActive()
    action_count = len(actions)

    _tracked_finish(
        session,
        controller,
        success=True,
        message="wrong target",
        target=(4.01, 6.0),
        motion_token=token,
    )

    assert session.snapshot() == before
    assert session._prediction_timer.isActive() is timer_active
    assert len(actions) == action_count

    _tracked_finish(
        session,
        controller,
        success=True,
        message="Arrived",
        motion_token=token,
    )
    consumed = session.snapshot()
    consumed_action_count = len(actions)
    assert consumed.planned_waiting_for_fresh_status is True
    assert consumed_action_count == action_count + 1

    _tracked_finish(
        session,
        controller,
        success=False,
        message="duplicate stale failure",
        motion_token=token,
    )
    assert session.snapshot() == consumed
    assert len(actions) == consumed_action_count

    controller.status_timestamp = 10.1
    _observe(session, controller, (3.5, 5.5, 3.0, 4.0, 5.0))
    assert session.snapshot().planned_waiting_for_fresh_status is False


def test_matching_completion_consumes_token_exactly_once() -> None:
    session, controller = _session()
    assert session.request_planned_xy_move(_request()) is True
    token = _motion_token(controller)
    _tracked_start(session, controller, motion_token=token)
    _tracked_finish(
        session,
        controller,
        success=True,
        message="Arrived",
        motion_token=token,
    )
    consumed = session.snapshot()

    _tracked_finish(
        session,
        controller,
        success=False,
        message="duplicate stale failure",
        motion_token=token,
    )

    assert session.snapshot() == consumed
