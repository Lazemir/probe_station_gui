"""CSV persistence for route measurement records."""

from __future__ import annotations

import csv
import math
import os
import threading
from pathlib import Path
from typing import Any


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
        self._path = Path(path).expanduser().resolve()
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        with self._lock:
            return self._path

    def set_path(self, path: str | Path) -> None:
        resolved = Path(path).expanduser().resolve()
        with self._lock:
            self._path = resolved

    def write_header(self) -> None:
        with self._lock:
            path = self._path
        self._write_header(path)

    def _write_header(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 0:
            return
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            handle.flush()
            os.fsync(handle.fileno())

    def append(self, record: object) -> None:
        with self._lock:
            path = self._path
        self._write_header(path)
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writerow(route_measurement_record_csv_row(record))
            handle.flush()
            os.fsync(handle.fileno())


def route_measurement_record_csv_row(record: object) -> dict[str, str]:
    contact = getattr(record, "contact_quality", None)
    return {
        "timestamp": str(getattr(record, "timestamp")),
        "structure_number": str(getattr(record, "structure_number")),
        "nplc": str(getattr(record, "nplc")),
        "measurement_type": str(getattr(record, "measurement_type")),
        "n_measurements": str(getattr(record, "n_measurements")),
        "resistance_ohm": _format_float(getattr(record, "resistance_ohm")),
        "resistance_rms_ohm": _format_float(
            getattr(record, "resistance_rms_ohm")
        ),
        "relative_rms": _format_float(getattr(record, "relative_rms")),
        "status": str(getattr(record, "status")),
        "contact_quality": "" if contact is None else str(getattr(contact, "status")),
        "contact_median_ohm": ""
        if contact is None
        else _format_float(getattr(contact, "median_ohm")),
        "contact_mad_sigma_ohm": ""
        if contact is None
        else _format_float(getattr(contact, "mad_sigma_ohm")),
        "contact_p95_abs_step_ohm": ""
        if contact is None
        else _format_float(getattr(contact, "p95_abs_step_ohm")),
        "contact_span_ohm": ""
        if contact is None
        else _format_float(getattr(contact, "span_ohm")),
        "contact_compliance_hits": ""
        if contact is None
        else str(getattr(contact, "compliance_hits")),
        "contact_polarity_sign_mismatches": ""
        if contact is None
        else str(getattr(contact, "polarity_sign_mismatch_count")),
    }


def _format_float(value: Any) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(numeric):
        return ""
    return f"{numeric:.12g}"


__all__ = [
    "CSV_FIELDS",
    "RouteMeasurementCsvWriter",
    "route_measurement_record_csv_row",
]
