from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from probe_station_gui.coordinates.coordinator_model import (
    CoordinateMotionLease,
    CoordinateMotionProjection,
)
from probe_station_gui.coordinates.rotation_geometry import (
    RotationGeometrySnapshot,
    rotation_geometry_snapshot,
)
from probe_station_gui.design import objective_offsets as offsets
from probe_station_gui.design.model import DesignModelError
from probe_station_gui.settings.manager import ordered_objective_names
from probe_station_gui.application.stage_motion_types import StageMotionActionState
from probe_station_gui.shared.wheel_guard import GuardedComboBox as QComboBox
from probe_station_gui.stage.exact_step import ExactStepClearReason
from probe_station_gui.views import main_window_homing as homing_ui
from probe_station_gui.views import main_window_coordinate_step as coordinate_step
from probe_station_gui.views import main_window_coordinate_motion as coordinate_motion
from probe_station_gui.views import (
    main_window_stage_position_panel as stage_position_panel_adapter,
)

logger = logging.getLogger("main")


class _MainStatusCoordinateUiMixin:
    def _schedule_status_refreshes(self, delays_ms: tuple[int, ...]) -> None:
        for delay_ms in delays_ms:
            QTimer.singleShot(delay_ms, self.stage_controller.request_status_refresh)

    def _on_stage_task_started(self) -> None:
        self._show_status("Moving stage...")
        self._update_stage_coordinate_apply_state()

    def _show_route_runtime_status(self, message: str, timeout_ms: int = 0) -> None:
        self._show_status(message, timeout_ms)
        self._route_runtime_presenter().set_status(message)

    def _show_route_dialog_status(self, message: str, timeout_ms: int = 0) -> None:
        self._show_status(message, timeout_ms)
        dialog = getattr(self, "_route_measurement_dialog", None)
        if dialog is not None:
            dialog.set_status(message)

    def _create_objective_widget(self) -> QWidget:
        widget = QWidget(self)
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        label = QLabel("Objective:", widget)
        label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(label)
        self._objective_combo = QComboBox(widget)
        for name in self._objective_names():
            self._objective_combo.addItem(name, name)
        self._objective_combo.setToolTip(
            "Select the installed microscope objective. Click-to-move and autofocus use this profile."
        )
        self._objective_combo.currentIndexChanged.connect(
            self._on_objective_combo_changed
        )
        layout.addWidget(self._objective_combo)
        return widget

    def _objective_names(self) -> list[str]:
        settings = self.settings_manager.objectives_configuration()
        return ordered_objective_names(settings.objectives)

    def _active_objective_xy_offset(self) -> tuple[float, float]:
        settings = self.settings_manager.objectives_configuration()
        return offsets.objective_xy_offset(settings.objectives, settings.active_name)

    def _camera_stage_xy_from_raw_stage_xy(
        self, raw_stage_xy: tuple[float, float]
    ) -> tuple[float, float]:
        return offsets.raw_stage_to_camera_stage(
            raw_stage_xy, self._active_objective_xy_offset()
        )

    def _raw_stage_xy_from_camera_stage_xy(
        self, camera_stage_xy: tuple[float, float]
    ) -> tuple[float, float]:
        return offsets.camera_stage_to_raw_stage(
            camera_stage_xy, self._active_objective_xy_offset()
        )

    def _rotation_geometry_snapshot(self) -> RotationGeometrySnapshot:
        try:
            software_coordinates = self.settings_manager.settings.software_coordinates
            return rotation_geometry_snapshot(software_coordinates)
        except (AttributeError, TypeError, ValueError) as exc:
            raise DesignModelError(str(exc)) from exc

    def _design_navigation_xy_from_physical_machine_xy(
        self,
        machine_xy: tuple[float, float],
    ) -> tuple[float, float]:
        snapshot = self.stage_controller.latest_machine_coordinate_snapshot()
        if snapshot is None:
            raise DesignModelError(
                "A synchronized Machine-coordinate snapshot is unavailable."
            )
        configured_xy = (
            snapshot.physical_machine_to_configured_controller("X", machine_xy[0]),
            snapshot.physical_machine_to_configured_controller("Y", machine_xy[1]),
        )
        return self._camera_stage_xy_from_raw_stage_xy(configured_xy)

    def _design_xy_from_raw_stage_xy(
        self, raw_stage_xy: tuple[float, float]
    ) -> tuple[float, float] | None:
        lease = self._coordinate_system_coordinator.current_design_lease()
        return self._coordinate_system_coordinator.project_raw_stage_to_design(
            lease,
            raw_stage_xy,
        )

    def _raw_stage_xy_from_design_xy(
        self, design_xy: tuple[float, float]
    ) -> tuple[float, float] | None:
        lease = self._coordinate_system_coordinator.current_design_lease()
        return self._coordinate_system_coordinator.project_design_to_raw_stage(
            lease,
            design_xy,
        )

    def _project_gui_coordinate_motion(
        self,
        axis_values: tuple[tuple[str, float], ...],
        *,
        mode: str,
        lease: CoordinateMotionLease | None = None,
        allow_pose_rebase: bool = False,
    ) -> CoordinateMotionProjection | None:
        return coordinate_motion.project_gui_coordinate_motion(
            self,
            axis_values,
            mode=mode,
            lease=lease,
            allow_pose_rebase=allow_pose_rebase,
        )

    def _project_gui_relative_motion(
        self,
        requested_distances: tuple[tuple[str, float], ...],
        lease: object | None,
    ) -> CoordinateMotionProjection:
        return coordinate_motion.project_gui_relative_motion(
            self,
            requested_distances,
            lease,
        )

    def _on_stage_axis_escape_pressed(self, axis_name: str) -> None:
        panel = getattr(self, "_stage_position_panel", None)
        if panel is None:
            return
        axis = axis_name.strip().upper()
        panel.discard_return_commit(axis)
        panel.pop_pending_target(axis)
        self._stage_motion.pop_pending_coordinate_edit(axis)
        panel.reset_axis_field(axis, self._stage_axis_display_values.get(axis))
        field = panel.field(axis)
        if field is not None:
            field.deselect()
            field.clearFocus()
        stage_position_panel_adapter.refresh_stage_axis_styles(self)
        self._update_stage_coordinate_apply_state()
        self.view.setFocus(Qt.OtherFocusReason)

    def _on_stage_coordinate_mode_changed(self) -> None:
        coordinate_step.clear_exact_steps(
            self,
            ExactStepClearReason.COORDINATE_MODE_CHANGED,
        )
        had_pending = self._stage_motion.clear_pending_coordinate_edits()
        if had_pending and self._stage_position_panel is not None:
            self._stage_position_panel.clear_pending_target_state()
            stage_position_panel_adapter.update_stage_position_display(
                self,
                self.stage_controller.latest_stage_position(),
            )
            stage_position_panel_adapter.refresh_coordinate_frame_display(self)
            self._show_status(
                "Cleared pending coordinate edits after input mode change.", 2000
            )
        self._update_stage_coordinate_apply_state()

    def _on_software_coordinate_system_changed(self, frame_id: str) -> None:
        joystick = getattr(self, "joystick_panel", None)
        cancel_jog_input = getattr(joystick, "cancel_jog_input", None)
        if callable(cancel_jog_input):
            cancel_jog_input()
        coordinate_step.clear_exact_steps(
            self,
            ExactStepClearReason.COORDINATE_MODE_CHANGED,
        )
        self._stage_motion.clear_pending_coordinate_edits()
        panel = getattr(self, "_stage_position_panel", None)
        if panel is not None:
            panel.clear_pending_targets(self._stage_axis_display_values)
        stage_position_panel_adapter.select_gui_coordinate_frame(self, frame_id)

    def _refresh_software_coordinate_display(self) -> None:
        stage_position_panel_adapter.refresh_coordinate_frame_display(self)

    def _surface_map_capture_running(self) -> bool:
        window = self.surface_map_window
        if window is None or not hasattr(window, "is_capture_running"):
            return False
        try:
            return bool(window.is_capture_running())
        except Exception:
            return False

    def _microscope_scan_running(self) -> bool:
        thread = getattr(self, "_microscope_scan_thread", None)
        return thread is not None and thread.is_alive()

    def _has_application_cancelable_operation(self) -> bool:
        route_contact_thread = getattr(self, "_route_contact_move_thread", None)
        return bool(
            (route_contact_thread is not None and route_contact_thread.is_alive())
            or self._route_run_execution.snapshot().thread_alive
            or self._surface_map_capture_running()
            or self._microscope_scan_running()
            or self._sample_handling_active()
            or self._manual_alignment_pick_slot is not None
            or self._microscope_interaction.has_pending_move
            or self._pending_homing_axes
            or self._homing_active_key is not None
            or self._stage_motion.alignment_rotation_pending()
        )

    def _cancel_stage_coordinate_action(self, *, focus_reason: object) -> None:
        cancelled_any = self._cancel_pending_stage_ui_intents()
        cancelled_any = self._cancel_route_measurement_for_stage_action() or cancelled_any
        cancelled_any = self._cancel_background_stage_operations() or cancelled_any
        outcome = self._stage_motion.cancel_stage_motion()
        if outcome.stage_motion_cancelled:
            stage_position_panel_adapter.clear_stage_motion_axes(self)
        panel = getattr(self, "_stage_position_panel", None)
        if outcome.pending_edits_cleared and panel is not None:
            panel.clear_pending_targets(self._stage_axis_display_values)
        if outcome.coordinate_priority:
            self.view.setFocus(focus_reason)
            self._schedule_cancel_state_refresh()
            return
        cancelled_any = outcome.stage_motion_cancelled or cancelled_any
        if outcome.pending_edits_cleared:
            self.view.setFocus(focus_reason)
        if cancelled_any:
            self.view.setFocus(focus_reason)
            self._show_status("Cancel requested.", 3000)
            self._schedule_cancel_state_refresh()
            return
        if outcome.pending_edits_cleared:
            self._show_status("Cleared pending coordinate edits.", 2000)
            self._schedule_cancel_state_refresh()

    def _cancel_pending_stage_ui_intents(self) -> bool:
        cancelled_any = False
        if self._microscope_interaction.has_pending_move:
            self._microscope_interaction.cancel_pending(clear_target=True)
            cancelled_any = True
        if self._manual_alignment_pick_slot is not None:
            self._cancel_manual_alignment_pick()
            cancelled_any = True
        if self._stage_motion.discard_alignment_rotation():
            cancelled_any = True
        if self._pending_homing_axes or self._homing_active_key is not None:
            homing_ui.clear_pending_homing_queue(self)
            cancelled_any = True
        return cancelled_any

    def _cancel_route_measurement_for_stage_action(self) -> bool:
        runner = self._route_run_execution.snapshot().runner
        if runner is None:
            return False
        runner.stop()
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_waiting(False)
            self.design_navigator_panel.set_route_measurement_status(
                "Route measurement cancel requested."
            )
        return True

    def _cancel_background_stage_operations(self) -> bool:
        cancelled_any = False
        if self._surface_map_capture_running():
            try:
                self.surface_map_window.stop_capture()
            except Exception:
                logger.exception("Failed to stop surface map capture from Cancel.")
            cancelled_any = True
        if self._microscope_scan_running():
            self._microscope_scan_stop_requested.set()
            if self.microscope_scan_dialog is not None:
                self.microscope_scan_dialog.set_status(
                    "Microscope scan stop requested."
                )
            cancelled_any = True
        return cancelled_any

    def _controller_reports_active_motion(self) -> bool:
        return bool(self._stage_motion.snapshot().reported_active_motion)

    def _controller_latest_state_blocks_motion(self) -> bool:
        if not hasattr(self, "stage_controller"):
            return False
        state = (self.stage_controller.latest_stage_state() or "").strip().lower()
        if state in {"", "idle"}:
            return False
        if state in {"run", "jog"}:
            return bool(self._stage_motion.snapshot().reported_active_motion)
        return True

    def _schedule_cancel_state_refresh(self) -> None:
        for delay_ms in (0, 100, 300, 1000, 2500):
            QTimer.singleShot(delay_ms, self._update_stage_coordinate_apply_state)

    def _update_stage_coordinate_apply_state(
        self, action_state: StageMotionActionState | None = None
    ) -> None:
        panel = getattr(self, "_stage_position_panel", None)
        if panel is None:
            return
        stage_position_panel_adapter.apply_coordinate_common_feedrate(self)
        snapshot = self._stage_motion.snapshot()
        motion_active = bool(getattr(snapshot, "cancelable", False))
        session_cancelable = motion_active
        if isinstance(action_state, StageMotionActionState):
            session_cancelable = action_state.cancelable
            coordinate_active = action_state.coordinate_active
        else:
            coordinate_active = snapshot.coordinate_active
        active = coordinate_active or motion_active
        available = panel.has_pending_or_modified_fields()
        panel.set_action_buttons_enabled(
            available
            and not active
            and stage_position_panel_adapter.gui_coordinate_motion_editing_enabled(
                self
            ),
            available
            or session_cancelable
            or self._has_application_cancelable_operation(),
        )

    def _append_status_log(self, message: str) -> None:
        if not message:
            return
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{timestamp} {message}\n"
        path: Path = self._status_log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError as exc:
            logger.warning("Failed to write status log: %s", exc)

    def _open_status_log(self) -> None:
        path: Path = self._status_log_path
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _prime_keyboard_focus(self) -> None:
        if not self.isVisible():
            return
        self.raise_()
        self.activateWindow()
        self.view.setFocus(Qt.ActiveWindowFocusReason)
