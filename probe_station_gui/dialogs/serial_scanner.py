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
        self._status_message = "Not connected."
        self._connected_port_name: str | None = None
        self._connected_baud: int | None = None
        self._connection_active: bool = False

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
        self.connect_button = QPushButton("Connect")
        cancel_button = QPushButton("Cancel")

        refresh_button.clicked.connect(self.populate_ports)
        self.connect_button.clicked.connect(self.on_connect)
        cancel_button.clicked.connect(self.reject)

        form = QFormLayout()
        form.addRow("Port", self.port_combo)
        form.addRow("Baud rate", self.baud_combo)

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        button_layout.addWidget(refresh_button)
        button_layout.addWidget(self.connect_button)
        button_layout.addWidget(cancel_button)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status_label)
        layout.addLayout(button_layout)

        self.populate_ports()

    def set_connection(
        self,
        serial_port: serial.Serial | None,
        *,
        port_name: str | None = None,
        baud_rate: int | None = None,
        connected: bool | None = None,
    ) -> None:
        """Update the dialog to reflect an externally managed connection."""

        if serial_port is not None:
            port_name = port_name or serial_port.port
            baud_rate = baud_rate or int(serial_port.baudrate)
            is_connected = connected if connected is not None else serial_port.is_open
        else:
            is_connected = bool(connected)

        self._connected_port_name = port_name
        self._connected_baud = baud_rate
        self._connection_active = bool(is_connected)

        if self._connection_active and self._connected_port_name and self._connected_baud:
            self._status_message = (
                f"Connected to {self._connected_port_name} @ {self._connected_baud} baud"
            )
        else:
            self._status_message = "Not connected."

        self._apply_connection_state()

    def populate_ports(self) -> None:
        self.port_combo.clear()
        ports = list_ports.comports()
        if not ports:
            self.port_combo.addItem("No ports found")
            self.port_combo.setEnabled(False)
            self.connect_button.setEnabled(False)
        else:
            self.port_combo.setEnabled(True)
            self.connect_button.setEnabled(True)
            for port in ports:
                description = f"{port.device} — {port.description}"
                self.port_combo.addItem(description, port.device)
        self._apply_connection_state()

    def on_connect(self) -> None:
        if not self.port_combo.isEnabled():
            self._set_status("No serial ports available. Use Refresh to scan again.")
            return

        port_name = self.port_combo.currentData()
        if port_name is None:
            port_name = self.port_combo.currentText().split(" ")[0]
        baud_rate = int(self.baud_combo.currentText())

        if (
            self._connection_active
            and self._connected_port_name == port_name
            and self._connected_baud == baud_rate
        ):
            self._set_status(
                f"Already connected to {port_name} @ {baud_rate} baud. Close this dialog to continue."
            )
            return

        if self._serial and self._serial.is_open:
            self._serial.close()
            self._serial = None

        try:
            self._serial = serial.Serial(port=port_name, baudrate=baud_rate, timeout=1)
        except serial.SerialException as exc:
            self._set_status(f"Connection failed: {exc}")
            return

        self._connected_port_name = port_name
        self._connected_baud = baud_rate
        self._connection_active = True
        self._set_status(f"Connected to {port_name} @ {baud_rate} baud")
        self._update_connect_button()
        self.connected.emit(self._serial)
        self._serial = None
        self.accept()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._serial and self._serial.is_open:
            self._serial.close()
            self._serial = None
        super().closeEvent(event)

    def _apply_connection_state(self) -> None:
        """Sync the UI widgets with the saved connection state."""

        if self._connected_baud is not None:
            baud_index = self.baud_combo.findText(str(self._connected_baud))
            if baud_index >= 0:
                self.baud_combo.setCurrentIndex(baud_index)

        if self._connected_port_name is not None:
            port_index = self.port_combo.findData(self._connected_port_name)
            if port_index < 0:
                port_index = self._find_port_by_text(self._connected_port_name)
            if port_index >= 0:
                self.port_combo.setCurrentIndex(port_index)
            elif self.port_combo.isEnabled():
                self.port_combo.insertItem(0, self._connected_port_name, self._connected_port_name)
                self.port_combo.setCurrentIndex(0)

        self.status_label.setText(self._status_message)
        self._update_connect_button()

    def _find_port_by_text(self, port_name: str) -> int:
        for index in range(self.port_combo.count()):
            text = self.port_combo.itemText(index)
            if text.startswith(port_name):
                return index
        return -1

    def _set_status(self, message: str) -> None:
        self._status_message = message
        self.status_label.setText(message)
        self._update_connect_button()

    def _update_connect_button(self) -> None:
        if self._connection_active and self._connected_port_name:
            self.connect_button.setText("Reconnect")
        else:
            self.connect_button.setText("Connect")


__all__ = ["SerialScannerDialog"]
