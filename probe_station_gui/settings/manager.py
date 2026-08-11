"""Settings loading and persistence for the probe station GUI."""

from __future__ import annotations

import json
import logging
import os
import platform
import threading
from importlib import resources
from pathlib import Path
from typing import Callable, Dict, List

from probe_station_gui.settings.axis_calibration_config import (
    AxisCalibrationSettings,
)
from probe_station_gui.settings.controls_config import (
    KeyBinding,
)
from probe_station_gui.settings.default_file import normalize_default_settings_data
from probe_station_gui.settings.document import Settings, SettingsDocumentCodec
from probe_station_gui.settings.feedrate_config import (
    FeedrateGroup,
    FeedrateSettings,
)
from probe_station_gui.settings.jog_config import JogSettings
from probe_station_gui.shared.logging_config import configure_logging
from probe_station_gui.settings.needle_calibration_config import (
    LCR_APERTURE_RATES,
    LCR_LEVEL_MODES,
    LCR_MEASUREMENT_FUNCTIONS,
    LCR_RANGE_MODES,
    LCR_SOURCE_RESISTANCES_OHM,
    NeedleCalibrationSettings,
    SavedStagePositionSettings,
)
from probe_station_gui.settings.objective_config import (
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
    default_objective,
    ordered_objective_names,
)
from probe_station_gui.settings.oscillation_config import (
    OscillationSettings,
)
from probe_station_gui.settings.precision_approach import PrecisionApproachSettings
from probe_station_gui.settings.runtime_documents import (
    CONTROLLER_STATE_FILENAME,
    METER_CONNECTION_STATE_FILENAME,
    SERIAL_CONNECTION_STATE_FILENAME,
    RuntimeStateDocuments,
)
from probe_station_gui.settings.selection_persistence import (
    SOFTWARE_COORDINATE_SELECTION_FILENAME,
    SOFTWARE_COORDINATE_SELECTION_VERSION,
    SoftwareCoordinateSelectionPersistence,
    SoftwareCoordinateSelectionSnapshot,
)
from probe_station_gui.settings.sections import (
    ApiSettings,
    CoordinateSystemSettings,
    ExposurePolicySettings,
    LoggingSettings,
)
from probe_station_gui.notifications.telegram_settings import (
    TELEGRAM_ALERT_TYPES,
    TelegramSettings,
)

__all__ = [
    "ApiSettings",
    "FeedrateGroup",
    "FeedrateSettings",
    "JogSettings",
    "LCR_APERTURE_RATES",
    "LCR_LEVEL_MODES",
    "LCR_MEASUREMENT_FUNCTIONS",
    "LCR_RANGE_MODES",
    "LCR_SOURCE_RESISTANCES_OHM",
    "LoggingSettings",
    "NeedleCalibrationSettings",
    "ObjectiveCalibrationSettings",
    "ObjectivesSettings",
    "SavedStagePositionSettings",
    "Settings",
    "SettingsManager",
    "TELEGRAM_ALERT_TYPES",
    "TelegramSettings",
    "ordered_objective_names",
]

logger = logging.getLogger(__name__)


