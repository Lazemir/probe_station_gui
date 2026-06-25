"""Pure objective calibration settings normalisation helpers."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field


OBJECTIVE_NAMES: tuple[str, ...] = ("X5", "X10", "X20", "X50")
DEFAULT_ACTIVE_OBJECTIVE = "X5"
OBJECTIVE_DEFAULTS: dict[str, dict[str, float]] = {
    "X5": {
        "magnification": 5.0,
        "autofocus_range_mm": 1.0,
        "autofocus_fine_step_mm": 0.02,
    },
    "X10": {
        "magnification": 10.0,
        "autofocus_range_mm": 0.6,
        "autofocus_fine_step_mm": 0.01,
    },
    "X20": {
        "magnification": 20.0,
        "autofocus_range_mm": 0.35,
        "autofocus_fine_step_mm": 0.005,
    },
    "X50": {
        "magnification": 50.0,
        "autofocus_range_mm": 0.15,
        "autofocus_fine_step_mm": 0.002,
    },
}


def normalize_objective_name(value: object) -> str:
    """Return a compact persisted objective profile name."""

    if not isinstance(value, str):
        return ""
    name = re.sub(r"\s+", "", value.strip().upper())
    if not name or len(name) > 32:
        return ""
    if not re.fullmatch(r"[A-Z0-9_.-]+", name):
        return ""
    return name


def ordered_objective_names(
    profiles: object,
) -> list[str]:
    """Return objective names with built-in profiles first and custom profiles after."""

    if not isinstance(profiles, dict):
        return list(OBJECTIVE_NAMES)
    normalized: list[str] = []
    for name in OBJECTIVE_NAMES:
        if name in profiles:
            normalized.append(name)
    for raw_name in profiles:
        name = normalize_objective_name(raw_name)
        if name and name not in normalized:
            normalized.append(name)
    return normalized or [DEFAULT_ACTIVE_OBJECTIVE]


@dataclass
class ObjectiveCalibrationSettings:
    """Per-objective optical offsets and camera-stage calibration."""

    name: str = DEFAULT_ACTIVE_OBJECTIVE
    magnification: float = 5.0
    xy_offset_x_mm: float = 0.0
    xy_offset_y_mm: float = 0.0
    xy_offset_configured: bool = False
    z_offset_mm: float = 0.0
    z_offset_configured: bool = False
    pixels_to_mm: list[list[float]] = field(default_factory=list)
    xy_calibration_configured: bool = False
    autofocus_range_mm: float = 1.0
    autofocus_fine_step_mm: float = 0.02

    def clone(self) -> "ObjectiveCalibrationSettings":
        """Return a copy of the objective calibration."""

        return ObjectiveCalibrationSettings(
            name=self.name,
            magnification=self.magnification,
            xy_offset_x_mm=self.xy_offset_x_mm,
            xy_offset_y_mm=self.xy_offset_y_mm,
            xy_offset_configured=self.xy_offset_configured,
            z_offset_mm=self.z_offset_mm,
            z_offset_configured=self.z_offset_configured,
            pixels_to_mm=[list(row) for row in self.pixels_to_mm],
            xy_calibration_configured=self.xy_calibration_configured,
            autofocus_range_mm=self.autofocus_range_mm,
            autofocus_fine_step_mm=self.autofocus_fine_step_mm,
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize the objective calibration."""

        return {
            "name": self.name,
            "magnification": self.magnification,
            "xy_offset_x_mm": self.xy_offset_x_mm,
            "xy_offset_y_mm": self.xy_offset_y_mm,
            "xy_offset_configured": self.xy_offset_configured,
            "z_offset_mm": self.z_offset_mm,
            "z_offset_configured": self.z_offset_configured,
            "pixels_to_mm": [list(row) for row in self.pixels_to_mm],
            "xy_calibration_configured": self.xy_calibration_configured,
            "autofocus_range_mm": self.autofocus_range_mm,
            "autofocus_fine_step_mm": self.autofocus_fine_step_mm,
        }


def default_objective(name: str) -> ObjectiveCalibrationSettings:
    """Return default calibration parameters for one objective."""

    defaults = OBJECTIVE_DEFAULTS.get(name, OBJECTIVE_DEFAULTS[DEFAULT_ACTIVE_OBJECTIVE])
    return ObjectiveCalibrationSettings(
        name=name,
        magnification=defaults["magnification"],
        autofocus_range_mm=defaults["autofocus_range_mm"],
        autofocus_fine_step_mm=defaults["autofocus_fine_step_mm"],
    )


def default_objectives() -> dict[str, ObjectiveCalibrationSettings]:
    """Return the default objective profile map."""

    return {name: default_objective(name) for name in OBJECTIVE_NAMES}


@dataclass
class ObjectivesSettings:
    """Collection of all objective-specific optical calibration values."""

    active_name: str = DEFAULT_ACTIVE_OBJECTIVE
    apply_offsets_on_change: bool = True
    objectives: dict[str, ObjectiveCalibrationSettings] = field(
        default_factory=default_objectives
    )

    def clone(self) -> "ObjectivesSettings":
        """Return a deep copy of objective settings."""

        return ObjectivesSettings(
            active_name=self.active_name,
            apply_offsets_on_change=self.apply_offsets_on_change,
            objectives={key: value.clone() for key, value in self.objectives.items()},
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize objective settings."""

        return {
            "active_name": self.active_name,
            "apply_offsets_on_change": self.apply_offsets_on_change,
            "objectives": {
                key: value.to_dict() for key, value in self.objectives.items()
            },
        }


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
