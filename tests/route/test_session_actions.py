from probe_station_gui.route.session_actions import (
    route_confirmation_action,
    route_session_action_from_payload,
)


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


def test_route_confirmation_action_replaces_next_with_pending_jump() -> None:
    action = route_confirmation_action(
        "next",
        waiting=True,
        pending_point_number=42,
    )

    assert action.action == "jump:42"
    assert action.action_key == "jump:42"
    assert action.replaced_pending_point is True
    assert action.status_label == "measure from point 42"


def test_route_confirmation_action_keeps_explicit_measurement_actions() -> None:
    action = route_confirmation_action(
        "remeasure",
        waiting=True,
        pending_point_number=42,
    )

    assert action.action == "remeasure"
    assert action.replaced_pending_point is False
    assert action.status_label == "remeasure"


def test_route_confirmation_action_labels_known_actions() -> None:
    assert (
        route_confirmation_action(
            "measure",
            waiting=False,
            pending_point_number=None,
        ).status_label
        == "measure"
    )
    assert (
        route_confirmation_action(
            "skip",
            waiting=False,
            pending_point_number=None,
        ).status_label
        == "skip"
    )
    assert (
        route_confirmation_action(
            "continue",
            waiting=False,
            pending_point_number=42,
        ).status_label
        == "next"
    )
