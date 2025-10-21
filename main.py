"""Application entry point for the probe station GUI."""

from __future__ import annotations

import sys

from PySide6.QtCore import QThread
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMainWindow

from probe_station_gui import Grabber, MicroscopeView, SerialScannerDialog


class Main(QMainWindow):
    """Main application window wiring the camera view and serial dialog."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Microscope control")
        self.view = MicroscopeView()
        self.setCentralWidget(self.view)
        self.serial_connection = None
        self.serial_dialog: SerialScannerDialog | None = None
        self.serial_port_name: str | None = None
        self.serial_baud_rate: int | None = None

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

    def on_click(self, dx: float, dy: float) -> None:
        print(f"Click Δx={dx:.1f}px  Δy={dy:.1f}px")
        # later you’ll add coordinate conversion and G-code sending here

    def on_error(self, message: str) -> None:
        print("Camera error:", message)

    def open_serial_scanner(self) -> None:
        if self.serial_dialog is None:
            self.serial_dialog = SerialScannerDialog(self)
            self.serial_dialog.connected.connect(self.on_serial_connected)

        if self.serial_dialog is not None:
            self.serial_dialog.populate_ports()
            is_connected = (
                self.serial_connection is not None and self.serial_connection.is_open
            )
            self.serial_dialog.set_connection(
                self.serial_connection,
                port_name=self.serial_port_name,
                baud_rate=self.serial_baud_rate,
                connected=is_connected,
            )
        self.serial_dialog.exec()

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
        if self.serial_dialog is not None:
            self.serial_dialog.set_connection(
                self.serial_connection,
                port_name=self.serial_port_name,
                baud_rate=self.serial_baud_rate,
                connected=True,
            )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.grabber.stop()
        self.thread.quit()
        self.thread.wait()
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
        if self.serial_dialog is not None:
            self.serial_dialog.set_connection(
                None,
                port_name=self.serial_port_name,
                baud_rate=self.serial_baud_rate,
                connected=False,
            )
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    window = Main()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
