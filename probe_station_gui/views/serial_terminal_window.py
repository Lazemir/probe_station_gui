"""Serial terminal window tied to the active FluidNC connection."""

from __future__ import annotations

import logging
import time
from typing import Optional, TYPE_CHECKING

import serial
from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from probe_station_gui.stage.controller import StageController


logger = logging.getLogger(__name__)


class SerialInputLineEdit(QLineEdit):
    """Line edit that emits signals for control commands."""

    control_x_pressed = Signal()
    history_previous_requested = Signal()
    history_next_requested = Signal()

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if (
            event.key() == Qt.Key_X
            and event.modifiers() & Qt.ControlModifier
            and not event.modifiers() & ~Qt.ControlModifier
        ):
            self.control_x_pressed.emit()
            event.accept()
            return
        if event.key() == Qt.Key_Up and event.modifiers() == Qt.NoModifier:
            self.history_previous_requested.emit()
            event.accept()
            return
        if event.key() == Qt.Key_Down and event.modifiers() == Qt.NoModifier:
            self.history_next_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class SerialTerminalWindow(QWidget):
    """Widget that echoes FluidNC serial traffic."""

    manual_command_sent = Signal(str)
    POLL_INTERVAL_MS = 100
    MANUAL_RESPONSE_TIMEOUT_S = 0.8
    MANUAL_RESPONSE_IDLE_GRACE_S = 0.2
    RESET_RESPONSE_TIMEOUT_S = 2.5

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self.serial_connection: Optional[serial.Serial] = None
        self.stage_controller: Optional["StageController"] = None
        self._poll_paused = False
        self._capture_manual_output = False
        self._capture_deadline = 0.0

        layout = QVBoxLayout(self)

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
        self.input_edit.history_previous_requested.connect(
            self._show_previous_history_entry
        )
        self.input_edit.history_next_requested.connect(
            self._show_next_history_entry
        )

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(self.POLL_INTERVAL_MS)
        self.poll_timer.timeout.connect(self._poll_serial)

        self._update_enabled_state()

        self._command_history: list[str] = []
        self._history_position: int = 0

    def set_stage_controller(self, stage_controller: Optional["StageController"]) -> None:
        """Assign the stage controller to coordinate serial access."""

        self.stage_controller = stage_controller
        self._sync_poll_timer()

    def set_serial(self, serial_connection: Optional[serial.Serial]) -> None:
        """Attach or detach the active serial connection."""

        self.serial_connection = serial_connection
        self._capture_manual_output = False
        self._capture_deadline = 0.0
        if serial_connection and serial_connection.is_open:
            self.status_label.setText(
                f"Connected to {serial_connection.port} @ {serial_connection.baudrate}"
            )
        else:
            self.status_label.setText("Disconnected")
        self._update_enabled_state()
        self._sync_poll_timer()

    def set_live_poll_paused(self, paused: bool) -> None:
        """Keep terminal background reads disabled while sharing the controller serial."""

        self._poll_paused = bool(paused)
        self._sync_poll_timer()

    def send_control_x(self) -> None:
        """Send a Ctrl+X (soft reset) control character."""

        if not self.serial_connection or not self.serial_connection.is_open:
            self._append_system_message("Cannot send: no active connection.")
            return
        if self.stage_controller is not None:
            try:
                self.stage_controller.queue_soft_reset(source="serial_terminal_ctrl_x")
            except Exception as error:  # pragma: no cover - UI safety guard
                self._append_system_message(str(error))
                return
            self._arm_manual_response_capture(self.RESET_RESPONSE_TIMEOUT_S)
            self.manual_command_sent.emit("CTRL-X")
            self._append_local_echo("\u2418")
            return
        try:
            self.serial_connection.write(b"\x18")
            self.serial_connection.flush()
            logger.debug("SERIAL TRACE terminal_write CTRL-X")
        except serial.SerialException as error:  # pragma: no cover - safety guard
            self._append_system_message(f"Serial write failed: {error}")
            self.set_serial(None)
            return
        self._arm_manual_response_capture(self.RESET_RESPONSE_TIMEOUT_S)
        self.manual_command_sent.emit("CTRL-X")
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
        if self.stage_controller is not None:
            try:
                self.stage_controller.queue_manual_command(text)
            except Exception as error:  # pragma: no cover - UI safety guard
                self._append_system_message(str(error))
                self.input_edit.selectAll()
                return
            self._arm_manual_response_capture(self.MANUAL_RESPONSE_TIMEOUT_S)
            self.manual_command_sent.emit(text or "\u240d")
            if text:
                self._append_local_echo(text)
                self._command_history.append(text)
            else:
                self._append_local_echo("\u240d")
            self._history_position = len(self._command_history)
            self.input_edit.clear()
            return
        payload = text if text.endswith("\n") else f"{text}\n"
        try:
            self.serial_connection.write(payload.encode("utf-8"))
            self.serial_connection.flush()
            logger.debug("SERIAL TRACE terminal_write payload=%r", payload.rstrip())
        except serial.SerialException as error:  # pragma: no cover - safety guard
            self._append_system_message(f"Serial write failed: {error}")
            self.set_serial(None)
            return
        self._arm_manual_response_capture(self.MANUAL_RESPONSE_TIMEOUT_S)
        self.manual_command_sent.emit(text or "\u240d")
        if text:
            self._append_local_echo(text)
            self._command_history.append(text)
        else:
            self._append_local_echo("\u240d")
        self._history_position = len(self._command_history)
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
        if self._poll_paused:
            return
        if not self.serial_connection or not self.serial_connection.is_open:
            self.poll_timer.stop()
            self._update_enabled_state()
            return
        if not self._capture_manual_output:
            return
        if time.monotonic() >= self._capture_deadline:
            self._capture_manual_output = False
            self._sync_poll_timer()
            return
        if self.stage_controller is not None:
            try:
                data = self.stage_controller.read_pending_serial_output()
            except Exception as error:  # pragma: no cover - UI safety guard
                self._append_system_message(str(error))
                self.set_serial(None)
                return
            if data:
                self._append_remote_message(data)
                self._extend_manual_response_capture()
            return
        try:
            waiting = self.serial_connection.in_waiting
            if waiting:
                logger.debug("SERIAL TRACE terminal_in_waiting bytes=%s", waiting)
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
            logger.debug("SERIAL TRACE terminal_read bytes=%r", data[:200])
            self._append_remote_message(data)
            self._extend_manual_response_capture()

    def _sync_poll_timer(self) -> None:
        should_poll = bool(
            not self._poll_paused
            and self.serial_connection is not None
            and self.serial_connection.is_open
            and self._capture_manual_output
        )
        if should_poll:
            if not self.poll_timer.isActive():
                self.poll_timer.start()
        else:
            self.poll_timer.stop()

    def _arm_manual_response_capture(self, timeout_s: float) -> None:
        self._capture_manual_output = True
        self._capture_deadline = time.monotonic() + max(0.05, float(timeout_s))
        self._sync_poll_timer()

    def _extend_manual_response_capture(self) -> None:
        self._capture_deadline = max(
            self._capture_deadline,
            time.monotonic() + self.MANUAL_RESPONSE_IDLE_GRACE_S,
        )

    def _update_enabled_state(self) -> None:
        enabled = bool(self.serial_connection and self.serial_connection.is_open)
        self.terminal_container.setEnabled(enabled)

    def _show_previous_history_entry(self) -> None:
        if not self._command_history:
            return
        if self._history_position > 0:
            self._history_position -= 1
        else:
            self._history_position = 0
        self.input_edit.setText(self._command_history[self._history_position])
        self.input_edit.selectAll()

    def _show_next_history_entry(self) -> None:
        if not self._command_history:
            return
        if self._history_position < len(self._command_history) - 1:
            self._history_position += 1
            self.input_edit.setText(self._command_history[self._history_position])
        else:
            self._history_position = len(self._command_history)
            self.input_edit.clear()
        self.input_edit.selectAll()


__all__ = ["SerialTerminalWindow"]
