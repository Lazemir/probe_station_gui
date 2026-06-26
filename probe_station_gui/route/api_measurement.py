from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from probe_station_gui.design.model import DesignModelError
from probe_station_gui.route.contact_quality import RouteContactQualityLimits
from probe_station_gui.route.measurement_payloads import (
    route_api_contact_seek_payload,
    route_api_measurement_record_payload,
)
from probe_station_gui.route.measurement_records import (
    RouteContactPlacementResult,
    RouteMeasurementPoint,
)
from probe_station_gui.route.payload_parsing import payload_bool, payload_float, payload_int
from probe_station_gui.route.session_start import (
    route_contact_quality_limits_from_payload,
    route_max_relative_rms_from_payload,
)

_MISSING = object()


@dataclass(frozen=True)
class ApiCurrentContactSettings:
    check_sample_count: int
    measurement_count: int
    contact_seek_range_mm: float
    contact_seek_step_mm: float
    contact_settle_s: float
    contact_quality_limits: RouteContactQualityLimits
    max_relative_rms: float | None


@dataclass(frozen=True)
class ApiCurrentContactFailureAlert:
    channel: str
    text: str
    attach_photo: bool
    reply_markup: object | None


def api_contact_number_from_payload(payload: dict[str, object]) -> int | None:
    for key in ("contact_number", "contact", "point_number", "structure_number"):
        if key not in payload:
            continue
        try:
            value = int(payload[key])
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None
    return None


def api_current_contact_settings_from_payload(
    payload: dict[str, object],
    *,
    default_check_sample_count: int,
    default_contact_seek_range_mm: float,
    default_contact_seek_step_mm: float,
    default_contact_settle_s: float,
) -> ApiCurrentContactSettings:
    check_sample_count = payload_int(
        payload,
        "check_sample_count",
        "initial_measurement_count",
        "initial_samples",
        default=default_check_sample_count,
        minimum=2,
    )
    return ApiCurrentContactSettings(
        check_sample_count=check_sample_count,
        measurement_count=payload_int(
            payload,
            "measurement_count",
            "sample_count",
            "samples",
            default=check_sample_count,
            minimum=check_sample_count,
        ),
        contact_seek_range_mm=payload_float(
            payload,
            "contact_seek_range_mm",
            "contact_seek_max_total_mm",
            "seek_range_mm",
            default=default_contact_seek_range_mm,
            minimum=0.0,
        ),
        contact_seek_step_mm=payload_float(
            payload,
            "contact_seek_step_mm",
            "seek_step_mm",
            default=abs(default_contact_seek_step_mm),
            minimum=0.0,
        ),
        contact_settle_s=payload_float(
            payload,
            "contact_settle_s",
            "settle_s",
            default=default_contact_settle_s,
            minimum=0.0,
        ),
        contact_quality_limits=route_contact_quality_limits_from_payload(payload),
        max_relative_rms=route_max_relative_rms_from_payload(payload),
    )


def api_current_contact_error_response(
    message: str,
    *,
    status_code: int,
    contact: object = _MISSING,
) -> dict[str, object]:
    response: dict[str, object] = {
        "accepted": False,
        "status_code": int(status_code),
        "message": str(message),
    }
    if contact is not _MISSING:
        response["contact"] = contact
    return response


def api_current_contact_response(
    result: RouteContactPlacementResult,
    *,
    contact: dict[str, object],
    settings: ApiCurrentContactSettings,
    needle_feedrate: float | None,
    timestamp_utc: str,
    seek: bool,
) -> dict[str, object]:
    response: dict[str, object] = {
        "accepted": True,
        "message": result.message,
        "timestamp_utc": str(timestamp_utc),
        "contact": contact,
        "needle_feedrate_mm_min": needle_feedrate,
        "contact_ok": bool(result.success),
        "check_sample_count": settings.check_sample_count,
        "measurement_count": settings.measurement_count,
        "contact_settle_s": settings.contact_settle_s,
        "contact_seek_range_mm": settings.contact_seek_range_mm,
        "contact_seek_step_mm": settings.contact_seek_step_mm,
        "contact_quality_limits": settings.contact_quality_limits.as_dict(),
        "measurement": route_api_measurement_record_payload(result.record),
        "contact_seek": route_api_contact_seek_payload(result.contact_seek),
    }
    if seek:
        response["contact_found"] = api_current_contact_found(result)
    return response


