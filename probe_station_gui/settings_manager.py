"""Settings loading and persistence for the probe station GUI."""

from __future__ import annotations

import json
import logging
import os
import platform
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Dict, Iterable, List

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
    ControlAction("rotate_b_negative", "B", -1, "Rotate B ↻ (clockwise)"),
    ControlAction("rotate_b_positive", "B", 1, "Rotate B ↺ (counter-clockwise)"),
)


@dataclass(eq=True, frozen=True)
class KeyBinding:
    """Representation of a single captured key binding."""

    qt_key: int
    modifiers: int = 0
    text: str = ""

    def to_dict(self) -> dict[str, int | str]:
        """Serialize the binding for persistence."""

        return {"qt_key": self.qt_key, "modifiers": self.modifiers, "text": self.text}

    @staticmethod
    def from_dict(data: dict) -> "KeyBinding":
        """Deserialize a binding from JSON data."""

        return KeyBinding(
            qt_key=int(data.get("qt_key", 0)),
            modifiers=int(data.get("modifiers", 0)),
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
class Settings:
    """Container for all configurable values."""

    controls: Dict[str, List[KeyBinding]] = field(default_factory=dict)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    feedrate_presets: List[float] = field(default_factory=list)

    def clone(self) -> "Settings":
        """Create a deep copy of the settings container."""

        return Settings(
            controls={key: list(value) for key, value in self.controls.items()},
            logging=self.logging.clone(),
            feedrate_presets=list(self.feedrate_presets),
        )

    def to_dict(self) -> dict:
        """Convert the settings into a JSON serializable structure."""

        return {
            "controls": {
                key: [binding.to_dict() for binding in bindings]
                for key, bindings in self.controls.items()
            },
            "logging": self.logging.to_dict(),
            "feedrate_presets": self.feedrate_presets,
        }


class SettingsManager:
    """Load, persist, and expose user configurable settings."""

    CONFIG_FILENAME = "settings.json"
    DEFAULT_LOG_FILENAME = "probe-station-gui.log"
    DEFAULT_FEEDRATE_PRESETS: tuple[float, ...] = (0.01, 0.1, 1.0, 10.0, 100.0)

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

        self._settings = settings
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

        return (self._settings.logging.level or "INFO").upper()

    def log_file_path(self) -> Path:
        """Return the resolved log file path based on the settings."""

        file_setting = (self._settings.logging.file or "").strip()
        if file_setting:
            path = Path(file_setting)
            if not path.is_absolute():
                path = self._config_dir / path
        else:
            path = self._config_dir / self.DEFAULT_LOG_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

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

    def _ensure_default_file(self) -> None:
        """Copy the default settings file when the user configuration is missing."""

        if self._config_path.exists():
            return
        self._config_dir.mkdir(parents=True, exist_ok=True)
        default_resource = resources.files("probe_station_gui").joinpath(
            "default_settings.json"
        )
        log_path = str(self._config_dir / self.DEFAULT_LOG_FILENAME)

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

        presets_section = data.get("feedrate_presets")
        if not isinstance(presets_section, list):
            data["feedrate_presets"] = list(self.DEFAULT_FEEDRATE_PRESETS)

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
                        bindings.append(KeyBinding.from_dict(value))
            controls[key] = bindings
        for action in CONTROL_ACTIONS:
            controls.setdefault(action.key, [])
        logging_raw = raw.get("logging", {}) if isinstance(raw, dict) else {}
        logging_settings = self._parse_logging(logging_raw)
        if not logging_settings.file:
            default_log = str(self._config_dir / self.DEFAULT_LOG_FILENAME)
            logging_settings.file = default_log
            self._logger.debug(
                "Log file path missing in settings; defaulting to %s", default_log
            )
        presets_raw = raw.get("feedrate_presets") if isinstance(raw, dict) else None
        feedrate_presets = self._parse_feedrate_presets(presets_raw)
        return Settings(
            controls=controls,
            logging=logging_settings,
            feedrate_presets=feedrate_presets,
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

    def _parse_feedrate_presets(self, raw_presets) -> List[float]:
        """Normalise the feedrate preset list from persisted data."""

        parsed: List[float] = []
        if isinstance(raw_presets, Iterable) and not isinstance(raw_presets, (str, bytes)):
            for value in raw_presets:
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if number <= 0:
                    continue
                parsed.append(number)
        if not parsed:
            parsed = list(self.DEFAULT_FEEDRATE_PRESETS)
        return parsed

    def feedrate_presets(self) -> List[float]:
        """Return the configured feedrate presets ensuring defaults exist."""

        presets = [value for value in self._settings.feedrate_presets if value > 0]
        if not presets:
            presets = list(self.DEFAULT_FEEDRATE_PRESETS)
        return presets

