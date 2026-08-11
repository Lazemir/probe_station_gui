from types import SimpleNamespace

import pytest

try:
    from .controller_test_support import AxisCalibrationSettings, StageController
except ImportError:
    from controller_test_support import AxisCalibrationSettings, StageController

from probe_station_gui.settings.axis_calibration_config import default_axis_calibrations
from probe_station_gui.stage.errors import StageControllerError
from probe_station_gui.stage.machine_coordinates import MachineCoordinateSnapshot
from probe_station_gui.stage.types import _Status


def _curve(axis: str) -> dict[str, AxisCalibrationSettings]:
    settings = default_axis_calibrations()
    settings[axis] = AxisCalibrationSettings(
        enabled=True,
        calibration_file=f"{axis}.npz",
        controller_points=[0.0, 1.0, 3.0],
        physical_points=[0.0, 2.0, 5.0],
    )
    return settings


@pytest.mark.parametrize("axis", ["X", "Y", "Z", "A", "B", "C"])
def test_controller_maps_display_and_targets_for_every_axis(axis: str) -> None:
    controller = StageController()
    try:
        controller._position_reporting_mode = "machine"
        controller.apply_axis_calibrations(_curve(axis))

        assert controller.calibrated_axis_display_value(axis, 2.0) == pytest.approx(3.5)
        assert controller.calibrated_axis_raw_value(axis, 3.5) == pytest.approx(2.0)
        assert controller.calibrated_axis_raw_target_value(axis, 3.5) == pytest.approx(2.0)
    finally:
        controller.shutdown()


def test_target_outside_physical_domain_is_unavailable() -> None:
    controller = StageController()
    try:
        controller._position_reporting_mode = "machine"
        controller.apply_axis_calibrations(_curve("X"))

        assert controller.calibrated_axis_raw_target_value("X", 5.001) is None
    finally:
        controller.shutdown()


def test_raw_target_outside_controller_domain_is_rejected() -> None:
    controller = StageController()
    try:
        controller._position_reporting_mode = "machine"
        controller.apply_axis_calibrations(_curve("X"))

        with pytest.raises(StageControllerError, match="cannot be represented"):
            controller.validate_calibrated_axis_raw_target("X", 3.001)
    finally:
        controller.shutdown()


def test_work_coordinate_mapping_uses_cached_offset() -> None:
    controller = StageController()
    try:
        settings = default_axis_calibrations()
        settings["X"] = AxisCalibrationSettings(
            enabled=True,
            controller_points=[0.0, 10.0, 20.0],
            physical_points=[0.0, 12.0, 30.0],
        )
        controller.apply_axis_calibrations(settings)
        controller._position_reporting_mode = "work"
        controller._active_work_coordinate_system = "G54"
        controller._controller_coordinate_offsets["G54"] = (10.0, 0.0, 0.0, 0.0, 0.0, 0.0)

        assert controller.calibrated_axis_display_value("X", 5.0) == pytest.approx(9.0)
        assert controller.calibrated_axis_raw_target_value("X", 9.0) == pytest.approx(5.0)
    finally:
        controller.shutdown()


def test_cached_machine_position_drives_preview_without_hardware_read() -> None:
    controller = StageController()
    try:
        controller._position_reporting_mode = "machine"
        controller.apply_axis_calibrations(_curve("Z"))
        controller._last_machine_position = (0.0, 0.0, 2.0, 0.0, 0.0, 0.0)
        controller._query_status = lambda *_args, **_kwargs: pytest.fail("hardware read")

        assert controller.latest_machine_position() == (0.0, 0.0, 2.0, 0.0, 0.0, 0.0)
        assert controller.axis_calibration_preview_position("Z") == pytest.approx((2.0, 3.5))
    finally:
        controller.shutdown()


def test_work_mode_derives_machine_position_from_cached_work_position_and_wco() -> None:
    controller = StageController()
    try:
        controller._position_reporting_mode = "work"
        controller._last_stage_position = (-9.047, -5.369, 5.441, 2.01, -3.505)
        controller._active_work_coordinate_system = "G54"
        controller._controller_coordinate_offsets["G54"] = (
            32.0,
            32.0,
            0.0,
            0.0,
            0.0,
        )

        assert controller.latest_machine_position() == pytest.approx(
            (22.953, 26.631, 5.441, 2.01, -3.505)
        )
    finally:
        controller.shutdown()


