from __future__ import annotations

import logging
import math
import time
from PySide6.QtCore import QTimer, Qt
from probe_station_gui.stage import move_lifecycle as stage_move_lifecycle
from probe_station_gui.stage import position_update as stage_position_update
from probe_station_gui.views import (
    main_window_coordinate_motion as coordinate_motion,
    main_window_coordinate_step as coordinate_step,
    main_window_stage_position_panel as stage_position_panel_adapter,
)
from probe_station_gui.views.main_window_auxiliary import (
    show_serial_terminal_window as show_serial_terminal_tool_window,
)

logger = logging.getLogger("main")


class _MainManualJogMixin:
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

    def _on_manual_jog_command_changed(
        self, commanded_distances: object, feedrate: float
    ) -> None:
        if not isinstance(commanded_distances, tuple):
            return
        self._clear_exact_step_targets()
        self._stage_motion.cancel_planned_xy_move()
        if self.serial_terminal_panel is not None:
            self.serial_terminal_panel.set_live_poll_paused(True)
        result = self._manual_jog_prediction.handle_command(
            commanded_distances,
            feedrate=feedrate,
            now=time.monotonic(),
            coordinate_move_active=self._coordinate_targets.has_active_move(),
            coordinate_move_stage_position=self._coordinate_targets.stage_position,
            latest_stage_position=self.stage_controller.latest_stage_position(),
            current_design_stage_xy=self._current_design_stage_xy,
        )
        if result.zero_distance:
            logger.debug(
                "MOTION PREDICTION stop_requested command=%s", commanded_distances
            )
            self._manual_jog_timer.stop()
            stage_position_panel_adapter.clear_stage_motion_axes(self)
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
            return
        if result.clear_coordinate_move_tracking:
            logger.debug(
                "Coordinate move tracking cleared after manual jog command: %s",
                commanded_distances,
            )
            stage_move_lifecycle.clear_coordinate_move_tracking(
                self,
                clear_pending=False,
                reset_override=False,
            )
        stage_position_panel_adapter.set_stage_motion_axes(
            self,
            set(result.motion_axes),
        )
        prediction = self._manual_jog_prediction
        design_stage_xy = (
            self._design_xy_from_raw_stage_xy(prediction.stage_xy)
            if prediction.stage_xy is not None
            else None
        )
        velocity_x, velocity_y = prediction.velocity_xy or (0.0, 0.0)
        logger.debug(
            "MOTION PREDICTION start stage=%s design=%s velocity=(%.4f, %.4f) feedrate=%.3f command=%s source=%s",
            self._format_optional_point(prediction.stage_xy),
            self._format_optional_point(design_stage_xy),
            velocity_x,
            velocity_y,
            float(feedrate),
            commanded_distances,
            result.stage_source,
        )
        if result.publish_position is not None:
            stage_position_update.publish_stage_position_estimate(
                self,
                result.publish_position,
            )
        if result.start_timer and not self._manual_jog_timer.isActive():
            self._manual_jog_timer.start()

    def _on_manual_jog_stopped(self) -> None:
        prediction = self._manual_jog_prediction
        result = prediction.handle_stop(
            now=time.monotonic(),
            last_status_timestamp=self.stage_controller.last_status_timestamp(),
        )
        design_stage_xy = (
            self._design_xy_from_raw_stage_xy(prediction.stage_xy)
            if prediction.stage_xy is not None
            else None
        )
        logger.debug(
            "MOTION PREDICTION stop_requested stage=%s design=%s stop_tail_s=%.4f",
            self._format_optional_point(prediction.stage_xy),
            self._format_optional_point(design_stage_xy),
            result.stop_tail_s,
        )
        if result.start_timer and not self._manual_jog_timer.isActive():
            self._manual_jog_timer.start()
        if result.stop_timer:
            self._manual_jog_timer.stop()
        if self.serial_terminal_panel is not None and result.resume_live_poll:
            QTimer.singleShot(
                self.TERMINAL_RESUME_AFTER_JOG_MS,
                lambda: (
                    self.serial_terminal_panel
                    and self.serial_terminal_panel.set_live_poll_paused(False)
                ),
            )
        if result.schedule_status_refreshes:
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)

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
            self._clear_exact_step_targets()
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
        self._apply_coordinate_move_feedrate(feedrate_mm_min)

    def _on_focus_feedrate_changed(self, feedrate_mm_min: float) -> None:
        self._save_jog_feedrate_setting("focus_feedrate_mm_min", feedrate_mm_min)
        self._apply_coordinate_move_feedrate(feedrate_mm_min)

    def _on_focus_step_feedrate_changed(self, feedrate_mm_min: float) -> None:
        self._save_jog_feedrate_setting(
            "focus_step_feedrate_mm_min",
            feedrate_mm_min,
        )
        self._apply_coordinate_move_feedrate(feedrate_mm_min)

    def _on_turntable_feedrate_changed(self, feedrate_mm_min: float) -> None:
        self._save_jog_feedrate_setting("turntable_feedrate_mm_min", feedrate_mm_min)
        self._apply_coordinate_move_feedrate(feedrate_mm_min)

    def _on_turntable_step_feedrate_changed(self, feedrate_mm_min: float) -> None:
        self._save_jog_feedrate_setting(
            "turntable_step_feedrate_mm_min",
            feedrate_mm_min,
        )
        self._apply_coordinate_move_feedrate(feedrate_mm_min)

    def _on_manual_axis_move_requested(
        self,
        axis: str,
        value_mm: float,
        mode: str,
        feedrate_mm_min: float,
    ) -> None:
        coordinate_step.on_manual_axis_move_requested(
            self,
            axis,
            value_mm,
            mode,
            feedrate_mm_min,
        )

    def _on_exact_step_window_elapsed(self) -> None:
        self._exact_step_window_elapsed = True
        self._dispatch_exact_step_targets()

    def _dispatch_exact_step_targets(self) -> bool:
        return coordinate_step.dispatch_exact_step_targets(self)

    def _on_coordinate_move_finished(
        self,
        success: bool,
        finished_display_targets: dict[str, float],
        finished_display_basis: object | None,
    ) -> None:
        if not success:
            self._clear_exact_step_targets()
            return
        accumulator = getattr(self, "_exact_step_accumulator", None)
        if accumulator is None:
            return
        queued_display_basis = coordinate_motion.motion_basis(
            getattr(self, "_exact_step_motion_lease", None)
        )
        if finished_display_basis != queued_display_basis:
            stage_position_panel_adapter.refresh_stage_axis_styles(self)
            self._update_stage_coordinate_apply_state()
            if not self._exact_step_timer.isActive():
                self._dispatch_exact_step_targets()
            return
        for axis, reached_target in finished_display_targets.items():
            pending_target = accumulator.targets.get(axis)
            if pending_target is None or not math.isclose(
                pending_target,
                reached_target,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                continue
            accumulator.targets.pop(axis, None)
            self._exact_step_pending_axes.discard(axis)
            self._pending_stage_axis_targets.pop(axis, None)
        stage_position_panel_adapter.refresh_stage_axis_styles(self)
        self._update_stage_coordinate_apply_state()
        if not self._exact_step_timer.isActive():
            self._dispatch_exact_step_targets()

    def _clear_exact_step_targets(self) -> None:
        timer = getattr(self, "_exact_step_timer", None)
        if timer is not None:
            timer.stop()
        accumulator = getattr(self, "_exact_step_accumulator", None)
        if accumulator is not None:
            accumulator.clear()
        for axis in getattr(self, "_exact_step_pending_axes", set()):
            self._pending_stage_axis_targets.pop(axis, None)
        self._exact_step_pending_axes = set()
        self._exact_step_motion_lease = None
        self._exact_step_pose_rebase_allowed = False
        self._exact_step_window_elapsed = False
        if (
            getattr(self, "_stage_position_panel", None) is not None
            and hasattr(self, "_stage_motion_axes")
            and hasattr(self, "_stage_motion_blink_dimmed")
        ):
            stage_position_panel_adapter.refresh_stage_axis_styles(self)
            self._update_stage_coordinate_apply_state()

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
        self._apply_coordinate_move_feedrate(feedrate_mm_min)

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
        self._apply_coordinate_move_feedrate(feedrate_mm_min)

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
