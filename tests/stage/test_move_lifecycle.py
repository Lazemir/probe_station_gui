from __future__ import annotations

from dataclasses import dataclass
import types

from probe_station_gui.stage import move_lifecycle
from probe_station_gui.stage.coordinate_targets import (
    CoordinateTargetConfig,
    CoordinateTargetMoveState,
)


AXES = ("X", "Y", "Z", "A", "B", "C")


def _coordinate_targets() -> CoordinateTargetMoveState:
    return CoordinateTargetMoveState(
        CoordinateTargetConfig(
            axis_names=AXES,
            min_feedrate_mm_min=1.0,
            duration_padding_s=0.0,
            min_idle_accept_s=0.0,
            target_tolerance_mm=0.01,
        )
    )


class _StageController:
    def __init__(self) -> None:
        self.busy = False
        self.state = "Idle"
        self.cancelled_tasks: list[str] = []
        self.cancelled_motions: list[str] = []
        self.feed_override_resets = 0
        self.last_status_time = 11.0
        self.home_axis_requests: list[str] = []

    def is_busy(self) -> bool:
        return self.busy

    def latest_stage_state(self) -> str:
        return self.state

    def last_status_timestamp(self) -> float:
        return self.last_status_time

    def cancel_active_task(self, reason: str) -> None:
        self.cancelled_tasks.append(reason)

    def cancel_active_motion(self, reason: str) -> None:
        self.cancelled_motions.append(reason)

    def queue_feed_override_reset(self) -> None:
        self.feed_override_resets += 1

    def request_home_axis(self, axis: str) -> bool:
        self.home_axis_requests.append(str(axis))
        return True


class _View:
    def __init__(self) -> None:
        self.focus_reasons: list[object] = []

    def setFocus(self, reason: object) -> None:  # noqa: N802 - Qt naming
        self.focus_reasons.append(reason)



class _MicroscopeInteraction:
    def __init__(self) -> None:
        self.has_pending_move = False
        self.cancel_calls: list[bool] = []
        self.finish_calls: list[bool] = []
        self.clear_calls = 0

    def cancel_pending(self, *, clear_target: bool) -> None:
        self.has_pending_move = False
        self.cancel_calls.append(clear_target)

    def finish_move(self, *, success: bool) -> None:
        self.finish_calls.append(success)

    def clear_target(self) -> None:
        self.clear_calls += 1


class _JoystickPanel:
    def __init__(self) -> None:
        self.temporary_bounds_cleared = 0
        self.common_targets_cleared = 0

    def clear_temporary_linear_feedrate_bounds(self) -> None:
        self.temporary_bounds_cleared += 1

    def clear_common_feedrate_target(self) -> None:
        self.common_targets_cleared += 1


class _Runner:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


class _DesignNavigatorPanel:
    def __init__(self) -> None:
        self.waiting: list[bool] = []
        self.statuses: list[str] = []

    def set_route_measurement_waiting(self, waiting: bool) -> None:
        self.waiting.append(bool(waiting))

    def set_route_measurement_status(self, status: str) -> None:
        self.statuses.append(status)


class _SurfaceMapWindow:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop_capture(self) -> None:
        self.stop_calls += 1


class _StopEvent:
    def __init__(self) -> None:
        self.set_calls = 0

    def set(self) -> None:
        self.set_calls += 1


class _MicroscopeScanDialog:
    def __init__(self) -> None:
        self.statuses: list[str] = []

    def set_status(self, status: str) -> None:
        self.statuses.append(status)


@dataclass
class _Preparation:
    rotation_deg: float = 1.25
    distance_ratio: float = 0.875
    rms_residual_mm: float = 0.0123
    max_residual_mm: float = 0.0456


