"""Blocking probe-route measurement runner and CSV persistence."""

from __future__ import annotations

import csv
import math
import os
import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


Point2D = tuple[float, float]


CONTACT_MAX_MAD_SIGMA_OHM = 300.0
CONTACT_MAX_P95_ABS_STEP_OHM = 1_000.0


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
        result_callback: Callable[[RouteMeasurementRecord, int, int, bool], None]
        | None = None,
        waiting_callback: Callable[[bool], None] | None = None,
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
        self._result_callback = result_callback
        self._waiting_callback = waiting_callback
        self._stop_requested = threading.Event()
        self._point_interrupt_requested = threading.Event()
        self._pause_requested = threading.Event()
        self._confirmation_condition = threading.Condition()
        self._pending_confirmation: str | None = None
        self._stage_task_active = False
        self._route_offset_lock = threading.Lock()
        self._route_offset_xy: Point2D = (0.0, 0.0)
        self._last_recorded_point: RouteMeasurementPoint | None = None

    @property
    def csv_path(self) -> Path:
        return self._csv_writer.path

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

    def run(self) -> tuple[bool, str]:
        needs_final_lift = False
        measurements_saved = 0
        success = False
        message = "Route measurement stopped."
        try:
            if not self._points:
                raise ValueError("Route has no enabled points.")
            if hasattr(self._lcr_controller, "open"):
                self._status("Route measurement: connecting meter.")
                self._lcr_controller.open()
            self._csv_writer.write_header()
            self._begin_stage_task()
            if self._stop_requested.is_set():
                message = "Route measurement stopped by user."
                return success, message
            self._status("Route measurement: lifting needles.")
            self._stage_controller.run_external_needles_action(
                "lift",
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
                target_xy = self._adjusted_stage_xy(point)
                self._stage_controller.run_external_move_to_xy(
                    target_xy[0],
                    target_xy[1],
                )
                if self._stop_requested.is_set():
                    message = "Route measurement stopped by user."
                    break
                needles_lowered = False
                record: RouteMeasurementRecord | None = None
                record_saved = False
                quality_rejected = False
                result_emitted = False
                point_interrupted = self._point_interrupt_requested.is_set()
                try:
                    if not point_interrupted:
                        needs_final_lift = True
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
                        samples = self._measure_samples(position=position, total=total)
                        if samples is None:
                            if self._point_interrupt_requested.is_set():
                                point_interrupted = True
                            else:
                                message = "Route measurement stopped by user."
                                break
                        if not point_interrupted:
                            record = self._record_for_point(
                                point=point,
                                samples=samples,
                            )
                    if not point_interrupted and record is not None:
                        if (
                            self._confirm_each_point
                            and self._record_exceeds_quality_limit(record)
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
                        self._emit_result(record, position, total, record_saved)
                        result_emitted = True
                finally:
                    if needles_lowered:
                        self._stage_controller.run_external_needles_action(
                            "lift",
                            self._needle_feedrate,
                        )
                        needs_final_lift = False
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
                if self._record_callback is not None:
                    self._record_callback(record, position, total)
                if self._confirm_each_point:
                    status_detail = (
                        "short-circuit detected; saved"
                        if record.status == "short"
                        else "saved"
                    )
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
                            "choose Next, Remeasure, Skip, or Go To."
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
            self._finish_stage_task()
            if hasattr(self._lcr_controller, "close"):
                try:
                    self._lcr_controller.close()
                except Exception as exc:
                    message = f"{message} Instrument close failed: {exc}"
            self._set_waiting(False)
        return success, message

    def _begin_stage_task(self) -> None:
        if self._stage_task_active:
            return
        self._stage_controller.begin_external_task("route measurement")
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
    ) -> list[RouteMeasurementSample] | None:
        initial_count = self._initial_measurement_count()
        samples = self._read_measurement_samples(initial_count, start_index=1)
        if samples is None:
            return None
        if not self._auto_contact_seek_on_bad_contact or self._samples_are_short(samples):
            return self._complete_measurement_samples(samples)
        if self._samples_have_bad_contact(samples):
            return self._seek_contact_from_current_position(
                initial_samples=samples,
                position=position,
                total=total,
            )
        completed_samples = self._complete_measurement_samples(samples)
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
        )

    def _complete_measurement_samples(
        self,
        samples: list[RouteMeasurementSample],
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
            if not self._sleep_contact_settle():
                return None
            samples = self._read_measurement_samples(
                self._initial_measurement_count(),
                start_index=1,
            )
            if samples is None:
                return None
            depth_label = (
                "after lift/lower"
                if depth_mm <= 0.0
                else f"{depth_mm:.4f} mm below down"
            )
            if self._samples_are_short(samples):
                self._status(
                    f"Route measurement: point {position}/{total} "
                    f"{depth_label}, short-circuit detected."
                )
                return samples
            quality = _contact_quality_from_samples(samples)
            self._status(
                f"Route measurement: point {position}/{total} "
                f"{depth_label}, {quality.status}, "
                f"median={_format_ohm(quality.median_ohm)}, "
                f"MAD={_format_ohm(quality.mad_sigma_ohm)}."
            )
            if quality.good is not False:
                completed_samples = self._complete_measurement_samples(samples)
                if completed_samples is None:
                    return None
                samples = completed_samples
                if self._completed_measurement_is_acceptable(completed_samples):
                    return completed_samples
                if self._samples_have_bad_contact(completed_samples):
                    full_quality = _contact_quality_from_samples(completed_samples)
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
        return samples

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
    ) -> list[RouteMeasurementSample] | None:
        count = max(0, int(count))
        if count <= 0:
            return []
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
            "correct position, Save Shift if needed, then Remeasure, Skip, "
            "or Go To."
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
                f"({contact_quality.status}); correct contact, then Remeasure, "
                "Skip, or Go To."
            )
        else:
            self._status(
                f"Route measurement: point {position}/{total} relative RMS "
                f"{_format_percent(record.relative_rms)} exceeds "
                f"{_format_percent(self._max_relative_rms or math.nan)}; "
                "correct contact, then Remeasure, Skip, or Go To."
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
    "RouteContactQuality",
    "RouteMeasurementPoint",
    "RouteMeasurementRecord",
    "RouteMeasurementSample",
    "RouteMeasurementRunner",
    "route_measurement_sample_from_raw",
    "summarize_route_contact_quality",
]


def _structure_number_for_point(point: RouteMeasurementPoint) -> int:
    for value in (point.label, point.point_id):
        match = re.search(r"(\d+)\s*$", str(value).strip())
        if match is not None:
            try:
                return int(match.group(1))
            except ValueError:
                pass
    return int(point.index)


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
