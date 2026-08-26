from __future__ import annotations

import types

from probe_station_gui.application.status_coordinate_ui import (
    _MainStatusCoordinateUiMixin,
)
from probe_station_gui.application.stage_motion_types import StageMotionCancelOutcome
from probe_station_gui.stage.exact_step import ExactStepClearReason
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
    def __init__(self, events: list[str] | None = None) -> None:
        self._events = events
        self.has_pending_move = False
        self.cancel_calls: list[bool] = []
        self.finish_calls: list[bool] = []
        self.clear_calls = 0

    def cancel_pending(self, *, clear_target: bool) -> None:
        if self._events is not None:
            self._events.append("pending-ui")
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

    def __init__(self, controller: _StageController, events: list[str]) -> None:
        self._controller = controller
        self._events = events
        self.coordinate_active = False
        self.pending: dict[str, tuple[float, float]] = {}
        self.cancel_planned_calls = 0
        self.exact_clear_reasons: list[ExactStepClearReason] = []
        self.alignment_pending = False

    def snapshot(self) -> object:
        return types.SimpleNamespace(
            coordinate_active=self.coordinate_active,
            cancelable=bool(self.coordinate_active or self._controller.is_busy()),
        )

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

    def cancel_stage_motion(self) -> StageMotionCancelOutcome:
        self._events.append("stage")
        self.exact_clear_reasons.append(ExactStepClearReason.CANCEL_REQUESTED)
        coordinate_priority = self.coordinate_active
        if coordinate_priority:
            self._controller.cancel_active_motion("Coordinate move cancel requested.")
            self._controller.queue_feed_override_reset()
            self.coordinate_active = False
            cancelled = True
        elif self._controller.is_busy():
            self._controller.cancel_active_task("Operation cancel requested.")
            self.cancel_planned_calls += 1
            cancelled = True
        else:
            cancelled = False
        pending_edits_cleared = self.clear_pending_coordinate_edits()
        return StageMotionCancelOutcome(
            stage_motion_cancelled=cancelled,
            coordinate_priority=coordinate_priority,
            pending_edits_cleared=pending_edits_cleared,
        )

    def alignment_rotation_pending(self) -> bool:
        return self.alignment_pending

    def discard_alignment_rotation(self) -> bool:
        was_pending = self.alignment_pending
        self.alignment_pending = False
        return was_pending


class _Runner:
    def __init__(self, events: list[str] | None = None) -> None:
        self.stop_calls = 0
        self._events = events

    def stop(self) -> None:
        if self._events is not None:
            self._events.append("route")
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
    def __init__(self, events: list[str] | None = None) -> None:
        self.stop_calls = 0
        self._events = events

    def stop_capture(self) -> None:
        if self._events is not None:
            self._events.append("surface")
        self.stop_calls += 1


class _StopEvent:
    def __init__(self, events: list[str] | None = None) -> None:
        self._events = events
        self.set_calls = 0

    def set(self) -> None:
        if self._events is not None:
            self._events.append("scan")
        self.set_calls += 1


class _MicroscopeScanDialog:
    def __init__(self) -> None:
        self.statuses: list[str] = []

    def set_status(self, status: str) -> None:
        self.statuses.append(status)


