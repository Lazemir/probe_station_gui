"""Explicit profile and settings persistence for route measurement UI state."""

from __future__ import annotations

import json
import math
import weakref
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QFileDialog, QWidget

from probe_station_gui.dialogs.route_measurement_defaults import (
    KEITHLEY_DEFAULTS_PROFILE_VERSION,
    ROUTE_MEASUREMENT_PROFILE_VERSION,
)
from probe_station_gui.dialogs.route_measurement_run_controls import (
    RouteMeasurementRunControls,
)
from probe_station_gui.dialogs.route_measurement_setup import (
    RouteMeasurementSetupEditor,
)
from probe_station_gui.route.meter_config import ROUTE_METER_KEITHLEY_TYPES


class RouteMeasurementProfileController(QObject):
    """Own route profile schema, migration, JSON I/O, and settings writes."""

    status_changed = Signal(str)

    def __init__(
        self,
        *,
        setup_editor: RouteMeasurementSetupEditor,
        run_controls: RouteMeasurementRunControls,
        settings_path: Path | None,
        dialog_parent: QWidget,
    ) -> None:
        super().__init__()
        self._setup = setup_editor
        self._run_controls = run_controls
        self._settings_path = settings_path
        self._dialog_parent = weakref.ref(dialog_parent)

    def load_profile(self) -> None:
        start = str(self._profile_start_directory() / "route-measurement-profile.json")
        path, _ = QFileDialog.getOpenFileName(
            self._dialog_parent(),
            "Load Route Measurement Profile",
            start,
            "JSON files (*.json);;All files (*)",
        )
        if not path:
            return
        try:
            self.apply_data(self._read_profile(Path(path)))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self.status_changed.emit(f"Unable to load profile: {exc}")
            return
        self.save_settings()
        self.status_changed.emit(f"Loaded profile {path}.")

    def save_profile(self) -> None:
        start = str(self._profile_start_directory() / "route-measurement-profile.json")
        path, _ = QFileDialog.getSaveFileName(
            self._dialog_parent(),
            "Save Route Measurement Profile",
            start,
            "JSON files (*.json);;All files (*)",
        )
        if not path:
            return
        try:
            self._write_profile(Path(path), self.data())
        except OSError as exc:
            self.status_changed.emit(f"Unable to save profile: {exc}")
            return
        self.save_settings()
        self.status_changed.emit(f"Saved profile {path}.")

    def load_settings(self) -> None:
        if self._settings_path is None or not self._settings_path.exists():
            return
        try:
            migrated = self.apply_data(self._read_profile(self._settings_path))
            if migrated:
                self.save_settings()
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return

    def save_settings(self) -> None:
        if self._settings_path is None:
            return
        try:
            self._write_profile(self._settings_path, self.data())
        except OSError:
            return

    def data(self) -> dict[str, Any]:
        """Return the complete versioned profile payload."""

        session_active = self._run_controls.measurement_session_active()
        return {
            "version": ROUTE_MEASUREMENT_PROFILE_VERSION,
            **self._setup.profile_data(),
            "current_point": self._run_controls.current_point(),
            "measurement_session_active": session_active,
            "measurement_pending": session_active,
        }

    def apply_data(self, data: object) -> bool:
        """Apply a profile payload and report whether it required migration."""

        if not isinstance(data, dict):
            raise ValueError("Profile JSON root must be an object.")
        migrate = self._should_migrate_keithley_defaults(data)
        self._setup.apply_profile_data(data)
        session_active = data.get(
            "measurement_session_active",
            data.get("measurement_pending", False),
        )
        self._run_controls.set_measurement_session_active(bool(session_active))
        current_point = data.get("current_point", data.get("start_point"))
        try:
            numeric_point = float(current_point)
        except (TypeError, ValueError):
            numeric_point = math.nan
        if migrate:
            self._setup.apply_default_keithley_settings()
        self._setup.refresh_state()
        if math.isfinite(numeric_point):
            self._run_controls.set_current_point_and_publish(int(round(numeric_point)))
        return migrate

    def _profile_start_directory(self) -> Path:
        return self._setup.profile_start_directory(self._settings_path)

    @staticmethod
    def _read_profile(path: Path) -> object:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def _write_profile(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)

    @staticmethod
    def _should_migrate_keithley_defaults(data: dict[str, Any]) -> bool:
        try:
            version = int(data.get("version", 0))
        except (TypeError, ValueError):
            version = 0
        meter = data.get("meter")
        return (
            version < KEITHLEY_DEFAULTS_PROFILE_VERSION
            and isinstance(meter, dict)
            and meter.get("meter_type") in ROUTE_METER_KEITHLEY_TYPES
        )


__all__ = ["RouteMeasurementProfileController"]
