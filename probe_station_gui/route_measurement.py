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
from typing import Any, Callable, Collection


Point2D = tuple[float, float]


logger = logging.getLogger(__name__)


CONTACT_MAX_MAD_SIGMA_OHM = 300.0
CONTACT_MAX_P95_ABS_STEP_OHM = 1_000.0
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
class RouteContactQuality:
    """Contact quality metrics calculated from repeated route samples."""

    assessed: bool
    good: bool | None
    status: str
    median_ohm: float = math.nan
    mad_sigma_ohm: float = math.nan
    p95_abs_step_ohm: float = math.nan
    span_ohm: float = math.nan
    compliance_hits: int = 0
    polarity_sign_mismatch_count: int = 0
    reasons: tuple[str, ...] = ()


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


@dataclass(frozen=True)
class RouteMeasurementSample:
    """One repeated raw route measurement, including optional polarity details."""

    sample_index: int
    differential_resistance_ohm: float
    compliance_hit: bool = False
    negative_source_voltage_v: float | None = None
    negative_measured_voltage_v: float | None = None
    negative_current_a: float | None = None
    negative_resistance_ohm: float | None = None
    positive_source_voltage_v: float | None = None
    positive_measured_voltage_v: float | None = None
    positive_current_a: float | None = None
    positive_resistance_ohm: float | None = None


CSV_FIELDS = [
    "timestamp",
    "structure_number",
    "nplc",
    "measurement_type",
    "n_measurements",
    "resistance_ohm",
    "resistance_rms_ohm",
    "relative_rms",
    "status",
    "contact_quality",
    "contact_median_ohm",
    "contact_mad_sigma_ohm",
    "contact_p95_abs_step_ohm",
    "contact_span_ohm",
    "contact_compliance_hits",
    "contact_polarity_sign_mismatches",
]


