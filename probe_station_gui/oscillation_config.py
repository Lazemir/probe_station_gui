"""Pure oscillation settings normalisation helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OscillationSettingsDefaults:
    """Default values and constraints for persisted oscillation settings."""

    mode: str
    amplitude_mm: float
    feedrate_mm_min: float
    turns_per_sweep: float
    modes: tuple[str, ...] = ("X", "Y", "SPIRAL")


@dataclass
class OscillationSettingsConfig:
    """Normalised persisted oscillation settings."""

    mode: str
    amplitude_mm: float
    feedrate_mm_min: float
    turns_per_sweep: float


def parse_oscillation_settings(
    raw_oscillation: object,
    defaults: OscillationSettingsDefaults,
) -> OscillationSettingsConfig:
    """Normalise persisted oscillation-panel settings."""

    mode = defaults.mode
    amplitude_mm = defaults.amplitude_mm
    feedrate_mm_min = defaults.feedrate_mm_min
    turns_per_sweep = defaults.turns_per_sweep
    if isinstance(raw_oscillation, dict):
        mode_candidate = raw_oscillation.get("mode", mode)
        if isinstance(mode_candidate, str):
            mode = mode_candidate.strip().upper() or mode
        amplitude_mm = _coerce_float(
            raw_oscillation.get("amplitude_mm", amplitude_mm),
            default=defaults.amplitude_mm,
        )
        feedrate_mm_min = _coerce_float(
            raw_oscillation.get("feedrate_mm_min", feedrate_mm_min),
            default=defaults.feedrate_mm_min,
        )
        turns_per_sweep = _coerce_float(
            raw_oscillation.get("turns_per_sweep", turns_per_sweep),
            default=defaults.turns_per_sweep,
        )
    if mode not in defaults.modes:
        mode = defaults.mode
    if amplitude_mm <= 0:
        amplitude_mm = defaults.amplitude_mm
    if feedrate_mm_min <= 0:
        feedrate_mm_min = defaults.feedrate_mm_min
    if turns_per_sweep <= 0:
        turns_per_sweep = defaults.turns_per_sweep
    return OscillationSettingsConfig(
        mode=mode,
        amplitude_mm=amplitude_mm,
        feedrate_mm_min=feedrate_mm_min,
        turns_per_sweep=turns_per_sweep,
    )


def _coerce_float(value: object, *, default: float) -> float:
    try:
        if isinstance(value, (int, float, str)):
            return float(value)
    except (TypeError, ValueError):
        pass
    return default
