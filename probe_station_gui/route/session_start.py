"""API route-session start payload parsing."""

from __future__ import annotations

from dataclasses import dataclass

from probe_station_gui.route.contact_quality import (
    RouteContactQualityLimits as _RouteContactQualityLimits,
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
    "DEFAULT_EXTERNAL_SESSION_FOLLOWUP_MEASUREMENT_COUNT",
    "DEFAULT_EXTERNAL_SESSION_INITIAL_MEASUREMENT_COUNT",
    "DEFAULT_EXTERNAL_SESSION_PHOTO_FOCUS_RANGE_MM",
    "DEFAULT_EXTERNAL_SESSION_PHOTO_SETTLE_S",
    "RouteExternalSessionStartSettings",
    "route_contact_quality_limits_from_payload",
    "route_external_session_start_settings_from_payload",
    "route_max_relative_rms_from_payload",
]
