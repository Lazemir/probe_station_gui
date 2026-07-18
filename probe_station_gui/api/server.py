"""FastAPI control surface for the probe station GUI."""

from __future__ import annotations

import logging
import os
import threading
import math
from typing import Any, Callable

from probe_station_gui.api.keys import (
    API_PERMISSION_CAMERA_READ,
    API_PERMISSION_CAMERA_WRITE,
    API_PERMISSION_ROUTE_MEASURE,
    API_PERMISSION_ROUTE_READ,
    API_PERMISSION_STAGE_READ,
    API_PERMISSION_STAGE_WRITE,
)


logger = logging.getLogger(__name__)

_AXIS_NAMES = ("X", "Y", "Z", "A", "B", "C")
_MAX_SWEEP_POINTS = 1000
_MAX_CAMERA_FRAME_TIMEOUT_MS = 30_000
_NO_PAYLOAD = object()


def _axis_targets_from_payload(payload: dict[str, Any]) -> dict[str, float]:
    targets: dict[str, float] = {}
    coordinates = payload.get("coordinates")
    if isinstance(coordinates, dict):
        for axis, value in coordinates.items():
            targets[str(axis).strip().upper()] = float(value)
    for axis in _AXIS_NAMES:
        key = axis.lower()
        if key in payload:
            targets[axis] = float(payload[key])
        elif axis in payload:
            targets[axis] = float(payload[axis])
    return targets


def _coordinate_mode_from_payload(payload: dict[str, Any]) -> str:
    """Return the requested coordinate input mode as G90 or G91."""

    raw_mode = payload.get("mode", payload.get("coordinate_mode", None))
    if raw_mode is None and "relative" in payload:
        return "G91" if bool(payload.get("relative")) else "G90"
    mode = str(raw_mode or "G90").strip().lower()
    if mode in {"", "absolute", "abs", "g90"}:
        return "G90"
    if mode in {"relative", "rel", "g91"}:
        return "G91"
    raise ValueError(f"Unsupported coordinate mode: {raw_mode!r}")


def _feedrate_from_payload(payload: dict[str, Any]) -> float | None:
    """Extract an optional positive feedrate from the request payload."""

    for key in ("feedrate", "feed_rate", "feedrate_mm_min", "feedrate_mm_per_min", "f"):
        if key not in payload or payload.get(key) is None:
            continue
        value = float(payload[key])
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError("Feedrate must be a positive finite number.")
        return value
    return None


def _voltage_sweep_from_payload(payload: dict[str, Any]) -> list[float]:
    """Extract a finite source-voltage sweep from an API payload."""

    raw_values = None
    for key in ("voltages_v", "voltages", "voltage_sweep_v", "source_voltages_v"):
        if key in payload:
            raw_values = payload.get(key)
            break
    if not isinstance(raw_values, (list, tuple)):
        raise ValueError("Provide voltages_v as a non-empty array.")
    if not raw_values:
        raise ValueError("Voltage sweep must contain at least one point.")
    if len(raw_values) > _MAX_SWEEP_POINTS:
        raise ValueError(
            f"Voltage sweep is too large; maximum is {_MAX_SWEEP_POINTS} points."
        )
    values: list[float] = []
    for index, raw_value in enumerate(raw_values):
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Voltage at index {index} is not a number.") from exc
        if not math.isfinite(value):
            raise ValueError(f"Voltage at index {index} must be finite.")
        values.append(value)
    return values


def _extract_api_key(
    authorization: str | None,
    x_api_key: str | None,
) -> str | None:
    bearer_prefix = "bearer "
    header = str(authorization or "").strip()
    if header.lower().startswith(bearer_prefix):
        return header[len(bearer_prefix) :].strip() or None
    if header and " " not in header:
        return header
    x_key = str(x_api_key or "").strip()
    return x_key or None


def _raise_for_rejected(result: dict[str, Any]) -> None:
    if result.get("accepted", False):
        return
    from fastapi import HTTPException

    status_code = int(result.get("status_code", 409))
    detail = {
        "message": result.get("message", "API request rejected."),
        "result": result,
    }
    raise HTTPException(status_code=status_code, detail=detail)


