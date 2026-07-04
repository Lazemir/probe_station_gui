"""Dispatch policy for local API command requests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from probe_station_gui.route.api_window_guard import probe_route_api_requires_window


ApiResponse = dict[str, Any]
PayloadHandler = Callable[[dict[str, Any]], ApiResponse]
NoPayloadHandler = Callable[[], ApiResponse]
RouteControlGuard = Callable[[str, dict[str, Any]], ApiResponse | None]
RouteWindowGuardSubmitter = Callable[[str, dict[str, Any]], ApiResponse]
GuiThreadSubmitter = Callable[[dict[str, Any]], ApiResponse]
RouteWindowRequirement = Callable[[str, dict[str, Any]], bool]


class DirectCommandDispatcher(Protocol):
    def __call__(
        self,
        command_request: dict[str, Any],
        *,
        apply_route_control_guard: bool,
    ) -> ApiResponse: ...


class MoveToCoordinatesHandler(Protocol):
    def __call__(
        self,
        targets: object,
        *,
        mode: object,
        feedrate: object,
    ) -> ApiResponse: ...


API_ROUTE_CONTROL_ACTIONS = frozenset(
    {
        "api_route_control_status",
        "api_route_control_action",
    }
)


@dataclass(frozen=True)
class ApiCommandDispatchHandlers:
    route_control_guard: RouteControlGuard
    list_contacts: NoPayloadHandler
    move_to_contact: PayloadHandler
    contact_needles: PayloadHandler
    check_contact: PayloadHandler
    stage_local_focus: PayloadHandler
    route_contact_focus: PayloadHandler
    route_contact_photo: PayloadHandler
    contact_seek: PayloadHandler
    api_route_control_status: NoPayloadHandler
    api_route_control_action: PayloadHandler
    configure_meter: PayloadHandler
    raw_voltage_sweep: PayloadHandler
    visa_list_resources: NoPayloadHandler
    visa_operation: PayloadHandler
    start_route_session: PayloadHandler
    route_session_status: NoPayloadHandler
    route_session_action: PayloadHandler
    route_session_result: PayloadHandler
    route_session_seek: NoPayloadHandler
    route_session_artifact: PayloadHandler
    lens_distortion_calibration: PayloadHandler
    microscope_area_scan: PayloadHandler


@dataclass(frozen=True)
class ApiBridgeRequestHandlers:
    move_to_coordinates: MoveToCoordinatesHandler
    stage_status: NoPayloadHandler
    submit_command: PayloadHandler
    route_control_guard: RouteControlGuard


def api_command_action_payload(
    command_request: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    action = str(command_request.get("action", "")).strip().lower()
    payload = command_request.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    return action, payload


def unsupported_api_command_response(action: str) -> ApiResponse:
    return {
        "accepted": False,
        "status_code": 400,
        "message": f"Unsupported API command: {action}",
    }


def unsupported_api_action_response(action: str) -> ApiResponse:
    return {
        "accepted": False,
        "status_code": 400,
        "message": f"Unsupported API action: {action}",
    }


def dispatch_api_command_request(
    command_request: dict[str, Any],
    handlers: ApiCommandDispatchHandlers,
    *,
    apply_route_control_guard: bool,
) -> ApiResponse:
    action, payload = api_command_action_payload(command_request)
    if apply_route_control_guard:
        route_control_guard = handlers.route_control_guard(action, payload)
        if route_control_guard is not None:
            return route_control_guard

    no_payload_handlers: dict[str, NoPayloadHandler] = {
        "list_contacts": handlers.list_contacts,
        "api_route_control_status": handlers.api_route_control_status,
        "visa_list_resources": handlers.visa_list_resources,
        "route_session_status": handlers.route_session_status,
        "route_session_seek": handlers.route_session_seek,
    }
    if action in no_payload_handlers:
        return no_payload_handlers[action]()

    payload_handlers: dict[str, PayloadHandler] = {
        "move_to_contact": handlers.move_to_contact,
        "contact_needles": handlers.contact_needles,
        "check_contact": handlers.check_contact,
        "stage_local_focus": handlers.stage_local_focus,
        "route_contact_focus": handlers.route_contact_focus,
        "route_contact_photo": handlers.route_contact_photo,
        "contact_seek": handlers.contact_seek,
        "api_route_control_action": handlers.api_route_control_action,
        "configure_meter": handlers.configure_meter,
        "raw_voltage_sweep": handlers.raw_voltage_sweep,
        "visa_operation": handlers.visa_operation,
        "start_route_session": handlers.start_route_session,
        "route_session_action": handlers.route_session_action,
        "route_session_result": handlers.route_session_result,
        "route_session_artifact": handlers.route_session_artifact,
        "lens_distortion_calibration": handlers.lens_distortion_calibration,
        "microscope_area_scan": handlers.microscope_area_scan,
    }
    if action in payload_handlers:
        return payload_handlers[action](payload)

    return unsupported_api_command_response(action)


def submit_api_command_request_from_api_thread(
    command_request: dict[str, Any],
    *,
    submit_on_gui_thread: GuiThreadSubmitter,
    submit_probe_route_window_guard_on_gui_thread: RouteWindowGuardSubmitter,
    dispatch_direct: DirectCommandDispatcher,
    route_window_required: RouteWindowRequirement = probe_route_api_requires_window,
) -> ApiResponse:
    action, payload = api_command_action_payload(command_request)
    if action in API_ROUTE_CONTROL_ACTIONS:
        return submit_on_gui_thread(command_request)
    if route_window_required(action, payload):
        guard = submit_probe_route_window_guard_on_gui_thread(action, payload)
        if not guard.get("accepted", False):
            return guard
        return dispatch_direct(command_request, apply_route_control_guard=False)
    return dispatch_direct(command_request, apply_route_control_guard=False)


def handle_api_request(
    request: dict[str, Any],
    handlers: ApiBridgeRequestHandlers,
) -> ApiResponse:
    action = str(request.get("action", "")).strip().lower()
    if action == "move_to_coordinates":
        return handlers.move_to_coordinates(
            request.get("targets"),
            mode=request.get("mode", "G90"),
            feedrate=request.get("feedrate"),
        )
    if action == "status":
        return handlers.stage_status()
    if action == "command":
        command = request.get("command")
        if not isinstance(command, dict):
            return {
                "accepted": False,
                "status_code": 400,
                "message": "API command request must be an object.",
            }
        return handlers.submit_command(command)
    if action == "probe_route_window_guard":
        guard_action = str(request.get("guard_action", "")).strip().lower()
        payload = request.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        route_control_guard = handlers.route_control_guard(guard_action, payload)
        if route_control_guard is not None:
            return route_control_guard
        return {
            "accepted": True,
            "route_control_window_open": True,
        }
    return unsupported_api_action_response(action)


__all__ = [
    "API_ROUTE_CONTROL_ACTIONS",
    "ApiBridgeRequestHandlers",
    "ApiCommandDispatchHandlers",
    "api_command_action_payload",
    "dispatch_api_command_request",
    "handle_api_request",
    "submit_api_command_request_from_api_thread",
    "unsupported_api_action_response",
    "unsupported_api_command_response",
]
