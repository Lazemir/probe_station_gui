from __future__ import annotations

import dataclasses
import inspect
import types

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from tests.stage.controller_test_support import StageController

from probe_station_gui.coordinates.model import PhysicalMachinePose
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateSystemSnapshot,
    CoordinateTransition,
    RegistrationWorkflowSnapshot,
)
from probe_station_gui.settings.axis_calibration_config import (
    AxisCalibrationSettings,
    default_axis_calibrations,
)
from probe_station_gui.stage.axis_calibration import StageAxisCalibrationMapper
from probe_station_gui.stage import types as stage_types
from probe_station_gui.stage.controller import StageController as QtStageController

from probe_station_gui.stage.manual_jog_prediction import (
    ManualJogPredictionConfig,
    ManualJogPredictionState,
)
from probe_station_gui.stage import position_update


AXES = ("X", "Y", "Z", "A", "B")


def _real_controller_with_x_curve() -> StageController:
    controller = StageController()
    calibrations = default_axis_calibrations()
    calibrations["X"] = AxisCalibrationSettings(
        enabled=True,
        calibration_file="x.npz",
        controller_points=[0.0, 10.0, 20.0],
        physical_points=[0.0, 12.0, 30.0],
    )
    controller.apply_axis_calibrations(calibrations)
    return controller


def test_latest_physical_machine_pose_maps_one_synchronized_generation_once(
    monkeypatch,
) -> None:
    controller = _real_controller_with_x_curve()
    controller._last_synchronized_machine_position = (
        15.0,
        22.0,
        3.0,
        4.0,
        5.0,
        6.0,
    )
    real_mapper = controller._axis_calibration_mapper
    mapper_calls = 0

    def counted_mapper():
        nonlocal mapper_calls
        mapper_calls += 1
        return real_mapper()

    monkeypatch.setattr(controller, "_axis_calibration_mapper", counted_mapper)
    monkeypatch.setattr(
        controller,
        "_query_current_stage_position_status",
        lambda: pytest.fail("public cached accessor queried the controller"),
    )

    try:
        pose = controller.latest_physical_machine_pose(("Y", "X", "B"))

        assert isinstance(pose, PhysicalMachinePose)
        assert pose.to_dict() == pytest.approx({"X": 21.0, "Y": 22.0, "B": 5.0})
        assert mapper_calls == 1
    finally:
        controller.shutdown()


def test_latest_physical_machine_pose_omits_only_out_of_domain_axis() -> None:
    controller = StageController()
    calibrations = default_axis_calibrations()
    calibrations["X"] = AxisCalibrationSettings(
        enabled=True,
        calibration_file="x.npz",
        controller_points=[0.0, 1.0],
        physical_points=[0.0, 2.0],
    )
    controller.apply_axis_calibrations(calibrations)
    controller._last_synchronized_machine_position = (2.0, 3.0, 4.0)

    try:
        pose = controller.latest_physical_machine_pose(("X", "Y", "Z"))

        assert pose.to_dict() == pytest.approx({"Y": 3.0, "Z": 4.0})
    finally:
        controller.shutdown()


def test_latest_physical_machine_pose_returns_empty_pose_without_snapshot() -> None:
    controller = StageController()

    try:
        pose = controller.latest_physical_machine_pose(AXES)

        assert pose == PhysicalMachinePose.from_mapping({})
    finally:
        controller.shutdown()


def test_latest_physical_machine_pose_omits_invalid_nonfinite_and_unknown_axes() -> (
    None
):
    controller = StageController()
    controller._last_synchronized_machine_position = (1.0, float("nan"), 3.0)

    try:
        pose = controller.latest_physical_machine_pose((" x ", "Y", "Q", "B"))

        assert pose.to_dict() == {"X": 1.0}
    finally:
        controller.shutdown()


def test_stage_position_observation_is_an_immutable_stage_value() -> None:
    observation_type = getattr(stage_types, "StagePositionObservation", None)

    assert observation_type is not None
    assert dataclasses.is_dataclass(observation_type)
    assert observation_type.__dataclass_params__.frozen is True


