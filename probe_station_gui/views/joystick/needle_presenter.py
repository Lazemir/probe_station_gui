"""Needle state, contact-coordinate, and animation presentation."""

from __future__ import annotations

import logging
import math

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMenu, QPushButton


logger = logging.getLogger(__name__)


class JoystickNeedlePresenterMixin:
    """Own needle actions, status presentation, and coordinate editing."""

    def set_axis_a_ready(self, ready: bool) -> None:
        was_ready = self._axis_a_ready
        self._axis_a_ready = ready
        if ready and not was_ready:
            self._invalidate_jog_stop_resend()
        if not ready and not self._motion_safety_disabled:
            self.stop_jog()
            self._pending_jog_axes = None
            self._key_stack.clear()
            self._key_press_times.clear()
            self._clear_pending_key_activations()
            self._sync_physical_key_watchdog()
        self._update_enabled_state()

    def set_needles_state(self, raised: bool, known: bool) -> None:
        self._needles_up = bool(raised)
        self._needles_known = bool(known)
        if not self._needles_known:
            self._needles_zone = None
        elif self._needles_up:
            self._needles_zone = "raise"
        elif getattr(self, "_needles_zone", None) not in {"lift", "lower"}:
            self._needles_zone = "lower"
        self._apply_needle_button_styles()

    def set_needles_zone(self, zone: str) -> None:
        zone_key = str(zone).strip().lower()
        self._needles_zone = (
            zone_key if zone_key in {"raise", "lift", "lower"} else None
        )
        self._needles_known = self._needles_zone is not None
        self._needles_up = self._needles_zone == "raise"
        self._apply_needle_button_styles()

    def _apply_needle_button_styles(self) -> None:
        self.needles_raise_button.setStyleSheet(self.NEEDLES_DOWN_STYLE)
        self.needles_lift_button.setStyleSheet(self.NEEDLES_DOWN_STYLE)
        self.needles_lower_button.setStyleSheet(self.NEEDLES_DOWN_STYLE)
        if self._needles_known:
            zone = getattr(self, "_needles_zone", None)
            if zone == "raise" or (zone is None and self._needles_up):
                self.needles_raise_button.setStyleSheet(self.NEEDLES_UP_STYLE)
            elif zone == "lift":
                self.needles_lift_button.setStyleSheet(self.NEEDLES_UP_STYLE)
            else:
                self.needles_lower_button.setStyleSheet(self.NEEDLES_UP_STYLE)
        active_style = (
            self.NEEDLES_ACTIVE_DIM_STYLE
            if self._needle_blink_dimmed
            else self.NEEDLES_ACTIVE_STYLE
        )
        for button in self._needle_targets.values():
            button.setStyleSheet(active_style)

    def set_needles_action_started(self, action: str) -> None:
        if action == "raise":
            self._start_needle_animation("raise", self.needles_raise_button)
        elif action == "lift":
            self._start_needle_animation("lift", self.needles_lift_button)
        elif action == "lower":
            self._start_needle_animation("lower", self.needles_lower_button)

    def set_needles_action_finished(
        self, success: bool, message: str, action: str
    ) -> None:
        if action == "raise":
            self._stop_needle_animation("raise")
        elif action == "lift":
            self._stop_needle_animation("lift")
        elif action == "lower":
            self._stop_needle_animation("lower")
        if not success:
            self._show_warning(message)

    def set_needle_contact_coordinate(
        self,
        action: str,
        a_coordinate: float | None,
    ) -> None:
        action_key = self._needle_action_for_key(action)
        if action_key is None:
            return
        if a_coordinate is None:
            self._saved_needle_contact_a_coordinates.pop(action_key, None)
            return
        try:
            value = float(a_coordinate)
        except (TypeError, ValueError):
            self._saved_needle_contact_a_coordinates.pop(action_key, None)
            return
        if math.isfinite(value):
            self._saved_needle_contact_a_coordinates[action_key] = value
        else:
            self._saved_needle_contact_a_coordinates.pop(action_key, None)

    def _raise_needles(self) -> None:
        if hasattr(self, "linear_feedrate_slider"):
            self._set_active_feedrate_target(self.FEED_TARGET_NEEDLES)
        self.needles_raise_requested.emit(self._needle_feedrate_value)

    def _lift_needles(self) -> None:
        if hasattr(self, "linear_feedrate_slider"):
            self._set_active_feedrate_target(self.FEED_TARGET_NEEDLES)
        self.needles_lift_requested.emit(self._needle_feedrate_value)

    def _lower_needles(self) -> None:
        if hasattr(self, "linear_feedrate_slider"):
            self._set_active_feedrate_target(self.FEED_TARGET_NEEDLES)
        self.needles_lower_requested.emit(self._needle_feedrate_value)

    def _save_lower_needle_contact_from_current_position(self) -> None:
        self.needle_current_lower_contact_save_requested.emit()

    def _needle_action_for_key(self, action: str) -> str | None:
        action_key = str(action).strip().lower()
        if action_key in {"raise", "lift", "lower"}:
            return action_key
        return None

    def _needle_action_for_button(self, button: QPushButton) -> str:
        if button is self.needles_lower_button:
            return "lower"
        if button is self.needles_lift_button:
            return "lift"
        return "raise"

    def _show_needle_contact_coordinate_menu(self, pos) -> None:
        sender = self.sender()
        target_button = (
            sender if isinstance(sender, QPushButton) else self.needles_raise_button
        )
        action = self._needle_action_for_button(target_button)
        default_a_position = self._saved_needle_contact_a_coordinates.get(action)
        if default_a_position is None:
            default_a_position = self._current_needle_contact_a_coordinate()
        self._show_needle_contact_coordinate_editor(
            target_button,
            default_a_position,
        )
        self._show_needle_contact_context_menu(
            target_button,
            action,
            target_button.mapToGlobal(pos),
        )

    def _show_active_needle_contact_coordinate_menu(self, pos) -> None:
        editor = self._needle_contact_coordinate_edit
        if editor is None:
            return
        target_button = (
            self._needle_contact_coordinate_button or self.needles_raise_button
        )
        action = self._needle_action_for_button(target_button)
        self._show_needle_contact_context_menu(
            target_button,
            action,
            editor.line_edit().mapToGlobal(pos),
        )

    def _show_needle_contact_context_menu(
        self,
        target_button: QPushButton,
        action: str,
        global_pos,
    ) -> None:

        editor = self._needle_contact_coordinate_edit
        if editor is not None:
            editor.set_cancel_on_focus_out(False)
        menu = (
            editor.line_edit().createStandardContextMenu()
            if editor is not None
            else QMenu(target_button)
        )
        menu.addSeparator()
        save_action = menu.addAction("Save")
        use_current_action = menu.addAction("Use Current A Coordinate")
        menu.setDefaultAction(save_action)
        selected = menu.exec(global_pos)
        if editor is not None:
            editor.set_cancel_on_focus_out(True)
        if selected == save_action:
            self._save_needle_contact_coordinate_from_editor(action)
        elif selected == use_current_action:
            self._show_needle_contact_coordinate_editor(
                target_button,
                self._current_needle_contact_a_coordinate(),
            )
        elif editor is not None and editor.isVisible():
            editor.setFocus(Qt.PopupFocusReason)

    def _current_needle_contact_a_coordinate(self) -> float | None:
        if self.stage_controller is None:
            self._show_warning("Stage controller is not available.")
            return None
        raw_a_position = self.stage_controller.latest_a_position()
        if raw_a_position is None:
            reason = self.stage_controller.last_a_position_read_failure()
            if reason:
                logger.warning("Unable to open contact coordinate editor: %s", reason)
            self._show_warning("Unable to read current A coordinate.")
            return None
        return self.stage_controller.calibrated_axis_display_value(
            "A",
            raw_a_position,
        )

    def _show_needle_contact_coordinate_editor(
        self,
        target_button: QPushButton,
        display_a_position: float | None,
    ) -> None:
        if display_a_position is None:
            return
        editor = self._needle_contact_coordinate_edit
        if editor is None:
            return
        self._needle_contact_coordinate_button = target_button
        self._position_needle_contact_coordinate_editor()
        editor.setText(f"{display_a_position:.4f}")
        editor.setModified(False)
        editor.show()
        editor.raise_()
        editor.setFocus(Qt.PopupFocusReason)
        editor.selectAll()

    def _save_needle_contact_coordinate(self, a_coordinate: float) -> None:
        target_button = (
            self._needle_contact_coordinate_button or self.needles_raise_button
        )
        action = self._needle_action_for_button(target_button)
        self._save_needle_contact_coordinate_value(action, a_coordinate)

    def _save_needle_contact_coordinate_from_editor(self, action: str) -> None:
        editor = self._needle_contact_coordinate_edit
        if editor is None:
            return
        value = editor.value()
        if value is None:
            QApplication.beep()
            return
        self._save_needle_contact_coordinate_value(action, value)

    def _save_needle_contact_coordinate_value(
        self,
        action: str,
        a_coordinate: float,
    ) -> None:
        editor = self._needle_contact_coordinate_edit
        if editor is not None:
            editor.hide()
        self._needle_contact_coordinate_button = None
        self.needle_contact_coordinate_save_requested.emit(action, float(a_coordinate))

    def _cancel_needle_contact_coordinate_edit(self) -> None:
        editor = self._needle_contact_coordinate_edit
        if editor is None or not editor.isVisible():
            return
        editor.hide()
        editor.clear()
        self._needle_contact_coordinate_button = None

    def _position_needle_contact_coordinate_editor(self) -> None:
        editor = self._needle_contact_coordinate_edit
        if editor is None:
            return
        target_button = (
            self._needle_contact_coordinate_button or self.needles_raise_button
        )
        button_rect = target_button.geometry()
        desired_width = max(button_rect.width() + 68, 148)
        left = button_rect.left()
        if target_button is self.needles_lower_button:
            left = button_rect.right() - desired_width + 1
        left = max(0, min(left, max(0, self.width() - desired_width)))
        editor.setGeometry(
            left,
            button_rect.top(),
            desired_width,
            button_rect.height(),
        )

    def _start_needle_animation(self, key: str, button: QPushButton) -> None:
        if key in self._needle_targets:
            return
        base_text = button.text()
        self._needle_targets[key] = button
        self._needle_text[key] = base_text
        button.setProperty("homing", True)
        button.setChecked(True)
        self.needles_raise_button.setEnabled(False)
        self.needles_lift_button.setEnabled(False)
        self.needles_lower_button.setEnabled(False)
        self._apply_needle_button_styles()
        if not self._needle_animation_timer.isActive():
            self._needle_animation_timer.start()

    def stop_needle_animation(self) -> None:
        for key in list(self._needle_targets.keys()):
            self._stop_needle_animation(key)

    def _stop_needle_animation(self, key: str) -> None:
        button = self._needle_targets.pop(key, None)
        base_text = self._needle_text.pop(key, None)
        if button is None:
            return
        button.setProperty("homing", False)
        button.setChecked(False)
        if base_text is not None:
            button.setText(base_text)
        if not self._needle_targets:
            self._needle_animation_timer.stop()
            self._needle_blink_dimmed = False
            self._update_enabled_state()
        self._apply_needle_button_styles()

    def _advance_needle_blink(self) -> None:
        if not self._needle_targets:
            return
        self._needle_blink_dimmed = not self._needle_blink_dimmed
        self._apply_needle_button_styles()
