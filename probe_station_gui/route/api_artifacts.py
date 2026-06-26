from __future__ import annotations

import threading
import uuid
from typing import Any, Callable

from probe_station_gui.route.measurement_payloads import (
    route_artifact_public_payload,
    route_artifact_record,
    route_external_result_payload,
)
from probe_station_gui.route.session_actions import route_session_action_from_payload


class ApiRouteArtifactsStore:
    def __init__(
        self,
        *,
        artifacts: dict[str, dict[str, object]] | None = None,
        lock: threading.Lock | None = None,
        create_artifact_id: Callable[[], str] | None = None,
    ) -> None:
        self._artifacts = artifacts if artifacts is not None else {}
        self._lock = lock if lock is not None else threading.Lock()
        self._create_artifact_id = (
            create_artifact_id if create_artifact_id is not None else lambda: uuid.uuid4().hex
        )

    def clear(self) -> None:
        with self._lock:
            self._artifacts.clear()

    def add(
        self,
        *,
        data: bytes,
        filename: str,
        content_type: str,
        kind: str,
        metadata: dict[str, object],
        created_at_utc: str,
    ) -> str:
        artifact_id = self._create_artifact_id()
        artifact = route_artifact_record(
            artifact_id=artifact_id,
            data=data,
            filename=filename,
            content_type=content_type,
            kind=kind,
            metadata=metadata,
            created_at_utc=created_at_utc,
        )
        with self._lock:
            self._artifacts[artifact_id] = artifact
        return artifact_id

    def public_payloads(self) -> list[dict[str, object]]:
        with self._lock:
            return [
                route_artifact_public_payload(artifact)
                for artifact in self._artifacts.values()
            ]

    def artifact_response(self, payload: dict[str, object]) -> dict[str, object]:
        artifact_id = str(payload.get("artifact_id", "")).strip()
        with self._lock:
            artifact = dict(self._artifacts.get(artifact_id) or {})
        if not artifact:
            return {
                "accepted": False,
                "status_code": 404,
                "message": "Route session artifact was not found.",
            }
        return {
            "accepted": True,
            "artifact_id": artifact_id,
            **artifact,
        }


def api_route_session_status_response(
    runner: object | None,
    last_status: dict[str, object] | None,
    artifacts_payload: list[dict[str, object]],
) -> dict[str, object]:
    if runner is not None and hasattr(runner, "status_payload"):
        status = runner.status_payload()
    elif last_status is not None:
        status = dict(last_status)
    else:
        return {
            "accepted": False,
            "status_code": 404,
            "message": "No API route session is active.",
        }
    status["artifacts"] = list(artifacts_payload)
    return status


def api_route_session_result_response(
    payload: dict[str, Any],
    *,
    runner: object | None,
    timestamp_utc: str,
) -> dict[str, object]:
    if runner is None or not hasattr(runner, "submit_external_result"):
        return {
            "accepted": False,
            "status_code": 404,
            "message": "No external route session is waiting for a result.",
        }
    result = route_external_result_payload(payload, timestamp_utc=timestamp_utc)
    if not runner.submit_external_result(result):
        return {
            "accepted": False,
            "status_code": 409,
            "message": "Route session is not waiting for an external result.",
        }
    return {
        "accepted": True,
        "message": "External result submitted.",
        "result": result,
    }


def api_route_session_seek_response(runner: object | None) -> dict[str, object]:
    if runner is None or not hasattr(runner, "request_contact_seek"):
        return {
            "accepted": False,
            "status_code": 404,
            "message": "No route session is active.",
        }
    if not runner.request_contact_seek():
        return {
            "accepted": False,
            "status_code": 409,
            "message": "Route session is not waiting for contact seek.",
        }
    return {
        "accepted": True,
        "message": "Contact seek requested for current route contact.",
    }


def api_route_session_action_response(
    payload: dict[str, Any],
    *,
    runner: object | None,
    interrupt_runner: Callable[..., None],
) -> dict[str, object]:
    if runner is None:
        return {
            "accepted": False,
            "status_code": 404,
            "message": "No route session is active.",
        }
    action = route_session_action_from_payload(payload)
    if action.kind == "pause":
        runner.request_pause_after_current_point()
        return {
            "accepted": True,
            "message": "Route session pause requested.",
            "action": "pause",
        }
    if action.kind == "interrupt":
        interrupt_runner(runner, reason="Route API session interrupt requested.")
        return {
            "accepted": True,
            "message": "Route session interrupt requested.",
            "action": "interrupt",
        }
    if action.kind == "stop":
        runner.stop()
        return {
            "accepted": True,
            "message": "Route session stop requested.",
            "action": "stop",
        }
    if not runner.submit_confirmation(action.action):
        return {
            "accepted": False,
            "status_code": 400,
            "message": "Unknown route session action.",
        }
    return {
        "accepted": True,
        "message": f"Route session action submitted: {action.action}.",
        "action": action.action,
    }


def final_api_route_session_status(
    session_id: str | None,
    runner: object | None,
    *,
    logger: object,
) -> dict[str, object] | None:
    if not session_id or runner is None or not hasattr(runner, "status_payload"):
        return None
    try:
        return runner.status_payload()
    except Exception:
        logger.exception("Failed to store final API route session status.")
        return None


def api_route_photo_artifact_metadata(
    point: object,
    *,
    position: int,
    total: int,
    contact_number: int,
    focus_result: object | None,
) -> dict[str, object]:
    return {
        "position": int(position),
        "total": int(total),
        "point_index": int(getattr(point, "index")),
        "contact_number": int(contact_number),
        "label": str(getattr(point, "label")),
        "focus": focus_result,
    }


def api_route_photo_content_type(photo_name: str) -> str:
    if str(photo_name).lower().endswith(".jpg"):
        return "image/jpeg"
    return "image/png"


__all__ = [
    "ApiRouteArtifactsStore",
    "api_route_photo_artifact_metadata",
    "api_route_photo_content_type",
    "api_route_session_action_response",
    "api_route_session_result_response",
    "api_route_session_seek_response",
    "api_route_session_status_response",
    "final_api_route_session_status",
]
