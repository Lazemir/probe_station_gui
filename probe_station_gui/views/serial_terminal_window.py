"""Serial terminal window tied to the active FluidNC connection."""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

import serial
from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from ..stage_controller import StageController


class SerialInputLineEdit(QLineEdit):
    """Line edit that emits a signal when Ctrl+X is pressed."""

    control_x_pressed = Signal()

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if (
            event.key() == Qt.Key_X
            and event.modifiers() & Qt.ControlModifier
            and not event.modifiers() & ~Qt.ControlModifier
        ):
            self.control_x_pressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class SerialTerminalWindow(QMainWindow):
    """Floating window that echoes FluidNC serial traffic."""

    POLL_INTERVAL_MS = 100

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Serial Terminal")

        self.serial_connection: Optional[serial.Serial] = None
        self.stage_controller: Optional["StageController"] = None

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self.status_label = QLabel("Disconnected", self)
        layout.addWidget(self.status_label)

        self.terminal_container = QWidget(self)
        terminal_layout = QVBoxLayout(self.terminal_container)
        terminal_layout.setContentsMargins(0, 0, 0, 0)

        self.output_edit = QTextEdit(self)
        self.output_edit.setReadOnly(True)
        terminal_layout.addWidget(self.output_edit)

        input_layout = QHBoxLayout()
        self.input_edit = SerialInputLineEdit(self)
        self.input_edit.setPlaceholderText("Enter command and press Enter")
        self.send_button = QPushButton("Send", self)
        input_layout.addWidget(self.input_edit)
        input_layout.addWidget(self.send_button)
        terminal_layout.addLayout(input_layout)

        layout.addWidget(self.terminal_container)

        self.send_button.clicked.connect(self.send_current_line)
        self.input_edit.returnPressed.connect(self.send_current_line)
        self.input_edit.control_x_pressed.connect(self.send_control_x)

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(self.POLL_INTERVAL_MS)
        self.poll_timer.timeout.connect(self._poll_serial)

        self._update_enabled_state()

    def set_stage_controller(self, stage_controller: Optional["StageController"]) -> None:
        """Assign the stage controller to coordinate serial access."""

        self.stage_controller = stage_controller

    def set_serial(self, serial_connection: Optional[serial.Serial]) -> None:
        """Attach or detach the active serial connection."""

        self.serial_connection = serial_connection
        if serial_connection and serial_connection.is_open:
            self.status_label.setText(
                f"Connected to {serial_connection.port} @ {serial_connection.baudrate}"
            )
            if not self.poll_timer.isActive():
                self.poll_timer.start()
        else:
            self.status_label.setText("Disconnected")
            self.poll_timer.stop()
        self._update_enabled_state()

    def send_control_x(self) -> None:
        """Send a Ctrl+X (soft reset) control character."""

        if not self.serial_connection or not self.serial_connection.is_open:
            self._append_system_message("Cannot send: no active connection.")
            return
        try:
            self.serial_connection.write(b"\x18")
            self.serial_connection.flush()
        except serial.SerialException as error:  # pragma: no cover - safety guard
            self._append_system_message(f"Serial write failed: {error}")
            self.set_serial(None)
            return
        self._append_local_echo("\u2418")

    def send_current_line(self) -> None:
        """Send the typed line to the serial port."""

        if self.stage_controller and self.stage_controller.is_busy():
            self._append_system_message("Cannot send while automated move is running.")
            return
        text = self.input_edit.text()
        if not self.serial_connection or not self.serial_connection.is_open:
            self._append_system_message("Cannot send: no active connection.")
            self.input_edit.selectAll()
            return
        payload = text if text.endswith("\n") else f"{text}\n"
        try:
            self.serial_connection.write(payload.encode("utf-8"))
            self.serial_connection.flush()
        except serial.SerialException as error:  # pragma: no cover - safety guard
            self._append_system_message(f"Serial write failed: {error}")
            self.set_serial(None)
            return
        if text:
            self._append_local_echo(text)
        else:
            self._append_local_echo("\u240d")
        self.input_edit.clear()

    def _append_local_echo(self, message: str) -> None:
        self._append_text(f"→ {message}")

    def _append_system_message(self, message: str) -> None:
        self._append_text(f"[ {message} ]")

    def _append_remote_message(self, data: bytes) -> None:
        decoded = data.decode("utf-8", errors="replace")
        for line in decoded.splitlines(keepends=True):
            self._append_text(line.rstrip("\r\n"))

    def _append_text(self, message: str) -> None:
        self.output_edit.append(message)
        self.output_edit.moveCursor(QTextCursor.End)

    def _poll_serial(self) -> None:
        if not self.serial_connection or not self.serial_connection.is_open:
            self.poll_timer.stop()
            self._update_enabled_state()
            return
        if self.stage_controller and self.stage_controller.is_busy():
            return
        try:
            waiting = self.serial_connection.in_waiting
        except serial.SerialException as error:  # pragma: no cover - safety guard
            self._append_system_message(f"Serial read failed: {error}")
            self.set_serial(None)
            return
        if not waiting:
            return
        try:
            data = self.serial_connection.read(waiting)
        except serial.SerialException as error:  # pragma: no cover - safety guard
            self._append_system_message(f"Serial read failed: {error}")
            self.set_serial(None)
            return
        if data:
            self._append_remote_message(data)

    def _update_enabled_state(self) -> None:
        enabled = bool(self.serial_connection and self.serial_connection.is_open)
        self.terminal_container.setEnabled(enabled)


__all__ = ["SerialTerminalWindow"]
