from __future__ import annotations

from typing import Any

import pytest

from probe_station_gui.api.command_dispatch import (
    ApiBridgeRequestHandlers,
    ApiCommandDispatchHandlers,
    api_command_action_payload,
    dispatch_api_command_request,
    handle_api_request,
    submit_api_command_request_from_api_thread,
    unsupported_api_command_response,
)

NO_PAYLOAD_ACTIONS = (
    ("list_contacts", "list_contacts"),
    ("api_route_control_status", "api_route_control_status"),
    ("visa_list_resources", "visa_list_resources"),
    ("route_session_status", "route_session_status"),
    ("route_session_seek", "route_session_seek"),
)

PAYLOAD_ACTIONS = (
    ("move_to_contact", "move_to_contact"),
    ("contact_needles", "contact_needles"),
    ("check_contact", "check_contact"),
    ("stage_local_focus", "stage_local_focus"),
    ("route_contact_focus", "route_contact_focus"),
    ("route_contact_photo", "route_contact_photo"),
    ("contact_seek", "contact_seek"),
    ("api_route_control_action", "api_route_control_action"),
    ("configure_meter", "configure_meter"),
    ("raw_voltage_sweep", "raw_voltage_sweep"),
    ("visa_operation", "visa_operation"),
    ("start_route_session", "start_route_session"),
    ("route_session_action", "route_session_action"),
    ("route_session_result", "route_session_result"),
    ("route_session_artifact", "route_session_artifact"),
    ("lens_distortion_calibration", "lens_distortion_calibration"),
    ("click_to_move_calibration", "click_to_move_calibration"),
    ("microscope_area_scan", "microscope_area_scan"),
)


def _response(name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"accepted": True, "handler": name}
    if payload is not None:
        response["payload"] = dict(payload)
    return response


def _command_handlers(
    *,
    route_control_guard=lambda action, payload: None,
) -> ApiCommandDispatchHandlers:
    return ApiCommandDispatchHandlers(
        route_control_guard=route_control_guard,
        list_contacts=lambda: _response("list_contacts"),
        move_to_contact=lambda payload: _response("move_to_contact", payload),
        contact_needles=lambda payload: _response("contact_needles", payload),
        check_contact=lambda payload: _response("check_contact", payload),
        stage_local_focus=lambda payload: _response("stage_local_focus", payload),
        route_contact_focus=lambda payload: _response("route_contact_focus", payload),
        route_contact_photo=lambda payload: _response("route_contact_photo", payload),
        contact_seek=lambda payload: _response("contact_seek", payload),
        api_route_control_status=lambda: _response("api_route_control_status"),
        api_route_control_action=lambda payload: _response(
            "api_route_control_action",
            payload,
        ),
        configure_meter=lambda payload: _response("configure_meter", payload),
        raw_voltage_sweep=lambda payload: _response("raw_voltage_sweep", payload),
        visa_list_resources=lambda: _response("visa_list_resources"),
        visa_operation=lambda payload: _response("visa_operation", payload),
        start_route_session=lambda payload: _response("start_route_session", payload),
        route_session_status=lambda: _response("route_session_status"),
        route_session_action=lambda payload: _response("route_session_action", payload),
        route_session_result=lambda payload: _response("route_session_result", payload),
        route_session_seek=lambda: _response("route_session_seek"),
        route_session_artifact=lambda payload: _response(
            "route_session_artifact",
            payload,
        ),
        lens_distortion_calibration=lambda payload: _response(
            "lens_distortion_calibration",
            payload,
        ),
        click_to_move_calibration=lambda payload: _response(
            "click_to_move_calibration",
            payload,
        ),
        microscope_area_scan=lambda payload: _response("microscope_area_scan", payload),
    )


def test_api_command_action_payload_normalizes_action_and_payload() -> None:
    assert api_command_action_payload({"action": " Move_To_Contact "}) == (
        "move_to_contact",
        {},
    )
    assert api_command_action_payload(
        {"action": "CONTACT_NEEDLES", "payload": "not an object"},
    ) == ("contact_needles", {})
    payload = {"contact_number": 12}
    assert api_command_action_payload(
        {"action": "contact_needles", "payload": payload},
    ) == ("contact_needles", payload)


