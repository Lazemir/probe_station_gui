import numpy as np
import pytest

from probe_station_gui.settings.axis_calibration_npz import (
    AxisCalibrationImportError,
    load_axis_calibration_npz,
)


@pytest.mark.parametrize("axis", ["X", "Y", "Z", "A", "B", "C"])
def test_imports_one_strict_curve_for_each_axis(tmp_path, axis: str) -> None:
    path = tmp_path / f"{axis}.npz"
    np.savez(
        path,
        axis=np.array(axis),
        controller=np.array([0.0, 1.0, 2.0]),
        physical=np.array([10.0, 11.5, 14.0]),
    )

    imported = load_axis_calibration_npz(path, expected_axis=axis)

    assert imported.axis == axis
    assert imported.calibration_file == str(path.resolve())
    assert imported.controller_points == (0.0, 1.0, 2.0)
    assert imported.physical_points == (10.0, 11.5, 14.0)


def test_import_rejects_axis_mismatch(tmp_path) -> None:
    path = tmp_path / "wrong-axis.npz"
    np.savez(path, axis=np.array("Y"), controller=[0.0, 1.0], physical=[0.0, 1.0])

    with pytest.raises(AxisCalibrationImportError, match="axis Y.*selected X"):
        load_axis_calibration_npz(path, expected_axis="X")


@pytest.mark.parametrize("missing", ["axis", "controller", "physical"])
def test_import_rejects_missing_required_entry(tmp_path, missing: str) -> None:
    path = tmp_path / "missing.npz"
    values = {"axis": np.array("X"), "controller": [0.0, 1.0], "physical": [0.0, 1.0]}
    values.pop(missing)
    np.savez(path, **values)

    with pytest.raises(AxisCalibrationImportError, match=missing):
        load_axis_calibration_npz(path, expected_axis="X")


@pytest.mark.parametrize("axis_value", [np.array(["X"]), np.array(1), np.array("x"), np.array("Q")])
def test_import_rejects_invalid_axis_value(tmp_path, axis_value) -> None:
    path = tmp_path / "axis.npz"
    np.savez(path, axis=axis_value, controller=[0.0, 1.0], physical=[0.0, 1.0])

    with pytest.raises(AxisCalibrationImportError, match="axis"):
        load_axis_calibration_npz(path, expected_axis="X")


@pytest.mark.parametrize(
    ("controller", "physical", "message"),
    [
        ([[0.0, 1.0]], [0.0, 1.0], "one-dimensional"),
        ([0.0, 1.0], [[0.0, 1.0]], "one-dimensional"),
        ([0.0, 1.0], [0.0, 1.0, 2.0], "same length"),
        ([0.0], [0.0], "at least two"),
        ([0.0, float("nan")], [0.0, 1.0], "finite"),
        ([0.0, 1.0], [0.0, float("inf")], "finite"),
        ([0.0, 0.0], [0.0, 1.0], "controller.*strictly increasing"),
        ([1.0, 0.0], [0.0, 1.0], "controller.*strictly increasing"),
        ([0.0, 1.0], [0.0, 0.0], "physical.*strictly increasing"),
        ([0.0, 1.0], [1.0, 0.0], "physical.*strictly increasing"),
    ],
)
def test_import_rejects_invalid_curve(tmp_path, controller, physical, message: str) -> None:
    path = tmp_path / "invalid.npz"
    np.savez(path, axis=np.array("X"), controller=controller, physical=physical)

    with pytest.raises(AxisCalibrationImportError, match=message):
        load_axis_calibration_npz(path, expected_axis="X")


def test_import_rejects_malformed_numeric_arrays(tmp_path) -> None:
    path = tmp_path / "malformed.npz"
    np.savez(path, axis=np.array("X"), controller=["left", "right"], physical=[0.0, 1.0])

    with pytest.raises(AxisCalibrationImportError, match="could not be read"):
        load_axis_calibration_npz(path, expected_axis="X")


def test_import_rejects_corrupted_archive(tmp_path) -> None:
    path = tmp_path / "corrupted.npz"
    path.write_bytes(b"PK\x03\x04" + bytes(26))

    with pytest.raises(AxisCalibrationImportError, match="could not be read"):
        load_axis_calibration_npz(path, expected_axis="X")

