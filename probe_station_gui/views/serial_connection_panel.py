"""Dockable panel for managing FluidNC serial connections."""

from __future__ import annotations

from typing import Optional

import serial
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from serial.tools import list_ports


class SerialConnectionPanel(QWidget):
    """Widget that embeds serial scanning and connection controls."""

    connected: Signal = Signal(object)
    disconnected: Signal = Signal()

    _AUTO_RECONNECT_DELAY_MS = 1500

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self._serial: Optional[serial.Serial] = None
        self._ports_available = False
        self._ports_cache = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self.status_label = QLabel("Disconnected", self)
        layout.addWidget(self.status_label)

        self.port_combo = QComboBox(self)
        self.baud_combo = QComboBox(self)
        self.baud_combo.addItems(
            [
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
        )
        self.baud_combo.setCurrentText("115200")

        layout.addWidget(QLabel("Port", self))
        layout.addWidget(self.port_combo)
        layout.addWidget(QLabel("Baud rate", self))
        layout.addWidget(self.baud_combo)

        button_row = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh", self)
        self.connect_button = QPushButton("Connect", self)
        button_row.addWidget(self.refresh_button)
        button_row.addWidget(self.connect_button)
        layout.addLayout(button_row)

        self.refresh_button.clicked.connect(self.populate_ports)
        self.connect_button.clicked.connect(self.on_connect_clicked)

        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.timeout.connect(self.auto_connect)

        self.populate_ports()

    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def populate_ports(self) -> None:
        if self._serial is not None and self._serial.is_open:
            return

        self.port_combo.clear()
        ports = list(list_ports.comports())
        self._ports_cache = ports
        if not ports:
            self.port_combo.addItem("No ports found")
            self._ports_available = False
        else:
            for port in ports:
                description = f"{port.device} — {port.description}"
                self.port_combo.addItem(description, port.device)
            self._ports_available = True
        self.status_label.setText(
            "Disconnected" if not self.status_label.text() else self.status_label.text()
        )
        self._update_ui_state()

    def on_connect_clicked(self) -> None:
        if self._serial is not None and self._serial.is_open:
            self._auto_timer.stop()
            self._serial.close()
            self._serial = None
            self.status_label.setText("Disconnected from board.")
            self.populate_ports()
            self.disconnected.emit()
            return

        if not self._ports_available:
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
            self._serial = None
            return

        self.status_label.setText(f"Connected to {port_name} @ {baud_rate} baud.")
        self._update_ui_state()
        self._auto_timer.stop()
        self.connected.emit(self._serial)

    def auto_connect(self) -> None:
        if self._serial is not None and self._serial.is_open:
            return
        if not self._ports_available:
            self.populate_ports()
            if not self._ports_available:
                self._auto_timer.start(self._AUTO_RECONNECT_DELAY_MS)
                return

        target_index = 0
        for index, port in enumerate(self._ports_cache):
            description = " ".join(filter(None, [port.description, port.manufacturer]))
            if description and "fluid" in description.lower():
                target_index = index
                break
        if self.port_combo.count() and 0 <= target_index < self.port_combo.count():
            self.port_combo.setCurrentIndex(target_index)

        self.on_connect_clicked()
        if self._serial is None or not self._serial.is_open:
            self._auto_timer.start(self._AUTO_RECONNECT_DELAY_MS)

    def handle_external_disconnect(self, auto_retry: bool = True) -> None:
        if self._serial is not None and self._serial.is_open:
            self._serial.close()
        self._serial = None
        self.status_label.setText("Disconnected from board.")
        self.populate_ports()
        if auto_retry:
            self._auto_timer.start(self._AUTO_RECONNECT_DELAY_MS)

    def _update_ui_state(self) -> None:
        connected = self._serial is not None and self._serial.is_open
        self.connect_button.setText("Disconnect" if connected else "Connect")
        self.port_combo.setEnabled(not connected and self._ports_available)
        self.baud_combo.setEnabled(not connected)
        self.refresh_button.setEnabled(not connected)

    def shutdown(self) -> None:
        self._auto_timer.stop()
        if self._serial is not None and self._serial.is_open:
            self._serial.close()
        self._serial = None


__all__ = ["SerialConnectionPanel"]
