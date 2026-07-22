"""Immutable rotation geometry shared by coordinate consumers."""

from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class RotationGeometrySnapshot:
    pivot_machine_xy: tuple[float, float]
    source: str
    calibration_version: int
    objective_name: str


def rotation_geometry_snapshot(software_coordinates: object) -> RotationGeometrySnapshot:
    """Validate and freeze the configured B-axis rotation geometry."""

    pivot = getattr(software_coordinates, "pivot", None)
    if pivot is None:
        raise ValueError("B-axis rotation pivot settings are unavailable.")
    try:
        x_value = float(getattr(pivot, "x_mm"))
        y_value = float(getattr(pivot, "y_mm"))
        version = int(getattr(pivot, "calibration_version", 0))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("B-axis rotation pivot settings are invalid.") from exc
    if not math.isfinite(x_value) or not math.isfinite(y_value) or version < 0:
        raise ValueError("B-axis rotation pivot settings are invalid.")
    source = str(getattr(pivot, "source", "assumed")).strip()
    objective = str(getattr(pivot, "objective_name", "")).strip()
    if not source:
        raise ValueError("B-axis rotation pivot settings are invalid.")
    return RotationGeometrySnapshot(
        pivot_machine_xy=(x_value, y_value),
        source=source,
        calibration_version=version,
        objective_name=objective,
    )


__all__ = ["RotationGeometrySnapshot", "rotation_geometry_snapshot"]
