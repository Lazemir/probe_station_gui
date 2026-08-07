"""Stage-position panel binding for the main window."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any, Protocol

from PySide6.QtCore import Qt

from probe_station_gui.coordinates.model import (
    PhysicalMachinePose,
    STAGE_AXES,
    VISIBLE_STAGE_AXES,
)
from probe_station_gui.coordinates.presentation import (
    MACHINE_FRAME_ID,
    build_coordinate_display_plan,
    decide_pending_frame_restore,
)
from probe_station_gui.coordinates.rotation_geometry import rotation_geometry_snapshot
from probe_station_gui.stage.position_presenter import (
    coordinate_confidence_role,
    stage_position_display_plan,
)
from probe_station_gui.views.stage_position_panel import StagePositionPanel


logger = logging.getLogger(__name__)


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
    panel = StagePositionPanel(VISIBLE_STAGE_AXES, owner)
    panel.axis_escape_pressed.connect(owner._on_stage_axis_escape_pressed)
    panel.axis_editing_finished.connect(owner._on_stage_axis_editing_finished)
    panel.axis_text_edited.connect(owner._update_stage_coordinate_apply_state)
    panel.input_mode_changed.connect(owner._on_stage_coordinate_mode_changed)
    panel.apply_requested.connect(owner._apply_pending_stage_coordinate_targets)
    panel.cancel_requested.connect(lambda: cancel_stage_coordinate_action(owner))
    selection_handler = getattr(owner, "_on_software_coordinate_system_changed", None)
    panel.coordinate_system_changed.connect(
        selection_handler
        if callable(selection_handler)
        else lambda frame_id: select_gui_coordinate_frame(owner, frame_id)
    )
    owner._stage_position_panel = panel
    owner._stage_axis_fields = panel.axis_fields
    owner._stage_axis_base_styles = panel.base_styles
    owner._pending_stage_axis_targets = panel.pending_targets
    initialize_gui_coordinate_selection(owner)
    return panel


def initialize_gui_coordinate_selection(owner: MainWindowStagePositionPanelOwner) -> None:
    """Start in Machine while retaining an eligible persisted restore candidate."""

    owner._selected_coordinate_frame_id = MACHINE_FRAME_ID
    settings = getattr(getattr(owner, "settings_manager", None), "settings", None)
    software = getattr(settings, "software_coordinates", None)
    last_selected = str(
        getattr(software, "last_selected_frame_id", MACHINE_FRAME_ID)
        or MACHINE_FRAME_ID
    )
    owner._pending_coordinate_frame_restore_id = (
        None if last_selected == MACHINE_FRAME_ID else last_selected
    )


def _persist_gui_coordinate_selection(owner: object, frame_id: str) -> None:
    manager = getattr(owner, "settings_manager", None)
    update_in_memory = getattr(manager, "set_software_coordinate_selection", None)
    store = getattr(owner, "_software_coordinate_selection_store", None)
    publish = getattr(store, "publish", None)
    try:
        snapshot = None
        if callable(update_in_memory):
            snapshot = update_in_memory(frame_id)
        if callable(publish) and snapshot is not None:
            publish(snapshot)
    except Exception:
        logger.exception("Software coordinate selection submission failed")
        show_status = getattr(owner, "_show_status", None)
        if callable(show_status):
            show_status("Coordinate selection could not be saved.", 6000)


def _coordinate_pivot(owner: object) -> tuple[float, float]:
    settings = getattr(getattr(owner, "settings_manager", None), "settings", None)
    return rotation_geometry_snapshot(settings.software_coordinates).pivot_machine_xy


def _coerce_physical_machine_pose(value: object) -> PhysicalMachinePose:
    if isinstance(value, PhysicalMachinePose):
        return value
    try:
        value_type = type(value)
        if (
            value_type.__name__ != "PhysicalMachinePose"
            or value_type.__module__ != "probe_station_gui.coordinates.model"
        ):
            return PhysicalMachinePose({})
        values = getattr(value, "values")
        if not isinstance(values, Mapping):
            return PhysicalMachinePose({})
        copied_values = dict(values)
        axes = set(copied_values)
        if axes not in (set(VISIBLE_STAGE_AXES), set(STAGE_AXES)):
            return PhysicalMachinePose({})
        return PhysicalMachinePose.from_mapping(copied_values)
    except Exception:
        return PhysicalMachinePose({})


def update_software_coordinate_display(
    owner: MainWindowStagePositionPanelOwner,
    physical_pose: PhysicalMachinePose | object,
) -> None:
    """Resolve GUI-local selection and apply one pure software-frame plan."""

    physical_pose = _coerce_physical_machine_pose(physical_pose)
    if physical_pose is None:
        return
    registry = getattr(owner, "_coordinate_frame_registry", None)
    if registry is None or not bool(getattr(owner, "_coordinate_frames_loaded", False)):
        return
    snapshot = registry.snapshot()
    homed_getter = getattr(getattr(owner, "stage_controller", None), "homed_axes", None)
    homed_axes = homed_getter() if callable(homed_getter) else set()
    authority_axes = set(physical_pose.values)

    pending = getattr(owner, "_pending_coordinate_frame_restore_id", None)
    if isinstance(pending, str) and pending:
        restore = decide_pending_frame_restore(
            snapshot,
            frame_id=pending,
            homed_axes=homed_axes,
            authority_axes=authority_axes,
        )
        if restore is not None:
            owner._selected_coordinate_frame_id = restore
            owner._pending_coordinate_frame_restore_id = None

    selected = str(
        getattr(owner, "_selected_coordinate_frame_id", MACHINE_FRAME_ID)
        or MACHINE_FRAME_ID
    )
    try:
        pivot = _coordinate_pivot(owner)
    except (AttributeError, TypeError, ValueError) as exc:
        selected = MACHINE_FRAME_ID
        owner._selected_coordinate_frame_id = MACHINE_FRAME_ID
        pivot = (float("nan"), float("nan"))
        show_status = getattr(owner, "_show_status", None)
        if callable(show_status):
            show_status(str(exc), 6000)
    plan = build_coordinate_display_plan(
        snapshot,
        selected_frame_id=selected,
        physical_pose=physical_pose,
        pivot_machine_xy=pivot,
        homed_axes=homed_axes,
        authority_axes=authority_axes,
    )
    if plan.selected_frame_id != selected:
        owner._selected_coordinate_frame_id = plan.selected_frame_id
        _persist_gui_coordinate_selection(owner, plan.selected_frame_id)
    owner._stage_axis_display_values = {
        update.axis: float(update.value)
        for update in plan.axis_updates
        if update.value is not None
    }
    panel = getattr(owner, "_stage_position_panel", None)
    if panel is not None:
        panel.set_coordinate_display_plan(plan)


def refresh_coordinate_frame_display(owner: MainWindowStagePositionPanelOwner) -> None:
    pose = _coerce_physical_machine_pose(
        getattr(owner, "_latest_physical_machine_pose", None)
    )
    if pose is not None:
        update_software_coordinate_display(owner, pose)


def select_gui_coordinate_frame(
    owner: MainWindowStagePositionPanelOwner,
    frame_id: str,
) -> None:
    """Apply one explicit GUI selection without touching API coordinate state."""

    requested = str(frame_id or MACHINE_FRAME_ID)
    pose = _coerce_physical_machine_pose(
        getattr(owner, "_latest_physical_machine_pose", None)
    )
    if requested != MACHINE_FRAME_ID:
        registry = getattr(owner, "_coordinate_frame_registry", None)
        if (
            registry is None
            or registry.get(requested) is None
            or pose is None
        ):
            requested = MACHINE_FRAME_ID
        else:
            homed_getter = getattr(owner.stage_controller, "homed_axes", None)
            try:
                pivot = _coordinate_pivot(owner)
            except (AttributeError, TypeError, ValueError) as exc:
                show_status = getattr(owner, "_show_status", None)
                if callable(show_status):
                    show_status(str(exc), 6000)
                requested = MACHINE_FRAME_ID
                pivot = (float("nan"), float("nan"))
            plan = build_coordinate_display_plan(
                registry.snapshot(),
                selected_frame_id=requested,
                physical_pose=pose,
                pivot_machine_xy=pivot,
                homed_axes=homed_getter() if callable(homed_getter) else set(),
                authority_axes=set(pose.values),
            )
            entry = next(
                (item for item in plan.selector_entries if item.frame_id == requested),
                None,
            )
            if entry is not None and not entry.enabled:
                return
            requested = plan.selected_frame_id
    owner._pending_coordinate_frame_restore_id = None
    owner._selected_coordinate_frame_id = requested
    if pose is not None:
        update_software_coordinate_display(owner, pose)
    _persist_gui_coordinate_selection(owner, requested)


def gui_coordinate_motion_editing_enabled(owner: object) -> bool:
    """Return whether legacy position-field/Step targets are Machine-valued."""

    return str(
        getattr(owner, "_selected_coordinate_frame_id", MACHINE_FRAME_ID)
        or MACHINE_FRAME_ID
    ) == MACHINE_FRAME_ID


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
) -> float | None:
    checked_converter = getattr(
        getattr(owner, "stage_controller", None),
        "calibrated_axis_raw_target_value",
        None,
    )
    if callable(checked_converter):
        return checked_converter(
            axis_name.strip().upper(),
            float(display_value),
        )
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
        pose = _coerce_physical_machine_pose(
            getattr(owner, "_latest_physical_machine_pose", None)
        )
        if pose is not None:
            update_software_coordinate_display(owner, pose)
        return
    panel.apply_display_plan(plan)
    if not plan.fields_available:
        panel.set_fields_available(False)
        owner._update_stage_coordinate_apply_state()
        return
    pose = _coerce_physical_machine_pose(
        getattr(owner, "_latest_physical_machine_pose", None)
    )
    if pose is not None:
        update_software_coordinate_display(owner, pose)
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
    "refresh_coordinate_frame_display",
    "initialize_gui_coordinate_selection",
    "select_gui_coordinate_frame",
    "set_stage_motion_axes",
    "update_coordinate_confidence",
    "update_software_coordinate_display",
    "update_stage_position_display",
]
