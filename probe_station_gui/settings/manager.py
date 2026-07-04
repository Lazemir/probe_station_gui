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
from typing import Dict, Iterable, List

from probe_station_gui.settings.axis_calibration_config import (
    AxisACalibrationConfig,
    AxisACalibrationSettings,
    AxisZCalibrationConfig,
    AxisZCalibrationSettings,
    parse_axis_a_calibration,
    parse_axis_z_calibration,
)
from probe_station_gui.settings.controls_config import (
    CONTROL_ACTIONS,
    ControlAction,
    KeyBinding,
)
from probe_station_gui.settings.default_file import normalize_default_settings_data
from probe_station_gui.settings.feedrate_config import (
    FeedrateGroup,
    FeedrateSettings,
    feedrate_group_from_config,
    normalise_feedrate_settings,
    parse_feedrate_groups,
)
from probe_station_gui.stage.fluidnc_protocol import parse_fluidnc_axis_max_feedrates
from probe_station_gui.settings.jog_config import (
    JogSettings,
    JogSettingsDefaults,
    parse_jog_settings,
)
from probe_station_gui.shared.logging_config import configure_logging
from probe_station_gui.settings.needle_calibration_config import (
    LCR_APERTURE_RATES,
    LCR_LEVEL_MODES,
    LCR_MEASUREMENT_FUNCTIONS,
    LCR_METER_TYPE_GWINSTEK,
    LCR_METER_TYPE_KEITHLEY,
    LCR_METER_TYPE_KEITHLEY_2400,
    LCR_METER_TYPES,
    LCR_MONITOR_PARAMETERS,
    LCR_RANGE_MODES,
    LCR_SOURCE_RESISTANCES_OHM,
    LCR_TRIGGER_SOURCES,
    NeedleCalibrationSettings,
    SavedStagePositionSettings,
    parse_needle_calibration_preferences,
)
from probe_station_gui.settings.objective_config import (
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
    default_objective,
    ordered_objective_names,
    parse_objectives_settings,
)
from probe_station_gui.settings.oscillation_config import (
    OscillationSettings,
    OscillationSettingsDefaults,
    parse_oscillation_settings,
)
from probe_station_gui.settings.value_parsing import (
    coerce_bool,
    finite_float,
)
from probe_station_gui.settings.section_parsing import (
    parse_api_settings,
    parse_coordinate_system_settings,
    parse_logging_settings,
)
from probe_station_gui.settings.sections import (
    ApiSettings,
    ClickToMoveSettings,
    CoordinateSystemSettings,
    LoggingSettings,
    WORK_COORDINATE_SYSTEMS,
)
from probe_station_gui.notifications.telegram_settings import (
    TELEGRAM_ALERT_TYPES,
    TelegramSettings,
    parse_telegram_alerts,
)
from probe_station_gui.notifications.telegram import (
    load_global_bot_token,
    save_global_bot_token,
)

