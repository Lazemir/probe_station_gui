"""Main-window serial and controller connection flow helpers."""

from __future__ import annotations

from copy import deepcopy
import logging

from PySide6.QtCore import QTimer

from probe_station_gui.coordinates.coordinator_model import (
    AutofocusResult,
    CaptureMachinePoseIntent,
    CoordinateAdapterCompletion,
    CoordinateTransition,
    DesignActivationRequest,
    FinishOperatorAlignmentUiEffect,
    FocusMoveResult,
    LegacyDesignStateRewriteResult,
    LoadCoordinateFramesIntent,
    MachineProfileObservation,
    MachinePoseCaptureResult,
    MoveToFocusTargetIntent,
    RunAutofocusIntent,
    SaveCoordinateFramesIntent,
    RestoreOperatorAlignmentUiEffect,
    RewriteLegacyDesignStateIntent,
)
from probe_station_gui.coordinates.persistence import (
    CoordinateFrameStoreFailure,
)
from probe_station_gui.design import navigation_adapter as design_navigation
from probe_station_gui.stage import move_lifecycle as stage_move_lifecycle
from probe_station_gui.views import main_window_homing as homing_ui
from probe_station_gui.views import main_window_stage_position_panel as stage_position_panel


logger = logging.getLogger(__name__)
_FRAME_METADATA_UNSET = object()


def apply_coordinate_transition(
    owner: object,
    transition: CoordinateTransition,
) -> None:
    """Render one finished domain transition and submit its adapter intents."""

    owner._coordinate_frames_loaded = transition.snapshot.frames_loaded
    stage_position_panel.refresh_coordinate_frame_display(owner)
    _render_registration_snapshot(owner, transition)
    _render_coordinate_ui_effects(owner, transition)
    if _registration_view_changed(transition):
        refresh_panel = getattr(owner, "_refresh_design_panel", None)
        if callable(refresh_panel):
            refresh_panel()
        refresh_position = getattr(owner, "_refresh_design_position", None)
        if callable(refresh_position):
            refresh_position()
    show_status = getattr(owner, "_show_status", None)
    for notice in transition.notices:
        if notice.message and callable(show_status):
            show_status(notice.message, notice.duration_ms)
        if notice.code == "operator_alignment_saved":
            collapse = getattr(owner, "_collapse_alignment_panel_if_ready", None)
            if callable(collapse):
                collapse()
    for intent in transition.intents:
        try:
            if isinstance(intent, LoadCoordinateFramesIntent):
                owner._coordinate_frame_store.load(
                    intent.intent_id,
                    machine_profile_id=intent.machine_profile_id,
                )
            elif isinstance(intent, SaveCoordinateFramesIntent):
                owner._coordinate_frame_store.publish(
                    intent.intent_id,
                    intent.document,
                )
            elif isinstance(intent, CaptureMachinePoseIntent):
                accepted = owner.stage_controller.request_machine_coordinate_snapshot(
                    intent.intent_id,
                    axes=intent.axes,
                )
                if not accepted:
                    _complete_coordinate_adapter(
                        owner,
                        MachinePoseCaptureResult(
                            intent.intent_id,
                            succeeded=False,
                            message="Machine coordinates are unavailable.",
                        ),
                    )
            elif isinstance(intent, MoveToFocusTargetIntent):
                accepted = owner.stage_controller.request_token_bound_move_to_xy(
                    intent.intent_id,
                    intent.target_xy[0],
                    intent.target_xy[1],
                    owner.design_registration_focus_move_finished.emit,
                )
                if not accepted:
                    _complete_coordinate_adapter(
                        owner,
                        FocusMoveResult(
                            intent.intent_id,
                            succeeded=False,
                            message="Focus move was not started.",
                        ),
                    )
            elif isinstance(intent, RunAutofocusIntent):
                accepted = owner.stage_controller.request_registration_autofocus(
                    intent.intent_id,
                    owner.design_registration_autofocus_finished.emit,
                )
                if not accepted:
                    _complete_coordinate_adapter(
                        owner,
                        AutofocusResult(
                            intent.intent_id,
                            succeeded=False,
                            message="Autofocus was not started.",
                        ),
                    )
            elif isinstance(intent, RewriteLegacyDesignStateIntent):
                complete_legacy_design_migration(
                    owner,
                    intent.persisted_design_state,
                )
                _complete_coordinate_adapter(
                    owner,
                    LegacyDesignStateRewriteResult(
                        intent.intent_id,
                        succeeded=True,
                    ),
                )
        except Exception as exc:
            logger.exception("Coordinate frame adapter submission failed")
            if isinstance(
                intent,
                (LoadCoordinateFramesIntent, SaveCoordinateFramesIntent),
            ):
                failure = CoordinateFrameStoreFailure(
                    request_id=intent.intent_id,
                    operation=(
                        "load"
                        if isinstance(intent, LoadCoordinateFramesIntent)
                        else "save"
                    ),
                    message=str(exc) or type(exc).__name__,
                )
            elif isinstance(intent, CaptureMachinePoseIntent):
                failure = MachinePoseCaptureResult(
                    intent.intent_id,
                    succeeded=False,
                    message=str(exc) or type(exc).__name__,
                )
            elif isinstance(intent, MoveToFocusTargetIntent):
                failure = FocusMoveResult(
                    intent.intent_id,
                    succeeded=False,
                    message=str(exc) or type(exc).__name__,
                )
            elif isinstance(intent, RunAutofocusIntent):
                failure = AutofocusResult(
                    intent.intent_id,
                    succeeded=False,
                    message=str(exc) or type(exc).__name__,
                )
            elif isinstance(intent, RewriteLegacyDesignStateIntent):
                failure = LegacyDesignStateRewriteResult(
                    intent.intent_id,
                    succeeded=False,
                    message=str(exc) or type(exc).__name__,
                )
            else:
                continue
            _complete_coordinate_adapter(owner, failure)