def test_stage_position_observation_compares_motion_facts_not_generation_metadata() -> (
    None
):
    observation = stage_types.StagePositionObservation(
        position=(1.0, 2.0, 3.0),
        physical_machine_pose=PhysicalMachinePose.from_mapping({"X": 11.0}),
        motion_coordinate_snapshot=None,
        stage_state="Idle",
        homed_axes=frozenset({"X", "Y", "Z"}),
        status_timestamp=10.0,
        last_jog_write_timestamp=4.0,
    )

    assert observation.has_same_motion_facts_as(
        dataclasses.replace(observation, status_timestamp=11.0)
    )
    assert observation.has_same_motion_facts_as(
        dataclasses.replace(
            observation,
            reset_reason=stage_types.StageMotionResetReason.CONNECTION_CHANGED,
        )
    )
    assert not observation.has_same_motion_facts_as(object())
    for changes in (
        {"position": (9.0, 2.0, 3.0)},
        {"physical_machine_pose": PhysicalMachinePose.from_mapping({"X": 12.0})},
        {"motion_coordinate_snapshot": object()},
        {"stage_state": "Run"},
        {"homed_axes": frozenset({"X", "Y"})},
        {"last_jog_write_timestamp": 5.0},
    ):
        assert not observation.has_same_motion_facts_as(
            dataclasses.replace(observation, **changes)
        )


def test_stage_position_observation_keeps_queued_generations_and_emits_outside_lock() -> (
    None
):
    application = QApplication.instance() or QApplication([])
    controller = QtStageController()
    observed = []
    legacy_positions = []
    lock_owned_during_emission = []
    observation_signal = getattr(controller, "stage_position_observed", None)
    assert observation_signal is not None
    observation_signal.connect(
        lambda observation: observed.append(observation),
        Qt.ConnectionType.QueuedConnection,
    )
    observation_signal.connect(
        lambda _observation: lock_owned_during_emission.append(
            controller._state_lock._is_owned()
        ),
        Qt.ConnectionType.DirectConnection,
    )
    controller.stage_position_changed.connect(legacy_positions.append)
    controller._position_reporting_mode = "machine"
    controller._homed_axes = {"X", "Y", "Z"}
    first_position = (1.0, 2.0, 3.0, 4.0, 5.0)
    second_position = (6.0, 7.0, 8.0, 9.0, 10.0)

    try:
        controller._last_status_timestamp = 10.0
        controller._last_jog_write_timestamp = 4.0
        controller._update_cached_positions(
            stage_types._Status(
                state="Run",
                position=(11.0, 12.0, 13.0, 14.0, 15.0),
                synchronized_machine_position=(11.0, 12.0, 13.0, 14.0, 15.0),
                display_position=first_position,
            )
        )
        controller._last_status_timestamp = 20.0
        controller._last_jog_write_timestamp = 5.0
        controller._update_cached_positions(
            stage_types._Status(
                state="Idle",
                position=(21.0, 22.0, 23.0, 24.0, 25.0),
                synchronized_machine_position=(21.0, 22.0, 23.0, 24.0, 25.0),
                display_position=second_position,
            )
        )

        assert observed == []
        assert lock_owned_during_emission == [False, False]
        application.processEvents()

        assert legacy_positions == [first_position, second_position]
        assert [observation.position for observation in observed] == [
            first_position,
            second_position,
        ]
        assert [
            observation.physical_machine_pose.to_dict()["X"] for observation in observed
        ] == [
            11.0,
            21.0,
        ]
        assert [
            observation.motion_coordinate_snapshot.raw_machine_position[0]
            for observation in observed
        ] == [11.0, 21.0]
        assert [observation.stage_state for observation in observed] == ["Run", "Idle"]
        assert [observation.homed_axes for observation in observed] == [
            frozenset({"X", "Y", "Z"}),
            frozenset({"X", "Y", "Z"}),
        ]
        assert [observation.status_timestamp for observation in observed] == [
            10.0,
            20.0,
        ]
        assert [observation.last_jog_write_timestamp for observation in observed] == [
            4.0,
            5.0,
        ]
    finally:
        controller.shutdown()


def test_typed_observation_publishes_every_valid_status_generation() -> None:
    controller = QtStageController()
    typed_observations = []
    legacy_positions = []
    controller.stage_position_observed.connect(typed_observations.append)
    controller.stage_position_changed.connect(legacy_positions.append)
    controller._position_reporting_mode = "machine"
    controller._homed_axes = {"X", "Y", "Z"}
    position = (1.0, 2.0, 3.0, 4.0, 5.0)
    status = stage_types._Status(
        state="Idle",
        position=position,
        synchronized_machine_position=position,
        display_position=position,
    )

    try:
        controller._last_status_timestamp = 10.0
        controller._update_cached_positions(status)
        controller._last_status_timestamp = 11.0
        controller._update_cached_positions(status)

        assert legacy_positions == [position]
        assert [value.position for value in typed_observations] == [position, position]
        assert [value.status_timestamp for value in typed_observations] == [10.0, 11.0]
        assert [value.reset_reason for value in typed_observations] == [None, None]
    finally:
        controller.shutdown()


