"""Contact sample readout and contact-seek helpers for route measurements."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

from probe_station_gui.instruments.meters.lcr_helpers import callable_accepts_keyword
from probe_station_gui.route import measurement_recording
from probe_station_gui.route.contact_quality import (
    RouteContactQuality,
    RouteMeasurementSample,
    _contact_quality_from_samples,
    _measurement_sample_from_raw,
)
from probe_station_gui.route.contact_seek import (
    ContactSeekAttempt,
    contact_seek_attempts,
)
from probe_station_gui.route.formatting import (
    format_route_ohm,
    format_route_percent,
)
from probe_station_gui.route.measurement_records import RouteContactSeekResult


@dataclass(frozen=True)
class ResistanceStats:
    count: int
    mean_ohm: float
    rms_ohm: float
    relative_rms: float
    complete_finite_batch: bool


@dataclass(frozen=True)
class ContactSeekAttemptMeasurement:
    samples: list[RouteMeasurementSample]
    axis_a_lowering_mm: float


@dataclass(frozen=True)
class ContactSeekAttemptResolution:
    samples: list[RouteMeasurementSample] | None
    final_status: str
    found: bool = False


def measure_samples(
    owner: Any,
    *,
    position: int,
    total: int,
    prepare_task: Any | None = None,
    after_measurement: Callable[[], object] | None = None,
) -> list[RouteMeasurementSample] | None:
    owner._current_contact_seek_result = None
    initial_count = owner._initial_measurement_count()
    initial_after_measurement = (
        after_measurement if owner._measurement_count <= initial_count else None
    )
    samples = owner._read_measurement_samples(
        initial_count,
        start_index=1,
        prepare_task=prepare_task,
        after_measurement=initial_after_measurement,
    )
    if samples is None:
        return None
    if not owner._auto_contact_seek_on_bad_contact or owner._samples_are_short(samples):
        return owner._complete_measurement_samples(
            samples,
            after_measurement=after_measurement,
        )
    if owner._samples_have_bad_contact(samples):
        return owner._seek_contact_from_current_position(
            initial_samples=samples,
            position=position,
            total=total,
            after_measurement=after_measurement,
        )
    completed_samples = owner._complete_measurement_samples(
        samples,
        after_measurement=after_measurement,
    )
    if (
        completed_samples is None
        or owner._completed_measurement_is_acceptable(completed_samples)
    ):
        return completed_samples
    return owner._seek_contact_from_current_position(
        initial_samples=completed_samples,
        position=position,
        total=total,
        skip_current_depth=True,
        after_measurement=after_measurement,
    )


def complete_measurement_samples(
    owner: Any,
    samples: list[RouteMeasurementSample],
    *,
    after_measurement: Callable[[], object] | None = None,
) -> list[RouteMeasurementSample] | None:
    remaining_count = owner._measurement_count - len(samples)
    if (
        remaining_count <= 0
        or owner._samples_are_short(samples)
        or owner._samples_have_bad_contact(samples)
    ):
        return samples
    extra_samples = owner._read_measurement_samples(
        remaining_count,
        start_index=len(samples) + 1,
        prepare_task=owner._start_measurement_prepare_task(remaining_count),
        after_measurement=after_measurement,
    )
    if extra_samples is None:
        return None
    return samples + extra_samples


def seek_contact_from_current_position(
    owner: Any,
    *,
    initial_samples: list[RouteMeasurementSample],
    position: int,
    total: int,
    skip_current_depth: bool = False,
    after_measurement: Callable[[], object] | None = None,
) -> list[RouteMeasurementSample] | None:
    _ = after_measurement
    if owner._auto_contact_seek_max_total_mm <= 0.0:
        return initial_samples
    lower_to_depth = getattr(
        owner._stage_controller,
        "run_external_needles_lower_to_depth_below_down",
        None,
    )
    adjust = getattr(owner._stage_controller, "run_external_needles_adjust", None)
    if not callable(lower_to_depth) and not callable(adjust):
        return initial_samples
    initial_quality = owner._contact_quality_from_samples(initial_samples)
    _status_contact_seek_start(
        owner,
        initial_quality=initial_quality,
        position=position,
        total=total,
        skip_current_depth=skip_current_depth,
    )
    owner._contact_seek_active.set()
    try:
        return owner._run_contact_seek_attempts(
            initial_samples=initial_samples,
            initial_quality=initial_quality,
            position=position,
            total=total,
            lower_to_depth=lower_to_depth,
            adjust=adjust,
        )
    finally:
        owner._contact_seek_active.clear()


def _status_contact_seek_start(
    owner: Any,
    *,
    initial_quality: RouteContactQuality,
    position: int,
    total: int,
    skip_current_depth: bool,
) -> None:
    if skip_current_depth:
        owner._status(
            f"Route measurement: point {position}/{total} full measurement "
            f"rejected after contact check {initial_quality.status}; "
            "trying deeper contact up to "
            f"{owner._auto_contact_seek_max_total_mm:.3f} mm."
        )
        return
    owner._status(
        f"Route measurement: point {position}/{total} contact check "
        f"{initial_quality.status}, "
        f"median={format_route_ohm(initial_quality.median_ohm)}, "
        f"MAD={format_route_ohm(initial_quality.mad_sigma_ohm)}"
        f"{measurement_recording.contact_quality_failure_suffix(owner, initial_quality)}; "
        "seeking contact up to "
        f"{owner._auto_contact_seek_max_total_mm:.3f} mm."
    )


def run_contact_seek_attempts(
    owner: Any,
    *,
    initial_samples: list[RouteMeasurementSample],
    initial_quality: RouteContactQuality,
    position: int,
    total: int,
    lower_to_depth: Callable[..., object] | None,
    adjust: Callable[..., object] | None,
) -> list[RouteMeasurementSample] | None:
    samples = initial_samples
    attempts_completed = 0
    last_depth_mm = math.nan
    last_axis_a_lowering_mm = math.nan
    last_status = initial_quality.status
    for attempt in contact_seek_attempts(
        owner._auto_contact_seek_step_mm,
        owner._auto_contact_seek_max_total_mm,
    ):
        attempt_measurement = _measure_contact_seek_attempt(
            owner,
            attempt,
            position=position,
            total=total,
            lower_to_depth=lower_to_depth,
            adjust=adjust,
        )
        if attempt_measurement is False:
            return initial_samples
        if attempt_measurement is None:
            return None
        attempts_completed += 1
        last_depth_mm = float(attempt.depth_mm)
        last_axis_a_lowering_mm = attempt_measurement.axis_a_lowering_mm
        resolution = owner._resolve_contact_seek_attempt(
            samples=attempt_measurement.samples,
            depth_label=f"{attempt.depth_mm:.4f} mm below down",
            position=position,
            total=total,
        )
        if resolution.samples is None:
            return None
        samples = resolution.samples
        last_status = resolution.final_status
        if resolution.found:
            owner._set_contact_seek_result(
                found=True,
                status="found" if last_status != "short" else "short",
                attempts=attempts_completed,
                initial_status=initial_quality.status,
                final_status=last_status,
                depth_below_down_mm=attempt.depth_mm,
                axis_a_lowering_mm=last_axis_a_lowering_mm,
            )
            return samples
    _set_exhausted_contact_seek_result(
        owner,
        attempts_completed=attempts_completed,
        initial_status=initial_quality.status,
        final_status=last_status,
        depth_below_down_mm=last_depth_mm,
        axis_a_lowering_mm=last_axis_a_lowering_mm,
        position=position,
        total=total,
    )
    return samples


def _measure_contact_seek_attempt(
    owner: Any,
    attempt: ContactSeekAttempt,
    *,
    position: int,
    total: int,
    lower_to_depth: Callable[..., object] | None,
    adjust: Callable[..., object] | None,
) -> ContactSeekAttemptMeasurement | bool | None:
    if owner._route_point_stop_requested():
        return None
    owner._status(
        f"Route measurement: point {position}/{total} "
        f"pressing deeper {attempt.attempt_number}/{attempt.max_attempts}, "
        f"{attempt.depth_mm:.4f} mm below down."
    )
    prepare_task = owner._start_measurement_prepare_task(
        owner._initial_measurement_count()
    )
    if owner._route_point_stop_requested():
        return None
    if not owner._press_contact_seek_attempt(
        attempt,
        lower_to_depth=lower_to_depth,
        adjust=adjust,
    ):
        return False
    if owner._route_point_stop_requested():
        return None
    return owner._read_contact_seek_attempt_measurement(prepare_task)


def _set_exhausted_contact_seek_result(
    owner: Any,
    *,
    attempts_completed: int,
    initial_status: str,
    final_status: str,
    depth_below_down_mm: float,
    axis_a_lowering_mm: float,
    position: int,
    total: int,
) -> None:
    owner._status(
        f"Route measurement: point {position}/{total} contact seek did not "
        f"find stable contact within {owner._auto_contact_seek_max_total_mm:.3f} mm."
    )
    owner._set_contact_seek_result(
        found=False,
        status="not_found",
        attempts=attempts_completed,
        initial_status=initial_status,
        final_status=final_status,
        depth_below_down_mm=depth_below_down_mm,
        axis_a_lowering_mm=axis_a_lowering_mm,
    )


def press_contact_seek_attempt(
    owner: Any,
    attempt: ContactSeekAttempt,
    *,
    lower_to_depth: Callable[..., object] | None,
    adjust: Callable[..., object] | None,
) -> bool:
    if attempt.depth_mm > 0.0 and callable(lower_to_depth):
        lower_to_depth(attempt.depth_mm, owner._needle_feedrate)
        return True
    if callable(adjust):
        adjust(
            attempt.adjust_delta_mm(owner._auto_contact_seek_step_mm),
            owner._needle_feedrate,
        )
        return True
    return False


def read_contact_seek_attempt_measurement(
    owner: Any,
    prepare_task: Any | None,
) -> ContactSeekAttemptMeasurement | None:
    if not owner._sleep_contact_settle():
        return None
    axis_a_lowering_mm = owner._latest_axis_a_lowering()
    samples = owner._read_measurement_samples(
        owner._initial_measurement_count(),
        start_index=1,
        prepare_task=prepare_task,
    )
    if samples is None:
        return None
    return ContactSeekAttemptMeasurement(
        samples=samples,
        axis_a_lowering_mm=axis_a_lowering_mm,
    )


def resolve_contact_seek_attempt(
    owner: Any,
    *,
    samples: list[RouteMeasurementSample],
    depth_label: str,
    position: int,
    total: int,
) -> ContactSeekAttemptResolution:
    if owner._samples_are_short(samples):
        owner._status(
            f"Route measurement: point {position}/{total} "
            f"{depth_label}, short-circuit detected."
        )
        return ContactSeekAttemptResolution(
            samples=samples,
            final_status="short",
            found=True,
        )
    quality = owner._contact_quality_from_samples(samples)
    _status_contact_seek_attempt_quality(
        owner,
        quality=quality,
        depth_label=depth_label,
        position=position,
        total=total,
    )
    if quality.good is False:
        return ContactSeekAttemptResolution(
            samples=samples,
            final_status=quality.status,
        )
    return _resolve_completed_contact_seek_samples(
        owner,
        samples=samples,
        quality=quality,
        depth_label=depth_label,
        position=position,
        total=total,
    )


def _status_contact_seek_attempt_quality(
    owner: Any,
    *,
    quality: RouteContactQuality,
    depth_label: str,
    position: int,
    total: int,
) -> None:
    owner._status(
        f"Route measurement: point {position}/{total} "
        f"{depth_label}, {quality.status}, "
        f"median={format_route_ohm(quality.median_ohm)}, "
        f"MAD={format_route_ohm(quality.mad_sigma_ohm)}"
        f"{measurement_recording.contact_quality_failure_suffix(owner, quality)}."
    )


def _resolve_completed_contact_seek_samples(
    owner: Any,
    *,
    samples: list[RouteMeasurementSample],
    quality: RouteContactQuality,
    depth_label: str,
    position: int,
    total: int,
) -> ContactSeekAttemptResolution:
    completed_samples = owner._complete_measurement_samples(samples)
    if completed_samples is None:
        return ContactSeekAttemptResolution(
            samples=None,
            final_status=quality.status,
        )
    if owner._completed_measurement_is_acceptable(completed_samples):
        return ContactSeekAttemptResolution(
            samples=completed_samples,
            final_status=owner._contact_status_for_samples(completed_samples),
            found=True,
        )
    if owner._samples_have_bad_contact(completed_samples):
        return _bad_contact_seek_resolution(
            owner,
            samples=completed_samples,
            depth_label=depth_label,
            position=position,
            total=total,
        )
    if owner._samples_exceed_relative_rms_limit(completed_samples):
        return _unstable_contact_seek_resolution(
            owner,
            samples=completed_samples,
            depth_label=depth_label,
            position=position,
            total=total,
        )
    return ContactSeekAttemptResolution(
        samples=completed_samples,
        final_status=quality.status,
    )


def _bad_contact_seek_resolution(
    owner: Any,
    *,
    samples: list[RouteMeasurementSample],
    depth_label: str,
    position: int,
    total: int,
) -> ContactSeekAttemptResolution:
    full_quality = owner._contact_quality_from_samples(samples)
    owner._status(
        f"Route measurement: point {position}/{total} full "
        f"measurement at {depth_label} failed contact check "
        f"({full_quality.status}, "
        f"median={format_route_ohm(full_quality.median_ohm)}, "
        f"MAD={format_route_ohm(full_quality.mad_sigma_ohm)}"
        f"{measurement_recording.contact_quality_failure_suffix(owner, full_quality)}); "
        "trying deeper."
    )
    return ContactSeekAttemptResolution(
        samples=samples,
        final_status=full_quality.status,
    )


def _unstable_contact_seek_resolution(
    owner: Any,
    *,
    samples: list[RouteMeasurementSample],
    depth_label: str,
    position: int,
    total: int,
) -> ContactSeekAttemptResolution:
    relative_rms = owner._relative_rms_from_samples(samples)
    owner._status(
        f"Route measurement: point {position}/{total} full "
        f"measurement at {depth_label} relative RMS "
        f"{format_route_percent(relative_rms)} exceeds "
        f"{format_route_percent(owner._max_relative_rms or math.nan)}; "
        "trying deeper."
    )
    return ContactSeekAttemptResolution(samples=samples, final_status="unstable")


def set_contact_seek_result(
    owner: Any,
    *,
    found: bool,
    status: str,
    attempts: int,
    initial_status: str,
    final_status: str,
    depth_below_down_mm: float,
    axis_a_lowering_mm: float,
) -> None:
    owner._current_contact_seek_result = RouteContactSeekResult(
        found=bool(found),
        status=str(status),
        attempts=max(0, int(attempts)),
        initial_status=str(initial_status),
        final_status=str(final_status),
        depth_below_down_mm=float(depth_below_down_mm),
        axis_a_lowering_mm=float(axis_a_lowering_mm),
        step_mm=abs(float(owner._auto_contact_seek_step_mm)),
        max_depth_mm=float(owner._auto_contact_seek_max_total_mm),
    )


def latest_axis_a_lowering(owner: Any) -> float:
    getter = getattr(owner._stage_controller, "latest_axis_a_lowering", None)
    if not callable(getter):
        return math.nan
    try:
        value = float(getter())
    except (TypeError, ValueError):
        return math.nan
    return value if math.isfinite(value) else math.nan


def contact_status_for_samples(
    owner: Any,
    samples: list[RouteMeasurementSample] | tuple[RouteMeasurementSample, ...],
) -> str:
    if owner._samples_are_short(samples):
        return "short"
    return owner._contact_quality_from_samples(samples).status


def completed_measurement_is_acceptable(
    owner: Any,
    samples: list[RouteMeasurementSample],
) -> bool:
    if owner._samples_are_short(samples):
        return True
    if owner._samples_have_bad_contact(samples):
        return False
    return not owner._samples_exceed_relative_rms_limit(samples)


def samples_exceed_relative_rms_limit(
    owner: Any,
    samples: list[RouteMeasurementSample],
) -> bool:
    if owner._max_relative_rms is None:
        return False
    relative_rms = owner._relative_rms_from_samples(samples)
    return math.isfinite(relative_rms) and relative_rms > owner._max_relative_rms


def relative_rms_from_samples(samples: list[RouteMeasurementSample]) -> float:
    return resistance_stats_from_samples(samples).relative_rms


def record_status_for_samples(
    owner: Any,
    samples: list[RouteMeasurementSample],
    contact_quality: RouteContactQuality,
) -> str:
    if owner._samples_are_short(samples):
        return "short"
    if contact_quality.good is False:
        return "bad_contact"
    return "ok"


def resistance_stats_from_samples(
    samples: list[RouteMeasurementSample],
) -> ResistanceStats:
    resistances_ohm = tuple(sample.differential_resistance_ohm for sample in samples)
    finite_resistances = tuple(
        float(value) for value in resistances_ohm if math.isfinite(value)
    )
    if len(finite_resistances) != len(resistances_ohm) or not finite_resistances:
        return ResistanceStats(
            count=len(resistances_ohm),
            mean_ohm=math.inf,
            rms_ohm=math.nan,
            relative_rms=math.nan,
            complete_finite_batch=False,
        )
    return _finite_resistance_stats(finite_resistances)


def _finite_resistance_stats(finite_resistances: tuple[float, ...]) -> ResistanceStats:
    mean_resistance = sum(finite_resistances) / len(finite_resistances)
    variance = sum(
        (value - mean_resistance) ** 2 for value in finite_resistances
    ) / len(finite_resistances)
    rms_resistance = math.sqrt(variance)
    relative_rms = (
        rms_resistance / abs(mean_resistance) if mean_resistance else math.nan
    )
    return ResistanceStats(
        count=len(finite_resistances),
        mean_ohm=mean_resistance,
        rms_ohm=rms_resistance,
        relative_rms=relative_rms,
        complete_finite_batch=True,
    )


def read_measurement_samples(
    owner: Any,
    count: int,
    *,
    start_index: int,
    prepare_task: Any | None = None,
    after_measurement: Callable[[], object] | None = None,
) -> list[RouteMeasurementSample] | None:
    count = max(0, int(count))
    if count <= 0:
        return []
    if prepare_task is not None:
        prepare_task.wait()
    batch_reader = getattr(
        owner._lcr_controller,
        "read_route_measurement_batch_now",
        None,
    )
    if callable(batch_reader) and count > 1:
        return _read_measurement_batch_samples(
            owner,
            batch_reader,
            count,
            start_index=start_index,
            after_measurement=after_measurement,
        )
    return _read_individual_measurement_samples(
        owner,
        count,
        start_index=start_index,
    )


def _read_measurement_batch_samples(
    owner: Any,
    batch_reader: Callable[..., object],
    count: int,
    *,
    start_index: int,
    after_measurement: Callable[[], object] | None,
) -> list[RouteMeasurementSample] | None:
    if owner._route_point_stop_requested():
        return None
    if (
        after_measurement is not None
        and callable_accepts_keyword(batch_reader, "after_measurement")
    ):
        raw_batch = list(batch_reader(count, after_measurement=after_measurement))
    else:
        raw_batch = list(batch_reader(count))
    samples = [
        _measurement_sample_from_raw(raw, index)
        for index, raw in enumerate(raw_batch, start=start_index)
    ]
    if owner._route_point_stop_requested():
        return None
    return samples


def _read_individual_measurement_samples(
    owner: Any,
    count: int,
    *,
    start_index: int,
) -> list[RouteMeasurementSample] | None:
    samples: list[RouteMeasurementSample] = []
    for index in range(start_index, start_index + count):
        if owner._route_point_stop_requested():
            return None
        samples.append(owner._read_measurement_sample(index))
        if owner._route_point_stop_requested():
            return None
    return samples


def read_measurement_sample(owner: Any, sample_index: int) -> RouteMeasurementSample:
    reader = getattr(owner._lcr_controller, "read_route_measurement_now", None)
    if callable(reader):
        raw = reader()
    else:
        raw = owner._lcr_controller.read_primary_value_now()
    return _measurement_sample_from_raw(raw, sample_index)


def samples_are_short(
    samples: list[RouteMeasurementSample] | tuple[RouteMeasurementSample, ...],
) -> bool:
    return any(sample.compliance_hit for sample in samples)


def contact_quality_from_samples(
    owner: Any,
    samples: list[RouteMeasurementSample] | tuple[RouteMeasurementSample, ...],
) -> RouteContactQuality:
    return _contact_quality_from_samples(
        samples,
        contact_quality_limits=owner._contact_quality_limits,
    )


def samples_have_bad_contact(
    owner: Any,
    samples: list[RouteMeasurementSample] | tuple[RouteMeasurementSample, ...],
) -> bool:
    return owner._contact_quality_from_samples(samples).good is False


__all__ = [
    "ContactSeekAttemptMeasurement",
    "ContactSeekAttemptResolution",
    "ResistanceStats",
    "callable_accepts_keyword",
    "completed_measurement_is_acceptable",
    "complete_measurement_samples",
    "contact_quality_from_samples",
    "contact_status_for_samples",
    "latest_axis_a_lowering",
    "measure_samples",
    "press_contact_seek_attempt",
    "read_contact_seek_attempt_measurement",
    "read_measurement_sample",
    "read_measurement_samples",
    "record_status_for_samples",
    "relative_rms_from_samples",
    "resistance_stats_from_samples",
    "resolve_contact_seek_attempt",
    "run_contact_seek_attempts",
    "samples_are_short",
    "samples_exceed_relative_rms_limit",
    "samples_have_bad_contact",
    "seek_contact_from_current_position",
    "set_contact_seek_result",
]
