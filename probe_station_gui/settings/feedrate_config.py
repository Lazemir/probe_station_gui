"""Pure feedrate preset normalisation helpers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass
class FeedrateGroupConfig:
    """Normalised feedrate presets and default value."""

    presets: list[float]
    default: float


@dataclass
class FeedrateGroup:
    """Collection of presets and a default value for a motion family."""

    presets: list[float] = field(default_factory=list)
    default: float = 1.0

    def clone(self) -> "FeedrateGroup":
        """Return a deep copy of the feedrate group."""

        return FeedrateGroup(presets=list(self.presets), default=self.default)


@dataclass
class FeedrateSettings:
    """Configuration for linear and rotary feed rates."""

    linear: FeedrateGroup = field(default_factory=FeedrateGroup)
    rotary: FeedrateGroup = field(default_factory=FeedrateGroup)

    def clone(self) -> "FeedrateSettings":
        """Return a deep copy of the feedrate configuration."""

        return FeedrateSettings(
            linear=self.linear.clone(),
            rotary=self.rotary.clone(),
        )


def parse_feedrate_groups(
    raw_feedrates: object,
    legacy_presets: object,
    *,
    linear_group: str,
    rotary_group: str,
    linear_defaults: tuple[float, ...],
    rotary_defaults: tuple[float, ...],
    default_feedrate: float,
    min_feedrate: float,
) -> tuple[FeedrateGroupConfig, FeedrateGroupConfig]:
    """Normalise feedrate data supporting current and legacy layouts."""

    presets_fallback = parse_feedrate_list(
        legacy_presets,
        fallback=linear_defaults,
        min_feedrate=min_feedrate,
    )
    legacy_raw_present = bool(legacy_presets)
    rotary_fallback_defaults = (
        tuple(presets_fallback)
        if legacy_raw_present
        else tuple(rotary_defaults)
    )
    if not isinstance(raw_feedrates, dict):
        linear = normalise_feedrate_group(
            FeedrateGroupConfig(
                presets=presets_fallback,
                default=default_feedrate,
            ),
            fallback=linear_defaults,
            default_feedrate=default_feedrate,
            min_feedrate=min_feedrate,
        )
        rotary = normalise_feedrate_group(
            FeedrateGroupConfig(
                presets=list(rotary_fallback_defaults),
                default=default_feedrate,
            ),
            fallback=rotary_defaults,
            default_feedrate=default_feedrate,
            min_feedrate=min_feedrate,
        )
        return linear, rotary

    linear_config = normalise_feedrate_group(
        feedrate_group_from_raw(
            raw_feedrates.get(linear_group),
            fallback=linear_defaults,
            default_feedrate=default_feedrate,
            min_feedrate=min_feedrate,
        ),
        fallback=linear_defaults,
        default_feedrate=default_feedrate,
        min_feedrate=min_feedrate,
    )
    rotary_config = normalise_feedrate_group(
        feedrate_group_from_raw(
            raw_feedrates.get(rotary_group),
            fallback=rotary_fallback_defaults,
            default_feedrate=default_feedrate,
            min_feedrate=min_feedrate,
        ),
        fallback=rotary_defaults,
        default_feedrate=default_feedrate,
        min_feedrate=min_feedrate,
    )
    return linear_config, rotary_config


def feedrate_group_from_config(config: FeedrateGroupConfig) -> FeedrateGroup:
    """Convert normalised feedrate data to mutable settings."""

    return FeedrateGroup(presets=list(config.presets), default=config.default)


def normalise_feedrate_settings(
    settings: FeedrateSettings,
    *,
    linear_defaults: tuple[float, ...],
    rotary_defaults: tuple[float, ...],
    default_feedrate: float,
    min_feedrate: float,
) -> FeedrateSettings:
    """Normalise both feedrate groups in a settings object."""

    return FeedrateSettings(
        linear=feedrate_group_from_config(
            normalise_feedrate_group(
                FeedrateGroupConfig(
                    presets=list(settings.linear.presets),
                    default=settings.linear.default,
                ),
                fallback=linear_defaults,
                default_feedrate=default_feedrate,
                min_feedrate=min_feedrate,
            )
        ),
        rotary=feedrate_group_from_config(
            normalise_feedrate_group(
                FeedrateGroupConfig(
                    presets=list(settings.rotary.presets),
                    default=settings.rotary.default,
                ),
                fallback=rotary_defaults,
                default_feedrate=default_feedrate,
                min_feedrate=min_feedrate,
            )
        ),
    )


def feedrate_group_from_raw(
    raw_group: object,
    *,
    fallback: tuple[float, ...],
    default_feedrate: float,
    min_feedrate: float,
) -> FeedrateGroupConfig:
    """Build feedrate group data from persisted values."""

    presets: list[float]
    default = default_feedrate
    if isinstance(raw_group, dict):
        presets = parse_feedrate_list(
            raw_group.get("presets"),
            fallback=fallback,
            min_feedrate=min_feedrate,
        )
        default_raw = raw_group.get("default")
        try:
            if isinstance(default_raw, (int, float, str)):
                default = float(default_raw)
        except (TypeError, ValueError):
            default = default_feedrate
    else:
        presets = list(fallback)
    return FeedrateGroupConfig(presets=presets, default=default)


def parse_feedrate_list(
    raw_presets: object,
    *,
    fallback: tuple[float, ...],
    min_feedrate: float,
) -> list[float]:
    """Normalise a preset list to positive unique floats preserving order."""

    parsed: list[float] = []
    seen: set[float] = set()
    if isinstance(raw_presets, Iterable) and not isinstance(raw_presets, (str, bytes)):
        for value in raw_presets:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number <= 0:
                continue
            number = max(min_feedrate, number)
            key = round(number, 9)
            if key in seen:
                continue
            seen.add(key)
            parsed.append(number)
    if not parsed:
        parsed = list(fallback)
    return parsed


def normalise_feedrate_group(
    group: FeedrateGroupConfig,
    *,
    fallback: tuple[float, ...],
    default_feedrate: float,
    min_feedrate: float,
) -> FeedrateGroupConfig:
    """Ensure the feedrate group contains valid presets and a valid default."""

    presets = parse_feedrate_list(
        group.presets,
        fallback=fallback,
        min_feedrate=min_feedrate,
    )
    presets.sort()
    default_value = group.default if group.default > 0 else default_feedrate
    default_value = max(min_feedrate, default_value)
    default_value = select_feedrate_default(
        default_value,
        default_feedrate=default_feedrate,
        min_feedrate=min_feedrate,
    )
    return FeedrateGroupConfig(presets=presets, default=default_value)


def select_feedrate_default(
    candidate: object,
    *,
    default_feedrate: float,
    min_feedrate: float,
) -> float:
    """Choose a positive default value without forcing it into presets."""

    try:
        candidate_value = float(candidate)
    except (TypeError, ValueError):
        candidate_value = default_feedrate

    if candidate_value <= 0:
        candidate_value = default_feedrate
    return max(min_feedrate, candidate_value)
