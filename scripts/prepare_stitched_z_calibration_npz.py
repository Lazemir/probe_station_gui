"""Stitch the three raw Z-calibration sections into one forward curve."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.prepare_axis_calibration_npz import (
    CALIBRATION_AXES,
    longest_strictly_increasing_indices,
)


SECTION_1_END_MM = 12.0
SECTION_2_END_MM = 20.214
SECTION_2_OFFSET_MM = 8.661368914604154
SECTION_3_OFFSET_MM = 13.56547962940159


def _load_undirected_section(source: str | Path) -> tuple[np.ndarray, np.ndarray]:
    source_path = Path(source)
    try:
        with np.load(source_path, allow_pickle=False) as archive:
            gcode = np.asarray(archive["gcode"], dtype=float)
            indicator = np.asarray(archive["indicator"], dtype=float)
    except KeyError as exc:
        raise ValueError(f"Raw archive is missing {exc.args[0]!r}.") from exc
    _validate_measurement_arrays(gcode, indicator)
    return gcode, indicator


def _load_directed_section(
    source: str | Path,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    source_path = Path(source)
    try:
        with np.load(source_path, allow_pickle=False) as archive:
            gcode = np.asarray(archive["gcode"], dtype=float)
            indicator = np.asarray(archive["indicator"], dtype=float)
            direction = np.asarray(archive["direction"], dtype=float)
    except KeyError as exc:
        raise ValueError(f"Raw archive is missing {exc.args[0]!r}.") from exc
    _validate_measurement_arrays(gcode, indicator, direction)
    return gcode, indicator, direction


def _validate_measurement_arrays(
    gcode: np.ndarray, indicator: np.ndarray, direction: np.ndarray | None = None
) -> None:
    arrays = (gcode, indicator) if direction is None else (gcode, indicator, direction)
    if any(array.ndim != 1 for array in arrays):
        raise ValueError("Raw calibration arrays must be one-dimensional.")
    if any(array.size != gcode.size for array in arrays):
        raise ValueError("Raw calibration arrays must have equal lengths.")
    if not (np.all(np.isfinite(gcode)) and np.all(np.isfinite(indicator))):
        raise ValueError("Raw calibration samples must be finite.")
    if direction is not None and not np.all(np.isfinite(direction)):
        raise ValueError("Raw calibration samples must be finite.")
    if not np.all(np.diff(gcode) > 0.0):
        raise ValueError("Raw gcode samples must be strictly increasing.")


def _validate_prepared_arrays(controller: np.ndarray, physical: np.ndarray) -> None:
    if controller.size < 2:
        raise ValueError("Prepared calibration has fewer than two usable samples.")
    if not (np.all(np.isfinite(controller)) and np.all(np.isfinite(physical))):
        raise ValueError("Prepared calibration samples must be finite.")
    if not np.all(np.diff(controller) > 0.0):
        raise ValueError("Prepared controller samples are not strictly increasing.")
    if not np.all(np.diff(physical) > 0.0):
        raise ValueError("Prepared physical samples are not strictly increasing.")


def prepare_stitched_z_calibration(
    section1: str | Path,
    section2: str | Path,
    section3: str | Path,
    destination: str | Path,
    *,
    axis: str = "Z",
) -> int:
    """Build a forward-only curve from the three Z-calibration sections."""
    destination_path = Path(destination)
    axis_name = str(axis).strip().upper()
    if axis_name not in CALIBRATION_AXES:
        raise ValueError(f"Unsupported calibration axis: {axis!r}")
    if destination_path.exists():
        raise FileExistsError(f"Destination already exists: {destination_path}")

    s1_gcode, s1_indicator = _load_undirected_section(section1)
    s2_gcode, s2_indicator = _load_undirected_section(section2)
    s3_gcode, s3_indicator, s3_direction = _load_directed_section(section3)

    s1_mask = s1_gcode < SECTION_1_END_MM
    s2_mask = (s2_gcode >= SECTION_1_END_MM) & (s2_gcode <= SECTION_2_END_MM)
    s3_mask = (s3_direction == 1) & (s3_gcode > SECTION_2_END_MM)
    controller = np.concatenate((s1_gcode[s1_mask], s2_gcode[s2_mask], s3_gcode[s3_mask]))
    physical = np.concatenate(
        (
            s1_indicator[s1_mask],
            s2_indicator[s2_mask] + SECTION_2_OFFSET_MM,
            s3_indicator[s3_mask] + SECTION_3_OFFSET_MM,
        )
    )
    selected = longest_strictly_increasing_indices(physical)
    controller = controller[selected]
    physical = physical[selected]
    _validate_prepared_arrays(controller, physical)

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        destination_path,
        axis=np.asarray(axis_name),
        controller=controller,
        physical=physical,
    )
    return int(controller.size)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("section1", type=Path)
    parser.add_argument("section2", type=Path)
    parser.add_argument("section3", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--axis", default="Z", choices=tuple(sorted(CALIBRATION_AXES)))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    count = prepare_stitched_z_calibration(
        arguments.section1,
        arguments.section2,
        arguments.section3,
        arguments.destination,
        axis=arguments.axis,
    )
    print(count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
