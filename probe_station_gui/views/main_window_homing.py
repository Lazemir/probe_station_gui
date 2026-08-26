"""Main-window homing and limit-axis UI orchestration."""

from __future__ import annotations

from typing import Any, Protocol

from PySide6.QtCore import QTimer

from probe_station_gui.stage.exact_step import ExactStepClearReason
from probe_station_gui.stage.motion_prediction import position_with_stage_xy
from probe_station_gui.views import main_window_coordinate_step as coordinate_step
from probe_station_gui.views import (
    main_window_stage_position_panel as stage_position_panel,
)


VALID_HOME_AXES = {"X", "Y", "Z", "A"}
HOMING_RETRY_DELAY_MS = 200


class MainWindowHomingOwner(Protocol):
    STAGE_AXIS_NAMES: tuple[str, ...]
    _stage_limit_axes: set[str]
    _stage_motion: Any
    _pending_homing_axes: list[str]
    _homing_active_key: str | None
    _stage_motion: Any
    joystick_panel: Any
    stage_controller: Any

    def _controller_latest_state_blocks_motion(self) -> bool: ...
    def _update_stage_coordinate_apply_state(self) -> None: ...


def on_limit_axes_changed(owner: MainWindowHomingOwner, axes: object) -> None:
    owner._stage_limit_axes = _normalized_limit_axes(owner, axes)
    predicted_position = _manual_jog_predicted_position(owner)
    display_position = (
        predicted_position
        if predicted_position is not None
        else owner.stage_controller.latest_stage_position()
    )
    stage_position_panel.update_stage_position_display(owner, display_position)
    stage_position_panel.refresh_coordinate_frame_display(owner)


def on_homing_status_changed(
    owner: MainWindowHomingOwner,
    _homed_axes: object,
) -> None:
    predicted_position = _manual_jog_predicted_position(owner)
    motion = owner._stage_motion.snapshot()
    if predicted_position is not None:
        display_position = predicted_position
    elif motion.planned_stage_xy is not None and (
        motion.planned_prediction_active or motion.planned_waiting_for_fresh_status
    ):
        display_position = motion.presented_position or position_with_stage_xy(
            owner.stage_controller.latest_stage_position(),
            motion.planned_stage_xy,
        )
    else:
        display_position = owner.stage_controller.latest_stage_position()
    stage_position_panel.update_stage_position_display(owner, display_position)
    from probe_station_gui.views import main_window_coordinate_flow

    main_window_coordinate_flow.observe_coordinate_authority(owner)


def request_home_axis_from_ui(owner: MainWindowHomingOwner, axis: str) -> None:
    axis_name = axis.strip().upper()
    if axis_name not in VALID_HOME_AXES:
        return
    coordinate_step.clear_exact_steps(owner, ExactStepClearReason.HOMING_REQUESTED)
    queue_or_start_homing_axes(owner, [axis_name])


def request_home_all_from_ui(owner: MainWindowHomingOwner) -> None:
    coordinate_step.clear_exact_steps(owner, ExactStepClearReason.HOMING_REQUESTED)
    if owner.stage_controller.request_home_all():
        owner._pending_homing_axes.clear()
        refresh_pending_homing_ui(owner)


def queue_or_start_homing_axes(
    owner: MainWindowHomingOwner,
    axes: list[str],
) -> None:
    normalized = _queued_axes(owner, axes)
    if not normalized:
        return
    if _can_start_homing_now(owner):
        first_axis = normalized.pop(0)
        if not owner.stage_controller.request_home_axis(first_axis):
            normalized.insert(0, first_axis)
    owner._pending_homing_axes.extend(normalized)
    refresh_pending_homing_ui(owner)
    owner._update_stage_coordinate_apply_state()
    if owner._pending_homing_axes and owner._homing_active_key is None:
        QTimer.singleShot(
            HOMING_RETRY_DELAY_MS,
            lambda: start_next_pending_homing_action(owner),
        )


