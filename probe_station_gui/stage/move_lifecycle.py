"""Stage move lifecycle and cancellation orchestration."""

from __future__ import annotations

import logging
from typing import Any, Callable, Protocol

from probe_station_gui.views import main_window_homing as homing_ui
from probe_station_gui.views import main_window_stage_position_panel as stage_position_panel


logger = logging.getLogger("main")


class StageMoveLifecycleOwner(Protocol):
    MANUAL_JOG_SETTLE_POLL_DELAYS_MS: tuple[int, ...]
    _coordinate_targets: Any
    _pending_click_to_move: Any
    _manual_alignment_pick_slot: Any
    _pending_alignment_preparation: Any
    _pending_quick_alignment_rotation: bool
    _pending_homing_axes: Any
    _homing_active_key: Any
    _route_measurement_runner: Any
    _microscope_scan_stop_requested: Any
    _pending_planned_move_target_xy: tuple[float, float] | None
    _pending_planned_move_source_label: str | None
    _planned_move_started_at: float | None
    _planned_move_waiting_for_fresh_status: bool
    _planned_move_target_xy: tuple[float, float] | None
    _planned_move_origin_xy: tuple[float, float] | None
    _planned_move_stage_xy: tuple[float, float] | None
    _planned_move_ends_at: float | None
    _planned_move_stop_status_timestamp: float | None
    _pending_stage_axis_targets: dict[str, tuple[float, float]]
    _design_session: Any
    design_navigator_panel: Any
    microscope_scan_dialog: Any
    surface_map_window: Any
    view: Any
    stage_controller: Any

    def _controller_reports_active_motion(self) -> bool: ...
    def _surface_map_capture_running(self) -> bool: ...
    def _microscope_scan_running(self) -> bool: ...
    def _sample_handling_active(self) -> bool: ...
    def _clear_pending_click_to_move(self, *, clear_cross: bool) -> None: ...
    def _cancel_manual_alignment_pick(self) -> None: ...
    def _clear_planned_move_prediction(self, *, clear_wait_state: bool) -> None: ...
    def _clear_pending_stage_coordinate_targets(self) -> bool: ...
    def _schedule_status_refreshes(self, delays_ms: tuple[int, ...]) -> None: ...
    def _schedule_cancel_state_refresh(self) -> None: ...
    def _show_status(self, message: str, timeout_ms: int = 0) -> None: ...
    def _format_optional_point(self, point: tuple[float, float] | None) -> str: ...
    def _set_design_snap_enabled(self, enabled: bool) -> None: ...
    def _refresh_design_panel(self) -> None: ...
    def _refresh_design_position(self) -> None: ...
    def _collapse_alignment_panel_if_ready(self) -> None: ...
    def _collapse_alignment_panel_if_design_open(self) -> None: ...
    def _update_stage_coordinate_apply_state(self) -> None: ...


ScheduleSingleShot = Callable[[int, Callable[[], None]], Any]


def has_cancelable_operation(owner: StageMoveLifecycleOwner) -> bool:
    return (
        _stage_motion_cancelable(owner)
        or _threaded_operation_active(owner)
        or _capture_or_sample_active(owner)
        or _pending_ui_intent_active(owner)
    )


def _stage_motion_cancelable(owner: StageMoveLifecycleOwner) -> bool:
    controller_busy = (
        hasattr(owner, "stage_controller") and owner.stage_controller.is_busy()
    )
    return (
        owner._coordinate_targets.has_active_move()
        or controller_busy
        or owner._controller_reports_active_motion()
    )


def _threaded_operation_active(owner: StageMoveLifecycleOwner) -> bool:
    return (
        _thread_is_alive(getattr(owner, "_route_contact_move_thread", None))
        or _thread_is_alive(getattr(owner, "_route_measurement_thread", None))
    )


def _capture_or_sample_active(owner: StageMoveLifecycleOwner) -> bool:
    return (
        owner._surface_map_capture_running()
        or owner._microscope_scan_running()
        or owner._sample_handling_active()
    )


def _pending_ui_intent_active(owner: StageMoveLifecycleOwner) -> bool:
    return (
        owner._manual_alignment_pick_slot is not None
        or owner._pending_click_to_move is not None
        or bool(owner._pending_homing_axes)
        or owner._homing_active_key is not None
        or owner._pending_alignment_preparation is not None
        or owner._pending_quick_alignment_rotation
    )


def cancel_stage_coordinate_action(
    owner: StageMoveLifecycleOwner,
    *,
    focus_reason: object,
) -> None:
    cancelled_any = _cancel_pending_ui_intents(owner)
    cancelled_any = _cancel_route_measurement(owner) or cancelled_any
    cancelled_any = _cancel_background_captures(owner) or cancelled_any
    if _cancel_active_coordinate_move(owner, focus_reason=focus_reason):
        return
    cancelled_any = _cancel_controller_activity(owner) or cancelled_any
    cleared_edits = owner._clear_pending_stage_coordinate_targets()
    if cleared_edits:
        owner.view.setFocus(focus_reason)
    if cancelled_any:
        owner.view.setFocus(focus_reason)
        owner._show_status("Cancel requested.", 3000)
        owner._schedule_cancel_state_refresh()
        return
    if cleared_edits:
        owner._show_status("Cleared pending coordinate edits.", 2000)
        owner._schedule_cancel_state_refresh()