class _Owner:
    STAGE_AXIS_NAMES = AXES
    MANUAL_JOG_SETTLE_POLL_DELAYS_MS = (40, 120)

    def __init__(self) -> None:
        self.stage_controller = _StageController()
        self.view = _View()
        self._microscope_interaction = _MicroscopeInteraction()
        self.joystick_panel = _JoystickPanel()
        self._coordinate_targets = _coordinate_targets()
        self._manual_alignment_pick_slot = None
        self._pending_alignment_preparation = None
        self._pending_quick_alignment_rotation = False
        self._pending_homing_axes: list[str] = []
        self._homing_active_key = None
        self._route_measurement_runner = None
        self._route_contact_move_thread = None
        self._route_measurement_thread = None
        self.surface_map_window = None
        self.microscope_scan_dialog = None
        self._surface_map_running = False
        self._microscope_scan_running_value = False
        self._microscope_scan_stop_requested = _StopEvent()
        self._pending_planned_move_target_xy = None
        self._pending_planned_move_source_label = None
        self._planned_move_started_at = None
        self._planned_move_waiting_for_fresh_status = False
        self._planned_move_target_xy = None
        self._planned_move_origin_xy = None
        self._planned_move_stage_xy = None
        self._planned_move_ends_at = None
        self._planned_move_stop_status_timestamp = None
        self._pending_stage_axis_targets: dict[str, tuple[float, float]] = {}
        self._design_session = types.SimpleNamespace(
            applied=[],
            apply_prepared_alignment=lambda preparation: self._design_session.applied.append(
                preparation
            ),
        )
        self.design_navigator_panel = None
        self.statuses: list[tuple[str, int]] = []
        self.status_refreshes: list[tuple[int, ...]] = []
        self.cancel_refreshes = 0
        self.stage_motion_clears = 0
        self.planned_prediction_clears: list[bool] = []
        self.pending_stage_coordinate_clears = 0
        self.axis_style_refreshes = 0
        self.apply_state_refreshes = 0
        self.homing_starts = 0
        self.design_snap_updates: list[bool] = []
        self.design_panel_refreshes = 0
        self.design_position_refreshes = 0
        self.alignment_panel_collapses = 0
        self.quick_alignment_collapses = 0
        self.finished_alignment_drafts = 0
        self._stage_motion_axes: set[str] = set()
        self._stage_motion_blink_dimmed = False
        self._stage_motion_blink_timer = types.SimpleNamespace(
            isActive=lambda: False,
            stop=lambda: None,
            start=lambda: None,
        )
        self._stage_position_panel = types.SimpleNamespace(
            refresh_axis_styles=lambda _axes, _dimmed: self._refresh_stage_axis_styles()
        )

    def _controller_reports_active_motion(self) -> bool:
        return self.stage_controller.latest_stage_state().lower() in {"run", "jog"}

    def _controller_latest_state_blocks_motion(self) -> bool:
        return False

    def _surface_map_capture_running(self) -> bool:
        return self._surface_map_running

    def _microscope_scan_running(self) -> bool:
        return self._microscope_scan_running_value

    def _sample_handling_active(self) -> bool:
        return False

    def _cancel_manual_alignment_pick(self) -> None:
        self._manual_alignment_pick_slot = None

    def _clear_pending_homing_queue(self) -> None:
        self._pending_homing_axes.clear()
        self._homing_active_key = None

    def _clear_stage_motion_axes(self) -> None:
        self.stage_motion_clears += 1

    def _clear_planned_move_prediction(self, *, clear_wait_state: bool) -> None:
        self.planned_prediction_clears.append(clear_wait_state)
        self._planned_move_started_at = None

    def _clear_pending_stage_coordinate_targets(self) -> bool:
        self.pending_stage_coordinate_clears += 1
        had_targets = bool(self._pending_stage_axis_targets)
        self._pending_stage_axis_targets.clear()
        return had_targets

    def _schedule_status_refreshes(self, delays_ms: tuple[int, ...]) -> None:
        self.status_refreshes.append(tuple(delays_ms))

    def _schedule_cancel_state_refresh(self) -> None:
        self.cancel_refreshes += 1

    def _show_status(self, message: str, timeout_ms: int = 0) -> None:
        self.statuses.append((message, int(timeout_ms)))

    def _format_optional_point(self, point: tuple[float, float] | None) -> str:
        return "None" if point is None else f"{point[0]:.3f},{point[1]:.3f}"

    def _set_design_snap_enabled(self, enabled: bool) -> None:
        self.design_snap_updates.append(enabled)

    def _refresh_design_panel(self) -> None:
        self.design_panel_refreshes += 1

    def _refresh_design_position(self) -> None:
        self.design_position_refreshes += 1

    def _collapse_alignment_panel_if_ready(self) -> None:
        self.alignment_panel_collapses += 1

    def _collapse_alignment_panel_if_design_open(self) -> None:
        self.quick_alignment_collapses += 1

    def _finish_alignment_draft(self) -> None:
        self.finished_alignment_drafts += 1

    def _refresh_stage_axis_styles(self) -> None:
        self.axis_style_refreshes += 1

    def _update_stage_coordinate_apply_state(self) -> None:
        self.apply_state_refreshes += 1

    def _start_next_pending_homing_action(self) -> None:
        self.homing_starts += 1


def test_has_cancelable_operation_reports_active_coordinate_move() -> None:
    owner = _Owner()
    owner._coordinate_targets.active_axis = "X"

    assert move_lifecycle.has_cancelable_operation(owner) is True


