"""Pure objective calibration settings normalisation helpers."""

from __future__ import annotations

import math
from collections.abc import Iterable


def parse_pixels_to_mm_matrix(raw_matrix: object) -> list[list[float]]:
    """Return a validated 2x2 pixels-to-mm matrix."""

    if not isinstance(raw_matrix, Iterable) or isinstance(raw_matrix, (str, bytes)):
        return []
    rows: list[list[float]] = []
    for raw_row in raw_matrix:
        if not isinstance(raw_row, Iterable) or isinstance(raw_row, (str, bytes)):
            return []
        row: list[float] = []
        for raw_value in raw_row:
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                return []
            if not math.isfinite(value):
                return []
            row.append(value)
        rows.append(row)
    if len(rows) != 2 or any(len(row) != 2 for row in rows):
        return []
    det = rows[0][0] * rows[1][1] - rows[0][1] * rows[1][0]
    if abs(det) < 1e-18:
        return []
    return rows