def start_next_pending_homing_action(owner: MainWindowHomingOwner) -> None:
    if owner._homing_active_key is not None or not owner._pending_homing_axes:
        return
    if (
        owner.stage_controller.is_busy()
        or owner._stage_motion.snapshot().coordinate_active
    ):
        QTimer.singleShot(
            HOMING_RETRY_DELAY_MS,
            lambda: start_next_pending_homing_action(owner),
        )
        return
    if owner._controller_latest_state_blocks_motion():
        QTimer.singleShot(
            HOMING_RETRY_DELAY_MS,
            lambda: start_next_pending_homing_action(owner),
        )
        return
    axis = owner._pending_homing_axes.pop(0)
    refresh_pending_homing_ui(owner)
    if not owner.stage_controller.request_home_axis(axis):
        owner._pending_homing_axes.insert(0, axis)
        refresh_pending_homing_ui(owner)
        QTimer.singleShot(
            HOMING_RETRY_DELAY_MS,
            lambda: start_next_pending_homing_action(owner),
        )


def clear_pending_homing_queue(owner: MainWindowHomingOwner) -> None:
    owner._homing_active_key = None
    owner._pending_homing_axes.clear()
    refresh_pending_homing_ui(owner)
    owner._update_stage_coordinate_apply_state()


def refresh_pending_homing_ui(owner: MainWindowHomingOwner) -> None:
    joystick_panel = getattr(owner, "joystick_panel", None)
    set_pending_homing_actions = getattr(
        joystick_panel,
        "set_pending_homing_actions",
        None,
    )
    if callable(set_pending_homing_actions):
        set_pending_homing_actions(set(owner._pending_homing_axes))


def on_homing_action_finished(
    owner: MainWindowHomingOwner,
    success: bool,
    _message: str,
    axis_key: str,
) -> None:
    key = axis_key.strip().upper()
    if key == owner._homing_active_key or key == "ALL":
        owner._homing_active_key = None
    if not success:
        owner._pending_homing_axes.clear()
        refresh_pending_homing_ui(owner)
        stage_position_panel.clear_stage_motion_axes(owner)
        owner._update_stage_coordinate_apply_state()
        return
    from probe_station_gui.views import main_window_coordinate_flow

    main_window_coordinate_flow.observe_coordinate_authority(owner)
    stage_position_panel.clear_stage_motion_axes(owner)
    refresh_pending_homing_ui(owner)
    owner._update_stage_coordinate_apply_state()
    if owner._pending_homing_axes:
        QTimer.singleShot(0, lambda: start_next_pending_homing_action(owner))


def on_homing_action_started(owner: MainWindowHomingOwner, axis_key: str) -> None:
    key = axis_key.strip().upper()
    owner._homing_active_key = key
    if key in owner._pending_homing_axes:
        owner._pending_homing_axes.remove(key)
        refresh_pending_homing_ui(owner)
    if key == "ALL":
        stage_position_panel.set_stage_motion_axes(owner, {"X", "Y", "Z", "A"})
    elif key in owner.STAGE_AXIS_NAMES:
        stage_position_panel.set_stage_motion_axes(owner, {key})
    owner._update_stage_coordinate_apply_state()


def on_needles_action_started(owner: MainWindowHomingOwner, _action: str) -> None:
    stage_position_panel.set_stage_motion_axes(owner, {"A"})
    owner._update_stage_coordinate_apply_state()


def on_needles_action_finished(
    owner: MainWindowHomingOwner,
    _success: bool,
    _message: str,
    _action: str,
) -> None:
    stage_position_panel.clear_stage_motion_axes(owner)
    owner._update_stage_coordinate_apply_state()


def _normalized_limit_axes(
    owner: MainWindowHomingOwner,
    axes: object,
) -> set[str]:
    if not isinstance(axes, (set, list, tuple)):
        return set()
    return {
        axis_name
        for axis in axes
        if (axis_name := str(axis).strip().upper()) in owner.STAGE_AXIS_NAMES
    }


def _manual_jog_predicted_position(owner: MainWindowHomingOwner) -> object | None:
    motion = owner._stage_motion.snapshot()
    if motion.manual_prediction_available:
        return motion.presented_position
    return None


def _queued_axes(
    owner: MainWindowHomingOwner,
    axes: list[str],
) -> list[str]:
    normalized: list[str] = []
    for axis in axes:
        axis_name = axis.strip().upper()
        if axis_name not in VALID_HOME_AXES:
            continue
        if axis_name == owner._homing_active_key:
            continue
        if axis_name in owner._pending_homing_axes:
            continue
        normalized.append(axis_name)
    return normalized


def _can_start_homing_now(owner: MainWindowHomingOwner) -> bool:
    return (
        owner._homing_active_key is None
        and not owner.stage_controller.is_busy()
        and not owner._controller_latest_state_blocks_motion()
        and not owner._stage_motion.snapshot().coordinate_active
    )
