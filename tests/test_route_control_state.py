from probe_station_gui.route_control_state import (
    ApiRouteControlState,
    api_route_control_command_from_payload,
    api_route_control_legacy_attrs,
    api_route_control_state_from_legacy_attrs,
    normalize_route_control_action,
)


def test_start_pause_ack_and_status_payload_preserve_api_route_control_semantics() -> None:
    state = ApiRouteControlState()

    state, message = state.start(label="chip 163", updated_utc="t1")
    assert message == "chip 163: running."
    assert state.status_payload(route_control_window_open=True) == {
        "accepted": True,
        "active": True,
        "pause_requested": False,
        "paused": False,
        "stop_requested": False,
        "pending_action": "",
        "label": "chip 163",
        "updated_utc": "t1",
        "route_control_window_open": True,
    }

    state, message = state.request_pause(updated_utc="t2")
    assert message == "chip 163: pause requested."
    assert state.pause_requested is True
    assert state.paused is False
    assert state.stop_requested is False
    assert state.pending_action == ""

    state, message = state.ack_pause(updated_utc="t3")
    assert message == "chip 163: paused."
    assert state.pause_requested is False
    assert state.paused is True
    assert state.stop_requested is False
    assert state.pending_action == ""


def test_resume_stop_finish_and_clear_action_preserve_pending_action_rules() -> None:
    state = ApiRouteControlState(
        active=True,
        paused=True,
        label="chip 163",
        pending_action="skip",
    )

    state, message = state.resume(
        action="continue",
        explicit_action="remeasure",
        updated_utc="t4",
    )
    assert message == "chip 163: remeasure."
    assert state.pause_requested is False
    assert state.paused is False
    assert state.pending_action == "remeasure"

    state, message = state.clear_action(updated_utc="t5")
    assert message is None
    assert state.pending_action == ""
    assert state.updated_utc == "t5"

    state, message = state.stop(updated_utc="t6")
    assert message == "chip 163: stop requested."
    assert state.pause_requested is False
    assert state.paused is False
    assert state.stop_requested is True
    assert state.pending_action == ""

    state, message = state.finish(label="", updated_utc="t7")
    assert message == "chip 163: finished."
    assert state.active is False
    assert state.stop_requested is False
    assert state.pending_action == ""


def test_failed_start_resets_activity_but_keeps_route_control_label() -> None:
    state, _message = ApiRouteControlState().start(
        label="chip 163",
        updated_utc="t1",
    )

    state, message = state.start_failed_window_not_open()

    assert message == "chip 163: route control window did not open."
    assert state.active is False
    assert state.pause_requested is False
    assert state.paused is False
    assert state.stop_requested is False
    assert state.pending_action == ""
    assert state.label == "chip 163"
    assert state.updated_utc == "t1"


def test_interrupt_marks_api_route_control_paused_without_requesting_stop() -> None:
    state = ApiRouteControlState(
        active=True,
        pause_requested=True,
        paused=False,
        stop_requested=True,
        pending_action="skip",
        label="chip 163",
    )

    state, message = state.interrupt(updated_utc="t8")

    assert message == "chip 163: interrupted; paused."
    assert state.active is True
    assert state.pause_requested is False
    assert state.paused is True
    assert state.stop_requested is False
    assert state.pending_action == ""
    assert state.updated_utc == "t8"


def test_normalize_route_control_action_preserves_external_aliases() -> None:
    assert normalize_route_control_action("resume") == "next"
    assert normalize_route_control_action(" next ") == "next"
    assert normalize_route_control_action("continue") == "next"
    assert normalize_route_control_action("measure") == "measure"
    assert normalize_route_control_action("remeasure") == "remeasure"
    assert normalize_route_control_action("skip") == "skip"
    assert normalize_route_control_action("stop") == "stop"
    assert normalize_route_control_action("interrupt") == "interrupt"
    assert normalize_route_control_action("seek") == "seek"
    assert normalize_route_control_action("7") == "jump:7"
    assert normalize_route_control_action("jump: 8") == "jump:8"
    assert normalize_route_control_action("jump:bad") is None
    assert normalize_route_control_action("unknown") is None


def test_api_route_control_command_from_payload_groups_external_actions() -> None:
    assert api_route_control_command_from_payload({}).kind == "status"
    assert api_route_control_command_from_payload({"command": "begin"}).kind == "start"
    assert (
        api_route_control_command_from_payload({"action": "pause_ack"}).kind
        == "pause_ack"
    )
    assert (
        api_route_control_command_from_payload(
            {
                "action": "jump: 8",
                "pending_action": "remeasure",
                "label": " chip 163 ",
            }
        )
        == api_route_control_command_from_payload(
            {
                "action": "jump: 8",
                "next_action": "remeasure",
                "name": "chip 163",
            }
        )
    )


def test_api_route_control_command_from_payload_marks_active_only_commands() -> None:
    active_required = [
        "pause",
        "pause_ack",
        "interrupt",
        "resume",
        "stop",
    ]

    for action in active_required:
        command = api_route_control_command_from_payload({"action": action})
        assert command.requires_active_control is True

    for action in ["status", "start", "finish", "ack", "unknown"]:
        command = api_route_control_command_from_payload({"action": action})
        assert command.requires_active_control is False


def test_api_route_control_ui_state_marks_pause_request_as_pending() -> None:
    ui_state = ApiRouteControlState(
        active=True,
        pause_requested=True,
        paused=False,
    ).ui_state()

    assert ui_state.active is True
    assert ui_state.waiting is False
    assert ui_state.waiting_reason == ""
    assert ui_state.control_waiting_reason == "paused"
    assert ui_state.pause_pending is True


def test_api_route_control_ui_state_marks_paused_control_as_waiting() -> None:
    ui_state = ApiRouteControlState(
        active=True,
        pause_requested=False,
        paused=True,
    ).ui_state()

    assert ui_state.active is True
    assert ui_state.waiting is True
    assert ui_state.waiting_reason == "paused"
    assert ui_state.control_waiting_reason == "paused"
    assert ui_state.pause_pending is False


def test_api_route_control_state_from_legacy_attrs_preserves_compat_fields() -> None:
    class LegacyRouteControl:
        _api_route_control_active = True
        _api_route_control_pause_requested = True
        _api_route_control_paused = False
        _api_route_control_stop_requested = True
        _api_route_control_pending_action = "skip"
        _api_route_control_label = "chip 163"
        _api_route_control_updated_utc = "t9"

    state = api_route_control_state_from_legacy_attrs(LegacyRouteControl())

    assert state == ApiRouteControlState(
        active=True,
        pause_requested=True,
        paused=False,
        stop_requested=True,
        pending_action="skip",
        label="chip 163",
        updated_utc="t9",
    )


def test_api_route_control_legacy_attrs_round_trips_state() -> None:
    state = ApiRouteControlState(
        active=True,
        pause_requested=False,
        paused=True,
        stop_requested=False,
        pending_action="remeasure",
        label="chip 163",
        updated_utc="t10",
    )

    legacy = type("LegacyRouteControl", (), api_route_control_legacy_attrs(state))()

    assert api_route_control_state_from_legacy_attrs(legacy) == state
