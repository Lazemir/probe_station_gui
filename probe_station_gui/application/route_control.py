from __future__ import annotations

import logging

from probe_station_gui.route.adjustment_flow import (
    RouteShiftSavePlan,
    RouteShiftSaveStatusPlan,
    route_shift_runner_offset_update,
    route_shift_save_guard_plan,
    route_shift_save_plan,
    route_shift_save_status_plan,
    route_shift_stage_position_error_plan,
    route_shift_stage_xy_plan,
)
from probe_station_gui.route.measurement import RouteMeasurementPoint
from probe_station_gui.route.shift import route_shift_from_stage_xy
from probe_station_gui.stage import position_update as stage_position_update
from probe_station_gui.stage.controller import StageControllerError
from probe_station_gui.views import (
    main_window_stage_position_panel as stage_position_panel_adapter,
)

logger = logging.getLogger("main")


class _MainRouteControlMixin:
    def _request_pause_route_measurement(self) -> None:
        api_route_pause_action = (
            self._api_route_control_state_snapshot().pause_control_action()
        )
        if api_route_pause_action:
            if api_route_pause_action == "resume":
                self._api_route_control_action({"action": "resume"})
            elif api_route_pause_action == "interrupt":
                self._interrupt_api_route_controlled_operation(
                    "API route control interrupt requested."
                )
            else:
                self._api_route_control_action({"action": "pause"})
            return
        runner = self._route_measurement_runner
        if runner is None:
            self._show_status("No route measurement is running.", 3000)
            return
        runner.request_pause_after_current_point()
        message = "Route measurement pause requested; will pause after current point."
        self._show_status(message, 5000)
        self._route_runtime_presenter().pause_requested(message)

    def _save_route_measurement_shift(self, point_number: int | None = None) -> None:
        runner = self._route_measurement_runner
        shift_plan = self._route_shift_save_plan(runner, point_number)
        if shift_plan.message:
            self._show_route_runtime_status(shift_plan.message, shift_plan.timeout_ms)
            return
        adjustment_selected, adjustment_point = self._route_shift_adjustment_point(
            shift_plan,
            runner,
        )
        if not adjustment_selected:
            return
        stage_xy = self._route_shift_current_stage_xy()
        if stage_xy is None:
            return
        if shift_plan.runner_active:
            saved, message = runner.save_current_position_adjustment(stage_xy)
            self._update_api_route_offset_from_runner(runner, saved)
        else:
            self._api_route_offset_xy, message = route_shift_from_stage_xy(
                stage_xy,
                adjustment_point.stage_xy,
            )
        status_plan = route_shift_save_status_plan(
            runner_active=shift_plan.runner_active,
            message=message,
        )
        self._apply_route_shift_save_status(status_plan)

    def _route_shift_save_plan(
        self, runner: object | None, point_number: int | None
    ) -> RouteShiftSavePlan:
        thread = self._route_measurement_thread
        route_active = thread is not None and thread.is_alive()
        route_waiting = getattr(self, "_route_measurement_waiting", False)
        api_route_control = self._api_route_control_state_snapshot()
        guard_plan = route_shift_save_guard_plan(
            runner_available=runner is not None,
            route_active=route_active,
            route_waiting=route_waiting,
            api_route_control=api_route_control,
        )
        if guard_plan.message:
            return guard_plan
        dialog_current_point = (
            int(self._route_measurement_dialog.current_configuration().current_point)
            if point_number is None and self._route_measurement_dialog is not None
            else None
        )
        return route_shift_save_plan(
            runner_available=runner is not None,
            route_active=route_active,
            route_waiting=route_waiting,
            api_route_control=api_route_control,
            requested_point_number=point_number,
            dialog_current_point=dialog_current_point,
            current_point=getattr(self, "_route_measurement_current_point", None),
        )

    def _route_shift_adjustment_point(
        self, shift_plan: RouteShiftSavePlan, runner: object | None
    ) -> tuple[bool, RouteMeasurementPoint | None]:
        point_number = shift_plan.point_number
        if shift_plan.needs_runner_adjustment and runner is not None:
            point_selected, message = runner.set_current_adjustment_point(
                int(point_number)
            )
            if not point_selected:
                self._show_route_runtime_status(message, 6000)
                return False, None
            return True, None
        if not shift_plan.needs_api_context:
            return True, None
        context_result = self._api_contact_context(int(point_number))
        if not context_result.get("accepted", False):
            message = str(
                context_result.get("message")
                or "Route point is unavailable for saving shift."
            )
            self._show_route_runtime_status(message, 6000)
            return False, None
        return True, context_result["point"]

    def _route_shift_current_stage_xy(self) -> tuple[float, float] | None:
        try:
            position = self.stage_controller.current_stage_position()
        except StageControllerError as exc:
            latest = self.stage_controller.latest_stage_position()
            position_plan = route_shift_stage_position_error_plan(
                error_message=str(exc),
                controller_busy=self.stage_controller.is_busy(),
                latest_position_available=latest is not None,
            )
            if position_plan.message:
                self._show_route_runtime_status(
                    position_plan.message, position_plan.timeout_ms
                )
                return None
            position = latest
        stage_xy = stage_position_update.stage_xy_from_position(position)
        xy_plan = route_shift_stage_xy_plan(stage_xy_available=stage_xy is not None)
        if xy_plan.message:
            self._show_route_runtime_status(xy_plan.message, xy_plan.timeout_ms)
            return None
        return stage_xy

    def _update_api_route_offset_from_runner(
        self,
        runner: object,
        saved: bool,
    ) -> None:
        if not saved or not hasattr(runner, "route_offset_xy"):
            return
        try:
            raw_offset_xy = runner.route_offset_xy()
        except (TypeError, ValueError, IndexError):
            return
        offset_xy = route_shift_runner_offset_update(raw_offset_xy)
        if offset_xy is not None:
            self._api_route_offset_xy = offset_xy

    def _apply_route_shift_save_status(
        self, status_plan: RouteShiftSaveStatusPlan
    ) -> None:
        message = status_plan.message
        self._show_status(message, status_plan.timeout_ms)
        self._route_runtime_presenter().shift_status(
            message, mark_interrupt_pending=status_plan.mark_interrupt_pending
        )

    def _interrupt_route_measurement_runner(
        self, runner: object, *, reason: str
    ) -> None:
        try:
            waiting = bool(
                runner.status_payload().get("waiting")
                if hasattr(runner, "status_payload")
                else self._route_measurement_waiting
            )
        except Exception:
            waiting = bool(self._route_measurement_waiting)
        if hasattr(runner, "request_current_point_correction"):
            runner.request_current_point_correction()
        if not waiting:
            self.stage_controller.cancel_active_task(reason)
            stage_position_panel_adapter.clear_stage_motion_axes(self)
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)

    def _on_route_measurement_status(self, message: str) -> None:
        self._show_status(message)
        if self._route_attention_status(message):
            self._send_route_attention_alert(message)
        self._route_runtime_presenter().set_status(message)

    def _on_route_measurement_progress(
        self,
        position: int,
        total: int,
        point_number: int,
    ) -> None:
        self._set_route_measurement_resume_point(point_number)
        self._route_runtime_presenter().set_progress(position, total, point_number)

    def _on_route_measurement_current_point_changed(self, point_number: int) -> None:
        self._set_route_measurement_resume_point(point_number)
        runner = self._route_measurement_runner
        if runner is not None and self._route_measurement_waiting:
            self._pending_route_measure_point = int(point_number)
            if hasattr(runner, "set_current_adjustment_point"):
                runner.set_current_adjustment_point(point_number)

    def _on_route_measurement_waiting_changed(self, waiting: bool) -> None:
        self._route_measurement_waiting = bool(waiting)
        waiting_reason = self._current_route_measurement_waiting_reason(waiting)
        self._route_measurement_waiting_reason = waiting_reason
        self._route_runtime_presenter().waiting_changed(waiting, waiting_reason)
        if not self._route_measurement_waiting:
            return
        pending_point_number = self._pending_route_measure_point
        if pending_point_number is None:
            self._send_route_waiting_attention_from_last_result()
            return
        self._pending_route_measure_point = None
        self._submit_route_measurement_confirmation(f"jump:{int(pending_point_number)}")

    def _current_route_measurement_waiting_reason(self, waiting: bool) -> str:
        if not waiting:
            return ""
        runner = getattr(self, "_route_measurement_runner", None)
        if runner is not None and hasattr(runner, "status_payload"):
            try:
                status = runner.status_payload()
            except Exception:
                logger.exception("Failed to read route waiting reason.")
            else:
                reason = str(status.get("waiting_reason") or "").strip()
                if reason:
                    return reason
        return "paused"
