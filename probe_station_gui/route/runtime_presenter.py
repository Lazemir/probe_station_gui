"""Route runtime presentation helpers for dialog and navigator fanout."""

from __future__ import annotations

from typing import Callable


class RouteDialogRuntimeSink:
    def __init__(self, current_dialog: Callable[[], object | None]) -> None:
        self._current_dialog = current_dialog

    def _dialog(self) -> object | None:
        return self._current_dialog()

    def _invoke(
        self,
        method_name: str,
        *args: object,
        optional: bool = False,
        **kwargs: object,
    ) -> bool:
        dialog = self._dialog()
        if dialog is None:
            return False
        if optional and not hasattr(dialog, method_name):
            return True
        getattr(dialog, method_name)(*args, **kwargs)
        return True

    def set_status(self, message: str) -> bool:
        return self._invoke("set_status", message)

    def set_running(self, active: bool) -> bool:
        return self._invoke("set_running", bool(active))

    def set_waiting(self, waiting: bool, *, reason: str = "") -> bool:
        waiting = bool(waiting)
        reason = str(reason)
        if reason:
            return self._invoke("set_waiting", waiting, reason)
        return self._invoke("set_waiting", waiting)

    def set_pause_request_pending(self, pending: bool) -> bool:
        return self._invoke(
            "set_pause_request_pending",
            bool(pending),
            optional=True,
        )

    def set_interrupt_request_pending(self, pending: bool) -> bool:
        return self._invoke(
            "set_interrupt_request_pending",
            bool(pending),
            optional=True,
        )

    def reset_progress(self, total: int) -> bool:
        return self._invoke("reset_progress", int(total))

    def finish_progress(self, success: bool) -> bool:
        return self._invoke("finish_progress", bool(success))

    def set_progress(self, position: int, total: int, point_number: int) -> bool:
        return self._invoke(
            "set_progress",
            int(position),
            int(total),
            int(point_number),
        )

    def set_result(
        self,
        record: object,
        position: int,
        total: int,
        saved: bool,
    ) -> bool:
        return self._invoke(
            "set_result",
            record,
            int(position),
            int(total),
            bool(saved),
        )

    def set_current_point(self, point_number: int, *, save: bool = True) -> bool:
        return self._invoke(
            "set_current_point",
            int(point_number),
            save=bool(save),
        )

    def set_measurement_session_active(
        self,
        active: bool,
        *,
        save: bool = True,
    ) -> bool:
        return self._invoke(
            "set_measurement_session_active",
            bool(active),
            save=bool(save),
        )


