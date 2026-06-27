from __future__ import annotations

from types import SimpleNamespace

from probe_station_gui.notifications import telegram_commands as commands


class Alive:
    def __init__(self, alive: bool) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


def test_parse_slash_command_strips_bot_mention_and_preserves_stripped_args() -> None:
    assert commands.parse_telegram_command("/Status@ProbeBot   now please  ") == (
        "status",
        "now please",
    )


def test_parse_plain_command_and_empty_input() -> None:
    assert commands.parse_telegram_command("  help   me  ") == ("help", "me")
    assert commands.parse_telegram_command("   ") == ("", "")


def test_message_routing_for_known_commands_and_unknown() -> None:
    assert commands.route_message_command("/help").kind == "help"
    assert commands.route_message_command("статус").kind == "status"
    assert commands.route_message_command("/photo").kind == "route_photo"
    assert commands.route_message_command("/contact").kind == "contact_photo"
    assert commands.route_message_command("/skip").kind == "route_action"
    assert commands.route_message_command("/skip").action == "skip"
    assert commands.route_message_command("/unknown") is None


def test_callback_routing_for_known_callbacks_and_unknown() -> None:
    assert commands.route_callback("status").kind == "status"
    assert commands.route_callback("watch:photo").kind == "route_photo"
    assert commands.route_callback("watch:contact").kind == "contact_photo"
    assert commands.route_callback("route:measure").kind == "route_action"
    assert commands.route_callback("route:measure").action == "measure"

    response = commands.route_callback("other")

    assert response.kind == "response"
    assert response.text == "Unknown Telegram action."
    assert response.callback_answer == "Unknown action."


def test_help_text_is_existing_command_text() -> None:
    assert commands.help_text() == (
        "Probe Station Telegram commands:\n"
        "/status - current state and microscope frame\n"
        "/next_photo - send the next route structure photo\n"
        "/next_contact - send the next route contact attempt photo\n"
        "/measure, /skip, /next - answer a waiting route prompt"
    )


def test_keyboard_rows_with_and_without_waiting_and_no_remeasure() -> None:
    assert commands.default_markup_rows(route_waiting=False) == [
        [("Status", "status")],
        [("Next photo", "watch:photo"), ("Next contact", "watch:contact")],
    ]

    waiting_rows = commands.default_markup_rows(route_waiting=True)

    assert waiting_rows[-1] == [("Measure", "route:measure"), ("Skip", "route:skip")]
    labels = [label for row in waiting_rows for label, _callback in row]
    assert "Remeasure" not in labels
    assert commands.route_action_markup_rows() == [
        [("Measure", "route:measure"), ("Skip", "route:skip")],
        [("Status", "status")],
    ]


def test_route_photo_plans_for_inactive_disabled_and_success() -> None:
    assert commands.next_route_photo_response(
        route_active=False,
        structure_photos_enabled=True,
    ) == commands.PhotoRequestPlan(
        text="No route measurement is running.",
        callback_answer="No active route.",
        request_photo=False,
    )
    assert commands.next_route_photo_response(
        route_active=True,
        structure_photos_enabled=False,
    ) == commands.PhotoRequestPlan(
        text="The active route is not configured to capture structure photos.",
        callback_answer="No route photos.",
        request_photo=False,
    )
    assert commands.next_route_photo_response(
        route_active=True,
        structure_photos_enabled=True,
    ) == commands.PhotoRequestPlan(
        text="The next route structure photo will be sent here.",
        callback_answer="Waiting for route photo.",
        request_photo=True,
    )


def test_contact_photo_plans_for_inactive_disabled_and_success() -> None:
    assert commands.next_contact_photo_response(
        route_active=False,
        contact_measurement_enabled=True,
    ) == commands.PhotoRequestPlan(
        text="No route measurement is running.",
        callback_answer="No active route.",
        request_photo=False,
    )
    assert commands.next_contact_photo_response(
        route_active=True,
        contact_measurement_enabled=False,
    ) == commands.PhotoRequestPlan(
        text="The active route is not configured to measure contacts.",
        callback_answer="No contact measurements.",
        request_photo=False,
    )
    assert commands.next_contact_photo_response(
        route_active=True,
        contact_measurement_enabled=True,
    ) == commands.PhotoRequestPlan(
        text="The next route contact attempt photo will be sent here.",
        callback_answer="Waiting for contact photo.",
        request_photo=True,
    )


