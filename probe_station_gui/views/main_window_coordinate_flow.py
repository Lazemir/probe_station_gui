"""Main-window adapter for Coordinate System transitions and intents."""

from __future__ import annotations

from copy import deepcopy
import logging

from probe_station_gui.coordinates.coordinator_model import (
    AutofocusResult,
    CaptureMachinePoseIntent,
    CoordinateAdapterCompletion,
    CoordinateAuthorityObservation,
    CoordinateTransition,
    CustomSystemsRequest,
    DesignCalibrationObservation,
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
    RestoreDesignWorkspaceUiEffect,
    RestoreOperatorAlignmentUiEffect,
    RewriteLegacyDesignStateIntent,
    RunAutofocusIntent,
    SaveCoordinateFramesIntent,
)
from probe_station_gui.coordinates.design_calibration import (
    design_calibration_fingerprints,
)
from probe_station_gui.coordinates.model import PhysicalMachinePose
from probe_station_gui.coordinates.persistence import CoordinateFrameStoreFailure
from probe_station_gui.design.model import DesignModelError
from probe_station_gui.views import main_window_design_workspace as design_workspace
from probe_station_gui.views import (
    main_window_stage_position_panel as stage_position_panel,
)


logger = logging.getLogger(__name__)


_FRAME_METADATA_UNSET = object()
_CONTROLLER_CACHE_UNSET = object()


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
    *,
    machine_snapshot: object = _CONTROLLER_CACHE_UNSET,
    homed_axes: object = _CONTROLLER_CACHE_UNSET,
) -> CoordinateAuthorityObservation:
    """Capture one immutable controller/settings authority observation."""

    stage = getattr(owner, "stage_controller", None)
    captured_snapshot = machine_snapshot
    if captured_snapshot is _CONTROLLER_CACHE_UNSET:
        latest_snapshot = getattr(stage, "latest_motion_coordinate_snapshot", None)
        if not callable(latest_snapshot):
            latest_snapshot = getattr(stage, "latest_machine_coordinate_snapshot", None)
        try:
            captured_snapshot = latest_snapshot() if callable(latest_snapshot) else None
        except Exception:
            captured_snapshot = None
    pose = physical_pose
    if not isinstance(pose, PhysicalMachinePose):
        pose = (
            captured_snapshot.physical_machine_pose
            if captured_snapshot is not None
            else PhysicalMachinePose({})
        )
    captured_homed_axes = homed_axes
    if captured_homed_axes is _CONTROLLER_CACHE_UNSET:
        homed_getter = getattr(stage, "homed_axes", None)
        try:
            captured_homed_axes = (
                frozenset(homed_getter()) if callable(homed_getter) else frozenset()
            )
        except Exception:
            captured_homed_axes = frozenset()
    elif captured_homed_axes is None:
        captured_homed_axes = frozenset()
    else:
        captured_homed_axes = frozenset(captured_homed_axes)
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
        homed_axes=captured_homed_axes,
        machine_snapshot=captured_snapshot,
        pivot_machine_xy=pivot,
        objective_xy_offset=objective_offset,
        pivot_error=pivot_error,
        pivot_error_permanent=pivot_error_permanent,
    )


def observe_coordinate_authority(
    owner: object,
    physical_pose: PhysicalMachinePose | None = None,
    *,
    machine_snapshot: object = _CONTROLLER_CACHE_UNSET,
    homed_axes: object = _CONTROLLER_CACHE_UNSET,
) -> CoordinateTransition | None:
    coordinator = getattr(owner, "_coordinate_system_coordinator", None)
    if coordinator is None:
        return None
    transition = coordinator.observe_authority(
        coordinate_authority_observation(
            owner,
            physical_pose,
            machine_snapshot=machine_snapshot,
            homed_axes=homed_axes,
        )
    )
    apply_coordinate_transition(owner, transition)
    return transition


def _registration_view_changed(transition: CoordinateTransition) -> bool:
    if transition.view_changed:
        return True
    if any(
        isinstance(intent, SaveCoordinateFramesIntent) for intent in transition.intents
    ):
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
        design_workspace.restore_design_workspace(owner, effect)


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
            or (active_slot == release.slot and active_generation == release.generation)
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
        design_workspace.capture_design_workspace(owner)
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
        design_workspace.apply_design_workspace_checkpoint(owner, workspace_after)
    apply_coordinate_transition(owner, transition)
    return transition


def handle_coordinate_frame_loaded(owner: object, result: object) -> None:
    was_loaded = bool(owner._coordinate_system_coordinator.snapshot().frames_loaded)
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
    _observe_loaded_design_calibrations(owner)
    activate_current_design(owner)


def _observe_loaded_design_calibrations(owner: object) -> None:
    transition = owner._coordinate_system_coordinator.observe_design_calibrations(
        DesignCalibrationObservation(
            design_calibration_fingerprints(
                owner.settings_manager.settings.axis_calibrations
            )
        )
    )
    apply_coordinate_transition(owner, transition)


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


__all__ = [
    "activate_current_design",
    "apply_coordinate_transition",
    "coordinate_authority_observation",
    "handle_coordinate_frame_failed",
    "handle_coordinate_frame_loaded",
    "handle_coordinate_frame_saved",
    "observe_coordinate_authority",
    "request_coordinate_frame_load",
]
