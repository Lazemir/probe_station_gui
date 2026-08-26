from __future__ import annotations

import logging
import re
from PySide6.QtCore import Qt
from probe_station_gui.stage.exact_step import ExactStepClearReason
from probe_station_gui.views import (
    main_window_coordinate_step as coordinate_step,
    main_window_stage_position_panel as stage_position_panel_adapter,
)
from probe_station_gui.views.main_window_auxiliary import (
    show_serial_terminal_window as show_serial_terminal_tool_window,
)

logger = logging.getLogger("main")


class _MainManualJogMixin:
    def _on_manual_terminal_command(self, command: str) -> None:
        stripped = command.strip().upper()
        if not stripped:
            return
        coordinate_step.clear_exact_steps(
            self,
            ExactStepClearReason.MANUAL_TERMINAL_COMMAND,
        )
        self.stage_controller.invalidate_needles_state()
        self.stage_controller.invalidate_coordinate_confidence(
            "Manual controller command."
        )
        if re.match(r"^G5(?:4|5|6|7|8|9(?:\.[123])?)$", stripped):
            self.stage_controller.request_startup_sync(auto_home_a=False)
            self._schedule_cancel_state_refresh()
            return
        if (
            stripped.startswith("$#")
            or stripped.startswith("$G")
            or stripped.startswith("$10")
            or stripped.startswith("G10")
        ):
            self.stage_controller.request_startup_sync(auto_home_a=False)
            self._schedule_cancel_state_refresh()

    def show_joystick_window(self) -> None:
        if not self.joystick_panel or not self.joystick_dock:
            return
        self.joystick_dock.setVisible(True)
        self.joystick_dock.raise_()
        if self.joystick_dock.isFloating():
            self.joystick_dock.activateWindow()
        else:
            self.joystick_panel.setFocus(Qt.ActiveWindowFocusReason)

    def show_serial_terminal_window(self) -> None:
        show_serial_terminal_tool_window(self)

    def _on_manual_motion_axis(self, axis: str) -> None:
        axis_name = axis.upper()
        stage_position_panel_adapter.set_stage_motion_axes(self, {axis_name})

    def _save_manual_axis_jog_settings(
        self, axis: str, distance_mm: float, mode: str, feedrate_mm_min: float
    ) -> None:
        axis = axis.strip().upper()
        if axis not in {"X", "Y", "Z", "A", "B", "C"}:
            return
        mode = mode.strip().upper()
        if mode not in {"G90", "G91"}:
            return
        distance = max(0.001, float(distance_mm))
        feedrate = max(self.MIN_FEEDRATE_MM_MIN, float(feedrate_mm_min))
        settings = self.settings_manager.settings.clone()
        if (
            settings.jog.manual_axis == axis
            and abs(settings.jog.manual_axis_distance_mm - distance) <= 1e-9
            and settings.jog.manual_axis_mode == mode
            and abs(settings.jog.manual_axis_feedrate_mm_min - feedrate) <= 1e-9
        ):
            return
        settings.jog.manual_axis = axis
        settings.jog.manual_axis_distance_mm = distance
        settings.jog.manual_axis_mode = mode
        settings.jog.manual_axis_feedrate_mm_min = feedrate
        self.settings_manager.replace_and_save(
            settings,
            preserve_exposure_policy=True,
        )

    def _save_jog_control_mode(self, mode: str) -> None:
        control_mode = str(mode).strip().lower()
        if control_mode not in {"jog", "step"}:
            return
        if control_mode != "step":
            coordinate_step.clear_exact_steps(
                self,
                ExactStepClearReason.CONTROL_MODE_CHANGED,
            )
        settings = self.settings_manager.settings.clone()
        if settings.jog.mode == control_mode:
            return
        settings.jog.mode = control_mode
        self.settings_manager.replace_and_save(
            settings,
            preserve_exposure_policy=True,
        )

    def _save_jog_feedrate_setting(self, key: str, feedrate_mm_min: float) -> None:
        try:
            feedrate = max(self.MIN_FEEDRATE_MM_MIN, float(feedrate_mm_min))
        except (TypeError, ValueError):
            return
        settings = self.settings_manager.settings.clone()
        current = getattr(settings.jog, key, None)
        if current is not None and abs(float(current) - feedrate) <= 1e-9:
            return
        setattr(settings.jog, key, feedrate)
        self.settings_manager.replace_and_save(
            settings,
            preserve_exposure_policy=True,
        )

    def _on_step_feedrate_changed(self, feedrate_mm_min: float) -> None:
        self._save_jog_feedrate_setting("manual_axis_feedrate_mm_min", feedrate_mm_min)
        self._stage_motion.set_coordinate_feedrate(feedrate_mm_min)

    def _on_focus_feedrate_changed(self, feedrate_mm_min: float) -> None:
        self._save_jog_feedrate_setting("focus_feedrate_mm_min", feedrate_mm_min)
        self._stage_motion.set_coordinate_feedrate(feedrate_mm_min)

    def _on_focus_step_feedrate_changed(self, feedrate_mm_min: float) -> None:
        self._save_jog_feedrate_setting(
            "focus_step_feedrate_mm_min",
            feedrate_mm_min,
        )
        self._stage_motion.set_coordinate_feedrate(feedrate_mm_min)

    def _on_turntable_feedrate_changed(self, feedrate_mm_min: float) -> None:
        self._save_jog_feedrate_setting("turntable_feedrate_mm_min", feedrate_mm_min)
        self._stage_motion.set_coordinate_feedrate(feedrate_mm_min)

    def _on_turntable_step_feedrate_changed(self, feedrate_mm_min: float) -> None:
        self._save_jog_feedrate_setting(
            "turntable_step_feedrate_mm_min",
            feedrate_mm_min,
        )
        self._stage_motion.set_coordinate_feedrate(feedrate_mm_min)

    def _schedule_linear_feedrate_save(self, feedrate_mm_min: float) -> None:
        try:
            self._pending_linear_feedrate_default = max(
                self.MIN_FEEDRATE_MM_MIN,
                float(feedrate_mm_min),
            )
        except (TypeError, ValueError):
            return
        self._linear_feedrate_save_timer.start()

    def _on_linear_feedrate_changed(self, feedrate_mm_min: float) -> None:
        self._schedule_linear_feedrate_save(feedrate_mm_min)
        self._stage_motion.set_coordinate_feedrate(feedrate_mm_min)

    def _on_needle_feedrate_changed(self, feedrate_mm_min: float) -> None:
        try:
            feedrate = max(self.MIN_FEEDRATE_MM_MIN, float(feedrate_mm_min))
        except (TypeError, ValueError):
            return
        settings = self.settings_manager.settings.clone()
        if abs(settings.needle_calibration.feedrate_mm_min - feedrate) > 1e-9:
            settings.needle_calibration.feedrate_mm_min = feedrate
            self.settings_manager.replace_and_save(
                settings,
                preserve_exposure_policy=True,
            )
        self.stage_controller.queue_active_needles_feedrate(feedrate)

    def _on_needle_step_feedrate_changed(self, feedrate_mm_min: float) -> None:
        self._save_jog_feedrate_setting(
            "needles_step_feedrate_mm_min",
            feedrate_mm_min,
        )
        self._stage_motion.set_coordinate_feedrate(feedrate_mm_min)

    def _save_pending_linear_feedrate_default(self) -> None:
        feedrate = self._pending_linear_feedrate_default
        self._pending_linear_feedrate_default = None
        if feedrate is None:
            return
        settings = self.settings_manager.settings.clone()
        if abs(settings.feedrates.linear.default - feedrate) <= 1e-9:
            return
        settings.feedrates.linear.default = feedrate
        self.settings_manager.replace_and_save(
            settings,
            preserve_exposure_policy=True,
        )

    def _current_linear_feedrate(self) -> float:
        if self.joystick_panel is not None:
            return self.joystick_panel.current_linear_feedrate()
        return float(self.settings_manager.feedrate_configuration().linear.default)

    def _coordinate_feedrate_for_axes(self, axes: object) -> float:
        axes_tuple = tuple(str(axis).strip().upper() for axis in axes)
        selector = getattr(
            self.joystick_panel,
            "select_coordinate_feedrate_for_axes",
            None,
        )
        if callable(selector):
            try:
                return max(
                    self.MIN_FEEDRATE_MM_MIN,
                    float(selector(axes_tuple)),
                )
            except (TypeError, ValueError):
                logger.exception(
                    "Invalid coordinate feedrate selected for %s.", axes_tuple
                )
        return max(self.MIN_FEEDRATE_MM_MIN, float(self._current_linear_feedrate()))

    def _current_needle_feedrate(self) -> float:
        if self.joystick_panel is not None:
            return self.joystick_panel.current_needle_feedrate()
        return float(
            self.settings_manager.needle_calibration_configuration().feedrate_mm_min
        )
