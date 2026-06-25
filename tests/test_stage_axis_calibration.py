from types import SimpleNamespace

import pytest

from probe_station_gui.stage_axis_calibration import StageAxisCalibrationMapper
from probe_station_gui.stage_axis_mapping import (
    axis_a_gcode_coordinate_for_lowering,
    axis_a_lowering_for_gcode_coordinate,
    axis_z_display_for_gcode_coordinate,
)


AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2, "A": 3, "B": 4, "C": 5}


def _axis_a_calibration() -> dict[str, float | str]:
    return {
        "model": "cosine_displacement",
        "steps_per_mm": 2500.0,
        "min": 0.0,
        "max": 6.0,
        "offset": -0.00013272701600556085,
        "amplitude": -4.29496757977153,
        "angular_frequency": 0.24349261926759336,
        "phase": 0.8994441869661569,
    }


def _axis_z_calibration() -> dict[str, float | str | tuple[float, ...]]:
    return {
        "model": "quintic_polynomial",
        "steps_per_mm": 6335.0,
        "min": 0.02,
        "max": 23.4,
        "coefficients": (
            -1.1689194871855767e-06,
            6.252947738309964e-05,
            -0.0006221022578588869,
            0.015449946058775076,
            0.5416754463041403,
            0.00910614542389841,
        ),
    }


def _mapper(
    *,
    mode: str = "machine",
    active_system: str | None = None,
    offsets: dict[str, tuple[float, ...]] | None = None,
) -> StageAxisCalibrationMapper:
    return StageAxisCalibrationMapper(
        axis_a_calibration=_axis_a_calibration(),
        axis_z_calibration=_axis_z_calibration(),
        position_reporting_mode=mode,
        active_work_coordinate_system=active_system,
        controller_coordinate_offsets=offsets or {},
        axis_index=AXIS_INDEX,
    )


def test_axis_work_offset_uses_status_before_cached_coordinate_system() -> None:
    mapper = _mapper(
        mode="work",
        active_system="G54",
        offsets={"G54": (1.0, 2.0, 3.0, 4.0), "G55": (5.0, 6.0, 7.0, 8.0)},
    )
    status = SimpleNamespace(coordinate_system="G55", work_offset=(9.0, 8.0, 7.0, 6.0))

    assert mapper.axis_work_offset_for_configured_mode("A", status) == 6.0


def test_axis_a_configured_coordinate_applies_active_work_offset() -> None:
    mapper = _mapper(mode="work", active_system="G54", offsets={"G54": (0, 0, 0, 2)})
    lowering = 0.2
    machine_a = axis_a_gcode_coordinate_for_lowering(_axis_a_calibration(), lowering)
    configured_a = machine_a - 2.0

    assert mapper.axis_a_lowering_for_configured_coordinate(configured_a) == pytest.approx(
        lowering,
    )
    assert mapper.axis_a_configured_coordinate_for_lowering(lowering) == pytest.approx(
        configured_a,
    )


def test_axis_a_lowering_step_uses_configured_coordinate_basis() -> None:
    mapper = _mapper(mode="work", active_system="G54", offsets={"G54": (0, 0, 0, 2)})
    current_lowering = 0.4
    current_machine_a = axis_a_gcode_coordinate_for_lowering(
        _axis_a_calibration(),
        current_lowering,
    )
    current_configured_a = current_machine_a - 2.0
    target_machine_a = axis_a_gcode_coordinate_for_lowering(
        _axis_a_calibration(),
        current_lowering - 0.2,
    )

    assert mapper.axis_a_gcode_coordinate_for_lowering_step(
        current_configured_a,
        0.2,
    ) == pytest.approx(target_machine_a - 2.0)


def test_axis_z_display_delegates_to_calibration_model() -> None:
    calibration = _axis_z_calibration()
    mapper = StageAxisCalibrationMapper(
        axis_a_calibration=None,
        axis_z_calibration=calibration,
        position_reporting_mode="machine",
        active_work_coordinate_system=None,
        controller_coordinate_offsets={},
        axis_index=AXIS_INDEX,
    )

    assert mapper.axis_z_display_for_gcode_coordinate(
        2.0
    ) == axis_z_display_for_gcode_coordinate(calibration, 2.0)
