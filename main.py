"""Application entry point for the probe station GUI."""

from __future__ import annotations

import logging
import math
import time
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
from probe_station_gui.design_model import DesignDocument, DesignModelError
from probe_station_gui.design_script import ScriptContext, load_measurement_plan
from probe_station_gui.design_session import AlignmentPreparation, DesignSession
from probe_station_gui.dialogs.settings_dialog import SettingsDialog
from probe_station_gui.lcr_meter import LCRMeterController
from probe_station_gui.settings_manager import SettingsManager
from probe_station_gui.views.design_navigator_panel import (
    DesignLayoutWindow,
    DesignNavigatorPanel,
)
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
    DESIGN_POSITION_REFRESH_MS = 800
    DESIGN_OVERLAY_UPDATE_MS = 120
    DESIGN_SPACING_RATIO_TOLERANCE = 0.35
    MANUAL_JOG_UPDATE_MS = 50
    MANUAL_JOG_SETTLE_POLL_DELAYS_MS = (180, 420)
    MANUAL_JOG_IGNORE_IDLE_AFTER_COMMAND_S = 0.25
    MANUAL_JOG_RECONCILE_SMOOTH_THRESHOLD_MM = 0.35
    MANUAL_JOG_RECONCILE_SMOOTH_ALPHA = 0.35
    TERMINAL_REFRESH_DELAYS_MS = (180, 500)
    TERMINAL_RESET_REFRESH_DELAYS_MS = (500, 1100, 1800)
    TERMINAL_RESUME_AFTER_JOG_MS = 180
    B_POSITION_CHANGE_TOLERANCE_DEG = 1e-3
    STARTUP_AUTO_CONNECT_DELAY_MS = 400
    SERIAL_STARTUP_SYNC_DELAY_MS = 150
    STARTUP_FOCUS_DELAY_MS = 120

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
        self.design_navigator_panel: DesignNavigatorPanel | None = None
        self.design_layout_window: DesignLayoutWindow | None = None
        self.oscillation_window: QMainWindow | None = None
        self.joystick_dock: CollapsibleDockWidget | None = None
        self.serial_terminal_dock: CollapsibleDockWidget | None = None
        self.serial_connection_dock: CollapsibleDockWidget | None = None
        self.needle_calibration_dock: CollapsibleDockWidget | None = None
        self.design_navigator_dock: CollapsibleDockWidget | None = None
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
        self._design_layout_window_action: QAction | None = None
        self._oscillation_window_action: QAction | None = None
        self._last_selected_design_point: tuple[float, float] | None = None
        self._current_design_stage_xy: tuple[float, float] | None = None
        self._pending_design_stage_xy: tuple[float, float] | None = None
        self._pending_alignment_preparation: AlignmentPreparation | None = None
        self._manual_jog_stage_xy: tuple[float, float] | None = None
        self._manual_jog_velocity_xy: tuple[float, float] | None = None
        self._manual_jog_last_timestamp: float | None = None
        self._manual_jog_last_prediction_log_at = 0.0
        self._last_reported_b_position: float | None = None
        self._design_session = DesignSession()
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
        self.view.design_minimap_double_clicked.connect(
            lambda: self._toggle_design_layout_window(True)
        )
        self.grabber.frame_ready.connect(self.view.set_frame)
        self.grabber.error.connect(self.on_error)
        self.thread.start()

        self.stage_controller = StageController()
        self.stage_controller.status_message.connect(self._show_status)
        self.stage_controller.movement_finished.connect(self.on_move_finished)
        self.stage_controller.calibration_changed.connect(self.on_calibration_changed)
        self.stage_controller.autofocus_finished.connect(self.on_autofocus_finished)
        self.stage_controller.stage_position_changed.connect(self._on_stage_position_changed)
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
        self._design_position_timer = QTimer(self)
        self._design_position_timer.setInterval(self.DESIGN_POSITION_REFRESH_MS)
        self._design_position_timer.timeout.connect(self._refresh_design_position)
        self._design_position_timer.start()
        self._design_overlay_timer = QTimer(self)
        self._design_overlay_timer.setSingleShot(True)
        self._design_overlay_timer.setInterval(self.DESIGN_OVERLAY_UPDATE_MS)
        self._design_overlay_timer.timeout.connect(self._flush_pending_design_position)
        self._manual_jog_timer = QTimer(self)
        self._manual_jog_timer.setInterval(self.MANUAL_JOG_UPDATE_MS)
        self._manual_jog_timer.timeout.connect(self._advance_manual_jog_prediction)
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
        if self.alignment_dock is not None:
            alignment_action = self.alignment_dock.toggleViewAction()
            alignment_action.setText("Chip Alignment")
            window_menu.addAction(alignment_action)

        QTimer.singleShot(
            self.STARTUP_AUTO_CONNECT_DELAY_MS, self._auto_connect_if_possible
        )
        QTimer.singleShot(self.STARTUP_FOCUS_DELAY_MS, self._prime_keyboard_focus)

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
        self._last_reported_b_position = None
        self.stage_controller.set_serial(self.serial_connection)
        self._restore_persisted_controller_state()
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
        QTimer.singleShot(
            self.SERIAL_STARTUP_SYNC_DELAY_MS, self._run_serial_startup_sync
        )
        self._refresh_design_position()

    def on_serial_disconnected(self) -> None:
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
        self.serial_connection = None
        self._last_reported_b_position = None
        self._manual_jog_timer.stop()
        self._manual_jog_stage_xy = None
        self._manual_jog_velocity_xy = None
        self._manual_jog_last_timestamp = None
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
        self._invalidate_design_registration(
            "Design registration cleared after serial disconnect."
        )
        self._update_design_position(None)

    def _auto_connect_if_possible(self) -> None:
        if self.serial_connection_panel and not self.serial_connection:
            logger.debug("Attempting auto-connect through connection panel")
            self.serial_connection_panel.auto_connect()

    def _run_serial_startup_sync(self) -> None:
        if self.serial_connection is None or not self.serial_connection.is_open:
            return
        self.stage_controller.request_startup_sync(auto_home_a=True)

    def _prime_keyboard_focus(self) -> None:
        if not self.isVisible():
            return
        self.raise_()
        self.activateWindow()
        self.view.setFocus(Qt.ActiveWindowFocusReason)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        QTimer.singleShot(0, self._prime_keyboard_focus)

    def _restore_persisted_controller_state(self) -> None:
        if self.serial_connection is None or not self.serial_connection.is_open:
            return
        if bool(
            getattr(self.serial_connection, "probe_station_reboot_detected", False)
        ):
            self.settings_manager.clear_controller_state()
            self.stage_controller.clear_cached_controller_state()
            self._show_status("Controller reboot detected. Cleared cached controller state.")
            return
        cached_state = self.settings_manager.load_controller_state()
        if not cached_state:
            return
        self.stage_controller.import_cached_controller_state(cached_state)
        self._show_status("Restored cached controller state from previous session.")

    def _persist_controller_state(self, *_args) -> None:
        self.settings_manager.save_controller_state(
            self.stage_controller.export_cached_controller_state()
        )

    def _setup_menus(self) -> None:
        app_menu = self.menuBar().addMenu("Application")
        tools_menu = self.menuBar().addMenu("Tools")
        design_menu = self.menuBar().addMenu("Design")
        oscillation_menu = self.menuBar().addMenu("Oscillation")

        settings_menu = self.menuBar().addMenu("Settings")
        settings_action = QAction("Settings…", self)
        settings_action.triggered.connect(self._open_settings_dialog)
        settings_menu.addAction(settings_action)

        open_log_action = QAction("Open Status Log…", self)
        open_log_action.triggered.connect(self._open_status_log)
        app_menu.addAction(open_log_action)

        self._design_layout_window_action = QAction("Design Window", self)
        self._design_layout_window_action.setCheckable(True)
        self._design_layout_window_action.toggled.connect(
            self._toggle_design_layout_window
        )
        design_menu.addAction(self._design_layout_window_action)

        self._oscillation_window_action = QAction("Open Controls", self)
        self._oscillation_window_action.triggered.connect(self._show_oscillation_window)
        oscillation_menu.addAction(self._oscillation_window_action)

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

    def _toggle_design_layout_window(self, visible: bool) -> None:
        if self.design_layout_window is None:
            if self._design_layout_window_action is not None:
                self._design_layout_window_action.blockSignals(True)
                self._design_layout_window_action.setChecked(False)
                self._design_layout_window_action.blockSignals(False)
            return
        if visible:
            self.design_layout_window.show_and_raise()
        else:
            self.design_layout_window.hide()

    def _on_design_layout_window_visibility_changed(self, visible: bool) -> None:
        if self._design_layout_window_action is None:
            return
        self._design_layout_window_action.blockSignals(True)
        self._design_layout_window_action.setChecked(visible)
        self._design_layout_window_action.blockSignals(False)

    def _show_oscillation_window(self) -> None:
        if self.oscillation_window is None:
            return
        self.oscillation_window.show()
        self.oscillation_window.raise_()
        self.oscillation_window.activateWindow()

    def _on_design_layout_point_selected(
        self, slot: int, x_value: float, y_value: float
    ) -> None:
        document = self._design_session.document
        if document is None:
            return
        snapped_point = (float(x_value), float(y_value))
        self._last_selected_design_point = snapped_point
        self._design_session.set_source_design_mark(slot, snapped_point)
        self._refresh_design_panel()
        slot_label = "1" if slot == 0 else "2"
        self._show_status(
            f"Design mark {slot_label} snapped to X={snapped_point[0]:.3f}, Y={snapped_point[1]:.3f}.",
            4000,
        )

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
            dcr_range=needle_settings.dcr_range,
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
            self._invalidate_design_registration(
                "Design registration cleared after B-axis zeroing."
            )
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
        self._invalidate_design_registration(
            "Design registration cleared after B-axis rotation."
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
            coordinates["Chip/Stage registered"] = chip_xy
        design_xy = self._resolve_design_coordinates(fluidnc_xy)
        if design_xy is not None:
            coordinates["Design"] = design_xy
        return coordinates

    def _resolve_chip_coordinates(
        self, fluidnc_xy: tuple[float, float]
    ) -> tuple[float, float] | None:
        registration = self._design_session.registration
        if registration is None or not registration.valid:
            return None
        if not registration.source_stage_marks:
            return None
        origin = registration.source_stage_marks[0]
        return (
            float(fluidnc_xy[0]) - float(origin[0]),
            float(fluidnc_xy[1]) - float(origin[1]),
        )

    def _resolve_design_coordinates(
        self, fluidnc_xy: tuple[float, float]
    ) -> tuple[float, float] | None:
        try:
            return self._design_session.design_from_stage(fluidnc_xy)
        except Exception:
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

    def _on_manual_motion_axis(self, axis: str) -> None:
        if axis.upper() == "B":
            self._invalidate_design_registration(
                "Design registration cleared after manual B-axis motion."
            )

    def _on_manual_jog_command_changed(
        self, commanded_distances: object, feedrate: float
    ) -> None:
        if not isinstance(commanded_distances, tuple):
            return
        if self.serial_terminal_panel is not None:
            self.serial_terminal_panel.set_live_poll_paused(True)
        xy_components: dict[str, float] = {"X": 0.0, "Y": 0.0}
        for item in commanded_distances:
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            axis = str(item[0]).upper()
            try:
                distance = float(item[1])
            except (TypeError, ValueError):
                continue
            if axis in xy_components:
                xy_components[axis] = distance
        path_length = math.hypot(xy_components["X"], xy_components["Y"])
        if path_length <= 1e-9:
            logger.debug("DESIGN MINIMAP prediction_stop_requested command=%s", commanded_distances)
            self._manual_jog_velocity_xy = None
            self._manual_jog_timer.stop()
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
            return
        speed_mm_per_s = max(0.0, float(feedrate)) / 60.0
        self._manual_jog_velocity_xy = (
            speed_mm_per_s * xy_components["X"] / path_length,
            speed_mm_per_s * xy_components["Y"] / path_length,
        )
        latest = self.stage_controller.latest_stage_position()
        if latest is not None and len(latest) >= 2:
            self._manual_jog_stage_xy = (float(latest[0]), float(latest[1]))
        elif self._current_design_stage_xy is not None:
            self._manual_jog_stage_xy = self._current_design_stage_xy
        self._manual_jog_last_timestamp = time.monotonic()
        self._manual_jog_last_prediction_log_at = 0.0
        logger.debug(
            "DESIGN MINIMAP prediction_start stage=%s design=%s velocity=(%.4f, %.4f) feedrate=%.3f command=%s",
            self._format_optional_point(self._manual_jog_stage_xy),
            self._format_optional_point(
                self._design_session.design_from_stage(self._manual_jog_stage_xy)
                if self._manual_jog_stage_xy is not None
                else None
            ),
            self._manual_jog_velocity_xy[0],
            self._manual_jog_velocity_xy[1],
            float(feedrate),
            commanded_distances,
        )
        if not self._manual_jog_timer.isActive():
            self._manual_jog_timer.start()

    def _on_manual_jog_stopped(self) -> None:
        logger.debug(
            "DESIGN MINIMAP prediction_stop stage=%s design=%s",
            self._format_optional_point(self._manual_jog_stage_xy),
            self._format_optional_point(
                self._design_session.design_from_stage(self._manual_jog_stage_xy)
                if self._manual_jog_stage_xy is not None
                else None
            ),
        )
        self._manual_jog_timer.stop()
        self._manual_jog_velocity_xy = None
        self._manual_jog_last_timestamp = None
        self._manual_jog_last_prediction_log_at = 0.0
        if self.serial_terminal_panel is not None:
            QTimer.singleShot(
                self.TERMINAL_RESUME_AFTER_JOG_MS,
                lambda: self.serial_terminal_panel
                and self.serial_terminal_panel.set_live_poll_paused(False),
            )
        self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)

    def _advance_manual_jog_prediction(self) -> None:
        if self._manual_jog_velocity_xy is None:
            self._manual_jog_timer.stop()
            return
        now = time.monotonic()
        if self._manual_jog_last_timestamp is None:
            self._manual_jog_last_timestamp = now
            return
        dt = max(0.0, now - self._manual_jog_last_timestamp)
        self._manual_jog_last_timestamp = now
        if dt <= 0.0:
            return
        if self._manual_jog_stage_xy is None:
            latest = self.stage_controller.latest_stage_position()
            if latest is None or len(latest) < 2:
                return
            self._manual_jog_stage_xy = (float(latest[0]), float(latest[1]))
        self._manual_jog_stage_xy = (
            float(self._manual_jog_stage_xy[0] + self._manual_jog_velocity_xy[0] * dt),
            float(self._manual_jog_stage_xy[1] + self._manual_jog_velocity_xy[1] * dt),
        )
        if now - self._manual_jog_last_prediction_log_at >= 0.15:
            logger.debug(
                "DESIGN MINIMAP prediction_tick stage=%s design=%s dt=%.4f velocity=(%.4f, %.4f)",
                self._format_optional_point(self._manual_jog_stage_xy),
                self._format_optional_point(
                    self._design_session.design_from_stage(self._manual_jog_stage_xy)
                ),
                dt,
                self._manual_jog_velocity_xy[0],
                self._manual_jog_velocity_xy[1],
            )
            self._manual_jog_last_prediction_log_at = now
        self._update_coordinate_display(center_xy=self._manual_jog_stage_xy)
        self._update_design_position(self._manual_jog_stage_xy)

    def _schedule_status_refreshes(self, delays_ms: tuple[int, ...]) -> None:
        for delay_ms in delays_ms:
            QTimer.singleShot(delay_ms, self.stage_controller.request_status_refresh)

    def _on_manual_terminal_command(self, command: str) -> None:
        if command == "CTRL-X":
            self._schedule_status_refreshes(self.TERMINAL_RESET_REFRESH_DELAYS_MS)
            return
        self._schedule_status_refreshes(self.TERMINAL_REFRESH_DELAYS_MS)

    def on_move_finished(self, success: bool, message: str) -> None:
        if self._pending_alignment_preparation is not None:
            preparation = self._pending_alignment_preparation
            self._pending_alignment_preparation = None
            if success:
                self._design_session.apply_prepared_alignment(preparation)
                self._refresh_design_panel()
                self._refresh_design_position()
                self._toggle_design_layout_window(False)
                self.view.clear_target_cross()
                self._show_status(
                    "Design calibration complete. "
                    f"Rotation {preparation.rotation_deg:+.3f} deg, "
                    f"spacing ratio {preparation.distance_ratio:.3f}.",
                    7000,
                )
            else:
                self._show_status(
                    f"Design calibration rotation failed: {message}",
                    7000,
                )
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
            return
        if success:
            self.view.clear_target_cross()
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
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

    def _load_design_document(self, design_path: str) -> None:
        try:
            document = DesignDocument.load(design_path)
        except DesignModelError as exc:
            self._show_status(str(exc), 6000)
            if self.design_navigator_panel:
                self.design_navigator_panel.set_status_message(str(exc))
            return
        self._design_session.load_document(document)
        self._pending_alignment_preparation = None
        self._last_selected_design_point = None
        self._refresh_design_panel()
        self._refresh_design_position()
        self._toggle_design_layout_window(True)
        self._show_status(
            f"Loaded design '{document.path.name}' ({document.top_cell_name}).",
            5000,
        )

    def _unload_design_document(self) -> None:
        if self._design_session.document is None:
            return
        document_name = self._design_session.document.path.name
        self._design_session.unload_document()
        self._pending_alignment_preparation = None
        self._last_selected_design_point = None
        self._refresh_design_panel()
        self._update_design_position(None)
        self._show_status(f"Unloaded design '{document_name}'.", 5000)

    def _set_design_top_cell(self, top_cell_name: str) -> None:
        try:
            self._design_session.set_top_cell(top_cell_name)
        except DesignModelError as exc:
            self._show_status(str(exc), 6000)
            return
        self._pending_alignment_preparation = None
        self._last_selected_design_point = None
        self._refresh_design_panel()
        self._refresh_design_position()
        self._show_status(f"Switched design top cell to '{top_cell_name}'.", 5000)

    def _set_design_layer_visibility(
        self, layer: int, datatype: int, visible: bool
    ) -> None:
        document = self._design_session.document
        if document is None:
            return
        visible_layers = set(document.visible_layers)
        layer_key = (int(layer), int(datatype))
        if visible:
            visible_layers.add(layer_key)
        else:
            visible_layers.discard(layer_key)
        try:
            self._design_session.set_visible_layers(visible_layers)
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        self._refresh_design_panel()

    def _load_measurement_script(self, script_path: str) -> None:
        document = self._design_session.document
        if document is None:
            self._show_status("Load a design before loading a measurement plan.", 5000)
            return
        try:
            module, targets = load_measurement_plan(script_path, ScriptContext(document))
        except DesignModelError as exc:
            self._show_status(str(exc), 7000)
            return
        self._design_session.script_path = script_path
        self._design_session.script_module_name = module.__name__
        self._design_session.set_targets(targets)
        self._refresh_design_panel()
        self._show_status(
            f"Loaded measurement plan '{Path(script_path).name}' with {len(targets)} targets.",
            5000,
        )

    def _reload_measurement_script(self) -> None:
        script_path = self._design_session.script_path
        if not script_path:
            self._show_status("No measurement script is loaded.", 5000)
            return
        self._load_measurement_script(script_path)

    def _add_design_source_mark(self, x_value: float, y_value: float) -> None:
        self._design_session.add_source_design_mark((x_value, y_value))
        self._refresh_design_panel()
        self._show_status(
            f"Design source mark captured at X={x_value:.3f}, Y={y_value:.3f}.",
            4000,
        )

    def _add_design_check_mark(self, x_value: float, y_value: float) -> None:
        self._design_session.add_check_design_mark((x_value, y_value))
        self._refresh_design_panel()
        self._show_status(
            f"Design check mark captured at X={x_value:.3f}, Y={y_value:.3f}.",
            4000,
        )

    def _capture_stage_source_mark(self) -> None:
        self._capture_stage_registration_mark(check_mark=False)

    def _capture_stage_check_mark(self) -> None:
        self._capture_stage_registration_mark(check_mark=True)

    def _capture_stage_registration_mark(self, *, check_mark: bool) -> None:
        try:
            stage_position = self.stage_controller.current_stage_position()
        except Exception as exc:
            self._show_status(str(exc), 6000)
            return
        if len(stage_position) < 2:
            self._show_status("X/Y coordinates are unavailable.", 5000)
            return
        stage_xy = (float(stage_position[0]), float(stage_position[1]))
        if check_mark:
            self._design_session.add_check_stage_mark(stage_xy)
            label = "check"
        else:
            self._design_session.add_source_stage_mark(stage_xy)
            label = "source"
        self._refresh_design_panel()
        self._refresh_design_position()
        self._show_status(
            f"Stage {label} mark captured at X={stage_xy[0]:.3f}, Y={stage_xy[1]:.3f}.",
            4000,
        )

    def _capture_calibration_chip_point(self, slot: int) -> None:
        if slot not in (0, 1):
            return
        design_point = self._design_session.source_design_marks[slot]
        if design_point is None:
            self._show_status(
                f"Choose design mark {slot + 1} in the design window first.",
                5000,
            )
            return
        try:
            stage_position = self.stage_controller.current_stage_position()
        except Exception as exc:
            self._show_status(str(exc), 6000)
            return
        if len(stage_position) < 2:
            self._show_status("X/Y coordinates are unavailable.", 5000)
            return
        stage_xy = (float(stage_position[0]), float(stage_position[1]))
        self._design_session.set_source_stage_mark(slot, stage_xy)
        pair_count = self._design_session.source_pair_count()
        self._refresh_design_panel()
        self._refresh_design_position()
        if pair_count < 2:
            self._show_status(
                f"Chip mark {slot + 1} captured. Capture the other chip mark to finish calibration.",
                6000,
            )
            return
        try:
            preparation = self._design_session.prepare_source_alignment()
        except DesignModelError as exc:
            self._show_status(str(exc), 7000)
            return
        if not self._design_spacing_ratio_is_reasonable(preparation.distance_ratio):
            self._show_status(
                "Design calibration aborted: mark spacing mismatch. "
                f"Design {preparation.design_distance_mm:.4f} mm vs chip {preparation.stage_distance_mm:.4f} mm.",
                8000,
            )
            return
        if abs(preparation.rotation_deg) < 1e-3:
            self._design_session.apply_prepared_alignment(preparation)
            self._refresh_design_panel()
            self._refresh_design_position()
            self._toggle_design_layout_window(False)
            self._show_status(
                "Design calibration complete. "
                f"Spacing ratio {preparation.distance_ratio:.3f}.",
                7000,
            )
            return
        self._pending_alignment_preparation = preparation
        self._show_status(
            "Two mark pairs captured. "
            f"Rotating chip by {preparation.rotation_deg:+.3f} deg to match the design.",
            7000,
        )
        self.stage_controller.request_rotate_b(preparation.rotation_deg)

    def _design_spacing_ratio_is_reasonable(self, ratio: float) -> bool:
        return abs(float(ratio) - 1.0) <= self.DESIGN_SPACING_RATIO_TOLERANCE

    def _clear_design_registration(self) -> None:
        self._pending_alignment_preparation = None
        self._last_selected_design_point = None
        self._design_session.clear_registration()
        self._refresh_design_panel()
        self._refresh_design_position()
        self._show_status("Design calibration restarted.", 4000)

    def _invalidate_design_registration(self, reason: str) -> None:
        self._pending_alignment_preparation = None
        self._design_session.invalidate_registration(reason)
        self._refresh_design_panel()
        latest = self.stage_controller.latest_stage_position()
        if latest is not None and len(latest) >= 2:
            self._update_design_position((float(latest[0]), float(latest[1])))
        else:
            self._update_design_position(None)

    def _on_design_target_selected(self, target_id: str) -> None:
        self._design_session.select_target_by_id(target_id)
        self._refresh_design_panel()

    def _select_next_design_target(self) -> None:
        target = self._design_session.select_next_target()
        self._refresh_design_panel()
        if target is not None:
            self._show_status(f"Selected target '{target.label}'.", 3000)

    def _select_previous_design_target(self) -> None:
        target = self._design_session.select_previous_target()
        self._refresh_design_panel()
        if target is not None:
            self._show_status(f"Selected target '{target.label}'.", 3000)

    def _move_to_design_target(self, target_id: str) -> None:
        target = self._design_session.select_target_by_id(target_id)
        if target is None:
            self._show_status(f"Unknown target '{target_id}'.", 5000)
            return
        stage_xy = self._design_session.selected_target_stage_xy()
        if stage_xy is None:
            self._show_status(
                "Design registration is required before moving to a target.",
                6000,
            )
            return
        self._refresh_design_panel()
        self.stage_controller.request_move_to_xy(stage_xy[0], stage_xy[1])

    def _refresh_design_panel(self) -> None:
        panel = self.design_navigator_panel
        current_target = self._design_session.current_target()
        selected_target_id = current_target.id if current_target else None
        if panel is not None:
            panel.set_document(self._design_session.document)
            panel.set_script_path(self._design_session.script_path)
            panel.set_targets(
                self._design_session.targets,
                selected_target_id=selected_target_id,
            )
            if self._pending_alignment_preparation is not None:
                panel.set_calibration_prompt(
                    "Calibration step 4/4: chip rotation is in progress."
                )
            else:
                panel.set_calibration_prompt(self._design_session.calibration_prompt())
            panel.set_registration_status(self._design_session.registration_status)
            panel.set_registration_marks(
                self._design_session.source_design_marks,
                self._design_session.check_design_marks,
            )
            panel.set_stage_registration_marks(self._design_session.source_stage_marks)
        if self.design_layout_window is not None:
            self.design_layout_window.set_document(self._design_session.document)
            self.design_layout_window.set_targets(
                self._design_session.targets,
                selected_target_id=selected_target_id,
            )
            self.design_layout_window.set_registration_marks(
                self._design_session.source_design_marks,
                self._design_session.check_design_marks,
            )
            self.design_layout_window.set_stage_registration_marks(
                self._design_session.source_stage_marks
            )
        self._update_design_position(self._current_design_stage_xy)

    def _on_stage_position_changed(self, position: object) -> None:
        if not isinstance(position, tuple) or len(position) < 2:
            return
        logger.debug("TIMING stage_position_changed position=%s", position)
        if len(position) > 4:
            current_b = float(position[4])
            if (
                self._last_reported_b_position is not None
                and self._pending_alignment_preparation is None
                and abs(current_b - self._last_reported_b_position)
                > self.B_POSITION_CHANGE_TOLERANCE_DEG
                and self._design_session.registration is not None
                and self._design_session.registration.valid
            ):
                self._invalidate_design_registration(
                    "Design registration cleared after B-axis motion."
                )
            self._last_reported_b_position = current_b
        predicted_stage_xy = (
            self._manual_jog_stage_xy
            if self._manual_jog_velocity_xy is not None
            else None
        )
        center_xy = (float(position[0]), float(position[1]))
        if self._should_ignore_manual_jog_status_sample(center_xy):
            return
        if predicted_stage_xy is not None:
            self._log_design_position_reconcile(predicted_stage_xy, center_xy)
            center_xy = self._smooth_manual_jog_actual_position(predicted_stage_xy, center_xy)
        self._manual_jog_stage_xy = center_xy
        if self._manual_jog_velocity_xy is not None:
            self._manual_jog_last_timestamp = time.monotonic()
        self._update_coordinate_display(center_xy=center_xy)
        self._pending_design_stage_xy = center_xy
        if not self._design_overlay_timer.isActive():
            self._design_overlay_timer.start()

    def _refresh_design_position(self) -> None:
        if self._design_session.document is None:
            return
        if self.serial_connection is None or not self.serial_connection.is_open:
            self._update_design_position(None)
            return
        latest = self.stage_controller.latest_stage_position()
        if latest is None or len(latest) < 2:
            self.stage_controller.request_status_refresh()
            return
        self._pending_design_stage_xy = (float(latest[0]), float(latest[1]))
        self._flush_pending_design_position()

    def _flush_pending_design_position(self) -> None:
        stage_xy = self._pending_design_stage_xy
        self._pending_design_stage_xy = None
        self._update_design_position(stage_xy)

    def _update_design_position(self, stage_xy: tuple[float, float] | None) -> None:
        self._current_design_stage_xy = stage_xy
        design_xy = None
        fov_design_size = None
        if stage_xy is not None:
            design_xy = self._design_session.design_from_stage(stage_xy)
            fov_design_size = self._resolve_design_fov_size()
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_current_position(
                stage_xy,
                design_xy,
                fov_design_size=fov_design_size,
            )
        if self.design_layout_window is not None:
            self.design_layout_window.set_current_design_position(
                design_xy,
                fov_design_size=fov_design_size,
            )
        current_target = self._design_session.current_target()
        self.view.set_design_minimap_data(
            document=self._design_session.document,
            targets=self._design_session.targets,
            selected_target_id=current_target.id if current_target else None,
            selected_design_point=self._last_selected_design_point,
            current_design_position=design_xy,
            fov_design_size=fov_design_size,
            source_design_marks=self._design_session.source_design_marks_compact(),
            check_design_marks=self._design_session.check_design_marks,
        )

    def _log_design_position_reconcile(
        self,
        predicted_stage_xy: tuple[float, float],
        actual_stage_xy: tuple[float, float],
    ) -> None:
        state = self.stage_controller.latest_stage_state()
        predicted_design_xy = self._design_session.design_from_stage(predicted_stage_xy)
        actual_design_xy = self._design_session.design_from_stage(actual_stage_xy)
        delta_x = float(actual_stage_xy[0] - predicted_stage_xy[0])
        delta_y = float(actual_stage_xy[1] - predicted_stage_xy[1])
        logger.debug(
            "DESIGN MINIMAP reconcile predicted_stage=%s actual_stage=%s delta=(%.4f, %.4f) delta_norm=%.4f state=%s predicted_design=%s actual_design=%s",
            self._format_optional_point(predicted_stage_xy),
            self._format_optional_point(actual_stage_xy),
            delta_x,
            delta_y,
            math.hypot(delta_x, delta_y),
            state,
            self._format_optional_point(predicted_design_xy),
            self._format_optional_point(actual_design_xy),
        )

    def _should_ignore_manual_jog_status_sample(
        self,
        actual_stage_xy: tuple[float, float],
    ) -> bool:
        if self._manual_jog_velocity_xy is None:
            return False
        state = (self.stage_controller.latest_stage_state() or "").lower()
        if state != "idle":
            return False
        last_jog_write = self.stage_controller.last_jog_write_timestamp()
        if last_jog_write is None:
            return False
        age = time.monotonic() - last_jog_write
        if age > self.MANUAL_JOG_IGNORE_IDLE_AFTER_COMMAND_S:
            return False
        logger.debug(
            "DESIGN MINIMAP ignored_idle_sample stage=%s age=%.3f state=%s",
            self._format_optional_point(actual_stage_xy),
            age,
            state,
        )
        return True

    def _smooth_manual_jog_actual_position(
        self,
        predicted_stage_xy: tuple[float, float],
        actual_stage_xy: tuple[float, float],
    ) -> tuple[float, float]:
        delta_x = float(actual_stage_xy[0] - predicted_stage_xy[0])
        delta_y = float(actual_stage_xy[1] - predicted_stage_xy[1])
        delta_norm = math.hypot(delta_x, delta_y)
        state = (self.stage_controller.latest_stage_state() or "").lower()
        if (
            delta_norm <= self.MANUAL_JOG_RECONCILE_SMOOTH_THRESHOLD_MM
            or state not in {"jog", "run"}
        ):
            return actual_stage_xy
        alpha = self.MANUAL_JOG_RECONCILE_SMOOTH_ALPHA
        smoothed = (
            float(predicted_stage_xy[0] + delta_x * alpha),
            float(predicted_stage_xy[1] + delta_y * alpha),
        )
        logger.debug(
            "DESIGN MINIMAP reconcile_smoothed predicted_stage=%s actual_stage=%s smoothed_stage=%s delta_norm=%.4f alpha=%.2f",
            self._format_optional_point(predicted_stage_xy),
            self._format_optional_point(actual_stage_xy),
            self._format_optional_point(smoothed),
            delta_norm,
            alpha,
        )
        return smoothed

    @staticmethod
    def _format_optional_point(point: tuple[float, float] | None) -> str:
        if point is None:
            return "None"
        return f"({float(point[0]):.4f}, {float(point[1]):.4f})"

    def _resolve_design_fov_size(self) -> tuple[float, float] | None:
        registration = self._design_session.registration
        if registration is None or not registration.valid:
            return None
        stage_fov = self.stage_controller.current_fov_size_mm()
        if stage_fov is None:
            return None
        try:
            inverse = np.linalg.inv(registration.matrix)
        except np.linalg.LinAlgError:
            return None
        width_vec = inverse @ np.asarray([float(stage_fov[0]), 0.0], dtype=float)
        height_vec = inverse @ np.asarray([0.0, float(stage_fov[1])], dtype=float)
        return (float(np.linalg.norm(width_vec)), float(np.linalg.norm(height_vec)))

    def _on_homing_action_finished(
        self, success: bool, _message: str, axis_key: str
    ) -> None:
        if not success:
            return
        if axis_key.upper() in {"X", "Y", "B", "ALL"}:
            self._invalidate_design_registration(
                f"Design registration cleared after homing {axis_key.upper()}."
            )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._design_position_timer.stop()
        self._manual_jog_timer.stop()
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
        if self.design_layout_window is not None:
            self.design_layout_window.close()
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
        self.joystick_panel.reset_calibration_requested.connect(
            self._reset_click_calibration
        )
        self.joystick_panel.motion_axis_requested.connect(self._on_manual_motion_axis)
        self.joystick_panel.jog_command_changed.connect(
            self._on_manual_jog_command_changed
        )
        self.joystick_panel.jog_stopped.connect(self._on_manual_jog_stopped)
        self.joystick_panel.reset_requested.connect(
            lambda: self._invalidate_design_registration(
                "Design registration cleared after controller reset."
            )
        )
        self.stage_controller.homing_status_changed.connect(
            self.joystick_panel.set_homing_status
        )
        self.stage_controller.homing_status_changed.connect(self._persist_controller_state)
        self.stage_controller.homing_action_started.connect(
            self.joystick_panel.set_homing_action_started
        )
        self.stage_controller.homing_action_finished.connect(
            self.joystick_panel.set_homing_action_finished
        )
        self.stage_controller.homing_action_finished.connect(self._on_homing_action_finished)
        self.stage_controller.axis_a_ready_changed.connect(
            self.joystick_panel.set_axis_a_ready
        )
        self.stage_controller.needles_state_changed.connect(
            self.joystick_panel.set_needles_state
        )
        self.stage_controller.needles_state_changed.connect(self._persist_controller_state)
        self.stage_controller.needles_action_started.connect(
            self.joystick_panel.set_needles_action_started
        )
        self.stage_controller.needles_action_finished.connect(
            self.joystick_panel.set_needles_action_finished
        )
        self.joystick_panel.reset_requested.connect(
            self.stage_controller.cancel_active_task
        )
        self.stage_controller.stage_position_changed.connect(self._persist_controller_state)
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
        self.oscillation_window = QMainWindow(self)
        self.oscillation_window.setWindowTitle("Oscillation")
        self.oscillation_window.setWindowFlag(Qt.Window, True)
        self.oscillation_window.setCentralWidget(self.oscillation_panel)
        self.oscillation_window.resize(420, 320)

        self.serial_terminal_panel = SerialTerminalWindow(self)
        self.serial_terminal_panel.set_stage_controller(self.stage_controller)
        self.serial_terminal_panel.set_serial(self.serial_connection)
        self.serial_terminal_panel.manual_command_sent.connect(
            self._on_manual_terminal_command
        )
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

        self.design_layout_window = DesignLayoutWindow()
        self.design_layout_window.calibration_point_selected.connect(
            self._on_design_layout_point_selected
        )
        self.design_layout_window.visibility_changed.connect(
            self._on_design_layout_window_visibility_changed
        )
        self.design_navigator_panel = self.design_layout_window.navigator_panel
        self.design_navigator_panel.load_design_requested.connect(self._load_design_document)
        self.design_navigator_panel.unload_design_requested.connect(
            self._unload_design_document
        )
        self.design_navigator_panel.top_cell_changed.connect(self._set_design_top_cell)
        self.design_navigator_panel.layer_visibility_changed.connect(
            self._set_design_layer_visibility
        )
        self.design_navigator_panel.load_script_requested.connect(
            self._load_measurement_script
        )
        self.design_navigator_panel.reload_script_requested.connect(
            self._reload_measurement_script
        )
        self.design_navigator_panel.capture_chip_point_requested.connect(
            self._capture_calibration_chip_point
        )
        self.design_navigator_panel.clear_registration_requested.connect(
            self._clear_design_registration
        )
        self.design_navigator_panel.move_to_target_requested.connect(
            self._move_to_design_target
        )
        self.design_navigator_panel.next_target_requested.connect(
            self._select_next_design_target
        )
        self.design_navigator_panel.previous_target_requested.connect(
            self._select_previous_design_target
        )
        self.design_navigator_panel.target_selected.connect(
            self._on_design_target_selected
        )
        self._refresh_design_panel()

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
