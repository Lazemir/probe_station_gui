"""Persistence helpers for route measurement session settings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class RouteMeasurementSettingsStore:
    """Read and update the route-measurement settings JSON file."""

    FILENAME = "route-measurement-settings.json"

    def __init__(self, config_dir: str | Path) -> None:
        self._path = Path(config_dir).expanduser() / self.FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, object]:
        if not self._path.exists():
            return {}
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        return dict(loaded) if isinstance(loaded, dict) else {}

    def save_current_point(self, point_number: int, *, session_active: bool) -> None:
        data = self.load()
        data["current_point"] = int(point_number)
        data["start_point"] = int(point_number)
        self._set_session_active(data, session_active)
        self.write(data)

    def save_pending(self, pending: bool) -> None:
        data = self.load()
        self._set_session_active(data, pending)
        self.write(data)

    def save_session_metadata(
        self,
        *,
        route: object | None,
        configuration: object | None,
        session_active: bool,
    ) -> None:
        data = self.load()
        if route is not None:
            data["session_route_name"] = str(getattr(route, "name", ""))
            data["session_route_point_count"] = len(getattr(route, "points", ()))
            route_path = getattr(route, "path", None)
            if route_path is not None:
                data["session_route_path"] = str(route_path)
        if configuration is not None:
            data["csv_path"] = str(getattr(configuration, "csv_path", ""))
            data["operation_mode"] = str(getattr(configuration, "operation_mode", ""))
            data["photo_output_dir"] = str(
                getattr(configuration, "photo_output_dir", "")
            )
            current_point = int(getattr(configuration, "current_point"))
            data["current_point"] = current_point
            data["start_point"] = current_point
        self._set_session_active(data, session_active)
        self.write(data)

    def write(self, data: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)

    @staticmethod
    def current_point(state: dict[str, object]) -> int | None:
        value = state.get("current_point", state.get("start_point"))
        try:
            point_number = int(value)
        except (TypeError, ValueError):
            return None
        return point_number if point_number >= 1 else None

    @staticmethod
    def session_active(state: dict[str, object]) -> bool:
        return bool(
            state.get(
                "measurement_session_active",
                state.get("measurement_pending", False),
            )
        )

    @staticmethod
    def settings_match_route(state: dict[str, object], route: object) -> bool:
        stored_count = state.get("session_route_point_count")
        try:
            if stored_count is not None and int(stored_count) != len(
                getattr(route, "points", ())
            ):
                return False
        except (TypeError, ValueError):
            return False
        stored_path = state.get("session_route_path")
        route_path = getattr(route, "path", None)
        if isinstance(stored_path, str) and stored_path.strip() and route_path is not None:
            try:
                return Path(stored_path).expanduser().resolve() == Path(
                    route_path
                ).resolve()
            except OSError:
                return str(stored_path).strip() == str(route_path)
        stored_name = state.get("session_route_name")
        if isinstance(stored_name, str) and stored_name.strip():
            return stored_name.strip() == getattr(route, "name", "")
        return True

    @staticmethod
    def _set_session_active(data: dict[str, object], active: bool) -> None:
        data["measurement_session_active"] = bool(active)
        data["measurement_pending"] = bool(active)


__all__ = ["RouteMeasurementSettingsStore"]
