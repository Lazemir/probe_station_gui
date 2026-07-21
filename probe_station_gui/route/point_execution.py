"""One-point route preparation, acquisition, recording, and cleanup."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from probe_station_gui.route.contact_quality import (
    RouteContactQualityLimits,
    RouteMeasurementSample,
)
from probe_station_gui.route.measurement_records import (
    Point2D,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
    RoutePhotoRecord,
)
from probe_station_gui.route.measurement_payloads import focus_result_to_dict
from probe_station_gui.route.measurement_recording import structure_number_for_point
from probe_station_gui.route.point_acquisition import (
    RoutePointAcquisition,
    RoutePointCleanupError,
    RoutePointMeasurementEvent,
    RoutePointResult,
)


class PointAction(Protocol):
    def wait(self) -> None: ...


class PointMotionPort(Protocol):
    def raise_needles(self, feedrate: float | None) -> None: ...

    def move_to(self, target_xy: Point2D) -> None: ...

    def lower_needles(self, feedrate: float | None) -> None: ...

    def start_lift(self, feedrate: float | None) -> PointAction: ...

    def fallback_lift(self, feedrate: float | None) -> None: ...

    def press_to_depth(
        self,
        depth_mm: float,
        adjust_delta_mm: float,
        feedrate: float | None,
    ) -> bool: ...

    def latest_axis_a_lowering(self) -> float: ...


class PointAcquisitionPort(Protocol):
    def prepare(self, count: int, source_list_count: int) -> PointAction | None: ...

    def enable_output(self) -> None: ...

    def read(
        self,
        count: int,
        start_index: int,
        after_measurement: Callable[[], object] | None = None,
    ) -> list[RouteMeasurementSample] | None: ...


@dataclass(frozen=True)
class PointFocusOutcome:
    value: object | None
    safe_z: bool


@dataclass(frozen=True)
class PointPhotoSettings:
    autofocus_enabled: bool = False
    autofocus_range_mm: float = 0.0
    output_dir: str = ""
    csv_path: str = ""
    photo_only_mode: bool = False


class PointPhotoPort(Protocol):
    def autofocus(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        settings: PointPhotoSettings,
    ) -> PointFocusOutcome: ...

    def capture(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        focus_result: object | None,
        stage_xy: Point2D,
        settings: PointPhotoSettings,
    ) -> Path: ...


class PointControlPort(Protocol):
    def stopped(self) -> bool: ...

    def interrupted(self) -> bool: ...

    def settle(self, seconds: float, *, interruptible: bool) -> bool: ...

    def cancelled(self, exc: BaseException) -> bool: ...


class PointEventPort(Protocol):
    def status(self, message: str) -> None: ...

    def photo_saved(
        self,
        record: RoutePhotoRecord,
        position: int,
        total: int,
    ) -> None: ...

    def pre_contact(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> None: ...

    def save(self, record: RouteMeasurementRecord) -> None: ...

    def measurement_finished(self, event: RoutePointMeasurementEvent) -> None: ...


@dataclass(frozen=True)
class PointMode:
    measure: bool
    photo: bool
    autofocus: bool


@dataclass(frozen=True)
class PointMeasurementSettings:
    count: int
    initial_count: int
    max_relative_rms: float | None
    quality_limits: RouteContactQualityLimits
    nplc_label: str
    measurement_type: str
    confirm_each_point: bool = False


@dataclass(frozen=True)
class PointSeekSettings:
    enabled: bool
    step_mm: float
    max_total_mm: float


@dataclass(frozen=True)
class RoutePointRequest:
    point: RouteMeasurementPoint
    position: int
    total: int
    route_offset_xy: Point2D
    mode: PointMode
    measurement: PointMeasurementSettings
    seek: PointSeekSettings
    contact_settle_s: float
    photo_settle_s: float
    needle_feedrate: float | None = None
    photo_settings: PointPhotoSettings = field(default_factory=PointPhotoSettings)


@dataclass(frozen=True)
class RoutePointRuntimeSnapshot:
    """All mutable route settings frozen at one point boundary."""

    measurement_count: int
    initial_measurement_count: int
    max_relative_rms: float | None
    quality_limits: RouteContactQualityLimits
    nplc_label: str
    measurement_type: str
    confirm_each_point: bool
    seek_enabled: bool
    seek_step_mm: float
    seek_max_total_mm: float
    contact_settle_s: float
    needle_feedrate: float | None
    measure_enabled: bool
    photo_enabled: bool
    photo_focus_enabled: bool
    photo_settle_s: float
    photo_focus_range_mm: float
    photo_output_dir: str
    csv_path: str

    @property
    def photo_settings(self) -> PointPhotoSettings:
        return PointPhotoSettings(
            autofocus_enabled=self.photo_focus_enabled,
            autofocus_range_mm=self.photo_focus_range_mm,
            output_dir=self.photo_output_dir,
            csv_path=self.csv_path,
            photo_only_mode=self.photo_enabled and not self.measure_enabled,
        )

    def request(
        self,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        route_offset_xy: Point2D,
    ) -> RoutePointRequest:
        return RoutePointRequest(
            point=point,
            position=position,
            total=total,
            route_offset_xy=route_offset_xy,
            mode=PointMode(
                measure=self.measure_enabled,
                photo=self.photo_enabled,
                autofocus=self.photo_focus_enabled,
            ),
            measurement=PointMeasurementSettings(
                count=self.measurement_count,
                initial_count=self.initial_measurement_count,
                max_relative_rms=self.max_relative_rms,
                quality_limits=self.quality_limits,
                nplc_label=self.nplc_label,
                measurement_type=self.measurement_type,
                confirm_each_point=self.confirm_each_point,
            ),
            seek=PointSeekSettings(
                enabled=self.seek_enabled,
                step_mm=self.seek_step_mm,
                max_total_mm=self.seek_max_total_mm,
            ),
            contact_settle_s=self.contact_settle_s,
            photo_settle_s=self.photo_settle_s,
            needle_feedrate=self.needle_feedrate,
            photo_settings=self.photo_settings,
        )


@dataclass(frozen=True)
class _PointPreparation:
    focus: PointFocusOutcome | None = None
    prepare_action: PointAction | None = None
    photos_saved: int = 0
    result: RoutePointResult | None = None


class RoutePointExecution:
    """Execute one route point through a single immutable request."""

    def __init__(
        self,
        *,
        motion: PointMotionPort,
        acquisition: PointAcquisitionPort,
        photo: PointPhotoPort,
        control: PointControlPort,
        events: PointEventPort,
    ) -> None:
        self._motion = motion
        self._acquisition = acquisition
        self._photo = photo
        self._control = control
        self._events = events
        self._acquisition_flow = RoutePointAcquisition(
            motion=motion,
            acquisition=acquisition,
            control=control,
            events=events,
        )

    def execute(self, request: RoutePointRequest) -> RoutePointResult:
        preparation = self._execute_preparation(request)
        if preparation.result is not None:
            return preparation.result
        if not request.mode.measure:
            return RoutePointResult(photos_saved=preparation.photos_saved)
        return self._execute_contact(request, preparation)

    def _execute_preparation(self, request: RoutePointRequest) -> _PointPreparation:
        try:
            focus, prepare_action = self._prepare_point(request)
        except Exception as exc:
            if self._control.cancelled(exc):
                return _PointPreparation(result=RoutePointResult(interrupted=True))
            raise
        if self._control.interrupted():
            if prepare_action is not None:
                prepare_action.wait()
            return _PointPreparation(
                result=RoutePointResult(
                    interrupted=True,
                    pending_cleanup=bool(focus is not None and not focus.safe_z),
                )
            )
        if self._control.stopped():
            return _PointPreparation(result=self._stopped_result())
        photos_saved = self._capture_photo(request, focus)
        if self._control.interrupted():
            return _PointPreparation(
                result=RoutePointResult(
                    interrupted=True,
                    photos_saved=photos_saved,
                )
            )
        try:
            self._move_to_contact_after_photo(request)
        except Exception as exc:
            if self._control.cancelled(exc):
                return _PointPreparation(
                    result=RoutePointResult(
                        interrupted=True,
                        photos_saved=photos_saved,
                    )
                )
            raise
        return _PointPreparation(
            focus=focus,
            prepare_action=prepare_action,
            photos_saved=photos_saved,
        )

    def _execute_contact(
        self,
        request: RoutePointRequest,
        preparation: _PointPreparation,
    ) -> RoutePointResult:
        prepare_result = self._finish_prepare_action(request, preparation)
        if prepare_result is not None:
            return prepare_result
        self._events.pre_contact(request.point, request.position, request.total)
        control_result = self._pre_lower_control_result(preparation.photos_saved)
        if control_result is not None:
            return control_result
        self._acquisition.enable_output()
        control_result = self._pre_lower_control_result(preparation.photos_saved)
        if control_result is not None:
            return control_result
        try:
            self._motion.lower_needles(request.needle_feedrate)
        except Exception as exc:
            if self._control.cancelled(exc):
                return replace(
                    self._interrupted_after_lower(request.needle_feedrate),
                    photos_saved=preparation.photos_saved,
                )
            raise
        if self._control.interrupted():
            return replace(
                self._interrupted_after_lower(request.needle_feedrate),
                photos_saved=preparation.photos_saved,
            )
        if not self._control.settle(
            request.contact_settle_s,
            interruptible=True,
        ):
            return self._settle_interrupted(
                preparation.photos_saved,
                request.needle_feedrate,
            )
        return self._acquisition_flow.acquire(
            request,
            photos_saved=preparation.photos_saved,
        )

    def _finish_prepare_action(
        self,
        request: RoutePointRequest,
        preparation: _PointPreparation,
    ) -> RoutePointResult | None:
        if preparation.prepare_action is not None:
            try:
                preparation.prepare_action.wait()
            except BaseException as exc:
                if self._lift_needles(request.needle_feedrate):
                    raise
                raise RoutePointCleanupError(exc) from exc
        if not self._control.interrupted():
            return None
        cleaned = self._lift_needles(request.needle_feedrate)
        return RoutePointResult(
            interrupted=True,
            pending_cleanup=not cleaned,
            photos_saved=preparation.photos_saved,
        )

    def _pre_lower_control_result(
        self,
        photos_saved: int,
    ) -> RoutePointResult | None:
        if self._control.interrupted():
            return RoutePointResult(interrupted=True, photos_saved=photos_saved)
        if self._control.stopped():
            return replace(self._stopped_result(), photos_saved=photos_saved)
        return None

    def _settle_interrupted(
        self,
        photos_saved: int,
        feedrate: float | None,
    ) -> RoutePointResult:
        cleaned = self._lift_needles(feedrate)
        stopped = self._stopped_result()
        return RoutePointResult(
            stopped=not self._control.interrupted(),
            interrupted=self._control.interrupted(),
            stop_message=None if self._control.interrupted() else stopped.stop_message,
            pending_cleanup=not cleaned,
            photos_saved=photos_saved,
        )

    def _prepare_point(
        self,
        request: RoutePointRequest,
    ) -> tuple[PointFocusOutcome | None, PointAction | None]:
        if request.mode.photo or request.mode.autofocus:
            self._motion.raise_needles(request.needle_feedrate)
        target = (
            self._photo_xy(request)
            if request.mode.photo or request.mode.autofocus
            else self._contact_xy(request)
        )
        self._motion.move_to(target)
        prepare_action = self._prepare_acquisition(request)
        if not request.mode.autofocus:
            return None, prepare_action
        self._events.status(
            f"Route measurement: point {request.position}/{request.total} local autofocus."
        )
        return (
            self._photo.autofocus(
                request.point,
                request.position,
                request.total,
                request.photo_settings,
            ),
            prepare_action,
        )

    def _prepare_acquisition(self, request: RoutePointRequest) -> PointAction | None:
        if not request.mode.measure or self._control.interrupted():
            return None
        initial_count = min(
            max(1, int(request.measurement.count)),
            max(1, int(request.measurement.initial_count)),
        )
        return self._acquisition.prepare(
            initial_count,
            max(initial_count, int(request.measurement.count) - initial_count),
        )

    def _capture_photo(
        self,
        request: RoutePointRequest,
        focus: PointFocusOutcome | None,
    ) -> int:
        if not request.mode.photo:
            return 0
        if not self._control.settle(request.photo_settle_s, interruptible=False):
            return 0
        if self._control.interrupted():
            return 0
        focus_value = None if focus is None else focus.value
        photo_xy = self._photo_xy(request)
        path = self._photo.capture(
            request.point,
            request.position,
            request.total,
            focus_value,
            photo_xy,
            request.photo_settings,
        )
        record = RoutePhotoRecord(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            path=str(path),
            structure_number=structure_number_for_point(request.point),
            point_index=int(request.point.index),
            point_id=request.point.point_id,
            label=request.point.label,
            design_center=request.point.design_center,
            stage_xy=photo_xy,
            focus=focus_result_to_dict(focus_value),
        )
        self._events.photo_saved(record, request.position, request.total)
        self._events.status(
            f"Route measurement: point {request.position}/{request.total} "
            f"photo saved to {path}."
        )
        return 1

    def _move_to_contact_after_photo(self, request: RoutePointRequest) -> None:
        if not request.mode.measure or not (
            request.mode.photo or request.mode.autofocus
        ):
            return
        photo_xy = self._photo_xy(request)
        contact_xy = self._contact_xy(request)
        if self._same_xy(photo_xy, contact_xy):
            return
        self._events.status(
            f"Route measurement: point {request.position}/{request.total} "
            "moving to contact position."
        )
        self._motion.move_to(contact_xy)

    def _interrupted_after_lower(
        self,
        feedrate: float | None,
    ) -> RoutePointResult:
        cleaned = self._lift_needles(feedrate)
        return RoutePointResult(
            interrupted=True,
            pending_cleanup=not cleaned,
        )

    def _lift_needles(self, feedrate: float | None) -> bool:
        try:
            self._motion.start_lift(feedrate).wait()
            return True
        except Exception:
            try:
                self._motion.fallback_lift(feedrate)
                return True
            except Exception:
                return False

    @staticmethod
    def _stopped_result() -> RoutePointResult:
        return RoutePointResult(
            stopped=True,
            stop_message="Route measurement stopped by user.",
        )

    @staticmethod
    def _contact_xy(request: RoutePointRequest) -> Point2D:
        return RoutePointExecution._offset_xy(
            request.point.stage_xy,
            request.route_offset_xy,
        )

    @staticmethod
    def _photo_xy(request: RoutePointRequest) -> Point2D:
        point_xy = request.point.photo_stage_xy or request.point.stage_xy
        return RoutePointExecution._offset_xy(point_xy, request.route_offset_xy)

    @staticmethod
    def _offset_xy(point_xy: Point2D, offset_xy: Point2D) -> Point2D:
        return (
            float(point_xy[0]) + float(offset_xy[0]),
            float(point_xy[1]) + float(offset_xy[1]),
        )

    @staticmethod
    def _same_xy(first: Point2D, second: Point2D) -> bool:
        return (
            abs(float(first[0]) - float(second[0])) <= 1e-9
            and abs(float(first[1]) - float(second[1])) <= 1e-9
        )


__all__ = [
    "PointAcquisitionPort",
    "PointControlPort",
    "PointEventPort",
    "PointFocusOutcome",
    "PointMeasurementSettings",
    "PointMode",
    "PointMotionPort",
    "PointPhotoPort",
    "PointPhotoSettings",
    "PointSeekSettings",
    "RoutePointExecution",
    "RoutePointMeasurementEvent",
    "RoutePointCleanupError",
    "RoutePointRequest",
    "RoutePointResult",
    "RoutePointRuntimeSnapshot",
]