def test_cache_clear_observation_carries_explicit_connection_reset_reason() -> None:
    controller = QtStageController()
    typed_observations = []
    controller.stage_position_observed.connect(typed_observations.append)

    try:
        controller.clear_cached_controller_state()

        assert len(typed_observations) == 1
        assert typed_observations[0].position is None
        assert (
            typed_observations[0].reset_reason
            is stage_types.StageMotionResetReason.CONNECTION_CHANGED
        )
    finally:
        controller.shutdown()


def test_typed_observation_is_captured_before_legacy_signal_reentry() -> None:
    controller = QtStageController()
    typed_observations = []
    controller._position_reporting_mode = "machine"
    controller._homed_axes = {"X", "Y", "Z"}
    position = (1.0, 2.0, 3.0, 4.0, 5.0)

    def mutate_controller_caches(_position: object) -> None:
        controller._last_synchronized_machine_position = (
            91.0,
            92.0,
            93.0,
            94.0,
            95.0,
        )
        controller._last_stage_state = "Alarm"
        controller._homed_axes = {"A"}
        controller._last_status_timestamp = 99.0
        controller._last_jog_write_timestamp = 98.0

    controller.stage_position_changed.connect(
        mutate_controller_caches,
        Qt.ConnectionType.DirectConnection,
    )
    controller.stage_position_observed.connect(typed_observations.append)

    try:
        controller._last_status_timestamp = 10.0
        controller._last_jog_write_timestamp = 4.0
        controller._update_cached_positions(
            stage_types._Status(
                state="Run",
                position=(11.0, 12.0, 13.0, 14.0, 15.0),
                synchronized_machine_position=(11.0, 12.0, 13.0, 14.0, 15.0),
                display_position=position,
            )
        )

        assert len(typed_observations) == 1
        observation = typed_observations[0]
        assert observation.position == position
        assert observation.physical_machine_pose.to_dict()["X"] == 11.0
        assert observation.motion_coordinate_snapshot.raw_machine_position[0] == 11.0
        assert observation.stage_state == "Run"
        assert observation.homed_axes == frozenset({"X", "Y", "Z"})
        assert observation.status_timestamp == 10.0
        assert observation.last_jog_write_timestamp == 4.0
        assert observation.reset_reason is None
    finally:
        controller.shutdown()


class _StageController:
    def __init__(self) -> None:
        self.state = "idle"
        self.position = (1.0, 2.0, 3.0, 0.0, 0.0)
        self.last_status_time = 10.0
        self.last_jog_write_time = None
        self.homed = {"X", "Y", "Z"}
        self.machine_position = self.position
        self.mapper = StageAxisCalibrationMapper(
            calibrations=default_axis_calibrations(),
            position_reporting_mode="work",
            active_work_coordinate_system="G54",
            controller_coordinate_offsets={"G54": (0.0,) * 6},
            axis_index={
                axis: index for index, axis in enumerate(("X", "Y", "Z", "A", "B", "C"))
            },
        )

    def latest_stage_state(self) -> str:
        return self.state

    def axes_are_homed(self, axes: set[str]) -> bool:
        return axes.issubset(self.homed)

    def latest_stage_position(self) -> tuple[float, ...]:
        return self.position

    def latest_synchronized_machine_position(self) -> tuple[float, ...]:
        return self.machine_position

    def _axis_calibration_mapper(self) -> StageAxisCalibrationMapper:
        return self.mapper

    def last_status_timestamp(self) -> float:
        return self.last_status_time

    def last_jog_write_timestamp(self) -> float | None:
        return self.last_jog_write_time

    def homed_axes(self) -> set[str]:
        return set(self.homed)


