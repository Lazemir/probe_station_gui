"""Needle target selection and persisted target normalisation."""

from __future__ import annotations

import math
from collections.abc import Callable


def normalise_needle_lowering_target(
    position_mm: float | None,
    *,
    lowering_for_gcode_coordinate: Callable[[float], float],
) -> float | None:
    if position_mm is None:
        return None
    lowering_mm = float(position_mm)
    if lowering_mm < 0.0:
        lowering_mm = lowering_for_gcode_coordinate(lowering_mm)
    return max(0.0, lowering_mm)


def normalise_needle_contact_zone(
    contact_zone_mm: float | None,
    *,
    default: float,
) -> float:
    if contact_zone_mm is None:
        return float(default)
    try:
        zone_mm = float(contact_zone_mm)
    except (TypeError, ValueError):
        zone_mm = float(default)
    if not math.isfinite(zone_mm) or zone_mm < 0.0:
        zone_mm = float(default)
    return zone_mm


def needle_target_lowering_for_action(
    action: str,
    *,
    down_lowering_mm: float | None,
    boundary_lowering: float | None,
    error_factory: Callable[[str], Exception] = ValueError,
) -> float:
    if action not in {"raise", "lift", "lower"}:
        raise error_factory(f"Unknown needle action: {action}.")
    if action == "raise":
        return 0.0
    if action == "lift":
        if boundary_lowering is None:
            if down_lowering_mm is None:
                raise error_factory("Needle down calibration missing; cannot lift.")
            raise error_factory("Needle contact zone is zero; cannot lift.")
        return boundary_lowering
    if down_lowering_mm is None:
        raise error_factory("Needle down calibration missing; cannot lower.")
    return max(0.0, float(down_lowering_mm))
