"""Application entry point for the probe station GUI."""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QEvent, QThread, QTimer, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow, QVBoxLayout, QWidget

from probe_station_gui import (
    Grabber,
    JoystickWindow,
    MicroscopeView,
    StageController,
    SerialTerminalWindow,
)
from probe_station_gui.dialogs.settings_dialog import SettingsDialog
from probe_station_gui.settings_manager import SettingsManager
from probe_station_gui.views.dock_widgets import CollapsibleDockWidget
from probe_station_gui.views.serial_connection_panel import SerialConnectionPanel


logger = logging.getLogger(__name__)


class Main(QMainWindow):
    """Main application window wiring the camera view and serial dialog."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Microscope control")
        self.menuBar().setNativeMenuBar(False)

        self.view = MicroscopeView()
        central_container = QWidget(self)
        central_layout = QVBoxLayout(central_container)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self.view, 1)
        self.setCentralWidget(central_container)
        self.serial_connection = None
        self.serial_port_name: str | None = None
        self.serial_baud_rate: int | None = None
        self.settings_manager: SettingsManager = SettingsManager()
        self.joystick_panel: JoystickWindow | None = None
        self.serial_terminal_panel: SerialTerminalWindow | None = None
        self.serial_connection_panel: SerialConnectionPanel | None = None
        self.joystick_dock: CollapsibleDockWidget | None = None
        self.serial_terminal_dock: CollapsibleDockWidget | None = None
        self.serial_connection_dock: CollapsibleDockWidget | None = None
        self._minimize_action: QAction | None = None
        self._restore_action: QAction | None = None
        self._maximize_action: QAction | None = None
        self._close_action: QAction | None = None
        self.statusBar()

        self.grabber = Grabber()
        self.thread = QThread()
        self.grabber.moveToThread(self.thread)
        self.thread.started.connect(self.grabber.start)
        self.view.clicked.connect(self.on_click)
        self.grabber.frame_ready.connect(self.view.set_frame)
        self.grabber.error.connect(self.on_error)
        self.thread.start()

        self.stage_controller = StageController()
        self.stage_controller.status_message.connect(self.statusBar().showMessage)
        self.stage_controller.movement_finished.connect(self.on_move_finished)
        self.stage_controller.calibration_changed.connect(self.on_calibration_changed)
        self.stage_controller.movement_started.connect(
            lambda: self.statusBar().showMessage("Moving stage…")
        )
        self.grabber.frame_ready.connect(self.stage_controller.on_frame_ready)

        self._create_dock_widgets()

        self._setup_menus()
        self._apply_settings()
        window_menu = self.menuBar().addMenu("Panels")
        if self.joystick_dock is not None:
            joystick_action = self.joystick_dock.toggleViewAction()
            joystick_action.setText("Joystick")
            window_menu.addAction(joystick_action)
        if self.serial_terminal_dock is not None:
            terminal_action = self.serial_terminal_dock.toggleViewAction()
            terminal_action.setText("Serial Terminal")
            window_menu.addAction(terminal_action)
        if self.serial_connection_dock is not None:
            connection_action = self.serial_connection_dock.toggleViewAction()
            connection_action.setText("Connection")
            window_menu.addAction(connection_action)

        QTimer.singleShot(0, self._auto_connect_if_possible)

        self.setStyleSheet(
            """
            QMainWindow::separator { width: 8px; height: 8px; background: palette(window); }
            """
        )

    def on_click(self, dx: float, dy: float, _rel_x: float, _rel_y: float) -> None:
        self.stage_controller.request_move(dx, dy)

    def on_error(self, message: str) -> None:
        logger.error("Camera error: %s", message)

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
        logger.info(
            "Serial connected: %s @ %s baud",
            self.serial_connection.port,
            self.serial_connection.baudrate,
        )
        self.stage_controller.set_serial(self.serial_connection)
        if self.joystick_panel and self.joystick_dock:
            self.joystick_panel.set_serial(self.serial_connection)
            self.joystick_dock.setVisible(True)
            self.joystick_dock.raise_()
            if self.joystick_dock.isFloating():
                self.joystick_dock.activateWindow()
        if self.serial_terminal_panel and self.serial_terminal_dock:
            self.serial_terminal_panel.set_serial(self.serial_connection)
            self.serial_terminal_dock.setVisible(True)
            self.serial_terminal_dock.raise_()
            if self.serial_terminal_dock.isFloating():
                self.serial_terminal_dock.activateWindow()

    def on_serial_disconnected(self) -> None:
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
        self.serial_connection = None
        logger.info("Serial disconnected")
        self.stage_controller.set_serial(None)
        auto_retry = self.sender() is not self.serial_connection_panel
        if self.serial_connection_panel:
            self.serial_connection_panel.handle_external_disconnect(auto_retry=auto_retry)
        if self.joystick_panel:
            self.joystick_panel.set_serial(None)
        if self.serial_terminal_panel:
            self.serial_terminal_panel.set_serial(None)

    def _auto_connect_if_possible(self) -> None:
        if self.serial_connection_panel and not self.serial_connection:
            logger.debug("Attempting auto-connect through connection panel")
            self.serial_connection_panel.auto_connect()

    def _setup_menus(self) -> None:
        app_menu = self.menuBar().addMenu("Application")

        self._minimize_action = QAction("Minimize", self)
        self._minimize_action.triggered.connect(self.showMinimized)
        app_menu.addAction(self._minimize_action)

        self._restore_action = QAction("Restore", self)
        self._restore_action.triggered.connect(self.showNormal)
        app_menu.addAction(self._restore_action)

        self._maximize_action = QAction("Toggle Maximized", self)
        self._maximize_action.setCheckable(True)
        self._maximize_action.setChecked(True)
        self._maximize_action.triggered.connect(self._toggle_maximized)

        settings_menu = self.menuBar().addMenu("Settings")
        settings_action = QAction("Settings…", self)
        settings_action.triggered.connect(self._open_settings_dialog)
        settings_menu.addAction(settings_action)

        app_menu.addAction(self._maximize_action)

        if self._minimize_action is not None:
            self._minimize_action.triggered.connect(
                lambda: self._set_maximized_checked(False)
            )
        if self._restore_action is not None:
            self._restore_action.triggered.connect(
                lambda: self._set_maximized_checked(False)
            )

        self._close_action = QAction("Close", self)
        self._close_action.triggered.connect(self.close)
        app_menu.addAction(self._close_action)

        self._update_maximize_action_state()

    def _apply_settings(self) -> None:
        if self.joystick_panel:
            bindings = self.settings_manager.control_bindings()
            self.joystick_panel.apply_control_bindings(bindings)
            logger.debug("Joystick bindings reapplied from settings")
            feedrates = self.settings_manager.feedrate_configuration()
            self.joystick_panel.apply_feedrate_settings(
                feedrates.linear.presets,
                feedrates.linear.default,
                feedrates.rotary.presets,
                feedrates.rotary.default,
            )
            logger.debug(
                "Joystick feedrate settings reapplied: linear=%s (default=%s) rotary=%s (default=%s)",
                feedrates.linear.presets,
                feedrates.linear.default,
                feedrates.rotary.presets,
                feedrates.rotary.default,
            )

    def _open_settings_dialog(self) -> None:
        dialog = SettingsDialog(self.settings_manager.settings, self)
        if dialog.exec() != QDialog.Accepted:
            logger.debug("Settings dialog cancelled")
            return
        new_settings = dialog.result_settings()
        self.settings_manager.replace(new_settings)
        self.settings_manager.save()
        self._apply_settings()
        logger.info("Settings updated from dialog")

    def show_joystick_window(self) -> None:
        if not self.joystick_panel or not self.joystick_dock:
            return
        self.joystick_dock.setVisible(True)
        self.joystick_dock.raise_()
        if self.joystick_dock.isFloating():
            self.joystick_dock.activateWindow()
        else:
            self.joystick_panel.setFocus(Qt.ActiveWindowFocusReason)

    def show_serial_terminal_window(self) -> None:
        if not self.serial_terminal_panel or not self.serial_terminal_dock:
            return
        self.serial_terminal_dock.setVisible(True)
        self.serial_terminal_dock.raise_()
        if self.serial_terminal_dock.isFloating():
            self.serial_terminal_dock.activateWindow()
        else:
            self.serial_terminal_panel.setFocus(Qt.ActiveWindowFocusReason)

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
        if self.joystick_panel:
            self.joystick_panel.set_serial(None)
        if self.serial_terminal_panel:
            self.serial_terminal_panel.set_serial(None)
        self.stage_controller.shutdown()
        if self.serial_connection_panel:
            self.serial_connection_panel.shutdown()
        event.accept()

    def changeEvent(self, event) -> None:  # type: ignore[override]
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            QTimer.singleShot(0, self._update_maximize_action_state)

    def _create_dock_widgets(self) -> None:
        self.serial_connection_panel = SerialConnectionPanel(self)
        self.serial_connection_panel.connected.connect(self.on_serial_connected)
        self.serial_connection_panel.disconnected.connect(self.on_serial_disconnected)
        self.serial_connection_dock = CollapsibleDockWidget("Connection", self)
        self.serial_connection_dock.setObjectName("SerialConnectionDock")
        self.serial_connection_dock.setWidget(self.serial_connection_panel)
        self.serial_connection_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
        )
        self.addDockWidget(Qt.LeftDockWidgetArea, self.serial_connection_dock)

        self.joystick_panel = JoystickWindow(self)
        feedrates = self.settings_manager.feedrate_configuration()
        self.joystick_panel.apply_feedrate_settings(
            feedrates.linear.presets,
            feedrates.linear.default,
            feedrates.rotary.presets,
            feedrates.rotary.default,
        )
        self.joystick_panel.set_serial(self.serial_connection)
        self.joystick_dock = CollapsibleDockWidget("Joystick", self)
        self.joystick_dock.setObjectName("JoystickDock")
        self.joystick_dock.setWidget(self.joystick_panel)
        self.joystick_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
        )
        self.addDockWidget(Qt.LeftDockWidgetArea, self.joystick_dock)
        self.splitDockWidget(
            self.serial_connection_dock, self.joystick_dock, Qt.Vertical
        )

        self.serial_terminal_panel = SerialTerminalWindow(self)
        self.serial_terminal_panel.set_stage_controller(self.stage_controller)
        self.serial_terminal_panel.set_serial(self.serial_connection)
        self.serial_terminal_dock = CollapsibleDockWidget("Serial Terminal", self)
        self.serial_terminal_dock.setObjectName("SerialTerminalDock")
        self.serial_terminal_dock.setWidget(self.serial_terminal_panel)
        self.serial_terminal_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
        )
        self.addDockWidget(Qt.LeftDockWidgetArea, self.serial_terminal_dock)
        self.splitDockWidget(self.joystick_dock, self.serial_terminal_dock, Qt.Vertical)

    def _set_maximized_checked(self, checked: bool) -> None:
        if self._maximize_action:
            block = self._maximize_action.blockSignals(True)
            self._maximize_action.setChecked(checked)
            self._maximize_action.blockSignals(block)

    def _update_maximize_action_state(self) -> None:
        self._set_maximized_checked(self.isMaximized())

    def _toggle_maximized(self, checked: bool) -> None:
        if checked:
            self.showMaximized()
        else:
            self.showNormal()
        self._update_maximize_action_state()


def main() -> int:
    app = QApplication(sys.argv)
    window = Main()
    window.showMaximized()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
