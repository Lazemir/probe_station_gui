"""Typed acquisition, seek, recording, and lift for one route point."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import math
from typing import TYPE_CHECKING, Callable

from probe_station_gui.route.contact_quality import (
    RouteMeasurementSample,
    summarize_route_contact_quality,
)
from probe_station_gui.route.contact_measurement import resistance_stats_from_samples
from probe_station_gui.route.contact_seek import contact_seek_attempts
from probe_station_gui.route.measurement_records import (
    RouteContactHeightRecord,
    RouteContactSeekResult,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
)
from probe_station_gui.route.measurement_recording import (
    contact_height_record_for_point,
    structure_number_for_point,
)

if TYPE_CHECKING:
    from probe_station_gui.route.point_execution import (
        PointAction,
        PointAcquisitionPort,
        PointControlPort,
        PointEventPort,
        PointMotionPort,
        PointPhotoSettings,
        RoutePointRequest,
    )


@dataclass(frozen=True)
class RoutePointMeasurementEvent:
    point: RouteMeasurementPoint
    record: RouteMeasurementRecord
    contact_height: RouteContactHeightRecord
    position: int
    total: int
    saved: bool
    accepted: bool
    photo_settings: PointPhotoSettings


@dataclass(frozen=True)
class RoutePointResult:
    record: RouteMeasurementRecord | None = None
    contact_height: RouteContactHeightRecord | None = None
    seek: RouteContactSeekResult | None = None
    record_saved: bool = False
    result_emitted: bool = False
    photos_saved: int = 0
    measurements_saved: int = 0
    stopped: bool = False
    interrupted: bool = False
    quality_rejected: bool = False
    save_exhausted_bad_contact: bool = False
    pending_cleanup: bool = False
    stop_message: str | None = None


class RoutePointCleanupError(RuntimeError):
    """Preserve a point failure while reporting that lift cleanup is pending."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.pending_cleanup = True


