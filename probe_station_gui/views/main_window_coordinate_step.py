"""Exact Step projection and widget adaptation."""

from __future__ import annotations

from typing import Any

from probe_station_gui.stage.coordinate_targets import (
    CoordinateMoveCompletion,
    CoordinateMoveRequest,
)
from probe_station_gui.stage.exact_step import (
    ExactStepClearReason,
    ExactStepRequest,
)
from probe_station_gui.views import main_window_coordinate_motion as coordinate_motion
from probe_station_gui.views import (
    main_window_stage_position_panel as stage_position_panel,
)


def on_manual_axis_move_requested(
    owner: Any,
    axis: str,
    value_mm: float,
    mode: str,
    feedrate_mm_min: float,
) -> None:
    """Project one Step endpoint and submit its resolved batch to the session."""

    del feedrate_mm_min
    axis = axis.strip().upper()
    if axis not in owner.STAGE_AXIS_NAMES:
        return
    if not stage_position_panel.gui_coordinate_motion_editing_enabled(owner):
        clear_exact_steps(owner, ExactStepClearReason.COORDINATE_MODE_CHANGED)
        owner._show_status("Selected Coordinate System is unavailable.", 3000)
        return
    mode = mode.strip().upper()
    if mode not in {"G90", "G91"}:
        owner._show_status(f"Unsupported manual move mode: {mode}.", 3000)
        return
    snapshot = owner._stage_motion.snapshot()
    if owner.stage_controller.is_busy() and not snapshot.coordinate_active:
        owner._show_status("Stage is busy. Ignoring manual axis move.", 3000)
        return
    motion_lease = coordinate_motion.gui_motion_lease(owner)
    active_basis = snapshot.coordinate_display_basis
    same_display_basis = (
        motion_lease is None
        or active_basis == coordinate_motion.motion_basis(motion_lease)
    )
    baseline = (
        dict(snapshot.coordinate_display_targets).get(axis)
        if same_display_basis
        else None
    )
    if baseline is None:
        baseline = owner._stage_axis_display_values.get(axis)
    if baseline is None:
        owner._show_status(f"{axis} coordinate is unavailable.", 3000)
        return
    candidates = dict(snapshot.exact_step_display_targets)
    try:
        display_target = (
            float(value_mm)
            if mode == "G90"
            else float(candidates.get(axis, baseline)) + float(value_mm)
        )
    except (TypeError, ValueError):
        owner._show_status(f"Invalid {axis} target coordinate.", 3000)
        return
    candidates[axis] = display_target
    resolved = _resolve_exact_targets(
        owner,
        candidates,
        motion_lease=motion_lease,
        snapshot=snapshot,
    )
    if resolved is None:
        return
    move_targets, pending_targets, physical_targets, projected_lease = resolved
    request = CoordinateMoveRequest(
        targets=tuple(
            (target_axis, raw, display)
            for target_axis, (raw, display) in move_targets.items()
        ),
        seed_position=(
            snapshot.presented_position
            or owner.stage_controller.latest_stage_position()
        ),
        feedrate_mm_min=owner._coordinate_feedrate_for_axes(move_targets),
        source_label="Step",
        physical_limit_targets=tuple(physical_targets.items()),
        display_basis=coordinate_motion.motion_basis(projected_lease),
    )
    outcome = owner._stage_motion.queue_exact_step(
        ExactStepRequest(
            move_request=request,
            motion_lease=projected_lease,
            allow_pose_rebase=bool(
                snapshot.coordinate_active or snapshot.exact_step_pose_rebase_allowed
            ),
            pending_targets=tuple(pending_targets),
        )
    )
    if not outcome.accepted:
        return
    panel = getattr(owner, "_stage_position_panel", None)
    if panel is not None:
        for target_axis, raw_target, pending_display in outcome.pending_targets:
            panel.set_pending_target(target_axis, raw_target, pending_display)
    stage_position_panel.refresh_stage_axis_styles(owner)
    owner._update_stage_coordinate_apply_state()


