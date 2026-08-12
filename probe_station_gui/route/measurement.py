"""Blocking probe-route measurement runner and CSV persistence."""

from __future__ import annotations

import csv
import logging
import math
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Collection

from probe_station_gui.route import run_coordinator as _run_coordinator
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
    RoutePointRuntimeSnapshot,
)
from probe_station_gui.route.point_execution_adapters import (
    LegacyContactBindings,
    PointExecutionAdapters,
    RouteMeasurementEvents,
)
from probe_station_gui.route.run_control_mailbox import _RouteRunControlMailbox


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


class RouteMeasurementRunner(_run_coordinator._RouteRunCoordinator):
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
        self._run_control = _RouteRunControlMailbox(
            waiting_changed=self._events.waiting,
        )
        self._stage_task_active = False
        self._progress_started_at: float | None = None
        self._route_offset_lock = threading.Lock()
        self._route_offset_xy: Point2D = (0.0, 0.0)
        self._last_recorded_point: RouteMeasurementPoint | None = None
        self._contact_state = ContactMeasurementState()
        self._csv_write_retry_interval_s = self.CSV_WRITE_RETRY_INTERVAL_S
        self._point_adapters = PointExecutionAdapters.create(
            stage_controller=self._stage_controller,
            lcr_controller=self._lcr_controller,
            callbacks=self._events,
            append_csv=self._append_csv_record,
            stopped=self._run_control.stop_requested,
            interrupted=self._run_control.interrupt_requested,
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
            stop_requested=self._run_control.point_stop_requested,
            clear_interrupt=self._run_control.clear_interrupt,
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
        return self._run_control.is_waiting()

    def wait_until_waiting(self, timeout_s: float) -> bool:
        return self._run_control.wait_until_waiting(timeout_s)

    def set_current_adjustment_point(self, point_number: int) -> tuple[bool, str]:
        index = self.index_for_point_number(int(point_number))
        if index is None:
            return False, f"Point {int(point_number)} is not enabled or not found."
        with self._route_offset_lock:
            self._last_recorded_point = self._points[index]
        return True, ""

    def stop(self) -> None:
        self._run_control.request_stop()

    def submit_confirmation(self, action: str) -> bool:
        return self._run_control.submit_confirmation(action)

    def submit_jump(self, point_number: int) -> bool:
        return self.submit_confirmation(f"jump:{int(point_number)}")

    def request_current_point_correction(self) -> None:
        self._run_control.request_interrupt()

    def current_point_correction_requested(self) -> bool:
        return self._run_control.interrupt_requested()

    def clear_current_point_correction_request(self) -> None:
        self._run_control.clear_interrupt()

    def request_pause_after_current_point(self) -> None:
        self._run_control.request_pause()

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

    def _route_point_reference_capture_eligible(self) -> bool:
        """Allow capture during a pause request, but never after stop/interrupt."""

        return not self._run_control.point_stop_requested()

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
        return self._run_route()

    def requires_optical_session(self) -> bool:
        return bool(self._photo_enabled or self._photo_focus_enabled)

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

    def _append_csv_record(self, record: RouteMeasurementRecord) -> None:
        notice_shown = False
        while True:
            try:
                self._csv_writer.append(record)
                return
            except PermissionError:
                if self._run_control.point_stop_requested():
                    raise _run_coordinator._RouteMeasurementStopped(
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
                    raise _run_coordinator._RouteMeasurementStopped(
                        "Route measurement stopped by user."
                    )

    def _wait_before_csv_write_retry(self) -> bool:
        retry_interval_s = max(0.0, float(self._csv_write_retry_interval_s))
        if retry_interval_s <= 0.0:
            return not self._run_control.point_stop_requested()
        deadline = time.monotonic() + retry_interval_s
        while True:
            if self._run_control.point_stop_requested():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return True
            self._run_control.wait_for_stop(min(remaining, 0.05))

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
            return not self._run_control.stop_requested()
        deadline = time.monotonic() + self._photo_settle_s
        while True:
            if self._run_control.stop_requested():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return True
            if self._run_control.wait_for_stop(min(remaining, 0.05)):
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

    def _auto_next_ok_or_short_enabled(self) -> bool:
        with self._auto_next_lock:
            return bool(self._auto_next_ok_or_short)

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
            self._run_control.interrupt_requested()
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
