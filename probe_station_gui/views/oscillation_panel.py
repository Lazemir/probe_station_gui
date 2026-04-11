"""Dockable panel for endless stage motion patterns."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class OscillationPanel(QWidget):
    """Small control panel for repeated stage motion patterns."""

    start_requested = Signal(str, float, float, float)
    stop_requested = Signal()
    configuration_changed = Signal(str, float, float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        root_layout = QVBoxLayout(self)

        self._status_label = QLabel("Reciprocation: idle", self)
        root_layout.addWidget(self._status_label)

        form_layout = QFormLayout()

        self._mode_combo = QComboBox(self)
        self._mode_combo.addItem("Along X", "X")
        self._mode_combo.addItem("Along Y", "Y")
        self._mode_combo.addItem("Spiral", "SPIRAL")
        form_layout.addRow("Mode", self._mode_combo)

        self._amplitude_spin = QDoubleSpinBox(self)
        self._amplitude_spin.setDecimals(3)
        self._amplitude_spin.setRange(0.001, 10.0)
        self._amplitude_spin.setSingleStep(0.1)
        self._amplitude_spin.setValue(0.5)
        self._amplitude_spin.setSuffix(" mm")
        form_layout.addRow("Amplitude", self._amplitude_spin)

        self._feedrate_spin = QDoubleSpinBox(self)
        self._feedrate_spin.setDecimals(1)
        self._feedrate_spin.setRange(1.0, 5000.0)
        self._feedrate_spin.setSingleStep(10.0)
        self._feedrate_spin.setValue(120.0)
        self._feedrate_spin.setSuffix(" mm/min")
        form_layout.addRow("Feedrate", self._feedrate_spin)

        self._turns_spin = QDoubleSpinBox(self)
        self._turns_spin.setDecimals(2)
        self._turns_spin.setRange(0.25, 50.0)
        self._turns_spin.setSingleStep(0.25)
        self._turns_spin.setValue(3.0)
        self._turns_spin.setSuffix(" turns")
        form_layout.addRow("Turns per sweep", self._turns_spin)

        root_layout.addLayout(form_layout)

        self._warning_label = QLabel(
            "Special mode: repeated motion with needles down. Stop ends the pattern without returning to the center.",
            self,
        )
        self._warning_label.setWordWrap(True)
        root_layout.addWidget(self._warning_label)

        self._start_button = QPushButton("Start", self)
        self._stop_button = QPushButton("Stop", self)
        self._start_button.clicked.connect(self._emit_start)
        self._stop_button.clicked.connect(self.stop_requested.emit)
        root_layout.addWidget(self._start_button)
        root_layout.addWidget(self._stop_button)
        root_layout.addStretch(1)

        self._mode_combo.currentIndexChanged.connect(self._update_mode_dependent_ui)
        self._mode_combo.currentIndexChanged.connect(self._emit_configuration_changed)
        self._amplitude_spin.valueChanged.connect(self._emit_configuration_changed)
        self._feedrate_spin.valueChanged.connect(self._emit_configuration_changed)
        self._turns_spin.valueChanged.connect(self._emit_configuration_changed)
        self.set_running(False, "")
        self._update_mode_dependent_ui()

    def set_running(self, running: bool, mode: str) -> None:
        """Update the panel state to reflect whether oscillation is active."""

        if running:
            self._status_label.setText(f"Reciprocation: running {mode}")
        else:
            self._status_label.setText("Reciprocation: idle")
        self._mode_combo.setEnabled(not running)
        self._amplitude_spin.setEnabled(not running)
        self._feedrate_spin.setEnabled(not running)
        self._turns_spin.setEnabled(not running and self._is_spiral_mode())
        self._start_button.setEnabled(not running)
        self._stop_button.setEnabled(running)

    def _emit_start(self) -> None:
        self.start_requested.emit(
            str(self._mode_combo.currentData() or "X"),
            self._amplitude_spin.value(),
            self._feedrate_spin.value(),
            self._turns_spin.value(),
        )

    def apply_configuration(
        self,
        *,
        mode: str,
        amplitude_mm: float,
        feedrate_mm_min: float,
        turns_per_sweep: float,
    ) -> None:
        """Load persisted oscillation values into the panel."""

        mode_key = mode.strip().upper()
        index = self._mode_combo.findData(mode_key)
        self._mode_combo.blockSignals(True)
        self._amplitude_spin.blockSignals(True)
        self._feedrate_spin.blockSignals(True)
        self._turns_spin.blockSignals(True)
        if index >= 0:
            self._mode_combo.setCurrentIndex(index)
        self._amplitude_spin.setValue(float(amplitude_mm))
        self._feedrate_spin.setValue(float(feedrate_mm_min))
        self._turns_spin.setValue(float(turns_per_sweep))
        self._mode_combo.blockSignals(False)
        self._amplitude_spin.blockSignals(False)
        self._feedrate_spin.blockSignals(False)
        self._turns_spin.blockSignals(False)
        self._update_mode_dependent_ui()

    def _is_spiral_mode(self) -> bool:
        return str(self._mode_combo.currentData() or "") == "SPIRAL"

    def _update_mode_dependent_ui(self) -> None:
        is_spiral = self._is_spiral_mode()
        self._turns_spin.setEnabled(is_spiral and not self._stop_button.isEnabled())

    def _emit_configuration_changed(self, *_args) -> None:
        self.configuration_changed.emit(
            str(self._mode_combo.currentData() or "X"),
            self._amplitude_spin.value(),
            self._feedrate_spin.value(),
            self._turns_spin.value(),
        )
