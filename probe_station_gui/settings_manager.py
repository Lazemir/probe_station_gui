"""Settings loading and persistence for the probe station GUI."""

from __future__ import annotations

import json
import logging
import math
import os
import platform
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from probe_station_gui.axis_calibration_config import (
    AxisACalibrationConfig,
    AxisZCalibrationConfig,
    parse_axis_a_calibration,
    parse_axis_z_calibration,
)
from probe_station_gui.feedrate_config import (
    FeedrateGroupConfig,
    feedrate_group_from_raw,
    normalise_feedrate_group,
    parse_feedrate_groups,
    parse_feedrate_list,
    select_feedrate_default,
)
from probe_station_gui.jog_config import JogSettingsDefaults, parse_jog_settings
from probe_station_gui.logging_config import configure_logging
from probe_station_gui.needle_calibration_config import (
    NeedleCalibrationDefaults,
    parse_needle_calibration_settings,
    parse_saved_stage_position,
)
from probe_station_gui.objective_config import parse_pixels_to_mm_matrix
from probe_station_gui.oscillation_config import (
    OscillationSettingsDefaults,
    parse_oscillation_settings,
)
from probe_station_gui.telegram_notifications import (
    load_global_bot_token,
    save_global_bot_token,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ControlAction:
    """Describe a logical control action exposed in the UI."""

    key: str
    axis: str
    direction: int
    label: str
    default_qt_key: int = 0
    default_text: str = ""
    default_modifiers: int = 0


CONTROL_ACTIONS: tuple[ControlAction, ...] = (
    ControlAction("move_y_positive", "Y", 1, "Move Up"),
    ControlAction("move_y_negative", "Y", -1, "Move Down"),
    ControlAction("move_x_negative", "X", -1, "Move Left"),
    ControlAction("move_x_positive", "X", 1, "Move Right"),
    ControlAction("toggle_jog_step", "", 0, "Toggle Jog/Step", 74, "j"),
)

FLUIDNC_AXIS_NAMES: tuple[str, ...] = ("X", "Y", "Z", "A", "B", "C")


def parse_fluidnc_axis_max_feedrates(lines: Iterable[str]) -> dict[str, float]:
    """Extract per-axis max_rate_mm_per_min values from a FluidNC config dump."""

    axis_headers = {f"{axis}:" for axis in FLUIDNC_AXIS_NAMES}
    rates: dict[str, float] = {}
    current_axis: str | None = None
    for line in lines:
        stripped = str(line).strip()
        if not stripped or stripped.startswith("#"):
            continue
        upper = stripped.upper()
        if upper in axis_headers:
            current_axis = upper[0]
            continue
        if current_axis is None:
            continue
        if not stripped.lower().startswith("max_rate_mm_per_min:"):
            continue
        raw_value = stripped.split(":", 1)[1].strip()
        try:
            value = float(raw_value)
        except ValueError:
            continue
        if math.isfinite(value) and value > 0.0:
            rates[current_axis] = value
    return rates

WORK_COORDINATE_SYSTEMS: tuple[str, ...] = (
    "G54",
    "G55",
    "G56",
    "G57",
    "G58",
    "G59",
    "G59.1",
    "G59.2",
    "G59.3",
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

LCR_MEASUREMENT_FUNCTIONS: tuple[str, ...] = (
    "Cs-Rs",
    "Cs-D",
    "Cp-Rp",
    "Cp-D",
    "Lp-Rp",
    "Lp-Q",
    "Ls-Rs",
    "Ls-Q",
    "Rs-Q",
    "Rp-Q",
    "R-X",
    "DCR",
    "Z-thr",
    "Z-thd",
    "Z-D",
    "Z-Q",
)
LCR_RANGE_MODES: tuple[str, ...] = ("HOLD", "AUTO")
LCR_LEVEL_MODES: tuple[str, ...] = ("VOLTAGE", "CURRENT")
LCR_APERTURE_RATES: tuple[str, ...] = ("FAST", "MED", "SLOW")
LCR_TRIGGER_SOURCES: tuple[str, ...] = ("INT", "MAN", "EXT", "BUS")
LCR_SOURCE_RESISTANCES_OHM: tuple[int, ...] = (30, 50, 100)
LCR_MONITOR_PARAMETERS: tuple[str, ...] = (
    "OFF",
    "Z",
    "D",
    "Q",
    "THR",
    "THD",
    "R",
    "X",
    "G",
    "B",
    "Y",
    "ABS",
    "PER",
    "VAC",
    "IAC",
)
LCR_METER_TYPE_GWINSTEK = "gwinstek_lcr_76200"
LCR_METER_TYPE_KEITHLEY = "keithley_2400_2182a"
LCR_METER_TYPES: tuple[str, ...] = (
    LCR_METER_TYPE_GWINSTEK,
    LCR_METER_TYPE_KEITHLEY,
)
LCR_METER_TYPE_LABELS: dict[str, str] = {
    LCR_METER_TYPE_GWINSTEK: "GW Instek LCR-76200",
    LCR_METER_TYPE_KEITHLEY: "Keithley 2400 + 2182A",
}

TELEGRAM_ALERT_TYPES: tuple[tuple[str, str], ...] = (
    ("route_attention", "Route needs attention"),
    ("route_started", "Route measurement started"),
    ("route_completed", "Route measurement complete"),
    ("route_failed", "Route measurement stopped or failed"),
    ("contact_seek_failed", "Contact seek failed"),
    ("camera_error", "Camera error"),
)


def default_telegram_alerts() -> Dict[str, bool]:
    """Return default Telegram alert selections."""

    return {key: True for key, _label in TELEGRAM_ALERT_TYPES}


@dataclass(eq=True, frozen=True)
class KeyBinding:
    """Representation of a single captured key binding."""

    qt_key: int
    modifiers: int = 0
    native_scan_code: int = 0
    text: str = ""

    def to_dict(self) -> dict[str, int | str]:
        """Serialize the binding for persistence."""

        return {
            "qt_key": self.qt_key,
            "modifiers": self.modifiers,
            "native_scan_code": self.native_scan_code,
            "text": self.text,
        }

    @staticmethod
    def from_dict(data: dict) -> "KeyBinding":
        """Deserialize a binding from JSON data."""

        return KeyBinding(
            qt_key=int(data.get("qt_key", 0)),
            modifiers=int(data.get("modifiers", 0)),
            native_scan_code=int(data.get("native_scan_code", 0)),
            text=str(data.get("text", "")),
        )


@dataclass
class LoggingSettings:
    """Configuration for application logging."""

    level: str = "INFO"
    file: str = ""

    def clone(self) -> "LoggingSettings":
        """Return a copy of the logging preferences."""

        return LoggingSettings(level=self.level, file=self.file)

    def to_dict(self) -> dict[str, str]:
        """Serialize the logging preferences."""

        return {"level": self.level, "file": self.file}


@dataclass
class ApiSettings:
    """Configuration for the local FastAPI control surface."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765

    def clone(self) -> "ApiSettings":
        """Return a copy of the API preferences."""

        return ApiSettings(
            enabled=self.enabled,
            host=self.host,
            port=self.port,
        )

    def to_dict(self) -> dict[str, bool | int | float | str]:
        """Serialize the API preferences."""

        return {
            "enabled": self.enabled,
            "host": self.host,
            "port": self.port,
        }


@dataclass
class TelegramSettings:
    """Configuration for Telegram notifications."""

    enabled: bool = False
    bot_token: str = ""
    bot_username: str = ""
    chat_id: str = ""
    chat_title: str = ""
    linked_at_utc: str = ""
    alerts: Dict[str, bool] = field(default_factory=default_telegram_alerts)

    def clone(self) -> "TelegramSettings":
        """Return a copy of the Telegram notification preferences."""

        return TelegramSettings(
            enabled=self.enabled,
            bot_token=self.bot_token,
            bot_username=self.bot_username,
            chat_id=self.chat_id,
            chat_title=self.chat_title,
            linked_at_utc=self.linked_at_utc,
            alerts=dict(self.alerts),
        )

    def to_dict(self) -> dict[str, bool | str | dict[str, bool]]:
        """Serialize Telegram preferences without the machine-wide bot token."""

        return {
            "enabled": self.enabled,
            "bot_username": self.bot_username,
            "chat_id": self.chat_id,
            "chat_title": self.chat_title,
            "linked_at_utc": self.linked_at_utc,
            "alerts": dict(self.alerts),
        }

    def alert_enabled(self, alert_key: str) -> bool:
        """Return whether a notification type is enabled."""

        defaults = default_telegram_alerts()
        return bool(self.alerts.get(alert_key, defaults.get(alert_key, False)))


@dataclass
class FeedrateGroup:
    """Collection of presets and a default value for a motion family."""

    presets: List[float] = field(default_factory=list)
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


@dataclass
class OscillationSettings:
    """Persisted defaults for the oscillation panel."""

    mode: str = "X"
    amplitude_mm: float = 0.5
    feedrate_mm_min: float = 120.0
    turns_per_sweep: float = 3.0

    def clone(self) -> "OscillationSettings":
        """Return a copy of the oscillation configuration."""

        return OscillationSettings(
            mode=self.mode,
            amplitude_mm=self.amplitude_mm,
            feedrate_mm_min=self.feedrate_mm_min,
            turns_per_sweep=self.turns_per_sweep,
        )

    def to_dict(self) -> dict[str, float | str]:
        """Serialize the oscillation preferences."""

        return {
            "mode": self.mode,
            "amplitude_mm": self.amplitude_mm,
            "feedrate_mm_min": self.feedrate_mm_min,
            "turns_per_sweep": self.turns_per_sweep,
        }


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


@dataclass
class ClickToMoveSettings:
    """Configuration for click-to-move UI behavior."""

    pending_timeout_s: float = 8.0

    def clone(self) -> "ClickToMoveSettings":
        """Return a copy of the click-to-move preferences."""

        return ClickToMoveSettings(pending_timeout_s=self.pending_timeout_s)

    def to_dict(self) -> dict[str, float]:
        """Serialize the click-to-move preferences."""

        return {"pending_timeout_s": self.pending_timeout_s}


@dataclass
class SavedStagePositionSettings:
    """A persisted XYZ bookmark used by calibration workflows."""

    x_mm: float = 0.0
    y_mm: float = 0.0
    z_mm: float = 0.0
    configured: bool = False

    def clone(self) -> "SavedStagePositionSettings":
        """Return a copy of the saved XYZ bookmark."""

        return SavedStagePositionSettings(
            x_mm=self.x_mm,
            y_mm=self.y_mm,
            z_mm=self.z_mm,
            configured=self.configured,
        )

    def to_dict(self) -> dict[str, float | bool]:
        """Serialize the saved XYZ bookmark."""

        return {
            "x_mm": self.x_mm,
            "y_mm": self.y_mm,
            "z_mm": self.z_mm,
            "configured": self.configured,
        }


@dataclass
class NeedleCalibrationSettings:
    """Configuration for needle control and the external measurement instrument."""

    meter_type: str = LCR_METER_TYPE_GWINSTEK
    visa_resource: str = "COM4"
    keithley_source_resource: str = "GPIB2::1::INSTR"
    keithley_voltmeter_resource: str = "GPIB2::2::INSTR"
    measurement_function: str = "R-X"
    range_mode: str = "AUTO"
    auto_range_enabled: bool = True
    impedance_range: int = 3
    dcr_range: int = 4
    frequency_hz: float = 50.0
    level_mode: str = "VOLTAGE"
    voltage_level_v: float = 0.01
    current_level_a: float = 0.0001
    source_resistance_ohm: int = 100
    aperture_rate: str = "SLOW"
    aperture_averages: int = 1
    trigger_source: str = "INT"
    trigger_delay_s: float = 0.0
    bias_enabled: bool = False
    bias_level_v: float = 0.0
    monitor1: str = "OFF"
    monitor2: str = "OFF"
    alc_enabled: bool = False
    short_threshold_ohm: float = 10.0
    poll_interval_ms: int = 250
    feedrate_mm_min: float = 1.0
    contact_zone_mm: float = 0.05
    raise_position_mm: float = 0.0
    raise_position_configured: bool = False
    down_position_mm: float = 0.0
    down_position_configured: bool = False
    chip_position: SavedStagePositionSettings = field(
        default_factory=SavedStagePositionSettings
    )
    stone_position: SavedStagePositionSettings = field(
        default_factory=SavedStagePositionSettings
    )

    def clone(self) -> "NeedleCalibrationSettings":
        """Return a copy of the needle calibration settings."""

        return NeedleCalibrationSettings(
            meter_type=self.meter_type,
            visa_resource=self.visa_resource,
            keithley_source_resource=self.keithley_source_resource,
            keithley_voltmeter_resource=self.keithley_voltmeter_resource,
            measurement_function=self.measurement_function,
            range_mode=self.range_mode,
            auto_range_enabled=self.auto_range_enabled,
            impedance_range=self.impedance_range,
            dcr_range=self.dcr_range,
            frequency_hz=self.frequency_hz,
            level_mode=self.level_mode,
            voltage_level_v=self.voltage_level_v,
            current_level_a=self.current_level_a,
            source_resistance_ohm=self.source_resistance_ohm,
            aperture_rate=self.aperture_rate,
            aperture_averages=self.aperture_averages,
            trigger_source=self.trigger_source,
            trigger_delay_s=self.trigger_delay_s,
            bias_enabled=self.bias_enabled,
            bias_level_v=self.bias_level_v,
            monitor1=self.monitor1,
            monitor2=self.monitor2,
            alc_enabled=self.alc_enabled,
            short_threshold_ohm=self.short_threshold_ohm,
            poll_interval_ms=self.poll_interval_ms,
            feedrate_mm_min=self.feedrate_mm_min,
            contact_zone_mm=self.contact_zone_mm,
            raise_position_mm=self.raise_position_mm,
            raise_position_configured=self.raise_position_configured,
            down_position_mm=self.down_position_mm,
            down_position_configured=self.down_position_configured,
            chip_position=self.chip_position.clone(),
            stone_position=self.stone_position.clone(),
        )

    def to_dict(self) -> dict[str, float | int | str | bool | dict[str, float | bool]]:
        """Serialize the needle calibration preferences."""

        return {
            "meter_type": self.meter_type,
            "visa_resource": self.visa_resource,
            "keithley_source_resource": self.keithley_source_resource,
            "keithley_voltmeter_resource": self.keithley_voltmeter_resource,
            "measurement_function": self.measurement_function,
            "range_mode": self.range_mode,
            "auto_range_enabled": self.auto_range_enabled,
            "impedance_range": self.impedance_range,
            "dcr_range": self.dcr_range,
            "frequency_hz": self.frequency_hz,
            "level_mode": self.level_mode,
            "voltage_level_v": self.voltage_level_v,
            "current_level_a": self.current_level_a,
            "source_resistance_ohm": self.source_resistance_ohm,
            "aperture_rate": self.aperture_rate,
            "aperture_averages": self.aperture_averages,
            "trigger_source": self.trigger_source,
            "trigger_delay_s": self.trigger_delay_s,
            "bias_enabled": self.bias_enabled,
            "bias_level_v": self.bias_level_v,
            "monitor1": self.monitor1,
            "monitor2": self.monitor2,
            "alc_enabled": self.alc_enabled,
            "short_threshold_ohm": self.short_threshold_ohm,
            "poll_interval_ms": self.poll_interval_ms,
            "feedrate_mm_min": self.feedrate_mm_min,
            "contact_zone_mm": self.contact_zone_mm,
            "raise_position_mm": self.raise_position_mm,
            "raise_position_configured": self.raise_position_configured,
            "down_position_mm": self.down_position_mm,
            "down_position_configured": self.down_position_configured,
            "chip_position": self.chip_position.to_dict(),
            "stone_position": self.stone_position.to_dict(),
        }


@dataclass
class AxisACalibrationSettings:
    """Compact signed calibrated model for the nonlinear A-axis linkage."""

    configured: bool = False
    model: str = "cosine_displacement"
    steps_per_mm: float = 2600.0
    commanded_lowering_min_mm: float = 0.0
    commanded_lowering_max_mm: float = 5.5
    offset_mm: float = -0.18025492860701603
    amplitude_mm: float = -4.256281153779931
    angular_frequency_rad_per_mm: float = 0.2560331555269034
    phase_rad: float = 0.9304927419233507
    fit_rmse_mm: float = 0.03390421874833405
    fit_max_abs_error_mm: float = 0.044862806662900656
    source: str = "calibrations/axis_a_spm2600_pulloff0p25_forward_reverse_settle1p0_20260504.png"
    created_at: str = "2026-05-06T00:00:00+03:00"

    def clone(self) -> "AxisACalibrationSettings":
        """Return a copy of the A-axis calibration model."""

        return AxisACalibrationSettings(
            configured=self.configured,
            model=self.model,
            steps_per_mm=self.steps_per_mm,
            commanded_lowering_min_mm=self.commanded_lowering_min_mm,
            commanded_lowering_max_mm=self.commanded_lowering_max_mm,
            offset_mm=self.offset_mm,
            amplitude_mm=self.amplitude_mm,
            angular_frequency_rad_per_mm=self.angular_frequency_rad_per_mm,
            phase_rad=self.phase_rad,
            fit_rmse_mm=self.fit_rmse_mm,
            fit_max_abs_error_mm=self.fit_max_abs_error_mm,
            source=self.source,
            created_at=self.created_at,
        )

    def to_dict(self) -> dict[str, bool | float | str]:
        """Serialize the A-axis calibration model."""

        return {
            "configured": self.configured,
            "model": self.model,
            "steps_per_mm": self.steps_per_mm,
            "commanded_lowering_min_mm": self.commanded_lowering_min_mm,
            "commanded_lowering_max_mm": self.commanded_lowering_max_mm,
            "offset_mm": self.offset_mm,
            "amplitude_mm": self.amplitude_mm,
            "angular_frequency_rad_per_mm": self.angular_frequency_rad_per_mm,
            "phase_rad": self.phase_rad,
            "fit_rmse_mm": self.fit_rmse_mm,
            "fit_max_abs_error_mm": self.fit_max_abs_error_mm,
            "source": self.source,
            "created_at": self.created_at,
        }


@dataclass
class AxisZCalibrationSettings:
    """Smooth calibrated model for the measured Z-axis branches."""

    configured: bool = False
    model: str = "quintic_polynomial"
    steps_per_mm: float = 6335.0
    gcode_min_mm: float = 0.02
    gcode_max_mm: float = 23.4
    coefficients_mm: List[float] = field(
        default_factory=lambda: [
            -1.1689194871855767e-06,
            6.252947738309964e-05,
            -0.0006221022578588869,
            0.015449946058775076,
            0.5416754463041403,
            0.00910614542389841,
        ]
    )
    fit_rmse_mm: float = 0.006126947872349345
    fit_max_abs_error_mm: float = 0.020464954405667868
    section2_indicator_offset_mm: float = 8.661368914604154
    section3_indicator_offset_mm: float = 13.56547962940159
    source: str = "calibrations/axis_z_spm6335_full_hysteresis_precise_s1_s2_s3_honest_stitches_20260505.png"
    created_at: str = "2026-05-06T00:00:00+03:00"

    def clone(self) -> "AxisZCalibrationSettings":
        """Return a copy of the Z-axis calibration model."""

        return AxisZCalibrationSettings(
            configured=self.configured,
            model=self.model,
            steps_per_mm=self.steps_per_mm,
            gcode_min_mm=self.gcode_min_mm,
            gcode_max_mm=self.gcode_max_mm,
            coefficients_mm=list(self.coefficients_mm),
            fit_rmse_mm=self.fit_rmse_mm,
            fit_max_abs_error_mm=self.fit_max_abs_error_mm,
            section2_indicator_offset_mm=self.section2_indicator_offset_mm,
            section3_indicator_offset_mm=self.section3_indicator_offset_mm,
            source=self.source,
            created_at=self.created_at,
        )

    def to_dict(self) -> dict[str, bool | float | str | list[float]]:
        """Serialize the Z-axis calibration model."""

        return {
            "configured": self.configured,
            "model": self.model,
            "steps_per_mm": self.steps_per_mm,
            "gcode_min_mm": self.gcode_min_mm,
            "gcode_max_mm": self.gcode_max_mm,
            "coefficients_mm": list(self.coefficients_mm),
            "fit_rmse_mm": self.fit_rmse_mm,
            "fit_max_abs_error_mm": self.fit_max_abs_error_mm,
            "section2_indicator_offset_mm": self.section2_indicator_offset_mm,
            "section3_indicator_offset_mm": self.section3_indicator_offset_mm,
            "source": self.source,
            "created_at": self.created_at,
        }


@dataclass
class CoordinateSystemSettings:
    """Configuration for work-coordinate system selection."""

    position_mode: str = "work"
    startup_mode: str = "controller"
    preferred_system: str = "G54"

    def clone(self) -> "CoordinateSystemSettings":
        """Return a copy of the coordinate-system preferences."""

        return CoordinateSystemSettings(
            position_mode=self.position_mode,
            startup_mode=self.startup_mode,
            preferred_system=self.preferred_system,
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize coordinate-system preferences."""

        return {
            "position_mode": self.position_mode,
            "startup_mode": self.startup_mode,
            "preferred_system": self.preferred_system,
        }


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
    pixels_to_mm: List[List[float]] = field(default_factory=list)
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


def default_objectives() -> Dict[str, ObjectiveCalibrationSettings]:
    """Return the default objective profile map."""

    return {name: default_objective(name) for name in OBJECTIVE_NAMES}


@dataclass
class ObjectivesSettings:
    """Collection of all objective-specific optical calibration values."""

    active_name: str = DEFAULT_ACTIVE_OBJECTIVE
    apply_offsets_on_change: bool = True
    objectives: Dict[str, ObjectiveCalibrationSettings] = field(
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


@dataclass
class Settings:
    """Container for all configurable values."""

    controls: Dict[str, List[KeyBinding]] = field(default_factory=dict)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    api: ApiSettings = field(default_factory=ApiSettings)
    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    feedrates: FeedrateSettings = field(default_factory=FeedrateSettings)
    oscillation: OscillationSettings = field(default_factory=OscillationSettings)
    jog: JogSettings = field(default_factory=JogSettings)
    click_to_move: ClickToMoveSettings = field(default_factory=ClickToMoveSettings)
    needle_calibration: NeedleCalibrationSettings = field(
        default_factory=NeedleCalibrationSettings
    )
    axis_a_calibration: AxisACalibrationSettings = field(
        default_factory=AxisACalibrationSettings
    )
    axis_z_calibration: AxisZCalibrationSettings = field(
        default_factory=AxisZCalibrationSettings
    )
    coordinate_system: CoordinateSystemSettings = field(
        default_factory=CoordinateSystemSettings
    )
    objectives: ObjectivesSettings = field(default_factory=ObjectivesSettings)
    design_last_directory: str = ""

    def clone(self) -> "Settings":
        """Create a deep copy of the settings container."""

        return Settings(
            controls={key: list(value) for key, value in self.controls.items()},
            logging=self.logging.clone(),
            api=self.api.clone(),
            telegram=self.telegram.clone(),
            feedrates=self.feedrates.clone(),
            oscillation=self.oscillation.clone(),
            jog=self.jog.clone(),
            click_to_move=self.click_to_move.clone(),
            needle_calibration=self.needle_calibration.clone(),
            axis_a_calibration=self.axis_a_calibration.clone(),
            axis_z_calibration=self.axis_z_calibration.clone(),
            coordinate_system=self.coordinate_system.clone(),
            objectives=self.objectives.clone(),
            design_last_directory=self.design_last_directory,
        )

    def to_dict(self) -> dict:
        """Convert the settings into a JSON serializable structure."""

        return {
            "controls": {
                key: [binding.to_dict() for binding in bindings]
                for key, bindings in self.controls.items()
            },
            "logging": self.logging.to_dict(),
            "api": self.api.to_dict(),
            "telegram": self.telegram.to_dict(),
            "feedrates": {
                "linear": {
                    "presets": self.feedrates.linear.presets,
                    "default": self.feedrates.linear.default,
                },
                "rotary": {
                    "presets": self.feedrates.rotary.presets,
                    "default": self.feedrates.rotary.default,
                },
            },
            "oscillation": self.oscillation.to_dict(),
            "jog": self.jog.to_dict(),
            "click_to_move": self.click_to_move.to_dict(),
            "needle_calibration": self.needle_calibration.to_dict(),
            "axis_a_calibration": self.axis_a_calibration.to_dict(),
            "axis_z_calibration": self.axis_z_calibration.to_dict(),
            "coordinate_system": self.coordinate_system.to_dict(),
            "objectives": self.objectives.to_dict(),
            "design_last_directory": self.design_last_directory,
        }


class SettingsManager:
    """Load, persist, and expose user configurable settings."""

    CONFIG_FILENAME = "settings.json"
    CONTROLLER_STATE_FILENAME = "controller-state.json"
    SERIAL_CONNECTION_STATE_FILENAME = "serial-connection-state.json"
    METER_CONNECTION_STATE_FILENAME = "meter-connection-state.json"
    DEFAULT_LOG_FILENAME = "probe-station-gui.log"
    DEFAULT_API_ENABLED: bool = True
    DEFAULT_API_HOST: str = "127.0.0.1"
    DEFAULT_API_PORT: int = 8765
    MIN_FEEDRATE_MM_MIN: float = 1.0
    DEFAULT_LINEAR_FEEDRATE_PRESETS: tuple[float, ...] = (
        1.0,
        3.0,
        10.0,
        30.0,
        100.0,
        300.0,
    )
    DEFAULT_ROTARY_FEEDRATE_PRESETS: tuple[float, ...] = (
        1.0,
        3.0,
        10.0,
        30.0,
        90.0,
        360.0,
    )
    DEFAULT_FEEDRATE_DEFAULT: float = 1.0
    DEFAULT_OSCILLATION_MODE: str = "X"
    DEFAULT_OSCILLATION_AMPLITUDE_MM: float = 0.5
    DEFAULT_OSCILLATION_FEEDRATE_MM_MIN: float = 120.0
    DEFAULT_OSCILLATION_TURNS_PER_SWEEP: float = 3.0
    DEFAULT_JOG_MODE: str = "jog"
    DEFAULT_LINEAR_JOG_DISTANCE_MM: float = 25.0
    DEFAULT_ROTARY_JOG_DISTANCE_DEG: float = 5.0
    DEFAULT_MOTION_SAFETY_DISABLED: bool = False
    DEFAULT_MANUAL_AXIS: str = "A"
    DEFAULT_MANUAL_AXIS_DISTANCE_MM: float = 1.0
    DEFAULT_MANUAL_AXIS_MODE: str = "G91"
    DEFAULT_MANUAL_AXIS_FEEDRATE_MM_MIN: float = 1.0
    DEFAULT_FOCUS_FEEDRATE_MM_MIN: float = 1.0
    DEFAULT_FOCUS_STEP_FEEDRATE_MM_MIN: float = 1.0
    DEFAULT_NEEDLES_STEP_FEEDRATE_MM_MIN: float = 1.0
    DEFAULT_TURNTABLE_FEEDRATE_MM_MIN: float = 1.0
    DEFAULT_TURNTABLE_STEP_FEEDRATE_MM_MIN: float = 1.0
    DEFAULT_CLICK_TO_MOVE_PENDING_TIMEOUT_S: float = 8.0
    MIN_CLICK_TO_MOVE_PENDING_TIMEOUT_S: float = 0.5
    MAX_CLICK_TO_MOVE_PENDING_TIMEOUT_S: float = 60.0
    MANUAL_AXIS_MODES: tuple[str, ...] = ("G91", "G90")
    MANUAL_JOG_AXES: tuple[str, ...] = ("X", "Y", "Z", "A", "B", "C")
    DEFAULT_LCR_VISA_RESOURCE: str = "COM4"
    DEFAULT_LCR_METER_TYPE: str = LCR_METER_TYPE_GWINSTEK
    DEFAULT_KEITHLEY_SOURCE_RESOURCE: str = "GPIB2::1::INSTR"
    DEFAULT_KEITHLEY_VOLTMETER_RESOURCE: str = "GPIB2::2::INSTR"
    DEFAULT_LCR_AUTO_RANGE_ENABLED: bool = True
    DEFAULT_LCR_MEASUREMENT_FUNCTION: str = "R-X"
    DEFAULT_LCR_RANGE_MODE: str = "AUTO"
    DEFAULT_LCR_IMPEDANCE_RANGE: int = 3
    DEFAULT_LCR_DCR_RANGE: int = 4
    DEFAULT_LCR_FREQUENCY_HZ: float = 50.0
    DEFAULT_LCR_LEVEL_MODE: str = "VOLTAGE"
    DEFAULT_LCR_VOLTAGE_LEVEL_V: float = 0.01
    DEFAULT_LCR_CURRENT_LEVEL_A: float = 0.0001
    DEFAULT_LCR_SOURCE_RESISTANCE_OHM: int = 100
    DEFAULT_LCR_APERTURE_RATE: str = "SLOW"
    DEFAULT_LCR_APERTURE_AVERAGES: int = 1
    DEFAULT_LCR_TRIGGER_SOURCE: str = "INT"
    DEFAULT_LCR_TRIGGER_DELAY_S: float = 0.0
    DEFAULT_LCR_BIAS_ENABLED: bool = False
    DEFAULT_LCR_BIAS_LEVEL_V: float = 0.0
    DEFAULT_LCR_MONITOR: str = "OFF"
    DEFAULT_LCR_ALC_ENABLED: bool = False
    DEFAULT_SHORT_THRESHOLD_OHM: float = 10.0
    DEFAULT_LCR_POLL_INTERVAL_MS: int = 250
    DEFAULT_NEEDLE_FEEDRATE_MM_MIN: float = 1.0
    DEFAULT_NEEDLE_CONTACT_ZONE_MM: float = 0.05
    DEFAULT_AXIS_A_CALIBRATION_MODEL: str = "cosine_displacement"
    DEFAULT_AXIS_A_CALIBRATION_STEPS_PER_MM: float = 2600.0
    DEFAULT_AXIS_A_CALIBRATION_MIN_MM: float = 0.0
    DEFAULT_AXIS_A_CALIBRATION_MAX_MM: float = 5.5
    DEFAULT_AXIS_A_CALIBRATION_OFFSET_MM: float = 0.15233792205087007
    DEFAULT_AXIS_A_CALIBRATION_AMPLITUDE_MM: float = 4.257907588676109
    DEFAULT_AXIS_A_CALIBRATION_ANGULAR_FREQUENCY: float = 0.2557266307909934
    DEFAULT_AXIS_A_CALIBRATION_PHASE_RAD: float = 0.9228536838711686
    DEFAULT_AXIS_A_CALIBRATION_RMSE_MM: float = 0.004633488125628648
    DEFAULT_AXIS_A_CALIBRATION_MAX_ABS_ERROR_MM: float = 0.042784046777278234
    DEFAULT_AXIS_A_CALIBRATION_SOURCE: str = "calibrations/axis_a_spm2600_pulloff0p25_forward_reverse_settle1p0_20260504.png"
    DEFAULT_AXIS_A_CALIBRATION_CREATED_AT: str = "2026-05-06T00:00:00+03:00"
    DEFAULT_POSITION_MODE: str = "work"
    DEFAULT_COORDINATE_STARTUP_MODE: str = "controller"
    DEFAULT_COORDINATE_SYSTEM: str = "G54"
    LINEAR_GROUP = "linear"
    ROTARY_GROUP = "rotary"
    CYRILLIC_PATTERN = re.compile(r"[\u0400-\u04FF]")

    def __init__(self) -> None:
        self._config_dir = self._determine_config_dir()
        self._config_path = self._config_dir / self.CONFIG_FILENAME
        self._logger = logging.getLogger(__name__)
        self._logger.debug("Configuration directory resolved to %s", self._config_dir)
        self._ensure_default_file()
        self._settings = self._load()
        self.apply()

    @property
    def settings(self) -> Settings:
        """Access the mutable settings container."""

        return self._settings

    def config_dir(self) -> Path:
        """Return the application configuration directory."""

        return self._config_dir

    def replace(self, settings: Settings) -> None:
        """Replace the stored settings with the provided instance."""

        self._settings = self._normalise_settings(settings)
        self.apply()

    def save(self) -> None:
        """Persist the current settings to disk."""

        data = self._settings.to_dict()
        self._config_dir.mkdir(parents=True, exist_ok=True)
        with self._config_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        self._logger.info("Settings saved to %s", self._config_path)

    def apply(self) -> None:
        """Apply runtime-affecting settings such as logging configuration."""

        log_path = self.log_file_path()
        level_name = self.logging_level_name()
        configure_logging(log_path, level_name)
        self._logger.info(
            "Logging configured at level %s (file: %s)",
            level_name,
            log_path,
        )

    def control_bindings(self) -> Dict[str, List[KeyBinding]]:
        """Return the control bindings ensuring defaults are present."""

        controls = {key: list(value) for key, value in self._settings.controls.items()}
        for action in CONTROL_ACTIONS:
            controls.setdefault(action.key, self._default_control_bindings(action))
        return controls

    def logging_level_name(self) -> str:
        """Return the configured logging level name."""

        override = os.environ.get("PROBE_STATION_LOG_LEVEL", "").strip()
        if override:
            return override.upper()
        return (self._settings.logging.level or "INFO").upper()

    def log_file_path(self) -> Path:
        """Return the resolved log file path based on the settings."""

        file_setting = (self._settings.logging.file or "").strip()
        if file_setting:
            path = Path(file_setting)
            if not path.is_absolute():
                path = self._determine_log_dir() / path
            elif self._is_legacy_default_log_path(path):
                path = self._default_log_path()
        else:
            path = self._default_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def load_controller_state(self) -> dict | None:
        """Load persisted controller runtime state, if present."""

        path = self._config_dir / self.CONTROLLER_STATE_FILENAME
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            self._logger.warning("Failed to load controller state from %s: %s", path, exc)
            return None
        if not isinstance(data, dict):
            return None
        return data

    def save_controller_state(self, data: dict | None) -> None:
        """Persist controller runtime state alongside user settings."""

        path = self._config_dir / self.CONTROLLER_STATE_FILENAME
        if not data:
            self.clear_controller_state()
            return
        self._config_dir.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)

    def clear_controller_state(self) -> None:
        """Remove persisted controller runtime state."""

        path = self._config_dir / self.CONTROLLER_STATE_FILENAME
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            self._logger.warning("Failed to clear controller state %s: %s", path, exc)

    def load_serial_connection_state(self) -> dict:
        """Load persisted serial connection state, if present."""

        path = self._config_dir / self.SERIAL_CONNECTION_STATE_FILENAME
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            self._logger.warning(
                "Failed to load serial connection state from %s: %s",
                path,
                exc,
            )
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def save_serial_connection_state(
        self,
        connected: bool,
        *,
        port: str | None = None,
        baud_rate: int | None = None,
    ) -> None:
        """Persist the latest serial connection status."""

        data: dict[str, object] = {
            "status": "connected" if connected else "disconnected",
        }
        if port:
            data["port"] = str(port)
        if baud_rate is not None:
            data["baud_rate"] = int(baud_rate)

        path = self._config_dir / self.SERIAL_CONNECTION_STATE_FILENAME
        self._config_dir.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
        except OSError as exc:
            self._logger.warning(
                "Failed to save serial connection state to %s: %s",
                path,
                exc,
            )

    def serial_auto_connect_enabled(self) -> bool:
        """Return whether startup should restore an open serial connection."""

        data = self.load_serial_connection_state()
        return data.get("status") == "connected"

    def load_meter_connection_state(self) -> dict:
        """Load persisted measurement-instrument connection state, if present."""

        path = self._config_dir / self.METER_CONNECTION_STATE_FILENAME
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            self._logger.warning(
                "Failed to load measurement-instrument connection state from %s: %s",
                path,
                exc,
            )
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def save_meter_connection_state(
        self,
        connected: bool,
        *,
        meter_type: str | None = None,
        description: str | None = None,
    ) -> None:
        """Persist the latest measurement-instrument connection status."""

        data: dict[str, object] = {
            "status": "connected" if connected else "disconnected",
        }
        if meter_type:
            data["meter_type"] = str(meter_type)
        if description:
            data["description"] = str(description)

        path = self._config_dir / self.METER_CONNECTION_STATE_FILENAME
        self._config_dir.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
        except OSError as exc:
            self._logger.warning(
                "Failed to save measurement-instrument connection state to %s: %s",
                path,
                exc,
            )

    def meter_auto_connect_enabled(self) -> bool:
        """Return whether startup should restore an open measurement instrument."""

        data = self.load_meter_connection_state()
        return data.get("status") == "connected"

    def _determine_config_dir(self) -> Path:
        """Compute the directory where configuration files should live."""

        system = platform.system()
        if system == "Windows":
            base = os.environ.get("APPDATA")
            if base:
                return Path(base) / "ProbeStationGUI"
            return Path.home() / "AppData" / "Roaming" / "ProbeStationGUI"
        if system == "Darwin":
            return Path.home() / "Library" / "Application Support" / "ProbeStationGUI"
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            return Path(xdg) / "probe-station-gui"
        return Path.home() / ".config" / "probe-station-gui"

    def _determine_log_dir(self) -> Path:
        """Compute the directory where log files should live."""

        system = platform.system()
        if system == "Windows":
            base = os.environ.get("LOCALAPPDATA")
            if base:
                return Path(base) / "ProbeStationGUI" / "Logs"
            return Path.home() / "AppData" / "Local" / "ProbeStationGUI" / "Logs"
        if system == "Darwin":
            return Path.home() / "Library" / "Logs" / "ProbeStationGUI"
        xdg_state = os.environ.get("XDG_STATE_HOME")
        if xdg_state:
            return Path(xdg_state) / "probe-station-gui"
        return Path.home() / ".local" / "state" / "probe-station-gui"

    def _default_log_path(self) -> Path:
        return self._determine_log_dir() / self.DEFAULT_LOG_FILENAME

    def _is_legacy_default_log_path(self, path: Path) -> bool:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            resolved = path.expanduser()
        legacy = (self._config_dir / self.DEFAULT_LOG_FILENAME).expanduser()
        return resolved == legacy

    def _ensure_default_file(self) -> None:
        """Copy the default settings file when the user configuration is missing."""

        if self._config_path.exists():
            return
        self._config_dir.mkdir(parents=True, exist_ok=True)
        default_resource = resources.files("probe_station_gui").joinpath(
            "default_settings.json"
        )
        log_path = str(self._default_log_path())

        try:
            with default_resource.open("r", encoding="utf-8") as source:
                data = json.load(source)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}

        if not isinstance(data, dict):
            data = {}

        logging_section = data.get("logging")
        if not isinstance(logging_section, dict):
            logging_section = {"level": "INFO", "file": log_path}
            data["logging"] = logging_section
        else:
            logging_section["file"] = log_path

        api_section = data.get("api")
        if not isinstance(api_section, dict):
            api_section = ApiSettings().to_dict()
            data["api"] = api_section
        else:
            defaults = ApiSettings().to_dict()
            for key, value in defaults.items():
                api_section.setdefault(key, value)

        telegram_section = data.get("telegram")
        if not isinstance(telegram_section, dict):
            data["telegram"] = TelegramSettings().to_dict()
        else:
            defaults = TelegramSettings().to_dict()
            for key, value in defaults.items():
                telegram_section.setdefault(key, value)
            telegram_section.pop("bot_token", None)
            alerts = telegram_section.get("alerts")
            if not isinstance(alerts, dict):
                telegram_section["alerts"] = default_telegram_alerts()
            else:
                for key, enabled in default_telegram_alerts().items():
                    alerts.setdefault(key, enabled)

        feedrates_section = data.get("feedrates")
        legacy_presets = data.get("feedrate_presets")
        if not isinstance(feedrates_section, dict):
            legacy_raw_present = bool(legacy_presets)
            linear_presets = self._parse_feedrate_list(
                legacy_presets,
                fallback=self.DEFAULT_LINEAR_FEEDRATE_PRESETS,
            )
            if legacy_raw_present:
                rotary_presets = list(linear_presets)
            else:
                rotary_presets = list(self.DEFAULT_ROTARY_FEEDRATE_PRESETS)
            feedrates_section = {
                self.LINEAR_GROUP: {
                    "presets": linear_presets,
                    "default": self.DEFAULT_FEEDRATE_DEFAULT,
                },
                self.ROTARY_GROUP: {
                    "presets": rotary_presets,
                    "default": self.DEFAULT_FEEDRATE_DEFAULT,
                },
            }
            data["feedrates"] = feedrates_section
        else:
            if self.LINEAR_GROUP not in feedrates_section:
                feedrates_section[self.LINEAR_GROUP] = {
                    "presets": list(self.DEFAULT_LINEAR_FEEDRATE_PRESETS),
                    "default": self.DEFAULT_FEEDRATE_DEFAULT,
                }
            if self.ROTARY_GROUP not in feedrates_section:
                feedrates_section[self.ROTARY_GROUP] = {
                    "presets": list(self.DEFAULT_ROTARY_FEEDRATE_PRESETS),
                    "default": self.DEFAULT_FEEDRATE_DEFAULT,
                }
            data["feedrates"] = feedrates_section

        jog_section = data.get("jog")
        if not isinstance(jog_section, dict):
            jog_section = {
                "mode": self.DEFAULT_JOG_MODE,
                "linear_distance_mm": self.DEFAULT_LINEAR_JOG_DISTANCE_MM,
                "rotary_distance_deg": self.DEFAULT_ROTARY_JOG_DISTANCE_DEG,
                "motion_safety_disabled": self.DEFAULT_MOTION_SAFETY_DISABLED,
                "manual_axis": self.DEFAULT_MANUAL_AXIS,
                "manual_axis_distance_mm": self.DEFAULT_MANUAL_AXIS_DISTANCE_MM,
                "manual_axis_mode": self.DEFAULT_MANUAL_AXIS_MODE,
                "manual_axis_feedrate_mm_min": self.DEFAULT_MANUAL_AXIS_FEEDRATE_MM_MIN,
                "focus_feedrate_mm_min": self.DEFAULT_FOCUS_FEEDRATE_MM_MIN,
                "focus_step_feedrate_mm_min": self.DEFAULT_FOCUS_STEP_FEEDRATE_MM_MIN,
                "needles_step_feedrate_mm_min": self.DEFAULT_NEEDLES_STEP_FEEDRATE_MM_MIN,
                "turntable_feedrate_mm_min": self.DEFAULT_TURNTABLE_FEEDRATE_MM_MIN,
                "turntable_step_feedrate_mm_min": self.DEFAULT_TURNTABLE_STEP_FEEDRATE_MM_MIN,
            }
            data["jog"] = jog_section
        else:
            jog_section.setdefault("mode", self.DEFAULT_JOG_MODE)
            jog_section.setdefault(
                "linear_distance_mm", self.DEFAULT_LINEAR_JOG_DISTANCE_MM
            )
            jog_section.setdefault(
                "rotary_distance_deg", self.DEFAULT_ROTARY_JOG_DISTANCE_DEG
            )
            jog_section.setdefault(
                "motion_safety_disabled", self.DEFAULT_MOTION_SAFETY_DISABLED
            )
            jog_section.setdefault("manual_axis", self.DEFAULT_MANUAL_AXIS)
            jog_section.setdefault(
                "manual_axis_distance_mm", self.DEFAULT_MANUAL_AXIS_DISTANCE_MM
            )
            jog_section.setdefault("manual_axis_mode", self.DEFAULT_MANUAL_AXIS_MODE)
            jog_section.setdefault(
                "manual_axis_feedrate_mm_min",
                self.DEFAULT_MANUAL_AXIS_FEEDRATE_MM_MIN,
            )
            jog_section.setdefault(
                "focus_feedrate_mm_min",
                self.DEFAULT_FOCUS_FEEDRATE_MM_MIN,
            )
            jog_section.setdefault(
                "focus_step_feedrate_mm_min",
                self.DEFAULT_FOCUS_STEP_FEEDRATE_MM_MIN,
            )
            jog_section.setdefault(
                "needles_step_feedrate_mm_min",
                self.DEFAULT_NEEDLES_STEP_FEEDRATE_MM_MIN,
            )
            jog_section.setdefault(
                "turntable_feedrate_mm_min",
                self.DEFAULT_TURNTABLE_FEEDRATE_MM_MIN,
            )
            jog_section.setdefault(
                "turntable_step_feedrate_mm_min",
                self.DEFAULT_TURNTABLE_STEP_FEEDRATE_MM_MIN,
            )

        click_to_move_section = data.get("click_to_move")
        if not isinstance(click_to_move_section, dict):
            click_to_move_section = ClickToMoveSettings(
                pending_timeout_s=self.DEFAULT_CLICK_TO_MOVE_PENDING_TIMEOUT_S
            ).to_dict()
            data["click_to_move"] = click_to_move_section
        else:
            click_to_move_section.setdefault(
                "pending_timeout_s",
                self.DEFAULT_CLICK_TO_MOVE_PENDING_TIMEOUT_S,
            )

        oscillation_section = data.get("oscillation")
        if not isinstance(oscillation_section, dict):
            oscillation_section = {
                "mode": self.DEFAULT_OSCILLATION_MODE,
                "amplitude_mm": self.DEFAULT_OSCILLATION_AMPLITUDE_MM,
                "feedrate_mm_min": self.DEFAULT_OSCILLATION_FEEDRATE_MM_MIN,
                "turns_per_sweep": self.DEFAULT_OSCILLATION_TURNS_PER_SWEEP,
            }
            data["oscillation"] = oscillation_section
        else:
            oscillation_section.setdefault("mode", self.DEFAULT_OSCILLATION_MODE)
            oscillation_section.setdefault(
                "amplitude_mm", self.DEFAULT_OSCILLATION_AMPLITUDE_MM
            )
            oscillation_section.setdefault(
                "feedrate_mm_min", self.DEFAULT_OSCILLATION_FEEDRATE_MM_MIN
            )
            oscillation_section.setdefault(
                "turns_per_sweep", self.DEFAULT_OSCILLATION_TURNS_PER_SWEEP
            )

        needle_section = data.get("needle_calibration")
        if not isinstance(needle_section, dict):
            needle_section = {
                "meter_type": self.DEFAULT_LCR_METER_TYPE,
                "visa_resource": self.DEFAULT_LCR_VISA_RESOURCE,
                "keithley_source_resource": self.DEFAULT_KEITHLEY_SOURCE_RESOURCE,
                "keithley_voltmeter_resource": self.DEFAULT_KEITHLEY_VOLTMETER_RESOURCE,
                "measurement_function": self.DEFAULT_LCR_MEASUREMENT_FUNCTION,
                "range_mode": self.DEFAULT_LCR_RANGE_MODE,
                "auto_range_enabled": self.DEFAULT_LCR_AUTO_RANGE_ENABLED,
                "impedance_range": self.DEFAULT_LCR_IMPEDANCE_RANGE,
                "dcr_range": self.DEFAULT_LCR_DCR_RANGE,
                "frequency_hz": self.DEFAULT_LCR_FREQUENCY_HZ,
                "level_mode": self.DEFAULT_LCR_LEVEL_MODE,
                "voltage_level_v": self.DEFAULT_LCR_VOLTAGE_LEVEL_V,
                "current_level_a": self.DEFAULT_LCR_CURRENT_LEVEL_A,
                "source_resistance_ohm": self.DEFAULT_LCR_SOURCE_RESISTANCE_OHM,
                "aperture_rate": self.DEFAULT_LCR_APERTURE_RATE,
                "aperture_averages": self.DEFAULT_LCR_APERTURE_AVERAGES,
                "trigger_source": self.DEFAULT_LCR_TRIGGER_SOURCE,
                "trigger_delay_s": self.DEFAULT_LCR_TRIGGER_DELAY_S,
                "bias_enabled": self.DEFAULT_LCR_BIAS_ENABLED,
                "bias_level_v": self.DEFAULT_LCR_BIAS_LEVEL_V,
                "monitor1": self.DEFAULT_LCR_MONITOR,
                "monitor2": self.DEFAULT_LCR_MONITOR,
                "alc_enabled": self.DEFAULT_LCR_ALC_ENABLED,
                "short_threshold_ohm": self.DEFAULT_SHORT_THRESHOLD_OHM,
                "poll_interval_ms": self.DEFAULT_LCR_POLL_INTERVAL_MS,
                "feedrate_mm_min": self.DEFAULT_NEEDLE_FEEDRATE_MM_MIN,
                "contact_zone_mm": self.DEFAULT_NEEDLE_CONTACT_ZONE_MM,
                "raise_position_mm": 0.0,
                "raise_position_configured": False,
                "down_position_mm": 0.0,
                "down_position_configured": False,
                "chip_position": {
                    "x_mm": 0.0,
                    "y_mm": 0.0,
                    "z_mm": 0.0,
                    "configured": False,
                },
                "stone_position": {
                    "x_mm": 0.0,
                    "y_mm": 0.0,
                    "z_mm": 0.0,
                    "configured": False,
                },
            }
            data["needle_calibration"] = needle_section
        else:
            needle_section.setdefault("meter_type", self.DEFAULT_LCR_METER_TYPE)
            needle_section.setdefault("visa_resource", self.DEFAULT_LCR_VISA_RESOURCE)
            needle_section.setdefault(
                "keithley_source_resource", self.DEFAULT_KEITHLEY_SOURCE_RESOURCE
            )
            needle_section.setdefault(
                "keithley_voltmeter_resource",
                self.DEFAULT_KEITHLEY_VOLTMETER_RESOURCE,
            )
            needle_section.setdefault(
                "measurement_function", self.DEFAULT_LCR_MEASUREMENT_FUNCTION
            )
            needle_section.setdefault("range_mode", self.DEFAULT_LCR_RANGE_MODE)
            needle_section.setdefault(
                "auto_range_enabled", self.DEFAULT_LCR_AUTO_RANGE_ENABLED
            )
            needle_section.setdefault(
                "impedance_range", self.DEFAULT_LCR_IMPEDANCE_RANGE
            )
            needle_section.setdefault("dcr_range", self.DEFAULT_LCR_DCR_RANGE)
            needle_section.setdefault("frequency_hz", self.DEFAULT_LCR_FREQUENCY_HZ)
            needle_section.setdefault("level_mode", self.DEFAULT_LCR_LEVEL_MODE)
            needle_section.setdefault(
                "voltage_level_v", self.DEFAULT_LCR_VOLTAGE_LEVEL_V
            )
            needle_section.setdefault(
                "current_level_a", self.DEFAULT_LCR_CURRENT_LEVEL_A
            )
            needle_section.setdefault(
                "source_resistance_ohm", self.DEFAULT_LCR_SOURCE_RESISTANCE_OHM
            )
            needle_section.setdefault("aperture_rate", self.DEFAULT_LCR_APERTURE_RATE)
            needle_section.setdefault(
                "aperture_averages", self.DEFAULT_LCR_APERTURE_AVERAGES
            )
            needle_section.setdefault("trigger_source", self.DEFAULT_LCR_TRIGGER_SOURCE)
            needle_section.setdefault(
                "trigger_delay_s", self.DEFAULT_LCR_TRIGGER_DELAY_S
            )
            needle_section.setdefault("bias_enabled", self.DEFAULT_LCR_BIAS_ENABLED)
            needle_section.setdefault("bias_level_v", self.DEFAULT_LCR_BIAS_LEVEL_V)
            needle_section.setdefault("monitor1", self.DEFAULT_LCR_MONITOR)
            needle_section.setdefault("monitor2", self.DEFAULT_LCR_MONITOR)
            needle_section.setdefault("alc_enabled", self.DEFAULT_LCR_ALC_ENABLED)
            needle_section.setdefault(
                "short_threshold_ohm", self.DEFAULT_SHORT_THRESHOLD_OHM
            )
            needle_section.setdefault(
                "poll_interval_ms", self.DEFAULT_LCR_POLL_INTERVAL_MS
            )
            needle_section.setdefault(
                "feedrate_mm_min", self.DEFAULT_NEEDLE_FEEDRATE_MM_MIN
            )
            needle_section.setdefault(
                "contact_zone_mm", self.DEFAULT_NEEDLE_CONTACT_ZONE_MM
            )
            needle_section.setdefault("raise_position_mm", 0.0)
            needle_section.setdefault("raise_position_configured", False)
            needle_section.setdefault("down_position_mm", 0.0)
            needle_section.setdefault("down_position_configured", False)
            for key in ("chip_position", "stone_position"):
                bookmark = needle_section.get(key)
                if not isinstance(bookmark, dict):
                    bookmark = {}
                    needle_section[key] = bookmark
                bookmark.setdefault("x_mm", 0.0)
                bookmark.setdefault("y_mm", 0.0)
                bookmark.setdefault("z_mm", 0.0)
                bookmark.setdefault("configured", False)

        axis_a_section = data.get("axis_a_calibration")
        if not isinstance(axis_a_section, dict):
            axis_a_section = AxisACalibrationSettings().to_dict()
            data["axis_a_calibration"] = axis_a_section
        else:
            defaults = AxisACalibrationSettings().to_dict()
            for key, value in defaults.items():
                axis_a_section.setdefault(key, value)

        axis_z_section = data.get("axis_z_calibration")
        if not isinstance(axis_z_section, dict):
            axis_z_section = AxisZCalibrationSettings().to_dict()
            data["axis_z_calibration"] = axis_z_section
        else:
            defaults = AxisZCalibrationSettings().to_dict()
            for key, value in defaults.items():
                axis_z_section.setdefault(key, value)

        coordinate_section = data.get("coordinate_system")
        if not isinstance(coordinate_section, dict):
            coordinate_section = {
                "position_mode": self.DEFAULT_POSITION_MODE,
                "startup_mode": self.DEFAULT_COORDINATE_STARTUP_MODE,
                "preferred_system": self.DEFAULT_COORDINATE_SYSTEM,
            }
            data["coordinate_system"] = coordinate_section
        else:
            coordinate_section.setdefault(
                "position_mode", self.DEFAULT_POSITION_MODE
            )
            coordinate_section.setdefault(
                "startup_mode", self.DEFAULT_COORDINATE_STARTUP_MODE
            )
            coordinate_section.setdefault(
                "preferred_system", self.DEFAULT_COORDINATE_SYSTEM
            )

        objectives_section = data.get("objectives")
        if not isinstance(objectives_section, dict):
            data["objectives"] = ObjectivesSettings().to_dict()
        else:
            defaults = ObjectivesSettings().to_dict()
            objectives_section.setdefault("active_name", defaults["active_name"])
            objectives_section.setdefault(
                "apply_offsets_on_change",
                defaults["apply_offsets_on_change"],
            )
            raw_profiles = objectives_section.get("objectives")
            if not isinstance(raw_profiles, dict) or not raw_profiles:
                raw_profiles = dict(defaults["objectives"])
                objectives_section["objectives"] = raw_profiles
            default_profiles = defaults["objectives"]
            if isinstance(default_profiles, dict):
                for name, stored in list(raw_profiles.items()):
                    normalized_name = normalize_objective_name(name)
                    profile = default_profiles.get(normalized_name)
                    if not isinstance(profile, dict):
                        profile = default_objective(normalized_name).to_dict()
                    if isinstance(stored, dict):
                        for key, value in profile.items():
                            stored.setdefault(key, value)

        design_last_directory = data.get("design_last_directory")
        if not isinstance(design_last_directory, str):
            data["design_last_directory"] = ""

        with self._config_path.open("w", encoding="utf-8") as target:
            json.dump(data, target, indent=2, ensure_ascii=False)

        self._logger.info("Default settings copied to %s", self._config_path)

    def _load(self) -> Settings:
        """Load settings from disk and normalise the structure."""

        with self._config_path.open("r", encoding="utf-8-sig") as handle:
            raw = json.load(handle)
        self._logger.debug("Loaded settings from %s", self._config_path)
        controls_raw = raw.get("controls", {}) if isinstance(raw, dict) else {}
        controls: Dict[str, List[KeyBinding]] = {}
        for key, values in controls_raw.items():
            bindings: List[KeyBinding] = []
            if isinstance(values, Iterable):
                for value in values:
                    if isinstance(value, dict):
                        binding = KeyBinding.from_dict(value)
                        if self._should_keep_control_binding(binding):
                            bindings.append(binding)
            controls[key] = bindings
        raw_control_keys = set(controls_raw) if isinstance(controls_raw, dict) else set()
        for action in CONTROL_ACTIONS:
            if action.key not in controls:
                controls[action.key] = (
                    self._default_control_bindings(action)
                    if action.key not in raw_control_keys
                    else []
                )
        logging_raw = raw.get("logging", {}) if isinstance(raw, dict) else {}
        logging_settings = self._parse_logging(logging_raw)
        if not logging_settings.file:
            default_log = str(self._default_log_path())
            logging_settings.file = default_log
            self._logger.debug(
                "Log file path missing in settings; defaulting to %s", default_log
            )
        feedrates_raw = raw.get("feedrates") if isinstance(raw, dict) else None
        legacy_presets = raw.get("feedrate_presets") if isinstance(raw, dict) else None
        feedrates = self._parse_feedrates(feedrates_raw, legacy_presets)
        api_raw = raw.get("api") if isinstance(raw, dict) else None
        telegram_raw = raw.get("telegram") if isinstance(raw, dict) else None
        oscillation_raw = raw.get("oscillation") if isinstance(raw, dict) else None
        jog_raw = raw.get("jog") if isinstance(raw, dict) else None
        click_to_move_raw = (
            raw.get("click_to_move") if isinstance(raw, dict) else None
        )
        needle_calibration_raw = (
            raw.get("needle_calibration") if isinstance(raw, dict) else None
        )
        axis_a_calibration_raw = (
            raw.get("axis_a_calibration") if isinstance(raw, dict) else None
        )
        axis_z_calibration_raw = (
            raw.get("axis_z_calibration") if isinstance(raw, dict) else None
        )
        coordinate_system_raw = (
            raw.get("coordinate_system") if isinstance(raw, dict) else None
        )
        objectives_raw = raw.get("objectives") if isinstance(raw, dict) else None
        design_last_directory = ""
        if isinstance(raw, dict):
            design_last_directory_raw = raw.get("design_last_directory", "")
            if isinstance(design_last_directory_raw, str):
                design_last_directory = design_last_directory_raw.strip()
        return Settings(
            controls=controls,
            logging=logging_settings,
            api=self._parse_api(api_raw),
            telegram=self._parse_telegram(telegram_raw),
            feedrates=feedrates,
            oscillation=self._parse_oscillation(oscillation_raw),
            jog=self._parse_jog(jog_raw),
            click_to_move=self._parse_click_to_move(click_to_move_raw),
            needle_calibration=self._parse_needle_calibration(needle_calibration_raw),
            axis_a_calibration=self._parse_axis_a_calibration(axis_a_calibration_raw),
            axis_z_calibration=self._parse_axis_z_calibration(axis_z_calibration_raw),
            coordinate_system=self._parse_coordinate_system(coordinate_system_raw),
            objectives=self._parse_objectives(objectives_raw),
            design_last_directory=design_last_directory,
        )

    def _parse_logging(self, raw_logging) -> LoggingSettings:
        """Create a logging configuration from persisted data."""

        level = "INFO"
        file_value = ""
        if isinstance(raw_logging, dict):
            level = str(raw_logging.get("level", level))
            file_raw = raw_logging.get("file", file_value)
            if isinstance(file_raw, str):
                file_value = file_raw
        return LoggingSettings(level=level.upper(), file=file_value)

    def _parse_api(self, raw_api) -> ApiSettings:
        """Normalise local API settings."""

        settings = ApiSettings()
        if isinstance(raw_api, dict):
            settings.enabled = bool(raw_api.get("enabled", settings.enabled))
            host_raw = raw_api.get("host", settings.host)
            if isinstance(host_raw, str):
                host = host_raw.strip()
                if host:
                    settings.host = host
            try:
                port = int(raw_api.get("port", settings.port))
            except (TypeError, ValueError):
                port = settings.port
            if 0 < port <= 65535:
                settings.port = port
        return settings

    def _parse_telegram(self, raw_telegram) -> TelegramSettings:
        """Normalise Telegram notification settings."""

        settings = TelegramSettings()
        if isinstance(raw_telegram, dict):
            settings.enabled = self._coerce_bool(
                raw_telegram.get("enabled", settings.enabled),
                default=settings.enabled,
            )
            legacy_bot_token = ""
            legacy_bot_token_raw = raw_telegram.get("bot_token", "")
            if isinstance(legacy_bot_token_raw, (str, int)):
                legacy_bot_token = str(legacy_bot_token_raw).strip()
            if legacy_bot_token and not load_global_bot_token():
                try:
                    save_global_bot_token(legacy_bot_token)
                except OSError as exc:
                    self._logger.warning(
                        "Failed to migrate Telegram bot token to global settings: %s",
                        exc,
                    )
                    settings.bot_token = legacy_bot_token
            for attr in (
                "bot_username",
                "chat_id",
                "chat_title",
                "linked_at_utc",
            ):
                raw_value = raw_telegram.get(attr, getattr(settings, attr))
                if isinstance(raw_value, (str, int)):
                    setattr(settings, attr, str(raw_value).strip())
            settings.bot_username = settings.bot_username.lstrip("@")
            settings.alerts = self._parse_telegram_alerts(
                raw_telegram.get("alerts")
            )
        return settings

    def _parse_telegram_alerts(self, raw_alerts) -> Dict[str, bool]:
        alerts = default_telegram_alerts()
        if isinstance(raw_alerts, dict):
            for key, _label in TELEGRAM_ALERT_TYPES:
                if key in raw_alerts:
                    alerts[key] = self._coerce_bool(
                        raw_alerts.get(key),
                        default=alerts[key],
                    )
        return alerts

    def _parse_feedrates(self, raw_feedrates, legacy_presets) -> FeedrateSettings:
        """Normalise persisted feedrate data supporting legacy layouts."""

        linear_group, rotary_group = self._parse_feedrate_groups(
            raw_feedrates, legacy_presets
        )
        return FeedrateSettings(
            linear=linear_group,
            rotary=rotary_group,
        )

    def _parse_jog(self, raw_jog) -> JogSettings:
        """Normalise persisted jog settings."""

        config = parse_jog_settings(
            raw_jog,
            JogSettingsDefaults(
                mode=self.DEFAULT_JOG_MODE,
                linear_distance_mm=self.DEFAULT_LINEAR_JOG_DISTANCE_MM,
                rotary_distance_deg=self.DEFAULT_ROTARY_JOG_DISTANCE_DEG,
                motion_safety_disabled=self.DEFAULT_MOTION_SAFETY_DISABLED,
                manual_axis=self.DEFAULT_MANUAL_AXIS,
                manual_axis_distance_mm=self.DEFAULT_MANUAL_AXIS_DISTANCE_MM,
                manual_axis_mode=self.DEFAULT_MANUAL_AXIS_MODE,
                manual_axis_feedrate_mm_min=(
                    self.DEFAULT_MANUAL_AXIS_FEEDRATE_MM_MIN
                ),
                focus_feedrate_mm_min=self.DEFAULT_FOCUS_FEEDRATE_MM_MIN,
                focus_step_feedrate_mm_min=self.DEFAULT_FOCUS_STEP_FEEDRATE_MM_MIN,
                needles_step_feedrate_mm_min=(
                    self.DEFAULT_NEEDLES_STEP_FEEDRATE_MM_MIN
                ),
                turntable_feedrate_mm_min=self.DEFAULT_TURNTABLE_FEEDRATE_MM_MIN,
                turntable_step_feedrate_mm_min=(
                    self.DEFAULT_TURNTABLE_STEP_FEEDRATE_MM_MIN
                ),
                min_feedrate_mm_min=self.MIN_FEEDRATE_MM_MIN,
                manual_axes=self.MANUAL_JOG_AXES,
                manual_axis_modes=self.MANUAL_AXIS_MODES,
            ),
        )
        return JogSettings(
            mode=config.mode,
            linear_distance_mm=config.linear_distance_mm,
            rotary_distance_deg=config.rotary_distance_deg,
            motion_safety_disabled=config.motion_safety_disabled,
            manual_axis=config.manual_axis,
            manual_axis_distance_mm=config.manual_axis_distance_mm,
            manual_axis_mode=config.manual_axis_mode,
            manual_axis_feedrate_mm_min=config.manual_axis_feedrate_mm_min,
            focus_feedrate_mm_min=config.focus_feedrate_mm_min,
            focus_step_feedrate_mm_min=config.focus_step_feedrate_mm_min,
            needles_step_feedrate_mm_min=config.needles_step_feedrate_mm_min,
            turntable_feedrate_mm_min=config.turntable_feedrate_mm_min,
            turntable_step_feedrate_mm_min=config.turntable_step_feedrate_mm_min,
        )

    def _parse_click_to_move(self, raw_click_to_move) -> ClickToMoveSettings:
        """Normalise click-to-move UI timing settings."""

        timeout_s = self.DEFAULT_CLICK_TO_MOVE_PENDING_TIMEOUT_S
        if isinstance(raw_click_to_move, dict):
            timeout_s = self._finite_float(
                raw_click_to_move.get("pending_timeout_s", timeout_s),
                default=self.DEFAULT_CLICK_TO_MOVE_PENDING_TIMEOUT_S,
            )
        if timeout_s < self.MIN_CLICK_TO_MOVE_PENDING_TIMEOUT_S:
            timeout_s = self.MIN_CLICK_TO_MOVE_PENDING_TIMEOUT_S
        if timeout_s > self.MAX_CLICK_TO_MOVE_PENDING_TIMEOUT_S:
            timeout_s = self.MAX_CLICK_TO_MOVE_PENDING_TIMEOUT_S
        return ClickToMoveSettings(pending_timeout_s=float(timeout_s))

    def _parse_oscillation(self, raw_oscillation) -> OscillationSettings:
        """Normalise persisted oscillation-panel settings."""

        config = parse_oscillation_settings(
            raw_oscillation,
            OscillationSettingsDefaults(
                mode=self.DEFAULT_OSCILLATION_MODE,
                amplitude_mm=self.DEFAULT_OSCILLATION_AMPLITUDE_MM,
                feedrate_mm_min=self.DEFAULT_OSCILLATION_FEEDRATE_MM_MIN,
                turns_per_sweep=self.DEFAULT_OSCILLATION_TURNS_PER_SWEEP,
            ),
        )
        return OscillationSettings(
            mode=config.mode,
            amplitude_mm=config.amplitude_mm,
            feedrate_mm_min=config.feedrate_mm_min,
            turns_per_sweep=config.turns_per_sweep,
        )

    def _parse_needle_calibration(
        self, raw_needle_calibration
    ) -> NeedleCalibrationSettings:
        """Normalise persisted needle calibration settings."""

        config = parse_needle_calibration_settings(
            raw_needle_calibration,
            self._needle_calibration_defaults(),
        )
        return NeedleCalibrationSettings(
            meter_type=config.meter_type,
            visa_resource=config.visa_resource,
            keithley_source_resource=config.keithley_source_resource,
            keithley_voltmeter_resource=config.keithley_voltmeter_resource,
            measurement_function=config.measurement_function,
            range_mode=config.range_mode,
            auto_range_enabled=config.auto_range_enabled,
            impedance_range=config.impedance_range,
            dcr_range=config.dcr_range,
            frequency_hz=config.frequency_hz,
            level_mode=config.level_mode,
            voltage_level_v=config.voltage_level_v,
            current_level_a=config.current_level_a,
            source_resistance_ohm=config.source_resistance_ohm,
            aperture_rate=config.aperture_rate,
            aperture_averages=config.aperture_averages,
            trigger_source=config.trigger_source,
            trigger_delay_s=config.trigger_delay_s,
            bias_enabled=config.bias_enabled,
            bias_level_v=config.bias_level_v,
            monitor1=config.monitor1,
            monitor2=config.monitor2,
            alc_enabled=config.alc_enabled,
            short_threshold_ohm=config.short_threshold_ohm,
            poll_interval_ms=config.poll_interval_ms,
            feedrate_mm_min=config.feedrate_mm_min,
            contact_zone_mm=config.contact_zone_mm,
            raise_position_mm=config.raise_position_mm,
            raise_position_configured=config.raise_position_configured,
            down_position_mm=config.down_position_mm,
            down_position_configured=config.down_position_configured,
            chip_position=self._saved_stage_position_from_config(
                config.chip_position
            ),
            stone_position=self._saved_stage_position_from_config(
                config.stone_position
            ),
        )

    def _needle_calibration_defaults(self) -> NeedleCalibrationDefaults:
        return NeedleCalibrationDefaults(
            meter_type=self.DEFAULT_LCR_METER_TYPE,
            visa_resource=self.DEFAULT_LCR_VISA_RESOURCE,
            keithley_source_resource=self.DEFAULT_KEITHLEY_SOURCE_RESOURCE,
            keithley_voltmeter_resource=self.DEFAULT_KEITHLEY_VOLTMETER_RESOURCE,
            measurement_function=self.DEFAULT_LCR_MEASUREMENT_FUNCTION,
            range_mode=self.DEFAULT_LCR_RANGE_MODE,
            auto_range_enabled=self.DEFAULT_LCR_AUTO_RANGE_ENABLED,
            impedance_range=self.DEFAULT_LCR_IMPEDANCE_RANGE,
            dcr_range=self.DEFAULT_LCR_DCR_RANGE,
            frequency_hz=self.DEFAULT_LCR_FREQUENCY_HZ,
            level_mode=self.DEFAULT_LCR_LEVEL_MODE,
            voltage_level_v=self.DEFAULT_LCR_VOLTAGE_LEVEL_V,
            current_level_a=self.DEFAULT_LCR_CURRENT_LEVEL_A,
            source_resistance_ohm=self.DEFAULT_LCR_SOURCE_RESISTANCE_OHM,
            aperture_rate=self.DEFAULT_LCR_APERTURE_RATE,
            aperture_averages=self.DEFAULT_LCR_APERTURE_AVERAGES,
            trigger_source=self.DEFAULT_LCR_TRIGGER_SOURCE,
            trigger_delay_s=self.DEFAULT_LCR_TRIGGER_DELAY_S,
            bias_enabled=self.DEFAULT_LCR_BIAS_ENABLED,
            bias_level_v=self.DEFAULT_LCR_BIAS_LEVEL_V,
            monitor=self.DEFAULT_LCR_MONITOR,
            alc_enabled=self.DEFAULT_LCR_ALC_ENABLED,
            short_threshold_ohm=self.DEFAULT_SHORT_THRESHOLD_OHM,
            poll_interval_ms=self.DEFAULT_LCR_POLL_INTERVAL_MS,
            feedrate_mm_min=self.DEFAULT_NEEDLE_FEEDRATE_MM_MIN,
            contact_zone_mm=self.DEFAULT_NEEDLE_CONTACT_ZONE_MM,
            min_feedrate_mm_min=self.MIN_FEEDRATE_MM_MIN,
            meter_types=LCR_METER_TYPES,
            measurement_functions=LCR_MEASUREMENT_FUNCTIONS,
            range_modes=LCR_RANGE_MODES,
            level_modes=LCR_LEVEL_MODES,
            source_resistances_ohm=LCR_SOURCE_RESISTANCES_OHM,
            aperture_rates=LCR_APERTURE_RATES,
            trigger_sources=LCR_TRIGGER_SOURCES,
            monitor_parameters=LCR_MONITOR_PARAMETERS,
        )

    def _parse_axis_a_calibration(self, raw_calibration) -> AxisACalibrationSettings:
        """Normalise the compact A-axis nonlinear calibration model."""

        config = parse_axis_a_calibration(
            raw_calibration,
            AxisACalibrationConfig(**AxisACalibrationSettings().to_dict()),
            expected_model=self.DEFAULT_AXIS_A_CALIBRATION_MODEL,
        )
        return AxisACalibrationSettings(**config.__dict__)

    def _parse_axis_z_calibration(self, raw_calibration) -> AxisZCalibrationSettings:
        """Normalise the compact Z-axis nonlinear calibration model."""

        config = parse_axis_z_calibration(
            raw_calibration,
            AxisZCalibrationConfig(**AxisZCalibrationSettings().to_dict()),
        )
        return AxisZCalibrationSettings(
            **{
                **config.__dict__,
                "coefficients_mm": list(config.coefficients_mm),
            }
        )

    @staticmethod
    def _coerce_bool(value, *, default: bool) -> bool:
        if isinstance(value, str):
            return value.strip().lower() not in {"", "0", "false", "off", "no"}
        if value is None:
            return default
        return bool(value)

    @staticmethod
    def _coerce_float(value, *, default: float) -> float:
        try:
            if isinstance(value, (int, float, str)):
                return float(value)
        except (TypeError, ValueError):
            pass
        return default

    @staticmethod
    def _coerce_int(value, *, default: int) -> int:
        try:
            if isinstance(value, (int, float, str)):
                return int(float(value))
        except (TypeError, ValueError):
            pass
        return default

    def _finite_float(self, value, *, default: float) -> float:
        result = self._coerce_float(value, default=default)
        if not math.isfinite(result):
            return default
        return result

    def _positive_float(self, value, *, default: float) -> float:
        result = self._finite_float(value, default=default)
        if result <= 0.0:
            return default
        return result

    @staticmethod
    def _normalise_choice(value, *, choices: tuple, default: str) -> str:
        if isinstance(value, str):
            candidate = value.strip()
            for choice in choices:
                if candidate.upper() == str(choice).upper():
                    return str(choice)
        return default

    def _parse_saved_stage_position(self, raw_position) -> SavedStagePositionSettings:
        """Normalise a persisted XYZ bookmark used by calibration workflows."""

        return self._saved_stage_position_from_config(
            parse_saved_stage_position(raw_position)
        )

    @staticmethod
    def _saved_stage_position_from_config(config) -> SavedStagePositionSettings:
        return SavedStagePositionSettings(
            x_mm=config.x_mm,
            y_mm=config.y_mm,
            z_mm=config.z_mm,
            configured=config.configured,
        )

    def _parse_coordinate_system(self, raw_coordinate_system) -> CoordinateSystemSettings:
        """Normalise persisted coordinate-system settings."""

        startup_mode = self.DEFAULT_COORDINATE_STARTUP_MODE
        preferred_system = self.DEFAULT_COORDINATE_SYSTEM
        position_mode = self.DEFAULT_POSITION_MODE
        if isinstance(raw_coordinate_system, dict):
            position_mode_raw = raw_coordinate_system.get(
                "position_mode", position_mode
            )
            if isinstance(position_mode_raw, str):
                position_mode = position_mode_raw.strip().lower()
            mode_raw = raw_coordinate_system.get("startup_mode", startup_mode)
            if isinstance(mode_raw, str):
                startup_mode = mode_raw.strip().lower()
            system_raw = raw_coordinate_system.get(
                "preferred_system", preferred_system
            )
            if isinstance(system_raw, str):
                preferred_system = system_raw.strip().upper()
        if position_mode not in {"work", "machine"}:
            position_mode = self.DEFAULT_POSITION_MODE
        if startup_mode not in {"controller", "fixed"}:
            startup_mode = self.DEFAULT_COORDINATE_STARTUP_MODE
        if preferred_system not in WORK_COORDINATE_SYSTEMS:
            preferred_system = self.DEFAULT_COORDINATE_SYSTEM
        return CoordinateSystemSettings(
            position_mode=position_mode,
            startup_mode=startup_mode,
            preferred_system=preferred_system,
        )

    def _parse_objectives(self, raw_objectives) -> ObjectivesSettings:
        """Normalise persisted objective profiles."""

        active_name = DEFAULT_ACTIVE_OBJECTIVE
        apply_offsets_on_change = True
        raw_profiles = None
        if isinstance(raw_objectives, dict):
            raw_active = raw_objectives.get("active_name", active_name)
            normalized_active = normalize_objective_name(raw_active)
            if normalized_active:
                active_name = normalized_active
            apply_offsets_on_change = self._coerce_bool(
                raw_objectives.get(
                    "apply_offsets_on_change",
                    apply_offsets_on_change,
                ),
                default=apply_offsets_on_change,
            )
            raw_profiles = raw_objectives.get("objectives")

        profiles: Dict[str, ObjectiveCalibrationSettings] = {}
        profile_map = raw_profiles if isinstance(raw_profiles, dict) else {}
        names: list[str] = []
        for raw_name in profile_map:
            name = normalize_objective_name(raw_name)
            if name and name not in names:
                names.append(name)
        if not names:
            names = list(OBJECTIVE_NAMES)
        if active_name not in names and profile_map:
            active_name = names[0]
        elif active_name not in names:
            names.append(active_name)
        for name in names:
            raw_profile = None
            for raw_key, candidate in profile_map.items():
                if normalize_objective_name(raw_key) == name:
                    raw_profile = candidate
                    break
            profiles[name] = self._parse_objective_profile(name, raw_profile)
        return ObjectivesSettings(
            active_name=active_name,
            apply_offsets_on_change=apply_offsets_on_change,
            objectives=profiles,
        )

    def _parse_objective_profile(
        self,
        name: str,
        raw_profile,
    ) -> ObjectiveCalibrationSettings:
        """Normalise one objective profile."""

        defaults = default_objective(name)
        if not isinstance(raw_profile, dict):
            return defaults
        matrix = self._parse_pixels_to_mm(raw_profile.get("pixels_to_mm"))
        xy_configured = self._coerce_bool(
            raw_profile.get("xy_calibration_configured", bool(matrix)),
            default=bool(matrix),
        )
        if not matrix:
            xy_configured = False
        return ObjectiveCalibrationSettings(
            name=name,
            magnification=self._positive_float(
                raw_profile.get("magnification", defaults.magnification),
                default=defaults.magnification,
            ),
            xy_offset_x_mm=self._finite_float(
                raw_profile.get("xy_offset_x_mm", defaults.xy_offset_x_mm),
                default=defaults.xy_offset_x_mm,
            ),
            xy_offset_y_mm=self._finite_float(
                raw_profile.get("xy_offset_y_mm", defaults.xy_offset_y_mm),
                default=defaults.xy_offset_y_mm,
            ),
            xy_offset_configured=self._coerce_bool(
                raw_profile.get(
                    "xy_offset_configured",
                    defaults.xy_offset_configured,
                ),
                default=defaults.xy_offset_configured,
            ),
            z_offset_mm=self._finite_float(
                raw_profile.get("z_offset_mm", defaults.z_offset_mm),
                default=defaults.z_offset_mm,
            ),
            z_offset_configured=self._coerce_bool(
                raw_profile.get(
                    "z_offset_configured",
                    defaults.z_offset_configured,
                ),
                default=defaults.z_offset_configured,
            ),
            pixels_to_mm=matrix,
            xy_calibration_configured=xy_configured,
            autofocus_range_mm=self._positive_float(
                raw_profile.get(
                    "autofocus_range_mm",
                    defaults.autofocus_range_mm,
                ),
                default=defaults.autofocus_range_mm,
            ),
            autofocus_fine_step_mm=self._positive_float(
                raw_profile.get(
                    "autofocus_fine_step_mm",
                    defaults.autofocus_fine_step_mm,
                ),
                default=defaults.autofocus_fine_step_mm,
            ),
        )

    def _parse_pixels_to_mm(self, raw_matrix) -> List[List[float]]:
        """Return a validated 2x2 pixels-to-mm matrix."""

        return parse_pixels_to_mm_matrix(raw_matrix)

    @classmethod
    def _should_keep_control_binding(cls, binding: KeyBinding) -> bool:
        text = (binding.text or "").strip()
        if not text:
            return True
        return cls.CYRILLIC_PATTERN.search(text) is None

    @staticmethod
    def _default_control_bindings(action: ControlAction) -> List[KeyBinding]:
        if action.default_qt_key <= 0:
            return []
        return [
            KeyBinding(
                qt_key=int(action.default_qt_key),
                modifiers=int(action.default_modifiers),
                text=str(action.default_text),
            )
        ]

    def _parse_feedrate_groups(
        self,
        raw_feedrates,
        legacy_presets,
    ) -> Tuple[FeedrateGroup, FeedrateGroup]:
        linear_config, rotary_config = parse_feedrate_groups(
            raw_feedrates,
            legacy_presets,
            linear_group=self.LINEAR_GROUP,
            rotary_group=self.ROTARY_GROUP,
            linear_defaults=self.DEFAULT_LINEAR_FEEDRATE_PRESETS,
            rotary_defaults=self.DEFAULT_ROTARY_FEEDRATE_PRESETS,
            default_feedrate=self.DEFAULT_FEEDRATE_DEFAULT,
            min_feedrate=self.MIN_FEEDRATE_MM_MIN,
        )
        return (
            FeedrateGroup(
                presets=linear_config.presets,
                default=linear_config.default,
            ),
            FeedrateGroup(
                presets=rotary_config.presets,
                default=rotary_config.default,
            ),
        )

    def _group_from_raw(
        self, raw_group, *, fallback: Tuple[float, ...]
    ) -> FeedrateGroup:
        """Build a feedrate group dataclass from persisted data."""

        config = feedrate_group_from_raw(
            raw_group,
            fallback=fallback,
            default_feedrate=self.DEFAULT_FEEDRATE_DEFAULT,
            min_feedrate=self.MIN_FEEDRATE_MM_MIN,
        )
        return FeedrateGroup(presets=config.presets, default=config.default)

    def _parse_feedrate_list(
        self, raw_presets, *, fallback: Tuple[float, ...]
    ) -> List[float]:
        """Normalise a preset list to positive unique floats preserving order."""

        return parse_feedrate_list(
            raw_presets,
            fallback=fallback,
            min_feedrate=self.MIN_FEEDRATE_MM_MIN,
        )

    def _normalise_feedrate_group(
        self, group: FeedrateGroup, *, fallback: Tuple[float, ...]
    ) -> FeedrateGroup:
        """Ensure the feedrate group contains valid presets and defaults."""

        config = normalise_feedrate_group(
            FeedrateGroupConfig(presets=list(group.presets), default=group.default),
            fallback=fallback,
            default_feedrate=self.DEFAULT_FEEDRATE_DEFAULT,
            min_feedrate=self.MIN_FEEDRATE_MM_MIN,
        )
        return FeedrateGroup(presets=config.presets, default=config.default)

    def _select_default(
        self, candidate: float, presets: List[float], *, fallback: Tuple[float, ...]
    ) -> float:
        """Choose a positive default value.

        The joystick feedrate slider is continuous, so the persisted default
        must not be forced back to one of the preset values.
        """

        return select_feedrate_default(
            candidate,
            default_feedrate=self.DEFAULT_FEEDRATE_DEFAULT,
            min_feedrate=self.MIN_FEEDRATE_MM_MIN,
        )

    def _normalise_settings(self, settings: Settings) -> Settings:
        """Return a copy of the settings with runtime values normalised."""

        clone = settings.clone()
        clone.api = self._parse_api(clone.api.to_dict())
        clone.telegram = self._parse_telegram(clone.telegram.to_dict())
        clone.feedrates = FeedrateSettings(
            linear=self._normalise_feedrate_group(
                clone.feedrates.linear, fallback=self.DEFAULT_LINEAR_FEEDRATE_PRESETS
            ),
            rotary=self._normalise_feedrate_group(
                clone.feedrates.rotary, fallback=self.DEFAULT_ROTARY_FEEDRATE_PRESETS
            ),
        )
        clone.oscillation = self._parse_oscillation(clone.oscillation.to_dict())
        clone.jog = self._parse_jog(clone.jog.to_dict())
        clone.click_to_move = self._parse_click_to_move(
            clone.click_to_move.to_dict()
        )
        clone.needle_calibration = self._parse_needle_calibration(
            clone.needle_calibration.to_dict()
        )
        clone.axis_a_calibration = self._parse_axis_a_calibration(
            clone.axis_a_calibration.to_dict()
        )
        clone.axis_z_calibration = self._parse_axis_z_calibration(
            clone.axis_z_calibration.to_dict()
        )
        clone.objectives = self._parse_objectives(clone.objectives.to_dict())
        clone.design_last_directory = clone.design_last_directory.strip()
        return clone

    def feedrate_group(self, motion_type: str) -> FeedrateGroup:
        """Return a feedrate group for the requested motion family."""

        if motion_type == self.LINEAR_GROUP:
            return self._settings.feedrates.linear.clone()
        if motion_type == self.ROTARY_GROUP:
            return self._settings.feedrates.rotary.clone()
        raise ValueError(f"Unknown feedrate motion type: {motion_type}")

    def feedrate_configuration(self) -> FeedrateSettings:
        """Return the full feedrate configuration clone."""

        return self._settings.feedrates.clone()

    def api_configuration(self) -> ApiSettings:
        """Return the current local API configuration clone."""

        return self._settings.api.clone()

    def telegram_configuration(self) -> TelegramSettings:
        """Return the current Telegram notification configuration clone."""

        return self._settings.telegram.clone()

    def jog_configuration(self) -> JogSettings:
        """Return the current jog configuration clone."""

        return self._settings.jog.clone()

    def oscillation_configuration(self) -> OscillationSettings:
        """Return the current oscillation configuration clone."""

        return self._settings.oscillation.clone()

    def needle_calibration_configuration(self) -> NeedleCalibrationSettings:
        """Return the current needle calibration configuration clone."""

        return self._settings.needle_calibration.clone()

    def axis_a_calibration_configuration(self) -> AxisACalibrationSettings:
        """Return the current compact A-axis calibration model clone."""

        return self._settings.axis_a_calibration.clone()

    def axis_z_calibration_configuration(self) -> AxisZCalibrationSettings:
        """Return the current compact Z-axis calibration model clone."""

        return self._settings.axis_z_calibration.clone()

    def coordinate_system_configuration(self) -> CoordinateSystemSettings:
        """Return the current coordinate-system configuration clone."""

        return self._settings.coordinate_system.clone()

    def objectives_configuration(self) -> ObjectivesSettings:
        """Return the current objective configuration clone."""

        return self._settings.objectives.clone()

    def active_objective_configuration(self) -> ObjectiveCalibrationSettings:
        """Return the active objective profile clone."""

        objectives = self._settings.objectives
        profile = objectives.objectives.get(objectives.active_name)
        if profile is None:
            profile = default_objective(objectives.active_name)
        return profile.clone()

    def design_last_directory(self) -> Path | None:
        """Return the most recently used design directory, if any."""

        raw_value = self._settings.design_last_directory.strip()
        if not raw_value:
            return None
        return Path(raw_value)

    def set_design_last_directory(self, directory: str | Path | None) -> None:
        """Persist the most recently used design directory."""

        if directory is None:
            new_value = ""
        else:
            path = Path(directory).expanduser()
            try:
                path = path.resolve()
            except OSError:
                pass
            new_value = str(path)
        if self._settings.design_last_directory == new_value:
            return
        updated = self._settings.clone()
        updated.design_last_directory = new_value
        self.replace(updated)
        self.save()

