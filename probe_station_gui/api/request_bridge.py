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
        }
        self.request_received.emit(envelope)
        if not event.wait(timeout_s):
            return {
                "accepted": False,
                "status_code": 503,
                "message": "GUI did not process the API request in time.",
            }
        result = envelope.get("result")
        if isinstance(result, dict):
            return result
        return {
            "accepted": False,
            "status_code": 500,
            "message": "GUI returned an invalid API response.",
        }

    def _handle_request(self, envelope: object) -> None:
        if not isinstance(envelope, dict):
            return
        event = envelope.get("event")
        try:
            request = envelope.get("request")
            if not isinstance(request, dict):
                raise ValueError("Invalid API request envelope.")
            envelope["result"] = self._handler(request)
        except Exception as exc:
            logger.exception("Failed to handle API request.")
            envelope["result"] = {
                "accepted": False,
                "status_code": 500,
                "message": str(exc),
            }
        finally:
            if isinstance(event, threading.Event):
                event.set()
