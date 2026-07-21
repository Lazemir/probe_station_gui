"""Contact sample readout and contact-seek helpers for route measurements."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
import threading
from typing import Callable

from probe_station_gui.instruments.meters.lcr_helpers import callable_accepts_keyword
from probe_station_gui.route import measurement_recording
from probe_station_gui.route.contact_quality import (
    RouteContactQuality,
    RouteContactQualityLimits,
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


@dataclass(frozen=True)
class ContactMeasurementConfig:
    measurement_count: int
    initial_measurement_count: int
    max_relative_rms: float | None
    quality_limits: RouteContactQualityLimits
    seek_enabled: bool
    seek_step_mm: float
    seek_max_total_mm: float
    contact_settle_s: float
    needle_feedrate: float | None


@dataclass
class ContactMeasurementState:
    seek_result: RouteContactSeekResult | None = None
    seek_active: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True)
class ContactMeasurementContext:
    config: ContactMeasurementConfig
    stage_controller: object
    lcr_controller: object
    state: ContactMeasurementState
    prepare: Callable[[int], object | None]
    stop_requested: Callable[[], bool]
    settle: Callable[[], bool]
    status: Callable[[str], None]

    def initial_count(self) -> int:
        return min(
            max(1, int(self.config.measurement_count)),
            max(1, int(self.config.initial_measurement_count)),
        )


def measure_samples(
    context: ContactMeasurementContext,
    *,
    position: int,
    total: int,
    prepare_task: object | None = None,
    after_measurement: Callable[[], object] | None = None,
) -> list[RouteMeasurementSample] | None:
    context.state.seek_result = None
    initial_count = context.initial_count()
    initial_after_measurement = (
        after_measurement if context.config.measurement_count <= initial_count else None
    )
    samples = read_measurement_samples(
        context,
        initial_count,
        start_index=1,
        prepare_task=prepare_task,
        after_measurement=initial_after_measurement,
    )
    if samples is None:
        return None
    if not context.config.seek_enabled or samples_are_short(samples):
        return complete_measurement_samples(
            context,
            samples,
            after_measurement=after_measurement,
        )
    if samples_have_bad_contact(context, samples):
        return seek_contact_from_current_position(
            context,
            initial_samples=samples,
            position=position,
            total=total,
            after_measurement=after_measurement,
        )
    completed_samples = complete_measurement_samples(
        context,
        samples,
        after_measurement=after_measurement,
    )
    if (
        completed_samples is None
        or completed_measurement_is_acceptable(context, completed_samples)
    ):
        return completed_samples
    return seek_contact_from_current_position(
        context,
        initial_samples=completed_samples,
        position=position,
        total=total,
        skip_current_depth=True,
        after_measurement=after_measurement,
    )


def complete_measurement_samples(
    context: ContactMeasurementContext,
    samples: list[RouteMeasurementSample],
    *,
    after_measurement: Callable[[], object] | None = None,
) -> list[RouteMeasurementSample] | None:
    remaining_count = context.config.measurement_count - len(samples)
    if (
        remaining_count <= 0
        or samples_are_short(samples)
        or samples_have_bad_contact(context, samples)
    ):
        return samples
    extra_samples = read_measurement_samples(
        context,
        remaining_count,
        start_index=len(samples) + 1,
        prepare_task=context.prepare(remaining_count),
        after_measurement=after_measurement,
    )
    if extra_samples is None:
        return None
    return samples + extra_samples


def seek_contact_from_current_position(
    context: ContactMeasurementContext,
    *,
    initial_samples: list[RouteMeasurementSample],
    position: int,
    total: int,
    skip_current_depth: bool = False,
    after_measurement: Callable[[], object] | None = None,
) -> list[RouteMeasurementSample] | None:
    _ = after_measurement
    if context.config.seek_max_total_mm <= 0.0:
        return initial_samples
    lower_to_depth = getattr(
        context.stage_controller,
        "run_external_needles_lower_to_depth_below_down",
        None,
    )
    adjust = getattr(context.stage_controller, "run_external_needles_adjust", None)
    if not callable(lower_to_depth) and not callable(adjust):
        return initial_samples
    initial_quality = contact_quality_from_samples(context, initial_samples)
    _status_contact_seek_start(
        context,
        initial_quality=initial_quality,
        position=position,
        total=total,
        skip_current_depth=skip_current_depth,
    )
    context.state.seek_active.set()
    try:
        return run_contact_seek_attempts(
            context,
            initial_samples=initial_samples,
            initial_quality=initial_quality,
            position=position,
            total=total,
            lower_to_depth=lower_to_depth,
            adjust=adjust,
        )
    finally:
        context.state.seek_active.clear()


def _status_contact_seek_start(
    context: ContactMeasurementContext,
    *,
    initial_quality: RouteContactQuality,
    position: int,
    total: int,
    skip_current_depth: bool,
) -> None:
    if skip_current_depth:
        context.status(
            f"Route measurement: point {position}/{total} full measurement "
            f"rejected after contact check {initial_quality.status}; "
            "trying deeper contact up to "
            f"{context.config.seek_max_total_mm:.3f} mm."
        )
        return
    context.status(
        f"Route measurement: point {position}/{total} contact check "
        f"{initial_quality.status}, "
        f"median={format_route_ohm(initial_quality.median_ohm)}, "
        f"MAD={format_route_ohm(initial_quality.mad_sigma_ohm)}"
        f"{measurement_recording.contact_quality_failure_suffix(context, initial_quality)}; "
        "seeking contact up to "
        f"{context.config.seek_max_total_mm:.3f} mm."
    )


def run_contact_seek_attempts(
    context: ContactMeasurementContext,
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
        context.config.seek_step_mm,
        context.config.seek_max_total_mm,
    ):
        attempt_measurement = _measure_contact_seek_attempt(
            context,
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
        resolution = resolve_contact_seek_attempt(
            context,
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
            set_contact_seek_result(
                context,
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
        context,
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
    context: ContactMeasurementContext,
    attempt: ContactSeekAttempt,
    *,
    position: int,
    total: int,
    lower_to_depth: Callable[..., object] | None,
    adjust: Callable[..., object] | None,
) -> ContactSeekAttemptMeasurement | bool | None:
    if context.stop_requested():
        return None
    context.status(
        f"Route measurement: point {position}/{total} "
        f"pressing deeper {attempt.attempt_number}/{attempt.max_attempts}, "
        f"{attempt.depth_mm:.4f} mm below down."
    )
    prepare_task = context.prepare(
        context.initial_count()
    )
    if context.stop_requested():
        return None
    if not press_contact_seek_attempt(
        context,
        attempt,
        lower_to_depth=lower_to_depth,
        adjust=adjust,
    ):
        return False
    if context.stop_requested():
        return None
    return read_contact_seek_attempt_measurement(context, prepare_task)


def _set_exhausted_contact_seek_result(
    context: ContactMeasurementContext,
    *,
    attempts_completed: int,
    initial_status: str,
    final_status: str,
    depth_below_down_mm: float,
    axis_a_lowering_mm: float,
    position: int,
    total: int,
) -> None:
    context.status(
        f"Route measurement: point {position}/{total} contact seek did not "
        f"find stable contact within {context.config.seek_max_total_mm:.3f} mm."
    )
    set_contact_seek_result(
        context,
        found=False,
        status="not_found",
        attempts=attempts_completed,
        initial_status=initial_status,
        final_status=final_status,
        depth_below_down_mm=depth_below_down_mm,
        axis_a_lowering_mm=axis_a_lowering_mm,
    )


def press_contact_seek_attempt(
    context: ContactMeasurementContext,
    attempt: ContactSeekAttempt,
    *,
    lower_to_depth: Callable[..., object] | None,
    adjust: Callable[..., object] | None,
) -> bool:
    if attempt.depth_mm > 0.0 and callable(lower_to_depth):
        lower_to_depth(attempt.depth_mm, context.config.needle_feedrate)
        return True
    if callable(adjust):
        adjust(
            attempt.adjust_delta_mm(context.config.seek_step_mm),
            context.config.needle_feedrate,
        )
        return True
    return False


def read_contact_seek_attempt_measurement(
    context: ContactMeasurementContext,
    prepare_task: object | None,
) -> ContactSeekAttemptMeasurement | None:
    if not context.settle():
        return None
    axis_a_lowering_mm = latest_axis_a_lowering(context)
    samples = read_measurement_samples(
        context,
        context.initial_count(),
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
    context: ContactMeasurementContext,
    *,
    samples: list[RouteMeasurementSample],
    depth_label: str,
    position: int,
    total: int,
) -> ContactSeekAttemptResolution:
    if samples_are_short(samples):
        context.status(
            f"Route measurement: point {position}/{total} "
            f"{depth_label}, short-circuit detected."
        )
        return ContactSeekAttemptResolution(
            samples=samples,
            final_status="short",
            found=True,
        )
    quality = contact_quality_from_samples(context, samples)
    _status_contact_seek_attempt_quality(
        context,
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
        context,
        samples=samples,
        quality=quality,
        depth_label=depth_label,
        position=position,
        total=total,
    )


def _status_contact_seek_attempt_quality(
    context: ContactMeasurementContext,
    *,
    quality: RouteContactQuality,
    depth_label: str,
    position: int,
    total: int,
) -> None:
    context.status(
        f"Route measurement: point {position}/{total} "
        f"{depth_label}, {quality.status}, "
        f"median={format_route_ohm(quality.median_ohm)}, "
        f"MAD={format_route_ohm(quality.mad_sigma_ohm)}"
        f"{measurement_recording.contact_quality_failure_suffix(context, quality)}."
    )


def _resolve_completed_contact_seek_samples(
    context: ContactMeasurementContext,
    *,
    samples: list[RouteMeasurementSample],
    quality: RouteContactQuality,
    depth_label: str,
    position: int,
    total: int,
) -> ContactSeekAttemptResolution:
    completed_samples = complete_measurement_samples(context, samples)
    if completed_samples is None:
        return ContactSeekAttemptResolution(
            samples=None,
            final_status=quality.status,
        )
    if completed_measurement_is_acceptable(context, completed_samples):
        return ContactSeekAttemptResolution(
            samples=completed_samples,
            final_status=contact_status_for_samples(context, completed_samples),
            found=True,
        )
    if samples_have_bad_contact(context, completed_samples):
        return _bad_contact_seek_resolution(
            context,
            samples=completed_samples,
            depth_label=depth_label,
            position=position,
            total=total,
        )
    if samples_exceed_relative_rms_limit(context, completed_samples):
        return _unstable_contact_seek_resolution(
            context,
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
    context: ContactMeasurementContext,
    *,
    samples: list[RouteMeasurementSample],
    depth_label: str,
    position: int,
    total: int,
) -> ContactSeekAttemptResolution:
    full_quality = contact_quality_from_samples(context, samples)
    context.status(
        f"Route measurement: point {position}/{total} full "
        f"measurement at {depth_label} failed contact check "
        f"({full_quality.status}, "
        f"median={format_route_ohm(full_quality.median_ohm)}, "
        f"MAD={format_route_ohm(full_quality.mad_sigma_ohm)}"
        f"{measurement_recording.contact_quality_failure_suffix(context, full_quality)}); "
        "trying deeper."
    )
    return ContactSeekAttemptResolution(
        samples=samples,
        final_status=full_quality.status,
    )


def _unstable_contact_seek_resolution(
    context: ContactMeasurementContext,
    *,
    samples: list[RouteMeasurementSample],
    depth_label: str,
    position: int,
    total: int,
) -> ContactSeekAttemptResolution:
    relative_rms = relative_rms_from_samples(samples)
    context.status(
        f"Route measurement: point {position}/{total} full "
        f"measurement at {depth_label} relative RMS "
        f"{format_route_percent(relative_rms)} exceeds "
        f"{format_route_percent(context.config.max_relative_rms or math.nan)}; "
        "trying deeper."
    )
    return ContactSeekAttemptResolution(samples=samples, final_status="unstable")


def set_contact_seek_result(
    context: ContactMeasurementContext,
    *,
    found: bool,
    status: str,
    attempts: int,
    initial_status: str,
    final_status: str,
    depth_below_down_mm: float,
    axis_a_lowering_mm: float,
) -> None:
    context.state.seek_result = RouteContactSeekResult(
        found=bool(found),
        status=str(status),
        attempts=max(0, int(attempts)),
        initial_status=str(initial_status),
        final_status=str(final_status),
        depth_below_down_mm=float(depth_below_down_mm),
        axis_a_lowering_mm=float(axis_a_lowering_mm),
        step_mm=abs(float(context.config.seek_step_mm)),
        max_depth_mm=float(context.config.seek_max_total_mm),
    )


def latest_axis_a_lowering(context: ContactMeasurementContext) -> float:
    getter = getattr(context.stage_controller, "latest_axis_a_lowering", None)
    if not callable(getter):
        return math.nan
    try:
        value = float(getter())
    except (TypeError, ValueError):
        return math.nan
    return value if math.isfinite(value) else math.nan


def contact_status_for_samples(
    context: ContactMeasurementContext,
    samples: list[RouteMeasurementSample] | tuple[RouteMeasurementSample, ...],
) -> str:
    if samples_are_short(samples):
        return "short"
    return contact_quality_from_samples(context, samples).status


def completed_measurement_is_acceptable(
    context: ContactMeasurementContext,
    samples: list[RouteMeasurementSample],
) -> bool:
    if samples_are_short(samples):
        return True
    if samples_have_bad_contact(context, samples):
        return False
    return not samples_exceed_relative_rms_limit(context, samples)


def samples_exceed_relative_rms_limit(
    context: ContactMeasurementContext,
    samples: list[RouteMeasurementSample],
) -> bool:
    if context.config.max_relative_rms is None:
        return False
    relative_rms = relative_rms_from_samples(samples)
    return math.isfinite(relative_rms) and relative_rms > context.config.max_relative_rms


def relative_rms_from_samples(samples: list[RouteMeasurementSample]) -> float:
    return resistance_stats_from_samples(samples).relative_rms


def record_status_for_samples(
    context: ContactMeasurementContext,
    samples: list[RouteMeasurementSample],
    contact_quality: RouteContactQuality,
) -> str:
    if samples_are_short(samples):
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
    context: ContactMeasurementContext,
    count: int,
    *,
    start_index: int,
    prepare_task: object | None = None,
    after_measurement: Callable[[], object] | None = None,
) -> list[RouteMeasurementSample] | None:
    count = max(0, int(count))
    if count <= 0:
        return []
    if prepare_task is not None:
        prepare_task.wait()
    batch_reader = getattr(
        context.lcr_controller,
        "read_route_measurement_batch_now",
        None,
    )
    if callable(batch_reader) and count > 1:
        return _read_measurement_batch_samples(
            context,
            batch_reader,
            count,
            start_index=start_index,
            after_measurement=after_measurement,
        )
    return _read_individual_measurement_samples(
        context,
        count,
        start_index=start_index,
    )


def _read_measurement_batch_samples(
    context: ContactMeasurementContext,
    batch_reader: Callable[..., object],
    count: int,
    *,
    start_index: int,
    after_measurement: Callable[[], object] | None,
) -> list[RouteMeasurementSample] | None:
    if context.stop_requested():
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
    if context.stop_requested():
        return None
    return samples


def _read_individual_measurement_samples(
    context: ContactMeasurementContext,
    count: int,
    *,
    start_index: int,
) -> list[RouteMeasurementSample] | None:
    samples: list[RouteMeasurementSample] = []
    for index in range(start_index, start_index + count):
        if context.stop_requested():
            return None
        samples.append(read_measurement_sample(context, index))
        if context.stop_requested():
            return None
    return samples


def read_measurement_sample(context: ContactMeasurementContext, sample_index: int) -> RouteMeasurementSample:
    reader = getattr(context.lcr_controller, "read_route_measurement_now", None)
    if callable(reader):
        raw = reader()
    else:
        raw = context.lcr_controller.read_primary_value_now()
    return _measurement_sample_from_raw(raw, sample_index)


def samples_are_short(
    samples: list[RouteMeasurementSample] | tuple[RouteMeasurementSample, ...],
) -> bool:
    return any(sample.compliance_hit for sample in samples)


def contact_quality_from_samples(
    context: ContactMeasurementContext,
    samples: list[RouteMeasurementSample] | tuple[RouteMeasurementSample, ...],
) -> RouteContactQuality:
    return _contact_quality_from_samples(
        samples,
        contact_quality_limits=context.config.quality_limits,
    )


def samples_have_bad_contact(
    context: ContactMeasurementContext,
    samples: list[RouteMeasurementSample] | tuple[RouteMeasurementSample, ...],
) -> bool:
    return contact_quality_from_samples(context, samples).good is False


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