def test_work_mode_ignores_stale_machine_cache_for_latest_machine_position() -> None:
    controller = StageController()
    try:
        controller._position_reporting_mode = "work"
        controller._last_machine_position = (999.0, 999.0, 999.0)
        controller._last_stage_position = (1.0, 2.0, 5.441)
        controller._active_work_coordinate_system = "G54"
        controller._controller_coordinate_offsets["G54"] = (10.0, 20.0, 0.0)

        assert controller.latest_machine_position() == pytest.approx((11.0, 22.0, 5.441))
    finally:
        controller.shutdown()


def test_work_mode_without_active_wco_has_no_latest_machine_position() -> None:
    controller = StageController()
    try:
        controller._position_reporting_mode = "work"
        controller._last_machine_position = (999.0, 999.0, 999.0)
        controller._last_stage_position = (1.0, 2.0, 5.441)
        controller._active_work_coordinate_system = "G54"

        assert controller.latest_machine_position() is None
    finally:
        controller.shutdown()


def test_work_mode_with_incomplete_coordinate_components_has_no_latest_machine_position() -> None:
    controller = StageController()
    try:
        controller._position_reporting_mode = "work"
        controller._last_stage_position = (1.0, 2.0)
        controller._active_work_coordinate_system = "G54"
        controller._controller_coordinate_offsets["G54"] = (10.0, 20.0, 0.0)

        assert controller.latest_machine_position() is None
    finally:
        controller.shutdown()


def test_a_needle_lowering_remains_negative_physical_axis_coordinate() -> None:
    controller = StageController()
    try:
        controller._position_reporting_mode = "machine"
        settings = default_axis_calibrations()
        settings["A"] = AxisCalibrationSettings(
            enabled=True,
            controller_points=[-3.0, -1.0, 0.0],
            physical_points=[-5.0, -2.0, 0.0],
        )
        controller.apply_axis_calibrations(settings)

        assert controller.axis_a_lowering_for_gcode_coordinate(-1.0) == pytest.approx(2.0)
        assert controller.axis_a_gcode_coordinate_for_lowering(2.0) == pytest.approx(-1.0)
    finally:
        controller.shutdown()


def test_preview_reports_none_when_cached_coordinate_is_outside_curve() -> None:
    controller = StageController()
    try:
        controller.apply_axis_calibrations(_curve("X"))
        controller._last_machine_position = (4.0, 0.0, 0.0, 0.0, 0.0, 0.0)

        assert controller.axis_calibration_preview_position("X") is None
    finally:
        controller.shutdown()


def test_synchronized_machine_snapshot_never_reuses_previous_status_generation() -> None:
    controller = StageController()
    try:
        controller._update_cached_positions(
            _Status(
                state="Idle",
                display_position=(5.0, 2.0, 3.0),
                work_position=(5.0, 2.0, 3.0),
                work_offset=(10.0, 20.0, 0.0),
                synchronized_machine_position=(15.0, 22.0, 3.0),
            )
        )
        assert controller.latest_synchronized_machine_position() == (
            15.0,
            22.0,
            3.0,
        )

        controller._update_cached_positions(
            _Status(
                state="Idle",
                display_position=(6.0, 2.0, 3.0),
                work_position=(6.0, 2.0, 3.0),
                work_offset=(10.0, 20.0, 0.0),
            )
        )

        assert controller.latest_synchronized_machine_position() is None
    finally:
        controller.shutdown()


@pytest.mark.parametrize(
    ("first_snapshot", "second_snapshot"),
    [
        ((15.0, 22.0, 3.0), None),
        (None, (15.0, 22.0, 3.0)),
        ((15.0, 22.0, 3.0), (16.0, 22.0, 3.0)),
    ],
)
def test_synchronized_snapshot_change_refreshes_unchanged_legacy_position(
    first_snapshot: tuple[float, ...] | None,
    second_snapshot: tuple[float, ...] | None,
) -> None:
    controller = StageController()
    positions: list[object] = []
    controller.stage_position_changed = SimpleNamespace(emit=positions.append)
    display_position = (5.0, 2.0, 3.0)
    try:
        controller._update_cached_positions(
            _Status(
                state="Idle",
                display_position=display_position,
                work_position=display_position,
                synchronized_machine_position=first_snapshot,
            )
        )
        controller._update_cached_positions(
            _Status(
                state="Idle",
                display_position=display_position,
                work_position=display_position,
                synchronized_machine_position=second_snapshot,
            )
        )

        assert positions == [display_position, display_position]
        assert controller.latest_synchronized_machine_position() == second_snapshot
    finally:
        controller.shutdown()


