"""Pure route-operation planning helpers."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from probe_station_gui.design.model import DesignModelError, Point2D
from probe_station_gui.design.objective_offsets import camera_stage_to_raw_stage
from probe_station_gui.route.control_state import (
    ApiRouteControlCommand,
    ApiRouteControlState,
    api_route_control_command_from_payload,
)
from probe_station_gui.route.measurement import filter_route_points_by_previous_status
from probe_station_gui.route.measurement_records import RouteMeasurementPoint
from probe_station_gui.route.model import MeasurementRoute, structure_number_from_labels


StageFromDesign = Callable[[Point2D], Point2D | None]
RoutePointsFactory = Callable[[object], Iterable[RouteMeasurementPoint]]
StructureNumberForPoint = Callable[[RouteMeasurementPoint], int]


@dataclass(frozen=True)
class RouteMeasurementStartPlan:
    points: list[RouteMeasurementPoint]
    selected_point: RouteMeasurementPoint
    previous_ok_skipped_count: int | None


@dataclass(frozen=True)
class RouteMeasurementStartDecision:
    plan: RouteMeasurementStartPlan | None = None
    message: str = ""
    timeout_ms: int = 5000
    dialog_status: bool = False

    @property
    def accepted(self) -> bool:
        return self.plan is not None


class ApiRouteControlActionEffect(str, Enum):
    REJECT = "reject"
    STATUS = "status"
    START = "start"
    PAUSE = "pause"
    PAUSE_ACK = "pause_ack"
    INTERRUPT = "interrupt"
    RESUME = "resume"
    STOP = "stop"
    FINISH = "finish"
    CLEAR_ACTION = "clear_action"


@dataclass(frozen=True)
class ApiRouteControlActionPlan:
    effect: ApiRouteControlActionEffect
    command: ApiRouteControlCommand
    state: ApiRouteControlState
    message: str = ""
    status_code: int = 200
    requires_control_window_open: bool = False

    @property
    def accepted(self) -> bool:
        return self.effect is not ApiRouteControlActionEffect.REJECT

    def rejection_payload(self) -> dict[str, Any]:
        return {
            "accepted": False,
            "status_code": int(self.status_code),
            "message": self.message,
        }


def route_measurement_points_for_route(
    route: MeasurementRoute,
    *,
    stage_from_design: StageFromDesign,
    contact_objective_offset: Point2D,
    photo_objective_offset: Point2D,
) -> list[RouteMeasurementPoint]:
    """Resolve enabled route points into contact and photo stage coordinates."""

    points: list[RouteMeasurementPoint] = []
    for route_index, route_point in enumerate(route.points, start=1):
        if not route_point.enabled:
            continue
        design_center = (
            float(route_point.camera_center[0]),
            float(route_point.camera_center[1]),
        )
        camera_stage_xy = stage_from_design(design_center)
        if camera_stage_xy is None:
            raise DesignModelError(
                "Design registration is required before measuring a route."
            )
        contact_stage_xy = camera_stage_to_raw_stage(
            (float(camera_stage_xy[0]), float(camera_stage_xy[1])),
            contact_objective_offset,
        )
        photo_stage_xy = camera_stage_to_raw_stage(
            (float(camera_stage_xy[0]), float(camera_stage_xy[1])),
            photo_objective_offset,
        )
        hits = route.needle_hits_for_point(route_point)
        needle_1_design = hits[0][1] if len(hits) > 0 else design_center
        needle_2_design = hits[1][1] if len(hits) > 1 else design_center
        points.append(
            RouteMeasurementPoint(
                index=route_index,
                point_id=route_point.id,
                label=route_point.label,
                design_center=design_center,
                stage_xy=(
                    float(contact_stage_xy[0]),
                    float(contact_stage_xy[1]),
                ),
                needle_1_design=(
                    float(needle_1_design[0]),
                    float(needle_1_design[1]),
                ),
                needle_2_design=(
                    float(needle_2_design[0]),
                    float(needle_2_design[1]),
                ),
                photo_stage_xy=(
                    float(photo_stage_xy[0]),
                    float(photo_stage_xy[1]),
                ),
            )
        )
    return points


def route_measurement_start_decision(
    *,
    route: object | None,
    registration_valid: bool,
    points_factory: RoutePointsFactory,
    current_point: int,
    previous_ok_only: bool,
    previous_csv_path: str | Path,
    structure_number_for_point: StructureNumberForPoint | None = None,
) -> RouteMeasurementStartDecision:
    """Plan the route points and selected starting point for a measurement run."""

    if route is None or not getattr(route, "points", None):
        return RouteMeasurementStartDecision(
            message="Create or load a probe route before measuring.",
            timeout_ms=5000,
        )
    if not registration_valid:
        return RouteMeasurementStartDecision(
            message="Design registration is required before measuring a route.",
            timeout_ms=6000,
        )
    try:
        points = list(points_factory(route))
    except DesignModelError as exc:
        return RouteMeasurementStartDecision(message=str(exc), timeout_ms=6000)
    if not points:
        return RouteMeasurementStartDecision(
            message="Route has no enabled points.",
            timeout_ms=5000,
        )

    previous_ok_skipped_count: int | None = None
    if previous_ok_only:
        original_point_count = len(points)
        try:
            points = filter_route_points_by_previous_status(
                points,
                previous_csv_path,
                allowed_statuses={"ok"},
            )
        except (OSError, csv.Error) as exc:
            return RouteMeasurementStartDecision(
                message=f"Unable to read previous route CSV: {exc}",
                timeout_ms=8000,
                dialog_status=True,
            )
        previous_ok_skipped_count = original_point_count - len(points)
        if not points:
            return RouteMeasurementStartDecision(
                message="Previous route CSV has no OK points for this route.",
                timeout_ms=8000,
                dialog_status=True,
            )

    selected_point_number = int(current_point)
    selected_point = find_route_contact_point(
        points,
        selected_point_number,
        structure_number_for_point=structure_number_for_point,
    )
    if selected_point is None:
        return RouteMeasurementStartDecision(
            message=(
                f"Contact {selected_point_number} is not enabled or not included "
                "by the current route filter."
            ),
            timeout_ms=6000,
            dialog_status=True,
        )
    return RouteMeasurementStartDecision(
        plan=RouteMeasurementStartPlan(
            points=points,
            selected_point=selected_point,
            previous_ok_skipped_count=previous_ok_skipped_count,
        )
    )


def find_route_contact_point(
    points: Iterable[RouteMeasurementPoint],
    contact_number: int,
    *,
    structure_number_for_point: StructureNumberForPoint | None = None,
) -> RouteMeasurementPoint | None:
    """Find a route point by route index first, then by structure number."""

    point_list = list(points)
    try:
        requested = int(contact_number)
    except (TypeError, ValueError):
        return None
    for point in point_list:
        if int(point.index) == requested:
            return point
    structure_number = structure_number_for_point or _structure_number_for_point
    for point in point_list:
        if int(structure_number(point)) == requested:
            return point
    return None


def api_route_control_action_plan(
    payload: dict[str, Any],
    state: ApiRouteControlState,
    *,
    updated_utc: str,
) -> ApiRouteControlActionPlan:
    """Plan the pure API Route Control state transition for one command."""

    state = state if isinstance(state, ApiRouteControlState) else ApiRouteControlState()
    command = api_route_control_command_from_payload(payload)
    if command.requires_active_control and not state.active:
        return _api_route_control_plan(
            ApiRouteControlActionEffect.REJECT,
            command,
            state,
            "No API route control run is active.",
            status_code=409,
        )
    if command.kind == "start":
        next_state, message = state.start(
            label=command.label,
            updated_utc=updated_utc,
        )
        return _api_route_control_plan(
            ApiRouteControlActionEffect.START, command, next_state, message
        )
    if command.kind == "pause":
        next_state, message = state.request_pause(updated_utc=updated_utc)
        return _api_route_control_plan(
            ApiRouteControlActionEffect.PAUSE, command, next_state, message
        )
    if command.kind == "pause_ack":
        next_state, message = state.ack_pause(updated_utc=updated_utc)
        return _api_route_control_plan(
            ApiRouteControlActionEffect.PAUSE_ACK, command, next_state, message
        )
    if command.kind == "interrupt":
        next_state, message = state.interrupt(updated_utc=updated_utc)
        return _api_route_control_plan(
            ApiRouteControlActionEffect.INTERRUPT, command, next_state, message
        )
    if command.kind == "resume":
        next_state, message = state.resume(
            action=command.action,
            explicit_action=command.explicit_action,
            updated_utc=updated_utc,
        )
        return _api_route_control_plan(
            ApiRouteControlActionEffect.RESUME,
            command,
            next_state,
            message,
            requires_control_window_open=True,
        )
    if command.kind == "stop":
        next_state, message = state.stop(updated_utc=updated_utc)
        return _api_route_control_plan(
            ApiRouteControlActionEffect.STOP, command, next_state, message
        )
    if command.kind == "finish":
        next_state, message = state.finish(
            label=command.label,
            updated_utc=updated_utc,
        )
        return _api_route_control_plan(
            ApiRouteControlActionEffect.FINISH, command, next_state, message
        )
    if command.kind == "clear_action":
        next_state, _message = state.clear_action(updated_utc=updated_utc)
        return _api_route_control_plan(
            ApiRouteControlActionEffect.CLEAR_ACTION, command, next_state
        )
    if command.kind == "status":
        return _api_route_control_plan(ApiRouteControlActionEffect.STATUS, command, state)
    return _api_route_control_plan(
        ApiRouteControlActionEffect.REJECT,
        command,
        state,
        f"Unknown API route control action: {command.action}",
        status_code=400,
    )


def _api_route_control_plan(
    effect: ApiRouteControlActionEffect,
    command: ApiRouteControlCommand,
    state: ApiRouteControlState,
    message: str = "",
    status_code: int = 200,
    requires_control_window_open: bool = False,
) -> ApiRouteControlActionPlan:
    return ApiRouteControlActionPlan(
        effect=effect,
        command=command,
        state=state,
        message=message,
        status_code=status_code,
        requires_control_window_open=requires_control_window_open,
    )


def _structure_number_for_point(point: RouteMeasurementPoint) -> int:
    return structure_number_from_labels(
        point.label,
        point.point_id,
        default=point.index,
    )


__all__ = [
    "ApiRouteControlActionEffect",
    "ApiRouteControlActionPlan",
    "RouteMeasurementStartDecision",
    "RouteMeasurementStartPlan",
    "api_route_control_action_plan",
    "find_route_contact_point",
    "route_measurement_points_for_route",
    "route_measurement_start_decision",
]