class RoutePointAcquisition:
    def __init__(
        self,
        *,
        motion: PointMotionPort,
        acquisition: PointAcquisitionPort,
        control: PointControlPort,
        events: PointEventPort,
    ) -> None:
        self._motion = motion
        self._acquisition = acquisition
        self._control = control
        self._events = events

    def acquire(
        self,
        request: RoutePointRequest,
        *,
        photos_saved: int,
    ) -> RoutePointResult:
        samples, seek, cleaned = self._read_and_lift(request)
        if self._point_cancelled():
            return RoutePointResult(
                stopped=not self._control.interrupted(),
                interrupted=self._control.interrupted(),
                stop_message=(
                    None
                    if self._control.interrupted()
                    else "Route measurement stopped by user."
                ),
                pending_cleanup=not cleaned,
                photos_saved=photos_saved,
            )
        if samples is None:
            if self._control.interrupted():
                return RoutePointResult(
                    interrupted=True,
                    pending_cleanup=not cleaned,
                    photos_saved=photos_saved,
                )
            return RoutePointResult(
                stopped=True,
                stop_message="Route measurement stopped by user.",
                pending_cleanup=not cleaned,
                photos_saved=photos_saved,
            )
        if not cleaned:
            return RoutePointResult(
                photos_saved=photos_saved,
                pending_cleanup=True,
            )
        return self._record_measurement(
            request,
            samples,
            seek=seek,
            photos_saved=photos_saved,
            cleaned=cleaned,
        )

    def _read_and_lift(
        self,
        request: RoutePointRequest,
    ) -> tuple[
        list[RouteMeasurementSample] | None,
        RouteContactSeekResult | None,
        bool,
    ]:
        lift_actions: list[PointAction] = []

        def start_lift() -> None:
            if not lift_actions:
                lift_actions.append(
                    self._motion.start_lift(request.needle_feedrate)
                )

        try:
            samples, seek = self._acquire_samples(
                request,
                start_lift=start_lift,
            )
        except BaseException as exc:
            if self._complete_lift(lift_actions, request.needle_feedrate):
                raise
            raise RoutePointCleanupError(exc) from exc
        cleaned = self._complete_lift(lift_actions, request.needle_feedrate)
        return samples, seek, cleaned

    def _complete_lift(
        self,
        lift_actions: list[PointAction],
        feedrate: float | None,
    ) -> bool:
        try:
            if not lift_actions:
                lift_actions.append(self._motion.start_lift(feedrate))
            lift_actions[0].wait()
            return True
        except Exception:
            try:
                self._motion.fallback_lift(feedrate)
                return True
            except Exception:
                return False

    def _acquire_samples(
        self,
        request: RoutePointRequest,
        *,
        start_lift: Callable[[], object],
    ) -> tuple[list[RouteMeasurementSample] | None, RouteContactSeekResult | None]:
        initial_count = min(
            max(1, int(request.measurement.count)),
            max(1, int(request.measurement.initial_count)),
        )
        lift_after_read = (
            start_lift
            if not request.seek.enabled
            and initial_count >= int(request.measurement.count)
            else None
        )
        samples = self._acquisition.read(
            initial_count,
            1,
            after_measurement=lift_after_read,
        )
        if samples is None or self._point_cancelled():
            return None, None
        completed = self._complete_samples(
            request,
            samples,
            start_lift=start_lift if not request.seek.enabled else None,
        )
        if completed is None:
            return None, None
        if self._samples_acceptable(request, completed) or not request.seek.enabled:
            return completed, None
        return self._seek_contact(request, completed)

    def _complete_samples(
        self,
        request: RoutePointRequest,
        samples: list[RouteMeasurementSample],
        *,
        start_lift: Callable[[], object] | None = None,
    ) -> list[RouteMeasurementSample] | None:
        count = max(1, int(request.measurement.count))
        if len(samples) >= count or self._samples_short(samples):
            return samples
        if self._samples_bad_contact(request, samples):
            return samples
        remaining = count - len(samples)
        prepare = self._acquisition.prepare(remaining, remaining)
        if prepare is not None:
            prepare.wait()
        extra = self._acquisition.read(
            remaining,
            len(samples) + 1,
            after_measurement=start_lift,
        )
        if extra is None or self._point_cancelled():
            return None
        return samples + extra

    def _seek_contact(
        self,
        request: RoutePointRequest,
        initial_samples: list[RouteMeasurementSample],
    ) -> tuple[list[RouteMeasurementSample] | None, RouteContactSeekResult | None]:
        samples = initial_samples
        initial_status = self._seek_sample_status(request, samples)
        self._events.status(
            f"Route measurement: point {request.position}/{request.total} "
            f"contact check {initial_status}; seeking contact up to "
            f"{request.seek.max_total_mm:.3f} mm."
        )
        attempts = 0
        depth = math.nan
        lowering = math.nan
        for attempt in contact_seek_attempts(
            request.seek.step_mm,
            request.seek.max_total_mm,
        ):
            if self._control.stopped() or self._control.interrupted():
                return None, None
            self._events.status(
                f"Route measurement: point {request.position}/{request.total} "
                f"pressing deeper {attempt.attempt_number}/{attempt.max_attempts}, "
                f"{attempt.depth_mm:.4f} mm below down."
            )
            adjust_delta = attempt.adjust_delta_mm(request.seek.step_mm)
            if not self._motion.press_to_depth(
                attempt.depth_mm,
                adjust_delta,
                request.needle_feedrate,
            ):
                return initial_samples, None
            attempts += 1
            depth = float(attempt.depth_mm)
            lowering = self._motion.latest_axis_a_lowering()
            if not self._control.settle(
                request.contact_settle_s,
                interruptible=True,
            ):
                return None, None
            measured = self._read_seek_samples(request)
            if measured is None:
                return None, None
            samples = measured
            final_status = self._seek_sample_status(request, samples)
            self._events.status(
                f"Route measurement: point {request.position}/{request.total} "
                f"{attempt.depth_mm:.4f} mm below down, {final_status}."
            )
            if self._samples_acceptable(request, samples):
                return samples, self._seek_result(
                    request,
                    found=True,
                    attempts=attempts,
                    initial_status=initial_status,
                    final_status=final_status,
                    depth=depth,
                    lowering=lowering,
                )
        self._events.status(
            f"Route measurement: point {request.position}/{request.total} "
            "contact seek did not find stable contact within "
            f"{request.seek.max_total_mm:.3f} mm."
        )
        return samples, self._seek_result(
            request,
            found=False,
            attempts=attempts,
            initial_status=initial_status,
            final_status=self._seek_sample_status(request, samples),
            depth=depth,
            lowering=lowering,
        )

    def _read_seek_samples(
        self,
        request: RoutePointRequest,
    ) -> list[RouteMeasurementSample] | None:
        initial_count = min(
            max(1, int(request.measurement.count)),
            max(1, int(request.measurement.initial_count)),
        )
        prepare = self._acquisition.prepare(initial_count, initial_count)
        if prepare is not None:
            prepare.wait()
        samples = self._acquisition.read(initial_count, 1)
        if samples is None or self._point_cancelled():
            return None
        return self._complete_samples(request, samples)

    def _point_cancelled(self) -> bool:
        return self._control.stopped() or self._control.interrupted()

    @staticmethod
    def _seek_result(
        request: RoutePointRequest,
        *,
        found: bool,
        attempts: int,
        initial_status: str,
        final_status: str,
        depth: float,
        lowering: float,
    ) -> RouteContactSeekResult:
        return RouteContactSeekResult(
            found=found,
            status=("short" if found and final_status == "short" else "found")
            if found
            else "not_found",
            attempts=attempts,
            initial_status=initial_status,
            final_status=final_status,
            depth_below_down_mm=depth,
            axis_a_lowering_mm=lowering,
            step_mm=abs(float(request.seek.step_mm)),
            max_depth_mm=float(request.seek.max_total_mm),
        )

    def _record_measurement(
        self,
        request: RoutePointRequest,
        samples: list[RouteMeasurementSample],
        *,
        seek: RouteContactSeekResult | None,
        photos_saved: int,
        cleaned: bool,
    ) -> RoutePointResult:
        record = self._measurement_record(request, samples)
        exhausted = bool(seek is not None and not seek.found and record.status == "bad_contact")
        rejected = self._quality_rejected(request, record) and not exhausted
        if rejected and record.status == "ok":
            record = replace(record, status="unstable")
        saved = not rejected
        if saved:
            self._events.save(record)
        contact_height = self._contact_height(request, record, seek=seek)
        self._events.measurement_finished(
            RoutePointMeasurementEvent(
                point=request.point,
                record=record,
                contact_height=contact_height,
                position=request.position,
                total=request.total,
                saved=saved,
                accepted=not rejected,
                photo_settings=request.photo_settings,
            )
        )
        return RoutePointResult(
            record=record,
            contact_height=contact_height,
            seek=seek,
            record_saved=saved,
            result_emitted=True,
            photos_saved=photos_saved,
            measurements_saved=int(saved),
            quality_rejected=rejected,
            save_exhausted_bad_contact=exhausted,
            pending_cleanup=not cleaned,
        )

    @staticmethod
    def _measurement_record(
        request: RoutePointRequest,
        samples: list[RouteMeasurementSample],
    ) -> RouteMeasurementRecord:
        stats = resistance_stats_from_samples(samples)
        quality = None
        status = "overload"
        if stats.complete_finite_batch:
            quality = summarize_route_contact_quality(
                samples,
                contact_quality_limits=request.measurement.quality_limits,
            )
            status = RoutePointAcquisition._measurement_status(samples, quality.good)
        return RouteMeasurementRecord(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            structure_number=structure_number_for_point(request.point),
            nplc=request.measurement.nplc_label,
            measurement_type=request.measurement.measurement_type,
            n_measurements=stats.count,
            resistance_ohm=stats.mean_ohm,
            resistance_rms_ohm=stats.rms_ohm,
            relative_rms=stats.relative_rms,
            status=status,
            contact_quality=quality,
            raw_samples=tuple(samples),
        )

    @staticmethod
    def _measurement_status(
        samples: list[RouteMeasurementSample],
        contact_good: bool | None,
    ) -> str:
        if any(sample.compliance_hit for sample in samples):
            return "short"
        return "bad_contact" if contact_good is False else "ok"

    @staticmethod
    def _quality_rejected(
        request: RoutePointRequest,
        record: RouteMeasurementRecord,
    ) -> bool:
        if not request.measurement.confirm_each_point or record.status == "short":
            return False
        if record.contact_quality is not None and record.contact_quality.good is False:
            return True
        limit = request.measurement.max_relative_rms
        return bool(
            limit is not None
            and record.status == "ok"
            and math.isfinite(record.relative_rms)
            and record.relative_rms > limit
        )

    @staticmethod
    def _samples_short(samples: list[RouteMeasurementSample]) -> bool:
        return any(sample.compliance_hit for sample in samples)

    @staticmethod
    def _samples_bad_contact(
        request: RoutePointRequest,
        samples: list[RouteMeasurementSample],
    ) -> bool:
        quality = summarize_route_contact_quality(
            samples,
            contact_quality_limits=request.measurement.quality_limits,
        )
        return quality.good is False

    @classmethod
    def _samples_acceptable(
        cls,
        request: RoutePointRequest,
        samples: list[RouteMeasurementSample],
    ) -> bool:
        if cls._samples_short(samples):
            return True
        if cls._samples_bad_contact(request, samples):
            return False
        limit = request.measurement.max_relative_rms
        stats = resistance_stats_from_samples(samples)
        return not bool(
            limit is not None
            and math.isfinite(stats.relative_rms)
            and stats.relative_rms > limit
        )

    @classmethod
    def _seek_sample_status(
        cls,
        request: RoutePointRequest,
        samples: list[RouteMeasurementSample],
    ) -> str:
        if cls._samples_short(samples):
            return "short"
        quality = summarize_route_contact_quality(
            samples,
            contact_quality_limits=request.measurement.quality_limits,
        )
        if quality.good is False:
            return quality.status
        limit = request.measurement.max_relative_rms
        relative_rms = resistance_stats_from_samples(samples).relative_rms
        if limit is not None and math.isfinite(relative_rms) and relative_rms > limit:
            return "unstable"
        return quality.status

    def _contact_height(
        self,
        request: RoutePointRequest,
        record: RouteMeasurementRecord,
        *,
        seek: RouteContactSeekResult | None,
    ) -> RouteContactHeightRecord:
        return contact_height_record_for_point(
            point=request.point,
            record=record,
            seek=seek,
            axis_a_lowering_mm=self._motion.latest_axis_a_lowering(),
        )


__all__ = [
    "RoutePointAcquisition",
    "RoutePointCleanupError",
    "RoutePointMeasurementEvent",
    "RoutePointResult",
]