def api_current_contact_failure_alert(
    payload: dict[str, object],
    *,
    contact_number: int,
    result: RouteContactPlacementResult,
    reply_markup: object | None,
) -> ApiCurrentContactFailureAlert | None:
    if bool(result.success) or api_current_contact_found(result):
        return None
    if not payload_bool(
        payload,
        "telegram_on_failure",
        "telegram_on_seek_failure",
        "notify_on_failure",
        default=False,
    ):
        return None
    return ApiCurrentContactFailureAlert(
        channel="route_attention",
        text=(
            "Probe route needs attention:\n"
            f"Contact seek failed for contact {contact_number}: {result.message}"
        ),
        attach_photo=True,
        reply_markup=reply_markup,
    )


def api_measurement_point_payload(
    point: RouteMeasurementPoint,
    *,
    requested_contact_number: int,
    structure_number: int,
    route_offset_xy: tuple[float, float],
    adjusted_stage_xy: tuple[float, float],
) -> dict[str, object]:
    return {
        "contact_number": int(requested_contact_number),
        "route_index": int(point.index),
        "structure_number": int(structure_number),
        "point_id": point.point_id,
        "label": point.label,
        "design_center": {
            "x": float(point.design_center[0]),
            "y": float(point.design_center[1]),
        },
        "stage_xy": {
            "x_mm": float(point.stage_xy[0]),
            "y_mm": float(point.stage_xy[1]),
        },
        "route_offset_xy": {
            "dx_mm": float(route_offset_xy[0]),
            "dy_mm": float(route_offset_xy[1]),
        },
        "adjusted_stage_xy": {
            "x_mm": float(adjusted_stage_xy[0]),
            "y_mm": float(adjusted_stage_xy[1]),
        },
        "needle_contacts": [
            {
                "needle": 1,
                "design": {
                    "x": float(point.needle_1_design[0]),
                    "y": float(point.needle_1_design[1]),
                },
            },
            {
                "needle": 2,
                "design": {
                    "x": float(point.needle_2_design[0]),
                    "y": float(point.needle_2_design[1]),
                },
            },
        ],
    }


def api_contact_context_response(
    contact_number: int,
    *,
    serial_available: bool,
    route: object | None,
    registration: object | None,
    points_factory: Callable[[object], list[RouteMeasurementPoint]],
    point_finder: Callable[[list[RouteMeasurementPoint], int], RouteMeasurementPoint | None],
    route_offset_xy: tuple[float, float],
    adjusted_stage_xy: Callable[[RouteMeasurementPoint], tuple[float, float]],
    structure_number_for_point: Callable[[RouteMeasurementPoint], int],
) -> dict[str, object]:
    if not serial_available:
        return api_current_contact_error_response(
            "Serial connection is not available.",
            status_code=503,
        )
    if route is None or not getattr(route, "points", None):
        return api_current_contact_error_response(
            "Create or load a probe route before using contacts.",
            status_code=409,
        )
    if registration is None or not bool(getattr(registration, "valid", False)):
        return api_current_contact_error_response(
            "Design registration is required before using contacts.",
            status_code=409,
        )
    try:
        points = points_factory(route)
    except DesignModelError as exc:
        return api_current_contact_error_response(str(exc), status_code=409)
    point = point_finder(points, contact_number)
    if point is None:
        return api_current_contact_error_response(
            f"Contact {contact_number} is not enabled or not found.",
            status_code=404,
        )
    return {
        "accepted": True,
        "point": point,
        "contact": api_measurement_point_payload(
            point,
            requested_contact_number=contact_number,
            structure_number=structure_number_for_point(point),
            route_offset_xy=route_offset_xy,
            adjusted_stage_xy=adjusted_stage_xy(point),
        ),
    }


def api_current_contact_found(result: RouteContactPlacementResult) -> bool:
    contact_seek = result.contact_seek
    return bool(
        result.success
        or (contact_seek is not None and bool(getattr(contact_seek, "found", False)))
    )


__all__ = [
    "ApiCurrentContactFailureAlert",
    "ApiCurrentContactSettings",
    "api_contact_context_response",
    "api_contact_number_from_payload",
    "api_current_contact_error_response",
    "api_current_contact_failure_alert",
    "api_current_contact_response",
    "api_current_contact_settings_from_payload",
    "api_measurement_point_payload",
]
