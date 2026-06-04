"""HTTP client for the probe station control API."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from .credentials import (
    DEFAULT_BASE_URL,
    ENV_BASE_URL,
    CredentialNotFoundError,
    CredentialStore,
)


Transport = Callable[
    [str, str, Mapping[str, str], bytes | None, float],
    tuple[int, Mapping[str, str], bytes],
]


class ProbeStationClientError(RuntimeError):
    """Base class for probe station client failures."""


class ProbeStationConnectionError(ProbeStationClientError):
    """Raised when the API server cannot be reached."""


class ProbeStationApiError(ProbeStationClientError):
    """Raised for non-success API responses."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        detail: object | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.detail = detail


class AuthenticationError(ProbeStationApiError):
    """Raised for missing or invalid API keys."""


class PermissionDeniedError(ProbeStationApiError):
    """Raised when the API key lacks a required permission."""


class ProbeStationMeterClient:
    """Measurement-instrument namespace for the probe station API client."""

    def __init__(self, client: "ProbeStationClient") -> None:
        self._client = client

    def configure(self, **configuration: Any) -> dict[str, Any]:
        return self._client._request(
            "POST",
            "/api/v1/meter/configure",
            dict(configuration),
        )

    def raw_sweep(
        self,
        voltages_v: list[float] | tuple[float, ...],
        **options: Any,
    ) -> dict[str, Any]:
        payload = dict(options)
        payload["voltages_v"] = [float(value) for value in voltages_v]
        return self._client._request(
            "POST",
            "/api/v1/measurements/raw-sweep",
            payload,
        )


