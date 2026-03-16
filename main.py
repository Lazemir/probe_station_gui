"""Application entry point for the probe station GUI."""

from __future__ import annotations

import logging
import math
from datetime import datetime
from pathlib import Path
import sys

from PySide6.QtCore import QThread, QTimer, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
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

    ALIGNMENT_MODE_SHORTCUT = "Ctrl+Alt+A"
    ALIGNMENT_CAPTURE_SHORTCUT = "Space"
    ALIGNMENT_TARGET_ANGLES = (-180.0, -90.0, 0.0, 90.0, 180.0)

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
        self.alignment_dock: CollapsibleDockWidget | None = None
        self._alignment_mode_enabled = False
        self._alignment_positions: list[tuple[float, float]] = []
        self._alignment_action: QAction | None = None
        self._alignment_capture_action: QAction | None = None
        self._alignment_exit_action: QAction | None = None
        self._alignment_status_label: QLabel | None = None
        self._alignment_center_label: QLabel | None = None
        self._alignment_cursor_label: QLabel | None = None
        self._alignment_capture_button: QPushButton | None = None
        self._alignment_reset_button: QPushButton | None = None
        self._alignment_exit_button: QPushButton | None = None
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
        self.view.hovered.connect(self._on_view_hover)
        self.view.hover_left.connect(self._on_view_hover_left)
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
        if self.alignment_dock is not None:
            alignment_action = self.alignment_dock.toggleViewAction()
            alignment_action.setText("Chip Alignment")
            window_menu.addAction(alignment_action)

        QTimer.singleShot(0, self._auto_connect_if_possible)

        self.setStyleSheet(
            """
            QMainWindow::separator { width: 8px; height: 8px; background: palette(window); }
            """
        )

    def on_click(self, dx: float, dy: float, _rel_x: float, _rel_y: float) -> None:
        if self._alignment_mode_enabled:
            self._capture_alignment_marker(dx, dy)
            return
        self.stage_controller.request_move(dx, dy)

    def on_error(self, message: str) -> None:
        logger.error("Camera error: %s", message)

    def _on_view_hover(
        self, dx: float, dy: float, _rel_x: float, _rel_y: float
    ) -> None:
        preview = self.stage_controller.preview_clicked_point_xy(dx, dy)
        if preview is None:
            self._update_coordinate_display()
            return
        center_xy, cursor_xy = preview
        self._update_coordinate_display(center_xy=center_xy, cursor_xy=cursor_xy)

    def _on_view_hover_left(self) -> None:
        self._update_coordinate_display(cursor_xy=None)

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
        if self._alignment_mode_enabled:
            self._exit_alignment_mode()

    def _auto_connect_if_possible(self) -> None:
        if self.serial_connection_panel and not self.serial_connection:
            logger.debug("Attempting auto-connect through connection panel")
            self.serial_connection_panel.auto_connect()

    def _setup_menus(self) -> None:
        app_menu = self.menuBar().addMenu("Application")
        tools_menu = self.menuBar().addMenu("Tools")

        settings_menu = self.menuBar().addMenu("Settings")
        settings_action = QAction("Settings…", self)
        settings_action.triggered.connect(self._open_settings_dialog)
        settings_menu.addAction(settings_action)

        open_log_action = QAction("Open Status Log…", self)
        open_log_action.triggered.connect(self._open_status_log)
        app_menu.addAction(open_log_action)


        self._alignment_action = QAction("Chip Alignment Mode", self)
        self._alignment_action.setCheckable(True)
        self._alignment_action.setShortcut(QKeySequence(self.ALIGNMENT_MODE_SHORTCUT))
        self._alignment_action.setShortcutContext(Qt.ApplicationShortcut)
        self._alignment_action.toggled.connect(self._set_alignment_mode_enabled)
        tools_menu.addAction(self._alignment_action)
        self.addAction(self._alignment_action)

        self._alignment_capture_action = QAction("Capture Alignment Marker", self)
        self._alignment_capture_action.setShortcut(
            QKeySequence(self.ALIGNMENT_CAPTURE_SHORTCUT)
        )
        self._alignment_capture_action.setShortcutContext(Qt.ApplicationShortcut)
        self._alignment_capture_action.triggered.connect(
            self._capture_alignment_marker
        )
        self.addAction(self._alignment_capture_action)

        self._alignment_exit_action = QAction("Exit Alignment Mode", self)
        self._alignment_exit_action.setShortcut(QKeySequence(Qt.Key_Escape))
        self._alignment_exit_action.setShortcutContext(Qt.ApplicationShortcut)
        self._alignment_exit_action.triggered.connect(self._exit_alignment_mode)
        self.addAction(self._alignment_exit_action)

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

    def _set_alignment_mode_enabled(self, enabled: bool) -> None:
        self._alignment_mode_enabled = enabled
        self._alignment_positions.clear()
        self.view.clear_target_cross()
        self.view.set_alignment_mode(enabled)
        self._update_alignment_ui()
        self._update_coordinate_display(cursor_xy=None)
        if self.alignment_dock:
            self.alignment_dock.setVisible(enabled)
            if enabled:
                self.alignment_dock.raise_()
        if enabled:
            self._show_status(
                "Chip alignment mode enabled. Center marker 1 and capture it, then move to marker 2.",
                6000,
            )
        else:
            self._show_status("Chip alignment mode disabled.", 3000)

    def _exit_alignment_mode(self) -> None:
        if self._alignment_action and self._alignment_action.isChecked():
            self._alignment_action.setChecked(False)

    def _reset_alignment_capture(self) -> None:
        self._alignment_positions.clear()
        self._update_alignment_ui()
        if self._alignment_mode_enabled:
            self._show_status(
                "Chip alignment capture reset. Center marker 1 and capture it again.",
                5000,
            )

    def _zero_b_axis(self) -> None:
        try:
            self.stage_controller.zero_b_axis()
        except Exception as exc:
            self._show_status(str(exc), 5000)

    def _reset_click_calibration(self) -> None:
        try:
            self.stage_controller.reset_calibration()
        except Exception as exc:
            self._show_status(str(exc), 5000)

    def _capture_alignment_marker(
        self, dx_pixels: float = 0.0, dy_pixels: float = 0.0
    ) -> None:
        if not self._alignment_mode_enabled:
            return
        try:
            center_xy, captured = self.stage_controller.resolve_clicked_point_xy(
                dx_pixels,
                dy_pixels,
            )
        except Exception as exc:
            self._show_status(str(exc), 5000)
            return
        self._update_coordinate_display(center_xy=center_xy, cursor_xy=captured)
        if len(self._alignment_positions) >= 2:
            self._alignment_positions.clear()
        self._alignment_positions.append(captured)
        self._update_alignment_ui()

        if len(self._alignment_positions) == 1:
            self._show_status(
                f"Chip alignment: marker 1 captured at X={captured[0]:.3f}, Y={captured[1]:.3f}. Move to marker 2 and capture it.",
                6000,
            )
            return

        rotation_deg = self._calculate_alignment_rotation(
            self._alignment_positions[0],
            self._alignment_positions[1],
        )
        self._alignment_positions.clear()
        self._update_alignment_ui()

        if rotation_deg is None:
            self._show_status(
                "Chip alignment markers are too close together. Capture two distinct markers.",
                5000,
            )
            return
        if abs(rotation_deg) < 1e-3:
            self._show_status(
                "Chip alignment markers are already aligned.",
                5000,
            )
            return

        self._show_status(
            f"Chip alignment: rotating B by {rotation_deg:+.3f} deg.",
            5000,
        )
        self.stage_controller.request_rotate_b(rotation_deg)

    def _update_alignment_ui(self) -> None:
        if self._alignment_capture_button is not None:
            next_index = 1 if not self._alignment_positions else 2
            self._alignment_capture_button.setText(f"Capture Marker {next_index}")
            self._alignment_capture_button.setEnabled(self._alignment_mode_enabled)
        if self._alignment_reset_button is not None:
            self._alignment_reset_button.setEnabled(
                self._alignment_mode_enabled and bool(self._alignment_positions)
            )
        if self._alignment_exit_button is not None:
            self._alignment_exit_button.setEnabled(self._alignment_mode_enabled)
        if self._alignment_status_label is not None:
            if not self._alignment_mode_enabled:
                text = "Alignment mode is off."
            elif not self._alignment_positions:
                text = (
                    "Click the first marker to record that exact point, or use Capture/Space "
                    "to record the crosshair center."
                )
            else:
                first = self._alignment_positions[0]
                text = (
                    f"Marker 1: X={first[0]:.3f}, Y={first[1]:.3f}\n"
                    "Now move to marker 2 and click it, or capture the center."
                )
            self._alignment_status_label.setText(text)
        if self._alignment_mode_enabled:
            if not self._alignment_positions:
                instruction = "Alignment mode: click marker 1 or press Space for the center."
            else:
                instruction = "Alignment mode: move to marker 2, then click it or press Space."
        else:
            instruction = ""
        self.view.set_alignment_instruction(instruction)

    def _update_coordinate_display(
        self,
        *,
        center_xy: tuple[float, float] | None = None,
        cursor_xy: tuple[float, float] | None = None,
    ) -> None:
        latest = self.stage_controller.latest_stage_position()
        if center_xy is None and latest is not None and len(latest) >= 2:
            center_xy = (float(latest[0]), float(latest[1]))
        if self._alignment_center_label is not None:
            self._alignment_center_label.setText(
                self._format_coordinate_label("Center", center_xy)
            )
        if self._alignment_cursor_label is not None:
            self._alignment_cursor_label.setText(
                self._format_coordinate_label("Cursor", cursor_xy)
            )

    def _format_coordinate_label(
        self, prefix: str, fluidnc_xy: tuple[float, float] | None
    ) -> str:
        if fluidnc_xy is None:
            return f"{prefix}: unavailable"
        systems = self._resolve_coordinate_systems(fluidnc_xy)
        parts = [
            f"{name} X={coords[0]:.3f}, Y={coords[1]:.3f}"
            for name, coords in systems.items()
        ]
        return f"{prefix}: " + " | ".join(parts)

    def _resolve_coordinate_systems(
        self, fluidnc_xy: tuple[float, float]
    ) -> dict[str, tuple[float, float]]:
        coordinates = {"FluidNC abs": fluidnc_xy}
        chip_xy = self._resolve_chip_coordinates(fluidnc_xy)
        if chip_xy is not None:
            coordinates["Chip"] = chip_xy
        return coordinates

    def _resolve_chip_coordinates(
        self, fluidnc_xy: tuple[float, float]
    ) -> tuple[float, float] | None:
        """Hook for future chip-coordinate mappings."""

        _ = fluidnc_xy
        return None

    @classmethod
    def _calculate_alignment_rotation(
        cls,
        first_position: tuple[float, float],
        second_position: tuple[float, float],
    ) -> float | None:
        dx = second_position[0] - first_position[0]
        dy = second_position[1] - first_position[1]
        if math.hypot(dx, dy) <= 1e-6:
            return None
        angle_deg = math.degrees(math.atan2(dy, dx))
        best_delta = min(
            (
                cls._normalise_angle(target - angle_deg)
                for target in cls.ALIGNMENT_TARGET_ANGLES
            ),
            key=lambda value: abs(value),
        )
        if abs(best_delta) > 45.0:
            return None
        return best_delta

    @staticmethod
    def _normalise_angle(angle_deg: float) -> float:
        return ((angle_deg + 180.0) % 360.0) - 180.0

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
        self.joystick_panel.zero_b_requested.connect(self._zero_b_axis)
        self.joystick_panel.reset_calibration_requested.connect(
            self._reset_click_calibration
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

        alignment_panel = QWidget(self)
        alignment_layout = QVBoxLayout(alignment_panel)
        alignment_layout.setContentsMargins(8, 8, 8, 8)
        alignment_layout.setSpacing(8)

        self._alignment_status_label = QLabel(alignment_panel)
        self._alignment_status_label.setWordWrap(True)
        alignment_layout.addWidget(self._alignment_status_label)

        self._alignment_center_label = QLabel(alignment_panel)
        self._alignment_center_label.setWordWrap(True)
        alignment_layout.addWidget(self._alignment_center_label)

        self._alignment_cursor_label = QLabel(alignment_panel)
        self._alignment_cursor_label.setWordWrap(True)
        alignment_layout.addWidget(self._alignment_cursor_label)

        button_row = QHBoxLayout()
        self._alignment_capture_button = QPushButton("Capture Marker 1", alignment_panel)
        self._alignment_capture_button.clicked.connect(self._capture_alignment_marker)
        button_row.addWidget(self._alignment_capture_button)
        self._alignment_reset_button = QPushButton("Reset", alignment_panel)
        self._alignment_reset_button.clicked.connect(self._reset_alignment_capture)
        button_row.addWidget(self._alignment_reset_button)
        alignment_layout.addLayout(button_row)

        self._alignment_exit_button = QPushButton("Exit Mode", alignment_panel)
        self._alignment_exit_button.clicked.connect(self._exit_alignment_mode)
        alignment_layout.addWidget(self._alignment_exit_button)
        alignment_layout.addStretch(1)

        self.alignment_dock = CollapsibleDockWidget("Chip Alignment", self)
        self.alignment_dock.setObjectName("ChipAlignmentDock")
        self.alignment_dock.setWidget(alignment_panel)
        self.alignment_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
        )
        self.addDockWidget(Qt.RightDockWidgetArea, self.alignment_dock)
        self.alignment_dock.setVisible(False)
        self._update_alignment_ui()
        self._update_coordinate_display()

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