def test_coordinate_cancel_has_priority_over_generic_busy_task() -> None:
    owner = _Owner()
    owner._coordinate_targets.active_axis = "X"
    owner._coordinate_targets.active_axes = {"X"}
    owner._pending_stage_axis_targets["X"] = (5.0, 5.0)
    owner.stage_controller.busy = True

    move_lifecycle.cancel_stage_coordinate_action(owner, focus_reason="focus")

    assert owner.stage_controller.cancelled_motions == [
        "Coordinate move cancel requested."
    ]
    assert owner.stage_controller.cancelled_tasks == []
    assert owner.statuses == []
    assert owner.view.focus_reasons == ["focus"]
    assert owner.status_refreshes == [owner.MANUAL_JOG_SETTLE_POLL_DELAYS_MS]
    assert owner.cancel_refreshes == 1
    assert owner._pending_stage_axis_targets == {}
    assert owner._coordinate_targets.has_active_move() is False


def test_route_cancel_runs_before_active_coordinate_move_early_return() -> None:
    owner = _Owner()
    runner = _Runner()
    design_panel = _DesignNavigatorPanel()
    owner._route_measurement_runner = runner
    owner.design_navigator_panel = design_panel
    owner._coordinate_targets.active_axis = "X"
    owner._coordinate_targets.active_axes = {"X"}

    move_lifecycle.cancel_stage_coordinate_action(owner, focus_reason="focus")

    assert runner.stop_calls == 1
    assert design_panel.waiting == [False]
    assert design_panel.statuses == ["Route measurement cancel requested."]
    assert owner.stage_controller.cancelled_motions == [
        "Coordinate move cancel requested."
    ]
    assert owner.statuses == []


def test_background_capture_cancel_reports_generic_cancel_status() -> None:
    owner = _Owner()
    surface_map = _SurfaceMapWindow()
    scan_dialog = _MicroscopeScanDialog()
    owner.surface_map_window = surface_map
    owner.microscope_scan_dialog = scan_dialog
    owner._surface_map_running = True
    owner._microscope_scan_running_value = True

    move_lifecycle.cancel_stage_coordinate_action(owner, focus_reason="focus")

    assert surface_map.stop_calls == 1
    assert owner._microscope_scan_stop_requested.set_calls == 1
    assert scan_dialog.statuses == ["Microscope scan stop requested."]
    assert owner.statuses == [("Cancel requested.", 3000)]
    assert owner.view.focus_reasons == ["focus"]


def test_pending_edits_only_cancel_clears_edits_and_reports_edit_status() -> None:
    owner = _Owner()
    owner._pending_stage_axis_targets["Y"] = (2.0, 2.0)

    move_lifecycle.cancel_stage_coordinate_action(owner, focus_reason="focus")

    assert owner.stage_controller.cancelled_motions == []
    assert owner.stage_controller.cancelled_tasks == []
    assert owner.view.focus_reasons == ["focus"]
    assert owner.statuses == [("Cleared pending coordinate edits.", 2000)]
    assert owner.cancel_refreshes == 1


def test_global_cancel_clears_pending_click_through_interaction_seam() -> None:
    owner = _Owner()
    owner._microscope_interaction.has_pending_move = True

    move_lifecycle.cancel_stage_coordinate_action(owner, focus_reason="focus")

    assert owner._microscope_interaction.cancel_calls == [True]
    assert owner.statuses == [("Cancel requested.", 3000)]
    assert owner.view.focus_reasons == ["focus"]


def test_move_finish_success_completes_planned_move_prediction_wait_state() -> None:
    owner = _Owner()
    owner._pending_planned_move_target_xy = (4.0, 5.0)
    owner._pending_planned_move_source_label = "design"
    owner._planned_move_started_at = 2.0
    owner._planned_move_origin_xy = (1.0, 1.0)
    owner._planned_move_target_xy = (4.0, 5.0)
    owner._planned_move_ends_at = 3.0

    move_lifecycle.on_move_finished(owner, True, "Done.")

    assert owner._pending_planned_move_target_xy is None
    assert owner._pending_planned_move_source_label is None
    assert owner._planned_move_stage_xy == (4.0, 5.0)
    assert owner._planned_move_origin_xy is None
    assert owner._planned_move_target_xy is None
    assert owner._planned_move_started_at is None
    assert owner._planned_move_ends_at is None
    assert owner._planned_move_waiting_for_fresh_status is True
    assert owner._planned_move_stop_status_timestamp == 11.0


