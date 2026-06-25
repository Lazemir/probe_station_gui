import pytest

from probe_station_gui.stage.axis_mapping import (
    axis_a_gcode_coordinate_for_lowering,
    axis_a_lowering_for_gcode_coordinate,
    axis_a_model_calibrated_coordinate_for_commanded,
    axis_a_model_lowering_for_commanded,
    axis_z_display_for_gcode_coordinate,
    axis_z_gcode_coordinate_for_display,
    evaluate_polynomial,
)


def test_axis_a_mapping_falls_back_to_raw_gcode_sign_without_calibration() -> None:
    assert axis_a_gcode_coordinate_for_lowering(None, 1.25) == -1.25
    assert axis_a_lowering_for_gcode_coordinate(None, -1.25) == 1.25
    assert axis_a_model_calibrated_coordinate_for_commanded(None, 1.25) == -1.25
    assert axis_a_model_lowering_for_commanded(None, 1.25) == 1.25


def test_evaluate_polynomial_uses_horner_order() -> None:
    assert evaluate_polynomial((2.0, 3.0, 4.0), 5.0) == 69.0


def test_axis_z_mapping_applies_and_inverts_monotonic_polynomial() -> None:
    calibration = {
        "coefficients": (2.0, 1.0),
        "min": 0.0,
        "max": 10.0,
    }

    assert axis_z_display_for_gcode_coordinate(calibration, 3.0) == 7.0
    assert axis_z_gcode_coordinate_for_display(calibration, 7.0) == pytest.approx(3.0)
    assert axis_z_gcode_coordinate_for_display(calibration, -5.0) == 0.0
    assert axis_z_gcode_coordinate_for_display(calibration, 99.0) == 10.0
