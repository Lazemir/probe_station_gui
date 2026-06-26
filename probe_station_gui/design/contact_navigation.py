from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from probe_station_gui.route.api_measurement import (
    api_contact_context_response,
    api_contact_number_from_payload,
)
from probe_station_gui.route.measurement import RouteMeasurementPoint
from probe_station_gui.route.operation import find_route_contact_point
from probe_station_gui.route.payload_parsing import payload_bool, payload_float


@dataclass(frozen=True)
class ApiMoveToContactRequest:
    contact_number: int
    lower_needles: bool
    lift_before_move: bool
    lift_after: bool
    contact_settle_s: float
    needle_feedrate_mm_min: float | None


@dataclass(frozen=True)
class ApiMoveToContactPlan:
    point: object
    contact: dict[str, object]
    request: ApiMoveToContactRequest


@dataclass(frozen=True)
class ApiContactNeedlesRequest:
    contact_number: int
    action: str
    needle_feedrate_mm_min: float | None


@dataclass(frozen=True)
class ApiContactNeedlesPlan:
    contact: dict[str, object]
    request: ApiContactNeedlesRequest


def api_move_to_contact_request(
    payload: dict[str, object],
    *,
    default_needle_feedrate: float | None,
    min_feedrate: float,
) -> ApiMoveToContactRequest | dict[str, object]:
    contact_number = api_contact_number_from_payload(payload)
    if contact_number is None:
        return _missing_contact_number_response()
    return ApiMoveToContactRequest(
        contact_number=contact_number,
        lower_needles=payload_bool(
            payload,
            "lower_needles",
            "lower",
            default=False,
        ),
        lift_before_move=payload_bool(
            payload,
            "lift_before_move",
            default=True,
        ),
        lift_after=payload_bool(payload, "lift_after", default=False),
        contact_settle_s=payload_float(
            payload,
            "contact_settle_s",
            "settle_s",
            default=0.2,
            minimum=0.0,
        ),
        needle_feedrate_mm_min=api_needle_feedrate_from_payload(
            payload,
            default_needle_feedrate=default_needle_feedrate,
            min_feedrate=min_feedrate,
        ),
    )


def api_move_to_contact_plan(
    payload: dict[str, object],
    *,
    contact_context: Callable[[int], dict[str, object]],
    default_needle_feedrate: float | None,
    min_feedrate: float,
) -> ApiMoveToContactPlan | dict[str, object]:
    request = api_move_to_contact_request(
        payload,
        default_needle_feedrate=default_needle_feedrate,
        min_feedrate=min_feedrate,
    )
    if isinstance(request, dict):
        return request
    context_result = contact_context(request.contact_number)
    if not context_result.get("accepted", False):
        return context_result
    return ApiMoveToContactPlan(
        point=context_result["point"],
        contact=context_result["contact"],
        request=request,
    )


def api_move_to_contact_success_response(
    plan: ApiMoveToContactPlan,
    *,
    timestamp_utc: str,
    route_offset_xy: tuple[float, float],
    target_stage_xy: tuple[float, float],
    needles_lowered: bool,
) -> dict[str, object]:
    request = plan.request
    return {
        "accepted": True,
        "message": (
            f"Moved to contact {plan.contact['contact_number']}"
            + (" and lowered needles." if request.lower_needles else ".")
        ),
        "timestamp_utc": str(timestamp_utc),
        "contact": plan.contact,
        "needles_lowered": bool(request.lower_needles),
        "lifted_before_move": bool(request.lift_before_move),
        "lifted_after": bool(request.lift_after and needles_lowered),
        "needle_feedrate_mm_min": request.needle_feedrate_mm_min,
        "route_offset_xy": {
            "dx_mm": float(route_offset_xy[0]),
            "dy_mm": float(route_offset_xy[1]),
        },
        "target_stage_xy": {
            "x_mm": float(target_stage_xy[0]),
            "y_mm": float(target_stage_xy[1]),
        },
    }


def api_move_to_contact_stage_error_response(
    message: str,
    *,
    contact: dict[str, object],
) -> dict[str, object]:
    return {
        "accepted": False,
        "status_code": 409,
        "message": str(message),
        "contact": contact,
    }


def api_contact_needles_request(
    payload: dict[str, object],
    *,
    default_needle_feedrate: float | None,
    min_feedrate: float,
) -> ApiContactNeedlesRequest | dict[str, object]:
    contact_number = api_contact_number_from_payload(payload)
    if contact_number is None:
        return _missing_contact_number_response()
    action = _normalize_needle_action(payload)
    if action is None:
        return {
            "accepted": False,
            "status_code": 400,
            "message": "Needle action must be lower, lift, or raise.",
        }
    return ApiContactNeedlesRequest(
        contact_number=contact_number,
        action=action,
        needle_feedrate_mm_min=api_needle_feedrate_from_payload(
            payload,
            default_needle_feedrate=default_needle_feedrate,
            min_feedrate=min_feedrate,
        ),
    )


