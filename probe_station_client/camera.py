"""Camera controls for the probe-station API client."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping


if TYPE_CHECKING:
    from .client import ProbeStationClient


@dataclass(frozen=True)
class CameraFrame:
    """PNG camera frame returned by the probe station API."""

    data: bytes
    counter: int
    space: str
    width: int
    height: int


class ProbeStationCameraClient:
    """Operator camera controls exposed by the probe station API."""

    def __init__(self, client: ProbeStationClient) -> None:
        self._client = client

    def settings(self, names: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
        path = "/api/v1/camera/settings"
        if names is not None:
            query = urllib.parse.urlencode(
                [("name", str(name)) for name in names]
            )
            path = f"{path}?{query}"
        return self._client._request("GET", path)

    def update_settings(
        self,
        settings: Mapping[str, object]
        | list[tuple[str, object]]
        | tuple[tuple[str, object], ...],
    ) -> dict[str, Any]:
        items = settings.items() if isinstance(settings, Mapping) else settings
        payload = {
            "settings": [
                {"name": str(name), "value": value}
                for name, value in items
            ]
        }
        return self._client._request("PATCH", "/api/v1/camera/settings", payload)

    def frame(
        self,
        *,
        space: str = "raw",
        after_counter: int | None = None,
        timeout_ms: int = 2000,
    ) -> CameraFrame:
        query_items: list[tuple[str, object]] = [("space", str(space))]
        if after_counter is not None:
            query_items.append(("after_counter", int(after_counter)))
        query_items.append(("timeout_ms", int(timeout_ms)))
        path = f"/api/v1/camera/frame?{urllib.parse.urlencode(query_items)}"
        data, headers = self._client._request_bytes_with_headers("GET", path)
        normalized_headers = {
            str(name).lower(): str(value) for name, value in headers.items()
        }
        return CameraFrame(
            data=data,
            counter=int(normalized_headers.get("x-camera-frame-counter", "0")),
            space=normalized_headers.get("x-camera-frame-space", str(space)),
            width=int(normalized_headers.get("x-camera-frame-width", "0")),
            height=int(normalized_headers.get("x-camera-frame-height", "0")),
        )

    def exposure_policy(self) -> dict[str, Any]:
        return self._client._request("GET", "/api/v1/camera/exposure-policy")

    def set_exposure_policy(
        self,
        *,
        auto_enabled: bool,
        engine: str,
    ) -> dict[str, Any]:
        return self._client._request(
            "PUT",
            "/api/v1/camera/exposure-policy",
            {"auto_enabled": bool(auto_enabled), "engine": str(engine)},
        )

    def exposure_once(self, *, timeout_s: float = 30.0) -> dict[str, Any]:
        return self._client._request(
            "POST",
            "/api/v1/camera/exposure-once",
            {},
            timeout_s=float(timeout_s),
        )
