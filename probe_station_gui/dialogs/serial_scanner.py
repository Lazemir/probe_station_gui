"""Dialog for scanning and connecting to serial ports."""

from __future__ import annotations

import serial
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)
from serial.tools import list_ports


class SerialScannerDialog(QDialog):
    """Dialog that scans for serial ports and allows connecting to one."""

    connected: Signal = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Serial Scanner")
        self.setModal(True)
        self._serial: serial.Serial | None = None

        self.port_combo = QComboBox()
        self.baud_combo = QComboBox()
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)

        baud_rates = [
            "250000",
            "230400",
            "200000",
            "128000",
            "115200",
            "57600",
            "38400",
            "19200",
            "9600",
        ]
        self.baud_combo.addItems(baud_rates)
        self.baud_combo.setCurrentIndex(baud_rates.index("115200"))

        refresh_button = QPushButton("Refresh")
        connect_button = QPushButton("Connect")
        cancel_button = QPushButton("Cancel")

        refresh_button.clicked.connect(self.populate_ports)
        connect_button.clicked.connect(self.on_connect)
        cancel_button.clicked.connect(self.reject)

        form = QFormLayout()
        form.addRow("Port", self.port_combo)
        form.addRow("Baud rate", self.baud_combo)

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        button_layout.addWidget(refresh_button)
        button_layout.addWidget(connect_button)
        button_layout.addWidget(cancel_button)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status_label)
        layout.addLayout(button_layout)

        self.populate_ports()

    def populate_ports(self) -> None:
        self.port_combo.clear()
        ports = list_ports.comports()
        if not ports:
            self.port_combo.addItem("No ports found")
            self.port_combo.setEnabled(False)
        else:
            self.port_combo.setEnabled(True)
            for port in ports:
                description = f"{port.device} — {port.description}"
                self.port_combo.addItem(description, port.device)
        self.status_label.clear()

    def on_connect(self) -> None:
        if not self.port_combo.isEnabled():
            self.status_label.setText("No serial ports available. Use Refresh to scan again.")
            return

        port_name = self.port_combo.currentData()
        if port_name is None:
            port_name = self.port_combo.currentText().split(" ")[0]
        baud_rate = int(self.baud_combo.currentText())

        if self._serial and self._serial.is_open:
            self._serial.close()
            self._serial = None

        try:
            self._serial = serial.Serial(port=port_name, baudrate=baud_rate, timeout=1)
        except serial.SerialException as exc:
            self.status_label.setText(f"Connection failed: {exc}")
            return

        self.status_label.setText(f"Connected to {port_name} @ {baud_rate} baud")
        self.connected.emit(self._serial)
        self._serial = None
        self.accept()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._serial and self._serial.is_open:
            self._serial.close()
            self._serial = None
        super().closeEvent(event)


__all__ = ["SerialScannerDialog"]
