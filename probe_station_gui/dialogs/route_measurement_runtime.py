"""Runtime progress, result, and raw-sample presentation for route runs."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from tqdm import tqdm

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.dialogs.route_measurement_result_views import (
    RouteMeasurementHistogram,
    RouteMeasurementRawDataDialog,
)
from probe_station_gui.route.formatting import (
    format_route_ohm,
    format_route_percent,
)
from probe_station_gui.route.measurement_display import parse_tqdm_interval
from probe_station_gui.route.measurement_records import RouteMeasurementRecord
from probe_station_gui.shared.wheel_guard import GuardedComboBox as QComboBox


class RouteMeasurementRuntimeView(QWidget):
    """Own route progress timing, result formatting, and sample inspection."""

    def __init__(
        self,
        *,
        route_point_count: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._last_raw_samples: tuple[object, ...] = ()
        self._progress_started_at: float | None = None
        self._progress_baseline_completed: int | None = None
        self._progress_total = max(1, int(route_point_count))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._status_label = QLabel("Idle.", self)
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)
        self._progress_label = QLabel("Progress: idle.", self)
        self._progress_label.setWordWrap(True)
        layout.addWidget(self._progress_label)
        self._progress_bar = QProgressBar(self)
        self._progress_bar.setRange(0, self._progress_total)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("0/%m")
        self._progress_bar.setStyleSheet(
            "QProgressBar { border: 1px solid #6b7280; border-radius: 4px; "
            "background: #111827; color: #f9fafb; min-height: 20px; "
            "text-align: center; } QProgressBar::chunk { border-radius: 3px; "
            "background: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 0, "
            "stop: 0 #16a34a, stop: 0.55 #0891b2, stop: 1 #4f46e5); }"
        )
        layout.addWidget(self._progress_bar)
        self._progress_eta_label = QLabel("Remaining: -- | Finish: --", self)
        self._progress_eta_label.setWordWrap(True)
        layout.addWidget(self._progress_eta_label)
        self._result_label = QLabel("Last result: none.", self)
        self._result_label.setWordWrap(True)
        layout.addWidget(self._result_label)

        analysis_row = QHBoxLayout()
        self._histogram_mode_combo = QComboBox(self)
        self._histogram_mode_combo.addItem("Differential dV/dI", "differential")
        self._histogram_mode_combo.addItem("Polarity V/I", "polarity")
        self._raw_data_button = QPushButton("Raw Data", self)
        self._raw_data_button.setEnabled(False)
        analysis_row.addWidget(QLabel("Histogram", self))
        analysis_row.addWidget(self._histogram_mode_combo)
        analysis_row.addWidget(self._raw_data_button)
        analysis_row.addStretch(1)
        layout.addLayout(analysis_row)
        self._histogram_widget = RouteMeasurementHistogram(self)
        self._histogram_widget.setMinimumHeight(170)
        layout.addWidget(self._histogram_widget)

        self._histogram_mode_combo.currentIndexChanged.connect(
            self._update_histogram_mode
        )
        self._raw_data_button.clicked.connect(self._show_raw_data)

    def set_status(self, message: str) -> None:
        self._status_label.setText(message or "Idle.")

    def reset_progress(self, total: int | None = None) -> None:
        self._progress_started_at = None
        self._progress_baseline_completed = None
        if total is not None:
            self._progress_total = max(1, int(total))
        self._progress_bar.setRange(0, self._progress_total)
        self._progress_bar.setValue(0)
        self._progress_bar.setFormat(f"0/{self._progress_total} (0%)")
        self._progress_label.setText("Progress: waiting for first route point.")
        self._progress_eta_label.setText("Remaining: -- | Finish: --")

    def set_progress(self, position: int, total: int, point_number: int) -> None:
        total_points = max(1, int(total))
        position_value = min(max(1, int(position)), total_points)
        completed = min(max(0, position_value - 1), total_points)
        self._progress_total = total_points
        if self._progress_started_at is None:
            self._progress_started_at = time.monotonic()
            self._progress_baseline_completed = completed
        self._progress_bar.setRange(0, total_points)
        self._progress_bar.setValue(completed)
        percent = int(round((completed / total_points) * 100.0))
        self._progress_bar.setFormat(f"{completed}/{total_points} ({percent}%)")
        self._progress_label.setText(
            f"Progress: point {position_value}/{total_points}, "
            f"route point {int(point_number)}."
        )
        self._progress_eta_label.setText(
            self._progress_eta_text(completed, total_points)
        )

    def finish_progress(self, success: bool) -> None:
        total_points = max(1, int(self._progress_total))
        if success:
            self._progress_bar.setRange(0, total_points)
            self._progress_bar.setValue(total_points)
            self._progress_bar.setFormat(f"{total_points}/{total_points} (100%)")
            self._progress_label.setText("Progress: complete.")
            self._progress_eta_label.setText("Remaining: 00:00 | Finish: now")
        else:
            self._progress_label.setText("Progress: stopped.")

    def set_result(
        self,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        saved: bool,
    ) -> None:
        prefix = "Saved" if saved else "Not saved"
        contact = record.contact_quality
        contact_text = ""
        if contact is not None and contact.assessed:
            reasons = contact.reasons
            reason_text = ", ".join(str(reason) for reason in reasons) or "none"
            contact_text = (
                f", contact={contact.status}, "
                f"reasons={reason_text}, "
                f"median={format_route_ohm(contact.median_ohm)}, "
                f"MAD={format_route_ohm(contact.mad_sigma_ohm)}, "
                f"p95 step={format_route_ohm(contact.p95_abs_step_ohm)}, "
                f"span={format_route_ohm(contact.span_ohm)}, "
                f"compliance hits={contact.compliance_hits}, "
                "polarity mismatches="
                f"{contact.polarity_sign_mismatch_count}"
            )
        self._result_label.setText(
            f"{prefix} point {position}/{total}: "
            f"R={format_route_ohm(record.resistance_ohm)}, "
            f"RMS={format_route_ohm(record.resistance_rms_ohm)}, "
            f"rel={format_route_percent(record.relative_rms)}, "
            f"status={record.status or 'unknown'}{contact_text}."
        )
        self._last_raw_samples = record.raw_samples
        self._histogram_widget.set_samples(self._last_raw_samples)
        self._raw_data_button.setEnabled(bool(self._last_raw_samples))

    def _progress_eta_text(self, completed: int, total: int) -> str:
        started_at = self._progress_started_at
        baseline = self._progress_baseline_completed
        if started_at is None or baseline is None:
            return "Remaining: ETA after first point | Finish: --"
        total_points = max(1, int(total))
        baseline_completed = min(max(0, int(baseline)), total_points)
        completed_since_start = min(
            max(0, int(completed) - baseline_completed),
            max(0, total_points - baseline_completed),
        )
        total_since_start = max(1, total_points - baseline_completed)
        if completed_since_start <= 0:
            return "Remaining: ETA after first point | Finish: --"
        elapsed_s = max(0.0, time.monotonic() - started_at)
        if elapsed_s <= 0.0:
            return "Remaining: ETA after first point | Finish: --"
        meter = tqdm.format_meter(
            completed_since_start,
            total_since_start,
            elapsed_s,
            ascii=True,
            ncols=0,
        )
        remaining_text = self._tqdm_remaining_text(meter)
        remaining_s = parse_tqdm_interval(remaining_text)
        if remaining_text is None or remaining_s is None:
            return "Remaining: ETA after first point | Finish: --"
        finish_at = datetime.now().astimezone() + timedelta(seconds=remaining_s)
        return f"Remaining: {remaining_text} | Finish: {finish_at:%Y-%m-%d %H:%M:%S %Z}"

    @staticmethod
    def _tqdm_remaining_text(meter: str) -> str | None:
        marker_index = meter.find("<")
        if marker_index < 0:
            return None
        remaining_start = marker_index + 1
        remaining_end = meter.find(",", remaining_start)
        if remaining_end < 0:
            return None
        remaining = meter[remaining_start:remaining_end].strip()
        return remaining if remaining and "?" not in remaining else None

    def _update_histogram_mode(self, *_args: object) -> None:
        mode = str(self._histogram_mode_combo.currentData() or "differential")
        self._histogram_widget.set_mode(mode)

    def _show_raw_data(self) -> None:
        if not self._last_raw_samples:
            self.set_status("No raw measurement data yet.")
            return
        RouteMeasurementRawDataDialog(self._last_raw_samples, self).exec()


__all__ = ["RouteMeasurementRuntimeView"]
