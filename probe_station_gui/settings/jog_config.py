"""Pure jog settings normalisation helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JogSettingsDefaults:
    """Default values and constraints for persisted jog settings."""

    mode: str
    linear_distance_mm: float
    rotary_distance_deg: float
    motion_safety_disabled: bool
    manual_axis: str
    manual_axis_distance_mm: float
    manual_axis_mode: str
    manual_axis_feedrate_mm_min: float
    focus_feedrate_mm_min: float
    focus_step_feedrate_mm_min: float
    needles_step_feedrate_mm_min: float
    turntable_feedrate_mm_min: float
    turntable_step_feedrate_mm_min: float
    min_feedrate_mm_min: float
    manual_axes: tuple[str, ...]
    manual_axis_modes: tuple[str, ...]


@dataclass
class JogSettingsConfig:
    """Normalised persisted jog settings."""

    mode: str
    linear_distance_mm: float
    rotary_distance_deg: float
    motion_safety_disabled: bool
    manual_axis: str
    manual_axis_distance_mm: float
    manual_axis_mode: str
    manual_axis_feedrate_mm_min: float
    focus_feedrate_mm_min: float
    focus_step_feedrate_mm_min: float
    needles_step_feedrate_mm_min: float
    turntable_feedrate_mm_min: float
    turntable_step_feedrate_mm_min: float


@dataclass
class JogSettings:
    """Configuration for joystick jog distances."""

    mode: str = "jog"
    linear_distance_mm: float = 25.0
    rotary_distance_deg: float = 5.0
    motion_safety_disabled: bool = False
    manual_axis: str = "A"
    manual_axis_distance_mm: float = 1.0
    manual_axis_mode: str = "G91"
    manual_axis_feedrate_mm_min: float = 1.0
    focus_feedrate_mm_min: float = 1.0
    focus_step_feedrate_mm_min: float = 1.0
    needles_step_feedrate_mm_min: float = 1.0
    turntable_feedrate_mm_min: float = 1.0
    turntable_step_feedrate_mm_min: float = 1.0

    def clone(self) -> "JogSettings":
        """Return a copy of the jog preferences."""

        return JogSettings(
            mode=self.mode,
            linear_distance_mm=self.linear_distance_mm,
            rotary_distance_deg=self.rotary_distance_deg,
            motion_safety_disabled=self.motion_safety_disabled,
            manual_axis=self.manual_axis,
            manual_axis_distance_mm=self.manual_axis_distance_mm,
            manual_axis_mode=self.manual_axis_mode,
            manual_axis_feedrate_mm_min=self.manual_axis_feedrate_mm_min,
            focus_feedrate_mm_min=self.focus_feedrate_mm_min,
            focus_step_feedrate_mm_min=self.focus_step_feedrate_mm_min,
            needles_step_feedrate_mm_min=self.needles_step_feedrate_mm_min,
            turntable_feedrate_mm_min=self.turntable_feedrate_mm_min,
            turntable_step_feedrate_mm_min=self.turntable_step_feedrate_mm_min,
        )

    def to_dict(self) -> dict[str, float | bool | str]:
        """Serialize the jog preferences."""

        return {
            "mode": self.mode,
            "linear_distance_mm": self.linear_distance_mm,
            "rotary_distance_deg": self.rotary_distance_deg,
            "motion_safety_disabled": self.motion_safety_disabled,
            "manual_axis": self.manual_axis,
            "manual_axis_distance_mm": self.manual_axis_distance_mm,
            "manual_axis_mode": self.manual_axis_mode,
            "manual_axis_feedrate_mm_min": self.manual_axis_feedrate_mm_min,
            "focus_feedrate_mm_min": self.focus_feedrate_mm_min,
            "focus_step_feedrate_mm_min": self.focus_step_feedrate_mm_min,
            "needles_step_feedrate_mm_min": self.needles_step_feedrate_mm_min,
            "turntable_feedrate_mm_min": self.turntable_feedrate_mm_min,
            "turntable_step_feedrate_mm_min": self.turntable_step_feedrate_mm_min,
        }


def parse_jog_settings(raw_jog: object, defaults: JogSettingsDefaults) -> JogSettingsConfig:
    """Normalise persisted jog settings supporting legacy field names."""

    mode = defaults.mode
    linear_distance = defaults.linear_distance_mm
    rotary_distance = defaults.rotary_distance_deg
    motion_safety_disabled = defaults.motion_safety_disabled
    manual_axis = defaults.manual_axis
    manual_axis_distance = defaults.manual_axis_distance_mm
    manual_axis_mode = defaults.manual_axis_mode
    manual_axis_feedrate = defaults.manual_axis_feedrate_mm_min
    focus_feedrate = defaults.focus_feedrate_mm_min
    focus_step_feedrate = defaults.focus_step_feedrate_mm_min
    needles_step_feedrate = defaults.needles_step_feedrate_mm_min
    turntable_feedrate = defaults.turntable_feedrate_mm_min
    turntable_step_feedrate = defaults.turntable_step_feedrate_mm_min

    if isinstance(raw_jog, dict):
        legacy_unsafe = _coerce_bool(
            raw_jog.get("unsafe_motion_enabled", False),
            default=False,
        )
        candidate = raw_jog.get("mode", raw_jog.get("control_mode", mode))
        if isinstance(candidate, str):
            candidate = candidate.strip().lower()
            if candidate in {"jog", "step"}:
                mode = candidate
        linear_distance = _coerce_float(
            raw_jog.get("linear_distance_mm", linear_distance),
            default=defaults.linear_distance_mm,
        )
        rotary_distance = _coerce_float(
            raw_jog.get("rotary_distance_deg", rotary_distance),
            default=defaults.rotary_distance_deg,
        )
        motion_safety_disabled = _coerce_bool(
            raw_jog.get("motion_safety_disabled", legacy_unsafe),
            default=defaults.motion_safety_disabled,
        )
        candidate = raw_jog.get("manual_axis", manual_axis)
        if isinstance(candidate, str):
            manual_axis = candidate.strip().upper() or manual_axis
        candidate = raw_jog.get("manual_axis_mode", manual_axis_mode)
        if isinstance(candidate, str):
            manual_axis_mode = candidate.strip().upper() or manual_axis_mode
        manual_axis_distance = _coerce_float(
            raw_jog.get("manual_axis_distance_mm", manual_axis_distance),
            default=defaults.manual_axis_distance_mm,
        )
        manual_axis_feedrate = _coerce_float(
            raw_jog.get("manual_axis_feedrate_mm_min", manual_axis_feedrate),
            default=defaults.manual_axis_feedrate_mm_min,
        )
        focus_feedrate = _coerce_float(
            raw_jog.get("focus_feedrate_mm_min", focus_feedrate),
            default=defaults.focus_feedrate_mm_min,
        )
        focus_step_feedrate = _coerce_float(
            raw_jog.get(
                "focus_step_feedrate_mm_min",
                raw_jog.get("focus_feedrate_mm_min", focus_step_feedrate),
            ),
            default=defaults.focus_step_feedrate_mm_min,
        )
        needles_step_feedrate = _coerce_float(
            raw_jog.get("needles_step_feedrate_mm_min", needles_step_feedrate),
            default=defaults.needles_step_feedrate_mm_min,
        )
        turntable_feedrate = _coerce_float(
            raw_jog.get("turntable_feedrate_mm_min", turntable_feedrate),
            default=defaults.turntable_feedrate_mm_min,
        )
        turntable_step_feedrate = _coerce_float(
            raw_jog.get(
                "turntable_step_feedrate_mm_min",
                raw_jog.get("turntable_feedrate_mm_min", turntable_step_feedrate),
            ),
            default=defaults.turntable_step_feedrate_mm_min,
        )

    if linear_distance <= 0:
        linear_distance = defaults.linear_distance_mm
    if rotary_distance <= 0:
        rotary_distance = defaults.rotary_distance_deg
    if manual_axis not in defaults.manual_axes:
        manual_axis = defaults.manual_axis
    if manual_axis_distance <= 0:
        manual_axis_distance = defaults.manual_axis_distance_mm
    if manual_axis_mode not in defaults.manual_axis_modes:
        manual_axis_mode = defaults.manual_axis_mode
    manual_axis_feedrate = _normalise_feedrate(
        manual_axis_feedrate,
        default=defaults.manual_axis_feedrate_mm_min,
        minimum=defaults.min_feedrate_mm_min,
    )
    focus_feedrate = _normalise_feedrate(
        focus_feedrate,
        default=defaults.focus_feedrate_mm_min,
        minimum=defaults.min_feedrate_mm_min,
    )
    focus_step_feedrate = _normalise_feedrate(
        focus_step_feedrate,
        default=defaults.focus_step_feedrate_mm_min,
        minimum=defaults.min_feedrate_mm_min,
    )
    needles_step_feedrate = _normalise_feedrate(
        needles_step_feedrate,
        default=defaults.needles_step_feedrate_mm_min,
        minimum=defaults.min_feedrate_mm_min,
    )
    turntable_feedrate = _normalise_feedrate(
        turntable_feedrate,
        default=defaults.turntable_feedrate_mm_min,
        minimum=defaults.min_feedrate_mm_min,
    )
    turntable_step_feedrate = _normalise_feedrate(
        turntable_step_feedrate,
        default=defaults.turntable_step_feedrate_mm_min,
        minimum=defaults.min_feedrate_mm_min,
    )
    return JogSettingsConfig(
        mode=mode,
        linear_distance_mm=linear_distance,
        rotary_distance_deg=rotary_distance,
        motion_safety_disabled=motion_safety_disabled,
        manual_axis=manual_axis,
        manual_axis_distance_mm=manual_axis_distance,
        manual_axis_mode=manual_axis_mode,
        manual_axis_feedrate_mm_min=manual_axis_feedrate,
        focus_feedrate_mm_min=focus_feedrate,
        focus_step_feedrate_mm_min=focus_step_feedrate,
        needles_step_feedrate_mm_min=needles_step_feedrate,
        turntable_feedrate_mm_min=turntable_feedrate,
        turntable_step_feedrate_mm_min=turntable_step_feedrate,
    )


def _coerce_bool(value: object, *, default: bool) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "off", "no"}
    if value is None:
        return default
    return bool(value)


def _coerce_float(value: object, *, default: float) -> float:
    try:
        if isinstance(value, (int, float, str)):
            return float(value)
    except (TypeError, ValueError):
        pass
    return default


def _normalise_feedrate(value: float, *, default: float, minimum: float) -> float:
    if value <= 0:
        value = default
    return max(minimum, value)