def test_route_action_plans_for_all_runner_and_api_states() -> None:
    assert commands.route_action_response(
        "remeasure",
        route_waiting=True,
        runner_available=True,
        api_route_control_accepts_confirmation=False,
    ) == commands.RouteActionPlan(
        text="Unknown route action.",
        callback_answer="Unknown action.",
        submit_action=None,
    )
    assert commands.route_action_response(
        "skip",
        route_waiting=False,
        runner_available=True,
        api_route_control_accepts_confirmation=False,
    ) == commands.RouteActionPlan(
        text="Route measurement is not waiting for an action.",
        callback_answer="Route is not waiting.",
        submit_action=None,
    )
    assert commands.route_action_response(
        "skip",
        route_waiting=True,
        runner_available=False,
        api_route_control_accepts_confirmation=True,
    ) == commands.RouteActionPlan(
        text="API route control action submitted: skip.",
        callback_answer="skip submitted.",
        submit_action="skip",
    )
    assert commands.route_action_response(
        "skip",
        route_waiting=True,
        runner_available=False,
        api_route_control_accepts_confirmation=False,
    ) == commands.RouteActionPlan(
        text="No route measurement is running.",
        callback_answer="No active route.",
        submit_action=None,
    )
    assert commands.route_action_response(
        "measure",
        route_waiting=True,
        runner_available=True,
        api_route_control_accepts_confirmation=False,
    ) == commands.RouteActionPlan(
        text="Route measurement action submitted: measure.",
        callback_answer="measure submitted.",
        submit_action="measure",
    )


def test_status_text_formats_waiting_route_position_homed_and_activity_lines() -> None:
    snapshot = commands.TelegramStatusSnapshot(
        latest_status_message="Ready",
        route_thread_active=True,
        route_waiting=True,
        route_session_active=False,
        route_current_point="P7",
        api_route_control_status_text="API waiting",
        stage_status={
            "connected": True,
            "busy": True,
            "state": "Run",
            "coordinate_display": "G54",
            "display_position": {
                "X": 1,
                "Y": "2.5",
                "Z": None,
                "A": "bad",
                "C": 3.333333,
            },
            "homed_axes": ["X", "Z"],
        },
        microscope_scan_active=True,
        contact_seek_active=True,
        camera_frame_available=False,
        stage_axis_names=("X", "Y", "Z", "A", "B", "C"),
    )

    assert commands.status_text(snapshot) == (
        "Probe Station status\n"
        "Current: Ready\n"
        "Route: waiting, point P7\n"
        "Serial: connected\n"
        "Stage: Run busy\n"
        "Coordinates: G54\n"
        "Position: X=1.0000, Y=2.5000, C=3.3333\n"
        "Homed: X, Z\n"
        "Microscope scan: running\n"
        "Contact seek: running\n"
        "Camera frame: unavailable"
    )


def test_status_text_uses_api_route_control_fallback_and_homed_none() -> None:
    snapshot = commands.TelegramStatusSnapshot(
        latest_status_message="",
        route_thread_active=False,
        route_waiting=False,
        route_session_active=False,
        route_current_point=None,
        api_route_control_status_text="API route control waiting",
        stage_status={
            "connected": False,
            "state": "",
            "coordinate_display": "",
            "homed_axes": [],
        },
        microscope_scan_active=False,
        contact_seek_active=False,
        camera_frame_available=True,
        stage_axis_names=("X", "Y"),
    )

    assert commands.status_text(snapshot) == (
        "Probe Station status\n"
        "Current: idle\n"
        "Route: API route control waiting\n"
        "Serial: disconnected\n"
        "Stage: unknown\n"
        "Coordinates: unknown\n"
        "Homed: none"
    )


def test_status_text_uses_session_active_with_point_before_api_fallback() -> None:
    snapshot = commands.TelegramStatusSnapshot(
        latest_status_message="Working",
        route_thread_active=False,
        route_waiting=False,
        route_session_active=True,
        route_current_point=42,
        api_route_control_status_text="API route control waiting",
        stage_status={"connected": True},
        microscope_scan_active=False,
        contact_seek_active=False,
        camera_frame_available=True,
        stage_axis_names=("X", "Y"),
    )

    assert "Route: session active, point 42" in commands.status_text(snapshot)
