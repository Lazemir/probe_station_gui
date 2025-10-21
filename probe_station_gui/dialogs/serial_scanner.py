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
    disconnected: Signal = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Serial Scanner")
        self.setModal(True)
        self._serial: serial.Serial | None = None
        self._ports_available = False

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

        self.refresh_button = QPushButton("Refresh")
        self.connect_button = QPushButton("Connect")
        cancel_button = QPushButton("Close")

        self.refresh_button.clicked.connect(self.populate_ports)
        self.connect_button.clicked.connect(self.on_connect_button_clicked)
        cancel_button.clicked.connect(self.reject)

        form = QFormLayout()
        form.addRow("Port", self.port_combo)
        form.addRow("Baud rate", self.baud_combo)

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        button_layout.addWidget(self.refresh_button)
        button_layout.addWidget(self.connect_button)
        button_layout.addWidget(cancel_button)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status_label)
        layout.addLayout(button_layout)

        self.populate_ports()

    def update_ui_state(self) -> None:
        """Toggle the widgets according to the connection state."""

        connected = self._serial is not None and self._serial.is_open
        self.connect_button.setText("Disconnect" if connected else "Connect")

        port_enabled = self._ports_available and not connected
        self.port_combo.setEnabled(port_enabled)
        self.baud_combo.setEnabled(not connected)
        self.refresh_button.setEnabled(not connected)

    def populate_ports(self, clear_status: bool = True) -> None:
        if self._serial is not None and self._serial.is_open:
            return

        self.port_combo.clear()
        ports = list_ports.comports()
        if not ports:
            self.port_combo.addItem("No ports found")
            self._ports_available = False
        else:
            for port in ports:
                description = f"{port.device} — {port.description}"
                self.port_combo.addItem(description, port.device)
            self._ports_available = True
        if clear_status:
            self.status_label.clear()
        self.update_ui_state()

    def on_connect_button_clicked(self) -> None:
        if self._serial is not None and self._serial.is_open:
            self._serial.close()
            self._serial = None
            self.status_label.setText("Disconnected from board.")
            self.populate_ports(clear_status=False)
            self.disconnected.emit()
            self.update_ui_state()
            return

        if not self.port_combo.isEnabled():
            self.status_label.setText("No serial ports available. Use Refresh to scan again.")
            return

        port_name = self.port_combo.currentData()
        if port_name is None:
            port_name = self.port_combo.currentText().split(" ")[0]
        baud_rate = int(self.baud_combo.currentText())

        try:
            self._serial = serial.Serial(port=port_name, baudrate=baud_rate, timeout=1)
        except serial.SerialException as exc:
            self.status_label.setText(f"Connection failed: {exc}")
            return

        self.status_label.setText(f"Connected to {port_name} @ {baud_rate} baud.")
        self.update_ui_state()
        self.connected.emit(self._serial)

    def handle_external_disconnect(self, message: str | None = None) -> None:
        if self._serial is not None and self._serial.is_open:
            self._serial.close()
        self._serial = None
        if message is not None:
            self.status_label.setText(message)
        elif not self.status_label.text():
            self.status_label.setText("Disconnected from board.")
        self.populate_ports(clear_status=False)
        self.update_ui_state()

__all__ = ["SerialScannerDialog"]
