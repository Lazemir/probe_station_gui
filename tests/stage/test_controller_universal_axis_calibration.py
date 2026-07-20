from types import SimpleNamespace

import pytest

try:
    from .controller_test_support import AxisCalibrationSettings, StageController
except ImportError:
    from controller_test_support import AxisCalibrationSettings, StageController

from probe_station_gui.settings.axis_calibration_config import default_axis_calibrations
from probe_station_gui.stage.errors import StageControllerError


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
