"""Dockable panel for LCR-guided needle height calibration."""

from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class NeedleCalibrationPanel(QWidget):
    """Control panel for manual needle calibration guided by the LCR meter."""

    connect_requested = Signal()
    disconnect_requested = Signal()
    start_requested = Signal()
    stop_requested = Signal()
    adjust_requested = Signal(float)
    save_current_requested = Signal()
    lower_to_saved_requested = Signal()
    raise_needles_requested = Signal()

    STEP_SIZES_MM = (0.100, 0.020, 0.005)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._lower_direction_sign = -1.0
        self._saved_height: Optional[float] = None
        self._current_height: Optional[float] = None

        root_layout = QVBoxLayout(self)

        self._connection_status = QLabel("LCR: disconnected", self)
        self._resource_label = QLabel("Resource: not set", self)
        self._instrument_label = QLabel("Instrument: n/a", self)
        root_layout.addWidget(self._connection_status)
        root_layout.addWidget(self._resource_label)
        root_layout.addWidget(self._instrument_label)

        connection_layout = QHBoxLayout()
        self._connect_button = QPushButton("Connect LCR", self)
        self._disconnect_button = QPushButton("Disconnect", self)
        self._connect_button.clicked.connect(self.connect_requested.emit)
        self._disconnect_button.clicked.connect(self.disconnect_requested.emit)
        connection_layout.addWidget(self._connect_button)
        connection_layout.addWidget(self._disconnect_button)
        root_layout.addLayout(connection_layout)

        status_group = QGroupBox("Live Status", self)
        status_layout = QGridLayout(status_group)
        status_layout.addWidget(QLabel("Resistance:", self), 0, 0)
        self._resistance_label = QLabel("n/a", self)
        status_layout.addWidget(self._resistance_label, 0, 1)
        status_layout.addWidget(QLabel("Short:", self), 1, 0)
        self._short_label = QLabel("Unknown", self)
        status_layout.addWidget(self._short_label, 1, 1)
        status_layout.addWidget(QLabel("Current A:", self), 2, 0)
        self._current_a_label = QLabel("n/a", self)
        status_layout.addWidget(self._current_a_label, 2, 1)
        status_layout.addWidget(QLabel("Saved down A:", self), 3, 0)
        self._saved_a_label = QLabel("n/a", self)
        status_layout.addWidget(self._saved_a_label, 3, 1)
        root_layout.addWidget(status_group)

        mode_layout = QHBoxLayout()
        self._start_button = QPushButton("Start Calibration", self)
        self._stop_button = QPushButton("Stop", self)
        self._start_button.clicked.connect(self.start_requested.emit)
        self._stop_button.clicked.connect(self.stop_requested.emit)
        mode_layout.addWidget(self._start_button)
        mode_layout.addWidget(self._stop_button)
        root_layout.addLayout(mode_layout)

        adjustment_group = QGroupBox("Needle Steps", self)
        adjustment_layout = QGridLayout(adjustment_group)
        self._lower_buttons: list[QPushButton] = []
        self._raise_buttons: list[QPushButton] = []
        for column, step_mm in enumerate(self.STEP_SIZES_MM):
            lower_button = QPushButton(f"Lower {step_mm:.3f}", self)
            lower_button.clicked.connect(
                lambda checked=False, step=step_mm: self._emit_adjust(True, step)
            )
            self._lower_buttons.append(lower_button)
            adjustment_layout.addWidget(lower_button, 0, column)

            raise_button = QPushButton(f"Raise {step_mm:.3f}", self)
            raise_button.clicked.connect(
                lambda checked=False, step=step_mm: self._emit_adjust(False, step)
            )
            self._raise_buttons.append(raise_button)
            adjustment_layout.addWidget(raise_button, 1, column)
        root_layout.addWidget(adjustment_group)

        action_layout = QHBoxLayout()
        self._save_button = QPushButton("Save Current", self)
        self._save_button.clicked.connect(self.save_current_requested.emit)
        self._raise_button = QPushButton("Raise Needles", self)
        self._raise_button.clicked.connect(self.raise_needles_requested.emit)
        self._lower_button = QPushButton("Lower To Saved", self)
        self._lower_button.clicked.connect(self.lower_to_saved_requested.emit)
        action_layout.addWidget(self._save_button)
        action_layout.addWidget(self._raise_button)
        action_layout.addWidget(self._lower_button)
        root_layout.addLayout(action_layout)

        root_layout.addStretch(1)
        self.set_calibration_active(False)
        self.set_connection_state(False, "", "Disconnected")
        self.set_reading(None, False)
        self.set_current_a(None)
        self.set_saved_height(None)

    def apply_configuration(
        self,
        *,
        resource_name: str,
        saved_height: Optional[float],
        lower_direction: str,
        short_threshold_ohm: float,
    ) -> None:
        """Update the static labels from the application settings."""

        self._resource_label.setText(
            f"Resource: {resource_name}" if resource_name else "Resource: not set"
        )
        self._short_label.setToolTip(f"Short threshold: {short_threshold_ohm:.3f} ohm")
        self._lower_direction_sign = 1.0 if lower_direction == "positive" else -1.0
        self.set_saved_height(saved_height)

    def set_connection_state(
        self, connected: bool, backend_name: str, description: str
    ) -> None:
        """Update the connection status labels."""

        if connected:
            self._connection_status.setText(f"LCR: connected via {backend_name}")
            self._instrument_label.setText(f"Instrument: {description}")
        else:
            self._connection_status.setText("LCR: disconnected")
            self._instrument_label.setText(f"Instrument: {description}")
        self._connect_button.setEnabled(not connected)
        self._disconnect_button.setEnabled(connected)

    def set_reading(self, resistance_ohm: Optional[float], is_short: bool) -> None:
        """Show the latest LCR reading."""

        if resistance_ohm is None:
            self._resistance_label.setText("n/a")
            self._short_label.setText("Unknown")
            self._short_label.setStyleSheet("")
            return
        if not math.isfinite(resistance_ohm):
            self._resistance_label.setText("OL")
        else:
            self._resistance_label.setText(f"{resistance_ohm:.6g} ohm")
        self._short_label.setText("Short" if is_short else "Open")
        if is_short:
            self._short_label.setStyleSheet("QLabel { color: #2e7d32; font-weight: 600; }")
        else:
            self._short_label.setStyleSheet("QLabel { color: #8d6e63; font-weight: 600; }")

    def set_current_a(self, position_mm: Optional[float]) -> None:
        """Update the displayed current A coordinate."""

        self._current_height = position_mm
        if position_mm is None:
            self._current_a_label.setText("n/a")
            self._save_button.setEnabled(False)
            return
        self._current_a_label.setText(f"{position_mm:.4f} mm")
        self._save_button.setEnabled(True)

    def set_saved_height(self, position_mm: Optional[float]) -> None:
        """Update the displayed saved down height."""

        self._saved_height = position_mm
        if position_mm is None:
            self._saved_a_label.setText("n/a")
            self._lower_button.setEnabled(False)
            return
        self._saved_a_label.setText(f"{position_mm:.4f} mm")
        self._lower_button.setEnabled(True)

    def set_calibration_active(self, active: bool) -> None:
        """Enable or disable manual adjustment controls."""

        self._start_button.setEnabled(not active)
        self._stop_button.setEnabled(active)
        for button in self._lower_buttons + self._raise_buttons:
            button.setEnabled(active)

    def _emit_adjust(self, is_lower: bool, step_mm: float) -> None:
        sign = self._lower_direction_sign if is_lower else -self._lower_direction_sign
        self.adjust_requested.emit(sign * step_mm)
