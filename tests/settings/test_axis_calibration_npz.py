import numpy as np
import pytest

from probe_station_gui.settings.axis_calibration_npz import (
    AxisCalibrationImportError,
    load_axis_calibration_npz,
)


def test_import_z_selects_requested_direction_and_sorts_points(tmp_path) -> None:
    path = tmp_path / "z.npz"
    np.savez(
        path,
        gcode=np.array([2.0, 0.0, 1.0, 2.0, 0.0, 1.0]),
        indicator=np.array([2.1, 0.1, 1.1, 2.0, 0.0, 1.0]),
        direction=np.array([-1, -1, -1, 1, 1, 1]),
    )

    imported = load_axis_calibration_npz(path, axis="Z", final_direction=1)

    assert imported.calibration_file == str(path.resolve())
    assert imported.gcode_points_mm == (0.0, 1.0, 2.0)
    assert imported.display_points_mm == (0.0, 1.0, 2.0)
    assert imported.branch_direction == 1


def test_import_a_uses_negative_indicator_display_convention(tmp_path) -> None:
    path = tmp_path / "a.npz"
    np.savez(path, gcode=[-2.0, -1.0, 0.0], indicator=[2.0, 1.0, 0.0])

    imported = load_axis_calibration_npz(path, axis="A", final_direction=-1)

    assert imported.gcode_points_mm == (-2.0, -1.0, 0.0)
    assert imported.display_points_mm == (-2.0, -1.0, -0.0)
    assert imported.branch_direction is None


@pytest.mark.parametrize("missing_name", ["gcode", "indicator"])
def test_import_rejects_missing_required_arrays(tmp_path, missing_name) -> None:
    path = tmp_path / "missing.npz"
    arrays = {"gcode": [0.0, 1.0], "indicator": [0.0, 1.0]}
    arrays.pop(missing_name)
    np.savez(path, **arrays)

    with pytest.raises(AxisCalibrationImportError, match=missing_name):
        load_axis_calibration_npz(path, axis="Z", final_direction=1)


def test_import_rejects_mismatched_array_shapes(tmp_path) -> None:
    path = tmp_path / "mismatched.npz"
    np.savez(path, gcode=[0.0, 1.0], indicator=[0.0, 1.0, 2.0])

    with pytest.raises(AxisCalibrationImportError, match="same one-dimensional shape"):
        load_axis_calibration_npz(path, axis="Z", final_direction=1)


@pytest.mark.parametrize("array_name", ["gcode", "indicator"])
def test_import_rejects_non_finite_values(tmp_path, array_name) -> None:
    path = tmp_path / "non-finite.npz"
    arrays = {"gcode": [0.0, 1.0], "indicator": [0.0, 1.0]}
    arrays[array_name][1] = np.nan
    np.savez(path, **arrays)

    with pytest.raises(AxisCalibrationImportError, match="finite"):
        load_axis_calibration_npz(path, axis="Z", final_direction=1)


def test_import_rejects_missing_requested_branch(tmp_path) -> None:
    path = tmp_path / "branch.npz"
    np.savez(
        path,
        gcode=[0.0, 1.0],
        indicator=[0.0, 1.0],
        direction=[-1, -1],
    )

    with pytest.raises(AxisCalibrationImportError, match="direction 1"):
        load_axis_calibration_npz(path, axis="Z", final_direction=1)


def test_import_rejects_fewer_than_two_unique_gcode_points(tmp_path) -> None:
    path = tmp_path / "one-point.npz"
    np.savez(path, gcode=[1.0, 1.0], indicator=[2.0, 2.1])

    with pytest.raises(AxisCalibrationImportError, match="two unique G-code"):
        load_axis_calibration_npz(path, axis="Z", final_direction=1)


def test_import_aggregates_duplicate_gcode_with_median_indicator(tmp_path) -> None:
    path = tmp_path / "duplicates.npz"
    np.savez(
        path,
        gcode=[0.0, 0.0, 0.0, 1.0, 1.0, 2.0],
        indicator=[0.0, 10.0, 2.0, 4.0, 6.0, 8.0],
    )

    imported = load_axis_calibration_npz(path, axis="Z", final_direction=1)

    assert imported.gcode_points_mm == (0.0, 1.0, 2.0)
    assert imported.display_points_mm == (2.0, 5.0, 8.0)


def test_import_rejects_non_monotonic_display_curve(tmp_path) -> None:
    path = tmp_path / "non-monotonic.npz"
    np.savez(path, gcode=[0.0, 1.0, 2.0], indicator=[0.0, 2.0, 1.0])

    with pytest.raises(AxisCalibrationImportError, match="strictly increasing"):
        load_axis_calibration_npz(path, axis="Z", final_direction=1)


def test_import_collapses_adjacent_equal_display_plateaus(tmp_path) -> None:
    path = tmp_path / "plateau.npz"
    np.savez(
        path,
        gcode=[0.0, 1.0, 2.0, 3.0],
        indicator=[0.0, 1.0, 1.0, 2.0],
    )

    imported = load_axis_calibration_npz(path, axis="Z", final_direction=1)

    assert imported.gcode_points_mm == (0.0, 1.5, 3.0)
    assert imported.display_points_mm == (0.0, 1.0, 2.0)


def test_import_wraps_malformed_numeric_arrays(tmp_path) -> None:
    path = tmp_path / "malformed.npz"
    np.savez(path, gcode=["left", "right"], indicator=[0.0, 1.0])

    with pytest.raises(AxisCalibrationImportError, match="could not be read"):
        load_axis_calibration_npz(path, axis="Z", final_direction=1)


def test_import_wraps_corrupted_zip_archive(tmp_path) -> None:
    path = tmp_path / "corrupted.npz"
    path.write_bytes(b"PK\x03\x04" + bytes(26))

    with pytest.raises(AxisCalibrationImportError, match="could not be read"):
        load_axis_calibration_npz(path, axis="Z", final_direction=1)


def test_import_rejects_curve_reduced_to_one_display_point(tmp_path) -> None:
    path = tmp_path / "flat.npz"
    np.savez(path, gcode=[0.0, 1.0, 2.0], indicator=[1.0, 1.0, 1.0])

    with pytest.raises(AxisCalibrationImportError, match="two usable points"):
        load_axis_calibration_npz(path, axis="Z", final_direction=1)


def test_import_rejects_unsupported_axis(tmp_path) -> None:
    path = tmp_path / "axis.npz"
    np.savez(path, gcode=[0.0, 1.0], indicator=[0.0, 1.0])

    with pytest.raises(AxisCalibrationImportError, match="A or Z"):
        load_axis_calibration_npz(path, axis="X", final_direction=1)
