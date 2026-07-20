"""Qt bridge for asynchronous camera exposure policy commands."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping

from PySide6.QtCore import QObject, Signal, Slot

from .exposure_policy import ExposurePolicyController


SettingsWrite = Callable[[list[tuple[str, object]]], Mapping[str, object]]


class ExposurePolicyQtAdapterError(RuntimeError):
    """Raised when the Qt adapter cannot stop outstanding work."""


class ExposurePolicyQtAdapter(QObject):
    """Run exposure policy commands away from the Qt GUI thread."""

    state_changed = Signal(object)
    command_finished = Signal(object)

    def __init__(
        self,
        controller: ExposurePolicyController,
        parent: QObject | None = None,
        *,
        settings_write: SettingsWrite | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._settings_write = settings_write
        self._command_state_lock = threading.Lock()
        self._command_active = False
        self._command_threads: set[threading.Thread] = set()
        self._controller_subscribed = True
        self._emission_gate = threading.Condition()
        self._emission_closed = False
        self._emissions_in_progress = 0
        self._controller.subscribe(self._on_controller_state_changed)

    def snapshot(self) -> dict[str, object]:
        """Return the controller state, including an adapter-dispatched command."""

        state = dict(self._controller.snapshot())
        if self._command_is_active():
            state["busy"] = True
        return state

    @Slot()
    def request_start(self) -> None:
        self._start_command(self._controller.start, operation="start")

    @Slot(bool, str)
    def request_update(self, auto_enabled: bool, engine: str) -> None:
        self._start_command(
            lambda: self._controller.set_policy(
                auto_enabled=auto_enabled,
                engine=engine,
            )
        )

    @Slot()
    def request_once(self) -> None:
        self._start_command(self._controller.run_once)

    @Slot(float)
    def request_exposure_time(self, value: float) -> None:
        def write() -> Mapping[str, object]:
            if self._settings_write is None:
                raise RuntimeError("Camera exposure settings writer is unavailable.")
            return self._controller.run_manual_exposure_write(
                lambda: self._settings_write([("ExposureTime", float(value))])
            )

        self._start_command(write, operation="manual_exposure_write")

    def shutdown(self, timeout_s: float = 2.0) -> None:
        """Reject new work and wait for the active command to finish."""

        timeout = max(0.0, float(timeout_s))
        deadline = time.monotonic() + timeout
        with self._emission_gate:
            self._emission_closed = True
        with self._command_state_lock:
            threads = tuple(self._command_threads)
            subscribed = self._controller_subscribed
            self._controller_subscribed = False
        if subscribed:
            self._controller.unsubscribe(self._on_controller_state_changed)
        for thread in threads:
            if thread is threading.current_thread() and thread.is_alive():
                raise ExposurePolicyQtAdapterError(
                    "Camera exposure adapter cannot join its active command."
                )
            if thread.is_alive():
                thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                raise ExposurePolicyQtAdapterError(
                    "Camera exposure adapter did not stop active command."
                )
        with self._command_state_lock:
            self._command_threads.difference_update(threads)
        with self._emission_gate:
            while self._emissions_in_progress:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    raise ExposurePolicyQtAdapterError(
                        "Camera exposure adapter did not finish signal emission."
                    )
                self._emission_gate.wait(remaining)

    def _start_command(
        self,
        command: Callable[[], Mapping[str, object]],
        *,
        operation: str | None = None,
    ) -> None:
        with self._emission_gate:
            if self._emission_closed:
                return
            with self._command_state_lock:
                self._command_threads = {
                    thread for thread in self._command_threads if thread.is_alive()
                }
                if self._command_active:
                    rejected = True
                else:
                    self._command_active = True
                    rejected = False
                    thread = threading.Thread(
                        target=self._run_command,
                        args=(command, operation),
                        name="camera-exposure-command",
                        daemon=True,
                    )
                    self._command_threads.add(thread)
                    try:
                        thread.start()
                    except Exception as exc:
                        self._command_active = False
                        self._command_threads.discard(thread)
                        start_error = exc
                    else:
                        start_error = None
        if rejected:
            result: dict[str, object] = {
                "accepted": False,
                "busy": True,
                "message": "Camera exposure command is already running.",
            }
            if operation is not None:
                result["operation"] = operation
            self._emit(self.command_finished, result)
            return
        if start_error is not None:
            self._emit(self.state_changed, self.snapshot())
            result = {
                "accepted": False,
                "message": str(start_error) or type(start_error).__name__,
            }
            if operation is not None:
                result["operation"] = operation
            self._emit(self.command_finished, result)
            return
        self._emit(self.state_changed, self.snapshot())

    def _run_command(
        self,
        command: Callable[[], Mapping[str, object]],
        operation: str | None = None,
    ) -> None:
        try:
            result = dict(command())
            result.setdefault("accepted", True)
        except Exception as exc:
            result = {
                "accepted": False,
                "message": str(exc) or type(exc).__name__,
            }
        finally:
            self._finish_command()
        if operation is not None:
            result["operation"] = operation
        self._emit(self.state_changed, self.snapshot())
        self._emit(self.command_finished, result)

    def _on_controller_state_changed(self, state: dict[str, object]) -> None:
        del state
        self._emit(self.state_changed, self.snapshot())

    def _command_is_active(self) -> bool:
        with self._command_state_lock:
            return self._command_active

    def _finish_command(self) -> None:
        with self._command_state_lock:
            self._command_active = False

    def _emit(self, signal: Signal, payload: object) -> None:
        with self._emission_gate:
            if self._emission_closed:
                return
            self._emissions_in_progress += 1
        try:
            signal.emit(payload)
        finally:
            with self._emission_gate:
                self._emissions_in_progress -= 1
                if not self._emissions_in_progress:
                    self._emission_gate.notify_all()


__all__ = ["ExposurePolicyQtAdapter", "ExposurePolicyQtAdapterError"]
