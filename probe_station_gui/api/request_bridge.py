"""Qt bridge for routing API thread requests onto the GUI thread."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, Signal


logger = logging.getLogger(__name__)


class ApiRequestBridge(QObject):
    """Route API thread requests onto the Qt GUI thread."""

    request_received: Signal = Signal(object)

    def __init__(
        self,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._handler = handler
        self._pending_lock = threading.Lock()
        self._pending: dict[int, dict[str, Any]] = {}
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
            "state": "queued",
        }
        envelope_id = id(envelope)
        with self._pending_lock:
            if self._closed:
                return self._shutdown_response()
            self._pending[envelope_id] = envelope
        self.request_received.emit(envelope)
        if not event.wait(timeout_s):
            timeout_response = {
                "accepted": False,
                "status_code": 503,
                "message": "GUI did not process the API request in time.",
            }
            with self._pending_lock:
                pending = self._pending.pop(envelope_id, None)
                if pending is envelope:
                    envelope["state"] = "cancelled"
                    envelope["result"] = timeout_response
                    event.set()
            return timeout_response
        result = envelope.get("result")
        if isinstance(result, dict):
            return result
        return {
            "accepted": False,
            "status_code": 500,
            "message": "GUI returned an invalid API response.",
        }

    def close(self) -> None:
        """Reject queued API requests and wake API threads during shutdown."""

        with self._pending_lock:
            self._closed = True
            pending = tuple(self._pending.values())
            self._pending.clear()
            response = self._shutdown_response()
            for envelope in pending:
                envelope["state"] = "cancelled"
                envelope["result"] = dict(response)
                event = envelope.get("event")
                if isinstance(event, threading.Event):
                    event.set()

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
            if self._pending.get(envelope_id) is not envelope:
                if isinstance(event, threading.Event):
                    event.set()
                return
            envelope["state"] = "handling"
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
        finally:
            with self._pending_lock:
                pending = self._pending.pop(envelope_id, None)
                if pending is envelope and envelope.get("state") != "cancelled":
                    envelope["result"] = result
                    envelope["state"] = "completed"
            if isinstance(event, threading.Event):
                event.set()