def _urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout_s: float,
) -> tuple[int, Mapping[str, str], bytes]:
    request = urllib.request.Request(
        url,
        data=body,
        headers=dict(headers),
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return (
                int(response.status),
                dict(response.headers.items()),
                response.read(),
            )
    except urllib.error.HTTPError as exc:
        return int(exc.code), dict(exc.headers.items()), exc.read()
    except urllib.error.URLError as exc:
        raise ProbeStationConnectionError(str(exc.reason)) from exc


class ProbeStationClient:
    """Small Python client for the local probe-station FastAPI server."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        credential_store: CredentialStore | None = None,
        profile: str = "default",
        timeout_s: float = 10.0,
        transport: Transport | None = None,
    ) -> None:
        self.base_url = (
            str(base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL)
            .strip()
            .rstrip("/")
        )
        self.timeout_s = float(timeout_s)
        self.credential_store = credential_store or CredentialStore(profile=profile)
        self._transport = transport or _urllib_transport
        self.api_key = str(api_key).strip() if api_key else self._load_api_key()
        self.meter = ProbeStationMeterClient(self)

    def reload_api_key(self) -> str | None:
        """Reload the API key from the configured credential store."""

        self.api_key = self._load_api_key()
        return self.api_key

    def save_api_key(self, api_key: str, *, backend: str = "auto") -> str:
        """Persist and activate an API key for this client profile."""

        backend_used = self.credential_store.save_api_key(api_key, backend=backend)
        self.api_key = str(api_key).strip()
        return backend_used

    def delete_saved_api_key(self) -> None:
        """Delete the saved API key for this client profile."""

        self.credential_store.delete_api_key()
        self.api_key = None

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health", auth=False)

    def stage_status(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/stage/status")

    def stage_position(self) -> dict[str, float]:
        status = self.stage_status()
        position = status.get("position")
        return dict(position) if isinstance(position, dict) else {}

    def move_stage(
        self,
        coordinates: Mapping[str, float] | None = None,
        *,
        mode: str = "absolute",
        feedrate: float | None = None,
        relative: bool | None = None,
        **axes: float,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        targets: dict[str, float] = {}
        if coordinates:
            targets.update(
                {
                    str(axis).upper(): float(value)
                    for axis, value in coordinates.items()
                }
            )
        for axis, value in axes.items():
            if value is not None:
                targets[str(axis).upper()] = float(value)
        payload["coordinates"] = targets
        if relative is None:
            payload["mode"] = mode
        else:
            payload["relative"] = bool(relative)
        if feedrate is not None:
            payload["feedrate"] = float(feedrate)
        return self._request("POST", "/api/v1/stage/move", payload)

    def move_to(
        self,
        coordinates: Mapping[str, float] | None = None,
        *,
        feedrate: float | None = None,
        **axes: float,
    ) -> dict[str, Any]:
        return self.move_stage(
            coordinates,
            mode="absolute",
            feedrate=feedrate,
            **axes,
        )

    def move_by(
        self,
        coordinates: Mapping[str, float] | None = None,
        *,
        feedrate: float | None = None,
        **axes: float,
    ) -> dict[str, Any]:
        return self.move_stage(
            coordinates,
            mode="relative",
            feedrate=feedrate,
            **axes,
        )

    def route_contacts(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/route/contacts")

    def move_to_contact(self, contact_number: int, **options: Any) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/route/contacts/{int(contact_number)}/move",
            dict(options),
        )

    def contact_needles(
        self,
        contact_number: int,
        *,
        action: str = "lower",
        **options: Any,
    ) -> dict[str, Any]:
        payload = dict(options)
        payload["action"] = str(action)
        return self._request(
            "POST",
            f"/api/v1/route/contacts/{int(contact_number)}/needles",
            payload,
        )

    def check_contact(self, contact_number: int, **options: Any) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/route/contacts/{int(contact_number)}/check",
            dict(options),
        )

    def contact_seek(self, contact_number: int, **options: Any) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/route/contacts/{int(contact_number)}/seek",
            dict(options),
        )

    def prepare_contact(self, contact_number: int, **options: Any) -> dict[str, Any]:
        """Client-side recipe that prepares one route contact for measurement."""

        payload = dict(options)
        meter = self._pop_option(payload, "meter", "meter_configuration")
        move_to_contact = self._pop_bool_option(
            payload,
            "move_to_contact",
            "move",
            default=True,
        )
        lower_needles = self._pop_bool_option(
            payload,
            "lower_needles",
            "lower",
            default=True,
        )
        lift_before_move = self._pop_bool_option(
            payload,
            "lift_before_move",
            default=True,
        )
        lift_on_failure = self._pop_bool_option(
            payload,
            "lift_on_failure",
            default=True,
        )
        steps: dict[str, Any] = {}
        if isinstance(meter, Mapping) and meter:
            steps["meter.configure"] = self.meter.configure(**meter)
        if move_to_contact:
            move_payload = self._contact_motion_payload(payload)
            move_payload["lift_before_move"] = lift_before_move
            move_payload["lower_needles"] = False
            steps["move_to_contact"] = self.move_to_contact(
                contact_number,
                **move_payload,
            )
        if lower_needles:
            steps["lower_needles"] = self.contact_needles(
                contact_number,
                action="lower",
                **self._contact_motion_payload(payload),
            )
        check = self.check_contact(contact_number, **payload)
        steps["check_contact"] = check
        final = check
        seek: dict[str, Any] | None = None
        if not self._contact_response_ok(check):
            seek = self.contact_seek(contact_number, **payload)
            steps["contact_seek"] = seek
            final = seek
        success = self._contact_response_ok(final)
        lifted_on_failure = False
        if not success and lift_on_failure:
            steps["lift_on_failure"] = self.contact_needles(
                contact_number,
                action="lift",
                **self._contact_motion_payload(payload),
            )
            lifted_on_failure = True
        response = dict(final)
        response["accepted"] = bool(success)
        response["prepared"] = bool(success)
        response["needles_lowered"] = bool(success and lower_needles)
        response["moved_to_contact"] = bool(move_to_contact)
        response["lifted_before_move"] = bool(move_to_contact and lift_before_move)
        response["lifted_on_failure"] = lifted_on_failure
        response["check"] = check
        response["steps"] = steps
        if seek is not None:
            response["seek"] = seek
            response["contact_seek"] = seek.get("contact_seek")
        if success:
            response["message"] = str(final.get("message") or "Contact ready.")
        else:
            response["message"] = str(
                final.get("message") or "Contact preparation failed."
            )
        return response

    @staticmethod
    def _pop_option(
        payload: dict[str, Any],
        *names: str,
        default: Any = None,
    ) -> Any:
        for name in names:
            if name in payload:
                return payload.pop(name)
        return default

    @classmethod
    def _pop_bool_option(
        cls,
        payload: dict[str, Any],
        *names: str,
        default: bool,
    ) -> bool:
        value = cls._pop_option(payload, *names, default=default)
        return cls._coerce_bool(value, default=default)

    @staticmethod
    def _coerce_bool(value: Any, *, default: bool) -> bool:
        if value is None:
            return bool(default)
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return bool(value)

    @staticmethod
    def _contact_motion_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        keys = (
            "needle_feedrate",
            "needle_feedrate_mm_min",
            "needle_feedrate_mm_per_min",
        )
        return {key: payload[key] for key in keys if key in payload}

    @staticmethod
    def _contact_response_ok(response: Mapping[str, Any]) -> bool:
        if "contact_ok" in response:
            return bool(response.get("contact_ok"))
        if "contact_found" in response:
            return bool(response.get("contact_found"))
        measurement = response.get("measurement")
        if isinstance(measurement, Mapping):
            status = str(measurement.get("status") or "").strip().lower()
            return status in {"ok", "short"}
        return bool(response.get("accepted", False))

    def _load_api_key(self) -> str | None:
        try:
            return self.credential_store.load_api_key()
        except CredentialNotFoundError:
            return None

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        auth: bool = True,
    ) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(dict(payload)).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        status_code, _response_headers, response_body = self._transport(
            method.upper(),
            self._url_for(path),
            headers,
            body,
            self.timeout_s,
        )
        response = self._decode_response(response_body)
        if 200 <= status_code < 300:
            return response
        self._raise_api_error(status_code, response)
        raise AssertionError("unreachable")

    def _url_for(self, path: str) -> str:
        return f"{self.base_url}/{str(path).lstrip('/')}"

    @staticmethod
    def _decode_response(response_body: bytes) -> dict[str, Any]:
        if not response_body:
            return {}
        try:
            decoded = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"raw": response_body.decode("utf-8", errors="replace")}
        return decoded if isinstance(decoded, dict) else {"value": decoded}

    @staticmethod
    def _raise_api_error(status_code: int, response: dict[str, Any]) -> None:
        detail = response.get("detail", response)
        message = "Probe station API request failed."
        if isinstance(detail, dict):
            message = str(detail.get("message") or message)
        elif detail:
            message = str(detail)
        error_class: type[ProbeStationApiError]
        if status_code == 401:
            error_class = AuthenticationError
        elif status_code == 403:
            error_class = PermissionDeniedError
        else:
            error_class = ProbeStationApiError
        raise error_class(message, status_code=status_code, detail=detail)
