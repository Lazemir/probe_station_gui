"""FastAPI control surface for the probe station GUI."""

from __future__ import annotations

import logging
import os
import threading
import math
from typing import Any, Callable

from probe_station_gui.api.keys import (
    API_PERMISSION_ROUTE_MEASURE,
    API_PERMISSION_ROUTE_READ,
    API_PERMISSION_STAGE_READ,
    API_PERMISSION_STAGE_WRITE,
)


logger = logging.getLogger(__name__)

_AXIS_NAMES = ("X", "Y", "Z", "A", "B", "C")
_MAX_SWEEP_POINTS = 1000


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
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        self.host = host or os.environ.get("PROBE_STATION_API_HOST", "127.0.0.1")
        self.port = int(port) if port is not None else self._port_from_environment()
        self._move_callback = move_callback
        self._status_callback = status_callback
        self._command_callback = command_callback
        self._auth_callback = auth_callback
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
        from fastapi import Body, FastAPI, Header, HTTPException
        from fastapi.responses import HTMLResponse, Response
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

        @app.get("/api/v1/stage/status")
        def stage_status(
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_STAGE_READ, authorization, x_api_key)
            return self._status_callback()

        @app.post("/api/v1/stage/move")
        def move_stage(
            payload: dict[str, Any] = Body(...),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_STAGE_WRITE, authorization, x_api_key)
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

        @app.post("/api/v1/stage/focus/local")
        def local_stage_focus(
            payload: dict[str, Any] | None = Body(default=None),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            result = self._call_command(
                {
                    "action": "stage_local_focus",
                    "payload": dict(payload or {}),
                }
            )
            _raise_for_rejected(result)
            return result

        @app.get("/api/v1/route/contacts")
        def list_route_contacts(
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_READ, authorization, x_api_key)
            result = self._call_command({"action": "list_contacts"})
            _raise_for_rejected(result)
            return result

        @app.post("/api/v1/route/contacts/{contact_number}/move")
        def move_to_route_contact(
            contact_number: int,
            payload: dict[str, Any] | None = Body(default=None),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            body = dict(payload or {})
            body["contact_number"] = int(contact_number)
            result = self._call_command(
                {
                    "action": "move_to_contact",
                    "payload": body,
                }
            )
            _raise_for_rejected(result)
            return result

        @app.post("/api/v1/route/contacts/{contact_number}/needles")
        def route_contact_needles(
            contact_number: int,
            payload: dict[str, Any] | None = Body(default=None),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            body = dict(payload or {})
            body["contact_number"] = int(contact_number)
            result = self._call_command(
                {
                    "action": "contact_needles",
                    "payload": body,
                }
            )
            _raise_for_rejected(result)
            return result

        @app.post("/api/v1/route/contacts/{contact_number}/check")
        def check_route_contact(
            contact_number: int,
            payload: dict[str, Any] | None = Body(default=None),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            body = dict(payload or {})
            body["contact_number"] = int(contact_number)
            result = self._call_command(
                {
                    "action": "check_contact",
                    "payload": body,
                }
            )
            _raise_for_rejected(result)
            return result

        @app.post("/api/v1/route/contacts/{contact_number}/focus")
        def focus_route_contact(
            contact_number: int,
            payload: dict[str, Any] | None = Body(default=None),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            body = dict(payload or {})
            body["contact_number"] = int(contact_number)
            result = self._call_command(
                {
                    "action": "route_contact_focus",
                    "payload": body,
                }
            )
            _raise_for_rejected(result)
            return result

        @app.get("/api/v1/route/contacts/{contact_number}/photo")
        def route_contact_photo(
            contact_number: int,
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> Response:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            result = self._call_command(
                {
                    "action": "route_contact_photo",
                    "payload": {"contact_number": int(contact_number)},
                }
            )
            _raise_for_rejected(result)
            data = result.get("data", b"")
            if not isinstance(data, (bytes, bytearray)):
                raise HTTPException(
                    status_code=500,
                    detail={"message": "Contact photo data is invalid."},
                )
            content_type = str(result.get("content_type") or "application/octet-stream")
            filename = str(result.get("filename") or f"contact_{contact_number}.jpg")
            headers = {
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Contact-Number": str(contact_number),
            }
            return Response(
                content=bytes(data),
                media_type=content_type,
                headers=headers,
            )

        @app.post("/api/v1/route/contacts/{contact_number}/seek")
        def seek_route_contact(
            contact_number: int,
            payload: dict[str, Any] | None = Body(default=None),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            body = dict(payload or {})
            body["contact_number"] = int(contact_number)
            result = self._call_command(
                {
                    "action": "contact_seek",
                    "payload": body,
                }
            )
            _raise_for_rejected(result)
            return result

        @app.get("/api/v1/route/control")
        def api_route_control_status(
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            result = self._call_command({"action": "api_route_control_status"})
            _raise_for_rejected(result)
            return result

        @app.post("/api/v1/route/control")
        def api_route_control_action(
            payload: dict[str, Any] | None = Body(default=None),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            result = self._call_command(
                {
                    "action": "api_route_control_action",
                    "payload": dict(payload or {}),
                }
            )
            _raise_for_rejected(result)
            return result

        @app.post("/api/v1/meter/configure")
        def configure_meter(
            payload: dict[str, Any] = Body(...),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            result = self._call_command(
                {
                    "action": "configure_meter",
                    "payload": dict(payload or {}),
                }
            )
            _raise_for_rejected(result)
            return result

        @app.post("/api/v1/measurements/raw-sweep")
        def raw_voltage_sweep(
            payload: dict[str, Any] = Body(...),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            try:
                voltages = _voltage_sweep_from_payload(payload)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"message": str(exc)},
                ) from exc
            body = dict(payload or {})
            body["voltages_v"] = voltages
            result = self._call_command(
                {
                    "action": "raw_voltage_sweep",
                    "payload": body,
                }
            )
            _raise_for_rejected(result)
            return result

        @app.get("/api/v1/visa/resources")
        def list_visa_resources(
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            result = self._call_command({"action": "visa_list_resources"})
            _raise_for_rejected(result)
            return result

        @app.post("/api/v1/visa/resources/{role}/write")
        def visa_write(
            role: str,
            payload: dict[str, Any] = Body(...),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            result = self._call_command(
                {
                    "action": "visa_operation",
                    "payload": {
                        **dict(payload or {}),
                        "role": role,
                        "operation": "write",
                    },
                }
            )
            _raise_for_rejected(result)
            return result

        @app.post("/api/v1/visa/resources/{role}/query")
        def visa_query(
            role: str,
            payload: dict[str, Any] = Body(...),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            result = self._call_command(
                {
                    "action": "visa_operation",
                    "payload": {
                        **dict(payload or {}),
                        "role": role,
                        "operation": "query",
                    },
                }
            )
            _raise_for_rejected(result)
            return result

        @app.post("/api/v1/visa/resources/{role}/read")
        def visa_read(
            role: str,
            payload: dict[str, Any] | None = Body(default=None),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            result = self._call_command(
                {
                    "action": "visa_operation",
                    "payload": {
                        **dict(payload or {}),
                        "role": role,
                        "operation": "read",
                    },
                }
            )
            _raise_for_rejected(result)
            return result

        @app.post("/api/v1/visa/resources/{role}/read-raw")
        def visa_read_raw(
            role: str,
            payload: dict[str, Any] | None = Body(default=None),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> Response:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            result = self._call_command(
                {
                    "action": "visa_operation",
                    "payload": {
                        **dict(payload or {}),
                        "role": role,
                        "operation": "read_raw",
                    },
                }
            )
            _raise_for_rejected(result)
            data = result.get("data", b"")
            if not isinstance(data, (bytes, bytearray)):
                raise HTTPException(
                    status_code=500,
                    detail={"message": "VISA raw response is invalid."},
                )
            return Response(content=bytes(data), media_type="application/octet-stream")

        @app.post("/api/v1/visa/resources/{role}/clear")
        def visa_clear(
            role: str,
            payload: dict[str, Any] | None = Body(default=None),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            result = self._call_command(
                {
                    "action": "visa_operation",
                    "payload": {
                        **dict(payload or {}),
                        "role": role,
                        "operation": "clear",
                    },
                }
            )
            _raise_for_rejected(result)
            return result

        @app.post("/api/v1/route/sessions")
        def start_route_session(
            payload: dict[str, Any] | None = Body(default=None),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            result = self._call_command(
                {
                    "action": "start_route_session",
                    "payload": dict(payload or {}),
                }
            )
            _raise_for_rejected(result)
            return result

        @app.get("/api/v1/route/sessions/current")
        def route_session_status(
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            result = self._call_command({"action": "route_session_status"})
            _raise_for_rejected(result)
            return result

        @app.post("/api/v1/route/sessions/current/actions")
        def route_session_action(
            payload: dict[str, Any] = Body(...),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            result = self._call_command(
                {
                    "action": "route_session_action",
                    "payload": dict(payload or {}),
                }
            )
            _raise_for_rejected(result)
            return result

        @app.post("/api/v1/route/sessions/current/result")
        def route_session_result(
            payload: dict[str, Any] = Body(...),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            result = self._call_command(
                {
                    "action": "route_session_result",
                    "payload": dict(payload or {}),
                }
            )
            _raise_for_rejected(result)
            return result

        @app.post("/api/v1/route/sessions/current/seek")
        def route_session_seek(
            payload: dict[str, Any] | None = Body(default=None),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> dict[str, Any]:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            result = self._call_command(
                {
                    "action": "route_session_seek",
                    "payload": dict(payload or {}),
                }
            )
            _raise_for_rejected(result)
            return result

        @app.get("/api/v1/route/sessions/current/artifacts/{artifact_id}")
        def route_session_artifact(
            artifact_id: str,
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> Response:
            self._authorize(API_PERMISSION_ROUTE_MEASURE, authorization, x_api_key)
            result = self._call_command(
                {
                    "action": "route_session_artifact",
                    "payload": {"artifact_id": artifact_id},
                }
            )
            _raise_for_rejected(result)
            data = result.get("data", b"")
            if not isinstance(data, (bytes, bytearray)):
                raise HTTPException(
                    status_code=500,
                    detail={"message": "Route artifact data is invalid."},
                )
            content_type = str(result.get("content_type") or "application/octet-stream")
            filename = str(result.get("filename") or f"{artifact_id}.bin")
            headers = {
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Artifact-Id": str(result.get("artifact_id") or artifact_id),
            }
            return Response(
                content=bytes(data),
                media_type=content_type,
                headers=headers,
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
