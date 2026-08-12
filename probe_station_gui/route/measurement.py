"""Blocking probe-route measurement runner and CSV persistence."""

from __future__ import annotations

import csv
import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Collection

from probe_station_gui.route.contact_quality import (
    RouteContactQuality,
    RouteContactQualityLimits,
    RouteMeasurementSample,
    route_measurement_sample_from_raw,
    summarize_route_contact_quality,
)
from probe_station_gui.route import contact_measurement
from probe_station_gui.route import measurement_recording
from probe_station_gui.route.contact_measurement import ContactMeasurementState
from probe_station_gui.route.contact_lifecycle import (
    CallbackContactAutofocusAdapter,
    CallbackContactPhotoAdapter,
    RouteContactRequest,
)
from probe_station_gui.route.contact_seek import (
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
    format_route_percent,
)
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
from probe_station_gui.route.point_execution import (
    RoutePointRequest,
    RoutePointCleanupError,
    RoutePointResult,
    RoutePointRuntimeSnapshot,
)
from probe_station_gui.route.point_execution_adapters import (
    LegacyContactBindings,
    PointExecutionAdapters,
    RouteMeasurementEvents,
)


logger = logging.getLogger(__name__)


def _design_frame_payload(snapshot: object | None) -> dict[str, object] | None:
    if snapshot is None:
        return None
    frame_id = getattr(snapshot, "frame_id", None)
    frame_version = getattr(snapshot, "frame_version", None)
    if frame_id is None or frame_version is None:
        return None
    return {
        "frame_id": str(frame_id),
        "frame_version": int(frame_version),
    }


class _RouteMeasurementStopped(RuntimeError):
    pass


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


