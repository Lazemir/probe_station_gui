"""Main-window serial and controller connection flow helpers."""

from __future__ import annotations

import logging

from PySide6.QtCore import QTimer

from probe_station_gui.coordinates.persistence import CoordinateFrameDocument
from probe_station_gui.coordinates.provenance import (
    mark_design_frame_provenance_pending,
)
from probe_station_gui.design import navigation_adapter as design_navigation
from probe_station_gui.stage import move_lifecycle as stage_move_lifecycle
from probe_station_gui.views import main_window_homing as homing_ui
from probe_station_gui.views import main_window_stage_position_panel as stage_position_panel


logger = logging.getLogger(__name__)


def _next_coordinate_frame_request_id(owner: object) -> int:
    request_id = int(getattr(owner, "_coordinate_frame_request_id", 0)) + 1
    owner._coordinate_frame_request_id = request_id
    return request_id


def request_coordinate_frame_load(owner: object) -> int:
    """Start the durable-frame load independently from serial connection state."""

    request_id = _next_coordinate_frame_request_id(owner)
    owner._coordinate_frame_load_request_id = request_id
    registry = getattr(owner, "_coordinate_frame_registry", None)
    if registry is not None:
        snapshot = registry.snapshot()
        registry.reset(mark_design_frame_provenance_pending(snapshot.records))
    owner._coordinate_frames_loaded = False
    profile_source = getattr(owner, "_current_machine_profile_id", None)
    profile_id = profile_source() if callable(profile_source) else "default"
    if not isinstance(profile_id, str) or not profile_id.strip():
        profile_id = "default"
    owner._coordinate_frame_store.load(
        request_id,
        machine_profile_id=profile_id.strip(),
    )
    return request_id


def handle_coordinate_frame_loaded(owner: object, result: object) -> None:
    if result.request_id != getattr(owner, "_coordinate_frame_load_request_id", None):
        return
    owner._coordinate_frame_load_request_id = None
    owner._coordinate_frame_document = result.document
    runtime_records = getattr(result, "runtime_records", None)
    owner._coordinate_frame_registry.reset(
        result.document.records if runtime_records is None else runtime_records
    )
    owner._coordinate_frames_loaded = True
    diagnostics = (
        *result.document.diagnostics,
        *getattr(result, "provenance_diagnostics", ()),
    )
    if diagnostics:
        for diagnostic in diagnostics:
            logger.warning("Design coordinate frame unavailable: %s", diagnostic)
        show_status = getattr(owner, "_show_status", None)
        if callable(show_status):
            show_status("Some Design coordinate frames are unavailable.", 6000)
    materialize_custom = getattr(owner, "_materialize_software_coordinate_frames", None)
    if callable(materialize_custom):
        materialize_custom()
    reconcile_calibrations = getattr(
        owner,
        "_reconcile_design_calibration_fingerprints",
        None,
    )
    if callable(reconcile_calibrations):
        reconcile_calibrations()
    apply_authority = getattr(owner, "_apply_coordinate_frame_authority_blocks", None)
    if callable(apply_authority):
        apply_authority()
    owner._activate_loaded_design_frame()
    stage_position_panel.refresh_coordinate_frame_display(owner)


def publish_coordinate_frames(
    owner: object,
    *,
    legacy_migration: bool = False,
) -> int:
    stage_position_panel.refresh_coordinate_frame_display(owner)
    request_id = _next_coordinate_frame_request_id(owner)
    base_document = getattr(owner, "_coordinate_frame_document", None)
    if not isinstance(base_document, CoordinateFrameDocument):
        base_document = CoordinateFrameDocument()
    document = base_document.with_records(
        owner._coordinate_frame_registry.snapshot().records
    )
    owner._coordinate_frame_document = document
    if legacy_migration or getattr(
        owner,
        "_legacy_design_migration_request_id",
        None,
    ) is not None:
        owner._legacy_design_migration_request_id = request_id
    owner._coordinate_frame_store.publish(request_id, document)
    return request_id