def test_physical_machine_coordinates_use_raw_mpos_not_work_display_or_wco() -> None:
    controller = StageController()
    try:
        settings = default_axis_calibrations()
        settings["Z"] = AxisCalibrationSettings(
            enabled=True,
            controller_points=[0.0, 2.0, 4.0],
            physical_points=[0.0, 3.0, 10.0],
        )
        controller.apply_axis_calibrations(settings)
        controller._position_reporting_mode = "work"
        controller._active_work_coordinate_system = "G54"
        controller._controller_coordinate_offsets["G54"] = (10.0, 20.0, 30.0)
        status = _Status(
            state="Idle",
            position=(11.0, 22.0, 2.0),
            display_position=(1.0, 2.0, -28.0),
            work_position=(1.0, 2.0, -28.0),
            work_offset=(10.0, 20.0, 30.0),
            coordinate_system="G54",
        )

        assert controller._physical_machine_coordinates_from_status(
            status,
            axes=("Z",),
        ) == {"Z": pytest.approx(3.0)}
    finally:
        controller.shutdown()


def test_physical_machine_coordinates_reject_missing_or_out_of_domain_mpos() -> None:
    controller = StageController()
    try:
        controller.apply_axis_calibrations(_curve("Z"))
        missing = _Status(state="Idle", position=(1.0, 2.0))
        outside = _Status(state="Idle", position=(1.0, 2.0, 4.0))

        with pytest.raises(StageControllerError, match="complete.*Z"):
            controller._physical_machine_coordinates_from_status(
                missing,
                axes=("Z",),
            )
        with pytest.raises(StageControllerError, match="outside.*calibration"):
            controller._physical_machine_coordinates_from_status(
                outside,
                axes=("Z",),
            )
    finally:
        controller.shutdown()


def test_cached_machine_coordinate_snapshot_uses_same_status_generation() -> None:
    controller = StageController()
    try:
        controller._position_reporting_mode = "work"
        controller._active_work_coordinate_system = "G54"
        controller._controller_coordinate_offsets["G54"] = (
            4.0,
            5.0,
            0.0,
            0.0,
            6.0,
        )
        controller._update_cached_positions(
            _Status(
                state="Idle",
                synchronized_machine_position=(15.0, 16.0, 0.0, 0.0, 17.0),
                display_position=(11.0, 11.0, 0.0, 0.0, 11.0),
                work_position=(11.0, 11.0, 0.0, 0.0, 11.0),
                work_offset=(4.0, 5.0, 0.0, 0.0, 6.0),
                coordinate_system="G54",
            )
        )

        snapshot = controller.latest_machine_coordinate_snapshot()
        assert isinstance(snapshot, MachineCoordinateSnapshot)
        assert snapshot.physical_machine_pose.require("X") == 15.0

        controller._update_cached_positions(
            _Status(
                state="Idle",
                synchronized_machine_position=None,
                display_position=(12.0, 11.0, 0.0, 0.0, 11.0),
                work_position=(12.0, 11.0, 0.0, 0.0, 11.0),
                work_offset=(4.0, 5.0, 0.0, 0.0, 6.0),
                coordinate_system="G54",
            )
        )
        assert controller.latest_machine_coordinate_snapshot() is None
    finally:
        controller.shutdown()


def test_machine_display_limits_ignore_active_work_origin() -> None:
    controller = StageController()
    try:
        settings = default_axis_calibrations()
        settings["X"] = AxisCalibrationSettings(
            enabled=True,
            controller_points=[0.0, 10.0, 20.0],
            physical_points=[0.0, 12.0, 30.0],
        )
        controller.apply_axis_calibrations(settings)
        controller._position_reporting_mode = "work"
        controller._active_work_coordinate_system = "G54"
        controller._controller_coordinate_offsets["G54"] = (
            10.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
        )
        controller._axis_limits["X"] = (0.0, 20.0)

        assert controller.axis_display_limits("X") == pytest.approx((-12.0, 18.0))
        assert controller.axis_machine_display_limits("X") == pytest.approx(
            (0.0, 30.0)
        )
    finally:
        controller.shutdown()