def clear_coordinate_move_tracking(
    owner: StageMoveLifecycleOwner,
    *,
    clear_pending: bool,
    reset_override: bool,
) -> None:
    owner._coordinate_targets.clear_tracking()
    if clear_pending:
        owner._pending_stage_axis_targets.clear()
    if reset_override:
        owner.stage_controller.queue_feed_override_reset()
    joystick_panel = getattr(owner, "joystick_panel", None)
    if joystick_panel is not None:
        joystick_panel.clear_temporary_linear_feedrate_bounds()
        if hasattr(joystick_panel, "clear_common_feedrate_target"):
            joystick_panel.clear_common_feedrate_target()
    stage_position_panel.refresh_stage_axis_styles(owner)
    owner._update_stage_coordinate_apply_state()


def finish_coordinate_move_if_idle(
    owner: StageMoveLifecycleOwner,
    position: object | None,
    *,
    monotonic_s: float,
    schedule_single_shot: ScheduleSingleShot,
) -> None:
    decision = owner._coordinate_targets.finish_if_idle_decision(
        latest_stage_state=owner.stage_controller.latest_stage_state(),
        position=position,
        monotonic_s=monotonic_s,
    )
    if not decision.finish:
        return
    if decision.stage_position is not None:
        owner._coordinate_targets.stage_position = decision.stage_position
    clear_coordinate_move_tracking(owner, clear_pending=False, reset_override=True)
    if owner._pending_homing_axes:
        schedule_single_shot(
            0,
            lambda: homing_ui.start_next_pending_homing_action(owner),
        )


