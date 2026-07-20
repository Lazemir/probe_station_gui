"""Strict loader for universal axis-calibration NPZ files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile

import numpy as np

from probe_station_gui.settings.axis_calibration_config import CALIBRATION_AXES


class AxisCalibrationImportError(ValueError):
    """Report an unreadable or structurally invalid calibration archive."""


@dataclass(frozen=True)
class ImportedAxisCalibration:
    """Validated calibration data ready to snapshot in settings."""

    axis: str
    calibration_file: str
    controller_points: tuple[float, ...]
    physical_points: tuple[float, ...]


def load_axis_calibration_npz(
    path: str | Path,
    *,
    expected_axis: str,
) -> ImportedAxisCalibration:
    """Read *path* without modifying its measured curve."""

    selected_axis = str(expected_axis).upper()
    if selected_axis not in CALIBRATION_AXES:
        raise AxisCalibrationImportError(f"Unsupported calibration axis {expected_axis!r}.")

    calibration_path = Path(path).resolve()
    try:
        return _load_axis_calibration_npz(calibration_path, selected_axis)
    except AxisCalibrationImportError:
        raise
    except (BadZipFile, EOFError, OSError, TypeError, ValueError) as error:
        raise AxisCalibrationImportError(
            f"Calibration file '{calibration_path}' could not be read."
        ) from error


def _load_axis_calibration_npz(
    calibration_path: Path,
    expected_axis: str,
) -> ImportedAxisCalibration:
    with np.load(calibration_path, allow_pickle=False) as archive:
        for required_name in ("axis", "controller", "physical"):
            if required_name not in archive.files:
                raise AxisCalibrationImportError(
                    f"Calibration file is missing the '{required_name}' entry."
                )
        axis_value = np.asarray(archive["axis"])
        controller = np.asarray(archive["controller"], dtype=float)
        physical = np.asarray(archive["physical"], dtype=float)

    if axis_value.ndim != 0 or axis_value.dtype.kind != "U":
        raise AxisCalibrationImportError("Calibration axis must be a scalar Unicode value.")
    axis = str(axis_value.item())
    if axis not in CALIBRATION_AXES:
        raise AxisCalibrationImportError("Calibration axis must be X, Y, Z, A, B, or C.")
    if axis != expected_axis:
        raise AxisCalibrationImportError(
            f"Calibration axis {axis} does not match selected {expected_axis}."
        )
    if controller.ndim != 1 or physical.ndim != 1:
        raise AxisCalibrationImportError("Calibration arrays must be one-dimensional.")
    if controller.size != physical.size:
        raise AxisCalibrationImportError("Calibration arrays must have the same length.")
    if controller.size < 2:
        raise AxisCalibrationImportError("Calibration curve needs at least two points.")
    if not np.all(np.isfinite(controller)) or not np.all(np.isfinite(physical)):
        raise AxisCalibrationImportError("Calibration coordinates must be finite.")
    if not np.all(np.diff(controller) > 0):
        raise AxisCalibrationImportError(
            "Calibration controller coordinates must be strictly increasing."
        )
    if not np.all(np.diff(physical) > 0):
        raise AxisCalibrationImportError(
            "Calibration physical coordinates must be strictly increasing."
        )

    return ImportedAxisCalibration(
        axis=axis,
        calibration_file=str(calibration_path),
        controller_points=tuple(float(value) for value in controller),
        physical_points=tuple(float(value) for value in physical),
    )
