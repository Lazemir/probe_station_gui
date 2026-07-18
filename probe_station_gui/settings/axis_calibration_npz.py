"""Pure import and normalisation for axis calibration NPZ files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile

import numpy as np


LINEAR_INTERPOLATION_MODEL = "linear_interpolation"


class AxisCalibrationImportError(ValueError):
    """Report calibration files that cannot produce a usable curve."""


@dataclass(frozen=True)
class ImportedAxisCalibration:
    """A deterministic interpolation snapshot imported from an NPZ file."""

    calibration_file: str
    gcode_points_mm: tuple[float, ...]
    display_points_mm: tuple[float, ...]
    branch_direction: int | None


def load_axis_calibration_npz(
    path: str | Path,
    *,
    axis: str,
    final_direction: int,
) -> ImportedAxisCalibration:
    """Load one axis calibration curve from *path*."""

    normalized_axis = axis.upper()
    if normalized_axis not in {"A", "Z"}:
        raise AxisCalibrationImportError("Calibration axis must be A or Z.")
    calibration_path = Path(path).resolve()
    try:
        return _load_axis_calibration_npz(
            calibration_path,
            axis=normalized_axis,
            final_direction=final_direction,
        )
    except AxisCalibrationImportError:
        raise
    except (BadZipFile, EOFError, OSError, TypeError, ValueError) as error:
        raise AxisCalibrationImportError(
            f"Calibration file '{calibration_path}' could not be read."
        ) from error


def _load_axis_calibration_npz(
    calibration_path: Path,
    *,
    axis: str,
    final_direction: int,
) -> ImportedAxisCalibration:
    with np.load(calibration_path, allow_pickle=False) as archive:
        for required_name in ("gcode", "indicator"):
            if required_name not in archive.files:
                raise AxisCalibrationImportError(
                    f"Calibration file is missing the '{required_name}' array."
                )
        gcode = np.asarray(archive["gcode"], dtype=float)
        indicator = np.asarray(archive["indicator"], dtype=float)
        direction = (
            np.asarray(archive["direction"]) if "direction" in archive.files else None
        )

    arrays = (gcode, indicator) if direction is None else (gcode, indicator, direction)
    if any(values.ndim != 1 for values in arrays) or any(
        values.shape != gcode.shape for values in arrays[1:]
    ):
        raise AxisCalibrationImportError(
            "Calibration arrays must have the same one-dimensional shape."
        )
    if not np.all(np.isfinite(gcode)) or not np.all(np.isfinite(indicator)):
        raise AxisCalibrationImportError(
            "Calibration G-code and indicator values must be finite."
        )

    if direction is not None:
        branch_mask = direction == final_direction
        if not np.any(branch_mask):
            raise AxisCalibrationImportError(
                f"Calibration file has no samples for direction {final_direction}."
            )
        gcode = gcode[branch_mask]
        indicator = indicator[branch_mask]
    order = np.argsort(gcode)
    gcode = gcode[order]
    indicator = indicator[order]
    unique_gcode, group_starts, group_counts = np.unique(
        gcode,
        return_index=True,
        return_counts=True,
    )
    if unique_gcode.size < 2:
        raise AxisCalibrationImportError(
            "Calibration curve needs at least two unique G-code points."
        )
    indicator = np.asarray(
        [
            np.median(indicator[start : start + count])
            for start, count in zip(group_starts, group_counts)
        ]
    )
    gcode = unique_gcode
    display = -indicator if axis.upper() == "A" else indicator
    plateau_starts = np.r_[0, np.flatnonzero(np.diff(display) != 0) + 1]
    plateau_ends = np.r_[plateau_starts[1:], display.size]
    gcode = np.asarray(
        [
            np.median(gcode[start:end])
            for start, end in zip(plateau_starts, plateau_ends)
        ]
    )
    display = display[plateau_starts]
    if display.size < 2:
        raise AxisCalibrationImportError(
            "Calibration curve needs at least two usable points after normalization."
        )
    if not np.all(np.diff(display) > 0):
        raise AxisCalibrationImportError(
            "Calibration display values must be strictly increasing."
        )

    return ImportedAxisCalibration(
        calibration_file=str(calibration_path),
        gcode_points_mm=tuple(float(value) for value in gcode),
        display_points_mm=tuple(float(value) for value in display),
        branch_direction=final_direction if direction is not None else None,
    )
