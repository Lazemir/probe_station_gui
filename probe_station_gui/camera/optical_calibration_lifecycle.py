"""Worker and retained-session lifecycle for optical calibration."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping

from probe_station_gui.camera.exposure_policy import ExposurePolicyBusyError
from probe_station_gui.camera.optical_calibration_adapters import (
    CalibrationStartDecision,
    FlatFieldCalibrationRequest,
    LensDistortionCalibrationRequest,
    OpticalCalibrationEventPort,
    OpticalCalibrationState,
    OpticalSessionLeasePort,
    OpticalSessionPort,
)


Request = FlatFieldCalibrationRequest | LensDistortionCalibrationRequest
Runner = Callable[[Request], None]
ThreadFactory = Callable[..., threading.Thread]


class OpticalCalibrationLifecycle:
    """Synchronize one worker and a parent exposure lease across wizard phases."""

    def __init__(
        self,
        *,
        sessions: OpticalSessionPort,
        events: OpticalCalibrationEventPort,
        thread_factory: ThreadFactory,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sessions = sessions
        self._events = events
        self._thread_factory = thread_factory
        self._sleep = sleep
        self._lock = threading.RLock()
        self.cancel_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._active_request: Request | None = None
        self._parent_lease: OpticalSessionLeasePort | None = None
        self._parent_token: str | None = None
        self._parent_close_thread: threading.Thread | None = None
        self._shutdown_requested = False

    def start(
        self,
        request: Request,
        kind: str,
        runner: Runner,
    ) -> CalibrationStartDecision:
        with self._lock:
            rejection = self._start_rejection(request, kind)
            if rejection is not None:
                return rejection
            self.cancel_event.clear()
            self._active_request = request
            thread = self._thread_factory(
                target=lambda: runner(request),
                name=("FlatFieldCalibration" if kind == "flat" else "LensDistortionCalibration"),
                daemon=True,
            )
            self._worker = thread
        try:
            thread.start()
        except Exception as exc:
            self._clear_unstarted(thread)
            return CalibrationStartDecision(
                False,
                500,
                f"{_label(kind)} calibration could not start: {exc}",
                request.run_id,
            )
        return CalibrationStartDecision(
            True,
            202,
            f"{_label(kind)} calibration started.",
            request.run_id,
        )

    def cancel(self, run_id: str | None = None) -> None:
        with self._lock:
            active = self._active_request
            if run_id is not None and active is not None and active.run_id != run_id:
                return
            self.cancel_event.set()
            close_parent = self._parent_lease is not None and not _thread_alive(
                self._worker
            )
        if close_parent:
            self._schedule_parent_close()

    def state(self) -> OpticalCalibrationState:
        with self._lock:
            request = self._active_request
            return OpticalCalibrationState(
                active_run_id=None if request is None else request.run_id,
                active_kind=_request_kind(request),
                parent_session_token=self._parent_token,
                cancel_requested=self.cancel_event.is_set(),
                shutdown_requested=self._shutdown_requested,
            )

    def shutdown(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._lock:
            self._shutdown_requested = True
        self.cancel()
        if not _join_worker(self._worker, deadline):
            return False
        with self._lock:
            self._active_request = None
            self._worker = None
        if self._parent_lease is not None:
            self.close_parent_session(deadline=deadline)
        if self._parent_lease is not None:
            return False
        if not _join_worker(self._parent_close_thread, deadline):
            return False
        with self._lock:
            return self._worker is None and self._parent_lease is None

    def open_child_session(
        self,
        operation: str,
        request: Request,
    ) -> OpticalSessionLeasePort:
        parent_token = request.parent_session_token
        if request.full_wizard and parent_token is None:
            parent_token = self._open_parent_session()
        try:
            return self._sessions.open(operation, parent_token=parent_token)
        except Exception:
            if request.full_wizard and request.parent_session_token is None:
                self.close_parent_session()
            raise

    def close_child(self, child: OpticalSessionLeasePort) -> list[str]:
        try:
            result = child.close()
            warning = str(result.get("warning") or "")
        except Exception as exc:
            warning = str(exc) or type(exc).__name__
        return [warning] if warning else []

    def should_close_parent(
        self,
        kind: str,
        request: Request,
        success: bool,
    ) -> bool:
        return request.full_wizard and (
            kind == "lens" or not success or self.cancel_event.is_set()
        )

    def close_parent_session(self, *, deadline: float | None = None) -> str:
        with self._lock:
            lease = self._parent_lease
        if lease is None:
            return ""
        warning, closed = self._close_parent_lease(lease, deadline=deadline)
        with self._lock:
            if closed and self._parent_lease is lease:
                self._parent_lease = None
                self._parent_token = None
        return warning

    def is_current(self, request: Request) -> bool:
        with self._lock:
            current = self._active_request
            return current is not None and current.run_id == request.run_id

    def finish(self, request: Request) -> bool:
        with self._lock:
            publish = self.is_current(request)
            if publish:
                self._active_request = None
                self._worker = None
            close_parent = (
                publish and self.cancel_event.is_set() and self._parent_lease is not None
            )
        if close_parent:
            self._schedule_parent_close()
        return publish

    def _start_rejection(
        self,
        request: Request,
        kind: str,
    ) -> CalibrationStartDecision | None:
        if self._shutdown_requested:
            return _rejected(request, "Optical calibration is shutting down.")
        if self._active_request is not None or _thread_alive(self._worker):
            return _rejected(request, "Optical calibration is already running.")
        if not self._parent_matches_request(request):
            return _rejected(
                request,
                "Optical calibration exposure session is unavailable.",
            )
        return None

    def _clear_unstarted(self, thread: threading.Thread) -> None:
        with self._lock:
            if self._worker is thread:
                self._worker = None
                self._active_request = None

    def _parent_matches_request(self, request: Request) -> bool:
        if not request.full_wizard or request.parent_session_token is None:
            return True
        return (
            self._parent_lease is not None
            and self._parent_token == request.parent_session_token
        )

    def _open_parent_session(self) -> str:
        outer = self._sessions.open("optical calibration")
        parent_token = str(outer.token)
        with self._lock:
            if self._parent_lease is not None:
                outer.close()
                raise RuntimeError("Optical calibration session is already active.")
            self._parent_lease = outer
            self._parent_token = parent_token
        return parent_token

    def _close_parent_lease(
        self,
        lease: OpticalSessionLeasePort,
        *,
        deadline: float | None = None,
    ) -> tuple[str, bool]:
        while True:
            try:
                result: Mapping[str, object] = lease.close()
                return str(result.get("warning") or ""), True
            except ExposurePolicyBusyError:
                if deadline is not None and time.monotonic() >= deadline:
                    return "Exposure policy is busy.", False
                delay = 0.05
                if deadline is not None:
                    delay = min(delay, max(0.0, deadline - time.monotonic()))
                self._sleep(delay)
            except Exception as exc:
                warning = str(exc) or type(exc).__name__
                try:
                    return warning, not lease.is_active()
                except Exception:
                    return warning, False

    def _schedule_parent_close(self) -> bool:
        with self._lock:
            if self._parent_lease is None or _thread_alive(self._parent_close_thread):
                return True
            thread = self._thread_factory(
                target=self._run_parent_close,
                name="OpticalCalibrationSessionClose",
                daemon=True,
            )
            self._parent_close_thread = thread
        try:
            thread.start()
        except Exception as exc:
            with self._lock:
                if self._parent_close_thread is thread:
                    self._parent_close_thread = None
            self._events.warning(f"Exposure policy restore could not start: {exc}")
            return False
        return True

    def _run_parent_close(self) -> None:
        warning = self.close_parent_session()
        with self._lock:
            self._parent_close_thread = None
        if warning:
            self._events.warning(f"Exposure policy restore failed: {warning}")


def _request_kind(request: Request | None) -> str | None:
    if request is None:
        return None
    return "flat" if isinstance(request, FlatFieldCalibrationRequest) else "lens"


def _label(kind: str) -> str:
    return "Flat-field" if kind == "flat" else "Lens distortion"


def _rejected(request: Request, message: str) -> CalibrationStartDecision:
    return CalibrationStartDecision(False, 409, message, request.run_id)


def _thread_alive(thread: threading.Thread | None) -> bool:
    return bool(thread is not None and thread.is_alive())


def _join_worker(thread: threading.Thread | None, deadline: float) -> bool:
    if thread is None or not thread.is_alive():
        return True
    thread.join(max(0.0, deadline - time.monotonic()))
    return not thread.is_alive()


__all__ = ["OpticalCalibrationLifecycle"]