class RouteRuntimePresentationSink:
    def __init__(
        self,
        *,
        current_dialog: Callable[[], object | None],
        current_navigator: Callable[[], object | None],
    ) -> None:
        self._dialog = RouteDialogRuntimeSink(current_dialog)
        self._current_navigator = current_navigator

    def _navigator(self) -> object | None:
        return self._current_navigator()

    def _navigator_invoke(
        self,
        method_name: str,
        *args: object,
        optional: bool = False,
        **kwargs: object,
    ) -> bool:
        navigator = self._navigator()
        if navigator is None:
            return False
        if optional and not hasattr(navigator, method_name):
            return True
        getattr(navigator, method_name)(*args, **kwargs)
        return True

    def _present(self) -> bool:
        return self._navigator() is not None or self._dialog._dialog() is not None

    def _set_navigator_status(self, message: str) -> bool:
        return self._navigator_invoke("set_route_measurement_status", message)

    def _set_navigator_running(self, active: bool) -> bool:
        return self._navigator_invoke("set_route_measurement_running", bool(active))

    def _set_navigator_waiting(self, waiting: bool, reason: str = "") -> bool:
        return self._navigator_invoke(
            "set_route_measurement_waiting",
            bool(waiting),
            str(reason),
        )

    def set_status(self, message: str) -> bool:
        panel_present = self._navigator_invoke(
            "set_route_measurement_status",
            message,
        )
        dialog_present = self._dialog.set_status(message)
        return panel_present or dialog_present

    def set_running(self, active: bool) -> bool:
        panel_present = self._navigator_invoke(
            "set_route_measurement_running",
            bool(active),
        )
        dialog_present = self._dialog.set_running(active)
        return panel_present or dialog_present

    def set_waiting(self, waiting: bool, *, reason: str = "") -> bool:
        waiting = bool(waiting)
        reason = str(reason)
        panel_present = self._navigator_invoke(
            "set_route_measurement_waiting",
            waiting,
            reason,
        )
        dialog_present = self._dialog.set_waiting(waiting, reason=reason)
        return panel_present or dialog_present

    def set_pause_request_pending(self, pending: bool) -> bool:
        panel_present = self._navigator_invoke(
            "set_route_measurement_pause_request_pending",
            bool(pending),
            optional=True,
        )
        dialog_present = self._dialog.set_pause_request_pending(pending)
        return panel_present or dialog_present

    def set_interrupt_request_pending(self, pending: bool) -> bool:
        panel_present = self._navigator_invoke(
            "set_route_measurement_interrupt_request_pending",
            bool(pending),
            optional=True,
        )
        dialog_present = self._dialog.set_interrupt_request_pending(pending)
        return panel_present or dialog_present

    def reset_progress(self, total: int) -> bool:
        return self._dialog.reset_progress(total)

    def finish_progress(self, success: bool) -> bool:
        return self._dialog.finish_progress(success)

    def set_progress(self, position: int, total: int, point_number: int) -> bool:
        return self._dialog.set_progress(position, total, point_number)

    def set_result(
        self,
        record: object,
        position: int,
        total: int,
        saved: bool,
    ) -> bool:
        return self._dialog.set_result(record, position, total, saved)

    def set_current_point(self, point_number: int, *, save: bool = True) -> bool:
        return self._dialog.set_current_point(point_number, save=save)

    def set_measurement_session_active(
        self,
        active: bool,
        *,
        save: bool = True,
    ) -> bool:
        return self._dialog.set_measurement_session_active(active, save=save)

    def apply_api_control_update(self, ui_state: object, message: str) -> bool:
        if not self.set_running(bool(getattr(ui_state, "active", False))):
            return False
        self.set_pause_request_pending(bool(getattr(ui_state, "pause_pending", False)))
        self.set_waiting(
            bool(getattr(ui_state, "waiting", False)),
            reason=str(getattr(ui_state, "control_waiting_reason", "") or ""),
        )
        self.set_status(message)
        return True

    def route_runner_started(self, message: str, total: int) -> bool:
        panel_present = self._set_navigator_running(True)
        if self._set_navigator_waiting(False):
            panel_present = True
        if self._set_navigator_status(message):
            panel_present = True
        dialog_present = self._dialog.set_running(True)
        if self._dialog.reset_progress(total):
            dialog_present = True
        if self._dialog.set_status(message):
            dialog_present = True
        return panel_present or dialog_present

    def route_started(
        self,
        message: str,
        total: int,
        *,
        waiting: bool,
        waiting_reason: str,
    ) -> bool:
        panel_present = self._set_navigator_running(True)
        if self._set_navigator_waiting(waiting, waiting_reason):
            panel_present = True
        if self._set_navigator_status(message):
            panel_present = True
        dialog_present = self._dialog.set_running(True)
        if self._dialog.set_waiting(waiting, reason=waiting_reason):
            dialog_present = True
        if self._dialog.reset_progress(total):
            dialog_present = True
        if self._dialog.set_status(message):
            dialog_present = True
        return panel_present or dialog_present

    def stop_requested(self, message: str) -> bool:
        present = self.set_waiting(False)
        if self.set_status(message):
            present = True
        return present

    def clear_waiting(self) -> bool:
        return self.set_waiting(False)

    def pause_requested(self, message: str) -> bool:
        if not self.set_pause_request_pending(True):
            return False
        self.set_status(message)
        return True

    def shift_status(self, message: str, *, mark_interrupt_pending: bool) -> bool:
        present = self.set_interrupt_request_pending(bool(mark_interrupt_pending))
        if not present:
            return False
        self.set_status(message)
        return True

    def waiting_changed(self, waiting: bool, reason: str) -> bool:
        return self.set_waiting(waiting, reason=reason)

    def unsaved_result_status(self, message: str) -> bool:
        return self.set_status(message)

    def recorded_result_status(self, message: str) -> bool:
        return self.set_status(message)

    def finished_ui(self, success: bool, message: str) -> bool:
        panel_present = self._set_navigator_running(False)
        if self._set_navigator_waiting(False):
            panel_present = True
        if self._set_navigator_status(message):
            panel_present = True
        dialog_present = self._dialog.set_running(False)
        if self._dialog.finish_progress(success):
            dialog_present = True
        if self._dialog.set_status(message):
            dialog_present = True
        return panel_present or dialog_present


__all__ = [
    "RouteDialogRuntimeSink",
    "RouteRuntimePresentationSink",
]
