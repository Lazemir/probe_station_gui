"""Dock and persistent panel construction for the main probe station window."""

from __future__ import annotations

import threading
from typing import Any, Protocol

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
)

from probe_station_gui.views.joystick_window import JoystickWindow
from probe_station_gui.views.alignment_panel import AlignmentPanel
from probe_station_gui.views.contact_oscillation_window import (
    ContactOscillationWindow,
)
from probe_station_gui.views.dock_widgets import CollapsibleDockWidget
from probe_station_gui.views.resistance_monitor_panel import ResistanceMonitorPanel
from probe_station_gui.views.serial_connection_panel import SerialConnectionPanel
from probe_station_gui.stage.exact_step import ExactStepClearReason
from probe_station_gui.views import main_window_connection_flow as connection_flow
from probe_station_gui.views import main_window_coordinate_step as coordinate_step
from probe_station_gui.views import main_window_homing as homing_ui
from probe_station_gui.views import (
    main_window_needle_calibration as needle_calibration_ui,
)
from probe_station_gui.views.main_window_auxiliary import (
    sync_contact_calibration_window_action,
    toggle_design_layout_window,
)


class MainWindowDockOwner(Protocol):
    serial_connection: Any
    stage_controller: Any
    lcr_controller: Any
    settings_manager: Any
    serial_connection_dialog: Any
    serial_connection_panel: Any
    resistance_panel: Any
    resistance_dock: Any
    joystick_panel: Any
    joystick_dock: Any
    contact_calibration_window: Any
    oscillation_panel: Any
    alignment_panel: Any
    alignment_dock: Any
    _stage_motion: Any

    def addDockWidget(self, area: Any, dock: Any) -> None: ...  # noqa: N802
    def splitDockWidget(self, first: Any, second: Any, orientation: Any) -> None: ...  # noqa: N802
    def resizeDocks(
        self, docks: list[Any], sizes: list[int], orientation: Any
    ) -> None: ...  # noqa: N802
    def _on_resistance_standby_enabled_changed(self, *args: Any) -> None: ...
    def _zero_b_axis(self, *args: Any) -> None: ...
    def _save_manual_axis_jog_settings(self, *args: Any) -> None: ...
    def _save_jog_control_mode(self, *args: Any) -> None: ...
    def _on_linear_feedrate_changed(self, *args: Any) -> None: ...
    def _on_step_feedrate_changed(self, *args: Any) -> None: ...
    def _on_focus_feedrate_changed(self, *args: Any) -> None: ...
    def _on_focus_step_feedrate_changed(self, *args: Any) -> None: ...
    def _on_needle_feedrate_changed(self, *args: Any) -> None: ...
    def _on_needle_step_feedrate_changed(self, *args: Any) -> None: ...
    def _on_turntable_feedrate_changed(self, *args: Any) -> None: ...
    def _on_turntable_step_feedrate_changed(self, *args: Any) -> None: ...
    def _on_manual_motion_axis(self, *args: Any) -> None: ...
    def _project_gui_relative_motion(self, *args: Any) -> Any: ...
    def _invalidate_design_registration(self, message: str) -> None: ...
    def _cancel_contact_seek(self, *args: Any) -> None: ...
    def _on_lcr_connection_changed(self, *args: Any) -> None: ...
    def _on_lcr_reading_started(self, *args: Any) -> None: ...
    def _on_lcr_reading_summary_updated(self, *args: Any) -> None: ...
    def _on_lcr_reading_updated(self, *args: Any) -> None: ...
    def _save_oscillation_configuration(self, *args: Any) -> None: ...
    def _request_alignment_capture(self, *args: Any) -> None: ...
    def _reset_alignment_capture_points(self, *args: Any) -> None: ...
    def _cancel_manual_alignment_pick(self, *args: Any) -> None: ...
    def _clear_design_registration(self, *args: Any) -> None: ...
    def _refresh_manual_alignment_ui(self) -> None: ...
    def _update_coordinate_display(self) -> None: ...
    def _refresh_design_panel(self) -> None: ...


