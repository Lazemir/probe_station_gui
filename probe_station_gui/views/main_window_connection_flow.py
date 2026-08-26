"""Main-window serial and controller connection flow helpers."""

from __future__ import annotations

import logging

from PySide6.QtCore import QTimer

from probe_station_gui.stage.types import StageMotionResetReason
from probe_station_gui.views import main_window_coordinate_flow as coordinate_flow
from probe_station_gui.views import main_window_design_workspace as design_workspace
from probe_station_gui.views import main_window_homing as homing_ui
from probe_station_gui.views import (
    main_window_stage_position_panel as stage_position_panel,
)


logger = logging.getLogger(__name__)


def serial_baud_rate(serial_port: object) -> int:
    try:
        return int(getattr(serial_port, "baudrate"))
    except TypeError:
        return int(float(getattr(serial_port, "baudrate")))


def on_serial_connected(owner: object, serial_port: object) -> None:
    if owner.serial_connection and owner.serial_connection.is_open:
        owner.serial_connection.close()
    owner.serial_connection = serial_port
    owner.serial_port_name = serial_port.port
    owner.serial_baud_rate = serial_baud_rate(serial_port)
    logger.info(
        "Serial connected: %s @ %s baud",
        owner.serial_connection.port,
        owner.serial_connection.baudrate,
    )
    owner._stage_unhomed_display_origins.clear()
    owner._stage_motion.reset(StageMotionResetReason.CONNECTION_CHANGED)
    cached_state = owner.settings_manager.load_controller_state()
    owner._controller_state_persistence_suspended = True
    try:
        owner.stage_controller.set_serial(owner.serial_connection)
    finally:
        owner._controller_state_persistence_suspended = False
    restore_persisted_controller_state(
        owner,
        cached_state,
        cache_already_loaded=True,
    )
    coordinate_flow.activate_current_design(owner)
    if owner.joystick_panel and owner.joystick_dock:
        owner.joystick_panel.set_serial(owner.serial_connection)
        owner.joystick_dock.setVisible(True)
        owner.joystick_dock.raise_()
        if owner.joystick_dock.isFloating():
            owner.joystick_dock.activateWindow()
    if owner.serial_terminal_panel:
        owner.serial_terminal_panel.set_serial(owner.serial_connection)
    QTimer.singleShot(0, lambda: run_serial_startup_sync(owner))
    owner._refresh_design_position()


def on_serial_disconnected(owner: object) -> None:
    from probe_station_gui.views.main_window_shutdown import (
        stop_jog_before_serial_close,
    )

    stop_jog_before_serial_close(owner, "serial disconnect")
    if owner.serial_connection and owner.serial_connection.is_open:
        owner.serial_connection.close()
    owner.serial_connection = None
    persist_serial_connection_state(owner, False)
    owner._stage_unhomed_display_origins.clear()
    owner.stage_controller.request_stop_oscillation()
    owner._controller_state_persistence_suspended = True
    try:
        owner.stage_controller.set_serial(None)
    finally:
        owner._controller_state_persistence_suspended = False
    owner._stage_motion.reset(StageMotionResetReason.DISCONNECT)
    owner._controller_reboot_recovery_scheduled = False
    homing_ui.clear_pending_homing_queue(owner)
    stage_position_panel.clear_stage_motion_axes(owner)
    owner._update_stage_coordinate_apply_state()
    logger.info("Serial disconnected")
    stage_position_panel.update_stage_position_display(owner, None)
    auto_retry = owner.sender() is not owner.serial_connection_panel
    if owner.serial_connection_panel:
        owner.serial_connection_panel.handle_external_disconnect(auto_retry=auto_retry)
    if owner.joystick_panel:
        owner.joystick_panel.set_serial(None)
    if owner.serial_terminal_panel:
        owner.serial_terminal_panel.set_serial(None)
    if owner.contact_calibration_window is not None:
        owner.contact_calibration_window.set_current_stage_position(None)
        owner.contact_calibration_window.set_current_needle_lowering(None)
    if owner.oscillation_panel:
        owner.oscillation_panel.set_running(False, "")
    owner._reset_manual_alignment(cancel_pick=True)
    coordinate_flow.activate_current_design(owner)
    owner._update_design_position(None)


def auto_connect_if_possible(owner: object) -> None:
    if owner.serial_connection_panel and not owner.serial_connection:
        if owner.settings_manager.serial_auto_connect_enabled():
            logger.debug(
                "Attempting serial auto-connect because previous session closed connected"
            )
            owner.serial_connection_panel.auto_connect()
        else:
            logger.debug(
                "Skipping serial auto-connect because previous session was disconnected"
            )
    if owner.lcr_controller is not None and not owner.lcr_controller.is_connected():
        if owner.settings_manager.meter_auto_connect_enabled():
            logger.debug(
                "Attempting measurement-instrument auto-connect because "
                "previous session closed connected"
            )
            owner.lcr_controller.request_connect()
        else:
            logger.debug(
                "Skipping measurement-instrument auto-connect because previous "
                "session was disconnected"
            )


def run_serial_startup_sync(owner: object) -> None:
    if owner.serial_connection is None or not owner.serial_connection.is_open:
        return
    owner.stage_controller.request_startup_sync(
        auto_home_a=True,
        clear_unverified_state=False,
    )
    owner._schedule_cancel_state_refresh()


def apply_axis_feedrate_limits(owner: object, rates: object) -> None:
    if not isinstance(rates, dict):
        return
    owner.stage_controller.apply_axis_max_feedrates(rates)
    applied_rates = owner.stage_controller.axis_max_feedrates()
    if owner.joystick_panel is not None:
        owner.joystick_panel.set_axis_feedrate_limits(applied_rates)


