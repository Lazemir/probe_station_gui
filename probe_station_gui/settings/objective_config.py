"""Pure objective calibration settings normalisation helpers."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from probe_station_gui.settings.value_parsing import (
    coerce_bool,
    finite_float,
    positive_float,
)


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


def parse_objective_profile(
    name: str,
    raw_profile: object,
) -> ObjectiveCalibrationSettings:
    """Normalise one persisted objective profile."""

    defaults = default_objective(name)
    if not isinstance(raw_profile, dict):
        return defaults
    matrix = parse_pixels_to_mm_matrix(raw_profile.get("pixels_to_mm"))
    xy_configured = coerce_bool(
        raw_profile.get("xy_calibration_configured", bool(matrix)),
        default=bool(matrix),
    )
    if not matrix:
        xy_configured = False
    return ObjectiveCalibrationSettings(
        name=name,
        magnification=positive_float(
            raw_profile.get("magnification", defaults.magnification),
            default=defaults.magnification,
        ),
        xy_offset_x_mm=finite_float(
            raw_profile.get("xy_offset_x_mm", defaults.xy_offset_x_mm),
            default=defaults.xy_offset_x_mm,
        ),
        xy_offset_y_mm=finite_float(
            raw_profile.get("xy_offset_y_mm", defaults.xy_offset_y_mm),
            default=defaults.xy_offset_y_mm,
        ),
        xy_offset_configured=coerce_bool(
            raw_profile.get(
                "xy_offset_configured",
                defaults.xy_offset_configured,
            ),
            default=defaults.xy_offset_configured,
        ),
        z_offset_mm=finite_float(
            raw_profile.get("z_offset_mm", defaults.z_offset_mm),
            default=defaults.z_offset_mm,
        ),
        z_offset_configured=coerce_bool(
            raw_profile.get(
                "z_offset_configured",
                defaults.z_offset_configured,
            ),
            default=defaults.z_offset_configured,
        ),
        pixels_to_mm=matrix,
        xy_calibration_configured=xy_configured,
        autofocus_range_mm=positive_float(
            raw_profile.get(
                "autofocus_range_mm",
                defaults.autofocus_range_mm,
            ),
            default=defaults.autofocus_range_mm,
        ),
        autofocus_fine_step_mm=positive_float(
            raw_profile.get(
                "autofocus_fine_step_mm",
                defaults.autofocus_fine_step_mm,
            ),
            default=defaults.autofocus_fine_step_mm,
        ),
    )


def parse_objectives_settings(raw_objectives: object) -> ObjectivesSettings:
    """Normalise persisted objective profiles."""

    active_name = DEFAULT_ACTIVE_OBJECTIVE
    apply_offsets_on_change = True
    raw_profiles = None
    if isinstance(raw_objectives, dict):
        normalized_active = normalize_objective_name(
            raw_objectives.get("active_name", active_name)
        )
        if normalized_active:
            active_name = normalized_active
        apply_offsets_on_change = coerce_bool(
            raw_objectives.get(
                "apply_offsets_on_change",
                apply_offsets_on_change,
            ),
            default=apply_offsets_on_change,
        )
        raw_profiles = raw_objectives.get("objectives")

    profile_map = raw_profiles if isinstance(raw_profiles, dict) else {}
    names = _objective_names_for_parse(profile_map, active_name)
    if active_name not in names and profile_map:
        active_name = names[0]
    elif active_name not in names:
        names.append(active_name)
    profiles = {
        name: parse_objective_profile(
            name,
            _raw_objective_profile(profile_map, name),
        )
        for name in names
    }
    return ObjectivesSettings(
        active_name=active_name,
        apply_offsets_on_change=apply_offsets_on_change,
        objectives=profiles,
    )


def _objective_names_for_parse(
    profile_map: dict[object, object],
    active_name: str,
) -> list[str]:
    names: list[str] = []
    for raw_name in profile_map:
        name = normalize_objective_name(raw_name)
        if name and name not in names:
            names.append(name)
    if names:
        return names
    return list(OBJECTIVE_NAMES) or [active_name]


def _raw_objective_profile(
    profile_map: dict[object, object],
    name: str,
) -> object:
    for raw_key, candidate in profile_map.items():
        if normalize_objective_name(raw_key) == name:
            return candidate
    return None
