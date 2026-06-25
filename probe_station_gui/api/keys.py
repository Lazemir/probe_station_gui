"""Local API key storage and permission checks."""

from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import secrets
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


API_KEY_FILENAME = "api-keys.json"
API_KEY_PREFIX = "psk_"
API_KEY_DISPLAY_PREFIX_LENGTH = 12
API_KEY_DISPLAY_SUFFIX_LENGTH = 6

API_PERMISSION_STAGE_READ = "stage_read"
API_PERMISSION_STAGE_WRITE = "stage_write"
API_PERMISSION_ROUTE_READ = "route_read"
API_PERMISSION_ROUTE_MEASURE = "route_measure"
API_KEY_PERMISSIONS = (
    API_PERMISSION_STAGE_READ,
    API_PERMISSION_STAGE_WRITE,
    API_PERMISSION_ROUTE_READ,
    API_PERMISSION_ROUTE_MEASURE,
)
DEFAULT_API_KEY_PERMISSIONS = {
    API_PERMISSION_STAGE_READ: True,
    API_PERMISSION_STAGE_WRITE: False,
    API_PERMISSION_ROUTE_READ: True,
    API_PERMISSION_ROUTE_MEASURE: True,
}


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _hash_api_key(api_key: str) -> str:
    return hashlib.sha256(str(api_key).encode("utf-8")).hexdigest()


def masked_api_key(key_prefix: str, key_suffix: str | None = None) -> str:
    """Return the non-secret display form for an API key."""

    prefix = str(key_prefix or "").strip()
    suffix = str(key_suffix or "").strip()
    if prefix and suffix:
        return f"{prefix}...{suffix}"
    return prefix


def default_api_user_name() -> str:
    """Return the local account name to use for new API keys."""

    try:
        user_name = getpass.getuser()
    except Exception:
        user_name = ""
    return str(user_name or "").strip() or "user"


def normalize_permissions(
    permissions: Mapping[str, object] | None,
    *,
    defaults: Mapping[str, bool] | None = None,
) -> dict[str, bool]:
    """Return a complete permission map with unknown keys discarded."""

    base = dict(defaults or {})
    normalized: dict[str, bool] = {}
    for permission in API_KEY_PERMISSIONS:
        normalized[permission] = bool(base.get(permission, False))
    if permissions is not None:
        for permission in API_KEY_PERMISSIONS:
            if permission in permissions:
                normalized[permission] = bool(permissions[permission])
    return normalized


