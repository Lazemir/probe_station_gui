"""Direct owner for the api stage contact domain."""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path
from typing import Any

from probe_station_gui.application.route_run_execution import (
    RouteRunKind,
    RouteRunReleaseCause,
    RouteRunReleaseRequest,
)
from probe_station_gui.design.contact_navigation import (
    api_contact_needles_plan,
    api_contact_needles_stage_error_response,
    api_contact_needles_success_response,
    api_move_to_contact_plan,
    api_move_to_contact_stage_error_response,
    api_move_to_contact_success_response,
    api_route_point_payload,
)
from probe_station_gui.route.api_measurement import api_contact_number_from_payload
from probe_station_gui.route.control_operation import (
    ApiRouteControlInterruptAdapter,
    ApiRouteControlOperationAdapters,
    ApiRouteControlRunnerAdapter,
    ApiRouteControlStateAdapter,
    ApiRouteControlWindowAdapter,
    execute_api_route_control_action,
)
from probe_station_gui.route.control_state import (
    ApiRouteControlState,
    api_route_control_legacy_attrs,
    api_route_control_state_from_legacy_attrs,
)
from probe_station_gui.route.payload_parsing import (
    payload_float,
    payload_optional_float,
)
from probe_station_gui.route.runtime_presenter import RouteRuntimePresentationSink
from probe_station_gui.route.telegram_adapter import route_photo_focus_payload
from probe_station_gui.stage.api_moves import (
    api_axis_value_map,
    api_coordinate_move_busy_response,
    api_coordinate_move_plan,
    api_coordinate_move_start_failed_response,
    api_coordinate_move_success_response,
)
from probe_station_gui.stage.controller import StageControllerError
from probe_station_gui.views import (
    main_window_stage_position_panel as stage_position_panel_adapter,
)

logger = logging.getLogger("main")


