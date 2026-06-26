from probe_station_gui.route.run_ui import (
    RouteRunControlState,
    route_run_control_presentation,
    route_run_pause_action,
)


def test_route_run_control_shows_resume_only_at_confirmable_waiting_point() -> None:
    presentation = route_run_control_presentation(
        running=True,
        waiting=True,
        waiting_reason="paused",
        pause_request_pending=False,
        interrupt_request_pending=False,
    )

    assert presentation.can_confirm_waiting is True
    assert presentation.external_measurement_waiting is False
    assert presentation.pause_text == "Resume"
    assert presentation.pause_enabled is True
    assert presentation.interrupt_text == "Resume"
    assert presentation.interrupt_enabled is False


def test_route_run_control_keeps_pending_pause_as_interrupt_path() -> None:
    presentation = route_run_control_presentation(
        running=True,
        waiting=False,
        waiting_reason="",
        pause_request_pending=True,
        interrupt_request_pending=False,
    )

    assert presentation.can_confirm_waiting is False
    assert presentation.pause_text == "Interrupt"
    assert presentation.pause_enabled is True
    assert presentation.interrupt_text == "Interrupt"
    assert presentation.interrupt_enabled is False


def test_route_run_control_disables_interrupt_path_after_interrupt_request() -> None:
    presentation = route_run_control_presentation(
        running=True,
        waiting=False,
        waiting_reason="",
        pause_request_pending=False,
        interrupt_request_pending=True,
    )

    assert presentation.pause_text == "Interrupt"
    assert presentation.pause_enabled is False


def test_route_run_control_external_measurement_waiting_stays_interrupt_only() -> None:
    presentation = route_run_control_presentation(
        running=True,
        waiting=True,
        waiting_reason="external_measurement",
        pause_request_pending=False,
        interrupt_request_pending=False,
    )

    assert presentation.can_confirm_waiting is False
    assert presentation.external_measurement_waiting is True
    assert presentation.pause_text == "Interrupt"
    assert presentation.pause_enabled is True


def test_route_run_control_idle_disables_pause_and_interrupt() -> None:
    presentation = route_run_control_presentation(
        running=False,
        waiting=False,
        waiting_reason="",
        pause_request_pending=False,
        interrupt_request_pending=False,
    )

    assert presentation.can_confirm_waiting is False
    assert presentation.pause_text == "Pause"
    assert presentation.pause_enabled is False
    assert presentation.interrupt_text == "Interrupt"
    assert presentation.interrupt_enabled is False


def test_route_run_pause_action_resumes_confirmable_waiting_point() -> None:
    action = route_run_pause_action(
        running=True,
        waiting=True,
        waiting_reason="paused",
        pause_request_pending=False,
    )

    assert action == "resume"


def test_route_run_pause_action_interrupts_pending_pause() -> None:
    action = route_run_pause_action(
        running=True,
        waiting=False,
        waiting_reason="",
        pause_request_pending=True,
    )

    assert action == "interrupt"


def test_route_run_pause_action_interrupts_external_measurement_waiting() -> None:
    action = route_run_pause_action(
        running=True,
        waiting=True,
        waiting_reason="external_measurement",
        pause_request_pending=False,
    )

    assert action == "interrupt"


def test_route_run_control_state_clears_waiting_and_pending_when_stopped() -> None:
    state = RouteRunControlState(
        running=True,
        waiting=True,
        waiting_reason="paused",
        pause_request_pending=True,
        interrupt_request_pending=True,
    )

    stopped = state.with_running(False)

    assert stopped.running is False
    assert stopped.waiting is False
    assert stopped.waiting_reason == ""
    assert stopped.pause_request_pending is False
    assert stopped.interrupt_request_pending is False


def test_route_run_control_state_preserves_pending_pause_when_waiting_clears() -> None:
    state = RouteRunControlState(
        running=True,
        waiting=False,
        pause_request_pending=True,
    )

    waiting_cleared = state.with_waiting(False)

    assert waiting_cleared.pause_request_pending is True
    assert waiting_cleared.interrupt_request_pending is False


def test_route_run_control_state_waiting_ack_clears_pending_requests() -> None:
    state = RouteRunControlState(
        running=True,
        pause_request_pending=True,
        interrupt_request_pending=True,
    )

    waiting = state.with_waiting(True, "paused")

    assert waiting.waiting is True
    assert waiting.waiting_reason == "paused"
    assert waiting.pause_request_pending is False
    assert waiting.interrupt_request_pending is False


def test_route_run_control_state_pause_request_clears_interrupt_request() -> None:
    state = RouteRunControlState(
        running=True,
        interrupt_request_pending=True,
    )

    paused = state.with_pause_request_pending(True)

    assert paused.pause_request_pending is True
    assert paused.interrupt_request_pending is False


def test_route_run_control_state_interrupt_request_preserves_pause_request() -> None:
    state = RouteRunControlState(
        running=True,
        pause_request_pending=True,
    )

    interrupted = state.with_interrupt_request_pending(True)

    assert interrupted.pause_request_pending is True
    assert interrupted.interrupt_request_pending is True
