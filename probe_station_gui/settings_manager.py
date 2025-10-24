"""Settings loading and persistence for the probe station GUI."""

from __future__ import annotations

import json
import logging
import os
import platform
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
class Settings:
    """Container for all configurable values."""

    controls: Dict[str, List[KeyBinding]] = field(default_factory=dict)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    feedrates: FeedrateSettings = field(default_factory=FeedrateSettings)

    def clone(self) -> "Settings":
        """Create a deep copy of the settings container."""

        return Settings(
            controls={key: list(value) for key, value in self.controls.items()},
            logging=self.logging.clone(),
            feedrates=self.feedrates.clone(),
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
        }


class SettingsManager:
    """Load, persist, and expose user configurable settings."""

    CONFIG_FILENAME = "settings.json"
    DEFAULT_LOG_FILENAME = "probe-station-gui.log"
    DEFAULT_FEEDRATE_PRESETS: tuple[float, ...] = (0.01, 0.1, 1.0, 10.0, 100.0)
    DEFAULT_FEEDRATE_DEFAULT: float = 1.0
    LINEAR_GROUP = "linear"
    ROTARY_GROUP = "rotary"

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

        feedrates_section = data.get("feedrates")
        legacy_presets = data.get("feedrate_presets")
        if not isinstance(feedrates_section, dict):
            presets = self._parse_feedrate_list(legacy_presets)
            feedrates_section = {
                self.LINEAR_GROUP: {
                    "presets": presets,
                    "default": self.DEFAULT_FEEDRATE_DEFAULT,
                },
                self.ROTARY_GROUP: {
                    "presets": presets,
                    "default": self.DEFAULT_FEEDRATE_DEFAULT,
                },
            }
            data["feedrates"] = feedrates_section
        else:
            if self.LINEAR_GROUP not in feedrates_section:
                feedrates_section[self.LINEAR_GROUP] = {
                    "presets": list(self.DEFAULT_FEEDRATE_PRESETS),
                    "default": self.DEFAULT_FEEDRATE_DEFAULT,
                }
            if self.ROTARY_GROUP not in feedrates_section:
                feedrates_section[self.ROTARY_GROUP] = {
                    "presets": list(self.DEFAULT_FEEDRATE_PRESETS),
                    "default": self.DEFAULT_FEEDRATE_DEFAULT,
                }
            data["feedrates"] = feedrates_section

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
        feedrates_raw = raw.get("feedrates") if isinstance(raw, dict) else None
        legacy_presets = raw.get("feedrate_presets") if isinstance(raw, dict) else None
        feedrates = self._parse_feedrates(feedrates_raw, legacy_presets)
        return Settings(
            controls=controls,
            logging=logging_settings,
            feedrates=feedrates,
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

        linear_group, rotary_group = self._parse_feedrate_groups(raw_feedrates, legacy_presets)
        return FeedrateSettings(
            linear=linear_group,
            rotary=rotary_group,
        )

    def _parse_feedrate_groups(
        self,
        raw_feedrates,
        legacy_presets,
    ) -> Tuple[FeedrateGroup, FeedrateGroup]:
        presets_fallback = self._parse_feedrate_list(legacy_presets)
        if not isinstance(raw_feedrates, dict):
            linear = self._normalise_feedrate_group(
                FeedrateGroup(presets=presets_fallback, default=self.DEFAULT_FEEDRATE_DEFAULT)
            )
            rotary = self._normalise_feedrate_group(
                FeedrateGroup(presets=presets_fallback, default=self.DEFAULT_FEEDRATE_DEFAULT)
            )
            return linear, rotary

        linear_raw = raw_feedrates.get(self.LINEAR_GROUP)
        rotary_raw = raw_feedrates.get(self.ROTARY_GROUP)
        linear = self._normalise_feedrate_group(self._group_from_raw(linear_raw))
        rotary = self._normalise_feedrate_group(self._group_from_raw(rotary_raw))
        return linear, rotary

    def _group_from_raw(self, raw_group) -> FeedrateGroup:
        """Build a feedrate group dataclass from persisted data."""

        presets = []
        default = self.DEFAULT_FEEDRATE_DEFAULT
        if isinstance(raw_group, dict):
            presets = self._parse_feedrate_list(raw_group.get("presets"))
            default_raw = raw_group.get("default")
            try:
                if isinstance(default_raw, (int, float, str)):
                    default = float(default_raw)
            except (TypeError, ValueError):
                default = self.DEFAULT_FEEDRATE_DEFAULT
        else:
            presets = list(self.DEFAULT_FEEDRATE_PRESETS)
        return FeedrateGroup(presets=presets, default=default)

    def _parse_feedrate_list(self, raw_presets) -> List[float]:
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
            parsed = list(self.DEFAULT_FEEDRATE_PRESETS)
        return parsed

    def _normalise_feedrate_group(self, group: FeedrateGroup) -> FeedrateGroup:
        """Ensure the feedrate group contains valid presets and defaults."""

        presets = self._parse_feedrate_list(group.presets)
        presets.sort()
        default_value = group.default if group.default > 0 else self.DEFAULT_FEEDRATE_DEFAULT
        default_text = self._select_default(default_value, presets)
        return FeedrateGroup(presets=presets, default=default_text)

    def _select_default(self, candidate: float, presets: List[float]) -> float:
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

        return self.DEFAULT_FEEDRATE_DEFAULT

    def _normalise_settings(self, settings: Settings) -> Settings:
        """Return a copy of the settings with feedrates normalised."""

        clone = settings.clone()
        clone.feedrates = FeedrateSettings(
            linear=self._normalise_feedrate_group(clone.feedrates.linear),
            rotary=self._normalise_feedrate_group(clone.feedrates.rotary),
        )
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

