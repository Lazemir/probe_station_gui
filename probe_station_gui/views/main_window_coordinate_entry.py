"""Coordinate field editing and Apply workflow for the Main window."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt

from probe_station_gui.stage.coordinate_targets import CoordinateMoveRequest
from probe_station_gui.stage import position_update as stage_position_update
from probe_station_gui.views import main_window_coordinate_motion as coordinate_motion
from probe_station_gui.views import (
    main_window_stage_position_panel as stage_position_panel,
)
from probe_station_gui.views.stage_position_panel import format_stage_axis_value


def on_stage_axis_editing_finished(
    owner: Any,
    axis_name: str,
) -> bool | None:
    """Validate one field against the same immutable lease used for rendering."""

    panel = getattr(owner, "_stage_position_panel", None)
    if panel is None or panel.is_programmatic_update:
        return None
    if not stage_position_panel.gui_coordinate_motion_editing_enabled(owner):
        panel.clear_pending_target_state()
        owner._stage_motion.clear_pending_coordinate_edits()
        owner._show_status("Selected Coordinate System is unavailable.", 3000)
        return False
    axis = axis_name.strip().upper()
    commit_from_return = panel.consume_return_commit(axis)
    field = panel.field(axis)
    if field is None or not field.isEnabled() or not field.isModified():
        return None

    def reject(message: str, timeout_ms: int) -> bool:
        panel.pop_pending_target(axis)
        owner._stage_motion.pop_pending_coordinate_edit(axis)
        panel.reset_axis_field(axis, owner._stage_axis_display_values.get(axis))
        stage_position_panel.refresh_stage_axis_styles(owner)
        owner._update_stage_coordinate_apply_state()
        owner._show_status(message, timeout_ms)
        return False

    text = field.text().strip().replace(",", ".")
    try:
        display_target = float(text)
    except (TypeError, ValueError):
        return reject(f"Invalid {axis} target coordinate.", 3000)
    input_mode = panel.selected_input_mode()
    motion_lease = coordinate_motion.gui_motion_lease(owner)
    if motion_lease is not None:
        if input_mode == "G91":
            baseline = owner._stage_axis_display_values.get(axis)
            if baseline is None:
                return reject(f"{axis} coordinate is unavailable.", 3000)
            resolved_display_target = float(baseline) + display_target
        else:
            resolved_display_target = display_target
        candidate_targets = {
            pending_axis: display
            for pending_axis, _raw, display in (
                owner._stage_motion.pending_coordinate_edits().targets
            )
        }
        candidate_targets[axis] = resolved_display_target
        projection = owner._project_gui_coordinate_motion(
            tuple(candidate_targets.items()),
            mode="G90",
            lease=(
                owner._stage_motion.pending_coordinate_edits().motion_lease
                or motion_lease
            ),
        )
        if projection is None or not projection.accepted:
            return reject(
                (
                    projection.reason
                    if projection is not None
                    else "Coordinate movement is unavailable."
                ),
                4000,
            )
        raw_target = dict(projection.raw_targets).get(axis)
        if raw_target is None:
            return reject(f"{axis} coordinate is unavailable.", 3000)
        limit_error = coordinate_motion.projection_limit_error(owner, projection)
        projected_lease = projection.lease
    else:
        raw_target, resolved_display_target = owner._resolve_stage_axis_target(
            axis,
            display_target,
            input_mode,
        )
        if raw_target is None:
            return reject(f"{axis} coordinate is unavailable.", 3000)
        limit_error = owner._stage_axis_target_limit_error(
            axis,
            resolved_display_target,
        )
    if limit_error is not None:
        return reject(limit_error, 4000)
    owner._stage_motion.upsert_pending_coordinate_edit(
        axis,
        raw_target,
        resolved_display_target,
        motion_lease=(projected_lease if motion_lease is not None else None),
    )
    field.blockSignals(True)
    field.setText(format_stage_axis_value(display_target))
    field.setModified(False)
    if commit_from_return:
        field.clearFocus()
    field.blockSignals(False)
    if commit_from_return:
        owner.view.setFocus(Qt.OtherFocusReason)
    panel.set_pending_target(axis, raw_target, resolved_display_target)
    stage_position_panel.refresh_stage_axis_styles(owner)
    owner._update_stage_coordinate_apply_state()
    return True


def apply_pending_stage_coordinate_targets(owner: Any) -> None:
    """Start all edited fields in the one selected-system motion lease."""

    if not stage_position_panel.gui_coordinate_motion_editing_enabled(owner):
        owner._stage_motion.clear_pending_coordinate_edits()
        panel = getattr(owner, "_stage_position_panel", None)
        if panel is not None:
            panel.clear_pending_target_state()
        owner._update_stage_coordinate_apply_state()
        owner._show_status("Selected Coordinate System is unavailable.", 3000)
        return
    had_error = False
    for axis in owner.STAGE_AXIS_NAMES:
        field = owner._stage_axis_fields.get(axis)
        if field is not None and field.isEnabled() and field.isModified():
            if on_stage_axis_editing_finished(owner, axis) is False:
                had_error = True
    if had_error:
        owner._update_stage_coordinate_apply_state()
        return
    pending = owner._stage_motion.pending_coordinate_edits()
    if not pending.targets:
        owner._show_status("No coordinate changes to apply.", 2000)
        return
    if (
        owner._stage_motion.snapshot().coordinate_active
        or owner.stage_controller.is_busy()
    ):
        owner._show_status("Stage is busy. Ignoring coordinate targets.", 3000)
        owner._update_stage_coordinate_apply_state()
        return
    motion_lease = coordinate_motion.gui_motion_lease(owner)
    projected_machine_targets: dict[str, float] | None = None
    if motion_lease is not None:
        display_targets = tuple(
            (axis, display) for axis, _raw, display in pending.targets
        )
        projection = owner._project_gui_coordinate_motion(
            display_targets,
            mode="G90",
            lease=(pending.motion_lease or motion_lease),
        )
        if projection is None or not projection.accepted:
            owner._stage_motion.clear_pending_coordinate_edits()
            panel = getattr(owner, "_stage_position_panel", None)
            if panel is not None:
                panel.clear_pending_target_state()
            owner._show_status(
                (
                    projection.reason
                    if projection is not None
                    else "Coordinate movement is unavailable."
                ),
                4000,
            )
            owner._update_stage_coordinate_apply_state()
            return
        limit_error = coordinate_motion.projection_limit_error(owner, projection)
        if limit_error is not None:
            owner._show_status(limit_error, 4000)
            return
        targets = coordinate_motion.projection_targets(projection)
        projected_machine_targets = dict(projection.machine_targets)
    else:
        targets = {axis: (raw, display) for axis, raw, display in pending.targets}
    owner.view.setFocus(Qt.OtherFocusReason)
    set_joystick_control_mode_for_coordinate_apply(owner)
    feedrate = owner._coordinate_feedrate_for_axes(targets)
    owner._stage_motion.start_coordinate_move(
        CoordinateMoveRequest(
            targets=tuple(
                (axis, raw, display) for axis, (raw, display) in targets.items()
            ),
            seed_position=stage_position_update.seed_motion_prediction_position(owner),
            feedrate_mm_min=feedrate,
            source_label="coordinate fields",
            physical_limit_targets=tuple(
                (axis, target)
                for axis, target in (projected_machine_targets or {}).items()
            ),
            display_basis=(
                coordinate_motion.motion_basis(projection.lease)
                if motion_lease is not None
                else None
            ),
        ),
    )
    panel = getattr(owner, "_stage_position_panel", None)
    if panel is not None:
        remaining_axes = {
            axis
            for axis, _raw, _display in (
                owner._stage_motion.pending_coordinate_edits().targets
            )
        }
        for axis in targets:
            if axis not in remaining_axes:
                panel.pop_pending_target(axis)
    owner._update_stage_coordinate_apply_state()


def set_joystick_control_mode_for_coordinate_apply(owner: Any) -> None:
    joystick = owner.joystick_panel
    if joystick is None:
        return
    setter = getattr(joystick, "set_control_mode", None)
    if callable(setter):
        try:
            setter("jog", emit_changed=True)
            return
        except TypeError:
            setter("jog")
            return
    private_setter = getattr(joystick, "_set_control_mode", None)
    if callable(private_setter):
        private_setter("jog", emit_changed=True)


__all__ = [
    "apply_pending_stage_coordinate_targets",
    "on_stage_axis_editing_finished",
]
