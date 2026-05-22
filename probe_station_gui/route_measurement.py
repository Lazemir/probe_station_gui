"""Blocking probe-route measurement runner and CSV persistence."""

from __future__ import annotations

import csv
import math
import threading
import time
from dataclasses import dataclass
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

    resistance_ohm: float


class RouteMeasurementCsvWriter:
    """Incrementally write route measurements so partial runs are preserved."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def write_header(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def append(self, record: RouteMeasurementRecord) -> None:
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([record.resistance_ohm])
            handle.flush()


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
        contact_settle_s: float = DEFAULT_CONTACT_SETTLE_S,
        status_callback: Callable[[str], None] | None = None,
        record_callback: Callable[[RouteMeasurementRecord, int, int], None] | None = None,
    ) -> None:
        self._points = list(points)
        self._csv_writer = RouteMeasurementCsvWriter(csv_path)
        self._stage_controller = stage_controller
        self._lcr_controller = lcr_controller
        self._needle_feedrate = needle_feedrate
        self._contact_settle_s = max(0.0, float(contact_settle_s))
        self._status_callback = status_callback
        self._record_callback = record_callback
        self._stop_requested = threading.Event()

    @property
    def csv_path(self) -> Path:
        return self._csv_writer.path

    def stop(self) -> None:
        self._stop_requested.set()

    def run(self) -> tuple[bool, str]:
        task_started = False
        needs_final_raise = False
        records_written = 0
        success = False
        message = "Route measurement stopped."
        try:
            if not self._points:
                raise ValueError("Route has no enabled points.")
            self._csv_writer.write_header()
            self._stage_controller.begin_external_task("route measurement")
            task_started = True
            self._status("Route measurement: raising needles.")
            self._stage_controller.run_external_needles_action(
                "raise",
                self._needle_feedrate,
            )
            total = len(self._points)
            for position, point in enumerate(self._points, start=1):
                if self._stop_requested.is_set():
                    message = "Route measurement stopped by user."
                    break
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
                    resistance_ohm = float(
                        self._lcr_controller.read_primary_value_now()
                    )
                    record = self._record_for_point(
                        resistance_ohm=resistance_ohm,
                    )
                    self._csv_writer.append(record)
                    records_written += 1
                    if self._record_callback is not None:
                        self._record_callback(record, position, total)
                finally:
                    if needles_lowered:
                        self._stage_controller.run_external_needles_action(
                            "raise",
                            self._needle_feedrate,
                        )
                        needs_final_raise = False
            else:
                success = True
                message = (
                    "Route measurement complete: "
                    f"{records_written} points written to {self.csv_path}."
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
        return success, message

    def _sleep_contact_settle(self) -> bool:
        if self._contact_settle_s <= 0.0:
            return not self._stop_requested.is_set()
        return not self._stop_requested.wait(self._contact_settle_s)

    def _status(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)

    @staticmethod
    def _record_for_point(
        *,
        resistance_ohm: float,
    ) -> RouteMeasurementRecord:
        stored_resistance = resistance_ohm if math.isfinite(resistance_ohm) else math.inf
        return RouteMeasurementRecord(
            resistance_ohm=stored_resistance,
        )


__all__ = [
    "RouteMeasurementCsvWriter",
    "RouteMeasurementPoint",
    "RouteMeasurementRecord",
    "RouteMeasurementRunner",
]