logger = logging.getLogger(__name__)



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

        data = normalize_default_settings_data(data, log_path=log_path)

        with self._config_path.open("w", encoding="utf-8") as target:
            json.dump(data, target, indent=2, ensure_ascii=False)

        self._logger.info("Default settings copied to %s", self._config_path)

    def _load(self) -> Settings:
        """Load settings from disk and normalise the structure."""

        with self._config_path.open("r", encoding="utf-8-sig") as handle:
            raw = json.load(handle)
        self._logger.debug("Loaded settings from %s", self._config_path)
        return self._settings_from_raw(raw)

    def _settings_from_raw(self, raw: object) -> Settings:
        controls = self._load_controls(raw)
        logging_settings = self._parse_logging(
            self._raw_section(raw, "logging", default={})
        )
        if not logging_settings.file:
            default_log = str(self._default_log_path())
            logging_settings.file = default_log
            self._logger.debug(
                "Log file path missing in settings; defaulting to %s", default_log
            )
        feedrates = self._parse_feedrates(
            self._raw_section(raw, "feedrates"),
            self._raw_section(raw, "feedrate_presets"),
        )
        return Settings(
            controls=controls,
            logging=logging_settings,
            api=self._parse_api(self._raw_section(raw, "api")),
            telegram=self._parse_telegram(self._raw_section(raw, "telegram")),
            feedrates=feedrates,
            oscillation=self._parse_oscillation(self._raw_section(raw, "oscillation")),
            jog=self._parse_jog(self._raw_section(raw, "jog")),
            click_to_move=self._parse_click_to_move(
                self._raw_section(raw, "click_to_move")
            ),
            needle_calibration=self._parse_needle_calibration(
                self._raw_section(raw, "needle_calibration")
            ),
            axis_a_calibration=self._parse_axis_a_calibration(
                self._raw_section(raw, "axis_a_calibration")
            ),
            axis_z_calibration=self._parse_axis_z_calibration(
                self._raw_section(raw, "axis_z_calibration")
            ),
            coordinate_system=self._parse_coordinate_system(
                self._raw_section(raw, "coordinate_system")
            ),
            objectives=parse_objectives_settings(self._raw_section(raw, "objectives")),
            design_last_directory=self._design_last_directory_from_raw(raw),
        )

    def _load_controls(self, raw: object) -> Dict[str, List[KeyBinding]]:
        controls_raw = self._raw_section(raw, "controls", default={})
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
        return controls

    @staticmethod
    def _raw_section(raw: object, key: str, *, default: object = None) -> object:
        if isinstance(raw, dict):
            return raw.get(key, default)
        return default

    @staticmethod
    def _design_last_directory_from_raw(raw: object) -> str:
        if not isinstance(raw, dict):
            return ""
        value = raw.get("design_last_directory", "")
        if isinstance(value, str):
            return value.strip()
        return ""

    def _parse_logging(self, raw_logging) -> LoggingSettings:
        """Create a logging configuration from persisted data."""

        return LoggingSettings(**parse_logging_settings(raw_logging))

    def _parse_api(self, raw_api) -> ApiSettings:
        """Normalise local API settings."""

        defaults = ApiSettings()
        return ApiSettings(
            **parse_api_settings(
                raw_api,
                default_enabled=defaults.enabled,
                default_host=defaults.host,
                default_port=defaults.port,
            )
        )

    def _parse_telegram(self, raw_telegram) -> TelegramSettings:
        """Normalise Telegram notification settings."""

        settings = TelegramSettings()
        if isinstance(raw_telegram, dict):
            settings.enabled = coerce_bool(
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
        return parse_telegram_alerts(raw_alerts)

    def _parse_feedrates(self, raw_feedrates, legacy_presets) -> FeedrateSettings:
        """Normalise persisted feedrate data supporting legacy layouts."""

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
        return FeedrateSettings(
            linear=feedrate_group_from_config(linear_config),
            rotary=feedrate_group_from_config(rotary_config),
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
            timeout_s = finite_float(
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

        return parse_needle_calibration_preferences(
            raw_needle_calibration,
            min_feedrate_mm_min=self.MIN_FEEDRATE_MM_MIN,
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

    def _parse_coordinate_system(self, raw_coordinate_system) -> CoordinateSystemSettings:
        """Normalise persisted coordinate-system settings."""

        return CoordinateSystemSettings(
            **parse_coordinate_system_settings(
                raw_coordinate_system,
                default_position_mode=self.DEFAULT_POSITION_MODE,
                default_startup_mode=self.DEFAULT_COORDINATE_STARTUP_MODE,
                default_coordinate_system=self.DEFAULT_COORDINATE_SYSTEM,
                work_coordinate_systems=WORK_COORDINATE_SYSTEMS,
            )
        )

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

    def _normalise_settings(self, settings: Settings) -> Settings:
        """Return a copy of the settings with runtime values normalised."""

        clone = settings.clone()
        clone.api = self._parse_api(clone.api.to_dict())
        clone.telegram = self._parse_telegram(clone.telegram.to_dict())
        clone.feedrates = normalise_feedrate_settings(
            clone.feedrates,
            linear_defaults=self.DEFAULT_LINEAR_FEEDRATE_PRESETS,
            rotary_defaults=self.DEFAULT_ROTARY_FEEDRATE_PRESETS,
            default_feedrate=self.DEFAULT_FEEDRATE_DEFAULT,
            min_feedrate=self.MIN_FEEDRATE_MM_MIN,
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
        clone.objectives = parse_objectives_settings(clone.objectives.to_dict())
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

