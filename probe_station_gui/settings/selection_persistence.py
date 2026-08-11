"""Durable software Coordinate System selection with stale-write rejection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import threading
from uuid import UUID

from probe_station_gui.settings.document import Settings


SOFTWARE_COORDINATE_SELECTION_FILENAME = "software-coordinate-selection.json"
SOFTWARE_COORDINATE_SELECTION_VERSION = 1


@dataclass(frozen=True)
class SoftwareCoordinateSelectionSnapshot:
    """One immutable GUI selection ordered by a monotonic generation."""

    frame_id: str
    generation: int


class SoftwareCoordinateSelectionPersistence:
    """Own selection state and serialize main/sidecar persistence."""

    def __init__(
        self,
        *,
        config_dir: str | Path,
        settings_path: str | Path,
        state_lock: threading.RLock,
        logger: logging.Logger | None = None,
    ) -> None:
        self._selection_path = Path(config_dir) / SOFTWARE_COORDINATE_SELECTION_FILENAME
        self._settings_path = Path(settings_path)
        self._state_lock = state_lock
        self._persistence_lock = threading.Lock()
        self._logger = logger or logging.getLogger(__name__)
        self._current: SoftwareCoordinateSelectionSnapshot | None = None

    @property
    def selection_path(self) -> Path:
        """Return the sidecar path owned by the configured application directory."""

        return self._selection_path

    def restore(self, settings: Settings) -> None:
        """Merge the newest valid main/sidecar selection into ``settings``."""

        with self._state_lock:
            main_snapshot = self._snapshot_from_settings(settings)
            sidecar_snapshot = self._load_sidecar()
            winner = (
                sidecar_snapshot
                if sidecar_snapshot is not None
                and sidecar_snapshot.generation > main_snapshot.generation
                else main_snapshot
            )
            self._current = winner
            self._apply_snapshot(settings, winner)

    def select(
        self,
        settings: Settings,
        frame_id: str,
    ) -> SoftwareCoordinateSelectionSnapshot:
        """Advance the in-memory selection without waiting for filesystem I/O."""

        selected = self._normalize_frame_id(frame_id)
        with self._state_lock:
            current = self._ensure_current(settings)
            snapshot = SoftwareCoordinateSelectionSnapshot(
                selected,
                current.generation + 1,
            )
            self._current = snapshot
            self._apply_snapshot(settings, snapshot)
            return snapshot

    def merge(self, settings: Settings) -> None:
        """Apply the authoritative in-memory selection to a settings clone."""

        with self._state_lock:
            self._apply_snapshot(settings, self._ensure_current(settings))

    def save_main(
        self,
        capture: Callable[[], dict | None],
        *,
        before_write: Callable[[], None] | None = None,
    ) -> bool:
        """Capture and atomically write main settings under the shared I/O lock."""

        with self._persistence_lock:
            with self._state_lock:
                data = capture()
            if data is None:
                return False
            if before_write is not None:
                before_write()
            self._atomic_write(self._settings_path, data)
            return True

    def persist(
        self,
        snapshot: SoftwareCoordinateSelectionSnapshot,
        capture_main: Callable[[], dict],
    ) -> bool:
        """Write one still-current selection to main settings and its sidecar."""

        if not isinstance(snapshot, SoftwareCoordinateSelectionSnapshot):
            raise TypeError("Selection persistence requires an immutable snapshot.")
        normalized = SoftwareCoordinateSelectionSnapshot(
            self._normalize_frame_id(snapshot.frame_id),
            self._normalize_generation(snapshot.generation),
        )
        with self._persistence_lock:
            with self._state_lock:
                if self._current != normalized:
                    return False
                main_document = capture_main()
            self._atomic_write(self._settings_path, main_document)
            self._validate_existing_sidecar()
            self._atomic_write(
                self._selection_path,
                {
                    "version": SOFTWARE_COORDINATE_SELECTION_VERSION,
                    "last_selected_frame_id": normalized.frame_id,
                    "generation": normalized.generation,
                },
            )
            return True

    def _ensure_current(self, settings: Settings) -> SoftwareCoordinateSelectionSnapshot:
        if self._current is None:
            self._current = self._snapshot_from_settings(settings)
        return self._current

    def _load_sidecar(self) -> SoftwareCoordinateSelectionSnapshot | None:
        if not self._selection_path.exists():
            return None
        try:
            with self._selection_path.open("r", encoding="utf-8-sig") as handle:
                return self._decode(json.load(handle))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._logger.warning(
                "Failed to load software coordinate selection from %s: %s",
                self._selection_path,
                exc,
            )
            return None

    def _validate_existing_sidecar(self) -> None:
        if not self._selection_path.exists():
            return
        try:
            with self._selection_path.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                "Software coordinate selection document is unreadable."
            ) from exc
        self._decode(data)

    @classmethod
    def _decode(cls, data: object) -> SoftwareCoordinateSelectionSnapshot:
        if not isinstance(data, dict):
            raise ValueError("Software coordinate selection document must be an object.")
        version = data.get("version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != SOFTWARE_COORDINATE_SELECTION_VERSION
        ):
            raise ValueError(
                f"unsupported software coordinate selection version: {version!r}"
            )
        if set(data) != {"version", "last_selected_frame_id", "generation"}:
            raise ValueError(
                "Software coordinate selection document schema is unsupported."
            )
        generation = data["generation"]
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
        ):
            raise ValueError(
                "Software coordinate selection document generation must be a "
                "non-negative integer."
            )
        return SoftwareCoordinateSelectionSnapshot(
            cls._normalize_frame_id(data["last_selected_frame_id"]),
            generation,
        )

    @classmethod
    def _snapshot_from_settings(
        cls,
        settings: Settings,
    ) -> SoftwareCoordinateSelectionSnapshot:
        coordinates = settings.software_coordinates
        try:
            selected = cls._normalize_frame_id(coordinates.last_selected_frame_id)
        except ValueError:
            selected = "machine"
        return SoftwareCoordinateSelectionSnapshot(
            selected,
            cls._normalize_generation(coordinates.selection_generation),
        )

    @staticmethod
    def _apply_snapshot(
        settings: Settings,
        snapshot: SoftwareCoordinateSelectionSnapshot,
    ) -> None:
        settings.software_coordinates.last_selected_frame_id = snapshot.frame_id
        settings.software_coordinates.selection_generation = snapshot.generation

    @staticmethod
    def _normalize_frame_id(frame_id: object) -> str:
        message = (
            "Software coordinate selection must be Machine or a "
            "coordinate-frame UUID."
        )
        if not isinstance(frame_id, str) or not frame_id.strip():
            raise ValueError(message)
        selected = frame_id.strip()
        if selected == "machine":
            return selected
        try:
            UUID(selected)
        except (ValueError, AttributeError) as exc:
            raise ValueError(message) from exc
        return selected

    @staticmethod
    def _normalize_generation(value: object) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return 0
        return value

    @staticmethod
    def _atomic_write(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            with temporary_path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


__all__ = [
    "SOFTWARE_COORDINATE_SELECTION_FILENAME",
    "SoftwareCoordinateSelectionPersistence",
    "SoftwareCoordinateSelectionSnapshot",
]
