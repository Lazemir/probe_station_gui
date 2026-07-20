from types import SimpleNamespace

import pytest

from probe_station_gui.settings.axis_calibration_config import (
    AxisCalibrationSettings,
    default_axis_calibrations,
)
from probe_station_gui.stage.axis_calibration import (
    CalibrationCoordinateUnavailable,
    StageAxisCalibrationMapper,
)
from probe_station_gui.stage.axis_mapping import CalibrationOutOfDomain


AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2, "A": 3, "B": 4, "C": 5}


def _calibrations(axis: str = "X") -> dict[str, AxisCalibrationSettings]:
    values = default_axis_calibrations()
    values[axis] = AxisCalibrationSettings(
        enabled=True,
        calibration_file=f"{axis}.npz",
        controller_points=[0.0, 10.0, 20.0],
        physical_points=[0.0, 12.0, 30.0],
    )
    return values


def _mapper(
    *,
    axis: str = "X",
    mode: str = "machine",
    active_system: str | None = None,
    offsets: dict[str, tuple[float, ...]] | None = None,
) -> StageAxisCalibrationMapper:
    return StageAxisCalibrationMapper(
        calibrations=_calibrations(axis),
        position_reporting_mode=mode,
        active_work_coordinate_system=active_system,
        controller_coordinate_offsets=offsets or {},
        axis_index=AXIS_INDEX,
    )


def test_machine_coordinates_use_curve_directly() -> None:
    mapper = _mapper()

    assert mapper.controller_to_physical("X", 15.0) == pytest.approx(21.0)
    assert mapper.physical_to_controller("X", 21.0) == pytest.approx(15.0)
    assert mapper.controller_domain("X") == (0.0, 20.0)
    assert mapper.physical_domain("X") == (0.0, 30.0)


def test_disabled_axis_uses_identity_without_a_domain() -> None:
    mapper = _mapper()

    assert mapper.controller_to_physical("Y", 123.0) == 123.0
    assert mapper.physical_to_controller("Y", -456.0) == -456.0
    assert mapper.controller_domain("Y") is None
    assert mapper.physical_domain("Y") is None


def test_work_coordinates_map_machine_position_relative_to_mapped_origin() -> None:
    mapper = _mapper(
        mode="work",
        active_system="G54",
        offsets={"G54": (10.0, 0.0, 0.0, 0.0, 0.0, 0.0)},
    )

    # Raw work 5 -> raw machine 15. f(15) - f(10) = 21 - 12.
    assert mapper.configured_controller_to_physical("X", 5.0) == pytest.approx(9.0)
    assert mapper.physical_to_configured_controller("X", 9.0) == pytest.approx(5.0)
    assert mapper.controller_domain_for_configured_mode("X") == (-10.0, 10.0)
    assert mapper.physical_domain_for_configured_mode("X") == (-12.0, 18.0)


def test_status_work_offset_takes_precedence_over_cached_coordinate_system() -> None:
    mapper = _mapper(
        mode="work",
        active_system="G54",
        offsets={"G54": (10.0, 0.0, 0.0, 0.0, 0.0, 0.0)},
    )
    status = SimpleNamespace(
        coordinate_system="G55",
        work_offset=(5.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    )

    # f(10) - f(5) = 12 - 6.
    assert mapper.configured_controller_to_physical("X", 5.0, status) == pytest.approx(6.0)


def test_enabled_calibration_requires_work_offset() -> None:
    mapper = _mapper(mode="work")

    with pytest.raises(CalibrationCoordinateUnavailable, match="work offset"):
        mapper.configured_controller_to_physical("X", 1.0)
    with pytest.raises(CalibrationCoordinateUnavailable, match="work offset"):
        mapper.physical_to_configured_controller("X", 1.0)


def test_disabled_axis_remains_identity_without_work_offset() -> None:
    mapper = _mapper(mode="work")

    assert mapper.configured_controller_to_physical("Y", 1.0) == 1.0
    assert mapper.physical_to_configured_controller("Y", 1.0) == 1.0


def test_work_origin_itself_must_be_inside_controller_domain() -> None:
    mapper = _mapper(
        mode="work",
        active_system="G54",
        offsets={"G54": (30.0, 0.0, 0.0, 0.0, 0.0, 0.0)},
    )

    with pytest.raises(CalibrationOutOfDomain):
        mapper.configured_controller_to_physical("X", -15.0)


def test_position_and_axis_value_follow_configured_mode() -> None:
    status = SimpleNamespace(
        position=(1.0, 2.0, 3.0),
        work_position=(4.0, 5.0, 6.0),
    )

    assert _mapper(mode="machine").position_for_configured_mode(status) == (1.0, 2.0, 3.0)
    assert _mapper(mode="work").axis_value_for_configured_mode(status, "Y") == 5.0


def test_raw_software_limits_are_translated_to_work_coordinates() -> None:
    mapper = _mapper(
        mode="work",
        active_system="G54",
        offsets={"G54": (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)},
    )

    assert mapper.axis_limits_for_configured_mode("Y", (0.0, 10.0), None) == (-2.0, 8.0)


def test_exact_target_outside_physical_domain_is_rejected() -> None:
    mapper = _mapper()

    with pytest.raises(CalibrationOutOfDomain):
        mapper.physical_to_configured_controller("X", 31.0)

