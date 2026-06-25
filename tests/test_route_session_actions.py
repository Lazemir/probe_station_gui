from probe_station_gui.route_session_actions import route_session_action_from_payload


def test_route_session_action_from_payload_detects_control_actions() -> None:
    assert route_session_action_from_payload({"action": " pause "}).kind == "pause"
    assert (
        route_session_action_from_payload({"command": "INTERRUPT"}).kind
        == "interrupt"
    )
    assert route_session_action_from_payload({"action": "stop"}).kind == "stop"


def test_route_session_action_from_payload_preserves_confirmation_action() -> None:
    action = route_session_action_from_payload({"action": " ReMeasure "})

    assert action.kind == "confirmation"
    assert action.action == "ReMeasure"
    assert action.is_confirmation is True


def test_route_session_action_from_payload_defaults_missing_action_to_next() -> None:
    action = route_session_action_from_payload({})

    assert action.kind == "confirmation"
    assert action.action == "next"
