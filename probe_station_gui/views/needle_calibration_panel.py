"""Compact manual panel for the saved needle contact position."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
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
    """Control panel for manual needle contact calibration."""

    connect_requested = Signal()
    disconnect_requested = Signal()
    start_requested = Signal()
    stop_requested = Signal()
    adjust_requested = Signal(float)
    save_current_requested = Signal()
    lower_to_saved_requested = Signal()
    raise_needles_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._saved_height: Optional[float] = None
        self._current_height: Optional[float] = None

        root_layout = QVBoxLayout(self)

        status_group = QGroupBox("Needle Contact", self)
        status_layout = QGridLayout(status_group)
        status_layout.addWidget(QLabel("Current lowering:", self), 0, 0)
        self._current_a_label = QLabel("n/a", self)
        status_layout.addWidget(self._current_a_label, 0, 1)
        status_layout.addWidget(QLabel("Saved lowering:", self), 1, 0)
        self._saved_a_label = QLabel("n/a", self)
        status_layout.addWidget(self._saved_a_label, 1, 1)
        root_layout.addWidget(status_group)

        action_layout = QHBoxLayout()
        self._save_button = QPushButton("Save Current", self)
        self._save_button.clicked.connect(self.save_current_requested.emit)
        self._raise_button = QPushButton("Raise", self)
        self._raise_button.clicked.connect(self.raise_needles_requested.emit)
        self._lower_button = QPushButton("Lower", self)
        self._lower_button.clicked.connect(self.lower_to_saved_requested.emit)
        action_layout.addWidget(self._save_button)
        action_layout.addWidget(self._raise_button)
        action_layout.addWidget(self._lower_button)
        root_layout.addLayout(action_layout)

        root_layout.addStretch(1)
        self.set_current_a(None)
        self.set_saved_height(None)

    def apply_configuration(
        self,
        *,
        resource_name: str,
        saved_height: Optional[float],
        short_threshold_ohm: float,
        measurement_function: str = "DCR",
    ) -> None:
        """Update the saved contact height from application settings."""

        del resource_name, short_threshold_ohm, measurement_function
        self.set_saved_height(saved_height)

    def set_connection_state(
        self, connected: bool, backend_name: str, description: str
    ) -> None:
        """Accept legacy LCR state updates without showing LCR controls."""

        del connected, backend_name, description

    def set_reading(self, resistance_ohm: Optional[float], is_short: bool) -> None:
        """Accept legacy LCR reading updates without showing LCR controls."""

        del resistance_ohm, is_short

    def set_current_a(self, position_mm: Optional[float]) -> None:
        """Update the displayed current physical A-axis lowering."""

        self._current_height = position_mm
        if position_mm is None:
            self._current_a_label.setText("n/a")
            self._save_button.setEnabled(False)
            return
        self._current_a_label.setText(f"{position_mm:.4f} mm")
        self._save_button.setEnabled(True)

    def set_saved_height(self, position_mm: Optional[float]) -> None:
        """Update the displayed saved physical down height."""

        self._saved_height = position_mm
        if position_mm is None:
            self._saved_a_label.setText("n/a")
            self._lower_button.setEnabled(False)
            return
        self._saved_a_label.setText(f"{position_mm:.4f} mm")
        self._lower_button.setEnabled(True)

    def set_calibration_active(self, active: bool) -> None:
        """Retain the legacy API; manual mode has no active session."""

        del active
