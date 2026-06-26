"""Execute API Route Control commands behind a route-module seam."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from probe_station_gui.route.control_state import ApiRouteControlState
from probe_station_gui.route.operation import (
    ApiRouteControlActionEffect,
    api_route_control_action_plan,
)


@dataclass(frozen=True)
class ApiRouteControlStateAdapter:
    snapshot: Callable[[], ApiRouteControlState]
    set_state: Callable[[ApiRouteControlState], None]
    status_payload: Callable[[], dict[str, Any]]
    update_ui: Callable[[str], None]
    updated_utc: Callable[[], str]


@dataclass(frozen=True)
class ApiRouteControlWindowAdapter:
    is_open: Callable[[], bool]
    guard_closed: Callable[[str, dict[str, Any]], dict[str, Any] | None]
    open_for_api_start: Callable[[], bool]


@dataclass(frozen=True)
class ApiRouteControlRunnerAdapter:
    clear_waiting_before_start: Callable[[], dict[str, Any] | None]


@dataclass(frozen=True)
class ApiRouteControlInterruptAdapter:
    perform: Callable[
        [str, ApiRouteControlState | None, str],
        dict[str, Any],
    ]


@dataclass(frozen=True)
class ApiRouteControlOperationAdapters:
    state: ApiRouteControlStateAdapter
    window: ApiRouteControlWindowAdapter
    runner: ApiRouteControlRunnerAdapter
    interrupt: ApiRouteControlInterruptAdapter


def execute_api_route_control_action(
    payload: dict[str, Any],
    adapters: ApiRouteControlOperationAdapters,
) -> dict[str, Any]:
    """Execute one API Route Control command through Main-provided adapters."""

    plan = api_route_control_action_plan(
        payload,
        adapters.state.snapshot(),
        updated_utc=adapters.state.updated_utc(),
    )
    if plan.effect is ApiRouteControlActionEffect.REJECT:
        return plan.rejection_payload()
    if plan.effect is ApiRouteControlActionEffect.STATUS:
        return adapters.state.status_payload()
    if plan.effect is ApiRouteControlActionEffect.START:
        existing_route_result = adapters.runner.clear_waiting_before_start()
        if existing_route_result is not None:
            return existing_route_result
    if plan.requires_control_window_open and not adapters.window.is_open():
        guarded = adapters.window.guard_closed(
            "api_route_control_resume",
            {"route_action": plan.command.action},
        )
        if guarded is not None:
            return guarded
    if plan.effect is ApiRouteControlActionEffect.INTERRUPT:
        return adapters.interrupt.perform(
            "API route control interrupt requested.",
            plan.state,
            plan.message,
        )
    adapters.state.set_state(plan.state)
    if plan.effect is ApiRouteControlActionEffect.CLEAR_ACTION:
        return adapters.state.status_payload()
    message = plan.message
    if (
        plan.effect is ApiRouteControlActionEffect.START
        and not adapters.window.open_for_api_start()
    ):
        state, message = adapters.state.snapshot().start_failed_window_not_open()
        adapters.state.set_state(state)
        adapters.state.update_ui(message)
        status = adapters.state.status_payload()
        status.update(
            {
                "accepted": False,
                "status_code": 409,
                "message": message,
                "route_control_window_open": False,
            }
        )
        return status
    adapters.state.update_ui(message)
    status = adapters.state.status_payload()
    status["message"] = message
    return status


__all__ = [
    "ApiRouteControlInterruptAdapter",
    "ApiRouteControlOperationAdapters",
    "ApiRouteControlRunnerAdapter",
    "ApiRouteControlStateAdapter",
    "ApiRouteControlWindowAdapter",
    "execute_api_route_control_action",
]
