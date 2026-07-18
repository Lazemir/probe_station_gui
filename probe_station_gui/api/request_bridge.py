"""Qt bridge for routing API thread requests onto the GUI thread."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, Signal


logger = logging.getLogger(__name__)

_QUEUED = "queued"
_HANDLING = "handling"
_COMPLETED = "completed"
_CANCELLED = "cancelled"


class DeferredApiResponse:
    """A worker-owned API result whose completion must wake the bridge submitter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._result: dict[str, Any] | None = None
        self._callbacks: list[Callable[[dict[str, Any]], None]] = []

    def complete(self, result: dict[str, Any]) -> bool:
        callbacks: tuple[Callable[[dict[str, Any]], None], ...]
        response = dict(result)
        with self._lock:
            if self._result is not None:
                return False
            self._result = response
            callbacks = tuple(self._callbacks)
            self._callbacks.clear()
            self._event.set()
        for callback in callbacks:
            callback(dict(response))
        return True

    def add_done_callback(
        self,
        callback: Callable[[dict[str, Any]], None],
    ) -> None:
        result: dict[str, Any] | None = None
        with self._lock:
            if self._result is None:
                self._callbacks.append(callback)
            else:
                result = dict(self._result)
        if result is not None:
            callback(result)

    def wait(self, *, timeout_s: float | None = None) -> dict[str, Any] | None:
        if not self._event.wait(timeout_s):
            return None
        with self._lock:
            return None if self._result is None else dict(self._result)


class ApiRequestBridge(QObject):
    """Route API thread requests onto the Qt GUI thread."""

    request_received: Signal = Signal(object)

    def __init__(
        self,
        handler: Callable[
            [dict[str, Any]],
            dict[str, Any] | DeferredApiResponse,
        ],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._handler = handler
        self._pending_lock = threading.Lock()
        self._pending_changed = threading.Condition(self._pending_lock)
        self._pending: dict[int, dict[str, Any]] = {}
        self._accepting = True
        self._closed = False
        self.request_received.connect(
            self._handle_request,
            Qt.ConnectionType.QueuedConnection,
        )

    def submit(
        self,
        request: dict[str, Any],
        *,
        timeout_s: float = 5.0,
    ) -> dict[str, Any]:
        event = threading.Event()
        envelope: dict[str, Any] = {
            "request": dict(request),
            "result": None,
            "event": event,
            "state": _QUEUED,
        }
        envelope_id = id(envelope)
        with self._pending_lock:
            if self._closed or not self._accepting:
                return self._shutdown_response()
            self._pending[envelope_id] = envelope
        self.request_received.emit(envelope)
        if not event.wait(timeout_s):
            timeout_response = {
                "accepted": False,
                "status_code": 503,
                "message": "GUI did not process the API request in time.",
            }
            wait_for_completion = False
            with self._pending_lock:
                pending = self._pending.get(envelope_id)
                state = envelope.get("state")
                if pending is envelope and state == _QUEUED:
                    self._pending.pop(envelope_id, None)
                    envelope["state"] = _CANCELLED
                    envelope["result"] = timeout_response
                    event.set()
                    self._pending_changed.notify_all()
                elif pending is envelope and state == _HANDLING:
                    wait_for_completion = True
            if wait_for_completion:
                event.wait()
        result = envelope.get("result")
        if isinstance(result, dict):
            return result
        return {
            "accepted": False,
            "status_code": 500,
            "message": "GUI returned an invalid API response.",
        }

    def stop_accepting(self) -> None:
        """Reject new and queued requests while preserving handling requests."""

        with self._pending_lock:
            self._accepting = False
            response = self._shutdown_response()
            queued = tuple(
                (envelope_id, envelope)
                for envelope_id, envelope in self._pending.items()
                if envelope.get("state") == _QUEUED
            )
            for envelope_id, envelope in queued:
                self._pending.pop(envelope_id, None)
                envelope["state"] = _CANCELLED
                envelope["result"] = dict(response)
                event = envelope.get("event")
                if isinstance(event, threading.Event):
                    event.set()
            self._pending_changed.notify_all()

    def wait_for_inflight(self, *, timeout_s: float) -> bool:
        """Wait for every handling request to publish its definitive response."""

        timeout = max(0.0, float(timeout_s))
        with self._pending_changed:
            return bool(
                self._pending_changed.wait_for(
                    lambda: not self._pending,
                    timeout=timeout,
                )
            )

    def close(self) -> bool:
        """Close after intake is stopped and every handling request has drained."""

        self.stop_accepting()
        with self._pending_lock:
            if self._pending:
                return False
            self._closed = True
            self._pending_changed.notify_all()
            return True

    @staticmethod
    def _shutdown_response() -> dict[str, Any]:
        return {
            "accepted": False,
            "status_code": 503,
            "message": "GUI API bridge is shutting down.",
        }

    def _handle_request(self, envelope: object) -> None:
        if not isinstance(envelope, dict):
            return
        event = envelope.get("event")
        envelope_id = id(envelope)
        with self._pending_lock:
            if (
                self._pending.get(envelope_id) is not envelope
                or envelope.get("state") != _QUEUED
            ):
                if isinstance(event, threading.Event):
                    event.set()
                return
            envelope["state"] = _HANDLING
        result: dict[str, Any] = {
            "accepted": False,
            "status_code": 500,
            "message": "GUI request processing was interrupted.",
        }
        try:
            request = envelope.get("request")
            if not isinstance(request, dict):
                raise ValueError("Invalid API request envelope.")
            result = self._handler(request)
        except Exception as exc:
            logger.exception("Failed to handle API request.")
            result = {
                "accepted": False,
                "status_code": 500,
                "message": str(exc),
            }
        if isinstance(result, DeferredApiResponse):
            result.add_done_callback(
                lambda response: self._complete_request(envelope, response)
            )
            return
        self._complete_request(envelope, result)

    def _complete_request(
        self,
        envelope: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        event = envelope.get("event")
        envelope_id = id(envelope)
        completed = False
        with self._pending_lock:
            if (
                self._pending.get(envelope_id) is envelope
                and envelope.get("state") == _HANDLING
            ):
                self._pending.pop(envelope_id, None)
                envelope["result"] = dict(result)
                envelope["state"] = _COMPLETED
                completed = True
                self._pending_changed.notify_all()
        if completed and isinstance(event, threading.Event):
            event.set()


__all__ = ["ApiRequestBridge", "DeferredApiResponse"]
