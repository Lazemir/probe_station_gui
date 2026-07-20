"""Dialog for configuring a microscope tile scan over the loaded design."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QLocale, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from probe_station_gui.shared.wheel_guard import GuardedDoubleSpinBox as QDoubleSpinBox


@dataclass(frozen=True)
class MicroscopeScanConfiguration:
    """Per-run design scan configuration from the dialog."""

    output_dir: str
    overlap_fraction: float
    settle_s: float


class MicroscopeScanDialog(QDialog):
    """Small non-modal scan setup and status dialog."""

    scan_requested = Signal(object)
    stop_requested = Signal()

    def __init__(
        self,
        *,
        default_output_dir: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Microscope Scan")
        self.setModal(False)
        self._running = False

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        output_row = QHBoxLayout()
        self._output_dir_edit = QLineEdit(self)
        self._output_dir_edit.setText(default_output_dir)
        self._output_dir_edit.setPlaceholderText("scan output directory")
        self._browse_button = QPushButton("Browse", self)
        output_row.addWidget(self._output_dir_edit, 1)
        output_row.addWidget(self._browse_button)
        form.addRow(QLabel("Output", self), output_row)

        self._overlap_spin = QDoubleSpinBox(self)
        self._overlap_spin.setLocale(QLocale.c())
        self._overlap_spin.setDecimals(1)
        self._overlap_spin.setRange(0.0, 80.0)
        self._overlap_spin.setSingleStep(5.0)
        self._overlap_spin.setSuffix(" %")
        self._overlap_spin.setValue(25.0)
        form.addRow(QLabel("Overlap", self), self._overlap_spin)

        self._settle_spin = QDoubleSpinBox(self)
        self._settle_spin.setLocale(QLocale.c())
        self._settle_spin.setDecimals(3)
        self._settle_spin.setRange(0.0, 10.0)
        self._settle_spin.setSingleStep(0.05)
        self._settle_spin.setSuffix(" s")
        self._settle_spin.setValue(0.2)
        form.addRow(QLabel("Settle", self), self._settle_spin)

        layout.addLayout(form)

        self._status_label = QLabel("Idle.", self)
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        button_row = QHBoxLayout()
        self._start_button = QPushButton("Start", self)
        self._stop_button = QPushButton("Stop", self)
        self._close_button = QPushButton("Close", self)
        self._stop_button.setEnabled(False)
        button_row.addWidget(self._start_button)
        button_row.addWidget(self._stop_button)
        button_row.addStretch(1)
        button_row.addWidget(self._close_button)
        layout.addLayout(button_row)

        self._browse_button.clicked.connect(self._choose_output_dir)
        self._start_button.clicked.connect(self._emit_scan_requested)
        self._stop_button.clicked.connect(self.stop_requested.emit)
        self._close_button.clicked.connect(self.close)
        self.resize(560, 180)

    def current_configuration(self) -> MicroscopeScanConfiguration:
        return MicroscopeScanConfiguration(
            output_dir=self._output_dir_edit.text().strip(),
            overlap_fraction=float(self._overlap_spin.value()) / 100.0,
            settle_s=float(self._settle_spin.value()),
        )

    def set_status(self, message: str) -> None:
        self._status_label.setText(message or "Idle.")

    def set_running(self, running: bool) -> None:
        self._running = bool(running)
        for widget in (
            self._output_dir_edit,
            self._browse_button,
            self._overlap_spin,
            self._settle_spin,
        ):
            widget.setEnabled(not self._running)
        self._start_button.setEnabled(not self._running)
        self._stop_button.setEnabled(self._running)

    def _choose_output_dir(self) -> None:
        current = self._output_dir_edit.text().strip()
        start = current or str(Path.cwd())
        path = QFileDialog.getExistingDirectory(self, "Microscope Scan Output", start)
        if path:
            self._output_dir_edit.setText(path)

    def _emit_scan_requested(self) -> None:
        config = self.current_configuration()
        if not config.output_dir:
            self.set_status("Choose an output directory before scanning.")
            return
        self.scan_requested.emit(config)


__all__ = ["MicroscopeScanConfiguration", "MicroscopeScanDialog"]