def test_replacing_calibration_remaps_cached_machine_snapshot_without_new_status() -> None:
    controller = StageController()
    try:
        controller._position_reporting_mode = "machine"
        controller._update_cached_positions(
            _Status(
                state="Idle",
                synchronized_machine_position=(1.0, 2.0, 3.0),
                display_position=(1.0, 2.0, 3.0),
            )
        )
        before = controller.latest_machine_coordinate_snapshot()
        assert before is not None
        assert before.physical_machine_pose.require("X") == pytest.approx(1.0)

        controller.apply_axis_calibrations(_curve("X"))

        remapped = controller.latest_machine_coordinate_snapshot()
        assert remapped is not None
        assert remapped.raw_machine_position == before.raw_machine_position
        assert remapped.physical_machine_pose.require("X") == pytest.approx(2.0)
    finally:
        controller.shutdown()


def test_active_jog_keeps_separate_motion_snapshot_for_coordinate_projection() -> None:
    controller = StageController()
    try:
        controller._position_reporting_mode = "machine"
        controller._update_cached_positions(
            _Status(
                state="Jog",
                synchronized_machine_position=(1.0, 2.0, 3.0, 4.0, 5.0),
                display_position=(1.0, 2.0, 3.0, 4.0, 5.0),
            )
        )

        assert controller.latest_machine_coordinate_snapshot() is None
        motion_snapshot = controller.latest_motion_coordinate_snapshot()
        assert isinstance(motion_snapshot, MachineCoordinateSnapshot)
        assert motion_snapshot.physical_machine_pose.require("B") == pytest.approx(5.0)
    finally:
        controller.shutdown()


def test_machine_coordinate_snapshot_request_runs_query_only_in_background_target() -> None:
    controller = StageController()
    started: dict[str, object] = {}
    emitted: list[tuple[object, ...]] = []
    try:
        controller.machine_coordinate_snapshot_finished = SimpleNamespace(
            emit=lambda *args: emitted.append(args)
        )
        controller._query_current_stage_position_status = lambda: _Status(
            state="Idle",
            synchronized_machine_position=(1.0, 2.0, 3.0, 4.0, 5.0),
            display_position=(1.0, 2.0, 3.0, 4.0, 5.0),
            work_position=(1.0, 2.0, 3.0, 4.0, 5.0),
            work_offset=(0.0, 0.0, 0.0, 0.0, 0.0),
            coordinate_system="G54",
        )

        def capture_start(**kwargs):
            started.update(kwargs)
            return True

        controller._start_background_task = capture_start

        assert controller.request_machine_coordinate_snapshot(
            "registration-1",
            axes=("X", "Y", "B"),
        )
        assert emitted == []

        started["target"](*started["args"])
        request_id, success, snapshot, message = emitted.pop()
        assert request_id == "registration-1"
        assert success is True
        assert snapshot.physical_machine_pose.to_dict() == {
            "X": 1.0,
            "Y": 2.0,
            "Z": 3.0,
            "A": 4.0,
            "B": 5.0,
        }
        assert message == ""
    finally:
        controller.shutdown()


def test_machine_coordinate_snapshot_worker_reports_failure_without_cached_fallback() -> None:
    controller = StageController()
    emitted: list[tuple[object, ...]] = []
    try:
        controller.machine_coordinate_snapshot_finished = SimpleNamespace(
            emit=lambda *args: emitted.append(args)
        )
        controller._last_machine_coordinate_snapshot = object()
        controller._query_current_stage_position_status = lambda: _Status(
            state="Idle",
            synchronized_machine_position=None,
            display_position=(1.0, 2.0, 3.0),
            work_position=(1.0, 2.0, 3.0),
            work_offset=None,
            coordinate_system="G54",
        )

        controller._run_machine_coordinate_snapshot_request(
            "registration-2",
            ("X", "Y", "B"),
        )

        assert emitted == [
            (
                "registration-2",
                False,
                None,
                "Synchronized Machine coordinates are unavailable.",
            )
        ]
    finally:
        controller.shutdown()


def test_continuous_jog_is_clipped_to_calibration_controller_domain() -> None:
    controller = StageController()
    try:
        controller._position_reporting_mode = "machine"
        controller.apply_axis_calibrations(_curve("X"))
        controller._axis_limits = {"X": (-100.0, 100.0)}
        controller._homed_axes = {"X"}
        controller._last_stage_position = (2.5, 0.0, 0.0, 0.0, 0.0, 0.0)
        controller.status_message = SimpleNamespace(emit=lambda _message: None)

        constrained = controller.constrain_jog_distances((("X", 1000.0),))

        assert constrained == (("X", pytest.approx(0.5)),)
    finally:
        controller.shutdown()