def test_move_finish_failure_clears_planned_move_prediction() -> None:
    owner = _Owner()
    owner._planned_move_started_at = 2.0

    move_lifecycle.on_move_finished(owner, False, "Failed.")

    assert owner.planned_prediction_clears == [True]


def test_move_finish_alignment_preparation_returns_before_normal_finish_cleanup() -> None:
    owner = _Owner()
    preparation = _Preparation()
    owner._pending_alignment_preparation = preparation

    move_lifecycle.on_move_finished(owner, True, "Done.")

    assert owner._design_session.applied == [preparation]
    assert owner.design_snap_updates == [False]
    assert owner.design_panel_refreshes == 1
    assert owner.design_position_refreshes == 1
    assert owner.alignment_panel_collapses == 1
    assert owner.finished_alignment_drafts == 1
    assert owner._microscope_interaction.clear_calls == 1
    assert owner.status_refreshes == [owner.MANUAL_JOG_SETTLE_POLL_DELAYS_MS]
    assert owner.cancel_refreshes == 0
    assert owner.statuses == [
        (
            "Design calibration complete. Rotation +1.250 deg, spacing ratio 0.875. "
            "RMS 0.0123 mm, max 0.0456 mm.",
            7000,
        )
    ]


def test_clear_coordinate_move_tracking_applies_pending_override_and_ui_cleanup() -> None:
    owner = _Owner()
    owner._coordinate_targets.active_axis = "Z"
    owner._pending_stage_axis_targets["Z"] = (3.0, 3.0)

    move_lifecycle.clear_coordinate_move_tracking(
        owner,
        clear_pending=True,
        reset_override=True,
    )

    assert owner._coordinate_targets.has_active_move() is False
    assert owner._pending_stage_axis_targets == {}
    assert owner.stage_controller.feed_override_resets == 1
    assert owner.joystick_panel.temporary_bounds_cleared == 1
    assert owner.joystick_panel.common_targets_cleared == 1
    assert owner.axis_style_refreshes == 1
    assert owner.apply_state_refreshes == 1


def test_move_finish_feedrate_reissue_cancel_keeps_tracking_and_suppresses_status() -> None:
    owner = _Owner()
    owner._coordinate_targets.active_axis = "X"
    owner._coordinate_targets.active_axes = {"X"}
    owner._coordinate_targets.reissue_cancel_pending = True

    move_lifecycle.on_move_finished(owner, False, "Operation cancelled.")

    assert owner._coordinate_targets.has_active_move() is True
    assert owner._coordinate_targets.reissue_cancel_pending is False
    assert owner.statuses == []
    assert owner.status_refreshes == [owner.MANUAL_JOG_SETTLE_POLL_DELAYS_MS]
    assert owner.cancel_refreshes == 1


def test_move_finish_normal_failure_clears_tracking_and_cross_then_reports_status() -> None:
    owner = _Owner()
    owner._coordinate_targets.active_axis = "X"

    move_lifecycle.on_move_finished(owner, False, "Limit reached.")

    assert owner._microscope_interaction.finish_calls == [False]
    assert owner._coordinate_targets.has_active_move() is False
    assert owner._stage_motion_axes == set()
    assert owner.statuses == [("Limit reached.", 5000)]
    assert owner.cancel_refreshes == 1


def test_move_finish_success_with_skipped_message_clears_coordinate_tracking() -> None:
    owner = _Owner()
    owner._coordinate_targets.active_axis = "X"

    move_lifecycle.on_move_finished(owner, True, "Move skipped.")

    assert owner._microscope_interaction.finish_calls == [True]
    assert owner._coordinate_targets.has_active_move() is False
    assert owner.status_refreshes == [owner.MANUAL_JOG_SETTLE_POLL_DELAYS_MS]
    assert owner.statuses == [("Move skipped.", 5000)]


def test_finish_coordinate_move_if_idle_schedules_pending_homing_action() -> None:
    owner = _Owner()
    owner._coordinate_targets.active_axis = "X"
    owner._coordinate_targets.active_axes = {"X"}
    owner._coordinate_targets.target_position = (1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    owner._coordinate_targets.started_at = 1.0
    owner._pending_homing_axes = ["Z"]
    scheduled: list[int] = []

    def schedule(delay_ms: int, callback) -> None:
        scheduled.append(delay_ms)
        callback()

    move_lifecycle.finish_coordinate_move_if_idle(
        owner,
        (1.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        monotonic_s=2.0,
        schedule_single_shot=schedule,
    )

    assert owner._coordinate_targets.has_active_move() is False
    assert scheduled == [0]
    assert owner.stage_controller.home_axis_requests == ["Z"]
