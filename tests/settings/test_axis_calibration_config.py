import pytest

from probe_station_gui.settings.axis_calibration_config import (
    CALIBRATION_AXES,
    AxisCalibrationSettings,
    axis_unit,
    default_axis_calibrations,
    parse_axis_calibrations,
)
from probe_station_gui.settings.manager import Settings, SettingsManager


def test_defaults_contain_all_six_disabled_axes() -> None:
    calibrations = default_axis_calibrations()

    assert tuple(calibrations) == CALIBRATION_AXES == ("X", "Y", "Z", "A", "B", "C")
    assert all(value == AxisCalibrationSettings() for value in calibrations.values())


@pytest.mark.parametrize(
    ("axis", "expected"),
    [("X", "mm"), ("Y", "mm"), ("Z", "mm"), ("A", "mm"), ("B", "deg"), ("C", "deg")],
)
def test_axis_unit_is_defined_by_axis(axis: str, expected: str) -> None:
    assert axis_unit(axis) == expected


def test_settings_clone_does_not_share_point_lists() -> None:
    original = AxisCalibrationSettings(
        enabled=True,
        calibration_file="x.npz",
        controller_points=[0.0, 1.0],
        physical_points=[2.0, 3.0],
    )

    clone = original.clone()
    clone.controller_points.append(2.0)

    assert original.controller_points == [0.0, 1.0]
    assert clone.to_dict() == {
        "enabled": True,
        "calibration_file": "x.npz",
        "controller_points": [0.0, 1.0, 2.0],
        "physical_points": [2.0, 3.0],
    }


def test_parser_normalizes_present_and_missing_axes() -> None:
    parsed = parse_axis_calibrations(
        {
            "x": {
                "enabled": True,
                "calibration_file": "  x.npz  ",
                "controller_points": [0, 1],
                "physical_points": [2, 3],
            }
        }
    )

    assert parsed["X"] == AxisCalibrationSettings(
        enabled=True,
        calibration_file="x.npz",
        controller_points=[0.0, 1.0],
        physical_points=[2.0, 3.0],
    )
    assert all(parsed[axis] == AxisCalibrationSettings() for axis in CALIBRATION_AXES[1:])


@pytest.mark.parametrize(
    ("controller", "physical"),
    [
        ([0.0], [0.0]),
        ([0.0, 1.0], [0.0]),
        ([0.0, 0.0], [0.0, 1.0]),
        ([1.0, 0.0], [0.0, 1.0]),
        ([0.0, 1.0], [0.0, 0.0]),
        ([0.0, 1.0], [1.0, 0.0]),
        ([0.0, float("nan")], [0.0, 1.0]),
    ],
)
def test_invalid_enabled_snapshot_is_disabled_and_cleared(controller, physical) -> None:
    parsed = parse_axis_calibrations(
        {
            "X": {
                "enabled": True,
                "calibration_file": "x.npz",
                "controller_points": controller,
                "physical_points": physical,
            }
        }
    )

    assert parsed["X"] == AxisCalibrationSettings()


def test_valid_disabled_snapshot_is_retained_for_preview() -> None:
    parsed = parse_axis_calibrations(
        {
            "Z": {
                "enabled": False,
                "calibration_file": "z.npz",
                "controller_points": [0.0, 1.0],
                "physical_points": [10.0, 11.0],
            }
        }
    )

    assert not parsed["Z"].enabled
    assert parsed["Z"].calibration_file == "z.npz"
    assert parsed["Z"].physical_points == [10.0, 11.0]


def test_invalid_disabled_snapshot_is_cleared_too() -> None:
    parsed = parse_axis_calibrations(
        {
            "Z": {
                "enabled": False,
                "calibration_file": "z.npz",
                "controller_points": [0.0, 1.0],
                "physical_points": [2.0, 1.0],
            }
        }
    )

    assert parsed["Z"] == AxisCalibrationSettings()


def test_application_settings_clone_and_serialize_axis_calibrations() -> None:
    settings = Settings()
    settings.axis_calibrations["B"] = AxisCalibrationSettings(
        enabled=True,
        calibration_file="b.npz",
        controller_points=[0.0, 90.0],
        physical_points=[1.0, 92.0],
    )

    clone = settings.clone()
    clone.axis_calibrations["B"].controller_points.append(180.0)

    assert settings.axis_calibrations["B"].controller_points == [0.0, 90.0]
    serialized = settings.to_dict()
    assert "axis_a_calibration" not in serialized
    assert "axis_z_calibration" not in serialized
    assert serialized["axis_calibrations"]["B"]["physical_points"] == [1.0, 92.0]


def test_settings_manager_returns_independent_axis_calibration_configuration() -> None:
    manager = SettingsManager.__new__(SettingsManager)
    manager._settings = Settings()

    configuration = manager.axis_calibrations_configuration()
    configuration["X"].controller_points.append(1.0)

    assert manager.settings.axis_calibrations["X"].controller_points == []
