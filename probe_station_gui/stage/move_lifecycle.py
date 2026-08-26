"""Stage move lifecycle and cancellation orchestration."""

from __future__ import annotations

import logging
from typing import Any

from probe_station_gui.coordinates.coordinator_model import (
    RegistrationAlignmentRequest,
)
from probe_station_gui.views import main_window_homing as homing_ui
from probe_station_gui.views import (
    main_window_stage_position_panel as stage_position_panel,
)


logger = logging.getLogger("main")


def has_application_cancelable_operation(owner: Any) -> bool:
    return (
        _threaded_operation_active(owner)
        or _capture_or_sample_active(owner)
        or _pending_ui_intent_active(owner)
    )


def _threaded_operation_active(owner: Any) -> bool:
    return (
        _thread_is_alive(getattr(owner, "_route_contact_move_thread", None))
        or owner._route_run_execution.snapshot().thread_alive
    )


def _capture_or_sample_active(owner: Any) -> bool:
    return (
        owner._surface_map_capture_running()
        or owner._microscope_scan_running()
        or owner._sample_handling_active()
    )


def _pending_ui_intent_active(owner: Any) -> bool:
    return (
        owner._manual_alignment_pick_slot is not None
        or owner._microscope_interaction.has_pending_move
        or bool(owner._pending_homing_axes)
        or owner._homing_active_key is not None
        or owner._pending_alignment_preparation is not None
        or owner._pending_quick_alignment_rotation
    )


def cancel_stage_coordinate_action(
    owner: Any,
    *,
    focus_reason: object,
) -> None:
    cancelled_any = _cancel_pending_ui_intents(owner)
    cancelled_any = _cancel_route_measurement(owner) or cancelled_any
    cancelled_any = _cancel_background_captures(owner) or cancelled_any
    outcome = owner._stage_motion.cancel_stage_motion()
    if outcome.stage_motion_cancelled:
        stage_position_panel.clear_stage_motion_axes(owner)
    panel = getattr(owner, "_stage_position_panel", None)
    if outcome.pending_edits_cleared and panel is not None:
        panel.clear_pending_targets(owner._stage_axis_display_values)
    if outcome.coordinate_priority:
        owner.view.setFocus(focus_reason)
        owner._schedule_cancel_state_refresh()
        return
    cancelled_any = outcome.stage_motion_cancelled or cancelled_any
    if outcome.pending_edits_cleared:
        owner.view.setFocus(focus_reason)
    if cancelled_any:
        owner.view.setFocus(focus_reason)
        owner._show_status("Cancel requested.", 3000)
        owner._schedule_cancel_state_refresh()
        return
    if outcome.pending_edits_cleared:
        owner._show_status("Cleared pending coordinate edits.", 2000)
        owner._schedule_cancel_state_refresh()


def on_move_finished(
    owner: Any,
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


def _cancel_pending_ui_intents(owner: Any) -> bool:
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


def _cancel_route_measurement(owner: Any) -> bool:
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


def _cancel_background_captures(owner: Any) -> bool:
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


def _finish_pending_alignment_preparation(
    owner: Any,
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
    owner: Any,
    success: bool,
) -> None:
    if not owner._pending_quick_alignment_rotation:
        return
    owner._pending_quick_alignment_rotation = False
    if success:
        owner._collapse_alignment_panel_if_design_open()


def _finish_target_cross(
    owner: Any,
    success: bool,
) -> None:
    owner._microscope_interaction.finish_move(success=success)
    if success:
        owner._schedule_status_refreshes(owner.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)


__all__ = [
    "cancel_stage_coordinate_action",
    "has_application_cancelable_operation",
    "on_move_finished",
]
