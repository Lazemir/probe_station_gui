"""Settings loading and persistence for the probe station GUI."""

from __future__ import annotations

import json
import logging
import os
import platform
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from probe_station_gui.logging_config import configure_logging


@dataclass(frozen=True)
class ControlAction:
    """Describe a logical control action exposed in the UI."""

    key: str
    axis: str
    direction: int
    label: str


CONTROL_ACTIONS: tuple[ControlAction, ...] = (
    ControlAction("move_y_positive", "Y", 1, "Move Up"),
    ControlAction("move_y_negative", "Y", -1, "Move Down"),
    ControlAction("move_x_negative", "X", -1, "Move Left"),
    ControlAction("move_x_positive", "X", 1, "Move Right"),
)

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

    linear_distance_mm: float = 25.0
    rotary_distance_deg: float = 5.0

    def clone(self) -> "JogSettings":
        """Return a copy of the jog preferences."""

        return JogSettings(
            linear_distance_mm=self.linear_distance_mm,
            rotary_distance_deg=self.rotary_distance_deg,
        )

    def to_dict(self) -> dict[str, float]:
        """Serialize the jog preferences."""

        return {
            "linear_distance_mm": self.linear_distance_mm,
            "rotary_distance_deg": self.rotary_distance_deg,
        }


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
    """Configuration for needle calibration and the external LCR meter."""

    visa_resource: str = "COM4"
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
            visa_resource=self.visa_resource,
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
            down_position_mm=self.down_position_mm,
            down_position_configured=self.down_position_configured,
            chip_position=self.chip_position.clone(),
            stone_position=self.stone_position.clone(),
        )

    def to_dict(self) -> dict[str, float | int | str | bool | dict[str, float | bool]]:
        """Serialize the needle calibration preferences."""

        return {
            "visa_resource": self.visa_resource,
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
            "down_position_mm": self.down_position_mm,
            "down_position_configured": self.down_position_configured,
            "chip_position": self.chip_position.to_dict(),
            "stone_position": self.stone_position.to_dict(),
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
class Settings:
    """Container for all configurable values."""

    controls: Dict[str, List[KeyBinding]] = field(default_factory=dict)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    feedrates: FeedrateSettings = field(default_factory=FeedrateSettings)
    oscillation: OscillationSettings = field(default_factory=OscillationSettings)
    jog: JogSettings = field(default_factory=JogSettings)
    needle_calibration: NeedleCalibrationSettings = field(
        default_factory=NeedleCalibrationSettings
    )
    coordinate_system: CoordinateSystemSettings = field(
        default_factory=CoordinateSystemSettings
    )
    design_last_directory: str = ""

    def clone(self) -> "Settings":
        """Create a deep copy of the settings container."""

        return Settings(
            controls={key: list(value) for key, value in self.controls.items()},
            logging=self.logging.clone(),
            feedrates=self.feedrates.clone(),
            oscillation=self.oscillation.clone(),
            jog=self.jog.clone(),
            needle_calibration=self.needle_calibration.clone(),
            coordinate_system=self.coordinate_system.clone(),
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
            "needle_calibration": self.needle_calibration.to_dict(),
            "coordinate_system": self.coordinate_system.to_dict(),
            "design_last_directory": self.design_last_directory,
        }


class SettingsManager:
    """Load, persist, and expose user configurable settings."""

    CONFIG_FILENAME = "settings.json"
    CONTROLLER_STATE_FILENAME = "controller-state.json"
    DEFAULT_LOG_FILENAME = "probe-station-gui.log"
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
    DEFAULT_LINEAR_JOG_DISTANCE_MM: float = 25.0
    DEFAULT_ROTARY_JOG_DISTANCE_DEG: float = 5.0
    DEFAULT_LCR_VISA_RESOURCE: str = "COM4"
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
            controls.setdefault(action.key, [])
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
            with path.open("r", encoding="utf-8") as handle:
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
                "linear_distance_mm": self.DEFAULT_LINEAR_JOG_DISTANCE_MM,
                "rotary_distance_deg": self.DEFAULT_ROTARY_JOG_DISTANCE_DEG,
            }
            data["jog"] = jog_section
        else:
            jog_section.setdefault(
                "linear_distance_mm", self.DEFAULT_LINEAR_JOG_DISTANCE_MM
            )
            jog_section.setdefault(
                "rotary_distance_deg", self.DEFAULT_ROTARY_JOG_DISTANCE_DEG
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
                "visa_resource": self.DEFAULT_LCR_VISA_RESOURCE,
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
            needle_section.setdefault("visa_resource", self.DEFAULT_LCR_VISA_RESOURCE)
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

        design_last_directory = data.get("design_last_directory")
        if not isinstance(design_last_directory, str):
            data["design_last_directory"] = ""

        with self._config_path.open("w", encoding="utf-8") as target:
            json.dump(data, target, indent=2, ensure_ascii=False)

        self._logger.info("Default settings copied to %s", self._config_path)

    def _load(self) -> Settings:
        """Load settings from disk and normalise the structure."""

        with self._config_path.open("r", encoding="utf-8") as handle:
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
        for action in CONTROL_ACTIONS:
            controls.setdefault(action.key, [])
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
        oscillation_raw = raw.get("oscillation") if isinstance(raw, dict) else None
        jog_raw = raw.get("jog") if isinstance(raw, dict) else None
        needle_calibration_raw = (
            raw.get("needle_calibration") if isinstance(raw, dict) else None
        )
        coordinate_system_raw = (
            raw.get("coordinate_system") if isinstance(raw, dict) else None
        )
        design_last_directory = ""
        if isinstance(raw, dict):
            design_last_directory_raw = raw.get("design_last_directory", "")
            if isinstance(design_last_directory_raw, str):
                design_last_directory = design_last_directory_raw.strip()
        return Settings(
            controls=controls,
            logging=logging_settings,
            feedrates=feedrates,
            oscillation=self._parse_oscillation(oscillation_raw),
            jog=self._parse_jog(jog_raw),
            needle_calibration=self._parse_needle_calibration(needle_calibration_raw),
            coordinate_system=self._parse_coordinate_system(coordinate_system_raw),
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

        linear_distance = self.DEFAULT_LINEAR_JOG_DISTANCE_MM
        rotary_distance = self.DEFAULT_ROTARY_JOG_DISTANCE_DEG
        if isinstance(raw_jog, dict):
            candidate = raw_jog.get("linear_distance_mm", linear_distance)
            try:
                if isinstance(candidate, (int, float, str)):
                    linear_distance = float(candidate)
            except (TypeError, ValueError):
                linear_distance = self.DEFAULT_LINEAR_JOG_DISTANCE_MM
            candidate = raw_jog.get("rotary_distance_deg", rotary_distance)
            try:
                if isinstance(candidate, (int, float, str)):
                    rotary_distance = float(candidate)
            except (TypeError, ValueError):
                rotary_distance = self.DEFAULT_ROTARY_JOG_DISTANCE_DEG
        if linear_distance <= 0:
            linear_distance = self.DEFAULT_LINEAR_JOG_DISTANCE_MM
        if rotary_distance <= 0:
            rotary_distance = self.DEFAULT_ROTARY_JOG_DISTANCE_DEG
        return JogSettings(
            linear_distance_mm=linear_distance,
            rotary_distance_deg=rotary_distance,
        )

    def _parse_oscillation(self, raw_oscillation) -> OscillationSettings:
        """Normalise persisted oscillation-panel settings."""

        mode = self.DEFAULT_OSCILLATION_MODE
        amplitude_mm = self.DEFAULT_OSCILLATION_AMPLITUDE_MM
        feedrate_mm_min = self.DEFAULT_OSCILLATION_FEEDRATE_MM_MIN
        turns_per_sweep = self.DEFAULT_OSCILLATION_TURNS_PER_SWEEP
        if isinstance(raw_oscillation, dict):
            mode_candidate = raw_oscillation.get("mode", mode)
            if isinstance(mode_candidate, str):
                mode = mode_candidate.strip().upper() or mode
            for key, default in (
                ("amplitude_mm", amplitude_mm),
                ("feedrate_mm_min", feedrate_mm_min),
                ("turns_per_sweep", turns_per_sweep),
            ):
                candidate = raw_oscillation.get(key, default)
                try:
                    if isinstance(candidate, (int, float, str)):
                        value = float(candidate)
                    else:
                        value = default
                except (TypeError, ValueError):
                    value = default
                if key == "amplitude_mm":
                    amplitude_mm = value
                elif key == "feedrate_mm_min":
                    feedrate_mm_min = value
                else:
                    turns_per_sweep = value
        if mode not in {"X", "Y", "SPIRAL"}:
            mode = self.DEFAULT_OSCILLATION_MODE
        if amplitude_mm <= 0:
            amplitude_mm = self.DEFAULT_OSCILLATION_AMPLITUDE_MM
        if feedrate_mm_min <= 0:
            feedrate_mm_min = self.DEFAULT_OSCILLATION_FEEDRATE_MM_MIN
        if turns_per_sweep <= 0:
            turns_per_sweep = self.DEFAULT_OSCILLATION_TURNS_PER_SWEEP
        return OscillationSettings(
            mode=mode,
            amplitude_mm=amplitude_mm,
            feedrate_mm_min=feedrate_mm_min,
            turns_per_sweep=turns_per_sweep,
        )

    def _parse_needle_calibration(
        self, raw_needle_calibration
    ) -> NeedleCalibrationSettings:
        """Normalise persisted needle calibration settings."""

        visa_resource = self.DEFAULT_LCR_VISA_RESOURCE
        measurement_function = self.DEFAULT_LCR_MEASUREMENT_FUNCTION
        range_mode = self.DEFAULT_LCR_RANGE_MODE
        auto_range_enabled = self.DEFAULT_LCR_AUTO_RANGE_ENABLED
        impedance_range = self.DEFAULT_LCR_IMPEDANCE_RANGE
        dcr_range = self.DEFAULT_LCR_DCR_RANGE
        frequency_hz = self.DEFAULT_LCR_FREQUENCY_HZ
        level_mode = self.DEFAULT_LCR_LEVEL_MODE
        voltage_level_v = self.DEFAULT_LCR_VOLTAGE_LEVEL_V
        current_level_a = self.DEFAULT_LCR_CURRENT_LEVEL_A
        source_resistance_ohm = self.DEFAULT_LCR_SOURCE_RESISTANCE_OHM
        aperture_rate = self.DEFAULT_LCR_APERTURE_RATE
        aperture_averages = self.DEFAULT_LCR_APERTURE_AVERAGES
        trigger_source = self.DEFAULT_LCR_TRIGGER_SOURCE
        trigger_delay_s = self.DEFAULT_LCR_TRIGGER_DELAY_S
        bias_enabled = self.DEFAULT_LCR_BIAS_ENABLED
        bias_level_v = self.DEFAULT_LCR_BIAS_LEVEL_V
        monitor1 = self.DEFAULT_LCR_MONITOR
        monitor2 = self.DEFAULT_LCR_MONITOR
        alc_enabled = self.DEFAULT_LCR_ALC_ENABLED
        short_threshold_ohm = self.DEFAULT_SHORT_THRESHOLD_OHM
        poll_interval_ms = self.DEFAULT_LCR_POLL_INTERVAL_MS
        down_position_mm = 0.0
        down_position_configured = False
        chip_position = SavedStagePositionSettings()
        stone_position = SavedStagePositionSettings()
        if isinstance(raw_needle_calibration, dict):
            resource_raw = raw_needle_calibration.get("visa_resource", visa_resource)
            if isinstance(resource_raw, str):
                visa_resource = resource_raw.strip()
            measurement_function = self._normalise_choice(
                raw_needle_calibration.get(
                    "measurement_function", measurement_function
                ),
                choices=LCR_MEASUREMENT_FUNCTIONS,
                default=measurement_function,
            )
            range_mode = self._normalise_choice(
                raw_needle_calibration.get("range_mode", range_mode),
                choices=LCR_RANGE_MODES,
                default=range_mode,
            )
            auto_range_raw = raw_needle_calibration.get(
                "auto_range_enabled", auto_range_enabled
            )
            if isinstance(auto_range_raw, str):
                auto_range_enabled = auto_range_raw.strip().lower() not in {
                    "",
                    "0",
                    "false",
                    "off",
                    "no",
                }
            else:
                auto_range_enabled = bool(auto_range_raw)
            auto_range_enabled = range_mode == "AUTO"
            impedance_range = self._coerce_int(
                raw_needle_calibration.get("impedance_range", impedance_range),
                default=self.DEFAULT_LCR_IMPEDANCE_RANGE,
            )
            candidate = raw_needle_calibration.get("dcr_range", dcr_range)
            try:
                dcr_range = int(float(candidate))
            except (TypeError, ValueError):
                dcr_range = self.DEFAULT_LCR_DCR_RANGE
            frequency_hz = self._coerce_float(
                raw_needle_calibration.get("frequency_hz", frequency_hz),
                default=self.DEFAULT_LCR_FREQUENCY_HZ,
            )
            level_mode = self._normalise_choice(
                raw_needle_calibration.get("level_mode", level_mode),
                choices=LCR_LEVEL_MODES,
                default=level_mode,
            )
            voltage_level_v = self._coerce_float(
                raw_needle_calibration.get("voltage_level_v", voltage_level_v),
                default=self.DEFAULT_LCR_VOLTAGE_LEVEL_V,
            )
            current_level_a = self._coerce_float(
                raw_needle_calibration.get("current_level_a", current_level_a),
                default=self.DEFAULT_LCR_CURRENT_LEVEL_A,
            )
            source_resistance_ohm = self._coerce_int(
                raw_needle_calibration.get(
                    "source_resistance_ohm", source_resistance_ohm
                ),
                default=self.DEFAULT_LCR_SOURCE_RESISTANCE_OHM,
            )
            aperture_rate = self._normalise_choice(
                raw_needle_calibration.get("aperture_rate", aperture_rate),
                choices=LCR_APERTURE_RATES,
                default=aperture_rate,
            )
            aperture_averages = self._coerce_int(
                raw_needle_calibration.get("aperture_averages", aperture_averages),
                default=self.DEFAULT_LCR_APERTURE_AVERAGES,
            )
            trigger_source = self._normalise_choice(
                raw_needle_calibration.get("trigger_source", trigger_source),
                choices=LCR_TRIGGER_SOURCES,
                default=trigger_source,
            )
            trigger_delay_s = self._coerce_float(
                raw_needle_calibration.get("trigger_delay_s", trigger_delay_s),
                default=self.DEFAULT_LCR_TRIGGER_DELAY_S,
            )
            bias_enabled = self._coerce_bool(
                raw_needle_calibration.get("bias_enabled", bias_enabled),
                default=self.DEFAULT_LCR_BIAS_ENABLED,
            )
            bias_level_v = self._coerce_float(
                raw_needle_calibration.get("bias_level_v", bias_level_v),
                default=self.DEFAULT_LCR_BIAS_LEVEL_V,
            )
            monitor1 = self._normalise_choice(
                raw_needle_calibration.get("monitor1", monitor1),
                choices=LCR_MONITOR_PARAMETERS,
                default=monitor1,
            )
            monitor2 = self._normalise_choice(
                raw_needle_calibration.get("monitor2", monitor2),
                choices=LCR_MONITOR_PARAMETERS,
                default=monitor2,
            )
            alc_enabled = self._coerce_bool(
                raw_needle_calibration.get("alc_enabled", alc_enabled),
                default=self.DEFAULT_LCR_ALC_ENABLED,
            )
            candidate = raw_needle_calibration.get(
                "short_threshold_ohm", short_threshold_ohm
            )
            try:
                if isinstance(candidate, (int, float, str)):
                    short_threshold_ohm = float(candidate)
            except (TypeError, ValueError):
                short_threshold_ohm = self.DEFAULT_SHORT_THRESHOLD_OHM
            candidate = raw_needle_calibration.get(
                "poll_interval_ms", poll_interval_ms
            )
            try:
                if isinstance(candidate, (int, float, str)):
                    poll_interval_ms = int(float(candidate))
            except (TypeError, ValueError):
                poll_interval_ms = self.DEFAULT_LCR_POLL_INTERVAL_MS
            candidate = raw_needle_calibration.get(
                "down_position_mm", down_position_mm
            )
            try:
                if isinstance(candidate, (int, float, str)):
                    down_position_mm = float(candidate)
            except (TypeError, ValueError):
                down_position_mm = 0.0
            down_position_configured = bool(
                raw_needle_calibration.get(
                    "down_position_configured", down_position_configured
                )
            )
            chip_position = self._parse_saved_stage_position(
                raw_needle_calibration.get("chip_position")
            )
            stone_position = self._parse_saved_stage_position(
                raw_needle_calibration.get("stone_position")
            )
        if impedance_range < 0 or impedance_range > 8:
            impedance_range = self.DEFAULT_LCR_IMPEDANCE_RANGE
        if dcr_range < 0 or dcr_range > 8:
            dcr_range = self.DEFAULT_LCR_DCR_RANGE
        if frequency_hz < 10:
            frequency_hz = self.DEFAULT_LCR_FREQUENCY_HZ
        if voltage_level_v < 0.01 or voltage_level_v > 2.0:
            voltage_level_v = self.DEFAULT_LCR_VOLTAGE_LEVEL_V
        if current_level_a < 0.0001 or current_level_a > 0.02:
            current_level_a = self.DEFAULT_LCR_CURRENT_LEVEL_A
        if source_resistance_ohm not in LCR_SOURCE_RESISTANCES_OHM:
            source_resistance_ohm = self.DEFAULT_LCR_SOURCE_RESISTANCE_OHM
        if aperture_averages < 1 or aperture_averages > 256:
            aperture_averages = self.DEFAULT_LCR_APERTURE_AVERAGES
        if trigger_delay_s < 0 or trigger_delay_s > 60:
            trigger_delay_s = self.DEFAULT_LCR_TRIGGER_DELAY_S
        if bias_level_v < -2.5 or bias_level_v > 2.5:
            bias_level_v = self.DEFAULT_LCR_BIAS_LEVEL_V
        if short_threshold_ohm < 0:
            short_threshold_ohm = self.DEFAULT_SHORT_THRESHOLD_OHM
        if poll_interval_ms < 50:
            poll_interval_ms = self.DEFAULT_LCR_POLL_INTERVAL_MS
        auto_range_enabled = range_mode == "AUTO"
        return NeedleCalibrationSettings(
            visa_resource=visa_resource,
            measurement_function=measurement_function,
            range_mode=range_mode,
            auto_range_enabled=auto_range_enabled,
            impedance_range=impedance_range,
            dcr_range=dcr_range,
            frequency_hz=frequency_hz,
            level_mode=level_mode,
            voltage_level_v=voltage_level_v,
            current_level_a=current_level_a,
            source_resistance_ohm=source_resistance_ohm,
            aperture_rate=aperture_rate,
            aperture_averages=aperture_averages,
            trigger_source=trigger_source,
            trigger_delay_s=trigger_delay_s,
            bias_enabled=bias_enabled,
            bias_level_v=bias_level_v,
            monitor1=monitor1,
            monitor2=monitor2,
            alc_enabled=alc_enabled,
            short_threshold_ohm=short_threshold_ohm,
            poll_interval_ms=poll_interval_ms,
            down_position_mm=down_position_mm,
            down_position_configured=down_position_configured,
            chip_position=chip_position,
            stone_position=stone_position,
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

        x_mm = 0.0
        y_mm = 0.0
        z_mm = 0.0
        configured = False
        if isinstance(raw_position, dict):
            for key, default in (("x_mm", 0.0), ("y_mm", 0.0), ("z_mm", 0.0)):
                candidate = raw_position.get(key, default)
                try:
                    if isinstance(candidate, (int, float, str)):
                        value = float(candidate)
                    else:
                        value = default
                except (TypeError, ValueError):
                    value = default
                if key == "x_mm":
                    x_mm = value
                elif key == "y_mm":
                    y_mm = value
                else:
                    z_mm = value
            configured = bool(raw_position.get("configured", configured))
        return SavedStagePositionSettings(
            x_mm=x_mm,
            y_mm=y_mm,
            z_mm=z_mm,
            configured=configured,
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

    @classmethod
    def _should_keep_control_binding(cls, binding: KeyBinding) -> bool:
        text = (binding.text or "").strip()
        if not text:
            return True
        return cls.CYRILLIC_PATTERN.search(text) is None

    def _parse_feedrate_groups(
        self,
        raw_feedrates,
        legacy_presets,
    ) -> Tuple[FeedrateGroup, FeedrateGroup]:
        linear_defaults = self.DEFAULT_LINEAR_FEEDRATE_PRESETS
        rotary_defaults = self.DEFAULT_ROTARY_FEEDRATE_PRESETS
        presets_fallback = self._parse_feedrate_list(
            legacy_presets,
            fallback=linear_defaults,
        )
        legacy_raw_present = bool(legacy_presets)
        rotary_fallback_defaults = (
            tuple(presets_fallback)
            if legacy_raw_present
            else tuple(rotary_defaults)
        )
        if not isinstance(raw_feedrates, dict):
            linear = self._normalise_feedrate_group(
                FeedrateGroup(
                    presets=presets_fallback, default=self.DEFAULT_FEEDRATE_DEFAULT
                ),
                fallback=linear_defaults,
            )
            rotary_source = list(rotary_fallback_defaults)
            rotary = self._normalise_feedrate_group(
                FeedrateGroup(
                    presets=rotary_source, default=self.DEFAULT_FEEDRATE_DEFAULT
                ),
                fallback=rotary_defaults,
            )
            return linear, rotary

        linear_raw = raw_feedrates.get(self.LINEAR_GROUP)
        rotary_raw = raw_feedrates.get(self.ROTARY_GROUP)
        linear = self._normalise_feedrate_group(
            self._group_from_raw(linear_raw, fallback=linear_defaults),
            fallback=linear_defaults,
        )
        rotary = self._normalise_feedrate_group(
            self._group_from_raw(
                rotary_raw, fallback=rotary_fallback_defaults
            ),
            fallback=rotary_defaults,
        )
        return linear, rotary

    def _group_from_raw(
        self, raw_group, *, fallback: Tuple[float, ...]
    ) -> FeedrateGroup:
        """Build a feedrate group dataclass from persisted data."""

        presets = []
        default = self.DEFAULT_FEEDRATE_DEFAULT
        if isinstance(raw_group, dict):
            presets = self._parse_feedrate_list(
                raw_group.get("presets"), fallback=fallback
            )
            default_raw = raw_group.get("default")
            try:
                if isinstance(default_raw, (int, float, str)):
                    default = float(default_raw)
            except (TypeError, ValueError):
                default = self.DEFAULT_FEEDRATE_DEFAULT
        else:
            presets = list(fallback)
        return FeedrateGroup(presets=presets, default=default)

    def _parse_feedrate_list(
        self, raw_presets, *, fallback: Tuple[float, ...]
    ) -> List[float]:
        """Normalise a preset list to positive unique floats preserving order."""

        parsed: List[float] = []
        seen: set[float] = set()
        if isinstance(raw_presets, Iterable) and not isinstance(raw_presets, (str, bytes)):
            for value in raw_presets:
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if number <= 0:
                    continue
                key = round(number, 9)
                if key in seen:
                    continue
                seen.add(key)
                parsed.append(number)
        if not parsed:
            parsed = list(fallback)
        return parsed

    def _normalise_feedrate_group(
        self, group: FeedrateGroup, *, fallback: Tuple[float, ...]
    ) -> FeedrateGroup:
        """Ensure the feedrate group contains valid presets and defaults."""

        presets = self._parse_feedrate_list(group.presets, fallback=fallback)
        presets.sort()
        default_value = group.default if group.default > 0 else self.DEFAULT_FEEDRATE_DEFAULT
        default_text = self._select_default(default_value, presets, fallback=fallback)
        return FeedrateGroup(presets=presets, default=default_text)

    def _select_default(
        self, candidate: float, presets: List[float], *, fallback: Tuple[float, ...]
    ) -> float:
        """Choose a default value from the preset list."""

        try:
            candidate_value = float(candidate)
        except (TypeError, ValueError):
            candidate_value = self.DEFAULT_FEEDRATE_DEFAULT

        if candidate_value <= 0:
            candidate_value = self.DEFAULT_FEEDRATE_DEFAULT

        if presets:
            for value in presets:
                if abs(value - candidate_value) <= 1e-9:
                    return value
            return presets[0]

        return fallback[0] if fallback else self.DEFAULT_FEEDRATE_DEFAULT

    def _normalise_settings(self, settings: Settings) -> Settings:
        """Return a copy of the settings with runtime values normalised."""

        clone = settings.clone()
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
        clone.needle_calibration = self._parse_needle_calibration(
            clone.needle_calibration.to_dict()
        )
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

    def jog_configuration(self) -> JogSettings:
        """Return the current jog configuration clone."""

        return self._settings.jog.clone()

    def oscillation_configuration(self) -> OscillationSettings:
        """Return the current oscillation configuration clone."""

        return self._settings.oscillation.clone()

    def needle_calibration_configuration(self) -> NeedleCalibrationSettings:
        """Return the current needle calibration configuration clone."""

        return self._settings.needle_calibration.clone()

    def coordinate_system_configuration(self) -> CoordinateSystemSettings:
        """Return the current coordinate-system configuration clone."""

        return self._settings.coordinate_system.clone()

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

