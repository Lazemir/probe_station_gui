"""Route-control and external-measurement clients."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from .client import ProbeStationClient


class ProbeStationApiRouteControlClient:
    """API route control exposed through the GUI."""

    def __init__(self, client: ProbeStationClient) -> None:
        self._client = client

    def status(self) -> dict[str, Any]:
        return self._client._request("GET", "/api/v1/route/control")

    def action(self, action: str, **payload: Any) -> dict[str, Any]:
        body = dict(payload)
        body["action"] = str(action)
        return self._client._request("POST", "/api/v1/route/control", body)

    def start(self, *, label: str = "API route control") -> dict[str, Any]:
        return self.action("start", label=label)

    def pause(self) -> dict[str, Any]:
        return self.action("pause")

    def pause_ack(self) -> dict[str, Any]:
        return self.action("pause_ack")

    def interrupt(self) -> dict[str, Any]:
        return self.action("interrupt")

    def resume(self) -> dict[str, Any]:
        return self.action("resume")

    def skip(self) -> dict[str, Any]:
        return self.action("skip")

    def remeasure(self) -> dict[str, Any]:
        return self.action("remeasure")

    def measure(self) -> dict[str, Any]:
        return self.action("measure")

    def ack(self) -> dict[str, Any]:
        return self.action("ack")

    def stop(self) -> dict[str, Any]:
        return self.action("stop")

    def finish(self, *, label: str = "API route control") -> dict[str, Any]:
        return self.action("finish", label=label)


class RouteReadyContact:
    """One route contact currently waiting for an API-owned measurement."""

    def __init__(
        self,
        session: ProbeStationRouteSession,
        status: Mapping[str, Any],
    ) -> None:
        self.session = session
        self.status = dict(status)
        current = self.status.get("current_contact")
        self.contact = dict(current) if isinstance(current, Mapping) else {}
        preparation = self.status.get("last_preparation")
        self.preparation = dict(preparation) if isinstance(preparation, Mapping) else {}

    @property
    def contact_number(self) -> int | None:
        value = self.contact.get("contact_number")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @property
    def position(self) -> int | None:
        value = self.status.get("position")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @property
    def external_measurement_request_id(self) -> int | None:
        value = self.status.get("external_measurement_request_id")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def submit_result(
        self,
        *,
        status: str = "ok",
        summary: Mapping[str, Any] | None = None,
        files: list[Mapping[str, Any]] | None = None,
        message: str = "",
    ) -> dict[str, Any]:
        return self.session.submit_result(
            status=status,
            summary=summary,
            files=files,
            message=message,
            external_measurement_request_id=self.external_measurement_request_id,
        )

    def download_artifact(self, artifact_id: str) -> bytes:
        return self.session.download_artifact(artifact_id)


class ProbeStationRouteSession:
    """Client-side handle for the active GUI-owned route session."""

    def __init__(self, route: ProbeStationRouteClient, status: Mapping[str, Any]) -> None:
        self.route = route
        self.client = route._client
        self.initial_status = dict(status)
        self.session_id = str(status.get("session_id") or "")

    def status(self) -> dict[str, Any]:
        return self.route.status()

    def action(self, action: str, **payload: Any) -> dict[str, Any]:
        return self.route.action(action, **payload)

    def pause(self) -> dict[str, Any]:
        return self.action("pause")

    def resume(self) -> dict[str, Any]:
        return self.action("resume")

    def interrupt(self) -> dict[str, Any]:
        return self.action("interrupt")

    def stop(self) -> dict[str, Any]:
        return self.action("stop")

    def skip(self) -> dict[str, Any]:
        return self.action("skip")

    def remeasure(self) -> dict[str, Any]:
        return self.action("remeasure")

    def seek_current(self) -> dict[str, Any]:
        return self.route.seek_current()

    def submit_result(
        self,
        *,
        status: str = "ok",
        summary: Mapping[str, Any] | None = None,
        files: list[Mapping[str, Any]] | None = None,
        message: str = "",
        external_measurement_request_id: int | None = None,
    ) -> dict[str, Any]:
        return self.route.submit_result(
            status=status,
            summary=summary,
            files=files,
            message=message,
            external_measurement_request_id=external_measurement_request_id,
        )

    def download_artifact(self, artifact_id: str) -> bytes:
        return self.route.download_artifact(artifact_id)

    def iter_ready(
        self,
        *,
        poll_interval_s: float = 0.5,
        timeout_s: float | None = None,
        raise_on_stop: bool = True,
    ):
        """Yield contacts as they become ready for external measurement."""

        started = time.monotonic()
        yielded: set[tuple[str, int, int]] = set()
        while True:
            status = self.status()
            state = str(status.get("state") or "")
            if state == "complete":
                return
            if state in {"stopped", "failed"}:
                if raise_on_stop:
                    message = str(
                        status.get("message")
                        or "Route session stopped before completion."
                    )
                    raise self.route._stopped_error(message)
                return
            reason = str(status.get("waiting_reason") or "")
            if reason in {"external_measurement", "external_measurement_failed"}:
                session_id = str(status.get("session_id") or self.session_id)
                position = int(status.get("position") or 0)
                try:
                    request_id = int(
                        status.get("external_measurement_request_id") or 0
                    )
                except (TypeError, ValueError):
                    request_id = 0
                key = (session_id, position, request_id)
                if key not in yielded:
                    yielded.add(key)
                    yield RouteReadyContact(self, status)
            if timeout_s is not None and time.monotonic() - started >= timeout_s:
                raise TimeoutError("Route session did not finish before timeout.")
            time.sleep(max(0.0, float(poll_interval_s)))


class ProbeStationRouteClient:
    """Route workflow namespace for API-owned measurements."""

    def __init__(
        self,
        client: ProbeStationClient,
    ) -> None:
        self._client = client
        self._stopped_error = client._route_stopped_error

    def start_external(self, **options: Any) -> ProbeStationRouteSession:
        status = self._client._request(
            "POST",
            "/api/v1/route/sessions",
            dict(options),
        )
        return ProbeStationRouteSession(self, status)

    def status(self) -> dict[str, Any]:
        return self._client._request("GET", "/api/v1/route/sessions/current")

    def action(self, action: str, **payload: Any) -> dict[str, Any]:
        body = dict(payload)
        body["action"] = str(action)
        return self._client._request(
            "POST",
            "/api/v1/route/sessions/current/actions",
            body,
        )

    def pause(self) -> dict[str, Any]:
        return self.action("pause")

    def resume(self) -> dict[str, Any]:
        return self.action("resume")

    def interrupt(self) -> dict[str, Any]:
        return self.action("interrupt")

    def stop(self) -> dict[str, Any]:
        return self.action("stop")

    def skip(self) -> dict[str, Any]:
        return self.action("skip")

    def remeasure(self) -> dict[str, Any]:
        return self.action("remeasure")

    def seek_current(self) -> dict[str, Any]:
        return self._client._request(
            "POST",
            "/api/v1/route/sessions/current/seek",
            {},
        )

    def submit_result(
        self,
        *,
        status: str = "ok",
        summary: Mapping[str, Any] | None = None,
        files: list[Mapping[str, Any]] | None = None,
        message: str = "",
        external_measurement_request_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": status,
            "summary": dict(summary or {}),
            "files": [dict(item) for item in files or []],
            "message": message,
        }
        if external_measurement_request_id is not None:
            payload["external_measurement_request_id"] = int(
                external_measurement_request_id
            )
        return self._client._request(
            "POST",
            "/api/v1/route/sessions/current/result",
            payload,
        )

    def download_artifact(self, artifact_id: str) -> bytes:
        return self._client._request_bytes(
            "GET",
            f"/api/v1/route/sessions/current/artifacts/{artifact_id}",
        )