def create_main_window_docks(owner: MainWindowDockOwner) -> None:
    """Create docks and persistent tool panels owned by the main window."""

    _create_serial_connection_dialog(owner)
    _create_resistance_dock(owner)
    _create_joystick_dock(owner)
    _create_contact_calibration_window(owner)
    _create_alignment_dock(owner)
    _refresh_created_docks(owner)


def _create_serial_connection_dialog(owner: MainWindowDockOwner) -> None:
    owner.serial_connection_dialog = QDialog(owner)
    owner.serial_connection_dialog.setWindowTitle("Connection")
    owner.serial_connection_dialog.setModal(False)
    owner.serial_connection_dialog.setMinimumWidth(420)
    owner.serial_connection_dialog.resize(640, 520)

    dialog_layout = QVBoxLayout(owner.serial_connection_dialog)
    dialog_layout.setContentsMargins(8, 8, 8, 8)
    dialog_layout.setSpacing(8)

    owner.serial_connection_panel = SerialConnectionPanel(
        owner.serial_connection_dialog
    )
    dialog_layout.addWidget(owner.serial_connection_panel)

    close_button_row = QHBoxLayout()
    close_button_row.addStretch(1)
    close_button = QPushButton("Close", owner.serial_connection_dialog)
    close_button.clicked.connect(owner.serial_connection_dialog.close)
    close_button_row.addWidget(close_button)
    dialog_layout.addLayout(close_button_row)

    owner.serial_connection_panel.connected.connect(
        lambda serial_port: connection_flow.on_serial_connected(owner, serial_port)
    )
    owner.serial_connection_panel.disconnected.connect(
        lambda: connection_flow.on_serial_disconnected(owner)
    )
    owner.serial_connection_panel.lcr_connect_requested.connect(
        owner.lcr_controller.request_connect
    )
    owner.serial_connection_panel.lcr_disconnect_requested.connect(
        lambda: connection_flow.request_lcr_disconnect(owner)
    )


def _create_resistance_dock(owner: MainWindowDockOwner) -> None:
    owner.resistance_panel = ResistanceMonitorPanel(owner)
    owner.resistance_panel.set_standby_enabled(
        owner.lcr_controller.live_polling_enabled()
    )
    owner.resistance_panel.standby_enabled_changed.connect(
        owner._on_resistance_standby_enabled_changed
    )
    owner.resistance_dock = CollapsibleDockWidget("Resistance", owner)
    owner.resistance_dock.setObjectName("ResistanceDock")
    owner.resistance_dock.setWidget(owner.resistance_panel)
    owner.resistance_dock.setAllowedAreas(
        Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
    )
    owner.addDockWidget(Qt.LeftDockWidgetArea, owner.resistance_dock)

    owner.lcr_controller.status_message.connect(
        owner.serial_connection_panel.set_lcr_status_message
    )
    owner.lcr_controller.status_message.connect(
        owner.resistance_panel.set_status_message
    )


def _create_joystick_dock(owner: MainWindowDockOwner) -> None:
    owner.joystick_panel = JoystickWindow(owner)
    owner.joystick_panel.set_stage_controller(owner.stage_controller)
    owner.joystick_panel.set_relative_motion_projector(
        owner._project_gui_relative_motion
    )
    connection_flow.apply_axis_feedrate_limits(
        owner,
        owner.stage_controller.axis_max_feedrates(),
    )
    _apply_joystick_settings(owner)
    _connect_joystick_panel(owner)
    _connect_stage_controller_to_joystick(owner)

    owner.joystick_dock = CollapsibleDockWidget("Joystick", owner)
    owner.joystick_dock.setObjectName("JoystickDock")
    owner.joystick_dock.setWidget(owner.joystick_panel)
    owner.joystick_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
    owner.addDockWidget(Qt.LeftDockWidgetArea, owner.joystick_dock)
    owner.splitDockWidget(owner.resistance_dock, owner.joystick_dock, Qt.Vertical)


