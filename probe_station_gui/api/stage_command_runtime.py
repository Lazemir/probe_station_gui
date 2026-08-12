"Serialized background execution for API stage commands."

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable


if TYPE_CHECKING:
    from probe_station_gui.api.request_bridge import DeferredApiResponse


_DirectDispatch = Callable[[dict[str, Any]], dict[str, Any]]
_ThreadFactory = Callable[..., threading.Thread]


_logger = logging.getLogger(__name__)


@dataclass
class _Reservation:
    action: str
    completion: DeferredApiResponse


class ApiStageCommandRuntime:
    "Run stage commands outside the GUI thread behind a small lifecycle seam."

    def __init__(
        self,
        direct_dispatch: _DirectDispatch,
        *,
        thread_factory: _ThreadFactory = threading.Thread,
    ) -> None:
        self._direct_dispatch = direct_dispatch
        self._thread_factory = thread_factory
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._reservation: _Reservation | None = None

    def submit(
        self,
        command_request: dict[str, Any],
        action: str,
    ) -> dict[str, Any] | DeferredApiResponse:
        from probe_station_gui.api.request_bridge import DeferredApiResponse

        completion = DeferredApiResponse()
        reservation = _Reservation(str(action), completion)
        request = dict(command_request)
        with self._lock:
            active = self._reservation
            if active is not None:
                return {
                    "accepted": False,
                    "status_code": 409,
                    "message": (
                        f"API stage command is already running: {active.action}."
                    ),
                }
            self._reservation = reservation
        try:
            thread = self._thread_factory(
                target=lambda: self._run(request, reservation),
                name=f"ApiStageCommand-{action}",
                daemon=True,
            )
            thread.start()
        except Exception as exc:
            self._release(reservation)
            return {
                "accepted": False,
                "status_code": 500,
                "message": f"API stage command could not start: {exc}",
            }
        return completion

    def active(self) -> bool:
        with self._lock:
            return self._reservation is not None

    def wait_until_idle(self, *, timeout_s: float) -> bool:
        with self._changed:
            return bool(
                self._changed.wait_for(
                    lambda: self._reservation is None,
                    timeout=max(0.0, float(timeout_s)),
                )
            )

    def _run(
        self,
        command_request: dict[str, Any],
        reservation: _Reservation,
    ) -> None:
        try:
            response = self._direct_dispatch(command_request)
        except Exception as exc:
            _logger.exception("API stage command failed.")
            response = {
                "accepted": False,
                "status_code": 500,
                "message": str(exc),
            }
        try:
            reservation.completion.complete(response)
        finally:
            self._release(reservation)

    def _release(self, reservation: _Reservation) -> bool:
        with self._lock:
            if self._reservation is not reservation:
                return False
            self._reservation = None
            self._changed.notify_all()
            return True
