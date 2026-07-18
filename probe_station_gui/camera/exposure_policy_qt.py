"""Qt bridge for asynchronous camera exposure policy commands."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping

from PySide6.QtCore import QObject, Signal, Slot

from .exposure_policy import ExposurePolicyController


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

    def _start_command(self, command: Callable[[], Mapping[str, object]]) -> None:
        with self._command_state_lock:
            if self._command_active:
                rejected = True
            else:
                self._command_active = True
                rejected = False
        if rejected:
            self.command_finished.emit(
                {
                    "accepted": False,
                    "busy": True,
                    "message": "Camera exposure command is already running.",
                }
            )
            return
        self.state_changed.emit(self.snapshot())
        try:
            threading.Thread(
                target=self._run_command,
                args=(command,),
                name="camera-exposure-command",
                daemon=True,
            ).start()
        except Exception as exc:
            self._set_command_active(False)
            self.state_changed.emit(self.snapshot())
            self.command_finished.emit(
                {
                    "accepted": False,
                    "message": str(exc) or type(exc).__name__,
                }
            )

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
            self._set_command_active(False)
        self.state_changed.emit(self.snapshot())
        self.command_finished.emit(result)

    def _on_controller_state_changed(self, state: dict[str, object]) -> None:
        del state
        self.state_changed.emit(self.snapshot())

    def _command_is_active(self) -> bool:
        with self._command_state_lock:
            return self._command_active

    def _set_command_active(self, active: bool) -> None:
        with self._command_state_lock:
            self._command_active = active


__all__ = ["ExposurePolicyQtAdapter"]