class _MainApiStageContactMixin:
    def _api_move_to_coordinates(
        self, targets: object, *, mode: object = "G90", feedrate: object = None
    ) -> dict[str, Any]:
        move_plan = api_coordinate_move_plan(
            targets,
            axis_names=self.STAGE_AXIS_NAMES,
            mode=mode,
            feedrate=feedrate,
            current_feedrate=self._current_linear_feedrate(),
            min_feedrate=self.MIN_FEEDRATE_MM_MIN,
            resolve_axis_target=self._resolve_api_stage_axis_target,
            axis_target_limit_error=self._machine_axis_target_limit_error,
        )
        if isinstance(move_plan, dict):
            return move_plan
        if (
            self._coordinate_targets.has_active_move()
            or self.stage_controller.is_busy()
        ):
            return api_coordinate_move_busy_response()
        if not self._start_coordinate_targets_move(
            move_plan.target_map,
            feedrate_mm_min=move_plan.feedrate_mm_min,
            source_label="API",
            limit_targets={
                axis: float(display_target)
                for axis, (_raw_target, display_target) in move_plan.target_map.items()
            },
        ):
            return api_coordinate_move_start_failed_response()
        return api_coordinate_move_success_response(
            move_plan,
            coordinate_display="Machine",
        )

    def _api_stage_status(self) -> dict[str, Any]:
        latest_position = self.stage_controller.latest_stage_position()
        payload = self._stage_status_payload(
            latest_position,
            display_position=self._api_machine_display_position(),
            accepted=True,
        )
        payload["coordinate_display"] = "Machine"
        return payload

    def _surface_map_stage_status(self) -> dict[str, Any]:
        latest_position = self.stage_controller.latest_stage_position()
        display_position = self._api_machine_display_position()
        if isinstance(latest_position, (tuple, list)):
            for axis, value in zip(self.STAGE_AXIS_NAMES, latest_position):
                display_position.setdefault(axis, float(value))
        return self._stage_status_payload(
            latest_position,
            display_position=display_position,
            accepted=False,
        )

    def _stage_status_payload(
        self,
        latest_position: object,
        *,
        display_position: dict[str, float],
        accepted: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if accepted:
            payload["accepted"] = True
        payload.update(
            {
                "connected": bool(
                    self.serial_connection is not None
                    and getattr(self.serial_connection, "is_open", False)
                ),
                "busy": self.stage_controller.is_busy(),
                "state": self.stage_controller.latest_stage_state(),
                "coordinate_display": self.stage_controller.coordinate_display_name(),
                "homed_axes": sorted(self.stage_controller.homed_axes()),
                "position": api_axis_value_map(
                    latest_position,
                    axis_names=self.STAGE_AXIS_NAMES,
                ),
                "display_position": display_position,
                "pending_targets": {
                    axis: float(values[1])
                    for axis, values in self._pending_stage_axis_targets.items()
                },
                "active_coordinate_axis": self._coordinate_targets.active_axis,
                "active_coordinate_axes": sorted(self._coordinate_targets.active_axes),
                "current_feedrate_mm_min": self._current_linear_feedrate(),
            }
        )
        return payload

    def _surface_map_move_to_xy(
        self,
        x_mm: float,
        y_mm: float,
    ) -> dict[str, Any]:
        if self.serial_connection is None or not self.serial_connection.is_open:
            return {
                "accepted": False,
                "message": "Serial connection is not available.",
            }
        if (
            self._coordinate_targets.has_active_move()
            or self.stage_controller.is_busy()
        ):
            return {
                "accepted": False,
                "message": "Stage is busy. Ignoring surface-map target.",
            }
        feedrate = self._coordinate_feedrate_for_axes(("X", "Y"))
        targets: dict[str, tuple[float, float]] = {}
        for axis, display_target in (("X", x_mm), ("Y", y_mm)):
            try:
                display_value = float(display_target)
            except (TypeError, ValueError):
                return {
                    "accepted": False,
                    "message": f"Invalid {axis} target: {display_target}.",
                }
            if not math.isfinite(display_value):
                return {
                    "accepted": False,
                    "message": f"Invalid {axis} target: {display_target}.",
                }
            raw_target, resolved_display_target = self._resolve_api_stage_axis_target(
                axis,
                display_value,
                "G90",
            )
            if raw_target is None:
                return {
                    "accepted": False,
                    "message": f"{axis} coordinate is unavailable.",
                }
            limit_error = self._stage_axis_target_limit_error(
                axis,
                resolved_display_target,
            )
            if limit_error is not None:
                return {"accepted": False, "message": limit_error}
            targets[axis] = (float(raw_target), float(resolved_display_target))
        accepted = self._start_coordinate_targets_move(
            targets,
            feedrate_mm_min=feedrate,
            source_label="Surface Map",
            limit_targets={
                axis: display_target
                for axis, (_raw_target, display_target) in targets.items()
            },
        )
        return {
            "accepted": bool(accepted),
            "message": "Surface-map XY move accepted."
            if accepted
            else "Unable to start XY move.",
            "targets": {"X": float(x_mm), "Y": float(y_mm)},
            "current_feedrate_mm_min": feedrate,
        }

    def _api_list_contacts(self) -> dict[str, Any]:
        route = self._design_session.route
        if route is None:
            return {
                "accepted": False,
                "status_code": 409,
                "message": "No probe route is loaded.",
            }
        registration_valid = self._coordinate_system_coordinator.snapshot().registration.registration_valid
        contacts = [
            api_route_point_payload(
                route_index=index,
                route_point=route_point,
                include_stage_xy=registration_valid,
                resolve_stage_xy=self._raw_stage_xy_from_design_xy,
                structure_number_for_route_point=self._api_structure_number_for_route_point,
            )
            for index, route_point in enumerate(route.points, start=1)
        ]
        return {
            "accepted": True,
            "route_name": route.name,
            "route_path": str(route.path) if route.path is not None else None,
            "registration_valid": registration_valid,
            "contacts": contacts,
        }

    def _api_move_to_contact(self, payload: dict[str, Any]) -> dict[str, Any]:
        contact_plan_result = api_move_to_contact_plan(
            payload,
            contact_context=self._api_contact_context,
            default_needle_feedrate=lambda: self._api_needle_feedrate({}),
            min_feedrate=self.MIN_FEEDRATE_MM_MIN,
        )
        if isinstance(contact_plan_result, dict):
            return contact_plan_result
        contact_plan = contact_plan_result
        needles_lowered = False
        try:
            with self.stage_controller.reserve_external_task("API contact move"):
                try:
                    if contact_plan.request.lift_before_move:
                        self.stage_controller.run_external_needles_action(
                            "lift",
                            contact_plan.request.needle_feedrate_mm_min,
                        )
                    target_xy = self._api_route_adjusted_stage_xy(contact_plan.point)
                    self.stage_controller.run_external_move_to_xy(
                        target_xy[0],
                        target_xy[1],
                    )
                    if contact_plan.request.lower_needles:
                        self.stage_controller.run_external_needles_action(
                            "lower",
                            contact_plan.request.needle_feedrate_mm_min,
                        )
                        needles_lowered = True
                        if contact_plan.request.contact_settle_s > 0.0:
                            time.sleep(contact_plan.request.contact_settle_s)
                    return api_move_to_contact_success_response(
                        contact_plan,
                        timestamp_utc=self._api_timestamp_utc(),
                        route_offset_xy=self._api_route_offset_xy,
                        target_stage_xy=target_xy,
                        needles_lowered=needles_lowered,
                    )
                finally:
                    if contact_plan.request.lift_after and needles_lowered:
                        try:
                            self.stage_controller.run_external_needles_action(
                                "lift",
                                contact_plan.request.needle_feedrate_mm_min,
                            )
                        except StageControllerError:
                            logger.exception("API contact move failed to lift needles.")
        except StageControllerError as exc:
            return api_move_to_contact_stage_error_response(
                str(exc),
                contact=contact_plan.contact,
            )

    def _api_contact_needles(self, payload: dict[str, Any]) -> dict[str, Any]:
        contact_plan_result = api_contact_needles_plan(
            payload,
            contact_context=self._api_contact_context,
            default_needle_feedrate=lambda: self._api_needle_feedrate({}),
            min_feedrate=self.MIN_FEEDRATE_MM_MIN,
        )
        if isinstance(contact_plan_result, dict):
            return contact_plan_result
        contact_plan = contact_plan_result
        try:
            with self.stage_controller.reserve_external_task("API needle action"):
                self.stage_controller.run_external_needles_action(
                    contact_plan.request.action,
                    contact_plan.request.needle_feedrate_mm_min,
                )
                return api_contact_needles_success_response(
                    contact_plan,
                    timestamp_utc=self._api_timestamp_utc(),
                )
        except StageControllerError as exc:
            return api_contact_needles_stage_error_response(
                str(exc),
                contact=contact_plan.contact,
            )

    def _api_check_contact(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._api_measure_current_contact(payload, seek=False)

    def _api_focus_settings_from_payload(
        self,
        payload: dict[str, Any],
    ) -> tuple[float, float | None]:
        return (
            payload_float(
                payload,
                "range_mm",
                "focus_range_mm",
                "photo_autofocus_range_mm",
                default=0.03,
                minimum=0.001,
            ),
            payload_optional_float(
                payload,
                "step_mm",
                "focus_step_mm",
                minimum=0.001,
            ),
        )

    def _api_focus_needles_rejection(
        self,
        message: str,
        *,
        contact: Any | None = None,
    ) -> dict[str, Any] | None:
        needles_known = bool(getattr(self.stage_controller, "_needles_known", False))
        needles_up = bool(getattr(self.stage_controller, "_needles_up", False))
        needles_zone = getattr(self.stage_controller, "_needles_zone", None)
        if needles_known and needles_up and needles_zone == "raise":
            return None
        response: dict[str, Any] = {
            "accepted": False,
            "status_code": 409,
            "message": message,
        }
        if contact is not None:
            response["contact"] = contact
        response.update(
            {
                "needles_known": needles_known,
                "needles_up": needles_up,
                "needles_zone": needles_zone or "unknown",
            }
        )
        return response

    def _api_contact_context_from_payload(
        self,
        payload: dict[str, Any],
    ) -> tuple[int | None, Any | None, dict[str, Any] | None]:
        contact_number = api_contact_number_from_payload(payload)
        if contact_number is None:
            return (
                None,
                None,
                {
                    "accepted": False,
                    "status_code": 400,
                    "message": "Provide a positive contact_number.",
                },
            )
        context_result = self._api_contact_context(contact_number)
        if not context_result.get("accepted", False):
            return contact_number, None, context_result
        return contact_number, context_result["contact"], None

    def _api_stage_local_focus(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            focus_range_mm, focus_step_mm = self._api_focus_settings_from_payload(
                payload
            )
        except ValueError as exc:
            return {
                "accepted": False,
                "status_code": 400,
                "message": str(exc),
            }

        try:
            with self.stage_controller.reserve_external_task("API local autofocus"):
                needles_rejection = self._api_focus_needles_rejection(
                    "Local autofocus requires fully raised needles "
                    "(known needle zone 'raise')."
                )
                if needles_rejection is not None:
                    return needles_rejection
                result = self.stage_controller.run_external_local_autofocus(
                    range_mm=focus_range_mm,
                    step_mm=focus_step_mm,
                )
        except StageControllerError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
            }
        return {
            "accepted": True,
            "message": str(result.summary()),
            "timestamp_utc": self._api_timestamp_utc(),
            "focus_range_mm": focus_range_mm,
            "focus_step_mm": focus_step_mm,
            "focus": route_photo_focus_payload(result),
        }

    def _api_route_contact_focus(self, payload: dict[str, Any]) -> dict[str, Any]:
        _contact_number, contact, context_error = (
            self._api_contact_context_from_payload(payload)
        )
        if context_error is not None:
            return context_error
        try:
            focus_range_mm, focus_step_mm = self._api_focus_settings_from_payload(
                payload
            )
        except ValueError as exc:
            return {
                "accepted": False,
                "status_code": 400,
                "message": str(exc),
                "contact": contact,
            }

        try:
            with self.stage_controller.reserve_external_task("API route contact focus"):
                needles_rejection = self._api_focus_needles_rejection(
                    "Route contact focus requires fully raised needles "
                    "(known needle zone 'raise').",
                    contact=contact,
                )
                if needles_rejection is not None:
                    return needles_rejection
                result = self.stage_controller.run_external_local_autofocus(
                    range_mm=focus_range_mm,
                    step_mm=focus_step_mm,
                )
        except StageControllerError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
                "contact": contact,
            }
        return {
            "accepted": True,
            "message": str(result.summary()),
            "timestamp_utc": self._api_timestamp_utc(),
            "contact": contact,
            "focus_range_mm": focus_range_mm,
            "focus_step_mm": focus_step_mm,
            "focus": route_photo_focus_payload(result),
        }

    def _api_route_contact_photo(self, payload: dict[str, Any]) -> dict[str, Any]:
        contact_number, contact, context_error = self._api_contact_context_from_payload(
            payload
        )
        if context_error is not None:
            return context_error
        photo = self._latest_camera_frame_photo()
        if photo is None:
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Camera frame is unavailable; cannot capture contact photo.",
                "contact": contact,
            }
        photo_bytes, photo_name = photo
        suffix = Path(photo_name).suffix or ".jpg"
        filename = f"contact_{contact_number:03d}_photo{suffix}"
        return {
            "accepted": True,
            "message": f"Captured contact {contact_number} photo.",
            "timestamp_utc": self._api_timestamp_utc(),
            "contact": contact,
            "filename": filename,
            "content_type": (
                "image/jpeg"
                if filename.lower().endswith((".jpg", ".jpeg"))
                else "image/png"
            ),
            "data": photo_bytes,
        }

    def _api_contact_seek(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._api_measure_current_contact(payload, seek=True)

    def _api_route_control_state_snapshot(self) -> ApiRouteControlState:
        state = getattr(self, "_api_route_control_state", ApiRouteControlState())
        if not isinstance(state, ApiRouteControlState):
            state = ApiRouteControlState()
        return api_route_control_state_from_legacy_attrs(self, fallback=state)

    def _set_api_route_control_state(self, state: ApiRouteControlState) -> None:
        self._api_route_control_state = state
        for name, value in api_route_control_legacy_attrs(state).items():
            setattr(self, name, value)

    def _api_route_control_status(self) -> dict[str, Any]:
        return self._api_route_control_state_snapshot().status_payload(
            route_control_window_open=self._route_control_window_is_open()
        )

    def _api_route_control_operation_adapters(self) -> ApiRouteControlOperationAdapters:
        return ApiRouteControlOperationAdapters(
            state=ApiRouteControlStateAdapter(
                snapshot=self._api_route_control_state_snapshot,
                set_state=self._set_api_route_control_state,
                status_payload=self._api_route_control_status,
                update_ui=self._update_api_route_control_ui,
                updated_utc=self._api_timestamp_utc,
            ),
            window=ApiRouteControlWindowAdapter(
                is_open=self._route_control_window_is_open,
                guard_closed=lambda action, payload: self._probe_route_api_window_guard(
                    action,
                    payload,
                ),
                open_for_api_start=self._show_route_measurement_dialog_for_api_session,
            ),
            runner=ApiRouteControlRunnerAdapter(
                clear_waiting_before_start=(
                    self._clear_waiting_route_runner_before_api_control
                ),
            ),
            interrupt=ApiRouteControlInterruptAdapter(
                perform=lambda reason, planned_state, planned_message: (
                    self._interrupt_api_route_controlled_operation(
                        reason,
                        planned_state=planned_state,
                        planned_message=planned_message,
                    )
                )
            ),
        )

    def _api_route_control_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        return execute_api_route_control_action(
            payload,
            self._api_route_control_operation_adapters(),
        )

    def _clear_waiting_route_runner_before_api_control(
        self,
    ) -> dict[str, Any] | None:
        execution = self._route_run_execution.snapshot()
        runner = execution.runner
        if not execution.active:
            return None
        if not execution.thread_alive:
            release = self._route_run_execution.release(
                RouteRunReleaseRequest(
                    cause=RouteRunReleaseCause.FINISHED,
                    expected_runner=runner,
                    join_timeout_s=0.1,
                )
            )
            if release.released:
                self._route_measurement_session_active = False
                self._pending_route_measure_point = None
            return None
        can_clear_waiting_gui_route = (
            runner is not None
            and execution.kind is RouteRunKind.GUI
            and execution.waiting
        )
        if not can_clear_waiting_gui_route:
            return {
                "accepted": False,
                "status_code": 409,
                "message": (
                    "Route measurement is already active. Stop or pause it "
                    "before starting API route control."
                ),
            }
        try:
            release = self._route_run_execution.release(
                RouteRunReleaseRequest(
                    cause=RouteRunReleaseCause.TAKEOVER,
                    expected_runner=runner,
                    join_timeout_s=2.0,
                )
            )
        except Exception as exc:
            logger.exception("Failed to clear waiting route measurement.")
            return {
                "accepted": False,
                "status_code": 409,
                "message": f"Failed to stop waiting route measurement: {exc}",
            }
        if not release.released:
            return {
                "accepted": False,
                "status_code": 409,
                "message": (
                    "Waiting route measurement did not stop before API route "
                    "control start."
                ),
            }
        self._route_measurement_session_active = False
        self._pending_route_measure_point = None
        return None

    def _request_api_route_control_pause(self, message: str) -> dict[str, Any]:
        state, transition_message = (
            self._api_route_control_state_snapshot().request_pause(
                updated_utc=self._api_timestamp_utc(),
            )
        )
        self._set_api_route_control_state(state)
        message = message or transition_message
        self._update_api_route_control_ui(message)
        status = self._api_route_control_status()
        status["message"] = message
        return status

    def _ack_api_route_control_pause(self, message: str) -> dict[str, Any]:
        state, transition_message = self._api_route_control_state_snapshot().ack_pause(
            updated_utc=self._api_timestamp_utc(),
        )
        self._set_api_route_control_state(state)
        message = message or transition_message
        self._update_api_route_control_ui(message)
        status = self._api_route_control_status()
        status["message"] = message
        return status

    def _interrupt_api_route_controlled_operation(
        self,
        reason: str,
        *,
        planned_state: ApiRouteControlState | None = None,
        planned_message: str = "",
    ) -> dict[str, Any]:
        self._pending_route_measure_point = None
        self._contact_seek_stop_requested.set()
        try:
            self.stage_controller.cancel_active_task(reason)
        except Exception:
            logger.exception("Failed to cancel API route control stage task.")
        try:
            if self._controller_reports_active_motion():
                self.stage_controller.cancel_active_motion(reason)
        except Exception:
            logger.exception("Failed to cancel API route control active motion.")
        stage_position_panel_adapter.clear_stage_motion_axes(self)
        try:
            self._stage_motion.cancel_planned_xy_move()
        except Exception:
            logger.exception(
                "Failed to clear planned move prediction after API route control interrupt."
            )
        self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
        if planned_state is None:
            state, message = self._api_route_control_state_snapshot().interrupt(
                updated_utc=self._api_timestamp_utc(),
            )
        else:
            state = planned_state
            message = planned_message or f"{state.display_label}: interrupted; paused."
        self._set_api_route_control_state(state)
        self._update_api_route_control_ui(message)
        status = self._api_route_control_status()
        status["message"] = message
        return status

    def _update_api_route_control_ui(self, message: str) -> None:
        ui_state = self._api_route_control_state_snapshot().ui_state()
        self._route_runtime_presenter().apply_api_control_update(ui_state, message)
        self._show_status(message, 5000)

    def _route_runtime_presenter(self) -> RouteRuntimePresentationSink:
        presenter = getattr(self, "_route_runtime_presenter_instance", None)
        if presenter is None:
            presenter = RouteRuntimePresentationSink(
                current_dialog=lambda: getattr(self, "_route_measurement_dialog", None),
                current_navigator=lambda: getattr(self, "design_navigator_panel", None),
            )
            self._route_runtime_presenter_instance = presenter
        return presenter