def handle_coordinate_frame_saved(owner: object, result: object) -> None:
    if result.request_id != getattr(
        owner,
        "_legacy_design_migration_request_id",
        None,
    ):
        return
    try:
        complete_legacy_design_migration(owner)
    except Exception:
        logger.exception("Failed to replace legacy Design registration state")
        show_status = getattr(owner, "_show_status", None)
        if callable(show_status):
            show_status("Design registration migration could not be finalized.", 6000)
        return
    owner._legacy_design_migration_request_id = None
    owner._legacy_design_migration_state = None


def complete_legacy_design_migration(owner: object) -> None:
    """Replace legacy controller-owned registration after frame publication."""

    state = owner.stage_controller.export_cached_controller_state()
    if state is None:
        loaded = owner.settings_manager.load_controller_state()
        state = dict(loaded) if isinstance(loaded, dict) else {}
    state.pop("design", None)
    state.pop("design_session", None)
    session_state = owner._design_session.export_persisted_state()
    if session_state is not None:
        state["design_session"] = session_state
    owner.settings_manager.save_controller_state(state)


def serial_baud_rate(serial_port: object) -> int:
    try:
        return int(getattr(serial_port, "baudrate"))
    except TypeError:
        return int(float(getattr(serial_port, "baudrate")))


def on_serial_connected(owner: object, serial_port: object) -> None:
    _clear_exact_step_targets(owner)
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
    owner._last_reported_b_position = None
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
    apply_authority = getattr(owner, "_apply_coordinate_frame_authority_blocks", None)
    if callable(apply_authority):
        apply_authority()
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
    from probe_station_gui.views.main_window_shutdown import stop_jog_before_serial_close

    _clear_exact_step_targets(owner)
    stop_jog_before_serial_close(owner, "serial disconnect")
    if owner.serial_connection and owner.serial_connection.is_open:
        owner.serial_connection.close()
    owner.serial_connection = None
    persist_serial_connection_state(owner, False)
    owner._stage_unhomed_display_origins.clear()
    owner._last_reported_b_position = None
    owner._manual_jog_timer.stop()
    owner._manual_jog_prediction.reset_tracking()
    owner._controller_reboot_recovery_scheduled = False
    stage_move_lifecycle.clear_coordinate_move_tracking(
        owner,
        clear_pending=True,
        reset_override=False,
    )
    homing_ui.clear_pending_homing_queue(owner)
    stage_position_panel.clear_stage_motion_axes(owner)
    owner._clear_planned_move_prediction(clear_wait_state=True)
    owner._update_stage_coordinate_apply_state()
    logger.info("Serial disconnected")
    owner.stage_controller.request_stop_oscillation()
    owner._controller_state_persistence_suspended = True
    try:
        owner.stage_controller.set_serial(None)
    finally:
        owner._controller_state_persistence_suspended = False
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
    apply_authority = getattr(owner, "_apply_coordinate_frame_authority_blocks", None)
    if callable(apply_authority):
        apply_authority()
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
    _clear_exact_step_targets(owner)
    owner._stage_unhomed_display_origins.clear()
    owner._pending_persisted_design_state = None
    owner._pending_persisted_design_position = None
    apply_authority = getattr(owner, "_apply_coordinate_frame_authority_blocks", None)
    if callable(apply_authority):
        apply_authority()


def _clear_exact_step_targets(owner: object) -> None:
    callback = getattr(owner, "_clear_exact_step_targets", None)
    if callable(callback):
        callback()


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
    prepare_persisted_design_restore(owner, cached_state)
    owner.stage_controller.import_cached_controller_state(cached_state)
    apply_axis_feedrate_limits(owner, owner.stage_controller.axis_max_feedrates())
    owner._show_status("Restored cached homing state; reading live coordinates.")