def api_contact_needles_plan(
    payload: dict[str, object],
    *,
    contact_context: Callable[[int], dict[str, object]],
    default_needle_feedrate: float | None,
    min_feedrate: float,
) -> ApiContactNeedlesPlan | dict[str, object]:
    request = api_contact_needles_request(
        payload,
        default_needle_feedrate=default_needle_feedrate,
        min_feedrate=min_feedrate,
    )
    if isinstance(request, dict):
        return request
    context_result = contact_context(request.contact_number)
    if not context_result.get("accepted", False):
        return context_result
    return ApiContactNeedlesPlan(
        contact=context_result["contact"],
        request=request,
    )


def api_contact_needles_success_response(
    plan: ApiContactNeedlesPlan,
    *,
    timestamp_utc: str,
) -> dict[str, object]:
    return {
        "accepted": True,
        "message": f"Needle action '{plan.request.action}' completed.",
        "timestamp_utc": str(timestamp_utc),
        "contact": plan.contact,
        "needle_action": plan.request.action,
        "needle_feedrate_mm_min": plan.request.needle_feedrate_mm_min,
    }


def api_contact_needles_stage_error_response(
    message: str,
    *,
    contact: dict[str, object],
) -> dict[str, object]:
    return {
        "accepted": False,
        "status_code": 409,
        "message": str(message),
        "contact": contact,
    }


def api_contact_context(
    contact_number: int,
    *,
    serial_available: bool,
    route: object | None,
    registration: object | None,
    points_factory: Callable[[object], list[RouteMeasurementPoint]],
    route_offset_xy: tuple[float, float],
    structure_number_for_point: Callable[[RouteMeasurementPoint], int],
) -> dict[str, object]:
    return api_contact_context_response(
        contact_number,
        serial_available=serial_available,
        route=route,
        registration=registration,
        points_factory=points_factory,
        point_finder=lambda points, selected_contact_number: find_route_contact_point(
            points,
            selected_contact_number,
            structure_number_for_point=structure_number_for_point,
        ),
        route_offset_xy=route_offset_xy,
        adjusted_stage_xy=lambda point: api_route_adjusted_stage_xy(
            point,
            route_offset_xy=route_offset_xy,
        ),
        structure_number_for_point=structure_number_for_point,
    )


def api_route_adjusted_stage_xy(
    point: RouteMeasurementPoint,
    *,
    route_offset_xy: tuple[float, float],
) -> tuple[float, float]:
    return (
        float(point.stage_xy[0]) + float(route_offset_xy[0]),
        float(point.stage_xy[1]) + float(route_offset_xy[1]),
    )


def api_route_point_payload(
    *,
    route_index: int,
    route_point: object,
    include_stage_xy: bool,
    resolve_stage_xy: Callable[[tuple[float, float]], tuple[float, float] | None],
    structure_number_for_route_point: Callable[[int, object], int],
) -> dict[str, object]:
    design_center = tuple(getattr(route_point, "camera_center", (0.0, 0.0)))
    stage_xy = None
    if include_stage_xy:
        try:
            resolved = resolve_stage_xy(
                (float(design_center[0]), float(design_center[1]))
            )
        except (TypeError, ValueError, IndexError):
            resolved = None
        if resolved is not None:
            stage_xy = {"x_mm": float(resolved[0]), "y_mm": float(resolved[1])}
    return {
        "contact_number": int(route_index),
        "structure_number": structure_number_for_route_point(
            route_index,
            route_point,
        ),
        "point_id": str(getattr(route_point, "id", "")),
        "label": str(getattr(route_point, "label", "")),
        "enabled": bool(getattr(route_point, "enabled", True)),
        "design_center": {
            "x": float(design_center[0]),
            "y": float(design_center[1]),
        },
        "stage_xy": stage_xy,
    }


def _missing_contact_number_response() -> dict[str, object]:
    return {
        "accepted": False,
        "status_code": 400,
        "message": "Provide a positive contact_number.",
    }


def _normalize_needle_action(payload: dict[str, object]) -> str | None:
    action = str(payload.get("action", "lower")).strip().lower()
    if action == "raise":
        return "raise"
    if action in {"lift", "up"}:
        return "lift"
    if action in {"lower", "down"}:
        return "lower"
    return None


def api_needle_feedrate_from_payload(
    payload: dict[str, object],
    *,
    default_needle_feedrate: float | None,
    min_feedrate: float,
) -> float | None:
    for key in ("needle_feedrate_mm_min", "feedrate_mm_min", "feedrate"):
        if key not in payload or payload.get(key) is None:
            continue
        value = payload_float(payload, key, default=math.nan, minimum=0.0)
        return max(min_feedrate, value)
    return (
        float(default_needle_feedrate)
        if default_needle_feedrate is not None
        else None
    )


__all__ = [
    "ApiContactNeedlesPlan",
    "ApiContactNeedlesRequest",
    "ApiMoveToContactPlan",
    "ApiMoveToContactRequest",
    "api_contact_context",
    "api_needle_feedrate_from_payload",
    "api_contact_needles_plan",
    "api_contact_needles_request",
    "api_contact_needles_stage_error_response",
    "api_contact_needles_success_response",
    "api_move_to_contact_plan",
    "api_move_to_contact_request",
    "api_move_to_contact_stage_error_response",
    "api_move_to_contact_success_response",
    "api_route_adjusted_stage_xy",
    "api_route_point_payload",
]