def _apply_joystick_settings(owner: MainWindowDockOwner) -> None:
    feedrates = owner.settings_manager.feedrate_configuration()
    owner.joystick_panel.apply_feedrate_settings(
        feedrates.linear.presets,
        feedrates.linear.default,
        feedrates.rotary.presets,
        feedrates.rotary.default,
    )
    jog = owner.settings_manager.jog_configuration()
    owner.joystick_panel.apply_jog_settings(
        jog.linear_distance_mm,
        jog.rotary_distance_deg,
        jog.motion_safety_disabled,
        jog.manual_axis,
        jog.manual_axis_distance_mm,
        jog.manual_axis_mode,
        jog.manual_axis_feedrate_mm_min,
        jog.focus_feedrate_mm_min,
        jog.turntable_feedrate_mm_min,
        jog.mode,
        focus_step_feedrate_mm_min=jog.focus_step_feedrate_mm_min,
        needle_step_feedrate_mm_min=jog.needles_step_feedrate_mm_min,
        turntable_step_feedrate_mm_min=jog.turntable_step_feedrate_mm_min,
    )
    needle_settings = owner.settings_manager.needle_calibration_configuration()
    owner.joystick_panel.apply_needle_settings(needle_settings.feedrate_mm_min)
    owner.stage_controller.set_motion_safety_disabled(jog.motion_safety_disabled)
    owner.joystick_panel.set_serial(owner.serial_connection)


def _connect_joystick_panel(owner: MainWindowDockOwner) -> None:
    _connect_joystick_motion_actions(owner)
    _connect_joystick_feedrate_actions(owner)
    _connect_joystick_manual_motion(owner)
    owner.joystick_panel.reset_requested.connect(
        lambda: owner._invalidate_design_registration(
            "Design registration cleared after controller reset."
        )
    )
    owner.joystick_panel.reset_requested.connect(
        lambda: coordinate_step.clear_exact_steps(
            owner,
            ExactStepClearReason.CONTROLLER_RESET_REQUESTED,
        )
    )
    owner.joystick_panel.reset_requested.connect(
        lambda: owner.stage_controller.reset_controller(source="joystick_reset_button")
    )


def _connect_joystick_motion_actions(owner: MainWindowDockOwner) -> None:
    owner.joystick_panel.autofocus_requested.connect(
        owner.stage_controller.request_autofocus
    )
    owner.joystick_panel.home_axis_requested.connect(
        lambda axis: homing_ui.request_home_axis_from_ui(owner, axis)
    )
    owner.joystick_panel.home_all_requested.connect(
        lambda: homing_ui.request_home_all_from_ui(owner)
    )
    owner.joystick_panel.needles_raise_requested.connect(
        owner.stage_controller.request_needles_raise
    )
    owner.joystick_panel.needles_lift_requested.connect(
        owner.stage_controller.request_needles_lift
    )
    owner.joystick_panel.needles_lower_requested.connect(
        owner.stage_controller.request_needles_lower
    )
    owner.joystick_panel.needle_current_lower_contact_save_requested.connect(
        lambda: needle_calibration_ui.save_current_needle_height(owner)
    )
    owner.stage_controller.needle_height_save_finished.connect(
        lambda request_id, result: needle_calibration_ui.on_needle_height_save_finished(
            owner,
            request_id,
            result,
        )
    )
    owner.joystick_panel.needle_contact_coordinate_save_requested.connect(
        lambda action, a_coordinate: (
            needle_calibration_ui.save_needle_position_from_display_a_coordinate(
                owner,
                action,
                a_coordinate,
            )
        )
    )
    owner.joystick_panel.zero_b_requested.connect(owner._zero_b_axis)


