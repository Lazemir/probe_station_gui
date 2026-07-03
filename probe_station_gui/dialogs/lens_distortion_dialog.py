"""Lens distortion calibration dialog."""

from __future__ import annotations

import math
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.settings.manager import ObjectivesSettings
from probe_station_gui.settings.objective_config import normalize_objective_name


class LensDistortionDialog(QDialog):
    """Small active-objective lens distortion calibration panel."""

    calibrate_requested: Signal = Signal()
    reset_requested: Signal = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Lens Distortion Calibration")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._objective_label = self._value_label()
        self._status_label = self._value_label()
        self._mean_error_label = self._value_label()
        self._max_error_label = self._value_label()
        self._message_label = QLabel("", self)
        self._message_label.setWordWrap(True)

        form.addRow(QLabel("Objective", self), self._objective_label)
        form.addRow(QLabel("Lens correction", self), self._status_label)
        form.addRow(QLabel("Mean error", self), self._mean_error_label)
        form.addRow(QLabel("Max error", self), self._max_error_label)
        layout.addLayout(form)
        layout.addWidget(self._message_label)

        button_layout = QHBoxLayout()
        self._calibrate_button = QPushButton("Calibrate", self)
        self._reset_button = QPushButton("Reset", self)
        close_button = QPushButton("Close", self)
        button_layout.addWidget(self._calibrate_button)
        button_layout.addWidget(self._reset_button)
        button_layout.addStretch(1)
        button_layout.addWidget(close_button)
        layout.addLayout(button_layout)

        self._calibrate_button.clicked.connect(self.calibrate_requested.emit)
        self._reset_button.clicked.connect(self.reset_requested.emit)
        close_button.clicked.connect(self.close)
        self._set_empty()

    def set_running(self, running: bool) -> None:
        self._calibrate_button.setEnabled(not running)
        self._reset_button.setEnabled(
            not running and self._status_label.text() == "Configured"
        )

    def set_status(self, message: str) -> None:
        self._message_label.setText(str(message or ""))

    def set_objectives(self, objectives: ObjectivesSettings) -> None:
        active_name = normalize_objective_name(objectives.active_name)
        profile = objectives.objectives.get(active_name)
        if not active_name or profile is None:
            self._set_empty()
            return

        self._objective_label.setText(active_name)
        configured = bool(profile.distortion_correction_configured)
        self._status_label.setText("Configured" if configured else "Not configured")
        payload = profile.distortion_correction if configured else {}
        self._mean_error_label.setText(
            self._format_pixel_error(payload.get("residual_mean_px"))
        )
        self._max_error_label.setText(
            self._format_pixel_error(payload.get("residual_max_px"))
        )
        self._reset_button.setEnabled(configured)

    def _set_empty(self) -> None:
        self._objective_label.setText("--")
        self._status_label.setText("Not configured")
        self._mean_error_label.setText("--")
        self._max_error_label.setText("--")
        self._reset_button.setEnabled(False)

    def _value_label(self) -> QLabel:
        label = QLabel("--", self)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return label

    @staticmethod
    def _format_pixel_error(value: Any) -> str:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return "--"
        if not math.isfinite(numeric):
            return "--"
        return f"{numeric:.3g} px"
