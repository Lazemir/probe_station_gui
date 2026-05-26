"""Helpers for objective-specific optical XY offsets."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from .settings_manager import normalize_objective_name, ordered_objective_names


Point2D = tuple[float, float]


@dataclass(frozen=True)
class ObjectiveOffsetReference:
    """A centered physical feature used as an objective offset reference."""

    objective_name: str
    stage_xy: Point2D
    offset_xy: Point2D


def base_objective_name(profiles: Mapping[str, object] | object) -> str:
    """Return the profile with the smallest positive magnification."""

    if not isinstance(profiles, Mapping):
        return "X5"
    ordered_names = ordered_objective_names(profiles)
    best_name = ordered_names[0] if ordered_names else "X5"
    best_magnification = math.inf
    for raw_name in ordered_names:
        profile = profiles.get(raw_name)
        try:
            magnification = float(getattr(profile, "magnification"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(magnification) and magnification > 0.0:
            if magnification < best_magnification:
                best_name = normalize_objective_name(raw_name) or raw_name
                best_magnification = magnification
    return normalize_objective_name(best_name) or "X5"


def objective_xy_offset(
    profiles: Mapping[str, object] | object,
    objective_name: str,
) -> Point2D:
    """Return an objective optical offset in raw stage millimetres.

    Offsets are stored relative to the lowest-magnification objective. The base
    objective is therefore always treated as zero even if its settings have not
    been explicitly saved yet.
    """

    if not isinstance(profiles, Mapping):
        return (0.0, 0.0)
    name = normalize_objective_name(objective_name)
    if not name:
        return (0.0, 0.0)
    if name == base_objective_name(profiles):
        return (0.0, 0.0)
    profile = profiles.get(name)
    if profile is None or not bool(getattr(profile, "xy_offset_configured", False)):
        return (0.0, 0.0)
    try:
        x_value = float(getattr(profile, "xy_offset_x_mm"))
        y_value = float(getattr(profile, "xy_offset_y_mm"))
    except (TypeError, ValueError):
        return (0.0, 0.0)
    if not math.isfinite(x_value) or not math.isfinite(y_value):
        return (0.0, 0.0)
    return (x_value, y_value)


def objective_xy_offset_is_configured(
    profiles: Mapping[str, object] | object,
    objective_name: str,
) -> bool:
    """Return whether an objective has a usable optical offset."""

    if not isinstance(profiles, Mapping):
        return False
    name = normalize_objective_name(objective_name)
    if not name:
        return False
    if name == base_objective_name(profiles):
        return True
    profile = profiles.get(name)
    return bool(profile is not None and getattr(profile, "xy_offset_configured", False))


def raw_stage_to_camera_stage(raw_stage_xy: Point2D, offset_xy: Point2D) -> Point2D:
    """Convert physical stage XY to the base-objective optical-center coordinate."""

    return (
        float(raw_stage_xy[0]) - float(offset_xy[0]),
        float(raw_stage_xy[1]) - float(offset_xy[1]),
    )


def camera_stage_to_raw_stage(camera_stage_xy: Point2D, offset_xy: Point2D) -> Point2D:
    """Convert base-objective optical-center coordinate to physical stage XY."""

    return (
        float(camera_stage_xy[0]) + float(offset_xy[0]),
        float(camera_stage_xy[1]) + float(offset_xy[1]),
    )


def calibrated_objective_offset(
    reference: ObjectiveOffsetReference,
    target_stage_xy: Point2D,
) -> Point2D:
    """Compute target objective offset from the same centered feature."""

    return (
        float(target_stage_xy[0]) - float(reference.stage_xy[0])
        + float(reference.offset_xy[0]),
        float(target_stage_xy[1]) - float(reference.stage_xy[1])
        + float(reference.offset_xy[1]),
    )


__all__ = [
    "ObjectiveOffsetReference",
    "base_objective_name",
    "calibrated_objective_offset",
    "camera_stage_to_raw_stage",
    "objective_xy_offset",
    "objective_xy_offset_is_configured",
    "raw_stage_to_camera_stage",
]
