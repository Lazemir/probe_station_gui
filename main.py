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
    SerialTerminalWindow,
    StageController,
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
        self.serial_dialog = SerialScannerDialog(self)
        self.serial_port_name: str | None = None
        self.serial_baud_rate: int | None = None
        self.joystick_window: JoystickWindow | None = None
        self.serial_terminal_window: SerialTerminalWindow | None = None
        self.statusBar()

        self.grabber = Grabber()
        self.thread = QThread()
        self.grabber.moveToThread(self.thread)
        self.thread.started.connect(self.grabber.start)
        self.view.clicked.connect(self.on_click)
        self.grabber.frame_ready.connect(self.view.set_frame)
        self.grabber.error.connect(self.on_error)
        self.thread.start()

        self.serial_dialog.connected.connect(self.on_serial_connected)
        self.serial_dialog.disconnected.connect(self.on_serial_disconnected)

        self.stage_controller = StageController()
        self.stage_controller.status_message.connect(self.statusBar().showMessage)
        self.stage_controller.movement_finished.connect(self.on_move_finished)
        self.stage_controller.calibration_changed.connect(self.on_calibration_changed)
        self.stage_controller.movement_started.connect(
            lambda: self.statusBar().showMessage("Moving stage…")
        )
        self.grabber.frame_ready.connect(self.stage_controller.on_frame_ready)

        tools_menu = self.menuBar().addMenu("Tools")
        serial_action = QAction("Serial Scanner", self)
        serial_action.triggered.connect(self.open_serial_scanner)
        tools_menu.addAction(serial_action)

        window_menu = self.menuBar().addMenu("Window")
        joystick_action = QAction("Joystick", self)
        joystick_action.triggered.connect(self.show_joystick_window)
        window_menu.addAction(joystick_action)
        terminal_action = QAction("Serial Terminal", self)
        terminal_action.triggered.connect(self.show_serial_terminal_window)
        window_menu.addAction(terminal_action)

        QTimer.singleShot(0, self.open_serial_scanner)

    def on_click(self, dx: float, dy: float, _rel_x: float, _rel_y: float) -> None:
        self.stage_controller.request_move(dx, dy)

    def on_error(self, message: str) -> None:
        print("Camera error:", message)

    def open_serial_scanner(self) -> None:
        if self.serial_dialog:
            self.serial_dialog.populate_ports(clear_status=False)
            self.serial_dialog.show()
            self.serial_dialog.raise_()
            self.serial_dialog.activateWindow()

    def on_serial_connected(self, serial_port) -> None:
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
        self.serial_connection = serial_port
        self.serial_port_name = serial_port.port
        try:
            baud_rate = int(serial_port.baudrate)
        except TypeError:
            baud_rate = int(float(serial_port.baudrate))
        self.serial_baud_rate = baud_rate
        print(
            f"Serial connected: {self.serial_connection.port} @ {self.serial_connection.baudrate} baud"
        )
        self.stage_controller.set_serial(self.serial_connection)
        joystick = self._ensure_joystick_window()
        joystick.set_serial(self.serial_connection)
        joystick.show()
        joystick.raise_()
        joystick.activateWindow()
        terminal = self._ensure_serial_terminal_window()
        terminal.set_serial(self.serial_connection)
        terminal.show()
        terminal.raise_()
        terminal.activateWindow()

    def on_serial_disconnected(self) -> None:
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
        self.serial_connection = None
        print("Serial disconnected")
        self.stage_controller.set_serial(None)
        if self.serial_dialog:
            self.serial_dialog.handle_external_disconnect()
        if self.joystick_window:
            self.joystick_window.set_serial(None)
        if self.serial_terminal_window:
            self.serial_terminal_window.set_serial(None)

    def show_joystick_window(self) -> None:
        joystick = self._ensure_joystick_window()
        joystick.show()
        joystick.raise_()
        joystick.activateWindow()

    def show_serial_terminal_window(self) -> None:
        terminal = self._ensure_serial_terminal_window()
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

    def _ensure_serial_terminal_window(self) -> SerialTerminalWindow:
        if self.serial_terminal_window is None:
            self.serial_terminal_window = SerialTerminalWindow(self)
            self.serial_terminal_window.set_stage_controller(self.stage_controller)
            self.serial_terminal_window.set_serial(self.serial_connection)
            self.serial_terminal_window.destroyed.connect(
                self._on_serial_terminal_destroyed
            )
        return self.serial_terminal_window

    def _on_serial_terminal_destroyed(self, _object=None) -> None:
        self.serial_terminal_window = None

    def on_move_finished(self, success: bool, message: str) -> None:
        if success:
            self.view.clear_target_cross()
        if message:
            self.statusBar().showMessage(message, 5000)

    def on_calibration_changed(self, mm_per_pixel_x: float, mm_per_pixel_y: float) -> None:
        self.statusBar().showMessage(
            f"Calibration: ΔX {mm_per_pixel_x:.6f} mm/px, ΔY {mm_per_pixel_y:.6f} mm/px",
            5000,
        )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.grabber.stop()
        self.thread.quit()
        self.thread.wait()
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
            if self.serial_dialog:
                self.serial_dialog.handle_external_disconnect()
        if self.joystick_window:
            self.joystick_window.close()
        if self.serial_terminal_window:
            self.serial_terminal_window.close()
            self.serial_terminal_window = None
        self.stage_controller.shutdown()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    window = Main()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
