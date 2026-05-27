"""Blocking probe-route measurement runner and CSV persistence."""

from __future__ import annotations

import csv
import math
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


Point2D = tuple[float, float]


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
class RouteMeasurementRecord:
    """One completed measurement row written to CSV."""

    timestamp: str
    junction: int
    nplc: str
    n_measurements: int
    resistance_ohm: float
    resistance_rms_ohm: float
    relative_rms: float
    status: str


CSV_FIELDS = [
    "timestamp",
    "junction",
    "nplc",
    "n_measurements",
    "resistance_ohm",
    "resistance_rms_ohm",
    "relative_rms",
    "status",
]


class RouteMeasurementCsvWriter:
    """Write route measurements after each point so partial runs are preserved."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def write_header(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            handle.flush()
            os.fsync(handle.fileno())

    def append(self, record: RouteMeasurementRecord) -> None:
        rows = self._read_rows()
        rows[record.junction] = _record_to_csv_row(record)
        self._write_rows(rows)

    def _read_rows(self) -> dict[int, dict[str, str]]:
        if not self.path.exists():
            return {}
        rows: dict[int, dict[str, str]] = {}
        with self.path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    junction = int(row.get("junction", ""))
                except (TypeError, ValueError):
                    continue
                rows[junction] = {
                    field: str(row.get(field, "")) for field in CSV_FIELDS
                }
        return rows

    def _write_rows(self, rows: dict[int, dict[str, str]]) -> None:
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for junction in sorted(rows):
                writer.writerow(
                    {
                        field: rows[junction].get(field, "")
                        for field in CSV_FIELDS
                    }
                )
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(self.path)


class RouteMeasurementRunner:
    """Run a saved probe route using direct stage and LCR controller methods."""

    DEFAULT_CONTACT_SETTLE_S = 0.2

    def __init__(
        self,
        *,
        points: list[RouteMeasurementPoint],
        csv_path: str | Path,
        stage_controller: Any,
        lcr_controller: Any,
        needle_feedrate: float | None,
        measurement_count: int = 1,
        confirm_each_point: bool = False,
        contact_settle_s: float = DEFAULT_CONTACT_SETTLE_S,
        nplc_label: str = "",
        status_callback: Callable[[str], None] | None = None,
        record_callback: Callable[[RouteMeasurementRecord, int, int], None] | None = None,
    ) -> None:
        self._points = list(points)
        self._csv_writer = RouteMeasurementCsvWriter(csv_path)
        self._stage_controller = stage_controller
        self._lcr_controller = lcr_controller
        self._needle_feedrate = needle_feedrate
        self._measurement_count = max(1, int(measurement_count))
        self._confirm_each_point = bool(confirm_each_point)
        self._contact_settle_s = max(0.0, float(contact_settle_s))
        self._nplc_label = str(nplc_label)
        self._status_callback = status_callback
        self._record_callback = record_callback
        self._stop_requested = threading.Event()
        self._confirmation_condition = threading.Condition()
        self._pending_confirmation: str | None = None

    @property
    def csv_path(self) -> Path:
        return self._csv_writer.path

    def stop(self) -> None:
        self._stop_requested.set()
        with self._confirmation_condition:
            self._confirmation_condition.notify_all()

    def submit_confirmation(self, action: str) -> None:
        normalized = str(action).strip().lower()
        if normalized not in {"next", "remeasure"}:
            return
        with self._confirmation_condition:
            self._pending_confirmation = normalized
            self._confirmation_condition.notify_all()

    def run(self) -> tuple[bool, str]:
        task_started = False
        needs_final_raise = False
        measurements_saved = 0
        completed_points = 0
        success = False
        message = "Route measurement stopped."
        try:
            if not self._points:
                raise ValueError("Route has no enabled points.")
            if hasattr(self._lcr_controller, "open"):
                self._status("Route measurement: connecting meter.")
                self._lcr_controller.open()
            self._csv_writer.write_header()
            self._stage_controller.begin_external_task("route measurement")
            task_started = True
            if self._stop_requested.is_set():
                message = "Route measurement stopped by user."
                return success, message
            self._status("Route measurement: raising needles.")
            self._stage_controller.run_external_needles_action(
                "raise",
                self._needle_feedrate,
            )
            if self._stop_requested.is_set():
                message = "Route measurement stopped by user."
                return success, message
            total = len(self._points)
            position_index = 0
            while position_index < total:
                if self._stop_requested.is_set():
                    message = "Route measurement stopped by user."
                    break
                position = position_index + 1
                point = self._points[position_index]
                self._status(
                    f"Route measurement: point {position}/{total} "
                    f"{point.label}."
                )
                self._stage_controller.run_external_move_to_xy(
                    point.stage_xy[0],
                    point.stage_xy[1],
                )
                if self._stop_requested.is_set():
                    message = "Route measurement stopped by user."
                    break
                needles_lowered = False
                record: RouteMeasurementRecord | None = None
                try:
                    needs_final_raise = True
                    self._stage_controller.run_external_needles_action(
                        "lower",
                        self._needle_feedrate,
                    )
                    needles_lowered = True
                    if not self._sleep_contact_settle():
                        message = "Route measurement stopped by user."
                        break
                    resistances_ohm = self._measure_resistances()
                    if resistances_ohm is None:
                        message = "Route measurement stopped by user."
                        break
                    record = self._record_for_point(
                        point=point,
                        resistances_ohm=resistances_ohm,
                    )
                    self._csv_writer.append(record)
                    measurements_saved += 1
                finally:
                    if needles_lowered:
                        self._stage_controller.run_external_needles_action(
                            "raise",
                            self._needle_feedrate,
                        )
                        needs_final_raise = False
                if record is None:
                    continue
                if self._confirm_each_point:
                    with self._confirmation_condition:
                        self._pending_confirmation = None
                if self._record_callback is not None:
                    self._record_callback(record, position, total)
                if self._confirm_each_point:
                    self._status(
                        f"Route measurement: point {position}/{total} saved; "
                        "choose Next or Cancel."
                    )
                    decision = self._wait_for_confirmation()
                    if decision == "stop":
                        message = "Route measurement stopped by user."
                        break
                    if decision == "remeasure":
                        continue
                completed_points += 1
                position_index += 1
            if position_index >= total:
                success = True
                message = (
                    "Route measurement complete: "
                    f"{completed_points} points saved to {self.csv_path} "
                    f"({measurements_saved} measurements)."
                )
        except Exception as exc:
            message = str(exc)
            self._status(f"Route measurement failed: {message}")
        finally:
            if task_started and needs_final_raise:
                try:
                    self._stage_controller.run_external_needles_action(
                        "raise",
                        self._needle_feedrate,
                    )
                except Exception as exc:
                    message = f"{message} Needle raise failed: {exc}"
            if task_started:
                self._stage_controller.finish_external_task()
            if hasattr(self._lcr_controller, "close"):
                try:
                    self._lcr_controller.close()
                except Exception as exc:
                    message = f"{message} Instrument close failed: {exc}"
        return success, message

    def _sleep_contact_settle(self) -> bool:
        if self._contact_settle_s <= 0.0:
            return not self._stop_requested.is_set()
        return not self._stop_requested.wait(self._contact_settle_s)

    def _status(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)

    def _measure_resistances(self) -> list[float] | None:
        readings: list[float] = []
        for _ in range(self._measurement_count):
            if self._stop_requested.is_set():
                return None
            readings.append(float(self._lcr_controller.read_primary_value_now()))
            if self._stop_requested.is_set():
                return None
        return readings

    def _wait_for_confirmation(self) -> str:
        with self._confirmation_condition:
            while not self._stop_requested.is_set():
                if self._pending_confirmation is not None:
                    decision = self._pending_confirmation
                    self._pending_confirmation = None
                    return decision
                self._confirmation_condition.wait(timeout=0.2)
        return "stop"

    def _record_for_point(
        self,
        *,
        point: RouteMeasurementPoint,
        resistances_ohm: list[float],
    ) -> RouteMeasurementRecord:
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
            status = "ok"
        else:
            mean_resistance = math.inf
            rms_resistance = math.nan
            relative_rms = math.nan
            status = "overload"
        return RouteMeasurementRecord(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            junction=_junction_for_point(point),
            nplc=self._nplc_label,
            n_measurements=len(resistances_ohm),
            resistance_ohm=mean_resistance,
            resistance_rms_ohm=rms_resistance,
            relative_rms=relative_rms,
            status=status,
        )


__all__ = [
    "RouteMeasurementCsvWriter",
    "CSV_FIELDS",
    "RouteMeasurementPoint",
    "RouteMeasurementRecord",
    "RouteMeasurementRunner",
]


def _junction_for_point(point: RouteMeasurementPoint) -> int:
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


def _record_to_csv_row(record: RouteMeasurementRecord) -> dict[str, str]:
    return {
        "timestamp": record.timestamp,
        "junction": str(record.junction),
        "nplc": str(record.nplc),
        "n_measurements": str(record.n_measurements),
        "resistance_ohm": _format_float(record.resistance_ohm),
        "resistance_rms_ohm": _format_float(record.resistance_rms_ohm),
        "relative_rms": _format_float(record.relative_rms),
        "status": record.status,
    }
