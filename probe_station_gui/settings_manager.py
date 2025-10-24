"""Settings loading and persistence for the probe station GUI."""

from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Dict, Iterable, List


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
class Settings:
    """Container for all configurable values."""

    controls: Dict[str, List[KeyBinding]] = field(default_factory=dict)

    def clone(self) -> "Settings":
        """Create a deep copy of the settings container."""

        return Settings({key: list(value) for key, value in self.controls.items()})

    def to_dict(self) -> dict:
        """Convert the settings into a JSON serializable structure."""

        return {
            "controls": {
                key: [binding.to_dict() for binding in bindings]
                for key, bindings in self.controls.items()
            }
        }


class SettingsManager:
    """Load, persist, and expose user configurable settings."""

    CONFIG_FILENAME = "settings.json"

    def __init__(self) -> None:
        self._config_dir = self._determine_config_dir()
        self._config_path = self._config_dir / self.CONFIG_FILENAME
        self._ensure_default_file()
        self._settings = self._load()

    @property
    def settings(self) -> Settings:
        """Access the mutable settings container."""

        return self._settings

    def replace(self, settings: Settings) -> None:
        """Replace the stored settings with the provided instance."""

        self._settings = settings

    def save(self) -> None:
        """Persist the current settings to disk."""

        data = self._settings.to_dict()
        self._config_dir.mkdir(parents=True, exist_ok=True)
        with self._config_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)

    def control_bindings(self) -> Dict[str, List[KeyBinding]]:
        """Return the control bindings ensuring defaults are present."""

        controls = {key: list(value) for key, value in self._settings.controls.items()}
        for action in CONTROL_ACTIONS:
            controls.setdefault(action.key, [])
        return controls

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
        with default_resource.open("rb") as source, self._config_path.open("wb") as target:
            target.write(source.read())

    def _load(self) -> Settings:
        """Load settings from disk and normalise the structure."""

        with self._config_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
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
        return Settings(controls)

