"""Blocking probe-route measurement runner and CSV persistence."""

from __future__ import annotations

import csv
import inspect
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from probe_station_gui.route_contact_quality import (
    RouteContactQuality,
    RouteContactQualityLimits,
    RouteMeasurementSample,
    _contact_quality_from_samples,
    _format_contact_quality_failure,
    _measurement_sample_from_raw,
    route_measurement_sample_from_raw,
    summarize_route_contact_quality,
)
from probe_station_gui.route_measurement_csv import (
    CSV_FIELDS,
    RouteMeasurementCsvWriter,
)


Point2D = tuple[float, float]


logger = logging.getLogger(__name__)


ROUTE_OPERATION_MEASURE = "measure"
ROUTE_OPERATION_PHOTO = "photo"
ROUTE_OPERATION_PHOTO_THEN_MEASURE = "photo_then_measure"
ROUTE_OPERATION_MODES = (
    ROUTE_OPERATION_MEASURE,
    ROUTE_OPERATION_PHOTO,
    ROUTE_OPERATION_PHOTO_THEN_MEASURE,
)


def _callable_accepts_keyword(function: object, name: str) -> bool:
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if (
            parameter.name == name
            and parameter.kind
            in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
        ):
            return True
    return False


class _BackgroundRouteTask:
    def __init__(self, target: Callable[[], object]) -> None:
        self._target = target
        self._done = threading.Event()
        self._exception: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "_BackgroundRouteTask":
        self._thread.start()
        return self

    def wait(self) -> None:
        self._thread.join()
        if self._exception is not None:
            raise self._exception

    def done(self) -> bool:
        return self._done.is_set()

    def _run(self) -> None:
        try:
            self._target()
        except BaseException as exc:
            self._exception = exc
        finally:
            self._done.set()


@dataclass(frozen=True)
class RouteMeasurementPoint:
    """One route point resolved into stage coordinates before a run starts."""

    index: int
    point_id: str
    label: str
    design_center: Point2D
    stage_xy: Point2D
    needle_1_design: Point2D
    needle_2_design: Point2D
    photo_stage_xy: Point2D | None = None


@dataclass(frozen=True)
class RouteContactSeekResult:
    """Needle depth selected by automatic contact seek for one route point."""

    found: bool
    status: str
    attempts: int
    initial_status: str
    final_status: str
    depth_below_down_mm: float
    axis_a_lowering_mm: float = math.nan
    step_mm: float = math.nan
    max_depth_mm: float = math.nan


@dataclass(frozen=True)
class RouteMeasurementRecord:
    """One completed measurement row written to CSV."""

    timestamp: str
    structure_number: int
    nplc: str
    measurement_type: str
    n_measurements: int
    resistance_ohm: float
    resistance_rms_ohm: float
    relative_rms: float
    status: str
    contact_quality: RouteContactQuality | None = None
    raw_samples: tuple["RouteMeasurementSample", ...] = ()


@dataclass(frozen=True)
class RouteContactPlacementResult:
    """Result of preparing one route contact for external measurements."""

    success: bool
    message: str
    point: RouteMeasurementPoint
    record: RouteMeasurementRecord
    contact_seek: RouteContactSeekResult | None = None


@dataclass(frozen=True)
class RouteExternalContactPreparation:
    """Prepared route contact plus optional pre-contact photo/focus details."""

    placement: RouteContactPlacementResult
    photo_path: str | None = None
    focus: dict[str, object] | None = None


@dataclass(frozen=True)
class RouteContactHeightRecord:
    """One contact-height map row written next to route measurements."""

    timestamp: str
    structure_number: int
    point_index: int
    point_id: str
    label: str
    design_center: Point2D
    stage_xy: Point2D
    measurement_status: str
    resistance_ohm: float
    resistance_rms_ohm: float
    relative_rms: float
    contact_quality: RouteContactQuality | None = None
    contact_found: bool = False
    contact_depth_below_down_mm: float = math.nan
    contact_axis_a_lowering_mm: float = math.nan
    contact_seek: RouteContactSeekResult | None = None


@dataclass(frozen=True)
class RoutePhotoRecord:
    """One completed route microscope image."""

    timestamp: str
    path: str
    structure_number: int
    point_index: int
    point_id: str
    label: str
    design_center: Point2D
    stage_xy: Point2D
    focus: dict[str, object] | None = None