class _Owner:
    STAGE_AXIS_NAMES = AXES
    B_POSITION_CHANGE_TOLERANCE_DEG = 0.1
    MANUAL_JOG_RECONCILE_SMOOTH_ALPHA = 0.35

    def __init__(self) -> None:
        self.stage_controller = _StageController()
        self._stage_position_panel = None
        self._stage_axis_fields = {}
        self._stage_unhomed_display_origins = {}
        self._stage_axis_raw_values = {}
        self._stage_axis_display_values = {}
        self._stage_axis_homed = set()
        self._stage_limit_axes = set()
        self._stage_axis_base_styles = {}
        self._stage_motion_axes = {"X"}
        self._stage_motion_blink_dimmed = False
        self._stage_motion_blink_timer = types.SimpleNamespace(isActive=lambda: False)
        self._stage_position_panel = types.SimpleNamespace(
            refresh_axis_styles=lambda _axes, _dimmed: self.calls.append(
                ("clear_motion", None)
            )
        )
        self._manual_jog_prediction = ManualJogPredictionState(
            ManualJogPredictionConfig(
                axis_names=AXES,
                ignore_idle_after_command_s=0.15,
                reconcile_smooth_threshold_mm=0.01,
                reconcile_smooth_alpha=0.35,
                status_settle_hold_s=0.2,
                default_stop_tail_s=0.05,
                stop_tail_min_s=0.0,
                stop_tail_max_s=0.2,
                stop_tail_learn_alpha=0.2,
            )
        )
        self._physical_machine_pose = PhysicalMachinePose.from_mapping({})
        self._stage_motion = types.SimpleNamespace(
            snapshot=lambda: types.SimpleNamespace(
                physical_machine_pose=self._physical_machine_pose,
                coordinate_active=False,
                coordinate_stage_position=None,
                presented_stage_xy=None,
            )
        )
        self._pending_alignment_preparation = None
        self._design_session = types.SimpleNamespace(
            document=object(),
            registration=types.SimpleNamespace(valid=True),
        )
        coordinate_snapshot = CoordinateSystemSnapshot(
            frames_loaded=True,
            records=(),
            document=None,
            registration=RegistrationWorkflowSnapshot(registration_valid=True),
        )
        self._coordinate_system_coordinator = types.SimpleNamespace(
            snapshot=lambda: coordinate_snapshot,
            observe_authority=lambda _observation: CoordinateTransition(
                coordinate_snapshot
            ),
        )
        self._current_design_stage_xy = (9.0, 9.0)
        self.contact_calibration_window = None
        self.calls: list[tuple[str, object]] = []

    def _coerce_position_tuple(self, value: object) -> tuple[float, ...] | None:
        if not isinstance(value, (tuple, list)):
            return None
        return tuple(float(item) for item in value)

    def _stage_xy_from_position(
        self, position: object | None
    ) -> tuple[float, float] | None:
        return position_update.stage_xy_from_position(position)

    def _position_with_stage_xy(
        self,
        stage_xy: tuple[float, float],
        *,
        base_position: object | None = None,
    ) -> tuple[float, ...]:
        return position_update.position_with_stage_xy(
            self,
            stage_xy,
            base_position=base_position,
        )

    def _can_display_design_position(self) -> bool:
        return True

    def _maybe_restore_persisted_design(self, position: tuple[float, ...]) -> None:
        self.calls.append(("restore", position))

    def _update_stage_position_display(self, position: object | None) -> None:
        self.calls.append(("display", position))

    def _update_coordinate_display(
        self,
        *,
        center_xy: tuple[float, float] | None = None,
        cursor_xy: tuple[float, float] | None = None,
    ) -> None:
        self.calls.append(("coordinate", center_xy))

    def _update_design_position(self, stage_xy: tuple[float, float] | None) -> None:
        self.calls.append(("design", stage_xy))

    def _invalidate_design_registration(self, message: str) -> None:
        self.calls.append(("invalidate", message))

    def _log_design_position_reconcile(
        self,
        predicted_stage_xy: tuple[float, float],
        actual_stage_xy: tuple[float, float],
    ) -> None:
        self.calls.append(("reconcile", (predicted_stage_xy, actual_stage_xy)))

    def _format_optional_point(self, point: tuple[float, float] | None) -> str:
        return "None" if point is None else f"{point[0]:.3f},{point[1]:.3f}"

    def _publish_stage_position_estimate(
        self,
        position: tuple[float, ...] | None,
    ) -> None:
        self.calls.append(("publish", position))


def test_invalid_stage_position_uses_display_only_owner_seam(monkeypatch) -> None:
    owner = _Owner()
    monkeypatch.setattr(
        position_update.stage_position_panel,
        "update_stage_position_display",
        lambda _owner, position: owner.calls.append(("display", position)),
    )

    position_update.on_stage_position_changed(owner, ["bad"])

    assert owner.calls == [("display", ["bad"])]


def test_position_update_reads_registration_projection_only_from_coordinator() -> None:
    source = inspect.getsource(position_update.on_stage_position_changed)

    assert "_design_session" not in source
    assert "_coordinate_system_coordinator.snapshot()" in source