def _connect_joystick_feedrate_actions(owner: MainWindowDockOwner) -> None:
    owner.joystick_panel.manual_axis_move_requested.connect(
        lambda *args: coordinate_step.on_manual_axis_move_requested(owner, *args)
    )
    owner.joystick_panel.manual_axis_settings_changed.connect(
        owner._save_manual_axis_jog_settings
    )
    owner.joystick_panel.control_mode_changed.connect(owner._save_jog_control_mode)
    owner.joystick_panel.linear_feedrate_changed.connect(
        owner._on_linear_feedrate_changed
    )
    owner.joystick_panel.common_feedrate_changed.connect(
        owner._stage_motion.set_coordinate_feedrate
    )
    owner.joystick_panel.step_feedrate_changed.connect(owner._on_step_feedrate_changed)
    owner.joystick_panel.focus_feedrate_changed.connect(
        owner._on_focus_feedrate_changed
    )
    owner.joystick_panel.focus_step_feedrate_changed.connect(
        owner._on_focus_step_feedrate_changed
    )
    owner.joystick_panel.needle_feedrate_changed.connect(
        owner._on_needle_feedrate_changed
    )
    owner.joystick_panel.needle_step_feedrate_changed.connect(
        owner._on_needle_step_feedrate_changed
    )
    owner.joystick_panel.turntable_feedrate_changed.connect(
        owner._on_turntable_feedrate_changed
    )
    owner.joystick_panel.turntable_step_feedrate_changed.connect(
        owner._on_turntable_step_feedrate_changed
    )


def _connect_joystick_manual_motion(owner: MainWindowDockOwner) -> None:
    owner.joystick_panel.motion_axis_requested.connect(owner._on_manual_motion_axis)
    owner.joystick_panel.jog_command_changed.connect(
        owner._stage_motion.on_manual_jog_command
    )
    owner.joystick_panel.jog_stopped.connect(owner._stage_motion.on_manual_jog_stopped)


def _connect_stage_controller_to_joystick(owner: MainWindowDockOwner) -> None:
    _connect_stage_homing_signals(owner)
    _connect_stage_limit_and_needle_signals(owner)
    owner.stage_controller.stage_position_changed.connect(
        lambda *args: connection_flow.persist_controller_state(owner, *args)
    )


def _connect_stage_homing_signals(owner: MainWindowDockOwner) -> None:
    owner.stage_controller.homing_status_changed.connect(
        owner.joystick_panel.set_homing_status
    )
    owner.stage_controller.homing_status_changed.connect(
        lambda *args: homing_ui.on_homing_status_changed(owner, *args)
    )
    owner.stage_controller.homing_status_changed.connect(
        lambda *args: connection_flow.persist_controller_state(owner, *args)
    )
    owner.stage_controller.homing_action_started.connect(
        owner.joystick_panel.set_homing_action_started
    )
    owner.stage_controller.homing_action_started.connect(
        lambda *args: homing_ui.on_homing_action_started(owner, *args)
    )
    owner.stage_controller.homing_action_finished.connect(
        owner.joystick_panel.set_homing_action_finished
    )
    owner.stage_controller.homing_action_finished.connect(
        lambda *args: homing_ui.on_homing_action_finished(owner, *args)
    )


def _connect_stage_limit_and_needle_signals(owner: MainWindowDockOwner) -> None:
    owner.stage_controller.limit_axes_changed.connect(
        lambda *args: homing_ui.on_limit_axes_changed(owner, *args)
    )
    owner.stage_controller.limit_axes_changed.connect(
        owner.joystick_panel.set_limit_axes
    )
    owner.stage_controller.axis_a_ready_changed.connect(
        owner.joystick_panel.set_axis_a_ready
    )
    owner.stage_controller.needles_state_changed.connect(
        owner.joystick_panel.set_needles_state
    )
    owner.stage_controller.needles_state_changed.connect(
        lambda *args: connection_flow.persist_controller_state(owner, *args)
    )
    owner.stage_controller.needles_zone_changed.connect(
        owner.joystick_panel.set_needles_zone
    )
    owner.stage_controller.needles_zone_changed.connect(
        lambda *args: connection_flow.persist_controller_state(owner, *args)
    )
    owner.stage_controller.needles_action_started.connect(
        owner.joystick_panel.set_needles_action_started
    )
    owner.stage_controller.needles_action_started.connect(
        lambda *args: homing_ui.on_needles_action_started(owner, *args)
    )
    owner.stage_controller.needles_action_finished.connect(
        owner.joystick_panel.set_needles_action_finished
    )
    owner.stage_controller.needles_action_finished.connect(
        lambda *args: homing_ui.on_needles_action_finished(owner, *args)
    )


