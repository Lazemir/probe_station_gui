"""Dockable panel for managing FluidNC serial connections."""

from __future__ import annotations

import logging
import time
from typing import Optional

import serial
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from serial.tools import list_ports


logger = logging.getLogger(__name__)

_SERIAL_IO_EXCEPTIONS = (
    serial.SerialException,
    OSError,
    ValueError,
    AttributeError,
    TypeError,
)


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
        self._connecting = False
        self._connect_cancelled = False
        self._auto_retry_pending = False
        self._connect_thread: Optional[QThread] = None
        self._connect_worker: Optional[_SerialConnectWorker] = None
        self._scan_thread: Optional[QThread] = None
        self._scan_worker: Optional[_SerialPortScanWorker] = None
        self._scan_in_progress = False
        self._pending_auto_connect_after_scan = False

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

        self.port_combo.addItem("Scanning ports...")
        self._update_ui_state()
        QTimer.singleShot(0, self.populate_ports)

    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def populate_ports(self) -> None:
        if self._serial is not None and self._serial.is_open:
            return
        if self._scan_in_progress:
            return

        self._scan_in_progress = True
        self.port_combo.clear()
        self.port_combo.addItem("Scanning ports...")
        self._ports_available = False
        self._set_scan_controls_enabled(False)

        worker = _SerialPortScanWorker()
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_port_scan_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._cleanup_scan_worker)

        self._scan_worker = worker
        self._scan_thread = thread
        thread.start()

    @Slot(object)
    def _on_port_scan_finished(self, ports: object) -> None:
        self.port_combo.clear()
        ports = list(ports) if ports is not None else []
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
        self._scan_in_progress = False
        self._update_ui_state()
        if self._pending_auto_connect_after_scan:
            self._pending_auto_connect_after_scan = False
            if not self._ports_available:
                self._auto_timer.start(self._AUTO_RECONNECT_DELAY_MS)
                return
            self.auto_connect()

    def on_connect_clicked(self) -> None:
        if self._connecting:
            self._connect_cancelled = True
            self._auto_retry_pending = False
            self._set_connecting(False)
            self.status_label.setText("Connection cancelled.")
            return

        if self._serial is not None and self._serial.is_open:
            self._auto_timer.stop()
            try:
                self._serial.close()
            except _SERIAL_IO_EXCEPTIONS:
                logger.debug("Serial close failed during manual disconnect", exc_info=True)
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

        self._auto_retry_pending = False
        self._start_async_connect(port_name, baud_rate)

    def auto_connect(self) -> None:
        if self._serial is not None and self._serial.is_open:
            return
        if self._connecting:
            return
        if self._scan_in_progress:
            self._pending_auto_connect_after_scan = True
            return
        if not self._ports_available:
            self._pending_auto_connect_after_scan = True
            self.populate_ports()
            if not self._ports_available:
                return

        target_index = 0
        for index, port in enumerate(self._ports_cache):
            description = " ".join(filter(None, [port.description, port.manufacturer]))
            if description and "fluid" in description.lower():
                target_index = index
                break
        if self.port_combo.count() and 0 <= target_index < self.port_combo.count():
            self.port_combo.setCurrentIndex(target_index)

        self._auto_retry_pending = True
        logger.debug(
            "Serial auto-connect attempting %s @ %s baud",
            self.port_combo.currentData(),
            self.baud_combo.currentText(),
        )
        self._start_async_connect(self.port_combo.currentData(), int(self.baud_combo.currentText()))

    def handle_external_disconnect(self, auto_retry: bool = True) -> None:
        if self._serial is not None and self._serial.is_open:
            try:
                self._serial.close()
            except _SERIAL_IO_EXCEPTIONS:
                logger.debug("Serial close failed during external disconnect", exc_info=True)
        self._serial = None
        self.status_label.setText("Disconnected from board.")
        self.populate_ports()
        if auto_retry:
            self._auto_timer.start(self._AUTO_RECONNECT_DELAY_MS)

    def _update_ui_state(self) -> None:
        connected = self._serial is not None and self._serial.is_open
        self.connect_button.setText("Disconnect" if connected else "Connect")
        self.port_combo.setEnabled(
            not connected and self._ports_available and not self._scan_in_progress
        )
        self.baud_combo.setEnabled(not connected)
        self.refresh_button.setEnabled(not connected and not self._scan_in_progress)

    def _set_connecting(self, connecting: bool) -> None:
        self._connecting = connecting
        if connecting:
            self.connect_button.setText("Cancel")
            self.port_combo.setEnabled(False)
            self.baud_combo.setEnabled(False)
            self.refresh_button.setEnabled(False)
        else:
            self._update_ui_state()

    def _set_scan_controls_enabled(self, enabled: bool) -> None:
        connected = self._serial is not None and self._serial.is_open
        self.port_combo.setEnabled(enabled and not connected and self._ports_available)
        self.refresh_button.setEnabled(enabled and not connected)

    def _start_async_connect(self, port_name: Optional[str], baud_rate: int) -> None:
        if port_name is None:
            port_name = self.port_combo.currentText().split(" ")[0]

        if self._connect_thread is not None:
            if self._connect_thread.isRunning():
                self.status_label.setText("Previous connection attempt is still stopping.")
                return
            self._cleanup_connect_worker()

        self._connect_cancelled = False
        self._set_connecting(True)
        self.status_label.setText(f"Connecting to {port_name} @ {baud_rate} baud...")

        worker = _SerialConnectWorker(port_name, baud_rate)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.connected.connect(self._on_connect_success)
        worker.failed.connect(self._on_connect_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._cleanup_connect_worker)

        self._connect_worker = worker
        self._connect_thread = thread
        thread.start()

    @Slot(object, str, int)
    def _on_connect_success(self, serial_connection: serial.Serial, port_name: str, baud_rate: int) -> None:
        if self._connect_cancelled:
            try:
                serial_connection.close()
            except _SERIAL_IO_EXCEPTIONS:
                pass
            return

        self._serial = serial_connection
        self.status_label.setText(f"Connected to {port_name} @ {baud_rate} baud.")
        self._auto_timer.stop()
        self._auto_retry_pending = False
        self._set_connecting(False)
        self.connected.emit(self._serial)

    @Slot(str)
    def _on_connect_failed(self, message: str) -> None:
        if self._connect_cancelled:
            return
        self.status_label.setText(message)
        self._set_connecting(False)
        if self._auto_retry_pending and not self._is_port_busy_failure(message):
            self._auto_timer.start(self._AUTO_RECONNECT_DELAY_MS)
        elif self._auto_retry_pending:
            logger.debug("Serial auto-connect stopped after busy-port failure: %s", message)
            self._auto_retry_pending = False

    def _cleanup_connect_worker(self) -> None:
        self._connect_worker = None
        self._connect_thread = None

    @staticmethod
    def _is_port_busy_failure(message: str) -> bool:
        text = message.lower()
        return any(
            token in text
            for token in (
                "access is denied",
                "permission denied",
                "permissionerror",
                "winerror 5",
                "winerror 32",
                "resource busy",
                "port is busy",
                "already open",
                "in use",
                "отказано в доступе",
                "доступ запрещ",
                "занят",
                "используется",
            )
        )

    def _cleanup_scan_worker(self) -> None:
        self._scan_worker = None
        self._scan_thread = None

    def shutdown(self) -> None:
        self._auto_timer.stop()
        self._connect_cancelled = True
        self._set_connecting(False)
        if self._serial is not None and self._serial.is_open:
            try:
                self._serial.close()
            except _SERIAL_IO_EXCEPTIONS:
                logger.debug("Serial close failed during shutdown", exc_info=True)
        self._serial = None


