"""Application entry point for the probe station GUI."""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt, QThread
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMainWindow

from probe_station_gui import (
    Grabber,
    JoystickWindow,
    MicroscopeView,
    SerialScannerDialog,
)


class Main(QMainWindow):
    """Main application window wiring the camera view and serial dialog."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Microscope control")
        self.view = MicroscopeView()
        self.setCentralWidget(self.view)
        self.serial_connection = None
        self.joystick_window: JoystickWindow | None = None

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

        joystick_action = QAction("Joystick", self)
        joystick_action.triggered.connect(self.open_joystick_window)
        tools_menu.addAction(joystick_action)

    def on_click(self, dx: float, dy: float) -> None:
        print(f"Click Δx={dx:.1f}px  Δy={dy:.1f}px")
        # later you’ll add coordinate conversion and G-code sending here

    def on_error(self, message: str) -> None:
        print("Camera error:", message)

    def open_serial_scanner(self) -> None:
        dialog = SerialScannerDialog(self)
        dialog.connected.connect(self.on_serial_connected)
        dialog.disconnected.connect(self.on_serial_disconnected)
        dialog.exec()

    def on_serial_connected(self, serial_port) -> None:
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
        self.serial_connection = serial_port
        print(
            f"Serial connected: {self.serial_connection.port} @ {self.serial_connection.baudrate} baud"
        )
        if self.joystick_window:
            self.joystick_window.set_serial(self.serial_connection)

    def on_serial_disconnected(self) -> None:
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
        self.serial_connection = None
        print("Serial disconnected")
        if self.joystick_window:
            self.joystick_window.set_serial(None)

    def open_joystick_window(self) -> None:
        if self.joystick_window is None:
            self.joystick_window = JoystickWindow(self)
            self.joystick_window.setAttribute(Qt.WA_DeleteOnClose, True)
            self.joystick_window.set_serial(self.serial_connection)
            self.joystick_window.destroyed.connect(self._on_joystick_destroyed)
        self.joystick_window.show()
        self.joystick_window.raise_()
        self.joystick_window.activateWindow()

    def _on_joystick_destroyed(self, _object=None) -> None:
        self.joystick_window = None

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.grabber.stop()
        self.thread.quit()
        self.thread.wait()
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
        if self.joystick_window:
            self.joystick_window.close()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    window = Main()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