def on_axis_max_feedrates_changed(owner: object, rates: object) -> None:
    apply_axis_feedrate_limits(owner, rates)
    if owner.stage_controller.axis_max_feedrates():
        apply_joystick_feedrate_preferences(owner)


def apply_joystick_feedrate_preferences(owner: object) -> None:
    if owner.joystick_panel is None:
        return
    needle_settings = owner.settings_manager.needle_calibration_configuration()
    feedrates = owner.settings_manager.feedrate_configuration()
    owner.joystick_panel.apply_feedrate_settings(
        feedrates.linear.presets,
        feedrates.linear.default,
        feedrates.rotary.presets,
        feedrates.rotary.default,
    )
    owner.joystick_panel.apply_needle_settings(needle_settings.feedrate_mm_min)
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
    logger.debug(
        "Joystick jog settings reapplied: mode=%s linear_distance_mm=%s "
        "rotary_distance_deg=%s safety_disabled=%s manual_axis=%s "
        "manual_axis_distance_mm=%s manual_mode=%s xy_step_feedrate_mm_min=%s "
        "focus_jog_feedrate_mm_min=%s focus_step_feedrate_mm_min=%s "
        "needle_step_feedrate_mm_min=%s turntable_jog_feedrate_mm_min=%s "
        "turntable_step_feedrate_mm_min=%s",
        jog.mode,
        jog.linear_distance_mm,
        jog.rotary_distance_deg,
        jog.motion_safety_disabled,
        jog.manual_axis,
        jog.manual_axis_distance_mm,
        jog.manual_axis_mode,
        jog.manual_axis_feedrate_mm_min,
        jog.focus_feedrate_mm_min,
        jog.focus_step_feedrate_mm_min,
        jog.needles_step_feedrate_mm_min,
        jog.turntable_feedrate_mm_min,
        jog.turntable_step_feedrate_mm_min,
    )


def on_controller_reboot_detected(owner: object) -> None:
    owner._stage_motion.reset(StageMotionResetReason.CONNECTION_CHANGED)
    owner._stage_unhomed_display_origins.clear()
    owner._pending_persisted_design_state = None
    owner._pending_persisted_design_position = None
    coordinate_flow.activate_current_design(owner)


def on_controller_reboot_ready(owner: object) -> None:
    if owner._controller_reboot_recovery_scheduled:
        return
    owner._controller_reboot_recovery_scheduled = True
    QTimer.singleShot(0, lambda: run_controller_reboot_recovery(owner))


def run_controller_reboot_recovery(owner: object) -> None:
    owner._controller_reboot_recovery_scheduled = False
    run_serial_startup_sync(owner)


def restore_persisted_controller_state(
    owner: object,
    cached_state: dict | None = None,
    *,
    cache_already_loaded: bool = False,
) -> None:
    if owner.serial_connection is None or not owner.serial_connection.is_open:
        return
    if not cache_already_loaded:
        cached_state = owner.settings_manager.load_controller_state()
    if not cached_state:
        logger.info("No cached controller homing state found for this connection.")
        return
    if not owner.stage_controller.cached_controller_session_is_current(cached_state):
        owner.settings_manager.clear_controller_state()
        owner.stage_controller.clear_cached_controller_state()
        owner._pending_persisted_design_state = None
        owner._pending_persisted_design_position = None
        owner._show_status("Controller session changed. Cleared cached homing state.")
        return
    logger.info(
        "Controller session marker matches; restoring cached homing state pending live status."
    )
    design_workspace.prepare_persisted_design_restore(owner, cached_state)
    owner.stage_controller.import_cached_controller_state(cached_state)
    apply_axis_feedrate_limits(owner, owner.stage_controller.axis_max_feedrates())
    owner._show_status("Restored cached homing state; reading live coordinates.")


def persist_controller_state(owner: object, *_args: object) -> None:
    if owner._controller_state_persistence_suspended:
        logger.debug("Skipping controller state persistence while serial state resets.")
        return
    state = design_workspace.controller_state_with_design(owner)
    owner.settings_manager.save_controller_state(state)


def persist_controller_state_if_available(owner: object) -> None:
    if owner._controller_state_persistence_suspended:
        return
    state = design_workspace.controller_state_with_design(owner)
    if state is None:
        return
    owner.settings_manager.save_controller_state(state)


def persist_serial_connection_state(owner: object, connected: bool) -> None:
    owner.settings_manager.save_serial_connection_state(
        connected,
        port=owner.serial_port_name,
        baud_rate=owner.serial_baud_rate,
    )


def persist_lcr_connection_state(
    owner: object,
    connected: bool,
    *,
    description: str = "",
) -> None:
    owner.settings_manager.save_meter_connection_state(
        connected,
        meter_type=owner.lcr_controller.meter_type(),
        description=description or owner.lcr_controller.connection_label(),
    )


def request_lcr_disconnect(owner: object) -> None:
    persist_lcr_connection_state(owner, False)
    owner.lcr_controller.request_disconnect()


__all__ = [
    "apply_axis_feedrate_limits",
    "apply_joystick_feedrate_preferences",
    "auto_connect_if_possible",
    "on_axis_max_feedrates_changed",
    "on_controller_reboot_detected",
    "on_controller_reboot_ready",
    "on_serial_connected",
    "on_serial_disconnected",
    "persist_controller_state",
    "persist_controller_state_if_available",
    "persist_lcr_connection_state",
    "persist_serial_connection_state",
    "request_lcr_disconnect",
    "restore_persisted_controller_state",
    "run_controller_reboot_recovery",
    "run_serial_startup_sync",
    "serial_baud_rate",
]
