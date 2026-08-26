"""Exact Step input workflow for the selected GUI Coordinate System."""

from __future__ import annotations

from typing import Any

from probe_station_gui.stage.coordinate_targets import CoordinateMoveRequest
from probe_station_gui.stage import position_update as stage_position_update
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
    """Accumulate exact Step targets and route them through coordinate moves."""

    del feedrate_mm_min
    axis = axis.strip().upper()
    if axis not in owner.STAGE_AXIS_NAMES:
        return
    if not stage_position_panel.gui_coordinate_motion_editing_enabled(owner):
        owner._clear_exact_step_targets()
        owner._show_status("Selected Coordinate System is unavailable.", 3000)
        return
    mode = mode.strip().upper()
    if mode not in {"G90", "G91"}:
        owner._show_status(f"Unsupported manual move mode: {mode}.", 3000)
        return
    coordinate_snapshot = owner._stage_motion.snapshot()
    if owner.stage_controller.is_busy() and not coordinate_snapshot.coordinate_active:
        owner._show_status("Stage is busy. Ignoring manual axis move.", 3000)
        return
    motion_lease = coordinate_motion.gui_motion_lease(owner)
    active_basis = coordinate_snapshot.coordinate_display_basis
    same_display_basis = (
        motion_lease is None
        or active_basis == coordinate_motion.motion_basis(motion_lease)
    )
    baseline = (
        dict(coordinate_snapshot.coordinate_display_targets).get(axis)
        if same_display_basis
        else None
    )
    if baseline is None:
        baseline = owner._stage_axis_display_values.get(axis)
    if baseline is None:
        owner._show_status(f"{axis} coordinate is unavailable.", 3000)
        return
    current_pending = owner._exact_step_accumulator.targets.get(axis)
    try:
        display_target = (
            float(value_mm)
            if mode == "G90"
            else float(current_pending if current_pending is not None else baseline)
            + float(value_mm)
        )
    except (TypeError, ValueError):
        owner._show_status(f"Invalid {axis} target coordinate.", 3000)
        return
    if motion_lease is not None:
        allow_pose_rebase = coordinate_snapshot.coordinate_active
        stored_lease = getattr(owner, "_exact_step_motion_lease", None)
        candidate_targets = dict(owner._exact_step_accumulator.targets)
        candidate_targets[axis] = display_target
        projection = owner._project_gui_coordinate_motion(
            tuple(candidate_targets.items()),
            mode="G90",
            lease=stored_lease or motion_lease,
            allow_pose_rebase=allow_pose_rebase,
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
            return
        limit_error = coordinate_motion.projection_limit_error(owner, projection)
        raw_target = dict(projection.raw_targets).get(axis)
        if raw_target is None:
            owner._show_status(f"{axis} coordinate is unavailable.", 3000)
            return
        projected_lease = projection.lease
    else:
        raw_target = owner._raw_target_from_display_value(axis, display_target)
        if raw_target is None:
            owner._show_status(f"{axis} coordinate is unavailable.", 3000)
            return
        limit_error = owner._stage_axis_target_limit_error(axis, display_target)
    if limit_error is not None:
        owner._show_status(limit_error, 4000)
        return
    if motion_lease is not None:
        owner._exact_step_motion_lease = projected_lease
        if allow_pose_rebase:
            owner._exact_step_pose_rebase_allowed = True
    owner._exact_step_accumulator.set_absolute(axis, display_target)
    owner._exact_step_pending_axes.add(axis)
    owner._stage_motion.upsert_pending_coordinate_edit(
        axis,
        float(raw_target),
        display_target,
        motion_lease=(
            owner._exact_step_motion_lease if motion_lease is not None else None
        ),
    )
    panel = getattr(owner, "_stage_position_panel", None)
    if panel is not None:
        panel.set_pending_target(axis, float(raw_target), display_target)
    stage_position_panel.refresh_stage_axis_styles(owner)
    owner._update_stage_coordinate_apply_state()
    if not owner._exact_step_timer.isActive() and not owner._exact_step_window_elapsed:
        owner._exact_step_timer.start()


def dispatch_exact_step_targets(owner: Any) -> bool:
    """Dispatch the accumulated exact Step endpoint through one frozen lease."""

    if not owner._exact_step_window_elapsed:
        return False
    if not stage_position_panel.gui_coordinate_motion_editing_enabled(owner):
        owner._clear_exact_step_targets()
        return False
    if (
        owner._stage_motion.snapshot().coordinate_active
        or owner.stage_controller.is_busy()
    ):
        return False
    display_targets = dict(owner._exact_step_accumulator.targets)
    if not display_targets:
        owner._exact_step_window_elapsed = False
        owner._exact_step_motion_lease = None
        return False
    motion_lease = coordinate_motion.gui_motion_lease(owner)
    projected_machine_targets: dict[str, float] | None = None
    if motion_lease is not None:
        projection = owner._project_gui_coordinate_motion(
            tuple(display_targets.items()),
            mode="G90",
            lease=getattr(owner, "_exact_step_motion_lease", None) or motion_lease,
            allow_pose_rebase=bool(
                getattr(owner, "_exact_step_pose_rebase_allowed", False)
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
            owner._clear_exact_step_targets()
            return False
        limit_error = coordinate_motion.projection_limit_error(owner, projection)
        if limit_error is not None:
            owner._show_status(limit_error, 4000)
            owner._clear_exact_step_targets()
            return False
        targets = coordinate_motion.projection_targets(projection)
        projected_machine_targets = dict(projection.machine_targets)
        owner._exact_step_motion_lease = projection.lease
    else:
        targets = {}
        for axis, display_target in display_targets.items():
            raw_target = owner._raw_target_from_display_value(axis, display_target)
            if raw_target is None:
                owner._show_status(f"{axis} coordinate is unavailable.", 3000)
                owner._clear_exact_step_targets()
                return False
            limit_error = owner._stage_axis_target_limit_error(axis, display_target)
            if limit_error is not None:
                owner._show_status(limit_error, 4000)
                owner._clear_exact_step_targets()
                return False
            targets[axis] = (float(raw_target), float(display_target))
    owner._exact_step_accumulator.drain()
    owner._exact_step_window_elapsed = False
    accepted = owner._stage_motion.start_coordinate_move(
        CoordinateMoveRequest(
            targets=tuple(
                (axis, raw, display) for axis, (raw, display) in targets.items()
            ),
            seed_position=stage_position_update.seed_motion_prediction_position(owner),
            feedrate_mm_min=owner._coordinate_feedrate_for_axes(targets),
            source_label="Step",
            physical_limit_targets=tuple(
                (axis, target)
                for axis, target in (projected_machine_targets or {}).items()
            ),
            display_basis=coordinate_motion.motion_basis(
                getattr(owner, "_exact_step_motion_lease", None)
            ),
        ),
    )
    panel = getattr(owner, "_stage_position_panel", None)
    if panel is not None:
        for axis in targets:
            panel.pop_pending_target(axis)
    if accepted:
        owner._exact_step_pending_axes.difference_update(targets)
        owner._exact_step_pose_rebase_allowed = False
    else:
        owner._clear_exact_step_targets()
    return accepted


__all__ = ["dispatch_exact_step_targets", "on_manual_axis_move_requested"]