class ProbeStationApiServer:
    """Run a FastAPI app in a background thread.

    The callbacks are expected to be thread-safe from the API server side. In
    the GUI they are routed through a queued Qt signal before touching widgets
    or the stage controller.
    """

    def __init__(
        self,
        *,
        move_callback: Callable[[dict[str, Any]], dict[str, Any]],
        status_callback: Callable[[], dict[str, Any]],
        command_callback: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        auth_callback: Callable[[str | None, str], dict[str, Any]] | None = None,
        camera_settings_read_callback: Callable[
            [list[str] | None], dict[str, Any]
        ]
        | None = None,
        camera_settings_write_callback: Callable[
            [list[dict[str, Any]]], dict[str, Any]
        ]
        | None = None,
        camera_frame_callback: Callable[
            [str, int | None, float], dict[str, Any]
        ]
        | None = None,
        camera_exposure_policy_snapshot_callback: Callable[[], dict[str, Any]] | None = None,
        camera_exposure_policy_set_callback: Callable[..., dict[str, Any]] | None = None,
        camera_exposure_once_callback: Callable[[], dict[str, Any]] | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        self.host = host or os.environ.get("PROBE_STATION_API_HOST", "127.0.0.1")
        self.port = int(port) if port is not None else self._port_from_environment()
        self._move_callback = move_callback
        self._status_callback = status_callback
        self._command_callback = command_callback
        self._auth_callback = auth_callback
        self._camera_settings_read_callback = camera_settings_read_callback
        self._camera_settings_write_callback = camera_settings_write_callback
        self._camera_frame_callback = camera_frame_callback
        self._camera_exposure_policy_snapshot_callback = (
            camera_exposure_policy_snapshot_callback
        )
        self._camera_exposure_policy_set_callback = camera_exposure_policy_set_callback
        self._camera_exposure_once_callback = camera_exposure_once_callback
        self._server: object | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @staticmethod
    def _port_from_environment() -> int:
        raw_port = os.environ.get("PROBE_STATION_API_PORT", "8765")
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            logger.warning("Invalid PROBE_STATION_API_PORT=%r; using 8765.", raw_port)
            return 8765
        if port <= 0 or port > 65535:
            logger.warning("Invalid PROBE_STATION_API_PORT=%r; using 8765.", raw_port)
            return 8765
        return port

    def start(self) -> tuple[bool, str]:
        if self._thread is not None and self._thread.is_alive():
            return True, f"FastAPI control API is already running at {self.url}"
        try:
            app, uvicorn = self._create_app()
        except ImportError as exc:
            logger.warning("FastAPI control API unavailable: %s", exc)
            return (
                False,
                "FastAPI control API is unavailable. Install fastapi and uvicorn.",
            )
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_config=None,
            access_log=False,
        )
        server = uvicorn.Server(config)
        self._server = server
        self._thread = threading.Thread(
            target=self._run_server,
            args=(server,),
            name="ProbeStationFastAPI",
            daemon=True,
        )
        self._thread.start()
        return True, f"FastAPI control API listening at {self.url}"

    def stop(self) -> None:
        server = self._server
        if server is not None:
            try:
                server.should_exit = True
            except AttributeError:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        self._server = None

    def _run_server(self, server: object) -> None:
        try:
            server.run()
        except Exception:
            logger.exception("FastAPI control API stopped unexpectedly.")

    def _create_app(self):
        from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
        from fastapi.responses import HTMLResponse, JSONResponse, Response
        import uvicorn

        app = FastAPI(
            title="Probe Station Control API",
            version="0.1.0",
            docs_url="/docs",
            redoc_url="/redoc",
        )

        @app.get("/health")
        def health() -> dict[str, str]:
            return {"status": "ok"}

        @app.get("/", include_in_schema=False)
        def docs_index() -> HTMLResponse:
            return HTMLResponse(
                """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Probe Station API</title>
  <style>
    body { font: 16px/1.4 system-ui, sans-serif; margin: 2rem; }
    a { display: block; margin: 0.5rem 0; }
  </style>
</head>
<body>
  <h1>Probe Station API</h1>
  <a href="/docs">Swagger UI</a>
  <a href="/redoc">ReDoc</a>
</body>
</html>
"""
            )

        def api_auth_headers(
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> tuple[str | None, str | None]:
            return authorization, x_api_key

        def authorize_request(
            permission: str,
            auth_headers: tuple[str | None, str | None],
        ) -> None:
            authorization, x_api_key = auth_headers
            self._authorize(permission, authorization, x_api_key)

        def dispatch_command_result(
            action: str,
            payload: object = _NO_PAYLOAD,
        ) -> dict[str, Any]:
            request: dict[str, Any] = {"action": action}
            if payload is not _NO_PAYLOAD:
                request["payload"] = payload
            result = self._call_command(request)
            _raise_for_rejected(result)
            return result

        def command_result(
            permission: str,
            auth_headers: tuple[str | None, str | None],
            action: str,
            payload: object = _NO_PAYLOAD,
        ) -> dict[str, Any]:
            authorize_request(permission, auth_headers)
            return dispatch_command_result(action, payload)

        def exposure_policy_result(
            callback: Callable[..., dict[str, Any]],
            *args: Any,
            **kwargs: Any,
        ) -> dict[str, Any]:
            from probe_station_gui.camera.exposure_policy import (
                ExposurePolicyBusyError,
                ExposurePolicyError,
            )

            try:
                result = callback(*args, **kwargs)
            except ExposurePolicyBusyError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"message": str(exc)},
                ) from exc
            except ExposurePolicyError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={"message": str(exc)},
                ) from exc
            if not isinstance(result, dict):
                raise HTTPException(
                    status_code=500,
                    detail={"message": "Exposure policy returned an invalid result."},
                )
            if "accepted" in result:
                _raise_for_rejected(result)
            return result

        def contact_payload(
            contact_number: int,
            payload: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            body = dict(payload or {})
            body["contact_number"] = int(contact_number)
            return body

        def visa_payload(
            role: str,
            operation: str,
            payload: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            return {
                **dict(payload or {}),
                "role": role,
                "operation": operation,
            }

        def binary_response(
            result: dict[str, Any],
            *,
            invalid_message: str,
            media_type: str | None = None,
            headers: dict[str, str] | None = None,
        ) -> Response:
            data = result.get("data", b"")
            if not isinstance(data, (bytes, bytearray)):
                raise HTTPException(
                    status_code=500,
                    detail={"message": invalid_message},
                )
            content_type = str(
                result.get("content_type") or media_type or "application/octet-stream"
            )
            return Response(
                content=bytes(data),
                media_type=content_type,
                headers=headers,
            )

        def endpoint_named(name: str, endpoint: Callable[..., Any]) -> Callable[..., Any]:
            endpoint.__name__ = name
            return endpoint

        def add_command_route(
            path: str,
            *,
            method: str,
            name: str,
            permission: str,
            action: str,
        ) -> None:
            def endpoint(
                auth_headers: tuple[str | None, str | None] = Depends(api_auth_headers),
            ) -> dict[str, Any]:
                return command_result(permission, auth_headers, action)

            app.add_api_route(
                path,
                endpoint_named(name, endpoint),
                methods=[method],
            )

        def add_body_command_route(
            path: str,
            *,
            name: str,
            permission: str,
            action: str,
            required: bool = False,
        ) -> None:
            if required:

                def endpoint(
                    payload: dict[str, Any] = Body(...),
                    auth_headers: tuple[str | None, str | None] = Depends(
                        api_auth_headers
                    ),
                ) -> dict[str, Any]:
                    return command_result(
                        permission,
                        auth_headers,
                        action,
                        dict(payload or {}),
                    )

            else:

                def endpoint(
                    payload: dict[str, Any] | None = Body(default=None),
                    auth_headers: tuple[str | None, str | None] = Depends(
                        api_auth_headers
                    ),
                ) -> dict[str, Any]:
                    return command_result(
                        permission,
                        auth_headers,
                        action,
                        dict(payload or {}),
                    )

            app.add_api_route(
                path,
                endpoint_named(name, endpoint),
                methods=["POST"],
            )

        def add_contact_command_route(
            path: str,
            *,
            name: str,
            action: str,
        ) -> None:
            def endpoint(
                contact_number: int,
                payload: dict[str, Any] | None = Body(default=None),
                auth_headers: tuple[str | None, str | None] = Depends(api_auth_headers),
            ) -> dict[str, Any]:
                return command_result(
                    API_PERMISSION_ROUTE_MEASURE,
                    auth_headers,
                    action,
                    contact_payload(contact_number, payload),
                )

            app.add_api_route(
                path,
                endpoint_named(name, endpoint),
                methods=["POST"],
            )

        def add_visa_command_route(
            path: str,
            *,
            name: str,
            operation: str,
            required: bool = False,
            raw_response: bool = False,
        ) -> None:
            if required:

                def endpoint(
                    role: str,
                    payload: dict[str, Any] = Body(...),
                    auth_headers: tuple[str | None, str | None] = Depends(
                        api_auth_headers
                    ),
                ) -> dict[str, Any] | Response:
                    result = command_result(
                        API_PERMISSION_ROUTE_MEASURE,
                        auth_headers,
                        "visa_operation",
                        visa_payload(role, operation, payload),
                    )
                    if raw_response:
                        return binary_response(
                            result,
                            invalid_message="VISA raw response is invalid.",
                        )
                    return result

            else:

                def endpoint(
                    role: str,
                    payload: dict[str, Any] | None = Body(default=None),
                    auth_headers: tuple[str | None, str | None] = Depends(
                        api_auth_headers
                    ),
                ) -> dict[str, Any] | Response:
                    result = command_result(
                        API_PERMISSION_ROUTE_MEASURE,
                        auth_headers,
                        "visa_operation",
                        visa_payload(role, operation, payload),
                    )
                    if raw_response:
                        return binary_response(
                            result,
                            invalid_message="VISA raw response is invalid.",
                        )
                    return result

            app.add_api_route(
                path,
                endpoint_named(name, endpoint),
                methods=["POST"],
                response_model=None,
            )

        @app.get("/api/v1/stage/status")
        def stage_status(
            auth_headers: tuple[str | None, str | None] = Depends(api_auth_headers),
        ) -> dict[str, Any]:
            authorize_request(API_PERMISSION_STAGE_READ, auth_headers)
            return self._status_callback()

        @app.post("/api/v1/stage/move")
        def move_stage(
            payload: dict[str, Any] = Body(...),
            auth_headers: tuple[str | None, str | None] = Depends(api_auth_headers),
        ) -> dict[str, Any]:
            authorize_request(API_PERMISSION_STAGE_WRITE, auth_headers)
            try:
                targets = _axis_targets_from_payload(payload)
                mode = _coordinate_mode_from_payload(payload)
                feedrate = _feedrate_from_payload(payload)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"message": str(exc)},
                ) from exc
            if not targets:
                raise HTTPException(
                    status_code=400,
                    detail={"message": "Provide at least one target coordinate."},
                )
            result = self._move_callback(
                {
                    "targets": targets,
                    "mode": mode,
                    "feedrate": feedrate,
                }
            )
            if not result.get("accepted", False):
                _raise_for_rejected(result)
            return result

        @app.get("/api/v1/camera/settings")
        def camera_settings(
            name: list[str] | None = Query(default=None),
            auth_headers: tuple[str | None, str | None] = Depends(api_auth_headers),
        ) -> dict[str, Any]:
            authorize_request(API_PERMISSION_CAMERA_READ, auth_headers)
            callback = self._camera_settings_read_callback
            if callback is None:
                raise HTTPException(
                    status_code=501,
                    detail={"message": "Camera settings API is unavailable."},
                )
            result = callback(list(name) if name is not None else None)
            _raise_for_rejected(result)
            return result

        @app.patch("/api/v1/camera/settings")
        def update_camera_settings(
            payload: dict[str, Any] = Body(...),
            auth_headers: tuple[str | None, str | None] = Depends(api_auth_headers),
        ) -> dict[str, Any]:
            authorize_request(API_PERMISSION_CAMERA_WRITE, auth_headers)
            raw_settings = payload.get("settings")
            if not isinstance(raw_settings, list) or not raw_settings:
                raise HTTPException(
                    status_code=400,
                    detail={"message": "Provide a non-empty settings array."},
                )
            settings: list[dict[str, Any]] = []
            for item in raw_settings:
                if not isinstance(item, dict):
                    raise HTTPException(
                        status_code=400,
                        detail={"message": "Each camera setting must be an object."},
                    )
                settings.append(dict(item))
            callback = self._camera_settings_write_callback
            if callback is None:
                raise HTTPException(
                    status_code=501,
                    detail={"message": "Camera settings API is unavailable."},
                )
            result = callback(settings)
            _raise_for_rejected(result)
            return result

        @app.get("/api/v1/camera/frame")
        def camera_frame(
            space: str = "raw",
            after_counter: int | None = None,
            timeout_ms: int = 2000,
            auth_headers: tuple[str | None, str | None] = Depends(api_auth_headers),
        ) -> Response:
            authorize_request(API_PERMISSION_CAMERA_READ, auth_headers)
            normalized_space = str(space or "").strip().lower()
            if normalized_space not in {"raw", "corrected"}:
                raise HTTPException(
                    status_code=400,
                    detail={"message": f"Unsupported camera frame space: {space!r}."},
                )
            if after_counter is not None and after_counter < 0:
                raise HTTPException(
                    status_code=400,
                    detail={"message": "after_counter must be non-negative."},
                )
            if timeout_ms < 0 or timeout_ms > _MAX_CAMERA_FRAME_TIMEOUT_MS:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": (
                            "timeout_ms must be between 0 and "
                            f"{_MAX_CAMERA_FRAME_TIMEOUT_MS}."
                        )
                    },
                )
            callback = self._camera_frame_callback
            if callback is None:
                raise HTTPException(
                    status_code=501,
                    detail={"message": "Camera frame API is unavailable."},
                )
            result = callback(
                normalized_space,
                after_counter,
                float(timeout_ms) / 1000.0,
            )
            _raise_for_rejected(result)
            data = result.get("data")
            if not isinstance(data, (bytes, bytearray)):
                raise HTTPException(
                    status_code=500,
                    detail={"message": "Camera frame data is invalid."},
                )
            return Response(
                content=bytes(data),
                media_type=str(result.get("content_type") or "image/png"),
                headers={
                    "X-Camera-Frame-Counter": str(result.get("counter", 0)),
                    "X-Camera-Frame-Space": str(
                        result.get("space") or normalized_space
                    ),
                    "X-Camera-Frame-Width": str(result.get("width", 0)),
                    "X-Camera-Frame-Height": str(result.get("height", 0)),
                },
            )

        @app.get("/api/v1/camera/exposure-policy")
        def camera_exposure_policy(
            auth_headers: tuple[str | None, str | None] = Depends(api_auth_headers),
        ) -> dict[str, Any]:
            authorize_request(API_PERMISSION_CAMERA_READ, auth_headers)
            callback = self._camera_exposure_policy_snapshot_callback
            if callback is None:
                raise HTTPException(
                    status_code=501,
                    detail={"message": "Camera exposure policy API is unavailable."},
                )
            return exposure_policy_result(callback)

        @app.put("/api/v1/camera/exposure-policy")
        def update_camera_exposure_policy(
            payload: dict[str, Any] = Body(...),
            auth_headers: tuple[str | None, str | None] = Depends(api_auth_headers),
        ) -> dict[str, Any]:
            authorize_request(API_PERMISSION_CAMERA_WRITE, auth_headers)
            unknown = sorted(set(payload) - {"auto_enabled", "engine"})
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail={"message": f"Unknown request setting: {unknown[0]}."},
                )
            auto_enabled = payload.get("auto_enabled")
            if not isinstance(auto_enabled, bool):
                raise HTTPException(
                    status_code=400,
                    detail={"message": "auto_enabled must be a boolean."},
                )
            engine = payload.get("engine")
            if not isinstance(engine, str) or engine not in {"software", "camera"}:
                raise HTTPException(
                    status_code=400,
                    detail={"message": "engine must be software or camera."},
                )
            callback = self._camera_exposure_policy_set_callback
            if callback is None:
                raise HTTPException(
                    status_code=501,
                    detail={"message": "Camera exposure policy API is unavailable."},
                )
            return exposure_policy_result(
                callback,
                auto_enabled=auto_enabled,
                engine=engine,
            )

        @app.post("/api/v1/camera/exposure-once")
        def camera_exposure_once(
            payload: dict[str, Any] | None = Body(default=None),
            auth_headers: tuple[str | None, str | None] = Depends(api_auth_headers),
        ) -> dict[str, Any]:
            authorize_request(API_PERMISSION_CAMERA_WRITE, auth_headers)
            if payload not in (None, {}):
                raise HTTPException(
                    status_code=400,
                    detail={"message": "exposure-once does not accept settings."},
                )
            callback = self._camera_exposure_once_callback
            if callback is None:
                raise HTTPException(
                    status_code=501,
                    detail={"message": "Camera exposure policy API is unavailable."},
                )
            return exposure_policy_result(callback)

        add_body_command_route(
            "/api/v1/stage/focus/local",
            name="local_stage_focus",
            permission=API_PERMISSION_ROUTE_MEASURE,
            action="stage_local_focus",
        )
        add_body_command_route(
            "/api/v1/calibration/lens-distortion",
            name="lens_distortion_calibration",
            permission=API_PERMISSION_STAGE_WRITE,
            action="lens_distortion_calibration",
        )
        add_body_command_route(
            "/api/v1/calibration/click-to-move",
            name="click_to_move_calibration",
            permission=API_PERMISSION_STAGE_WRITE,
            action="click_to_move_calibration",
        )
        @app.post(
            "/api/v1/camera/area-scan",
            name="microscope_area_scan",
            response_model=None,
        )
        def microscope_area_scan(
            payload: dict[str, Any] | None = Body(default=None),
            auth_headers: tuple[str | None, str | None] = Depends(api_auth_headers),
        ) -> JSONResponse:
            authorize_request(API_PERMISSION_STAGE_WRITE, auth_headers)
            body = dict(payload or {})
            if "auto_exposure" in body:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": (
                            "auto_exposure is no longer supported for area scans."
                        )
                    },
                )
            result = dispatch_command_result(
                "microscope_area_scan",
                body,
            )
            return JSONResponse(status_code=202, content=result)
        add_command_route(
            "/api/v1/route/contacts",
            method="GET",
            name="list_route_contacts",
            permission=API_PERMISSION_ROUTE_READ,
            action="list_contacts",
        )
        add_contact_command_route(
            "/api/v1/route/contacts/{contact_number}/move",
            name="move_to_route_contact",
            action="move_to_contact",
        )
        add_contact_command_route(
            "/api/v1/route/contacts/{contact_number}/needles",
            name="route_contact_needles",
            action="contact_needles",
        )
        add_contact_command_route(
            "/api/v1/route/contacts/{contact_number}/check",
            name="check_route_contact",
            action="check_contact",
        )
        add_contact_command_route(
            "/api/v1/route/contacts/{contact_number}/focus",
            name="focus_route_contact",
            action="route_contact_focus",
        )

        @app.get("/api/v1/route/contacts/{contact_number}/photo")
        def route_contact_photo(
            contact_number: int,
            auth_headers: tuple[str | None, str | None] = Depends(api_auth_headers),
        ) -> Response:
            result = command_result(
                API_PERMISSION_ROUTE_MEASURE,
                auth_headers,
                "route_contact_photo",
                contact_payload(contact_number),
            )
            filename = str(result.get("filename") or f"contact_{contact_number}.jpg")
            return binary_response(
                result,
                invalid_message="Contact photo data is invalid.",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "X-Contact-Number": str(contact_number),
                },
            )

        add_contact_command_route(
            "/api/v1/route/contacts/{contact_number}/seek",
            name="seek_route_contact",
            action="contact_seek",
        )

        add_command_route(
            "/api/v1/route/control",
            method="GET",
            name="api_route_control_status",
            permission=API_PERMISSION_ROUTE_MEASURE,
            action="api_route_control_status",
        )
        add_body_command_route(
            "/api/v1/route/control",
            name="api_route_control_action",
            permission=API_PERMISSION_ROUTE_MEASURE,
            action="api_route_control_action",
        )
        add_body_command_route(
            "/api/v1/meter/configure",
            name="configure_meter",
            permission=API_PERMISSION_ROUTE_MEASURE,
            action="configure_meter",
            required=True,
        )

        @app.post("/api/v1/measurements/raw-sweep")
        def raw_voltage_sweep(
            payload: dict[str, Any] = Body(...),
            auth_headers: tuple[str | None, str | None] = Depends(api_auth_headers),
        ) -> dict[str, Any]:
            authorize_request(API_PERMISSION_ROUTE_MEASURE, auth_headers)
            try:
                voltages = _voltage_sweep_from_payload(payload)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"message": str(exc)},
                ) from exc
            body = dict(payload or {})
            body["voltages_v"] = voltages
            return dispatch_command_result("raw_voltage_sweep", body)

        add_command_route(
            "/api/v1/visa/resources",
            method="GET",
            name="list_visa_resources",
            permission=API_PERMISSION_ROUTE_MEASURE,
            action="visa_list_resources",
        )
        add_visa_command_route(
            "/api/v1/visa/resources/{role}/write",
            name="visa_write",
            operation="write",
            required=True,
        )
        add_visa_command_route(
            "/api/v1/visa/resources/{role}/query",
            name="visa_query",
            operation="query",
            required=True,
        )
        add_visa_command_route(
            "/api/v1/visa/resources/{role}/read",
            name="visa_read",
            operation="read",
        )
        add_visa_command_route(
            "/api/v1/visa/resources/{role}/read-raw",
            name="visa_read_raw",
            operation="read_raw",
            raw_response=True,
        )
        add_visa_command_route(
            "/api/v1/visa/resources/{role}/clear",
            name="visa_clear",
            operation="clear",
        )

        add_body_command_route(
            "/api/v1/route/sessions",
            name="start_route_session",
            permission=API_PERMISSION_ROUTE_MEASURE,
            action="start_route_session",
        )
        add_command_route(
            "/api/v1/route/sessions/current",
            method="GET",
            name="route_session_status",
            permission=API_PERMISSION_ROUTE_MEASURE,
            action="route_session_status",
        )
        add_body_command_route(
            "/api/v1/route/sessions/current/actions",
            name="route_session_action",
            permission=API_PERMISSION_ROUTE_MEASURE,
            action="route_session_action",
            required=True,
        )
        add_body_command_route(
            "/api/v1/route/sessions/current/result",
            name="route_session_result",
            permission=API_PERMISSION_ROUTE_MEASURE,
            action="route_session_result",
            required=True,
        )
        add_body_command_route(
            "/api/v1/route/sessions/current/seek",
            name="route_session_seek",
            permission=API_PERMISSION_ROUTE_MEASURE,
            action="route_session_seek",
        )

        @app.get("/api/v1/route/sessions/current/artifacts/{artifact_id}")
        def route_session_artifact(
            artifact_id: str,
            auth_headers: tuple[str | None, str | None] = Depends(api_auth_headers),
        ) -> Response:
            result = command_result(
                API_PERMISSION_ROUTE_MEASURE,
                auth_headers,
                "route_session_artifact",
                {"artifact_id": artifact_id},
            )
            filename = str(result.get("filename") or f"{artifact_id}.bin")
            return binary_response(
                result,
                invalid_message="Route artifact data is invalid.",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "X-Artifact-Id": str(result.get("artifact_id") or artifact_id),
                },
            )

        app.add_api_route(
            "/move",
            move_stage,
            methods=["POST"],
            include_in_schema=False,
        )
        return app, uvicorn

    def _authorize(
        self,
        permission: str,
        authorization: str | None,
        x_api_key: str | None,
    ) -> dict[str, Any]:
        if self._auth_callback is None:
            return {"accepted": True}
        from fastapi import HTTPException

        api_key = _extract_api_key(authorization, x_api_key)
        result = self._auth_callback(api_key, permission)
        if isinstance(result, dict) and result.get("accepted", False):
            return result
        if not isinstance(result, dict):
            result = {
                "accepted": False,
                "status_code": 401,
                "message": "API key rejected.",
            }
        status_code = int(result.get("status_code", 401))
        raise HTTPException(
            status_code=status_code,
            detail={
                "message": result.get("message", "API key rejected."),
                "result": result,
            },
        )

    def _call_command(self, request: dict[str, Any]) -> dict[str, Any]:
        if self._command_callback is None:
            return {
                "accepted": False,
                "status_code": 501,
                "message": "This API command is not available in this GUI build.",
            }
        return self._command_callback(request)
