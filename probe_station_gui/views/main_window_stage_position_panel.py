"""Stage-position panel binding for the main window."""

from __future__ import annotations

from typing import Any, Protocol

from PySide6.QtCore import Qt

from probe_station_gui.stage.position_presenter import (
    coordinate_confidence_role,
    stage_position_display_plan,
)
from probe_station_gui.views.stage_position_panel import StagePositionPanel


class MainWindowStagePositionPanelOwner(Protocol):
    STAGE_AXIS_NAMES: tuple[str, ...]
    _stage_position_panel: Any
    _stage_axis_fields: Any
    _stage_unhomed_display_origins: dict[str, float]
    _stage_axis_raw_values: dict[str, float]
    _stage_axis_display_values: dict[str, float]
    _stage_axis_homed: set[str]
    _stage_limit_axes: set[str]
    _stage_axis_base_styles: dict[str, tuple[str, str]]
    _pending_stage_axis_targets: dict[str, tuple[float, float]]
    _stage_motion_axes: set[str]
    _stage_motion_blink_dimmed: bool
    _stage_motion_blink_timer: Any
    stage_controller: Any

    def _on_stage_axis_escape_pressed(self, axis_name: str) -> None: ...
    def _on_stage_axis_editing_finished(self, axis_name: str) -> bool | None: ...
    def _update_stage_coordinate_apply_state(self) -> None: ...
    def _on_stage_coordinate_mode_changed(self) -> None: ...
    def _apply_pending_stage_coordinate_targets(self) -> None: ...
    def _current_linear_feedrate(self) -> float: ...


def create_stage_position_widget(
    owner: MainWindowStagePositionPanelOwner,
) -> StagePositionPanel:
    panel = StagePositionPanel(owner.STAGE_AXIS_NAMES, owner)
    panel.axis_escape_pressed.connect(owner._on_stage_axis_escape_pressed)
    panel.axis_editing_finished.connect(owner._on_stage_axis_editing_finished)
    panel.axis_text_edited.connect(owner._update_stage_coordinate_apply_state)
    panel.input_mode_changed.connect(owner._on_stage_coordinate_mode_changed)
    panel.apply_requested.connect(owner._apply_pending_stage_coordinate_targets)
    panel.cancel_requested.connect(lambda: cancel_stage_coordinate_action(owner))
    owner._stage_position_panel = panel
    owner._stage_axis_fields = panel.axis_fields
    owner._stage_axis_base_styles = panel.base_styles
    owner._pending_stage_axis_targets = panel.pending_targets
    return panel


def display_axis_value_from_raw(
    owner: MainWindowStagePositionPanelOwner,
    axis_name: str,
    raw_value: float,
) -> float:
    converter = getattr(
        getattr(owner, "stage_controller", None),
        "calibrated_axis_display_value",
        None,
    )
    if not callable(converter):
        return float(raw_value)
    return converter(
        axis_name.strip().upper(),
        float(raw_value),
    )


def raw_axis_value_from_display(
    owner: MainWindowStagePositionPanelOwner,
    axis_name: str,
    display_value: float,
) -> float:
    converter = getattr(
        getattr(owner, "stage_controller", None),
        "calibrated_axis_raw_value",
        None,
    )
    if not callable(converter):
        return float(display_value)
    return converter(
        axis_name.strip().upper(),
        float(display_value),
    )


def refresh_stage_axis_styles(owner: MainWindowStagePositionPanelOwner) -> None:
    panel = getattr(owner, "_stage_position_panel", None)
    if panel is None:
        return
    panel.refresh_axis_styles(
        owner._stage_motion_axes,
        owner._stage_motion_blink_dimmed,
    )


def update_coordinate_confidence(
    owner: MainWindowStagePositionPanelOwner,
    confidence_updates: object,
) -> None:
    """Apply confidence signal changes without rebuilding the position fields."""

    panel = getattr(owner, "_stage_position_panel", None)
    if panel is None or not isinstance(confidence_updates, dict):
        return
    enabled_getter = getattr(
        getattr(owner, "stage_controller", None),
        "precision_approach_enabled_axes",
        None,
    )
    enabled_axes = enabled_getter() if callable(enabled_getter) else frozenset()
    roles = {
        str(axis).strip().upper(): coordinate_confidence_role(
            str(axis),
            precision_enabled_axes=enabled_axes,
            limit_axes=owner._stage_limit_axes,
            coordinate_confidence={str(axis).strip().upper(): confidence},
        )
        for axis, confidence in confidence_updates.items()
    }
    panel.update_confidence_roles(roles)