def _create_contact_calibration_window(owner: MainWindowDockOwner) -> None:
    owner.contact_calibration_window = ContactOscillationWindow()
    owner.contact_calibration_window.visibility_changed.connect(
        lambda visible: sync_contact_calibration_window_action(owner, visible)
    )
    owner.contact_calibration_window.autofocus_requested.connect(
        owner.stage_controller.request_autofocus
    )
    owner.contact_calibration_window.save_surface_position_requested.connect(
        lambda target: needle_calibration_ui.save_surface_position(owner, target)
    )
    owner.contact_calibration_window.move_to_surface_position_requested.connect(
        lambda target: needle_calibration_ui.move_to_surface_position(owner, target)
    )
    owner.contact_calibration_window.contact_seek_requested.connect(
        lambda: needle_calibration_ui.request_contact_seek(
            owner,
            thread_factory=threading.Thread,
        )
    )
    owner.contact_calibration_window.contact_seek_cancel_requested.connect(
        owner._cancel_contact_seek
    )
    owner.lcr_controller.connection_changed.connect(owner._on_lcr_connection_changed)
    owner.lcr_controller.reading_started.connect(owner._on_lcr_reading_started)
    owner.lcr_controller.reading_summary_updated.connect(
        owner._on_lcr_reading_summary_updated
    )
    owner.lcr_controller.reading_updated.connect(owner._on_lcr_reading_updated)

    owner.oscillation_panel = owner.contact_calibration_window.oscillation_panel
    owner.oscillation_panel.start_requested.connect(
        owner.stage_controller.request_oscillation
    )
    owner.oscillation_panel.start_requested.connect(
        owner._save_oscillation_configuration
    )
    owner.oscillation_panel.stop_requested.connect(
        owner.stage_controller.request_stop_oscillation
    )
    owner.oscillation_panel.configuration_changed.connect(
        owner._save_oscillation_configuration
    )
    owner.joystick_dock.raise_()


def _create_alignment_dock(owner: MainWindowDockOwner) -> None:
    owner.alignment_panel = AlignmentPanel(owner)
    owner.alignment_panel.open_design_window_requested.connect(
        lambda: toggle_design_layout_window(owner, True)
    )
    owner.alignment_panel.capture_point_requested.connect(
        owner._request_alignment_capture
    )
    owner.alignment_panel.reset_points_requested.connect(
        owner._reset_alignment_capture_points
    )
    owner.alignment_panel.cancel_pick_requested.connect(
        owner._cancel_manual_alignment_pick
    )
    owner.alignment_panel.clear_registration_requested.connect(
        owner._clear_design_registration
    )
    owner.alignment_dock = CollapsibleDockWidget("Alignment", owner)
    owner.alignment_dock.setObjectName("AlignmentDock")
    owner.alignment_dock.setWidget(owner.alignment_panel)
    owner.alignment_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
    owner.addDockWidget(Qt.RightDockWidgetArea, owner.alignment_dock)
    owner.alignment_dock.hide()


def _refresh_created_docks(owner: MainWindowDockOwner) -> None:
    owner._refresh_manual_alignment_ui()
    owner._update_coordinate_display()
    owner._refresh_design_panel()
    owner.resizeDocks(
        [owner.resistance_dock, owner.joystick_dock],
        [130, 430],
        Qt.Vertical,
    )
    owner.resizeDocks(
        [owner.joystick_dock, owner.alignment_dock],
        [360, 520],
        Qt.Horizontal,
    )


__all__ = ["create_main_window_docks"]
