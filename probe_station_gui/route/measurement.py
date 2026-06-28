"""Blocking probe-route measurement runner and CSV persistence."""

from __future__ import annotations

import csv
import inspect
import logging
import math
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Collection

from probe_station_gui.route.contact_quality import (
    RouteContactQuality,
    RouteContactQualityLimits,
    RouteMeasurementSample,
    _contact_quality_from_samples,
    _format_contact_quality_failure,
    _measurement_sample_from_raw,
    route_measurement_sample_from_raw,
    summarize_route_contact_quality,
)
from probe_station_gui.route import contact_lifecycle
from probe_station_gui.route.contact_seek import (
    ContactSeekAttempt,
    contact_seek_attempts,
    normalize_contact_seek_limit,
    normalize_contact_seek_step,
)
from probe_station_gui.route.measurement_csv import (
    CSV_FIELDS,
    RouteMeasurementCsvWriter,
)
from probe_station_gui.route.measurement_defaults import (
    AUTO_CONTACT_SEEK_MAX_TOTAL_MM as DEFAULT_AUTO_CONTACT_SEEK_MAX_TOTAL_MM,
    AUTO_CONTACT_SEEK_STEP_MM as DEFAULT_AUTO_CONTACT_SEEK_STEP_MM,
    DEFAULT_CONTACT_SETTLE_S as DEFAULT_ROUTE_CONTACT_SETTLE_S,
    SHORT_CHECK_SAMPLE_COUNT as DEFAULT_SHORT_CHECK_SAMPLE_COUNT,
)
from probe_station_gui.route.measurement_payloads import (
    focus_result_to_dict as _focus_result_to_dict,
)
from probe_station_gui.route.measurement_records import (
    Point2D,
    RouteContactHeightRecord,
    RouteContactPlacementResult,
    RouteContactSeekResult,
    RouteExternalContactPreparation,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
    RoutePhotoRecord,
)
from probe_station_gui.route.external_session import RouteExternalMeasurementSessionRunner
from probe_station_gui.route.formatting import (
    format_route_ohm,
    format_route_percent,
)
from probe_station_gui.route.model import structure_number_from_labels
from probe_station_gui.route.operation_modes import (
    ROUTE_OPERATION_MEASURE,
    ROUTE_OPERATION_MODES,
    ROUTE_OPERATION_PHOTO,
    ROUTE_OPERATION_PHOTO_THEN_MEASURE,
    normalize_route_operation_mode,
    route_operation_measure_enabled,
    route_operation_photo_enabled,
)
from probe_station_gui.route.shift import route_shift_from_stage_xy


logger = logging.getLogger(__name__)


def _callable_accepts_keyword(function: object, name: str) -> bool:
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return False
    keyword_kinds = {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == name
            and parameter.kind in keyword_kinds
        )
        for parameter in signature.parameters.values()
    )


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
class _RoutePointPreparation:
    point_interrupted: bool
    measurement_prepare_task: _BackgroundRouteTask | None
    photos_saved: int = 0
    stop_message: str | None = None


@dataclass(frozen=True)
class _RoutePointPhotoPreparation:
    point_interrupted: bool
    photos_saved: int = 0
    stop_message: str | None = None


@dataclass(frozen=True)
class _RoutePointLoopDecision:
    position_index: int
    stop_message: str | None = None


@dataclass
class _RouteRunProgress:
    position_index: int = 0
    measurements_saved: int = 0
    photos_saved: int = 0
    needs_final_lift: bool = False


@dataclass(frozen=True)
class _RoutePointFlowResult:
    position_index: int
    measurements_saved: int = 0
    photos_saved: int = 0
    stop_message: str | None = None


@dataclass
class _RoutePointMeasurementState:
    measurement_prepare_task: _BackgroundRouteTask | None
    point_interrupted: bool
    needles_lowered: bool = False
    lift_task: _BackgroundRouteTask | None = None
    record: RouteMeasurementRecord | None = None
    contact_height_record: RouteContactHeightRecord | None = None
    record_saved: bool = False
    quality_rejected: bool = False
    result_emitted: bool = False
    save_exhausted_bad_contact: bool = False
    measurements_saved: int = 0
    stop_message: str | None = None


@dataclass(frozen=True)
class _ResistanceStats:
    count: int
    mean_ohm: float
    rms_ohm: float
    relative_rms: float
    complete_finite_batch: bool


@dataclass(frozen=True)
class _ContactSeekAttemptMeasurement:
    samples: list[RouteMeasurementSample]
    axis_a_lowering_mm: float


@dataclass(frozen=True)
class _ContactSeekAttemptResolution:
    samples: list[RouteMeasurementSample] | None
    final_status: str
    found: bool = False


