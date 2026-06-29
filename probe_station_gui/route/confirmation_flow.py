"""Pure planning helpers for route measurement confirmation actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from probe_station_gui.route.control_state import ApiRouteControlState
from probe_station_gui.route.runtime_settings import (
    route_common_runtime_settings,
    route_measurement_runtime_settings,
    route_runtime_requires_meter_configuration,
    route_waiting_restart_required,
)
from probe_station_gui.route.session_actions import (
    RouteConfirmationAction,
    route_confirmation_action,
)


@dataclass(frozen=True)
class RouteConfirmationSubmissionPlan:
    api_action: str | None = None
    confirmation: RouteConfirmationAction | None = None
    message: str = ""
    timeout_ms: int = 0

    @property
    def accepted(self) -> bool:
        return self.api_action is not None or self.confirmation is not None


@dataclass(frozen=True)
class RouteConfirmationRuntimePlan:
    restart_required: bool
    runtime_settings: dict[str, Any]
    meter_configuration_required: bool


def route_confirmation_submission_plan(
    action: str,
    *,
    api_route_control: ApiRouteControlState,
    runner_available: bool,
    contact_move_active: bool,
    waiting: bool,
    pending_point_number: int | None,
) -> RouteConfirmationSubmissionPlan:
    api_action = api_route_control.confirmation_api_action(action)
    if api_action is not None:
        return RouteConfirmationSubmissionPlan(api_action=api_action)
    if not runner_available:
        return RouteConfirmationSubmissionPlan(
            message="No route measurement is waiting.",
            timeout_ms=3000,
        )
    if contact_move_active:
        return RouteConfirmationSubmissionPlan(
            message="Wait for route contact move to finish.",
            timeout_ms=3000,
        )
    return RouteConfirmationSubmissionPlan(
        confirmation=route_confirmation_action(
            action,
            waiting=waiting,
            pending_point_number=pending_point_number,
        )
    )


def route_confirmation_runtime_plan(
    configuration: object,
    *,
    external_session: bool,
    waiting: bool,
    setup_changed: bool,
) -> RouteConfirmationRuntimePlan:
    return RouteConfirmationRuntimePlan(
        restart_required=route_waiting_restart_required(
            external_session=external_session,
            waiting=waiting,
            setup_changed=setup_changed,
        ),
        runtime_settings=(
            route_common_runtime_settings(configuration)
            if external_session
            else route_measurement_runtime_settings(configuration)
        ),
        meter_configuration_required=route_runtime_requires_meter_configuration(
            configuration
        ),
    )


__all__ = [
    "RouteConfirmationRuntimePlan",
    "RouteConfirmationSubmissionPlan",
    "route_confirmation_runtime_plan",
    "route_confirmation_submission_plan",
]
