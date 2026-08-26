"""Stage move lifecycle and cancellation orchestration."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from probe_station_gui.coordinates.coordinator_model import (
    RegistrationAlignmentRequest,
)
from probe_station_gui.stage.exact_step import ExactStepClearReason
from probe_station_gui.views import main_window_homing as homing_ui
from probe_station_gui.views import (
    main_window_stage_position_panel as stage_position_panel,
)


logger = logging.getLogger("main")


class ClickMoveLifecycle(Protocol):
    @property
    def has_pending_move(self) -> bool: ...

    def cancel_pending(self, *, clear_target: bool) -> None: ...
    def finish_move(self, *, success: bool) -> None: ...
    def clear_target(self) -> None: ...


class StageMoveLifecycleOwner(Protocol):
    MANUAL_JOG_SETTLE_POLL_DELAYS_MS: tuple[int, ...]
    _microscope_interaction: ClickMoveLifecycle
    _manual_alignment_pick_slot: Any
    _pending_alignment_preparation: Any
    _pending_quick_alignment_rotation: bool
    _pending_homing_axes: Any
    _homing_active_key: Any
    _route_run_execution: Any
    _microscope_scan_stop_requested: Any
    _stage_motion: Any
    _coordinate_system_coordinator: Any
    design_navigator_panel: Any
    microscope_scan_dialog: Any
    surface_map_window: Any
    view: Any
    stage_controller: Any

    def _controller_reports_active_motion(self) -> bool: ...
    def _surface_map_capture_running(self) -> bool: ...
    def _microscope_scan_running(self) -> bool: ...
    def _sample_handling_active(self) -> bool: ...
    def _cancel_manual_alignment_pick(self) -> None: ...
    def _schedule_status_refreshes(self, delays_ms: tuple[int, ...]) -> None: ...
    def _schedule_cancel_state_refresh(self) -> None: ...
    def _show_status(self, message: str, timeout_ms: int = 0) -> None: ...
    def _set_design_snap_enabled(self, enabled: bool) -> None: ...
    def _refresh_design_panel(self) -> None: ...
    def _refresh_design_position(self) -> None: ...
    def _collapse_alignment_panel_if_ready(self) -> None: ...
    def _collapse_alignment_panel_if_design_open(self) -> None: ...
    def _finish_alignment_draft(self) -> None: ...
    def _update_stage_coordinate_apply_state(self) -> None: ...


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
        owner._stage_motion.snapshot().coordinate_active
        or controller_busy
        or owner._controller_reports_active_motion()
    )


def _threaded_operation_active(owner: StageMoveLifecycleOwner) -> bool:
    return (
        _thread_is_alive(getattr(owner, "_route_contact_move_thread", None))
        or owner._route_run_execution.snapshot().thread_alive
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
        or owner._microscope_interaction.has_pending_move
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
    owner._stage_motion.clear_exact_steps(ExactStepClearReason.CANCEL_REQUESTED)
    cancelled_any = _cancel_pending_ui_intents(owner)
    cancelled_any = _cancel_route_measurement(owner) or cancelled_any
    cancelled_any = _cancel_background_captures(owner) or cancelled_any
    if _cancel_active_coordinate_move(owner, focus_reason=focus_reason):
        return
    cancelled_any = _cancel_controller_activity(owner) or cancelled_any
    cleared_edits = owner._stage_motion.clear_pending_coordinate_edits()
    panel = getattr(owner, "_stage_position_panel", None)
    if cleared_edits and panel is not None:
        panel.clear_pending_targets(owner._stage_axis_display_values)
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


def on_move_finished(
    owner: StageMoveLifecycleOwner,
    success: bool,
    message: str,
) -> None:
    if _finish_pending_alignment_preparation(owner, success, message):
        owner._schedule_status_refreshes(owner.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
        return
    _finish_quick_alignment_rotation(owner, success)
    _finish_target_cross(owner, success)
    if message:
        owner._show_status(message, 5000)
    owner._schedule_cancel_state_refresh()


def _thread_is_alive(thread: object | None) -> bool:
    return thread is not None and thread.is_alive()


def _cancel_pending_ui_intents(owner: StageMoveLifecycleOwner) -> bool:
    cancelled_any = False
    if owner._microscope_interaction.has_pending_move:
        owner._microscope_interaction.cancel_pending(clear_target=True)
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
    runner = owner._route_run_execution.snapshot().runner
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
            owner.microscope_scan_dialog.set_status("Microscope scan stop requested.")
        cancelled_any = True
    return cancelled_any


def _cancel_active_coordinate_move(
    owner: StageMoveLifecycleOwner,
    *,
    focus_reason: object,
) -> bool:
    if not owner._stage_motion.snapshot().coordinate_active:
        return False
    owner._stage_motion.cancel_coordinate_move()
    stage_position_panel.clear_stage_motion_axes(owner)
    panel = getattr(owner, "_stage_position_panel", None)
    if panel is not None:
        panel.clear_pending_targets(owner._stage_axis_display_values)
    owner.view.setFocus(focus_reason)
    owner._schedule_status_refreshes(owner.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
    owner._schedule_cancel_state_refresh()
    return True


def _cancel_controller_activity(owner: StageMoveLifecycleOwner) -> bool:
    cancelled_any = False
    if owner._controller_reports_active_motion():
        owner.stage_controller.cancel_active_motion("Motion cancel requested.")
        stage_position_panel.clear_stage_motion_axes(owner)
        owner._stage_motion.cancel_planned_xy_move()
        cancelled_any = True
        owner._schedule_status_refreshes(owner.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
    if owner.stage_controller.is_busy():
        owner.stage_controller.cancel_active_task("Operation cancel requested.")
        stage_position_panel.clear_stage_motion_axes(owner)
        owner._stage_motion.cancel_planned_xy_move()
        cancelled_any = True
        owner._schedule_status_refreshes(owner.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
    return cancelled_any


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
        transition = owner._coordinate_system_coordinator.apply_registration_alignment(
            RegistrationAlignmentRequest(preparation)
        )
        from probe_station_gui.views import main_window_coordinate_flow

        main_window_coordinate_flow.apply_coordinate_transition(owner, transition)
        owner._finish_alignment_draft()
        owner._set_design_snap_enabled(False)
        owner._refresh_design_panel()
        owner._refresh_design_position()
        owner._collapse_alignment_panel_if_ready()
        owner._microscope_interaction.clear_target()
        owner._show_status(
            "Design calibration complete. "
            f"Rotation {preparation.rotation_deg:+.3f} deg, "
            f"spacing ratio {preparation.distance_ratio:.3f}. "
            f"RMS {preparation.rms_residual_mm:.4f} mm, "
            f"max {preparation.max_residual_mm:.4f} mm.",
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


def _finish_target_cross(
    owner: StageMoveLifecycleOwner,
    success: bool,
) -> None:
    owner._microscope_interaction.finish_move(success=success)
    if success:
        owner._schedule_status_refreshes(owner.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)


__all__ = [
    "cancel_stage_coordinate_action",
    "has_cancelable_operation",
    "on_move_finished",
]