class RouteMeasurementRunner:
    """Run a saved probe route using direct stage and LCR controller methods."""

    DEFAULT_CONTACT_SETTLE_S = 0.2
    SHORT_CHECK_SAMPLE_COUNT = 10
    AUTO_CONTACT_SEEK_STEP_MM = -0.001
    AUTO_CONTACT_SEEK_MAX_TOTAL_MM = 0.010

    def __init__(
        self,
        *,
        points: list[RouteMeasurementPoint],
        csv_path: str | Path,
        stage_controller: Any,
        lcr_controller: Any,
        needle_feedrate: float | None,
        measurement_count: int = 1,
        initial_measurement_count: int | None = None,
        start_point_number: int = 1,
        max_relative_rms: float | None = None,
        short_threshold_ohm: float | None = None,
        contact_quality_limits: RouteContactQualityLimits | None = None,
        confirm_each_point: bool = False,
        auto_next_ok_or_short: bool = False,
        auto_contact_seek_on_bad_contact: bool = False,
        auto_contact_seek_step_mm: float = AUTO_CONTACT_SEEK_STEP_MM,
        auto_contact_seek_max_total_mm: float = AUTO_CONTACT_SEEK_MAX_TOTAL_MM,
        contact_settle_s: float = DEFAULT_CONTACT_SETTLE_S,
        nplc_label: str = "",
        measurement_type: str = "",
        status_callback: Callable[[str], None] | None = None,
        progress_callback: Callable[[int, int, int], None] | None = None,
        record_callback: Callable[[RouteMeasurementRecord, int, int], None] | None = None,
        photo_callback: Callable[
            [RouteMeasurementPoint, int, int, object | None],
            str | Path,
        ]
        | None = None,
        photo_focus_callback: Callable[[RouteMeasurementPoint, int, int], object | None]
        | None = None,
        photo_record_callback: Callable[[RoutePhotoRecord, int, int], None]
        | None = None,
        contact_height_record_callback: Callable[
            [RouteContactHeightRecord, int, int],
            None,
        ]
        | None = None,
        contact_photo_callback: Callable[
            [RouteMeasurementPoint, RouteMeasurementRecord, int, int, bool],
            None,
        ]
        | None = None,
        pre_contact_photo_callback: Callable[
            [RouteMeasurementPoint, int, int],
            None,
        ]
        | None = None,
        result_callback: Callable[[RouteMeasurementRecord, int, int, bool], None]
        | None = None,
        waiting_callback: Callable[[bool], None] | None = None,
        operation_mode: str = ROUTE_OPERATION_MEASURE,
        photo_settle_s: float = 0.2,
        photo_focus_enabled: bool = False,
        wait_before_first_point: bool = False,
    ) -> None:
        self._points = list(points)
        self._csv_writer = RouteMeasurementCsvWriter(csv_path)
        self._stage_controller = stage_controller
        self._lcr_controller = lcr_controller
        self._needle_feedrate = needle_feedrate
        self._measurement_count = max(1, int(measurement_count))
        if initial_measurement_count is None:
            initial_count = self.SHORT_CHECK_SAMPLE_COUNT
        else:
            initial_count = int(initial_measurement_count)
        self._initial_measurement_count_value = max(1, initial_count)
        self._start_point_number = max(1, int(start_point_number))
        try:
            max_relative_rms_value = float(max_relative_rms)
        except (TypeError, ValueError):
            max_relative_rms_value = math.nan
        self._max_relative_rms = (
            max_relative_rms_value
            if math.isfinite(max_relative_rms_value)
            and max_relative_rms_value > 0.0
            else None
        )
        self._contact_quality_limits = (
            contact_quality_limits or RouteContactQualityLimits()
        ).normalized()
        _ = short_threshold_ohm
        self._confirm_each_point = bool(confirm_each_point)
        self._auto_next_lock = threading.Lock()
        self._auto_next_ok_or_short = bool(auto_next_ok_or_short)
        self._auto_contact_seek_on_bad_contact = bool(
            auto_contact_seek_on_bad_contact
        )
        self._auto_contact_seek_step_mm = self._normalized_contact_seek_step(
            auto_contact_seek_step_mm
        )
        self._auto_contact_seek_max_total_mm = self._normalized_contact_seek_limit(
            auto_contact_seek_max_total_mm
        )
        self._contact_settle_s = max(0.0, float(contact_settle_s))
        self._nplc_label = str(nplc_label)
        self._measurement_type = str(measurement_type)
        self._status_callback = status_callback
        self._progress_callback = progress_callback
        self._record_callback = record_callback
        self._photo_callback = photo_callback
        self._photo_focus_callback = photo_focus_callback
        self._photo_record_callback = photo_record_callback
        self._contact_height_record_callback = contact_height_record_callback
        self._contact_photo_callback = contact_photo_callback
        self._pre_contact_photo_callback = pre_contact_photo_callback
        self._result_callback = result_callback
        self._waiting_callback = waiting_callback
        self._operation_mode = _normalize_operation_mode(operation_mode)
        self._measure_enabled = self._operation_mode in {
            ROUTE_OPERATION_MEASURE,
            ROUTE_OPERATION_PHOTO_THEN_MEASURE,
        }
        self._photo_enabled = self._operation_mode in {
            ROUTE_OPERATION_PHOTO,
            ROUTE_OPERATION_PHOTO_THEN_MEASURE,
        }
        self._photo_settle_s = max(0.0, float(photo_settle_s))
        self._photo_focus_enabled = bool(photo_focus_enabled)
        self._wait_before_first_point = bool(wait_before_first_point)
        self._stop_requested = threading.Event()
        self._point_interrupt_requested = threading.Event()
        self._contact_seek_active = threading.Event()
        self._pause_requested = threading.Event()
        self._confirmation_condition = threading.Condition()
        self._pending_confirmation: str | None = None
        self._stage_task_active = False
        self._progress_started_at: float | None = None
        self._route_offset_lock = threading.Lock()
        self._route_offset_xy: Point2D = (0.0, 0.0)
        self._last_recorded_point: RouteMeasurementPoint | None = None
        self._current_contact_seek_result: RouteContactSeekResult | None = None
        self._background_tasks: list[_BackgroundRouteTask] = []
        self._waiting_condition = threading.Condition()
        self._waiting = False
        self._meter_output_context: object | None = None

    @property
    def csv_path(self) -> Path:
        return self._csv_writer.path

    def route_offset_xy(self) -> Point2D:
        with self._route_offset_lock:
            return self._route_offset_xy

    def set_route_offset_xy(self, offset_xy: Point2D) -> None:
        try:
            offset_x = float(offset_xy[0])
            offset_y = float(offset_xy[1])
        except (TypeError, ValueError, IndexError):
            return
        if not math.isfinite(offset_x) or not math.isfinite(offset_y):
            return
        with self._route_offset_lock:
            self._route_offset_xy = (offset_x, offset_y)

    def contact_quality_limits(self) -> RouteContactQualityLimits:
        return self._contact_quality_limits

    def is_waiting(self) -> bool:
        with self._waiting_condition:
            return bool(self._waiting)

    def wait_until_waiting(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._waiting_condition:
            while True:
                if self._waiting:
                    return True
                if self._stop_requested.is_set():
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._waiting_condition.wait(timeout=min(0.05, remaining))

    def set_current_adjustment_point(self, point_number: int) -> tuple[bool, str]:
        index = self._index_for_point_number(int(point_number))
        if index is None:
            return False, f"Point {int(point_number)} is not enabled or not found."
        with self._route_offset_lock:
            self._last_recorded_point = self._points[index]
        return True, ""

    def stop(self) -> None:
        self._stop_requested.set()
        with self._confirmation_condition:
            self._confirmation_condition.notify_all()

    def submit_confirmation(self, action: str) -> bool:
        normalized = str(action).strip().lower()
        if normalized.isdigit():
            normalized = f"jump:{int(normalized)}"
        elif normalized.startswith("jump:"):
            try:
                normalized = f"jump:{int(normalized.split(':', 1)[1].strip())}"
            except ValueError:
                return False
        elif normalized not in {"next", "measure", "remeasure", "skip"}:
            return False
        with self._confirmation_condition:
            self._pending_confirmation = normalized
            self._confirmation_condition.notify_all()
        return True

    def submit_jump(self, point_number: int) -> bool:
        return self.submit_confirmation(f"jump:{int(point_number)}")

    def request_current_point_correction(self) -> None:
        self._point_interrupt_requested.set()
        with self._confirmation_condition:
            self._confirmation_condition.notify_all()

    def clear_current_point_correction_request(self) -> None:
        self._point_interrupt_requested.clear()

    def request_pause_after_current_point(self) -> None:
        self._pause_requested.set()

    def set_auto_next_ok_or_short(self, enabled: bool) -> None:
        with self._auto_next_lock:
            self._auto_next_ok_or_short = bool(enabled)

    def update_runtime_settings(
        self,
        *,
        measurement_count: int,
        initial_measurement_count: int,
        max_relative_rms: float | None,
        auto_contact_seek_step_mm: float,
        auto_contact_seek_max_total_mm: float,
        contact_settle_s: float,
        contact_quality_limits: RouteContactQualityLimits | None = None,
        photo_settle_s: float | None = None,
        photo_focus_enabled: bool | None = None,
        csv_path: str | Path | None = None,
        nplc_label: str | None = None,
        measurement_type: str | None = None,
    ) -> None:
        """Update settings that are safe to change while waiting for confirmation."""

        self._measurement_count = max(1, int(measurement_count))
        self._initial_measurement_count_value = max(
            1,
            int(initial_measurement_count),
        )
        try:
            max_relative_rms_value = float(max_relative_rms)
        except (TypeError, ValueError):
            max_relative_rms_value = math.nan
        self._max_relative_rms = (
            max_relative_rms_value
            if math.isfinite(max_relative_rms_value)
            and max_relative_rms_value > 0.0
            else None
        )
        if contact_quality_limits is not None:
            self._contact_quality_limits = contact_quality_limits.normalized()
        self._auto_contact_seek_step_mm = self._normalized_contact_seek_step(
            auto_contact_seek_step_mm
        )
        self._auto_contact_seek_max_total_mm = self._normalized_contact_seek_limit(
            auto_contact_seek_max_total_mm
        )
        self._contact_settle_s = max(0.0, float(contact_settle_s))
        if photo_settle_s is not None:
            self._photo_settle_s = max(0.0, float(photo_settle_s))
        if photo_focus_enabled is not None:
            self._photo_focus_enabled = bool(photo_focus_enabled)
        if csv_path is not None:
            path_text = str(csv_path).strip()
            if path_text:
                self._csv_writer.set_path(path_text)
        if nplc_label is not None:
            self._nplc_label = str(nplc_label)
        if measurement_type is not None:
            self._measurement_type = str(measurement_type)

    def apply_meter_configuration(self, configuration: object) -> None:
        applicator = getattr(
            self._lcr_controller,
            "apply_route_meter_configuration",
            None,
        )
        if callable(applicator):
            applicator(configuration)

    def save_current_position_adjustment(
        self,
        current_stage_xy: Point2D,
    ) -> tuple[bool, str]:
        try:
            current_x = float(current_stage_xy[0])
            current_y = float(current_stage_xy[1])
        except (TypeError, ValueError, IndexError):
            return False, "Current stage X/Y position is unavailable."
        if not math.isfinite(current_x) or not math.isfinite(current_y):
            return False, "Current stage X/Y position is unavailable."
        with self._route_offset_lock:
            point = self._last_recorded_point
            if point is None:
                return False, "Measure a route point before saving a route shift."
            offset_x = current_x - float(point.stage_xy[0])
            offset_y = current_y - float(point.stage_xy[1])
            self._route_offset_xy = (offset_x, offset_y)
        return (
            True,
            "Route shift saved: "
            f"dX={offset_x:+.4f} mm, dY={offset_y:+.4f} mm.",
        )

    def place_contact(
        self,
        point: RouteMeasurementPoint,
        *,
        position: int = 1,
        total: int = 1,
        move_to_point: bool = True,
        lift_before_move: bool = True,
        lift_on_failure: bool = True,
        clear_interrupt: bool = True,
    ) -> RouteContactPlacementResult:
        """Move to a route point and leave verified contact under the needles.

        This intentionally reuses the same short contact check and automatic
        deeper-contact seek path as route measurements, but does not write a CSV row.
        """

        if clear_interrupt:
            self._point_interrupt_requested.clear()
        elif self._point_interrupt_requested.is_set():
            raise RuntimeError("Contact placement interrupted.")
        self._current_contact_seek_result = None
        self._begin_stage_task()
        needles_lowered = False
        placement_succeeded = False
        prepare_task: _BackgroundRouteTask | None = None
        try:
            if lift_before_move:
                self._status(
                    f"Route contact: point {position}/{total} lifting needles."
                )
                self._stage_controller.run_external_needles_action(
                    "lift",
                    self._needle_feedrate,
                )
                self._raise_if_point_interrupted()
            if move_to_point:
                target_xy = self._adjusted_stage_xy(point)
                self._status(
                    f"Route contact: point {position}/{total} moving."
                )
                self._stage_controller.run_external_move_to_xy(
                    target_xy[0],
                    target_xy[1],
                )
                self._raise_if_point_interrupted()
            self._raise_if_point_interrupted()
            prepare_task = self._start_measurement_prepare_task(
                self._initial_measurement_count()
            )
            self._raise_if_point_interrupted()
            if prepare_task is not None:
                prepare_task.wait()
                prepare_task = None
            self._status(
                f"Route contact: point {position}/{total} lowering needles."
            )
            self._emit_pre_contact_photo(point, position, total)
            self._raise_if_point_interrupted()
            self._lower_needles_for_measurement()
            needles_lowered = True
            if not self._sleep_contact_settle():
                raise RuntimeError("Contact placement stopped.")
            self._status(
                f"Route contact: point {position}/{total} checking contact."
            )
            samples = self._measure_samples(
                position=position,
                total=total,
                prepare_task=prepare_task,
            )
            prepare_task = None
            if samples is None:
                raise RuntimeError("Contact placement stopped.")
            record = self._record_for_point(point=point, samples=samples)
            success = self._contact_placement_record_is_success(record)
            if not success and lift_on_failure and needles_lowered:
                self._stage_controller.run_external_needles_action(
                    "lift",
                    self._needle_feedrate,
                )
                needles_lowered = False
                self._close_meter_output_context()
            status = record.status or "unknown"
            if success:
                message = (
                    f"Contact ready: point {position}/{total} {point.label}, "
                    f"{status}."
                )
                placement_succeeded = True
            else:
                contact_suffix = (
                    self._contact_quality_failure_suffix(record.contact_quality)
                    if record.contact_quality is not None
                    else ""
                )
                message = (
                    f"Contact check failed: point {position}/{total} "
                    f"{point.label}, {status}{contact_suffix}."
                )
            self._emit_contact_photo(point, record, position, total, success)
            self._status(message)
            return RouteContactPlacementResult(
                success=success,
                message=message,
                point=point,
                record=record,
                contact_seek=self._current_contact_seek_result,
            )
        finally:
            if needles_lowered and lift_on_failure and not placement_succeeded:
                try:
                    self._stage_controller.run_external_needles_action(
                        "lift",
                        self._needle_feedrate,
                    )
                    self._close_meter_output_context()
                except Exception:
                    logger.exception("Failed to lift needles after contact check.")
            try:
                self._wait_for_background_tasks()
            finally:
                self._finish_stage_task()

    def prepare_external_contact(
        self,
        point: RouteMeasurementPoint,
        *,
        position: int = 1,
        total: int = 1,
        photo_enabled: bool = False,
        photo_focus_enabled: bool = False,
        move_to_point: bool = True,
        lift_before_move: bool = True,
        lift_on_failure: bool = False,
    ) -> RouteExternalContactPreparation:
        """Prepare one route contact and leave needles down when usable.

        This is the contact lifecycle used by API-driven external measurement
        sessions. It reuses the route photo/autofocus and resistance/contact seek
        code paths, but does not write resistance rows to CSV.
        """

        focus_result: object | None = None
        photo_path: str | None = None
        if move_to_point and (photo_enabled or photo_focus_enabled):
            self._begin_stage_task()
            try:
                self._status(
                    f"Route contact: point {position}/{total} raising needles."
                )
                self._stage_controller.run_external_needles_action(
                    "raise",
                    self._needle_feedrate,
                )
                self._raise_if_point_interrupted()
                photo_xy = self._adjusted_photo_stage_xy(point)
                self._status(
                    f"Route contact: point {position}/{total} moving to photo."
                )
                self._stage_controller.run_external_move_to_xy(
                    photo_xy[0],
                    photo_xy[1],
                )
                self._raise_if_point_interrupted()
                if photo_focus_enabled:
                    self._status(
                        f"Route contact: point {position}/{total} local autofocus."
                    )
                    focus_result = self._run_photo_focus(point, position, total)
                    self._raise_if_point_interrupted()
                    focus_message = str(focus_result or "")
                    if focus_message.strip():
                        self._status(
                            f"Route contact: point {position}/{total} "
                            f"{focus_message}"
                        )
                if photo_enabled:
                    if not self._sleep_photo_settle():
                        raise RuntimeError("Route contact photo stopped.")
                    photo_path = str(
                        self._capture_photo(
                            point,
                            position,
                            total,
                            focus_result=focus_result,
                        )
                    )
                    self._status(
                        f"Route contact: point {position}/{total} photo captured."
                    )
                    self._raise_if_point_interrupted()
                contact_xy = self._adjusted_stage_xy(point)
                if not self._same_stage_xy(photo_xy, contact_xy):
                    self._status(
                        f"Route contact: point {position}/{total} "
                        "moving to contact."
                    )
                    self._stage_controller.run_external_move_to_xy(
                        contact_xy[0],
                        contact_xy[1],
                    )
                    self._raise_if_point_interrupted()
            finally:
                try:
                    self._wait_for_background_tasks()
                finally:
                    self._finish_stage_task()
            move_to_point = False
            lift_before_move = False
        placement = self.place_contact(
            point,
            position=position,
            total=total,
            move_to_point=move_to_point,
            lift_before_move=lift_before_move,
            lift_on_failure=lift_on_failure,
            clear_interrupt=False,
        )
        return RouteExternalContactPreparation(
            placement=placement,
            photo_path=photo_path,
            focus=_focus_result_to_dict(focus_result),
        )

    def _raise_if_point_interrupted(self) -> None:
        if self._point_interrupt_requested.is_set():
            raise RuntimeError("Route contact interrupted.")

    def lift_needles_after_external_measurement(
        self,
        *,
        position: int = 1,
        total: int = 1,
    ) -> None:
        """Lift needles after an API-owned external measurement."""

        self._begin_stage_task()
        try:
            self._status(
                f"Route contact: point {position}/{total} lifting needles."
            )
            self._stage_controller.run_external_needles_action(
                "lift",
                self._needle_feedrate,
            )
        finally:
            try:
                self._wait_for_background_tasks()
            finally:
                try:
                    self._close_meter_output_context()
                finally:
                    self._finish_stage_task()

    def check_contact(
        self,
        point: RouteMeasurementPoint,
        *,
        position: int = 1,
        total: int = 1,
    ) -> RouteContactPlacementResult:
        """Measure current contact quality without moving needles deeper."""

        return self._measure_current_contact(
            point,
            position=position,
            total=total,
            auto_contact_seek=False,
            action_label="Contact check",
        )

    def seek_contact(
        self,
        point: RouteMeasurementPoint,
        *,
        position: int = 1,
        total: int = 1,
    ) -> RouteContactPlacementResult:
        """Run automatic contact seek from the current needle position."""

        return self._measure_current_contact(
            point,
            position=position,
            total=total,
            auto_contact_seek=True,
            action_label="Contact seek",
        )

    def _measure_current_contact(
        self,
        point: RouteMeasurementPoint,
        *,
        position: int,
        total: int,
        auto_contact_seek: bool,
        action_label: str,
    ) -> RouteContactPlacementResult:
        self._point_interrupt_requested.clear()
        self._current_contact_seek_result = None
        previous_auto_seek = self._auto_contact_seek_on_bad_contact
        self._auto_contact_seek_on_bad_contact = bool(auto_contact_seek)
        self._begin_stage_task()
        prepare_task: _BackgroundRouteTask | None = None
        try:
            prepare_task = self._start_measurement_prepare_task(
                self._initial_measurement_count()
            )
            if not self._sleep_contact_settle():
                raise RuntimeError(f"{action_label} stopped.")
            self._status(
                f"{action_label}: point {position}/{total} checking contact."
            )
            samples = self._measure_samples(
                position=position,
                total=total,
                prepare_task=prepare_task,
            )
            prepare_task = None
            if samples is None:
                raise RuntimeError(f"{action_label} stopped.")
            record = self._record_for_point(point=point, samples=samples)
            success = self._contact_placement_record_is_success(record)
            status = record.status or "unknown"
            seek = self._current_contact_seek_result
            contact_suffix = (
                self._contact_quality_failure_suffix(record.contact_quality)
                if record.contact_quality is not None
                else ""
            )
            if auto_contact_seek and seek is not None:
                message = (
                    f"{action_label}: point {position}/{total} {point.label}, "
                    f"{seek.status}, final={status}{contact_suffix}."
                )
            elif success:
                message = (
                    f"{action_label}: point {position}/{total} {point.label}, "
                    f"{status}."
                )
            else:
                message = (
                    f"{action_label} failed: point {position}/{total} "
                    f"{point.label}, {status}{contact_suffix}."
                )
            self._status(message)
            return RouteContactPlacementResult(
                success=success,
                message=message,
                point=point,
                record=record,
                contact_seek=seek,
            )
        finally:
            self._auto_contact_seek_on_bad_contact = previous_auto_seek
            try:
                self._wait_for_background_tasks()
            finally:
                self._finish_stage_task()

    def run(self) -> tuple[bool, str]:
        needs_final_lift = False
        measurements_saved = 0
        photos_saved = 0
        success = False
        message = "Route measurement stopped."
        try:
            if not self._points:
                raise ValueError("Route has no enabled points.")
            if self._photo_enabled and self._photo_callback is None:
                raise ValueError("Route photo capture is not configured.")
            if self._photo_focus_enabled and self._photo_focus_callback is None:
                raise ValueError("Route autofocus is not configured.")
            if self._measure_enabled and hasattr(self._lcr_controller, "open"):
                self._status("Route measurement: connecting meter.")
                self._lcr_controller.open()
            self._begin_stage_task()
            if self._stop_requested.is_set():
                message = "Route measurement stopped by user."
                return success, message
            initial_needle_action = (
                "raise"
                if self._photo_enabled or self._photo_focus_enabled
                else "lift"
            )
            self._status(
                "Route measurement: raising needles."
                if initial_needle_action == "raise"
                else "Route measurement: lifting needles."
            )
            self._stage_controller.run_external_needles_action(
                initial_needle_action,
                self._needle_feedrate,
            )
            if self._stop_requested.is_set():
                message = "Route measurement stopped by user."
                return success, message
            total = len(self._points)
            start_index = self._index_for_point_number(self._start_point_number)
            if start_index is None and self._start_point_number == 1:
                start_index = 0
            if start_index is None:
                raise ValueError(
                    "Route start point "
                    f"{self._start_point_number} is not enabled or not found."
                )
            position_index = start_index
            self._progress_started_at = time.monotonic()
            if self._wait_before_first_point:
                decision = self._wait_before_first_route_point(
                    point=self._points[position_index],
                    position=position_index + 1,
                    total=total,
                )
                if decision == "stop":
                    message = "Route measurement stopped by user."
                    return success, message
                self._begin_stage_task()
                jump_index = self._jump_target_index(decision)
                if jump_index is not None:
                    position_index = jump_index
                elif decision == "skip":
                    position_index += 1
            while position_index < total:
                if self._stop_requested.is_set():
                    message = "Route measurement stopped by user."
                    break
                self._point_interrupt_requested.clear()
                position = position_index + 1
                point = self._points[position_index]
                point_interrupted = self._point_interrupt_requested.is_set()
                self._emit_progress(position, total, int(point.index))
                self._status(
                    f"Route measurement: point {position}/{total} "
                    f"{point.label}."
                )
                if self._photo_enabled or self._photo_focus_enabled:
                    self._status(
                        f"Route measurement: point {position}/{total} "
                        "raising needles before move."
                    )
                    point_interrupted = not self._run_point_interruptible_action(
                        lambda: self._stage_controller.run_external_needles_action(
                            "raise",
                            self._needle_feedrate,
                        )
                    )
                    if self._stop_requested.is_set():
                        message = "Route measurement stopped by user."
                        break
                target_xy = (
                    self._adjusted_photo_stage_xy(point)
                    if self._photo_enabled or self._photo_focus_enabled
                    else self._adjusted_stage_xy(point)
                )
                if not point_interrupted:
                    point_interrupted = not self._run_point_interruptible_action(
                        lambda: self._stage_controller.run_external_move_to_xy(
                            target_xy[0],
                            target_xy[1],
                        )
                    )
                if self._stop_requested.is_set():
                    message = "Route measurement stopped by user."
                    break
                measurement_prepare_task = (
                    self._start_measurement_prepare_task(
                        self._initial_measurement_count()
                    )
                    if self._measure_enabled and not point_interrupted
                    else None
                )
                focus_result: object | None = None
                point_interrupted = (
                    point_interrupted or self._point_interrupt_requested.is_set()
                )
                if self._photo_focus_enabled and not point_interrupted:
                    self._status(
                        f"Route measurement: point {position}/{total} "
                        "local autofocus."
                    )
                    focus_result = self._run_photo_focus(point, position, total)
                    point_interrupted = self._point_interrupt_requested.is_set()
                    focus_message = str(focus_result or "")
                    if focus_message.strip():
                        self._status(
                            f"Route measurement: point {position}/{total} "
                            f"{focus_message}"
                        )
                    if self._stop_requested.is_set():
                        message = "Route measurement stopped by user."
                        break
                if not point_interrupted and self._photo_enabled:
                    if not self._sleep_photo_settle():
                        message = "Route measurement stopped by user."
                        break
                    if self._point_interrupt_requested.is_set():
                        point_interrupted = True
                    if not point_interrupted:
                        photo_path = self._capture_photo(
                            point,
                            position,
                            total,
                            focus_result=focus_result,
                        )
                        photos_saved += 1
                        self._status(
                            f"Route measurement: point {position}/{total} "
                            f"photo saved to {photo_path}."
                        )
                    if self._stop_requested.is_set():
                        message = "Route measurement stopped by user."
                        break
                if self._measure_enabled and not point_interrupted:
                    contact_xy = self._adjusted_stage_xy(point)
                    if not self._same_stage_xy(target_xy, contact_xy):
                        self._status(
                            f"Route measurement: point {position}/{total} "
                            "moving to contact position."
                        )
                        point_interrupted = not self._run_point_interruptible_action(
                            lambda: self._stage_controller.run_external_move_to_xy(
                                contact_xy[0],
                                contact_xy[1],
                            )
                        )
                        if self._stop_requested.is_set():
                            message = "Route measurement stopped by user."
                            break
                        point_interrupted = (
                            point_interrupted
                            or self._point_interrupt_requested.is_set()
                        )
                if not self._measure_enabled:
                    if point_interrupted:
                        self._point_interrupt_requested.clear()
                        decision = self._wait_after_interrupted_point(
                            point=point,
                            position=position,
                            total=total,
                        )
                        if decision == "stop":
                            message = "Route measurement stopped by user."
                            break
                        self._begin_stage_task()
                        jump_index = self._jump_target_index(decision)
                        if jump_index is not None:
                            position_index = jump_index
                            continue
                        if decision == "skip":
                            position_index += 1
                            continue
                        continue
                    position_index += 1
                    continue
                needles_lowered = False
                lift_task: _BackgroundRouteTask | None = None

                def start_lift_after_measurement() -> None:
                    nonlocal lift_task
                    if not needles_lowered or lift_task is not None:
                        return
                    lift_task = self._start_needles_lift_task()

                record: RouteMeasurementRecord | None = None
                contact_height_record: RouteContactHeightRecord | None = None
                record_saved = False
                quality_rejected = False
                result_emitted = False
                try:
                    if not point_interrupted:
                        if measurement_prepare_task is not None:
                            measurement_prepare_task.wait()
                            measurement_prepare_task = None
                        self._emit_pre_contact_photo(point, position, total)
                        self._lower_needles_for_measurement()
                        needles_lowered = True
                        needs_final_lift = True
                        if not self._sleep_contact_settle():
                            if self._point_interrupt_requested.is_set():
                                point_interrupted = True
                            else:
                                message = "Route measurement stopped by user."
                                break
                    if not point_interrupted:
                        self._status(
                            f"Route measurement: point {position}/{total} "
                            "measuring."
                        )
                        samples = self._measure_samples(
                            position=position,
                            total=total,
                            prepare_task=measurement_prepare_task,
                            after_measurement=start_lift_after_measurement,
                        )
                        measurement_prepare_task = None
                        if samples is None:
                            if self._point_interrupt_requested.is_set():
                                point_interrupted = True
                            else:
                                message = "Route measurement stopped by user."
                                break
                        if not point_interrupted:
                            if lift_task is not None:
                                lift_task.wait()
                                needles_lowered = False
                                needs_final_lift = False
                            record = self._record_for_point(
                                point=point,
                                samples=samples,
                            )
                            contact_height_record = (
                                self._contact_height_record_for_point(
                                    point=point,
                                    record=record,
                                )
                            )
                            if needles_lowered:
                                self._stage_controller.run_external_needles_action(
                                    "lift",
                                    self._needle_feedrate,
                                )
                                needles_lowered = False
                                needs_final_lift = False
                    if not point_interrupted and record is not None:
                        save_exhausted_bad_contact = (
                            self._should_save_exhausted_bad_contact(record)
                        )
                        if (
                            self._confirm_each_point
                            and self._record_exceeds_quality_limit(record)
                            and not save_exhausted_bad_contact
                        ):
                            if not (
                                record.contact_quality is not None
                                and record.contact_quality.good is False
                            ):
                                record = replace(record, status="unstable")
                            quality_rejected = True
                        else:
                            self._csv_writer.append(record)
                            measurements_saved += 1
                            record_saved = True
                        self._emit_contact_photo(
                            point,
                            record,
                            position,
                            total,
                            record_saved,
                        )
                        self._emit_result(record, position, total, record_saved)
                        result_emitted = True
                except Exception as exc:
                    if self._point_interrupt_cancelled_exception(exc):
                        point_interrupted = True
                    else:
                        raise
                finally:
                    if lift_task is not None and needles_lowered:
                        try:
                            if self._point_interrupt_requested.is_set():
                                self._clear_stage_cancel_after_point_interrupt()
                            lift_task.wait()
                            needles_lowered = False
                            needs_final_lift = False
                        except Exception:
                            logger.warning(
                                "Background route needle lift failed; retrying.",
                                exc_info=True,
                            )
                    if needles_lowered:
                        if self._point_interrupt_requested.is_set():
                            self._clear_stage_cancel_after_point_interrupt()
                        self._stage_controller.run_external_needles_action(
                            "lift",
                            self._needle_feedrate,
                        )
                        needs_final_lift = False
                    if measurement_prepare_task is not None:
                        measurement_prepare_task.wait()
                if point_interrupted:
                    self._clear_stage_cancel_after_point_interrupt()
                    self._point_interrupt_requested.clear()
                    decision = self._wait_after_interrupted_point(
                        point=point,
                        position=position,
                        total=total,
                    )
                    if decision == "stop":
                        message = "Route measurement stopped by user."
                        break
                    self._begin_stage_task()
                    jump_index = self._jump_target_index(decision)
                    if jump_index is not None:
                        position_index = jump_index
                        continue
                    if decision == "skip":
                        position_index += 1
                        continue
                    continue
                if record is None:
                    continue
                if quality_rejected:
                    self._consume_pause_request()
                    decision = self._wait_after_rejected_result(
                        point=point,
                        record=record,
                        position=position,
                        total=total,
                        emit_result=not result_emitted,
                    )
                    if decision == "stop":
                        message = "Route measurement stopped by user."
                        break
                    if decision == "measure":
                        manual_record = self._measure_manual_contact_here(
                            point=point,
                            position=position,
                            total=total,
                        )
                        if manual_record is None:
                            if self._point_interrupt_requested.is_set():
                                self._point_interrupt_requested.clear()
                                continue
                            message = "Route measurement stopped by user."
                            break
                        measurements_saved += 1
                        position_index += 1
                        if position_index < total:
                            self._begin_stage_task()
                        continue
                    self._begin_stage_task()
                    jump_index = self._jump_target_index(decision)
                    if jump_index is not None:
                        position_index = jump_index
                        continue
                    if decision == "skip":
                        position_index += 1
                        continue
                    continue
                auto_next = (
                    self._confirm_each_point
                    and record_saved
                    and record.status in {"ok", "short"}
                    and self._auto_next_ok_or_short_enabled()
                )
                if self._confirm_each_point:
                    with self._confirmation_condition:
                        self._pending_confirmation = None
                    with self._route_offset_lock:
                        self._last_recorded_point = point
                    pause_after_point = self._consume_pause_request()
                    auto_next = auto_next and not pause_after_point
                    if not auto_next:
                        self._finish_stage_task()
                        self._set_waiting(True)
                if not result_emitted:
                    self._emit_result(record, position, total, record_saved)
                if (
                    record_saved
                    and contact_height_record is not None
                    and self._contact_height_record_callback is not None
                ):
                    self._contact_height_record_callback(
                        contact_height_record,
                        position,
                        total,
                    )
                if self._record_callback is not None:
                    self._record_callback(record, position, total)
                if self._confirm_each_point:
                    if save_exhausted_bad_contact:
                        status_detail = "contact seek exhausted; saved"
                    elif record.status == "short":
                        status_detail = "short-circuit detected; saved"
                    else:
                        status_detail = "saved"
                    if auto_next:
                        self._status(
                            f"Route measurement: point {position}/{total} "
                            f"{status_detail}; continuing."
                        )
                    else:
                        action_text = (
                            "paused"
                            if pause_after_point
                            else status_detail
                        )
                        self._status(
                            f"Route measurement: point {position}/{total} "
                            f"{action_text}; "
                            "choose Measure or Skip."
                        )
                        decision = self._wait_for_valid_confirmation()
                        self._set_waiting(False)
                        if decision == "stop":
                            message = "Route measurement stopped by user."
                            break
                        if decision == "measure":
                            manual_record = self._measure_manual_contact_here(
                                point=point,
                                position=position,
                                total=total,
                            )
                            if manual_record is None:
                                if self._point_interrupt_requested.is_set():
                                    self._point_interrupt_requested.clear()
                                    continue
                                message = "Route measurement stopped by user."
                                break
                            measurements_saved += 1
                            position_index += 1
                            if position_index < total:
                                self._begin_stage_task()
                            continue
                        self._begin_stage_task()
                        jump_index = self._jump_target_index(decision)
                        if jump_index is not None:
                            position_index = jump_index
                            continue
                        if decision == "remeasure":
                            continue
                position_index += 1
            if position_index >= total:
                success = True
                if self._measure_enabled and self._photo_enabled:
                    message = (
                        "Route measurement complete: "
                        f"{photos_saved} photos and {measurements_saved} "
                        f"measurements saved. CSV: {self.csv_path}."
                    )
                elif self._photo_enabled:
                    message = (
                        "Route photo capture complete: "
                        f"{photos_saved} photos saved."
                    )
                else:
                    message = (
                        "Route measurement complete: "
                        f"{measurements_saved} measurements saved to {self.csv_path}."
                    )
        except Exception as exc:
            message = str(exc)
            self._status(f"Route measurement failed: {message}")
        finally:
            if self._stage_task_active and needs_final_lift:
                try:
                    self._stage_controller.run_external_needles_action(
                        "lift",
                        self._needle_feedrate,
                    )
                except Exception as exc:
                    message = f"{message} Needle lift failed: {exc}"
            try:
                self._wait_for_background_tasks()
            except Exception as exc:
                message = f"{message} Background route task failed: {exc}"
            try:
                self._close_meter_output_context()
            except Exception as exc:
                message = f"{message} Instrument output disable failed: {exc}"
            self._finish_stage_task()
            if self._measure_enabled and hasattr(self._lcr_controller, "close"):
                try:
                    self._lcr_controller.close()
                except Exception as exc:
                    message = f"{message} Instrument close failed: {exc}"
            self._set_waiting(False)
        return success, message

    def _begin_stage_task(self) -> None:
        if self._stage_task_active:
            return
        label = (
            "route photo capture"
            if self._photo_enabled and not self._measure_enabled
            else "route measurement"
        )
        self._stage_controller.begin_external_task(label)
        self._stage_task_active = True

    def _finish_stage_task(self) -> None:
        if not self._stage_task_active:
            return
        self._stage_controller.finish_external_task()
        self._stage_task_active = False

    def _adjusted_stage_xy(self, point: RouteMeasurementPoint) -> Point2D:
        with self._route_offset_lock:
            offset_x, offset_y = self._route_offset_xy
        return (
            float(point.stage_xy[0]) + offset_x,
            float(point.stage_xy[1]) + offset_y,
        )

    def _adjusted_photo_stage_xy(self, point: RouteMeasurementPoint) -> Point2D:
        photo_xy = (
            point.photo_stage_xy
            if point.photo_stage_xy is not None
            else point.stage_xy
        )
        with self._route_offset_lock:
            offset_x, offset_y = self._route_offset_xy
        return (
            float(photo_xy[0]) + offset_x,
            float(photo_xy[1]) + offset_y,
        )

    @staticmethod
    def _same_stage_xy(first: Point2D, second: Point2D) -> bool:
        return (
            abs(float(first[0]) - float(second[0])) <= 1e-9
            and abs(float(first[1]) - float(second[1])) <= 1e-9
        )

    def _sleep_contact_settle(self) -> bool:
        if self._contact_settle_s <= 0.0:
            return (
                not self._stop_requested.is_set()
                and not self._point_interrupt_requested.is_set()
            )
        deadline = time.monotonic() + self._contact_settle_s
        while True:
            if (
                self._stop_requested.is_set()
                or self._point_interrupt_requested.is_set()
            ):
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return True
            if self._stop_requested.wait(min(remaining, 0.05)):
                return False

    def _sleep_photo_settle(self) -> bool:
        if self._photo_settle_s <= 0.0:
            return not self._stop_requested.is_set()
        deadline = time.monotonic() + self._photo_settle_s
        while True:
            if self._stop_requested.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return True
            if self._stop_requested.wait(min(remaining, 0.05)):
                return False

    def _status(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)

    def _emit_progress(
        self,
        position: int,
        total: int,
        point_number: int,
    ) -> None:
        if self._progress_callback is not None:
            self._progress_callback(
                int(position),
                int(total),
                int(point_number),
            )

    def _set_waiting(self, waiting: bool) -> None:
        with self._waiting_condition:
            self._waiting = bool(waiting)
            self._waiting_condition.notify_all()
        if self._waiting_callback is not None:
            self._waiting_callback(bool(waiting))

    def _auto_next_ok_or_short_enabled(self) -> bool:
        with self._auto_next_lock:
            return bool(self._auto_next_ok_or_short)

    def _consume_pause_request(self) -> bool:
        requested = self._pause_requested.is_set()
        if requested:
            self._pause_requested.clear()
        return requested

    def _emit_result(
        self,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        saved: bool,
    ) -> None:
        if self._result_callback is not None:
            self._result_callback(record, position, total, saved)

    def _emit_contact_photo(
        self,
        point: RouteMeasurementPoint,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        saved: bool,
    ) -> None:
        if self._contact_photo_callback is None:
            return
        try:
            self._contact_photo_callback(point, record, position, total, saved)
        except Exception as exc:
            logger.warning("Route contact photo callback failed: %s", exc)

    def _emit_pre_contact_photo(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> None:
        if self._pre_contact_photo_callback is None:
            return
        try:
            self._pre_contact_photo_callback(point, position, total)
        except Exception as exc:
            logger.warning("Route pre-contact photo callback failed: %s", exc)

    def _measure_manual_contact_here(
        self,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> RouteMeasurementRecord | None:
        self._current_contact_seek_result = None
        self._status(
            f"Route measurement: point {position}/{total} measuring current contact."
        )
        samples = self._read_measurement_samples(
            self._measurement_count,
            start_index=1,
            prepare_task=self._start_measurement_prepare_task(
                self._measurement_count
            ),
        )
        if samples is None:
            return None
        record = self._record_for_point(point=point, samples=samples)
        contact_height_record = self._contact_height_record_for_point(
            point=point,
            record=record,
        )
        self._csv_writer.append(record)
        self._emit_contact_photo(point, record, position, total, True)
        self._emit_result(record, position, total, True)
        if (
            contact_height_record is not None
            and self._contact_height_record_callback is not None
        ):
            self._contact_height_record_callback(
                contact_height_record,
                position,
                total,
            )
        if self._record_callback is not None:
            self._record_callback(record, position, total)
        self._status(
            f"Route measurement: point {position}/{total} saved; continuing."
        )
        return record

    def _capture_photo(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        focus_result: object | None = None,
    ) -> Path:
        if self._photo_callback is None:
            raise ValueError("Route photo capture is not configured.")
        photo_stage_xy = self._adjusted_photo_stage_xy(point)
        result = Path(
            self._photo_callback(point, position, total, focus_result)
        ).expanduser()
        record = RoutePhotoRecord(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            path=str(result),
            structure_number=_structure_number_for_point(point),
            point_index=int(point.index),
            point_id=point.point_id,
            label=point.label,
            design_center=point.design_center,
            stage_xy=photo_stage_xy,
            focus=_focus_result_to_dict(focus_result),
        )
        if self._photo_record_callback is not None:
            self._photo_record_callback(record, position, total)
        return result

    def _run_photo_focus(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> object | None:
        if self._photo_focus_callback is None:
            raise ValueError("Route autofocus is not configured.")
        try:
            return self._photo_focus_callback(point, position, total)
        except Exception as exc:
            if self._point_interrupt_cancelled_exception(exc):
                return None
            raise

    def _point_interrupt_cancelled_exception(self, exc: BaseException) -> bool:
        return bool(
            self._point_interrupt_requested.is_set()
            and str(exc) == "Operation cancelled."
        )

    def _run_point_interruptible_action(
        self,
        action: Callable[[], object],
    ) -> bool:
        try:
            action()
        except Exception as exc:
            if self._point_interrupt_cancelled_exception(exc):
                return False
            raise
        return True

    def _clear_stage_cancel_after_point_interrupt(self) -> None:
        if not self._stage_task_active:
            return
        self._finish_stage_task()
        self._begin_stage_task()

    def _start_background_task(
        self,
        target: Callable[[], object],
    ) -> _BackgroundRouteTask:
        task = _BackgroundRouteTask(target).start()
        self._background_tasks.append(task)
        return task

    def _wait_for_background_tasks(self) -> None:
        tasks = list(self._background_tasks)
        self._background_tasks.clear()
        first_exception: BaseException | None = None
        for task in tasks:
            try:
                task.wait()
            except BaseException as exc:
                if first_exception is None:
                    first_exception = exc
        if first_exception is not None:
            raise first_exception

    def _start_measurement_prepare_task(
        self,
        count: int,
    ) -> _BackgroundRouteTask | None:
        preparer = getattr(
            self._lcr_controller,
            "prepare_route_measurement_batch_now",
            None,
        )
        if not callable(preparer):
            return None
        count = max(1, int(count))
        source_list_count = self._source_list_prepare_count(count)

        def prepare() -> None:
            if _callable_accepts_keyword(preparer, "source_list_count"):
                preparer(count, source_list_count=source_list_count)
            else:
                preparer(count)

        return self._start_background_task(prepare)

    def _start_needles_lift_task(self) -> _BackgroundRouteTask:
        return self._start_background_task(
            lambda: self._stage_controller.run_external_needles_action(
                "lift",
                self._needle_feedrate,
            )
        )

    def _lower_needles_for_measurement(self) -> None:
        self._ensure_meter_output_context()
        self._stage_controller.run_external_needles_action(
            "lower",
            self._needle_feedrate,
        )

    def _ensure_meter_output_context(self) -> None:
        if self._meter_output_context is not None:
            return
        output = getattr(self._lcr_controller, "output", None)
        if not callable(output):
            return
        context = output(True)
        enter = getattr(context, "__enter__", None)
        if not callable(enter):
            return
        enter()
        self._meter_output_context = context

    def _close_meter_output_context(self) -> None:
        context = self._meter_output_context
        self._meter_output_context = None
        if context is None:
            return
        exit_method = getattr(context, "__exit__", None)
        if callable(exit_method):
            exit_method(None, None, None)

    def _source_list_prepare_count(self, count: int) -> int:
        initial_count = self._initial_measurement_count()
        followup_count = max(0, self._measurement_count - initial_count)
        return max(1, int(count), followup_count)

    @staticmethod
    def _normalized_contact_seek_step(value: object) -> float:
        try:
            step = float(value)
        except (TypeError, ValueError):
            step = RouteMeasurementRunner.AUTO_CONTACT_SEEK_STEP_MM
        if not math.isfinite(step) or step == 0.0:
            step = RouteMeasurementRunner.AUTO_CONTACT_SEEK_STEP_MM
        return -abs(step)

    @staticmethod
    def _normalized_contact_seek_limit(value: object) -> float:
        try:
            limit = float(value)
        except (TypeError, ValueError):
            limit = RouteMeasurementRunner.AUTO_CONTACT_SEEK_MAX_TOTAL_MM
        if not math.isfinite(limit) or limit < 0.0:
            return 0.0
        return limit

    def _measure_samples(
        self,
        *,
        position: int,
        total: int,
        prepare_task: _BackgroundRouteTask | None = None,
        after_measurement: Callable[[], object] | None = None,
    ) -> list[RouteMeasurementSample] | None:
        self._current_contact_seek_result = None
        initial_count = self._initial_measurement_count()
        initial_after_measurement = (
            after_measurement if self._measurement_count <= initial_count else None
        )
        samples = self._read_measurement_samples(
            initial_count,
            start_index=1,
            prepare_task=prepare_task,
            after_measurement=initial_after_measurement,
        )
        if samples is None:
            return None
        if not self._auto_contact_seek_on_bad_contact or self._samples_are_short(samples):
            return self._complete_measurement_samples(
                samples,
                after_measurement=after_measurement,
            )
        if self._samples_have_bad_contact(samples):
            return self._seek_contact_from_current_position(
                initial_samples=samples,
                position=position,
                total=total,
                after_measurement=after_measurement,
            )
        completed_samples = self._complete_measurement_samples(
            samples,
            after_measurement=after_measurement,
        )
        if (
            completed_samples is None
            or self._completed_measurement_is_acceptable(completed_samples)
        ):
            return completed_samples
        return self._seek_contact_from_current_position(
            initial_samples=completed_samples,
            position=position,
            total=total,
            skip_current_depth=True,
            after_measurement=after_measurement,
        )

    def _complete_measurement_samples(
        self,
        samples: list[RouteMeasurementSample],
        *,
        after_measurement: Callable[[], object] | None = None,
    ) -> list[RouteMeasurementSample] | None:
        remaining_count = self._measurement_count - len(samples)
        if (
            remaining_count <= 0
            or self._samples_are_short(samples)
            or self._samples_have_bad_contact(samples)
        ):
            return samples
        extra_samples = self._read_measurement_samples(
            remaining_count,
            start_index=len(samples) + 1,
            prepare_task=self._start_measurement_prepare_task(remaining_count),
            after_measurement=after_measurement,
        )
        if extra_samples is None:
            return None
        return samples + extra_samples

    def _seek_contact_from_current_position(
        self,
        *,
        initial_samples: list[RouteMeasurementSample],
        position: int,
        total: int,
        skip_current_depth: bool = False,
        after_measurement: Callable[[], object] | None = None,
    ) -> list[RouteMeasurementSample] | None:
        if self._auto_contact_seek_max_total_mm <= 0.0:
            return initial_samples
        lower_to_depth = getattr(
            self._stage_controller,
            "run_external_needles_lower_to_depth_below_down",
            None,
        )
        adjust = getattr(self._stage_controller, "run_external_needles_adjust", None)
        if not callable(lower_to_depth) and not callable(adjust):
            return initial_samples
        initial_quality = self._contact_quality_from_samples(initial_samples)
        if skip_current_depth:
            self._status(
                f"Route measurement: point {position}/{total} full measurement "
                f"rejected after contact check {initial_quality.status}; "
                "trying deeper contact up to "
                f"{self._auto_contact_seek_max_total_mm:.3f} mm."
            )
        else:
            self._status(
                f"Route measurement: point {position}/{total} contact check "
                f"{initial_quality.status}, "
                f"median={_format_ohm(initial_quality.median_ohm)}, "
                f"MAD={_format_ohm(initial_quality.mad_sigma_ohm)}"
                f"{self._contact_quality_failure_suffix(initial_quality)}; "
                "seeking contact up to "
                f"{self._auto_contact_seek_max_total_mm:.3f} mm."
            )
        self._contact_seek_active.set()
        try:
            return self._run_contact_seek_attempts(
                initial_samples=initial_samples,
                initial_quality=initial_quality,
                position=position,
                total=total,
                lower_to_depth=lower_to_depth,
                adjust=adjust,
            )
        finally:
            self._contact_seek_active.clear()

    def _run_contact_seek_attempts(
        self,
        *,
        initial_samples: list[RouteMeasurementSample],
        initial_quality: RouteContactQuality,
        position: int,
        total: int,
        lower_to_depth: Callable[..., object] | None,
        adjust: Callable[..., object] | None,
    ) -> list[RouteMeasurementSample] | None:
        step_mm = self._auto_contact_seek_step_mm
        max_depth_steps = int(
            math.ceil(self._auto_contact_seek_max_total_mm / abs(step_mm))
        )
        samples = initial_samples
        depths_mm = [
            min((step_index + 1) * abs(step_mm), self._auto_contact_seek_max_total_mm)
            for step_index in range(max_depth_steps)
        ]
        attempts = 0
        previous_depth_mm = 0.0
        last_depth_mm = math.nan
        last_axis_a_lowering_mm = math.nan
        last_status = initial_quality.status
        for depth_mm in depths_mm:
            if (
                self._stop_requested.is_set()
                or self._point_interrupt_requested.is_set()
            ):
                return None
            attempt_number = max(1, int(math.ceil(depth_mm / abs(step_mm))))
            self._status(
                f"Route measurement: point {position}/{total} "
                f"pressing deeper {attempt_number}/{max_depth_steps}, "
                f"{depth_mm:.4f} mm below down."
            )
            prepare_task = self._start_measurement_prepare_task(
                self._initial_measurement_count()
            )
            if (
                self._stop_requested.is_set()
                or self._point_interrupt_requested.is_set()
            ):
                return None
            if depth_mm > 0.0 and callable(lower_to_depth):
                lower_to_depth(depth_mm, self._needle_feedrate)
            elif callable(adjust):
                delta_mm = depth_mm - previous_depth_mm
                adjust(math.copysign(delta_mm, step_mm), self._needle_feedrate)
            else:
                return initial_samples
            previous_depth_mm = float(depth_mm)
            if (
                self._stop_requested.is_set()
                or self._point_interrupt_requested.is_set()
            ):
                return None
            attempts += 1
            last_depth_mm = float(depth_mm)
            if not self._sleep_contact_settle():
                return None
            last_axis_a_lowering_mm = self._latest_axis_a_lowering()
            samples = self._read_measurement_samples(
                self._initial_measurement_count(),
                start_index=1,
                prepare_task=prepare_task,
            )
            if samples is None:
                return None
            depth_label = f"{depth_mm:.4f} mm below down"
            if self._samples_are_short(samples):
                last_status = "short"
                self._status(
                    f"Route measurement: point {position}/{total} "
                    f"{depth_label}, short-circuit detected."
                )
                self._set_contact_seek_result(
                    found=True,
                    status="short",
                    attempts=attempts,
                    initial_status=initial_quality.status,
                    final_status=last_status,
                    depth_below_down_mm=depth_mm,
                    axis_a_lowering_mm=last_axis_a_lowering_mm,
                )
                return samples
            quality = self._contact_quality_from_samples(samples)
            last_status = quality.status
            self._status(
                f"Route measurement: point {position}/{total} "
                f"{depth_label}, {quality.status}, "
                f"median={_format_ohm(quality.median_ohm)}, "
                f"MAD={_format_ohm(quality.mad_sigma_ohm)}"
                f"{self._contact_quality_failure_suffix(quality)}."
            )
            if quality.good is not False:
                completed_samples = self._complete_measurement_samples(
                    samples,
                )
                if completed_samples is None:
                    return None
                samples = completed_samples
                if self._completed_measurement_is_acceptable(completed_samples):
                    last_status = self._contact_status_for_samples(completed_samples)
                    self._set_contact_seek_result(
                        found=True,
                        status="found"
                        if last_status != "short"
                        else "short",
                        attempts=attempts,
                        initial_status=initial_quality.status,
                        final_status=last_status,
                        depth_below_down_mm=depth_mm,
                        axis_a_lowering_mm=last_axis_a_lowering_mm,
                    )
                    return completed_samples
                if self._samples_have_bad_contact(completed_samples):
                    full_quality = self._contact_quality_from_samples(
                        completed_samples
                    )
                    last_status = full_quality.status
                    self._status(
                        f"Route measurement: point {position}/{total} full "
                        f"measurement at {depth_label} failed contact check "
                        f"({full_quality.status}, "
                        f"median={_format_ohm(full_quality.median_ohm)}, "
                        f"MAD={_format_ohm(full_quality.mad_sigma_ohm)}"
                        f"{self._contact_quality_failure_suffix(full_quality)}); "
                        "trying deeper."
                    )
                elif self._samples_exceed_relative_rms_limit(completed_samples):
                    relative_rms = self._relative_rms_from_samples(completed_samples)
                    last_status = "unstable"
                    self._status(
                        f"Route measurement: point {position}/{total} full "
                        f"measurement at {depth_label} relative RMS "
                        f"{_format_percent(relative_rms)} exceeds "
                        f"{_format_percent(self._max_relative_rms or math.nan)}; "
                        "trying deeper."
                    )
        self._status(
            f"Route measurement: point {position}/{total} contact seek did not "
            f"find stable contact within {self._auto_contact_seek_max_total_mm:.3f} mm."
        )
        self._set_contact_seek_result(
            found=False,
            status="not_found",
            attempts=attempts,
            initial_status=initial_quality.status,
            final_status=last_status,
            depth_below_down_mm=last_depth_mm,
            axis_a_lowering_mm=last_axis_a_lowering_mm,
        )
        return samples

    def _set_contact_seek_result(
        self,
        *,
        found: bool,
        status: str,
        attempts: int,
        initial_status: str,
        final_status: str,
        depth_below_down_mm: float,
        axis_a_lowering_mm: float,
    ) -> None:
        self._current_contact_seek_result = RouteContactSeekResult(
            found=bool(found),
            status=str(status),
            attempts=max(0, int(attempts)),
            initial_status=str(initial_status),
            final_status=str(final_status),
            depth_below_down_mm=float(depth_below_down_mm),
            axis_a_lowering_mm=float(axis_a_lowering_mm),
            step_mm=abs(float(self._auto_contact_seek_step_mm)),
            max_depth_mm=float(self._auto_contact_seek_max_total_mm),
        )

    def _latest_axis_a_lowering(self) -> float:
        getter = getattr(self._stage_controller, "latest_axis_a_lowering", None)
        if not callable(getter):
            return math.nan
        try:
            value = float(getter())
        except (TypeError, ValueError):
            return math.nan
        return value if math.isfinite(value) else math.nan

    def _contact_height_record_for_point(
        self,
        *,
        point: RouteMeasurementPoint,
        record: RouteMeasurementRecord,
    ) -> RouteContactHeightRecord:
        contact_seek = self._current_contact_seek_result
        axis_a_lowering_mm = self._latest_axis_a_lowering()
        depth_below_down_mm = 0.0
        if contact_seek is not None:
            depth_below_down_mm = contact_seek.depth_below_down_mm
            if not math.isfinite(axis_a_lowering_mm):
                axis_a_lowering_mm = contact_seek.axis_a_lowering_mm
        return RouteContactHeightRecord(
            timestamp=record.timestamp,
            structure_number=record.structure_number,
            point_index=int(point.index),
            point_id=point.point_id,
            label=point.label,
            design_center=point.design_center,
            stage_xy=point.stage_xy,
            measurement_status=record.status,
            resistance_ohm=record.resistance_ohm,
            resistance_rms_ohm=record.resistance_rms_ohm,
            relative_rms=record.relative_rms,
            contact_quality=record.contact_quality,
            contact_found=self._measurement_record_has_contact(record),
            contact_depth_below_down_mm=depth_below_down_mm,
            contact_axis_a_lowering_mm=axis_a_lowering_mm,
            contact_seek=contact_seek,
        )

    @staticmethod
    def _measurement_record_has_contact(record: RouteMeasurementRecord) -> bool:
        if record.status in {"ok", "short"}:
            return True
        contact = record.contact_quality
        return contact is not None and contact.good is True

    def _contact_status_for_samples(
        self,
        samples: list[RouteMeasurementSample] | tuple[RouteMeasurementSample, ...],
    ) -> str:
        if self._samples_are_short(samples):
            return "short"
        return self._contact_quality_from_samples(samples).status

    def _completed_measurement_is_acceptable(
        self,
        samples: list[RouteMeasurementSample],
    ) -> bool:
        if self._samples_are_short(samples):
            return True
        if self._samples_have_bad_contact(samples):
            return False
        return not self._samples_exceed_relative_rms_limit(samples)

    def _samples_exceed_relative_rms_limit(
        self,
        samples: list[RouteMeasurementSample],
    ) -> bool:
        if self._max_relative_rms is None:
            return False
        relative_rms = self._relative_rms_from_samples(samples)
        return (
            math.isfinite(relative_rms)
            and relative_rms > self._max_relative_rms
        )

    @staticmethod
    def _relative_rms_from_samples(samples: list[RouteMeasurementSample]) -> float:
        resistances_ohm = [
            sample.differential_resistance_ohm for sample in samples
        ]
        finite_resistances = [
            float(value) for value in resistances_ohm if math.isfinite(value)
        ]
        if len(finite_resistances) != len(resistances_ohm) or not finite_resistances:
            return math.nan
        mean_resistance = sum(finite_resistances) / len(finite_resistances)
        if mean_resistance == 0:
            return math.nan
        variance = sum(
            (value - mean_resistance) ** 2 for value in finite_resistances
        ) / len(finite_resistances)
        return math.sqrt(variance) / abs(mean_resistance)

    def _initial_measurement_count(self) -> int:
        return min(self._measurement_count, self._initial_measurement_count_value)

    def _read_measurement_samples(
        self,
        count: int,
        *,
        start_index: int,
        prepare_task: _BackgroundRouteTask | None = None,
        after_measurement: Callable[[], object] | None = None,
    ) -> list[RouteMeasurementSample] | None:
        count = max(0, int(count))
        if count <= 0:
            return []
        if prepare_task is not None:
            prepare_task.wait()
        batch_reader = getattr(
            self._lcr_controller,
            "read_route_measurement_batch_now",
            None,
        )
        if callable(batch_reader) and count > 1:
            if (
                self._stop_requested.is_set()
                or self._point_interrupt_requested.is_set()
            ):
                return None
            if (
                after_measurement is not None
                and _callable_accepts_keyword(batch_reader, "after_measurement")
            ):
                raw_batch = list(
                    batch_reader(count, after_measurement=after_measurement)
                )
            else:
                raw_batch = list(batch_reader(count))
            samples = [
                _measurement_sample_from_raw(raw, index)
                for index, raw in enumerate(raw_batch, start=start_index)
            ]
            if (
                self._stop_requested.is_set()
                or self._point_interrupt_requested.is_set()
            ):
                return None
            return samples
        samples: list[RouteMeasurementSample] = []
        for index in range(start_index, start_index + count):
            if (
                self._stop_requested.is_set()
                or self._point_interrupt_requested.is_set()
            ):
                return None
            samples.append(self._read_measurement_sample(index))
            if (
                self._stop_requested.is_set()
                or self._point_interrupt_requested.is_set()
            ):
                return None
        return samples

    def _read_measurement_sample(self, sample_index: int) -> RouteMeasurementSample:
        reader = getattr(self._lcr_controller, "read_route_measurement_now", None)
        if callable(reader):
            raw = reader()
        else:
            raw = self._lcr_controller.read_primary_value_now()
        return _measurement_sample_from_raw(raw, sample_index)

    def _wait_before_first_route_point(
        self,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> str:
        self._consume_pause_request()
        with self._confirmation_condition:
            self._pending_confirmation = None
        with self._route_offset_lock:
            self._last_recorded_point = point
        self._finish_stage_task()
        self._emit_progress(position, total, int(point.index))
        self._set_waiting(True)
        self._status(
            f"Route measurement ready: point {position}/{total} {point.label}."
        )
        decision = self._wait_for_valid_confirmation()
        self._set_waiting(False)
        return decision

    def _wait_after_interrupted_point(
        self,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> str:
        self._consume_pause_request()
        with self._confirmation_condition:
            self._pending_confirmation = None
        with self._route_offset_lock:
            self._last_recorded_point = point
        self._finish_stage_task()
        self._set_waiting(True)
        self._status(
            f"Route measurement: point {position}/{total} interrupted; "
            "correct position, then Measure or Skip."
        )
        decision = self._wait_for_valid_confirmation()
        self._set_waiting(False)
        return decision

    def _wait_after_rejected_result(
        self,
        *,
        point: RouteMeasurementPoint,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        emit_result: bool = True,
    ) -> str:
        with self._confirmation_condition:
            self._pending_confirmation = None
        with self._route_offset_lock:
            self._last_recorded_point = point
        self._finish_stage_task()
        self._set_waiting(True)
        if emit_result:
            self._emit_result(record, position, total, False)
        contact_quality = record.contact_quality
        if contact_quality is not None and contact_quality.good is False:
            self._status(
                f"Route measurement: point {position}/{total} contact check failed "
                f"({contact_quality.status}"
                f"{self._contact_quality_failure_suffix(contact_quality)}); "
                "correct contact, then Measure, "
                "Remeasure, or Skip."
            )
        else:
            self._status(
                f"Route measurement: point {position}/{total} relative RMS "
                f"{_format_percent(record.relative_rms)} exceeds "
                f"{_format_percent(self._max_relative_rms or math.nan)}; "
                "correct contact, then Measure, Remeasure, or Skip."
            )
        decision = self._wait_for_valid_confirmation()
        self._set_waiting(False)
        return decision

    def _wait_for_valid_confirmation(self) -> str:
        while True:
            decision = self._wait_for_confirmation()
            if not decision.startswith("jump:"):
                return decision
            if self._jump_target_index(decision) is not None:
                return decision
            point_number = decision.split(":", 1)[1]
            self._status(
                f"Route measurement: point {point_number} is not enabled or not found."
            )
            self._set_waiting(True)

    def _wait_for_confirmation(self) -> str:
        with self._confirmation_condition:
            while not self._stop_requested.is_set():
                if self._pending_confirmation is not None:
                    decision = self._pending_confirmation
                    self._pending_confirmation = None
                    return decision
                self._confirmation_condition.wait(timeout=0.2)
        return "stop"

    def _jump_target_index(self, decision: str) -> int | None:
        if not decision.startswith("jump:"):
            return None
        try:
            point_number = int(decision.split(":", 1)[1])
        except ValueError:
            return None
        return self._index_for_point_number(point_number)

    def _index_for_point_number(self, point_number: int) -> int | None:
        try:
            target = int(point_number)
        except (TypeError, ValueError):
            return None
        for index, point in enumerate(self._points):
            if int(point.index) == target:
                return index
        for index, point in enumerate(self._points):
            if _structure_number_for_point(point) == target:
                return index
        return None

    def _record_for_point(
        self,
        *,
        point: RouteMeasurementPoint,
        samples: list[RouteMeasurementSample],
    ) -> RouteMeasurementRecord:
        resistances_ohm = [
            sample.differential_resistance_ohm for sample in samples
        ]
        finite_resistances = [
            float(value) for value in resistances_ohm if math.isfinite(value)
        ]
        complete_finite_batch = len(finite_resistances) == len(resistances_ohm)
        if complete_finite_batch and finite_resistances:
            mean_resistance = sum(finite_resistances) / len(finite_resistances)
            variance = sum(
                (value - mean_resistance) ** 2 for value in finite_resistances
            ) / len(finite_resistances)
            rms_resistance = math.sqrt(variance)
            relative_rms = (
                rms_resistance / abs(mean_resistance)
                if mean_resistance
                else math.nan
            )
            contact_quality = self._contact_quality_from_samples(samples)
            if self._samples_are_short(samples):
                status = "short"
            elif contact_quality.good is False:
                status = "bad_contact"
            else:
                status = "ok"
        else:
            mean_resistance = math.inf
            rms_resistance = math.nan
            relative_rms = math.nan
            status = "overload"
            contact_quality = None
        return RouteMeasurementRecord(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            structure_number=_structure_number_for_point(point),
            nplc=self._nplc_label,
            measurement_type=self._measurement_type,
            n_measurements=len(resistances_ohm),
            resistance_ohm=mean_resistance,
            resistance_rms_ohm=rms_resistance,
            relative_rms=relative_rms,
            status=status,
            contact_quality=contact_quality,
            raw_samples=tuple(samples),
        )

    def _record_exceeds_quality_limit(self, record: RouteMeasurementRecord) -> bool:
        if record.status == "short":
            return False
        contact_quality = record.contact_quality
        if contact_quality is not None and contact_quality.good is False:
            return True
        if self._max_relative_rms is None or record.status != "ok":
            return False
        return (
            math.isfinite(record.relative_rms)
            and record.relative_rms > self._max_relative_rms
        )

    @staticmethod
    def _contact_placement_record_is_success(record: RouteMeasurementRecord) -> bool:
        return str(record.status).strip().lower() in {"ok", "short"}

    def _should_save_exhausted_bad_contact(
        self,
        record: RouteMeasurementRecord,
    ) -> bool:
        contact_quality = record.contact_quality
        if contact_quality is None or contact_quality.good is not False:
            return False
        contact_seek = self._current_contact_seek_result
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

    def _samples_are_short(
        self,
        samples: list[RouteMeasurementSample] | tuple[RouteMeasurementSample, ...],
    ) -> bool:
        return any(sample.compliance_hit for sample in samples)

    def _contact_quality_from_samples(
        self,
        samples: list[RouteMeasurementSample] | tuple[RouteMeasurementSample, ...],
    ) -> RouteContactQuality:
        return _contact_quality_from_samples(
            samples,
            contact_quality_limits=self._contact_quality_limits,
        )

    def _contact_quality_failure_suffix(self, quality: RouteContactQuality) -> str:
        detail = _format_contact_quality_failure(
            quality,
            self._contact_quality_limits,
        )
        return f", failed criterion: {detail}" if detail else ""

    def _samples_have_bad_contact(
        self,
        samples: list[RouteMeasurementSample] | tuple[RouteMeasurementSample, ...],
    ) -> bool:
        return self._contact_quality_from_samples(samples).good is False


class RouteExternalMeasurementSessionRunner:
    """Run route contacts for API-owned external measurements.

    The GUI-visible route state machine remains the owner of movement, contact
    checks, contact seek, pause, interrupt, and Telegram callbacks. The external
    client only owns the final experiment-specific measurement and storage.
    """

    def __init__(
        self,
        *,
        session_id: str,
        points: list[RouteMeasurementPoint],
        stage_controller: Any,
        lcr_controller: Any,
        needle_feedrate: float | None,
        measurement_count: int,
        initial_measurement_count: int,
        start_point_number: int = 1,
        max_relative_rms: float | None = None,
        contact_quality_limits: RouteContactQualityLimits | None = None,
        auto_contact_seek_step_mm: float = RouteMeasurementRunner.AUTO_CONTACT_SEEK_STEP_MM,
        auto_contact_seek_max_total_mm: float = RouteMeasurementRunner.AUTO_CONTACT_SEEK_MAX_TOTAL_MM,
        contact_settle_s: float = RouteMeasurementRunner.DEFAULT_CONTACT_SETTLE_S,
        nplc_label: str = "",
        measurement_type: str = "",
        status_callback: Callable[[str], None] | None = None,
        progress_callback: Callable[[int, int, int], None] | None = None,
        photo_callback: Callable[
            [RouteMeasurementPoint, int, int, object | None],
            str | Path,
        ]
        | None = None,
        photo_focus_callback: Callable[[RouteMeasurementPoint, int, int], object | None]
        | None = None,
        photo_record_callback: Callable[[RoutePhotoRecord, int, int], None]
        | None = None,
        contact_photo_callback: Callable[
            [RouteMeasurementPoint, RouteMeasurementRecord, int, int, bool],
            None,
        ]
        | None = None,
        pre_contact_photo_callback: Callable[
            [RouteMeasurementPoint, int, int],
            None,
        ]
        | None = None,
        result_callback: Callable[[RouteMeasurementRecord, int, int, bool], None]
        | None = None,
        waiting_callback: Callable[[bool], None] | None = None,
        photo_enabled: bool = True,
        photo_focus_enabled: bool = True,
        photo_settle_s: float = 0.2,
        wait_before_first_point: bool = False,
    ) -> None:
        self.session_id = str(session_id)
        self._points = list(points)
        self._csv_path = Path(os.devnull)
        self._status_callback = status_callback
        self._progress_callback = progress_callback
        self._result_callback = result_callback
        self._waiting_callback = waiting_callback
        self._photo_enabled = bool(photo_enabled)
        self._photo_focus_enabled = bool(photo_focus_enabled)
        self._wait_before_first_point = bool(wait_before_first_point)
        self._condition = threading.Condition()
        self._stop_requested = False
        self._pause_requested = False
        self._pending_action: str | None = None
        self._pending_external_result: dict[str, Any] | None = None
        self._external_measurement_request_id = 0
        self._state = "idle"
        self._waiting_reason = ""
        self._message = "Route API session idle."
        self._position = 0
        self._total = len(self._points)
        self._current_point: RouteMeasurementPoint | None = None
        self._last_preparation: RouteExternalContactPreparation | None = None
        self._last_external_result: dict[str, Any] | None = None
        self._history: list[dict[str, Any]] = []
        self._contact_runner = RouteMeasurementRunner(
            points=self._points,
            csv_path=self._csv_path,
            stage_controller=stage_controller,
            lcr_controller=lcr_controller,
            needle_feedrate=needle_feedrate,
            measurement_count=measurement_count,
            initial_measurement_count=initial_measurement_count,
            start_point_number=start_point_number,
            max_relative_rms=max_relative_rms,
            contact_quality_limits=contact_quality_limits,
            confirm_each_point=False,
            auto_contact_seek_on_bad_contact=True,
            auto_contact_seek_step_mm=auto_contact_seek_step_mm,
            auto_contact_seek_max_total_mm=auto_contact_seek_max_total_mm,
            contact_settle_s=contact_settle_s,
            nplc_label=nplc_label,
            measurement_type=measurement_type,
            status_callback=status_callback,
            progress_callback=progress_callback,
            photo_callback=photo_callback,
            photo_focus_callback=photo_focus_callback,
            photo_record_callback=photo_record_callback,
            contact_photo_callback=contact_photo_callback,
            pre_contact_photo_callback=pre_contact_photo_callback,
            result_callback=result_callback,
            waiting_callback=waiting_callback,
            operation_mode=ROUTE_OPERATION_PHOTO_THEN_MEASURE
            if self._photo_enabled
            else ROUTE_OPERATION_MEASURE,
            photo_settle_s=photo_settle_s,
            photo_focus_enabled=self._photo_focus_enabled,
        )

    @property
    def csv_path(self) -> Path:
        return self._csv_path

    def route_offset_xy(self) -> Point2D:
        return self._contact_runner.route_offset_xy()

    def set_route_offset_xy(self, offset_xy: Point2D) -> None:
        self._contact_runner.set_route_offset_xy(offset_xy)

    def stop(self) -> None:
        with self._condition:
            self._stop_requested = True
            self._condition.notify_all()
        self._contact_runner.stop()

    def request_pause_after_current_point(self) -> None:
        with self._condition:
            self._pause_requested = True
            self._message = (
                "Route API session pause requested; will pause after current contact."
            )
            self._condition.notify_all()

    def request_current_point_correction(self) -> None:
        self._contact_runner.request_current_point_correction()
        with self._condition:
            if self._state.startswith("waiting"):
                self._pending_action = "interrupt"
            self._condition.notify_all()

    def submit_confirmation(self, action: str) -> bool:
        normalized = self._normalize_action(action)
        if normalized is None:
            return False
        with self._condition:
            self._pending_action = normalized
            self._condition.notify_all()
        return True

    def submit_external_result(self, result: dict[str, Any]) -> bool:
        with self._condition:
            if self._state != "waiting_external_measurement":
                return False
            request_id = result.get("external_measurement_request_id")
            if request_id is None:
                request_id = result.get("request_id")
            if request_id is not None and str(request_id) != str(
                self._external_measurement_request_id
            ):
                return False
            self._pending_external_result = dict(result)
            self._condition.notify_all()
        return True

    def request_contact_seek(self) -> bool:
        with self._condition:
            if not self._state.startswith("waiting"):
                return False
            self._pending_action = "seek"
            self._condition.notify_all()
        return True

    def set_current_adjustment_point(self, point_number: int) -> tuple[bool, str]:
        return self._contact_runner.set_current_adjustment_point(point_number)

    def save_current_position_adjustment(
        self,
        current_stage_xy: Point2D,
    ) -> tuple[bool, str]:
        return self._contact_runner.save_current_position_adjustment(
            current_stage_xy
        )

    def update_runtime_settings(
        self,
        *,
        measurement_count: int,
        initial_measurement_count: int,
        max_relative_rms: float | None,
        auto_contact_seek_step_mm: float,
        auto_contact_seek_max_total_mm: float,
        contact_settle_s: float,
        contact_quality_limits: RouteContactQualityLimits | None = None,
        photo_settle_s: float | None = None,
    ) -> None:
        self._contact_runner.update_runtime_settings(
            measurement_count=measurement_count,
            initial_measurement_count=initial_measurement_count,
            max_relative_rms=max_relative_rms,
            auto_contact_seek_step_mm=auto_contact_seek_step_mm,
            auto_contact_seek_max_total_mm=auto_contact_seek_max_total_mm,
            contact_settle_s=contact_settle_s,
            contact_quality_limits=contact_quality_limits,
            photo_settle_s=photo_settle_s,
        )

    def status_payload(self) -> dict[str, Any]:
        with self._condition:
            return {
                "accepted": True,
                "session_id": self.session_id,
                "state": self._state,
                "waiting": self._state.startswith("waiting"),
                "waiting_reason": self._waiting_reason,
                "message": self._message,
                "position": self._position,
                "total": self._total,
                "external_measurement_request_id": self._external_measurement_request_id,
                "contact_quality_limits": (
                    self._contact_runner.contact_quality_limits().as_dict()
                ),
                "current_contact": self._point_payload(self._current_point),
                "last_preparation": self._preparation_payload(
                    self._last_preparation
                ),
                "last_external_result": self._last_external_result,
                "history": list(self._history),
            }

    def wait_until_initial_pause(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._condition:
            while True:
                if (
                    self._state == "waiting_paused"
                    and self._waiting_reason == "paused"
                ):
                    return True
                if self._state in {"complete", "stopped", "failed"}:
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._condition.wait(timeout=min(0.05, remaining))

    def run(self) -> tuple[bool, str]:
        success = False
        message = "Route API session stopped."
        try:
            if not self._points:
                raise ValueError("Route has no enabled points.")
            lcr = self._contact_runner._lcr_controller
            if hasattr(lcr, "open"):
                self._status("Route API session: connecting meter.")
                lcr.open()
            self._set_state("running", message="Route API session starting.")
            index = self._start_index()
            if self._wait_before_first_point and index < len(self._points):
                point = self._points[index]
                position = index + 1
                self._set_current(position, point)
                self._emit_progress(position, len(self._points), int(point.index))
                decision = self._wait_before_first_point_decision(point, position)
                action = str(decision.get("action") or "next")
                if action == "stop":
                    self.stop()
                    message = "Route API session stopped by user."
                    index = len(self._points)
                else:
                    jump = self._jump_index(action)
                    if jump is not None:
                        index = jump
                    elif action == "skip":
                        index += 1
            while index < len(self._points):
                if self._stop_requested_now():
                    message = "Route API session stopped by user."
                    break
                point = self._points[index]
                position = index + 1
                self._set_current(position, point)
                self._emit_progress(position, len(self._points), int(point.index))
                try:
                    preparation = self._prepare_point(point, position, len(self._points))
                except RuntimeError:
                    if not self._point_interrupted():
                        raise
                    decision = self._wait_for_interrupted_point(point, position)
                    jump = self._handle_interrupted_decision(
                        decision,
                        position,
                        len(self._points),
                    )
                    index = position - 1 if jump is None else jump
                    continue
                record = preparation.placement.record
                self._store_preparation(preparation)
                self._emit_result(record, position, len(self._points), record.status in {"ok", "short"})
                if record.status == "short":
                    self._append_history(
                        point,
                        position,
                        "short",
                        preparation=preparation,
                        external_result=None,
                    )
                    self._status(
                        f"Route API session: point {position}/{len(self._points)} "
                        "short-circuit detected; external measurement skipped."
                    )
                    self._lift_needles(position, len(self._points))
                    pause_decision = self._wait_if_pause_requested(point, position)
                    jump = self._handle_post_point_decision(pause_decision)
                    if jump is not None:
                        index = jump
                        continue
                    index += 1
                    continue
                if record.status != "ok":
                    decision = self._wait_for_contact_attention(point, position)
                    jump = self._handle_attention_decision(
                        decision,
                        point,
                        position,
                        len(self._points),
                    )
                    if jump is None:
                        index += 1
                    else:
                        index = jump
                    continue
                decision = self._wait_for_external_result(point, position)
                if "result" in decision:
                    jump = self._handle_external_result(
                        dict(decision["result"]),
                        point,
                        position,
                        len(self._points),
                        preparation=preparation,
                    )
                    if jump is not None:
                        index = jump
                        continue
                    index += 1
                    continue
                jump = self._handle_attention_decision(
                    decision,
                    point,
                    position,
                    len(self._points),
                )
                if jump is None:
                    index += 1
                else:
                    index = jump
            if index >= len(self._points) and not self._stop_requested_now():
                success = True
                message = "Route API session complete."
        except Exception as exc:
            message = str(exc)
            self._status(f"Route API session failed: {message}")
        finally:
            try:
                lcr = self._contact_runner._lcr_controller
                if hasattr(lcr, "close"):
                    lcr.close()
            except Exception as exc:
                message = f"{message} Instrument close failed: {exc}"
            self._set_waiting(False)
            self._set_state(
                "complete" if success else "stopped",
                waiting_reason="",
                message=message,
            )
        return success, message

    @staticmethod
    def _normalize_action(action: str) -> str | None:
        normalized = str(action or "").strip().lower()
        if normalized in {"resume", "next", "continue"}:
            return "next"
        if normalized == "measure":
            return "measure"
        if normalized == "remeasure":
            return "remeasure"
        if normalized in {"skip", "stop", "interrupt", "seek"}:
            return normalized
        if normalized.isdigit():
            return f"jump:{int(normalized)}"
        if normalized.startswith("jump:"):
            try:
                return f"jump:{int(normalized.split(':', 1)[1].strip())}"
            except ValueError:
                return None
        return None

    def _start_index(self) -> int:
        start = self._contact_runner._start_point_number
        index = self._contact_runner._index_for_point_number(start)
        return 0 if index is None else index

    def _prepare_point(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> RouteExternalContactPreparation:
        self._set_state(
            "running",
            waiting_reason="",
            message=f"Route API session: preparing point {position}/{total}.",
        )
        return self._contact_runner.prepare_external_contact(
            point,
            position=position,
            total=total,
            photo_enabled=self._photo_enabled,
            photo_focus_enabled=self._photo_focus_enabled,
            lift_on_failure=False,
        )

    def _wait_for_contact_attention(
        self,
        point: RouteMeasurementPoint,
        position: int,
    ) -> dict[str, Any]:
        message = (
            f"Route API session: point {position}/{self._total} "
            f"{point.label} needs contact action."
        )
        self._status(message)
        return self._wait_for_decision(
            state="waiting_contact",
            reason="contact",
            message=message,
            allow_external_result=False,
        )

    def _wait_for_interrupted_point(
        self,
        point: RouteMeasurementPoint,
        position: int,
    ) -> dict[str, Any]:
        message = (
            f"Route API session: point {position}/{self._total} "
            f"{point.label} interrupted."
        )
        self._status(message)
        return self._wait_for_decision(
            state="waiting_interrupted",
            reason="interrupted",
            message=message,
            allow_external_result=False,
        )

    def _wait_for_external_result(
        self,
        point: RouteMeasurementPoint,
        position: int,
    ) -> dict[str, Any]:
        self._begin_external_measurement_request()
        message = (
            f"Route API session: point {position}/{self._total} "
            f"{point.label} waiting for external measurement."
        )
        self._status(message)
        return self._wait_for_decision(
            state="waiting_external_measurement",
            reason="external_measurement",
            message=message,
            allow_external_result=True,
        )

    def _begin_external_measurement_request(self) -> int:
        with self._condition:
            self._external_measurement_request_id += 1
            self._pending_external_result = None
            self._condition.notify_all()
            return self._external_measurement_request_id

    def _wait_before_first_point_decision(
        self,
        point: RouteMeasurementPoint,
        position: int,
    ) -> dict[str, Any]:
        message = (
            f"Route API session: ready at point {position}/{self._total} "
            f"{point.label}."
        )
        self._status(message)
        return self._wait_for_decision(
            state="waiting_paused",
            reason="paused",
            message=message,
            allow_external_result=False,
        )

    def _wait_if_pause_requested(
        self,
        point: RouteMeasurementPoint,
        position: int,
    ) -> dict[str, Any]:
        with self._condition:
            requested = self._pause_requested
            self._pause_requested = False
        if not requested:
            return {"action": "next"}
        message = (
            f"Route API session: paused after point {position}/{self._total} "
            f"{point.label}."
        )
        self._status(message)
        return self._wait_for_decision(
            state="waiting_paused",
            reason="paused",
            message=message,
            allow_external_result=False,
        )

    def _wait_for_decision(
        self,
        *,
        state: str,
        reason: str,
        message: str,
        allow_external_result: bool,
    ) -> dict[str, Any]:
        self._set_state(state, waiting_reason=reason, message=message)
        self._set_waiting(True)
        try:
            with self._condition:
                while True:
                    if self._stop_requested:
                        return {"action": "stop"}
                    if allow_external_result and self._pending_external_result is not None:
                        result = self._pending_external_result
                        self._pending_external_result = None
                        return {"result": result}
                    if self._pending_action is not None:
                        action = self._pending_action
                        self._pending_action = None
                        return {"action": action}
                    self._condition.wait(timeout=0.2)
        finally:
            self._set_waiting(False)

    def _handle_attention_decision(
        self,
        decision: dict[str, Any],
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> int | None:
        if "result" in decision:
            return self._handle_external_result(
                dict(decision["result"]),
                point,
                position,
                total,
                preparation=self._last_preparation,
            )
        action = str(decision.get("action") or "")
        if action == "stop":
            self.stop()
            return len(self._points)
        if action == "interrupt":
            self._contact_runner.clear_current_point_correction_request()
            self._lift_needles(position, total)
            return position - 1
        if action == "skip":
            self._append_history(
                point,
                position,
                "skipped",
                preparation=self._last_preparation,
                external_result=None,
            )
            self._lift_needles(position, total)
            return None
        if action == "measure":
            followup = self._wait_for_external_result(point, position)
            return self._handle_attention_decision(followup, point, position, total)
        if action == "seek":
            preparation = self._seek_current_contact(point, position, total)
            self._store_preparation(preparation)
            record = preparation.placement.record
            self._emit_result(record, position, total, record.status in {"ok", "short"})
            if record.status == "short":
                self._append_history(
                    point,
                    position,
                    "short",
                    preparation=preparation,
                    external_result=None,
                )
                self._lift_needles(position, total)
                return None
            if record.status == "ok":
                followup = self._wait_for_external_result(point, position)
                return self._handle_attention_decision(followup, point, position, total)
            return self._handle_attention_decision(
                self._wait_for_contact_attention(point, position),
                point,
                position,
                total,
            )
        if action == "remeasure":
            self._lift_needles(position, total)
            return position - 1
        jump = self._jump_index(action)
        if jump is not None:
            self._lift_needles(position, total)
            return jump
        self._lift_needles(position, total)
        return None

    def _handle_external_result(
        self,
        external_result: dict[str, Any],
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        preparation: RouteExternalContactPreparation | None,
    ) -> int | None:
        self._last_external_result = dict(external_result)
        if not self._external_result_succeeded(external_result):
            return self._wait_after_external_result_failed(
                external_result,
                point,
                position,
                total,
            )
        self._append_history(
            point,
            position,
            str(external_result.get("status") or "ok"),
            preparation=preparation,
            external_result=external_result,
        )
        self._lift_needles(position, total)
        pause_decision = self._wait_if_pause_requested(point, position)
        return self._handle_post_point_decision(pause_decision)

    def _wait_after_external_result_failed(
        self,
        external_result: dict[str, Any],
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> int | None:
        message_text = str(external_result.get("message") or "").strip()
        status_text = str(external_result.get("status") or "failed").strip() or "failed"
        detail = f": {message_text}" if message_text else "."
        message = (
            f"Route API session: point {position}/{self._total} "
            f"{point.label} external measurement {status_text}{detail}"
        )
        self._status(message)
        self._begin_external_measurement_request()
        decision = self._wait_for_decision(
            state="waiting_external_measurement",
            reason="external_measurement_failed",
            message=message,
            allow_external_result=True,
        )
        return self._handle_attention_decision(decision, point, position, total)

    @staticmethod
    def _external_result_succeeded(external_result: dict[str, Any]) -> bool:
        status = str(external_result.get("status") or "ok").strip().lower()
        return status in {"", "ok", "success", "complete", "completed"}

    def _handle_interrupted_decision(
        self,
        decision: dict[str, Any],
        position: int,
        total: int,
    ) -> int | None:
        action = str(decision.get("action") or "")
        if action == "stop":
            self.stop()
            return len(self._points)
        if action == "skip":
            self._contact_runner.clear_current_point_correction_request()
            self._lift_needles(position, total)
            return position
        jump = self._jump_index(action)
        if jump is not None:
            self._contact_runner.clear_current_point_correction_request()
            self._lift_needles(position, total)
            return jump
        self._contact_runner.clear_current_point_correction_request()
        self._lift_needles(position, total)
        return None

    def _handle_post_point_decision(self, decision: dict[str, Any]) -> int | None:
        action = str(decision.get("action") or "next")
        if action == "stop":
            self.stop()
            return len(self._points)
        if action == "remeasure":
            return max(0, self._position - 1)
        jump = self._jump_index(action)
        return jump

    def _seek_current_contact(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> RouteExternalContactPreparation:
        placement = self._contact_runner.seek_contact(
            point,
            position=position,
            total=total,
        )
        return RouteExternalContactPreparation(
            placement=placement,
            photo_path=None,
            focus=None,
        )

    def _lift_needles(self, position: int, total: int) -> None:
        try:
            self._contact_runner.lift_needles_after_external_measurement(
                position=position,
                total=total,
            )
        except Exception:
            logger.exception("Route API session failed to lift needles.")
            raise

    def _jump_index(self, action: str) -> int | None:
        if not str(action).startswith("jump:"):
            return None
        try:
            point_number = int(str(action).split(":", 1)[1])
        except ValueError:
            return None
        index = self._contact_runner._index_for_point_number(point_number)
        return index

    def _stop_requested_now(self) -> bool:
        with self._condition:
            return bool(self._stop_requested)

    def _point_interrupted(self) -> bool:
        return bool(self._contact_runner._point_interrupt_requested.is_set())

    def _set_current(self, position: int, point: RouteMeasurementPoint) -> None:
        with self._condition:
            self._position = int(position)
            self._current_point = point
            self._condition.notify_all()

    def _store_preparation(
        self,
        preparation: RouteExternalContactPreparation,
    ) -> None:
        with self._condition:
            self._last_preparation = preparation

    def _append_history(
        self,
        point: RouteMeasurementPoint,
        position: int,
        status: str,
        *,
        preparation: RouteExternalContactPreparation | None,
        external_result: dict[str, Any] | None,
    ) -> None:
        entry = {
            "position": int(position),
            "contact": self._point_payload(point),
            "status": str(status),
            "preparation": self._preparation_payload(preparation),
            "external_result": external_result,
        }
        with self._condition:
            self._history.append(entry)

    def _set_state(
        self,
        state: str,
        *,
        waiting_reason: str | None = None,
        message: str | None = None,
    ) -> None:
        with self._condition:
            self._state = str(state)
            if waiting_reason is not None:
                self._waiting_reason = str(waiting_reason)
            if message is not None:
                self._message = str(message)
            self._condition.notify_all()

    def _set_waiting(self, waiting: bool) -> None:
        if self._waiting_callback is not None:
            self._waiting_callback(bool(waiting))

    def _status(self, message: str) -> None:
        self._set_state(self._state, message=message)
        if self._status_callback is not None:
            self._status_callback(message)

    def _emit_progress(self, position: int, total: int, point_number: int) -> None:
        if self._progress_callback is not None:
            self._progress_callback(position, total, point_number)

    def _emit_result(
        self,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        saved: bool,
    ) -> None:
        if self._result_callback is not None:
            self._result_callback(record, position, total, saved)

    @staticmethod
    def _point_payload(point: RouteMeasurementPoint | None) -> dict[str, Any] | None:
        if point is None:
            return None
        return {
            "contact_number": _structure_number_for_point(point),
            "point_index": int(point.index),
            "point_id": point.point_id,
            "label": point.label,
            "design_center": list(point.design_center),
            "stage_xy": list(point.stage_xy),
        }

    @classmethod
    def _preparation_payload(
        cls,
        preparation: RouteExternalContactPreparation | None,
    ) -> dict[str, Any] | None:
        if preparation is None:
            return None
        placement = preparation.placement
        return {
            "success": bool(placement.success),
            "message": placement.message,
            "contact": cls._point_payload(placement.point),
            "measurement": _route_measurement_record_payload(placement.record),
            "contact_seek": _route_contact_seek_payload(placement.contact_seek),
            "photo_artifact_id": preparation.photo_path,
            "focus": preparation.focus,
        }


def _route_measurement_record_payload(record: RouteMeasurementRecord) -> dict[str, Any]:
    return {
        "timestamp": record.timestamp,
        "structure_number": int(record.structure_number),
        "nplc": record.nplc,
        "measurement_type": record.measurement_type,
        "n_measurements": int(record.n_measurements),
        "resistance_ohm": _json_ready(record.resistance_ohm),
        "resistance_rms_ohm": _json_ready(record.resistance_rms_ohm),
        "relative_rms": _json_ready(record.relative_rms),
        "status": record.status,
        "contact_quality": _route_contact_quality_payload(record.contact_quality),
        "raw_samples": [
            _route_measurement_sample_payload(sample)
            for sample in record.raw_samples
        ],
    }


def _route_measurement_sample_payload(sample: RouteMeasurementSample) -> dict[str, Any]:
    return {
        field: _json_ready(getattr(sample, field))
        for field in (
            "sample_index",
            "differential_resistance_ohm",
            "compliance_hit",
            "negative_source_voltage_v",
            "negative_measured_voltage_v",
            "negative_current_a",
            "negative_resistance_ohm",
            "positive_source_voltage_v",
            "positive_measured_voltage_v",
            "positive_current_a",
            "positive_resistance_ohm",
        )
    }


def _route_contact_quality_payload(
    quality: RouteContactQuality | None,
) -> dict[str, Any] | None:
    if quality is None:
        return None
    return {
        "assessed": bool(quality.assessed),
        "good": quality.good,
        "status": quality.status,
        "median_ohm": _json_ready(quality.median_ohm),
        "mad_sigma_ohm": _json_ready(quality.mad_sigma_ohm),
        "p95_abs_step_ohm": _json_ready(quality.p95_abs_step_ohm),
        "span_ohm": _json_ready(quality.span_ohm),
        "compliance_hits": int(quality.compliance_hits),
        "polarity_sign_mismatch_count": int(
            quality.polarity_sign_mismatch_count
        ),
        "reasons": list(quality.reasons),
        "failure_criteria": list(quality.failure_criteria),
    }


def _route_contact_seek_payload(
    seek: RouteContactSeekResult | None,
) -> dict[str, Any] | None:
    if seek is None:
        return None
    return {
        "found": bool(seek.found),
        "status": seek.status,
        "attempts": int(seek.attempts),
        "initial_status": seek.initial_status,
        "final_status": seek.final_status,
        "depth_below_down_mm": _json_ready(seek.depth_below_down_mm),
        "axis_a_lowering_mm": _json_ready(seek.axis_a_lowering_mm),
        "step_mm": _json_ready(seek.step_mm),
        "max_depth_mm": _json_ready(seek.max_depth_mm),
    }


def _json_ready(value: object) -> object:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


__all__ = [
    "RouteMeasurementCsvWriter",
    "CSV_FIELDS",
    "filter_route_points_by_previous_status",
    "latest_route_measurement_statuses",
    "RouteContactHeightRecord",
    "RouteContactQuality",
    "RouteContactPlacementResult",
    "RouteContactSeekResult",
    "RouteExternalContactPreparation",
    "RouteExternalMeasurementSessionRunner",
    "RouteContactQualityLimits",
    "RouteMeasurementPoint",
    "RoutePhotoRecord",
    "RouteMeasurementRecord",
    "RouteMeasurementSample",
    "RouteMeasurementRunner",
    "ROUTE_OPERATION_MEASURE",
    "ROUTE_OPERATION_PHOTO",
    "ROUTE_OPERATION_PHOTO_THEN_MEASURE",
    "ROUTE_OPERATION_MODES",
    "route_measurement_sample_from_raw",
    "summarize_route_contact_quality",
]


def _normalize_operation_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "measurement": ROUTE_OPERATION_MEASURE,
        "measure_only": ROUTE_OPERATION_MEASURE,
        "photo_only": ROUTE_OPERATION_PHOTO,
        "image": ROUTE_OPERATION_PHOTO,
        "capture": ROUTE_OPERATION_PHOTO,
        "photo_measure": ROUTE_OPERATION_PHOTO_THEN_MEASURE,
        "photo+measure": ROUTE_OPERATION_PHOTO_THEN_MEASURE,
        "photo_then_measure": ROUTE_OPERATION_PHOTO_THEN_MEASURE,
    }
    normalized = aliases.get(text, text)
    if normalized in ROUTE_OPERATION_MODES:
        return normalized
    return ROUTE_OPERATION_MEASURE


def _structure_number_for_point(point: RouteMeasurementPoint) -> int:
    for value in (point.label, point.point_id):
        match = re.search(r"(\d+)\s*$", str(value).strip())
        if match is not None:
            try:
                return int(match.group(1))
            except ValueError:
                pass
    return int(point.index)


def latest_route_measurement_statuses(csv_path: str | Path) -> dict[int, str]:
    """Return the latest measurement status by structure number from a route CSV."""

    statuses: dict[int, str] = {}
    with Path(csv_path).expanduser().open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                structure_number = int(str(row.get("structure_number", "")).strip())
            except (TypeError, ValueError):
                continue
            status = str(row.get("status", "")).strip().lower()
            if status:
                statuses[structure_number] = status
    return statuses


def filter_route_points_by_previous_status(
    points: Collection[RouteMeasurementPoint],
    csv_path: str | Path,
    *,
    allowed_statuses: Collection[str],
) -> list[RouteMeasurementPoint]:
    """Keep only points whose latest CSV status is in allowed_statuses."""

    allowed = {str(status).strip().lower() for status in allowed_statuses}
    if not allowed:
        return []
    statuses = latest_route_measurement_statuses(csv_path)
    return [
        point
        for point in points
        if statuses.get(_structure_number_for_point(point)) in allowed
    ]


def _focus_result_to_dict(result: object | None) -> dict[str, object] | None:
    if result is None:
        return None
    if isinstance(result, dict):
        return dict(result)
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        return dict(data) if isinstance(data, dict) else None
    data: dict[str, object] = {}
    for source_name, target_name in (
        ("objective_name", "objective_name"),
        ("mode", "mode"),
        ("start_z_mm", "focus_start_z_mm"),
        ("best_z_mm", "focus_best_z_mm"),
        ("delta_um", "focus_delta_um"),
        ("best_score", "focus_score"),
        ("sample_count", "focus_sample_count"),
        ("edge_peak", "focus_edge_peak"),
        ("range_mm", "autofocus_range_mm"),
        ("fine_step_mm", "autofocus_fine_step_mm"),
        ("lower_z_mm", "autofocus_lower_z_mm"),
        ("upper_z_mm", "autofocus_upper_z_mm"),
    ):
        if hasattr(result, source_name):
            data[target_name] = getattr(result, source_name)
    return data or None


def _format_percent(value: float) -> str:
    if not math.isfinite(value):
        return "nan%"
    return f"{float(value) * 100.0:.3g}%"


def _format_ohm(value: float) -> str:
    if not math.isfinite(value):
        return "nan Ohm"
    abs_value = abs(value)
    for scale, unit in (
        (1e9, "GOhm"),
        (1e6, "MOhm"),
        (1e3, "kOhm"),
        (1.0, "Ohm"),
        (1e-3, "mOhm"),
        (1e-6, "uOhm"),
    ):
        if abs_value >= scale:
            return f"{value / scale:.3g} {unit}"
    return f"{value:.3g} Ohm"
