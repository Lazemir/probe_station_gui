from probe_station_gui.route_control_state import ApiRouteControlState


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
