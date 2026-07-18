"""Qt bridge for asynchronous camera exposure policy commands."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping

from PySide6.QtCore import QObject, Signal, Slot

from .exposure_policy import ExposurePolicyController


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
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._command_state_lock = threading.Lock()
        self._command_active = False
        self._command_thread: threading.Thread | None = None
        self._closed = False
        self._controller.subscribe(self._on_controller_state_changed)

    def snapshot(self) -> dict[str, object]:
        """Return the controller state, including an adapter-dispatched command."""

        state = dict(self._controller.snapshot())
        if self._command_is_active():
            state["busy"] = True
        return state

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

    def shutdown(self, timeout_s: float = 2.0) -> None:
        """Reject new work and wait for the active command to finish."""

        timeout = max(0.0, float(timeout_s))
        with self._command_state_lock:
            self._closed = True
            thread = self._command_thread
        self._controller.unsubscribe(self._on_controller_state_changed)
        if thread is None or not thread.is_alive():
            return
        if thread is threading.current_thread():
            raise ExposurePolicyQtAdapterError(
                "Camera exposure adapter cannot join its active command."
            )
        thread.join(timeout)
        if thread.is_alive():
            raise ExposurePolicyQtAdapterError(
                "Camera exposure adapter did not stop active command."
            )

    def _start_command(self, command: Callable[[], Mapping[str, object]]) -> None:
        with self._command_state_lock:
            if self._closed:
                return
            if self._command_active:
                rejected = True
            else:
                self._command_active = True
                rejected = False
                thread = threading.Thread(
                    target=self._run_command,
                    args=(command,),
                    name="camera-exposure-command",
                    daemon=True,
                )
                self._command_thread = thread
                try:
                    thread.start()
                except Exception as exc:
                    self._command_active = False
                    self._command_thread = None
                    start_error = exc
                else:
                    start_error = None
        if rejected:
            self.command_finished.emit(
                {
                    "accepted": False,
                    "busy": True,
                    "message": "Camera exposure command is already running.",
                }
            )
            return
        if start_error is not None:
            self.state_changed.emit(self.snapshot())
            self.command_finished.emit(
                {
                    "accepted": False,
                    "message": str(start_error) or type(start_error).__name__,
                }
            )
            return
        self.state_changed.emit(self.snapshot())

    def _run_command(self, command: Callable[[], Mapping[str, object]]) -> None:
        try:
            result = dict(command())
            result.setdefault("accepted", True)
        except Exception as exc:
            result = {
                "accepted": False,
                "message": str(exc) or type(exc).__name__,
            }
        finally:
            closed = self._finish_command()
        if not closed:
            self.state_changed.emit(self.snapshot())
            self.command_finished.emit(result)

    def _on_controller_state_changed(self, state: dict[str, object]) -> None:
        del state
        if not self._is_closed():
            self.state_changed.emit(self.snapshot())

    def _command_is_active(self) -> bool:
        with self._command_state_lock:
            return self._command_active

    def _finish_command(self) -> bool:
        with self._command_state_lock:
            self._command_active = False
            if self._command_thread is threading.current_thread():
                self._command_thread = None
            return self._closed

    def _is_closed(self) -> bool:
        with self._command_state_lock:
            return self._closed


__all__ = ["ExposurePolicyQtAdapter", "ExposurePolicyQtAdapterError"]
