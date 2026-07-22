from __future__ import annotations

from probe_station_gui.coordinates.calibration import changed_calibration_axes
from probe_station_gui.settings.axis_calibration_config import AxisCalibrationSettings, default_axis_calibrations


def test_curve_data_change_has_a_distinct_calibration_fingerprint() -> None:
    previous = default_axis_calibrations()
    current = default_axis_calibrations()
    current["Z"] = AxisCalibrationSettings(
        controller_points=[0.0, 1.0, 2.0],
        physical_points=[0.0, 1.1, 2.3],
    )

    assert changed_calibration_axes(previous, current) == {"Z"}


def test_calibration_fingerprints_only_report_changed_axis() -> None:
    previous = default_axis_calibrations()
    current = default_axis_calibrations()
    current["A"] = AxisCalibrationSettings(
        controller_points=[0.0, 1.0],
        physical_points=[0.0, 2.0],
    )

    assert changed_calibration_axes(previous, current) == {"A"}