def _registration_view_changed(transition: CoordinateTransition) -> bool:
    if transition.view_changed:
        return True
    if any(isinstance(intent, SaveCoordinateFramesIntent) for intent in transition.intents):
        return True
    return any(
        notice.code in {"registration_capture_updated", "registration_save_pending"}
        or notice.message == "Design coordinate frames could not be saved."
        for notice in transition.notices
    )


def _render_coordinate_ui_effects(
    owner: object,
    transition: CoordinateTransition,
) -> None:
    for effect in transition.ui_effects:
        if isinstance(effect, FinishOperatorAlignmentUiEffect):
            set_snap = getattr(owner, "_set_design_snap_enabled", None)
            if callable(set_snap):
                set_snap(False)
            finish = getattr(owner, "_finish_alignment_draft", None)
            if callable(finish):
                finish()
            continue
        if not isinstance(effect, RestoreOperatorAlignmentUiEffect):
            continue
        owner._alignment_design_draft = tuple(effect.design_marks)
        owner._alignment_stage_draft = list(effect.stage_marks)
        owner._alignment_draft_fit_residuals = None
        set_snap = getattr(owner, "_set_design_snap_enabled", None)
        if callable(set_snap):
            set_snap(True)
        window = getattr(owner, "design_layout_window", None)
        if window is not None and hasattr(window, "set_alignment_capture_points"):
            window.set_alignment_capture_points(effect.design_marks)
        refresh = getattr(owner, "_refresh_manual_alignment_ui", None)
        if callable(refresh):
            refresh()
        update = getattr(owner, "_update_stage_coordinate_apply_state", None)
        if callable(update):
            update()


def _render_registration_snapshot(
    owner: object,
    transition: CoordinateTransition,
) -> None:
    registration = transition.snapshot.registration
    release = registration.operator_pick_release
    marks_changed = False
    if registration.operator_stage_marks:
        marks = list(registration.operator_stage_marks)
        marks_changed = marks != getattr(owner, "_alignment_stage_draft", None)
        owner._alignment_stage_draft = marks
    if release is not None:
        active_slot = getattr(owner, "_manual_alignment_pick_slot", None)
        active_generation = getattr(owner, "_manual_alignment_pick_generation", None)
        matching = bool(
            (release.generation is None and active_slot is None)
            or (
                active_slot == release.slot
                and active_generation == release.generation
            )
        )
        if matching:
            owner._manual_alignment_pick_slot = None
        if not registration.operator_stage_marks:
            marks_changed = bool(getattr(owner, "_alignment_stage_draft", ()))
            owner._alignment_stage_draft = []
    if marks_changed or release is not None:
        refresh = getattr(owner, "_refresh_manual_alignment_ui", None)
        if callable(refresh):
            refresh()
        update = getattr(owner, "_update_stage_coordinate_apply_state", None)
        if callable(update):
            update()
    window = getattr(owner, "design_layout_window", None)
    if window is not None:
        window.set_focus_candidate(registration.focus_candidate)
        window.set_selected_focus_point(
            None
            if registration.focus_candidate is None
            else registration.focus_candidate.center
        )


def _complete_coordinate_adapter(owner: object, result: object) -> None:
    intent_id = getattr(result, "intent_id", getattr(result, "request_id", None))
    if not isinstance(intent_id, int):
        return
    transition = owner._coordinate_system_coordinator.complete(
        CoordinateAdapterCompletion(
            intent_id=intent_id,
            result=result,
        )
    )
    apply_coordinate_transition(owner, transition)


def request_coordinate_frame_load(owner: object) -> int:
    """Start the durable-frame load independently from serial connection state."""

    profile_source = getattr(owner, "_current_machine_profile_id", None)
    profile_id = profile_source() if callable(profile_source) else "default"
    transition = owner._coordinate_system_coordinator.start(
        MachineProfileObservation(str(profile_id or "default"))
    )
    load_intent = next(
        intent
        for intent in transition.intents
        if isinstance(intent, LoadCoordinateFramesIntent)
    )
    apply_coordinate_transition(owner, transition)
    return load_intent.intent_id