class SettingsManager:
    """Load, persist, and expose user configurable settings."""

    CONFIG_FILENAME = "settings.json"
    COORDINATE_FRAMES_FILENAME = "coordinate-frames.json"
    SOFTWARE_COORDINATE_SELECTION_FILENAME = SOFTWARE_COORDINATE_SELECTION_FILENAME
    SOFTWARE_COORDINATE_SELECTION_VERSION = SOFTWARE_COORDINATE_SELECTION_VERSION
    CONTROLLER_STATE_FILENAME = CONTROLLER_STATE_FILENAME
    SERIAL_CONNECTION_STATE_FILENAME = SERIAL_CONNECTION_STATE_FILENAME
    METER_CONNECTION_STATE_FILENAME = METER_CONNECTION_STATE_FILENAME
    DEFAULT_LOG_FILENAME = "probe-station-gui.log"
    LINEAR_GROUP = "linear"
    ROTARY_GROUP = "rotary"

    def __init__(self) -> None:
        self._settings_lock = threading.RLock()
        self._config_dir = self._determine_config_dir()
        self._config_path = self._config_dir / self.CONFIG_FILENAME
        self._logger = logging.getLogger(__name__)
        self._logger.debug("Configuration directory resolved to %s", self._config_dir)
        self._document_codec = SettingsDocumentCodec(
            default_log_path=self._default_log_path(),
            logger=self._logger,
        )
        self._selection_persistence = SoftwareCoordinateSelectionPersistence(
            config_dir=self._config_dir,
            settings_path=self._config_path,
            state_lock=self._settings_lock,
            logger=self._logger,
        )
        self._runtime_documents = RuntimeStateDocuments(
            self._config_dir,
            logger=self._logger,
        )
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

    def coordinate_frames_path(self) -> Path:
        """Return the separate measured Design-frame document path."""

        return self._config_dir / self.COORDINATE_FRAMES_FILENAME

    def set_software_coordinate_selection(
        self,
        frame_id: str,
    ) -> SoftwareCoordinateSelectionSnapshot:
        """Update the GUI selection in memory without performing filesystem I/O."""

        with self._settings_lock:
            updated = self._settings.clone()
            snapshot = self._selection_persistence.select(updated, frame_id)
            self._replace_locked(updated)
            return snapshot

    def persist_software_coordinate_selection(
        self,
        snapshot: SoftwareCoordinateSelectionSnapshot,
    ) -> bool:
        """Persist one still-current immutable selection on a worker thread."""

        persisted = self._selection_persistence.persist(
            snapshot,
            self._capture_settings_data,
        )
        if persisted:
            self._logger.info("Settings saved to %s", self._config_path)
        return persisted

    def replace(self, settings: Settings) -> None:
        """Replace the stored settings with the provided instance."""

        with self._settings_lock:
            self._replace_locked(settings)
        self.apply()

    def save(self) -> None:
        """Persist the current settings to disk."""

        self._save_main(self._capture_settings_data)

    def _capture_settings_data(self) -> dict:
        with self._settings_lock:
            return self._capture_settings_data_locked()

    def _save_main(
        self,
        capture: Callable[[], dict | None],
        *,
        before_write: Callable[[], None] | None = None,
    ) -> bool:
        persisted = self._selection_persistence.save_main(
            capture,
            before_write=before_write,
        )
        if persisted:
            self._logger.info("Settings saved to %s", self._config_path)
        return persisted

    def replace_and_save(
        self,
        settings: Settings,
        *,
        preserve_exposure_policy: bool = False,
        apply_runtime: bool = True,
    ) -> None:
        """Replace and atomically persist settings as one serialized transaction."""

        def capture() -> dict:
            with self._settings_lock:
                updated = settings.clone()
                if preserve_exposure_policy:
                    updated.exposure_policy = self._settings.exposure_policy.clone()
                self._replace_locked(updated)
                return self._capture_settings_data_locked()

        self._save_main(capture, before_write=self.apply if apply_runtime else None)

    def update_and_save(
        self,
        mutation: Callable[[Settings], None],
        *,
        apply_runtime: bool = False,
    ) -> None:
        """Mutate a fresh settings clone and persist it under one lock."""

        def capture() -> dict:
            with self._settings_lock:
                updated = self._settings.clone()
                mutation(updated)
                self._replace_locked(updated)
                return self._capture_settings_data_locked()

        self._save_main(capture, before_write=self.apply if apply_runtime else None)

    def _replace_locked(self, settings: Settings) -> None:
        updated = self._document_codec.normalize(settings)
        self._selection_persistence.merge(updated)
        self._settings = updated

    def _capture_settings_data_locked(self) -> dict:
        self._selection_persistence.merge(self._settings)
        return self._settings.to_dict()

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

        return self._document_codec.control_bindings(self._settings)

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

        return self._runtime_documents.load_controller_state()

    def save_controller_state(self, data: dict | None) -> None:
        """Persist controller runtime state alongside user settings."""

        self._runtime_documents.save_controller_state(data)

    def clear_controller_state(self) -> None:
        """Remove persisted controller runtime state."""

        self._runtime_documents.clear_controller_state()

    def load_serial_connection_state(self) -> dict:
        """Load persisted serial connection state, if present."""

        return self._runtime_documents.load_serial_connection_state()

    def save_serial_connection_state(
        self,
        connected: bool,
        *,
        port: str | None = None,
        baud_rate: int | None = None,
    ) -> None:
        """Persist the latest serial connection status."""

        self._runtime_documents.save_serial_connection_state(
            connected,
            port=port,
            baud_rate=baud_rate,
        )

    def serial_auto_connect_enabled(self) -> bool:
        """Return whether startup should restore an open serial connection."""

        return self._runtime_documents.serial_auto_connect_enabled()

    def load_meter_connection_state(self) -> dict:
        """Load persisted measurement-instrument connection state, if present."""

        return self._runtime_documents.load_meter_connection_state()

    def save_meter_connection_state(
        self,
        connected: bool,
        *,
        meter_type: str | None = None,
        description: str | None = None,
    ) -> None:
        """Persist the latest measurement-instrument connection status."""

        self._runtime_documents.save_meter_connection_state(
            connected,
            meter_type=meter_type,
            description=description,
        )

    def meter_auto_connect_enabled(self) -> bool:
        """Return whether startup should restore an open measurement instrument."""

        return self._runtime_documents.meter_auto_connect_enabled()

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
        settings = self._document_codec.decode(raw)
        self._selection_persistence.restore(settings)
        return settings

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

    def exposure_policy_configuration(self) -> ExposurePolicySettings:
        """Return the current camera exposure policy clone."""

        return self._settings.exposure_policy.clone()

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

    def axis_calibrations_configuration(self) -> dict[str, AxisCalibrationSettings]:
        """Return independent calibration snapshots for every stage axis."""

        return {
            axis: calibration.clone()
            for axis, calibration in self._settings.axis_calibrations.items()
        }

    def coordinate_system_configuration(self) -> CoordinateSystemSettings:
        """Return the current coordinate-system configuration clone."""

        return self._settings.coordinate_system.clone()

    def precision_approach_configuration(self) -> PrecisionApproachSettings:
        """Return the current per-axis final approach profiles."""

        return self._settings.precision_approach.clone()

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

        def capture() -> dict | None:
            with self._settings_lock:
                if self._settings.design_last_directory == new_value:
                    return None
                updated = self._settings.clone()
                updated.design_last_directory = new_value
                self._replace_locked(updated)
                return self._capture_settings_data_locked()

        self._save_main(capture)

    def set_exposure_policy_configuration(
        self, settings: ExposurePolicySettings
    ) -> None:
        """Persist the camera exposure policy settings."""

        def update_exposure_policy(updated: Settings) -> None:
            updated.exposure_policy = settings.clone()

        self.update_and_save(update_exposure_policy)

