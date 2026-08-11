"""Main-window serial and controller connection flow helpers."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import logging

from PySide6.QtCore import QTimer

from probe_station_gui.coordinates.coordinator_model import (
    AutofocusResult,
    CaptureMachinePoseIntent,
    CoordinateAdapterCompletion,
    CoordinateAuthorityObservation,
    CoordinateTransition,
    CustomSystemsRequest,
    DesignActivationRequest,
    DesignWorkspaceCheckpoint,
    FinishOperatorAlignmentUiEffect,
    FocusMoveResult,
    LegacyDesignStateRewriteResult,
    LoadCoordinateFramesIntent,
    MachineProfileObservation,
    MachinePoseCaptureResult,
    MoveToFocusTargetIntent,
    PersistCoordinateSelectionIntent,
    RunAutofocusIntent,
    SaveCoordinateFramesIntent,
    RestoreOperatorAlignmentUiEffect,
    RestoreDesignWorkspaceUiEffect,
    RewriteLegacyDesignStateIntent,
)
from probe_station_gui.coordinates.model import PhysicalMachinePose
from probe_station_gui.coordinates.persistence import (
    CoordinateFrameStoreFailure,
)
from probe_station_gui.design import navigation_adapter as design_navigation
from probe_station_gui.design.model import DesignModelError
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

    stage_position_panel.render_coordinate_system_snapshot(owner, transition.snapshot)
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
    if not transition.accepted:
        plan = transition.snapshot.display_plan
        reason = None if plan is None else plan.selection_reason
        if reason and callable(show_status):
            show_status(reason, 5000)
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
            elif isinstance(intent, PersistCoordinateSelectionIntent):
                _persist_coordinate_selection(owner, intent.frame_id)
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
            elif isinstance(intent, PersistCoordinateSelectionIntent):
                if callable(show_status):
                    show_status(
                        "Coordinate selection could not be saved.",
                        6000,
                    )
                continue
            else:
                continue
            _complete_coordinate_adapter(owner, failure)


def _persist_coordinate_selection(owner: object, frame_id: str) -> None:
    manager = getattr(owner, "settings_manager", None)
    update = getattr(manager, "set_software_coordinate_selection", None)
    store = getattr(owner, "_software_coordinate_selection_store", None)
    publish = getattr(store, "publish", None)
    snapshot = update(frame_id) if callable(update) else None
    if snapshot is not None and callable(publish):
        publish(snapshot)


def coordinate_authority_observation(
    owner: object,
    physical_pose: PhysicalMachinePose | None = None,
) -> CoordinateAuthorityObservation:
    """Capture one immutable controller/settings authority observation."""

    stage = getattr(owner, "stage_controller", None)
    latest_snapshot = getattr(stage, "latest_motion_coordinate_snapshot", None)
    if not callable(latest_snapshot):
        latest_snapshot = getattr(stage, "latest_machine_coordinate_snapshot", None)
    try:
        machine_snapshot = latest_snapshot() if callable(latest_snapshot) else None
    except Exception:
        machine_snapshot = None
    pose = physical_pose
    if not isinstance(pose, PhysicalMachinePose):
        pose = (
            machine_snapshot.physical_machine_pose
            if machine_snapshot is not None
            else PhysicalMachinePose({})
        )
    homed_getter = getattr(stage, "homed_axes", None)
    try:
        homed_axes = frozenset(homed_getter()) if callable(homed_getter) else frozenset()
    except Exception:
        homed_axes = frozenset()
    pivot = None
    pivot_error = None
    pivot_error_permanent = False
    geometry_getter = getattr(owner, "_rotation_geometry_snapshot", None)
    try:
        if callable(geometry_getter):
            pivot_value = geometry_getter().pivot_machine_xy
            pivot = (float(pivot_value[0]), float(pivot_value[1]))
    except (DesignModelError, IndexError, TypeError, ValueError) as exc:
        pivot_error = str(exc) or type(exc).__name__
        pivot_error_permanent = True
    except Exception as exc:
        pivot_error = str(exc) or type(exc).__name__
    objective_getter = getattr(owner, "_active_objective_xy_offset", None)
    try:
        objective_value = objective_getter() if callable(objective_getter) else None
        objective_offset = (
            (float(objective_value[0]), float(objective_value[1]))
            if objective_value is not None
            else (float("nan"), float("nan"))
        )
    except Exception:
        objective_offset = (float("nan"), float("nan"))
    return CoordinateAuthorityObservation(
        physical_pose=pose,
        homed_axes=homed_axes,
        machine_snapshot=machine_snapshot,
        pivot_machine_xy=pivot,
        objective_xy_offset=objective_offset,
        pivot_error=pivot_error,
        pivot_error_permanent=pivot_error_permanent,
    )


def observe_coordinate_authority(
    owner: object,
    physical_pose: PhysicalMachinePose | None = None,
) -> CoordinateTransition | None:
    coordinator = getattr(owner, "_coordinate_system_coordinator", None)
    if coordinator is None:
        return None
    transition = coordinator.observe_authority(
        coordinate_authority_observation(owner, physical_pose)
    )
    apply_coordinate_transition(owner, transition)
    return transition


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
    workspace_rollbacks: list[RestoreDesignWorkspaceUiEffect] = []
    for effect in transition.ui_effects:
        if isinstance(effect, RestoreDesignWorkspaceUiEffect):
            workspace_rollbacks.append(effect)
            continue
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
    for effect in reversed(workspace_rollbacks):
        _restore_design_workspace(owner, effect)


def _restore_design_workspace(
    owner: object,
    effect: RestoreDesignWorkspaceUiEffect,
) -> None:
    session = getattr(owner, "_design_session", None)
    snapshot_state = getattr(session, "snapshot_state", None)
    apply_state = getattr(session, "apply_state", None)
    if not callable(snapshot_state) or not callable(apply_state):
        return
    current = capture_design_workspace(owner)
    applied = effect.applied
    previous = effect.previous
    if current.session_state.document is not applied.session_state.document:
        return
    if not _workspace_documents_are_compatible(previous, applied):
        apply_design_workspace_checkpoint(owner, previous)
        return
    current_state = current.session_state
    applied_state = applied.session_state
    previous_state = previous.session_state
    if (
        current_state.targets == applied_state.targets
        and current_state.selected_target_index
        == applied_state.selected_target_index
    ):
        targets = previous_state.targets
        selected_target_index = previous_state.selected_target_index
    else:
        targets = current_state.targets
        selected_target_index = current_state.selected_target_index
    if (
        current_state.route == applied_state.route
        and current_state.selected_route_point_index
        == applied_state.selected_route_point_index
    ):
        route = previous_state.route
        selected_route_point_index = previous_state.selected_route_point_index
    else:
        route = current_state.route
        selected_route_point_index = current_state.selected_route_point_index
    apply_design_workspace_checkpoint(
        owner,
        replace(
            current,
            session_state=replace(
                current_state,
                document=previous_state.document,
                targets=targets,
                selected_target_index=selected_target_index,
                route=route,
                selected_route_point_index=selected_route_point_index,
            ),
            frame_metadata=_rollback_value(
                current.frame_metadata,
                applied.frame_metadata,
                previous.frame_metadata,
            ),
            markup=_rollback_value(
                current.markup,
                applied.markup,
                previous.markup,
            ),
            direct_guide_ids=_rollback_value(
                current.direct_guide_ids,
                applied.direct_guide_ids,
                previous.direct_guide_ids,
            ),
            pending_visibility=_rollback_value(
                current.pending_visibility,
                applied.pending_visibility,
                previous.pending_visibility,
            ),
            last_selected_design_point=_rollback_value(
                current.last_selected_design_point,
                applied.last_selected_design_point,
                previous.last_selected_design_point,
            ),
            pending_alignment_preparation=_rollback_value(
                current.pending_alignment_preparation,
                applied.pending_alignment_preparation,
                previous.pending_alignment_preparation,
            ),
        ),
    )


def _rollback_value(current: object, applied: object, previous: object):
    return previous if current == applied else current


def _workspace_documents_are_compatible(
    previous: DesignWorkspaceCheckpoint,
    applied: DesignWorkspaceCheckpoint,
) -> bool:
    previous_document = previous.session_state.document
    applied_document = applied.session_state.document
    if previous_document is None or applied_document is None:
        return previous_document is applied_document
    return bool(
        str(previous_document.path) == str(applied_document.path)
        and previous_document.source_load_id == applied_document.source_load_id
        and previous_document.top_cell_name == applied_document.top_cell_name
        and previous_document.rotation_quarter_turns
        == applied_document.rotation_quarter_turns
    )


def capture_design_workspace(
    owner: object,
    *,
    session_state: object | None = None,
    frame_metadata: object = _FRAME_METADATA_UNSET,
    markup: object = _FRAME_METADATA_UNSET,
    direct_guide_ids: object = _FRAME_METADATA_UNSET,
    pending_visibility: object = _FRAME_METADATA_UNSET,
    last_selected_design_point: object = _FRAME_METADATA_UNSET,
    pending_alignment_preparation: object = _FRAME_METADATA_UNSET,
) -> DesignWorkspaceCheckpoint:
    if session_state is None:
        session_state = owner._design_session.snapshot_state()
    return DesignWorkspaceCheckpoint(
        session_state=session_state,
        frame_metadata=(
            getattr(owner, "_active_design_frame_metadata", None)
            if frame_metadata is _FRAME_METADATA_UNSET
            else frame_metadata
        ),
        markup=(
            getattr(owner, "_design_markup", None)
            if markup is _FRAME_METADATA_UNSET
            else markup
        ),
        direct_guide_ids=tuple(
            getattr(owner, "_design_markup_direct_guide_ids", ())
            if direct_guide_ids is _FRAME_METADATA_UNSET
            else direct_guide_ids
        ),
        pending_visibility=(
            getattr(owner, "_design_markup_pending_visibility", None)
            if pending_visibility is _FRAME_METADATA_UNSET
            else pending_visibility
        ),
        last_selected_design_point=(
            getattr(owner, "_last_selected_design_point", None)
            if last_selected_design_point is _FRAME_METADATA_UNSET
            else last_selected_design_point
        ),
        pending_alignment_preparation=(
            getattr(owner, "_pending_alignment_preparation", None)
            if pending_alignment_preparation is _FRAME_METADATA_UNSET
            else pending_alignment_preparation
        ),
    )


def apply_design_workspace_checkpoint(
    owner: object,
    checkpoint: DesignWorkspaceCheckpoint,
) -> None:
    owner._design_session.apply_state(checkpoint.session_state)
    owner._active_design_frame_metadata = checkpoint.frame_metadata
    owner._design_markup = checkpoint.markup
    owner._design_markup_direct_guide_ids = list(checkpoint.direct_guide_ids)
    owner._design_markup_pending_visibility = checkpoint.pending_visibility
    owner._last_selected_design_point = checkpoint.last_selected_design_point
    owner._pending_alignment_preparation = (
        checkpoint.pending_alignment_preparation
    )


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
    workspace_after: DesignWorkspaceCheckpoint | None = None,
) -> CoordinateTransition | None:
    """Capture live adapter observations and submit one Design activation."""

    coordinator = getattr(owner, "_coordinate_system_coordinator", None)
    if coordinator is None:
        return None
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
    workspace_before = (
        capture_design_workspace(owner)
        if workspace_after is not None
        else None
    )
    transition = coordinator.activate_design(
        DesignActivationRequest(
            session_state=session_state,
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
            workspace_before=workspace_before,
            workspace_after=workspace_after,
        )
    )
    if transition.accepted and workspace_after is not None:
        apply_design_workspace_checkpoint(owner, workspace_after)
    apply_coordinate_transition(owner, transition)
    return transition


def handle_coordinate_frame_loaded(owner: object, result: object) -> None:
    was_loaded = bool(
        owner._coordinate_system_coordinator.snapshot().frames_loaded
    )
    if not was_loaded:
        settings = getattr(getattr(owner, "settings_manager", None), "settings", None)
        software_coordinates = getattr(settings, "software_coordinates", None)
        if software_coordinates is not None:
            try:
                synchronized = (
                    owner._coordinate_system_coordinator.synchronize_custom_systems(
                        CustomSystemsRequest(software_coordinates)
                    )
                )
            except (TypeError, ValueError) as exc:
                show_status = getattr(owner, "_show_status", None)
                if callable(show_status):
                    show_status(
                        f"Custom coordinate settings could not be loaded: {exc}",
                        6000,
                    )
            else:
                apply_coordinate_transition(owner, synchronized)
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
    design_state = owner._coordinate_runtime.controller_persistence_state(
        owner._design_session.snapshot_state()
    )
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
    if owner._coordinate_system_coordinator.current_design_lease().document is not None:
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