class RouteMeasurementRunner:
    """Run a saved probe route using direct stage and LCR controller methods."""

    DEFAULT_CONTACT_SETTLE_S = DEFAULT_ROUTE_CONTACT_SETTLE_S
    SHORT_CHECK_SAMPLE_COUNT = DEFAULT_SHORT_CHECK_SAMPLE_COUNT
    AUTO_CONTACT_SEEK_STEP_MM = DEFAULT_AUTO_CONTACT_SEEK_STEP_MM
    AUTO_CONTACT_SEEK_MAX_TOTAL_MM = DEFAULT_AUTO_CONTACT_SEEK_MAX_TOTAL_MM
    CSV_WRITE_RETRY_INTERVAL_S = 1.0

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
        contact_quality_limits: RouteContactQualityLimits | None = None,
        confirm_each_point: bool = False,
        auto_next_ok_or_short: bool = False,
        auto_contact_seek_on_bad_contact: bool = False,
        auto_contact_seek_step_mm: float = AUTO_CONTACT_SEEK_STEP_MM,
        auto_contact_seek_max_total_mm: float = AUTO_CONTACT_SEEK_MAX_TOTAL_MM,
        contact_settle_s: float = DEFAULT_CONTACT_SETTLE_S,
        nplc_label: str = "",
        measurement_type: str = "",
        events: RouteMeasurementEvents | None = None,
        operation_mode: str = ROUTE_OPERATION_MEASURE,
        photo_settle_s: float = 0.2,
        photo_focus_enabled: bool = False,
        photo_focus_range_mm: float = 0.0,
        photo_output_dir: str = "",
        wait_before_first_point: bool = False,
        design_frame_snapshot: object | None = None,
        post_success_contact: Callable[
            [RouteContactPlacementResult], Callable[[], None] | None
        ]
        | None = None,
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
        self._events = events or RouteMeasurementEvents()
        self._operation_mode = _normalize_operation_mode(operation_mode)
        self._measure_enabled = route_operation_measure_enabled(self._operation_mode)
        self._photo_enabled = route_operation_photo_enabled(self._operation_mode)
        self._photo_settle_s = max(0.0, float(photo_settle_s))
        self._photo_focus_enabled = bool(photo_focus_enabled)
        self._photo_focus_range_mm = max(0.0, float(photo_focus_range_mm))
        self._photo_output_dir = str(photo_output_dir)
        self._wait_before_first_point = bool(wait_before_first_point)
        self._design_frame_snapshot = design_frame_snapshot
        self._last_run_result: dict[str, object] | None = None
        self._stop_requested = threading.Event()
        self._point_interrupt_requested = threading.Event()
        self._pause_requested = threading.Event()
        self._confirmation_condition = threading.Condition()
        self._pending_confirmation: str | None = None
        self._stage_task_active = False
        self._progress_started_at: float | None = None
        self._route_offset_lock = threading.Lock()
        self._route_offset_xy: Point2D = (0.0, 0.0)
        self._last_recorded_point: RouteMeasurementPoint | None = None
        self._contact_state = ContactMeasurementState()
        self._waiting_condition = threading.Condition()
        self._waiting = False
        self._csv_write_retry_interval_s = self.CSV_WRITE_RETRY_INTERVAL_S
        self._point_adapters = PointExecutionAdapters.create(
            stage_controller=self._stage_controller,
            lcr_controller=self._lcr_controller,
            callbacks=self._events,
            append_csv=self._append_csv_record,
            stopped=self._stop_requested.is_set,
            interrupted=self._point_interrupt_requested.is_set,
        )
        self._legacy_contact = LegacyContactBindings(
            stage_controller=self._stage_controller,
            lcr_controller=self._lcr_controller,
            state=self._contact_state,
            snapshot=self._route_point_snapshot,
            adapters=self._point_adapters,
            begin_stage=self._begin_stage_task,
            finish_stage=self._finish_stage_task,
            lower_needles=self._lower_needles_for_measurement,
            contact_xy=self._adjusted_stage_xy,
            photo_xy=self._adjusted_photo_stage_xy,
            settle_photo=self._sleep_photo_settle,
            stop_requested=self._route_point_stop_requested,
            clear_interrupt=self._point_interrupt_requested.clear,
            set_seek_enabled=self._set_auto_contact_seek_enabled,
            status=self._status,
        )
        self._contact_flow = self._legacy_contact.build_flow(
            post_success_contact=post_success_contact,
            post_success_contact_eligible=self._route_point_reference_capture_eligible,
        )

    @property
    def csv_path(self) -> Path:
        return self._csv_writer.path

    @property
    def design_frame_snapshot(self) -> object | None:
        return self._design_frame_snapshot

    def design_frame_payload(self) -> dict[str, object] | None:
        return _design_frame_payload(self._design_frame_snapshot)

    def result_payload(self, *, success: bool, message: str) -> dict[str, object]:
        return {
            "success": bool(success),
            "message": str(message),
            "design_frame": self.design_frame_payload(),
        }

    def status_payload(self) -> dict[str, object]:
        return {
            "accepted": True,
            "running": (
                self._progress_started_at is not None
                and self._last_run_result is None
            ),
            "waiting": self.is_waiting(),
            "design_frame": self.design_frame_payload(),
            "result": (
                dict(self._last_run_result)
                if self._last_run_result is not None
                else None
            ),
        }

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
        index = self.index_for_point_number(int(point_number))
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

    def current_point_correction_requested(self) -> bool:
        return self._point_interrupt_requested.is_set()

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
        photo_focus_range_mm: float | None = None,
        photo_output_dir: str | None = None,
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
        if photo_focus_range_mm is not None:
            self._photo_focus_range_mm = max(0.0, float(photo_focus_range_mm))
        if photo_output_dir is not None:
            self._photo_output_dir = str(photo_output_dir)
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
        """Leave verified route contact under the needles without writing CSV."""
        return self._contact_flow.place_contact(
            RouteContactRequest(
                point=point,
                position=position,
                total=total,
                move_to_point=move_to_point,
                lift_before_move=lift_before_move,
                lift_on_failure=lift_on_failure,
                clear_interrupt=clear_interrupt,
            )
        ).require_placement()

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
        """Prepare one route contact for the external route-control session."""

        autofocus = (
            CallbackContactAutofocusAdapter(self._run_photo_focus)
            if photo_focus_enabled
            else None
        )
        photo = CallbackContactPhotoAdapter(self._capture_photo) if photo_enabled else None
        return self._contact_flow.prepare_external_contact(
            RouteContactRequest(
                point=point,
                position=position,
                total=total,
                move_to_point=move_to_point,
                lift_before_move=lift_before_move,
                lift_on_failure=lift_on_failure,
                clear_interrupt=False,
                autofocus=autofocus,
                photo=photo,
            )
        ).require_preparation()

    def _route_point_stop_requested(self) -> bool:
        return (
            self._stop_requested.is_set()
            or self._point_interrupt_requested.is_set()
        )

    def _route_point_reference_capture_eligible(self) -> bool:
        """Allow capture during a pause request, but never after stop/interrupt."""

        return not self._route_point_stop_requested()

    def _set_auto_contact_seek_enabled(self, enabled: bool) -> None:
        self._auto_contact_seek_on_bad_contact = bool(enabled)

    def lift_needles_after_external_measurement(
        self,
        *,
        position: int = 1,
        total: int = 1,
    ) -> None:
        """Lift needles after an API-owned external measurement."""

        self._contact_flow.lift_after_external_measurement(
            position=position,
            total=total,
        )

    def check_contact(
        self,
        point: RouteMeasurementPoint,
        *,
        position: int = 1,
        total: int = 1,
    ) -> RouteContactPlacementResult:
        """Measure current contact quality without moving needles deeper."""

        return self._contact_flow.check_contact(
            RouteContactRequest(point=point, position=position, total=total)
        ).require_placement()

    def seek_contact(
        self,
        point: RouteMeasurementPoint,
        *,
        position: int = 1,
        total: int = 1,
    ) -> RouteContactPlacementResult:
        """Run automatic contact seek from the current needle position."""

        return self._contact_flow.seek_contact(
            RouteContactRequest(point=point, position=position, total=total)
        ).require_placement()

    def run(self) -> tuple[bool, str]:
        progress = _RouteRunProgress()
        success = False
        message = "Route measurement stopped."
        self._last_run_result = None
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
        except _RouteMeasurementStopped as exc:
            message = str(exc) or "Route measurement stopped by user."
        except Exception as exc:
            message = str(exc)
            self._status(f"Route measurement failed: {message}")
        finally:
            message = self._finish_route_run(
                message=message,
                needs_final_lift=progress.needs_final_lift,
            )
            self._last_run_result = self.result_payload(
                success=success,
                message=message,
            )
        return success, message

    def requires_optical_session(self) -> bool:
        return bool(self._photo_enabled or self._photo_focus_enabled)

    def _validate_route_run_configuration(self) -> None:
        if not self._points:
            raise ValueError("Route has no enabled points.")
        if (
            self._photo_enabled
            and self._events.photo is None
            and self._events.point_photo is None
        ):
            raise ValueError("Route photo capture is not configured.")
        if (
            self._photo_focus_enabled
            and self._events.photo_focus is None
            and self._events.point_photo_focus is None
        ):
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
        start_index = self.index_for_point_number(self._start_point_number)
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
        position = position_index + 1
        point = self._points[position_index]
        self._emit_progress(position, total, int(point.index))
        self._status(
            f"Route measurement: point {position}/{total} {point.label}."
        )
        try:
            result = self._point_adapters.execution.execute(
                self._point_request(point, position=position, total=total)
            )
        except RoutePointCleanupError as exc:
            progress.needs_final_lift = exc.pending_cleanup
            raise exc.cause from exc
        progress.needs_final_lift = result.pending_cleanup
        return self._finish_point_execution(
            point=point,
            result=result,
            position=position,
            total=total,
            position_index=position_index,
        )

    def _point_request(
        self,
        point: RouteMeasurementPoint,
        *,
        position: int,
        total: int,
    ) -> RoutePointRequest:
        return self._route_point_snapshot().request(
            point=point,
            position=position,
            total=total,
            route_offset_xy=self.route_offset_xy(),
        )

    def _route_point_snapshot(self) -> RoutePointRuntimeSnapshot:
        return RoutePointRuntimeSnapshot(
            measurement_count=self._measurement_count,
            initial_measurement_count=self._initial_measurement_count(),
            max_relative_rms=self._max_relative_rms,
            quality_limits=self._contact_quality_limits,
            nplc_label=self._nplc_label,
            measurement_type=self._measurement_type,
            confirm_each_point=self._confirm_each_point,
            seek_enabled=self._auto_contact_seek_on_bad_contact,
            seek_step_mm=self._auto_contact_seek_step_mm,
            seek_max_total_mm=self._auto_contact_seek_max_total_mm,
            contact_settle_s=self._contact_settle_s,
            needle_feedrate=self._needle_feedrate,
            measure_enabled=self._measure_enabled,
            photo_enabled=self._photo_enabled,
            photo_focus_enabled=self._photo_focus_enabled,
            photo_settle_s=self._photo_settle_s,
            photo_focus_range_mm=self._photo_focus_range_mm,
            photo_output_dir=self._photo_output_dir,
            csv_path=str(self.csv_path),
        )

    def _finish_point_execution(
        self,
        *,
        point: RouteMeasurementPoint,
        result: RoutePointResult,
        position: int,
        total: int,
        position_index: int,
    ) -> _RoutePointFlowResult:
        if result.stopped:
            return _RoutePointFlowResult(
                position_index=position_index,
                photos_saved=result.photos_saved,
                stop_message=result.stop_message,
            )
        if result.pending_cleanup:
            raise RuntimeError("Route point needle cleanup failed.")
        if result.interrupted:
            decision = self._interrupted_route_point_loop_decision(
                point=point,
                position=position,
                total=total,
                position_index=position_index,
                clear_stage_cancel=self._measure_enabled,
            )
            return _RoutePointFlowResult(
                position_index=decision.position_index,
                photos_saved=result.photos_saved,
                stop_message=decision.stop_message,
            )
        if not self._measure_enabled:
            return _RoutePointFlowResult(
                position_index=position_index + 1,
                photos_saved=result.photos_saved,
            )
        if result.record is None:
            return _RoutePointFlowResult(
                position_index=position_index,
                photos_saved=result.photos_saved,
            )
        if result.quality_rejected:
            return self._finish_rejected_point_result(
                point=point,
                result=result,
                position=position,
                total=total,
                position_index=position_index,
            )
        return self._finish_accepted_point_result(
            point=point,
            result=result,
            position=position,
            total=total,
            position_index=position_index,
        )

    def _finish_rejected_point_result(
        self,
        *,
        point: RouteMeasurementPoint,
        result: RoutePointResult,
        position: int,
        total: int,
        position_index: int,
    ) -> _RoutePointFlowResult:
        assert result.record is not None
        self._consume_pause_request()
        decision = self._wait_after_rejected_result(
            point=point,
            record=result.record,
            position=position,
            total=total,
            emit_result=not result.result_emitted,
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
            photos_saved=result.photos_saved,
            stop_message=loop_decision.stop_message,
        )

    def _finish_accepted_point_result(
        self,
        *,
        point: RouteMeasurementPoint,
        result: RoutePointResult,
        position: int,
        total: int,
        position_index: int,
    ) -> _RoutePointFlowResult:
        assert result.record is not None
        auto_next = self._route_point_auto_next(
            result.record,
            record_saved=result.record_saved,
        )
        pause_after_point = False
        if self._confirm_each_point:
            auto_next, pause_after_point = self._prepare_saved_route_point_wait(
                point=point,
                auto_next=auto_next,
            )
            confirmation = self._saved_route_point_confirmation_result(
                point=point,
                record=result.record,
                position=position,
                total=total,
                position_index=position_index,
                auto_next=auto_next,
                pause_after_point=pause_after_point,
                save_exhausted_bad_contact=result.save_exhausted_bad_contact,
            )
            if confirmation is not None:
                return _RoutePointFlowResult(
                    position_index=confirmation.position_index,
                    measurements_saved=(
                        result.measurements_saved
                        + confirmation.measurements_saved
                    ),
                    photos_saved=result.photos_saved,
                    stop_message=confirmation.stop_message,
                )
        return _RoutePointFlowResult(
            position_index=position_index + 1,
            measurements_saved=result.measurements_saved,
            photos_saved=result.photos_saved,
        )

    def _append_csv_record(self, record: RouteMeasurementRecord) -> None:
        notice_shown = False
        while True:
            try:
                self._csv_writer.append(record)
                return
            except PermissionError:
                if self._route_point_stop_requested():
                    raise _RouteMeasurementStopped(
                        "Route measurement stopped by user."
                    )
                if not notice_shown:
                    logger.warning(
                        "Route measurement CSV write blocked; retrying.",
                        exc_info=True,
                    )
                    self._status(
                        f"CSV is open. Close the CSV to save: {self.csv_path}."
                    )
                    notice_shown = True
                if not self._wait_before_csv_write_retry():
                    raise _RouteMeasurementStopped(
                        "Route measurement stopped by user."
                    )

    def _wait_before_csv_write_retry(self) -> bool:
        retry_interval_s = max(0.0, float(self._csv_write_retry_interval_s))
        if retry_interval_s <= 0.0:
            return not self._route_point_stop_requested()
        deadline = time.monotonic() + retry_interval_s
        while True:
            if self._route_point_stop_requested():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return True
            self._stop_requested.wait(min(remaining, 0.05))

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
        status_detail = measurement_recording.saved_route_point_status_detail(
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
        if self._events.status is not None:
            self._events.status(message)

    def _emit_progress(
        self,
        position: int,
        total: int,
        point_number: int,
    ) -> None:
        if self._events.progress is not None:
            self._events.progress(
                int(position),
                int(total),
                int(point_number),
            )

    def _set_waiting(self, waiting: bool) -> None:
        with self._waiting_condition:
            self._waiting = bool(waiting)
            self._waiting_condition.notify_all()
        if self._events.waiting is not None:
            self._events.waiting(bool(waiting))

    def _auto_next_ok_or_short_enabled(self) -> bool:
        with self._auto_next_lock:
            return bool(self._auto_next_ok_or_short)

    def _consume_pause_request(self) -> bool:
        requested = self._pause_requested.is_set()
        if requested:
            self._pause_requested.clear()
        return requested

    def _measure_manual_contact_here(
        self,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> RouteMeasurementRecord | None:
        self._contact_state.seek_result = None
        self._status(
            f"Route measurement: point {position}/{total} measuring current contact."
        )
        samples = contact_measurement.read_measurement_samples(
            self._legacy_contact.contact_context(),
            self._measurement_count,
            start_index=1,
            prepare_task=self._legacy_contact.contact_context().prepare(
                self._measurement_count
            ),
        )
        if samples is None:
            return None
        record = measurement_recording.record_for_point(
            self._legacy_contact.recording_context(),
            point=point,
            samples=samples,
        )
        contact_height_record = measurement_recording.contact_height_record_for_point(
            point=point,
            record=record,
            seek=self._contact_state.seek_result,
            axis_a_lowering_mm=contact_measurement.latest_axis_a_lowering(
                self._legacy_contact.contact_context()
            ),
        )
        self._append_csv_record(record)
        self._point_adapters.events.emit_contact_photo(
            point, record, position, total, True
        )
        self._point_adapters.events.emit_result(record, position, total, True)
        point_settings = self._route_point_snapshot().photo_settings
        if contact_height_record is not None and self._events.point_contact_height:
            self._events.point_contact_height(
                contact_height_record,
                position,
                total,
                point_settings,
            )
        elif contact_height_record is not None and self._events.contact_height:
            self._events.contact_height(
                contact_height_record,
                position,
                total,
            )
        if self._events.record is not None:
            self._events.record(record, position, total)
        self._status(
            f"Route measurement: point {position}/{total} saved; continuing."
        )
        return record

    def _capture_photo(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        focus_result: object | None = None,
    ) -> Path:
        point_callback = self._events.point_photo
        callback = self._events.photo
        if point_callback is None and callback is None:
            raise ValueError("Route photo capture is not configured.")
        photo_stage_xy = self._adjusted_photo_stage_xy(point)
        point_settings = self._route_point_snapshot().photo_settings
        result = Path(
            point_callback(
                point,
                position,
                total,
                focus_result,
                point_settings,
            )
            if point_callback is not None
            else callback(point, position, total, focus_result)
        ).expanduser()
        record = RoutePhotoRecord(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            path=str(result),
            structure_number=measurement_recording.structure_number_for_point(point),
            point_index=int(point.index),
            point_id=point.point_id,
            label=point.label,
            design_center=point.design_center,
            stage_xy=photo_stage_xy,
            focus=_focus_result_to_dict(focus_result),
        )
        if self._events.photo_record is not None:
            self._events.photo_record(record, position, total)
        return result

    def _run_photo_focus(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> object | None:
        point_callback = self._events.point_photo_focus
        callback = self._events.photo_focus
        if point_callback is None and callback is None:
            raise ValueError("Route autofocus is not configured.")
        try:
            settings = self._route_point_snapshot().photo_settings
            return (
                point_callback(point, position, total, settings)
                if point_callback is not None
                else callback(point, position, total)
            )
        except Exception as exc:
            if self._point_interrupt_cancelled_exception(exc):
                return None
            raise

    def _point_interrupt_cancelled_exception(self, exc: BaseException) -> bool:
        return bool(
            self._point_interrupt_requested.is_set()
            and str(exc) == "Operation cancelled."
        )

    def _clear_stage_cancel_after_point_interrupt(self) -> None:
        if not self._stage_task_active:
            return
        self._finish_stage_task()
        self._begin_stage_task()

    def _lower_needles_for_measurement(self) -> None:
        self._ensure_meter_output_context()
        self._stage_controller.run_external_needles_action(
            "lower",
            self._needle_feedrate,
        )

    def _ensure_meter_output_context(self) -> None:
        self._point_adapters.acquisition.enable_output()

    def _close_meter_output_context(self) -> None:
        self._point_adapters.acquisition.close_output()

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

    def _initial_measurement_count(self) -> int:
        return min(self._measurement_count, self._initial_measurement_count_value)

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
            self._point_adapters.events.emit_result(
                record, position, total, False
            )
        contact_quality = record.contact_quality
        if measurement_recording.record_has_failed_contact_quality(record):
            assert contact_quality is not None
            self._status(
                f"Route measurement: point {position}/{total} contact check failed "
                f"({contact_quality.status}"
                f"{measurement_recording.contact_quality_failure_suffix(self._legacy_contact.recording_context(), contact_quality)}); "
                "correct contact, then Measure, "
                "Remeasure, or Skip."
            )
        else:
            self._status(
                f"Route measurement: point {position}/{total} relative RMS "
                f"{format_route_percent(record.relative_rms)} exceeds "
                f"{format_route_percent(self._max_relative_rms or math.nan)}; "
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
        return self.index_for_point_number(point_number)

    def index_for_point_number(self, point_number: int) -> int | None:
        try:
            target = int(point_number)
        except (TypeError, ValueError):
            return None
        for index, point in enumerate(self._points):
            if int(point.index) == target:
                return index
        for index, point in enumerate(self._points):
            if measurement_recording.structure_number_for_point(point) == target:
                return index
        return None

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
        if statuses.get(measurement_recording.structure_number_for_point(point))
        in allowed
    ]
