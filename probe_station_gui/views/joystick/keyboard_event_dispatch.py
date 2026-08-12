"""Qt event dispatch and focus scoping for joystick keyboard input."""

from __future__ import annotations

import logging
import time
from typing import Optional

from PySide6.QtCore import QEvent, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QWidget,
)

from probe_station_gui.shared.qt_compat import (
    keyboard_modifiers_to_int,
    native_scan_code_to_int,
)


logger = logging.getLogger(__name__)


class JoystickKeyboardEventDispatchMixin:
    """Own Qt lifecycle, global routing, and focus eligibility."""

    def _install_event_filter(self) -> None:
        if self._event_filter_installed:
            return
        app = QApplication.instance()
        if app is None:
            if not self._event_filter_retry_scheduled:
                self._event_filter_retry_scheduled = True
                QTimer.singleShot(0, self._install_event_filter)
            logger.warning(
                "QApplication instance unavailable; joystick event filter deferred"
            )
            return
        app.installEventFilter(self)
        self._event_filter_installed = True
        self._event_filter_retry_scheduled = False
        logger.debug("Joystick event filter installed")

    def _remove_event_filter(self) -> None:
        if not self._event_filter_installed:
            return
        app = QApplication.instance()
        if app is None:
            return
        app.removeEventFilter(self)
        self._event_filter_installed = False
        self._event_filter_retry_scheduled = False
        logger.debug("Joystick event filter removed")

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        if not self._apply_wheel_delta(event.angleDelta().y()):
            super().wheelEvent(event)
            return
        event.accept()

    def _apply_wheel_delta(self, delta_y: int) -> bool:
        if delta_y == 0:
            return False
        now = time.monotonic()
        dt = now - self._last_feedrate_wheel_at if self._last_feedrate_wheel_at else 1.0
        self._last_feedrate_wheel_at = now
        notch_units = abs(delta_y) / 120.0
        speed_multiplier = self._wheel_speed_multiplier(dt)
        base_step = max(0.2, self._linear_feedrate_value * 0.03)
        step = base_step * notch_units * speed_multiplier
        if delta_y < 0:
            step = -step
        self._set_linear_feedrate(
            self._linear_feedrate_value + step,
            reissue_if_active=self._active_axes is not None,
        )
        logger.debug(
            "Feedrate wheel applied: delta=%s step=%s value=%s active_axes=%s",
            delta_y,
            step,
            self._linear_feedrate_value,
            self._active_axes,
        )
        return True

    @staticmethod
    def _wheel_speed_multiplier(dt: float) -> float:
        multiplier = 1.0
        if dt < 0.25:
            multiplier += min(5.0, (0.25 - dt) * 12.0)
        return multiplier

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if self._handle_key_press_event(event):
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # type: ignore[override]
        if self._handle_key_release_event(event):
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event) -> None:  # type: ignore[override]
        self._pending_jog_axes = None
        self._clear_pending_key_activations()
        self._key_stack.clear()
        self._key_press_times.clear()
        self._sync_physical_key_watchdog()
        self.stop_jog()
        super().focusOutEvent(event)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        editor = self._needle_contact_coordinate_edit
        if editor is not None and editor.isVisible():
            self._position_needle_contact_coordinate_editor()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._install_event_filter()

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        self._pending_jog_axes = None
        self._clear_pending_key_activations()
        self._key_stack.clear()
        self._key_press_times.clear()
        self._sync_physical_key_watchdog()
        self.stop_jog()
        self._remove_event_filter()
        super().closeEvent(event)

    def eventFilter(self, obj, event):  # type: ignore[override]
        event_type = event.type()
        if event_type in self.GLOBAL_KEY_EVENT_TYPES:
            self._log_global_key_event(obj, event)
        handler_name = self.GLOBAL_EVENT_HANDLER_NAMES.get(event_type)
        if handler_name is not None and getattr(self, handler_name)(obj, event):
            return self._accept_global_event(event)
        return super().eventFilter(obj, event)

    def _handle_shortcut_override_global_event(self, obj, event) -> bool:
        if self._should_process_global_event(
            obj, require_motion_ready=False
        ) and self._is_control_mode_toggle_event(event):
            return True
        if not self._should_process_global_event(obj):
            return False
        identifier, mapping = self._mapping_from_event(event)
        return bool(identifier and mapping)

    def _handle_key_press_global_event(self, obj, event) -> bool:
        if self._should_process_global_event(
            obj, require_motion_ready=False
        ) and self._handle_control_mode_toggle_press(event):
            return True
        return self._should_process_global_event(obj) and self._handle_key_press_event(
            event
        )

    def _handle_key_release_global_event(self, obj, event) -> bool:
        if self._should_process_global_event(
            obj, require_motion_ready=False
        ) and self._handle_control_mode_toggle_release(event):
            return True
        return self._should_process_global_event(
            obj
        ) and self._handle_key_release_event(event)

    def _handle_wheel_global_event(self, obj, event) -> bool:
        return self._should_process_global_event(obj) and self._handle_wheel_event(
            event, obj
        )

    def _log_global_key_event(self, obj, event) -> None:
        event_type_name = {
            QEvent.KeyPress: "KeyPress",
            QEvent.KeyRelease: "KeyRelease",
            QEvent.ShortcutOverride: "ShortcutOverride",
        }.get(event.type(), str(int(event.type())))
        key_value = getattr(event, "key", lambda: None)()
        scan_code_value = native_scan_code_to_int(
            getattr(event, "nativeScanCode", lambda: 0)()
        )
        text_value = getattr(event, "text", lambda: "")()
        modifiers_value = keyboard_modifiers_to_int(
            getattr(event, "modifiers", lambda: 0)()
        )
        object_name = getattr(obj, "objectName", lambda: "")()
        source_name = object_name or obj.__class__.__name__
        logger.debug(
            "Global key event: type=%s key=%s scan=%s text=%r modifiers=%s source=%s",
            event_type_name,
            key_value,
            scan_code_value,
            text_value,
            modifiers_value,
            source_name,
        )

    @staticmethod
    def _accept_global_event(event) -> bool:
        event.accept()
        return True

    def _should_process_global_event(
        self, obj, *, require_motion_ready: bool = True
    ) -> bool:
        if not self.isVisible():
            logger.debug("Ignoring global key event because joystick is hidden")
            return False
        app = self._global_event_context(obj)
        if app is None:
            return False
        if require_motion_ready and not (
            self._axis_a_ready or self._motion_safety_disabled
        ):
            logger.debug("Ignoring global key event because A axis is not homed/zero")
            return False
        if self._global_event_focus_is_blocked(app, obj):
            return False
        return True

    def _global_event_context(self, obj):
        window = self.window()
        app = QApplication.instance()
        active_window = app.activeWindow() if app is not None else None
        if window is None:
            logger.debug(
                "Ignoring global key event because joystick window is unavailable"
            )
            return None
        if active_window is None:
            logger.debug(
                "Ignoring global key event because application has no active window"
            )
            return None
        if active_window is not window and obj is not active_window:
            try:
                obj_window = obj.window() if hasattr(obj, "window") else None
            except RuntimeError:
                obj_window = None
            if obj_window is not window:
                logger.debug(
                    "Ignoring global key event because active window does not belong to joystick host"
                )
                return None
        if not window.isActiveWindow() and active_window is not window:
            logger.debug(
                "Ignoring global key event because joystick host window is not active"
            )
            return None
        return app

    def _global_event_focus_is_blocked(self, app, obj) -> bool:
        focus_widget = app.focusWidget() if app else None
        if self._is_text_entry_widget(focus_widget) or self._is_terminal_widget(
            focus_widget
        ):
            logger.debug(
                "Ignoring global key event because focus is in terminal/text input"
            )
            return True
        if isinstance(obj, QWidget) and self._is_text_entry_widget(obj):
            logger.debug(
                "Ignoring global key event originating from text widget %s",
                obj.objectName() or obj.__class__.__name__,
            )
            return True
        return False

    def _handle_wheel_event(self, event, obj) -> bool:
        widget = obj if isinstance(obj, QWidget) else None
        if self._is_text_entry_widget(widget) or self._is_terminal_widget(widget):
            return False
        return self._apply_wheel_delta(event.angleDelta().y())

    @staticmethod
    def _is_text_entry_widget(widget: Optional[QWidget]) -> bool:
        if widget is None:
            return False
        if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)):
            return True
        parent = widget.parentWidget()
        if parent is not None and parent is not widget:
            return JoystickKeyboardEventDispatchMixin._is_text_entry_widget(parent)
        return False

    @staticmethod
    def _is_terminal_widget(widget: Optional[QWidget]) -> bool:
        current = widget
        while current is not None:
            if current.__class__.__name__ == "SerialTerminalWindow":
                return True
            current = current.parentWidget()
        return False
