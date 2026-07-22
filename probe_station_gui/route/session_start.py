"""Pure route-session start parsing, planning, and presentation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from probe_station_gui.design.model import DesignModelError
from probe_station_gui.route.contact_quality import (
    RouteContactQualityLimits as _RouteContactQualityLimits,
)
from probe_station_gui.route.measurement_records import RouteMeasurementPoint
from probe_station_gui.route.operation import find_route_contact_point
from probe_station_gui.route.operation_modes import (
    route_operation_measure_enabled,
    route_operation_photo_enabled,
)
from probe_station_gui.route.payload_parsing import (
    payload_bool as _payload_bool,
    payload_float as _payload_float,
    payload_int as _payload_int,
)


DEFAULT_EXTERNAL_SESSION_INITIAL_MEASUREMENT_COUNT = 10
DEFAULT_EXTERNAL_SESSION_FOLLOWUP_MEASUREMENT_COUNT = 240
DEFAULT_EXTERNAL_SESSION_PHOTO_SETTLE_S = 0.2
DEFAULT_EXTERNAL_SESSION_PHOTO_FOCUS_RANGE_MM = 0.03

RoutePointsFactory = Callable[[object], Iterable[RouteMeasurementPoint]]


@dataclass(frozen=True)
class RouteExternalSessionStartSettings:
    start_point: int
    initial_measurement_count: int
    followup_measurement_count: int
    measurement_count: int
    contact_seek_range_mm: float
    contact_seek_step_mm: float
    contact_settle_s: float
    photo_settle_s: float
    photo_enabled: bool
    photo_focus_enabled: bool
    photo_focus_range_mm: float
    contact_quality_limits: _RouteContactQualityLimits
    max_relative_rms: float | None


@dataclass(frozen=True)
class _RouteSessionMeasurementCounts:
    initial: int
    followup: int
    total: int


@dataclass(frozen=True)
class _RouteSessionPhotoSettings:
    settle_s: float
    enabled: bool
    focus_enabled: bool
    focus_range_mm: float


@dataclass(frozen=True)
class ApiRouteSessionStartPlan:
    points: list[RouteMeasurementPoint]
    selected_point: RouteMeasurementPoint
    start_settings: RouteExternalSessionStartSettings
    design_frame_snapshot: RouteDesignFrameSnapshot | None = None


@dataclass(frozen=True)
class RouteDesignFrameSnapshot:
    frame_id: str
    frame_version: int


@dataclass(frozen=True)
class ApiRouteSessionStartDecision:
    plan: ApiRouteSessionStartPlan | None = None
    message: str = ""
    status_code: int = 200

    @property
    def accepted(self) -> bool:
        return self.plan is not None

    def rejection_payload(self) -> dict[str, Any]:
        return {
            "accepted": False,
            "status_code": int(self.status_code),
            "message": self.message,
        }


@dataclass(frozen=True)
class RouteLaunchPresentation:
    message: str
    point_numbers: list[int]
    remaining_count: int


@dataclass(frozen=True)
class ApiRouteSessionLaunchState:
    session_id: str
    point_numbers: list[int]
    selected_point_number: int
    start_message: str
    photo_enabled: bool
    measure_enabled: bool = True


@dataclass(frozen=True)
class GuiRouteStartPreflight:
    accepted: bool = True
    message: str = ""
    timeout_ms: int = 0
    dialog_status: bool = False
    check_camera_frame: bool = False
    telegram_failure_text: str | None = None
    attach_failure_photo: bool = False


@dataclass(frozen=True)
class GuiRouteLaunchState:
    photo_enabled: bool
    measure_enabled: bool
    presentation: RouteLaunchPresentation
    send_start_telegram: bool


def api_route_session_start_decision(
    *,
    route: object | None,
    registration_valid: bool,
    payload: dict[str, object],
    current_point: int,
    points_factory: RoutePointsFactory,
    default_contact_seek_range_mm: float,
    default_contact_seek_step_mm: float,
    default_contact_settle_s: float,
    design_frame_snapshot: RouteDesignFrameSnapshot | None = None,
) -> ApiRouteSessionStartDecision:
    if route is None or not getattr(route, "points", None):
        return ApiRouteSessionStartDecision(
            message="Create or load a probe route before starting a route session.",
            status_code=409,
        )
    if not registration_valid:
        return ApiRouteSessionStartDecision(
            message="Design registration is required before using contacts.",
            status_code=409,
        )
    try:
        points = list(points_factory(route))
    except DesignModelError as exc:
        return ApiRouteSessionStartDecision(message=str(exc), status_code=409)
    if not points:
        return ApiRouteSessionStartDecision(
            message="Route has no enabled points.",
            status_code=409,
        )
    try:
        start_settings = route_external_session_start_settings_from_payload(
            payload,
            default_start_point=int(current_point),
            default_contact_seek_range_mm=default_contact_seek_range_mm,
            default_contact_seek_step_mm=default_contact_seek_step_mm,
            default_contact_settle_s=default_contact_settle_s,
        )
    except ValueError as exc:
        return ApiRouteSessionStartDecision(message=str(exc), status_code=400)
    selected_point = find_route_contact_point(points, start_settings.start_point)
    if selected_point is None:
        return ApiRouteSessionStartDecision(
            message=(
                f"Contact {start_settings.start_point} is not enabled or not included "
                "by the current route filter."
            ),
            status_code=409,
        )
    return ApiRouteSessionStartDecision(
        plan=ApiRouteSessionStartPlan(
            points=points,
            selected_point=selected_point,
            start_settings=start_settings,
            design_frame_snapshot=design_frame_snapshot,
        )
    )


def snapshot_route_design_frame(
    *,
    frame_id: str | None,
    frame_version: int | None,
) -> RouteDesignFrameSnapshot | None:
    """Capture an immutable Design-frame identity for one route launch."""

    if frame_id is None or frame_version is None:
        return None
    return RouteDesignFrameSnapshot(str(frame_id), int(frame_version))


def route_launch_presentation(
    points: Sequence[RouteMeasurementPoint],
    selected_point: RouteMeasurementPoint,
    *,
    wait_before_first_point: bool = False,
    previous_ok_skipped_count: int | None = None,
    api_session: bool = False,
) -> RouteLaunchPresentation:
    point_list = list(points)
    try:
        start_offset = next(
            index
            for index, point in enumerate(point_list)
            if int(point.index) == int(selected_point.index)
        )
    except StopIteration:
        start_offset = 0
    remaining_count = max(1, len(point_list) - start_offset)
    if api_session:
        message = (
            "Route API session ready at "
            f"point {int(selected_point.index)} {selected_point.label}; "
            f"{len(point_list)} points selected."
        )
    elif wait_before_first_point:
        message = (
            "Preparing route measurement: "
            f"point {int(selected_point.index)} {selected_point.label}; "
            f"{remaining_count} points selected."
        )
    else:
        message = (
            "Route measurement starting at "
            f"point {int(selected_point.index)} {selected_point.label}; "
            f"{remaining_count} points remaining."
        )
    if previous_ok_skipped_count is not None and not api_session:
        message = (
            f"{message} Previous filter skipped "
            f"{previous_ok_skipped_count} points."
        )
    return RouteLaunchPresentation(
        message=message,
        point_numbers=[int(point.index) for point in point_list],
        remaining_count=remaining_count,
    )


def api_route_existing_session_response(
    *,
    payload: dict[str, object],
    status: dict[str, Any],
) -> dict[str, Any]:
    if _payload_bool(
        payload,
        "attach_existing_session",
        "attach_existing",
        "resume_existing",
        default=False,
    ):
        return status
    contact = status.get("current_contact")
    if isinstance(contact, dict):
        point_label = (
            contact.get("label")
            or contact.get("contact_number")
            or contact.get("point_index")
        )
    else:
        point_label = status.get("position")
    return {
        "accepted": False,
        "status_code": 409,
        "message": (
            "External route session is already active"
            f" at {point_label}. Stop it first or pass "
            "attach_existing_session=true to attach explicitly."
        ),
        "active_session": status,
    }


def api_route_session_launch_state(
    *,
    session_id: str,
    points: Sequence[RouteMeasurementPoint],
    selected_point: RouteMeasurementPoint,
    photo_enabled: bool,
) -> ApiRouteSessionLaunchState:
    presentation = route_launch_presentation(
        points,
        selected_point,
        api_session=True,
    )
    return ApiRouteSessionLaunchState(
        session_id=str(session_id),
        point_numbers=presentation.point_numbers,
        selected_point_number=int(selected_point.index),
        start_message=presentation.message,
        photo_enabled=bool(photo_enabled),
    )


def gui_route_start_availability(
    *,
    route_thread_active: bool,
    serial_connected: bool,
) -> GuiRouteStartPreflight:
    if route_thread_active:
        return GuiRouteStartPreflight(
            accepted=False,
            message="Route measurement is already active.",
            timeout_ms=4000,
        )
    if not serial_connected:
        return GuiRouteStartPreflight(
            accepted=False,
            message="Connect the stage controller before measuring a route.",
            timeout_ms=5000,
        )
    return GuiRouteStartPreflight()


def gui_route_start_preflight(
    *,
    photo_enabled: bool,
    photo_autofocus_enabled: bool,
    wait_before_first_point: bool,
    objective_scale_available: bool,
) -> GuiRouteStartPreflight:
    if photo_enabled and not objective_scale_available:
        return GuiRouteStartPreflight(
            accepted=False,
            message=(
                "Calibrate click-to-move for the active objective before saving "
                "microscope photos with a scale bar."
            ),
            timeout_ms=8000,
            dialog_status=True,
        )
    return GuiRouteStartPreflight(
        check_camera_frame=(
            not wait_before_first_point
            and (photo_enabled or photo_autofocus_enabled)
        )
    )


def gui_route_camera_frame_preflight(
    *,
    photo_enabled: bool,
    photo_autofocus_enabled: bool,
    camera_frame_available: bool,
) -> GuiRouteStartPreflight:
    if camera_frame_available or not (photo_enabled or photo_autofocus_enabled):
        return GuiRouteStartPreflight()
    message = (
        "Camera frame is unavailable; cannot capture route photos."
        if photo_enabled
        else "Camera frame is unavailable; cannot autofocus route points."
    )
    return GuiRouteStartPreflight(
        accepted=False,
        message=message,
        timeout_ms=8000,
        dialog_status=True,
        telegram_failure_text=f"Probe route could not start:\n{message}",
        attach_failure_photo=True,
    )


def gui_route_launch_state(
    *,
    operation_mode: object,
    points: Sequence[RouteMeasurementPoint],
    selected_point: RouteMeasurementPoint,
    wait_before_first_point: bool,
    previous_ok_skipped_count: int | None,
) -> GuiRouteLaunchState:
    return GuiRouteLaunchState(
        photo_enabled=route_operation_photo_enabled(operation_mode),
        measure_enabled=route_operation_measure_enabled(operation_mode),
        presentation=route_launch_presentation(
            points,
            selected_point,
            wait_before_first_point=wait_before_first_point,
            previous_ok_skipped_count=previous_ok_skipped_count,
        ),
        send_start_telegram=not wait_before_first_point,
    )


def route_external_session_start_settings_from_payload(
    payload: dict[str, object],
    *,
    default_start_point: int,
    default_contact_seek_range_mm: float,
    default_contact_seek_step_mm: float,
    default_contact_settle_s: float,
) -> RouteExternalSessionStartSettings:
    counts = _route_session_measurement_counts(payload)
    photo = _route_session_photo_settings(payload)
    return RouteExternalSessionStartSettings(
        start_point=_route_session_start_point(payload, default_start_point),
        initial_measurement_count=counts.initial,
        followup_measurement_count=counts.followup,
        measurement_count=counts.total,
        contact_seek_range_mm=_route_session_contact_seek_range(
            payload,
            default=default_contact_seek_range_mm,
        ),
        contact_seek_step_mm=_route_session_contact_seek_step(
            payload,
            default=default_contact_seek_step_mm,
        ),
        contact_settle_s=_payload_float(
            payload,
            "contact_settle_s",
            "settle_s",
            default=default_contact_settle_s,
            minimum=0.0,
        ),
        photo_settle_s=photo.settle_s,
        photo_enabled=photo.enabled,
        photo_focus_enabled=photo.focus_enabled,
        photo_focus_range_mm=photo.focus_range_mm,
        contact_quality_limits=route_contact_quality_limits_from_payload(payload),
        max_relative_rms=route_max_relative_rms_from_payload(payload),
    )


def route_contact_quality_limits_from_payload(
    payload: dict[str, object],
) -> _RouteContactQualityLimits:
    nested = payload.get("contact_quality", payload.get("contact_quality_limits"))
    if nested is None:
        nested_payload: dict[str, object] = {}
    elif isinstance(nested, dict):
        nested_payload = dict(nested)
    else:
        raise ValueError("contact_quality must be an object.")
    combined = dict(payload)
    combined.update(nested_payload)
    defaults = _RouteContactQualityLimits()
    return _RouteContactQualityLimits(
        max_mad_sigma_ohm=_payload_float(
            combined,
            "max_mad_sigma_ohm",
            "contact_max_mad_sigma_ohm",
            default=defaults.max_mad_sigma_ohm,
            minimum=0.0,
        ),
        max_p95_abs_step_ohm=_payload_float(
            combined,
            "max_p95_abs_step_ohm",
            "contact_max_p95_abs_step_ohm",
            default=defaults.max_p95_abs_step_ohm,
            minimum=0.0,
        ),
        max_relative_mad_sigma=_payload_float(
            combined,
            "max_relative_mad_sigma",
            "contact_max_relative_mad_sigma",
            default=defaults.max_relative_mad_sigma,
            minimum=0.0,
        ),
        max_relative_p95_abs_step=_payload_float(
            combined,
            "max_relative_p95_abs_step",
            "contact_max_relative_p95_abs_step",
            default=defaults.max_relative_p95_abs_step,
            minimum=0.0,
        ),
    ).normalized()


def route_max_relative_rms_from_payload(payload: dict[str, object]) -> float | None:
    if not any(
        key in payload
        for key in ("max_relative_rms", "max_rel_rms", "max_relative_rms_percent")
    ):
        return None
    if "max_relative_rms_percent" in payload:
        return (
            _payload_float(
                payload,
                "max_relative_rms_percent",
                default=0.0,
                minimum=0.0,
            )
            / 100.0
        )
    return _payload_float(
        payload,
        "max_relative_rms",
        "max_rel_rms",
        default=0.0,
        minimum=0.0,
    )


def _route_session_start_point(
    payload: dict[str, object],
    default_start_point: int,
) -> int:
    return _payload_int(
        payload,
        "start_point",
        "current_point",
        "contact_number",
        default=int(default_start_point),
        minimum=1,
    )


def _route_session_measurement_counts(
    payload: dict[str, object],
) -> _RouteSessionMeasurementCounts:
    initial_count = _payload_int(
        payload,
        "initial_measurement_count",
        "initial_samples",
        "check_sample_count",
        default=DEFAULT_EXTERNAL_SESSION_INITIAL_MEASUREMENT_COUNT,
        minimum=1,
    )
    followup_count = _payload_int(
        payload,
        "followup_measurement_count",
        "followup_samples",
        default=DEFAULT_EXTERNAL_SESSION_FOLLOWUP_MEASUREMENT_COUNT,
        minimum=0,
    )
    total_count = _route_session_measurement_count(
        payload,
        initial_count=initial_count,
        followup_count=followup_count,
    )
    return _RouteSessionMeasurementCounts(
        initial=initial_count,
        followup=followup_count,
        total=total_count,
    )


def _route_session_measurement_count(
    payload: dict[str, object],
    *,
    initial_count: int,
    followup_count: int,
) -> int:
    return _payload_int(
        payload,
        "measurement_count",
        "sample_count",
        "samples",
        default=int(initial_count) + int(followup_count),
        minimum=int(initial_count),
    )


def _route_session_photo_settings(
    payload: dict[str, object],
) -> _RouteSessionPhotoSettings:
    return _RouteSessionPhotoSettings(
        settle_s=_payload_float(
            payload,
            "photo_settle_s",
            default=DEFAULT_EXTERNAL_SESSION_PHOTO_SETTLE_S,
            minimum=0.0,
        ),
        enabled=_payload_bool(payload, "photo_enabled", "photo", default=True),
        focus_enabled=_payload_bool(
            payload,
            "photo_autofocus_enabled",
            "autofocus",
            "focus",
            default=True,
        ),
        focus_range_mm=_payload_float(
            payload,
            "photo_autofocus_range_mm",
            "focus_range_mm",
            default=DEFAULT_EXTERNAL_SESSION_PHOTO_FOCUS_RANGE_MM,
            minimum=0.001,
        ),
    )


def _route_session_contact_seek_range(
    payload: dict[str, object],
    *,
    default: float,
) -> float:
    return _payload_float(
        payload,
        "contact_seek_range_mm",
        "contact_seek_max_total_mm",
        "seek_range_mm",
        default=float(default),
        minimum=0.0,
    )


def _route_session_contact_seek_step(
    payload: dict[str, object],
    *,
    default: float,
) -> float:
    return _payload_float(
        payload,
        "contact_seek_step_mm",
        "seek_step_mm",
        default=abs(float(default)),
        minimum=0.0,
    )


__all__ = [
    "ApiRouteSessionLaunchState",
    "ApiRouteSessionStartDecision",
    "ApiRouteSessionStartPlan",
    "DEFAULT_EXTERNAL_SESSION_FOLLOWUP_MEASUREMENT_COUNT",
    "DEFAULT_EXTERNAL_SESSION_INITIAL_MEASUREMENT_COUNT",
    "DEFAULT_EXTERNAL_SESSION_PHOTO_FOCUS_RANGE_MM",
    "DEFAULT_EXTERNAL_SESSION_PHOTO_SETTLE_S",
    "api_route_existing_session_response",
    "api_route_session_launch_state",
    "GuiRouteLaunchState",
    "GuiRouteStartPreflight",
    "RouteExternalSessionStartSettings",
    "RouteDesignFrameSnapshot",
    "RouteLaunchPresentation",
    "api_route_session_start_decision",
    "gui_route_camera_frame_preflight",
    "gui_route_launch_state",
    "gui_route_start_availability",
    "gui_route_start_preflight",
    "route_launch_presentation",
    "route_contact_quality_limits_from_payload",
    "route_external_session_start_settings_from_payload",
    "route_max_relative_rms_from_payload",
    "snapshot_route_design_frame",
]
