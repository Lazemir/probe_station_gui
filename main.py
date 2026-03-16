"""Application entry point for the probe station GUI."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
import sys

from PySide6.QtCore import QThread, QTimer, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QMainWindow,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui import (
    Grabber,
    JoystickWindow,
    MicroscopeView,
    StageController,
    SerialTerminalWindow,
)
from probe_station_gui.dialogs.settings_dialog import SettingsDialog
from probe_station_gui.lcr_meter import LCRMeterController
from probe_station_gui.settings_manager import SettingsManager
from probe_station_gui.views.dock_widgets import CollapsibleDockWidget
from probe_station_gui.views.needle_calibration_panel import NeedleCalibrationPanel
from probe_station_gui.views.oscillation_panel import OscillationPanel
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
        self.needle_calibration_panel: NeedleCalibrationPanel | None = None
        self.oscillation_panel: OscillationPanel | None = None
        self.joystick_dock: CollapsibleDockWidget | None = None
        self.serial_terminal_dock: CollapsibleDockWidget | None = None
        self.serial_connection_dock: CollapsibleDockWidget | None = None
        self.needle_calibration_dock: CollapsibleDockWidget | None = None
        self.oscillation_dock: CollapsibleDockWidget | None = None
        self._needle_calibration_active = False
        self.statusBar()
        self._status_log = QPlainTextEdit(self)
        self._status_log.setReadOnly(True)
        self._status_log.setMaximumHeight(80)
        self._status_log.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._status_log.document().setMaximumBlockCount(200)
        self.statusBar().addPermanentWidget(self._status_log, 1)
        self._status_log_path = (
            self.settings_manager.log_file_path().with_name("status-history.log")
        )
        self.grabber = Grabber()
        self.thread = QThread()
        self.grabber.moveToThread(self.thread)
        self.thread.started.connect(self.grabber.start)
        self.view.clicked.connect(self.on_click)
        self.grabber.frame_ready.connect(self.view.set_frame)
        self.grabber.error.connect(self.on_error)
        self.thread.start()

        self.stage_controller = StageController()
        self.stage_controller.status_message.connect(self._show_status)
        self.stage_controller.movement_finished.connect(self.on_move_finished)
        self.stage_controller.calibration_changed.connect(self.on_calibration_changed)
        self.stage_controller.autofocus_finished.connect(self.on_autofocus_finished)
        self.stage_controller.needle_height_changed.connect(self._on_needle_height_changed)
        self.stage_controller.oscillation_state_changed.connect(
            self._on_oscillation_state_changed
        )
        self.stage_controller.movement_started.connect(
            lambda: self._show_status("Moving stage...")
        )
        self.grabber.frame_ready.connect(self.stage_controller.on_frame_ready)
        self.lcr_controller = LCRMeterController()
        self.lcr_controller.status_message.connect(self._show_status)
        self._needle_height_timer = QTimer(self)
        self._needle_height_timer.setInterval(400)
        self._needle_height_timer.timeout.connect(self._refresh_needle_height)

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
        if self.needle_calibration_dock is not None:
            needle_action = self.needle_calibration_dock.toggleViewAction()
            needle_action.setText("Needle Calibration")
            window_menu.addAction(needle_action)
        if self.oscillation_dock is not None:
            oscillation_action = self.oscillation_dock.toggleViewAction()
            oscillation_action.setText("Oscillation")
            window_menu.addAction(oscillation_action)

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

    def _show_status(self, message: str, timeout_ms: int = 0) -> None:
        if message:
            self.statusBar().showMessage(message, timeout_ms)
            self._status_log.appendPlainText(message)
            self._append_status_log(message)

    def _append_status_log(self, message: str) -> None:
        if not message:
            return
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{timestamp} {message}\n"
        path: Path = self._status_log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError as exc:
            logger.warning("Failed to write status log: %s", exc)

    def _open_status_log(self) -> None:
        path: Path = self._status_log_path
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

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
        self.stage_controller.request_stop_oscillation()
        self._stop_needle_calibration()
        self.stage_controller.set_serial(None)
        auto_retry = self.sender() is not self.serial_connection_panel
        if self.serial_connection_panel:
            self.serial_connection_panel.handle_external_disconnect(auto_retry=auto_retry)
        if self.joystick_panel:
            self.joystick_panel.set_serial(None)
        if self.serial_terminal_panel:
            self.serial_terminal_panel.set_serial(None)
        if self.needle_calibration_panel:
            self.needle_calibration_panel.set_current_a(None)
        if self.oscillation_panel:
            self.oscillation_panel.set_running(False, "")

    def _auto_connect_if_possible(self) -> None:
        if self.serial_connection_panel and not self.serial_connection:
            logger.debug("Attempting auto-connect through connection panel")
            self.serial_connection_panel.auto_connect()

    def _setup_menus(self) -> None:
        app_menu = self.menuBar().addMenu("Application")

        settings_menu = self.menuBar().addMenu("Settings")
        settings_action = QAction("Settings…", self)
        settings_action.triggered.connect(self._open_settings_dialog)
        settings_menu.addAction(settings_action)

        open_log_action = QAction("Open Status Log…", self)
        open_log_action.triggered.connect(self._open_status_log)
        app_menu.addAction(open_log_action)


    def _apply_settings(self) -> None:
        if self.joystick_panel:
            bindings = self.settings_manager.control_bindings()
            self.joystick_panel.apply_control_bindings(bindings)
            logger.debug("Joystick bindings reapplied from settings")
            jog = self.settings_manager.jog_configuration()
            self.joystick_panel.apply_jog_settings(
                jog.linear_distance_mm,
                jog.rotary_distance_deg,
            )
            logger.debug(
                "Joystick jog settings reapplied: linear_distance_mm=%s rotary_distance_deg=%s",
                jog.linear_distance_mm,
                jog.rotary_distance_deg,
            )
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
        needle_settings = self.settings_manager.needle_calibration_configuration()
        self.stage_controller.apply_needle_calibration(
            down_position_mm=(
                needle_settings.down_position_mm
                if needle_settings.down_position_configured
                else None
            ),
            lower_direction=needle_settings.lower_direction,
        )
        self.lcr_controller.apply_configuration(
            resource_name=needle_settings.visa_resource,
            short_threshold_ohm=needle_settings.short_threshold_ohm,
            poll_interval_ms=needle_settings.poll_interval_ms,
        )
        if self.needle_calibration_panel:
            self.needle_calibration_panel.apply_configuration(
                resource_name=needle_settings.visa_resource,
                saved_height=(
                    needle_settings.down_position_mm
                    if needle_settings.down_position_configured
                    else None
                ),
                lower_direction=needle_settings.lower_direction,
                short_threshold_ohm=needle_settings.short_threshold_ohm,
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
            self._show_status(message, 5000)

    def on_autofocus_finished(self, success: bool, message: str) -> None:
        if message:
            self._show_status(message, 5000)
        if not success:
            logger.error("Autofocus failed: %s", message)

    def on_calibration_changed(self, mm_per_pixel_x: float, mm_per_pixel_y: float) -> None:
        self._show_status(
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
        self.stage_controller.request_stop_oscillation()
        self.stage_controller.shutdown()
        self.lcr_controller.shutdown()
        if self.serial_connection_panel:
            self.serial_connection_panel.shutdown()
        event.accept()

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
        self.joystick_panel.set_stage_controller(self.stage_controller)
        jog = self.settings_manager.jog_configuration()
        self.joystick_panel.apply_jog_settings(
            jog.linear_distance_mm,
            jog.rotary_distance_deg,
        )
        feedrates = self.settings_manager.feedrate_configuration()
        self.joystick_panel.apply_feedrate_settings(
            feedrates.linear.presets,
            feedrates.linear.default,
            feedrates.rotary.presets,
            feedrates.rotary.default,
        )
        self.joystick_panel.set_serial(self.serial_connection)
        self.joystick_panel.autofocus_requested.connect(
            self.stage_controller.request_autofocus
        )
        self.joystick_panel.home_axis_requested.connect(
            self.stage_controller.request_home_axis
        )
        self.joystick_panel.home_all_requested.connect(
            self.stage_controller.request_home_all
        )
        self.joystick_panel.needles_raise_requested.connect(
            self.stage_controller.request_needles_raise
        )
        self.joystick_panel.needles_lower_requested.connect(
            self.stage_controller.request_needles_lower
        )
        self.stage_controller.homing_status_changed.connect(
            self.joystick_panel.set_homing_status
        )
        self.stage_controller.homing_action_started.connect(
            self.joystick_panel.set_homing_action_started
        )
        self.stage_controller.homing_action_finished.connect(
            self.joystick_panel.set_homing_action_finished
        )
        self.stage_controller.axis_a_ready_changed.connect(
            self.joystick_panel.set_axis_a_ready
        )
        self.stage_controller.needles_state_changed.connect(
            self.joystick_panel.set_needles_state
        )
        self.stage_controller.needles_action_started.connect(
            self.joystick_panel.set_needles_action_started
        )
        self.stage_controller.needles_action_finished.connect(
            self.joystick_panel.set_needles_action_finished
        )
        self.joystick_panel.reset_requested.connect(
            self.stage_controller.cancel_active_task
        )
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

        self.needle_calibration_panel = NeedleCalibrationPanel(self)
        self.needle_calibration_panel.connect_requested.connect(
            self.lcr_controller.request_connect
        )
        self.needle_calibration_panel.disconnect_requested.connect(
            self.lcr_controller.request_disconnect
        )
        self.needle_calibration_panel.start_requested.connect(
            self._start_needle_calibration
        )
        self.needle_calibration_panel.stop_requested.connect(
            self._stop_needle_calibration
        )
        self.needle_calibration_panel.adjust_requested.connect(
            self.stage_controller.request_needles_adjust
        )
        self.needle_calibration_panel.save_current_requested.connect(
            self._save_current_needle_height
        )
        self.needle_calibration_panel.lower_to_saved_requested.connect(
            self.stage_controller.request_needles_lower
        )
        self.needle_calibration_panel.raise_needles_requested.connect(
            self.stage_controller.request_needles_raise
        )
        self.lcr_controller.connection_changed.connect(
            self._on_lcr_connection_changed
        )
        self.lcr_controller.reading_updated.connect(
            self._on_lcr_reading_updated
        )
        self.needle_calibration_dock = CollapsibleDockWidget(
            "Needle Calibration", self
        )
        self.needle_calibration_dock.setObjectName("NeedleCalibrationDock")
        self.needle_calibration_dock.setWidget(self.needle_calibration_panel)
        self.needle_calibration_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
        )
        self.addDockWidget(Qt.RightDockWidgetArea, self.needle_calibration_dock)

        self.oscillation_panel = OscillationPanel(self)
        self.oscillation_panel.start_requested.connect(
            self.stage_controller.request_oscillation
        )
        self.oscillation_panel.stop_requested.connect(
            self.stage_controller.request_stop_oscillation
        )
        self.oscillation_dock = CollapsibleDockWidget("Oscillation", self)
        self.oscillation_dock.setObjectName("OscillationDock")
        self.oscillation_dock.setWidget(self.oscillation_panel)
        self.oscillation_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
        )
        self.addDockWidget(Qt.RightDockWidgetArea, self.oscillation_dock)
        self.splitDockWidget(
            self.needle_calibration_dock, self.oscillation_dock, Qt.Vertical
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

    def _start_needle_calibration(self) -> None:
        if self.serial_connection is None or not self.serial_connection.is_open:
            self._show_status("Connect the stage controller before needle calibration.")
            return
        if not self.lcr_controller.is_connected():
            self._show_status("Connect the LCR meter before starting calibration.")
            return
        self._needle_calibration_active = True
        self._needle_height_timer.start()
        if self.needle_calibration_panel:
            self.needle_calibration_panel.set_calibration_active(True)
        self.stage_controller.request_needles_raise()
        self._show_status(
            "Needle calibration started. Move above metal and lower the needles in steps until the LCR reports a short."
        )

    def _stop_needle_calibration(self) -> None:
        self._needle_calibration_active = False
        self._needle_height_timer.stop()
        if self.needle_calibration_panel:
            self.needle_calibration_panel.set_calibration_active(False)
        self._show_status("Needle calibration stopped.")

    def _refresh_needle_height(self) -> None:
        if not self._needle_calibration_active:
            return
        a_position = self.stage_controller.current_a_position()
        if a_position is None:
            return
        self._on_needle_height_changed(a_position)

    def _on_needle_height_changed(self, a_position: float) -> None:
        if self.needle_calibration_panel:
            self.needle_calibration_panel.set_current_a(a_position)

    def _on_lcr_connection_changed(
        self, connected: bool, backend_name: str, description: str
    ) -> None:
        if self.needle_calibration_panel:
            self.needle_calibration_panel.set_connection_state(
                connected, backend_name, description
            )
            if not connected:
                self.needle_calibration_panel.set_reading(None, False)

    def _on_lcr_reading_updated(self, resistance_ohm: float, is_short: bool) -> None:
        if self.needle_calibration_panel:
            self.needle_calibration_panel.set_reading(resistance_ohm, is_short)

    def _save_current_needle_height(self) -> None:
        a_position = self.stage_controller.current_a_position()
        if a_position is None:
            self._show_status("Unable to read A position. Wait for the stage to become idle.")
            return
        settings = self.settings_manager.settings.clone()
        settings.needle_calibration.down_position_mm = a_position
        settings.needle_calibration.down_position_configured = True
        self.settings_manager.replace(settings)
        self.settings_manager.save()
        self._apply_settings()
        self._show_status(f"Saved needle down height at A={a_position:.4f} mm.")

    def _on_oscillation_state_changed(self, running: bool, axis: str) -> None:
        if self.oscillation_panel:
            self.oscillation_panel.set_running(running, axis)

def main() -> int:
    app = QApplication(sys.argv)
    window = Main()
    window.showMaximized()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
