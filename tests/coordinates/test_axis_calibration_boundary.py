import pytest

from probe_station_gui.coordinates import BFrameTransform, PhysicalMachinePose
from probe_station_gui.stage.axis_calibration import StageAxisCalibrationMapper
from probe_station_gui.stage.axis_mapping import AxisCalibrationCurve


def test_frame_math_is_applied_after_universal_controller_to_physical_mapping() -> None:
    mapper = StageAxisCalibrationMapper(
        calibrations={
            "X": AxisCalibrationCurve((0.0, 10.0), (0.0, 20.0)),
            "Y": AxisCalibrationCurve((0.0, 10.0), (0.0, 10.0)),
        },
        position_reporting_mode="machine",
        active_work_coordinate_system=None,
        controller_coordinate_offsets={},
        axis_index={"X": 0, "Y": 1},
    )
    machine = PhysicalMachinePose.from_mapping(
        {
            "X": mapper.controller_to_physical("X", 5.0),
            "Y": mapper.controller_to_physical("Y", 4.0),
            "B": 0.0,
        }
    )
    transform = BFrameTransform.identity()

    assert transform.machine_xy_to_frame(
        (machine.require("X"), machine.require("Y")),
        machine_b_deg=machine.require("B"),
        pivot_machine_xy=(0.0, 0.0),
    ) == pytest.approx((10.0, 4.0))
    assert mapper.physical_to_controller("X", 10.0) == pytest.approx(5.0)