class _SerialPortScanWorker(QObject):
    finished: Signal = Signal(object)

    @Slot()
    def run(self) -> None:
        self.finished.emit(list(list_ports.comports()))


class _SerialConnectWorker(QObject):
    connected: Signal = Signal(object, str, int)
    failed: Signal = Signal(str)
    finished: Signal = Signal()

    def __init__(self, port_name: str, baud_rate: int) -> None:
        super().__init__()
        self._port_name = port_name
        self._baud_rate = baud_rate

    @Slot()
    def run(self) -> None:
        connection: serial.Serial | None = None
        try:
            connection = serial.Serial()
            connection.port = self._port_name
            connection.baudrate = self._baud_rate
            connection.timeout = 1
            connection.rtscts = False
            connection.dsrdtr = False
            try:
                connection.rts = False
                connection.dtr = False
            except (AttributeError, ValueError):
                pass
            connection.open()
            try:
                connection.rts = False
                connection.dtr = False
            except (AttributeError, ValueError):
                pass
        except _SERIAL_IO_EXCEPTIONS as exc:
            logger.debug("Serial connection failed on %s: %s", self._port_name, exc)
            self._close_connection(connection)
            self.failed.emit(f"Connection failed on {self._port_name}: {exc}")
            self.finished.emit()
            return
        except Exception as exc:  # pragma: no cover - defensive hardware guard
            logger.exception("Unexpected serial connection failure on %s", self._port_name)
            self._close_connection(connection)
            self.failed.emit(f"Connection failed on {self._port_name}: {exc!r}")
            self.finished.emit()
            return

        startup_lines: list[str] = []
        reboot_detected = False
        original_timeout = connection.timeout
        try:
            connection.timeout = 0.05
            deadline = time.monotonic() + 1.5
            quiet_deadline = time.monotonic() + 0.25
            while time.monotonic() < deadline:
                if time.monotonic() >= quiet_deadline:
                    break
                try:
                    raw = connection.readline()
                except _SERIAL_IO_EXCEPTIONS:
                    break
                line = raw.decode("ascii", errors="ignore").strip()
                if not line:
                    continue
                startup_lines.append(line)
                quiet_deadline = time.monotonic() + 0.25
                upper = line.upper()
                if self._line_indicates_controller_reboot(upper):
                    reboot_detected = True
        except Exception:
            logger.debug("Initial serial banner probe failed", exc_info=True)
        finally:
            try:
                connection.timeout = original_timeout
            except _SERIAL_IO_EXCEPTIONS:
                pass

        try:
            setattr(connection, "probe_station_reboot_detected", reboot_detected)
            setattr(connection, "probe_station_startup_lines", tuple(startup_lines))
        except Exception:
            logger.debug("Unable to attach serial connection metadata", exc_info=True)
        if startup_lines:
            logger.debug(
                "Serial connect banner probe lines=%s reboot_detected=%s",
                startup_lines,
                reboot_detected,
            )

        self.connected.emit(connection, self._port_name, self._baud_rate)
        self.finished.emit()

    @staticmethod
    def _close_connection(connection: serial.Serial | None) -> None:
        if connection is None:
            return
        try:
            if connection.is_open:
                connection.close()
        except _SERIAL_IO_EXCEPTIONS:
            pass

    @staticmethod
    def _line_indicates_controller_reboot(upper_line: str) -> bool:
        return any(
            token in upper_line
            for token in (
                "[VER:",
                "FLUIDNC",
                "GRBL",
                "[MSG:RST",
                "FAST_FLASH_BOOT",
                "ESP-ROM",
                "LOAD:",
                "ENTRY ",
                "RST:",
            )
        )


__all__ = ["SerialConnectionPanel"]