@pytest.mark.parametrize(("action", "handler_name"), NO_PAYLOAD_ACTIONS)
def test_dispatch_api_command_request_routes_no_payload_actions(
    action: str,
    handler_name: str,
) -> None:
    handlers = _command_handlers()

    response = dispatch_api_command_request(
        {"action": action, "payload": {"ignored": True}},
        handlers,
        apply_route_control_guard=False,
    )

    assert response == {"accepted": True, "handler": handler_name}


@pytest.mark.parametrize(("action", "handler_name"), PAYLOAD_ACTIONS)
def test_dispatch_api_command_request_routes_payload_actions(
    action: str,
    handler_name: str,
) -> None:
    handlers = _command_handlers()
    payload = {"contact_number": 7}

    response = dispatch_api_command_request(
        {"action": action, "payload": payload},
        handlers,
        apply_route_control_guard=False,
    )

    assert response == {
        "accepted": True,
        "handler": handler_name,
        "payload": payload,
    }


def test_dispatch_api_command_request_returns_unsupported_response() -> None:
    assert unsupported_api_command_response("unknown") == {
        "accepted": False,
        "status_code": 400,
        "message": "Unsupported API command: unknown",
    }
    assert dispatch_api_command_request(
        {"action": "unknown"},
        _command_handlers(),
        apply_route_control_guard=False,
    ) == unsupported_api_command_response("unknown")


def test_submit_from_api_thread_routes_api_route_control_through_gui_thread() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def submit_on_gui_thread(command_request: dict[str, Any]) -> dict[str, Any]:
        calls.append(("gui", dict(command_request)))
        return {"accepted": True, "bridged": True}

    response = submit_api_command_request_from_api_thread(
        {"action": "api_route_control_action", "payload": {"action": "pause"}},
        submit_on_gui_thread=submit_on_gui_thread,
        submit_probe_route_window_guard_on_gui_thread=lambda action, payload: {
            "accepted": True
        },
        dispatch_direct=lambda command_request, *, apply_route_control_guard: {
            "accepted": True,
            "direct": True,
        },
    )

    assert response == {"accepted": True, "bridged": True}
    assert calls == [
        (
            "gui",
            {
                "action": "api_route_control_action",
                "payload": {"action": "pause"},
            },
        )
    ]


def test_submit_from_api_thread_routes_api_route_control_status_through_gui_thread() -> None:
    calls: list[dict[str, Any]] = []

    response = submit_api_command_request_from_api_thread(
        {"action": "api_route_control_status"},
        submit_on_gui_thread=lambda command_request: (
            calls.append(dict(command_request)) or {"accepted": True, "bridged": True}
        ),
        submit_probe_route_window_guard_on_gui_thread=lambda action, payload: {
            "accepted": True
        },
        dispatch_direct=lambda command_request, *, apply_route_control_guard: {
            "accepted": True,
            "direct": True,
        },
    )

    assert response == {"accepted": True, "bridged": True}
    assert calls == [{"action": "api_route_control_status"}]


@pytest.mark.parametrize(
    "action",
    (
        "lens_distortion_calibration",
        "click_to_move_calibration",
        "microscope_area_scan",
    ),
)
def test_submit_from_api_thread_serializes_optical_mutations_on_gui_thread(
    action: str,
) -> None:
    gui_calls: list[dict[str, Any]] = []
    direct_calls: list[dict[str, Any]] = []
    command = {"action": action, "payload": {"reset": True}}

    response = submit_api_command_request_from_api_thread(
        command,
        submit_on_gui_thread=lambda request: (
            gui_calls.append(dict(request))
            or {"accepted": True, "bridged": True}
        ),
        submit_probe_route_window_guard_on_gui_thread=lambda _action, _payload: {
            "accepted": True
        },
        dispatch_direct=lambda request, *, apply_route_control_guard: (
            direct_calls.append(dict(request))
            or {"accepted": True, "direct": True}
        ),
    )

    assert response == {"accepted": True, "bridged": True}
    assert gui_calls == [command]
    assert direct_calls == []


