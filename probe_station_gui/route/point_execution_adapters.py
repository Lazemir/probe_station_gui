"""Concrete adapters for one-point route execution."""

from __future__ import annotations

import inspect
import logging
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from probe_station_gui.route import contact_lifecycle_adapter, contact_measurement
from probe_station_gui.route import measurement_recording
from probe_station_gui.route.contact_lifecycle import (
    RouteContactFlow,
    RouteContactMessage,
)
from probe_station_gui.route.contact_measurement import (
    ContactMeasurementConfig,
    ContactMeasurementContext,
    ContactMeasurementState,
)
from probe_station_gui.route.contact_quality import (
    RouteMeasurementSample,
    route_measurement_sample_from_raw,
)
from probe_station_gui.route.measurement_records import (
    Point2D,
    RouteContactPlacementResult,
    RouteContactHeightRecord,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
    RoutePhotoRecord,
)
from probe_station_gui.route.measurement_recording import (
    RouteRecordingConfig,
    RouteRecordingContext,
)
from probe_station_gui.route.point_execution import (
    PointAction,
    PointFocusOutcome,
    PointPhotoSettings,
    RoutePointExecution,
    RoutePointMeasurementEvent,
    RoutePointRuntimeSnapshot,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RouteMeasurementEvents:
    status: Callable[[str], None] | None = None
    progress: Callable[[int, int, int], None] | None = None
    record: Callable[[RouteMeasurementRecord, int, int], None] | None = None
    photo: Callable[[RouteMeasurementPoint, int, int, object | None], str | Path] | None = None
    photo_focus: Callable[[RouteMeasurementPoint, int, int], object | None] | None = None
    photo_record: Callable[[RoutePhotoRecord, int, int], None] | None = None
    contact_height: Callable[[RouteContactHeightRecord, int, int], None] | None = None
    contact_photo: Callable[[RouteMeasurementPoint, RouteMeasurementRecord, int, int, bool], None] | None = None
    pre_contact_photo: Callable[[RouteMeasurementPoint, int, int], None] | None = None
    result: Callable[[RouteMeasurementRecord, int, int, bool], None] | None = None
    waiting: Callable[[bool], None] | None = None
    point_photo: Callable[
        [RouteMeasurementPoint, int, int, object | None, PointPhotoSettings],
        str | Path,
    ] | None = None
    point_photo_focus: Callable[
        [RouteMeasurementPoint, int, int, PointPhotoSettings], object | None
    ] | None = None
    point_contact_height: Callable[
        [RouteContactHeightRecord, int, int, PointPhotoSettings], None
    ] | None = None


@dataclass(frozen=True)
class PointExecutionAdapters:
    """Concrete one-point ports assembled without exposing the runner."""

    motion: StagePointMotionAdapter
    acquisition: MeterPointAcquisitionAdapter
    control: EventPointControlAdapter
    events: CallbackPointEventAdapter
    execution: RoutePointExecution

    @classmethod
    def create(
        cls,
        *,
        stage_controller: object,
        lcr_controller: object,
        callbacks: RouteMeasurementEvents,
        append_csv: Callable[[RouteMeasurementRecord], None],
        stopped: Callable[[], bool],
        interrupted: Callable[[], bool],
    ) -> PointExecutionAdapters:
        control = EventPointControlAdapter(
            stopped=stopped,
            interrupted=interrupted,
        )
        motion = StagePointMotionAdapter(stage_controller)
        acquisition = MeterPointAcquisitionAdapter(
            lcr_controller,
            cancelled=lambda: control.stopped() or control.interrupted(),
        )
        events = CallbackPointEventAdapter(
            callbacks=callbacks,
            append_csv=append_csv,
        )
        execution = RoutePointExecution(
            motion=motion,
            acquisition=acquisition,
            photo=CallbackPointPhotoAdapter(callbacks, control.cancelled),
            control=control,
            events=events,
        )
        return cls(motion, acquisition, control, events, execution)


@dataclass(frozen=True)
class LegacyContactBindings:
    """Typed bridge for API-owned contact operations outside point execution."""

    stage_controller: object
    lcr_controller: object
    state: ContactMeasurementState
    snapshot: Callable[[], RoutePointRuntimeSnapshot]
    adapters: PointExecutionAdapters
    begin_stage: Callable[[], None]
    finish_stage: Callable[[], None]
    lower_needles: Callable[[], None]
    contact_xy: Callable[[RouteMeasurementPoint], Point2D]
    photo_xy: Callable[[RouteMeasurementPoint], Point2D]
    settle_photo: Callable[[], bool]
    stop_requested: Callable[[], bool]
    clear_interrupt: Callable[[], None]
    set_seek_enabled: Callable[[bool], None]
    status: Callable[[str], None]

    def contact_context(self) -> ContactMeasurementContext:
        settings = self.snapshot()
        return ContactMeasurementContext(
            config=ContactMeasurementConfig(
                measurement_count=settings.measurement_count,
                initial_measurement_count=settings.initial_measurement_count,
                max_relative_rms=settings.max_relative_rms,
                quality_limits=settings.quality_limits,
                seek_enabled=settings.seek_enabled,
                seek_step_mm=settings.seek_step_mm,
                seek_max_total_mm=settings.seek_max_total_mm,
                contact_settle_s=settings.contact_settle_s,
                needle_feedrate=settings.needle_feedrate,
            ),
            stage_controller=self.stage_controller,
            lcr_controller=self.lcr_controller,
            state=self.state,
            prepare=lambda count: self.adapters.acquisition.prepare(
                count,
                self._source_list_count(settings, count),
            ),
            stop_requested=self.stop_requested,
            settle=lambda: self.adapters.control.settle(
                settings.contact_settle_s,
                interruptible=True,
            ),
            status=self.status,
        )

    def recording_context(self) -> RouteRecordingContext:
        settings = self.snapshot()
        return RouteRecordingContext(
            config=RouteRecordingConfig(
                nplc_label=settings.nplc_label,
                measurement_type=settings.measurement_type,
                confirm_each_point=settings.confirm_each_point,
                max_relative_rms=settings.max_relative_rms,
                quality_limits=settings.quality_limits,
            ),
            current_seek=lambda: self.state.seek_result,
            save=self.adapters.events.save,
            contact_photo=self.adapters.events.emit_contact_photo,
            result=self.adapters.events.emit_result,
        )

    def build_flow(
        self,
        *,
        post_success_contact: Callable[
            [RouteContactPlacementResult], Callable[[], None] | None
        ]
        | None = None,
        post_success_contact_eligible: Callable[[], bool] | None = None,
    ) -> RouteContactFlow:
        control = contact_lifecycle_adapter.ContactControlBindings(
            clear=self.clear_interrupt,
            requested=self.adapters.control.interrupted,
            status=self.status,
            pre_contact_photo=self.adapters.events.pre_contact,
            contact_photo=self.adapters.events.emit_contact_photo,
        )
        return RouteContactFlow(
            stage=contact_lifecycle_adapter.ContactStageBindings(
                controller=self.stage_controller,
                needle_feedrate=lambda: self.snapshot().needle_feedrate,
                begin=self.begin_stage,
                finish=self.finish_stage,
                wait_for_background_tasks=lambda: None,
                lower_needles=self.lower_needles,
                contact_xy=self.contact_xy,
                photo_xy=self.photo_xy,
                settle_contact=self._settle_contact,
                settle_photo=self.settle_photo,
            ),
            meter=contact_lifecycle_adapter.ContactMeterBindings(
                initial_count=lambda: self.snapshot().initial_measurement_count,
                prepare=lambda count: self.contact_context().prepare(count),
                measure=self._measure,
                close_output=self.adapters.acquisition.close_output,
            ),
            quality=contact_lifecycle_adapter.ContactQualityBindings(
                reset_seek=self._reset_seek,
                current_seek=lambda: self.state.seek_result,
                auto_seek_enabled=lambda: self.snapshot().seek_enabled,
                set_auto_seek_enabled=self.set_seek_enabled,
                record=self._record,
                placement_success=measurement_recording.contact_placement_record_is_success,
                placement_message=self._placement_message,
            ),
            interrupt=control,
            events=control,
            post_success_contact=post_success_contact,
            post_success_contact_eligible=post_success_contact_eligible,
        )

    def _measure(
        self,
        position: int,
        total: int,
        prepare_task: object | None,
    ) -> list[RouteMeasurementSample] | None:
        return contact_measurement.measure_samples(
            self.contact_context(),
            position=position,
            total=total,
            prepare_task=prepare_task,
        )

    def _record(
        self,
        point: RouteMeasurementPoint,
        samples: list[RouteMeasurementSample],
    ) -> RouteMeasurementRecord:
        return measurement_recording.record_for_point(
            self.recording_context(),
            point=point,
            samples=samples,
        )

    def _placement_message(
        self,
        message: RouteContactMessage,
    ) -> str:
        return measurement_recording.contact_placement_message(
            self.recording_context(),
            action_label=message.action_label,
            failure_label=message.failure_label,
            point=message.point,
            record=message.record,
            position=message.position,
            total=message.total,
            success=message.success,
            seek=message.seek,
        )

    def _settle_contact(self) -> bool:
        return self.adapters.control.settle(
            self.snapshot().contact_settle_s,
            interruptible=True,
        )

    def _reset_seek(self) -> None:
        self.state.seek_result = None

    @staticmethod
    def _source_list_count(
        settings: RoutePointRuntimeSnapshot,
        count: int,
    ) -> int:
        followup_count = max(
            0,
            settings.measurement_count - settings.initial_measurement_count,
        )
        return max(1, int(count), followup_count)


class BackgroundPointAction:
    def __init__(self, target: Callable[[], object]) -> None:
        self._target = target
        self._exception: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def wait(self) -> None:
        self._thread.join()
        if self._exception is not None:
            raise self._exception

    def _run(self) -> None:
        try:
            self._target()
        except BaseException as exc:
            self._exception = exc


class StagePointMotionAdapter:
    def __init__(self, controller: object) -> None:
        self._controller = controller

    def raise_needles(self, feedrate: float | None) -> None:
        self._needles_action("raise", feedrate)

    def move_to(self, target_xy: Point2D) -> None:
        mover = getattr(self._controller, "run_external_move_to_xy")
        mover(float(target_xy[0]), float(target_xy[1]))

    def lower_needles(self, feedrate: float | None) -> None:
        self._needles_action("lower", feedrate)

    def start_lift(self, feedrate: float | None) -> PointAction:
        return BackgroundPointAction(lambda: self._needles_action("lift", feedrate))

    def fallback_lift(self, feedrate: float | None) -> None:
        self._needles_action("lift", feedrate)

    def press_to_depth(
        self,
        depth_mm: float,
        adjust_delta_mm: float,
        feedrate: float | None,
    ) -> bool:
        lower = getattr(
            self._controller,
            "run_external_needles_lower_to_depth_below_down",
            None,
        )
        if callable(lower) and depth_mm > 0.0:
            lower(float(depth_mm), feedrate)
            return True
        adjust = getattr(self._controller, "run_external_needles_adjust", None)
        if not callable(adjust):
            return False
        adjust(float(adjust_delta_mm), feedrate)
        return True

    def latest_axis_a_lowering(self) -> float:
        getter = getattr(self._controller, "latest_axis_a_lowering", None)
        if not callable(getter):
            return math.nan
        try:
            value = float(getter())
        except (TypeError, ValueError):
            return math.nan
        return value if math.isfinite(value) else math.nan

    def _needles_action(self, action: str, feedrate: float | None) -> None:
        runner = getattr(self._controller, "run_external_needles_action")
        runner(action, feedrate)


class MeterPointAcquisitionAdapter:
    def __init__(
        self,
        controller: object,
        *,
        cancelled: Callable[[], bool],
    ) -> None:
        self._controller = controller
        self._cancelled = cancelled
        self._output_context: object | None = None

    def prepare(self, count: int, source_list_count: int) -> PointAction | None:
        prepare = getattr(
            self._controller,
            "prepare_route_measurement_batch_now",
            None,
        )
        if not callable(prepare):
            return None

        def run() -> None:
            if _accepts_keyword(prepare, "source_list_count"):
                prepare(int(count), source_list_count=int(source_list_count))
            else:
                prepare(int(count))

        return BackgroundPointAction(run)

    def enable_output(self) -> None:
        if self._output_context is not None:
            return
        output = getattr(self._controller, "output", None)
        if not callable(output):
            return
        context = output(True)
        enter = getattr(context, "__enter__", None)
        if callable(enter):
            enter()
            self._output_context = context

    def close_output(self) -> None:
        context = self._output_context
        self._output_context = None
        if context is None:
            return
        exit_method = getattr(context, "__exit__", None)
        if callable(exit_method):
            exit_method(None, None, None)

    def read(
        self,
        count: int,
        start_index: int,
        after_measurement: Callable[[], object] | None = None,
    ) -> list[RouteMeasurementSample] | None:
        batch_reader = getattr(
            self._controller,
            "read_route_measurement_batch_now",
            None,
        )
        if callable(batch_reader) and count > 1:
            if after_measurement is not None and _accepts_keyword(
                batch_reader,
                "after_measurement",
            ):
                raw = list(
                    batch_reader(
                        int(count),
                        after_measurement=after_measurement,
                    )
                )
            else:
                raw = list(batch_reader(int(count)))
            return [
                route_measurement_sample_from_raw(value, index)
                for index, value in enumerate(raw, start=int(start_index))
            ]
        return self._read_individual(int(count), int(start_index))

    def _read_individual(
        self,
        count: int,
        start_index: int,
    ) -> list[RouteMeasurementSample] | None:
        route_reader = getattr(self._controller, "read_route_measurement_now", None)
        fallback = getattr(self._controller, "read_primary_value_now", None)
        reader = route_reader if callable(route_reader) else fallback
        if not callable(reader):
            raise RuntimeError("Route measurement readout is unavailable.")
        samples: list[RouteMeasurementSample] = []
        for index in range(start_index, start_index + count):
            if self._cancelled():
                return None
            samples.append(route_measurement_sample_from_raw(reader(), index))
            if self._cancelled():
                return None
        return samples


class CallbackPointPhotoAdapter:
    def __init__(
        self,
        events: RouteMeasurementEvents,
        cancelled: Callable[[BaseException], bool],
    ) -> None:
        self._events = events
        self._cancelled = cancelled

    def autofocus(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        settings: PointPhotoSettings,
    ) -> PointFocusOutcome:
        point_callback = self._events.point_photo_focus
        callback = self._events.photo_focus
        if point_callback is None and callback is None:
            raise ValueError("Route autofocus is not configured.")
        try:
            value = (
                point_callback(point, position, total, settings)
                if point_callback is not None
                else callback(point, position, total)
            )
            return PointFocusOutcome(value, safe_z=True)
        except Exception as exc:
            if self._cancelled(exc):
                return PointFocusOutcome(None, safe_z=True)
            raise

    def capture(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        focus_result: object | None,
        stage_xy: Point2D,
        settings: PointPhotoSettings,
    ) -> Path:
        _ = stage_xy
        point_callback = self._events.point_photo
        callback = self._events.photo
        if point_callback is None and callback is None:
            raise ValueError("Route photo capture is not configured.")
        path = (
            point_callback(point, position, total, focus_result, settings)
            if point_callback is not None
            else callback(point, position, total, focus_result)
        )
        return Path(path).expanduser()


class EventPointControlAdapter:
    def __init__(
        self,
        *,
        stopped: Callable[[], bool],
        interrupted: Callable[[], bool],
    ) -> None:
        self._stopped = stopped
        self._interrupted = interrupted

    def stopped(self) -> bool:
        return self._stopped()

    def interrupted(self) -> bool:
        return self._interrupted()

    def settle(self, seconds: float, *, interruptible: bool) -> bool:
        deadline = time.monotonic() + max(0.0, float(seconds))
        while True:
            if self.stopped() or (interruptible and self.interrupted()):
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return True
            time.sleep(min(remaining, 0.05))

    def cancelled(self, exc: BaseException) -> bool:
        return self.interrupted() and str(exc) == "Operation cancelled."


class CallbackPointEventAdapter:
    def __init__(
        self,
        *,
        callbacks: RouteMeasurementEvents,
        append_csv: Callable[[RouteMeasurementRecord], None],
    ) -> None:
        self._callbacks = callbacks
        self._append_csv = append_csv

    def status(self, message: str) -> None:
        if self._callbacks.status is not None:
            self._callbacks.status(message)

    def photo_saved(
        self,
        record: RoutePhotoRecord,
        position: int,
        total: int,
    ) -> None:
        if self._callbacks.photo_record is not None:
            self._callbacks.photo_record(record, position, total)

    def pre_contact(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> None:
        callback = self._callbacks.pre_contact_photo
        if callback is None:
            return
        try:
            callback(point, position, total)
        except Exception as exc:
            logger.warning("Route pre-contact photo callback failed: %s", exc)

    def save(self, record: RouteMeasurementRecord) -> None:
        self._append_csv(record)

    def measurement_finished(self, event: RoutePointMeasurementEvent) -> None:
        self.emit_contact_photo(
            event.point,
            event.record,
            event.position,
            event.total,
            event.saved,
        )
        self.emit_result(event.record, event.position, event.total, event.saved)
        if not event.accepted:
            return
        if event.saved and self._callbacks.point_contact_height is not None:
            self._callbacks.point_contact_height(
                event.contact_height,
                event.position,
                event.total,
                event.photo_settings,
            )
        elif event.saved and self._callbacks.contact_height is not None:
            self._callbacks.contact_height(
                event.contact_height,
                event.position,
                event.total,
            )
        if self._callbacks.record is not None:
            self._callbacks.record(event.record, event.position, event.total)

    def emit_contact_photo(
        self,
        point: RouteMeasurementPoint,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        saved: bool,
    ) -> None:
        callback = self._callbacks.contact_photo
        if callback is None:
            return
        try:
            callback(
                point,
                record,
                position,
                total,
                saved,
            )
        except Exception as exc:
            logger.warning("Route contact photo callback failed: %s", exc)

    def emit_result(
        self,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        saved: bool,
    ) -> None:
        if self._callbacks.result is not None:
            self._callbacks.result(record, position, total, saved)


def _accepts_keyword(callback: Callable[..., object], keyword: str) -> bool:
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return False
    parameters = signature.parameters
    return keyword in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


__all__ = [
    "CallbackPointEventAdapter",
    "CallbackPointPhotoAdapter",
    "EventPointControlAdapter",
    "LegacyContactBindings",
    "MeterPointAcquisitionAdapter",
    "PointExecutionAdapters",
    "RouteMeasurementEvents",
    "StagePointMotionAdapter",
]
