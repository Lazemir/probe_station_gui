from __future__ import annotations

import pytest

from probe_station_gui.settings.axis_calibration_config import (
    AxisCalibrationSettings,
    default_axis_calibrations,
)
from probe_station_gui.stage.axis_calibration import StageAxisCalibrationMapper
from probe_station_gui.stage.machine_coordinates import (
    MachineCoordinateSnapshot,
    MachineCoordinateSnapshotUnavailable,
)
from probe_station_gui.stage.types import _Status


AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2, "A": 3, "B": 4, "C": 5}


def _mapper() -> StageAxisCalibrationMapper:
    calibrations = default_axis_calibrations()
    for axis, physical_points in {
        "X": [0.0, 12.0, 30.0],
        "Y": [0.0, 8.0, 25.0],
        "B": [0.0, 9.0, 24.0],
    }.items():
        calibrations[axis] = AxisCalibrationSettings(
            enabled=True,
            calibration_file=f"{axis}.npz",
            controller_points=[0.0, 10.0, 20.0],
            physical_points=physical_points,
        )
    return StageAxisCalibrationMapper(
        calibrations=calibrations,
        position_reporting_mode="work",
        active_work_coordinate_system="G54",
        controller_coordinate_offsets={"G54": (4.0, 5.0, 0.0, 0.0, 6.0, 0.0)},
        axis_index=AXIS_INDEX,
    )


def _status() -> _Status:
    return _Status(
        state="Idle",
        synchronized_machine_position=(15.0, 16.0, 0.0, 0.0, 17.0, 0.0),
        display_position=(11.0, 11.0, 0.0, 0.0, 11.0, 0.0),
        work_position=(11.0, 11.0, 0.0, 0.0, 11.0, 0.0),
        work_offset=(4.0, 5.0, 0.0, 0.0, 6.0, 0.0),
        coordinate_system="G54",
    )


def test_snapshot_uses_one_status_wco_and_universal_nonlinear_mapping() -> None:
    snapshot = MachineCoordinateSnapshot.from_status(_status(), _mapper(), AXIS_INDEX)

    assert snapshot.physical_machine_pose.require("X") == pytest.approx(21.0)
    assert snapshot.physical_machine_pose.require("Y") == pytest.approx(18.2)
    assert snapshot.physical_machine_pose.require("B") == pytest.approx(19.5)

    assert snapshot.configured_controller_to_physical_machine("X", 11.0) == pytest.approx(21.0)
    assert snapshot.configured_controller_to_physical_machine("Y", 11.0) == pytest.approx(18.2)
    assert snapshot.configured_controller_to_physical_machine("B", 11.0) == pytest.approx(19.5)

    assert snapshot.physical_machine_to_configured_controller("X", 21.0) == pytest.approx(11.0)
    assert snapshot.physical_machine_to_configured_controller("Y", 18.2) == pytest.approx(11.0)
    assert snapshot.physical_machine_to_configured_controller("B", 19.5) == pytest.approx(11.0)


@pytest.mark.parametrize(
    "status",
    [
        _Status(
            state="Idle",
            synchronized_machine_position=None,
            work_position=(1.0, 2.0, 3.0),
            work_offset=(4.0, 5.0, 6.0),
            coordinate_system="G54",
        ),
        _Status(
            state="Idle",
            synchronized_machine_position=(5.0, 7.0, 9.0),
            work_position=(1.0, 2.0, 3.0),
            work_offset=None,
            coordinate_system="G54",
        ),
        _Status(
            state="Run",
            synchronized_machine_position=(5.0, 7.0, 9.0),
            work_position=(1.0, 2.0, 3.0),
            work_offset=(4.0, 5.0, 6.0),
            coordinate_system="G54",
        ),
    ],
)
def test_snapshot_fails_closed_without_stationary_same_generation_provenance(
    status: _Status,
) -> None:
    with pytest.raises(MachineCoordinateSnapshotUnavailable):
        MachineCoordinateSnapshot.from_status(status, _mapper(), AXIS_INDEX)
