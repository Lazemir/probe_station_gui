"""FastAPI control surface for the probe station GUI."""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable


logger = logging.getLogger(__name__)

_AXIS_NAMES = ("X", "Y", "Z", "A", "B", "C")


def _model_dump(model: object) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)  # type: ignore[no-any-return]
    if hasattr(model, "dict"):
        return model.dict(exclude_none=True)  # type: ignore[no-any-return]
    return {}


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


class ProbeStationApiServer:
    """Run a FastAPI app in a background thread.

    The callbacks are expected to be thread-safe from the API server side. In
    the GUI they are routed through a queued Qt signal before touching widgets
    or the stage controller.
    """

    def __init__(
        self,
        *,
        move_callback: Callable[[dict[str, float]], dict[str, Any]],
        status_callback: Callable[[], dict[str, Any]],
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        self.host = host or os.environ.get("PROBE_STATION_API_HOST", "127.0.0.1")
        self.port = int(port) if port is not None else self._port_from_environment()
        self._move_callback = move_callback
        self._status_callback = status_callback
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
        from fastapi import FastAPI, HTTPException
        from pydantic import BaseModel, Field
        import uvicorn

        class MoveRequest(BaseModel):
            x: float | None = Field(default=None, description="Target X coordinate")
            y: float | None = Field(default=None, description="Target Y coordinate")
            z: float | None = Field(default=None, description="Target Z coordinate")
            a: float | None = Field(default=None, description="Target A coordinate")
            b: float | None = Field(default=None, description="Target B coordinate")
            c: float | None = Field(default=None, description="Target C coordinate")
            coordinates: dict[str, float] | None = Field(
                default=None,
                description="Alternative axis map, e.g. {'X': 1.0, 'Y': 2.0}",
            )

            class Config:
                extra = "allow"

        app = FastAPI(
            title="Probe Station Control API",
            version="0.1.0",
            docs_url="/docs",
            redoc_url="/redoc",
        )

        @app.get("/health")
        def health() -> dict[str, str]:
            return {"status": "ok"}

        @app.get("/api/v1/stage/status")
        def stage_status() -> dict[str, Any]:
            return self._status_callback()

        @app.post("/api/v1/stage/move")
        def move_stage(request: MoveRequest) -> dict[str, Any]:
            targets = _axis_targets_from_payload(_model_dump(request))
            if not targets:
                raise HTTPException(
                    status_code=400,
                    detail={"message": "Provide at least one target coordinate."},
                )
            result = self._move_callback(targets)
            if not result.get("accepted", False):
                status_code = int(result.get("status_code", 409))
                detail = {
                    "message": result.get("message", "Move request rejected."),
                    "result": result,
                }
                raise HTTPException(status_code=status_code, detail=detail)
            return result

        app.add_api_route(
            "/move",
            move_stage,
            methods=["POST"],
            include_in_schema=False,
        )
        return app, uvicorn
