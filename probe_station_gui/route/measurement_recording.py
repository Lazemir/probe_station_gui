"""Route measurement record creation and result policy."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime
from typing import Any

from probe_station_gui.route.contact_quality import (
    RouteContactQuality,
    RouteMeasurementSample,
    _format_contact_quality_failure,
)
from probe_station_gui.route.measurement_records import (
    RouteContactSeekResult,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
)
from probe_station_gui.route.model import structure_number_from_labels


def structure_number_for_point(point: RouteMeasurementPoint) -> int:
    return structure_number_from_labels(
        point.label,
        point.point_id,
        default=point.index,
    )


def record_for_point(
    owner: Any,
    *,
    point: RouteMeasurementPoint,
    samples: list[RouteMeasurementSample],
) -> RouteMeasurementRecord:
    from probe_station_gui.route import contact_measurement

    stats = contact_measurement.resistance_stats_from_samples(samples)
    if stats.complete_finite_batch:
        contact_quality = contact_measurement.contact_quality_from_samples(
            owner,
            samples,
        )
        status = contact_measurement.record_status_for_samples(
            owner,
            samples,
            contact_quality,
        )
    else:
        status = "overload"
        contact_quality = None
    return RouteMeasurementRecord(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        structure_number=structure_number_for_point(point),
        nplc=owner._nplc_label,
        measurement_type=owner._measurement_type,
        n_measurements=stats.count,
        resistance_ohm=stats.mean_ohm,
        resistance_rms_ohm=stats.rms_ohm,
        relative_rms=stats.relative_rms,
        status=status,
        contact_quality=contact_quality,
        raw_samples=tuple(samples),
    )


def record_route_point_measurement(
    owner: Any,
    *,
    point: RouteMeasurementPoint,
    record: RouteMeasurementRecord,
    position: int,
    total: int,
) -> tuple[RouteMeasurementRecord, bool, bool, bool, int]:
    save_exhausted_bad_contact = should_save_exhausted_bad_contact(owner, record)
    record_saved = False
    quality_rejected = False
    if (
        owner._confirm_each_point
        and record_exceeds_quality_limit(owner, record)
        and not save_exhausted_bad_contact
    ):
        if not record_has_failed_contact_quality(record):
            record = replace(record, status="unstable")
        quality_rejected = True
    else:
        owner._csv_writer.append(record)
        record_saved = True
    owner._emit_contact_photo(
        point,
        record,
        position,
        total,
        record_saved,
    )
    owner._emit_result(record, position, total, record_saved)
    return (
        record,
        record_saved,
        quality_rejected,
        save_exhausted_bad_contact,
        1 if record_saved else 0,
    )


def record_exceeds_quality_limit(owner: Any, record: RouteMeasurementRecord) -> bool:
    if record.status == "short":
        return False
    if record_has_failed_contact_quality(record):
        return True
    if owner._max_relative_rms is None or record.status != "ok":
        return False
    return (
        math.isfinite(record.relative_rms)
        and record.relative_rms > owner._max_relative_rms
    )


def record_has_failed_contact_quality(record: RouteMeasurementRecord) -> bool:
    contact_quality = record.contact_quality
    return contact_quality is not None and contact_quality.good is False


def contact_placement_record_is_success(record: RouteMeasurementRecord) -> bool:
    return str(record.status).strip().lower() in {"ok", "short"}


def should_save_exhausted_bad_contact(
    owner: Any,
    record: RouteMeasurementRecord,
) -> bool:
    if not record_has_failed_contact_quality(record):
        return False
    contact_seek = owner._current_contact_seek_result
    if (
        contact_seek is None
        or contact_seek.found
        or contact_seek.status != "not_found"
    ):
        return False
    depth = float(contact_seek.depth_below_down_mm)
    max_depth = float(contact_seek.max_depth_mm)
    return (
        math.isfinite(depth)
        and math.isfinite(max_depth)
        and max_depth > 0.0
        and depth >= max_depth - 1e-9
    )


def contact_quality_failure_suffix(
    owner: Any,
    quality: RouteContactQuality,
) -> str:
    detail = _format_contact_quality_failure(
        quality,
        owner._contact_quality_limits,
    )
    return f", failed criterion: {detail}" if detail else ""


def contact_placement_message(
    owner: Any,
    *,
    action_label: str,
    failure_label: str,
    point: RouteMeasurementPoint,
    record: RouteMeasurementRecord,
    position: int,
    total: int,
    success: bool,
    seek: RouteContactSeekResult | None = None,
) -> str:
    status = record.status or "unknown"
    contact_suffix = (
        contact_quality_failure_suffix(owner, record.contact_quality)
        if record.contact_quality is not None
        else ""
    )
    if seek is not None:
        return (
            f"{action_label}: point {position}/{total} {point.label}, "
            f"{seek.status}, final={status}{contact_suffix}."
        )
    if success:
        return (
            f"{action_label}: point {position}/{total} {point.label}, "
            f"{status}."
        )
    return (
        f"{failure_label}: point {position}/{total} "
        f"{point.label}, {status}{contact_suffix}."
    )


def saved_route_point_status_detail(
    record: RouteMeasurementRecord,
    *,
    save_exhausted_bad_contact: bool,
) -> str:
    if save_exhausted_bad_contact:
        return "contact seek exhausted; saved"
    if record.status == "short":
        return "short-circuit detected; saved"
    return "saved"


__all__ = [
    "contact_placement_message",
    "contact_placement_record_is_success",
    "contact_quality_failure_suffix",
    "record_exceeds_quality_limit",
    "record_for_point",
    "record_has_failed_contact_quality",
    "record_route_point_measurement",
    "saved_route_point_status_detail",
    "should_save_exhausted_bad_contact",
    "structure_number_for_point",
]