def on_move_finished(
    owner: StageMoveLifecycleOwner,
    success: bool,
    message: str,
) -> None:
    message_lower = message.lower() if message else ""
    _clear_pending_planned_move_target(owner, success, message)
    _finish_planned_move_prediction(owner, success)
    if _finish_pending_alignment_preparation(owner, success, message):
        owner._schedule_status_refreshes(owner.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
        return
    _finish_quick_alignment_rotation(owner, success)
    if _handle_coordinate_feedrate_reissue_cancel(owner, success, message_lower):
        return
    owner._coordinate_targets.reissue_cancel_pending = False
    _finish_target_cross(owner, success)
    if _coordinate_tracking_should_clear(success, message_lower):
        clear_coordinate_move_tracking(
            owner,
            clear_pending=not success,
            reset_override=True,
        )
        stage_position_panel.clear_stage_motion_axes(owner)
    if message:
        owner._show_status(message, 5000)
    owner._schedule_cancel_state_refresh()


def _thread_is_alive(thread: object | None) -> bool:
    return thread is not None and thread.is_alive()


def _cancel_pending_ui_intents(owner: StageMoveLifecycleOwner) -> bool:
    cancelled_any = False
    if owner._pending_click_to_move is not None:
        owner._clear_pending_click_to_move(clear_cross=True)
        cancelled_any = True
    if owner._manual_alignment_pick_slot is not None:
        owner._cancel_manual_alignment_pick()
        cancelled_any = True
    if owner._pending_alignment_preparation is not None:
        owner._pending_alignment_preparation = None
        cancelled_any = True
    if owner._pending_quick_alignment_rotation:
        owner._pending_quick_alignment_rotation = False
        cancelled_any = True
    if owner._pending_homing_axes or owner._homing_active_key is not None:
        homing_ui.clear_pending_homing_queue(owner)
        cancelled_any = True
    return cancelled_any


def _cancel_route_measurement(owner: StageMoveLifecycleOwner) -> bool:
    runner = owner._route_measurement_runner
    if runner is None:
        return False
    runner.stop()
    if owner.design_navigator_panel is not None:
        owner.design_navigator_panel.set_route_measurement_waiting(False)
        owner.design_navigator_panel.set_route_measurement_status(
            "Route measurement cancel requested."
        )
    return True


def _cancel_background_captures(owner: StageMoveLifecycleOwner) -> bool:
    cancelled_any = False
    if owner._surface_map_capture_running():
        try:
            owner.surface_map_window.stop_capture()
        except Exception:
            logger.exception("Failed to stop surface map capture from Cancel.")
        cancelled_any = True
    if owner._microscope_scan_running():
        owner._microscope_scan_stop_requested.set()
        if owner.microscope_scan_dialog is not None:
            owner.microscope_scan_dialog.set_status(
                "Microscope scan stop requested."
            )
        cancelled_any = True
    return cancelled_any


def _cancel_active_coordinate_move(
    owner: StageMoveLifecycleOwner,
    *,
    focus_reason: object,
) -> bool:
    if not owner._coordinate_targets.has_active_move():
        return False
    owner.stage_controller.cancel_active_motion("Coordinate move cancel requested.")
    clear_coordinate_move_tracking(
        owner,
        clear_pending=True,
        reset_override=True,
    )
    stage_position_panel.clear_stage_motion_axes(owner)
    owner._clear_pending_stage_coordinate_targets()
    owner.view.setFocus(focus_reason)
    owner._schedule_status_refreshes(owner.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
    owner._schedule_cancel_state_refresh()
    return True


def _cancel_controller_activity(owner: StageMoveLifecycleOwner) -> bool:
    cancelled_any = False
    if owner._controller_reports_active_motion():
        owner.stage_controller.cancel_active_motion("Motion cancel requested.")
        stage_position_panel.clear_stage_motion_axes(owner)
        owner._clear_planned_move_prediction(clear_wait_state=True)
        cancelled_any = True
        owner._schedule_status_refreshes(owner.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
    if owner.stage_controller.is_busy():
        owner.stage_controller.cancel_active_task("Operation cancel requested.")
        stage_position_panel.clear_stage_motion_axes(owner)
        owner._clear_planned_move_prediction(clear_wait_state=True)
        cancelled_any = True
        owner._schedule_status_refreshes(owner.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
    return cancelled_any


def _clear_pending_planned_move_target(
    owner: StageMoveLifecycleOwner,
    success: bool,
    message: str,
) -> None:
    if owner._pending_planned_move_target_xy is None:
        return
    logger.debug(
        "MOTION PREDICTION planned_move_pending_cleared success=%s message=%s",
        success,
        message,
    )
    owner._pending_planned_move_target_xy = None
    owner._pending_planned_move_source_label = None


def _finish_planned_move_prediction(
    owner: StageMoveLifecycleOwner,
    success: bool,
) -> None:
    if (
        owner._planned_move_started_at is None
        and not owner._planned_move_waiting_for_fresh_status
    ):
        return
    logger.debug(
        "MOTION PREDICTION planned_move_finish success=%s stage=%s",
        success,
        owner._format_optional_point(owner._planned_move_stage_xy),
    )
    if not success:
        owner._clear_planned_move_prediction(clear_wait_state=True)
        return
    if owner._planned_move_target_xy is not None:
        owner._planned_move_stage_xy = owner._planned_move_target_xy
    owner._planned_move_origin_xy = None
    owner._planned_move_target_xy = None
    owner._planned_move_started_at = None
    owner._planned_move_ends_at = None
    owner._planned_move_waiting_for_fresh_status = (
        owner._planned_move_stage_xy is not None
    )
    owner._planned_move_stop_status_timestamp = (
        owner.stage_controller.last_status_timestamp()
    )


def _finish_pending_alignment_preparation(
    owner: StageMoveLifecycleOwner,
    success: bool,
    message: str,
) -> bool:
    if owner._pending_alignment_preparation is None:
        return False
    preparation = owner._pending_alignment_preparation
    owner._pending_alignment_preparation = None
    if success:
        owner._design_session.apply_prepared_alignment(preparation)
        owner._set_design_snap_enabled(False)
        owner._refresh_design_panel()
        owner._refresh_design_position()
        owner._collapse_alignment_panel_if_ready()
        owner.view.clear_target_cross()
        owner._show_status(
            "Design calibration complete. "
            f"Rotation {preparation.rotation_deg:+.3f} deg, "
            f"spacing ratio {preparation.distance_ratio:.3f}.",
            7000,
        )
    else:
        owner._show_status(
            f"Design calibration rotation failed: {message}",
            7000,
        )
    return True


def _finish_quick_alignment_rotation(
    owner: StageMoveLifecycleOwner,
    success: bool,
) -> None:
    if not owner._pending_quick_alignment_rotation:
        return
    owner._pending_quick_alignment_rotation = False
    if success:
        owner._collapse_alignment_panel_if_design_open()


def _handle_coordinate_feedrate_reissue_cancel(
    owner: StageMoveLifecycleOwner,
    success: bool,
    message_lower: str,
) -> bool:
    if (
        success
        or not owner._coordinate_targets.reissue_cancel_pending
        or "operation cancelled" not in message_lower
    ):
        return False
    owner._coordinate_targets.reissue_cancel_pending = False
    logger.debug(
        "Coordinate move worker cancelled for feedrate reissue; "
        "keeping coordinate tracking active."
    )
    owner._schedule_status_refreshes(owner.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
    owner._schedule_cancel_state_refresh()
    return True


def _finish_target_cross(
    owner: StageMoveLifecycleOwner,
    success: bool,
) -> None:
    if success:
        if owner._pending_click_to_move is None:
            owner.view.finish_target_motion_to_center()
            owner.view.clear_target_cross()
        owner._schedule_status_refreshes(owner.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
    elif owner._pending_click_to_move is None:
        owner.view.clear_target_cross()


def _coordinate_tracking_should_clear(success: bool, message_lower: str) -> bool:
    return (
        not success
        or "skipped" in message_lower
        or "already" in message_lower
        or "unchanged" in message_lower
    )


__all__ = [
    "cancel_stage_coordinate_action",
    "clear_coordinate_move_tracking",
    "finish_coordinate_move_if_idle",
    "has_cancelable_operation",
    "on_move_finished",
]
