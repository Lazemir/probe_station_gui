from __future__ import annotations

from dataclasses import dataclass
import types

from probe_station_gui.coordinates.coordinator_model import (
    CoordinateSystemSnapshot,
    CoordinateTransition,
)
from probe_station_gui.stage import move_lifecycle
from tests.app.route_run_execution_support import (
    activate_route_run,
    install_route_run_execution,
)


AXES = ("X", "Y", "Z", "A", "B", "C")


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


class _StageMotionBoundary:
    """Only the typed session seam exercised by cancellation integration."""

    def __init__(self, controller: _StageController) -> None:
        self._controller = controller
        self.coordinate_active = False
        self.pending: dict[str, tuple[float, float]] = {}
        self.cancel_planned_calls = 0

    def snapshot(self) -> object:
        return types.SimpleNamespace(coordinate_active=self.coordinate_active)

    def pending_coordinate_edits(self) -> object:
        return types.SimpleNamespace(
            targets=tuple(
                (axis, raw, display) for axis, (raw, display) in self.pending.items()
            )
        )

    def clear_pending_coordinate_edits(self) -> bool:
        had_pending = bool(self.pending)
        self.pending.clear()
        return had_pending

    def cancel_coordinate_move(self) -> bool:
        if not self.coordinate_active:
            return False
        self._controller.cancel_active_motion("Coordinate move cancel requested.")
        self.coordinate_active = False
        self.pending.clear()
        return True

    def cancel_planned_xy_move(self) -> None:
        self.cancel_planned_calls += 1


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
        self._manual_alignment_pick_slot = None
        self._pending_alignment_preparation = None
        self._pending_quick_alignment_rotation = False
        self._pending_homing_axes: list[str] = []
        self._homing_active_key = None
        install_route_run_execution(self)
        self._route_contact_move_thread = None
        self.surface_map_window = None
        self.microscope_scan_dialog = None
        self._surface_map_running = False
        self._microscope_scan_running_value = False
        self._microscope_scan_stop_requested = _StopEvent()
        self._stage_motion = _StageMotionBoundary(self.stage_controller)
        self._design_session = types.SimpleNamespace(
            applied=[],
            apply_prepared_alignment=lambda preparation: (
                self._design_session.applied.append(preparation)
            ),
        )
        self._coordinate_system_coordinator = types.SimpleNamespace(
            apply_registration_alignment=lambda request: (
                self._design_session.applied.append(request.preparation)
                or CoordinateTransition(CoordinateSystemSnapshot(False, (), None))
            )
        )
        self.design_navigator_panel = None
        self.statuses: list[tuple[str, int]] = []
        self.status_refreshes: list[tuple[int, ...]] = []
        self.cancel_refreshes = 0
        self.stage_motion_clears = 0
        self.pending_stage_coordinate_clears = 0
        self.axis_style_refreshes = 0
        self.apply_state_refreshes = 0
        self.coordinate_move_completions: list[
            tuple[bool, dict[str, float], object | None]
        ] = []
        self.homing_starts = 0
        self.design_snap_updates: list[bool] = []
        self.design_panel_refreshes = 0
        self.design_position_refreshes = 0
        self.alignment_panel_collapses = 0
        self.quick_alignment_collapses = 0
        self.finished_alignment_drafts = 0
        self._stage_motion_axes: set[str] = set()
        self._stage_axis_display_values: dict[str, float] = {}
        self._stage_motion_blink_dimmed = False
        self._stage_motion_blink_timer = types.SimpleNamespace(
            isActive=lambda: False,
            stop=lambda: None,
            start=lambda: None,
        )
        self._stage_position_panel = types.SimpleNamespace(
            refresh_axis_styles=lambda _axes, _dimmed: (
                self._refresh_stage_axis_styles()
            ),
            clear_pending_targets=lambda _display_values: (
                self._stage_motion.clear_pending_coordinate_edits()
            ),
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

    def _on_coordinate_move_finished(
        self,
        success: bool,
        finished_display_targets: dict[str, float],
        finished_display_basis: object | None,
    ) -> None:
        self.coordinate_move_completions.append(
            (
                bool(success),
                dict(finished_display_targets),
                finished_display_basis,
            )
        )

    def _start_next_pending_homing_action(self) -> None:
        self.homing_starts += 1


def test_has_cancelable_operation_reports_active_coordinate_move() -> None:
    owner = _Owner()
    owner._stage_motion.coordinate_active = True

    assert move_lifecycle.has_cancelable_operation(owner) is True


def test_coordinate_cancel_has_priority_over_generic_busy_task() -> None:
    owner = _Owner()
    owner._stage_motion.coordinate_active = True
    owner._stage_motion.pending["X"] = (5.0, 5.0)
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
    assert owner._stage_motion.pending == {}
    assert owner._stage_motion.coordinate_active is False


def test_route_cancel_runs_before_active_coordinate_move_early_return() -> None:
    owner = _Owner()
    runner = _Runner()
    design_panel = _DesignNavigatorPanel()
    activate_route_run(owner, runner)
    owner.design_navigator_panel = design_panel
    owner._stage_motion.coordinate_active = True

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
    owner._stage_motion.pending["Y"] = (2.0, 2.0)

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


def test_move_finish_alignment_preparation_returns_before_normal_finish_cleanup() -> (
    None
):
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


def test_unclaimed_move_finish_clears_cross_then_reports_status() -> None:
    owner = _Owner()

    move_lifecycle.on_move_finished(owner, False, "Limit reached.")

    assert owner._microscope_interaction.finish_calls == [False]
    assert owner.statuses == [("Limit reached.", 5000)]
    assert owner.cancel_refreshes == 1