def persist_controller_state(owner: object, *_args: object) -> None:
    if owner._controller_state_persistence_suspended:
        logger.debug("Skipping controller state persistence while serial state resets.")
        return
    state = controller_state_with_design(owner)
    owner.settings_manager.save_controller_state(state)


def persist_controller_state_if_available(owner: object) -> None:
    if owner._controller_state_persistence_suspended:
        return
    state = controller_state_with_design(owner)
    if state is None:
        return
    owner.settings_manager.save_controller_state(state)


def controller_state_with_design(owner: object) -> dict[str, object] | None:
    state = owner.stage_controller.export_cached_controller_state()
    if state is None:
        return None
    if getattr(owner, "_legacy_design_migration_request_id", None) is not None:
        legacy_state = getattr(owner, "_legacy_design_migration_state", None)
        if not isinstance(legacy_state, dict):
            persisted = owner.settings_manager.load_controller_state()
            if isinstance(persisted, dict):
                legacy_state = persisted.get(
                    "design_session",
                    persisted.get("design"),
                )
        if isinstance(legacy_state, dict):
            state["design_session"] = dict(legacy_state)
        return state
    design_state = owner._design_session.export_persisted_state()
    if design_state is not None:
        state["design_session"] = design_state
    return state


def prepare_persisted_design_restore(
    owner: object,
    cached_state: dict[str, object],
) -> None:
    design_state = cached_state.get("design_session", cached_state.get("design"))
    cached_position = design_navigation.coerce_position_tuple(
        cached_state.get("last_stage_position")
    )
    if not isinstance(design_state, dict) or cached_position is None:
        owner._pending_persisted_design_state = None
        owner._pending_persisted_design_position = None
        return
    owner._pending_persisted_design_state = dict(design_state)
    owner._pending_persisted_design_position = cached_position


def maybe_restore_persisted_design(
    owner: object,
    position: tuple[float, ...],
) -> None:
    design_state = owner._pending_persisted_design_state
    expected_position = owner._pending_persisted_design_position
    if design_state is None:
        return
    owner._pending_persisted_design_state = None
    owner._pending_persisted_design_position = None
    if owner._design_session.document is not None:
        return
    decision = design_navigation.prepare_persisted_design_restore(
        design_state,
        expected_position=expected_position,
        actual_position=position,
        document_loaded=False,
        axis_names=owner.STAGE_AXIS_NAMES,
        tolerance=owner.DESIGN_RESTORE_POSITION_TOLERANCE,
        file_is_current=design_navigation.persisted_design_file_is_current,
    )
    if decision.axes_to_mark_unhomed:
        removed_axes = owner.stage_controller.mark_axes_unhomed(
            decision.axes_to_mark_unhomed
        )
        if removed_axes:
            axes_label = ", ".join(sorted(removed_axes))
            if removed_axes == {"Z"}:
                owner._show_status(
                    "Controller Z coordinate changed. Cleared cached Z homing.",
                    5000,
                )
            else:
                owner._show_status(
                    f"Controller {axes_label} coordinate changed. Cleared cached homing.",
                    5000,
                )
    if decision.status_message is not None:
        owner._show_status(decision.status_message, decision.status_timeout_ms)
    if decision.clear_cached_design:
        save_controller_state_without_design(owner)
        return
    if decision.should_start_load and decision.design_path is not None:
        owner._start_design_document_load(
            decision.design_path,
            restore_state=decision.restore_state,
            show_window=False,
        )


def save_controller_state_without_design(owner: object) -> None:
    state = owner.stage_controller.export_cached_controller_state()
    if state is None:
        cached_state = owner.settings_manager.load_controller_state()
        if isinstance(cached_state, dict):
            cached_state.pop("design", None)
            cached_state.pop("design_session", None)
            owner.settings_manager.save_controller_state(cached_state)
        return
    state.pop("design", None)
    state.pop("design_session", None)
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