class RouteMeasurementRunner:
    """Run a saved probe route using direct stage and LCR controller methods."""

    DEFAULT_CONTACT_SETTLE_S = DEFAULT_ROUTE_CONTACT_SETTLE_S
    SHORT_CHECK_SAMPLE_COUNT = DEFAULT_SHORT_CHECK_SAMPLE_COUNT
    AUTO_CONTACT_SEEK_STEP_MM = DEFAULT_AUTO_CONTACT_SEEK_STEP_MM
    AUTO_CONTACT_SEEK_MAX_TOTAL_MM = DEFAULT_AUTO_CONTACT_SEEK_MAX_TOTAL_MM

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
        self._measure_enabled = route_operation_measure_enabled(self._operation_mode)
        self._photo_enabled = route_operation_photo_enabled(self._operation_mode)
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
            self._route_offset_xy, message = route_shift_from_stage_xy(
                (current_x, current_y),
                point.stage_xy,
            )
        return True, message

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
        return contact_lifecycle.place_contact(
            self,
            point,
            position=position,
            total=total,
            move_to_point=move_to_point,
            lift_before_move=lift_before_move,
            lift_on_failure=lift_on_failure,
            clear_interrupt=clear_interrupt,
        )

    def _prepare_contact_placement_move(
        self,
        point: RouteMeasurementPoint,
        *,
        position: int,
        total: int,
        move_to_point: bool,
        lift_before_move: bool,
    ) -> None:
        contact_lifecycle.prepare_contact_placement_move(
            self,
            point,
            position=position,
            total=total,
            move_to_point=move_to_point,
            lift_before_move=lift_before_move,
        )

    def _prepare_contact_measurement_batch(self) -> None:
        contact_lifecycle.prepare_contact_measurement_batch(self)

    def _measure_contact_placement_record(
        self,
        point: RouteMeasurementPoint,
        *,
        position: int,
        total: int,
    ) -> RouteMeasurementRecord:
        return contact_lifecycle.measure_contact_placement_record(
            self,
            point,
            position=position,
            total=total,
        )

    def _lift_needles_after_failed_contact(self) -> None:
        contact_lifecycle.lift_needles_after_failed_contact(self)

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
        return contact_lifecycle.prepare_external_contact(
            self,
            point,
            position=position,
            total=total,
            photo_enabled=photo_enabled,
            photo_focus_enabled=photo_focus_enabled,
            move_to_point=move_to_point,
            lift_before_move=lift_before_move,
            lift_on_failure=lift_on_failure,
        )

    def _raise_if_point_interrupted(self) -> None:
        if self._point_interrupt_requested.is_set():
            raise RuntimeError("Route contact interrupted.")

    def _route_point_stop_requested(self) -> bool:
        return (
            self._stop_requested.is_set()
            or self._point_interrupt_requested.is_set()
        )

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
        return contact_lifecycle.measure_current_contact(
            self,
            point,
            position=position,
            total=total,
            auto_contact_seek=auto_contact_seek,
            action_label=action_label,
        )

    def run(self) -> tuple[bool, str]:
        progress = _RouteRunProgress()
        success = False
        message = "Route measurement stopped."
        try:
            self._validate_route_run_configuration()
            self._open_route_meter_if_needed()
            startup_stop_message = self._start_route_run()
            if startup_stop_message is not None:
                message = startup_stop_message
                return success, message
            total = len(self._points)
            progress.position_index = self._route_start_index()
            self._progress_started_at = time.monotonic()
            if self._wait_before_first_point:
                initial_decision = self._initial_route_point_loop_decision(
                    position_index=progress.position_index,
                    total=total,
                )
                if initial_decision.stop_message is not None:
                    message = initial_decision.stop_message
                    return success, message
                progress.position_index = initial_decision.position_index
            while progress.position_index < total:
                point_result = self._run_route_point(
                    position_index=progress.position_index,
                    total=total,
                    progress=progress,
                )
                progress.photos_saved += point_result.photos_saved
                progress.measurements_saved += point_result.measurements_saved
                if point_result.stop_message is not None:
                    message = point_result.stop_message
                    break
                progress.position_index = point_result.position_index
            if progress.position_index >= total:
                success = True
                message = self._route_completion_message(progress)
        except Exception as exc:
            message = str(exc)
            self._status(f"Route measurement failed: {message}")
        finally:
            message = self._finish_route_run(
                message=message,
                needs_final_lift=progress.needs_final_lift,
            )
        return success, message

    def _validate_route_run_configuration(self) -> None:
        if not self._points:
            raise ValueError("Route has no enabled points.")
        if self._photo_enabled and self._photo_callback is None:
            raise ValueError("Route photo capture is not configured.")
        if self._photo_focus_enabled and self._photo_focus_callback is None:
            raise ValueError("Route autofocus is not configured.")

    def _open_route_meter_if_needed(self) -> None:
        if self._measure_enabled and hasattr(self._lcr_controller, "open"):
            self._status("Route measurement: connecting meter.")
            self._lcr_controller.open()

    def _start_route_run(self) -> str | None:
        self._begin_stage_task()
        if self._stop_requested.is_set():
            return "Route measurement stopped by user."
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
            return "Route measurement stopped by user."
        return None

    def _route_start_index(self) -> int:
        start_index = self._index_for_point_number(self._start_point_number)
        if start_index is None and self._start_point_number == 1:
            return 0
        if start_index is None:
            raise ValueError(
                "Route start point "
                f"{self._start_point_number} is not enabled or not found."
            )
        return start_index

    def _initial_route_point_loop_decision(
        self,
        *,
        position_index: int,
        total: int,
    ) -> _RoutePointLoopDecision:
        decision = self._wait_before_first_route_point(
            point=self._points[position_index],
            position=position_index + 1,
            total=total,
        )
        if decision == "stop":
            return _RoutePointLoopDecision(
                position_index=position_index,
                stop_message="Route measurement stopped by user.",
            )
        self._begin_stage_task()
        jump_index = self._jump_target_index(decision)
        if jump_index is not None:
            return _RoutePointLoopDecision(position_index=jump_index)
        if decision == "skip":
            return _RoutePointLoopDecision(position_index=position_index + 1)
        return _RoutePointLoopDecision(position_index=position_index)

    def _run_route_point(
        self,
        *,
        position_index: int,
        total: int,
        progress: _RouteRunProgress,
    ) -> _RoutePointFlowResult:
        if self._stop_requested.is_set():
            return _RoutePointFlowResult(
                position_index=position_index,
                stop_message="Route measurement stopped by user.",
            )
        self._point_interrupt_requested.clear()
        position = position_index + 1
        point = self._points[position_index]
        point_interrupted = self._point_interrupt_requested.is_set()
        self._emit_progress(position, total, int(point.index))
        self._status(
            f"Route measurement: point {position}/{total} "
            f"{point.label}."
        )
        preparation = self._prepare_route_point_for_measurement(
            point=point,
            position=position,
            total=total,
            point_interrupted=point_interrupted,
        )
        point_interrupted = preparation.point_interrupted
        if preparation.stop_message is not None:
            return _RoutePointFlowResult(
                position_index=position_index,
                photos_saved=preparation.photos_saved,
                stop_message=preparation.stop_message,
            )
        if not self._measure_enabled:
            return self._finish_photo_only_route_point(
                point=point,
                position=position,
                total=total,
                position_index=position_index,
                point_interrupted=point_interrupted,
                photos_saved=preparation.photos_saved,
            )
        return self._run_measured_route_point(
            point=point,
            position=position,
            total=total,
            position_index=position_index,
            point_interrupted=point_interrupted,
            measurement_prepare_task=preparation.measurement_prepare_task,
            photos_saved=preparation.photos_saved,
            progress=progress,
        )

    def _finish_photo_only_route_point(
        self,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        position_index: int,
        point_interrupted: bool,
        photos_saved: int,
    ) -> _RoutePointFlowResult:
        if point_interrupted:
            loop_decision = self._interrupted_route_point_loop_decision(
                point=point,
                position=position,
                total=total,
                position_index=position_index,
                clear_stage_cancel=False,
            )
            return _RoutePointFlowResult(
                position_index=loop_decision.position_index,
                photos_saved=photos_saved,
                stop_message=loop_decision.stop_message,
            )
        return _RoutePointFlowResult(
            position_index=position_index + 1,
            photos_saved=photos_saved,
        )

    def _run_measured_route_point(
        self,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        position_index: int,
        point_interrupted: bool,
        measurement_prepare_task: _BackgroundRouteTask | None,
        photos_saved: int,
        progress: _RouteRunProgress,
    ) -> _RoutePointFlowResult:
        measurement = self._perform_route_point_measurement(
            point=point,
            position=position,
            total=total,
            point_interrupted=point_interrupted,
            measurement_prepare_task=measurement_prepare_task,
            progress=progress,
        )
        if measurement.stop_message is not None:
            return _RoutePointFlowResult(
                position_index=position_index,
                measurements_saved=measurement.measurements_saved,
                photos_saved=photos_saved,
                stop_message=measurement.stop_message,
            )
        if measurement.point_interrupted:
            loop_decision = self._interrupted_route_point_loop_decision(
                point=point,
                position=position,
                total=total,
                position_index=position_index,
                clear_stage_cancel=True,
            )
            return _RoutePointFlowResult(
                position_index=loop_decision.position_index,
                measurements_saved=measurement.measurements_saved,
                photos_saved=photos_saved,
                stop_message=loop_decision.stop_message,
            )
        if measurement.record is None:
            return _RoutePointFlowResult(
                position_index=position_index,
                measurements_saved=measurement.measurements_saved,
                photos_saved=photos_saved,
            )
        if measurement.quality_rejected:
            return self._finish_rejected_route_point_measurement(
                point=point,
                record=measurement.record,
                position=position,
                total=total,
                position_index=position_index,
                result_emitted=measurement.result_emitted,
                photos_saved=photos_saved,
            )
        return self._finish_accepted_route_point_measurement(
            point=point,
            position=position,
            total=total,
            position_index=position_index,
            measurement=measurement,
            photos_saved=photos_saved,
        )

    def _perform_route_point_measurement(
        self,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        point_interrupted: bool,
        measurement_prepare_task: _BackgroundRouteTask | None,
        progress: _RouteRunProgress,
    ) -> _RoutePointMeasurementState:
        state = _RoutePointMeasurementState(
            measurement_prepare_task=measurement_prepare_task,
            point_interrupted=point_interrupted,
        )
        try:
            if not state.point_interrupted:
                self._lower_and_settle_route_point_measurement(
                    state=state,
                    point=point,
                    position=position,
                    total=total,
                    progress=progress,
                )
            if not state.point_interrupted and state.stop_message is None:
                self._measure_route_point_record(
                    state=state,
                    point=point,
                    position=position,
                    total=total,
                    progress=progress,
                )
            if not state.point_interrupted and state.record is not None:
                self._record_route_point_measurement_state(
                    state=state,
                    point=point,
                    position=position,
                    total=total,
                )
        except Exception as exc:
            if self._point_interrupt_cancelled_exception(exc):
                state.point_interrupted = True
            else:
                raise
        finally:
            self._cleanup_route_point_measurement_state(state, progress)
        return state

    def _lower_and_settle_route_point_measurement(
        self,
        *,
        state: _RoutePointMeasurementState,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        progress: _RouteRunProgress,
    ) -> None:
        if state.measurement_prepare_task is not None:
            state.measurement_prepare_task.wait()
            state.measurement_prepare_task = None
        self._emit_pre_contact_photo(point, position, total)
        self._lower_needles_for_measurement()
        state.needles_lowered = True
        progress.needs_final_lift = True
        if self._sleep_contact_settle():
            return
        if self._point_interrupt_requested.is_set():
            state.point_interrupted = True
        else:
            state.stop_message = "Route measurement stopped by user."

    def _measure_route_point_record(
        self,
        *,
        state: _RoutePointMeasurementState,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        progress: _RouteRunProgress,
    ) -> None:
        self._status(
            f"Route measurement: point {position}/{total} "
            "measuring."
        )
        samples = self._measure_samples(
            position=position,
            total=total,
            prepare_task=state.measurement_prepare_task,
            after_measurement=lambda: self._start_route_point_lift_after_measurement(
                state
            ),
        )
        state.measurement_prepare_task = None
        if samples is None:
            if self._point_interrupt_requested.is_set():
                state.point_interrupted = True
            else:
                state.stop_message = "Route measurement stopped by user."
            return
        self._wait_for_route_point_lift_before_record(state, progress)
        state.record = self._record_for_point(
            point=point,
            samples=samples,
        )
        state.contact_height_record = self._contact_height_record_for_point(
            point=point,
            record=state.record,
        )
        self._lift_route_needles_after_record_if_needed(state, progress)

    def _start_route_point_lift_after_measurement(
        self,
        state: _RoutePointMeasurementState,
    ) -> None:
        if not state.needles_lowered or state.lift_task is not None:
            return
        state.lift_task = self._start_needles_lift_task()

    def _wait_for_route_point_lift_before_record(
        self,
        state: _RoutePointMeasurementState,
        progress: _RouteRunProgress,
    ) -> None:
        if state.lift_task is None:
            return
        state.lift_task.wait()
        state.needles_lowered = False
        progress.needs_final_lift = False

    def _lift_route_needles_after_record_if_needed(
        self,
        state: _RoutePointMeasurementState,
        progress: _RouteRunProgress,
    ) -> None:
        if not state.needles_lowered:
            return
        self._stage_controller.run_external_needles_action(
            "lift",
            self._needle_feedrate,
        )
        state.needles_lowered = False
        progress.needs_final_lift = False

    def _record_route_point_measurement_state(
        self,
        *,
        state: _RoutePointMeasurementState,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> None:
        assert state.record is not None
        (
            state.record,
            state.record_saved,
            state.quality_rejected,
            state.save_exhausted_bad_contact,
            state.measurements_saved,
        ) = self._record_route_point_measurement(
            point=point,
            record=state.record,
            position=position,
            total=total,
        )
        state.result_emitted = True

    def _cleanup_route_point_measurement_state(
        self,
        state: _RoutePointMeasurementState,
        progress: _RouteRunProgress,
    ) -> None:
        if state.lift_task is not None and state.needles_lowered:
            try:
                if self._point_interrupt_requested.is_set():
                    self._clear_stage_cancel_after_point_interrupt()
                state.lift_task.wait()
                state.needles_lowered = False
                progress.needs_final_lift = False
            except Exception:
                logger.warning(
                    "Background route needle lift failed; retrying.",
                    exc_info=True,
                )
        if state.needles_lowered:
            if self._point_interrupt_requested.is_set():
                self._clear_stage_cancel_after_point_interrupt()
            self._stage_controller.run_external_needles_action(
                "lift",
                self._needle_feedrate,
            )
            progress.needs_final_lift = False
        if state.measurement_prepare_task is not None:
            state.measurement_prepare_task.wait()

    def _finish_rejected_route_point_measurement(
        self,
        *,
        point: RouteMeasurementPoint,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        position_index: int,
        result_emitted: bool,
        photos_saved: int,
    ) -> _RoutePointFlowResult:
        self._consume_pause_request()
        decision = self._wait_after_rejected_result(
            point=point,
            record=record,
            position=position,
            total=total,
            emit_result=not result_emitted,
        )
        loop_decision, saved_count = self._route_point_confirmation_loop_decision(
            decision,
            point=point,
            position=position,
            total=total,
            position_index=position_index,
            default_advances=False,
        )
        return _RoutePointFlowResult(
            position_index=loop_decision.position_index,
            measurements_saved=saved_count,
            photos_saved=photos_saved,
            stop_message=loop_decision.stop_message,
        )

    def _finish_accepted_route_point_measurement(
        self,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        position_index: int,
        measurement: _RoutePointMeasurementState,
        photos_saved: int,
    ) -> _RoutePointFlowResult:
        assert measurement.record is not None
        auto_next = self._route_point_auto_next(
            measurement.record,
            record_saved=measurement.record_saved,
        )
        pause_after_point = False
        if self._confirm_each_point:
            auto_next, pause_after_point = self._prepare_saved_route_point_wait(
                point=point,
                auto_next=auto_next,
            )
        if not measurement.result_emitted:
            self._emit_result(
                measurement.record,
                position,
                total,
                measurement.record_saved,
            )
        self._emit_route_point_record_callbacks(
            measurement=measurement,
            position=position,
            total=total,
        )
        if self._confirm_each_point:
            confirmation = self._saved_route_point_confirmation_result(
                point=point,
                record=measurement.record,
                position=position,
                total=total,
                position_index=position_index,
                auto_next=auto_next,
                pause_after_point=pause_after_point,
                save_exhausted_bad_contact=(
                    measurement.save_exhausted_bad_contact
                ),
            )
            if confirmation is not None:
                return _RoutePointFlowResult(
                    position_index=confirmation.position_index,
                    measurements_saved=(
                        measurement.measurements_saved
                        + confirmation.measurements_saved
                    ),
                    photos_saved=photos_saved,
                    stop_message=confirmation.stop_message,
                )
        return _RoutePointFlowResult(
            position_index=position_index + 1,
            measurements_saved=measurement.measurements_saved,
            photos_saved=photos_saved,
        )

    def _route_point_auto_next(
        self,
        record: RouteMeasurementRecord,
        *,
        record_saved: bool,
    ) -> bool:
        return (
            self._confirm_each_point
            and record_saved
            and record.status in {"ok", "short"}
            and self._auto_next_ok_or_short_enabled()
        )

    def _prepare_saved_route_point_wait(
        self,
        *,
        point: RouteMeasurementPoint,
        auto_next: bool,
    ) -> tuple[bool, bool]:
        with self._confirmation_condition:
            self._pending_confirmation = None
        with self._route_offset_lock:
            self._last_recorded_point = point
        pause_after_point = self._consume_pause_request()
        auto_next = auto_next and not pause_after_point
        if not auto_next:
            self._finish_stage_task()
            self._set_waiting(True)
        return auto_next, pause_after_point

    def _emit_route_point_record_callbacks(
        self,
        *,
        measurement: _RoutePointMeasurementState,
        position: int,
        total: int,
    ) -> None:
        if (
            measurement.record_saved
            and measurement.contact_height_record is not None
            and self._contact_height_record_callback is not None
        ):
            self._contact_height_record_callback(
                measurement.contact_height_record,
                position,
                total,
            )
        if self._record_callback is not None:
            assert measurement.record is not None
            self._record_callback(measurement.record, position, total)

    def _saved_route_point_confirmation_result(
        self,
        *,
        point: RouteMeasurementPoint,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        position_index: int,
        auto_next: bool,
        pause_after_point: bool,
        save_exhausted_bad_contact: bool,
    ) -> _RoutePointFlowResult | None:
        status_detail = self._saved_route_point_status_detail(
            record,
            save_exhausted_bad_contact=save_exhausted_bad_contact,
        )
        if auto_next:
            self._status(
                f"Route measurement: point {position}/{total} "
                f"{status_detail}; continuing."
            )
            return None
        action_text = "paused" if pause_after_point else status_detail
        self._status(
            f"Route measurement: point {position}/{total} "
            f"{action_text}; "
            "choose Measure or Skip."
        )
        decision = self._wait_for_valid_confirmation()
        self._set_waiting(False)
        loop_decision, saved_count = self._route_point_confirmation_loop_decision(
            decision,
            point=point,
            position=position,
            total=total,
            position_index=position_index,
            default_advances=True,
        )
        return _RoutePointFlowResult(
            position_index=loop_decision.position_index,
            measurements_saved=saved_count,
            stop_message=loop_decision.stop_message,
        )

    @staticmethod
    def _saved_route_point_status_detail(
        record: RouteMeasurementRecord,
        *,
        save_exhausted_bad_contact: bool,
    ) -> str:
        if save_exhausted_bad_contact:
            return "contact seek exhausted; saved"
        if record.status == "short":
            return "short-circuit detected; saved"
        return "saved"

    def _route_completion_message(self, progress: _RouteRunProgress) -> str:
        if self._measure_enabled and self._photo_enabled:
            return (
                "Route measurement complete: "
                f"{progress.photos_saved} photos and "
                f"{progress.measurements_saved} measurements saved. "
                f"CSV: {self.csv_path}."
            )
        if self._photo_enabled:
            return (
                "Route photo capture complete: "
                f"{progress.photos_saved} photos saved."
            )
        return (
            "Route measurement complete: "
            f"{progress.measurements_saved} measurements saved to "
            f"{self.csv_path}."
        )

    def _finish_route_run(
        self,
        *,
        message: str,
        needs_final_lift: bool,
    ) -> str:
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
        return message

    def _measure_manual_contact_and_advance(
        self,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        position_index: int,
    ) -> tuple[_RoutePointLoopDecision, int]:
        manual_record = self._measure_manual_contact_here(
            point=point,
            position=position,
            total=total,
        )
        if manual_record is None:
            if self._point_interrupt_requested.is_set():
                self._point_interrupt_requested.clear()
                return _RoutePointLoopDecision(position_index=position_index), 0
            return (
                _RoutePointLoopDecision(
                    position_index=position_index,
                    stop_message="Route measurement stopped by user.",
                ),
                0,
            )
        next_index = position_index + 1
        if next_index < total:
            self._begin_stage_task()
        return _RoutePointLoopDecision(position_index=next_index), 1

    def _route_point_confirmation_loop_decision(
        self,
        decision: str,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        position_index: int,
        default_advances: bool,
    ) -> tuple[_RoutePointLoopDecision, int]:
        if decision == "stop":
            return (
                _RoutePointLoopDecision(
                    position_index=position_index,
                    stop_message="Route measurement stopped by user.",
                ),
                0,
            )
        if decision == "measure":
            return self._measure_manual_contact_and_advance(
                point=point,
                position=position,
                total=total,
                position_index=position_index,
            )
        self._begin_stage_task()
        jump_index = self._jump_target_index(decision)
        if jump_index is not None:
            return _RoutePointLoopDecision(position_index=jump_index), 0
        if decision == "skip" or (default_advances and decision != "remeasure"):
            return _RoutePointLoopDecision(position_index=position_index + 1), 0
        return _RoutePointLoopDecision(position_index=position_index), 0

    def _record_route_point_measurement(
        self,
        *,
        point: RouteMeasurementPoint,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
    ) -> tuple[RouteMeasurementRecord, bool, bool, bool, int]:
        save_exhausted_bad_contact = self._should_save_exhausted_bad_contact(record)
        record_saved = False
        quality_rejected = False
        if (
            self._confirm_each_point
            and self._record_exceeds_quality_limit(record)
            and not save_exhausted_bad_contact
        ):
            if not self._record_has_failed_contact_quality(record):
                record = replace(record, status="unstable")
            quality_rejected = True
        else:
            self._csv_writer.append(record)
            record_saved = True
        self._emit_contact_photo(
            point,
            record,
            position,
            total,
            record_saved,
        )
        self._emit_result(record, position, total, record_saved)
        return (
            record,
            record_saved,
            quality_rejected,
            save_exhausted_bad_contact,
            1 if record_saved else 0,
        )

    def _prepare_route_point_for_measurement(
        self,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        point_interrupted: bool,
    ) -> _RoutePointPreparation:
        point_interrupted = self._raise_needles_before_route_point_if_needed(
            position=position,
            total=total,
            point_interrupted=point_interrupted,
        )
        if self._stop_requested.is_set():
            return self._stopped_route_point_preparation(point_interrupted)
        target_xy = self._route_point_initial_target_xy(point)
        point_interrupted = self._move_to_route_point_xy(
            target_xy,
            point_interrupted=point_interrupted,
        )
        if self._stop_requested.is_set():
            return self._stopped_route_point_preparation(point_interrupted)
        measurement_prepare_task = self._route_point_measurement_prepare_task(
            point_interrupted
        )
        photo_preparation = self._focus_and_capture_route_point_photo(
            point=point,
            position=position,
            total=total,
            point_interrupted=point_interrupted,
        )
        point_interrupted = photo_preparation.point_interrupted
        if photo_preparation.stop_message is not None:
            return self._stopped_route_point_preparation(
                point_interrupted=point_interrupted,
                measurement_prepare_task=measurement_prepare_task,
                photos_saved=photo_preparation.photos_saved,
            )
        point_interrupted = self._move_from_photo_to_contact_xy_if_needed(
            point=point,
            target_xy=target_xy,
            position=position,
            total=total,
            point_interrupted=point_interrupted,
        )
        return _RoutePointPreparation(
            point_interrupted=point_interrupted,
            measurement_prepare_task=measurement_prepare_task,
            photos_saved=photo_preparation.photos_saved,
        )

    @staticmethod
    def _stopped_route_point_preparation(
        point_interrupted: bool,
        *,
        measurement_prepare_task: _BackgroundRouteTask | None = None,
        photos_saved: int = 0,
    ) -> _RoutePointPreparation:
        return _RoutePointPreparation(
            point_interrupted=point_interrupted,
            measurement_prepare_task=measurement_prepare_task,
            photos_saved=photos_saved,
            stop_message="Route measurement stopped by user.",
        )

    def _route_point_initial_target_xy(self, point: RouteMeasurementPoint) -> Point2D:
        if self._photo_enabled or self._photo_focus_enabled:
            return self._adjusted_photo_stage_xy(point)
        return self._adjusted_stage_xy(point)

    def _route_point_measurement_prepare_task(
        self,
        point_interrupted: bool,
    ) -> _BackgroundRouteTask | None:
        if not self._measure_enabled or point_interrupted:
            return None
        return self._start_measurement_prepare_task(self._initial_measurement_count())

    def _raise_needles_before_route_point_if_needed(
        self,
        *,
        position: int,
        total: int,
        point_interrupted: bool,
    ) -> bool:
        if not (self._photo_enabled or self._photo_focus_enabled):
            return point_interrupted
        self._status(
            f"Route measurement: point {position}/{total} "
            "raising needles before move."
        )
        return not self._run_point_interruptible_action(
            lambda: self._stage_controller.run_external_needles_action(
                "raise",
                self._needle_feedrate,
            )
        )

    def _move_to_route_point_xy(
        self,
        target_xy: Point2D,
        *,
        point_interrupted: bool,
    ) -> bool:
        if point_interrupted:
            return True
        return not self._run_point_interruptible_action(
            lambda: self._stage_controller.run_external_move_to_xy(
                target_xy[0],
                target_xy[1],
            )
        )

    def _focus_route_point_photo(
        self,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        point_interrupted: bool,
    ) -> tuple[object | None, bool]:
        point_interrupted = point_interrupted or self._point_interrupt_requested.is_set()
        if not self._photo_focus_enabled or point_interrupted:
            return None, point_interrupted
        self._status(
            f"Route measurement: point {position}/{total} local autofocus."
        )
        focus_result = self._run_photo_focus(point, position, total)
        point_interrupted = self._point_interrupt_requested.is_set()
        focus_message = str(focus_result or "")
        if focus_message.strip():
            self._status(
                f"Route measurement: point {position}/{total} {focus_message}"
            )
        return focus_result, point_interrupted

    def _focus_and_capture_route_point_photo(
        self,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        point_interrupted: bool,
    ) -> _RoutePointPhotoPreparation:
        focus_result, point_interrupted = self._focus_route_point_photo(
            point=point,
            position=position,
            total=total,
            point_interrupted=point_interrupted,
        )
        if self._stop_requested.is_set():
            return _RoutePointPhotoPreparation(
                point_interrupted=point_interrupted,
                stop_message="Route measurement stopped by user.",
            )
        photos_saved = self._capture_route_point_photo_if_needed(
            point=point,
            position=position,
            total=total,
            focus_result=focus_result,
            point_interrupted=point_interrupted,
        )
        point_interrupted = point_interrupted or self._point_interrupt_requested.is_set()
        stop_message = (
            "Route measurement stopped by user."
            if self._stop_requested.is_set()
            else None
        )
        return _RoutePointPhotoPreparation(
            point_interrupted=point_interrupted,
            photos_saved=photos_saved,
            stop_message=stop_message,
        )

    def _capture_route_point_photo_if_needed(
        self,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        focus_result: object | None,
        point_interrupted: bool,
    ) -> int:
        if point_interrupted or not self._photo_enabled:
            return 0
        if not self._sleep_photo_settle():
            return 0
        if self._point_interrupt_requested.is_set():
            return 0
        photo_path = self._capture_photo(
            point,
            position,
            total,
            focus_result=focus_result,
        )
        self._status(
            f"Route measurement: point {position}/{total} "
            f"photo saved to {photo_path}."
        )
        return 1

    def _move_from_photo_to_contact_xy_if_needed(
        self,
        *,
        point: RouteMeasurementPoint,
        target_xy: Point2D,
        position: int,
        total: int,
        point_interrupted: bool,
    ) -> bool:
        if not self._measure_enabled or point_interrupted:
            return point_interrupted
        contact_xy = self._adjusted_stage_xy(point)
        if self._same_stage_xy(target_xy, contact_xy):
            return self._point_interrupt_requested.is_set()
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
        return point_interrupted or self._point_interrupt_requested.is_set()

    def _interrupted_route_point_loop_decision(
        self,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        position_index: int,
        clear_stage_cancel: bool,
    ) -> _RoutePointLoopDecision:
        if clear_stage_cancel:
            self._clear_stage_cancel_after_point_interrupt()
        self._point_interrupt_requested.clear()
        decision = self._wait_after_interrupted_point(
            point=point,
            position=position,
            total=total,
        )
        if decision == "stop":
            return _RoutePointLoopDecision(
                position_index=position_index,
                stop_message="Route measurement stopped by user.",
            )
        self._begin_stage_task()
        jump_index = self._jump_target_index(decision)
        if jump_index is not None:
            return _RoutePointLoopDecision(position_index=jump_index)
        if decision == "skip":
            return _RoutePointLoopDecision(position_index=position_index + 1)
        return _RoutePointLoopDecision(position_index=position_index)

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
            return not self._route_point_stop_requested()
        deadline = time.monotonic() + self._contact_settle_s
        while True:
            if self._route_point_stop_requested():
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
        return normalize_contact_seek_step(
            value,
            default_step_mm=RouteMeasurementRunner.AUTO_CONTACT_SEEK_STEP_MM,
        )

    @staticmethod
    def _normalized_contact_seek_limit(value: object) -> float:
        return normalize_contact_seek_limit(
            value,
            default_limit_mm=RouteMeasurementRunner.AUTO_CONTACT_SEEK_MAX_TOTAL_MM,
        )

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
        samples = initial_samples
        attempts = contact_seek_attempts(
            step_mm,
            self._auto_contact_seek_max_total_mm,
        )
        attempts_completed = 0
        last_depth_mm = math.nan
        last_axis_a_lowering_mm = math.nan
        last_status = initial_quality.status
        for attempt in attempts:
            if self._route_point_stop_requested():
                return None
            self._status(
                f"Route measurement: point {position}/{total} "
                f"pressing deeper {attempt.attempt_number}/{attempt.max_attempts}, "
                f"{attempt.depth_mm:.4f} mm below down."
            )
            prepare_task = self._start_measurement_prepare_task(
                self._initial_measurement_count()
            )
            if self._route_point_stop_requested():
                return None
            if not self._press_contact_seek_attempt(
                attempt,
                lower_to_depth=lower_to_depth,
                adjust=adjust,
            ):
                return initial_samples
            if self._route_point_stop_requested():
                return None
            attempts_completed += 1
            last_depth_mm = float(attempt.depth_mm)
            measurement = self._read_contact_seek_attempt_measurement(prepare_task)
            if measurement is None:
                return None
            last_axis_a_lowering_mm = measurement.axis_a_lowering_mm
            depth_label = f"{attempt.depth_mm:.4f} mm below down"
            resolution = self._resolve_contact_seek_attempt(
                samples=measurement.samples,
                depth_label=depth_label,
                position=position,
                total=total,
            )
            if resolution.samples is None:
                return None
            samples = resolution.samples
            last_status = resolution.final_status
            if resolution.found:
                self._set_contact_seek_result(
                    found=True,
                    status="found" if last_status != "short" else "short",
                    attempts=attempts_completed,
                    initial_status=initial_quality.status,
                    final_status=last_status,
                    depth_below_down_mm=attempt.depth_mm,
                    axis_a_lowering_mm=last_axis_a_lowering_mm,
                )
                return samples
        self._status(
            f"Route measurement: point {position}/{total} contact seek did not "
            f"find stable contact within {self._auto_contact_seek_max_total_mm:.3f} mm."
        )
        self._set_contact_seek_result(
            found=False,
            status="not_found",
            attempts=attempts_completed,
            initial_status=initial_quality.status,
            final_status=last_status,
            depth_below_down_mm=last_depth_mm,
            axis_a_lowering_mm=last_axis_a_lowering_mm,
        )
        return samples

    def _press_contact_seek_attempt(
        self,
        attempt: ContactSeekAttempt,
        *,
        lower_to_depth: Callable[..., object] | None,
        adjust: Callable[..., object] | None,
    ) -> bool:
        if attempt.depth_mm > 0.0 and callable(lower_to_depth):
            lower_to_depth(attempt.depth_mm, self._needle_feedrate)
            return True
        if callable(adjust):
            adjust(
                attempt.adjust_delta_mm(self._auto_contact_seek_step_mm),
                self._needle_feedrate,
            )
            return True
        return False

    def _read_contact_seek_attempt_measurement(
        self,
        prepare_task: _BackgroundRouteTask | None,
    ) -> _ContactSeekAttemptMeasurement | None:
        if not self._sleep_contact_settle():
            return None
        axis_a_lowering_mm = self._latest_axis_a_lowering()
        samples = self._read_measurement_samples(
            self._initial_measurement_count(),
            start_index=1,
            prepare_task=prepare_task,
        )
        if samples is None:
            return None
        return _ContactSeekAttemptMeasurement(
            samples=samples,
            axis_a_lowering_mm=axis_a_lowering_mm,
        )

    def _resolve_contact_seek_attempt(
        self,
        *,
        samples: list[RouteMeasurementSample],
        depth_label: str,
        position: int,
        total: int,
    ) -> _ContactSeekAttemptResolution:
        if self._samples_are_short(samples):
            self._status(
                f"Route measurement: point {position}/{total} "
                f"{depth_label}, short-circuit detected."
            )
            return _ContactSeekAttemptResolution(
                samples=samples,
                final_status="short",
                found=True,
            )
        quality = self._contact_quality_from_samples(samples)
        self._status(
            f"Route measurement: point {position}/{total} "
            f"{depth_label}, {quality.status}, "
            f"median={_format_ohm(quality.median_ohm)}, "
            f"MAD={_format_ohm(quality.mad_sigma_ohm)}"
            f"{self._contact_quality_failure_suffix(quality)}."
        )
        if quality.good is False:
            return _ContactSeekAttemptResolution(
                samples=samples,
                final_status=quality.status,
            )
        completed_samples = self._complete_measurement_samples(samples)
        if completed_samples is None:
            return _ContactSeekAttemptResolution(
                samples=None,
                final_status=quality.status,
            )
        if self._completed_measurement_is_acceptable(completed_samples):
            return _ContactSeekAttemptResolution(
                samples=completed_samples,
                final_status=self._contact_status_for_samples(completed_samples),
                found=True,
            )
        if self._samples_have_bad_contact(completed_samples):
            full_quality = self._contact_quality_from_samples(completed_samples)
            self._status(
                f"Route measurement: point {position}/{total} full "
                f"measurement at {depth_label} failed contact check "
                f"({full_quality.status}, "
                f"median={_format_ohm(full_quality.median_ohm)}, "
                f"MAD={_format_ohm(full_quality.mad_sigma_ohm)}"
                f"{self._contact_quality_failure_suffix(full_quality)}); "
                "trying deeper."
            )
            return _ContactSeekAttemptResolution(
                samples=completed_samples,
                final_status=full_quality.status,
            )
        if self._samples_exceed_relative_rms_limit(completed_samples):
            relative_rms = self._relative_rms_from_samples(completed_samples)
            self._status(
                f"Route measurement: point {position}/{total} full "
                f"measurement at {depth_label} relative RMS "
                f"{_format_percent(relative_rms)} exceeds "
                f"{_format_percent(self._max_relative_rms or math.nan)}; "
                "trying deeper."
            )
            return _ContactSeekAttemptResolution(
                samples=completed_samples,
                final_status="unstable",
            )
        return _ContactSeekAttemptResolution(
            samples=completed_samples,
            final_status=quality.status,
        )

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
        return RouteMeasurementRunner._resistance_stats_from_samples(samples).relative_rms

    def _record_status_for_samples(
        self,
        samples: list[RouteMeasurementSample],
        contact_quality: RouteContactQuality,
    ) -> str:
        if self._samples_are_short(samples):
            return "short"
        if contact_quality.good is False:
            return "bad_contact"
        return "ok"

    @staticmethod
    def _resistance_stats_from_samples(
        samples: list[RouteMeasurementSample],
    ) -> _ResistanceStats:
        resistances_ohm = tuple(
            sample.differential_resistance_ohm for sample in samples
        )
        finite_resistances = tuple(
            float(value) for value in resistances_ohm if math.isfinite(value)
        )
        if len(finite_resistances) != len(resistances_ohm) or not finite_resistances:
            return _ResistanceStats(
                count=len(resistances_ohm),
                mean_ohm=math.inf,
                rms_ohm=math.nan,
                relative_rms=math.nan,
                complete_finite_batch=False,
            )
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
        return _ResistanceStats(
            count=len(resistances_ohm),
            mean_ohm=mean_resistance,
            rms_ohm=rms_resistance,
            relative_rms=relative_rms,
            complete_finite_batch=True,
        )

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
            if self._route_point_stop_requested():
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
            if self._route_point_stop_requested():
                return None
            return samples
        samples: list[RouteMeasurementSample] = []
        for index in range(start_index, start_index + count):
            if self._route_point_stop_requested():
                return None
            samples.append(self._read_measurement_sample(index))
            if self._route_point_stop_requested():
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
        if self._record_has_failed_contact_quality(record):
            assert contact_quality is not None
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
        stats = self._resistance_stats_from_samples(samples)
        if stats.complete_finite_batch:
            contact_quality = self._contact_quality_from_samples(samples)
            status = self._record_status_for_samples(samples, contact_quality)
        else:
            status = "overload"
            contact_quality = None
        return RouteMeasurementRecord(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            structure_number=_structure_number_for_point(point),
            nplc=self._nplc_label,
            measurement_type=self._measurement_type,
            n_measurements=stats.count,
            resistance_ohm=stats.mean_ohm,
            resistance_rms_ohm=stats.rms_ohm,
            relative_rms=stats.relative_rms,
            status=status,
            contact_quality=contact_quality,
            raw_samples=tuple(samples),
        )

    def _record_exceeds_quality_limit(self, record: RouteMeasurementRecord) -> bool:
        if record.status == "short":
            return False
        if self._record_has_failed_contact_quality(record):
            return True
        if self._max_relative_rms is None or record.status != "ok":
            return False
        return (
            math.isfinite(record.relative_rms)
            and record.relative_rms > self._max_relative_rms
        )

    @staticmethod
    def _record_has_failed_contact_quality(record: RouteMeasurementRecord) -> bool:
        contact_quality = record.contact_quality
        return contact_quality is not None and contact_quality.good is False

    @staticmethod
    def _contact_placement_record_is_success(record: RouteMeasurementRecord) -> bool:
        return str(record.status).strip().lower() in {"ok", "short"}

    def _should_save_exhausted_bad_contact(
        self,
        record: RouteMeasurementRecord,
    ) -> bool:
        if not self._record_has_failed_contact_quality(record):
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

    def _contact_placement_message(
        self,
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
            self._contact_quality_failure_suffix(record.contact_quality)
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

    def _samples_have_bad_contact(
        self,
        samples: list[RouteMeasurementSample] | tuple[RouteMeasurementSample, ...],
    ) -> bool:
        return self._contact_quality_from_samples(samples).good is False


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
    return normalize_route_operation_mode(value)


def _structure_number_for_point(point: RouteMeasurementPoint) -> int:
    return structure_number_from_labels(
        point.label,
        point.point_id,
        default=point.index,
    )


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


def _format_percent(value: float) -> str:
    return format_route_percent(value)


def _format_ohm(value: float) -> str:
    return format_route_ohm(value)
