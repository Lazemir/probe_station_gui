"""Pure planning helpers for route measurement adjustment actions."""

from __future__ import annotations

from dataclasses import dataclass

from probe_station_gui.route.control_state import ApiRouteControlState
from probe_station_gui.route.operation_guards import (
    route_contact_move_block_message,
    route_shift_save_block_message,
)

@dataclass(frozen=True)
class RouteContactMovePlan:
    message: str = ""
    timeout_ms: int = 0
    route_active: bool = False
    route_waiting: bool = False
    set_resume_point: bool = False
    set_adjustment_point: bool = False

    @property
    def accepted(self) -> bool:
        return not self.message


@dataclass(frozen=True)
class RouteShiftSavePlan:
    message: str = ""
    timeout_ms: int = 0
    runner_active: bool = False
    runner_waiting: bool = False
    point_number: int | None = None
    needs_runner_adjustment: bool = False
    needs_api_context: bool = False

    @property
    def accepted(self) -> bool:
        return not self.message


@dataclass(frozen=True)
class RouteShiftStagePositionPlan:
    message: str = ""
    timeout_ms: int = 0
    use_latest_position: bool = False

    @property
    def accepted(self) -> bool:
        return not self.message


@dataclass(frozen=True)
class RouteShiftStageXyPlan:
    message: str = ""
    timeout_ms: int = 0

    @property
    def accepted(self) -> bool:
        return not self.message


@dataclass(frozen=True)
class RouteShiftSaveStatusPlan:
    message: str
    timeout_ms: int
    mark_interrupt_pending: bool = False


def route_contact_move_plan(
    *,
    contact_move_active: bool,
    route_active: bool,
    route_waiting: bool,
    api_route_control: ApiRouteControlState,
) -> RouteContactMovePlan:
    if contact_move_active:
        return RouteContactMovePlan(
            message="Route contact move is already active.",
            timeout_ms=3000,
        )
    effective_route_active = bool(route_active)
    effective_route_waiting = bool(route_waiting if effective_route_active else False)
    message = route_contact_move_block_message(
        route_active=effective_route_active,
        route_waiting=effective_route_waiting,
        api_route_control=api_route_control,
    )
    if message is not None:
        return RouteContactMovePlan(
            message=message,
            timeout_ms=5000,
            route_active=effective_route_active,
            route_waiting=effective_route_waiting,
        )
    return RouteContactMovePlan(
        route_active=effective_route_active,
        route_waiting=effective_route_waiting,
        set_resume_point=not effective_route_active or effective_route_waiting,
        set_adjustment_point=effective_route_active and effective_route_waiting,
    )


def route_shift_stage_position_error_plan(
    *,
    error_message: str,
    controller_busy: bool,
    latest_position_available: bool,
) -> RouteShiftStagePositionPlan:
    if controller_busy or not latest_position_available:
        return RouteShiftStagePositionPlan(
            message=str(error_message),
            timeout_ms=6000,
        )
    return RouteShiftStagePositionPlan(use_latest_position=True)


def route_shift_stage_xy_plan(
    *,
    stage_xy_available: bool,
) -> RouteShiftStageXyPlan:
    if stage_xy_available:
        return RouteShiftStageXyPlan()
    return RouteShiftStageXyPlan(
        message="Current stage X/Y position is unavailable.",
        timeout_ms=5000,
    )


def route_shift_runner_offset_update(offset_xy: object) -> tuple[float, float] | None:
    try:
        return (float(offset_xy[0]), float(offset_xy[1]))  # type: ignore[index]
    except (TypeError, ValueError, IndexError):
        return None


def route_shift_save_status_plan(
    *,
    runner_active: bool,
    message: str,
) -> RouteShiftSaveStatusPlan:
    return RouteShiftSaveStatusPlan(
        message=str(message),
        timeout_ms=5000,
        mark_interrupt_pending=bool(runner_active),
    )


def route_shift_save_plan(
    *,
    runner_available: bool,
    route_active: bool,
    route_waiting: bool,
    api_route_control: ApiRouteControlState,
    requested_point_number: int | None,
    dialog_current_point: int | None,
    current_point: int | None,
) -> RouteShiftSavePlan:
    guard_plan = route_shift_save_guard_plan(
        runner_available=runner_available,
        route_active=route_active,
        route_waiting=route_waiting,
        api_route_control=api_route_control,
    )
    if guard_plan.message:
        return guard_plan

    runner_active = guard_plan.runner_active
    runner_waiting = guard_plan.runner_waiting
    point_number = _first_point_number(
        requested_point_number,
        dialog_current_point,
        current_point,
    )
    if not runner_active and point_number is None:
        return RouteShiftSavePlan(
            message="Select a route point before saving shift.",
            timeout_ms=5000,
            runner_active=runner_active,
            runner_waiting=runner_waiting,
        )
    return RouteShiftSavePlan(
        runner_active=runner_active,
        runner_waiting=runner_waiting,
        point_number=point_number,
        needs_runner_adjustment=runner_active and point_number is not None,
        needs_api_context=not runner_active,
    )


def route_shift_save_guard_plan(
    *,
    runner_available: bool,
    route_active: bool,
    route_waiting: bool,
    api_route_control: ApiRouteControlState,
) -> RouteShiftSavePlan:
    runner_active = bool(runner_available and route_active)
    if api_route_control.active:
        runner_active = False
    runner_waiting = bool(route_waiting if runner_active else False)
    message = route_shift_save_block_message(
        runner_active=runner_active,
        runner_waiting=runner_waiting,
        api_route_control=api_route_control,
    )
    if message is not None:
        return RouteShiftSavePlan(
            message=message,
            timeout_ms=5000,
            runner_active=runner_active,
            runner_waiting=runner_waiting,
        )
    return RouteShiftSavePlan(
        runner_active=runner_active,
        runner_waiting=runner_waiting,
    )


def _first_point_number(*values: int | None) -> int | None:
    for value in values:
        if value is not None:
            return int(value)
    return None


__all__ = [
    "RouteContactMovePlan",
    "RouteShiftSavePlan",
    "RouteShiftSaveStatusPlan",
    "RouteShiftStagePositionPlan",
    "RouteShiftStageXyPlan",
    "route_contact_move_plan",
    "route_shift_runner_offset_update",
    "route_shift_save_guard_plan",
    "route_shift_save_plan",
    "route_shift_save_status_plan",
    "route_shift_stage_position_error_plan",
    "route_shift_stage_xy_plan",
]