def _resolve_exact_targets(
    owner: Any,
    candidates: dict[str, float],
    *,
    motion_lease: object | None,
    snapshot: Any,
) -> (
    tuple[
        dict[str, tuple[float, float]],
        tuple[tuple[str, float, float], ...],
        dict[str, float],
        object | None,
    ]
    | None
):
    if motion_lease is not None:
        projection = owner._project_gui_coordinate_motion(
            tuple(candidates.items()),
            mode="G90",
            lease=snapshot.exact_step_motion_lease or motion_lease,
            allow_pose_rebase=bool(
                snapshot.coordinate_active or snapshot.exact_step_pose_rebase_allowed
            ),
        )
        if projection is None or not projection.accepted:
            owner._show_status(
                (
                    projection.reason
                    if projection is not None
                    else "Coordinate movement is unavailable."
                ),
                4000,
            )
            return None
        limit_error = coordinate_motion.projection_limit_error(owner, projection)
        if limit_error is not None:
            owner._show_status(limit_error, 4000)
            return None
        move_targets = coordinate_motion.projection_targets(projection)
        pending_targets = _pending_projection_targets(candidates, move_targets)
        if pending_targets is None:
            owner._show_status("Coordinate movement is unavailable.", 3000)
            return None
        return (
            move_targets,
            pending_targets,
            dict(projection.machine_targets),
            projection.lease,
        )
    move_targets: dict[str, tuple[float, float]] = {}
    for target_axis, target_display in candidates.items():
        raw_target = owner._raw_target_from_display_value(
            target_axis,
            target_display,
        )
        if raw_target is None:
            owner._show_status(
                f"{target_axis} coordinate is unavailable.",
                3000,
            )
            return None
        limit_error = owner._stage_axis_target_limit_error(
            target_axis,
            target_display,
        )
        if limit_error is not None:
            owner._show_status(limit_error, 4000)
            return None
        move_targets[target_axis] = (float(raw_target), float(target_display))
    return (
        move_targets,
        tuple(
            (target_axis, raw, display)
            for target_axis, (raw, display) in move_targets.items()
        ),
        {},
        None,
    )


def _pending_projection_targets(
    candidates: dict[str, float],
    move_targets: dict[str, tuple[float, float]],
) -> tuple[tuple[str, float, float], ...] | None:
    pending: list[tuple[str, float, float]] = []
    for axis, display_target in candidates.items():
        resolved = move_targets.get(axis)
        if resolved is None:
            return None
        pending.append((axis, float(resolved[0]), float(display_target)))
    return tuple(pending)


def clear_exact_steps(owner: Any, reason: ExactStepClearReason) -> None:
    axes = tuple(dict(owner._stage_motion.snapshot().exact_step_display_targets))
    owner._stage_motion.clear_exact_steps(reason)
    panel = getattr(owner, "_stage_position_panel", None)
    if panel is not None:
        for axis in axes:
            panel.pop_pending_target(axis)
    stage_position_panel.refresh_stage_axis_styles(owner)
    owner._update_stage_coordinate_apply_state()


def on_coordinate_move_finished(
    owner: Any,
    completion: CoordinateMoveCompletion,
) -> None:
    if not isinstance(completion, CoordinateMoveCompletion):
        raise TypeError("completion must be a CoordinateMoveCompletion")
    pending_axes = owner._stage_motion.snapshot().pending_edit_axes
    panel = getattr(owner, "_stage_position_panel", None)
    if panel is not None:
        for axis, _display in completion.display_targets:
            if axis not in pending_axes:
                panel.pop_pending_target(axis)
    stage_position_panel.refresh_stage_axis_styles(owner)
    owner._update_stage_coordinate_apply_state()


__all__ = [
    "clear_exact_steps",
    "on_coordinate_move_finished",
    "on_manual_axis_move_requested",
]