def test_unhomed_fallback_clears_prediction_and_motion_axes(
    monkeypatch,
) -> None:
    owner = _Owner()
    owner.stage_controller.homed = set()
    owner._manual_jog_prediction.stage_position = (9.0, 9.0, 3.0)
    owner._manual_jog_prediction.stage_xy = (9.0, 9.0)
    monkeypatch.setattr(
        position_update.design_workspace,
        "maybe_restore_persisted_design",
        lambda _owner, position: owner.calls.append(("restore", position)),
    )
    monkeypatch.setattr(
        position_update.stage_position_panel,
        "update_stage_position_display",
        lambda _owner, position: owner.calls.append(("display", position)),
    )
    monkeypatch.setattr(
        position_update.coordinate_flow,
        "observe_coordinate_authority",
        lambda _owner, pose: owner.calls.append(("authority", pose)),
    )

    position_update.on_stage_position_changed(owner, (1.0, 2.0, 3.0))

    assert owner._manual_jog_prediction.stage_position is None
    assert owner._manual_jog_prediction.stage_xy is None
    assert owner.calls == [
        ("restore", (1.0, 2.0, 3.0)),
        ("display", (1.0, 2.0, 3.0)),
        ("authority", owner._physical_machine_pose),
        ("coordinate", None),
        ("design", (1.0, 2.0)),
        ("clear_motion", None),
    ]


def test_position_publication_observes_authority_without_main_policy_helper(
    monkeypatch,
) -> None:
    owner = _Owner()
    observations: list[object] = []
    monkeypatch.setattr(
        position_update.stage_position_panel,
        "update_stage_position_display",
        lambda _owner, _position: None,
    )
    monkeypatch.setattr(
        position_update.coordinate_flow,
        "observe_coordinate_authority",
        lambda _owner, pose: observations.append(pose),
    )

    position_update.publish_stage_position_estimate(owner, (1.0, 2.0, 3.0))

    assert observations == [owner._physical_machine_pose]


def test_deferred_presentations_keep_complete_queued_observation_generations(
    monkeypatch,
) -> None:
    from probe_station_gui.application.stage_design_position import (
        _MainStageDesignPositionMixin,
    )
    from probe_station_gui.application.stage_motion_types import (
        StageMotionPresentation,
    )

    owner = _Owner()
    cache_reads: list[str] = []

    def unexpected_cache_read(name: str):
        def read(*_args: object) -> object:
            cache_reads.append(name)
            raise RuntimeError(f"unexpected mutable cache read: {name}")

        return read

    owner.stage_controller.latest_stage_state = unexpected_cache_read("state")
    owner.stage_controller.axes_are_homed = unexpected_cache_read("homed")
    owner.stage_controller.last_jog_write_timestamp = unexpected_cache_read("jog")
    owner.stage_controller.latest_motion_coordinate_snapshot = unexpected_cache_read(
        "motion snapshot"
    )
    owner.stage_controller.homed_axes = unexpected_cache_read("homed axes")
    pose_a = PhysicalMachinePose.from_mapping({"X": 11.0})
    pose_b = PhysicalMachinePose.from_mapping({"X": 22.0})
    owner._physical_machine_pose = pose_b
    observations: list[tuple[object, object, object]] = []
    monkeypatch.setattr(
        position_update.design_workspace,
        "maybe_restore_persisted_design",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        position_update.stage_position_panel,
        "update_stage_position_display",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        position_update.coordinate_flow,
        "observe_coordinate_authority",
        lambda _owner, pose, **facts: observations.append(
            (pose, facts["machine_snapshot"], facts["homed_axes"])
        ),
    )

    def presentation(
        position: tuple[float, ...],
        pose: PhysicalMachinePose,
        motion_snapshot: object,
        homed_axes: frozenset[str],
        jog_timestamp: float,
    ) -> StageMotionPresentation:
        return StageMotionPresentation(
            reported_position=position,
            presented_position=position,
            raw_stage_xy=(position[0], position[1]),
            presented_stage_xy=(position[0], position[1]),
            physical_machine_pose=pose,
            contact_calibration_position=(position[0], position[1], position[2]),
            b_position=None,
            active_axes=frozenset(),
            unhomed_fallback=False,
            clear_motion_axes=False,
            motion_coordinate_snapshot=motion_snapshot,
            stage_state="Run",
            homed_axes=homed_axes,
            status_timestamp=jog_timestamp + 1.0,
            last_jog_write_timestamp=jog_timestamp,
        )

    snapshots = (object(), object())
    homed = (frozenset({"X", "Y"}), frozenset({"X", "Y", "Z"}))
    queued = (
        presentation((1.0, 2.0, 3.0), pose_a, snapshots[0], homed[0], 1.0),
        presentation((4.0, 5.0, 6.0), pose_b, snapshots[1], homed[1], 2.0),
    )
    for payload in queued:
        _MainStageDesignPositionMixin._apply_stage_motion_presentation(owner, payload)

    assert cache_reads == []
    assert observations == [
        (pose_a, snapshots[0], homed[0]),
        (pose_b, snapshots[1], homed[1]),
    ]
