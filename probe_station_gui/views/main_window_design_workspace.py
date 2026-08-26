"""Main-window Design workspace checkpoint and persistence adapter."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from probe_station_gui.coordinates.coordinator_model import (
    DesignWorkspaceCheckpoint,
    RestoreDesignWorkspaceUiEffect,
)
from probe_station_gui.design import navigation_adapter as design_navigation


_FRAME_METADATA_UNSET = object()


def restore_design_workspace(
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
        and current_state.selected_target_index == applied_state.selected_target_index
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


__all__ = [
    "apply_design_workspace_checkpoint",
    "capture_design_workspace",
    "controller_state_with_design",
    "maybe_restore_persisted_design",
    "prepare_persisted_design_restore",
    "restore_design_workspace",
    "save_controller_state_without_design",
]