def activate_current_design(
    owner: object,
    *,
    session_state: object | None = None,
    frame_metadata: object = _FRAME_METADATA_UNSET,
    requested_frame_id: str | None = None,
    create_new: bool = False,
    create_new_if_registered: bool = False,
) -> CoordinateTransition | None:
    """Capture live adapter observations and submit one Design activation."""

    session = getattr(owner, "_design_session", None)
    coordinator = getattr(owner, "_coordinate_system_coordinator", None)
    if session is None or coordinator is None:
        return None
    state = session.snapshot_state() if session_state is None else session_state
    try:
        pivot_value = owner._rotation_geometry_snapshot().pivot_machine_xy
        pivot = (float(pivot_value[0]), float(pivot_value[1]))
    except Exception:
        pivot = None
    try:
        objective_value = owner._active_objective_xy_offset()
        objective_offset = (
            float(objective_value[0]),
            float(objective_value[1]),
        )
    except Exception:
        objective_offset = (0.0, 0.0)
    stage = owner.stage_controller
    axes_are_homed = getattr(stage, "axes_are_homed", None)
    homed_axes = frozenset(
        axis
        for axis in ("X", "Y")
        if callable(axes_are_homed) and axes_are_homed({axis})
    )
    latest_snapshot = getattr(stage, "latest_machine_coordinate_snapshot", None)
    transition = coordinator.activate_design(
        DesignActivationRequest(
            session_state=state,
            frame_metadata=(
                getattr(owner, "_active_design_frame_metadata", None)
                if frame_metadata is _FRAME_METADATA_UNSET
                else frame_metadata
            ),
            machine_snapshot=(latest_snapshot() if callable(latest_snapshot) else None),
            pivot_machine_xy=pivot,
            objective_xy_offset=objective_offset,
            requested_frame_id=requested_frame_id,
            create_new=create_new,
            create_new_if_registered=create_new_if_registered,
            homed_axes=homed_axes,
        )
    )
    apply_coordinate_transition(owner, transition)
    return transition


def handle_coordinate_frame_loaded(owner: object, result: object) -> None:
    was_loaded = bool(getattr(owner, "_coordinate_frames_loaded", False))
    transition = owner._coordinate_system_coordinator.complete(
        CoordinateAdapterCompletion(
            intent_id=int(result.request_id),
            result=result,
        )
    )
    apply_coordinate_transition(owner, transition)
    if was_loaded or not transition.snapshot.frames_loaded:
        return
    diagnostics = (
        *result.document.diagnostics,
        *getattr(result, "provenance_diagnostics", ()),
    )
    if diagnostics:
        for diagnostic in diagnostics:
            logger.warning("Design coordinate frame unavailable: %s", diagnostic)
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
    activate_current_design(owner)
def handle_coordinate_frame_saved(owner: object, result: object) -> None:
    transition = owner._coordinate_system_coordinator.complete(
        CoordinateAdapterCompletion(
            intent_id=int(result.request_id),
            result=result,
        )
    )
    apply_coordinate_transition(owner, transition)


def handle_coordinate_frame_failed(owner: object, failure: object) -> None:
    request_id = getattr(failure, "request_id", None)
    if not isinstance(request_id, int):
        return
    transition = owner._coordinate_system_coordinator.complete(
        CoordinateAdapterCompletion(intent_id=request_id, result=failure)
    )
    apply_coordinate_transition(owner, transition)
    if str(getattr(failure, "operation", "")) == "save" and transition.notices:
        refresh_panel = getattr(owner, "_refresh_design_panel", None)
        if callable(refresh_panel):
            refresh_panel()
        refresh_position = getattr(owner, "_refresh_design_position", None)
        if callable(refresh_position):
            refresh_position()


def complete_legacy_design_migration(
    owner: object,
    persisted_design_state: object,
) -> None:
    """Replace legacy controller-owned registration after frame publication."""

    if not isinstance(persisted_design_state, dict):
        raise TypeError("persisted_design_state must be a mapping")
    state = owner.stage_controller.export_cached_controller_state()
    if state is None:
        loaded = owner.settings_manager.load_controller_state()
        state = dict(loaded) if isinstance(loaded, dict) else {}
    state.pop("design", None)
    state.pop("design_session", None)
    state["design_session"] = deepcopy(persisted_design_state)
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
    activate_current_design(owner)
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
    activate_current_design(owner)
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
    activate_current_design(owner)


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
    registration = owner._coordinate_system_coordinator.snapshot().registration
    if isinstance(registration.legacy_migration_state, dict):
        state["design_session"] = deepcopy(registration.legacy_migration_state)
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