class RouteMeasurementCsvWriter:
    """Write route measurements after each point so partial runs are preserved."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def write_header(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size > 0:
            return
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            handle.flush()
            os.fsync(handle.fileno())

    def append(self, record: RouteMeasurementRecord) -> None:
        self.write_header()
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writerow(_record_to_csv_row(record))
            handle.flush()
            os.fsync(handle.fileno())


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

    @property
    def csv_path(self) -> Path:
        return self._csv_writer.path

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
        elif normalized not in {"next", "remeasure", "skip"}:
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
        photo_settle_s: float | None = None,
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
        self._auto_contact_seek_step_mm = self._normalized_contact_seek_step(
            auto_contact_seek_step_mm
        )
        self._auto_contact_seek_max_total_mm = self._normalized_contact_seek_limit(
            auto_contact_seek_max_total_mm
        )
        self._contact_settle_s = max(0.0, float(contact_settle_s))
        if photo_settle_s is not None:
            self._photo_settle_s = max(0.0, float(photo_settle_s))

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
    ) -> RouteContactPlacementResult:
        """Move to a route point and leave verified contact under the needles.

        This intentionally reuses the same short contact check and automatic
        lift/lower retry path as route measurements, but does not write a CSV row.
        """

        self._point_interrupt_requested.clear()
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
            if move_to_point:
                target_xy = self._adjusted_stage_xy(point)
                self._status(
                    f"Route contact: point {position}/{total} moving."
                )
                self._stage_controller.run_external_move_to_xy(
                    target_xy[0],
                    target_xy[1],
                )
            prepare_task = self._start_measurement_prepare_task(
                self._initial_measurement_count()
            )
            self._status(
                f"Route contact: point {position}/{total} lowering needles."
            )
            self._stage_controller.run_external_needles_action(
                "lower",
                self._needle_feedrate,
            )
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
            status = record.status or "unknown"
            if success:
                message = (
                    f"Contact ready: point {position}/{total} {point.label}, "
                    f"{status}."
                )
                placement_succeeded = True
            else:
                message = (
                    f"Contact check failed: point {position}/{total} "
                    f"{point.label}, {status}."
                )
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
                except Exception:
                    logger.exception("Failed to lift needles after contact check.")
            if prepare_task is not None:
                prepare_task.wait()
            self._wait_for_background_tasks()
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
            if auto_contact_seek and seek is not None:
                message = (
                    f"{action_label}: point {position}/{total} {point.label}, "
                    f"{seek.status}, final={status}."
                )
            elif success:
                message = (
                    f"{action_label}: point {position}/{total} {point.label}, "
                    f"{status}."
                )
            else:
                message = (
                    f"{action_label} failed: point {position}/{total} "
                    f"{point.label}, {status}."
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
            if prepare_task is not None:
                prepare_task.wait()
            self._wait_for_background_tasks()
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
            if self._measure_enabled:
                self._csv_writer.write_header()
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
                    self._stage_controller.run_external_needles_action(
                        "raise",
                        self._needle_feedrate,
                    )
                    if self._stop_requested.is_set():
                        message = "Route measurement stopped by user."
                        break
                target_xy = (
                    self._adjusted_photo_stage_xy(point)
                    if self._photo_enabled or self._photo_focus_enabled
                    else self._adjusted_stage_xy(point)
                )
                self._stage_controller.run_external_move_to_xy(
                    target_xy[0],
                    target_xy[1],
                )
                if self._stop_requested.is_set():
                    message = "Route measurement stopped by user."
                    break
                measurement_prepare_task = (
                    self._start_measurement_prepare_task(
                        self._initial_measurement_count()
                    )
                    if self._measure_enabled
                    else None
                )
                focus_result: object | None = None
                if self._photo_focus_enabled:
                    self._status(
                        f"Route measurement: point {position}/{total} "
                        "local autofocus."
                    )
                    focus_result = self._run_photo_focus(point, position, total)
                    focus_message = str(focus_result or "")
                    if focus_message.strip():
                        self._status(
                            f"Route measurement: point {position}/{total} "
                            f"{focus_message}"
                        )
                    if self._stop_requested.is_set():
                        message = "Route measurement stopped by user."
                        break
                if self._photo_enabled:
                    if not self._sleep_photo_settle():
                        message = "Route measurement stopped by user."
                        break
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
                if self._measure_enabled:
                    contact_xy = self._adjusted_stage_xy(point)
                    if not self._same_stage_xy(target_xy, contact_xy):
                        self._status(
                            f"Route measurement: point {position}/{total} "
                            "moving to contact position."
                        )
                        self._stage_controller.run_external_move_to_xy(
                            contact_xy[0],
                            contact_xy[1],
                        )
                        if self._stop_requested.is_set():
                            message = "Route measurement stopped by user."
                            break
                if not self._measure_enabled:
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
                point_interrupted = self._point_interrupt_requested.is_set()
                try:
                    if not point_interrupted:
                        needs_final_lift = True
                        self._emit_pre_contact_photo(point, position, total)
                        self._stage_controller.run_external_needles_action(
                            "lower",
                            self._needle_feedrate,
                        )
                        needles_lowered = True
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
                finally:
                    if lift_task is not None and needles_lowered:
                        try:
                            lift_task.wait()
                            needles_lowered = False
                            needs_final_lift = False
                        except Exception:
                            logger.warning(
                                "Background route needle lift failed; retrying.",
                                exc_info=True,
                            )
                    if needles_lowered:
                        self._stage_controller.run_external_needles_action(
                            "lift",
                            self._needle_feedrate,
                        )
                        needs_final_lift = False
                    if measurement_prepare_task is not None:
                        measurement_prepare_task.wait()
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
        return self._photo_focus_callback(point, position, total)

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
        for task in tasks:
            task.wait()

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
        needle_action = getattr(self._stage_controller, "run_external_needles_action", None)
        if not callable(needle_action):
            return initial_samples
        lower_to_depth = getattr(
            self._stage_controller,
            "run_external_needles_lower_to_depth_below_down",
            None,
        )
        adjust = getattr(self._stage_controller, "run_external_needles_adjust", None)
        if not callable(lower_to_depth) and not callable(adjust):
            return initial_samples
        initial_quality = _contact_quality_from_samples(initial_samples)
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
                f"MAD={_format_ohm(initial_quality.mad_sigma_ohm)}; "
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
                skip_current_depth=skip_current_depth,
                needle_action=needle_action,
                lower_to_depth=lower_to_depth,
                adjust=adjust,
                after_measurement=after_measurement,
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
        skip_current_depth: bool,
        needle_action: Callable[..., object],
        lower_to_depth: Callable[..., object] | None,
        adjust: Callable[..., object],
        after_measurement: Callable[[], object] | None,
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
        if not skip_current_depth:
            depths_mm.insert(0, 0.0)
        attempts = 0
        last_depth_mm = math.nan
        last_axis_a_lowering_mm = math.nan
        last_status = initial_quality.status
        for depth_mm in depths_mm:
            if (
                self._stop_requested.is_set()
                or self._point_interrupt_requested.is_set()
            ):
                return None
            if depth_mm <= 0.0:
                self._status(
                    f"Route measurement: point {position}/{total} "
                    "retrying lift/lower."
                )
            else:
                attempt_number = max(1, int(math.ceil(depth_mm / abs(step_mm))))
                self._status(
                    f"Route measurement: point {position}/{total} "
                    f"lift/lower retry {attempt_number}/{max_depth_steps}, "
                    f"{depth_mm:.4f} mm below down."
                )
            prepare_task = self._start_measurement_prepare_task(
                self._initial_measurement_count()
            )
            needle_action("lift", self._needle_feedrate)
            if (
                self._stop_requested.is_set()
                or self._point_interrupt_requested.is_set()
            ):
                return None
            if depth_mm > 0.0 and callable(lower_to_depth):
                lower_to_depth(depth_mm, self._needle_feedrate)
            else:
                needle_action("lower", self._needle_feedrate)
                if depth_mm > 0.0:
                    adjust(math.copysign(depth_mm, step_mm), self._needle_feedrate)
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
            initial_after_measurement = (
                after_measurement
                if self._measurement_count <= self._initial_measurement_count()
                else None
            )
            samples = self._read_measurement_samples(
                self._initial_measurement_count(),
                start_index=1,
                prepare_task=prepare_task,
                after_measurement=initial_after_measurement,
            )
            if samples is None:
                return None
            depth_label = (
                "after lift/lower"
                if depth_mm <= 0.0
                else f"{depth_mm:.4f} mm below down"
            )
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
            quality = _contact_quality_from_samples(samples)
            last_status = quality.status
            self._status(
                f"Route measurement: point {position}/{total} "
                f"{depth_label}, {quality.status}, "
                f"median={_format_ohm(quality.median_ohm)}, "
                f"MAD={_format_ohm(quality.mad_sigma_ohm)}."
            )
            if quality.good is not False:
                completed_samples = self._complete_measurement_samples(
                    samples,
                    after_measurement=after_measurement,
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
                    full_quality = _contact_quality_from_samples(completed_samples)
                    last_status = full_quality.status
                    self._status(
                        f"Route measurement: point {position}/{total} full "
                        f"measurement at {depth_label} failed contact check "
                        f"({full_quality.status}, "
                        f"median={_format_ohm(full_quality.median_ohm)}, "
                        f"MAD={_format_ohm(full_quality.mad_sigma_ohm)}); "
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
        return _contact_quality_from_samples(samples).status

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
                f"({contact_quality.status}); correct contact, then Measure "
                "or Skip."
            )
        else:
            self._status(
                f"Route measurement: point {position}/{total} relative RMS "
                f"{_format_percent(record.relative_rms)} exceeds "
                f"{_format_percent(self._max_relative_rms or math.nan)}; "
                "correct contact, then Measure or Skip."
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
            contact_quality = _contact_quality_from_samples(samples)
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

    @staticmethod
    def _samples_have_bad_contact(
        samples: list[RouteMeasurementSample] | tuple[RouteMeasurementSample, ...],
    ) -> bool:
        return _contact_quality_from_samples(samples).good is False


__all__ = [
    "RouteMeasurementCsvWriter",
    "CSV_FIELDS",
    "filter_route_points_by_previous_status",
    "latest_route_measurement_statuses",
    "RouteContactHeightRecord",
    "RouteContactQuality",
    "RouteContactPlacementResult",
    "RouteContactSeekResult",
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


def _format_float(value: float) -> str:
    if not math.isfinite(value):
        return ""
    return f"{float(value):.12g}"


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


def _contact_quality_from_samples(
    samples: list[RouteMeasurementSample] | tuple[RouteMeasurementSample, ...],
) -> RouteContactQuality:
    finite_values = [
        float(sample.differential_resistance_ohm)
        for sample in samples
        if math.isfinite(float(sample.differential_resistance_ohm))
    ]
    compliance_hits = sum(1 for sample in samples if sample.compliance_hit)
    polarity_mismatches = sum(
        1 for sample in samples if _sample_has_polarity_sign_mismatch(sample)
    )
    if len(finite_values) < 2:
        return RouteContactQuality(
            assessed=False,
            good=None,
            status="unchecked",
            compliance_hits=compliance_hits,
            polarity_sign_mismatch_count=polarity_mismatches,
            reasons=("too_few_readings",),
        )

    sorted_values = sorted(finite_values)
    median = _percentile(sorted_values, 50.0)
    abs_deviations = sorted(abs(value - median) for value in finite_values)
    mad_sigma = 1.4826 * _percentile(abs_deviations, 50.0)
    abs_steps = sorted(
        abs(finite_values[index] - finite_values[index - 1])
        for index in range(1, len(finite_values))
    )
    p95_abs_step = _percentile(abs_steps, 95.0) if abs_steps else 0.0
    span = max(finite_values) - min(finite_values)

    reasons: list[str] = []
    if mad_sigma > CONTACT_MAX_MAD_SIGMA_OHM:
        reasons.append("mad_sigma_too_high")
    if p95_abs_step > CONTACT_MAX_P95_ABS_STEP_OHM:
        reasons.append("step_noise_too_high")
    if polarity_mismatches:
        reasons.append("polarity_sign_mismatch")

    good = not reasons
    return RouteContactQuality(
        assessed=True,
        good=good,
        status="good" if good else "bad_contact",
        median_ohm=median,
        mad_sigma_ohm=mad_sigma,
        p95_abs_step_ohm=p95_abs_step,
        span_ohm=span,
        compliance_hits=compliance_hits,
        polarity_sign_mismatch_count=polarity_mismatches,
        reasons=tuple(reasons),
    )


def _sample_has_polarity_sign_mismatch(sample: RouteMeasurementSample) -> bool:
    negative_current = sample.negative_current_a
    positive_current = sample.positive_current_a
    if negative_current is None or positive_current is None:
        return False
    if not math.isfinite(negative_current) or not math.isfinite(positive_current):
        return False
    return negative_current * positive_current >= 0.0


def _percentile(sorted_values: list[float], percentile: float) -> float:
    if not sorted_values:
        return math.nan
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    bounded = max(0.0, min(100.0, float(percentile)))
    position = (len(sorted_values) - 1) * bounded / 100.0
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(sorted_values[lower])
    fraction = position - lower
    return float(
        sorted_values[lower]
        + (sorted_values[upper] - sorted_values[lower]) * fraction
    )


def _measurement_sample_from_raw(
    raw: object,
    sample_index: int,
) -> RouteMeasurementSample:
    if isinstance(raw, RouteMeasurementSample):
        return replace(raw, sample_index=sample_index)
    if isinstance(raw, dict):
        negative = raw.get("negative")
        positive = raw.get("positive")
        differential = _raw_float(
            raw,
            "differential_resistance_ohm",
            "primary_value",
            default=math.nan,
        )
        negative_data = negative if isinstance(negative, dict) else {}
        positive_data = positive if isinstance(positive, dict) else {}
        return RouteMeasurementSample(
            sample_index=sample_index,
            differential_resistance_ohm=differential,
            compliance_hit=_raw_bool(
                raw,
                "compliance_hit",
                "short_detected",
                default=False,
            ),
            negative_source_voltage_v=_raw_float_or_none(
                negative_data,
                "source_voltage_v",
                "bias_voltage_v",
            ),
            negative_measured_voltage_v=_raw_float_or_none(
                negative_data,
                "measured_voltage_v",
                "voltage_v",
            ),
            negative_current_a=_raw_float_or_none(
                negative_data,
                "current_a",
                "measured_current_a",
            ),
            negative_resistance_ohm=_raw_float_or_none(
                negative_data,
                "resistance_ohm",
                "v_over_i_ohm",
            ),
            positive_source_voltage_v=_raw_float_or_none(
                positive_data,
                "source_voltage_v",
                "bias_voltage_v",
            ),
            positive_measured_voltage_v=_raw_float_or_none(
                positive_data,
                "measured_voltage_v",
                "voltage_v",
            ),
            positive_current_a=_raw_float_or_none(
                positive_data,
                "current_a",
                "measured_current_a",
            ),
            positive_resistance_ohm=_raw_float_or_none(
                positive_data,
                "resistance_ohm",
                "v_over_i_ohm",
            ),
        )
    return RouteMeasurementSample(
        sample_index=sample_index,
        differential_resistance_ohm=float(raw),
    )


def route_measurement_sample_from_raw(
    raw: object,
    sample_index: int,
) -> RouteMeasurementSample:
    """Convert one backend measurement payload into a route sample."""

    return _measurement_sample_from_raw(raw, sample_index)


def summarize_route_contact_quality(
    samples: list[RouteMeasurementSample] | tuple[RouteMeasurementSample, ...],
) -> RouteContactQuality:
    """Evaluate the contact-quality metrics used by route workflows."""

    return _contact_quality_from_samples(samples)


def _raw_bool(
    data: dict[str, object],
    *keys: str,
    default: bool,
) -> bool:
    for key in keys:
        if key not in data:
            continue
        value = data.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "yes", "y", "on"}:
                return True
            if text in {"0", "false", "no", "n", "off"}:
                return False
    return default


def _raw_float(
    data: dict[str, object],
    *keys: str,
    default: float,
) -> float:
    value = _raw_float_or_none(data, *keys)
    return default if value is None else value


def _raw_float_or_none(data: dict[str, object], *keys: str) -> float | None:
    for key in keys:
        if key not in data:
            continue
        value = data.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _record_to_csv_row(record: RouteMeasurementRecord) -> dict[str, str]:
    contact = record.contact_quality
    return {
        "timestamp": record.timestamp,
        "structure_number": str(record.structure_number),
        "nplc": str(record.nplc),
        "measurement_type": str(record.measurement_type),
        "n_measurements": str(record.n_measurements),
        "resistance_ohm": _format_float(record.resistance_ohm),
        "resistance_rms_ohm": _format_float(record.resistance_rms_ohm),
        "relative_rms": _format_float(record.relative_rms),
        "status": record.status,
        "contact_quality": "" if contact is None else contact.status,
        "contact_median_ohm": ""
        if contact is None
        else _format_float(contact.median_ohm),
        "contact_mad_sigma_ohm": ""
        if contact is None
        else _format_float(contact.mad_sigma_ohm),
        "contact_p95_abs_step_ohm": ""
        if contact is None
        else _format_float(contact.p95_abs_step_ohm),
        "contact_span_ohm": ""
        if contact is None
        else _format_float(contact.span_ohm),
        "contact_compliance_hits": ""
        if contact is None
        else str(contact.compliance_hits),
        "contact_polarity_sign_mismatches": ""
        if contact is None
        else str(contact.polarity_sign_mismatch_count),
    }