def set_stage_motion_axes(
    owner: MainWindowStagePositionPanelOwner,
    axes: object,
) -> None:
    if isinstance(axes, str):
        raw_axes = [axes]
    elif isinstance(axes, (set, list, tuple)):
        raw_axes = list(axes)
    else:
        raw_axes = []
    motion_axes = {
        str(axis).strip().upper()
        for axis in raw_axes
        if str(axis).strip().upper() in owner.STAGE_AXIS_NAMES
    }
    if not motion_axes:
        clear_stage_motion_axes(owner)
        return
    owner._stage_motion_axes = motion_axes
    owner._stage_motion_blink_dimmed = False
    blink_timer = getattr(owner, "_stage_motion_blink_timer", None)
    if blink_timer is not None and not blink_timer.isActive():
        blink_timer.start()
    refresh_stage_axis_styles(owner)


def clear_stage_motion_axes(owner: MainWindowStagePositionPanelOwner) -> None:
    blink_timer = getattr(owner, "_stage_motion_blink_timer", None)
    if blink_timer is not None and blink_timer.isActive():
        blink_timer.stop()
    motion_axes = getattr(owner, "_stage_motion_axes", None)
    blink_dimmed = bool(getattr(owner, "_stage_motion_blink_dimmed", False))
    if not motion_axes and not blink_dimmed:
        return
    if motion_axes is None:
        owner._stage_motion_axes = set()
    else:
        motion_axes.clear()
    owner._stage_motion_blink_dimmed = False
    refresh_stage_axis_styles(owner)


def advance_stage_motion_blink(owner: MainWindowStagePositionPanelOwner) -> None:
    if not owner._stage_motion_axes:
        owner._stage_motion_blink_timer.stop()
        owner._stage_motion_blink_dimmed = False
        return
    owner._stage_motion_blink_dimmed = not owner._stage_motion_blink_dimmed
    refresh_stage_axis_styles(owner)


def update_stage_position_display(
    owner: MainWindowStagePositionPanelOwner,
    position: object | None,
) -> None:
    panel = getattr(owner, "_stage_position_panel", None)
    controller = getattr(owner, "stage_controller", None)
    enabled_getter = getattr(controller, "precision_approach_enabled_axes", None)
    confidence_getter = getattr(controller, "coordinate_confidence", None)
    plan = stage_position_display_plan(
        position,
        axis_names=owner.STAGE_AXIS_NAMES,
        available_axes=owner._stage_axis_fields,
        homed_axes=(
            owner.stage_controller.homed_axes()
            if isinstance(position, tuple) and len(position) >= 2
            else set()
        ),
        limit_axes=owner._stage_limit_axes,
        pending_targets=owner._pending_stage_axis_targets,
        display_axis_value=lambda axis_name, raw_value: display_axis_value_from_raw(
            owner,
            axis_name,
            raw_value,
        ),
        feedrate_mm_min=owner._current_linear_feedrate(),
        precision_enabled_axes=(
            enabled_getter() if callable(enabled_getter) else frozenset()
        ),
        coordinate_confidence=(
            confidence_getter() if callable(confidence_getter) else None
        ),
    )
    if plan.reset_all:
        owner._stage_unhomed_display_origins.clear()
        owner._stage_axis_raw_values.clear()
        owner._stage_axis_display_values.clear()
        owner._stage_axis_homed.clear()
        owner._stage_axis_base_styles.clear()
        if panel is None:
            return
        panel.base_styles.clear()
        panel.pending_targets.clear()
        panel.return_commits.clear()
        clear_stage_motion_axes(owner)
        panel.set_fields_available(False)
        owner._update_stage_coordinate_apply_state()
        return
    owner._stage_axis_homed = set(plan.homed_axes)
    for axis_plan in plan.axis_updates:
        owner._stage_axis_raw_values[axis_plan.axis] = axis_plan.raw_value
        if axis_plan.axis not in plan.homed_axes:
            owner._stage_unhomed_display_origins.setdefault(
                axis_plan.axis,
                axis_plan.raw_value,
            )
        owner._stage_axis_base_styles[axis_plan.axis] = (
            axis_plan.base_background,
            axis_plan.base_foreground,
        )
        owner._stage_axis_display_values[axis_plan.axis] = axis_plan.display_value
    if panel is None:
        return
    panel.apply_display_plan(plan)
    if not plan.fields_available:
        panel.set_fields_available(False)
        owner._update_stage_coordinate_apply_state()
        return
    owner._update_stage_coordinate_apply_state()


def cancel_stage_coordinate_action(owner: MainWindowStagePositionPanelOwner) -> None:
    from probe_station_gui.stage import move_lifecycle as stage_move_lifecycle

    stage_move_lifecycle.cancel_stage_coordinate_action(
        owner,
        focus_reason=Qt.OtherFocusReason,
    )


__all__ = [
    "advance_stage_motion_blink",
    "cancel_stage_coordinate_action",
    "clear_stage_motion_axes",
    "create_stage_position_widget",
    "display_axis_value_from_raw",
    "raw_axis_value_from_display",
    "refresh_stage_axis_styles",
    "set_stage_motion_axes",
    "update_coordinate_confidence",
    "update_stage_position_display",
]
