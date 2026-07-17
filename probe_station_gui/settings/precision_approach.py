"""Backlash-aware final approach settings for stage axes."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from probe_station_gui.settings.value_parsing import coerce_bool, finite_float


PRECISION_APPROACH_AXES: tuple[str, ...] = ("X", "Y", "Z", "A", "B", "C")


@dataclass(frozen=True)
class PrecisionApproachProfile:
    """Final approach preference for one calibrated display axis."""

    enabled: bool = False
    backlash: float = 0.0
    final_direction: int = 1

    def __post_init__(self) -> None:
        if not math.isfinite(self.backlash) or self.backlash < 0.0:
            raise ValueError("backlash must be finite and non-negative")
        if self.final_direction not in (-1, 1):
            raise ValueError("final_direction must be -1 or +1")

    def to_dict(self) -> dict[str, bool | float | int]:
        return {
            "enabled": self.enabled,
            "backlash": self.backlash,
            "final_direction": self.final_direction,
        }


def precision_profile_is_effective(profile: PrecisionApproachProfile) -> bool:
    """Return whether a profile can perform a non-zero backlash approach."""

    return bool(profile.enabled and profile.backlash > 0.0)


def default_precision_approach_profiles() -> dict[str, PrecisionApproachProfile]:
    profiles = {axis: PrecisionApproachProfile() for axis in PRECISION_APPROACH_AXES}
    profiles["Z"] = PrecisionApproachProfile(True, 0.03, 1)
    profiles["A"] = PrecisionApproachProfile(False, 0.0, -1)
    return profiles


@dataclass
class PrecisionApproachSettings:
    """Collection of final approach profiles in stable stage-axis order."""

    profiles: dict[str, PrecisionApproachProfile] = field(
        default_factory=default_precision_approach_profiles
    )

    def clone(self) -> "PrecisionApproachSettings":
        return PrecisionApproachSettings(profiles=dict(self.profiles))

    def to_dict(self) -> dict[str, dict[str, bool | float | int]]:
        return {
            axis: self.profiles[axis].to_dict()
            for axis in PRECISION_APPROACH_AXES
        }

    def fingerprint_payload(self) -> tuple[tuple[str, bool, float, int], ...]:
        return tuple(
            (
                axis,
                self.profiles[axis].enabled,
                self.profiles[axis].backlash,
                self.profiles[axis].final_direction,
            )
            for axis in PRECISION_APPROACH_AXES
        )


def parse_precision_approach_settings(raw: object) -> PrecisionApproachSettings:
    """Parse a partial persisted mapping while retaining per-axis defaults."""

    defaults = default_precision_approach_profiles()
    if not isinstance(raw, dict):
        return PrecisionApproachSettings(defaults)

    profiles: dict[str, PrecisionApproachProfile] = {}
    for axis in PRECISION_APPROACH_AXES:
        default = defaults[axis]
        value = raw.get(axis)
        if not isinstance(value, dict):
            profiles[axis] = default
            continue
        backlash = finite_float(value.get("backlash"), default=default.backlash)
        if backlash < 0.0:
            backlash = default.backlash
        profiles[axis] = PrecisionApproachProfile(
            enabled=coerce_bool(value.get("enabled"), default=default.enabled),
            backlash=backlash,
            final_direction=_parse_final_direction(
                value.get("final_direction"),
                default=default.final_direction,
            ),
        )
    return PrecisionApproachSettings(profiles)


def _parse_final_direction(value: object, *, default: int) -> int:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized == "+":
            return 1
        if normalized == "-":
            return -1
        try:
            value = int(normalized)
        except ValueError:
            return default
    if value in (-1, 1):
        return int(value)
    return default


__all__ = [
    "PRECISION_APPROACH_AXES",
    "PrecisionApproachProfile",
    "PrecisionApproachSettings",
    "default_precision_approach_profiles",
    "parse_precision_approach_settings",
    "precision_profile_is_effective",
]
