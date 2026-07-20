"""Prepare a strict forward-only axis calibration archive from raw measurements."""

from __future__ import annotations

import argparse
from bisect import bisect_left
from pathlib import Path
from typing import Sequence

import numpy as np


CALIBRATION_AXES = frozenset("XYZABC")


def longest_strictly_increasing_indices(values: Sequence[float]) -> np.ndarray:
    samples = np.asarray(values, dtype=float)
    if samples.ndim != 1:
        raise ValueError("Values must be a one-dimensional array.")
    if samples.size == 0:
        return np.asarray([], dtype=np.int64)
    if not np.all(np.isfinite(samples)):
        raise ValueError("Values must be finite.")

    tail_values: list[float] = []
    tail_indices: list[int] = []
    predecessors = np.full(samples.size, -1, dtype=np.int64)
    for index, sample in enumerate(samples):
        insertion = bisect_left(tail_values, float(sample))
        if insertion > 0:
            predecessors[index] = tail_indices[insertion - 1]
        if insertion == len(tail_values):
            tail_values.append(float(sample))
            tail_indices.append(index)
        else:
            tail_values[insertion] = float(sample)
            tail_indices[insertion] = index

    selected: list[int] = []
    current = tail_indices[-1]
    while current >= 0:
        selected.append(current)
        current = int(predecessors[current])
    selected.reverse()
    return np.asarray(selected, dtype=np.int64)


def prepare_forward_calibration(
    source: str | Path,
    destination: str | Path,
    *,
    axis: str = "Z",
) -> int:
    source_path = Path(source)
    destination_path = Path(destination)
    axis_name = str(axis).strip().upper()
    if axis_name not in CALIBRATION_AXES:
        raise ValueError(f"Unsupported calibration axis: {axis!r}")
    if destination_path.exists():
        raise FileExistsError(f"Destination already exists: {destination_path}")

    try:
        with np.load(source_path, allow_pickle=False) as archive:
            gcode = np.asarray(archive["gcode"], dtype=float)
            indicator = np.asarray(archive["indicator"], dtype=float)
            direction = np.asarray(archive["direction"])
    except KeyError as exc:
        raise ValueError(f"Raw archive is missing {exc.args[0]!r}.") from exc

    if any(array.ndim != 1 for array in (gcode, indicator, direction)):
        raise ValueError("Raw calibration arrays must be one-dimensional.")
    if not (gcode.size == indicator.size == direction.size):
        raise ValueError("Raw calibration arrays must have equal lengths.")
    direct = direction == 1
    controller_candidates = gcode[direct]
    physical_candidates = indicator[direct]
    if controller_candidates.size < 2:
        raise ValueError("Raw archive has fewer than two direct-pass samples.")
    if not (
        np.all(np.isfinite(controller_candidates))
        and np.all(np.isfinite(physical_candidates))
    ):
        raise ValueError("Raw direct-pass samples must be finite.")
    if not np.all(np.diff(controller_candidates) > 0.0):
        raise ValueError("Direct-pass gcode samples must be strictly increasing.")

    selected = longest_strictly_increasing_indices(physical_candidates)
    controller = controller_candidates[selected]
    physical = physical_candidates[selected]
    if controller.size < 2:
        raise ValueError("No usable strictly increasing calibration curve was found.")
    if not np.all(np.diff(controller) > 0.0):
        raise ValueError("Prepared controller samples are not strictly increasing.")
    if not np.all(np.diff(physical) > 0.0):
        raise ValueError("Prepared physical samples are not strictly increasing.")

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
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--axis", default="Z", choices=tuple(sorted(CALIBRATION_AXES)))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    count = prepare_forward_calibration(
        arguments.source,
        arguments.destination,
        axis=arguments.axis,
    )
    print(count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
