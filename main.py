"""Application entry point for the probe station GUI."""

from __future__ import annotations

import sys

from PySide6.QtCore import QThread, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMainWindow

from probe_station_gui import (
    Grabber,
    JoystickWindow,
    MicroscopeView,
    SerialScannerDialog,
    SerialTerminalWindow,
)


class Main(QMainWindow):
    """Main application window wiring the camera view and serial dialog."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Microscope control")
        self.view = MicroscopeView()
        self.setCentralWidget(self.view)
        self.serial_connection = None
        self.serial_dialog = SerialScannerDialog(self)
        self.serial_dialog.connected.connect(self.on_serial_connected)
        self.serial_dialog.disconnected.connect(self.on_serial_disconnected)
        self.joystick_window: JoystickWindow | None = None
        self.terminal_window: SerialTerminalWindow | None = None

        self.grabber = Grabber()
        self.thread = QThread()
        self.grabber.moveToThread(self.thread)
        self.thread.started.connect(self.grabber.start)
        self.view.clicked.connect(self.on_click)
        self.grabber.frame_ready.connect(self.view.set_frame)
        self.grabber.error.connect(self.on_error)
        self.thread.start()

        tools_menu = self.menuBar().addMenu("Tools")
        serial_action = QAction("Serial Scanner", self)
        serial_action.triggered.connect(self.open_serial_scanner)
        tools_menu.addAction(serial_action)

        window_menu = self.menuBar().addMenu("Window")
        joystick_action = QAction("Joystick", self)
        joystick_action.triggered.connect(self.show_joystick_window)
        window_menu.addAction(joystick_action)

        terminal_action = QAction("Serial Terminal", self)
        terminal_action.triggered.connect(self.show_serial_terminal)
        window_menu.addAction(terminal_action)

        QTimer.singleShot(0, self.open_serial_scanner)

    def on_click(self, dx: float, dy: float) -> None:
        print(f"Click Δx={dx:.1f}px  Δy={dy:.1f}px")
        # later you’ll add coordinate conversion and G-code sending here

    def on_error(self, message: str) -> None:
        print("Camera error:", message)

    def open_serial_scanner(self) -> None:
        self.serial_dialog.populate_ports(clear_status=False)
        self.serial_dialog.show()
        self.serial_dialog.raise_()
        self.serial_dialog.activateWindow()

    def on_serial_connected(self, serial_port) -> None:
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
        self.serial_connection = serial_port
        print(
            f"Serial connected: {self.serial_connection.port} @ {self.serial_connection.baudrate} baud"
        )
        joystick = self._ensure_joystick_window()
        joystick.set_serial(self.serial_connection)
        joystick.show()
        joystick.raise_()
        joystick.activateWindow()

        terminal = self._ensure_terminal_window()
        terminal.set_serial(self.serial_connection)
        if not terminal.isVisible():
            terminal.show()
        terminal.raise_()
        joystick.activateWindow()

    def on_serial_disconnected(self) -> None:
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
        self.serial_connection = None
        print("Serial disconnected")
        self.serial_dialog.handle_external_disconnect()
        if self.joystick_window:
            self.joystick_window.set_serial(None)
        if self.terminal_window:
            self.terminal_window.set_serial(None)

    def show_joystick_window(self) -> None:
        joystick = self._ensure_joystick_window()
        joystick.show()
        joystick.raise_()
        joystick.activateWindow()

    def show_serial_terminal(self) -> None:
        terminal = self._ensure_terminal_window()
        terminal.show()
        terminal.raise_()
        terminal.activateWindow()

    def _ensure_joystick_window(self) -> JoystickWindow:
        if self.joystick_window is None:
            self.joystick_window = JoystickWindow(self)
            self.joystick_window.set_serial(self.serial_connection)
            self.joystick_window.destroyed.connect(self._on_joystick_destroyed)
        return self.joystick_window

    def _on_joystick_destroyed(self, _object=None) -> None:
        self.joystick_window = None

    def _ensure_terminal_window(self) -> SerialTerminalWindow:
        if self.terminal_window is None:
            self.terminal_window = SerialTerminalWindow(self)
            self.terminal_window.set_serial(self.serial_connection)
            self.terminal_window.destroyed.connect(self._on_terminal_destroyed)
        return self.terminal_window

    def _on_terminal_destroyed(self, _object=None) -> None:
        self.terminal_window = None

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.grabber.stop()
        self.thread.quit()
        self.thread.wait()
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
            self.serial_dialog.handle_external_disconnect()
        if self.joystick_window:
            self.joystick_window.close()
        if self.terminal_window:
            self.terminal_window.close()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    window = Main()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