@dataclass(frozen=True)
class ApiKeyRecord:
    id: str
    user_name: str
    key_name: str
    key_prefix: str
    key_suffix: str
    key_hash: str
    created_at_utc: str
    last_used_at_utc: str | None = None
    enabled: bool = True
    permissions: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ApiKeyRecord":
        record_id = str(data.get("id") or secrets.token_hex(8))
        user_name = str(
            data.get("user_name") or data.get("user") or default_api_user_name()
        ).strip()
        key_name = str(data.get("key_name") or data.get("name") or "").strip()
        key_prefix = str(data.get("key_prefix") or data.get("prefix") or "").strip()
        key_suffix = str(data.get("key_suffix") or data.get("suffix") or "").strip()
        key_hash = str(data.get("key_hash") or data.get("hash") or "").strip()
        created_at = str(
            data.get("created_at_utc") or data.get("created_at") or _utc_now()
        )
        last_used_raw = data.get("last_used_at_utc", data.get("last_used_at"))
        last_used = str(last_used_raw).strip() if last_used_raw else None
        return cls(
            id=record_id,
            user_name=user_name or default_api_user_name(),
            key_name=key_name,
            key_prefix=key_prefix,
            key_suffix=key_suffix,
            key_hash=key_hash,
            created_at_utc=created_at,
            last_used_at_utc=last_used or None,
            enabled=bool(data.get("enabled", True)),
            permissions=normalize_permissions(
                data.get("permissions")
                if isinstance(data.get("permissions"), Mapping)
                else None
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "user_name": self.user_name,
            "key_name": self.key_name,
            "key_prefix": self.key_prefix,
            "key_suffix": self.key_suffix,
            "key_hash": self.key_hash,
            "created_at_utc": self.created_at_utc,
            "last_used_at_utc": self.last_used_at_utc,
            "enabled": bool(self.enabled),
            "permissions": normalize_permissions(self.permissions),
        }

    def with_updates(
        self,
        *,
        user_name: str | None = None,
        key_name: str | None = None,
        enabled: bool | None = None,
        permissions: Mapping[str, object] | None = None,
        last_used_at_utc: str | None = None,
    ) -> "ApiKeyRecord":
        return ApiKeyRecord(
            id=self.id,
            user_name=(user_name if user_name is not None else self.user_name).strip()
            or default_api_user_name(),
            key_name=(key_name if key_name is not None else self.key_name).strip(),
            key_prefix=self.key_prefix,
            key_suffix=self.key_suffix,
            key_hash=self.key_hash,
            created_at_utc=self.created_at_utc,
            last_used_at_utc=(
                last_used_at_utc
                if last_used_at_utc is not None
                else self.last_used_at_utc
            ),
            enabled=bool(self.enabled if enabled is None else enabled),
            permissions=normalize_permissions(
                permissions if permissions is not None else self.permissions
            ),
        )


class ApiKeyStore:
    """Persist hashed API keys and authorize incoming API requests."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._records: list[ApiKeyRecord] | None = None

    @property
    def path(self) -> Path:
        return self._path

    def list_keys(self) -> list[ApiKeyRecord]:
        with self._lock:
            return list(self._load_locked())

    def create_key(
        self,
        *,
        user_name: str,
        key_name: str = "",
        permissions: Mapping[str, object] | None = None,
    ) -> tuple[str, ApiKeyRecord]:
        api_key = f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
        record = ApiKeyRecord(
            id=secrets.token_hex(8),
            user_name=str(user_name or default_api_user_name()).strip()
            or default_api_user_name(),
            key_name=str(key_name or "").strip(),
            key_prefix=api_key[:API_KEY_DISPLAY_PREFIX_LENGTH],
            key_suffix=api_key[-API_KEY_DISPLAY_SUFFIX_LENGTH:],
            key_hash=_hash_api_key(api_key),
            created_at_utc=_utc_now(),
            enabled=True,
            permissions=normalize_permissions(
                permissions,
                defaults=DEFAULT_API_KEY_PERMISSIONS,
            ),
        )
        with self._lock:
            records = self._load_locked()
            records.append(record)
            self._save_locked(records)
        return api_key, record

    def update_key(
        self,
        record_id: str,
        *,
        user_name: str | None = None,
        key_name: str | None = None,
        enabled: bool | None = None,
        permissions: Mapping[str, object] | None = None,
    ) -> ApiKeyRecord | None:
        with self._lock:
            records = self._load_locked()
            updated: ApiKeyRecord | None = None
            for index, record in enumerate(records):
                if record.id != record_id:
                    continue
                updated = record.with_updates(
                    user_name=user_name,
                    key_name=key_name,
                    enabled=enabled,
                    permissions=permissions,
                )
                records[index] = updated
                break
            if updated is not None:
                self._save_locked(records)
            return updated

    def delete_key(self, record_id: str) -> bool:
        with self._lock:
            records = self._load_locked()
            remaining = [record for record in records if record.id != record_id]
            if len(remaining) == len(records):
                return False
            self._save_locked(remaining)
            return True

    def authorize(self, api_key: str | None, permission: str) -> dict[str, Any]:
        if not str(api_key or "").strip():
            return {
                "accepted": False,
                "status_code": 401,
                "message": "API key required.",
            }
        permission = str(permission or "").strip()
        with self._lock:
            records = self._load_locked()
            api_key_hash = _hash_api_key(str(api_key).strip())
            match_index = -1
            for index, record in enumerate(records):
                if hmac.compare_digest(record.key_hash, api_key_hash):
                    match_index = index
                    break
            if match_index < 0:
                return {
                    "accepted": False,
                    "status_code": 401,
                    "message": "Invalid API key.",
                }
            record = records[match_index]
            if not record.enabled:
                return {
                    "accepted": False,
                    "status_code": 401,
                    "message": "API key is disabled.",
                }
            permissions = normalize_permissions(record.permissions)
            if permission and not permissions.get(permission, False):
                return {
                    "accepted": False,
                    "status_code": 403,
                    "message": f"API key is missing permission: {permission}.",
                    "key_id": record.id,
                    "user_name": record.user_name,
                }
            updated = record.with_updates(last_used_at_utc=_utc_now())
            records[match_index] = updated
            self._save_locked(records)
            return {
                "accepted": True,
                "status_code": 200,
                "key_id": updated.id,
                "user_name": updated.user_name,
                "permissions": permissions,
            }

    def _load_locked(self) -> list[ApiKeyRecord]:
        if self._records is not None:
            return self._records
        self._records = []
        if not self._path.exists():
            return self._records
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return self._records
        raw_records = raw.get("keys") if isinstance(raw, dict) else raw
        if isinstance(raw_records, list):
            self._records = [
                ApiKeyRecord.from_dict(item)
                for item in raw_records
                if isinstance(item, Mapping)
                and str(item.get("key_hash") or item.get("hash") or "").strip()
            ]
        return self._records

    def _save_locked(self, records: list[ApiKeyRecord]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "keys": [record.to_dict() for record in records],
        }
        temp_path = self._path.with_name(f"{self._path.name}.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        temp_path.replace(self._path)
        self._records = list(records)
