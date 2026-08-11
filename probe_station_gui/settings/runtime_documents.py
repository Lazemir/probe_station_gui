"""Runtime controller and connection-state documents in the config directory."""

from __future__ import annotations

import json
import logging
from pathlib import Path


CONTROLLER_STATE_FILENAME = "controller-state.json"
SERIAL_CONNECTION_STATE_FILENAME = "serial-connection-state.json"
METER_CONNECTION_STATE_FILENAME = "meter-connection-state.json"


class RuntimeStateDocuments:
    """Read and write runtime-state JSON documents beside user settings."""

    def __init__(
        self,
        config_dir: str | Path,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config_dir = Path(config_dir)
        self._logger = logger or logging.getLogger(__name__)

    def load_controller_state(self) -> dict | None:
        """Load persisted controller runtime state, if present."""

        return self._load_document(
            CONTROLLER_STATE_FILENAME,
            description="controller state",
            fallback=None,
        )

    def save_controller_state(self, data: dict | None) -> None:
        """Persist controller state or clear an empty state."""

        if not data:
            self.clear_controller_state()
            return
        self._write_document(CONTROLLER_STATE_FILENAME, data)

    def clear_controller_state(self) -> None:
        """Remove persisted controller runtime state."""

        path = self._config_dir / CONTROLLER_STATE_FILENAME
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            self._logger.warning("Failed to clear controller state %s: %s", path, exc)

    def load_serial_connection_state(self) -> dict:
        """Load the latest serial connection state."""

        return self._load_document(
            SERIAL_CONNECTION_STATE_FILENAME,
            description="serial connection state",
            fallback={},
        )

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
        self._write_connection_document(
            SERIAL_CONNECTION_STATE_FILENAME,
            data,
            description="serial connection state",
        )

    def serial_auto_connect_enabled(self) -> bool:
        """Return whether startup should restore an open serial connection."""

        return self.load_serial_connection_state().get("status") == "connected"

    def load_meter_connection_state(self) -> dict:
        """Load the latest measurement-instrument connection state."""

        return self._load_document(
            METER_CONNECTION_STATE_FILENAME,
            description="measurement-instrument connection state",
            fallback={},
        )

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
        self._write_connection_document(
            METER_CONNECTION_STATE_FILENAME,
            data,
            description="measurement-instrument connection state",
        )

    def meter_auto_connect_enabled(self) -> bool:
        """Return whether startup should restore an open measurement instrument."""

        return self.load_meter_connection_state().get("status") == "connected"

    def _load_document(
        self,
        filename: str,
        *,
        description: str,
        fallback,
    ):
        path = self._config_dir / filename
        if not path.exists():
            return fallback
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            self._logger.warning("Failed to load %s from %s: %s", description, path, exc)
            return fallback
        return data if isinstance(data, dict) else fallback

    def _write_connection_document(
        self,
        filename: str,
        data: dict[str, object],
        *,
        description: str,
    ) -> None:
        path = self._config_dir / filename
        self._config_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._write_document(filename, data)
        except OSError as exc:
            self._logger.warning("Failed to save %s to %s: %s", description, path, exc)

    def _write_document(self, filename: str, data: dict) -> None:
        path = self._config_dir / filename
        self._config_dir.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)


__all__ = [
    "CONTROLLER_STATE_FILENAME",
    "METER_CONNECTION_STATE_FILENAME",
    "RuntimeStateDocuments",
    "SERIAL_CONNECTION_STATE_FILENAME",
]