class _Owner(_MainStatusCoordinateUiMixin):
    STAGE_AXIS_NAMES = AXES
    MANUAL_JOG_SETTLE_POLL_DELAYS_MS = (40, 120)

    def __init__(self) -> None:
        self.cancel_order: list[str] = []
        self.stage_controller = _StageController()
        self.view = _View()
        self._microscope_interaction = _MicroscopeInteraction(self.cancel_order)
        self.joystick_panel = _JoystickPanel()
        self._manual_alignment_pick_slot = None
        self._pending_homing_axes: list[str] = []
        self._homing_active_key = None
        install_route_run_execution(self)
        self._route_contact_move_thread = None
        self.surface_map_window = None
        self.microscope_scan_dialog = None
        self._surface_map_running = False
        self._microscope_scan_running_value = False
        self._microscope_scan_stop_requested = _StopEvent(self.cancel_order)
        self._stage_motion = _StageMotionBoundary(
            self.stage_controller,
            self.cancel_order,
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


def test_application_cancelability_excludes_session_motion_state() -> None:
    owner = _Owner()
    owner._stage_motion.coordinate_active = True

    assert (
        _MainStatusCoordinateUiMixin._has_application_cancelable_operation(owner)
        is False
    )
    owner._microscope_interaction.has_pending_move = True
    assert (
        _MainStatusCoordinateUiMixin._has_application_cancelable_operation(owner)
        is True
    )


def test_coordinate_cancel_has_priority_over_generic_busy_task() -> None:
    owner = _Owner()
    owner._stage_motion.coordinate_active = True
    owner._stage_motion.pending["X"] = (5.0, 5.0)
    owner.stage_controller.busy = True

    _MainStatusCoordinateUiMixin._cancel_stage_coordinate_action(
        owner, focus_reason="focus"
    )

    assert owner._stage_motion.exact_clear_reasons == [
        ExactStepClearReason.CANCEL_REQUESTED
    ]
    assert owner.stage_controller.cancelled_motions == [
        "Coordinate move cancel requested."
    ]
    assert owner.stage_controller.cancelled_tasks == []
    assert owner.statuses == []
    assert owner.view.focus_reasons == ["focus"]
    assert owner.status_refreshes == []
    assert owner.cancel_refreshes == 1
    assert owner._stage_motion.pending == {}
    assert owner._stage_motion.coordinate_active is False


def test_route_cancel_runs_before_active_coordinate_move_early_return() -> None:
    owner = _Owner()
    runner = _Runner(owner.cancel_order)
    design_panel = _DesignNavigatorPanel()
    activate_route_run(owner, runner)
    owner.design_navigator_panel = design_panel
    owner._stage_motion.coordinate_active = True

    _MainStatusCoordinateUiMixin._cancel_stage_coordinate_action(
        owner, focus_reason="focus"
    )

    assert runner.stop_calls == 1
    assert design_panel.waiting == [False]
    assert design_panel.statuses == ["Route measurement cancel requested."]
    assert owner.stage_controller.cancelled_motions == [
        "Coordinate move cancel requested."
    ]
    assert owner.statuses == []
    assert owner.cancel_order == ["route", "stage"]


def test_global_cancel_preserves_all_application_to_stage_slice_order() -> None:
    owner = _Owner()
    runner = _Runner(owner.cancel_order)
    activate_route_run(owner, runner)
    owner._microscope_interaction.has_pending_move = True
    owner.surface_map_window = _SurfaceMapWindow(owner.cancel_order)
    owner._surface_map_running = True
    owner._microscope_scan_running_value = True
    owner._stage_motion.coordinate_active = True

    _MainStatusCoordinateUiMixin._cancel_stage_coordinate_action(
        owner, focus_reason="focus"
    )

    assert owner.cancel_order == [
        "pending-ui",
        "route",
        "surface",
        "scan",
        "stage",
    ]


def test_background_capture_cancel_reports_generic_cancel_status() -> None:
    owner = _Owner()
    surface_map = _SurfaceMapWindow(owner.cancel_order)
    scan_dialog = _MicroscopeScanDialog()
    owner.surface_map_window = surface_map
    owner.microscope_scan_dialog = scan_dialog
    owner._surface_map_running = True
    owner._microscope_scan_running_value = True

    _MainStatusCoordinateUiMixin._cancel_stage_coordinate_action(
        owner, focus_reason="focus"
    )

    assert surface_map.stop_calls == 1
    assert owner._microscope_scan_stop_requested.set_calls == 1
    assert scan_dialog.statuses == ["Microscope scan stop requested."]
    assert owner.statuses == [("Cancel requested.", 3000)]
    assert owner.view.focus_reasons == ["focus"]
    assert owner.cancel_order == ["surface", "scan", "stage"]


def test_pending_edits_only_cancel_clears_edits_and_reports_edit_status() -> None:
    owner = _Owner()
    owner._stage_motion.pending["Y"] = (2.0, 2.0)

    _MainStatusCoordinateUiMixin._cancel_stage_coordinate_action(
        owner, focus_reason="focus"
    )

    assert owner.stage_controller.cancelled_motions == []
    assert owner.stage_controller.cancelled_tasks == []
    assert owner.view.focus_reasons == ["focus"]
    assert owner.statuses == [("Cleared pending coordinate edits.", 2000)]
    assert owner.cancel_refreshes == 1


def test_global_cancel_clears_pending_click_through_interaction_seam() -> None:
    owner = _Owner()
    owner._microscope_interaction.has_pending_move = True

    _MainStatusCoordinateUiMixin._cancel_stage_coordinate_action(
        owner, focus_reason="focus"
    )

    assert owner._microscope_interaction.cancel_calls == [True]
    assert owner.statuses == [("Cancel requested.", 3000)]
    assert owner.view.focus_reasons == ["focus"]


def test_global_cancel_discards_pending_alignment_rotation() -> None:
    owner = _Owner()
    owner._stage_motion.alignment_pending = True

    _MainStatusCoordinateUiMixin._cancel_stage_coordinate_action(
        owner, focus_reason="focus"
    )

    assert owner._stage_motion.alignment_pending is False
    assert owner.statuses == [("Cancel requested.", 3000)]
