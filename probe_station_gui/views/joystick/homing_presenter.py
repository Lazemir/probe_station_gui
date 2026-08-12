"""Homing status and animation presentation for the joystick."""

from __future__ import annotations

from PySide6.QtWidgets import QPushButton


class JoystickHomingPresenterMixin:
    """Own homing actions, status presentation, and animation."""

    def set_homing_status(self, homed_axes: set[str]) -> None:
        active_axes = set(homed_axes).intersection(self.HOMING_AXES)
        self._homed_axes = set(active_axes)
        all_homed = active_axes == set(self.HOMING_AXES)
        for axis, button in self._homing_buttons.items():
            if axis in self._homing_targets:
                self._set_homing_button_state(
                    button,
                    axis in active_axes,
                    all_homed=all_homed,
                    limit=axis in self._limit_axes,
                    active=True,
                )
            elif axis in active_axes:
                self._set_homing_button_state(
                    button,
                    True,
                    all_homed=all_homed,
                    limit=axis in self._limit_axes,
                )
            else:
                self._set_homing_button_state(
                    button,
                    False,
                    all_homed=all_homed,
                    limit=axis in self._limit_axes,
                    pending=axis in self._pending_homing_axes,
                )
        all_pending = bool(self._pending_homing_axes)
        if "ALL" in self._homing_targets:
            self._set_homing_button_state(
                self.home_all_button,
                all_homed,
                all_homed=all_homed,
                active=True,
            )
        elif all_homed:
            self._set_homing_button_state(self.home_all_button, True, all_homed=True)
        else:
            self._set_homing_button_state(
                self.home_all_button,
                False,
                all_homed=False,
                pending=all_pending,
            )

    def _set_homing_button_state(
        self,
        button: QPushButton,
        homed: bool,
        *,
        all_homed: bool = False,
        limit: bool = False,
        pending: bool = False,
        active: bool = False,
    ) -> None:
        button.setProperty("homing", active)
        button.setProperty("all_homed", all_homed)
        button.setProperty("limit", limit)
        button.setProperty("pending", pending)
        button.setChecked(active)
        if active:
            button.setStyleSheet(
                self.HOMING_ACTIVE_DIM_STYLE
                if self._homing_blink_dimmed
                else self.HOMING_ACTIVE_STYLE
            )
        elif pending:
            button.setStyleSheet(self.HOMING_PENDING_STYLE)
        elif limit:
            button.setStyleSheet(self.LIMIT_STYLE)
        elif all_homed:
            button.setStyleSheet(self.ALL_HOMED_STYLE)
        else:
            button.setStyleSheet(self.HOMED_STYLE if homed else self.NOT_HOMED_STYLE)

    def set_pending_homing_actions(self, axes: object) -> None:
        if isinstance(axes, (set, list, tuple)):
            self._pending_homing_axes = {
                str(axis).strip().upper()
                for axis in axes
                if str(axis).strip().upper() in self.HOMING_AXES
            }
        else:
            self._pending_homing_axes = set()
        self.set_homing_status(set(self._homed_axes))

    def set_limit_axes(self, axes: object) -> None:
        if isinstance(axes, (set, list, tuple)):
            self._limit_axes = {
                str(axis).strip().upper()
                for axis in axes
                if str(axis).strip().upper() in self.HOMING_AXES
            }
        else:
            self._limit_axes = set()
        self.set_homing_status(set(self._homed_axes))

    def _home_all(self) -> None:
        self.home_all_requested.emit()

    def _home_axis(self, axis: str) -> None:
        self.home_axis_requested.emit(axis)

    def set_homing_action_started(self, axis_key: str) -> None:
        axis_key = axis_key.strip().upper()
        if axis_key == "ALL":
            self._start_homing_animation("ALL", self.home_all_button)
            return
        button = self._homing_buttons.get(axis_key)
        if button is not None:
            self._start_homing_animation(axis_key, button)

    def set_homing_action_finished(
        self, success: bool, message: str, axis_key: str
    ) -> None:
        axis_key = axis_key.strip().upper()
        if axis_key == "ALL":
            self._stop_homing_animation("ALL")
        else:
            self._stop_homing_animation(axis_key)
        if not success:
            self._show_warning(message)

    def _start_homing_animation(self, key: str, button: QPushButton) -> None:
        if key in self._homing_targets:
            return
        base_text = button.text()
        self._homing_targets[key] = button
        self._homing_text[key] = base_text
        button.setProperty("homing", True)
        button.setChecked(True)
        self.set_homing_status(set(self._homed_axes))
        self._advance_homing_spinner()
        if not self._homing_animation_timer.isActive():
            self._homing_animation_timer.start()

    def _stop_homing_animation(self, key: str) -> None:
        button = self._homing_targets.pop(key, None)
        base_text = self._homing_text.pop(key, None)
        overlay = self._homing_overlays.pop(key, None)
        if button is None:
            return
        button.setProperty("homing", False)
        button.setChecked(False)
        if not self._homing_targets:
            button.setEnabled(True)
        if base_text is not None:
            button.setText(base_text)
        if overlay is not None:
            overlay.hide()
            overlay.deleteLater()
        button.setEnabled(True)
        if not self._homing_targets:
            self._homing_animation_timer.stop()
            self._homing_spinner_angle = 0
            self._homing_blink_dimmed = False
        self.set_homing_status(set(self._homed_axes))

    def _advance_homing_spinner(self) -> None:
        if not self._homing_targets:
            return
        self._homing_spinner_angle = (self._homing_spinner_angle + 30) % 360
        self._homing_blink_dimmed = not self._homing_blink_dimmed
        self.set_homing_status(set(self._homed_axes))