def test_submit_from_api_thread_checks_route_window_guard_before_direct_dispatch() -> None:
    guard_calls: list[tuple[str, dict[str, Any]]] = []
    dispatch_calls: list[tuple[dict[str, Any], bool]] = []

    def guard(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        guard_calls.append((action, dict(payload)))
        return {"accepted": True, "route_control_window_open": True}

    def dispatch(
        command_request: dict[str, Any],
        *,
        apply_route_control_guard: bool,
    ) -> dict[str, Any]:
        dispatch_calls.append((dict(command_request), apply_route_control_guard))
        return {"accepted": True, "direct": True}

    command = {"action": "move_to_contact", "payload": {"contact_number": 3}}
    response = submit_api_command_request_from_api_thread(
        command,
        submit_on_gui_thread=lambda command_request: {"accepted": True, "bridged": True},
        submit_probe_route_window_guard_on_gui_thread=guard,
        dispatch_direct=dispatch,
    )

    assert response == {"accepted": True, "direct": True}
    assert guard_calls == [("move_to_contact", {"contact_number": 3})]
    assert dispatch_calls == [(command, False)]


def test_submit_from_api_thread_returns_rejected_route_window_guard_response() -> None:
    dispatch_calls: list[dict[str, Any]] = []

    response = submit_api_command_request_from_api_thread(
        {"action": "move_to_contact", "payload": {"contact_number": 3}},
        submit_on_gui_thread=lambda command_request: {"accepted": True, "bridged": True},
        submit_probe_route_window_guard_on_gui_thread=lambda action, payload: {
            "accepted": False,
            "status_code": 409,
        },
        dispatch_direct=lambda command_request, *, apply_route_control_guard: (
            dispatch_calls.append(dict(command_request)) or {"accepted": True}
        ),
    )

    assert response == {"accepted": False, "status_code": 409}
    assert dispatch_calls == []


def test_handle_api_request_keeps_bridge_facing_actions_stable() -> None:
    commands: list[dict[str, Any]] = []
    guards: list[tuple[str, dict[str, Any]]] = []
    handlers = ApiBridgeRequestHandlers(
        move_to_coordinates=lambda targets, *, mode, feedrate: {
            "accepted": True,
            "targets": targets,
            "mode": mode,
            "feedrate": feedrate,
        },
        stage_status=lambda: {"accepted": True, "status": "idle"},
        submit_command=lambda command: commands.append(dict(command))
        or {"accepted": True, "command": dict(command)},
        route_control_guard=lambda action, payload: guards.append(
            (action, dict(payload))
        )
        or None,
    )

    assert handle_api_request(
        {"action": "move_to_coordinates", "targets": {"X": 1.0}},
        handlers,
    ) == {
        "accepted": True,
        "targets": {"X": 1.0},
        "mode": "G90",
        "feedrate": None,
    }
    assert handle_api_request({"action": "status"}, handlers) == {
        "accepted": True,
        "status": "idle",
    }
    assert handle_api_request(
        {"action": "command", "command": {"action": "list_contacts"}},
        handlers,
    ) == {"accepted": True, "command": {"action": "list_contacts"}}
    assert handle_api_request(
        {
            "action": "probe_route_window_guard",
            "guard_action": "MOVE_TO_CONTACT",
            "payload": {"contact_number": 2},
        },
        handlers,
    ) == {"accepted": True, "route_control_window_open": True}
    assert commands == [{"action": "list_contacts"}]
    assert guards == [("move_to_contact", {"contact_number": 2})]


def test_handle_api_request_rejects_bad_command_and_unknown_action() -> None:
    handlers = ApiBridgeRequestHandlers(
        move_to_coordinates=lambda targets, *, mode, feedrate: {"accepted": True},
        stage_status=lambda: {"accepted": True},
        submit_command=lambda command: {"accepted": True},
        route_control_guard=lambda action, payload: None,
    )

    assert handle_api_request({"action": "command", "command": None}, handlers) == {
        "accepted": False,
        "status_code": 400,
        "message": "API command request must be an object.",
    }
    assert handle_api_request({"action": "missing"}, handlers) == {
        "accepted": False,
        "status_code": 400,
        "message": "Unsupported API action: missing",
    }
