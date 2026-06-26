from __future__ import annotations

from dataclasses import replace

from probe_station_gui.route.control_operation import (
    ApiRouteControlInterruptAdapter,
    ApiRouteControlOperationAdapters,
    ApiRouteControlRunnerAdapter,
    ApiRouteControlStateAdapter,
    ApiRouteControlWindowAdapter,
    execute_api_route_control_action,
)
from probe_station_gui.route.control_state import ApiRouteControlState


class _StateHarness:
    def __init__(
        self,
        state: ApiRouteControlState,
        *,
        route_control_window_open: bool = False,
    ) -> None:
        self.state = state
        self.route_control_window_open = route_control_window_open
        self.ui_messages: list[str] = []
        self.updated_utc = "t0"

    def snapshot(self) -> ApiRouteControlState:
        return self.state

    def set_state(self, state: ApiRouteControlState) -> None:
        self.state = state

    def status_payload(self) -> dict[str, object]:
        return self.state.status_payload(
            route_control_window_open=self.route_control_window_open
        )

    def update_ui(self, message: str) -> None:
        self.ui_messages.append(str(message))


def _make_adapters(
    harness: _StateHarness,
    *,
    clear_waiting_before_start=None,
    guard_closed=None,
    interrupt=None,
    open_for_api_start=None,
    is_open=None,
) -> ApiRouteControlOperationAdapters:
    return ApiRouteControlOperationAdapters(
        state=ApiRouteControlStateAdapter(
            snapshot=harness.snapshot,
            set_state=harness.set_state,
            status_payload=harness.status_payload,
            update_ui=harness.update_ui,
            updated_utc=lambda: harness.updated_utc,
        ),
        window=ApiRouteControlWindowAdapter(
            is_open=is_open or (lambda: harness.route_control_window_open),
            guard_closed=guard_closed or (lambda _action, _payload: None),
            open_for_api_start=open_for_api_start or (lambda: True),
        ),
        runner=ApiRouteControlRunnerAdapter(
            clear_waiting_before_start=clear_waiting_before_start or (lambda: None)
        ),
        interrupt=ApiRouteControlInterruptAdapter(
            perform=interrupt
            or (
                lambda reason, planned_state, planned_message: {
                    "accepted": True,
                    "reason": reason,
                    "planned_state": planned_state,
                    "planned_message": planned_message,
                }
            ),
        ),
    )


def test_execute_api_route_control_action_start_clears_waiting_runner_before_opening() -> None:
    harness = _StateHarness(ApiRouteControlState())
    calls: list[str] = []

    adapters = _make_adapters(
        harness,
        clear_waiting_before_start=lambda: calls.append("clear") or None,
        open_for_api_start=lambda: calls.append("open") or True,
    )

    response = execute_api_route_control_action(
        {"action": "start", "label": "chip 163"},
        adapters,
    )

    assert response["accepted"] is True
    assert calls == ["clear", "open"]
    assert harness.state == ApiRouteControlState(
        active=True,
        pause_requested=False,
        paused=False,
        stop_requested=False,
        pending_action="",
        label="chip 163",
        updated_utc="t0",
    )
    assert harness.ui_messages == ["chip 163: running."]


def test_execute_api_route_control_action_resume_uses_window_guard_payload() -> None:
    harness = _StateHarness(
        ApiRouteControlState(active=True, paused=True, label="chip 163")
    )
    guard_calls: list[tuple[str, dict[str, object]]] = []

    adapters = _make_adapters(
        harness,
        guard_closed=lambda action, payload: guard_calls.append(
            (action, dict(payload))
        )
        or {
            "accepted": False,
            "status_code": 409,
            "message": "window closed",
            "route_control_window_open": False,
        },
    )

    response = execute_api_route_control_action({"action": "resume"}, adapters)

    assert response == {
        "accepted": False,
        "status_code": 409,
        "message": "window closed",
        "route_control_window_open": False,
    }
    assert guard_calls == [("api_route_control_resume", {"route_action": "resume"})]
    assert harness.state.paused is True
    assert harness.ui_messages == []


def test_execute_api_route_control_action_failed_start_rolls_back_state_and_payload() -> None:
    harness = _StateHarness(ApiRouteControlState(), route_control_window_open=True)

    adapters = _make_adapters(
        harness,
        open_for_api_start=lambda: False,
    )

    response = execute_api_route_control_action(
        {"action": "start", "label": "chip 163"},
        adapters,
    )

    assert response == {
        "accepted": False,
        "active": False,
        "pause_requested": False,
        "paused": False,
        "stop_requested": False,
        "pending_action": "",
        "label": "chip 163",
        "updated_utc": "t0",
        "route_control_window_open": False,
        "status_code": 409,
        "message": "chip 163: route control window did not open.",
    }
    assert harness.state == replace(
        ApiRouteControlState(label="chip 163", updated_utc="t0"),
        active=False,
    )
    assert harness.ui_messages == ["chip 163: route control window did not open."]


def test_execute_api_route_control_action_interrupt_uses_planned_state_and_message() -> None:
    harness = _StateHarness(ApiRouteControlState(active=True, label="chip 163"))
    interrupt_calls: list[tuple[str, ApiRouteControlState | None, str]] = []

    adapters = _make_adapters(
        harness,
        interrupt=lambda reason, planned_state, planned_message: interrupt_calls.append(
            (reason, planned_state, planned_message)
        )
        or {
            "accepted": True,
            "message": planned_message,
        },
    )

    response = execute_api_route_control_action({"action": "interrupt"}, adapters)

    assert response == {
        "accepted": True,
        "message": "chip 163: interrupted; paused.",
    }
    assert interrupt_calls == [
        (
            "API route control interrupt requested.",
            ApiRouteControlState(
                active=True,
                pause_requested=False,
                paused=True,
                stop_requested=False,
                pending_action="",
                label="chip 163",
                updated_utc="t0",
            ),
            "chip 163: interrupted; paused.",
        )
    ]
    assert harness.state == ApiRouteControlState(active=True, label="chip 163")
