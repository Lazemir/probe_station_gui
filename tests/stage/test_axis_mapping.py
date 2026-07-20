import math

import pytest

from probe_station_gui.settings.axis_calibration_config import AxisCalibrationSettings
from probe_station_gui.stage.axis_mapping import (
    AxisCalibrationCurve,
    CalibrationOutOfDomain,
    controller_to_physical,
    curve_from_settings,
    physical_to_controller,
)


def test_piecewise_linear_interpolation_round_trips_interior_values() -> None:
    curve = AxisCalibrationCurve(
        controller=(0.0, 1.0, 3.0),
        physical=(10.0, 12.0, 15.0),
    )
    assert controller_to_physical(curve, 2.0) == pytest.approx(13.5)
    assert physical_to_controller(curve, 13.5) == pytest.approx(2.0)


def test_domain_endpoints_are_valid() -> None:
    curve = AxisCalibrationCurve((0.0, 2.0), (1.0, 5.0))

    assert controller_to_physical(curve, 0.0) == 1.0
    assert controller_to_physical(curve, 2.0) == 5.0
    assert physical_to_controller(curve, 1.0) == 0.0
    assert physical_to_controller(curve, 5.0) == 2.0


@pytest.mark.parametrize("value", [-0.001, 2.001, -math.inf, math.inf, math.nan])
def test_controller_values_outside_domain_are_rejected(value: float) -> None:
    curve = AxisCalibrationCurve((0.0, 2.0), (1.0, 5.0))

    with pytest.raises(CalibrationOutOfDomain):
        controller_to_physical(curve, value)


@pytest.mark.parametrize("value", [0.999, 5.001, -math.inf, math.inf, math.nan])
def test_physical_values_outside_domain_are_rejected(value: float) -> None:
    curve = AxisCalibrationCurve((0.0, 2.0), (1.0, 5.0))

    with pytest.raises(CalibrationOutOfDomain):
        physical_to_controller(curve, value)


def test_curve_is_created_only_for_enabled_valid_settings() -> None:
    disabled = AxisCalibrationSettings(
        enabled=False,
        controller_points=[0.0, 1.0],
        physical_points=[0.0, 2.0],
    )
    enabled = disabled.clone()
    enabled.enabled = True

    assert curve_from_settings(disabled) is None
    assert curve_from_settings(enabled) == AxisCalibrationCurve(
        (0.0, 1.0),
        (0.0, 2.0),
    )
