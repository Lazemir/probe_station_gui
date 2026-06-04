"""Credential storage for the probe station client.

The store prefers the operating-system credential backend exposed by the
optional ``keyring`` package. If keyring is not available, it falls back to a
per-user credentials file with restrictive permissions where the platform
supports them. The environment variable always wins and is never written.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


ENV_API_KEY = "PROBE_STATION_API_KEY"
ENV_BASE_URL = "PROBE_STATION_API_URL"
DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_PROFILE = "default"
DEFAULT_SERVICE_NAME = "ProbeStationGUI API"
_CREDENTIALS_FILENAME = "client-credentials.json"


class CredentialError(RuntimeError):
    """Base class for credential storage failures."""


class CredentialNotFoundError(CredentialError):
    """Raised when no API key is available for the requested profile."""


class CredentialStorageUnavailable(CredentialError):
    """Raised when a requested credential backend is unavailable."""


def default_config_dir() -> Path:
    """Return the per-user config directory for client-side credentials."""

    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "ProbeStationGUI"
        return Path.home() / "AppData" / "Roaming" / "ProbeStationGUI"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ProbeStationGUI"
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / "probe-station-gui"
    return Path.home() / ".config" / "probe-station-gui"


class CredentialStore:
    """Load and persist API keys for a named probe-station profile."""

    def __init__(
        self,
        *,
        profile: str = DEFAULT_PROFILE,
        config_dir: str | Path | None = None,
        service_name: str = DEFAULT_SERVICE_NAME,
    ) -> None:
        self.profile = str(profile or DEFAULT_PROFILE).strip() or DEFAULT_PROFILE
        self.config_dir = (
            Path(config_dir).expanduser()
            if config_dir is not None
            else default_config_dir()
        )
        self.service_name = str(service_name or DEFAULT_SERVICE_NAME)
        self.path = self.config_dir / _CREDENTIALS_FILENAME

    def load_api_key(self) -> str:
        """Return the API key from env, keyring, or the private file store."""

        env_key = os.environ.get(ENV_API_KEY, "").strip()
        if env_key:
            return env_key
        keyring_key = self._load_from_keyring()
        if keyring_key:
            return keyring_key
        file_key = self._load_from_file()
        if file_key:
            return file_key
        raise CredentialNotFoundError(
            f"No API key configured for profile {self.profile!r}."
        )

    def save_api_key(self, api_key: str, *, backend: str = "auto") -> str:
        """Persist an API key and return the backend used.

        ``backend`` can be ``"auto"``, ``"keyring"``, or ``"file"``. ``auto``
        prefers keyring and falls back to the private file store.
        """

        key = str(api_key or "").strip()
        if not key:
            raise ValueError("API key must be non-empty.")
        normalized_backend = str(backend or "auto").strip().lower()
        if normalized_backend not in {"auto", "keyring", "file"}:
            raise ValueError("backend must be 'auto', 'keyring', or 'file'.")
        if normalized_backend in {"auto", "keyring"}:
            try:
                self._save_to_keyring(key)
            except CredentialStorageUnavailable:
                if normalized_backend == "keyring":
                    raise
            else:
                return "keyring"
        self._save_to_file(key)
        return "file"

    def delete_api_key(self) -> None:
        """Delete the stored API key from both supported backends."""

        self._delete_from_keyring()
        if not self.path.exists():
            return
        data = self._load_file_data()
        profiles = data.get("profiles")
        if isinstance(profiles, dict):
            removed = profiles.pop(self.profile, None)
            if removed is not None:
                if profiles:
                    self._save_file_data(data)
                else:
                    try:
                        self.path.unlink()
                    except OSError:
                        self._save_file_data(data)

    def _load_from_keyring(self) -> str | None:
        keyring = self._import_keyring()
        if keyring is None:
            return None
        try:
            value = keyring.get_password(self.service_name, self.profile)
        except Exception:
            return None
        value = str(value or "").strip()
        return value or None

    def _save_to_keyring(self, api_key: str) -> None:
        keyring = self._import_keyring()
        if keyring is None:
            raise CredentialStorageUnavailable(
                "Install keyring or use backend='file'."
            )
        try:
            keyring.set_password(self.service_name, self.profile, api_key)
        except Exception as exc:
            raise CredentialStorageUnavailable(str(exc)) from exc

    def _delete_from_keyring(self) -> None:
        keyring = self._import_keyring()
        if keyring is None:
            return
        try:
            keyring.delete_password(self.service_name, self.profile)
        except Exception:
            return

    @staticmethod
    def _import_keyring() -> Any | None:
        try:
            import keyring  # type: ignore
        except Exception:
            return None
        return keyring

    def _load_from_file(self) -> str | None:
        data = self._load_file_data()
        profiles = data.get("profiles")
        if not isinstance(profiles, dict):
            return None
        profile_data = profiles.get(self.profile)
        if not isinstance(profile_data, dict):
            return None
        value = str(profile_data.get("api_key") or "").strip()
        return value or None

    def _load_file_data(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "profiles": {}}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "profiles": {}}
        if not isinstance(data, dict):
            return {"version": 1, "profiles": {}}
        profiles = data.get("profiles")
        if not isinstance(profiles, dict):
            data["profiles"] = {}
        return data

    def _save_to_file(self, api_key: str) -> None:
        data = self._load_file_data()
        profiles = data.setdefault("profiles", {})
        if not isinstance(profiles, dict):
            profiles = {}
            data["profiles"] = profiles
        profiles[self.profile] = {"api_key": api_key}
        self._save_file_data(data)

    def _save_file_data(self, data: dict[str, Any]) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f"{self.path.name}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(str(temp_path), flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
        except Exception:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        temp_path.replace(self.path)
        self._set_private_permissions(self.path)

    @staticmethod
    def _set_private_permissions(path: Path) -> None:
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            return
