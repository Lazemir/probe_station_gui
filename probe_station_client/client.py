"""HTTP client for the probe station control API."""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import camera as _camera
from . import meter as _meter
from . import route as _route
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

    _route_stopped_error = ProbeStationClientError

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
        self.camera = _camera.ProbeStationCameraClient(self)
        self.meter = _meter.ProbeStationMeterClient(self)
        self.api_route_control = _route.ProbeStationApiRouteControlClient(self)
        self.route = _route.ProbeStationRouteClient(self)

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

    def local_focus(self, **options: Any) -> dict[str, Any]:
        """Run the API local static autofocus at the current stage position."""

        return self._request("POST", "/api/v1/stage/focus/local", dict(options))

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

    def contact_focus(self, contact_number: int, **options: Any) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/route/contacts/{int(contact_number)}/focus",
            dict(options),
        )

    def contact_photo(self, contact_number: int) -> bytes:
        return self._request_bytes(
            "GET",
            f"/api/v1/route/contacts/{int(contact_number)}/photo",
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
        raise_before_move = self._pop_bool_option(
            payload,
            "raise_before_move",
            "raise_needles_before_move",
            default=False,
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
        focus_before_lower = self._pop_bool_option(
            payload,
            "focus_before_lower",
            "focus",
            default=False,
        )
        photo_path = self._pop_option(
            payload,
            "photo_path",
            "contact_photo_path",
            default=None,
        )
        capture_photo_before_lower = self._pop_bool_option(
            payload,
            "capture_photo_before_lower",
            "photo_before_lower",
            "photo",
            default=photo_path is not None,
        )
        seek_before_measurement = self._pop_bool_option(
            payload,
            "seek_before_measurement",
            "seek_before_check",
            "force_seek",
            "always_seek",
            default=False,
        )
        seek_attempts = self._pop_int_option(
            payload,
            "seek_attempts",
            "contact_seek_attempts",
            default=1,
        )
        reference_resistance_ohm = self._pop_float_option(
            payload,
            "reference_resistance_ohm",
            "target_resistance_ohm",
            default=None,
        )
        if reference_resistance_ohm is None and "expected_resistance_ohm" in payload:
            reference_resistance_ohm = self._coerce_float(
                payload.get("expected_resistance_ohm"),
                default=None,
            )
        reference_resistance_relative_tolerance = self._pop_float_option(
            payload,
            "reference_resistance_relative_tolerance",
            "resistance_relative_tolerance",
            "resistance_tolerance_fraction",
            default=math.inf,
        )
        if (
            reference_resistance_relative_tolerance is not None
            and reference_resistance_relative_tolerance < 0.0
        ):
            reference_resistance_relative_tolerance = 0.0
        focus_payload = self._contact_focus_payload(payload)
        steps: dict[str, Any] = {}

        def failure_response(
            step_response: Mapping[str, Any],
            *,
            message: str,
            moved: bool,
        ) -> dict[str, Any]:
            response = dict(step_response)
            response["accepted"] = False
            response["prepared"] = False
            response["needles_lowered"] = False
            response["moved_to_contact"] = bool(moved)
            response["lifted_before_move"] = bool(move_to_contact and lift_before_move)
            response["raised_before_move"] = bool(raise_before_move)
            response["lifted_on_failure"] = False
            response["check"] = None
            response["steps"] = steps
            response["message"] = str(step_response.get("message") or message)
            return response

        if isinstance(meter, Mapping) and meter:
            steps["meter.configure"] = self.meter.configure(**meter)
        if raise_before_move:
            raise_step = self.contact_needles(
                contact_number,
                action="raise",
                **self._contact_motion_payload(payload),
            )
            steps["raise_needles"] = raise_step
            if not self._contact_step_accepted(raise_step):
                return failure_response(
                    raise_step,
                    message="Needle raise failed.",
                    moved=False,
                )
        if move_to_contact:
            move_payload = self._contact_motion_payload(payload)
            move_payload["lift_before_move"] = lift_before_move
            move_payload["lower_needles"] = False
            move_step = self.move_to_contact(
                contact_number,
                **move_payload,
            )
            steps["move_to_contact"] = move_step
            if not self._contact_step_accepted(move_step):
                return failure_response(
                    move_step,
                    message="Contact move failed.",
                    moved=False,
                )
        if focus_before_lower:
            focus = self.contact_focus(contact_number, **focus_payload)
            steps["focus_before_lower"] = focus
            if not bool(focus.get("accepted", False)):
                return failure_response(
                    focus,
                    message="Contact focus failed.",
                    moved=move_to_contact,
                )
        if capture_photo_before_lower:
            try:
                photo_bytes = self.contact_photo(contact_number)
                photo_step: dict[str, Any] = {
                    "accepted": True,
                    "bytes": len(photo_bytes),
                }
                if photo_path is not None:
                    resolved_photo_path = Path(photo_path).expanduser()
                    resolved_photo_path.parent.mkdir(parents=True, exist_ok=True)
                    resolved_photo_path.write_bytes(photo_bytes)
                    photo_step["path"] = str(resolved_photo_path)
                steps["photo_before_lower"] = photo_step
            except Exception as exc:
                return failure_response(
                    {
                        "accepted": False,
                        "message": f"Contact photo failed: {exc}",
                    },
                    message="Contact photo failed.",
                    moved=move_to_contact,
                )
        if lower_needles:
            lower_step = self.contact_needles(
                contact_number,
                action="lower",
                **self._contact_motion_payload(payload),
            )
            steps["lower_needles"] = lower_step
            if not self._contact_step_accepted(lower_step):
                return failure_response(
                    lower_step,
                    message="Needle lower failed.",
                    moved=move_to_contact,
                )
        seek: dict[str, Any] | None = None
        check: dict[str, Any] | None = None
        if seek_before_measurement:
            final = self.contact_seek(contact_number, **payload)
            seek = final
            steps["contact_seek_1"] = seek
        else:
            check = self.check_contact(contact_number, **payload)
            steps["check_contact"] = check
            final = check

        success, resistance_summary = self._contact_response_ready(
            final,
            reference_resistance_ohm=reference_resistance_ohm,
            reference_resistance_relative_tolerance=reference_resistance_relative_tolerance,
        )
        attempts_used = 1 if seek_before_measurement else 0
        while not success and attempts_used < max(1, seek_attempts):
            attempts_used += 1
            seek = self.contact_seek(contact_number, **payload)
            steps[f"contact_seek_{attempts_used}"] = seek
            final = seek
            success, resistance_summary = self._contact_response_ready(
                final,
                reference_resistance_ohm=reference_resistance_ohm,
                reference_resistance_relative_tolerance=reference_resistance_relative_tolerance,
            )
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
        response["needles_lowered"] = bool(
            lower_needles and (success or not lifted_on_failure)
        )
        response["moved_to_contact"] = bool(move_to_contact)
        response["lifted_before_move"] = bool(move_to_contact and lift_before_move)
        response["raised_before_move"] = bool(raise_before_move)
        response["lifted_on_failure"] = lifted_on_failure
        response["check"] = check
        response["steps"] = steps
        response["seek_attempts"] = attempts_used
        if photo_path is not None:
            response["photo_path"] = str(Path(photo_path).expanduser())
        response["reference_resistance_ohm"] = reference_resistance_ohm
        response["reference_resistance_relative_tolerance"] = (
            reference_resistance_relative_tolerance
        )
        response.update(resistance_summary)
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

    @classmethod
    def _pop_int_option(
        cls,
        payload: dict[str, Any],
        *names: str,
        default: int,
    ) -> int:
        value = cls._pop_option(payload, *names, default=default)
        return cls._coerce_int(value, default=default)

    @classmethod
    def _pop_float_option(
        cls,
        payload: dict[str, Any],
        *names: str,
        default: float | None,
    ) -> float | None:
        value = cls._pop_option(payload, *names, default=default)
        return cls._coerce_float(value, default=default)

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
    def _coerce_int(value: Any, *, default: int) -> int:
        if value is None:
            return int(default)
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            return int(default)
        return number

    @staticmethod
    def _coerce_float(value: Any, *, default: float | None) -> float | None:
        if value is None:
            return default
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else default

    @staticmethod
    def _contact_motion_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        keys = (
            "needle_feedrate",
            "needle_feedrate_mm_min",
            "needle_feedrate_mm_per_min",
        )
        return {key: payload[key] for key in keys if key in payload}

    @staticmethod
    def _contact_step_accepted(response: Mapping[str, Any]) -> bool:
        return bool(response.get("accepted", False))

    @staticmethod
    def _contact_focus_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if "focus_range_mm" in payload:
            result["range_mm"] = payload["focus_range_mm"]
        if "range_mm" in payload:
            result["range_mm"] = payload["range_mm"]
        if "focus_step_mm" in payload:
            result["step_mm"] = payload["focus_step_mm"]
        if "step_mm" in payload:
            result["step_mm"] = payload["step_mm"]
        return result

    @classmethod
    def _contact_response_ready(
        cls,
        response: Mapping[str, Any],
        *,
        reference_resistance_ohm: float | None,
        reference_resistance_relative_tolerance: float | None,
    ) -> tuple[bool, dict[str, Any]]:
        contact_ok = cls._contact_response_ok(response)
        measured = cls._contact_response_resistance_ohm(response)
        summary: dict[str, Any] = {
            "measured_resistance_ohm": measured,
            "resistance_relative_error": None,
            "resistance_match": None,
        }
        if reference_resistance_ohm is None:
            return contact_ok, summary
        if reference_resistance_ohm <= 0.0 or measured is None:
            summary["resistance_match"] = False
            return False, summary
        relative_error = abs(measured - reference_resistance_ohm) / reference_resistance_ohm
        tolerance = (
            reference_resistance_relative_tolerance
            if reference_resistance_relative_tolerance is not None
            else math.inf
        )
        resistance_match = relative_error <= tolerance
        summary["resistance_relative_error"] = relative_error
        summary["resistance_match"] = resistance_match
        return contact_ok and resistance_match, summary

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

    @classmethod
    def _contact_response_resistance_ohm(
        cls,
        response: Mapping[str, Any],
    ) -> float | None:
        measurement = response.get("measurement")
        if isinstance(measurement, Mapping):
            quality = measurement.get("contact_quality")
            if isinstance(quality, Mapping):
                for key in ("median_ohm", "resistance_median_ohm"):
                    number = cls._coerce_float(quality.get(key), default=None)
                    if number is not None:
                        return number
            for key in (
                "contact_median_ohm",
                "median_ohm",
                "resistance_ohm",
                "resistance_median_ohm",
            ):
                number = cls._coerce_float(measurement.get(key), default=None)
                if number is not None:
                    return number
        for key in (
            "contact_median_ohm",
            "measured_resistance_ohm",
            "resistance_ohm",
        ):
            number = cls._coerce_float(response.get(key), default=None)
            if number is not None:
                return number
        return None

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
        timeout_s: float | None = None,
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
            self.timeout_s if timeout_s is None else float(timeout_s),
        )
        response = self._decode_response(response_body)
        if 200 <= status_code < 300:
            return response
        self._raise_api_error(status_code, response)
        raise AssertionError("unreachable")

    def _request_bytes(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        auth: bool = True,
    ) -> bytes:
        data, _headers = self._request_bytes_with_headers(
            method,
            path,
            payload,
            auth=auth,
        )
        return data

    def _request_bytes_with_headers(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        auth: bool = True,
    ) -> tuple[bytes, Mapping[str, str]]:
        body = None
        headers = {"Accept": "*/*"}
        if payload is not None:
            body = json.dumps(dict(payload)).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        status_code, response_headers, response_body = self._transport(
            method.upper(),
            self._url_for(path),
            headers,
            body,
            self.timeout_s,
        )
        if 200 <= status_code < 300:
            return bytes(response_body), response_headers
        response = self._decode_response(response_body)
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
